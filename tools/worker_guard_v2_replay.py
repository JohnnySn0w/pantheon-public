#!/usr/bin/env python3
"""Replay validation for worker-guard v2 (T-5686). A tool you run, not a CI test.

  nice python3 tools/worker_guard_v2_replay.py [--out DIR] [--workers 2] [--limit N]

What it does (read-only on ~/.claude/projects; writes only under --out):
  1. extracts every subagent PreToolUse-able call (Bash, Edit, Write, MultiEdit, NotebookEdit,
     Read, Grep) from ~/.claude/projects/**/subagents/*.jsonl, rebuilding the hook payload
     (tool_name, tool_input, cwd from the record, agent_type from the sibling .meta.json, else
     "subagent"), deduped by tool_use_id; pairs each with its field denial, if any (a tool_result
     starting `PreToolUse:<Tool> hook error: BLOCKED (worker-guard`);
  2. judges each call with the CURRENT guard (worker_guard_impl) in child processes (RLIMIT_AS
     2 GiB, per-command timeout; a timeout counts as v1's own fail-closed deny and is listed
     separately; the v1 in-process time is measured in the child around the decision) and with v2
     in-process;
  3. reports totals, v2-DENY & v1-ALLOW (newly denied), v1-DENY & v2-ALLOW by old reason family,
     the field denials with their v2 verdict (the real-catch check), and timing (in-process
     percentiles, plus end-to-end fresh-process p50/p99 over a sample of Bash commands).

Run it under `nice`, one at a time. Output: <out>/replay.json (raw) and a text summary on stdout.
"""

import argparse
import glob
import json
import os
import random
import re
import selectors
import statistics
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.join(HERE, "hooks")
TOOLS = ("Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "Read", "Grep")
KEEP_INPUT_KEYS = ("command", "file_path", "notebook_path", "path", "pattern")

# ---------------------------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------------------------


def _agent_type(path, cache):
    meta = path[:-len(".jsonl")] + ".meta.json"
    if meta not in cache:
        try:
            with open(meta) as f:
                cache[meta] = json.load(f).get("agentType") or "subagent"
        except Exception:
            cache[meta] = "subagent"
    return cache[meta]


def _result_text(block):
    c = block.get("content")
    if isinstance(c, list):
        c = "".join(x.get("text", "") for x in c if isinstance(x, dict))
    return c if isinstance(c, str) else ""


def extract(projects_dir):
    """Return (calls, stats). calls: dict tool_use_id -> payload dict (+ meta)."""
    files = sorted(glob.glob(os.path.join(projects_dir, "**", "subagents", "*.jsonl"), recursive=True))
    calls, denials, cache = {}, {}, {}
    first_ts = last_ts = None
    for path in files:
        atype = _agent_type(path, cache)
        try:
            fh = open(path, errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"tool_use"' not in line and '"tool_result"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
                    continue
                ts = rec.get("timestamp")
                for b in msg["content"]:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use" and b.get("name") in TOOLS:
                        tid = b.get("id")
                        if not tid or tid in calls:
                            continue
                        tin = b.get("input") if isinstance(b.get("input"), dict) else {}
                        tin = {k: v for k, v in tin.items() if k in KEEP_INPUT_KEYS}
                        calls[tid] = {
                            "id": tid, "ts": ts, "file": os.path.basename(path),
                            "payload": {"tool_name": b["name"], "tool_input": tin,
                                        "cwd": rec.get("cwd") or "", "agent_type": atype},
                        }
                        if ts:
                            first_ts = min(first_ts or ts, ts)
                            last_ts = max(last_ts or ts, ts)
                    elif b.get("type") == "tool_result":
                        text = _result_text(b)
                        if text.startswith("PreToolUse:") and "hook error: BLOCKED (worker-guard" in text[:120]:
                            denials[b.get("tool_use_id")] = text
    for tid, text in denials.items():
        if tid in calls:
            calls[tid]["field_denial"] = text[:600]
    return calls, {"files": len(files), "first_ts": first_ts, "last_ts": last_ts,
                   "field_denials_total": len(denials)}


# ---------------------------------------------------------------------------------------------
# v1 families
# ---------------------------------------------------------------------------------------------
_FAMILIES = (
    ("HO-records (Bash)", "write/commit orchestrator-owned"),
    ("Edit/Write protected doc", "Edit/Write/patch a protected"),
    ("deadline (T-4389)", "timed out deciding"),
    ("deadline (T-4389)", "ran out of time"),
    ("sudo / escalation", "escalate privileges"),
    ("non-literal command word", "a command word here is not a plain"),
    ("non-literal command word", "command whose name this hook cannot read"),
    ("non-literal command word", "has a part the guard cannot"),
    ("push / PR", "git push"),
    ("gh allowlist / api", "gh "),
    ("credential-file read", "credential file"),
    ("credential-file read", "Telegram bot token"),
    ("git identity / --global", "git user identity"),
    ("git identity / --global", "--global/--system git config"),
    ("git identity / --global", "git config"),
    ("python startup-hook paths", "Python startup-hook"),
    ("T-5505 script reader", "T-5505"),
    ("T-5505 script reader", "script file"),
    ("T-5505 script reader", "PATH="),
    ("T-5505 script reader", "process subst"),
    ("T-5491 run-time variable names", "T-5491"),
    ("T-5386 renames", "T-5386"),
    ("T-5634 prompt/history", "T-5634"),
    ("env -S / mapfile / other bypass", "T-5363"),
    ("env -S / mapfile / other bypass", "T-5417"),
)


def v1_family(reason):
    if not reason:
        return "other"
    for fam, key in _FAMILIES:
        if key in reason:
            return fam
    m = re.search(r"worker-guard, (T-[\d/T-]+)\)", reason)
    return "other " + (m.group(1) if m else "")


# ---------------------------------------------------------------------------------------------
# v1 child pool
# ---------------------------------------------------------------------------------------------
def _child_main():
    """Child: read one JSON payload per line on stdin, answer one JSON line on stdout."""
    import resource

    cap = 2 << 30
    try:
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    except Exception:
        pass
    sys.path.insert(0, HOOKS)
    import worker_guard_impl as v1

    for line in sys.stdin:
        data = json.loads(line)
        t = time.perf_counter()
        try:
            payload = v1._decide_by_deadline(data, v1.DECIDE_DEADLINE_S)
            ms = (time.perf_counter() - t) * 1000
            if payload is None:
                out = {"deny": False, "ms": ms}
            else:
                reason = str(payload["hookSpecificOutput"]["permissionDecisionReason"])
                out = {"deny": True, "ms": ms, "reason": reason[:400]}
        except BaseException as exc:  # noqa: BLE001 - e.g. MemoryError under the cap
            out = {"deny": True, "ms": (time.perf_counter() - t) * 1000,
                   "reason": f"v1-exception {type(exc).__name__}", "exc": True}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


class _Child:
    def __init__(self):
        self.p = None
        self.start()

    def start(self):
        self.p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--child"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def ask(self, payload, timeout):
        try:
            self.p.stdin.write(json.dumps(payload) + "\n")
            self.p.stdin.flush()
            # T-5872: selectors, not select.select() (ValueError for fd >= 1024).
            with selectors.PollSelector() as sel:
                sel.register(self.p.stdout, selectors.EVENT_READ)
                r = sel.select(timeout)
            if r:
                line = self.p.stdout.readline()
                if line:
                    return json.loads(line)
        except (BrokenPipeError, ValueError, OSError):
            pass
        self.kill()
        self.start()
        return {"deny": True, "ms": timeout * 1000, "reason": "v1-timeout (replay backstop)", "timeout": True}

    def kill(self):
        try:
            self.p.kill()
            self.p.wait(timeout=5)
        except Exception:
            pass

    def close(self):
        try:
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
            self.kill()


def judge_v1(items, workers, timeout):
    results = {}
    lock = threading.Lock()
    it = iter(items)
    done = [0]

    def run():
        child = _Child()
        try:
            while True:
                with lock:
                    nxt = next(it, None)
                if nxt is None:
                    return
                tid, payload = nxt
                res = child.ask(payload, timeout)
                with lock:
                    results[tid] = res
                    done[0] += 1
                    if done[0] % 2000 == 0:
                        print(f"  v1 judged {done[0]}/{len(items)}", file=sys.stderr, flush=True)
        finally:
            child.close()

    threads = [threading.Thread(target=run) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# ---------------------------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------------------------
def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def summarize_ms(xs):
    return {"n": len(xs), "p50": pct(xs, .5), "p90": pct(xs, .9), "p99": pct(xs, .99),
            "max": max(xs) if xs else None, "mean": statistics.fmean(xs) if xs else None}


def e2e_timing(calls, n, seed=5686):
    bash = [c for c in calls.values() if c["payload"]["tool_name"] == "Bash"]
    rng = random.Random(seed)
    sample = rng.sample(bash, min(n, len(bash)))
    v1_entry = os.path.join(HOOKS, "worker-guard.py")
    v2_entry = os.path.join(HOOKS, "worker_guard_v2.py")
    t1, t2 = [], []
    load0 = os.getloadavg()
    for c in sample:
        raw = json.dumps(c["payload"])
        for entry, acc in ((v1_entry, t1), (v2_entry, t2)):
            t = time.perf_counter()
            subprocess.run([sys.executable, entry], input=raw, text=True, capture_output=True, timeout=60)
            acc.append((time.perf_counter() - t) * 1000)
    return {"n": len(sample), "v1": summarize_ms(t1), "v2": summarize_ms(t2),
            "load_before": load0, "load_after": os.getloadavg()}


# ---------------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------------
def snippet(payload, n=220):
    tin = payload["tool_input"]
    s = tin.get("command") or tin.get("file_path") or tin.get("notebook_path") or tin.get("path") or ""
    return s if len(s) <= n else s[:n] + "..."


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--out", default="/tmp/t5686-drafts")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--v1-timeout", type=float, default=15.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--e2e", type=int, default=200)
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.child:
        return _child_main()
    try:
        os.nice(10)
    except OSError:
        pass
    os.makedirs(a.out, exist_ok=True)
    sys.path.insert(0, HOOKS)
    import worker_guard_v2 as v2

    load_start = os.getloadavg()
    t0 = time.time()
    calls, stats = extract(a.projects)
    ids = sorted(calls, key=lambda k: calls[k]["ts"] or "")
    if a.limit:
        ids = ids[:a.limit]
    print(f"extracted {len(calls)} calls from {stats['files']} files ({stats['first_ts']}..{stats['last_ts']}); "
          f"{stats['field_denials_total']} field denials", file=sys.stderr, flush=True)

    v1res = judge_v1([(i, calls[i]["payload"]) for i in ids], a.workers, a.v1_timeout)

    rows = []
    v2_ms = []
    for i in ids:
        c = calls[i]
        t = time.perf_counter()
        try:
            v = v2.verdict(c["payload"])
            exc = None
        except Exception as e:  # noqa: BLE001
            v, exc = ("EXC", type(e).__name__, ""), type(e).__name__
        v2_ms.append((time.perf_counter() - t) * 1000)
        r1 = v1res.get(i, {"deny": False, "ms": 0})
        rows.append({
            "id": i, "ts": c["ts"], "tool": c["payload"]["tool_name"], "snippet": snippet(c["payload"]),
            "cwd": c["payload"]["cwd"], "v1_deny": r1["deny"], "v1_family": v1_family(r1.get("reason")) if r1["deny"] else None,
            "v1_reason": (r1.get("reason") or "")[:160] if r1["deny"] else None, "v1_timeout": bool(r1.get("timeout")),
            "v1_exc": bool(r1.get("exc")), "v1_ms": r1["ms"],
            "v2_deny": v is not None, "v2_rule": v[0] if v else None, "v2_what": v[1][:160] if v else None,
            "v2_exc": exc, "field_denied": "field_denial" in c, "field_denial": c.get("field_denial", "")[:200],
        })

    n = len(rows)
    new_denied = [r for r in rows if r["v2_deny"] and not r["v1_deny"]]
    v1_only = [r for r in rows if r["v1_deny"] and not r["v2_deny"]]
    both = [r for r in rows if r["v1_deny"] and r["v2_deny"]]
    fam = {}
    for r in v1_only:
        fam.setdefault(r["v1_family"], []).append(r)
    field = [r for r in rows if r["field_denied"]]
    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stats": stats, "calls": n,
        "by_tool": {t: sum(1 for r in rows if r["tool"] == t) for t in TOOLS},
        "v1_deny": sum(r["v1_deny"] for r in rows), "v2_deny": sum(r["v2_deny"] for r in rows),
        "both_deny": len(both), "newly_denied": len(new_denied), "v1_only_denied": len(v1_only),
        "v1_timeouts": sum(r["v1_timeout"] for r in rows), "v1_exceptions": sum(r["v1_exc"] for r in rows),
        "v2_exceptions": sum(1 for r in rows if r["v2_exc"]),
        "v2_rule_counts": {k: sum(1 for r in rows if r["v2_rule"] == k) for k in sorted({r["v2_rule"] for r in rows if r["v2_rule"]})},
        "v1_only_by_family": {k: len(v) for k, v in sorted(fam.items(), key=lambda kv: -len(kv[1]))},
        "newly_denied_list": new_denied,
        "v1_only_examples": {k: [x["snippet"] for x in v[:4]] for k, v in fam.items()},
        "field_denials": field,
        "field_denials_v2_still_deny": sum(1 for r in field if r["v2_deny"]),
        "timing_inprocess_ms": {"v1 (child)": summarize_ms([r["v1_ms"] for r in rows if not r["v1_timeout"]]),
                                "v2": summarize_ms(v2_ms)},
        "load_start": load_start,
    }
    if a.e2e:
        print("end-to-end timing...", file=sys.stderr, flush=True)
        report["timing_e2e_ms"] = e2e_timing(calls, a.e2e)
    report["load_end"] = os.getloadavg()
    report["wall_s"] = round(time.time() - t0, 1)
    with open(os.path.join(a.out, "replay.json"), "w") as f:
        json.dump(report, f, indent=1)
    summary = {k: v for k, v in report.items() if k not in ("newly_denied_list", "v1_only_examples", "field_denials")}
    print(json.dumps(summary, indent=1))
    print(f"\nnewly denied ({len(new_denied)}):")
    for r in new_denied:
        print(f"  [{r['v2_rule']}] {r['tool']}: {r['snippet'][:140]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
