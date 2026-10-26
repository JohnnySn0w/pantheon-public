#!/usr/bin/env python3
"""Agent-monitor liveness watchdog — structural guard for the node event-loop
wedge documented in bugs/agent-monitor-node-event-loop-wedge-long-uptime.md
(T-1885, S3STAR-WN-2026-07-20-001).

`agent-monitor.service` (`node server/index.js`, port 4820) is a long-uptime
node app whose event loop wedges: the listener stays open (`Restart=on-failure`
never trips — the process never exits) but HTTP requests stall/time out. The
only known fix is `systemctl --user restart agent-monitor.service`, and until
now that only happened when a human noticed the dashboard was down.

This closes the gap the same way the sibling monitors do: pure HTTP probe (no
LLM/API), timer-driven, ANTI-FLAP via a persisted consecutive-stall counter
(each timer fire is a fresh oneshot process — the counter lives in the state
file, not in-process; N=3 consecutive stalls), a restart cooldown so a slow recovery can't
restart-storm, and windowed escalation (a SECOND restart inside 2x the cooldown
means the restart isn't clearing the wedge — that's the operator-look signal,
not the routine case).

Runs on the `claude` account (same account agent-monitor.service runs under —
no cross-account sudo needed, unlike channel_liveness_monitor).

Alert routing: direct-to-operator via inbox_consumer._send_telegram_alert (the
disk_usage_monitor/credential_expiry_monitor shape — self-account infra
visibility, not per-agent liveness with an identity to route through Recovery) —
and ONLY on escalation. A single successful watchdog restart is the mechanism
working, not an incident; it's logged to the forensics jsonl, not pinged to
The operator (D1 — don't spend his bandwidth on routine self-healing).

POSTURE — DETECT (live) + REMEDIATE (ship-dark), matching the fleet's
restart-automation stance (channel_liveness_monitor):
detection, the consecutive-stall counter, and dry-run all run LIVE unconditionally;
the actual `systemctl restart` is gated behind PANTHEON_AGENT_MONITOR_REMEDIATE_ENABLED
(default OFF) pending a supervised first-fire (T-1134/T-1140 precedent — every
restart-automation arm gets one). The shipped systemd unit installs UNARMED;
arming is a separate post-merge HO deploy step.

CLI:
    python3 tools/agent_monitor_healthcheck.py --check     # cron path
    python3 tools/agent_monitor_healthcheck.py --dry-run   # probe + log, no restart/alert

Exit codes: 0 always (a monitor must not fail its own timer — breach state is
in the log/state file, matching disk_usage_monitor); 64 on a flagless run
(default-safe, T-0261).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"

# The dashboard's own baseline is 3-41ms; a genuine event-loop wedge stalls
# indefinitely rather than merely running slow, so one timeout value serves as
# both the request timeout AND the "too slow" bound — no separate threshold.
PROBE_TIMEOUT_SECONDS = int(os.environ.get("AGENT_MONITOR_PROBE_TIMEOUT_SECONDS", "5"))

AGENT_MONITOR_URL = os.environ.get("AGENT_MONITOR_URL", "http://127.0.0.1:4820/")
AGENT_MONITOR_UNIT_NAME = os.environ.get("AGENT_MONITOR_UNIT_NAME", "agent-monitor.service")

# N=3 consecutive stalls, chosen for the timer cadence: at the 2-min timer
# interval this is a ~4-6min confirm window —
# appropriate for a failure mode that degrades over days, not a spike.
STALL_CONSECUTIVE_THRESHOLD = int(os.environ.get("AGENT_MONITOR_STALL_THRESHOLD", "3"))

# Minimum spacing between restart attempts (mirrors channel_liveness_monitor's
# REMEDIATE_COOLDOWN_SECONDS / disk_usage_monitor's per-class cooldown
# convention) — a flapping/slow-recovering service must not get restart-stormed.
RESTART_COOLDOWN_SECONDS = int(os.environ.get("AGENT_MONITOR_RESTART_COOLDOWN_SECONDS", str(30 * 60)))

# A second restart within this multiple of the cooldown means the first
# restart did NOT clear the wedge — escalate to the operator (same windowed-count
# shape as channel_liveness_monitor._maybe_remediate's REMEDIATE_ESCALATE_AT).
RESTART_ESCALATE_WINDOW_MULT = 2

# Live-verified 2026-07-27 (T-1885 wet-run): `systemctl --user show
# agent-monitor.service -p TimeoutStopUSec` is 90s (systemd's default, unit
# has no override) — a genuinely wedged process can take up to that long to
# die under SIGTERM before systemd escalates to SIGKILL, and the restart
# subprocess blocks on the FULL stop+start cycle. A wet-run against a live
# wedge measured a 48s stop→start cycle; 30s was observed to time out the
# subprocess mid-cycle (the underlying systemd job still completed — killing
# the `systemctl` client does not cancel it — but this tool then mis-recorded
# a successful restart as a failure). 120s gives margin over the 90s stop
# ceiling plus node startup.
RESTART_TIMEOUT_SECONDS = int(os.environ.get("AGENT_MONITOR_RESTART_TIMEOUT_SECONDS", "120"))

# SHIP-DARK (T-1885 REBOUND round 1): matches channel_liveness_monitor's
# PANTHEON_CHANNEL_REMEDIATE_ENABLED — identical read shape (source-verified),
# default OFF pending a supervised first-fire (T-1134/T-1140 precedent: every
# restart-automation arm gets one). Detection, the consecutive-stall counter,
# and dry-run behave identically regardless of this flag — only the live
# `systemctl restart` is gated.
REMEDIATE_ENABLED = os.environ.get("PANTHEON_AGENT_MONITOR_REMEDIATE_ENABLED", "").strip().lower() in (
    "1", "true", "yes", "on",
)


def _log(msg: str) -> None:
    print(f"[agent_monitor_healthcheck ts={int(time.time())}] {msg}", flush=True)


def _state_dir() -> Path:
    d = os.environ.get("PANTHEON_STATE_DIR")
    return Path(d) if d else _DEFAULT_STATE_DIR


def _state_path() -> Path:
    return _state_dir() / "agent_monitor_healthcheck.json"


def _restart_log_path() -> Path:
    return _state_dir() / "agent_monitor_healthcheck_restarts.jsonl"


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        p = _state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state), encoding="utf-8")
    except OSError as exc:
        _log(f"could not persist state: {exc!r}")


def _append_restart_log(record: dict) -> None:
    """Best-effort forensics trail of every restart attempt. NEVER raises —
    the restart decision must not depend on this succeeding."""
    try:
        p = _restart_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as exc:
        _log(f"could not append restart log: {exc!r}")


def probe_healthcheck(url: str = AGENT_MONITOR_URL, timeout: int = PROBE_TIMEOUT_SECONDS) -> tuple[str, str]:
    """Probe the dashboard. Returns (status, detail):
      'HEALTHY' — HTTP 200 within timeout.
      'STALLED' — non-200, timeout, or connection error (the wedge signature:
                  listener open, requests stall/time out — see the bug doc)."""
    start = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            elapsed = time.monotonic() - start
            code = resp.status
    except (urllib.error.URLError, socket.timeout, OSError, ValueError) as exc:
        elapsed = time.monotonic() - start
        return "STALLED", f"request failed after {elapsed:.1f}s: {exc!r}"
    if code != 200:
        return "STALLED", f"HTTP {code} in {elapsed:.2f}s"
    return "HEALTHY", f"HTTP 200 in {elapsed * 1000:.0f}ms"


def _service_active_enter_ts(unit: str = AGENT_MONITOR_UNIT_NAME) -> int | None:
    """Epoch seconds when the monitored unit last entered `active`, or None.

    Systemd's own clock is the source of truth deliberately (T-2045): it covers
    restarts this watchdog issued AND restarts issued outside it (manual
    `systemctl --user restart`, a deploy) — the same two-source problem
    channel_liveness_monitor._in_restart_window documents, solved without a
    second state stamp that could go stale. None = cannot tell (unit missing,
    systemd unreachable), and callers must treat None as NOT-in-grace: an
    unreadable clock must not suppress evidence (fail-open for a DETECTOR).
    """
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        r = subprocess.run(
            ["systemctl", "--user", "show", unit, "--property=ActiveEnterTimestamp"],
            capture_output=True, text=True, timeout=10, env=env, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    # "ActiveEnterTimestamp=Tue 2026-07-29 04:12:03 UTC" (empty value if never active)
    val = r.stdout.partition("=")[2].strip()
    if not val:
        return None
    try:
        return int(time.mktime(time.strptime(val, "%a %Y-%m-%d %H:%M:%S %Z")))
    except ValueError:
        return None


# T-2045: post-boot grace window. The dashboard's own startup runs an
# 'upstream update available' git-remote check ~8s after boot that can
# transiently stall the HTTP surface; a probe landing in that window must not
# CONTRIBUTE EVIDENCE toward the N-consecutive restart trigger. 30s default
# gives ~4x margin over the measured ~8s stall. Probes still run and still log
# inside the window — only the counting is suppressed, so the observability
# surface is unchanged. Suppression is deliberately NOT gated behind
# REMEDIATE_ENABLED: the counter runs live unconditionally by design (soak
# fidelity — the unarmed counter must behave exactly as the armed one would),
# and the restart itself stays behind the arm-gate as before, so nothing is
# un-gated by this hardening.
BOOT_GRACE_SECONDS = int(os.environ.get("AGENT_MONITOR_BOOT_GRACE_SECONDS", "30"))


def _in_boot_grace(now: int, unit: str = AGENT_MONITOR_UNIT_NAME) -> bool:
    """True while the monitored unit is within BOOT_GRACE_SECONDS of its last
    activation. Fail-open: unknown start time = not in grace (see helper)."""
    started = _service_active_enter_ts(unit)
    if started is None:
        return False
    return 0 <= (now - started) < BOOT_GRACE_SECONDS


def _restart_service(unit: str = AGENT_MONITOR_UNIT_NAME, timeout: int = RESTART_TIMEOUT_SECONDS) -> tuple[bool, str]:
    """`systemctl --user restart <unit>`. XDG_RUNTIME_DIR is set explicitly
    (falls back to /run/user/<uid> if unset) matching the documented fix
    command's shape (bugs/agent-monitor-node-event-loop-wedge-long-uptime.md) —
    normally inherited from the systemd --user unit's own environment, but a
    wet-run/manual invocation may not have it set."""
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", unit],
            capture_output=True, text=True, timeout=timeout, env=env, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, repr(exc)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()
    return True, "restarted"


def _alert_operator(msg: str) -> None:
    from tools.inbox_consumer import _send_telegram_alert
    _send_telegram_alert(msg)


# Two distinct escalations, deliberately different severities (T-2084).
#
# The urgent one is a restart that FAILED to dispatch: remediation is broken and
# nothing is clearing the wedge. Until T-2084 that case alerted nobody at all —
# it logged and returned — while the RECUR case below wrongly claimed the
# restart was "NOT clearing" the wedge even though every restart had reported
# ok:true. Frequent-but-successful restarts are a degradation signal, not a
# remediation failure, and must not read as the same alarm.
_ESCALATE_FAIL_MSG = (
    "\U0001f6a8 [agent-monitor] the watchdog FAILED to restart agent-monitor.service — "
    "remediation is not working and the wedge is not being cleared. "
    "Restart error: {output}. Probe detail: {detail}. Operator action needed "
    "(T-1885; bugs/agent-monitor-node-event-loop-wedge-long-uptime.md)."
)

_ESCALATE_RECUR_MSG = (
    "⚠️ [agent-monitor] agent-monitor.service has wedged and been restarted "
    "{consecutive}x within {window}s. Each restart SUCCEEDED and the service is up — "
    "this is not a remediation failure, it is the wedge recurring faster than before "
    "(degradation signal). No action needed to keep it running. Probe detail: {detail} "
    "(T-1885; bugs/agent-monitor-node-event-loop-wedge-long-uptime.md)."
)

# A failed restart writes no cooldown state (so the next poll retries promptly),
# which would otherwise alert on EVERY poll. Rate-limit to one per cooldown.
_FAIL_ALERT_COOLDOWN_SECONDS = RESTART_COOLDOWN_SECONDS


def check(*, dry_run: bool = False, now: int | None = None) -> str:
    """Probe once; on a confirmed (N-consecutive) stall, restart (cooldown-gated)
    and escalate on a repeat-within-window. Returns the probe status."""
    now = int(time.time()) if now is None else now
    status, detail = probe_healthcheck()
    _log(f"{status}: {detail}")

    state = _load_state()

    if status == "HEALTHY":
        if state.get("consecutive_stalls"):
            _log("recovered — resetting consecutive-stall counter")
        state["consecutive_stalls"] = 0
        _save_state(state)
        return status

    # T-2045 boot-grace: a stall observed within BOOT_GRACE_SECONDS of the
    # monitored unit's own activation is the documented ~8s post-boot
    # git-remote-check transient, not evidence of the wedge — it must not
    # count toward the N-consecutive trigger (which is what makes a
    # false-positive restart of a FRESHLY-restarted dashboard structurally
    # impossible, rather than merely unlikely under the N=3 anti-flap).
    # Probe ran, log line lands; only the evidence-counting is suppressed.
    if _in_boot_grace(now):
        _log("stalled probe within boot-grace of unit activation — not counted "
             f"(grace={BOOT_GRACE_SECONDS}s)")
        return status

    consecutive_stalls = int(state.get("consecutive_stalls", 0)) + 1
    state["consecutive_stalls"] = consecutive_stalls
    _save_state(state)
    _log(f"stalled probe {consecutive_stalls}/{STALL_CONSECUTIVE_THRESHOLD}")

    if consecutive_stalls < STALL_CONSECUTIVE_THRESHOLD:
        return status

    if dry_run:
        _log("[dry-run] confirmed stall — would restart, not acting")
        return status

    if not REMEDIATE_ENABLED:
        _log("restart suppressed — remediation disarmed (PANTHEON_AGENT_MONITOR_REMEDIATE_ENABLED unset)")
        return status

    last_restart_ts = int(state.get("last_restart_ts", 0) or 0)
    if last_restart_ts and (now - last_restart_ts) < RESTART_COOLDOWN_SECONDS:
        _log("confirmed stall but in restart cooldown; not re-firing")
        return status

    ok, output = _restart_service()
    if not ok:
        _log(f"restart command FAILED: {output}")
        _append_restart_log({"ts": now, "detail": detail, "ok": False, "output": output})
        # No cooldown state written on a failed dispatch — the next poll should
        # retry rather than wait out the full cooldown. That retry cadence is
        # why the alert itself is rate-limited rather than fired every poll.
        last_fail_alert_ts = int(state.get("last_fail_alert_ts", 0) or 0)
        if not last_fail_alert_ts or (now - last_fail_alert_ts) >= _FAIL_ALERT_COOLDOWN_SECONDS:
            try:
                _alert_operator(_ESCALATE_FAIL_MSG.format(output=output, detail=detail))
                state["last_fail_alert_ts"] = now
                _save_state(state)
                _log("escalated to the operator (restart dispatch FAILED)")
            except Exception as exc:  # alert routing must never crash the poll
                _log(f"failure-escalation alert failed ({exc!r})")
        else:
            _log("restart failure already alerted within cooldown; not re-alerting")
        return status

    consecutive_restarts = 1
    window = RESTART_ESCALATE_WINDOW_MULT * RESTART_COOLDOWN_SECONDS
    if last_restart_ts and (now - last_restart_ts) < window:
        consecutive_restarts = int(state.get("consecutive_restarts", 0)) + 1

    state["last_restart_ts"] = now
    state["consecutive_restarts"] = consecutive_restarts
    state["consecutive_stalls"] = 0  # next probe re-confirms fresh
    _save_state(state)
    _log(f"restart issued (attempt #{consecutive_restarts}): {output}")
    _append_restart_log({"ts": now, "detail": detail, "ok": True, "consecutive_restarts": consecutive_restarts})

    if consecutive_restarts >= 2:
        try:
            # Every restart in this path returned ok:true — the service came back
            # up. Report it as recurrence/degradation, NOT as a failed remediation
            # (the pre-T-2084 wording said "restart is NOT clearing the wedge",
            # which read as an outage and cost an operator look that wasn't needed).
            _alert_operator(_ESCALATE_RECUR_MSG.format(
                window=window, consecutive=consecutive_restarts, detail=detail,
            ))
            _log("escalated to the operator (wedge recurring; restarts succeeding)")
        except Exception as exc:  # alert routing must never crash the poll
            _log(f"recurrence alert failed ({exc!r})")

    return status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Agent-monitor liveness watchdog (T-1885) — restart on a confirmed HTTP stall."
    )
    ap.add_argument("--check", action="store_true", help="probe + restart-on-confirmed-stall (cron path)")
    ap.add_argument("--dry-run", action="store_true", help="probe + log, no restart, no alert")
    args = ap.parse_args(argv)

    if not (args.check or args.dry_run):
        print(
            "ERROR: refusing to run with no mode flag. Pass --check (cron path) "
            "or --dry-run to preview without restarting.",
            file=sys.stderr,
        )
        return 64

    check(dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
