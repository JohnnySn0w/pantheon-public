"""Tests for tools/headless_billing_canary.py (T-4634). Synthetic results only; no model call."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from tools import headless_billing_canary as canary

USAGE_OK = {"is_error": False, "result": "You are currently using your subscription to power your Claude Code usage\n..."}
USAGE_WEEKLY_CAP = {
    "is_error": False,
    "result": (
        "You are currently using your subscription to power your Claude Code usage\n\n"
        "Current session: 0% used\n"
        "Current week (all models): 100% used · resets Nov 1, 4:59pm (UTC)"
    ),
}
TURN_OK = {"type": "result", "subtype": "success", "is_error": False, "result": "OK", "num_turns": 1}


class TestClassify(unittest.TestCase):
    def test_healthy_subscription_and_turn(self) -> None:
        self.assertEqual(canary.classify(USAGE_OK, TURN_OK)[0], 0)

    def test_credit_balance_refusal_is_a_reroute(self) -> None:
        turn = {"subtype": "success", "is_error": True, "result": "Credit balance is too low", "api_error_status": 400}
        self.assertEqual(canary.classify(USAGE_OK, turn)[0], 1)

    def test_usage_not_on_subscription_is_a_reroute(self) -> None:
        usage = {"is_error": False, "result": "Opus 5 API Usage Billing"}
        self.assertEqual(canary.classify(usage, TURN_OK)[0], 1)

    def test_negated_subscription_readout_is_a_reroute(self) -> None:
        for text in ("You are currently not using your subscription to power your Claude Code usage",
                     "You are not currently using your subscription",
                     "Not using your subscription: API key in use"):
            with self.subTest(text=text):
                self.assertEqual(canary.classify({"is_error": False, "result": text}, TURN_OK)[0], 1)

    def test_other_turn_failure_is_not_called_a_reroute(self) -> None:
        turn = {"subtype": "error_during_execution", "is_error": True, "result": "overloaded"}
        self.assertEqual(canary.classify(USAGE_OK, turn)[0], 2)

    def test_429_at_explicit_subscription_cap_is_neutral(self) -> None:
        turn = {"subtype": "success", "is_error": True, "result": "", "api_error_status": 429}
        self.assertEqual(canary.classify(USAGE_WEEKLY_CAP, turn), (
            4,
            "cap-window: subscription confirmed at 100%; inference billing check deferred",
        ))

    def test_429_without_explicit_cap_still_fails(self) -> None:
        turn = {"subtype": "success", "is_error": True, "result": "", "api_error_status": 429}
        self.assertEqual(canary.classify(USAGE_OK, turn)[0], 2)

    def test_429_at_fable_only_cap_still_fails(self) -> None:
        usage = dict(
            USAGE_OK,
            result=USAGE_OK["result"] + "\nCurrent week (Fable): 100% used",
        )
        turn = {"subtype": "success", "is_error": True, "result": "", "api_error_status": 429}
        self.assertEqual(canary.classify(usage, turn)[0], 2)

    def test_reroute_signature_wins_even_at_cap(self) -> None:
        turn = {
            "subtype": "success",
            "is_error": True,
            "result": "Credit balance is too low",
            "api_error_status": 429,
        }
        self.assertEqual(canary.classify(USAGE_WEEKLY_CAP, turn)[0], 1)

    def test_wrong_answer_fails(self) -> None:
        self.assertEqual(canary.classify(USAGE_OK, dict(TURN_OK, result="Sure! OK"))[0], 2)
        self.assertEqual(canary.classify(USAGE_OK, dict(TURN_OK, result="ok."))[0], 0)


class TestMain(unittest.TestCase):
    def test_cannot_run_exits_3(self) -> None:
        with patch.object(canary, "run_claude", side_effect=FileNotFoundError("no claude")):
            self.assertEqual(canary.main(), 3)

    def test_runs_in_the_safe_plugin_less_shape(self) -> None:
        """The no-second-claude rule exempts only `-p` with `--safe-mode`."""
        seen = []

        def fake_run(argv, **kw):
            seen.append((argv, kw.get("cwd")))
            class R:  # noqa: D401
                returncode, stdout, stderr = 0, '{"result": "OK"}', ""
            return R()

        with patch.object(canary.subprocess, "run", side_effect=fake_run):
            canary.run_claude("Reply with exactly: OK", "--model", "haiku")
        argv, cwd = seen[0]
        self.assertIn("-p", argv)
        self.assertIn("--safe-mode", argv)
        self.assertIn("--no-session-persistence", argv)
        self.assertTrue(cwd.startswith("/tmp/pantheon-canary-"))

    def test_child_does_not_inherit_billing_credentials(self) -> None:
        seen = []

        def fake_run(argv, **kw):
            seen.append(kw.get("env"))
            class R:  # noqa: D401
                returncode, stdout, stderr = 0, '{"result": "OK"}', ""
            return R()

        planted = {k: "x" for k in canary.BILLING_ENV}
        with patch.dict(canary.os.environ, dict(planted, KEEP_ME="1")), \
                patch.object(canary.subprocess, "run", side_effect=fake_run):
            canary.run_claude("/usage")
        env = seen[0]
        self.assertIsNotNone(env)
        for k in canary.BILLING_ENV:
            self.assertNotIn(k, env)
        self.assertEqual(env.get("KEEP_ME"), "1")


class TestSystemdContract(unittest.TestCase):
    def test_neutral_cap_exit_is_success_for_systemd(self) -> None:
        unit = Path("tools/systemd/pantheon-headless-billing-canary.service").read_text()
        self.assertIn("SuccessExitStatus=4", unit)


if __name__ == "__main__":
    unittest.main()
