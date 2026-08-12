#!/usr/bin/env bash
# Test harness for pantheon-agent-state-snapshot.sh (T-2424 mechanism; T-2736
# memory-only-seat support).
#
# Strategy: per case, build a temp state-repo (git init), a temp memory dir, and
# a temp HANDOFF, then run the SUT with PANTHEON_* env overrides and assert on
# exit code + on-disk snapshot contents. No systemd, no real seats.
#
# Invocation:  bash tools/systemd/test_pantheon-agent-state-snapshot.sh
# Exit: 0 = all pass; non-zero = a failure (self-reports "N passed, M failed").

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUT="$SCRIPT_DIR/pantheon-agent-state-snapshot.sh"

PASS=0
FAIL=0
FAILED=()

# --- sandbox -----------------------------------------------------------------
setup() {
  SBX=$(mktemp -d -t agent_snap_test.XXXXXX)
  REPO="$SBX/repo"
  MEM="$SBX/memory"
  HO="$SBX/HANDOFF.md"
  git init -q "$REPO"
  git -C "$REPO" config user.email "test@pantheon.local"
  git -C "$REPO" config user.name "test"
  mkdir -p "$MEM"
  echo "fact one" > "$MEM/m1.md"
  echo "handoff body" > "$HO"
}
teardown() { rm -rf "$SBX"; }

# run_sut <require_handoff|-> ; sets RC + captures STDERR to $SBX/err
run_sut() {
  local req="$1"
  local -a env=(
    "PANTHEON_AGENT_STATE_REPO=$REPO"
    "PANTHEON_MEMORY_DIR=$MEM"
    "PANTHEON_HANDOFF=$HO"
  )
  [[ "$req" != "-" ]] && env+=("PANTHEON_SNAPSHOT_REQUIRE_HANDOFF=$req")
  env "${env[@]}" bash "$SUT" >"$SBX/out" 2>"$SBX/err"
  RC=$?
}

ok()   { PASS=$((PASS+1)); echo "  PASS: $1"; }
bad()  { FAIL=$((FAIL+1)); FAILED+=("$1"); echo "  FAIL: $1 — $2";
         echo "    --- out ---"; sed 's/^/    /' "$SBX/out";
         echo "    --- err ---"; sed 's/^/    /' "$SBX/err"; }

# --- 1: HANDOFF present, default mode → snapshots both. ----------------------
setup
run_sut -
if [[ $RC -eq 0 && -f "$REPO/HANDOFF.md" && -f "$REPO/memory/m1.md" ]] \
   && git -C "$REPO" rev-parse HEAD >/dev/null 2>&1; then
  ok "handoff_present_default_snapshots_both"
else
  bad "handoff_present_default_snapshots_both" "rc=$RC handoff=$([[ -f $REPO/HANDOFF.md ]]&&echo y) mem=$([[ -f $REPO/memory/m1.md ]]&&echo y)"
fi
teardown

# --- 2: HANDOFF missing, default (require=1) → exit 3 (loss detection intact). -
setup
rm -f "$HO"
run_sut -
if [[ $RC -eq 3 ]] && grep -q "HANDOFF MISSING" "$SBX/err"; then
  ok "handoff_missing_default_exit3"
else
  bad "handoff_missing_default_exit3" "expected rc=3 + 'HANDOFF MISSING', got rc=$RC"
fi
teardown

# --- 3: HANDOFF missing, require=0 → memory-only snapshot, exit 0, warns. ----
setup
rm -f "$HO"
run_sut 0
if [[ $RC -eq 0 && -f "$REPO/memory/m1.md" && ! -f "$REPO/HANDOFF.md" ]] \
   && grep -q "memory-only snapshot" "$SBX/err"; then
  ok "handoff_missing_optout_memory_only"
else
  bad "handoff_missing_optout_memory_only" "rc=$RC handoff_present=$([[ -f $REPO/HANDOFF.md ]]&&echo y) mem=$([[ -f $REPO/memory/m1.md ]]&&echo y)"
fi
teardown

# --- 4: memory dir missing → exit 3 regardless of mode. ---------------------
setup
rm -rf "$MEM"
run_sut 0
if [[ $RC -eq 3 ]] && grep -q "memory dir MISSING" "$SBX/err"; then
  ok "memory_missing_exit3"
else
  bad "memory_missing_exit3" "expected rc=3 + 'memory dir MISSING', got rc=$RC"
fi
teardown

# --- 5: no-change second run → exit 0, "nothing to snapshot". ----------------
setup
run_sut -            # first snapshot
run_sut -            # second, unchanged
if [[ $RC -eq 0 ]] && grep -q "nothing to snapshot" "$SBX/out"; then
  ok "no_change_second_run_noop"
else
  bad "no_change_second_run_noop" "expected rc=0 + 'nothing to snapshot', got rc=$RC"
fi
teardown

# --- 6: require=0 but HANDOFF present → still snapshots it (bonus). ----------
setup
run_sut 0
if [[ $RC -eq 0 && -f "$REPO/HANDOFF.md" ]]; then
  ok "optout_but_handoff_present_still_snapshotted"
else
  bad "optout_but_handoff_present_still_snapshotted" "rc=$RC handoff_present=$([[ -f $REPO/HANDOFF.md ]]&&echo y)"
fi
teardown

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] || { echo "Failed: ${FAILED[*]}"; exit 1; }
exit 0
