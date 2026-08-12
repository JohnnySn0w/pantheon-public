"""Tests for tools/t2611_recovery_persona_apply.py.

T-2611: the driver folded into tools/t2590_recovery_cutover.sh's install-done
precondition (before the destructive bespoke-disable) and its replacement
step-5 (the real persona_installer activation, after the bespoke session is
confirmed dead). See tickets/inbox/T-2611.md for the root cause this closes.

Uses the same fixture-manifest-in-a-tmpdir shape as test_persona_installer.py
(InstallerCase): a real PersonaInstaller(REPO_ROOT) against the real
tools/systemd/pantheon-persona-consume@.{service,timer} templates, with a
sandboxed `root=` so nothing ever touches this host's real /home. Backends
are always injected (never a live LocalHostBackend) so these tests carry no
dependency on the claude-fallback account, systemd --user, or any other real
host state.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tools.persona_installer import Manifest, SimulatedHostBackend
from tools.t2611_recovery_persona_apply import check_precondition, main, run_apply


REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_PATH = REPO_ROOT / "tools" / "t2611_recovery_persona_apply.py"


def _manifest_dict() -> dict:
    return {
        "manifest_version": 2,
        "persona_installer": {
            "schema": "pantheon-persona-install/v1",
            "framework": {"source": "pantheon-config", "revision": "test-rev"},
            "paths": {
                "repo": "pantheon-config",
                "unit_dir": ".config/systemd/user",
                "state_dir": ".local/state/pantheon/persona-installer",
            },
            "mailbox": {
                "hub": "/mailbox",
                "group": "pantheon-mailbox",
                "probe_peer": "orchestrator",
            },
            "systemd": {
                "service_template": "tools/systemd/pantheon-persona-consume@.service",
                "timer_template": "tools/systemd/pantheon-persona-consume@.timer",
            },
            "policy": {"bundle": ["rulebook/", "sop/", "adr/"]},
            "security": {
                "credential_mode": "references-only",
                "prohibit_auth_copy": True,
            },
            "lifecycle": {
                "require_disabled_first_fire": True,
                "require_mailbox_send_receive": True,
                "require_finite_next_elapse": True,
                "require_observed_timed_fire": True,
            },
            "provenance": {"binding_source": "instance.yaml"},
        },
        "instance": {"id": "test-instance", "harness_default": "claude-code"},
        "roles": {
            "recovery": {
                "persona": "recovery",
                "account": "claude-fallback",
                "harness": "claude-code",
            }
        },
    }


class DriverCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest_path = self.root / "instance.yaml"
        self.write_manifest(_manifest_dict())
        self.sandbox_root = self.root / "projection"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_manifest(self, value: dict) -> None:
        self.manifest_path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")

    def check_precondition(self, **kwargs):
        kwargs.setdefault("root", self.sandbox_root)
        return check_precondition(
            self.manifest_path, "recovery", "claude", REPO_ROOT, **kwargs
        )

    def run_apply(self, **kwargs):
        kwargs.setdefault("root", self.sandbox_root)
        return run_apply(self.manifest_path, "recovery", "claude", REPO_ROOT, **kwargs)


class CheckPreconditionTests(DriverCase):
    @mock.patch("tools.t2611_recovery_persona_apply.shutil.which")
    def test_ok_with_permissive_backend_and_resolvable_harness(self, which) -> None:
        which.return_value = "/home/claude-fallback/.npm-global/bin/claude"
        ok, message = self.check_precondition(backend=SimulatedHostBackend())
        self.assertTrue(ok, message)
        self.assertIn("consume-dry-run preflight clean", message)
        which.assert_called_once_with("claude")

    @mock.patch("tools.t2611_recovery_persona_apply.shutil.which")
    def test_fails_when_harness_missing_on_path(self, which) -> None:
        which.return_value = None
        ok, message = self.check_precondition(backend=SimulatedHostBackend())
        self.assertFalse(ok)
        self.assertIn("not found on PATH", message)
        self.assertIn("T-2611", message)

    @mock.patch("tools.t2611_recovery_persona_apply.shutil.which")
    def test_fails_when_consume_templates_are_not_loadable(self, which) -> None:
        which.return_value = "/usr/bin/claude"
        raw = _manifest_dict()
        raw["persona_installer"]["systemd"]["service_template"] = "tools/systemd/does-not-exist.service"
        self.write_manifest(raw)
        ok, message = self.check_precondition(backend=SimulatedHostBackend())
        self.assertFalse(ok)
        self.assertIn("plan precondition failed", message)

    @mock.patch("tools.t2611_recovery_persona_apply.shutil.which")
    def test_fails_when_backend_preflight_refuses(self, which) -> None:
        which.return_value = "/usr/bin/claude"
        # "policy-check" is a real operation kind in the recovery plan (the policy
        # bundle precondition) -- forcing it unavailable exercises the same
        # capability+backend.preflight loop apply() runs internally, without
        # needing a live claude-fallback account.
        backend = SimulatedHostBackend(unavailable_kinds=["policy-check"])
        ok, message = self.check_precondition(backend=backend)
        self.assertFalse(ok)
        self.assertIn("consume-dry-run preflight failed", message)
        self.assertIn("policy", message)


class RunApplyTests(DriverCase):
    def test_reports_applied_and_verified_against_simulated_backend(self) -> None:
        exit_code, summary = self.run_apply(backend=SimulatedHostBackend())
        self.assertEqual(exit_code, 0, summary)
        self.assertIn("APPLY_STATUS=applied", summary)
        self.assertIn("VERIFY_STATUS=verified", summary)

    def test_surfaces_refused_status_without_raising(self) -> None:
        backend = SimulatedHostBackend(unavailable_kinds=["mkdir"])
        exit_code, summary = self.run_apply(backend=backend)
        self.assertEqual(exit_code, 1)
        self.assertIn("APPLY_STATUS=refused", summary)


class MainDispatchTests(DriverCase):
    def test_precondition_subcommand_dispatches_with_parsed_args(self) -> None:
        with mock.patch(
            "tools.t2611_recovery_persona_apply.check_precondition",
            return_value=(True, "ok"),
        ) as mocked:
            exit_code = main(
                [
                    "precondition",
                    "--manifest",
                    str(self.manifest_path),
                    "--persona",
                    "recovery",
                    "--harness",
                    "claude",
                    "--repo-root",
                    str(REPO_ROOT),
                ]
            )
        self.assertEqual(exit_code, 0)
        mocked.assert_called_once_with(self.manifest_path, "recovery", "claude", REPO_ROOT)

    def test_apply_subcommand_dispatches_with_parsed_args_and_propagates_exit_code(self) -> None:
        with mock.patch(
            "tools.t2611_recovery_persona_apply.run_apply",
            return_value=(1, "APPLY_STATUS=refused"),
        ) as mocked:
            exit_code = main(
                [
                    "apply",
                    "--manifest",
                    str(self.manifest_path),
                    "--persona",
                    "recovery",
                    "--harness",
                    "claude",
                    "--repo-root",
                    str(REPO_ROOT),
                ]
            )
        self.assertEqual(exit_code, 1)
        mocked.assert_called_once_with(self.manifest_path, "recovery", "claude", REPO_ROOT)


class RealInvocationTests(unittest.TestCase):
    """T-2618: t2590_recovery_cutover.sh shells out to this helper as
    `python3 <abs-path-to-helper> ...` with no PYTHONPATH and no cwd
    guarantee (see PERSONA_APPLY_HELPER call sites in t2590_recovery_cutover.sh).
    That means sys.path[0] is the helper's OWN directory (tools/), not the
    repo root, so `from tools.persona_installer import ...` is unresolvable
    at real fire time -- even though every test above imports the module
    in-process, where the repo root is already on sys.path via pytest's
    rootdir insertion, and so never observes this failure. Running the
    helper as an actual subprocess from a cwd outside the repo is the only
    shape that reproduces the real invocation contract.
    """

    def test_help_runs_clean_from_outside_the_repo(self) -> None:
        with tempfile.TemporaryDirectory() as outside_repo:
            result = subprocess.run(
                [sys.executable, str(HELPER_PATH), "--help"],
                cwd=outside_repo,
                capture_output=True,
                text=True,
            )
        self.assertNotIn(
            "ModuleNotFoundError", result.stderr, result.stderr
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
