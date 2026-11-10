"""Tests for backup_staleness_check.py (T-2886) — the pure age logic and the
main() verdict paths, with the network call stubbed; plus (T-4575) the
installed-copy drift check, against real temp files."""
import datetime as dt
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import backup_staleness_check as bsc

NOW = dt.datetime(2026, 8, 13, 12, 0, 0, tzinfo=dt.timezone.utc)


class AgeHours(unittest.TestCase):
    def test_z_suffix_utc(self):
        self.assertAlmostEqual(bsc.age_hours("2026-08-13T00:00:00Z", NOW), 12.0)

    def test_explicit_offset(self):
        # 06:00 at -04:00 == 10:00 UTC -> 2h before NOW
        self.assertAlmostEqual(bsc.age_hours("2026-08-13T06:00:00-04:00", NOW), 2.0)

    def test_naive_treated_as_utc(self):
        self.assertAlmostEqual(bsc.age_hours("2026-08-13T11:00:00", NOW), 1.0)

    def test_future_commit_is_negative_age_not_stale(self):
        self.assertLess(bsc.age_hours("2026-08-13T13:00:00Z", NOW), 0.0)


class MainVerdicts(unittest.TestCase):
    def _run(self, iso=None, err=None, max_age="48", drift=None, lib_drift=None):
        patcher = (
            mock.patch.object(bsc, "latest_commit_date", side_effect=err)
            if err else
            mock.patch.object(bsc, "latest_commit_date", return_value=iso)
        )
        with patcher, mock.patch.object(bsc.time, "sleep"), \
                mock.patch.object(bsc, "installed_copy_drift", return_value=drift), \
                mock.patch.object(bsc, "installed_lib_drift", return_value=lib_drift):
            return bsc.main(["--max-age-hours", max_age])

    def test_fresh_exits_zero(self):
        recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self._run(iso=recent), 0)

    def test_stale_exits_one(self):
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=72)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self._run(iso=old), 1)

    def test_api_error_fails_closed(self):
        self.assertEqual(self._run(err=RuntimeError("gh api failed")), 1)

    def test_fresh_but_drifted_exits_one(self):
        recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self._run(iso=recent, drift="differs"), 1)


    def test_fresh_but_lib_drifted_exits_one(self):
        recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(self._run(iso=recent, lib_drift="differs"), 1)


class InstalledLibDrift(unittest.TestCase):
    """T-5993: the mirror leg imports _git_utils from a root-owned lib copy. Absent lib =
    the leg is not installed (it prints 'skipped'), not drift. Once present, every file must
    pass the installed_copy_drift test against its tracked source, and the dirs must be
    owned by root and writable by nobody else."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        base = Path(td.name)
        self.src = base / "src"
        (self.src / "tools").mkdir(parents=True)
        for rel in bsc.LIB_FILES:
            (self.src / rel).write_text(f"# {rel}\n")
        self.lib = base / "lib"
        (self.lib / "tools").mkdir(parents=True)
        for rel in bsc.LIB_FILES:
            (self.lib / rel).write_bytes((self.src / rel).read_bytes())
            (self.lib / rel).chmod(0o644)
        self.lib.chmod(0o755)
        (self.lib / "tools").chmod(0o755)

    def _drift(self, lib=None):
        return bsc.installed_lib_drift(lib or self.lib, self.src, owner_uid=os.geteuid())

    def test_the_lib_set_is_the_mirror_legs_imports(self):
        self.assertEqual(set(bsc.LIB_FILES), {"tools/_git_utils.py", "tools/remote_resolve.py", "repos.yaml"})

    def test_an_absent_lib_is_not_drift(self):
        self.assertIsNone(self._drift(self.lib.parent / "nope"))

    def test_an_identical_root_owned_lib_is_clean(self):
        self.assertIsNone(self._drift())

    def test_a_differing_file_is_drift(self):
        (self.lib / "tools" / "_git_utils.py").write_text("# tampered\n")
        self.assertIn("differs", self._drift())

    def test_a_missing_file_in_a_present_lib_is_drift(self):
        (self.lib / "repos.yaml").unlink()
        self.assertIsNotNone(self._drift())

    def test_a_group_writable_file_is_drift(self):
        (self.lib / "tools" / "remote_resolve.py").chmod(0o664)
        self.assertIn("writable", self._drift())

    def test_a_group_writable_dir_is_drift(self):
        (self.lib / "tools").chmod(0o775)
        self.assertIn("writable", self._drift())

    def test_a_lib_not_owned_by_root_is_drift(self):
        self.assertIn("owned", bsc.installed_lib_drift(self.lib, self.src, owner_uid=os.geteuid() + 1))

    def test_main_prints_the_lib_reinstall_hint(self):
        recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        err = io.StringIO()
        with mock.patch.object(bsc, "latest_commit_date", return_value=recent), \
                mock.patch.object(bsc, "installed_copy_drift", return_value=None), \
                mock.patch.object(bsc, "installed_lib_drift", return_value="x differs"), \
                contextlib.redirect_stderr(err):
            self.assertEqual(bsc.main([]), 1)
        self.assertIn(bsc.LIB_REINSTALL_HINT, err.getvalue())


class InstalledCopyDrift(unittest.TestCase):
    """T-4575: root runs a root-owned copy of the backup script, never the checkout.
    The daily check fails (and alerts) when that copy is missing, differs from the
    tracked source, or is writable by anyone but its owner."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name)
        self.source = self.dir / "vps_state_backup.sh"
        self.source.write_bytes(b"#!/bin/bash\necho backup\n")
        self.installed = self.dir / "pantheon-vps-state-backup"
        self.installed.write_bytes(self.source.read_bytes())
        self.installed.chmod(0o755)

    def _drift(self, root_uid=None):
        uid = os.geteuid() if root_uid is None else root_uid
        return bsc.installed_copy_drift(self.installed, self.source, owner_uid=uid)

    def test_identical_owner_only_writable_copy_is_clean(self):
        self.assertIsNone(self._drift())

    def test_a_missing_copy_is_drift(self):
        self.installed.unlink()
        self.assertIn("cannot read", self._drift())

    def test_a_differing_copy_is_drift(self):
        self.source.write_bytes(b"#!/bin/bash\necho backup v2\n")
        self.assertIn("differs", self._drift())

    def test_a_group_or_world_writable_copy_is_drift(self):
        for mode in (0o775, 0o757):
            with self.subTest(mode=oct(mode)):
                self.installed.chmod(mode)
                self.assertIn("writable", self._drift())

    def test_a_copy_not_owned_by_root_is_drift(self):
        self.assertIn("owned by uid", self._drift(root_uid=os.geteuid() + 1))

    def test_a_symlink_to_an_identical_copy_is_drift(self):
        target = self.dir / "elsewhere"
        target.write_bytes(self.source.read_bytes())
        target.chmod(0o755)
        self.installed.unlink()
        self.installed.symlink_to(target)
        self.assertIn("is a symlink", self._drift())

    def test_a_fifo_is_drift_and_does_not_block(self):
        self.installed.unlink()
        os.mkfifo(self.installed, 0o755)
        self.assertIn("not a regular file", self._drift())

    def test_an_unreadable_source_is_drift(self):
        self.source.unlink()
        self.assertIn("cannot read", self._drift())

    def test_the_default_source_is_the_tracked_script(self):
        self.assertEqual(bsc.SOURCE_SCRIPT.name, "vps_state_backup.sh")
        self.assertTrue(bsc.SOURCE_SCRIPT.is_file())


if __name__ == "__main__":
    unittest.main()
