"""tools/git_fixture.py — the shared bubble-safe real-git test harness (T-3183 §1).

BUBBLE-SAFE DISCIPLINE (phase-1 hard constraint). Every git repo built through
this harness is a REAL git repo created under a per-case
`tempfile.TemporaryDirectory()`. `BubbleGit` holds `self.root` (the tempdir), and
EVERY path it constructs flows through `BubbleGit.path(...)`, which asserts the
result is under `self.root`. No caller names, reads, or mutates any path outside
the bubble; no real/live checkout is ever operated on. Functional git mutation
(clone/fetch/checkout/push) happens ONLY here, against these temporary repos.

This module promotes the git helpers that grew up inside `test_repo_reconciler.py`
(`_GIT_ENV`, `_git`, `_seed`, `_advance_remote`, `_clone_seat`, the under-tempdir
`_path` guard) into one importable place shared by the reconciler tests
(`test_repo_reconciler.py`), the bounded assembly pilot (`assembly_transaction.py`
+ its tests), and the extraction executor (`repository_extraction.py` + its
tests). The bodies are the same ones those tests already relied on.

`git()` defaults to `check=True` (a fixture builder step that fails IS a test
error), but accepts `check=False` so a fail-closed primitive — the staging
engine in `assembly_transaction.py`, the extraction executor — can inspect a
non-zero returncode (e.g. `git cat-file -e <pin>` probing presence) instead of
raising.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

# Hermetic git environment: no global/system config, no credential/terminal
# prompt, deterministic author/committer identity. Shared by every fixture repo
# so tests are reproducible regardless of the seat's git config.
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "T2951 Test",
    "GIT_AUTHOR_EMAIL": "t2951@example.invalid",
    "GIT_COMMITTER_NAME": "T2951 Test",
    "GIT_COMMITTER_EMAIL": "t2951@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
}


def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run one hermetic `git` command against a bubble repo.

    `check=True` (default) matches the fixture-builder posture the reconciler
    tests always used: a failed clone/commit/push is a test error. Fail-closed
    primitives pass `check=False` to read a non-zero returncode themselves.
    """
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=check,
        env=GIT_ENV,
    )


class BubbleGit:
    """A per-case bubble of real git repos rooted at one tempdir.

    Every path is minted through `path(...)`, which asserts it stays under
    `root` — the bubble-safe guard. The builder methods (`new_remote`, `seed`,
    `advance_remote`, `clone_seat`) carry the exact bodies the reconciler test
    matrix has always used, so behaviour is unchanged by the move.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def path(self, *parts: str) -> str:
        """Mint a path under the bubble; assert it does not escape `root`."""
        p = (self.root / Path(*parts)).resolve()
        assert str(p) == str(self.root) or str(p).startswith(str(self.root) + os.sep), (
            f"bubble-safe violation: {p} is outside {self.root}"
        )
        return str(p)

    def new_remote(self, name: str = "remote") -> str:
        remote = self.path(f"{name}.git")
        git("init", "--bare", "-b", "main", remote)
        return remote

    def seed(self, remote: str, *, content: str = "v1", name: str = "seed") -> str:
        work = self.path(f"_{name}_work")
        git("clone", remote, work)
        wp = Path(work)
        (wp / "file.txt").write_text(content, encoding="utf-8")
        git("add", "file.txt", cwd=wp)
        git("commit", "-m", "seed", cwd=wp)
        git("push", "origin", "main", cwd=wp)
        return git("rev-parse", "HEAD", cwd=wp).stdout.strip()

    def advance_remote(self, remote: str, *, content: str = "v2", name: str = "seed") -> str:
        work = self.path(f"_{name}_work")
        wp = Path(work)
        if not wp.exists():
            git("clone", remote, work)
        else:
            git("pull", "origin", "main", cwd=wp)
        (wp / "file.txt").write_text(content, encoding="utf-8")
        git("add", "file.txt", cwd=wp)
        git("commit", "-m", "advance", cwd=wp)
        git("push", "origin", "main", cwd=wp)
        return git("rev-parse", "HEAD", cwd=wp).stdout.strip()

    def clone_seat(self, remote: str, name: str) -> str:
        target = self.path(name)
        git("clone", remote, target)
        return target


class BubbleSafeCase(unittest.TestCase):
    """A `unittest.TestCase` mixin owning one bubble's `TemporaryDirectory`
    lifecycle. Exposes `self.root` and a `BubbleGit` (`self.bubble`) plus thin
    passthroughs to its builders, so a test body reads `self.new_remote(...)`,
    `self.seed(...)`, `self.path(...)`, etc. directly."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        self.bubble = BubbleGit(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def path(self, *parts: str) -> str:
        return self.bubble.path(*parts)

    def new_remote(self, name: str = "remote") -> str:
        return self.bubble.new_remote(name)

    def seed(self, remote: str, *, content: str = "v1", name: str = "seed") -> str:
        return self.bubble.seed(remote, content=content, name=name)

    def advance_remote(self, remote: str, *, content: str = "v2", name: str = "seed") -> str:
        return self.bubble.advance_remote(remote, content=content, name=name)

    def clone_seat(self, remote: str, name: str) -> str:
        return self.bubble.clone_seat(remote, name)
