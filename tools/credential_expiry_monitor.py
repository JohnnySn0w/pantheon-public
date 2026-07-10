#!/usr/bin/env python3
"""Proactive OAuth-credential-expiry monitor for the fleet (T-1216 / Jun-2026 401 outage).

The 2026-06-20→22 outage: Specialist's `~/.claude/.credentials.json` carried an EMPTY
refresh token, so when its access token expired (Jun 20) it could never auto-renew
→ hard 401 → comms-dark. It hid for ~3 days because every existing liveness check
reads a 401-wedged agent as healthy:
  - session-liveness / Recovery's idle sweeps   → the PROCESS is up (it is);
  - channel_liveness_monitor                → the inbound poller is up (it is —
    the bun bridge keeps polling getUpdates even when the LLM behind it 401s);
  - NONE check "is the credential valid / can the agent authenticate."

This monitor closes that gap from the credential side. It is a PURE FILE READ — no
LLM, no Claude API call — so it keeps working during a full auth outage, and it
alerts the operator DIRECTLY via the Telegram BOT token (independent of Claude OAuth),
which still delivers when every agent is auth-dead.

Per agent it reads the credential and classifies (first match wins):
  MALFORMED   — file missing/unreadable/unparseable, or no accessToken
  NO_REFRESH  — refreshToken missing/empty (the exact Specialist failure — cannot
                auto-renew; a ticking time bomb that fires at access-token expiry)
  EXPIRED     — now >= expiresAt
  NEAR_EXPIRY — expiresAt - now <= warn-window (default 12h)
  OK          — otherwise

A credential problem is ALWAYS a "the operator must re-login" class (no agent can
self-remediate an expired OAuth), so alerts go straight to the operator (mirrors the
recovery-self carve-out in route_failure_alert), throttled per (agent, class) so the
hourly timer nags without spamming. Default-safe (T-0261): refuses a flagless run.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Accounts that authenticate with a Claude OAuth credential file (agent -> unix
# user). Deliberately INDEPENDENT of context_fill_monitor.AGENT_CONFIG: that set
# carries restart/session semantics and omits the-ancestor, but the-ancestor runs
# `claude` headless with its own ~/.claude/.credentials.json and can 401-expire
# exactly like the others (its access token had already expired by the time of the
# Jun-2026 outage; it survived only because its refresh token was intact). It is
# channel-less / on-demand with different restart semantics, so it belongs HERE for
# credential health but NOT in AGENT_CONFIG. codex is intentionally EXCLUDED — it
# authenticates via a separate (OpenAI) system, not a Claude OAuth credential file.
CREDENTIALED_ACCOUNTS = {
    "orchestrator": "claude",
    "specialist": "claude-research",
    "recovery": "claude-fallback",
    "the-ancestor": "the-ancestor",
}

# Tunable (env-overridable): re-alert cadence per (agent, problem class).
ALERT_COOLDOWN_SECONDS = int(os.environ.get("PANTHEON_CRED_ALERT_COOLDOWN_SECONDS", str(6 * 3600)))

STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
STATE_PATH = STATE_DIR / "credential_expiry_alert_state.json"


def _log(agent: str, msg: str) -> None:
    print(f"[credential_expiry_monitor agent={agent} ts={int(time.time())}] {msg}", flush=True)


def _cred_path_for(cfg: dict) -> str:
    return f"/home/{cfg['sudo_user']}/.claude/.credentials.json"


def _read_credential(agent: str, cfg: dict) -> tuple[str, dict | None]:
    """Read+parse an agent's credential via Recovery's cross-account sudo.

    Returns (status, oauth_dict). status is "ok" or a MALFORMED reason string.
    Recovery reads its own file directly (no self-sudo); siblings via `sudo -n`.
    """
    path = _cred_path_for(cfg)
    user = cfg["sudo_user"]
    me = getpass.getuser()
    try:
        if user == me:
            raw = Path(path).read_text()
        else:
            res = subprocess.run(
                ["sudo", "-n", "-u", user, "cat", path],
                capture_output=True, text=True, timeout=15,
            )
            if res.returncode != 0:
                return f"unreadable (rc={res.returncode}: {(res.stderr or '').strip()[:60]})", None
            raw = res.stdout
    except (OSError, subprocess.SubprocessError) as e:
        return f"read error: {e!r}", None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return f"unparseable JSON: {e}", None
    oauth = data.get("claudeAiOauth", data)
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return "no accessToken field", None
    return "ok", oauth


def classify(agent: str, cfg: dict, now: float | None = None) -> tuple[str, str]:
    """Return (severity_class, human_detail) for an agent's credential.

    severity_class ∈ {OK, NO_REFRESH, MALFORMED}.

    Design note — why access-token expiry is NOT an alert class: access tokens are
    short-lived (~8h) and rotate continuously; as long as a VALID refresh token is
    present, near/post expiry just triggers a routine silent auto-renew. Alerting on
    it would page the operator nightly. So the only file-readable, actionable signals are
    a MISSING refresh token (NO_REFRESH — the Specialist failure; guarantees a hard 401
    at the next access-token expiry) and a MALFORMED credential. A refresh token
    that is PRESENT but server-side-revoked cannot be detected from the file — that
    case is covered by the complementary auth-dead detector (the "/login" / 401
    pane-scrape), not here. The access-token TTL is still reported in the OK detail
    as a useful log signal.
    """
    now = time.time() if now is None else now
    status, oauth = _read_credential(agent, cfg)
    if oauth is None:
        return "MALFORMED", f"credential {status}"

    refresh = oauth.get("refreshToken") or ""
    if not refresh.strip():
        # Cannot auto-renew — fires a hard 401 at access-token expiry. This is the
        # exact Specialist failure; flag it regardless of remaining access-token life so
        # it is fixed BEFORE the 401 rather than discovered days after.
        return "NO_REFRESH", "refreshToken is missing/empty — cannot auto-renew; will hard-401 at access-token expiry"

    exp_ms = oauth.get("expiresAt")
    if not isinstance(exp_ms, (int, float)):
        return "MALFORMED", "expiresAt missing/non-numeric"
    exp = exp_ms / 1000.0
    secs_left = exp - now
    if secs_left <= 0:
        return "OK", f"refresh token present; access token expired {_fmt_dur(-secs_left)} ago — will auto-renew on next use"
    return "OK", f"refresh token present; access token valid for {_fmt_dur(secs_left)} ({_fmt_ts(exp)})"


def _fmt_dur(secs: float) -> str:
    secs = int(abs(secs))
    h, rem = divmod(secs, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h{m:02d}m"


def _fmt_ts(epoch: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        print(f"[credential_expiry_monitor] could not persist state: {e}", file=sys.stderr)


def _should_alert(agent: str, cls: str, now: float, state: dict) -> bool:
    """Throttle per (agent, class): re-alert only after the cooldown elapses."""
    key = f"{agent}:{cls}"
    last = state.get(key, 0)
    return (now - last) >= ALERT_COOLDOWN_SECONDS


def _alert(text: str) -> None:
    """Direct-to-operator Bot-API alert (a credential problem is always his to fix).

    Reuses inbox_consumer's direct send (bot token, independent of Claude OAuth, so
    it delivers even when every agent is auth-dead). Lazy import mirrors
    channel_liveness_monitor's use of inbox_consumer helpers.
    """
    from tools.inbox_consumer import _send_telegram_alert
    _send_telegram_alert(text)


def check(agents: list[str], *, dry_run: bool, now: float | None = None) -> int:
    """Check each agent; alert (throttled) on any non-OK class. Returns problem count."""
    now = time.time() if now is None else now
    state = _load_state()
    problems = 0
    for agent in agents:
        sudo_user = CREDENTIALED_ACCOUNTS.get(agent)
        if not sudo_user:
            _log(agent, "not a credentialed account; skipping")
            continue
        cfg = {"sudo_user": sudo_user}
        cls, detail = classify(agent, cfg, now=now)
        _log(agent, f"{cls}: {detail}")
        if cls == "OK":
            continue
        problems += 1
        if dry_run:
            _log(agent, f"[dry-run] would alert ({cls})")
            continue
        if _should_alert(agent, cls, now, state):
            _alert(
                f"🔑 [credential-{cls.lower()}] {agent}: {detail}\n"
                f"No agent can self-remediate an OAuth credential — re-login is needed. "
                f"Re-auth procedure: sop/remediation/agent-oauth-relogin.md "
                f"(drive /login on the agent's pane → code-paste with the operator)."
            )
            state[f"{agent}:{cls}"] = now
            _log(agent, f"alerted the operator ({cls})")
        else:
            _log(agent, f"alert suppressed (cooldown, last < {ALERT_COOLDOWN_SECONDS}s ago)")
    if not dry_run:
        _save_state(state)
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = p.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="check every credentialed account (cron path)")
    g.add_argument("--agent", choices=sorted(CREDENTIALED_ACCOUNTS), help="check one account")
    p.add_argument("--dry-run", action="store_true", help="classify + print, send no alert")
    args = p.parse_args(argv)

    if not (args.all or args.agent):
        print(
            "ERROR: refusing to run with no mode flag. Pass --all (cron path) or "
            "--agent <name>; add --dry-run to preview without alerting.",
            file=sys.stderr,
        )
        return 64

    agents = sorted(CREDENTIALED_ACCOUNTS) if args.all else [args.agent]
    problems = check(agents, dry_run=args.dry_run)
    # Exit 0 always: a monitor must not fail its own timer. Problem count is in the log.
    print(f"[credential_expiry_monitor] checked {len(agents)} agent(s); {problems} with credential issues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
