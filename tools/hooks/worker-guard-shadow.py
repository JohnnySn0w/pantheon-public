#!/usr/bin/env python3
"""Shadow entry for worker-guard v2 (T-5686). NOT wired into any settings.json or hook config.

Same stdin contract as worker-guard.py. It denies nothing, prints nothing and always exits 0
(fail-open on every error). For a subagent payload it runs BOTH guards, the current one
(worker_guard_impl, through its deadline-bounded path and memory cap, as its main() does) and v2,
times each, and appends one JSONL record to the shadow log:

  ts, tool_name, agent_type, v1 {verdict, reason}, v2 {verdict, rule}, v1_ms, v2_ms, sha256 (of
  the raw payload), and `command` (Bash) / `path` (other tools) ONLY when the verdicts disagree.

Log: $WORKER_GUARD_SHADOW_DIR (tests only) or ~/.local/state/pantheon/worker-guard-shadow/,
one file per UTC day, mode 0600. The main-thread fast path imports nothing.
"""

import sys


def _main():
    import time

    raw = sys.stdin.read()
    if "agent_type" not in raw and "\\u" not in raw:
        return  # main thread: nothing imported, nothing logged
    import hashlib
    import json
    import os
    import re

    data = json.loads(raw)
    if not isinstance(data, dict) or not data.get("agent_type"):
        return
    here = os.path.dirname(os.path.realpath(__file__))
    sys.path.insert(0, here)
    import worker_guard_impl as v1mod
    import worker_guard_v2 as v2mod

    def run_v1():
        t = time.perf_counter()
        try:
            with v1mod._memory_cap():
                payload = v1mod._decide_by_deadline(data, v1mod.DECIDE_DEADLINE_S)
        except BaseException as exc:  # noqa: BLE001 - a v1 crash is v1's own fail-closed deny
            return time.perf_counter() - t, {"verdict": "deny", "reason": f"v1-exception:{type(exc).__name__}"}
        ms = time.perf_counter() - t
        if payload is None:
            return ms, {"verdict": "allow", "reason": None}
        reason = str(payload.get("hookSpecificOutput", {}).get("permissionDecisionReason", ""))
        m = re.search(r"worker-guard, (T-\d+)\)", reason)
        what = reason.split("attempted", 1)[-1][:90].strip() if "attempted" in reason else reason[:90]
        return ms, {"verdict": "deny", "reason": f"{m.group(1) if m else '?'}: {what}"}

    def run_v2():
        t = time.perf_counter()
        try:
            v = v2mod.verdict(data)
        except BaseException as exc:  # noqa: BLE001 - v2's main() would deny on a raise
            return time.perf_counter() - t, {"verdict": "deny", "rule": f"EXC:{type(exc).__name__}"}
        return time.perf_counter() - t, ({"verdict": "allow", "rule": None} if v is None
                                         else {"verdict": "deny", "rule": v[0]})

    t1, r1 = run_v1()
    t2, r2 = run_v2()
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool_name": data.get("tool_name"),
        "agent_type": data.get("agent_type"),
        "v1": r1,
        "v2": r2,
        "v1_ms": round(t1 * 1000, 3),
        "v2_ms": round(t2 * 1000, 3),
        "sha256": hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest(),
    }
    if r1["verdict"] != r2["verdict"]:
        tin = data.get("tool_input") or {}
        if data.get("tool_name") == "Bash":
            rec["command"] = tin.get("command")
        else:
            rec["path"] = tin.get("file_path") or tin.get("notebook_path") or tin.get("path") or tin.get("command")
    logdir = os.environ.get("WORKER_GUARD_SHADOW_DIR") or os.path.expanduser(
        "~/.local/state/pantheon/worker-guard-shadow")
    os.makedirs(logdir, mode=0o700, exist_ok=True)
    path = os.path.join(logdir, "shadow-" + time.strftime("%Y-%m-%d", time.gmtime()) + ".jsonl")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(rec) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


if __name__ == "__main__":
    try:
        _main()
    except BaseException:  # noqa: BLE001 - the shadow never blocks, never prints
        pass
    sys.exit(0)
