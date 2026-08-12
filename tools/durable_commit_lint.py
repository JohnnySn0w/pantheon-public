#!/usr/bin/env python3
"""durable_commit_lint — structural guard that keeps the T-2334 concurrent-writer
corruption class CLOSED as new tools are added (T-2595, from the T-2335 sweep).

THE HAZARD (T-2334): the git index is per-CLONE, so two executors sharing one
account (e.g. specialist's two seats) share ONE index. A tool that does a bare
`_git("add", "--", <paths>)` followed by a whole-index `_git("commit", ...)`
with NO serialization can commit ANOTHER executor's staged files under this
writer's message — silently corrupting a DURABLE record (the ticket audit chain,
the ledger) whose wrong value is trusted later and is indistinguishable from a
correct one. T-2335 verified the class is closed TODAY (every tickets//ledger/
writer holds git_mutation_lock + a scope-verified commit); this lint keeps it
closed FORWARD, because nothing else enforces it.

STRUCTURAL, not an allowlist (so a new tool cannot silently escape). A tool is
IN SCOPE iff its own body:
  1. authors a DIRECT commit — `_git("add"|"commit", ...)` (NOT the safe
     `commit_scoped` delegate, which holds the lock + scope-verify internally),
     AND
  2. references a durable path — tickets/ or ledger/.
An in-scope tool is a VIOLATION unless it is SAFE:
  - uses git_mutation_lock or commit_scoped (the shared locked primitives), OR
  - is one of the two DOCUMENTED inline exceptions (ticket.py._commit_and_push
    and watch_runner.git_commit_and_push) — both do a direct whole-index commit
    but hold git_mutation_lock AND a diff-cached scope-guard (T-2334/T-2336).

This linter is itself report-only / authors no commits — same exemption class as
auto_commit_contract_lint.py. It excludes test files (their fixtures carry a
bare committer ON PURPOSE) and _git_utils.py (the machinery that DEFINES the
safe primitives). The scan regex is backslash-anchored (`_git\\(`), so this
file's OWN pattern definitions do not self-match (a source `_git\\(` is not a
literal `_git(`).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_TOOLS = Path(__file__).resolve().parent

# authors a DIRECT commit — the anchored `\(` means a source-literal `_git\(`
# (as in these regex definitions) is NOT matched, only a real call `_git(`.
_DIRECT_COMMIT_RE = re.compile(r'_git\(\s*["\'](?:add|commit)["\']')
_DURABLE_RE = re.compile(r'\btickets/|\bledger/')
_SAFE_RE = re.compile(r'git_mutation_lock|commit_scoped')

# The two documented inline exceptions: direct whole-index commit, but each holds
# git_mutation_lock + a scope-guard (they'd pass the _SAFE_RE check anyway; named
# for clarity + robustness if the lock-usage regex ever drifts).
DOCUMENTED_EXCEPTIONS = frozenset({"ticket.py", "watch_runner.py"})
# Never a consumer-violation: the lint itself, and _git_utils (defines the safe
# primitives — it IS the trusted machinery, not a caller of it).
_SELF_EXEMPT = frozenset({"durable_commit_lint.py", "_git_utils.py"})


def lint(tools_dir: Path = _TOOLS) -> list[str]:
    """Return the basenames of tools that commit to a durable path without the
    locked helper. Empty list == clean."""
    violations: list[str] = []
    for py in sorted(tools_dir.glob("*.py")):
        name = py.name
        if name.startswith("test_") or name.startswith("_test") or name in _SELF_EXEMPT:
            continue
        try:
            src = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not (_DIRECT_COMMIT_RE.search(src) and _DURABLE_RE.search(src)):
            continue  # not a direct durable committer
        if _SAFE_RE.search(src) or name in DOCUMENTED_EXCEPTIONS:
            continue  # safe: locked helper, or a documented lock+scope-guard exception
        violations.append(name)
    return violations


def main(argv: list[str] | None = None) -> int:
    violations = lint()
    if not violations:
        print("durable-commit lint: clean — every direct tickets//ledger/ committer "
              "routes through git_mutation_lock/commit_scoped or a documented exception.")
        return 0
    print("VIOLATION (T-2595 / T-2334 class): tool(s) author a DIRECT git add/commit "
          "touching a durable path (tickets//ledger/) WITHOUT git_mutation_lock or "
          "commit_scoped:")
    for v in violations:
        print(f"  {v}")
    print("FIX: route the commit through tools/_git_utils.commit_scoped (holds the "
          "lock + pathspec add + diff-cached scope-verify), OR — if a bespoke inline "
          "pipeline is genuinely required — wrap the add->commit(->push) span in "
          "git_mutation_lock with a scope-guard and add the tool to DOCUMENTED_EXCEPTIONS "
          "with the reason. A bare add+whole-index-commit reintroduces the T-2334 hazard.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
