#!/usr/bin/env bash
# self_restart.sh — an always-on agent restarts its OWN long-lived Claude Code
# session AND gets the fresh session kicked into action (read HANDOFF + announce),
# unattended. Generalized from recovery_self_restart.sh (T-0531) so HO/Orchestrator/Specialist
# get the same self-heal: substrate is passed as flags, not hardcoded.
#
# Why this exists (2026-05-21):
#   A plain `systemctl restart <service>` recovers the bridge but leaves the fresh
#   session SILENT — the SessionStart auto-read hook injects "Read HANDOFF.md" only
#   as additionalContext, which does NOT trigger a first-turn action. The
#   post-restart KICK in handoff_and_restart.sh (step 8) forces that first turn —
#   but that script cannot drive a SELF-restart: it would be killed when its own
#   session is restarted.
#
#   Fix: run handoff_and_restart.sh --skip-ack from a DETACHED `systemd-run`
#   transient unit. The transient unit lives in its own cgroup, so it survives the
#   restart of <service> and delivers the kick to the fresh session. --skip-ack
#   bypasses steps 2-4 (the in-session handoff prompt we cannot answer mid-run) and
#   runs restart -> is-active -> steady-state -> kick. The kick uses
#   send_keys_submit (T-0519: delayed Enter + submit-verify).
#
# IMPORTANT: write/refresh the HANDOFF file BEFORE calling this — the kick tells
# the fresh session to read it. This wrapper does NOT write the handoff (an agent
# can't write its own handoff and then restart synchronously without deadlocking).
#
# Usage:
#   self_restart.sh --service <unit> --target-pane <pane> [--mode user|system] \
#                   [--handoff-file <path>] [--steady-timeout <sec>] \
#                   [--self-restart-delay <sec>] [--unit <transient-unit-name>] \
#                   [--handoff-fresh-hours <h>] [--dry-run]
#
# Per-agent invocation is the same flag contract as handoff_and_restart.sh; the
# agent fires it in-session as its own user (self-restart is always same-account —
# no cross-account sudo, unlike Recovery driving another agent's pane).
set -euo pipefail

# --- Defaults (override via flags). -----------------------------------------
SERVICE=""
TARGET_PANE=""
MODE="user"
HANDOFF_FILE="${HOME}/HANDOFF.md"
STEADY_TIMEOUT="90"          # claude + plugin + bun boot can exceed the wrapped script's 60s default
SELF_RESTART_DELAY="3"       # lets the launching turn flush before the restart kills it
HANDOFF_FRESH_HOURS="1"
UNIT=""                      # derived from --service if unset
DRY_RUN=0
RESTART_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/handoff_and_restart.sh"

usage() { sed -n '2,40p' "$0"; }

log() { echo "[self-restart] $*"; }
err() { echo "[self-restart] ERROR: $*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --service)             SERVICE="$2"; shift 2 ;;
    --target-pane)         TARGET_PANE="$2"; shift 2 ;;
    --mode)                MODE="$2"; shift 2 ;;
    --handoff-file)        HANDOFF_FILE="$2"; shift 2 ;;
    --steady-timeout)      STEADY_TIMEOUT="$2"; shift 2 ;;
    --self-restart-delay)  SELF_RESTART_DELAY="$2"; shift 2 ;;
    --unit)                UNIT="$2"; shift 2 ;;
    --handoff-fresh-hours) HANDOFF_FRESH_HOURS="$2"; shift 2 ;;
    --dry-run)             DRY_RUN=1; shift ;;
    -h|--help)             usage; exit 0 ;;
    *) err "unknown arg: $1"; exit 64 ;;
  esac
done

# --- Validate required flags. -----------------------------------------------
[[ -n "$SERVICE" ]]     || { err "--service is required"; exit 64; }
[[ -n "$TARGET_PANE" ]] || { err "--target-pane is required"; exit 64; }
case "$MODE" in
  user|system) ;;
  *) err "--mode must be 'user' or 'system' (got '$MODE')"; exit 64 ;;
esac

# Derive the transient unit name from the service if not given (must be unique
# per agent so a fixed name doesn't collide across agents on the same bus).
if [[ -z "$UNIT" ]]; then
  UNIT="self-restart-$(echo "${SERVICE%.service}" | tr -c 'a-zA-Z0-9' '-' | sed 's/-*$//')"
fi

# --mode threads into BOTH the transient-unit scope (systemctl/systemd-run) and
# the wrapped handoff_and_restart.sh. user-scope needs --user + XDG_RUNTIME_DIR;
# system-scope (root context) needs neither.
if [[ "$MODE" == "user" ]]; then
  SCOPE_FLAG="--user"
  RUNTIME_DIR="/run/user/$(id -u)"
  TMUX_ENV=(env "XDG_RUNTIME_DIR=$RUNTIME_DIR")
else
  SCOPE_FLAG=""
  RUNTIME_DIR=""
  TMUX_ENV=()
fi

# --- Preflight 1: the tmux session the kick targets must exist. -------------
# target-pane is "<session>:<window>" — strip the window/pane suffix for has-session.
TMUX_SESSION="${TARGET_PANE%%:*}"
if ! "${TMUX_ENV[@]}" tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
  err "tmux session '$TMUX_SESSION' not found — cannot target the fresh pane"
  exit 1
fi

# --- Preflight 2: the restart script must exist and be executable. ----------
if [[ ! -x "$RESTART_SCRIPT" ]]; then
  err "restart script not found/executable: $RESTART_SCRIPT"
  exit 1
fi

# --- Preflight 3: HANDOFF freshness (warn-only — the kick reads whatever is there).
if [[ -f "$HANDOFF_FILE" ]]; then
  mtime=$(stat -c %Y "$HANDOFF_FILE" 2>/dev/null || echo 0)
  age_h=$(( ( $(date +%s) - mtime ) / 3600 ))
  if (( age_h >= HANDOFF_FRESH_HOURS )); then
    log "WARNING: $HANDOFF_FILE is ${age_h}h old (>= ${HANDOFF_FRESH_HOURS}h) — fresh session will read a STALE handoff. Refresh it first."
  else
    log "HANDOFF ok (${age_h}h old): $HANDOFF_FILE"
  fi
else
  log "WARNING: $HANDOFF_FILE missing — fresh session's SessionStart auto-read will be skipped."
fi

# --- The detached command: wait briefly (let the launching turn flush), then
# run the restart+kick. exec so the transient unit's MainPID is the restart script.
INNER="sleep ${SELF_RESTART_DELAY}; exec ${RESTART_SCRIPT} --skip-ack --service ${SERVICE} --mode ${MODE} --target-pane ${TARGET_PANE} --handoff-file ${HANDOFF_FILE} --steady-timeout ${STEADY_TIMEOUT}"

# systemd-run setenv only matters for user scope (XDG_RUNTIME_DIR for the bus).
RUN_ENV_ARGS=()
[[ "$MODE" == "user" ]] && RUN_ENV_ARGS=(--setenv=XDG_RUNTIME_DIR="$RUNTIME_DIR")

if [[ "$DRY_RUN" == "1" ]]; then
  log "DRY RUN — would launch transient unit '$UNIT' (mode=$MODE) with:"
  echo "  systemd-run $SCOPE_FLAG --collect --unit=$UNIT ${RUN_ENV_ARGS[*]} -- bash -c '$INNER'"
  exit 0
fi

log "firing detached self-restart (unit=$UNIT, mode=$MODE, service=$SERVICE, pane=$TARGET_PANE, steady=${STEADY_TIMEOUT}s)"
# Clear any lingering failed instance from a prior run so systemd-run can reuse
# the fixed unit name (a failed unit would otherwise block the launch).
"${TMUX_ENV[@]}" systemctl $SCOPE_FLAG reset-failed "$UNIT" 2>/dev/null || true
# shellcheck disable=SC2086  # SCOPE_FLAG is intentionally word-split (empty for system scope).
systemd-run $SCOPE_FLAG --collect --unit="$UNIT" \
  "${RUN_ENV_ARGS[@]}" \
  -- bash -c "$INNER"
log "detached unit launched; this session will be restarted in ~${SELF_RESTART_DELAY}s. Fresh session will be kicked to read $HANDOFF_FILE and announce."
