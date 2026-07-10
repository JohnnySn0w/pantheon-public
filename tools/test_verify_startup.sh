#!/usr/bin/env bash
# Smoke tests for tools/verify_startup.sh (T-0728).
#
# Strategy mirrors test_handoff_and_restart.sh: per-case sandbox with a fake
# `tmux` (capture-pane reads $SBX/pane_text; switches to $SBX/pane_text_after
# once the boot-dialog dismiss `-l 1` is sent; send-keys recorded), fake `curl`.
# All cases run --agent recovery as the current user → self-mode (the SessionStart
# case), so no sudo path is exercised here (cross-account is covered by review).
#
# Self-reports "N passed, M failed"; exits non-zero on any failure.

set -uo pipefail
SUT="$(cd "$(dirname "$0")" && pwd)/verify_startup.sh"
PASS=0; FAIL=0; FAILED_TESTS=()

setup_sandbox() {
  SBX=$(mktemp -d -t verify_startup_test.XXXXXX)
  FAKEBIN="$SBX/bin"; mkdir -p "$FAKEBIN"
  : > "$SBX/pane_text"
  : > "$SBX/sendkeys_args"

  cat > "$FAKEBIN/tmux" <<EOF
#!/bin/bash
case "\${1:-}" in
  send-keys)
    echo "\$*" >> "$SBX/sendkeys_args"
    if [[ "\$*" == *"-l 1" ]]; then echo 1 > "$SBX/boot_dismissed"; fi
    exit 0 ;;
  capture-pane)
    if [[ -f "$SBX/capture_fails" ]]; then exit 1; fi
    if [[ -f "$SBX/pane_text_after" && -f "$SBX/boot_dismissed" ]]; then
      cat "$SBX/pane_text_after"
    else
      cat "$SBX/pane_text"
    fi
    exit 0 ;;
  *) exit 0 ;;
esac
EOF
  chmod +x "$FAKEBIN/tmux"

  cat > "$FAKEBIN/curl" <<EOF
#!/bin/bash
[[ -f "$SBX/curl_response" ]] && cat "$SBX/curl_response"
exit 0
EOF
  chmod +x "$FAKEBIN/curl"

  # Fake git: services only `config --get core.hooksPath` for githooks_check
  # (C7). Returns $SBX/hookspath_value if set, else .githooks (the healthy
  # default so unrelated cases see an active note). Everything else exits 0.
  cat > "$FAKEBIN/git" <<EOF
#!/bin/bash
if [[ "\$*" == *"config --get core.hooksPath"* ]]; then
  if [[ -f "$SBX/hookspath_value" ]]; then cat "$SBX/hookspath_value"; else echo ".githooks"; fi
  exit 0
fi
exit 0
EOF
  chmod +x "$FAKEBIN/git"

  # Fake systemctl: services timers_check (C8). list-units emits $SBX/timers_list
  # (empty by default → no timers → unrelated cases see the healthy note); show
  # emits the requested property from \$SBX/timerstate_<unit>. Everything else
  # (e.g. service_check, skipped in self-mode) exits 0.
  cat > "$FAKEBIN/systemctl" <<EOF
#!/bin/bash
if [[ "\$*" == *"list-units"* ]]; then
  [[ -f "$SBX/timers_list" ]] && cat "$SBX/timers_list"
  exit 0
fi
if [[ "\$*" == *"show"* ]]; then
  unit=""; prop=""; prev=""
  for a in "\$@"; do
    [[ "\$a" == *.timer ]] && unit="\$a"
    [[ "\$prev" == "-p" ]] && prop="\$a"
    prev="\$a"
  done
  sf="$SBX/timerstate_\$unit"
  if [[ -n "\$prop" && -f "\$sf" ]] && grep -q "^\$prop=" "\$sf"; then
    grep "^\$prop=" "\$sf"
  else
    echo "\$prop="
  fi
  exit 0
fi
exit 0
EOF
  chmod +x "$FAKEBIN/systemctl"

  export PATH="$FAKEBIN:$PATH"
}
teardown_sandbox() { rm -rf "$SBX"; }

run_test() {
  local name="$1" expected="$2"; shift 2
  setup_sandbox
  declare -f "preflight_${name}" >/dev/null && "preflight_${name}"
  local actual=0
  "$SUT" "$@" >"$SBX/stdout" 2>"$SBX/stderr" || actual=$?
  local ok=1
  [[ "$actual" -eq "$expected" ]] || ok=0
  if [[ "$ok" == 1 ]] && declare -f "postcheck_${name}" >/dev/null; then
    "postcheck_${name}" || ok=0
  fi
  if [[ "$ok" == 1 ]]; then
    echo "  PASS: $name (exit=$actual)"; PASS=$((PASS+1))
  else
    echo "  FAIL: $name (expected=$expected got=$actual)"
    echo "  --- stdout ---"; cat "$SBX/stdout"; echo "  --- stderr ---"; cat "$SBX/stderr"
    FAIL=$((FAIL+1)); FAILED_TESTS+=("$name")
  fi
  teardown_sandbox
}

IDLE_PANE='╭─────────────────────────────────────╮
│ ❯ Try "what is the weather"         │
╰─────────────────────────────────────╯
  ? for shortcuts'

BOOT_DIALOG='  WARNING: Loading development channels
────────────────────────────────────────────────────────────────────────────────
  ❯ 1. I am using this for local development
    2. Exit
────────────────────────────────────────────────────────────────────────────────
  Enter to confirm · Esc to cancel'

DESTRUCTIVE_DIALOG='  Bash command
    rm -rf /tmp/x
────────────────────────────────────────────────────────────────────────────────
  ❯ 1. Yes
    2. No
────────────────────────────────────────────────────────────────────────────────
  Do you want to proceed?
  Esc to cancel · Tab to amend'

# --- cases ---
preflight_healthy_self() { printf '%s' "$IDLE_PANE" > "$SBX/pane_text"; }

preflight_pane_not_idle() { printf '%s' "$DESTRUCTIVE_DIALOG" > "$SBX/pane_text"; }
postcheck_pane_not_idle() {  # fail-closed: a destructive dialog must NOT be dismissed
  if grep -q -- "-l 1" "$SBX/sendkeys_args"; then echo "  (violation: pressed 1 on destructive dialog)"; return 1; fi
  return 0
}

preflight_capture_fails() { : > "$SBX/capture_fails"; }  # capture failure → SOFT → exit 0

preflight_boot_dialog_dismissed() {
  printf '%s' "$BOOT_DIALOG" > "$SBX/pane_text"
  printf '%s' "$IDLE_PANE"   > "$SBX/pane_text_after"
}
postcheck_boot_dialog_dismissed() {
  grep -q "dismissing inline" "$SBX/stdout" || { echo "  (no dismiss logged)"; return 1; }
  grep -q -- "-l 1" "$SBX/sendkeys_args" || { echo "  (no '1' sent)"; return 1; }
  return 0
}

preflight_denylist_blocks() {  # boot sentinel in scrollback + destructive focused
  printf '%s\n%s' "  (scrollback) I am using this for local development" "$DESTRUCTIVE_DIALOG" > "$SBX/pane_text"
}
postcheck_denylist_blocks() {
  if grep -q -- "-l 1" "$SBX/sendkeys_args"; then echo "  (violation: dismissed despite denylist)"; return 1; fi
  return 0
}

preflight_no_dewedge() { printf '%s' "$BOOT_DIALOG" > "$SBX/pane_text"; printf '%s' "$IDLE_PANE" > "$SBX/pane_text_after"; }
postcheck_no_dewedge() {
  if grep -q -- "-l 1" "$SBX/sendkeys_args"; then echo "  (violation: dismissed with --no-dewedge)"; return 1; fi
  return 0
}

preflight_liveness_downgrade() {  # dialog footer in scrollback (ABOVE bottom-6) but bottom shows live input
  printf '%s\n' \
    "  Enter to confirm · Esc to cancel  (captured peer dialog, scrolled up)" \
    "  ● work output line 1" "  ● work output line 2" "  ● work output line 3" \
    "$IDLE_PANE" > "$SBX/pane_text"
}

# C7 (T-1091): hooksPath parity-guard. Pane idle in both → exit stays 0 (SOFT);
# only the note/warn text differs by the faked core.hooksPath value.
preflight_githooks_active()   { printf '%s' "$IDLE_PANE" > "$SBX/pane_text"; echo ".githooks"  > "$SBX/hookspath_value"; }
postcheck_githooks_active()   { grep -q "git hooks active"     "$SBX/stdout" || { echo "  (no active note)";    return 1; }; }
preflight_githooks_reverted() { printf '%s' "$IDLE_PANE" > "$SBX/pane_text"; echo ".git/hooks" > "$SBX/hookspath_value"; }
postcheck_githooks_reverted() { grep -q "git hooks NOT active" "$SBX/stdout" || { echo "  (no revert warning)"; return 1; }; }

# C4 token-grep regression (tidy-skim 2026-06-11): the plugin .env key is
# TELEGRAM_BOT_TOKEN; the old '^BOT_TOKEN=' anchor never matched it, so the
# getMe probe was silently dead fleet-wide. With the real Recovery .env present
# (self-mode reads it) + a faked ok:true getMe, there must be NO "token not
# readable" warning — i.e. the token IS now found. Skips cleanly if the .env
# is absent in this environment (the warning is then legitimate).
preflight_telegram_token_found() { printf '%s' "$IDLE_PANE" > "$SBX/pane_text"; printf '{"ok": true}' > "$SBX/curl_response"; }
postcheck_telegram_token_found() {
  [[ -r "/home/claude-fallback/.claude/channels/telegram/.env" ]] || { echo "  (skip: no .env here)"; return 0; }
  if grep -q "token not readable" "$SBX/stdout"; then echo "  (token still not found — grep pattern regressed)"; return 1; fi
  return 0
}

# C8 (T-1097): timer-stall watchdog. Pane idle → exit stays 0 (SOFT) in BOTH;
# only the note/warn differs by the faked timer state. A healthy monotonic timer
# (finite NextElapseUSecMonotonic) must NOT flag; a stalled one (monotonic=
# infinity + realtime empty + never triggered) MUST flag. Guards against the
# false-positive traps (calendar monotonic=0, the --value=infinity quirk).
_TIMER=pantheon-inbox-consumer@recovery.timer
preflight_timer_healthy() {
  printf '%s' "$IDLE_PANE" > "$SBX/pane_text"
  echo "$_TIMER" > "$SBX/timers_list"
  printf 'NextElapseUSecMonotonic=1d 7h 13min\nNextElapseUSecRealtime=\nLastTriggerUSec=Thu 2026-06-11 05:56:52 UTC\n' > "$SBX/timerstate_$_TIMER"
}
postcheck_timer_healthy() { grep -q "no stalled timers" "$SBX/stdout" || { echo "  (healthy timer not recognized)"; return 1; }; }
preflight_timer_stalled() {
  printf '%s' "$IDLE_PANE" > "$SBX/pane_text"
  echo "$_TIMER" > "$SBX/timers_list"
  printf 'NextElapseUSecMonotonic=infinity\nNextElapseUSecRealtime=\nLastTriggerUSec=\n' > "$SBX/timerstate_$_TIMER"
}
postcheck_timer_stalled() { grep -q "stalled timer(s)" "$SBX/stdout" || { echo "  (stalled timer not flagged)"; return 1; }; }

# --at-boot (T-1099): SessionStart-hook mode MUST exit 0 even when the pane would
# otherwise wedge (rc=7) — the pane is unsettled mid-boot, so C1-C3 is skipped.
# Feed a DESTRUCTIVE_DIALOG (normally → wedged/rc=7) and assert --at-boot still
# exits 0 + logs the pane-skip (proves it can never brick a boot).
preflight_at_boot_never_wedges() { printf '%s' "$DESTRUCTIVE_DIALOG" > "$SBX/pane_text"; }
postcheck_at_boot_never_wedges() {
  grep -q "pane check skipped" "$SBX/stdout" || { echo "  (pane not skipped at boot)"; return 1; }
  if grep -q -- "-l 1" "$SBX/sendkeys_args"; then echo "  (violation: sent keys at boot — no-self-inject breached)"; return 1; fi
  return 0
}

echo "=== verify_startup.sh smoke ==="
run_test healthy_self           0 --agent recovery
run_test pane_not_idle          7 --agent recovery
run_test capture_fails          0 --agent recovery
run_test boot_dialog_dismissed  0 --agent recovery
run_test denylist_blocks        7 --agent recovery
run_test no_dewedge             7 --agent recovery --no-dewedge
run_test liveness_downgrade     0 --agent recovery
run_test githooks_active        0 --agent recovery
run_test githooks_reverted      0 --agent recovery
run_test telegram_token_found   0 --agent recovery
run_test timer_healthy          0 --agent recovery
run_test timer_stalled          0 --agent recovery
run_test at_boot_never_wedges   0 --agent recovery --at-boot
run_test unknown_agent         64 --agent bogus

# --- constants must stay byte-equal with handoff_and_restart.sh (drift guard) ---
echo "=== constant-drift guard ==="
HR="$(cd "$(dirname "$0")" && pwd)/handoff_and_restart.sh"
drift=0
for v in BOOT_DIALOG_SENTINEL BOOT_DISMISS_DENYLIST VERIFY_PATTERN IDLE_SENTINEL DIALOG_EXCLUDE; do
  # Compare the single-quoted VALUE only (handoff_and_restart.sh has trailing
  # inline comments; verify_startup.sh doesn't — the values must match).
  a="$(sed -n "s/^$v=\('[^']*'\).*/\1/p" "$SUT" | head -1)"
  b="$(sed -n "s/^$v=\('[^']*'\).*/\1/p" "$HR" | head -1)"
  if [[ "$a" != "$b" ]]; then
    echo "  FAIL: $v drifted between verify_startup.sh and handoff_and_restart.sh"
    echo "    verify_startup: $a"; echo "    handoff_restart: $b"
    drift=1
  fi
done
if [[ "$drift" == 0 ]]; then echo "  PASS: shared constants byte-equal"; PASS=$((PASS+1)); else FAIL=$((FAIL+1)); FAILED_TESTS+=("constant_drift"); fi

# --- T-0816: the retired bridge-client path must not regress. All three agents
# self-poll their own bot (T-0767); telegram_check probes own-bot getMe only —
# no AGENT_BRIDGE_CLIENT map, no bridge /health probe. ---
echo "=== bridge-client retirement guard (T-0816) ==="
bridge_resid=0
if grep -qE 'AGENT_BRIDGE_CLIENT|HEALTH_URL|8765' "$SUT"; then
  echo "  FAIL: residual bridge-client probe (map / health-URL / :8765) in verify_startup.sh"
  bridge_resid=1
fi
if ! grep -q 'getMe' "$SUT"; then
  echo "  FAIL: telegram_check own-bot getMe plugin probe missing"
  bridge_resid=1
fi
if [[ "$bridge_resid" == 0 ]]; then echo "  PASS: bridge path retired; single plugin getMe probe for all agents"; PASS=$((PASS+1)); else FAIL=$((FAIL+1)); FAILED_TESTS+=("bridge_retirement"); fi

echo
echo "Results: $PASS passed, $FAIL failed"
(( FAIL == 0 )) || { printf 'Failed: %s\n' "${FAILED_TESTS[*]}"; exit 1; }
