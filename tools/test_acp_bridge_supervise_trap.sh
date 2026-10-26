#!/usr/bin/env bash
# test_acp_bridge_supervise_trap.sh — regression guard for the T-1689 fix-1
# SIGTERM/EXIT tmux-teardown trap in acp-bridge-supervise.sh.
#
# BLOCKS-PILOT-class defect this guards against: on the pilot account
# (claude-research) the bridge's tmux session lands OUTSIDE the systemd
# unit's cgroup (a tmux server already exists — specialist's TUI), so
# `systemctl --user stop/disable --now` killing only the supervise
# script's MainPID would ORPHAN the live bridge+wrapper -> the exact
# two-consumer split-Maildir hazard the design prevents (T-1689).
#
# This proves the trap actually tears the session down under SIGTERM,
# without a real tmux/sg/python3 dependency: it substitutes a stub `tmux`
# on PATH that records invocations and simulates has-session/new-session/
# kill-session via a marker file, backgrounds the supervise script under a
# sandbox HOME, sends SIGTERM, and asserts the stub logged a kill-session
# call for the expected session name, and that the script actually exits
# (bounded poll — well under systemd's default 90s TimeoutStopSec; the
# poll interval is the worst-case delay since a synchronous `sleep N`
# defers trap execution until that sleep returns).
#
# Run after any edit to acp-bridge-supervise.sh's trap/session logic.
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
    # FAITHFUL TO REAL TMUX (T-2264 follow-up): killing a session that is not
    # there exits 1 ("can't find session"). The old stub used `rm -f`, which
    # never fails — so the supervise script's EXIT-trap-under-`set -e` bug
    # (a failing kill-session overriding `exit 0`) was UNREACHABLE in this
    # harness, and the test passed for two months while every real clean
    # shutdown reported status=1. A stub that cannot fail cannot test the
    # failure path.
    if [[ -e "$MARK" ]]; then
      rm -f "$MARK"
    else
      echo "can't find session: $3" >&2
      exit 1
    fi
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

# Wall-clock bounds (T-4348). Both are $SECONDS deadlines, not iteration counts,
# so a bound means the same thing on an idle box and on the saturated CI runner.
#
# STARTUP waits on a POSITIVE readiness signal (the stub's new-session marker), so
# the 60s bound only caps a broken start and never paces a healthy one. The old 5s
# bound lost to load: before its first new-session the script resolves the mailbox
# hub with a python3 import of agent_role_map, which measured 4-5s by itself on the
# loaded runner. ACP_BRIDGE_INBOX is the script's own override for that lookup.
# This file tests the SIGTERM trap, not hub resolution, so it sets the override and
# drops the slow step. test_acp_bridge_supervise_batch.sh keeps the default
# (hub-lookup) launch path covered.
#
# EXIT is timed from the SIGTERM. A synchronous `sleep 5` in the supervise poll loop
# defers the trap until that sleep returns, so a healthy exit takes up to one poll
# interval plus a stub-tmux exec. The bound is 30s: six poll intervals of load
# margin, still far under systemd's default TimeoutStopSec=90s.
STARTUP_BOUND=60
EXIT_BOUND=30

echo "T1: supervise script tears down its tmux session on SIGTERM"

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
if [[ "$up" -eq 1 ]]; then pass "session came up before signaling"
else fail "session never came up within ${STARTUP_BOUND}s (new-session not observed)"; fi

kill -TERM "$SCRIPT_PID" 2>/dev/null

# Wait for the script to actually exit (bounded; see EXIT_BOUND above).
exited=0
deadline=$((SECONDS + EXIT_BOUND))
while (( SECONDS < deadline )); do
  if ! kill -0 "$SCRIPT_PID" 2>/dev/null; then exited=1; break; fi
  sleep 0.1
done
if [[ "$exited" -eq 1 ]]; then pass "script exited after SIGTERM"
else fail "script did not exit within ${EXIT_BOUND}s of SIGTERM"; fi

# T-2264 follow-up: assert the STATUS, not merely that it terminated.
# This test previously checked only that the process went away, so it passed
# for two months while every clean shutdown reported status=1 — the EXIT trap's
# failing kill-session overriding `exit 0` under `set -e`. systemd reads the
# status, OnFailure= fires on it, and "it exited" is not the property that
# matters. A test that stops one question short of the one the consumer asks
# is how this stayed invisible.
#
# `wait` ONLY when the bounded poll above saw the process go (T-4348). Waiting on
# a still-running script is an unbounded wait, so a supervisor that ignores
# SIGTERM would HANG this test instead of failing it. The session-death suite
# carries the same guard.
if [[ "$exited" -eq 1 ]]; then
  wait "$SCRIPT_PID" 2>/dev/null; SCRIPT_RC=$?
  SCRIPT_PID=""
  # 0 = clean end; 143 = 128+SIGTERM, also a clean systemd stop.
  if [[ "$SCRIPT_RC" -eq 0 || "$SCRIPT_RC" -eq 143 ]]; then
    pass "script exited with a NON-FAILURE status ($SCRIPT_RC)"
  else
    fail "script exited $SCRIPT_RC — a clean shutdown must not report failure (systemd fires OnFailure= on this)"
  fi
else
  kill -KILL "$SCRIPT_PID" 2>/dev/null
  SCRIPT_PID=""
  fail "exit status unavailable — the script never exited after SIGTERM (see above)"
fi

if grep -qF "kill-session -t $SESSION" "$STUB_TMUX_LOG"; then
  pass "tmux kill-session -t $SESSION was invoked"
else
  fail "no kill-session -t $SESSION call observed in stub log"
  echo "  --- stub tmux log ---"
  sed 's/^/  /' "$STUB_TMUX_LOG"
fi

if [[ -e "$STUB_TMUX_MARK" ]]; then
  fail "session marker still present after SIGTERM (orphan-equivalent)"
else
  pass "session torn down (no orphaned marker)"
fi

echo
echo "T2: the systemd unit tells systemd that 143 is a clean stop (T-2543)"
# The exit-code assertion above proves the SCRIPT reports 143 on SIGTERM, but
# systemd fires OnFailure= on 143 UNLESS the unit whitelists it. That gap is
# exactly how T-2543 happened: T1 was green for months while every graceful
# restart still paged a spurious "unit FAILED". Close the loop the consumer
# actually reads — the unit file — not just the script's exit code.
UNIT="$HERE/systemd/pantheon-acp-bridge@.service"
if [[ -f "$UNIT" ]]; then
  pass "unit template present at $UNIT"
  # A SuccessExitStatus line that lists 143 (space/comma-separated, in any order).
  if grep -qE '^[[:space:]]*SuccessExitStatus=([^#]*[[:space:]])?143([[:space:]]|,|$)' "$UNIT"; then
    pass "unit declares SuccessExitStatus including 143 (systemd will not fire OnFailure= on a clean stop)"
  else
    fail "unit does NOT whitelist exit 143 — a graceful restart will still trip OnFailure= (cry-wolf)"
  fi
else
  fail "unit template not found at $UNIT"
fi

echo
echo "== $PASS passed, $FAIL failed =="
[[ "$FAIL" -eq 0 ]]
