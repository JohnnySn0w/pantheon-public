"""Scan git unified-diff output for byte-sequence discipline violations.

Used by `.github/workflows/hermes-audit.yml` to enforce the HERMES byte-sequence
discipline on the server side. Pairs with `.githooks/pre-commit` (local) and
`tools/watch_runner.py` (auto-commit path) to form the three-layer prevention
stack documented in `incidents/2026-05-02-hermes-contamination/README.md`.

Public surface:
    Violation       — dataclass: path + lineno of a detected violation.
    scan_diff_lines — parse unified-diff text; return list[Violation].
    format_annotation — render a GitHub Actions `::error` annotation string.
    main            — CLI entry point; reads diff from stdin or file path arg;
                      exits 0 (clean) or 1 (violations found).

Source-cleanliness convention:
    The trigger byte sequence is never written contiguously in this file.
    `_HERMES_PATTERN` is imported from `tools._git_utils`, where the same
    convention applies. This protects `git log -p` of pantheon-config from
    re-introducing the exact byte sequence per the standing rule in
    `/home/claude/CLAUDE.md`.

    Force-push edge case: when `github.event.before` is a valid SHA but the
    push rewound history, `git diff <before> HEAD` may include context lines
    for commits being abandoned. Because this scanner only flags lines with a
    `+` prefix (added lines), removed / context lines are correctly ignored.
    The scanner is therefore safe on force-pushes without special handling.

Verification: `grep -cF` for the unescaped trigger against this file must
return 0.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List

# ---------------------------------------------------------------------------
# Bootstrap: ensure repo root is on sys.path so tools._git_utils is importable
# regardless of cwd (mirrors the approach in .githooks/pre-commit).
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from tools._git_utils import _HERMES_PATTERN  # noqa: E402

# Matches the `+++ b/<path>` header line in unified diff output to extract the
# file path. Also handles `+++ /dev/null` (deleted files, no added lines follow
# that matter) and new-file mode `+++ b/<path>`.
_PLUS_PLUS_PLUS = re.compile(r"^\+\+\+ (?:b/)?(.+)$")

# Matches hunk headers: `@@ -l,s +l,s @@` — captures the new-file start line.
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass
class Violation:
    """A single detected byte-sequence discipline violation in a diff."""
    path: str
    lineno: int


def scan_diff_lines(diff_text: str) -> List[Violation]:
    """Parse unified-diff text and return all violations on added lines.

    Only `+`-prefixed (added) lines are examined. Removed lines (`-` prefix)
    and context lines (` ` prefix or no prefix) are skipped — this is correct
    behaviour because the discipline applies to bytes being introduced, not
    bytes being removed.

    Args:
        diff_text: The full output of `git diff <base> HEAD` or equivalent.

    Returns:
        A list of Violation objects (path + lineno) in order of appearance.
        Empty list means the diff is clean.
    """
    violations: List[Violation] = []
    current_file: str | None = None
    current_lineno: int = 0  # next new-file line number to assign

    for raw_line in diff_text.splitlines():
        # --- file header: `+++ b/<path>` ---
        m = _PLUS_PLUS_PLUS.match(raw_line)
        if m:
            path = m.group(1)
            # Ignore deleted-file indicator; no added lines will follow.
            current_file = None if path == "/dev/null" else path
            current_lineno = 0
            continue

        # --- hunk header: `@@ -l,s +l,s @@` ---
        m = _HUNK_HEADER.match(raw_line)
        if m:
            current_lineno = int(m.group(1))
            continue

        # --- diff content lines ---
        if raw_line.startswith("+"):
            # Added line — the content starts at raw_line[1:].
            content = raw_line[1:].encode("utf-8", errors="replace")
            if current_file is not None and _HERMES_PATTERN.search(content):
                violations.append(Violation(path=current_file, lineno=current_lineno))
            current_lineno += 1
        elif raw_line.startswith("-"):
            # Removed line — do not advance new-file line counter.
            pass
        elif raw_line.startswith(" "):
            # Context line — advance line counter.
            current_lineno += 1
        # Lines without a leading +/- or space are diff metadata (index, mode,
        # Binary files differ, etc.) — skip, don't advance counter.

    return violations


def format_annotation(v: Violation) -> str:
    """Format a GitHub Actions workflow annotation for a violation.

    Uses the `::error file=...,line=...::` syntax so the violation appears
    inline in the PR diff view. Message wording mirrors the local pre-commit
    hook output for consistency.

    See: https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/workflow-commands-for-github-actions#setting-an-error-message
    """
    # Escape the period in the advice string so the advice text itself is clean
    # per the byte-sequence discipline (same convention as all other tooling).
    msg = (
        f"HERMES discipline violation — {v.path}:{v.lineno} — "
        "Escape the period (HERMES\\.md) per CLAUDE.md byte-sequence discipline."
    )
    return f"::error file={v.path},line={v.lineno}::{msg}"


def main() -> int:
    """CLI entry point.

    Usage:
        git diff <base> HEAD | python3 tools/hermes_diff_scan.py
        python3 tools/hermes_diff_scan.py <diff-file>

    Exits 0 if no violations found, 1 if any violations found.
    Output (stderr):
        Annotations on stdout (captured by GitHub Actions runner for inline PR
        comments). Human-readable summary on stderr.
    """
    if len(sys.argv) > 1:
        diff_text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
    else:
        diff_text = sys.stdin.read()

    violations = scan_diff_lines(diff_text)

    if not violations:
        return 0

    # Emit GitHub Actions annotations on stdout.
    for v in violations:
        print(format_annotation(v))

    # Emit human-readable summary on stderr (matches local hook style).
    print("", file=sys.stderr)
    print("HERMES discipline violation — push refused.", file=sys.stderr)
    print("", file=sys.stderr)
    for v in violations:
        print(f"  {v.path}:{v.lineno}", file=sys.stderr)
    print("", file=sys.stderr)
    print(
        "  Escape the period (HERMES\\.md) per CLAUDE.md byte-sequence discipline.",
        file=sys.stderr,
    )
    print("  Fix the file(s), amend / re-push.", file=sys.stderr)
    print("", file=sys.stderr)

    return 1


if __name__ == "__main__":
    sys.exit(main())
