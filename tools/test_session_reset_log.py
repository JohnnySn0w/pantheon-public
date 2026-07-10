"""Unit tests for tools/session_reset_log.py — T-0256.

Run from repo root:
    python3 -m unittest tools.test_session_reset_log -v
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.session_reset_log as srl
from tools.session_reset_log import (
    CLASSES,
    SCHEMA,
    append_event,
    class_log_path,
    count_by_class,
    count_by_class_and_trigger,
    main,
    read_events,
    read_events_cross_account,
    session_resets_dir,
)


# ---------------------------------------------------------------------------
# Schema sanity
# ---------------------------------------------------------------------------


class TestSchema(unittest.TestCase):
    def test_schema_has_expected_fields(self):
        self.assertEqual(SCHEMA["version"], 1)
        self.assertEqual(set(SCHEMA["classes"]), set(CLASSES))
        self.assertIn("ts", SCHEMA["entry_fields"])
        self.assertIn("class", SCHEMA["entry_fields"])
        self.assertIn("trigger", SCHEMA["entry_fields"])

    def test_classes_constant(self):
        self.assertIn("cap_hit_auto_resume", CLASSES)
        self.assertIn("handoff_restart", CLASSES)
        self.assertIn("rate_limit_retry_inject", CLASSES)
        self.assertIn("service_restart_unattributed", CLASSES)


# ---------------------------------------------------------------------------
# append_event
# ---------------------------------------------------------------------------


class TestAppendEvent(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_appends_one_line_to_per_class_file(self):
        append_event("cap_hit_auto_resume", "ho_limit_hit", state_dir_root=self.tmp_path)
        path = self.tmp_path / "session_resets" / "cap_hit_auto_resume.jsonl"
        self.assertTrue(path.exists())
        lines = path.read_text().strip().split("\n")
        self.assertEqual(len(lines), 1)
        obj = json.loads(lines[0])
        self.assertEqual(obj["class"], "cap_hit_auto_resume")
        self.assertEqual(obj["trigger"], "ho_limit_hit")
        self.assertIsInstance(obj["ts"], int)
        self.assertTrue(obj["ts_iso"].endswith("Z"))
        self.assertIn("agent_account", obj)

    def test_appends_are_additive(self):
        for i in range(3):
            append_event(
                "handoff_restart", f"trigger_{i}", state_dir_root=self.tmp_path
            )
        path = self.tmp_path / "session_resets" / "handoff_restart.jsonl"
        lines = path.read_text().strip().split("\n")
        self.assertEqual(len(lines), 3)
        triggers = [json.loads(line)["trigger"] for line in lines]
        self.assertEqual(triggers, ["trigger_0", "trigger_1", "trigger_2"])

    def test_rejects_unknown_class(self):
        with self.assertRaises(ValueError):
            append_event("not_a_class", "x", state_dir_root=self.tmp_path)

    def test_extras_serialized_verbatim(self):
        append_event(
            "cap_hit_auto_resume",
            "trig",
            state_dir_root=self.tmp_path,
            session_id="sess-abc",
            resets_at=12345,
        )
        path = class_log_path("cap_hit_auto_resume", self.tmp_path)
        obj = json.loads(path.read_text().strip())
        self.assertEqual(obj["session_id"], "sess-abc")
        self.assertEqual(obj["resets_at"], 12345)

    def test_extras_skip_none_values(self):
        append_event(
            "cap_hit_auto_resume", "trig",
            state_dir_root=self.tmp_path,
            note=None,           # should be skipped
            session_id="kept",
        )
        path = class_log_path("cap_hit_auto_resume", self.tmp_path)
        obj = json.loads(path.read_text().strip())
        self.assertNotIn("note", obj)
        self.assertIn("session_id", obj)

    def test_explicit_ts_and_agent_overrides(self):
        append_event(
            "cap_hit_auto_resume", "trig",
            state_dir_root=self.tmp_path,
            ts=1700000000,
            agent_account="claude",
        )
        path = class_log_path("cap_hit_auto_resume", self.tmp_path)
        obj = json.loads(path.read_text().strip())
        self.assertEqual(obj["ts"], 1700000000)
        self.assertEqual(obj["agent_account"], "claude")


# ---------------------------------------------------------------------------
# read_events (same-account)
# ---------------------------------------------------------------------------


class TestReadEvents(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed(self, cls: str, ts: int, **extras) -> None:
        append_event(cls, extras.pop("trigger", "trig"), state_dir_root=self.tmp_path, ts=ts, **extras)

    def test_reads_back_what_was_written(self):
        self._seed("cap_hit_auto_resume", 1000)
        self._seed("handoff_restart", 2000)
        entries = read_events(state_dir_root=self.tmp_path)
        self.assertEqual(len(entries), 2)
        # Sorted by ts ascending.
        self.assertEqual(entries[0]["ts"], 1000)
        self.assertEqual(entries[1]["ts"], 2000)

    def test_filter_by_class(self):
        self._seed("cap_hit_auto_resume", 1000)
        self._seed("handoff_restart", 2000)
        entries = read_events(
            state_dir_root=self.tmp_path, classes=["cap_hit_auto_resume"]
        )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["class"], "cap_hit_auto_resume")

    def test_filter_by_since(self):
        self._seed("cap_hit_auto_resume", 1000)
        self._seed("cap_hit_auto_resume", 2000)
        self._seed("cap_hit_auto_resume", 3000)
        entries = read_events(state_dir_root=self.tmp_path, since_epoch=2000)
        self.assertEqual([e["ts"] for e in entries], [2000, 3000])

    def test_filter_by_until(self):
        self._seed("cap_hit_auto_resume", 1000)
        self._seed("cap_hit_auto_resume", 2000)
        self._seed("cap_hit_auto_resume", 3000)
        entries = read_events(state_dir_root=self.tmp_path, until_epoch=2000)
        self.assertEqual([e["ts"] for e in entries], [1000, 2000])

    def test_skips_malformed_lines(self):
        self._seed("cap_hit_auto_resume", 1000)
        path = class_log_path("cap_hit_auto_resume", self.tmp_path)
        # Manually append garbage + a valid entry.
        with path.open("a") as f:
            f.write("this is not json\n")
            f.write(json.dumps({"ts": 2000, "ts_iso": "2026-05-12T00:00:00Z",
                                "class": "cap_hit_auto_resume", "trigger": "x",
                                "agent_account": "claude"}) + "\n")
        entries = read_events(state_dir_root=self.tmp_path)
        # Garbage skipped, both valid entries returned.
        self.assertEqual(len(entries), 2)

    def test_returns_empty_when_no_files(self):
        entries = read_events(state_dir_root=self.tmp_path)
        self.assertEqual(entries, [])


# ---------------------------------------------------------------------------
# read_events_cross_account
# ---------------------------------------------------------------------------


class TestReadEventsCrossAccount(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_reads_own_account_directly(self):
        """When the caller's account is in `accounts`, read in-process."""
        with patch.dict(os.environ, {"PANTHEON_STATE_DIR": str(self.tmp_path)}):
            # Seed an entry as the current account.
            append_event("cap_hit_auto_resume", "x")
            with patch.object(srl, "_current_account", return_value="claude-fallback"), \
                 patch.object(srl, "_account_state_dir") as acct_dir:
                acct_dir.side_effect = lambda a: self.tmp_path if a == "claude-fallback" else Path("/nonexistent")
                entries = read_events_cross_account(accounts=["claude-fallback"])
                self.assertEqual(len(entries), 1)

    def test_reads_other_accounts_via_sudo(self):
        # Patch _current_account so the caller appears to be claude-fallback;
        # patch subprocess.run so the sudo cat returns a synthetic line for claude.
        fake_entry = {
            "ts": 1000,
            "ts_iso": "2026-05-12T00:00:00Z",
            "class": "cap_hit_auto_resume",
            "trigger": "ho_limit_hit",
            "agent_account": "claude",
        }
        with patch.object(srl, "_current_account", return_value="claude-fallback"), \
             patch.object(srl, "_account_state_dir", return_value=Path("/home/claude/.local/state/pantheon")), \
             patch.object(srl.subprocess, "run") as run:
            run.return_value = MagicMock(returncode=0, stdout=json.dumps(fake_entry) + "\n", stderr="")
            entries = read_events_cross_account(accounts=["claude"], classes=["cap_hit_auto_resume"])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["agent_account"], "claude")
        # T-0443: Confirm sudo arg shape — direct /bin/cat invocation
        # (NOT `bash -c "cat ..."`) so the kino sudoers fragment can match
        # it per-account + per-path-pattern.
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["sudo", "-nu", "claude"])
        self.assertEqual(argv[3], "/bin/cat")
        self.assertNotIn("bash", argv)
        self.assertNotIn("-c", argv)
        # The path arg ends with .jsonl under the canonical session_resets dir.
        self.assertTrue(argv[4].endswith("cap_hit_auto_resume.jsonl"))
        self.assertIn("session_resets", argv[4])

    def test_sudo_failure_returns_empty(self):
        with patch.object(srl, "_current_account", return_value="claude-fallback"), \
             patch.object(srl, "_account_state_dir", return_value=Path("/x")), \
             patch.object(srl.subprocess, "run") as run:
            run.return_value = MagicMock(returncode=1, stdout="", stderr="denied")
            entries = read_events_cross_account(accounts=["claude"])
        self.assertEqual(entries, [])

    def test_filters_apply_across_accounts(self):
        fake_old = {
            "ts": 100, "ts_iso": "2026-01-01T00:00:00Z",
            "class": "cap_hit_auto_resume", "trigger": "x", "agent_account": "claude",
        }
        fake_new = {
            "ts": 9999, "ts_iso": "2026-12-12T00:00:00Z",
            "class": "cap_hit_auto_resume", "trigger": "y", "agent_account": "claude",
        }
        with patch.object(srl, "_current_account", return_value="claude-fallback"), \
             patch.object(srl, "_account_state_dir", return_value=Path("/x")), \
             patch.object(srl.subprocess, "run") as run:
            run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(fake_old) + "\n" + json.dumps(fake_new) + "\n",
                stderr="",
            )
            entries = read_events_cross_account(
                accounts=["claude"], classes=["cap_hit_auto_resume"], since_epoch=1000
            )
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["ts"], 9999)


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


class TestAggregation(unittest.TestCase):
    def test_count_by_class(self):
        entries = [
            {"class": "cap_hit_auto_resume", "trigger": "a"},
            {"class": "cap_hit_auto_resume", "trigger": "b"},
            {"class": "handoff_restart", "trigger": "c"},
            {"class": "bogus", "trigger": "d"},  # unknown — skipped
        ]
        counts = count_by_class(entries)
        self.assertEqual(counts["cap_hit_auto_resume"], 2)
        self.assertEqual(counts["handoff_restart"], 1)
        self.assertEqual(counts["rate_limit_retry_inject"], 0)
        # bogus class doesn't show up in counts at all.
        self.assertNotIn("bogus", counts)

    def test_count_by_class_and_trigger(self):
        entries = [
            {"class": "cap_hit_auto_resume", "trigger": "a"},
            {"class": "cap_hit_auto_resume", "trigger": "a"},
            {"class": "cap_hit_auto_resume", "trigger": "b"},
            {"class": "handoff_restart", "trigger": "c"},
        ]
        out = count_by_class_and_trigger(entries)
        self.assertEqual(out[("cap_hit_auto_resume", "a")], 2)
        self.assertEqual(out[("cap_hit_auto_resume", "b")], 1)
        self.assertEqual(out[("handoff_restart", "c")], 1)


# ---------------------------------------------------------------------------
# CLI — emit subcommand
# ---------------------------------------------------------------------------


class TestCliEmit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        os.environ["PANTHEON_STATE_DIR"] = str(self.tmp_path)

    def tearDown(self) -> None:
        os.environ.pop("PANTHEON_STATE_DIR", None)
        self.tmp.cleanup()

    def test_emit_writes_entry(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["emit", "--class", "handoff_restart", "--trigger", "operator_initiated"])
        self.assertEqual(rc, 0)
        path = self.tmp_path / "session_resets" / "handoff_restart.jsonl"
        self.assertTrue(path.exists())

    def test_emit_with_extras(self):
        with redirect_stdout(io.StringIO()):
            rc = main([
                "emit", "--class", "handoff_restart", "--trigger", "operator_initiated",
                "--note", "smoke test",
                "--extras", "service=claude-telegram.service", "mode=user",
            ])
        self.assertEqual(rc, 0)
        path = self.tmp_path / "session_resets" / "handoff_restart.jsonl"
        obj = json.loads(path.read_text().strip())
        self.assertEqual(obj["note"], "smoke test")
        self.assertEqual(obj["service"], "claude-telegram.service")
        self.assertEqual(obj["mode"], "user")

    def test_emit_rejects_unknown_class(self):
        # argparse exits 2 on choices=... violation.
        rc = main(["emit", "--class", "bogus", "--trigger", "x"])
        self.assertEqual(rc, 2)

    def test_emit_skips_malformed_extras(self):
        with redirect_stdout(io.StringIO()):
            rc = main([
                "emit", "--class", "handoff_restart", "--trigger", "operator_initiated",
                "--extras", "noequals", "valid=ok",
            ])
        self.assertEqual(rc, 0)
        path = self.tmp_path / "session_resets" / "handoff_restart.jsonl"
        obj = json.loads(path.read_text().strip())
        self.assertEqual(obj.get("valid"), "ok")
        self.assertNotIn("noequals", obj)


if __name__ == "__main__":
    unittest.main()
