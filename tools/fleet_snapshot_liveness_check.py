#!/usr/bin/env python3
"""tools/fleet_snapshot_liveness_check.py — per-persona snapshot-timer liveness (T-3791).

WHY THIS EXISTS. Archivist's `exocortex-data-snapshot.timer` was silently
disabled+dead for ~49 days (T-3790): the operator's SOLE personal-data copy went
uncaptured while the app was live, and nothing surfaced it. Snapshot services
deliberately carry NO OnFailure ("a missed snapshot shows as a git log gap, not
an incident"), so a timer that STOPS is invisible to every process/liveness
check — the account is up, the agent is up, the snapshot just quietly ceased.
This monitor makes snapshot-timer death visible fleet-wide, mirroring the T-2798
clone-currency check one plane over (durability infra instead of clone drift).

RELATIONSHIP TO tools/memory_snapshot_coverage_monitor.py (T-2838). That monitor
already timer-health-checks the memory/HANDOFF snapshot timers
(pantheon-agent-state-snapshot on recovery/orchestrator/specialist/artisan/mason + archivist-state
on exocortex). It does NOT cover `exocortex-data-snapshot` — the exact timer from
the T-3790 incident. This check unifies ALL snapshot timers (adding exocortex-data)
under one fleet sweep AND adds a last-commit-recency ("snapshot not landing")
predicate the coverage monitor deliberately omits. The two are complementary;
this one is the fleet-wide roll-up with the recency leg.

⚠️ DESIGN NOTE — THE IDLE-SEAT CONFOUND and why recency is CHANGE-DRIVEN-GATED
(the hard-won T-2838 finding, resolved structurally). The snapshot scripts commit
only when their source actually changed (`git diff --cached --quiet` guards every
commit), so a HEALTHY timer on a QUIET seat legitimately produces no new commit and
its repo HEAD ages indefinitely. Measured live 2026-09-17 while building this:
artisan's agent-state HEAD was 5 weeks old and archivist's memory HEAD 7 weeks old —
BOTH with active+enabled timers (idle/dormant seats, not dead ones). So commit-age
alone CANNOT distinguish "idle" from "snapshot not landing". The resolution encoded
below (superseding an earlier "set generous thresholds / skip-list the idle seats"
framing):
  * Timer STATE (active+enabled) is the AUTHORITATIVE, idle-proof liveness signal.
    It is checked first + independently for EVERY target and directly catches the
    T-3790 "timer stopped" failure with zero idle-seat false-positive.
  * Commit-recency (STALE) is a valid "snapshot not landing" alert ONLY for
    CHANGE-DRIVEN repos — a source written continuously (exocortex-data: the operator's
    live app), where an old commit really is a not-landing signal. For idle-prone
    repos (memory/agent-state, commit-on-change) it is confounded, so those targets
    (change_driven=False) are monitored by the STATE signal ALONE; their commit-age
    is reported as informational context but NEVER classifies STALE.
  * WHY GATING BEATS threshold-tuning or a skip: a generous threshold merely blunts
    the signal, and a skip that silences an idle STALE would ALSO silence a real
    DISABLED death on the same target (the skip suppresses the whole target). Gating
    the recency leg on change_driven never masks a real timer death — the STATE
    leg is always live. The skip-list stays for GENUINE dispositioned carve-outs
    (e.g. an intentionally-stopped timer), not for idle-tuning.
  * A monitor whose broken state reads as HEALTHY is the anti-pattern (T-2798
    house style): every unreadable step that gates a verdict classifies UNKNOWN
    (which alerts), never a silent HEALTHY pass.

STATUS TAXONOMY (per target, first match wins):
  HEALTHY              — timer active+enabled AND (change_driven ? commit within
                         threshold, OR (T-3992) commit stale but the tracked source
                         is quiescent — nothing to snapshot : STATE-only pass —
                         commit-age informational).
  DISABLED_OR_INACTIVE — timer state fail (not active, or not enabled): the exact
                         T-3790 archivist case. The primary, idle-proof signal, for
                         EVERY target regardless of change_driven.
  STALE                — CHANGE-DRIVEN target only: timer OK, newest commit older than
                         its recency threshold, AND (T-3992) the tracked source WAS
                         written since that commit (or the source mtime is unverifiable
                         → fail-closed) — "the source changed but the snapshot isn't
                         landing". A stale commit over a QUIESCENT source (the exocortex
                         frozen-app case) is HEALTHY, not STALE. Never emitted for an
                         idle-prone target.
  UNKNOWN              — could not read the timer state OR the commit (repo missing,
                         sudo/bus failure, timeout). Alerted exactly like a failure —
                         an unverifiable durability guard is a silent-loss risk.
  SKIPPED              — a dispositioned-mitigation carve-out was verified active at
                         check time (skip-list; fail-open — expired disposition
                         re-alerts).

ALERTS land in orchestrator's own ORG-026 mailbox (env-overridable PANTHEON_ORCHESTRATOR_INBOX),
sent via tools/mailbox.py as sender "recovery", per-(target,class) throttled by a
cooldown state file. Read-only across the fleet: the only commands that ever reach
a seat are `systemctl --user is-active/is-enabled <timer>` and `git log -1`. Recovery
reads its own seat directly; siblings via NOPASSWD `sudo -n -u <account>` (recovery's
cross-account grants — no password, no root.env). Default-safe (T-0261): refuses a
flagless run. Always exits 0 — a monitor must not fail its own timer.

Intended cadence: HOURLY (like Recovery's other fleet monitors) — wiring is left to the
operator (a systemd --user timer; NOT created by this change).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import agent_role_map as arm  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# Env-overridable so tests never depend on a live mailbox hub path.
ORCHESTRATOR_INBOX = os.environ.get("PANTHEON_ORCHESTRATOR_INBOX", f"{arm.mailbox_hub()}/orchestrator")

# Global recency-threshold default (days). A per-target threshold in the registry
# wins over this; --threshold overrides every target uniformly. Env sets the
# fallback used where the registry leaves a target unspecified.
RECENCY_THRESHOLD_DAYS_DEFAULT = float(
    os.environ.get("PANTHEON_SNAPSHOT_RECENCY_THRESHOLD_DAYS", "3.0")
)

# Re-alert cadence per (target, class). 12h matches memory_snapshot_coverage_monitor:
# a snapshot rot is a DR-recoverability concern (not a live outage), so the hourly
# timer nags without spamming. Env-overridable.
ALERT_COOLDOWN_SECONDS = int(
    os.environ.get("PANTHEON_SNAPSHOT_LIVENESS_ALERT_COOLDOWN_SECONDS", str(12 * 3600))
)

STATE_DIR = Path.home() / ".local" / "state" / "pantheon"
STATE_PATH = STATE_DIR / "snapshot_liveness_alert_state.json"

# Skip-list: per-target dispositioned-mitigation carve-out (mirrors T-3425's
# fleet-clone-currency-skip-list). Fail-open toward ALERTING at every step (missing
# file / parse error / malformed entry / moved disposition ticket = zero skips
# applied) — a broken skip-list can never SUPPRESS an alert, only ever fail to add
# a carve-out. Keyed by (account, timer) because one account (exocortex) hosts two
# snapshot timers, so account alone is too coarse.
SKIP_LIST_PATH_DEFAULT = REPO_ROOT / "fleet-snapshot-liveness-skip-list.yaml"
SKIP_LIST_PATH = Path(
    os.environ.get("PANTHEON_SNAPSHOT_SKIP_LIST", str(SKIP_LIST_PATH_DEFAULT))
)
_TICKET_STATE_DIRS = ("inbox", "active", "done")

HEALTHY, DISABLED_OR_INACTIVE, STALE, UNKNOWN, SKIPPED = (
    "HEALTHY", "DISABLED_OR_INACTIVE", "STALE", "UNKNOWN", "SKIPPED",
)
_ALERT_CLASSES = (DISABLED_OR_INACTIVE, STALE, UNKNOWN)

# systemctl is-active / is-enabled print a state TOKEN to stdout even on a non-zero
# rc (inactive→rc3, disabled→rc1). "active" and one of these enabled-tokens mean
# the timer is scheduled + persistent; anything else is DISABLED_OR_INACTIVE. An
# EMPTY token means the user manager was unreachable (sudo/bus/timeout) → UNKNOWN,
# never a silent pass.
_ENABLED_OK_TOKENS = ("enabled", "enabled-runtime")


# ---------------------------------------------------------------------------
# Target registry. Each snapshot timer in scope → its commit repo + cadence +
# recency threshold. account→persona is DERIVED (arm.account_persona), never
# hand-carried, so the label tracks instance.yaml. Discovered + verified live
# 2026-09-17 (see the module docstring's design note for the commit-repo sources:
# tools/systemd/pantheon-agent-state-snapshot.sh's PANTHEON_AGENT_STATE_REPO
# default, and each exocortex service's `cd <repo> && git commit`).
#
# recency_threshold_days is per-target, cadence-derived (daily → ~3d slack) and is
# consulted ONLY for change_driven=True targets. The idle-prone repos (agent-state
# ×5, archivist-state) carry change_driven=False, so their commit-age is never a
# STALE classification — only informational — and no threshold tuning is needed to
# keep them quiet (see the design note: this is strictly better than a generous
# threshold or a skip, which would also mask a real DISABLED death).
#
# OUT OF SCOPE, verified 2026-09-17:
#   * pantheon-memory-snapshot-coverage.timer (recovery) — a coverage CHECKER (runs
#     memory_snapshot_coverage_monitor.py); commits nothing, so it is not a
#     snapshot to liven-check. It is itself one of the monitors on this plane.
#   * pantheon-ci-audit-tip-coverage.timer (claude) — likewise a checker, not a
#     snapshot committer.
# ---------------------------------------------------------------------------
_TARGET_REGISTRY: tuple[dict, ...] = (
    # pantheon-agent-state-snapshot: dedicated ~/.local/state/pantheon/
    # agent-state-history repo (memory dir + HANDOFF copies). Daily.
    {"account": "claude", "timer": "pantheon-agent-state-snapshot.timer",
     "commit_repo": "/home/claude/.local/state/pantheon/agent-state-history",
     "oncalendar": "daily", "recency_threshold_days": 3.0, "change_driven": False},
    {"account": "claude-research", "timer": "pantheon-agent-state-snapshot.timer",
     "commit_repo": "/home/claude-research/.local/state/pantheon/agent-state-history",
     "oncalendar": "daily", "recency_threshold_days": 3.0, "change_driven": False},
    {"account": "claude-fallback", "timer": "pantheon-agent-state-snapshot.timer",
     "commit_repo": "/home/claude-fallback/.local/state/pantheon/agent-state-history",
     "oncalendar": "daily", "recency_threshold_days": 3.0, "change_driven": False},
    {"account": "suborch1", "timer": "pantheon-agent-state-snapshot.timer",
     "commit_repo": "/home/suborch1/.local/state/pantheon/agent-state-history",
     "oncalendar": "daily", "recency_threshold_days": 3.0, "change_driven": False},
    {"account": "suborch2", "timer": "pantheon-agent-state-snapshot.timer",
     "commit_repo": "/home/suborch2/.local/state/pantheon/agent-state-history",
     "oncalendar": "daily", "recency_threshold_days": 3.0, "change_driven": False},
    # exocortex-data-snapshot (the T-3790 incident timer) was retired 2026-10-16 with
    # the exocortex app (the operator TG 815, archivist: "drop all related live services").
    # The app no longer writes exocortex-data, so there is nothing left to snapshot.
    # archivist-state-snapshot: commits archivist's session state IN PLACE. The
    # service commits TWO legs each fire — $HOME (`git add -u`, HANDOFF) and the
    # memory dir (`git add -A`). Commit-on-change + archivist is a dormant on-demand
    # seat → idle-prone, change_driven=False (STATE-only monitored; commit-age is
    # informational, never STALE). The MEMORY leg is the informational-age repo (the
    # cleaner signal; the $HOME leg's `-u` rarely restages), per the T-2838 choice.
    {"account": "exocortex", "timer": "archivist-state-snapshot.timer",
     "commit_repo": "/home/exocortex/.claude/projects/-home-exocortex/memory",
     "oncalendar": "hourly", "recency_threshold_days": 3.0, "change_driven": False},
)


@dataclass(frozen=True)
class SnapshotTarget:
    persona: str
    account: str
    timer: str
    commit_repo: str
    oncalendar: str
    recency_threshold_days: float
    # change_driven gates the commit-recency (STALE) leg. True = the source is
    # written continuously (exocortex-data: the operator's live app), so an old commit
    # IS a real "snapshot not landing" signal. False = commit-on-change + idle-prone
    # (memory/agent-state), so an old commit is CONFOUNDED with idleness and must
    # NOT classify STALE — those targets are monitored by the idle-proof STATE
    # signal alone (see the module docstring's design note).
    change_driven: bool

    @property
    def key(self) -> str:
        """Stable (account, timer) identity for skip-list + cooldown keying —
        one account can host multiple snapshot timers, so the account alone is
        not a unique target key."""
        return f"{self.account}:{self.timer}"


def discover_targets(*, threshold_override: float | None = None) -> list[SnapshotTarget]:
    """Every snapshot timer in scope, as SnapshotTargets. persona is DERIVED from
    the account via agent_role_map (never hand-listed), so the label tracks
    instance.yaml. `threshold_override` (the --threshold flag) replaces every
    target's per-target recency threshold uniformly when given."""
    targets = []
    for entry in _TARGET_REGISTRY:
        account = entry["account"]
        persona = arm.account_persona(account) or account
        threshold = (
            threshold_override if threshold_override is not None
            else float(entry.get("recency_threshold_days", RECENCY_THRESHOLD_DAYS_DEFAULT))
        )
        targets.append(SnapshotTarget(
            persona=persona,
            account=account,
            timer=entry["timer"],
            commit_repo=entry["commit_repo"],
            oncalendar=entry.get("oncalendar", "?"),
            recency_threshold_days=threshold,
            change_driven=bool(entry.get("change_driven", False)),
        ))
    return targets


def _systemctl_token(target: SnapshotTarget, verb: str, *, run=subprocess.run) -> str | None:
    """Run `systemctl --user <verb> <timer>` for one target and return the state
    TOKEN it prints to stdout (e.g. "active"/"inactive"; "enabled"/"disabled"),
    or None if the user manager was unreachable (sudo/bus/timeout — an empty
    token, or a subprocess failure). Own account direct; siblings via NOPASSWD
    `sudo -n -u <account>` with XDG_RUNTIME_DIR so the user-manager bus is
    reachable (recovery's cross-account grants — no password, no root.env)."""
    me = getpass.getuser()
    if target.account == me:
        argv = ["systemctl", "--user", verb, target.timer]
    else:
        inner = (
            f"XDG_RUNTIME_DIR=/run/user/$(id -u) "
            f"systemctl --user {verb} {target.timer}"
        )
        argv = ["sudo", "-n", "-u", target.account, "bash", "-c", inner]
    try:
        r = run(argv, capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return None
    token = (r.stdout or "").strip()
    # A reachable manager always prints a token for is-active/is-enabled even on
    # a non-zero rc; an empty token means the command could not run (sudo denied,
    # no bus, XDG unset) → unverifiable, not "inactive".
    return token or None


def _timer_state(target: SnapshotTarget, *, run=subprocess.run) -> tuple[str, tuple[str, str] | None]:
    """Read a target's timer state. Returns (verdict, (active_token, enabled_token)):
    verdict is "" on success (tokens read), else UNKNOWN (manager unreachable)."""
    active = _systemctl_token(target, "is-active", run=run)
    if active is None:
        return UNKNOWN, None
    enabled = _systemctl_token(target, "is-enabled", run=run)
    if enabled is None:
        return UNKNOWN, None
    return "", (active, enabled)


def _last_commit_epoch(target: SnapshotTarget, *, run=subprocess.run) -> tuple[str, int | None]:
    """`git -C <commit_repo> log -1 --format=%ct` for one target. Returns
    (verdict, epoch): "" + committer-epoch on success, else UNKNOWN (repo missing,
    no commits, or unreadable). Own account direct; siblings via NOPASSWD sudo
    (700 homes). Read-only — a `git log` never mutates a seat's repo."""
    me = getpass.getuser()
    argv = ["git", "-C", target.commit_repo, "log", "-1", "--format=%ct"]
    if target.account != me:
        argv = ["sudo", "-n", "-u", target.account] + argv
    try:
        r = run(argv, capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return UNKNOWN, None
    if r.returncode != 0:
        return UNKNOWN, None
    out = (r.stdout or "").strip()
    if not out.isdigit():
        return UNKNOWN, None
    return "", int(out)


def _newest_source_mtime(target: SnapshotTarget, *, run=subprocess.run) -> tuple[str, int | None]:
    """Newest mtime (epoch) among the commit repo's TRACKED files (excluding .git)
    — the "was the source actually written since the last snapshot?" signal
    (T-3992, archivist's quiescent-source logic). Returns ("", mtime) on success,
    else (UNKNOWN, None): repo missing, no tracked files, or unreadable. Own
    account direct; siblings via NOPASSWD sudo (700 homes). Read-only.

    Uses `git ls-files` (not `find`) so ONLY tracked files count — an untracked
    build artifact must not read as a source write. The snapshot pipeline's
    `git commit` only lands tracked content, so tracked-file mtime is the right
    "pending un-snapshotted change" signal to compare against the last commit."""
    me = getpass.getuser()
    inner = (
        f"cd {shlex.quote(target.commit_repo)} && "
        f'git ls-files -z | xargs -0 -r stat -c "%Y" 2>/dev/null | sort -rn | head -1'
    )
    if target.account == me:
        argv = ["bash", "-c", inner]
    else:
        argv = ["sudo", "-n", "-u", target.account, "bash", "-c", inner]
    try:
        r = run(argv, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return UNKNOWN, None
    if r.returncode != 0:
        return UNKNOWN, None
    out = (r.stdout or "").strip()
    if not out.isdigit():
        return UNKNOWN, None
    return "", int(out)


def check_target(target: SnapshotTarget, *, now: float | None = None,
                 state_run=subprocess.run, commit_run=subprocess.run,
                 source_run=subprocess.run) -> dict:
    """Classify one target.

    Order (T-3791 predicate, change_driven-gated recency):
      1. Read timer STATE. Unreadable → UNKNOWN (fail-closed). This applies to
         EVERY target — the idle-proof primary signal that catches the T-3790
         silent-death.
      2. State fail (not active, or not enabled) → DISABLED_OR_INACTIVE.
      3. STATE ok:
         * change_driven=True  → read commit; unreadable → UNKNOWN (recency
           unverifiable); older than threshold → STALE; else HEALTHY.
         * change_driven=False → HEALTHY (STATE-only monitored). The commit is
           read best-effort for an INFORMATIONAL age in `detail`; it NEVER
           classifies STALE and its unreadability never degrades the verdict —
           an idle-prone repo's commit-age is confounded with idleness, so it is
           not an alert signal (see the design note).
    Every unreadable step that DOES gate a verdict classifies UNKNOWN, never a
    silent HEALTHY pass."""
    now = time.time() if now is None else now
    base = {"persona": target.persona, "account": target.account,
            "timer": target.timer, "commit_repo": target.commit_repo,
            "key": target.key, "change_driven": target.change_driven}

    verdict, tokens = _timer_state(target, run=state_run)
    if verdict:
        return {**base, "verdict": UNKNOWN,
                "detail": f"could not read timer state for {target.timer} "
                          f"(user manager unreachable — sudo/bus/timeout)"}
    active, enabled = tokens
    if active != "active" or enabled not in _ENABLED_OK_TOKENS:
        return {**base, "verdict": DISABLED_OR_INACTIVE,
                "detail": f"timer {target.timer} is-active={active} is-enabled={enabled} "
                          f"— snapshots not scheduled (the T-3790 silent-death case)"}

    cverdict, epoch = _last_commit_epoch(target, run=commit_run)

    if not target.change_driven:
        # STATE-only monitored: the timer is the authoritative signal and it
        # passed → HEALTHY. Commit-age is informational, never a classification.
        if cverdict:
            age_note = "commit age unknown (repo unreadable/no commits) — informational only"
        else:
            age_note = f"newest commit {_fmt_age((now - epoch) / 86400.0)} old, informational"
        detail = (f"timer active+enabled; STATE-only monitored "
                  f"(idle-prone repo — commit-recency confounded, not an alert signal); {age_note}")
        out = {**base, "verdict": HEALTHY, "detail": detail}
        if not cverdict:
            out.update({"commit_epoch": epoch, "age_days": (now - epoch) / 86400.0})
        return out

    # change_driven → commit recency IS a real "snapshot not landing" signal.
    if cverdict:
        return {**base, "verdict": UNKNOWN,
                "detail": f"timer active+enabled but commit repo unreadable/no commits "
                          f"({target.commit_repo}) — recency unverifiable"}
    age_days = (now - epoch) / 86400.0
    if age_days > target.recency_threshold_days:
        # T-3992 (archivist's quiescent-source logic): an old commit on a
        # change-driven repo is only a real "snapshot not landing" break if the
        # SOURCE has actually been written since the last snapshot. A quiescent
        # source (e.g. the exocortex app FROZEN per the operator TG525) legitimately
        # produces no diff → the daily `git commit` finds nothing → commit-age
        # crosses the threshold though the pipeline is sound. Gate the STALE on the
        # newest tracked-source mtime: SUPPRESS to HEALTHY when the source has NOT
        # been written since the last commit (nothing to snapshot); keep STALE when
        # it HAS (source written, snapshot not landing). Source-mtime unreadable →
        # keep STALE (fail-closed: never downgrade a break without positive evidence
        # of quiescence — the "never mask a real snapshot break" invariant).
        sverdict, src_mtime = _newest_source_mtime(target, run=source_run)
        if not sverdict and src_mtime is not None and src_mtime <= epoch:
            return {**base, "verdict": HEALTHY, "commit_epoch": epoch,
                    "age_days": age_days, "source_mtime": src_mtime,
                    "detail": f"change-driven repo: newest commit {_fmt_age(age_days)} "
                              f"old (> {target.recency_threshold_days:g}d) BUT source "
                              f"quiescent (newest tracked write not past last snapshot) "
                              f"— nothing to snapshot, not broken (T-3992)"}
        stale_reason = (
            "source written since last snapshot but no commit followed"
            if (not sverdict and src_mtime is not None)
            else "source mtime unverifiable — STALE reported fail-closed"
        )
        return {**base, "verdict": STALE, "commit_epoch": epoch, "age_days": age_days,
                "source_mtime": src_mtime,
                "detail": f"change-driven repo: timer active+enabled but newest commit "
                          f"{_fmt_age(age_days)} old (> {target.recency_threshold_days:g}d "
                          f"threshold) — snapshot not landing ({stale_reason})"}
    return {**base, "verdict": HEALTHY, "commit_epoch": epoch, "age_days": age_days,
            "detail": f"change-driven repo: timer active+enabled, newest commit "
                      f"{_fmt_age(age_days)} old (<= {target.recency_threshold_days:g}d)"}


def _fmt_age(age_days: float) -> str:
    if age_days >= 1.0:
        return f"{age_days:.1f}d"
    hours = age_days * 24.0
    if hours >= 1.0:
        return f"{hours:.1f}h"
    return f"{age_days * 1440.0:.0f}m"


def sweep(targets: list[SnapshotTarget], *, now: float | None = None,
          state_run=subprocess.run, commit_run=subprocess.run,
          source_run=subprocess.run) -> list[dict]:
    return [check_target(t, now=now, state_run=state_run, commit_run=commit_run,
                         source_run=source_run)
            for t in targets]


def needs_alert(results: list[dict]) -> list[dict]:
    """DISABLED_OR_INACTIVE, STALE and UNKNOWN all need attention. HEALTHY and
    SKIPPED (a dispositioned carve-out verified active at check time) do not — an
    unverifiable target IS the silent-loss risk this monitor exists to catch, so
    UNKNOWN alerts (fail-closed toward loudness, T-2798 house style)."""
    return [r for r in results if r["verdict"] in _ALERT_CLASSES]


# ----------------------------------------------------------------- skip-list
def load_skip_list(path: Path | None = None) -> list[dict]:
    """Load the per-target dispositioned-mitigation skip-list. Fail-open toward
    ALERTING at every step: a missing/unreadable/unparseable file, a non-mapping
    root, or a non-list `skips` all yield [] (no skips applied → alert normally).
    A skip-list we cannot understand must never silently suppress an alert."""
    p = path or SKIP_LIST_PATH
    try:
        raw = p.read_text()
    except OSError:
        return []
    try:
        import yaml
        data = yaml.safe_load(raw) or {}
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    entries = data.get("skips")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def is_skip_active(entry: dict, *, repo_root: Path = REPO_ROOT) -> tuple[bool, str]:
    """Whether a skip entry's disposition still holds: its ticket must currently
    live in the expected state directory (tickets/<state>/<id>.md). Any other
    outcome — missing account/timer/ticket/state, invalid state, moved or missing
    ticket, malformed entry — is fail-open ("expired", so the target re-alerts).
    The reason string rides render_alert's SKIPPED footer for auditability."""
    account = entry.get("account")
    timer = entry.get("timer")
    ticket_id = entry.get("disposition_ticket")
    expected = entry.get("expected_state")
    reason = entry.get("reason", "(no reason given)")
    if not (account and timer and ticket_id and expected):
        return False, "malformed skip entry (account/timer/disposition_ticket/expected_state required); ignored"
    if expected not in _TICKET_STATE_DIRS:
        return False, f"skip for {account}:{timer}: expected_state '{expected}' not one of {_TICKET_STATE_DIRS}; ignored"
    actual = None
    for state in _TICKET_STATE_DIRS:
        if (repo_root / "tickets" / state / f"{ticket_id}.md").exists():
            actual = state
            break
    if actual is None:
        return False, f"skip for {account}:{timer} EXPIRED: disposition ticket {ticket_id} not found in tickets/{{inbox,active,done}}/"
    if actual != expected:
        return False, f"skip for {account}:{timer} EXPIRED: {ticket_id} now in tickets/{actual}/ (expected {expected}/)"
    return True, f"skip active per {ticket_id} (state={actual}): {reason}"


def apply_skip_list(results: list[dict], skip_list: list[dict],
                    *, repo_root: Path = REPO_ROOT) -> list[dict]:
    """Downgrade an alert-class verdict to SKIPPED for a target whose (account,
    timer) disposition-skip is currently active. Returns a NEW list; never mutates
    the input. HEALTHY is never suppressed (nothing to suppress). A SKIPPED result
    carries the original verdict under `original_verdict` and the reason under
    `skip_reason` for the dry-run/json output + the alert footer."""
    by_key: dict[str, dict] = {}
    for entry in skip_list:
        acct, timer = entry.get("account"), entry.get("timer")
        if isinstance(acct, str) and isinstance(timer, str):
            by_key[f"{acct}:{timer}"] = entry
    out: list[dict] = []
    for r in results:
        entry = by_key.get(r.get("key", ""))
        if entry is None or r["verdict"] not in _ALERT_CLASSES:
            out.append(r)
            continue
        active, reason = is_skip_active(entry, repo_root=repo_root)
        if active:
            out.append({**r, "verdict": SKIPPED, "original_verdict": r["verdict"],
                        "skip_reason": reason,
                        "detail": f"{r.get('detail', '')} — SKIPPED: {reason}"})
        else:
            out.append({**r, "detail": f"{r.get('detail', '')} — skip attempted but not applied: {reason}"})
    return out


# --------------------------------------------------------------------- alert
def render_alert(flagged: list[dict], *, skipped: list[dict] | None = None) -> str:
    lines = [f"Fleet snapshot-timer liveness: {len(flagged)} timer(s) need attention.", ""]
    for r in flagged:
        lines.append(f"- {r['persona']}/{r['account']} [{r['timer']}]: {r['verdict']} — {r['detail']}")
    lines += [
        "",
        "A dead/stale snapshot timer is a SILENT durability loss (T-3790: exocortex-data "
        "was dead ~49 days, uncaught). Remedy per target:",
        "  DISABLED_OR_INACTIVE → restart it: sudo -n -u <account> bash -c "
        "'XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user enable --now <timer>'",
        "  STALE → a CHANGE-DRIVEN repo (its source is written continuously) whose "
        "timer fires but whose snapshots stopped landing: confirm the timer is FIRING "
        "(list-timers) and its snapshot script is committing (run it by hand, check for "
        "an error).",
        "  UNKNOWN → the seat's systemd/commit-repo could not be read; verify the "
        "account + Recovery's sudo grant.",
    ]
    if skipped:
        lines += [
            "",
            f"Skipped (dispositioned-mitigation carve-outs still active): {len(skipped)} timer(s).",
        ]
        for r in skipped:
            lines.append(
                f"- {r['persona']}/{r['account']} [{r['timer']}]: "
                f"was {r.get('original_verdict', '?')} → SKIPPED. {r.get('skip_reason', '')}"
            )
    return "\n".join(lines)


def alert(flagged: list[dict], *, skipped: list[dict] | None = None) -> None:
    from tools import mailbox
    mailbox.send(
        ORCHESTRATOR_INBOX, render_alert(flagged, skipped=skipped), "recovery",
        subject=f"fleet snapshot-liveness: {len(flagged)} timer(s) need attention",
    )


# ----------------------------------------------------------- cooldown state
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
        print(f"[fleet_snapshot_liveness_check] could not persist state: {e}", file=sys.stderr)


def _cooldown_key(result: dict) -> str:
    return f"{result.get('key', result['account'])}:{result['verdict']}"


def _should_alert(result: dict, now: float, state: dict) -> bool:
    """Throttle per (target, class): re-alert only after the cooldown elapses."""
    last = state.get(_cooldown_key(result), 0)
    return (now - last) >= ALERT_COOLDOWN_SECONDS


# Diagnostic log stream. Defaults to stdout (systemd captures StandardOutput=journal);
# main() flips it to stderr under --json so stdout carries ONLY the JSON payload.
_LOG_STREAM = sys.stdout


def _log(msg: str) -> None:
    print(f"[fleet_snapshot_liveness_check ts={int(time.time())}] {msg}",
          file=_LOG_STREAM, flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--check", "--once", dest="check", action="store_true",
                    help="run one fleet sweep + alert on any failure (cron path)")
    p.add_argument("--dry-run", action="store_true", help="sweep + print, send no alert")
    p.add_argument("--threshold", type=float, default=None,
                    help="override every target's recency threshold (days); "
                         "default is the per-target registry value")
    p.add_argument("--skip-list", type=Path, default=SKIP_LIST_PATH,
                    help=f"per-target dispositioned-mitigation skip-list YAML (default {SKIP_LIST_PATH})")
    p.add_argument("--json", action="store_true", help="emit the sweep as JSON")
    args = p.parse_args(argv)

    if not (args.check or args.dry_run):
        print(
            "ERROR: refusing to run with no mode flag. Pass --check/--once (cron path) "
            "or --dry-run to preview without alerting.",
            file=sys.stderr,
        )
        return 64

    targets = discover_targets(threshold_override=args.threshold)
    now = time.time()
    raw_results = sweep(targets, now=now)
    skip_list = load_skip_list(args.skip_list)
    results = apply_skip_list(raw_results, skip_list)
    flagged = needs_alert(results)
    skipped = [r for r in results if r["verdict"] == SKIPPED]

    if args.json:
        global _LOG_STREAM
        _LOG_STREAM = sys.stderr  # keep stdout pure JSON; diagnostics to stderr
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            _log(f"[{r['verdict']:<20}] {r['persona']:10} {r['account']:15} {r['timer']:38} {r['detail']}")

    if not flagged:
        _log(f"all snapshot timers healthy; no alert ({len(skipped)} dispositioned-skipped)")
        return 0

    if args.dry_run:
        _log(f"[dry-run] would alert: {len(flagged)} timer(s) need attention "
             f"({len(skipped)} dispositioned-skipped)")
        return 0

    # Per-(target,class) cooldown filter: drop targets that already alerted for the
    # same class within the cooldown window, so an hourly timer nags without spam.
    state = _load_state()
    to_alert = [r for r in flagged if _should_alert(r, now, state)]
    if to_alert:
        alert(to_alert, skipped=skipped)
        for r in to_alert:
            state[_cooldown_key(r)] = now
        _save_state(state)
        _log(f"alerted orchestrator: {len(to_alert)} timer(s) need attention "
             f"({len(flagged) - len(to_alert)} within cooldown, {len(skipped)} dispositioned-skipped)")
    else:
        _log(f"{len(flagged)} timer(s) flagged but all within alert cooldown; no send "
             f"({len(skipped)} dispositioned-skipped)")
    # Exit 0 always: a monitor must not fail its own timer.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
