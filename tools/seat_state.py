#!/usr/bin/env python3
"""tools/seat_state.py - ORG-106 L0 (T-5523): seat-state record schema + read-only reader.

One reader over every mechanism that holds a seat back, so consumers never read
private flags. Contract: notes/2026-10-26-org106-seat-state-mechanism.md sections
3-6, with one deviation (D6, orchestrator-flagged): the record lives at
``<state_root>/personas/<persona>/overrides/seat-state.json``, beside holds.json
(``harness_state.seat_state_path``), not under runtime-controller/.

Sources read by ``effective()``: the seat-state record, seat maintenance (T-5282
registry, via notify_policy), the OOM hold (ORG-104 holds.json, read as raw JSON),
and, only when a path is passed, the legacy ``autonomy_off`` file.

FAIL-OPEN, per source: an unreadable or malformed source records a short ``fault``
and contributes nothing; ``effective()`` never raises for an I/O or parse fault
(an invalid persona name raises HarnessStateError, as harness_state does).

Known limitation: notify_policy swallows its own maintenance-registry faults, so a
corrupt maintenance registry reads as "no maintenance" with no ``fault`` reported.

Read-only. Writers (L1+) live in harness.py. No dependencies beyond the stdlib and
this repo's modules.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import agent_role_map  # noqa: E402
from tools import harness_state as hs  # noqa: E402
from tools import notify_policy  # noqa: E402

SCHEMA = hs.SCHEMA_SEAT_STATE

STATE_ACTIVE = "active"
STATE_PAUSED_INTAKE = "paused-intake"
STATE_DEEP_SLEEP = "deep-sleep"
#: T-5868 (ORG-106 amendment, notes s10): a run_state pause that halted partway. Alertable:
#: intake stays blocked, repair is withheld, and every reader shows it as an INCIDENT.
STATE_HALTED = "halted"
RECORD_STATES = (STATE_PAUSED_INTAKE, STATE_DEEP_SLEEP, STATE_HALTED)
#: T-5868: extra keys a halted record must carry (notes s10).
HALTED_REQUIRED_KEYS = ("run_id", "op_id", "pause_id", "rollback_cmd")


def halted_rollback_cmd(run_id: str) -> str:
    """The exact rollback line a halted record carries for ``run_id``. Mirrors
    ``tools.germinate.engine.escalation.rollback_command`` (seat_state must not import germinate);
    a parity test pins the two equal."""
    return f"python3 -m tools.germinate rollback --run {run_id}"


#: T-5918: the record key a harness-manager run_state pause (T-5858, U5) carries. A valid,
#: unexpired paused-intake record with a non-empty string here is a U5 pause: its wake timers
#: are disabled on purpose, so automated unit repair (pantheon-parity-remediate) must not
#: re-enable them. An operator `harness pause` (L1) never writes this key.
RUN_STATE_PAUSE_KEY = "pause_id"
#: T-5918: record states that veto automated unit repair WITHOUT standing the monitors down
#: (repair_vetoed, not remediation_vetoed). T-5868: 'halted' keeps vetoing repair while it pages.
REPAIR_ONLY_VETO_STATES: tuple[str, ...] = (STATE_HALTED,)

PHASE_SET = "set"
PHASE_DRAINING = "draining"
PHASE_ASLEEP = "asleep"
PHASE_HALTED = "halted"
PHASES_BY_STATE = {
    STATE_PAUSED_INTAKE: (PHASE_SET,),
    STATE_DEEP_SLEEP: (PHASE_DRAINING, PHASE_ASLEEP),
    STATE_HALTED: (PHASE_HALTED,),
}

#: ORG-106 L3b: how a deep-sleep drain reached `asleep` (record key `drain_outcome`).
DRAIN_OUTCOMES = ("handoff", "idle", "timeout", "undelivered", "no-session")

# Note section 4 values (build defaults; L1+ import these, define once here).
TTL_DEFAULT = {
    STATE_PAUSED_INTAKE: timedelta(hours=4),
    STATE_DEEP_SLEEP: timedelta(hours=24),
}
TTL_MAX = {
    STATE_PAUSED_INTAKE: timedelta(days=7),
    STATE_DEEP_SLEEP: timedelta(days=14),
}
PRE_EXPIRY_NOTICE = timedelta(hours=1)
INDEFINITE_FLAG_AGE = timedelta(days=7)
TELEGRAM_DARK_THRESHOLD = timedelta(hours=20)

REASON_MAX = 200

SRC_SEAT_STATE = "seat-state"
SRC_MAINTENANCE = "maintenance"
SRC_OOM = "oom-hold"
SRC_AUTONOMY_OFF = "autonomy_off"

DEFAULT_OOM_HOLDS = Path("/var/lib/pantheon/oom/holds.json")
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class Contribution:
    """What one source adds. An empty Contribution (all defaults) adds nothing."""

    source: str = ""
    state: str = STATE_ACTIVE
    intake_blocked: bool = False
    launch_blocked: bool = False
    remediation_vetoed: bool = False
    expected_down: bool = False
    #: T-5918: vetoes automated unit repair only (see vetoes_repair); every other reader
    #: ignores it, so it never stands a monitor down.
    repair_vetoed: bool = False
    #: T-5868: an INCIDENT, never an authorised quiet pause (halted). Readers that suppress
    #: on a valid record must alert instead.
    alertable: bool = False
    rollback_cmd: str | None = None
    owner: str | None = None
    reason: str | None = None
    expires: str | None = None
    fault: str | None = None

    @property
    def active(self) -> bool:
        return bool(self.source)


@dataclass(frozen=True)
class EffectiveSeatState:
    state: str
    intake_blocked: bool
    launch_blocked: bool
    remediation_vetoed: bool
    expected_down: bool
    sources: tuple[str, ...]
    owner: str | None
    reason: str | None
    expires: str | None
    fault: str | None
    #: T-5918: True when automated unit repair must be withheld although the seat is not
    #: held down (a U5 run_state pause; T-5868's halted state). remediation_vetoed keeps its
    #: meaning (maintenance or deep-sleep: monitors stand down). Read through vetoes_repair().
    repair_vetoed: bool = False
    #: T-5868: True for a halted record. Default False keeps every other source unchanged.
    alertable: bool = False
    #: T-5868: the exact `python3 -m tools.germinate rollback --run <run_id>` line of a halted record, else None.
    rollback_cmd: str | None = None


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, _TS_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _as_utc(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now


def validate_record(doc: object) -> list[str]:
    """Problems with a seat-state record (empty list means valid). Never raises."""
    if not isinstance(doc, dict):
        return [f"record is {type(doc).__name__}, expected an object"]
    problems: list[str] = []
    if doc.get("schema") != SCHEMA:
        problems.append(f"schema {doc.get('schema')!r} != {SCHEMA!r}")
    state = doc.get("state")
    if state not in RECORD_STATES:
        problems.append(f"state {state!r} not in {list(RECORD_STATES)}")
    reason = doc.get("reason")
    if not isinstance(reason, str) or not (1 <= len(reason) <= REASON_MAX) \
            or not reason.isprintable():
        problems.append(f"reason must be 1-{REASON_MAX} printable chars")
    indefinite = doc.get("indefinite")
    if not isinstance(indefinite, bool):
        problems.append("indefinite must be a boolean")
    expires = doc.get("expires")
    if expires is None:
        if indefinite is False:
            problems.append("expires is null but indefinite is false")
    else:
        if indefinite is True:
            problems.append("expires is set but indefinite is true")
        if _parse_ts(expires) is None:
            problems.append(f"expires {expires!r} is not an ISO-Z timestamp")
    if state == STATE_HALTED:
        if indefinite is not True:
            problems.append("halted requires indefinite true")
        if expires is not None:
            problems.append("halted requires expires null")
        for key in HALTED_REQUIRED_KEYS:
            v = doc.get(key)
            if not isinstance(v, str) or not v.strip():
                problems.append(f"halted requires a non-empty string {key}")
        cmd = doc.get("rollback_cmd")
        run_id = doc.get("run_id")
        if isinstance(cmd, str) and isinstance(run_id, str) and run_id.strip() and cmd != halted_rollback_cmd(run_id):
            problems.append(f"rollback_cmd must equal {halted_rollback_cmd(run_id)!r} (exact, for the record's run_id)")
        if _parse_ts(doc.get("halted_at")) is None:
            problems.append(f"halted_at {doc.get('halted_at')!r} is not an ISO-Z timestamp")
    if isinstance(state, str) and state in PHASES_BY_STATE and doc.get("phase") not in PHASES_BY_STATE[state]:
        problems.append(
            f"phase {doc.get('phase')!r} invalid for {state} "
            f"(want one of {list(PHASES_BY_STATE[state])})")
    for key in ("set_ts", "phase_ts"):
        if _parse_ts(doc.get(key)) is None:
            problems.append(f"{key} {doc.get(key)!r} is not an ISO-Z timestamp")
    # ORG-106 L3b: optional drain bookkeeping (deep-sleep only; absent on every other record).
    for key in ("drain_started_ts", "handoff_prompted_ts"):
        if key in doc and doc[key] is not None and _parse_ts(doc[key]) is None:
            problems.append(f"{key} {doc[key]!r} is not an ISO-Z timestamp")
    outcome = doc.get("drain_outcome")
    if outcome is not None and outcome not in DRAIN_OUTCOMES:
        problems.append(f"drain_outcome {outcome!r} not in {list(DRAIN_OUTCOMES)}")
    return problems


def parse_record(doc: object, *, now: datetime | None = None) -> Contribution:
    """Pure: a seat-state document -> its Contribution. No I/O, so L4 can feed it a
    document read across accounts. Expired -> empty; malformed -> fault only."""
    problems = validate_record(doc)
    if problems:
        return Contribution(fault=f"{SRC_SEAT_STATE}: invalid record: {'; '.join(problems)}")
    assert isinstance(doc, dict)
    expires = doc.get("expires")
    if expires is not None and _parse_ts(expires) <= _as_utc(now):  # type: ignore[operator]
        return Contribution()
    state = doc["state"]
    sleeping = state == STATE_DEEP_SLEEP
    halted = state == STATE_HALTED
    return Contribution(
        source=SRC_SEAT_STATE,
        state=state,
        intake_blocked=True,
        launch_blocked=sleeping,
        remediation_vetoed=sleeping,
        expected_down=sleeping and doc["phase"] == PHASE_ASLEEP,
        repair_vetoed=state in REPAIR_ONLY_VETO_STATES or is_run_state_pause(doc),  # T-5918
        alertable=halted,
        rollback_cmd=doc["rollback_cmd"] if halted else None,
        owner=doc.get("owner") if isinstance(doc.get("owner"), str) else None,
        reason=doc["reason"],
        expires=expires,
    )


def is_run_state_pause(doc: object) -> bool:
    """T-5918: is this record a harness-manager run_state pause (T-5858, U5)? Pure; the
    caller has validated it. A halted U5 pause keeps the key (Recovery D15 T-5858 (r1)), so it
    stays a run_state pause until rollback(run_id) or restore clears the record.
    T-5868 C2: true for a HALTED record too, as a repair-veto input ONLY. Its sole caller
    is parse_record's repair_vetoed; no alert-suppression path may key on it."""
    if not isinstance(doc, dict) or doc.get("state") not in (STATE_PAUSED_INTAKE, STATE_HALTED):
        return False
    marker = doc.get(RUN_STATE_PAUSE_KEY)
    return isinstance(marker, str) and bool(marker.strip())


def _read_seat_state(persona: str, now: datetime, state_root: Path | None) -> Contribution:
    try:
        root = state_root if state_root is not None else hs.state_root_from_env(
            Path.home() / ".local" / "state" / "pantheon")
        doc = hs.load_seat_state(persona, root=Path(root))
    except hs.HarnessStateError as exc:
        return Contribution(fault=f"{SRC_SEAT_STATE}: {exc}")
    except Exception as exc:  # fail-open
        return Contribution(fault=f"{SRC_SEAT_STATE}: {type(exc).__name__}: {exc}")
    if doc is None:
        return Contribution()
    try:
        return parse_record(doc, now=now)
    except Exception as exc:  # fail-open, defense in depth
        return Contribution(fault=f"{SRC_SEAT_STATE}: {type(exc).__name__}: {exc}")


def _read_maintenance(persona: str, now: datetime, registry: Any) -> Contribution:
    try:
        now_iso = now.astimezone(timezone.utc).strftime(_TS_FMT)
        if not notify_policy.in_maintenance(persona, registry, now_iso):
            return Contribution()
        entry = notify_policy.list_maintenance(registry).get(persona) or {}
        return Contribution(
            source=SRC_MAINTENANCE, intake_blocked=True, remediation_vetoed=True,
            owner=entry.get("by") or None, reason=entry.get("reason") or None,
            expires=entry.get("expires"))
    except Exception as exc:  # fail-open
        return Contribution(fault=f"{SRC_MAINTENANCE}: {type(exc).__name__}: {exc}")


def _read_oom(persona: str, path: Any) -> Contribution:
    try:
        p = Path(path)
        if not p.exists():
            return Contribution()
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return Contribution(fault=f"{SRC_OOM}: expected an object in {p}")
        entry = data.get(persona)
        if entry is None:
            return Contribution()
        reason = entry.get("reason") if isinstance(entry, dict) else None
        return Contribution(
            source=SRC_OOM, intake_blocked=True, launch_blocked=True,
            owner="oom-killer", reason=reason if isinstance(reason, str) else None)
    except Exception as exc:  # fail-open
        return Contribution(fault=f"{SRC_OOM}: {type(exc).__name__}: {exc}")


def _read_autonomy_off(path: Any) -> Contribution:
    try:
        if not os.path.lexists(path):
            return Contribution()
        return Contribution(
            source=SRC_AUTONOMY_OFF, intake_blocked=True, owner="legacy",
            reason="legacy autonomy_off file present")
    except Exception as exc:  # fail-open
        return Contribution(fault=f"{SRC_AUTONOMY_OFF}: {type(exc).__name__}: {exc}")


def _combine(record: Contribution, maint: Contribution, oom: Contribution,
             legacy: Contribution) -> EffectiveSeatState:
    """Aggregate the four source contributions. Shared by effective() / effective_for()."""
    # Display precedence: OOM > HALTED > deep-sleep > maintenance > paused-intake > autonomy_off.
    rank = {
        SRC_OOM: 0,
        SRC_MAINTENANCE: 3,
        SRC_AUTONOMY_OFF: 5,
    }

    def _rank(c: Contribution) -> int:
        if c.source == SRC_SEAT_STATE:
            return {STATE_HALTED: 1, STATE_DEEP_SLEEP: 2}.get(c.state, 4)
        return rank[c.source]

    parts = [c for c in (record, maint, oom, legacy) if c.active]
    ordered = sorted(parts, key=_rank)
    top = ordered[0] if ordered else Contribution()
    faults = [c.fault for c in (record, maint, oom, legacy) if c.fault]
    return EffectiveSeatState(
        state=record.state,
        intake_blocked=any(c.intake_blocked for c in parts),
        launch_blocked=any(c.launch_blocked for c in parts),
        remediation_vetoed=any(c.remediation_vetoed for c in parts),
        expected_down=any(c.expected_down for c in parts),
        repair_vetoed=any(c.repair_vetoed for c in parts),
        alertable=any(c.alertable for c in parts),
        rollback_cmd=record.rollback_cmd,
        sources=tuple(c.source for c in ordered),
        owner=top.owner, reason=top.reason, expires=top.expires,
        fault="; ".join(faults) if faults else None,
    )


def effective(persona: str, *, now: datetime | None = None,
              state_root: Path | None = None, maintenance_registry: Any = None,
              oom_holds_path: Any = DEFAULT_OOM_HOLDS,
              autonomy_off_path: Any = None) -> EffectiveSeatState:
    """The seat's effective hold state across all sources. See module docstring."""
    hs.validate_persona(persona)
    when = _as_utc(now)
    record = _read_seat_state(persona, when, state_root)
    maint = _read_maintenance(persona, when, maintenance_registry)
    oom = _read_oom(persona, oom_holds_path)
    legacy = (_read_autonomy_off(autonomy_off_path)
              if autonomy_off_path is not None else Contribution())
    return _combine(record, maint, oom, legacy)


def _read_seat_state_cross(persona: str, account: str, now: datetime,
                           runner: Callable[..., Any], home_root: str) -> Contribution:
    """Another account's seat-state record, fetched with `sudo -n -u <account> cat`
    (cap_mute_driver._read_state_raw posture). Missing file -> no record; any other
    failure -> a fault Contribution (fail-open, contributes nothing)."""
    path = hs.seat_state_path(
        persona, root=Path(home_root) / account / ".local" / "state" / "pantheon")
    try:
        proc = runner(["sudo", "-n", "-u", account, "cat", str(path)],
                      capture_output=True, text=True, timeout=15)
        stdout = proc.stdout or ""
        if proc.returncode != 0 and not stdout.strip():
            if "No such file" in (proc.stderr or ""):
                return Contribution()
            return Contribution(fault=f"{SRC_SEAT_STATE}: cross-account read of {account} "
                                      f"failed (rc={proc.returncode})")
        if not stdout.strip():
            return Contribution()
        return parse_record(json.loads(stdout), now=now)
    except Exception as exc:  # fail-open (timeout, OSError, bad JSON, ...)
        return Contribution(fault=f"{SRC_SEAT_STATE}: {type(exc).__name__}: {exc}")


def effective_for(persona: str, *, now: datetime | None = None,
                  state_root: Path | None = None, maintenance_registry: Any = None,
                  oom_holds_path: Any = DEFAULT_OOM_HOLDS,
                  autonomy_off_path: Any = None,
                  runner: Callable[..., Any] = subprocess.run,
                  home_root: str = "/home") -> EffectiveSeatState:
    """effective() for a caller that may run as a different OS account than the seat.

    The persona's account comes from agent_role_map.instance_binding. Same account as
    the caller (or no binding): the record is read directly, never via the privilege
    helper. Another account: the record text is fetched with `sudo -n -u <account> cat`.
    Maintenance and the OOM hold are cross-account shared files, read directly either way.
    autonomy_off_path is the caller's own file, so only pass it for the caller's seat."""
    hs.validate_persona(persona)
    when = _as_utc(now)
    try:
        account = agent_role_map.instance_binding(persona).get("account")
    except Exception:  # fail-open: an unknown binding reads as self
        account = None
    if not account or account == getpass.getuser():
        record = _read_seat_state(persona, when, state_root)
    else:
        record = _read_seat_state_cross(persona, account, when, runner, home_root)
    maint = _read_maintenance(persona, when, maintenance_registry)
    oom = _read_oom(persona, oom_holds_path)
    legacy = (_read_autonomy_off(autonomy_off_path)
              if autonomy_off_path is not None else Contribution())
    return _combine(record, maint, oom, legacy)


def safe_effective_for(persona: str, **kw: Any) -> EffectiveSeatState | None:
    """ORG-106 L4b: effective_for() for monitors. ANY raise (including an invalid persona
    name) reads None = "unknown", which every caller treats as active/not-held, so a broken
    reader can never silence a page or veto a repair."""
    try:
        return effective_for(persona, **kw)
    except Exception:  # noqa: BLE001 - fail-open
        return None


def vetoes_repair(e: EffectiveSeatState | None) -> bool:
    """T-5918: must automated unit repair (re-enabling or starting the seat's units) be
    withheld? True for seat maintenance or deep-sleep (remediation_vetoed) and for a U5
    run_state pause or a REPAIR_ONLY_VETO_STATES record (repair_vetoed). None (reader
    unknown) is False: fail-open, a broken reader never vetoes a repair."""
    return bool(e is not None and (e.remediation_vetoed or e.repair_vetoed))


def remediation_vetoed(persona: str, **kw: Any) -> bool:
    """True iff repair actions on this seat must be withheld: vetoes_repair() over
    safe_effective_for(), i.e. seat maintenance, deep-sleep, or a U5 run_state pause (T-5918;
    the reader does NOT set it for an OOM hold; note section 6). NOT the same as the
    EffectiveSeatState.remediation_vetoed field, which the monitors read as "stand down" and
    which a U5 pause leaves False. Fail-open (False)."""
    return vetoes_repair(safe_effective_for(persona, **kw))


def maintenance_active(persona: str, on_fault: Callable[[Exception], None] | None = None) -> bool:
    """notify_policy.in_maintenance, fail-open (any raise reads False). The one shared copy
    of the wrapper the monitors each carried privately; reads ONLY the maintenance registry
    (no seat-state read, so no cross-account sudo)."""
    try:
        return bool(notify_policy.in_maintenance(persona))
    except Exception as exc:  # noqa: BLE001 - a registry fault must never silence an alert
        if on_fault is not None:
            try:
                on_fault(exc)       # lets a site keep its own fail-open log line
            except Exception:  # noqa: BLE001
                pass
        return False


def _format_text(persona: str, e: EffectiveSeatState) -> str:
    lines = [f"{persona}: {e.state}"]
    flags = [n for n in ("intake_blocked", "launch_blocked", "remediation_vetoed",
                         "repair_vetoed", "alertable", "expected_down") if getattr(e, n)]
    lines.append("  blocks: " + (", ".join(flags) if flags else "none"))
    if e.sources:
        lines.append("  sources: " + ", ".join(e.sources))
        lines.append(f"  owner: {e.owner or '-'}")
        lines.append(f"  reason: {e.reason or '-'}")
        lines.append(f"  expires: {e.expires or 'never'}")
    if e.fault:
        lines.append(f"  fault: {e.fault}")
    return "\n".join(lines)


LAUNCH_CHECK_BLOCKED = 3  # `launch-check`: deep-sleep in force. 0 = allowed (also on a fault).


def _launch_check(args: argparse.Namespace) -> int:
    """ORG-106 L3a: cheap gate for shell launchers. 0 = launch allowed, 3 = blocked by
    deep-sleep. FAIL-OPEN: any fault exits 0 with a stderr line (the controller's
    launch_breach tripwire and L4's sleep_breach are the backstops)."""
    try:
        persona = args.persona
        if not persona and args.account:
            persona = agent_role_map.account_persona(args.account)
        if not persona:
            print(f"launch-check: no persona for account {args.account!r}; allowing (fail-open)",
                  file=sys.stderr)
            return 0
        hs.validate_persona(persona)
        if args.state_root is not None:  # an explicit root is read directly (tests, relocated state)
            e = effective(persona, state_root=args.state_root, oom_holds_path=args.oom_holds)
        else:
            e = effective_for(persona, oom_holds_path=args.oom_holds)
    except Exception as exc:  # noqa: BLE001 - fail-open
        print(f"launch-check: fault ({type(exc).__name__}: {exc}); allowing (fail-open)",
              file=sys.stderr)
        return 0
    if e.state == STATE_DEEP_SLEEP:
        print(f"launch-check: {persona} is in deep-sleep ({e.reason or '-'}; owner {e.owner or '-'}; "
              f"expires {e.expires or 'never'}); launch refused, wake it first", file=sys.stderr)
        return LAUNCH_CHECK_BLOCKED
    if e.fault:
        print(f"launch-check: fault ({e.fault}); allowing (fail-open)", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only ORG-106 seat-state reader.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    lc = sub.add_parser("launch-check", help="exit 0 if a session may be launched, 3 if deep-sleep blocks it")
    lc.add_argument("persona", nargs="?", default=None)
    lc.add_argument("--account", default=None, help="OS account (resolved to its persona)")
    lc.add_argument("--state-root", type=Path, default=None)
    lc.add_argument("--oom-holds", type=Path, default=DEFAULT_OOM_HOLDS)
    st = sub.add_parser("status", help="show the effective seat state")
    st.add_argument("persona")
    st.add_argument("--json", action="store_true")
    st.add_argument("--state-root", type=Path, default=None)
    st.add_argument("--maintenance-registry", default=None)
    st.add_argument("--oom-holds", type=Path, default=DEFAULT_OOM_HOLDS)
    st.add_argument("--autonomy-off", type=Path, default=None,
                    help="legacy file; only this account's own file is meaningful")
    args = parser.parse_args(argv)
    if args.cmd == "launch-check":
        return _launch_check(args)
    try:
        e = effective(args.persona, state_root=args.state_root,
                      maintenance_registry=args.maintenance_registry,
                      oom_holds_path=args.oom_holds, autonomy_off_path=args.autonomy_off)
    except hs.HarnessStateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        doc = asdict(e)
        doc["sources"] = list(e.sources)
        print(json.dumps(doc, indent=2, sort_keys=True))
    else:
        print(_format_text(args.persona, e))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
