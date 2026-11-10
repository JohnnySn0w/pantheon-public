"""Tests for tools._git_publish (T-5750): main-based single-file publication from
a clone that has an unrelated development branch checked out. Real git repos."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._git_publish import PublishKind, publish_file_edit_to_main  # noqa: E402

REAL_HOOKS = Path(__file__).resolve().parent.parent / ".githooks"
REL = "tickets/inbox/T-0001.md"
BASE_TEXT = (
    "---\nid: T-0001\ncreated: 2026-05-04T12:00:00Z\nrequester: orchestrator\n"
    "target: orchestrator\nstate: inbox\ndepends_on: []\nlabels: []\nstale_after: 7d\n---\n\n"
    "# T\n\n## Ask\n\nDo the thing.\n\n## Transition log\n\n"
    "- 2026-05-04T12:00:00Z [orchestrator] filed in inbox\n"
)


def git(cwd, *args, check=True):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise AssertionError(f"git {args} failed: {r.stderr}")
    return r


def add_marker(text: str) -> str:
    return text.replace("id: T-0001\n", "id: T-0001\nauto_delivered: true\n")


class DevBranchFixture(unittest.TestCase):
    """origin (bare) + clone on `dev`, one code commit ahead of main, real hook on."""

    def setUp(self):
        self._git_config_env = patch.dict(
            os.environ,
            {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
        )
        self._git_config_env.start()
        self.addCleanup(self._git_config_env.stop)
        old_identity = {
            key: os.environ.pop(key, None)
            for key in (
                "GIT_AUTHOR_NAME",
                "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL",
            )
        }

        def restore_identity() -> None:
            for key, value in old_identity.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore_identity)
        self.tmp = Path(tempfile.mkdtemp(prefix="t5750-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = self.tmp / "origin.git"
        self.clone = self.tmp / "clone"
        git(self.tmp, "init", "--bare", "-b", "main", str(self.origin))
        git(self.tmp, "init", "-b", "main", str(self.clone))
        git(self.clone, "config", "user.name", "Pantheon Test")
        git(self.clone, "config", "user.email", "pantheon-test@example.invalid")
        git(self.clone, "remote", "add", "origin", str(self.origin))
        (self.clone / "tickets/inbox").mkdir(parents=True)
        (self.clone / REL).write_text(BASE_TEXT)
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-m", "base")
        git(self.clone, "push", "origin", "main")
        git(self.clone, "fetch", "origin")
        git(self.clone, "config", "core.hooksPath", str(REAL_HOOKS))
        git(self.clone, "checkout", "-b", "dev")
        (self.clone / "tools").mkdir()
        (self.clone / "tools/x.py").write_text("print('wip')\n")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-m", "wip code")

    def last_commit_files(self):
        return git(self.origin, "diff-tree", "--no-commit-id", "--name-only", "-r", "main").stdout.split()


class PublishTests(DevBranchFixture):
    def test_publish_from_dev_branch_lands_only_the_ticket_file(self):
        head_before = git(self.clone, "rev-parse", "HEAD").stdout
        r = publish_file_edit_to_main(self.clone, REL, add_marker, "tickets: stamp x")
        self.assertIs(r.kind, PublishKind.OK, r.detail)
        self.assertEqual(self.last_commit_files(), [REL])
        blob = git(self.origin, "show", f"main:{REL}").stdout
        self.assertIn("auto_delivered: true", blob)
        self.assertNotIn("tools/x.py", git(self.origin, "ls-tree", "-r", "--name-only", "main").stdout)
        self.assertEqual(r.published, blob.encode())
        # checked-out branch, index and working tree untouched
        self.assertEqual(git(self.clone, "rev-parse", "HEAD").stdout, head_before)
        self.assertEqual(git(self.clone, "status", "--porcelain").stdout, "")

    def test_real_seat_guard_blocks_head_push_but_passes_publish(self):
        """Run as a seat (a PATH shim makes the hook's `id -un` say 'watchman'): the
        old HEAD:main push from `dev` is refused by the real hook; publish passes it."""
        shim = self.tmp / "shim"
        shim.mkdir()
        (shim / "id").write_text("#!/bin/sh\necho watchman\n")
        (shim / "id").chmod(0o755)
        with patch.dict(os.environ, {"PATH": f"{shim}:{os.environ['PATH']}"}):
            old = git(self.clone, "push", "origin", "HEAD:main", check=False)
            self.assertNotEqual(old.returncode, 0)
            self.assertIn("CODE-path push", old.stderr)
            r = publish_file_edit_to_main(self.clone, REL, add_marker, "m")
        self.assertIs(r.kind, PublishKind.OK, r.detail)
        self.assertEqual(self.last_commit_files(), [REL])

    def test_mutates_origin_blob_not_stale_local_copy(self):
        other = self.tmp / "other"
        git(self.tmp, "clone", str(self.origin), str(other))
        (other / REL).write_text(BASE_TEXT + "newer line on main\n")
        git(other, "commit", "-am", "main moved")
        git(other, "push", "origin", "main")
        r = publish_file_edit_to_main(self.clone, REL, add_marker, "m")
        self.assertIs(r.kind, PublishKind.OK, r.detail)
        blob = git(self.origin, "show", f"main:{REL}").stdout
        self.assertIn("newer line on main", blob)
        self.assertIn("auto_delivered: true", blob)

    def test_non_ff_race_retries_on_new_tip(self):
        other = self.tmp / "other"
        git(self.tmp, "clone", str(self.origin), str(other))
        calls = {"n": 0}

        def mutate(text):
            calls["n"] += 1
            if calls["n"] == 1:  # origin advances after we read the tip
                (other / "ledger.txt").write_text("x")
                git(other, "add", "-A")
                git(other, "commit", "-m", "race")
                git(other, "push", "origin", "main")
            return add_marker(text)

        r = publish_file_edit_to_main(self.clone, REL, mutate, "m")
        self.assertIs(r.kind, PublishKind.OK, r.detail)
        self.assertEqual(calls["n"], 2)
        self.assertIn("ledger.txt", git(self.origin, "ls-tree", "-r", "--name-only", "main").stdout)
        self.assertEqual(self.last_commit_files(), [REL])

    def test_absent_file_reports_absent_and_pushes_nothing(self):
        tip = git(self.origin, "rev-parse", "main").stdout
        r = publish_file_edit_to_main(self.clone, "tickets/inbox/T-9999.md", add_marker, "m")
        self.assertIs(r.kind, PublishKind.ABSENT)
        self.assertEqual(git(self.origin, "rev-parse", "main").stdout, tip)

    def test_push_failure_is_failed_with_detail(self):
        git(self.clone, "remote", "set-url", "origin", str(self.tmp / "nope.git"))
        r = publish_file_edit_to_main(self.clone, REL, add_marker, "m")
        self.assertIs(r.kind, PublishKind.FAILED)
        self.assertTrue(r.detail)

    def test_sg_prefix_wraps_every_git_call(self):
        seen = []
        real_run = subprocess.run

        def fake_run(argv, *a, **kw):
            seen.append(argv)
            if argv[0] == "sg":  # emulate the group hop: run the inner command
                return real_run(["bash", "-c", argv[3]], *a, **kw)
            return real_run(argv, *a, **kw)

        with patch("tools._git_publish.subprocess.run", side_effect=fake_run):
            r = publish_file_edit_to_main(self.clone, REL, add_marker, "m", use_sg=True)
        self.assertIs(r.kind, PublishKind.OK, r.detail)
        # patch() hits the shared subprocess module, so ignore unrelated callers
        # (mirror leg uses cwd=, not -C): every `-C <clone>` call must be sg-wrapped.
        ours = [a for a in seen if a[0] == "sg" or "-C" in a]
        self.assertTrue(ours and all(a[0] == "sg" and a[1] == "pantheon-shared" for a in ours))


if __name__ == "__main__":
    unittest.main()
