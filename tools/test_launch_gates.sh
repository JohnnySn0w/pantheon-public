#!/bin/bash
# ORG-106 L3a (T-5526): the shell launch gates, proven with a STUB seat_state.py.
#
# Every gated launcher calls `seat_state.py launch-check`. Here each script runs from a
# sandbox copy whose seat_state.py is a stub: exit 3 = deep-sleep (the gate must refuse and
# nothing may launch), exit 0 = allowed (it must proceed to launch), any other code = a
# reader fault (it must FAIL OPEN and proceed). Fakes stand in for tmux/id/sg/sleep, so no
# real session, systemd or seat state is touched.
#
# Wired into the smoke loop by tools/test_shell_suites.py (discovery by glob).
#   bash tools/test_launch_gates.sh        # exit 0 = all pass

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PASS=0
FAIL=0
FAILED=()

ok()   { echo "  PASS: $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $1 -- $2"; FAIL=$((FAIL+1)); FAILED+=("$1"); }

new_sandbox() {
  SBX="$(mktemp -d -t launch_gates.XXXXXX)"
  mkdir -p "$SBX/bin" "$SBX/tools" "$SBX/home/pantheon-config/tools"
  : > "$SBX/launched"
  : > "$SBX/sleeps"
  # fake tmux: has-session always "absent" so the script launches; new-session is recorded
  cat > "$SBX/bin/tmux" <<'EOF'
#!/bin/bash
case "$1" in
  new-session) echo "$*" >> "$SBX_LAUNCHED" ;;
  has-session) exit 1 ;;
esac
exit 0
EOF
  # fake sg: `sg <group> -c "<cmd>"` just runs the command
  cat > "$SBX/bin/sg" <<'EOF'
#!/bin/bash
exec bash -c "$3"
EOF
  # fake sleep: records, returns at once (the supervise gate WAITS with `sleep 60`)
  cat > "$SBX/bin/sleep" <<'EOF'
#!/bin/bash
echo "$*" >> "$SBX_SLEEPS"
EOF
  # fake id: a seat account for seat_spawn.sh's suborch* check
  cat > "$SBX/bin/id" <<'EOF'
#!/bin/bash
[[ "$1" == "-un" ]] && { echo suborch9; exit 0; }
exec /usr/bin/id "$@"
EOF
  # fake ls: recovery-bot-supervise.sh globs the telegram plugin cache with `ls -d ...|sort|tail`
  # under pipefail; on a box without that cache the pipeline fails before the gate is reached.
  printf '#!/bin/bash\nexit 0\n' > "$SBX/bin/ls"
  chmod +x "$SBX"/bin/*
  export SBX_LAUNCHED="$SBX/launched" SBX_SLEEPS="$SBX/sleeps"
}

# stub seat_state.py: exits with the code in $SBX/rc_seq (first line consumed per call),
# repeating the last when exhausted; logs its argv.
write_stub() {
  local dest="$1"; shift
  printf '%s\n' "$@" > "$SBX/rc_seq"
  cat > "$dest" <<'EOF'
import os, sys
seq = os.path.join(os.environ["SBX"], "rc_seq")
lines = open(seq).read().split()
code = int(lines[0])
if len(lines) > 1:
    open(seq, "w").write("\n".join(lines[1:]) + "\n")
with open(os.path.join(os.environ["SBX"], "calls"), "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
sys.stderr.write("stub launch-check\n")
sys.exit(code)
EOF
}

launched_count() { wc -l < "$SBX/launched" | tr -d ' '; }

# --- seat_spawn.sh ------------------------------------------------------------------------
run_seat_spawn() {  # $1 = case name, $2 = stub rc, $3 = expected exit, $4 = expect launch (0|1)
  new_sandbox
  cp "$SCRIPT_DIR/seat_spawn.sh" "$SBX/tools/"
  write_stub "$SBX/tools/seat_state.py" "$2"
  local rc=0
  env SBX="$SBX" PATH="$SBX/bin:$PATH" HOME="$SBX/home" SEAT_SESSION=gate-test \
    bash "$SBX/tools/seat_spawn.sh" >"$SBX/out" 2>"$SBX/err" || rc=$?
  if [[ "$rc" == "$3" && "$(launched_count)" == "$4" ]] && grep -q -- "--account suborch9" "$SBX/calls"; then
    ok "seat_spawn: $1"
  else
    bad "seat_spawn: $1" "rc=$rc (want $3) launches=$(launched_count) (want $4) err=$(cat "$SBX/err")"
  fi
  rm -rf "$SBX"
}
run_seat_spawn refuses_deep_sleep 3 75 0
run_seat_spawn allows 0 0 1
run_seat_spawn fails_open_on_fault 1 0 1

# --- pantheon-seat-relaunch.sh --------------------------------------------------------------
run_relaunch() {  # $1 = case, $2 = stub rc, $3 = "refused"|"proceeds"
  new_sandbox
  cp "$SCRIPT_DIR/pantheon-seat-relaunch.sh" "$SCRIPT_DIR/send_keys_submit.sh" "$SBX/tools/"
  write_stub "$SBX/tools/seat_state.py" "$2"
  echo "handoff" > "$SBX/HANDOFF.md"
  local rc=0
  env SBX="$SBX" PATH="$SBX/bin:$PATH" HOME="$SBX/home" PANTHEON_STATE_DIR="$SBX/state" \
    bash "$SBX/tools/pantheon-seat-relaunch.sh" --persona mason --session mason-main \
      --target-pane mason-main:0 --scope-unit pantheon-mason-seat.scope \
      --handoff-file "$SBX/HANDOFF.md" --account "$(id -un)" >"$SBX/out" 2>"$SBX/err" || rc=$?
  local refused=0
  [[ "$rc" == "75" ]] && grep -q "deep-sleep" "$SBX/err" && refused=1
  if [[ "$3" == "refused" && $refused == 1 ]] || [[ "$3" == "proceeds" && $refused == 0 && -s "$SBX/calls" ]]; then
    ok "seat-relaunch: $1"
  else
    bad "seat-relaunch: $1" "rc=$rc refused=$refused err=$(head -c 300 "$SBX/err")"
  fi
  rm -rf "$SBX"
}
run_relaunch refuses_deep_sleep 3 refused
run_relaunch allows 0 proceeds
run_relaunch fails_open_on_fault 1 proceeds

# --- the four bespoke supervise scripts (WAIT loop, not exit) --------------------------------
run_supervise() {  # $1 = script, $2 = persona, $3 = "waits"|"launches", $4.. = stub rc sequence
  local script="$1" persona="$2" want="$3"; shift 3
  new_sandbox
  cp "$SCRIPT_DIR/systemd/$script" "$SBX/tools/"
  write_stub "$SBX/home/pantheon-config/tools/seat_state.py" "$@"
  local rc=0
  env SBX="$SBX" PATH="$SBX/bin:$PATH" HOME="$SBX/home" XDG_RUNTIME_DIR="$SBX/xdg" \
    timeout 60 bash "$SBX/tools/$script" >"$SBX/out" 2>"$SBX/err" || rc=$?
  local launches sleeps60
  launches="$(launched_count)"
  sleeps60="$(grep -c '^60$' "$SBX/sleeps")"
  case "$want" in
    waits)     # 3,3,0: two waits (sleep 60) and only then the launch
      if [[ "$launches" == "1" && "$sleeps60" -ge 2 ]] && grep -q "launch-check $persona" "$SBX/calls"; then
        ok "$script: waits while deep-sleeping, launches after wake"
      else bad "$script: waits" "rc=$rc launches=$launches sleeps60=$sleeps60 calls=$(cat "$SBX/calls" 2>/dev/null) err=$(head -c 300 "$SBX/err")"; fi ;;
    launches)  # allowed / fault: launches with no wait
      if [[ "$launches" == "1" && "$sleeps60" == "0" ]]; then
        ok "$script: launches immediately when allowed or on a fault"
      else bad "$script: launches" "rc=$rc launches=$launches sleeps60=$sleeps60 err=$(head -c 300 "$SBX/err")"; fi ;;
  esac
  rm -rf "$SBX"
}
for pair in "claude-bot-supervise.sh orchestrator" "specialist-bot-supervise.sh specialist" "exo-bot-supervise.sh archivist" "recovery-bot-supervise.sh recovery"; do
  set -- $pair
  run_supervise "$1" "$2" waits 3 3 0
  run_supervise "$1" "$2" launches 0
  run_supervise "$1" "$2" launches 1
done

# --- persona_cutover.sh / persona_rollback.sh (ORG-106 L3b fold-in) -----------------------
# Both resolve the persona first, then run the gate. A STUB resolver supplies the PCX_ identity
# (this uid, a real XDG dir), so the script reaches the gate on any box. Refused = exit 1 with the
# deep-sleep message and nothing after it ran; allowed / fault = it got PAST the gate, proven by
# the next stage's own refusal marker (no real unit, backup or manifest is needed or touched).
write_resolver_stub() {  # $1 = destination (persona_cutover_resolve.py in the sandbox tools dir)
  mkdir -p "$SBX/xdg"
  cat > "$1" <<'EOF'
import os, sys
sbx = os.environ["SBX"]
vals = {
    "PCX_UID": str(os.getuid()), "PCX_ACCOUNT": "stub", "PCX_HOME": sbx + "/home",
    "PCX_XDG_RUNTIME_DIR": sbx + "/xdg", "PCX_DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent",
    "PCX_ROLE": "stub-role", "PCX_MANIFEST_PATH": sbx + "/instance.yaml", "PCX_REPO_ROOT": sbx + "/repo",
    "PCX_APPLY_PATH": os.environ["PATH"], "PCX_SERVICE_UNIT_PATH": sbx + "/unit.service",
    "PCX_SUPERVISE_WRAPPER_PATH": sbx + "/supervise.sh", "PCX_SETTINGS_JSON_PATH": sbx + "/settings.json",
    "PCX_CONSUME_SERVICE": "consume.service", "PCX_CONSUME_TIMER": "consume.timer",
    "PCX_SERVICE": "stub.service", "PCX_TMUX_SESSION": "stub", "PCX_MODEL": "m", "PCX_CHANNELS": "",
    "PCX_COMPANION_UNITS": "",
}
for k, v in vals.items():
    print(f"{k}={v}")
EOF
}

run_persona_script() {  # $1 = script name, $2 = case, $3 = stub rc, $4 = "refused"|"passes", $5 = marker the next stage prints
  local script="$1" name="$2" stubrc="$3" want="$4" marker="$5"
  new_sandbox
  cp "$SCRIPT_DIR/$script" "$SCRIPT_DIR/t2590_cutover_verify_lib.sh" "$SBX/tools/"
  write_resolver_stub "$SBX/tools/persona_cutover_resolve.py"
  write_stub "$SBX/tools/seat_state.py" "$stubrc"
  local rc=0
  env SBX="$SBX" PATH="$SBX/bin:$PATH" HOME="$SBX/home" \
    bash "$SBX/tools/$script" --persona stubseat --stamp TS >"$SBX/out" 2>"$SBX/err" || rc=$?
  local refused=0 passed=0
  grep -q "deep-sleep; wake it first" "$SBX/err" && refused=1
  grep -q -- "$marker" "$SBX/err" "$SBX/out" 2>/dev/null && passed=1
  if [[ "$want" == "refused" && $refused == 1 && $rc -ne 0 && $passed == 0 ]] \
     || [[ "$want" == "passes" && $refused == 0 && $passed == 1 ]]; then
    if grep -q "launch-check stubseat" "$SBX/calls"; then ok "$script: $name"; else bad "$script: $name" "gate never asked launch-check"; fi
  else
    bad "$script: $name" "rc=$rc refused=$refused passed=$passed err=$(head -c 300 "$SBX/err")"
  fi
  rm -rf "$SBX"
}
run_persona_script persona_cutover.sh refuses_deep_sleep 3 refused "install-done precondition failed"
run_persona_script persona_cutover.sh allows 0 passes "install-done precondition failed"
run_persona_script persona_cutover.sh fails_open_on_fault 1 passes "install-done precondition failed"
run_persona_script persona_rollback.sh refuses_deep_sleep 3 refused "missing backup"
run_persona_script persona_rollback.sh allows 0 passes "missing backup"
run_persona_script persona_rollback.sh fails_open_on_fault 1 passes "missing backup"

echo "launch gates: $PASS passed, $FAIL failed"
if [[ $FAIL -ne 0 ]]; then
  printf 'FAILED: %s\n' "${FAILED[@]}"
  exit 1
fi
exit 0
