#!/usr/bin/env bash
# test_acp_bridge_supervise_session_death.sh — the OTHER half of the supervise
# contract (T-2245, item 5).
#
# test_acp_bridge_supervise_trap.sh proves the script tears its tmux session down
# when systemd stops it. This proves the converse direction, which is the one the
# 2026-07-28 outage actually took: when the SESSION dies on its own, the supervise
# loop must NOTICE and EXIT, so systemd's Restart=always can respawn it.
#
# That direction is the whole reason the poll loop at the bottom of
# acp-bridge-supervise.sh exists:
#
#     while tmux has-session -t "$SESSION" 2>/dev/null; do sleep 5; done
#     exit 0
#
# A supervisor that blocks forever after its child is gone is indistinguishable
# from a healthy one — systemd sees a live MainPID, the unit stays `active`, no
# alert fires anywhere, and mail simply stops moving. That is precisely what
# archivist's bridge looked like from the outside for ninety minutes: the loop
# logged iteration lines with empty processed[] to iteration 542 and then nothing,
# and it was invisible at BOTH ends.
#
# HARNESS — same pattern as the trap test, no real tmux/sg/python3 dependency: a
# stub `tmux` on PATH records invocations and simulates has-session/new-session/
# kill-session via a marker file; the script runs backgrounded under a sandbox
# HOME. Killing the session is then just `rm` on the marker, which is exactly what
# the script's own has-session probe consults.
#
# THE BOUND IS LOAD-BEARING. The failure mode being guarded against is a
# supervisor that HANGS, and an unbounded wait for a hang is itself a hang — a
# regression would stall CI instead of failing it, which is the same
# silence-reads-as-health defect the test is about. So the wait is a bounded
# poll: 30s from session death (EXIT_BOUND below), six of the loop's own 5s poll
# intervals so it holds on a saturated runner, and far under systemd's default 90s
# TimeoutStopSec. Replace the poll loop with `while true; do sleep 5; done` and
# this FAILS in 30 seconds rather than hanging.
#
# Run after any edit to acp-bridge-supervise.sh's session/poll logic.
# Exit 0 == passed.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUPERVISE="$HERE/acp-bridge-supervise.sh"
[[ -x "$SUPERVISE" ]] || { echo "FATAL: supervise script not executable at $SUPERVISE"; exit 1; }

SANDBOX="$(mktemp -d)"
STUBDIR="$SANDBOX/stubbin"
mkdir -p "$STUBDIR"

SCRIPT_PID=""
cleanup() {
  [[ -n "$SCRIPT_PID" ]] && kill -KILL "$SCRIPT_PID" 2>/dev/null
  rm -rf "$SANDBOX"
}
trap cleanup EXIT

export STUB_TMUX_LOG="$SANDBOX/tmux.log"
export STUB_TMUX_MARK="$SANDBOX/tmux.session.mark"
: > "$STUB_TMUX_LOG"

cat > "$STUBDIR/tmux" <<'STUB'
#!/usr/bin/env bash
# Minimal stub: only the subset acp-bridge-supervise.sh calls.
LOG="${STUB_TMUX_LOG:?}"
MARK="${STUB_TMUX_MARK:?}"
case "$1" in
  has-session)
    [[ -e "$MARK" ]]
    ;;
  new-session)
    echo "new-session $*" >> "$LOG"
    touch "$MARK"
    ;;
  kill-session)
    echo "kill-session $*" >> "$LOG"
    rm -f "$MARK"
    ;;
  *)
    echo "unhandled tmux subcommand: $*" >&2
    exit 1
    ;;
esac
STUB
chmod +x "$STUBDIR/tmux"

AGENT="testagent"
SESSION="acp-bridge-$AGENT"

PASS=0; FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

# Wall-clock bounds (T-4348). Both waits are deadline-based on $SECONDS, not
# iteration counts, so the bound means the same thing on an idle box and on the
# saturated CI runner.
#
# STARTUP is a wait on a POSITIVE readiness signal (the stub's new-session marker),
# so its bound only caps a broken start and never paces a healthy one: 60s. The
# old 5s bound was a timing guess, and it lost. Before its first new-session the
# script resolves the mailbox hub with a python3 import of agent_role_map, which
# measured 4-5s on the loaded runner by itself, and on pve2 it did not reach
# new-session inside 5s at all (the stub log was empty). ACP_BRIDGE_INBOX is the
# script's own override for that lookup. Hub resolution is not what this file
# tests, so it is set below. That removes the slow step from this test instead of
# only outwaiting it. test_acp_bridge_supervise_batch.sh keeps the default
# (hub-lookup) launch path covered.
#
# EXIT is timed from the moment of session death (the rm), never from launch. The
# supervisor polls has-session with a fixed `sleep 5`, which has no env knob, so a
# healthy exit takes one poll interval plus a stub-tmux exec, about 5.5s measured.
# The bound is 30s: six poll intervals of load margin, still far under systemd's
# 90s TimeoutStopSec, and it still FAILS on a supervisor that never exits.
STARTUP_BOUND=60
EXIT_BOUND=30

echo "T1: supervise loop exits when its tmux session dies underneath it"

PATH="$STUBDIR:$PATH" HOME="$SANDBOX" ACP_BRIDGE_AGENT="$AGENT" \
  ACP_BRIDGE_INBOX="$SANDBOX/inbox" \
  "$SUPERVISE" &
SCRIPT_PID=$!

# Wait for the session to come up (bounded poll on the readiness marker, not a
# fixed sleep).
up=0
deadline=$((SECONDS + STARTUP_BOUND))
while (( SECONDS < deadline )); do
  if [[ -e "$STUB_TMUX_MARK" ]]; then up=1; break; fi
  # A supervisor that died before new-session never will: fail now, not at 60s.
  kill -0 "$SCRIPT_PID" 2>/dev/null || break
  sleep 0.1
done
if [[ "$up" -eq 1 ]]; then pass "session came up before killing it"
else fail "session never came up within ${STARTUP_BOUND}s (new-session not observed)"; fi

# The bridge's tmux session dies on its own — NOT a systemd stop, no SIGTERM.
# This is the 2026-07-28 shape: the supervisor is untouched and its child is gone.
rm -f "$STUB_TMUX_MARK"

# BOUNDED wait. A supervisor that hangs here is the defect; an unbounded wait for
# it would hang too, so the guard must time out and FAIL rather than block.
exited=0
deadline=$((SECONDS + EXIT_BOUND))
while (( SECONDS < deadline )); do
  if ! kill -0 "$SCRIPT_PID" 2>/dev/null; then exited=1; break; fi
  sleep 0.1
done
if [[ "$exited" -eq 1 ]]; then
  pass "supervise loop exited after its session died"
else
  fail "supervise loop did NOT exit within ${EXIT_BOUND}s of session death — a supervisor \
that outlives its child keeps the unit 'active' and nothing ever restarts it"
fi

# Exit status matters as much as exiting: the script must take its `exit 0` path
# so Restart=always respawns cleanly. A non-zero here would be a different bug
# (and, with no OnFailure= on the unit, an equally silent one).
#
# `wait` ONLY when the bounded poll above already saw the process go. Waiting on a
# process that is still running is an unbounded wait for the exact hang this file
# guards against — found by running the mutation rather than reasoning about it:
# the first draft failed correctly at 15s and then blocked here until an external
# timeout killed it, turning a FAIL into a hang. Never wait on the failure path.
if [[ "$exited" -eq 1 ]]; then
  wait "$SCRIPT_PID"; rc=$?
  SCRIPT_PID=""
  if [[ "$rc" -eq 0 ]]; then
    pass "exited 0 (clean respawn path for Restart=always)"
  else
    fail "expected exit 0 from the session-death path, got rc=$rc"
  fi
else
  kill -KILL "$SCRIPT_PID" 2>/dev/null
  SCRIPT_PID=""
  fail "exit status unavailable — the supervisor never exited (see above)"
fi

# It must have really run the loop against the session it created, not exited for
# some unrelated reason before ever getting there.
if grep -qF "new-session -d -s $SESSION" "$STUB_TMUX_LOG"; then
  pass "tmux new-session -d -s $SESSION was invoked before the wait"
else
  fail "no new-session -d -s $SESSION call observed in stub log"
  echo "  --- stub tmux log ---"
  sed 's/^/  /' "$STUB_TMUX_LOG"
fi

echo
echo "== $PASS passed, $FAIL failed =="
[[ "$FAIL" -eq 0 ]]
