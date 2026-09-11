"""Tests for notify_policy.py (ORG-076 §7 operator-plane mute control, T-2978).

Proves the load-bearing invariants: FAIL-OPEN (a broken/missing registry never
mutes → never silences an alert), idempotent mute/unmute, atomic round-trip, and
that is_muted keys on explicit presence only.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.notify_policy as np


class IsMuted(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.reg = Path(self._tmp.name) / "notify_mute.json"
        self.addCleanup(self._tmp.cleanup)

    def test_missing_file_is_not_muted_failopen(self):
        # No registry file at all → fail-open → not muted (alerts still fire).
        self.assertFalse(np.is_muted("orchestrator", self.reg))

    def test_malformed_file_is_not_muted_failopen(self):
        self.reg.write_text("{not valid json", encoding="utf-8")
        self.assertFalse(np.is_muted("orchestrator", self.reg))

    def test_wrong_shape_is_not_muted_failopen(self):
        # muted must be a dict; a list (or anything else) fails open.
        self.reg.write_text(json.dumps({"schema": np.SCHEMA, "muted": ["orchestrator"]}), encoding="utf-8")
        self.assertFalse(np.is_muted("orchestrator", self.reg))

    def test_explicitly_muted_is_true(self):
        self.reg.write_text(json.dumps({"schema": np.SCHEMA, "muted": {"mnemosyne": {"since": "x"}}}), encoding="utf-8")
        self.assertTrue(np.is_muted("mnemosyne", self.reg))
        self.assertFalse(np.is_muted("orchestrator", self.reg))  # a different persona is not muted

    def test_empty_persona_is_not_muted(self):
        self.reg.write_text(json.dumps({"schema": np.SCHEMA, "muted": {"": {}}}), encoding="utf-8")
        self.assertFalse(np.is_muted("", self.reg))


class MuteUnmute(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.reg = Path(self._tmp.name) / "notify_mute.json"
        self.addCleanup(self._tmp.cleanup)

    def test_mute_then_is_muted_roundtrip(self):
        self.assertTrue(np.mute("mnemosyne", by="test", reason="offline", registry=self.reg))
        self.assertTrue(np.is_muted("mnemosyne", self.reg))
        # metadata persisted + readable
        meta = np.list_muted(self.reg)["mnemosyne"]
        self.assertEqual(meta["by"], "test")
        self.assertEqual(meta["reason"], "offline")
        self.assertIn("since", meta)

    def test_mute_is_idempotent(self):
        self.assertTrue(np.mute("atlas", registry=self.reg))
        self.assertFalse(np.mute("atlas", registry=self.reg))  # already muted → False, no dup
        self.assertEqual(list(np.list_muted(self.reg)), ["atlas"])

    def test_unmute_removes_and_reports(self):
        np.mute("atlas", registry=self.reg)
        self.assertTrue(np.unmute("atlas", registry=self.reg))
        self.assertFalse(np.is_muted("atlas", self.reg))
        self.assertFalse(np.unmute("atlas", registry=self.reg))  # not muted → False

    def test_mute_creates_parent_dir(self):
        nested = Path(self._tmp.name) / "deep" / "notify_mute.json"
        self.assertTrue(np.mute("orchestrator", registry=nested))
        self.assertTrue(nested.exists())
        self.assertTrue(np.is_muted("orchestrator", nested))

    def test_atomic_write_is_group_readable(self):
        # T-2987: the shared cross-account registry must stay group-readable across
        # toggles (mkstemp creates 0600, writer-only). _atomic_write widens to 0640 so
        # every pantheon-mailbox alert-emitter reads the same source of truth.
        import os
        import stat
        np.mute("mnemosyne", registry=self.reg)
        mode = stat.S_IMODE(os.stat(self.reg).st_mode)
        self.assertTrue(mode & stat.S_IRGRP, f"registry not group-readable (mode {oct(mode)})")
        self.assertEqual(mode, 0o640, f"expected 0640, got {oct(mode)}")

    def test_written_registry_is_valid_schema(self):
        np.mute("orchestrator", registry=self.reg)
        data = json.loads(self.reg.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], np.SCHEMA)
        self.assertIn("orchestrator", data["muted"])

    def test_multiple_personas_independent(self):
        np.mute("mnemosyne", registry=self.reg)
        np.mute("the-ancestor", registry=self.reg)
        self.assertTrue(np.is_muted("mnemosyne", self.reg))
        self.assertTrue(np.is_muted("the-ancestor", self.reg))
        np.unmute("mnemosyne", self.reg)
        self.assertFalse(np.is_muted("mnemosyne", self.reg))
        self.assertTrue(np.is_muted("the-ancestor", self.reg))  # unrelated mute survives

    def test_empty_persona_rejected(self):
        with self.assertRaises(ValueError):
            np.mute("", registry=self.reg)


class SeatMaintenance(unittest.TestCase):
    """T-2984: maintenance suppresses remediation ACTIONS with AUTO-EXPIRY, distinct
    from mute (which suppresses alerts). Uses its own state-dir-backed registry."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.reg = Path(self._tmp.name) / "seat_maintenance.json"
        self.addCleanup(self._tmp.cleanup)

    def test_enter_then_in_maintenance_roundtrip(self):
        exp = np.enter_maintenance("specialist", minutes=30, by="test", reason="cutover", registry=self.reg)
        self.assertTrue(np.in_maintenance("specialist", self.reg))
        meta = np.list_maintenance(self.reg)["specialist"]
        self.assertEqual(meta["by"], "test")
        self.assertEqual(meta["reason"], "cutover")
        self.assertEqual(meta["expires"], exp)
        self.assertIn("since", meta)

    def test_exit_clears_and_reports(self):
        np.enter_maintenance("specialist", registry=self.reg)
        self.assertTrue(np.exit_maintenance("specialist", self.reg))
        self.assertFalse(np.in_maintenance("specialist", self.reg))
        self.assertFalse(np.exit_maintenance("specialist", self.reg))  # not present → False

    def test_auto_expiry_past_window_reads_false(self):
        # Arm a 10-min window anchored in the past → already expired at real 'now'.
        np.enter_maintenance("metis", minutes=10, registry=self.reg, now_iso="2020-01-01T00:00:00Z")
        self.assertFalse(np.in_maintenance("metis", self.reg))

    def test_active_window_within_and_after(self):
        np.enter_maintenance("atlas", minutes=10, registry=self.reg, now_iso="2030-01-01T00:00:00Z")
        self.assertTrue(np.in_maintenance("atlas", self.reg, now_iso="2030-01-01T00:05:00Z"))
        self.assertFalse(np.in_maintenance("atlas", self.reg, now_iso="2030-01-01T00:15:00Z"))

    def test_missing_expires_reads_false_failsafe(self):
        # A hand-written entry lacking `expires` must NOT leave a seat suppressed forever.
        self.reg.write_text(json.dumps({"schema": np.MAINTENANCE_SCHEMA,
                                        "maintenance": {"orchestrator": {"since": "x", "by": "y"}}}),
                            encoding="utf-8")
        self.assertFalse(np.in_maintenance("orchestrator", self.reg))

    def test_malformed_expires_reads_false_failsafe(self):
        self.reg.write_text(json.dumps({"schema": np.MAINTENANCE_SCHEMA,
                                        "maintenance": {"orchestrator": {"expires": "not-a-date"}}}),
                            encoding="utf-8")
        self.assertFalse(np.in_maintenance("orchestrator", self.reg))

    def test_missing_file_failopen(self):
        self.assertFalse(np.in_maintenance("specialist", self.reg))  # no file → not in maintenance

    def test_malformed_file_failopen(self):
        self.reg.write_text("{broken", encoding="utf-8")
        self.assertFalse(np.in_maintenance("specialist", self.reg))

    def test_wrong_shape_failopen(self):
        self.reg.write_text(json.dumps({"schema": np.MAINTENANCE_SCHEMA, "maintenance": ["specialist"]}),
                            encoding="utf-8")
        self.assertFalse(np.in_maintenance("specialist", self.reg))

    def test_minutes_clamped_to_max(self):
        np.enter_maintenance("specialist", minutes=99999, registry=self.reg)
        self.assertEqual(np.list_maintenance(self.reg)["specialist"]["minutes"], np.MAINTENANCE_MAX_MINUTES)

    def test_minutes_floored_to_one(self):
        np.enter_maintenance("specialist", minutes=0, registry=self.reg)
        self.assertEqual(np.list_maintenance(self.reg)["specialist"]["minutes"], 1)

    def test_reenter_rearms_window(self):
        first = np.enter_maintenance("specialist", minutes=30, registry=self.reg, now_iso="2030-01-01T00:00:00Z")
        second = np.enter_maintenance("specialist", minutes=30, registry=self.reg, now_iso="2030-01-01T00:20:00Z")
        self.assertGreater(second, first)  # expires moved forward

    def test_prune_drops_expired_on_write(self):
        # An expired entry for 'old' is pruned when a fresh entry for 'new' is written.
        np.enter_maintenance("old", minutes=1, registry=self.reg, now_iso="2020-01-01T00:00:00Z")
        np.enter_maintenance("new", minutes=30, registry=self.reg)  # now = real now → prunes 'old'
        entries = np.list_maintenance(self.reg)
        self.assertNotIn("old", entries)
        self.assertIn("new", entries)

    def test_empty_persona(self):
        self.assertFalse(np.in_maintenance("", self.reg))
        with self.assertRaises(ValueError):
            np.enter_maintenance("", registry=self.reg)
        with self.assertRaises(ValueError):
            np.exit_maintenance("", self.reg)

    def test_written_registry_is_valid_schema(self):
        np.enter_maintenance("specialist", registry=self.reg)
        data = json.loads(self.reg.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], np.MAINTENANCE_SCHEMA)
        self.assertIn("specialist", data["maintenance"])

    def test_multiple_seats_independent(self):
        np.enter_maintenance("specialist", registry=self.reg)
        np.enter_maintenance("orchestrator", registry=self.reg)
        self.assertTrue(np.in_maintenance("specialist", self.reg))
        self.assertTrue(np.in_maintenance("orchestrator", self.reg))
        np.exit_maintenance("specialist", self.reg)
        self.assertFalse(np.in_maintenance("specialist", self.reg))
        self.assertTrue(np.in_maintenance("orchestrator", self.reg))  # unrelated window survives

    def test_maintenance_and_mute_are_orthogonal(self):
        # Muting a persona does not put it in maintenance, and vice versa (separate registries).
        mute_reg = Path(self._tmp.name) / "notify_mute.json"
        np.mute("specialist", registry=mute_reg)
        self.assertFalse(np.in_maintenance("specialist", self.reg))
        np.enter_maintenance("orchestrator", registry=self.reg)
        self.assertFalse(np.is_muted("orchestrator", mute_reg))


if __name__ == "__main__":
    unittest.main()
