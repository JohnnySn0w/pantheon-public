"""Whole-history HERMES byte-sequence audit.

Scans every commit in the git history of a given repo for the unescaped
9-byte trigger phrase by running `git log --format=%H` to enumerate all
commit SHAs, then `git show <sha> -p --text --diff-merges=remerge` per commit
(per-parent `separate` for octopus merges) and inspecting only the added
(`+`-prefix) diff lines. See `_SHOW_ARGS`. A merge is thus charged only with
the bytes its resolution introduced; a clean merge is never counted.

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
    wall-clock time exceeds 30s; no cap is applied. By 2026-09-29 it was
    30k commits and 389s, against grooming's 10-min TimeoutStartSec (T-4308).
    So an opt-in `cache_path` remembers the SHAs already scanned CLEAN (see
    `_cache_key`). A commit is immutable, so a cached clean verdict under the
    same scan logic equals a rescan: the scope stays every commit.

Source-cleanliness convention:
    The trigger byte sequence (H E R M E S . m d) is never written
    contiguously in this source file. The matching pattern lives in
    `tools._git_utils` (`_HERMES_PATTERN`, reached through
    `tools.hermes_diff_scan`), which assembles it via `re.compile(rb"HERMES\\.md")`
    — the backslash escapes the dot in the regex but the literal contiguous
    9-byte sequence is still absent from source because the backslash is
    present. See that module's docstring for the full rationale.

    Verification: `grep -cF` for the unescaped trigger against this file
    must return 0.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap sys.path so `tools._git_utils` is importable whether this module
# is invoked as a script or imported as part of the `tools` package.
# ---------------------------------------------------------------------------
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import hermes_diff_scan as _diff_scan_module  # noqa: E402 — after sys.path bootstrap
from tools.hermes_diff_scan import scan_diff_stream  # noqa: E402
from tools._git_utils import _HERMES_PATTERN  # noqa: E402

# Maximum number of dirty-commit SHAs to display inline before truncating.
_SHA_DISPLAY_CAP = 5

# `git show` flags (T-4211). Each one closes a way for committed bytes to be
# absent from, or transformed in, the patch the scanner reads:
#   --text      without it git prints only "Binary files ... differ" for a file
#               with a NUL in its first 8000 bytes or a .gitattributes
#               binary / -diff file.
#   --diff-merges=remerge  (two-parent merges; git >= 2.36)
#               a merge is shown as the diff from git's own automatic re-merge
#               of its parents to the committed result, as an ordinary
#               hunk-tracked patch: EXACTLY the bytes the merge resolution
#               introduced. A clean merge is an empty patch; an evil-merge line
#               or a conflict resolution's new line is a `+` line; conflict
#               markers of the re-merge appear only as REMOVED lines (ignored).
#               The default for a merge is the dense combined diff (--cc, `@@@`
#               hunks), which the scanner does not hunk-track, so a resolution
#               that removes a `- x` line and adds a `+ <trigger>` line rendered
#               as a `--- x` / `+++ <trigger>` header lookalike pair and was
#               skipped. `separate` also closes that, but re-shows a merged
#               branch's content against the other parent, so a CLEAN merge of
#               a dirty branch was counted as well and could crowd the real
#               introducer out of the SHA list. remerge stays complete because
#               every parent's own commits are in the audited log.
#   --diff-merges=separate (octopus merges only)
#               git skips remerge for 3+ parents (it prints a warning and NO
#               patch), so those fall back to per-parent diffs (fail-closed:
#               may over-report, never under-reports). Spelled out rather than
#               `-m`, which follows the user's log.diffMerges config.
#   Non-merge commits: byte-identical output under either mode.
#   --no-textconv / --no-ext-diff
#               raw blob bytes, never a configured textconv filter's or external
#               diff tool's rendering of them.
_BASE_SHOW_ARGS = ("-p", "--text", "--no-textconv", "--no-ext-diff")
_SHOW_ARGS = (*_BASE_SHOW_ARGS, "--diff-merges=remerge")
_OCTOPUS_SHOW_ARGS = (*_BASE_SHOW_ARGS, "--diff-merges=separate")

# Longest stderr excerpt quoted in an ERROR line.
_ERR_DETAIL_CAP = 200

# Wall-clock threshold in seconds; exceeded → append a warning to the line.
_PERF_WARNING_THRESHOLD_S = 30.0

# T-4308: the clean-verdict cache. Line 1 is `key <hex>`; each further line is
# the full SHA of a commit that was shown successfully and scanned clean.
_CACHE_FILENAME = "hermes_history_audit_clean.txt"
# Checkpoint every N newly scanned clean commits. A cold scan can outlast the
# grooming unit's 10-min TimeoutStartSec (946s measured under load), and a kill
# before the final save would leave every later run cold, and killed, too.
_CACHE_CHECKPOINT_EVERY = 1000


def default_cache_path() -> Path:
    """Where grooming keeps the cache: $PANTHEON_STATE_DIR, else
    ~/.local/state/pantheon (the fleet state-dir convention)."""
    root = os.environ.get("PANTHEON_STATE_DIR") or str(Path.home() / ".local" / "state" / "pantheon")
    return Path(root) / _CACHE_FILENAME


def _cache_key(repo_root: Path) -> str | None:
    """Hash of everything a verdict depends on besides the commit itself: the
    show flags, this module's and the diff parser's source, the pattern, and the
    git version (git renders the patch). Any change re-scans everything. None if
    any input can't be read, which disables the cache for the run."""
    try:
        git_version = subprocess.run(["git", "--version"], cwd=repo_root, capture_output=True,
                                     check=True).stdout
        pattern = _HERMES_PATTERN.pattern
        parts = [repr((_SHOW_ARGS, _OCTOPUS_SHOW_ARGS)).encode(),
                 pattern if isinstance(pattern, bytes) else pattern.encode(),
                 str(_HERMES_PATTERN.flags).encode(),  # same text + IGNORECASE matches more
                 Path(__file__).read_bytes(), Path(_diff_scan_module.__file__).read_bytes(),
                 git_version]
    except (OSError, subprocess.SubprocessError, TypeError):
        return None
    h = hashlib.sha256()
    for p in parts:
        h.update(len(p).to_bytes(8, "big") + p)
    return h.hexdigest()


def _load_clean_cache(path: Path, key: str) -> set[str]:
    """SHAs cached clean under `key`. Missing, unreadable or other-key: empty set
    (a full scan)."""
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        return set()
    if not lines or lines[0] != f"key {key}":
        return set()
    return {ln for ln in lines[1:] if len(ln) == 40}


def _save_clean_cache(path: Path, key: str, shas: set[str]) -> None:
    """Best-effort atomic write. A failure only costs the next run a full scan.
    mkstemp: a fresh random name opened O_EXCL at 0600, so a pre-planted symlink
    or a stale looser-mode tmp file can't be written through (anyone who can write
    the cache can suppress detection)."""
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".tmp.")
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(f"key {key}\n" + "".join(f"{s}\n" for s in sorted(shas)))
        os.replace(tmp, path)
    except OSError:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """Thin subprocess wrapper; binary output (no text=True). Caller inspects
    `.returncode` and `.stdout`/`.stderr` for domain-specific handling.
    """
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        check=False,
        # Binary output: the scanner operates on bytes directly, avoiding
        # a text-decode + re-encode round-trip and matching the convention used
        # in _hermes_scan_file.
    )


def _scan_show_output(raw: bytes) -> bool:
    """Return True iff the `git show -p` output for one commit contains the
    unescaped trigger on at least one added line.

    Parsing is delegated to `tools.hermes_diff_scan.scan_diff_stream` (T-4185):
    file headers are recognised structurally from hunk counts, and the output
    is split on the newline byte only. The previous "skip any line starting
    with `++`" guard silently dropped an added line whose CONTENT began with
    `+`, and `bytes.splitlines()` let a trigger hide after a lone carriage
    return. One shared parser means one place to get that right.

    Context lines (` ` prefix) and removed lines (`-` prefix) are skipped.
    This means the report surfaces the *introducing* commit, not every commit
    that ever had the bytes in its diff context.
    """
    for _violation in scan_diff_stream(io.BytesIO(raw)):
        return True
    return False


def run_history_audit(repo_root: Path, cache_path: Path | None = None) -> str:
    """Scan all commits in `repo_root` for the unescaped HERMES trigger on
    added diff lines. Returns a single plain-text line.

    `cache_path` (T-4308): skip `git show` for commits already cached CLEAN under
    the current `_cache_key`, and record newly clean ones. None (the default)
    scans every commit with no cache I/O. Dirty or unshowable commits are never
    cached, so they are re-shown and re-reported every run. A cached commit
    counts as scanned; the line says how many came from the cache.

    Format (clean):
        HERMES audit: 0 unescaped occurrence(s) across 74 commits scanned.

    Format (dirty, up to 5 SHAs):
        HERMES audit: 2 unescaped occurrence(s) in 2 commit(s): abc1234, def5678. (74 commits scanned)

    Format (dirty, more than 5 SHAs):
        HERMES audit: 7 unescaped occurrence(s) in 7 commit(s): a, b, c, d, e and 2 more. (74 commits scanned)

    Format (git log failure):
        HERMES audit: ERROR: git log failed: <detail>

    Format (any `git show` failed; fail-closed, never reported as clean):
        HERMES audit: ERROR: git show failed for 1 of 74 commit(s) (first: abc1234: <detail>); 0 unescaped occurrence(s) in the 73 commit(s) scanned.
    (With occurrences, the tail reads `...; 2 unescaped occurrence(s) in 2
    commit(s): a, b. (73 commit(s) scanned)`.) An old git that lacks a flag
    in `_SHOW_ARGS` fails every show and lands here, not on a clean line.

    A timing warning is appended if the scan takes longer than 30s:
        ... (WARNING: scan took 42s; commit-count growth may impact grooming)
    """
    t_start = time.monotonic()

    # Step 1: enumerate all commit SHAs.
    # `%H %P`: the SHA plus its parents, so octopus merges can be told apart.
    res = _git("log", "--format=%H %P", cwd=repo_root)
    if res.returncode != 0:
        detail = res.stderr.decode("utf-8", errors="replace").strip() or res.stdout.decode("utf-8", errors="replace").strip()
        return f"HERMES audit: ERROR: git log failed: {detail}"

    raw_log = res.stdout.decode("utf-8", errors="replace")
    commits = [ln.split() for ln in raw_log.splitlines() if ln.strip()]
    total = len(commits)

    # Step 2: scan each commit's diff for added-line violations.
    dirty_shas: list[str] = []
    scanned = 0
    failed: list[str] = []
    first_fail_detail = ""

    key = _cache_key(repo_root) if cache_path is not None else None
    cached_clean = _load_clean_cache(cache_path, key) if key else set()
    now_clean: set[str] = set()
    from_cache = 0

    for sha, *parents in commits:
        if sha in cached_clean:
            scanned += 1
            from_cache += 1
            now_clean.add(sha)
            continue
        args = _OCTOPUS_SHOW_ARGS if len(parents) > 2 else _SHOW_ARGS
        show_res = _git("show", sha, *args, cwd=repo_root)
        if show_res.returncode != 0:
            # Keep scanning the rest, but the result can no longer be clean.
            if not failed:
                first_fail_detail = (
                    show_res.stderr.decode("utf-8", errors="replace").strip().splitlines() or [""]
                )[0][:_ERR_DETAIL_CAP]
            failed.append(sha[:7])
            continue
        scanned += 1
        if _scan_show_output(show_res.stdout):
            dirty_shas.append(sha[:7])  # short SHA (7 chars) for readability
        else:
            now_clean.add(sha)
            if key and (scanned - from_cache) % _CACHE_CHECKPOINT_EVERY == 0:
                # Keep not-yet-visited cached entries: only the final save prunes.
                _save_clean_cache(cache_path, key, cached_clean | now_clean)

    # Rewrite only when something new is clean; entries for commits no longer in
    # the log (a rewritten branch) drop out.
    if key and now_clean != cached_clean:
        _save_clean_cache(cache_path, key, now_clean)

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

    # Fail-closed: a commit that could not be shown was not audited.
    if failed or scanned < total:
        body = line[len("HERMES audit: "):]
        if n_occurrences == 0:
            body = f"0 unescaped occurrence(s) in the {scanned} commit(s) scanned."
        line = (
            f"HERMES audit: ERROR: git show failed for {total - scanned} of {total} "
            f"commit(s) (first: {failed[0] if failed else '?'}: {first_fail_detail}); {body}"
        )

    if from_cache:
        line += f" ({from_cache} clean verdict(s) from the T-4308 cache.)"

    # Step 4: append performance warning if needed.
    if t_elapsed > _PERF_WARNING_THRESHOLD_S:
        elapsed_s = int(t_elapsed)
        line += f" (WARNING: scan took {elapsed_s}s; commit-count growth may impact grooming)"

    return line


if __name__ == "__main__":
    import sys as _sys
    root = Path(_sys.argv[1]) if len(_sys.argv) > 1 else Path(__file__).resolve().parent.parent
    print(run_history_audit(root))
