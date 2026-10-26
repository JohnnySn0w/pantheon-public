"""T-2917 5d.1 hardening (ORG-052 Unit 5): capability-neutral fixes carried
into 5d.

* N4: the result-size arithmetic that makes ``encode_result``'s
  "mutations alone oversize" branch unreachable;
* N5d (ruling T-2917:D23): the Codex standing instructions pin, checked at
  parse and again at apply;
* the hard per-execution deadline (5c review L3, ruling T-2917:D11): the
  shared per-call clamp and the watchdog exit;
* N5a (ruling T-2917:D18): the LIFETIME verify re-dispatch cap;
* gap m3 (ruling T-2917:D14): the executor's ``refused`` state and its
  journal-based classification at the broker.

Fixture evidence only (fakes, a disposable seat root, sqlite); the real user
bus and the real cross-uid paths are the bus tiers and T-2924.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import _host_control_executor_fixtures as fx
from tools.test_host_control_driver import (
    SUBJECT_UID,
    RECOVERY_UID,
    FakePort,
    _DomainCase,
    _ok,
)
from tools.test_host_control_executor_activation import (
    RECEIVER,
    SERVICE,
    TIMER,
    _Case as _ActivationCase,
    _service_state,
    _timer_state,
)
from tools.host_control.contracts import canonical_json, sha256_json
from tools.host_control.driver import (
    LIFETIME_VERIFY_REDISPATCH_CAP,
    VERIFY_CAP_DETAIL,
    DriverContext,
    DriverCore,
)
from tools.host_control.execution_source import BrokerExecutionSource
from tools.host_control.executor import engine as engine_module
from tools.host_control.executor import wire as executor_wire
from tools.host_control.executor.dbus_service import ExecutorService
from tools.host_control.executor.engine import EXECUTION_SPEC_SCHEMA
from tools.host_control.executor.user_bus import DEFAULT_CALL_TIMEOUT
from tools.host_control.executor.wire import (
    EVIDENCE_RESERVE_BYTES,
    EXIT_HARD_DEADLINE,
    HARD_DEADLINE_GRACE_SECONDS,
    RECORD_BUDGET_BYTES,
    RESULT_JSON_LIMIT,
    ExecutionResult,
    InvalidSpec,
    encode_result,
    record_size,
)
from tools.host_control.executor_client import (
    CollectedResult,
    DBusExecutorPort,
    ExecutorUnknownExecution,
)
from tools.persona_installer import MutationRecord, OperationEvidence

BROKER_UID = 5555
BROKER_UNIQUE = ":1.42"


# ---------------------------------------------------------------------------
# N4: the result budget arithmetic
# ---------------------------------------------------------------------------


class ResultBudgetArithmeticTests(unittest.TestCase):
    def test_n4_mutations_alone_cannot_exceed_the_result_limit(self) -> None:
        # the envelope of an empty result, measured, not assumed
        empty = encode_result(
            operation_handle="ab" * 32,
            spec_digest="cd" * 32,
            result=ExecutionResult(ok=False, mutations=(), evidence=(), detail="x" * 512),
        )
        envelope = len(empty.encode("utf-8"))
        # the engine admits records up to RECORD_BUDGET_BYTES, plus at most
        # one post-commit record whose size was projected (it embeds the
        # before-snapshot; the after-fingerprint and the evidence reserve
        # bound the difference)
        self.assertLess(RECORD_BUDGET_BYTES + EVIDENCE_RESERVE_BYTES + envelope, RESULT_JSON_LIMIT)
        # empirically: mutations filling the whole budget encode under the
        # limit WITHOUT degrading (the mutations-only branch is not taken)
        content = "A" * (200 * 1024)
        mutations = []
        used = 0
        index = 0
        while True:
            mutation = MutationRecord(
                f"w{index}", "atomic-write", f"/seat/f{index}", fx.ACCOUNT, True,
                {"state": "file", "content_b64": content, "fingerprint": "f" * 64, "mode": "0644"},
                "e" * 64,
            )
            size = record_size(mutation, None)
            if used + size > RECORD_BUDGET_BYTES:
                break
            mutations.append(mutation)
            used += size
            index += 1
        evidence = (OperationEvidence("w0", 1, "atomic-write", True, "ok", {}),)
        encoded = encode_result(
            operation_handle="ab" * 32,
            spec_digest="cd" * 32,
            result=ExecutionResult(ok=True, mutations=tuple(mutations), evidence=evidence, detail="d"),
        )
        self.assertLessEqual(len(encoded.encode("utf-8")), RESULT_JSON_LIMIT)
        value = json.loads(encoded)
        self.assertTrue(value["ok"])  # nothing degraded
        self.assertEqual(len(value["mutations"]), len(mutations))
        self.assertEqual(len(value["evidence"]), 1)


# ---------------------------------------------------------------------------
# N5d: the Codex standing instructions pin (T-2917:D23)
# ---------------------------------------------------------------------------


class CodexPinTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.fixture = fx.build_executor_fixture(Path(temp.name) / "x")
        self.addCleanup(self.fixture.close)
        self.seat = self.fixture.seat_root
        self.content = "standing instructions\n"  # the framework copy's bytes
        self.pin = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        self.target = self.seat / "AGENTS.md"

    def source_check(self, sha: str | None = None):
        return fx.op(
            "codex-standing-instructions.source-check",
            "codex-standing-instructions-source-check",
            str(self.fixture.framework_root / "codex" / "standing.md"),
            {"source_sha256": sha or self.pin, "source_commit": "c" * 40},
        )

    def install(self, content: str | None = None):
        return fx.content_op(
            "codex-standing-instructions.install",
            "codex-standing-instructions-install",
            self.target,
            content or self.content,
        )

    def target_check(self, sha: str | None = None):
        return fx.op(
            "codex-standing-instructions.target-check",
            "codex-standing-instructions-target-check",
            str(self.target),
            {"sha256": sha or self.pin},
        )

    def test_install_pin_must_equal_source_check_sha_at_parse(self) -> None:
        with self.assertRaises(InvalidSpec) as caught:
            fx.parsed(self.fixture, (self.source_check(sha="0" * 64), self.install(), self.target_check()))
        self.assertIn("source check", str(caught.exception))
        # a second install is refused too
        second = fx.content_op("second", "codex-standing-instructions-install", self.seat / "B.md", self.content)
        with self.assertRaises(InvalidSpec):
            fx.parsed(self.fixture, (self.source_check(), self.install(), second))
        # the agreeing triple parses
        fx.parsed(self.fixture, (self.source_check(), self.install(), self.target_check()))

    def test_target_check_pin_must_agree(self) -> None:
        with self.assertRaises(InvalidSpec) as caught:
            fx.parsed(self.fixture, (self.source_check(), self.install(), self.target_check(sha="1" * 64)))
        self.assertIn("target check", str(caught.exception))

    def test_install_refused_when_framework_copy_hash_differs_at_apply(self) -> None:
        engine = self.fixture.engine()
        # (1) the observed framework hash differs from the pin: refused
        # BEFORE the write (the source check is patched to report a passing
        # observation of DIFFERENT bytes: defence in depth behind the parse)
        real = engine_module.ExecutorEngine._codex_source_check

        def drifted(self_engine, operation):
            observed = real(self_engine, operation)
            return OperationEvidence(
                observed.operation_id, observed.layer, observed.kind, True, "patched",
                dict(observed.evidence, source_sha256="9" * 64),
            )

        spec = fx.parsed(self.fixture, (self.source_check(), self.install(), self.target_check()))
        with mock.patch.dict(engine_module.ExecutorEngine._DIAGNOSTICS, {"codex-standing-instructions-source-check": drifted}):
            result = engine.run(spec)
        self.assertFalse(result.ok)
        self.assertFalse(any(m.changed for m in result.mutations))
        self.assertNotIn("codex-standing-instructions.install", [m.operation_id for m in result.mutations])
        self.assertIn("observed hash differs", result.evidence[-1].detail)
        self.assertFalse(self.target.exists())
        # (2) no source check in the spec at all: refused at apply
        result = engine.run(fx.parsed(self.fixture, (self.install(),)))
        self.assertFalse(result.ok)
        self.assertIn("no passing source check", result.evidence[-1].detail)
        self.assertFalse(self.target.exists())
        # (3) green: the real source check observed the pinned bytes
        result = engine.run(spec)
        self.assertTrue(result.ok, result.evidence)
        self.assertEqual(self.target.read_text(encoding="utf-8"), self.content)
        self.assertEqual([m.operation_id for m in result.mutations if m.changed], ["codex-standing-instructions.install"])


# ---------------------------------------------------------------------------
# the hard per-execution deadline (T-2917:D11)
# ---------------------------------------------------------------------------


class HardDeadlineTests(_ActivationCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake.units[SERVICE] = _service_state()
        self.fake.units[TIMER] = _timer_state(active="active", sub="waiting", unit_file="enabled", monotonic=7)
        self.fake.units[RECEIVER] = _service_state()

    def read_ops(self):
        return (
            self.disabled_first_op(),
            self.reload_op(),
            self.unit_fire_op(),
            self.timer_observe_op(),
            self.stale_op(),
        )

    def test_every_port_call_receives_a_timeout_within_the_remaining_budget(self) -> None:
        engine = self.engine(run_budget=100)
        observed: list[tuple[str, float | None, float]] = []
        timed = ("read_unit", "unit_file_enablement", "start_unit", "stop_unit")

        def on_call(fake, name, args):
            if name in timed:
                observed.append((name, fake.timeouts[-1][1], engine._deadline - self.clock()))
            self.clock.now += 7.0  # each call takes time

        self.fake.on_call = on_call
        self.run_ops(*self.read_ops(), kind="verify", engine=engine)
        self.assertTrue(observed)
        for name, timeout, remaining in observed:
            with self.subTest(call=name, remaining=remaining):
                self.assertIsNotNone(timeout)
                self.assertLessEqual(timeout, DEFAULT_CALL_TIMEOUT)
                self.assertLessEqual(timeout, remaining + 1e-9)
        # the deadline was bound into the port for the run and cleared after
        self.assertEqual(len(self.fake.bound_deadlines), 2)
        self.assertIsNotNone(self.fake.bound_deadlines[0])
        self.assertIsNone(self.fake.bound_deadlines[1])

    def test_bound_deadline_clamps_untimed_port_methods(self) -> None:
        try:
            import dbus  # noqa: F401
            import dbus.exceptions  # noqa: F401
        except ImportError:
            self.skipTest("dbus-python is required for the adapter's call path")
        from tools.host_control.executor.user_bus import DEADLINE_REACHED, DBusUserManager

        recorded: list[tuple[str, float]] = []

        class Connection:
            def call_blocking(self, owner, path, interface, method, signature, args, *, timeout, byte_arrays=False):
                recorded.append((method, timeout))
                if method == "EnableUnitFiles":
                    return (False, [])
                if method == "StopUnit" or method == "StartUnit":
                    return "/org/freedesktop/systemd1/job/9"
                if method == "GetJob":
                    import dbus

                    raise dbus.exceptions.DBusException("gone", name="org.freedesktop.systemd1.NoSuchJob")
                return ""

        now = {"t": 500.0}
        with tempfile.TemporaryDirectory() as runtime:
            manager = DBusUserManager(uid=4242, runtime_root=Path(runtime), clock=lambda: now["t"], sleep=lambda s: None)
            with mock.patch.object(DBusUserManager, "_attested", return_value=(Connection(), ":1.5")):
                manager.bind_deadline(now["t"] + 5.0)
                for call in (
                    manager.reload,
                    lambda: manager.reset_failed_unit(SERVICE),
                    lambda: manager.enable_now(TIMER),
                    lambda: manager.disable_now(TIMER),
                ):
                    self.assertTrue(call().ok)
                self.assertTrue(recorded)
                for method, timeout in recorded:
                    with self.subTest(method=method):
                        self.assertLessEqual(timeout, 5.0)
                # past the deadline: every call is refused without reaching the bus
                recorded.clear()
                now["t"] += 6.0
                for call in (manager.reload, lambda: manager.enable_now(TIMER), lambda: manager.disable_now(TIMER)):
                    outcome = call()
                    self.assertFalse(outcome.ok)
                    self.assertEqual(outcome.error_name, DEADLINE_REACHED)
                self.assertEqual(recorded, [])
                # cleared: the per-call timeout applies again
                manager.bind_deadline(None)
                self.assertTrue(manager.reload().ok)
                self.assertEqual(recorded, [("Reload", DEFAULT_CALL_TIMEOUT)])

    def test_direct_fire_child_timeout_is_clamped(self) -> None:
        self.add_framework_entrypoints()
        timeouts: list[float] = []

        def run_child(argv, **kwargs):
            timeouts.append(kwargs["timeout"])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        result = self.run_ops(self.direct_fire_op(), engine=self.engine(run_child=run_child, run_budget=45))
        self.assertTrue(result.ok, result.evidence)
        self.assertEqual(timeouts, [45.0])
        result = self.run_ops(self.direct_fire_op(), engine=self.engine(run_child=run_child, run_budget=500))
        self.assertEqual(timeouts[-1], engine_module.DIRECT_FIRE_TIMEOUT_SECONDS)

    def test_operation_starting_at_the_deadline_overruns_by_at_most_one_clamped_call(self) -> None:
        engine = self.engine(run_budget=40)

        def on_call(fake, name, args):
            # every call blocks for its whole (clamped) timeout
            timeout = fake.timeouts[-1][1] if fake.timeouts and name in ("read_unit", "unit_file_enablement") else 0.0
            self.clock.now += float(timeout or 0.0)

        self.fake.on_call = on_call
        start = self.clock()
        result = self.run_ops(*self.read_ops(), kind="verify", engine=engine)
        deadline = start + 40
        self.assertFalse(result.ok)
        # no call outlived the deadline: the overrun is bounded by one call
        # clamped to the time left, i.e. zero on this fake clock
        self.assertLessEqual(self.clock() - deadline, 1e-9)
        self.assertTrue(any("budget" in item.detail for item in result.evidence), result.evidence)

    # -- the watchdog backstop --------------------------------------------

    def _service(self, engine, timers, exits):
        def watchdog(seconds, callback):
            timer = SimpleNamespace(seconds=seconds, callback=callback, cancelled=False)
            timer.cancel = lambda: setattr(timer, "cancelled", True)
            timers.append(timer)
            return timer

        payload = fx.spec_payload(self.fixture, (fx.op("dir", "mkdir", str(self.seat / "made"), {"mode": "0755"}),))
        source = SimpleNamespace(
            operation_spec=lambda unique, handle, digest: (
                EXECUTION_SPEC_SCHEMA, "apply", sha256_json(payload), canonical_json(payload)
            )
        )
        bus = SimpleNamespace(unix_user=lambda name: BROKER_UID, name_owner=lambda name: BROKER_UNIQUE)
        deferred: list = []
        service = ExecutorService(
            identity=self.fixture.identity,
            registry=self.fixture.registry,
            broker_uid=BROKER_UID,
            engine=engine,
            spec_source=source,
            bus=bus,
            start_worker=deferred.append,
            watchdog=watchdog,
            exit=exits.append,
        )
        return service, deferred, sha256_json(payload)

    def test_watchdog_exits_when_a_run_outlives_budget_plus_grace(self) -> None:
        timers: list = []
        exits: list[int] = []

        class Stuck:
            run_budget = 100.0

            def run(self, spec):
                # the run is still going when the timer fires
                timers[-1].callback()
                return ExecutionResult(ok=True, mutations=(), evidence=(), detail="late")

        service, deferred, digest = self._service(Stuck(), timers, exits)
        service.execute(BROKER_UNIQUE, fx.OPERATION_HANDLE, digest)
        deferred.pop(0)()
        self.assertEqual(timers[0].seconds, 100.0 + HARD_DEADLINE_GRACE_SECONDS)
        self.assertEqual(exits, [EXIT_HARD_DEADLINE])

    def test_watchdog_is_cancelled_when_the_run_finishes(self) -> None:
        timers: list = []
        exits: list[int] = []
        engine = self.fixture.engine()
        service, deferred, digest = self._service(engine, timers, exits)
        handle = service.execute(BROKER_UNIQUE, fx.OPERATION_HANDLE, digest)
        deferred.pop(0)()
        self.assertEqual(service.table.get(handle)[0], "succeeded")
        self.assertEqual(timers[0].seconds, executor_wire.EXECUTOR_RUN_BUDGET_SECONDS + HARD_DEADLINE_GRACE_SECONDS)
        self.assertTrue(timers[0].cancelled)
        timers[0].callback()  # a late fire after the run finished does nothing
        self.assertEqual(exits, [])


# ---------------------------------------------------------------------------
# N5a: the lifetime verify re-dispatch cap (T-2917:D18)
# ---------------------------------------------------------------------------


class _AlwaysUnknown(FakePort):
    def collect(self, operation, execution_ref):
        raise ExecutorUnknownExecution("gone")


class LifetimeVerifyCapTests(_DomainCase):
    def events(self, handle: str, kind: str) -> int:
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM operation_events WHERE operation_handle=? AND event_kind=?",
                (handle, kind),
            ).fetchone()[0]

    def make_core(self, port, **kwargs) -> DriverCore:
        return DriverCore(
            DriverContext(self.service, self.fixture.journal, port, lambda: None),
            sleeper=lambda seconds: None,
            **kwargs,
        )

    def failed_receipt(self, handle: str) -> dict:
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            row = connection.execute(
                "SELECT receipt_json FROM operation_receipts WHERE operation_handle=? AND phase='failed'",
                (handle,),
            ).fetchone()
        return json.loads(row[0])

    def test_n5_cap_counts_across_drives_and_restarts(self) -> None:
        handle = self.verify("n5-life")
        port = _AlwaysUnknown(self.fixture.journal)
        outcomes = []
        for _restart in range(LIFETIME_VERIFY_REDISPATCH_CAP + 1):
            # a NEW DriverCore each time: a broker restart forgets every
            # per-drive counter; only the journal remembers
            outcomes.append(self.make_core(port, max_verify_redispatches=1).drive(handle))
        self.assertEqual(outcomes, ["collect_pending"] * LIFETIME_VERIFY_REDISPATCH_CAP + ["failed"])
        # the first dispatch plus exactly the lifetime cap of re-dispatches
        self.assertEqual(len(port.executions), 1 + LIFETIME_VERIFY_REDISPATCH_CAP)
        self.assertEqual(self.events(handle, "redispatched"), LIFETIME_VERIFY_REDISPATCH_CAP)
        self.assertEqual(self.events(handle, "verify_redispatch_cap"), 1)
        self.assertEqual(self.state(handle), "failed")

    def test_n5_cap_terminates_verify_failed_with_no_mutations(self) -> None:
        handle = self.verify("n5-term")
        port = _AlwaysUnknown(self.fixture.journal)
        self.assertEqual(self.make_core(port).drive(handle), "collect_pending")  # per-drive bound first
        self.assertEqual(self.make_core(port).drive(handle), "failed")
        receipt = self.failed_receipt(handle)
        self.assertEqual((receipt["mutations"], receipt["evidence"]), ([], []))
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            payload = connection.execute(
                "SELECT payload_json FROM operation_events WHERE operation_handle=? AND event_kind='result'",
                (handle,),
            ).fetchone()[0]
        self.assertEqual(json.loads(payload)["detail"], VERIFY_CAP_DETAIL)
        # terminal: a further drive does nothing
        self.assertEqual(self.make_core(port).drive(handle), "terminal")
        self.assertEqual(len(port.executions), 1 + LIFETIME_VERIFY_REDISPATCH_CAP)

    def test_n5_apply_never_takes_the_verify_cap_path(self) -> None:
        handle = self.apply("n5-apply")
        port = _AlwaysUnknown(self.fixture.journal)
        for _ in range(LIFETIME_VERIFY_REDISPATCH_CAP + 2):
            self.assertEqual(self.make_core(port).drive(handle), "awaiting_reconciler")
        self.assertEqual(len(port.executions), 1)
        self.assertEqual(self.events(handle, "verify_redispatch_cap"), 0)
        self.assertEqual(self.events(handle, "redispatched"), 0)
        self.assertEqual(self.state(handle), "dispatched")


# ---------------------------------------------------------------------------
# gap m3: the executor's ``refused`` state (T-2917:D14)
# ---------------------------------------------------------------------------


def _refused() -> CollectedResult:
    return CollectedResult(
        ok=False,
        result=ExecutionResult(ok=False, mutations=(), evidence=(), detail="refused before execution: InvalidSpec: x"),
        refused=True,
    )


class _ServingPort(FakePort):
    """Fetches the spec the way the real executor's worker does, right after
    Execute (while the operation is still DISPATCH_INTENT)."""

    def __init__(self, journal, registry) -> None:
        super().__init__(journal)
        self.source = BrokerExecutionSource(journal=journal, registry=registry)

    def execute(self, operation, staged_receipt, intents) -> str:
        reference = super().execute(operation, staged_receipt, intents)
        self.source.operation_spec(RECOVERY_UID, operation.operation_handle, self.source.expected_digest(operation))
        return reference


class RefusedStateTests(_DomainCase):
    def receipts(self, handle: str) -> dict[str, dict]:
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            rows = connection.execute(
                "SELECT phase, receipt_json FROM operation_receipts WHERE operation_handle=?", (handle,)
            ).fetchall()
        return {phase: json.loads(raw) for phase, raw in rows}

    def make_core(self, port) -> DriverCore:
        # collect_timeout=0: a still-running execution returns at once
        return DriverCore(
            DriverContext(self.service, self.fixture.journal, port, lambda: None),
            sleeper=lambda seconds: None,
            collect_timeout=0.0,
        )

    def test_pre_engine_refusal_is_state_refused_not_failed(self) -> None:
        # the wire admits the state as terminal
        self.assertIn("refused", executor_wire.EXECUTION_STATES)
        self.assertIn("refused", executor_wire.TERMINAL_EXECUTION_STATES)
        # the broker client reports it as refused, never as a clean failure
        handle = self.verify("refused-client")
        self.service.authorize(handle)
        record = self.service.dispatch(handle, FakePort(self.fixture.journal))
        source = BrokerExecutionSource(journal=self.fixture.journal, registry=self.fixture.registry)
        raw = encode_result(
            operation_handle=handle,
            spec_digest=source.expected_digest(record),
            result=ExecutionResult(ok=False, mutations=(), evidence=(), detail="refused before execution"),
        )
        transport = SimpleNamespace(
            name_owner=lambda name: ":1.77",
            unix_user=lambda unique: RECOVERY_UID,
            call=lambda unique, method, signature, args: ("refused", raw),
        )
        port = DBusExecutorPort(registry=self.fixture.registry, execution_source=source, transport=transport)
        collected = port.collect(record, "e" * 64)
        self.assertTrue(collected.refused)
        self.assertFalse(collected.ok)

    def test_spec_already_served_refusal_is_never_a_clean_failed_receipt(self) -> None:
        handle = self.apply("refused-served")
        port = _ServingPort(self.fixture.journal, self.fixture.registry)
        port.collect_script = [None]
        self.assertEqual(self.make_core(port).drive(handle), "collect_pending")
        record = self.fixture.journal.load_operation_unbound(handle)
        # a later pre-Execute marker (a re-dispatch), then a refused result:
        # the SERVE was refused (already served to the earlier attempt)
        self.fixture.journal.append_event(
            current=record, event_kind="execute_retry", payload={"test": True}, created_at=self.service.now_iso()
        )
        port.collect_script = [_refused()]
        self.assertEqual(self.make_core(port).drive(handle), "awaiting_reconciler")
        self.assertEqual(self.state(handle), "dispatched")
        self.assertNotIn("failed", self.receipts(handle))

    def test_parse_refusal_after_a_fresh_serve_ends_failed_with_no_mutations(self) -> None:
        handle = self.apply("refused-fresh")
        port = _ServingPort(self.fixture.journal, self.fixture.registry)
        port.collect_script = [_refused()]
        self.assertEqual(self.make_core(port).drive(handle), "failed")
        failed = self.receipts(handle)["failed"]
        self.assertEqual((failed["mutations"], failed["evidence"]), ([], []))

    def test_never_served_refusal_ends_failed_with_no_mutations(self) -> None:
        # the executor refused before fetching (e.g. the broker identity
        # re-check): no spec was ever handed out, so nothing can have run
        handle = self.apply("refused-unserved")
        port = FakePort(self.fixture.journal)
        port.collect_script = [_refused()]
        self.assertEqual(self.make_core(port).drive(handle), "failed")
        self.assertEqual(self.receipts(handle)["failed"]["mutations"], [])


if __name__ == "__main__":  # must stay LAST
    unittest.main()
