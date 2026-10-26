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
  NEAR_CLIFF  — refreshTokenExpiresAt within the warn window (default 3d) or already
                past (T-2973): the non-rotating ~30-day refresh token is about to/has
                lapsed → PRE-EMPTIVE re-login needed before the next renew zeroes it
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
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:  # ORG-076 §7 / T-2978: the unified per-persona mute registry.
    from tools.notify_policy import is_muted as _persona_is_muted
except ImportError:  # bare run with cwd=tools/
    from notify_policy import is_muted as _persona_is_muted  # type: ignore

try:  # T-4096: the mailbox hub path comes from instance.yaml, never a literal.
    from tools.agent_role_map import mailbox_hub
except ImportError:  # bare run with cwd=tools/
    from agent_role_map import mailbox_hub  # type: ignore

# Accounts that authenticate with a Claude OAuth credential file (agent -> unix
# user). Deliberately INDEPENDENT of context_fill_monitor.AGENT_CONFIG: that set
# carries restart/session semantics, this one only credential health. the-ancestor
# was listed here until T-3421 (2026-10-24, the operator TG 8778): it is retired and runs
# no agent, so its leftover credential file only produced NEAR_CLIFF pages once its
# mute was cleared. codex is intentionally EXCLUDED — it
# authenticates via a separate (OpenAI) system, not a Claude OAuth credential file.
CREDENTIALED_ACCOUNTS = {
    "orchestrator": "claude",
    "specialist": "claude-research",
    "recovery": "claude-fallback",
    # Archivist (second head, ORG-042) — Claude OAuth like the fleet; added
    # 2026-07-20 with the recovery→exocortex sudo grant (observation-only profile).
    "archivist": "exocortex",
    # Mason + Artisan (sub-orchestrator seats, ORG-043) — independent Claude OAuth
    # lineages from the 2026-07-21 batch (~mid-Aug refresh cliffs); added with
    # the recovery→suborch* sudo grants (/etc/sudoers.d/61-fallback-suborch, T-1904/
    # T-1905). Credential health only — their restart/wake semantics are orchestrator's
    # bringup surface, so they belong HERE, not in AGENT_CONFIG.
    "mason": "suborch1",
    "artisan": "suborch2",
}

# Tunable (env-overridable): re-alert cadence per (agent, problem class).
ALERT_COOLDOWN_SECONDS = int(os.environ.get("PANTHEON_CRED_ALERT_COOLDOWN_SECONDS", str(6 * 3600)))
# Agents whose credential ALERTS are suppressed — intentionally offline/retired seats
# (T-2973: archivist offline; the-ancestor was here too until T-3421 removed it). Their credentials will trip
# NEAR_CLIFF/MALFORMED forever with no one to relogin, so alerting the operator about them
# is pure noise (a D1 violation). The class is still CLASSIFIED + LOGGED (trace kept,
# so a resurrected seat is not silently blind — check the log), only the the operator page
# + Recovery wake are suppressed, and the agent is not counted a problem. Comma-separated
# env (the channel-liveness analogue was removed under ORG-076 s7.1). REVERSIBLE: drop from the env
# when a seat comes back online.
CREDENTIAL_ALERT_SKIP = {a.strip() for a in os.environ.get("PANTHEON_CRED_ALERT_SKIP", "").split(",") if a.strip()}
# T-2973: the refresh token is NON-rotating and expires ~30d from login; when it
# lapses the next access-token renew hard-fails and Claude Code ZEROES the credential
# (the 2026-08-18 cascade — artisan/orchestrator/recovery/mason within hours, undetected because
# this monitor only watched the short-lived ACCESS token). Warn this many seconds
# before the refreshTokenExpiresAt cliff so the SOP's pre-emptive relogin arc fires in
# time. Default 3 days — enough runway to batch-relogin the whole cohort.
REFRESH_CLIFF_WARN_SECONDS = int(os.environ.get("PANTHEON_CRED_REFRESH_CLIFF_WARN_SECONDS", str(3 * 24 * 3600)))

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
    if not isinstance(oauth, dict):
        return "no accessToken field", None
    if not oauth.get("accessToken"):
        # Parseable-but-blanked (e.g. a rejected-refresh writeback, the 2026-07-20
        # orchestrator signature): still MALFORMED for classify(), but the dict is returned
        # so login-event tracking (T-1874) can fingerprint it as 'blanked' rather
        # than lumping it with unreadable files.
        return "no accessToken field", oauth
    return "ok", oauth


def _read_account_json(user: str, path: str) -> dict | None:
    """Parse a JSON file owned by `user` (Recovery direct, siblings via `sudo -n`).
    None on ANY failure: callers treat None as "unknown", never as a signal."""
    try:
        if user == getpass.getuser():
            raw = Path(path).read_text()
        else:
            res = subprocess.run(["sudo", "-n", "-u", user, "cat", path],
                                 capture_output=True, text=True, timeout=15)
            if res.returncode != 0:
                return None
            raw = res.stdout
        data = json.loads(raw)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _claude_hold(agent: str, cfg: dict) -> str | None:
    """T-4791: why this persona's Claude credential is unused, or None.

    Non-None only when BOTH hold: the operator HELD claude/* off the persona
    (runtime-controller overrides/holds.json, an explicit operator act) AND its
    recorded live runtime is another harness (active-runtime.json). Then the
    credential's cliff pages the operator every cooldown for a login nothing uses; when
    the operator returns the seat to claude the hold is removed and alerts resume,
    which is exactly when the /login is needed. Muting the persona instead would
    silence ALL its alerts. Any unreadable/unknown state returns None (alert as
    before): this can only ever skip a page on positive evidence."""
    root = f"/home/{cfg['sudo_user']}/.local/state/pantheon/personas/{agent}"
    holds = (_read_account_json(cfg["sudo_user"], f"{root}/overrides/holds.json") or {}).get("holds")
    held = sorted(k for k in holds if k.startswith("claude/")) if isinstance(holds, dict) else []
    if not held:
        return None
    runtime = (_read_account_json(cfg["sudo_user"], f"{root}/active-runtime.json") or {}).get("runtime")
    if not isinstance(runtime, str) or not runtime or runtime.startswith("claude/"):
        return None
    reason = str((holds.get(held[0]) or {}).get("reason", ""))[:120]
    return f"{', '.join(held)} held ({reason}); live runtime {runtime}"


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
    if oauth is None or status != "ok":
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

    # T-2973 — the ~30-day refresh-token cliff. Unlike the access token (short-lived,
    # routine silent renew — deliberately NOT an alert class per the note above), the
    # refresh token is non-rotating; once refreshTokenExpiresAt lapses the next renew
    # hard-fails and Claude Code zeroes the credential. This is the one file-readable
    # signal that PRE-warns the cliff, so it IS actionable/alertable. Absent field
    # (older login format) → fall through to OK, never a false cliff.
    rte_ms = oauth.get("refreshTokenExpiresAt")
    if isinstance(rte_ms, (int, float)):
        rte_left = rte_ms / 1000.0 - now
        if rte_left <= REFRESH_CLIFF_WARN_SECONDS:
            when = f"expired {_fmt_dur(-rte_left)} ago" if rte_left <= 0 else f"expires in {_fmt_dur(rte_left)}"
            return "NEAR_CLIFF", (
                f"refresh token {when} ({_fmt_ts(rte_ms / 1000.0)}) — the ~30-day cliff. A "
                f"PRE-EMPTIVE re-login is needed before it lapses, or the next access-token "
                f"renew hard-fails and the credential is zeroed (T-2973)."
            )

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


RECOVERY_INBOX = os.environ.get("PANTHEON_RECOVERY_INBOX") or f"{mailbox_hub()}/recovery"

# T-1874: fleet login/rotation event log — every credential-fingerprint change is
# recorded so the NEXT auth-death can be correlated against fleet login activity
# (leading hypothesis for the 2026-07-20 orchestrator death: per-account OAuth session-cap
# eviction — a new agent login kills the stalest agent's refresh token server-side).
LOGIN_EVENTS_PATH = STATE_DIR / "login_events.jsonl"


def _login_fingerprint(oauth: dict | None) -> str:
    """Stable, secret-free fingerprint of a credential's login identity: the
    refresh token's sha256 prefix + its expiry. Rotation or relogin changes it;
    a rejected-refresh writeback collapses it to 'blanked'."""
    if not isinstance(oauth, dict):
        return "unreadable"
    refresh = (oauth.get("refreshToken") or "").strip()
    if not refresh:
        return "blanked"
    digest = hashlib.sha256(refresh.encode()).hexdigest()[:12]
    return f"{digest}@{oauth.get('refreshTokenExpiresAt')}"


def _track_login_event(agent: str, cfg: dict, now: float, state: dict) -> None:
    """Record a login/rotation event when an agent's credential fingerprint
    changes between fires. Fail-soft; own read so classify()'s contract is
    untouched. Skips unreadable creds (no event; keeps last-known fp)."""
    try:
        _status, oauth = _read_credential(agent, cfg)
        if oauth is None:
            # Unreadable/unparseable file: no event; keep the last-known
            # fingerprint so a transient sudo hiccup doesn't fabricate churn.
            return
        fp = _login_fingerprint(oauth)
        key = f"fp:{agent}"
        old = state.get(key)
        if old == fp:
            return
        state[key] = fp
        event = {
            "ts": _fmt_ts(now), "agent": agent,
            "old_fp": old, "new_fp": fp, "baseline": old is None,
        }
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LOGIN_EVENTS_PATH.open("a") as fh:
            fh.write(json.dumps(event) + "\n")
        _log(agent, f"login-event: fingerprint {old or '(first observation)'} -> {fp}")
    except Exception as exc:
        _log(agent, f"login-event tracking failed (non-fatal): {exc!r}")


def _wake_recovery(agent: str, cls: str, detail: str) -> None:
    """Drop the auth-dead finding into Recovery's ORG-026 mailbox alongside the the operator
    page (T-1873): the operator still owns the browser tap, but Recovery woken within ~5min
    runs the relogin LEGWORK (drive /login, capture the OAuth URL, hand it over
    ready-to-tap) — the 2026-07-20 orchestrator outage sat 6h because the page alone got
    lost in the operator's day. Fail-soft like disk_usage_monitor._wake_recovery: a mailbox
    error must never break the alert path. Skipped when Recovery itself is the dead
    agent (it cannot act on its own wake; the the operator page is the whole story)."""
    if agent == "recovery":
        return
    try:
        from tools import mailbox
        mailbox.send(
            RECOVERY_INBOX,
            f"credential-{cls} on {agent}: {detail}\n"
            f"Action (recovery): run sop/remediation/agent-oauth-relogin.md legwork NOW — "
            f"confirm with credential_expiry_monitor --agent {agent} --dry-run, drive "
            f"/login on the agent's pane, capture the OAuth URL, send it to the operator "
            f"ready-to-tap, paste his returned code (check #STATE matches the live pane).",
            "recovery",
            subject=f"auth-dead: {agent} ({cls})",
            # T-2237(a): a credential is dead or dying and only Recovery can drive the
            # relogin. Sitting undrained IS the outage continuing.
            severity="urgent",
        )
        _log(agent, "recovery mailbox wake dropped")
    except Exception as exc:
        _log(agent, f"recovery mailbox wake failed (non-fatal): {exc!r}")


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
        if not dry_run:
            _track_login_event(agent, cfg, now, state)
        if cls == "OK":
            continue
        # Intentionally offline/retired seat (T-2973): classified + logged above so the
        # trace survives, but no alert/wake and not counted a problem — nobody is going
        # to relogin it, so paging the operator would be pure noise.
        # Suppress the operator page for an intentionally offline/retired persona, via
        # the unified mute registry (ORG-076 §7 / T-2978, the go-forward path). The
        # legacy per-monitor env skip (PANTHEON_CRED_ALERT_SKIP) was RETIRED at the
        # T-2987 go-live once the shared registry was provisioned + populated with the
        # offline seats; the CREDENTIAL_ALERT_SKIP clause is kept as a dormant, always-
        # empty escape hatch (re-set the env for an emergency skip without a code change).
        # is_muted fail-opens, so an absent/broken registry is a no-op (never silences).
        if agent in CREDENTIAL_ALERT_SKIP or _persona_is_muted(agent):
            _log(agent, f"alert suppressed (persona muted / skip-list: intentionally offline/retired) — {cls}")
            continue
        hold = _claude_hold(agent, cfg)
        if hold:
            _log(agent, f"alert suppressed (claude runtime held by operator: {hold}) — {cls}")
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
            _wake_recovery(agent, cls, detail)
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
