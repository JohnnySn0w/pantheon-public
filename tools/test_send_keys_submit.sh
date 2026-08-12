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
  unset TMUX_SOCKET_NAME  # T-2589: isolate the env-var socket override per test

  # Default sandbox state.
  echo "0" > "$SBX/sendkeys_calls"     # incremented by send-keys fake
  : > "$SBX/sendkeys_args"             # one line per send-keys invocation
  echo "0" > "$SBX/sendkeys_exitcode"  # legacy name; now the PASTE (text) step exit code
  echo "0" > "$SBX/paste_calls"        # incremented by paste-buffer fake (T-2259)
  : > "$SBX/tmux_order"                # subcommand name, one line per tmux call (T-2259)
  : > "$SBX/full_argv_log"             # FULL argv (incl. any -L socket), one line per tmux call (T-2589)
  : > "$SBX/copy_mode_args"            # one line per copy-mode invocation (T-2259)
  echo "0" > "$SBX/pane_in_mode"       # #{pane_in_mode} reported by display-message (T-2268)
  echo "0" > "$SBX/enter_exitcode"     # what send-keys returns (Enter step)
  # Pane text sequence: list of states, one per line.
  # On the Nth capture-pane call, returns the Nth entry (last entry repeats).
  echo "" > "$SBX/pane_states"         # default: no sentinel (empty pane)
  echo "0" > "$SBX/capture_call"       # which capture-pane invocation we're on

  # Fake tmux.
  cat > "$FAKEBIN/tmux" <<'TMUXEOF'
#!/bin/bash
SBX_PATH="__SBX__"
echo "$*" >> "$SBX_PATH/full_argv_log"
# T-2589: a named-socket invocation prepends "-L <name>" before the real
# subcommand. Strip it here (after logging the FULL argv above) so the
# dispatch below keeps working regardless of whether a socket was requested.
if [[ "${1:-}" == "-L" ]]; then
  shift 2
fi
echo "${1:-}" >> "$SBX_PATH/tmux_order"
case "${1:-}" in
  display-message)
    # T-2268: reports #{pane_in_mode}. Sandbox file drives it so a test can
    # simulate a pane sitting in copy-mode at VERIFY time.
    cat "$SBX_PATH/pane_in_mode" 2>/dev/null || echo 0
    exit 0
    ;;
  copy-mode)
    # T-2259: the real `copy-mode -q` cancels any pane mode and is a no-op
    # otherwise. Modelled as always-succeeding; the ORDER assertion (it must
    # precede load-buffer) is what the test actually pins.
    echo "$*" >> "$SBX_PATH/copy_mode_args"
    exit 0
    ;;
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
  load-buffer)
    cat >/dev/null   # consume stdin like the real thing
    exit 0
    ;;
  paste-buffer)
    n=$(cat "$SBX_PATH/paste_calls")
    echo "$((n+1))" > "$SBX_PATH/paste_calls"
    echo "$*" >> "$SBX_PATH/sendkeys_args"
    exit "$(cat "$SBX_PATH/sendkeys_exitcode")"
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
  # T-2259: text goes via paste-buffer now; send-keys carries only Enter.
  [[ "$n" -eq 1 ]] || { echo "    expected sendkeys_calls=1 (enter only), got $n"; return 1; }
  local pc; pc=$(cat "$SBX/paste_calls")
  [[ "$pc" -eq 1 ]] || { echo "    expected paste_calls=1, got $pc"; return 1; }
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
  # T-2259: Enter(initial) + Enter(retry) = 2 send-keys calls; text was pasted.
  [[ "$n" -eq 2 ]] || { echo "    expected sendkeys_calls=2 (enter+retry), got $n"; return 1; }
  return 0
}

# --- 2b: T-2382 Claude uncollapsed first-fire paste retries ----------------
# A fresh Claude TUI can render the full pasted prompt at the composer without
# collapsing it into a "[Pasted text" marker. If the first Enter is eaten while
# the TUI is still becoming ready, the composer needle is the only evidence that
# the prompt remains pending. The helper must retry Enter without repasting.
preflight_claude_uncollapsed_first_fire_retries() {
  printf '%s\n' \
    "❯ T2382_first_fire_distinctive_payload_abcdefghijklmnopqrstuvwxyz" \
    "❯ " > "$SBX/pane_states"
}
postcheck_claude_uncollapsed_first_fire_retries() {
  local n pc
  n=$(cat "$SBX/sendkeys_calls")
  pc=$(cat "$SBX/paste_calls")
  [[ "$n" -eq 2 ]] \
    || { echo "    expected two Enter attempts, got $n"; return 1; }
  [[ "$pc" -eq 1 ]] \
    || { echo "    expected exactly one paste, got $pc"; return 1; }
  grep -q "still pending" "$SBX/stderr" \
    || { echo "    expected pending/retry diagnostic"; return 1; }
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
  # T-2259: 3x Enter (attempt 0,1,2); text was pasted.
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 3 ]] || { echo "    expected sendkeys_calls=3 (3xenter), got $n"; return 1; }
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
  # T-2259: the failing text step is now paste-buffer; NO send-keys at all.
  [[ "$n" -eq 0 ]] || { echo "    expected sendkeys_calls=0 (paste failed, no Enter), got $n"; return 1; }
  local pc; pc=$(cat "$SBX/paste_calls")
  [[ "$pc" -eq 1 ]] || { echo "    expected paste_calls=1 (the failed paste), got $pc"; return 1; }
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
  # T-2259: Enter(fail) is the only send-keys call; text was pasted.
  [[ "$n" -eq 1 ]] || { echo "    expected sendkeys_calls=1 (failed-enter), got $n"; return 1; }
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

# --- 9: T-2259 copy-mode is cancelled BEFORE the text is delivered ----------
# A pane left in tmux copy-mode swallows the submit: paste-buffer still lands
# the text in the composer, but `send-keys Enter` is consumed by copy-mode's
# key table and never reaches the TUI (found live on mnemosyne's seat, and the
# same signature as the 9-day T-0284 mystery). The helper must cancel the mode
# first. This pins BOTH halves of that fix:
#   - the FLAG: `-q` is the cancel form; a bare `copy-mode` would ENTER the
#     mode, i.e. inflict the bug on every healthy pane. An assertion that only
#     checked "copy-mode was called" would pass on that inversion.
#   - the ORDER: cancel must precede load-buffer, not follow it.
preflight_copy_mode_cancelled_first() {
  printf '%s\n' "clean pane — no sentinel" > "$SBX/pane_states"
}
postcheck_copy_mode_cancelled_first() {
  grep -qx "copy-mode" "$SBX/tmux_order" \
    || { echo "    helper never issued tmux copy-mode"; return 1; }
  grep -q -- "-q" "$SBX/copy_mode_args" \
    || { echo "    copy-mode issued without -q (bare copy-mode ENTERS the mode)"; return 1; }
  local cm lb
  cm=$(grep -nx "copy-mode"   "$SBX/tmux_order" | head -1 | cut -d: -f1)
  lb=$(grep -nx "load-buffer" "$SBX/tmux_order" | head -1 | cut -d: -f1)
  [[ -n "$cm" && -n "$lb" && "$cm" -lt "$lb" ]] \
    || { echo "    copy-mode (line ${cm:-none}) must precede load-buffer (line ${lb:-none})"; return 1; }
  return 0
}

# --- 10: T-2268 a frozen surface must never yield a "submitted" verdict ------
# The pane is in copy-mode at VERIFY time, and the pane text is CLEAN (no
# pending-paste sentinel) — which is exactly what a frozen capture of a
# previously-working pane looks like. Pre-T-2268 the predicate read that stale
# snapshot and returned "submitted" while nothing had been submitted: positive
# confirmation of a thing that did not happen, which is strictly worse than no
# verifier at all. The helper must decline to vote and ultimately fail (1).
preflight_frozen_surface_never_false_passes() {
  echo "1" > "$SBX/pane_in_mode"            # stuck in a mode for every attempt
  printf '%s\n' "clean pane — no sentinel" > "$SBX/pane_states"
}
postcheck_frozen_surface_never_false_passes() {
  grep -q "FROZEN view" "$SBX/stderr" \
    || { echo "    expected the declining-to-verify warning on stderr"; return 1; }
  # It must have tried to self-heal by cancelling the mode, not just given up.
  grep -q -- "-q" "$SBX/copy_mode_args" \
    || { echo "    expected copy-mode -q cancel attempts during verify"; return 1; }
  # And it must NOT have consulted the pane text at all while untrusted.
  local caps; caps=$(cat "$SBX/capture_call")
  [[ "$caps" -eq 0 ]] \
    || { echo "    capture-pane was read $caps time(s) despite untrusted surface"; return 1; }
  return 0
}

# --- 10b: T-2589 default path never carries -L (no socket requested) --------
preflight_default_path_no_socket_flag() {
  printf '%s\n' "clean pane — no sentinel" > "$SBX/pane_states"
}
postcheck_default_path_no_socket_flag() {
  if grep -q -- "-L" "$SBX/full_argv_log"; then
    echo "    default invocation must not include -L (no socket requested)"
    return 1
  fi
  return 0
}

# --- 10c: T-2589 --socket CLI flag threads -L into EVERY tmux invocation ----
preflight_socket_flag_threads_through() {
  printf '%s\n' "clean pane — no sentinel" > "$SBX/pane_states"
}
postcheck_socket_flag_threads_through() {
  local total with_socket
  total=$(grep -c . "$SBX/full_argv_log")
  with_socket=$(grep -c -- "^-L disposable-test-sock " "$SBX/full_argv_log")
  [[ "$total" -gt 0 ]] || { echo "    no tmux invocations recorded"; return 1; }
  [[ "$total" -eq "$with_socket" ]] \
    || { echo "    expected every tmux call ($total) to carry -L disposable-test-sock, got $with_socket"; return 1; }
  return 0
}

# --- 10d: T-2589 TMUX_SOCKET_NAME env var does the same, without the flag ---
preflight_env_socket_threads_through() {
  export TMUX_SOCKET_NAME="env-test-sock"
  printf '%s\n' "clean pane — no sentinel" > "$SBX/pane_states"
}
postcheck_env_socket_threads_through() {
  unset TMUX_SOCKET_NAME
  local total with_socket
  total=$(grep -c . "$SBX/full_argv_log")
  with_socket=$(grep -c -- "^-L env-test-sock " "$SBX/full_argv_log")
  [[ "$total" -gt 0 ]] || { echo "    no tmux invocations recorded"; return 1; }
  [[ "$total" -eq "$with_socket" ]] \
    || { echo "    expected every tmux call ($total) to carry -L env-test-sock, got $with_socket"; return 1; }
  return 0
}

# --- 11: T-2268 an UNREADABLE mode query is treated as untrusted -------------
# `|| echo 1` in the helper: if display-message cannot be read we must assume
# the surface is untrustworthy rather than assume it is fine. Fail-safe is the
# only direction consistent with "NEVER false-pass".
preflight_unreadable_mode_is_untrusted() {
  rm -f "$SBX/pane_in_mode"                 # display-message falls through
  echo "unreadable" > "$SBX/pane_in_mode"   # non-"0" => untrusted
  printf '%s\n' "clean pane — no sentinel" > "$SBX/pane_states"
}
postcheck_unreadable_mode_is_untrusted() {
  grep -q "FROZEN view" "$SBX/stderr" \
    || { echo "    a non-numeric/unreadable mode must be treated as untrusted"; return 1; }
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
run_test claude_uncollapsed_first_fire_retries 0 "main:0.0" \
  "T2382_first_fire_distinctive_payload_abcdefghijklmnopqrstuvwxyz"
run_test fail_after_retries 1 "main:0.0" "Hello, agent"
run_test sendkeys_text_failure 1 "main:0.0" "Hello, agent"
run_test sendkeys_enter_failure 1 "main:0.0" "Hello, agent"
# T-1106: --harness routing — same pane, default(claude-code) submits / codex pends.
run_test harness_default_no_sentinel 0 "main:0.0" "T1106_harness_routing_probe_distinctive_payload_xyz"
run_test harness_codex_pending 1 "--harness" "codex" "main:0.0" "T1106_harness_routing_probe_distinctive_payload_xyz"
run_test harness_missing_value 1 "--harness"
# T-2259: a pane in copy-mode eats the submit — cancel the mode, in that order.
run_test copy_mode_cancelled_first 0 "main:0.0" "Hello, agent"
# T-2268: the verifier must not report success while reading a frozen surface.
run_test frozen_surface_never_false_passes 1 "main:0.0" "Hello, agent"
run_test unreadable_mode_is_untrusted 1 "main:0.0" "Hello, agent"
# T-2589: optional named-socket targeting (off-prod disposable scope testing).
run_test default_path_no_socket_flag 0 "main:0.0" "Hello, agent"
run_test socket_flag_threads_through 0 "--socket" "disposable-test-sock" "main:0.0" "Hello, agent"
run_test env_socket_threads_through 0 "main:0.0" "Hello, agent"

echo
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  echo "Failed tests: ${FAILED_TESTS[*]}"
  exit 1
fi
exit 0
