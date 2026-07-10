#!/bin/bash
# Test harness for tools/self_restart.sh (T-0531 generalization).
#
# Strategy mirrors test_handoff_and_restart.sh: a sandbox per case with fake
# `tmux`, `systemctl`, and `systemd-run` on PATH. self_restart.sh launches a
# DETACHED transient unit (it does not block on the restart), so the testable
# surface is: arg validation, preflights, transient-unit-name derivation, the
# --mode user/system scope split, and the constructed systemd-run command. The
# --dry-run path prints that command without firing; the non-dry-run path is
# checked by faking systemd-run + systemctl and asserting on recorded args.
#
# Invocation: bash tools/test_self_restart.sh
# Exit: 0 = all pass; 1 = at least one failure.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUT="${SCRIPT_DIR}/self_restart.sh"

PASS=0
FAIL=0
FAILED_TESTS=()

setup_sandbox() {
  SBX=$(mktemp -d -t self_restart_test.XXXXXX)
  FAKEBIN="$SBX/bin"
  mkdir -p "$FAKEBIN"

  echo "0" > "$SBX/tmux_has_session_rc"   # 0 = session exists; 1 = not found
  : > "$SBX/systemd_run_args"             # one line: args of systemd-run invocation
  : > "$SBX/systemctl_args"               # lines: args of systemctl invocations

  # Fake tmux: has-session honors the rc flag; everything else is a no-op success.
  cat > "$FAKEBIN/tmux" <<EOF
#!/bin/bash
case "\${1:-}" in
  has-session) exit \$(cat "$SBX/tmux_has_session_rc") ;;
  *) exit 0 ;;
esac
EOF
  chmod +x "$FAKEBIN/tmux"

  # Fake systemd-run: record args, succeed (do NOT actually spawn anything).
  cat > "$FAKEBIN/systemd-run" <<EOF
#!/bin/bash
echo "\$*" >> "$SBX/systemd_run_args"
exit 0
EOF
  chmod +x "$FAKEBIN/systemd-run"

  # Fake systemctl: record args (covers reset-failed), succeed.
  cat > "$FAKEBIN/systemctl" <<EOF
#!/bin/bash
echo "\$*" >> "$SBX/systemctl_args"
exit 0
EOF
  chmod +x "$FAKEBIN/systemctl"

  export PATH="$FAKEBIN:$PATH"

  # A real, fresh handoff file so the warn-only freshness preflight stays quiet.
  HANDOFF="$SBX/HANDOFF.md"
  echo "handoff content" > "$HANDOFF"
}

teardown_sandbox() { rm -rf "$SBX"; }

run_test() {
  local name="$1"; shift
  local expected_exit="$1"; shift

  setup_sandbox
  if declare -f "preflight_${name}" >/dev/null; then "preflight_${name}"; fi

  local actual_exit=0
  bash "$SUT" "$@" >"$SBX/stdout" 2>"$SBX/stderr" || actual_exit=$?

  if [[ "$actual_exit" -eq "$expected_exit" ]]; then
    if declare -f "postcheck_${name}" >/dev/null; then
      if "postcheck_${name}"; then
        echo "  PASS: $name (exit=$actual_exit)"; PASS=$((PASS+1))
      else
        echo "  FAIL: $name (exit ok=$actual_exit, postcheck failed)"
        echo "  --- stdout ---"; cat "$SBX/stdout"
        echo "  --- stderr ---"; cat "$SBX/stderr"
        FAIL=$((FAIL+1)); FAILED_TESTS+=("$name")
      fi
    else
      echo "  PASS: $name (exit=$actual_exit)"; PASS=$((PASS+1))
    fi
  else
    echo "  FAIL: $name (expected exit=$expected_exit, got $actual_exit)"
    echo "  --- stdout ---"; cat "$SBX/stdout"
    echo "  --- stderr ---"; cat "$SBX/stderr"
    FAIL=$((FAIL+1)); FAILED_TESTS+=("$name")
  fi
  teardown_sandbox
}

# --- 1: dry-run, user mode (default). Derived unit + --user + XDG + --skip-ack. ---
postcheck_dryrun_user() {
  grep -q -- "--user" "$SBX/stdout" || { echo "    expected --user in systemd-run line"; return 1; }
  grep -q "XDG_RUNTIME_DIR" "$SBX/stdout" || { echo "    expected XDG_RUNTIME_DIR setenv"; return 1; }
  grep -q "self-restart-claude-telegram-fallback" "$SBX/stdout" || { echo "    expected derived unit name"; return 1; }
  grep -q -- "--skip-ack" "$SBX/stdout" || { echo "    expected --skip-ack in inner cmd"; return 1; }
  # Dry-run must NOT fire systemd-run or systemctl.
  [[ ! -s "$SBX/systemd_run_args" ]] || { echo "    dry-run fired systemd-run"; return 1; }
  return 0
}

# --- 2: dry-run, system mode. No --user, no XDG setenv. ---------------------
postcheck_dryrun_system() {
  if grep -q "XDG_RUNTIME_DIR" "$SBX/stdout"; then echo "    system mode must not setenv XDG"; return 1; fi
  # The bare 'systemd-run --collect' (no '--user' token) is the system-scope shape.
  grep -qE "systemd-run +--collect" "$SBX/stdout" || { echo "    expected bare 'systemd-run --collect' for system scope"; return 1; }
  return 0
}

# --- 6: tmux session not found → exit 1. ------------------------------------
preflight_no_session() { echo "1" > "$SBX/tmux_has_session_rc"; }

# --- 7: explicit --unit override wins over derivation. ----------------------
postcheck_unit_override() {
  grep -q "my-custom-unit" "$SBX/stdout" || { echo "    expected explicit --unit name"; return 1; }
  return 0
}

# --- 9: real (non-dry-run) fire: systemd-run + reset-failed invoked correctly. ---
postcheck_real_fire() {
  grep -q -- "--unit=self-restart-claude-telegram-fallback" "$SBX/systemd_run_args" \
    || { echo "    systemd-run not invoked with derived --unit"; return 1; }
  grep -q -- "--skip-ack" "$SBX/systemd_run_args" \
    || { echo "    inner cmd missing --skip-ack"; return 1; }
  grep -q "handoff_and_restart.sh" "$SBX/systemd_run_args" \
    || { echo "    inner cmd missing handoff_and_restart.sh"; return 1; }
  grep -q "reset-failed self-restart-claude-telegram-fallback" "$SBX/systemctl_args" \
    || { echo "    reset-failed not called for the derived unit"; return 1; }
  return 0
}

# --- 10: recovery_self_restart.sh back-compat wrapper forwards Recovery defaults. ----
postcheck_recovery_wrapper() {
  grep -q -- "--unit=recovery-self-restart" "$SBX/stdout" \
    || { echo "    recovery wrapper should pin --unit recovery-self-restart"; return 1; }
  grep -q "claude-telegram-fallback.service" "$SBX/stdout" \
    || { echo "    recovery wrapper should default to claude-telegram-fallback.service"; return 1; }
  return 0
}

COMMON=(--service claude-telegram-fallback.service --target-pane claude-telegram-bot-fallback:0)

if [[ ! -f "$SUT" ]]; then echo "FATAL: SUT not found at $SUT"; exit 1; fi

echo "Running self_restart.sh tests..."
echo

run_test dryrun_user 0 "${COMMON[@]}" --dry-run
run_test dryrun_system 0 "${COMMON[@]}" --mode system --dry-run
run_test missing_service 64 --target-pane foo:0 --dry-run
run_test missing_pane 64 --service foo.service --dry-run
run_test bad_mode 64 "${COMMON[@]}" --mode sideways --dry-run
run_test no_session 1 "${COMMON[@]}"
run_test unit_override 0 "${COMMON[@]}" --unit my-custom-unit --dry-run
run_test real_fire 0 "${COMMON[@]}"

# Back-compat: drive the Recovery wrapper itself (dry-run, default substrate).
RECOVERY_SUT="${SCRIPT_DIR}/recovery_self_restart.sh"
run_recovery_wrapper() {
  setup_sandbox
  local actual_exit=0
  bash "$RECOVERY_SUT" --dry-run >"$SBX/stdout" 2>"$SBX/stderr" || actual_exit=$?
  if [[ "$actual_exit" -eq 0 ]] && postcheck_recovery_wrapper; then
    echo "  PASS: recovery_wrapper (exit=$actual_exit)"; PASS=$((PASS+1))
  else
    echo "  FAIL: recovery_wrapper (exit=$actual_exit)"
    echo "  --- stdout ---"; cat "$SBX/stdout"
    echo "  --- stderr ---"; cat "$SBX/stderr"
    FAIL=$((FAIL+1)); FAILED_TESTS+=("recovery_wrapper")
  fi
  teardown_sandbox
}
run_recovery_wrapper

echo
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  echo "Failed tests: ${FAILED_TESTS[*]}"
  exit 1
fi
exit 0
