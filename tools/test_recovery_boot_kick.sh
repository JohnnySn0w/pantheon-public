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

# Stub submit command: records each call to $WITNESS, exit code from $STUB_RC.
STUB="$TMP/fake_send.sh"
cat > "$STUB" <<'STUBEOF'
#!/usr/bin/env bash
echo "SENT pane=$1" >> "$WITNESS"
exit "${STUB_RC:-0}"
STUBEOF
chmod +x "$STUB"

IDLE_PANE=$'╭────────────────────────────╮\n❯ \n╰────────────────────────────╯'
DIALOG_PANE=$'╭────────────────────────────╮\n❯ Yes\n  No\nEnter to confirm · Esc to cancel'
JUNK_PANE='booting... please wait'

pass=0; fail=0
check() { # <desc> <actual> <expected>
  if [[ "$2" == "$3" ]]; then pass=$((pass+1)); echo "PASS: $1";
  else fail=$((fail+1)); echo "FAIL: $1 (got '$2', want '$3')"; fi
}

# run_case reads INFLIGHT/CAPTURE/TO/STUB_RC from its (prefixed) env; uses parent $WITNESS.
run_case() {
  ( export WITNESS STUB_RC="${STUB_RC:-0}" \
           RECOVERY_KICK_SEND_CMD="$STUB" \
           RECOVERY_KICK_SENTINEL_TIMEOUT="${TO:-3}" \
           RECOVERY_KICK_POLL_INTERVAL="1" \
           RECOVERY_KICK_INFLIGHT_CMD="${INFLIGHT:-false}" \
           RECOVERY_KICK_CAPTURE_CMD="${CAPTURE:-printf %s \"\"}"
    "$KICK" "$PANE" recovery >/dev/null 2>&1 )
  echo $?
}
kick_count() { grep -c '^SENT ' "$WITNESS" 2>/dev/null || true; }

# 1. Kicker in flight -> skip, no send.
: > "$WITNESS"; rc=$(INFLIGHT="true" CAPTURE="printf '%s' '$IDLE_PANE'" run_case)
check "in-flight kicker: exit 0" "$rc" "0"
check "in-flight kicker: no send" "$(kick_count)" "0"

# 2. Bare respawn + idle prompt -> kick fires exactly once.
: > "$WITNESS"; rc=$(INFLIGHT="false" CAPTURE="printf '%s' '$IDLE_PANE'" run_case)
check "bare+idle: exit 0" "$rc" "0"
check "bare+idle: kicked once" "$(kick_count)" "1"

# 3. Bare respawn but sentinel never appears -> no kick (timeout), still exit 0.
: > "$WITNESS"; rc=$(INFLIGHT="false" CAPTURE="printf '%s' '$JUNK_PANE'" TO="2" run_case)
check "no-sentinel: exit 0" "$rc" "0"
check "no-sentinel: no send" "$(kick_count)" "0"

# 4. Confirm-dialog modal present -> disqualified, no kick.
: > "$WITNESS"; rc=$(INFLIGHT="false" CAPTURE="printf '%s' '$DIALOG_PANE'" TO="2" run_case)
check "dialog-modal: exit 0" "$rc" "0"
check "dialog-modal: no send (negative prong)" "$(kick_count)" "0"

# 5. Send fails -> still exit 0 (fail-open), one attempt recorded.
: > "$WITNESS"; rc=$(STUB_RC="1" INFLIGHT="false" CAPTURE="printf '%s' '$IDLE_PANE'" run_case)
check "send-fails: exit 0 (fail-open)" "$rc" "0"
check "send-fails: one attempt" "$(kick_count)" "1"

echo "----------------------------------------"
echo "$pass passed, $fail failed"
[[ "$fail" -eq 0 ]]
