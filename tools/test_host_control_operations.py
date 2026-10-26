"""T-2909 tests: broker operation lifecycle, journal v2, v1->v2 migration.

Process-boundary labeling (per dispatch brief): tests that prove DURABILITY
use a real second process (subprocess commits or reads across an exit);
same-process tests prove logic correctness or commit-visibility ordering
only and say so in their comments.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import itertools
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import yaml

from tools.host_control.contracts import (
    OPERATION_TRANSITIONS,
    OPERATION_STATES,
    ApprovalUnavailable,
    BindOutcome,
    HostControlError,
    IdempotencyConflict,
    InvalidTransition,
    JournalBusy,
    MigrationRequired,
    OperationRecord,
    ReconcileOutcome,
    RecoveryRefused,
    SubjectMismatch,
    UnknownHandle,
    Unsupported,
    canonical_json,
)
from tools.host_control import journal as journal_module
from tools.host_control.journal import (
    APPLICATION_ID,
    DDL,
    DDL_V1,
    SCHEMA_DIGEST,
    SCHEMA_DIGEST_V1,
    USER_VERSION,
    BrokerJournal,
)
from tools.host_control.operations import OperationService, transition
from tools.host_control.planner import HostPlanService
from tools.host_control.policy import HostPolicy
from tools.host_control.registry import SeatRegistry
from tools.host_control.trusted_files import RootOwnedFileLoader
from tools.persona_installer import (
    Manifest,
    MutationRecord,
    OperationEvidence,
    PersonaInstaller,
)
from tools.test_host_control_core import _manifest_dict


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXED_NOW = datetime(2026, 8, 12, 1, 0, tzinfo=timezone.utc)
V2_TABLES = [
    ("journal_meta", 1),
    ("operation_events", 1),
    ("operation_receipts", 1),
    ("operations", 1),
    ("plans", 1),
    ("rollback_intents", 1),
]


class FakeBinder:
    """Programmable ValidateAndBind seam; records the exact call tuples."""

    def __init__(self, outcomes: list | None = None) -> None:
        self.calls: list[tuple[str, str, str, str]] = []
        self.outcomes = list(outcomes or [])
        self.default = BindOutcome("bound", "authority-1", "bound")

    def validate_and_bind(
        self,
        approval_handle: str,
        plan_handle: str,
        expected_digest: str,
        operation_handle: str,
    ) -> BindOutcome:
        self.calls.append(
            (approval_handle, plan_handle, expected_digest, operation_handle)
        )
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return self.default


class FakeExecutor:
    def __init__(self, ref: str = "exec-ref-1", error: Exception | None = None):
        self.calls: list[tuple] = []
        self.ref = ref
        self.error = error

    def execute(self, operation, staged_receipt, intents) -> str:
        self.calls.append((operation, staged_receipt, intents))
        if self.error is not None:
            raise self.error
        return self.ref


class JournalPeekExecutor:
    """Executor that inspects the journal through its OWN connection.

    Same-process but a genuinely separate SQLite connection: it proves
    commit-VISIBILITY ordering (rows committed before the executor runs),
    not the durable crash boundary — that claim is carried by the
    cross-process test.
    """

    def __init__(self, journal_path: Path) -> None:
        self.journal_path = journal_path
        self.observed: tuple | None = None

    def execute(self, operation, staged_receipt, intents) -> str:
        connection = sqlite3.connect(self.journal_path)
        try:
            state = connection.execute(
                "SELECT state FROM operations WHERE operation_handle=?",
                (operation.operation_handle,),
            ).fetchone()[0]
            phases = connection.execute(
                "SELECT phase FROM operation_receipts "
                "WHERE operation_handle=? ORDER BY phase",
                (operation.operation_handle,),
            ).fetchall()
            intent_rows = connection.execute(
                "SELECT seq, resolved FROM rollback_intents "
                "WHERE operation_handle=? ORDER BY seq",
                (operation.operation_handle,),
            ).fetchall()
        finally:
            connection.close()
        self.observed = (state, phases, intent_rows)
        return "exec-ref-peek"


class FakeReconciler:
    def __init__(self, outcome: ReconcileOutcome | None = None) -> None:
        self.calls: list[tuple] = []
        self.outcome = outcome or ReconcileOutcome(
            "clean_prestate", (), (), ""
        )

    def reconcile(self, operation, staged_receipt, unresolved_intents):
        self.calls.append((operation, staged_receipt, unresolved_intents))
        return self.outcome


class OperationCaseBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.manifest_path = self.root / "instance.yaml"
        self.manifest_path.write_text(
            yaml.safe_dump(_manifest_dict(), sort_keys=False),
            encoding="utf-8",
        )
        self.manifest_path.chmod(0o600)
        self.installer = PersonaInstaller(REPO_ROOT)
        manifest = Manifest.load(self.manifest_path)
        prototype = self.installer.plan(
            manifest,
            "recovery",
            harness_override="codex",
            root=Path("/projection"),
        )
        self.framework_sha256 = prototype.provenance["source_artifact_sha256"]
        self.template_ids = (
            "tools/systemd/pantheon-persona-consume@.service",
            "tools/systemd/pantheon-persona-consume@.timer",
            "tools/systemd/pantheon-persona-quota@.service",
            "tools/systemd/pantheon-persona-quota@.timer",
        )
        self.registry_path = self.root / "registry.json"
        self.policy_path = self.root / "policy.json"
        self.journal_path = self.root / "broker.sqlite3"
        self.write_json(self.registry_path, self.registry_dict())
        self.write_json(self.policy_path, self.policy_dict())
        self.files = RootOwnedFileLoader._open_for_test(
            (self.root,),
            expected_owner_uid=os.getuid(),
        )
        self.registry = SeatRegistry.load(self.registry_path, files=self.files)
        self.policy = HostPolicy.load(self.policy_path, files=self.files)
        self.journal = BrokerJournal.open(self.journal_path)
        self.handle_counter = itertools.count(1)
        self.planner = HostPlanService(
            registry=self.registry,
            policy=self.policy,
            journal=self.journal,
            installer=self.installer,
            clock=lambda: FIXED_NOW,
            handle_factory=self.next_handle,
            plan_ttl=timedelta(hours=1),
            contract_version="org.pantheon.HostBroker1/v1",
            broker_revision="unit-test",
            host_operation_families=frozenset(
                ("persona.install", "persona.verify")
            ),
            health_check=lambda: "ready",
        )
        self.plan_result = self.planner.plan(
            1003,
            "recovery",
            "manifest-r1",
            "operations-fixture-plan",
        )
        self.subject = self.registry.subject_for_uid(1003)
        self.binder = FakeBinder()
        self.reconciler = FakeReconciler()
        self.service = self.build_service()

    def tearDown(self) -> None:
        self.journal.close()
        self.files.close()
        self.temp.cleanup()

    # ------------------------------------------------------------------
    # fixture helpers
    # ------------------------------------------------------------------

    def next_handle(self) -> str:
        return f"{next(self.handle_counter):064x}"

    def build_service(
        self,
        *,
        binder: FakeBinder | None = None,
        reconciler: FakeReconciler | None = None,
        clock=None,
        handle_factory=None,
    ) -> OperationService:
        return OperationService(
            registry=self.registry,
            journal=self.journal,
            binder=binder or self.binder,
            reconciler=reconciler or self.reconciler,
            clock=clock or (lambda: FIXED_NOW),
            handle_factory=handle_factory or self.next_handle,
        )

    def write_json(self, path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def registry_dict(self) -> dict:
        return {
            "schema": "pantheon-host-seat-registry/v1",
            "seats": [
                {
                    "seat_id": "consultant",
                    "uid": 1003,
                    "persona": "consultant",
                    "role": "worker",
                    "instance": "test-instance",
                    "account": "codex",
                    "harness": "codex",
                    "install_root": "/home/codex",
                },
                {
                    "seat_id": "peer",
                    "uid": 1004,
                    "persona": "peer",
                    "role": "worker",
                    "instance": "test-instance",
                    "account": "peer",
                    "harness": "codex",
                    "install_root": "/home/peer",
                },
                {
                    "seat_id": "recovery",
                    "uid": 1002,
                    "persona": "recovery",
                    "role": "recovery",
                    "instance": "test-instance",
                    "account": "claude-fallback",
                    "harness": "codex",
                    "install_root": "/projection",
                },
            ],
            "manifests": [
                {
                    "revision": "manifest-r1",
                    "path": str(self.manifest_path),
                    "sha256": hashlib.sha256(
                        self.manifest_path.read_bytes()
                    ).hexdigest(),
                    "framework_revision": "test-rev",
                    "framework_sha256": self.framework_sha256,
                    "template_ids": list(self.template_ids),
                }
            ],
        }

    def policy_dict(self) -> dict:
        grants = []
        for subject in ("consultant", "peer"):
            grants.append(
                {
                    "subject_seat_id": subject,
                    "action": "plan",
                    "target_seat_id": "recovery",
                    "manifest_revisions": ["manifest-r1"],
                    "operation_families": ["persona.install"],
                    "approval_class": "human",
                }
            )
        return {
            "schema": "pantheon-host-policy/v1",
            "revision": "policy-r1",
            "grants": grants,
        }

    def prepare(self, key: str = "op-1", approval: str = "a1" * 32):
        record, _ = self.service.prepare_apply(
            1003,
            self.plan_result.plan_handle,
            self.plan_result.plan_digest,
            key,
            approval,
        )
        return record

    def to_authorized(self, key: str = "op-1") -> OperationRecord:
        record = self.prepare(key)
        return self.service.authorize(record.operation_handle)

    def to_dispatch_intent(self, key: str = "op-1") -> OperationRecord:
        # Natural crash cut: the executor dies AFTER the durable
        # dispatch-intent transaction, leaving state == dispatch_intent.
        record = self.to_authorized(key)
        boom = FakeExecutor(error=RuntimeError("executor died post-intent"))
        with self.assertRaises(RuntimeError):
            self.service.dispatch(record.operation_handle, boom)
        return self.journal.load_operation_unbound(record.operation_handle)

    def to_dispatched(self, key: str = "op-1") -> OperationRecord:
        record = self.to_authorized(key)
        return self.service.dispatch(record.operation_handle, FakeExecutor())

    def to_completed(self, key: str = "op-1") -> OperationRecord:
        record = self.to_dispatched(key)
        return self.service.append_result(
            record.operation_handle,
            ok=True,
            mutations=(self.sample_mutation(),),
            evidence=(self.sample_evidence(),),
            detail="done",
        )

    @staticmethod
    def sample_mutation() -> MutationRecord:
        return MutationRecord(
            operation_id="connective.unit-dir",
            kind="dir-ensure",
            target="/projection/.config/systemd/user",
            owner="recovery",
            changed=True,
            before={"state": "absent"},
            after_fingerprint="d" * 64,
        )

    @staticmethod
    def sample_evidence() -> OperationEvidence:
        return OperationEvidence(
            operation_id="connective.unit-dir",
            layer=2,
            kind="dir-ensure",
            ok=True,
            detail="created",
            evidence={"mode": "0700"},
        )

    def op_count(self) -> int:
        with sqlite3.connect(self.journal_path) as connection:
            return connection.execute(
                "SELECT count(*) FROM operations"
            ).fetchone()[0]

    def db_state(self, handle: str) -> str:
        with sqlite3.connect(self.journal_path) as connection:
            return connection.execute(
                "SELECT state FROM operations WHERE operation_handle=?",
                (handle,),
            ).fetchone()[0]

    def event_rows(self, handle: str) -> list[tuple]:
        with sqlite3.connect(self.journal_path) as connection:
            return connection.execute(
                "SELECT seq, from_state, to_state, event_kind "
                "FROM operation_events WHERE operation_handle=? ORDER BY seq",
                (handle,),
            ).fetchall()

    def receipt_handle_for(self, handle: str, phase: str) -> str:
        with sqlite3.connect(self.journal_path) as connection:
            row = connection.execute(
                "SELECT receipt_handle FROM operation_receipts "
                "WHERE operation_handle=? AND phase=?",
                (handle, phase),
            ).fetchone()
        self.assertIsNotNone(row)
        return row[0]

    def record_in_state(self, state: str, key: str) -> OperationRecord:
        base = self.prepare(key)
        values = {
            field.name: getattr(base, field.name)
            for field in dataclasses.fields(base)
            if field.name != "digest"
        }
        values["state"] = state
        values["authority_record_id"] = (
            None if state in ("prepared", "refused") else "authority-1"
        )
        return OperationRecord.create(**values)

    def build_v1_journal(
        self,
        path: Path,
        rows: tuple[tuple, ...],
    ) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.executescript(DDL_V1)
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute("PRAGMA user_version=1")
            connection.execute(
                "INSERT INTO journal_meta VALUES (1, 1, ?, ?)",
                (SCHEMA_DIGEST_V1, "2026-07-31T00:00:00+00:00"),
            )
            connection.executemany(
                "INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            connection.commit()
        finally:
            connection.close()

    def plan_rows(self) -> tuple[tuple, ...]:
        with sqlite3.connect(self.journal_path) as connection:
            return tuple(
                tuple(row)
                for row in connection.execute(
                    journal_module._SELECT_PLAN
                ).fetchall()
            )

    def classify(self, path: Path) -> str:
        """Fail-closed oracle: a journal path is v1-awaiting-migration or a
        serving v2 — never anything else."""

        try:
            journal = BrokerJournal.open(path)
        except MigrationRequired:
            return "v1_migration_required"
        try:
            plan = journal.get_plan(
                subject=self.subject,
                plan_handle=self.plan_result.plan_handle,
                expected_digest=self.plan_result.plan_digest,
            )
            self.assertTrue(plan.validate_digest())
            return "v2_serves"
        finally:
            journal.close()


class OperationLifecycleTests(OperationCaseBase):
    def test_operation_handles_are_random_hex_and_subject_bound(self) -> None:
        record = self.prepare("t1")
        self.assertRegex(record.operation_handle, r"^[0-9a-f]{64}$")
        loaded = self.service.get_operation(1003, record.operation_handle)
        self.assertEqual(loaded, record)
        with self.assertRaises(SubjectMismatch):
            self.service.get_operation(1004, record.operation_handle)
        with self.assertRaises(UnknownHandle):
            self.service.get_operation(1003, "f" * 64)
        bad_factory = self.build_service(handle_factory=lambda: "not-hex")
        with self.assertRaises(HostControlError):
            bad_factory.prepare_apply(
                1003,
                self.plan_result.plan_handle,
                self.plan_result.plan_digest,
                "t1-bad-handle",
                "a1" * 32,
            )
        self.assertEqual(self.op_count(), 1)

    def test_operation_binds_exact_plan_handle_digest_subject_target_and_families(self) -> None:
        with self.assertRaises(HostControlError):
            self.service.prepare_apply(
                1003,
                self.plan_result.plan_handle,
                "a" * 64,
                "t2-wrong-digest",
                "a1" * 32,
            )
        with self.assertRaises(UnknownHandle):
            self.service.prepare_apply(
                1003,
                "b" * 64,
                self.plan_result.plan_digest,
                "t2-unknown-plan",
                "a1" * 32,
            )
        with self.assertRaises(SubjectMismatch):
            self.service.prepare_apply(
                1004,
                self.plan_result.plan_handle,
                self.plan_result.plan_digest,
                "t2-foreign-subject",
                "a1" * 32,
            )
        expired_clock = lambda: FIXED_NOW + timedelta(hours=2)  # noqa: E731
        expired_service = self.build_service(clock=expired_clock)
        with self.assertRaises(HostControlError):
            expired_service.prepare_apply(
                1003,
                self.plan_result.plan_handle,
                self.plan_result.plan_digest,
                "t2-expired-plan",
                "a1" * 32,
            )
        # every refusal above happened BEFORE journaling
        self.assertEqual(self.op_count(), 0)
        record = self.prepare("t2-good")
        self.assertEqual(record.plan_handle, self.plan_result.plan_handle)
        self.assertEqual(record.plan_digest, self.plan_result.plan_digest)
        self.assertEqual(record.target_seat_id, "recovery")
        self.assertEqual(record.operation_families, ("persona.install",))
        self.assertEqual(record.owner, self.subject)

    def test_rollback_operation_binds_exact_receipt_handle_and_digest(self) -> None:
        # T-2917 5d.2: a rollback needs a RESTORABLE receipt (a changed
        # mutation matching a write-ahead intent of the source apply)
        dispatched = self.to_dispatched("t3-src")
        intent = next(
            item for item in self.journal.intents_for(dispatched.operation_handle) if item.kind == "mkdir"
        )
        completed = self.service.append_result(
            dispatched.operation_handle,
            ok=True,
            mutations=(
                MutationRecord(
                    intent.operation_id, intent.kind, intent.target, intent.owner, True, {"state": "absent"}, "directory"
                ),
            ),
            evidence=(self.sample_evidence(),),
            detail="done",
        )
        complete_handle = self.receipt_handle_for(
            completed.operation_handle,
            "complete",
        )
        staged_handle = self.receipt_handle_for(
            completed.operation_handle,
            "staged",
        )
        record, replayed = self.service.prepare_rollback(
            1003,
            complete_handle,
            "t3-rollback",
            "ab" * 32,
        )
        self.assertFalse(replayed)
        self.assertEqual(record.kind, "rollback")
        self.assertEqual(record.receipt_handle, complete_handle)
        stored_receipt = self.journal.get_receipt(
            subject=self.subject,
            receipt_handle=complete_handle,
        )
        self.assertEqual(record.receipt_digest, stored_receipt.digest)
        self.assertEqual(record.plan_handle, completed.plan_handle)
        self.assertEqual(record.plan_digest, completed.plan_digest)
        self.assertEqual(
            record.operation_families,
            completed.operation_families,
        )
        with self.assertRaises(UnknownHandle):
            self.service.prepare_rollback(
                1003,
                "e" * 64,
                "t3-unknown",
                "ab" * 32,
            )
        with self.assertRaises(HostControlError):
            self.service.prepare_rollback(
                1003,
                staged_handle,
                "t3-staged",
                "ab" * 32,
            )
        with self.assertRaises(SubjectMismatch):
            self.service.prepare_rollback(
                1004,
                complete_handle,
                "t3-foreign",
                "ab" * 32,
            )

    def test_operation_idempotency_replays_identical_and_refuses_conflict(self) -> None:
        first = self.prepare("t4-same", "aa" * 32)
        replay, replayed = self.service.prepare_apply(
            1003,
            self.plan_result.plan_handle,
            self.plan_result.plan_digest,
            "t4-same",
            "aa" * 32,
        )
        self.assertTrue(replayed)
        self.assertEqual(first.operation_handle, replay.operation_handle)
        self.assertEqual(self.op_count(), 1)
        with self.assertRaises(IdempotencyConflict):
            self.service.prepare_apply(
                1003,
                self.plan_result.plan_handle,
                self.plan_result.plan_digest,
                "t4-same",
                "bb" * 32,
            )
        self.assertEqual(self.op_count(), 1)
        other = self.prepare("t4-other", "aa" * 32)
        self.assertNotEqual(other.operation_handle, first.operation_handle)
        self.assertEqual(self.op_count(), 2)
        # database constraint, not only service checks
        with sqlite3.connect(self.journal_path) as connection:
            row = list(
                connection.execute(
                    "SELECT * FROM operations WHERE operation_handle=?",
                    (first.operation_handle,),
                ).fetchone()
            )
            row[0] = "c" * 64
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO operations VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    row,
                )
            connection.rollback()

    def test_state_machine_rejects_every_illegal_transition(self) -> None:
        # pure layer: exhaustive product over all states (same-process,
        # logic-correctness only)
        legal = 0
        for index, from_state in enumerate(sorted(OPERATION_STATES)):
            base = self.record_in_state(from_state, f"t5-{index}")
            for to_state in sorted(OPERATION_STATES):
                expected_legal = to_state in OPERATION_TRANSITIONS[from_state]
                with self.subTest(edge=f"{from_state}->{to_state}"):
                    if expected_legal:
                        legal += 1
                        result = transition(
                            base,
                            to_state,
                            updated_at="2026-08-12T01:05:00+00:00",
                            authority_record_id=(
                                "authority-1"
                                if from_state == "prepared"
                                and to_state == "authorized"
                                else None
                            ),
                        )
                        self.assertEqual(result.state, to_state)
                        self.assertTrue(result.validate_digest())
                    else:
                        with self.assertRaises(InvalidTransition):
                            transition(
                                base,
                                to_state,
                                updated_at="2026-08-12T01:05:00+00:00",
                            )
        self.assertEqual(legal, 10)
        # SQL layer: a stale current record cannot move the row
        record = self.prepare("t5-sql")
        authorized = self.service.authorize(record.operation_handle)
        events_before = self.event_rows(record.operation_handle)
        stale_new = transition(
            record,
            "refused",
            updated_at="2026-08-12T01:06:00+00:00",
        )
        with self.assertRaises(HostControlError):
            self.journal.transition_operation(
                current=record,
                new=stale_new,
                event_kind="stale-attempt",
                payload={},
            )
        self.assertEqual(self.db_state(record.operation_handle), "authorized")
        self.assertEqual(
            self.event_rows(record.operation_handle),
            events_before,
        )
        self.assertEqual(authorized.state, "authorized")

    def test_terminal_states_are_terminal_and_manual_recovery_is_loud_and_non_reusable(self) -> None:
        completed = self.to_completed("t6-done")
        for to_state in sorted(OPERATION_STATES):
            with self.subTest(target=to_state):
                with self.assertRaises(InvalidTransition):
                    transition(
                        completed,
                        to_state,
                        updated_at="2026-08-12T01:07:00+00:00",
                    )
        # manual_recovery via irreconcilable reconciliation
        parked_service = self.build_service(
            reconciler=FakeReconciler(
                ReconcileOutcome("irreconcilable", (), (), "cannot restore")
            )
        )
        record = self.to_dispatched("t6-park")
        count_before = self.op_count()
        with self.assertRaises(RecoveryRefused):
            parked_service.recover(record.operation_handle)
        self.assertEqual(
            self.db_state(record.operation_handle),
            "manual_recovery",
        )
        # the idempotency tuple stays consumed: a repeat call returns the
        # parked operation loudly instead of minting a new one
        replay, replayed = self.service.prepare_apply(
            1003,
            self.plan_result.plan_handle,
            self.plan_result.plan_digest,
            "t6-park",
            "a1" * 32,
        )
        self.assertTrue(replayed)
        self.assertEqual(replay.operation_handle, record.operation_handle)
        self.assertEqual(replay.state, "manual_recovery")
        self.assertEqual(self.op_count(), count_before)
        parked = self.journal.load_operation_unbound(record.operation_handle)
        with self.assertRaises(InvalidTransition):
            transition(
                parked,
                "authorized",
                updated_at="2026-08-12T01:08:00+00:00",
                authority_record_id="authority-1",
            )

    def test_staged_receipt_and_intents_durably_synced_before_dispatch(self) -> None:
        # Commit-VISIBILITY ordering via a second connection (same-process);
        # the durable crash boundary is carried by
        # test_operation_commit_survives_cross_process_restart.
        record = self.to_authorized("t7")
        peek = JournalPeekExecutor(self.journal_path)
        dispatched = self.service.dispatch(record.operation_handle, peek)
        self.assertIsNotNone(peek.observed)
        state, phases, intent_rows = peek.observed
        self.assertEqual(state, "dispatch_intent")
        self.assertEqual(phases, [("staged",)])
        self.assertEqual(len(intent_rows), 15)  # D6 (T-2917 5c): +1 broker-synthesized timer-enable intent
        self.assertTrue(all(resolved == 0 for _, resolved in intent_rows))
        self.assertEqual(dispatched.state, "dispatched")
        # natural crash cut: executor dies AFTER the durable write; the
        # write-ahead truth is already committed
        crashed = self.to_dispatch_intent("t7-crash")
        self.assertEqual(crashed.state, "dispatch_intent")
        staged = self.journal.get_staged_receipt(crashed.operation_handle)
        self.assertEqual(staged.phase, "staged")
        self.assertEqual(
            len(self.journal.unresolved_intents(crashed.operation_handle)),
            15,  # D6 (T-2917 5c): +1 broker-synthesized timer-enable intent
        )

    def test_executor_seam_cannot_write_journal(self) -> None:
        # structural: the executor receives frozen typed values and no
        # journal reference of any kind
        record = self.to_authorized("t8")
        executor = FakeExecutor()
        self.service.dispatch(record.operation_handle, executor)
        operation, staged_receipt, intents = executor.calls[0]
        for value in (operation, staged_receipt, *intents):
            self.assertTrue(dataclasses.is_dataclass(value))
            with self.assertRaises(dataclasses.FrozenInstanceError):
                object.__setattr__  # keep linters honest
                value.__setattr__("state", "completed")
            for attribute in vars(value).values():
                self.assertNotIsInstance(attribute, BrokerJournal)
                self.assertNotIsInstance(attribute, sqlite3.Connection)
        # behavioral: a result append for a wrong-state operation is refused
        prepared = self.prepare("t8-prepared")
        with self.assertRaises(InvalidTransition):
            self.service.append_result(
                prepared.operation_handle,
                ok=True,
                mutations=(),
                evidence=(),
            )
        self.assertEqual(self.db_state(prepared.operation_handle), "prepared")

    def test_bind_outage_fails_closed_and_prepared_stays_retryable(self) -> None:
        binder = FakeBinder(outcomes=[ApprovalUnavailable("authority down")])
        service = self.build_service(binder=binder)
        record, _ = service.prepare_apply(
            1003,
            self.plan_result.plan_handle,
            self.plan_result.plan_digest,
            "t9",
            "a1" * 32,
        )
        with self.assertRaises(ApprovalUnavailable):
            service.authorize(record.operation_handle)
        self.assertEqual(self.db_state(record.operation_handle), "prepared")
        # no cached authority, no transition event for the outage
        self.assertEqual(
            [row[3] for row in self.event_rows(record.operation_handle)],
            ["prepared"],
        )
        recovered = service.authorize(record.operation_handle)
        self.assertEqual(recovered.state, "authorized")
        self.assertEqual(recovered.authority_record_id, "authority-1")

    def test_identical_tuple_bind_retry_and_different_tuple_denial(self) -> None:
        binder = FakeBinder(
            outcomes=[ApprovalUnavailable("transient outage")]
        )
        service = self.build_service(binder=binder)
        record, _ = service.prepare_apply(
            1003,
            self.plan_result.plan_handle,
            self.plan_result.plan_digest,
            "t10",
            "ac" * 32,
        )
        with self.assertRaises(ApprovalUnavailable):
            service.authorize(record.operation_handle)
        service.authorize(record.operation_handle)
        self.assertEqual(len(binder.calls), 2)
        self.assertEqual(binder.calls[0], binder.calls[1])
        self.assertEqual(
            binder.calls[0],
            (
                "ac" * 32,
                record.plan_handle,
                record.plan_digest,
                record.operation_handle,
            ),
        )
        # denial is terminal refusal, distinct from outage
        denied_binder = FakeBinder(
            outcomes=[BindOutcome("denied", None, "revoked")]
        )
        denied_service = self.build_service(binder=denied_binder)
        denied, _ = denied_service.prepare_apply(
            1003,
            self.plan_result.plan_handle,
            self.plan_result.plan_digest,
            "t10-denied",
            "ad" * 32,
        )
        refused = denied_service.authorize(denied.operation_handle)
        self.assertEqual(refused.state, "refused")
        self.assertIsNone(refused.authority_record_id)
        with self.assertRaises(InvalidTransition):
            denied_service.authorize(denied.operation_handle)


class RecoveryCutTests(OperationCaseBase):
    def test_recovery_prepared_with_no_binding_retries_or_terminates_refused(self) -> None:
        # cut: PREPARED with no binding (ORG-052 section 6)
        record = self.prepare("t11-bound")
        outcome = self.service.recover(record.operation_handle)
        self.assertEqual(outcome.action, "authorized")
        self.assertEqual(outcome.record.state, "authorized")

        denied_service = self.build_service(
            binder=FakeBinder(outcomes=[BindOutcome("denied", None, "expired")])
        )
        denied = self.prepare("t11-denied")
        denied_outcome = denied_service.recover(denied.operation_handle)
        self.assertEqual(denied_outcome.action, "refused")
        self.assertEqual(self.db_state(denied.operation_handle), "refused")

        outage_service = self.build_service(
            binder=FakeBinder(outcomes=[ApprovalUnavailable("down")])
        )
        stuck = self.prepare("t11-outage")
        with self.assertRaises(ApprovalUnavailable):
            outage_service.recover(stuck.operation_handle)
        self.assertEqual(self.db_state(stuck.operation_handle), "prepared")
        retried = outage_service.recover(stuck.operation_handle)
        self.assertEqual(retried.action, "authorized")

    def test_recovery_bound_grant_with_broker_prepared_records_authorized_and_continues(self) -> None:
        # cut: approval BOUND with broker PREPARED — the identical-tuple
        # retry returns the ORIGINAL binding (fake mimics the approval-side
        # replay) and the broker records AUTHORIZED, then continues.
        binder = FakeBinder()
        binder.default = BindOutcome("bound", "authority-replay", "bound")
        service = self.build_service(binder=binder)
        record, _ = service.prepare_apply(
            1003,
            self.plan_result.plan_handle,
            self.plan_result.plan_digest,
            "t12",
            "a1" * 32,
        )
        outcome = service.recover(record.operation_handle)
        self.assertEqual(outcome.action, "authorized")
        self.assertEqual(
            outcome.record.authority_record_id,
            "authority-replay",
        )
        continued = service.recover(
            record.operation_handle,
            executor=FakeExecutor(),
        )
        self.assertEqual(continued.action, "dispatched")
        self.assertEqual(self.db_state(record.operation_handle), "dispatched")
        staged = self.journal.get_staged_receipt(record.operation_handle)
        self.assertEqual(staged.phase, "staged")

    def test_recovery_bound_grant_with_missing_or_corrupt_prepared_refuses_loudly_grant_consumed(self) -> None:
        # cut: matching prepared record MISSING -> loud refusal, no record
        # invented
        with self.assertRaises(RecoveryRefused):
            self.service.recover("d" * 64)
        # cut: matching prepared record CORRUPT -> loud refusal, parked in
        # manual_recovery, tuple stays consumed
        record = self.prepare("t13")
        with sqlite3.connect(self.journal_path) as connection:
            connection.execute(
                "UPDATE operations SET operation_json=? "
                "WHERE operation_handle=?",
                ('{"broken":true}', record.operation_handle),
            )
            connection.commit()
        with self.assertRaises(RecoveryRefused):
            self.service.recover(record.operation_handle)
        self.assertEqual(
            self.db_state(record.operation_handle),
            "manual_recovery",
        )
        self.assertIn(
            "corruption",
            [row[3] for row in self.event_rows(record.operation_handle)],
        )
        # a replay of the same tuple is LOUD (corrupt record refuses),
        # and no new operation is minted
        count_before = self.op_count()
        with self.assertRaises(HostControlError):
            self.service.prepare_apply(
                1003,
                self.plan_result.plan_handle,
                self.plan_result.plan_digest,
                "t13",
                "a1" * 32,
            )
        self.assertEqual(self.op_count(), count_before)
        with self.assertRaises(HostControlError):
            self.service.get_operation(1003, record.operation_handle)

    def test_recovery_authorized_without_dispatch_intent_resumes_from_journal(self) -> None:
        # cut: AUTHORIZED without DISPATCH_INTENT
        record = self.to_authorized("t14")
        pending = self.service.recover(record.operation_handle)
        self.assertEqual(pending.action, "dispatch_pending")
        self.assertEqual(self.db_state(record.operation_handle), "authorized")
        outcome = self.service.recover(
            record.operation_handle,
            executor=FakeExecutor(),
        )
        self.assertEqual(outcome.action, "dispatched")
        self.assertEqual(self.db_state(record.operation_handle), "dispatched")
        self.assertEqual(
            len(self.journal.unresolved_intents(record.operation_handle)),
            15,  # D6 (T-2917 5c): +1 broker-synthesized timer-enable intent
        )

    def test_recovery_dispatch_intent_without_result_reconciles_before_proceeding(self) -> None:
        # cut: DISPATCH_INTENT without a result — reconcile via staged
        # receipt + unresolved intents, then proceed deterministically
        clean = self.to_dispatch_intent("t15-clean")
        clean_service = self.build_service(
            reconciler=FakeReconciler(
                ReconcileOutcome("clean_prestate", (), (), "no mutation seen")
            )
        )
        pending = clean_service.recover(clean.operation_handle)
        self.assertEqual(pending.action, "redispatch_pending")
        outcome = clean_service.recover(
            clean.operation_handle,
            executor=FakeExecutor(),
        )
        self.assertEqual(outcome.action, "dispatched")

        complete = self.to_dispatch_intent("t15-complete")
        complete_service = self.build_service(
            reconciler=FakeReconciler(
                ReconcileOutcome(
                    "complete_poststate",
                    (self.sample_mutation(),),
                    (self.sample_evidence(),),
                    "post-state observed",
                )
            )
        )
        done = complete_service.recover(complete.operation_handle)
        self.assertEqual(done.action, "completed")
        self.assertEqual(self.db_state(complete.operation_handle), "completed")
        self.assertEqual(
            self.journal.unresolved_intents(complete.operation_handle),
            (),
        )
        self.assertEqual(
            self.journal.get_receipt(
                subject=self.subject,
                receipt_handle=self.receipt_handle_for(
                    complete.operation_handle,
                    "complete",
                ),
            ).phase,
            "complete",
        )

        restored = self.to_dispatch_intent("t15-restored")
        restored_service = self.build_service(
            reconciler=FakeReconciler(
                ReconcileOutcome(
                    "restored",
                    (self.sample_mutation(),),
                    (self.sample_evidence(),),
                    "rolled back per intents",
                )
            )
        )
        failed = restored_service.recover(restored.operation_handle)
        self.assertEqual(failed.action, "failed")
        self.assertEqual(self.db_state(restored.operation_handle), "failed")
        self.assertEqual(
            self.journal.unresolved_intents(restored.operation_handle),
            (),
        )

        stuck = self.to_dispatch_intent("t15-stuck")
        stuck_service = self.build_service(
            reconciler=FakeReconciler(
                ReconcileOutcome("irreconcilable", (), (), "diverged")
            )
        )
        with self.assertRaises(RecoveryRefused):
            stuck_service.recover(stuck.operation_handle)
        self.assertEqual(
            self.db_state(stuck.operation_handle),
            "manual_recovery",
        )
        # intents stay UNRESOLVED: they remain outstanding evidence for the
        # operator, loudly
        self.assertEqual(
            len(self.journal.unresolved_intents(stuck.operation_handle)),
            15,  # D6 (T-2917 5c): +1 broker-synthesized timer-enable intent
        )

    def test_recovery_dispatched_without_result_follows_same_reconciliation(self) -> None:
        # cut: DISPATCHED without a result — same reconciliation path
        complete = self.to_dispatched("t16-complete")
        complete_service = self.build_service(
            reconciler=FakeReconciler(
                ReconcileOutcome(
                    "complete_poststate",
                    (self.sample_mutation(),),
                    (self.sample_evidence(),),
                    "post-state observed",
                )
            )
        )
        done = complete_service.recover(complete.operation_handle)
        self.assertEqual(done.action, "completed")

        restored = self.to_dispatched("t16-restored")
        restored_service = self.build_service(
            reconciler=FakeReconciler(
                ReconcileOutcome("restored", (), (), "rolled back")
            )
        )
        failed = restored_service.recover(restored.operation_handle)
        self.assertEqual(failed.action, "failed")

        clean = self.to_dispatched("t16-clean")
        clean_service = self.build_service(
            reconciler=FakeReconciler(
                ReconcileOutcome("clean_prestate", (), (), "no effect seen")
            )
        )
        executor = FakeExecutor(ref="exec-ref-retry")
        redone = clean_service.recover(
            clean.operation_handle,
            executor=executor,
        )
        self.assertEqual(redone.action, "redispatched")
        self.assertEqual(self.db_state(clean.operation_handle), "dispatched")
        self.assertEqual(len(executor.calls), 1)
        events = self.event_rows(clean.operation_handle)
        self.assertEqual(events[-1][3], "redispatched")
        self.assertEqual(events[-1][1], events[-1][2])

        stuck = self.to_dispatched("t16-stuck")
        stuck_service = self.build_service(
            reconciler=FakeReconciler(
                ReconcileOutcome("irreconcilable", (), (), "diverged")
            )
        )
        with self.assertRaises(RecoveryRefused):
            stuck_service.recover(stuck.operation_handle)
        self.assertEqual(
            self.db_state(stuck.operation_handle),
            "manual_recovery",
        )


@contextlib.contextmanager
def held_write_lock(path: Path):
    """Hold a journal's write lock from a second connection.

    ``BEGIN IMMEDIATE`` returns only once the lock is actually held, so any
    journal write attempted inside the with-block is guaranteed to contend
    for as long as the block lasts: a deterministic handshake with no
    sleeps and no dependence on host load.
    """

    holder = sqlite3.connect(path, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        yield
    finally:
        try:
            holder.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        holder.close()


class LockContentionTests(OperationCaseBase):
    """Broker write-lock contention refuses with JournalBusy, never a raw
    sqlite error (T-4334). Contention is forced deterministically: a second
    connection HOLDS the write lock (BEGIN IMMEDIATE has returned) while the
    journal under test has a 50 ms busy timeout. The shared classifier and
    the approval journal's sites are proven in test_host_control_approval.
    """

    BUSY_MS = 50

    def setUp(self) -> None:
        super().setUp()
        # setup work runs through self.service / self.planner (default 5 s
        # timeout); only the act under contention uses the short journal
        self.short = BrokerJournal.open(
            self.journal_path, busy_timeout_ms=self.BUSY_MS
        )
        self.short_service = OperationService(
            registry=self.registry,
            journal=self.short,
            binder=self.binder,
            reconciler=self.reconciler,
            clock=lambda: FIXED_NOW,
            handle_factory=self.next_handle,
        )
        self.short_planner = HostPlanService(
            registry=self.registry,
            policy=self.policy,
            journal=self.short,
            installer=self.installer,
            clock=lambda: FIXED_NOW,
            handle_factory=self.next_handle,
            plan_ttl=timedelta(hours=1),
            contract_version="org.pantheon.HostBroker1/v1",
            broker_revision="unit-test",
            host_operation_families=frozenset(
                ("persona.install", "persona.verify")
            ),
            health_check=lambda: "ready",
        )

    def tearDown(self) -> None:
        self.short.close()
        super().tearDown()

    def plan_count(self) -> int:
        with sqlite3.connect(self.journal_path) as connection:
            return connection.execute(
                "SELECT count(*) FROM plans"
            ).fetchone()[0]

    def assert_busy(self, caught, site: str) -> None:
        error = caught.exception
        self.assertIsInstance(error, HostControlError)
        self.assertIn(site, str(error))
        self.assertIn("lock contention", str(error))
        self.assertIn(f"{self.BUSY_MS} ms", str(error))
        # The message is the wire body (bounded but verbatim): it must
        # never name where the journal lives. The root check also catches
        # a parent-dir leak.
        self.assertNotIn(str(self.journal_path), str(error))
        self.assertNotIn(str(self.root), str(error))
        self.assertIsInstance(error.__cause__, sqlite3.OperationalError)
        self.assertEqual(error.__cause__.sqlite_errorcode & 0xFF, 5)

    def test_open_applies_the_injected_busy_timeout_to_the_connection(self) -> None:
        # Pins the seam itself: the injected value must reach the live
        # connection. Wall time alone cannot prove it (a hard-coded 5000
        # would still lose the lock, only slower), so read the PRAGMA back
        # on the journal's own connection (busy_timeout is per-connection).
        self.assertEqual(
            self.short._connection.execute(
                "PRAGMA busy_timeout"
            ).fetchone()[0],
            self.BUSY_MS,
        )
        # the fixture's default-opened journal keeps the documented 5 s
        self.assertEqual(
            self.journal._connection.execute(
                "PRAGMA busy_timeout"
            ).fetchone()[0],
            5000,
        )

    def test_plan_refuses_with_journal_busy_and_writes_nothing(self) -> None:
        plans_before = self.plan_count()
        with held_write_lock(self.journal_path):
            with self.assertRaises(JournalBusy) as caught:
                self.short_planner.plan(
                    1003, "recovery", "manifest-r1", "busy-plan"
                )
        self.assert_busy(caught, "put_plan_or_replay")
        self.assertEqual(self.plan_count(), plans_before)
        # retryable once the lock frees
        result = self.short_planner.plan(
            1003, "recovery", "manifest-r1", "busy-plan"
        )
        self.assertEqual(self.plan_count(), plans_before + 1)
        self.assertTrue(result.plan_handle)

    def test_prepare_refuses_with_journal_busy_and_writes_nothing(self) -> None:
        with held_write_lock(self.journal_path):
            with self.assertRaises(JournalBusy) as caught:
                self.short_service.prepare_apply(
                    1003,
                    self.plan_result.plan_handle,
                    self.plan_result.plan_digest,
                    "op-busy",
                    "a1" * 32,
                )
        self.assert_busy(caught, "put_operation_or_replay")
        self.assertEqual(self.op_count(), 0)
        record, replayed = self.short_service.prepare_apply(
            1003,
            self.plan_result.plan_handle,
            self.plan_result.plan_digest,
            "op-busy",
            "a1" * 32,
        )
        self.assertFalse(replayed)
        self.assertEqual(record.state, "prepared")

    def test_authorize_refuses_with_journal_busy_and_stays_prepared(self) -> None:
        record = self.prepare("op-auth")
        with held_write_lock(self.journal_path):
            with self.assertRaises(JournalBusy) as caught:
                self.short_service.authorize(record.operation_handle)
        self.assert_busy(caught, "transition_operation[bound]")
        self.assertEqual(self.db_state(record.operation_handle), "prepared")
        events = self.event_rows(record.operation_handle)
        authorized = self.short_service.authorize(record.operation_handle)
        self.assertEqual(authorized.state, "authorized")
        self.assertEqual(len(self.event_rows(record.operation_handle)), len(events) + 1)

    def test_dispatch_never_invokes_the_executor_without_the_durable_intent(self) -> None:
        # ORG-052 section 6 ordering: the write-ahead intent transaction is
        # the gate to the executor. A lost lock must stop BEFORE the call.
        record = self.to_authorized("op-dispatch")
        executor = FakeExecutor()
        with held_write_lock(self.journal_path):
            with self.assertRaises(JournalBusy) as caught:
                self.short_service.dispatch(record.operation_handle, executor)
        self.assert_busy(caught, "record_dispatch_intent")
        self.assertEqual(executor.calls, [])
        self.assertEqual(self.db_state(record.operation_handle), "authorized")
        dispatched = self.short_service.dispatch(
            record.operation_handle, executor
        )
        self.assertEqual(dispatched.state, "dispatched")
        self.assertEqual(len(executor.calls), 1)

    def test_append_result_refuses_with_journal_busy_and_stays_dispatched(self) -> None:
        record = self.to_dispatched("op-result")
        with held_write_lock(self.journal_path):
            with self.assertRaises(JournalBusy) as caught:
                self.short_service.append_result(
                    record.operation_handle,
                    ok=True,
                    mutations=(self.sample_mutation(),),
                    evidence=(self.sample_evidence(),),
                    detail="done",
                )
        self.assert_busy(caught, "record_terminal")
        self.assertEqual(self.db_state(record.operation_handle), "dispatched")

    def test_journal_level_sites_refuse_with_journal_busy(self) -> None:
        record = self.prepare("op-sites")
        sites = {
            "append_event[note]": lambda: self.short.append_event(
                current=record,
                event_kind="note",
                payload={},
                created_at=FIXED_NOW.isoformat(),
            ),
            "mark_manual_recovery": lambda: self.short.mark_manual_recovery(
                record.operation_handle,
                "lock contention probe",
                marked_at=FIXED_NOW.isoformat(),
            ),
        }
        events_before = self.event_rows(record.operation_handle)
        for site, act in sites.items():
            with self.subTest(site=site):
                with held_write_lock(self.journal_path):
                    with self.assertRaises(JournalBusy) as caught:
                        act()
                self.assert_busy(caught, site)
        # nothing was written, and a busy park never became manual_recovery
        self.assertEqual(self.db_state(record.operation_handle), "prepared")
        self.assertEqual(
            self.event_rows(record.operation_handle), events_before
        )

    def test_non_lock_operational_errors_surface_as_is(self) -> None:
        class BeginFails:
            """A connection whose BEGIN IMMEDIATE fails for a non-lock
            reason; every other statement mimics 'no transaction'."""

            def __init__(self, error: sqlite3.OperationalError) -> None:
                self.error = error

            def execute(self, sql: str, *args: object) -> None:
                if sql == "BEGIN IMMEDIATE":
                    raise self.error
                raise sqlite3.OperationalError(
                    "cannot rollback - no transaction is active"
                )

        record = self.prepare("op-nonlock")
        error = sqlite3.OperationalError("unable to open database file")
        journal = BrokerJournal(self.journal_path, BeginFails(error))
        with self.assertRaises(sqlite3.OperationalError) as caught:
            journal.append_event(
                current=record,
                event_kind="note",
                payload={},
                created_at=FIXED_NOW.isoformat(),
            )
        self.assertIs(caught.exception, error)
        self.assertNotIsInstance(caught.exception, HostControlError)


class DurabilityTests(OperationCaseBase):
    def test_operation_commit_survives_cross_process_restart(self) -> None:
        # REAL second process: this parent commits each lifecycle stage,
        # a fresh interpreter reopens the journal, validates the record's
        # digest chain, and (for the unresolved-dispatch stage) recovers.
        read_script = """
import sys
from pathlib import Path
from tools.host_control.journal import BrokerJournal
journal = BrokerJournal.open(Path(sys.argv[1]))
record = journal.load_operation_unbound(sys.argv[2])
assert record.validate_digest()
print(record.state)
journal.close()
"""
        stages = {
            "prepared": self.prepare("t17-prepared").operation_handle,
            "authorized": self.to_authorized(
                "t17-authorized"
            ).operation_handle,
            "dispatch_intent": self.to_dispatch_intent(
                "t17-intent"
            ).operation_handle,
            "dispatched": self.to_dispatched("t17-dispatched").operation_handle,
            "completed": self.to_completed("t17-completed").operation_handle,
        }
        for expected_state, handle in stages.items():
            with self.subTest(stage=expected_state):
                completed = subprocess.run(
                    (
                        sys.executable,
                        "-c",
                        read_script,
                        str(self.journal_path),
                        handle,
                    ),
                    cwd=REPO_ROOT,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.stdout.strip(), expected_state)
        recover_script = """
import itertools
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from tools.host_control.contracts import ReconcileOutcome
from tools.host_control.journal import BrokerJournal
from tools.host_control.operations import OperationService
from tools.host_control.registry import SeatRegistry
from tools.host_control.trusted_files import RootOwnedFileLoader


class NoBinder:
    def validate_and_bind(self, *args):
        raise AssertionError("bind must not be called for this stage")


class PoststateReconciler:
    def reconcile(self, operation, staged_receipt, unresolved_intents):
        assert staged_receipt.phase == "staged"
        # D6 (T-2917 5c): +1 broker-synthesized timer-enable intent
        assert len(unresolved_intents) == 15
        return ReconcileOutcome(
            "complete_poststate", (), (), "recovered cross-process"
        )


files = RootOwnedFileLoader._open_for_test(
    (Path(sys.argv[3]),), expected_owner_uid=os.getuid()
)
registry = SeatRegistry.load(Path(sys.argv[4]), files=files)
journal = BrokerJournal.open(Path(sys.argv[1]))
counter = itertools.count(9000)
service = OperationService(
    registry=registry,
    journal=journal,
    binder=NoBinder(),
    reconciler=PoststateReconciler(),
    clock=lambda: datetime(2026, 8, 12, 1, 30, tzinfo=timezone.utc),
    handle_factory=lambda: format(next(counter), "064x"),
)
outcome = service.recover(sys.argv[2])
print(outcome.action, outcome.record.state)
journal.close()
files.close()
"""
        completed = subprocess.run(
            (
                sys.executable,
                "-c",
                recover_script,
                str(self.journal_path),
                stages["dispatch_intent"],
                str(self.root),
                str(self.registry_path),
            ),
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.stdout.strip(), "completed completed")
        # and the parent-side connection observes the subprocess's commit
        self.assertEqual(
            self.db_state(stages["dispatch_intent"]),
            "completed",
        )

    def test_uncommitted_or_corrupt_operation_never_serves(self) -> None:
        record = self.prepare("t18")
        # REAL second process dying pre-commit: the row never becomes
        # visible
        death_script = """
import os
import sqlite3
import sys
connection = sqlite3.connect(sys.argv[1])
connection.execute("BEGIN IMMEDIATE")
connection.execute(
    "INSERT INTO operations SELECT ?, operation_kind, state, subject_uid, "
    "subject_seat_id, target_seat_id, plan_handle, plan_digest, "
    "receipt_handle, receipt_digest, approval_handle, authority_record_id, "
    "?, request_digest, operation_json, created_at, updated_at "
    "FROM operations LIMIT 1",
    ("f" * 64, "uncommitted-key"),
)
os._exit(17)
"""
        completed = subprocess.run(
            (sys.executable, "-c", death_script, str(self.journal_path)),
            cwd=REPO_ROOT,
            check=False,
        )
        self.assertEqual(completed.returncode, 17)
        with sqlite3.connect(self.journal_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM operations WHERE operation_handle=?",
                    ("f" * 64,),
                ).fetchone()[0],
                0,
            )
        # corruption is loud, never repaired by inference (same-process;
        # logic correctness)
        with sqlite3.connect(self.journal_path) as connection:
            original = connection.execute(
                "SELECT operation_json FROM operations "
                "WHERE operation_handle=?",
                (record.operation_handle,),
            ).fetchone()[0]

        def overwrite(value: str) -> None:
            with sqlite3.connect(self.journal_path) as inner:
                inner.execute(
                    "UPDATE operations SET operation_json=? "
                    "WHERE operation_handle=?",
                    (value, record.operation_handle),
                )
                inner.commit()

        noncanonical = json.dumps(json.loads(original), indent=2)
        overwrite(noncanonical)
        with self.assertRaises(HostControlError):
            self.service.get_operation(1003, record.operation_handle)
        tampered = dict(json.loads(original))
        tampered["digest"] = "c" * 64
        overwrite(canonical_json(tampered))
        with self.assertRaises(HostControlError):
            self.service.get_operation(1003, record.operation_handle)
        overwrite(original)
        # column divergence: row state disagrees with the record JSON
        with sqlite3.connect(self.journal_path) as connection:
            connection.execute(
                "UPDATE operations SET state='authorized' "
                "WHERE operation_handle=?",
                (record.operation_handle,),
            )
            connection.commit()
        with self.assertRaises(HostControlError):
            self.service.get_operation(1003, record.operation_handle)


class JournalV2SchemaTests(OperationCaseBase):
    def test_journal_v2_schema_locked_and_refuses_drift(self) -> None:
        self.assertEqual(
            hashlib.sha256(DDL.encode("utf-8")).hexdigest(),
            SCHEMA_DIGEST,
        )
        self.assertNotEqual(SCHEMA_DIGEST, SCHEMA_DIGEST_V1)
        self.assertEqual(USER_VERSION, 2)
        # the v1 plans DDL is embedded VERBATIM (migrated journals validate
        # byte-wise against sqlite_master)
        self.assertIn(
            DDL_V1[DDL_V1.index("CREATE TABLE plans") :],
            DDL,
        )
        with sqlite3.connect(self.journal_path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA application_id").fetchone()[0],
                APPLICATION_ID,
            )
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                2,
            )
            tables = connection.execute(
                "SELECT name, strict FROM pragma_table_list "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            self.assertEqual(tables, V2_TABLES)

        def assert_refused(name: str, mutation: str) -> None:
            path = self.root / f"{name}.sqlite3"
            journal = BrokerJournal.open(path)
            journal.close()
            with sqlite3.connect(path) as connection:
                connection.executescript(mutation)
                connection.commit()
            with self.subTest(schema_case=name):
                with self.assertRaises(HostControlError) as caught:
                    BrokerJournal.open(path)
                self.assertNotIsInstance(caught.exception, MigrationRequired)

        assert_refused("older-marker", "PRAGMA user_version=1;")
        assert_refused("newer-marker", "PRAGMA user_version=3;")
        assert_refused("wrong-app", "PRAGMA application_id=1;")
        assert_refused(
            "metadata-digest",
            f"UPDATE journal_meta SET schema_digest='{'a' * 64}';",
        )
        assert_refused(
            "missing-index",
            "DROP INDEX operations_state_idx;",
        )
        assert_refused(
            "extra-table",
            "CREATE TABLE extra(value TEXT) STRICT;",
        )
        assert_refused(
            "extra-column",
            "ALTER TABLE operations ADD COLUMN extra TEXT;",
        )
        assert_refused(
            "mutated-column",
            "ALTER TABLE operations RENAME COLUMN state TO changed_state;",
        )
        assert_refused(
            "mutated-index",
            "DROP INDEX operations_state_idx;"
            "CREATE INDEX operations_state_idx ON operations(operation_kind);",
        )
        assert_refused("missing-table", "DROP TABLE rollback_intents;")

        non_strict_path = self.root / "non-strict.sqlite3"
        connection = sqlite3.connect(non_strict_path)
        try:
            connection.executescript(DDL.replace(") STRICT;", ");"))
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={USER_VERSION}")
            connection.execute(
                "INSERT INTO journal_meta VALUES (1, ?, ?, ?)",
                (USER_VERSION, SCHEMA_DIGEST, "2026-08-12T00:00:00+00:00"),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(HostControlError):
            BrokerJournal.open(non_strict_path)
        # the closed terminal-state CHECK holds at the schema tier: a fifth
        # state value cannot be written even by direct SQL
        with sqlite3.connect(self.journal_path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO operations VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "9" * 64,
                        "apply",
                        "obsoleted",
                        1003,
                        "consultant",
                        "recovery",
                        self.plan_result.plan_handle,
                        self.plan_result.plan_digest,
                        None,
                        None,
                        "a1" * 32,
                        None,
                        "schema-check",
                        "9" * 64,
                        "{}",
                        "t",
                        "t",
                    ),
                )
            connection.rollback()
        # "verify" is ACCEPTED at the schema tier (T-2917 needed no
        # migration); the service tier now serves it (5b) but refuses a
        # plan it cannot verify read-only
        with sqlite3.connect(self.journal_path) as connection:
            connection.execute(
                "INSERT INTO operations VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "8" * 64,
                    "verify",
                    "prepared",
                    1003,
                    "consultant",
                    "recovery",
                    self.plan_result.plan_handle,
                    self.plan_result.plan_digest,
                    None,
                    None,
                    "a1" * 32,
                    None,
                    "schema-accepts-verify",
                    "8" * 64,
                    "{}",
                    "t",
                    "t",
                ),
            )
            connection.rollback()
        # T-2917 5b ruling B3: a plan holding a class this revision does not
        # execute makes verify Unsupported BEFORE any operation record is
        # written. T-2917 5d.3 re-points it at the PRE-flip table: the real
        # fixture plan is fully implemented since the mailbox.probe flip.
        from tools.test_host_control_driver import pre_flip_table

        with sqlite3.connect(self.journal_path) as connection:
            before = connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
        with pre_flip_table(), self.assertRaises(Unsupported) as caught:
            self.service.prepare_verify(
                1003,
                "recovery",
                self.plan_result.plan_handle,
                "verify-key",
            )
        self.assertIn("verify not implemented in this revision", str(caught.exception))
        with sqlite3.connect(self.journal_path) as connection:
            after = connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
        self.assertEqual(before, after)


class MigrationTests(OperationCaseBase):
    def make_v1(self, name: str = "legacy.sqlite3") -> Path:
        path = self.root / name
        self.build_v1_journal(path, self.plan_rows())
        return path

    def test_migration_v1_to_v2_preserves_all_plans_and_markers(self) -> None:
        second = self.planner.plan(1004, "recovery", "manifest-r1", "peer-plan")
        rows = self.plan_rows()
        self.assertEqual(len(rows), 2)
        path = self.make_v1()
        BrokerJournal.migrate_v1_to_v2(path)
        journal = BrokerJournal.open(path)
        try:
            for uid, result in ((1003, self.plan_result), (1004, second)):
                stored = journal.get_plan(
                    subject=self.registry.subject_for_uid(uid),
                    plan_handle=result.plan_handle,
                    expected_digest=result.plan_digest,
                )
                self.assertTrue(stored.validate_digest())
                self.assertEqual(stored.effect_digest, result.effect_digest)
        finally:
            journal.close()
        with sqlite3.connect(path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT schema_digest FROM journal_meta"
                ).fetchone()[0],
                SCHEMA_DIGEST,
            )
        backup = Path(f"{path}.v1-backup")
        self.assertTrue(backup.exists())
        with sqlite3.connect(backup) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT count(*) FROM plans").fetchone()[0],
                2,
            )
        # rerun after success is an idempotent no-op
        BrokerJournal.migrate_v1_to_v2(path)
        self.assertEqual(self.classify(path), "v2_serves")

    def test_open_refuses_v1_without_explicit_migration(self) -> None:
        path = self.make_v1()
        before = path.read_bytes()
        with self.assertRaises(MigrationRequired):
            BrokerJournal.open(path)
        # open() mutated nothing: the file is byte-identical and still
        # migratable
        self.assertEqual(path.read_bytes(), before)
        BrokerJournal.migrate_v1_to_v2(path)
        self.assertEqual(self.classify(path), "v2_serves")

    def test_migration_interrupted_at_each_stage_never_serves_half_state(self) -> None:
        # Oracle at every cut: the serving path is EITHER a valid v1
        # (MigrationRequired; rerun converges) OR a valid v2 that serves —
        # never a third state. Post-recovery the journal must actually
        # SERVE (plan read round-trip), not merely validate.
        original_build = BrokerJournal._migrate_build_tmp

        with self.subTest(stage="post-backup"):
            path = self.make_v1("stage-post-backup.sqlite3")
            with mock.patch.object(
                BrokerJournal,
                "_migrate_build_tmp",
                side_effect=HostControlError("injected: died post-backup"),
            ):
                with self.assertRaises(HostControlError):
                    BrokerJournal.migrate_v1_to_v2(path)
            self.assertEqual(self.classify(path), "v1_migration_required")
            BrokerJournal.migrate_v1_to_v2(path)
            self.assertEqual(self.classify(path), "v2_serves")

        with self.subTest(stage="mid-tmp-build"):
            path = self.make_v1("stage-mid-build.sqlite3")

            def broken_build(tmp, rows):
                tmp.write_bytes(b"garbage-half-built-journal")
                raise HostControlError("injected: died mid tmp build")

            with mock.patch.object(
                BrokerJournal,
                "_migrate_build_tmp",
                side_effect=broken_build,
            ):
                with self.assertRaises(HostControlError):
                    BrokerJournal.migrate_v1_to_v2(path)
            self.assertFalse(Path(f"{path}.migrate-tmp").exists())
            self.assertEqual(self.classify(path), "v1_migration_required")
            # a hard kill would leave the garbage tmp behind: prove a stale
            # tmp is never reused
            Path(f"{path}.migrate-tmp").write_bytes(b"stale-garbage")
            BrokerJournal.migrate_v1_to_v2(path)
            self.assertEqual(self.classify(path), "v2_serves")

        with self.subTest(stage="corrupt-tmp-caught-by-verification"):
            path = self.make_v1("stage-corrupt-tmp.sqlite3")

            def corrupting_build(tmp, rows):
                original_build(tmp, rows)
                size = tmp.stat().st_size
                with open(tmp, "r+b") as handle:
                    handle.truncate(size // 2)

            with mock.patch.object(
                BrokerJournal,
                "_migrate_build_tmp",
                side_effect=corrupting_build,
            ):
                with self.assertRaises(HostControlError) as caught:
                    BrokerJournal.migrate_v1_to_v2(path)
            # verification refused BEFORE cutover: the bad file never
            # reached the serving path — this must NOT be the post-cutover
            # restore arm rescuing a cutover that should never have happened
            self.assertNotIn("restored from backup", str(caught.exception))
            self.assertEqual(self.classify(path), "v1_migration_required")
            BrokerJournal.migrate_v1_to_v2(path)
            self.assertEqual(self.classify(path), "v2_serves")

        with self.subTest(stage="pre-verification"):
            path = self.make_v1("stage-pre-verify.sqlite3")
            with mock.patch.object(
                BrokerJournal,
                "_migrate_verify_tmp",
                side_effect=HostControlError("injected: died pre-verify"),
            ):
                with self.assertRaises(HostControlError):
                    BrokerJournal.migrate_v1_to_v2(path)
            self.assertFalse(Path(f"{path}.migrate-tmp").exists())
            self.assertEqual(self.classify(path), "v1_migration_required")
            BrokerJournal.migrate_v1_to_v2(path)
            self.assertEqual(self.classify(path), "v2_serves")

        # pre-replace and post-replace-pre-dirsync run in a REAL second
        # process (subprocess dies; parent inspects and recovers).
        pre_replace_script = """
import sys
from pathlib import Path
import tools.host_control.journal as journal_module


def die_pre_replace(source, target):
    raise journal_module.HostControlError("injected: died pre-replace")


journal_module._atomic_cutover = die_pre_replace
try:
    journal_module.BrokerJournal.migrate_v1_to_v2(Path(sys.argv[1]))
except journal_module.HostControlError:
    import os
    os._exit(23)
import os
os._exit(0)
"""
        with self.subTest(stage="pre-replace-subprocess"):
            path = self.make_v1("stage-pre-replace.sqlite3")
            completed = subprocess.run(
                (sys.executable, "-c", pre_replace_script, str(path)),
                cwd=REPO_ROOT,
                check=False,
            )
            self.assertEqual(completed.returncode, 23)
            self.assertEqual(self.classify(path), "v1_migration_required")
            BrokerJournal.migrate_v1_to_v2(path)
            self.assertEqual(self.classify(path), "v2_serves")

        # Honest bound (stated verbatim in the falsification record): a test
        # cannot make the kernel lose an un-fsynced directory entry on
        # demand. This cut proves the code path ORDERS correctly and that
        # both possible post-crash directory states recover
        # deterministically; it does not simulate the power loss itself.
        post_replace_script = """
import os
import sys
from pathlib import Path
import tools.host_control.journal as journal_module


def replace_then_die(source, target):
    os.replace(source, target)
    raise journal_module.HostControlError(
        "injected: died post-replace pre-dirsync"
    )


journal_module._atomic_cutover = replace_then_die
try:
    journal_module.BrokerJournal.migrate_v1_to_v2(Path(sys.argv[1]))
except journal_module.HostControlError:
    os._exit(24)
os._exit(0)
"""
        with self.subTest(stage="post-replace-pre-dirsync-subprocess"):
            path = self.make_v1("stage-post-replace.sqlite3")
            completed = subprocess.run(
                (sys.executable, "-c", post_replace_script, str(path)),
                cwd=REPO_ROOT,
                check=False,
            )
            self.assertEqual(completed.returncode, 24)
            state = self.classify(path)
            self.assertIn(
                state,
                ("v1_migration_required", "v2_serves"),
            )
            BrokerJournal.migrate_v1_to_v2(path)
            self.assertEqual(self.classify(path), "v2_serves")

    def test_migration_rollback_restores_backup_on_post_cutover_failure(self) -> None:
        path = self.make_v1("rollback-arm.sqlite3")
        with mock.patch.object(
            BrokerJournal,
            "open",
            side_effect=HostControlError("injected: post-cutover validation"),
        ):
            with self.assertRaises(HostControlError) as caught:
                BrokerJournal.migrate_v1_to_v2(path)
        self.assertIn("restored from backup", str(caught.exception))
        # the restored journal VALIDATES as v1 (drift-refusal path yields
        # MigrationRequired, not a corruption error) ...
        self.assertEqual(self.classify(path), "v1_migration_required")
        with sqlite3.connect(path) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                1,
            )
        # ... the backup artifact is retained ...
        self.assertTrue(Path(f"{path}.v1-backup").exists())
        # ... and the restored journal actually SERVES after a real
        # migration (rollback arm produces a working journal, not merely a
        # validating one)
        BrokerJournal.migrate_v1_to_v2(path)
        self.assertEqual(self.classify(path), "v2_serves")

    def test_migration_refuses_corrupt_or_foreign_source(self) -> None:
        def assert_untouched_and_refused(path: Path) -> None:
            before = path.read_bytes()
            with self.assertRaises(HostControlError):
                BrokerJournal.migrate_v1_to_v2(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(Path(f"{path}.v1-backup").exists())
            self.assertFalse(Path(f"{path}.migrate-tmp").exists())

        legacy = self.root / "legacy-v0.sqlite3"
        connection = sqlite3.connect(legacy)
        try:
            connection.execute("CREATE TABLE legacy(value TEXT)")
            connection.commit()
        finally:
            connection.close()
        assert_untouched_and_refused(legacy)

        garbage = self.root / "garbage.sqlite3"
        garbage.write_bytes(b"this is not a sqlite database at all")
        assert_untouched_and_refused(garbage)

        drifted = self.make_v1("drifted-v1.sqlite3")
        with sqlite3.connect(drifted) as inner:
            inner.executescript("CREATE TABLE extra(value TEXT) STRICT;")
            inner.commit()
        assert_untouched_and_refused(drifted)

        wrong_version = self.make_v1("wrong-version.sqlite3")
        with sqlite3.connect(wrong_version) as inner:
            inner.execute("PRAGMA user_version=5")
            inner.commit()
        assert_untouched_and_refused(wrong_version)

        with self.assertRaises(HostControlError):
            BrokerJournal.migrate_v1_to_v2(self.root / "missing.sqlite3")

        # a valid v2 source is an idempotent NO-OP, not a refusal: nothing
        # is written and it keeps serving
        BrokerJournal.migrate_v1_to_v2(self.journal_path)
        self.assertFalse(Path(f"{self.journal_path}.v1-backup").exists())
        self.assertFalse(Path(f"{self.journal_path}.migrate-tmp").exists())
        record = self.prepare("t24-still-serving")
        self.assertEqual(self.db_state(record.operation_handle), "prepared")


if __name__ == "__main__":
    unittest.main()
