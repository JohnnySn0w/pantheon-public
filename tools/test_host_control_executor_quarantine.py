"""T-5608: the per-seat executor runs the T-5473 residue quarantine.

Intent (T-5608, HO ruling 2026-10-27): admit ``seat-runtime-residue-quarantine``
as ``files.seat_root`` plus the declared inspection reads in
``envelope.KIND_INSPECTION_READS`` (ORG-052 section 8 "bounded user-systemd
inspection for that seat"; precedent T-4861), so codex plans keep their fires
broker-executed. Pinned here, against the fixture seat root and the recording
``FakeUserManager``:

* the executor's ACTUAL port calls for the kind equal the declared reads,
  exactly and in order, per predicate run; verify, reconcile and rollback make
  none; an undeclared read is refused before the port is called;
* preflight (the apply pre-pass) and apply refuse a live or unreadable seat
  through the installer's own predicate object;
* apply moves the closed residue set receipt-bound and never deletes; the
  receipt is byte-compatible with the installer's; unknown files stay;
* rollback restores exactly from the receipt and refuses on drift;
* a failure part-way restores everything; if the restore cannot finish, the
  still-quarantined paths come back as a changed mutation; a lost apply is
  recoverable through the reconcile observation;
* parse-time: the timer's shape and the persona == timer-instance binding.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import _host_control_executor_fixtures as fx
from tools import persona_installer as PI
from tools import persona_installer_activation as ACT
from tools.host_control.envelope import KIND_INSPECTION_READS
from tools.host_control.executor import engine as engine_module
from tools.host_control.executor import seat_files as seat_files_module
from tools.host_control.executor.wire import InvalidSpec
from tools.user_systemd import ReadPurpose

KIND = "seat-runtime-residue-quarantine"
OP_ID = "service.runtime-residue.quarantine"
PERSONA = "seat"
TIMER = f"pantheon-persona-consume@{PERSONA}.timer"
CONSUME = f"pantheon-persona-consume@{PERSONA}.service"
CONTROLLER = f"pantheon-runtime-controller@{PERSONA}.service"
STAMP = "20000101T000000Z"
ROLE_OF = {TIMER: "timer", CONSUME: "consume", CONTROLLER: "controller"}
PURPOSE_VALUE = {purpose.name: purpose.value for purpose in ReadPurpose}
RESIDUE = (
    ("runtime-controller", "runtime.json"),
    ("runtime-controller", "initial-viability-invocations"),
    ("persona-root", "staged-cutover-1.json"),
    ("persona-root", "active-runtime.json"),
    ("state-root", f"{PERSONA}.json"),
)
UNKNOWN = ("runtime-controller/overrides/holds.json", "notes.txt")


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fixture = fx.build_executor_fixture(Path(temp.name) / "x")
        self.addCleanup(self.fixture.close)
        self.fake = self.fixture.user_manager
        self.state_root = self.fixture.seat_root / ".local/state/pantheon/personas"
        self.persona_root = self.state_root / PERSONA
        self.persona_root.mkdir(parents=True)
        for unit in (CONTROLLER, CONSUME):
            self.fake.units[unit] = {"ActiveState": "inactive"}
        patcher = mock.patch.object(engine_module, "_quarantine_stamp", return_value=STAMP)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.qdir = self.persona_root / f".installer-quarantine-{STAMP}"

    # -- helpers ----------------------------------------------------------

    def op(self, **desired):
        values = {"persona_state_root": str(self.state_root), "persona": PERSONA, "timer": TIMER}
        values.update(desired)
        return fx.op(OP_ID, KIND, str(self.persona_root), values)

    def seed(self) -> None:
        rc = self.persona_root / "runtime-controller"
        (rc / "initial-viability-invocations").mkdir(parents=True)
        (rc / "initial-viability-invocations" / "a.json").write_text("{}", encoding="utf-8")
        (rc / "runtime.json").write_text('{"pid": 1}', encoding="utf-8")
        (self.persona_root / "staged-cutover-1.json").write_text("s", encoding="utf-8")
        (self.persona_root / "active-runtime.json").write_text("a", encoding="utf-8")
        (self.state_root / f"{PERSONA}.json").write_text("p", encoding="utf-8")
        (rc / "overrides").mkdir()
        (rc / "overrides" / "holds.json").write_text("h", encoding="utf-8")
        (self.persona_root / "notes.txt").write_text("n", encoding="utf-8")

    def tree(self) -> dict[str, str]:
        out = {}
        for path in sorted(self.state_root.rglob("*")):
            rel = str(path.relative_to(self.state_root))
            if path.is_symlink():
                out[rel] = "link:" + os.readlink(path)
            elif path.is_dir():
                out[rel] = "dir"
            else:
                out[rel] = path.read_text(encoding="utf-8")
        return out

    def execute(self, *operations, kind="apply", rollback=None):
        if rollback is None:
            spec = fx.parsed(self.fixture, tuple(operations), execution_kind=kind)
        else:
            payload = fx.spec_payload(self.fixture, tuple(operations), execution_kind="rollback")
            payload.update(receipt_handle="ab" * 32, receipt_digest="cd" * 32, rollback_mutations=rollback)
            spec = self.parse(payload)
        return self.fixture.engine().run(spec)

    def check(self):
        """The engine's preflight predicate outside a run (a live deadline bound)."""
        engine = self.fixture.engine()
        engine._deadline = engine._clock() + 60.0
        return engine._quarantine_check(self.op())

    def parse(self, payload):
        from tools.host_control.contracts import canonical_json, sha256_json
        from tools.host_control.executor.engine import EXECUTION_SPEC_SCHEMA, parse_spec

        digest = sha256_json(payload)
        return parse_spec(
            schema_version=EXECUTION_SPEC_SCHEMA,
            execution_kind=payload["execution_kind"],
            returned_digest=digest,
            spec_json=canonical_json(payload),
            expected_digest=digest,
            operation_handle=fx.OPERATION_HANDLE,
            identity=self.fixture.identity,
            registry=self.fixture.registry,
        )

    @staticmethod
    def mutation_dict(mutation) -> dict:
        return dataclasses.asdict(mutation)

    def declared_calls(self) -> list[tuple[str, str, str]]:
        """The recording port's calls, in the envelope table's encoding."""
        out = []
        for call in self.fake.calls:
            name, _, args = call.partition(":")
            parts = args.split(",") if args else []
            if name == "unit_file_enablement":
                out.append((name, ROLE_OF.get(parts[0], parts[0]), ""))
            elif name == "read_unit":
                out.append((name, ROLE_OF.get(parts[0], parts[0]), PURPOSE_VALUE[parts[1]]))
            else:
                out.append((name, args, "?"))
        return out


class DeclaredReadsTests(_Case):
    def test_actual_port_calls_equal_the_declared_reads_exactly(self) -> None:
        self.seed()
        result = self.execute(self.op())
        self.assertTrue(result.ok, result.evidence)
        declared = list(KIND_INSPECTION_READS[KIND])
        # the pre-pass and apply each run the predicate once: same reads, same order
        self.assertEqual(self.declared_calls(), declared + declared)

    def test_verify_reconcile_and_rollback_make_no_port_call(self) -> None:
        self.seed()
        applied = self.execute(self.op())
        self.fake.calls.clear()
        for kind in ("verify", "reconcile"):
            with self.subTest(kind=kind):
                self.assertTrue(self.execute(self.op(), kind=kind).ok)
        self.assertTrue(self.execute(self.op(), rollback=[self.mutation_dict(applied.mutations[0])]).ok)
        self.assertEqual(self.fake.calls, [])

    def test_an_undeclared_read_is_refused_before_the_port_is_called(self) -> None:
        units = {"timer": TIMER, "consume": CONSUME, "controller": CONTROLLER}
        port = engine_module.DeclaredInspectionPort(self.fake, KIND_INSPECTION_READS[KIND], units, lambda: 1.0)
        for call in (
            lambda: port.read_unit(TIMER, ReadPurpose.TIMER_ELAPSE),
            lambda: port.read_unit(CONTROLLER, ReadPurpose.UNIT_FILE_ACTIVE),
            lambda: port.unit_file_enablement(CONSUME),
            lambda: port.read_unit("pantheon-persona-consume@other.service", ReadPurpose.SERVICE_OBSERVE),
        ):
            with self.assertRaises(engine_module.UnitRefused):
                call()
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(hasattr(port, "start_unit") or hasattr(port, "enable_now") or hasattr(port, "reload"))


class PreflightTests(_Case):
    def test_live_or_unreadable_seat_refuses_before_anything_changes(self) -> None:
        self.seed()
        before = self.tree()
        for unit in (CONTROLLER, CONSUME):
            for state in ("active", "activating", "deactivating", "reloading", "weird", None):
                with self.subTest(unit=unit, state=state):
                    self.fake.units[unit] = {} if state is None else {"ActiveState": state}
                    if state is None:
                        self.fake.refuse.clear()
                        original = self.fake.read_unit

                        def read(u, purpose, *, timeout=None, _unit=unit, _orig=original):
                            if u == _unit:
                                self.fake.calls.append(f"read_unit:{u},{purpose.name}")
                                raise PI._us.UserManagerProtocolError("bad", reason="x")
                            return _orig(u, purpose, timeout=timeout)

                        with mock.patch.object(self.fake, "read_unit", side_effect=read):
                            result = self.execute(self.op())
                    else:
                        result = self.execute(self.op())
                    self.assertFalse(result.ok)
                    self.assertEqual(result.mutations, ())
                    self.assertIn("pre-pass", result.evidence[0].detail)
                    self.assertEqual(self.tree(), before)
                    self.fake.units[unit] = {"ActiveState": "inactive"}

    def test_unreachable_manager_refuses(self) -> None:
        self.seed()
        before = self.tree()
        self.fake.reachable = False
        result = self.execute(self.op())
        self.assertFalse(result.ok)
        self.assertIn("cannot read the consume timer enablement", result.evidence[0].detail)
        self.assertEqual(self.tree(), before)

    def test_inactive_or_failed_does_not_block(self) -> None:
        self.seed()
        for state in ("inactive", "failed"):
            self.fake.units[CONTROLLER] = {"ActiveState": state}
            self.assertEqual(
                self.check().classification, "ready"
            )

    def test_enabled_timer_is_the_converged_no_op(self) -> None:
        self.seed()
        self.fake.enablement[TIMER] = "enabled"
        self.fake.units[CONTROLLER] = {"ActiveState": "active"}
        before = self.tree()
        result = self.execute(self.op())
        self.assertTrue(result.ok, result.evidence)
        self.assertFalse(result.mutations[0].changed)
        self.assertEqual(result.mutations[0].before["state"], "skipped")
        self.assertEqual(self.tree(), before)
        # converged: only the bootstrap read ran (pre-pass + apply)
        self.assertEqual(self.declared_calls(), [KIND_INSPECTION_READS[KIND][0]] * 2)

    def test_the_predicate_is_the_installers_object(self) -> None:
        self.seed()
        gate = mock.MagicMock(wraps=ACT._runtime_liveness_refusal)
        with mock.patch.object(ACT, "_runtime_liveness_refusal", gate):
            self.assertTrue(self.execute(self.op()).ok)
        # the installer's own gate ran in the pre-pass and in apply, on the same states
        self.assertEqual(gate.call_count, 2)
        self.assertEqual(gate.call_args_list[0], gate.call_args_list[1])
        self.assertEqual(
            gate.call_args_list[0].args[0], {CONTROLLER: "inactive", CONSUME: "inactive"}
        )
        self.assertIs(engine_module._residue_quarantine_check, PI._residue_quarantine_check)

    def test_executor_and_installer_agree_on_every_matrix_cell(self) -> None:
        # the executor's preflight equals the installer local backend's over the
        # same reads (enabled x controller x consume x residue present/absent)
        self.seed()
        for enabled in (True, False):
            for controller in ("inactive", "failed", "active", None):
                for consume in ("inactive", "activating", None):
                    with self.subTest(enabled=enabled, controller=controller, consume=consume):
                        states = {CONTROLLER: controller, CONSUME: consume}
                        self.fake.enablement[TIMER] = "enabled" if enabled else "disabled"
                        for unit, state in states.items():
                            self.fake.units[unit] = {} if state is None else {"ActiveState": state}
                        backend = mock.Mock()
                        backend._bootstrap_fire_pending = lambda timer: not enabled
                        backend._runtime_liveness = lambda persona: {
                            unit: (state or None) for unit, state in states.items()
                        }
                        installer = PI.LocalHostBackend._residue_quarantine_preflight(backend, self.op())
                        mine = self.check()
                        if controller is None or consume is None:
                            # an empty native read is "" for the executor: unreadable/unknown, both refuse
                            self.assertEqual((mine.ok, mine.classification), (installer.ok, installer.classification))
                        else:
                            self.assertEqual(mine, installer)


class ApplyTests(_Case):
    def test_moves_the_closed_set_receipt_bound_and_keeps_unknown_files(self) -> None:
        self.seed()
        expected_listing = json.dumps(
            [list(item) for item in ACT._bootstrap_residue_entries(self.state_root, PERSONA)], separators=(",", ":")
        )
        self.assertEqual(json.loads(expected_listing), [list(item) for item in sorted_residue()])
        unlinks = mock.MagicMock(wraps=os.unlink)
        rmtrees = mock.MagicMock()
        with mock.patch.object(seat_files_module.os, "unlink", unlinks), \
                mock.patch("shutil.rmtree", rmtrees):
            result = self.execute(self.op())
        self.assertTrue(result.ok, result.evidence)
        unlinks.assert_not_called()
        rmtrees.assert_not_called()
        mutation = result.mutations[0]
        self.assertTrue(mutation.changed)
        self.assertEqual(mutation.after_fingerprint, "quarantined")
        self.assertEqual(
            dict(mutation.before),
            {"state": "present", "persona_state_root": str(self.state_root), "persona": PERSONA,
             "stamp": STAMP, "moved": expected_listing},
        )
        for cls, name in RESIDUE:
            self.assertTrue(os.path.lexists(self.qdir / cls / name), (cls, name))
            base = Path(ACT._quarantine_class_base(str(self.state_root), PERSONA, cls))
            self.assertFalse(os.path.lexists(base / name))
        for rel in UNKNOWN:
            self.assertTrue((self.persona_root / rel).exists(), rel)
        self.assertEqual((self.qdir / "runtime-controller" / "runtime.json").read_text(), '{"pid": 1}')

    def test_receipt_is_byte_compatible_with_the_installer_backend(self) -> None:
        self.seed()
        executor = self.execute(self.op()).mutations[0]
        # restore the tree, then run the installer's local backend over the same seat
        self.assertTrue(self.execute(self.op(), rollback=[self.mutation_dict(executor)]).ok)
        backend = PI.LocalHostBackend()
        with mock.patch.object(backend, "_bootstrap_fire_pending", return_value=True), \
                mock.patch.object(backend, "_runtime_liveness", return_value={CONTROLLER: "inactive", CONSUME: "inactive"}), \
                mock.patch.object(PI, "_quarantine_stamp", return_value=STAMP):
            installer, _ = backend._execute_residue_quarantine(self.op())
        self.assertEqual(self.mutation_dict(executor), self.mutation_dict(installer))

    def test_no_residue_is_an_absent_no_op(self) -> None:
        result = self.execute(self.op())
        self.assertTrue(result.ok)
        self.assertFalse(result.mutations[0].changed)
        self.assertEqual(result.mutations[0].before, {"state": "absent"})
        self.assertFalse(self.qdir.exists())

    def test_an_existing_quarantine_dir_refuses(self) -> None:
        self.seed()
        self.qdir.mkdir()
        before = self.tree()
        result = self.execute(self.op())
        self.assertFalse(result.ok)
        self.assertEqual(result.mutations, ())
        self.assertEqual(self.tree(), before)

    def test_a_symlinked_class_base_refuses_without_reading_through_it(self) -> None:
        elsewhere = self.fixture.seat_root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "runtime.json").write_text("x", encoding="utf-8")
        os.symlink(str(elsewhere), self.persona_root / "runtime-controller")
        result = self.execute(self.op())
        self.assertFalse(result.ok)
        self.assertEqual(result.mutations, ())
        self.assertIn("runtime-controller is not a directory", result.evidence[0].detail)
        self.assertTrue((elsewhere / "runtime.json").exists())

    def test_a_residue_symlink_is_moved_as_the_link_itself(self) -> None:
        target = self.fixture.seat_root / "outside.json"
        target.write_text("keep", encoding="utf-8")
        os.symlink(str(target), self.persona_root / "active-runtime.json")
        result = self.execute(self.op())
        self.assertTrue(result.ok, result.evidence)
        moved = self.qdir / "persona-root" / "active-runtime.json"
        self.assertTrue(moved.is_symlink())
        self.assertEqual(target.read_text(), "keep")

    def test_target_must_be_the_persona_root(self) -> None:
        self.seed()
        operation = dataclasses.replace(self.op(), target=str(self.state_root / "other"))
        result = self.execute(operation)
        self.assertFalse(result.ok)
        self.assertIn("persona root", result.evidence[0].detail)

    def test_a_failure_part_way_restores_everything(self) -> None:
        self.seed()
        before = self.tree()
        real = seat_files_module.SeatFiles.rename_within
        calls = {"n": 0}

        def flaky(files, source, destination):
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError("disk full")
            return real(files, source, destination)

        with mock.patch.object(seat_files_module.SeatFiles, "rename_within", flaky):
            result = self.execute(self.op())
        self.assertFalse(result.ok)
        self.assertEqual(result.mutations, ())
        self.assertIn("restored", result.evidence[0].detail)
        self.assertEqual(self.tree(), before)

    def test_an_unfinishable_restore_keeps_the_receipt_for_what_stayed_moved(self) -> None:
        self.seed()
        real = seat_files_module.SeatFiles.rename_within
        state = {"n": 0}

        def flaky(files, source, destination):
            state["n"] += 1
            if state["n"] == 3:
                raise OSError("disk full")
            if ".installer-quarantine-" in source and source.endswith("runtime.json"):
                raise OSError("cannot move back")
            return real(files, source, destination)

        with mock.patch.object(seat_files_module.SeatFiles, "rename_within", flaky):
            result = self.execute(self.op())
        self.assertFalse(result.ok)
        self.assertEqual(len(result.mutations), 1)
        mutation = result.mutations[0]
        self.assertTrue(mutation.changed)
        self.assertEqual(json.loads(mutation.before["moved"]), [["runtime-controller", "runtime.json"]])
        # and that receipt drives an exact rollback of the one stranded path
        restored = self.execute(self.op(), rollback=[self.mutation_dict(mutation)])
        self.assertTrue(restored.ok, restored.evidence)
        self.assertEqual(
            (self.persona_root / "runtime-controller" / "runtime.json").read_text(), '{"pid": 1}'
        )
        self.assertFalse(self.qdir.exists())


class RollbackTests(_Case):
    def applied(self):
        self.seed()
        self.before = self.tree()
        result = self.execute(self.op())
        self.assertTrue(result.ok, result.evidence)
        return result.mutations[0]

    def test_rollback_restores_exactly_from_the_receipt(self) -> None:
        mutation = self.applied()
        result = self.execute(self.op(), rollback=[self.mutation_dict(mutation)])
        self.assertTrue(result.ok, result.evidence)
        self.assertEqual(self.tree(), self.before)
        self.assertEqual(result.mutations[0].after_fingerprint, "restored")

    def test_a_re_created_original_refuses_the_whole_rollback(self) -> None:
        mutation = self.applied()
        (self.persona_root / "runtime-controller" / "runtime.json").write_text("new", encoding="utf-8")
        snapshot = self.tree()
        result = self.execute(self.op(), rollback=[self.mutation_dict(mutation)])
        self.assertFalse(result.ok)
        self.assertIn("drifted", result.detail)
        self.assertEqual(self.tree(), snapshot)

    def test_a_vanished_copy_refuses(self) -> None:
        mutation = self.applied()
        os.unlink(self.qdir / "persona-root" / "active-runtime.json")
        result = self.execute(self.op(), rollback=[self.mutation_dict(mutation)])
        self.assertFalse(result.ok)
        self.assertIn("drifted", result.detail)

    def test_a_malformed_or_foreign_receipt_refuses(self) -> None:
        mutation = self.applied()
        for before in (
            dict(mutation.before, moved='[["runtime-controller","../escape"]]'),
            dict(mutation.before, moved='[["elsewhere","x"]]'),
            dict(mutation.before, persona="other"),
            dict(mutation.before, persona_state_root="/etc"),
            dict(mutation.before, stamp="../x"),
        ):
            with self.subTest(before=before):
                bad = dict(self.mutation_dict(mutation), before=before)
                result = self.execute(self.op(), rollback=[bad])
                self.assertFalse(result.ok)
                self.assertTrue(self.qdir.exists())

    def test_a_skipped_or_absent_record_is_not_restorable(self) -> None:
        with self.assertRaises(InvalidSpec):
            self.execute(self.op(), rollback=[{
                "operation_id": OP_ID, "kind": KIND, "target": str(self.persona_root), "owner": fx.ACCOUNT,
                "changed": False, "before": {"state": "absent"}, "after_fingerprint": "quarantined",
            }])


class RecoveryTests(_Case):
    def test_reconcile_reports_a_partial_move_for_recovery(self) -> None:
        # a crash between moves: some residue already in the stamped dir, no receipt
        self.seed()
        (self.qdir / "runtime-controller").mkdir(parents=True)
        os.rename(self.persona_root / "runtime-controller" / "runtime.json",
                  self.qdir / "runtime-controller" / "runtime.json")
        result = self.execute(self.op(), kind="reconcile")
        self.assertTrue(result.ok, result.evidence)
        values = result.evidence[0].evidence
        self.assertEqual(values["observation"], "residue-quarantine")
        self.assertEqual(json.loads(values["quarantine_dirs"]), [self.qdir.name])
        self.assertEqual(
            json.loads(values["quarantined"]), {self.qdir.name: [["runtime-controller", "runtime.json"]]}
        )
        self.assertNotIn(["runtime-controller", "runtime.json"], json.loads(values["residue"]))
        # a re-apply converges forward under a new stamp; nothing is lost
        with mock.patch.object(engine_module, "_quarantine_stamp", return_value="20000102T000000Z"):
            again = self.execute(self.op())
        self.assertTrue(again.ok, again.evidence)
        self.assertTrue((self.qdir / "runtime-controller" / "runtime.json").exists())
        self.assertEqual(ACT._bootstrap_residue_entries(self.state_root, PERSONA), [])


class SeatFilesMoveTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.files = seat_files_module.SeatFiles(seat_root=self.fixture.seat_root, owner_uid=os.getuid())
        (self.persona_root / "a").write_text("a", encoding="utf-8")
        (self.persona_root / "dir").mkdir()

    def test_rename_within_moves_and_refuses_an_existing_destination(self) -> None:
        source, destination = str(self.persona_root / "a"), str(self.persona_root / "dir" / "a")
        self.files.rename_within(source, destination)
        self.assertEqual(Path(destination).read_text(), "a")
        self.assertFalse(os.path.lexists(source))
        (self.persona_root / "b").write_text("b", encoding="utf-8")
        with self.assertRaises(seat_files_module.SeatPathRefused):
            self.files.rename_within(str(self.persona_root / "b"), destination)
        self.assertEqual(Path(destination).read_text(), "a")  # never replaced
        self.assertEqual((self.persona_root / "b").read_text(), "b")

    def test_rename_within_refuses_absent_outside_and_symlinked_paths(self) -> None:
        outside = self.fixture.root / "outside"
        outside.mkdir()
        os.symlink(str(outside), self.persona_root / "link")
        for source, destination in (
            (str(self.persona_root / "missing"), str(self.persona_root / "dir" / "m")),
            (str(self.persona_root / "a"), str(outside / "a")),
            (str(self.persona_root / "a"), str(self.persona_root / "link" / "a")),
            (str(self.persona_root / "a"), str(self.persona_root / "nodir" / "a")),
        ):
            with self.subTest(source=source, destination=destination):
                with self.assertRaises(seat_files_module.SeatPathRefused):
                    self.files.rename_within(source, destination)
        self.assertTrue((self.persona_root / "a").exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_lexists_and_list_dir(self) -> None:
        os.symlink("/nonexistent", self.persona_root / "dangling")
        self.assertTrue(self.files.lexists(str(self.persona_root / "dangling")))
        self.assertFalse(self.files.lexists(str(self.persona_root / "nope")))
        self.assertFalse(self.files.lexists(str(self.persona_root / "nope" / "deeper")))
        self.assertEqual(self.files.list_dir(str(self.persona_root)), ("a", "dangling", "dir"))
        self.assertEqual(self.files.list_dir(str(self.persona_root / "nope")), ())
        with self.assertRaises(seat_files_module.SeatPathRefused):
            self.files.list_dir(str(self.persona_root / "dangling"))


class ParseTests(_Case):
    def test_the_call_time_guard_binds_persona_to_the_timer_instance(self) -> None:
        engine = self.fixture.engine()
        for desired in ({"persona": "other"}, {"timer": "pantheon-persona-consume@other.timer"}):
            with self.subTest(desired=desired), self.assertRaises(engine_module.UnitRefused):
                engine._quarantine_units(self.op(**desired))
        self.assertEqual(
            engine._quarantine_units(self.op()), {"timer": TIMER, "consume": CONSUME, "controller": CONTROLLER}
        )

    def test_persona_must_be_the_timer_instance(self) -> None:
        for desired in ({"persona": "other"}, {"timer": "pantheon-persona-consume@other.timer"}):
            with self.subTest(desired=desired), self.assertRaises(InvalidSpec):
                fx.parsed(self.fixture, (self.op(**desired),))

    def test_the_timer_must_have_the_persona_timer_shape(self) -> None:
        for timer in ("codex-telegram-poller.service", "pantheon-persona-consume@seat.service", "x.timer"):
            with self.subTest(timer=timer), self.assertRaises(InvalidSpec):
                fx.parsed(self.fixture, (self.op(timer=timer),))

    def test_one_consume_instance_per_spec(self) -> None:
        other = fx.op(
            "connective.timer-observe", "timer-observe", "pantheon-persona-consume@other.timer",
            {"service": "pantheon-persona-consume@other.service"},
        )
        with self.assertRaises(InvalidSpec):
            fx.parsed(self.fixture, (self.op(), other))


def sorted_residue():
    """RESIDUE in the installer's listing order (class tables, then patterns)."""
    order = {name: index for index, name in enumerate(
        ACT.RESIDUE_RUNTIME_CONTROLLER + ACT.RESIDUE_PERSONA_ROOT
    )}
    classes = {"runtime-controller": 0, "persona-root": 1, "state-root": 2}

    def key(item):
        cls, name = item
        pattern = "staged-cutover-*.json" if name.startswith("staged-cutover-") else name
        return classes[cls], order.get(pattern, 99)

    return sorted(RESIDUE, key=key)


if __name__ == "__main__":
    unittest.main()
