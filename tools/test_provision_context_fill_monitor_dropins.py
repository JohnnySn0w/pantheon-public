"""Tests for provision_context_fill_monitor_dropins.py (T-1214).

Exercise the provision/verify reproducibility tool against a throwaway config
root so a re-provision can no longer silently disarm the monitor legs.
"""
from __future__ import annotations

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
        self.total = len(prov.AGENTS) * len(prov.DROPINS)

    def tearDown(self):
        self._tmp.cleanup()

    def test_provision_creates_all_legs(self):
        written = prov.provision(self.root, daemon_reload=False)
        self.assertEqual(len(written), self.total)
        # Every (agent, detector) file exists with its expected env token.
        for agent in prov.AGENTS:
            for name, (envvar, _note) in prov.DROPINS.items():
                p = prov.dropin_path(self.root, agent, name)
                self.assertTrue(p.exists(), f"{p} should exist")
                self.assertIn(prov.expected_token(envvar), p.read_text())
        self.assertEqual(prov.verify(self.root), [])

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
        envvar = prov.DROPINS[name][0]
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


if __name__ == "__main__":
    unittest.main()
