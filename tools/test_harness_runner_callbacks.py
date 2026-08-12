"""Focused evidence-boundary tests for :mod:`tools.harness_adapter.runner`."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from tools.harness_adapter import CodexHarnessAdapter, ExecutionRequest, HarnessRunner


class _Process:
    def __init__(self) -> None:
        self.stdin = mock.Mock()
        self.stdout = iter(
            (
                '{"type":"thread.started","thread_id":"thread-1"}\n',
                '{"type":"turn.started","thread_id":"thread-1"}\n',
            )
        )
        self.stderr = mock.Mock()
        self.stderr.readlines.return_value = []
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self) -> int:
        return -15 if self.terminated else 0


class RunnerCallbackBoundaryTests(unittest.TestCase):
    def test_callback_failure_terminates_run_and_returns_typed_local_failure(self) -> None:
        process = _Process()
        callback = mock.Mock(side_effect=RuntimeError("receipt store unavailable"))
        adapter = CodexHarnessAdapter("recovery")

        with mock.patch(
            "tools.harness_adapter.runner.subprocess.Popen",
            return_value=process,
        ):
            result = HarnessRunner().run(
                adapter,
                ExecutionRequest(prompt="task", cwd=Path("/tmp")),
                on_event=callback,
            )

        self.assertTrue(process.terminated)
        self.assertEqual(callback.call_count, 1)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error.category if result.error else None, "config")
        self.assertTrue(result.error.retryable if result.error else False)
        self.assertIn(
            "receipt store unavailable",
            result.error.message if result.error else "",
        )


if __name__ == "__main__":
    unittest.main()
