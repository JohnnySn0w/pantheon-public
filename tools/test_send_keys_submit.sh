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
export EMPTY_RECAPTURE_S=0.001
export LATE_CONFIRM_POLL_S=0.001  # T-5680: the failure-path late-confirm poll
# T-4362: the render gate's poll interval. The gate itself stays off (RENDER_WAIT_S
# unset = 0) unless a test passes --render-wait.
export RENDER_POLL_S=0.001
unset RENDER_WAIT_S

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
# T-4832: the pre-Enter dialog re-check captures with -J. It is a PEEK: logged under
# its own name and it does not advance pane_states, so the sequences that model the
# post-Enter verify captures keep their meaning.
if [[ "${1:-}" == "capture-pane" && " $* " == *" -J "* ]]; then
  echo "capture-pane-peek" >> "$SBX_PATH/tmux_order"
  if [[ -f "$SBX_PATH/pre_enter_state" && "$(cat "$SBX_PATH/sendkeys_calls")" == "0" ]]; then
    cat "$SBX_PATH/pre_enter_state"
  else
    mapfile -t states < "$SBX_PATH/pane_states"
    idx=$(cat "$SBX_PATH/capture_call"); total="${#states[@]}"
    (( total > 0 )) && echo "${states[$(( idx < total ? idx : total - 1 ))]}"
  fi
  [[ -f "$SBX_PATH/pane_footer" ]] && cat "$SBX_PATH/pane_footer"
  exit 0
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
    # T-4371 r1: an optional per-capture cost (a loaded host), and an optional state
    # shown until the first Enter (a paste that renders only then), which leaves
    # pane_states' sequence for the captures after it. Both absent by default.
    [[ -f "$SBX_PATH/capture_delay" ]] && sleep "$(cat "$SBX_PATH/capture_delay")"
    # T-4646: a real composer always draws a footer row under it; tests that need one
    # (the Enter-anyway check reads it) put it in pane_footer. Absent by default.
    footer() { [[ -f "$SBX_PATH/pane_footer" ]] && cat "$SBX_PATH/pane_footer"; return 0; }
    # T-4646: a capture before the paste (the gated pre-paste check) shows the
    # pre-paste screen, an empty composer unless pre_paste_state says otherwise, and
    # leaves pane_states' sequence for the captures after the paste.
    if [[ "$(cat "$SBX_PATH/paste_calls")" == "0" ]]; then
      if [[ -f "$SBX_PATH/pre_paste_state" ]]; then cat "$SBX_PATH/pre_paste_state"; else echo "❯ "; fi
      footer
      exit 0
    fi
    if [[ -f "$SBX_PATH/pre_enter_state" && "$(cat "$SBX_PATH/sendkeys_calls")" == "0" ]]; then
      cat "$SBX_PATH/pre_enter_state"
      footer
      exit 0
    fi
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
    footer
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
  # T-5380 r2: post-Enter-1 capture, the pre-retry re-check (still pending), then the
  # post-Enter-2 capture (clean).
  printf '%s\n' "[Pasted text — still pending" "[Pasted text — still pending" "clean pane" > "$SBX/pane_states"
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
# Expected: exit 2 (verified still pending; T-4117).
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
# the injected text -> pending -> unconfirmed after retries -> exit 2 (T-4117). This is
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
# key table and never reaches the TUI (found live on archivist's seat, and the
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

# --- T-4117: a broken predicate is never read as "queued" ---------------------
# rc 2 means the verifier itself said "pending" (a busy pane queued the text), and
# restart callers act on it by waiting for the ack. A verifier that errors or prints
# garbage must stay rc 1, or a broken check would masquerade as a busy pane.
preflight_broken_predicate_is_not_queued() {
  printf '%s\n' "[Pasted text — stuck" "[Pasted text — stuck" \
                "[Pasted text — stuck" "[Pasted text — stuck" > "$SBX/pane_states"
  printf 'print("garbage")\n' > "$SBX/garbage_verify.py"
  export SUBMIT_VERIFY_PY="$SBX/garbage_verify.py"
}

echo "Running send_keys_submit.sh tests..."
echo

# --- T-4362: render gate --------------------------------------------------------
# Recovery's T-3026 hand-move (reproduced 2026-09-30): a freshly booted TUI had not yet
# rendered the paste when the fixed-delay Enter went in, so the Enter landed inside
# the paste as a literal CR, and the verify capture (still the empty composer) read
# "submitted": a false rc 0 with the probe stuck in the composer. The pane states
# below are that sequence: two captures before the paste renders, the text, the
# text again (the first Enter still in flight), then the empty composer.
_t4362_states() {
  printf '%s\n' "❯ " "❯ " "❯ $T4362_TEXT" "❯ $T4362_TEXT" "❯ $T4362_TEXT" "❯ " > "$SBX/pane_states"  # T-5380 r2: + pre-retry read
}
_first_line_of() {  # _first_line_of <subcommand>: 1-based line in tmux_order, 0 if absent
  local n; n=$(grep -n -m1 -x "$1" "$SBX/tmux_order" | cut -d: -f1)
  echo "${n:-0}"
}
# Captures between the paste and the first Enter: the render gate's own cost. T-4646's
# pre-paste check adds one capture before the paste, counted separately.
_gate_captures() {
  sed -n "$(_first_line_of paste-buffer),$(( $(_first_line_of send-keys) - 1 ))p" "$SBX/tmux_order" \
    | grep -c -x capture-pane
}
preflight_render_gate_enters_only_after_the_paste_shows() { _t4362_states; }
postcheck_render_gate_enters_only_after_the_paste_shows() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  # Enter 1 after the text showed, still pending -> retry Enter 2 -> submitted.
  [[ "$n" -eq 2 ]] || { echo "    expected 2 Enters, got $n"; return 1; }
  local captures_before_enter
  captures_before_enter=$(_gate_captures)
  [[ "$captures_before_enter" -eq 3 ]] \
    || { echo "    expected the first Enter after 3 captures, got $captures_before_enter"; return 1; }
  ! grep -q "WARNING" "$SBX/stderr" || { echo "    unexpected WARNING"; return 1; }
}
# T-4646: the Enter-anyway is for a composer, which always draws a footer row.
_composer_footer() { echo "  ⏸ manual mode on (shift+tab to cycle)" > "$SBX/pane_footer"; }
preflight_render_gate_timeout_presses_enter_anyway() {
  printf '%s\n' "❯ " > "$SBX/pane_states"
  _composer_footer
}
# T-4646: the same timeout on a pane with no composer footer (an option list, a boot
# screen) must not press Enter: on 2026-10-05 that Enter picked an auto-mode "Yes".
preflight_render_gate_timeout_without_composer_refuses_enter() {
  printf '%s\n' " ❯ 1. Continue" > "$SBX/pane_states"
}
postcheck_render_gate_timeout_without_composer_refuses_enter() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 0 ]] || { echo "    expected no Enter, got $n"; return 1; }
  grep -q "NOT pressing Enter \[T-4646\]" "$SBX/stderr" || { echo "    expected the refusal on stderr"; return 1; }
}
# T-4646: a gated call that finds a known dialog BEFORE the paste sends nothing at all.
preflight_render_gate_refuses_paste_into_a_dialog() {
  printf '%s\n' "────────────────────────────────────────" \
    " Make auto mode your default permission mode?" \
    " ❯ Yes, set auto mode as my default permission mode" \
    "   No, keep manual" > "$SBX/pre_paste_state"
}
postcheck_render_gate_refuses_paste_into_a_dialog() {
  [[ "$(cat "$SBX/paste_calls")" -eq 0 ]] || { echo "    pasted into the dialog"; return 1; }
  [[ "$(cat "$SBX/sendkeys_calls")" -eq 0 ]] || { echo "    pressed a key in the dialog"; return 1; }
  grep -q "refusing to paste or press Enter \[T-4646\]" "$SBX/stderr" || { echo "    expected the refusal on stderr"; return 1; }
}
# The ungated path never reads the pane before the paste: same dialog, old behaviour.
preflight_render_gate_off_skips_the_pre_paste_check() {
  preflight_render_gate_refuses_paste_into_a_dialog
  printf '%s\n' "❯ " > "$SBX/pane_states"
}
postcheck_render_gate_off_skips_the_pre_paste_check() {
  [[ "$(_first_line_of capture-pane)" -gt "$(_first_line_of paste-buffer)" ]] \
    || { echo "    the ungated path captured before the paste"; return 1; }
}
postcheck_render_gate_timeout_presses_enter_anyway() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 1 ]] || { echo "    expected 1 Enter, got $n"; return 1; }
  grep -Eq "not visible in the composer after [0-9]+\.[0-9]s \(render wait 0\.01s, [0-9]+ polls\)" "$SBX/stderr" \
    || { echo "    expected the timeout WARNING (elapsed, limit, polls) on stderr"; return 1; }
}
preflight_render_gate_off_by_default() { _t4362_states; }
postcheck_render_gate_off_by_default() {
  # Today's behaviour, unchanged for every caller that does not opt in: the Enter
  # comes before any capture (and on this pane sequence the verify false-passes,
  # which is exactly why the controller's fresh-runtime submits opt in).
  local enter capture
  enter=$(_first_line_of send-keys); capture=$(_first_line_of capture-pane)
  [[ "$enter" -gt 0 && "$enter" -lt "$capture" ]] \
    || { echo "    expected Enter (line $enter) before the first capture (line $capture)"; return 1; }
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 1 ]] || { echo "    expected 1 Enter, got $n"; return 1; }
}

# --- T-4371: orchestrator's edge cases for the render gate ------------------------------
# A long or multi-line paste renders as Claude Code's "[Pasted text #N +M lines]"
# placeholder, not the literal text. The gate's "rendered" is submit_verify's
# pending-check, which reads the placeholder as pending (composer_box scope), so
# it must end the wait on the placeholder too, not fall through to the timeout.
preflight_render_gate_accepts_the_collapsed_paste_placeholder() {
  printf '%s\n' "❯ " "❯ [Pasted text #1 +4 lines]" "❯ " > "$SBX/pane_states"
}
postcheck_render_gate_accepts_the_collapsed_paste_placeholder() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 1 ]] || { echo "    expected 1 Enter, got $n"; return 1; }
  ! grep -q "WARNING" "$SBX/stderr" || { echo "    the placeholder fell through to the timeout"; return 1; }
}
# The gate never presses Enter itself. After a timeout, the Enters are exactly
# what the ungated helper sends for the same post-Enter panes: here the text
# renders just after the timeout (the verify sees it pending, one retry, then
# submitted), so both variants send 2.
# T-4371 r1: the text renders only after the first Enter, however many polls the
# gate took (it used to hard-wire 10 = ceil(0.01 / 0.001) polls).
_t4371_after_timeout_states() {
  echo "❯ " > "$SBX/pre_enter_state"
  printf '%s\n' "❯ $T4362_TEXT" "❯ $T4362_TEXT" "❯ " > "$SBX/pane_states"
  _composer_footer
}
preflight_render_gate_timeout_adds_no_enter_of_its_own() { _t4371_after_timeout_states; }
postcheck_render_gate_timeout_adds_no_enter_of_its_own() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 2 ]] || { echo "    expected 2 Enters (1 + the loop's own retry), got $n"; return 1; }
  grep -q "not visible in the composer" "$SBX/stderr" || { echo "    expected the timeout WARNING"; return 1; }
}
preflight_render_gate_off_same_post_enter_sequence() {
  printf '%s\n' "❯ $T4362_TEXT" "❯ $T4362_TEXT" "❯ " > "$SBX/pane_states"
}
postcheck_render_gate_off_same_post_enter_sequence() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 2 ]] || { echo "    expected 2 Enters, got $n"; return 1; }
}
# T-4117: a busy pane queues the text; the verify keeps seeing it pending and the
# helper exits 2 after its retries. The gate ends on the first poll (pending) and
# changes nothing downstream.
preflight_render_gate_busy_pane_still_exits_2() {
  printf '%s\n' "❯ $T4362_TEXT" > "$SBX/pane_states"
}
postcheck_render_gate_busy_pane_still_exits_2() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 3 ]] || { echo "    expected 1 + SUBMIT_RETRIES(2) = 3 Enters, got $n"; return 1; }
}
# Steady state: the text is already in the composer at the first poll. The gate
# costs exactly one capture + one pending-check before the usual settle and Enter,
# and returns at once despite the 8s cap.
preflight_render_gate_steady_state_costs_one_capture() {
  printf '%s\n' "❯ $T4362_TEXT" "❯ " > "$SBX/pane_states"
}
postcheck_render_gate_steady_state_costs_one_capture() {
  local before
  before=$(_gate_captures)
  [[ "$before" -eq 1 ]] || { echo "    expected 1 capture before the Enter, got $before"; return 1; }
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 1 ]] || { echo "    expected 1 Enter, got $n"; return 1; }
  # "Returns at once despite the 8s cap", pinned without a clock (T-4371 r1: a 4s
  # wall-clock check took 5.9s at load 15): a gate that kept waiting would poll
  # again (a 2nd capture before the Enter) or time out with the WARNING.
  ! grep -q "WARNING" "$SBX/stderr" || { echo "    the gate timed out instead of returning at once"; return 1; }
}

# --- T-4371 r1 (rev-t4371): the bound is wall-clock, and off means off ------------
# R1: each poll costs a capture and a python start, which a poll COUNT never counted
# (--render-wait 2 took 16.6s at load 15). Here every capture costs 0.4s and the paste
# never renders: a count of ceil(0.3 / 0.001) = 300 polls would take 120s+. The
# wall-clock gate stops at the first poll after 0.3s, so after one 0.4s capture, and
# the WARNING says how long it really took (>= 0.4s), not the 0.3s limit.
preflight_render_gate_bound_is_wall_clock_not_polls() {
  echo "0.4" > "$SBX/capture_delay"
  echo "❯ " > "$SBX/pre_enter_state"
  printf '%s\n' "❯ " > "$SBX/pane_states"
  _composer_footer
  date +%s%N > "$SBX/t0"
}
postcheck_render_gate_bound_is_wall_clock_not_polls() {
  local ms=$(( ( $(date +%s%N) - $(cat "$SBX/t0") ) / 1000000 ))
  [[ "$ms" -lt 15000 ]] || { echo "    took ${ms}ms: the render wait is not bounded by wall-clock"; return 1; }
  local reported
  reported=$(grep -Eo "after [0-9]+\.[0-9]s \(render wait 0\.3s" "$SBX/stderr" | grep -Eo "[0-9]+\.[0-9]" | head -1)
  [[ -n "$reported" ]] || { echo "    expected the WARNING to report the elapsed time"; return 1; }
  (( 10#${reported/./} >= 4 )) || { echo "    WARNING reported ${reported}s, not the >= 0.4s it took"; return 1; }
}
# R2: a caller that does not opt in runs no extra command. awk is shimmed to log.
_t4371_awk_shim() {
  local real; real=$(PATH="${PATH#"$FAKEBIN":}" command -v awk)
  printf '#!/bin/bash\necho awk >> "%s/awk_calls"\nexec %s "$@"\n' "$SBX" "$real" > "$FAKEBIN/awk"
  chmod +x "$FAKEBIN/awk"
  : > "$SBX/awk_calls"
}
preflight_render_gate_off_forks_no_awk() { _t4371_awk_shim; printf '%s\n' "❯ $T4362_TEXT" "❯ " > "$SBX/pane_states"; }
postcheck_render_gate_off_forks_no_awk() {
  [[ ! -s "$SBX/awk_calls" ]] || { echo "    ungated call forked awk $(wc -l < "$SBX/awk_calls")x"; return 1; }
}
preflight_render_gate_on_forks_no_awk() { _t4371_awk_shim; printf '%s\n' "❯ $T4362_TEXT" "❯ " > "$SBX/pane_states"; }
postcheck_render_gate_on_forks_no_awk() {
  [[ ! -s "$SBX/awk_calls" ]] || { echo "    gated call forked awk $(wc -l < "$SBX/awk_calls")x"; return 1; }
}
# "0.0" and "0" are both off: the Enter precedes any capture, as on main.
preflight_render_wait_zero_forms_are_off() { _t4362_states; }
postcheck_render_wait_zero_forms_are_off() { postcheck_render_gate_off_by_default; }

# --- T-4390: rev-t4371 r2 NIT-1 / NIT-2 ------------------------------------------
# An env value the CLI would refuse turns the gate off, as before, but now says so.
# The preflights export; the run_test lines below restore the harness defaults.
preflight_render_wait_env_invalid_warns() { _t4362_states; export RENDER_WAIT_S=soon; }
postcheck_render_wait_env_invalid_warns() {
  grep -q "RENDER_WAIT_S='soon' is not plain decimal seconds" "$SBX/stderr" \
    || { echo "    expected the RENDER_WAIT_S WARNING on stderr"; return 1; }
  postcheck_render_gate_off_by_default
}
preflight_render_poll_env_zero_warns() { _t4362_states; export RENDER_POLL_S=0; }
postcheck_render_poll_env_zero_warns() {
  grep -q "RENDER_POLL_S='0' is not a positive plain decimal" "$SBX/stderr" \
    || { echo "    expected the RENDER_POLL_S WARNING on stderr"; return 1; }
  postcheck_render_gate_off_by_default
}
# The default (gate off, RENDER_POLL_S unused) must stay silent.
# T-5412: needs a non-empty pane (the default empty capture is now "unverified", exit 2).
preflight_render_gate_off_is_silent() { printf '%s\n' "clean pane" > "$SBX/pane_states"; }
postcheck_render_gate_off_is_silent() {
  ! grep -q "WARNING" "$SBX/stderr" || { echo "    unexpected WARNING on the ungated path"; return 1; }
}

# --- T-4369: the render gate on a CODEX composer ---------------------------------
# The controller gates codex fresh-runtime submits too (T-4362), but nothing pinned
# that the gate can SEE a codex paste: the '›' composer holding the needle (short
# paste), or the "[Pasted Content" sentinel (collapsed paste). If it couldn't, every
# gated codex submit would wait out the full render wait and then warn.
preflight_render_gate_codex_waits_for_the_paste() {
  printf '%s\n' "› " "› " "› $T4362_TEXT" "› $T4362_TEXT" "› $T4362_TEXT" "› " > "$SBX/pane_states"
}
postcheck_render_gate_codex_waits_for_the_paste() {
  postcheck_render_gate_enters_only_after_the_paste_shows
}
preflight_render_gate_codex_accepts_the_collapsed_paste() {
  printf '%s\n' "› " "› [Pasted Content 1184 chars]" "› [Pasted Content 1184 chars]" "› [Pasted Content 1184 chars]" "› " \
    > "$SBX/pane_states"
}
postcheck_render_gate_codex_accepts_the_collapsed_paste() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 2 ]] || { echo "    expected 2 Enters, got $n"; return 1; }
  local captures_before_enter
  captures_before_enter=$(_gate_captures)
  [[ "$captures_before_enter" -eq 2 ]] \
    || { echo "    expected the first Enter after 2 captures, got $captures_before_enter"; return 1; }
  ! grep -q "WARNING" "$SBX/stderr" || { echo "    unexpected WARNING"; return 1; }
}

# --- T-5680: bounded late-confirm poll on the failure path ----------------------
# specialist keep-going 10-27 15:08Z: every check in the retry window read pending, the
# Enter landed after it, and the unit false-failed. Captures 1-5 are the 3 post-Enter
# reads and the 2 pre-retry reads; then the late poll looks again, with NO Enter.
preflight_late_confirm_after_last_retry() {
  printf '%s\n' "[Pasted text — still pending" "[Pasted text — still pending" \
    "[Pasted text — still pending" "[Pasted text — still pending" "[Pasted text — still pending" \
    "[Pasted text — still pending" "[Pasted text — still pending" "clean pane" > "$SBX/pane_states"
}
postcheck_late_confirm_after_last_retry() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 3 ]] || { echo "    expected exactly 3 Enters (no Enter from the late poll), got $n"; return 1; }
  grep -q "confirmed late" "$SBX/stderr" || { echo "    expected the 'confirmed late' note"; return 1; }
}
# All pending: the late poll is bounded (LATE_CONFIRM_POLLS looks), sends no Enter, rc 2.
preflight_late_confirm_poll_is_bounded() {
  printf '%s\n' "[Pasted text — still pending" > "$SBX/pane_states"
}
postcheck_late_confirm_poll_is_bounded() {
  local n caps; n=$(cat "$SBX/sendkeys_calls"); caps=$(cat "$SBX/capture_call")
  [[ "$n" -eq 3 ]] || { echo "    expected 3 Enters, got $n"; return 1; }
  [[ "$caps" -eq $((5 + 6)) ]] || { echo "    expected 5 retry-window + 6 late captures, got $caps"; return 1; }
}

# --- T-5380 r2: re-verify before every RETRY Enter ----------------------------
# Enter 1's capture reads pending; the pre-retry capture reads submitted (Enter 1
# landed late, inside the settle window). No second Enter may be pressed.
preflight_late_enter_gets_no_retry_enter() {
  printf '%s\n' "[Pasted text — still pending" "clean pane" > "$SBX/pane_states"
}
postcheck_late_enter_gets_no_retry_enter() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 1 ]] || { echo "    expected exactly 1 Enter, got $n"; return 1; }
  return 0
}
# The pre-retry capture still reads pending: the retry Enter fires (that is
# success_on_retry), and with every capture pending all 3 Enters fire, exit 2.
preflight_pre_retry_pending_still_retries() {
  printf '%s\n' "[Pasted text — still pending" > "$SBX/pane_states"
}
postcheck_pre_retry_pending_still_retries() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 3 ]] || { echo "    expected 3 Enters, got $n"; return 1; }
  return 0
}
# An empty pre-retry capture (a failed capture-pane) is no vote: the retry Enter fires.
preflight_empty_pre_retry_capture_is_no_vote() {
  printf '%s\n' "[Pasted text — still pending" "" "clean pane" > "$SBX/pane_states"
}
postcheck_empty_pre_retry_capture_is_no_vote() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 2 ]] || { echo "    expected 2 Enters, got $n"; return 1; }
  return 0
}

# --- T-5412: an empty POST-Enter capture is unknown, never "submitted" --------------
# Every capture empty: all 3 Enters fire, exit 2 (unproven), never 0.
preflight_empty_post_enter_capture_is_unverified() { printf '%s\n' "" > "$SBX/pane_states"; }
postcheck_empty_post_enter_capture_is_unverified() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 3 ]] || { echo "    expected 3 Enters, got $n"; return 1; }
  grep -q "unverified" "$SBX/stderr" || { echo "    expected an 'unverified' warning on stderr"; return 1; }
  return 0
}
preflight_whitespace_post_enter_capture_is_unverified() { printf '%s\n' "   	  " > "$SBX/pane_states"; }
postcheck_whitespace_post_enter_capture_is_unverified() { postcheck_empty_post_enter_capture_is_unverified; }
# One blank read after Enter 1 is re-read; the re-read sees the submit: exit 0, 1 Enter.
preflight_one_blank_read_is_re_read() { printf '%s\n' "" "clean pane" > "$SBX/pane_states"; }
postcheck_one_blank_read_is_re_read() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 1 ]] || { echo "    expected exactly 1 Enter, got $n"; return 1; }
  ! grep -q "unverified" "$SBX/stderr" || { echo "    unexpected 'unverified' on a single blank read"; return 1; }
  return 0
}
# Two blank reads after Enter 1, then a real submitted capture at the pre-retry check:
# no more Enters.
preflight_empty_post_enter_then_clean_pre_retry_submits() { printf '%s\n' "" "" "clean pane" > "$SBX/pane_states"; }
postcheck_empty_post_enter_then_clean_pre_retry_submits() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 1 ]] || { echo "    expected exactly 1 Enter, got $n"; return 1; }
  return 0
}
# "" then pending: the final verdict is the plain pending one (exit 2 as before).
preflight_empty_then_pending_is_exit_2() { printf '%s\n' "" "[Pasted text — stuck" > "$SBX/pane_states"; }

run_test success_first_try 0 "main:0.0" "Hello, agent"
run_test success_on_retry  0 "main:0.0" "Hello, agent"
run_test late_enter_gets_no_retry_enter 0 "main:0.0" "Hello, agent"        # T-5380 r2
# T-5680: the shell poll equals submit_verify.py's (1.5s x floor(10/1.5)=6 looks).
if python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import submit_verify as sv
sys.exit(0 if (sv.LATE_CONFIRM_POLL_S, int(sv.LATE_CONFIRM_TOTAL_S // sv.LATE_CONFIRM_POLL_S)) == (1.5, 6) else 1)
" && grep -q '^LATE_CONFIRM_POLL_S="\${LATE_CONFIRM_POLL_S:-1.5}"' "$SUT" && grep -q '^LATE_CONFIRM_POLLS="\${LATE_CONFIRM_POLLS:-6}"' "$SUT"; then
  echo "  PASS: late_confirm_constants_match_submit_verify"; PASS=$((PASS+1))
else
  echo "  FAIL: late_confirm_constants_match_submit_verify"; FAIL=$((FAIL+1)); FAILED_TESTS+=("late_confirm_constants_match_submit_verify")
fi
run_test late_confirm_after_last_retry 0 "main:0.0" "Hello, agent"       # T-5680
run_test late_confirm_poll_is_bounded 2 "main:0.0" "Hello, agent"        # T-5680
run_test pre_retry_pending_still_retries 2 "main:0.0" "Hello, agent"  # T-5380 r2
run_test empty_pre_retry_capture_is_no_vote 0 "main:0.0" "Hello, agent"  # T-5380 r3
run_test empty_post_enter_capture_is_unverified 2 "main:0.0" "Hello, agent"  # T-5412
run_test whitespace_post_enter_capture_is_unverified 2 "main:0.0" "Hello, agent"  # T-5412
run_test one_blank_read_is_re_read 0 "main:0.0" "Hello, agent"  # T-5412
run_test empty_post_enter_then_clean_pre_retry_submits 0 "main:0.0" "Hello, agent"  # T-5412
run_test empty_then_pending_is_exit_2 2 "main:0.0" "Hello, agent"  # T-5412
run_test claude_uncollapsed_first_fire_retries 0 "main:0.0" \
  "T2382_first_fire_distinctive_payload_abcdefghijklmnopqrstuvwxyz"
run_test fail_after_retries 2 "main:0.0" "Hello, agent"
run_test sendkeys_text_failure 1 "main:0.0" "Hello, agent"
run_test sendkeys_enter_failure 1 "main:0.0" "Hello, agent"
# T-1106: --harness routing — same pane, default(claude-code) submits / codex pends.
run_test harness_default_no_sentinel 0 "main:0.0" "T1106_harness_routing_probe_distinctive_payload_xyz"
run_test harness_codex_pending 2 "--harness" "codex" "main:0.0" "T1106_harness_routing_probe_distinctive_payload_xyz"
run_test harness_missing_value 1 "--harness"
# T-2259: a pane in copy-mode eats the submit — cancel the mode, in that order.
run_test copy_mode_cancelled_first 0 "main:0.0" "Hello, agent"
# T-2268: the verifier must not report success while reading a frozen surface.
run_test frozen_surface_never_false_passes 1 "main:0.0" "Hello, agent"
run_test unreadable_mode_is_untrusted 1 "main:0.0" "Hello, agent"
run_test broken_predicate_is_not_queued 1 "main:0.0" "Hello, agent"
unset SUBMIT_VERIFY_PY
# T-2589: optional named-socket targeting (off-prod disposable scope testing).
run_test default_path_no_socket_flag 0 "main:0.0" "Hello, agent"
# T-4362: the opt-in render gate.
T4362_TEXT="T4362_fresh_tui_probe_distinctive_payload_abcdefghijklmnop"
run_test render_gate_enters_only_after_the_paste_shows 0 "--render-wait" "30" "main:0.0" "$T4362_TEXT"
run_test render_gate_timeout_presses_enter_anyway 0 "--render-wait" "0.01" "main:0.0" "$T4362_TEXT"
run_test render_gate_timeout_without_composer_refuses_enter 3 "--render-wait" "0.01" "main:0.0" "$T4362_TEXT"  # T-4646
run_test render_gate_refuses_paste_into_a_dialog 3 "--render-wait" "30" "main:0.0" "$T4362_TEXT"            # T-4646
run_test render_gate_off_skips_the_pre_paste_check 0 "main:0.0" "$T4362_TEXT"                              # T-4646
run_test render_gate_off_by_default 0 "main:0.0" "$T4362_TEXT"
run_test render_wait_rejects_a_non_number 1 "--render-wait" "soon" "main:0.0" "$T4362_TEXT"
# T-4371 (orchestrator's edge cases).
T4371_MULTILINE="$(printf 'line one of a long wake prompt\nline two\nline three\nline four\nline five')"
run_test render_gate_accepts_the_collapsed_paste_placeholder 0 "--render-wait" "30" "main:0.0" "$T4371_MULTILINE"
run_test render_gate_timeout_adds_no_enter_of_its_own 0 "--render-wait" "0.01" "main:0.0" "$T4362_TEXT"
run_test render_gate_off_same_post_enter_sequence 0 "main:0.0" "$T4362_TEXT"
run_test render_gate_busy_pane_still_exits_2 2 "--render-wait" "30" "main:0.0" "$T4362_TEXT"
run_test render_gate_steady_state_costs_one_capture 0 "--render-wait" "8" "main:0.0" "$T4362_TEXT"
# T-4371 r1 (rev-t4371 R1, R2).
run_test render_gate_bound_is_wall_clock_not_polls 0 "--render-wait" "0.3" "main:0.0" "$T4362_TEXT"
run_test render_gate_off_forks_no_awk 0 "main:0.0" "$T4362_TEXT"
run_test render_gate_on_forks_no_awk 0 "--render-wait" "30" "main:0.0" "$T4362_TEXT"
run_test render_wait_zero_forms_are_off 0 "--render-wait" "0.0" "main:0.0" "$T4362_TEXT"
# T-4390 (rev-t4371 r2 NIT-1, NIT-2).
run_test render_wait_rejects_an_overflowing_value 1 "--render-wait" "9999999999999999" "main:0.0" "$T4362_TEXT"
run_test render_wait_env_invalid_warns 0 "main:0.0" "$T4362_TEXT"
unset RENDER_WAIT_S
run_test render_poll_env_zero_warns 0 "--render-wait" "30" "main:0.0" "$T4362_TEXT"
export RENDER_POLL_S=0.001
RENDER_POLL_S=bogus run_test render_gate_off_is_silent 0 "main:0.0" "$T4362_TEXT"
export RENDER_POLL_S=0.001
# T-4369: the gate on a codex composer.
run_test render_gate_codex_waits_for_the_paste 0 "--harness" "codex" "--render-wait" "30" "main:0.0" "$T4362_TEXT"
run_test render_gate_codex_accepts_the_collapsed_paste 0 "--harness" "codex" "--render-wait" "30" "main:0.0" "$T4371_MULTILINE"
# T-4832: a dialog that opens AFTER the paste (the T-4828 step-3 guard's TOCTOU
# window) must not get the Enter: its cursor sits on "Yes". Render gate off, so no
# pre-paste check runs: only the new pre-Enter re-check stands between.
preflight_dialog_after_paste_refuses_enter() {
  printf '%s\n' " Do you want to proceed?" " ❯ 1. Yes" "   2. No" "" " Esc to cancel · Tab to amend" \
    > "$SBX/pre_enter_state"
  printf '%s\n' "❯ " > "$SBX/pane_states"
}
postcheck_dialog_after_paste_refuses_enter() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 0 ]] || { echo "    expected NO Enter into the dialog, got $n"; return 1; }
  grep -q "refusing to inject" "$SBX/stderr" || { echo "    expected the T-4833 journal marker"; return 1; }
}
preflight_plan_dialog_after_paste_refuses_enter() {
  printf '%s\n' " Would you like to proceed?" " ❯ 1. Yes, and auto-accept edits" \
    "   2. Yes, manually approve edits" "   3. No, keep planning" "   4. Tell Claude what to change" \
    > "$SBX/pre_enter_state"
  printf '%s\n' "❯ " > "$SBX/pane_states"
}
postcheck_plan_dialog_after_paste_refuses_enter() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 0 ]] || { echo "    expected NO Enter into the plan dialog, got $n"; return 1; }
}
preflight_plan_words_above_the_bottom_do_not_block() {
  { echo "● The dialog offers: No, keep planning"; for i in $(seq 1 14); do echo "  transcript line $i"; done
    echo "❯ Hello, agent"; } > "$SBX/pre_enter_state"
  printf '%s\n' "❯ " > "$SBX/pane_states"
}
postcheck_plan_words_above_the_bottom_do_not_block() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 1 ]] || { echo "    prose above the bottom region must not block: expected 1 Enter, got $n"; return 1; }
}
run_test dialog_after_paste_refuses_enter 3 "main:0.0" "Hello, agent"
run_test plan_dialog_after_paste_refuses_enter 3 "main:0.0" "Hello, agent"
run_test plan_words_above_the_bottom_do_not_block 0 "main:0.0" "Hello, agent"
# T-4832 r1: the boot-dialog dismiss MUST Enter into its dialog. The one-call bypass
# lets exactly that through; the same dialog without it is refused (test above).
preflight_bypass_enters_into_the_dialog() { preflight_dialog_after_paste_refuses_enter; }
postcheck_bypass_enters_into_the_dialog() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -ge 1 ]] || { echo "    the bypass must press Enter into the dialog, got $n"; return 1; }
  ! grep -q "refusing to inject" "$SBX/stderr" || { echo "    refused despite the bypass"; return 1; }
}
run_test bypass_enters_into_the_dialog 0 "--allow-dialog-enter" "main:0.0" "1"   # T-4849: explicit flag
# T-4849: an EXPORTED value is never the bypass, on the CLI or when sourced; only the
# call-scoped prefix on the function (what the boot-dismiss callers write) is.
preflight_exported_bypass_ignored_on_cli() { preflight_dialog_after_paste_refuses_enter; }
postcheck_exported_bypass_ignored_on_cli() { postcheck_dialog_after_paste_refuses_enter; }
SEND_KEYS_ALLOW_DIALOG_ENTER=1 run_test exported_bypass_ignored_on_cli 3 "main:0.0" "1"
SBX_SRC=$(mktemp -d -t send_keys_submit_src.XXXXXX)
REAL_SUT="$SUT"
printf '%s\n' '#!/bin/bash' 'export SEND_KEYS_ALLOW_DIALOG_ENTER=1' "source \"$REAL_SUT\"" \
  'send_keys_submit "$1" "$2"' > "$SBX_SRC/exported.sh"
printf '%s\n' '#!/bin/bash' "source \"$REAL_SUT\"" \
  'SEND_KEYS_ALLOW_DIALOG_ENTER=1 send_keys_submit "$1" "$2"' > "$SBX_SRC/scoped.sh"
preflight_exported_bypass_ignored_when_sourced() { preflight_dialog_after_paste_refuses_enter; }
postcheck_exported_bypass_ignored_when_sourced() { postcheck_dialog_after_paste_refuses_enter; }
SUT="$SBX_SRC/exported.sh" run_test exported_bypass_ignored_when_sourced 3 "main:0.0" "1"
preflight_call_scoped_bypass_works_when_sourced() { preflight_dialog_after_paste_refuses_enter; }
postcheck_call_scoped_bypass_works_when_sourced() { postcheck_bypass_enters_into_the_dialog; }
SUT="$SBX_SRC/scoped.sh" run_test call_scoped_bypass_works_when_sourced 0 "main:0.0" "1"
SUT="$REAL_SUT"
rm -rf "$SBX_SRC"
# T-4832 r1: a broken verifier fails OPEN (the Enter goes ahead) but says so on stderr.
SBX_VERIFY=$(mktemp -d -t send_keys_submit_verify.XXXXXX)
cat > "$SBX_VERIFY/verify.py" <<VEOF
import os, sys
if "--enter-check" in sys.argv:
    sys.exit(1)
sys.argv[0] = "$SCRIPT_DIR/submit_verify.py"
sys.path.insert(0, "$SCRIPT_DIR")
import runpy; runpy.run_path("$SCRIPT_DIR/submit_verify.py", run_name="__main__")
VEOF
preflight_verifier_error_warns_and_proceeds() { printf '%s\n' "clean pane" > "$SBX/pane_states"; }
postcheck_verifier_error_warns_and_proceeds() {
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 1 ]] || { echo "    expected the Enter to go ahead, got $n"; return 1; }
  grep -q "WARNING: dialog re-check verifier failed" "$SBX/stderr" || { echo "    expected the WARNING"; return 1; }
}
SUBMIT_VERIFY_PY="$SBX_VERIFY/verify.py" run_test verifier_error_warns_and_proceeds 0 "main:0.0" "Hello, agent"
rm -rf "$SBX_VERIFY"
run_test socket_flag_threads_through 0 "--socket" "disposable-test-sock" "main:0.0" "Hello, agent"
run_test env_socket_threads_through 0 "main:0.0" "Hello, agent"

# T-4371 r1: the gate's seconds -> microseconds parse (pure bash, so an ungated call
# forks nothing). The fraction is padded, not read as an integer: 0.5s is 500000us.
check_sks_usec() {
  local out rc bad=0 input want
  for pair in "0.5=500000" "2=2000000" "8.=8000000" "0.1=100000" "1.25=1250000" \
              "0=0" "0.0000019=1" "soon=ERR" "=ERR" ".5=ERR" "-1=ERR" "1e3=ERR" \
              "999999.5=999999500000" "1000000=ERR" "9999999999999999=ERR"; do
    input="${pair%=*}" want="${pair##*=}"
    out="unset"
    ( source "$SUT"; _sks_usec "$input" out && echo "$out" || echo ERR ) > "$SBX_USEC/out" 2>&1
    rc="$(cat "$SBX_USEC/out")"
    if [[ "$rc" != "$want" ]]; then
      echo "    _sks_usec '$input': want $want, got $rc"; bad=1
    fi
  done
  return "$bad"
}
SBX_USEC=$(mktemp -d -t send_keys_submit_usec.XXXXXX)
if check_sks_usec; then
  echo "  PASS: sks_usec_parses_decimal_seconds"; PASS=$((PASS+1))
else
  echo "  FAIL: sks_usec_parses_decimal_seconds"; FAIL=$((FAIL+1)); FAILED_TESTS+=("sks_usec_parses_decimal_seconds")
fi
rm -rf "$SBX_USEC"

echo
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  echo "Failed tests: ${FAILED_TESTS[*]}"
  exit 1
fi
exit 0
