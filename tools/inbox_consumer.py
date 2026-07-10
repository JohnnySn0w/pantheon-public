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
  off_switch / cap_hit / recent_telegram / dispatch_locked / stamp_write_failed /
  empty_inbox / dependency_blocked / interactive_required / not_before_unmet /
  tmux_unreachable / inject_pending_paste
"""

from __future__ import annotations

import argparse
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
OPERATOR_CHAT_ID = "456474197"

HEALTH_URL = "http://127.0.0.1:8765/health"
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
#   stamp_write_failed (T-0717: persistent write failure warrants surfacing)
#
# mid_turn is no longer emitted from main() (T-0717: dispatch-gating pane
# scrape replaced by auto_delivered mutex; check_mid_turn removed).
NEVER_ALERT_SKIP_VOCABS: frozenset[str] = frozenset({
    "recent_telegram",          # user is active; alert would be noise
    "empty_inbox",              # no work; idle is fine
    "dependency_blocked",       # tickets blocked on dep; intentional
    "interactive_required",     # tickets labeled interactive; intentional
    "not_before_unmet",         # date-deferred tickets; intentional (T-0219b)
    "dispatch_locked",          # T-0717: auto_delivered mutex held — healthy, not stuck
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

# Recovery-side observability of Orchestrator — runs piggybacked on Recovery's cycle only.
ORCHESTRATOR_HEALTH_LAST_ALERT_PATH = Path.home() / ".local" / "state" / "pantheon" / "orchestrator_health_last_alert_at"
ORCHESTRATOR_HEALTH_ALERT_COOLDOWN_SECONDS = 1800  # 30 min between alerts

# Pane-wedge signal: TUI dialog stuck on screen across multiple cycles. Bridge
# fields can all read green while the loop is blocked on a permission dialog
# (observed twice on 2026-05-05); this catches that "Mode B" gap.
ORCHESTRATOR_PANE_TARGET = "claude-telegram-bot:0"
ORCHESTRATOR_HEALTH_PANE_WEDGE_STATE_PATH = Path.home() / ".local" / "state" / "pantheon" / "orchestrator_pane_wedge_first_seen_at"
ORCHESTRATOR_HEALTH_PANE_WEDGE_PERSIST_SECONDS = 120  # min wedge-persistence before acting; cadence-independent floor (see _check_pane_wedge_persistent, T-0527)

# T-0541: Per-agent health monitor config. Maps monitored-agent name →
# {pane_target, unix_user, last_alert_path, wedge_state_path}.
#
# unix_user: the Unix account that owns the tmux session (and the XDG_RUNTIME_DIR
#   that holds it). Cross-account captures/dismissals use `sudo -n -u <unix_user>`.
#   Recovery (claude-fallback) has NOPASSWD sudo into both claude (Orchestrator) and
#   claude-research (Specialist) per /etc/sudoers.d/fallback-rescues-prime and
#   /etc/sudoers.d/50-claude-fallback-research.
#
# pane_target: tmux session:window.pane specifier. Specialist runs in
#   "claude-research-bot" (confirmed tmux ls 2026-05-22); append :0 like Orchestrator.
#
# last_alert_path / wedge_state_path: per-agent state files under
#   Recovery's ~/.local/state/pantheon/ so multiple agents' state is independent.
_PANTHEON_STATE_DIR = Path.home() / ".local" / "state" / "pantheon"

_AGENT_HEALTH_CONFIG: dict[str, dict] = {
    "orchestrator": {
        "pane_target": ORCHESTRATOR_PANE_TARGET,
        "unix_user": "claude",
        "last_alert_path": ORCHESTRATOR_HEALTH_LAST_ALERT_PATH,
        "wedge_state_path": ORCHESTRATOR_HEALTH_PANE_WEDGE_STATE_PATH,
        # T-0553: orchestrator connects to pantheon-bridge via --channels; bridge
        # connected_clients and telegram_connected are meaningful for orchestrator.
        "is_bridge_client": True,
    },
    "specialist": {
        "pane_target": "claude-research-bot:0",
        "unix_user": "claude-research",
        "last_alert_path": _PANTHEON_STATE_DIR / "specialist_health_last_alert_at",
        "wedge_state_path": _PANTHEON_STATE_DIR / "specialist_pane_wedge_first_seen_at",
        # T-0553: specialist launches bare `claude` with no --channels flag (confirmed
        # 2026-05-22; service unchanged since 2026-05-09). Specialist is never a
        # pantheon-bridge client; connected_clients absence is permanent, not a
        # fault. Bridge-connectivity dimensions (telegram_connected, connected_clients,
        # queued_messages) are meaningless for specialist. Set False to suppress false
        # specialist_disconnected alerts. Flip to True if specialist migrates onto
        # pantheon-bridge (see T-0554).
        "is_bridge_client": False,
    },
    "recovery": {
        # T-0559: Recovery self-monitor (Layer 1 of the T-0555 design). Recovery is the
        # recovery agent — if a Layer-1 self-inject misfires, there is NO
        # external recovery (Layer-2 watcher-of-the-watcher is unbuilt). The
        # no-backstop safety constraint requires Layer 1 ship in ALERT-ONLY
        # mode first; self-inject only becomes safe after a Layer-2 backstop
        # exists. The `alert_only` flag below routes Recovery through
        # detect+alert with NO send-keys auto-resolve.
        "pane_target": "claude-telegram-bot-fallback:0",
        "unix_user": "claude-fallback",
        "last_alert_path": _PANTHEON_STATE_DIR / "recovery_health_last_alert_at",
        "wedge_state_path": _PANTHEON_STATE_DIR / "recovery_pane_wedge_first_seen_at",
        # Recovery is bare-claude (no --channels), never a pantheon-bridge client —
        # mirror specialist's flag. Bridge-connectivity vocab won't fire.
        "is_bridge_client": False,
        # Detect wedges, alert the operator, do NOT send-keys to auto-resolve.
        "alert_only": True,
    },
}

# T-1036: the T-0213 sync-alert plumbing (PANTHEON_CONFIG_SYNC_ALERT_PATH +
# SELF_HEALING_SYNC_ALERT_CLASSES + check_pantheon_config_sync_alert) was retired
# here. T-0365 retired the 60s pantheon-config-sync timer (replaced by kino
# read-path pull-on-demand / syncGate), so nothing emits the sync-alert state
# file anymore. pantheon_config_sync.py itself is kept (kino's syncGate may use
# the sync tool); only the moot ALERT detection/routing is gone.
PANE_WEDGE_SENTINELS: tuple[str, ...] = (
    # ---------------------------------------------------------------------------
    # "Do you want to …" prefix family — LONGEST-PREFIX-FIRST within family
    # (T-0474: first match wins; longest-prefix-first surfaces the most-specific
    # sentinel for operator triage per HO resolution #4).
    # ---------------------------------------------------------------------------

    # WebFetch permission request header. Longer than sibling "allow this
    # connection?" so placed first. Source: WebFetchPermissionRequest.tsx; T-0473.
    "Do you want to allow Claude to fetch this content?",
    # Network/sandbox egress backup detector. Source: SandboxPermissionRequest.tsx;
    # T-0473.
    "Do you want to allow this connection?",
    # NotebookEdit (insert cell) dialog. Source: NotebookEditPermissionRequest.tsx;
    # T-0473.
    "Do you want to insert this cell into",
    # NotebookEdit (delete cell) dialog — DESTRUCTIVE. Source:
    # NotebookEditPermissionRequest.tsx; T-0473.
    "Do you want to delete this cell from",
    # Edit/Write permission dialog HEADER — generic catch for any per-file
    # edit prompt regardless of option-2 wording variant. T-0358 structural
    # fix: catch the dialog by its question text, not by trust-expansion text.
    "Do you want to make this edit to",
    # File-write (overwrite existing file) dialog. Distinct from "create" class
    # (new-file creation). Source: FileWritePermissionRequest.tsx; T-0473.
    "Do you want to overwrite",
    # Write/create-new-file permission dialog HEADER (sibling of Edit/Write).
    # 2026-05-20 msg_1637: Orchestrator wedged on `Do you want to create routing.yaml?`
    # during T-0468 reviewer cycle. Same dialog class as Edit/Write but for
    # new-file creation. Catches all "Do you want to create <X>?" prompts.
    "Do you want to create",
    # Destructive-Bash confirm dialog header (T-0142 / T-0201).
    # Broadest match; specific actions are NOT auto-resolved — escalate path
    # surfaces the actual content to the operator for review (option 2 expands trust,
    # arbitrary destructive intent needs human eyes).
    "Do you want to proceed?",

    # ---------------------------------------------------------------------------
    # "Yes, …" prefix family — LONGEST-PREFIX-FIRST within family (T-0474).
    # ---------------------------------------------------------------------------

    # Edit/Write permission dialog (Claude Code's per-file confirm).
    "Yes, and allow Claude to edit its own settings",
    # Edit/Write permission option-2 variant (newer Claude Code phrasing; T-0358
    # caught Orchestrator wedged 90min undetected because this wording wasn't here).
    "Yes, allow all edits during this session",
    # Option-2 prefix ("don't ask again" — expands trust scope; T-0142 / T-0201).
    # Detect but NEVER auto-resolve; always escalate to the operator.
    "Yes, and don't ask again for",
    # File-read out-of-cwd allow option. Source:
    # FilePermissionDialog/permissionOptions.tsx; T-0473.
    "Yes, allow reading from",
    # File-edit out-of-cwd allow option (trailing space distinguishes from
    # "Yes, allow all edits during this session" above).
    # Source: FilePermissionDialog/permissionOptions.tsx; T-0473.
    "Yes, allow all edits in ",
    # File-read in-cwd session-allow option. Source:
    # FilePermissionDialog/permissionOptions.tsx; T-0473.
    "Yes, during this session",
    # Workspace-trust current dialog: option-1 text. Note: also contains
    # "trust this folder" (existing entry below), but this longer entry surfaces
    # the more-specific class first per longest-prefix-first ordering.
    # Source: TrustDialog.tsx; T-0473.
    "Yes, I trust this folder",
    # Bypass-permissions option-label (backup for WARNING sentinel below).
    # Short sentinel; accepted false-positive risk documented in T-0474 tests.
    # Source: bypass-permissions confirmation (v2.1.74+), confirmed via CAO #119;
    # T-0473.
    "Yes, I accept",

    # ---------------------------------------------------------------------------
    # Workspace-trust dialog variants
    # ---------------------------------------------------------------------------

    # Workspace-trust legacy dialog header (still surfacing in some code paths;
    # issues #6797, #2147). Source: older TrustDialog.tsx code path; T-0473.
    "Do you trust the files in this folder?",
    # Workspace-trust body variant detector (belt-and-suspenders for the two
    # entries above). Source: TrustDialog.tsx; T-0473.
    "Quick safety check: Is this a project you created or one you trust?",
    # Session-startup folder-trust dialog. Claude Code prompts when starting
    # in a directory not in the trust allowlist. Caught Specialist wedged 2026-05-20
    # post-restart (the operator msg_1640 incident-adjacent). Broadest trust-dialog
    # catch; also sub-matches "Yes, I trust this folder" — harmless, the more-
    # specific entries above fire first.
    "trust this folder",

    # ---------------------------------------------------------------------------
    # Bypass-permissions confirmation (v2.1.74+)
    # ---------------------------------------------------------------------------

    # Bypass-permissions warning banner — UNSAFE; default-selected option is No.
    # Added v2.1.74; bit sibling harness CAO immediately (issue #119).
    # Source: bypass-permissions confirmation, confirmed verbatim via CAO #119;
    # T-0473.
    "WARNING: Claude Code running in Bypass Permissions mode",

    # ---------------------------------------------------------------------------
    # Managed-settings security dialog
    # ---------------------------------------------------------------------------

    # Managed-settings primary detector. Source: ManagedSettingsSecurityDialog.tsx;
    # T-0473.
    "Managed settings require approval",
    # Managed-settings body backup detector. Source: ManagedSettingsSecurityDialog.tsx;
    # T-0473.
    "Your organization has configured managed settings",

    # ---------------------------------------------------------------------------
    # Dev-channels startup confirm
    # ---------------------------------------------------------------------------

    # Dev-channels startup confirm — blocks fresh-Orchestrator on graceful restart.
    "I am using this for local development",

    # ---------------------------------------------------------------------------
    # Network / Sandbox egress
    # ---------------------------------------------------------------------------

    # Network-sandbox egress primary detector. Source: SandboxPermissionRequest.tsx;
    # T-0473.
    "Network request outside of sandbox",

    # ---------------------------------------------------------------------------
    # Plan-mode lifecycle (#16 "Here is Claude's plan:" DROPPED per HO resolution
    # #2 — also appears in non-modal transcript echo; no tiebreaker in matcher)
    # ---------------------------------------------------------------------------

    # ExitPlanMode dialog primary — 6-option semantic-permission menu. Active churn
    # in v2.1.110/118; UNSAFE (picks permission mode for rest of session).
    # Source: ExitPlanModePermissionRequest.tsx; T-0473.
    "Ready to code?",
    # ExitPlanMode dialog alt title (backup for "Ready to code?" above).
    # Source: ExitPlanModePermissionRequest.tsx; T-0473.
    "Exit plan mode?",
    # EnterPlanMode confirmation (mostly safe; read-only mode; detect and escalate
    # until operator ratifies auto-resolve). Source: EnterPlanModePermissionRequest.tsx;
    # T-0473.
    "Enter plan mode?",

    # ---------------------------------------------------------------------------
    # Skills / MCP / Computer Use
    # (#24 "Tool use" DROPPED per HO amendment — bare 8-char substring fires on
    # every TUI pane; title-pair AND-logic is out-of-scope matcher change.
    # Option-2 "Yes, and don't ask again for" already catches MCP dialogs above.)
    # ---------------------------------------------------------------------------

    # Skill permission dialog header. Source: SkillPermissionRequest.tsx; T-0473.
    'Use skill "',
    # Computer Use macOS permissions panel. Should not fire on Linux Pantheon;
    # treat as anomaly sentinel. Source: ComputerUseApproval/ComputerUseApproval.tsx;
    # T-0473.
    "Computer Use needs macOS permissions",
    # Computer Use app-allowlist dialog. Source:
    # ComputerUseApproval/ComputerUseApproval.tsx; T-0473.
    "Computer Use wants to control these apps",

    # ---------------------------------------------------------------------------
    # AskUserQuestion modal
    # ---------------------------------------------------------------------------

    # AskUserQuestion final-submit option. Short sentinel; accepted false-positive
    # risk documented in T-0474 tests. Source:
    # AskUserQuestionPermissionRequest/SubmitQuestionsView.tsx; T-0473.
    "Submit answers",
    # AskUserQuestion incomplete-warning (informational — Pantheon undersupplied
    # answers). Source: AskUserQuestionPermissionRequest/SubmitQuestionsView.tsx;
    # T-0473.
    "You have not answered all questions",
    # AskUserQuestion plan-mode escape hatch. Source: QuestionView.tsx; T-0473.
    "Skip interview and plan immediately",

    # ---------------------------------------------------------------------------
    # Hard-fail / dead-pane signals
    # ---------------------------------------------------------------------------

    # Root-refusal dead-pane: --dangerously-skip-permissions cannot run as root.
    # Pane is dead; process exited. Signal "pane needs operator restart".
    # Source: docs.claude.com/en/permission-modes; T-0473.
    "cannot be used with root/sudo privileges",
    # OAuth fallback code-paste prompt. Pane wedged waiting for OAuth code input.
    # Source: docs.claude.com/en/authentication; T-0473.
    "Pasted code here if prompted",

    # ---------------------------------------------------------------------------
    # Modal-dialog FOOTERS (T-0358 structural fix, option b): Claude Code's
    # bottom-bar text for blocking modals is stable across versions and prompt
    # types. Catching the footer catches any future modal-question-class
    # without per-prompt-text updates.
    # ---------------------------------------------------------------------------

    "Esc to cancel · Tab to amend",       # Edit/Write/Bash-confirm modals
    "Enter to confirm · Esc to cancel",   # Dev-channels-style yes/no modals
    # ManagedSettingsSecurityDialog uses '· Esc to exit' (not '· Esc to cancel').
    # Verbatim difference confirmed in source. Source: ManagedSettingsSecurityDialog.tsx;
    # T-0473.
    "Enter to confirm · Esc to exit",     # ManagedSettingsSecurityDialog footer
)

# T-0525: kickoff prompt chained after a STARTUP-DIALOG-class dismiss.
#
# A startup dialog (the dev-channels confirm, sentinel "I am using this for
# local development") is a reliable signal the daemon came up via a bare
# Restart=always respawn with NO graceful kickoff: dismissing it gets the
# session UP but IDLE (no HANDOFF read, no status post — the "up but no
# kickoff" symptom). So for THIS class only we chain: dismiss -> wait for the
# TUI idle prompt -> inject this kickoff prompt.
#
# Byte-identical to handoff_and_restart.sh's DEFAULT_KICK_PROMPT
# (HANDOFF_FILE=/home/claude/HANDOFF.md, step 8). Keep in sync if either changes.
RECOVERY_AUTO_RESOLVE_KICKOFF_PROMPT = (
    "Read /home/claude/HANDOFF.md, then post a one-paragraph status snapshot to"
    " the operator via your Telegram tool (chat_id 456474197). Then continue working"
    " immediately: pick up the handoff's queued next work (or your next ticket"
    " per standing priority) and execute — do not park after the snapshot. Begin."
)


# T-0479: AutoResolveRule — context-aware auto-resolve rule (posture C).
#
# Frozen dataclass: rules are immutable config; frozen=True enforces this at
# the type level and makes rules hashable / safe to share (P1: architecture
# over policy). Placed here (inbox_consumer.py) rather than _pane_predicates.py
# because it is inbox_consumer's own data model; placing it in the helper module
# would invert the dependency (utility module defining consumer's data model).
#
# Fields:
#   sentinel         — substring that triggers rule lookup (must also appear in
#                      PANE_WEDGE_SENTINELS per shape invariant)
#   predicate        — receives the full captured pane text; returns True if
#                      safe-to-auto-resolve in this specific instance
#   action           — tmux send-keys op sequence when predicate True
#   escalate_template— message rendered when predicate False; explains WHY this
#                      specific instance escalated (diagnostic for operator)
#   kickoff_prompt   — (T-0525) if set, after a successful dismiss the resolver
#                      polls the pane for the idle prompt then injects this text
#                      as a kickoff (cross-account). Default None = dismiss-only
#                      (the existing behavior for every other rule). Set ONLY on
#                      the startup-dialog rule — see RECOVERY_AUTO_RESOLVE_ALLOWLIST.
@dataclass(frozen=True)
class AutoResolveRule:
    sentinel: str
    predicate: Callable[[str], bool]
    action: list[str]
    escalate_template: str
    kickoff_prompt: str | None = None


# Auto-resolve allowlist per T-0142 ratified design (the operator msg_797 2026-05-09),
# extended to context-aware AutoResolveRule per T-0479 (posture C, 2026-05-20).
#
# Structure: dict[sentinel_str, AutoResolveRule]. Dict keyed by sentinel so
# lookup at the call-site remains O(1) (.get(matched_sentinel)) and existing
# key-iteration tests (for sentinel in RECOVERY_AUTO_RESOLVE_ALLOWLIST) continue
# to work without change.
#
# Flow: sentinel matched → get rule → call rule.predicate(pane_text):
#   True  → apply rule.action via _apply_auto_resolve_action + info Telegram
#   False → escalate with rule.escalate_template rendered as the diagnostic msg
#
# Adding a new rule requires the operator ratification per T-0142 §3. The shape:
# file a ticket proposing the sentinel + predicate + action, surface the
# rationale, wait for ratification before landing.
RECOVERY_AUTO_RESOLVE_ALLOWLIST: dict[str, AutoResolveRule] = {
    # Dev-channels confirm (fresh-Orchestrator post-graceful-restart). Option 1 ("I am
    # using this for local development") is safe + correct for our use case.
    # Today's T-0189 monitor fire (msg_959) triggered exactly this; Recovery
    # manually injected this sequence; codified here so next fire auto-handles.
    # Migrated to AutoResolveRule with always-True predicate (T-0479): behavior
    # is byte-identical — same action, same auto-resolve, same informational
    # Telegram. Ratification: T-0142, the operator msg_797 2026-05-09.
    "I am using this for local development": AutoResolveRule(
        sentinel="I am using this for local development",
        predicate=lambda _pane: True,
        action=["1", "Enter"],
        escalate_template=(
            "[recovery auto-resolve] sentinel='I am using this for local development'"
            " predicate=False (unexpected — always-True rule); pane_excerpt={excerpt!r}"
        ),
        # T-0525: STARTUP-DIALOG class ONLY. This sentinel is a reliable signal
        # the daemon came up via a bare Restart=always respawn with no graceful
        # kickoff — dismissing leaves it up-but-idle. Chain dismiss -> kickoff so
        # the recovered session reads its HANDOFF and posts status. Every other
        # rule below leaves kickoff_prompt=None (dismiss-only): those are
        # mid-session modals where the session already has its context.
        kickoff_prompt=RECOVERY_AUTO_RESOLVE_KICKOFF_PROMPT,
    ),

    # ---------------------------------------------------------------------------
    # BOUNDARY 1 — file-ops-in-cwd (T-0489, ratified T-0480 2026-05-20)
    #
    # Auto-resolve file create/edit/overwrite dialogs ONLY when the file path is
    # within Orchestrator's own cwd/worktree (path_in_agent_cwd). Escalate otherwise.
    # Option 1 ("Yes") is the in-scope action; option 2 (trust-expansion variants
    # "Yes, and don't ask again…", "allow Claude to edit its own settings") are
    # hard-deny and remain NOT in this allowlist.
    #
    # Predicate binds agent="orchestrator" at construction — check_orchestrator_health() is a
    # Orchestrator-specific monitor (hardcodes ORCHESTRATOR_PANE_TARGET, ORCHESTRATOR_* state paths);
    # no call-site signature change needed.
    # ---------------------------------------------------------------------------

    "Do you want to create": AutoResolveRule(
        sentinel="Do you want to create",
        predicate=lambda pane: path_in_agent_cwd(
            extract_file_dialog_path(pane) or "", "orchestrator"
        ),
        action=["1", "Enter"],
        escalate_template=(
            "[recovery auto-resolve] out-of-cwd file op"
            " — sentinel={sentinel!r}; excerpt={excerpt!r} — operator review"
        ),
    ),
    "Do you want to make this edit to": AutoResolveRule(
        sentinel="Do you want to make this edit to",
        predicate=lambda pane: path_in_agent_cwd(
            extract_file_dialog_path(pane) or "", "orchestrator"
        ),
        action=["1", "Enter"],
        escalate_template=(
            "[recovery auto-resolve] out-of-cwd file op"
            " — sentinel={sentinel!r}; excerpt={excerpt!r} — operator review"
        ),
    ),
    "Do you want to overwrite": AutoResolveRule(
        sentinel="Do you want to overwrite",
        predicate=lambda pane: path_in_agent_cwd(
            extract_file_dialog_path(pane) or "", "orchestrator"
        ),
        action=["1", "Enter"],
        escalate_template=(
            "[recovery auto-resolve] out-of-cwd file op"
            " — sentinel={sentinel!r}; excerpt={excerpt!r} — operator review"
        ),
    ),

    # ---------------------------------------------------------------------------
    # BOUNDARY 2 — trust-canonical-dirs (T-0489, ratified T-0480 2026-05-20)
    #
    # Auto-resolve workspace-trust dialogs ONLY when the folder path is a
    # canonical Pantheon dir (path_in_canonical_pantheon_dirs). Escalate otherwise.
    # Option 1 ("Yes, I trust this folder") is the trust-affirming action.
    #
    # Extractor note: extract_trust_dialog_path handles the first three sentinels
    # correctly (the question header "Do you trust…" appears before the path in the
    # pane, so the line-scan returns the path). For "Quick safety check…", the
    # extractor returns None (the header is not in _TRUST_DIALOG_SENTINELS; the
    # only matching line is the option-1 text "Yes, I trust this folder" which
    # appears AFTER the path, so no '/'-prefixed line follows it). None → "" →
    # path_in_canonical_pantheon_dirs("") = False → predicate False → escalate.
    # Safe degradation: unknown path → decline auto-trust → operator review.
    # ---------------------------------------------------------------------------

    "Do you trust the files in this folder?": AutoResolveRule(
        sentinel="Do you trust the files in this folder?",
        predicate=lambda pane: path_in_canonical_pantheon_dirs(
            extract_trust_dialog_path(pane) or ""
        ),
        action=["1", "Enter"],
        escalate_template=(
            "[recovery auto-resolve] unexpected folder — Recovery declines auto-trust"
            " — sentinel={sentinel!r}; excerpt={excerpt!r} — operator review"
        ),
    ),
    "Yes, I trust this folder": AutoResolveRule(
        sentinel="Yes, I trust this folder",
        predicate=lambda pane: path_in_canonical_pantheon_dirs(
            extract_trust_dialog_path(pane) or ""
        ),
        action=["1", "Enter"],
        escalate_template=(
            "[recovery auto-resolve] unexpected folder — Recovery declines auto-trust"
            " — sentinel={sentinel!r}; excerpt={excerpt!r} — operator review"
        ),
    ),
    "trust this folder": AutoResolveRule(
        sentinel="trust this folder",
        predicate=lambda pane: path_in_canonical_pantheon_dirs(
            extract_trust_dialog_path(pane) or ""
        ),
        action=["1", "Enter"],
        escalate_template=(
            "[recovery auto-resolve] unexpected folder — Recovery declines auto-trust"
            " — sentinel={sentinel!r}; excerpt={excerpt!r} — operator review"
        ),
    ),
    "Quick safety check: Is this a project you created or one you trust?": AutoResolveRule(
        sentinel="Quick safety check: Is this a project you created or one you trust?",
        predicate=lambda pane: path_in_canonical_pantheon_dirs(
            extract_trust_dialog_path(pane) or ""
        ),
        action=["1", "Enter"],
        escalate_template=(
            "[recovery auto-resolve] unexpected folder — Recovery declines auto-trust"
            " — sentinel={sentinel!r}; excerpt={excerpt!r} — operator review"
        ),
    ),
}

# Explicitly NOT in RECOVERY_AUTO_RESOLVE_ALLOWLIST (escalate-only sentinels):
#   - "Yes, and allow Claude to edit its own settings" (option 2 expands trust)
#   - "Do you want to proceed?" (destructive-Bash dialog; varies per command)
#   - "Yes, and don't ask again for" (option 2 prefix; expands trust)

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
}

IDLE_SENTINELS: tuple[str, ...] = (
    "? for shortcuts",        # idle, no task list
    "ctrl+t to hide tasks",   # idle, task list visible (Recovery's case — T-0065)
    # Add further idle footers here if new TUI states are discovered empirically.
)

PENDING_PASTE_SENTINEL = "[Pasted text"
INJECT_ENTER_DELAY = 0.7  # seconds — allows TUI paste-mode to settle before Enter

# T-0538: shared helper — single source of truth for submit-verify tmux injection.
_SEND_KEYS_HELPER = Path(__file__).resolve().parent / "send_keys_submit.sh"

# Sentinels that mark the TUI as ready to accept a typed kickoff (idle prompt).
# Superset of IDLE_SENTINELS plus the bare prompt glyph and the streaming/idle
# footer hints the drain uses ("❯|esc to interrupt|? for shortcuts|ctrl+t to
# hide tasks"). Any of these visible => the input box is live. (T-0525)
KICKOFF_IDLE_SENTINELS: tuple[str, ...] = (
    "❯",
    "esc to interrupt",
    "? for shortcuts",
    "ctrl+t to hide tasks",
)

# Bounded poll for the idle prompt after a startup-dialog dismiss. Module-level
# so tests can patch them to keep the suite from actually sleeping ~30s. (T-0525)
KICKOFF_POLL_TIMEOUT_SECONDS = 30.0
KICKOFF_POLL_INTERVAL_SECONDS = 2.0

# ---------------------------------------------------------------------------
# Bootstrap: make `from tools.ticket import ...` work when invoked as a script.
# Same pattern as tools/ticket.py lines 40-41.
# ---------------------------------------------------------------------------

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.ticket import TICKETS_ROOT, parse_ticket  # noqa: E402
from tools.agent_role_map import resolve_role  # noqa: E402
from tools._git_utils import pull_repo_or_warn  # noqa: E402
from tools._pane_predicates import (  # noqa: E402
    path_in_agent_cwd,
    path_in_canonical_pantheon_dirs,
    extract_file_dialog_path,
    extract_trust_dialog_path,
)


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


# ---------------------------------------------------------------------------
# Gating checks.
# ---------------------------------------------------------------------------


def check_autonomy_off() -> None:
    if AUTONOMY_OFF_PATH.exists():
        raise SkipCycle("off_switch")


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
    """Bail if last Telegram inbound to this agent was <5 min ago.

    Connection errors are treated as non-bail: bridge being down should not
    freeze ticket delivery (fail-open; see DECISIONS).
    """
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return  # fail-open: can't check, proceed

    last_inbound_at: dict[str, str | None] = data.get("last_inbound_at", {})
    ts_str = last_inbound_at.get(agent)
    if ts_str is None:
        return  # no inbound ever; proceed

    try:
        ts = datetime.fromisoformat(ts_str)
        now = datetime.now(timezone.utc)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_seconds = (now - ts).total_seconds()
    except (ValueError, TypeError):
        return  # malformed timestamp; proceed

    if age_seconds < RECENT_INBOUND_THRESHOLD_SECONDS:
        raise SkipCycle("recent_telegram")


# check_mid_turn removed by T-0717: dispatch-gating pane scrape replaced by
# the auto_delivered-stamp mutex (check_dispatch_locked). The per-turn tmux
# sentinel was the only caller of this function in the main() cycle; the
# wedge-health-detection path (_check_pane_wedge_persistent_for_agent,
# ORCHESTRATOR/SPECIALIST/RECOVERY_HEALTH_PANE_WEDGE) is a distinct mechanism kept intact.
# Per msg_2364 alpha-removal preference: orphaned code deleted outright.


# ---------------------------------------------------------------------------
# Ticket listing + filtering.
# ---------------------------------------------------------------------------


def _ticket_state_on_disk(ticket_id: str) -> str | None:
    """Return the state of a ticket by scanning the tickets/ tree."""
    tid_clean = ticket_id.strip()
    if not re.fullmatch(r"T-\d{4}", tid_clean):
        return None
    fname = f"{tid_clean}.md"
    for state_dir in ("inbox", "active", "done"):
        candidate = TICKETS_ROOT / state_dir / fname
        if candidate.exists():
            return state_dir
    return None


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

    found_any = False
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
        role = resolve_role(agent)
        target = fields.get("target")
        if target != agent and target != role:
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

        deps_done = all(
            _ticket_state_on_disk(dep) == "done" for dep in depends_on
        )
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

    if not found_any:
        raise SkipCycle("empty_inbox")
    if eligible:
        return eligible
    if dep_blocked:
        raise SkipCycle("dependency_blocked")
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
    dispatch and must not hold the dispatch mutex. Three exclusion arms:

      1. dep-blocked    — an unfinished dependency in depends_on.
      2. calendar-gated — not_before is in the future.
      3. interactive-required — labelled interactive-required (parked/deferred); such a
         ticket is never auto-deliverable (the consumer routes it to interactive_locked,
         never auto-injects), so it can never be a live auto-delivered dispatch.

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
    if not all(_ticket_state_on_disk(dep) == "done" for dep in depends_on):
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
    return False


def _list_auto_delivered_unresolved_for_agent(agent: str) -> list[TicketInfo]:
    """Return all unresolved, *in-flight* tickets (inbox/ or active/) for agent with
    auto_delivered: true.

    "In-flight" excludes dep-blocked / not_before-unmet tickets (T-0864): a ticket that
    cannot currently be worked is not an in-flight dispatch and must not hold the mutex.

    Used by check_dispatch_locked() to implement the per-agent dispatch-inflight
    mutex. Scans both inbox/ and active/ directories; done/ is excluded (resolved
    tickets release the lock naturally).

    Matches on target field (persona name or role ID via resolve_role), mirroring
    list_eligible_tickets(). Fail-open: parse errors skip the ticket.
    """
    role = resolve_role(agent)
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
            target = fields.get("target")
            if target != agent and target != role:
                continue
            # Check auto_delivered frontmatter field (string "true" or bool True).
            ad = fields.get("auto_delivered")
            if str(ad).lower() != "true":
                continue
            # T-0864/T-0981: a non-workable auto_delivered ticket (dep-blocked,
            # not_before-unmet, or interactive-required) is NOT in-flight, so exclude
            # it from the mutex — else it silently holds the lock and suppresses ALL
            # inbox delivery (the T-0768 regression class).
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
            ))
    return results


def _is_auto_delivered_stale(ticket: TicketInfo, threshold_seconds: int) -> bool:
    """Return True iff the ticket's liveness signal is older than threshold_seconds.

    Liveness = max(transition-log timestamp, ticket-file mtime). Taking the max
    catches tickets actively worked for hours with no state transitions (the file
    gets touched on each agent edit even without a formal transition). Returns
    False (not stale) when the transition log is absent or unparseable — skip-on-
    None: cannot prove staleness, so don't claim it.

    Residual risk: a ticket with zero file touches AND no transitions could trip
    the backstop on a genuinely long active ticket. v1 mitigation: operator-alert
    (human in loop). See AUTO_DELIVERED_STALE_SECONDS docstring for TODO.
    """
    now = datetime.now(timezone.utc)

    # Arm 1: transition-log timestamp.
    log_ts = _last_transition_ts(ticket.body)
    if log_ts is None:
        return False  # skip-on-None: cannot prove staleness
    if log_ts.tzinfo is None:
        log_ts = log_ts.replace(tzinfo=timezone.utc)

    # Arm 2: file mtime (catches ticket-file touches without formal transitions).
    try:
        mtime_epoch = ticket.path.stat().st_mtime
        file_ts = datetime.fromtimestamp(mtime_epoch, tz=timezone.utc)
    except OSError:
        file_ts = log_ts  # fall back to log ts if stat fails

    # Use the more-recent of the two as the liveness signal.
    liveness_ts = max(log_ts, file_ts)
    return (now - liveness_ts).total_seconds() > threshold_seconds


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
    inflight = _list_auto_delivered_unresolved_for_agent(agent)
    if not inflight:
        return  # no lock — proceed

    # Check each in-flight ticket for staleness.
    stale_tickets = []
    fresh_tickets = []
    for ticket in inflight:
        if _is_auto_delivered_stale(ticket, AUTO_DELIVERED_STALE_SECONDS):
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
        raise SkipCycle("dispatch_locked")


def _stamp_auto_delivered(ticket: TicketInfo) -> bool:
    """Write auto_delivered: true + auto_delivered_at: <ISO-ts> to ticket frontmatter.

    Split failure contract (two distinct failure classes):

    1. WRITE FAILURE → returns False (fail-CLOSED for caller).
       If the frontmatter write does not persist to disk, the mutex stamp is
       absent — a subsequent consumer cycle will not see the lock and WILL
       deliver another ticket. Caller (main()) must NOT inject on False; it
       should raise SkipCycle('stamp_write_failed') and retry next cycle.

    2. COMMIT / PUSH FAILURE → returns True (best-effort; inject proceeds).
       The local file-write already establishes the lock for THIS agent's own
       consumer (it reads its own working tree), so the mutex is intact locally
       even if the commit or push fails. Inject should happen. Failures are
       logged prominently to stderr. Push is needed for HO's ff-only pulls and
       cross-agent visibility; a push failure is notable but not fatal to
       this-cycle delivery correctness.

    Returns True if the frontmatter write succeeded (inject may proceed),
    False if the write failed (inject must NOT proceed).
    """
    from tools.ticket import parse_front_matter, _build_ticket_text
    from tools._git_utils import REPO_ROOT

    try:
        text = ticket.path.read_text(encoding="utf-8")
        fields, body = parse_front_matter(text)
        fields["auto_delivered"] = "true"
        fields["auto_delivered_at"] = _now_iso()
        new_text = _build_ticket_text(fields, body)
        ticket.path.write_text(new_text, encoding="utf-8")
    except Exception as exc:
        print(f"[inbox_consumer] _stamp_auto_delivered write failed for {ticket.id}: {exc}",
              file=sys.stderr)
        return False  # fail-CLOSED: caller must not inject

    # Commit + push via sg pantheon-shared (T-0514 group fix: daemon session
    # may lack pantheon-shared supplementary group; wrap git writes in sg).
    # Commit/push failures are best-effort: the local write established the
    # mutex, so this-cycle delivery is safe. Log prominently but return True.
    try:
        rel_path = ticket.path.relative_to(REPO_ROOT)
    except ValueError:
        rel_path = ticket.path

    commit_msg = f"tickets: stamp auto_delivered on {ticket.id}"
    # git add
    add_result = subprocess.run(
        ["sg", "pantheon-shared", "-c",
         f"git -C {REPO_ROOT} add {rel_path}"],
        capture_output=True, text=True, check=False,
    )
    if add_result.returncode != 0:
        print(f"[inbox_consumer] _stamp_auto_delivered git add failed (stamp written locally;"
              f" push skipped): {add_result.stderr}", file=sys.stderr)
        return True  # write succeeded; inject should proceed

    # git commit
    commit_result = subprocess.run(
        ["sg", "pantheon-shared", "-c",
         f"git -C {REPO_ROOT} commit -m {subprocess.list2cmdline([commit_msg])}"],
        capture_output=True, text=True, check=False,
    )
    if commit_result.returncode != 0:
        print(f"[inbox_consumer] _stamp_auto_delivered git commit failed (stamp written locally;"
              f" push skipped): {commit_result.stderr}", file=sys.stderr)
        return True  # write succeeded; inject should proceed

    # git push with retry on non-ff (fetch + rebase, up to 3 attempts)
    from tools._git_utils import push_with_retry
    ok, detail = push_with_retry(REPO_ROOT)
    if not ok:
        print(f"[inbox_consumer] _stamp_auto_delivered push failed (stamp committed locally;"
              f" HO pull may need manual ff-only merge): {detail}", file=sys.stderr)
    return True  # write succeeded; inject should proceed


def _clear_auto_delivered(ticket: TicketInfo) -> bool:
    """Remove auto_delivered + auto_delivered_at from ticket frontmatter (genuine lock release).

    Called on staleness-backstop fire so "Lock released" is true on disk: without
    this, the stamp persisted and every subsequent 10-min consumer cycle re-detected
    the same stale ticket and re-alerted (the ~30-copy spam, msg_3073). Clearing the
    stamp makes the alert idempotent — it fires once, on the cycle that first detects
    staleness.

    Best-effort: returns True if the frontmatter write persisted (stamp gone locally,
    so this consumer won't re-alert), False if the write failed (stamp remains; next
    cycle will re-alert + retry — self-healing, no worse than the pre-fix behavior).
    Commit/push failures are non-fatal (local write already suppresses re-alert for
    this agent's own working-tree reads); logged to stderr.
    """
    from tools.ticket import parse_front_matter, _build_ticket_text
    from tools._git_utils import REPO_ROOT, push_with_retry

    try:
        text = ticket.path.read_text(encoding="utf-8")
        fields, body = parse_front_matter(text)
        fields.pop("auto_delivered", None)
        fields.pop("auto_delivered_at", None)
        new_text = _build_ticket_text(fields, body)
        ticket.path.write_text(new_text, encoding="utf-8")
    except Exception as exc:
        print(f"[inbox_consumer] _clear_auto_delivered write failed for {ticket.id}: {exc}",
              file=sys.stderr)
        return False  # stamp remains; next cycle re-alerts + retries (self-healing)

    try:
        rel_path = ticket.path.relative_to(REPO_ROOT)
    except ValueError:
        rel_path = ticket.path
    commit_msg = f"tickets: clear stale auto_delivered on {ticket.id} (lock release)"
    add_result = subprocess.run(
        ["sg", "pantheon-shared", "-c", f"git -C {REPO_ROOT} add {rel_path}"],
        capture_output=True, text=True, check=False,
    )
    if add_result.returncode != 0:
        print(f"[inbox_consumer] _clear_auto_delivered git add failed (cleared locally;"
              f" push skipped): {add_result.stderr}", file=sys.stderr)
        return True
    commit_result = subprocess.run(
        ["sg", "pantheon-shared", "-c",
         f"git -C {REPO_ROOT} commit -m {subprocess.list2cmdline([commit_msg])}"],
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
    ask = _ask_body(ticket.body)
    base = (
        INJECT_PROMPT
        .replace("T-NNNN", ticket.id)
        .replace("<title>", ticket.title)
        .replace("<agent>", agent)
        .replace("<body>", ask)
    )
    # T-1103 / ORG-034: when the ticket carries a serves: arc-intent, prepend it
    # so the worker sees WHY before the body. Conditional prepend keeps the
    # byte-exact INJECT_PROMPT constant unchanged (the constant guards autonomy-
    # policy text, not metadata — changing it would break test_inject_prompt_matches_locked_text).
    if ticket.serves:
        return f"Serves: {ticket.serves}\n{base}"
    return base


def inject(tmux_target: str, prompt: str) -> None:
    """Inject prompt into tmux target and verify submission.

    T-0538: delegates to tools/send_keys_submit.sh (single source of truth for
    submit-verify tmux injection). The helper handles text send, paste-mode
    settle delay, Enter send, and submit verification with retry. Any failure
    — including unconfirmed submit after retries — maps to SkipCycle("tmux_unreachable").
    A distinct skip vocab for pending-paste is a possible future enhancement
    (out of scope for this DRY refactor; would require registering across skip-vocab
    handling / NEVER_ALERT sets / consecutive-skip logic).
    """
    result = subprocess.run(
        [str(_SEND_KEYS_HELPER), tmux_target, prompt],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "INJECT_ENTER_DELAY": str(INJECT_ENTER_DELAY)},
    )
    if result.returncode != 0:
        raise SkipCycle("tmux_unreachable")


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
        state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
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

    role = resolve_role(agent)
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
        if target != agent and target != role:
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
    ask = _ask_body(ticket.body)
    return (
        ACTIVE_SURFACE_PROMPT
        .replace("T-NNNN", ticket.id)
        .replace("<title>", ticket.title)
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

    Returns a log vocab string "active_surface:{tid}" on successful inject,
    or None if nothing to surface (no tickets, all rate-limited, inject failed).

    Fail-open on inject errors — a surface failure must not break the skip path.
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
            inject(tmux_target, prompt)
        except SkipCycle:
            return None  # inject failed (tmux issue); skip quietly

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


def _send_telegram_alert(text: str) -> None:
    """Direct Bot API alert — mirrors usage_monitor.py send_telegram pattern."""
    if _live_telegram_suppressed():
        print(
            f"[inbox_consumer] test context — live telegram suppressed: {text[:80]!r}",
            file=sys.stderr,
        )
        return
    token = _read_telegram_token()
    if not token:
        print("[inbox_consumer] no telegram token; alert NOT sent", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({"chat_id": OPERATOR_CHAT_ID, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if not (200 <= resp.status < 300):
                print(f"[inbox_consumer] telegram alert returned {resp.status}", file=sys.stderr)
    except (urllib.error.URLError, OSError) as e:
        print(f"[inbox_consumer] telegram alert failed: {e}", file=sys.stderr)


def _send_to_recovery_pane(text: str) -> bool:
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
    """
    if _live_telegram_suppressed():
        print(
            f"[inbox_consumer] test context — recovery-pane injection suppressed: {text[:80]!r}",
            file=sys.stderr,
        )
        return True
    recovery_target = _resolve_tmux_target("recovery")
    result = subprocess.run(
        [str(_SEND_KEYS_HELPER), recovery_target, text],
        capture_output=True, text=True, check=False,
        env={**os.environ, "INJECT_ENTER_DELAY": str(INJECT_ENTER_DELAY)},
    )
    return result.returncode == 0


def count_consecutive_skips(agent: str) -> int:
    """Count consecutive skip entries for agent from the tail of the log.

    Scans backwards; resets on `pulled` entry, non-matching-agent entry, or EOF.
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
        if outcome.startswith("skip:"):
            count += 1
        elif outcome.startswith("pulled"):
            break
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
        msg = (
            f"[pantheon] inbox-consumer alert: {agent} has skipped"
            f" {count} consecutive cycles (last: skip:{skip_vocab})."
            f" Check ~/.local/log/pantheon/inbox_consumer.log for details."
        )
        # T-0364: route via Recovery's pane first, audit-Telegram on success,
        # fallback-Telegram on recovery-pane failure.
        route_failure_alert(agent, msg, vocab=f"consecutive-skip ({agent})")  # T-0722
        # Append a reset marker so the next skip starts a fresh count.
        log_outcome(agent, "skip:_alert_reset")


# ---------------------------------------------------------------------------
# Recovery-side Orchestrator health check (piggybacks Recovery's consumer cycle).
# ---------------------------------------------------------------------------


def _orchestrator_health_alert_throttled() -> bool:
    if not ORCHESTRATOR_HEALTH_LAST_ALERT_PATH.exists():
        return False
    try:
        last = int(ORCHESTRATOR_HEALTH_LAST_ALERT_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return (int(time.time()) - last) < ORCHESTRATOR_HEALTH_ALERT_COOLDOWN_SECONDS


def _orchestrator_health_record_alert() -> None:
    ORCHESTRATOR_HEALTH_LAST_ALERT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ORCHESTRATOR_HEALTH_LAST_ALERT_PATH.write_text(str(int(time.time())), encoding="utf-8")


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


def _orchestrator_pane_capture() -> str | None:
    """Backward-compat alias — capture Orchestrator's pane. Delegates to _pane_capture."""
    return _pane_capture("orchestrator")


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


def _pane_wedge_match(pane: str) -> str | None:
    for s in PANE_WEDGE_SENTINELS:
        if s in pane:
            return s
    return None


# T-0716: liveness signals meaning the loop is NOT blocked on a modal — the live
# bottom status line shows "esc to interrupt" (actively generating) or an idle
# prompt hint. A genuine blocking modal replaces this region with a dialog
# footer instead. Bare "❯" is deliberately EXCLUDED (a modal's selection cursor
# uses it too, so it cannot discriminate). Overlaps KICKOFF_IDLE_SENTINELS minus
# that ambiguous glyph.
_WEDGE_LIVENESS_SENTINELS: tuple[str, ...] = (
    "esc to interrupt",      # actively generating
    "? for shortcuts",       # idle at prompt, no task list
    "ctrl+t to hide tasks",  # idle at prompt, task list visible
)

# T-0716: modal-dialog footers (the T-0358 footer family, mirrored from
# PANE_WEDGE_SENTINELS lines ~374-379). Their presence in the bottom region is a
# POSITIVE witness that a blocking modal owns the bottom of the pane. When a
# footer is present we declare wedged even if a (stale) liveness token also
# rendered, so a genuine modal is never suppressed.
_WEDGE_DIALOG_FOOTERS: tuple[str, ...] = (
    "Esc to cancel · Tab to amend",
    "Enter to confirm · Esc to cancel",
    "Enter to confirm · Esc to exit",
)

# Trailing NON-BLANK lines treated as the live "bottom region". tmux
# capture-pane -p pads to terminal height with blanks, so blanks are filtered
# first. 6 ≈ Claude Code's working status block height (spinner / tip / borders /
# prompt / "esc to interrupt") — tight enough to exclude a peer dialog sitting in
# scrollback above the live prompt.
_WEDGE_BOTTOM_REGION_LINES = 6


def _bottom_region_shows_live_input(pane: str) -> bool:
    """True iff the pane's live bottom region shows the loop is NOT blocked on a
    modal — a liveness sentinel is present in the last _WEDGE_BOTTOM_REGION_LINES
    non-blank lines AND no modal footer is present there.

    T-0716: suppresses wedge FALSE-POSITIVES where a wedge sentinel appears only
    as TEXT — a captured PEER dialog (Recovery's core remediation role echoes other
    agents' dialogs into its own pane) or the health-alert message quoting the
    sentinel verbatim — while the agent is actually working/idle.

    False-negative-safe: suppression requires BOTH positive liveness evidence
    AND the absence of a modal footer, so a genuine modal (footer present, or at
    minimum no liveness token) is never suppressed.
    """
    non_blank = [ln for ln in pane.splitlines() if ln.strip()]
    bottom = "\n".join(non_blank[-_WEDGE_BOTTOM_REGION_LINES:])
    has_liveness = any(s in bottom for s in _WEDGE_LIVENESS_SENTINELS)
    has_footer = any(f in bottom for f in _WEDGE_DIALOG_FOOTERS)
    return has_liveness and not has_footer


def _check_pane_wedge_persistent() -> tuple[str, str] | None:
    """Detect a wedge sentinel that has persisted ≥ persist threshold.

    Persistence requirement: a brief permission dialog that resolves quickly
    is normal operation. Only alert when the dialog has been visible across
    at least one full Recovery-cycle gap (the timer is OnUnitActiveSec=10min)
    AND for ≥ ORCHESTRATOR_HEALTH_PANE_WEDGE_PERSIST_SECONDS (120s) — i.e., Orchestrator's
    loop genuinely is not advancing past it.

    The 120s floor is cadence-independent (T-0527): at the current 10min
    cadence the 2nd sighting always clears it, so the binding constraint
    today is the once-per-cycle gap, not the 120s value. The floor stays
    rather than being dropped as redundant so that a future shorter cadence
    cannot silently weaken the persistence guarantee — it is exercised by
    test_persistent_wedge_returns_sentinel and its under-threshold sibling.

    State file: timestamp of first sighting in current wedged streak. Cleared
    on any pane capture that doesn't match a sentinel. Returns
    (matched_sentinel, pane_text) when persistent, else None.

    The pane_text is threaded through to the call-site (T-0479) so that
    AutoResolveRule.predicate receives the full captured text without a
    second tmux capture-pane call. Single-capture-point discipline.
    """
    pane = _orchestrator_pane_capture()
    state_path = ORCHESTRATOR_HEALTH_PANE_WEDGE_STATE_PATH

    if pane is None:
        # Pane unreachable: clear state (we can't claim wedged or healthy).
        try:
            state_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    matched = _pane_wedge_match(pane)
    if matched is None:
        try:
            state_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    now = int(time.time())
    if not state_path.exists():
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(str(now), encoding="utf-8")
        return None  # first sighting; require persistence

    try:
        first_seen = int(state_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        # Corrupt state file — treat as first sighting.
        state_path.write_text(str(now), encoding="utf-8")
        return None

    if now - first_seen >= ORCHESTRATOR_HEALTH_PANE_WEDGE_PERSIST_SECONDS:
        # T-0716: final liveness guard. Suppress if the live bottom region shows
        # the loop working/idle (the sentinel was present only as TEXT — e.g. a
        # captured peer dialog or the alert quoting the sentinel). Applied HERE,
        # not in _pane_wedge_match, so a transient/stale liveness sighting defers
        # THIS cycle's declaration WITHOUT resetting the persistence streak (the
        # streak is driven purely by sentinel presence; the dialog must clear the
        # sentinel to reset the clock).
        if _bottom_region_shows_live_input(pane):
            return None
        return (matched, pane)
    return None


def _apply_resolve_action(
    tmux_target: str, unix_user: str, action: list[str]
) -> bool:
    """Send a sequence of tmux send-keys ops to the target pane.

    T-0541: parameterized replacement for _apply_auto_resolve_action().
    Accepts explicit unix_user so Recovery can drive dialogs on any agent's pane
    cross-account (claude for Orchestrator, claude-research for Specialist).

    Each item in action is either a literal string (sent as text) or a special
    key name ("Enter", "Escape", "C-c", etc. — passed through to tmux as-is).
    Returns True iff every send-keys call exited 0.

    CROSS-ACCOUNT: `$(id -u)` inside the bash -c string resolves to the target
    user's uid (because sudo switches user before the shell expands it), giving
    the correct XDG_RUNTIME_DIR for that user's tmux server. Same pattern as
    _pane_capture. Bug history: T-0201 / msg_1593.

    PASTE-MODE: sleep 0.7s between ops (same threshold inject() uses).
    """
    for op in action:
        try:
            shell_cmd = (
                f"XDG_RUNTIME_DIR=/run/user/$(id -u) "
                f"tmux send-keys -t {tmux_target} {subprocess.list2cmdline([op])}"
            )
            res = subprocess.run(
                ["sudo", "-u", unix_user, "bash", "-c", shell_cmd],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        if res.returncode != 0:
            return False
        # Paste-mode race protection (mirrors inject() INJECT_ENTER_DELAY).
        time.sleep(0.7)
    return True


def _apply_auto_resolve_action(
    tmux_target: str, action: list[str]
) -> bool:
    """Send a sequence of tmux send-keys ops to the target pane.

    Backward-compat wrapper — delegates to _apply_resolve_action with
    unix_user="claude" (Orchestrator's account). Preserved for callers/tests that
    reference _apply_auto_resolve_action directly (T-0201 history, T-0479).

    CROSS-ACCOUNT: target panes live on the `claude` user's tmux server (not
    Recovery's). Without sudo wrapper, plain `tmux send-keys -t claude-telegram-bot:0`
    runs against Recovery's OWN tmux server and prefix-matches Recovery's session
    `claude-telegram-bot-fallback`, silently typing into Recovery's pane instead
    of Orchestrator's. Wrap with `sudo -u claude bash -c 'XDG_RUNTIME_DIR=... tmux ...'`
    to actually reach Orchestrator's tmux. Bug surfaced 2026-05-19 (the operator msg_1593).

    PASTE-MODE: tmux's TUI paste-mode heuristic batches successive send-keys
    calls; without a delay between text and Enter, the Enter gets swallowed
    and the "1" sits in the input buffer without being submitted. Sleep 0.7s
    between ops (same threshold inject() + send_to_recovery_pane use).
    """
    return _apply_resolve_action(tmux_target, "claude", action)


def _poll_orchestrator_pane_for_idle(dialog_sentinel: str) -> bool:
    """Poll Orchestrator's pane until it reaches the idle prompt after a dismiss.

    T-0525: after the dismiss keystrokes, claude boots past the dialog (channels
    bridge init etc.) before the TUI input box is ready to accept typed input.
    Injecting too early loses the kickoff (same fragility class as the reverted
    launch-arg kick, T-0519). So poll the pane (cross-account capture, reusing
    _orchestrator_pane_capture) on a bounded loop until it shows an idle-prompt sentinel
    AND no longer shows the dialog sentinel.

    Returns True iff the idle prompt appeared (and the dialog cleared) within
    KICKOFF_POLL_TIMEOUT_SECONDS. Returns False on timeout — the caller does a
    best-effort kickoff inject anyway and surfaces the timeout in the log/audit.

    Bounds (KICKOFF_POLL_TIMEOUT_SECONDS / KICKOFF_POLL_INTERVAL_SECONDS) are
    module-level so tests patch them to avoid real sleeps.
    """
    deadline = time.monotonic() + KICKOFF_POLL_TIMEOUT_SECONDS
    while True:
        pane = _orchestrator_pane_capture()
        if pane is not None:
            dialog_gone = dialog_sentinel not in pane
            idle_visible = any(s in pane for s in KICKOFF_IDLE_SENTINELS)
            if dialog_gone and idle_visible:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(KICKOFF_POLL_INTERVAL_SECONDS)


def _poll_pane_for_idle(agent: str, dialog_sentinel: str) -> bool:
    """Poll the given agent's pane until it reaches idle after a dismiss.

    T-0541: parameterized replacement for _poll_orchestrator_pane_for_idle(). Uses
    _pane_capture(agent) so any agent's pane can be polled cross-account.

    Returns True iff the idle prompt appeared (and dialog cleared) within
    KICKOFF_POLL_TIMEOUT_SECONDS.
    """
    deadline = time.monotonic() + KICKOFF_POLL_TIMEOUT_SECONDS
    while True:
        pane = _pane_capture(agent)
        if pane is not None:
            dialog_gone = dialog_sentinel not in pane
            idle_visible = any(s in pane for s in KICKOFF_IDLE_SENTINELS)
            if dialog_gone and idle_visible:
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(KICKOFF_POLL_INTERVAL_SECONDS)


def _check_pane_wedge_persistent_for_agent(agent: str) -> tuple[str, str] | None:
    """Detect a wedge sentinel that has persisted ≥ persist threshold for agent.

    T-0541: parameterized replacement for _check_pane_wedge_persistent(). Uses
    _pane_capture(agent) and per-agent wedge_state_path from _AGENT_HEALTH_CONFIG.

    Returns (matched_sentinel, pane_text) when persistent, else None.
    Unknown agents return None (fail-open).
    """
    cfg = _AGENT_HEALTH_CONFIG.get(agent)
    if cfg is None:
        return None

    state_path: Path = cfg["wedge_state_path"]
    pane = _pane_capture(agent)

    if pane is None:
        try:
            state_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    matched = _pane_wedge_match(pane)
    if matched is None:
        try:
            state_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None

    now = int(time.time())
    if not state_path.exists():
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(str(now), encoding="utf-8")
        return None  # first sighting; require persistence

    try:
        first_seen = int(state_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        state_path.write_text(str(now), encoding="utf-8")
        return None

    if now - first_seen >= ORCHESTRATOR_HEALTH_PANE_WEDGE_PERSIST_SECONDS:
        # T-0716: final liveness guard (see _check_pane_wedge_persistent). Same
        # discrimination for the per-agent path that drives recovery-health — the
        # exact path that false-fired 2026-06-02.
        if _bottom_region_shows_live_input(pane):
            return None
        return (matched, pane)
    return None


def _auto_resolve_if_allowlisted_for_agent(
    matched_sentinel: str, pane_text: str, agent: str
) -> str | None:
    """Apply the allowlisted auto-resolve for matched_sentinel for the given agent.

    T-0541: parameterized replacement for _auto_resolve_if_allowlisted(). Resolves
    pane_target and unix_user from _AGENT_HEALTH_CONFIG[agent] so the send-keys
    action targets the correct agent's tmux pane cross-account.

    Behavior is identical to _auto_resolve_if_allowlisted for agent="orchestrator".
    For file-op rules whose predicates bind agent="orchestrator", a non-orchestrator agent's
    out-of-home paths will return predicate False → escalate (Option C, T-0541).
    Trust-folder rules use path_in_canonical_pantheon_dirs which covers all agents.
    """
    cfg = _AGENT_HEALTH_CONFIG.get(agent)
    if cfg is None:
        return None
    pane_target: str = cfg["pane_target"]
    unix_user: str = cfg["unix_user"]
    wedge_state_path: Path = cfg["wedge_state_path"]

    rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST.get(matched_sentinel)
    if rule is None or rule.predicate(pane_text) is not True:
        return None
    if not _apply_resolve_action(pane_target, unix_user, rule.action):
        return None  # send-keys failed; caller escalates — no kickoff attempted
    try:
        wedge_state_path.unlink(missing_ok=True)
    except OSError:
        pass

    # T-0525: STARTUP-DIALOG class only — chain dismiss -> kickoff inject.
    kickoff_note = ""
    if rule.kickoff_prompt is not None:
        idle_ready = _poll_pane_for_idle(agent, matched_sentinel)
        if not idle_ready:
            log_outcome(
                "recovery",
                f"agent_health:kickoff_idle_poll_timeout"
                f":agent={agent}:sentinel={matched_sentinel!r}:best_effort_inject",
            )
        kickoff_sent = _apply_resolve_action(
            pane_target, unix_user, [rule.kickoff_prompt, "Enter"]
        )
        if idle_ready and kickoff_sent:
            kickoff_note = " + kickoff injected (idle confirmed)"
        elif kickoff_sent:
            kickoff_note = " + kickoff injected (best-effort, idle poll timed out)"
        else:
            kickoff_note = " + kickoff inject FAILED (send-keys error)"
            log_outcome("recovery", f"agent_health:kickoff_inject_send_keys_failed:agent={agent}")

    _send_telegram_info(
        f"[recovery auto-resolve] agent={agent} sentinel={matched_sentinel!r}"
        f" action={rule.action}{kickoff_note} pane={pane_target} at {_now_iso()}"
    )
    return "pane_wedge_auto_resolved"


def _auto_resolve_if_allowlisted(matched_sentinel: str, pane_text: str) -> str | None:
    """Apply the allowlisted auto-resolve for matched_sentinel, if eligible.

    Eligible = sentinel is in RECOVERY_AUTO_RESOLVE_ALLOWLIST AND its predicate
    returns True for pane_text AND the send-keys action succeeds. On success:
    clears the wedge-streak state, emits the audit Telegram, returns
    "pane_wedge_auto_resolved". Otherwise returns None and the caller decides
    how to escalate (the allowlist envelope is unchanged — same rules, same
    predicates, same actions).

    Shared by the pane-wedge branch and the orchestrator_disconnected branch (T-0520):
    a startup dialog (e.g. the dev-channels confirm) that blocks Orchestrator's loop
    from connecting surfaces as "not in connected_clients", which preempted the
    pane-wedge auto-resolve and mis-recommended a restart. Centralizing the
    gate+apply+audit here lets both branches resolve such dialogs identically.
    """
    rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST.get(matched_sentinel)
    if rule is None or rule.predicate(pane_text) is not True:
        return None
    if not _apply_auto_resolve_action(ORCHESTRATOR_PANE_TARGET, rule.action):
        return None  # send-keys failed; caller escalates — no kickoff attempted
    try:
        ORCHESTRATOR_HEALTH_PANE_WEDGE_STATE_PATH.unlink(missing_ok=True)
    except OSError:
        pass

    # T-0525: STARTUP-DIALOG class only — chain dismiss -> kickoff inject. Gated
    # strictly on rule.kickoff_prompt being set (None for every other rule, which
    # stays dismiss-only). After a successful dismiss the session is UP but idle;
    # poll for the idle prompt, then inject the kickoff so it reads its HANDOFF
    # and posts status. On poll timeout: best-effort inject anyway (better to
    # attempt than leave idle) and make the timeout visible in the log + audit.
    kickoff_note = ""
    if rule.kickoff_prompt is not None:
        idle_ready = _poll_orchestrator_pane_for_idle(matched_sentinel)
        if not idle_ready:
            log_outcome(
                "recovery",
                "orchestrator_health:kickoff_idle_poll_timeout"
                f":sentinel={matched_sentinel!r}:best_effort_inject",
            )
        # Deliver the kickoff via the same cross-account sudo+paste-delay path as
        # the dismiss (NOT the bare inject() helper, which would hit Recovery's own
        # tmux server — T-0525 trap #1). _apply_auto_resolve_action sends each op
        # with the 0.7s paste-mode delay between text and Enter.
        kickoff_sent = _apply_auto_resolve_action(
            ORCHESTRATOR_PANE_TARGET, [rule.kickoff_prompt, "Enter"]
        )
        if idle_ready and kickoff_sent:
            kickoff_note = " + kickoff injected (idle confirmed)"
        elif kickoff_sent:
            kickoff_note = " + kickoff injected (best-effort, idle poll timed out)"
        else:
            kickoff_note = " + kickoff inject FAILED (send-keys error)"
            log_outcome("recovery", "orchestrator_health:kickoff_inject_send_keys_failed")

    _send_telegram_info(
        f"[recovery auto-resolve] sentinel={matched_sentinel!r}"
        f" action={rule.action}{kickoff_note} pane={ORCHESTRATOR_PANE_TARGET} at {_now_iso()}"
    )
    return "pane_wedge_auto_resolved"


def _send_telegram_info(text: str) -> None:
    """Send an informational Telegram message (audit, not escalation).

    Per T-0142 ratified design: each successful auto-resolve emits a per-firing
    informational message. Same delivery mechanism as the alert path; intent
    differs (audit trail rather than 'human action required'). Future operator-
    surface enhancement could route info messages to a quieter channel.
    """
    _send_telegram_alert(text)


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
    digests, activity digests). Those keep going direct to the operator per the
    user-facing-message-vs-operational-alert distinction.
    """
    if _send_to_recovery_pane(msg):
        audit = (
            f"[recovery routed] {vocab} alert delivered to Recovery's pane."
            f" See Recovery for action."
        )
        _send_telegram_info(audit)
    else:
        _send_telegram_alert(f"[fallback: recovery-pane unreachable] {msg}")


# Persona name of the self-monitoring/triage agent. Failure alerts for any OTHER
# agent route THROUGH this one; this agent's OWN failures cannot (it can't
# remediate itself), so they go direct to the operator. (T-0722)
SELF_TRIAGE_AGENT = "recovery"


def route_failure_alert(agent: str, msg: str, vocab: str) -> None:
    """Route a FAILURE alert per the operator's directive (2026-06-02, msg 2235):
    "all failures should wire to you [Recovery] direct, except for your own, which
    should curl to telegram and let me know I need to do something to fix
    things."

    - agent != recovery → _send_alert_via_recovery (Recovery triages; it audit-pings the operator
      and falls back to direct operator-telegram if Recovery's pane is unreachable).
    - agent == recovery → DIRECT to the operator (Recovery cannot self-remediate, so routing a
      Recovery-failure to Recovery's own pane is useless — the operator must intervene).

    Generalizes T-0709's "pipe internal alerts to Recovery" with the Recovery-self
    carve-out. Use for every per-agent failure-class alert.
    """
    if agent == SELF_TRIAGE_AGENT:
        _send_telegram_alert(
            f"⚠️ [recovery-self-failure] {msg}\n"
            f"Recovery cannot self-remediate — the operator, manual intervention needed."
        )
        return
    _send_alert_via_recovery(msg, vocab)


def check_orchestrator_health() -> str | None:
    """Probe bridge + pane for Orchestrator-wedge signals; auto-resolve or alert.

    Signals (escalating, first match wins):
      - bridge unreachable / malformed JSON  → silent skip (fail-open, return None)
      - telegram_connected is False          → "telegram_disconnected"
      - orchestrator not in connected_clients       → "orchestrator_disconnected"
      - queued_messages.orchestrator > 0            → "queue_stalled"
      - pane wedge sentinel persists ≥ 120s  → "pane_wedge" OR "pane_wedge_auto_resolved"
        (catches Mode B — Orchestrator's loop blocked on a TUI dialog while bridge
        fields read green).

    Pane-wedge sub-branch (T-0201):
      - If sentinel is in RECOVERY_AUTO_RESOLVE_ALLOWLIST: Recovery sends the rule's
        keystrokes via tmux, emits an informational Telegram (audit), clears
        the wedge-streak state, and returns "pane_wedge_auto_resolved" (no
        escalation alert). On send-keys failure: falls back to escalate.
      - If sentinel is NOT in allowlist: existing escalate-to-operator path
        (alert with sentinel + recommended action).

    Throttled to one ESCALATION alert per ``ORCHESTRATOR_HEALTH_ALERT_COOLDOWN_SECONDS``.
    Informational (auto-resolve) messages are NOT throttled — operator wants the
    full audit trail of what Recovery did, even at high frequency. Designed to run
    piggybacked on Recovery's inbox-consumer cycle and never block its main flow;
    raises nothing on its own.

    Returns the signal vocab (or None if healthy / unreachable). Used for tests
    + log correlation.
    """
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None

    msg: str | None = None
    vocab: str | None = None

    if data.get("telegram_connected") is False:
        vocab = "telegram_disconnected"
        msg = (
            "[pantheon] orchestrator-health: bridge cannot reach Telegram"
            " (telegram_connected=false). Check bridge logs / upstream."
        )
    elif "orchestrator" not in (data.get("connected_clients") or []):
        # T-0520: a disconnect is frequently caused by an allowlisted STARTUP
        # dialog blocking Orchestrator's loop from ever connecting (e.g. the
        # dev-channels confirm "I am using this for local development"). Try the
        # auto-resolve IMMEDIATELY — no 120s persistence gate, because the
        # persistence gate exists to avoid acting on dialogs that self-resolve
        # during NORMAL operation; a detached bridge means Orchestrator is not
        # operating, and a graceful restart would just re-show the same dialog.
        # Only allowlisted sentinels with passing predicates act (same safety
        # envelope as the pane-wedge branch); anything else falls through to the
        # restart recommendation below.
        pane = _orchestrator_pane_capture()
        matched = _pane_wedge_match(pane) if pane is not None else None
        if matched is not None:
            resolved = _auto_resolve_if_allowlisted(matched, pane)
            if resolved is not None:
                return resolved
        vocab = "orchestrator_disconnected"
        msg = (
            "[pantheon] orchestrator-health: orchestrator is not in connected_clients"
            " — its loop has detached from the bridge. Likely needs"
            " graceful restart via tools/handoff_and_restart.sh on the claude account."
        )
    else:
        orchestrator_queue = (data.get("queued_messages") or {}).get("orchestrator")
        if isinstance(orchestrator_queue, int) and orchestrator_queue > 0:
            vocab = "queue_stalled"
            last_in = (data.get("last_inbound_at") or {}).get("orchestrator")
            msg = (
                f"[pantheon] orchestrator-health: {orchestrator_queue} message(s) queued"
                f" for orchestrator and not draining (last inbound: {last_in})."
                f" Loop may be wedged; consider graceful restart."
            )
        else:
            wedge_result = _check_pane_wedge_persistent()
            if wedge_result:
                matched_sentinel, pane_text = wedge_result
                # T-0201 / T-0479: try the allowlist auto-resolve first (posture
                # C). _auto_resolve_if_allowlisted applies the rule iff the
                # sentinel is allowlisted AND its predicate passes AND send-keys
                # succeeds (audit Telegram + state clear inside). On None we
                # escalate, distinguishing predicate-False (rule template) from
                # not-allowlisted / send-keys-failed (generic).
                resolved = _auto_resolve_if_allowlisted(matched_sentinel, pane_text)
                if resolved is not None:
                    return resolved
                rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST.get(matched_sentinel)
                predicate_result = rule.predicate(pane_text) if rule is not None else None
                vocab = "pane_wedge"
                if predicate_result is False:
                    # Predicate returned False: this instance is unusual.
                    # Render the rule's escalate_template for diagnostic context
                    # so the operator understands WHY this specific instance
                    # escalated rather than auto-resolving.
                    excerpt = pane_text[:200] if pane_text else ""
                    msg = rule.escalate_template.format(  # type: ignore[union-attr]
                        sentinel=matched_sentinel,
                        excerpt=excerpt,
                    )
                else:
                    # No rule (sentinel not allowlisted) or send-keys failed
                    # after predicate True: generic escalation message.
                    msg = (
                        f"[pantheon] orchestrator-health: pane has shown wedge sentinel"
                        f" {matched_sentinel!r} for ≥ {ORCHESTRATOR_HEALTH_PANE_WEDGE_PERSIST_SECONDS}s."
                        f" Orchestrator's loop is blocked on a TUI dialog (likely the"
                        f" permission-prompt class T-0077 covers). Send `1` + Enter"
                        f" via tmux to unblock, or claim the dialog manually."
                    )

    if vocab is None:
        return None

    if _orchestrator_health_alert_throttled():
        return vocab  # return signal for log/tests, but suppress notification

    # T-0314 (originally inline here, extracted to _send_alert_via_recovery in T-0364):
    # route the escalate alert to Recovery's pane first; audit-Telegram on success;
    # fallback-Telegram on recovery-pane failure.
    _send_alert_via_recovery(msg, vocab=f"orchestrator-health ({vocab})")
    _orchestrator_health_record_alert()
    return vocab


# ---------------------------------------------------------------------------
# T-0541: Per-agent alert-throttle helpers.
# ---------------------------------------------------------------------------


def _agent_health_alert_throttled(agent: str) -> bool:
    """Return True if a health alert for this agent was already sent recently."""
    cfg = _AGENT_HEALTH_CONFIG.get(agent)
    if cfg is None:
        return False
    path: Path = cfg["last_alert_path"]
    if not path.exists():
        return False
    try:
        last = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return (int(time.time()) - last) < ORCHESTRATOR_HEALTH_ALERT_COOLDOWN_SECONDS


def _agent_health_record_alert(agent: str) -> None:
    cfg = _AGENT_HEALTH_CONFIG.get(agent)
    if cfg is None:
        return
    path: Path = cfg["last_alert_path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(time.time())), encoding="utf-8")


def check_agent_health(monitored_agent: str) -> str | None:
    """Probe bridge + pane for wedge signals for monitored_agent; auto-resolve or alert.

    T-0541: generalized replacement for check_orchestrator_health(). Parameterized on
    monitored_agent so Recovery can monitor any agent (orchestrator, specialist) using their
    respective pane targets and cross-account sudo paths.

    T-0553: Bridge-connectivity dimensions (telegram_connected, connected_clients,
    queued_messages) are only meaningful for agents that connect to pantheon-bridge
    via --channels. This is gated on _AGENT_HEALTH_CONFIG[agent]["is_bridge_client"].
    Non-bridge agents (specialist, recovery if ever added) skip all bridge-connectivity checks
    and run only the persistence-gated pane-wedge/boot-dialog check.

    Signal vocabulary (is_bridge_client=True):
      - bridge unreachable / malformed JSON       → None (fail-open)
      - telegram_connected is False               → "telegram_disconnected"
      - agent not in connected_clients            → "<agent>_disconnected"
      - queued_messages.<agent> > 0               → "queue_stalled"
      - pane wedge persists ≥ persist threshold   → "pane_wedge" or
                                                     "pane_wedge_auto_resolved"

    Signal vocabulary (is_bridge_client=False):
      - bridge unreachable / malformed JSON       → None (fail-open)
      - pane wedge persists ≥ persist threshold   → "pane_wedge" or
                                                     "pane_wedge_auto_resolved"
      (bridge-connectivity vocab never emitted)

    Uses _check_pane_wedge_persistent_for_agent / _auto_resolve_if_allowlisted_for_agent
    so pane capture and send-keys target the correct agent's tmux server.

    Throttled per agent via _agent_health_alert_throttled / _agent_health_record_alert.
    check_orchestrator_health() remains as a standalone function for backward compat with
    callers/tests that patch ORCHESTRATOR_HEALTH_LAST_ALERT_PATH and _orchestrator_pane_capture.
    """
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None

    cfg = _AGENT_HEALTH_CONFIG.get(monitored_agent, {})
    is_bridge_client: bool = cfg.get("is_bridge_client", False)

    msg: str | None = None
    vocab: str | None = None

    if is_bridge_client:
        # ----------------------------------------------------------------
        # Bridge-connectivity checks (only meaningful for bridge clients).
        # ----------------------------------------------------------------
        if data.get("telegram_connected") is False:
            vocab = "telegram_disconnected"
            msg = (
                f"[pantheon] {monitored_agent}-health: bridge cannot reach Telegram"
                " (telegram_connected=false). Check bridge logs / upstream."
            )
        elif monitored_agent not in (data.get("connected_clients") or []):
            # Mirror T-0520 logic: a disconnect may be caused by a startup dialog
            # blocking the connect. Try auto-resolve immediately (no persistence gate).
            pane = _pane_capture(monitored_agent)
            matched = _pane_wedge_match(pane) if pane is not None else None
            if matched is not None:
                resolved = _auto_resolve_if_allowlisted_for_agent(matched, pane, monitored_agent)
                if resolved is not None:
                    return resolved
            vocab = f"{monitored_agent}_disconnected"
            msg = (
                f"[pantheon] {monitored_agent}-health: {monitored_agent} is not in"
                f" connected_clients — its loop has detached from the bridge. Likely"
                f" needs graceful restart via tools/handoff_and_restart.sh on the"
                f" {monitored_agent} account."
            )
        else:
            agent_queue = (data.get("queued_messages") or {}).get(monitored_agent)
            if isinstance(agent_queue, int) and agent_queue > 0:
                vocab = "queue_stalled"
                last_in = (data.get("last_inbound_at") or {}).get(monitored_agent)
                msg = (
                    f"[pantheon] {monitored_agent}-health: {agent_queue} message(s) queued"
                    f" for {monitored_agent} and not draining (last inbound: {last_in})."
                    f" Loop may be wedged; consider graceful restart."
                )
            else:
                # Bridge-connected and queue empty: run persistence-gated pane check.
                wedge_result = _check_pane_wedge_persistent_for_agent(monitored_agent)
                if wedge_result:
                    matched_sentinel, pane_text = wedge_result
                    resolved = _auto_resolve_if_allowlisted_for_agent(
                        matched_sentinel, pane_text, monitored_agent
                    )
                    if resolved is not None:
                        return resolved
                    rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST.get(matched_sentinel)
                    predicate_result = rule.predicate(pane_text) if rule is not None else None
                    vocab = "pane_wedge"
                    if predicate_result is False:
                        excerpt = pane_text[:200] if pane_text else ""
                        msg = rule.escalate_template.format(  # type: ignore[union-attr]
                            sentinel=matched_sentinel,
                            excerpt=excerpt,
                        )
                    else:
                        msg = (
                            f"[pantheon] {monitored_agent}-health: pane has shown wedge"
                            f" sentinel {matched_sentinel!r} for"
                            f" ≥ {ORCHESTRATOR_HEALTH_PANE_WEDGE_PERSIST_SECONDS}s."
                            f" {monitored_agent.title()}'s loop is blocked on a TUI dialog."
                            f" Send `1` + Enter via tmux to unblock, or claim manually."
                        )
    else:
        # ----------------------------------------------------------------
        # Non-bridge-client path (T-0553): skip all bridge-connectivity
        # checks. Run only the persistence-gated pane-wedge/boot-dialog
        # check (the T-0541 intent for send-keys agents like specialist).
        # ----------------------------------------------------------------
        wedge_result = _check_pane_wedge_persistent_for_agent(monitored_agent)
        if wedge_result:
            matched_sentinel, pane_text = wedge_result
            # T-0559 alert-only gate: Recovery must NOT self-inject until a
            # Layer-2 backstop exists (no-backstop safety constraint). Other
            # agents (orchestrator, specialist) continue to auto-resolve via send-keys.
            alert_only = cfg.get("alert_only", False)
            if not alert_only:
                resolved = _auto_resolve_if_allowlisted_for_agent(
                    matched_sentinel, pane_text, monitored_agent
                )
                if resolved is not None:
                    return resolved
            rule = RECOVERY_AUTO_RESOLVE_ALLOWLIST.get(matched_sentinel)
            predicate_result = rule.predicate(pane_text) if rule is not None else None
            vocab = "pane_wedge"
            if predicate_result is False:
                excerpt = pane_text[:200] if pane_text else ""
                msg = rule.escalate_template.format(  # type: ignore[union-attr]
                    sentinel=matched_sentinel,
                    excerpt=excerpt,
                )
            else:
                msg = (
                    f"[pantheon] {monitored_agent}-health: pane has shown wedge"
                    f" sentinel {matched_sentinel!r} for"
                    f" ≥ {ORCHESTRATOR_HEALTH_PANE_WEDGE_PERSIST_SECONDS}s."
                    f" {monitored_agent.title()}'s loop is blocked on a TUI dialog."
                    f" Send `1` + Enter via tmux to unblock, or claim manually."
                )

    if vocab is None:
        return None

    if _agent_health_alert_throttled(monitored_agent):
        return vocab

    route_failure_alert(monitored_agent, msg, vocab=f"{monitored_agent}-health ({vocab})")  # T-0722
    _agent_health_record_alert(monitored_agent)
    return vocab


# ---------------------------------------------------------------------------
# T-1036: the T-0213 sync-alert plumbing (_sync_alert_throttled / _sync_alert_record
# / _read_sync_alert_state / _append_sync_alert_ledger / check_pantheon_config_sync_alert)
# was retired here. T-0365 retired the 60s pantheon-config-sync timer that wrote
# the state file these functions read, so they could never fire again.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Main cycle.
# ---------------------------------------------------------------------------


def _resolve_tmux_target(agent: str) -> str:
    return TMUX_TARGET_BY_AGENT.get(agent, f"claude-{agent}-bot")


def main(agent: str) -> int:
    if agent == "recovery":
        # T-0541: probe each monitored agent exactly once via check_agent_health.
        # The legacy check_orchestrator_health() call is replaced by this loop so Orchestrator
        # is not double-probed (disconnect branch has no persistence gate — two
        # calls in one cycle would both inject send-keys into Orchestrator's live pane).
        # check_orchestrator_health() remains defined as a backward-compat alias for
        # external/test callers but is no longer invoked from the main cycle.
        # T-0559: add recovery to the health loop in alert-only mode. The
        # _AGENT_HEALTH_CONFIG["recovery"]["alert_only"]=True gate suppresses
        # the auto-resolve send-keys path — detect+alert only until a
        # Layer-2 watcher-of-the-watcher backstop exists (no-backstop
        # safety constraint, T-0555 Layer 2 unbuilt).
        for monitored in ("orchestrator", "specialist", "recovery"):
            try:
                agent_vocab = check_agent_health(monitored)
                if agent_vocab is not None:
                    log_outcome("recovery", f"agent_health:{monitored}:{agent_vocab}")
            except Exception as e:
                print(
                    f"[inbox_consumer] check_agent_health({monitored!r}) failed: {e}",
                    file=sys.stderr,
                )
        # T-1036: check_pantheon_config_sync_alert() probe removed — the
        # pantheon-config-sync timer it monitored was retired by T-0365.

    # Resolve tmux target before the try block so it's accessible in the except
    # path for active-tail surface delivery (T-0383). _resolve_tmux_target never
    # raises, so this is safe to call unconditionally.
    tmux_target = _resolve_tmux_target(agent)

    skip_vocab: str | None = None
    try:
        check_autonomy_off()
        check_cap_hit(agent)
        check_recent_inbound(agent)
        # T-0717: check_mid_turn (per-turn pane sentinel) REMOVED here.
        # Dispatch gating is now handled by check_dispatch_locked (the
        # auto_delivered-stamp mutex), which is state-derived and session-
        # independent. The wedge-health-detection path (HEALTH_PANE_WEDGE)
        # is a separate mechanism and is NOT changed by this ticket.
        check_dispatch_locked(agent)
        pull_repo_or_warn("inbox_consumer")
        tickets = list_eligible_tickets(agent)
        ticket = pick_ticket(tickets)
        prompt = format_prompt(ticket, agent)
        # T-0717: stamp BEFORE inject. Two failure modes:
        #   - Write failure (False) → fail-CLOSED: raise SkipCycle('stamp_write_failed');
        #     do NOT inject. Without the stamp on disk, the mutex is absent next cycle
        #     and a second injection could follow. Clean retry on the next cycle fire.
        #   - Commit/push failure (True) → proceed: local write established the mutex
        #     for this agent's own consumer. Inject happens; push failure is logged.
        stamp_ok = _stamp_auto_delivered(ticket)
        if not stamp_ok:
            raise SkipCycle("stamp_write_failed")
        inject(tmux_target, prompt)
        outcome = f"pulled {ticket.id}"
        log_outcome(agent, outcome)
        print(f"[inbox_consumer] {agent}: {outcome}")
        return 0
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
        #   - All other gating vocabs (off_switch, cap_hit, recent_telegram,
        #     tmux_unreachable, inject_pending_paste): not idle states.
        if skip_vocab in {"empty_inbox", "not_before_unmet"}:
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
    parser.add_argument("--agent", required=True, help="Agent name (orchestrator, specialist, recovery).")
    args = parser.parse_args()
    sys.exit(main(args.agent))
