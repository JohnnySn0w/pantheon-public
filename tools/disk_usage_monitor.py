#!/usr/bin/env python3
"""Proactive resource monitor for the fleet — disk + RAM + CPU (2026-06-26 the operator ask; RAM/CPU legs T-1632).

The operator flagged (TG 3366-3367) that the box was getting full and we want resource
monitoring going forward. At first look the root FS was at 82% (125G/154G), almost
entirely from one agent's build/venv caches (echo-prismatic/target = 51G of Rust
build artifacts). Nothing was watching disk, so it could have silently filled to
100% — which wedges every agent (can't write session logs, handoffs, commits).

This closes that gap. T-1632 (the operator TG 3718) added RAM (MemAvailable absolute
floors — the 2026-07-02 reaper-killed-builds incident class) and CPU (load15 vs
cores — sustained runaway, not build spikes) legs on the same plumbing. All legs
are PURE /proc + df reads — no LLM, no Claude API — mirroring
credential_expiry_monitor: hourly Recovery timer, alerts the operator DIRECTLY via the
Telegram BOT token (independent of Claude OAuth, so it delivers even mid-outage),
throttled per severity class so the hourly timer nags without spamming.

Classify root-FS usage (first match wins):
  URGENT  — used% >= urgent threshold (default 92) — act now, near wedge
  WARN    — used% >= warn   threshold (default 85) — reclaim soon
  OK      — otherwise

On breach the alert appends a best-effort top-consumers breakdown (per-home sizes)
so it is actionable at a glance; that breakdown is wrapped so it can never break the
core check. Default-safe (T-0261): refuses a flagless run. Exit 0 always — a monitor
must not fail its own timer.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Filesystem to watch (the one that wedges the fleet if it fills).
WATCH_PATH = os.environ.get("PANTHEON_DISK_WATCH_PATH", "/")

# Tunable (env-overridable) thresholds, percent-used.
WARN_PCT = int(os.environ.get("PANTHEON_DISK_WARN_PCT", "85"))
URGENT_PCT = int(os.environ.get("PANTHEON_DISK_URGENT_PCT", "92"))

# Re-alert cadence per severity class (don't spam the hourly timer).
ALERT_COOLDOWN_SECONDS = int(os.environ.get("PANTHEON_DISK_ALERT_COOLDOWN_SECONDS", str(6 * 3600)))

# --- RAM leg (T-1632, the operator TG 3718) ------------------------------------
# ABSOLUTE MemAvailable floors, not percent: the failure mode is "a build eats
# the box" (the 2026-07-02 host-side reaper killing two cargo builds), and what
# matters is how much headroom remains for everyone else, which doesn't scale
# with total RAM. Defaults sized for the 7.8G box.
RAM_WARN_MB = int(os.environ.get("PANTHEON_RAM_WARN_MB", "1024"))
RAM_URGENT_MB = int(os.environ.get("PANTHEON_RAM_URGENT_MB", "512"))

# --- CPU leg (T-1632) ------------------------------------------------------
# load15 vs core count: the 15-minute average is ALREADY "sustained" (an hourly
# timer can't do multi-poll confirmation with useful latency, and single-poll
# load1 would cry wolf at normal build spikes). WARN when load15 exceeds
# multiplier × nproc — a runaway pegging cores for 15+ minutes.
#
# RECALIBRATED 2.0 → 2.5 (2026-07-10, after the first three live fires): the
# fleet's LEGITIMATE operating band with the echo daemon (~0.7 cores constant)
# + the ci-runner's cargo jobs overlapping sits at load15 ≈ 8-9 on 4 cores —
# exactly the old 8.0 line, so the third fire was a 8.08 barely-over re-triage
# of already-classified steady state. Empirical anchors: legit band ≤ ~9;
# the one genuine contention event hit 27 (2026-07-10 02:50Z, five-workspace
# sprint + CI + echo). 2.5 (warn at 10 on 4 cores) sits above the legit band
# and far below the runaway signature. Re-lower if the echo daemon's constant
# burn gets fixed or CI moves off-box.
CPU_LOAD15_WARN_MULT = float(os.environ.get("PANTHEON_CPU_LOAD15_WARN_MULT", "2.5"))

# Agent homes to size in the breakdown (best-effort; du runs as each owner).
#
# DERIVED from instance.yaml (T-2256). Was a hand-carried five that predated
# ORG-042/ORG-043, so exocortex/suborch1/suborch2 were never sized — and the
# "best-effort" in the line above is about the du CALL failing, not about which
# homes are in scope, so three homes were absent from every top-consumers
# breakdown with nothing marking their absence. That is the signature this
# ticket exists to remove: a disk alert naming the biggest home is worth exactly
# as much as the enumeration behind it.
from tools.agent_role_map import fleet_accounts  # noqa: E402

AGENT_HOMES = fleet_accounts()

# Per-home du bound + a SHARED total budget for the whole breakdown sweep. The
# service TimeoutStartSec is 2min; 8 homes each waiting a per-home timeout in turn
# could blow that and fire OnFailure (reporting the monitor, not the fault — the
# same-second-timeout trap). So the sweep stops sizing once the shared budget is
# spent (marking the rest ?skipped), and a home that cannot be sized within its
# per-home bound is MARKED ?timeout, never dropped: an unsizable home is itself the
# actionable signal. (The actual 2026-09-06 miss — T-3441 — was NOT a timeout but a
# nonzero-du-exit drop; see the rc handling in _top_consumers. This budget is the
# defensive companion so a genuinely slow home also stays visible within budget.)
DU_PER_HOME_TIMEOUT_SEC = int(os.environ.get("PANTHEON_DISK_DU_PER_HOME_TIMEOUT_SEC", "30"))
DU_TOTAL_BUDGET_SEC = int(os.environ.get("PANTHEON_DISK_DU_TOTAL_BUDGET_SEC", "90"))

STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
STATE_PATH = STATE_DIR / "disk_usage_alert_state.json"


def _log(msg: str) -> None:
    print(f"[disk_usage_monitor ts={int(time.time())}] {msg}", flush=True)


def _gib(nbytes: float) -> str:
    return f"{nbytes / (1024 ** 3):.0f}G"


def read_usage(path: str = WATCH_PATH) -> tuple[int, int, int, float]:
    """Return (total, used, free, used_pct) for `path`. Pure stdlib."""
    u = shutil.disk_usage(path)
    used_pct = (u.used / u.total) * 100 if u.total else 0.0
    return u.total, u.used, u.free, used_pct


def classify(used_pct: float) -> str:
    """OK / WARN / URGENT from used-percent."""
    if used_pct >= URGENT_PCT:
        return "URGENT"
    if used_pct >= WARN_PCT:
        return "WARN"
    return "OK"


def read_meminfo() -> tuple[int, int]:
    """(total_bytes, available_bytes) from /proc/meminfo. MemAvailable is the
    kernel's own estimate of allocatable-without-swap memory — the right
    headroom signal (free alone undercounts reclaimable page cache)."""
    fields = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        parts = line.split()
        if parts and parts[0].rstrip(":") in ("MemTotal", "MemAvailable"):
            fields[parts[0].rstrip(":")] = int(parts[1]) * 1024  # kB → bytes
    return fields["MemTotal"], fields["MemAvailable"]


def classify_ram(available_bytes: int) -> str:
    """OK / WARN / URGENT from absolute MemAvailable floors."""
    if available_bytes < RAM_URGENT_MB * 1024 ** 2:
        return "URGENT"
    if available_bytes < RAM_WARN_MB * 1024 ** 2:
        return "WARN"
    return "OK"


def read_load15_and_cores() -> tuple[float, int]:
    """(load15, nproc) from /proc/loadavg + os.cpu_count()."""
    load15 = float(Path("/proc/loadavg").read_text().split()[2])
    return load15, (os.cpu_count() or 1)


def classify_cpu(load15: float, cores: int) -> str:
    """OK / WARN — no URGENT tier: a hot CPU degrades, it doesn't wedge
    (unlike a full disk or exhausted RAM), so one severity suffices."""
    return "WARN" if load15 > CPU_LOAD15_WARN_MULT * cores else "OK"


def _top_processes(sort_key: str, limit: int = 3) -> str:
    """Best-effort top processes by %mem or %cpu. NEVER raises; '' on failure."""
    try:
        res = subprocess.run(
            ["ps", "-eo", "user:12,pid,pcpu,pmem,comm", f"--sort=-{sort_key}"],
            capture_output=True, text=True, timeout=15,
        )
        if res.returncode != 0:
            return ""
        lines = res.stdout.strip().splitlines()[1 : limit + 1]
        return "; ".join(" ".join(ln.split()) for ln in lines)
    except (OSError, subprocess.SubprocessError):
        return ""


def _top_consumers(limit: int = 4) -> str:
    """Per-home sizes, largest first, followed by any home that could NOT be sized —
    each MARKED (?timeout / ?err / ?skipped), never silently dropped. A home the
    sweep cannot measure is the actionable signal, not noise: on 2026-09-06 the
    largest home (claude) was dropped on a nonzero du exit, leaving the alert naming
    a 6x-smaller home (T-3441). NEVER raises (the core df check must not depend on
    cross-account du),
    and the whole sweep is bounded by DU_TOTAL_BUDGET_SEC so it always fits inside
    the service TimeoutStartSec."""
    import getpass
    me = getpass.getuser()
    measured: list[tuple[str, str]] = []
    unmeasured: list[tuple[str, str]] = []
    deadline = time.monotonic() + DU_TOTAL_BUDGET_SEC
    for home in AGENT_HOMES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            unmeasured.append((home, "?skipped"))
            continue
        path = f"/home/{home}"
        cmd = ["du", "-sh", path] if home == me else ["sudo", "-n", "-u", home, "du", "-sh", path]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=min(DU_PER_HOME_TIMEOUT_SEC, remaining))
        except subprocess.TimeoutExpired:
            unmeasured.append((home, "?timeout"))
            continue
        except (OSError, subprocess.SubprocessError):
            unmeasured.append((home, "?err"))
            continue
        if res.stdout.strip():
            # du prints the grand total even when it exits NONZERO from an unreadable
            # subdir (e.g. the root-owned overlay dirs rootless podman leaves under
            # .local/share/containers in the claude/claude-research/codex homes). A
            # valid total is the actionable number — do NOT discard it for rc!=0.
            # THIS was the 2026-09-06 miss: du printed a valid '/home/claude' total
            # with rc=1 and the old `returncode == 0` guard dropped the biggest home
            # every run, so the alert named a smaller home (T-3441).
            measured.append((home, res.stdout.split()[0]))
        else:
            unmeasured.append((home, "?err"))
    if not measured and not unmeasured:
        return ""

    def _to_bytes(s: str) -> float:
        units = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
        try:
            return float(s[:-1]) * units.get(s[-1].upper(), 1)
        except (ValueError, IndexError):
            return 0.0

    measured.sort(key=lambda kv: _to_bytes(kv[1]), reverse=True)
    parts = [f"{h}:{sz}" for h, sz in measured[:limit]]
    # Unmeasured homes are ALWAYS shown (never subject to the top-N truncation): the
    # one we could not size is the most likely culprit, so its marker must survive.
    parts += [f"{h}:{marker}" for h, marker in unmeasured]
    return "  ".join(parts)


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state))
    except OSError as e:
        print(f"[disk_usage_monitor] could not persist state: {e}", file=sys.stderr)


def _should_alert(cls: str, now: float, state: dict) -> bool:
    """Throttle per class: re-alert only after the cooldown elapses."""
    return (now - state.get(cls, 0)) >= ALERT_COOLDOWN_SECONDS


def _alert(text: str) -> None:
    """Direct-to-operator Bot-API alert (reuses inbox_consumer's bot-token send, so it
    delivers independent of Claude OAuth — same path the credential monitor uses)."""
    from tools.inbox_consumer import _send_telegram_alert
    _send_telegram_alert(text)


def _wake_recovery(kind: str, body: str) -> None:
    """Self-wake alongside the the operator alert (2026-07-04 incident, the operator TG 3678):
    drop the breach into recovery's ORG-026 mailbox so Recovery triages directly instead of
    the operator having to relay the WARN back ("pantheon-internal alerts must not relay
    through the operator"). Fail-soft: a mailbox error must never break the alert path."""
    try:
        from tools import mailbox
        mailbox.send(
            "/home/the-ancestor/mailbox/recovery",
            body
            + "\nAction (recovery): triage per the 2026-07-04 pattern — locate the growth "
            "(du -sh /tmp/* + per-home breakdown), clean only regenerable caches after "
            "verifying no owner process is using them, FYI the owning agent.",
            "recovery",
            subject=f"{kind}: self-wake for triage",
            # T-2237(a): only sent at WARN/URGENT breach, and a disk that fills
            # while the notice waits takes the fleet with it.
            severity="urgent",
        )
    except Exception as exc:
        _log(f"recovery self-wake failed (non-fatal): {exc!r}")


def check(*, dry_run: bool, now: float | None = None) -> int:
    """Read usage; alert (throttled) on WARN/URGENT. Returns 1 if breached else 0."""
    now = time.time() if now is None else now
    total, used, free, pct = read_usage()
    cls = classify(pct)
    _log(f"{WATCH_PATH} {cls}: {pct:.0f}% used ({_gib(used)}/{_gib(total)}, {_gib(free)} free)")
    if cls == "OK":
        return 0
    if dry_run:
        _log(f"[dry-run] would alert ({cls})")
        return 1
    state = _load_state()
    if _should_alert(cls, now, state):
        consumers = _top_consumers()
        body = (
            f"💾 [disk-{cls.lower()}] {WATCH_PATH} at {pct:.0f}% used "
            f"({_gib(used)}/{_gib(total)}, {_gib(free)} free)."
        )
        if consumers:
            body += f"\nTop homes: {consumers}"
        body += (
            f"\nReclaim: build/venv caches are usually the bulk (Rust target/, "
            f".venv, .npm, .cache) — safe to clean, regenerate from source. "
            f"Coordinate before cleaning another agent's live project."
        )
        _alert(body)
        _wake_recovery(f"disk-{cls.lower()}", body)
        state[cls] = now
        _save_state(state)
        _log(f"alerted the operator + woke recovery ({cls})")
    else:
        _log(f"alert suppressed (cooldown, last < {ALERT_COOLDOWN_SECONDS}s ago)")
    return 1


def check_ram(*, dry_run: bool, now: float | None = None) -> int:
    """RAM leg (T-1632): alert on low MemAvailable. Returns 1 if breached."""
    now = time.time() if now is None else now
    total, avail = read_meminfo()
    cls = classify_ram(avail)
    _log(f"ram {cls}: {_gib(avail)} available of {_gib(total)} "
         f"(floors: warn <{RAM_WARN_MB}M urgent <{RAM_URGENT_MB}M)")
    if cls == "OK":
        return 0
    if dry_run:
        _log(f"[dry-run] would alert (ram {cls})")
        return 1
    state = _load_state()
    key = f"ram_{cls}"
    if _should_alert(key, now, state):
        hogs = _top_processes("pmem")
        body = (
            f"🧠 [ram-{cls.lower()}] MemAvailable down to {_gib(avail)} "
            f"(of {_gib(total)} total)."
        )
        if hogs:
            body += f"\nTop by memory: {hogs}"
        _alert(body)
        _wake_recovery(
            f"ram-{cls.lower()}",
            body
            + "\nAction (recovery): identify the hog (likely a build — the 2026-07-02 "
            "reaper incident pattern); coordinate with the owning agent before "
            "killing anything; a stuck build can be re-run, a live session cannot.",
        )
        state[key] = now
        _save_state(state)
        _log(f"alerted the operator + woke recovery (ram {cls})")
    else:
        _log(f"ram alert suppressed (cooldown)")
    return 1


def check_cpu(*, dry_run: bool, now: float | None = None) -> int:
    """CPU leg (T-1632): WARN on sustained load15 > mult × cores. Returns 1 if breached."""
    now = time.time() if now is None else now
    load15, cores = read_load15_and_cores()
    cls = classify_cpu(load15, cores)
    _log(f"cpu {cls}: load15={load15:.2f} on {cores} cores "
         f"(warn >{CPU_LOAD15_WARN_MULT:.1f}x)")
    if cls == "OK":
        return 0
    if dry_run:
        _log("[dry-run] would alert (cpu WARN)")
        return 1
    state = _load_state()
    key = "cpu_WARN"
    if _should_alert(key, now, state):
        hogs = _top_processes("pcpu")
        body = (
            f"🔥 [cpu-warn] load15={load15:.2f} sustained on {cores} cores "
            f"(threshold {CPU_LOAD15_WARN_MULT:.1f}×cores)."
        )
        if hogs:
            body += f"\nTop by CPU: {hogs}"
        _alert(body)
        _wake_recovery(
            "cpu-warn",
            body
            + "\nAction (recovery): identify whether it's a legitimate long build or a "
            "runaway loop (15-min sustained load is past normal build spikes); "
            "check the owning agent's pane state before intervening.",
        )
        state[key] = now
        _save_state(state)
        _log("alerted the operator + woke recovery (cpu WARN)")
    else:
        _log("cpu alert suppressed (cooldown)")
    return 1


def run_all(*, dry_run: bool, now: float | None = None) -> None:
    """All three legs, each isolated: one leg's failure must never silence the
    others (fail-open per leg, matching the monitor-must-not-fail-its-timer rule)."""
    for leg in (check, check_ram, check_cpu):
        try:
            leg(dry_run=dry_run, now=now)
        except Exception as exc:
            _log(f"{getattr(leg, '__name__', 'leg')} failed (non-fatal): {exc!r}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--check", action="store_true", help="check disk+ram+cpu + alert on breach (cron path)")
    p.add_argument("--dry-run", action="store_true", help="classify + print, send no alert")
    args = p.parse_args(argv)

    if not (args.check or args.dry_run):
        print(
            "ERROR: refusing to run with no mode flag. Pass --check (cron path) "
            "or --dry-run to preview without alerting.",
            file=sys.stderr,
        )
        return 64

    run_all(dry_run=args.dry_run)
    # Exit 0 always: a monitor must not fail its own timer. Breach state is in the log.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
