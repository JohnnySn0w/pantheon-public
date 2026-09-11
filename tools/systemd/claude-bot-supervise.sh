#!/usr/bin/env bash
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

SESSION="claude-telegram-bot"
CLAUDE_BIN="/home/claude/.npm-global/bin/claude"
# --permission-mode default pins the pre-v2.1.200 launch default (CC v2.1.200 flipped
# the unset default to Manual, which stalls unattended relaunches at the first tool
# prompt — T-1541/T-1544, watch report 2026-07-06). Restores prior behavior, no escalation.
# --effort xhigh: operator-directed baseline (TG 2026-08-10, T-1994 model-mix change).
# --model claude-opus-4-7: operator-directed (TG 8481, 2026-08-25 17:52Z — "oh I forgot I
# can't select opus 4.7 via /model. Yeah get the model line swapped and restart"). Flipped
# by Orchestrator 2026-08-25T17:53Z on the live ~/.local/bin/claude-bot-supervise.sh; this repo
# mirror backported 2026-08-25T19:4xZ after Recovery flagged the drift (T-2444-class untracked
# backport — the mirror had been sync'd to fable-5 by T-3192 hours before the live flip).
# To REVERT: restore --model claude-fable-5 (line below) — one-line change.
# (prior lines, for the audit trail:)
# --model claude-fable-5: operator-directed (TG 8368 + 8382, 2026-08-24 — "swapped you to
# fable for the mental bandwidth" / "model fixed"). Set via TUI /model; the flag was
# flipped 2026-08-24T23:3xZ so the choice survived restarts. Superseded by the opus-4-7
# flip above.
# --model claude-opus-4-7: operator-directed swap (TG 7885, 2026-08-12 23:38Z, at the
# 95%-quota hold) — ended the first Fable experiment (TG 7727/7728). Standing baseline
# reference remains opus-4.8 (model-policy pin).
# --model claude-fable-5: EXPERIMENT — the operator directed orchestrator onto Fable (TG 7727/7728,
# 2026-08-12, "let's try something new"). To REVERT to the standing opus-4.8 baseline,
# restore: --model claude-opus-4-8 (see the model-policy pin).
CLAUDE_ARGS="--channels plugin:telegram@claude-plugins-official --model claude-opus-4-7 --effort xhigh --permission-mode default"

# (Re)create the session only if absent — idempotent across restarts. Wrapped in
# `sg pantheon-shared -c` so the tmux server + inner claude inherit the shared
# group (orchestrator-only; see header).
if ! sg pantheon-shared -c "tmux has-session -t $SESSION" 2>/dev/null; then
  sg pantheon-shared -c "tmux new-session -d -s $SESSION '$CLAUDE_BIN $CLAUDE_ARGS'"
  ~/pantheon-config/tools/arm_pane_log.sh "$SESSION:0" orchestrator || true  # T-1636 pane time-log tap
fi

# Block as systemd's MainPID until the session goes away (5s poll).
while sg pantheon-shared -c "tmux has-session -t $SESSION" 2>/dev/null; do
  sleep 5
done

# Session ended → exit so systemd's Restart=always brings us back.
exit 0
