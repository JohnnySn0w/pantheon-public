"""Unit tests for the neutral typed user-manager port (T-2917 5c.1).

Covers the text-form and native typed value converters, each pure
classifier, the IS_ENABLED exit-0 table, the elapse sentinels, the evidence
digest key helper, and the AST cleanliness of tools/user_systemd.py (no
process-spawning import, no transport-specific literal), which a 5c.2 guard
relies on.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import json
import unittest
from pathlib import Path

from tools import user_systemd as us
from tools.user_systemd import (
    Elapse,
    ExitPolicy,
    NonCanonical,
    ReadPurpose,
    SnapshotSource,
    UnitSnapshot,
    UserManagerProtocolError,
)

MODULE_PATH = Path(__file__).resolve().parent / "user_systemd.py"
TEXT_SOURCE = SnapshotSource("cli", "cli-show-stdout", 0, "")
VALID_ID = "0123456789abcdef0123456789abcdef"


def _text(purpose: ReadPurpose, **values: str) -> UnitSnapshot:
    return us.unit_snapshot_from_text_values("u.service", purpose, values, source=TEXT_SOURCE)


def _native(purpose: ReadPurpose, **values: object) -> UnitSnapshot:
    return us.unit_snapshot_from_native("u.service", purpose, values, transport="dbus")


class ReadPurposeTests(unittest.TestCase):
    def test_property_tuples_are_exact_historical_order(self) -> None:
        expected = {
            ReadPurpose.STALE_RESET: (
                "LoadState", "ActiveState", "SubState", "Result", "ExecMainStatus", "InvocationID", "Job",
            ),
            ReadPurpose.UNIT_FILE_ACTIVE: ("LoadState", "UnitFileState", "ActiveState", "SubState"),
            ReadPurpose.UNIT_FIRE_POLL: (
                "InvocationID", "ActiveState", "SubState", "Result", "ExecMainStatus", "SuccessExitStatus", "Job",
            ),
            ReadPurpose.SERVICE_SETTLE: (
                "InvocationID", "ActiveState", "Result", "ExecMainStatus", "SuccessExitStatus", "Job",
            ),
            ReadPurpose.SERVICE_OBSERVE: (
                "InvocationID", "ActiveState", "Result", "ExecMainStatus", "SuccessExitStatus",
            ),
            ReadPurpose.TIMER_ELAPSE: (
                "ActiveState", "SubState", "NextElapseUSecRealtime", "NextElapseUSecMonotonic",
            ),
            ReadPurpose.RECEIVER_ACTIVE: ("ActiveState", "MainPID"),
            ReadPurpose.RELOAD_STATE: ("NeedDaemonReload",),
            ReadPurpose.SERVICE_BRINGUP_OBSERVE: ("ActiveState", "SubState", "MainPID", "NRestarts"),
        }
        self.assertEqual({purpose: purpose.properties for purpose in ReadPurpose}, expected)

    def test_purposes_are_a_closed_set_of_distinct_members(self) -> None:
        self.assertEqual(len(list(ReadPurpose)), 9)
        self.assertEqual(len({purpose.properties for purpose in ReadPurpose}), 9)


class TextConverterTests(unittest.TestCase):
    def test_exec_status(self) -> None:
        self.assertEqual(us.exec_status_from_text("0"), 0)
        self.assertEqual(us.exec_status_from_text("5"), 5)
        for raw in ("", "007", "+5", " 5", "-1", "1_0", "٥", "²", "garbage"):
            with self.subTest(raw=raw):
                self.assertEqual(us.exec_status_from_text(raw), NonCanonical(raw))
                self.assertEqual(us.render_exec_status(us.exec_status_from_text(raw)), raw)

    def test_job(self) -> None:
        self.assertEqual(us.job_from_text(""), 0)
        self.assertEqual(us.job_from_text("17"), 17)
        self.assertEqual(us.job_from_text("0"), NonCanonical("0"))
        self.assertEqual(us.job_from_text(" 1"), NonCanonical(" 1"))
        for raw in ("", "17", "0", " 1", "007"):
            with self.subTest(raw=raw):
                self.assertEqual(us.render_job(us.job_from_text(raw)), raw)
        self.assertEqual(us.render_job(None), "")

    def test_main_pid(self) -> None:
        self.assertEqual(us.main_pid_from_text("0"), 0)
        self.assertEqual(us.main_pid_from_text("123"), 123)
        self.assertEqual(us.main_pid_from_text(""), NonCanonical(""))
        self.assertEqual(us.main_pid_from_text("00"), NonCanonical("00"))
        self.assertEqual(us.render_main_pid(None), "")

    def test_exit_policy(self) -> None:
        self.assertEqual(us.exit_policy_from_text(""), ExitPolicy())
        self.assertEqual(us.exit_policy_from_text("3 4 5"), ExitPolicy((3, 4, 5), ()))
        self.assertEqual(us.exit_policy_from_text("3 4 5 SIGTERM"), ExitPolicy((3, 4, 5), ("SIGTERM",)))
        for raw in ("SIGTERM 3", "03 4", " 3", "3  4", "3 4 ", "1_0"):
            with self.subTest(raw=raw):
                self.assertEqual(us.exit_policy_from_text(raw), NonCanonical(raw))
        for raw in ("", "3 4 5", "3 4 5 SIGTERM", "SIGTERM 3", "03 4"):
            with self.subTest(round_trip=raw):
                self.assertEqual(us.render_success_exit(us.exit_policy_from_text(raw)), raw)

    def test_elapse_text_sentinels(self) -> None:
        for raw in ("", "0", "n/a", " N/A ", "  "):
            with self.subTest(raw=raw):
                self.assertEqual(us.elapse_from_text(raw), Elapse("unset"))
        self.assertEqual(us.elapse_from_text("infinity"), Elapse("infinity"))
        self.assertEqual(us.elapse_from_text(" Infinity"), Elapse("infinity"))
        self.assertEqual(us.elapse_from_text("12345"), Elapse("finite", 12345))
        self.assertEqual(us.elapse_from_text("Sat 2026-08-15 04:00:00 UTC"), Elapse("finite"))

    def test_n_restarts_text(self) -> None:
        for raw, want in (("0", 0), ("7", 7)):
            self.assertEqual(us.n_restarts_from_text(raw), want)
        for raw in ("", "04", "-1", "x", " 1", "1 "):
            with self.subTest(raw=raw):
                self.assertEqual(us.n_restarts_from_text(raw), NonCanonical(raw))

    def test_bringup_observe_snapshot_text_and_native(self) -> None:
        purpose = ReadPurpose.SERVICE_BRINGUP_OBSERVE
        text = _text(purpose, ActiveState="active", SubState="running", MainPID="42", NRestarts="3")
        native = _native(purpose, ActiveState="active", SubState="running", MainPID=42, NRestarts=3)
        self.assertEqual((text.main_pid, text.n_restarts), (42, 3))
        self.assertEqual((native.main_pid, native.n_restarts), (42, 3))
        self.assertEqual(text.present, native.present)
        for bad in ("3", True, -1, None):
            with self.subTest(bad=bad):
                with self.assertRaises(UserManagerProtocolError):
                    _native(purpose, NRestarts=bad)

    def test_bool(self) -> None:
        self.assertIs(us.bool_from_text("yes"), True)
        self.assertIs(us.bool_from_text("no"), False)
        self.assertEqual(us.bool_from_text(""), NonCanonical(""))

    def test_text_values_reject_properties_outside_purpose(self) -> None:
        with self.assertRaises(UserManagerProtocolError) as raised:
            _text(ReadPurpose.RECEIVER_ACTIVE, ActiveState="inactive", Job="")
        self.assertEqual(raised.exception.reason, "contains unexpected keys")

    def test_text_snapshot_fields_and_present(self) -> None:
        snapshot = _text(ReadPurpose.RECEIVER_ACTIVE, ActiveState="inactive")
        self.assertEqual(snapshot.present, frozenset({"ActiveState"}))
        self.assertEqual(snapshot.active_state, "inactive")
        self.assertIsNone(snapshot.main_pid)
        self.assertIsNone(snapshot.load_state)


class NativeConverterTests(unittest.TestCase):
    def test_invocation_id(self) -> None:
        raw = bytes.fromhex(VALID_ID)
        self.assertEqual(_native(ReadPurpose.UNIT_FIRE_POLL, InvocationID=raw).invocation_id, VALID_ID)
        self.assertEqual(_native(ReadPurpose.UNIT_FIRE_POLL, InvocationID=b"").invocation_id, "")
        self.assertEqual(_native(ReadPurpose.UNIT_FIRE_POLL, InvocationID=b"\x00" * 16).invocation_id, "")
        self.assertTrue(_native(ReadPurpose.UNIT_FIRE_POLL, InvocationID=raw).invocation_id_valid)
        for bad in (b"\x01" * 15, b"\x01" * 17, VALID_ID):
            with self.subTest(bad=bad), self.assertRaises(UserManagerProtocolError):
                _native(ReadPurpose.UNIT_FIRE_POLL, InvocationID=bad)

    def test_elapse_sentinels(self) -> None:
        self.assertEqual(_native(ReadPurpose.TIMER_ELAPSE, NextElapseUSecRealtime=0).next_elapse_realtime, Elapse("unset"))
        self.assertEqual(
            _native(ReadPurpose.TIMER_ELAPSE, NextElapseUSecRealtime=2**64 - 1).next_elapse_realtime,
            Elapse("infinity"),
        )
        self.assertEqual(
            _native(ReadPurpose.TIMER_ELAPSE, NextElapseUSecMonotonic=7).next_elapse_monotonic,
            Elapse("finite", 7),
        )
        for bad in (-1, 2**64, "12", True):
            with self.subTest(bad=bad), self.assertRaises(UserManagerProtocolError):
                _native(ReadPurpose.TIMER_ELAPSE, NextElapseUSecRealtime=bad)

    def test_job(self) -> None:
        self.assertEqual(_native(ReadPurpose.STALE_RESET, Job=(0, "/")).job_id, 0)
        self.assertEqual(_native(ReadPurpose.STALE_RESET, Job=(9, "/org/freedesktop/systemd1/job/9")).job_id, 9)
        for bad in (9, (9,), ("9", "/"), (9, 9), (-1, "/")):
            with self.subTest(bad=bad), self.assertRaises(UserManagerProtocolError):
                _native(ReadPurpose.STALE_RESET, Job=bad)

    def test_success_exit_status(self) -> None:
        snap = _native(ReadPurpose.UNIT_FIRE_POLL, SuccessExitStatus=([3, 4, 5], [15]))
        self.assertEqual(snap.success_exit, ExitPolicy((3, 4, 5), ("SIGTERM",)))
        snap = _native(ReadPurpose.UNIT_FIRE_POLL, SuccessExitStatus=([], ["SIGHUP"]))
        self.assertEqual(snap.success_exit, ExitPolicy((), ("SIGHUP",)))
        for bad in ([3], ([3], [15], []), (["3"], []), ([True], [])):
            with self.subTest(bad=bad), self.assertRaises(UserManagerProtocolError):
                _native(ReadPurpose.UNIT_FIRE_POLL, SuccessExitStatus=bad)

    def test_scalar_types_are_checked(self) -> None:
        with self.assertRaises(UserManagerProtocolError):
            _native(ReadPurpose.UNIT_FIRE_POLL, ActiveState=1)
        with self.assertRaises(UserManagerProtocolError):
            _native(ReadPurpose.UNIT_FIRE_POLL, ExecMainStatus="0")
        with self.assertRaises(UserManagerProtocolError):
            _native(ReadPurpose.UNIT_FIRE_POLL, ExecMainStatus=False)
        with self.assertRaises(UserManagerProtocolError):
            _native(ReadPurpose.RELOAD_STATE, NeedDaemonReload=1)
        self.assertIs(_native(ReadPurpose.RELOAD_STATE, NeedDaemonReload=True).need_daemon_reload, True)

    def test_unexpected_property_is_a_protocol_error(self) -> None:
        with self.assertRaises(UserManagerProtocolError):
            _native(ReadPurpose.RECEIVER_ACTIVE, ActiveState="inactive", Job=(0, "/"))

    def test_digest_is_typed_canonical_json(self) -> None:
        snap = _native(ReadPurpose.RECEIVER_ACTIVE, ActiveState="inactive", MainPID=0)
        expected = json.dumps(
            {"properties": {"ActiveState": "inactive", "MainPID": 0}, "purpose": "receiver-active", "unit": "u.service"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(snap.source.encoding, us.TYPED_CANONICAL_JSON)
        self.assertEqual(snap.source.transport, "dbus")
        self.assertEqual(snap.source.length, len(expected))
        self.assertEqual(snap.source.sha256, hashlib.sha256(expected).hexdigest())
        self.assertEqual(snap.present, frozenset({"ActiveState", "MainPID"}))

    def test_environment_files_and_enablement(self) -> None:
        self.assertFalse(us.environment_file_declared_from_native([]))
        self.assertTrue(us.environment_file_declared_from_native([("/etc/x", False)]))
        with self.assertRaises(UserManagerProtocolError):
            us.environment_file_declared_from_native([("/etc/x",)])
        self.assertEqual(us.unit_file_enablement_from_state("enabled"), us.UnitFileEnablement("enabled", True))
        self.assertEqual(us.unit_file_enablement_from_state("disabled"), us.UnitFileEnablement("disabled", False))


class IsEnabledTableTests(unittest.TestCase):
    def test_exit_zero_states_are_the_documented_set(self) -> None:
        self.assertEqual(
            us.IS_ENABLED_ZERO_EXIT_STATES,
            frozenset({"enabled", "enabled-runtime", "static", "alias", "indirect", "generated", "transient"}),
        )
        for state in ("disabled", "masked", "masked-runtime", "linked", "linked-runtime", "bad", "not-found", ""):
            with self.subTest(state=state):
                self.assertFalse(us.unit_file_enablement_from_state(state).enabled)


class ClassifierTests(unittest.TestCase):
    def stale(self, **overrides: str) -> UnitSnapshot:
        values = {
            "LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead", "Result": "success",
            "ExecMainStatus": "0", "InvocationID": "", "Job": "",
        }
        values.update(overrides)
        return _text(ReadPurpose.STALE_RESET, **values)

    def test_stale_reset(self) -> None:
        self.assertEqual(us.classify_stale_reset(self.stale()), "clean-noop")
        reset = self.stale(ActiveState="failed", SubState="failed", Result="exit-code", ExecMainStatus="1", InvocationID=VALID_ID)
        self.assertEqual(us.classify_stale_reset(reset), "reset-required")
        with self.assertRaisesRegex(us.UnitStateRefused, "^pre-existing loaded service$"):
            us.classify_stale_reset(self.stale(LoadState="loaded"))
        for overrides in (
            {"ExecMainStatus": "00"},
            {"Job": "0"},
            {"InvocationID": VALID_ID},
            {"ActiveState": "failed", "SubState": "failed", "Result": "exit-code", "ExecMainStatus": "01", "InvocationID": VALID_ID},
            {"ActiveState": "failed", "SubState": "failed", "Result": "exit-code", "ExecMainStatus": "1", "InvocationID": "bad"},
        ):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(us.UnitStateRefused, "^unsupported stale unit state$"):
                us.classify_stale_reset(self.stale(**overrides))

    def test_stale_reset_keeps_historical_crash_on_superscript_digit(self) -> None:
        crash = self.stale(ActiveState="failed", SubState="failed", Result="exit-code", ExecMainStatus="²", InvocationID=VALID_ID)
        with self.assertRaises(ValueError):
            us.classify_stale_reset(crash)

    def timer(self, load: str, unit_file: str, active: str = "inactive", sub: str = "dead") -> UnitSnapshot:
        return _text(ReadPurpose.UNIT_FILE_ACTIVE, LoadState=load, UnitFileState=unit_file, ActiveState=active, SubState=sub)

    def test_timer_unit_file(self) -> None:
        self.assertEqual(us.classify_timer_unit_file(self.timer("loaded", "enabled")), "enabled")
        self.assertEqual(us.classify_timer_unit_file(self.timer("loaded", "disabled")), "disabled")
        self.assertEqual(us.classify_timer_unit_file(self.timer("not-found", "")), "not-found")
        self.assertEqual(us.classify_timer_unit_file(self.timer("not-found", "not-found")), "not-found")
        for load, unit_file in (("loaded", "static"), ("masked", "masked"), ("not-found", "disabled")):
            with self.subTest(load=load, unit_file=unit_file), self.assertRaisesRegex(
                us.UnitStateRefused, "^timer show output has unsupported load/unit-file state$"
            ):
                us.classify_timer_unit_file(self.timer(load, unit_file))

    def test_unit_file_presence_and_proofs(self) -> None:
        self.assertEqual(us.classify_unit_file_presence(self.timer("not-found", "x")), "not-found")
        self.assertEqual(us.classify_unit_file_presence(self.timer("loaded", "enabled")), "enabled")
        self.assertEqual(us.classify_unit_file_presence(self.timer("masked", "static")), "disabled")
        self.assertTrue(us.proves_inactive_dead(self.timer("loaded", "enabled")))
        self.assertFalse(us.proves_inactive_dead(self.timer("loaded", "enabled", active="active", sub="waiting")))
        self.assertTrue(us.proves_notfound_inactive_dead(self.timer("not-found", "")))
        self.assertFalse(us.proves_notfound_inactive_dead(self.timer("loaded", "")))
        self.assertFalse(us.proves_notfound_inactive_dead(self.timer("not-found", "disabled")))

    def test_exit_status_within_policy(self) -> None:
        f = us.exit_status_within_policy
        self.assertTrue(f(0, None))
        self.assertTrue(f(5, ExitPolicy((3, 4, 5))))
        self.assertFalse(f(1, ExitPolicy((3, 4, 5), ("SIGTERM",))))
        self.assertFalse(f(5, ExitPolicy()))
        self.assertFalse(f(None, ExitPolicy((5,))))
        self.assertFalse(f(NonCanonical(""), ExitPolicy((5,))))
        self.assertTrue(f(NonCanonical("05"), ExitPolicy((5,))))  # historical int() leniency
        self.assertTrue(f(5, NonCanonical("03 5")))
        self.assertFalse(f(NonCanonical("garbage"), ExitPolicy((5,))))

    def poll(self, **overrides: str) -> UnitSnapshot:
        values = {
            "InvocationID": "", "ActiveState": "inactive", "SubState": "dead", "Result": "success",
            "ExecMainStatus": "0", "SuccessExitStatus": "3 4 5", "Job": "",
        }
        values.update(overrides)
        return _text(ReadPurpose.UNIT_FIRE_POLL, **values)

    def test_unit_fire_has_failed(self) -> None:
        self.assertFalse(us.unit_fire_has_failed(self.poll()))
        self.assertFalse(us.unit_fire_has_failed(self.poll(ExecMainStatus="5")))
        self.assertFalse(us.unit_fire_has_failed(self.poll(ExecMainStatus="")))
        self.assertTrue(us.unit_fire_has_failed(self.poll(ExecMainStatus="1")))
        self.assertTrue(us.unit_fire_has_failed(self.poll(ExecMainStatus="5", SuccessExitStatus="")))
        self.assertTrue(us.unit_fire_has_failed(self.poll(Result="exit-code", ExecMainStatus="1")))
        self.assertTrue(us.unit_fire_has_failed(self.poll(ActiveState="failed")))
        self.assertTrue(us.unit_fire_has_failed(self.poll(Result="signal")))

    def test_unit_fire_idle_before_enqueue(self) -> None:
        self.assertTrue(us.unit_fire_idle_before_enqueue(self.poll()))
        self.assertTrue(us.unit_fire_idle_before_enqueue(self.poll(ActiveState="failed", Job="0")))
        for job in ("n/a", "none", "None", " "):
            with self.subTest(job=job):
                self.assertTrue(us.unit_fire_idle_before_enqueue(self.poll(Job=job)))
        self.assertFalse(us.unit_fire_idle_before_enqueue(self.poll(Job="4")))
        self.assertFalse(us.unit_fire_idle_before_enqueue(self.poll(ActiveState="active")))
        self.assertFalse(us.unit_fire_idle_before_enqueue(self.poll(InvocationID=VALID_ID)))

    def test_finite_next_elapse_basis(self) -> None:
        f = us.finite_next_elapse_basis
        self.assertEqual(f(Elapse("finite", 1), Elapse("finite", 2)), "realtime")
        self.assertEqual(f(Elapse("unset"), Elapse("finite")), "monotonic")
        self.assertEqual(f(Elapse("infinity"), Elapse("finite", 3)), "monotonic")
        self.assertIsNone(f(Elapse("infinity"), Elapse("unset")))
        self.assertIsNone(f(None, None))

    def test_service_in_flight(self) -> None:
        for state in ("activating", "active", "deactivating"):
            with self.subTest(state=state):
                self.assertTrue(us.service_in_flight(state, 0))
        self.assertFalse(us.service_in_flight("inactive", 0))
        self.assertFalse(us.service_in_flight("inactive", None))
        self.assertTrue(us.service_in_flight("inactive", 3))
        self.assertTrue(us.service_in_flight("inactive", NonCanonical("0")))
        self.assertFalse(us.service_in_flight("inactive", NonCanonical("  ")))
        self.assertFalse(us.service_in_flight(None, None))

    def test_receiver_idle(self) -> None:
        self.assertTrue(us.receiver_idle("inactive", 0))
        self.assertTrue(us.receiver_idle("failed", None))
        self.assertTrue(us.receiver_idle("inactive", NonCanonical("")))
        self.assertFalse(us.receiver_idle("inactive", 12))
        self.assertFalse(us.receiver_idle("inactive", NonCanonical("00")))
        self.assertFalse(us.receiver_idle("active", 0))
        self.assertFalse(us.receiver_idle(None, 0))

    def test_valid_invocation_id(self) -> None:
        self.assertTrue(us.valid_invocation_id(VALID_ID))
        self.assertFalse(us.valid_invocation_id(""))
        self.assertFalse(us.valid_invocation_id(VALID_ID.upper()))
        self.assertFalse(us.valid_invocation_id("1" * 32))  # historical islower() rule
        self.assertFalse(us.valid_invocation_id(VALID_ID[:-1] + "g"))


class ErrorAndValueTypeTests(unittest.TestCase):
    def test_error_hierarchy(self) -> None:
        for cls in (us.UserManagerUnavailable, us.UserManagerCallFailed, us.UserManagerTimeout, us.UserManagerProtocolError):
            self.assertTrue(issubclass(cls, us.UserManagerError))

    def test_call_failed_detail_is_bounded(self) -> None:
        exc = us.UserManagerCallFailed("org.freedesktop.DBus.Error.Failed", "x" * 5000, transport_evidence=7)
        self.assertEqual(exc.error_name, "org.freedesktop.DBus.Error.Failed")
        self.assertEqual(len(exc.detail), 512)
        self.assertEqual(exc.transport_evidence, 7)

    def test_elapse_validation(self) -> None:
        with self.assertRaises(ValueError):
            Elapse("never")
        with self.assertRaises(ValueError):
            Elapse("unset", 5)

    def test_snapshot_source_rejects_unknown_encoding(self) -> None:
        with self.assertRaises(ValueError):
            SnapshotSource("x", "json", 0, "")

    def test_value_types_are_frozen(self) -> None:
        snap = _text(ReadPurpose.RECEIVER_ACTIVE, ActiveState="inactive")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snap.active_state = "active"  # type: ignore[misc]


class DigestKeyTests(unittest.TestCase):
    def test_text_stdout_keeps_historical_keys(self) -> None:
        source = SnapshotSource("cli", "cli-show-stdout", 12, "ab")
        self.assertEqual(us.snapshot_digest_evidence("pre", source), {"pre_stdout_length": "12", "pre_stdout_sha256": "ab"})
        self.assertEqual(us.snapshot_digest_evidence("", source), {"stdout_length": "12", "stdout_sha256": "ab"})

    def test_typed_digest_is_never_labelled_stdout(self) -> None:
        source = SnapshotSource("dbus", us.TYPED_CANONICAL_JSON, 9, "cd")
        evidence = us.snapshot_digest_evidence("pre", source)
        self.assertEqual(
            evidence,
            {"pre_snapshot_length": "9", "pre_snapshot_sha256": "cd", "pre_snapshot_encoding": "typed-canonical-json"},
        )
        self.assertFalse(any("stdout" in key for key in evidence))


class ModuleCleanlinessTests(unittest.TestCase):
    BANNED_SUBSTRINGS = (
        "system" + "ctl",
        "login" + "ctl",
        "bus" + "ctl",
        "systemd/" + "private",
        "DBUS_SESSION_" + "BUS_ADDRESS",
        "XDG_RUNTIME" + "_DIR",
    )
    BANNED_IMPORTS = ("subprocess", "tools.host_control", "tools.persona_installer", "dbus", "os")

    def setUp(self) -> None:
        self.source = MODULE_PATH.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_no_banned_imports(self) -> None:
        imported: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(f"{node.module}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                self.assertNotIn(name, ("import_module", "__import__"), "dynamic import")
        for banned in self.BANNED_IMPORTS:
            with self.subTest(banned=banned):
                self.assertFalse(
                    any(name == banned or name.startswith(banned + ".") for name in imported),
                    f"user_systemd imports {banned}",
                )

    def test_no_banned_string_constants_or_text(self) -> None:
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for banned in self.BANNED_SUBSTRINGS:
                    with self.subTest(line=node.lineno, banned=banned):
                        self.assertNotIn(banned, node.value)
        # comments too: the raw source must not name them either
        for banned in self.BANNED_SUBSTRINGS:
            with self.subTest(source=banned):
                self.assertNotIn(banned, self.source)


if __name__ == "__main__":
    unittest.main()
