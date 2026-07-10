#!/bin/bash
# Test harness for tools/send_keys_submit.sh.
#
# T-0533: verify the standalone submit-verify helper behaves correctly for
# all three caller paths: success-first-try, success-on-retry,
# fail-after-retries, send-keys-text-failure, send-keys-enter-failure.
#
# Strategy: build a sandbox per test case with a fake `tmux` binary on PATH
# that models the real-world contract (send-keys exits 0/1, capture-pane
# returns configurable pane text). The helper's INJECT_ENTER_DELAY is
# overridden to 0.001 via environment variable so tests run fast.
#
# Invocation:
#   bash tools/test_send_keys_submit.sh
#
# Exit code: 0 = all pass; 1 = at least one failure.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUT="${SCRIPT_DIR}/send_keys_submit.sh"

PASS=0
FAIL=0
FAILED_TESTS=()

# Speed up sleeps for tests (overrides the default 0.7s delay).
export INJECT_ENTER_DELAY=0.001

# ---------------------------------------------------------------------------
# Sandbox + fake-bin construction.
# ---------------------------------------------------------------------------

setup_sandbox() {
  SBX=$(mktemp -d -t send_keys_submit_test.XXXXXX)
  FAKEBIN="$SBX/bin"
  mkdir -p "$FAKEBIN"

  # Default sandbox state.
  echo "0" > "$SBX/sendkeys_calls"     # incremented by send-keys fake
  : > "$SBX/sendkeys_args"             # one line per send-keys invocation
  echo "0" > "$SBX/sendkeys_exitcode"  # what send-keys returns (text step)
  echo "0" > "$SBX/enter_exitcode"     # what send-keys returns (Enter step)
  # Pane text sequence: list of states, one per line.
  # On the Nth capture-pane call, returns the Nth entry (last entry repeats).
  echo "" > "$SBX/pane_states"         # default: no sentinel (empty pane)
  echo "0" > "$SBX/capture_call"       # which capture-pane invocation we're on

  # Fake tmux.
  cat > "$FAKEBIN/tmux" <<'TMUXEOF'
#!/bin/bash
SBX_PATH="__SBX__"
case "${1:-}" in
  send-keys)
    n=$(cat "$SBX_PATH/sendkeys_calls")
    echo "$((n+1))" > "$SBX_PATH/sendkeys_calls"
    echo "$*" >> "$SBX_PATH/sendkeys_args"
    # Check if this is an Enter keystroke (last arg is literally "Enter").
    last_arg="${*: -1}"
    if [[ "$last_arg" == "Enter" ]]; then
      exit "$(cat "$SBX_PATH/enter_exitcode")"
    else
      exit "$(cat "$SBX_PATH/sendkeys_exitcode")"
    fi
    ;;
  capture-pane)
    idx=$(cat "$SBX_PATH/capture_call")
    echo "$((idx+1))" > "$SBX_PATH/capture_call"
    # Read line $((idx+1)) from pane_states; if beyond end, repeat last line.
    mapfile -t states < "$SBX_PATH/pane_states"
    total="${#states[@]}"
    if [[ "$total" -eq 0 ]]; then
      echo ""
    elif [[ "$idx" -lt "$total" ]]; then
      echo "${states[$idx]}"
    else
      echo "${states[$((total-1))]}"
    fi
    exit 0
    ;;
  *)
    echo "fake tmux: unhandled subcommand ${1:-}" >&2
    exit 2
    ;;
esac
TMUXEOF
  # Substitute the actual sandbox path (can't use shell expansion inside single-quoted heredoc).
  sed -i "s|__SBX__|$SBX|g" "$FAKEBIN/tmux"
  chmod +x "$FAKEBIN/tmux"

  export PATH="$FAKEBIN:$PATH"
}

teardown_sandbox() {
  rm -rf "$SBX"
}

# ---------------------------------------------------------------------------
# Assertion helpers.
# ---------------------------------------------------------------------------

run_test() {
  local name="$1"
  shift
  local expected_exit="$1"
  shift
  # Remaining args are passed to the SUT.

  setup_sandbox
  if declare -f "preflight_${name}" >/dev/null; then
    "preflight_${name}"
  fi

  local actual_exit=0
  bash "$SUT" "$@" >"$SBX/stdout" 2>"$SBX/stderr" || actual_exit=$?

  if [[ "$actual_exit" -eq "$expected_exit" ]]; then
    if declare -f "postcheck_${name}" >/dev/null; then
      if "postcheck_${name}"; then
        echo "  PASS: $name (exit=$actual_exit)"
        PASS=$((PASS+1))
      else
        echo "  FAIL: $name (exit ok=$actual_exit, postcheck failed)"
        echo "  --- stdout ---"; cat "$SBX/stdout"
        echo "  --- stderr ---"; cat "$SBX/stderr"
        FAIL=$((FAIL+1))
        FAILED_TESTS+=("$name")
      fi
    else
      echo "  PASS: $name (exit=$actual_exit)"
      PASS=$((PASS+1))
    fi
  else
    echo "  FAIL: $name (expected exit=$expected_exit, got $actual_exit)"
    echo "  --- stdout ---"; cat "$SBX/stdout"
    echo "  --- stderr ---"; cat "$SBX/stderr"
    FAIL=$((FAIL+1))
    FAILED_TESTS+=("$name")
  fi

  teardown_sandbox
}

# ---------------------------------------------------------------------------
# Test cases.
# ---------------------------------------------------------------------------

# --- 1: success on first try -----------------------------------------------
# send-keys succeeds; capture-pane returns no pending-paste sentinel.
# Expected: exit 0; Enter sent exactly once.
preflight_success_first_try() {
  printf '%s\n' "clean pane — no sentinel" > "$SBX/pane_states"
}
postcheck_success_first_try() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  # text send + 1x Enter = 2 calls total
  [[ "$n" -eq 2 ]] || { echo "    expected sendkeys_calls=2, got $n"; return 1; }
  # Enter appeared in args
  grep -q "Enter" "$SBX/sendkeys_args" \
    || { echo "    Enter not found in sendkeys_args"; return 1; }
  return 0
}

# --- 2: success on retry ----------------------------------------------------
# First capture still shows sentinel; second capture is clean.
# Expected: exit 0; Enter sent twice (one initial + one retry).
preflight_success_on_retry() {
  # Two capture-pane states: first call returns sentinel, second is clean.
  printf '%s\n' "[Pasted text — still pending" "clean pane" > "$SBX/pane_states"
}
postcheck_success_on_retry() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  # text send + Enter(initial) + Enter(retry) = 3 calls
  [[ "$n" -eq 3 ]] || { echo "    expected sendkeys_calls=3 (text+enter+retry), got $n"; return 1; }
  return 0
}

# --- 3: fail after retries --------------------------------------------------
# Sentinel persists across all captures (initial + SUBMIT_RETRIES retries).
# Expected: exit 1.
# SUBMIT_RETRIES defaults to 2 in the helper, so loop runs attempt 0,1,2 = 3 Enters.
preflight_fail_after_retries() {
  # All capture-pane calls return the pending-paste sentinel.
  printf '%s\n' "[Pasted text — stuck" "[Pasted text — stuck" \
                "[Pasted text — stuck" "[Pasted text — stuck" > "$SBX/pane_states"
}
postcheck_fail_after_retries() {
  # text send + 3x Enter (attempt 0,1,2) = 4 calls
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 4 ]] || { echo "    expected sendkeys_calls=4 (text+3xenter), got $n"; return 1; }
  # Must have emitted an error to stderr
  grep -q "unconfirmed" "$SBX/stderr" \
    || { echo "    expected 'unconfirmed' in stderr"; return 1; }
  return 0
}

# --- 4: send-keys text failure ----------------------------------------------
# The initial send-keys (text, not Enter) returns non-zero.
# Expected: exit 1 immediately (no Enter attempt).
preflight_sendkeys_text_failure() {
  echo "1" > "$SBX/sendkeys_exitcode"
  printf '%s\n' "clean pane" > "$SBX/pane_states"
}
postcheck_sendkeys_text_failure() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  # Only 1 send-keys call (the text send that failed); no Enter.
  [[ "$n" -eq 1 ]] || { echo "    expected sendkeys_calls=1 (text-fail, no Enter), got $n"; return 1; }
  grep -q "failed" "$SBX/stderr" \
    || { echo "    expected 'failed' in stderr"; return 1; }
  return 0
}

# --- 5: send-keys Enter failure ---------------------------------------------
# Text send succeeds; Enter send returns non-zero.
# Expected: exit 1.
preflight_sendkeys_enter_failure() {
  echo "1" > "$SBX/enter_exitcode"
  printf '%s\n' "clean pane" > "$SBX/pane_states"
}
postcheck_sendkeys_enter_failure() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  # text send (ok) + Enter (fail) = 2 calls
  [[ "$n" -eq 2 ]] || { echo "    expected sendkeys_calls=2 (text+failed-enter), got $n"; return 1; }
  grep -q "failed" "$SBX/stderr" \
    || { echo "    expected 'failed' in stderr"; return 1; }
  return 0
}

# --- 6: T-1106 --harness routing (default claude-code) -----------------------
# A codex-composer pane ("› <text>") holds the injected text but has NO
# "[Pasted text" sentinel. The DEFAULT (claude-code) profile sees no sentinel ->
# reports submitted -> exit 0. Same pane, --harness codex (test 7) disagrees.
preflight_harness_default_no_sentinel() {
  printf '%s\n' "› T1106_harness_routing_probe_distinctive_payload_xyz" > "$SBX/pane_states"
}

# --- 7: T-1106 --harness codex on the SAME pane ------------------------------
# The codex profile checks composer-holds-text: the "› <text>" line still holds
# the injected text -> pending -> unconfirmed after retries -> exit 1. This is
# the routing proof: identical pane, opposite verdict, selected purely by
# --harness. (It is also the bug the ticket fixes: codex must NOT false-pass.)
preflight_harness_codex_pending() {
  printf '%s\n' "› T1106_harness_routing_probe_distinctive_payload_xyz" > "$SBX/pane_states"
}
postcheck_harness_codex_pending() {
  grep -q "unconfirmed" "$SBX/stderr" \
    || { echo "    expected 'unconfirmed' in stderr (codex composer still holds text)"; return 1; }
  return 0
}

# --- 8: --harness with no value is a usage error ----------------------------
preflight_harness_missing_value() {
  printf '%s\n' "clean pane" > "$SBX/pane_states"
}
postcheck_harness_missing_value() {
  grep -q "Usage:" "$SBX/stderr" || { echo "    expected Usage: on missing --harness value"; return 1; }
  return 0
}

# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------

if [[ ! -f "$SUT" ]]; then
  echo "FATAL: SUT not found at $SUT"
  exit 1
fi

echo "Running send_keys_submit.sh tests..."
echo

run_test success_first_try 0 "main:0.0" "Hello, agent"
run_test success_on_retry  0 "main:0.0" "Hello, agent"
run_test fail_after_retries 1 "main:0.0" "Hello, agent"
run_test sendkeys_text_failure 1 "main:0.0" "Hello, agent"
run_test sendkeys_enter_failure 1 "main:0.0" "Hello, agent"
# T-1106: --harness routing — same pane, default(claude-code) submits / codex pends.
run_test harness_default_no_sentinel 0 "main:0.0" "T1106_harness_routing_probe_distinctive_payload_xyz"
run_test harness_codex_pending 1 "--harness" "codex" "main:0.0" "T1106_harness_routing_probe_distinctive_payload_xyz"
run_test harness_missing_value 1 "--harness"

echo
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  echo "Failed tests: ${FAILED_TESTS[*]}"
  exit 1
fi
exit 0
