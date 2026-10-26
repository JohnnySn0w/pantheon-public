"""Tests for tools.harness_state — durable-state schema helpers for T-2967 (BT4).

Schemas + layout per notes/2026-08-18-t2953-phase1-spec.md §Durable-state layout.
Every test uses a tempdir as `root`; nothing here touches the real
~/.local/state/pantheon tree.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools import harness_state as hs


class TestValidation(unittest.TestCase):
    def test_valid_persona_passes(self):
        self.assertEqual(hs.validate_persona("orchestrator"), "orchestrator")
        self.assertEqual(hs.validate_persona("specialist-2"), "specialist-2")

    def test_invalid_persona_rejected(self):
        for bad in ("", "Orchestrator", "../etc", "th oth", "orchestrator/x"):
            with self.assertRaises(hs.HarnessStateError):
                hs.validate_persona(bad)

    def test_valid_runtime_passes(self):
        self.assertEqual(hs.validate_runtime("claude/anthropic-code"), "claude/anthropic-code")
        self.assertEqual(hs.validate_runtime("codex/interactive-chatgpt"), "codex/interactive-chatgpt")

    def test_invalid_runtime_rejected(self):
        for bad in ("", "claude", "claude/", "/anthropic-code", "claude/anthropic/code", "../x/y"):
            with self.assertRaises(hs.HarnessStateError):
                hs.validate_runtime(bad)

    def test_runtime_slug_double_dash_rule(self):
        self.assertEqual(hs.runtime_slug("claude/anthropic-code"), "claude--anthropic-code")
        self.assertEqual(hs.runtime_slug("codex/interactive-chatgpt"), "codex--interactive-chatgpt")

    def test_runtime_slug_rejects_invalid_runtime(self):
        with self.assertRaises(hs.HarnessStateError):
            hs.runtime_slug("not-a-runtime")


class TestPathHelpers(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_persona_root_layout(self):
        self.assertEqual(
            hs.persona_root("orchestrator", root=self.root),
            self.root / "personas" / "orchestrator",
        )

    def test_active_runtime_path(self):
        self.assertEqual(
            hs.active_runtime_path("orchestrator", root=self.root),
            self.root / "personas" / "orchestrator" / "active-runtime.json",
        )

    def test_last_attempt_path_uses_slug(self):
        self.assertEqual(
            hs.last_attempt_path("orchestrator", "codex/interactive-chatgpt", root=self.root),
            self.root
            / "personas"
            / "orchestrator"
            / "runtimes"
            / "codex--interactive-chatgpt"
            / "last-attempt.json",
        )

    def test_holds_path(self):
        self.assertEqual(
            hs.holds_path("orchestrator", root=self.root),
            self.root / "personas" / "orchestrator" / "overrides" / "holds.json",
        )


class TestAtomicJsonRoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_then_read_back(self):
        path = hs.active_runtime_path("orchestrator", root=self.root)
        payload = {"schema": hs.SCHEMA_ACTIVE_RUNTIME, "runtime": "claude/anthropic-code"}
        hs._atomic_json(path, payload)
        self.assertTrue(path.is_file())
        loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded, payload)

    def test_write_creates_parent_dirs(self):
        path = hs.last_attempt_path("orchestrator", "codex/interactive-chatgpt", root=self.root)
        self.assertFalse(path.parent.is_dir())
        hs._atomic_json(path, {"schema": hs.SCHEMA_LAST_ATTEMPT})
        self.assertTrue(path.parent.is_dir())

    def test_write_leaves_no_temp_file_behind(self):
        path = hs.holds_path("orchestrator", root=self.root)
        hs._atomic_json(path, {"schema": hs.SCHEMA_HOLDS, "holds": {}})
        siblings = list(path.parent.iterdir())
        self.assertEqual(siblings, [path])

    def test_refuses_to_write_through_symlink(self):
        path = hs.holds_path("orchestrator", root=self.root)
        path.parent.mkdir(parents=True)
        real = path.parent / "real.json"
        real.write_text("{}", encoding="utf-8")
        path.symlink_to(real)
        with self.assertRaises(hs.HarnessStateError):
            hs._atomic_json(path, {"schema": hs.SCHEMA_HOLDS, "holds": {}})


class TestLoaders(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_active_runtime_missing_returns_none(self):
        self.assertIsNone(hs.load_active_runtime("orchestrator", root=self.root))

    def test_load_last_attempt_missing_returns_none(self):
        self.assertIsNone(hs.load_last_attempt("orchestrator", "claude/anthropic-code", root=self.root))

    def test_load_holds_missing_returns_default_shape(self):
        doc = hs.load_holds("orchestrator", root=self.root)
        self.assertEqual(doc, {"schema": hs.SCHEMA_HOLDS, "holds": {}})

    def test_load_holds_existing_file(self):
        path = hs.holds_path("orchestrator", root=self.root)
        hs._atomic_json(path, {"schema": hs.SCHEMA_HOLDS, "holds": {"codex/interactive-chatgpt": {"reason": "x"}}})
        doc = hs.load_holds("orchestrator", root=self.root)
        self.assertIn("codex/interactive-chatgpt", doc["holds"])

    def test_malformed_json_raises_clear_error(self):
        path = hs.active_runtime_path("orchestrator", root=self.root)
        path.parent.mkdir(parents=True)
        path.write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(hs.HarnessStateError):
            hs.load_active_runtime("orchestrator", root=self.root)

    def test_non_object_json_raises_clear_error(self):
        path = hs.active_runtime_path("orchestrator", root=self.root)
        path.parent.mkdir(parents=True)
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with self.assertRaises(hs.HarnessStateError):
            hs.load_active_runtime("orchestrator", root=self.root)


class TestListRuntimeRecords(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_runtimes_dir_returns_empty(self):
        self.assertEqual(hs.list_runtime_records("orchestrator", root=self.root), {})

    def test_enumerates_existing_records_by_runtime_field(self):
        hs.write_last_attempt(
            "orchestrator",
            "claude/anthropic-code",
            {"schema": hs.SCHEMA_LAST_ATTEMPT, "runtime": "claude/anthropic-code", "outcome": "success"},
            root=self.root,
        )
        hs.write_last_attempt(
            "orchestrator",
            "codex/interactive-chatgpt",
            {"schema": hs.SCHEMA_LAST_ATTEMPT, "runtime": "codex/interactive-chatgpt", "outcome": "failed"},
            root=self.root,
        )
        records = hs.list_runtime_records("orchestrator", root=self.root)
        self.assertEqual(set(records), {"claude/anthropic-code", "codex/interactive-chatgpt"})
        self.assertEqual(records["codex/interactive-chatgpt"]["outcome"], "failed")

    def test_ignores_slug_dirs_without_last_attempt_file(self):
        (self.root / "personas" / "orchestrator" / "runtimes" / "empty--slug").mkdir(parents=True)
        self.assertEqual(hs.list_runtime_records("orchestrator", root=self.root), {})


class TestHoldMutations(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_set_hold_on_empty_doc(self):
        doc = hs.set_hold(
            "orchestrator",
            "codex/interactive-chatgpt",
            reason="known broken",
            operator="operator",
            root=self.root,
            now_ts="2026-08-18T05:00:00Z",
        )
        self.assertEqual(
            doc["holds"]["codex/interactive-chatgpt"],
            {"set_ts": "2026-08-18T05:00:00Z", "set_by": "operator:operator", "reason": "known broken"},
        )

    def test_set_hold_preserves_other_holds(self):
        hs.write_holds(
            "orchestrator",
            {"schema": hs.SCHEMA_HOLDS, "holds": {"claude/anthropic-code": {"set_ts": "t", "set_by": "operator:x", "reason": "r"}}},
            root=self.root,
        )
        doc = hs.set_hold(
            "orchestrator", "codex/interactive-chatgpt", reason="r2", operator="operator", root=self.root, now_ts="t2"
        )
        self.assertIn("claude/anthropic-code", doc["holds"])
        self.assertIn("codex/interactive-chatgpt", doc["holds"])

    def test_clear_hold_removes_existing(self):
        hs.write_holds(
            "orchestrator",
            {"schema": hs.SCHEMA_HOLDS, "holds": {"codex/interactive-chatgpt": {"set_ts": "t", "set_by": "x", "reason": "r"}}},
            root=self.root,
        )
        doc, existed = hs.clear_hold("orchestrator", "codex/interactive-chatgpt", root=self.root)
        self.assertTrue(existed)
        self.assertNotIn("codex/interactive-chatgpt", doc["holds"])

    def test_clear_hold_missing_reports_false(self):
        doc, existed = hs.clear_hold("orchestrator", "codex/interactive-chatgpt", root=self.root)
        self.assertFalse(existed)
        self.assertEqual(doc["holds"], {})


class TestClearCooldown(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_clear_cooldown_on_missing_record_creates_placeholder(self):
        doc, had_cooldown = hs.clear_cooldown(
            "orchestrator", "codex/interactive-chatgpt", operator="operator", now_ts="t", root=self.root
        )
        self.assertFalse(had_cooldown)
        self.assertIsNone(doc["cooldown_until"])
        self.assertEqual(doc["runtime"], "codex/interactive-chatgpt")
        self.assertEqual(doc["provenance"], {"trigger": "manual-override", "operator": "operator"})

    def test_clear_cooldown_on_existing_record_nulls_cooldown_keeps_failures(self):
        hs.write_last_attempt(
            "orchestrator",
            "codex/interactive-chatgpt",
            {
                "schema": hs.SCHEMA_LAST_ATTEMPT,
                "runtime": "codex/interactive-chatgpt",
                "outcome": "failed",
                "failure_class": "subscription_cap",
                "cooldown_until": "2026-08-24T17:00:00Z",
                "consecutive_failures": 3,
                "provenance": {"trigger": "controller-selection", "operator": None},
            },
            root=self.root,
        )
        doc, had_cooldown = hs.clear_cooldown(
            "orchestrator", "codex/interactive-chatgpt", operator="operator", now_ts="t2", root=self.root
        )
        self.assertTrue(had_cooldown)
        self.assertIsNone(doc["cooldown_until"])
        self.assertEqual(doc["consecutive_failures"], 3)
        self.assertEqual(doc["provenance"], {"trigger": "manual-override", "operator": "operator"})

    def test_clear_cooldown_on_record_with_no_active_cooldown_reports_false(self):
        hs.write_last_attempt(
            "orchestrator",
            "claude/anthropic-code",
            {
                "schema": hs.SCHEMA_LAST_ATTEMPT,
                "runtime": "claude/anthropic-code",
                "outcome": "success",
                "cooldown_until": None,
                "consecutive_failures": 0,
                "provenance": {"trigger": "controller-selection", "operator": None},
            },
            root=self.root,
        )
        _doc, had_cooldown = hs.clear_cooldown(
            "orchestrator", "claude/anthropic-code", operator="operator", now_ts="t", root=self.root
        )
        self.assertFalse(had_cooldown)


class TestSeatStateHelpers(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_schema_constant(self):
        self.assertEqual(hs.SCHEMA_SEAT_STATE, "pantheon-seat-state/v1")

    def test_path_is_sibling_of_holds(self):
        p = hs.seat_state_path("specialist", root=self.root)
        self.assertEqual(p, self.root / "personas" / "specialist" / "overrides" / "seat-state.json")
        self.assertEqual(p.parent, hs.holds_path("specialist", root=self.root).parent)

    def test_path_rejects_bad_persona(self):
        with self.assertRaises(hs.HarnessStateError):
            hs.seat_state_path("../x", root=self.root)

    def test_load_missing_is_none(self):
        self.assertIsNone(hs.load_seat_state("specialist", root=self.root))

    def test_load_roundtrip_and_corrupt(self):
        p = hs.seat_state_path("specialist", root=self.root)
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"state": "paused-intake"}))
        self.assertEqual(hs.load_seat_state("specialist", root=self.root), {"state": "paused-intake"})
        p.write_text("{nope")
        with self.assertRaises(hs.HarnessStateError):
            hs.load_seat_state("specialist", root=self.root)


class TestSeatStateWriters(unittest.TestCase):
    """ORG-106 L1 (T-5524): write / clear / tombstone helpers."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _doc(self, **over):
        doc = {
            "schema": hs.SCHEMA_SEAT_STATE, "persona": "specialist", "state": "paused-intake",
            "phase": "set", "phase_ts": "2026-10-26T07:00:00Z", "set_ts": "2026-10-26T07:00:00Z",
            "set_by": "operator:orchestrator", "owner": "operator", "reason": "r", "ticket": None,
            "expires": "2026-10-26T11:00:00Z", "indefinite": False,
            "telegram_dark_ack": False, "pre_expiry_notified": False, "ledger_entry": "x",
        }
        doc.update(over)
        return doc

    def test_write_roundtrip_mode_0600(self):
        hs.write_seat_state("specialist", self._doc(), root=self.root)
        self.assertEqual(hs.load_seat_state("specialist", root=self.root)["reason"], "r")
        mode = hs.seat_state_path("specialist", root=self.root).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_invalid_record_refused_and_not_written(self):
        with self.assertRaises(hs.HarnessStateError):
            hs.write_seat_state("specialist", self._doc(reason=""), root=self.root)
        self.assertFalse(hs.seat_state_path("specialist", root=self.root).exists())

    def test_clear_reports_existence(self):
        self.assertFalse(hs.clear_seat_state("specialist", root=self.root))
        hs.write_seat_state("specialist", self._doc(), root=self.root)
        self.assertTrue(hs.clear_seat_state("specialist", root=self.root))
        self.assertIsNone(hs.load_seat_state("specialist", root=self.root))

    def test_tombstones_append_lines_mode_0600(self):
        hs.append_seat_tombstone("specialist", {"a": 1}, root=self.root)
        hs.append_seat_tombstone("specialist", {"a": 2}, root=self.root)
        path = hs.seat_state_tombstones_path("specialist", root=self.root)
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(rows, [{"a": 1}, {"a": 2}])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent, hs.seat_state_path("specialist", root=self.root).parent)


class TestSeatStateLock(unittest.TestCase):
    """ORG-106 L5 (T-5529): the shared per-seat lock."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_nonblocking_busy_raises_immediately(self):
        with hs.seat_state_lock("specialist", root=self.root):
            with self.assertRaises(hs.SeatStateBusy):
                with hs.seat_state_lock("specialist", root=self.root, blocking=False):
                    pass

    def test_blocking_times_out(self):
        with hs.seat_state_lock("specialist", root=self.root):
            with self.assertRaises(hs.SeatStateBusy):
                with hs.seat_state_lock("specialist", root=self.root, timeout_s=0.2):
                    pass

    def test_released_after_exit_and_mode_0600(self):
        with hs.seat_state_lock("specialist", root=self.root):
            pass
        with hs.seat_state_lock("specialist", root=self.root, blocking=False):
            pass
        self.assertEqual(hs.seat_state_lock_path("specialist", root=self.root).stat().st_mode & 0o777, 0o600)

    def test_other_seat_not_blocked(self):
        with hs.seat_state_lock("specialist", root=self.root):
            with hs.seat_state_lock("recovery", root=self.root, blocking=False):
                pass


if __name__ == "__main__":
    unittest.main()
