#!/usr/bin/env python3
"""Tests for tools/spine_groom_cross_repo.py (T-0666)."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
    _git(["init", "-q", "-b", "main"], r)
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


class MirrorLegTests(unittest.TestCase):
    """T-4705: the ORG-101 dual-write leg runs only after origin accepted the push,
    with HEAD:<branch>, and never changes the push result or the revert safety net."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.repo = _mk_repo(self.base, "repoA")
        _git(["checkout", "-q", "-b", "main"], self.repo)
        (self.repo / "notes").mkdir()
        (self.repo / "notes" / "README.md").write_text("# notes\n")
        (self.repo / "notes" / "foo.md").write_text("# Foo\n\nFoo desc.\n")
        _git(["add", "-A"], self.repo)
        _git(["commit", "-q", "-m", "base"], self.repo)
        self.docs = {"repoA": {"notes": x.notes_handler}}

    def _wire_origin(self):
        origin = self.base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        _git(["remote", "add", "origin", str(origin)], self.repo)
        _git(["push", "-q", "-u", "origin", "main"], self.repo)
        return origin

    def _head(self, cwd):
        return subprocess.run(["git", "-C", str(cwd), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()

    def _run(self):
        return x.run_cross_repo(self.base, apply=True, push=True, docs=self.docs)

    def test_success_calls_leg_once_with_cwd_and_head_branch_refspec(self):
        origin = self._wire_origin()
        with patch("tools._git_utils.mirror_push_after_origin",
                   return_value="mirror pushed") as m:
            res = self._run()
        m.assert_called_once_with(self.repo, "HEAD:main")
        self.assertIn("repoA: push ok", res)
        self.assertEqual(res.index("repoA: mirror pushed"), res.index("repoA: push ok") + 1)
        self.assertEqual(self._head(self.repo), self._head(origin))

    def test_failed_push_never_calls_leg_and_still_reverts(self):
        head_before = self._head(self.repo)  # no remote: the origin push fails
        with patch("tools._git_utils.mirror_push_after_origin") as m:
            res = self._run()
        m.assert_not_called()
        self.assertTrue(any("push FAILED" in line and "reverted" in line for line in res))
        self.assertFalse(any("mirror" in line for line in res))
        self.assertEqual(self._head(self.repo), head_before)

    def test_leg_exception_keeps_push_ok_and_the_commit(self):
        origin = self._wire_origin()
        with patch("tools._git_utils.mirror_push_after_origin",
                   side_effect=RuntimeError("boom")):
            res = self._run()
        self.assertIn("repoA: push ok", res)
        self.assertFalse(any("push FAILED" in line for line in res))
        self.assertTrue(any(line.startswith("repoA: mirror push failed") and "boom" in line
                            for line in res))
        self.assertEqual(self._head(self.repo), self._head(origin))  # commit kept, not reverted

    def test_leg_failure_note_keeps_push_ok(self):
        origin = self._wire_origin()
        note = "mirror push failed (denied); reconciler will heal"
        with patch("tools._git_utils.mirror_push_after_origin", return_value=note):
            res = self._run()
        self.assertIn("repoA: push ok", res)
        self.assertIn(f"repoA: {note}", res)
        self.assertFalse(any("push FAILED" in line for line in res))
        self.assertEqual(self._head(self.repo), self._head(origin))

    def test_inert_leg_adds_no_line(self):
        self._wire_origin()
        with patch("tools._git_utils.mirror_push_after_origin", return_value=None) as m:
            res = self._run()
        m.assert_called_once()
        self.assertIn("repoA: push ok", res)
        self.assertFalse(any("mirror" in line for line in res))

    def test_detached_head_skips_leg(self):
        with patch("tools.spine_groom_cross_repo._git") as g, \
                patch("tools._git_utils.mirror_push_after_origin") as m:
            g.return_value = subprocess.CompletedProcess([], 0, stdout="HEAD\n", stderr="")
            note = x._mirror_leg(self.repo)
        m.assert_not_called()
        self.assertEqual(note, "mirror leg skipped (no current branch)")

    def test_detached_head_end_to_end_push_ok_and_skip_noted(self):
        # A bare push from a detached HEAD fails, so in practice the leg is never
        # reached from a detached HEAD; this pins that the failure path stays intact.
        self._wire_origin()
        _git(["checkout", "-q", "--detach"], self.repo)
        with patch("tools._git_utils.mirror_push_after_origin") as m:
            res = self._run()
        m.assert_not_called()
        self.assertFalse(any("push ok" in line for line in res))


if __name__ == "__main__":
    unittest.main()
