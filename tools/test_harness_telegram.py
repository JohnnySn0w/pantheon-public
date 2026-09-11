"""Tests for tools.harness_telegram — /harness command surface for T-2967 (BT4).

Per notes/2026-08-18-t2953-phase1-spec.md §Telegram bot wiring: mirrors the
CLI's argument shape, gated to the operator allowlist (channels/telegram/
access.json "or equivalent" — here an injectable path, never the real file),
non-allowlisted senders refused, missing --reason prompts rather than
silently failing. Never touches the real access.json or a live Telegram bot.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools import harness_telegram as ht


OPERATOR = 456474197
STRANGER = 999999999


class TestPrefixRecognition(unittest.TestCase):
    def test_non_harness_message_returns_none(self):
        result = ht.parse_harness_message("hey what's up", OPERATOR, allow_from={OPERATOR})
        self.assertIsNone(result)

    def test_partial_prefix_match_not_recognized(self):
        result = ht.parse_harness_message("/harnessed orchestrator status", OPERATOR, allow_from={OPERATOR})
        self.assertIsNone(result)

    def test_bot_mention_suffix_recognized(self):
        result = ht.parse_harness_message("/harness@some_bot orchestrator status", OPERATOR, allow_from={OPERATOR})
        self.assertIsInstance(result, ht.ParsedInvocation)


class TestAuthGate(unittest.TestCase):
    def test_non_allowlisted_sender_refused(self):
        result = ht.parse_harness_message("/harness orchestrator status", STRANGER, allow_from={OPERATOR})
        self.assertIsInstance(result, ht.AuthRefusal)
        self.assertIn("not authorized", result.message.lower())

    def test_auth_checked_before_parsing_garbled_command(self):
        # Even a malformed command from a non-allowlisted sender is a refusal,
        # not a parse error — we don't want to leak command-shape feedback to
        # strangers.
        result = ht.parse_harness_message("/harness this is nonsense !!!", STRANGER, allow_from={OPERATOR})
        self.assertIsInstance(result, ht.AuthRefusal)

    def test_allowlisted_sender_proceeds_to_parse(self):
        result = ht.parse_harness_message("/harness orchestrator status", OPERATOR, allow_from={OPERATOR})
        self.assertIsInstance(result, ht.ParsedInvocation)

    def test_empty_allow_from_refuses_everyone(self):
        result = ht.parse_harness_message("/harness orchestrator status", OPERATOR, allow_from=frozenset())
        self.assertIsInstance(result, ht.AuthRefusal)


class TestReasonPrompt(unittest.TestCase):
    def test_missing_reason_on_mutating_verb_prompts(self):
        result = ht.parse_harness_message("/harness orchestrator restart", OPERATOR, allow_from={OPERATOR})
        self.assertIsInstance(result, ht.ReasonPrompt)
        self.assertEqual(result.command, "restart")
        self.assertIn("reason", result.message.lower())

    def test_status_never_prompts_for_reason(self):
        result = ht.parse_harness_message("/harness orchestrator status", OPERATOR, allow_from={OPERATOR})
        self.assertIsInstance(result, ht.ParsedInvocation)

    def test_hold_with_quoted_reason_parses_clean(self):
        result = ht.parse_harness_message(
            '/harness orchestrator hold codex/interactive-chatgpt --reason "known broken; awaiting fix"',
            OPERATOR,
            allow_from={OPERATOR},
        )
        self.assertIsInstance(result, ht.ParsedInvocation)
        self.assertEqual(result.args.reason, "known broken; awaiting fix")
        self.assertEqual(result.args.command, "hold")
        self.assertEqual(result.args.runtime, "codex/interactive-chatgpt")


class TestParseErrors(unittest.TestCase):
    def test_unknown_subcommand_is_parse_error(self):
        result = ht.parse_harness_message("/harness orchestrator not-a-verb", OPERATOR, allow_from={OPERATOR})
        self.assertIsInstance(result, ht.ParseError)

    def test_invalid_persona_is_parse_error(self):
        result = ht.parse_harness_message("/harness Orchestrator status", OPERATOR, allow_from={OPERATOR})
        self.assertIsInstance(result, ht.ParseError)

    def test_empty_remainder_is_parse_error(self):
        result = ht.parse_harness_message("/harness", OPERATOR, allow_from={OPERATOR})
        self.assertIsInstance(result, ht.ParseError)

    def test_unclosed_quote_is_parse_error_not_crash(self):
        result = ht.parse_harness_message('/harness orchestrator hold x/y --reason "unterminated', OPERATOR, allow_from={OPERATOR})
        self.assertIsInstance(result, ht.ParseError)


class TestParsedInvocationShape(unittest.TestCase):
    def test_argv_round_trips_through_cli_parser(self):
        from tools import harness

        result = ht.parse_harness_message(
            "/harness orchestrator clear-cooldown --all --reason \"extra tokens bought\"",
            OPERATOR,
            allow_from={OPERATOR},
        )
        self.assertIsInstance(result, ht.ParsedInvocation)
        cli_parser = harness.build_parser()
        cli_args = cli_parser.parse_args(result.argv)
        self.assertEqual(vars(cli_args), vars(result.args))


class TestLoadAllowFrom(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_returns_empty(self):
        self.assertEqual(ht.load_allow_from(self.base / "nope.json"), frozenset())

    def test_valid_file_returns_int_set(self):
        path = self.base / "access.json"
        path.write_text(json.dumps({"dmPolicy": "allowlist", "allowFrom": ["456474197"]}), encoding="utf-8")
        self.assertEqual(ht.load_allow_from(path), frozenset({456474197}))

    def test_malformed_json_returns_empty_not_crash(self):
        path = self.base / "access.json"
        path.write_text("{not valid", encoding="utf-8")
        self.assertEqual(ht.load_allow_from(path), frozenset())

    def test_non_integer_entries_skipped(self):
        path = self.base / "access.json"
        path.write_text(json.dumps({"allowFrom": ["abc", "123"]}), encoding="utf-8")
        self.assertEqual(ht.load_allow_from(path), frozenset({123}))


class TestExecuteParsedInvocation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "state"
        self.ledger_dir = base / "ledger"
        self.root.mkdir()
        self.ledger_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_execute_runs_the_cli_and_appends_ledger(self):
        result = ht.parse_harness_message(
            "/harness orchestrator hold codex/interactive-chatgpt --reason x",
            OPERATOR,
            allow_from={OPERATOR},
        )
        self.assertIsInstance(result, ht.ParsedInvocation)
        code = ht.execute_parsed_invocation(result, root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 0)
        entries = list(self.ledger_dir.rglob("*.jsonl"))
        self.assertEqual(len(entries), 1)


if __name__ == "__main__":
    unittest.main()
