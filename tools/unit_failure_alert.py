#!/usr/bin/env python3
"""OnFailure handler: route a systemd unit failure into Recovery-triage (T-1868).

Invoked by `pantheon-unit-failure-alert@.service` with the FAILED unit's full
name (systemd `%n` on the failing unit → `%i` here). Closes the T-1867 gap
class: a cron unit dying silently, with digest-absence as the only signal
(grooming failed 2026-07-19 12:00Z; nobody knew for ~21h).

Routing per the operator's directive (2026-06-02, msg 2235; `route_failure_alert`
in tools/inbox_consumer.py): failures go to RECOVERY to triage — here as a message
into Recovery's ORG-026 mailbox, which the clock-driven
`pantheon-mailbox-wake@recovery.timer` picks up within ~5 minutes. Only when the
mailbox path itself fails does the alert go DIRECT to the operator's Telegram
(Recovery's attention path is broken = the operator must know).

Contract: fail-open, storm-proof. Per-unit cooldown (default 6h) so a
crash-looping 5-minute monitor produces one alert per window, not a flood.
Exits 0 on throttle and on successful delivery via EITHER path; exits 1 only
when both the mailbox and the Telegram fallback failed (the alert template
unit is a leaf — it carries no OnFailure= of its own, so a failing alert can
never recurse).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Env override first. Otherwise the recovery box under the hub from instance.yaml
# (T-4096), resolved LAZILY in _recovery_inbox(): this OnFailure leaf keeps its
# zero-module-level-sibling-import posture, so a broken import cannot stop the
# alert from loading — a raise there lands in the mailbox leg's except and the
# direct-Telegram fallback still fires.
RECOVERY_INBOX = os.environ.get("PANTHEON_RECOVERY_INBOX") or None


def _recovery_inbox() -> str:
    if RECOVERY_INBOX:
        return RECOVERY_INBOX
    from tools.agent_role_map import mailbox_hub
    return f"{mailbox_hub()}/recovery"


# Per-unit re-alert cadence (mirrors disk_usage_monitor / credential_expiry_monitor).
ALERT_COOLDOWN_SECONDS = int(
    os.environ.get("PANTHEON_UNIT_FAILURE_COOLDOWN_SECONDS", str(6 * 3600))
)

JOURNAL_TAIL_LINES = 12
JOURNAL_TIMEOUT_SECONDS = 15

STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
STATE_PATH = STATE_DIR / "unit_failure_alert_state.json"

# Instance of a templated unit: `<name>@<instance>.<suffix>` -> <instance>. For the
# per-persona pantheon units (keep-going@artisan, context-fill-monitor@mason,
# persona-consume@artisan, inbox-consumer@recovery, …) the instance IS the persona.
_UNIT_INSTANCE_RE = re.compile(r"^[^@]+@(?P<inst>[^.]+)\.")


def _log(msg: str) -> None:
    print(f"[unit_failure_alert ts={int(time.time())}] {msg}", flush=True)


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state))
    except OSError as e:
        print(f"[unit_failure_alert] could not persist state: {e}", file=sys.stderr)


def _should_alert(unit: str, now: float, state: dict) -> bool:
    return (now - state.get(unit, 0)) >= ALERT_COOLDOWN_SECONDS


def _journal_tail(unit: str) -> str:
    """Best-effort journal tail for the failed unit. By OnFailure time the
    failing process has exited so its journal lines are flushed; the timeout
    guards against a slow/hung journald ever wedging this oneshot."""
    try:
        proc = subprocess.run(
            [
                "journalctl", "--user", "-u", unit,
                "-n", str(JOURNAL_TAIL_LINES), "--no-pager", "-o", "cat",
            ],
            capture_output=True, text=True, timeout=JOURNAL_TIMEOUT_SECONDS,
        )
        return proc.stdout.strip() or "(journal tail empty)"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"(journal tail unavailable: {exc!r})"


def _compose_body(unit: str, account: str, now: float) -> str:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    return (
        f"systemd unit FAILED: {unit} (account {account}, {ts})\n"
        f"Journal tail:\n{_journal_tail(unit)}\n"
        f"Action (recovery): triage — `journalctl --user -u {unit}` on the "
        f"{account} account for full context; fix or file a ticket; "
        f"`systemctl --user reset-failed {unit}` once handled."
    )


def _deliver_mailbox(unit: str, body: str, sender: str) -> None:
    from tools import mailbox
    # T-2237(a): a cron died and this notice IS the only record. Undelivered =
    # the failure stays invisible, which is the harm — so it is urgent, not age-y.
    mailbox.send(_recovery_inbox(), body, sender, subject=f"unit-failure: {unit}",
                 severity="urgent")


def _deliver_telegram_fallback(text: str) -> None:
    # Lazy import: inbox_consumer carries live-Telegram surface that must not
    # load on the happy path (the full-suite leak lesson; see mailbox_wake.py).
    from tools.inbox_consumer import _send_telegram_alert
    _send_telegram_alert(text)


def _persona_for_unit(unit: str) -> str | None:
    """Best-effort map a failed unit to the persona it belongs to, for the mute gate.

    A templated per-persona unit carries the persona in its instance
    (`pantheon-keep-going@artisan.service` -> artisan); a BARE unit maps to the persona
    of the account this OnFailure handler runs under. Returns None when neither
    yields a KNOWN persona, so the caller fails OPEN and alerts — the mute never
    invents a persona to silence.

    T-4043: a TEMPLATED unit whose @instance is NOT a known persona
    (`pantheon-thing@notapersona.service`) must ALSO fail OPEN — the account
    fallback is for bare units ONLY. Falling an unknown @instance through to the
    account persona let a cap-muted seat silence a unit failure it could not
    attribute (e.g. suborch1's account persona = mason, muted), violating the
    ORG-076 §7 fail-open contract (a mute must never silence what it can't map).
    The @instance being present at all means the unit named its own target, so a
    non-persona instance is an unattributable target, not this account's."""
    try:
        from tools.agent_role_map import account_persona, account_persona_map
        personas = set(account_persona_map().values())
    except Exception:
        return None
    m = _UNIT_INSTANCE_RE.match(unit)
    if m:
        # Templated: the persona IS the instance. Known -> that persona; unknown ->
        # fail OPEN (None), NEVER the account fallback (T-4043).
        inst = m.group("inst")
        return inst if inst in personas else None
    # Bare unit: maps to the persona of the account this handler runs under.
    try:
        acct_persona = account_persona(getpass.getuser())
    except Exception:
        acct_persona = None
    return acct_persona if acct_persona in personas else None


def _suppressed_by_mute(unit: str, registry: str | os.PathLike | None) -> bool:
    """True iff `unit`'s persona is explicitly, readably muted (ORG-076 §7 / T-3844).

    notify_policy names "unit-failure" as one of the classes a persona-mute silences,
    but this OnFailure handler bypassed route_failure_alert's mute chokepoint — so a
    cap-muted seat's unit failures (keep-going / consume / cfm / reconcile, all of
    which non-zero-exit mid-cap) flooded Recovery's mailbox as cap-collateral. FAIL-OPEN:
    an unmappable unit or any registry fault returns False (is_muted itself fail-opens),
    so the failure direction is a spurious page, never a silenced real one."""
    persona = _persona_for_unit(unit)
    if persona is None:
        return False
    try:
        from tools.notify_policy import is_muted
        return bool(is_muted(persona, registry))
    except Exception:
        return False


def run(unit: str, *, registry: str | os.PathLike | None = None) -> int:
    # Mute chokepoint (ORG-076 §7 / T-3844) — FIRST, before the throttle: a muted
    # persona's unit-failure is withheld and state is left UNTOUCHED, so it re-alerts
    # promptly the moment the mute lifts (mute != blind; the failure isn't cleared,
    # only the page). Dormant no-op until a persona is actually muted.
    if _suppressed_by_mute(unit, registry):
        _log(f"suppressed: {unit} — its persona is muted (cap-collateral / "
             f"operator mute, ORG-076 §7); no alert, throttle state untouched")
        return 0
    account = getpass.getuser()
    now = time.time()
    state = _load_state()
    if not _should_alert(unit, now, state):
        _log(f"throttled: {unit} alerted within the last "
             f"{ALERT_COOLDOWN_SECONDS}s; skipping")
        return 0

    body = _compose_body(unit, account, now)
    try:
        _deliver_mailbox(unit, body, account)
        _log(f"delivered unit-failure({unit}) to recovery mailbox")
    except Exception as exc:
        _log(f"mailbox path failed ({exc!r}); falling back to direct Telegram")
        try:
            _deliver_telegram_fallback(
                f"⚠️ [unit-failure, recovery mailbox unreachable] {body}"
            )
            _log("fallback Telegram alert sent")
        except Exception as exc2:
            print(
                f"[unit_failure_alert] BOTH paths failed: mailbox={exc!r} "
                f"telegram={exc2!r}",
                file=sys.stderr,
            )
            return 1

    state[unit] = now
    _save_state(state)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit", required=True,
                        help="full name of the failed unit (systemd %%n)")
    args = parser.parse_args()
    return run(args.unit)


if __name__ == "__main__":
    sys.exit(main())
