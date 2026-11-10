#!/usr/bin/env python3
"""pantheon_metrics_gen.py -- emit COARSE, surface-level fleet metrics JSON for the
personal-site PANTHEON window (T-3625).

STATIC-by-construction safety: emits ONLY the allowlisted coarse fields (persona
label + cpu%/mem%/state per member; cluster load%/up/total; and coarse per-host
up/down under ALLOWLISTED host labels for the SYSTEMS//UPLINK panel -- a raw
infra hostname is never emitted; sensitive ones like pve2/boxon stay on the
FORBIDDEN floor). NEVER account/service names, uids, paths, pids, host<->VM<->CT
topology, model pins, session/token internals, or ticket content. A leak_scan()
runs on every output and REFUSES to write if any forbidden content or
non-allowlisted key appears -- the "do not overexpose" guardrail enforced in code.

Writes a LOCAL file. Public exposure (caddy file_server + DNS) is separate.

Roster (operator-confirmed surface, T-3625): 6 claude-harness agents + consultant
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

try:  # dual-identity-safe (run-as-package vs bare run with cwd=tools/)
    from tools.seat_exec import seat_argv
except ImportError:  # pragma: no cover
    from seat_exec import seat_argv  # type: ignore
try:  # T-5655: the exporter's own out-dir constant (never hard-coded here)
    from tools.health_export import DEFAULT_OUT_DIR as HEALTH_DIR
except Exception:  # pragma: no cover - fail open: no dir -> every seat counted as today
    try:
        from health_export import DEFAULT_OUT_DIR as HEALTH_DIR  # type: ignore
    except Exception:
        HEALTH_DIR = None

SCHEMA = "pantheon-metrics/1"
NCPU = os.cpu_count() or 1
ACTIVE_JSONL_FRESH_SEC = 60

ROSTER = [
    {"label": "orchestrator",     "acct": "claude",         "kind": "agent"},
    {"label": "specialist",     "acct": "claude-research", "kind": "agent"},
    {"label": "recovery",      "acct": "claude-fallback", "kind": "agent"},
    {"label": "mason",     "acct": "suborch1",        "kind": "agent"},
    {"label": "artisan",     "acct": "suborch2",        "kind": "agent"},
    {"label": "archivist", "acct": "exocortex",       "kind": "agent"},
    {"label": "consultant",     "acct": "codex",           "kind": "codex"},
    {"label": "echo",      "acct": "claude", "kind": "service", "service": "echo.service"},
]

# Per-host coarse up/down for the personal-site SYSTEMS // UPLINK panel (T-3721).
# The site consumer lowercases label and matches it against ["vps","pve2","boxon",
# "laptop"], rendering up / down / "?" (absent). Probe internals (ssh hostnames)
# stay private -- ONLY {label, up:bool} is emitted and leak_scan-checked.
#   local -> the host this collector runs on (the swarm VPS): up by construction.
#   ssh   -> reachability of sshhost from recovery's account. up = ssh exit != 255;
#            255 is a transport/connect failure, while any other code means the
#            transport connected and the remote ran -- so it distinguishes a
#            down host from a remote-command quirk (e.g. Windows powershell
#            rejecting the unix `true`).
# The two home-cluster hosts emit under GENERIC tokens, NOT their real names:
# pve2 -> "lab", boxon -> "home". This is the operator's A decision on T-3721 (chose A
# over B on 2026-09-15): keep the leak_scan FORBIDDEN floor intact (pve2/boxon
# stay forbidden substrings; only the coarse token + up/down reach the public
# feed) rather than carve real infra names out of the floor. The site remaps its
# visible PVE2/BOXON labels to lab/home to match (specialist owns that). vps + laptop
# keep real labels (they were never on the floor).
HOSTS_ROSTER = [
    {"label": "vps",    "probe": "local"},
    {"label": "laptop", "probe": "ssh", "sshhost": "operator-w11"},
    {"label": "lab",    "probe": "ssh", "sshhost": "pve2"},
    {"label": "home",   "probe": "ssh", "sshhost": "boxon"},
]

# --- output allowlist (leak_scan invariants) ---
TOP_KEYS = {"schema", "generated_at", "cluster", "agents", "hosts"}
CLUSTER_KEYS = {"load_pct", "agents_up", "agents_total"}
AGENT_KEYS = {"label", "cpu_pct", "mem_pct", "state"}
HOST_KEYS = {"label", "up"}
STATES = {"active", "idle", "down", "unknown"}
# T-4070: "unknown" = the cross-account probe itself FAILED (sudo/bus error), which is
# not a real negative. The site consumer (T-3626) hard-guards the schema and falls back
# to simulated data on an unexpected value, so "unknown" is only EMITTED once the site
# accepts it. Until then it renders as the legacy "down", with a stderr line saying it
# was a probe failure, not a real down.
EMIT_UNKNOWN = os.environ.get("PANTHEON_METRICS_EMIT_UNKNOWN") == "1"
ALLOWED_LABELS = {m["label"] for m in ROSTER}
ALLOWED_HOST_LABELS = {h["label"] for h in HOSTS_ROSTER}
# Forbidden substrings: account/service names + host/path/id chrome. Persona
# labels (orchestrator/specialist/recovery/mason/artisan/archivist/consultant/echo) are the ALLOWED
# public identifiers; the underlying account names (claude/claude-research/
# codex/...) and the ".service" suffix are forbidden -- disjoint from the labels.
FORBIDDEN = [
    "claude", "research", "fallback", "suborch", "exocortex", "codex",
    "the-ancestor", "/home", "your-domain", "swarm", "boxon", "pve2", "gpu-ml",
    ".service", ".scope", "tmux", "/proc", "/sys", "uid", "pid", "jsonl",
]


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True)


def _seat_argv(account, cmd):
    """argv to run `bash -c cmd` AS `account` — DIRECTLY (no sudo) when `account` is our
    own, else `sudo -u <account> bash -c cmd`. Delegates to the shared seat_exec.seat_argv
    (T-4066) so the own-account-direct discrimination lives in one place; preserves this
    tool's original `sudo -u` (no -n) shape via the sudo= override. The class this guards:
    bugs/sudo-u-to-runners-own-account-fails-per-seat-cross-account-tool-false-null.md."""
    return seat_argv(account, ["bash", "-c", cmd], sudo=("sudo", "-u"))


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
    """True/False = fresh or not; None = the probe FAILED (T-4070: not 'idle')."""
    cmd = ('find ~/.claude/projects -maxdepth 2 -name "*.jsonl" '
           '-printf "%T@\\n" 2>/dev/null | sort -rn | head -1')
    out = _run(_seat_argv(account, cmd))
    if out.returncode != 0:
        return None
    try:
        return (time.time() - float(out.stdout.strip())) <= ACTIVE_JSONL_FRESH_SEC
    except ValueError:
        return False


def _svc_show(account, service, prop):
    """The property value, or None when the probe FAILED (rc != 0: sudo/bus error).
    T-4070: a failed probe is NOT an empty/"inactive" answer. systemctl itself exits 0
    with "inactive" for a stopped or unknown unit."""
    out = _run(_seat_argv(account,
                f'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user show '
                f'{service} -p {prop} --value'))
    if out.returncode != 0:
        return None
    return out.stdout.strip()


# T-5655: ORG-106 decision 6 -- an expected deep-sleep is calm, not an incident.
# Source of truth for "asleep" = health_export's per-persona record
# (<HEALTH_DIR>/<persona>.json, rewritten every 60s on THIS account), the same field kino's
# "asleep" glance keys on. No sudo, no second reader of the seat-state record. The
# per-persona `exported_at` is authoritative (health_export stamps each record with its
# own run time); the _exporter.json heartbeat adds nothing a fresh record lacks.
HEALTH_RECORD_MAX_AGE_SEC = 180  # 3 exporter cycles; age > this = stale = ignored


def _expected_down(persona, health_dir=None, now=None):
    """True ONLY for a fresh (age <= HEALTH_RECORD_MAX_AGE_SEC), well-formed health record
    whose `expected_down` is exactly True. FAIL-OPEN: a missing, stale, future-dated,
    unreadable, malformed or wrongly-typed record is False, so the seat is counted exactly
    as before and an outage can never read as calm."""
    try:
        base = HEALTH_DIR if health_dir is None else health_dir
        if base is None:
            return False
        with open(os.path.join(str(base), f"{persona}.json"), "r", encoding="ascii") as f:
            doc = json.loads(f.read(65536))
        exported_at = doc.get("exported_at")
        if isinstance(exported_at, bool) or not isinstance(exported_at, (int, float)):
            return False
        age = (time.time() if now is None else now) - exported_at
        if age < 0 or age > HEALTH_RECORD_MAX_AGE_SEC:
            return False
        return doc.get("expected_down") is True
    except Exception:  # noqa: BLE001
        return False


def count_cluster(entries):
    """(up, total) from [(state, asleep)] per roster row. A row is dropped from the TOTAL
    only when it is a confirmed "down" AND expected-down (asleep); it is never counted up.
    A live asleep-flagged seat and an "unknown" probe stay in the total (T-4070 principle:
    unknown is not a negative). So 0 <= up <= total always holds."""
    up = total = 0
    for state, asleep in entries:
        if state == "down" and asleep:
            continue
        total += 1
        if state not in ("down", "unknown"):
            up += 1
    return up, total


def _host_up(entry):
    """Coarse reachability for one UPLINK host. local -> True (the collector runs
    on it). ssh -> up unless ssh reports a transport/connect failure (exit 255)
    or the probe times out. BatchMode + ConnectTimeout so a down host fails fast
    and never hangs the push timer; the remote command's own exit code is
    irrelevant (we only care that the transport connected -- see HOSTS_ROSTER)."""
    if entry["probe"] == "local":
        return True
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4",
             "-o", "StrictHostKeyChecking=accept-new", entry["sshhost"], "exit"],
            capture_output=True, text=True, timeout=8)
        return r.returncode != 255
    except (subprocess.TimeoutExpired, OSError):
        return False


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
            ns = _svc_show(m["acct"], m["service"], "CPUUsageNSec") or ""
            ctx[m["label"]] = {"ns0": int(ns) if ns.isdigit() else 0}
    tot0 = _total_cpu_jiffies()
    time.sleep(sample_sec)
    tot1 = _total_cpu_jiffies()
    dtot = max(tot1 - tot0, 1)
    cap_ns = sample_sec * 1e9 * NCPU  # total CPU-ns capacity over the interval

    members, entries = [], []
    for m in roster:
        lbl, kind = m["label"], m["kind"]
        c = ctx[lbl]
        if kind in ("agent", "codex"):
            # Clamped like the service branch: over a short window, jiffy granularity can
            # put an account's delta past the total, and leak_scan would then refuse the
            # WHOLE write (the site falls back to simulated data).
            cpu = round(min(max(_pids_jiffies(c["pids"]) - c["j0"], 0) / dtot * 100.0, 100.0), 1)
            mem = _mem_pct_account(c["uid"])
            if c["uid"] < 0:
                state = "unknown"  # T-4070: `id -u` failed -- can't look, not "down"
            elif not _has_agent_proc(c["uid"]):
                state = "down"
            elif kind == "agent":
                fresh = _jsonl_fresh(m["acct"])
                state = "unknown" if fresh is None else ("active" if fresh else "idle")
            else:  # codex: running but no claude-transcript -> idle (no active detect)
                state = "idle"
        else:  # service
            ns1 = _svc_show(m["acct"], m["service"], "CPUUsageNSec") or ""
            dns = max((int(ns1) if ns1.isdigit() else c["ns0"]) - c["ns0"], 0)
            cpu = round(min(dns / cap_ns * 100.0, 100.0), 1)
            memc = _svc_show(m["acct"], m["service"], "MemoryCurrent") or ""
            mem = round((int(memc) / memtotal * 100.0), 1) if memc.isdigit() and memtotal else 0.0
            active = _svc_show(m["acct"], m["service"], "ActiveState")
            state = "unknown" if active is None else ("idle" if active == "active" else "down")
        # T-5655: asked only for a probe-CONFIRMED down, before the legacy unknown->down
        # remap below (a failed probe must never be excluded as "asleep"). Persona == label
        # for every seat row; the echo service has no seat record.
        asleep = (state == "down" and kind in ("agent", "codex") and _expected_down(lbl))
        if asleep:
            print(f"[pantheon_metrics_gen] {lbl}: expected-down (ORG-106 deep-sleep); "
                  f"excluded from agents_total, state stays 'down' (T-5655)", file=sys.stderr)
        if state == "unknown" and not EMIT_UNKNOWN:
            print(f"[pantheon_metrics_gen] {lbl}: probe FAILED; rendering the legacy 'down' "
                  f"until the site accepts 'unknown' (T-4070)", file=sys.stderr)
            state = "down"
        entries.append((state, asleep))
        members.append({"label": lbl, "cpu_pct": cpu, "mem_pct": mem, "state": state})

    with open("/proc/loadavg") as f:
        load1 = float(f.read().split()[0])
    # agents_total = seats EXPECTED to be running (T-5655): a confirmed expected-down
    # (ORG-106 deep-sleep) seat is omitted from it, so it can be < len(agents); that seat
    # still appears in `agents` with state "down". No new key or state (public schema).
    up, total = count_cluster(entries)
    cluster = {"load_pct": round(min(load1 / NCPU * 100.0, 100.0), 1),
               "agents_up": up, "agents_total": total}
    hosts = [{"label": h["label"], "up": _host_up(h)} for h in HOSTS_ROSTER]
    return {"schema": SCHEMA,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cluster": cluster, "agents": members, "hosts": hosts}


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
    for h in doc.get("hosts", []):  # optional key; absent doc still validates
        assert set(h) <= HOST_KEYS, f"host keys: {set(h) - HOST_KEYS}"
        assert h["label"] in ALLOWED_HOST_LABELS, f"host label not allowlisted: {h['label']}"
        assert isinstance(h["up"], bool), f"host up not bool: {h['label']}"
    blob = json.dumps(doc).lower()
    hits = [s for s in FORBIDDEN if s in blob]
    assert not hits, f"forbidden substrings in output: {hits}"
    return True


def _selftest():
    good = {"schema": SCHEMA, "generated_at": "2026-09-11T00:00:00Z",
            "cluster": {"load_pct": 40.0, "agents_up": 7, "agents_total": 8},
            "agents": [{"label": "orchestrator", "cpu_pct": 10.0, "mem_pct": 20.0, "state": "active"},
                       {"label": "consultant", "cpu_pct": 1.0, "mem_pct": 2.0, "state": "idle"},
                       {"label": "echo", "cpu_pct": 0.5, "mem_pct": 0.1, "state": "idle"},
                       {"label": "archivist", "cpu_pct": 0.0, "mem_pct": 0.0, "state": "down"}],
            "hosts": [{"label": "vps", "up": True},
                      {"label": "laptop", "up": False}]}
    assert leak_scan(good)
    # hosts is optional: a doc predating the field still validates (backward-compat).
    nohosts = json.loads(json.dumps(good)); del nohosts["hosts"]
    assert leak_scan(nohosts)
    for mutate, why in [
        (lambda d: d.update(hostname="swarm"), "extra top key"),
        (lambda d: d["agents"][0].__setitem__("label", "claude"), "forbidden label"),
        (lambda d: d["agents"][0].__setitem__("state", "active/home"), "forbidden substring"),
        (lambda d: d["agents"][0].__setitem__("label", "codex"), "account-name label"),
        (lambda d: d["hosts"][0].__setitem__("zone", "dc1"), "extra host key"),
        # "node9" is neither a FORBIDDEN substring nor allowlisted -> isolates the
        # ALLOWED_HOST_LABELS guard (a forbidden name like pve2 would be caught by
        # the substring scan too, so it would not prove THIS guard fires).
        (lambda d: d["hosts"][0].__setitem__("label", "node9"), "host label not allowlisted"),
        # belt-and-suspenders: a forbidden infra name is doubly blocked (label
        # allowlist AND the FORBIDDEN substring scan).
        (lambda d: d["hosts"][0].__setitem__("label", "pve2"), "forbidden host label"),
        (lambda d: d["hosts"][0].__setitem__("up", "yes"), "host up non-bool"),
    ]:
        bad = json.loads(json.dumps(good)); mutate(bad)
        try:
            leak_scan(bad); raise SystemExit(f"FAIL: {why} not caught")
        except AssertionError:
            pass
    print("selftest: OK (clean + hosts pass; no-hosts back-compat; extra-key, forbidden-label, "
          "forbidden-substring, account-name-label, extra-host-key, host-label-not-allowlisted, "
          "forbidden-host-label, host-up-non-bool all caught)")


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
