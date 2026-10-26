#!/usr/bin/env bash
# test_ensure_hooks_active.sh - T-4427: the SessionStart hook hooks/ensure-hooks-active.sh
# repairs an UNSET core.hooksPath and stays silent for every value that really runs the
# committed hooks. That includes a LINKED worktree whose path is its main worktree's
# absolute .githooks (the shared config Claude Code's isolation worktrees write). It WARNS
# on, and never clobbers, a value that runs something else. It always exits 0.
#
# Runs the real hook with HOME pointed at a temp dir holding throwaway repos.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
HOOK="$HERE/hooks/ensure-hooks-active.sh"
T="$(realpath "$(mktemp -d)")"
[[ -n "$T" && -d "$T" && "$T" != / ]] || { echo "FAIL: could not create a temp dir"; exit 1; }
trap 'rm -rf "$T"' EXIT
export GIT_CONFIG_NOSYSTEM=1 GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
H="$T/home"

pass=0; fail=0
check() { # <name> <got> <want>
  if [ "$2" = "$3" ]; then pass=$((pass + 1)); else fail=$((fail + 1)); echo "FAIL: $1 (got '$2', want '$3')"; fi
}
has() { grep -cF -e "$2" <<<"$1"; } # <text> <literal>: prints the matching line count
mkrepo() { # <dir>: a repo that commits .githooks/{pre-commit,commit-msg,pre-push}
  mkdir -p "$1/.githooks"
  git init -q -b main "$1"
  for h in pre-commit commit-msg pre-push; do printf '#!/bin/sh\nexit 0\n' > "$1/.githooks/$h"; chmod +x "$1/.githooks/$h"; done
  git -C "$1" add .githooks && git -C "$1" -c core.hooksPath=/dev/null commit -q -m init
}
reset() { # $H/main plus two linked worktrees ($H/wt, $H/wt-b); a second main OUTSIDE $HOME with a worktree inside
  rm -rf "${T:?}/home" "${T:?}/outside"; mkdir -p "$H" "$T/outside"
  mkrepo "$H/main"
  git -C "$H/main" worktree add -q -b wt "$H/wt"
  git -C "$H/main" worktree add -q -b wt-b "$H/wt-b"
  mkrepo "$T/outside/main2"
  git -C "$T/outside/main2" worktree add -q -b wt2 "$H/wt2"
}
run() { out="$(HOME="$H" bash "$HOOK" </dev/null 2>&1)"; rc=$?; } # sets $out and $rc
hp() { git -C "$1" config --local core.hooksPath; } # the value a repo/worktree reads

# 1. unset: repaired to .githooks, and the linked worktrees (shared config) inherit it
reset
git -C "$T/outside/main2" config core.hooksPath .githooks
run
check "unset: exits 0" "$rc" "0"
check "unset: repaired" "$(hp "$H/main")" ".githooks"
check "unset: the repair is reported once, for main" "$(has "$out" "was UNSET in $H/main")" "1"
check "unset: the worktrees read the repaired value" "$(hp "$H/wt")" ".githooks"
check "unset: nothing else is reported" "$(grep -c . <<<"$out")" "2"

# 2. relative .githooks: silent everywhere
reset
git -C "$H/main" config core.hooksPath .githooks; git -C "$T/outside/main2" config core.hooksPath .githooks
run
check "relative: exits 0" "$rc" "0"
check "relative: silent" "$out" ""

# 3. the main worktree's ABSOLUTE .githooks in the shared config: silent for main AND its
#    linked worktrees, including one whose main lives outside $HOME (the T-4427 false positive)
reset
git -C "$H/main" config core.hooksPath "$H/main/.githooks"
git -C "$T/outside/main2" config core.hooksPath "$T/outside/main2/.githooks"
run
check "main-absolute: exits 0" "$rc" "0"
check "main-absolute: silent (no WARN for the linked worktrees)" "$out" ""
check "main-absolute: not rewritten" "$(hp "$H/wt")" "$H/main/.githooks"

# 4. a foreign value: WARN for the main and each of its worktrees, and never clobber
reset
git -C "$H/main" config core.hooksPath "$T/elsewhere/hooks"; git -C "$T/outside/main2" config core.hooksPath .githooks
run
check "foreign: exits 0" "$rc" "0"
check "foreign: main warns" "$(has "$out" "core.hooksPath in $H/main is '$T/elsewhere/hooks'")" "1"
check "foreign: worktree warns" "$(has "$out" "core.hooksPath in $H/wt is '$T/elsewhere/hooks'")" "1"
check "foreign: not clobbered" "$(hp "$H/main")" "$T/elsewhere/hooks"

# 5. a SIBLING worktree's absolute .githooks is not the main's: still a WARN. The fix must
#    not widen into accepting any */.githooks.
reset
git -C "$H/main" config core.hooksPath "$H/wt-b/.githooks"; git -C "$T/outside/main2" config core.hooksPath .githooks
run
check "sibling: main warns" "$(has "$out" "core.hooksPath in $H/main is '$H/wt-b/.githooks'")" "1"
check "sibling: the other worktree warns" "$(has "$out" "core.hooksPath in $H/wt is '$H/wt-b/.githooks'")" "1"

# 5b. a repo whose git dir is NOT <root>/.git (--separate-git-dir) has no main root to
#     compare against, so "/.githooks" must still warn
reset
git -C "$H/main" config core.hooksPath .githooks; git -C "$T/outside/main2" config core.hooksPath .githooks
mkdir -p "$H/sep/.githooks"; printf '#!/bin/sh\n' > "$H/sep/.githooks/pre-commit"; chmod +x "$H/sep/.githooks/pre-commit"
git init -q --separate-git-dir "$T/outside/sep.gitdir" "$H/sep"
git -C "$H/sep" config core.hooksPath /.githooks
run
check "separate git dir: '/.githooks' warns" "$(has "$out" "core.hooksPath in $H/sep is '/.githooks'")" "1"

# 6. hook files that lost their exec bit are made executable again
reset
git -C "$H/main" config core.hooksPath .githooks; git -C "$T/outside/main2" config core.hooksPath .githooks
chmod -x "$H/main/.githooks/pre-push"
run
check "exec bit restored" "$([ -x "$H/main/.githooks/pre-push" ] && echo yes)" "yes"

echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
