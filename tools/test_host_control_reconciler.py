"""T-2917 5d.2: the broker live-state reconciler (T-2917:D5, D6, D12, D13),
the journaled spec re-arm (carry-in N5b, T-2917:D17), the read-only
``reconcile`` probe spec, and the driver routing that replaces the 5c
``awaiting_reconciler`` dead end.

Fixture evidence: the real broker domain (journal, planner, operation
service, execution source) with a fake executor port and a fake probe; the
executor engine's reconcile run uses the disposable seat-root fixture.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tools import _host_control_executor_fixtures as fx
from tools.test_host_control_driver import (
    SUBJECT_UID,
    RECOVERY_UID,
    FakePort,
    _DomainCase,
    _ok,
)
from tools.host_control.contracts import (
    HostControlError,
    InvalidTransition,
    RecoveryRefused,
    RollbackIntent,
)
from tools.host_control.driver import DriverContext, DriverCore
from tools.host_control.envelope import EXECUTOR_KIND_CLASSES
from tools.host_control.execution_source import BrokerExecutionSource
from tools.host_control.executor.dbus_service import ExecutionTable
from tools.host_control.executor.wire import ExecutorDenied, ExecutorBusy
from tools.host_control.executor_client import ExecutorAmbiguous, ExecutorUnknownExecution
from tools.host_control.journal import MAX_SPEC_REARMS
from tools.host_control.operations import (
    SYNTHESIZED_TIMER_ENABLE_INVERSE,
    OperationService,
)
from tools.host_control.reconciler import (
    MUTATING_CLASSES,
    BrokerLiveStateReconciler,
    ProbeReport,
)
from tools.persona_installer import MutationRecord, OperationEvidence

REPO_ROOT = Path(__file__).resolve().parent.parent
HOST_CONTROL = REPO_ROOT / "tools" / "host_control"


class FakeProbe:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.at_desired = "false"
        self.complete = True
        self.extra: tuple[OperationEvidence, ...] = ()

    def probe(self, operation):
        self.calls.append(operation.operation_handle)
        return ProbeReport(evidence=self.observations + self.extra, complete=self.complete, detail="fake probe")

    observations: tuple[OperationEvidence, ...] = ()


class ServingPort(FakePort):
    """Fetches the mutating spec right after Execute, the way the executor's
    worker does; a refused fetch is remembered (the executor would report
    state ``refused``)."""

    def __init__(self, journal, registry) -> None:
        super().__init__(journal)
        self.source = BrokerExecutionSource(journal=journal, registry=registry)
        self.serve_refusals = 0

    def execute(self, operation, staged_receipt, intents) -> str:
        durable = self.journal.load_operation_unbound(operation.operation_handle)
        self.executions.append((operation.operation_handle, durable.state, staged_receipt.phase, len(intents)))
        try:
            self.source.operation_spec(RECOVERY_UID, operation.operation_handle, self.source.expected_digest(operation))
        except InvalidTransition:
            self.serve_refusals += 1
        if self.execute_errors:  # e.g. the reply is lost AFTER the fetch
            raise self.execute_errors.pop(0)
        return format(len(self.executions), "064x")


class _ReconcileCase(_DomainCase):
    def setUp(self) -> None:
        super().setUp()
        self.probe = FakeProbe()
        self.reconciler = BrokerLiveStateReconciler(
            journal=self.fixture.journal,
            probe=self.probe,
            now=lambda: self.service.now_iso(),
        )
        self.service = OperationService(
            registry=self.fixture.registry,
            journal=self.fixture.journal,
            binder=self.binder,
            reconciler=self.reconciler,
            clock=self.fixture.clock,
            handle_factory=self._handles(),
        )
        plan = self.fixture.journal.load_plan_unbound(self.plan.plan_handle)
        self.first_mutating = next(
            op for op in plan.installer_plan.operations if EXECUTOR_KIND_CLASSES.get(op.kind) in MUTATING_CLASSES
        )
        self.plan_ops = plan.installer_plan.operations

    @staticmethod
    def _handles():
        counter = {"n": 1 << 44}

        def next_handle() -> str:
            counter["n"] += 1
            return format(counter["n"], "064x")

        return next_handle

    def make_core(self, port) -> DriverCore:
        return DriverCore(
            DriverContext(self.service, self.fixture.journal, port, lambda: None, reconcile=True),
            sleeper=lambda seconds: None,
            collect_timeout=0.0,
        )

    def set_observation(self, at_desired: str) -> None:
        self.probe.observations = (
            OperationEvidence(
                self.first_mutating.operation_id,
                self.first_mutating.layer,
                self.first_mutating.kind,
                True,
                "live target observed",
                {"observation": "file-target", "state": "absent", "at_desired": at_desired},
            ),
        )

    def events(self, handle: str, kind: str) -> list[dict]:
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            rows = connection.execute(
                "SELECT payload_json FROM operation_events WHERE operation_handle=? AND event_kind=? ORDER BY seq",
                (handle, kind),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def served_and_lost(self, key: str) -> tuple[str, ServingPort]:
        """An apply whose spec was served and whose execution then vanished."""

        handle = self.apply(key)
        port = ServingPort(self.fixture.journal, self.fixture.registry)
        port.collect_script = [None]
        self.assertEqual(self.make_core(port).drive(handle), "collect_pending")
        self.assertEqual(len(self.events(handle, "spec_served")), 1)
        return handle, port


# ---------------------------------------------------------------------------
# journal: the re-arm
# ---------------------------------------------------------------------------


class RearmTests(_ReconcileCase):
    def dispatched_apply(self, key: str):
        handle = self.apply(key)
        self.service.authorize(handle)
        record = self.service.dispatch(handle, FakePort(self.fixture.journal))
        source = BrokerExecutionSource(journal=self.fixture.journal, registry=self.fixture.registry)
        return record, source, source.expected_digest(record)

    def test_rearm_without_a_prior_serve_is_refused(self) -> None:
        record, _source, _digest = self.dispatched_apply("rearm-none")
        with self.assertRaises(RecoveryRefused):
            self.fixture.journal.rearm_spec_serve(
                current=record, reason="reconcile-probe-clean", created_at=self.service.now_iso()
            )
        self.assertEqual(self.events(record.operation_handle, "spec_rearmed"), [])

    def test_serve_after_rearm_is_exactly_once(self) -> None:
        record, source, digest = self.dispatched_apply("rearm-once")
        source.operation_spec(RECOVERY_UID, record.operation_handle, digest)
        with self.assertRaises(InvalidTransition):
            source.operation_spec(RECOVERY_UID, record.operation_handle, digest)
        self.fixture.journal.rearm_spec_serve(
            current=record, reason="reconcile-probe-clean", created_at=self.service.now_iso()
        )
        source.operation_spec(RECOVERY_UID, record.operation_handle, digest)  # one more serve
        with self.assertRaises(InvalidTransition):
            source.operation_spec(RECOVERY_UID, record.operation_handle, digest)
        self.assertEqual(len(self.events(record.operation_handle, "spec_served")), 2)

    def test_rearm_is_journaled_capped_and_unreachable_from_dbus_handlers(self) -> None:
        record, source, digest = self.dispatched_apply("rearm-cap")
        journal = self.fixture.journal
        with self.assertRaises(HostControlError):
            journal.rearm_spec_serve(current=record, reason="caller-chosen", created_at=self.service.now_iso())
        source.operation_spec(RECOVERY_UID, record.operation_handle, digest)
        for count in range(1, MAX_SPEC_REARMS + 1):
            self.assertEqual(
                journal.rearm_spec_serve(current=record, reason="rollback-redispatch", created_at=self.service.now_iso()),
                count,
            )
            # an outstanding arm cannot be doubled
            with self.assertRaises(RecoveryRefused):
                journal.rearm_spec_serve(current=record, reason="rollback-redispatch", created_at=self.service.now_iso())
            source.operation_spec(RECOVERY_UID, record.operation_handle, digest)
        with self.assertRaises(RecoveryRefused):
            journal.rearm_spec_serve(current=record, reason="rollback-redispatch", created_at=self.service.now_iso())
        rearms = self.events(record.operation_handle, "spec_rearmed")
        self.assertEqual([item["reason"] for item in rearms], ["rollback-redispatch"] * MAX_SPEC_REARMS)
        self.assertEqual(MAX_SPEC_REARMS, 2)
        # reachability: the ONLY call of rearm_spec_serve in the package is
        # OperationService._rearm_if_served, whose only caller is _reconcile;
        # no D-Bus handler module names either
        callers: list[tuple[str, str]] = []
        helper_callers: list[str] = []
        for path in sorted(HOST_CONTROL.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(function):
                    if isinstance(node, ast.Attribute) and node.attr == "rearm_spec_serve" and function.name != "rearm_spec_serve":
                        callers.append((path.name, function.name))
                    if isinstance(node, ast.Attribute) and node.attr == "_rearm_if_served":
                        helper_callers.append(function.name)
        self.assertEqual(callers, [("operations.py", "_rearm_if_served")])
        self.assertEqual(helper_callers, ["_reconcile"])
        for module in ("dbus_service.py", "health_dbus.py", "approval_source.py"):
            text = (HOST_CONTROL / module).read_text(encoding="utf-8")
            self.assertNotIn("rearm_spec_serve", text)
            self.assertNotIn("_rearm_if_served", text)


# ---------------------------------------------------------------------------
# the reconciler's decision table
# ---------------------------------------------------------------------------


class ReconcilerTests(_ReconcileCase):
    def test_journal_clean_spec_never_served_redispatches_without_rearm(self) -> None:
        handle = self.apply("journal-clean")
        port = FakePort(self.fixture.journal)  # never fetches: nothing served
        port.collect_script = [ExecutorUnknownExecution("restarted"), _ok()]
        self.assertEqual(self.make_core(port).drive(handle), "completed")
        self.assertEqual(len(port.executions), 2)
        self.assertEqual(self.events(handle, "spec_rearmed"), [])
        self.assertEqual(self.probe.calls, [])  # no live probe needed
        self.assertTrue(any(item.get("recovery") for item in self.events(handle, "redispatched")))

    def test_probe_clean_first_mutating_target_untouched_rearms_and_redispatches(self) -> None:
        handle, port = self.served_and_lost("probe-clean")
        self.set_observation("false")
        port.collect_script = [ExecutorUnknownExecution("restarted"), _ok()]
        self.assertEqual(self.make_core(port).drive(handle), "completed")
        self.assertEqual(self.probe.calls, [handle])
        self.assertEqual([item["reason"] for item in self.events(handle, "spec_rearmed")], ["reconcile-probe-clean"])
        self.assertEqual(len(self.events(handle, "spec_served")), 2)  # exactly one more serve
        self.assertEqual(len(self.events(handle, "reconcile_probe")), 1)
        self.assertEqual(port.serve_refusals, 0)

    def test_first_mutating_target_at_desired_parks_manual_recovery_with_report(self) -> None:
        handle, port = self.served_and_lost("at-desired")
        self.set_observation("true")
        port.collect_script = [ExecutorUnknownExecution("restarted")]
        self.assertEqual(self.make_core(port).drive(handle), "manual_recovery")
        self.assertEqual(self.state(handle), "manual_recovery")
        self.assertEqual(len(port.executions), 1)  # never re-dispatched
        (parked,) = self.events(handle, "irreconcilable")
        self.assertEqual(parked["detail"], "lost-result-mutating")
        self.assertEqual(parked["report"][0]["operation_id"], self.first_mutating.operation_id)
        self.assertEqual(parked["report"][0]["evidence"]["at_desired"], "true")
        # an incomplete probe is never proof of a clean host either
        handle2, port2 = self.served_and_lost("incomplete")
        self.set_observation("false")
        self.probe.complete = False
        port2.collect_script = [ExecutorUnknownExecution("restarted")]
        self.assertEqual(self.make_core(port2).drive(handle2), "manual_recovery")

    def _timer_intent(self, handle: str, seq: int) -> RollbackIntent:
        return RollbackIntent(
            operation_handle=handle,
            seq=seq,
            operation_id="connective.timer-enable",
            kind="timer-enable",
            target="pantheon-persona-consume@recovery.timer",
            owner="claude-fallback",
            inverse=dict(SYNTHESIZED_TIMER_ENABLE_INVERSE),
            created_at=self.service.now_iso(),
        )

    def test_d6_enabled_timer_without_owned_record_is_never_cleaned(self) -> None:
        handle, _port = self.served_and_lost("d6-enabled")
        record = self.fixture.journal.load_operation_unbound(handle)
        staged = self.fixture.journal.get_staged_receipt(handle)
        intents = self.fixture.journal.unresolved_intents(handle)
        timer = self._timer_intent(handle, len(intents) + 1)
        self.set_observation("true")
        self.probe.extra = (
            OperationEvidence("connective.timer-enable", 3, "timer-enable", True, "observed",
                              {"observation": "timer-enable", "classification": "enabled", "link_state": "symlink"}),
        )
        outcome = self.reconciler.reconcile(record, staged, intents + (timer,))
        self.assertEqual(outcome.outcome, "irreconcilable")
        self.assertEqual(outcome.mutations, ())
        d6 = [item for item in outcome.evidence if item.detail.startswith("d6-no-owned-record")]
        self.assertEqual([item.operation_id for item in d6], ["connective.timer-enable"])
        self.assertEqual(d6[0].evidence["action"], "none")

    def test_d6_reconciler_has_no_cleanup_path_for_a_synthesized_intent(self) -> None:
        # static: the module never constructs a restored/complete_poststate
        tree = ast.parse((HOST_CONTROL / "reconciler.py").read_text(encoding="utf-8"))
        outcomes = {
            node.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", "") == "ReconcileOutcome"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        }
        self.assertEqual(outcomes, {"clean_prestate", "irreconcilable"})
        # behavioural: whatever the probe says, a synthesized intent's
        # outcome is never a cleanup
        handle, _port = self.served_and_lost("d6-sweep")
        record = self.fixture.journal.load_operation_unbound(handle)
        staged = self.fixture.journal.get_staged_receipt(handle)
        timer = self._timer_intent(handle, 99)
        for at_desired in ("true", "false"):
            for complete in (True, False):
                with self.subTest(at_desired=at_desired, complete=complete):
                    self.set_observation(at_desired)
                    self.probe.complete = complete
                    outcome = self.reconciler.reconcile(record, staged, (timer,))
                    self.assertIn(outcome.outcome, ("clean_prestate", "irreconcilable"))
                    self.assertEqual(outcome.mutations, ())

    def test_rollback_served_and_lost_rearms_then_parks_at_cap(self) -> None:
        # an apply with one restorable mkdir, completed, then its rollback
        handle = self.apply("rb-src")
        made = MutationRecord(
            self.first_mutating.operation_id, self.first_mutating.kind, self.first_mutating.target,
            self.first_mutating.owner, True, {"state": "absent"}, "directory",
        )
        port = ServingPort(self.fixture.journal, self.fixture.registry)
        port.collect_script = [_ok((made.operation_id,), (made,))]
        self.assertEqual(self.make_core(port).drive(handle), "completed")
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            receipt = connection.execute(
                "SELECT receipt_handle FROM operation_receipts WHERE operation_handle=? AND phase='complete'", (handle,)
            ).fetchone()[0]
        rollback, _ = self.service.prepare_rollback(SUBJECT_UID, receipt, "rb-lost", "cd" * 32)
        rb = rollback.operation_handle
        outcomes = []
        for _ in range(MAX_SPEC_REARMS + 1):
            port.collect_script = [ExecutorUnknownExecution("gone"), ExecutorUnknownExecution("gone")]
            outcomes.append(self.make_core(port).drive(rb))
        self.assertEqual(outcomes, ["reconcile_pending"] * MAX_SPEC_REARMS + ["manual_recovery"])
        self.assertEqual(len(self.events(rb, "spec_rearmed")), MAX_SPEC_REARMS)
        self.assertEqual(len(self.events(rb, "spec_served")), MAX_SPEC_REARMS + 1)
        (parked,) = self.events(rb, "irreconcilable")
        self.assertEqual(parked["detail"], "rearm-cap")
        self.assertEqual(self.probe.calls, [])  # a rollback is re-dispatched, not probed

    def test_verify_is_never_routed_to_the_reconciler(self) -> None:
        handle = self.verify("v-never")
        port = FakePort(self.fixture.journal)
        port.collect_script = [ExecutorUnknownExecution("gone"), _ok()]
        core = DriverCore(
            DriverContext(self.service, self.fixture.journal, port, lambda: None, reconcile=True),
            sleeper=lambda seconds: None,
        )
        self.assertEqual(core.drive(handle), "completed")
        self.assertEqual(len(port.executions), 2)  # re-dispatched (read-only)
        self.assertEqual(self.probe.calls, [])
        self.assertEqual(self.events(handle, "reconcile_probe"), [])
        record = self.fixture.journal.load_operation_unbound(self.verify("v-direct"))
        with self.assertRaises(RecoveryRefused):
            self.reconciler.reconcile(record, None, ())

    def test_e29_p4_identical_retry_after_a_lost_execute_reply(self) -> None:
        handle = self.apply("p4")
        port = FakePort(self.fixture.journal)
        port.execute_errors = [ExecutorAmbiguous("reply lost")]
        port.collect_script = [_ok()]
        self.assertEqual(self.make_core(port).drive(handle), "completed")
        self.assertEqual(len(port.executions), 2)
        self.assertEqual(len(self.events(handle, "execute_retry")), 1)
        self.assertTrue(any(item.get("identical_retry") for item in self.events(handle, "dispatched")))
        self.assertEqual(self.events(handle, "spec_rearmed"), [])

    def test_identical_retry_to_a_restarted_executor_is_refused_then_reconciled(self) -> None:
        from tools.test_host_control_hardening import _refused

        handle = self.apply("p4-restart")
        port = ServingPort(self.fixture.journal, self.fixture.registry)
        port.execute_errors = [ExecutorAmbiguous("reply lost after the executor fetched")]
        # the retry reaches a RESTARTED executor: its fetch is refused
        port.collect_script = [_refused()]
        self.set_observation("true")
        self.assertEqual(self.make_core(port).drive(handle), "manual_recovery")
        self.assertEqual(port.serve_refusals, 1)
        self.assertEqual(self.probe.calls, [handle])


# ---------------------------------------------------------------------------
# T-2917:D27: a refusal is clean only with ONE lifetime Execute
# ---------------------------------------------------------------------------


class SingleLifetimeExecuteTests(_ReconcileCase):
    """Regression pins for the refused-classification race (review HIGH,
    ruling T-2917:D27): a serve after the latest pre-Execute marker is not
    proof that the REFUSED execution fetched once an earlier Execute may
    still be outstanding."""

    def receipts(self, handle: str) -> list[str]:
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT phase FROM operation_receipts WHERE operation_handle=?", (handle,)
                )
            ]

    def fresh_service(self) -> OperationService:
        return OperationService(
            registry=self.fixture.registry,
            journal=self.fixture.journal,
            binder=self.binder,
            reconciler=BrokerLiveStateReconciler(
                journal=self.fixture.journal, probe=self.probe, now=lambda: self.service.now_iso()
            ),
            clock=self.fixture.clock,
            handle_factory=self._handles(),
        )

    def fresh_core(self, port, *, reconcile: bool) -> DriverCore:
        # a broker restart: new service, new driver core; only the journal
        return DriverCore(
            DriverContext(self.fresh_service(), self.fixture.journal, port, lambda: None, reconcile=reconcile),
            sleeper=lambda seconds: None,
            collect_timeout=0.0,
        )

    def source(self) -> BrokerExecutionSource:
        return BrokerExecutionSource(journal=self.fixture.journal, registry=self.fixture.registry)

    def test_d27_a_identical_retry_race_is_never_a_clean_failure(self) -> None:
        from tools.test_host_control_hardening import _refused

        handle = self.apply("d27-a")
        source, test = self.source(), self

        class Port(FakePort):
            def execute(self, operation, staged_receipt, intents):
                if not self.executions:  # A0 reaches the executor; its reply is lost before it fetches
                    super().execute(operation, staged_receipt, intents)
                    raise ExecutorAmbiguous("reply lost")
                # the execute_retry marker is journaled; A0 now fetches (and
                # mutates), its executor dies, and the retry's fetch is refused
                source.operation_spec(RECOVERY_UID, operation.operation_handle, source.expected_digest(operation))
                with test.assertRaises(InvalidTransition):
                    source.operation_spec(RECOVERY_UID, operation.operation_handle, source.expected_digest(operation))
                return super().execute(operation, staged_receipt, intents)

        port = Port(self.fixture.journal)
        port.collect_script = [_refused()]
        self.set_observation("true")  # A0's mutation is visible
        self.assertEqual(self.make_core(port).drive(handle), "manual_recovery")
        self.assertEqual(self.fixture.journal.execute_issuance_count(handle), 2)
        self.assertNotIn("failed", self.receipts(handle))
        self.assertEqual(self.probe.calls, [handle])

    def test_d27_b_journal_clean_race_is_never_a_clean_failure(self) -> None:
        from tools.test_host_control_hardening import _refused

        handle, serving = self.served_and_lost("d27-b")
        self.set_observation("false")
        # drive 1: unknown -> probe-clean -> re-arm -> the re-armed Execute's reply is lost before it fetches
        lost = FakePort(self.fixture.journal)
        lost.collect_script = [ExecutorUnknownExecution("restarted")]
        lost.execute_errors = [ExecutorAmbiguous("reply lost")]
        self.assertEqual(self.make_core(lost).drive(handle), "reconcile_pending")
        source, test = self.source(), self

        class RacePort(FakePort):
            def execute(self, operation, staged_receipt, intents):
                # the journal-clean re-dispatch: the outstanding execution
                # fetches now (and runs), then this one's fetch is refused
                source.operation_spec(RECOVERY_UID, operation.operation_handle, source.expected_digest(operation))
                with test.assertRaises(InvalidTransition):
                    source.operation_spec(RECOVERY_UID, operation.operation_handle, source.expected_digest(operation))
                return super().execute(operation, staged_receipt, intents)

        race = RacePort(self.fixture.journal)
        race.collect_script = [ExecutorUnknownExecution("restarted"), _refused()]
        outcome = self.fresh_core(race, reconcile=True).drive(handle)
        self.assertEqual(outcome, "reconcile_pending")
        self.assertEqual(len(self.events(handle, "redispatch_intent")), 2)  # every re-dispatch is marked
        self.assertNotIn("failed", self.receipts(handle))
        self.assertEqual(self.state(handle), "dispatched")

    def test_d27_c_non_fetch_refusal_of_the_retry_is_never_a_clean_failure(self) -> None:
        from tools.test_host_control_hardening import _refused

        handle = self.apply("d27-c")
        source = self.source()

        class Port(FakePort):
            def execute(self, operation, staged_receipt, intents):
                if not self.executions:
                    super().execute(operation, staged_receipt, intents)
                    raise ExecutorAmbiguous("reply lost")
                # A0's serve lands AFTER the retry marker; the retry itself is
                # refused WITHOUT fetching (broker-identity / wrong-seat recheck)
                source.operation_spec(RECOVERY_UID, operation.operation_handle, source.expected_digest(operation))
                return super().execute(operation, staged_receipt, intents)

        port = Port(self.fixture.journal)
        port.collect_script = [_refused()]
        self.set_observation("true")
        self.assertEqual(self.make_core(port).drive(handle), "manual_recovery")
        self.assertTrue(self.fixture.journal.spec_served_after_latest_dispatch(handle))  # the old rule's trap
        self.assertNotIn("failed", self.receipts(handle))

    def test_d27_d_single_execute_refusal_still_ends_failed_with_no_mutations(self) -> None:
        from tools.test_host_control_hardening import _refused

        handle = self.apply("d27-d")
        port = ServingPort(self.fixture.journal, self.fixture.registry)
        port.collect_script = [_refused()]
        self.assertEqual(self.fresh_core(port, reconcile=True).drive(handle), "failed")
        self.assertEqual(self.fixture.journal.execute_issuance_count(handle), 1)
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            failed = json.loads(
                connection.execute(
                    "SELECT receipt_json FROM operation_receipts WHERE operation_handle=? AND phase='failed'", (handle,)
                ).fetchone()[0]
            )
        self.assertEqual((failed["mutations"], failed["evidence"]), ([], []))
        self.assertEqual(self.probe.calls, [])

    def test_d27_e_cross_generation_serve_of_the_rearmed_spec(self) -> None:
        from tools.test_host_control_hardening import _refused

        handle, _serving = self.served_and_lost("d27-e")
        self.set_observation("false")
        source, test = self.source(), self

        class Port(FakePort):
            def execute(self, operation, staged_receipt, intents):
                # after the re-arm: the OUTSTANDING pre-re-arm execution
                # serves the re-armed spec; the re-armed Execute is refused
                source.operation_spec(RECOVERY_UID, operation.operation_handle, source.expected_digest(operation))
                with test.assertRaises(InvalidTransition):
                    source.operation_spec(RECOVERY_UID, operation.operation_handle, source.expected_digest(operation))
                return super().execute(operation, staged_receipt, intents)

        port = Port(self.fixture.journal)
        port.collect_script = [ExecutorUnknownExecution("restarted"), _refused()]
        self.assertEqual(self.make_core(port).drive(handle), "reconcile_pending")
        self.assertEqual(len(self.events(handle, "spec_rearmed")), 1)
        self.assertTrue(self.fixture.journal.spec_served_after_latest_dispatch(handle))  # the old rule's trap
        self.assertNotIn("failed", self.receipts(handle))
        # the next drive (a restart) reconciles: the re-armed spec WAS served,
        # the host is observed changed -> loud manual_recovery
        self.set_observation("true")
        port.collect_script = [_refused()]
        self.assertEqual(self.fresh_core(port, reconcile=True).drive(handle), "manual_recovery")
        self.assertNotIn("failed", self.receipts(handle))

    def test_d27_opt_out_parks_loudly_with_more_than_one_execute(self) -> None:
        from tools.test_host_control_hardening import _refused

        handle = self.apply("d27-off")
        port = FakePort(self.fixture.journal)
        port.execute_errors = [ExecutorAmbiguous("reply lost")]
        port.collect_script = [None]  # the identical retry is still running
        self.assertEqual(self.make_core(port).drive(handle), "collect_pending")
        self.assertEqual(self.fixture.journal.execute_issuance_count(handle), 2)
        port.collect_script = [_refused()]
        for _ in range(2):  # restarts without a reconciler
            self.assertEqual(self.fresh_core(port, reconcile=False).drive(handle), "awaiting_reconciler")
            port.collect_script = [_refused()]
        self.assertEqual(self.state(handle), "dispatched")
        self.assertNotIn("failed", self.receipts(handle))
        self.assertEqual(self.probe.calls, [])


# ---------------------------------------------------------------------------
# the read-only reconcile probe spec
# ---------------------------------------------------------------------------


class ReconcileProbeSpecTests(_ReconcileCase):
    def test_execution_kinds_are_transcribed_on_both_sides(self) -> None:
        from tools.host_control import execution_source
        from tools.host_control.executor import engine

        self.assertEqual(engine.EXECUTION_KINDS, execution_source.EXECUTION_KINDS)
        self.assertIn("reconcile", engine.EXECUTION_KINDS)

    def test_reconcile_probe_spec_is_read_only_and_refetchable(self) -> None:
        handle = self.apply("probe-spec")
        self.service.authorize(handle)
        record = self.service.dispatch(handle, FakePort(self.fixture.journal))
        source = BrokerExecutionSource(journal=self.fixture.journal, registry=self.fixture.registry)
        probe_digest = source.reconcile_digest(record)
        self.assertNotEqual(probe_digest, source.expected_digest(record))
        for _ in range(3):
            schema, kind, digest, spec_json = source.operation_spec(RECOVERY_UID, handle, probe_digest)
            self.assertEqual((kind, digest), ("reconcile", probe_digest))
            spec = json.loads(spec_json)
            self.assertEqual(spec["execution_kind"], "reconcile")
            self.assertEqual(spec["rollback_mutations"], [])
        self.assertEqual(self.events(handle, "spec_served"), [])  # never claimed
        # the mutating spec is still served exactly once afterwards
        source.operation_spec(RECOVERY_UID, handle, source.expected_digest(record))
        with self.assertRaises(InvalidTransition):
            source.operation_spec(RECOVERY_UID, handle, source.expected_digest(record))
        # a verify has no reconcile probe spec
        verify = self.fixture.journal.load_operation_unbound(self.verify("probe-verify"))
        with self.assertRaises(HostControlError):
            source.reconcile_spec_for(verify)

    def test_reconcile_probe_returns_no_mutation_records(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = fx.build_executor_fixture(Path(temp) / "x")
            try:
                seat = fixture.seat_root
                operations = (
                    fx.op("dir", "mkdir", str(seat / "d"), {"mode": "0755"}),
                    fx.content_op("file", "atomic-write", seat / "f", "# Managed by pantheon persona installer\n"),
                    fx.op("harness", "harness-check", "codex"),
                )
                engine = fixture.engine()
                probe = engine.run(fx.parsed(fixture, operations, execution_kind="reconcile"))
                self.assertEqual(probe.mutations, ())
                self.assertTrue(probe.ok, probe.evidence)
                self.assertEqual(sorted(p.name for p in seat.iterdir()), [])  # nothing changed
                observed = {item.operation_id: item.evidence for item in probe.evidence}
                self.assertEqual(observed["dir"]["at_desired"], "false")
                self.assertEqual(observed["file"]["at_desired"], "false")
                self.assertEqual(observed["harness"]["observation"], "not-probed")
                self.assertTrue(engine.run(fx.parsed(fixture, operations)).ok)
                after = engine.run(fx.parsed(fixture, operations, execution_kind="reconcile"))
                observed = {item.operation_id: item.evidence for item in after.evidence}
                self.assertEqual((observed["dir"]["at_desired"], observed["file"]["at_desired"]), ("true", "true"))
                self.assertEqual(after.mutations, ())
            finally:
                fixture.close()

    def test_executor_table_runs_a_probe_digest_after_the_operation_finished(self) -> None:
        counter = iter(range(1, 100))
        table = ExecutionTable(handle_factory=lambda: format(next(counter), "064x"))
        apply_entry, created = table.submit("ab" * 32, "11" * 32)
        self.assertTrue(created)
        with self.assertRaises(ExecutorDenied):  # same handle, other digest, while running
            table.submit("ab" * 32, "22" * 32)
        with self.assertRaises(ExecutorBusy):
            table.submit("cd" * 32, "33" * 32)
        table.finish(apply_entry.execution_handle, "succeeded", "{}")
        probe_entry, created = table.submit("ab" * 32, "22" * 32)
        self.assertTrue(created)
        self.assertNotEqual(probe_entry.execution_handle, apply_entry.execution_handle)
        table.finish(probe_entry.execution_handle, "succeeded", "{}")
        # an identical Execute still deduplicates to the finished execution
        self.assertEqual(table.submit("ab" * 32, "11" * 32), (apply_entry, False))
        # ... unless it was REFUSED (it touched nothing): then it starts afresh
        refused_entry, _ = table.submit("ef" * 32, "44" * 32)
        table.finish(refused_entry.execution_handle, "refused", "{}")
        again, created = table.submit("ef" * 32, "44" * 32)
        self.assertTrue(created)
        self.assertNotEqual(again.execution_handle, refused_entry.execution_handle)



# ---------------------------------------------------------------------------
# T-2917:D28: every reconcile probe is a FRESH execution (never a stale dedup)
# ---------------------------------------------------------------------------


class _TableTransport:
    """The executor's real ``ExecutionTable`` behind a fake bus: Execute
    deduplicates exactly as the executor does, and a CREATED execution
    fetches its spec from the broker and observes the host NOW."""

    def __init__(self, source: BrokerExecutionSource, first, observe) -> None:
        counter = iter(range(1, 1000))
        self.table = ExecutionTable(handle_factory=lambda: format(next(counter), "064x"))
        self.source = source
        self.first = first
        self.observe = observe
        self.created = 0

    def name_owner(self, well_known_name: str) -> str:
        return ":1.42"

    def unix_user(self, unique_name: str) -> int:
        return RECOVERY_UID

    def call(self, unique_name, method, signature, args):
        from tools.host_control.executor.wire import ExecutionResult, encode_result

        if method == "Execute":
            handle, digest = args
            entry, created = self.table.submit(handle, digest)
            if created:
                self.created += 1
                _schema, kind, _digest, _spec = self.source.operation_spec(RECOVERY_UID, handle, digest)
                first = self.first
                observed = OperationEvidence(
                    first.operation_id, first.layer, first.kind, True, "live target observed",
                    {"observation": "file-target", "state": "absent", "at_desired": self.observe()},
                )
                result = ExecutionResult(ok=True, mutations=(), evidence=(observed,), detail=f"{kind} probe")
                self.table.finish(
                    entry.execution_handle,
                    "succeeded",
                    encode_result(operation_handle=handle, spec_digest=digest, result=result),
                )
            return (entry.execution_handle,)
        if method == "ExecutionGet":
            return self.table.get(args[0])
        raise AssertionError(method)


class StaleProbeDedupTests(_ReconcileCase):
    def test_d28_each_recorded_probe_moves_the_next_probe_to_a_new_digest(self) -> None:
        handle, _port = self.served_and_lost("d28-digest")
        record = self.fixture.journal.load_operation_unbound(handle)
        source = BrokerExecutionSource(journal=self.fixture.journal, registry=self.fixture.registry)
        first = source.reconcile_digest(record)
        self.assertEqual(source.reconcile_digest(record), first)  # deterministic: no clock, no randomness
        self.assertEqual(source.reconcile_spec_for(record)["probe_nonce"], 0)
        self.fixture.journal.append_event(
            current=record, event_kind="reconcile_probe", payload={"complete": True}, created_at=self.service.now_iso()
        )
        second = source.reconcile_digest(record)
        self.assertNotEqual(second, first)
        self.assertEqual(source.reconcile_spec_for(record)["probe_nonce"], 1)
        # restart-safe: a fresh source over the same journal derives the same nonce
        again = BrokerExecutionSource(journal=self.fixture.journal, registry=self.fixture.registry)
        self.assertEqual(again.reconcile_digest(record), second)
        # the mutating spec never carries a nonce (its digest is unchanged)
        self.assertNotIn("probe_nonce", source.spec_for(record))
        from tools.host_control import execution_source
        from tools.host_control.executor import engine

        self.assertEqual(engine.MAX_PROBE_NONCE, execution_source.MAX_PROBE_NONCE)

    def test_d28_second_probe_returns_fresh_observations_not_the_first_result(self) -> None:
        from tools.host_control.executor_client import DBusExecutorPort, ExecutorReconcileProbe

        handle, _port = self.served_and_lost("d28-fresh")
        record = self.fixture.journal.load_operation_unbound(handle)
        staged = self.fixture.journal.get_staged_receipt(handle)
        intents = self.fixture.journal.unresolved_intents(handle)
        source = BrokerExecutionSource(journal=self.fixture.journal, registry=self.fixture.registry)
        live = {"at_desired": "true"}
        transport = _TableTransport(source, self.first_mutating, lambda: live["at_desired"])
        port = DBusExecutorPort(registry=self.fixture.registry, execution_source=source, transport=transport)
        reconciler = BrokerLiveStateReconciler(
            journal=self.fixture.journal,
            probe=ExecutorReconcileProbe(port=port, sleeper=lambda seconds: None),
            now=lambda: self.service.now_iso(),
        )
        first = reconciler.reconcile(record, staged, intents)
        self.assertEqual((first.outcome, first.detail), ("irreconcilable", "lost-result-mutating"))
        # the first probe's execution is still RETAINED by the executor; the
        # live host has changed since
        live["at_desired"] = "false"
        second = reconciler.reconcile(record, staged, intents)
        self.assertEqual((second.outcome, second.detail), ("clean_prestate", "probe-clean"))
        self.assertEqual(transport.created, 2)  # a NEW execution, never the old one's result
        probes = self.events(handle, "reconcile_probe")
        self.assertEqual([item["report"][0]["evidence"]["at_desired"] for item in probes], ["true", "false"])

    def test_d28_executor_parse_requires_a_bounded_nonce_on_reconcile_only(self) -> None:
        from tools.host_control.contracts import canonical_json, sha256_json
        from tools.host_control.executor.engine import parse_spec
        from tools.host_control.executor.wire import InvalidSpec

        with tempfile.TemporaryDirectory() as temp:
            fixture = fx.build_executor_fixture(Path(temp) / "x")
            try:
                operations = (fx.op("dir", "mkdir", str(fixture.seat_root / "d"), {"mode": "0755"}),)

                def parse(payload):
                    digest = sha256_json(payload)
                    return parse_spec(
                        schema_version=fx.EXECUTION_SPEC_SCHEMA,
                        execution_kind=payload["execution_kind"],
                        returned_digest=digest,
                        spec_json=canonical_json(payload),
                        expected_digest=digest,
                        operation_handle=payload["operation_handle"],
                        identity=fixture.identity,
                        registry=fixture.registry,
                    )

                good = fx.spec_payload(fixture, operations, execution_kind="reconcile")
                self.assertEqual(parse(good).execution_kind, "reconcile")
                for nonce in (7, 2**31 - 1):
                    self.assertEqual(parse(dict(good, probe_nonce=nonce)).execution_kind, "reconcile")
                missing = dict(good)
                del missing["probe_nonce"]
                for label, payload in (
                    ("missing", missing),
                    ("bool", dict(good, probe_nonce=True)),
                    ("negative", dict(good, probe_nonce=-1)),
                    ("huge", dict(good, probe_nonce=2**31)),
                    ("string", dict(good, probe_nonce="1")),
                    ("on-apply", dict(fx.spec_payload(fixture, operations), probe_nonce=0)),
                ):
                    with self.subTest(label=label), self.assertRaises(InvalidSpec):
                        parse(payload)
            finally:
                fixture.close()


# ---------------------------------------------------------------------------
# T-2917:D29: a definitive Busy voids its Execute issuance (Denied never does)
# ---------------------------------------------------------------------------


class BusyVoidTests(_ReconcileCase):
    def receipts(self, handle: str) -> list[str]:
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT phase FROM operation_receipts WHERE operation_handle=?", (handle,)
                )
            ]

    @staticmethod
    def refusal(name: str):
        from tools.host_control.executor_client import ExecutorRefused

        return ExecutorRefused("refused", name=name)

    def scripted_port(self, script):
        """Execute #n follows ``script[n]``: ``busy``/``denied`` refuse
        WITHOUT any fetch (no execution created); ``ambiguous`` reaches the
        executor but its reply is lost before the execution fetches;
        ``serve`` fetches now; ``race`` lets the OUTSTANDING earlier
        execution fetch first, then this one's fetch is refused."""

        from tools.host_control.executor.wire import BUSY_ERROR, DENIED_ERROR

        source, test, refusal = BrokerExecutionSource(journal=self.fixture.journal, registry=self.fixture.registry), self, self.refusal

        class Port(FakePort):
            def execute(self, operation, staged_receipt, intents):
                step = script[len(self.executions)]
                super().execute(operation, staged_receipt, intents)
                digest = source.expected_digest(operation)
                if step == "busy":
                    raise refusal(BUSY_ERROR)
                if step == "denied":
                    raise refusal(DENIED_ERROR)
                if step == "ambiguous":
                    raise ExecutorAmbiguous("reply lost")
                if step == "serve":
                    source.operation_spec(RECOVERY_UID, operation.operation_handle, digest)
                elif step == "race":
                    source.operation_spec(RECOVERY_UID, operation.operation_handle, digest)
                    with test.assertRaises(InvalidTransition):
                        source.operation_spec(RECOVERY_UID, operation.operation_handle, digest)
                return format(len(self.executions), "064x")

        return Port(self.fixture.journal)

    def test_d29_a_busy_then_a_genuine_refusal_ends_failed_with_no_mutations(self) -> None:
        from tools.test_host_control_hardening import _refused

        handle = self.apply("d29-a")
        port = self.scripted_port(["busy", "serve"])
        port.collect_script = [_refused()]
        core = self.make_core(port)
        self.assertEqual(core.drive(handle), "dispatch_refused")
        self.assertEqual(self.fixture.journal.execute_issuance_count(handle), 0)
        (voided,) = self.events(handle, "execute_voided")
        self.assertEqual(voided["refusal"], "Busy")
        self.assertEqual(core.drive(handle), "failed")
        self.assertEqual(self.fixture.journal.execute_issuance_count(handle), 1)
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            failed = json.loads(
                connection.execute(
                    "SELECT receipt_json FROM operation_receipts WHERE operation_handle=? AND phase='failed'", (handle,)
                ).fetchone()[0]
            )
        self.assertEqual((failed["mutations"], failed["evidence"]), ([], []))
        self.assertEqual(self.probe.calls, [])
        self.assertEqual(self.state(handle), "failed")

    def test_d29_b_denied_then_a_refusal_still_routes_to_lost(self) -> None:
        from tools.test_host_control_hardening import _refused

        handle = self.apply("d29-b")
        port = self.scripted_port(["denied", "serve"])
        port.collect_script = [_refused()]
        core = self.make_core(port)
        self.assertEqual(core.drive(handle), "dispatch_refused")
        self.set_observation("true")
        self.assertEqual(core.drive(handle), "manual_recovery")
        self.assertEqual(self.events(handle, "execute_voided"), [])  # Denied proves nothing
        self.assertEqual(self.fixture.journal.execute_issuance_count(handle), 2)
        self.assertEqual(self.probe.calls, [handle])  # the reconciler decided, not a clean failure
        self.assertNotIn("failed", self.receipts(handle))

    def test_d29_c_busy_then_ambiguous_then_a_serve_race_still_routes_to_lost(self) -> None:
        from tools.test_host_control_hardening import _refused

        handle = self.apply("d29-c")
        port = self.scripted_port(["busy", "ambiguous", "race"])
        core = self.make_core(port)
        self.assertEqual(core.drive(handle), "dispatch_refused")
        self.assertEqual(core.drive(handle), "reconcile_pending")  # the definitive retry's reply is lost
        port.collect_script = [_refused()]
        self.set_observation("true")  # the ambiguous execution ran
        self.assertEqual(self.fresh_core(port, reconcile=True).drive(handle), "manual_recovery")
        self.assertEqual(len(self.events(handle, "execute_voided")), 1)
        self.assertEqual(self.fixture.journal.execute_issuance_count(handle), 2)
        self.assertNotIn("failed", self.receipts(handle))
        self.assertEqual(self.probe.calls, [handle])

    def fresh_core(self, port, *, reconcile: bool) -> DriverCore:
        service = OperationService(
            registry=self.fixture.registry,
            journal=self.fixture.journal,
            binder=self.binder,
            reconciler=self.reconciler,
            clock=self.fixture.clock,
            handle_factory=self._handles(),
        )
        return DriverCore(
            DriverContext(service, self.fixture.journal, port, lambda: None, reconcile=reconcile),
            sleeper=lambda seconds: None,
            collect_timeout=0.0,
        )

    def test_d29_journal_void_is_closed_bound_to_one_marker_and_never_after_acceptance(self) -> None:
        handle = self.apply("d29-journal")
        self.service.authorize(handle)
        record = self.service.dispatch(handle, FakePort(self.fixture.journal))  # accepted
        journal = self.fixture.journal
        with self.assertRaises(HostControlError):  # Denied is never a voiding refusal
            journal.void_execute_issuance(current=record, refusal="Denied", created_at=self.service.now_iso())
        with self.assertRaises(HostControlError):  # the latest issuance was ACCEPTED
            journal.void_execute_issuance(current=record, refusal="Busy", created_at=self.service.now_iso())
        self.assertEqual(journal.execute_issuance_count(handle), 1)
        # a Busy on a fresh Busy-able issuance voids it exactly once
        handle2 = self.apply("d29-journal-2")
        port = self.scripted_port(["busy"])
        self.assertEqual(self.make_core(port).drive(handle2), "dispatch_refused")
        record2 = journal.load_operation_unbound(handle2)
        with self.assertRaises(HostControlError):  # already voided
            journal.void_execute_issuance(current=record2, refusal="Busy", created_at=self.service.now_iso())
        self.assertEqual(len(self.events(handle2, "execute_voided")), 1)
        self.assertEqual(journal.execute_issuance_count(handle2), 0)

    def test_d29_the_void_is_broker_written_and_unreachable_from_bus_handlers(self) -> None:
        callers: list[tuple[str, str]] = []
        helper_callers: list[str] = []
        literal_modules: set[str] = set()
        for path in sorted(HOST_CONTROL.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "execute_voided" in text:
                literal_modules.add(str(path.relative_to(HOST_CONTROL)))
            tree = ast.parse(text)
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(function):
                    if (
                        isinstance(node, ast.Attribute)
                        and node.attr == "void_execute_issuance"
                        and function.name != "void_execute_issuance"
                    ):
                        callers.append((path.name, function.name))
                    if isinstance(node, ast.Attribute) and node.attr == "_execute_or_void":
                        helper_callers.append(function.name)
        self.assertEqual(callers, [("operations.py", "_execute_or_void")])
        self.assertEqual(
            sorted(set(helper_callers)), ["_reconcile", "dispatch", "resume_dispatch", "retry_identical_dispatch"]
        )
        # only the journal names the event; the executor package and every
        # D-Bus handler module never reach it
        self.assertEqual(literal_modules, {"journal.py"})
        for module in ("dbus_service.py", "health_dbus.py", "approval_source.py", "execution_source.py"):
            text = (HOST_CONTROL / module).read_text(encoding="utf-8")
            self.assertNotIn("void_execute_issuance", text)
            self.assertNotIn("_execute_or_void", text)
        for path in sorted((HOST_CONTROL / "executor").glob("*.py")):
            self.assertNotIn("void_execute_issuance", path.read_text(encoding="utf-8"))

if __name__ == "__main__":  # must stay LAST
    unittest.main()
