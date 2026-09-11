#!/usr/bin/env python3
"""Hermetic tests for the prune-stale-teams SessionStart hook (T-3074)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_HOOK = Path(__file__).resolve().parent / "prune-stale-teams.py"
_spec = importlib.util.spec_from_file_location("prune_stale_teams", _HOOK)
pst = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pst)


def _touch(p: Path, mtime: float | None = None) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    if mtime is not None:
        os.utime(p, (mtime, mtime))


def _set_dir_mtime(p: Path, mtime: float) -> None:
    os.utime(p, (mtime, mtime))


class ShortIdTest(unittest.TestCase):
    def test_slices_first_eight_chars_of_full_uuid(self) -> None:
        self.assertEqual(pst._short_id("e60969bd-99d2-4df1-95d5-c9aac04c43a1"), "e60969bd")

    def test_short_input_is_idempotent(self) -> None:
        self.assertEqual(pst._short_id("e60969bd"), "e60969bd")


class RunTest(unittest.TestCase):
    """Drives run() directly against tmp roots -- no real ~/.claude touched."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.teams_root = Path(self._tmp.name) / "teams"
        self.projects_root = Path(self._tmp.name) / "projects"
        self.teams_root.mkdir()
        self.projects_root.mkdir()
        self.now = time.time()
        self.stale = self.now - 7 * 3600   # 7h old, past the 6h default bar
        self.recent = self.now - 1 * 3600  # 1h old, within the 6h default bar

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _mk_team_dir(self, short_id: str, *, config_mtime: float) -> Path:
        d = self.teams_root / f"session-{short_id}"
        _touch(d / "config.json", config_mtime)
        _set_dir_mtime(d, config_mtime)
        return d

    def test_keeps_current_session_regardless_of_staleness(self) -> None:
        current = self._mk_team_dir("e60969bd", config_mtime=self.stale)
        rc = pst.run(self.teams_root, self.projects_root, "e60969bd", 6.0, dry_run=False)
        self.assertEqual(rc, 0)
        self.assertTrue(current.exists(), "current session dir must never be pruned")

    def test_keeps_dir_with_recent_file_under_it(self) -> None:
        stale_other = self._mk_team_dir("11111111", config_mtime=self.stale)
        # A fresh inbox file arrives after config.json went stale.
        _touch(stale_other / "inboxes" / "someone.json", self.recent)
        rc = pst.run(self.teams_root, self.projects_root, "e60969bd", 6.0, dry_run=False)
        self.assertEqual(rc, 0)
        self.assertTrue(stale_other.exists(), "a recent file anywhere under the dir keeps it")

    def test_keeps_dir_whose_directory_mtime_moved_recently(self) -> None:
        # T-3074 coordinator note: an inbox FILE being deleted still bumps its
        # parent DIRECTORY's mtime -- that must count as activity even though
        # every remaining file is stale.
        stale_other = self._mk_team_dir("22222222", config_mtime=self.stale)
        inbox_dir = stale_other / "inboxes"
        inbox_dir.mkdir()
        _set_dir_mtime(inbox_dir, self.recent)  # simulate: a file inside was just deleted
        rc = pst.run(self.teams_root, self.projects_root, "e60969bd", 6.0, dry_run=False)
        self.assertEqual(rc, 0)
        self.assertTrue(stale_other.exists(), "a recently-touched subdirectory keeps the dir")

    def test_keeps_dir_with_recent_matching_transcript(self) -> None:
        stale_other = self._mk_team_dir("33333333", config_mtime=self.stale)
        proj = self.projects_root / "-home-claude"
        proj.mkdir()
        _touch(proj / "33333333-full-uuid-suffix.jsonl", self.recent)
        rc = pst.run(self.teams_root, self.projects_root, "e60969bd", 6.0, dry_run=False)
        self.assertEqual(rc, 0)
        self.assertTrue(stale_other.exists(), "a fresh matching transcript keeps the dir")

    def test_prunes_stale_dir_with_absent_transcript(self) -> None:
        stale_other = self._mk_team_dir("44444444", config_mtime=self.stale)
        rc = pst.run(self.teams_root, self.projects_root, "e60969bd", 6.0, dry_run=False)
        self.assertEqual(rc, 0)
        self.assertFalse(stale_other.exists(), "stale dir + absent transcript must be pruned")

    def test_prunes_stale_dir_with_stale_transcript(self) -> None:
        stale_other = self._mk_team_dir("55555555", config_mtime=self.stale)
        proj = self.projects_root / "-home-claude"
        proj.mkdir()
        _touch(proj / "55555555-full-uuid-suffix.jsonl", self.stale)
        rc = pst.run(self.teams_root, self.projects_root, "e60969bd", 6.0, dry_run=False)
        self.assertEqual(rc, 0)
        self.assertFalse(stale_other.exists(), "stale dir + stale transcript must be pruned")

    def test_symlinked_session_dir_is_skipped_and_target_survives(self) -> None:
        # Reviewer nice-to-have: a session-* entry that's a symlink must never
        # be treated as a real team dir -- following it into is_dir()/rmtree
        # could delete whatever it points at, possibly outside teams_root.
        real_target = Path(self._tmp.name) / "real_target_outside_teams_root"
        _touch(real_target / "config.json", self.stale)
        _set_dir_mtime(real_target, self.stale)

        symlink_entry = self.teams_root / "session-11112222"
        symlink_entry.symlink_to(real_target, target_is_directory=True)

        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = pst.run(self.teams_root, self.projects_root, "e60969bd", 6.0, dry_run=False)
        self.assertEqual(rc, 0)
        self.assertIn("skipping symlink", buf.getvalue())
        self.assertTrue(symlink_entry.is_symlink(), "the symlink entry itself is left alone")
        self.assertTrue(real_target.exists(), "the symlink's target must survive untouched")

    def test_dry_run_prunes_nothing_but_reports(self) -> None:
        stale_other = self._mk_team_dir("66666666", config_mtime=self.stale)
        rc = pst.run(self.teams_root, self.projects_root, "e60969bd", 6.0, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertTrue(stale_other.exists(), "dry-run must never delete")

    def test_permission_error_on_one_dir_does_not_abort_others(self) -> None:
        bad = self._mk_team_dir("77777777", config_mtime=self.stale)
        good = self._mk_team_dir("88888888", config_mtime=self.stale)

        real_rmtree = pst.shutil.rmtree

        def flaky_rmtree(path, *a, **kw):
            if str(path) == str(bad):
                raise OSError("permission denied (simulated)")
            return real_rmtree(path, *a, **kw)

        with mock.patch.object(pst.shutil, "rmtree", side_effect=flaky_rmtree):
            rc = pst.run(self.teams_root, self.projects_root, "e60969bd", 6.0, dry_run=False)

        self.assertEqual(rc, 0, "a per-dir removal failure must not change the exit code")
        self.assertTrue(bad.exists(), "the dir that failed to remove is still there")
        self.assertFalse(good.exists(), "the other stale dir is still pruned")

    def test_unresolvable_current_session_is_a_full_noop(self) -> None:
        stale_other = self._mk_team_dir("99999999", config_mtime=self.stale)
        # No --current-session, empty stdin (nothing piped in this direct call),
        # no env var, and an empty projects_root -> every source comes back None.
        # This runs in-process (unlike the SubprocessEndToEndTest cases), so it
        # inherits THIS interpreter's own environment directly -- which on this
        # live seat genuinely carries CLAUDE_CODE_SESSION_ID. Pop it for the
        # whole body (not just the resolve_current_session probe) so run()'s
        # own internal resolution is exercised under the same "truly absent"
        # condition, restored after regardless of outcome.
        old = os.environ.pop(pst._ENV_SESSION_ID, None)
        try:
            current_id, source = pst.resolve_current_session(None, self.projects_root)
            self.assertIsNone(current_id)
            self.assertEqual(source, "unresolved")
            rc = pst.run(self.teams_root, self.projects_root, None, 6.0, dry_run=False)
        finally:
            if old is not None:
                os.environ[pst._ENV_SESSION_ID] = old
        self.assertEqual(rc, 0)
        self.assertTrue(stale_other.exists(), "unresolved current session -> touch nothing")

    def test_run_propagates_and_main_guard_is_the_fail_open_boundary(self) -> None:
        # run() itself is a plain composable function and does not swallow
        # exceptions -- the fail-open contract lives specifically at the
        # `__main__` catch-all (exercised end-to-end by the subprocess tests
        # below, e.g. test_unreadable_teams_root_still_exits_zero).
        self._mk_team_dir("aaaaaaaa", config_mtime=self.stale)
        with mock.patch.object(pst, "find_stale_dirs", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                pst.run(self.teams_root, self.projects_root, "e60969bd", 6.0, dry_run=False)


class SubprocessEndToEndTest(unittest.TestCase):
    """Drives the hook as a real subprocess -- the actual stdin/CLI contract."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.teams_root = Path(self._tmp.name) / "teams"
        self.projects_root = Path(self._tmp.name) / "projects"
        self.teams_root.mkdir()
        self.projects_root.mkdir()
        self.now = time.time()
        self.stale = self.now - 7 * 3600

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(
        self, *, stdin_payload: dict | None, extra_args: list[str],
        env_overrides: dict | None = None,
    ) -> tuple[int, str]:
        args = [
            sys.executable, str(_HOOK),
            "--teams-root", str(self.teams_root),
            "--projects-root", str(self.projects_root),
            *extra_args,
        ]
        env = dict(os.environ)
        if env_overrides:
            env.update(env_overrides)
        p = subprocess.run(
            args,
            input=(json.dumps(stdin_payload) if stdin_payload is not None else ""),
            capture_output=True, text=True, env=env,
        )
        return p.returncode, p.stderr

    def test_session_id_from_stdin_json_keeps_current_prunes_stale_sibling(self) -> None:
        current = self.teams_root / "session-e60969bd"
        current.mkdir()
        (current / "config.json").write_text("{}")
        os.utime(current / "config.json", (self.stale, self.stale))
        os.utime(current, (self.stale, self.stale))

        stale_sibling = self.teams_root / "session-63ebc964"
        stale_sibling.mkdir()
        (stale_sibling / "config.json").write_text("{}")
        os.utime(stale_sibling / "config.json", (self.stale, self.stale))
        os.utime(stale_sibling, (self.stale, self.stale))

        rc, err = self._run(
            stdin_payload={
                "session_id": "e60969bd-99d2-4df1-95d5-c9aac04c43a1",
                "hook_event_name": "SessionStart",
                "source": "startup",
            },
            extra_args=[],
        )
        self.assertEqual(rc, 0)
        self.assertTrue(current.exists(), "current session (from stdin JSON) must survive")
        self.assertFalse(stale_sibling.exists(), "stale sibling must be pruned")
        self.assertIn("pruned", err)
        self.assertIn("session-63ebc964", err)

    def test_session_id_from_env_var_when_no_flag_and_empty_stdin(self) -> None:
        # Review round 1: CLAUDE_CODE_SESSION_ID is what's actually live on
        # this seat's Bash-tool child processes (verified via `env`), not
        # CLAUDE_SESSION_ID. No --current-session flag, stdin JSON present
        # but without a session_id key -- resolution must fall through
        # stdin-json (no usable field) to the env var.
        current = self.teams_root / "session-e60969bd"
        current.mkdir()
        (current / "config.json").write_text("{}")
        os.utime(current / "config.json", (self.stale, self.stale))
        os.utime(current, (self.stale, self.stale))

        stale_sibling = self.teams_root / "session-63ebc964"
        stale_sibling.mkdir()
        (stale_sibling / "config.json").write_text("{}")
        os.utime(stale_sibling / "config.json", (self.stale, self.stale))
        os.utime(stale_sibling, (self.stale, self.stale))

        rc, err = self._run(
            stdin_payload={},
            extra_args=[],
            env_overrides={"CLAUDE_CODE_SESSION_ID": "e60969bd-99d2-4df1-95d5-c9aac04c43a1"},
        )
        self.assertEqual(rc, 0)
        self.assertTrue(current.exists(), "current session (from env var) must survive")
        self.assertFalse(stale_sibling.exists(), "stale sibling must be pruned")
        self.assertIn("via env", err)

    def test_dry_run_via_cli_reports_and_keeps(self) -> None:
        stale = self.teams_root / "session-abcdefab"
        stale.mkdir()
        (stale / "config.json").write_text("{}")
        os.utime(stale / "config.json", (self.stale, self.stale))
        os.utime(stale, (self.stale, self.stale))

        rc, err = self._run(
            stdin_payload=None,
            extra_args=["--current-session", "e60969bd", "--dry-run"],
        )
        self.assertEqual(rc, 0)
        self.assertTrue(stale.exists())
        self.assertIn("would prune", err)

    def test_unreadable_teams_root_still_exits_zero(self) -> None:
        # Real permission-denied at the teams-root level (not the mocked
        # rmtree case above, which covers per-dir removal failures) --
        # exercises the actual __main__ fail-open boundary end-to-end.
        stale = self.teams_root / "session-dddddddd"
        stale.mkdir()
        (stale / "config.json").write_text("{}")
        os.utime(stale / "config.json", (self.stale, self.stale))
        os.utime(stale, (self.stale, self.stale))
        os.chmod(self.teams_root, 0o000)
        try:
            rc, err = self._run(
                stdin_payload=None,
                extra_args=["--current-session", "e60969bd"],
            )
        finally:
            os.chmod(self.teams_root, 0o755)  # restore so tearDown can clean up
        self.assertEqual(rc, 0, f"stderr was: {err}")

    def test_malformed_stdin_still_exits_zero_and_noops(self) -> None:
        stale = self.teams_root / "session-cccccccc"
        stale.mkdir()
        (stale / "config.json").write_text("{}")
        os.utime(stale / "config.json", (self.stale, self.stale))
        os.utime(stale, (self.stale, self.stale))

        # Strip the env fallback explicitly: this seat's own live Bash-tool
        # env carries a real CLAUDE_CODE_SESSION_ID (inherited by default),
        # which would otherwise resolve "current" and defeat the point of
        # this test -- it needs every source to genuinely come back empty.
        env = dict(os.environ)
        env.pop(pst._ENV_SESSION_ID, None)
        p = subprocess.run(
            [
                sys.executable, str(_HOOK),
                "--teams-root", str(self.teams_root),
                "--projects-root", str(self.projects_root),
            ],
            input="{not json", capture_output=True, text=True, env=env,
        )
        self.assertEqual(p.returncode, 0)
        # No --current-session, malformed stdin, no env var, empty
        # projects_root -> unresolved source -> full no-op. The stale dir
        # survives because we can't tell what "current" means, not because
        # it looked recent.
        self.assertTrue(stale.exists())


if __name__ == "__main__":
    unittest.main()
