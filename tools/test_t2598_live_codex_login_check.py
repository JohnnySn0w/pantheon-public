"""Tests for tools.t2598_live_codex_login_check (T-2598).

GATE-ISOLATION: the live gate env var (T2598_LIVE_CODEX_LOGIN_CHECK) is
never set here, mirroring test_t2582_live_telegram_accept.py's own
SEND-ISOLATION posture -- main()'s live branch is never exercised by this
suite. ``run_live_login_check`` itself IS exercised directly, but only
against a local stand-in script (never a real codex binary, never network)
-- same posture as harness_codex_ready.py's own safe stand-in fixture.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from tools.bringup_acceptance.types import HardStop
from tools.t2598_live_codex_login_check import (
    GATE_ENV,
    AcceptanceSkipped,
    require_live_gate,
    run_live_login_check,
)


def _write_stand_in_codex(path: Path, *, logged_in: bool) -> None:
    output = "Logged in using ChatGPT" if logged_in else "Not logged in"
    path.write_text(f"#!/bin/sh\necho '{output}'\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class RequireLiveGateTest(unittest.TestCase):
    def test_skips_when_gate_absent(self) -> None:
        with self.assertRaises(AcceptanceSkipped):
            require_live_gate({})

    def test_skips_when_gate_is_a_falsy_value(self) -> None:
        with self.assertRaises(AcceptanceSkipped):
            require_live_gate({GATE_ENV: "0"})

    def test_passes_for_each_truthy_value(self) -> None:
        for value in ("1", "true", "yes"):
            with self.subTest(value=value):
                require_live_gate({GATE_ENV: value})  # must not raise


class RunLiveLoginCheckTest(unittest.TestCase):
    def test_returns_stdout_on_logged_in_stand_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp) / "cache"
            stand_in = Path(tmp) / "stand-in-codex.sh"
            _write_stand_in_codex(stand_in, logged_in=True)

            stdout = run_live_login_check(
                codex_executable=str(stand_in), cache_root=cache_root
            )
            self.assertIn("Logged in", stdout)

    def test_hard_stops_when_stand_in_reports_not_logged_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp) / "cache"
            stand_in = Path(tmp) / "stand-in-codex.sh"
            _write_stand_in_codex(stand_in, logged_in=False)

            with self.assertRaises(HardStop):
                run_live_login_check(
                    codex_executable=str(stand_in), cache_root=cache_root
                )

    def test_disposable_home_is_removed_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp) / "cache"
            stand_in = Path(tmp) / "stand-in-codex.sh"
            _write_stand_in_codex(stand_in, logged_in=True)

            run_live_login_check(codex_executable=str(stand_in), cache_root=cache_root)
            self.assertEqual(os.listdir(cache_root), [])

    def test_disposable_home_is_removed_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_root = Path(tmp) / "cache"
            stand_in = Path(tmp) / "stand-in-codex.sh"
            _write_stand_in_codex(stand_in, logged_in=False)

            with self.assertRaises(HardStop):
                run_live_login_check(
                    codex_executable=str(stand_in), cache_root=cache_root
                )
            self.assertEqual(os.listdir(cache_root), [])


if __name__ == "__main__":
    unittest.main()
