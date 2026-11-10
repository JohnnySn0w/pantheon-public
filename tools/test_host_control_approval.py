"""T-2910 tests: approval journal, review/grant state machines, ValidateAndBind.

Process-boundary labeling (AB-1 convention): tests that prove DURABILITY or
SERIALIZATION use a real second process (subprocess commits, reads, or races
across an exit); same-process tests prove logic correctness or
commit-visibility ordering only and say so.

Separation-claim labeling (L4): the tests below prove the SOURCE-DECLARATION,
CAPABILITY, and DATA layers of the broker/approval separation. Python
``sys.modules`` reachability through the parent package ``__init__`` is
conceded, not claimed. The DEPLOYMENT layer (separate Unix uids, root-owned
units, file ownership/modes) is NOT proven by this unit — it is owned by
T-2913 + T-2920; no test below implies otherwise.

Gate honesty labels (ORG-052 acceptance gate 16), kept in shipped docstrings
per PO mandate:
- the single-use ``nonce`` is server-side and is NOT a ValidateAndBind
  argument; its denial case is a bind attempt against an already-consumed
  grant (the same surface as single-use BOUND / gate 16's "operation"
  dimension);
- the ``policy`` dimension is a stored-revision vs live-revision comparison,
  NOT policy evaluation (approver-policy evaluation lives in
  ``tools.host_control.approval.policy`` and is exercised by
  ``tools/test_host_control_approval_policy.py``);
- the ``handoff`` dimension compares the stored link tuple against a live
  re-fetch of the request plan; ``handoff_kind`` is the discriminator
  selecting that comparison and has no live counterpart of its own.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import hashlib
import inspect
import itertools
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import typing
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import tools.host_control.approval.journal as approval_journal_module
import tools.host_control.journal as broker_journal_module
from tools.host_control.approval.contracts import (
    APPROVAL_REASON_CODES,
    BIND_DENIAL_DIMENSIONS,
    BIND_DENIAL_REASON_CODES,
    BIND_DIMENSION_EXCLUSIONS,
    BIND_SUCCESS_REASON_CODES,
    DECIDE_REASON_CODES,
    GRANT_SCHEMA,
    GRANT_STATES,
    GRANT_TRANSITIONS,
    REVIEW_SCHEMA,
    REVIEW_STATES,
    REVIEW_TRANSITIONS,
    ApprovalCounts,
    ApprovalStatus,
    ApproverDenied,
    DecisionResult,
    ExpirySweepStatus,
    GrantRecord,
    MetadataUnavailable,
    PlanMetadata,
    PolicyVerdict,
    ReviewRecord,
    ReviewResult,
    StaleAuthorityState,
)
from tools.host_control.approval.journal import (
    APPLICATION_ID,
    DDL,
    SCHEMA_DIGEST,
    USER_VERSION,
    ApprovalJournal,
)
from tools.host_control.approval.service import (
    DENY_ALL_AUTHORIZER,
    ApprovalService,
    ConsumerBinder,
    ReviewExtras,
    default_handle_factory,
    transition_grant_record,
    transition_review_record,
)
from tools.host_control.approval.human import (
    HumanDecisionResult,
    HumanRequestResult,
)
from tools.host_control.contracts import (
    BindOutcome,
    HostControlError,
    IdempotencyConflict,
    InvalidTransition,
    JournalBusy,
    UnknownHandle,
    canonical_json,
)

# The test module (NOT the approval tree) may import broker modules: it is
# the surface where the separation claims get exercised from both sides.
from tools.host_control.journal import (
    APPLICATION_ID as BROKER_APPLICATION_ID,
    BrokerJournal,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXED_NOW = datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)
LATE_NOW = FIXED_NOW + timedelta(hours=3)
CONSUMER_UID = 2001

APPROVAL_PACKAGE_DIR = REPO_ROOT / "tools" / "host_control" / "approval"
BROKER_MODULE_FILES = (
    REPO_ROOT / "tools" / "host_control" / "__init__.py",
    REPO_ROOT / "tools" / "host_control" / "broker_config.py",  # T-2920 U2
    REPO_ROOT / "tools" / "host_control" / "contracts.py",
    REPO_ROOT / "tools" / "host_control" / "daemon_config.py",  # T-2920 U2 (shared)
    REPO_ROOT / "tools" / "host_control" / "health.py",
    REPO_ROOT / "tools" / "host_control" / "health_dbus.py",
    REPO_ROOT / "tools" / "host_control" / "journal.py",
    REPO_ROOT / "tools" / "host_control" / "operations.py",
    REPO_ROOT / "tools" / "host_control" / "planner.py",
    REPO_ROOT / "tools" / "host_control" / "policy.py",
    REPO_ROOT / "tools" / "host_control" / "registry.py",
    REPO_ROOT / "tools" / "host_control" / "trusted_files.py",
)
# trusted_files was reclassified as SHARED SUBSTRATE (T-2911, M4): it is a
# pure root-owned-file validator importing only RegistryError — no journal,
# plan, operation, or receipt surface — so the approval tree reuses it for
# approver-policy file trust instead of drifting a ~300-line duplicate.
BROKER_ONLY_MODULES = frozenset(
    (
        "tools.host_control.__main__",  # T-2920 U2 broker entrypoint
        "tools.host_control.approval_client",
        "tools.host_control.broker_config",  # T-2920 U2
        "tools.host_control.driver",
        "tools.host_control.envelope",
        "tools.host_control.execution_source",
        "tools.host_control.executor",
        "tools.host_control.executor.__main__",
        "tools.host_control.executor.config",
        "tools.host_control.executor.dbus_service",
        "tools.host_control.executor.engine",
        "tools.host_control.executor.identity",
        "tools.host_control.executor.mailbox_probe",  # T-2917 5d.3
        "tools.host_control.executor.seat_files",
        "tools.host_control.executor.user_bus",  # T-2917 5c.2 (review L5)
        "tools.host_control.executor.user_manager",
        "tools.host_control.executor.wire",
        "tools.host_control.executor_client",
        "tools.host_control.journal",
        "tools.host_control.operations",
        "tools.host_control.planner",
        "tools.host_control.policy",
        "tools.host_control.ratelimit",
        "tools.host_control.reconciler",  # T-2917 5d.2
        "tools.host_control.registry",
    )
)
# Names importable FROM THE PARENT PACKAGE by approval modules. Everything
# else via `from tools.host_control import X` (or `import tools.host_control`)
# is flagged: the parent __init__ re-exports broker symbols, so the
# module-set check alone has a re-export bypass (AB-2 review gap 1).
SHARED_SUBSTRATE_NAMES = frozenset(("contracts", "trusted_files", "daemon_config"))

APPROVAL_TABLES = [
    ("grant_events", 1),
    ("grants", 1),
    ("human_requests", 1),
    ("journal_meta", 1),
    ("provider_attestations", 1),
    ("review_events", 1),
    ("reviews", 1),
]


def _module_import_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.add(f"<relative:{node.module or ''}>")
            elif node.module:
                names.add(node.module)
    return names


def _parent_package_violations(source: str) -> set[str]:
    """Flag parent-package imports that evade the module-set check.

    `from tools.host_control import BrokerJournal` never names a forbidden
    MODULE, but the parent __init__ re-exports broker symbols — so any
    name pulled from the bare parent package outside the explicit
    shared-substrate allowlist is a violation, as is importing the parent
    package itself (attribute access would evade name checks entirely).
    """

    tree = ast.parse(source)
    violations: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tools.host_control":
                    violations.add("tools.host_control")
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module == "tools.host_control":
                for alias in node.names:
                    if alias.name not in SHARED_SUBSTRATE_NAMES:
                        violations.add(f"tools.host_control.{alias.name}")
            elif node.level == 0 and node.module == "tools":
                # grandparent spelling: `from tools import host_control`
                # binds the same package object (AB-3 review gap 2,
                # tightened in T-2912)
                for alias in node.names:
                    if alias.name == "host_control":
                        violations.add("tools.host_control")
    return violations


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


class FakeMetadataSource:
    """Programmable PlanMetadata seam; records the exact calls made.

    ``events`` is the ORDERED spy log of (method, plan_handle) — the
    gate-12 metadata-first proof asserts call COUNT and ORDER against it,
    not merely withheld return values. ``calls`` keeps the original
    plan_metadata-only shape existing tests assert against.
    """

    def __init__(self) -> None:
        self.plans: dict[str, PlanMetadata] = {}
        self.summaries: dict[str, str] = {}
        self.error: Exception | None = None
        self.calls: list[str] = []
        self.events: list[tuple[str, str]] = []

    def plan_metadata(self, plan_handle: str) -> PlanMetadata:
        self.calls.append(plan_handle)
        self.events.append(("plan_metadata", plan_handle))
        if self.error is not None:
            raise self.error
        if plan_handle not in self.plans:
            raise UnknownHandle("unknown plan handle")
        return self.plans[plan_handle]

    def plan_summary(
        self, plan_handle: str, expected_digest: str
    ) -> tuple[str, str]:
        self.events.append(("plan_summary", plan_handle))
        if self.error is not None:
            raise self.error
        if plan_handle not in self.plans:
            raise UnknownHandle("unknown plan handle")
        metadata = self.plans[plan_handle]
        summary = self.summaries.get(
            plan_handle, f"redacted-effects:{plan_handle[:8]}"
        )
        return metadata.plan_digest, summary


class PermissiveAuthorizer:
    """Test-only permissive policy: allows everything (closed verdicts)."""

    def may_review(
        self, approver_subject: str, metadata: PlanMetadata
    ) -> PolicyVerdict:
        return PolicyVerdict(True, "authorized")

    def may_handoff(
        self,
        approver_subject: str,
        metadata: PlanMetadata,
        request_metadata: PlanMetadata,
        chain_participants: frozenset[str],
    ) -> PolicyVerdict:
        return PolicyVerdict(True, "authorized")

    def may_decide(
        self,
        approver_subject: str,
        review: ReviewRecord,
        chain_participants: frozenset[str],
    ) -> PolicyVerdict:
        return PolicyVerdict(True, "authorized")

    def may_revoke(self, subject: str, grant: GrantRecord) -> PolicyVerdict:
        return PolicyVerdict(True, "authorized")


class ApprovalCaseBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.journal_path = self.root / "approval.sqlite3"
        self.journal = ApprovalJournal.open(self.journal_path)
        self.handle_counter = itertools.count(1)
        self.metadata_source = FakeMetadataSource()
        self.plan_handle = self.next_handle()
        self.plan_digest = self.next_handle()
        self.effect_digest = self.next_handle()
        self.metadata = PlanMetadata(
            owner="consultant",
            performer="consultant",
            target_seat_id="recovery",
            operation_families=("persona.install",),
            plan_digest=self.plan_digest,
            effect_digest=self.effect_digest,
            manifest_revision="manifest-r1",
            policy_revision="policy-r1",
        )
        self.metadata_source.plans[self.plan_handle] = self.metadata
        self.service = self.build_service()

    def tearDown(self) -> None:
        self.journal.close()
        self.temp.cleanup()

    # ------------------------------------------------------------------
    # fixture helpers
    # ------------------------------------------------------------------

    def next_handle(self) -> str:
        return f"{next(self.handle_counter):064x}"

    def build_service(
        self,
        *,
        journal: ApprovalJournal | None = None,
        metadata_source: FakeMetadataSource | None = None,
        clock=None,
        authorizer=None,
        handle_factory=None,
        consumer_uids: frozenset[int] = frozenset((CONSUMER_UID,)),
    ) -> ApprovalService:
        return ApprovalService(
            journal=journal or self.journal,
            metadata_source=metadata_source or self.metadata_source,
            clock=clock or (lambda: FIXED_NOW),
            authorizer=(
                PermissiveAuthorizer() if authorizer is None else authorizer
            ),
            handle_factory=handle_factory or self.next_handle,
            registered_consumer_uids=consumer_uids,
            review_ttl=timedelta(hours=1),
            grant_ttl=timedelta(hours=1),
        )

    def fresh_plan(self) -> str:
        """A brand-new plan handle with its own metadata entry."""

        plan_handle = self.next_handle()
        self.metadata_source.plans[plan_handle] = dataclasses.replace(
            self.metadata,
            plan_digest=self.next_handle(),
            effect_digest=self.next_handle(),
        )
        return plan_handle

    def handoff_request_plan(
        self,
        plan_handle: str,
        owner: str = "mason",
    ) -> str:
        """A request plan ALIGNED with plan_handle's full handoff tuple.

        Same target, manifest revision, operation families, and effect
        digest (the gate-23 full-tuple match); different owner (the
        requester must differ from the performer plan's owner/performer)
        and its own plan handle/digest.
        """

        base = self.metadata_source.plans[plan_handle]
        request_handle = self.next_handle()
        self.metadata_source.plans[request_handle] = dataclasses.replace(
            base,
            owner=owner,
            performer=owner,
            plan_digest=self.next_handle(),
        )
        return request_handle

    def open_review(
        self,
        approver: str = "specialist",
        plan_handle: str | None = None,
        request_plan_handle: str | None = None,
        handoff_anchor: str | None = None,
    ) -> ReviewRecord:
        plan_handle = plan_handle or self.plan_handle
        result = self.service.review(
            approver,
            plan_handle,
            self.metadata_source.plans[plan_handle].plan_digest,
            request_plan_handle,
            handoff_anchor=handoff_anchor,
        )
        return result.record

    def issue_grant(
        self,
        approver: str = "specialist",
        key: str | None = None,
        plan_handle: str | None = None,
        request_plan_handle: str | None = None,
    ) -> tuple[str, str]:
        """Review + grant a plan; returns (plan_handle, approval_handle)."""

        plan_handle = plan_handle or self.fresh_plan()
        review = self.open_review(approver, plan_handle, request_plan_handle)
        result = self.service.decide(
            approver,
            review.review_handle,
            self.metadata_source.plans[plan_handle].plan_digest,
            "granted",
            "approved",
            "anchor-1",
            key or f"decide-{plan_handle[-8:]}",
        )
        self.assertEqual(result.state, "granted")
        self.assertIsNotNone(result.approval_handle)
        return plan_handle, result.approval_handle

    def bind(
        self,
        plan_handle: str,
        approval_handle: str,
        operation_handle: str | None = None,
        *,
        service: ApprovalService | None = None,
        expected_digest: str | None = None,
        bind_plan_handle: str | None = None,
    ) -> BindOutcome:
        service = service or self.service
        return service.validate_and_bind(
            CONSUMER_UID,
            approval_handle,
            bind_plan_handle or plan_handle,
            expected_digest
            or self.metadata_source.plans[plan_handle].plan_digest,
            operation_handle or self.next_handle(),
        )

    def grant_row(self, approval_handle: str) -> dict[str, object]:
        with sqlite3.connect(self.journal_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM grants WHERE approval_handle=?",
                (approval_handle,),
            ).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def review_row(self, review_handle: str) -> dict[str, object]:
        with sqlite3.connect(self.journal_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM reviews WHERE review_handle=?",
                (review_handle,),
            ).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def grant_events(self, approval_handle: str) -> list[tuple]:
        with sqlite3.connect(self.journal_path) as connection:
            return connection.execute(
                "SELECT seq, from_state, to_state, event_kind "
                "FROM grant_events WHERE approval_handle=? ORDER BY seq",
                (approval_handle,),
            ).fetchall()

    def make_review_record(
        self,
        state: str = "open",
        **overrides: object,
    ) -> ReviewRecord:
        decided = state in ("granted", "denied")
        values: dict[str, object] = {
            "schema": REVIEW_SCHEMA,
            "review_handle": self.next_handle(),
            "plan_handle": self.plan_handle,
            "plan_digest": self.plan_digest,
            "plan_owner": "consultant",
            "performer": "consultant",
            "target_seat_id": "recovery",
            "operation_families": ("persona.install",),
            "effect_digest": self.effect_digest,
            "manifest_revision": "manifest-r1",
            "policy_revision": "policy-r1",
            "handoff_kind": "direct_no_handoff",
            "handoff": None,
            "requested_by": "specialist",
            "state": state,
            "decision": state if decided else None,
            "reason_code": (
                ("approved" if state == "granted" else "rejected")
                if decided
                else None
            ),
            "approver_subject": "specialist" if decided else None,
            "authority_source": "agent" if decided else None,
            "audit_anchor": "anchor-1" if decided else None,
            "source_event": None,
            "decide_idempotency_key": "decide-x" if decided else None,
            "created_at": FIXED_NOW.isoformat(),
            "expires_at": (FIXED_NOW + timedelta(hours=1)).isoformat(),
            "decided_at": FIXED_NOW.isoformat() if decided else None,
        }
        values.update(overrides)
        return ReviewRecord.create(**values)

    def make_grant_record(
        self,
        state: str = "issued",
        **overrides: object,
    ) -> GrantRecord:
        bound = state == "bound"
        values: dict[str, object] = {
            "schema": GRANT_SCHEMA,
            "approval_handle": self.next_handle(),
            "review_handle": self.next_handle(),
            "plan_handle": self.plan_handle,
            "plan_digest": self.plan_digest,
            "plan_owner": "consultant",
            "performer": "consultant",
            "target_seat_id": "recovery",
            "operation_families": ("persona.install",),
            "effect_digest": self.effect_digest,
            "manifest_revision": "manifest-r1",
            "policy_revision": "policy-r1",
            "handoff_kind": "direct_no_handoff",
            "handoff": None,
            "approver_subject": "specialist",
            "authority_source": "agent",
            "audit_anchor": "anchor-1",
            "source_event": None,
            "decision": "granted",
            "reason_code": "approved",
            "issued_at": FIXED_NOW.isoformat(),
            "expires_at": (FIXED_NOW + timedelta(hours=1)).isoformat(),
            "nonce": self.next_handle(),
            "idempotency_key": "decide-x",
            "state": state,
            "consuming_operation_handle": self.next_handle() if bound else None,
            "authority_record_id": self.next_handle() if bound else None,
            "updated_at": FIXED_NOW.isoformat(),
        }
        values.update(overrides)
        return GrantRecord.create(**values)


class SeparationTests(ApprovalCaseBase):
    def test_source_level_approval_modules_declare_no_broker_dependency(self) -> None:
        # SOURCE-DECLARATION level only: ast-parse of the approval package's
        # own module files; sys.modules reachability via the parent package
        # __init__ is conceded, not claimed (L4a).
        module_files = sorted(APPROVAL_PACKAGE_DIR.glob("*.py"))
        self.assertEqual(
            [path.name for path in module_files],
            [
                "__init__.py",
                "__main__.py",  # T-2920 U2 daemon entrypoint
                "broker_client.py",
                "config.py",  # T-2920 U2 root config schema
                "contracts.py",
                "dbus_service.py",
                "human.py",
                "identity.py",
                "journal.py",
                "policy.py",
                "presentation.py",
                "service.py",
                "telegram_presenter.py",
                "wire.py",
            ],
        )
        for path in module_files:
            with self.subTest(module=path.name):
                source = path.read_text(encoding="utf-8")
                imports = _module_import_names(path)
                self.assertEqual(imports & BROKER_ONLY_MODULES, set())
                self.assertEqual(_parent_package_violations(source), set())
                self.assertFalse(
                    any(name.startswith("<relative:") for name in imports),
                    f"{path.name} uses relative imports",
                )

    def test_parent_package_reexport_import_is_flagged(self) -> None:
        # AB-2 review gap 1: `from tools.host_control import BrokerJournal`
        # names no forbidden MODULE (the parent __init__ re-exports it), so
        # the module-set check alone has a bypass. The checker below is the
        # SAME one the real-files test above applies — injecting this exact
        # import into any approval module fails that test.
        self.assertEqual(
            _parent_package_violations(
                "from tools.host_control import BrokerJournal\n"
            ),
            {"tools.host_control.BrokerJournal"},
        )
        self.assertEqual(
            _parent_package_violations("import tools.host_control\n"),
            {"tools.host_control"},
        )
        self.assertEqual(
            _parent_package_violations(
                "import tools.host_control as broker\n"
            ),
            {"tools.host_control"},
        )
        # AB-3 review gap 2 (tightened in T-2912): the GRANDPARENT
        # spelling binds the same package object and evaded both checks.
        self.assertEqual(
            _parent_package_violations("from tools import host_control\n"),
            {"tools.host_control"},
        )
        self.assertEqual(
            _parent_package_violations(
                "from tools import host_control as broker\n"
            ),
            {"tools.host_control"},
        )
        # sibling names from the grandparent stay unflagged
        self.assertEqual(
            _parent_package_violations("from tools import ticket\n"),
            set(),
        )
        # shared substrate stays importable, in both spellings
        self.assertEqual(
            _parent_package_violations(
                "from tools.host_control import contracts\n"
                "from tools.host_control import trusted_files\n"
                "from tools.host_control.contracts import sha256_json\n"
                "from tools.host_control.trusted_files import (\n"
                "    RootOwnedFileLoader,\n"
                ")\n"
            ),
            set(),
        )

    def test_source_level_broker_modules_declare_no_approval_dependency(self) -> None:
        for path in BROKER_MODULE_FILES:
            with self.subTest(module=path.name):
                imports = _module_import_names(path)
                self.assertFalse(
                    any(
                        name.startswith("tools.host_control.approval")
                        for name in imports
                    ),
                    f"{path.name} imports the approval tree",
                )

    def test_capability_level_no_signature_hands_either_service_the_foreign_journal(self) -> None:
        # CAPABILITY level (L4b): no constructor parameter or public method
        # signature hands the approval service a broker journal (or the
        # broker service an approval journal).
        from tools.host_control.operations import OperationService

        approval_init = inspect.signature(ApprovalService.__init__)
        for name, parameter in approval_init.parameters.items():
            self.assertNotIn("broker", name)
            self.assertNotIn("BrokerJournal", str(parameter.annotation))
        for name, method in inspect.getmembers(
            ApprovalService, predicate=inspect.isfunction
        ):
            if name.startswith("_"):
                continue
            self.assertNotIn(
                "BrokerJournal",
                str(inspect.signature(method)),
                f"ApprovalService.{name} names the broker journal",
            )
        broker_init = inspect.signature(OperationService.__init__)
        for name, parameter in broker_init.parameters.items():
            self.assertNotIn("approval_journal", name)
            self.assertNotIn("ApprovalJournal", str(parameter.annotation))

    def test_data_level_each_journal_loudly_refuses_the_others_database_file(self) -> None:
        # DATA level (L4c): distinct application_id markers; each open()
        # refuses the other's file even if a code path reached across.
        self.assertNotEqual(APPLICATION_ID, BROKER_APPLICATION_ID)
        broker_path = self.root / "broker.sqlite3"
        broker = BrokerJournal.open(broker_path)
        broker.close()
        with self.assertRaises(HostControlError):
            ApprovalJournal.open(broker_path)
        with self.assertRaises(HostControlError):
            BrokerJournal.open(self.journal_path)


class JournalSchemaTests(ApprovalCaseBase):
    def test_journal_v1_schema_locked_and_refuses_drift(self) -> None:
        self.assertEqual(
            hashlib.sha256(DDL.encode("utf-8")).hexdigest(),
            SCHEMA_DIGEST,
        )
        self.assertEqual(USER_VERSION, 1)
        with sqlite3.connect(self.journal_path) as connection:
            tables = connection.execute(
                "SELECT name, count(*) FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "GROUP BY name ORDER BY name"
            ).fetchall()
            self.assertEqual(tables, APPROVAL_TABLES)
            self.assertEqual(
                connection.execute("PRAGMA application_id").fetchone()[0],
                APPLICATION_ID,
            )
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                USER_VERSION,
            )

        def tampered_copy(mutate: str) -> Path:
            copy_path = self.root / f"tampered-{hashlib.sha256(mutate.encode()).hexdigest()[:8]}.sqlite3"
            shutil.copyfile(self.journal_path, copy_path)
            with sqlite3.connect(copy_path) as connection:
                connection.executescript(mutate)
            return copy_path

        tampers = {
            "application_id": "PRAGMA application_id=1;",
            "user_version": "PRAGMA user_version=9;",
            "meta_digest": (
                "UPDATE journal_meta SET schema_digest='"
                + "b" * 64
                + "';"
            ),
            "dropped_index": "DROP INDEX reviews_open_request_idx;",
            "extra_table": "CREATE TABLE sneaky (x INTEGER) STRICT;",
        }
        for label, mutate in tampers.items():
            with self.subTest(tamper=label):
                with self.assertRaises(HostControlError):
                    ApprovalJournal.open(tampered_copy(mutate))

    def test_provider_attestations_table_shape_and_checks(self) -> None:
        # T-2919 widened the table (journal v1 amended in place — valid only
        # because no v1 approval journal was ever installed): the
        # attestation binds ONE human request and ONE review, its decision,
        # the registered human subject, and the ingress idempotency key.
        # The only writer is consume_human_nonce_and_decide (service
        # human_decision); these are the raw SQL-level CHECK/UNIQUE/FK
        # guarantees under it.
        review = self.open_review()
        request_handle = self.next_handle()
        with sqlite3.connect(self.journal_path) as connection:
            connection.execute(
                "INSERT INTO human_requests (request_handle, review_handle, "
                "requested_by, request_idempotency_key, plan_digest, "
                "target_seat_id, operation_families, redacted_summary, nonce, "
                "state, provider_message_ref, created_at, expires_at, "
                "presented_at, updated_at) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    request_handle,
                    review.review_handle,
                    "specialist",
                    "rh-1",
                    review.plan_digest,
                    review.target_seat_id,
                    canonical_json(list(review.operation_families)),
                    "summary",
                    self.next_handle(),
                    "presented",
                    "42",
                    FIXED_NOW.isoformat(),
                    FIXED_NOW.isoformat(),
                    FIXED_NOW.isoformat(),
                    FIXED_NOW.isoformat(),
                ),
            )
            connection.commit()
        base = {
            "attestation_handle": self.next_handle(),
            "review_handle": review.review_handle,
            "request_handle": request_handle,
            "decision": "granted",
            "human_subject": "operator",
            "idempotency_key": "tg-key-1",
            "provider": "telegram",
            "provider_user_id": "12345",
            "provider_chat_id": "67890",
            "provider_message_id": "42",
            "callback_nonce": self.next_handle(),
            "source_event": "telegram:callback:42",
            "attestation_json": canonical_json({"v": 1}),
            "created_at": FIXED_NOW.isoformat(),
        }
        columns = ",".join(base)
        placeholders = ",".join("?" for _ in base)
        insert = (
            f"INSERT INTO provider_attestations ({columns}) "
            f"VALUES ({placeholders})"
        )
        with sqlite3.connect(self.journal_path) as connection:
            # FK enforcement is per-connection in SQLite; the journal's own
            # connection enables it, and this direct one must too for the
            # REFERENCES check below to be exercised
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(insert, tuple(base.values()))
            connection.commit()
            bad_provider = dict(base)
            bad_provider["attestation_handle"] = self.next_handle()
            bad_provider["callback_nonce"] = self.next_handle()
            bad_provider["provider"] = "email"
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(insert, tuple(bad_provider.values()))
            replayed_nonce = dict(base)
            replayed_nonce["attestation_handle"] = self.next_handle()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(insert, tuple(replayed_nonce.values()))
            bad_nonce = dict(base)
            bad_nonce["attestation_handle"] = self.next_handle()
            bad_nonce["callback_nonce"] = "not-hex"
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(insert, tuple(bad_nonce.values()))
            unknown_review = dict(base)
            unknown_review["attestation_handle"] = self.next_handle()
            unknown_review["callback_nonce"] = self.next_handle()
            unknown_review["review_handle"] = self.next_handle()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(insert, tuple(unknown_review.values()))
            connection.rollback()


class ReviewLifecycleTests(ApprovalCaseBase):
    def test_review_replay_returns_open_review_and_new_review_allowed_after_terminal(self) -> None:
        first = self.open_review()
        replay_result = self.service.review(
            "specialist",
            self.plan_handle,
            self.plan_digest,
            None,
        )
        self.assertTrue(replay_result.replayed)
        self.assertEqual(
            first.review_handle, replay_result.record.review_handle
        )
        # a different requester's identical request is a DIFFERENT open review
        other = self.open_review(approver="mason")
        self.assertNotEqual(other.review_handle, first.review_handle)
        # deny the first; the ADR requires a NEW review for any later attempt
        denied = self.service.decide(
            "specialist",
            first.review_handle,
            self.plan_digest,
            "denied",
            "rejected",
            "anchor-deny",
            "decide-deny-1",
        )
        self.assertEqual(denied.state, "denied")
        self.assertIsNone(denied.approval_handle)
        fresh_result = self.service.review(
            "specialist",
            self.plan_handle,
            self.plan_digest,
            None,
        )
        self.assertFalse(fresh_result.replayed)
        self.assertNotEqual(
            fresh_result.record.review_handle, first.review_handle
        )
        self.assertEqual(fresh_result.record.state, "open")
        # wrong expected digest never opens a review
        with self.assertRaises(HostControlError):
            self.service.review(
                "specialist",
                self.plan_handle,
                self.next_handle(),
                None,
            )

    def test_review_stores_server_derived_request_plan_linkage(self) -> None:
        # T-2911: the link is server-derived AND validated (full-tuple
        # match; enforcement tests live in the policy test module). The
        # bounded handoff_anchor now has its in-scope caller-supplied
        # source and rides the link as audit context, never authority.
        request_plan = self.handoff_request_plan(self.plan_handle)
        review = self.open_review(
            request_plan_handle=request_plan,
            handoff_anchor="ticket:T-2911",
        )
        self.assertEqual(review.handoff_kind, "request_plan")
        self.assertEqual(review.handoff.request_plan_handle, request_plan)
        self.assertEqual(
            review.handoff.request_plan_digest,
            self.metadata_source.plans[request_plan].plan_digest,
        )
        self.assertEqual(review.handoff.request_plan_owner, "mason")
        self.assertEqual(review.handoff.handoff_anchor, "ticket:T-2911")
        direct = self.open_review(plan_handle=self.fresh_plan())
        self.assertEqual(direct.handoff_kind, "direct_no_handoff")
        self.assertIsNone(direct.handoff)

    def test_decide_idempotency_key_replays_decision_and_refuses_conflicting_reuse(self) -> None:
        # Gate 24, approval half. Composition note (plan DECISIONS): replay
        # of a decide against a now-TERMINAL review resolves via the stored
        # decide_idempotency_key on the review row — the open-review replay
        # index no longer matches once the review left 'open'.
        plan_handle, approval_handle = self.issue_grant(key="decide-same")
        review_handle = self.grant_row(approval_handle)["review_handle"]
        replay = self.service.decide(
            "specialist",
            review_handle,
            self.metadata_source.plans[plan_handle].plan_digest,
            "granted",
            "approved",
            "anchor-1",
            "decide-same",
        )
        self.assertEqual(replay.approval_handle, approval_handle)
        self.assertEqual(replay.state, "granted")
        # same key, conflicting decision → refused
        with self.assertRaises(IdempotencyConflict):
            self.service.decide(
                "specialist",
                review_handle,
                self.metadata_source.plans[plan_handle].plan_digest,
                "denied",
                "rejected",
                "anchor-1",
                "decide-same",
            )
        # same key reused for a DIFFERENT review → refused
        other_review = self.open_review(plan_handle=self.fresh_plan())
        with self.assertRaises(IdempotencyConflict):
            self.service.decide(
                "specialist",
                other_review.review_handle,
                other_review.plan_digest,
                "granted",
                "approved",
                "anchor-1",
                "decide-same",
            )
        # different key on an already-decided review → refused loudly
        with self.assertRaises(HostControlError):
            self.service.decide(
                "specialist",
                review_handle,
                self.metadata_source.plans[plan_handle].plan_digest,
                "granted",
                "approved",
                "anchor-1",
                "decide-other",
            )

    def test_decide_replay_with_differing_audit_anchor_refuses(self) -> None:
        # AB-2 review gap 3 (T-2911, M6): the replay comparison previously
        # checked decision/reason_code/plan_digest only, so a same-key
        # replay carrying a DIFFERENT audit anchor replayed silently. The
        # anchor is context, not authority — but a replay that would have
        # recorded different context is a CONFLICT, not an idempotent
        # repeat.
        plan_handle, approval_handle = self.issue_grant(key="decide-anchor")
        review_handle = self.grant_row(approval_handle)["review_handle"]
        with self.assertRaises(IdempotencyConflict):
            self.service.decide(
                "specialist",
                review_handle,
                self.metadata_source.plans[plan_handle].plan_digest,
                "granted",
                "approved",
                "anchor-DIFFERENT",
                "decide-anchor",
            )
        # the durable record still carries the ORIGINAL anchor
        self.assertEqual(
            self.review_row(review_handle)["audit_anchor"], "anchor-1"
        )

    def test_abandoned_review_does_not_mutate_grant_state(self) -> None:
        # Gate 31 sliver: an opened-then-abandoned review mints nothing.
        review = self.open_review(plan_handle=self.fresh_plan())
        with sqlite3.connect(self.journal_path) as connection:
            grants = connection.execute(
                "SELECT count(*) FROM grants"
            ).fetchone()[0]
        self.assertEqual(grants, 0)
        self.assertEqual(
            self.review_row(review.review_handle)["state"],
            "open",
        )

    def test_approver_supplied_values_can_never_land_in_source_event(self) -> None:
        # PO mandate (a): audit_anchor is bounded audit context, NOT
        # authority; source_event is the provider-authenticated pointer only
        # T-2919 may populate. Conflating them would let an approver-supplied
        # string later read as provider-authenticated provenance.
        self.assertNotIn(
            "source_event",
            inspect.signature(ApprovalService.decide).parameters,
        )
        self.assertNotIn(
            "source_event",
            inspect.signature(ApprovalService.review).parameters,
        )
        plan_handle, approval_handle = self.issue_grant()
        grant_row = self.grant_row(approval_handle)
        self.assertEqual(grant_row["audit_anchor"], "anchor-1")
        self.assertIsNone(grant_row["source_event"])
        review_row = self.review_row(grant_row["review_handle"])
        self.assertIsNone(review_row["source_event"])
        # SQL tier: an agent-sourced row structurally cannot carry one
        with sqlite3.connect(self.journal_path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE grants SET source_event='forged' "
                    "WHERE approval_handle=?",
                    (approval_handle,),
                )
            connection.rollback()


class StateMachineTests(ApprovalCaseBase):
    def test_review_and_grant_state_machines_reject_every_illegal_transition(self) -> None:
        # Gate 25: exhaustive cross-product over BOTH machines (pure layer,
        # same-process, logic correctness).
        review_kwargs = {
            "granted": {
                "decision": "granted",
                "reason_code": "approved",
                "approver_subject": "specialist",
                "authority_source": "agent",
                "audit_anchor": "anchor-1",
                "decide_idempotency_key": "decide-x",
                "decided_at": FIXED_NOW.isoformat(),
            },
            "denied": {
                "decision": "denied",
                "reason_code": "rejected",
                "approver_subject": "specialist",
                "authority_source": "agent",
                "audit_anchor": "anchor-1",
                "decide_idempotency_key": "decide-x",
                "decided_at": FIXED_NOW.isoformat(),
            },
            "expired": {},
        }
        legal = 0
        for from_state in sorted(REVIEW_STATES):
            base = self.make_review_record(from_state)
            for to_state in sorted(REVIEW_STATES):
                expected_legal = to_state in REVIEW_TRANSITIONS[from_state]
                with self.subTest(edge=f"review:{from_state}->{to_state}"):
                    if expected_legal:
                        legal += 1
                        result = transition_review_record(
                            base,
                            to_state,
                            **review_kwargs[to_state],
                        )
                        self.assertEqual(result.state, to_state)
                        self.assertTrue(result.validate_digest())
                    else:
                        with self.assertRaises(InvalidTransition):
                            transition_review_record(
                                base,
                                to_state,
                                **review_kwargs.get(to_state, {}),
                            )
        self.assertEqual(legal, 3)
        legal = 0
        for from_state in sorted(GRANT_STATES):
            base = self.make_grant_record(from_state)
            for to_state in sorted(GRANT_STATES):
                expected_legal = to_state in GRANT_TRANSITIONS[from_state]
                kwargs: dict[str, object] = {
                    "updated_at": LATE_NOW.isoformat()
                }
                if to_state == "bound":
                    kwargs["consuming_operation_handle"] = self.next_handle()
                    kwargs["authority_record_id"] = self.next_handle()
                with self.subTest(edge=f"grant:{from_state}->{to_state}"):
                    if expected_legal:
                        legal += 1
                        result = transition_grant_record(
                            base, to_state, **kwargs
                        )
                        self.assertEqual(result.state, to_state)
                        self.assertTrue(result.validate_digest())
                    else:
                        with self.assertRaises(InvalidTransition):
                            transition_grant_record(base, to_state, **kwargs)
        self.assertEqual(legal, 3)
        # SQL layer: a stale current record cannot move the row
        plan_handle, approval_handle = self.issue_grant()
        stale = self.journal.get_grant(approval_handle)
        outcome = self.bind(plan_handle, approval_handle)
        self.assertEqual(outcome.state, "bound")
        events_before = self.grant_events(approval_handle)
        with self.assertRaises(HostControlError):
            self.journal.transition_grant(
                current=stale,
                new=transition_grant_record(
                    stale,
                    "revoked",
                    updated_at=LATE_NOW.isoformat(),
                ),
                event_kind="stale-attempt",
                payload={},
            )
        self.assertEqual(self.grant_row(approval_handle)["state"], "bound")
        self.assertEqual(self.grant_events(approval_handle), events_before)

    def test_denied_revoked_expired_and_bound_grants_never_become_reusable(self) -> None:
        # Gate 25: per-terminal non-reusability, each with its own reason
        # code, each leaving the terminal state unmoved.
        cases: list[tuple[str, str, str]] = []
        # revoked
        plan_handle, approval_handle = self.issue_grant()
        self.service.revoke("admin", approval_handle, "revoked_by_admin")
        cases.append((plan_handle, approval_handle, "revoked"))
        # expired (durable lazy transition through the late-clock service)
        plan_handle, approval_handle = self.issue_grant()
        late = self.build_service(clock=lambda: LATE_NOW)
        outcome = self.bind(plan_handle, approval_handle, service=late)
        self.assertEqual(outcome.reason_code, "expired")
        cases.append((plan_handle, approval_handle, "expired"))
        # bound (single-use consumed)
        plan_handle, approval_handle = self.issue_grant()
        self.bind(plan_handle, approval_handle)
        cases.append((plan_handle, approval_handle, "already_bound"))
        for plan_handle, approval_handle, expected_code in cases:
            with self.subTest(terminal=expected_code):
                state_before = self.grant_row(approval_handle)["state"]
                retry = self.bind(plan_handle, approval_handle)
                self.assertEqual(retry.state, "denied")
                self.assertEqual(retry.reason_code, expected_code)
                self.assertIsNone(retry.authority_record_id)
                self.assertEqual(
                    self.grant_row(approval_handle)["state"],
                    state_before,
                )
        # a DENIED review mints no grant at all, and a fresh review for the
        # same plan is the (only) later path — gate 25's "denial requires a
        # new review".
        plan_handle = self.fresh_plan()
        review = self.open_review(plan_handle=plan_handle)
        self.service.decide(
            "specialist",
            review.review_handle,
            self.metadata_source.plans[plan_handle].plan_digest,
            "denied",
            "rejected",
            "anchor-x",
            "decide-denied-terminal",
        )
        with sqlite3.connect(self.journal_path) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM grants WHERE review_handle=?",
                    (review.review_handle,),
                ).fetchone()[0],
                0,
            )
        fresh = self.open_review(plan_handle=plan_handle)
        self.assertNotEqual(fresh.review_handle, review.review_handle)

    def test_single_use_bound_enforced_by_sql_constraints_not_python(self) -> None:
        # L6: prefer SQL-level enforcement — prove the constraints fire even
        # against direct SQL that bypasses every Python check.
        plan_a, approval_a = self.issue_grant()
        outcome = self.bind(plan_a, approval_a)
        consumed_op = self.grant_row(approval_a)["consuming_operation_handle"]
        plan_b, approval_b = self.issue_grant()
        with sqlite3.connect(self.journal_path) as connection:
            # a second grant cannot take the same consuming operation handle
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE grants SET state='bound', "
                    "consuming_operation_handle=?, authority_record_id=? "
                    "WHERE approval_handle=?",
                    (consumed_op, "c" * 64, approval_b),
                )
            # an issued grant cannot carry a consuming handle
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE grants SET consuming_operation_handle=? "
                    "WHERE approval_handle=?",
                    ("d" * 64, approval_b),
                )
            # a bound grant cannot have its consumption cleared while bound
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE grants SET consuming_operation_handle=NULL "
                    "WHERE approval_handle=?",
                    (approval_a,),
                )
            # duplicate nonce is structurally impossible
            nonce = self.grant_row(approval_a)["nonce"]
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE grants SET nonce=? WHERE approval_handle=?",
                    (nonce, approval_b),
                )
            connection.rollback()


class BindMatrixTests(ApprovalCaseBase):
    def test_validate_and_bind_known_good_tuple_binds(self) -> None:
        # Gate 16 anchor: the matrix below is non-vacuous because this exact
        # construction demonstrably BINDS.
        plan_handle, approval_handle = self.issue_grant()
        operation_handle = self.next_handle()
        outcome = self.bind(plan_handle, approval_handle, operation_handle)
        self.assertEqual(outcome.state, "bound")
        self.assertEqual(outcome.reason_code, "bound")
        self.assertIsNotNone(outcome.authority_record_id)
        row = self.grant_row(approval_handle)
        self.assertEqual(row["state"], "bound")
        self.assertEqual(row["consuming_operation_handle"], operation_handle)
        self.assertEqual(
            row["authority_record_id"],
            outcome.authority_record_id,
        )
        self.assertEqual(
            [event[1:] for event in self.grant_events(approval_handle)],
            [("none", "issued", "issued"), ("issued", "bound", "bound")],
        )

    def test_bind_denial_matrix_derived_from_grant_fields_one_reason_per_dimension(self) -> None:
        """Gate 16 completeness, enforced structurally.

        The dimension list is DERIVED from dataclasses.fields(GrantRecord):
        every field must appear in exactly one of BIND_DENIAL_DIMENSIONS or
        BIND_DIMENSION_EXCLUSIONS, so adding a grant field later FAILS this
        test rather than silently escaping coverage. Each dimension case
        mutates exactly one element and asserts its own distinct reason code.

        Honesty labels (shipped, not just planned):
        - nonce: server-side, not a ValidateAndBind argument; its denial case
          is a second bind against the already-consumed grant — the same
          surface as single-use BOUND and gate 16's "operation" dimension.
        - policy_revision: stored-revision vs live-revision COMPARISON, not
          policy evaluation (the approver policy module owns evaluation at
          review/decide time).
        - effect_digest / manifest_revision: DERIVED-REDUNDANT given
          plan_digest equality; a mismatch diagnoses broker-side metadata
          inconsistency, not a forging attacker.
        - handoff: stored link tuple vs a LIVE re-fetch of the request
          plan (handle from the GRANT record, not the caller — so an
          unknown request plan RAISES rather than denies; see the
          vanished-request-plan test in the policy module). handoff_kind
          is the discriminator selecting this comparison and stays an
          exclusion. handoff_anchor inside the link is issuance-bound
          context with NO live source — deliberately not a live
          comparison; non-authority proven by verdict-independence.
        """

        field_names = {
            field.name
            for field in dataclasses.fields(GrantRecord)
            if field.name != "digest"
        }
        dimensions = set(BIND_DENIAL_DIMENSIONS)
        exclusions = set(BIND_DIMENSION_EXCLUSIONS) - {"digest"}
        self.assertEqual(dimensions & exclusions, set())
        self.assertEqual(dimensions | exclusions, field_names)
        self.assertTrue(
            set(BIND_DENIAL_DIMENSIONS.values()) <= BIND_DENIAL_REASON_CODES
        )
        for reason in BIND_DIMENSION_EXCLUSIONS.values():
            self.assertTrue(reason)  # every exclusion is documented

        def drift(plan_handle: str, **changes: object) -> None:
            self.metadata_source.plans[plan_handle] = dataclasses.replace(
                self.metadata_source.plans[plan_handle], **changes
            )

        for field_name in sorted(BIND_DENIAL_DIMENSIONS):
            expected_code = BIND_DENIAL_DIMENSIONS[field_name]
            with self.subTest(dimension=field_name, code=expected_code):
                plan_handle, approval_handle = self.issue_grant()
                service = self.service
                kwargs: dict[str, object] = {}
                if field_name == "approval_handle":
                    approval_handle = self.next_handle()  # unknown handle
                elif field_name == "plan_handle":
                    kwargs["bind_plan_handle"] = self.next_handle()
                elif field_name == "plan_digest":
                    kwargs["expected_digest"] = self.next_handle()
                elif field_name == "plan_owner":
                    drift(plan_handle, owner="mallory")
                elif field_name == "performer":
                    drift(plan_handle, performer="mallory")
                elif field_name == "target_seat_id":
                    drift(plan_handle, target_seat_id="other-seat")
                elif field_name == "operation_families":
                    drift(
                        plan_handle,
                        operation_families=("persona.install", "extra"),
                    )
                elif field_name == "effect_digest":
                    drift(plan_handle, effect_digest=self.next_handle())
                elif field_name == "manifest_revision":
                    drift(plan_handle, manifest_revision="manifest-r2")
                elif field_name == "policy_revision":
                    drift(plan_handle, policy_revision="policy-r2")
                elif field_name == "expires_at":
                    service = self.build_service(clock=lambda: LATE_NOW)
                elif field_name == "state":
                    self.service.revoke(
                        "admin", approval_handle, "revoked_by_admin"
                    )
                elif field_name == "nonce":
                    first = self.bind(plan_handle, approval_handle)
                    self.assertEqual(first.state, "bound")
                elif field_name == "handoff":
                    # re-issue as a HANDOFF grant, then drift the live
                    # request plan out from under the stored link
                    plan_handle = self.fresh_plan()
                    request_plan = self.handoff_request_plan(plan_handle)
                    plan_handle, approval_handle = self.issue_grant(
                        plan_handle=plan_handle,
                        request_plan_handle=request_plan,
                    )
                    drift(request_plan, owner="mallory")
                else:  # pragma: no cover - the completeness assert catches it
                    self.fail(f"dimension {field_name} has no scenario")
                outcome = self.bind(
                    plan_handle,
                    approval_handle,
                    service=service,
                    **kwargs,
                )
                self.assertEqual(outcome.state, "denied")
                self.assertIsNone(outcome.authority_record_id)
                self.assertEqual(outcome.reason_code, expected_code)
                # a mismatch denial never consumes an issued grant
                if field_name not in ("expires_at", "state", "nonce"):
                    self.assertEqual(
                        self.grant_row(approval_handle)["state"]
                        if field_name != "approval_handle"
                        else "issued",
                        "issued",
                    )
        # live-metadata digest drift is a second surface of the plan_digest
        # dimension and keeps its own diagnostic code
        plan_handle, approval_handle = self.issue_grant()
        drift(plan_handle, plan_digest=self.next_handle())
        outcome = self.service.validate_and_bind(
            CONSUMER_UID,
            approval_handle,
            plan_handle,
            self.journal.get_grant(approval_handle).plan_digest,
            self.next_handle(),
        )
        self.assertEqual(outcome.state, "denied")
        self.assertEqual(outcome.reason_code, "metadata_digest_mismatch")
        # the handoff dimension has THREE live sub-surfaces (owner drift is
        # the matrix case above); digest and shared-effect drift share the
        # same code — same precedent as metadata_digest_mismatch
        for drift_field in ("plan_digest", "effect_digest"):
            with self.subTest(handoff_drift=drift_field):
                plan_handle = self.fresh_plan()
                request_plan = self.handoff_request_plan(plan_handle)
                plan_handle, approval_handle = self.issue_grant(
                    plan_handle=plan_handle,
                    request_plan_handle=request_plan,
                )
                drift(request_plan, **{drift_field: self.next_handle()})
                outcome = self.bind(plan_handle, approval_handle)
                self.assertEqual(outcome.state, "denied")
                self.assertEqual(outcome.reason_code, "handoff_mismatch")
                self.assertEqual(
                    self.grant_row(approval_handle)["state"], "issued"
                )

    def test_reason_codes_are_a_closed_enum_and_every_service_denial_is_a_member(self) -> None:
        # L12: the closed set is authoritative; the service's single _deny
        # helper runtime-refuses any code outside it (structural, P1).
        self.assertTrue(BIND_DENIAL_REASON_CODES <= APPROVAL_REASON_CODES)
        self.assertTrue(BIND_SUCCESS_REASON_CODES <= APPROVAL_REASON_CODES)
        self.assertTrue(DECIDE_REASON_CODES <= APPROVAL_REASON_CODES)
        outcome = self.service._deny("revoked")
        self.assertEqual(outcome, BindOutcome("denied", None, "revoked"))
        with self.assertRaises(HostControlError):
            self.service._deny("some_new_unregistered_code")

    def test_identical_tuple_bind_replay_returns_original_binding_without_second_write(self) -> None:
        # Gate 26 sliver (authority BOUND with broker PREPARED): the
        # identical-tuple retry returns the ORIGINAL binding; no second
        # write, no new event row.
        plan_handle, approval_handle = self.issue_grant()
        operation_handle = self.next_handle()
        first = self.bind(plan_handle, approval_handle, operation_handle)
        events_before = self.grant_events(approval_handle)
        row_before = self.grant_row(approval_handle)
        replay = self.bind(plan_handle, approval_handle, operation_handle)
        self.assertEqual(replay.state, "bound")
        self.assertEqual(replay.reason_code, "bound_replay")
        self.assertEqual(
            replay.authority_record_id,
            first.authority_record_id,
        )
        self.assertEqual(self.grant_events(approval_handle), events_before)
        self.assertEqual(self.grant_row(approval_handle), row_before)

    def test_identical_tuple_replay_after_grant_expiry_is_denied_without_write(self) -> None:
        # T-5233: the bound_replay honours expires_at (gate 16 refuses on
        # expiry). Denied "expired", zero writes, the grant stays BOUND.
        plan_handle, approval_handle = self.issue_grant()
        operation_handle = self.next_handle()
        first = self.bind(plan_handle, approval_handle, operation_handle)
        self.assertEqual(first.state, "bound")
        events_before = self.grant_events(approval_handle)
        row_before = self.grant_row(approval_handle)
        late = self.build_service(clock=lambda: LATE_NOW)
        replay = self.bind(
            plan_handle, approval_handle, operation_handle, service=late
        )
        self.assertEqual(replay, BindOutcome("denied", None, "expired"))
        self.assertEqual(self.grant_events(approval_handle), events_before)
        self.assertEqual(self.grant_row(approval_handle), row_before)
        self.assertEqual(row_before["state"], "bound")
        # a different operation after expiry is still already_bound-class
        # refused (never a fresh bind)
        other = self.bind(
            plan_handle, approval_handle, self.next_handle(), service=late
        )
        self.assertEqual(other.state, "denied")

    def test_identical_tuple_replay_just_before_expiry_returns_original(self) -> None:
        plan_handle, approval_handle = self.issue_grant()
        operation_handle = self.next_handle()
        first = self.bind(plan_handle, approval_handle, operation_handle)
        expires = datetime.fromisoformat(
            self.grant_row(approval_handle)["expires_at"]
        )
        edge = self.build_service(
            clock=lambda: expires - timedelta(seconds=1)
        )
        replay = self.bind(
            plan_handle, approval_handle, operation_handle, service=edge
        )
        self.assertEqual(
            (replay.state, replay.reason_code, replay.authority_record_id),
            ("bound", "bound_replay", first.authority_record_id),
        )
        at_expiry = self.build_service(clock=lambda: expires)
        self.assertEqual(
            self.bind(
                plan_handle,
                approval_handle,
                operation_handle,
                service=at_expiry,
            ).reason_code,
            "expired",
        )

    def test_a_different_operation_can_never_acquire_a_bound_grant(self) -> None:
        # Gate 27's third clause.
        plan_handle, approval_handle = self.issue_grant()
        operation_handle = self.next_handle()
        self.bind(plan_handle, approval_handle, operation_handle)
        thief = self.bind(plan_handle, approval_handle, self.next_handle())
        self.assertEqual(thief.state, "denied")
        self.assertEqual(thief.reason_code, "already_bound")
        self.assertEqual(
            self.grant_row(approval_handle)["consuming_operation_handle"],
            operation_handle,
        )

    def test_a_second_grant_can_never_bind_an_already_consumed_operation(self) -> None:
        # PO mandate (c): the reverse direction — one operation can never be
        # authorized by two grants (of possibly different scope). Structural
        # force is the UNIQUE(consuming_operation_handle) constraint.
        operation_handle = self.next_handle()
        plan_a, approval_a = self.issue_grant()
        first = self.bind(plan_a, approval_a, operation_handle)
        self.assertEqual(first.state, "bound")
        plan_b, approval_b = self.issue_grant()
        second = self.bind(plan_b, approval_b, operation_handle)
        self.assertEqual(second.state, "denied")
        self.assertEqual(second.reason_code, "operation_consumed")
        self.assertEqual(self.grant_row(approval_b)["state"], "issued")
        self.assertEqual(
            self.grant_row(approval_a)["consuming_operation_handle"],
            operation_handle,
        )

    def test_metadata_source_outage_fails_closed_and_grant_stays_issued(self) -> None:
        # Reverse-seam outage: refuse WITHOUT writing; the grant stays
        # ISSUED and later binds. (The broker-side approval outage half of
        # gate 28 landed in AB-1.)
        plan_handle, approval_handle = self.issue_grant()
        self.metadata_source.error = MetadataUnavailable("broker seam down")
        with self.assertRaises(MetadataUnavailable):
            self.bind(plan_handle, approval_handle)
        self.assertEqual(self.grant_row(approval_handle)["state"], "issued")
        self.metadata_source.error = None
        recovered = self.bind(plan_handle, approval_handle)
        self.assertEqual(recovered.state, "bound")

    def test_unregistered_consumer_uid_cannot_bind_or_read_status(self) -> None:
        # ORG-052 §5: each service rechecks the live bus-derived uid.
        # Default-empty allowlist = deny (composition with the deny-all
        # authorizer: a default-constructed service is inert end-to-end).
        plan_handle, approval_handle = self.issue_grant()
        with self.assertRaises(HostControlError):
            self.service.validate_and_bind(
                9999,
                approval_handle,
                plan_handle,
                self.metadata_source.plans[plan_handle].plan_digest,
                self.next_handle(),
            )
        with self.assertRaises(HostControlError):
            self.service.approval_status(9999, approval_handle)
        self.assertEqual(self.grant_row(approval_handle)["state"], "issued")


class SerializationTests(ApprovalCaseBase):
    def _race_script(self, action: str) -> str:
        # argv: journal_path approval_handle plan_handle plan_digest
        #       effect_digest operation_handle
        return (
            """
import sys
from datetime import datetime, timezone
from pathlib import Path
from tools.host_control.approval.contracts import PlanMetadata, PolicyVerdict
from tools.host_control.approval.journal import ApprovalJournal
from tools.host_control.approval.service import ApprovalService


class Meta:
    def __init__(self, plans):
        self.plans = plans

    def plan_metadata(self, plan_handle):
        return self.plans[plan_handle]


class Yes:
    def may_review(self, *args):
        return PolicyVerdict(True, "authorized")

    def may_handoff(self, *args):
        return PolicyVerdict(True, "authorized")

    def may_decide(self, *args):
        return PolicyVerdict(True, "authorized")

    def may_revoke(self, *args):
        return PolicyVerdict(True, "authorized")


journal = ApprovalJournal.open(Path(sys.argv[1]))
metadata = PlanMetadata(
    "consultant", "consultant", "recovery", ("persona.install",),
    sys.argv[4], sys.argv[5], "manifest-r1", "policy-r1",
)
service = ApprovalService(
    journal=journal,
    metadata_source=Meta({sys.argv[3]: metadata}),
    clock=lambda: datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc),
    authorizer=Yes(),
    registered_consumer_uids=frozenset((2001,)),
)
sys.stdin.readline()
"""
            + (
                """
from tools.host_control.contracts import HostControlError

try:
    outcome = service.validate_and_bind(
        2001, sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[6]
    )
    print("BIND", outcome.state, outcome.reason_code, flush=True)
except HostControlError as exc:
    print("BIND", "refused", type(exc).__name__, flush=True)
"""
                if action == "bind"
                else """
try:
    status = service.revoke("admin", sys.argv[2], "revoked_by_admin")
    print("REVOKE", "ok", status.state, flush=True)
except Exception as exc:
    print("REVOKE", "refused", type(exc).__name__, flush=True)
"""
            )
            + "journal.close()\n"
        )

    def test_concurrent_revoke_and_bind_serialize_to_exactly_one_winner_across_processes(self) -> None:
        # Gate 27: a REAL race across two real processes, serialized by
        # BEGIN IMMEDIATE + the state-guarded conditional UPDATE in the
        # approval journal. Exactly one winner, never both, never neither.
        plan_handle, approval_handle = self.issue_grant()
        metadata = self.metadata_source.plans[plan_handle]
        operation_handle = self.next_handle()
        argv = (
            str(self.journal_path),
            approval_handle,
            plan_handle,
            metadata.plan_digest,
            metadata.effect_digest,
            operation_handle,
        )
        # the fixture connection would hold its own lock windows during the
        # race; close it so only the two racing processes touch the journal
        self.journal.close()
        processes = [
            subprocess.Popen(
                (sys.executable, "-c", self._race_script(action), *argv),
                cwd=REPO_ROOT,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for action in ("bind", "revoke")
        ]
        time.sleep(0.5)  # both sit blocked on the stdin gate
        # release BOTH gates before collecting either result — a sequential
        # communicate() would serialize the arms and prove nothing
        for process in processes:
            process.stdin.write("GO\n")
            process.stdin.flush()
        outputs = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(stdout.strip())
        bind_out = next(line for line in outputs if line.startswith("BIND"))
        revoke_out = next(
            line for line in outputs if line.startswith("REVOKE")
        )
        with sqlite3.connect(self.journal_path) as connection:
            state, consumed = connection.execute(
                "SELECT state, consuming_operation_handle FROM grants "
                "WHERE approval_handle=?",
                (approval_handle,),
            ).fetchone()
        # The loser refuses with a HostControlError either way: the plain
        # one when it lost the state-guarded UPDATE, or JournalBusy (its
        # subclass) when the winner held the write lock past the busy
        # timeout on a loaded host. Never a raw sqlite3 error. The
        # exactly-one-winner and final-state invariants carry the intent.
        if bind_out == "BIND bound bound":
            # bind won: the commit stands; revoke refused; grant consumed
            self.assertIn(
                revoke_out,
                (
                    "REVOKE refused HostControlError",
                    "REVOKE refused JournalBusy",
                ),
            )
            self.assertEqual(state, "bound")
            self.assertEqual(consumed, operation_handle)
        else:
            # revoke won: bind denied with the revoked reason code, or
            # refused outright on lock contention; grant stays unconsumed
            self.assertIn(
                bind_out,
                ("BIND denied revoked", "BIND refused JournalBusy"),
            )
            self.assertEqual(revoke_out, "REVOKE ok revoked")
            self.assertEqual(state, "revoked")
            self.assertIsNone(consumed)
        self.journal = ApprovalJournal.open(self.journal_path)

    def test_revoke_before_bind_prevents_authorization(self) -> None:
        # Gate 27, deterministic ordering 1 (same-process; the racing proof
        # is the cross-process test above).
        plan_handle, approval_handle = self.issue_grant()
        status = self.service.revoke(
            "admin", approval_handle, "revoked_by_admin"
        )
        self.assertEqual(status.state, "revoked")
        outcome = self.bind(plan_handle, approval_handle)
        self.assertEqual(outcome.state, "denied")
        self.assertEqual(outcome.reason_code, "revoked")
        self.assertEqual(
            [event[1:] for event in self.grant_events(approval_handle)][-1][
                :2
            ],
            ("issued", "revoked"),
        )

    def test_bind_before_revoke_commit_stands_and_revoke_does_not_unconsume(self) -> None:
        # Gate 27, deterministic ordering 2: a bound grant is never
        # "unconsumed" — stopping the operation is the broker's
        # cancellation/rollback policy, not a revocation.
        plan_handle, approval_handle = self.issue_grant()
        operation_handle = self.next_handle()
        first = self.bind(plan_handle, approval_handle, operation_handle)
        self.assertEqual(first.state, "bound")
        with self.assertRaises(HostControlError):
            self.service.revoke("admin", approval_handle, "revoked_by_admin")
        row = self.grant_row(approval_handle)
        self.assertEqual(row["state"], "bound")
        self.assertEqual(
            row["consuming_operation_handle"], operation_handle
        )
        replay = self.bind(plan_handle, approval_handle, operation_handle)
        self.assertEqual(
            replay.authority_record_id, first.authority_record_id
        )


class LockClassifierTests(ApprovalCaseBase):
    """Both journals classify sqlite lock contention identically.

    The classifier is duplicated per journal on purpose (L3: no import edge
    across the authority boundary), so one sample table drives both copies:
    drift between them fails here.
    """

    MODULES = (approval_journal_module, broker_journal_module)

    def real_busy_error(self) -> sqlite3.OperationalError:
        with held_write_lock(self.journal_path):
            contender = sqlite3.connect(
                self.journal_path, timeout=0, isolation_level=None
            )
            try:
                contender.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                return exc
            finally:
                contender.close()
        raise AssertionError("a held write lock did not block BEGIN IMMEDIATE")

    def test_a_real_busy_error_is_lock_contention(self) -> None:
        exc = self.real_busy_error()
        # the classifier's real input: SQLITE_BUSY (5), wording as observed
        self.assertEqual(exc.sqlite_errorcode & 0xFF, 5)
        self.assertEqual(str(exc), "database is locked")
        for module in self.MODULES:
            with self.subTest(module=module.__name__):
                self.assertTrue(module._is_lock_contention(exc))

    def test_a_real_non_lock_operational_error_is_not_contention(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            with self.assertRaises(sqlite3.OperationalError) as caught:
                connection.execute("SELECT * FROM no_such_table")
        finally:
            connection.close()
        for module in self.MODULES:
            with self.subTest(module=module.__name__):
                self.assertFalse(
                    module._is_lock_contention(caught.exception)
                )

    def test_error_code_decides_and_message_is_only_a_fallback(self) -> None:
        def coded(message: str, code: int) -> sqlite3.OperationalError:
            exc = sqlite3.OperationalError(message)
            exc.sqlite_errorcode = code
            return exc

        samples = (
            # (exception, expected contention)
            (coded("database is locked", 5), True),  # SQLITE_BUSY
            (coded("database table is locked", 6), True),  # SQLITE_LOCKED
            (coded("database is locked", 5 | (2 << 8)), True),  # extended
            (coded("anything", 261), True),  # BUSY_RECOVERY extended code
            (coded("database is locked", 1), False),  # code wins
            (coded("disk I/O error", 10), False),
            # no error code (older interpreters / hand-built): exact text
            (sqlite3.OperationalError("database is locked"), True),
            (sqlite3.OperationalError("database table is locked"), True),
            (sqlite3.OperationalError("unable to open database file"), False),
            (sqlite3.OperationalError("cannot rollback"), False),
        )
        for module in self.MODULES:
            for exc, expected in samples:
                with self.subTest(
                    module=module.__name__,
                    message=str(exc),
                    code=getattr(exc, "sqlite_errorcode", None),
                ):
                    self.assertIs(module._is_lock_contention(exc), expected)


class LockContentionTests(ApprovalCaseBase):
    """Write-lock contention refuses with JournalBusy, never a raw sqlite
    error (T-4334). Every test forces the contention deterministically:
    a second connection HOLDS the write lock (handshake: BEGIN IMMEDIATE
    has returned) while the journal under test has a 50 ms busy timeout,
    so the outcome does not depend on host load.
    """

    BUSY_MS = 50

    def setUp(self) -> None:
        super().setUp()
        # setup work runs through self.service (default 5 s timeout); only
        # the act under contention goes through the short-timeout journal
        self.short = ApprovalJournal.open(
            self.journal_path, busy_timeout_ms=self.BUSY_MS
        )
        self.short_service = self.build_service(journal=self.short)

    def tearDown(self) -> None:
        self.short.close()
        super().tearDown()

    def assert_discloses_no_path(self, error: Exception) -> None:
        # The message is the wire body (error_body_for, bounded but
        # verbatim): server-authored, and it must never name where the
        # journal lives. The root check also catches a parent-dir leak.
        self.assertNotIn(str(self.journal_path), str(error))
        self.assertNotIn(str(self.root), str(error))

    def assert_nothing_written(self) -> None:
        with sqlite3.connect(self.journal_path) as connection:
            for table in (
                "reviews",
                "review_events",
                "grants",
                "grant_events",
            ):
                self.assertEqual(
                    connection.execute(
                        f"SELECT count(*) FROM {table}"
                    ).fetchone()[0],
                    0,
                    table,
                )

    def test_revoke_refuses_with_journal_busy_when_lock_held_past_timeout(self) -> None:
        plan_handle, approval_handle = self.issue_grant()
        events_before = self.grant_events(approval_handle)
        with held_write_lock(self.journal_path):
            with self.assertRaises(JournalBusy) as caught:
                self.short_service.revoke(
                    "admin", approval_handle, "revoked_by_admin"
                )
        error = caught.exception
        self.assertIsInstance(error, HostControlError)
        # a lost LOCK is not a lost state-guarded UPDATE: callers must not
        # re-read and classify against a winner that has not committed
        self.assertNotIsInstance(error, StaleAuthorityState)
        message = str(error)
        self.assertIn("transition_grant[revoked]", message)
        self.assertIn("lock contention", message)
        self.assertIn(f"{self.BUSY_MS} ms", message)
        self.assert_discloses_no_path(error)
        self.assertIsInstance(error.__cause__, sqlite3.OperationalError)
        self.assertEqual(error.__cause__.sqlite_errorcode & 0xFF, 5)
        # fail closed with no partial write; retryable once the lock frees
        self.assertEqual(self.grant_row(approval_handle)["state"], "issued")
        self.assertEqual(self.grant_events(approval_handle), events_before)
        status = self.short_service.revoke(
            "admin", approval_handle, "revoked_by_admin"
        )
        self.assertEqual(status.state, "revoked")

    def test_bind_refuses_with_journal_busy_and_leaves_the_grant_issued(self) -> None:
        plan_handle, approval_handle = self.issue_grant()
        events_before = self.grant_events(approval_handle)
        with held_write_lock(self.journal_path):
            with self.assertRaises(JournalBusy) as caught:
                self.bind(
                    plan_handle,
                    approval_handle,
                    service=self.short_service,
                )
        self.assertIn("transition_grant[bound]", str(caught.exception))
        self.assertIn("lock contention", str(caught.exception))
        self.assert_discloses_no_path(caught.exception)
        row = self.grant_row(approval_handle)
        self.assertEqual(row["state"], "issued")
        self.assertIsNone(row["consuming_operation_handle"])
        self.assertEqual(self.grant_events(approval_handle), events_before)
        # retryable: the same bind succeeds once the lock frees
        outcome = self.bind(
            plan_handle, approval_handle, service=self.short_service
        )
        self.assertEqual(outcome.state, "bound")

    def test_every_write_site_refuses_with_journal_busy_and_writes_nothing(self) -> None:
        review = self.make_review_record("open")
        granted = transition_review_record(
            review,
            "granted",
            decision="granted",
            reason_code="approved",
            approver_subject="specialist",
            authority_source="agent",
            audit_anchor="anchor-1",
            decide_idempotency_key="decide-x",
            decided_at=FIXED_NOW.isoformat(),
        )
        grant = self.make_grant_record(
            "issued", review_handle=review.review_handle
        )
        revoked = transition_grant_record(
            grant, "revoked", updated_at=LATE_NOW.isoformat()
        )
        journal = self.short
        sites = {
            "put_review_or_replay": lambda: journal.put_review_or_replay(
                record=review
            ),
            "record_decision": lambda: journal.record_decision(
                current=review, new=granted, grant=grant, payload={}
            ),
            "transition_review[expired]": lambda: journal.transition_review(
                current=review,
                new=transition_review_record(review, "expired"),
                event_kind="expired",
                payload={},
                created_at=LATE_NOW.isoformat(),
            ),
            "transition_grant[revoked]": lambda: journal.transition_grant(
                current=grant,
                new=revoked,
                event_kind="revoked",
                payload={},
            ),
            "append_grant_event[bind_denied]": lambda: (
                journal.append_grant_event(
                    current=grant,
                    event_kind="bind_denied",
                    payload={},
                    created_at=LATE_NOW.isoformat(),
                )
            ),
        }
        for label, act in sites.items():
            with self.subTest(site=label):
                with held_write_lock(self.journal_path):
                    with self.assertRaises(JournalBusy) as caught:
                        act()
                self.assertIn(label, str(caught.exception))
                self.assertIn("lock contention", str(caught.exception))
                self.assert_discloses_no_path(caught.exception)
        self.assert_nothing_written()

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

        error = sqlite3.OperationalError("unable to open database file")
        journal = ApprovalJournal(self.journal_path, BeginFails(error))
        grant = self.make_grant_record("issued")
        with self.assertRaises(sqlite3.OperationalError) as caught:
            journal.transition_grant(
                current=grant,
                new=transition_grant_record(
                    grant, "revoked", updated_at=LATE_NOW.isoformat()
                ),
                event_kind="revoked",
                payload={},
            )
        self.assertIs(caught.exception, error)
        self.assertNotIsInstance(caught.exception, HostControlError)

    def test_open_refuses_an_invalid_busy_timeout_and_allows_zero(self) -> None:
        for journal_class in (ApprovalJournal, BrokerJournal):
            for bad in (-1, True, 1.5, "50", None):
                path = self.root / f"bad-{journal_class.__name__}-{bad!r}"
                with self.subTest(journal=journal_class.__name__, value=bad):
                    with self.assertRaises(HostControlError):
                        journal_class.open(path, busy_timeout_ms=bad)
                    # refused before any file was touched
                    self.assertFalse(path.exists())
            zero = self.root / f"zero-{journal_class.__name__}.sqlite3"
            journal_class.open(zero, busy_timeout_ms=0).close()

    def test_open_applies_the_injected_busy_timeout_to_the_connection(self) -> None:
        # Pins the seam itself: the injected value must reach the live
        # connection. Wall time alone cannot prove it (a hard-coded 5000
        # would still lose the lock, only slower), so read the PRAGMA back
        # on the journal's own connection (busy_timeout is per-connection).
        for journal_class, module in (
            (ApprovalJournal, approval_journal_module),
            (BrokerJournal, broker_journal_module),
        ):
            for injected in (0, self.BUSY_MS, 1234):
                path = (
                    self.root
                    / f"timeout-{journal_class.__name__}-{injected}.sqlite3"
                )
                with self.subTest(
                    journal=journal_class.__name__, value=injected
                ):
                    journal = journal_class.open(path, busy_timeout_ms=injected)
                    try:
                        applied = journal._connection.execute(
                            "PRAGMA busy_timeout"
                        ).fetchone()[0]
                    finally:
                        journal.close()
                    self.assertEqual(applied, injected)
            # and the default is the documented 5 s, unchanged
            default_path = self.root / f"default-{journal_class.__name__}.sqlite3"
            journal = journal_class.open(default_path)
            try:
                self.assertEqual(
                    journal._connection.execute(
                        "PRAGMA busy_timeout"
                    ).fetchone()[0],
                    module.BUSY_TIMEOUT_MS,
                )
                self.assertEqual(module.BUSY_TIMEOUT_MS, 5000)
            finally:
                journal.close()

    def test_no_raw_begin_immediate_remains_outside_the_helper(self) -> None:
        # Structural guard (P1): a future write method that opens its
        # transaction with a raw execute("BEGIN IMMEDIATE") would leak the
        # sqlite error again. The broker's migration builder writes only a
        # private tmp file and is already wrapped into HostControlError.
        allowed = {"_begin_immediate", "_migrate_build_tmp"}
        minimum_helper_calls = {
            approval_journal_module.__file__: 5,
            broker_journal_module.__file__: 7,
        }
        for module_file, minimum in minimum_helper_calls.items():
            with self.subTest(module=Path(module_file).name):
                tree = ast.parse(Path(module_file).read_text("utf-8"))
                raw: set[str] = set()
                helper_calls = 0
                for function in ast.walk(tree):
                    if not isinstance(function, ast.FunctionDef):
                        continue
                    for node in ast.walk(function):
                        if not isinstance(node, ast.Call) or not isinstance(
                            node.func, ast.Attribute
                        ):
                            continue
                        if (
                            node.func.attr == "execute"
                            and node.args
                            and isinstance(node.args[0], ast.Constant)
                            and node.args[0].value == "BEGIN IMMEDIATE"
                        ):
                            raw.add(function.name)
                        if node.func.attr == "_begin_immediate":
                            helper_calls += 1
                self.assertLessEqual(raw, allowed)
                self.assertGreaterEqual(helper_calls, minimum)


class DurabilityTests(ApprovalCaseBase):
    READ_SCRIPT = """
import sys
from pathlib import Path
from tools.host_control.approval.journal import ApprovalJournal
journal = ApprovalJournal.open(Path(sys.argv[1]))
if sys.argv[2] == "review":
    record = journal.get_review(sys.argv[3])
else:
    record = journal.get_grant(sys.argv[3])
assert record.validate_digest()
print(record.state)
journal.close()
"""

    def _read_state(self, kind: str, handle: str) -> str:
        completed = subprocess.run(
            (
                sys.executable,
                "-c",
                self.READ_SCRIPT,
                str(self.journal_path),
                kind,
                handle,
            ),
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def test_review_and_grant_state_survive_cross_process_restart(self) -> None:
        # Gate 29 sliver: REAL second process reopens the journal and
        # validates the digest chain at every lifecycle stage, including the
        # bind commit.
        open_review = self.open_review(plan_handle=self.fresh_plan())
        plan_denied = self.fresh_plan()
        denied_review = self.open_review(plan_handle=plan_denied)
        self.service.decide(
            "specialist",
            denied_review.review_handle,
            self.metadata_source.plans[plan_denied].plan_digest,
            "denied",
            "rejected",
            "anchor-d",
            "decide-durable-denied",
        )
        _, issued_approval = self.issue_grant()
        plan_bound, bound_approval = self.issue_grant()
        self.bind(plan_bound, bound_approval)
        _, revoked_approval = self.issue_grant()
        self.service.revoke("admin", revoked_approval, "revoked_by_admin")
        expectations = {
            ("review", open_review.review_handle): "open",
            ("review", denied_review.review_handle): "denied",
            ("grant", issued_approval): "issued",
            ("grant", bound_approval): "bound",
            ("grant", revoked_approval): "revoked",
        }
        for (kind, handle), expected in expectations.items():
            with self.subTest(stage=f"{kind}:{expected}"):
                self.assertEqual(self._read_state(kind, handle), expected)

    def test_cached_issued_grant_authorizes_nothing_across_restart(self) -> None:
        # Gate 28 sliver: an ISSUED grant durably survives restart but
        # grants NOTHING until the bind commit — status is bounded state,
        # never authority.
        plan_handle, approval_handle = self.issue_grant()
        script = """
import sys
from datetime import datetime, timezone
from pathlib import Path
from tools.host_control.approval.journal import ApprovalJournal
from tools.host_control.approval.service import ApprovalService


class NoMetadata:
    def plan_metadata(self, plan_handle):
        raise AssertionError("status must not fetch metadata")


journal = ApprovalJournal.open(Path(sys.argv[1]))
service = ApprovalService(
    journal=journal,
    metadata_source=NoMetadata(),
    clock=lambda: datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc),
    registered_consumer_uids=frozenset((2001,)),
)
status = service.approval_status(2001, sys.argv[2])
assert not hasattr(status, "authority_record_id")
assert not hasattr(status, "nonce")
print("STATUS", status.state)
journal.close()
"""
        completed = subprocess.run(
            (
                sys.executable,
                "-c",
                script,
                str(self.journal_path),
                approval_handle,
            ),
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.stdout.strip(), "STATUS issued")
        self.assertEqual(self.grant_row(approval_handle)["state"], "issued")

    def test_abandoned_bind_whose_commit_landed_stays_bound_and_consumed(self) -> None:
        # Gate 31 sliver: the client abandoning the call does not cancel,
        # revoke, reuse, or duplicate — the durable commit IS the truth.
        plan_handle, approval_handle = self.issue_grant()
        metadata = self.metadata_source.plans[plan_handle]
        operation_handle = self.next_handle()
        script = (
            self.__class__.ABANDON_SCRIPT
        )
        completed = subprocess.run(
            (
                sys.executable,
                "-c",
                script,
                str(self.journal_path),
                approval_handle,
                plan_handle,
                metadata.plan_digest,
                metadata.effect_digest,
                operation_handle,
            ),
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 7, completed.stderr)
        abandoned_authority = completed.stdout.strip()
        row = self.grant_row(approval_handle)
        self.assertEqual(row["state"], "bound")
        self.assertEqual(
            row["consuming_operation_handle"], operation_handle
        )
        replay = self.bind(plan_handle, approval_handle, operation_handle)
        self.assertEqual(replay.state, "bound")
        self.assertEqual(replay.reason_code, "bound_replay")
        self.assertEqual(replay.authority_record_id, abandoned_authority)

    ABANDON_SCRIPT = """
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from tools.host_control.approval.contracts import PlanMetadata
from tools.host_control.approval.journal import ApprovalJournal
from tools.host_control.approval.service import ApprovalService


class Meta:
    def __init__(self, plans):
        self.plans = plans

    def plan_metadata(self, plan_handle):
        return self.plans[plan_handle]


journal = ApprovalJournal.open(Path(sys.argv[1]))
metadata = PlanMetadata(
    "consultant", "consultant", "recovery", ("persona.install",),
    sys.argv[4], sys.argv[5], "manifest-r1", "policy-r1",
)
service = ApprovalService(
    journal=journal,
    metadata_source=Meta({sys.argv[3]: metadata}),
    clock=lambda: datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc),
    registered_consumer_uids=frozenset((2001,)),
)
outcome = service.validate_and_bind(
    2001, sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[6]
)
assert outcome.state == "bound", outcome
print(outcome.authority_record_id, flush=True)
os._exit(7)  # abandon: no close, no result delivery
"""

    def test_uncommitted_or_corrupt_approval_record_never_serves(self) -> None:
        # Gate 29 sliver. REAL second process dying pre-commit: the row
        # never becomes visible. Corruption arms are same-process (logic
        # correctness): tampering is loud, never repaired by inference, and
        # can never un-consume a grant.
        plan_handle, approval_handle = self.issue_grant()
        review_handle = self.grant_row(approval_handle)["review_handle"]
        death_script = """
import os
import sqlite3
import sys
connection = sqlite3.connect(sys.argv[1])
connection.execute("BEGIN IMMEDIATE")
connection.execute(
    "INSERT INTO reviews SELECT ?, plan_handle, plan_digest, plan_owner, "
    "performer, target_seat_id, operation_families, effect_digest, "
    "manifest_revision, policy_revision, handoff_kind, request_plan_handle, "
    "request_plan_digest, request_plan_owner, shared_effect_digest, "
    "handoff_anchor, ?, state, decision, reason_code, approver_subject, "
    "authority_source, audit_anchor, source_event, ?, created_at, "
    "expires_at, decided_at, review_json FROM reviews LIMIT 1",
    ("f" * 64, "other-requester", "uncommitted-key"),
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
                    "SELECT count(*) FROM reviews WHERE review_handle=?",
                    ("f" * 64,),
                ).fetchone()[0],
                0,
            )

        def overwrite(table: str, column: str, value: object, handle_column: str, handle: str) -> None:
            with sqlite3.connect(self.journal_path) as connection:
                connection.execute(
                    f"UPDATE {table} SET {column}=? WHERE {handle_column}=?",
                    (value, handle),
                )
                connection.commit()

        original_grant_json = self.grant_row(approval_handle)["grant_json"]
        # non-canonical JSON
        overwrite(
            "grants",
            "grant_json",
            json.dumps(json.loads(original_grant_json), indent=2),
            "approval_handle",
            approval_handle,
        )
        with self.assertRaises(HostControlError):
            self.journal.get_grant(approval_handle)
        # tampered digest
        tampered = dict(json.loads(original_grant_json))
        tampered["digest"] = "c" * 64
        overwrite(
            "grants",
            "grant_json",
            canonical_json(tampered),
            "approval_handle",
            approval_handle,
        )
        with self.assertRaises(HostControlError):
            self.journal.get_grant(approval_handle)
        overwrite(
            "grants",
            "grant_json",
            original_grant_json,
            "approval_handle",
            approval_handle,
        )
        # column-vs-JSON divergence: a doctored state column cannot serve
        overwrite(
            "grants", "state", "revoked", "approval_handle", approval_handle
        )
        # (also clears the CHECK pairing? no: issued has no consuming handle,
        # revoked is also unconsumed — the row stays CHECK-valid, only the
        # JSON mirror disagrees. That disagreement must be loud.)
        with self.assertRaises(HostControlError):
            self.journal.get_grant(approval_handle)
        overwrite(
            "grants", "state", "issued", "approval_handle", approval_handle
        )
        # tampering can never un-consume: bind, then doctor the consuming
        # handle column; the mirror check refuses to serve the row
        outcome = self.bind(plan_handle, approval_handle)
        self.assertEqual(outcome.state, "bound")
        bound_json = self.grant_row(approval_handle)["grant_json"]
        overwrite(
            "grants",
            "consuming_operation_handle",
            "e" * 64,
            "approval_handle",
            approval_handle,
        )
        with self.assertRaises(HostControlError):
            self.journal.get_grant(approval_handle)
        overwrite(
            "grants",
            "consuming_operation_handle",
            json.loads(bound_json)["consuming_operation_handle"],
            "approval_handle",
            approval_handle,
        )
        # reviews: divergent state column is equally loud
        original_review_json = self.review_row(review_handle)["review_json"]
        overwrite(
            "reviews", "review_json",
            json.dumps(json.loads(original_review_json), indent=2),
            "review_handle", review_handle,
        )
        with self.assertRaises(HostControlError):
            self.journal.get_review(review_handle)


class Gate28SurfaceTests(ApprovalCaseBase):
    # The documented public-surface classification map: adding a public
    # method without classifying it here FAILS the enumeration test.
    PUBLIC_METHOD_RETURNS = {
        "review": (ReviewResult,),
        "review_get": (ReviewRecord,),
        "decide": (DecisionResult,),
        "validate_and_bind": (BindOutcome,),
        "approval_status": (ApprovalStatus,),
        "as_binder": (ConsumerBinder,),
        "revoke": (ApprovalStatus,),
        "expire_due": (str,),
        "expiry_sweep_status": (ExpirySweepStatus,),
        "pending_counts": (ApprovalCounts,),
        # T-2919 human ingress: none carries a nonce or authority record
        "request_human": (HumanRequestResult,),
        "human_decision": (HumanDecisionResult,),
        "withdraw_human_request": (HumanRequestResult,),
        "review_extras": (ReviewExtras,),
        # T-5518 async card delivery: a flag and a count, never a record
        "deliver_human_request": (bool,),
        "resume_human_deliveries": (int,),
        # T-2920 U2: the keyset walk over every pending request; a count
        "resume_all_human_deliveries": (int,),
    }

    def test_no_service_method_returns_authority_without_durable_bound_commit(self) -> None:
        """Gate 28 (approval half) + PO mandate (f), structural.

        There is NO method on the approval service that yields authority
        without the durable BOUND commit: the only BindOutcome-returning
        surface is validate_and_bind (and the ConsumerBinder adapter that
        delegates to it), and no other return type carries a nonce or an
        authority_record_id field. ApprovalStatus is bounded state, never
        authority — the broker cannot substitute a status read for binding.
        """

        public_methods = {
            name
            for name, member in inspect.getmembers(
                ApprovalService, predicate=inspect.isfunction
            )
            if not name.startswith("_")
        }
        self.assertEqual(public_methods, set(self.PUBLIC_METHOD_RETURNS))
        for name, return_types in self.PUBLIC_METHOD_RETURNS.items():
            with self.subTest(method=name):
                hint = str(
                    typing.get_type_hints(
                        getattr(ApprovalService, name)
                    ).get("return")
                )
                self.assertNotIn("GrantRecord", hint)
                for return_type in return_types:
                    if not dataclasses.is_dataclass(return_type):
                        continue
                    field_names = {
                        field.name
                        for field in dataclasses.fields(return_type)
                    }
                    self.assertNotIn("nonce", field_names)
                    if return_type is not BindOutcome:
                        self.assertNotIn(
                            "authority_record_id", field_names
                        )
        # BindOutcome is the single authority channel and only
        # validate_and_bind returns it
        bind_returners = {
            name
            for name, return_types in self.PUBLIC_METHOD_RETURNS.items()
            if BindOutcome in return_types
        }
        self.assertEqual(bind_returners, {"validate_and_bind"})
        # behavioral: an ISSUED grant's status carries state, never authority
        plan_handle, approval_handle = self.issue_grant()
        status = self.service.approval_status(CONSUMER_UID, approval_handle)
        self.assertEqual(status.state, "issued")
        self.assertLessEqual(len(status.summary), 256)
        self.assertFalse(hasattr(status, "authority_record_id"))
        self.assertFalse(hasattr(status, "nonce"))
        # T-4875: the status names the grant's plan binding so the broker can
        # refuse a read whose grant is not bound to the caller's own plan
        self.assertEqual(status.plan_handle, plan_handle)
        grant = self.service._journal.get_grant(approval_handle)
        self.assertEqual(status.plan_owner, grant.plan_owner)
        # and the DecisionResult that minted it exposed only the opaque
        # handle (gate 21: grant payloads never enter model context)
        self.assertEqual(
            {field.name for field in dataclasses.fields(DecisionResult)},
            {
                "review_handle",
                "state",
                "decision",
                "reason_code",
                "approval_handle",
            },
        )

    def test_consumer_binder_adapter_satisfies_landed_approval_binder_contract(self) -> None:
        # L5: binding a uid yields an object satisfying the broker's landed
        # ApprovalBinder Protocol — signature parity plus behavioral proof.
        from tools.host_control.operations import ApprovalBinder

        protocol_params = list(
            inspect.signature(ApprovalBinder.validate_and_bind).parameters
        )
        adapter_params = list(
            inspect.signature(ConsumerBinder.validate_and_bind).parameters
        )
        self.assertEqual(adapter_params, protocol_params)
        plan_handle, approval_handle = self.issue_grant()
        binder = self.service.as_binder(CONSUMER_UID)
        outcome = binder.validate_and_bind(
            approval_handle,
            plan_handle,
            self.metadata_source.plans[plan_handle].plan_digest,
            self.next_handle(),
        )
        self.assertIsInstance(outcome, BindOutcome)
        self.assertEqual(outcome.state, "bound")


class ExpiryTests(ApprovalCaseBase):
    def test_expiry_is_durable_lazy_and_never_reusable(self) -> None:
        # L8: a grant or review past expires_at transitions to expired
        # DURABLY on access — the terminal transition is written, not merely
        # computed in memory — and never becomes reusable.
        plan_handle, approval_handle = self.issue_grant()
        late = self.build_service(clock=lambda: LATE_NOW)
        status = late.approval_status(CONSUMER_UID, approval_handle)
        self.assertEqual(status.state, "expired")
        self.assertEqual(self.grant_row(approval_handle)["state"], "expired")
        self.assertEqual(
            [event[1:] for event in self.grant_events(approval_handle)][-1],
            ("issued", "expired", "expired"),
        )
        outcome = self.bind(plan_handle, approval_handle, service=late)
        self.assertEqual(outcome.state, "denied")
        self.assertEqual(outcome.reason_code, "expired")
        # the early clock cannot resurrect it: expired is terminal
        outcome = self.bind(plan_handle, approval_handle)
        self.assertEqual(outcome.reason_code, "expired")
        # reviews expire durably on the decide path and refuse the decision
        plan_handle = self.fresh_plan()
        review = self.open_review(plan_handle=plan_handle)
        with self.assertRaises(HostControlError):
            late.decide(
                "specialist",
                review.review_handle,
                self.metadata_source.plans[plan_handle].plan_digest,
                "granted",
                "approved",
                "anchor-late",
                "decide-late",
            )
        self.assertEqual(
            self.review_row(review.review_handle)["state"],
            "expired",
        )

    def test_expire_due_sweeps_bounded(self) -> None:
        # L8: a bounded expire_due for a FUTURE sweeper — nothing calls it
        # yet (no daemon, no timer); a future sweeper must expose
        # consecutive_busy_sweeps via Health1 (T-4350).
        # Composition note (plan DECISIONS): the expiry transition goes
        # through the same state-guarded UPDATE as bind/revoke, so an expiry
        # racing a bind also serializes to exactly one winner.
        handles = []
        for _ in range(3):
            handles.append(
                self.open_review(plan_handle=self.fresh_plan()).review_handle
            )
        for _ in range(2):
            handles.append(self.issue_grant()[1])
        late = self.build_service(clock=lambda: LATE_NOW)
        first = late.expire_due(limit=3)
        self.assertEqual(len(first), 3)
        second = late.expire_due(limit=100)
        self.assertEqual(len(second), 2)
        self.assertEqual(set(first) | set(second), set(handles))
        self.assertEqual(late.expire_due(limit=100), ())
        counts = self.service.pending_counts()
        self.assertEqual(counts, ApprovalCounts(0, 0, 0))


class SweepFaultJournal:
    """Delegating journal proxy that injects faults into an expiry sweep.

    Wraps the REAL journal under test and counts ``transition_*`` attempts.
    ``lock_before`` (1-based attempt number) makes a REAL second sqlite
    connection take the write lock (``BEGIN IMMEDIATE`` has returned, so
    the lock is held) immediately before that attempt is delegated: the
    JournalBusy then comes from real sqlite through the real
    ``_begin_immediate`` mapping, with no sleeps. ``stale_on`` attempts
    raise ``StaleAuthorityState`` instead (a lost state-guarded race).
    """

    def __init__(
        self,
        journal: ApprovalJournal,
        *,
        lock_before: int | None = None,
        stale_on: frozenset[int] = frozenset(),
    ) -> None:
        self._journal = journal
        self._lock_before = lock_before
        self._stale_on = stale_on
        self._holder: sqlite3.Connection | None = None
        self.attempts = 0

    def __getattr__(self, name: str):
        return getattr(self._journal, name)

    def _before_attempt(self) -> None:
        self.attempts += 1
        if self.attempts == self._lock_before and self._holder is None:
            self._holder = sqlite3.connect(
                self._journal.path, isolation_level=None
            )
            self._holder.execute("BEGIN IMMEDIATE")
        if self.attempts in self._stale_on:
            raise StaleAuthorityState("injected lost race")

    def transition_review(self, **kwargs):
        self._before_attempt()
        return self._journal.transition_review(**kwargs)

    def transition_grant(self, **kwargs):
        self._before_attempt()
        return self._journal.transition_grant(**kwargs)

    def release(self) -> None:
        holder, self._holder = self._holder, None
        if holder is None:
            return
        try:
            holder.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        holder.close()


class ExpiryLockContentionTests(ApprovalCaseBase):
    """expire_due under write-lock contention (T-4350).

    The sqlite write lock covers the whole database, so contention is
    correlated across items: the first JournalBusy stops the sweep (a
    per-item skip would cost up to limit x busy-timeout), the partial
    progress is returned, and every remaining due item - including the one
    that hit busy - retries on the next sweep. Contention stays observable
    through ``expiry_sweep_status``.
    """

    BUSY_MS = 50

    def setUp(self) -> None:
        super().setUp()
        self.short = ApprovalJournal.open(
            self.journal_path, busy_timeout_ms=self.BUSY_MS
        )
        self.faults: SweepFaultJournal | None = None

    def tearDown(self) -> None:
        if self.faults is not None:
            self.faults.release()
        self.short.close()
        super().tearDown()

    def make_due_items(self, reviews: int, grants: int) -> None:
        for _ in range(reviews):
            self.open_review(plan_handle=self.fresh_plan())
        for _ in range(grants):
            self.issue_grant()

    def sweeper(self, **fault_kwargs) -> ApprovalService:
        self.faults = SweepFaultJournal(self.short, **fault_kwargs)
        return self.build_service(
            journal=self.faults, clock=lambda: LATE_NOW
        )

    def due_items(self) -> tuple[tuple[str, str], ...]:
        return self.journal.due_for_expiry(LATE_NOW.isoformat(), limit=100)

    def due_handles(self) -> tuple[str, ...]:
        return tuple(handle for _kind, handle in self.due_items())

    def test_sweep_keeps_partial_progress_when_the_lock_is_taken_mid_sweep(self) -> None:
        self.make_due_items(reviews=2, grants=2)
        order = self.due_handles()
        self.assertEqual(len(order), 4)
        # reviews sweep first, so contention lands on a GRANT item here
        # (the review kind is pinned by the stop-at-first-busy test)
        self.assertEqual(
            [kind for kind, _handle in self.due_items()],
            ["review", "review", "grant", "grant"],
        )
        sweeper = self.sweeper(lock_before=3)
        # no raise: the two items transitioned before contention survive
        first = sweeper.expire_due(limit=100)
        self.assertEqual(first, order[:2])
        # the sweep stopped at the busy grant: attempts 1-2 committed,
        # attempt 3 was the busy one, and the 4th item was never tried
        self.assertEqual(self.faults.attempts, 3)
        self.assertEqual(self.due_handles(), order[2:])
        # the item that hit busy was not transitioned and is not returned
        self.assertNotIn(order[2], first)
        self.faults.release()
        # the next sweep expires the rest, including the one that hit busy
        second = sweeper.expire_due(limit=100)
        self.assertEqual(second, order[2:])
        self.assertEqual(self.due_handles(), ())
        self.assertEqual(sweeper.expire_due(limit=100), ())
        self.assertEqual(
            self.service.pending_counts(), ApprovalCounts(0, 0, 0)
        )

    def test_sweep_stops_at_the_first_busy_and_does_not_try_the_rest(self) -> None:
        self.make_due_items(reviews=2, grants=3)
        order = self.due_handles()
        self.assertEqual(len(order), 5)
        sweeper = self.sweeper(lock_before=1)
        self.assertEqual(sweeper.expire_due(limit=100), ())
        # one attempt, not five: a per-item skip under a held lock would
        # burn limit x busy-timeout before returning
        self.assertEqual(self.faults.attempts, 1)
        self.assertEqual(self.due_handles(), order)
        self.faults.release()
        self.assertEqual(sweeper.expire_due(limit=100), order)
        self.assertEqual(self.due_handles(), ())

    def test_stale_authority_state_still_skips_per_item_and_is_not_contention(self) -> None:
        self.make_due_items(reviews=2, grants=2)
        order = self.due_handles()
        sweeper = self.sweeper(stale_on=frozenset((2,)))
        # the lost-race item is skipped; the sweep keeps going after it
        self.assertEqual(
            sweeper.expire_due(limit=100),
            (order[0], order[2], order[3]),
        )
        self.assertEqual(self.faults.attempts, 4)
        self.assertEqual(sweeper.expiry_sweep_status(), ExpirySweepStatus(0, 0))

    def test_contention_is_observable_and_resets_only_on_a_clean_sweep(self) -> None:
        self.make_due_items(reviews=1, grants=2)
        order = self.due_handles()
        # a fresh service has seen no contention
        self.assertEqual(
            self.build_service().expiry_sweep_status(),
            ExpirySweepStatus(busy_sweeps_total=0, consecutive_busy_sweeps=0),
        )
        sweeper = self.sweeper(lock_before=1)
        self.assertEqual(sweeper.expire_due(limit=100), ())
        self.assertEqual(sweeper.expiry_sweep_status(), ExpirySweepStatus(1, 1))
        # still contended: a permanently busy sweeper is visibly stuck
        self.assertEqual(sweeper.expire_due(limit=100), ())
        self.assertEqual(sweeper.expiry_sweep_status(), ExpirySweepStatus(2, 2))
        self.faults.release()
        self.assertEqual(sweeper.expire_due(limit=100), order)
        # consecutive resets on a clean sweep; the total never decreases
        self.assertEqual(sweeper.expiry_sweep_status(), ExpirySweepStatus(2, 0))
        # an idle clean sweep is not contention either
        self.assertEqual(sweeper.expire_due(limit=100), ())
        self.assertEqual(sweeper.expiry_sweep_status(), ExpirySweepStatus(2, 0))

    def test_partial_progress_then_busy_still_counts_as_a_busy_sweep(self) -> None:
        self.make_due_items(reviews=2, grants=1)
        order = self.due_handles()
        sweeper = self.sweeper(lock_before=2)
        self.assertEqual(sweeper.expire_due(limit=100), order[:1])
        self.assertEqual(sweeper.expiry_sweep_status(), ExpirySweepStatus(1, 1))

    def test_idle_sweep_resets_a_latched_busy_count_after_another_drain(self) -> None:
        # Failure mode: busy sweeps latch consecutive_busy_sweeps, the
        # leftovers are then drained elsewhere (lazy bind-time expiry or
        # another service on the same journal), and the sweeps go idle. An
        # idle sweep is a clean sweep, so the health signal must clear
        # instead of alarming falsely forever.
        self.make_due_items(reviews=1, grants=2)
        order = self.due_handles()
        sweeper = self.sweeper(lock_before=1)
        self.assertEqual(sweeper.expire_due(limit=100), ())
        self.assertEqual(sweeper.expiry_sweep_status(), ExpirySweepStatus(1, 1))
        self.faults.release()
        # a SECOND service on the same journal file drains the leftovers
        other = self.build_service(clock=lambda: LATE_NOW)
        self.assertEqual(other.expire_due(limit=100), order)
        self.assertEqual(self.due_handles(), ())
        # the first service's sweep now finds nothing due
        self.assertEqual(sweeper.expire_due(limit=100), ())
        self.assertEqual(self.faults.attempts, 1)
        self.assertEqual(sweeper.expiry_sweep_status(), ExpirySweepStatus(1, 0))

    def test_non_busy_journal_error_propagates_and_is_not_counted_as_contention(self) -> None:
        # JournalBusy IS-A HostControlError, so a broad catch would swallow
        # journal-tamper detection and report it as lock contention. Two
        # grants sweep first, then two reviews; tamper the first review so
        # get_review raises (a real corrupted JSON mirror, not a fake).
        for _ in range(2):
            self.issue_grant()
        for _ in range(2):
            self.open_review(plan_handle=self.fresh_plan())
        items = self.due_items()
        self.assertEqual(
            [kind for kind, _handle in items],
            ["grant", "grant", "review", "review"],
        )
        tampered_index = 2
        tampered_handle = items[tampered_index][1]
        original = self.review_row(tampered_handle)["review_json"]
        with sqlite3.connect(self.journal_path) as connection:
            connection.execute(
                "UPDATE reviews SET review_json=? WHERE review_handle=?",
                (
                    json.dumps(json.loads(original), indent=2),
                    tampered_handle,
                ),
            )
            connection.commit()
        # LATCH a busy sweep first (contention lands on the first grant,
        # before the tampered row is reached): a corruption incident must
        # not clear a latched busy count, which "starting from (0, 0)" could
        # not distinguish from a reset (a try/finally counter refactor).
        sweeper = self.sweeper(lock_before=1)
        self.assertEqual(sweeper.expire_due(limit=100), ())
        self.assertEqual(sweeper.expiry_sweep_status(), ExpirySweepStatus(1, 1))
        self.faults.release()
        with self.assertRaises(HostControlError) as caught:
            sweeper.expire_due(limit=100)
        self.assertNotIsInstance(caught.exception, JournalBusy)
        # neither clean nor busy: the raise itself is the signal, and the
        # latched counters are exactly as the busy sweep left them
        self.assertEqual(sweeper.expiry_sweep_status(), ExpirySweepStatus(1, 1))
        # everything before the bad row committed and stays expired
        for kind, handle in items[:tampered_index]:
            row = (
                self.review_row(handle)
                if kind == "review"
                else self.grant_row(handle)
            )
            self.assertEqual(row["state"], "expired", handle)
        # the tampered row and everything after it were never transitioned
        for kind, handle in items[tampered_index:]:
            row = (
                self.review_row(handle)
                if kind == "review"
                else self.grant_row(handle)
            )
            self.assertEqual(
                row["state"],
                "open" if kind == "review" else "issued",
                handle,
            )


class DefaultsTests(ApprovalCaseBase):
    def test_shipped_default_authorizer_denies_review_decide_and_revoke(self) -> None:
        # L9: the module default is DENY-ALL; this unit cannot ship an open
        # door. Composition: with the default-empty consumer allowlist a
        # default-constructed service is inert END-TO-END — it can neither
        # issue nor consume authority.
        permissive = self.service
        plan_handle = self.fresh_plan()
        review = self.open_review(plan_handle=plan_handle)
        _, approval_handle = self.issue_grant()
        default_service = ApprovalService(
            journal=self.journal,
            metadata_source=self.metadata_source,
            clock=lambda: FIXED_NOW,
        )
        self.assertIs(
            inspect.signature(ApprovalService.__init__)
            .parameters["authorizer"]
            .default,
            DENY_ALL_AUTHORIZER,
        )
        with self.assertRaises(ApproverDenied):
            default_service.review(
                "specialist",
                self.plan_handle,
                self.plan_digest,
                None,
            )
        with self.assertRaises(ApproverDenied):
            default_service.decide(
                "specialist",
                review.review_handle,
                self.metadata_source.plans[plan_handle].plan_digest,
                "granted",
                "approved",
                "anchor-1",
                "decide-default",
            )
        with self.assertRaises(ApproverDenied):
            default_service.revoke(
                "admin", approval_handle, "revoked_by_admin"
            )
        with self.assertRaises(ApproverDenied):
            default_service.review_get("specialist", review.review_handle)
        # default consumer allowlist is EMPTY: no uid can bind
        with self.assertRaises(HostControlError):
            default_service.validate_and_bind(
                CONSUMER_UID,
                approval_handle,
                self.plan_handle,
                self.plan_digest,
                self.next_handle(),
            )
        # nothing above mutated any state
        self.assertEqual(self.grant_row(approval_handle)["state"], "issued")
        self.assertEqual(
            self.review_row(review.review_handle)["state"], "open"
        )

    def test_default_handle_factory_mints_unique_wellformed_random_hex(self) -> None:
        # L11: the deterministic factory is test-injection; the DEFAULT is
        # real randomness.
        pattern = re.compile(r"[0-9a-f]{64}\Z")
        minted = [default_handle_factory() for _ in range(128)]
        self.assertEqual(len(set(minted)), 128)
        for handle in minted:
            self.assertIsNotNone(pattern.fullmatch(handle))


class ConsumerContractTests(unittest.TestCase):
    """The REAL approval service driving the REAL broker OperationService.

    Local minimal broker fixture (PO disposition OQ7: no import of the
    frozen AB-1 test module's case base; ``_manifest_dict`` is reused from
    ``tools.test_host_control_core`` — the same fixture-data import the
    landed operations tests already make).
    """

    BROKER_UID = 1003

    def setUp(self) -> None:
        import yaml

        from tools.host_control.journal import BrokerJournal
        from tools.host_control.operations import OperationService
        from tools.host_control.planner import HostPlanService
        from tools.host_control.policy import HostPolicy
        from tools.host_control.registry import SeatRegistry
        from tools.host_control.trusted_files import RootOwnedFileLoader
        from tools.persona_installer import Manifest, PersonaInstaller
        from tools.test_host_control_core import _manifest_dict

        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        manifest_path = self.root / "instance.yaml"
        manifest_path.write_text(
            yaml.safe_dump(_manifest_dict(), sort_keys=False),
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)
        installer = PersonaInstaller(REPO_ROOT)
        prototype = installer.plan(
            Manifest.load(manifest_path),
            "recovery",
            harness_override="codex",
            root=Path("/projection"),
        )
        registry = {
            "schema": "pantheon-host-seat-registry/v1",
            "seats": [
                {
                    "seat_id": "consultant",
                    "uid": self.BROKER_UID,
                    "persona": "consultant",
                    "role": "worker",
                    "instance": "test-instance",
                    "account": "codex",
                    "harness": "codex",
                    "install_root": "/home/codex",
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
                    "path": str(manifest_path),
                    "sha256": hashlib.sha256(
                        manifest_path.read_bytes()
                    ).hexdigest(),
                    "framework_revision": "test-rev",
                    "framework_sha256": prototype.provenance[
                        "source_artifact_sha256"
                    ],
                    "template_ids": [
                        "tools/systemd/pantheon-persona-consume@.service",
                        "tools/systemd/pantheon-persona-consume@.timer",
                        "tools/systemd/pantheon-persona-quota@.service",
                        "tools/systemd/pantheon-persona-quota@.timer",
                    ],
                }
            ],
        }
        # T-5533: the broker reads the root-registered plan, not an installer.
        from tools._host_control_plan_fixtures import framework_overlay, register_plan

        register_plan(registry, self.root, prototype, seat_id="recovery", manifest_revision="manifest-r1")
        policy = {
            "schema": "pantheon-host-policy/v1",
            "revision": "policy-r1",
            "grants": [
                {
                    "subject_seat_id": "consultant",
                    "action": "plan",
                    "target_seat_id": "recovery",
                    "manifest_revisions": ["manifest-r1"],
                    "operation_families": ["persona.install"],
                    "approval_class": "human",
                }
            ],
        }
        registry_path = self.root / "registry.json"
        policy_path = self.root / "policy.json"
        for path, value in (
            (registry_path, registry),
            (policy_path, policy),
        ):
            path.write_text(
                json.dumps(value, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            path.chmod(0o600)
        self.files = RootOwnedFileLoader._open_for_test(
            (self.root,),
            expected_owner_uid=os.getuid(),
        )
        self.broker_journal = BrokerJournal.open(
            self.root / "broker.sqlite3"
        )
        self.handle_counter = itertools.count(0x1000)
        planner = HostPlanService(
            registry=SeatRegistry.load(
                registry_path,
                files=self.files,
                # T-5533 review r1 B1: the broker plans against a framework overlay.
                framework_root=framework_overlay(
                    self.root / "framework", REPO_ROOT, tuple(registry["manifests"][0]["template_ids"])
                ),
            ),
            policy=HostPolicy.load(policy_path, files=self.files),
            journal=self.broker_journal,
            clock=lambda: FIXED_NOW,
            handle_factory=self.next_handle,
            plan_ttl=timedelta(hours=1),
            contract_version="org.pantheon.HostBroker1/v1",
            broker_revision="unit-test",
            host_operation_families=frozenset(("persona.install",)),
            health_check=lambda: "ready",
        )
        self.plan_result = planner.plan(
            self.BROKER_UID,
            "recovery",
            "manifest-r1",
            "consumer-contract-plan",
        )
        # the physically separate approval side
        self.approval_journal = ApprovalJournal.open(
            self.root / "approval.sqlite3"
        )
        self.metadata_source = FakeMetadataSource()
        self.metadata_source.plans[self.plan_result.plan_handle] = (
            PlanMetadata(
                owner="consultant",
                performer="consultant",
                target_seat_id="recovery",
                operation_families=("persona.install",),
                plan_digest=self.plan_result.plan_digest,
                effect_digest=self.plan_result.effect_digest,
                manifest_revision="manifest-r1",
                policy_revision="policy-r1",
            )
        )
        self.approval_service = ApprovalService(
            journal=self.approval_journal,
            metadata_source=self.metadata_source,
            clock=lambda: FIXED_NOW,
            authorizer=PermissiveAuthorizer(),
            handle_factory=self.next_handle,
            registered_consumer_uids=frozenset((self.BROKER_UID,)),
        )

        class NeverReconcile:
            def reconcile(self, operation, staged_receipt, intents):
                raise AssertionError("reconciliation is out of scope here")

        self.operation_service = OperationService(
            registry=SeatRegistry.load(registry_path, files=self.files),
            journal=self.broker_journal,
            binder=self.approval_service.as_binder(self.BROKER_UID),
            reconciler=NeverReconcile(),
            clock=lambda: FIXED_NOW,
            handle_factory=self.next_handle,
        )

    def tearDown(self) -> None:
        self.approval_journal.close()
        self.broker_journal.close()
        self.files.close()
        self.temp.cleanup()

    def next_handle(self) -> str:
        return f"{next(self.handle_counter):064x}"

    def _granted_approval_handle(self) -> str:
        review = self.approval_service.review(
            "specialist",
            self.plan_result.plan_handle,
            self.plan_result.plan_digest,
            None,
        ).record
        result = self.approval_service.decide(
            "specialist",
            review.review_handle,
            self.plan_result.plan_digest,
            "granted",
            "approved",
            "anchor-cc",
            "decide-cc-1",
        )
        return result.approval_handle

    def test_real_approval_service_authorizes_real_operation_service(self) -> None:
        # End to end across the REAL boundary: the broker consumes the real
        # approval service purely through the landed ApprovalBinder seam;
        # neither side holds the other's journal.
        approval_handle = self._granted_approval_handle()
        record, replayed = self.operation_service.prepare_apply(
            self.BROKER_UID,
            self.plan_result.plan_handle,
            self.plan_result.plan_digest,
            "op-cc-1",
            approval_handle,
        )
        self.assertFalse(replayed)
        authorized = self.operation_service.authorize(
            record.operation_handle
        )
        self.assertEqual(authorized.state, "authorized")
        self.assertIsNotNone(authorized.authority_record_id)
        # gate 26 sliver through the REAL recovery path: authority BOUND
        # with broker PREPARED — recover() retries the identical tuple and
        # lands the ORIGINAL authority record id.
        second_approval = self._reissue_for_second_operation()
        prepared, _ = self.operation_service.prepare_apply(
            self.BROKER_UID,
            self.plan_result.plan_handle,
            self.plan_result.plan_digest,
            "op-cc-2",
            second_approval,
        )
        direct = self.approval_service.validate_and_bind(
            self.BROKER_UID,
            second_approval,
            prepared.plan_handle,
            prepared.plan_digest,
            prepared.operation_handle,
        )
        self.assertEqual(direct.state, "bound")
        outcome = self.operation_service.recover(prepared.operation_handle)
        self.assertEqual(outcome.action, "authorized")
        self.assertEqual(
            outcome.record.authority_record_id,
            direct.authority_record_id,
        )

    def _reissue_for_second_operation(self) -> str:
        review = self.approval_service.review(
            "mason",
            self.plan_result.plan_handle,
            self.plan_result.plan_digest,
            None,
        ).record
        result = self.approval_service.decide(
            "mason",
            review.review_handle,
            self.plan_result.plan_digest,
            "granted",
            "approved",
            "anchor-cc2",
            "decide-cc-2",
        )
        return result.approval_handle


if __name__ == "__main__":
    unittest.main()
