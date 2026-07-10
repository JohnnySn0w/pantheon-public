#!/usr/bin/env bash
# recovery_self_restart.sh — BACK-COMPAT WRAPPER (T-0531).
#
# The self-restart-with-kick logic is now generalized in self_restart.sh (takes
# --service/--mode/--target-pane so any agent can self-heal). This wrapper just
# pins Recovery's substrate defaults and forwards to it, preserving the original
# RECOVERY_* env-override contract so existing call sites / muscle memory keep working.
#
# Prefer calling self_restart.sh directly with explicit flags for non-Recovery agents.
# See sop/standard/agent-self-restart-with-kick.md for the usage SOP.
#
# IMPORTANT: write/refresh the HANDOFF file BEFORE calling this — the kick tells
# fresh-Recovery to read it. This wrapper does NOT write the handoff.
set -euo pipefail

SERVICE="${RECOVERY_SERVICE:-claude-telegram-fallback.service}"
TMUX_SESSION="${RECOVERY_TMUX_SESSION:-claude-telegram-bot-fallback}"
TARGET_PANE="${RECOVERY_TARGET_PANE:-${TMUX_SESSION}:0}"
HANDOFF_FILE="${RECOVERY_HANDOFF_FILE:-/home/claude-fallback/HANDOFF.md}"
HANDOFF_FRESH_HOURS="${RECOVERY_HANDOFF_FRESH_HOURS:-1}"
STEADY_TIMEOUT="${RECOVERY_STEADY_TIMEOUT:-90}"
SELF_RESTART_DELAY="${RECOVERY_SELF_RESTART_DELAY:-3}"

GENERAL_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/self_restart.sh"

exec "$GENERAL_SCRIPT" \
  --service "$SERVICE" \
  --mode user \
  --target-pane "$TARGET_PANE" \
  --handoff-file "$HANDOFF_FILE" \
  --steady-timeout "$STEADY_TIMEOUT" \
  --self-restart-delay "$SELF_RESTART_DELAY" \
  --handoff-fresh-hours "$HANDOFF_FRESH_HOURS" \
  --unit "recovery-self-restart" \
  "$@"
