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
CLAUDE_ARGS="--channels plugin:telegram@claude-plugins-official"

# (Re)create the session only if absent — idempotent across restarts. Wrapped in
# `sg pantheon-shared -c` so the tmux server + inner claude inherit the shared
# group (orchestrator-only; see header).
if ! sg pantheon-shared -c "tmux has-session -t $SESSION" 2>/dev/null; then
  sg pantheon-shared -c "tmux new-session -d -s $SESSION '$CLAUDE_BIN $CLAUDE_ARGS'"
fi

# Block as systemd's MainPID until the session goes away (5s poll).
while sg pantheon-shared -c "tmux has-session -t $SESSION" 2>/dev/null; do
  sleep 5
done

# Session ended → exit so systemd's Restart=always brings us back.
exit 0
