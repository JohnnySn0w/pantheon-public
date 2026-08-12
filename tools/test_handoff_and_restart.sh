#!/bin/bash
# Test harness for tools/handoff_and_restart.sh.
#
# Strategy: build a sandbox per test case with fake `tmux`, `systemctl`, `stat`,
# `flock`, and `date` binaries on PATH. Each fake models the real-world contract
# of the underlying tool (per T-0004 meta-validation lesson):
#
#   - `systemctl is-active` writes "active" / "inactive" / "failed" /
#     "activating" to stdout and exits 0 only for "active" (per systemd(1)).
#   - `tmux capture-pane -p` writes multiline pane text to stdout.
#   - `tmux send-keys` is invoked for prompt delivery; the fake records args.
#   - `stat -c %Y <file>` writes an integer epoch second; the fake reads from
#     a sandbox-scoped state file the test bumps to simulate mtime change.
#   - `flock` (non-blocking `-n`) exits 1 if the lock is held; the fake reads
#     a sandbox-scoped flag.
#
# Each test creates a fresh sandbox dir, a fresh fake-bin dir, prepends PATH,
# runs the SUT, and asserts on exit code + recorded side effects.
#
# Invocation:
#   bash tools/test_handoff_and_restart.sh
#
# Exit code: 0 = all pass; 1 = at least one failure.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUT="${SCRIPT_DIR}/handoff_and_restart.sh"

PASS=0
FAIL=0
FAILED_TESTS=()

# ---------------------------------------------------------------------------
# Sandbox + fake-bin construction.
# ---------------------------------------------------------------------------

setup_sandbox() {
  # Create sandbox dir; export $SBX so fakes can read state from it.
  SBX=$(mktemp -d -t handoff_test.XXXXXX)
  FAKEBIN="$SBX/bin"
  mkdir -p "$FAKEBIN"

  # Default sandbox state.
  echo "1000" > "$SBX/handoff_mtime"        # current mtime (epoch seconds)
  echo "active" > "$SBX/systemctl_state"    # what is-active will print
  echo "0" > "$SBX/restart_exitcode"        # what `systemctl restart` returns
  echo "0" > "$SBX/lock_held"               # 1 = flock should fail
  echo "" > "$SBX/pane_text"                # what capture-pane will print
  echo "0" > "$SBX/pane_in_mode"            # T-2268: #{pane_in_mode} for display-message
  echo "0" > "$SBX/sendkeys_calls"          # incremented by send-keys fake
  : > "$SBX/sendkeys_args"                  # one line per send-keys invocation
  : > "$SBX/restart_args"                   # arg string of restart invocation
  # T-2658: control-plane branch state. session_live drives both `tmux has-
  # session` and whether `kill-session` actually takes (see kill_session_
  # survives below). tmux_server_pid/proc fixture back `-N list-sessions` +
  # runtime_scope_owned_and_session_live's cgroup read. Bespoke-path tests
  # never touch any of this.
  echo "1" > "$SBX/session_live"            # 1 = has-session succeeds (session present)
  echo "0" > "$SBX/kill_session_survives"   # 1 = session_live stays 1 even after kill-session
  : > "$SBX/kill_session_calls"             # one line per kill-session invocation
  mkdir -p "$SBX/proc"
  export HANDOFF_RESTART_PROC_ROOT="$SBX/proc"

  # Fake stat: only handles `-c %Y <path>`.
  cat > "$FAKEBIN/stat" <<EOF
#!/bin/bash
# Sandbox state at: $SBX
if [[ "\$1" == "-c" && "\$2" == "%Y" ]]; then
  cat "$SBX/handoff_mtime"
  exit 0
fi
exec /usr/bin/stat "\$@"
EOF
  chmod +x "$FAKEBIN/stat"

  # Fake systemctl: handles `is-active` and `restart`.
  cat > "$FAKEBIN/systemctl" <<EOF
#!/bin/bash
# Sandbox state at: $SBX
# Drop --user / --system flag.
args=()
for a in "\$@"; do
  case "\$a" in
    --user|--system) ;;
    *) args+=("\$a") ;;
  esac
done
case "\${args[0]:-}" in
  is-active)
    state=\$(cat "$SBX/systemctl_state")
    echo "\$state"
    [[ "\$state" == "active" ]] && exit 0 || exit 3
    ;;
  restart)
    echo "\${args[*]}" >> "$SBX/restart_args"
    # T-2658: model the REAL blocking semantics of a Type=oneshot
    # pantheon-persona-consume@<persona> restart — by the time the blocking
    # systemctl-restart call returns, persona_bringup.py consume ->
    # ensure_runtime already created the fresh session, so the
    # control-plane readiness poll should normally find it live immediately.
    # A test omits session_live_after_restart to model "still not live even
    # after restart returned" (the scope-liveness timeout path).
    if [[ -f "$SBX/session_live_after_restart" ]]; then
      echo "\$(cat "$SBX/session_live_after_restart")" > "$SBX/session_live"
    fi
    exit \$(cat "$SBX/restart_exitcode")
    ;;
  *)
    echo "fake systemctl: unhandled subcommand \${args[0]:-}" >&2
    exit 2
    ;;
esac
EOF
  chmod +x "$FAKEBIN/systemctl"

  # Fake tmux: handles the T-2259 delivery path plus `send-keys`/`capture-pane`.
  #
  # T-2319: this fake went stale the moment T-2259 changed HOW text is delivered.
  # send_keys_submit no longer does `send-keys -l "$text"` — it now does
  # `copy-mode -q`, then `load-buffer` (text on STDIN, as opaque bytes so
  # semicolons cannot be re-parsed as tmux commands), then `paste-buffer`, and
  # send-keys carries only the Enter. The fake handled none of those, so
  # load-buffer hit the `*)` arm, exited 2, and every test that delivers a
  # prompt failed — 29 of 33. The SUT was fine; the harness was measuring a
  # mechanism that no longer existed.
  #
  # A delivery is recorded in the SAME place `send-keys -l` used to write, so
  # the existing count and content assertions keep measuring the same property:
  # one delivery + one Enter = 2 calls, exactly as before T-2259.
  cat > "$FAKEBIN/tmux" <<EOF
#!/bin/bash
# Sandbox state at: $SBX
case "\${1:-}" in
  display-message)
    # T-2268: send_keys_submit asks #{pane_in_mode} BEFORE trusting capture-pane,
    # and its fallback is \`|| echo 1\` — untrustworthy — precisely so an
    # unreadable answer can never false-pass. So an unhandled display-message
    # here does not merely skip a check, it makes every delivery decline to
    # verify and fail. Driven by a sandbox file so a test can simulate a pane
    # sitting in copy-mode at verify time.
    cat "$SBX/pane_in_mode" 2>/dev/null || echo 0
    exit 0
    ;;
  copy-mode)
    # T-2259 precondition (cancel any mode before pasting). No-op here, but it
    # MUST be accepted: it is the first tmux call of every delivery.
    exit 0
    ;;
  load-buffer)
    # The text arrives on STDIN now, not in argv.
    cat > "$SBX/tmux_buffer"
    exit 0
    ;;
  paste-buffer)
    n=\$(cat "$SBX/sendkeys_calls")
    echo "\$((n+1))" > "$SBX/sendkeys_calls"
    text=\$(cat "$SBX/tmux_buffer" 2>/dev/null)
    { echo "\$* -- pasted:"; echo "\$text"; } >> "$SBX/sendkeys_args"
    # T-0721: the boot-dialog dismiss is send_keys_submit "\$pane" "1", so the
    # "1" is now PASTED rather than sent as \`-l 1\`. Detect it here.
    if [[ "\$text" == "1" ]]; then echo 1 > "$SBX/boot_dismissed"; fi
    # T-0899: kick token likewise now arrives via the buffer.
    if [[ -f "$SBX/kick_busy_token" ]] && grep -qF "\$(cat "$SBX/kick_busy_token")" <<<"\$text"; then
      echo 1 > "$SBX/kick_sent"
    fi
    exit 0
    ;;
  send-keys)
    n=\$(cat "$SBX/sendkeys_calls")
    echo "\$((n+1))" > "$SBX/sendkeys_calls"
    echo "\$*" >> "$SBX/sendkeys_args"
    # T-0721: flag the boot-dialog dismiss (send_keys_submit sends \`-l 1\`) so
    # capture-pane can model the dialog clearing (dialog -> idle).
    if [[ "\$*" == *"-l 1" ]]; then echo 1 > "$SBX/boot_dismissed"; fi
    # T-0899: when the kick prompt (its unique token) is injected, mark kick-sent
    # so capture-pane can model the dispatched turn (idle -> busy 'esc to interrupt').
    if [[ -f "$SBX/kick_busy_token" ]] && grep -qF "\$(cat "$SBX/kick_busy_token")" <<<"\$*"; then
      echo 1 > "$SBX/kick_sent"
    fi
    exit 0
    ;;
  capture-pane)
    # T-0899: once the kick was sent, the pane shows the busy indicator (the
    # dispatched-kick contract that kick_verify.py polls for). Default ON via
    # pane_text_busy; a test omits pane_text_busy to model a kick that drops.
    # T-0721: else, if a sequenced "after" pane is set, return it once the boot
    # dialog has been dismissed; otherwise the static pane_text (back-compat).
    if [[ -f "$SBX/kick_sent" && -f "$SBX/pane_text_busy" ]]; then
      cat "$SBX/pane_text_busy"
    elif [[ -f "$SBX/pane_text_after" && -f "$SBX/boot_dismissed" ]]; then
      cat "$SBX/pane_text_after"
    else
      cat "$SBX/pane_text"
    fi
    exit 0
    ;;
  kill-session)
    # T-2658: control-plane step 5's pre-restart teardown. Records the call;
    # flips session_live to 0 UNLESS kill_session_survives=1 (models the
    # abort-if-survived path).
    echo call >> "$SBX/kill_session_calls"
    survives=\$(cat "$SBX/kill_session_survives" 2>/dev/null || echo 0)
    [[ "\$survives" == 1 ]] || echo 0 > "$SBX/session_live"
    exit 0
    ;;
  has-session)
    # T-2658: control-plane step 5's abort-if-survived check.
    live=\$(cat "$SBX/session_live" 2>/dev/null || echo 1)
    [[ "\$live" == 1 ]] && exit 0 || exit 1
    ;;
  -N)
    # T-2658: runtime_scope_owned_and_session_live's server-pid probe
    # (-N list-sessions -F #{pid}, via t2590_cutover_verify_lib.sh, sourced
    # by the SUT). Empty output (no pid file written) models "no server".
    if [[ "\${2:-}" == list-sessions ]]; then
      cat "$SBX/tmux_server_pid" 2>/dev/null || true
      exit 0
    fi
    exit 2
    ;;
  *)
    echo "fake tmux: unhandled subcommand \${1:-}" >&2
    exit 2
    ;;
esac
EOF
  chmod +x "$FAKEBIN/tmux"

  # Fake ps for T-0584 Layer 0 + Layer 2.
  #
  # The SUT calls ps in two distinct shapes:
  #   1. Layer 0 — `ps -u <user> -o pid=,args=` (find inner claude PID).
  #      By default returns empty (test-safe). Tests that exercise the
  #      PID-change path write PIDs to $SBX/pre_inner_claude_pid and
  #      $SBX/post_inner_claude_pid; the fake rotates between them via
  #      a phase counter so pre/post invocations return different PIDs.
  #   2. Layer 2 — `ps -o etimes= -p <pid>` (look up holder elapsed).
  #      Returns the value in $SBX/stale_flock_holder_elapsed when set.
  cat > "$FAKEBIN/ps" <<EOF
#!/bin/bash
# Sandbox state at: $SBX
# Detect invocation shape by scanning args.
shape="layer0"
for arg in "\$@"; do
  case "\$arg" in
    -p) shape="layer2" ;;
  esac
done
if [[ "\$shape" == "layer2" ]]; then
  if [[ -f "$SBX/stale_flock_holder_elapsed" ]]; then
    cat "$SBX/stale_flock_holder_elapsed"
  fi
  exit 0
fi
# Layer 0 shape — phase-counter rotation.
phase_file="$SBX/ps_phase"
[[ -f "\$phase_file" ]] || echo 0 > "\$phase_file"
phase=\$(cat "\$phase_file")
echo "\$((phase + 1))" > "\$phase_file"
if [[ "\$phase" == "0" && -f "$SBX/pre_inner_claude_pid" ]]; then
  pid=\$(cat "$SBX/pre_inner_claude_pid")
  echo "\$pid /home/test-user/.npm-global/bin/claude"
elif [[ "\$phase" != "0" && -f "$SBX/post_inner_claude_pid" ]]; then
  pid=\$(cat "$SBX/post_inner_claude_pid")
  echo "\$pid /home/test-user/.npm-global/bin/claude"
fi
# Default: empty output → SUT skips PID-change check (test-safe default).
exit 0
EOF
  chmod +x "$FAKEBIN/ps"

  # Fake lsof for T-0584 Layer 2. Returns the holder PID from
  # $SBX/stale_flock_holder_pid when set; otherwise empty (test-safe
  # default → SUT skips takeover).
  cat > "$FAKEBIN/lsof" <<EOF
#!/bin/bash
# Sandbox state at: $SBX
if [[ -f "$SBX/stale_flock_holder_pid" ]]; then
  cat "$SBX/stale_flock_holder_pid"
fi
exit 0
EOF
  chmod +x "$FAKEBIN/lsof"

  # Fake flock: only handles `-n <fd>` non-blocking acquisition.
  # Real flock(1) syntax: \`flock -n <lockfile> <command...>\` or
  # \`flock -n <fd>\` (when fd already opened by shell). We support the
  # second form, which the SUT uses via \`exec {LOCKFD}>"\$LOCKFILE"\` then
  # \`flock -n "\$LOCKFD"\`.
  cat > "$FAKEBIN/flock" <<EOF
#!/bin/bash
# Sandbox state at: $SBX
held=\$(cat "$SBX/lock_held")
if [[ "\$held" == "1" ]]; then
  exit 1
fi
exit 0
EOF
  chmod +x "$FAKEBIN/flock"

  # PATH: fakes win.
  export PATH="$FAKEBIN:$PATH"

  # T-0256: session-reset emit writes here so smoke runs don't pollute the real
  # state dir at ~/.local/state/pantheon/session_resets/.
  export PANTHEON_STATE_DIR="$SBX/pantheon_state"
  export PANE_LOG_DIR="$SBX/pane_logs"
  mkdir -p "$SBX/pane_logs"

  # T-0550: shorten the paste-mode settle delay for the test harness.
  # The fake tmux has no paste-mode debounce, so the production default
  # (INJECT_ENTER_DELAY=0.7s × up to 3 attempts = 2.1s) can exhaust the
  # --ack-timeout 2 window before "prompt delivered" is even logged.
  # 0.05s is sufficient for the fake; production default is unchanged.
  export INJECT_ENTER_DELAY=0.05

  # T-0590 (a): disable the kick-readiness settle for smoke runs.
  # Production default is 3s; the fake pane is always ready, so the
  # settle is just wasted wall-clock. Specific tests opt in via override.
  export HANDOFF_RESTART_KICK_READINESS_SETTLE_SECS=0

  # T-0899: step-8 now verify-and-re-fires the kick via kick_verify.py (settle ->
  # inject -> poll the pane for a started turn). Fast poll so smoke runs don't
  # wait, and model the dispatched-kick contract by default: kick_busy_token is
  # the default kick prompt's unique phrase; once the fake tmux sees it in a
  # send-keys, capture-pane serves pane_text_busy ('esc to interrupt'). So every
  # kick that fires in the suite verifies as dispatched (exit 0). The
  # deliver-but-not-dispatch path is unit-tested in test_kick_verify.py.
  export HANDOFF_RESTART_KICK_POLL_SECS=3
  export HANDOFF_RESTART_KICK_POLL_INTERVAL=0.1
  echo "post a one-paragraph status snapshot" > "$SBX/kick_busy_token"
  cat > "$SBX/pane_text_busy" <<'EOF'
╭─────────────────────────────────────╮
│                                     │
╰─────────────────────────────────────╯
  esc to interrupt
EOF

  # The SUT references this as the handoff file; create it.
  HANDOFF="$SBX/HANDOFF.md"
  echo "old handoff content" > "$HANDOFF"
}

teardown_sandbox() {
  unset PANTHEON_STATE_DIR
  unset PANE_LOG_DIR
  # T-0550: reap any still-running background bumper to prevent process
  # accumulation across tests (multiple live bumpers compete for CPU and
  # starve the current test's bumper, re-introducing the ack-timeout race).
  if [[ -f "$SBX/bumper_pid" ]]; then
    local bpid
    bpid=$(cat "$SBX/bumper_pid" 2>/dev/null || true)
    if [[ -n "$bpid" ]]; then
      kill "$bpid" 2>/dev/null || true
      wait "$bpid" 2>/dev/null || true
    fi
  fi
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
  # Allow the test body to override sandbox state via $SBX before we run.
  if declare -f "preflight_${name}" >/dev/null; then
    "preflight_${name}"
  fi

  local actual_exit=0
  bash "$SUT" "$@" \
    --handoff-file "$HANDOFF" \
    >"$SBX/stdout" 2>"$SBX/stderr" || actual_exit=$?

  if [[ "$actual_exit" -eq "$expected_exit" ]]; then
    # Allow optional postcondition check.
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
# Common SUT args.
# ---------------------------------------------------------------------------

# T-0550: ack-timeout raised from 2s → 8s and poll-interval from 1s → 0.5s.
# Rationale: the sync-based bumper fires within ~0.1s of the ack-window
# opening; however, integer-epoch deadline arithmetic plus load-induced
# process scheduling jitter means a 2s window with 1s poll gives at most
# two poll attempts — a single scheduling hiccup can miss both.  8s/0.5s
# gives ~16 attempts; the bump lands in attempt 1-2 on any sane system.
# Tests that expect ack-timeout failure (ack_timeout, skip_ack logic-checks)
# are unaffected — they still exit=4 after the window expires, just take up
# to 8s instead of 2s.  The overall suite wall-time increases by <10s total.
COMMON_ARGS=(
  --service claude.service
  --mode user
  --target-pane main:0.0
  --ack-timeout 8
  --steady-timeout 2
  --poll-interval 0.5
)

# ---------------------------------------------------------------------------
# Test cases.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Deterministic mtime-bumper helper.
#
# wait_for_prompt_delivered <stdout_file>
#
# Polls <stdout_file> until the SUT logs "prompt delivered" (written
# synchronously after step 3 completes and just before step 4's ack-wait
# loop begins).  Once that line is present the bump is guaranteed to land
# inside the ack-wait window regardless of system load, eliminating the
# race that made kick_invocation (and every other ack-wait preflight) exit
# 4 instead of the expected 6/7/0 on busy hosts.
#
# Cap: max 100 iterations × 0.05 s = 5 s.  If the SUT never prints the
# line (genuine failure), the loop exits without bumping and the test fails
# loudly with an ack-timeout rather than hanging.
#
# T-0550: replaces the former blind `sleep 1` across all 13 bump sites.
wait_for_prompt_delivered() {
  local stdout_file="$1"
  local i=0
  until grep -q "prompt delivered" "$stdout_file" 2>/dev/null; do
    i=$((i + 1))
    if [[ $i -ge 100 ]]; then
      # SUT never printed the sync line; exit without bumping so the test
      # fails with a clear ack-timeout rather than a silent hang.
      return 1
    fi
    sleep 0.05
  done
  return 0
}

# --- 1: Happy path. ---------------------------------------------------------
preflight_happy() {
  # Simulate the HO writing a fresh handoff: bump mtime once the SUT has
  # reached the ack-wait loop (signalled by "prompt delivered" in stdout).
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"

  # Pane text contains the sentinel prompt frame AND the idle-state sentinel
  # (T-0172 + T-0528: VERIFY_PATTERN AND IDLE_SENTINEL=`❯` AND NOT DIALOG_EXCLUDE).
  cat > "$SBX/pane_text" <<'EOF'
some scrollback
more scrollback
╭─────────────────────────────────────╮
│ ❯ Try "what's the weather"          │
╰─────────────────────────────────────╯
  ? for shortcuts
EOF
}
postcheck_happy() {
  # Background bumper might still be running; reap it.
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true

  # send-keys was invoked at least once (prompt delivery).
  local n
  n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -ge 1 ]] || { echo "    expected sendkeys >=1, got $n"; return 1; }

  # systemctl restart was invoked with the right service.
  grep -q "restart claude.service" "$SBX/restart_args" \
    || { echo "    expected 'restart claude.service' in restart_args"; return 1; }

  return 0
}

# --- 1b (T-1049): pause mode (--no-restart). --------------------------------
# Steps 1-4 + graveyard run; then STOP — no systemctl restart, no post-restart
# steps. The fleet-reboot quiesce: agent finished + recorded + paused.
preflight_no_restart() {
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"
  # Pane text not needed (steps 5-8 skipped) but harmless.
  cat > "$SBX/pane_text" <<'EOF'
some scrollback
EOF
}
postcheck_no_restart() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  # Prompt was delivered (step 3 ran).
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -ge 1 ]] || { echo "    expected sendkeys >=1, got $n"; return 1; }
  # The pause-mode log line fired.
  grep -q "pause mode" "$SBX/stdout" \
    || { echo "    expected 'pause mode' log line in stdout"; return 1; }
  # CRITICAL: systemctl restart must NOT have been invoked.
  if [[ -s "$SBX/restart_args" ]]; then
    echo "    pause mode must NOT restart, but restart_args has: $(cat "$SBX/restart_args")"
    return 1
  fi
  return 0
}

# --- 1c (T-1049): --no-restart + --skip-ack are mutually exclusive. ---------
postcheck_no_restart_skip_ack_rejected() {
  grep -q "mutually exclusive" "$SBX/stderr" \
    || { echo "    expected 'mutually exclusive' in stderr"; return 1; }
  return 0
}

# --- 2: Lock contention. ----------------------------------------------------
preflight_lock_held() {
  echo "1" > "$SBX/lock_held"
}

# --- 3: Ack timeout (mtime never bumps). ------------------------------------
preflight_ack_timeout() {
  : # No bump; mtime stays at 1000 the whole window.
}
postcheck_ack_timeout() {
  # send-keys was still attempted (we deliver before polling).
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -ge 1 ]] || return 1
  return 0
}

# --- 4: Restart fails. ------------------------------------------------------
preflight_restart_fail() {
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"
  echo "1" > "$SBX/restart_exitcode"
}
postcheck_restart_fail() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  return 0
}

# --- 5: is-active fast-fail. ------------------------------------------------
preflight_isactive_fail() {
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"
  echo "failed" > "$SBX/systemctl_state"
}
postcheck_isactive_fail() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  return 0
}

# ---------------------------------------------------------------------------
# T-2658: control-plane branch (--supervisor control-plane). Step 5 kills the
# target session before restarting the (caller-resolved) consume unit; step 6
# polls runtime_scope_owned_and_session_live instead of `systemctl is-active`.
# ---------------------------------------------------------------------------

# write_scope_fixture <pid> — synthetic /proc/<pid>/cgroup line the control-
# plane tests point HANDOFF_RESTART_PROC_ROOT at (set unconditionally in
# setup_sandbox to $SBX/proc), matching runtime_scope_owned_and_session_live's
# real cgroup-membership read.
write_scope_fixture() {
  local pid="$1"
  echo "$pid" > "$SBX/tmux_server_pid"
  mkdir -p "$SBX/proc/$pid"
  echo "0::/user.slice/user-1002.slice/user@1002.service/app.slice/pantheon-runtime-tmux.scope" \
    > "$SBX/proc/$pid/cgroup"
}

# --- 5cp: happy path — kill-session, consume-unit restart, scope-live poll. -
preflight_control_plane_happy() {
  preflight_happy
  write_scope_fixture 777
  # Models the real blocking-oneshot semantics: by the time `systemctl
  # restart` returns, ensure_runtime() already created the fresh session.
  echo "1" > "$SBX/session_live_after_restart"
}
postcheck_control_plane_happy() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  local n; n=$(wc -l < "$SBX/kill_session_calls")
  [[ "$n" -ge 1 ]] || { echo "    expected kill-session >=1, got $n"; return 1; }
  grep -q "pantheon-persona-consume@specialist.service" "$SBX/restart_args" \
    || { echo "    expected consume unit in restart_args: $(cat "$SBX/restart_args")"; return 1; }
  grep -q "control-plane: killed session" "$SBX/stdout" \
    || { echo "    expected 'control-plane: killed session' log line"; return 1; }
  grep -q "runtime scope/session live" "$SBX/stdout" \
    || { echo "    expected 'runtime scope/session live' log line"; return 1; }
  return 0
}

# --- 5cp-b: session survives kill-session -> abort (exit 5), no restart. ----
preflight_control_plane_kill_survives() {
  preflight_happy
  echo "1" > "$SBX/kill_session_survives"
}
postcheck_control_plane_kill_survives() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  local n; n=$(wc -l < "$SBX/kill_session_calls")
  [[ "$n" -ge 1 ]] || { echo "    expected kill-session >=1, got $n"; return 1; }
  if [[ -s "$SBX/restart_args" ]]; then
    echo "    must NOT restart when the session survived kill-session, but restart_args has: $(cat "$SBX/restart_args")"
    return 1
  fi
  grep -q "survived kill-session" "$SBX/stdout" "$SBX/stderr" \
    || { echo "    expected 'survived kill-session' in stdout/stderr"; return 1; }
  return 0
}

# --- 5cp-c: scope/session never live after restart -> timeout (exit 6). ----
preflight_control_plane_scope_timeout() {
  preflight_happy
  write_scope_fixture 777
  # No session_live_after_restart write: session stays dead post-restart, so
  # the readiness poll exhausts STEADY_TIMEOUT (2s per COMMON_ARGS).
}
postcheck_control_plane_scope_timeout() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  grep -q "runtime scope/session-live timeout" "$SBX/stdout" "$SBX/stderr" \
    || { echo "    expected 'runtime scope/session-live timeout' in stdout/stderr"; return 1; }
  return 0
}

# --- 6: Steady-state pane never shows sentinel. -----------------------------
preflight_steady_timeout() {
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"
  # Pane shows boot output but never the prompt frame.
  cat > "$SBX/pane_text" <<'EOF'
loading...
some warmup messages
not yet ready
EOF
}
postcheck_steady_timeout() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  return 0
}

# --- 7: Custom verify pattern. ---------------------------------------------
preflight_custom_pattern() {
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"
  # T-0172: custom_pattern overrides VERIFY_PATTERN but the IDLE_SENTINEL
  # default still applies. Pane must contain both the custom token AND
  # the idle sentinel (T-0528: now `❯`) for step 7 to pass.
  cat > "$SBX/pane_text" <<'EOF'
FRESH_HO_READY_TOKEN_42
❯
EOF
}
postcheck_custom_pattern() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  return 0
}

# --- 8: Dry-run skips side effects. -----------------------------------------
preflight_dry_run() {
  : # Even though mtime never bumps, dry-run shouldn't poll for ack.
}
postcheck_dry_run() {
  # No send-keys, no restart.
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 0 ]] || { echo "    dry-run sent keys ($n times)"; return 1; }
  if [[ -s "$SBX/restart_args" ]]; then
    echo "    dry-run invoked restart"
    return 1
  fi
  return 0
}

# --- 9: Missing required arg (--service). -----------------------------------
# No preflight needed.

# --- 10: Bare-horizontal sentinel (no corners). -----------------------------
preflight_bare_horizontal() {
  # Simulate HO writing a fresh handoff in the background.
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"

  # Pane text uses bare horizontal runs only — no ╭/╮ corners.
  # Models the rendering variant observed on Claude Code v2.1.x (Recovery restart
  # fire 2026-05-05T04:55Z: cat -A confirmed E2 94 80 (U+2500) only).
  # T-0172: also include the idle-state sentinel "? for shortcuts" since
  # step 7 now requires both prompt-frame AND idle-sentinel to match.
  cat > "$SBX/pane_text" <<'EOF'
────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ? for shortcuts
EOF
}
postcheck_bare_horizontal() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true

  # send-keys was invoked at least once (prompt delivery).
  local n
  n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -ge 1 ]] || { echo "    expected sendkeys >=1, got $n"; return 1; }

  # systemctl restart was invoked with the right service.
  grep -q "restart claude.service" "$SBX/restart_args" \
    || { echo "    expected 'restart claude.service' in restart_args"; return 1; }

  return 0
}

# --- 11: T-0528 + T-0721 — a NON-allowlisted (destructive) dialog must NOT
# false-pass step 7 AND must NOT be auto-dismissed. The dialog renders with the
# prompt-frame chars (─) AND `❯` option cursor, so the positive prongs match;
# the `Esc to cancel` footer must reject it (DIALOG_EXCLUDE), and T-0721's inline
# dismiss must NOT fire (boot sentinel absent) → exit 7, fail-closed.
preflight_dialog_false_positive() {
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"

  # Destructive-Bash confirm: NOT the dev-channels startup dialog. T-0721 must
  # leave it alone (no '1' press), and DIALOG_EXCLUDE must reject the steady poll.
  cat > "$SBX/pane_text" <<'EOF'
  Bash command
    rm -rf /tmp/some/path

────────────────────────────────────────────────────────────────────────────────
  ❯ 1. Yes
    2. No
────────────────────────────────────────────────────────────────────────────────

  Do you want to proceed?
  Esc to cancel · Tab to amend · ctrl+e to explain
EOF
}
postcheck_dialog_false_positive() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  # T-0721: confirm NO inline dismiss was attempted on this non-boot dialog.
  if grep -q "dismissing inline" "$SBX/stdout"; then
    echo "  (T-0721 violation: inline-dismissed a non-boot dialog)"
    return 1
  fi
  return 0
}

# --- 11b (T-0721): dev-channels startup dialog is DISMISSED inline, then step 7
# passes once the pane clears to idle. Models dialog -> (press 1) -> idle.
preflight_boot_dialog_dismiss() {
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"

  # Initial pane: the dev-channels startup confirm (boot sentinel + footer).
  cat > "$SBX/pane_text" <<'EOF'
  WARNING: Loading development channels
  Channels: server:telegram-bridge-stdio
────────────────────────────────────────────────────────────────────────────────
  ❯ 1. I am using this for local development
    2. Exit
────────────────────────────────────────────────────────────────────────────────
  Enter to confirm · Esc to cancel
EOF
  # After the dismiss (`-l 1`), the fake tmux serves this idle pane.
  cat > "$SBX/pane_text_after" <<'EOF'
╭─────────────────────────────────────╮
│ ❯ Try "what's the weather"          │
╰─────────────────────────────────────╯
  ? for shortcuts
EOF
}
postcheck_boot_dialog_dismiss() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  # The inline dismiss must have fired, and a literal '1' must have been sent.
  if ! grep -q "dismissing inline" "$SBX/stdout"; then
    echo "  (expected inline boot-dialog dismiss, none logged)"
    return 1
  fi
  # T-2319: the dismiss is send_keys_submit "$pane" "1", so since T-2259 the
  # "1" is PASTED (load-buffer/paste-buffer), never sent as `-l 1`. Assert on
  # the delivered TEXT — which is the property that always mattered — rather
  # than on the tmux verb that happened to carry it in 2026.
  if ! grep -qx -- "1" "$SBX/sendkeys_args"; then
    echo "  (expected a '1' delivered to the pane for the dismiss)"
    return 1
  fi
  return 0
}

# --- 11c (T-0721): boot sentinel co-present with a DANGEROUS confirm
# (denylist) must NOT be dismissed → fail-closed exit 7.
preflight_boot_dialog_denylist_blocks() {
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"

  # Boot sentinel present (e.g. in scrollback) BUT a destructive confirm is the
  # focused modal. T-0721 denylist must block the dismiss.
  cat > "$SBX/pane_text" <<'EOF'
  (scrollback) I am using this for local development
────────────────────────────────────────────────────────────────────────────────
  ❯ 1. Yes
    2. No
────────────────────────────────────────────────────────────────────────────────
  Do you want to proceed?
  Esc to cancel · Tab to amend
EOF
}
postcheck_boot_dialog_denylist_blocks() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  if grep -q "dismissing inline" "$SBX/stdout"; then
    echo "  (T-0721 violation: dismissed despite denylisted marker co-present)"
    return 1
  fi
  return 0
}

# --- 12: T-0172 (B) — post-step-7 kick fires on happy path. ----------------
preflight_kick_invocation() {
  # Same fixture as happy: prompt frame + idle sentinel `❯`.
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"
  cat > "$SBX/pane_text" <<'EOF'
╭─────────────────────────────────────╮
│ ❯ Try "what's the weather"          │
╰─────────────────────────────────────╯
  ? for shortcuts
EOF
}
postcheck_kick_invocation() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true

  # Initial prompt: 2 send-keys (text + Enter). Kick: 2 send-keys (text + Enter).
  local n
  n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 4 ]] \
    || { echo "    expected sendkeys=4 (prompt+enter+kick+enter), got $n"; return 1; }

  # Kick text must reference the HANDOFF read directive.
  grep -q "Read $HANDOFF" "$SBX/sendkeys_args" \
    || { echo "    expected kick text 'Read $HANDOFF' in sendkeys_args"; return 1; }

  return 0
}

# --- 13: T-0172 (B) — --no-kick skips the post-step-7 kick. ----------------
preflight_no_kick() {
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"
  cat > "$SBX/pane_text" <<'EOF'
╭─────────────────────────────────────╮
│ ❯ prompt                            │
╰─────────────────────────────────────╯
  ? for shortcuts
EOF
}
postcheck_no_kick() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true

  # With --no-kick, only the initial prompt's 2 send-keys should fire.
  local n
  n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 2 ]] \
    || { echo "    expected sendkeys=2 (--no-kick suppresses kick), got $n"; return 1; }

  # Kick text must NOT appear in sendkeys_args.
  if grep -q "Read $HANDOFF" "$SBX/sendkeys_args"; then
    echo "    --no-kick still emitted kick text"; return 1
  fi

  return 0
}

# --- 14: T-0172 (A) — custom --idle-sentinel override. ---------------------
preflight_custom_idle_sentinel() {
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"
  # Custom idle sentinel; pane has prompt frame + custom marker (not the default).
  cat > "$SBX/pane_text" <<'EOF'
╭─────────────────────────────────────╮
│ > prompt                            │
╰─────────────────────────────────────╯
CUSTOM_IDLE_TOKEN_99
EOF
}
postcheck_custom_idle_sentinel() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  return 0
}

# --- 15: T-0510 — --skip-ack force-restart bypasses prompt + ack-wait. ------
preflight_skip_ack() {
  # Deliberately NO mtime bump: under normal flow this would exit 4 (ack
  # timeout). --skip-ack must bypass steps 2-4 entirely and still restart.
  cat > "$SBX/pane_text" <<'EOF'
╭─────────────────────────────────────╮
│ ❯ prompt                            │
╰─────────────────────────────────────╯
  ? for shortcuts
EOF
}
postcheck_skip_ack() {
  # Restart must have fired (proves we got past the skipped ack-wait without
  # the mtime ever bumping — a non-skip run would have exited 4 here).
  grep -q "restart claude.service" "$SBX/restart_args" \
    || { echo "    expected 'restart claude.service' in restart_args"; return 1; }

  # send-keys count must be exactly 2 = the post-restart kick (text + Enter)
  # ONLY. The initial handoff prompt (steps 2-4) is skipped, so no prompt
  # send-keys fired. Happy path is 4; skip-ack is 2.
  local n; n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 2 ]] \
    || { echo "    expected sendkeys=2 (kick only; prompt skipped), got $n"; return 1; }

  # The 2 send-keys that DID fire are the kick — must reference the HANDOFF read.
  grep -q "Read $HANDOFF" "$SBX/sendkeys_args" \
    || { echo "    expected kick text 'Read $HANDOFF' in sendkeys_args"; return 1; }

  return 0
}

# --- T-2829a: --dismiss-modal sends Escape BEFORE the handoff prompt. --------
# A session wedged on a permission modal ignores the handoff prompt (the modal
# captures the input). --dismiss-modal presses Escape first (fail-closed: cancels
# the modal, never approves) so the session can receive + act on the prompt. This
# is the happy path (ack bumps), so exit 0; the assertions are that Escape fired
# and that it fired BEFORE the prompt was pasted.
preflight_dismiss_modal_sends_escape() {
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"
  cat > "$SBX/pane_text" <<'EOF'
╭─────────────────────────────────────╮
│ ❯ Try "what's the weather"          │
╰─────────────────────────────────────╯
  ? for shortcuts
EOF
}
postcheck_dismiss_modal_sends_escape() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  # The dismiss-modal log line fired.
  grep -q "dismiss-modal: sending Escape" "$SBX/stdout" \
    || { echo "    expected 'dismiss-modal: sending Escape' log line"; return 1; }
  # An Escape send-keys was recorded.
  grep -q "Escape" "$SBX/sendkeys_args" \
    || { echo "    expected an Escape send in sendkeys_args"; return 1; }
  # Ordering: Escape must precede the pasted handoff prompt (the "-- pasted:"
  # marker the fake tmux writes on paste-buffer). The whole point is Escape
  # BEFORE the graceful handoff.
  local esc_line prompt_line
  esc_line=$(grep -n "Escape" "$SBX/sendkeys_args" | head -1 | cut -d: -f1)
  prompt_line=$(grep -n -- "-- pasted:" "$SBX/sendkeys_args" | head -1 | cut -d: -f1)
  if [[ -z "$esc_line" || -z "$prompt_line" || "$esc_line" -ge "$prompt_line" ]]; then
    echo "    Escape (line ${esc_line:-none}) must precede the pasted prompt (line ${prompt_line:-none})"
    return 1
  fi
  return 0
}

# --- T-2829b: default (no --dismiss-modal) sends NO Escape. -----------------
# Regression guard: the operator's attended manual restart must be byte-identical to
# pre-T-2829 — no stray Escape into a healthy session.
preflight_no_dismiss_modal_no_escape() {
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"
  cat > "$SBX/pane_text" <<'EOF'
╭─────────────────────────────────────╮
│ ❯ Try "what's the weather"          │
╰─────────────────────────────────────╯
  ? for shortcuts
EOF
}
postcheck_no_dismiss_modal_no_escape() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  if grep -q "Escape" "$SBX/sendkeys_args"; then
    echo "    default run must NOT send Escape, but sendkeys_args has one"
    return 1
  fi
  if grep -q "dismiss-modal" "$SBX/stdout"; then
    echo "    default run must NOT log dismiss-modal"
    return 1
  fi
  return 0
}

# --- T-2829c: --hard-restart-on-ack-timeout falls through to the restart. ----
# No mtime bump → the ack times out. WITHOUT the flag this is exit 4 (see the
# ack_timeout case). WITH the flag, the script must fall through to the restart
# (steps 5-8) and exit 0 — recovering a session that cannot hand off instead of
# leaving it wedged (the specialist modal-wedge hole).
preflight_hard_restart_on_ack_timeout() {
  : # No bumper — mtime never changes, so step 4 times out.
  cat > "$SBX/pane_text" <<'EOF'
╭─────────────────────────────────────╮
│ ❯ prompt                            │
╰─────────────────────────────────────╯
  ? for shortcuts
EOF
}
postcheck_hard_restart_on_ack_timeout() {
  # The fallback log line fired.
  grep -q "HARD-RESTART fallback" "$SBX/stdout" "$SBX/stderr" \
    || { echo "    expected 'HARD-RESTART fallback' in stdout/stderr"; return 1; }
  # The restart actually fired despite the ack timeout.
  grep -q "restart claude.service" "$SBX/restart_args" \
    || { echo "    expected 'restart claude.service' — fallback did not restart"; return 1; }
  # No graveyard archive of a (nonexistent) fresh handoff on this path.
  if grep -q "\[graveyard\] " "$SBX/stdout"; then
    echo "    fallback path must NOT archive a handoff (none was written)"
    return 1
  fi
  return 0
}

# --- 16: T-0543 — Claude Code 2.1.x idle pane reaches steady-state (TDD). ---
# The NEW default IDLE_SENTINEL ('❯|\? for shortcuts|ctrl\+t to hide tasks')
# must match a 2.1.x idle pane showing '❯'. The OLD default
# ('\? for shortcuts|ctrl\+t to hide tasks') does NOT match → exit 7.
# This test is RED before the fix (old default can't match '❯'-only pane)
# and GREEN after (new default matches '❯').
preflight_idle_sentinel_default_v21x() {
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"
  # Claude Code 2.1.x idle pane: has prompt frame + '❯', NO '? for shortcuts'.
  cat > "$SBX/pane_text" <<'EOF'
────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
EOF
}
postcheck_idle_sentinel_default_v21x() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true

  # Restart must have fired — proved we reached steady-state.
  grep -q "restart claude.service" "$SBX/restart_args" \
    || { echo "    expected 'restart claude.service' — steady-state not reached"; return 1; }

  return 0
}

# --- 17: T-0543 — OLD default fails on 2.1.x idle pane (documents bug shape). -
# Explicitly pass the OLD stale sentinel. Same '❯'-only pane must exit 7.
# This test stays exit 7 permanently — it documents why the old default
# was the class of failure and must never change.
preflight_idle_sentinel_old_default_would_fail() {
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"
  # Same 2.1.x idle pane as above — '❯' only, no old sentinel text.
  cat > "$SBX/pane_text" <<'EOF'
────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
EOF
}
postcheck_idle_sentinel_old_default_would_fail() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  return 0
}

# --- 19: T-0528 — disabling DIALOG_EXCLUDE lets dialog false-pass (negative
# evidence for the new prong). Same fixture as case 11 (dialog with both `─`
# frame chars AND `❯` option cursor), but pass `--dialog-exclude NEVER_MATCH`
# so the negative prong is effectively disabled. Without the exclusion, the
# positive conjunction MATCHES → exit 0. This documents EXACTLY what the
# exclusion guards against: a dialog with positives would otherwise sail
# through step 7 as steady-state, and the kick would land in the dialog.
preflight_dialog_exclude_disabled() {
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"
  # Identical to dialog_false_positive fixture.
  cat > "$SBX/pane_text" <<'EOF'
  WARNING: Loading development channels

  --dangerously-load-development-channels is for local channel development only.

  Channels: server:telegram-bridge-stdio

────────────────────────────────────────────────────────────────────────────────
  ❯ 1. I am using this for local development
    2. Exit
────────────────────────────────────────────────────────────────────────────────

  Enter to confirm · Esc to cancel
EOF
}
postcheck_dialog_exclude_disabled() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  return 0
}

# --- 18: T-0543 — Busy pane (esc to interrupt, no frame) must NOT pass. -----
# A pane showing 'esc to interrupt' with NO prompt frame must exit 7.
# Documents that busy panes never reach steady-state; also confirms that
# 'esc to interrupt' alone (absent VERIFY_PATTERN) cannot false-pass step 7.
preflight_busy_pane_no_false_pass() {
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"
  # Busy pane: 'esc to interrupt' visible but NO prompt frame chars.
  cat > "$SBX/pane_text" <<'EOF'
Running a long task...
esc to interrupt
EOF
}
postcheck_busy_pane_no_false_pass() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  return 0
}

# ---------------------------------------------------------------------------
# T-0584 Layer 1: wall-clock watchdog test.
#
# Replace the fake tmux with one that hangs on send-keys for 60s — way past
# the 2s watchdog budget we set via env. The watchdog must TERM-then-KILL the
# script (and its tmux child) within budget + 5s grace + epsilon.
#
# Postcheck verifies:
#   - the FATAL wall-clock log line landed on stderr
#   - the hung tmux send-keys child PID is no longer alive (the watchdog
#     reaped it via pkill -P, not just killed the parent script)
# ---------------------------------------------------------------------------
preflight_wall_clock_watchdog_fires() {
  # Replace tmux with one that hangs the send-keys path. Other subcommands
  # respond normally so steps before step 3 (no tmux) and after (capture-pane)
  # don't need to also be patched.
  cat > "$FAKEBIN/tmux" <<EOF
#!/bin/bash
# Sandbox state at: $SBX
case "\${1:-}" in
  load-buffer)
    # T-2319: the hang has to be on the call the SUT actually blocks in. Since
    # T-2259 that is the DELIVERY path (load-buffer), not send-keys — this fake
    # still hung on send-keys, so the SUT died on an unhandled load-buffer
    # before ever reaching the hang, and the watchdog under test never armed.
    # A fake that fails EARLIER than the thing it is simulating tests nothing.
    echo \$\$ > "$SBX/hung_tmux_pid"
    sleep 60
    exit 0
    ;;
  copy-mode)
    exit 0
    ;;
  display-message)
    echo 0
    exit 0
    ;;
  send-keys)
    # Kept hanging too: the Enter step is also a legitimate place to wedge, and
    # a delivery that got past load-buffer must not then sail through.
    echo \$\$ > "$SBX/hung_tmux_pid"
    sleep 60
    exit 0
    ;;
  capture-pane)
    cat "$SBX/pane_text"
    exit 0
    ;;
  *)
    echo "fake tmux: unhandled subcommand \${1:-}" >&2
    exit 2
    ;;
esac
EOF
  chmod +x "$FAKEBIN/tmux"
  # Tight watchdog budget. Production default is 1800s; 2s is fine for
  # the harness — the hung tmux fires for 60s so 2s + 5s grace + epsilon
  # is well bounded.
  export HANDOFF_RESTART_WALL_CLOCK_SECS=2
}
postcheck_wall_clock_watchdog_fires() {
  unset HANDOFF_RESTART_WALL_CLOCK_SECS
  # The FATAL watchdog line must have landed on stderr.
  if ! grep -q "wall-clock timeout 2s exceeded" "$SBX/stderr"; then
    echo "  (watchdog FATAL log line not found in stderr)"
    return 1
  fi
  # The hung tmux send-keys child must be reaped — not lingering as a
  # zombie holding the flock indefinitely (the original 2026-05-25
  # failure mode).
  if [[ -f "$SBX/hung_tmux_pid" ]]; then
    local pid
    pid=$(cat "$SBX/hung_tmux_pid")
    if kill -0 "$pid" 2>/dev/null; then
      echo "  (hung tmux send-keys child PID $pid still alive)"
      kill -KILL "$pid" 2>/dev/null || true  # cleanup so the test runner doesn't accumulate
      return 1
    fi
  fi
  return 0
}

# ---------------------------------------------------------------------------
# T-0584 Layer 1: happy-path watchdog cleanup test.
#
# Confirms a NORMAL run (no hang) leaves no leaked watchdog process — the
# EXIT trap must cancel the watchdog so the sleep doesn't outlive the
# script.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# T-0584 Layer 2: stale-flock takeover tests.
#
# Scenario: a lockfile exists, lsof says PID 99999 holds it, that PID's
# elapsed time exceeds the threshold (we set it to 60s for the test so a
# fixture of 3600s reliably trips it). SUT should:
#   - log WARN about the stale flock
#   - "kill" the holder (harmless no-op since PID 99999 doesn't exist)
#   - rm the lockfile
#   - proceed with the rest of the cycle to exit 0
# ---------------------------------------------------------------------------
# Fixed sandbox lockfile path for stale-flock tests (computed once per
# smoke run, shared across these two cases — cleaned up by the postchecks).
STALE_LOCKFILE_PATH="/tmp/handoff_test_stale_flock.$$.lock"

preflight_stale_flock_takeover() {
  preflight_happy
  : > "$STALE_LOCKFILE_PATH"
  echo 99999 > "$SBX/stale_flock_holder_pid"
  echo 3600 > "$SBX/stale_flock_holder_elapsed"
  export HANDOFF_RESTART_STALE_FLOCK_THRESHOLD_SECS=60
}
postcheck_stale_flock_takeover() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  unset HANDOFF_RESTART_STALE_FLOCK_THRESHOLD_SECS
  rm -f "$STALE_LOCKFILE_PATH"
  grep -q "stale flock detected" "$SBX/stdout" \
    || { echo "    expected 'stale flock detected' WARN in stdout"; return 1; }
  return 0
}

# Stale-flock skipped — holder elapsed below threshold.
preflight_stale_flock_below_threshold_skipped() {
  preflight_happy
  : > "$STALE_LOCKFILE_PATH"
  echo 12345 > "$SBX/stale_flock_holder_pid"
  echo 30 > "$SBX/stale_flock_holder_elapsed"  # below threshold
  export HANDOFF_RESTART_STALE_FLOCK_THRESHOLD_SECS=60
}
postcheck_stale_flock_below_threshold_skipped() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  unset HANDOFF_RESTART_STALE_FLOCK_THRESHOLD_SECS
  rm -f "$STALE_LOCKFILE_PATH"
  if grep -q "stale flock detected" "$SBX/stdout"; then
    echo "    unexpected 'stale flock detected' log when holder is below threshold"
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# T-0590 (a) / T-0899: kick-readiness settle test.
#
# Overrides the default-disabled-for-tests setting to use a tiny settle (0.1s).
# Post-T-0899 the settle is owned by kick_verify.py (passed as --settle), so the
# SUT no longer logs a bash-side "kick readiness settle" line; instead kick_verify
# logs the effective params. Assert the settle is plumbed through to kick_verify
# and the kick still delivers + dispatch-verifies. Covers the production path that
# closes the silent-kick-drop race window (now verify-and-re-fire, not blind settle).
# ---------------------------------------------------------------------------
preflight_kick_readiness_settle() {
  preflight_happy
  export HANDOFF_RESTART_KICK_READINESS_SETTLE_SECS=0.1
}
postcheck_kick_readiness_settle() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  unset HANDOFF_RESTART_KICK_READINESS_SETTLE_SECS
  grep -q "settle=0.1s" "$SBX/stdout" \
    || { echo "    expected kick_verify 'settle=0.1s' (settle plumbed to kick_verify)"; return 1; }
  grep -q "post-restart kick delivered + dispatch-verified" "$SBX/stdout" \
    || { echo "    expected 'post-restart kick delivered + dispatch-verified' log"; return 1; }
  return 0
}

# T-0899: kick DROPS — integration coverage of the production failure mode.
#
# The kick is sent but the pane never shows a started turn (omit pane_text_busy),
# so kick_verify polls out and re-fires the bounded number of times, then the SUT
# exits 8. This exercises the bash wiring's drop path end-to-end (the unit tests
# in test_kick_verify.py cover the deliver_kick_verified logic itself).
# ---------------------------------------------------------------------------
preflight_kick_drops_refires() {
  preflight_happy
  rm -f "$SBX/pane_text_busy"            # model a kick that never dispatches
  export HANDOFF_RESTART_KICK_POLL_SECS=0.3
  export HANDOFF_RESTART_KICK_MAX_ATTEMPTS=2
}
postcheck_kick_drops_refires() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  unset HANDOFF_RESTART_KICK_POLL_SECS HANDOFF_RESTART_KICK_MAX_ATTEMPTS
  # prompt delivery (text+enter=2) + 2 bounded kick attempts (text+enter each) = 6.
  local n
  n=$(cat "$SBX/sendkeys_calls")
  [[ "$n" -eq 6 ]] \
    || { echo "    expected sendkeys=6 (prompt + 2 bounded kick attempts), got $n"; return 1; }
  grep -q "failed verification" "$SBX/stderr" \
    || { echo "    expected 'failed verification' err on a dropped kick"; return 1; }
  return 0
}

# ---------------------------------------------------------------------------
# T-0584 Layer 0: PID-change assertion test cases.
#
# pid_change_verified: pre PID 1234 / post PID 5678 — different. Step 7
# logs "PID-change verified" and exits 0. Same fixture as happy test.
# ---------------------------------------------------------------------------
preflight_pid_change_verified() {
  preflight_happy
  echo 1234 > "$SBX/pre_inner_claude_pid"
  echo 5678 > "$SBX/post_inner_claude_pid"
}
postcheck_pid_change_verified() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  grep -q "pre-restart inner-claude PID: 1234" "$SBX/stdout" \
    || { echo "    expected 'pre-restart inner-claude PID: 1234' in stdout"; return 1; }
  grep -q "PID-change verified: 1234 -> 5678" "$SBX/stdout" \
    || { echo "    expected 'PID-change verified: 1234 -> 5678' in stdout"; return 1; }
  return 0
}

# ---------------------------------------------------------------------------
# pid_change_unchanged_fails: pre PID 9999 / post PID 9999 — same.
# Captures the 2026-05-25 14:46Z legacy out-of-cgroup case where step 5
# silently no-op'd the inner claude. Script must exit 7 (step-7 failure)
# instead of falsely reporting success.
# ---------------------------------------------------------------------------
preflight_pid_change_unchanged_fails() {
  preflight_happy
  echo 9999 > "$SBX/pre_inner_claude_pid"
  echo 9999 > "$SBX/post_inner_claude_pid"  # same as pre — restart was a no-op
}
postcheck_pid_change_unchanged_fails() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  grep -q "inner-claude PID unchanged after restart" "$SBX/stderr" \
    || { echo "    expected 'inner-claude PID unchanged' err in stderr"; return 1; }
  return 0
}

# ---------------------------------------------------------------------------
# pid_change_skipped_when_no_baseline: ps returns empty pre-restart (no
# claude running for this user, or test-default). Check is skipped silently;
# the rest of the pipeline succeeds. Documents the production-safe default.
# ---------------------------------------------------------------------------
preflight_pid_change_skipped_when_no_baseline() {
  preflight_happy
  # No pre_inner_claude_pid file → fake ps returns empty → SUT skips check.
}
postcheck_pid_change_skipped_when_no_baseline() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  # Should NOT log a pre-restart PID line OR a PID-change-verified line.
  if grep -q "pre-restart inner-claude PID:" "$SBX/stdout"; then
    echo "    unexpected 'pre-restart inner-claude PID' log when baseline empty"
    return 1
  fi
  if grep -q "PID-change verified" "$SBX/stdout"; then
    echo "    unexpected 'PID-change verified' log when baseline empty"
    return 1
  fi
  return 0
}

preflight_wall_clock_watchdog_clean_exit() {
  # Same setup as the happy-path test (bumper + idle pane text), with the
  # watchdog set to a distinctive sleep value we can scan for post-exit.
  preflight_happy
  # 17357 is unlikely to collide with anything else on the system; the
  # postcheck greps for this exact `sleep 17357` to detect leaks.
  export HANDOFF_RESTART_WALL_CLOCK_SECS=17357
}
postcheck_wall_clock_watchdog_clean_exit() {
  unset HANDOFF_RESTART_WALL_CLOCK_SECS
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  # If a watchdog sleep leaked, it would be visible as a `sleep 17357`
  # — we use a distinctive value to avoid collisions. The EXIT trap
  # must cancel the watchdog.
  if pgrep -f "sleep 17357" >/dev/null 2>&1; then
    pkill -KILL -f "sleep 17357" 2>/dev/null || true
    echo "  (leaked watchdog sleep process detected after clean exit)"
    return 1
  fi
  # T-1536: the cancel must be SILENT — the interrupted-sleep guard makes the
  # subshell exit quietly instead of racing into the FATAL branch (a clean
  # exit must never log a wall-clock FATAL or TERM the exiting script).
  if grep -q "wall-clock timeout" "$SBX/stderr"; then
    echo "  (spurious wall-clock FATAL on a clean exit — T-1536 race)"
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# T-2617: wall-clock watchdog deterministic reap-ordering test.
#
# The clean_exit test above only catches a leak if the real watchdog sleep
# happens to still be alive at the exact instant the postcheck runs — a race
# that needs genuine CPU-contention luck to hit (unreproducible across 660
# synthetic trials — CPU-spin load, fork/exec-churn load, and the real
# fakebin-sandboxed test under live multi-agent VPS contention — during T-2617
# investigation). This test makes the race DETERMINISTIC instead of
# load-dependent: a fake `sleep` stands in for the watchdog's own timer and,
# ONLY when invoked with the watchdog's distinctive budget, deliberately takes
# 1s past receiving SIGTERM before it actually exits — modeling "signalled but
# not yet scheduled to die," the real effect CPU contention has on
# signal-to-exit latency. Every OTHER sleep call in the SUT's own run (poll
# intervals, ack-wait, INJECT_ENTER_DELAY, kick settle, etc.) delegates
# straight through to the real /usr/bin/sleep, unaffected.
#
# Pre-fix, this failed 20/20 in isolated testing: the outer EXIT trap only
# `wait`s for the watchdog subshell, not the subshell's own sleep child, so
# the subshell can be reaped (it has no trap of its own and dies on SIGTERM
# regardless of its child's state) while the child is still mid-way through
# dying — orphaning it under init for up to its full budget. Post-fix (the
# subshell installs its own TERM trap that kills+reaps its sleep child before
# the subshell itself exits), this passes deterministically every run.
# ---------------------------------------------------------------------------
WALL_CLOCK_SLOW_DIE_MARKER="182460"  # distinctive; no other sleep call in the SUT or harness uses this value
preflight_wall_clock_watchdog_reaps_slow_child() {
  # Same setup as the happy-path test (bumper + idle pane text).
  preflight_happy
  export HANDOFF_RESTART_WALL_CLOCK_SECS="$WALL_CLOCK_SLOW_DIE_MARKER"
  cat > "$FAKEBIN/sleep" <<EOF
#!/bin/bash
# Fake sleep: only the watchdog's own timer (identified by the distinctive
# WALL_CLOCK_SLOW_DIE_MARKER budget) gets the slow-die treatment. Every other
# duration (poll intervals, ack-wait, INJECT_ENTER_DELAY, kick settle, etc.)
# delegates straight through to the real binary, unaffected.
if [[ "\$1" == "$WALL_CLOCK_SLOW_DIE_MARKER" ]]; then
  echo \$\$ > "$SBX/slow_die_pid"
  # On SIGTERM: kill the real inner sleep, then deliberately take 1s more
  # before actually exiting — the deterministic stand-in for "signalled but
  # not yet scheduled to die."
  trap 'kill "\$REAL_SLEEP_PID" 2>/dev/null; sleep 1; exit 143' TERM
  /usr/bin/sleep "\$1" &
  REAL_SLEEP_PID=\$!
  wait "\$REAL_SLEEP_PID"
else
  exec /usr/bin/sleep "\$@"
fi
EOF
  chmod +x "$FAKEBIN/sleep"
}
postcheck_wall_clock_watchdog_reaps_slow_child() {
  wait "$(cat "$SBX/bumper_pid")" 2>/dev/null || true
  unset HANDOFF_RESTART_WALL_CLOCK_SECS
  # The postcheck runs immediately after the SUT process exits. A correct
  # cleanup must have already reaped the slow-dying fake-sleep child by then
  # — if it hasn't (pre-fix), the recorded PID is still alive (it takes 1s
  # past receiving SIGTERM to actually exit; the outer trap's `wait`
  # returning before that is exactly the T-2617 defect).
  if [[ ! -f "$SBX/slow_die_pid" ]]; then
    echo "  (fake sleep never recorded a PID — the marker budget never ran; test fixture broken)"
    return 1
  fi
  local pid
  pid=$(cat "$SBX/slow_die_pid")
  if kill -0 "$pid" 2>/dev/null; then
    echo "  (T-2617 regression: watchdog's slow-dying child PID $pid still alive right after clean exit)"
    kill -KILL "$pid" 2>/dev/null || true
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Driver.
# ---------------------------------------------------------------------------

if [[ ! -f "$SUT" ]]; then
  echo "FATAL: SUT not found at $SUT"
  exit 1
fi

echo "Running handoff_and_restart.sh tests..."
echo

# ---------------------------------------------------------------------------
# T-1636: pane time-log staging + graveyard companion.
#
# Preflight seeds a fake timestamped pane log in the sandboxed PANE_LOG_DIR.
# The SUT must: stage it (rename *.log -> *.log.dying) BEFORE the restart, and
# after the (fake) restart gzip it into the sandboxed graveyard as
# <ts>-<uuid>.pane.log.gz — agent derived from the source filename (the
# sandbox handoff path is not /home/<user>/ shaped), consumed on success.
# ---------------------------------------------------------------------------
preflight_pane_log_companion() {
  # Ack bumper (same shape as preflight_happy — every ack-path case needs one).
  (
    wait_for_prompt_delivered "$SBX/stdout" || exit 0
    echo "$(($(cat "$SBX/handoff_mtime") + 100))" > "$SBX/handoff_mtime"
  ) &
  echo $! > "$SBX/bumper_pid"
  cat > "$SBX/pane_text" <<'EOF'
╭─────────────────────────────────────╮
│ ❯ Try "what's the weather"          │
╰─────────────────────────────────────╯
EOF
  printf '[2026-07-07 23:00:01] hello from the dying session\n' \
    > "$SBX/pane_logs/pane-timed-testagent.log"
}
postcheck_pane_log_companion() {
  # Staged + consumed: neither the live-name log nor the .dying remains.
  if compgen -G "$SBX/pane_logs/pane-timed-*.log" >/dev/null; then
    echo "  (live pane log not staged)"; return 1
  fi
  if compgen -G "$SBX/pane_logs/pane-timed-*.log.dying" >/dev/null; then
    echo "  (.dying staged file not consumed by companion archive)"; return 1
  fi
  # Archived: exactly one .pane.log.gz under the sandbox graveyard for testagent.
  local gz
  gz=$(compgen -G "$SBX/pantheon_state/graveyard/testagent/*/*.pane.log.gz" | head -1)
  if [[ -z "$gz" ]]; then
    echo "  (no .pane.log.gz companion in sandbox graveyard)"; return 1
  fi
  if ! zcat "$gz" | grep -q "hello from the dying session"; then
    echo "  (companion gz missing the staged content)"; return 1
  fi
  return 0
}

run_test happy 0 "${COMMON_ARGS[@]}"
run_test pane_log_companion 0 "${COMMON_ARGS[@]}"
run_test no_restart 0 "${COMMON_ARGS[@]}" --no-restart                       # T-1049 pause mode
run_test no_restart_skip_ack_rejected 64 "${COMMON_ARGS[@]}" --no-restart --skip-ack  # T-1049 mutual-exclusion
run_test lock_held 1 "${COMMON_ARGS[@]}"
run_test ack_timeout 4 "${COMMON_ARGS[@]}"
run_test restart_fail 5 "${COMMON_ARGS[@]}"
run_test isactive_fail 6 "${COMMON_ARGS[@]}"
# T-2658: control-plane branch (--supervisor control-plane). Later --service
# overrides COMMON_ARGS' claude.service (last-flag-wins arg parsing).
run_test control_plane_happy 0 "${COMMON_ARGS[@]}" \
  --service pantheon-persona-consume@specialist.service --supervisor control-plane
run_test control_plane_kill_survives 5 "${COMMON_ARGS[@]}" \
  --service pantheon-persona-consume@specialist.service --supervisor control-plane
run_test control_plane_scope_timeout 6 "${COMMON_ARGS[@]}" \
  --service pantheon-persona-consume@specialist.service --supervisor control-plane
run_test steady_timeout 7 "${COMMON_ARGS[@]}"
run_test custom_pattern 0 "${COMMON_ARGS[@]}" --verify-pattern "FRESH_HO_READY_TOKEN_42"
run_test dry_run 0 "${COMMON_ARGS[@]}" --dry-run
run_test missing_service 64 \
  --mode user --target-pane main:0.0 --ack-timeout 2 --steady-timeout 2
run_test bare_horizontal 0 "${COMMON_ARGS[@]}"
# T-0172 (A) sentinel-tightening + (B) post-step-7 kick:
run_test dialog_false_positive 7 "${COMMON_ARGS[@]}"
run_test boot_dialog_dismiss 0 "${COMMON_ARGS[@]}"           # T-0721
run_test boot_dialog_denylist_blocks 7 "${COMMON_ARGS[@]}"  # T-0721
run_test kick_invocation 0 "${COMMON_ARGS[@]}"
run_test no_kick 0 "${COMMON_ARGS[@]}" --no-kick
run_test custom_idle_sentinel 0 "${COMMON_ARGS[@]}" --idle-sentinel "CUSTOM_IDLE_TOKEN_99"
# T-0510 force-restart mode:
run_test skip_ack 0 "${COMMON_ARGS[@]}" --skip-ack

# T-2829: modal-wedge recovery for the graceful restart path.
run_test dismiss_modal_sends_escape 0 "${COMMON_ARGS[@]}" --dismiss-modal
run_test no_dismiss_modal_no_escape 0 "${COMMON_ARGS[@]}"
run_test hard_restart_on_ack_timeout 0 "${COMMON_ARGS[@]}" --ack-timeout 2 --hard-restart-on-ack-timeout
# T-0543 — stale IDLE_SENTINEL fix (Claude Code 2.1.x):
run_test idle_sentinel_default_v21x 0 "${COMMON_ARGS[@]}"
run_test idle_sentinel_old_default_would_fail 7 "${COMMON_ARGS[@]}" \
  --idle-sentinel '\? for shortcuts|ctrl\+t to hide tasks'
run_test busy_pane_no_false_pass 7 "${COMMON_ARGS[@]}"
# T-0528 — disabling DIALOG_EXCLUDE demonstrates the false-pass the new
# negative prong prevents. Same dialog fixture as case 11; without the
# exclusion the conjunction of positives matches → exit 0.
run_test dialog_exclude_disabled 0 "${COMMON_ARGS[@]}" --dialog-exclude 'NEVER_MATCH_THIS'
# T-0584 Layer 1 — wall-clock watchdog. The fires test hangs send-keys 60s;
# the watchdog's pkill -P kills the hung tmux, the script then naturally
# fails step 3 (send-keys error) and exits 3 — that's the clean recovery
# path (proves the wedged child is reaped before the watchdog's own SIGTERM
# to the script PID lands). The postcheck additionally asserts the FATAL
# log line + that the hung tmux child is reaped.
# The clean-exit test confirms the EXIT trap reaps the watchdog so a healthy
# run leaves no leaked sleep process.
run_test wall_clock_watchdog_fires 3 "${COMMON_ARGS[@]}"
run_test wall_clock_watchdog_clean_exit 0 "${COMMON_ARGS[@]}"
# T-2617: deterministic (not load-dependent) regression guard for the
# watchdog-child orphan race — see the preflight/postcheck above.
run_test wall_clock_watchdog_reaps_slow_child 0 "${COMMON_ARGS[@]}"
# T-0584 Layer 0 — PID-change assertion defends against the legacy out-of-
# cgroup scenario where `systemctl restart` silently no-ops the inner claude.
run_test pid_change_verified 0 "${COMMON_ARGS[@]}"
run_test pid_change_unchanged_fails 7 "${COMMON_ARGS[@]}"
run_test pid_change_skipped_when_no_baseline 0 "${COMMON_ARGS[@]}"
# T-0590 (a) — kick-readiness settle closes the silent-kick-drop window.
run_test kick_readiness_settle 0 "${COMMON_ARGS[@]}"
run_test kick_drops_refires 8 "${COMMON_ARGS[@]}"          # T-0899: dropped kick → bounded re-fire → exit 8
# T-0584 Layer 2 — stale-flock takeover. Pass the shared lockfile path
# defined alongside the preflights so the SUT operates on it.
run_test stale_flock_takeover 0 "${COMMON_ARGS[@]}" --lockfile "$STALE_LOCKFILE_PATH"
run_test stale_flock_below_threshold_skipped 0 "${COMMON_ARGS[@]}" --lockfile "$STALE_LOCKFILE_PATH"

echo
echo "Results: $PASS passed, $FAIL failed"
if [[ "$FAIL" -gt 0 ]]; then
  echo "Failed tests: ${FAILED_TESTS[*]}"
  exit 1
fi
exit 0
