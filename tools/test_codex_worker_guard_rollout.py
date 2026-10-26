"""Tests for tools/codex_worker_guard_rollout.py (T-4395): the per-home status/apply ops CLI."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tools import codex_worker_guard_rollout as rollout
from tools.test_harness_adapter import FakeCodexHooks


class RolloutCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "seat"
        (self.home / "pantheon-config" / "tools" / "hooks").mkdir(parents=True)
        (self.home / "pantheon-config" / "tools" / "hooks" / "worker-guard.py").write_text("# guard\n")
        self.codex_home = self.home / ".codex"
        self.codex_home.mkdir()
        self.config = self.codex_home / "config.toml"
        legacy = f"/usr/bin/python3 {self.home}/pantheon-config/tools/hooks/worker-guard.py"
        old_hash = FakeCodexHooks.definition_hash(legacy, 10, "Bash|apply_patch")
        self.config.write_text(
            f'[[hooks.PreToolUse]]\nmatcher = "Bash|apply_patch"\n\n[[hooks.PreToolUse.hooks]]\n'
            f'type = "command"\ncommand = "{legacy}"\ntimeout = 10\n\n'
            f'[hooks.state."{self.config}:pre_tool_use:0:0"]\ntrusted_hash = "{old_hash}"\n')
        self.codex = FakeCodexHooks(real_home=self.codex_home)

    def _run(self, action: str):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = rollout.main([action, "--account-home", str(self.home)], read_hooks=self.codex)
        return rc, json.loads(out.getvalue())

    def test_status_is_read_only_and_not_ok_on_a_legacy_home(self) -> None:
        before = self.config.read_bytes()
        rc, report = self._run("status")
        self.assertEqual(rc, rollout.EXIT_NOT_OK)
        self.assertFalse(report["status"]["block_current"])
        self.assertTrue(report["status"]["legacy_present"])
        self.assertEqual(self.config.read_bytes(), before)

    def test_apply_then_status_is_ok(self) -> None:
        rc, report = self._run("apply")
        self.assertEqual((rc, report["changed"], report["status"]["ok"]), (rollout.EXIT_OK, True, True))
        rc, report = self._run("apply")
        self.assertEqual((rc, report["changed"]), (rollout.EXIT_OK, False))
        self.assertEqual(self._run("status")[0], rollout.EXIT_OK)

    def test_apply_never_trusts_the_launch_marker(self) -> None:
        # T-4428: the ops surface always asks codex; only launches may skip via the marker.
        with unittest.mock.patch.object(rollout, "ensure_worker_guard_hook",
                                        wraps=rollout.ensure_worker_guard_hook) as ensure:
            self._run("apply")
        self.assertIs(ensure.call_args.kwargs["trust_marker"], False)

    def test_unmeasurable_when_the_app_server_read_fails(self) -> None:
        self._run("apply")
        self.codex.real_result = None
        rc, report = self._run("status")
        self.assertEqual(rc, rollout.EXIT_UNMEASURABLE)
        self.assertFalse(report["status"]["measurable"])

    def test_unparseable_config_is_unmeasurable(self) -> None:
        self.config.write_text("[broken\n")
        rc, report = self._run("status")
        self.assertEqual(rc, rollout.EXIT_UNMEASURABLE)
        self.assertIn("error", report)


if __name__ == "__main__":
    unittest.main()
