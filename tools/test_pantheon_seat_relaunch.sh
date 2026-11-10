#!/bin/bash
# Test harness for tools/pantheon-seat-relaunch.sh (T-3838).
#
# Strategy (per T-0004 meta-validation lesson): build a sandbox per case with
# fake `tmux`, `systemctl`, `systemd-run`, and `flock` on PATH, each modelling
# the real tool's contract, plus a fake /proc for the launch capture. Assert on
# exit code + recorded side effects. Hermetic: no live seat, no real systemd.
#
# The full happy path uses --skip-ack so we do not have to model
# send_keys_submit's paste-mode protocol (covered by test_send_keys_submit.sh +
# test_handoff_and_restart.sh); the ack path's mtime logic is identical to
# handoff_and_restart.sh's and tested there.
#
# Invocation: bash tools/test_pantheon_seat_relaunch.sh
# Exit: 0 = all pass; 1 = at least one failure.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUT="${SCRIPT_DIR}/pantheon-seat-relaunch.sh"

# T-4213: precondition. The T-4141 re-press predicate (submit_verify.should_repress)
# lazily imports inbox_consumer, which imports the `tools` package from this dir's
# PARENT. From a copy NOT named tools/ (e.g. a scratch mutation copy), that import
# fails, the predicate fails safe to "don't press", and the re-press case times out
# (exit 4). That once read as a CPU-load flake. Refuse up front and name the cause.
if ! (cd / && SV_DIR="$SCRIPT_DIR" python3 -c 'import os, sys; sys.path.insert(0, os.environ["SV_DIR"]); import submit_verify; submit_verify._inbox_consumer()') >/dev/null 2>&1; then
  echo "PRECONDITION FAILED: $SCRIPT_DIR is not importable as the 'tools' package, so submit_verify cannot load its pane classifier (inbox_consumer) and the T-4141 re-press case would fail. Run this suite from a pantheon-config checkout, or copy it into a directory named tools/." >&2
  exit 1
fi
PASS=0
FAIL=0
FAILED_TESTS=()

# write_instance <model|null> [effort]: role config for BOTH seats (mason=-1, artisan=-2).
write_instance() {
  local model="$1" effort="${2:-}" role
  {
    echo "harness_runtime:"
    echo "  roles:"
    for role in engineering-orchestrator-1 engineering-orchestrator-2; do
      echo "    $role:"
      echo "      claude:"
      echo "        model: $model"
      echo "        native:"
      echo "          permission_mode: bypassPermissions"
      [[ -n "$effort" ]] && echo "          effort: $effort"
    done
  } > "$SBX/instance.yaml"
}

setup_sandbox() {
  SBX=$(mktemp -d -t seat_relaunch_test.XXXXXX)
  FAKEBIN="$SBX/bin"
  mkdir -p "$FAKEBIN" "$SBX/proc/4242"
  # Default captured launch argv (NUL-delimited) — the interactive claude.
  printf '/home/suborch1/.npm-global/bin/claude\0--model\0claude-opus-4-8\0--permission-mode\0bypassPermissions\0' \
    > "$SBX/proc/4242/cmdline"
  # T-4085: sandbox instance.yaml. Default declares the SAME model as the captured
  # argv, so every pre-T-4085 case replays byte-identical argv.
  write_instance claude-opus-4-8
  export PANTHEON_SEAT_RELAUNCH_INSTANCE_YAML="$SBX/instance.yaml"
  # T-4201: the last-good launch snapshot lands here, never in the runner's real state dir.
  export PANTHEON_STATE_DIR="$SBX/state"
  # T-5016: the session-end Chronicle duty (step 5b) renames the seat's pane log and
  # may alert Recovery: keep both inside the sandbox.
  export PANE_LOG_DIR="$SBX/pane_logs"
  export PANTHEON_PERSONA_STATE_DIR="$SBX/state/personas"
  export PANTHEON_SESSION_CHRONICLE_ALERT=off
  mkdir -p "$SBX/pane_logs"
  echo "alive"  > "$SBX/session_state"     # tmux has-session reads this
  echo "1"      > "$SBX/consume_inactive"  # 1 => is-active exits nonzero (inactive)
  echo "0"      > "$SBX/consume_activating_polls"  # T-3846: N polls of "activating" before a consume drains (0 => drained)
  echo "0"      > "$SBX/lock_held"         # 1 => flock -n fails
  echo "1000"   > "$SBX/mtime"
  # Default pane: at an idle prompt (verify-pattern + idle-sentinel, no dialog).
  printf '╭──────────────────────────────╮\n│ ready                        │\n╰──────────────────────────────╯\n ❯ \n' \
    > "$SBX/pane_text"
  # T-4088: once the resume kick is pasted, the pane shows a dispatched turn (the
  # 'esc to interrupt' contract kick_verify.py polls for). A test removes
  # pane_text_busy to model a kick that DROPS (session stays idle).
  printf ' ❯ \n  ⏸ manual mode on · esc to interrupt\n' > "$SBX/pane_text_busy"
  echo "Your session was just cycled to reclaim context" > "$SBX/kick_busy_token"

  cat > "$FAKEBIN/tmux" <<EOF
#!/bin/bash
SBX="$SBX"
args="\$*"
case "\$args" in
  *"#{pane_pid}"*)          echo 4242 ;;
  *"#{pane_current_path}"*) echo /home/suborch1/pantheon-config ;;
  *"#{pane_in_mode}"*)      echo 0 ;;   # send_keys_submit/kick_verify trust capture only when not in a mode
  *"list-panes"*)           cat "\$SBX/pane_dead" 2>/dev/null || echo 0 ;;   # T-4201: one line per pane (line 1 = the target)
  *"#{pane_dead}"*)         head -1 "\$SBX/pane_dead" 2>/dev/null || echo 0 ;;   # T-4171: respawned pane died at startup (target pane only)
  *"set-option"*)           echo "set-option \$args" >> "\$SBX/tmux_setopt_log" ;;
  *"kill-server"*)          echo dead > "\$SBX/session_state"
                            rm -f "\$SBX/pane_dead"   # T-4201: a dead pane goes with its server
                            echo "KILL-SERVER" >> "\$SBX/events_log"
                            # T-4171: the pane's harness exits on the server's SIGHUP -- unless the
                            # case models one that outlives its tmux server (harness_lingers).
                            [[ -f "\$SBX/harness_lingers" ]] || rm -rf "\$SBX/proc/4242"
                            # T-4117: a fresh session comes up idle (replaces a pending-paste pane)
                            [[ -f "\$SBX/pane_text_fresh" ]] && mv "\$SBX/pane_text_fresh" "\$SBX/pane_text" ;;
  *"has-session"*)          [[ "\$(cat "\$SBX/session_state")" == "alive" ]] && exit 0 || exit 1 ;;
  *"new-session"*)          echo alive > "\$SBX/session_state" ;;   # reached if systemd-run execs its args
  *"load-buffer"*)          cat > "\$SBX/tmux_buffer" ;;           # send_keys_submit: text arrives on stdin
  *"paste-buffer"*)
    text="\$(cat "\$SBX/tmux_buffer" 2>/dev/null)"
    { echo "--- pasted:"; echo "\$text"; } >> "\$SBX/paste_log"
    echo "PASTE \${text:0:60}" >> "\$SBX/events_log"   # shared ordering log (kick vs unfence)
    # The handoff prompt landing models the seat writing its handoff (mtime ack).
    # T-4117: no_ack models a seat that never writes it (queued prompt, turn never ends).
    [[ "\$text" == *"[ctxfill graceful relaunch]"* && ! -f "\$SBX/no_ack" ]] && echo 2000 > "\$SBX/mtime"
    grep -qF "\$(cat "\$SBX/kick_busy_token")" <<<"\$text" && echo 1 > "\$SBX/kick_sent"
    # T-4832 r1: a boot-dialog dismiss digit; like real tmux the dialog stays until the Enter.
    [[ "\$text" == "1" || "\$text" == "2" ]] && echo 1 > "\$SBX/boot_digit_pasted"
    ;;
  *"capture-pane"*)
    if [[ -f "\$SBX/kick_sent" && -f "\$SBX/pane_text_busy" ]]; then cat "\$SBX/pane_text_busy"
    else cat "\$SBX/pane_text"; fi ;;
  *"send-keys"*)            echo "send-keys \$args" >> "\$SBX/tmux_sendkeys_log"
                            if [[ -f "\$SBX/boot_digit_pasted" && -f "\$SBX/pane_text_boot_after" && "\$args" == *Enter* ]]; then
                              mv "\$SBX/pane_text_boot_after" "\$SBX/pane_text"; fi
                            # T-4141: ack_on_repress models a stuck composer that only submits on
                            # the RE-PRESS: send_keys_submit sends 3 Enters (1 + SUBMIT_RETRIES=2),
                            # so the 4th Enter is the ack wait's re-press.
                            if [[ -f "\$SBX/ack_on_repress" && "\$args" == *Enter* ]] && \
                               (( \$(grep -c Enter "\$SBX/tmux_sendkeys_log") >= 4 )); then echo 2000 > "\$SBX/mtime"; fi ;;
  *)                        : ;;
esac
exit 0
EOF

  cat > "$FAKEBIN/systemctl" <<EOF
#!/bin/bash
SBX="$SBX"
echo "systemctl \$*" >> "\$SBX/systemctl_log"
echo "systemctl \$*" >> "\$SBX/events_log"
if [[ "\$*" == *"is-active"* ]]; then
  if [[ "\$*" == *".scope"* ]]; then echo inactive; exit 3; fi   # old scope: stopped/gone
  if [[ "\$(cat "\$SBX/consume_inactive")" == "1" ]]; then echo inactive; exit 3; fi
  echo active; exit 0
fi
# T-3846: the fence drains an in-flight consume via \`show -p ActiveState --value\`.
# persona-consume@ is Type=oneshot => a running instance is "activating", never
# "active". Model a consume that drains after N polls: consume_activating_polls>0
# echoes "activating" and decrements; 0 echoes "inactive" (drained).
if [[ "\$*" == *"show"* && "\$*" == *"ActiveState"* ]]; then
  if [[ "\$*" == *".scope"* ]]; then echo inactive; exit 0; fi
  n="\$(cat "\$SBX/consume_activating_polls" 2>/dev/null || echo 0)"
  if [[ "\$n" -gt 0 ]]; then echo "\$((n-1))" > "\$SBX/consume_activating_polls"; echo activating; exit 0; fi
  echo inactive; exit 0
fi
exit 0
EOF

  cat > "$FAKEBIN/systemd-run" <<EOF
#!/bin/bash
SBX="$SBX"
echo "systemd-run \$*" >> "\$SBX/systemd_run_log"
# T-3846: detect whether the flock fd (9) leaked into the respawn. The script must
# close it (\`9>&-\` on the respawn) so the respawned tmux server + claude do NOT
# inherit the relaunch lock (else the seat's NEXT relaunch deadlocks on a lock held
# by the very session it wants to relaunch).
if { true >&9; } 2>/dev/null; then echo "FD9_OPEN" >> "\$SBX/systemd_run_log"; else echo "FD9_CLOSED" >> "\$SBX/systemd_run_log"; fi
echo alive > "\$SBX/session_state"   # the respawn brings the session live
echo "RESPAWN" >> "\$SBX/events_log"
[[ -f "\$SBX/new_pane_dies" ]] && echo 1 > "\$SBX/pane_dead"   # T-4171: harness exits at startup
exit 0
EOF

  # T-4171: external kill (the script signals via \`env kill\`); the lingering harness
  # exits on SIGTERM unless the case models one that ignores it.
  cat > "$FAKEBIN/kill" <<EOF
#!/bin/bash
SBX="$SBX"
echo "kill \$*" >> "\$SBX/kill_log"
echo "KILL \$*" >> "\$SBX/events_log"
[[ -f "\$SBX/harness_ignores_term" ]] || rm -rf "\$SBX/proc/4242"
exit 0
EOF

  cat > "$FAKEBIN/flock" <<EOF
#!/bin/bash
SBX="$SBX"
[[ "\$(cat "\$SBX/lock_held")" == "1" ]] && exit 1
exit 0
EOF

  cat > "$FAKEBIN/stat" <<EOF
#!/bin/bash
SBX="$SBX"
cat "\$SBX/mtime"
EOF

  chmod +x "$FAKEBIN"/*
}

# T-4230: FAIL-FAST. The positive ack windows are wide condition polls (60s): free
# while the SUT is healthy, slow only when it is broken, and a broken SUT fails every
# case. Stop once SUITE_MAX_FAILED_CASES sandboxes (= cases; a case is
# setup_sandbox..teardown_sandbox, and one case can fail several assertions) have
# failed, so a broken SUT goes red in a few minutes with the cause named instead of
# grinding to the CI job timeout as an unattributed red. One flaky case still just
# fails that case (1 < 3). Set SUITE_MAX_FAILED_CASES=999 to see every failure.
# (Duplicated, not shared, in test_handoff_and_restart.sh: each suite stays
# copy-and-runnable on its own.)
SUITE_MAX_FAILED_CASES="${SUITE_MAX_FAILED_CASES:-3}"
_FAILED_CASES=0
_FAIL_AT_LAST_CASE_END=0
suite_case_end() {
  if (( FAIL > _FAIL_AT_LAST_CASE_END )); then _FAILED_CASES=$((_FAILED_CASES+1)); fi
  _FAIL_AT_LAST_CASE_END=$FAIL
  if (( _FAILED_CASES >= SUITE_MAX_FAILED_CASES )); then
    echo
    echo "aborting after $_FAILED_CASES failures (fail-fast, T-4230): the SUT looks broken, not slow."
    echo "Failed so far:"
    printf '  - %s\n' "${FAILED_TESTS[@]}"
    echo "pantheon-seat-relaunch: $PASS passed, $FAIL failed (suite ABORTED, not completed)"
    exit 1
  fi
}

teardown_sandbox() { rm -rf "$SBX"; suite_case_end; }

# T-4230: the suite-wide default for --poll-interval. The SUT's own default is 2s
# and it sleeps that long, unconditionally, once per run right after `tmux
# kill-server` (a settle before it checks the session is gone) and in every poll
# loop: 43 of those in this suite, ~86s of the ~235s run. The fakes are
# synchronous (kill-server flips session_state at once), so nothing here waits on
# real time and the sleep only pads the suite. Verified that no case asserts a
# poll count or elapsed time that depends on the 2s: every log assertion is on
# WHICH step ran, and the five cases that care about the cadence (the ack-wait
# ones) pass their own --poll-interval, which wins because it comes AFTER this
# default (last flag wins). Production keeps its 2s; only the suite is faster.
SUITE_POLL_INTERVAL=0.2

# run_sut <expected_exit> <label> -- <args...>
run_sut() {
  local expected="$1" label="$2"; shift 2; [[ "$1" == "--" ]] && shift
  local out rc
  # Sandbox-scoped lockfile so tests never create the script-default
  # /tmp/pantheon-seat-relaunch.<uid>.lock as the test-runner account — that file
  # is then unwritable by the seat account on a later real run (the exact
  # cross-account lock collision the script's per-lockfile design warns about).
  out=$(PATH="$FAKEBIN:$PATH" PANTHEON_SEAT_RELAUNCH_PROC_ROOT="$SBX/proc" \
        bash "$SUT" --poll-interval "$SUITE_POLL_INTERVAL" "$@" --lockfile "$SBX/relaunch.lock" 2>&1)
  rc=$?
  LAST_OUT="$out"
  # T-4230: one progress line per case. This suite is otherwise silent on success,
  # and tools/test_shell_suites.py kills a suite that emits NO output for 300s (a
  # no-progress watchdog, not a total-wall cap): without this line a healthy but
  # slow run of this ~60-case suite would look identical to a hang.
  echo "  ran: $label (exit $rc, expected $expected)"
  if [[ "$rc" == "$expected" ]]; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_TESTS+=("$label (exit $rc != $expected)")
    echo "FAIL: $label — exit $rc, expected $expected"
    echo "----- output -----"; echo "$out"; echo "------------------"
  fi
}

assert_contains() {  # <needle> <label>
  if [[ "$LAST_OUT" == *"$1"* ]] || grep -Fq "$1" "$SBX"/*_log 2>/dev/null; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_TESTS+=("$2 (missing: $1)")
    echo "FAIL: $2 — expected to find: $1"
  fi
}
assert_logged() {  # <logfile> <needle> <label>
  if grep -Fq "$2" "$SBX/$1" 2>/dev/null; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_TESTS+=("$3 (not in $1: $2)")
    echo "FAIL: $3 — expected in $1: $2"
  fi
}
assert_not_logged() {  # <logfile> <needle> <label>
  if [[ ! -f "$SBX/$1" ]] || ! grep -Fq "$2" "$SBX/$1" 2>/dev/null; then
    PASS=$((PASS+1))
  else
    FAIL=$((FAIL+1)); FAILED_TESTS+=("$3 (unexpected in $1: $2)")
    echo "FAIL: $3 — did NOT expect in $1: $2"
  fi
}

# T-4088: fast resume-kick loop for the suite (no settle, short poll, 2 attempts)
# and no paste-mode settle in send_keys_submit.
export PANTHEON_SEAT_RELAUNCH_KICK_SETTLE_SECS=0
export PANTHEON_SEAT_RELAUNCH_KICK_POLL_SECS=1
export PANTHEON_SEAT_RELAUNCH_KICK_POLL_INTERVAL=0.1
export PANTHEON_SEAT_RELAUNCH_KICK_MAX_ATTEMPTS=2
# T-4456: kick render gate off for the suite (the fake pane never renders a paste,
# so the gate would wait its full bound); the T-4456 cases below pin it.
export PANTHEON_SEAT_RELAUNCH_KICK_RENDER_WAIT=0
export INJECT_ENTER_DELAY=0
export PANTHEON_SEAT_RELAUNCH_OLD_HARNESS_EXIT_SECS=1   # T-4171: short harness-exit bound

REQ=(--persona mason --account suborch1 --uid 1007
     --session suborch1-main --target-pane suborch1-main:0.0
     --scope-unit pantheon-mason-seat.scope
     --handoff-file /home/suborch1/HANDOFF.md)

# --- usage guards ----------------------------------------------------------
setup_sandbox
run_sut 64 "missing required arg" -- --persona mason
teardown_sandbox

setup_sandbox
run_sut 64 "bad scope-unit rejected" -- "${REQ[@]/pantheon-mason-seat.scope/not-a-seat.scope}"
teardown_sandbox

setup_sandbox
run_sut 64 "bad consume-timer rejected" -- "${REQ[@]}" --consume-timer pantheon-persona-consume@mason.service
run_sut 64 "non-integer --queued-ack-timeout rejected" -- "${REQ[@]}" --queued-ack-timeout 4s
teardown_sandbox

# --- capture ---------------------------------------------------------------
setup_sandbox
: > "$SBX/proc/4242/cmdline"   # empty argv
run_sut 2 "empty captured argv fails" -- "${REQ[@]}" --dry-run
teardown_sandbox

setup_sandbox
run_sut 0 "dry-run captures + prints plan" -- "${REQ[@]}" \
  --consume-timer pantheon-persona-consume@mason.timer --dry-run
assert_contains "systemd-run --user --scope --unit=pantheon-mason-seat.scope" "dry-run respawn shape"
assert_contains "PANTHEON_TMUX_SESSION=suborch1-main" "dry-run env reconstruction"
assert_contains "/home/suborch1/.npm-global/bin/claude --model claude-opus-4-8 --permission-mode bypassPermissions" "dry-run captured argv"
teardown_sandbox

# --- T-4533: fleet-usage mod env rides the respawn; helper failure fails open ----
setup_sandbox
mkdir -p "$SBX/clone/tools/mods/fleet-usage"
export PANTHEON_SEAT_RELAUNCH_CLONE_ROOT="$SBX/clone"
run_sut 0 "T-4533 dry-run carries fleet-usage env" -- "${REQ[@]}" --dry-run
assert_contains "PANTHEON_FLEET_USAGE_SEAT=mason" "dry-run injects the persona as the fleet-usage seat"
export PANTHEON_SEAT_RELAUNCH_CLONE_ROOT="$SBX/no-such-clone"
run_sut 0 "T-4533 helper yielding nothing still respawns (fail open)" -- "${REQ[@]}" --dry-run
assert_contains "PANTHEON_TMUX_SESSION=suborch1-main" "respawn plan intact without the fleet-usage keys"
export PANTHEON_SEAT_RELAUNCH_CLONE_ROOT="$SBX/clone"
run_sut 0 "T-4533 real respawn carries fleet-usage env" -- "${REQ[@]}" --skip-ack
assert_logged systemd_run_log "PANTHEON_FLEET_USAGE_SEAT=mason" "respawn argv carries the fleet-usage seat"
assert_not_logged systemd_run_log "CLAUDE_CODE_PLUGIN_DIRS" "T-4643 step 4: respawn never injects the mod sideload"
unset PANTHEON_SEAT_RELAUNCH_CLONE_ROOT
teardown_sandbox

# --- fence DRAINS an in-flight consume, THEN proceeds (T-3846) --------------
# persona-consume@ is Type=oneshot => a running run is ActiveState=activating,
# NEVER "active". The old `is-active`-only check missed it, so a consume the timer
# fired in the same instant the fence masked the timer ran its controller AFTER our
# respawn and blanked runtime.json (artisan 2026-09-21). The fence now WAITS for the
# in-flight run to drain (harmless — it validates the still-live OLD session), THEN
# kill+respawn.
setup_sandbox
echo "1" > "$SBX/consume_activating_polls"   # consume "activating" for one poll, then drains
export FENCE_DRAIN_POLL=1
run_sut 0 "fence DRAINS an in-flight consume then proceeds" -- "${REQ[@]}" \
  --consume-timer pantheon-persona-consume@mason.timer --skip-ack
unset FENCE_DRAIN_POLL
# NON-vacuous: prove the DRAIN path actually executed (the old is-active-only check
# would have proceeded WITHOUT draining — this log line is emitted only by the new
# drain loop when it observes an "activating" oneshot consume).
if [[ "$LAST_OUT" == *"draining before respawn"* ]]; then PASS=$((PASS+1));
else FAIL=$((FAIL+1)); FAILED_TESTS+=("drain path did not execute (no 'draining' log)"); echo "FAIL: drain path did not execute — expected a 'draining before respawn' log line"; fi
assert_logged systemd_run_log "new-session" "respawn happens AFTER the in-flight consume drains"
assert_logged systemctl_log "start pantheon-persona-consume@mason.timer" "consume unfenced (restarted) after drain-then-proceed"
teardown_sandbox

# --- T-5016: the relaunch archives the ended session + re-arms the pane-log tap ---
setup_sandbox
printf '[2026-10-14 20:00:01] last words of the old session\n' > "$SBX/pane_logs/pane-timed-mason.log"
run_sut 0 "T-5016 relaunch archives the ended session" -- "${REQ[@]}" --skip-ack
if grep -q "| session-end/ctxfill-relaunch |" "$SBX/state/chronicle/mason/index.md" 2>/dev/null; then PASS=$((PASS+1));
else FAIL=$((FAIL+1)); FAILED_TESTS+=("T-5016: no session-end/ctxfill-relaunch index line"); echo "FAIL: T-5016 — no session-end/ctxfill-relaunch index line"; fi
if compgen -G "$SBX/state/chronicle/mason/*/*.pane.log.gz" >/dev/null && [[ ! -e "$SBX/pane_logs/pane-timed-mason.log" ]]; then PASS=$((PASS+1));
else FAIL=$((FAIL+1)); FAILED_TESTS+=("T-5016: pane log not staged + archived"); echo "FAIL: T-5016 — pane log not staged + archived"; fi
assert_contains "[arm-pane-log]" "T-5016: the pane-log tap is re-armed after the respawn"
assert_contains "session-chronicle:" "T-5016: the duty's outcome is logged"
teardown_sandbox

# --- fence ABORTS if an in-flight consume won't drain in the bound (exit 9) ---
setup_sandbox
echo "999" > "$SBX/consume_activating_polls"  # consume stuck "activating" (never drains)
export FENCE_DRAIN_TIMEOUT=0                   # 0 => abort on first still-activating poll
run_sut 9 "fence-abort when in-flight consume won't drain (exit 9, distinct from kill-survivor)" -- "${REQ[@]}" \
  --consume-timer pantheon-persona-consume@mason.timer --skip-ack
unset FENCE_DRAIN_TIMEOUT
# NON-vacuous: kill-server writes session_state=dead, so "alive" proves the session was never touched.
if [[ "$(cat "$SBX/session_state")" == "alive" ]]; then PASS=$((PASS+1));
else FAIL=$((FAIL+1)); FAILED_TESTS+=("stuck-consume abort touched the session"); echo "FAIL: stuck-consume abort touched the session"; fi
# masks FIRST, then the drain-timeout abort UNFENCES (restores) before returning — net no-op, nothing left masked.
assert_logged systemctl_log "unmask pantheon-persona-consume@mason.service" "stuck-consume abort restores (unmasks) — nothing left masked"
assert_not_logged systemd_run_log "new-session" "no respawn after stuck-consume abort"
teardown_sandbox

# --- happy path (skip-ack) -------------------------------------------------
setup_sandbox
run_sut 0 "happy path relaunch (skip-ack, with fence)" -- "${REQ[@]}" \
  --consume-timer pantheon-persona-consume@mason.timer --skip-ack
assert_logged systemctl_log "mask pantheon-persona-consume@mason.service" "fence: masked service (blocks ALL activation)"
assert_logged systemctl_log "mask pantheon-persona-consume@mason.timer" "fence: masked timer"
assert_logged systemctl_log "stop pantheon-mason-seat.scope" "old scope torn down before respawn (GC-race guard)"
assert_logged systemd_run_log "new-session -d -s suborch1-main -n harness" "respawn issued"
assert_logged systemd_run_log "FD9_CLOSED" "respawn does NOT inherit the flock fd (no lock-leak into the new session — T-3846)"
assert_logged systemctl_log "unmask pantheon-persona-consume@mason.service" "unfence: unmasked service"
assert_logged systemctl_log "unmask pantheon-persona-consume@mason.timer" "unfence: unmasked timer"
assert_logged systemctl_log "start pantheon-persona-consume@mason.timer" "unfence: restarted timer"
teardown_sandbox

# --- happy path with NO fence (no --consume-timer) -------------------------
setup_sandbox
run_sut 0 "happy path without fence" -- "${REQ[@]}" --skip-ack
assert_not_logged systemctl_log "mask" "no fence without --consume-timer"
assert_logged systemd_run_log "new-session" "respawn still issued without fence"
teardown_sandbox

# --- kill-server survivor => exit 5 ----------------------------------------
setup_sandbox
# tmux kill-server writes dead, but a broken teardown leaves it alive: override.
cat > "$FAKEBIN/tmux" <<EOF
#!/bin/bash
case "\$*" in
  *"#{pane_pid}"*)          echo 4242 ;;
  *"#{pane_current_path}"*) echo /home/suborch1/pantheon-config ;;
  *"has-session"*)          exit 0 ;;   # ALWAYS alive => survivor
  *)                        : ;;
esac
exit 0
EOF
chmod +x "$FAKEBIN/tmux"
run_sut 5 "kill-server survivor refuses respawn" -- "${REQ[@]}" --skip-ack
assert_not_logged systemd_run_log "new-session" "no respawn into a surviving session"
teardown_sandbox

# --- steady-state sentinel fail => exit 7, fence left up -------------------
setup_sandbox
echo "no idle prompt here" > "$SBX/pane_text"   # sentinel never matches
# respawn brings session alive so we reach step 7; shrink the steady wait.
run_sut 7 "sentinel-fail exits 7 and leaves fence up" -- "${REQ[@]}" \
  --consume-timer pantheon-persona-consume@mason.timer --skip-ack --steady-timeout 1 --poll-interval 1
assert_logged systemctl_log "mask pantheon-persona-consume@mason.timer" "fence masked before failure"
assert_not_logged systemctl_log "unmask" "fence LEFT UP on sentinel failure (fail-safe)"
teardown_sandbox

# --- respawn failure => exit 6, fence LEFT UP + reconcile hint -------------
setup_sandbox
cat > "$FAKEBIN/systemd-run" <<EOF
#!/bin/bash
echo "systemd-run \$*" >> "$SBX/systemd_run_log"
exit 1
EOF
chmod +x "$FAKEBIN/systemd-run"
run_sut 6 "respawn failure exits 6, fence left up (fail-safe)" -- "${REQ[@]}" \
  --consume-timer pantheon-persona-consume@mason.timer --skip-ack
assert_logged systemctl_log "mask pantheon-persona-consume@mason.service" "fence masked before failed respawn"
assert_not_logged systemctl_log "unmask" "exit 6 leaves the DEAD seat's fence UP (fail-safe)"
assert_contains "Reconcile after fixing mason" "exit 6 prints the reconcile command"
teardown_sandbox

# --- happy path under LC_ALL=C — guards the grouped-VERIFY_PATTERN regression (#1) ---
# A bare (ungrouped) ─+/─{20,} binds the quantifier to the trailing byte under a
# byte locale and never matches box-drawing; the SUT runs via sudo env_reset, so
# this pins the failure the UTF-8 dev box would otherwise hide.
setup_sandbox
_LC_SAVE="${LC_ALL:-__unset__}"
export LC_ALL=C
run_sut 0 "happy path under LC_ALL=C (grouped VERIFY_PATTERN matches box-drawing)" -- "${REQ[@]}" --skip-ack
if [[ "$_LC_SAVE" == "__unset__" ]]; then unset LC_ALL; else export LC_ALL="$_LC_SAVE"; fi
teardown_sandbox

# ---------------------------------------------------------------------------
# T-3937: echo shared-cargo-target durability. The respawn of an ECHO-BUILDING
# seat (artisan) is wrapped so cargo writes to the shared CARGO_TARGET_DIR with
# umask 002; a NON-echo seat (mason) is left byte-identical. The wrapper MUST
# `exec` claude in place so the pane foreground image that capture_launch reads on
# the NEXT relaunch stays the bare claude argv (no wrapper accumulation) — the
# fleet-critical capture-respawn safety property (ticket note @04f8138fa).
# ---------------------------------------------------------------------------
ARTISAN_REQ=(--persona artisan --account suborch2 --uid 1008
     --session suborch2-main --target-pane suborch2-main:0.0
     --scope-unit pantheon-artisan-seat.scope
     --handoff-file /home/suborch2/HANDOFF.md)

# artisan: shared-cargo-target wrapper is injected into the respawn.
setup_sandbox
run_sut 0 "artisan relaunch injects shared cargo-target wrapper" -- "${ARTISAN_REQ[@]}" --skip-ack
assert_logged systemd_run_log "/var/lib/pantheon/echo-target" "artisan respawn passes the shared CARGO_TARGET_DIR"
assert_logged systemd_run_log "umask 002" "artisan respawn sets umask 002 (group-writable cache artifacts)"
assert_logged systemd_run_log 'exec "$@"' "artisan wrapper execs claude in place (capture-safe: no wrapper accumulation)"
assert_logged systemd_run_log "new-session -d -s suborch2-main" "artisan respawn still issued with the correct session"
teardown_sandbox

# artisan: dry-run plan surfaces the wrapper (the planning/echo block must match the real respawn).
setup_sandbox
run_sut 0 "artisan dry-run plan shows the cargo wrapper" -- "${ARTISAN_REQ[@]}" --dry-run
assert_contains "umask 002" "artisan dry-run plan includes umask"
assert_contains "/var/lib/pantheon/echo-target" "artisan dry-run plan includes the shared target dir"
teardown_sandbox

# mason: NON-echo seat is NOT wrapped (scoping) — path byte-identical to pre-T-3937.
setup_sandbox
run_sut 0 "mason relaunch is NOT wrapped (non-echo-builder scoping)" -- "${REQ[@]}" --skip-ack
assert_not_logged systemd_run_log "/var/lib/pantheon/echo-target" "mason respawn carries NO shared cargo-target"
assert_not_logged systemd_run_log "umask 002" "mason respawn carries NO umask wrapper"
assert_logged systemd_run_log "new-session -d -s suborch1-main" "mason respawn still issued (unchanged)"
teardown_sandbox

# ---------------------------------------------------------------------------
# T-4088: post-verify resume KICK. A fresh claude boots idle and never reads the
# handoff unless prompted; an idle session also writes no jsonl, so the ctxfill
# monitor keeps measuring the predecessor's full transcript. The relaunch now
# delivers a dispatch-verified resume prompt (kick_verify.py) after step 7.
# ---------------------------------------------------------------------------

# T-4456: the resume kick is render-gated (fresh TUI, T-4362/T-4455). The env value
# reaches send_keys_submit's own gate (its stderr names the bound; the fake pane
# never renders the paste), the kick still dispatch-verifies, and the production
# default is 8s (that case waits ~8s once, for the same reason).
setup_sandbox
export PANTHEON_SEAT_RELAUNCH_KICK_RENDER_WAIT=0.2
run_sut 0 "resume kick is render-gated (env plumbed)" -- "${REQ[@]}" \
  --consume-timer pantheon-persona-consume@mason.timer --skip-ack
assert_contains "render_wait=0.2s" "kick_verify got the render wait"
assert_contains "render wait 0.2s" "send_keys_submit's render gate ran"
assert_contains "step7b: resume kick delivered + dispatch-verified" "gated kick still dispatch-verifies"
teardown_sandbox
setup_sandbox
unset PANTHEON_SEAT_RELAUNCH_KICK_RENDER_WAIT
run_sut 0 "resume kick render gate: production default" -- "${REQ[@]}" \
  --consume-timer pantheon-persona-consume@mason.timer --skip-ack
assert_contains "render_wait=8s" "production kick render gate is 8s"
teardown_sandbox
export PANTHEON_SEAT_RELAUNCH_KICK_RENDER_WAIT=0

# Kick delivered + dispatch-verified; skip-ack => the prompt warns the handoff may be stale.
setup_sandbox
run_sut 0 "resume kick delivered + dispatch-verified after relaunch" -- "${REQ[@]}" \
  --consume-timer pantheon-persona-consume@mason.timer --skip-ack
assert_contains "step7b: resume kick delivered + dispatch-verified" "kick verified as a dispatched turn"
assert_logged paste_log "Read /home/suborch1/HANDOFF.md" "kick names the seat's handoff file"
assert_logged paste_log "drain your mailbox" "kick tells a mailbox-first seat to drain mail"
assert_logged paste_log "may predate this relaunch" "skip-ack kick warns the handoff may be stale"
assert_logged systemctl_log "start pantheon-persona-consume@mason.timer" "consume unfenced after a kicked relaunch"
# ORDER: the kick lands while the consume fence is still up (before the unmask).
kick_line=$(grep -n "PASTE \[ctxfill relaunch" "$SBX/events_log" | head -1 | cut -d: -f1)
unfence_line=$(grep -n "unmask pantheon-persona-consume@mason.service" "$SBX/events_log" | head -1 | cut -d: -f1)
if [[ -n "$kick_line" && -n "$unfence_line" ]] && (( kick_line < unfence_line )); then PASS=$((PASS+1));
else FAIL=$((FAIL+1)); FAILED_TESTS+=("kick must precede the unfence (kick@${kick_line:-none} unfence@${unfence_line:-none})"); echo "FAIL: kick must precede the unfence (kick@${kick_line:-none} unfence@${unfence_line:-none})"; fi
teardown_sandbox

# Graceful path (witnessed ack): the kick carries NO stale-handoff warning.
setup_sandbox
: > "$SBX/HANDOFF.md"   # must EXIST (else pre-mtime reads 0 off the host fs — non-hermetic)
run_sut 0 "graceful relaunch: ack witnessed, kick has no stale warning" -- "${REQ[@]}" \
  --handoff-file "$SBX/HANDOFF.md" --ack-timeout 60 --poll-interval 1   # T-4230: positive case, condition poll => generous window (was 5)
assert_contains "handoff ack: mtime 1000 -> 2000" "ack witnessed via the handoff mtime"
assert_logged paste_log "Your session was just cycled to reclaim context" "kick sent on the graceful path"
assert_not_logged paste_log "may predate this relaunch" "fresh handoff => no stale warning"
teardown_sandbox

# Kick DROPS (pane never shows a started turn) => exit 10, but the fence is still restored.
setup_sandbox
rm -f "$SBX/pane_text_busy"
run_sut 10 "kick not dispatched exits 10 (session live, idle)" -- "${REQ[@]}" \
  --consume-timer pantheon-persona-consume@mason.timer --skip-ack
assert_contains "successor is IDLE" "exit 10 names the idle-successor state"
assert_logged systemctl_log "unmask pantheon-persona-consume@mason.service" "failed kick still unfences the live seat"
assert_logged systemctl_log "start pantheon-persona-consume@mason.timer" "failed kick still restarts the consume timer"
teardown_sandbox

# --no-kick => no resume prompt pasted, exit 0.
setup_sandbox
run_sut 0 "--no-kick skips the resume kick" -- "${REQ[@]}" --skip-ack --no-kick
assert_contains "step7b: resume kick skipped (--no-kick)" "no-kick logged"
assert_not_logged paste_log "cycled to reclaim context" "no kick pasted under --no-kick"
teardown_sandbox

teardown_sandbox

# T-4278: a retry accepts a handoff written after the episode's first dispatch,
# even when it is unchanged relative to this retry's pre-prompt stat.
setup_sandbox
: > "$SBX/HANDOFF.md"
touch "$SBX/no_ack"
run_sut 0 "episode-start accepts the prior attempt's late handoff" -- "${REQ[@]}" \
  --handoff-file "$SBX/HANDOFF.md" --episode-start 900 --ack-timeout 2 --poll-interval 1
assert_contains "episode-start=900" "episode freshness witnessed"
teardown_sandbox

# A handoff older than the episode must never count.
setup_sandbox
: > "$SBX/HANDOFF.md"
touch "$SBX/no_ack"
run_sut 4 "handoff from before the episode is refused" -- "${REQ[@]}" \
  --handoff-file "$SBX/HANDOFF.md" --episode-start 1100 --ack-timeout 2 --poll-interval 1
assert_contains "ack timeout 2s" "pre-episode handoff did not acknowledge"
teardown_sandbox

# ---------------------------------------------------------------------------
# T-4832 r1: the respawned seat boots into the dev-channels dialog. The step-7 dismiss
# MUST Enter into that dialog (SEND_KEYS_ALLOW_DIALOG_ENTER), and the fake keeps the
# dialog on screen until the Enter lands, as real tmux does. Without the bypass the
# pre-Enter dialog check refuses and the relaunch fails masked.
# ---------------------------------------------------------------------------
setup_sandbox
printf '╭──────────────────────────────╮\n│ ready                        │\n╰──────────────────────────────╯\n ❯ \n' > "$SBX/pane_text"
printf '  WARNING: Loading development channels\n  Channels: server:telegram-bridge-stdio\n  ❯ 1. I am using this for local development\n    2. Exit\n  Enter to confirm · Esc to cancel\n' \
  > "$SBX/pane_text_fresh"
printf '╭──────────────────────────────╮\n│ ready                        │\n╰──────────────────────────────╯\n ❯ \n' > "$SBX/pane_text_boot_after"
: > "$SBX/HANDOFF.md"
run_sut 0 "boot dialog after respawn is dismissed with a real Enter" -- "${REQ[@]}" \
  --handoff-file "$SBX/HANDOFF.md" --consume-timer pantheon-persona-consume@mason.timer \
  --ack-timeout 2 --poll-interval 1
assert_contains "dismissing inline (option 1" "boot dialog dismiss attempted"
assert_logged tmux_sendkeys_log "Enter" "an Enter was sent for the dismiss"
teardown_sandbox

# ---------------------------------------------------------------------------
# T-4117: the handoff prompt lands on a BUSY pane. send_keys_submit returns 2
# (verified still pending: Claude Code queues input mid-turn). Before T-4117 the
# relaunch exited 3; now it waits for the ack on the queued window, pasting once.
# ---------------------------------------------------------------------------
pending_pane() {
  # The OLD boxed frame: no line starts with "❯", so the claude-code verifier sees no
  # live composer and falls back to the whole pane, where the marker reads "pending"
  # (T-4293). On the current TUI a pending paste sits IN the composer line.
  printf 'Working on something...\n╭─────────────────────────────────────╮\n│ ❯                                   │\n╰─────────────────────────────────────╯\n[Pasted text #1 +4 lines]\n' \
    > "$SBX/pane_text"
  printf '╭──────────────────────────────╮\n│ ready                        │\n╰──────────────────────────────╯\n ❯ \n' \
    > "$SBX/pane_text_fresh"
}

# Queued, then the turn ends and the seat writes its handoff => relaunch proceeds.
setup_sandbox
pending_pane
: > "$SBX/HANDOFF.md"
run_sut 0 "queued handoff prompt waits for the ack, then relaunches" -- "${REQ[@]}" \
  --handoff-file "$SBX/HANDOFF.md" --consume-timer pantheon-persona-consume@mason.timer \
  --ack-timeout 2 --queued-ack-timeout 60 --poll-interval 1   # T-4230: positive case; queued window stays > ack window (was 5)
assert_contains "prompt queued on suborch1-main:0.0" "queued submit logged"
assert_contains "waiting up to 60s for the ack" "ack window raised to the queued timeout"
assert_contains "handoff ack: mtime 1000 -> 2000" "ack witnessed after a queued submit"
if [[ "$(grep -c 'ctxfill graceful relaunch' "$SBX/paste_log")" == "1" ]]; then PASS=$((PASS+1));
else FAIL=$((FAIL+1)); FAILED_TESTS+=("queued prompt pasted exactly once"); echo "FAIL: queued prompt pasted exactly once"; fi
if [[ "$LAST_OUT" != *"delivery/submit failed"* ]]; then PASS=$((PASS+1));
else FAIL=$((FAIL+1)); FAILED_TESTS+=("queued submit is not a delivery failure"); echo "FAIL: queued submit is not a delivery failure"; fi
teardown_sandbox

# Queued, but no ack arrives => exit 4 on the QUEUED window, and the fence is restored.
setup_sandbox
pending_pane
: > "$SBX/HANDOFF.md"
touch "$SBX/no_ack"
run_sut 4 "queued handoff prompt with no ack times out (exit 4)" -- "${REQ[@]}" \
  --handoff-file "$SBX/HANDOFF.md" --consume-timer pantheon-persona-consume@mason.timer \
  --ack-timeout 1 --queued-ack-timeout 3 --poll-interval 1
assert_contains "ack timeout 3s" "timeout used the queued window, not --ack-timeout"
assert_logged systemctl_log "start pantheon-persona-consume@mason.timer" "queued ack-timeout still unfences the live seat"
assert_not_logged systemd_run_log "new-session" "no respawn after a queued ack-timeout"
teardown_sandbox

# T-4141: queued prompt stuck in the composer of a pane that is not busy -> the ack
# wait presses Enter once (never re-pastes), and that re-press is what submits it.
setup_sandbox
: > "$SBX/HANDOFF.md"
touch "$SBX/no_ack" "$SBX/ack_on_repress"
prompt="[ctxfill graceful relaunch] Please write a fresh $SBX/HANDOFF.md capturing your live state (active ticket, in-flight arc, next actions) NOW"
{ echo "Working on something..."; printf '─%.0s' {1..60}; echo; echo "❯ $prompt"
  printf '─%.0s' {1..60}; echo; echo "  ⏸ manual mode on · 2 shells · ↓ to manage"; } > "$SBX/pane_text"
printf '╭──────────────────────────────╮\n│ ready                        │\n╰──────────────────────────────╯\n ❯ \n' > "$SBX/pane_text_fresh"
PANTHEON_SEAT_RELAUNCH_REPRESS_INTERVAL_SECS=1 run_sut 0 "stuck queued prompt is re-pressed once, then the relaunch proceeds" -- "${REQ[@]}" \
  --handoff-file "$SBX/HANDOFF.md" --ack-timeout 2 --queued-ack-timeout 60 --poll-interval 1   # T-4230: positive case (was 10; failed under load)
assert_contains "re-pressed Enter on suborch1-main:0.0" "re-press logged"
assert_contains "handoff ack: mtime 1000 -> 2000" "the re-press produced the ack"
assert_logged paste_log "even if nothing changed" "T-4276: the prompt asks for a rewrite even if nothing changed"
if [[ "$(grep -c 'ctxfill graceful relaunch' "$SBX/paste_log")" == "1" ]]; then PASS=$((PASS+1));
else FAIL=$((FAIL+1)); FAILED_TESTS+=("re-press never re-pastes"); echo "FAIL: re-press never re-pastes"; fi
teardown_sandbox

# ---------------------------------------------------------------------------
# T-4085: re-derive --model/--effort from the seat's role config. Replaying the
# captured argv verbatim meant an instance.yaml model bump (T-4067) never reached a
# not-cut-over seat. Driven through the PROC_ROOT capture seam + a sandbox
# instance.yaml (PANTHEON_SEAT_RELAUNCH_INSTANCE_YAML).
# ---------------------------------------------------------------------------
CLAUDE_BIN=/home/suborch1/.npm-global/bin/claude

# Model bump reaches the respawn; the substitution is logged loudly.
setup_sandbox
write_instance claude-opus-5-5
run_sut 0 "role model bump rewrites captured --model on respawn" -- "${REQ[@]}" --skip-ack
assert_contains "argv --model claude-opus-4-8 -> claude-opus-5-5 from instance.yaml" "model substitution logged"
assert_logged systemd_run_log "$CLAUDE_BIN --model claude-opus-5-5 --permission-mode bypassPermissions" "respawn carries the role's model"
assert_not_logged systemd_run_log "claude-opus-4-8" "stale captured model gone from the respawn"
teardown_sandbox

# Dry-run plan shows the re-derived argv (the plan must match the real respawn).
setup_sandbox
write_instance claude-opus-5-5
run_sut 0 "dry-run plan shows the role-derived argv" -- "${REQ[@]}" --dry-run
assert_contains "$CLAUDE_BIN --model claude-opus-5-5 --permission-mode bypassPermissions" "dry-run shows re-derived model"
teardown_sandbox

# --model=<v> form is rewritten in place.
setup_sandbox
write_instance claude-opus-5-5
printf '%s\0--model=claude-opus-4-8\0--permission-mode\0bypassPermissions\0' "$CLAUDE_BIN" > "$SBX/proc/4242/cmdline"
run_sut 0 "--model=<v> form rewritten" -- "${REQ[@]}" --skip-ack
assert_logged systemd_run_log "$CLAUDE_BIN --model=claude-opus-5-5 --permission-mode bypassPermissions" "= form rewritten in place"
teardown_sandbox

# Absent --model is inserted right after argv[0]; nothing after `--` is touched.
setup_sandbox
write_instance claude-opus-5-5
printf '%s\0--permission-mode\0bypassPermissions\0--\0--model\0literal-arg\0' "$CLAUDE_BIN" > "$SBX/proc/4242/cmdline"
run_sut 0 "absent --model inserted after argv[0]" -- "${REQ[@]}" --skip-ack
assert_logged systemd_run_log "$CLAUDE_BIN --model claude-opus-5-5 --permission-mode bypassPermissions -- --model literal-arg" "inserted after argv0; post-'--' args untouched"
teardown_sandbox

# Declared effort: inserted when absent, replaced when present.
setup_sandbox
write_instance claude-opus-5-5 xhigh
run_sut 0 "role effort inserted" -- "${REQ[@]}" --skip-ack
assert_logged systemd_run_log "$CLAUDE_BIN --effort xhigh --model claude-opus-5-5 --permission-mode bypassPermissions" "effort inserted"
teardown_sandbox
setup_sandbox
write_instance claude-opus-5-5 xhigh
printf '%s\0--model\0claude-opus-4-8\0--effort\0high\0' "$CLAUDE_BIN" > "$SBX/proc/4242/cmdline"
run_sut 0 "role effort replaces captured effort" -- "${REQ[@]}" --skip-ack
assert_logged systemd_run_log "$CLAUDE_BIN --model claude-opus-5-5 --effort xhigh" "effort replaced"
teardown_sandbox

# Role declares no model (null) => captured --model kept.
setup_sandbox
write_instance null
run_sut 0 "null role model keeps captured argv" -- "${REQ[@]}" --skip-ack
assert_contains "declares no claude.model — keeping captured --model" "null model logged"
assert_logged systemd_run_log "$CLAUDE_BIN --model claude-opus-4-8 --permission-mode bypassPermissions" "captured model kept"
teardown_sandbox

# Unresolvable config => loud warning, captured argv kept, relaunch proceeds.
setup_sandbox
export PANTHEON_SEAT_RELAUNCH_INSTANCE_YAML="$SBX/no-such-instance.yaml"
run_sut 0 "unresolvable role config keeps captured argv (fail-safe)" -- "${REQ[@]}" --skip-ack
assert_contains "WARNING launch config: cannot resolve mason's role" "resolve failure is loud"
assert_logged systemd_run_log "$CLAUDE_BIN --model claude-opus-4-8 --permission-mode bypassPermissions" "captured argv replayed on resolve failure"
teardown_sandbox

# artisan resolves its OWN role (engineering-orchestrator-2), under the cargo wrapper.
setup_sandbox
write_instance claude-opus-5-5
printf '/home/suborch2/.npm-global/bin/claude\0--model\0claude-opus-4-8\0' > "$SBX/proc/4242/cmdline"
run_sut 0 "artisan relaunch re-derives its role model" -- "${ARTISAN_REQ[@]}" --skip-ack
assert_logged systemd_run_log 'exec "$@" _ /var/lib/pantheon/echo-target /home/suborch2/.npm-global/bin/claude --model claude-opus-5-5' "artisan model re-derived inside the cargo wrapper"
# --kick-prompt overrides the default text verbatim.
setup_sandbox
echo "CUSTOM-RESUME-TOKEN" > "$SBX/kick_busy_token"
run_sut 0 "--kick-prompt overrides the default kick" -- "${REQ[@]}" --skip-ack \
  --kick-prompt "CUSTOM-RESUME-TOKEN read the handoff"
assert_logged paste_log "CUSTOM-RESUME-TOKEN read the handoff" "custom kick prompt pasted verbatim"
assert_not_logged paste_log "cycled to reclaim context" "default kick text not used when overridden"
teardown_sandbox

# ---------------------------------------------------------------------------
# T-4171: the OLD harness must be gone before the respawn, and a harness that dies
# at startup must leave its output. artisan 2026-09-28 19:15Z: the old claude outlived
# its tmux server by 3s (its own tmux-spawn scope), the new one died at startup and
# the session vanished with no trace, leaving the seat DOWN and fenced.
# ---------------------------------------------------------------------------
setup_sandbox
run_sut 0 "happy path respawns with remain-on-exit, then clears it" -- "${REQ[@]}" --skip-ack
assert_logged systemd_run_log "start-server ; set-option -g remain-on-exit on ; new-session" "respawn sets remain-on-exit atomically, before new-session"
assert_logged tmux_setopt_log "set-option -gu remain-on-exit" "remain-on-exit cleared once the session is verified up"
assert_not_logged kill_log "kill -TERM" "a harness that exits on kill-server is never signalled"
teardown_sandbox

setup_sandbox
touch "$SBX/harness_lingers"
run_sut 0 "a lingering old harness is TERMed and waited out BEFORE the respawn" -- "${REQ[@]}" --skip-ack
assert_logged kill_log "kill -TERM 4242" "lingering old harness got SIGTERM"
if grep -n -E "^(KILL -TERM|RESPAWN)" "$SBX/events_log" | head -1 | grep -q "KILL -TERM"; then PASS=$((PASS+1))
else FAIL=$((FAIL+1)); FAILED_TESTS+=("TERM precedes respawn"); echo "FAIL: TERM must precede RESPAWN"; cat "$SBX/events_log"; fi
teardown_sandbox

setup_sandbox
touch "$SBX/harness_lingers" "$SBX/harness_ignores_term"
run_sut 11 "an old harness that survives SIGTERM blocks the respawn (exit 11)" -- "${REQ[@]}" \
  --consume-timer pantheon-persona-consume@mason.timer --skip-ack
assert_not_logged events_log "RESPAWN" "no second harness started beside the survivor"
assert_not_logged systemctl_log "unmask" "exit 11 leaves the fence UP (session is dead)"
assert_contains "Reconcile after fixing mason" "exit 11 prints the reconcile command"
teardown_sandbox

setup_sandbox
touch "$SBX/new_pane_dies"
printf 'Error: simulated startup crash T4171-TOKEN\n' > "$SBX/pane_text"
run_sut 6 "a harness that dies at startup leaves its output, then exit 6" -- "${REQ[@]}" \
  --consume-timer pantheon-persona-consume@mason.timer --skip-ack
assert_contains "EXITED at startup" "dead-pane path names the failure"
assert_contains "T4171-TOKEN" "the dead pane's last output reaches the log"
if [[ "$(grep -c KILL-SERVER "$SBX/events_log")" -ge 2 ]]; then PASS=$((PASS+1))
else FAIL=$((FAIL+1)); FAILED_TESTS+=("dead pane is torn down"); echo "FAIL: dead pane not torn down (kill-server after respawn)"; fi
assert_not_logged systemctl_log "unmask" "exit 6 leaves the fence UP"
teardown_sandbox

# ---------------------------------------------------------------------------
# T-4201: last-good launch snapshot + --recover for a seat with NO live pane.
# The snapshot is ONE NUL-delimited file: record 0 = cwd, records 1.. = argv.
# ---------------------------------------------------------------------------
SNAP="state/seat_relaunch_mason.last_launch"
DEFAULT_ARGV_NUL='/home/suborch1/.npm-global/bin/claude\0--model\0claude-opus-4-8\0--permission-mode\0bypassPermissions\0'
# seed_snapshot <token>: a distinctive persisted launch for mason (cwd + argv).
seed_snapshot() {
  mkdir -p "$SBX/state"
  printf '/home/suborch1/snapdir\0/home/suborch1/.npm-global/bin/claude\0--model\0claude-opus-4-8\0--append-system-prompt\0%s\0' "$1" \
    > "$SBX/$SNAP"
}
# seed_snapshot_of_proc: snapshot argv == the fake /proc/4242 harness argv (orphan cases).
seed_snapshot_of_proc() { mkdir -p "$SBX/state"; { printf '/home/suborch1/pantheon-config\0'; cat "$SBX/proc/4242/cmdline"; } > "$SBX/$SNAP"; }
# no_live_pane: the failed relaunch killed the server and its harness is gone.
no_live_pane() { echo dead > "$SBX/session_state"; rm -rf "$SBX/proc/4242"; }
snap_has() { tr '\0' ' ' < "$SBX/$SNAP" 2>/dev/null | grep -Fq -- "$1"; }
check() {  # <cond-rc> <label>
  if [[ "$1" == 0 ]]; then PASS=$((PASS+1)); else FAIL=$((FAIL+1)); FAILED_TESTS+=("$2"); echo "FAIL: $2"; fi
}

setup_sandbox
run_sut 0 "T-4201: a relaunch persists the captured launch as the snapshot" -- "${REQ[@]}" --skip-ack
cmp -s "$SBX/$SNAP" <(printf "/home/suborch1/pantheon-config\0$DEFAULT_ARGV_NUL")
check $? "snapshot is cwd + the captured argv, byte-identical (NUL-delimited)"
[[ -z "$(find "$SBX/state" -name '.seat_relaunch_*' 2>/dev/null)" ]]; check $? "no temp file left behind"
teardown_sandbox

setup_sandbox
write_instance claude-opus-5-5
run_sut 0 "T-4201: the verified relaunch re-persists the LAUNCHED argv (role rewrite)" -- "${REQ[@]}" --skip-ack
snap_has "--model claude-opus-5-5"; check $? "snapshot carries the T-4085 role-config model after step 7"
teardown_sandbox

setup_sandbox
touch "$SBX/new_pane_dies"
run_sut 6 "T-4201: a relaunch that dies at startup keeps the CAPTURED snapshot" -- "${REQ[@]}" --skip-ack
snap_has "--permission-mode bypassPermissions"; check $? "snapshot persisted before the disruption survives a failed respawn"
teardown_sandbox

setup_sandbox
run_sut 0 "T-4201: --dry-run persists nothing" -- "${REQ[@]}" --dry-run
[[ ! -e "$SBX/$SNAP" ]]; check $? "dry-run wrote no snapshot"
teardown_sandbox

setup_sandbox
run_sut 0 "T-4201: --record-argv seeds the snapshot and touches nothing else" -- "${REQ[@]}" \
  --record-argv --consume-timer pantheon-persona-consume@mason.timer
[[ -s "$SBX/$SNAP" ]]; check $? "--record-argv wrote the snapshot"
assert_not_logged events_log "KILL-SERVER" "--record-argv never kills the server"
assert_not_logged events_log "RESPAWN" "--record-argv never respawns"
assert_not_logged systemctl_log "mask" "--record-argv never fences"
teardown_sandbox

setup_sandbox
run_sut 0 "T-4201: --record-argv --dry-run writes nothing" -- "${REQ[@]}" --record-argv --dry-run
[[ ! -e "$SBX/$SNAP" ]]; check $? "--record-argv under --dry-run wrote no snapshot"
assert_contains "would persist" "--record-argv --dry-run says what it would persist"
teardown_sandbox

setup_sandbox
run_sut 64 "T-4201: --record-argv with --recover is a usage error" -- "${REQ[@]}" --record-argv --recover
run_sut 64 "T-4201: --launch-cwd without --launch-argv-file is a usage error" -- "${REQ[@]}" --recover --launch-cwd /tmp
run_sut 64 "T-4201: --launch-argv-file with no value is a usage error (not a set -u abort)" -- "${REQ[@]}" --launch-argv-file
assert_contains "--launch-argv-file needs a value" "the missing value is named, not a later arg swallowed"
teardown_sandbox

setup_sandbox
seed_snapshot T4201-SNAP-TOKEN
no_live_pane
run_sut 0 "T-4201: --recover replays the snapshot for a seat with no live pane" -- "${REQ[@]}" \
  --recover --consume-timer pantheon-persona-consume@mason.timer
assert_logged systemd_run_log "T4201-SNAP-TOKEN" "respawn carries the snapshot argv"
assert_logged systemd_run_log "/home/suborch1/snapdir" "respawn uses the snapshot cwd"
assert_not_logged paste_log "[ctxfill graceful relaunch]" "recovery skips the handoff prompt (no harness to ask)"
assert_logged paste_log "may predate this relaunch" "the successor is warned its handoff may be stale"
assert_logged systemctl_log "mask pantheon-persona-consume@mason.service" "recovery still fences the consume timer"
assert_logged systemctl_log "unmask pantheon-persona-consume@mason.service" "recovery unfences after a verified relaunch"
assert_not_logged kill_log "kill -TERM" "no orphan harness => nothing signalled"
teardown_sandbox

setup_sandbox
seed_snapshot T4201-SNAP-TOKEN
write_instance claude-opus-5-5
no_live_pane
run_sut 0 "T-4201: --recover applies the role config over the snapshot" -- "${REQ[@]}" --recover
assert_logged systemd_run_log "model claude-opus-5-5" "recovery respawn carries the role-config model"
teardown_sandbox

setup_sandbox
seed_snapshot T4201-SNAP-TOKEN   # its cwd (snapdir) must NOT be borrowed by an argv file
no_live_pane
printf '/home/suborch1/.npm-global/bin/claude\0--resume\0T4201-FILE-TOKEN\0' > "$SBX/custom.argv"
run_sut 0 "T-4201: --launch-argv-file replays the given file" -- "${REQ[@]}" --launch-argv-file "$SBX/custom.argv"
assert_logged systemd_run_log "T4201-FILE-TOKEN" "respawn carries the given file's argv"
assert_logged systemd_run_log "/home/suborch1/pantheon-config" "argv file defaults to the account's pantheon-config cwd"
assert_not_logged systemd_run_log "snapdir" "argv file never borrows the persisted snapshot's cwd"
teardown_sandbox

setup_sandbox
no_live_pane
printf '/home/suborch1/.npm-global/bin/claude\0--resume\0T4201-FILE-TOKEN\0' > "$SBX/custom.argv"
run_sut 0 "T-4201: --launch-cwd sets the argv file's cwd" -- "${REQ[@]}" \
  --launch-argv-file "$SBX/custom.argv" --launch-cwd /home/suborch1/elsewhere
assert_logged systemd_run_log "/home/suborch1/elsewhere" "respawn uses --launch-cwd"
teardown_sandbox

setup_sandbox
seed_snapshot T4201-SNAP-TOKEN
run_sut 12 "T-4201: --recover refuses a LIVE session (exit 12)" -- "${REQ[@]}" \
  --recover --consume-timer pantheon-persona-consume@mason.timer
assert_not_logged events_log "KILL-SERVER" "refused recovery leaves the live session alone"
assert_not_logged systemctl_log "mask" "refused recovery never fences"
teardown_sandbox

setup_sandbox
seed_snapshot T4201-SNAP-TOKEN
printf '1\n0\n' > "$SBX/pane_dead"   # two panes: one dead, one LIVE
run_sut 12 "T-4201: --recover refuses a session with ANY live pane (exit 12)" -- "${REQ[@]}" --recover
assert_not_logged events_log "KILL-SERVER" "a live sibling pane is never killed"
teardown_sandbox

setup_sandbox
seed_snapshot T4201-SNAP-TOKEN
echo 1 > "$SBX/pane_dead"   # the session survived as a remain-on-exit DEAD pane
run_sut 0 "T-4201: --recover clears a leftover DEAD pane, then respawns" -- "${REQ[@]}" --recover
if grep -n -E "^(KILL-SERVER|RESPAWN)" "$SBX/events_log" | head -1 | grep -q "KILL-SERVER"; then PASS=$((PASS+1))
else FAIL=$((FAIL+1)); FAILED_TESTS+=("dead pane cleared before respawn"); echo "FAIL: KILL-SERVER must precede RESPAWN"; cat "$SBX/events_log"; fi
teardown_sandbox

setup_sandbox
no_live_pane
run_sut 2 "T-4201: --recover with no snapshot refuses a guessed command (exit 2)" -- "${REQ[@]}" --recover
assert_contains "refusing to respawn a guessed command" "names the refusal"
assert_not_logged events_log "RESPAWN" "no snapshot => no respawn"
teardown_sandbox

setup_sandbox
no_live_pane
: > "$SBX/custom.argv"
run_sut 2 "T-4201: an EMPTY argv file is refused (exit 2)" -- "${REQ[@]}" --launch-argv-file "$SBX/custom.argv"
assert_not_logged events_log "RESPAWN" "empty argv => no respawn"
teardown_sandbox

setup_sandbox
seed_snapshot T4201-SNAP-TOKEN
truncate -s -1 "$SBX/$SNAP"   # drop the final NUL: a torn/truncated write
no_live_pane
run_sut 2 "T-4201: a snapshot that is not NUL-terminated is refused (exit 2)" -- "${REQ[@]}" --recover
assert_contains "not NUL-terminated" "names the truncation"
assert_not_logged events_log "RESPAWN" "truncated snapshot => no respawn"
teardown_sandbox

setup_sandbox
seed_snapshot T4201-SNAP-TOKEN
mv "$SBX/$SNAP" "$SBX/elsewhere.snap"; ln -s "$SBX/elsewhere.snap" "$SBX/$SNAP"
no_live_pane
run_sut 2 "T-4201: a SYMLINKED snapshot is refused (exit 2)" -- "${REQ[@]}" --recover
assert_not_logged events_log "RESPAWN" "symlinked snapshot => no respawn"
teardown_sandbox

# An orphan harness: the failed relaunch's claude outlived its server (T-4171 class).
# proc/4242 carries the SAME argv as the snapshot, and there is no session.
setup_sandbox
seed_snapshot_of_proc
echo dead > "$SBX/session_state"
touch "$SBX/harness_lingers" "$SBX/harness_ignores_term"
run_sut 11 "T-4201: --recover refuses while the failed relaunch's harness survives SIGTERM" -- "${REQ[@]}" \
  --recover --consume-timer pantheon-persona-consume@mason.timer
assert_logged kill_log "kill -TERM 4242" "the orphan harness (found by snapshot argv) got SIGTERM"
assert_not_logged events_log "RESPAWN" "no second harness beside the orphan"
teardown_sandbox

setup_sandbox
seed_snapshot_of_proc
echo dead > "$SBX/session_state"
touch "$SBX/harness_lingers"
run_sut 0 "T-4201: --recover waits out an orphan harness that exits on SIGTERM" -- "${REQ[@]}" --recover
if grep -n -E "^(KILL -TERM|RESPAWN)" "$SBX/events_log" | head -1 | grep -q "KILL -TERM"; then PASS=$((PASS+1))
else FAIL=$((FAIL+1)); FAILED_TESTS+=("orphan TERM precedes respawn"); echo "FAIL: orphan TERM must precede RESPAWN"; cat "$SBX/events_log"; fi
teardown_sandbox

setup_sandbox
seed_snapshot_of_proc
echo dead > "$SBX/session_state"
touch "$SBX/harness_lingers"
mkdir -p "$SBX/proc/4343"; cp "$SBX/proc/4242/cmdline" "$SBX/proc/4343/cmdline"   # a DUPLICATE orphan
run_sut 11 "T-4201: a duplicate orphan still present at respawn time blocks it (exit 11)" -- "${REQ[@]}" --recover
assert_contains "pid 4343 still carries the snapshot argv" "the pre-respawn re-scan names the duplicate"
assert_not_logged events_log "RESPAWN" "no harness started beside the duplicate"
teardown_sandbox

setup_sandbox
seed_snapshot_of_proc
no_live_pane
mkdir -p "$SBX/proc/4444"
printf '/home/suborch1/.npm-global/bin/claude\0--model claude-opus-4-8\0--permission-mode\0bypassPermissions\0' > "$SBX/proc/4444/cmdline"
run_sut 0 "T-4201: a process whose argv only JOINS equal is not an orphan" -- "${REQ[@]}" --recover
assert_not_logged kill_log "kill -TERM" "element-wise argv compare: the look-alike is never signalled"
teardown_sandbox

# ---------------------------------------------------------------------------
echo
echo "pantheon-seat-relaunch: $PASS passed, $FAIL failed"
if (( FAIL > 0 )); then
  printf '  - %s\n' "${FAILED_TESTS[@]}"
  exit 1
fi
exit 0
