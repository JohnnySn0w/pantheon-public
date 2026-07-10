#!/usr/bin/env python3
"""Tests for tools/spine_groom_cross_repo.py (T-0666)."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# Bootstrap: same sys.path trick as inbox_consumer.py — pass canonical smoke
# `python3 -m unittest tools.test_spine_groom_cross_repo` AND a standalone run.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import spine_groom_cross_repo as x  # noqa: E402


def _git(args, cwd):
    subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)


def _mk_repo(base: Path, name: str) -> Path:
    r = base / name
    r.mkdir()
    _git(["init", "-q"], r)
    _git(["config", "user.email", "t@t"], r)
    _git(["config", "user.name", "t"], r)
    return r


class BootstrapTests(unittest.TestCase):
    def test_needs_bootstrap_true_when_no_region(self):
        self.assertTrue(x.needs_bootstrap("# adr\n\nsome text\n"))

    def test_needs_bootstrap_false_when_region_present(self):
        t = f"# adr\n{x.SENTINEL_START}\n{x.SENTINEL_END}\n"
        self.assertFalse(x.needs_bootstrap(t))

    def test_bootstrap_appends_region_idempotent(self):
        once = x.bootstrap_text("# adr\n\ntext\n")
        self.assertIn(x.SENTINEL_START, once)
        self.assertIn(x.SENTINEL_END, once)
        twice = x.bootstrap_text(once)
        self.assertEqual(once, twice)  # no double-append

    def test_bootstrap_handles_no_trailing_newline(self):
        out = x.bootstrap_text("# adr")
        self.assertIn(x.SENTINEL_START, out)


class RunCrossRepoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def _docs(self, repo):
        return {repo: {"notes": x.notes_handler}}

    def test_dry_run_writes_nothing(self):
        r = _mk_repo(self.base, "repoA")
        (r / "notes").mkdir()
        (r / "notes" / "README.md").write_text("# notes\n")
        (r / "notes" / "foo.md").write_text("# Foo\n\nFoo desc.\n")
        res = x.run_cross_repo(self.base, apply=False, push=False, docs=self._docs("repoA"))
        self.assertTrue(any("would" in line for line in res))
        # nothing written
        self.assertNotIn(x.SENTINEL_START, (r / "notes" / "README.md").read_text())

    def test_apply_bootstraps_and_grooms_and_commits(self):
        r = _mk_repo(self.base, "repoA")
        (r / "notes").mkdir()
        (r / "notes" / "README.md").write_text("# notes\n")
        (r / "notes" / "foo.md").write_text("# Foo\n\nFoo description.\n")
        res = x.run_cross_repo(self.base, apply=True, push=False, docs=self._docs("repoA"))
        readme = (r / "notes" / "README.md").read_text()
        self.assertIn(x.SENTINEL_START, readme)        # bootstrapped
        self.assertIn("foo.md", readme)                # groomed (row written)
        self.assertTrue(any("commit ok" in line for line in res))
        # idempotent: second apply = noop, no new commit
        res2 = x.run_cross_repo(self.base, apply=True, push=False, docs=self._docs("repoA"))
        self.assertTrue(all("commit" not in line or "noop" in line or "wrote" not in line for line in res2))

    def test_creates_readme_when_missing(self):
        r = _mk_repo(self.base, "repoA")
        (r / "notes").mkdir()
        (r / "notes" / "bar.md").write_text("# Bar\n\nBar desc.\n")  # no README
        x.run_cross_repo(self.base, apply=True, push=False, docs=self._docs("repoA"))
        self.assertTrue((r / "notes" / "README.md").exists())
        self.assertIn(x.SENTINEL_START, (r / "notes" / "README.md").read_text())

    def test_missing_dir_skipped_gracefully(self):
        _mk_repo(self.base, "repoA")  # no notes/ dir
        res = x.run_cross_repo(self.base, apply=True, push=False, docs=self._docs("repoA"))
        self.assertTrue(any("MISSING dir" in line for line in res))

    def test_non_git_repo_skipped(self):
        (self.base / "repoA").mkdir()  # not a git repo
        res = x.run_cross_repo(self.base, apply=True, push=False, docs=self._docs("repoA"))
        self.assertTrue(any("NOT a git repo" in line for line in res))

    def test_push_failure_reverts_local_commit(self):
        # No remote configured → push fails → the commit must be reverted so the
        # working tree never strands a divergent/unpushable commit (protected-branch case).
        r = _mk_repo(self.base, "repoA")
        (r / "notes").mkdir()
        (r / "notes" / "README.md").write_text("# notes\n")
        (r / "notes" / "foo.md").write_text("# Foo\n\nFoo desc.\n")
        _git(["add", "-A"], r); _git(["commit", "-q", "-m", "base"], r)
        head_before = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"],
                                     capture_output=True, text=True).stdout.strip()
        res = x.run_cross_repo(self.base, apply=True, push=True, docs=self._docs("repoA"))
        head_after = subprocess.run(["git", "-C", str(r), "rev-parse", "HEAD"],
                                    capture_output=True, text=True).stdout.strip()
        self.assertTrue(any("push FAILED" in line and "reverted" in line for line in res))
        self.assertEqual(head_before, head_after)  # commit reverted
        # working tree clean (no stranded changes)
        dirty = subprocess.run(["git", "-C", str(r), "status", "--porcelain"],
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(dirty, "")


if __name__ == "__main__":
    unittest.main()
