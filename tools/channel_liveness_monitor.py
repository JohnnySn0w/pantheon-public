#!/usr/bin/env python3
"""Channel-liveness monitor — detect a comms-dark-but-session-healthy agent (T-0894).

The T-0893 detection gap: every existing probe (context_fill_monitor wedge/
staleness, verify_startup pane capture) checks SESSION/pane liveness, which reads
GREEN while an agent's Telegram INBOUND poller is dead — the agent cannot receive
DMs/inbound but looks healthy. This let Orchestrator sit comms-dark ~2.5h on 2026-06-07.

This probe is DISTINCT from session-liveness: it checks the inbound poller itself.

Detection (cheap + reliable, from the 2026-06-06/07 incident forensics):
  - the plugin bridge writes `~/.claude/channels/telegram/bot.pid` with ITS pid;
  - the 409-death signature is bot.pid PRESENT but its PID DEAD (the poller exits
    on the Telegram single-poller 409, leaving a stale pidfile);
  - so: bot.pid -> live `bun ... server.ts` process == bridge UP; bot.pid present
    but PID dead/not-a-bun == bridge DOWN.
  - NB outbound `getMe`/sendMessage is NOT used as the primary signal — it
    false-PASSES (Orchestrator's outbound worked while inbound was dead). bot.pid +
    process liveness is the inbound-specific signal.

Scope: agents whose AGENT_CONFIG account has a bot.pid file (a configured inbound
bridge). An agent with NO bot.pid is treated as "no bridge configured" → skipped
(not DOWN). Runs Recovery-side; cross-account reads via Recovery's sudo reach.

This is the DETECT+ALERT half of T-0894 (alert-only — low-risk, cannot cycle a
session). Auto-remediation (tie a bridge-down to the graceful-restart path) and
the `mcp list` prevention guard are separate gated prongs (children of T-0894).

Alert routing: route_failure_alert (others→Recovery, recovery-self→the operator, T-0722).
Per-agent cooldown so a persistent down-state doesn't re-alert every poll.

Bail-and-poll design (mirrors context_fill_monitor): each cycle exits fast.

CLI:
    python3 tools/channel_liveness_monitor.py --agent orchestrator   # probe one agent
    python3 tools/channel_liveness_monitor.py --all           # probe all configured
    python3 tools/channel_liveness_monitor.py --agent recovery --dry-run  # probe, no alert

Exit codes: 0 = clean (bridge up, skipped, or alerted-down); 2 = argv error.

KNOWN v1 LIMITATIONS (hardening for the supervised first-fire / a follow-up):
  - stale-PID recycling: CLOSED by T-1606 (S3STAR-2026-07-07-002). The probe now
    requires the PID's /proc cwd to sit under the Claude Telegram plugin cache
    (BRIDGE_CWD_RE; the bridge child's argv is just `bun server.ts` — the
    identifying signal is its cwd, e.g.
    ~/.claude/plugins/cache/claude-plugins-official/telegram/<ver>). A recycled
    PID belonging to an unrelated `bun server.ts` (echo-recorder et al.) now
    reads DOWN, not up.
  - restart-window false-positive: CLOSED by T-1622. A down-read within
    RESTART_GRACE_SECONDS of a context-fill-initiated graceful restart
    (last_fire/last_restart state stamp) is suppressed — the restart IS the
    expected poller gap. Restarts initiated outside that machinery still have
    a one-poll early-alert exposure (documented in _in_restart_window).
"""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
import re
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Reuse context_fill_monitor's per-agent config + sudo helper (one source of
# truth for sudo_user) and inbox_consumer's failure-alert router.
from tools.context_fill_monitor import (  # noqa: E402
    AGENT_CONFIG,
    _sudo_prefix,
    fire_restart,
    write_tombstone,
)

_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"

# Re-alert at most once per this window per agent (a down bridge persists until
# remediation; don't spam every poll). 30 min mirrors the restart cooldown.
ALERT_COOLDOWN_SECONDS = 30 * 60

# Probe subprocess timeout.
PROBE_TIMEOUT_SECONDS = 15

# Post-restart grace (T-1622): suppress down-alerts this long after a graceful
# restart fired (context-fill last_fire/last_restart stamp). 600s covers the
# observed cycle (~150s fire→kick-verified) + fresh-session bridge registration
# with margin, while still surfacing a restart that failed to bring the bridge
# back within two poll intervals.
RESTART_GRACE_SECONDS = int(os.environ.get("CHANNEL_LIVENESS_RESTART_GRACE_SECONDS", "600"))

VOCAB = "channel-liveness"

# --- Auto-remediation (T-0997, prong B) — SHIP DARK -----------------------
# On a confirmed bridge-down, fire the GRACEFUL-RESTART path (handoff_and_restart
# via the same fire_restart/write_tombstone machinery context_fill_monitor uses
# for context-fill/wedge/api-health) — NOT a raw tmux kill-session (the operator
# 2026-06-09). The respawn re-inits the plugin bridge, preserving session context
# via the handoff. Default OFF; enable per-agent at a supervised first-fire.
REMEDIATE_ENABLED = os.environ.get("PANTHEON_CHANNEL_REMEDIATE_ENABLED", "").strip().lower() in (
    "1", "true", "yes", "on",
)
# Restart reason recorded on the tombstone / in logs.
REMEDIATE_REASON = "bridge_down"
# Cooldown between bridge-down remediations per agent — spaces restarts so a
# flapping bridge cannot restart-loop. 30 min mirrors the context-fill cooldown.
REMEDIATE_COOLDOWN_SECONDS = 30 * 60
# At this many consecutive remediations without recovery, the restart is not
# clearing the bridge — escalate (a restart-loop is not the fix).
REMEDIATE_ESCALATE_AT = 2


# Bridge identity (T-1606 / S3STAR-2026-07-07-002): the telegram plugin bridge
# runs with cwd inside the Claude plugin cache. Its argv is only
# `.../bun server.ts`, so cwd is the discriminating signal vs. an unrelated
# `bun server.ts` that inherited a recycled PID. Env-overridable in case the
# plugin cache layout drifts (a wrong pattern fails toward DOWN → alert →
# a human looks; never a silent false-pass).
BRIDGE_CWD_RE = re.compile(
    os.environ.get(
        "CHANNEL_LIVENESS_BRIDGE_CWD_PATTERN",
        r"/\.claude/plugins/cache/[^ ]*telegram",
    )
)


def _log(agent: str, msg: str) -> None:
    print(f"[channel_liveness_monitor agent={agent} ts={int(time.time())}] {msg}", flush=True)


def _bot_pid_path_for(cfg: dict) -> str:
    """The plugin bridge's pidfile for an agent, under its account home."""
    return f"/home/{cfg['sudo_user']}/.claude/channels/telegram/bot.pid"


def _alert_state_path_for(agent: str) -> Path:
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"channel_liveness_{agent}_last_alert.json"


def _run(cmd: list[str]) -> tuple[int, str]:
    """Run a probe subprocess; (rc, stdout). rc=-1 on subprocess error (fail-open)."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=PROBE_TIMEOUT_SECONDS
        )
    except (subprocess.TimeoutExpired, OSError):
        return -1, ""
    return r.returncode, r.stdout.strip()


def probe_bridge(agent: str, cfg: dict) -> tuple[str, str]:
    """Probe one agent's inbound telegram poller.

    Returns (status, detail) where status is:
      'up'      — bot.pid points to a live `bun ... server.ts` process.
      'down'    — bot.pid present but its PID is dead / not a bun-server process
                  (the 409-death signature). THIS is the alertable state.
      'no_bridge' — no bot.pid file (agent has no configured inbound bridge) → skip.
      'inconclusive' — a probe subprocess failed (sudo denied, timeout); fail-open,
                  do NOT alert (a probe failure is not a bridge failure).
    """
    pid_path = _bot_pid_path_for(cfg)
    sudo = _sudo_prefix(cfg["sudo_user"])

    # Read bot.pid (cat returns the pid; "MISSING" if absent — disambiguates a
    # genuinely-absent pidfile from a sudo failure read off rc).
    rc, out = _run(sudo + ["bash", "-c", f"cat {pid_path} 2>/dev/null || echo MISSING"])
    if rc != 0:
        return "inconclusive", f"bot.pid read rc={rc}"
    if out == "MISSING" or out == "":
        return "no_bridge", "no bot.pid (no inbound bridge configured)"
    pid = out.split()[0].strip()
    if not pid.isdigit():
        return "inconclusive", f"bot.pid not numeric: {out!r}"

    # Is that PID a live `bun ... server.ts` process? (the bridge cmdline).
    rc2, cmd = _run(sudo + ["bash", "-c", f"ps -o args= -p {pid} 2>/dev/null || echo DEAD"])
    if rc2 != 0:
        return "inconclusive", f"ps rc={rc2}"
    if cmd == "DEAD" or cmd == "":
        return "down", f"bot.pid={pid} but process DEAD (409-death signature)"
    if "bun" not in cmd or "server.ts" not in cmd:
        return "down", f"bot.pid={pid} is not the bun bridge: {cmd[:80]!r}"

    # Identity check (T-1606): a recycled PID can land on an UNRELATED
    # `bun server.ts` (e.g. echo-recorder). The real bridge's cwd sits under the
    # Claude Telegram plugin cache — require it. A cwd probe subprocess FAILURE
    # is inconclusive (fail-open: probe failure ≠ bridge failure; a mid-probe
    # process death reads DEAD on the next poll); a cwd MISMATCH is down.
    rc3, cwd = _run(sudo + ["bash", "-c", f"readlink /proc/{pid}/cwd 2>/dev/null || echo UNKNOWN"])
    if rc3 != 0:
        return "inconclusive", f"cwd probe rc={rc3}"
    if cwd not in ("UNKNOWN", ""):
        if not BRIDGE_CWD_RE.search(cwd):
            return "down", (
                f"bot.pid={pid} is a bun server.ts but NOT the telegram plugin "
                f"(cwd={cwd[:80]!r}) — stale-PID recycling (T-1606)"
            )
        return "up", f"bot.pid={pid} live telegram bridge (cwd …{cwd[-48:]})"

    # cwd unreadable — NOT a race: an `sg`-spawned bridge (orchestrator's launcher wraps
    # tmux in `sg pantheon-shared`) is non-dumpable, so /proc/PID/cwd is denied
    # even to its owner, PERSISTENTLY. Fall back to PPID ancestry (the finding's
    # "and/or"): the bridge child's parent is the plugin's `bun run --cwd
    # <plugin-cache-path> …` wrapper, whose argv IS readable regardless of
    # dumpability and carries the same identifying path. An echo-orphan or other
    # recycled PID has a parent (often PID 1) whose argv does not match.
    rc4, ppid = _run(sudo + ["bash", "-c", f"ps -o ppid= -p {pid} 2>/dev/null || echo UNKNOWN"])
    ppid = ppid.strip()
    if rc4 != 0 or not ppid.isdigit():
        return "inconclusive", f"bot.pid={pid} cwd unreadable and ppid probe failed; not judging"
    rc5, pargs = _run(sudo + ["bash", "-c", f"ps -o args= -p {ppid} 2>/dev/null || echo UNKNOWN"])
    if rc5 != 0 or pargs in ("UNKNOWN", ""):
        return "inconclusive", f"bot.pid={pid} cwd unreadable and parent args unreadable; not judging"
    if not BRIDGE_CWD_RE.search(pargs):
        return "down", (
            f"bot.pid={pid} is a bun server.ts but neither cwd nor parent (ppid={ppid}, "
            f"args={pargs[:60]!r}) identifies the telegram plugin — stale-PID recycling (T-1606)"
        )
    return "up", f"bot.pid={pid} live telegram bridge (via parent --cwd, ppid={ppid})"


def _in_alert_cooldown(agent: str, now: int, cooldown: int) -> bool:
    import json
    p = _alert_state_path_for(agent)
    if not p.exists():
        return False
    try:
        last = int(json.loads(p.read_text(encoding="utf-8")).get("ts", 0))
    except (ValueError, OSError, TypeError):
        return False
    return (now - last) < cooldown


def _record_alert(agent: str, now: int, detail: str) -> None:
    import json
    p = _alert_state_path_for(agent)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ts": now, "agent": agent, "detail": detail}) + "\n", encoding="utf-8")


def _remediate_state_path_for(agent: str) -> Path:
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    return state_dir / f"channel_liveness_{agent}_remediate.json"


def _maybe_remediate(agent: str, cfg: dict, detail: str, *, now: int) -> bool:
    """On a confirmed bridge-down, fire the graceful-restart path. SHIP DARK.

    Gated on PANTHEON_CHANNEL_REMEDIATE_ENABLED (default off). Own cooldown +
    consecutive tracking, independent of the alert throttle. Tombstone path for
    recovery (use_tombstone); direct fire_restart for orchestrator/specialist. Returns True iff a
    restart was initiated. "Verify the bridge returns" is cross-cycle: if the next
    probe still reads down after a remediation + cooldown, `consecutive` climbs and
    we escalate (a restart that does not clear the bridge is not the fix)."""
    import json
    if not REMEDIATE_ENABLED:
        _log(agent, "bridge down: remediation DISABLED (ship-dark); alert-only")
        return False
    path = _remediate_state_path_for(agent)
    prior = None
    try:
        if path.exists():
            prior = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        prior = None
    prior_ts = 0
    if isinstance(prior, dict):
        try:
            prior_ts = int(prior.get("ts", 0))
        except (TypeError, ValueError):
            prior_ts = 0
    if prior_ts and (now - prior_ts) < REMEDIATE_COOLDOWN_SECONDS:
        _log(agent, "bridge down but in remediation cooldown; not re-firing")
        return False
    consecutive = 1
    if prior_ts and (now - prior_ts) < 2 * REMEDIATE_COOLDOWN_SECONDS:
        try:
            consecutive = int(prior.get("consecutive", 0)) + 1
        except (TypeError, ValueError):
            consecutive = 2
    if consecutive >= REMEDIATE_ESCALATE_AT:
        try:
            from tools.inbox_consumer import route_failure_alert
            route_failure_alert(
                agent,
                f"🔁 [channel-liveness] {agent}: bridge-down remediation #{consecutive} — a "
                f"prior graceful restart did NOT restore the inbound poller. The restart is "
                f"not clearing it (upstream / repeated 409?). Operator look needed. T-0997.",
                VOCAB,
            )
        except Exception as exc:
            _log(agent, f"escalation alert failed ({exc!r})")
    initiated = False
    if cfg.get("use_tombstone"):
        state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
        state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
        tombstone = write_tombstone(state_dir, agent, reason=REMEDIATE_REASON)
        _log(agent, f"bridge down: wrote graceful-restart tombstone {tombstone} (drain fires it)")
        initiated = True
    else:
        ok, output = fire_restart(cfg, agent)
        _log(agent, f"bridge down: graceful restart {'fired' if ok else 'FAILED'}: {output[:160]}")
        initiated = ok
    if initiated:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"ts": now, "agent": agent, "consecutive": consecutive, "reason": REMEDIATE_REASON}) + "\n",
            encoding="utf-8",
        )
    return initiated


def _in_restart_window(agent: str, now: int, grace: int | None = None) -> bool:
    """True while the agent is within the post-restart grace window (T-1622).

    A LEGITIMATE graceful restart briefly has no live poller (the bridge
    re-registers when the fresh session boots), so a 'down' read inside this
    window is expected, not alertable. Source of truth: the context-fill
    monitor's per-agent state files — last_fire (written unconditionally at
    fire time, covers restarts recorded 'failed') and last_restart (success
    path + recovery's tombstone write). Scope note: restarts initiated OUTSIDE the
    context-fill machinery (manual tmux kill, watchdog child-recovery) don't
    stamp these files; their exposure window is one poll and the alert is
    merely early, not wrong. Fail-open: unreadable state = not in window.
    """
    import json
    grace = RESTART_GRACE_SECONDS if grace is None else grace
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    latest = 0
    for name in (f"context_fill_{agent}_last_fire.json", f"context_fill_{agent}_last_restart.json"):
        try:
            ts = json.loads((state_dir / name).read_text()).get("ts", 0)
            latest = max(latest, int(ts))
        except (OSError, ValueError, TypeError):
            continue
    return latest > 0 and (now - latest) < grace


def check_agent(agent: str, *, dry_run: bool = False, now: int | None = None) -> str:
    """Probe one agent and alert on a fresh DOWN. Returns the probe status."""
    now = int(time.time()) if now is None else now
    cfg = AGENT_CONFIG.get(agent)
    if cfg is None:
        _log(agent, "no AGENT_CONFIG entry; skipping")
        return "no_bridge"
    status, detail = probe_bridge(agent, cfg)
    _log(agent, f"bridge {status}: {detail}")
    if status != "down":
        return status
    # Restart-window precondition (T-1622): a down-read during a recent graceful
    # restart is the restart, not an incident — suppress BOTH the alert and the
    # (flag-gated) remediation; a genuinely dead bridge is still down next poll
    # past the grace and fires normally.
    if _in_restart_window(agent, now):
        _log(agent, f"down but within the post-restart grace window "
                    f"({RESTART_GRACE_SECONDS}s); expected during a graceful "
                    f"restart — not alerting/remediating this poll")
        return status
    if dry_run:
        _log(agent, "DRY-RUN: would alert + (if enabled) remediate (bridge down)")
        return status
    # Alert (throttled by the alert cooldown).
    if _in_alert_cooldown(agent, now, ALERT_COOLDOWN_SECONDS):
        _log(agent, "down but in alert cooldown; not re-alerting")
    else:
        msg = (
            f"📵 [channel-liveness] {agent}: inbound Telegram poller DOWN while session "
            f"appears healthy ({detail}). Comms-dark — inbound DMs not received. "
            f"Likely the single-poller 409 (a transient `claude mcp list` on the live "
            f"bot). Remediate by cycling the session (graceful restart re-inits the "
            f"plugin; `systemctl restart` is a no-op). T-0894."
        )
        try:
            from tools.inbox_consumer import route_failure_alert
            route_failure_alert(agent, msg, VOCAB)
            _record_alert(agent, now, detail)
            _log(agent, "alerted via route_failure_alert")
        except Exception as exc:  # fail-open: a notify failure must not crash the poll
            _log(agent, f"alert routing failed ({exc!r})")
    # Auto-remediation (flag-gated, ship-dark; own cooldown) — independent of the
    # alert throttle so a persistent-down bridge still gets remediated.
    try:
        _maybe_remediate(agent, cfg, detail, now=now)
    except Exception as exc:  # fail-open: remediation errors must not crash the poll
        _log(agent, f"remediation error ({exc!r})")
    return status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Channel-liveness monitor (T-0894).")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--agent", choices=sorted(AGENT_CONFIG.keys()), help="probe one agent")
    g.add_argument("--all", action="store_true", help="probe every configured agent")
    ap.add_argument("--dry-run", action="store_true", help="probe + log, but do NOT alert")
    args = ap.parse_args(argv)

    agents = sorted(AGENT_CONFIG.keys()) if args.all else [args.agent]
    for agent in agents:
        check_agent(agent, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
