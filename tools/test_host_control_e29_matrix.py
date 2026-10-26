"""ORG-052 gate E29 as FIXTURE evidence (T-2917 5d.5; rulings T-2917:D1,
D24).

Tier A (``E29JournalCutTests``, CI-run; sqlite and fakes, no bus): for each
(apply / verify / rollback) x cut point, a CHILD PROCESS runs the real
broker domain over the parent's disposable journal and, at the cut, signals
a pipe and blocks inside a fake port, binder or journal step; the parent
SIGKILLs that child (its own child only), reopens the journal and runs
recovery through the real driver, service and reconciler, then asserts
``EXPECTED_E29[(kind, point)]`` and the cell's invariants. P9 ends the child
through the executor service's REAL hard-deadline watchdog exit.

Tier B (``E29BusFaultTests``, ``skipUnless`` dbus; CI skips it, T-4931): the
real broker and executor roles on a private bus with fault injection in the
TEST harness only (a held engine, process kills, a forced transport
disconnect); no crash hook is in production code. Each cell prints one
bounded JSON evidence line labelled ``fixture``.

E29 legs NOT proved here (T-2917:D24): MCP-adapter exit and adapter-side
provider retry are Unit 4 legs, cited from T-2915's evidence. E26's stuck
states are covered by P0-P5. Live proof remains T-2924's.
"""

from __future__ import annotations

import json
import os
import secrets
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tools.test_host_control_driver import SUBJECT_UID, RECOVERY_UID, FakePort, _ok
from tools.test_host_control_reconciler import ServingPort, _ReconcileCase
from tools._host_control_e29_child import FileBinder, _load
from tools.host_control.driver import DriverContext, DriverCore
from tools.host_control.executor.wire import EXIT_HARD_DEADLINE, ExecutionResult
from tools.host_control.executor_client import (
    CollectedResult,
    ExecutorAmbiguous,
    ExecutorUnknownExecution,
)
from tools.host_control.journal import BrokerJournal
from tools.host_control.operations import OperationService
from tools.host_control.reconciler import BrokerLiveStateReconciler
from tools.persona_installer import MutationRecord, OperationEvidence

REPO_ROOT = Path(__file__).resolve().parent.parent
CHILD_TIMEOUT = 120.0
KINDS = ("apply", "verify", "rollback")
POINTS = ("P0", "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9")

# (kind, point) -> the deterministic terminal outcome after recovery
EXPECTED_E29 = {
    **{(kind, point): "completed" for kind in KINDS for point in POINTS},
    # P5/P9 apply whose first mutating target is observed CHANGED: loud park
    ("apply", "P5-changed"): "manual_recovery",
}
NOT_APPLICABLE = {
    ("verify", "P1"): "a verify binds no approval (policy-authorized, ruling B2)",
}
for _cell in NOT_APPLICABLE:
    EXPECTED_E29.pop(_cell)


class _RecoveryPort(FakePort):
    """The parent's executor after the cut: an identical Execute of an
    execution the (killed) child's executor still holds deduplicates to the
    SAME reference, as the real executor table does."""

    def __init__(self, journal, held: dict[str, str]) -> None:
        super().__init__(journal)
        self.held = held

    def execute(self, operation, staged_receipt, intents) -> str:
        reference = super().execute(operation, staged_receipt, intents)
        return self.held.get(operation.operation_handle, reference)


class E29JournalCutTests(_ReconcileCase):
    def setUp(self) -> None:
        super().setUp()
        self.work = self.fixture.root / "e29"
        self.work.mkdir()

    # -- helpers ---------------------------------------------------------

    def receipts(self, handle: str) -> list[str]:
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT phase FROM operation_receipts WHERE operation_handle=? AND phase!='staged'", (handle,)
                )
            ]

    def state_on_disk(self, handle: str) -> str:
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            return connection.execute("SELECT state FROM operations WHERE operation_handle=?", (handle,)).fetchone()[0]

    def completed_apply_receipt(self, key: str) -> str:
        """A completed apply holding ONE restorable mutation (a rollback source)."""

        handle = self.apply(key)
        made = MutationRecord(
            self.first_mutating.operation_id, self.first_mutating.kind, self.first_mutating.target,
            self.first_mutating.owner, True, {"state": "absent"}, "directory",
        )
        port = ServingPort(self.fixture.journal, self.fixture.registry)
        port.collect_script = [_ok((made.operation_id,), (made,))]
        self.assertEqual(self.make_core(port).drive(handle), "completed")
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            return connection.execute(
                "SELECT receipt_handle FROM operation_receipts WHERE operation_handle=? AND phase='complete'", (handle,)
            ).fetchone()[0]

    def prepared(self, kind: str, key: str) -> str:
        if kind == "apply":
            return self.apply(key)
        if kind == "verify":
            return self.verify(key)
        record, _ = self.service.prepare_rollback(SUBJECT_UID, self.completed_apply_receipt(f"{key}-src"), key, "cd" * 32)
        return record.operation_handle

    def dispatched(self, kind: str, key: str) -> str:
        handle = self.prepared(kind, key)
        port = FakePort(self.fixture.journal)
        port.collect_script = [None]
        self.assertEqual(self.make_core(port).drive(handle), "collect_pending")
        self.assertEqual(self.state_on_disk(handle), "dispatched")
        return handle

    def run_child(self, kind: str, point: str, key: str, **extra: str) -> tuple[str, subprocess.Popen]:
        read, write = os.pipe()
        config = {
            "fd": write,
            "kind": kind,
            "point": point,
            "root": str(self.fixture.root),
            "now": self.fixture.clock().isoformat(),
            "subject_uid": SUBJECT_UID,
            "seat_uid": RECOVERY_UID,
            "plan_handle": self.plan.plan_handle,
            "plan_digest": self.plan.plan_digest,
            "key": key,
            "binder_state": str(self.work / "bindings.json"),
            "executor_state": str(self.work / "executor.json"),
            "handle_out": str(self.work / f"{key}.handle.json"),
            **extra,
        }
        config_path = self.work / f"{key}.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        child = subprocess.Popen(
            [sys.executable, "-m", "tools._host_control_e29_child", str(config_path)],
            cwd=REPO_ROOT,
            pass_fds=(write,),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        os.close(write)
        try:
            if point == "P9":
                try:
                    code = child.wait(timeout=CHILD_TIMEOUT)
                except subprocess.TimeoutExpired:
                    child.kill()  # never leave this test's own child behind
                    child.wait(timeout=30)
                    self.fail(f"{kind}/P9: the watchdog never exited the child")
                self.assertEqual(code, EXIT_HARD_DEADLINE, child.stderr.read().decode(errors="replace")[-800:])
            else:
                ready, _, _ = select.select([read], [], [], CHILD_TIMEOUT)
                data = os.read(read, 16) if ready else b""
                if data != b"CUT\n":
                    child.kill()
                    child.wait(timeout=30)
                    self.fail(f"{kind}/{point}: the child never reached its cut: {child.stderr.read().decode(errors='replace')[-800:]}")
                child.kill()  # SIGKILL of this test's own child at the cut
                self.assertEqual(child.wait(timeout=30), -9)
        finally:
            os.close(read)
            child.stderr.close()
        handle = extra.get("handle") or _load(config["handle_out"])["handle"]
        return handle, child

    def recover(self, handle: str, port: FakePort, binder=None) -> str:
        """A broker RESTART: a fresh journal handle, service, reconciler and
        driver over the same on-disk journal."""

        journal = BrokerJournal.open(self.fixture.root / "broker.sqlite3")
        self.addCleanup(journal.close)
        port.journal = journal
        service = OperationService(
            registry=self.fixture.registry,
            journal=journal,
            binder=binder or FileBinder(self.work / "bindings.json"),
            reconciler=BrokerLiveStateReconciler(journal=journal, probe=self.probe, now=lambda: self.service.now_iso()),
            clock=self.fixture.clock,
            handle_factory=lambda: secrets.token_hex(32),
        )
        core = DriverCore(
            DriverContext(service, journal, port, lambda: None, reconcile=True),
            sleeper=lambda seconds: None,
            # positive: a read-only verify re-dispatch needs time left (the
            # scripted collects answer at once; the sleeper is a no-op)
            collect_timeout=30.0,
        )
        return core.drive(handle)

    # -- the matrix --------------------------------------------------------

    def cell(self, kind: str, point: str) -> None:
        key = f"e29-{kind}-{point}"
        held = {}
        binder = FileBinder(self.work / "bindings.json")
        if point in ("P5", "P9", "P5-changed"):
            handle = self.dispatched(kind, key)
            self.run_child(kind, "P9" if point == "P9" else "P5", key, handle=handle)
            if kind != "verify":  # a mutating spec was served to the dead executor
                self.assertEqual(len(self.events(handle, "spec_served")), 1)
            self.set_observation("true" if point == "P5-changed" else "false")
            port = _RecoveryPort(self.fixture.journal, held)
            port.collect_script = [ExecutorUnknownExecution("the executor died mid-run"), _ok()]
        else:
            extra = {}
            if kind == "rollback":
                extra["receipt_handle"] = self.completed_apply_receipt(f"{key}-src")
            handle, _child = self.run_child(kind, point, key, **extra)
            held = _load(self.work / "executor.json")
            port = _RecoveryPort(self.fixture.journal, held)
            port.collect_script = [_ok()]
        before = self.state_on_disk(handle)
        receipts_before = self.receipts(handle)
        outcome = self.recover(handle, port, binder)
        expected = EXPECTED_E29[(kind, point)]
        self.assertEqual(outcome, expected, (kind, point, before, self.events(handle, "result")))
        # invariants: the cut never left a terminal receipt behind, and
        # recovery appended at most ONE
        self.assertEqual(receipts_before, [])
        self.assertEqual(len(self.receipts(handle)), 1 if expected in ("completed", "failed") else 0)
        record = self.fixture.journal.load_operation_unbound(handle)
        if point == "P0":
            self.assertEqual(before, "prepared")
            self.assertEqual(len(binder.calls), 0 if kind == "verify" else 1)
        elif point == "P1":
            self.assertEqual(before, "prepared")
            bound = _load(self.work / "bindings.json")[handle]["tuple"]
            self.assertEqual(list(binder.calls[0]), bound)  # the identical tuple
            # the ORIGINAL binding the killed process made is the one used
            self.assertEqual(record.authority_record_id, _load(self.work / "bindings.json")[handle]["authority"])
        elif point == "P2":
            self.assertEqual(before, "authorized")
            self.assertEqual(len(port.executions), 1)
        elif point == "P3":
            self.assertEqual(before, "dispatch_intent")
            self.assertEqual(self.events(handle, "spec_served"), [])
            self.assertEqual(len(port.executions), 1)
        elif point == "P4":
            self.assertEqual(before, "dispatch_intent")
            # the identical Execute was answered with the SAME execution
            self.assertEqual(self.fixture.journal.dispatch_execution_ref(handle), held[handle])
        elif point in ("P5", "P9"):
            self.assertEqual(before, "dispatched")
            if kind == "apply":
                self.assertEqual([item["reason"] for item in self.events(handle, "spec_rearmed")], ["reconcile-probe-clean"])
                self.assertEqual(len(self.events(handle, "reconcile_probe")), 1)
            elif kind == "rollback":
                self.assertEqual([item["reason"] for item in self.events(handle, "spec_rearmed")], ["rollback-redispatch"])
            else:
                self.assertEqual(self.events(handle, "spec_rearmed"), [])
                self.assertTrue(self.events(handle, "redispatched"))
        elif point == "P5-changed":
            (parked,) = self.events(handle, "irreconcilable")
            self.assertEqual(parked["detail"], "lost-result-mutating")
            self.assertTrue(parked["report"])
        elif point in ("P6", "P7", "P8"):
            self.assertEqual(before, "dispatched")  # P8: the terminal transaction rolled back atomically
            self.assertEqual(self.fixture.journal.dispatch_execution_ref(handle), held[handle])
            self.assertEqual(len(port.executions), 0)  # collected via the recorded ref, never re-dispatched

    def test_e29_matrix_apply(self) -> None:
        for point in (*POINTS, "P5-changed"):
            with self.subTest(point=point):
                self.cell("apply", point)

    def test_e29_matrix_verify(self) -> None:
        for point in POINTS:
            if ("verify", point) in NOT_APPLICABLE:
                continue
            with self.subTest(point=point):
                self.cell("verify", point)

    def test_e29_matrix_rollback(self) -> None:
        for point in POINTS:
            with self.subTest(point=point):
                self.cell("rollback", point)

    def test_e29_expected_table_is_total(self) -> None:
        cells = {(kind, point) for kind in KINDS for point in POINTS}
        self.assertEqual(cells, (set(EXPECTED_E29) - {("apply", "P5-changed")}) | set(NOT_APPLICABLE))
        self.assertFalse(set(EXPECTED_E29) & set(NOT_APPLICABLE))

    # -- extra tier-A cells --------------------------------------------------

    def test_e29_identical_retry_after_restart_returns_the_same_operation(self) -> None:
        handle, _child = self.run_child("apply", "P0", "e29-retry")
        journal = BrokerJournal.open(self.fixture.root / "broker.sqlite3")
        self.addCleanup(journal.close)
        service = OperationService(
            registry=self.fixture.registry, journal=journal, binder=FileBinder(self.work / "bindings.json"),
            reconciler=self.reconciler, clock=self.fixture.clock, handle_factory=lambda: secrets.token_hex(32),
        )
        record, replayed = service.prepare_apply(SUBJECT_UID, self.plan.plan_handle, self.plan.plan_digest, "e29-retry", "ab" * 32)
        self.assertEqual((record.operation_handle, replayed), (handle, True))  # the SAME operation, replayed

    def test_e29_verification_failure_is_a_failed_receipt(self) -> None:
        handle = self.verify("e29-verify-fails")
        port = FakePort(self.fixture.journal)
        drift = OperationEvidence(self.first_mutating.operation_id, 1, self.first_mutating.kind, False, "drifted")
        port.collect_script = [
            CollectedResult(ok=False, result=ExecutionResult(ok=False, mutations=(), evidence=(drift,), detail="verify failed"))
        ]
        self.assertEqual(self.make_core(port).drive(handle), "failed")
        with sqlite3.connect(self.fixture.root / "broker.sqlite3") as connection:
            receipt = json.loads(
                connection.execute(
                    "SELECT receipt_json FROM operation_receipts WHERE operation_handle=? AND phase='failed'", (handle,)
                ).fetchone()[0]
            )
        self.assertEqual(receipt["mutations"], [])
        self.assertEqual([item["detail"] for item in receipt["evidence"]], ["drifted"])

    def test_e29_executor_timeout_is_ambiguous_then_reconciled(self) -> None:
        handle = self.apply("e29-timeout")
        port = FakePort(self.fixture.journal)
        port.execute_errors = [ExecutorAmbiguous("timeout"), ExecutorAmbiguous("timeout again")]
        self.assertEqual(self.make_core(port).drive(handle), "reconcile_pending")
        self.assertEqual(self.state_on_disk(handle), "dispatch_intent")
        self.assertEqual(self.receipts(handle), [])
        port.collect_script = [_ok()]
        self.assertEqual(self.recover(handle, port), "completed")  # a restart: the identical retry
        self.assertEqual(len(self.events(handle, "execute_retry")), 2)


# ---------------------------------------------------------------------------
# tier B: the private-bus fault matrix
# ---------------------------------------------------------------------------

try:  # pragma: no cover - environment probe
    import dbus
    import dbus.bus
    import dbus.lowlevel
    import dbus.mainloop

    from tools import _host_control_testbus as testbus

    DBUS = testbus.DBUS_DAEMON is not None
except ImportError:  # pragma: no cover
    DBUS = False

_BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
 "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <type>session</type>
  <listen>unix:path={socket}</listen>
  <auth>EXTERNAL</auth>
  <policy context="default">
    <allow send_destination="*" eavesdrop="true"/>
    <allow eavesdrop="true"/>
    <allow own="*"/>
  </policy>
</busconfig>
"""


def _evidence(cell: str, fault: str, outcome: str) -> None:
    print(
        json.dumps(
            {
                "gate": "E29",
                "cell": cell,
                "fault": fault,
                "outcome": outcome,
                "evidence_label": "fixture",
                "transport": "private-bus",
            },
            sort_keys=True,
        )
    )


@unittest.skipUnless(DBUS, "dbus-python and dbus-daemon are required")
class E29BusFaultTests(unittest.TestCase):
    """Each cell gets its own private bus, tree and role processes (the
    ``test_host_control_executor_dbus_driver`` hosts with the E29 knobs:
    the broker with the live-state reconciler, an executor engine that can
    be HELD at run entry, a short watchdog). The permissive bus config is
    deliberate: every refusal is the services' own check."""

    def setUp(self) -> None:
        from tools import test_host_control_executor_dbus_driver as host

        self.bus = testbus.spawn_daemon("t2917e29-", _BUS_CONFIG)
        self.addCleanup(testbus.stop_daemon, self.bus)
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.tree = host.prepare_tree(Path(work.name) / "e29")
        self.config = dict(
            self.tree,
            bus_address=self.bus.bus_address,
            reconcile=True,
            max_attempts=40,
            retry_backoff=0.25,
            hold_path=str(Path(self.tree["root"]) / "hold"),
            held_marker=str(Path(self.tree["root"]) / "held"),
            release_path=str(Path(self.tree["root"]) / "release"),
        )
        self.config_path = Path(self.bus.bus_dir) / "e29.json"
        self.processes: dict[str, subprocess.Popen] = {}
        self.logs: list = []
        self.addCleanup(self._stop_all)
        self.connection = dbus.bus.BusConnection(self.bus.bus_address)
        from tools.host_control.executor import wire as executor_wire

        self.executor_name = executor_wire.bus_name_for_uid(os.getuid())

    # -- process management (this test's own children only) -------------

    def write_config(self, **overrides) -> None:
        self.config.update(overrides)
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def spawn(self, role: str) -> subprocess.Popen:
        from tools.host_control import wire

        self.write_config()
        log = open(Path(self.bus.bus_dir) / f"{role}-{len(self.logs)}.log", "w", encoding="utf-8")  # noqa: SIM115
        self.logs.append(log)
        process = subprocess.Popen(
            [sys.executable, "-m", "tools.test_host_control_executor_dbus_driver", role, str(self.config_path)],
            cwd=REPO_ROOT,
            stdout=log,
            stderr=log,
            preexec_fn=testbus.pdeathsig_preexec_fn(),
        )
        self.bus.register_driver(process.pid)
        self.processes[role] = process
        name = self.executor_name if role == "executor" else wire.BUS_NAME
        self.wait_for(lambda: self.connection.name_has_owner(name), f"{role} owns {name}", process)
        return process

    def kill(self, role: str) -> int:
        process = self.processes.pop(role)
        process.kill()
        return process.wait(timeout=30)

    def _stop_all(self) -> None:
        for process in self.processes.values():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        for log in self.logs:
            log.close()

    def logs_tail(self) -> str:
        return " | ".join(Path(log.name).read_text(encoding="utf-8")[-1200:] for log in self.logs)

    def wait_for(self, predicate, what: str, process: subprocess.Popen | None = None, timeout: float = 90.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if process is not None and process.poll() is not None:
                self.fail(f"process exited before: {what}; logs: {self.logs_tail()}")
            if time.monotonic() > deadline:
                self.fail(f"timed out waiting for {what}; logs: {self.logs_tail()}")
            time.sleep(0.05)

    def manager(self, method: str, signature: str, args: tuple):
        from tools.host_control import wire

        return self.connection.call_blocking(wire.BUS_NAME, wire.OBJECT_PATH, wire.MANAGER_INTERFACE, method, signature, args)

    def operation_state(self, handle: str) -> str:
        state, _payload = self.manager("OperationGet", "s", (handle,))
        return str(state)

    def wait_terminal(self, handle: str, timeout: float = 120.0) -> str:
        terminal = ("completed", "failed", "refused", "manual_recovery")
        self.wait_for(lambda: self.operation_state(handle) in terminal, f"{handle[:8]} terminal", timeout=timeout)
        return self.operation_state(handle)

    def events(self, handle: str, kind: str) -> list:
        with sqlite3.connect(f"file:{self.tree['journal']}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT payload_json FROM operation_events WHERE operation_handle=? AND event_kind=? ORDER BY seq",
                (handle, kind),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def apply_held(self, key: str) -> str:
        Path(self.config["hold_path"]).write_text("hold\n", encoding="utf-8")
        plan_handle, plan_digest, _summary = self.manager("Plan", "sss", ("recovery", "manifest-r1", f"{key}-plan"))
        handle = str(self.manager("Apply", "ssss", (plan_handle, plan_digest, key, "ab" * 32)))
        self.wait_for(lambda: Path(self.config["held_marker"]).exists(), "the executor held mid-run")
        Path(self.config["hold_path"]).unlink()
        return handle

    # -- cells -------------------------------------------------------------

    def test_e29_kill_executor_held_in_start_unit_then_reconcile(self) -> None:
        # held at engine run entry (the spec served, nothing applied): this
        # fixture's plan carries no activation kinds, so the hold point is
        # the run entry rather than StartUnit (see the module docstring)
        self.spawn("executor")
        self.spawn("broker")
        handle = self.apply_held("e29-kill-executor")
        self.assertEqual(self.kill("executor"), -9)
        self.spawn("executor")  # the seat's executor comes back (activation stand-in)
        self.assertEqual(self.wait_terminal(handle), "completed", self.logs_tail())
        self.assertEqual([item["reason"] for item in self.events(handle, "spec_rearmed")], ["reconcile-probe-clean"])
        self.assertEqual(len(self.events(handle, "reconcile_probe")), 1)
        _evidence("kill-executor-held-then-reconcile", "SIGKILL executor mid-run", "completed via probe-clean re-arm")

    def test_e29_kill_broker_while_executor_held_then_collect_after_restart(self) -> None:
        self.spawn("executor")
        self.spawn("broker")
        handle = self.apply_held("e29-kill-broker")
        self.assertEqual(self.kill("broker"), -9)
        Path(self.config["release_path"]).write_text("go\n", encoding="utf-8")  # the executor finishes alone
        self.spawn("broker")  # restart: the driver recovers pending operations
        self.assertEqual(self.wait_terminal(handle), "completed", self.logs_tail())
        self.assertEqual(self.events(handle, "spec_rearmed"), [])  # collected, never re-dispatched
        self.assertEqual(len(self.events(handle, "spec_served")), 1)
        self.assertEqual(len(self.events(handle, "result")), 1)
        _evidence("kill-broker-while-executor-held", "SIGKILL broker mid-execution", "completed via recorded ref")

    def test_e29_executor_unique_name_change_is_re_resolved_and_identity_checked(self) -> None:
        first = self.spawn("executor")
        self.spawn("broker")
        old_owner = str(self.connection.get_name_owner(self.executor_name))
        self.assertEqual(self.kill("executor"), -9)
        self.assertNotEqual(first.poll(), None)
        self.spawn("executor")
        new_owner = str(self.connection.get_name_owner(self.executor_name))
        self.assertNotEqual(new_owner, old_owner)
        self.assertEqual(int(self.connection.get_unix_user(new_owner)), os.getuid())
        plan_handle, _digest, _summary = self.manager("Plan", "sss", ("recovery", "manifest-r1", "e29-rename-plan"))
        handle = str(self.manager("Verify", "sss", ("recovery", plan_handle, "e29-rename-verify")))
        self.assertIn(self.wait_terminal(handle), ("completed", "failed"))  # served by the NEW owner
        self.assertTrue(self.events(handle, "dispatched"))
        _evidence("executor-unique-name-change", "executor restarted under a new unique name", "re-resolved and served")

    def test_e29_disconnect_after_submit_keeps_the_operation_running(self) -> None:
        from tools.host_control import wire

        self.spawn("executor")
        self.spawn("broker")
        plan_handle, plan_digest, _summary = self.manager("Plan", "sss", ("recovery", "manifest-r1", "e29-drop-plan"))
        client = dbus.bus.BusConnection(self.bus.bus_address)
        message = dbus.lowlevel.MethodCallMessage(wire.BUS_NAME, wire.OBJECT_PATH, wire.MANAGER_INTERFACE, "Apply")
        message.append(plan_handle, plan_digest, "e29-drop", "ab" * 32, signature="ssss")
        client.send_message(message)
        client.flush()
        # the broker has taken the call (the operation is durable) ...
        self.wait_for(lambda: self._handle_for_key("e29-drop") is not None, "the submitted Apply to be durable")
        client.close()  # ... and the client drops without ever reading the reply
        handle = self._handle_for_key("e29-drop")
        # it keeps running; an identical retry from a new connection names
        # the SAME operation, and OperationGet answers later
        self.assertEqual(str(self.manager("Apply", "ssss", (plan_handle, plan_digest, "e29-drop", "ab" * 32))), handle)
        self.assertEqual(self.wait_terminal(handle), "completed", self.logs_tail())
        _evidence("disconnect-after-submit", "client disconnect before the Apply reply", "completed; retry names the same operation")

    def _handle_for_key(self, key: str) -> str | None:
        with sqlite3.connect(f"file:{self.tree['journal']}?mode=ro", uri=True) as connection:
            row = connection.execute("SELECT operation_handle FROM operations WHERE idempotency_key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def test_e29_bus_disconnect_reconnect_of_the_driver_transport(self) -> None:
        from tools.host_control.executor_client import DBusExecutorTransport, ExecutorCallError

        self.spawn("executor")
        made: list = []

        def factory():
            made.append(dbus.bus.BusConnection(self.bus.bus_address, mainloop=dbus.mainloop.NULL_MAIN_LOOP))
            return made[-1]

        transport = DBusExecutorTransport(factory, timeout=10.0)
        owner = transport.name_owner(self.executor_name)
        made[0].close()  # the driver's connection drops
        with self.assertRaises(ExecutorCallError) as caught:
            transport.name_owner(self.executor_name)
        self.assertEqual(caught.exception.name, "org.freedesktop.DBus.Error.Disconnected")
        self.assertEqual(transport.name_owner(self.executor_name), owner)  # reconnected
        self.assertEqual(len(made), 2)
        _evidence("driver-transport-disconnect", "driver bus connection closed", "reconnected on the next call")

    def test_e29_hard_deadline_breach_exits_and_is_reconciled(self) -> None:
        self.write_config(watchdog_seconds=1.0)
        executor = self.spawn("executor")
        self.spawn("broker")
        handle = self.apply_held("e29-deadline")
        self.assertEqual(executor.wait(timeout=60), EXIT_HARD_DEADLINE)  # the REAL watchdog exit
        self.processes.pop("executor")
        self.write_config(watchdog_seconds=None)
        self.spawn("executor")
        self.assertEqual(self.wait_terminal(handle), "completed", self.logs_tail())
        self.assertEqual([item["reason"] for item in self.events(handle, "spec_rearmed")], ["reconcile-probe-clean"])
        _evidence("hard-deadline-breach", "run outlived budget + grace", "executor exited 70; reconciled")


if __name__ == "__main__":  # must stay LAST
    unittest.main()
