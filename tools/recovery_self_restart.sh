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
#
# T-4159: the restart target is RESOLVED from instance.yaml via
# `agent_role_map.py restart-target recovery`, not hardcoded. Today recovery is bespoke
# (claude-telegram-fallback.service). Once T-3026's commit B marks recovery
# `supervisor: control-plane`, the same call yields the persona-consume unit plus
# --supervisor control-plane, so this wrapper (and the bun-child watchdog that
# calls it) cycles the control-plane session instead of resurrecting the retired
# bespoke unit next to the runtime controller. A resolution failure REFUSES
# rather than falling back to the bespoke name (that fallback is the hazard).
# RECOVERY_SERVICE / RECOVERY_SUPERVISOR / RECOVERY_TARGET_PANE / RECOVERY_TMUX_SESSION still
# override. PANTHEON_AGENT_ROLE_MAP swaps the resolver (tests).
set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESOLVER="${PANTHEON_AGENT_ROLE_MAP:-$TOOLS_DIR/agent_role_map.py}"
declare -A RT=()
if RT_OUT="$(python3 "$RESOLVER" restart-target recovery)"; then
  while IFS='=' read -r _k _v; do [[ -n "$_k" ]] && RT[$_k]="$_v"; done <<<"$RT_OUT"
elif [[ -z "${RECOVERY_SERVICE:-}" ]]; then
  echo "[recovery-self-restart] ERROR: could not resolve recovery's restart target ($RESOLVER restart-target recovery); refusing to guess a unit. Set RECOVERY_SERVICE (+ RECOVERY_SUPERVISOR) to override." >&2
  exit 1
fi

SERVICE="${RECOVERY_SERVICE:-${RT[SERVICE]:-}}"
SUPERVISOR="${RECOVERY_SUPERVISOR:-${RT[SUPERVISOR]:-bespoke}}"
if [[ -n "${RECOVERY_TMUX_SESSION:-}" ]]; then
  TARGET_PANE="${RECOVERY_TARGET_PANE:-${RECOVERY_TMUX_SESSION}:0}"
else
  TARGET_PANE="${RECOVERY_TARGET_PANE:-${RT[PANE]:-claude-telegram-bot-fallback:0}}"
fi
HANDOFF_FILE="${RECOVERY_HANDOFF_FILE:-/home/claude-fallback/HANDOFF.md}"
HANDOFF_FRESH_HOURS="${RECOVERY_HANDOFF_FRESH_HOURS:-1}"
STEADY_TIMEOUT="${RECOVERY_STEADY_TIMEOUT:-90}"
SELF_RESTART_DELAY="${RECOVERY_SELF_RESTART_DELAY:-3}"

GENERAL_SCRIPT="$TOOLS_DIR/self_restart.sh"

exec "$GENERAL_SCRIPT" \
  --service "$SERVICE" \
  --mode user \
  --target-pane "$TARGET_PANE" \
  --handoff-file "$HANDOFF_FILE" \
  --steady-timeout "$STEADY_TIMEOUT" \
  --self-restart-delay "$SELF_RESTART_DELAY" \
  --handoff-fresh-hours "$HANDOFF_FRESH_HOURS" \
  --unit "recovery-self-restart" \
  --supervisor "$SUPERVISOR" \
  "$@"
