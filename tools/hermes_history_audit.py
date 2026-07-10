"""Whole-history HERMES byte-sequence audit.

Scans every commit in the git history of a given repo for the unescaped
9-byte trigger phrase by running `git log --format=%H` to enumerate all
commit SHAs, then `git show <sha> -p` per commit and inspecting only the
added (`+`-prefix) diff lines.

Public surface:
    run_history_audit(repo_root) — scan the repo; return a single
        plain-text digest line suitable for appending to Recovery's grooming
        digest.

Idempotency: this module is read-only. It calls `git log` and `git show`
but never writes, stages, or commits anything. Running multiple times
against an unchanged repo produces the same line.

Self-auditing property: once this module is committed to pantheon-config,
the next grooming fire will include this file itself in the `git log -p`
scan. That is intentional and safe — the trigger bytes never appear
contiguously in this source (the pattern is imported from `_git_utils.py`,
which uses the same convention). Future readers who notice the apparent
recursion: it resolves correctly.

Scan scope:
    ALL commits, no date/author filter, no recency window. Broad-by-default
    per the project directive (msg_382). Only added lines (+ prefix) are
    examined — context and removed lines are skipped — so the report
    identifies the *introducing* commit, not every commit that ever
    mentioned the bytes.

Performance:
    ~N subprocess calls for N commits. For pantheon-config (~70 commits as
    of 2026-05-02) this is fast (<1s). A structured warning is appended if
    wall-clock time exceeds 30s; no cap is applied.

Source-cleanliness convention:
    The trigger byte sequence (H E R M E S . m d) is never written
    contiguously in this source file. `_HERMES_PATTERN` is imported from
    `tools._git_utils`, which assembles it via `re.compile(rb"HERMES\\.md")`
    — the backslash escapes the dot in the regex but the literal contiguous
    9-byte sequence is still absent from source because the backslash is
    present. See that module's docstring for the full rationale.

    Verification: `grep -cF` for the unescaped trigger against this file
    must return 0.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap sys.path so `tools._git_utils` is importable whether this module
# is invoked as a script or imported as part of the `tools` package.
# ---------------------------------------------------------------------------
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._git_utils import _HERMES_PATTERN  # noqa: E402 — after sys.path bootstrap

# Maximum number of dirty-commit SHAs to display inline before truncating.
_SHA_DISPLAY_CAP = 5

# Wall-clock threshold in seconds; exceeded → append a warning to the line.
_PERF_WARNING_THRESHOLD_S = 30.0


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """Thin subprocess wrapper; binary output (no text=True). Caller inspects
    `.returncode` and `.stdout`/`.stderr` for domain-specific handling.
    """
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        check=False,
        # Binary output: _HERMES_PATTERN operates on bytes directly, avoiding
        # a text-decode + re-encode round-trip and matching the convention used
        # in _hermes_scan_file.
    )


def _scan_show_output(raw: bytes) -> bool:
    """Return True iff the `git show -p` output for one commit contains the
    unescaped trigger on at least one added line.

    Only lines whose first byte is `+` (added lines) are examined. The `+++`
    file-header lines start with `+++` and their second byte is also `+`, so
    we guard against them by checking that the byte after the leading `+` is
    NOT `+`.

    Context lines (` ` prefix) and removed lines (`-` prefix) are skipped.
    This means the report surfaces the *introducing* commit, not every commit
    that ever had the bytes in its diff context.
    """
    for line in raw.splitlines():
        # Added line: starts with b'+' but NOT b'++' (which is the +++ header).
        if line.startswith(b"+") and not line.startswith(b"++"):
            if _HERMES_PATTERN.search(line[1:]):
                return True
    return False


def run_history_audit(repo_root: Path) -> str:
    """Scan all commits in `repo_root` for the unescaped HERMES trigger on
    added diff lines. Returns a single plain-text line.

    Format (clean):
        HERMES audit: 0 unescaped occurrence(s) across 74 commits scanned.

    Format (dirty, up to 5 SHAs):
        HERMES audit: 2 unescaped occurrence(s) in 2 commit(s): abc1234, def5678. (74 commits scanned)

    Format (dirty, more than 5 SHAs):
        HERMES audit: 7 unescaped occurrence(s) in 7 commit(s): a, b, c, d, e and 2 more. (74 commits scanned)

    Format (git log failure):
        HERMES audit: ERROR: git log failed: <detail>

    A timing warning is appended if the scan takes longer than 30s:
        ... (WARNING: scan took 42s; commit-count growth may impact grooming)
    """
    t_start = time.monotonic()

    # Step 1: enumerate all commit SHAs.
    res = _git("log", "--format=%H", cwd=repo_root)
    if res.returncode != 0:
        detail = res.stderr.decode("utf-8", errors="replace").strip() or res.stdout.decode("utf-8", errors="replace").strip()
        return f"HERMES audit: ERROR: git log failed: {detail}"

    raw_log = res.stdout.decode("utf-8", errors="replace")
    shas = [s.strip() for s in raw_log.splitlines() if s.strip()]
    total = len(shas)

    # Step 2: scan each commit's diff for added-line violations.
    dirty_shas: list[str] = []
    scanned = 0

    for sha in shas:
        show_res = _git("show", sha, "-p", cwd=repo_root)
        if show_res.returncode != 0:
            # Skip this commit; don't halt the whole scan.
            continue
        scanned += 1
        if _scan_show_output(show_res.stdout):
            dirty_shas.append(sha[:7])  # short SHA (7 chars) for readability

    t_elapsed = time.monotonic() - t_start

    # Step 3: format the output line.
    n_occurrences = len(dirty_shas)

    if n_occurrences == 0:
        line = f"HERMES audit: 0 unescaped occurrence(s) across {scanned} commit(s) scanned."
    else:
        n_shown = min(n_occurrences, _SHA_DISPLAY_CAP)
        sha_list = ", ".join(dirty_shas[:n_shown])
        overflow = n_occurrences - n_shown
        if overflow > 0:
            sha_list += f" and {overflow} more"
        line = (
            f"HERMES audit: {n_occurrences} unescaped occurrence(s) "
            f"in {n_occurrences} commit(s): {sha_list}. "
            f"({scanned} commit(s) scanned)"
        )

    # Step 4: append performance warning if needed.
    if t_elapsed > _PERF_WARNING_THRESHOLD_S:
        elapsed_s = int(t_elapsed)
        line += f" (WARNING: scan took {elapsed_s}s; commit-count growth may impact grooming)"

    return line


if __name__ == "__main__":
    import sys as _sys
    root = Path(_sys.argv[1]) if len(_sys.argv) > 1 else Path(__file__).resolve().parent.parent
    print(run_history_audit(root))
