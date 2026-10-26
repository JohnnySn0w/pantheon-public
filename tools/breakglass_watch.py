#!/usr/bin/env python3
"""Fleet break-glass watcher (T-4392 phase 2a; Recovery's surfacing ruling (d), 2026-09-30).

While a LIVE break-glass flag stands for an account, the fail-closed hooks it names FAIL OPEN on
their fail-closed paths until it expires (tools/hooks/_failclosed.py). Recovery must know. This runs
once per context_fill_monitor cycle on RECOVERY's instance (the 5-min hook_allowlist cadence, one
fleet-wide sender), lists every account's flag under /var/lib/pantheon-breakglass (root:root
0755, so no sudo), and mails Recovery through the operational-alert router:

  * ONCE when a live flag is first seen ("created"), and
  * ONCE when it is within PRE_EXPIRY_S (1h) of expiry ("expiring"),

never every cycle. Dedup state is a small JSON file keyed on account:inode:ctime_ns:expiry, so a
re-created or rewritten flag (new ctime) alerts again, and entries for flags that are gone or
expired are pruned. The state is saved BEFORE any send (a state file that cannot be written
means nothing is sent, never a re-mail every cycle); a failed send is then un-recorded, so it
retries next cycle. Only LIVE flags
alert (the same strict checks the hooks apply); a REJECTED file is ignored by the hooks and is
surfaced per-seat by verify-hook-integrity / verify_startup. Best-effort: never raises into
the monitor.

    python3 tools/breakglass_watch.py [--dry-run]    # one cycle by hand
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

PRE_EXPIRY_S = 3600
STATE_NAME = "breakglass_alerts.json"
_HELPER = Path(__file__).resolve().parent / "hooks" / "_failclosed.py"
_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"


def _helper():
    mod = sys.modules.get("_failclosed")
    if mod is not None and Path(getattr(mod, "__file__", "")).resolve() == _HELPER:
        return mod
    spec = importlib.util.spec_from_file_location("_failclosed_watch", _HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def default_state_path() -> Path:
    env = os.environ.get("PANTHEON_STATE_DIR")
    return (Path(env) if env else _DEFAULT_STATE_DIR) / STATE_NAME


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def flag_key(s) -> str:
    return f"{s.account}:{s.ino}:{s.ctime_ns}:{s.expiry}"


def _default_send(msg: str, vocab: str) -> None:
    try:
        from tools.inbox_consumer import _send_alert_via_recovery  # run-as-package / tests
    except ImportError:
        from inbox_consumer import _send_alert_via_recovery  # run-as-script (prod)
    _send_alert_via_recovery(msg, vocab)


def _describe(fc, s, now: float) -> str:
    left = max(0, int(s.expiry - now))
    reason = f"; reason: {s.note}" if s.note else ""
    return (f"hooks={','.join(s.hooks)} owner={s.owner_name} expires={fc._iso(s.expiry)} "
            f"(in {left // 3600}h{(left % 3600) // 60:02d}m){reason}")


def check_and_alert(*, statuses=None, state_path: Path | None = None, send=None,
                    now: float | None = None) -> list[str]:
    """One cycle. Returns the messages sent. `statuses`/`send`/`now` are injectable for tests."""
    fc = _helper()
    now = time.time() if now is None else now
    state_path = state_path or default_state_path()
    send = send or _default_send
    if statuses is None:
        statuses = fc.list_flags(now=now)
    old = _load(state_path)
    new: dict = {}
    pending: list[tuple[str, str, str]] = []  # (key, state field, message)

    for s in statuses:
        if not getattr(s, "live", False):
            continue
        key = flag_key(s)
        rec = dict(old.get(key) or {})
        rec.update(account=s.account, expiry=s.expiry)
        if not rec.get("created_sent"):
            pending.append((key, "created_sent",
                f"🚨 [breakglass] LIVE break-glass flag on {s.account}: {_describe(fc, s, now)}. "
                f"Those hooks' fail-closed paths (exception/ambiguity/deadline/cap) FAIL OPEN on "
                f"{s.account} until expiry (T-4392). Confirm it was deliberate; to end it early: "
                f"sudo rm /var/lib/pantheon-breakglass/{s.account}/flag"))
        if s.expiry - now <= PRE_EXPIRY_S and not rec.get("expiring_sent"):
            pending.append((key, "expiring_sent",
                f"⏳ [breakglass] the flag on {s.account} expires within 1h: {_describe(fc, s, now)}. "
                f"After expiry those hooks fail CLOSED again; if the underlying fault is not fixed, "
                f"expect denies (T-4392)."))
        new[key] = rec

    # RECORD BEFORE SENDING: the state (including every alert about to go out) is saved first,
    # so a state file that cannot be written can never re-mail Recovery every cycle. If this save
    # raises, nothing is sent this cycle and the caller logs the failure.
    for key, field, _ in pending:
        new[key][field] = now
    if new != old:
        _save(state_path, new)

    sent: list[str] = []
    unsent = False
    for key, field, msg in pending:
        try:
            send(msg, "breakglass")
        except Exception:
            new[key].pop(field, None)  # not delivered: un-record it so the next cycle retries
            unsent = True
            continue
        sent.append(msg)
    if unsent:
        try:
            _save(state_path, new)
        except Exception:
            pass  # the alert then counts as sent: a lost alert beats a mail storm
    return sent


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--dry-run" in argv:
        fc = _helper()
        for s in fc.list_flags():
            print(fc.format_status(s))
        return 0
    for msg in check_and_alert():
        print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
