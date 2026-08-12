#!/usr/bin/env bash
# run_shell_suites.sh — execute every tools/test_*.sh and fail on any failure (T-2330).
#
# WHY THIS EXISTS. CI runs `python -m pytest tools/ -q`, which collects test_*.py
# and nothing else. Six shell suites — 78 cases guarding the restart path, the
# submit path, the bridge supervisor and the boot-health checker — were executed
# by NOTHING: not CI, not `unittest discover`, not any Python test. The only
# reference to one of them anywhere in tools/*.py was a comment.
#
# The cost was not hypothetical. test_handoff_and_restart.sh sat at 4/33 through a
# commit, a night, and several full-suite runs (T-2319), and test_verify_startup.sh
# was unrun for 39 days while passing on exactly one account (T-2322). A green
# "4400 tests, 0 failures" was true and said nothing about any of it.
#
# DISCOVERY IS BY GLOB, ON PURPOSE. A hardcoded list is the same defect one level
# up: a new suite would be added, not listed, and go unwatched exactly as these
# did. tools/test_shell_suite_coverage.py asserts that this script is glob-driven
# and that CI invokes it, so neither half can quietly regress.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 2
shopt -s nullglob
suites=(tools/test_*.sh)

if (( ${#suites[@]} == 0 )); then
  # An empty run must NEVER be a pass. A glob that matches nothing looks
  # identical to a clean sweep, and "0 suites, all green" is the shape of a
  # broken harness reporting success.
  echo "run_shell_suites: FOUND NO tools/test_*.sh — refusing to report success" >&2
  exit 2
fi

pass=0; fail=0; failed=()
for s in "${suites[@]}"; do
  printf '=== %s\n' "$s"
  if bash "$s"; then
    pass=$((pass+1))
  else
    rc=$?
    fail=$((fail+1)); failed+=("$s (exit $rc)")
  fi
done

echo
echo "shell suites: ${pass} passed, ${fail} failed (of ${#suites[@]} discovered)"
if (( fail )); then
  printf '  FAILED: %s\n' "${failed[@]}"
  exit 1
fi
exit 0
