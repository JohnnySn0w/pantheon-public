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
import subprocess
import sys
import time
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RECOVERY_INBOX = os.environ.get(
    "PANTHEON_RECOVERY_INBOX", "/home/the-ancestor/mailbox/recovery"
)

# Per-unit re-alert cadence (mirrors disk_usage_monitor / credential_expiry_monitor).
ALERT_COOLDOWN_SECONDS = int(
    os.environ.get("PANTHEON_UNIT_FAILURE_COOLDOWN_SECONDS", str(6 * 3600))
)

JOURNAL_TAIL_LINES = 12
JOURNAL_TIMEOUT_SECONDS = 15

STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
STATE_PATH = STATE_DIR / "unit_failure_alert_state.json"


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
    mailbox.send(RECOVERY_INBOX, body, sender, subject=f"unit-failure: {unit}",
                 severity="urgent")


def _deliver_telegram_fallback(text: str) -> None:
    # Lazy import: inbox_consumer carries live-Telegram surface that must not
    # load on the happy path (the full-suite leak lesson; see mailbox_wake.py).
    from tools.inbox_consumer import _send_telegram_alert
    _send_telegram_alert(text)


def run(unit: str) -> int:
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
