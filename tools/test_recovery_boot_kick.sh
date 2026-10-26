#!/usr/bin/env bash
# test_recovery_boot_kick.sh — T-2453. Exercises recovery_boot_kick.sh's guards via its
# test seams (RECOVERY_KICK_INFLIGHT_CMD / RECOVERY_KICK_CAPTURE_CMD / RECOVERY_KICK_SEND_CMD)
# so no real tmux or Claude session is needed. Expect: "N passed, 0 failed", exit 0.
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KICK="$SCRIPT_DIR/recovery_boot_kick.sh"
PANE="claude-telegram-bot-fallback:0"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
WITNESS="$TMP/witness"; : > "$WITNESS"

# Stub submit command: records each call's argv to $WITNESS, exit code from $STUB_RC.
STUB="$TMP/fake_send.sh"
cat > "$STUB" <<'STUBEOF'
#!/usr/bin/env bash
echo "SENT $*" >> "$WITNESS"
exit "${STUB_RC:-0}"
STUBEOF
chmod +x "$STUB"

# T-4459: stub journal. recovery_boot_kick logs every line via `logger -t pantheon-recovery-boot-kick`
# (post_start sends its stdout/stderr to DEVNULL); the seam records them here instead.
JOURNAL="$TMP/journal"; : > "$JOURNAL"
LOGGER="$TMP/fake_logger.sh"
cat > "$LOGGER" <<'LOGEOF'
#!/usr/bin/env bash
echo "$1" >> "$JOURNAL"
LOGEOF
chmod +x "$LOGGER"

IDLE_PANE=$'╭────────────────────────────╮\n❯ \n╰────────────────────────────╯'
DIALOG_PANE=$'╭────────────────────────────╮\n❯ Yes\n  No\nEnter to confirm · Esc to cancel'
JUNK_PANE='booting... please wait'
# T-4459: the current TUI draws its frame and ❯ mid-turn too. Attempt 4's kick fired into
# exactly this (a running probe turn) and sat unsent.
BUSY_PANE=$'✻ Cogitating… (4s · esc to interrupt)\n╭────────────────────────────╮\n❯ \n╰────────────────────────────╯'
BGAGENT_PANE=$'╭────────────────────────────╮\n❯ \n╰────────────────────────────╯\n  Waiting for 1 background agent'

pass=0; fail=0
check() { # <desc> <actual> <expected>
  if [[ "$2" == "$3" ]]; then pass=$((pass+1)); echo "PASS: $1";
  else fail=$((fail+1)); echo "FAIL: $1 (got '$2', want '$3')"; fi
}

# run_case reads INFLIGHT/CAPTURE/TO/STUB_RC from its (prefixed) env; uses parent $WITNESS.
run_case() {
  ( export WITNESS JOURNAL STUB_RC="${STUB_RC:-0}" \
           RECOVERY_KICK_SEND_CMD="$STUB" \
           RECOVERY_KICK_LOGGER="$LOGGER" \
           RECOVERY_KICK_SENTINEL_TIMEOUT="${TO:-3}" \
           RECOVERY_KICK_POLL_INTERVAL="1" \
           RECOVERY_KICK_INFLIGHT_CMD="${INFLIGHT:-false}" \
           RECOVERY_KICK_CAPTURE_CMD="${CAPTURE:-printf %s \"\"}"
    "$KICK" "$PANE" recovery >/dev/null 2>&1 )
  echo $?
}
kick_count() { grep -c '^SENT ' "$WITNESS" 2>/dev/null || true; }
journaled() { grep -cF -- "$1" "$JOURNAL" 2>/dev/null || true; }

# 1. Kicker in flight -> skip, no send.
: > "$WITNESS"; rc=$(INFLIGHT="true" CAPTURE="printf '%s' '$IDLE_PANE'" run_case)
check "in-flight kicker: exit 0" "$rc" "0"
check "in-flight kicker: no send" "$(kick_count)" "0"

# 2. Bare respawn + idle prompt -> kick fires exactly once.
: > "$WITNESS"; rc=$(INFLIGHT="false" CAPTURE="printf '%s' '$IDLE_PANE'" run_case)
check "bare+idle: exit 0" "$rc" "0"
check "bare+idle: kicked once" "$(kick_count)" "1"
check "bare+idle: outcome journaled with the helper's rc" \
  "$(journaled "boot kick submitted (send_keys_submit rc=0)")" "1"
# T-4455: the kick lands on a FRESH TUI, so the submit must be render-gated.
# Ungated, the Enter beat the paste render and the verify read the empty
# composer as submitted (a false rc 0; 2026-09-30 15:51:38Z, idle 2m41s).
check "bare+idle: render-gated submit to the pane" \
  "$(grep '^SENT ' "$WITNESS" | cut -d' ' -f2-4)" "--render-wait 8 $PANE"

# 3. Bare respawn but sentinel never appears -> no kick (timeout), still exit 0.
: > "$WITNESS"; rc=$(INFLIGHT="false" CAPTURE="printf '%s' '$JUNK_PANE'" TO="2" run_case)
check "no-sentinel: exit 0" "$rc" "0"
check "no-sentinel: no send" "$(kick_count)" "0"

# 4. Confirm-dialog modal present -> disqualified, no kick.
: > "$WITNESS"; rc=$(INFLIGHT="false" CAPTURE="printf '%s' '$DIALOG_PANE'" TO="2" run_case)
check "dialog-modal: exit 0" "$rc" "0"
check "dialog-modal: no send (negative prong)" "$(kick_count)" "0"

# 4b. T-4646: Claude Code 2.1.289's auto-mode startup nudge. Frame run + ❯ option cursor,
#     no confirm footer: the old conjunction passed it and the Enter picked "Yes".
NUDGE_PANE=$'────────────────────────────────────────\n Make auto mode your default permission mode?\n ❯ Yes, set auto mode as my default permission mode\n   No, keep manual'
: > "$WITNESS"; rc=$(INFLIGHT="false" CAPTURE="printf '%s' '$NUDGE_PANE'" TO="2" run_case)
check "auto-mode nudge: exit 0" "$rc" "0"
check "auto-mode nudge: no send" "$(kick_count)" "0"

# 5. Send fails -> still exit 0 (fail-open), one attempt recorded.
: > "$WITNESS"; rc=$(STUB_RC="1" INFLIGHT="false" CAPTURE="printf '%s' '$IDLE_PANE'" run_case)
check "send-fails: exit 0 (fail-open)" "$rc" "0"
check "send-fails: one attempt" "$(kick_count)" "1"
check "send-fails: failure journaled with the helper's rc" \
  "$(journaled "boot kick submit failed (send_keys_submit rc=1)")" "1"

# 6. T-4459: frame + ❯ but a turn is RUNNING -> not ready, no kick, and why is journaled.
: > "$WITNESS"; : > "$JOURNAL"
rc=$(INFLIGHT="false" CAPTURE="printf '%s' '$BUSY_PANE'" TO="2" run_case)
check "busy-turn: exit 0" "$rc" "0"
check "busy-turn: no send (the attempt-4 kick)" "$(kick_count)" "0"
check "busy-turn: journaled NOT kicking with the busy verdict" \
  "$(journaled "last verdict: busy:generating); NOT kicking")" "1"

# 7. T-4459: background agents still running (a generating footer can omit
#    "esc to interrupt", T-4193) -> no kick.
: > "$WITNESS"; : > "$JOURNAL"
rc=$(INFLIGHT="false" CAPTURE="printf '%s' '$BGAGENT_PANE'" TO="2" run_case)
check "bg-agents: no send" "$(kick_count)" "0"
check "bg-agents: journaled verdict" "$(journaled "last verdict: busy:background_agents")" "1"

# 8. T-4459: the in-flight skip is journaled too (every exit path leaves a record).
: > "$WITNESS"; : > "$JOURNAL"
rc=$(INFLIGHT="true" CAPTURE="printf '%s' '$IDLE_PANE'" run_case)
check "in-flight: skip journaled" "$(journaled "it owns the kick")" "1"

echo "----------------------------------------"
echo "$pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
