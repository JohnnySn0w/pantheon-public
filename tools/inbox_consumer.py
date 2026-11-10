#!/usr/bin/env python3
"""Pantheon inbox auto-consumer.

Each cycle (fired by systemd timer every 10 min):
  1. Bail if autonomy_off backup file exists.
  2. (HO only) Bail if cap-hit interlock is active.
  3. Bail if recent Telegram inbound (<5 min ago).
  4. Bail if agent has an in-flight auto_delivered ticket (dispatch mutex, T-0717).
  5. List inbox tickets for this agent; filter and sort.
  6. Take 1 (FIFO + lower-id tiebreak).
  7. Stamp the ticket with auto_delivered: true + auto_delivered_at (commit+push).
  8. Inject locked prompt via tmux send-keys.
  9. Log outcome.
  10. Track consecutive skips; alert on 6th.

Step 4 replaces the former per-turn pane sentinel (tmux capture-pane mid-turn
check). The mutex is state-derived: it reads ticket frontmatter, so it survives
session cutoff/reset/cap-hit-resume. The wedge-health-detection path
(ORCHESTRATOR/SPECIALIST/RECOVERY_HEALTH_PANE_WEDGE) is a SEPARATE mechanism kept intact.

Skip-reason vocab:
  off_switch / cap_hit / seat_maintenance / recent_telegram / dispatch_locked /
  stamp_write_failed / empty_inbox / dependency_blocked / interactive_required /
  not_before_unmet / tmux_unreachable / inject_pending_paste / mailbox_unreachable

Step 8's delivery is per-agent switchable (T-1828 R2, mirrors keep_going.py's
T-1827 R1 DELIVERY_MODE shape): "send-keys" (default, unchanged tmux inject) or
"mailbox" (routes the locked prompt through mailbox.send() instead — the
ORG-031 ACP intake pilot). Only specialist is flipped; see DELIVERY_MODE below. A
mailbox-send failure raises SkipCycle("mailbox_unreachable") — same alerting
class as tmux_unreachable, not a silent fallback to send-keys.
"""

from __future__ import annotations

import argparse
import enum
import json
import os
import re
import subprocess
import sys
import time
import getpass
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Locked inject prompt (verbatim — stored as module constant, not docstring).
# ---------------------------------------------------------------------------

INJECT_PROMPT = (
    "[inbox-auto-consumer] T-NNNN ('<title>') is in your inbox; target=<agent>."
    " Apply the autonomy bar from rulebook/policies/trust-posture.md \"What HO auto-approves\""
    " before acting on the body. If the ticket asks for anything outside"
    " auto-approve scope, surface to the operator via the Telegram bridge and halt"
    " rather than proceeding. Otherwise: claim via ticket.py, work the Ask,"
    " resolve when complete. Body follows: <body>."
)

# ---------------------------------------------------------------------------
# Paths and constants.
# ---------------------------------------------------------------------------

AUTONOMY_OFF_PATH = Path.home() / ".local" / "state" / "pantheon" / "autonomy_off"
CAP_HIT_PATH = Path.home() / ".local" / "state" / "pantheon" / "ho_limit_hit.json"
LOG_PATH = Path.home() / ".local" / "log" / "pantheon" / "inbox_consumer.log"
TELEGRAM_ENV = Path.home() / ".claude" / "channels" / "telegram" / ".env"
OPERATOR_CHAT_ID = "424242424"

# T-4205: the recent_telegram gate reads THIS account's own Claude Code sessions (the
# consumer runs as the agent's account). Replaces the decommissioned bridge HEALTH_URL.
CLAUDE_PROJECTS_ROOT = Path.home() / ".claude" / "projects"
RECENT_INBOUND_THRESHOLD_SECONDS = 300  # 5 min
CAP_HIT_RESUME_MARGIN_SECONDS = 90

CONSECUTIVE_SKIP_ALERT_THRESHOLD = 6

# Skip vocabs that represent NORMAL intentional-defer states, not stuck
# conditions. Excluded from the consecutive-skip alert so date-deferred
# tickets / empty inbox / dependency chains don't fire the alert every
# 60 min when there's nothing wrong.
#
# Remaining vocabs that DO trigger the alert (genuinely stuck / real problems):
#   off_switch / cap_hit / tmux_unreachable / inject_pending_paste /
#   stamp_write_failed (T-0717: persistent write failure warrants surfacing) /
#   mailbox_unreachable (T-1828 R2: a persistent mailbox-send failure for the
#   "mailbox" DELIVERY_MODE agents is the same class of real infra problem as
#   tmux_unreachable — deliberately NOT added here)
#
# mid_turn is no longer emitted from main() (T-0717: dispatch-gating pane
# scrape replaced by auto_delivered mutex; check_mid_turn removed).
NEVER_ALERT_SKIP_VOCABS: frozenset[str] = frozenset({
    "recent_telegram",          # user is active; alert would be noise
    "empty_inbox",              # no work; idle is fine
    "dependency_blocked",       # tickets blocked on dep; intentional
    "interactive_required",     # tickets labeled interactive; intentional
    "not_before_unmet",         # date-deferred tickets; intentional (T-0219b)
    "appetite_overrun_blocked", # T-3440/ORG-089: substate:appetite-overrun; intentional structural gate
    "security_pending",         # T-5678/ORG-105: awaiting HO security-accept; intentional structural gate
    "pending_prompt_recovered", # T-5408: our stuck prompt was submitted with one Enter; healed
    "dispatch_locked",          # T-0717: auto_delivered mutex held — healthy, not stuck
    "concurrent_state_flip",    # T-3010: ticket state flipped between scan + deliver
    "seat_maintenance",         # T-4362: seat deliberately in maintenance; auto-expires (240m cap)
    "seat_paused",              # ORG-106 L2: paused-intake record; TTL-bounded, own pre-expiry notice
    "seat_sleep",               # ORG-106 L2: deep-sleep record; TTL-bounded
    # stamp_write_failed is intentionally NOT here — persistent write failure
    # is a real infra problem (disk full? permissions?) worth the 6-skip alert.
})

# T-0383: active-queue tail-surface state + prompt. Rate-limited to at most
# once per 24h per {agent, ticket_id} pair; state keyed as "{agent}:{tid}" in
# a JSON dict at ACTIVE_SURFACE_LOG_PATH.
ACTIVE_SURFACE_LOG_PATH = Path.home() / ".local" / "state" / "pantheon" / "active_surface_log.json"
ACTIVE_SURFACE_PROMPT = (
    "[inbox-auto-consumer] Reminder: active ticket T-NNNN ('<title>') has had no"
    " transition for >24h; target=<agent>. Check status — in progress,"
    " blocked-with-reason, or resolve. Body follows: <body>."
)
ACTIVE_SURFACE_COOLDOWN_SECONDS = 86400  # 24h per-ticket rate-limit (re-surface guard)
ACTIVE_STALENESS_SECONDS = 86400  # 24h min since last transition before surfacing

# T-0717: auto_delivered mutex heartbeat-timeout backstop.
# If a stamped unresolved ticket's liveness signal (max of transition-log ts
# and ticket-file mtime) shows no activity for this many seconds, the lock is
# released and the operator is alerted. Default 8h (28800s) — long enough for
# marathon tickets; shorter than the 24h active-surface staleness threshold.
#
# Liveness = max(transition-log ts, file mtime). The file-mtime arm catches
# tickets actively worked for hours with no state transitions. Residual risk:
# a truly long active ticket with ZERO ticket-file touches could still trip
# the backstop. v1 mitigation: operator-alert (human in loop) + auto-release.
# TODO: if false-positives observed, escalate to alert-first-hold (don't
# auto-release until a 2nd longer ceiling or operator confirm). Keep
# alert+release for v1 as specced by T-0717 approved design.
AUTO_DELIVERED_STALE_SECONDS: int = 28800  # 8 hours

ORCHESTRATOR_PANE_TARGET = "claude-telegram-bot:0"

# T-0541: per-agent pane-capture config, read by _pane_capture. Maps agent name →
# {pane_target, unix_user}. (T-4203 retired the bridge-gated health loop that
# also read last_alert_path / wedge_state_path / is_bridge_client / alert_only
# from here; the map keeps its name because mailbox_wake + verify_startup.sh
# cite it as the source their own copies mirror.)
#
# unix_user: the Unix account that owns the tmux session (and the XDG_RUNTIME_DIR
#   that holds it). Cross-account captures/dismissals use `sudo -n -u <unix_user>`.
#   Recovery (claude-fallback) has NOPASSWD sudo into both claude (Orchestrator) and
#   claude-research (Specialist) per /etc/sudoers.d/fallback-rescues-prime and
#   /etc/sudoers.d/50-claude-fallback-research.
#
# pane_target: tmux session:window.pane specifier. Specialist runs in
#   "claude-research-bot" (confirmed tmux ls 2026-05-22); append :0 like Orchestrator.
_PANTHEON_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"

_AGENT_HEALTH_CONFIG: dict[str, dict] = {
    "orchestrator": {
        "pane_target": ORCHESTRATOR_PANE_TARGET,
        "unix_user": "claude",
    },
    "specialist": {
        "pane_target": "claude-research-bot:0",
        "unix_user": "claude-research",
    },
    "recovery": {
        "pane_target": "claude-telegram-bot-fallback:0",
        "unix_user": "claude-fallback",
    },
    # T-1919: the ORG-043 sub-orchestrator seats (session names verified live
    # 2026-07-21: suborch1-main / suborch2-main). The seats' own consumer cycles
    # read only TMUX_TARGET_BY_AGENT, not this map.
    "mason": {
        "pane_target": "suborch1-main:0",
        "unix_user": "suborch1",
    },
    "artisan": {
        "pane_target": "suborch2-main:0",
        "unix_user": "suborch2",
    },
}

# T-1036: the T-0213 sync-alert plumbing (PANTHEON_CONFIG_SYNC_ALERT_PATH +
# SELF_HEALING_SYNC_ALERT_CLASSES + check_pantheon_config_sync_alert) was retired
# here. T-0365 retired the 60s pantheon-config-sync timer (replaced by kino
# read-path pull-on-demand / syncGate), so nothing emits the sync-alert state
# file anymore. pantheon_config_sync.py itself is kept (kino's syncGate may use
# the sync tool); only the moot ALERT detection/routing is gone.

# Tmux targets: env-var-overridable; defaults are best-guess from RECOVERY.md.
# The env-var override is the operator-correct path — see rulebook/policies/autonomy.md §Disable.
TMUX_TARGET_BY_AGENT: dict[str, str] = {
    "orchestrator": os.environ.get("PANTHEON_HO_TMUX_TARGET", "claude-telegram-bot"),
    # T-0717: fixed from "claude-fallback-bot" → "claude-telegram-bot-fallback".
    # Root cause of Recovery tmux_unreachable false-positive: TMUX_TARGET_BY_AGENT
    # defaulted to "claude-fallback-bot" but Recovery's actual tmux session (per
    # _AGENT_HEALTH_CONFIG["recovery"]["pane_target"]) is "claude-telegram-bot-fallback".
    # tmux capture-pane against the wrong name fails (no session), producing
    # tmux_unreachable on every Recovery consumer cycle.
    "recovery": os.environ.get("PANTHEON_RECOVERY_TMUX_TARGET", "claude-telegram-bot-fallback"),
    "specialist": os.environ.get("PANTHEON_SPECIALIST_TMUX_TARGET", "claude-research-bot"),
    # T-1919: ORG-043 seats. BARE session names (no ":0" window suffix) — this
    # map feeds _resolve_tmux_target, whose value goes to send_keys_submit.sh;
    # the ":0"-suffixed form belongs in _AGENT_HEALTH_CONFIG["pane_target"].
    # Without these entries _resolve_tmux_target falls back to
    # f"claude-{agent}-bot" → "claude-mason-bot", which is not a live session,
    # so every cycle would inject nowhere and skip tmux_unreachable.
    # Verified live 2026-07-21: `tmux list-sessions` on suborch1 shows exactly
    # "suborch1-main".
    "mason": os.environ.get("PANTHEON_MASON_TMUX_TARGET", "suborch1-main"),
    "artisan": os.environ.get("PANTHEON_ARTISAN_TMUX_TARGET", "suborch2-main"),
    # T-3832 (2026-09-20, the operator TG 7231/10223): archivist consolidated to ONE
    # claude instance — her ACP bridge (which ran a redundant claude-code-acp 2nd
    # instance) is retired and she now receives ticket pokes by pane-inject like
    # every other claude-code seat. BARE session name (no ":0" suffix — this map
    # feeds send_keys_submit.sh; the ":0" form belongs in mailbox_wake.AGENT_PANE).
    # Verified live 2026-09-20: `tmux list-sessions` on exocortex shows
    # "claude-telegram-bot-exocortex". Without this entry _resolve_tmux_target
    # falls back to "claude-archivist-bot" (no such session) and every send-keys
    # cycle would inject nowhere / go tmux_unreachable.
    "archivist": os.environ.get("PANTHEON_ARCHIVIST_TMUX_TARGET", "claude-telegram-bot-exocortex"),
}

# Per-agent delivery mode for the step-8 ticket-poke (T-1828 R2). Same shape as
# tools/keep_going.py's DELIVERY_MODE (T-1827 R1): "send-keys" (default,
# byte-identical legacy pane-inject via inject()) or "mailbox" (routes the
# locked prompt through mailbox.send() instead — the ORG-031 ACP intake
# pilot). ONLY specialist is flipped; every other agent, and any agent absent from
# this map, fails open to "send-keys" via .get(agent, "send-keys") at the
# deliver() call site — never KeyError. Rollback = flip the entry back.
DELIVERY_MODE = {
    "orchestrator": "send-keys",
    # specialist: consolidated back to send-keys 2026-09-20 (T-3832, the operator TG 7231) —
    # the ACP second-claude was drift (ORG-031 intake was for non-claude harnesses;
    # specialist is claude + already has persona-consume@specialist @2min into its own
    # claude-research-bot:harness.0 pane). Removing the redundant subscription-billed
    # instance also closes the T-3827 cross-plane relay gap by construction.
    "specialist": "send-keys",
    "recovery": "send-keys",
    # archivist: send-keys since T-3832 (2026-09-20, the operator TG 7231). She WAS
    # "mailbox" (T-2146) when her intake was the ACP bridge (acp_bridge.py --agent
    # archivist), which ran a redundant 2nd claude (claude-code-acp). T-3832 retired
    # that bridge (the operator: "two claude instances is absurd"); her single
    # interactive claude-code seat now takes ticket pokes by pane-inject like every
    # other claude seat (TMUX_TARGET_BY_AGENT["archivist"] above), and her
    # inter-agent MAIL is woken by a standalone pantheon-mailbox-wake@archivist.timer
    # (2min) — the SAME mailbox_wake->main-seat path the ACP bridge used in its
    # --prefer-main-seat mode (T-3811), so her ORG-042 heartbeat topology is
    # preserved, just triggered by a timer instead of a poll-loop-plus-2nd-claude.
    #
    # The old "must NOT get a mailbox-wake timer / pane is the operator's own
    # conversation" warning is RETIRED: her pane (claude-telegram-bot-exocortex:0)
    # is her OWN claude-code work session (verified by capture 2026-09-20; the operator
    # confirmed "no live chat there"), so a send-keys kick lands in her agent loop,
    # not in the operator's chat — identical to recovery's own shape. No _AGENT_HEALTH_CONFIG
    # entry is needed: that map is Recovery's health-monitor surface (orchestrator/specialist/recovery/
    # mason/artisan) and every access is .get()-soft; the send-keys ticket path reads
    # TMUX_TARGET_BY_AGENT, not _AGENT_HEALTH_CONFIG. Rollback = flip back to
    # "mailbox" + restart pantheon-acp-bridge@archivist.service.
    "archivist": "send-keys",
    # T-2144 (2026-07-28): codex, same PURE-ACP shape as archivist. It runs its
    # own supervisor + sandbox (codex-daemon.service, codex-config MAILBOX.md),
    # NOT a claude TUI pane, so a send-keys inject has nothing to inject into.
    #
    # The gate named in the old comment here — "which account runs its consumer
    # unit (T-2142)" — is RESOLVED, verified rather than assumed: codex's own
    # account carries a pantheon-config clone, working systemd user timers
    # (pantheon-mailbox-wake@codex.timer fires every 5min) and the
    # pantheon-unit-failure-alert@ OnFailure target. So the unit runs on codex's
    # account, exactly like every other agent's.
    #
    # Mailbox delivery IS the wake for codex: unlike archivist it DOES have a
    # pantheon-mailbox-wake@codex timer, which is what turns a mailbox poke into
    # an actual nudge. No _AGENT_HEALTH_CONFIG entry, for the same reason given
    # above — the pane paths .get() to None and the bracket access is
    # unreachable in mailbox mode.
    "codex": "mailbox",
    # mason + artisan were previously ABSENT from this map and fell through to the
    # .get(agent, "send-keys") default. Listing them explicitly changes no
    # behaviour — it makes the map total, so the coverage test below can tell
    # "deliberately send-keys" apart from "nobody ever considered this persona",
    # which is exactly the distinction that hid codex for its whole existence.
    #
    # send-keys is right for these two IF they are running: both are pane agents
    # by design.
    #
    # ⚠️ THE OLD NOTE HERE SAID "neither has a live session today" (measured
    # 2026-07-28) AND IT ROTTED WITHIN HOURS. Both sessions came up 2026-07-28
    # 22:55 and have run since — re-measured 2026-08-02, ~4d20h uptime, tmux
    # sessions suborch1-main / suborch2-main, both idle at a healthy prompt.
    # T-2232 still carries the stale framing ("dormant seats") and its option (c),
    # "mark both DELIVERY_EXEMPT as dormant-by-design", would have written that
    # falsehood into the classification. Do not act on the dormancy framing
    # without re-measuring first.
    #
    # A LIVENESS CLAIM IS NOT A FACT TO RECORD IN A COMMENT — it is a reading that
    # decays. What is durable is where to take it again:
    #     ps -u suborch1 -o pid,etime,cmd | grep claude
    #     sudo -n -u suborch1 tmux list-sessions      (XDG_RUNTIME_DIR=/run/user/$(id -u))
    # Deliberately NOT asserted by a test: these seats may legitimately go up or
    # down, so a test pinning either state would fail on a correct change. The
    # coverage test below asserts CLASSIFICATION, which is the part that should
    # not drift silently.
    #
    # Standing, and independent of liveness: artisan has NO inbox-consumer timer at
    # all — confirmed twice, once by per-account enumeration and once by its
    # absence from the TG 6694 pause manifest, which captured every other seat's.
    # mason HAS one, currently paused by that manifest. So a ticket filed at artisan
    # still goes nowhere, and that is now a LIVE seat with no delivery rather than
    # a dormant one. mason's consumer historically passed only because its queue
    # was empty (journal: "skip:empty_inbox") — never reaching the inject step.
    "mason": "send-keys",
    "artisan": "send-keys",
    # T-4884 B6: watchman is a codex TUI (codex-fallback), like codex: the send-keys
    # path here verifies against Claude Code sentinels, so ticket pokes go to its
    # mailbox, and the control-plane consume runtime (harness-aware submit) wakes it.
    "watchman": "mailbox",
}


# Personas that intentionally have NO ticket delivery, with the reason. Anything
# NOT here and NOT in DELIVERY_MODE is a silent gap — see test_inbox_consumer's
# delivery-coverage test, which exists because codex and archivist each went
# without ticket delivery for their whole existence and nothing said so (T-2144).
# The gap was invisible precisely because their MAILBOXES worked: agent-to-agent
# mail landed fine, only the ticket poke was missing.
DELIVERY_EXEMPT: dict[str, str] = {}

# MAILBOX_HUB (ORG-026 Path A hub) is resolved from instance.yaml via
# agent_role_map.mailbox_hub() — defined just below the bootstrap imports
# (T-4096), so an ORG-087 hub move is a manifest flip, not a code edit.

IDLE_SENTINELS: tuple[str, ...] = (
    "? for shortcuts",        # idle, no task list
    "ctrl+t to hide tasks",   # idle, task list visible (Recovery's case — T-0065)
    # Add further idle footers here if new TUI states are discovered empirically.
)

PENDING_PASTE_SENTINEL = "[Pasted text"
INJECT_ENTER_DELAY = 0.7  # seconds — allows TUI paste-mode to settle before Enter
# T-5011 PR-B: a hermes pane needs a longer settle (submit_verify.HERMES_SETTLE_DELAY_S,
# test-pinned); every other harness keeps INJECT_ENTER_DELAY.
HERMES_INJECT_ENTER_DELAY = 1.2


def _inject_enter_delay(harness: str) -> float:
    return HERMES_INJECT_ENTER_DELAY if harness == "hermes" else INJECT_ENTER_DELAY

# T-0538: shared helper — single source of truth for submit-verify tmux injection.
_SEND_KEYS_HELPER = Path(__file__).resolve().parent / "send_keys_submit.sh"

# ---------------------------------------------------------------------------
# Bootstrap: make `from tools.ticket import ...` work when invoked as a script.
# Same pattern as tools/ticket.py lines 40-41.
# ---------------------------------------------------------------------------

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import _security_gate  # noqa: E402  (T-5678)
from tools.ticket import TICKETS_ROOT, parse_ticket, ticket_holder  # noqa: E402
from tools.agent_role_map import (  # noqa: E402
    resolve_role,
    resolve_persona,
    functional_roles,
    role_members,
    resolve_instance_role,
    PersonaRoleMembershipError,
    mailbox_hub,
)
from tools._git_utils import REPO_ROOT, pull_repo_or_warn  # noqa: E402
from tools.repo_registry import load_repo_registry  # noqa: E402  (T-5342)
from tools import live_harness  # noqa: E402  (T-4792)
from tools.notify_policy import is_muted as _persona_is_muted  # noqa: E402  (ORG-076 §7 / T-2978)
from tools import seat_state as _seat_state  # noqa: E402  (ORG-106 L2 / T-5525: the one seat-state reader)
from tools.kick_verify import _spinner_above_composer  # noqa: E402  (T-4193 spinner rule, T-4284)
from tools.inject_sanitize import neutralize_at_mentions  # noqa: E402  (T-4382 @path residual)

# ORG-026 Path A hub, from instance.yaml (T-4096). Same value keep_going.py and
# mailbox_wake.py resolve — one canonical source, no hand-carried duplicate.
MAILBOX_HUB = Path(mailbox_hub())

# Dual-identity-safe import (mirrors tools/keep_going.py's own mailbox import,
# T-1827 R1): `python -m unittest discover tools` (package identity
# tools.mailbox) and a bare run from inside tools/ both resolve to ONE module
# object.
try:
    from tools import mailbox  # noqa: E402
except ImportError:  # bare run with cwd=tools/
    import mailbox  # type: ignore  # noqa: E402


# ---------------------------------------------------------------------------
# ORG-088 rev-2: filter-at-pull-time addressed-to-seat predicate (T-3789).
#
# A role:R ticket stays role-targeted on disk; each seat's inbox_consumer decides
# at PULL time whether a ticket addresses it. A ticket addresses seat <agent> when:
#   (a) target == <persona>            — personal target (unchanged)
#   (b) resolve_persona(target) == <agent> — audit-role/legacy-alias back-compat: any
#       token that canonicalizes to this seat's persona addresses it (D-8). This covers
#       the CURRENT forward audit-role (resolve_role(agent)) AND a RETIRED audit-role
#       still carried by historical tickets — after the operator's T-3807 roles rename, specialist's
#       audit-id is head_research_orchestrator but a legacy target=specialist ticket must
#       still route to specialist (D1, P4 audit-continuity; specialist is retained as a
#       PERSONA_ALIASES legacy input-alias that resolve_persona dealiases to specialist).
#   (c) target == role:<R> AND R ∈ functional_roles(<agent>)  — rev-2 membership
# This is a pure MEMBERSHIP filter — no domain narrowing (the DomainRegistry /
# domain-eligibility engine was deleted, the operator Call #1). The existing
# auto_delivered stamp mutex (ticket-identity-keyed) arbitrates exactly-once at
# CYCLE-BOUNDARY granularity — see check_dispatch_locked / _stamp_auto_delivered.
# ---------------------------------------------------------------------------


def _safe_functional_roles(agent: str) -> frozenset[str]:
    """The functional roles *agent* holds, or an empty set if L3 membership is
    unverifiable. Fail-SAFE: a PersonaRoleMembershipError (missing/stale/malformed
    source) yields NO roles, so role:<R> tickets are SKIPPED for this seat while
    personal (a) and legacy audit-role (b) targeting are unaffected — the same
    fail direction a READ-visibility surface must choose (agent_role_map ORG-088)."""
    try:
        return functional_roles(agent)
    except PersonaRoleMembershipError:
        return frozenset()


def _target_addresses_seat(
    target: object, agent: str, audit_role: str, functional: frozenset[str]
) -> bool:
    """True when a ticket's *target* addresses seat *agent* under ORG-088 rev-2.

    Pure membership test over three arms (see module header): (a) personal, (b) legacy
    audit-role back-compat, (c) role:<R> membership. *audit_role* and *functional* are
    hoisted per call-site (computed once per invocation, not per ticket)."""
    if target == agent or target == audit_role:
        return True  # (a) personal, (b) current audit-role fast-path (D-8 back-compat)
    if isinstance(target, str) and resolve_persona(target) == agent:
        return True  # (b) any audit-role / retired legacy-alias that canonicalizes to
        #             this seat's persona — e.g. specialist -> specialist (T-3807 D1)
    if isinstance(target, str) and target.startswith("role:"):
        return target[len("role:"):] in functional  # (c) rev-2 membership
    return False


# ---------------------------------------------------------------------------
# T-5342: repo-grant filter for role-pool tickets.
#
# A role:<R> ticket whose code lives in a repo this seat holds no usable repos.yaml
# grant for is left out of this seat's eligible list (skip, not reroute: a pool
# member that does hold the grant still sees it). "Usable" is deliberately simple:
#   - no grant at all                         -> blocked
#   - ro only, and the work needs a write     -> blocked
#   - otherwise                               -> eligible
# The work needs a write when an explicit `repos:` entry says `:rw`, or (no explicit
# access) when the ticket carries a build-ish label (_BUILD_LABELS). A ticket with a
# read-only work label (_READ_ONLY_WORK_LABELS) is never grant-skipped.
#
# The repo is resolved from the first source that names a repos.yaml repo:
# `repos:` field, then a `ratifiable_files` "<repo>:<path>" prefix (the exact file
# the work changes), then project_id (when it is itself a repo name), then labels,
# which count ONLY through
# _REPO_LABEL_ALIASES (bare repo-name labels are topic tags, not code locations).
# Unknown repo, a personally addressed ticket, or an unreadable registry delivers
# exactly as before.
#
# When NO member of the pool can take the ticket, each member mails the HO once
# (see _surface_grant_gap_once).
# ---------------------------------------------------------------------------

REPOS_REGISTRY_ROOT = REPO_ROOT  # tests patch this to a fixture root
_REPO_LABEL_ALIASES = {
    "pantheon-pwa": "pantheon-pwa", "pwa": "pantheon-pwa",
    "kino-pwa": "pantheon-pwa", "kino": "pantheon-pwa",
    "echo-prismatic": "echo-prismatic",
}
_BUILD_LABELS = frozenset({
    "feature", "bug", "build", "impl", "fix", "ui", "gui", "refactor", "adr",
    "implementation", "bugfix",
})
_READ_ONLY_WORK_LABELS = frozenset({
    "research", "design", "investigation", "investigate", "spike", "technical-spike",
    "discussion-session", "survey", "audit",
})
_GRANT_SURFACE_LOG_PATH_DEFAULT = Path.home() / ".local" / "state" / "pantheon" / "repo_grant_surface_log.json"
GRANT_SURFACE_LOG_PATH = _GRANT_SURFACE_LOG_PATH_DEFAULT


def _load_grants() -> dict[str, dict] | None:
    """{repo name: grants map} from repos.yaml via the shared loader, or None (with a
    WARNING on stderr) when the registry is missing or malformed: fail open."""
    registry = load_repo_registry(REPOS_REGISTRY_ROOT)
    repos = registry.get("repos") if isinstance(registry, dict) else None
    if not isinstance(repos, list):
        print(f"[inbox_consumer] WARNING: repos.yaml under {REPOS_REGISTRY_ROOT} missing or "
              f"malformed; repo-grant filter off, delivering as before (T-5342)",
              file=sys.stderr)
        return None
    return {
        e["name"]: (e.get("grants") if isinstance(e.get("grants"), dict) else {})
        for e in repos if isinstance(e, dict) and isinstance(e.get("name"), str)
    }


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def _ticket_repos(fields: dict, known: dict) -> list[tuple[str, str | None]]:
    """(repo, declared access or None) pairs from the first source naming a known repo."""
    declared = []
    for item in _as_list(fields.get("repos")):
        name, _sep, access = item.strip().partition(":")
        if name in known:
            declared.append((name, access if access in ("ro", "rw") else None))
    if declared:
        return declared
    found: list[tuple[str, str | None]] = []
    for item in _as_list(fields.get("ratifiable_files")):
        name = item.strip().partition(":")[0]
        if name in known and (name, None) not in found:
            found.append((name, None))
    if found:
        return found
    project = fields.get("project_id")
    if isinstance(project, str) and project in known:
        return [(project, None)]
    for label in _as_list(fields.get("labels")):
        name = _REPO_LABEL_ALIASES.get(label)
        if name in known and (name, None) not in found:
            found.append((name, None))
    return found


def _needs_rw(access: str | None, labels: list[str]) -> bool:
    return access == "rw" if access else bool(_BUILD_LABELS.intersection(labels))


def _repo_grant_block(persona: str, repos: list[tuple[str, str | None]],
                      grants: dict, labels: list[str]) -> str | None:
    """Why *persona* cannot take a ticket on *repos*, or None if it can."""
    role_key = resolve_instance_role(persona)
    for name, access in repos:
        grant = grants.get(name, {}).get(role_key)
        if grant not in ("ro", "rw"):
            return f"{role_key} has no repos.yaml grant for {name}"
        if _needs_rw(access, labels) and grant != "rw":
            return f"{role_key} holds only ro on {name} and the ticket needs a write"
    return None


def _surface_grant_gap_once(ticket_id: str, title: str, target: str,
                            repos: list[tuple[str, str | None]], labels: list[str]) -> None:
    """Mail the HO, at most once per ticket from this seat, that no pool member can
    take the ticket.

    Every pool member may send, each deduplicated by its own per-seat state file
    (recorded only after a send lands, so a failed send retries next cycle). The
    text is built from the ticket alone, never from the sending seat, so the
    copies are byte-identical and mailbox.send's pending dedupe collapses any still
    unconsumed; the total is bounded by the pool size either way. The ticket
    itself is not touched (no git write), so it stays in the inbox."""
    if _live_telegram_suppressed():
        return  # test harness: never mail the live HO
    path = GRANT_SURFACE_LOG_PATH
    if path == _GRANT_SURFACE_LOG_PATH_DEFAULT and _in_test_context():
        return  # a test that forgets to redirect state never touches seat state
    seen = _load_active_surface_log(path)
    if ticket_id in seen:
        return
    needs = ", ".join(f"{n} ({'rw' if _needs_rw(a, labels) else 'any grant'})"
                      for n, a in repos)
    msg = (f"[inbox-auto-consumer] {ticket_id} ('{title}') targets {target}, but no member "
           f"of that pool holds the repos.yaml grant it needs: {needs}. It stays in the "
           f"inbox undelivered; please retarget it (or widen the grant). Each pool seat "
           f"sends this at most once per ticket (T-5342).")
    try:
        mailbox.send(ORCHESTRATOR_MAILBOX, msg, "inbox_consumer",
                     subject=f"repo-grant gap ({ticket_id})", severity="normal")
    except Exception as exc:  # noqa: BLE001 — retry next cycle
        print(f"[inbox_consumer] repo-grant surface for {ticket_id} failed: {exc!r}",
              file=sys.stderr)
        return
    seen[ticket_id] = _now_iso()
    _save_active_surface_log(path, seen)


def _role_ticket_grant_skip(agent: str, target: object, fields: dict,
                            grants: dict | None, ticket_id: str, title: str,
                            pools: dict[str, list[str]]) -> str | None:
    """The reason *agent* should skip this role-pool ticket for lack of a repo grant,
    or None. Surfaces a ticket that nobody in the pool can take. *pools* caches
    role_members per target for one cycle."""
    if grants is None or not (isinstance(target, str) and target.startswith("role:")):
        return None
    labels = _as_list(fields.get("labels"))
    if _READ_ONLY_WORK_LABELS.intersection(labels):
        return None
    repos = _ticket_repos(fields, grants)
    if not repos:
        return None
    reason = _repo_grant_block(agent, repos, grants, labels)
    if reason is None:
        return None
    if target not in pools:
        try:
            pools[target] = sorted(role_members(target[len("role:"):]))
        except PersonaRoleMembershipError:
            pools[target] = []
    pool = pools[target]
    if pool and all(_repo_grant_block(p, repos, grants, labels) for p in pool):
        _surface_grant_gap_once(ticket_id, title, target, repos, labels)
    return reason


# ---------------------------------------------------------------------------
# Domain types.
# ---------------------------------------------------------------------------


class SkipCycle(Exception):
    def __init__(self, vocab: str) -> None:
        super().__init__(vocab)
        self.vocab = vocab


@dataclass
class TicketInfo:
    id: str
    title: str
    created_str: str
    depends_on: list[str]
    labels: list[str]
    body: str
    path: Path
    # T-1103 / ORG-034: arc-intent field. None when the ticket pre-dates ORG-034
    # or the filer omitted --serves. When present, format_prompt prepends it so
    # the worker sees the WHY before the body.
    serves: str | None = None
    # T-4695: set only by the in-flight scan (the dispatch mutex): whether the
    # ticket carries the consumer's auto_delivered stamp. Governs the stale
    # auto-release (stamped tickets only) and the operator log line.
    auto_delivered: bool = False


# ---------------------------------------------------------------------------
# Gating checks.
# ---------------------------------------------------------------------------


# T-2336: the stamp/clear git writes must serialize with every other lock-taking
# writer on this shared checkout (ticket.py, grooming, watch_runner). Imported at
# module level so tests can patch `tools.inbox_consumer.git_mutation_lock`.
try:
    from tools._git_utils import GitMutationLockTimeout, git_mutation_lock
except ImportError:  # bare run with cwd=tools/
    from _git_utils import GitMutationLockTimeout, git_mutation_lock  # type: ignore


def check_seat_intake(agent: str) -> None:
    """ORG-106 L2 (T-5525): skip the WHOLE cycle while any seat-state source blocks intake.

    One reader (tools/seat_state) replaces the two private checks that used to live here
    (autonomy_off file, T-4362 seat-maintenance). Raised vocab by source, so the existing
    log/alert vocab and fleet_health's expected-skip matching are unchanged:
      maintenance / oom-hold   -> seat_maintenance  (T-4362: before the pull, so a seat
                                  mid-move keeps its clone where the move left it)
      seat-state deep-sleep    -> seat_sleep
      seat-state paused-intake -> seat_paused
      autonomy_off only        -> off_switch  (not NEVER_ALERT: a forgotten off-switch
                                  still alerts after 6 skips, as before)
    The consumer runs as the seat account, so the reader reads the record directly and
    the legacy file is this account's AUTONOMY_OFF_PATH. Fail-open: any reader fault
    means do not skip (logged), so a broken source can never stop delivery."""
    try:
        e = _seat_state.effective_for(agent, autonomy_off_path=AUTONOMY_OFF_PATH)
    except Exception as exc:
        _log_seat_intake_fault(agent, f"{type(exc).__name__}: {exc}")
        return
    if e.fault:
        _log_seat_intake_fault(agent, e.fault)
    if not e.intake_blocked:
        return
    srcs = set(e.sources)
    if srcs & {_seat_state.SRC_MAINTENANCE, _seat_state.SRC_OOM}:
        raise SkipCycle("seat_maintenance")
    if _seat_state.SRC_SEAT_STATE in srcs:
        raise SkipCycle("seat_sleep" if e.state == _seat_state.STATE_DEEP_SLEEP else "seat_paused")
    raise SkipCycle("off_switch")


def run_seat_state_janitor(agent: str) -> None:
    """ORG-106 L5 (T-5529): expiry notices / tombstones / indefinite nag for this seat's
    own record. Notification only; ANY fault is logged and swallowed so it can never
    change the cycle outcome (correctness lives in the L0 reader, not here)."""
    try:
        from tools import seat_state_janitor
        seat_state_janitor.tick(agent)
    except Exception as exc:
        try:
            print(f"[inbox_consumer] {agent}: seat-state janitor fault (ignored): "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
        except Exception:
            pass


def _log_seat_intake_fault(agent: str, detail: str) -> None:
    try:
        print(f"[inbox_consumer] {agent}: seat-state reader fault (fail-open, not skipping): {detail}",
              file=sys.stderr)
    except Exception:
        pass


def check_cap_hit(agent: str) -> None:
    """HO-only: bail if cap-hit interlock is active. Mirrors ho_resumer.py logic."""
    if agent != "orchestrator":
        return
    if not CAP_HIT_PATH.exists():
        return
    try:
        state: dict[str, Any] = json.loads(CAP_HIT_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    resets_at = state.get("resets_at")
    if not isinstance(resets_at, (int, float)):
        return
    fire_at = int(resets_at) + CAP_HIT_RESUME_MARGIN_SECONDS
    if fire_at > int(time.time()):
        raise SkipCycle("cap_hit")


def check_recent_inbound(agent: str) -> None:
    """Bail if the operator messaged this agent on Telegram < RECENT_INBOUND_THRESHOLD_SECONDS ago.

    Don't hand the agent a ticket in the middle of a live conversation with the operator.
    T-4205: the last-inbound time comes from this account's own session jsonls
    (tools/telegram_inbound.py). The pantheon-bridge `last_inbound_at` it used to read
    was decommissioned on 2026-06-04, which silently fail-opened this gate for ~4 months
    (orchestrator had logged 363 recent_telegram skips before that, and 0 since). `agent` is
    kept for the call signature; the account running the consumer IS the agent.

    Fail-open: any probe error proceeds, since a delivery-deferral gate must never
    freeze delivery (see DECISIONS).
    """
    try:
        try:
            from tools import telegram_inbound
        except ImportError:  # bare run with cwd=tools/
            import telegram_inbound  # type: ignore
        ts = telegram_inbound.recent_inbound_ts(CLAUDE_PROJECTS_ROOT, RECENT_INBOUND_THRESHOLD_SECONDS)
    except Exception as e:  # noqa: BLE001 — fail-open by design
        print(f"[inbox_consumer] {agent}: recent_telegram probe failed (proceeding): {e}", file=sys.stderr)
        return
    if ts is not None and time.time() - ts < RECENT_INBOUND_THRESHOLD_SECONDS:
        raise SkipCycle("recent_telegram")


# check_mid_turn removed by T-0717: dispatch-gating pane scrape replaced by
# the auto_delivered-stamp mutex (check_dispatch_locked). The per-turn tmux
# sentinel was the only caller of this function in the main() cycle. (The
# separate wedge-health-detection path was itself retired later, in T-4203.)
# Per msg_2364 alpha-removal preference: orphaned code deleted outright.


# ---------------------------------------------------------------------------
# Ticket listing + filtering.
# ---------------------------------------------------------------------------


def _ticket_state_on_disk(ticket_id: str) -> str | None:
    """Return the state of a ticket by scanning the tickets/ tree."""
    tid_clean = ticket_id.strip()
    if not re.fullmatch(r"T-\d{4,}", tid_clean):  # T-3215
        return None
    fname = f"{tid_clean}.md"
    for state_dir in ("inbox", "active", "done"):
        candidate = TICKETS_ROOT / state_dir / fname
        if candidate.exists():
            return state_dir
    return None


def _dep_is_shipped(dep: str) -> bool:
    """ORG-089 completion predicate (T-3440 Scope B), bound to this module's
    TICKETS_ROOT.

    Shipped iff `dep` is in done/ AND not `substate: dropped`. A dropped dep is
    NOT satisfied (D-N / falsifier 2) — a dependant gated on it stays dep-blocked.

    Kept local (rather than importing ticket._dep_is_shipped) so it resolves
    against `inbox_consumer.TICKETS_ROOT` — the root the consumer's own
    `_ticket_state_on_disk` uses and the one tests patch. In production both
    globals are the same path (inbox_consumer imports TICKETS_ROOT from ticket).
    Fail-CLOSED on malformed id / unreadable file (never count as shipped).
    """
    if _ticket_state_on_disk(dep) != "done":
        return False
    path = TICKETS_ROOT / "done" / f"{dep.strip()}.md"
    try:
        t = parse_ticket(path)
    except Exception:
        return False
    return t.fields.get("substate") != "dropped"


def list_eligible_tickets(agent: str) -> list[TicketInfo]:
    """Return eligible inbox tickets for agent.

    Four-bucket partition emits distinct skip vocab per locked decision #11
    (extended by T-0061 to add not_before_unmet):
      empty_inbox          — no tickets targeted at this agent
      dependency_blocked   — tickets exist but blocked on unfinished deps
      interactive_required — tickets exist, unblocked, but all flagged not-auto-pullable
      not_before_unmet     — tickets exist, unblocked, not interactive, but all have
                             a not_before timestamp in the future

    Precedence: eligible > dep_blocked > interactive_locked > not_before_unmet.
    Returns a non-empty list only when at least one ticket is fully eligible.
    """
    inbox_dir = TICKETS_ROOT / "inbox"
    if not inbox_dir.exists():
        raise SkipCycle("empty_inbox")

    eligible: list[TicketInfo] = []
    dep_blocked: list[TicketInfo] = []
    interactive_locked: list[TicketInfo] = []
    not_before_unmet: list[TicketInfo] = []
    overrun_blocked: list[TicketInfo] = []  # T-3440 / ORG-089
    security_pending: list[TicketInfo] = []  # T-5678 / ORG-105

    found_any = False
    # ORG-088 rev-2 (T-3789): hoist per-site addressing inputs above the loop —
    # audit-role (branch b) and functional-role membership (branch c) are computed
    # once per invocation, not per ticket.
    audit_role = resolve_role(agent)
    functional = _safe_functional_roles(agent)
    grants = _load_grants()  # T-5342; None = fail open (deliver as before)
    pools: dict[str, list[str]] = {}  # T-5342: role_members per target, this cycle
    grant_skipped: list[str] = []
    for path in inbox_dir.iterdir():
        if not path.is_file() or not path.name.endswith(".md"):
            continue
        try:
            t = parse_ticket(path)
        except Exception:
            continue

        fields = t.fields
        if fields.get("state") != "inbox":
            continue
        target = fields.get("target")
        if not _target_addresses_seat(target, agent, audit_role, functional):
            continue
        # T-4695: an already-delivered (auto_delivered-stamped) ticket is not
        # re-deliverable. With the mutex now keyed on the stamped taker, a role:<R>
        # ticket stamped to seat A no longer locks fellow member B, so B's
        # eligibility scan must skip it or B would deliver it a second time (the
        # ORG-088 / T-3789 F7 exactly-once). Personal tickets are unchanged: the
        # mutex already stops the owner before this scan when the stamp is live.
        if str(fields.get("auto_delivered")).lower() == "true":
            continue
        # T-5049: this seat was offered the ticket, left it unclaimed, and the stale
        # release recorded that (auto_declined_by). Do not re-offer to the same seat;
        # other pool members stay eligible. `ticket.py retarget` clears the field.
        declined = fields.get("auto_declined_by") or []
        if isinstance(declined, str):
            declined = [declined]
        if agent in declined:
            continue
        # T-5342: a role-pool ticket on a repo this seat lacks the grant for.
        grant_reason = _role_ticket_grant_skip(agent, target, fields, grants,
                                               fields.get("id", path.stem), t.title, pools)
        if grant_reason:
            grant_skipped.append(f"{fields.get('id', path.stem)} ({grant_reason})")
            continue

        found_any = True
        labels = fields.get("labels") or []
        depends_on = fields.get("depends_on") or []

        raw_serves = fields.get("serves")
        serves_val = str(raw_serves).strip() if raw_serves is not None else None
        info = TicketInfo(
            id=fields.get("id", path.stem),
            title=t.title,
            created_str=fields.get("created", ""),
            depends_on=depends_on,
            labels=labels,
            body=t.body,
            path=path,
            serves=serves_val if serves_val else None,
        )

        if "interactive-required" in labels:
            interactive_locked.append(info)
            continue

        # T-3440 / ORG-089 §Decision.4 (Scope D): a ticket flagged
        # substate: appetite-overrun is structurally not-deliverable (the breaker
        # refuses claim/delivery/resolve until a re-decision command). Joins the
        # not_before/depends_on skip family.
        if fields.get("substate") == "appetite-overrun":
            overrun_blocked.append(info)
            continue

        # T-5678 / ORG-105 s4: a security-shaped filing awaits HO's security-accept.
        # Structurally not-deliverable until then; joins the same skip family.
        if _security_gate.is_pending(fields):
            security_pending.append(info)
            continue

        # T-3440 / ORG-089 Scope B: a dep counts as satisfied only when SHIPPED
        # (in done/ and not `substate: dropped`). A dropped dep keeps the
        # dependant dep-blocked (falsifier 2), same as a still-open dep.
        deps_done = all(_dep_is_shipped(dep) for dep in depends_on)
        if not deps_done:
            dep_blocked.append(info)
            continue

        nb_raw = fields.get("not_before")
        if nb_raw:
            try:
                nb_dt = datetime.fromisoformat(nb_raw.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) < nb_dt:
                    not_before_unmet.append(info)
                    continue
            except (ValueError, TypeError):
                pass  # malformed not_before treated as missing — fail-open (see DECISIONS)

        eligible.append(info)

    if grant_skipped:  # T-5342: one summary line per cycle
        print(f"[inbox_consumer] {agent}: repo-grant skip {len(grant_skipped)}: "
              + "; ".join(sorted(grant_skipped)), file=sys.stderr)
    if not found_any:
        raise SkipCycle("empty_inbox")
    if eligible:
        return eligible
    if dep_blocked:
        raise SkipCycle("dependency_blocked")
    if overrun_blocked:
        raise SkipCycle("appetite_overrun_blocked")
    if security_pending:
        raise SkipCycle("security_pending")
    if interactive_locked:
        raise SkipCycle("interactive_required")
    if not_before_unmet:
        raise SkipCycle("not_before_unmet")
    raise SkipCycle("empty_inbox")


def _grooming_stale_ts(body: str) -> str | None:
    """Return the timestamp string of the latest [grooming] stale entry, or None.

    Walks the transition log bottom-up. Returns the ISO ts of the last
    grooming-stale bullet IFF that's the latest entry overall (matching
    latest_entry_is_grooming_stale's contract). Returns None when the
    latest entry isn't grooming-stale, even if older stale entries exist —
    a newer non-stale entry indicates the ticket has been touched since.
    """
    from tools.ticket_grooming import (
        latest_entry_is_grooming_stale,
        _iter_transition_log_lines,
        _LOG_BULLET_RE,
        GROOMING_ACTOR,
    )
    if not latest_entry_is_grooming_stale(body):
        return None
    bullets = _iter_transition_log_lines(body)
    for line in reversed(bullets):
        m = _LOG_BULLET_RE.match(line)
        if not m:
            continue
        if m.group("actor") != GROOMING_ACTOR:
            continue
        return m.group("ts")
    return None


def _last_transition_ts(body: str) -> "datetime | None":
    """Return the timestamp of the most recent parseable transition-log entry.

    Walks the '## Transition log' section bottom-up (any actor) and returns
    the datetime of the first parseable bullet found.  Returns None when the
    section is absent, empty, or every bullet is unparseable.

    Used by surface_active_ticket to gate the '>24h no transition' claim:
    only surface when (now - _last_transition_ts) > ACTIVE_STALENESS_SECONDS.
    Skip-on-None — don't assert staleness we cannot prove.
    """
    from tools.ticket_grooming import _iter_transition_log_lines, _parse_log_entry

    bullets = _iter_transition_log_lines(body)
    for line in reversed(bullets):
        parsed = _parse_log_entry(line)
        if parsed is not None:
            ts, _actor = parsed
            return ts
    return None


# ---------------------------------------------------------------------------
# T-0717: auto_delivered-stamp dispatch-inflight mutex.
# ---------------------------------------------------------------------------


def _is_non_workable(fields: dict) -> bool:
    """True iff the ticket cannot currently be worked — so it cannot be an in-flight
    dispatch and must not hold the dispatch mutex. Four exclusion arms:

      1. dep-blocked    — an unfinished dependency in depends_on.
      2. calendar-gated — not_before is in the future.
      3. interactive-required — labelled interactive-required (parked/deferred); such a
         ticket is never auto-deliverable (the consumer routes it to interactive_locked,
         never auto-injects), so it can never be a live auto-delivered dispatch.
      4. awaiting-merge — substate: awaiting-merge (T-4119): the ticket's OWN work is
         done; it is parked on the HO-exclusive merge (T-1673), not on anything the
         agent is actively driving.

    Arms 1+2 mirror the dep_blocked / not_before_unmet predicates in
    list_eligible_tickets(). Used by the dispatch mutex (T-0864): an auto_delivered
    ticket that later gets blocked/dep-gated/parked must NOT keep holding the lock —
    a ticket that can't be worked is not in-flight.

    Regressions this guards: T-0768 (a dep-gated ticket held orchestrator's lock ~7.6h,
    suppressing all inbox delivery until a human noticed) and T-0981 (deferring the
    §F build tickets by marking interactive-required while a fresh auto_delivered
    stamp from a race delivery persisted → mutex held, all delivery suppressed until
    a hand-clear; the T-0768 incident class via the interactive-required path).
    """
    depends_on = fields.get("depends_on") or []
    # T-3440 / ORG-089 Scope B: a dropped dep is NOT shipped, so a ticket gated
    # on it is still non-workable (dep-blocked) — mirrors list_eligible_tickets.
    if not all(_dep_is_shipped(dep) for dep in depends_on):
        return True
    nb_raw = fields.get("not_before")
    if nb_raw:
        try:
            nb_dt = datetime.fromisoformat(nb_raw.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < nb_dt:
                return True
        except (ValueError, TypeError):
            pass  # malformed not_before treated as missing — fail-open (matches list_eligible_tickets)
    if "interactive-required" in (fields.get("labels") or []):
        return True
    # T-4119: a ticket parked `substate: awaiting-merge` must release the
    # per-agent dispatch mutex instead of holding it — mirrors the WIP-cap
    # exclusion in ticket.py's cmd_claim (same substate, same "cannot
    # currently be worked" predicate, on the auto-dispatch side rather than
    # the manual-claim side).
    if fields.get("substate") == "awaiting-merge":
        return True
    # T-5678: a security_gate: pending ticket cannot be worked (nor be in flight).
    if _security_gate.is_pending(fields):
        return True
    return False


def _seat_holds(fields: dict, agent: str, audit_role: str, functional: frozenset[str]) -> bool:
    """T-4695: does *agent* HOLD this ticket (so it counts toward the agent's busy-ness)?

    role:<R> target: bound to the stamped taker (`delivered_to`, via ticket.py's
    shared ticket_holder, the same predicate the WIP cap uses). LEGACY: a role
    ticket with no `delivered_to` that carries the auto_delivered stamp is held by
    EVERY member of R (conservative, pre-T-4695 behavior; clears on resolve). An
    unstamped, untaken pool ticket is held by nobody.
    Any other target: the ordinary addressing predicate."""
    target = fields.get("target")
    if isinstance(target, str) and target.startswith("role:"):
        holder = ticket_holder(fields)
        if holder:
            return holder == agent
        return (str(fields.get("auto_delivered")).lower() == "true"
                and _target_addresses_seat(target, agent, audit_role, functional))
    return _target_addresses_seat(target, agent, audit_role, functional)


def _list_inflight_for_agent(agent: str) -> list[TicketInfo]:
    """T-4695: the tickets that make *agent* BUSY (hold the dispatch mutex).

    Busy = holds ANY workable ticket: an active/ ticket (claimed or delivered), or
    an inbox/ ticket carrying the auto_delivered stamp (delivered, not yet claimed).
    Mirrors ticket.py's one-active WIP cap (T-0877) on the dispatch side, with the
    same non-workable exclusions (_is_non_workable). done/ is excluded."""
    audit_role = resolve_role(agent)
    functional = _safe_functional_roles(agent)  # ORG-088 rev-2 branch (c), T-3789
    results: list[TicketInfo] = []
    for state_dir_name in ("inbox", "active"):
        state_dir = TICKETS_ROOT / state_dir_name
        if not state_dir.exists():
            continue
        for path in state_dir.iterdir():
            if not path.is_file() or not path.name.endswith(".md"):
                continue
            try:
                t = parse_ticket(path)
            except Exception:
                continue
            fields = t.fields
            stamped = str(fields.get("auto_delivered")).lower() == "true"
            # inbox/ tickets hold the mutex only once delivered (stamped); active/
            # tickets hold it whether stamped or manually claimed.
            if state_dir_name == "inbox" and not stamped:
                continue
            # T-3789 LOAD-BEARING for exactly-once (F7): a stamped role:<R> ticket
            # must stay visible to the mutex (legacy: to every member; T-4695: to
            # its taker), or a second seat could deliver a ticket already stamped.
            if not _seat_holds(fields, agent, audit_role, functional):
                continue
            if _is_non_workable(fields):
                continue
            raw_serves = fields.get("serves")
            serves_str = str(raw_serves).strip() if raw_serves is not None else None
            results.append(TicketInfo(
                id=fields.get("id", path.stem),
                title=t.title,
                created_str=fields.get("created", ""),
                depends_on=fields.get("depends_on") or [],
                labels=fields.get("labels") or [],
                body=t.body,
                path=path,
                serves=serves_str if serves_str else None,
                auto_delivered=stamped,
            ))
    return results


def _list_auto_delivered_unresolved_for_agent(agent: str) -> list[TicketInfo]:
    """The auto_delivered-stamped subset of _list_inflight_for_agent (the pre-T-4695
    contract, kept for callers/tests that mean specifically stamped tickets)."""
    return [t for t in _list_inflight_for_agent(agent) if t.auto_delivered]


def _is_auto_delivered_stale(ticket: TicketInfo, threshold_seconds: int) -> bool:
    """Return True iff the ticket's liveness signal is older than threshold_seconds.

    Liveness = max of the available signals: the transition-log timestamp and the
    ticket-file mtime. Taking the max catches tickets actively worked for hours
    with no state transitions (the file gets touched on each agent edit even
    without a formal transition). When the transition log has no parseable ts, the
    file mtime alone is the signal (T-2971 fix — the old skip-on-None early return
    bailed here, wedging the dispatch mutex forever on a stale no-transition-log
    auto_delivered ticket: T-2374 held consultant's lock 19 days). Returns False (not
    stale) only when NEITHER signal is available (e.g. a missing file) — cannot
    prove staleness, so don't claim it.

    Residual risk: a ticket with zero file touches AND no transitions could trip
    the backstop on a genuinely long active ticket. v1 mitigation: operator-alert
    (human in loop). See AUTO_DELIVERED_STALE_SECONDS docstring for TODO.
    """
    now = datetime.now(timezone.utc)

    # Arm 1: transition-log timestamp.
    log_ts = _last_transition_ts(ticket.body)
    if log_ts is not None and log_ts.tzinfo is None:
        log_ts = log_ts.replace(tzinfo=timezone.utc)

    # Arm 2: file mtime (catches ticket-file touches without formal transitions,
    # AND is the sole liveness signal when the ticket has no parseable transition
    # log at all — the old skip-on-None early return bailed BEFORE reaching here,
    # which wedged the dispatch mutex forever on a stale no-transition-log
    # auto_delivered ticket: T-2374 held consultant's lock 19 days, T-2971).
    try:
        mtime_epoch = ticket.path.stat().st_mtime
        file_ts = datetime.fromtimestamp(mtime_epoch, tz=timezone.utc)
    except OSError:
        file_ts = None

    # Liveness = the most-recent available signal. Skip-on-None only when NEITHER
    # a transition-log ts NOR a file mtime is available (genuinely cannot prove).
    candidates = [t for t in (log_ts, file_ts) if t is not None]
    if not candidates:
        return False  # cannot prove staleness → don't claim it
    liveness_ts = max(candidates)
    return (now - liveness_ts).total_seconds() > threshold_seconds


def _recover_in_flight_prompt(agent: str) -> None:
    """T-5408: shared-helper call for the dispatch_locked branch. A never-alert skip has
    no streak, so a failed Enter pages once here (the record makes it once-only)."""
    try:
        tmux_target = _resolve_tmux_target(agent)
        harness = live_harness.resolve(agent, "claude-code")
        if recover_pending_prompt(agent, tmux_target, harness) == "failed":
            route_failure_alert(
                agent,
                f"[pantheon] inbox-consumer: {agent}'s in-flight ticket prompt is stuck unsubmitted "
                f"in its composer; one automatic Enter did not submit it and no further Enter "
                f"will be sent. Look at pane {tmux_target}. (T-5408)",
                vocab=f"pending-prompt-stuck ({agent})")
    except Exception as exc:  # noqa: BLE001 - recovery must never break the lock
        print(f"[inbox_consumer] {agent}: pending-prompt recovery error {exc!r} (T-5408)",
              file=sys.stderr)


def check_dispatch_locked(agent: str) -> None:
    """Bail with SkipCycle('dispatch_locked') if agent has an in-flight auto-delivered ticket.

    Implements the T-0717 auto_delivered-stamp mutex. Called in main() BEFORE
    list_eligible_tickets(), replacing the removed check_mid_turn() dispatch-gate:
      - Scan inbox/ + active/ for tickets targeting agent with auto_delivered: true.
      - If any found AND not stale: raise SkipCycle('dispatch_locked').
      - Heartbeat backstop: if any such ticket's liveness signal (max of
        transition-log ts + file mtime) is older than AUTO_DELIVERED_STALE_SECONDS,
        release the lock (do NOT bail) AND alert the operator via Telegram.

    Staleness check runs only when about to raise dispatch_locked (cheap + sufficient;
    approved by Orchestrator msg_2896-arc).

    Per-target isolation (multi-agent safety): each agent's consumer only considers
    target=<itself> tickets; orchestrator/specialist/recovery never contend and the lock has no
    cross-agent blast radius.
    """
    inflight = _list_inflight_for_agent(agent)
    if not inflight:
        return  # no lock — proceed

    # Check each in-flight ticket for staleness. T-4695: stale auto-release (and
    # its the operator alert) applies to auto_delivered-stamped tickets ONLY. A manually
    # claimed active ticket has no stamp to clear, so releasing it would re-alert
    # every cycle; it holds the lock until resolved/unclaimed/awaiting-merge, like
    # the WIP cap (T-0877) does.
    stale_tickets = []
    fresh_tickets = []
    for ticket in inflight:
        if ticket.auto_delivered and _is_auto_delivered_stale(ticket, AUTO_DELIVERED_STALE_SECONDS):
            stale_tickets.append(ticket)
        else:
            fresh_tickets.append(ticket)

    # Alert on stale tickets (lock release + the operator alert), then genuinely
    # release the lock by clearing the auto_delivered stamp on disk. Without the
    # clear, the stamp persisted and every subsequent cycle re-detected the same
    # stale ticket and re-alerted (~30-copy spam, msg_3073). Clearing makes the
    # alert idempotent: it fires once, on the cycle that first detects staleness.
    for ticket in stale_tickets:
        hours = AUTO_DELIVERED_STALE_SECONDS // 3600
        if ticket.path.parent.name == "inbox":
            # T-5049: offered, never claimed. Record the decline durably, log once,
            # no page: the the operator alert is for abandoned CLAIMED work only.
            print(
                f"[inbox_consumer] {agent} declined offered ticket {ticket.id}"
                f" (stamp stale >{hours}h, never claimed); recorded auto_declined_by,"
                f" not re-offering to this seat (retarget to re-offer; also fires if"
                f" the seat never saw it, e.g. down for {hours}h)",
                file=sys.stderr,
            )
            _clear_auto_delivered(ticket, decline_agent=agent)
            continue
        msg = (
            f"[inbox-consumer] auto_delivered ticket {ticket.id} ('{ticket.title}')"
            f" on agent={agent} is stale — no activity in >{hours}h."
            f" Lock released; agent may receive new work. Check ticket status:"
            f" abandoned, blocked, or session-reset without resumer?"
        )
        _send_telegram_alert(msg)
        _clear_auto_delivered(ticket)

    # If any fresh in-flight ticket remains, hold the lock.
    if fresh_tickets:
        # T-4695: make a non-stamped hold visible (one deduped line per cycle, log
        # only, no mail/page) so an operator can see WHY a seat gets no delivery.
        manual = sorted({t.id for t in fresh_tickets if not t.auto_delivered})
        if manual:
            print(
                f"[inbox_consumer] {agent}: dispatch_locked by active (non-auto-delivered)"
                f" ticket {', '.join(manual)}; no auto-delivery until it is resolved,"
                f" unclaimed or parked awaiting-merge",
                file=sys.stderr,
            )
        # T-5408: the 2026-10-22 shape. A false-confirmed delivery left the ticket
        # stamped in-flight, so every cycle ends HERE and deliver()'s guard never ran.
        # If the in-flight (stamped) prompt is what sits in the composer, take the same
        # guarded, once-only recovery Enter. Never raises; the lock is unchanged.
        if any(t.auto_delivered for t in fresh_tickets):
            _recover_in_flight_prompt(agent)
        locked = SkipCycle("dispatch_locked")
        # T-4695: held ONLY by manually claimed work (no stamped delivery in flight)
        # -> main() may still send the rate-limited aging-active reminder (T-0383).
        locked.active_only = not any(t.auto_delivered for t in fresh_tickets)
        raise locked


def _git_direct_writable(repo_root: Path) -> bool:
    """True when the running process can write `<repo_root>/.git` directly,
    without the `sg pantheon-shared` group-hop.

    T-3325 Defect 1: the group-hop used to be unconditional, so it fired even
    for a process that already owns its own repo (codex's own worktree —
    codex is not a pantheon-shared member by design, T-3324) and `sg` failed
    outright ("Invalid password for group pantheon-shared"). This probes the
    real, live permission bit via `os.access` (a kernel-checked, real-uid/gid
    read — not a cached `/etc/group` lookup), so it also correctly falls back
    to the sg-hop for T-0514's stale-supplementary-group case, where
    `/etc/group` lists the membership but the process's own live group set
    (fixed at process-creation time — e.g. an un-restarted `systemd --user`
    manager) predates the grant and a direct write genuinely fails.

    `.git/` (not the repo's top-level dir) is the actual write surface for
    `git add` / `git commit` — index, objects, refs, logs — so it's the
    precise thing to probe rather than a proxy for it.

    Coverage caveat (flagged at plan-review, not a defect): on a `git
    worktree` checkout, `<repo_root>/.git` is a FILE holding a `gitdir: ...`
    pointer, not the real git-dir — so this checks the pointer file's
    ownership/perms, not the pointed-at directory under
    `.../.git/worktrees/<name>/` where the actual writes land. In every
    known Pantheon deployment shape the two are co-owned (same uid created
    both at `git worktree add` time), so the pointer is a reliable proxy for
    the real surface — but it IS a proxy, not the surface itself, and a
    deployment that ever split that ownership would need a different check.
    """
    return os.access(repo_root / ".git", os.W_OK)


# T-3631: a transient .git/index.lock race — a concurrent git process on the
# agent clone (its own site commits, codex on its worktree, a human `git`) holds
# the index lock while this stamp commit runs — is a SELF-HEALING condition: the
# delivery already happened, and the mutex either lands on a bounded retry or is
# reconciled by a later cycle. It must NOT be reported as a hard provenance
# failure, because the caller OnFailure-pages Recovery on that. Mirrors _git_utils's
# fetch-side ref-lock retry (_REF_LOCK_MARKERS / _FETCH_REF_LOCK_*, T-3207).
_INDEX_LOCK_MARKERS = ("index.lock", "Another git process seems to be running")
_STAMP_INDEX_LOCK_ATTEMPTS = 3
_STAMP_INDEX_LOCK_BACKOFF_S = 0.4


class StampResult(enum.Enum):
    """Outcome of _stamp_auto_delivered (T-3631). Delivery has ALREADY happened
    by the time the stamp runs (T-2152 deliver-then-stamp), so these describe
    only whether the audit/provenance leg landed:

      OK             — frontmatter write + add + commit + push all succeeded.
      LOCK_CONTENDED — add/commit could not acquire .git/index.lock even after
                       bounded retries (a concurrent git process on the clone).
                       The local frontmatter write persisted, so THIS agent's own
                       next-cycle scan still sees the lock; the mutex just is not
                       on `origin` yet. SELF-HEALING — a later cycle reconciles —
                       so the caller exits 0 with a WARNING, NOT a hard page.
      TICKET_GONE    — the ticket file vanished before the stamp's read (T-3925):
                       it was claimed/resolved OUT of inbox/ in the same window as
                       this deliver-then-stamp cycle, so read_text() hit ENOENT.
                       Delivery already happened, and an unstamped ticket that has
                       LEFT inbox/ is no longer a pending inbox delivery — so the
                       stamp is MOOT, not a provenance gap. A benign race (sibling
                       to the concurrent_state_flip guard in main()), so the caller
                       exits 0 with a WARNING, NOT an OnFailure page.
      FAILED         — any other failure (a non-ENOENT local write error, a
                       non-lock add/commit error, push, or the pantheon
                       mutation-lock timeout): a genuine provenance gap for other
                       readers, so the caller surfaces it loudly (non-zero exit →
                       OnFailure), unchanged from the T-3325 fail-loud contract.
    """

    OK = "ok"
    LOCK_CONTENDED = "lock_contended"
    TICKET_GONE = "ticket_gone"
    FAILED = "failed"
    # T-4760: the ORG-021 frontmatter registry REFUSED the stamp's fields
    # (refuse mode only). Nothing was written. main() runs the same check
    # BEFORE delivering (_preflight_stamp_registry), so this is reachable only
    # if the registry changed between the preflight and the stamp; it is
    # logged as a registry refusal (ticket id + finding), not a generic FAILED.
    REGISTRY_REFUSED = "registry_refused"


def _is_index_lock_contention(stderr: str) -> bool:
    """True iff git stderr indicates a transient .git/index.lock race (T-3631)."""
    return any(marker in stderr for marker in _INDEX_LOCK_MARKERS)


def _run_git_stamp_step(cmd: list[str]) -> "subprocess.CompletedProcess[str]":
    """Run one stamp git step (add/commit) with bounded retry on .git/index.lock
    contention (T-3631). Returns rc==0 as soon as it succeeds; on index.lock
    contention it backs off and retries up to _STAMP_INDEX_LOCK_ATTEMPTS, giving
    the foreign git process time to release the lock. A non-lock non-zero result
    is returned immediately (no point retrying a real error). Returns the final
    CompletedProcess regardless."""
    last: "subprocess.CompletedProcess[str]" | None = None
    for attempt in range(_STAMP_INDEX_LOCK_ATTEMPTS):
        last = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if last.returncode == 0 or not _is_index_lock_contention(last.stderr):
            return last
        if attempt < _STAMP_INDEX_LOCK_ATTEMPTS - 1:
            time.sleep(_STAMP_INDEX_LOCK_BACKOFF_S)
    return last


def _apply_stamp_fields(fields: dict, agent: str | None) -> None:
    """The auto_delivered stamp's frontmatter mutation (shared by the T-4760
    preflight and the real stamp, so both check exactly the same fields)."""
    fields["auto_delivered"] = "true"
    fields["auto_delivered_at"] = _now_iso()
    if agent:
        fields["delivered_to"] = agent


def _preflight_stamp_registry(ticket: TicketInfo, agent: str | None = None) -> str | None:
    """T-4760: run the ORG-021 frontmatter-registry check for the stamp BEFORE
    delivery, writing nothing. Returns the refusal message when the registry
    (refuse mode) would refuse the stamp, else None.

    Why before delivery: deliver-then-stamp (T-2152) means a stamp refused
    AFTER the poke leaves a delivered-but-unstamped ticket that re-delivers
    every cycle. Refusing up front leaves no side effect at all. Any other
    error here (file gone, parse error, warn-mode findings) returns None: those
    keep their existing handling in _stamp_auto_delivered / the cycle."""
    from tools.ticket import (
        TicketRefusal, _check_frontmatter_registry, _fm_registry, _fm_snapshot,
        parse_front_matter,
    )
    # Only refuse mode can block; warn mode's WARN lines are printed once, by
    # the real stamp's build (no duplicate here).
    if _fm_registry is None or _fm_registry.get_mode() != "refuse":
        return None
    try:
        fields, _body = parse_front_matter(ticket.path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — not this preflight's concern (see docstring)
        return None
    old_fields = _fm_snapshot(fields)
    _apply_stamp_fields(fields, agent)
    try:
        _check_frontmatter_registry(fields, old_fields)
    except TicketRefusal as exc:
        return str(exc)
    return None


def _on_non_main_branch(repo_root: Path) -> bool:
    """True iff `repo_root` has a NAMED branch other than `main` checked out (T-5750).

    On such a clone, committing the stamp onto HEAD and pushing HEAD:main would
    carry every unrelated code commit on that branch to main, which the seat
    pre-push CODE-path guard correctly refuses. Those clones publish through
    tools/_git_publish (a main-based plumbing commit) instead. On `main`, or on
    a detached HEAD (the with_worktree cron shape), the classic add/commit/push
    path is already safe and is kept unchanged. Fail-closed to False (classic
    path) on any probe error.
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True, text=True, check=False, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    branch = (res.stdout or "").strip() if res.returncode == 0 else ""
    return bool(branch) and branch != "main"


def _publish_edit_via_main(ticket: TicketInfo, rel_path, mutate, message: str, direct: bool,
                           repo_root: Path):
    """Publish `mutate` of the ticket file to origin/main without pushing the
    checked-out branch (T-5750), under the same git_mutation_lock the classic path
    takes. Returns the PublishResult; raises GitMutationLockTimeout (or whatever
    `mutate` raises on origin's blob; callers classify it).

    On success, still under the lock, the local clone is reconciled to the
    published state in two steps:
      1. the EXACT published blob bytes are written to the working-tree file;
      2. a ticket-only commit with that content is made on the checked-out
         branch (pathspec-scoped to this one file; NEVER pushed).
    Step 2 is what keeps a later `git merge origin/main` / `git pull --rebase`
    clean: an unstaged modification blocks both ("commit your changes or stash
    them") even when byte-identical to main's blob, whereas a local commit makes
    the same change on both sides (merge is clean; rebase drops it as already
    applied). The commit also keeps this consumer's own local mutex read intact.

    The local commit is best-effort: origin already holds the mutex, so an
    add/commit failure (index.lock contention included) is logged and the
    publish result stays OK. On any non-OK publish the caller's earlier local
    write stands, uncommitted, as before."""
    from tools._git_publish import PublishKind, publish_file_edit_to_main
    with git_mutation_lock(label=f"publish {ticket.id} via origin/main"):
        result = publish_file_edit_to_main(
            repo_root, str(rel_path), mutate, message, use_sg=not direct,
        )
        if result.kind is PublishKind.OK and result.published is not None:
            _sync_local_to_published(ticket, rel_path, result.published, message, direct,
                                     repo_root)
    return result


def _sync_local_to_published(ticket: TicketInfo, rel_path, published: bytes, message: str,
                             direct: bool, repo_root: Path) -> None:
    """Write `published` to the local file and commit it on the current branch,
    file-scoped, unpushed. Never raises; failures are logged (see caller)."""
    try:
        ticket.path.write_bytes(published)
    except OSError as exc:
        print(f"[inbox_consumer] {ticket.id}: published, but could not sync the local"
              f" copy to the published blob: {exc}", file=sys.stderr)
        return

    def _cmd(*args: str) -> list[str]:
        if direct:
            return ["git", "-C", str(repo_root), *args]
        return ["sg", "pantheon-shared", "-c",
                f"git -C {repo_root} " + subprocess.list2cmdline(list(args))]

    try:
        # Nothing to commit when the branch already holds this exact content.
        if subprocess.run(_cmd("diff", "--quiet", "--", str(rel_path)),
                          capture_output=True, text=True, check=False).returncode == 0:
            return
        res = _run_git_stamp_step(_cmd("add", "--", str(rel_path)))
        if res.returncode == 0:
            res = _run_git_stamp_step(_cmd("commit", "-m", message, "--", str(rel_path)))
        if res.returncode != 0:
            print(f"[inbox_consumer] {ticket.id}: published to origin/main, but the local"
                  f" ticket-only commit failed (file synced, uncommitted; a later merge of"
                  f" main may need it committed or reset): {res.stderr}", file=sys.stderr)
    except Exception as exc:  # origin holds the mutex; local bookkeeping is best-effort
        print(f"[inbox_consumer] {ticket.id}: published, local commit error: {exc!r}",
              file=sys.stderr)


def _stamp_auto_delivered(ticket: TicketInfo, agent: str | None = None) -> StampResult:
    """Write auto_delivered: true + auto_delivered_at: <ISO-ts> (+ T-4695
    delivered_to: <agent> when *agent* is given: the seat that took the ticket,
    so a role:<R> pool ticket busies only its taker) to ticket
    frontmatter, then commit + push that write to origin so the mutex is
    visible fleet-wide (other agents' and HO's own reads of `origin` depend
    on the push leg, not just the local file).

    Called AFTER delivery (T-2152 reordered stamp-then-deliver to
    deliver-then-stamp). By the time this function runs the agent has
    already been poked, so a False return here can no longer retroactively
    un-deliver anything — it exists purely to tell the caller (main())
    whether the audit/provenance leg landed. (Docstring-drift note: this
    function's contract text used to say "caller must NOT inject on False"
    — that was accurate for T-0717's original stamp-then-deliver order, but
    stale ever since T-2152 reversed it; the text here never caught up until
    T-3325 fixed it.)

    Two distinct failure classes, BOTH now fail-loud (T-3325 — previously
    the git leg was fail-open and silently returned True):

    1. LOCAL WRITE FAILURE → returns False (unchanged behavior). The
       frontmatter write itself didn't persist; even this agent's own
       next-cycle scan won't see the lock (self-healing risk of
       re-delivery, same as before T-3325).

    2. COMMIT / ADD / PUSH FAILURE, OR A MUTATION-LOCK TIMEOUT → returns
       StampResult.FAILED (T-3325; all four used to silently return True).
       The local file write succeeded, so THIS agent's own next-cycle scan
       still correctly sees the lock — but the stamp never reached `origin`,
       so any OTHER reader (HO, a different agent's inbox_consumer, a human
       `git pull`) sees no lock at all. Reporting success here would mean
       the delivery pipeline lies about its own audit trail (P1); the
       caller now surfaces this loudly (non-zero exit) instead of
       swallowing it. Behavior-change note: a lock-timeout used to be
       silent and is now equally loud — an operator who only ever saw the
       "lock timeout (stamp written locally; commit deferred)" log line
       should now also expect the unit's OnFailure= alert to fire if the
       mutex genuinely times out.

    EXCEPTION (T-3631): a git add/commit failure that is specifically a
    transient .git/index.lock race — a concurrent git process on the clone
    holds the index lock — is retried with bounded backoff, and if it still
    cannot land, returns StampResult.LOCK_CONTENDED (NOT FAILED). This is a
    self-healing transient: the delivery already happened and a later cycle
    reconciles the mutex, so the caller exits 0 with a WARNING rather than
    OnFailure-paging Recovery. Only the .git/index.lock class is softened; every
    other add/commit/push/mutation-lock failure stays FAILED (fail-loud).

    Group-hop (T-0514): `add`/`commit` normally need `sg pantheon-shared`
    when the running process's live supplementary-group set doesn't include
    pantheon-shared — either the process was never a member (e.g. codex on
    its own worktree, T-3325) or `/etc/group` was updated but this
    process's manager predates the grant (T-0514's stale-group case).
    `_git_direct_writable` probes the real on-disk permission first and
    skips the `sg` wrap whenever the process can already write `.git/`
    directly — the common case, and the T-3325 fix (codex owns its own
    worktree, so the direct check passes and `sg`, which codex has no
    password for, is never invoked). The `sg`-wrap remains as the T-0514
    fallback for the genuine stale-group case.

    Returns StampResult.OK only if the frontmatter write AND add AND commit
    AND push all succeeded; StampResult.LOCK_CONTENDED for a self-healing
    .git/index.lock race (T-3631); StampResult.FAILED otherwise — see the
    failure classes above.
    """
    from tools.ticket import (
        TicketRefusal, parse_front_matter, _build_ticket_text, _fm_snapshot,
    )
    from tools._git_utils import REPO_ROOT

    def _mutate(text: str) -> str:
        fields, body = parse_front_matter(text)
        old_fields = _fm_snapshot(fields)
        _apply_stamp_fields(fields, agent)
        return _build_ticket_text(fields, body, old_fields=old_fields)

    try:
        text = ticket.path.read_text(encoding="utf-8")
        ticket.path.write_text(_mutate(text), encoding="utf-8")
    except TicketRefusal as exc:
        # T-4760: an ORG-021 registry refusal (refuse mode) — raised by the
        # build, BEFORE the write, so nothing was written. Classified on its
        # own (not FAILED): main() logs it as a registry refusal. Only
        # reachable if the registry changed after _preflight_stamp_registry.
        print(f"[inbox_consumer] _stamp_auto_delivered REGISTRY REFUSED for {ticket.id} "
              f"(nothing written): {exc}", file=sys.stderr)
        return StampResult.REGISTRY_REFUSED
    except FileNotFoundError as exc:
        # T-3925: the ticket file vanished before this stamp's read — it was
        # claimed/resolved OUT of inbox/ in the same window as this
        # deliver-then-stamp cycle (T-3252 claim race). Delivery already
        # happened, and an unstamped ticket that has LEFT inbox/ is no longer a
        # pending inbox delivery, so the stamp is MOOT — a benign race, not a
        # provenance gap. Sibling to the concurrent_state_flip guard in main().
        print(f"[inbox_consumer] _stamp_auto_delivered: {ticket.id} left inbox mid-cycle "
              f"(claimed/resolved) — stamp moot, benign race: {exc}",
              file=sys.stderr)
        return StampResult.TICKET_GONE
    except Exception as exc:
        print(f"[inbox_consumer] _stamp_auto_delivered write failed for {ticket.id}: {exc}",
              file=sys.stderr)
        return StampResult.FAILED  # fail-CLOSED: caller must not inject

    # Commit + push, direct when possible, via sg pantheon-shared as the
    # T-0514 fallback (see _git_direct_writable + T-3325). Commit/add/push
    # failures — and a lock timeout — are now FAIL-LOUD (T-3325): the local
    # write already established the mutex for this agent's own reads, but an
    # unpublished stamp is a real audit-provenance gap for every other
    # reader, so it must not be reported as success. See the docstring above.
    try:
        rel_path = ticket.path.relative_to(REPO_ROOT)
    except ValueError:
        rel_path = ticket.path

    direct = _git_direct_writable(REPO_ROOT)
    commit_msg = f"tickets: stamp auto_delivered on {ticket.id}"
    # T-2336: two defenses against the writer4 corruption (a bare pathspec-less
    # `git commit` swallowing ANOTHER writer's staged files under this message):
    #   1. the commit is PATHSPEC-scoped (`-- <rel_path>`), so foreign staged
    #      files cannot enter this commit even if a writer bypasses the lock;
    #   2. add+commit+push run under git_mutation_lock, serializing with every
    #      lock-taking writer (ticket.py, grooming, watch_runner).
    if _on_non_main_branch(REPO_ROOT):
        # T-5750: dev-branch clone: publish a ticket-only commit built on
        # origin/main; never commit onto, or push, the checked-out branch.
        from tools._git_publish import PublishKind
        try:
            pub = _publish_edit_via_main(ticket, rel_path, _mutate, commit_msg, direct, REPO_ROOT)
        except TicketRefusal as exc:
            print(f"[inbox_consumer] _stamp_auto_delivered REGISTRY REFUSED for {ticket.id} "
                  f"(nothing published): {exc}", file=sys.stderr)
            return StampResult.REGISTRY_REFUSED
        except GitMutationLockTimeout as exc:
            print(f"[inbox_consumer] _stamp_auto_delivered lock timeout (stamp written locally;"
                  f" NOT published — mutex not visible to other readers): {exc}",
                  file=sys.stderr)
            return StampResult.FAILED
        except Exception as exc:  # e.g. UnicodeDecodeError from mutate on origin's blob
            print(f"[inbox_consumer] _stamp_auto_delivered publish ERROR (stamp written"
                  f" locally; NOT published — mutex not visible to other readers): {exc!r}",
                  file=sys.stderr)
            return StampResult.FAILED
        if pub.kind is PublishKind.ABSENT:
            print(f"[inbox_consumer] _stamp_auto_delivered: {ticket.id} is not on origin/main"
                  f" (claimed/resolved elsewhere) — stamp moot: {pub.detail}", file=sys.stderr)
            return StampResult.TICKET_GONE
        if pub.kind is not PublishKind.OK:
            print(f"[inbox_consumer] _stamp_auto_delivered publish FAILED (stamp written"
                  f" locally; NOT pushed — mutex not visible to other readers): {pub.detail}",
                  file=sys.stderr)
            return StampResult.FAILED
        return StampResult.OK

    try:
        with git_mutation_lock(label=f"stamp auto_delivered {ticket.id}"):
            # add + commit each retry on transient .git/index.lock contention
            # (T-3631); a non-lock error returns immediately.
            if direct:
                add_cmd = ["git", "-C", str(REPO_ROOT), "add", str(rel_path)]
            else:
                add_cmd = ["sg", "pantheon-shared", "-c",
                           f"git -C {REPO_ROOT} add {rel_path}"]
            add_result = _run_git_stamp_step(add_cmd)
            if add_result.returncode != 0:
                if _is_index_lock_contention(add_result.stderr):
                    # T-3631: delivery already succeeded; only the mutex stamp
                    # lost a self-healing index.lock race — soft, not a page.
                    print(f"[inbox_consumer] _stamp_auto_delivered git add deferred on"
                          f" .git/index.lock contention (stamp written locally; a later"
                          f" cycle reconciles the mutex): {add_result.stderr}",
                          file=sys.stderr)
                    return StampResult.LOCK_CONTENDED
                print(f"[inbox_consumer] _stamp_auto_delivered git add FAILED (stamp written"
                      f" locally; NOT pushed — mutex not visible to other readers):"
                      f" {add_result.stderr}", file=sys.stderr)
                return StampResult.FAILED

            if direct:
                commit_cmd = ["git", "-C", str(REPO_ROOT), "commit", "-m", commit_msg,
                              "--", str(rel_path)]
            else:
                commit_cmd = ["sg", "pantheon-shared", "-c",
                              f"git -C {REPO_ROOT} commit -m {subprocess.list2cmdline([commit_msg])}"
                              f" -- {rel_path}"]
            commit_result = _run_git_stamp_step(commit_cmd)
            if commit_result.returncode != 0:
                if _is_index_lock_contention(commit_result.stderr):
                    print(f"[inbox_consumer] _stamp_auto_delivered git commit deferred on"
                          f" .git/index.lock contention (stamp written locally; a later"
                          f" cycle reconciles the mutex): {commit_result.stderr}",
                          file=sys.stderr)
                    return StampResult.LOCK_CONTENDED
                print(f"[inbox_consumer] _stamp_auto_delivered git commit FAILED (stamp written"
                      f" locally; NOT pushed — mutex not visible to other readers):"
                      f" {commit_result.stderr}", file=sys.stderr)
                return StampResult.FAILED

            # git push with retry on non-ff (fetch + rebase, up to 3 attempts)
            from tools._git_utils import push_with_retry
            ok, detail = push_with_retry(REPO_ROOT)
            if not ok:
                print(f"[inbox_consumer] _stamp_auto_delivered push FAILED (stamp committed"
                      f" locally; NOT pushed — mutex not visible to other readers):"
                      f" {detail}", file=sys.stderr)
                return StampResult.FAILED
    except GitMutationLockTimeout as exc:
        print(f"[inbox_consumer] _stamp_auto_delivered lock timeout (stamp written locally;"
              f" NOT committed/pushed — mutex not visible to other readers): {exc}",
              file=sys.stderr)
        return StampResult.FAILED
    return StampResult.OK  # frontmatter write + add + commit + push all succeeded


def _clear_auto_delivered(ticket: TicketInfo, decline_agent: str | None = None) -> bool:
    """Remove auto_delivered + auto_delivered_at from ticket frontmatter (genuine lock release).

    Called on staleness-backstop fire so "Lock released" is true on disk: without
    this, the stamp persisted and every subsequent 10-min consumer cycle re-detected
    the same stale ticket and re-alerted (the ~30-copy spam, msg_3073). Clearing the
    stamp makes the alert idempotent — it fires once, on the cycle that first detects
    staleness.

    Best-effort BY DESIGN — unlike the acquire side (`_stamp_auto_delivered`,
    fail-loud since T-3325), a release-side commit/push failure does not open
    the same fleet-wide correctness gap an unpublished ACQUIRE stamp does.
    Argument (T-3325 — deliberately kept, not merely inherited):

      - The caller (`check_dispatch_locked`'s staleness backstop) already
        sent the stale-lock Telegram alert and already discards this
        function's return value entirely — nothing downstream reads it.
      - Acquire vs release is a real asymmetry, not a place to force
        symmetry for its own sake: an unpublished ACQUIRE hides an
        in-flight ticket from every OTHER reader, risking a genuine
        fleet-wide double-dispatch. An unpublished CLEAR just means another
        reader still sees the OLD (already-alerted, already-known-stale)
        stamp until a later cycle's commit succeeds — self-healing, and
        there is no "delivery" on this side for a False return to
        misreport (clear doesn't deliver anything).
      - The local frontmatter write already achieves this consumer's own
        purpose: it stops THIS agent's own re-alerting, which is the
        idempotency property `_clear_auto_delivered` exists to provide.

    So this function keeps its historical best-effort contract: returns True
    if the frontmatter write persisted (stamp gone locally, so this consumer
    won't re-alert), False if the write failed (stamp remains; next cycle
    will re-alert + retry — self-healing, no worse than the pre-fix
    behavior). Commit/push failures remain non-fatal; logged to stderr.

    T-3325 Defect 1 DOES apply here: `add`/`commit` skip the `sg
    pantheon-shared` group-hop when `_git_direct_writable` says the process
    can already write `.git/` directly (see that helper's docstring).
    """
    from tools.ticket import parse_front_matter, _build_ticket_text, _fm_snapshot
    from tools._git_utils import REPO_ROOT, push_with_retry

    in_inbox = ticket.path.parent.name == "inbox"

    def _mutate(text: str) -> str:
        fields, body = parse_front_matter(text)
        # T-4760: remove-only write — it can never yield a registry finding.
        old_fields = _fm_snapshot(fields)
        fields.pop("auto_delivered", None)
        fields.pop("auto_delivered_at", None)
        # T-4695: the stamp's taker goes with it, but only while the ticket is still
        # undelivered work in inbox/. T-4698: in active/ the taker has CLAIMED it and
        # still holds live work (ticket_holder: WIP cap + dispatch mutex).
        if in_inbox:
            fields.pop("delivered_to", None)
            # T-5049: record the offer-and-not-taken outcome in the same write.
            if decline_agent:
                declined = fields.get("auto_declined_by") or []
                if isinstance(declined, str):
                    declined = [declined]
                if decline_agent not in declined:
                    declined = list(declined) + [decline_agent]
                fields["auto_declined_by"] = declined
        return _build_ticket_text(fields, body, old_fields=old_fields)

    try:
        text = ticket.path.read_text(encoding="utf-8")
        ticket.path.write_text(_mutate(text), encoding="utf-8")
    except Exception as exc:
        print(f"[inbox_consumer] _clear_auto_delivered write failed for {ticket.id}: {exc}",
              file=sys.stderr)
        return False  # stamp remains; next cycle re-alerts + retries (self-healing)

    try:
        rel_path = ticket.path.relative_to(REPO_ROOT)
    except ValueError:
        rel_path = ticket.path
    direct = _git_direct_writable(REPO_ROOT)
    commit_msg = f"tickets: clear stale auto_delivered on {ticket.id} (lock release)"
    if decline_agent and ticket.path.parent.name == "inbox":
        commit_msg = (f"tickets: clear stale auto_delivered + record auto_declined_by"
                      f" {decline_agent} on {ticket.id}")
    if _on_non_main_branch(REPO_ROOT):
        # T-5750: same dev-branch isolation as the stamp; best-effort as ever.
        from tools._git_publish import PublishKind
        try:
            pub = _publish_edit_via_main(ticket, rel_path, _mutate, commit_msg, direct, REPO_ROOT)
            if pub.kind is not PublishKind.OK:
                print(f"[inbox_consumer] _clear_auto_delivered publish {pub.kind.value}"
                      f" (cleared locally; deferred to a later cycle): {pub.detail}",
                      file=sys.stderr)
        except GitMutationLockTimeout as exc:
            print(f"[inbox_consumer] _clear_auto_delivered lock timeout (cleared locally;"
                  f" publish deferred to a later cycle): {exc}", file=sys.stderr)
        except Exception as exc:  # best-effort release side (T-3325 asymmetry)
            print(f"[inbox_consumer] _clear_auto_delivered publish error (cleared locally): {exc}",
                  file=sys.stderr)
        return True

    # T-2336: pathspec-scoped commit + git_mutation_lock — same two defenses and
    # the same best-effort degradation as _stamp_auto_delivered above.
    try:
        with git_mutation_lock(label=f"clear auto_delivered {ticket.id}"):
            if direct:
                add_result = subprocess.run(
                    ["git", "-C", str(REPO_ROOT), "add", str(rel_path)],
                    capture_output=True, text=True, check=False,
                )
            else:
                add_result = subprocess.run(
                    ["sg", "pantheon-shared", "-c", f"git -C {REPO_ROOT} add {rel_path}"],
                    capture_output=True, text=True, check=False,
                )
            if add_result.returncode != 0:
                print(f"[inbox_consumer] _clear_auto_delivered git add failed (cleared locally;"
                      f" push skipped): {add_result.stderr}", file=sys.stderr)
                return True
            if direct:
                commit_result = subprocess.run(
                    ["git", "-C", str(REPO_ROOT), "commit", "-m", commit_msg,
                     "--", str(rel_path)],
                    capture_output=True, text=True, check=False,
                )
            else:
                commit_result = subprocess.run(
                    ["sg", "pantheon-shared", "-c",
                     f"git -C {REPO_ROOT} commit -m {subprocess.list2cmdline([commit_msg])}"
                     f" -- {rel_path}"],
                    capture_output=True, text=True, check=False,
                )
            if commit_result.returncode != 0:
                print(f"[inbox_consumer] _clear_auto_delivered git commit failed (cleared locally;"
                      f" push skipped): {commit_result.stderr}", file=sys.stderr)
                return True
            ok, detail = push_with_retry(REPO_ROOT)
            if not ok:
                print(f"[inbox_consumer] _clear_auto_delivered push failed (cleared + committed locally): {detail}",
                      file=sys.stderr)
    except GitMutationLockTimeout as exc:
        print(f"[inbox_consumer] _clear_auto_delivered lock timeout (cleared locally;"
              f" commit deferred to a later cycle): {exc}", file=sys.stderr)
    return True


def pick_ticket(tickets: list[TicketInfo]) -> TicketInfo:
    """Pickup priority (T-0253 per the operator msg_1117):

    1. Stale bucket — tickets whose latest transition-log entry is a
       `[grooming] stale; ...` line. Within bucket: oldest stale-flag first
       (FIFO by the stale-flag ts).
    2. Normal bucket — everything else. FIFO by `created` ts; lower numeric
       id as tiebreak.

    Stale tickets jump the queue so accumulated-but-untouched work doesn't
    rot when agents are busy on fresher items.
    """
    def numeric_id(t: TicketInfo) -> int:
        m = re.search(r"(\d+)", t.id)
        return int(m.group(1)) if m else 0

    stale: list[tuple[str, int, TicketInfo]] = []
    normal: list[tuple[str, int, TicketInfo]] = []
    for t in tickets:
        ts = _grooming_stale_ts(t.body)
        nid = numeric_id(t)
        if ts is not None:
            stale.append((ts, nid, t))
        else:
            normal.append((t.created_str, nid, t))
    if stale:
        # Oldest stale flag wins (FIFO within stale bucket).
        stale.sort(key=lambda x: (x[0], x[1]))
        return stale[0][2]
    # Fall through: standard FIFO by created.
    normal.sort(key=lambda x: (x[0], x[1]))
    return normal[0][2]


# ---------------------------------------------------------------------------
# Prompt + inject.
# ---------------------------------------------------------------------------


def _ask_body(body: str) -> str:
    """Extract the ## Ask section body, or fall back to full body."""
    m = re.search(r"##\s+Ask\s*\n(.*?)(?:\n##|\Z)", body, re.DOTALL)
    if m:
        return m.group(1).strip()
    return body.strip()


def format_prompt(ticket: TicketInfo, agent: str) -> str:
    # T-4382: title, Ask and serves are filer-authored free text (an automated filer
    # can carry web/watch text into them), and this prompt is pasted into a live
    # composer, where an '@<path>' token loads the file pre-model with no tool call.
    # Neutralize those fields only; the locked INJECT_PROMPT text stays byte-exact.
    ask = neutralize_at_mentions(_ask_body(ticket.body))
    base = (
        INJECT_PROMPT
        .replace("T-NNNN", ticket.id)
        .replace("<title>", neutralize_at_mentions(ticket.title))
        .replace("<agent>", agent)
        .replace("<body>", ask)
    )
    # T-1103 / ORG-034: when the ticket carries a serves: arc-intent, prepend it
    # so the worker sees WHY before the body. Conditional prepend keeps the
    # byte-exact INJECT_PROMPT constant unchanged (the constant guards autonomy-
    # policy text, not metadata — changing it would break test_inject_prompt_matches_locked_text).
    if ticket.serves:
        return f"Serves: {neutralize_at_mentions(str(ticket.serves))}\n{base}"
    return base


def inject(tmux_target: str, prompt: str, harness: str = "claude-code") -> None:
    """Inject prompt into tmux target and verify submission.

    T-0538: delegates to tools/send_keys_submit.sh (single source of truth for
    submit-verify tmux injection). The helper handles text send, paste-mode
    settle delay, Enter send, and submit verification with retry. Any failure
    — including unconfirmed submit after retries — maps to SkipCycle("tmux_unreachable").
    A distinct skip vocab for pending-paste is a possible future enhancement
    (out of scope for this DRY refactor; would require registering across skip-vocab
    handling / NEVER_ALERT sets / consecutive-skip logic).

    T-4792: ``harness`` picks the submit-verify profile. The claude-code profile
    false-passes on a codex pane (the paste sits unsubmitted), so deliver() passes
    the seat's LIVE harness from the controller's runtime.json.
    """
    result = subprocess.run(
        [str(_SEND_KEYS_HELPER), "--harness", harness, tmux_target, prompt],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "INJECT_ENTER_DELAY": str(_inject_enter_delay(harness))},
    )
    # T-5380: the helper's stderr carried the only trace of a swallowed Enter, and it
    # was discarded, so two strands hid for 2h+ with nothing in the journal. One line,
    # tail only, on success (a retry warning) and on failure alike.
    err = result.stderr.strip() if isinstance(result.stderr, str) else ""
    if err or result.returncode != 0:
        print(f"[inbox_consumer] inject {tmux_target} rc={result.returncode}: "
              f"{err[-400:] or '(no stderr)'}", file=sys.stderr)
    if result.returncode != 0:
        raise SkipCycle("tmux_unreachable")


#: T-5380: static fragments of the prompts inject() delivers, matched (whitespace-
#: stripped) inside the composer region. The HEAD of a long prompt scrolls out of the
#: composer's visible rows, so the opening marker alone is not enough; the middle
#: fragments are what both stuck panes actually showed. Each is a substring of
#: INJECT_PROMPT or ACTIVE_SURFACE_PROMPT (test-pinned).
_PENDING_PROMPT_FRAGMENTS = (
    "[inbox-auto-consumer]",
    "auto-approve scope, surface to the operator via the Telegram bridge and halt rather than proceeding.",
    "blocked-with-reason, or resolve. Body follows:",
)


def _capture_for_guard(tmux_target: str) -> str:
    """Read-only pane capture for check_no_pending_delivery. "" on any failure."""
    try:
        r = subprocess.run(["tmux", "capture-pane", "-p", "-t", tmux_target],
                           capture_output=True, text=True, timeout=10, check=False)
        return r.stdout if r.returncode == 0 and isinstance(r.stdout, str) else ""
    except Exception:  # noqa: BLE001 - fail open: no capture, no guard
        return ""


def check_no_pending_delivery(tmux_target: str, harness: str, *,
                              capture: Callable[[str], str] | None = None,
                              agent: str | None = None) -> None:
    """T-5380: raise SkipCycle("inject_pending_paste") if an earlier delivery of ours
    is still sitting unsubmitted in the composer, so the next cycle never pastes a
    second prompt onto it.

    Why it is needed: a failed delivery leaves no stamp (T-2152), so the next cycle
    re-picks the same ticket, and main() has no pane check (T-0717). Once the verifier
    stopped false-confirming a head-scrolled composer, "unconfirmed after retries" is
    reachable, and without this the retry would concatenate the prompts. The vocab is
    already in the consecutive-skip alert set (6 skips, about 30 min), so a prompt
    that never submits pages instead of waiting for the 2h mail_staleness backstop.
    Fail-open: no capture, an unknown harness or no composer means no guard. Under
    unittest/pytest the default capture is skipped (no test reads a live pane)."""
    if capture is None:
        if _in_test_context():
            return
        capture = _capture_for_guard
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import submit_verify as sv  # noqa: PLC0415 (lazy: sv imports this module)
        glyph = sv.get_profile(harness)["composer_glyph"]
        pane = capture(tmux_target)
        region = sv._composer_region(pane.splitlines(), glyph) if pane else None
    except Exception:  # noqa: BLE001
        return
    if not region:
        return
    held = "".join("".join(region).split())
    if any("".join(f.split()) in held for f in _PENDING_PROMPT_FRAGMENTS):
        # T-5408: with an agent, try the one guarded recovery Enter first. Recovered
        # skips with a benign vocab (no paste: the prompt is now submitted); anything
        # else keeps the alerting skip below.
        recovered = False
        if agent:
            try:
                recovered = recover_pending_prompt(agent, tmux_target, harness) == "recovered"
            except Exception as exc:  # noqa: BLE001 - never let recovery break the guard
                print(f"[inbox_consumer] {agent}: pending-prompt recovery error {exc!r} (T-5408)",
                      file=sys.stderr)
        if recovered:
            raise SkipCycle("pending_prompt_recovered")
        raise SkipCycle("inject_pending_paste")
    if agent:  # T-5408: a clean composer ends any stuck episode, so the record resets
        clear_pending_enter_if_clean(agent, pane, harness)


#: T-5408: one bounded recovery Enter for OUR OWN ticket prompt already stuck in an idle
#: composer. State: one file per agent, written only when an Enter was spent and the prompt
#: was still there; cleared by the next clean composer sample or a verified recovery. While
#: it exists the same stuck prompt never gets a second automatic Enter.
PENDING_ENTER_STATE_DIR = _PANTHEON_STATE_DIR
_PENDING_ENTER_SETTLE_S = 1.5


def _recovery_capture(tmux_target: str) -> str:
    """Read-only capture for the recovery helper. Inert ("") under unittest/pytest."""
    return "" if _in_test_context() else _capture_for_guard(tmux_target)


def _recovery_enter(tmux_target: str) -> bool:
    """The one bare Enter, via submit_verify's helper. Inert (False) under test."""
    if _in_test_context():
        return False
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import submit_verify as sv  # noqa: PLC0415 (lazy: sv imports this module)
    return bool(sv.tmux_send_enter(tmux_target))


def _recovery_pane_in_mode(tmux_target: str) -> str | None:
    """tmux #{pane_in_mode} ("0" = normal). None when unknown or under test."""
    if _in_test_context():
        return None
    try:
        r = subprocess.run(["tmux", "display-message", "-p", "-t", tmux_target, "#{pane_in_mode}"],
                           capture_output=True, text=True, timeout=10, check=False)
        return r.stdout.strip() if r.returncode == 0 and isinstance(r.stdout, str) else None
    except Exception:  # noqa: BLE001
        return None


def _recovery_sleep(seconds: float) -> None:
    time.sleep(seconds)


def _pending_enter_record(agent: str) -> Path:
    return Path(PENDING_ENTER_STATE_DIR) / f"pending_prompt_enter_{agent}.json"


def _pending_fragments_in(text: str) -> bool:
    held = "".join(text.split())
    return any("".join(f.split()) in held for f in _PENDING_PROMPT_FRAGMENTS)


# The vouch for an Enter (not the skip-only guard) accepts only what a human would not
# type: our prompt's own opening ("[inbox-auto-consumer] T-NNNN ('" or the active-queue
# reminder's), or one of the long sentence fragments when the head has scrolled out.
# A bare "[inbox-auto-consumer]" mid-sentence is a human's draft about this consumer.
_PENDING_PROMPT_HEAD = re.compile(
    r"^\[inbox-auto-consumer\](?:T-\d+\('|Reminder:activeticketT-\d+)")


def _ours_beyond_doubt(text: str) -> bool:
    held = "".join(text.split())
    return bool(_PENDING_PROMPT_HEAD.match(held)) or any(
        "".join(f.split()) in held for f in _PENDING_PROMPT_FRAGMENTS[1:])


def _vouched_pending_prompt(pane: str, harness: str) -> tuple[bool, bool]:
    """(stuck, vouched) for `pane`. stuck: our fragments sit in the live composer's
    first paragraph. vouched: one Enter is safe there. Stricter than the delivery
    guard, which only skips: a composer border must be visible (claude), or only
    codex's footer may follow the paragraph (codex has no bottom border), so a dialog
    hiding the composer above a submitted echo is never taken for ours. Any doubt is
    (stuck, False); any exception is (False, False)."""
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import submit_verify as sv  # noqa: PLC0415
        import mailbox_wake as mw  # noqa: PLC0415 (no top-level import of this module)
        from codex_modal import is_codex_model_migration_modal, is_codex_update_modal  # noqa: PLC0415
        from codex_modal_monitor import classify_codex_pane  # noqa: PLC0415
        glyph = sv.get_profile(harness)["composer_glyph"]
        if not pane or not pane.strip():
            return False, False
        parts = mw.composer_parts(pane, glyph)
        if parts is None:
            return False, False
        text, rest = parts
        if not _pending_fragments_in(text):
            return False, False
        if mw.WAKE_NOTICE_MARKER.strip() in text:
            return True, False  # a wake notice is mailbox_wake's to recover
        if not _ours_beyond_doubt(text):
            return True, False  # the bare tag in a human's draft: skip, never Enter
        lines = pane.splitlines()
        last = max(i for i, ln in enumerate(lines) if ln.lstrip().startswith(glyph))
        bordered = any(sv._COMPOSER_BORDER.search(ln) for ln in lines[last + 1:])
        if harness == "codex":
            if not mw._footer_only(rest):
                return True, False
        elif not bordered or rest:
            return True, False
        if sv.busy_reason(pane) is not None or _GENERATING_SENTINEL in pane[pane.rfind(glyph):]:
            return True, False
        if (sv.enter_refusal(pane) is not None or sv.paste_refusal(pane) is not None
                or classify_codex_pane(pane) is not None
                or is_codex_update_modal(pane) or is_codex_model_migration_modal(pane)):
            return True, False
        return True, True
    except Exception:  # noqa: BLE001 - nothing we cannot vouch for gets an Enter
        return False, False


def _claim_pending_enter(record: Path) -> str:
    """Atomically claim the right to ONE Enter: create the record O_EXCL before sending.
    "claimed", "exists" (an Enter was already spent for this episode) or "error"
    (could not claim: send nothing)."""
    try:
        record.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(record, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return "exists"
    except OSError:
        return "error"
    try:
        os.write(fd, json.dumps({"ts": time.time()}).encode("utf-8"))
    except OSError:
        pass  # the claim is the file's existence, not its content
    finally:
        os.close(fd)
    return "claimed"


def clear_pending_enter_if_clean(agent: str, pane: str, harness: str) -> None:
    """T-5408: a positively identified composer holding none of our fragments ends the
    stuck episode, so the once-only record is dropped and a later, distinct stuck prompt
    gets its own single Enter. Called from BOTH hosts (the deliver() guard sees clean
    panes too). No composer, an empty capture or any error is not a clean sample."""
    try:
        if pane and pane.strip() and _clean_composer(pane, harness):
            _pending_enter_record(agent).unlink()
    except OSError:
        pass
    except Exception:  # noqa: BLE001
        pass


def recover_pending_prompt(agent: str, tmux_target: str, harness: str, *,
                           capture: Callable[[str], str] | None = None) -> str:
    """T-5408: submit our own stuck ticket prompt with ONE Enter, once per stuck episode.
    The single helper behind both hosts (deliver()'s guard and the dispatch_locked
    branch), sharing one per-agent record so they cannot both Enter.

    The record is a CLAIM taken (O_EXCL) BEFORE the Enter: if the claim cannot be taken,
    no Enter is sent, and a crash, a failed write or a second consumer process can never
    produce a second Enter. Only a verified recovery (a POSITIVE clean composer sample
    after the Enter) removes it; so does a later clean sample (clear_pending_enter_if_clean).

    Returns "none" (no stuck prompt of ours), "refused" (stuck but not vouched, or the
    claim could not be taken; no Enter), "already_tried" (claim exists), "recovered", or
    "failed" (Enter sent or not sendable, prompt not verified gone; claim stays).

    Known residual: our fragment mixed with user text in the SAME paragraph cannot be told
    apart from the prompt body (the head scrolls out of view), so one Enter would submit
    both, exactly as a human's Enter would. Text on later lines is refused."""
    cap = capture or _recovery_capture
    pane = cap(tmux_target)
    stuck, vouched = _vouched_pending_prompt(pane, harness)
    if not stuck:
        clear_pending_enter_if_clean(agent, pane, harness)
        return "none"
    record = _pending_enter_record(agent)
    if record.exists():
        return "already_tried"
    if not vouched or _recovery_pane_in_mode(tmux_target) != "0":
        return "refused"
    claim = _claim_pending_enter(record)
    if claim == "exists":
        return "already_tried"
    if claim != "claimed":
        return "refused"
    sent = _recovery_enter(tmux_target)
    recovered = False
    if sent:
        _recovery_sleep(_PENDING_ENTER_SETTLE_S)
        try:
            after = cap(tmux_target)
            recovered = bool(after and after.strip()) and _clean_composer(after, harness)
        except Exception:  # noqa: BLE001 - unverified is not recovered
            recovered = False
    if recovered:
        try:
            record.unlink()
        except OSError:
            pass
        print(f"[inbox_consumer] {agent}: submitted our own stuck ticket prompt with one "
              f"Enter (T-5408)")
        return "recovered"
    print(f"[inbox_consumer] {agent}: one Enter {'sent' if sent else 'could not be sent'} to our "
          f"stuck ticket prompt and it is not verified submitted; no further automatic Enter "
          f"(T-5408)", file=sys.stderr)
    return "failed"


def _clean_composer(pane: str, harness: str) -> bool:
    """A composer is visible and holds none of our fragments (so the stuck episode, if
    any, is over). No composer or a failure is not a clean sample."""
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import submit_verify as sv  # noqa: PLC0415
        region = sv._composer_region(pane.splitlines(), sv.get_profile(harness)["composer_glyph"])
        return region is not None and not _pending_fragments_in("".join(region))
    except Exception:  # noqa: BLE001
        return False


def mailbox_inbox_for(agent: str) -> Path:
    """The mailbox hub inbox path for `agent` (ORG-026 Path A convention). Pure,
    no I/O. Mirrors keep_going.py's function of the same name (T-1827 R1)."""
    return MAILBOX_HUB / agent


def send_mailbox(agent: str, prompt: str, *, inbox: Path | None = None) -> bool:
    """Deliver the locked ticket-poke prompt via mailbox.send() instead of a
    pane inject (T-1828 R2, mirrors keep_going.py's send_mailbox, T-1827 R1).

    payload is `prompt` VERBATIM (format_prompt()'s output) — it already
    satisfies the design note's state-pointer invariant, so it is never
    reworded for this delivery path. `inbox` overrides the default hub path
    (tests only; production always uses mailbox_inbox_for(agent))."""
    box = inbox if inbox is not None else mailbox_inbox_for(agent)
    try:
        # sender="inbox_consumer", not the account persona or "keep_going"
        # (T-1935 forensics precedent, applied to this distinct producer):
        # provenance should name the PRODUCER, and this is a different
        # producer from keep_going.py's nudges. With mailbox.send()'s
        # pending-window dedupe (T-1935, default on), an identical unconsumed
        # resend collapses onto the existing message rather than piling up.
        mailbox.send(str(box), prompt, sender="inbox_consumer", subject="ticket poke")
        return True
    except (OSError, ValueError, TypeError) as exc:
        print(f"[inbox_consumer] mailbox send error: {exc!r}", file=sys.stderr)
        return False


def deliver(agent: str, tmux_target: str, prompt: str) -> None:
    """Delivery-mode dispatch for the step-8 ticket-poke (T-1828 R2). Mirrors
    keep_going.py's deliver() dispatch (T-1827 R1): the decision logic in
    main() above is identical for every agent; only this final step is
    per-agent switchable. Fail-open to send-keys for any agent missing from
    DELIVERY_MODE. A mailbox-send failure raises SkipCycle("mailbox_unreachable")
    — same alerting class as inject()'s tmux_unreachable, not a silent
    fallback to send-keys."""
    if DELIVERY_MODE.get(agent, "send-keys") == "mailbox":
        if not send_mailbox(agent, prompt):
            raise SkipCycle("mailbox_unreachable")
        return
    harness = live_harness.resolve(agent, "claude-code")
    check_no_pending_delivery(tmux_target, harness, agent=agent)  # T-5380/T-5408: never paste over our own pending prompt
    inject(tmux_target, prompt, harness)


# ---------------------------------------------------------------------------
# Active-queue tail-surface (T-0383).
# ---------------------------------------------------------------------------


def _load_active_surface_log(state_path: Path) -> dict[str, str]:
    """Load {agent:tid -> last_surface_iso_ts} dict from state file. Fail-open."""
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_active_surface_log(state_path: Path, data: dict[str, str]) -> None:
    """Persist active surface log state. mkdir -p; fail-open on write error."""
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        # T-5342: temp file + os.replace, so a crash mid-write never leaves a torn
        # file (which would load as {} and re-surface everything).
        tmp = state_path.with_name(f"{state_path.name}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, state_path)
    except OSError as exc:
        print(f"[inbox_consumer] active_surface_log write failed: {exc}", file=sys.stderr)


def _list_active_tickets_for_agent(agent: str) -> list[TicketInfo]:
    """Return all tickets in tickets/active/ whose target matches agent.

    Matches both persona name (e.g. 'orchestrator') and role ID (e.g. 'orchestrator')
    using agent_role_map.resolve_role, mirroring list_eligible_tickets().
    """
    active_dir = TICKETS_ROOT / "active"
    if not active_dir.exists():
        return []

    audit_role = resolve_role(agent)
    functional = _safe_functional_roles(agent)  # ORG-088 rev-2 branch (c), T-3789
    tickets: list[TicketInfo] = []
    for path in active_dir.iterdir():
        if not path.is_file() or not path.name.endswith(".md"):
            continue
        try:
            t = parse_ticket(path)
        except Exception:
            continue
        fields = t.fields
        if fields.get("state") != "active":
            continue
        target = fields.get("target")
        if not _target_addresses_seat(target, agent, audit_role, functional):
            continue
        # T-6292: a merge-wait is not a stall (same substate T-4119 excludes from
        # the WIP cap and the dispatch mutex), so no ">24h" reminder for anyone.
        if fields.get("substate") == "awaiting-merge":
            continue
        # T-6292: a role:<R> pool ticket matches every member of R, but only its
        # stamped taker (`delivered_to`, via the shared ticket_holder) should be
        # reminded. No taker stamped -> unchanged target-based behaviour.
        if isinstance(target, str) and target.startswith("role:"):
            holder = ticket_holder(fields)
            if holder and holder != agent:
                continue
        tickets.append(TicketInfo(
            id=fields.get("id", path.stem),
            title=t.title,
            created_str=fields.get("created", ""),
            depends_on=fields.get("depends_on") or [],
            labels=fields.get("labels") or [],
            body=t.body,
            path=path,
        ))
    return tickets


def _format_active_surface_prompt(ticket: TicketInfo, agent: str) -> str:
    """Format the check-in nudge prompt for an already-active ticket."""
    # T-4382: same untrusted fields as format_prompt, same pane destination.
    ask = neutralize_at_mentions(_ask_body(ticket.body))
    return (
        ACTIVE_SURFACE_PROMPT
        .replace("T-NNNN", ticket.id)
        .replace("<title>", neutralize_at_mentions(ticket.title))
        .replace("<agent>", agent)
        .replace("<body>", ask)
    )


def surface_active_ticket(
    agent: str,
    tmux_target: str,
    state_path: Path,
) -> str | None:
    """Surface the oldest untouched active ticket for agent if rate-limit allows.

    Rate-limit: at most once per ACTIVE_SURFACE_COOLDOWN_SECONDS (24h) per
    {agent, ticket_id}. State lives in `state_path` (JSON dict keyed by
    "{agent}:{tid}" -> ISO timestamp string).

    Returns a log vocab string "active_surface:{tid}" on successful delivery,
    or None if nothing to surface (no tickets, all rate-limited, delivery failed).

    Delivery goes through deliver() (T-2065), so this reminder honours the same
    per-agent DELIVERY_MODE as the step-8 new-ticket poke. Before T-2065 it
    called inject() unconditionally, which left a mailbox-mode agent (specialist)
    getting new-ticket pokes over the mailbox but this reminder over the very
    tmux pane-inject path T-1828 R2 exists to move it off.

    Redelivery-safe: the payload points at durable ticket state ("check status
    on T-NNNN — in progress, blocked-with-reason, or resolve"), so a replayed
    reminder re-reads the ticket and no-ops if it has since moved. The one
    point-in-time assertion it carries is the ">24h no transition" claim, which
    a delayed redelivery can render stale; that is cosmetic, not corrupting,
    because the instruction is to CHECK state rather than to assume it.

    Fail-open on delivery errors — a surface failure must not break the skip path.
    """
    active_tickets = _list_active_tickets_for_agent(agent)
    if not active_tickets:
        return None

    # Pick oldest by created timestamp (FIFO), lower numeric id as tiebreak.
    def _sort_key(t: TicketInfo):
        m = re.search(r"(\d+)", t.id)
        nid = int(m.group(1)) if m else 0
        return (t.created_str, nid)

    active_tickets.sort(key=_sort_key)

    log = _load_active_surface_log(state_path)
    now = datetime.now(timezone.utc)

    for ticket in active_tickets:
        state_key = f"{agent}:{ticket.id}"
        last_ts_str = log.get(state_key)
        if last_ts_str is not None:
            try:
                last_ts = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                age = (now - last_ts).total_seconds()
                if age < ACTIVE_SURFACE_COOLDOWN_SECONDS:
                    continue  # rate-limited; try next ticket
            except (ValueError, TypeError):
                pass  # malformed timestamp; treat as expired, allow surface

        # Real-staleness gate (T-0544): only surface when the ticket's own
        # last transition is >ACTIVE_STALENESS_SECONDS old.  This makes the
        # '>24h no transition' claim in ACTIVE_SURFACE_PROMPT factually true.
        # Skip-on-None: if the transition log is absent or unparseable we
        # cannot prove staleness, so we do not assert it.
        last_tx = _last_transition_ts(ticket.body)
        if last_tx is None:
            continue  # unparseable transition log; don't assert '>24h' claim
        if (now - last_tx).total_seconds() < ACTIVE_STALENESS_SECONDS:
            continue  # transitioned recently; not yet stale

        # This ticket is eligible — surface it.
        prompt = _format_active_surface_prompt(ticket, agent)
        try:
            deliver(agent, tmux_target, prompt)
        except SkipCycle as exc:
            # Fail-open is REQUIRED here and is not a copy of deliver()'s
            # contract: this function runs *inside* main()'s `except SkipCycle`
            # handler (the T-0383 tail-surface on an already-skipped cycle), so
            # re-raising could not produce deliver()'s intended skip-alerting —
            # nothing above re-handles it as a skip, and check_consecutive_skips
            # has already fired for the ORIGINAL vocab. Raising would only
            # surface as the caller's generic "active surface failed".
            #
            # But a mailbox outage must not read like a routine tmux hiccup, so
            # the vocab is logged rather than swallowed anonymously (T-2065).
            print(
                f"[inbox_consumer] active surface delivery failed for {agent}: {exc.vocab}",
                file=sys.stderr,
            )
            return None

        # Update state file.
        log[state_key] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        _save_active_surface_log(state_path, log)
        return f"active_surface:{ticket.id}"

    return None  # all tickets rate-limited


# ---------------------------------------------------------------------------
# Logging.
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log_outcome(agent: str, outcome: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = f"{_now_iso()} {agent} {outcome}\n"
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(entry)


# ---------------------------------------------------------------------------
# Consecutive-skip tracking + alert.
# ---------------------------------------------------------------------------


def _read_telegram_token() -> str | None:
    if not TELEGRAM_ENV.exists():
        return None
    for line in TELEGRAM_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("TELEGRAM_BOT_TOKEN="):
            return line.split("=", 1)[1].strip()
    return None


def _live_telegram_suppressed() -> bool:
    """True when the live Bot API send must be suppressed — i.e. under a test harness.

    Belt-and-suspenders against the recurring test-leak failure mode: a test that
    forgets to mock a notify call site would otherwise reach the operator's LIVE channel
    with the real bot token. Bit us on T-0708/T-0709, and again 2026-06-06 when a
    restart_drain validation run leaked ~20 alerts (rainbow of reasons + the new
    quarantine alert). This function gates the single network boundary every info/
    alert path funnels through, so one guard protects all call sites — present and
    future — regardless of which one a test forgets to mock.

    Detection: the prod entry points (inbox_consumer / restart_drain /
    context_fill_monitor run as `python3 tools/<x>.py`) do NOT import unittest or
    pytest, so their presence in sys.modules is a low-false-positive test signal.
    `PANTHEON_DISABLE_LIVE_TELEGRAM` overrides in either direction for the rare case
    the heuristic is wrong: '1'/'true'/'yes' force-suppress, '0'/'false'/'no'
    force-send (so a test that legitimately exercises the real sender body can opt in).
    """
    override = os.environ.get("PANTHEON_DISABLE_LIVE_TELEGRAM", "").strip().lower()
    if override in ("1", "true", "yes"):
        return True
    if override in ("0", "false", "no"):
        return False
    return (
        "unittest" in sys.modules
        or "pytest" in sys.modules
        or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    )


def _send_telegram_alert(text: str) -> bool:
    """Direct Bot API alert — mirrors usage_monitor.py send_telegram pattern.

    Returns True only when sendMessage answered 2xx; False on every other path
    (test-context suppression, no token, non-2xx, network error), each of which is
    also logged to stderr. Most callers ignore the result; a caller that must know
    whether the operator was reached (recovery_watcher.PageSender) reads it (T-4930).
    """
    if _live_telegram_suppressed():
        print(
            f"[inbox_consumer] test context — live telegram suppressed: {text[:80]!r}",
            file=sys.stderr,
        )
        return False
    token = _read_telegram_token()
    if not token:
        print("[inbox_consumer] no telegram token; alert NOT sent", file=sys.stderr)
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": OPERATOR_CHAT_ID, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if not (200 <= resp.status < 300):
                print(f"[inbox_consumer] telegram alert returned {resp.status}", file=sys.stderr)
                return False
            return True
    except (urllib.error.URLError, OSError) as e:
        print(f"[inbox_consumer] telegram alert failed: {e}", file=sys.stderr)
        return False


_RECOVERY_ROUTING_LOG_DEFAULT = Path.home() / ".local" / "log" / "pantheon" / "recovery_alert_routing.log"
RECOVERY_ROUTING_LOG = _RECOVERY_ROUTING_LOG_DEFAULT


def _in_test_context() -> bool:
    """Structural test-context signal for the OBSERVABILITY log (T-2351).

    Deliberately NOT `_live_telegram_suppressed()`. That function honours
    `PANTHEON_DISABLE_LIVE_TELEGRAM`, and a test that wants to exercise the real
    injection BODY sets it to '0' to force-send — which is legitimate (the
    subprocess is mocked, nothing reaches a pane) but ALSO switched this log
    back on, so the test's synthetic routing decisions were appended to the
    production log. That override means "run the real body", never "record this
    as production history".

    So this reads only the signal a test cannot turn off in service of an
    unrelated goal: the prod entry points do not import unittest/pytest
    (verified against inbox_consumer and context_fill_monitor), so their
    presence in sys.modules is the same low-false-positive marker documented on
    `_live_telegram_suppressed`, minus the override.
    """
    return (
        "unittest" in sys.modules
        or "pytest" in sys.modules
        or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    )


def _log_recovery_routing(outcome: str, vocab: str, why: str) -> None:
    """Append one line recording how an operational alert was routed (T-2206).

    Deliberately a plain append-only text log, not a state JSON: the question
    this exists to answer ("is the fallback rare, or is it quietly the primary
    path?") is about a SEQUENCE over time, and a single-valued state file can
    only ever hold the most recent decision.

    Best-effort — logging must never be able to suppress an alert. Any failure
    here is swallowed: the alert itself is the load-bearing side effect, and a
    full disk should degrade our observability, not our paging.

    T-2351 — WHY THE TEST-CONTEXT GATE BELOW EXISTS. This log is a MEASUREMENT
    SURFACE, and it was measuring the test suite. 123 of its 136 lines on
    2026-07-29 were synthetic: `TestSendToRecoveryPaneHelperDelegation` force-sends
    to exercise the real body and its four cases append
    routed/fallback/routed/routed on every full-suite run. The real production
    numbers underneath were 13 alerts, ALL routed, ZERO fallback.

    That pollution was not cosmetic — it was READ AS DATA and cost two tickets.
    T-2282 measured "103 routed / 24 fallback" and T-2351 was filed on it,
    reporting 24 real peer alerts a day being paged to the operator instead of
    reaching Recovery. None of them existed. The suite-run cadence even supplied a
    convincing mechanism: the firings tracked Recovery's working hours (a 5.5h
    overnight gap, resuming 06:32) because that is when Recovery RUNS TESTS — the
    same correlation the "injection fails while Recovery is busy" hypothesis
    predicted, with the causation inverted.

    The gate is therefore structural, not a convention: write iff we are not
    under a test harness, OR the caller has redirected the path away from the
    production default. Tests that legitimately assert log CONTENT already
    patch `RECOVERY_ROUTING_LOG` to a tmpdir and keep working unchanged; a test that
    does not redirect cannot write to the production log even by accident, and a
    future one inherits that for free. Same reasoning as
    `test_suppressed_context_does_not_write_a_routing_line`, which already held
    that logging a decision nothing acted on "would fabricate history" — this
    closes the force-send hole in that rule.
    """
    if RECOVERY_ROUTING_LOG == _RECOVERY_ROUTING_LOG_DEFAULT and _in_test_context():
        return
    try:
        RECOVERY_ROUTING_LOG.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"{ts} {outcome} vocab={vocab!r} {why}\n"
        with RECOVERY_ROUTING_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001 — observability must never break delivery
        pass


def _send_to_recovery_pane_diagnosed(text: str) -> tuple[bool, str]:
    """_send_to_recovery_pane plus the REASON, for T-2206.

    The original returned a bare bool built from `returncode == 0` while
    capturing the helper's stdout and stderr and discarding both — so the
    fallback was unattributable by construction. You could see THAT it had
    bypassed Recovery (the prefix made that visible, correctly) but never WHY:
    session-name drift, a pane present but wedged, and the probe itself
    erroring were indistinguishable from the outside.

    Returns (ok, why). `why` names the resolved target and, on failure, the
    exit code plus a stderr tail — enough to tell those three apart without a
    reproduction.
    """
    recovery_target = _resolve_tmux_target("recovery")
    if _live_telegram_suppressed():
        return True, f"target={recovery_target} suppressed=1"
    # T-4591: never paste into a blocking modal on Recovery's pane. The helper pastes the
    # text and presses Enter. On the spend-limit modal that Enter picks the highlighted
    # "Adjust monthly spend limit", and digits in the text can pick numbered options.
    # Refusing here is a pane-miss, so _send_alert_via_recovery falls through to Recovery's
    # mailbox. A failed capture (e.g. cross-account) reads as no evidence and keeps
    # today's behaviour.
    try:
        blocker = _pane_blocks_injection(_pane_capture("recovery") or "")
    except Exception:  # noqa: BLE001 — the gate must never cost the delivery itself
        blocker = None
    if blocker:
        return False, f"target={recovery_target} refused: {blocker} (T-4591)"
    # T-4382: every Recovery-bound alert from ~40 fleet callers lands here, and many
    # interpolate untrusted text (mail subjects and senders, ticket titles, pane
    # captures) into a message pasted into Recovery's live composer, where an '@<path>'
    # token loads the file pre-model. Alerts are machine-composed and never mean an
    # @-mention, so the whole text is neutralized at this one pane boundary.
    text = neutralize_at_mentions(str(text))
    try:
        result = subprocess.run(
            [str(_SEND_KEYS_HELPER), recovery_target, text],
            capture_output=True, text=True, check=False,
            env={**os.environ, "INJECT_ENTER_DELAY": str(INJECT_ENTER_DELAY)},
        )
    except OSError as exc:
        # The probe itself failing is a THIRD failure mode, and the old bool
        # could not express it — an exception here propagated instead of
        # falling back, so an alert could be lost rather than rerouted.
        return False, f"target={recovery_target} probe-error={exc!r}"
    if result.returncode == 0:
        return True, f"target={recovery_target} rc=0"
    err = " ".join((result.stderr or "").split())[-400:]
    return False, f"target={recovery_target} rc={result.returncode} stderr={err!r}"


def _send_to_recovery_pane(text: str, vocab: str = "") -> bool:
    """Inject alert text into Recovery's tmux pane via send_keys_submit.sh.

    T-0538: delegates to tools/send_keys_submit.sh (single source of truth for
    submit-verify tmux injection). Returns True on success (exit 0); False on any
    failure — send-keys error, paste-mode settle timeout, or unconfirmed submit
    after retries — so the caller can fall back to direct the operator Telegram.

    T-0891: pane injection is a LIVE-DELIVERY boundary too, not just the Bot-API
    paths — so it shares the `_live_telegram_suppressed()` guard. Without this, a
    test that fails to mock `_send_alert_via_recovery` leaks the alert into Recovery's REAL
    pane via send-keys even though every Telegram path is suppressed (this caused a
    ~14-alert flood into Recovery's session during a full-discover run). Returns True
    (treat as a no-op delivery) under suppression so callers skip the fallback path.
    Opt back into the real body in a unit test via PANTHEON_DISABLE_LIVE_TELEGRAM=0.

    T-2206: also records the routing decision AND its reason to
    RECOVERY_ROUTING_LOG. Kept inside this function on purpose — it is the single
    boundary every existing test already stubs, so a stubbed test suppresses
    the injection and the log together, and no new mock surface is introduced.
    """
    if _live_telegram_suppressed():
        print(
            f"[inbox_consumer] test context — recovery-pane injection suppressed: {text[:80]!r}",
            file=sys.stderr,
        )
        return True
    ok, why = _send_to_recovery_pane_diagnosed(text)
    _log_recovery_routing("routed" if ok else "pane-miss", vocab, why)
    return ok


RECOVERY_MAILBOX = os.environ.get("PANTHEON_RECOVERY_MAILBOX") or f"{mailbox_hub()}/recovery"


def _deliver_alert_to_recovery_mailbox(msg: str, vocab: str) -> bool:
    """Cross-account-safe delivery of a recovery-bound alert whose pane-inject missed.

    T-3474: send_keys into Recovery's pane CANNOT work from another agent's account —
    Recovery's tmux server lives under a different UID, so a cross-account caller (e.g.
    specialist's acp-dlq watchdog) gets rc=1 'can't find pane' and, before this, EVERY
    such alert fell straight back to direct-operator, bypassing Recovery's triage lane
    (specialist recovery_alert_routing.log: routed=0 / fallback=28, unbroken 2026-08-03..09-06).
    The ORG-026 hub mailbox IS writable cross-account — it is how every inter-agent
    message already flows — and Recovery drains its own inbox, so depositing the alert
    there restores recovery-first routing with no cross-account tmux at all.

    Returns True on successful deposit (caller skips the the operator fallback), False on
    any failure (caller then falls back to direct-operator — a lost alert is worse
    than a duplicated one). Suppressed as a no-op True in test contexts, the same
    live-delivery boundary as the pane path (a test that stubs neither leg must not
    write a real hub message)."""
    if _live_telegram_suppressed():
        return True
    try:
        mailbox.send(
            RECOVERY_MAILBOX,
            f"[{vocab}] {msg}",
            "inbox_consumer",
            subject=f"routed alert ({vocab})",
            severity="normal",
        )
        return True
    except Exception as exc:
        print(f"[inbox_consumer] recovery-mailbox alert delivery FAILED ({exc!r}); "
              f"falling back to direct the operator", file=sys.stderr)
        return False


# Markers check_consecutive_skips logs when it spends a threshold. They end a
# streak like `pulled` does, so a persistent failure alerts again after the
# next CONSECUTIVE_SKIP_ALERT_THRESHOLD skips instead of never.
_SKIP_STREAK_RESET_MARKERS = frozenset({"skip:_alert_reset", "skip:_alert_suppressed_pane_advancing"})


def count_consecutive_skips(agent: str) -> int:
    """Count the ALERT-WORTHY skips for agent since the streak last ended.

    Scans the log backwards. A `pulled` entry, a reset marker, another agent's entry
    or EOF ends the streak. Skips in NEVER_ALERT_SKIP_VOCABS are passed over: neither
    counted nor ending it (T-4376). Counting them meant that after any long benign
    run (recovery's log is thousands of dispatch_locked in a row) the count was already
    past CONSECUTIVE_SKIP_ALERT_THRESHOLD, so the ==threshold alert for a real
    failure could never fire. Ending the streak on them instead would let a real
    failure interleaved with benign cycles never alert. Passing over them keeps
    "N real skips since the last delivery or alert", the failure-loud reading.
    The reset marker itself used to count too, so the documented re-alert every
    N skips never happened either.
    """
    if not LOG_PATH.exists():
        return 0
    try:
        lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0

    count = 0
    for line in reversed(lines):
        parts = line.strip().split(" ", 2)
        if len(parts) < 3:
            continue
        _ts, line_agent, outcome = parts[0], parts[1], parts[2]
        if line_agent != agent:
            break
        if outcome in _SKIP_STREAK_RESET_MARKERS or outcome.startswith("pulled"):
            break
        if outcome.startswith("skip:"):
            if outcome[len("skip:"):] not in NEVER_ALERT_SKIP_VOCABS:
                count += 1
    return count


def check_consecutive_skips(agent: str, skip_vocab: str) -> None:
    """After logging the skip, check if we've hit the alert threshold.

    Resets count to 0 after alerting so persistent breakage alerts every 6 skips
    (~hourly at 10-min cadence) rather than once-then-silent or on every cycle.

    Vocabs in NEVER_ALERT_SKIP_VOCABS represent normal intentional-defer states
    (date-deferred tickets, empty inbox, recent-telegram) and do NOT trigger
    the alert — they'd otherwise fire every ~60 min for days/weeks during
    routine ops with no actionable signal.
    """
    if skip_vocab in NEVER_ALERT_SKIP_VOCABS:
        return
    count = count_consecutive_skips(agent)
    if count == CONSECUTIVE_SKIP_ALERT_THRESHOLD:
        # T-0868: skip:mid_turn fires identically whether the agent is legitimately
        # busy + PROGRESSING through a backlog flush or genuinely wedged mid-turn.
        # Discriminate on pane-staleness: a progressing session's pane advances
        # (animated status line) between captures; a wedged one is frozen. Suppress
        # the alert ONLY when the pane is confirmed advancing — alert on frozen
        # (real wedge) or an inconclusive probe (fail-safe: never miss a wedge).
        # Other skip vocabs (dispatch_locked, etc.) are unaffected.
        if skip_vocab == "mid_turn" and _pane_frozen_across_samples(agent) is False:
            # Busy + advancing → benign. Consume the threshold identically to the
            # alert path (reset marker) so the cadence matches — just no Telegram.
            log_outcome(agent, "skip:_alert_suppressed_pane_advancing")
            return
        # T-4399: the streak passes over benign skips (T-4376), so "N consecutive
        # cycles" overstated it. Say what is counted.
        msg = (
            f"[pantheon] inbox-consumer alert: {agent} has had {count} failing skips"
            f" since its last delivery or alert (last: skip:{skip_vocab}; benign skips"
            f" such as dispatch_locked are not counted)."
            f" Check ~/.local/log/pantheon/inbox_consumer.log for details."
        )
        # T-0364: route via Recovery's pane first, audit-Telegram on success,
        # fallback-Telegram on recovery-pane failure.
        route_failure_alert(agent, msg, vocab=f"consecutive-skip ({agent})")  # T-0722
        # Append a reset marker so the next skip starts a fresh count.
        log_outcome(agent, "skip:_alert_reset")


# ---------------------------------------------------------------------------
# Cross-account pane capture + pane-state classification.
# ---------------------------------------------------------------------------


def _pane_capture(agent: str) -> str | None:
    """Best-effort sudo'd capture of the given agent's pane. Returns None on failure.

    T-0541: parameterized replacement for _orchestrator_pane_capture(). Uses
    _AGENT_HEALTH_CONFIG to resolve pane_target and unix_user per agent, so
    Recovery can capture both Orchestrator's and Specialist's panes cross-account.

    Cross-account note: `$(id -u)` inside the bash -c string resolves to the
    target user's uid (because sudo switches user before the shell expands it),
    giving the correct XDG_RUNTIME_DIR for that user's tmux server.

    Failure modes silently swallowed: passwordless sudo unavailable, tmux
    session gone, sudo timeout. Pane probes are observability — must never
    raise into the consumer flow.
    """
    cfg = _AGENT_HEALTH_CONFIG.get(agent)
    if cfg is None:
        return None
    pane_target = cfg["pane_target"]
    unix_user = cfg["unix_user"]
    cmd = (
        f"XDG_RUNTIME_DIR=/run/user/$(id -u) tmux capture-pane "
        f"-t {pane_target} -p"
    )
    # T-0559: same-user fast path — passwordless `sudo -n -u $self` is NOT
    # universally permitted (sudoers typically gates -u to OTHER users).
    # Run the bash directly when probing our own pane.
    if unix_user == getpass.getuser():
        argv = ["bash", "-c", cmd]
    else:
        argv = ["sudo", "-n", "-u", unix_user, "bash", "-c", cmd]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _pane_frozen_across_samples(
    agent: str, samples: int = 3, gap_seconds: float = 3.0
) -> bool | None:
    """T-0868: capture agent's pane `samples` times, `gap_seconds` apart, and
    report whether it is FROZEN.

    Returns True if every successful capture is byte-identical (frozen → the
    session is not advancing → genuinely wedged), False if any capture differs
    (a healthy working session's live status line — spinner / elapsed-seconds /
    token counter — advances every second), or None if fewer than 2 captures
    succeeded (inconclusive → caller must fail safe, i.e. do NOT suppress).

    Used to discriminate a benign busy-but-progressing skip:mid_turn streak from
    a real wedge before escalating the consecutive-skip alert. Reuses the
    frozen-vs-advancing idea from the T-0857 staleness ring, applied to the pane.
    """
    caps: list[str] = []
    for i in range(samples):
        if i:
            time.sleep(gap_seconds)
        cap = _pane_capture(agent)
        if cap is not None:
            caps.append(cap)
    if len(caps) < 2:
        return None
    return all(cap == caps[0] for cap in caps)


# T-0716: modal-dialog footers (the T-0358 footer family). Their presence in the
# bottom region is a POSITIVE witness that a blocking modal owns the bottom of the
# pane, so pane_idle_state reads it as UNKNOWN, never idle. Canonical here since
# T-4203 retired PANE_WEDGE_SENTINELS (which it used to mirror); submit_verify
# imports it as ic._WEDGE_DIALOG_FOOTERS.
_WEDGE_DIALOG_FOOTERS: tuple[str, ...] = (
    "Esc to cancel · Tab to amend",
    "Enter to confirm · Esc to cancel",
    "Enter to confirm · Esc to exit",
    # T-5044: Claude Code 2.1.300's /model picker ("Enter to set as default ·
    # s to use this session only · Esc to cancel"); missed by the two above, so
    # a stuck picker read as unknown, not modal (Watchman B13 abort, 2026-10-15).
    "s to use this session only · Esc to cancel",
    # T-4646: not a footer. Claude Code 2.1.289's "Make auto mode your default
    # permission mode?" startup prompt draws none, so its first option line is the
    # witness (submit_verify.AUTO_MODE_NUDGE_MARKER; kept literal to avoid an import
    # cycle, submit_verify imports this module).
    "Yes, set auto mode as my default permission mode",
)

# Trailing NON-BLANK lines treated as the live "bottom region". tmux
# capture-pane -p pads to terminal height with blanks, so blanks are filtered
# first. 6 ≈ Claude Code's working status block height (spinner / tip / borders /
# prompt / "esc to interrupt") — tight enough to exclude a peer dialog sitting in
# scrollback above the live prompt.
_WEDGE_BOTTOM_REGION_LINES = 6


# Sentinels that mean "at an idle prompt", as distinct from "alive" ("esc to
# interrupt" is alive but GENERATING). Named separately so callers cannot collapse
# the two by accident.
_IDLE_AT_PROMPT_SENTINELS: tuple[str, ...] = (
    "? for shortcuts",       # idle at prompt, no task list
    "ctrl+t to hide tasks",  # idle at prompt, task list visible
)
_GENERATING_SENTINEL = "esc to interrupt"

# T-3579: the sub-orch/agent MANAGE-VIEW footer ("⏸ manual mode on · N shell ·
# ← for agents · ↓ to manage") is shown iff background shells/agents are active
# and the main loop is idle-waiting on them (the idle COMPOSER shows
# "? for shortcuts" instead, never "↓ to manage"). T-3579 read this state as
# "Claude Code QUEUES injected input", so a waker/poker send-keys would be doomed.
# T-4087 and T-4284 (below) disproved that for shells and for agents: once the
# main turn has ended, input submits. "↓ to manage" is
# the distinctive token: "← for agents" also appears on the idle view, so it is
# NOT usable; "↓ to manage" is manage-view-only.
_MANAGE_VIEW_SENTINEL = "↓ to manage"
# T-4087: at current TUI widths the manage footer TRUNCATES ("… · ← for agents · ↓ to…"),
# so the manage view is recognised by either form.
_MANAGE_VIEW_TRUNCATED = "↓ to…"

# T-4087 (narrows T-3579): the manage footer marks background SHELLS OR agents, and
# the two states behave differently. Live captures, 2026-09-28, Recovery's own pane:
#   A. turn ended, only background SHELLS running — "✻ Crunched for … · done … · 1 shell
#      still running", an EMPTY "❯" composer, footer "⏸ manual mode on · 1 shell · ←
#      for agents · ↓ to manage". Injected input SUBMITS (orchestrator's send-keys woke Recovery
#      at once). This is IDLE; the T-3579 rule deferred every wake here, for as long as
#      any watcher shell lived (urgent T-4084 mail sat 02:44→02:49Z).
#   B. a background AGENT running — "✻ Waiting for 1 background agent to finish",
#      then one row per running agent BELOW the footer: "◯ general-purpose  <desc>
#      3s · ↓ 33.1k tokens". A finished agent's row disappears, so a streamed-token
#      row means a RUNNING agent.
# The T-2729 "shell subview" worry (a manage-view read idle → keys go into a SHELL)
# does not match these captures: the "❯" under the footer is Claude's own composer.
# To keep the idle call positive-evidence-only, state A also requires that composer
# to be EMPTY; a manage footer without one stays BUSY (defer — the safe direction).
#
# T-4284 CORRECTS state B. T-4087 (following T-3579) read B as "injected input QUEUES",
# but that was inferred from submit_verify's "still pending" exit-8s, never seen in a
# transcript. B is the TURN-ENDED state. Recovery's own pane, captured 2026-09-29 06:37Z
# after its last message printed, rendered exactly B ("✻ Waiting for 1 background
# agent to finish", empty "❯", one agent row). mason's turn ended 05:49:10Z with async
# agent af4a1703 still running, and pantheon-seat-relaunch's send_keys_submit landed
# as a user turn at 05:50:29Z (rc 0, no re-press). Reading B as busy starved orchestrator's
# mail for the life of its bg implementers: artisan's URGENT "HOLD #386" sat 37 min
# while orchestrator, taking turns, merged #386. So B is IDLE for delivery. Callers that
# must not NUDGE an agent waiting on its own delegated work (activity_monitor's P1.b
# poke) opt back in with background_agents_busy=True.
_BACKGROUND_AGENT_ROW_RE = re.compile(r"↓\s*[\d.,]+\s*[kKmM]?\s+tokens")
_WAITING_FOR_AGENTS_RE = re.compile(r"Waiting for \d+ background agents?\b")
_EMPTY_COMPOSER_RE = re.compile(r"^\s*❯\s*$", re.MULTILINE)
# T-4284: the agent list ("● main", then one "◯ <name> …" row per agent) renders
# BELOW the footer, one line per agent. With enough agents the fixed bottom region
# held only rows, so the footer — and its "esc to interrupt" — fell out of view
# (orchestrator, 2026-09-29: footer + 5 list lines = exactly the 6-line region).
# In "⏸ manual mode on" (every seat's permission mode, TUI 2.1.284) the idle footer
# is just "⏸ manual mode on · ← for agents", with no "? for shortcuts", so an idle
# seat with no background work read UNKNOWN (specialist 2026-09-29 07:41Z). That footer
# also sits under a generating turn whose footer omits "esc to interrupt" (specialist
# 07:40Z, and 2026-09-28 21:25Z), so it is idle evidence only AFTER the spinner
# check has ruled generation out, and only over an EMPTY composer (T-4292 finding).
# T-4935 follow-up: the bypass/accept-edits footer ("⏵⏵ bypass permissions on (shift+tab
# to cycle)") is a mode footer too. Without "← for agents" on the line (no subagents in
# view) it used to match nothing here, so an idle bypass seat read UNKNOWN and every
# mailbox wake deferred (recovery 2026-10-13 ~18:4x-19:4xZ, footer "⏵⏵ bypass permissions on
# (shift+tab to cycle) · gh auth login for PR status").
_MODE_FOOTER_RE = re.compile(r"^\s*⏸ .*\bmode on\b|^\s*⏵⏵? [^·]*\bon\b|← for agents", re.MULTILINE)
_AGENT_LIST_ROW_RE = re.compile(r"^\s*[●◯]\s")
# T-4319: the agent panel FOCUSED (the user pressed ← for agents). The footer becomes
# "Enter to view · x to stop · ctrl+x ctrl+k to stop all agents" and a "❯ ◯ <agent>"
# cursor row marks the selection (orchestrator 2026-09-29 18:01Z). Keys typed there are
# HOTKEYS: a pasted wake prompt's "x" would stop an agent and its Enter would open one.
# So it reads BUSY outright, not UNKNOWN, because a caller that injects on None would
# otherwise paste into it (mailbox_wake did until T-4292).
# The same holds for ANY "Enter to view" footer: keyboard focus is off the composer.
# The background-TASKS pill focused ("⏸ manual mode on · 5 shells · Enter to view
# tasks", orchestrator 2026-09-29 19:10Z) read IDLE (the mode footer over a dim-stripped
# empty composer), and a wake there opened the tasks list ("↑/↓ to select · Enter to
# view · Esc to close") instead of submitting. Also BUSY: viewing a finished
# teammate ("esc to return to team lead": the composer types into the teammate) and
# the parenthesised hint ("5 shells (Enter to view tasks)"), both from the 2.1.284
# bundle's footer hints, not yet captured live (rev-t4284b).
_AGENT_PANEL_FOCUSED_RE = re.compile(
    r"(?:^\s*|·\s*|\(\s*)Enter to view\b|\besc to return to\b", re.MULTILINE)
# Only the lines BELOW the composer's bottom border are footer and agent list. The
# transcript never is, and quoting a focus footer there (a grep of this file, a
# message about this bug) made an idle pane read BUSY, silently: mail_staleness
# alerts only idle+unread, and an idle pane prints nothing to scroll the quote away
# (rev-t4284b R1). With no rule visible (the open tasks list replaces the composer),
# fall back to the fixed bottom region.
_COMPOSER_RULE_RE = re.compile(r"^\s*[─━]{8,}\s*$")


def _below_composer(non_blank: list[str]) -> list[str]:
    idx = max((i for i, ln in enumerate(non_blank) if _COMPOSER_RULE_RE.match(ln)), default=None)
    return non_blank[idx + 1:] if idx is not None else non_blank[-_WEDGE_BOTTOM_REGION_LINES:]


# T-4323(a): the footer's hint slot is read as a WHITELIST of composer-focus hints.
# _AGENT_PANEL_FOCUSED_RE above is a blacklist. It names the focus-off footers seen so
# far, so a focus state nobody has captured (a pill not yet enumerated, or a rebound
# footer:openSelected keychord, since "Enter to view" is only the default binding) read
# IDLE over an empty composer, and a wake was pasted into it. The two routes that call a
# pane idle from its footer (the manage footer and the plain mode footer) now also
# require every hint in the footer to be one of these. An unknown hint reads BUSY:
# over-defer, which mail_staleness's busy-stale tier (T-4323 b) turns into an alert
# after 2h, never an inject we cannot vouch for. The blacklist stays as the first gate.
# Sources: live captures, except ctrl+t, shift+tab and hold space, which come from
# rev-t4284b's read of the 2.1.284 bundle (T-4319 review, note 1).
_COMPOSER_FOCUS_HINT_RES = tuple(re.compile(p) for p in (
    r"\d+ (?:shells?|monitors?)(?:, \d+ (?:shells?|monitors?))*",  # background-work counts
    r"\d+ feedback drafts?",  # specialist 2026-09-30 02:03Z
    r"← for agents",
    r"↓ to manage",
    r"/tasks to see subagents",  # Recovery 2026-09-28
    r"\? for shortcuts",
    r"ctrl\+t to (?:show|hide) tasks",
    r"shift\+tab to cycle",
    r"hold space to speak",
    # T-4933 (Recovery 2026-10-12/13, live): shown when the clone's gh CLI is
    # unauthenticated. Informational; no focus change, input still submits.
    r"gh auth login for PR status",
))
# A footer wider than the pane ends in "…" ("← for agen…", "↓ to…" at today's widths).
# The cut segment counts as known when it is the start of one of these.
_COMPOSER_FOCUS_HINT_PREFIXES = (
    "← for agents", "↓ to manage", "/tasks to see subagents", "? for shortcuts",
    "ctrl+t to show tasks", "ctrl+t to hide tasks", "shift+tab to cycle", "hold space to speak",
    "gh auth login for PR status")
# The permission-mode segment that leads the footer ("⏸ manual mode on",
# "⏵⏵ bypass permissions on (shift+tab to cycle)").
_MODE_INDICATOR_RE = re.compile(r"(?:⏸|⏵⏵?)\s[^·]*\bon\b(?:\s*\(shift\+tab to cycle\))?")
_FOOTER_SEGMENT_SEP = " · "


def _footer_hints_known(core: list[str], anchor: Callable[[str], bool]) -> bool:
    """True when the footer (the last line below the composer that `anchor` accepts)
    carries only composer-focus hints. No such line below the composer means the route
    matched on transcript text, so the real footer is unrecognised: False.

    Only the left group is the hint slot; a run of 3+ spaces starts the right-aligned
    notices, which are not focus states."""
    line = next((ln for ln in reversed(_below_composer(core)) if anchor(ln)), None)
    if line is None:
        return False
    segments = re.split(r"\s{3,}", line.strip())[0].split(_FOOTER_SEGMENT_SEP)
    if _MODE_INDICATOR_RE.fullmatch(segments[0]):
        segments = segments[1:]
    for i, seg in enumerate(segments):
        if any(r.fullmatch(seg) for r in _COMPOSER_FOCUS_HINT_RES):
            continue
        stem = seg[:-1].rstrip() if seg.endswith("…") else None
        if i == len(segments) - 1 and stem and any(
                h.startswith(stem) for h in _COMPOSER_FOCUS_HINT_PREFIXES):
            continue
        return False
    return True


# From the 6th agent the list ends in an overflow line, "  ↓ 1 more" (orchestrator
# 2026-09-29 ~17:00Z, tools/testdata/orchestrator_generating_agent_overflow_pane.txt). It
# is part of the list: left unsplit, it pushed the footer and composer out of the
# bottom region, so a turn-ended pane with 6+ agents starved (rev-t4284 round 2).
_AGENT_LIST_OVERFLOW_RE = re.compile(r"^\s*↓\s*\d+\s+more\s*$")
_AGENT_LIST_FOOTER_MARKERS = ("← for agents", _MANAGE_VIEW_SENTINEL, _MANAGE_VIEW_TRUNCATED)
# T-4857: a right-aligned notice can take its own line between the footer and the
# agent list: "✔ Update installed · Restart to apply" (archivist 2026-10-10 04:51Z,
# tools/testdata/archivist_turn_ended_update_notice_agents_pane.txt). It hid the
# footer from the split, and the unsplit rows read the turn-ended pane BUSY until the
# 2h busy-stale backstop. Right-aligned means deeply indented; list rows are not.
_RIGHT_ALIGNED_NOTICE_RE = re.compile(r"^\s{20,}\S")
_MAX_NOTICE_LINES = 2


# T-4284: Claude Code draws a prompt SUGGESTION into an idle composer in DIM text
# ("❯ \x1b[2mkeep going\x1b[0m", artisan 2026-09-29 16:5xZ). A plain capture (no -e)
# cannot tell that from typed text, so the empty-composer rules read the idle pane
# BUSY and every wake deferred (artisan: 3 unread, "deferred, pane busy" at 16:45 and
# 16:48 while its turn had ended at 11:33). Wake-path callers capture with -e;
# pane_idle_state drops dim spans on composer lines and then strips the escapes.
# A pending paste placeholder is drawn at normal intensity (artisan's pane stream),
# so it survives as composer text.
_ESCAPE_RE = re.compile(r"\x1b\[[0-9;:?<>=]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]")
_DIM_SPAN_RE = re.compile(r"\x1b\[2m.*?(?:\x1b\[(?:0|22)?m|$)")


def strip_ghost_text(pane: str) -> str:
    """A `capture-pane -e` capture as plain text, minus DIM spans on composer lines.
    Plain text (no escapes) is returned unchanged."""
    if "\x1b" not in pane:
        return pane
    out = []
    for ln in pane.splitlines():
        if _ESCAPE_RE.sub("", ln).lstrip().startswith("❯"):
            ln = _DIM_SPAN_RE.sub("", ln)
        out.append(_ESCAPE_RE.sub("", ln))
    return "\n".join(out)


def _split_agent_list(non_blank: list[str]) -> tuple[list[str], list[str]]:
    """Split the trailing agent-list rows off a pane's non-blank lines.

    Returns (lines above and including the footer, the agent-list rows). Rows are
    split only when the line directly above them is the footer, so an assistant
    "● …" bullet in the transcript is never mistaken for the list. A trailing
    "↓ N more" overflow line belongs to the list. Up to _MAX_NOTICE_LINES right-aligned
    notice lines may sit between the footer and the rows (T-4857); they stay on the
    footer side, as they do on a pane with no agent list."""
    i = len(non_blank)
    while i > 0 and _AGENT_LIST_OVERFLOW_RE.match(non_blank[i - 1]):
        i -= 1
    while i > 0 and _AGENT_LIST_ROW_RE.match(non_blank[i - 1]):
        i -= 1
    if not 0 < i < len(non_blank):
        return non_blank, []
    footer = i
    while (footer > 0 and i - footer < _MAX_NOTICE_LINES
           and _RIGHT_ALIGNED_NOTICE_RE.match(non_blank[footer - 1])):
        footer -= 1
    if footer > 0 and _is_agent_list_footer(non_blank[footer - 1]):
        return non_blank[:i], non_blank[i:]
    return non_blank, []


def _is_agent_list_footer(line: str) -> bool:
    """T-5607: some builds hang the agent list straight under the mode footer
    ("⏵⏵ bypass permissions on · 9 shells", orchestrator 2026-10-26) with none of the
    _AGENT_LIST_FOOTER_MARKERS, so the mode line counts as the footer too."""
    return (any(m in line for m in _AGENT_LIST_FOOTER_MARKERS)
            or bool(_MODE_FOOTER_RE.search(line)))


def _pane_blocks_injection(pane: str) -> str | None:
    """T-4591: why pasting text + Enter into ``pane`` would act on a modal, or None.

    Positive evidence only, read the way pane_idle_state reads it: a T-0716 modal
    footer in the bottom region (scrollback above it never counts), or a focused
    agent panel or pill (T-4319: keys become hotkeys). An empty or unreadable
    capture is None, not a block, so a caller with no view of the pane behaves
    as it did before this check."""
    if not pane or not pane.strip():
        return None
    pane = strip_ghost_text(pane)
    non_blank = [ln for ln in pane.splitlines() if ln.strip()]
    core, _rows = _split_agent_list(non_blank)
    bottom = "\n".join(core[-_WEDGE_BOTTOM_REGION_LINES:])
    raw_bottom = "\n".join(non_blank[-_WEDGE_BOTTOM_REGION_LINES:])
    for footer in _WEDGE_DIALOG_FOOTERS:
        if footer in bottom or footer in raw_bottom:
            return f"modal footer {footer!r}"
    if _AGENT_PANEL_FOCUSED_RE.search("\n".join(_below_composer(non_blank))):
        return "focused agent panel"
    return None


def pane_idle_state(pane: str, *, background_agents_busy: bool = False) -> bool | None:
    """THREE-state pane classification: True=idle at prompt, False=generating,
    None=unknown. Built for T-2158 condition 5 and T-2157.

    WHY THIS IS NOT A LIVENESS CHECK: "is the TUI loop ALIVE (not blocked on a
    modal)" is TRUE for a busily-generating agent. (That was the retired T-0716
    wedge predicate _bottom_region_shows_live_input, removed with the dead health
    loop in T-4203.) A caller that used a liveness check as an idle check would
    read a busy agent as idle: condition 5 would re-kick it every sweep (kick
    spam), and a mail-backlog detector would alert on healthy-busy-with-unread,
    which is the exact false positive the idle+unread framing exists to eliminate.

    None means UNKNOWN and callers MUST NOT act on it — same contract as
    session_key(): "a flaky query must not cause kick spam". Unknown arises from an
    empty/unreadable capture, a modal footer (the loop is blocked, so neither idle
    nor generating is the right answer), or no recognised sentinel at all — which
    is how a foreign substrate (codex's different TUI, archivist's absent pane)
    degrades to today's behaviour rather than to noise.

    Reads only the bottom region, and preserves the _WEDGE_DIALOG_FOOTERS
    exclusion, for the T-0716 reason: a sentinel appearing in SCROLLBACK — a
    captured peer dialog, or an alert quoting the sentinel verbatim — must not be
    read as the pane's current state. The agent list below the footer is split off
    first (T-4284), so the region always reaches the footer however many agents run.

    background_agents_busy=True reads a running background agent as BUSY even when
    the main turn has ended (state B above). Delivery callers leave it False: input
    submits there. A caller whose action is a NUDGE (activity_monitor's P1.b poke)
    sets it, so an agent waiting on its own delegated work is not prodded.
    """
    if not pane or not pane.strip():
        return None
    pane = strip_ghost_text(pane)
    non_blank = [ln for ln in pane.splitlines() if ln.strip()]
    core, agent_rows = _split_agent_list(non_blank)
    bottom = "\n".join(core[-_WEDGE_BOTTOM_REGION_LINES:])
    # The raw bottom (agent rows included) is still checked for a modal and for
    # "esc to interrupt": splitting the list off may only ever ADD evidence.
    raw_bottom = "\n".join(non_blank[-_WEDGE_BOTTOM_REGION_LINES:])
    if any(f in bottom or f in raw_bottom for f in _WEDGE_DIALOG_FOOTERS):
        return None  # blocked on a modal — neither idle nor generating
    # T-4319: a focused panel or pill (any "Enter to view" footer) turns keys into
    # hotkeys; never inject there. The list sits under its footer, so read everything
    # below the composer, not the fixed bottom region.
    if _AGENT_PANEL_FOCUSED_RE.search("\n".join(_below_composer(non_blank))):
        return False
    # PRECEDENCE IS DELIBERATE — do not reorder. A pane can show BOTH a task-list
    # line and "esc to interrupt"; generating must win. The asymmetry is the point:
    # a busy agent misread as idle causes kick spam and false alerts, while an idle
    # agent misread as busy costs one wasted sample. Pinned by
    # test_generating_wins_when_both_sentinels_present.
    if _GENERATING_SENTINEL in bottom or _GENERATING_SENTINEL in raw_bottom:
        return False
    # T-4284: a generating footer can omit "esc to interrupt" (specialist 2026-09-28
    # 21:25Z, kick_verify's T-4193 capture). The background-agent rows used to mark
    # such a pane busy by accident. With them demoted for delivery, the live spinner
    # row just above the composer is the generating evidence (shared with kick_verify).
    if _spinner_above_composer(non_blank):
        return False
    # T-4284: a running background agent (streamed-token row, or the turn-ended
    # "Waiting for N background agent" line) is BUSY only for callers that opt in.
    # For delivery it is state B, where the main turn has ended and input submits.
    if background_agents_busy and (
            any(_BACKGROUND_AGENT_ROW_RE.search(r) for r in agent_rows)
            or _BACKGROUND_AGENT_ROW_RE.search(bottom)
            or _WAITING_FOR_AGENTS_RE.search(bottom)):
        return False
    # Manage footer (background shells and/or agents) and no generating sentinel:
    # idle when Claude's composer is visibly EMPTY (states A and B — injected input
    # submits); anything else under that footer stays BUSY (defer: the sole failure
    # mode is over-defer, and mail_staleness T-2157 backstops it — never an inject
    # we cannot vouch for). T-4323(a): and only when every footer hint is a known
    # composer-focus hint. The route is taken only from a manage line BELOW the
    # composer: a transcript line quoting "↓ to manage" (a grep of this file) would
    # otherwise pre-empt the sentinel and mode routes, find no footer, and latch an
    # idle pane BUSY until busy-stale (rev-t4323a r1 R1). Without a rule on screen,
    # _below_composer is the fixed bottom region, as before.
    def _is_manage(ln: str) -> bool:
        return _MANAGE_VIEW_SENTINEL in ln or _MANAGE_VIEW_TRUNCATED in ln

    if any(_is_manage(ln) for ln in _below_composer(core)):
        return bool(_EMPTY_COMPOSER_RE.search(bottom)) and _footer_hints_known(core, _is_manage)
    # T-4364 (rev-t4323a r1 N2): the same reading for the idle-at-prompt sentinels.
    # Matched anywhere in the bottom region, a transcript line quoting "? for
    # shortcuts" read IDLE over a focus footer nobody had vouched for ("5 shells ·
    # space to view tasks"), and a wake was pasted into it. The real sentinel sits in
    # the footer below the composer, and only over an empty one.
    def _is_idle_sentinel(ln: str) -> bool:
        return any(s in ln for s in _IDLE_AT_PROMPT_SENTINELS)

    if any(_is_idle_sentinel(ln) for ln in _below_composer(core)):
        return bool(_EMPTY_COMPOSER_RE.search(bottom)) and _footer_hints_known(core, _is_idle_sentinel)
    # The plain mode footer, with generation already ruled out above. Same rule as
    # the manage footer: idle only over a visibly EMPTY composer and known hints.
    if _MODE_FOOTER_RE.search(bottom):
        return bool(_EMPTY_COMPOSER_RE.search(bottom)) and _footer_hints_known(
            core, lambda ln: bool(_MODE_FOOTER_RE.search(ln)))
    # T-4284: streamed-token rows at the very bottom that did not split off as an
    # anchored agent list (a footer truncated inside "← for agents", a trailing
    # overflow line, or a modal footer pushed out of view by the rows) mean the
    # footer is unreadable. Read BUSY rather than UNKNOWN: not every caller defers on
    # None (mailbox_wake did until T-4292; activity_monitor still pokes on it), and
    # over-defer is the safe direction (mail_staleness backstops it).
    if not agent_rows and _BACKGROUND_AGENT_ROW_RE.search("\n".join(non_blank[-3:])):
        return False
    return None


def _send_telegram_info(text: str) -> None:
    """Send an informational Telegram message (audit, not escalation).

    Per T-0142 ratified design: each successful auto-resolve emits a per-firing
    informational message. Same delivery mechanism as the alert path; intent
    differs (audit trail rather than 'human action required'). Future operator-
    surface enhancement could route info messages to a quieter channel.
    """
    _send_telegram_alert(text)


# T-2237 rev2 part 2: per-vocab cooldown for the `[recovery routed]` AUDIT pings.
# 6h, so a class that alerts all day still surfaces to the operator a few times —
# long enough to collapse a flood, short enough that the routing never goes
# silently dark on him. Vocab is deliberately the key and is low-cardinality by
# construction (a fixed class name, usually plus the agent), so this collapses
# repeats WITHIN a class without ever hiding a class he has not heard about.
ROUTED_AUDIT_COOLDOWN_SECONDS = int(
    os.environ.get("PANTHEON_ROUTED_AUDIT_COOLDOWN_SECONDS", str(6 * 3600)))
_ROUTED_AUDIT_STATE_PATH_DEFAULT = _PANTHEON_STATE_DIR / "recovery_routed_audit_last_at.json"
ROUTED_AUDIT_STATE_PATH = _ROUTED_AUDIT_STATE_PATH_DEFAULT


def _routed_audit_state_is_production(path: Path | None) -> bool:
    """True iff we would touch Recovery's REAL throttle state from a test harness.

    Same structural gate as `_log_recovery_routing`, for the same reason it exists:
    a test that does not redirect the path must not be able to reach production
    state even by accident. Here the failure would be quieter than that incident
    and worse — a suite run would stamp cooldowns that then SUPPRESS real audit
    pings to the operator, and the missing pings look exactly like an absence of
    alerts. Reads are gated too, so tests never inherit Recovery's live cooldowns
    and stay deterministic.
    """
    effective = ROUTED_AUDIT_STATE_PATH if path is None else path
    return effective == _ROUTED_AUDIT_STATE_PATH_DEFAULT and _in_test_context()


def _load_routed_audit_state(path: Path | None = None) -> dict:
    """{vocab: last_audit_epoch}. Any unreadable/wrong shape degrades to empty —
    this gates a NOTIFICATION, so failing toward sending is the safe direction."""
    if _routed_audit_state_is_production(path):
        return {}
    p = ROUTED_AUDIT_STATE_PATH if path is None else path
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, (int, float))}


def _routed_audit_throttled(vocab: str, path: Path | None = None) -> bool:
    last = _load_routed_audit_state(path).get(vocab)
    if not isinstance(last, (int, float)):
        return False
    return (time.time() - last) < ROUTED_AUDIT_COOLDOWN_SECONDS


def _record_routed_audit(vocab: str, path: Path | None = None) -> None:
    if _routed_audit_state_is_production(path):
        return
    p = ROUTED_AUDIT_STATE_PATH if path is None else path
    state = _load_routed_audit_state(p)
    state[vocab] = time.time()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        # A state-write failure must not swallow the audit that follows it, and
        # must not break routing. Worst case we under-throttle — the old behaviour.
        print(f"[inbox_consumer] routed-audit state write failed: {exc!r}",
              file=sys.stderr)


def _send_alert_via_recovery(msg: str, vocab: str) -> None:
    """Route an OPERATIONAL alert to Recovery's pane first; audit-Telegram on success,
    fallback-Telegram on recovery-pane failure.

    Per T-0314 (originally for check_orchestrator_health escalate path) + T-0364
    (extended to all operational alert classes that previously POSTed direct
    to OPERATOR_CHAT_ID). Consolidates the routing pattern so future alerts
    inherit it for free.

    On recovery-pane success: an audit-only `[recovery routed]` info-Telegram lands in
    the operator's chat noting Recovery has handled the surfacing — the operator isn't on the
    decision path (which preserves the bandwidth budget per D1).

    On recovery-pane failure (tmux unreachable, paste-mode wedge, etc.): falls
    back to direct the operator Telegram with `[fallback: recovery-pane unreachable]`
    prefix so the bypass is visible and the operator knows the normal path was
    tried.

    Out of scope: digest / audit-trail messages (research summaries, grooming
    digests). Those keep going direct to the operator per the
    user-facing-message-vs-operational-alert distinction.
    """
    # T-2206: routing outcome + reason are recorded inside _send_to_recovery_pane
    # (which stays the single mockable boundary, so existing tests that stub it
    # keep suppressing both the injection AND the log).
    delivered = _send_to_recovery_pane(msg, vocab=vocab)
    # T-3474: the pane leg is structurally impossible cross-account (specialist cannot
    # see Recovery's tmux server), so a pane miss must try Recovery's INBOX before bypassing
    # to the operator — the hub is writable cross-account and Recovery drains it. Only both
    # legs failing is a true operator-fallback.
    if not delivered and _deliver_alert_to_recovery_mailbox(msg, vocab):
        _log_recovery_routing("routed", vocab, f"via recovery mailbox {RECOVERY_MAILBOX} (pane unreachable)")
        delivered = True
    if delivered:
        # T-2237 rev2 part 2: the AUDIT ping is throttled per vocab; the pane
        # injection above is NOT. The audit says "Recovery has this, you are not on
        # the decision path" — that is a standing fact about the routing, not a
        # per-alert event, so N alerts of one class in a window are worth one
        # ping, not N. On 2026-08-02 the operator got "a ton" of these: every
        # mail_staleness alert (one per inbox, every 5 minutes) minted its own.
        # Suppressing the audit costs nothing recoverable — Recovery already has the
        # alert, and the fallback path below is deliberately untouched.
        if _routed_audit_throttled(vocab):
            return
        _record_routed_audit(vocab)
        # Lead the operator ping with the alert's OWN first line — it already names
        # the affected channel/agent + symptom (e.g. "📵 [channel-liveness] specialist:
        # inbound Telegram poller DOWN …"). The prior audit interpolated only {vocab}
        # ("channel-liveness alert delivered to Recovery's pane"), so the operator couldn't tell
        # WHICH channel was alerting (his feedback, Telegram msg 5380, 2026-08-07).
        # Still the lightweight "Recovery's handling it, you're not on the decision path"
        # note — just no longer detail-stripped. Cap the summary so a verbose first
        # line can't blow up the ping; fall back to the {vocab} form if msg is empty.
        summary = ""
        if msg and msg.strip():
            summary = msg.strip().splitlines()[0].strip()
        if len(summary) > 240:
            summary = summary[:237] + "..."
        audit = (
            (f"{summary}\n" if summary else f"[{vocab}] alert\n")
            + "[recovery routed] → Recovery is handling this; you're not on the decision path."
            " See Recovery for action."
        )
        _send_telegram_info(audit)
    else:
        _send_telegram_alert(f"[fallback: recovery pane+mailbox unreachable] {msg}")


# Persona name of the self-monitoring/triage agent. Failure alerts for any OTHER
# agent route THROUGH this one; this agent's OWN failures cannot (it can't
# remediate itself). They go to orchestrator first, and to the operator only if that fails
# (T-0722, T-4399).
SELF_TRIAGE_AGENT = "recovery"

# T-4399: where Recovery's OWN failures go. The operator's 2026-06-02 directive (msg 2235)
# sent them straight to him because Recovery cannot remediate itself, so a Recovery-bound
# route would vanish into a dead pane. Orchestrator CAN remediate Recovery (it restarts and
# moves Recovery's seat), so HO ruled under D1 (2026-09-30): orchestrator's mailbox first, and
# the operator only when that route fails. The directive's point survives: a Recovery failure
# never lands on Recovery.
ORCHESTRATOR_MAILBOX = os.environ.get("PANTHEON_ORCHESTRATOR_MAILBOX") or f"{mailbox_hub()}/orchestrator"

# T-4399: repeats of ONE Recovery-self failure back off: the first alert is immediate,
# then no sooner than 1h, 4h, then daily. A persistent fault used to re-alert every
# 6 real skips (~hourly), overnight included. A failure that goes quiet for
# RECOVERY_SELF_ALERT_EPISODE_GAP_SECONDS starts a new episode, which alerts at once.
# Keyed by vocab, which is a fixed class name plus the agent (low-cardinality).
RECOVERY_SELF_ALERT_BACKOFF_SECONDS = (3600, 4 * 3600, 24 * 3600)
RECOVERY_SELF_ALERT_EPISODE_GAP_SECONDS = 6 * 3600
_RECOVERY_SELF_BACKOFF_STATE_PATH_DEFAULT = _PANTHEON_STATE_DIR / "recovery_self_alert_backoff.json"
RECOVERY_SELF_BACKOFF_STATE_PATH = _RECOVERY_SELF_BACKOFF_STATE_PATH_DEFAULT


def _load_recovery_self_backoff(path: Path) -> dict:
    """{vocab: {"sent": n, "last_sent": epoch, "last_seen": epoch}}. Anything
    unreadable degrades to empty: this gates a NOTIFICATION, so fail toward sending.
    A test that does not redirect the path never reads (or writes) production state,
    the same structural gate as the routed-audit throttle."""
    if path == _RECOVERY_SELF_BACKOFF_STATE_PATH_DEFAULT and _in_test_context():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def _recovery_self_alert_decision(vocab: str, now: float, path: Path | None = None) -> tuple[bool, int]:
    """(send?, repeat number) for a Recovery-self alert of ``vocab`` at ``now``, and
    record the outcome. The repeat number is 1 for the first alert of an episode."""
    p = RECOVERY_SELF_BACKOFF_STATE_PATH if path is None else path
    state = _load_recovery_self_backoff(p)
    entry = state.get(vocab) or {}
    sent = entry.get("sent") if isinstance(entry.get("sent"), int) else 0
    last_sent = entry.get("last_sent") if isinstance(entry.get("last_sent"), (int, float)) else None
    last_seen = entry.get("last_seen") if isinstance(entry.get("last_seen"), (int, float)) else None
    if last_seen is None or now - last_seen >= RECOVERY_SELF_ALERT_EPISODE_GAP_SECONDS:
        sent, last_sent = 0, None
    if sent == 0 or last_sent is None:
        due = True
    else:
        wait = RECOVERY_SELF_ALERT_BACKOFF_SECONDS[min(sent, len(RECOVERY_SELF_ALERT_BACKOFF_SECONDS)) - 1]
        due = now - last_sent >= wait
    if due:
        sent, last_sent = sent + 1, now
    state[vocab] = {"sent": sent, "last_sent": last_sent, "last_seen": now}
    if not (p == _RECOVERY_SELF_BACKOFF_STATE_PATH_DEFAULT and _in_test_context()):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            # Worst case we under-throttle: the old hourly behaviour, never silence.
            print(f"[inbox_consumer] recovery-self backoff state write failed: {exc!r}",
                  file=sys.stderr)
    return due, sent


def _deliver_recovery_self_alert_to_orchestrator(msg: str, vocab: str) -> bool:
    """Deposit a Recovery-self failure in orchestrator's hub mailbox. True on success; False on
    any failure, and the caller then falls back to the operator (a lost alert is worse
    than a duplicated one). A no-op True in test contexts, the same live-delivery
    boundary as the Recovery-mailbox leg."""
    if _live_telegram_suppressed():
        return True
    try:
        mailbox.send(
            ORCHESTRATOR_MAILBOX,
            f"⚠️ [recovery-self-failure] {msg}\nRecovery cannot self-remediate; orchestrator, please "
            f"check Recovery's seat. The operator is paged only if this mailbox route fails.",
            "inbox_consumer",
            subject=f"recovery-self-failure ({vocab})",
            severity="normal",
        )
        return True
    except Exception as exc:
        print(f"[inbox_consumer] orchestrator-mailbox recovery-self alert FAILED ({exc!r}); "
              f"falling back to direct the operator", file=sys.stderr)
        return False


def route_failure_alert(agent: str, msg: str, vocab: str) -> None:
    """Route a FAILURE alert per the operator's directive (2026-06-02, msg 2235):
    "all failures should wire to you [Recovery] direct, except for your own, which
    should curl to telegram and let me know I need to do something to fix
    things."

    - agent != recovery → _send_alert_via_recovery (Recovery triages; it audit-pings the operator
      and falls back to direct operator-telegram if Recovery's pane is unreachable).
    - agent == recovery → orchestrator's mailbox, with the operator only as the fallback when that
      route fails, and repeats of one failure backed off (1h, 4h, daily). Recovery cannot
      self-remediate, so routing a Recovery-failure to Recovery's own pane is useless; orchestrator
      can (T-4399, HO ruling under D1, superseding the direct-to-operator leg).

    Generalizes T-0709's "pipe internal alerts to Recovery" with the Recovery-self
    carve-out. Use for every per-agent failure-class alert.

    ORG-076 §7 / T-2978 — the per-persona notification-mute chokepoint. 13 fleet
    monitors route their per-agent failure alerts through here, so gating on
    `is_muted(agent)` uniformly silences an intentionally-offline/retired persona's
    pages from ONE place (superseding the piecemeal per-monitor skip envs). The
    detecting monitor still classifies + logs the fault; only the operator page is
    withheld (mute ≠ blind). FAIL-OPEN by construction: is_muted() returns False on
    any registry fault, so a broken/absent registry never silences an alert — and
    the whole check is a dormant no-op until the registry is provisioned + a persona
    is explicitly muted.
    """
    if _persona_is_muted(agent):
        print(f"[inbox_consumer] {agent}: failure alert suppressed (persona muted) — {vocab}",
              file=sys.stderr)
        return
    if agent == SELF_TRIAGE_AGENT:
        due, repeat = _recovery_self_alert_decision(vocab, time.time())
        if not due:
            print(f"[inbox_consumer] recovery-self alert backed off (repeat {repeat} already "
                  f"sent; T-4399) — {vocab}", file=sys.stderr)
            return
        if repeat > 1:
            msg = f"{msg} (repeat {repeat} of this failure; repeats back off to daily)"
        if _deliver_recovery_self_alert_to_orchestrator(msg, vocab):
            return
        _send_telegram_alert(
            f"⚠️ [recovery-self-failure] [fallback: orchestrator mailbox unreachable] {msg}\n"
            f"Recovery cannot self-remediate — the operator, manual intervention needed."
        )
        return
    _send_alert_via_recovery(msg, vocab)


# ---------------------------------------------------------------------------
# T-1036: the T-0213 sync-alert plumbing (_sync_alert_throttled / _sync_alert_record
# / _read_sync_alert_state / _append_sync_alert_ledger / check_pantheon_config_sync_alert)
# was retired here. T-0365 retired the 60s pantheon-config-sync timer that wrote
# the state file these functions read, so they could never fire again.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Main cycle.
# ---------------------------------------------------------------------------


#: T-3434: render --check takes a few seconds; 120 s caps a wedged render.
SKILL_REFRESH_TIMEOUT_SECONDS = 120


def refresh_skills_or_warn(agent: str, timeout: int = SKILL_REFRESH_TIMEOUT_SECONDS) -> None:
    """T-3434: after the pull, re-render this seat's skills if they drifted.

    `render.py --check --harness all`: exit 0 = fresh (nothing), exit 2 = stale ->
    `render.py --no-stamp --harness all` (artifacts only; never dirties the clone).
    Fail-open: any other exit, timeout or exception logs one stderr line; never raises.
    """
    try:
        from tools._git_utils import REPO_ROOT
        render = [sys.executable, str(Path(REPO_ROOT) / "tools" / "persona_orientation" / "render.py")]
        kw = dict(cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=timeout)
        check = subprocess.run([*render, "--check", "--harness", "all"], **kw)
        if check.returncode == 0:
            return
        if check.returncode != 2:
            print(f"[inbox_consumer] {agent}: skill freshness check exited {check.returncode} "
                  "(proceeding)", file=sys.stderr)
            return
        result = subprocess.run([*render, "--no-stamp", "--harness", "all"], **kw)
        if result.returncode == 0:
            print(f"[inbox_consumer] {agent}: skills were stale; re-rendered (--no-stamp)",
                  file=sys.stderr)
        else:
            print(f"[inbox_consumer] {agent}: skills stale but re-render exited "
                  f"{result.returncode} (proceeding)", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print(f"[inbox_consumer] {agent}: skill refresh timed out after {timeout}s (proceeding)",
              file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        print(f"[inbox_consumer] {agent}: skill refresh failed (proceeding): {exc!r}",
              file=sys.stderr)


def _resolve_tmux_target(agent: str) -> str:
    return TMUX_TARGET_BY_AGENT.get(agent, f"claude-{agent}-bot")


def main(agent: str) -> int:
    # T-4203: recovery's check_agent_health loop retired (bridge decommissioned; last outcome 2026-06-03).
    # Coverage now: unknown_dialog_monitor / activity_monitor / verify_startup / cap_modal_reaper.

    # Resolve tmux target before the try block so it's accessible in the except
    # path for active-tail surface delivery (T-0383). _resolve_tmux_target never
    # raises, so this is safe to call unconditionally.
    tmux_target = _resolve_tmux_target(agent)

    skip_vocab: str | None = None
    try:
        # ORG-106 L5: before every skip gate, so a paused and/or capped seat still
        # gets its expiry notices. Fault-isolated; cannot change the outcome.
        run_seat_state_janitor(agent)
        check_cap_hit(agent)
        # ORG-106 L2: autonomy_off + maintenance + OOM + seat-state, one reader. Before
        # the pull (T-4362), so a held seat keeps its clone where it was left.
        check_seat_intake(agent)
        # T-3003 (Recovery, live root-cause 2026-08-21): pull FIRST, then run the
        # dispatch-lock maintenance, BEFORE the recent_telegram gate.
        #  - pull before check_dispatch_locked: the auto_delivered mutex scans the
        #    working tree; run pre-pull it evaluates a stale view and can miss an
        #    in-flight flag that exists on origin, over-delivering.
        #  - check_dispatch_locked before check_recent_inbound: the stale-lock
        #    release + one-in-flight enforcement live INSIDE check_dispatch_locked.
        #    With recent_telegram checked first, a long active Telegram conversation
        #    short-circuited every cycle, the cleanup never ran, and stale flags
        #    accumulated (>1 in-flight) then dumped all-at-once on the first lull
        #    (the 5-flag burst). Lock maintenance must run every cycle, independent
        #    of user activity; recent_telegram gates DELIVERY only (it is still
        #    checked below, before list_eligible_tickets/deliver).
        pull_repo_or_warn("inbox_consumer")
        # T-3434: keep this seat's persona/SOP skills current after the pull (fail-open).
        refresh_skills_or_warn(agent)
        # T-0717: check_mid_turn (per-turn pane sentinel) REMOVED; dispatch gating is
        # the auto_delivered-stamp mutex (state-derived, session-independent). The
        # HEALTH_PANE_WEDGE wedge-detection path is separate and unchanged.
        check_dispatch_locked(agent)
        check_recent_inbound(agent)
        tickets = list_eligible_tickets(agent)
        ticket = pick_ticket(tickets)
        prompt = format_prompt(ticket, agent)
        # T-2152: DELIVER FIRST, THEN STAMP. This reverses T-0717's order, which
        # stamped first to guarantee "never deliver twice" — but that traded a benign
        # failure for a sticky one, and the sticky one fired on 2026-07-28 (archivist,
        # T-1825): delivery raised, the stamp survived, check_dispatch_locked then
        # treated a poke that never arrived as in-flight and blocked her ENTIRE queue
        # until the 8h AUTO_DELIVERED_STALE_SECONDS backstop.
        #
        # Both orders have a failure mode; they are not symmetric:
        #   - stamp-then-deliver  → delivery fails ⇒ PHANTOM-DELIVERED. Queue blocked
        #     8h. Silent (dispatch_locked is in NEVER_ALERT_SKIP_VOCABS, correctly, for
        #     the genuinely-in-flight case). Not self-healing.
        #   - deliver-then-stamp  → stamp fails ⇒ possible RE-DELIVERY next cycle.
        #     Self-healing, and already defended in depth: the payload is a durable
        #     state-pointer (T-1828 R2), mailbox.send()'s dedupe_pending collapses an
        #     identical unconsumed resend (T-1935), and ticket.py's claim guard refuses
        #     a second claim on a non-inbox ticket.
        #
        # So the surviving risk is idempotent and guarded, where the old one was neither.
        # This is also what makes the one-cycle code-staleness window safe (a fresh
        # process can re-run a failed delivery); with the old order, any transient
        # delivery failure became a persistent block.
        #
        # T-3010: re-check state RIGHT BEFORE deliver — the scan gate at
        # list_eligible_tickets filters state=inbox at scan-time, but between scan
        # and deliver the ticket state can flip (concurrent resolve, or the DLQ-retry
        # path where a queued poke gets replayed after the target already handled the
        # ticket). Without this check, a done+stamped ticket can be re-poked N times
        # (observed T-3010, T-1098 x3 post-resolve). _ticket_state_on_disk returns None
        # for a ticket that has disappeared entirely — treat that as not-inbox so we
        # never deliver a poke for a missing ticket either.
        state_now = _ticket_state_on_disk(ticket.id)
        if state_now != "inbox":
            print(
                f"[inbox_consumer] {agent}: {ticket.id} state flipped "
                f"scan→deliver (was inbox, now {state_now or 'missing'}); "
                f"skipping stale poke",
                file=sys.stderr,
            )
            raise SkipCycle("concurrent_state_flip")
        # T-4760: the stamp's ORG-021 registry check runs BEFORE the delivery
        # side effect. In refuse mode a refused stamp means NO delivery this
        # cycle (a delivered-but-unstampable ticket would re-deliver every
        # cycle); it is logged loudly with the ticket id + finding and exits
        # non-zero (OnFailure page) — a registry/writer drift a human must fix.
        refusal = _preflight_stamp_registry(ticket, agent=agent)
        if refusal is not None:
            print(
                f"[inbox_consumer] {agent}: NOT delivering {ticket.id} — the "
                f"auto_delivered stamp would be refused by the frontmatter "
                f"registry: {refusal}",
                file=sys.stderr,
            )
            log_outcome(agent, f"stamp-registry-refused {ticket.id}")
            return 1
        deliver(agent, tmux_target, prompt)
        stamp_result = _stamp_auto_delivered(ticket, agent=agent)
        if stamp_result is StampResult.LOCK_CONTENDED:
            # T-3631: delivery succeeded; only the stamp add/commit lost a
            # transient .git/index.lock race to a concurrent git process on the
            # clone (its own commits, codex, a human `git`). SELF-HEALING — the
            # local write persisted and a later cycle reconciles the mutex on
            # `origin` — so exit 0 with a WARNING rather than OnFailure-paging
            # Recovery for a self-healing transient. Distinct from a genuine stamp
            # FAILURE below (which stays a hard, provenance-gap page, T-3325).
            print(
                f"[inbox_consumer] {agent}: WARNING delivered {ticket.id} but "
                f"auto_delivered stamp DEFERRED — .git/index.lock contention "
                f"(concurrent git); mutex not yet on origin, a later cycle "
                f"reconciles (self-healing; payload is a durable state-pointer)",
                file=sys.stderr,
            )
        elif stamp_result is StampResult.TICKET_GONE:
            # T-3925: delivery succeeded, but the ticket file vanished before the
            # stamp's read — it was claimed/resolved OUT of inbox/ in the same
            # window (T-3252 claim race). An unstamped ticket that has LEFT inbox/
            # is no longer a pending inbox delivery, so the stamp is MOOT, not a
            # provenance gap. SELF-HEALING (the ticket already moved on), so exit 0
            # with a WARNING rather than OnFailure-paging Recovery. Sibling to
            # LOCK_CONTENDED above and to the concurrent_state_flip guard.
            print(
                f"[inbox_consumer] {agent}: WARNING delivered {ticket.id} but "
                f"auto_delivered stamp MOOT — ticket left inbox mid-cycle "
                f"(claimed/resolved); benign race, no provenance gap",
                file=sys.stderr,
            )
        elif stamp_result is StampResult.REGISTRY_REFUSED:
            # T-4760: delivered, but the registry refused the stamp after the
            # preflight passed (registry changed mid-cycle). Distinct from a
            # git/provenance FAILED: nothing was written at all. Loud + non-zero
            # exit; the next cycle's preflight refuses BEFORE delivering.
            print(
                f"[inbox_consumer] {agent}: WARNING delivered {ticket.id} but "
                f"auto_delivered stamp REFUSED by the frontmatter registry "
                f"(nothing written); next cycle's preflight will refuse before "
                f"delivering",
                file=sys.stderr,
            )
        elif stamp_result is not StampResult.OK:  # StampResult.FAILED
            # The agent HAS been poked — do not raise SkipCycle, that would log
            # this as a skip and hide a delivery that happened. Loud on stderr
            # AND on the exit code (T-3325): an unpersisted stamp is a genuine
            # provenance-audit failure — the delivery mutex the rest of the
            # fleet reads from `origin` never got the update, even though this
            # agent's own local state (and therefore its own next-cycle
            # re-scan) sees the lock and won't double-deliver itself. A
            # non-zero exit trips this unit's existing
            # OnFailure=pantheon-unit-failure-alert@%n.service without
            # changing the outcome-log vocab below, so log readers still see
            # that the delivery itself happened.
            print(
                f"[inbox_consumer] {agent}: WARNING delivered {ticket.id} but "
                f"auto_delivered stamp FAILED — mutex not persisted to origin; "
                f"next cycle may re-deliver (safe: payload is a durable "
                f"state-pointer)",
                file=sys.stderr,
            )
        outcome = f"pulled {ticket.id}"
        log_outcome(agent, outcome)
        print(f"[inbox_consumer] {agent}: {outcome}")
        # T-3631: LOCK_CONTENDED is a self-healing transient → exit 0 (no page);
        # T-3925: TICKET_GONE is a benign claim-race (stamp moot) → exit 0 (no page);
        # only a genuine FAILED stamp keeps the non-zero exit / OnFailure page.
        return 0 if stamp_result in (StampResult.OK, StampResult.LOCK_CONTENDED, StampResult.TICKET_GONE) else 1
    except SkipCycle as exc:
        skip_vocab = exc.vocab
        outcome = f"skip:{skip_vocab}"
        log_outcome(agent, outcome)
        print(f"[inbox_consumer] {agent}: {outcome}")
        check_consecutive_skips(agent, skip_vocab)

        # T-0383: active-queue tail-surface when inbox is genuinely empty.
        # Triggered only for vocabs that indicate the inbox slot is truly idle:
        #   - empty_inbox:     no inbox tickets at all
        #   - not_before_unmet: tickets exist but all date-deferred
        # Excluded vocabs:
        #   - dependency_blocked:  inbox tickets exist (just not yet unblocked);
        #     surfacing active tickets on top would create conflicting pressure.
        #   - interactive_required: agent is likely mid-turn (TUI dialog or
        #     interactive ticket pending); injecting now risks text queuing into
        #     an in-flight session. Safety trumps surfacing cadence.
        #   - dispatch_locked: agent has in-flight ticket; surface would conflict.
        #     EXCEPTION (T-4695): a lock held only by manually claimed active work
        #     (exc.active_only) still gets the rate-limited aging-active reminder,
        #     else the busy-means-any-active rule would silence it for exactly the
        #     tickets it exists to nudge.
        #   - All other gating vocabs (off_switch, cap_hit, recent_telegram,
        #     tmux_unreachable, inject_pending_paste): not idle states.
        if skip_vocab in {"empty_inbox", "not_before_unmet"} or (
            skip_vocab == "dispatch_locked" and getattr(exc, "active_only", False)
        ):
            try:
                active_vocab = surface_active_ticket(
                    agent, tmux_target, ACTIVE_SURFACE_LOG_PATH
                )
                if active_vocab:
                    log_outcome(agent, active_vocab)
                    print(f"[inbox_consumer] {agent}: {active_vocab}")
            except Exception as e:
                # Fail-open: active surface failure must never break the skip path.
                print(
                    f"[inbox_consumer] active surface failed: {e}", file=sys.stderr
                )

        return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Pantheon inbox auto-consumer.")
    parser.add_argument(
        "--agent",
        required=True,
        # T-2144: this string is the surface that would have made the codex and
        # archivist gaps visible, and it was stale for as long as the gaps existed.
        # Keep it in sync with DELIVERY_MODE — the coverage test enforces that.
        help=("Agent name (orchestrator, specialist, recovery, mason, artisan, archivist, codex, watchman). "
              "codex is mailbox-delivery (its own supervisor/sandbox); "
              "see DELIVERY_MODE."),
    )
    args = parser.parse_args()
    sys.exit(main(args.agent))
