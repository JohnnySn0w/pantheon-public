"""Tests for notify_policy.py (ORG-076 §7 operator-plane mute control, T-2978).

Proves the load-bearing invariants: FAIL-OPEN (a broken/missing registry never
mutes → never silences an alert), idempotent mute/unmute, atomic round-trip, and
that is_muted keys on explicit presence only.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

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
        self.reg.write_text(json.dumps({"schema": np.SCHEMA, "muted": {"archivist": {"since": "x"}}}), encoding="utf-8")
        self.assertTrue(np.is_muted("archivist", self.reg))
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
        self.assertTrue(np.mute("archivist", by="test", reason="offline", registry=self.reg))
        self.assertTrue(np.is_muted("archivist", self.reg))
        # metadata persisted + readable
        meta = np.list_muted(self.reg)["archivist"]
        self.assertEqual(meta["by"], "test")
        self.assertEqual(meta["reason"], "offline")
        self.assertIn("since", meta)

    def test_mute_is_idempotent(self):
        self.assertTrue(np.mute("artisan", registry=self.reg))
        self.assertFalse(np.mute("artisan", registry=self.reg))  # already muted → False, no dup
        self.assertEqual(list(np.list_muted(self.reg)), ["artisan"])

    def test_unmute_removes_and_reports(self):
        np.mute("artisan", registry=self.reg)
        self.assertTrue(np.unmute("artisan", registry=self.reg))
        self.assertFalse(np.is_muted("artisan", self.reg))
        self.assertFalse(np.unmute("artisan", registry=self.reg))  # not muted → False

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
        np.mute("archivist", registry=self.reg)
        mode = stat.S_IMODE(os.stat(self.reg).st_mode)
        self.assertTrue(mode & stat.S_IRGRP, f"registry not group-readable (mode {oct(mode)})")
        self.assertEqual(mode, 0o640, f"expected 0640, got {oct(mode)}")

    def test_written_registry_is_valid_schema(self):
        np.mute("orchestrator", registry=self.reg)
        data = json.loads(self.reg.read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], np.SCHEMA)
        self.assertIn("orchestrator", data["muted"])

    def test_multiple_personas_independent(self):
        np.mute("archivist", registry=self.reg)
        np.mute("the-ancestor", registry=self.reg)
        self.assertTrue(np.is_muted("archivist", self.reg))
        self.assertTrue(np.is_muted("the-ancestor", self.reg))
        np.unmute("archivist", self.reg)
        self.assertFalse(np.is_muted("archivist", self.reg))
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
        np.enter_maintenance("mason", minutes=10, registry=self.reg, now_iso="2020-01-01T00:00:00Z")
        self.assertFalse(np.in_maintenance("mason", self.reg))

    def test_active_window_within_and_after(self):
        np.enter_maintenance("artisan", minutes=10, registry=self.reg, now_iso="2030-01-01T00:00:00Z")
        self.assertTrue(np.in_maintenance("artisan", self.reg, now_iso="2030-01-01T00:05:00Z"))
        self.assertFalse(np.in_maintenance("artisan", self.reg, now_iso="2030-01-01T00:15:00Z"))

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


class SharedMaintenanceRegistry(unittest.TestCase):
    """T-4150: the default registry is SHARED so an arm from any account reaches the
    monitors (which run as Recovery). Overrides keep precedence; an unprovisioned host
    falls back per-account and the CLI says so loudly."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.shared = self.tmp / "seat-maintenance"
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in ("PANTHEON_MAINTENANCE_REGISTRY", "PANTHEON_STATE_DIR"):
            os.environ.pop(key, None)
        shared = mock.patch.object(np, "SHARED_MAINTENANCE_DIR", self.shared)
        shared.start()
        self.addCleanup(shared.stop)
        home = mock.patch.object(np.Path, "home", return_value=self.tmp / "home")
        home.start()
        self.addCleanup(home.stop)

    def test_shared_dir_is_the_default_when_provisioned(self):
        self.shared.mkdir()
        self.assertEqual(np._maintenance_registry_path(), self.shared / "seat_maintenance.json")

    def test_unprovisioned_host_falls_back_per_account(self):
        self.assertEqual(np._maintenance_registry_path(),
                         self.tmp / "home" / ".local/state/pantheon/seat_maintenance.json")

    def test_overrides_keep_precedence_over_the_shared_dir(self):
        self.shared.mkdir()
        os.environ["PANTHEON_STATE_DIR"] = str(self.tmp / "state")
        self.assertEqual(np._maintenance_registry_path(), self.tmp / "state" / "seat_maintenance.json")
        os.environ["PANTHEON_MAINTENANCE_REGISTRY"] = str(self.tmp / "x.json")
        self.assertEqual(np._maintenance_registry_path(), self.tmp / "x.json")
        self.assertEqual(np._maintenance_registry_path(self.tmp / "y.json"), self.tmp / "y.json")

    def test_arm_written_by_one_account_is_read_by_the_default_reader(self):
        # The monitors call in_maintenance(persona) with NO registry: they must see
        # an arm the CLI wrote through the same default.
        self.shared.mkdir()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(np.main(["enter-maintenance", "recovery", "--minutes", "5"]), 0)
        self.assertTrue(np.in_maintenance("recovery"))
        self.assertTrue((self.shared / "seat_maintenance.json").exists())

    def test_cli_names_the_registry_and_warns_on_per_account_fallback(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(np.main(["enter-maintenance", "recovery", "--minutes", "5"]), 0)
        self.assertIn("registry:", err.getvalue())
        self.assertIn("PER-ACCOUNT", err.getvalue())
        self.shared.mkdir()
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(np.main(["exit-maintenance", "recovery"]), 0)
        self.assertNotIn("PER-ACCOUNT", err.getvalue())

    def test_cli_unwritable_registry_fails_loudly_not_with_a_traceback(self):
        err = io.StringIO()
        with mock.patch.object(np, "enter_maintenance", side_effect=PermissionError(13, "denied")), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(np.main(["enter-maintenance", "recovery"]), 1)
        self.assertIn("pantheon-shared", err.getvalue())


class RegistryPathFallback(unittest.TestCase):
    """T-4668: with PANTHEON_MUTE_REGISTRY unset and /opt absent, the registry is the
    health-plane file (18 alerting units never set the env and lost muting)."""

    def setUp(self):
        np._load_fault_logged = False
        self.addCleanup(setattr, np, "_load_fault_logged", False)

    def test_health_plane_registry_is_independent_of_retiring_account(self):
        self.assertEqual(
            np.HEALTH_PLANE_REGISTRY,
            "/var/lib/pantheon/notify-mute/notify_mute.json",
        )

    def test_recovery_units_share_the_canonical_registry(self):
        unit_dir = Path(__file__).resolve().parent / "systemd"
        expected = f"Environment=PANTHEON_MUTE_REGISTRY={np.HEALTH_PLANE_REGISTRY}"
        for name in (
            "pantheon-cap-mute-driver.service",
            "pantheon-channel-liveness.service",
            "pantheon-context-fill-monitor@.service",
            "pantheon-credential-expiry.service",
            "pantheon-mail-staleness.service",
        ):
            with self.subTest(unit=name):
                self.assertIn(expected, (unit_dir / name).read_text())

    def _env(self, value):
        env = {k: v for k, v in os.environ.items() if k != "PANTHEON_MUTE_REGISTRY"}
        if value is not None:
            env["PANTHEON_MUTE_REGISTRY"] = value
        return mock.patch.dict(os.environ, env, clear=True)

    def test_unset_env_and_absent_opt_falls_back_to_health_plane(self):
        with self._env(None), mock.patch.object(np.os.path, "exists", return_value=False):
            self.assertEqual(np._registry_path(), Path(np.HEALTH_PLANE_REGISTRY))

    def test_unset_env_and_present_opt_keeps_opt(self):
        with self._env(None), mock.patch.object(
                np.os.path, "exists", side_effect=lambda p: p == np.DEFAULT_REGISTRY):
            self.assertEqual(np._registry_path(), Path(np.DEFAULT_REGISTRY))

    def test_env_wins_over_both(self):
        with self._env("/tmp/x/notify_mute.json"):
            self.assertEqual(np._registry_path(), Path("/tmp/x/notify_mute.json"))

    def test_explicit_argument_wins(self):
        with self._env("/tmp/x/notify_mute.json"):
            self.assertEqual(np._registry_path("/tmp/y.json"), Path("/tmp/y.json"))

    def test_fallback_file_is_honoured_by_is_muted(self):
        with TemporaryDirectory() as td:
            reg = Path(td) / "notify_mute.json"
            reg.write_text(json.dumps({"schema": np.SCHEMA, "muted": {"artisan": {}}}))
            with self._env(None), mock.patch.object(np, "HEALTH_PLANE_REGISTRY", str(reg)), \
                 mock.patch.object(np.os.path, "exists", return_value=False):
                self.assertTrue(np.is_muted("artisan"))
                self.assertFalse(np.is_muted("recovery"))

    def test_unreadable_registry_fails_open_and_logs_once(self):
        err = io.StringIO()
        with self._env("/nonexistent/notify_mute.json"), contextlib.redirect_stderr(err):
            self.assertFalse(np.is_muted("artisan"))
            self.assertFalse(np.is_muted("mason"))
        self.assertEqual(err.getvalue().count("failing open"), 1)


class StrictWriteTests(unittest.TestCase):
    """T-5396: a write must never rebuild a registry it could not read. The fail-open
    load used to hand mute()/unmute()/enter_/exit_maintenance() an empty registry on any
    fault, and the write then replaced every existing entry with just its own."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.reg = Path(self._tmp.name) / "mute.json"
        self.mreg = Path(self._tmp.name) / "maintenance.json"

    def _seed(self):
        np.mute("specialist", by="t", reason="r", registry=self.reg)
        np.mute("orchestrator", by="t", reason="r", registry=self.reg)
        np.enter_maintenance("mason", minutes=60, registry=self.mreg)

    def test_malformed_registry_refuses_every_writer_and_is_left_untouched(self):
        self._seed()
        for path, write in ((self.reg, lambda: np.mute("recovery", registry=self.reg)),
                            (self.reg, lambda: np.unmute("specialist", registry=self.reg)),
                            (self.mreg, lambda: np.enter_maintenance("artisan", registry=self.mreg)),
                            (self.mreg, lambda: np.exit_maintenance("mason", registry=self.mreg))):
            good = path.read_text()
            path.write_text(good[:-5])              # a torn write: invalid JSON
            torn = path.read_text()
            with self.assertRaises(np.RegistryUnreadable):
                write()
            self.assertEqual(path.read_text(), torn, "the writer replaced a registry it could not read")
            path.write_text(good)

    def test_wrong_shape_refuses(self):
        self.reg.write_text('{"schema": "x", "muted": ["specialist"]}')
        with self.assertRaises(np.RegistryUnreadable):
            np.mute("recovery", registry=self.reg)

    def test_unreadable_registry_refuses(self):
        self._seed()
        if os.geteuid() == 0:
            self.skipTest("root reads through mode 000")
        self.reg.chmod(0)
        self.addCleanup(self.reg.chmod, 0o640)
        with self.assertRaises(np.RegistryUnreadable):
            np.mute("recovery", registry=self.reg)

    def test_absent_registry_is_created_by_the_first_write(self):
        self.assertTrue(np.mute("recovery", registry=self.reg))
        self.assertEqual(list(np.list_muted(self.reg)), ["recovery"])

    def test_reads_still_fail_open_on_the_same_fault(self):
        self._seed()
        self.reg.write_text("{not json")
        self.assertFalse(np.is_muted("specialist", registry=self.reg))

    def test_cli_mute_on_a_broken_registry_exits_3_and_changes_nothing(self):
        self._seed()
        self.reg.write_text("{torn")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = np.main(["--registry", str(self.reg), "mute", "recovery"])
        self.assertEqual(rc, 3)
        self.assertIn("REFUSED mute", err.getvalue())
        self.assertEqual(self.reg.read_text(), "{torn")


if __name__ == "__main__":
    unittest.main()
