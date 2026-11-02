#!/usr/bin/env python3
"""PreToolUse entry shim for worker-guard v2 (T-5695 step 2). The guard body is worker_guard_v2.py.
NOT wired into any settings.json yet: the swap phase of T-5695 points the hook here.

Same contract as worker-guard.py (the v1 shim, T-4177), so the swap changes only which body runs:
  * stdin is read once; a read failure is a silent fail-open exit 0.
  * `_definitely_main_thread`: neither the text `agent_type` nor any `\\u` escape appears in the
    payload, so no top-level key can be `agent_type`. That case exits 0 with no output and
    imports nothing (the main thread is never restricted).
  * otherwise worker_guard_v2 is imported and given the SAME text plus the start stamp, and its
    main() applies the deadline and the size cap.
This runs at module level, not inside a function, so v2 sees the same stack depth v1's shim gives.
"""

import sys


def _definitely_main_thread(raw):
    return "agent_type" not in raw and "\\u" not in raw


if __name__ == "__main__":
    # `time` is a builtin already loaded at startup: a sys.modules lookup, not an import.
    import time

    started = time.monotonic()
    try:
        raw = sys.stdin.read()
    except Exception:
        sys.exit(0)  # fail-open, as v1's shim
    if _definitely_main_thread(raw):
        sys.exit(0)  # main thread: silent allow, nothing imported
    import os

    here = os.path.dirname(os.path.realpath(__file__))
    # Unconditional and FIRST, as in v1's shim: a decoy earlier on sys.path must not win.
    sys.path.insert(0, here)
    import worker_guard_v2

    worker_guard_v2.main(raw, started_monotonic=started)
