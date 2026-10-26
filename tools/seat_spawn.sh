#!/usr/bin/env bash
# tools/seat_spawn.sh — canonical ORG-043 seat-session spawn (T-1974).
#
# Run AS the seat account; the HO invokes it cross-account:
#   sudo -u suborch1 /home/suborch1/pantheon-config/tools/seat_spawn.sh
#
# WHY THIS EXISTS: seats were spawned ad hoc (`sudo -u suborchN bash -lc
# 'tmux new-session ...'` — mason and artisan even differed in shape), and a
# sudo-spawned tmux SERVER bakes SUDO_USER/SUDO_UID/SUDO_GID/SUDO_COMMAND
# into its environment — inherited by the agent process and EVERY tool it
# ever runs. Seat tool runs then diverge from a normal login on any
# SUDO_USER-sensitive code path (T-1974 ruling: scrub, don't document-around).
# This wrapper is the one sanctioned spawn: it scrubs the sudo vars before
# the tmux server is created, so the whole session tree looks like a normal
# login. Idempotent: no-op if the session already exists.
set -euo pipefail

SEAT_USER="$(id -un)"
SESSION="${SEAT_SESSION:-${SEAT_USER}-main}"
CLAUDE_BIN="${SEAT_CLAUDE_BIN:-$HOME/.npm-global/bin/claude}"
# Extra launch args via SEAT_CLAUDE_ARGS (word-split deliberately).
SEAT_CLAUDE_ARGS="${SEAT_CLAUDE_ARGS:-}"

case "$SEAT_USER" in
  suborch*) ;;
  *) echo "[seat_spawn] refusing: $SEAT_USER is not a seat account (suborch*)" >&2; exit 64 ;;
esac

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "[seat_spawn] session $SESSION already exists — no-op (idempotent)"
  exit 0
fi

# ORG-106 L3a (T-5526): a deep-sleeping seat is never launched. Fail-open on a fault.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_lc_rc=0
python3 "$SCRIPT_DIR/seat_state.py" launch-check --account "$SEAT_USER" || _lc_rc=$?
if [[ $_lc_rc -eq 3 ]]; then
  echo "[seat_spawn] refusing: $SEAT_USER is in deep-sleep; wake it first (harness <seat> wake)" >&2
  exit 75   # EX_TEMPFAIL: ORG-106 deep-sleep, nothing touched
fi

# The scrub. env -u removes the vars for the tmux SERVER this command forks,
# which is what every future window/pane/respawn inherits from.
exec env -u SUDO_USER -u SUDO_UID -u SUDO_GID -u SUDO_COMMAND \
  tmux new-session -d -s "$SESSION" -c "$HOME" \
  "$CLAUDE_BIN $SEAT_CLAUDE_ARGS"
