"""Tests for provision_context_fill_monitor_dropins.py (T-1214).

Exercise the provision/verify reproducibility tool against a throwaway config
root so a re-provision can no longer silently disarm the monitor legs.
"""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.provision_context_fill_monitor_dropins as prov


class ProvisionVerify(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.total = sum(1 for _ in prov.expected_legs())

    def tearDown(self):
        self._tmp.cleanup()

    def test_provision_creates_all_legs(self):
        written = prov.provision(self.root, daemon_reload=False)
        self.assertEqual(len(written), self.total)
        # Every expected (agent, detector) file exists with its expected env token.
        for agent, name, leg in prov.expected_legs():
            p = prov.dropin_path(self.root, agent, name)
            self.assertTrue(p.exists(), f"{p} should exist")
            self.assertIn(prov.expected_token(leg.envvar), p.read_text())
        self.assertEqual(prov.verify(self.root), [])

    # ----- per-leg agent scope (T-3314) -----

    def test_original_three_legs_unchanged_fleet_wide(self):
        expected = {
            "enable.conf": "PANTHEON_API_HEALTH_ENABLED",
            "staleness.conf": "PANTHEON_STALENESS_ENABLED",
            "model-outage.conf": "PANTHEON_MODEL_OUTAGE_ENABLED",
        }
        for name, envvar in expected.items():
            self.assertEqual(prov.DROPINS[name].envvar, envvar)
            self.assertEqual(prov.DROPINS[name].agents, ("orchestrator", "specialist", "recovery"))
        prov.provision(self.root, daemon_reload=False)
        for agent in ("orchestrator", "specialist", "recovery"):
            for name in expected:
                self.assertTrue(prov.dropin_path(self.root, agent, name).exists())

    def test_every_leg_subset_is_within_agents(self):
        # A subset naming an agent outside AGENTS would be silently never armed.
        for name, leg in prov.DROPINS.items():
            self.assertTrue(leg.agents, f"{name} has an empty agent subset")
            self.assertTrue(set(leg.agents) <= set(prov.AGENTS), name)

    def test_repo_audit_leg_provisioned_only_for_orchestrator(self):
        leg = prov.DROPINS["repo-audit.conf"]
        self.assertEqual(leg.envvar, "PANTHEON_REPO_AUDIT_ENABLED")
        self.assertEqual(leg.agents, ("orchestrator",))
        prov.provision(self.root, daemon_reload=False)
        orchestrator = prov.dropin_path(self.root, "orchestrator", "repo-audit.conf")
        self.assertTrue(orchestrator.exists())
        self.assertIn("Environment=PANTHEON_REPO_AUDIT_ENABLED=1", orchestrator.read_text())
        for agent in ("specialist", "recovery"):
            self.assertFalse(prov.dropin_path(self.root, agent, "repo-audit.conf").exists())
        self.assertEqual(self.total, 3 * 3 + 1)

    def test_verify_flags_missing_repo_audit_for_orchestrator_only(self):
        prov.provision(self.root, daemon_reload=False)
        prov.dropin_path(self.root, "orchestrator", "repo-audit.conf").unlink()
        missing = prov.verify(self.root)
        self.assertEqual(missing, ["orchestrator/repo-audit.conf (PANTHEON_REPO_AUDIT_ENABLED)"])

    def test_verify_does_not_expect_repo_audit_on_specialist_recovery(self):
        prov.provision(self.root, daemon_reload=False)
        # Absent for specialist/recovery is the expected state, so verify stays clean.
        self.assertEqual(prov.verify(self.root), [])
        missing_all = prov.verify(Path(self._tmp.name) / "empty")
        self.assertNotIn("specialist/repo-audit.conf", " ".join(missing_all))
        self.assertNotIn("recovery/repo-audit.conf", " ".join(missing_all))
        self.assertIn("orchestrator/repo-audit.conf", " ".join(missing_all))
        self.assertEqual(len(missing_all), self.total)

    def test_provision_is_idempotent(self):
        prov.provision(self.root, daemon_reload=False)
        second = prov.provision(self.root, daemon_reload=False)
        self.assertEqual(second, [], "second provision should write nothing")

    def test_provision_triggers_daemon_reload_only_when_changed(self):
        with mock.patch.object(prov.subprocess, "run") as mrun:
            prov.provision(self.root, daemon_reload=True)  # fresh -> writes -> reload
            self.assertTrue(mrun.called)
            mrun.reset_mock()
            prov.provision(self.root, daemon_reload=True)  # no-op -> no reload
            mrun.assert_not_called()

    def test_verify_detects_a_disarmed_leg(self):
        prov.provision(self.root, daemon_reload=False)
        # Simulate a re-provision dropping one leg.
        prov.dropin_path(self.root, "orchestrator", "model-outage.conf").unlink()
        missing = prov.verify(self.root)
        self.assertEqual(len(missing), 1)
        self.assertIn("orchestrator/model-outage.conf", missing[0])

    def test_verify_is_content_tolerant(self):
        # A leg with DIFFERENT comments but the right env token still counts armed
        # (the live files carry per-agent history comments).
        agent, name = "recovery", "staleness.conf"
        envvar = prov.DROPINS[name].envvar
        p = prov.dropin_path(self.root, agent, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"[Service]\n# totally different historical comment\n{prov.expected_token(envvar)}\n")
        # The other legs are absent; verify should flag exactly the rest, NOT this one.
        missing = prov.verify(self.root)
        self.assertNotIn(f"{agent}/{name}", " ".join(missing))

    # ----- main() exit codes -----

    def test_main_default_safe_refuses_no_mode(self):
        self.assertEqual(prov.main(["--config-root", str(self.root)]), 64)

    def test_main_verify_nonzero_when_disarmed(self):
        rc = prov.main(["--verify", "--config-root", str(self.root)])  # empty root
        self.assertEqual(rc, 1)

    def test_main_verify_zero_when_armed(self):
        prov.provision(self.root, daemon_reload=False)
        rc = prov.main(["--verify", "--config-root", str(self.root)])
        self.assertEqual(rc, 0)

    def test_main_provision_then_verifies(self):
        rc = prov.main(["--provision", "--no-reload", "--config-root", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertEqual(prov.verify(self.root), [])

    def test_main_counts_respect_leg_subsets(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = prov.main(["--provision", "--no-reload", "--config-root", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertIn(f"{self.total} drop-in(s) (re)created", out.getvalue())
        self.assertIn(f"verified {self.total} enablement leg(s) armed", out.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = prov.main(["--verify", "--config-root", str(self.root / "empty")])
        self.assertEqual(rc, 1)
        self.assertIn(f"{self.total}/{self.total}", err.getvalue())


if __name__ == "__main__":
    unittest.main()
