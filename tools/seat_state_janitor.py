#!/usr/bin/env python3
"""tools/seat_state_janitor.py - ORG-106 L5 (T-5529): seat-state expiry lifecycle.

Notification and hygiene ONLY. Correctness never depends on this module: the L0 reader
already treats an expired record as ACTIVE. Runs as the seat account against the seat's
own record. Each tick, under the shared per-seat lock hs.seat_state_lock, NON-BLOCKING
(busy -> skip the tick, never wait; mail is sent OUTSIDE the lock and the follow-up write
re-checks the record):

  * live, not indefinite, <= PRE_EXPIRY_NOTICE left, not yet notified -> mail the owner
    once, THEN set pre_expiry_notified (so a failed send retries next tick);
  * expired -> tombstone (ended_by "expiry"), unlink the record, mail "pause expired";
    tombstone-before-unlink, same order as `harness wake` (L1);
  * indefinite and older than INDEFINITE_FLAG_AGE -> at most one nag per NAG_INTERVAL,
    last-nag time kept in a sidecar (overrides/seat-state.janitor.json) so the v1
    record schema is untouched.

`harness pause`/`wake` take the same lock, so a refresh can never interleave with a
janitor write. A corrupt or invalid record is logged and left alone (wake repairs it).
Work per tick is bounded: at most MAX_RECIPIENTS mails and TICK_DEADLINE_SECONDS.

CLI: python3 tools/seat_state_janitor.py tick <persona> [--dry-run] [--state-root P]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import agent_role_map  # noqa: E402
from tools import harness_state as hs  # noqa: E402
from tools import seat_sleep  # noqa: E402
from tools import seat_state as ss  # noqa: E402

NAG_INTERVAL = timedelta(hours=24)
FALLBACK_RECIPIENT = "orchestrator"
MAX_RECIPIENTS = 8
TICK_DEADLINE_SECONDS = 20.0
SIDECAR_SCHEMA = "pantheon-seat-state-janitor/v1"
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

SendMail = Callable[[str, str, str], None]  # (recipient_persona, subject, payload); raises on failure


def _log(msg: str) -> None:
    try:
        print(f"[seat_state_janitor] {msg}", file=sys.stderr)
    except Exception:
        pass


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, _TS_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(_TS_FMT)


def sidecar_path(persona: str, *, root: Path) -> Path:
    return hs.seat_state_path(persona, root=root).with_name("seat-state.janitor.json")


def resolve_recipients(owner: object) -> list[str]:
    """Owner string -> personas to mail (deduped, capped). Never empty: unknown -> orchestrator.
    operator is not mailed directly; orchestrator relays."""
    found: list[str] = []
    try:
        if isinstance(owner, str) and owner.startswith("role:"):
            role = owner[len("role:"):]
            members: list[str] = []
            try:
                members = sorted(agent_role_map.role_members(role))
            except Exception:
                members = []
            if not members:
                members = list(agent_role_map.personas_for_role_id(role))
            found = members
        elif isinstance(owner, str) and owner.startswith("operator:"):
            persona = agent_role_map.account_persona(owner[len("operator:"):])
            found = [persona] if persona else []
        elif isinstance(owner, str) and owner and owner != "operator":
            found = [owner]
    except Exception as exc:
        _log(f"owner routing fault for {owner!r}: {type(exc).__name__}: {exc}")
        found = []
    valid: list[str] = []
    for p in found:
        try:
            hs.validate_persona(p)
        except Exception:
            continue
        if p not in valid:
            valid.append(p)
    return (valid or [FALLBACK_RECIPIENT])[:MAX_RECIPIENTS]


def _default_send_mail(sender: str) -> SendMail:
    def send(recipient: str, subject: str, payload: str) -> None:
        from tools import mailbox  # absolute tools path, never stdlib mailbox

        hub = agent_role_map.current_mailbox_hub()
        mailbox.send(str(Path(hub) / recipient), payload, sender=sender,
                     subject=subject, severity="normal")
    return send


def _notify(owner: object, subject: str, payload: str, send_mail: SendMail,
            deadline: float) -> bool:
    """Mail every recipient; True only if all sends succeeded within the deadline.
    mailbox.send has no timeout of its own: it is local-fs and bounded here by
    MAX_RECIPIENTS sends and the tick deadline."""
    ok = True
    for r in resolve_recipients(owner):
        if time.monotonic() > deadline:
            _log("tick deadline reached; remaining notices retry next tick")
            return False
        try:
            send_mail(r, subject, payload)
        except Exception as exc:
            _log(f"mail to {r} failed: {type(exc).__name__}: {exc}")
            ok = False
    return ok


def _read_sidecar(persona: str, root: Path, set_ts: str) -> str | None:
    """last_nag_ts for THIS record (keyed by set_ts), else None. Corrupt -> None."""
    try:
        doc = json.loads(sidecar_path(persona, root=root).read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(doc, dict) and doc.get("set_ts") == set_ts and isinstance(doc.get("last_nag_ts"), str):
        return doc["last_nag_ts"]
    return None


def _write_sidecar(persona: str, root: Path, set_ts: str, last_nag_ts: str) -> None:
    hs._atomic_json(sidecar_path(persona, root=root),
                    {"schema": SIDECAR_SCHEMA, "set_ts": set_ts, "last_nag_ts": last_nag_ts})


def _how_to_extend(persona: str) -> str:
    return (f"To extend: harness {persona} pause --for <duration> --reason \"...\" "
            f"(refreshes the record). To end it now: harness {persona} wake --reason \"...\".")


def _key(record: dict) -> tuple:
    """Identity of one lifecycle: a pause refresh changes set_ts (and usually the rest)."""
    return (record.get("set_ts"), record.get("expires"), record.get("owner"))


def _classify(record: dict, when: datetime, root: Path, persona: str) -> str | None:
    """'expire' | 'pre_expiry_notice' | 'indefinite_nag' | None."""
    expires = _parse(record.get("expires"))
    if expires is not None:
        if expires <= when:
            return "expire"
        if expires - when <= ss.PRE_EXPIRY_NOTICE and not record.get("pre_expiry_notified"):
            return "pre_expiry_notice"
        return None
    set_ts = _parse(record.get("set_ts"))
    if set_ts is None or when - set_ts < ss.INDEFINITE_FLAG_AGE:
        return None
    last = _parse(_read_sidecar(persona, root, record["set_ts"]))
    if last is not None and when - last < NAG_INTERVAL:
        return None
    return "indefinite_nag"


def _load_valid(persona: str, root: Path) -> dict | None:
    """The record if present and valid; None (logged) if absent, unreadable or invalid."""
    try:
        record = hs.load_seat_state(persona, root=root)
    except Exception as exc:
        _log(f"{persona}: unreadable record, leaving it for wake: {exc}")
        return None
    if record is None:
        return None
    problems = ss.validate_record(record)
    if problems:
        _log(f"{persona}: invalid record, no action (wake repairs): {'; '.join(problems)}")
        return None
    return record


def tick(persona: str, *, now: datetime | None = None, state_root: Path | None = None,
         send_mail: SendMail | None = None, dry_run: bool = False,
         hold_dir: Path | None = None) -> list[str]:
    """One janitor pass over the seat's own record. Returns the actions taken (strings).
    Never raises for I/O, parse or mail faults.

    Decide + any record mutation happen under the lock on a FRESH load; mail goes
    OUTSIDE the lock; the follow-up write re-locks, re-loads and applies only if the
    record is still the same lifecycle (set_ts/expires/owner), merging into the fresh
    record so a concurrent `harness pause` refresh is never overwritten."""
    hs.validate_persona(persona)
    try:
        return _tick_locked(persona, now=now, state_root=state_root,
                            send_mail=send_mail, dry_run=dry_run, hold_dir=hold_dir)
    except hs.SeatStateBusy:
        _log(f"{persona}: seat-state lock busy, skipping this tick")
        return []
    except Exception as exc:
        _log(f"{persona}: janitor fault (fail-open): {type(exc).__name__}: {exc}")
        return []


def _tick_locked(persona: str, *, now: datetime | None = None, state_root: Path | None = None,
                 send_mail: SendMail | None = None, dry_run: bool = False,
                 hold_dir: Path | None = None) -> list[str]:
    """tick()'s worker (keyword parity with tick, per test_locked_wrapper_parity): resolves
    defaults, then runs the lock/mail/re-lock sequence. May raise; tick() isolates faults."""
    when = ss._as_utc(now)
    root = Path(state_root) if state_root is not None else hs.state_root_from_env(
        Path.home() / ".local" / "state" / "pantheon")
    mail = send_mail if send_mail is not None else _default_send_mail(persona)
    deadline = time.monotonic() + TICK_DEADLINE_SECONDS
    if dry_run:
        record = _load_valid(persona, root)
        kind = _classify(record, when, root, persona) if record else None
        return [kind] if kind else []
    flag_failed = False
    with hs.seat_state_lock(persona, root=root, blocking=False):
        record = _load_valid(persona, root)
        kind = _classify(record, when, root, persona) if record else None
        if kind is None:
            return []
        if kind == "expire":
            if record.get("state") == ss.STATE_DEEP_SLEEP:
                # ORG-106 L3a: the system-orchestrator hold-down flag goes first (as in
                # `harness wake`). A removal failure leaves the (already inert, expired)
                # record for the next tick instead of ending it with the flag stuck.
                try:
                    seat_sleep.clear_sysorch_flag_accounted(persona, root=root, hold_dir=hold_dir, now=when)
                except OSError as exc:
                    _log(f"{persona}: oom-hold flag not removed ({exc}); record kept, retry next tick")
                    flag_failed = True
            if not flag_failed:
                # tombstone before unlink (same order as `harness wake`), on the fresh record
                hs.append_seat_tombstone(
                    persona, {**record, "ended_ts": _fmt(when), "ended_by": "expiry"}, root=root)
                hs.clear_seat_state(persona, root=root)
    if flag_failed:
        # ORG-106 L3b: a repeatedly stuck flag pages (cooldown-gated), outside the lock.
        seat_sleep.maybe_alert_flag_failure(persona, root, now=when)
        return ["expire_flag_failed"]
    owner, reason = record.get("owner"), record["reason"]
    if kind == "expire":
        _notify(owner, f"{persona}: pause expired",
                f"The {record['state']} on {persona} (reason: {reason}) expired at "
                f"{record['expires']}. The seat is active again.", mail, deadline)
        return ["expire"]
    if kind == "pre_expiry_notice":
        sent = _notify(owner, f"{persona}: pause expires soon",
                       f"The {record['state']} on {persona} (reason: {reason}) expires at "
                       f"{record['expires']}. {_how_to_extend(persona)}", mail, deadline)
    else:
        age_days = (when - _parse(record["set_ts"])).days
        sent = _notify(owner, f"{persona}: indefinite pause is {age_days}d old",
                       f"The {record['state']} on {persona} (reason: {reason}) has been "
                       f"indefinite for {age_days} days. {_how_to_extend(persona)}", mail, deadline)
    if not sent:
        return [kind, f"{kind}_failed"]
    with hs.seat_state_lock(persona, root=root, blocking=False):
        fresh = _load_valid(persona, root)
        if fresh is None or _key(fresh) != _key(record):
            _log(f"{persona}: record changed while mailing; not marking it (new lifecycle)")
            return [kind, f"{kind}_superseded"]
        if kind == "pre_expiry_notice":
            hs.write_seat_state(persona, {**fresh, "pre_expiry_notified": True}, root=root)
        else:
            _write_sidecar(persona, root, fresh["set_ts"], _fmt(when))
    return [kind]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ORG-106 L5 seat-state janitor.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick", help="run one janitor pass for a seat")
    t.add_argument("persona")
    t.add_argument("--dry-run", action="store_true")
    t.add_argument("--state-root", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        actions = tick(args.persona, state_root=args.state_root, dry_run=args.dry_run)
    except hs.HarnessStateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(", ".join(actions) if actions else "no action")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
