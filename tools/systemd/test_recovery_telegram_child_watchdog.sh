#!/usr/bin/env bash
# test_recovery_telegram_child_watchdog.sh — smoke for the T-1456 bun-child watchdog.
#
# Exercises the bounded-remediation state machine (debounce / cooldown / per-hour
# cap+escalate / window-pruning) WITHOUT touching Recovery's live state or firing a
# real restart: it runs the watchdog under a sandbox $HOME with --simulate-absent
# (forces the "child gone" branch, so no real pgrep/child dependency) and
# --dry-run (logs the decision instead of calling self_restart). Assert on the
# sandbox log's last line per scenario.
#
# Run after any edit to recovery-telegram-child-watchdog.sh. Exit 0 == all passed.
set -uo pipefail

WD="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/recovery-telegram-child-watchdog.sh"
[[ -x "$WD" ]] || { echo "FATAL: watchdog not executable at $WD"; exit 1; }

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT
mkdir -p "$SANDBOX/.claude/logs" "$SANDBOX/.claude/channels/telegram"
STATE="$SANDBOX/.claude/recovery-telegram-watchdog.state"
LOG="$SANDBOX/.claude/logs/recovery-telegram-watchdog.log"

PASS=0; FAIL=0
seed() { printf '%s\n' "$@" > "$STATE"; }
run()  { rm -f "$LOG"; HOME="$SANDBOX" "$WD" --simulate-absent --dry-run >/dev/null 2>&1; }
last() { tail -1 "$LOG" 2>/dev/null; }
# assert <label> <regex-the-last-log-line-must-match>
assert() {
  local label="$1" pat="$2" line; line="$(last)"
  if [[ "$line" =~ $pat ]]; then echo "  PASS: $label"; PASS=$((PASS+1))
  else echo "  FAIL: $label"; echo "        want ~ /$pat/"; echo "        got:   $line"; FAIL=$((FAIL+1)); fi
}

NOW="$(date +%s)"

echo "T1: first absent fire -> debounce, no action (consec=1)"
seed "consec=0" "last_restart=0" "restarts=" "escalated_at=0"; run
assert "consec=1 logged" 'consec=1'
# a lone debounce fire must NOT reach a FIRE/HOLD/ESCALATE decision
[[ "$(last)" =~ (WOULD\ FIRE|HOLD|ESCALATE) ]] && { echo "  FAIL: debounce leaked to action"; FAIL=$((FAIL+1)); } || { echo "  PASS: no action on 1st fire"; PASS=$((PASS+1)); }

echo "T2: second absent fire -> debounce met -> WOULD FIRE"
seed "consec=1" "last_restart=0" "restarts=" "escalated_at=0"; run
assert "would-fire self_restart" 'WOULD FIRE self_restart'

echo "T3: within cooldown of a recent restart -> HOLD"
seed "consec=1" "last_restart=$NOW" "restarts=$NOW" "escalated_at=0"; run
assert "cooldown hold" 'HOLD: within cooldown'

echo "T4: cap reached (3 in window), already escalated -> paused, no re-send"
seed "consec=1" "last_restart=$((NOW-700))" "restarts=$((NOW-300)) $((NOW-200)) $((NOW-100))" "escalated_at=$((NOW-100))"; run
assert "cap still-paused" 'ESCALATE: cap reached .*still paused'

echo "T5: window pruning (2 restarts stale) -> under cap -> WOULD FIRE"
seed "consec=1" "last_restart=$((NOW-5000))" "restarts=$((NOW-5000)) $((NOW-4000)) $((NOW-100))" "escalated_at=0"; run
assert "pruned under cap fires" 'WOULD FIRE self_restart'

echo
echo "== $PASS passed, $FAIL failed =="
[[ "$FAIL" -eq 0 ]]
