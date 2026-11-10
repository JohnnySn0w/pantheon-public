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
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools import harness
from tools import harness_state as hs
from tools import ledger_shard


def _freeze_skew_check(testcase):
    """T-4739: ORG-001 is enforced, and its ts rule wants the write-time clock.
    Fixtures that pin `_utc_now_ts` to a fixed day stay valid with the skew limit lifted."""
    patcher = mock.patch.object(ledger_shard, "TS_MAX_SKEW_SECONDS", 10**10)
    patcher.start()
    testcase.addCleanup(patcher.stop)


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


def _without_org001_warnings(stderr: str) -> str:
    """`stderr` minus the T-4734 ORG-001 WARN lines from ledger_shard."""
    return "".join(
        line for line in stderr.splitlines(keepends=True)
        if not line.startswith("ledger: WARN ORG-001 ")
    )


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
        _freeze_skew_check(self)
        self.root = base / "state"
        self.ledger_dir = base / "ledger"
        self.root.mkdir()
        self.ledger_dir.mkdir()
        # Hermetic status: never read the host's real /var/lib/pantheon/oom/holds.json.
        patcher = mock.patch.object(harness, "_OOM_HOLDS_PATH", base / "no-oom-holds.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        # ORG-106 L3a: never touch the real /var/lib/pantheon/oom-hold flag dir.
        self.hold_dir = base / "oom-hold"
        self.hold_dir.mkdir()
        flag_patcher = mock.patch.object(harness, "_OOM_HOLD_DIR", self.hold_dir)
        flag_patcher.start()
        self.addCleanup(flag_patcher.stop)

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
        # T-4737: ORG-001 fields; a human operator writes human:operator.
        self.assertEqual(
            set(entry),
            {"id", "ts", "agent", "action", "rationale", "justifies_via", "summary", "links"},
        )
        self.assertEqual(entry["agent"], "human:operator")
        self.assertIn("operator: operator", entry["rationale"])
        self.assertEqual(entry["justifies_via"], ["P4"])
        self.assertEqual(entry["action"], "harness_override")
        self.assertIn("known broken", entry["summary"])
        self.assertIn({"type": "persona", "ref": "orchestrator"}, entry["links"])
        self.assertIn({"type": "runtime", "ref": "codex/interactive-chatgpt"}, entry["links"])
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
        self.assertNotIn("runtime", [link["type"] for link in entries[0]["links"]])
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
        self.assertNotIn("runtime", [link["type"] for link in entries[0]["links"]])

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
        self.assertIn({"type": "runtime", "ref": "claude/anthropic-code"}, entries[0]["links"])
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
        agent, executor = harness.ledger_identity_for_operator(getpass.getuser())
        self.assertEqual(entries[0]["agent"], agent)
        self.assertEqual(entries[0].get("executor"), executor)
        self.assertIn(f"operator: {getpass.getuser()}", entries[0]["rationale"])

    def test_operator_flag_overrides(self):
        _run(
            ["--operator", "recovery", "orchestrator", "hold", "codex/interactive-chatgpt", "--reason", "x"],
            root=self.root, ledger_dir=self.ledger_dir,
        )
        entries = _read_ledger_entries(self.ledger_dir)
        # T-4737: a fleet seat writes its ORG-100 role id and seat slot.
        self.assertEqual(entries[0]["agent"], "head_system_orchestrator")
        self.assertEqual(entries[0]["executor"], "head-system-orchestrator")


class TestSharedRoleOperatorRefused(HarnessCliTestCase):
    def test_shared_role_id_as_operator_is_refused_before_anything_runs(self):
        # T-4737 (review F2): engineering_orchestrator is artisan AND mason.
        code, _out, err = _run_capture_stderr(
            ["--operator", "engineering_orchestrator", "orchestrator", "hold", "codex/interactive-chatgpt", "--reason", "x"],
            root=self.root, ledger_dir=self.ledger_dir,
        )
        self.assertEqual(code, 2)
        self.assertIn("shared by several seats", err)
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])


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


class TestRestartHandoffFirst(HarnessCliTestCase):
    """T-5069: restart of a live session asks for a fresh HANDOFF first."""

    def _seed_active(self):
        active_path = hs.active_runtime_path("orchestrator", root=self.root)
        active_path.parent.mkdir(parents=True)
        active_path.write_text(
            json.dumps({"schema": hs.SCHEMA_ACTIVE_RUNTIME, "runtime": "claude/anthropic-code", "tmux_session": "s1"}),
            encoding="utf-8",
        )

    def _run(self, extra, *, live=True, confirmed=True):
        from tools import handoff_first

        outcome = handoff_first.HandoffOutcome(confirmed, "no ack" if not confirmed else "ok", Path("/x/HANDOFF.md"))
        out, err = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err),
            mock.patch("tools.harness._tmux_session_live", return_value=live),
            mock.patch("tools.harness._request_handoff_first", return_value=outcome) as ask,
            mock.patch("tools.harness._kill_tmux_session", return_value=True) as kill,
        ):
            code = harness.main(
                ["orchestrator", "restart", "--reason", "x", *extra], root=self.root, ledger_dir=self.ledger_dir
            )
        return code, out.getvalue(), err.getvalue(), ask, kill

    def test_confirmed_handoff_then_kill(self):
        self._seed_active()
        code, out, _err, ask, kill = self._run([])
        self.assertEqual(code, 0)
        ask.assert_called_once_with("orchestrator")
        kill.assert_called_once()
        self.assertIn("fresh handoff confirmed", out)

    def test_unconfirmed_handoff_refuses_and_does_not_kill(self):
        self._seed_active()
        code, _out, err, ask, kill = self._run([], confirmed=False)
        self.assertEqual(code, 2)
        self.assertIn("--skip-handoff", err)
        kill.assert_not_called()
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])

    def test_skip_handoff_kills_without_asking(self):
        self._seed_active()
        code, out, _err, ask, kill = self._run(["--skip-handoff"], confirmed=False)
        self.assertEqual(code, 0)
        ask.assert_not_called()
        kill.assert_called_once()
        self.assertIn("handoff skipped", out)

    def test_no_live_session_no_handoff_request(self):
        self._seed_active()
        code, _out, _err, ask, kill = self._run([], live=False)
        self.assertEqual(code, 0)
        ask.assert_not_called()

    def test_dry_run_does_not_ask(self):
        self._seed_active()
        code, out, _err, ask, kill = self._run(["--dry-run"], confirmed=False)
        self.assertEqual(code, 0)
        ask.assert_not_called()
        self.assertIn("would ask the live session", out)


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
        # T-4734: ORG-001 WARN lines (the harness writer conforms in T-4737)
        # are not a collision refusal; nothing else may reach stderr.
        self.assertEqual(_without_org001_warnings(err2), "")
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


def _git(*args, cwd):
    import subprocess

    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


class _LandingFixture(unittest.TestCase):
    """T-5021: with no ledger_dir (production), an override's ledger entry lands on
    origin/main from a throwaway worktree and NEVER dirties the clone harness.py
    runs from (the seat's). Real git against a tmp bare origin + tmp seat clone."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        gitconfig = base / "gitconfig"
        gitconfig.write_text("", encoding="utf-8")
        env = mock.patch.dict(
            "os.environ",
            {
                "GIT_CONFIG_GLOBAL": str(gitconfig),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "seat",
                "GIT_AUTHOR_EMAIL": "seat@example.invalid",
                "GIT_COMMITTER_NAME": "seat",
                "GIT_COMMITTER_EMAIL": "seat@example.invalid",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.origin = base / "origin.git"
        self.seat = base / "seat-clone"
        self.state = base / "state"
        self.land_tmp = base / "landing-tmp"
        self.land_tmp.mkdir()
        _git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=base)
        _git("clone", "-q", str(self.origin), str(self.seat), cwd=base)
        (self.seat / "ledger").mkdir()
        (self.seat / "ledger" / ".keep").write_text("", encoding="utf-8")
        _git("add", "ledger/.keep", cwd=self.seat)
        _git("commit", "-q", "-m", "seed", cwd=self.seat)
        _git("push", "-q", "origin", "HEAD:main", cwd=self.seat)

    def tearDown(self):
        self._tmp.cleanup()

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch(
            "tools.harness._kill_tmux_session", return_value=False
        ):
            code = harness.main(
                argv, root=self.state, repo_root=self.seat, landing_tmp_root=self.land_tmp
            )
        return code, out.getvalue(), err.getvalue()

    def _origin_entries(self) -> list[dict]:
        listing = _git("ls-tree", "-r", "--name-only", "main", cwd=self.origin).split()
        entries = []
        for name in listing:
            if name.endswith(".jsonl"):
                text = _git("show", f"main:{name}", cwd=self.origin)
                entries += [json.loads(line) for line in text.splitlines() if line.strip()]
        return entries

    def _assert_seat_clone_untouched(self):
        self.assertEqual(_git("status", "--porcelain", "--untracked-files=all", cwd=self.seat), "")
        self.assertEqual([p.name for p in (self.seat / "ledger").iterdir()], [".keep"])
        worktrees = [line for line in _git("worktree", "list", "--porcelain", cwd=self.seat).splitlines()
                     if line.startswith("worktree ")]
        self.assertEqual(len(worktrees), 1)
        self.assertEqual(list(self.land_tmp.iterdir()), [])

class TestOverrideLandsOnOriginNotSeatClone(_LandingFixture):
    def setUp(self):
        super().setUp()
        _freeze_skew_check(self)

    def test_hold_lands_on_origin_and_leaves_the_seat_clone_clean(self):
        code, out, err = self._main(
            ["--operator", "orchestrator", "specialist", "hold", "claude/interactive-subscription", "--reason", "T-5021"]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("hold on specialist", out)
        entries = self._origin_entries()
        self.assertEqual([e["action"] for e in entries], ["harness_override"])
        self.assertEqual(entries[0]["agent"], harness.ledger_identity_for_operator("orchestrator")[0])
        self._assert_seat_clone_untouched()
        self.assertFalse(harness.spool_path(self.state).exists())
        self.assertIn("claude/interactive-subscription", hs.load_holds("specialist", root=self.state)["holds"])

    def test_unreachable_origin_spools_0600_then_next_run_drains_idempotently(self):
        good_url = _git("remote", "get-url", "origin", cwd=self.seat).strip()
        _git("remote", "set-url", "origin", str(self.seat.parent / "missing.git"), cwd=self.seat)
        code, out, err = self._main(["specialist", "hold", "claude/interactive-subscription", "--reason", "down"])
        self.assertEqual(code, 2)
        self.assertIn("hold on specialist", out)
        self.assertIn("did not reach origin/main", err)
        spool = harness.spool_path(self.state)
        self.assertEqual(spool.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(harness._read_spool(self.state)), 1)
        self._assert_seat_clone_untouched()
        # The override itself took effect.
        self.assertIn("claude/interactive-subscription", hs.load_holds("specialist", root=self.state)["holds"])

        _git("remote", "set-url", "origin", good_url, cwd=self.seat)
        with mock.patch("tools.harness._utc_now_ts", return_value="2026-10-15T03:00:00Z"):
            code, _out, err = self._main(["specialist", "clear-hold", "claude/interactive-subscription", "--reason", "up"])
        self.assertEqual(code, 0, err)
        self.assertEqual(
            sorted(e["id"].split("-override-", 1)[1] for e in self._origin_entries()), ["clear-hold", "hold"]
        )
        self.assertFalse(spool.exists())
        self._assert_seat_clone_untouched()

    def test_spooled_entry_already_on_origin_is_dropped_not_duplicated(self):
        code, _out, err = self._main(["specialist", "hold", "codex/interactive-chatgpt", "--reason", "a"])
        self.assertEqual(code, 0, err)
        landed = self._origin_entries()[0]
        harness._append_spool(self.state, landed)  # a crash after the landing, before the drain
        with mock.patch("tools.harness._utc_now_ts", return_value="2026-10-15T03:00:00Z"):
            code, _out, err = self._main(["specialist", "clear-hold", "codex/interactive-chatgpt", "--reason", "b"])
        self.assertEqual(code, 0, err)
        ids = [e["id"] for e in self._origin_entries()]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 2)
        self.assertFalse(harness.spool_path(self.state).exists())

    def test_network_steps_are_bounded_by_a_timeout(self):
        import subprocess

        real_run = subprocess.run
        seen: list[tuple] = []

        def run(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "fetch"]:
                seen.append(tuple(cmd))
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
            return real_run(cmd, *args, **kwargs)

        with mock.patch("tools.harness.subprocess.run", side_effect=run):
            code, _out, err = self._main(["specialist", "hold", "codex/interactive-chatgpt", "--reason", "slow"])
        self.assertEqual(code, 2)
        self.assertIn(f"timed out after {harness.GIT_FETCH_TIMEOUT_SECONDS}s", err)
        self.assertTrue(seen)
        self.assertEqual(len(harness._read_spool(self.state)), 1)
        self._assert_seat_clone_untouched()

    def test_dry_run_writes_nothing_anywhere(self):
        code, _out, _err = self._main(["--dry-run", "specialist", "hold", "codex/interactive-chatgpt", "--reason", "d"])
        self.assertEqual(code, 0)
        self.assertEqual(self._origin_entries(), [])
        self.assertFalse(harness.spool_path(self.state).exists())
        self._assert_seat_clone_untouched()

    def test_status_reports_unlanded_entries_on_a_separate_line_only_when_present(self):
        probe = lambda bindings: None  # noqa: E731
        args = harness.build_parser().parse_args(["specialist", "status"])
        before = io.StringIO()
        with contextlib.redirect_stdout(before):
            harness._do_status(args, root=self.state, pane_probe=probe)
        self.assertNotIn("unlanded", before.getvalue())
        harness._append_spool(self.state, {"id": "x", "summary": "s"})
        after = io.StringIO()
        with contextlib.redirect_stdout(after):
            harness._do_status(args, root=self.state, pane_probe=probe)
        self.assertEqual(after.getvalue().splitlines()[:-1], before.getvalue().splitlines())
        self.assertEqual(
            after.getvalue().splitlines()[-1],
            f"unlanded ledger entries: 1 (spooled at {harness.spool_path(self.state)})",
        )

    def test_a_permanently_bad_spooled_entry_is_quarantined_and_does_not_block(self):
        bad = {"id": "bad-entry", "ts": "not-a-timestamp", "agent": "x", "action": "harness_override"}
        harness._append_spool(self.state, bad)
        code, _out, err = self._main(["specialist", "hold", "codex/interactive-chatgpt", "--reason", "q"])
        self.assertEqual(code, 0, err)
        self.assertEqual([e["action"] for e in self._origin_entries()], ["harness_override"])
        self.assertNotIn("bad-entry", [e["id"] for e in self._origin_entries()])
        self.assertFalse(harness.spool_path(self.state).exists())
        rows = harness._read_quarantine(self.state)
        self.assertEqual([r["entry"]["id"] for r in rows], ["bad-entry"])
        self.assertIn("ledger writer refused", rows[0]["reason"])
        self.assertEqual(harness.quarantine_path(self.state).stat().st_mode & 0o777, 0o600)
        # A later override is not blocked by it either.
        with mock.patch("tools.harness._utc_now_ts", return_value="2026-10-15T03:00:00Z"):
            code, _out, err = self._main(["specialist", "clear-hold", "codex/interactive-chatgpt", "--reason", "q2"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(harness._read_quarantine(self.state)), 1)
        self._assert_seat_clone_untouched()

    def test_a_conflicting_spooled_entry_is_quarantined_and_the_new_one_lands(self):
        code, _out, err = self._main(["specialist", "hold", "codex/interactive-chatgpt", "--reason", "a"])
        self.assertEqual(code, 0, err)
        on_main = self._origin_entries()[0]
        harness._append_spool(self.state, {**on_main, "summary": "a different body"})
        with mock.patch("tools.harness._utc_now_ts", return_value="2026-10-15T03:00:00Z"):
            code, _out, err = self._main(["specialist", "clear-hold", "codex/interactive-chatgpt", "--reason", "b"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self._origin_entries()), 2)
        rows = harness._read_quarantine(self.state)
        self.assertEqual([r["entry"]["id"] for r in rows], [on_main["id"]])
        self.assertIn("different content", rows[0]["reason"])
        self.assertFalse(harness.spool_path(self.state).exists())

    def test_status_reports_quarantined_entries_on_a_separate_line(self):
        probe = lambda bindings: None  # noqa: E731
        args = harness.build_parser().parse_args(["specialist", "status"])
        harness._append_quarantine(self.state, {"id": "x"}, "why")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            harness._do_status(args, root=self.state, pane_probe=probe)
        self.assertEqual(
            out.getvalue().splitlines()[-1],
            f"quarantined ledger entries: 1 (at {harness.quarantine_path(self.state)})",
        )

    def test_crash_leftovers_are_swept_and_pruned(self):
        import os
        import shutil
        import time

        crashed = self.land_tmp / "harness-ledger-crashed"
        _git("worktree", "add", "-q", "--detach", str(crashed / "wt"), "HEAD", cwd=self.seat)
        old = time.time() - harness.STALE_LANDING_DIR_SECONDS - 60
        os.utime(crashed, (old, old))
        fresh = self.land_tmp / "harness-ledger-in-flight"
        fresh.mkdir()
        unrelated = self.land_tmp / "someone-else-old"
        unrelated.mkdir()
        os.utime(unrelated, (old, old))
        # The crashed run's checkout dir is gone but its .git/worktrees record stays.
        shutil.rmtree(crashed / "wt")
        os.utime(crashed, (old, old))
        code, _out, err = self._main(["specialist", "hold", "codex/interactive-chatgpt", "--reason", "s"])
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(p.name for p in self.land_tmp.iterdir()), ["harness-ledger-in-flight", "someone-else-old"])
        worktrees = [line for line in _git("worktree", "list", "--porcelain", cwd=self.seat).splitlines()
                     if line.startswith("worktree ")]
        self.assertEqual(len(worktrees), 1)

    def test_the_spool_lock_is_not_held_across_the_network(self):
        import fcntl
        import os

        observed = []

        def land(entries, **kwargs):
            lock = harness.spool_path(self.state).with_suffix(".lock")
            fd = os.open(lock, os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if the caller holds it
                observed.append("free")
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            return harness.LandingResult(True, "stub", landed=tuple(e["id"] for e in entries))

        with mock.patch("tools.harness.land_ledger_entries", side_effect=land):
            code, _out, err = self._main(["specialist", "hold", "codex/interactive-chatgpt", "--reason", "l"])
        self.assertEqual(code, 0, err)
        self.assertEqual(observed, ["free"])

    def test_ledger_dir_callers_keep_the_direct_append_and_exit_codes(self):
        ledger_dir = Path(self._tmp.name) / "ledger-direct"
        ledger_dir.mkdir()
        with mock.patch("tools.harness.land_ledger_entries") as lander:
            code, _out = _run(["specialist", "hold", "codex/interactive-chatgpt", "--reason", "t"],
                              root=self.state, ledger_dir=ledger_dir)
            with mock.patch("tools.harness.append_entry", side_effect=RuntimeError("disk full")):
                code2, _o, err2 = _run_capture_stderr(
                    ["specialist", "clear-hold", "codex/interactive-chatgpt", "--reason", "t"],
                    root=self.state, ledger_dir=ledger_dir,
                )
        self.assertEqual(code, 0)
        self.assertEqual(len(_read_ledger_entries(ledger_dir)), 1)
        lander.assert_not_called()
        self.assertEqual(code2, 2)
        self.assertIn("failed to append ledger entry", err2)
        self.assertFalse(harness.spool_path(self.state).exists())


class TestOperatorRestartMarker(HarnessCliTestCase):
    """T-5483: restart drops a durable marker BEFORE the kill; fail-open; dry-run writes none."""

    def _seed_active(self):
        path = hs.active_runtime_path("orchestrator", root=self.root)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"schema": hs.SCHEMA_ACTIVE_RUNTIME, "runtime": "claude/anthropic-code",
                        "tmux_session": "s1", "pid": 4242}),
            encoding="utf-8",
        )

    def _restart(self, extra=(), *, live=True, kill=None, write_patch=None):
        seen = {}

        def fake_kill(session, dry_run):
            seen["marker_at_kill"] = hs.load_operator_restart_marker("orchestrator", root=self.root)
            return True

        out, err = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err),
            mock.patch("tools.harness._tmux_session_live", return_value=live),
            mock.patch("tools.harness._request_handoff_first",
                       return_value=__import__("tools.handoff_first", fromlist=["x"]).HandoffOutcome(True, "ok", Path("/x"))),
            mock.patch("tools.harness._kill_tmux_session", side_effect=kill or fake_kill),
        ):
            if write_patch is not None:
                with mock.patch.object(hs, "write_operator_restart_marker", side_effect=write_patch):
                    code = harness.main(["orchestrator", "restart", "--reason", "x", *extra],
                                        root=self.root, ledger_dir=self.ledger_dir)
            else:
                code = harness.main(["orchestrator", "restart", "--reason", "x", *extra],
                                    root=self.root, ledger_dir=self.ledger_dir)
        return code, seen, err.getvalue()

    def test_marker_written_before_kill_with_fields(self):
        self._seed_active()
        code, seen, _err = self._restart()
        self.assertEqual(code, 0)
        marker = seen["marker_at_kill"]
        self.assertEqual(marker["runtime"], "claude/anthropic-code")
        self.assertEqual(marker["pid"], 4242)
        self.assertEqual(marker["tmux_session"], "s1")
        self.assertTrue(marker["correlation_id"])
        self.assertTrue(marker["ts"])

    def test_dry_run_writes_no_marker(self):
        self._seed_active()
        code, seen, _err = self._restart(["--dry-run"])
        self.assertEqual(code, 0)
        self.assertIsNone(seen["marker_at_kill"])
        self.assertFalse(hs.operator_restart_path("orchestrator", root=self.root).exists())

    def test_no_active_runtime_cache_no_marker(self):
        code, _seen, _err = self._restart()
        self.assertEqual(code, 0)
        self.assertFalse(hs.operator_restart_path("orchestrator", root=self.root).exists())

    def test_marker_removed_when_session_was_not_live(self):
        self._seed_active()
        _code, _seen, _err = self._restart(kill=lambda session, dry_run: False)
        self.assertFalse(hs.operator_restart_path("orchestrator", root=self.root).exists())

    def test_marker_write_failure_is_fail_open(self):
        self._seed_active()
        code, seen, err = self._restart(write_patch=OSError("disk full"))
        self.assertEqual(code, 0)  # the restart still happened
        self.assertIsNone(seen["marker_at_kill"])
        self.assertIn("marker not written", err)


class TestControllerOwnedRestartCleanup(HarnessCliTestCase):
    """T-6180: controller-owned app-server panes must unwind before tmux dies."""

    @staticmethod
    def _completed(returncode=0, stdout=""):
        return mock.Mock(returncode=returncode, stdout=stdout)

    def test_controller_owned_wrapper_gets_pidfd_term_and_cleanup_completes(self):
        pane = "4321\t/usr/bin/python3 /repo/harness/codex/appserver_supervise.py --controller-owned\n"
        calls = [self._completed(), self._completed(stdout=pane), self._completed(stdout=pane)]
        with mock.patch.object(harness.subprocess, "run", side_effect=calls) as run, \
                mock.patch.object(harness.os, "pidfd_open", return_value=71) as pidfd_open, \
                mock.patch.object(harness.os, "close") as close, \
                mock.patch.object(harness.signal, "pidfd_send_signal") as send, \
                mock.patch.object(
                    harness, "_live_process_identity",
                    return_value=(123, ("python3", "/repo/appserver_supervise.py", "--controller-owned")),
                ), \
                mock.patch.object(harness.select, "select", return_value=([71], [], [])):
            self.assertTrue(harness._kill_tmux_session("s1", dry_run=False))

        pidfd_open.assert_called_once_with(4321)
        send.assert_called_once_with(71, signal.SIGTERM)
        close.assert_called_once_with(71)
        self.assertNotIn(["tmux", "kill-session", "-t", "s1"], [c.args[0] for c in run.call_args_list])

    def test_controller_owned_wrapper_identity_change_refuses_before_signal(self):
        pane1 = "4321\t/usr/bin/python3 /repo/harness/codex/appserver_supervise.py --controller-owned\n"
        pane2 = "9876\t/usr/bin/python3 /repo/harness/codex/appserver_supervise.py --controller-owned\n"
        calls = [self._completed(), self._completed(stdout=pane1), self._completed(stdout=pane2)]
        with mock.patch.object(harness.subprocess, "run", side_effect=calls), \
                mock.patch.object(harness.os, "pidfd_open", return_value=71), \
                mock.patch.object(harness.os, "close"), \
                mock.patch.object(
                    harness, "_live_process_identity",
                    return_value=(123, ("python3", "/repo/appserver_supervise.py", "--controller-owned")),
                ), \
                mock.patch.object(harness.signal, "pidfd_send_signal") as send:
            with self.assertRaisesRegex(hs.HarnessStateError, "pane identity changed"):
                harness._kill_tmux_session("s1", dry_run=False)
        send.assert_not_called()

    def test_controller_owned_wrapper_cleanup_timeout_never_hard_kills_tmux(self):
        pane = "4321\t/usr/bin/python3 /repo/harness/codex/appserver_supervise.py --controller-owned\n"
        calls = [self._completed(), self._completed(stdout=pane), self._completed(stdout=pane)]
        with mock.patch.object(harness.subprocess, "run", side_effect=calls) as run, \
                mock.patch.object(harness.os, "pidfd_open", return_value=71), \
                mock.patch.object(harness.os, "close"), \
                mock.patch.object(
                    harness, "_live_process_identity",
                    return_value=(123, ("python3", "/repo/appserver_supervise.py", "--controller-owned")),
                ), \
                mock.patch.object(harness.signal, "pidfd_send_signal"), \
                mock.patch.object(harness.select, "select", return_value=([], [], [])), \
                mock.patch.object(harness.time, "monotonic", side_effect=[0.0, 0.0, 46.0]):
            with self.assertRaisesRegex(hs.HarnessStateError, "cleanup did not complete"):
                harness._kill_tmux_session("s1", dry_run=False)
        self.assertNotIn(["tmux", "kill-session", "-t", "s1"], [c.args[0] for c in run.call_args_list])

    def test_tmux_probe_failure_cannot_replace_pidfd_extinction_receipt(self):
        pane = "4321\t/usr/bin/python3 /repo/harness/codex/appserver_supervise.py --controller-owned\n"
        calls = [self._completed(), self._completed(stdout=pane), self._completed(stdout=pane)]
        with mock.patch.object(harness.subprocess, "run", side_effect=calls) as run, \
                mock.patch.object(harness.os, "pidfd_open", return_value=71), \
                mock.patch.object(harness.os, "close"), \
                mock.patch.object(
                    harness, "_live_process_identity",
                    return_value=(123, ("python3", "/repo/appserver_supervise.py", "--controller-owned")),
                ), \
                mock.patch.object(harness.signal, "pidfd_send_signal"), \
                mock.patch.object(harness, "_tmux_session_live", return_value=False), \
                mock.patch.object(harness.select, "select", return_value=([], [], [])), \
                mock.patch.object(harness.time, "monotonic", side_effect=[0.0, 0.0, 46.0]):
            with self.assertRaisesRegex(hs.HarnessStateError, "pinned wrapper remained live"):
                harness._kill_tmux_session("s1", dry_run=False)
        self.assertNotIn(["tmux", "kill-session", "-t", "s1"], [c.args[0] for c in run.call_args_list])

    def test_real_pidfd_refuses_when_tmux_probe_fails_but_wrapper_stays_alive(self):
        fixture = Path(self._tmp.name)
        ready = fixture / "sigterm-ignored.ready"
        wrapper = subprocess.Popen([
            sys.executable,
            "-c",
            (
                "import signal,time,pathlib; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"pathlib.Path({str(ready)!r}).touch(); "
                "time.sleep(60)"
            ),
        ])
        def cleanup_wrapper():
            if wrapper.poll() is None:
                wrapper.kill()
            wrapper.wait(timeout=5)

        self.addCleanup(cleanup_wrapper)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready.exists())
        pane_command = "/repo/harness/codex/appserver_supervise.py --controller-owned"

        with mock.patch.object(
            harness, "_tmux_pane_identity", return_value=(wrapper.pid, pane_command)
        ), mock.patch.object(
            harness, "_live_process_identity",
            return_value=(123, (sys.executable, "/repo/appserver_supervise.py", "--controller-owned")),
        ), mock.patch.object(harness, "_tmux_session_live", return_value=False):
            with self.assertRaisesRegex(hs.HarnessStateError, "pinned wrapper remained live"):
                harness._graceful_controller_owned_shutdown(
                    "s1", wrapper.pid, pane_command, timeout=0.05
                )

        self.assertIsNone(wrapper.poll(), "tmux probe failure must not fake wrapper extinction")

    def test_tmux_wrapper_history_cannot_classify_live_sleep_process(self):
        sleeper = subprocess.Popen(["sleep", "60"])
        def cleanup_sleeper():
            if sleeper.poll() is None:
                sleeper.kill()
            sleeper.wait(timeout=5)

        self.addCleanup(cleanup_sleeper)
        # Popen returns after fork, not after the child has completed exec.
        # Wait for the exact fixture argv so an empty pre-exec cmdline cannot
        # make this test pass through the wrong fail-closed refusal.
        deadline = time.monotonic() + 5
        observed_argv = ()
        while time.monotonic() < deadline:
            try:
                raw = Path(f"/proc/{sleeper.pid}/cmdline").read_bytes()
            except FileNotFoundError:
                break
            observed_argv = tuple(part for part in raw.split(b"\0") if part)
            if observed_argv and Path(os.fsdecode(observed_argv[0])).name == "sleep":
                break
            time.sleep(0.01)
        self.assertTrue(observed_argv, "sleep fixture never exposed readable argv")
        self.assertEqual(Path(os.fsdecode(observed_argv[0])).name, "sleep")
        pane_command = "/repo/harness/codex/appserver_supervise.py --controller-owned"
        with mock.patch.object(
            harness, "_tmux_pane_identity", return_value=(sleeper.pid, pane_command)
        ), mock.patch.object(harness.signal, "pidfd_send_signal") as send:
            with self.assertRaisesRegex(hs.HarnessStateError, "live pane argv does not match"):
                harness._graceful_controller_owned_shutdown(
                    "s1", sleeper.pid, pane_command, timeout=0.05
                )
        send.assert_not_called()
        self.assertIsNone(sleeper.poll(), "unrelated live process must not be signalled")

    def test_plain_codex_pane_keeps_existing_tmux_kill_path(self):
        pane = "4321\tnode /usr/bin/codex resume thread-1\n"
        calls = [self._completed(), self._completed(stdout=pane), self._completed()]
        with mock.patch.object(harness.subprocess, "run", side_effect=calls) as run:
            self.assertTrue(harness._kill_tmux_session("s1", dry_run=False))
        self.assertIn(["tmux", "kill-session", "-t", "s1"], [c.args[0] for c in run.call_args_list])

    @unittest.skipUnless(shutil.which("tmux"), "needs tmux")
    def test_real_tmux_restart_waits_for_wrapper_to_reap_all_children(self):
        fixture = Path(self._tmp.name)
        real_tmux = shutil.which("tmux")
        socket_name = f"t6180-{os.getpid()}"
        shim = fixture / "tmux"
        shim.write_text(f"#!/bin/sh\nexec {real_tmux} -L {socket_name} \"$@\"\n")
        shim.chmod(0o755)
        wrapper = fixture / "appserver_supervise.py"
        children = fixture / "children.json"
        wrapper.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json, signal, subprocess, sys, time
            children = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True) for _ in range(3)]
            with open({str(children)!r}, 'w') as fh:
                json.dump([p.pid for p in children], fh)
            def stop(*_args):
                for p in children:
                    p.terminate()
                for p in children:
                    p.wait(timeout=5)
                raise SystemExit(0)
            signal.signal(signal.SIGTERM, stop)
            while True:
                time.sleep(1)
        """))
        wrapper.chmod(0o755)
        env = {**os.environ, "PATH": f"{fixture}:{os.environ['PATH']}", "TMUX": ""}
        session = "t6180-controller-owned"
        with mock.patch.dict(os.environ, env, clear=True):
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", session,
                 str(wrapper), "--controller-owned"], check=True,
            )
            self.addCleanup(
                lambda: subprocess.run(
                    [real_tmux, "-L", socket_name, "kill-server"], capture_output=True
                )
            )
            deadline = time.monotonic() + 5
            while not children.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(children.exists())
            child_pids = json.loads(children.read_text())
            self.assertTrue(harness._kill_tmux_session(session, dry_run=False))
            self.assertFalse(harness._tmux_session_live(session))
            for pid in child_pids:
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)


class TestOperatorRestartMarkerOrdering(HarnessCliTestCase):
    """T-5483: marker is written AFTER the handoff and immediately BEFORE the kill, on every path."""

    def _seed(self, with_hold=False):
        path = hs.active_runtime_path("orchestrator", root=self.root)
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"schema": hs.SCHEMA_ACTIVE_RUNTIME, "runtime": "claude/anthropic-code",
                        "tmux_session": "s1", "pid": 4242}),
            encoding="utf-8",
        )
        if with_hold:
            hs.write_holds(
                "orchestrator",
                {"schema": hs.SCHEMA_HOLDS, "holds": {"claude/anthropic-code": {"set_ts": "t", "set_by": "operator:x", "reason": "r"}}},
                root=self.root,
            )

    def _go(self, extra):
        from tools import handoff_first

        order = []
        clock = iter(f"2026-10-25T22:{m:02d}:00Z" for m in range(10, 59))

        def tick():
            return next(clock)

        def handoff(persona):
            order.append(("handoff", tick()))  # a long wait: the clock moves on
            return handoff_first.HandoffOutcome(True, "ok", Path("/x"))

        real_write = hs.write_operator_restart_marker

        def write(*a, **k):
            order.append(("marker", k["ts"]))
            return real_write(*a, **k)

        def kill(session, dry_run):
            order.append(("kill", tick()))
            return True

        with (
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()),
            mock.patch("tools.harness._tmux_session_live", return_value=True),
            mock.patch("tools.harness._utc_now_ts", side_effect=tick),
            mock.patch("tools.harness._request_handoff_first", side_effect=handoff),
            mock.patch("tools.harness._kill_tmux_session", side_effect=kill),
            mock.patch.object(hs, "write_operator_restart_marker", side_effect=write),
        ):
            code = harness.main(["orchestrator", "restart", "--reason", "x", *extra], root=self.root, ledger_dir=self.ledger_dir)
        return code, order

    def test_marker_ts_is_after_handoff_and_before_kill(self):
        self._seed()
        code, order = self._go([])
        self.assertEqual(code, 0)
        self.assertEqual([n for n, _ in order], ["handoff", "marker", "kill"])
        ts = {n: t for n, t in order}
        self.assertLess(ts["handoff"], ts["marker"])
        self.assertLess(ts["marker"], ts["kill"])

    def test_skip_handoff_path_writes_marker_before_kill(self):
        self._seed()
        code, order = self._go(["--skip-handoff"])
        self.assertEqual(code, 0)
        self.assertEqual([n for n, _ in order], ["marker", "kill"])

    def test_override_hold_path_writes_marker_before_kill(self):
        self._seed(with_hold=True)
        code, order = self._go(["--runtime", "claude/anthropic-code", "--override-hold"])
        self.assertEqual(code, 0)
        self.assertEqual([n for n, _ in order], ["handoff", "marker", "kill"])
        self.assertNotIn("claude/anthropic-code", hs.load_holds("orchestrator", root=self.root)["holds"])


class TestDelayedLandingKeepsOtherRules(_LandingFixture):
    """T-4739 review F1: the ts-vs-clock rule is checked when the entry is stamped.
    A spooled retry or a slow handler appends later, so landing measures skew against the
    entry's own ts. NO skew freeze here: real clock, real ENFORCE. Every other rule stays."""

    def _entry(self, age_seconds: int, slug: str = "old") -> dict:
        ts = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "id": f"{ts}-harness-specialist-override-{slug}", "ts": ts, "agent": "human:operator",
            "action": "harness_override", "rationale": "delayed", "justifies_via": ["P4"],
            "summary": "delayed", "links": [{"type": "persona", "ref": "specialist"}],
        }

    def test_a_spooled_entry_one_hour_old_lands_on_retry_and_is_not_quarantined(self):
        old = self._entry(3600)
        harness._append_spool(self.state, old)
        code, _out, err = self._main(["specialist", "hold", "codex/interactive-chatgpt", "--reason", "retry"])
        self.assertEqual(code, 0, err)
        self.assertIn(old["id"], [e["id"] for e in self._origin_entries()])
        self.assertEqual(harness._read_quarantine(self.state), [])
        self.assertFalse(harness.spool_path(self.state).exists())

    def test_a_direct_append_after_a_slow_handler_lands(self):
        with tempfile.TemporaryDirectory() as d:
            ledger = Path(d)
            slow = self._entry(600, "slow")  # stamped 10 minutes before the append
            self.assertEqual(harness._record_entry(
                slow, state_root=self.state, ledger_dir=ledger, ledger_root=ledger,
                repo_root=None, landing_tmp_root=None,
            ), ("landed", 0))
            self.assertEqual([e["id"] for e in _read_ledger_entries(ledger)], [slow["id"]])

    def test_a_spooled_entry_invalid_for_a_non_skew_reason_is_still_quarantined(self):
        bad = self._entry(3600, "norationale")
        del bad["rationale"]
        harness._append_spool(self.state, bad)
        code, _out, err = self._main(["specialist", "hold", "codex/interactive-chatgpt", "--reason", "q"])
        self.assertEqual(code, 0, err)
        self.assertNotIn(bad["id"], [e["id"] for e in self._origin_entries()])
        rows = harness._read_quarantine(self.state)
        self.assertEqual([r["entry"]["id"] for r in rows], [bad["id"]])
        self.assertIn("rationale", rows[0]["reason"])

    def test_append_entry_without_now_still_enforces_skew(self):
        with tempfile.TemporaryDirectory() as d:
            old = self._entry(3600, "skew")
            with self.assertRaises(ValueError) as ctx:
                ledger_shard.append_entry(Path(d), old)
            self.assertIn("write time", str(ctx.exception))
            self.assertEqual(list(Path(d).rglob("*.jsonl")), [])
            # and with now = its own ts the same entry lands
            ts = datetime.strptime(old["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            ledger_shard.append_entry(Path(d), old, now=ts)
            self.assertEqual(len(list(Path(d).rglob("*.jsonl"))), 1)


class TestEnforceValidatesBeforeSideEffects(HarnessCliTestCase):
    """T-4739 (orchestrator requirement 2): a ledger entry ORG-001 would refuse means NO override
    is applied, never an applied-but-unrecorded one. Tmpdir state/ledger only."""

    def _snapshot(self):
        return {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in sorted(self.root.rglob("*")) if p.is_file()
        }

    def _run_bad_identity(self, argv):
        err = io.StringIO()
        with (
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err),
            mock.patch("tools.harness._kill_tmux_session", return_value=True) as killed,
            mock.patch.object(harness, "ledger_identity_for_operator", return_value=("artisan", None)),
        ):
            code = harness.main(argv, root=self.root, ledger_dir=self.ledger_dir)
        return code, err.getvalue(), killed

    def test_restart_with_a_refusable_entry_kills_nothing_and_changes_no_state(self):
        active_path = hs.active_runtime_path("orchestrator", root=self.root)
        active_path.parent.mkdir(parents=True)
        active_path.write_text(
            json.dumps({"schema": hs.SCHEMA_ACTIVE_RUNTIME, "runtime": "claude/anthropic-code", "tmux_session": "s"}),
            encoding="utf-8",
        )
        before = self._snapshot()
        code, err, killed = self._run_bad_identity(["orchestrator", "restart", "--reason", "x"])
        self.assertEqual(code, 2)
        self.assertIn("no override applied", err)
        killed.assert_not_called()
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])

    def test_hold_with_a_refusable_entry_changes_no_state(self):
        before = self._snapshot()
        code, err, _killed = self._run_bad_identity(
            ["orchestrator", "hold", "codex/interactive-chatgpt", "--reason", "x"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(hs.load_holds("orchestrator", root=self.root)["holds"], {})
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])

    def test_a_conforming_entry_still_applies(self):
        code, _out = _run(
            ["orchestrator", "hold", "codex/interactive-chatgpt", "--reason", "x"],
            root=self.root, ledger_dir=self.ledger_dir,
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(_read_ledger_entries(self.ledger_dir)), 1)


class TestDeprecatedAccountRefused(HarnessCliTestCase):
    """T-4739 (orchestrator ruling F1 a): no harness run from the-ancestor's account."""

    def _main(self, argv, user):
        err = io.StringIO()
        with (
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err),
            mock.patch("tools.harness._kill_tmux_session", return_value=True) as killed,
        ):
            code = harness.main(argv, root=self.root, ledger_dir=self.ledger_dir, os_user_fn=lambda: user)
        return code, err.getvalue(), killed

    def test_mutating_run_from_the_ancestor_is_refused_with_nothing_changed(self):
        code, err, killed = self._main(
            ["orchestrator", "hold", "codex/interactive-chatgpt", "--reason", "x"], "the-ancestor"
        )
        self.assertEqual(code, 2)
        self.assertIn("the-ancestor", err)
        killed.assert_not_called()
        self.assertEqual(hs.load_holds("orchestrator", root=self.root)["holds"], {})
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])

    def test_read_only_and_dry_run_from_the_ancestor_are_refused_too(self):
        for argv in (["orchestrator", "status"], ["orchestrator", "hold", "codex/interactive-chatgpt", "--dry-run", "--reason", "x"]):
            with self.subTest(argv=argv):
                code, _err, _killed = self._main(argv, "the-ancestor")
                self.assertEqual(code, 2)

    def test_logname_in_the_environment_does_not_decide_the_account(self):
        # F2: getpass.getuser() trusts LOGNAME/USER; the check uses the effective uid.
        fake_pw = mock.Mock(pw_name="some-seat")
        err = io.StringIO()
        with (
            mock.patch.dict("os.environ", {"LOGNAME": "the-ancestor", "USER": "the-ancestor"}),
            mock.patch.object(harness.os, "geteuid", return_value=4242),
            mock.patch.object(harness.pwd, "getpwuid", return_value=fake_pw) as lookup,
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err),
        ):
            code = harness.main(["orchestrator", "status"], root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 0, err.getvalue())
        lookup.assert_any_call(4242)

    def test_euid_mapped_to_the_ancestor_is_refused_whatever_the_environment_says(self):
        fake_pw = mock.Mock(pw_name="the-ancestor")
        err = io.StringIO()
        with (
            mock.patch.dict("os.environ", {"LOGNAME": "somebody-else", "USER": "somebody-else"}),
            mock.patch.object(harness.os, "geteuid", return_value=4243),
            mock.patch.object(harness.pwd, "getpwuid", return_value=fake_pw),
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err),
        ):
            code = harness.main(["orchestrator", "status"], root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 2)
        self.assertIn("the-ancestor", err.getvalue())

    def test_any_other_account_is_not_refused(self):
        code, _err, _killed = self._main(["orchestrator", "status"], "somebody-else")
        self.assertEqual(code, 0)


class TestSeatAccountGuard(HarnessCliTestCase):
    """T-5483: harness state lives in the seat account's home."""

    def _main(self, argv, *, euid, expected):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = harness.main(
                argv, root=self.root, ledger_dir=self.ledger_dir,
                euid_fn=lambda: euid, account_fn=lambda persona: expected,
            )
        return code, err.getvalue()

    def test_mutating_as_wrong_account_is_refused_before_anything_runs(self):
        import os, pwd

        me = pwd.getpwuid(os.geteuid()).pw_name
        code, err = self._main(["orchestrator", "hold", "codex/interactive-chatgpt", "--reason", "x"],
                               euid=os.geteuid(), expected=me + "-other")
        self.assertEqual(code, 2)
        self.assertIn("refusing", err)
        self.assertIn("pantheon-sudo -u", err)
        self.assertEqual(hs.load_holds("orchestrator", root=self.root)["holds"], {})
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])

    def test_status_and_dry_run_only_warn(self):
        import os, pwd

        me = pwd.getpwuid(os.geteuid()).pw_name
        for argv in (["orchestrator", "status"], ["orchestrator", "hold", "codex/interactive-chatgpt", "--dry-run", "--reason", "x"]):
            code, err = self._main(argv, euid=os.geteuid(), expected=me + "-other")
            self.assertEqual(code, 0, argv)
            self.assertIn("WARN", err)

    def test_mutating_as_root_refused_even_when_account_unresolved(self):
        code, err = self._main(["orchestrator", "hold", "codex/interactive-chatgpt", "--reason", "x"], euid=0, expected=None)
        self.assertEqual(code, 2)
        self.assertIn("root", err)
        code, err = self._main(["orchestrator", "status"], euid=0, expected=None)
        self.assertEqual(code, 0)
        self.assertIn("WARN", err)

    def test_matching_account_is_silent(self):
        import os, pwd

        me = pwd.getpwuid(os.geteuid()).pw_name
        code, err = self._main(["orchestrator", "status"], euid=os.geteuid(), expected=me)
        self.assertEqual(code, 0)
        self.assertNotIn("WARN", err)


class TestSeatPauseWake(HarnessCliTestCase):
    """ORG-106 L1 (T-5524): pause / wake verbs and seat state in status."""

    def setUp(self):
        super().setUp()

    def _pause(self, *extra, persona="specialist"):
        return _run_capture_stderr(
            ["--operator", "operator", persona, "pause", *extra],
            root=self.root, ledger_dir=self.ledger_dir)

    def _wake(self, *extra, persona="specialist"):
        return _run_capture_stderr(
            ["--operator", "operator", persona, "wake", *extra],
            root=self.root, ledger_dir=self.ledger_dir)

    def _record(self, persona="specialist"):
        return hs.load_seat_state(persona, root=self.root)

    def _tombstones(self, persona="specialist"):
        path = hs.seat_state_tombstones_path(persona, root=self.root)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def _plant(self, **over):
        doc = {
            "schema": hs.SCHEMA_SEAT_STATE, "persona": "specialist", "state": "paused-intake",
            "phase": "set", "phase_ts": "2026-01-01T00:00:00Z", "set_ts": "2026-01-01T00:00:00Z",
            "set_by": "operator:x", "owner": "operator", "reason": "old", "ticket": None,
            "expires": "2026-01-01T04:00:00Z", "indefinite": False,
            "telegram_dark_ack": False, "pre_expiry_notified": False, "ledger_entry": "e",
        }
        doc.update(over)
        path = hs.seat_state_path("specialist", root=self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))

    def test_pause_without_reason_refused(self):
        code, _out, err = self._pause()
        self.assertEqual(code, 2)
        self.assertIn("reason", err)
        self.assertIsNone(self._record())

    def test_wake_without_reason_refused(self):
        code, _out, err = self._wake()
        self.assertEqual(code, 2)
        self.assertIn("reason", err)

    # --- T-5868 Phase B: `wake --clear-halted` (notes s10: the ledgered operator escape) ---

    def _plant_halted(self, run_id="01HZZZZZZZZZZZZZZZZZZZZZZZ"):
        from tools import seat_state
        self._plant(state="halted", phase="halted", expires=None, indefinite=True, run_id=run_id,
                    op_id="rs-drain", halted_at="2026-01-01T00:30:00Z", pause_id="rs-specialist-x",
                    rollback_cmd=seat_state.halted_rollback_cmd(run_id))

    def _clear_halted(self, *extra, state="ROLLED_BACK"):
        with mock.patch("tools.germinate.engine.executor.run_state_of", return_value=state):
            return self._wake("--clear-halted", "--simulated-host-root", str(self.root), *extra)

    def test_plain_wake_never_clears_a_halted_record(self):
        self._plant_halted()
        code, _out, err = self._wake("--reason", "x")
        self.assertEqual(code, 2)
        self.assertIn("HALTED", err)
        self.assertEqual(self._record()["state"], "halted")
        self.assertEqual(self._tombstones(), [])

    def test_clear_halted_requires_a_reason(self):
        self._plant_halted()
        code, _out, err = self._wake("--clear-halted", "--force")
        self.assertEqual(code, 2)
        self.assertIn("reason", err)
        self.assertEqual(self._record()["state"], "halted")

    def test_clear_halted_refuses_a_non_terminal_run(self):
        self._plant_halted()
        for state in ("HALTED", "ROLLBACK_INCOMPLETE", "ROLLING_BACK", None):
            code, _out, err = self._clear_halted("--reason", "operator", state=state)
            self.assertEqual(code, 2, state)
            self.assertIn("not in a terminal rollback state", err)
        self.assertEqual(self._record()["state"], "halted")
        self.assertEqual(self._tombstones(), [])
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])

    def test_clear_halted_refuses_without_a_run_store_unless_forced(self):
        self._plant_halted()
        code, _out, err = self._wake("--clear-halted", "--reason", "operator")
        self.assertEqual(code, 2)
        self.assertIn("--simulated-host-root", err)
        self.assertEqual(self._record()["state"], "halted")

    def test_clear_halted_works_on_a_rolled_back_run_and_is_ledgered(self):
        self._plant_halted()
        code, _out, err = self._clear_halted("--reason", "rollback verified by hand")
        self.assertEqual(code, 0, err)
        self.assertIsNone(self._record())
        [tomb] = self._tombstones()
        self.assertEqual(tomb["state"], "halted")
        self.assertEqual(tomb["ended_by"], "operator:operator")
        [entry] = _read_ledger_entries(self.ledger_dir)
        self.assertIn("HALTED record cleared", entry["rationale"])
        self.assertIn("state ROLLED_BACK", entry["rationale"])
        self.assertNotIn("FORCED", entry["rationale"])
        self.assertIn("rollback verified by hand", entry["rationale"])

    def test_clear_halted_force_clears_a_non_terminal_run_and_says_so_in_the_ledger(self):
        self._plant_halted()
        code, _out, err = self._clear_halted("--reason", "run store lost", "--force", state="ROLLBACK_INCOMPLETE")
        self.assertEqual(code, 0, err)
        self.assertIsNone(self._record())
        [entry] = _read_ledger_entries(self.ledger_dir)
        self.assertIn("FORCED", entry["rationale"])
        self.assertIn("ROLLBACK_INCOMPLETE", entry["rationale"])

    def test_clear_halted_refuses_a_record_that_is_not_halted_and_stray_flags(self):
        self._plant(expires="2999-01-01T00:00:00Z")
        code, _out, err = self._wake("--clear-halted", "--force", "--reason", "x")
        self.assertEqual(code, 2)
        self.assertIn("not halted", err)
        self.assertIsNotNone(self._record())
        code, _out, err = self._wake("--force", "--reason", "x")
        self.assertEqual(code, 2)
        self.assertIn("only for wake --clear-halted", err)

    def test_clear_halted_dry_run_writes_nothing(self):
        self._plant_halted()
        with mock.patch("tools.germinate.engine.executor.run_state_of", return_value="ROLLED_BACK"):
            code, _out, err = _run_capture_stderr(
                ["--operator", "operator", "--dry-run", "specialist", "wake", "--clear-halted", "--reason", "x",
                 "--simulated-host-root", str(self.root)], root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 0, err)
        self.assertEqual(self._record()["state"], "halted")
        self.assertEqual(self._tombstones(), [])

    def test_pause_default_ttl_is_four_hours_and_record_shape(self):
        code, _out, err = self._pause("--reason", "quota crunch")
        self.assertEqual(code, 0, err)
        rec = self._record()
        from tools import seat_state
        self.assertEqual(seat_state.validate_record(rec), [])
        self.assertEqual(rec["state"], "paused-intake")
        self.assertEqual(rec["phase"], "set")
        self.assertFalse(rec["indefinite"])
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        delta = datetime.strptime(rec["expires"], fmt) - datetime.strptime(rec["set_ts"], fmt)
        self.assertEqual(delta, timedelta(hours=4))
        self.assertEqual(rec["set_by"], "operator:operator")
        self.assertEqual(rec["owner"], "operator")
        self.assertIsNone(rec["ticket"])
        self.assertFalse(rec["telegram_dark_ack"])
        self.assertFalse(rec["pre_expiry_notified"])
        self.assertEqual(rec["persona"], "specialist")
        self.assertEqual(rec["phase_ts"], rec["set_ts"])
        self.assertEqual(hs.seat_state_path("specialist", root=self.root).stat().st_mode & 0o777, 0o600)

    def test_pause_ledger_entry_id_matches_record_and_summary(self):
        code, _out, _err = self._pause("--reason", "quota crunch", "--ticket", "T-5524")
        self.assertEqual(code, 0)
        entries = _read_ledger_entries(self.ledger_dir)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["id"], self._record()["ledger_entry"])
        self.assertIn("harness-specialist-override-pause", entry["id"])
        self.assertEqual(entry["action"], "harness_override")
        self.assertIn("quota crunch", entry["summary"])
        self.assertIn("pause on specialist", entry["summary"])
        self.assertIn({"type": "persona", "ref": "specialist"}, entry["links"])
        self.assertEqual(self._record()["ticket"], "T-5524")

    def test_pause_for_parsing(self):
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        for text, want in (("30m", timedelta(minutes=30)), ("2d", timedelta(days=2)),
                           ("1d12h", timedelta(days=1, hours=12))):
            code, _out, err = self._pause("--for", text, "--reason", "x")
            self.assertEqual(code, 0, err)
            rec = self._record()
            got = datetime.strptime(rec["expires"], fmt) - datetime.strptime(rec["set_ts"], fmt)
            self.assertEqual(got, want, text)
            time.sleep(1.1)  # distinct ledger-id second

    def test_pause_for_bad_values_refused(self):
        for bad in ("abc", "0m", "4", "-1h", "1w", ""):
            code, _out, err = self._pause("--for", bad, "--reason", "x")
            self.assertEqual(code, 2, bad)
            self.assertIn("--for", err)
        self.assertIsNone(self._record())

    def test_huge_or_unicode_for_refused_cleanly(self):
        for bad in ("99999999999d", "9999999d", "\u0663h", "\u0664\u0660m"):
            code, _out, err = self._pause("--for", bad, "--reason", "x")
            self.assertEqual(code, 2, bad)
            self.assertIn("--for", err)
            self.assertNotIn("Traceback", err)
        self.assertIsNone(self._record())

    def test_pause_above_max_refused_without_indefinite(self):
        code, _out, err = self._pause("--for", "8d", "--reason", "x")
        self.assertEqual(code, 2)
        self.assertIn("--indefinite", err)
        self.assertIsNone(self._record())
        code, _out, err = self._pause("--for", "7d", "--reason", "x")
        self.assertEqual(code, 0, err)

    def test_pause_indefinite(self):
        code, _out, err = self._pause("--indefinite", "--reason", "x")
        self.assertEqual(code, 0, err)
        rec = self._record()
        self.assertIsNone(rec["expires"])
        self.assertTrue(rec["indefinite"])

    def test_for_and_indefinite_mutually_exclusive(self):
        code, _out, err = self._pause("--for", "1h", "--indefinite", "--reason", "x")
        self.assertEqual(code, 2)
        self.assertIn("--indefinite", err)

    def test_owner_and_ticket_validation(self):
        code, _out, _err = self._pause("--owner", "has space", "--reason", "x")
        self.assertEqual(code, 2)
        code, _out, _err = self._pause("--owner", "x" * 81, "--reason", "x")
        self.assertEqual(code, 2)
        code, _out, _err = self._pause("--ticket", "5524", "--reason", "x")
        self.assertEqual(code, 2)
        code, _out, err = self._pause("--owner", "role:head_engineering_orchestrator", "--reason", "x")
        self.assertEqual(code, 0, err)
        self.assertEqual(self._record()["owner"], "role:head_engineering_orchestrator")

    def test_default_owner_for_seat_operator_is_role(self):
        code, _out, err = _run_capture_stderr(
            ["--operator", "orchestrator", "specialist", "pause", "--reason", "x"],
            root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 0, err)
        self.assertEqual(self._record()["owner"], "role:head_engineering_orchestrator")
        self.assertEqual(self._record()["set_by"], "operator:orchestrator")

    def _op_run(self, operator, verb, *extra, persona="specialist"):
        # fresh ledger dir per run: same-second runs of one verb otherwise collide on the entry id
        self._op_n = getattr(self, "_op_n", 0) + 1
        return _run_capture_stderr(
            ["--operator", operator, persona, verb, *extra],
            root=self.root, ledger_dir=self.ledger_dir / f"op{self._op_n}")

    def test_operator_prefix_normalised_for_pause_sleep_wake(self):
        """T-5621: bare and pre-prefixed --operator give identical records."""
        for verb, extra in (("pause", ()), ("sleep", ("--for", "1h"))):
            recs = []
            for op in ("orchestrator", "operator:orchestrator"):
                hs.clear_seat_state("specialist", root=self.root)
                code, _o, err = self._op_run(op, verb, "--reason", "x", *extra)
                self.assertEqual(code, 0, err)
                rec = self._record()
                self.assertEqual(rec["set_by"], "operator:orchestrator", (verb, op))
                self.assertEqual(rec["owner"], "role:head_engineering_orchestrator", (verb, op))
                recs.append(rec)
            self.assertEqual(recs[0]["owner"], recs[1]["owner"])
        for op in ("orchestrator", "operator:orchestrator"):
            self._plant(owner="old", reason="old", expires="2099-01-01T00:00:00Z")
            code, _o, err = self._op_run(op, "wake", "--reason", "x")
            self.assertEqual(code, 0, err)
            self.assertEqual(self._tombstones()[-1]["ended_by"], "operator:orchestrator")

    def test_operator_bad_prefix_or_empty_refused(self):
        for bad in ("role:x", "operator:", "", "operator:role:x"):
            for verb in ("pause", "sleep", "wake"):
                code, _o, err = self._op_run(bad, verb, "--reason", "x")
                self.assertEqual(code, 2, (bad, verb))
                self.assertIn("--operator", err)
        self.assertIsNone(self._record())

    def test_pause_refresh_replaces_record(self):
        self._plant(owner="old", reason="old", expires="2099-01-01T00:00:00Z")
        code, out, err = self._pause("--owner", "newowner", "--reason", "new", "--for", "1h")
        self.assertEqual(code, 0, err)
        rec = self._record()
        self.assertEqual((rec["owner"], rec["reason"]), ("newowner", "new"))
        self.assertNotEqual(rec["set_ts"], "2026-01-01T00:00:00Z")
        self.assertIn("refreshed", out)

    def test_pause_and_wake_fail_cleanly_when_seat_state_lock_busy(self):
        """ORG-106 L5 (T-5529): pause/wake wait on the janitor's lock, then fail, changing nothing."""
        self._plant(owner="old", reason="old", expires="2099-01-01T00:00:00Z")
        with patch("tools.harness.SEAT_STATE_LOCK_TIMEOUT_S", 0.2), \
             hs.seat_state_lock("specialist", root=self.root):
            pcode, _o, perr = self._pause("--reason", "new")
            wcode, _o, werr = self._wake("--reason", "bye")
        self.assertNotEqual(pcode, 0)
        self.assertNotEqual(wcode, 0)
        self.assertIn("seat-state busy, retry", perr)
        self.assertIn("seat-state busy, retry", werr)
        self.assertEqual(self._record()["reason"], "old")
        self.assertEqual(self._tombstones(), [])

    def test_pause_refresh_tombstones_the_replaced_record(self):
        self._plant(owner="old", reason="old", expires="2099-01-01T00:00:00Z")
        code, _out, err = self._pause("--reason", "new")
        self.assertEqual(code, 0, err)
        rows = self._tombstones()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ended_by"], "replaced:operator:operator")
        self.assertEqual(rows[0]["reason"], "old")
        self.assertEqual(rows[0]["ended_ts"], self._record()["set_ts"])

    def test_pause_over_invalid_parseable_record_tombstones_it(self):
        self._plant(reason="")
        code, _out, err = self._pause("--reason", "new")
        self.assertEqual(code, 0, err)
        rows = self._tombstones()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ended_by"], "replaced:operator:operator")
        self.assertEqual(self._record()["reason"], "new")

    def test_first_pause_and_dry_run_leave_no_tombstone(self):
        self._pause("--reason", "first")
        self.assertEqual(self._tombstones(), [])
        self._plant(expires="2099-01-01T00:00:00Z")
        self._pause("--dry-run", "--reason", "x")
        self.assertEqual(self._tombstones(), [])

    def _plant_persona(self, persona, **over):
        doc = {
            "schema": hs.SCHEMA_SEAT_STATE, "persona": persona, "state": "deep-sleep",
            "phase": "asleep", "phase_ts": "2026-01-01T00:00:00Z", "set_ts": "2026-01-01T00:00:00Z",
            "set_by": "operator:x", "owner": "operator", "reason": "old", "ticket": None,
            "expires": "2099-01-01T00:00:00Z", "indefinite": False,
            "telegram_dark_ack": False, "pre_expiry_notified": False, "ledger_entry": "e",
        }
        doc.update(over)
        path = hs.seat_state_path(persona, root=self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc))

    def test_pause_over_deep_sleep_downgrades_to_paused_intake(self):
        # ORG-106 L3a, risk 8: tombstone replaced:operator:<op>, then a paused-intake record.
        self._plant(state="deep-sleep", phase="asleep", expires="2099-01-01T00:00:00Z")
        code, out, err = self._pause("--reason", "back to intake-only")
        self.assertEqual(code, 0, err)
        rec = self._record()
        self.assertEqual((rec["state"], rec["phase"]), ("paused-intake", "set"))
        rows = self._tombstones()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ended_by"], "replaced:operator:operator")
        self.assertEqual(rows[0]["state"], "deep-sleep")
        from tools import seat_sleep
        self.assertFalse(seat_sleep.launch_block("specialist", root=self.root).sleeping)
        self.assertIn("downgraded", out)

    def test_downgrade_clears_the_sysorch_flag_before_tombstoning(self):
        self._plant_persona("recovery")
        flag = self.hold_dir / "recovery"
        flag.write_text("seat-sleep\n")
        code, _out, err = _run_capture_stderr(
            ["--operator", "operator", "recovery", "pause", "--reason", "x"],
            root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 0, err)
        self.assertFalse(flag.exists())
        self.assertEqual(hs.load_seat_state("recovery", root=self.root)["state"], "paused-intake")

    def test_downgrade_flag_failure_refuses_and_leaves_the_record(self):
        self._plant_persona("recovery")
        before = hs.seat_state_path("recovery", root=self.root).read_text()
        with mock.patch("tools.seat_sleep.clear_sysorch_flag", side_effect=PermissionError("no")):
            code, _out, err = _run_capture_stderr(
                ["--operator", "operator", "recovery", "pause", "--reason", "x"],
                root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 2)
        self.assertIn("oom-hold flag", err)
        self.assertEqual(hs.seat_state_path("recovery", root=self.root).read_text(), before)
        self.assertEqual(hs.seat_state_tombstones_path("recovery", root=self.root).exists(), False)

    def test_wake_of_deep_sleep_clears_the_sysorch_flag_only_for_sysorch(self):
        self._plant_persona("recovery")
        (self.hold_dir / "recovery").write_text("x")
        (self.hold_dir / "specialist").write_text("operator manual hold, not ours")
        self._plant_persona("specialist")
        for persona in ("recovery", "specialist"):
            code, _out, err = _run_capture_stderr(
                ["--operator", "operator", persona, "wake", "--reason", "x"],
                root=self.root, ledger_dir=self.ledger_dir)
            self.assertEqual(code, 0, err)
        self.assertFalse((self.hold_dir / "recovery").exists())
        self.assertTrue((self.hold_dir / "specialist").exists(), "non-sysorch flags are never touched")

    def test_wake_flag_failure_refuses_and_keeps_the_record(self):
        self._plant_persona("recovery")
        with mock.patch("tools.seat_sleep.clear_sysorch_flag", side_effect=PermissionError("no")):
            code, _out, err = _run_capture_stderr(
                ["--operator", "operator", "recovery", "wake", "--reason", "x"],
                root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 2)
        self.assertIsNotNone(hs.load_seat_state("recovery", root=self.root))

    def test_wake_does_not_touch_a_paused_intake_sysorch_flag(self):
        self._plant_persona("recovery", state="paused-intake", phase="set")
        (self.hold_dir / "recovery").write_text("manual")
        code, _out, err = _run_capture_stderr(
            ["--operator", "operator", "recovery", "wake", "--reason", "x"],
            root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 0, err)
        self.assertTrue((self.hold_dir / "recovery").exists())

    def test_wake_does_not_clear_cap_cooldowns(self):
        self._plant(state="deep-sleep", phase="asleep", expires="2099-01-01T00:00:00Z")
        doc = {"schema": hs.SCHEMA_LAST_ATTEMPT, "outcome": "failed", "failure_class": "subscription_cap",
               "cooldown_until": "2099-01-01T00:00:00Z"}
        hs.write_last_attempt("specialist", "claude/primary", doc, root=self.root)
        code, _out, err = self._wake("--reason", "x")
        self.assertEqual(code, 0, err)
        self.assertEqual(hs.load_last_attempt("specialist", "claude/primary", root=self.root)["cooldown_until"],
                         "2099-01-01T00:00:00Z")

    def test_restart_refused_on_deep_sleep(self):
        self._plant(state="deep-sleep", phase="asleep", expires="2099-01-01T00:00:00Z")
        code, _out, err = _run_capture_stderr(
            ["--operator", "operator", "specialist", "restart", "--reason", "x"],
            root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 2)
        self.assertIn("wake", err)
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])

    def test_status_shows_the_deep_sleep_phase(self):
        self._plant(state="deep-sleep", phase="draining", expires="2099-01-01T00:00:00Z")
        code, out = _run(["specialist", "status"], root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 0)
        self.assertIn("phase: draining", out)

    def test_pause_dry_run_writes_nothing(self):
        code, out, err = self._pause("--dry-run", "--reason", "x")
        self.assertEqual(code, 0, err)
        self.assertIn("dry-run", out)
        self.assertIsNone(self._record())
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])

    def test_wake_dry_run_writes_nothing(self):
        self._plant(expires="2099-01-01T00:00:00Z")
        code, _out, _err = self._wake("--dry-run", "--reason", "x")
        self.assertEqual(code, 0)
        self.assertIsNotNone(self._record())
        self.assertEqual(self._tombstones(), [])
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])

    def test_wake_noop_without_record(self):
        code, out, err = self._wake("--reason", "x")
        self.assertEqual(code, 0, err)
        self.assertIn("nothing to wake", out)
        self.assertEqual(self._tombstones(), [])

    def test_wake_tombstones_and_removes(self):
        self._plant(expires="2099-01-01T00:00:00Z")
        code, _out, err = self._wake("--reason", "done")
        self.assertEqual(code, 0, err)
        self.assertIsNone(self._record())
        rows = self._tombstones()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ended_by"], "operator:operator")
        self.assertEqual(rows[0]["reason"], "old")
        self.assertRegex(rows[0]["ended_ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        entry = _read_ledger_entries(self.ledger_dir)[0]
        self.assertIn("harness-specialist-override-wake", entry["id"])
        self.assertIn("done", entry["summary"])

    def test_wake_expired_record_tombstoned_as_expiry(self):
        self._plant()  # expired in 2026-01
        code, out, err = self._wake("--reason", "x")
        self.assertEqual(code, 0, err)
        self.assertIsNone(self._record())
        self.assertEqual(self._tombstones()[0]["ended_by"], "expiry")
        self.assertIn("expired", out)

    def test_wake_clears_invalid_but_parseable_record(self):
        self._plant(reason="")
        code, _out, err = self._wake("--reason", "x")
        self.assertEqual(code, 0, err)
        self.assertIsNone(self._record())
        self.assertEqual(len(self._tombstones()), 1)

    def test_wake_refuses_unparseable_file_and_names_path(self):
        path = hs.seat_state_path("specialist", root=self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        code, _out, err = self._wake("--reason", "x")
        self.assertEqual(code, 2)
        self.assertIn(str(path), err)
        self.assertTrue(path.exists())
        self.assertEqual(_read_ledger_entries(self.ledger_dir), [])

    def test_status_shows_seat_state(self):
        out = _run(["specialist", "status"], root=self.root, ledger_dir=self.ledger_dir)[1]
        self.assertIn("seat state: active", out)
        self._pause("--owner", "operator", "--reason", "quota crunch")
        out = _run(["specialist", "status"], root=self.root, ledger_dir=self.ledger_dir)[1]
        self.assertIn("seat state: paused-intake", out)
        self.assertIn("owner: operator", out)
        self.assertIn("quota crunch", out)
        self.assertIn("left", out)
        self.assertIn("holds: none", out)

    def test_status_shows_fault_on_corrupt_record(self):
        path = hs.seat_state_path("specialist", root=self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        out = _run(["specialist", "status"], root=self.root, ledger_dir=self.ledger_dir)[1]
        self.assertIn("fault:", out)

    def test_seat_account_guard_applies(self):
        import os
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = harness.main(
                ["specialist", "pause", "--reason", "x"], root=self.root, ledger_dir=self.ledger_dir,
                euid_fn=lambda: os.geteuid(), account_fn=lambda persona: "someone-else-entirely")
        self.assertEqual(code, 2)
        self.assertIn("refusing", err.getvalue())
        self.assertIsNone(self._record())

    def test_no_sleep_verb(self):
        code, _out, _err = _run_capture_stderr(
            ["specialist", "sleep", "--reason", "x"], root=self.root, ledger_dir=self.ledger_dir)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
