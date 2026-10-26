"""T-2917 5d.2: rollback execution (rulings T-2917:D15, D16; D6 in three
layers).

Broker half: the restorable set (write-ahead intents x changed mutation
records, the D6 owned-record rule), prepare/dispatch/collect through the
driver, the elided rollback spec, and the ``Rollback`` D-Bus method wired
like ``Apply``. Executor half: the installer's ``InstallerFlow.rollback``
semantics over the confined seat files and the user-manager port.

Fixture evidence only (fakes, sqlite, a disposable seat root).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import _host_control_executor_fixtures as fx
from tools.test_host_control_driver import SUBJECT_UID, FakePort, _DomainCase, _ok
from tools.test_host_control_executor_activation import (
    SERVICE,
    TIMER,
    _Case as _ActivationCase,
    _service_state,
    _timer_state,
)
from tools.host_control.contracts import (
    HostControlError,
    RollbackIntent,
    canonical_json,
    sha256_json,
)
from tools.host_control.execution_source import SPEC_JSON_LIMIT, BrokerExecutionSource
from tools.host_control.executor.engine import EXECUTION_SPEC_SCHEMA, parse_spec
from tools.host_control.executor.seat_files import SeatChangeIncomplete
from tools.host_control.operations import (
    RESTORE_CLEANUP_AUTHORITY,
    RESTORE_INVERSE_ACTION,
    SYNTHESIZED_TIMER_ENABLE_INVERSE,
    restorable_mutations,
)
from tools.persona_installer import MutationRecord

MARK = "# Managed by pantheon persona installer\n"


def _intent(operation_id: str, kind: str, target: str, owner: str = "acct", inverse=None) -> RollbackIntent:
    return RollbackIntent(
        operation_handle="ab" * 32,
        seq=1,
        operation_id=operation_id,
        kind=kind,
        target=target,
        owner=owner,
        inverse=dict(inverse or {"state": "receipt-snapshot"}),
        created_at="2026-10-13T00:00:00+00:00",
    )


def _mutation(operation_id: str, kind: str, target: str, *, changed: bool = True, before=None, owner: str = "acct"):
    return MutationRecord(operation_id, kind, target, owner, changed, dict(before or {"state": "absent"}), "directory")


def _rollback_spec(fixture, operations, mutations):
    payload = fx.spec_payload(fixture, tuple(operations), execution_kind="rollback")
    payload.update(
        receipt_handle="ab" * 32,
        receipt_digest="cd" * 32,
        rollback_mutations=[
            {
                "operation_id": item.operation_id,
                "kind": item.kind,
                "target": item.target,
                "owner": item.owner,
                "changed": item.changed,
                "before": dict(item.before),
                "after_fingerprint": item.after_fingerprint,
            }
            for item in mutations
        ],
    )
    for operation in payload["operations"]:  # D16: the broker elides content
        operation["desired"].pop("content", None)
    digest = sha256_json(payload)
    return parse_spec(
        schema_version=EXECUTION_SPEC_SCHEMA,
        execution_kind="rollback",
        returned_digest=digest,
        spec_json=canonical_json(payload),
        expected_digest=digest,
        operation_handle=payload["operation_handle"],
        identity=fixture.identity,
        registry=fixture.registry,
    )


# ---------------------------------------------------------------------------
# broker: the restorable set (T-2917:D6/D15)
# ---------------------------------------------------------------------------


class RestorableSetTests(unittest.TestCase):
    def test_restorable_set_is_intents_intersect_changed_mutations(self) -> None:
        intents = (
            _intent("dir", "mkdir", "/s/d"),
            _intent("file", "atomic-write", "/s/f"),
            _intent("moved", "mkdir", "/s/m"),
        )
        receipt = SimpleNamespace(
            mutations=(
                _mutation("dir", "mkdir", "/s/d"),  # changed + intent: restorable
                _mutation("file", "atomic-write", "/s/f", changed=False),  # unchanged
                _mutation("diag", "harness-check", "codex"),  # no intent
                _mutation("moved", "mkdir", "/s/OTHER"),  # target differs from its intent
                _mutation("dir", "mkdir", "/s/d", owner="intruder"),  # owner differs
            )
        )
        self.assertEqual([m.operation_id for m in restorable_mutations(receipt, intents)], ["dir"])
        self.assertEqual(restorable_mutations(receipt, ()), ())

    def test_d6_synthesized_timer_intent_without_owned_record_is_not_restorable(self) -> None:
        intent = _intent("timer", "timer-enable", "t.timer", inverse=SYNTHESIZED_TIMER_ENABLE_INVERSE)
        for before in (
            {"state": "disabled"},
            {"state": "disabled", "owned": "false", "activation_intent": "false"},
            {"state": "disabled", "owned": "true", "activation_intent": "true"},
        ):
            with self.subTest(before=before):
                receipt = SimpleNamespace(mutations=(_mutation("timer", "timer-enable", "t.timer", before=before),))
                self.assertEqual(restorable_mutations(receipt, (intent,)), ())

    def test_d6_owned_timer_record_is_restorable(self) -> None:
        intent = _intent("timer", "timer-enable", "t.timer", inverse=SYNTHESIZED_TIMER_ENABLE_INVERSE)
        owned = _mutation(
            "timer", "timer-enable", "t.timer",
            before={"state": "disabled", "owned": "true", "activation_intent": "false"},
        )
        self.assertEqual(restorable_mutations(SimpleNamespace(mutations=(owned,)), (intent,)), (owned,))


# ---------------------------------------------------------------------------
# broker: prepare, dispatch, collect; the D-Bus method
# ---------------------------------------------------------------------------


class _BrokerRollbackCase(_DomainCase):
    def plan_op(self, kind: str):
        plan = self.fixture.journal.load_plan_unbound(self.plan.plan_handle)
        return next(op for op in plan.installer_plan.operations if op.kind == kind and op.inverse)

    def receipt_for(self, handle: str, phase: str) -> str:
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            return connection.execute(
                "SELECT receipt_handle FROM operation_receipts WHERE operation_handle=? AND phase=?",
                (handle, phase),
            ).fetchone()[0]

    def completed_apply(self, key: str, *, ok: bool = True, before=None, kind: str = "mkdir") -> tuple[str, MutationRecord]:
        handle = self.apply(key)
        op = self.plan_op(kind)
        made = MutationRecord(op.operation_id, op.kind, op.target, op.owner, True, dict(before or {"state": "absent"}), "directory")
        result = _ok((op.operation_id,), (made,))
        if not ok:
            result = dataclass_replace_ok(result, False)
        self.port.collect_script = [result]
        self.assertEqual(self.core.drive(handle), "completed" if ok else "failed")
        return self.receipt_for(handle, "complete" if ok else "failed"), made


def dataclass_replace_ok(collected, ok: bool):
    import dataclasses

    return dataclasses.replace(collected, ok=ok)


class BrokerRollbackTests(_BrokerRollbackCase):
    def test_rollback_requires_restorable_mutations(self) -> None:
        handle = self.apply("rb-empty")
        self.port.collect_script = [_ok()]
        self.assertEqual(self.core.drive(handle), "completed")
        with self.assertRaises(HostControlError) as caught:
            self.service.prepare_rollback(SUBJECT_UID, self.receipt_for(handle, "complete"), "rb-empty", "cd" * 32)
        self.assertIn("no restorable mutation", str(caught.exception))
        # D15: a FAILED apply receipt with a restorable change is admitted
        receipt, _made = self.completed_apply("rb-failed", ok=False)
        record, _ = self.service.prepare_rollback(SUBJECT_UID, receipt, "rb-failed", "cd" * 32)
        self.assertEqual((record.kind, record.state), ("rollback", "prepared"))

    def test_rollback_is_bound_dispatched_collected_and_completed(self) -> None:
        receipt, made = self.completed_apply("rb-src")
        rollback, _ = self.service.prepare_rollback(SUBJECT_UID, receipt, "rb-1", "cd" * 32)
        binds = len(self.binder.calls)
        restored = MutationRecord(made.operation_id, made.kind, made.target, made.owner, True, {"state": "directory", "mode": "0755"}, None)
        self.port.collect_script = [_ok((made.operation_id,), (restored,))]
        self.assertEqual(self.core.drive(rollback.operation_handle), "completed")
        self.assertEqual(len(self.binder.calls), binds + 1)  # grant-bound
        _handle, durable_state, phase, intent_count = self.port.executions[-1]
        self.assertEqual((durable_state, phase, intent_count), ("dispatch_intent", "staged", 1))
        (intent,) = self.fixture.journal.intents_for(rollback.operation_handle)
        self.assertEqual(
            dict(intent.inverse),
            {"action": RESTORE_INVERSE_ACTION, "source_receipt_handle": receipt, "cleanup_authority": RESTORE_CLEANUP_AUTHORITY},
        )
        final = json.loads(
            sqlite3.connect(self.fixture.root / "broker.sqlite3").execute(
                "SELECT receipt_json FROM operation_receipts WHERE operation_handle=? AND phase='complete'",
                (rollback.operation_handle,),
            ).fetchone()[0]
        )
        self.assertEqual([m["operation_id"] for m in final["mutations"]], [made.operation_id])

    def test_rollback_spec_elides_desired_content_and_fits_the_limit(self) -> None:
        big = base64.b64encode(os.urandom(300 * 1024)).decode("ascii")
        receipt, made = self.completed_apply(
            "rb-big",
            kind="atomic-write",
            before={"state": "file", "content_b64": big, "fingerprint": "f" * 64, "mode": "0644"},
        )
        rollback, _ = self.service.prepare_rollback(SUBJECT_UID, receipt, "rb-big", "cd" * 32)
        self.service.authorize(rollback.operation_handle)
        record = self.service.dispatch(rollback.operation_handle, FakePort(self.fixture.journal))
        source = BrokerExecutionSource(journal=self.fixture.journal, registry=self.fixture.registry)
        spec = source.spec_for(record)
        encoded = canonical_json(spec)
        self.assertLess(len(encoded.encode("utf-8")), SPEC_JSON_LIMIT)
        self.assertTrue(all("content" not in op["desired"] for op in spec["operations"]))
        self.assertEqual([m["operation_id"] for m in spec["rollback_mutations"]], [made.operation_id])
        self.assertEqual(spec["rollback_mutations"][0]["before"]["content_b64"], big)
        # the plan still carried the content: elision happened in the spec
        plan = self.fixture.journal.load_plan_unbound(self.plan.plan_handle)
        self.assertTrue(any("content" in op.desired for op in plan.installer_plan.operations))


class RollbackMethodTests(_BrokerRollbackCase):
    def setUp(self) -> None:
        super().setUp()
        from tools.host_control import dbus_service

        if not dbus_service.DBUS_AVAILABLE:
            self.skipTest("dbus-python is required")
        from tools.host_control.ratelimit import BrokerRateLimits

        self.dbus_service = dbus_service
        self.submitted: list[str] = []
        instance = object.__new__(dbus_service.HostBroker1Object)
        instance._registry = self.fixture.registry
        instance._operation_service = self.service
        instance._rate_limits = BrokerRateLimits()
        instance._driver = mock.Mock(submit=self.submitted.append, failure=None)
        instance._get_unix_user = lambda sender: SUBJECT_UID
        self.instance = instance

    def operation_count(self) -> int:
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            return connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0]

    def test_rollback_method_submits_to_the_driver(self) -> None:
        receipt, _made = self.completed_apply("rb-method")
        handle = self.dbus_service.HostBroker1Object.Rollback(self.instance, receipt, "rb-m", "cd" * 32, sender=":1.5")
        self.assertEqual(self.submitted, [handle])
        self.assertEqual(self.state(handle), "prepared")

    def test_rollback_refused_before_prepare_with_a_dead_driver(self) -> None:
        import dbus

        from tools.host_control import wire

        receipt, _made = self.completed_apply("rb-dead")
        self.instance._driver.failure = "driver thread died: RuntimeError"
        before = self.operation_count()
        with self.assertRaises(dbus.exceptions.DBusException) as caught:
            self.dbus_service.HostBroker1Object.Rollback(self.instance, receipt, "rb-d", "cd" * 32, sender=":1.5")
        self.assertEqual(caught.exception.get_dbus_name(), wire.FAILED_ERROR)
        self.assertEqual(self.operation_count(), before)
        self.assertEqual(self.submitted, [])


# ---------------------------------------------------------------------------
# executor: restores over the confined seat files
# ---------------------------------------------------------------------------


class ExecutorFileRollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fixture = fx.build_executor_fixture(Path(temp.name) / "x")
        self.addCleanup(self.fixture.close)
        self.seat = self.fixture.seat_root
        self.engine = self.fixture.engine()

    def apply(self, *operations):
        result = self.engine.run(fx.parsed(self.fixture, tuple(operations)))
        self.assertTrue(result.ok, result.evidence)
        return tuple(m for m in result.mutations if m.changed)

    def rollback(self, operations, mutations):
        return self.engine.run(_rollback_spec(self.fixture, operations, mutations))

    def test_restores_file_bytes_and_mode_from_before_snapshot(self) -> None:
        target = self.seat / "managed.conf"
        target.write_text(MARK + "old\n", encoding="utf-8")
        target.chmod(0o600)
        write = fx.content_op("w", "atomic-write", target, MARK + "new\n", mode="0644")
        mutations = self.apply(write)
        self.assertEqual(target.read_text(encoding="utf-8"), MARK + "new\n")
        result = self.rollback((write,), mutations)
        self.assertTrue(result.ok, result.evidence)
        self.assertEqual(target.read_text(encoding="utf-8"), MARK + "old\n")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        (record,) = result.mutations
        self.assertTrue(record.changed)
        # the restore record's before is the PRE-restore snapshot
        self.assertEqual(base64.b64decode(record.before["content_b64"]).decode("utf-8"), MARK + "new\n")

    def test_removes_a_created_file(self) -> None:
        target = self.seat / "created.conf"
        write = fx.content_op("w", "atomic-write", target, MARK + "x\n")
        result = self.rollback((write,), self.apply(write))
        self.assertTrue(result.ok, result.evidence)
        self.assertFalse(target.exists())
        self.assertEqual(result.evidence[0].evidence["action"], "unlink")

    def test_rmdir_only_if_created_and_empty(self) -> None:
        kept = fx.op("kept", "mkdir", str(self.seat / "kept"), {"mode": "0755"})
        gone = fx.op("gone", "mkdir", str(self.seat / "gone"), {"mode": "0755"})
        mutations = self.apply(kept, gone)
        (self.seat / "kept" / "user-data").write_text("mine", encoding="utf-8")
        result = self.rollback((kept, gone), mutations)
        self.assertTrue(result.ok, result.evidence)
        self.assertTrue((self.seat / "kept" / "user-data").exists())  # never recursive
        self.assertFalse((self.seat / "gone").exists())
        details = {item.operation_id: item.detail for item in result.evidence}
        self.assertEqual(details["kept"], "kept: directory not empty")
        self.assertEqual(details["gone"], "restored")
        # a pre-existing directory is never a changed record, so never restored
        (self.seat / "pre").mkdir()
        pre = fx.op("pre", "mkdir", str(self.seat / "pre"), {"mode": "0755"})
        self.assertEqual(self.apply(pre), ())

    def test_any_drifted_target_refuses_the_whole_rollback_before_any_change(self) -> None:
        directory = fx.op("dir", "mkdir", str(self.seat / "d"), {"mode": "0755"})
        write = fx.content_op("w", "atomic-write", self.seat / "f", MARK + "v1\n")
        mutations = self.apply(directory, write)
        (self.seat / "f").write_text(MARK + "someone's later edit\n", encoding="utf-8")
        result = self.rollback((directory, write), mutations)
        self.assertFalse(result.ok)
        self.assertEqual(result.mutations, ())
        self.assertIn("drifted", result.detail)
        self.assertTrue((self.seat / "d").is_dir())  # untouched
        self.assertEqual((self.seat / "f").read_text(encoding="utf-8"), MARK + "someone's later edit\n")

    def test_not_applied_targets_are_skipped(self) -> None:
        write = fx.content_op("w", "atomic-write", self.seat / "f", MARK + "v1\n")
        mutations = self.apply(write)
        (self.seat / "f").unlink()  # already back at the before-state (absent)
        result = self.rollback((write,), mutations)
        self.assertTrue(result.ok, result.evidence)
        self.assertEqual(result.mutations, ())
        self.assertTrue(result.evidence[0].detail.startswith("not-applied"))

    def test_reverse_plan_order(self) -> None:
        directory = fx.op("dir", "mkdir", str(self.seat / "d"), {"mode": "0755"})
        write = fx.content_op("w", "atomic-write", self.seat / "d" / "f", MARK + "v1\n")
        mutations = self.apply(directory, write)
        result = self.rollback((directory, write), mutations)
        self.assertTrue(result.ok, result.evidence)
        self.assertEqual([item.operation_id for item in result.evidence], ["w", "dir"])
        self.assertEqual([m.operation_id for m in result.mutations], ["w", "dir"])
        self.assertFalse((self.seat / "d").exists())  # emptied first, then removed

    def test_rollback_is_idempotent_on_redispatch(self) -> None:
        directory = fx.op("dir", "mkdir", str(self.seat / "d"), {"mode": "0755"})
        write = fx.content_op("w", "atomic-write", self.seat / "d" / "f", MARK + "v1\n")
        mutations = self.apply(directory, write)
        first = self.rollback((directory, write), mutations)
        self.assertTrue(first.ok, first.evidence)
        second = self.rollback((directory, write), mutations)
        self.assertTrue(second.ok, second.evidence)
        self.assertEqual(second.mutations, ())
        self.assertTrue(all(item.detail.startswith("not-applied") for item in second.evidence))

    def test_rollback_records_are_never_dropped(self) -> None:
        target = self.seat / "managed.conf"
        target.write_text(MARK + "old\n", encoding="utf-8")
        write = fx.content_op("w", "atomic-write", target, MARK + "new\n")
        mutations = self.apply(write)
        real = self.engine._files.restore_file

        def committed_then_failed(*args, **kwargs):
            real(*args, **kwargs)
            raise SeatChangeIncomplete("file replaced but directory sync failed: OSError")

        with mock.patch.object(self.engine._files, "restore_file", side_effect=committed_then_failed):
            result = self.rollback((write,), mutations)
        self.assertFalse(result.ok)
        self.assertEqual([m.operation_id for m in result.mutations], ["w"])
        self.assertTrue(result.mutations[0].changed)
        self.assertIn("restored, then failed", result.evidence[0].detail)
        self.assertEqual(target.read_text(encoding="utf-8"), MARK + "old\n")


class ExecutorTimerRollbackTests(_ActivationCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake.units[TIMER] = _timer_state()
        self.fake.units[SERVICE] = _service_state()

    def owned_enable(self):
        def enable_creates_link(fake, name, args):
            if name == "enable_now":
                self.make_link()
                fake.units[TIMER] = _timer_state(active="active", sub="waiting", unit_file="enabled", monotonic=5)

        self.fake.on_call = enable_creates_link
        operation = self.timer_enable_op()
        result = self.run_ops(operation)
        self.assertTrue(result.ok, result.evidence)
        self.fake.on_call = None
        self.fake.calls.clear()
        return operation, result.mutations[0]

    def test_timer_enable_restore_sequence_matches_the_installer(self) -> None:
        operation, mutation = self.owned_enable()

        def systemd(fake, name, args):
            if name == "stop_unit" and args[0] == TIMER:
                fake.units[TIMER] = _timer_state(unit_file="enabled")
            if name == "disable_now":
                fake.units[TIMER] = _timer_state(unit_file="disabled")

        self.fake.on_call = systemd
        result = self.engine().run(_rollback_spec(self.fixture, (operation,), (mutation,)))
        self.assertTrue(result.ok, result.evidence)
        self.assertEqual(
            self.write_calls(),
            [
                f"stop_unit:{TIMER}",
                f"stop_unit:{SERVICE}",
                f"disable_now:{TIMER}",
                "reload:",
                f"reset_failed_unit:{TIMER}",
                f"reset_failed_unit:{SERVICE}",
            ],
        )
        self.assertFalse(os.path.lexists(self.link))  # the receipt-owned link is gone
        (record,) = result.mutations
        self.assertEqual((record.changed, record.after_fingerprint), (True, "timer-disabled"))
        self.assertEqual(record.before["enablement_link_state"], "symlink")

    def test_executor_refuses_timer_restore_without_owned_record(self) -> None:
        operation, mutation = self.owned_enable()
        for change in ({"owned": "false"}, {"activation_intent": "true"}, {"state": "enabled"}):
            with self.subTest(change=change):
                forged = MutationRecord(
                    mutation.operation_id, mutation.kind, mutation.target, mutation.owner, True,
                    dict(mutation.before, **change), mutation.after_fingerprint,
                )
                result = self.engine().run(_rollback_spec(self.fixture, (operation,), (forged,)))
                self.assertFalse(result.ok)
                self.assertEqual(result.mutations, ())
                self.assertIn("refused", result.detail)
        self.assertEqual(self.write_calls(), [])
        self.assertTrue(os.path.lexists(self.link))


if __name__ == "__main__":  # must stay LAST
    unittest.main()
