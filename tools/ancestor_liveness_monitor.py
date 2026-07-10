"""tools/ancestor_liveness_monitor.py — light alive-but-wedged recovery for the-ancestor (T-1134).

the-ancestor (pantheon-ancestor-daemon, live since T-1124) has `Restart=always` for process
EXIT, but no alive-but-WEDGED recovery — the T-0510-class gap (process up, session stuck mid
turn / dropped its turn / boot-dialog wedged). This is the LIGHT path per T-0765 deliverable 6:
Orchestrator scoped it thinner than full context-fill-monitor + restart-drain enrollment because
the-ancestor is thin/idle — a periodic liveness probe + Recovery-driven restart suffices.

POSTURE — DETECT (live) + REMEDIATE (ship-dark), matching the fleet's channel-resilience stance
(T-0894 detect live / T-0997 remediate-dark, consciously affirmed in T-1140): the probe + the
Recovery alert run LIVE (Recovery, the-ancestor's recovery owner, is notified the moment it wedges and
can restart it by hand — literally Recovery's role); the AUTO-restart is gated behind
PANTHEON_ANCESTOR_REMEDIATE_ENABLED (default OFF) pending a supervised first-fire (bundled with
T-0899 / T-1140-arming — every restart-automation arm gets a supervised fire).

SIGNAL (read-only capture-pane; no side effects): the-ancestor is ALIVE if its pane's live
bottom region shows the idle prompt ("? for shortcuts" / "ctrl+t to hide tasks") OR a mid-turn
marker ("esc to interrupt") — idle OR busy are both healthy. It is treated as NOT-alive if the
pane shows neither, the tmux session is absent, or the pane can't be read. FAIL-SAFE against
transients: a restart/alert only fires after N CONSECUTIVE not-alive probes (a single blip — a
mid-restart window, a momentary sudo/tmux hiccup — never trips it), and a cooldown bounds
re-fires. Channel-less: alerts route via route_failure_alert("the-ancestor", ...) → Recovery, never
a bridge. Restart is kill-session (the-ancestor's documented mechanism: kill the
`ancestor-daemon` session → the supervise wrapper exits → systemd `Restart=always` respawns) —
NOT the fire_restart/drain path, which is for the live agents that carry the managed restart
stack the-ancestor deliberately does not.

Coordinates with T-1126 (the broader alive-but-erroring monitoring-gap work) — shared theme;
this is the the-ancestor-specific slice.
"""
from __future__ import annotations

import argparse
import json
import os
import pwd
import subprocess
import sys
import time
from pathlib import Path

ANCESTOR_AGENT = "the-ancestor"
ANCESTOR_SESSION = "ancestor-daemon"        # tmux session the daemon runs in
ANCESTOR_PANE = f"{ANCESTOR_SESSION}:0"
# A pane whose live bottom shows ANY of these is a healthy claude TUI (idle or mid-turn).
_ALIVE_SENTINELS = ("? for shortcuts", "ctrl+t to hide tasks", "esc to interrupt")
DEFAULT_WEDGE_THRESHOLD = 3      # consecutive not-alive probes before acting (transient guard)
DEFAULT_COOLDOWN = 1800          # ≥30 min between remediation restarts (anti-loop)
_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"

# SHIP-DARK: the auto-restart is inert until this flag is armed (supervised first-fire).
REMEDIATE_ENABLED = os.environ.get("PANTHEON_ANCESTOR_REMEDIATE_ENABLED", "").strip().lower() in (
    "1", "true", "yes", "on",
)


def _log(msg: str) -> None:
    print(f"[ancestor_liveness] {msg}", file=sys.stderr)


def _state_path(state_dir: Path) -> Path:
    return state_dir / "ancestor_liveness.json"


def is_alive(pane: str | None) -> bool:
    """True if the pane reading shows a healthy claude TUI (idle OR busy). A None pane
    (session absent / unreadable) is NOT alive — but the consecutive-N threshold absorbs a
    transient read miss, so this never trips on a single blip."""
    if pane is None:
        return False
    # Anchor to the live bottom region: a sentinel echoed earlier in scrollback (e.g. inside a
    # reloaded transcript) must not false-positive "alive".
    non_blank = [ln for ln in pane.splitlines() if ln.strip()]
    bottom = "\n".join(non_blank[-6:])
    return any(s in bottom for s in _ALIVE_SENTINELS)


class Ops:
    """Side-effecting operations, isolated so the orchestration is pure + unit-testable.
    Defaults wrap sudo/tmux/the filesystem/the alert path; tests inject a fake."""

    def capture_pane(self) -> str | None:
        """Read the-ancestor's pane as the-ancestor (Recovery → sudo). Returns the pane text, or
        None if the session is absent or the read fails (both → not-alive, transient-guarded)."""
        try:
            uid = pwd.getpwnam(ANCESTOR_AGENT).pw_uid
        except KeyError:
            return None
        argv = ["sudo", "-n", "-u", ANCESTOR_AGENT, "env", f"XDG_RUNTIME_DIR=/run/user/{uid}",
                "tmux", "capture-pane", "-t", ANCESTOR_PANE, "-p"]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=15)
        except (subprocess.TimeoutExpired, OSError):
            return None
        return r.stdout if r.returncode == 0 else None

    def restart(self) -> bool:
        """Kill the ancestor-daemon tmux session → the supervise wrapper exits → systemd
        Restart=always respawns a fresh daemon. The-ancestor's documented restart mechanism."""
        try:
            uid = pwd.getpwnam(ANCESTOR_AGENT).pw_uid
        except KeyError:
            return False
        argv = ["sudo", "-n", "-u", ANCESTOR_AGENT, "env", f"XDG_RUNTIME_DIR=/run/user/{uid}",
                "tmux", "kill-session", "-t", ANCESTOR_SESSION]
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=15).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    def alert(self, msg: str) -> None:
        """Route a the-ancestor failure alert → Recovery (channel-less; never a bridge)."""
        try:
            try:
                from tools.inbox_consumer import route_failure_alert
            except ImportError:
                from inbox_consumer import route_failure_alert
            route_failure_alert(ANCESTOR_AGENT, msg, "the-ancestor")
        except Exception as exc:  # alerting is best-effort — never crash the monitor
            _log(f"alert routing failed ({exc!r}); intended msg: {msg}")

    def now(self) -> int:
        return int(time.time())


def _read_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _write_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        _log(f"state write failed ({exc!r})")


def check_once(*, ops: Ops | None = None, state_dir: Path | None = None,
               threshold: int = DEFAULT_WEDGE_THRESHOLD, cooldown: int = DEFAULT_COOLDOWN,
               dry_run: bool = False) -> str:
    """One probe cycle. Returns a status string:
      'alive'                    — healthy, counter reset.
      'transient' / 'wedge_pending' — not-alive but below the consecutive threshold.
      'wedge_alerted'            — threshold reached: Recovery alerted (always).
      'wedge_remediated'         — alerted AND a ship-dark restart fired (flag armed + cooldown).
      'wedge_cooldown'           — alerted but restart suppressed by cooldown.
      'wedge_disarmed'           — alerted but auto-restart is ship-dark (flag off).
    """
    ops = ops or Ops()
    state_dir = state_dir or _DEFAULT_STATE_DIR
    sp = _state_path(state_dir)
    state = _read_state(sp)
    now = ops.now()

    pane = ops.capture_pane()
    if is_alive(pane):
        if state.get("consecutive"):
            _log("the-ancestor alive again; resetting wedge counter")
        state["consecutive"] = 0
        _write_state(sp, state)
        return "alive"

    consecutive = int(state.get("consecutive", 0)) + 1
    state["consecutive"] = consecutive
    if consecutive < threshold:
        _write_state(sp, state)
        _log(f"the-ancestor not-alive ({consecutive}/{threshold}); within transient guard")
        return "wedge_pending"

    # Threshold reached — a confirmed wedge.
    detail = (f"the-ancestor daemon appears WEDGED — {consecutive} consecutive not-alive probes "
              f"(no idle/mid-turn TUI sentinel on session {ANCESTOR_SESSION}).")
    if dry_run:
        _write_state(sp, state)
        _log(f"[dry-run] would alert+remediate: {detail}")
        return "wedge_alerted"

    ops.alert(detail)  # ALWAYS alert Recovery (detect+alert is the LIVE half)

    if not REMEDIATE_ENABLED:
        _write_state(sp, state)
        _log("auto-restart DISABLED (ship-dark); alerted Recovery only")
        return "wedge_disarmed"

    last = int(state.get("last_remediate_ts", 0))
    if last and (now - last) < cooldown:
        _write_state(sp, state)
        _log(f"restart suppressed by cooldown ({now - last}s < {cooldown}s); alerted only")
        return "wedge_cooldown"

    ok = ops.restart()
    state["last_remediate_ts"] = now
    if ok:
        state["consecutive"] = 0  # restart fired; give it a clean slate to recover
    _write_state(sp, state)
    _log(f"ship-dark restart {'fired' if ok else 'FAILED'}; Recovery alerted")
    return "wedge_remediated" if ok else "wedge_alerted"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="the-ancestor alive-but-wedged liveness monitor (T-1134).")
    ap.add_argument("--dry-run", action="store_true", help="probe + report, no alert/restart")
    ap.add_argument("--threshold", type=int, default=DEFAULT_WEDGE_THRESHOLD)
    ap.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN)
    args = ap.parse_args(argv)
    status = check_once(threshold=args.threshold, cooldown=args.cooldown, dry_run=args.dry_run)
    print(status)
    return 0  # fail-open: the monitor never blocks/loops on its own non-zero


if __name__ == "__main__":
    raise SystemExit(main())
