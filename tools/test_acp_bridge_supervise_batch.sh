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
# no real tmux/sg/python3. The stub records the new-session invocation; we inspect
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
  if [[ "$spec" == "UNSET" ]]; then
    env -u ACP_BRIDGE_BATCH PATH="$stubdir:$PATH" HOME="$sandbox" ACP_BRIDGE_AGENT="batchtest" "$SUPERVISE" &
  else
    env ACP_BRIDGE_BATCH="$spec" PATH="$stubdir:$PATH" HOME="$sandbox" ACP_BRIDGE_AGENT="batchtest" "$SUPERVISE" &
  fi
  pid=$!
  for _ in $(seq 1 50); do [[ -s "$log" ]] && { up=1; break; }; sleep 0.1; done
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
if grep -qF -- '--batch' <<<"$out"; then fail "ACP_BRIDGE_BATCH=0 STILL added --batch — opt-out BROKEN (out: $out)"
else pass "ACP_BRIDGE_BATCH=0 -> --batch absent (opt-out honored)"; fi

echo
echo "== $PASS passed, $FAIL failed =="
[[ "$FAIL" -eq 0 ]]
