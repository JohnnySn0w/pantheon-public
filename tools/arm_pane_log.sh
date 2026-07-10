#!/usr/bin/env bash
# tools/arm_pane_log.sh — arm the timestamped pane side-log tap (T-1636).
#
# Streams everything the agent's tmux pane prints to a per-line-timestamped
# side-log via `tmux pipe-pane` (the operator TG 3722: the TUI has no native
# per-turn timestamps; the pane stream + wall-clock prefix is the zero-impact
# interim). The log's lifecycle pairs with the graveyard (TG 3724): at each
# graceful restart, handoff_and_restart.sh archives it as the dying session's
# <ts>-<uuid>.pane.log.gz beside its HANDOFF snapshot, and the fresh session's
# supervise launcher re-arms a fresh tap via this script.
#
# Call from each agent's supervise launcher right after `tmux new-session`:
#     ~/pantheon-config/tools/arm_pane_log.sh <pane> <agent> || true
#
# Contract:
#   - FAIL-OPEN: exit 0 always. A tap is observability; it must never block or
#     brick a bot launch (mirror of the graveyard's best-effort discipline).
#   - Idempotent: `pipe-pane -o` toggles OFF if a pipe is already attached, so
#     we only arm when no pipe exists (tmux #{pane_pipe} flag guards the toggle
#     footgun). Re-running is safe.
#   - Runs AS the pane's owner (each launcher runs on its own account; no sudo
#     here — cross-account arming is the caller's job, not this script's).

set -u

PANE="${1:-}"
AGENT="${2:-}"

log() { echo "[arm-pane-log] $*" >&2; }

if [[ -z "$PANE" || -z "$AGENT" ]]; then
  log "usage: arm_pane_log.sh <tmux-pane> <agent-name> (fail-open: exiting 0)"
  exit 0
fi

LOG_DIR="${PANE_LOG_DIR:-$HOME/.local/log/pantheon}"
LOG_FILE="$LOG_DIR/pane-timed-${AGENT}.log"

mkdir -p "$LOG_DIR" 2>/dev/null || { log "mkdir failed for $LOG_DIR (non-fatal)"; exit 0; }

# Guard the -o toggle: if a pipe is already attached, arming again would
# DISARM it. #{pane_pipe} is 1 when a pipe-pane is active.
piped="$(tmux display-message -p -t "$PANE" '#{pane_pipe}' 2>/dev/null)" || {
  log "pane $PANE not found (non-fatal)"; exit 0; }
if [[ "$piped" == "1" ]]; then
  log "pane $PANE already piped; leaving existing tap in place"
  exit 0
fi

# Per-line wall-clock prefix; fflush so the log tails live. The awk holds the
# file's INODE for the life of the tap — the graveyard archive step gzip-copies
# then unlinks the file, so post-archive restart chatter goes harmlessly to the
# unlinked inode and the next session's tap opens a fresh file here.
if tmux pipe-pane -o -t "$PANE" \
    'awk "{print strftime(\"[%Y-%m-%d %H:%M:%S] \") \$0; fflush()}" >> '"$LOG_FILE" 2>/dev/null; then
  log "armed: $PANE -> $LOG_FILE"
else
  log "pipe-pane failed for $PANE (non-fatal)"
fi
exit 0
