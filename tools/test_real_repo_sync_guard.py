"""T-4167: under PANTHEON_TEST_NO_REAL_REPO_SYNC=1 the sync helpers never fast-forward
the REAL checkout, but still sync any other repo.

The CI flake behind T-4165: a Layer-3 test spawned session_reset_reconcile as a real
subprocess, and its pull_repo_or_warn fetched + fast-forwarded CI's shared checkout,
moving a ticket origin had just resolved out from under a concurrent reader. Real git
against throwaway repos: "the real checkout" is a tmp clone one commit behind its bare
origin, stood in for by patching _git_utils._REAL_REPO_ROOT.
"""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import _git_utils
from tools._git_utils import pull_or_rebase_strict, pull_repo_or_warn, pull_repo_strict

ENV = "PANTHEON_TEST_NO_REAL_REPO_SYNC"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _behind_clone(base: Path, name: str) -> Path:
    """A clone of a fresh bare origin, left exactly one commit behind it."""
    origin, clone, other = base / f"{name}.git", base / name, base / f"{name}-up"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True, capture_output=True)
    _git(other, "commit", "-q", "--allow-empty", "-m", "seed")
    _git(other, "push", "-q", "origin", "HEAD:main")
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    _git(other, "commit", "-q", "--allow-empty", "-m", "upstream advance")
    _git(other, "push", "-q", "origin", "HEAD:main")
    return clone.resolve()


class RealRepoSyncGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.real = _behind_clone(base, "real")
        self.head0 = _git(self.real, "rev-parse", "HEAD")
        for target, value in (("_REAL_REPO_ROOT", self.real), ("REPO_ROOT", self.real)):
            p = patch.object(_git_utils, target, value)
            p.start()
            self.addCleanup(p.stop)
        saved = os.environ.pop(ENV, None)
        self.addCleanup(lambda: os.environ.pop(ENV, None) if saved is None
                        else os.environ.__setitem__(ENV, saved))
        self.base = base

    def _moved(self, repo: Path, head0: str) -> bool:
        return _git(repo, "rev-parse", "HEAD") != head0

    def test_control_without_the_env_the_real_clone_is_fast_forwarded(self) -> None:
        # Proves the fixture CAN move HEAD, so the guarded "unchanged" below means something.
        self.assertEqual(pull_repo_strict(), (True, ""))
        self.assertTrue(self._moved(self.real, self.head0))

    def test_pull_repo_or_warn_skips_the_real_checkout(self) -> None:
        os.environ[ENV] = "1"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            pull_repo_or_warn("t4167")
        self.assertFalse(self._moved(self.real, self.head0))
        self.assertIn(ENV, err.getvalue())

    def test_pull_repo_strict_skips_the_real_checkout(self) -> None:
        os.environ[ENV] = "1"
        self.assertEqual(pull_repo_strict(), (True, ""))
        self.assertFalse(self._moved(self.real, self.head0))

    def test_pull_or_rebase_strict_skips_the_real_checkout(self) -> None:
        os.environ[ENV] = "1"
        self.assertEqual(pull_or_rebase_strict(cwd=self.real), (True, ""))
        self.assertFalse(self._moved(self.real, self.head0))

    def test_another_repo_is_still_synced_under_the_env(self) -> None:
        os.environ[ENV] = "1"
        other = _behind_clone(self.base, "other")
        head0 = _git(other, "rev-parse", "HEAD")
        self.assertEqual(pull_or_rebase_strict(cwd=other), (True, ""))
        self.assertTrue(self._moved(other, head0))

    def test_a_patched_repo_root_is_not_mistaken_for_the_real_one(self) -> None:
        os.environ[ENV] = "1"
        other = _behind_clone(self.base, "patched")
        head0 = _git(other, "rev-parse", "HEAD")
        with patch.object(_git_utils, "REPO_ROOT", other):
            self.assertEqual(pull_repo_strict(), (True, ""))
        self.assertTrue(self._moved(other, head0))

    def test_layer3_integration_env_sets_the_guard(self) -> None:
        from tools._test_fixtures import setup_integration_env, teardown_integration_env
        env = setup_integration_env()
        self.addCleanup(teardown_integration_env, env)
        self.assertEqual(env.env_overrides.get(ENV), "1")


if __name__ == "__main__":
    unittest.main()
