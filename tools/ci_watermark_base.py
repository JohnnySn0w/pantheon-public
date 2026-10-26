"""tools/ci_watermark_base.py -- high-watermark diff base for range-scanning CI workflows (T-4182).

Used by `.github/workflows/hermes-audit.yml` (push path); it was also used by
cascade-on-merge.yml until T-4186 retired that workflow. Prints ONE line on stdout: the git
revision range the workflow should scan (`<base>..HEAD`, or `HEAD` = the whole history). The reason
goes to stderr.

WHY. The audit scans a DIFF RANGE, not a state. Its base used to be `github.event.before`,
i.e. per-push, so a superseded (cancelled) run's commits were never scanned by anything else.
That is why it was deliberately given no concurrency group (ci.yml, T-2344) -- and why, with
one runner and ~2 main pushes/min, its queue grew without bound (T-4182).

THE FIX is the consumer-offset pattern: commit the offset only on success, resume from the last
committed offset. base = the head sha of the last SUCCESSFUL run of the same workflow on the same
branch (GitHub REST API, GITHUB_TOKEN with `actions: read`, stdlib urllib -- `gh` is not assumed on
the runner). A cancelled, failed or superseded run never advances the offset, so the run that
replaces it scans everything it would have.

INVARIANT the whole scheme leans on: every commit reachable from the head of any successful run has
been scanned (by that run or an earlier successful one). So any successful head that is an ancestor
of HEAD is a valid base, and an OLDER valid base is only ever broader.

DECISION LADDER (every fallback is BROAD -- scanning more is safe, missing a commit is the failure):
  existing branch (real `before`)
    1. `before` malformed / not in this clone (force-push)              -> default-branch fallback (N3)
    2. lookup of THIS branch's successful runs fails                    -> full history
    3. no successful head is an ancestor of HEAD (force-push, no runs)  -> default-branch fallback (N3)
    4. else base = merge-base(first ancestor head, before) -- whichever is older by ancestry,
       generalised to diverged histories.
    N3 (T-4199/T-4186) default-branch fallback, for 1 and 3: base = merge-base(W, HEAD) where W = the
       newest default-branch success present in this clone (the new-branch rule below). Coverage-
       safe by the invariant: everything reachable from W was already scanned and the merge-base is
       reachable from W. It matters for a fresh branch whose first push run was superseded, which
       otherwise paid a ~70 s / 1.3 GB full-history scan on the second push. If that lookup fails or
       finds no usable head, the original full-history decision stands (never `before`).
  new branch (all-zero `before`, O2(b) + D16)
    1. look up the last successful runs on the DEFAULT branch (`main`), same workflow
    2. lookup fails, or none of the listed heads exists in this clone,
       or there is no merge-base                                        -> full history
    3. else W = the NEWEST listed head present locally -- an ancestor of HEAD or not -- and
       base = merge-base(W, HEAD). Coverage-safe by the same invariant: everything reachable
       from W was already scanned, and the merge-base is reachable from W. A branch forked from
       an old main commit has W far ahead of it and never an ancestor, so requiring one would
       send most branches to the full-history scan. The range is exactly the branch's commits.
       `actions/checkout` with fetch-depth 0 fetches every branch (`+refs/heads/*`), so main's
       history is present on the runner; a head newer than that fetch is skipped as "not present".

⚠️ The fallback is BROAD (the main fork point where N3 applies, else FULL HISTORY), never
`event.before`. Under supersession `before` is NARROW: the
range a cancelled run owned sits behind it. Falling back to it on an API blip would let the run
succeed and advance the offset past commits nobody scanned.

FAIL LOUD, NOT NARROW. Any unexpected error still prints `HEAD`. If git itself is broken, the
workflow's `git log <range>` fails and the step goes red under `bash -eo pipefail`. The only
non-zero exit here is an argparse error (exit 2), which also fails the step.

STICKY FAILURES -- RECOVERY (T-4199 N1). The offset advances only on success, so a failure CI can
never fix is STICKY: every later run re-walks the same range and stays red until the cause is gone.
  - A real trigger in the audited range (hermes-audit): red until history is remediated per the
    standing rule in CLAUDE.md. A history rewrite makes the new head a non-descendant of the old
    watermark, so the next run takes the N3 fork-point / full-history path over the rewritten
    history, scans clean, and goes green (its success becomes the new watermark).
  - The former cascade-on-merge hazard (a `ticket.py cascade` push that 403s on protect-main,
    precedent run 34911462049 / T-3717, red on every later run) no longer exists: that workflow was
    retired in T-4186 and the cascade runs seat-side (tools/cascade_sweep.py), where it can push.

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
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence, TextIO

ZERO_SHA = "0" * 40
FULL_HISTORY = "HEAD"
DEFAULT_API_URL = "https://api.github.com"
DEFAULT_BRANCH = "main"
API_VERSION = "2022-11-28"

_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GIT_TIMEOUT = 120


class WatermarkLookupError(Exception):
    """The GitHub run lookup failed or returned something unusable."""


@dataclass(frozen=True)
class BaseDecision:
    base: Optional[str]  # commit sha, or None => full history (root-inclusive)
    reason: str

    @property
    def range_spec(self) -> str:
        return f"{self.base}..HEAD" if self.base else FULL_HISTORY


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.match(value))


def _full(why: str) -> BaseDecision:
    return BaseDecision(None, f"full-history:{why}")


# ---------------------------------------------------------------------------
# GitHub lookup
# ---------------------------------------------------------------------------

def _retryable(code: int) -> bool:
    return code == 429 or 500 <= code < 600


def fetch_successful_heads(
    *,
    repo: str,
    workflow: str,
    branch: str,
    event: str,
    token: str,
    api_url: str = DEFAULT_API_URL,
    timeout: float = 20.0,
    attempts: int = 3,
    backoff: float = 2.0,
    urlopen: Callable = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Head shas of the successful runs of `workflow` on `branch`/`event`, newest first, deduped.

    `status=success` is sent AND re-checked client-side (`conclusion == "success"`): a cancelled,
    failed, timed-out or still-running run must never become a watermark. Raises
    WatermarkLookupError on any failure; transient ones (network, 429, 5xx) are retried.
    """
    if not _REPO_RE.match(repo or ""):
        raise WatermarkLookupError(f"unusable repository name {repo!r}")
    query = urllib.parse.urlencode(
        {"branch": branch, "event": event, "status": "success", "per_page": 100}
    )
    url = (
        f"{api_url.rstrip('/')}/repos/{repo}/actions/workflows/"
        f"{urllib.parse.quote(workflow, safe='')}/runs?{query}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "pantheon-ci-watermark-base",
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
            raise WatermarkLookupError(f"HTTP {exc.code}") from exc
        except OSError as exc:  # URLError, socket.timeout, TimeoutError, ConnectionError
            last = f"{type(exc).__name__}: {exc}"
            continue
        break
    else:
        raise WatermarkLookupError(f"lookup failed after {max(1, attempts)} attempts: {last}")

    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise WatermarkLookupError(f"malformed JSON: {type(exc).__name__}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("workflow_runs"), list):
        raise WatermarkLookupError("unexpected response shape (no workflow_runs list)")

    heads: list[str] = []
    for run in data["workflow_runs"]:
        if not isinstance(run, dict) or run.get("conclusion") != "success":
            continue
        if run.get("event", event) != event or run.get("head_branch", branch) != branch:
            continue
        sha = run.get("head_sha")
        if _is_sha(sha) and sha not in heads:
            heads.append(sha)
    return heads


# ---------------------------------------------------------------------------
# git helpers -- every failure reads as "no", which callers turn into a BROADER base
# ---------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 128, "", str(exc))


def is_ancestor(cwd: Path, ancestor: str, descendant: str) -> bool:
    return _git(cwd, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def commit_exists(cwd: Path, sha: str) -> bool:
    return _is_sha(sha) and _git(cwd, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def merge_base(cwd: Path, a: str, b: str) -> Optional[str]:
    proc = _git(cwd, "merge-base", a, b)
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and _is_sha(out) else None


def _first_ancestor(cwd: Path, heads: Sequence[str], head: str) -> Optional[str]:
    """First (newest) head that is a valid sha and an ancestor of `head`."""
    for candidate in heads:
        if _is_sha(candidate) and is_ancestor(cwd, candidate, head):
            return candidate
    return None


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------

def choose_base(
    cwd: Path, *, heads: Sequence[str], before: str, head: str = "HEAD"
) -> BaseDecision:
    """Base for a push to a branch that already existed (real `before`)."""
    if not _is_sha(before) or before == ZERO_SHA or not commit_exists(cwd, before):
        return _full("before-unusable")
    watermark = _first_ancestor(cwd, heads, head)
    if watermark is None:
        return _full("no-ancestor-watermark" if heads else "no-successful-run")
    base = merge_base(cwd, watermark, before)  # the older of the two; fork point if diverged
    if base is None:
        return _full("no-merge-base")
    if base == watermark:
        reason = "watermark"
    elif base == before:
        reason = "before-older-than-watermark"
    else:
        reason = "merge-base"
    return BaseDecision(base, reason)


def choose_base_new_branch(
    cwd: Path, *, default_branch_heads: Sequence[str], head: str = "HEAD"
) -> BaseDecision:
    """Base for the first push of a branch (all-zero `before`), from the default branch's watermark.

    W = the NEWEST listed default-branch success that exists in this clone, ancestor of `head` or
    not (D16). A branch forked from an old main commit has W far ahead of it and never an ancestor;
    demanding one would push most branches onto the full-history scan. base = merge-base(W, head),
    which is reachable from W and so already audited, and the range is exactly the branch's own
    commits. Only the newest locally-present head is consulted: an older one has no more to offer.
    """
    for candidate in default_branch_heads:
        if not commit_exists(cwd, candidate):
            continue  # main moved between the checkout's fetch and the lookup: try the next
        base = merge_base(cwd, candidate, head)
        if base is None:
            return _full("no-merge-base")
        return BaseDecision(base, "default-branch-watermark")
    return _full("no-default-branch-head-present" if default_branch_heads else "no-default-branch-success")


# `choose_base` reasons that mean "this branch has no watermark of its own to use" (N3 applies).
_NO_OWN_WATERMARK = ("full-history:no-ancestor-watermark", "full-history:no-successful-run")


def _default_branch_fallback(
    cwd: Path,
    *,
    fetch_heads: Callable[[str], Sequence[str]],
    default_branch: str,
    head: str,
    otherwise: BaseDecision,
) -> BaseDecision:
    """N3: base for an EXISTING branch from the default branch's watermark, else `otherwise`.

    `otherwise` is the FULL-HISTORY decision that would have been returned, so any failure here
    (lookup error, no head present in this clone, no merge-base) leaves the safe, broad answer
    standing rather than inventing a narrower one.
    """
    try:
        found = choose_base_new_branch(cwd, default_branch_heads=fetch_heads(default_branch), head=head)
    except WatermarkLookupError:
        return otherwise
    if found.base is None:
        return otherwise
    why = otherwise.reason.split(":", 1)[-1]
    return BaseDecision(found.base, f"default-branch-watermark:{why}")


def compute_base(
    cwd: Path,
    *,
    fetch_heads: Callable[[str], Sequence[str]],
    branch: str,
    before: str,
    default_branch: str = DEFAULT_BRANCH,
    head: str = "HEAD",
) -> BaseDecision:
    """Full decision. `fetch_heads(branch)` returns successful-run head shas, newest first.

    Never raises: a lookup failure or any unexpected error yields FULL HISTORY (broad).
    """
    try:
        if before == ZERO_SHA:
            heads = fetch_heads(default_branch)
            return choose_base_new_branch(cwd, default_branch_heads=heads, head=head)
        if not _is_sha(before) or not commit_exists(cwd, before):
            # Force-push shape: no ancestor question to put to this branch's own runs (no API call
            # for them), but the default branch's watermark still bounds the scan (N3).
            return _default_branch_fallback(
                cwd, fetch_heads=fetch_heads, default_branch=default_branch, head=head,
                otherwise=_full("before-unusable"),
            )
        decision = choose_base(cwd, heads=fetch_heads(branch), before=before, head=head)
        if decision.base is None and decision.reason in _NO_OWN_WATERMARK:
            return _default_branch_fallback(
                cwd, fetch_heads=fetch_heads, default_branch=default_branch, head=head,
                otherwise=decision,
            )
        return decision
    except WatermarkLookupError as exc:
        return _full(f"lookup-failed: {exc}")
    except Exception as exc:  # noqa: BLE001 -- anything unexpected must land BROAD
        return _full(f"unexpected: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _redact(text: str, secret: str) -> str:
    return text.replace(secret, "***") if secret else text


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Print the git range a range-scanning CI workflow should scan (T-4182)."
    )
    p.add_argument("--workflow", required=True, help="workflow FILE name, e.g. hermes-audit.yml")
    p.add_argument("--event", required=True, help="triggering event to match, e.g. push")
    p.add_argument("--branch", required=True, help="branch the push landed on")
    p.add_argument("--before", required=True, help="github.event.before (all-zero on a new branch)")
    p.add_argument("--default-branch", default=DEFAULT_BRANCH)
    p.add_argument("--repo-dir", default=".", help="checkout to run git in")
    p.add_argument("--repo", default=None, help="owner/name (default: $GITHUB_REPOSITORY)")
    p.add_argument("--api-url", default=None, help="default: $GITHUB_API_URL or api.github.com")
    return p


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    environ: Optional[dict] = None,
    urlopen: Callable = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    env = os.environ if environ is None else environ
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    args = _build_parser().parse_args(argv)  # bad flags -> SystemExit(2): loud, not narrow

    token = env.get("GITHUB_TOKEN") or ""
    repo = args.repo or env.get("GITHUB_REPOSITORY") or ""
    api_url = args.api_url or env.get("GITHUB_API_URL") or DEFAULT_API_URL

    def fetch(branch: str) -> list[str]:
        if not token:
            raise WatermarkLookupError("GITHUB_TOKEN not set")
        return fetch_successful_heads(
            repo=repo, workflow=args.workflow, branch=branch, event=args.event,
            token=token, api_url=api_url, urlopen=urlopen, sleep=sleep,
        )

    try:
        decision = compute_base(
            Path(args.repo_dir), fetch_heads=fetch, branch=args.branch,
            before=args.before, default_branch=args.default_branch,
        )
    except Exception as exc:  # noqa: BLE001 -- compute_base should not raise; belt and braces
        decision = _full(f"unexpected: {type(exc).__name__}: {exc}")

    reason = " ".join(_redact(decision.reason, token).split())[:300]
    print(f"ci_watermark_base: range={decision.range_spec} reason={reason}", file=err)
    print(decision.range_spec, file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
