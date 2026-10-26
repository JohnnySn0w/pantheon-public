#!/usr/bin/env python3
"""Direct-code-push audit: alert when code lands on main with no merged PR (T-4226).

The server-side backstop for the seat pre-push guard. `.githooks/pre-push` stops a seat
pushing CODE (tools/, .github/, .githooks/) straight to main, but `--no-verify` and a
hookless clone bypass a client-side hook. This runs on origin after every push to main that
touches a code path (`.github/workflows/direct-code-push-audit.yml`) and flags any code
commit that GitHub does not associate with a MERGED pull request. It can only ALERT: the
push has already landed. The alert is a red workflow run plus one `::error` annotation per
offending commit.

HOW. `git rev-list --no-merges` over the push range lists its commits, and each is read with
its own `git diff-tree -z --name-only` (pure NUL-separated names, nothing to forge) to find
those touching a code path (the same definition as the hook, minus the one exempt spine
README). A commit whose paths cannot be read fails the audit (exit 2), never reads as clean.
For each code commit,
`GET /repos/{repo}/commits/{sha}/pulls` returns the merged PR that introduced the commit to
the default branch. Shapes verified against the real API on 2026-09-29 (D11):
  - squash-merged PR commit          -> [that PR]
  - `--merge` PR: merge commit AND every branch commit -> [that PR]  (stacked series merge
    this way, so branch commits are audited individually, not via their merge commit)
  - a commit pushed directly to main -> []
Association can lag the push event by a moment, so a commit with no merged PR is re-checked
(`--attempts`, `--delay`); the wait is shared across all pending commits, not per commit.

EXIT CODES (three, deliberately distinct):
  0  clean: no code commits in range, or every one has a merged PR
  1  unreviewed code found (annotated)
  2  the audit could not answer (API failure, bad token/permission, unreadable range).
     Loud on purpose: a 403 must not leave the backstop silently dead.

KNOWN LIMITS (alert-only, informational; not a required check):
  - A pure "evil merge" (a merge commit whose own diff adds code, with no non-merge code
    commit behind it) is not listed: merges carry no diff of their own to read.
  - HO's own direct code push to main is flagged too, by design: HO merges through the
    GitHub API, so its git pushes to main are ticket/ledger/stamp only.

Source-cleanliness: the byte-sequence trigger is never written contiguously in this file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# One definition of "code path" shared with the client-side hook: tools/test_direct_code_push_audit.py
# asserts these two strings appear verbatim in .githooks/pre-push, so they cannot drift apart.
CODE_RE = r"^(tools/|\.github/|\.githooks/|harness/)"
# The ONE exemption: the tools spine README, whose generated sentinel region Recovery's
# spine-index-groom timer pushes to main. Exact path; see the hook for WHY and what retires it.
EXEMPT_RE = r"^tools/README\.md$"

EXIT_CLEAN = 0
EXIT_UNREVIEWED = 1
EXIT_API_FAILURE = 2

API_VERSION = "2022-11-28"
DEFAULT_API_URL = "https://api.github.com"
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_ZERO_RE = re.compile(r"^0+$")
_REPO_SEGMENT_RE = re.compile(r"^(?!\.+$)[A-Za-z0-9_.-]+$")
_GIT_TIMEOUT = 60

Fetch = Callable[[str], list]


class ApiError(RuntimeError):
    """The GitHub API could not answer (transport, auth, 5xx, malformed body)."""


@dataclass(frozen=True)
class Commit:
    sha: str
    author: str
    subject: str
    paths: tuple = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Which commits to audit
# ---------------------------------------------------------------------------

def is_code_path(path: str) -> bool:
    """True for a path under a code root, except the one exempt spine README.

    The exemption is anchored with `\\Z` on top of EXEMPT_RE's `$`: Python's `$` also
    matches BEFORE a trailing newline, so `tools/README.md<LF>` (one file whose name ends
    in a newline) would otherwise take the exemption. Bash's `$` (the hook) does not.
    """
    return bool(re.match(CODE_RE, path)) and not re.match(EXEMPT_RE + r"\Z", path)




def parse_paths(text: str) -> tuple:
    """Split `git diff-tree -z --name-only` output into paths.

    That output is NUL-separated names and NOTHING else -- no header, no commit id -- so
    there is no delimiter a filename could forge or truncate. A name holding a newline, a
    quote, an invalid-UTF-8 byte or a \\x01/\\x02 stays one string. (An earlier version
    parsed one `git log` stream, split on a header byte, and let `docs/a\\x01zz\\x02q`
    truncate a commit's paths and get the remainder silently dropped.)
    """
    return tuple(p for p in text.split("\0") if p)


def _commit_paths(sha: str, *, cwd: Path) -> tuple:
    """Every path one commit changed, read on its own. Raises ApiError if git cannot say:
    a commit whose paths are unreadable must never look like 'no code paths'."""
    res = _git(["diff-tree", "--root", "-r", "-z", "--no-commit-id", "--name-only",
                "--no-renames", sha], cwd=cwd)
    if res.returncode != 0:
        raise ApiError(f"git diff-tree failed for {sha[:7]}: "
                       f"{res.stderr.strip() or res.stdout.strip()}")
    return parse_paths(res.stdout)


def _with_metadata(commits: list, *, cwd: Path) -> list:
    """Attach author + subject (cosmetic: they only decorate the annotation).

    Best effort: any failure -- including a timeout -- leaves them blank rather than
    failing the audit (exit 2 over a cosmetic step would be wrong), since the decision
    (code path, merged PR) never depends on them.
    """
    if not commits:
        return commits
    try:
        res = _git(["log", "--no-walk=unsorted", "-z", "--format=%H%x1f%an%x1f%s",
                    *[c.sha for c in commits]], cwd=cwd)
    except subprocess.TimeoutExpired:
        return [Commit(c.sha, "", "", c.paths) for c in commits]
    meta = {}
    if res.returncode == 0:
        for rec in res.stdout.split("\0"):
            sha, _, tail = rec.strip("\n").partition("\x1f")
            author, _, subject = tail.partition("\x1f")
            meta[sha] = (author, subject)
    return [Commit(c.sha, *meta.get(c.sha, ("", "")), c.paths) for c in commits]


def _git(args: list, *, cwd: Path) -> subprocess.CompletedProcess:
    # errors="surrogateescape": a path/author/subject holding an invalid-UTF-8 byte must
    # not make the output undecodable (UnicodeDecodeError -> traceback, exit 1 by accident
    # instead of the documented exit 2). The bytes survive as lone surrogates and the path
    # still matches its code root; `_printable` renders them for the annotation.
    return subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=cwd, capture_output=True, text=True, errors="surrogateescape",
        timeout=_GIT_TIMEOUT, check=False,
    )


def resolve_range_args(before: str, sha: str, *, cwd: Path) -> list:
    """`git log` revision args for the push: `before..sha`, or just the head commit.

    A new branch (all-zero `before`), an empty `before`, or a `before` this clone does not
    have (force-push) cannot be ranged, so only the head commit is audited.
    """
    if before and not _ZERO_RE.match(before):
        if _git(["cat-file", "-e", f"{before}^{{commit}}"], cwd=cwd).returncode == 0:
            return [f"{before}..{sha}"]
    return ["-1", sha]


def list_code_commits(range_args: list, *, cwd: Path) -> list:
    """Non-merge commits in the range that touch a code path, each with ONLY its code paths.

    `--no-renames` so a file renamed OUT of tools/ still names the old path (same as the hook).
    """
    res = _git(["rev-list", "--no-merges", *range_args], cwd=cwd)
    if res.returncode != 0:
        raise ApiError(f"git rev-list failed: {res.stderr.strip() or res.stdout.strip()}")
    out = []
    for sha in res.stdout.split():
        if not _SHA_RE.match(sha):
            # Never `continue` past a token we cannot interpret (a 64-hex SHA-256 sha,
            # garbage): skipping it would read as "no code paths" for a commit the audit
            # never looked at. Fail loud (exit 2) instead.
            raise ApiError(f"unrecognised commit id in `git rev-list` output: {sha!r}")
        code = tuple(p for p in _commit_paths(sha, cwd=cwd) if is_code_path(p))
        if code:
            out.append(Commit(sha, "", "", code))
    return _with_metadata(out, cwd=cwd)


# ---------------------------------------------------------------------------
# Which of them were reviewed
# ---------------------------------------------------------------------------

def has_merged_pr(prs: list) -> bool:
    """True if any associated PR was actually merged (an open PR is not review)."""
    return any(isinstance(pr, dict) and pr.get("merged_at") for pr in prs)


def find_unreviewed(commits: list, *, fetch: Fetch, attempts: int = 3,
                    delay: float = 5.0, sleep: Callable[[float], None] | None = None) -> list:
    """Commits with no merged PR after `attempts` rounds.

    Each round re-checks only the still-pending commits, and the wait between rounds is
    shared, so total waiting is O(attempts), not O(commits). ApiError propagates.
    """
    sleep = sleep or time.sleep
    pending = list(commits)
    for attempt in range(max(1, attempts)):
        if attempt:
            sleep(delay)
        pending = [c for c in pending if not has_merged_pr(fetch(c.sha))]
        if not pending:
            break
    return pending


def _printable(text: str) -> str:
    """Render lone surrogates (bytes that were not valid UTF-8) as readable `\\xNN`.

    Printing a lone surrogate to a strict-UTF-8 stdout raises UnicodeEncodeError.
    """
    try:
        raw = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:  # a surrogate outside the surrogateescape range
        raw = text.encode("utf-8", "backslashreplace")
    return raw.decode("utf-8", "backslashreplace")


def _escape(text: str) -> str:
    """Workflow-command data escaping: commit text is attacker-influenced."""
    return _printable(text).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def annotation(commit: Commit) -> str:
    paths = ", ".join(commit.paths)
    msg = (f"{commit.sha[:7]} \"{commit.subject}\" by {commit.author} changed code path(s) "
           f"[{paths}] on main with no merged PR. Code reaches main only through a reviewed, "
           f"CI-gated PR (T-4226).")
    return f"::error title=Unreviewed code on main::{_escape(msg)}"


# ---------------------------------------------------------------------------
# GitHub lookup (stdlib urllib; `gh` is not assumed on the runner)
# ---------------------------------------------------------------------------

def _retryable(code: int) -> bool:
    return code == 429 or 500 <= code < 600


def github_fetch(repo: str, token: str, *, api_url: str = DEFAULT_API_URL,
                 timeout: float = 20.0, attempts: int = 3, backoff: float = 2.0,
                 urlopen: Callable = urllib.request.urlopen,
                 sleep: Callable[[float], None] = time.sleep) -> Fetch:
    """Return fetch(sha) -> list of PR dicts for `commits/{sha}/pulls`.

    Transient failures (network, 429, 5xx) are retried; anything else raises ApiError. The
    token is never included in an error message.
    """
    segments = (repo or "").split("/")
    if len(segments) != 2 or not all(_REPO_SEGMENT_RE.match(s) for s in segments):
        raise ApiError(f"unusable repository name {repo!r}")

    def fetch(sha: str) -> list:
        if not _SHA_RE.match(sha or ""):
            raise ApiError(f"unusable commit sha {sha!r}")
        request = urllib.request.Request(
            f"{api_url.rstrip('/')}/repos/{repo}/commits/{sha}/pulls",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "pantheon-direct-code-push-audit",
            },
        )
        body = b""
        last = "no attempt made"
        for attempt in range(max(1, attempts)):
            if attempt:
                sleep(backoff * 2 ** (attempt - 1))
            try:
                with urlopen(request, timeout=timeout) as resp:
                    body = resp.read()
            except urllib.error.HTTPError as exc:  # before URLError: it is a subclass
                if _retryable(exc.code):
                    last = f"HTTP {exc.code}"
                    continue
                raise ApiError(f"HTTP {exc.code} for commits/{sha[:7]}/pulls") from exc
            except OSError as exc:  # URLError, socket.timeout, ConnectionError
                last = f"{type(exc).__name__}: {exc}"
                continue
            break
        else:
            raise ApiError(f"commits/{sha[:7]}/pulls failed after {max(1, attempts)} attempts: {last}")
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ApiError(f"malformed JSON from commits/{sha[:7]}/pulls") from exc
        if not isinstance(data, list):
            raise ApiError(f"unexpected response shape from commits/{sha[:7]}/pulls (not a list)")
        return data

    return fetch


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None, *, fetch: Fetch | None = None, cwd: Path | None = None,
         sleep: Callable[[float], None] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--before", default="", help="push base sha (github.event.before)")
    ap.add_argument("--sha", required=True, help="push head sha (github.sha)")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
                    help="owner/name (default: $GITHUB_REPOSITORY)")
    ap.add_argument("--attempts", type=int, default=3)
    ap.add_argument("--delay", type=float, default=5.0)
    args = ap.parse_args(argv)
    cwd = cwd or Path.cwd()

    try:
        if fetch is None:
            token = os.environ.get("GITHUB_TOKEN", "")
            if not token:
                raise ApiError("GITHUB_TOKEN is not set")
            fetch = github_fetch(
                args.repo, token, api_url=os.environ.get("GITHUB_API_URL", DEFAULT_API_URL))
        commits = list_code_commits(resolve_range_args(args.before, args.sha, cwd=cwd), cwd=cwd)
        if not commits:
            print("direct-code-push-audit: no code-path commits in this push; nothing to audit.")
            return EXIT_CLEAN
        unreviewed = find_unreviewed(
            commits, fetch=fetch, attempts=args.attempts, delay=args.delay, sleep=sleep)
    except (ApiError, OSError, subprocess.SubprocessError, ValueError) as exc:
        # ValueError covers UnicodeDecodeError/EncodeError: belt and braces behind the
        # surrogateescape decode, so an odd byte anywhere is "could not verify" (2),
        # never an uncaught traceback (which would exit 1 = "unreviewed code found").
        print(f"::error title=Direct-code-push audit could not verify::{_escape(str(exc))}")
        print(f"direct-code-push-audit: could not verify ({exc}); NOT a clean result.",
              file=sys.stderr)
        return EXIT_API_FAILURE

    if not unreviewed:
        print(f"direct-code-push-audit: {len(commits)} code commit(s), all with a merged PR.")
        return EXIT_CLEAN
    for c in unreviewed:
        print(annotation(c))
    print(f"direct-code-push-audit: {len(unreviewed)} of {len(commits)} code commit(s) on main "
          f"have NO merged PR (pushed directly).", file=sys.stderr)
    return EXIT_UNREVIEWED


if __name__ == "__main__":
    raise SystemExit(main())
