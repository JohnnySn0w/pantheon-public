#!/usr/bin/env python3
"""resource_sampler — per-process memory attribution, so the NEXT spike is diagnosable (T-2213).

WHY THIS EXISTS. On 2026-07-28 the box thrashed to a standstill and the operator force-restarted it.
The post-mortem was reconstructable only as far as sysstat goes: sar showed memory 39% -> 95%,
swap 48% -> 100%, %system 8% -> 95% while %user collapsed — a textbook thrash spiral. What it
could NOT show is WHO. sar has no per-process dimension, the kernel journal is root/adm-only, and
/home/ci-runner is outside recovery's sudo. So the single most useful fact — which process took ~3GB
between 15:20 and 15:30 — is unrecoverable.

This closes exactly that gap and nothing more. It is not a monitor, it raises no alerts, and it
makes no judgements about what is "too much": it writes a small periodic record of who is holding
memory so that a future post-mortem has a per-process history to read. Attribution, not alerting.

DESIGN CONSTRAINTS, all learned from the incident it exists for:
  - CHEAP. It runs on a box that has already died of memory pressure once. It reads /proc, holds
    one sample in memory, appends one line, and exits. No accumulation in the sampler itself.
  - APPEND-ONLY JSONL, SELF-ROTATING. An unbounded log on a constrained box is a second outage
    waiting to happen; a log that rotates by deleting the newest is useless. Oldest-first
    truncation, bounded by line count.
  - NEVER FAILS THE CALLER. A sampler that crashes during a memory event destroys the only
    evidence of that event. Every failure path is swallowed and the run exits 0.
  - RECORDS SWAP, NOT JUST RSS. The 2026-07-28 spiral was a SWAP exhaustion story: RSS alone
    would have shown the fleet looking unremarkable while swap filled underneath it.
  - RECORDS MEMORY PSI (added 2026-09-02). mem_used_pct is RAM-fullness, not stall; the box
    routinely sits high on cache with ~0 stall. PSI is the stall signal AND the exact metric
    systemd-oomd now sheds on (T-2241) — sampling it makes the next post-mortem and the
    T-3399 threshold-calibration data-driven instead of blind.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

LOG_PATH = Path.home() / ".local" / "state" / "pantheon" / "resource_samples.jsonl"
MAX_LINES = 20000          # ~7 days at 5-min cadence; a few MB at most
TOP_N = 12                 # per-sample process cap — the tail is noise, not evidence
MIN_RSS_KB = 20 * 1024     # ignore anything under 20MB


def _meminfo() -> dict[str, int]:
    out: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, rest = line.partition(":")
            v = rest.strip().split(" ")[0]
            if v.isdigit():
                out[k] = int(v)
    except OSError:
        pass
    return out


def _parse_mem_pressure(text: str) -> dict[str, float]:
    """Parse /proc/pressure/memory content into flat psi_mem_{some,full}_avg{10,60,300}
    floats. Tolerant: unknown lines/fields are skipped, a malformed value is skipped
    (never raises) — a sampler that dies mid-event destroys the evidence it exists for."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        kind, _, rest = line.partition(" ")  # "some" / "full"
        if kind not in ("some", "full"):
            continue
        for field in rest.split():
            k, _, v = field.partition("=")
            if k.startswith("avg"):
                try:
                    out[f"psi_mem_{kind}_{k}"] = float(v)
                except ValueError:
                    continue
    return out


def _mem_pressure() -> dict[str, float]:
    """Memory PSI (pressure stall info): the 'how much did tasks STALL waiting for
    memory' signal that mem_used_pct/swap cannot show — a box can be 90% RAM-full
    with ~0% PSI when the fill is cache. It is also the EXACT metric systemd-oomd
    acts on (T-2241, live 2026-09-01: DefaultMemoryPressureLimit=60%/20s on
    user.slice), so sampling it lets the T-3399 soak calibrate that threshold
    against real load and lets a future oomd kill be classed real-vs-false.
    some/full averaged over 10/60/300s. Empty on any error (no PSI kernel, EACCES)."""
    try:
        return _parse_mem_pressure(Path("/proc/pressure/memory").read_text())
    except OSError:
        return {}


def _proc_rows() -> list[dict]:
    """Per-process RSS + swap. Skips anything unreadable — a peer's 700-mode /proc
    entry or a process that exits mid-scan must not abort the sample."""
    rows: list[dict] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            status = Path(f"/proc/{entry}/status").read_text()
        except (OSError, UnicodeDecodeError):
            continue  # exited mid-scan, or not ours to read
        rss = swap = 0
        name = ""
        uid = ""
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1])
            elif line.startswith("VmSwap:"):
                swap = int(line.split()[1])
            elif line.startswith("Name:"):
                name = line.split(maxsplit=1)[-1].strip()
            elif line.startswith("Uid:"):
                uid = line.split()[1]
        if rss < MIN_RSS_KB and swap < MIN_RSS_KB:
            continue
        rows.append({"pid": int(entry), "comm": name, "uid": uid,
                     "rss_kb": rss, "swap_kb": swap})
    rows.sort(key=lambda r: r["rss_kb"] + r["swap_kb"], reverse=True)
    return rows[:TOP_N]


def _rotate(path: Path, max_lines: int) -> None:
    """Keep the NEWEST max_lines. Oldest-first truncation: during an incident the
    recent samples are the evidence, so dropping them to stay small would defeat
    the purpose."""
    try:
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) <= max_lines:
            return
        with path.open("w", encoding="utf-8") as fh:
            fh.writelines(lines[-max_lines:])
    except OSError:
        pass


def sample(now: float | None = None) -> dict:
    mem = _meminfo()
    total, free = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
    sw_t, sw_f = mem.get("SwapTotal", 0), mem.get("SwapFree", 0)
    return {
        "ts": int(now if now is not None else time.time()),
        "mem_total_kb": total,
        "mem_available_kb": free,
        "mem_used_pct": round((total - free) * 100 / total, 1) if total else None,
        "swap_total_kb": sw_t,
        "swap_used_kb": sw_t - sw_f,
        "swap_used_pct": round((sw_t - sw_f) * 100 / sw_t, 1) if sw_t else None,
        "loadavg": os.getloadavg()[0],
        **_mem_pressure(),
        "procs": _proc_rows(),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Per-process memory attribution sampler (T-2213).")
    ap.add_argument("--print", action="store_true", help="print the sample instead of appending")
    ap.add_argument("--log", default=None, help="override the log path")
    args = ap.parse_args(argv)

    try:
        rec = sample()
    except Exception:
        return 0  # never fail: a crashed sampler destroys the evidence it exists to keep

    if args.print:
        print(json.dumps(rec, indent=1))
        return 0

    path = Path(args.log) if args.log else LOG_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        _rotate(path, MAX_LINES)
    except OSError:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
