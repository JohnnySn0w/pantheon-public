#!/usr/bin/env python3
"""cap_mute_driver.py (T-3844; cap-flag source re-pointed T-4041) — auto-mute
cap-collateral alerts while a seat is usage-capped; auto-clear the moment the cap lifts.

WHY (the operator, TG 7289 2026-09-20): during a weekly spend-cap window every capped
seat keeps tripping the monitors — no-progress reads exactly like wedged/stale/
down — and floods the operator with ~30 cap-COLLATERAL alerts. The seats are not
broken, they are capped. This driver mutes a seat the moment it is detected capped
and unmutes it the moment the cap lifts, so the noise never reaches the operator,
while a GENUINE failure resurfaces at cap-lift (and is unactionable mid-cap
regardless — blanket-while-capped, orchestrator's ruling T-3844).

CAP-SIGNAL SOURCE (T-4041 — re-pointed off breaker_open): the T-4037 mid-session-cap
detector's published state file `<state_root>/<persona>/mid-session-cap.json` (schema
`pantheon-persona-mid-session-cap/v1`, authoritative reader
`runtime_controller.read_mid_session_cap_state`). A seat is CAPPED iff `modal_present`
is True AND `observed_at` is FRESH (the detector's poller is still writing). The old
source (runtime_controller transition_state `breaker_open`, from
<persona>/runtime-controller/transition.json) was structurally BLIND to the common
case: T-3847 validated the driver against the real 2026-09-27 weekly cap and it never
fired — breaker_open is only set by the controller's START-UP subscription_cap
classification, never mid-session, and the pane never dies (orchestrator T-2985/T-4037). This
unifies the cap cluster: ONE T-4037 detector feeds the mute (this), the reap (T-4024),
and the swap (T-4037). `modal_present` is the ONLY trigger — it is already 2-sample-
debounced by the detector; the non-authoritative `candidate_modal_kind` is NEVER read.

MECHANISM: reads each fleet seat's cap flag (cross-account via sudo, same posture as
cap_modal_reaper / context_fill_monitor) and drives the SHARED notify-mute registry
(notify_policy — the same registry _seat_muted() reads to suppress alert pages):
  capped True  + not already cap-muted        -> mute(persona, reason=CAP_REASON)
  capped False + cap-muted (reason==CAP_REASON) -> unmute(persona)
It manages ONLY its own cap-auto mutes: a manual/operator mute (any other reason)
is never cleared and never overwritten.

BACKSTOP: a cap-auto mute older than MAX_CAP_MUTE_HOURS (> one weekly cap cycle) is
force-unmuted + flagged — a defence against a stuck detector leaving a seat
permanently silenced. The bias is always toward ALERTING: an unreadable/stale cap
flag never auto-mutes, and a too-old mute is dropped, so the failure direction is a
spurious page, never a missed one (the invariant _seat_muted itself keeps).

ARM GATE (T-4041 — supervised first-fire): the LIVE registry write is default-OFF.
Unarmed, the driver runs in OBSERVE mode: it reads the same cap flags, reports the
`would-*` disposition it would take, and writes NOTHING. Arm it (`--arm` / env
CAP_MUTE_ARM=1) only for the SUPERVISED first-fire and thereafter in the service unit.
This mirrors cap_modal_reaper's default-OFF `--arm`, and makes re-pointing the source
safe to land while the 5-min Recovery timer is already live.

Recovery-run (timer). The mute registry (PANTHEON_MUTE_REGISTRY, live
/var/lib/pantheon/notify-mute/notify_mute.json) is Recovery-owned + group-readable by
every seat's monitors, so one write here covers all readers.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# Dual-identity-safe imports (run-as-package in tests, run-as-script in prod) —
# same shape as the other tools/ monitors.
try:  # pragma: no cover - import shim
    from tools import notify_policy
    from tools.agent_role_map import instance_binding
except ImportError:  # pragma: no cover - bare run with cwd=tools/
    import notify_policy  # type: ignore
    from agent_role_map import instance_binding  # type: ignore

# The reason stamped on OUR mutes — the discriminator that keeps the driver from
# ever clearing (or re-stamping) a manual/operator mute set for a different reason.
CAP_REASON = "usage-cap"
CAP_MUTE_BY = "cap-auto"

# Stuck-detector backstop: a real weekly cap resolves in well under a week, so a
# cap-mute held longer than this is anomalous — force-unmute + flag rather than
# leave a seat silenced forever. Env-tunable.
MAX_CAP_MUTE_HOURS = float(os.environ.get("CAP_MUTE_MAX_HOURS", str(24 * 8)))  # 8 days

# The fleet seats that emit operator alerts. Non-seat role-map entries
# (codex) are excluded — they have no per-seat cap state to read.
FLEET_PERSONAS: tuple[str, ...] = ("orchestrator", "specialist", "recovery", "archivist", "mason", "artisan")

# Per-seat persona state root (uniform across the fleet). mid-session-cap.json is a
# sibling of active-runtime.json under _controller_persona_root — the T-2966 stateless
# tree — NOT the older <persona>/runtime-controller/ KEEP layer (transition.json). Path
# and read posture are identical to cap_modal_reaper's (the sibling T-4037 consumer).
_STATE_ROOT_TMPL = "/home/{account}/.local/state/pantheon/personas"
CAP_STATE_FILENAME = "mid-session-cap.json"
MID_SESSION_CAP_SCHEMA = "pantheon-persona-mid-session-cap/v1"  # detector's schema (doc/contract)

# observed_at freshness bound. The detector polls ~60s; a record older than this means
# its poller is not currently writing (paused / stopped / racing a teardown), so the
# state is not authoritative -> do not act on it (fail-open toward alerting). Generous
# multiple of the poll interval; env-tunable. Matches cap_modal_reaper's default.
STALE_AFTER_SECONDS = float(os.environ.get("CAP_MUTE_STALE_AFTER_SECONDS", "600"))


def _mid_session_cap_path(persona: str, account: str) -> str:
    return f"{_STATE_ROOT_TMPL.format(account=account)}/{persona}/{CAP_STATE_FILENAME}"


def _read_state_raw(path: str, account: str) -> tuple[str, str]:
    """Fetch a seat's mid-session-cap.json text. Returns (status, text) where status is
    'missing' (never written / cleared -> not capped), 'fault' (genuine read error ->
    unknown), or 'ok' (text to parse).

    SELF-ACCOUNT reads DIRECTLY — never via sudo. Recovery's sudoers grant covers the
    OTHER pantheon accounts, not self-sudo, so `sudo -n -u <self> cat` is REJECTED
    with a permission error (NOT a 'No such file'); routing recovery's own state through
    sudo would mis-read a plainly-missing file as 'fault'/unknown and leave the recovery
    seat permanently unreadable (so never cap-muted). A direct read also skips a
    needless sudo on the account the driver already runs as. Mirrors
    cap_modal_reaper._read_cap_state_raw exactly."""
    if account == getpass.getuser():
        try:
            return "ok", Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return "missing", ""
        except OSError:
            return "fault", ""
    try:
        proc = subprocess.run(
            ["sudo", "-n", "-u", account, "cat", path],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return "fault", ""
    if proc.returncode != 0:
        # Missing file (never capped) => not capped, disambiguated from a genuine
        # fault by "No such file" in stderr with no stdout. A different error with no
        # stdout is 'fault' (unknown). Partial stdout despite rc!=0 is still parsed.
        if not proc.stdout.strip() and "No such file" in (proc.stderr or ""):
            return "missing", ""
        if not proc.stdout.strip():
            return "fault", ""
    return "ok", proc.stdout


def parse_iso(ts) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp (the detector writes `...Z`). None on anything odd.
    Mirrors cap_modal_reaper.parse_iso."""
    if not isinstance(ts, str) or not ts:
        return None
    s = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def cap_flag_from_state(
    state: dict, now: datetime, *, stale_after_seconds: float = STALE_AFTER_SECONDS,
) -> bool | None:
    """PURE decision on a parsed mid-session-cap dict: True (capped), False (not capped),
    or None (unknown -> caller NEVER auto-mutes, fail-open toward alerting).

    `modal_present` is the ONLY trigger (never candidate_modal_kind). A cap requires BOTH
    modal_present True AND a FRESH observed_at — a modal_present True with a missing/stale
    observed_at is 'unknown', not 'capped', because the detector is not currently
    confirming it. modal_present anything-but-True (cleared / absent) is a definite
    not-capped (drives the unmute on cap-lift)."""
    if not isinstance(state, dict):
        return None
    if state.get("modal_present") is not True:
        return False  # cleared / never present / not the modal -> not capped
    observed_at = parse_iso(state.get("observed_at"))
    if observed_at is None:
        return None  # cannot confirm freshness -> unknown
    if (now - observed_at).total_seconds() > stale_after_seconds:
        return None  # detector poller not currently writing -> not authoritative
    return True  # modal present + fresh -> capped


def read_mid_session_cap(
    persona: str, account: str, *,
    now: datetime | None = None,
    read_raw: Callable[[str, str], tuple[str, str]] = _read_state_raw,
    stale_after_seconds: float = STALE_AFTER_SECONDS,
) -> bool | None:
    """Read `persona`'s cap flag from the T-4037 detector state.

    Returns True (capped), False (not capped / cleared / never recorded), or None on
    ANY read/parse fault or a stale/unconfirmable record — None means 'unknown', and
    the caller NEVER auto-mutes on unknown (fail-open). A MISSING mid-session-cap.json (a
    seat that has never recorded a modal, or whose detector cleared it) reads as False,
    not None: absence of a modal record is a definite not-capped. See `_read_state_raw`
    for the self-vs-cross-account read path."""
    now = now or datetime.now(timezone.utc)
    status, out = read_raw(_mid_session_cap_path(persona, account), account)
    if status == "missing":
        return False
    if status == "fault":
        return None
    out = out.strip()
    if not out:
        return False  # empty/cleared file -> not capped
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return cap_flag_from_state(data, now, stale_after_seconds=stale_after_seconds)


def _cap_mute_entry(persona: str, registry) -> dict | None:
    """The registry entry for `persona` IFF it is OUR cap-auto mute (reason matches),
    else None — so the driver only ever clears mutes it set."""
    try:
        muted = notify_policy.list_muted(registry)
    except Exception:
        return None
    entry = muted.get(persona) if isinstance(muted, dict) else None
    if isinstance(entry, dict) and entry.get("reason") == CAP_REASON:
        return entry
    return None


def _is_muted_at_all(persona: str, registry) -> bool:
    try:
        return bool(notify_policy.is_muted(persona, registry))
    except Exception:
        return False


def _mute_age_hours(entry: dict, now: datetime) -> float | None:
    since = entry.get("since")
    if not isinstance(since, str):
        return None
    try:
        dt = datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (now - dt).total_seconds() / 3600.0


def drive_seat(
    persona: str,
    account: str,
    *,
    registry=None,
    now: datetime | None = None,
    arm: bool = False,
    read_cap: Callable[[str, str], "bool | None"] = read_mid_session_cap,
) -> str:
    """Reconcile one seat's cap-mute against its cap flag. Returns a short disposition
    string (for the run summary + tests). Never raises. `read_cap` is injected so tests
    exercise every branch without a live seat.

    ARM gate: when `arm` is False (default), this is OBSERVE-only — it computes the
    disposition and writes NOTHING, returning a `would-*` string for any action it would
    take. When `arm` is True, it drives the live registry. The read-only dispositions
    (skip/hold/noop) are identical in both modes."""
    now = now or datetime.now(timezone.utc)
    capped = read_cap(persona, account)

    if capped is None:
        # Unknown cap flag -> leave mute state untouched (fail-open toward alerting).
        return "skip:unreadable"

    cap_entry = _cap_mute_entry(persona, registry)

    if capped:
        if cap_entry is None:
            if _is_muted_at_all(persona, registry):
                # Muted by someone else (manual/operator) for a non-cap reason —
                # do NOT overwrite; it is already silent, which is fine.
                return "skip:manual-mute"
            if not arm:
                return "would-mute"
            notify_policy.mute(persona, by=CAP_MUTE_BY, reason=CAP_REASON, registry=registry)
            return "muted"
        # Already cap-muted. Stuck-detector backstop: if it has been silenced longer
        # than a full cap cycle, the cap flag is not clearing as expected -> drop the
        # mute + flag, so we never silence a seat indefinitely.
        age = _mute_age_hours(cap_entry, now)
        if age is not None and age > MAX_CAP_MUTE_HOURS:
            if not arm:
                return "would-backstop-unmute:stuck-breaker"
            notify_policy.unmute(persona, registry=registry)
            return "backstop-unmute:stuck-breaker"
        return "hold:capped"

    # Not capped.
    if cap_entry is not None:
        if not arm:
            return "would-unmute:cap-lifted"
        notify_policy.unmute(persona, registry=registry)
        return "unmuted:cap-lifted"
    return "noop:uncapped"


# Dispositions that represent (or would represent, in observe mode) a registry change.
_ACTION_DISPOSITIONS = frozenset({
    "muted", "unmuted:cap-lifted", "backstop-unmute:stuck-breaker",
    "would-mute", "would-unmute:cap-lifted", "would-backstop-unmute:stuck-breaker",
})


# T-4057 observe-mode notice. The supervised first fire is event-gated on a real cap
# window, which is easy to miss between ticket re-offers. So in OBSERVE mode, recovery gets
# one mail when a seat first reaches would-mute and one when that episode ends. Dedup
# state lives in this file; a notice failure never stops the reconcile.
OBSERVE_NOTICE_STATE = Path.home() / ".local" / "state" / "pantheon" / "cap_mute_observe_notified.json"
_EPISODE_END = frozenset({"would-unmute:cap-lifted", "noop:uncapped"})


def observe_notices(results: dict[str, str], state: dict) -> tuple[list[str], dict]:
    """Pure: (notice lines, new state). A seat enters the state on its first would-mute
    and leaves it when the cap lifts; unreadable/skip dispositions leave it alone."""
    lines, new_state = [], dict(state)
    for persona, disposition in results.items():
        if disposition == "would-mute" and persona not in new_state:
            new_state[persona] = True
            lines.append(f"{persona}: would-mute (mid-session cap detected)")
        elif persona in new_state and disposition in _EPISODE_END:
            del new_state[persona]
            lines.append(f"{persona}: {disposition} (cap episode ended)")
    return lines, new_state


def _observe_notify(results: dict[str, str], notify: Callable[[list[str]], None],
                    state_path: Path, log) -> None:
    try:
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state = {}
        lines, new_state = observe_notices(results, state)
        if lines:
            notify(lines)
        if new_state != state:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(new_state))
    except Exception as exc:  # never break the reconcile; state unsaved -> retried next fire
        log(f"[cap_mute_driver] observe notice failed (will retry): {exc}")


def _mail_recovery(lines: list[str]) -> None:
    try:
        from tools import mailbox
        from tools.agent_role_map import mailbox_hub
    except ImportError:  # bare run with cwd=tools/
        import mailbox  # type: ignore
        from agent_role_map import mailbox_hub  # type: ignore
    body = ("cap_mute_driver OBSERVE (T-4057 supervised first fire):\n"
            + "\n".join(f"  - {line}" for line in lines)
            + "\n\nIf a seat is capped now: confirm the would-* lines in the observe log, "
              "then arm and verify per T-4057 steps 2-4.")
    mailbox.send(f"{mailbox_hub()}/recovery", body, getpass.getuser(),
                 subject=f"cap_mute_driver observe: {lines[0]}")


def run(personas: tuple[str, ...] = FLEET_PERSONAS, *, registry=None, arm: bool = False,
        read_cap: Callable[[str, str], "bool | None"] = read_mid_session_cap,
        log=print, notify: Callable[[list[str]], None] | None = None,
        notice_state: Path | None = None) -> dict[str, str]:
    """Drive every fleet seat once. Returns {persona: disposition}. Observe-only unless
    `arm` is True (see drive_seat). With `notify`, OBSERVE mode also sends the T-4057
    first-fire notice (see observe_notices)."""
    results: dict[str, str] = {}
    for persona in personas:
        try:
            account = instance_binding(persona).get("account")
        except Exception:
            account = None
        if not account:
            results[persona] = "skip:no-account"
            continue
        try:
            results[persona] = drive_seat(persona, account, registry=registry, now=None,
                                          arm=arm, read_cap=read_cap)
        except notify_policy.RegistryUnreadable as exc:
            # T-5396: the registry exists but cannot be read, so a write would wipe every
            # other mute. notify_policy refuses it; record it and carry on with the rest.
            results[persona] = "error:registry-unreadable"
            log(f"[cap_mute_driver] {persona}: registry unreadable, nothing written: {exc}")
    changed = {p: d for p, d in results.items() if d in _ACTION_DISPOSITIONS}
    mode = "ARMED" if arm else "OBSERVE"
    log(f"[cap_mute_driver] {mode} {len(personas)} seats; changes={changed or 'none'}; all={results}")
    if notify is not None and not arm:
        _observe_notify(results, notify, notice_state or OBSERVE_NOTICE_STATE, log)
    return results


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Auto-mute cap-collateral alerts while a seat is usage-capped (T-3844; source=T-4037 detector, T-4041).")
    p.add_argument("--persona", help="drive a single persona (default: whole fleet)")
    p.add_argument("--registry", help="mute registry (default env PANTHEON_MUTE_REGISTRY or notify_policy default)")
    p.add_argument("--arm", action="store_true",
                   default=os.environ.get("CAP_MUTE_ARM", "").lower() in ("1", "true", "yes"),
                   help="write the live mute registry (default OFF: observe-only, for the T-4041 supervised first-fire)")
    args = p.parse_args(argv)
    personas = (args.persona,) if args.persona else FLEET_PERSONAS
    run(personas, registry=args.registry, arm=args.arm, notify=_mail_recovery)
    return 0


if __name__ == "__main__":
    sys.exit(main())
