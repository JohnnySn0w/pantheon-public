---
symptom: "The tools/ test suite fires real Telegram notifications and mock patches silently miss with call_count 0"
fix: "Run-as-script imports create dual module identities - never sys.path.insert(tools/) in a test, import as tools.<mod> and run pytest from repo root; the conftest autouse fixture deadens TELEGRAM_ENV as backstop"
domain: pantheon, pytest, python-imports, telegram, test-isolation
---
# Tests fire real Telegram notifs / mocks silently miss — run-as-script vs run-as-package dual module identity

**Symptom(s)**
- Running the `tools/` test suite pings the operator with real Telegram notifications.
- A test patches `tools.inbox_consumer._send_alert_via_recovery` (or `_send_telegram_info`, etc.) but the patch does **not** intercept — the real send fires, `mock.call_count == 0`.
- A notify/alert test passes from repo-root but fails from inside `tools/` (or vice-versa).

**Root cause**
Several `tools/` modules call their dependencies with a run-as-script-first import:
```python
try:
    from inbox_consumer import _send_alert_via_recovery      # bare (cron/script)
except ImportError:
    from tools.inbox_consumer import _send_alert_via_recovery  # package
```
When `tools/` is on `sys.path`, **both** `inbox_consumer` and `tools.inbox_consumer`
load as **separate module objects** with separate attributes. A test that patches
`tools.inbox_consumer.X` then does nothing to the bare `inbox_consumer.X` the
production code actually calls → the real function runs. Since the real senders read
the live bot token from a fixed file (`~/.claude/channels/telegram/.env`, no env
override), the result is a real notification to the operator.

What put `tools/` on `sys.path` suite-wide was a single test
(`test_weekly_ticket_summary.py`) doing `sys.path.insert(0, <tools dir>)` — a
process-wide side effect that poisoned every later test in the run.

**Fix (surgical)**
1. Never `sys.path.insert(<tools/>)` in a test — import the module under test as
   `import tools.<mod>` (a `tools/__init__.py` package exists). Run pytest from the
   **repo root** (canonical invocation); that keeps only the `tools.X` identities live.
2. Belt-and-suspenders: `tools/conftest.py` has an autouse fixture that repoints every
   sender module's `TELEGRAM_ENV` at a dead path, so `read_telegram_token()` returns
   None and **no** test can fire a real notification even if a patch misses. Tests that
   legitimately exercise the send path patch `TELEGRAM_ENV` themselves (inner patch wins).

**Provenance:** T-0856 (2026-06-05). Surfaced when full-suite runs from `tools/` pinged
The operator. Sibling gotcha: bare `import <mod>` in a test breaks repo-root collection
(`ModuleNotFoundError`) — same fix (use `import tools.<mod>`).
