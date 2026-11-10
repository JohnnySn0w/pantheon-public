#!/usr/bin/env python3
"""pve2_heat_monitor.py — alert when pve2's CPU or GPU runs hot for a sustained stretch (T-5309).

Why: the operator (TG 8653/8667) wants pve2 kept off its thermal limits for longevity;
T-5309 capped its power. This watches that the caps hold: the 10-minute AVERAGE of
pve2's CPU package temperature and of the A2000 (passed through to VM210 gpu-ml),
read from the netdata parent in CT201 (both hosts stream to it). Averages, so a
brief build burst does not fire; only sustained heat does.

Alerting is transition-shaped: the first failing run exits 1 (the unit's OnFailure
routes it to Recovery), later failing runs stay quiet for RE_ALERT_S, and a clean run
clears the state. A missing reading or unreachable parent is a problem too
(fail-closed), under the same rate limit.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import tailnet_self  # noqa: E402  (T-5385)

HOST = "pve2"
NETDATA_CT = 201
SSH_TIMEOUT_S = 60
LIMITS_C = {"cpu_package": 90.0, "gpu": 85.0}
RE_ALERT_S = 6 * 3600
DEFAULT_STATE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) \
    / "pantheon" / "pve2-heat" / "state.json"

# Runs inside the netdata parent CT (fed to `python3 -` on stdin). Prints one JSON object.
# Read-only queries against the local netdata API.
REMOTE = r'''
import json, time, urllib.request
B = "http://localhost:19999/host/%s/api/v1/"
def get(host, path):
    with urllib.request.urlopen(B % host + path, timeout=15) as r:
        return json.load(r)
def avg(host, chart):
    for window in (600, 120):  # a chart younger than 10 min answers "no data" for -600
        d = get(host, "data?chart=%s&after=-%d&points=1&group=average&format=json" % (chart, window))
        if d.get("data") and d["data"][0][1] is not None:
            return d["data"][0][1]
    return None
gpu_chart = next((k for k in get("gpu-ml", "charts")["charts"]
                  if k.startswith("nvidia_smi") and k.endswith("_temperature")), None)
print(json.dumps({"now": time.time(), "readings": {
    "cpu_package": avg("pve2", "sensors.temperature_coretemp-isa-0000_temp1_Package_id_0_input"),
    "gpu": avg("gpu-ml", gpu_chart) if gpu_chart else None}}))
'''


def gather(host: str = HOST, *, run=None) -> dict:
    run = subprocess.run if run is None else run  # resolved per call, so tests can patch it
    r = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", host,
             f"timeout {SSH_TIMEOUT_S - 10} pct exec {NETDATA_CT} -- python3 -"],
            input=REMOTE, capture_output=True, text=True, timeout=SSH_TIMEOUT_S)
    if r.returncode != 0:
        raise RuntimeError(f"ssh {host} exited {r.returncode}: {(r.stderr or '').strip()[:200]}")
    return json.loads(r.stdout)


def evaluate(facts: dict) -> list[str]:
    """Problems, one line each; [] when every reading is present and under its limit."""
    problems = []
    for name, limit in LIMITS_C.items():
        v = facts["readings"].get(name)
        if v is None:
            problems.append(f"{name}: no temperature data from netdata")
        elif v > limit:
            problems.append(f"{name}: 10-min average {v:.1f}C (limit {limit:g}C)")
    return problems


def should_alert(problems: list[str], state: dict, now: float) -> bool:
    """Mutates state. True on the first failing run and again every RE_ALERT_S while failing."""
    if not problems:
        state.clear()
        return False
    last = state.get("alerted_at")
    if last is None or now - last > RE_ALERT_S:
        state["alerted_at"] = now
        return True
    return False


def save_state(path: Path, state: dict) -> None:
    """Atomically persist alert state without risking truncation of the prior state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", delete=False) as handle:
            temporary = handle.name
            json.dump(state, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    a = ap.parse_args(argv)
    try:
        state = json.loads(a.state.read_text())
    except (FileNotFoundError, ValueError):
        state = {}
    now = time.time()
    try:
        facts = gather()
        problems = evaluate(facts)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        off = tailnet_self.off_tailnet()
        if off:  # T-5385: this host is off the tailnet; nothing is known about pve2's heat
            tailnet_self.report_off_tailnet("pve2-heat", off, detail=str(exc))
            return 0
        facts, problems = None, [f"could not read temperatures via {HOST} CT{NETDATA_CT}: {exc}"]
    else:
        tailnet_self.note_reachable()
    alert = should_alert(problems, state, now)
    save_state(a.state, state)
    if not problems:
        r = facts["readings"]
        print(f"pve2_heat_monitor: OK cpu_package {r['cpu_package']:.1f}C, gpu {r['gpu']:.1f}C "
              f"(10-min averages) at {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(now))}")
        return 0
    if not alert:
        since = time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime(state['alerted_at']))
        # An unreadable sensor is not a hot one: say which (the 10-23 tailnet outage logged
        # "still hot" for six hours over a box last read at 56C).
        state_word = "still unreadable" if facts is None else "still hot"
        print(f"pve2_heat_monitor: {state_word} (alerted {since}): " + "; ".join(problems))
        return 0
    for p in problems:
        print(f"pve2_heat_monitor: FAIL {p}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
