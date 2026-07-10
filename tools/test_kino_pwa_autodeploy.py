"""Unit + integration tests for tools/kino_pwa_autodeploy.py (T-0316).

Run from repo root:
    python3 -m unittest tools.test_kino_pwa_autodeploy -v

Covers (per the T-0316 test matrix):
  (a) disabled-by-default → no-op exit 0, no subprocess fired
  (b) origin/main unchanged vs state file → no deploy
  (c) origin advanced → deploy path invoked (build→smoke→rsync→restart→verify)
  (d) smoke-build failure → NO rsync, NO restart, state file unchanged, non-zero exit
  (e) idempotency — state file gates re-deploy; SHA advanced only after full verify
  + flock concurrency guard; ff-only divergence abort; first-run (no state file)

All subprocess / git / systemctl calls are mocked. Tests NEVER touch the real
system: PANTHEON_STATE_DIR + the lockfile are redirected into a tempdir, and the
pipeline-step functions (which would otherwise sudo / build / rsync / restart) are
patched. No sudo, no bun, no rsync, no systemctl is invoked.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.kino_pwa_autodeploy as ad  # noqa: E402


# ---------------------------------------------------------------------------
# Shared base: tempdir state + lockfile redirection, env hygiene.
# ---------------------------------------------------------------------------


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        os.environ["PANTHEON_STATE_DIR"] = str(self.state_dir)
        os.environ["PANTHEON_PWA_AUTODEPLOY_LOCKFILE"] = str(self.tmp / "lock")
        # Default-enabled for most tests; the disabled test overrides.
        os.environ["PANTHEON_PWA_AUTODEPLOY_ENABLED"] = "1"
        # Drop any lingering lock fd reference from a prior test.
        if hasattr(ad.main, "_lock_fd"):
            try:
                ad.main._lock_fd.close()
            except Exception:
                pass
            del ad.main._lock_fd

    def tearDown(self) -> None:
        for k in (
            "PANTHEON_STATE_DIR",
            "PANTHEON_PWA_AUTODEPLOY_LOCKFILE",
            "PANTHEON_PWA_AUTODEPLOY_ENABLED",
        ):
            os.environ.pop(k, None)
        if hasattr(ad.main, "_lock_fd"):
            try:
                ad.main._lock_fd.close()
            except Exception:
                pass
            del ad.main._lock_fd
        self._tmp.cleanup()

    # -- helpers ------------------------------------------------------------

    def _set_state(self, sha: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        ad._state_file().write_text(sha + "\n", encoding="utf-8")

    def _patch_pipeline(self, **overrides):
        """Patch all pipeline-step functions to no-op success unless overridden.

        Returns a contextlib.ExitStack-like list of started patchers; callers use
        it via the `with` helper below. Each step returns (True, "") by default.
        """
        defaults = {
            "advance_source": (True, ""),
            "build": (True, ""),
            "smoke": (True, ""),
            "rsync_to_deploy": (True, ""),
            "restart_kino": (True, ""),
            "verify_active": (True, "active"),
        }
        defaults.update(overrides)
        return defaults


# ---------------------------------------------------------------------------
# (a) Disabled-by-default → no-op exit 0.
# ---------------------------------------------------------------------------


class TestDisabled(_Base):
    def test_disabled_by_default_no_op(self) -> None:
        os.environ.pop("PANTHEON_PWA_AUTODEPLOY_ENABLED", None)
        with patch("tools.kino_pwa_autodeploy.subprocess.run") as mock_run:
            rc = ad.main([])
        self.assertEqual(rc, ad.EXIT_OK)
        mock_run.assert_not_called()

    def test_wrong_value_treated_as_disabled(self) -> None:
        os.environ["PANTHEON_PWA_AUTODEPLOY_ENABLED"] = "0"
        with patch("tools.kino_pwa_autodeploy.subprocess.run") as mock_run:
            rc = ad.main([])
        self.assertEqual(rc, ad.EXIT_OK)
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# (b) origin/main unchanged vs state file → no deploy.
# ---------------------------------------------------------------------------


class TestNoChange(_Base):
    def test_origin_matches_state_no_deploy(self) -> None:
        self._set_state("abc123")
        with patch("tools.kino_pwa_autodeploy.fetch_origin_sha", return_value=(True, "abc123")), \
             patch("tools.kino_pwa_autodeploy._deploy") as mock_deploy:
            rc = ad.main([])
        self.assertEqual(rc, ad.EXIT_OK)
        mock_deploy.assert_not_called()

    def test_fetch_failure_returns_exit_fetch(self) -> None:
        self._set_state("abc123")
        with patch("tools.kino_pwa_autodeploy.fetch_origin_sha",
                   return_value=(False, "network down")), \
             patch("tools.kino_pwa_autodeploy._deploy") as mock_deploy:
            rc = ad.main([])
        self.assertEqual(rc, ad.EXIT_FETCH)
        mock_deploy.assert_not_called()


# ---------------------------------------------------------------------------
# (c) origin advanced → deploy path invoked end-to-end.
# ---------------------------------------------------------------------------


class TestDeployPath(_Base):
    def test_advanced_runs_full_pipeline_and_advances_state(self) -> None:
        self._set_state("old_sha")
        steps = self._patch_pipeline()
        with patch("tools.kino_pwa_autodeploy.fetch_origin_sha", return_value=(True, "new_sha")), \
             patch("tools.kino_pwa_autodeploy.advance_source", return_value=steps["advance_source"]) as m_adv, \
             patch("tools.kino_pwa_autodeploy.build", return_value=steps["build"]) as m_build, \
             patch("tools.kino_pwa_autodeploy.smoke", return_value=steps["smoke"]) as m_smoke, \
             patch("tools.kino_pwa_autodeploy.rsync_to_deploy", return_value=steps["rsync_to_deploy"]) as m_rsync, \
             patch("tools.kino_pwa_autodeploy.restart_kino", return_value=steps["restart_kino"]) as m_restart, \
             patch("tools.kino_pwa_autodeploy.verify_active", return_value=steps["verify_active"]) as m_verify:
            rc = ad.main([])
        self.assertEqual(rc, ad.EXIT_OK)
        # Every step invoked, in order.
        m_adv.assert_called_once()
        m_build.assert_called_once()
        m_smoke.assert_called_once()
        m_rsync.assert_called_once()
        m_restart.assert_called_once()
        m_verify.assert_called_once()
        # State file advanced to the new SHA.
        self.assertEqual(ad._read_last_sha(), "new_sha")

    def test_first_run_no_state_file_deploys(self) -> None:
        # No state file at all → treated as deploy-needed.
        steps = self._patch_pipeline()
        with patch("tools.kino_pwa_autodeploy.fetch_origin_sha", return_value=(True, "fresh_sha")), \
             patch("tools.kino_pwa_autodeploy.advance_source", return_value=steps["advance_source"]), \
             patch("tools.kino_pwa_autodeploy.build", return_value=steps["build"]), \
             patch("tools.kino_pwa_autodeploy.smoke", return_value=steps["smoke"]), \
             patch("tools.kino_pwa_autodeploy.rsync_to_deploy", return_value=steps["rsync_to_deploy"]), \
             patch("tools.kino_pwa_autodeploy.restart_kino", return_value=steps["restart_kino"]), \
             patch("tools.kino_pwa_autodeploy.verify_active", return_value=steps["verify_active"]):
            rc = ad.main([])
        self.assertEqual(rc, ad.EXIT_OK)
        self.assertEqual(ad._read_last_sha(), "fresh_sha")


# ---------------------------------------------------------------------------
# (d) smoke-build failure → NO rsync, NO restart, state unchanged, non-zero exit.
# ---------------------------------------------------------------------------


class TestSmokeGate(_Base):
    def test_smoke_failure_aborts_before_rsync_and_restart(self) -> None:
        self._set_state("old_sha")
        with patch("tools.kino_pwa_autodeploy.fetch_origin_sha", return_value=(True, "new_sha")), \
             patch("tools.kino_pwa_autodeploy.advance_source", return_value=(True, "")), \
             patch("tools.kino_pwa_autodeploy.build", return_value=(True, "")), \
             patch("tools.kino_pwa_autodeploy.smoke", return_value=(False, "missing sw.js")), \
             patch("tools.kino_pwa_autodeploy.rsync_to_deploy") as m_rsync, \
             patch("tools.kino_pwa_autodeploy.restart_kino") as m_restart, \
             patch("tools.kino_pwa_autodeploy.verify_active") as m_verify:
            rc = ad.main([])
        # Non-zero, specifically the smoke-gate code.
        self.assertEqual(rc, ad.EXIT_SMOKE)
        # The bright line: nothing downstream of the gate ran.
        m_rsync.assert_not_called()
        m_restart.assert_not_called()
        m_verify.assert_not_called()
        # State file UNCHANGED — re-deploy still pending on next fire.
        self.assertEqual(ad._read_last_sha(), "old_sha")

    def test_build_failure_aborts_before_smoke_rsync_restart(self) -> None:
        self._set_state("old_sha")
        with patch("tools.kino_pwa_autodeploy.fetch_origin_sha", return_value=(True, "new_sha")), \
             patch("tools.kino_pwa_autodeploy.advance_source", return_value=(True, "")), \
             patch("tools.kino_pwa_autodeploy.build", return_value=(False, "tsc error")), \
             patch("tools.kino_pwa_autodeploy.smoke") as m_smoke, \
             patch("tools.kino_pwa_autodeploy.rsync_to_deploy") as m_rsync, \
             patch("tools.kino_pwa_autodeploy.restart_kino") as m_restart:
            rc = ad.main([])
        self.assertEqual(rc, ad.EXIT_BUILD)
        m_smoke.assert_not_called()
        m_rsync.assert_not_called()
        m_restart.assert_not_called()
        self.assertEqual(ad._read_last_sha(), "old_sha")

    def test_rsync_failure_does_not_restart_or_advance_state(self) -> None:
        self._set_state("old_sha")
        with patch("tools.kino_pwa_autodeploy.fetch_origin_sha", return_value=(True, "new_sha")), \
             patch("tools.kino_pwa_autodeploy.advance_source", return_value=(True, "")), \
             patch("tools.kino_pwa_autodeploy.build", return_value=(True, "")), \
             patch("tools.kino_pwa_autodeploy.smoke", return_value=(True, "")), \
             patch("tools.kino_pwa_autodeploy.rsync_to_deploy", return_value=(False, "perm denied")), \
             patch("tools.kino_pwa_autodeploy.restart_kino") as m_restart:
            rc = ad.main([])
        self.assertEqual(rc, ad.EXIT_RSYNC)
        m_restart.assert_not_called()
        self.assertEqual(ad._read_last_sha(), "old_sha")

    def test_restart_failure_does_not_advance_state(self) -> None:
        self._set_state("old_sha")
        with patch("tools.kino_pwa_autodeploy.fetch_origin_sha", return_value=(True, "new_sha")), \
             patch("tools.kino_pwa_autodeploy.advance_source", return_value=(True, "")), \
             patch("tools.kino_pwa_autodeploy.build", return_value=(True, "")), \
             patch("tools.kino_pwa_autodeploy.smoke", return_value=(True, "")), \
             patch("tools.kino_pwa_autodeploy.rsync_to_deploy", return_value=(True, "")), \
             patch("tools.kino_pwa_autodeploy.restart_kino", return_value=(False, "restart failed")):
            rc = ad.main([])
        self.assertEqual(rc, ad.EXIT_RESTART)
        self.assertEqual(ad._read_last_sha(), "old_sha")

    def test_isactive_failure_does_not_advance_state(self) -> None:
        self._set_state("old_sha")
        with patch("tools.kino_pwa_autodeploy.fetch_origin_sha", return_value=(True, "new_sha")), \
             patch("tools.kino_pwa_autodeploy.advance_source", return_value=(True, "")), \
             patch("tools.kino_pwa_autodeploy.build", return_value=(True, "")), \
             patch("tools.kino_pwa_autodeploy.smoke", return_value=(True, "")), \
             patch("tools.kino_pwa_autodeploy.rsync_to_deploy", return_value=(True, "")), \
             patch("tools.kino_pwa_autodeploy.restart_kino", return_value=(True, "")), \
             patch("tools.kino_pwa_autodeploy.verify_active", return_value=(False, "timeout")):
            rc = ad.main([])
        self.assertEqual(rc, ad.EXIT_ISACTIVE)
        self.assertEqual(ad._read_last_sha(), "old_sha")


# ---------------------------------------------------------------------------
# (e) idempotency — state gates re-deploy across consecutive fires.
# ---------------------------------------------------------------------------


class TestIdempotency(_Base):
    def test_deploy_then_rerun_is_noop(self) -> None:
        self._set_state("old_sha")
        steps = self._patch_pipeline()
        patches = [
            patch("tools.kino_pwa_autodeploy.fetch_origin_sha", return_value=(True, "new_sha")),
            patch("tools.kino_pwa_autodeploy.advance_source", return_value=steps["advance_source"]),
            patch("tools.kino_pwa_autodeploy.build", return_value=steps["build"]),
            patch("tools.kino_pwa_autodeploy.smoke", return_value=steps["smoke"]),
            patch("tools.kino_pwa_autodeploy.rsync_to_deploy", return_value=steps["rsync_to_deploy"]),
            patch("tools.kino_pwa_autodeploy.restart_kino", return_value=steps["restart_kino"]),
            patch("tools.kino_pwa_autodeploy.verify_active", return_value=steps["verify_active"]),
        ]
        # First fire: deploys, advances state.
        for p in patches:
            p.start()
        try:
            rc1 = ad.main([])
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(rc1, ad.EXIT_OK)
        self.assertEqual(ad._read_last_sha(), "new_sha")

        # Reset lock fd between fires (single process simulating two timer fires).
        if hasattr(ad.main, "_lock_fd"):
            ad.main._lock_fd.close()
            del ad.main._lock_fd

        # Second fire: origin/main unchanged → no deploy.
        with patch("tools.kino_pwa_autodeploy.fetch_origin_sha", return_value=(True, "new_sha")), \
             patch("tools.kino_pwa_autodeploy._deploy") as mock_deploy:
            rc2 = ad.main([])
        self.assertEqual(rc2, ad.EXIT_OK)
        mock_deploy.assert_not_called()

    def test_failed_deploy_leaves_state_so_next_fire_retries(self) -> None:
        self._set_state("old_sha")
        # Smoke fails → state stays "old_sha".
        with patch("tools.kino_pwa_autodeploy.fetch_origin_sha", return_value=(True, "new_sha")), \
             patch("tools.kino_pwa_autodeploy.advance_source", return_value=(True, "")), \
             patch("tools.kino_pwa_autodeploy.build", return_value=(True, "")), \
             patch("tools.kino_pwa_autodeploy.smoke", return_value=(False, "broken")), \
             patch("tools.kino_pwa_autodeploy.rsync_to_deploy"), \
             patch("tools.kino_pwa_autodeploy.restart_kino"):
            rc = ad.main([])
        self.assertEqual(rc, ad.EXIT_SMOKE)
        self.assertEqual(ad._read_last_sha(), "old_sha")

        if hasattr(ad.main, "_lock_fd"):
            ad.main._lock_fd.close()
            del ad.main._lock_fd

        # Next fire still sees a mismatch → attempts deploy again (now succeeds).
        steps = self._patch_pipeline()
        with patch("tools.kino_pwa_autodeploy.fetch_origin_sha", return_value=(True, "new_sha")), \
             patch("tools.kino_pwa_autodeploy.advance_source", return_value=steps["advance_source"]), \
             patch("tools.kino_pwa_autodeploy.build", return_value=steps["build"]), \
             patch("tools.kino_pwa_autodeploy.smoke", return_value=steps["smoke"]), \
             patch("tools.kino_pwa_autodeploy.rsync_to_deploy", return_value=steps["rsync_to_deploy"]), \
             patch("tools.kino_pwa_autodeploy.restart_kino", return_value=steps["restart_kino"]), \
             patch("tools.kino_pwa_autodeploy.verify_active", return_value=steps["verify_active"]):
            rc2 = ad.main([])
        self.assertEqual(rc2, ad.EXIT_OK)
        self.assertEqual(ad._read_last_sha(), "new_sha")


# ---------------------------------------------------------------------------
# Concurrency guard (flock).
# ---------------------------------------------------------------------------


class TestConcurrencyGuard(_Base):
    def test_lock_held_returns_exit_lock(self) -> None:
        # Hold the lock from "another instance".
        lockpath = os.environ["PANTHEON_PWA_AUTODEPLOY_LOCKFILE"]
        holder = open(lockpath, "w")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with patch("tools.kino_pwa_autodeploy.fetch_origin_sha") as mock_fetch:
                rc = ad.main([])
            self.assertEqual(rc, ad.EXIT_LOCK)
            # We bailed before doing any work.
            mock_fetch.assert_not_called()
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()


# ---------------------------------------------------------------------------
# ff-only divergence abort (advance_source) + no destructive flag.
# ---------------------------------------------------------------------------


class TestSourceAdvance(_Base):
    def test_divergence_aborts_with_exit_advance(self) -> None:
        self._set_state("old_sha")
        with patch("tools.kino_pwa_autodeploy.fetch_origin_sha", return_value=(True, "new_sha")), \
             patch("tools.kino_pwa_autodeploy.advance_source",
                   return_value=(False, "non-fast-forward; NOT force-resetting")), \
             patch("tools.kino_pwa_autodeploy.build") as m_build, \
             patch("tools.kino_pwa_autodeploy.rsync_to_deploy") as m_rsync, \
             patch("tools.kino_pwa_autodeploy.restart_kino") as m_restart:
            rc = ad.main([])
        self.assertEqual(rc, ad.EXIT_ADVANCE)
        m_build.assert_not_called()
        m_rsync.assert_not_called()
        m_restart.assert_not_called()
        self.assertEqual(ad._read_last_sha(), "old_sha")

    def test_advance_source_uses_ff_only_no_hard_reset(self) -> None:
        """advance_source must use `pull --ff-only` and never `reset --hard`."""
        captured = {}

        def fake_run(cmd, timeout):
            captured["cmd"] = cmd
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("tools.kino_pwa_autodeploy._run", side_effect=fake_run):
            ok, _ = ad.advance_source()
        self.assertTrue(ok)
        joined = " ".join(captured["cmd"])
        self.assertIn("pull --ff-only", joined)
        self.assertNotIn("reset --hard", joined)
        self.assertNotIn("--delete", joined)


# ---------------------------------------------------------------------------
# Step-shape tests: confirm the sudo-wrap + flag discipline of the real callers.
# These exercise the actual step functions with _run mocked (no system touched).
# ---------------------------------------------------------------------------


class TestStepShapes(_Base):
    def _capture_run(self):
        calls = []

        def fake_run(cmd, timeout):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="active", stderr="")

        return calls, fake_run

    def test_build_sudo_wraps_claude_and_exports_bun(self) -> None:
        calls, fake = self._capture_run()
        with patch("tools.kino_pwa_autodeploy._run", side_effect=fake):
            ad.build()
        cmd = calls[0]
        self.assertEqual(cmd[:4], ["sudo", "-nu", ad.DEPLOY_USER, "bash"])
        inner = cmd[-1]
        self.assertIn(ad.BUN_PATH, inner)
        self.assertIn("bun run build:web", inner)

    def test_smoke_runs_smoke_build(self) -> None:
        calls, fake = self._capture_run()
        with patch("tools.kino_pwa_autodeploy._run", side_effect=fake):
            ad.smoke()
        self.assertIn("bun run smoke:build", calls[0][-1])

    def test_rsync_excludes_and_no_delete(self) -> None:
        calls, fake = self._capture_run()
        with patch("tools.kino_pwa_autodeploy._run", side_effect=fake):
            ad.rsync_to_deploy()
        inner = calls[0][-1]
        self.assertIn("rsync -a", inner)
        self.assertIn("--exclude node_modules", inner)
        self.assertIn("--exclude .git", inner)
        self.assertIn("--exclude web/node_modules", inner)
        self.assertNotIn("--delete", inner)
        self.assertIn(f"{ad.SOURCE_DIR}/ {ad.DEPLOY_DIR}/", inner)

    def test_restart_uses_bare_kino_token(self) -> None:
        calls, fake = self._capture_run()
        with patch("tools.kino_pwa_autodeploy._run", side_effect=fake):
            ad.restart_kino()
        # Exact NOPASSWD token: `systemctl restart kino` (NOT kino.service).
        self.assertEqual(calls[0], ["sudo", "systemctl", "restart", "kino"])

    def test_verify_active_uses_bare_kino_token(self) -> None:
        calls, fake = self._capture_run()
        with patch("tools.kino_pwa_autodeploy._run", side_effect=fake):
            ok, detail = ad.verify_active()
        self.assertTrue(ok)
        self.assertEqual(calls[0], ["sudo", "systemctl", "is-active", "kino"])

    def test_fetch_origin_sha_wraps_git_in_sudo_claude(self) -> None:
        calls = []

        def fake_run(cmd, timeout):
            calls.append(cmd)
            # First call = fetch, second = rev-parse.
            out = "deadbeef" if "rev-parse" in cmd[-1] else ""
            return MagicMock(returncode=0, stdout=out, stderr="")

        with patch("tools.kino_pwa_autodeploy._run", side_effect=fake_run):
            ok, sha = ad.fetch_origin_sha()
        self.assertTrue(ok)
        self.assertEqual(sha, "deadbeef")
        for cmd in calls:
            self.assertEqual(cmd[:3], ["sudo", "-nu", ad.DEPLOY_USER])
            self.assertIn(ad.SOURCE_DIR, cmd[-1])


if __name__ == "__main__":
    unittest.main()
