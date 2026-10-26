#!/usr/bin/env python3
"""disk_health_monitor.py — alert on pve2 disk defects, wear and write rate from RAW SMART (T-4997).

Why: pve2's sdb carried ~1600 pending sectors for weeks behind a SMART "PASSED"
verdict before it dropped off the bus (T-4992). So this never reads the overall
verdict. Per physical disk (enumerated with lsblk each run, so a newly added drive
is covered without an edit) it checks:
  - defect counts, raw: 5 Reallocated, 187 Reported_Uncorrect, 197 Current_Pending,
    198 Offline_Uncorrectable, 199 UDMA_CRC. Problem when above the disk's acked
    value (hosts/pve2/disk-health-ack.json, keyed by serial; default 0), so a known
    stable count stays quiet and any rise fires. NVMe: media_errors, critical_warning.
  - life used: SSD wear attr (202/231/233/177, 100 - normalized) or NVMe
    percentage_used; HDD the vendor's own Load_Cycle gauge (193, 100 - normalized).
    Problem at >= LIFE_USED_LIMIT_PCT.
  - write rate: bytes written per day against the newest sufficiently old history
    sample, versus a per-model daily budget (rated TBW over 5 years; HDD rated
    workload). SSDs use >=20h; HDDs use >=6d because their workload rating is an
    annual average and pve2 writes its full guest backup in one weekly burst.
  - kernel ATA link resets / I/O errors in the last 26h.
  - a SATA/NVMe disk whose SMART cannot be read is a problem (unknown is not OK).
    USB-bridged sticks are listed as not monitorable instead.
pve2's nvme0n1 (the operator's Windows install) is excluded by serial and never queried.
gpu-ml (VM210) has only virtual disks on pve2's drives, so pve2 covers it.

One ssh call gathers facts; evaluate() is pure. History (JSONL) lives on the VPS,
off the monitored disks. A problem exits 1 and the unit's OnFailure routes it to
Recovery; a host that cannot be read is also exit 1 (fail-closed).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HOST = "pve2"
SSH_TIMEOUT_S = 120
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from tools import tailnet_self  # noqa: E402  (T-5385, T-5390)
DEFAULT_ACK = REPO / "hosts" / "pve2" / "disk-health-ack.json"
DEFAULT_HISTORY = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) \
    / "pantheon" / "disk-health" / "history.jsonl"
HISTORY_KEEP = 400
MIN_RATE_WINDOW_S = 20 * 3600
HDD_RATE_WINDOW_S = 6 * 86400
LIFE_USED_LIMIT_PCT = 90

# the operator's Windows NVMe on pve2: off limits, never touched or queried.
EXCLUDE_SERIALS = ("S75YNF0X238969",)

DEFECT_ATTRS = {5: "Reallocated", 187: "Reported_Uncorrect", 197: "Current_Pending",
                198: "Offline_Uncorrectable", 199: "UDMA_CRC"}
SSD_WEAR_ATTRS = (202, 231, 233, 177)
WRITTEN_LBA_ATTRS = (241, 246)

# model prefix -> ratings. tbw: rated terabytes written (SSD); workload_tb_yr: rated
# annual workload (HDD); load_cycles: rated load/unload cycles (HDD).
RATINGS = {
    "CT1000MX500": {"tbw": 360e12},
    "ST1000DM003": {"workload_tb_yr": 55e12, "load_cycles": 300_000},
    "ST3000DM008": {"workload_tb_yr": 55e12, "load_cycles": 300_000},
}

# Runs on the host as root (fed to `python3 -` on stdin). Prints one JSON object. Read-only.
REMOTE = r'''
import json, re, subprocess, time
blk = json.loads(subprocess.run(["lsblk", "-J", "-d", "-o", "NAME,TYPE,TRAN,SERIAL"],
                                capture_output=True, text=True, check=True).stdout)
disks = []
for b in blk["blockdevices"]:
    if b.get("type") != "disk" or (b.get("serial") or "") in EXCLUDE:
        continue
    r = subprocess.run(["smartctl", "-j", "-a", "/dev/" + b["name"]], capture_output=True, text=True)
    try:
        sm = json.loads(r.stdout)
    except ValueError:
        sm = {"smartctl": {"exit_status": r.returncode, "messages": [{"string": r.stderr[:200]}]}}
    disks.append({"name": b["name"], "tran": b.get("tran"), "smart": sm})
k = subprocess.run(["journalctl", "-k", "--since", "-26h", "--no-pager", "-o", "cat"],
                   capture_output=True, text=True).stdout
pat = re.compile(r"ata\d+(\.\d+)?: (hard resetting link|SError|exception Emask|failed command|link is slow)"
                 r"|I/O error, dev sd|Buffer I/O error")
print(json.dumps({"host": __import__("os").uname().nodename, "now": time.time(), "disks": disks,
                  "kernel_errors": [l for l in k.splitlines() if pat.search(l)]}))
'''


def remote_script() -> str:
    return f"EXCLUDE = {list(EXCLUDE_SERIALS)!r}\n" + REMOTE


def gather(host: str = HOST, *, run=None) -> dict:
    run = subprocess.run if run is None else run  # resolved per call, so tests can patch it
    r = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host,
             f"timeout {SSH_TIMEOUT_S - 10} python3 -"],
            input=remote_script(), capture_output=True, text=True, timeout=SSH_TIMEOUT_S)
    if r.returncode != 0:
        raise RuntimeError(f"ssh {host} exited {r.returncode}: {(r.stderr or '').strip()[:200]}")
    return json.loads(r.stdout)


def _rating(model: str) -> dict:
    return next((v for k, v in RATINGS.items() if model.startswith(k)), {})


def summarise(facts: dict) -> list[dict]:
    """One dict per monitorable disk; unreadable ones carry 'error'. USB sticks are dropped."""
    out = []
    for d in facts["disks"]:
        sm, name = d["smart"], d["name"]
        serial = sm.get("serial_number") or f"?{name}"
        model = sm.get("model_name") or "?"
        nvme = sm.get("nvme_smart_health_information_log")
        attrs = {a["id"]: a for a in sm.get("ata_smart_attributes", {}).get("table", [])}
        if not attrs and nvme is None:
            if d.get("tran") == "usb":
                continue
            msgs = "; ".join(m.get("string", "") for m in sm.get("smartctl", {}).get("messages", []))
            out.append({"name": name, "serial": serial, "model": model,
                        "error": f"could not read SMART ({msgs or 'no attributes'})"})
            continue
        s = {"name": name, "serial": serial, "model": model, "defects": {},
             "life_used_pct": None, "written_bytes": None, "tbw_used_pct": None,
             "rating": _rating(model), "power_on_hours": sm.get("power_on_time", {}).get("hours"),
             "temp_c": sm.get("temperature", {}).get("current")}
        if nvme is not None:
            s["kind"] = "nvme"
            s["defects"] = {"media_errors": nvme.get("media_errors", 0),
                            "critical_warning": nvme.get("critical_warning", 0)}
            s["life_used_pct"] = nvme.get("percentage_used")
            if nvme.get("data_units_written") is not None:
                s["written_bytes"] = nvme["data_units_written"] * 512_000
        else:
            s["kind"] = "ssd" if sm.get("rotation_rate") == 0 else "hdd"
            s["defects"] = {str(i): int(attrs[i]["raw"]["value"]) for i in DEFECT_ATTRS if i in attrs}
            wear = SSD_WEAR_ATTRS if s["kind"] == "ssd" else (193,)
            w = next((attrs[i] for i in wear if i in attrs and attrs[i]["value"] <= 100), None)
            if w is not None:
                s["life_used_pct"] = 100 - w["value"]
            lba = next((attrs[i] for i in WRITTEN_LBA_ATTRS if i in attrs), None)
            if lba is not None:
                s["written_bytes"] = int(lba["raw"]["value"]) * sm.get("logical_block_size", 512)
            if 193 in attrs and s["rating"].get("load_cycles"):
                s["load_cycles"] = int(attrs[193]["raw"]["value"])
        if s["written_bytes"] is not None and s["rating"].get("tbw"):
            s["tbw_used_pct"] = round(100 * s["written_bytes"] / s["rating"]["tbw"], 1)
        out.append(s)
    return out


def daily_budget_bytes(s: dict) -> float | None:
    r = s["rating"]
    if r.get("tbw"):
        return r["tbw"] / (5 * 365)
    if r.get("workload_tb_yr"):
        return r["workload_tb_yr"] / 365
    return None


def evaluate(facts: dict, *, ack: dict, history: list[dict]) -> list[str]:
    """Problems, one line each; [] when every disk is clean."""
    host, now = facts["host"], facts["now"]
    problems = []
    for s in summarise(facts):
        tag = f"{host} {s['name']} {s['model']} {s['serial']}"
        if "error" in s:
            problems.append(f"{tag}: {s['error']}")
            continue
        acked = ack.get(s["serial"], {}).get("attrs", {})
        for k, v in s["defects"].items():
            if v > acked.get(k, 0):
                label = f"{k} {DEFECT_ATTRS[int(k)]}" if k.isdigit() else k
                problems.append(f"{tag}: {label}={v}" + (f" (acked {acked[k]})" if k in acked else ""))
        if s["life_used_pct"] is not None and s["life_used_pct"] >= LIFE_USED_LIMIT_PCT:
            problems.append(f"{tag}: {s['life_used_pct']}% of rated life used")
        budget = daily_budget_bytes(s)
        min_rate_window = HDD_RATE_WINDOW_S if s["kind"] == "hdd" else MIN_RATE_WINDOW_S
        base = next((h for h in reversed(history)
                     if h.get("host") == host and now - h["now"] >= min_rate_window), None)
        prev = (base or {}).get("disks", {}).get(s["serial"], {}).get("written_bytes")
        if budget and prev is not None and s["written_bytes"] is not None:
            rate = (s["written_bytes"] - prev) / ((now - base["now"]) / 86400)
            if rate > budget:
                problems.append(f"{tag}: wrote {rate / 1e9:.0f} GB/day (budget {budget / 1e9:.0f} GB/day)")
    ke = facts.get("kernel_errors") or []
    if ke:
        problems.append(f"{host}: {len(ke)} kernel ATA error line(s) in the last 26h, e.g. {ke[0][:160]}")
    return problems


def history_row(facts: dict) -> dict:
    return {"host": facts["host"], "now": facts["now"],
            "disks": {s["serial"]: {k: s.get(k) for k in ("name", "model", "defects", "life_used_pct",
                                                          "written_bytes", "power_on_hours", "temp_c",
                                                          "load_cycles")}
                      for s in summarise(facts) if "error" not in s}}


def status_line(s: dict) -> str:
    if "error" in s:
        return f"{s['name']} {s['model']} {s['serial']}: {s['error']}"
    bits = [f"{s['name']} {s['model']} {s['serial']} ({s['kind']})"]
    if s["life_used_pct"] is not None:
        bits.append(f"life {s['life_used_pct']}%")
    if s.get("load_cycles"):
        bits.append(f"load cycles {s['load_cycles']}/{s['rating']['load_cycles']}")
    if s["tbw_used_pct"] is not None:
        bits.append(f"TBW {s['tbw_used_pct']}%")
    elif s["written_bytes"] is not None:
        bits.append(f"written {s['written_bytes'] / 1e12:.1f} TB")
    if s["power_on_hours"] is not None:
        bits.append(f"{s['power_on_hours']} h")
    nz = {k: v for k, v in s["defects"].items() if v}
    bits.append(f"defects {nz}" if nz else "defects 0")
    return ", ".join(bits)


def _load_history(path: Path) -> list[dict]:
    try:
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    except FileNotFoundError:
        return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--ack", type=Path, default=DEFAULT_ACK)
    ap.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    a = ap.parse_args(argv)
    try:
        facts = gather(a.host)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        off = tailnet_self.off_tailnet()
        if off:  # T-5390: this host is off the tailnet; nothing is known about pve2's disks
            tailnet_self.report_off_tailnet("pve2-disk-health", off, detail=str(exc))
            return 0
        print(f"disk_health_monitor: FAIL could not read disks on {a.host}: {exc}", file=sys.stderr)
        return 1
    tailnet_self.note_reachable()
    ack = json.loads(a.ack.read_text()) if a.ack.exists() else {}
    history = _load_history(a.history)
    problems = evaluate(facts, ack=ack, history=history)
    for s in summarise(facts):
        print(f"disk_health_monitor: {facts['host']}: {status_line(s)}")
    a.history.parent.mkdir(parents=True, exist_ok=True)
    rows = (history + [history_row(facts)])[-HISTORY_KEEP:]
    tmp = a.history.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    tmp.replace(a.history)
    for p in problems:
        print(f"disk_health_monitor: FAIL {p}", file=sys.stderr)
    if not problems:
        print(f"disk_health_monitor: OK {a.host} at {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
