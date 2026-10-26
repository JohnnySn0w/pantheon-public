"""T-4789: a seat whose runtime controller is active never restarts via seat-relaunch.

context_fill_monitor's bespoke seat-relaunch kills the tmux server and replays a
captured argv outside the controller, ignoring overrides/holds.json. A seat with
an active pantheon-runtime-controller@<persona> unit restarts through the
controller instead (handoff_and_restart.sh --supervisor control-plane: end the
pane, controller re-runs selection). systemctl, tmux and subprocess are mocked.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.context_fill_monitor as cfm  # noqa: E402

BESPOKE_CFG = {
    "sudo_user": "suborch1",
    "bespoke_relaunch": True,
    "restart_bin": "/home/suborch1/pantheon-config/tools/pantheon-seat-relaunch.sh",
    "restart_target_pane": "suborch1-main:0.0",
    "relaunch_session": "suborch1-main",
    "relaunch_window": "harness",
    "relaunch_scope_unit": "pantheon-mason-seat.scope",
    "consume_timer": "pantheon-persona-consume@mason.timer",
    "handoff_file": "/home/suborch1/HANDOFF.md",
}


def _probe_result(stdout: str, rc: int = 0) -> MagicMock:
    return MagicMock(returncode=rc, stdout=stdout, stderr="")


class ControllerStateProbeTests(unittest.TestCase):
    def _state(self, **run_kwargs) -> str:
        with patch.object(cfm, "_current_user", return_value="claude-fallback"), \
             patch.object(cfm.subprocess, "run", **run_kwargs) as run:
            state = cfm._controller_unit_state(BESPOKE_CFG, "mason")
        cmd = run.call_args[0][0]
        self.assertIn("pantheon-runtime-controller@mason.service", cmd[-1])
        self.assertIn("is-active", cmd[-1])
        return state

    def test_active(self) -> None:
        self.assertEqual(self._state(return_value=_probe_result("active\n")), "active")

    def test_inactive_and_dead_are_inactive(self) -> None:
        for word in ("inactive", "dead"):
            self.assertEqual(self._state(return_value=_probe_result(word + "\n", 3)), "inactive")

    def test_failed_controller_is_unknown(self) -> None:
        # A broken controller still means a managed seat: never fall back to seat-relaunch.
        self.assertEqual(self._state(return_value=_probe_result("failed\n", 3)), "unknown")

    def test_timeout_and_garbage_are_unknown(self) -> None:
        self.assertEqual(
            self._state(side_effect=subprocess.TimeoutExpired("systemctl", 1)), "unknown")
        self.assertEqual(self._state(return_value=_probe_result("")), "unknown")
        self.assertEqual(self._state(return_value=_probe_result("weird\n")), "unknown")


class ResolveRestartCfgTests(unittest.TestCase):
    def test_controller_managed_bespoke_routes_through_controller(self) -> None:
        with patch.object(cfm, "_controller_unit_state", return_value="active"):
            cfg = cfm.resolve_restart_cfg(BESPOKE_CFG, "mason")
        self.assertIsNotNone(cfg)
        self.assertFalse(cfg.get("bespoke_relaunch"))
        with patch.object(cfm, "_current_user", return_value="claude-fallback"):
            inner = cfm.build_restart_argv(cfg, "mason")[-1]
        self.assertNotIn("seat-relaunch", inner)
        self.assertIn("/home/suborch1/pantheon-config/tools/handoff_and_restart.sh", inner)
        self.assertIn("--service pantheon-persona-consume@mason.service", inner)
        self.assertIn("--supervisor control-plane", inner)
        self.assertIn("--target-pane suborch1-main:0.0", inner)

    def test_unmanaged_bespoke_keeps_seat_relaunch(self) -> None:
        with patch.object(cfm, "_controller_unit_state", return_value="inactive"):
            cfg = cfm.resolve_restart_cfg(BESPOKE_CFG, "mason")
        self.assertIs(cfg, BESPOKE_CFG)

    def test_unknown_state_refuses(self) -> None:
        with patch.object(cfm, "_controller_unit_state", return_value="unknown"):
            self.assertIsNone(cfm.resolve_restart_cfg(BESPOKE_CFG, "mason"))

    def test_non_bespoke_cfg_is_never_probed(self) -> None:
        std = {"sudo_user": "orchestrator", "restart_service": "x.service"}
        with patch.object(cfm, "_controller_unit_state") as probe:
            self.assertIs(cfm.resolve_restart_cfg(std, "orchestrator"), std)
        probe.assert_not_called()


class FireRestartNeverSeatRelaunchTests(unittest.TestCase):
    def _launched_argvs(self, state: str, fire) -> list[list[str]]:
        argvs: list[list[str]] = []

        def fake_popen(cmd, **_kw):
            argvs.append(cmd)
            proc = MagicMock()
            proc.stdout = iter([])
            proc.wait.return_value = 0
            return proc

        def fake_run(cmd, **_kw):
            argvs.append(cmd)
            return _probe_result("")

        with patch.object(cfm, "_controller_unit_state", return_value=state), \
             patch.object(cfm, "_current_user", return_value="claude-fallback"), \
             patch.object(cfm.subprocess, "Popen", side_effect=fake_popen), \
             patch.object(cfm.subprocess, "run", side_effect=fake_run):
            fire()
        return argvs

    def test_fire_restart_managed_seat_never_invokes_seat_relaunch(self) -> None:
        argvs = self._launched_argvs("active", lambda: cfm.fire_restart(BESPOKE_CFG, "mason"))
        self.assertEqual(len(argvs), 1)
        self.assertNotIn("seat-relaunch", argvs[0][-1])
        self.assertIn("handoff_and_restart.sh", argvs[0][-1])

    def test_fire_restart_unmanaged_seat_still_invokes_seat_relaunch(self) -> None:
        argvs = self._launched_argvs("inactive", lambda: cfm.fire_restart(BESPOKE_CFG, "mason"))
        self.assertEqual(len(argvs), 1)
        self.assertIn("pantheon-seat-relaunch.sh", argvs[0][-1])

    def test_detached_managed_seat_never_invokes_seat_relaunch(self) -> None:
        argvs = self._launched_argvs(
            "active", lambda: cfm.fire_restart_detached(BESPOKE_CFG, "mason"))
        self.assertEqual(len(argvs), 1)
        self.assertNotIn("seat-relaunch", " ".join(argvs[0]))

    def test_detached_unmanaged_seat_still_invokes_seat_relaunch(self) -> None:
        argvs = self._launched_argvs(
            "inactive", lambda: cfm.fire_restart_detached(BESPOKE_CFG, "mason"))
        self.assertEqual(len(argvs), 1)
        self.assertIn("pantheon-seat-relaunch.sh", " ".join(argvs[0]))

    def test_unknown_state_launches_nothing(self) -> None:
        for fire in (
            lambda: cfm.fire_restart(BESPOKE_CFG, "mason"),
            lambda: cfm.fire_restart_detached(BESPOKE_CFG, "mason"),
        ):
            self.assertEqual(self._launched_argvs("unknown", fire), [])
        with patch.object(cfm, "_controller_unit_state", return_value="unknown"):
            ok, _out = cfm.fire_restart(BESPOKE_CFG, "mason")
            self.assertFalse(ok)
            ok, _out, _unit = cfm.fire_restart_detached(BESPOKE_CFG, "mason")
            self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
