#!/usr/bin/env bash
# ancestor-bot-supervise.sh — systemd-supervised wrapper for the the-ancestor daemon
# (Veneration, T-0765). Modeled on recovery-bot-supervise.sh, with the load-bearing
# difference: CHANNEL-LESS.
#
# the-ancestor is the neutral shared substrate for Veneration (resume-the-dead recovery)
# and the first node of the ORG-026 inter-agent mailbox. It is a thin, extremely idle
# standing claude session whose ONLY jobs are: (1) consume resume-requests from its
# mailbox and service them by running channel-less `claude --resume` of a dead session
# to harvest a fresh HANDOFF, and (2) serve archived history on request.
#
# SAFETY INVARIANT — CHANNEL-LESS, NEVER a telegram bot:
#   CLAUDE_ARGS carries NO `--channels` / NO telegram plugin. The daemon holds no bot
#   token, so it cannot hijack a live agent's Telegram bridge (the recurring
#   second-claude-steals-the-live-token landmine). Its resume subprocesses ALSO run
#   channels-disabled. This is the entire reason Veneration runs as a separate account.
#   Do NOT add --channels here. Ever.
#
# Like recovery-bot-supervise.sh: keeps the detached tmux session (so capture-pane /
# send-keys + the mailbox-wake kick still target it) and stays alive as systemd's
# MainPID for as long as the session lives; on session death it exits and
# Restart=always respawns it (auto-recovery for the inert-daemon failure mode).
set -euo pipefail

SESSION="${ANCESTOR_SESSION:-ancestor-daemon}"
CLAUDE_BIN="${ANCESTOR_CLAUDE_BIN:-/home/the-ancestor/.npm-global/bin/claude}"
# Channel-less by construction: no --channels, no telegram plugin.
# --permission-mode default pins the pre-v2.1.200 launch default (CC v2.1.200 flipped
# the unset default to Manual → unattended stall class; T-1541/T-1544). No escalation.
CLAUDE_ARGS="${ANCESTOR_CLAUDE_ARGS:---permission-mode default}"

# (Re)create the session only if it isn't already up — idempotent across restarts.
if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  # shellcheck disable=SC2086  # CLAUDE_ARGS is intentionally word-split (usually empty).
  tmux new-session -d -s "$SESSION" "$CLAUDE_BIN $CLAUDE_ARGS"
fi

# Block as systemd's MainPID until the session goes away (poll, not busy-wait).
while tmux has-session -t "$SESSION" 2>/dev/null; do
  sleep 5
done

exit 0
