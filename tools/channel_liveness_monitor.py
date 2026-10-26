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

from tools import seat_state  # noqa: E402  (ORG-106 L4b)

# Reuse context_fill_monitor's per-agent config + sudo helper (one source of
# truth for sudo_user) and inbox_consumer's failure-alert router.
from tools.context_fill_monitor import (  # noqa: E402
    AGENT_CONFIG,
    _read_runtime_state,
    _reads_cross_account,
    _runtime_state_path_for_agent,
    _seat_in_maintenance,
    _seat_muted,
    _sudo_prefix,
    fire_restart_detached,
    write_tombstone,
)
# CONTRACT (T-1795): this module must NEVER import/call the BLOCKING
# fire_restart — this unit is a short-budget oneshot (TimeoutStartSec ~3min)
# and a blocking restart gets cgroup-SIGKILLed mid-ack, orphaning the delivered
# handoff prompt. Remediation goes through fire_restart_detached only; the
# absence of a `fire_restart` binding here is pinned by the test suite.

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


def _bridge_env_path_for(cfg: dict) -> str:
    """The telegram bridge's .env — present iff the bridge is CONFIGURED for this
    account (T-1794). Distinguishes 'no bridge ever configured' from 'configured
    bridge died and left no pidfile'."""
    return f"/home/{cfg['sudo_user']}/.claude/channels/telegram/.env"


def _bridge_is_configured(cfg: dict, sudo: list[str]) -> bool | None:
    """True if the bridge .env exists (configured), False if absent, None if the
    probe itself failed (never treat a probe failure as 'unconfigured')."""
    rc, out = _run(sudo + ["bash", "-c",
                           f"test -f {_bridge_env_path_for(cfg)} && echo YES || echo NO"])
    if rc != 0:
        return None
    return out.strip() == "YES"


def _session_unit_is_disabled(agent: str, cfg: dict) -> bool | None:
    """Is this agent's session-owning unit DISABLED — i.e. deliberately paused?

    T-2393. A seat whose telegram/session unit has been `systemctl disable`d is OFF
    ON PURPOSE. Its poller is absent because somebody took it down, not because the
    bridge died — so the down-read is the pause, not an incident, and remediating it
    is fighting whoever paused it. MEASURED 2026-08-02: after the operator-directed
    fleet daemon pause (the operator TG6694 — "temp turn off daemons for everyone except
    you, codex, and recovery"; orchestrator stopped AND disabled 23 units across specialist,
    archivist, mason, artisan), this monitor alerted on specialist and archivist every poll
    and fired FOUR graceful-restart remediations at specialist, each one escalating
    "Operator look needed" to Recovery — while the fleet sat under token minimization.

    The alert's own premise was false, and that is the sharper half: it said "poller
    DOWN while SESSION APPEARS HEALTHY", and those seats had no claude process and no
    tmux server at all. The monitor never checked the session; "appears healthy" was
    an assumption baked into the alert TEXT, inherited from the 409-death incident
    this probe was written for, where the session genuinely was up.

    DISABLED is the discriminator, not INACTIVE. A crashed unit stays `enabled` and
    reads inactive/failed — that must still alert. Only an explicit disable means a
    human took it out of service. Verified both directions at the time of writing:
    orchestrator (running) reads enabled+active; specialist and archivist (paused) read
    disabled+inactive.

    Returns True (disabled → paused), False (enabled/static/… → judge normally), or
    None when we cannot tell — no restart_service configured, or the probe failed.
    None must NOT be read as paused: fail-open here means keep alerting, because
    silencing a real comms-dark agent is the expensive direction. (See
    feedback_unknown_must_not_stand_in_for_no.)
    """
    svc = cfg.get("restart_service")
    if not svc:
        return None
    mode = cfg.get("restart_mode", "user")
    if mode == "user":
        probe = f'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user is-enabled {svc}'
    else:
        probe = f"systemctl is-enabled {svc}"
    # is-enabled exits NON-ZERO for `disabled`/`masked`, so the verdict comes from
    # stdout, never from rc. rc is only consulted to detect a probe that produced
    # nothing at all (sudo denied, timeout) → unknown.
    rc, out = _run(_sudo_prefix(cfg["sudo_user"]) + ["bash", "-c", probe])
    state = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if not state:
        return None
    if state in ("disabled", "masked", "masked-runtime"):
        # T-2671: `disabled` ALONE is not a pause. A seat can be disabled (not
        # enabled to auto-start) yet ACTIVE — launched by a scope or manually
        # rather than by enabling the unit (recovery ran disabled+ACTIVE via
        # pantheon-runtime-tmux.scope). Treating disabled-alone as paused
        # FALSE-SUPPRESSES a live seat's alerts. Genuine pause is disabled AND
        # not-running (specialist/archivist read disabled+INACTIVE, T-2393). So
        # confirm the unit is actually stopped before calling it paused; a
        # disabled-but-ACTIVE seat judges normally. Fail-open: if we cannot
        # confirm it is inactive, do NOT suppress (None), because silencing a
        # possibly-live comms-dark agent is the expensive direction.
        active = _session_unit_is_active(cfg)
        if active is True:
            return False   # disabled but RUNNING → not paused; judge normally
        if active is False:
            return True    # disabled AND stopped → genuinely paused
        return None        # can't confirm stopped → fail-open (keep alerting)
    if state in ("enabled", "enabled-runtime", "static", "linked", "linked-runtime",
                 "indirect", "generated", "transient", "alias"):
        return False
    return None


def _session_unit_is_active(cfg: dict) -> bool | None:
    """Is this agent's session-owning unit ACTIVE (running)?

    T-2671 companion to `_session_unit_is_disabled`: the disabled-vs-paused
    decision needs to know whether a `disabled` unit is nonetheless running
    (disabled+ACTIVE = live via a scope, not paused). Returns True (active/
    activating), False (inactive/failed/deactivating/dead), or None when the
    probe could not answer — None must NOT be read as stopped (fail-open:
    a probe failure is not proof the seat is down).
    """
    svc = cfg.get("restart_service")
    if not svc:
        return None
    mode = cfg.get("restart_mode", "user")
    if mode == "user":
        probe = f'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user is-active {svc}'
    else:
        probe = f"systemctl is-active {svc}"
    # is-active exits non-zero for inactive/failed, so the verdict comes from
    # stdout, never rc; rc is only used to detect a probe that produced nothing.
    rc, out = _run(_sudo_prefix(cfg["sudo_user"]) + ["bash", "-c", probe])
    state = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if not state:
        return None
    if state in ("active", "activating", "reloading"):
        return True
    if state in ("inactive", "failed", "deactivating", "dead"):
        return False
    return None


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


def _pidfile_is_provably_absent(cfg: dict, sudo: list[str]) -> bool | None:
    """True if bot.pid provably does not exist, False if it EXISTS but was
    unreadable, None if the probe could not tell (T-2359).

    Exists because `cat P 2>/dev/null || echo MISSING` cannot answer this. The
    `|| echo` makes the pipeline succeed no matter why `cat` failed, so rc is
    always 0 and the caller's `rc != 0 -> inconclusive` guard is UNREACHABLE.
    Measured: a genuinely-absent file, a mode-000 file, and a present file under
    a non-traversable parent all return rc=0 out=MISSING — byte-identical.
    """
    p = _bot_pid_path_for(cfg)
    rc, out = _run(sudo + ["bash", "-c",
                           f'if [ -e {p} ]; then echo PRESENT; '
                           f'elif [ -d "$(dirname {p})" ]; then echo ABSENT; '
                           f'else echo UNKNOWN; fi'])
    if rc != 0:
        return None
    if out == "ABSENT":
        return True
    if out == "PRESENT":
        return False
    # Parent dir unreadable/absent: cannot prove the pidfile is gone.
    return None


CODEX_POLLER_UNIT = "codex-telegram-poller.service"

# Runs as the seat's account. Prints exactly one verdict word and never the token:
# SAME (the codex poller's live process holds the bot token in the plugin .env),
# DIFF, NOPOLLER (unit not active / no MainPID), or UNKNOWN (anything unreadable).
_CODEX_POLLER_PROBE = r"""
import hashlib, os, re, subprocess, sys
def h(b): return hashlib.sha256(b).hexdigest()
try:
    out = subprocess.run(["systemctl", "--user", "show", "-p", "ActiveState", "-p", "MainPID",
                          sys.argv[1]], capture_output=True, text=True, timeout=10,
                         env=dict(os.environ, XDG_RUNTIME_DIR="/run/user/%d" % os.getuid())).stdout
    f = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    pid = f.get("MainPID", "0")
    if f.get("ActiveState") != "active" or pid in ("", "0"):
        print("NOPOLLER"); sys.exit(0)
    live = None
    for e in open("/proc/%s/environ" % pid, "rb").read().split(b"\0"):
        if e.startswith(b"TELEGRAM_BOT_TOKEN="):
            live = e.split(b"=", 1)[1]
    if live is None:
        cfgdir = os.path.expanduser("~/.config/codex-telegram/.env")
        for l in open(cfgdir, "rb"):
            if l.strip().startswith(b"TELEGRAM_BOT_TOKEN="):
                live = l.strip().split(b"=", 1)[1].strip(b"'\"")
    plug = None
    for l in open(sys.argv[2], "rb"):
        m = re.match(rb"\s*(?:export\s+)?TELEGRAM_BOT_TOKEN\s*=\s*[\"']?([^\"'\s]+)", l)
        if m:
            plug = m.group(1)
    if not live or not plug:
        print("UNKNOWN")
    else:
        print("SAME" if h(live) == h(plug) else "DIFF")
except Exception:
    print("UNKNOWN")
"""


def _codex_poller_serves_bridge(cfg: dict) -> bool | None:
    """T-2959: is this seat's inbound bot polled by its codex harness instead?

    A seat on codex runs codex-telegram-poller.service against the SAME bot as the
    Claude plugin bridge. Telegram allows one poller per bot, so the plugin bridge
    is down by design while codex holds it, and probe_bridge's Claude-only checks
    read comms-dark (specialist 2026-10-14: 7 false pages; orchestrator during T-2953).
    True only on positive evidence: the poller unit is active AND its live process
    holds the plugin .env's token (compared by hash in the seat's own account; the
    token never leaves that process). A poller for a DIFFERENT bot is False, so it
    can never mask a real outage. None = could not tell (callers keep 'down')."""
    rc, out = _run(_sudo_prefix(cfg["sudo_user"]) + [
        "python3", "-I", "-c", _CODEX_POLLER_PROBE, CODEX_POLLER_UNIT, _bridge_env_path_for(cfg)])
    verdict = out.strip().splitlines()[-1].strip() if (rc == 0 and out.strip()) else ""
    if verdict == "SAME":
        return True
    if verdict in ("DIFF", "NOPOLLER"):
        return False
    return None


def probe_bridge(agent: str, cfg: dict) -> tuple[str, str]:
    """Probe one agent's inbound telegram path: the Claude plugin bridge, or (when
    that reads down) a codex-telegram poller holding the same bot (T-2959).

    Returns the same statuses as _probe_claude_bridge; a 'down' becomes 'up' only
    when _codex_poller_serves_bridge proves the codex harness polls the bot."""
    status, detail = _probe_claude_bridge(agent, cfg)
    if status != "down":
        return status, detail
    if _codex_poller_serves_bridge(cfg):
        return "up", (f"{CODEX_POLLER_UNIT} active on the same bot token (codex harness); "
                      f"Claude plugin bridge idle by design ({detail})")
    return status, detail


def _probe_claude_bridge(agent: str, cfg: dict) -> tuple[str, str]:
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
        # T-2359: "MISSING" does NOT mean absent. `cat P || echo MISSING` masks
        # WHY cat failed — absent, mode 000, and a non-traversable parent are all
        # rc=0/MISSING, measured and byte-identical — so the rc guard above cannot
        # fire and a merely-UNREADABLE pidfile would be laundered into the
        # confident "bridge died/never-started" below. That is a stated CAUSE
        # produced by a blind predicate, and it drives both a misdirecting alert
        # ("Likely the single-poller 409") and a flag-gated session cycle of a
        # possibly-healthy bridge. Note the asymmetry this fixes: the cwd/ppid
        # probes further down already use a DON'T-KNOW sentinel (UNKNOWN) and
        # fall through; only this branch used a CONCLUSION word. Prove absence
        # before naming a cause.
        provably_absent = _pidfile_is_provably_absent(cfg, sudo)
        if provably_absent is None:
            return "inconclusive", "bot.pid unreadable and absence not provable"
        if provably_absent is False:
            return ("inconclusive",
                    "bot.pid EXISTS but could not be read — a read failure is not "
                    "a bridge failure")
        # T-1794: a genuinely-absent bot.pid is ambiguous. Disambiguate via the bridge
        # .env: present → the bridge is CONFIGURED but its pidfile is gone → the
        # bridge DIED/never-started → DOWN (alertable; orchestrator sat comms-dark ~1
        # day on 2026-07-16 because this returned no_bridge/skip). Absent → the
        # account has no bridge → no_bridge (skip). A configured-but-missing
        # DOWN during a legitimate restart is suppressed by the T-1622
        # restart-window guard in check_agent.
        configured = _bridge_is_configured(cfg, sudo)
        if configured is None:
            return "inconclusive", "bot.pid absent + .env probe failed"
        if configured:
            return "down", "bot.pid ABSENT but bridge configured (.env present) — bridge died/never-started"
        return "no_bridge", "no bot.pid and no .env (no inbound bridge configured)"
    pid = out.split()[0].strip()
    if not pid.isdigit():
        return "inconclusive", f"bot.pid not numeric: {out!r}"

    # Is that PID a live `bun ... server.ts` process? (the bridge cmdline).
    # T-2359: same masking as the pidfile read above — `|| echo DEAD` turned every
    # ps failure into a confident "process DEAD (409-death signature)". Separate
    # ps's own exit status instead: rc 1 is ps's documented "no such process"
    # (genuinely dead), anything else is ps failing to answer, which is not a
    # statement about the bridge.
    rc2, cmd = _run(sudo + ["bash", "-c",
                            f'o=$(ps -o args= -p {pid} 2>/dev/null); r=$?; '
                            f'if [ $r -eq 0 ]; then printf "%s" "$o"; '
                            f'elif [ $r -eq 1 ]; then echo NOSUCHPROC; '
                            f'else echo PSFAIL; fi'])
    if rc2 != 0:
        return "inconclusive", f"ps rc={rc2}"
    if cmd == "PSFAIL" or cmd == "":
        return "inconclusive", f"ps could not answer for pid={pid}"
    if cmd == "NOSUCHPROC":
        return "down", f"bot.pid={pid} but process DEAD (409-death signature)"
    if "bun" not in cmd or "server.ts" not in cmd:
        return "down", f"bot.pid={pid} is not the bun bridge: {cmd[:80]!r}"

    # Identity via the bridge's OWN argv (T-2697): the plugin launches the bridge
    # as `bun run --cwd <plugin-cache> … server.ts`, so the identifying plugin-cache
    # path is frequently in the bridge PROCESS's own argv — that is recovery's live
    # topology (`claude --channels plugin:telegram` spawns the bun bridge DIRECTLY,
    # with no intermediate `bun run --cwd` wrapper, so /proc/PID/cwd is unreadable
    # AND the parent is a plain `claude --channels` whose argv lacks the cache path;
    # only the own argv carries it). Check it FIRST: it is a stronger positive id
    # than /proc/PID/cwd (often unreadable) and needs no extra probe. An unrelated
    # recycled `bun server.ts` (echo-recorder et al.) never carries the telegram
    # plugin-cache --cwd, so this does not weaken the T-1606 anti-recycling guard.
    if BRIDGE_CWD_RE.search(cmd):
        return "up", f"bot.pid={pid} live telegram bridge (own argv --cwd plugin cache)"

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
    # Accept EITHER the plugin's `bun run --cwd <cache>` wrapper parent (orchestrator's
    # topology) OR a `claude --channels plugin:telegram …` parent (T-2697): the
    # bun bridge's direct parent when claude spawns it without an intermediate
    # wrapper. A recycled unrelated `bun server.ts` is not parented by a
    # `claude --channels plugin:telegram` process, so this stays fail-toward-DOWN
    # for genuine impostors.
    if not (BRIDGE_CWD_RE.search(pargs) or "plugin:telegram" in pargs):
        return "down", (
            f"bot.pid={pid} is a bun server.ts but neither cwd nor parent (ppid={ppid}, "
            f"args={pargs[:60]!r}) identifies the telegram plugin — stale-PID recycling (T-1606)"
        )
    return "up", f"bot.pid={pid} live telegram bridge (via parent, ppid={ppid})"


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
    if not cfg.get("remediate", True):
        # Per-agent observation-only opt-out (archivist: auto-restart is her
        # call per the 2026-07-20 orchestrator+recovery profile agreement, and her account
        # has no pantheon-config clone for restart_bin anyway).
        _log(agent, "bridge down: remediation opted out per-agent (remediate=False); alert-only")
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
        prior_unit = prior.get("unit") if isinstance(prior, dict) else None
        forensics = (
            f" Prior attempt forensics: journalctl --user -u {prior_unit}." if prior_unit else ""
        )
        try:
            from tools.inbox_consumer import route_failure_alert
            route_failure_alert(
                agent,
                f"🔁 [channel-liveness] {agent}: bridge-down remediation #{consecutive} — a "
                f"prior graceful restart did NOT restore the inbound poller. The restart is "
                f"not clearing it (upstream / repeated 409?). Operator look needed."
                f"{forensics} T-0997.",
                VOCAB,
            )
        except Exception as exc:
            _log(agent, f"escalation alert failed ({exc!r})")
    initiated = False
    unit_name = None
    if cfg.get("use_tombstone"):
        state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
        state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
        tombstone = write_tombstone(state_dir, agent, reason=REMEDIATE_REASON)
        _log(agent, f"bridge down: wrote graceful-restart tombstone {tombstone} (drain fires it)")
        initiated = True
    else:
        # T-1795: DETACHED dispatch — the restart runs in its own transient
        # unit, outside this oneshot's cgroup and timeout. ok means dispatched,
        # not restarted; a script failure surfaces cross-cycle (the next
        # post-cooldown probe still reads down → consecutive climbs → escalate).
        # Retry-latency note: a dispatch that later fails waits out the full
        # cooldown before re-fire (the old sync path retried next poll) —
        # accepted per the bounded-remediation posture.
        ok, output, unit_name = fire_restart_detached(cfg, agent)
        if ok:
            _log(agent, f"bridge down: graceful restart DISPATCHED as transient unit "
                        f"{unit_name} (async; forensics: journalctl --user -u {unit_name})")
        else:
            _log(agent, f"bridge down: restart dispatch FAILED: {output[:160]}")
        initiated = ok
    if initiated:
        record = {"ts": now, "agent": agent, "consecutive": consecutive, "reason": REMEDIATE_REASON}
        if unit_name:
            record["unit"] = unit_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
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

    T-1795: our own detached remediation dispatch also counts — the restart now
    runs OUTSIDE this monitor's poll (the sync path used to block through it),
    so the next poll can land mid-restart; without this stamp that legitimate
    down-read logs noise and, worse, an independent second bridge-death 30-60
    min later would read consecutive=2 → a false "restart did NOT restore"
    escalation. Do NOT stamp context_fill_last_fire from the remediation path —
    that file feeds context-fill's dead-transcript guard (T-1330).

    T-5156: a runtime-controller respawn counts too (see
    _controller_respawn_ts). With remediation ON, a probe that lands mid-
    respawn used to read the OLD bot.pid as dead and dispatch a restart that
    killed the fresh session (specialist 21:04Z + 22:34Z 2026-10-18).
    """
    import json
    grace = RESTART_GRACE_SECONDS if grace is None else grace
    state_dir_env = os.environ.get("PANTHEON_STATE_DIR")
    state_dir = Path(state_dir_env) if state_dir_env else _DEFAULT_STATE_DIR
    latest = 0
    for name in (
        f"context_fill_{agent}_last_fire.json",
        f"context_fill_{agent}_last_restart.json",
        f"channel_liveness_{agent}_remediate.json",
    ):
        try:
            ts = json.loads((state_dir / name).read_text()).get("ts", 0)
            latest = max(latest, int(ts))
        except (OSError, ValueError, TypeError):
            continue
    if latest > 0 and (now - latest) < grace:
        return True  # already in window: skip the (sudo) controller reads
    latest = max(latest, _controller_respawn_ts(agent))
    return latest > 0 and (now - latest) < grace


# Controller evidence events that mean "this seat's runtime is being (re)started":
# the session died (the controller respawns it) or a start just completed.
# Deliberately NOT controller_start_deferred_lock_contended: our own remediation
# holding the operation lock emits it, so it must not extend suppression.
_RESPAWN_EVIDENCE_EVENTS = ("chronicle_session_end", "post_start_command")
_EVIDENCE_TAIL_BYTES = 262144


def _iso_epoch(value) -> int:
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return 0


def _controller_respawn_ts(agent: str) -> int:
    """Epoch of the seat's latest controller (re)start signal, 0 if none (T-5156).

    Sources, both under the seat's runtime-controller dir: runtime.json's
    runtime_start_{boundary,observed}_at (written once a start is observed) and
    the tail of controller-evidence.jsonl for _RESPAWN_EVIDENCE_EVENTS, which
    covers the gap between the old session dying and runtime.json moving.
    Another account's dir is mode 700, so both are read as that account via
    sudo (same path as context_fill_monitor's T-5024 gate). Fail-open: any
    unreadable/malformed source contributes nothing. Bounded by the caller's
    grace, so a seat that dies and is NOT respawned still alerts."""
    import json
    account = (AGENT_CONFIG.get(agent) or {}).get("sudo_user")
    runtime_path = _runtime_state_path_for_agent(agent, account)
    latest = 0
    kind, text = _read_runtime_state(runtime_path, account)
    if kind == "ok":
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
        if isinstance(doc, dict):
            for key in ("runtime_start_boundary_at", "runtime_start_observed_at"):
                latest = max(latest, _iso_epoch(doc.get(key)))
    for rec in _respawn_evidence_records(agent, account, runtime_path):
        latest = max(latest, _iso_epoch(rec.get("recorded_at")))
    return latest


def _respawn_evidence_records(agent: str, account, runtime_path: Path) -> list[dict]:
    """The _RESPAWN_EVIDENCE_EVENTS records in the tail of the seat's
    controller-evidence.jsonl (read as the seat account when cross-account).
    Fail-open: an unreadable file or a malformed line contributes nothing."""
    import json
    evidence_path = runtime_path.parent / "controller-evidence.jsonl"
    try:
        if _reads_cross_account(account):
            proc = subprocess.run(
                _sudo_prefix(str(account)) + [
                    "bash", "-c", f'exec tail -c {_EVIDENCE_TAIL_BYTES} -- "$1"', "_", str(evidence_path)],
                capture_output=True, text=True, timeout=15)
            tail = proc.stdout if proc.returncode == 0 else ""
        else:
            with open(evidence_path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - _EVIDENCE_TAIL_BYTES))
                tail = fh.read().decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        tail = ""
    out = []
    for line in tail.splitlines():
        if not any(ev in line for ev in _RESPAWN_EVIDENCE_EVENTS):
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("event") in _RESPAWN_EVIDENCE_EVENTS:
            out.append(rec)
    return out


# T-5165: the grace above reopens on EVERY respawn, so a seat whose session dies
# and is respawned every <=10 min would never page. The unit's StartLimitBurst only
# bounds controller-process crashes, not session deaths, and no other monitor counts
# them. So: this many controller-recorded session DEATHS (chronicle_session_end
# cause=death; graceful replace/start/fallback-return ends do not count) inside the
# window means a crash loop, and the grace stops suppressing the alert. Calibrated on
# specialist's evidence 10-15..10-19: 14 deaths, at most 2 in any hour (the oomd pairs).
RESPAWN_LOOP_DEATHS = int(os.environ.get("CHANNEL_LIVENESS_RESPAWN_LOOP_DEATHS", "3"))
RESPAWN_LOOP_WINDOW_SECONDS = int(os.environ.get("CHANNEL_LIVENESS_RESPAWN_LOOP_WINDOW_SECONDS", "3600"))


def _session_deaths_in_window(agent: str, now: int, window: int | None = None) -> int:
    """Controller-recorded session deaths for *agent* in the last *window* seconds (T-5165)."""
    window = RESPAWN_LOOP_WINDOW_SECONDS if window is None else window
    account = (AGENT_CONFIG.get(agent) or {}).get("sudo_user")
    runtime_path = _runtime_state_path_for_agent(agent, account)
    deaths = set()
    for rec in _respawn_evidence_records(agent, account, runtime_path):
        if rec.get("event") != "chronicle_session_end" or rec.get("cause") != "death":
            continue
        ts = _iso_epoch(rec.get("recorded_at"))
        if ts and 0 <= now - ts < window:
            deaths.add(rec.get("transition_id") or ts)
    return len(deaths)


def _sleep_breach(agent: str, detail: str, seat, *, now: int, dry_run: bool) -> str:
    """ORG-106 L4b: the seat is declared asleep (deep-sleep, phase asleep) yet its inbound
    bridge is UP, i.e. a session is running. A real fault: page it. Alert-only (this
    monitor never kills a session). Deliberately NOT gated by maintenance, a pause or the
    restart grace, and it skips this monitor's LOCAL mute gate (_seat_muted). The global
    ORG-076 section 7 chokepoint still applies: the page goes through route_failure_alert,
    which is where the operator mute is enforced, so a muted seat's breach page is
    suppressed there in production. Only the alert cooldown throttles repeats here."""
    _log(agent, f"SLEEP BREACH: declared asleep but the bridge is up ({detail})")
    if dry_run:
        _log(agent, "DRY-RUN: would page sleep_breach")
        return "sleep_breach"
    if _in_alert_cooldown(agent, now, ALERT_COOLDOWN_SECONDS):
        _log(agent, "sleep breach in alert cooldown; not re-alerting")
        return "sleep_breach"
    msg = (
        f"🛌 [channel-liveness] {agent}: SLEEP BREACH — the seat is declared in deep-sleep "
        f"(phase asleep; {seat.owner or '?'}: {seat.reason or '-'}; until "
        f"{seat.expires or 'no expiry'}) but its inbound Telegram bridge is UP, so a session "
        f"is running. Either the sleep drain did not finish or something relaunched the seat. "
        f"Not auto-remediated. Check `seat_state status {agent}` and the controller evidence."
    )
    try:
        from tools.inbox_consumer import route_failure_alert
        route_failure_alert(agent, msg, "sleep_breach")
        _record_alert(agent, now, "sleep_breach")
        _log(agent, "sleep breach alerted via route_failure_alert")
    except Exception as exc:  # fail-open: a notify failure must not crash the poll
        _log(agent, f"alert routing failed ({exc!r})")
    return "sleep_breach"


def check_agent(agent: str, *, dry_run: bool = False, now: int | None = None) -> str:
    """Probe one agent and alert on a fresh DOWN. Returns the probe status.

    Adds one status probe_bridge itself cannot return: 'paused' (T-2393) — the
    bridge is down AND the agent's session unit is disabled, i.e. the seat was
    deliberately taken out of service. Not alertable, not remediable.
    """
    now = int(time.time()) if now is None else now
    cfg = AGENT_CONFIG.get(agent)
    if cfg is None:
        _log(agent, "no AGENT_CONFIG entry; skipping")
        return "no_bridge"
    status, detail = probe_bridge(agent, cfg)
    _log(agent, f"bridge {status}: {detail}")
    # ORG-106 L4b (T-5527): the declared seat state, read once and evaluated BEFORE every
    # suppression below (maintenance, pause, restart grace, mute, cooldown), so none of
    # them can hide a broken sleep.
    seat = seat_state.safe_effective_for(agent)
    if seat is not None and seat.expected_down:
        if status == "up":
            return _sleep_breach(agent, detail, seat, now=now, dry_run=dry_run)
        _log(agent, f"seat is asleep (deep-sleep; {seat.owner or '?'}: {seat.reason or '-'}; "
                    f"until {seat.expires or 'no expiry'}) — bridge {status} is expected; "
                    f"not alerting/remediating")
        return "asleep"
    if seat is not None and seat.state == seat_state.STATE_DEEP_SLEEP:
        # draining toward sleep: the session is being wound down on purpose.
        _log(agent, "seat is draining toward deep-sleep — not alerting/remediating this poll")
        return "asleep"
    if seat is not None and seat.state == seat_state.STATE_PAUSED_INTAKE:
        _log(agent, f"seat is paused-intake ({seat.owner or '?'}: {seat.reason or '-'}; "
                    f"until {seat.expires or 'no expiry'}) — the seat is up, judged normally")
    if status != "down":
        return status
    # Seat-maintenance precondition (T-2984) — the strongest "leave this seat alone"
    # signal, checked first among the down-path preconditions. During a runtime-
    # controller cutover (T-2953) the bridge may read down while the harness is being
    # swapped; alerting is noise and remediating fights the cutover. Auto-expires in
    # notify_policy, so a forgotten window self-clears and monitoring resumes.
    if _seat_in_maintenance(agent):
        _log(agent, "down but seat is in T-2984 maintenance mode (runtime-controller "
                    "cutover window) — not alerting/remediating this poll")
        return "maintenance"
    # Deliberate-pause precondition (T-2393) — checked BEFORE the restart window,
    # because a paused seat is not mid-restart and should not consult restart state
    # at all. A disabled session unit means somebody took this seat out of service;
    # alerting is noise and remediating is fighting them. Unknown (None) keeps the
    # existing behaviour: alert.
    if _session_unit_is_disabled(agent, cfg):
        _log(agent, f"down but session unit {cfg.get('restart_service')} is DISABLED "
                    f"— seat deliberately paused, not an incident; not "
                    f"alerting/remediating")
        return "paused"
    # Restart-window precondition (T-1622): a down-read during a recent graceful
    # restart is the restart, not an incident — suppress BOTH the alert and the
    # (flag-gated) remediation; a genuinely dead bridge is still down next poll
    # past the grace and fires normally.
    crash_loop_deaths = 0
    if _in_restart_window(agent, now):
        # T-5165: a crash loop keeps the window permanently open. Past the bound,
        # alert anyway; remediation stays off (cycling cannot fix a session that keeps
        # dying, and a probe mid-respawn kills the fresh session, T-5156).
        crash_loop_deaths = _session_deaths_in_window(agent, now)
        if crash_loop_deaths < RESPAWN_LOOP_DEATHS:
            _log(agent, f"down but within the post-restart grace window "
                        f"({RESTART_GRACE_SECONDS}s); expected during a graceful "
                        f"restart — not alerting/remediating this poll")
            return status
        _log(agent, f"down inside the restart grace, but {crash_loop_deaths} session deaths in "
                    f"{RESPAWN_LOOP_WINDOW_SECONDS}s: crash loop — alerting, not remediating")
    if dry_run:
        _log(agent, "DRY-RUN: would alert + (if enabled) remediate (bridge down)")
        return status
    # Alert (throttled by the alert cooldown). Operator mute (ORG-076 §7 / T-2987) is
    # checked FIRST: a deliberately-silenced persona withholds the comms-dark PAGE, but —
    # unlike 'paused'/'maintenance' above — a mute is ALERT-ONLY, so the remediation block
    # below still runs. Fail-open (is_muted → False on any registry fault). Trace stays in
    # the bridge-status _log above.
    if _seat_muted(agent):
        _log(agent, "down but seat muted (ORG-076 §7) — page suppressed (trace kept); remediation unaffected")
    elif _in_alert_cooldown(agent, now, ALERT_COOLDOWN_SECONDS):
        _log(agent, "down but in alert cooldown; not re-alerting")
    elif crash_loop_deaths:
        msg = (
            f"🔁 [channel-liveness] {agent}: session CRASH-LOOPING — the runtime controller "
            f"recorded {crash_loop_deaths} session deaths in the last "
            f"{RESPAWN_LOOP_WINDOW_SECONDS // 60} min and the inbound Telegram poller reads "
            f"DOWN ({detail}). Each respawn reopens the restart grace, so this is the bound "
            f"(T-5165). Not auto-remediated: cycling cannot fix a session that keeps dying. "
            f"Read controller-evidence.jsonl for the death cause; see "
            f"sop/remediation/control-plane-seat-dead-or-wedged.md."
        )
        try:
            from tools.inbox_consumer import route_failure_alert
            route_failure_alert(agent, msg, VOCAB)
            _record_alert(agent, now, detail)
            _log(agent, "crash-loop alerted via route_failure_alert")
        except Exception as exc:  # fail-open: a notify failure must not crash the poll
            _log(agent, f"alert routing failed ({exc!r})")
    else:
        msg = (
            # NB (T-2393): this used to assert "while session appears healthy". It
            # does not check the session, and on 2026-08-02 it said that about two
            # seats that had no claude process and no tmux server at all — an
            # unmeasured claim, stated in the alert's own voice, that sent the
            # reader looking for a 409 instead of at the pause. State only what
            # was probed.
            f"📵 [channel-liveness] {agent}: inbound Telegram poller DOWN — session "
            f"state NOT checked by this probe ({detail}). Comms-dark — inbound DMs not received. "
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
    if crash_loop_deaths:
        return status  # T-5165: alert-only inside the restart window (see above)
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
