#!/bin/bash
# Rehearsal for tools/t2693_hub_cutover.sh (T-2693): runs the REAL copy,
# verify-copy, cut, verify and rollback stages on temp hubs owned by the invoking
# account (T2693_* overrides), with ACLs shaped like the live hub, and asserts
# where every message ends up. pull/units/configs touch other accounts and are
# not rehearsed here (configs has its own unit tests: test_t2693_config_roots.py).
#
# Invocation: bash tools/test_t2693_hub_cutover.sh    Exit: 0 = all pass.
set -u
SUT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/t2693_hub_cutover.sh"
PASS=0; FAIL=0
T=$(mktemp -d -t t2693_rehearsal.XXXXXX); trap 'rm -rf "$T"' EXIT
export T2693_OLD="$T/old" T2693_NEW="$T/new" T2693_OLD_OWNER="$(id -un)" T2693_NEW_OWNER="$(id -un)" T2693_GROUP="$(id -gn)"
RECIPIENTS=(orchestrator specialist specialist-a2a recovery codex archivist mason artisan the-ancestor)

ok()   { PASS=$((PASS+1)); }
bad()  { FAIL=$((FAIL+1)); echo "FAIL: $*"; }
check() { local label="$1"; shift; if "$@"; then ok; else bad "$label"; fi; }
stage() { bash "$SUT" "$@" > "$T/last.out" 2>&1; local rc=$?; [[ $rc == 0 ]] || { echo "--- stage $* rc=$rc"; cat "$T/last.out"; }; return $rc; }

# Live-shaped old hub: root 0710 + a named --x; recipient dirs 2770, group::---,
# named rwx + default ACLs. NUMERIC uids (4242/4243), not account names: the CI
# runner has none of this host's accounts, and setfacl accepts a uid with no passwd
# entry. If the filesystem or host can't hold ACLs at all, only the ACL assertions
# are skipped; the copy/cut/rollback logic is still rehearsed.
mkdir -p "$T2693_OLD" "$T2693_NEW"; chmod 0710 "$T2693_OLD"; chmod 2770 "$T2693_NEW"
ACL_OK=0
if command -v setfacl >/dev/null && command -v getfacl >/dev/null \
   && setfacl -m u:4243:--x "$T2693_OLD" 2>/dev/null \
   && getfacl -n -p "$T2693_OLD" 2>/dev/null | grep -q '^user:4243:--x'; then ACL_OK=1
else echo "SKIP: no ACL support here -- ACL assertions skipped, logic still rehearsed"; fi
for r in "${RECIPIENTS[@]}"; do
  for d in "" /new /cur /tmp; do
    mkdir -p "$T2693_OLD/$r$d"; chmod 2770 "$T2693_OLD/$r$d"
    (( ACL_OK )) && setfacl -m g::---,u:4242:rwx,d:u:4242:rwx,d:g::--- "$T2693_OLD/$r$d"
  done
done
echo m1 > "$T2693_OLD/orchestrator/new/m1"; for c in c1 c2 c3; do echo $c > "$T2693_OLD/orchestrator/cur/$c"; done
echo w1 > "$T2693_OLD/recovery/new/w1"

check "copy stage" stage copy
check "verify-copy: tree + ACLs identical" stage verify-copy
check "copy refuses a non-empty new hub" bash -c "! bash '$SUT' copy >/dev/null 2>&1"

# Traffic on the OLD hub after the copy: an arrival (orchestrator m2) and an ack (recovery w1).
echo m2 > "$T2693_OLD/orchestrator/new/m2"
mv "$T2693_OLD/recovery/new/w1" "$T2693_OLD/recovery/cur/w1"
echo m5 > "$T2693_OLD/orchestrator/cur/m5"   # arrived AND acked inside the copy->cut window

check "cut stage" stage cut
check "old orchestrator new/ renamed away" test ! -e "$T2693_OLD/orchestrator/new"
check "old orchestrator kept under the mark" test -f "$T2693_OLD/orchestrator/new.pre-post-office-cut/m2"
check "post-copy arrival carried to the new hub" test -f "$T2693_NEW/orchestrator/new/m2"
check "pre-copy unread still unread on the new hub" test -f "$T2693_NEW/orchestrator/new/m1"
check "post-copy ack reconciled (cur)" test -f "$T2693_NEW/recovery/cur/w1"
check "post-copy ack reconciled (not new)" test ! -e "$T2693_NEW/recovery/new/w1"
check "window-acked message kept in the new cur (history/stats)" test -f "$T2693_NEW/orchestrator/cur/m5"
check "cut is idempotent" stage cut
check "verify stage (old dead, negative send raises, new inboxes exist)" stage verify

# Traffic on the NEW hub before a rollback: orchestrator acks m1, a new m3 arrives,
# and m4 arrives and is acked entirely on the new hub.
mv "$T2693_NEW/orchestrator/new/m1" "$T2693_NEW/orchestrator/cur/m1"
echo m3 > "$T2693_NEW/orchestrator/new/m3"
echo m4 > "$T2693_NEW/orchestrator/cur/m4"

check "rollback stage" stage rollback
check "old orchestrator new/ restored" test -d "$T2693_OLD/orchestrator/new"
check "mark gone after restore" test ! -e "$T2693_OLD/orchestrator/new.pre-post-office-cut"
check "ack on the new hub carried back (m1 in old cur)" test -f "$T2693_OLD/orchestrator/cur/m1"
check "... and not replayed (m1 not in old new)" test ! -e "$T2693_OLD/orchestrator/new/m1"
check "unread post-cut arrival carried back (m3)" test -f "$T2693_OLD/orchestrator/new/m3"
check "post-copy arrival still unread (m2)" test -f "$T2693_OLD/orchestrator/new/m2"
check "new-hub-only acked message kept (m4 in old cur)" test -f "$T2693_OLD/orchestrator/cur/m4"
if (( ACL_OK )); then
  check "old ACLs survive the round trip" bash -c "getfacl -n -p '$T2693_OLD/orchestrator/new' 2>/dev/null | grep -q '^user:4242:rwx'"
  check "copy carried the named ACL to the new hub" bash -c "getfacl -n -p '$T2693_NEW/orchestrator/cur' 2>/dev/null | grep -q '^user:4242:rwx'"
fi

echo
echo "t2693_hub_cutover rehearsal: $PASS passed, $FAIL failed"
(( FAIL == 0 ))
