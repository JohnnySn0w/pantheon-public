#!/usr/bin/env python3
"""Coverage monitor for the fleet's memory-snapshot guard (T-2838 / T-1871 rot).

THE GAP THIS CLOSES: T-2424 gave each memory-bearing seat a daily snapshot timer
that git-commits its memory dir (+ HANDOFF) into a local history repo, so a bad
sed or a disk event is recoverable instead of a silent single-copy loss. But a
one-time INSTALL does not prevent a timer that later silently STOPS. The T-1871
audit found exactly that: mnemosyne's snapshot had quietly rotted since ~2026-07-29
(~12 days) and nobody noticed until someone went looking. A stopped snapshot timer
reads as healthy to every process check (the account is up, the agent is up) — the
only witness is that the snapshots stopped landing. This monitor makes that a
surfaced alert, not a latent exposure.

WHY THE TIMER, NOT THE LAST COMMIT (the design pivot, evidenced live 2026-08-11):
the obvious check — "alert if the newest snapshot COMMIT is older than N days" —
FALSE-ALARMS on idle seats. The snapshot scripts commit only when the memory dir
actually changed (`git diff --cached --quiet` guards the commit), so a healthy
timer on a quiet seat produces NO commit and the repo HEAD legitimately ages. Live
proof: mnemosyne's in-place memory git HEAD was 13 days old while its hourly
snapshot timer was firing perfectly (last fire 44min prior) — the seat simply had
not changed its memory since 07-29. Paging on that is noise, and noise trains
alert-blindness. So the AUTHORITATIVE signal is snapshot-TIMER health (is the
mechanism scheduled and running), which has zero idle-seat false-positive and
directly detects the "timer stopped" failure this monitor exists to catch. Commit
HEAD age is kept as corroborating context and as the FALLBACK signal for the one
case timer health cannot cover — a seat whose systemd cannot be introspected.

(LastTriggerUSec, the tempting "when did it last fire" field, is NOT used: it
resets to empty when the account's user-manager restarts, so it read empty for a
long-healthy specialist timer on 2026-08-11. ActiveState / UnitFileState /
NextElapseUSecRealtime are stable across manager restarts and are what we key on.)

Per covered seat the monitor classifies (first match wins):
  STALLED           — timer not active/enabled, or next-fire absent/in the past
                      beyond a grace window (the T-1871 failure).
  MISFIRE_WINDOW    — timer active but next-fire is absurdly far out (> max-days):
                      a mis-scheduled timer that will not fire on its daily cadence.
  UNVERIFIABLE_STALE— cannot introspect the seat's systemd AND its last snapshot
                      commit is older than the fallback window (fail-open: surface
                      softly rather than pass silently on an unreadable seat).
  UNVERIFIABLE_OK   — cannot introspect systemd but a snapshot committed recently
                      (the mechanism is demonstrably alive; log only, no alert).
  HEALTHY           — timer active + enabled + next-fire within the cadence bound.

Cross-account reads are privileged (700 homes) and go through Recovery's sudo grants,
the same posture as context_fill_monitor's cross-account pane reads and
credential_expiry_monitor's credential reads. Recovery reads its own seat directly.

ROUTING: a stalled snapshot timer is ALWAYS Recovery-fixable (systemctl --user restart
of the timer, via sudo for a sibling) — unlike an OAuth death, no operator-only
action is required. So every alert routes through the Recovery-triage channel
(_send_alert_via_recovery), NOT the recovery-self→the operator carve-out of route_failure_alert
(whose "Recovery cannot self-remediate" text would be false here). Throttled per
(seat, class) so the timer nags without spamming. Fail-open throughout: a seat the
monitor cannot read WARNs, never blocks; the process always exits 0 (a monitor must
not fail its own timer). Default-safe (T-0261): refuses a flagless run.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Covered seats: the memory-bearing accounts with a snapshot mechanism. Each maps
# to (unix user, snapshot timer unit, corroboration repo). Two mechanisms exist:
#   - pantheon-agent-state-snapshot.timer → commits COPIES into a dedicated
#     ~/.local/state/pantheon/agent-state-history repo (recovery/orchestrator/specialist/atlas/metis).
#   - mnemosyne-state-snapshot.timer → commits mnemosyne's memory dir IN PLACE
#     (exocortex; hourly). Its corroboration repo is that live memory .git.
# the-ancestor is intentionally EXCLUDED: it carries no persistent memory-snapshot
# mechanism (no agent-state-history repo, no snapshot timer) — verified 2026-08-11.
# codex is excluded (separate substrate, no Claude-side memory snapshot).
COVERED_SEATS: dict[str, dict] = {
    "recovery": {
        "user": "claude-fallback",
        "timer": "pantheon-agent-state-snapshot.timer",
        "repo": "/home/claude-fallback/.local/state/pantheon/agent-state-history",
    },
    "orchestrator": {
        "user": "claude",
        "timer": "pantheon-agent-state-snapshot.timer",
        "repo": "/home/claude/.local/state/pantheon/agent-state-history",
    },
    "specialist": {
        "user": "claude-research",
        "timer": "pantheon-agent-state-snapshot.timer",
        "repo": "/home/claude-research/.local/state/pantheon/agent-state-history",
    },
    "atlas": {
        "user": "suborch2",
        "timer": "pantheon-agent-state-snapshot.timer",
        "repo": "/home/suborch2/.local/state/pantheon/agent-state-history",
    },
    "metis": {
        "user": "suborch1",
        "timer": "pantheon-agent-state-snapshot.timer",
        "repo": "/home/suborch1/.local/state/pantheon/agent-state-history",
    },
    "mnemosyne": {
        "user": "exocortex",
        "timer": "mnemosyne-state-snapshot.timer",
        # In-place-git design (ORG-042 observation seat): the live memory dir is
        # itself the git repo the snapshot service commits. Check its HEAD.
        "repo": "/home/exocortex/.claude/projects/-home-exocortex/memory",
    },
}

# Tunables (env-overridable).
# A healthy daily timer's next fire is always < 1 day out; hourly < 1h. 2 days is a
# generous bound that both cadences clear while a mis-scheduled timer trips.
NEXT_FIRE_MAX_DAYS = float(os.environ.get("PANTHEON_SNAPSHOT_NEXT_FIRE_MAX_DAYS", "2.0"))
# Allow a small window where next-fire is just-past (timer about to run / just
# missed and rescheduling) before calling it stalled.
NEXT_FIRE_PAST_GRACE_HOURS = float(os.environ.get("PANTHEON_SNAPSHOT_NEXT_FIRE_PAST_GRACE_HOURS", "2.0"))
# Fallback-only: when systemd is unreadable, a snapshot commit older than this is
# the soft-WARN threshold. Generous (idle seats legitimately age), so it fires only
# when BOTH signals are unavailable/old.
FALLBACK_STALE_DAYS = float(os.environ.get("PANTHEON_SNAPSHOT_FALLBACK_STALE_DAYS", "7.0"))
# Re-alert cadence per (seat, class).
ALERT_COOLDOWN_SECONDS = int(os.environ.get("PANTHEON_SNAPSHOT_ALERT_COOLDOWN_SECONDS", str(12 * 3600)))

STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
STATE_PATH = STATE_DIR / "memory_snapshot_coverage_alert_state.json"

ALERT_CLASSES = {"STALLED", "MISFIRE_WINDOW", "UNVERIFIABLE_STALE"}

_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")


def _log(seat: str, msg: str) -> None:
    print(f"[memory_snapshot_coverage_monitor seat={seat} ts={int(time.time())}] {msg}", flush=True)


def _run(argv: list[str], timeout: int = 15) -> tuple[int, str, str]:
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return res.returncode, res.stdout, res.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 127, "", f"{e!r}"


def _systemctl_show(user: str, timer: str) -> tuple[int, str, str]:
    """Run `systemctl --user show <timer>` for a seat. Self directly; siblings via
    sudo with XDG_RUNTIME_DIR so the user manager's bus is reachable."""
    props = "-p LoadState -p ActiveState -p UnitFileState -p NextElapseUSecRealtime"
    if user == getpass.getuser():
        return _run(["systemctl", "--user", "show", timer, *props.split()])
    inner = f"XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user show {timer} {props}"
    return _run(["sudo", "-n", "-u", user, "bash", "-c", inner])


def _git_head_epoch(user: str, repo: str) -> int | None:
    """Committer epoch of the repo's HEAD, or None if unreadable/no commits. Self
    directly; siblings via sudo (700 homes)."""
    if user == getpass.getuser():
        argv = ["git", "-C", repo, "log", "-1", "--format=%ct"]
    else:
        argv = ["sudo", "-n", "-u", user, "git", "-C", repo, "log", "-1", "--format=%ct"]
    rc, out, _ = _run(argv)
    if rc != 0:
        return None
    out = out.strip()
    return int(out) if out.isdigit() else None


def _parse_next_elapse(show_out: str) -> tuple[dict[str, str], int | None]:
    """Parse `systemctl show` KEY=VALUE lines into a props dict + the parsed
    NextElapseUSecRealtime as a UTC epoch (or None if empty/absent/unparseable).

    systemd prints the field as a human date ("Wed 2026-08-12 00:13:53 UTC"); we
    regex-extract the datetime and read it as UTC (the substrate runs UTC). An
    empty value ("NextElapseUSecRealtime=") means the timer has no scheduled fire.
    """
    props: dict[str, str] = {}
    for line in show_out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            props[k.strip()] = v.strip()
    raw = props.get("NextElapseUSecRealtime", "")
    next_epoch: int | None = None
    m = _TS_RE.search(raw)
    if m:
        import calendar
        try:
            tm = time.strptime(m.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S")
            next_epoch = int(calendar.timegm(tm))  # interpret as UTC
        except ValueError:
            next_epoch = None
    return props, next_epoch


def classify(seat: str, cfg: dict, now: float | None = None) -> tuple[str, str]:
    """Return (class, human_detail) for a seat's snapshot coverage.

    Authoritative signal: snapshot-timer health. Fallback (systemd unreadable):
    last snapshot commit age. See module docstring for why timer-health beats
    commit-age (idle-seat false-positive)."""
    now = time.time() if now is None else now
    user, timer, repo = cfg["user"], cfg["timer"], cfg["repo"]
    head_epoch = _git_head_epoch(user, repo)
    head_age = _fmt_head_age(head_epoch, now)

    rc, out, err = _systemctl_show(user, timer)
    props, next_epoch = _parse_next_elapse(out)
    load = props.get("LoadState", "")
    active = props.get("ActiveState", "")
    enabled = props.get("UnitFileState", "")

    # systemd unreadable for this seat → fall back to commit age (fail-open).
    if rc != 0 or load in ("", "not-found") or active == "":
        why = f"systemd unreadable (rc={rc}, load={load or '?'}: {(err or '').strip()[:50]})"
        if head_epoch is None:
            return "UNVERIFIABLE_STALE", f"{why}; and no readable snapshot commit either — coverage cannot be confirmed"
        age_days = (now - head_epoch) / 86400.0
        if age_days > FALLBACK_STALE_DAYS:
            return "UNVERIFIABLE_STALE", f"{why}; last snapshot commit {head_age} ago (> {FALLBACK_STALE_DAYS:g}d fallback window)"
        return "UNVERIFIABLE_OK", f"{why}; but last snapshot commit {head_age} ago — mechanism recently alive"

    # Timer readable → key on scheduling health (idle-seat-proof).
    if active != "active" or enabled not in ("enabled", "enabled-runtime"):
        return "STALLED", f"timer {timer} not running (ActiveState={active or '?'}, UnitFileState={enabled or '?'}); last snapshot {head_age} ago"
    if next_epoch is None:
        return "STALLED", f"timer {timer} active but has NO scheduled next fire — not counting down; last snapshot {head_age} ago"
    secs_to_fire = next_epoch - now
    if secs_to_fire < -(NEXT_FIRE_PAST_GRACE_HOURS * 3600):
        return "STALLED", f"timer {timer} next-fire is {_fmt_dur(-secs_to_fire)} in the PAST — missed and not rescheduled; last snapshot {head_age} ago"
    if secs_to_fire > NEXT_FIRE_MAX_DAYS * 86400:
        return "MISFIRE_WINDOW", f"timer {timer} next-fire is {_fmt_dur(secs_to_fire)} out (> {NEXT_FIRE_MAX_DAYS:g}d) — mis-scheduled for a daily cadence; last snapshot {head_age} ago"
    return "HEALTHY", f"timer {timer} active+enabled, next fire in {_fmt_dur(secs_to_fire)}; last snapshot {head_age} ago"


def _fmt_head_age(head_epoch: int | None, now: float) -> str:
    if head_epoch is None:
        return "unknown (no readable commit)"
    return _fmt_dur(now - head_epoch)


def _fmt_dur(secs: float) -> str:
    secs = int(abs(secs))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d{h:02d}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def _load_state() -> dict:
    import json
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    import json
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state))
    except OSError as e:
        print(f"[memory_snapshot_coverage_monitor] could not persist state: {e}", file=sys.stderr)


def _should_alert(seat: str, cls: str, now: float, state: dict) -> bool:
    """Throttle per (seat, class): re-alert only after the cooldown elapses."""
    last = state.get(f"{seat}:{cls}", 0)
    return (now - last) >= ALERT_COOLDOWN_SECONDS


def _alert(seat: str, cls: str, detail: str) -> None:
    """Route to the Recovery-triage channel: a stalled snapshot timer is Recovery-fixable
    (systemctl --user restart the timer, via sudo for a sibling), so Recovery gets the
    actionable surface + a throttled the operator audit-ping. Lazy import mirrors
    credential_expiry_monitor's use of inbox_consumer helpers."""
    from tools.inbox_consumer import _send_alert_via_recovery
    remedy = (
        f"restart {seat}'s snapshot timer: "
        f"sudo -n -u {COVERED_SEATS[seat]['user']} bash -c "
        f"'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user restart "
        f"{COVERED_SEATS[seat]['timer']}' then re-run this monitor --dry-run to confirm"
    )
    _send_alert_via_recovery(
        f"🗂️ [memory-snapshot-{cls.lower()}] {seat}: {detail}\n"
        f"A stalled snapshot leaves {seat}'s memory single-copy (the T-1871 rot). "
        f"Remedy: {remedy}.",
        vocab="memory-snapshot-coverage",
    )


def check(seats: list[str], *, dry_run: bool, now: float | None = None) -> int:
    """Check each seat; alert (throttled) on any alert-class. Returns problem count."""
    now = time.time() if now is None else now
    state = _load_state()
    problems = 0
    for seat in seats:
        cfg = COVERED_SEATS.get(seat)
        if not cfg:
            _log(seat, "not a covered seat; skipping")
            continue
        cls, detail = classify(seat, cfg, now=now)
        _log(seat, f"{cls}: {detail}")
        if cls not in ALERT_CLASSES:
            continue
        problems += 1
        if dry_run:
            _log(seat, f"[dry-run] would alert ({cls})")
            continue
        if _should_alert(seat, cls, now, state):
            _alert(seat, cls, detail)
            state[f"{seat}:{cls}"] = now
            _log(seat, f"alerted via Recovery ({cls})")
        else:
            _log(seat, f"alert suppressed (cooldown, last < {ALERT_COOLDOWN_SECONDS}s ago)")
    if not dry_run:
        _save_state(state)
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="check every covered seat (timer path)")
    g.add_argument("--seat", choices=sorted(COVERED_SEATS), help="check one seat")
    p.add_argument("--dry-run", action="store_true", help="classify + print, send no alert")
    args = p.parse_args(argv)

    if not (args.all or args.seat):
        print(
            "ERROR: refusing to run with no mode flag. Pass --all (timer path) or "
            "--seat <name>; add --dry-run to preview without alerting.",
            file=sys.stderr,
        )
        return 64

    seats = sorted(COVERED_SEATS) if args.all else [args.seat]
    problems = check(seats, dry_run=args.dry_run)
    # Exit 0 always: a monitor must not fail its own timer. Problem count is logged.
    print(f"[memory_snapshot_coverage_monitor] checked {len(seats)} seat(s); {problems} with coverage issues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
