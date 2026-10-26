#!/usr/bin/env python3
"""PreToolUse entry shim for the worker-guard hook (T-4177). The guard body is worker_guard_impl.py.

Why a shim: the hook fires on the fleet's hottest tools (Bash/Edit/Write/Read/Grep) and the
guard never restricts the main thread. Run as `__main__`, the old ~1200-line script was
recompiled, its regexes rebuilt and json/re/shlex imported on EVERY call, all to return "allow"
for a payload with no `agent_type`. This shim imports nothing on that path.

Contract (decision-identical to the pre-shim guard; tools/test_worker_guard_shim.py and
tools/worker_guard_cutover_smoke.py prove it):
  * stdin is read once, exactly as the guard did; a read failure is a silent fail-open exit 0.
  * `_definitely_main_thread`: neither the text `agent_type` nor any `\\u` escape appears in the
    payload. JSON can spell the key's letters only literally or as `\\uXXXX`, so no top-level key
    can be `agent_type`: the guard would end in a silent allow (unparseable, non-dict, or no
    key). That case exits 0 with no output. Anything doubtful (a present-but-falsy agent_type, an
    escaped spelling) goes to the impl, which decides exactly as before.
  * otherwise the impl is imported (cached .pyc) and given the SAME text, so stdin is not read
    twice. If the impl cannot be imported, the traceback propagates (exit 1, non-blocking, loud;
    subagent calls only), the same visible failure an unloadable monolith produced. (That exit 1
    is still a fail-OPEN for the call; it is left as is and named for the fleet audit, T-4392.)
  * T-4389, fail CLOSED on the guard's own clock (a timed-out hook is non-blocking on Claude Code
    and Codex alike, so the call would PROCEED): on this subagent path the impl gives the whole
    call DECIDE_DEADLINE_S (5s, counted from PROCESS START via /proc/self/stat, because the
    harness counts its timeout from spawn; Codex's is 30s, T-4395) and prints ONE deny in its usual shape
    and exits 0 when it runs out (immediately, if it is already spent). The timer is SIGALRM, or a
    thread with a checked deadline where SIGALRM is unavailable. Where /proc is missing, the
    `started` stamp this shim takes first is the start. A Bash command over BASH_COMMAND_MAX_CHARS
    (64 KiB; the Bash command only, not Edit/Write payloads) is denied before any parsing. The
    budget is a module constant: this shim passes no override and nothing reads one from the
    environment, so a worker cannot lengthen it. The main-thread fast path is unchanged: no clock
    check, no cap, nothing imported (`time` is a builtin already in sys.modules at startup).
This runs at module level, not inside a function, so the impl sees the same stack depth the
monolith's main() did.
"""

import sys


def _definitely_main_thread(raw):
    return "agent_type" not in raw and "\\u" not in raw


if __name__ == "__main__":
    # T-4389 r2: the start stamp for the guard's deadline where /proc is missing. `time` is a
    # builtin the interpreter has already loaded, so this is a sys.modules lookup, not an import.
    import time

    started = time.monotonic()
    try:
        raw = sys.stdin.read()
    except Exception:
        sys.exit(0)  # fail-open, as before
    if _definitely_main_thread(raw):
        sys.exit(0)  # main thread: silent allow, nothing imported
    import os

    here = os.path.dirname(os.path.realpath(__file__))
    # Unconditional, and FIRST: a `not in sys.path` guard would skip this when PYTHONPATH already
    # lists the hooks dir behind a decoy (PYTHONSAFEPATH=1 / -I / -P), letting a same-named
    # module win and silently drop every subagent deny. A duplicate later entry is harmless.
    sys.path.insert(0, here)
    import worker_guard_impl

    worker_guard_impl.main(raw, started_monotonic=started)
