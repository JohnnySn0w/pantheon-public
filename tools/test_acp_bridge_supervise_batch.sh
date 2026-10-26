#!/usr/bin/env bash
# test_acp_bridge_supervise_batch.sh — regression guard for the T-3025 batch-default
# gate in acp-bridge-supervise.sh.
#
# T-3025 flipped ACP_BRIDGE_BATCH from opt-in (default OFF) to default-ON fleet-wide
# (BRIDGE_BATCH="${ACP_BRIDGE_BATCH:-1}"), after a clean ~34h soak on specialist. Because
# the default is now NON-EMPTY, the enable gate had to change from `[ -n "$BRIDGE_BATCH" ]`
# (which treats the "0" opt-out as on) to `[ "$BRIDGE_BATCH" != "0" ]`, with
# ACP_BRIDGE_BATCH=0 the explicit opt-out. This pins the contract in both directions:
#   unset/1 -> --batch present ;  0 -> --batch absent.
#
# MUTATION-CHECKED (shown to fail): reverting the gate to `-n` breaks the opt-out
# (the ACP_BRIDGE_BATCH=0 case then still gets --batch and FAILS); reverting the
# default to `${...:-}` (off) breaks default-on (the unset case FAILS). Point
# ACP_SUPERVISE_UNDER_TEST at a mutated copy to reproduce.
#
# HARNESS: same stub-tmux + sandbox-HOME pattern as the trap/session_death tests —
# no real tmux or sg, and no bridge python3. The supervise script's own python3
# mailbox-hub lookup DOES run here on purpose (T-4348): this is the suite that keeps
# the default launch path covered. The stub records the new-session invocation; we inspect
# the captured command for the --batch flag per ACP_BRIDGE_BATCH value, then KILL the
# supervise loop (we only need the one-shot launch argv, not the lifecycle).
#
# Run after any edit to acp-bridge-supervise.sh's BRIDGE_BATCH gate. Exit 0 == passed.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUPERVISE="${ACP_SUPERVISE_UNDER_TEST:-$HERE/acp-bridge-supervise.sh}"
[[ -x "$SUPERVISE" ]] || { echo "FATAL: supervise script not executable at $SUPERVISE"; exit 1; }

PASS=0; FAIL=0
pass() { echo "  PASS: $1"; PASS=$((PASS+1)); }
fail() { echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

# Launch the supervise script once under the stub tmux, capture the new-session
# command line it builds, then kill the loop. $1 = "UNSET" or a literal value for
# ACP_BRIDGE_BATCH. Echoes the captured new-session line ("__NONE__" if never launched).
capture_new_session() {
  local spec="$1" sandbox stubdir mark log pid up=0
  sandbox="$(mktemp -d)"; stubdir="$sandbox/stubbin"; mkdir -p "$stubdir"
  mark="$sandbox/mark"; log="$sandbox/tmux.log"; : > "$log"
  cat > "$stubdir/tmux" <<STUB
#!/usr/bin/env bash
case "\$1" in
  has-session)  [[ -e "$mark" ]] ;;
  new-session)  echo "new-session \$*" >> "$log"; touch "$mark" ;;
  kill-session) rm -f "$mark" ;;
  *) exit 1 ;;
esac
STUB
  chmod +x "$stubdir/tmux"
  # The supervise script's output goes to a sandbox file and NEVER to this
  # function's stdout (T-4348). The caller reads this function through $(...), and
  # $(...) returns only when every holder of its pipe closes it. Killing $pid does
  # not kill that script's children (its poll-loop `sleep 5`, or a stuck step
  # before new-session), so an inherited pipe kept this test blocked. That cost
  # about 5s per case when healthy and was an unbounded HANG when the supervisor
  # never reached new-session. Mutation-found.
  if [[ "$spec" == "UNSET" ]]; then
    env -u ACP_BRIDGE_BATCH PATH="$stubdir:$PATH" HOME="$sandbox" ACP_BRIDGE_AGENT="batchtest" "$SUPERVISE" >"$sandbox/supervise.out" 2>&1 &
  else
    env ACP_BRIDGE_BATCH="$spec" PATH="$stubdir:$PATH" HOME="$sandbox" ACP_BRIDGE_AGENT="batchtest" "$SUPERVISE" >"$sandbox/supervise.out" 2>&1 &
  fi
  pid=$!
  # Bounded wait on the POSITIVE readiness signal: the stub's new-session log line
  # (T-4348). Deliberately NO ACP_BRIDGE_INBOX override. This suite keeps the
  # DEFAULT launch path covered, including the python3 mailbox-hub lookup, while
  # the trap and session_death suites skip that path. The lookup measured 4-5s on
  # the loaded runner by itself, so the old 5s (50 x 0.1s) poll was a timing guess
  # that load could beat. The bound is 60s wall-clock ($SECONDS, not an iteration
  # count), and it only caps a launch that never happens.
  local deadline=$((SECONDS + 60))
  # A supervisor that died before new-session never will, so the kill -0 check
  # breaks out at once instead of spending the whole 60s.
  while (( SECONDS < deadline )); do
    [[ -s "$log" ]] && { up=1; break; }
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.1
  done
  # Reap the children first (see above), then the script itself.
  pkill -KILL -P "$pid" 2>/dev/null
  kill -KILL "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  if [[ "$up" -eq 1 ]]; then cat "$log"; else echo "__NONE__"; fi
  rm -rf "$sandbox"
}

echo "T1: ACP_BRIDGE_BATCH default-on + opt-out gate (T-3025)"

out="$(capture_new_session UNSET)"
if grep -qF -- '--batch' <<<"$out"; then pass "default (unset) -> --batch present (default-on)"
else fail "default (unset) did NOT add --batch (out: $out)"; fi

out="$(capture_new_session 1)"
if grep -qF -- '--batch' <<<"$out"; then pass "ACP_BRIDGE_BATCH=1 -> --batch present"
else fail "=1 did NOT add --batch (out: $out)"; fi

out="$(capture_new_session 0)"
# An absence assertion must first prove there was a launch to inspect (T-4348).
# Without this guard a launch that never happened ("__NONE__") has no --batch
# either, so this case would PASS vacuously on a supervisor that never starts.
if [[ "$out" == "__NONE__" ]]; then fail "ACP_BRIDGE_BATCH=0: supervise never reached new-session, so there was no launch argv to inspect"
elif grep -qF -- '--batch' <<<"$out"; then fail "ACP_BRIDGE_BATCH=0 STILL added --batch — opt-out BROKEN (out: $out)"
else pass "ACP_BRIDGE_BATCH=0 -> --batch absent (opt-out honored)"; fi

echo
echo "== $PASS passed, $FAIL failed =="
[[ "$FAIL" -eq 0 ]]
