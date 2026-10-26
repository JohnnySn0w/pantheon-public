"""Tests for tools.handoff_first (T-5069): ask a live session for a fresh
HANDOFF before a swap kills it. No real tmux or sleep is ever used."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import handoff_first as hf


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class RequestHandoffTests(unittest.TestCase):
    def test_confirmed_when_mtime_moves_after_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "HANDOFF.md"
            path.write_text("old")
            os.utime(path, (1_000_000, 1_000_000))
            prompts: list[str] = []

            def submit(prompt: str) -> None:
                prompts.append(prompt)
                path.write_text("new")  # the session answers the prompt

            clock = _Clock()
            out = hf.request_handoff(path, submit, sleep=clock.sleep, monotonic=clock.monotonic)
            self.assertTrue(out.confirmed)
            self.assertIn(str(path), prompts[0])

    def test_missing_file_created_counts_as_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "HANDOFF.md"
            clock = _Clock()
            out = hf.request_handoff(
                path, lambda p: path.write_text("x"), sleep=clock.sleep, monotonic=clock.monotonic
            )
            self.assertTrue(out.confirmed)

    def test_times_out_when_mtime_never_moves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "HANDOFF.md"
            path.write_text("old")
            clock = _Clock()
            out = hf.request_handoff(
                path, lambda p: None, timeout_seconds=30, poll_interval_seconds=2,
                sleep=clock.sleep, monotonic=clock.monotonic,
            )
            self.assertFalse(out.confirmed)
            self.assertIn("no handoff ack within 30s", out.reason)
            self.assertGreaterEqual(clock.t, 30)

    def test_delivery_failure_is_unconfirmed_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "HANDOFF.md"

            def submit(prompt: str) -> None:
                raise RuntimeError("dialog focused")

            out = hf.request_handoff(path, submit)
            self.assertFalse(out.confirmed)
            self.assertIn("not delivered", out.reason)
            self.assertIn("dialog focused", out.reason)


class HandoffNewerThanTests(unittest.TestCase):
    def test_compares_mtime_to_session_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "HANDOFF.md"
            self.assertFalse(hf.handoff_newer_than(path, datetime(2026, 1, 1, tzinfo=timezone.utc)))
            path.write_text("x")
            os.utime(path, (1_800_000_000, 1_800_000_000))
            before = datetime.fromtimestamp(1_799_999_000, tz=timezone.utc)
            after = datetime.fromtimestamp(1_800_000_500, tz=timezone.utc)
            self.assertTrue(hf.handoff_newer_than(path, before))
            self.assertFalse(hf.handoff_newer_than(path, after))


class AutoSwapGateTests(unittest.TestCase):
    def _gate(self, outcome_confirmed: bool, handoff_mtime: float, started_ts: float) -> tuple[bool, list]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "HANDOFF.md"
            path.write_text("x")
            os.utime(path, (handoff_mtime, handoff_mtime))
            binding = SimpleNamespace(persona="specialist", account_home=Path(tmp))
            started = datetime.fromtimestamp(started_ts, tz=timezone.utc)
            logs: list = []
            fake = lambda b, **kw: hf.HandoffOutcome(outcome_confirmed, "r", path)  # noqa: E731
            with mock.patch(
                "tools.runtime_controller.recorded_runtime_handle",
                return_value=SimpleNamespace(started_at=started),
            ):
                ok = hf.auto_swap_handoff_gate(
                    binding, request_fn=fake, log_fn=lambda e, p: logs.append((e, p))
                )
            return ok, logs

    def test_confirmed_proceeds(self) -> None:
        ok, logs = self._gate(True, 1_000, 2_000)
        self.assertTrue(ok)
        self.assertEqual(logs, [])

    def test_unconfirmed_but_handoff_newer_than_session_proceeds(self) -> None:
        ok, logs = self._gate(False, 3_000, 2_000)
        self.assertTrue(ok)
        self.assertEqual(logs[0][1]["action"], "proceed")

    def test_unconfirmed_and_handoff_older_than_session_defers(self) -> None:
        ok, logs = self._gate(False, 1_000, 2_000)
        self.assertFalse(ok)
        self.assertEqual(logs[0][0], "handoff_first_unconfirmed")
        self.assertEqual(logs[0][1]["action"], "defer")

    def test_unknown_session_start_defers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "HANDOFF.md"
            path.write_text("x")
            binding = SimpleNamespace(persona="specialist", account_home=Path(tmp))
            with mock.patch(
                "tools.runtime_controller.recorded_runtime_handle", side_effect=RuntimeError("mismatch")
            ):
                ok = hf.auto_swap_handoff_gate(
                    binding, request_fn=lambda b, **kw: hf.HandoffOutcome(False, "r", path)
                )
            self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
