"""Main-based, branch-independent publication of a single-file edit (T-5750).

Why: a seat clone can sit on a development branch carrying unrelated code
commits. Committing a ticket-frontmatter edit onto that branch and pushing
`HEAD:main` would carry the code too, and the pre-push CODE-path guard
(rightly) refuses. This module builds the commit on `origin/main` with git
plumbing and a PRIVATE index, then pushes that commit's sha. The checked-out
branch, its index and its working tree are never touched, so the push carries
exactly one file regardless of what the clone has checked out.

Public surface:
    PublishKind                 -- OK / ABSENT / FAILED.
    PublishResult               -- (kind, detail, published_bytes).
    publish_file_edit_to_main   -- fetch, mutate origin/main's blob, commit-tree,
                                   push <sha>:refs/heads/main with non-ff retry.
"""

from __future__ import annotations

import enum
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, NamedTuple

_NON_FF_MARKERS = ("rejected", "non-fast-forward", "fetch first")
_REF_LOCK_MARKERS = ("cannot lock ref",)
_FETCH_ATTEMPTS = 3
_FETCH_BACKOFF_S = 0.5
_GIT_TIMEOUT_S = 120


class PublishKind(enum.Enum):
    OK = "ok"          # the commit is on origin/main
    ABSENT = "absent"  # the file does not exist on origin/main (nothing to edit)
    FAILED = "failed"  # fetch / plumbing / push failed; detail says which


class PublishResult(NamedTuple):
    kind: PublishKind
    detail: str
    published: bytes | None  # exact blob bytes now on origin/main (OK only)


def _run(repo: Path, args: list[str], use_sg: bool, env: dict | None = None):
    """One git call against `repo`. `env` vars ride as an `env K=V` prefix so they
    survive the `sg pantheon-shared -c` group hop (T-0514) unchanged."""
    argv = ["git", "-C", str(repo), *args]
    if env:
        argv = ["env", *[f"{k}={v}" for k, v in env.items()], *argv]
    if use_sg:
        argv = ["sg", "pantheon-shared", "-c", shlex.join(argv)]
    try:
        return subprocess.run(argv, capture_output=True, check=False, timeout=_GIT_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return subprocess.CompletedProcess(argv, 124, b"", repr(exc).encode())


def _err(res) -> str:
    return (res.stderr or res.stdout or b"").decode("utf-8", "replace").strip()


def _fetch_main(repo: Path, use_sg: bool) -> str | None:
    """Fetch origin/main with bounded retry on the ref-lock race. None = ok."""
    detail = ""
    for attempt in range(1, _FETCH_ATTEMPTS + 1):
        res = _run(repo, ["fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"], use_sg)
        if res.returncode == 0:
            return None
        detail = _err(res)
        if any(m in detail.lower() for m in _REF_LOCK_MARKERS) and attempt < _FETCH_ATTEMPTS:
            time.sleep(_FETCH_BACKOFF_S * attempt)
            continue
        break
    return f"git fetch origin main failed: {detail}"


def _build_commit(repo, rel_path, new_bytes, mode, tip, message, use_sg) -> tuple[str | None, str]:
    """commit-tree on a private index: (sha, "") or (None, detail)."""
    workdir = Path(tempfile.mkdtemp(prefix="t5750-publish-"))
    try:
        blob_file = workdir / "blob"
        blob_file.write_bytes(new_bytes)
        idx = {"GIT_INDEX_FILE": str(workdir / "index")}
        res = _run(repo, ["hash-object", "-w", str(blob_file)], use_sg)
        if res.returncode != 0:
            return None, f"hash-object failed: {_err(res)}"
        blob = res.stdout.decode().strip()
        steps = (
            ["read-tree", tip],
            ["update-index", "--add", "--cacheinfo", f"{mode},{blob},{rel_path}"],
        )
        for step in steps:
            res = _run(repo, step, use_sg, env=idx)
            if res.returncode != 0:
                return None, f"{step[0]} failed: {_err(res)}"
        res = _run(repo, ["write-tree"], use_sg, env=idx)
        if res.returncode != 0:
            return None, f"write-tree failed: {_err(res)}"
        tree = res.stdout.decode().strip()
        res = _run(repo, ["commit-tree", tree, "-p", tip, "-m", message], use_sg)
        if res.returncode != 0:
            return None, f"commit-tree failed: {_err(res)}"
        return res.stdout.decode().strip(), ""
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def publish_file_edit_to_main(
    repo: Path,
    rel_path: str,
    mutate: Callable[[str], str],
    message: str,
    *,
    use_sg: bool = False,
    attempts: int = 3,
) -> PublishResult:
    """Apply `mutate(text) -> text` to `rel_path` as it exists on origin/main and
    publish the result as ONE commit whose only change is that file.

    Never touches the checked-out branch, index or working tree (private
    GIT_INDEX_FILE). On a non-fast-forward the tip is refetched and the edit is
    re-applied to the NEW blob (up to `attempts`), so a concurrent writer's
    change to the same file is preserved, not clobbered. `mutate` exceptions
    propagate (caller decides the classification). A no-op mutation publishes
    nothing and reports OK with the unchanged bytes.

    `use_sg` wraps every git call in the T-0514 `sg pantheon-shared` hop. The
    ORG-101 mirror dual-write leg runs best-effort after origin accepts; origin
    -down failover to the mirror is deliberately NOT offered here (callers treat
    an unpublished stamp as FAILED and a later cycle reconciles).
    """
    from tools._git_utils import mirror_push_after_origin  # lazy: avoid import cycle

    detail = "no attempt made"
    for attempt in range(1, attempts + 1):
        err = _fetch_main(repo, use_sg)
        if err:
            return PublishResult(PublishKind.FAILED, err, None)
        tip_res = _run(repo, ["rev-parse", "refs/remotes/origin/main"], use_sg)
        if tip_res.returncode != 0:
            return PublishResult(PublishKind.FAILED, f"rev-parse origin/main: {_err(tip_res)}", None)
        tip = tip_res.stdout.decode().strip()
        ls = _run(repo, ["ls-tree", tip, "--", rel_path], use_sg)
        if ls.returncode != 0:
            return PublishResult(PublishKind.FAILED, f"ls-tree failed: {_err(ls)}", None)
        if not ls.stdout.strip():
            return PublishResult(PublishKind.ABSENT, f"{rel_path} not on origin/main", None)
        mode = ls.stdout.decode().split()[0]
        cat = _run(repo, ["cat-file", "blob", f"{tip}:{rel_path}"], use_sg)
        if cat.returncode != 0:
            return PublishResult(PublishKind.FAILED, f"cat-file failed: {_err(cat)}", None)
        old_bytes = cat.stdout
        new_bytes = mutate(old_bytes.decode("utf-8")).encode("utf-8")
        if new_bytes == old_bytes:
            return PublishResult(PublishKind.OK, "no change vs origin/main; nothing pushed", old_bytes)
        sha, err = _build_commit(repo, rel_path, new_bytes, mode, tip, message, use_sg)
        if sha is None:
            return PublishResult(PublishKind.FAILED, err, None)
        refspec = f"{sha}:refs/heads/main"
        sent = _run(repo, ["push", "origin", refspec], use_sg)
        if sent.returncode == 0:
            note = mirror_push_after_origin(repo, refspec)
            return PublishResult(
                PublishKind.OK,
                f"published {sha[:10]} (attempt {attempt})" + (f"; {note}" if note else ""),
                new_bytes,
            )
        detail = _err(sent)
        if not any(m in detail.lower() for m in _NON_FF_MARKERS):
            return PublishResult(PublishKind.FAILED, f"push failed (non-retryable): {detail}", None)
    return PublishResult(
        PublishKind.FAILED, f"push failed after {attempts} attempt(s) (non-ff): {detail}", None
    )
