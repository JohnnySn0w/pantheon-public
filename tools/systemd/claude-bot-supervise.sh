#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# REFERENCE SNAPSHOT of orchestrator's LIVE supervise script, synced 2026-09-28T06:25Z (T-4068).
# The LIVE copy at /home/claude/.local/bin/claude-bot-supervise.sh is AUTHORITATIVE.
# This tracked copy is for review, audit and diff only. Edits land live first, and
# model/effort changes land live-only (see project_model_policy_opus_standard).
# Re-sync when a live change is load-bearing (e.g. a boot-time re-assert such as
# T-2629/T-4092). No drift lint by design: these scripts retire with the
# harness-controller cutover (T-2953/T-3650).
# ---------------------------------------------------------------------------
# claude-bot-supervise.sh — systemd-supervised wrapper for the ORCHESTRATOR's
# (Orchestrator / HO) Telegram bot. The sg-wrapped variant of the supervise pattern.
#
# Same core as recovery-bot-supervise.sh / specialist-bot-supervise.sh (T-0509/T-0755):
# the old Type=oneshot + RemainAfterExit unit returned 0 immediately so systemd
# reported "active (exited)" forever even after the tmux session + inner `claude`
# died (nothing respawned it — the 2026-05-21 Recovery outage mode). This wrapper
# keeps the SAME detached tmux session but stays alive as systemd's MainPID for
# the session's lifetime; on session death it exits and Restart=always respawns
# it. CLAUDE_ARGS MUST carry --channels for a bidirectional bot.
#
# WHAT MAKES THIS THE ORCHESTRATOR VARIANT — the `sg pantheon-shared -c` wrap
# (T-0783): the orchestrator's pantheon-config clone is the CANONICAL one that
# multiple accounts (kino, etc.) write to via the `pantheon-shared` group, so the
# tmux server + inner claude TUI must inherit that group to write
# pantheon-config/.git/objects without permission failures. user-mode systemd
# CANNOT grant supplementary groups (T-0659/T-0661), so the group is inherited
# in user-space via `sg pantheon-shared -c '...'`.
#   • The RECOVERY (recovery-bot-supervise.sh) and SPECIALIST (specialist-bot-supervise.sh)
#     variants OMIT the sg-wrap on purpose: they operate on their OWN per-account
#     clones, which they own outright, so no shared-group inheritance is needed.
#   • So sg-wrap is an ORCHESTRATOR-ONLY add-on, NOT the universal spine — pick
#     the variant that matches the role's clone ownership.
#
# NOTE: catches only the process-exit / inert-daemon mode. The "alive but every
# request 400s" mode needs a health probe (context_fill_monitor api-health / drain).
set -euo pipefail

# T-3937: echo shared-cargo-target durability. Set umask + the shared
# CARGO_TARGET_DIR HERE (before the tmux session spawns) so the inner claude TUI
# and every subagent / non-login build shell it launches inherit them through the
# process tree (sg -> tmux -> sh -> claude -> subagents; sg-boundary inheritance
# verified). Closes the per-seat target/ regrowth gap for orchestrator's dispatched echo
# builds. Fleet-optional: redirect only if the shared cache is writable, else fall
# through to the per-seat default (preserves T-3777's fleet-optional property).
# umask 002 keeps cache artifacts group-writable for peer-seat cache sharing.
umask 002
PANTHEON_ECHO_TARGET_DIR="${PANTHEON_ECHO_TARGET_DIR:-/var/lib/pantheon/echo-target}"
if [[ -z "${CARGO_TARGET_DIR:-}" && -d "${PANTHEON_ECHO_TARGET_DIR}" && -w "${PANTHEON_ECHO_TARGET_DIR}" && -x "${PANTHEON_ECHO_TARGET_DIR}" ]]; then
  export CARGO_TARGET_DIR="${PANTHEON_ECHO_TARGET_DIR}"
fi

SESSION="claude-telegram-bot"
CLAUDE_BIN="/home/claude/.npm-global/bin/claude"
# --permission-mode default pins the pre-v2.1.200 launch default (CC v2.1.200 flipped
# the unset default to Manual, which stalls unattended relaunches at the first tool
# prompt — T-1541/T-1544, watch report 2026-07-06). Restores prior behavior, no escalation.
# SUPERSEDED 2026-10-13 (T-4935, the operator TG 11753): the launch is now --permission-mode
# bypassPermissions (unix account = boundary); tools/bypass_consent.py pre-accepts the dialog.
# --effort xhigh: operator-directed baseline (TG 2026-08-10, T-1994 model-mix change).
# --model claude-fable-5: operator-directed (TG 8368 + 8382, 2026-08-24 — "swapped you to
# fable for the mental bandwidth" / "model fixed"). He set it via TUI /model, which the
# launch flag overrides on every respawn — so the flag is flipped here to make the choice
# survive restarts. Flipped by Orchestrator 2026-08-24T23:3xZ ahead of the mcp-list-guard restart.
# To REVERT: restore --model claude-opus-4-7 (line below) — one-line change.
# (prior lines, for the audit trail:)
# --model claude-opus-4-7: operator-directed swap (TG 7885, 2026-08-12 23:38Z, at the
# 95%-quota hold) — ended the first Fable experiment (TG 7727/7728). Standing baseline
# reference remains opus-4.8 (model-policy pin).
# --model claude-fable-5: EXPERIMENT — the operator directed orchestrator onto Fable (TG 7727/7728,
# 2026-08-12, "let's try something new"). To REVERT to the standing opus-4.8 baseline,
# restore: --model claude-opus-4-8 (see the model-policy pin).
# --model claude-opus-4-7: operator-directed swap back (TG 8476 "opus 4.7 might be the
# preferred model for now" + TG 8481 "get the model line swapped and restart",
# 2026-08-25 17:41Z/17:52Z). Flipped by Orchestrator 2026-08-25T17:5xZ, then self-restarted.
# To REVERT to Fable: restore --model claude-fable-5 (one-line change).
# --model claude-opus-5-5: operator-directed fleet-wide model bump (TG 10361
# "put yourself on xhigh 5.5. We're going to bump the fleet up and see how it
# goes.", 2026-09-25 00:24Z). Specialist bumped in parallel to --effort high on 5.5.
# Backup: claude-bot-supervise.sh.bak.pre-opus55-20260925T002531Z.
# T-4653 (the operator TG 10756, 2026-10-05): every seat on --effort medium to cut weekly-Max burn.
CLAUDE_ARGS="--channels plugin:telegram@claude-plugins-official --model claude-opus-5-5 --effort medium --permission-mode bypassPermissions"

# T-4935 (the operator TG 11753 via T-4939; ORG-063 s1): launch is --permission-mode bypassPermissions
# (full unlock; supersedes the `default` pin in the comments above). The bypass warning dialog
# ("No, exit" preselected) is skipped only when skipDangerousModePermissionPrompt is in USER
# settings (--settings/flagSettings does not count, verified in the 2.1.296 binary), so the
# consent is merged into settings.json here: idempotent, fail-open (tools/bypass_consent.py).
python3 -c 'import json,os,sys;d=os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude");p=os.path.join(d,"settings.json");s=json.load(open(p)) if os.path.exists(p) else {};sys.exit(0) if s.get("skipDangerousModePermissionPrompt") is True else None;os.makedirs(d,exist_ok=True);s["skipDangerousModePermissionPrompt"]=True;t=p+".tmp";json.dump(s,open(t,"w"),indent=2);os.replace(t,p)' >/dev/null 2>&1 || true

# (Re)create the session only if absent — idempotent across restarts. Wrapped in
# `sg pantheon-shared -c` so the tmux server + inner claude inherit the shared
# group (orchestrator-only; see header).
# ORG-106 L3a (T-5526): never launch a deep-sleeping seat. WAIT rather than exit (Restart=always
# would churn the unit); fail-open on a reader fault. Tracked copy of the live script: re-sync it.
while :; do
  _lc_rc=0
  python3 "$HOME/pantheon-config/tools/seat_state.py" launch-check orchestrator || _lc_rc=$?
  [[ $_lc_rc -eq 3 ]] || break
  sleep 60
done

if ! sg pantheon-shared -c "tmux has-session -t $SESSION" 2>/dev/null; then
  # DURABLE 409-RESILIENCE RE-ASSERT (T-2629, wired for orchestrator by T-4092): a
  # plugin-cache rebuild restores the upstream telegram server.ts, which gives up
  # after 8 consecutive getUpdates 409s and goes alive-but-deaf. Re-apply the
  # never-give-up patch BEFORE the session (and its server.ts) starts.
  # Marker-guarded and exact-literal, so it's idempotent; fail-open (|| true).
  ~/pantheon-config/tools/telegram_poller_409_patch.sh >/dev/null 2>&1 || true
  # T-5016: archive the prior session to the Chronicle before a fresh start
  # (covers crash/reboot ends; bounded, fail-open, exactly-once).
  timeout 90 python3 ~/pantheon-config/tools/session_chronicle.py session-end --persona orchestrator --cause start-reconcile --stage --certain --handoff-file /home/claude/HANDOFF.md >/dev/null 2>&1 || true
  sg pantheon-shared -c "tmux new-session -d -s $SESSION '$CLAUDE_BIN $CLAUDE_ARGS'"
  ~/pantheon-config/tools/arm_pane_log.sh "$SESSION:0" orchestrator || true  # T-1636 pane time-log tap
fi

# Block as systemd's MainPID until the session goes away (5s poll).
while sg pantheon-shared -c "tmux has-session -t $SESSION" 2>/dev/null; do
  sleep 5
done

# Session ended → exit so systemd's Restart=always brings us back.
exit 0
