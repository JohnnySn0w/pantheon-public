"""Tests for tools/_organ_lifecycle.py (T-2696).

Direct unit coverage for the primitives extracted from tools/consume_onboard.py
(T-2679) and tools/mailbox_onboard.py (T-2683). None of these had a direct,
isolated test before centralization -- each was previously exercised only
indirectly, through onboard()/hub_setup()/provision() integration in their
respective test_*_onboard.py suites (both of which stay unchanged and green;
this file adds coverage, it does not replace any of theirs).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools._organ_lifecycle import (
    CommandResult,
    FakeCommandRunner,
    _ensure_dir,
    _git_head,
    _write_if_changed,
    group_membership_lookup,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class FakeCommandRunnerTests(unittest.TestCase):
    def test_scripted_call_returns_scripted_result_and_is_recorded(self) -> None:
        runner = FakeCommandRunner(scripted={("echo", "hi"): CommandResult(0, "hi\n", "")})
        result = runner.run(["echo", "hi"])
        self.assertEqual(result, CommandResult(0, "hi\n", ""))
        self.assertEqual(runner.calls, [("echo", "hi")])

    def test_unscripted_call_returns_default_and_is_still_recorded(self) -> None:
        runner = FakeCommandRunner()
        result = runner.run(["whatever"])
        self.assertEqual(result, CommandResult(0, "", ""))
        self.assertEqual(runner.calls, [("whatever",)])


class EnsureDirTests(unittest.TestCase):
    def test_creates_missing_dir_and_reports_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sub" / "dir"
            created = _ensure_dir(target, 0o700)
            self.assertTrue(created)
            self.assertTrue(target.is_dir())

    def test_existing_dir_reports_not_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sub"
            target.mkdir()
            created = _ensure_dir(target, 0o700)
            self.assertFalse(created)


class WriteIfChangedTests(unittest.TestCase):
    def test_first_write_creates_file_and_reports_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sub" / "file.txt"
            changed = _write_if_changed(target, b"hello")
            self.assertTrue(changed)
            self.assertEqual(target.read_bytes(), b"hello")

    def test_identical_content_is_a_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "file.txt"
            _write_if_changed(target, b"hello")
            changed = _write_if_changed(target, b"hello")
            self.assertFalse(changed)

    def test_different_content_overwrites_and_reports_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "file.txt"
            _write_if_changed(target, b"hello")
            changed = _write_if_changed(target, b"goodbye")
            self.assertTrue(changed)
            self.assertEqual(target.read_bytes(), b"goodbye")


class GitHeadTests(unittest.TestCase):
    def test_real_repo_returns_a_revision_string(self) -> None:
        revision = _git_head(REPO_ROOT)
        self.assertNotEqual(revision, "unversioned")
        self.assertTrue(revision)

    def test_non_repo_dir_returns_unversioned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(_git_head(Path(tmp)), "unversioned")


class FakeGrp:
    def __init__(self, gr_mem, gr_gid) -> None:
        self.gr_mem = gr_mem
        self.gr_gid = gr_gid


class FakePw:
    def __init__(self, pw_gid) -> None:
        self.pw_gid = pw_gid


class GroupMembershipLookupTests(unittest.TestCase):
    """Pins the 4 branches consume_onboard.RealGroupMembership.is_member() and
    mailbox_onboard.RealHostIdentity.user_in_group() both delegate to post-
    extraction -- neither tool's own suite exercised these branches directly
    before (only end-to-end via FakeGroupMembership/FakeHostIdentity)."""

    def test_group_missing_returns_false(self) -> None:
        with patch("tools._organ_lifecycle.grp.getgrnam", side_effect=KeyError):
            ok, detail = group_membership_lookup("alice", "nogroup")
        self.assertFalse(ok)
        self.assertIn("does not exist", detail)

    def test_member_via_gr_mem_returns_true(self) -> None:
        with patch("tools._organ_lifecycle.grp.getgrnam", return_value=FakeGrp(["alice"], 100)):
            ok, detail = group_membership_lookup("alice", "somegroup")
        self.assertTrue(ok)
        self.assertIn("is a member of", detail)

    def test_account_missing_returns_false(self) -> None:
        with patch("tools._organ_lifecycle.grp.getgrnam", return_value=FakeGrp([], 100)), patch(
            "tools._organ_lifecycle.pwd.getpwnam", side_effect=KeyError
        ):
            ok, detail = group_membership_lookup("ghost", "somegroup")
        self.assertFalse(ok)
        self.assertIn("does not exist", detail)

    def test_member_via_primary_gid_returns_true(self) -> None:
        with patch("tools._organ_lifecycle.grp.getgrnam", return_value=FakeGrp([], 100)), patch(
            "tools._organ_lifecycle.pwd.getpwnam", return_value=FakePw(100)
        ):
            ok, detail = group_membership_lookup("alice", "somegroup")
        self.assertTrue(ok)
        self.assertIn("primary group", detail)

    def test_not_a_member_returns_false(self) -> None:
        with patch("tools._organ_lifecycle.grp.getgrnam", return_value=FakeGrp([], 100)), patch(
            "tools._organ_lifecycle.pwd.getpwnam", return_value=FakePw(999)
        ):
            ok, detail = group_membership_lookup("alice", "somegroup")
        self.assertFalse(ok)
        self.assertIn("is not a member of", detail)


if __name__ == "__main__":
    unittest.main()
