#!/usr/bin/env bash
# recovery_boot_kick.sh — T-2453. Deliver the missing first-turn KICK on a BARE recovery
# supervise respawn so a fresh session auto-resumes instead of sitting comms-dark.
#
# WHY (2026-08-03 04:13 incident): recovery-bot-supervise.sh (re)creates the tmux
# session but sends NO kick. The KICKING restart paths — self_restart.sh (recovery's
# graceful self-restart) and recovery-telegram-child-watchdog.sh — run
# handoff_and_restart.sh --skip-ack in a DETACHED transient unit that survives the
# restart and delivers a step-8 kick. But a BARE respawn (crash, a manual/model-pin
# `systemctl restart`, OR a graceful self-EXIT that did not call self_restart.sh —
# exactly the 04:13 case) has no such kicker, so the fresh session comes up IDLE
# until a human types. Recovery is the recovery fallback; its silent-idle is a fleet SPOF.
#
# THIS IS NOT THE FORBIDDEN SELF-SEND (T-0559): the kick is delivered by the
# SUPERVISOR (external to the CLI session) into the FRESH pane at boot — the same
# legitimacy as handoff_and_restart.sh kicking any other seat.
#
# FAIL-OPEN BY CONTRACT: recovery-bot-supervise.sh invokes this BACKGROUNDED and with
# `|| true`, so it can never block MainPID or fail the boot. Every step here also
# degrades to "do nothing" (session stays idle-but-alive == pre-T-2453) on any
# error/timeout — a missing kick is never worse than today.
#
# DOUBLE-KICK COORDINATION (STRUCTURAL, no flag file — .claude/rules/
# restart-pipeline-invariants.md: flag-files are backup kill-switches only). Two
# guards, both structural:
#   1. If a handoff_and_restart kicker process targeting this pane is IN FLIGHT
#      (the graceful self_restart / child-watchdog path is mid-restart), it OWNS
#      the kick -> we skip. The live process is the witness, not a marker file.
#   2. We only ever send into a CONFIRMED-IDLE prompt. A session another kicker
#      already kicked is mid-turn (non-idle), so the idle gate alone prevents a
#      double submit even if guard 1 races.
#
# Test seams (unit-testable without real tmux): RECOVERY_KICK_CAPTURE_CMD overrides the
# pane read; RECOVERY_KICK_SEND_CMD overrides the submit; RECOVERY_KICK_INFLIGHT_CMD
# overrides the in-flight-kicker probe. See test_recovery_boot_kick.sh.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PANE="${1:-claude-telegram-bot-fallback:0}"
AGENT="${2:-recovery}"

SENTINEL_TIMEOUT="${RECOVERY_KICK_SENTINEL_TIMEOUT:-180}"    # s; then give up quietly (stays idle)
POLL_INTERVAL="${RECOVERY_KICK_POLL_INTERVAL:-3}"            # s between pane polls
HANDOFF_FILE="${RECOVERY_HANDOFF_FILE:-$HOME/HANDOFF.md}"
SEND_CMD="${RECOVERY_KICK_SEND_CMD:-$SCRIPT_DIR/send_keys_submit.sh}"

# Steady-state sentinel conjunction — identical to handoff_and_restart.sh step 7.
# The `─` quantifiers MUST stay grouped: `─` is 3 bytes and in a byte locale a bare
# `─+`/`─{20,}` binds the quantifier to the last byte only and silently never matches
# (the CI-red lesson noted in handoff_and_restart.sh). Grouping is identical under UTF-8.
VERIFY_PATTERN='(╭(─)+╮|(─){20,})'                        # prompt-frame top edge
IDLE_SENTINEL='❯'                                         # positive idle-prompt cursor (T-0528)
DIALOG_EXCLUDE='Enter to confirm|Esc to cancel'          # negative: a focused confirm modal disqualifies

KICK_PROMPT="${RECOVERY_BOOT_KICK_PROMPT:-Fresh session on a bare respawn: read ${HANDOFF_FILE}, then resume your queued work / next ticket per standing priority. Post a one-line status to the operator via the Telegram reply tool only if something genuinely needs his attention.}"

log() { echo "[recovery_boot_kick] $*"; }

_now() { date +%s 2>/dev/null || echo 0; }

_capture() {
  if [[ -n "${RECOVERY_KICK_CAPTURE_CMD:-}" ]]; then
    eval "$RECOVERY_KICK_CAPTURE_CMD" 2>/dev/null || true
  else
    tmux capture-pane -t "$PANE" -p 2>/dev/null || true
  fi
}

# True if a handoff_and_restart kicker for THIS pane is already running (guard 1).
_kicker_in_flight() {
  if [[ -n "${RECOVERY_KICK_INFLIGHT_CMD:-}" ]]; then
    eval "$RECOVERY_KICK_INFLIGHT_CMD"          # test seam: exit 0 == in flight
    return $?
  fi
  # Match the live restart process (self_restart's detached unit execs
  # handoff_and_restart.sh --target-pane <this pane>). Session part of the pane is
  # enough and avoids ':' regex fuss. Same-account (recovery self-restarts as itself).
  pgrep -u "$(id -u)" -f "handoff_and_restart.*${PANE%%:*}" >/dev/null 2>&1
}

# --- guard 1: an external kicker owns this respawn -> skip (no double-kick) ---
if _kicker_in_flight; then
  log "a handoff_and_restart kicker for ${PANE} is in flight; it owns the kick — NOT kicking"
  exit 0
fi

# --- wait for the fresh session to reach a real idle prompt, then kick once ---
deadline=$(( $(_now) + SENTINEL_TIMEOUT ))
while (( $(_now) < deadline )); do
  pane_text="$(_capture)"
  if [[ -n "$pane_text" ]] \
     && printf '%s' "$pane_text" | grep -Eq "$VERIFY_PATTERN" \
     && printf '%s' "$pane_text" | grep -q "$IDLE_SENTINEL" \
     && ! printf '%s' "$pane_text" | grep -Eq "$DIALOG_EXCLUDE"; then
    log "idle prompt confirmed on $PANE; delivering boot kick"
    if "$SEND_CMD" "$PANE" "$KICK_PROMPT"; then
      log "boot kick submitted"
    else
      log "boot kick submit failed; leaving session idle-but-alive (fail-open)"
    fi
    exit 0
  fi
  sleep "$POLL_INTERVAL"
done
log "idle sentinel not seen within ${SENTINEL_TIMEOUT}s; NOT kicking (idle-but-alive == pre-T-2453)"
exit 0
