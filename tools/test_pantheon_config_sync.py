"""Tests for tools/pantheon_config_sync.py — T-0211 Phase B + T-0213.

Mock subprocess.run to exercise the 5-state branch matrix without touching
git or the network. T-0213 adds alert-state file lifecycle tests.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import tools.pantheon_config_sync as sync


def _result(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestPantheonConfigSync(unittest.TestCase):
    def setUp(self) -> None:
        # T-4388: these tests script subprocess.run call-by-call; the push path's
        # rebase-state probes are tested against real git in TestPushMainNeverStrands.
        p = patch.object(sync, "_rebase_in_progress", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        # push_main takes the git mutation lock; with subprocess.run scripted, the
        # lockfile must not be resolved through git (pytest's conftest also sets this).
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        e = patch.dict(os.environ, {"PANTHEON_GIT_MUTATION_LOCKFILE": str(Path(tmp.name) / "lock")})
        e.start()
        self.addCleanup(e.stop)

    def _patch_subprocess(self, results_by_call: list):
        """Patch subprocess.run to return a sequence of results matched by call order."""
        mock = MagicMock(side_effect=results_by_call)
        return patch.object(subprocess, "run", mock), mock

    def test_pantheon_config_sync_clean_state_returns_0(self) -> None:
        # fetch ok; local_count=0; remote_count=0
        results = [_result(), _result(stdout="0\n"), _result(stdout="0\n")]
        with self._patch_subprocess(results)[0]:
            self.assertEqual(sync.main([]), 0)

    def test_pantheon_config_sync_local_ahead_pushes_returns_0(self) -> None:
        # fetch ok; local_count=2; remote_count=0; push ok
        results = [_result(), _result(stdout="2\n"), _result(stdout="0\n"), _result()]
        with self._patch_subprocess(results)[0]:
            self.assertEqual(sync.main([]), 0)

    def test_pantheon_config_sync_remote_ahead_pulls_returns_0(self) -> None:
        # fetch ok; local_count=0; remote_count=3; rev-parse=main; pull ok
        results = [
            _result(), _result(stdout="0\n"), _result(stdout="3\n"),
            _result(stdout="main\n"), _result(),
        ]
        with self._patch_subprocess(results)[0]:
            self.assertEqual(sync.main([]), 0)

    def test_pantheon_config_sync_divergent_state_returns_3(self) -> None:
        # fetch ok; both ahead -> DIVERGENT
        results = [_result(), _result(stdout="2\n"), _result(stdout="3\n")]
        with self._patch_subprocess(results)[0]:
            self.assertEqual(sync.main([]), 3)

    def test_pantheon_config_sync_push_failure_returns_4(self) -> None:
        # fetch ok; local ahead; push fails
        results = [
            _result(),
            _result(stdout="2\n"),
            _result(stdout="0\n"),
            _result(returncode=1, stderr="rejected"),
        ]
        with self._patch_subprocess(results)[0]:
            self.assertEqual(sync.main([]), 4)

    def test_pantheon_config_sync_pull_failure_returns_5(self) -> None:
        # fetch ok; remote ahead; pull fails
        results = [
            _result(),
            _result(stdout="0\n"),
            _result(stdout="3\n"),
            _result(returncode=1, stderr="merge conflict"),
        ]
        with self._patch_subprocess(results)[0]:
            self.assertEqual(sync.main([]), 5)

    def test_pantheon_config_sync_fetch_failure_returns_2(self) -> None:
        # fetch fails immediately
        results = [_result(returncode=1, stderr="network down")]
        with self._patch_subprocess(results)[0]:
            self.assertEqual(sync.main([]), 2)

    def test_determine_state_uses_main_not_head_when_head_on_feature_branch(self) -> None:
        """T-0214: determine_state must compare local `main` vs origin/main, NOT HEAD.

        When HEAD is on a feature branch with commits not on main, the OLD code
        would report local_ahead=True. The fix uses explicit `main` refspec so
        the feature branch's tip is invisible to the sync timer.
        """
        # Mock sequence: fetch ok; rev-list count for origin/main..main = "0"
        # (main is NOT ahead); rev-list count for main..origin/main = "3"
        # (main is BEHIND origin/main). Pull path triggers; HEAD reports feat;
        # refspec-fetch is used instead of pull.
        results = [
            _result(),  # fetch
            _result(stdout="0\n"),  # rev-list origin/main..main
            _result(stdout="3\n"),  # rev-list main..origin/main
            _result(stdout="feat/T-XXXX-test\n"),  # rev-parse HEAD
            _result(),  # fetch origin main:main (refspec update)
        ]
        ctx, mock_run = self._patch_subprocess(results)
        with ctx:
            self.assertEqual(sync.main([]), 0)
            calls = [tuple(c.args[0]) for c in mock_run.call_args_list]
            # Verify the rev-list commands use explicit `main`, not HEAD
            self.assertIn(("git", "rev-list", "--count", "origin/main..main"), calls)
            self.assertIn(("git", "rev-list", "--count", "main..origin/main"), calls)
            # Verify refspec-fetch was used (NOT pull-from-current-branch)
            self.assertIn(("git", "fetch", "origin", "main:main"), calls)
            # Verify pull-from-current was NOT used (would have advanced feat branch)
            self.assertNotIn(("git", "pull", "--ff-only", "origin", "main"), calls)

    def test_pull_uses_pull_command_when_head_is_on_main(self) -> None:
        """T-0214: when HEAD IS on main, fall back to `git pull --ff-only` since
        the refspec-fetch form would be rejected for the checked-out ref."""
        results = [
            _result(),  # fetch
            _result(stdout="0\n"),  # rev-list origin/main..main
            _result(stdout="3\n"),  # rev-list main..origin/main
            _result(stdout="main\n"),  # rev-parse HEAD = main
            _result(),  # git pull --ff-only origin main
        ]
        ctx, mock_run = self._patch_subprocess(results)
        with ctx:
            self.assertEqual(sync.main([]), 0)
            calls = [tuple(c.args[0]) for c in mock_run.call_args_list]
            # Verify pull command was used (HEAD on main path)
            self.assertIn(("git", "pull", "--ff-only", "origin", "main"), calls)
            # Verify refspec-fetch was NOT used (would fail on checked-out ref)
            self.assertNotIn(("git", "fetch", "origin", "main:main"), calls)

    def test_refspec_fetch_falls_back_to_plain_fetch_when_another_worktree_has_main(self) -> None:
        """T-0222: when ANY worktree has main checked out (e.g., transient /tmp HO
        worktree), `git fetch origin main:main` refuses with 'refusing to fetch
        into branch'. Fall back to plain `git fetch origin main` — leaves local
        main stale this cycle but exits 0 (no alert state). Next cycle when HEAD
        lands on main, the normal pull path completes the update.
        """
        refusing_err = (
            "fatal: refusing to fetch into branch 'refs/heads/main' "
            "checked out at '/tmp/pantheon-config-ho'"
        )
        results = [
            _result(),  # fetch (determine_state)
            _result(stdout="0\n"),  # rev-list origin/main..main
            _result(stdout="3\n"),  # rev-list main..origin/main
            _result(stdout="feat/T-XXXX-test\n"),  # rev-parse HEAD
            _result(returncode=1, stderr=refusing_err),  # fetch origin main:main → refuses
            _result(),  # fetch origin main (plain fallback) → ok
        ]
        ctx, mock_run = self._patch_subprocess(results)
        with ctx:
            self.assertEqual(sync.main([]), 0)
            calls = [tuple(c.args[0]) for c in mock_run.call_args_list]
            self.assertIn(("git", "fetch", "origin", "main:main"), calls)
            self.assertIn(("git", "fetch", "origin", "main"), calls)
            # And no pull-from-current-branch (we are on feat)
            self.assertNotIn(("git", "pull", "--ff-only", "origin", "main"), calls)

    def test_refspec_fetch_non_worktree_error_still_raises_pull_failed(self) -> None:
        """T-0222: only the specific 'refusing to fetch into branch' error
        triggers the fallback. Other fetch errors (network down, etc.) must
        still surface as pull_failed (exit 5) — they are not benign-defer cases.
        """
        network_err = "fatal: unable to access 'https://github.com/...': Could not resolve host"
        results = [
            _result(),  # fetch (determine_state)
            _result(stdout="0\n"),  # rev-list origin/main..main
            _result(stdout="3\n"),  # rev-list main..origin/main
            _result(stdout="feat/T-XXXX-test\n"),  # rev-parse HEAD
            _result(returncode=1, stderr=network_err),  # fetch origin main:main → unrelated failure
        ]
        ctx, mock_run = self._patch_subprocess(results)
        with ctx, tempfile.TemporaryDirectory() as tmp:
            alert_path = Path(tmp) / "alert.json"
            with patch.object(sync, "_ALERT_STATE_PATH", alert_path):
                self.assertEqual(sync.main([]), 5)
                # Alert state should be written for pull_failed
                self.assertTrue(alert_path.exists())


class TestPantheonConfigSyncAlertState(unittest.TestCase):
    """T-0213: alert-state file lifecycle on each non-clean exit + rotation on clean."""

    def setUp(self) -> None:
        # T-4388: these tests script subprocess.run call-by-call; the push path's
        # rebase-state probes are tested against real git in TestPushMainNeverStrands.
        p = patch.object(sync, "_rebase_in_progress", return_value=False)
        p.start()
        self.addCleanup(p.stop)
        # push_main takes the git mutation lock; with subprocess.run scripted, the
        # lockfile must not be resolved through git (pytest's conftest also sets this).
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        e = patch.dict(os.environ, {"PANTHEON_GIT_MUTATION_LOCKFILE": str(Path(tmp.name) / "lock")})
        e.start()
        self.addCleanup(e.stop)

    def _patch_subprocess(self, results_by_call: list):
        mock = MagicMock(side_effect=results_by_call)
        return patch.object(subprocess, "run", mock), mock

    def test_clean_sync_clears_existing_alert(self) -> None:
        """Existing alert state rotates to .consumed.<epoch> on a clean fire."""
        with tempfile.TemporaryDirectory() as tmp:
            tmppath = Path(tmp)
            alert_path = tmppath / "pantheon_config_sync_alert.json"
            alert_path.write_text(json.dumps({
                "alert_class": "push_failed", "exit_code": 4,
                "stderr_excerpt": "older", "first_seen_at": int(time.time()) - 600,
            }), encoding="utf-8")
            results = [_result(), _result(stdout="0\n"), _result(stdout="0\n")]
            with patch.object(sync, "_ALERT_STATE_PATH", alert_path), \
                 self._patch_subprocess(results)[0]:
                self.assertEqual(sync.main([]), 0)
            self.assertFalse(alert_path.exists())
            # Rotated artifact survives
            rotated = list(tmppath.glob("pantheon_config_sync_alert.json.consumed.*"))
            self.assertEqual(len(rotated), 1)

    def test_divergent_writes_alert_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            alert_path = Path(tmp) / "alert.json"
            results = [_result(), _result(stdout="2\n"), _result(stdout="3\n")]
            with patch.object(sync, "_ALERT_STATE_PATH", alert_path), \
                 self._patch_subprocess(results)[0]:
                self.assertEqual(sync.main([]), 3)
            payload = json.loads(alert_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["alert_class"], "divergent")
            self.assertEqual(payload["exit_code"], 3)
            self.assertIn("DIVERGENT", payload["stderr_excerpt"])
            self.assertIsInstance(payload["first_seen_at"], int)

    def test_push_failed_writes_alert_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            alert_path = Path(tmp) / "alert.json"
            results = [
                _result(), _result(stdout="2\n"), _result(stdout="0\n"),
                _result(returncode=1, stderr="rejected — no SSH key"),
            ]
            with patch.object(sync, "_ALERT_STATE_PATH", alert_path), \
                 self._patch_subprocess(results)[0]:
                self.assertEqual(sync.main([]), 4)
            payload = json.loads(alert_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["alert_class"], "push_failed")
            self.assertEqual(payload["exit_code"], 4)

    def test_pull_failed_writes_alert_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            alert_path = Path(tmp) / "alert.json"
            results = [
                _result(), _result(stdout="0\n"), _result(stdout="3\n"),
                _result(stdout="main\n"),  # rev-parse HEAD = main
                _result(returncode=1, stderr="merge conflict"),
            ]
            with patch.object(sync, "_ALERT_STATE_PATH", alert_path), \
                 self._patch_subprocess(results)[0]:
                self.assertEqual(sync.main([]), 5)
            payload = json.loads(alert_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["alert_class"], "pull_failed")

    def test_fetch_failed_writes_alert_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            alert_path = Path(tmp) / "alert.json"
            results = [_result(returncode=1, stderr="network down")]
            with patch.object(sync, "_ALERT_STATE_PATH", alert_path), \
                 self._patch_subprocess(results)[0]:
                self.assertEqual(sync.main([]), 2)
            payload = json.loads(alert_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["alert_class"], "fetch_failed")
            self.assertEqual(payload["exit_code"], 2)

    def test_repeated_failure_preserves_first_seen_at(self) -> None:
        """Same alert_class on consecutive non-clean fires keeps the original first_seen_at."""
        with tempfile.TemporaryDirectory() as tmp:
            alert_path = Path(tmp) / "alert.json"
            original_ts = int(time.time()) - 900  # 15 min ago
            alert_path.write_text(json.dumps({
                "alert_class": "divergent", "exit_code": 3,
                "stderr_excerpt": "prior", "first_seen_at": original_ts,
            }), encoding="utf-8")
            results = [_result(), _result(stdout="2\n"), _result(stdout="3\n")]
            with patch.object(sync, "_ALERT_STATE_PATH", alert_path), \
                 self._patch_subprocess(results)[0]:
                sync.main([])
            payload = json.loads(alert_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["first_seen_at"], original_ts)
            # But the stderr_excerpt reflects the current fire
            self.assertIn("DIVERGENT", payload["stderr_excerpt"])

    def test_stderr_excerpt_truncated(self) -> None:
        """Long stderr is clipped to _STDERR_EXCERPT_MAX bytes."""
        long_err = "x" * 5000
        with tempfile.TemporaryDirectory() as tmp:
            alert_path = Path(tmp) / "alert.json"
            results = [
                _result(), _result(stdout="2\n"), _result(stdout="0\n"),
                _result(returncode=1, stderr=long_err),
            ]
            with patch.object(sync, "_ALERT_STATE_PATH", alert_path), \
                 self._patch_subprocess(results)[0]:
                sync.main([])
            payload = json.loads(alert_path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["stderr_excerpt"]), sync._STDERR_EXCERPT_MAX)



class TestPushMainNeverStrands(unittest.TestCase):
    """T-4388, real git: push_main never pushes over a rebase found at entry, and aborts
    a rebase that appears during the push (the pre-push freshness hook's)."""

    def _git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.bare = base / "bare.git"
        self._git(base, "init", "-q", "--bare", "-b", "main", str(self.bare))
        self.repo = base / "repo"
        self._git(base, "clone", "-q", str(self.bare), str(self.repo))
        for k, v in (("user.email", "t@example"), ("user.name", "T"), ("core.hooksPath", "/dev/null")):
            self._git(self.repo, "config", k, v)
        (self.repo / "f.txt").write_text("base\n")
        self._git(self.repo, "add", "f.txt")
        self._git(self.repo, "commit", "-q", "-m", "base")
        self._git(self.repo, "push", "-q", "origin", "main")
        p = patch.object(sync, "REPO", self.repo)
        p.start()
        self.addCleanup(p.stop)
        # Hermetic outside pytest's conftest too: never take the real repo's lockfile.
        e = patch.dict(os.environ, {"PANTHEON_GIT_MUTATION_LOCKFILE": str(base / "lock")})
        e.start()
        self.addCleanup(e.stop)
        self.addCleanup(self._tmp.cleanup)

    def _head(self, cwd):
        return self._git(cwd, "rev-parse", "HEAD").stdout.strip()

    def test_rebase_at_entry_refuses_without_pushing(self) -> None:
        (self.repo / "f.txt").write_text("local\n")
        self._git(self.repo, "commit", "-q", "-am", "local")
        (Path(self._git(self.repo, "rev-parse", "--absolute-git-dir").stdout.strip()) / "rebase-merge").mkdir()
        before = self._head(self.bare)
        with self.assertRaises(sync.SyncError) as cm:
            sync.push_main()
        self.assertIn("already in progress", str(cm.exception))
        self.assertEqual(self._head(self.bare), before, "nothing may be pushed")
        self.assertTrue(sync._rebase_in_progress(), "the rebase is left as found")

    def test_rebase_left_by_the_push_is_aborted(self) -> None:
        (self.repo / "f.txt").write_text("local\n")
        self._git(self.repo, "commit", "-q", "-am", "local")
        local = self._head(self.repo)
        real_run = sync.run

        def run(cmd, check=True):
            if cmd[:2] == ["git", "push"]:
                # What the hook does on a conflict: start a rebase and exit 1.
                self._git(self.repo, "checkout", "-q", "--detach", "HEAD~1")
                (self.repo / "f.txt").write_text("theirs\n")
                self._git(self.repo, "commit", "-q", "-am", "theirs")
                theirs = self._head(self.repo)
                self._git(self.repo, "checkout", "-q", "main")
                r = subprocess.run(["git", "rebase", theirs], cwd=str(self.repo), capture_output=True, text=True)
                assert r.returncode != 0 and sync._rebase_in_progress(), "fixture: conflicted rebase"
                return subprocess.CompletedProcess(cmd, 1, "", "pre-push: rebase conflict\nerror: failed to push some refs")
            return real_run(cmd, check=check)

        with patch.object(sync, "run", run):
            with self.assertRaises(sync.SyncError) as cm:
                sync.push_main()
        self.assertIn("aborted", str(cm.exception))
        self.assertFalse(sync._rebase_in_progress(), "the hook's rebase must not be left behind")
        self.assertEqual(self._head(self.repo), local, "main is back at the local commit")

    def test_push_runs_inside_the_git_mutation_lock(self) -> None:
        import contextlib
        state = {"held": False, "pushed_while_held": None}

        @contextlib.contextmanager
        def lock(**k):
            state["held"] = True
            try:
                yield
            finally:
                state["held"] = False

        real_run = sync.run

        def run(cmd, check=True):
            if cmd[:2] == ["git", "push"]:
                state["pushed_while_held"] = state["held"]
            return real_run(cmd, check=check)

        (self.repo / "f.txt").write_text("local\n")
        self._git(self.repo, "commit", "-q", "-am", "local")
        with patch("tools._git_utils.git_mutation_lock", lock), patch.object(sync, "run", run):
            sync.push_main()
        self.assertTrue(state["pushed_while_held"], "probe/push/clean-up must hold the shared lock")

    def test_busy_lock_is_a_sync_error_without_pushing(self) -> None:
        from tools._git_utils import GitMutationLockTimeout

        def lock(**k):
            raise GitMutationLockTimeout("busy")

        before = self._head(self.bare)
        (self.repo / "f.txt").write_text("local\n")
        self._git(self.repo, "commit", "-q", "-am", "local")
        with patch("tools._git_utils.git_mutation_lock", lock):
            with self.assertRaises(sync.SyncError) as cm:
                sync.push_main()
        self.assertIn("lock busy", str(cm.exception))
        self.assertEqual(self._head(self.bare), before)

    def test_clean_push_lands(self) -> None:
        (self.repo / "f.txt").write_text("local\n")
        self._git(self.repo, "commit", "-q", "-am", "local")
        sync.push_main()
        self.assertEqual(self._head(self.bare), self._head(self.repo))

    def test_clean_push_dual_writes_to_the_mirror(self) -> None:
        """T-4691: after origin accepts, the ORG-101 mirror leg runs for the same ref."""
        (self.repo / "f.txt").write_text("local\n")
        self._git(self.repo, "commit", "-q", "-am", "local")
        with patch("tools._git_utils.mirror_push_after_origin", return_value="mirror pushed") as m:
            sync.push_main()
        m.assert_called_once_with(self.repo, "main")

    def test_failed_push_never_touches_the_mirror(self) -> None:
        real_run = sync.run

        def run(cmd, check=True):
            if cmd[:2] == ["git", "push"]:
                return subprocess.CompletedProcess(cmd, 1, "", "error: failed to push some refs")
            return real_run(cmd, check=check)

        with patch.object(sync, "run", run), \
             patch("tools._git_utils.mirror_push_after_origin") as m:
            with self.assertRaises(sync.SyncError):
                sync.push_main()
        m.assert_not_called()

class TestDefaultSafeContractT0262AdditiveVariant(unittest.TestCase):
    """T-0262 (additive variant per the operator msg_1412): --commit-prod is no-op
    additive (default still commits + pushes); --dry-run skips push/pull;
    strict refusal only when PANTHEON_SYNC_STRICT_DEFAULT_SAFE=1 env is set.
    """

    def _patch(self, results: list):
        return patch.object(subprocess, "run", MagicMock(side_effect=results))

    def test_no_flag_does_NOT_refuse_in_default_mode(self) -> None:
        # Additive variant: no mode → default prod behavior, NOT refusal.
        results = [_result(), _result(stdout="0\n"), _result(stdout="0\n")]
        with self._patch(results):
            self.assertEqual(sync.main([]), 0)  # clean state, no commits

    def test_commit_prod_alone_does_NOT_refuse_in_default_mode(self) -> None:
        results = [_result(), _result(stdout="0\n"), _result(stdout="0\n")]
        with self._patch(results):
            self.assertEqual(sync.main(["--commit-prod"]), 0)

    def test_dry_run_skips_push_pull(self) -> None:
        # fetch ok; local ahead — but --dry-run must NOT push.
        results = [_result(), _result(stdout="2\n"), _result(stdout="0\n")]
        with self._patch(results) as mock:
            rc = sync.main(["--dry-run"])
        self.assertEqual(rc, 0)
        # Only 3 subprocess calls (fetch + 2 rev-list). No push.
        self.assertEqual(mock.call_count, 3)

    def test_commit_prod_plus_dry_run_is_ambiguous(self) -> None:
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()):
            rc = sync.main(["--commit-prod", "--dry-run"])
        self.assertEqual(rc, 64)

    def test_strict_default_safe_env_refuses_no_mode(self) -> None:
        from contextlib import redirect_stderr
        os.environ["PANTHEON_SYNC_STRICT_DEFAULT_SAFE"] = "1"
        try:
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = sync.main([])
            self.assertEqual(rc, 64)
            self.assertIn("refusing to run without explicit mode", buf.getvalue())
        finally:
            os.environ.pop("PANTHEON_SYNC_STRICT_DEFAULT_SAFE", None)

    def test_strict_default_safe_env_accepts_commit_prod(self) -> None:
        os.environ["PANTHEON_SYNC_STRICT_DEFAULT_SAFE"] = "1"
        try:
            results = [_result(), _result(stdout="0\n"), _result(stdout="0\n")]
            with self._patch(results):
                self.assertEqual(sync.main(["--commit-prod"]), 0)
        finally:
            os.environ.pop("PANTHEON_SYNC_STRICT_DEFAULT_SAFE", None)


# T-4940: isolate the seat's state dir for plain-unittest runs (pytest's conftest
# _isolate_state_dir already does this per test). See tools/_seat_state_isolation.py.
try:
    from tools import _seat_state_isolation
except ImportError:  # pragma: no cover - run from tools/
    import _seat_state_isolation  # type: ignore[no-redef]


def setUpModule() -> None:
    _seat_state_isolation.start()


def tearDownModule() -> None:
    _seat_state_isolation.stop()


def test_sync_adapter_preserves_push_half_and_refuses_divergence_without_reset() -> None:
    adapter = MagicMock(return_value=(True, ""))
    with patch.object(sync, "determine_state", return_value=(True, False)), \
         patch.object(sync, "push_main") as push, \
         patch.object(sync, "_clear_alert_state"):
        assert sync.main([], reconcile_adapter=adapter) == 0
    push.assert_called_once_with(); adapter.assert_not_called()

    adapter.reset_mock()
    with patch.object(sync, "determine_state", return_value=(True, True)), \
         patch.object(sync, "_write_alert_state"):
        assert sync.main([], reconcile_adapter=adapter) == 3
    adapter.assert_not_called()

    refusing = MagicMock(return_value=(False, "diverged; no reset"))
    with patch.object(sync, "determine_state", return_value=(False, True)), \
         patch.object(sync, "_write_alert_state"):
        assert sync.main([], reconcile_adapter=refusing) == 5
    refusing.assert_called_once_with(sync.REPO)


if __name__ == "__main__":
    unittest.main()
