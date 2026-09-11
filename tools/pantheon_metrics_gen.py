#!/usr/bin/env python3
"""pantheon_metrics_gen.py -- emit COARSE, surface-level fleet metrics JSON for the
personal-site PANTHEON window (T-3625).

STATIC-by-construction safety: emits ONLY the allowlisted coarse fields (persona
label + cpu%/mem%/state per member; cluster load%/up/total). NEVER hostnames,
account/service names, uids, paths, pids, host<->VM<->CT topology, model pins,
session/token internals, or ticket content. A leak_scan() runs on every output
and REFUSES to write if any forbidden content or non-allowlisted key appears --
the "do not overexpose" guardrail enforced in code.

Writes a LOCAL file. Public exposure (caddy file_server + DNS) is separate.

Roster (operator-confirmed surface, T-3625): 6 claude-harness agents + cairn
(codex harness) + echo (the echo.service audio daemon). Three measurement kinds:
  agent   -- per-account (claude harness): ps %mem, /proc cpu-delta, .claude
             jsonl freshness for active/idle/down.
  codex   -- per-account (codex harness): ps %mem, /proc cpu-delta, proc-presence
             for idle/down (no .claude transcript -> no "active" detection).
  service -- per systemd --user service: cgroup MemoryCurrent + CPUUsageNSec
             delta, ActiveState for idle/down.
"""
import argparse, json, os, subprocess, sys, time
from datetime import datetime, timezone

SCHEMA = "pantheon-metrics/1"
NCPU = os.cpu_count() or 1
ACTIVE_JSONL_FRESH_SEC = 60

ROSTER = [
    {"label": "orchestrator",     "acct": "claude",         "kind": "agent"},
    {"label": "specialist",     "acct": "claude-research", "kind": "agent"},
    {"label": "recovery",      "acct": "claude-fallback", "kind": "agent"},
    {"label": "metis",     "acct": "suborch1",        "kind": "agent"},
    {"label": "atlas",     "acct": "suborch2",        "kind": "agent"},
    {"label": "mnemosyne", "acct": "exocortex",       "kind": "agent"},
    {"label": "cairn",     "acct": "codex",           "kind": "codex"},
    {"label": "echo",      "acct": "claude", "kind": "service", "service": "echo.service"},
]

# --- output allowlist (leak_scan invariants) ---
TOP_KEYS = {"schema", "generated_at", "cluster", "agents"}
CLUSTER_KEYS = {"load_pct", "agents_up", "agents_total"}
AGENT_KEYS = {"label", "cpu_pct", "mem_pct", "state"}
STATES = {"active", "idle", "down"}
ALLOWED_LABELS = {m["label"] for m in ROSTER}
# Forbidden substrings: account/service names + host/path/id chrome. Persona
# labels (orchestrator/specialist/recovery/metis/atlas/mnemosyne/cairn/echo) are the ALLOWED
# public identifiers; the underlying account names (claude/claude-research/
# codex/...) and the ".service" suffix are forbidden -- disjoint from the labels.
FORBIDDEN = [
    "claude", "research", "fallback", "suborch", "exocortex", "codex",
    "the-ancestor", "/home", "snowis", "swarm", "boxon", "pve2", "gpu-ml",
    ".service", ".scope", "tmux", "/proc", "/sys", "uid", "pid", "jsonl",
]


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True)


def _uid(account):
    out = _run(["id", "-u", account])
    return int(out.stdout.strip()) if out.returncode == 0 else -1


def _meminfo_total_bytes():
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    return 0


def _total_cpu_jiffies():
    with open("/proc/stat") as f:
        parts = f.readline().split()
    return sum(int(x) for x in parts[1:])


def _account_pids(uid):
    out = _run(["ps", "-u", str(uid), "-o", "pid="])
    return [int(p) for p in out.stdout.split()] if out.returncode == 0 else []


def _pids_jiffies(pids):
    # utime (f14) + stime (f15). comm (f2) is parenthesised and may contain
    # spaces, so parse fields AFTER the final ')': index 0 there is state (f3),
    # so utime=index 11, stime=index 12.
    total = 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat") as f:
                rest = f.read().rsplit(")", 1)[1].split()
            total += int(rest[11]) + int(rest[12])
        except (OSError, ValueError, IndexError):
            continue
    return total


def _mem_pct_account(uid):
    out = _run(["ps", "-u", str(uid), "-o", "%mem="])
    if out.returncode != 0:
        return 0.0
    return round(sum(float(x) for x in out.stdout.split()), 1)


def _has_agent_proc(uid):
    comms = _run(["ps", "-u", str(uid), "-o", "comm="]).stdout.lower()
    return ("claude" in comms) or ("codex" in comms)


def _jsonl_fresh(account):
    cmd = ('find ~/.claude/projects -maxdepth 2 -name "*.jsonl" '
           '-printf "%T@\\n" 2>/dev/null | sort -rn | head -1')
    out = _run(["sudo", "-u", account, "bash", "-c", cmd])
    try:
        return (time.time() - float(out.stdout.strip())) <= ACTIVE_JSONL_FRESH_SEC
    except ValueError:
        return False


def _svc_show(account, service, prop):
    out = _run(["sudo", "-u", account, "bash", "-c",
                f'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user show '
                f'{service} -p {prop} --value'])
    return out.stdout.strip()


def collect(roster, sample_sec=1.0):
    memtotal = _meminfo_total_bytes()
    # t0 snapshots: per-account pid jiffies, service CPUUsageNSec, total cpu.
    ctx = {}
    for m in roster:
        if m["kind"] in ("agent", "codex"):
            uid = _uid(m["acct"])
            pids = _account_pids(uid)
            ctx[m["label"]] = {"uid": uid, "pids": pids, "j0": _pids_jiffies(pids)}
        elif m["kind"] == "service":
            ns = _svc_show(m["acct"], m["service"], "CPUUsageNSec")
            ctx[m["label"]] = {"ns0": int(ns) if ns.isdigit() else 0}
    tot0 = _total_cpu_jiffies()
    time.sleep(sample_sec)
    tot1 = _total_cpu_jiffies()
    dtot = max(tot1 - tot0, 1)
    cap_ns = sample_sec * 1e9 * NCPU  # total CPU-ns capacity over the interval

    members, up = [], 0
    for m in roster:
        lbl, kind = m["label"], m["kind"]
        c = ctx[lbl]
        if kind in ("agent", "codex"):
            cpu = round(max(_pids_jiffies(c["pids"]) - c["j0"], 0) / dtot * 100.0, 1)
            mem = _mem_pct_account(c["uid"])
            if not _has_agent_proc(c["uid"]):
                state = "down"
            elif kind == "agent":
                state = "active" if _jsonl_fresh(m["acct"]) else "idle"
            else:  # codex: running but no claude-transcript -> idle (no active detect)
                state = "idle"
        else:  # service
            ns1 = _svc_show(m["acct"], m["service"], "CPUUsageNSec")
            dns = max((int(ns1) if ns1.isdigit() else c["ns0"]) - c["ns0"], 0)
            cpu = round(min(dns / cap_ns * 100.0, 100.0), 1)
            memc = _svc_show(m["acct"], m["service"], "MemoryCurrent")
            mem = round((int(memc) / memtotal * 100.0), 1) if memc.isdigit() and memtotal else 0.0
            state = "idle" if _svc_show(m["acct"], m["service"], "ActiveState") == "active" else "down"
        if state != "down":
            up += 1
        members.append({"label": lbl, "cpu_pct": cpu, "mem_pct": mem, "state": state})

    with open("/proc/loadavg") as f:
        load1 = float(f.read().split()[0])
    cluster = {"load_pct": round(min(load1 / NCPU * 100.0, 100.0), 1),
               "agents_up": up, "agents_total": len(roster)}
    return {"schema": SCHEMA,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cluster": cluster, "agents": members}


def leak_scan(doc):
    """Refuse to emit anything outside the coarse allowlist. Raises on violation."""
    assert set(doc) <= TOP_KEYS, f"top keys: {set(doc) - TOP_KEYS}"
    assert doc.get("schema") == SCHEMA, "schema"
    assert isinstance(doc.get("generated_at"), str), "generated_at"
    c = doc["cluster"]
    assert set(c) <= CLUSTER_KEYS, f"cluster keys: {set(c) - CLUSTER_KEYS}"
    assert 0 <= c["load_pct"] <= 100, "load_pct range"
    assert 0 <= c["agents_up"] <= c["agents_total"], "agents_up range"
    for a in doc["agents"]:
        assert set(a) <= AGENT_KEYS, f"agent keys: {set(a) - AGENT_KEYS}"
        assert a["label"] in ALLOWED_LABELS, f"label not allowlisted: {a['label']}"
        assert a["state"] in STATES, f"state: {a['state']}"
        assert 0 <= a["cpu_pct"] <= 100, "cpu_pct range"
        assert 0 <= a["mem_pct"] <= 100, "mem_pct range"
    blob = json.dumps(doc).lower()
    hits = [s for s in FORBIDDEN if s in blob]
    assert not hits, f"forbidden substrings in output: {hits}"
    return True


def _selftest():
    good = {"schema": SCHEMA, "generated_at": "2026-09-11T00:00:00Z",
            "cluster": {"load_pct": 40.0, "agents_up": 7, "agents_total": 8},
            "agents": [{"label": "orchestrator", "cpu_pct": 10.0, "mem_pct": 20.0, "state": "active"},
                       {"label": "cairn", "cpu_pct": 1.0, "mem_pct": 2.0, "state": "idle"},
                       {"label": "echo", "cpu_pct": 0.5, "mem_pct": 0.1, "state": "idle"},
                       {"label": "mnemosyne", "cpu_pct": 0.0, "mem_pct": 0.0, "state": "down"}]}
    assert leak_scan(good)
    for mutate, why in [
        (lambda d: d.update(hostname="swarm"), "extra top key"),
        (lambda d: d["agents"][0].__setitem__("label", "claude"), "forbidden label"),
        (lambda d: d["agents"][0].__setitem__("state", "active/home"), "forbidden substring"),
        (lambda d: d["agents"][0].__setitem__("label", "codex"), "account-name label"),
    ]:
        bad = json.loads(json.dumps(good)); mutate(bad)
        try:
            leak_scan(bad); raise SystemExit(f"FAIL: {why} not caught")
        except AssertionError:
            pass
    print("selftest: OK (clean passes; extra-key, forbidden-label, forbidden-substring, "
          "account-name-label all caught)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="")
    ap.add_argument("--sample-sec", type=float, default=1.0)
    ap.add_argument("--validate", action="store_true", help="self-test, no real reads")
    args = ap.parse_args()
    if args.validate:
        _selftest(); return 0
    doc = collect(ROSTER, args.sample_sec)
    leak_scan(doc)  # raises -> no write on any violation
    if args.out:
        blob = json.dumps(doc, separators=(",", ":"))
        tmp = args.out + ".tmp"
        with open(tmp, "w") as f:
            f.write(blob)
        os.replace(tmp, args.out)
        print(f"wrote {len(blob)}B -> {args.out}")
    else:
        print(json.dumps(doc, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
