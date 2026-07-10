#!/usr/bin/env bash
# recovery-bot-supervise.sh — systemd-supervised wrapper for Recovery's Telegram bot.
#
# Why this exists (RCA 2026-05-21, T-0495-adjacent outage):
#   The old unit was Type=oneshot + RemainAfterExit=yes: it fired
#   `tmux new-session -d` and returned 0 immediately. systemd then believed
#   the service was "active (exited)" forever — even after the tmux session
#   (and the `claude` process inside it) died. Nothing restarted it. Result:
#   a ~14h dead-daemon outage.
#
# This wrapper keeps the EXACT same detached tmux session (so capture-pane /
# send-keys diagnosis and any restart machinery still work) but stays alive
# as systemd's MainPID for as long as the session lives. When the session
# dies, this script exits and `Restart=always` respawns it — auto-recovery.
#
# NOTE: this only catches the *process-exit / inert-daemon* failure mode.
# The "alive but every request 400s" mode (the actual 2026-05-21 trigger) is
# NOT caught here — that needs a health probe. See the recovery SOP.
#
# NO sg-wrap here (T-0783): this account operates on its OWN pantheon-config clone,
# which it owns, so no shared-group inheritance is needed. If you're standing up
# the ORCHESTRATOR (whose clone is the canonical one written by multiple
# pantheon-shared accounts), use the sg-wrapped claude-bot-supervise.sh instead —
# copying THIS script for the orchestrator would lose group inheritance and fail
# git writes to the canonical .git.
set -euo pipefail

SESSION="claude-telegram-bot-fallback"
CLAUDE_BIN="/home/claude-fallback/.npm-global/bin/claude"
CLAUDE_ARGS="--channels plugin:telegram@claude-plugins-official"

# (Re)create the session only if it isn't already up — makes the script
# idempotent across systemd restarts and tolerant of a manual tmux spawn.
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux new-session -d -s "$SESSION" "$CLAUDE_BIN $CLAUDE_ARGS"
fi

# Block as systemd's MainPID until the session goes away. Polling (not a
# busy-wait) — 5s is responsive enough for a chat bridge and costs nothing.
while tmux has-session -t "$SESSION" 2>/dev/null; do
  sleep 5
done

# Session ended → exit so systemd's Restart=always brings us back.
exit 0
