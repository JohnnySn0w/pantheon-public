#!/usr/bin/env bash
# specialist-bot-supervise.sh — systemd-supervised wrapper for Specialist's Claude Code
# session (claude-research account, @c_specialist_bot Telegram bidirectional).
#
# Why this exists (restart-capability #1, per sop/standard/adding-restart-
# capability-to-a-new-agent.md; T-0517 parity / T-0755):
#   Specialist's old unit was Type=oneshot + RemainAfterExit=yes: it fired
#   `tmux new-session -d` and returned 0 immediately, so systemd reported the
#   service "active (exited)" forever — even after the tmux session (and the
#   `claude` inside it) died. Nothing restarted it (the same dead-daemon mode
#   that bit Recovery 2026-05-21). This wrapper keeps the EXACT same detached tmux
#   session (so capture-pane / send-keys diagnosis + restart machinery still
#   work) but stays alive as systemd's MainPID for as long as the session
#   lives. When it dies, this script exits and `Restart=always` respawns it.
#
# Adapted from the canonical tools/systemd/recovery-bot-supervise.sh (T-0526).
# CLAUDE_ARGS MUST carry --channels (P1/T-0754 made @c_specialist_bot
# bidirectional; this is the launch path that keeps it on). The plugin must
# also stay enabled in settings.json (enabledPlugins telegram=true).
#
# NOTE: catches only the process-exit / inert-daemon mode. The "alive but
# 400ing" mode is handled by the api-health path (context_fill_monitor +
# drain), not here.
set -euo pipefail

SESSION="claude-research-bot"
CLAUDE_BIN="/home/claude-research/.npm-global/bin/claude"
# --model pins claude-opus-4-8 (the operator TG 2026-08-03, fleet uniform on opus 4.8). The
# launch flag is the authoritative pin and outranks settings.json. Backported here to
# match the live supervise script — without it a redeploy would drop specialist's pin.
CLAUDE_ARGS="--channels plugin:telegram@claude-plugins-official --model claude-opus-4-8 --permission-mode default"

# NO sg-wrap (T-0783): claude-research is not in pantheon-shared and operates on
# its OWN pantheon-config clone — no shared-group inheritance needed. The
# ORCHESTRATOR variant (claude-bot-supervise.sh) sg-wraps because its clone is the
# canonical group-shared one; don't copy that wrap here.

# (Re)create the session only if it isn't already up — idempotent across
# systemd restarts and tolerant of a manual tmux spawn.
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  tmux new-session -d -s "$SESSION" "$CLAUDE_BIN $CLAUDE_ARGS"
fi

# Block as systemd's MainPID until the session goes away. Polling (not a
# busy-wait) — 5s is responsive enough and costs nothing.
while tmux has-session -t "$SESSION" 2>/dev/null; do
  sleep 5
done

# Session ended → exit so systemd's Restart=always brings us back.
exit 0
