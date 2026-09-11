"""Tests for tools.harness — manual-override CLI for T-2967 (T-2953 BT4).

Command surface + ledger schema per notes/2026-08-18-t2953-phase1-spec.md
§Manual-override CLI. Every test uses a tempdir `root` (persona state) and a
tempdir `ledger_dir` — nothing here touches the real ~/.local/state/pantheon
tree or the real ledger/. `tools.harness._kill_tmux_session` is always
patched: no test invokes a real tmux binary.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import harness
from tools import harness_state as hs


def _run(argv, *, root, ledger_dir):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), mock.patch("tools.harness._kill_tmux_session", return_value=False):
        code = harness.main(argv, root=root, ledger_dir=ledger_dir)
    return code, buf.getvalue()


def _run_capture_stderr(argv, *, root, ledger_dir, kill_return_value=False):
    """Like _run, but also captures stderr — needed for the error-path tests
    below (harness: error: ... messages go to stderr, not stdout)."""
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with (
        contextlib.redirect_stdout(out_buf),
        contextlib.redirect_stderr(err_buf),
        mock.patch("tools.harness._kill_tmux_session", return_value=kill_return_value),
    ):
        code = harness.main(argv, root=root, ledger_dir=ledger_dir)
    return code, out_buf.getvalue(), err_buf.getvalue()


def _read_ledger_entries(ledger_dir: Path) -> list[dict]:
    entries: list[dict] = []
    for path in sorted(ledger_dir.rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
    return entries


class HarnessCliTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "state"
        self.ledger_dir = base / "ledger"
        self.root.mkdir()
        self.ledger_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()


class TestBuildParser(HarnessCliTestCase):
    def test_persona_and_command_required(self):
        parser = harness.build_parser()
        with self.assertRaises(harness.HarnessCliError):
            parser.parse_args([])

    def test_unknown_command_rejected(self):
        parser = harness.build_parser()
        with self.assertRaises(harness.HarnessCliError):
            parser.parse_args(["orchestrator", "not-a-command"])

    def test_dry_run_before_subcommand(self):
        parser = harness.build_parser()
        args = parser.parse_args(["--dry-run", "orchestrator", "status"])
        self.assertTrue(args.dry_run)

    def test_dry_run_after_subcommand(self):
        parser = harness.build_parser()
        args = parser.parse_args(["orchestrator", "restart", "--dry-run", "--reason", "x"])
        self.assertTrue(args.dry_run)

    def test_operator_override_parses(self):
        parser = harness.build_parser()
        args = parser.parse_args(["--operator", "operator", "orchestrator", "status"])
        self.assertEqual(args.operator, "operator")


class TestReasonEnforcement(HarnessCliTestCase):
    def test_status_does_not_require_reason(self):
        code, _out = _run(["orchestrator", "status"], root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 0)

    def test_restart_missing_reason_refused(self):
        code, out = _run(["orchestrator", "restart"], root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 2)
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])

    def test_clear_cooldown_missing_reason_refused(self):
        code, _out = _run(["orchestrator", "clear-cooldown", "--all"], root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 2)
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])

    def test_hold_missing_reason_refused(self):
        code, _out = _run(["orchestrator", "hold", "codex/interactive-chatgpt"], root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 2)

    def test_clear_hold_missing_reason_refused(self):
        code, _out = _run(["orchestrator", "clear-hold", "codex/interactive-chatgpt"], root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 2)


class TestArgShapeValidation(HarnessCliTestCase):
    def test_clear_cooldown_requires_exactly_one_of_runtime_or_all(self):
        code, _out = _run(
            ["orchestrator", "clear-cooldown", "codex/interactive-chatgpt", "--all", "--reason", "x"],
            root=self.root, ledger_dir=self.ledger_dir,
        )
        self.assertEqual(code, 2)

    def test_clear_cooldown_requires_at_least_one(self):
        code, _out = _run(["orchestrator", "clear-cooldown", "--reason", "x"], root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 2)

    def test_restart_clear_cooldown_requires_runtime(self):
        code, _out = _run(
            ["orchestrator", "restart", "--clear-cooldown", "--reason", "x"],
            root=self.root, ledger_dir=self.ledger_dir,
        )
        self.assertEqual(code, 2)

    def test_invalid_persona_rejected(self):
        code, _out = _run(["Orchestrator", "status"], root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 2)

    def test_invalid_runtime_rejected(self):
        code, _out = _run(
            ["orchestrator", "hold", "not-a-runtime", "--reason", "x"],
            root=self.root, ledger_dir=self.ledger_dir,
        )
        self.assertEqual(code, 2)


class TestStatus(HarnessCliTestCase):
    def test_status_on_fresh_persona(self):
        code, out = _run(["orchestrator", "status"], root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 0)
        self.assertIn("orchestrator", out)
        self.assertIn("none recorded", out)

    def test_status_reports_holds_and_records(self):
        hs.write_last_attempt(
            "orchestrator",
            "codex/interactive-chatgpt",
            {
                "schema": hs.SCHEMA_LAST_ATTEMPT,
                "runtime": "codex/interactive-chatgpt",
                "outcome": "failed",
                "failure_class": "subscription_cap",
                "cooldown_until": "2099-01-01T00:00:00Z",
                "consecutive_failures": 1,
                "provenance": {"trigger": "controller-selection", "operator": None},
            },
            root=self.root,
        )
        hs.write_holds(
            "orchestrator",
            {"schema": hs.SCHEMA_HOLDS, "holds": {"claude/anthropic-code": {"set_ts": "t", "set_by": "operator:operator", "reason": "known broken"}}},
            root=self.root,
        )
        code, out = _run(["orchestrator", "status"], root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 0)
        self.assertIn("codex/interactive-chatgpt", out)
        self.assertIn("claude/anthropic-code", out)
        self.assertIn("known broken", out)

    def test_status_never_writes_ledger(self):
        _run(["orchestrator", "status"], root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])


class TestDryRunNeverMutates(HarnessCliTestCase):
    def _state_snapshot(self) -> set[str]:
        if not self.root.is_dir():
            return set()
        return {str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file()}

    def test_restart_dry_run_writes_nothing(self):
        before = self._state_snapshot()
        code, out = _run(
            ["orchestrator", "restart", "--dry-run", "--reason", "test"],
            root=self.root, ledger_dir=self.ledger_dir,
        )
        self.assertEqual(code, 0)
        self.assertEqual(self._state_snapshot(), before)
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])
        self.assertIn("dry-run", out)

    def test_clear_cooldown_dry_run_writes_nothing(self):
        hs.write_last_attempt(
            "orchestrator", "codex/interactive-chatgpt",
            {"schema": hs.SCHEMA_LAST_ATTEMPT, "runtime": "codex/interactive-chatgpt", "cooldown_until": "2099-01-01T00:00:00Z", "consecutive_failures": 2, "provenance": {}},
            root=self.root,
        )
        before_content = hs.load_last_attempt("orchestrator", "codex/interactive-chatgpt", root=self.root)
        code, _out = _run(
            ["orchestrator", "clear-cooldown", "codex/interactive-chatgpt", "--dry-run", "--reason", "test"],
            root=self.root, ledger_dir=self.ledger_dir,
        )
        self.assertEqual(code, 0)
        after_content = hs.load_last_attempt("orchestrator", "codex/interactive-chatgpt", root=self.root)
        self.assertEqual(before_content, after_content)
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])

    def test_hold_dry_run_writes_nothing(self):
        code, _out = _run(
            ["orchestrator", "hold", "codex/interactive-chatgpt", "--dry-run", "--reason", "test"],
            root=self.root, ledger_dir=self.ledger_dir,
        )
        self.assertEqual(code, 0)
        self.assertEqual(hs.load_holds("orchestrator", root=self.root), {"schema": hs.SCHEMA_HOLDS, "holds": {}})
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])

    def test_clear_hold_dry_run_writes_nothing(self):
        hs.write_holds(
            "orchestrator",
            {"schema": hs.SCHEMA_HOLDS, "holds": {"codex/interactive-chatgpt": {"set_ts": "t", "set_by": "operator:x", "reason": "r"}}},
            root=self.root,
        )
        code, _out = _run(
            ["orchestrator", "clear-hold", "codex/interactive-chatgpt", "--dry-run", "--reason", "test"],
            root=self.root, ledger_dir=self.ledger_dir,
        )
        self.assertEqual(code, 0)
        self.assertIn("codex/interactive-chatgpt", hs.load_holds("orchestrator", root=self.root)["holds"])
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])


class TestLedgerEntryShape(HarnessCliTestCase):
    def test_hold_lands_exact_schema_ledger_entry(self):
        code, _out = _run(
            ["--operator", "operator", "orchestrator", "hold", "codex/interactive-chatgpt", "--reason", "known broken"],
            root=self.root, ledger_dir=self.ledger_dir,
        )
        self.assertEqual(code, 0)
        entries = _read_ledger_entries(self.ledger_dir)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(set(entry), {"id", "ts", "agent", "action", "summary", "links"})
        self.assertEqual(entry["agent"], "operator:operator")
        self.assertEqual(entry["action"], "harness_override")
        self.assertIn("known broken", entry["summary"])
        self.assertIn("persona:orchestrator", entry["links"])
        self.assertIn("runtime:codex/interactive-chatgpt", entry["links"])
        self.assertTrue(entry["id"].startswith(entry["ts"]))
        self.assertIn("harness-orchestrator-override-hold", entry["id"])

    def test_hold_actually_mutates_holds_file(self):
        import getpass

        _run(
            ["orchestrator", "hold", "codex/interactive-chatgpt", "--reason", "x"],
            root=self.root, ledger_dir=self.ledger_dir,
        )
        doc = hs.load_holds("orchestrator", root=self.root)
        self.assertIn("codex/interactive-chatgpt", doc["holds"])
        self.assertEqual(doc["holds"]["codex/interactive-chatgpt"]["set_by"], f"operator:{getpass.getuser()}")

    def test_clear_hold_lands_ledger_entry_and_removes_hold(self):
        hs.write_holds(
            "orchestrator",
            {"schema": hs.SCHEMA_HOLDS, "holds": {"codex/interactive-chatgpt": {"set_ts": "t", "set_by": "operator:x", "reason": "r"}}},
            root=self.root,
        )
        code, _out = _run(
            ["orchestrator", "clear-hold", "codex/interactive-chatgpt", "--reason", "resolved"],
            root=self.root, ledger_dir=self.ledger_dir,
        )
        self.assertEqual(code, 0)
        self.assertNotIn("codex/interactive-chatgpt", hs.load_holds("orchestrator", root=self.root)["holds"])
        entries = _read_ledger_entries(self.ledger_dir)
        self.assertEqual(len(entries), 1)
        self.assertIn("resolved", entries[0]["summary"])

    def test_clear_cooldown_all_lands_one_entry_no_runtime_link(self):
        hs.write_last_attempt(
            "orchestrator", "claude/anthropic-code",
            {"schema": hs.SCHEMA_LAST_ATTEMPT, "runtime": "claude/anthropic-code", "cooldown_until": "2099-01-01T00:00:00Z", "consecutive_failures": 1, "provenance": {}},
            root=self.root,
        )
        hs.write_last_attempt(
            "orchestrator", "codex/interactive-chatgpt",
            {"schema": hs.SCHEMA_LAST_ATTEMPT, "runtime": "codex/interactive-chatgpt", "cooldown_until": "2099-01-01T00:00:00Z", "consecutive_failures": 2, "provenance": {}},
            root=self.root,
        )
        code, _out = _run(
            ["orchestrator", "clear-cooldown", "--all", "--reason", "extra tokens bought"],
            root=self.root, ledger_dir=self.ledger_dir,
        )
        self.assertEqual(code, 0)
        entries = _read_ledger_entries(self.ledger_dir)
        self.assertEqual(len(entries), 1)
        self.assertNotIn("runtime:", str(entries[0]["links"]))
        self.assertIsNone(hs.load_last_attempt("orchestrator", "claude/anthropic-code", root=self.root)["cooldown_until"])
        self.assertIsNone(hs.load_last_attempt("orchestrator", "codex/interactive-chatgpt", root=self.root)["cooldown_until"])

    def test_restart_without_runtime_only_kills_tmux(self):
        hs.write_last_attempt(
            "orchestrator", "claude/anthropic-code",
            {"schema": hs.SCHEMA_LAST_ATTEMPT, "runtime": "claude/anthropic-code", "cooldown_until": "2099-01-01T00:00:00Z", "consecutive_failures": 1, "provenance": {}},
            root=self.root,
        )
        code, _out = _run(
            ["orchestrator", "restart", "--reason", "kick it back up"],
            root=self.root, ledger_dir=self.ledger_dir,
        )
        self.assertEqual(code, 0)
        # cooldown untouched — restart alone doesn't clear anything
        self.assertEqual(
            hs.load_last_attempt("orchestrator", "claude/anthropic-code", root=self.root)["cooldown_until"],
            "2099-01-01T00:00:00Z",
        )
        entries = _read_ledger_entries(self.ledger_dir)
        self.assertEqual(len(entries), 1)
        self.assertNotIn("runtime:", str(entries[0]["links"]))

    def test_restart_with_runtime_and_clear_cooldown(self):
        hs.write_last_attempt(
            "orchestrator", "claude/anthropic-code",
            {"schema": hs.SCHEMA_LAST_ATTEMPT, "runtime": "claude/anthropic-code", "cooldown_until": "2099-01-01T00:00:00Z", "consecutive_failures": 1, "provenance": {}},
            root=self.root,
        )
        code, out = _run(
            ["orchestrator", "restart", "--runtime", "claude/anthropic-code", "--clear-cooldown", "--reason", "bought extra tokens"],
            root=self.root, ledger_dir=self.ledger_dir,
        )
        self.assertEqual(code, 0)
        self.assertIsNone(hs.load_last_attempt("orchestrator", "claude/anthropic-code", root=self.root)["cooldown_until"])
        entries = _read_ledger_entries(self.ledger_dir)
        self.assertIn("runtime:claude/anthropic-code", entries[0]["links"])
        self.assertIn("does not force selection", entries[0]["summary"] + out)

    def test_restart_with_runtime_no_clear_cooldown_leaves_cooldown(self):
        hs.write_last_attempt(
            "orchestrator", "codex/interactive-chatgpt",
            {"schema": hs.SCHEMA_LAST_ATTEMPT, "runtime": "codex/interactive-chatgpt", "cooldown_until": "2099-01-01T00:00:00Z", "consecutive_failures": 1, "provenance": {}},
            root=self.root,
        )
        code, _out = _run(
            ["orchestrator", "restart", "--runtime", "codex/interactive-chatgpt", "--reason", "try codex anyway"],
            root=self.root, ledger_dir=self.ledger_dir,
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            hs.load_last_attempt("orchestrator", "codex/interactive-chatgpt", root=self.root)["cooldown_until"],
            "2099-01-01T00:00:00Z",
        )


class TestOperatorResolution(HarnessCliTestCase):
    def test_default_operator_is_current_unix_user(self):
        import getpass

        _run(["orchestrator", "hold", "codex/interactive-chatgpt", "--reason", "x"], root=self.root, ledger_dir=self.ledger_dir)
        entries = _read_ledger_entries(self.ledger_dir)
        self.assertEqual(entries[0]["agent"], f"operator:{getpass.getuser()}")

    def test_operator_flag_overrides(self):
        _run(
            ["--operator", "recovery", "orchestrator", "hold", "codex/interactive-chatgpt", "--reason", "x"],
            root=self.root, ledger_dir=self.ledger_dir,
        )
        entries = _read_ledger_entries(self.ledger_dir)
        self.assertEqual(entries[0]["agent"], "operator:recovery")


class TestTmuxKillWiring(HarnessCliTestCase):
    def test_restart_calls_kill_when_active_runtime_cache_present(self):
        active_path = hs.active_runtime_path("orchestrator", root=self.root)
        active_path.parent.mkdir(parents=True)
        active_path.write_text(
            json.dumps({"schema": hs.SCHEMA_ACTIVE_RUNTIME, "runtime": "claude/anthropic-code", "tmux_session": "claude-telegram-bot"}),
            encoding="utf-8",
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch("tools.harness._kill_tmux_session", return_value=True) as killed:
            code = harness.main(["orchestrator", "restart", "--reason", "x"], root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 0)
        killed.assert_called_once_with("claude-telegram-bot", dry_run=False)

    def test_restart_skips_kill_when_no_active_runtime_cache(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), mock.patch("tools.harness._kill_tmux_session") as killed:
            code = harness.main(["orchestrator", "restart", "--reason", "x"], root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 0)
        killed.assert_not_called()


class TestLedgerIdCollisionRefusal(HarnessCliTestCase):
    """Reviewer REBOUND regression coverage (T-2967, agent/T-2967 3608995b8b
    -> fix): the ledger id is second-granularity and doesn't include the
    runtime, so two same-second mutating invocations for the same
    persona+verb collide. Before the fix, the handler (state write /
    tmux-kill) ran BEFORE the id-uniqueness check, so a collision meant the
    second override took effect live with zero ledger record — the exact
    "silent bypass MUST be impossible" outcome the ticket forbids. The fix
    moves the collision check before handler dispatch."""

    def test_second_same_second_hold_refused_before_any_state_mutation(self):
        with mock.patch("tools.harness._utc_now_ts", return_value="2026-08-18T12:00:00Z"):
            code1, _out1, _err1 = _run_capture_stderr(
                ["orchestrator", "hold", "claude/anthropic-code", "--reason", "first"],
                root=self.root, ledger_dir=self.ledger_dir,
            )
            code2, _out2, err2 = _run_capture_stderr(
                ["orchestrator", "hold", "codex/interactive-chatgpt", "--reason", "second"],
                root=self.root, ledger_dir=self.ledger_dir,
            )
        self.assertEqual(code1, 0)
        self.assertEqual(code2, 2)
        self.assertIn("already landed", err2)

        entries = _read_ledger_entries(self.ledger_dir)
        self.assertEqual(len(entries), 1)

        holds = hs.load_holds("orchestrator", root=self.root)["holds"]
        self.assertIn("claude/anthropic-code", holds)
        # This is the load-bearing assertion: the SECOND (refused) call's
        # hold must be entirely absent from state — proving the collision
        # check ran BEFORE the handler, not that a mutation happened and was
        # merely left unaudited.
        self.assertNotIn("codex/interactive-chatgpt", holds)

    def test_second_same_second_restart_does_not_kill_tmux(self):
        active_path = hs.active_runtime_path("orchestrator", root=self.root)
        active_path.parent.mkdir(parents=True)
        active_path.write_text(
            json.dumps(
                {
                    "schema": hs.SCHEMA_ACTIVE_RUNTIME,
                    "runtime": "claude/anthropic-code",
                    "tmux_session": "claude-telegram-bot",
                }
            ),
            encoding="utf-8",
        )
        with mock.patch("tools.harness._utc_now_ts", return_value="2026-08-18T12:00:00Z"), mock.patch(
            "tools.harness._kill_tmux_session", return_value=True
        ) as killed:
            buf1 = io.StringIO()
            with contextlib.redirect_stdout(buf1):
                code1 = harness.main(
                    ["orchestrator", "restart", "--reason", "first"], root=self.root, ledger_dir=self.ledger_dir
                )
            buf2 = io.StringIO()
            err2 = io.StringIO()
            with contextlib.redirect_stdout(buf2), contextlib.redirect_stderr(err2):
                code2 = harness.main(
                    ["orchestrator", "restart", "--reason", "second"], root=self.root, ledger_dir=self.ledger_dir
                )
        self.assertEqual(code1, 0)
        self.assertEqual(code2, 2)
        self.assertIn("already landed", err2.getvalue())
        # The refused second restart must NOT have fired a second tmux-kill —
        # this is the "worse" shape the reviewer flagged (emergency restart
        # already killed tmux before the crash, pre-fix).
        killed.assert_called_once()

    def test_different_verbs_same_second_do_not_collide(self):
        # Sanity check: the id includes action_slug, so hold + clear-hold on
        # the same persona in the same second are DIFFERENT ids and both
        # succeed — the fix must not over-refuse.
        with mock.patch("tools.harness._utc_now_ts", return_value="2026-08-18T12:00:00Z"):
            code1, _out1, _err1 = _run_capture_stderr(
                ["orchestrator", "hold", "claude/anthropic-code", "--reason", "x"],
                root=self.root, ledger_dir=self.ledger_dir,
            )
            code2, _out2, err2 = _run_capture_stderr(
                ["orchestrator", "clear-hold", "claude/anthropic-code", "--reason", "y"],
                root=self.root, ledger_dir=self.ledger_dir,
            )
        self.assertEqual(code1, 0)
        self.assertEqual(code2, 0)
        self.assertEqual(err2, "")
        self.assertEqual(len(_read_ledger_entries(self.ledger_dir)), 2)

    def test_dry_run_never_triggers_collision_check(self):
        # Two dry-run calls in the same second must never refuse — nothing is
        # ever appended on a dry-run, so there is nothing to collide with.
        with mock.patch("tools.harness._utc_now_ts", return_value="2026-08-18T12:00:00Z"):
            code1, _out1, err1 = _run_capture_stderr(
                ["orchestrator", "hold", "claude/anthropic-code", "--dry-run", "--reason", "x"],
                root=self.root, ledger_dir=self.ledger_dir,
            )
            code2, _out2, err2 = _run_capture_stderr(
                ["orchestrator", "hold", "claude/anthropic-code", "--dry-run", "--reason", "y"],
                root=self.root, ledger_dir=self.ledger_dir,
            )
        self.assertEqual(code1, 0)
        self.assertEqual(code2, 0)
        self.assertEqual(err1, "")
        self.assertEqual(err2, "")
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])


class TestLedgerAppendFailureHandling(HarnessCliTestCase):
    """Reviewer item 3/4: unexpected append_entry failures must print a
    clean `harness: error: ...` message, never a raw traceback."""

    def test_unexpected_append_entry_failure_is_clean_not_a_traceback(self):
        with mock.patch("tools.harness.append_entry", side_effect=OSError("disk full")):
            code, _out, err = _run_capture_stderr(
                ["orchestrator", "hold", "claude/anthropic-code", "--reason", "x"],
                root=self.root, ledger_dir=self.ledger_dir,
            )
        self.assertEqual(code, 2)
        self.assertIn("harness: error:", err)
        self.assertIn("failed to append ledger entry", err)

    def test_unexpected_collision_precheck_failure_is_clean_not_a_traceback(self):
        with mock.patch("tools.harness.existing_entry_ids", side_effect=OSError("permission denied")):
            code, _out, err = _run_capture_stderr(
                ["orchestrator", "hold", "claude/anthropic-code", "--reason", "x"],
                root=self.root, ledger_dir=self.ledger_dir,
            )
        self.assertEqual(code, 2)
        self.assertIn("harness: error:", err)
        # And critically: the handler must not have run either, since the
        # precheck failure happens before dispatch.
        self.assertEqual(hs.load_holds("orchestrator", root=self.root)["holds"], {})


if __name__ == "__main__":
    unittest.main()
