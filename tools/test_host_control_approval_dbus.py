"""Tests for ORG-052 Unit 3b — the ``org.pantheon.HostApproval1`` D-Bus
service (T-2913).

Layout mirrors ``tools/test_host_control_dbus.py`` (T-2912):

- Unguarded tests (wire constants, error-namespace totality, identity
  resolution, wire serializations, staged-artifact parses, structure)
  run on any interpreter — no dbus import, so they can never silently
  skip on a dbus-less runner.
- dbus-guarded in-process tests are decorated with ``skipUnless``; on
  the canonical host dbus-python IS importable under system python3, so
  a skip there means the guard broke, not that the environment is
  missing.
- Private-bus integration tests spawn one dbus-daemon plus one approval
  service subprocess per TestCase class (never the real system bus).

The two headline security properties this unit must PROVE (ticket Ask):

- a requester-only uid calling Reviewer methods directly is DENIED —
  ``RequesterOnlyUidBusTests`` (ORG-052 gate 5, server half);
- a second process under the same registered uid CANNOT approve that
  uid's own plan — ``SelfApprovalOwnerBusTests`` +
  ``SelfApprovalTrustDomainBusTests`` (gate 11).
"""

from __future__ import annotations

import ast
import inspect
import json
import sys
import unittest
from pathlib import Path

from tools.host_control import wire as broker_wire
from tools.host_control.approval import identity
from tools.host_control.approval import wire
from tools.host_control.approval.contracts import (
    BIND_DENIAL_REASON_CODES,
    GRANT_SCHEMA,
    REVIEW_SCHEMA,
    ApprovalStatus,
    ApproverDenied,
    HandoffLink,
    HandoffMismatch,
    MetadataUnavailable,
    ReviewRecord,
)
from tools.host_control.approval.policy import (
    InvalidApproverPolicy,
    parse_approver_policy,
)
from tools.host_control.contracts import (
    BindOutcome,
    HostControlError,
    IdempotencyConflict,
    InvalidTransition,
    JournalBusy,
    PolicyDenied,
    RegistryError,
    SubjectMismatch,
    UnknownHandle,
    canonical_json,
    resolve_error_name,
)
from tools.test_host_control_approval_dbus_driver import (
    PLAN_DIGEST,
    PLAN_HANDLE,
    FakeApprovalMetadataSource,
    approver_entry,
    approver_policy_raw,
    build_approval_domain,
    plan_metadata_fixture,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOST_CONTROL = REPO_ROOT / "tools" / "host_control"
APPROVAL_DIR = HOST_CONTROL / "approval"


class ApprovalWireConstantsTests(unittest.TestCase):
    """The service triple and interface names, transcribed independently
    from ORG-052 §5 (deliberately not compared against any production
    constant elsewhere — drift in wire.py cannot self-certify)."""

    def test_wire_constants_match_the_ratified_names(self) -> None:
        self.assertEqual(wire.BUS_NAME, "org.pantheon.HostApproval1")
        self.assertEqual(wire.OBJECT_PATH, "/org/pantheon/HostApproval1")
        self.assertEqual(
            wire.REVIEWER_INTERFACE,
            "org.pantheon.HostApproval1.Reviewer",
        )
        self.assertEqual(
            wire.CONSUMER_INTERFACE,
            "org.pantheon.HostApproval1.Consumer",
        )
        self.assertEqual(
            wire.HUMAN_INGRESS_INTERFACE,
            "org.pantheon.HostApproval1.HumanIngress",
        )
        self.assertEqual(
            wire.ERROR_NAMESPACE,
            "org.pantheon.HostApproval1.Error",
        )


class SharedErrorResolverTests(unittest.TestCase):
    """The extracted nearest-ancestor resolver (T-2913 OQ2 disposition):
    one resolver shared by both wire modules, so neither service's
    mapping can silently stop being TOTAL through drift."""

    def test_nearest_ancestor_resolution(self) -> None:
        mapping = {IdempotencyConflict: "conflict", HostControlError: "failed"}
        self.assertEqual(
            resolve_error_name(IdempotencyConflict("x"), mapping, "internal"),
            "conflict",
        )
        # an unmapped subclass resolves via its closest mapped ancestor
        self.assertEqual(
            resolve_error_name(UnknownHandle("x"), mapping, "internal"),
            "failed",
        )

    def test_runtime_minted_subclass_resolves_via_its_ancestor(self) -> None:
        # PO-required falsification test for the shared resolver: a
        # subclass minted AFTER the mapping was written still resolves.
        mapping = {IdempotencyConflict: "conflict", HostControlError: "failed"}
        fresh = type("FreshConflict", (IdempotencyConflict,), {})
        self.assertEqual(
            resolve_error_name(fresh("x"), mapping, "internal"),
            "conflict",
        )

    def test_fallback_for_exceptions_outside_the_hierarchy(self) -> None:
        mapping = {HostControlError: "failed"}
        for exc in (TypeError("x"), ValueError("x"), Exception("x")):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(
                    resolve_error_name(exc, mapping, "internal"),
                    "internal",
                )

    def test_broker_wire_delegates_without_behavior_change(self) -> None:
        # Behavior preservation for the wire.py delegation hunk; the
        # landed broker ErrorNamespaceTests remain the authoritative
        # proof and must stay green.
        for exc in (
            PolicyDenied("subject_denied"),
            UnknownHandle("boom"),
            HostControlError("boom"),
            RuntimeError("boom"),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(
                    broker_wire.error_name_for(exc),
                    resolve_error_name(
                        exc,
                        broker_wire._ERROR_NAME_BY_TYPE,
                        broker_wire.INTERNAL_ERROR,
                    ),
                )


class ApprovalErrorNamespaceTests(unittest.TestCase):
    """The approval service's OWN closed, versioned error namespace.

    Never a broker error name (design constraint): the disjointness test
    pins that structurally.
    """

    def _hierarchy(self) -> set[type]:
        found: set[type] = {HostControlError}
        frontier = [HostControlError]
        while frontier:
            klass = frontier.pop()
            for child in klass.__subclasses__():
                if child not in found:
                    found.add(child)
                    frontier.append(child)
        return found

    def _construct_for_totality(self, klass: type) -> HostControlError:
        """Construct one instance of any HostControlError subclass.

        Same structural shape as the T-2912 prober: a required
        keyword-only argument is satisfied by probing the closed
        ``*_REASON_CODES`` frozensets of the class's own defining
        module, never by special-casing the class name.
        """
        if klass is PolicyDenied:
            return klass("subject_denied")
        try:
            return klass("boom")
        except TypeError:
            pass
        signature = inspect.signature(klass.__init__)
        required = [
            name
            for name, param in signature.parameters.items()
            if param.kind is inspect.Parameter.KEYWORD_ONLY
            and param.default is inspect.Parameter.empty
        ]
        self.assertTrue(
            required,
            f"{klass.__name__} rejected a bare message for a reason "
            "other than a missing required keyword-only argument",
        )
        closed_sets = [
            value
            for name, value in vars(sys.modules[klass.__module__]).items()
            if name.endswith("REASON_CODES") and isinstance(value, frozenset)
        ]
        for candidate_set in closed_sets:
            for candidate in sorted(candidate_set):
                try:
                    return klass("boom", **{n: candidate for n in required})
                except HostControlError:
                    continue
        self.fail(
            f"{klass.__name__} requires keyword-only {required} but no "
            f"closed reason-code set in {klass.__module__} satisfies it"
        )

    def test_closed_error_namespace_is_versioned_and_exact(self) -> None:
        expected = frozenset(
            f"org.pantheon.HostApproval1.Error.{suffix}"
            for suffix in (
                "ApproverDenied",
                "HandoffMismatch",
                "PolicyDenied",
                "UnknownHandle",
                "IdempotencyConflict",
                "InvalidTransition",
                "MetadataUnavailable",
                "RegistryDenied",
                "BindDenied",
                "Failed",
                "Unsupported",
                "Internal",
            )
        )
        self.assertEqual(wire.CLOSED_ERROR_NAMES, expected)
        for name in wire.CLOSED_ERROR_NAMES:
            self.assertTrue(
                name.startswith("org.pantheon.HostApproval1.Error."),
                name,
            )

    def test_namespace_is_disjoint_from_the_broker_namespace(self) -> None:
        # The approval service must never emit a broker error name.
        self.assertEqual(
            wire.CLOSED_ERROR_NAMES & broker_wire.CLOSED_ERROR_NAMES,
            frozenset(),
        )

    def test_each_mapped_class_resolves_to_its_own_exact_name(self) -> None:
        # Exact pins, not closed-set membership: dropping a subclass
        # mapping silently degrades it to Failed via ancestor fallback
        # (the AB-4 probe-iii lesson).
        expected = {
            ApproverDenied: wire.APPROVER_DENIED_ERROR,
            HandoffMismatch: wire.HANDOFF_MISMATCH_ERROR,
            PolicyDenied: wire.POLICY_DENIED_ERROR,
            UnknownHandle: wire.UNKNOWN_HANDLE_ERROR,
            IdempotencyConflict: wire.IDEMPOTENCY_CONFLICT_ERROR,
            InvalidTransition: wire.INVALID_TRANSITION_ERROR,
            MetadataUnavailable: wire.METADATA_UNAVAILABLE_ERROR,
            RegistryError: wire.REGISTRY_DENIED_ERROR,
            HostControlError: wire.FAILED_ERROR,
        }
        for klass, name in expected.items():
            with self.subTest(klass=klass.__name__):
                instance = self._construct_for_totality(klass)
                self.assertEqual(wire.error_name_for(instance), name)
        # no two mapped classes share one name
        self.assertEqual(len(set(expected.values())), len(expected))

    def test_deliberate_ancestor_fallbacks_resolve_to_failed(self) -> None:
        # These classes are DELIBERATELY unmapped (wire.py docstring):
        # StaleAuthorityState / OperationConsumed are absorbed by the
        # service as BindOutcome values and are never expected on the
        # wire; InvalidApproverPolicy is a config-load fault that
        # precedes any call; SubjectMismatch is broker-domain and no
        # approval path raises it. Pinning them keeps the choice tested
        # rather than accidental.
        from tools.host_control.approval.contracts import (
            OperationConsumed,
            StaleAuthorityState,
        )

        for klass in (
            StaleAuthorityState,
            OperationConsumed,
            InvalidApproverPolicy,
            SubjectMismatch,
        ):
            with self.subTest(klass=klass.__name__):
                self.assertEqual(
                    wire.error_name_for(klass("boom")),
                    wire.FAILED_ERROR,
                )

    def test_journal_busy_resolves_to_failed_via_its_ancestor(self) -> None:
        # T-4334: journal write-lock contention is a retryable refusal at
        # the journal boundary, deliberately NOT a new wire name (the
        # closed namespace is ratified): it resolves through HostControlError
        # to Failed, and its server-authored message is the bounded body.
        exc = JournalBusy(
            "transition_grant[revoked]: journal write lock busy after "
            "5000 ms (lock contention); nothing was written; retry later"
        )
        self.assertEqual(wire.error_name_for(exc), wire.FAILED_ERROR)
        body = wire.error_body_for(exc)
        self.assertEqual(body, str(exc))
        self.assertLessEqual(len(body), 200)

    def test_error_mapping_is_total_over_the_live_exception_hierarchy(
        self,
    ) -> None:
        hierarchy = self._hierarchy()
        # broker + approval classes are all live in this interpreter
        self.assertGreaterEqual(len(hierarchy), 15)
        for klass in hierarchy:
            with self.subTest(klass=klass.__name__):
                instance = self._construct_for_totality(klass)
                name = wire.error_name_for(instance)
                self.assertIn(
                    name,
                    wire.CLOSED_ERROR_NAMES,
                    f"{klass.__name__} resolved outside the closed set",
                )
                self.assertEqual(name, wire.error_name_for(instance))
        # a NEW subclass minted at runtime inherits its ancestor's name
        fresh = type("FreshDenial", (ApproverDenied,), {})
        self.assertEqual(
            wire.error_name_for(fresh("boom")),
            wire.APPROVER_DENIED_ERROR,
        )

    def test_unmapped_exceptions_become_bounded_internal_error(self) -> None:
        secret = "/etc/shadow token=hunter2 " + "x" * 4096
        for exc in (
            TypeError(secret),
            ValueError(secret),
            RuntimeError(secret),
            Exception(secret),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(wire.error_name_for(exc), wire.INTERNAL_ERROR)
                body = wire.error_body_for(exc)
                self.assertEqual(body, "internal error")
                self.assertNotIn("hunter2", body)
                self.assertNotIn("/etc/shadow", body)

    def test_reason_code_bearing_bodies_are_the_code_only(self) -> None:
        cases = (
            (PolicyDenied("subject_denied"), "subject_denied"),
            (
                ApproverDenied(
                    "approver may not review this plan",
                    reason_code="approver_shares_trust_domain",
                ),
                "approver_shares_trust_domain",
            ),
            (
                HandoffMismatch(
                    "handoff target does not match",
                    reason_code="handoff_target_mismatch",
                ),
                "handoff_target_mismatch",
            ),
        )
        for exc, body in cases:
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(wire.error_body_for(exc), body)
                # the human-authored message never crosses the wire
                self.assertNotIn("may not", wire.error_body_for(exc))

    def test_registry_faults_carry_a_fixed_non_disclosing_body(self) -> None:
        exc = RegistryError("policy path is outside trusted roots /root/x")
        self.assertEqual(wire.error_name_for(exc), wire.REGISTRY_DENIED_ERROR)
        body = wire.error_body_for(exc)
        self.assertEqual(body, "registry refused the request")
        self.assertNotIn("/root", body)

    def test_domain_error_bodies_are_bounded(self) -> None:
        exc = HostControlError("y" * 4096)
        self.assertEqual(wire.error_name_for(exc), wire.FAILED_ERROR)
        self.assertLessEqual(len(wire.error_body_for(exc)), 200)


class ApproverIdentityResolutionTests(unittest.TestCase):
    """uid -> exactly-one-approver-roster-subject, else refuse (fail
    closed). The roster intersection is the T-2913 plan refinement: a
    uid also carrying non-approver subject labels is NOT ambiguous."""

    def _resolver(self, raw: dict) -> identity.ApproverResolver:
        return identity.ApproverResolver(parse_approver_policy(raw))

    def test_exactly_one_approver_subject_resolves(self) -> None:
        resolver = self._resolver(
            approver_policy_raw(
                subjects={"specialist": 1001, "alpha": 2002},
                approvers={"specialist": approver_entry()},
            )
        )
        self.assertEqual(resolver.subject_for_uid(1001), "specialist")

    def test_unknown_uid_refuses(self) -> None:
        resolver = self._resolver(
            approver_policy_raw(
                subjects={"specialist": 1001},
                approvers={"specialist": approver_entry()},
            )
        )
        self.assertIsNone(resolver.subject_for_uid(4242))

    def test_uid_of_a_non_approver_subject_refuses(self) -> None:
        # the literal requester-only uid: registered as a subject, no
        # approver standing
        resolver = self._resolver(
            approver_policy_raw(
                subjects={"specialist": 1001, "alpha": 2002},
                approvers={"specialist": approver_entry()},
            )
        )
        self.assertIsNone(resolver.subject_for_uid(2002))

    def test_two_approver_subjects_on_one_uid_refuse_as_ambiguous(
        self,
    ) -> None:
        resolver = self._resolver(
            approver_policy_raw(
                subjects={"specialist": 1001, "mason": 1001},
                approvers={
                    "specialist": approver_entry(),
                    "mason": approver_entry(),
                },
            )
        )
        self.assertIsNone(resolver.subject_for_uid(1001))

    def test_approver_plus_non_approver_label_on_one_uid_resolves(
        self,
    ) -> None:
        # The refinement: the non-approver label grants no standing and
        # takes none away; the uid-level trust-domain separation at the
        # policy tier still evaluates the shared uid.
        resolver = self._resolver(
            approver_policy_raw(
                subjects={"specialist": 1001, "specialist-worker": 1001},
                approvers={"specialist": approver_entry()},
            )
        )
        self.assertEqual(resolver.subject_for_uid(1001), "specialist")

    def test_malformed_uids_refuse(self) -> None:
        resolver = self._resolver(
            approver_policy_raw(
                subjects={"specialist": 1001},
                approvers={"specialist": approver_entry()},
            )
        )
        for bad in (True, False, -1, "1001", None, 1001.0):
            with self.subTest(uid=repr(bad)):
                self.assertIsNone(resolver.subject_for_uid(bad))


class BindWireHelperTests(unittest.TestCase):
    """Value-level ValidateAndBind wire mapping (OQ3 disposition):
    success returns the two ratified slots; denial maps to the closed
    BindDenied error whose body is the closed denial reason code."""

    def test_success_slots_for_bound_and_replay_are_identical(self) -> None:
        # Gate 16: identical retry returns the ORIGINAL binding — the
        # wire shape for first-bind and replay is byte-identical, so a
        # caller cannot distinguish them (deliberate, per the OQ3(b)
        # disposition).
        record_id = "c" * 64
        first = wire.bind_success_slots(
            BindOutcome("bound", record_id, "bound")
        )
        replay = wire.bind_success_slots(
            BindOutcome("bound", record_id, "bound_replay")
        )
        self.assertEqual(first, ("bound", record_id))
        self.assertEqual(first, replay)

    def test_denial_reason_mapping_is_total_over_the_live_closed_set(
        self,
    ) -> None:
        # PO-required totality: iterate the LIVE module-level frozenset,
        # never a hand-copied list — a denial code added later cannot
        # silently fall through.
        for code in sorted(BIND_DENIAL_REASON_CODES):
            with self.subTest(code=code):
                self.assertEqual(
                    wire.bind_denial_reason(BindOutcome("denied", None, code)),
                    code,
                )

    def test_helpers_refuse_the_wrong_outcome_shape(self) -> None:
        with self.assertRaises(HostControlError):
            wire.bind_success_slots(
                BindOutcome("denied", None, "digest_mismatch")
            )
        with self.assertRaises(HostControlError):
            wire.bind_denial_reason(BindOutcome("bound", "c" * 64, "bound"))
        with self.assertRaises(HostControlError):
            wire.bind_denial_reason(
                BindOutcome("denied", None, "not_a_registered_code")
            )
        with self.assertRaises(HostControlError):
            # a bound outcome must carry a well-formed authority id
            wire.bind_success_slots(BindOutcome("bound", None, "bound"))


SENTINEL_DECIDE_KEY = "sentinel-decide-key-73x"
SENTINEL_SOURCE_EVENT = "sentinel-source-event-73x"


def _decided_review_with_sentinels() -> ReviewRecord:
    """A granted handoff review whose EXCLUDED fields carry sentinels.

    The anti-vacuity control (OQ4 disposition): every excluded field is
    POPULATED so the exclusion assertions cannot pass vacuously.
    ``authority_source='human'`` is the only shape that may carry a
    ``source_event`` sentinel (agent decisions structurally refuse one).
    """

    handoff = HandoffLink(
        request_plan_handle="d" * 64,
        request_plan_digest="e" * 64,
        request_plan_owner="alpha",
        shared_effect_digest="f" * 64,
        handoff_anchor="ticket:T-0000",
    )
    return ReviewRecord.create(
        schema=REVIEW_SCHEMA,
        review_handle="1" * 64,
        plan_handle="2" * 64,
        plan_digest="3" * 64,
        plan_owner="bravo",
        performer="bravo",
        target_seat_id="recovery",
        operation_families=("persona.install",),
        effect_digest="f" * 64,
        manifest_revision="manifest-r1",
        policy_revision="policy-r1",
        handoff_kind="request_plan",
        handoff=handoff,
        requested_by="specialist",
        state="granted",
        decision="granted",
        reason_code="approved",
        approver_subject="specialist",
        authority_source="human",
        audit_anchor="ticket:T-0001",
        source_event=SENTINEL_SOURCE_EVENT,
        decide_idempotency_key=SENTINEL_DECIDE_KEY,
        created_at="2026-08-12T00:00:00+00:00",
        expires_at="2026-08-12T01:00:00+00:00",
        decided_at="2026-08-12T00:30:00+00:00",
    )


REVIEW_SUMMARY_KEYS = {
    "schema",
    "review_handle",
    "plan_handle",
    "plan_digest",
    "plan_owner",
    "performer",
    "target_seat_id",
    "operation_families",
    "effect_digest",
    "manifest_revision",
    "policy_revision",
    "handoff_kind",
    "handoff",
    "requested_by",
    "state",
    "decision",
    "reason_code",
    "approver_subject",
    "authority_source",
    "audit_anchor",
    "created_at",
    "expires_at",
    "decided_at",
    # T-2919 Q3 (additive, provisional v1)
    "approval_handle",
    "human_request",
}
HANDOFF_SUMMARY_KEYS = {
    "request_plan_handle",
    "request_plan_digest",
    "request_plan_owner",
    "shared_effect_digest",
    "handoff_anchor",
}


class ReviewSummaryJsonTests(unittest.TestCase):
    """The minted ``ReviewGet`` summary serialization (OQ4): canonical,
    schema-versioned, bounded, exact closed key set, record-derived
    only — it never re-discloses plan effects."""

    def test_summary_is_canonical_schema_versioned_and_closed(self) -> None:
        record = _decided_review_with_sentinels()
        encoded = wire.review_summary_json(record)
        raw = json.loads(encoded)
        self.assertEqual(encoded, canonical_json(raw))
        self.assertEqual(raw["schema"], "pantheon-approval-review-summary/v1")
        self.assertEqual(set(raw), REVIEW_SUMMARY_KEYS)
        self.assertEqual(set(raw["handoff"]), HANDOFF_SUMMARY_KEYS)
        self.assertEqual(raw["state"], "granted")
        self.assertEqual(raw["operation_families"], ["persona.install"])
        self.assertLessEqual(len(encoded.encode("utf-8")), 65536)

    def test_excluded_fields_carry_populated_sentinels_that_never_leak(
        self,
    ) -> None:
        # Anti-vacuity (the arc's five-times-caught shape): the excluded
        # fields are POPULATED with sentinels, then proven absent from
        # the serialized bytes — exclusion is observable, not vacuous.
        record = _decided_review_with_sentinels()
        self.assertEqual(record.decide_idempotency_key, SENTINEL_DECIDE_KEY)
        self.assertEqual(record.source_event, SENTINEL_SOURCE_EVENT)
        # the record-integrity digest is a computed 64-hex distinct from
        # every disclosed digest — a natural sentinel
        self.assertNotEqual(record.digest, record.plan_digest)
        self.assertNotEqual(record.digest, record.effect_digest)
        encoded = wire.review_summary_json(record)
        self.assertNotIn(SENTINEL_DECIDE_KEY, encoded)
        self.assertNotIn(SENTINEL_SOURCE_EVENT, encoded)
        self.assertNotIn(record.digest, encoded)

    def test_direct_review_serializes_a_null_handoff(self) -> None:
        record = ReviewRecord.create(
            schema=REVIEW_SCHEMA,
            review_handle="1" * 64,
            plan_handle="2" * 64,
            plan_digest="3" * 64,
            plan_owner="bravo",
            performer="bravo",
            target_seat_id="recovery",
            operation_families=("persona.install",),
            effect_digest="f" * 64,
            manifest_revision="manifest-r1",
            policy_revision="policy-r1",
            handoff_kind="direct_no_handoff",
            handoff=None,
            requested_by="specialist",
            state="open",
            decision=None,
            reason_code=None,
            approver_subject=None,
            authority_source=None,
            audit_anchor=None,
            source_event=None,
            decide_idempotency_key=None,
            created_at="2026-08-12T00:00:00+00:00",
            expires_at="2026-08-12T01:00:00+00:00",
            decided_at=None,
        )
        raw = json.loads(wire.review_summary_json(record))
        self.assertIsNone(raw["handoff"])
        self.assertEqual(raw["state"], "open")
        self.assertIsNone(raw["decision"])


class ApprovalStatusJsonTests(unittest.TestCase):
    """The minted ``ApprovalStatus`` payload (OQ4). The input dataclass
    carries no nonce and no authority-record id BY CONSTRUCTION (gate
    21), so the closed key set is the whole exclusion story here."""

    def test_status_json_is_canonical_schema_versioned_and_closed(
        self,
    ) -> None:
        status = ApprovalStatus(
            approval_handle="a" * 64,
            state="issued",
            expires_at="2026-08-12T01:00:00+00:00",
            summary="grant issued; target=recovery; families=persona.install",
        )
        encoded = wire.approval_status_json(status)
        raw = json.loads(encoded)
        self.assertEqual(encoded, canonical_json(raw))
        self.assertEqual(raw["schema"], "pantheon-approval-status/v1")
        self.assertEqual(
            set(raw),
            {"schema", "approval_handle", "state", "expires_at", "summary"},
        )
        self.assertEqual(raw["state"], "issued")
        self.assertLessEqual(len(encoded.encode("utf-8")), 65536)


class SummaryBoundAlignmentTests(unittest.TestCase):
    """The falsification device for the T-2913 summary-bound finding
    (PO condition 4 on the authorized fix): the domain's plan-summary
    bound must never be tighter than the wire-tier JSON limits of the
    contract it consumes from (the broker produces the summary) and
    serves on (this service's own wire). Re-tightening the constant
    fails HERE, not in production — the original 1024 refused every
    real broker summary."""

    def test_domain_summary_bound_is_not_tighter_than_the_wire_limits(
        self,
    ) -> None:
        from tools.host_control.approval.contracts import (
            MAX_PLAN_SUMMARY_LENGTH,
        )

        # producer side: the broker's wire tier bounds every JSON payload
        self.assertGreaterEqual(
            MAX_PLAN_SUMMARY_LENGTH,
            broker_wire._JSON_LIMIT,
        )
        # consumer side: this service's own wire tier
        self.assertGreaterEqual(MAX_PLAN_SUMMARY_LENGTH, wire._JSON_LIMIT)


def _module_import_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    return names


def _guarded_dbus_imports_only(path: Path) -> bool:
    """True when every ``import dbus`` in the file sits inside a Try."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    guarded_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for child in ast.walk(node):
                guarded_nodes.add(id(child))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "dbus" or alias.name.startswith("dbus."):
                    if id(node) not in guarded_nodes:
                        return False
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "dbus" or module.startswith("dbus."):
                if id(node) not in guarded_nodes:
                    return False
    return True


class ApprovalModuleStructureTests(unittest.TestCase):
    """Structural guards over the new approval wire tier (gate 18
    approval half + the guarded-import discipline). The landed
    ``SeparationTests`` in test_host_control_approval.py glob-scan the
    whole package for broker-module imports; these tests add the
    dbus-placement rules that scan does not know about."""

    DBUS_FREE = ("wire.py", "identity.py")

    def test_wire_and_identity_import_no_dbus_at_all(self) -> None:
        for name in self.DBUS_FREE:
            with self.subTest(module=name):
                imports = _module_import_names(APPROVAL_DIR / name)
                self.assertFalse(
                    any(
                        item == "dbus" or item.startswith("dbus.")
                        for item in imports
                    ),
                    f"{name} imports dbus",
                )

    def test_dbus_bearing_modules_guard_every_dbus_import(self) -> None:
        # The AB-4/T-2912 split: dbus-less runners must import these
        # safely, so the contract tests can never silently skip.
        for name in ("dbus_service.py", "broker_client.py"):
            with self.subTest(module=name):
                self.assertTrue(
                    _guarded_dbus_imports_only(APPROVAL_DIR / name),
                    f"{name} has an unguarded dbus import",
                )

    def test_broker_wire_import_is_confined_to_the_broker_client(
        self,
    ) -> None:
        # A client of the broker's wire contract must name it; nothing
        # else in the approval tree may (placement pin — the deliberate
        # exception stays exactly one module wide).
        for path in sorted(APPROVAL_DIR.glob("*.py")):
            imports = _module_import_names(path)
            with self.subTest(module=path.name):
                if path.name == "broker_client.py":
                    self.assertIn("tools.host_control.wire", imports)
                else:
                    self.assertNotIn("tools.host_control.wire", imports)


class BrokerClientConversionTests(unittest.TestCase):
    """The PURE half of the ApprovalSource client (OQ5 split): wire
    tuple -> dataclass conversion, falsifiable without dbus."""

    VALID_SLOTS = (
        "alpha",
        "alpha",
        "recovery",
        "persona.install,persona.verify",
        "3" * 64,
        "f" * 64,
        "manifest-r1",
        "policy-r1",
    )

    def test_valid_slots_convert_exactly(self) -> None:
        from tools.host_control.approval.broker_client import (
            plan_metadata_from_wire,
        )

        metadata = plan_metadata_from_wire(self.VALID_SLOTS)
        self.assertEqual(metadata.owner, "alpha")
        self.assertEqual(metadata.performer, "alpha")
        self.assertEqual(metadata.target_seat_id, "recovery")
        self.assertEqual(
            metadata.operation_families,
            ("persona.install", "persona.verify"),
        )
        self.assertEqual(metadata.plan_digest, "3" * 64)
        self.assertEqual(metadata.effect_digest, "f" * 64)
        self.assertEqual(metadata.manifest_revision, "manifest-r1")
        self.assertEqual(metadata.policy_revision, "policy-r1")

    def test_malformed_replies_fail_closed_as_unavailable(self) -> None:
        from tools.host_control.approval.broker_client import (
            plan_metadata_from_wire,
            plan_summary_from_wire,
        )

        cases = (
            ("seven slots", self.VALID_SLOTS[:7]),
            ("nine slots", self.VALID_SLOTS + ("extra",)),
            ("empty owner", ("",) + self.VALID_SLOTS[1:]),
            (
                "empty family in list",
                self.VALID_SLOTS[:3]
                + ("persona.install,",)
                + self.VALID_SLOTS[4:],
            ),
        )
        for label, slots in cases:
            with self.subTest(case=label):
                with self.assertRaises(MetadataUnavailable):
                    plan_metadata_from_wire(slots)
        for label, slots in (
            ("one slot", ("3" * 64,)),
            ("three slots", ("3" * 64, "summary", "extra")),
            ("empty digest", ("", "summary")),
            ("empty summary", ("3" * 64, "")),
        ):
            with self.subTest(case=label):
                with self.assertRaises(MetadataUnavailable):
                    plan_summary_from_wire(slots)

    def test_valid_summary_slots_convert(self) -> None:
        from tools.host_control.approval.broker_client import (
            plan_summary_from_wire,
        )

        digest, summary = plan_summary_from_wire(("3" * 64, "{}"))
        self.assertEqual(digest, "3" * 64)
        self.assertEqual(summary, "{}")

    def test_empty_summary_slot_is_treated_as_unavailable_not_valid(
        self,
    ) -> None:
        """Pins the deliberate assumption documented on
        ``plan_summary_from_wire``: an empty summary slot fails closed
        as ``MetadataUnavailable`` rather than being accepted as a
        legitimate empty result. The broker's canonical summary always
        serializes at least a ``schema`` key, so this is untested by
        production traffic — a future broker change that legitimately
        emits an empty summary should trip this test, not silently
        change behavior."""

        from tools.host_control.approval.broker_client import (
            plan_summary_from_wire,
        )

        with self.assertRaises(MetadataUnavailable):
            plan_summary_from_wire(("3" * 64, ""))


class BrokerClientErrorMappingTests(unittest.TestCase):
    """The PURE error-mapping half: only the structured error NAME
    routes; remote messages are dropped entirely."""

    def test_broker_unknown_handle_maps_to_unknown_handle(self) -> None:
        from tools.host_control.approval.broker_client import (
            map_broker_error,
        )

        exc = map_broker_error(
            broker_wire.UNKNOWN_HANDLE_ERROR,
            "unknown plan handle",
        )
        self.assertIsInstance(exc, UnknownHandle)
        # the fixed non-disclosing body, byte-identical to the broker
        # provider's (expired/never-existed/malformed stay
        # indistinguishable through this client)
        self.assertEqual(str(exc), "unknown plan handle")

    def test_every_other_error_maps_to_metadata_unavailable(self) -> None:
        from tools.host_control.approval.broker_client import (
            map_broker_error,
        )

        for name in (
            broker_wire.FAILED_ERROR,
            broker_wire.POLICY_DENIED_ERROR,
            broker_wire.INTERNAL_ERROR,
            "org.freedesktop.DBus.Error.ServiceUnknown",
            "org.freedesktop.DBus.Error.NoReply",
            "org.freedesktop.DBus.Error.Disconnected",
        ):
            with self.subTest(name=name):
                exc = map_broker_error(name, "boom")
                self.assertIsInstance(exc, MetadataUnavailable)

    def test_remote_messages_never_propagate(self) -> None:
        from tools.host_control.approval.broker_client import (
            map_broker_error,
        )

        secret = "secret /etc/shadow token=hunter2"
        for name in (
            broker_wire.UNKNOWN_HANDLE_ERROR,
            broker_wire.FAILED_ERROR,
            "org.freedesktop.DBus.Error.NoReply",
        ):
            with self.subTest(name=name):
                body = str(map_broker_error(name, secret))
                self.assertNotIn("hunter2", body)
                self.assertNotIn("/etc/shadow", body)


# import-safe: dbus guarded inside the module
from tools.host_control.approval import dbus_service as approval_dbus  # noqa: E402

requires_dbus = unittest.skipUnless(
    approval_dbus.DBUS_AVAILABLE,
    "dbus-python not importable — on the canonical host this skip means "
    "the guard is broken, not that the environment is missing",
)

# The ratified inventory, transcribed INDEPENDENTLY from ORG-052 §5
# (deliberately not imported from approval/wire.py, so a drift in the
# production constants cannot self-certify):
# (interface, method) -> (arg names, in, out).
APPROVAL_RATIFIED_INVENTORY = {
    ("org.pantheon.HostApproval1.Reviewer", "Review"): (
        ("plan_handle", "expected_digest", "request_plan_handle"),
        "sss",
        "ssss",
    ),
    ("org.pantheon.HostApproval1.Reviewer", "ReviewGet"): (
        ("review_handle",),
        "s",
        "ss",
    ),
    ("org.pantheon.HostApproval1.Reviewer", "Decide"): (
        (
            "review_handle",
            "expected_digest",
            "decision",
            "reason_code",
            "audit_anchor",
            "idempotency_key",
        ),
        "ssssss",
        "ss",
    ),
    ("org.pantheon.HostApproval1.Reviewer", "RequestHuman"): (
        ("review_handle", "expected_digest", "idempotency_key"),
        "sss",
        "ss",
    ),
    ("org.pantheon.HostApproval1.Consumer", "ValidateAndBind"): (
        (
            "approval_handle",
            "plan_handle",
            "expected_digest",
            "operation_handle",
        ),
        "ssss",
        "ss",
    ),
    ("org.pantheon.HostApproval1.Consumer", "ApprovalStatus"): (
        ("approval_handle",),
        "s",
        "ss",
    ),
    ("org.pantheon.HostApproval1.HumanIngress", "HumanDecision"): (
        (
            "review_handle",
            "expected_digest",
            "decision",
            "source_event_handle",
            "callback_nonce",
            "idempotency_key",
        ),
        "ssssss",
        "ss",
    ),
}

# Identity may NEVER be a caller argument (ORG-052 §2).
IDENTITY_ARG_NAMES = frozenset(
    (
        "uid",
        "subject",
        "subject_uid",
        "caller",
        "caller_uid",
        "persona",
        "role",
        "instance",
        "account",
        "user",
        "approver",
        "approver_subject",
        "performer",
        "owner",
        "seat_uid",
        "authority_source",
    )
)

# One canonical call-shape per exported method, for the
# every-method-refuses loops (subTest per method, PO requirement 3).
ALL_METHOD_CALLS = (
    ("Review", ("a" * 64, "b" * 64, "")),
    ("ReviewGet", ("a" * 64,)),
    (
        "Decide",
        ("a" * 64, "b" * 64, "granted", "approved", "anchor", "key-1"),
    ),
    ("RequestHuman", ("a" * 64, "b" * 64, "key-1")),
    ("ValidateAndBind", ("a" * 64, "b" * 64, "c" * 64, "d" * 64)),
    ("ApprovalStatus", ("a" * 64,)),
    (
        "HumanDecision",
        ("a" * 64, "b" * 64, "granted", "e" * 64, "f" * 64, "key-1"),
    ),
)
REVIEWER_METHOD_CALLS = ALL_METHOD_CALLS[:4]


def _approval_exported_methods() -> tuple[dict, dict]:
    """Partition exported members into (pantheon, inherited-standard) —
    the T-2912 shape: OUR class defines exactly the ratified seven, and
    every foreign member is a standard org.freedesktop.DBus.* one
    inherited from the library base, never defined or overridden here."""

    pantheon: dict = {}
    foreign: dict = {}
    own_names = set(vars(approval_dbus.HostApproval1Object))
    for name in dir(approval_dbus.HostApproval1Object):
        member = getattr(approval_dbus.HostApproval1Object, name)
        if getattr(member, "_dbus_is_signal", False):
            raise AssertionError(f"signal exported: {name}")
        if not getattr(member, "_dbus_is_method", False):
            continue
        interface = member._dbus_interface
        if interface.startswith("org.pantheon."):
            pantheon[(interface, name)] = member
            continue
        if not interface.startswith("org.freedesktop.DBus."):
            raise AssertionError(
                f"unexpected non-standard interface: {interface}.{name}"
            )
        if name in own_names:
            raise AssertionError(
                f"standard member defined/overridden locally: {name}"
            )
        foreign[(interface, name)] = member
    return pantheon, foreign


class ResolverReturns:
    def __init__(self, subject: str | None) -> None:
        self.subject = subject
        self.calls: list[int] = []

    def subject_for_uid(self, uid: int) -> str | None:
        self.calls.append(uid)
        return self.subject


class ResolverExplodes:
    def subject_for_uid(self, uid: int) -> str | None:
        raise RuntimeError("policy backing store unreachable")


class FakeConnectionRefuses:
    def get_unix_user(self, sender: str) -> int:
        raise RuntimeError("bus cannot resolve sender credentials")


class RecordingApprovalService:
    """Programmable domain fake; records every call so gate tests can
    assert the domain was NEVER touched on a transport refusal."""

    def __init__(self, **returns: object) -> None:
        self.returns = returns
        self.calls: list[tuple] = []
        self.error: Exception | None = None

    def _respond(self, name: str, args: tuple) -> object:
        self.calls.append((name,) + args)
        if self.error is not None:
            raise self.error
        return self.returns[name]

    def review(self, *args: object) -> object:
        return self._respond("review", args)

    def review_get(self, *args: object) -> object:
        return self._respond("review_get", args)

    def decide(self, *args: object) -> object:
        return self._respond("decide", args)

    def validate_and_bind(self, *args: object) -> object:
        return self._respond("validate_and_bind", args)

    def approval_status(self, *args: object) -> object:
        return self._respond("approval_status", args)

    def request_human(self, *args: object) -> object:
        return self._respond("request_human", args)

    def human_decision(self, *args: object) -> object:
        return self._respond("human_decision", args)

    def review_extras(self, *args: object) -> object:
        return self._respond("review_extras", args)


def _bare_approval(**attrs: object) -> object:
    """Instance without dbus export machinery, for negative paths a
    live bus cannot produce (the T-2912 bare-instance pattern)."""

    instance = object.__new__(approval_dbus.HostApproval1Object)
    for key, value in attrs.items():
        setattr(instance, key, value)
    return instance


def _invoke(instance: object, method: str, args: tuple, sender: object):
    func = getattr(approval_dbus.HostApproval1Object, method)
    if getattr(func, "_dbus_async_callbacks", None):
        # T-5518: RequestHuman replies through dbus-python async callbacks;
        # collect what it settled with (synchronously, against these fakes)
        out: dict = {}
        func(
            instance,
            *args,
            sender=sender,
            reply_cb=lambda *values: out.setdefault("reply", values),
            error_cb=lambda error: out.setdefault("error", error),
        )
        if "error" in out:
            raise out["error"]
        return out["reply"]
    return func(instance, *args, sender=sender)


@requires_dbus
class ApprovalHandlerSurfaceTests(unittest.TestCase):
    """In-process assertions over the exported surface (gate 2 halves
    that need no live bus)."""

    def test_declared_signatures_match_the_ratified_tables_exactly(
        self,
    ) -> None:
        pantheon, foreign = _approval_exported_methods()
        observed = {
            key: (
                tuple(member._dbus_args),
                member._dbus_in_signature,
                member._dbus_out_signature,
            )
            for key, member in pantheon.items()
        }
        self.assertEqual(observed, dict(APPROVAL_RATIFIED_INVENTORY))
        # scalar-only inputs: no dict/variant/array/passthrough slot
        for _, in_signature, _out in observed.values():
            self.assertTrue(set(in_signature) <= {"s"}, in_signature)
        # the inherited standard surface is exactly the library base's
        for interface, name in foreign:
            self.assertTrue(
                interface.startswith("org.freedesktop.DBus."),
                f"{interface}.{name}",
            )

    def test_no_method_accepts_caller_identity_arguments(self) -> None:
        pantheon, _foreign = _approval_exported_methods()
        for (interface, name), member in pantheon.items():
            with self.subTest(method=f"{interface}.{name}"):
                self.assertEqual(
                    member._dbus_sender_keyword,
                    "sender",
                    f"{interface}.{name} must resolve the live bus sender",
                )
                declared = set(member._dbus_args)
                self.assertFalse(
                    declared & IDENTITY_ARG_NAMES,
                    f"{interface}.{name} exposes an identity argument",
                )


@requires_dbus
class ApprovalIdentityGateInProcessTests(unittest.TestCase):
    """Per-interface uid gates, bare-instance (gates 5/10/11/12 halves
    that need no live bus). Every loop is subTest-per-method."""

    def _dbus(self):
        import dbus as dbus_module

        return dbus_module

    def _assert_subject_denied(self, caught) -> None:
        self.assertEqual(
            caught.exception.get_dbus_name(),
            wire.POLICY_DENIED_ERROR,
        )
        self.assertEqual(
            caught.exception.get_dbus_message(),
            "subject_denied",
        )

    def test_unresolvable_sender_fails_closed_on_every_method(self) -> None:
        dbus_module = self._dbus()
        service = RecordingApprovalService()
        instance = _bare_approval(
            _service=service,
            _approver_resolver=ResolverReturns("specialist"),
            _consumer_uid=9001,
            _ingress_uid=9002,
        )
        instance._get_unix_user = FakeConnectionRefuses().get_unix_user
        for method, args in ALL_METHOD_CALLS:
            for sender in (":1.42", None, ""):
                with self.subTest(method=method, sender=repr(sender)):
                    with self.assertRaises(
                        dbus_module.DBusException
                    ) as caught:
                        _invoke(instance, method, args, sender)
                    self._assert_subject_denied(caught)
        self.assertEqual(service.calls, [])

    def test_requester_only_uid_refused_on_every_reviewer_method(
        self,
    ) -> None:
        # The resolver returns None for a uid with no approver
        # standing — zero domain calls happen (the transport-tier arm
        # of the gate-12 pair).
        dbus_module = self._dbus()
        service = RecordingApprovalService()
        resolver = ResolverReturns(None)
        instance = _bare_approval(
            _service=service,
            _approver_resolver=resolver,
            _consumer_uid=9001,
            _ingress_uid=9002,
        )
        instance._get_unix_user = lambda sender: 4242
        for method, args in REVIEWER_METHOD_CALLS:
            with self.subTest(method=method):
                with self.assertRaises(dbus_module.DBusException) as caught:
                    _invoke(instance, method, args, ":1.9")
                self._assert_subject_denied(caught)
        self.assertEqual(service.calls, [])
        self.assertEqual(resolver.calls, [4242] * 4)

    def test_exploding_resolver_fails_closed(self) -> None:
        dbus_module = self._dbus()
        instance = _bare_approval(
            _service=RecordingApprovalService(),
            _approver_resolver=ResolverExplodes(),
            _consumer_uid=9001,
            _ingress_uid=9002,
        )
        instance._get_unix_user = lambda sender: 4242
        with self.assertRaises(dbus_module.DBusException) as caught:
            _invoke(instance, "Review", ("a" * 64, "b" * 64, ""), ":1.9")
        self._assert_subject_denied(caught)

    def test_non_consumer_uid_refused_on_consumer_methods(self) -> None:
        dbus_module = self._dbus()
        service = RecordingApprovalService()
        instance = _bare_approval(
            _service=service,
            _approver_resolver=ResolverReturns("specialist"),
            _consumer_uid=9001,
            _ingress_uid=9002,
        )
        instance._get_unix_user = lambda sender: 9002  # ingress, not broker
        for method, args in ALL_METHOD_CALLS[4:6]:
            with self.subTest(method=method):
                with self.assertRaises(dbus_module.DBusException) as caught:
                    _invoke(instance, method, args, ":1.9")
                self._assert_subject_denied(caught)
        self.assertEqual(service.calls, [])

    def test_non_ingress_uid_refused_on_human_decision(self) -> None:
        dbus_module = self._dbus()
        service = RecordingApprovalService()
        instance = _bare_approval(
            _service=service,
            _approver_resolver=ResolverReturns("specialist"),
            _consumer_uid=9001,
            _ingress_uid=9002,
        )
        instance._get_unix_user = lambda sender: 9001  # broker, not ingress
        method, args = ALL_METHOD_CALLS[6]
        with self.assertRaises(dbus_module.DBusException) as caught:
            _invoke(instance, method, args, ":1.9")
        self._assert_subject_denied(caught)
        self.assertEqual(service.calls, [])

    def test_ingress_uid_reaches_exactly_one_human_decision_call(self) -> None:
        # T-2919: the transport passes the LIVE bus uid (never a caller
        # field) and maps the two ratified slots
        from tools.host_control.approval.human import HumanDecisionResult

        self._dbus()
        service = RecordingApprovalService(
            human_decision=HumanDecisionResult("a" * 64, "granted", "e" * 64, False)
        )
        instance = _bare_approval(
            _service=service,
            _approver_resolver=ResolverReturns(None),
            _consumer_uid=9001,
            _ingress_uid=9002,
        )
        instance._get_unix_user = lambda sender: 9002
        method, args = ALL_METHOD_CALLS[6]
        self.assertEqual(_invoke(instance, method, args, ":1.9"), ("granted", "e" * 64))
        self.assertEqual(service.calls, [("human_decision", 9002) + tuple(args)])

    def test_approver_reaches_exactly_one_request_human_call(self) -> None:
        from tools.host_control.approval.human import HumanRequestResult

        self._dbus()
        service = RecordingApprovalService(
            request_human=HumanRequestResult("d" * 64, "presented", False)
        )
        instance = _bare_approval(
            _service=service,
            _approver_resolver=ResolverReturns("specialist"),
            _consumer_uid=9001,
            _ingress_uid=9002,
        )
        instance._get_unix_user = lambda sender: 1001
        self.assertEqual(
            _invoke(instance, "RequestHuman", ("a" * 64, "b" * 64, "key-1"), ":1.9"),
            ("d" * 64, "presented"),
        )
        self.assertEqual(
            service.calls, [("request_human", "specialist", "a" * 64, "b" * 64, "key-1")]
        )

    def test_approver_denied_surfaces_exact_name_and_reason_body(
        self,
    ) -> None:
        dbus_module = self._dbus()
        service = RecordingApprovalService(review=None)
        service.error = ApproverDenied(
            "approver may not review this plan",
            reason_code="approver_shares_trust_domain",
        )
        instance = _bare_approval(
            _service=service,
            _approver_resolver=ResolverReturns("specialist"),
            _consumer_uid=9001,
            _ingress_uid=9002,
        )
        instance._get_unix_user = lambda sender: 1001
        with self.assertRaises(dbus_module.DBusException) as caught:
            _invoke(instance, "Review", ("a" * 64, "b" * 64, ""), ":1.9")
        self.assertEqual(
            caught.exception.get_dbus_name(),
            wire.APPROVER_DENIED_ERROR,
        )
        self.assertEqual(
            caught.exception.get_dbus_message(),
            "approver_shares_trust_domain",
        )

    def test_internal_errors_never_carry_tracebacks_or_messages(
        self,
    ) -> None:
        dbus_module = self._dbus()
        service = RecordingApprovalService(review=None)
        service.error = TypeError("secret /etc/path leak candidate")
        instance = _bare_approval(
            _service=service,
            _approver_resolver=ResolverReturns("specialist"),
            _consumer_uid=9001,
            _ingress_uid=9002,
        )
        instance._get_unix_user = lambda sender: 1001
        with self.assertRaises(dbus_module.DBusException) as caught:
            _invoke(instance, "Review", ("a" * 64, "b" * 64, ""), ":1.9")
        self.assertEqual(
            caught.exception.get_dbus_name(),
            wire.INTERNAL_ERROR,
        )
        self.assertEqual(
            caught.exception.get_dbus_message(),
            "internal error",
        )


@requires_dbus
class Gate12DisclosureThroughTransportTests(unittest.TestCase):
    """Gate 12 through the TRANSPORT: the spy pair the PO required —
    the unauthorized arm proves zero summary fetches, the authorized
    arm proves the SAME counter increments (positive control, so the
    zero cannot be vacuous)."""

    def _build(self, owner_subject: str):
        import tempfile
        from datetime import datetime, timezone
        from pathlib import Path as _Path

        from tools.host_control.approval.identity import ApproverResolver

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        policy_raw = approver_policy_raw(
            subjects={"specialist": 1001, "alpha": 2002},
            approvers={"specialist": approver_entry()},
        )
        fixture = build_approval_domain(
            _Path(temp.name) / "domain",
            policy_raw=policy_raw,
            clock=lambda: datetime.now(timezone.utc),
        )
        self.addCleanup(fixture.close)
        fixture.metadata.plans[PLAN_HANDLE] = plan_metadata_fixture(
            owner=owner_subject
        )
        fixture.metadata.summaries[PLAN_HANDLE] = "redacted-effects"
        instance = _bare_approval(
            _service=fixture.service,
            _approver_resolver=ApproverResolver(
                parse_approver_policy(policy_raw)
            ),
            _consumer_uid=9001,
            _ingress_uid=9002,
        )
        instance._get_unix_user = lambda sender: 1001  # specialist
        return instance, fixture.metadata

    def test_unauthorized_reviewer_triggers_zero_summary_fetches(
        self,
    ) -> None:
        import dbus as dbus_module

        # the plan is owned by the approver itself: policy denies
        # BEFORE any summary disclosure (approver_is_plan_owner)
        instance, metadata = self._build(owner_subject="specialist")
        with self.assertRaises(dbus_module.DBusException) as caught:
            _invoke(
                instance,
                "Review",
                (PLAN_HANDLE, PLAN_DIGEST, ""),
                ":1.9",
            )
        self.assertEqual(
            caught.exception.get_dbus_name(),
            wire.APPROVER_DENIED_ERROR,
        )
        self.assertEqual(
            caught.exception.get_dbus_message(),
            "approver_is_plan_owner",
        )
        summary_events = [
            event for event in metadata.events if event[0] == "plan_summary"
        ]
        self.assertEqual(summary_events, [])

    def test_authorized_reviewer_increments_the_same_counter(self) -> None:
        instance, metadata = self._build(owner_subject="alpha")
        handle, state, digest, summary = _invoke(
            instance,
            "Review",
            (PLAN_HANDLE, PLAN_DIGEST, ""),
            ":1.9",
        )
        self.assertRegex(str(handle), r"^[0-9a-f]{64}$")
        self.assertEqual(state, "open")
        self.assertEqual(digest, PLAN_DIGEST)
        self.assertEqual(summary, "redacted-effects")
        summary_events = [
            event for event in metadata.events if event[0] == "plan_summary"
        ]
        self.assertEqual(summary_events, [("plan_summary", PLAN_HANDLE)])


@requires_dbus
class ConsumerWireMappingTests(unittest.TestCase):
    """ValidateAndBind / ApprovalStatus / Decide wire mapping over a
    programmable domain fake (OQ3 disposition; gate 16 wire half)."""

    def _instance(self, service: RecordingApprovalService):
        instance = _bare_approval(
            _service=service,
            _approver_resolver=ResolverReturns("specialist"),
            _consumer_uid=9001,
            _ingress_uid=9002,
        )
        instance._get_unix_user = lambda sender: 9001
        return instance

    def test_bound_and_replay_return_identical_success_slots(self) -> None:
        record_id = "c" * 64
        for reason in ("bound", "bound_replay"):
            with self.subTest(reason=reason):
                service = RecordingApprovalService(
                    validate_and_bind=BindOutcome("bound", record_id, reason)
                )
                slots = _invoke(
                    self._instance(service),
                    "ValidateAndBind",
                    ("a" * 64, "b" * 64, "c" * 64, "d" * 64),
                    ":1.9",
                )
                self.assertEqual(slots, ("bound", record_id))
        # the live uid — never a constant — reaches the domain
        self.assertEqual(service.calls[0][1], 9001)

    def test_every_denial_code_maps_to_bind_denied_with_exact_body(
        self,
    ) -> None:
        import dbus as dbus_module

        # PO-required totality: iterate the LIVE closed set
        for code in sorted(BIND_DENIAL_REASON_CODES):
            with self.subTest(code=code):
                service = RecordingApprovalService(
                    validate_and_bind=BindOutcome("denied", None, code)
                )
                with self.assertRaises(dbus_module.DBusException) as caught:
                    _invoke(
                        self._instance(service),
                        "ValidateAndBind",
                        ("a" * 64, "b" * 64, "c" * 64, "d" * 64),
                        ":1.9",
                    )
                self.assertEqual(
                    caught.exception.get_dbus_name(),
                    wire.BIND_DENIED_ERROR,
                )
                self.assertEqual(
                    caught.exception.get_dbus_message(),
                    code,
                )

    def test_approval_status_maps_to_state_and_bounded_json(self) -> None:
        status = ApprovalStatus(
            approval_handle="a" * 64,
            state="issued",
            expires_at="2026-08-12T01:00:00+00:00",
            summary="grant issued; target=recovery; families=persona.install",
        )
        service = RecordingApprovalService(approval_status=status)
        state, payload = _invoke(
            self._instance(service),
            "ApprovalStatus",
            ("a" * 64,),
            ":1.9",
        )
        self.assertEqual(state, "issued")
        raw = json.loads(payload)
        self.assertEqual(raw["schema"], "pantheon-approval-status/v1")
        self.assertEqual(raw["approval_handle"], "a" * 64)

    def test_decide_denied_returns_empty_handle_slot(self) -> None:
        from tools.host_control.approval.contracts import DecisionResult

        service = RecordingApprovalService(
            decide=DecisionResult(
                review_handle="a" * 64,
                state="denied",
                decision="denied",
                reason_code="rejected",
                approval_handle=None,
            )
        )
        instance = _bare_approval(
            _service=service,
            _approver_resolver=ResolverReturns("specialist"),
            _consumer_uid=9001,
            _ingress_uid=9002,
        )
        instance._get_unix_user = lambda sender: 1001
        slots = _invoke(
            instance,
            "Decide",
            ("a" * 64, "b" * 64, "denied", "rejected", "anchor", "key-1"),
            ":1.9",
        )
        self.assertEqual(slots, ("denied", ""))


import xml.etree.ElementTree as ElementTree  # noqa: E402

DIST = HOST_CONTROL / "dist"

REVIEWER_MEMBERS = frozenset(
    ("Review", "ReviewGet", "Decide", "RequestHuman")
)
CONSUMER_MEMBERS = frozenset(("ValidateAndBind", "ApprovalStatus"))
HUMAN_INGRESS_MEMBERS = frozenset(("HumanDecision",))
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
APPROVAL_HEALTH_PATH = "/org/pantheon/HostApproval1/Health"


def _policies(tree: ElementTree.Element) -> dict:
    result: dict = {}
    for node in tree.findall("policy"):
        key = (
            ("context", node.get("context"))
            if node.get("context")
            else ("user", node.get("user"))
        )
        result[key] = node
    return result


def _assert_member_level_allows(
    case: unittest.TestCase,
    policy: ElementTree.Element,
    *,
    interface: str,
    members: frozenset,
) -> None:
    """Every allow is destination+interface+member exact; the member
    set equals the ratified inventory; nothing else is allowed."""

    allows = policy.findall("allow")
    case.assertEqual(len(policy.findall("deny")), 0)
    observed = set()
    for allow in allows:
        case.assertIsNone(allow.get("own"), "no own grant on a caller policy")
        case.assertEqual(
            allow.get("send_destination"),
            "org.pantheon.HostApproval1",
        )
        case.assertEqual(allow.get("send_interface"), interface)
        member = allow.get("send_member")
        case.assertIsNotNone(
            member,
            "an allow without send_member is interface-wide — drift "
            "toward permissiveness",
        )
        observed.add(member)
    case.assertEqual(observed, set(members))


def _assert_approval_monitor_health_policy(
    case: unittest.TestCase,
    policy: ElementTree.Element,
) -> None:
    case.assertEqual(len(policy.findall("deny")), 0)
    observed = {
        tuple(sorted(allow.attrib.items()))
        for allow in policy.findall("allow")
    }
    case.assertEqual(
        observed,
        {
            tuple(
                sorted(
                    {
                        "send_destination": "org.pantheon.HostApproval1",
                        "send_path": APPROVAL_HEALTH_PATH,
                        "send_interface": PROPERTIES_INTERFACE,
                        "send_member": "Get",
                    }.items()
                )
            ),
            tuple(
                sorted(
                    {
                        "send_destination": "org.pantheon.HostApproval1",
                        "send_path": APPROVAL_HEALTH_PATH,
                        "send_interface": PROPERTIES_INTERFACE,
                        "send_member": "GetAll",
                    }.items()
                )
            ),
            tuple(
                sorted(
                    {
                        "receive_sender": "org.pantheon.HostApproval1",
                        "receive_path": APPROVAL_HEALTH_PATH,
                        "receive_interface": PROPERTIES_INTERFACE,
                        "receive_member": "PropertiesChanged",
                        "receive_type": "signal",
                    }.items()
                )
            ),
        },
    )


class ApprovalBusPolicyArtifactTests(unittest.TestCase):
    """The staged approval bus policy cannot silently drift permissive
    (the T-2912 N8 posture, per uid class)."""

    def test_base_policy_is_default_deny_with_exact_allowances(self) -> None:
        tree = ElementTree.parse(DIST / "org.pantheon.HostApproval1.conf")
        policies = _policies(tree.getroot())
        self.assertEqual(
            set(policies),
            {
                ("context", "default"),
                ("user", "pantheon-hostapproval"),
                ("user", "pantheon-hostmonitor"),
                ("user", "pantheon-hostbroker"),
                ("user", "pantheon-hostapproval-ingress"),
            },
        )
        # deny-by-default: the default context contains ONLY denies
        default = policies[("context", "default")]
        self.assertEqual(default.findall("allow"), [])
        denies = {
            tuple(sorted(deny.attrib.items()))
            for deny in default.findall("deny")
        }
        self.assertEqual(
            denies,
            {
                (("own", "org.pantheon.HostApproval1"),),
                (("send_destination", "org.pantheon.HostApproval1"),),
                (
                    ("receive_sender", "org.pantheon.HostApproval1"),
                    ("receive_type", "signal"),
                ),
            },
        )
        # the approval account may own the name and nothing else
        approval = policies[("user", "pantheon-hostapproval")]
        self.assertEqual(len(approval.findall("deny")), 0)
        approval_allows = approval.findall("allow")
        self.assertEqual(len(approval_allows), 1)
        self.assertEqual(
            approval_allows[0].get("own"),
            "org.pantheon.HostApproval1",
        )
        self.assertIsNone(approval_allows[0].get("send_destination"))
        # the broker account gets exactly the two Consumer members
        _assert_member_level_allows(
            self,
            policies[("user", "pantheon-hostbroker")],
            interface="org.pantheon.HostApproval1.Consumer",
            members=CONSUMER_MEMBERS,
        )
        # the ingress account gets exactly HumanDecision
        _assert_member_level_allows(
            self,
            policies[("user", "pantheon-hostapproval-ingress")],
            interface="org.pantheon.HostApproval1.HumanIngress",
            members=HUMAN_INGRESS_MEMBERS,
        )
        _assert_approval_monitor_health_policy(
            self,
            policies[("user", "pantheon-hostmonitor")],
        )

    def test_approver_template_allows_exactly_the_four_reviewer_members(
        self,
    ) -> None:
        raw = (
            DIST / "pantheon-host-approval-approver.conf.template"
        ).read_text(encoding="utf-8")
        self.assertIn("@APPROVER_ACCOUNT@", raw)
        substituted = raw.replace("@APPROVER_ACCOUNT@", "testapprover")
        tree = ElementTree.fromstring(substituted)
        policies = _policies(tree)
        self.assertEqual(set(policies), {("user", "testapprover")})
        _assert_member_level_allows(
            self,
            policies[("user", "testapprover")],
            interface="org.pantheon.HostApproval1.Reviewer",
            members=REVIEWER_MEMBERS,
        )

    def test_no_wildcards_anywhere_in_staged_policies(self) -> None:
        for name in (
            "org.pantheon.HostApproval1.conf",
            "pantheon-host-approval-approver.conf.template",
        ):
            raw = (DIST / name).read_text(encoding="utf-8")
            self.assertNotIn('="*"', raw, name)
            self.assertNotIn("eavesdrop", raw, name)

    def test_policies_are_labeled_staged_and_provisional(self) -> None:
        for name in (
            "org.pantheon.HostApproval1.conf",
            "pantheon-host-approval-approver.conf.template",
        ):
            raw = (DIST / name).read_text(encoding="utf-8")
            self.assertIn("STAGED ARTIFACT", raw, name)
            self.assertIn("T-2920", raw, name)
        base = (DIST / "org.pantheon.HostApproval1.conf").read_text(
            encoding="utf-8"
        )
        self.assertIn("PROVISIONAL", base)
        # the ingress account names the service that will run as it
        self.assertIn("pantheon-hostapproval-ingress", base)
        self.assertIn("T-2919", base)


class ApprovalSystemdUnitArtifactTests(unittest.TestCase):
    def _unit(self) -> dict:
        raw = (DIST / "pantheon-host-approval.service").read_text(
            encoding="utf-8"
        )
        values: dict = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "[", ";")):
                continue
            key, _, value = line.partition("=")
            values.setdefault(key, []).append(value)
        return values

    def test_unit_is_type_dbus_unprivileged_and_hardened(self) -> None:
        unit = self._unit()
        self.assertEqual(unit["Type"], ["dbus"])
        self.assertEqual(unit["BusName"], ["org.pantheon.HostApproval1"])
        self.assertEqual(unit["User"], ["pantheon-hostapproval"])
        self.assertNotEqual(unit["User"], ["root"])
        self.assertEqual(unit["NoNewPrivileges"], ["yes"])
        self.assertEqual(unit["ProtectSystem"], ["strict"])
        self.assertEqual(unit["ProtectHome"], ["yes"])
        self.assertEqual(unit["PrivateTmp"], ["yes"])
        self.assertEqual(unit["RestrictAddressFamilies"], ["AF_UNIX"])
        # empty bounding set: the authority holds no capabilities
        self.assertEqual(unit["CapabilityBoundingSet"], [""])
        self.assertEqual(unit["SystemCallFilter"], ["@system-service"])
        # its OWN journal directory, never the broker's
        self.assertEqual(unit["StateDirectory"], ["pantheon-host-approval"])
        exec_start = unit["ExecStart"]
        self.assertEqual(len(exec_start), 1)
        self.assertTrue(exec_start[0].startswith("/"))
        for metachar in ("|", ";", "&", "$", "`"):
            self.assertNotIn(metachar, exec_start[0])

    def test_unit_is_labeled_staged_and_provisional(self) -> None:
        raw = (DIST / "pantheon-host-approval.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("STAGED ARTIFACT", raw)
        self.assertIn("PROVISIONAL", raw)
        self.assertIn("T-2920", raw)


import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402

import tools._host_control_testbus as testbus  # noqa: E402

DBUS_DAEMON = shutil.which("dbus-daemon")

requires_private_bus = unittest.skipUnless(
    approval_dbus.DBUS_AVAILABLE and DBUS_DAEMON,
    "private-bus integration needs dbus-python and dbus-daemon",
)

_PERMISSIVE_BUS_CONFIG = """<!DOCTYPE busconfig PUBLIC
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


class ApprovalPrivateBusCase(unittest.TestCase):
    """One private dbus-daemon + one approval-service subprocess per
    class (plus, for the cross-service class, the T-2912 broker
    driver on the same bus).

    The permissive bus config is DELIBERATE (gate 10 posture): every
    refusal these tests observe is the service's own server-side
    authorization, never bus policy — bus policy is defence in depth,
    not the perimeter.

    Which uid class the CALLER occupies is scenario data (the
    config-relative-uid trick): one test process has one real uid, so
    the service's configured uid classes shift relative to it.
    """

    SCENARIO = "reviewer"
    WITH_BROKER = False

    @classmethod
    def _spawn_driver(
        cls,
        module: str,
        config: dict,
        log_name: str,
    ) -> tuple[subprocess.Popen, object]:
        config_path = os.path.join(cls.bus_dir, f"{log_name}.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        log = open(  # noqa: SIM115 - closed in tearDownClass
            os.path.join(cls.bus_dir, f"{log_name}.log"),
            "w",
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [sys.executable, "-m", module, config_path],
            cwd=REPO_ROOT,
            stdout=log,
            stderr=log,
            preexec_fn=testbus.pdeathsig_preexec_fn(),
        )
        cls._bus_handle.register_driver(process.pid)
        return process, log

    @classmethod
    def _await_name_and_state(
        cls,
        process: subprocess.Popen,
        log,
        bus_name: str,
        state_path: Path,
    ) -> dict:
        deadline = time.monotonic() + 30
        while not cls.connection.name_has_owner(bus_name):
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"service never owned {bus_name}; driver log: "
                    + Path(log.name).read_text(encoding="utf-8")[-2000:]
                )
            if process.poll() is not None:
                raise AssertionError(
                    "driver exited early; log: "
                    + Path(log.name).read_text(encoding="utf-8")[-2000:]
                )
            time.sleep(0.05)
        deadline = time.monotonic() + 20
        while not state_path.exists():
            if time.monotonic() > deadline:
                raise AssertionError("driver never wrote its state file")
            time.sleep(0.05)
        return json.loads(state_path.read_text(encoding="utf-8"))

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._bus_handle = testbus.spawn_daemon("t2913-", _PERMISSIVE_BUS_CONFIG)
        cls.bus_dir = cls._bus_handle.bus_dir
        cls.daemon_log = cls._bus_handle.log
        cls.daemon = cls._bus_handle.process
        cls.bus_address = cls._bus_handle.bus_address
        cls.work = tempfile.TemporaryDirectory()
        cls._logs = []
        cls._processes = []

        import dbus
        import dbus.bus

        cls.connection = dbus.bus.BusConnection(cls.bus_address)

        if cls.WITH_BROKER:
            broker_root = Path(cls.work.name) / "broker-domain"
            broker_process, broker_log = cls._spawn_driver(
                "tools.test_host_control_dbus_driver",
                {
                    "bus_address": cls.bus_address,
                    "root": str(broker_root),
                    "subject_uid": os.getuid(),
                    "register_subject": True,
                    "approval_uid": os.getuid(),
                },
                "broker-driver",
            )
            cls._processes.append(broker_process)
            cls._logs.append(broker_log)
            cls.broker_state = cls._await_name_and_state(
                broker_process,
                broker_log,
                broker_wire.BUS_NAME,
                broker_root / "state.json",
            )

        approval_root = Path(cls.work.name) / "approval-domain"
        approval_process, approval_log = cls._spawn_driver(
            "tools.test_host_control_approval_dbus_driver",
            {
                "bus_address": cls.bus_address,
                "root": str(approval_root),
                "scenario": cls.SCENARIO,
            },
            "approval-driver",
        )
        cls._processes.append(approval_process)
        cls._logs.append(approval_log)
        cls.state = cls._await_name_and_state(
            approval_process,
            approval_log,
            wire.BUS_NAME,
            approval_root / "state.json",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        for process in reversed(getattr(cls, "_processes", [])):
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        for log in getattr(cls, "_logs", []):
            log.close()
        if getattr(cls, "work", None) is not None:
            cls.work.cleanup()
        if getattr(cls, "_bus_handle", None) is not None:
            testbus.stop_daemon(cls._bus_handle)
        super().tearDownClass()

    @classmethod
    def _call(
        cls,
        method: str,
        signature: str,
        args: tuple,
        interface: str = wire.REVIEWER_INTERFACE,
    ):
        return cls.connection.call_blocking(
            wire.BUS_NAME,
            wire.OBJECT_PATH,
            interface,
            method,
            signature,
            args,
        )

    def assert_wire_error(
        self,
        expected_name: str,
        method: str,
        signature: str,
        args: tuple,
        interface: str = wire.REVIEWER_INTERFACE,
    ) -> "Exception":
        import dbus

        with self.assertRaises(dbus.exceptions.DBusException) as caught:
            self._call(method, signature, args, interface)
        self.assertEqual(
            caught.exception.get_dbus_name(),
            expected_name,
            f"{interface}.{method}",
        )
        return caught.exception


# (method, signature, args, interface) covering ALL SEVEN exported
# methods — the every-method-refuses loops iterate this (subTest per
# method, PO requirement 3).
def _all_bus_calls() -> tuple:
    return (
        ("Review", "sss", ("a" * 64, "b" * 64, ""), wire.REVIEWER_INTERFACE),
        ("ReviewGet", "s", ("a" * 64,), wire.REVIEWER_INTERFACE),
        (
            "Decide",
            "ssssss",
            ("a" * 64, "b" * 64, "granted", "approved", "anchor", "key-1"),
            wire.REVIEWER_INTERFACE,
        ),
        (
            "RequestHuman",
            "sss",
            ("a" * 64, "b" * 64, "key-1"),
            wire.REVIEWER_INTERFACE,
        ),
        (
            "ValidateAndBind",
            "ssss",
            ("a" * 64, "b" * 64, "c" * 64, "d" * 64),
            wire.CONSUMER_INTERFACE,
        ),
        ("ApprovalStatus", "s", ("a" * 64,), wire.CONSUMER_INTERFACE),
        (
            "HumanDecision",
            "ssssss",
            ("a" * 64, "b" * 64, "granted", "e" * 64, "f" * 64, "key-1"),
            wire.HUMAN_INGRESS_INTERFACE,
        ),
    )


@requires_private_bus
class ApprovalReviewerBusTests(ApprovalPrivateBusCase):
    """Registered approver on a permissive private bus — the happy
    reviewer path plus the locked-inventory introspection (gate 2)."""

    SCENARIO = "reviewer"

    def test_introspection_exposes_exactly_the_locked_inventory(
        self,
    ) -> None:
        xml = self._call(
            "Introspect",
            "",
            (),
            "org.freedesktop.DBus.Introspectable",
        )
        root = ElementTree.fromstring(str(xml))
        interfaces = {
            node.get("name"): node for node in root.findall("interface")
        }
        pantheon = {
            name for name in interfaces if name.startswith("org.pantheon.")
        }
        self.assertEqual(
            pantheon,
            {
                wire.REVIEWER_INTERFACE,
                wire.CONSUMER_INTERFACE,
                wire.HUMAN_INGRESS_INTERFACE,
            },
        )
        for name in set(interfaces) - pantheon:
            self.assertTrue(name.startswith("org.freedesktop.DBus."), name)

        def observed(interface_name: str) -> dict:
            table = {}
            for method in interfaces[interface_name].findall("method"):
                in_signature = ""
                out_signature = ""
                for arg in method.findall("arg"):
                    direction = arg.get("direction", "in")
                    if direction == "in":
                        in_signature += arg.get("type")
                    else:
                        out_signature += arg.get("type")
                table[method.get("name")] = (in_signature, out_signature)
            return table

        self.assertEqual(
            observed(wire.REVIEWER_INTERFACE),
            {
                "Review": ("sss", "ssss"),
                "ReviewGet": ("s", "ss"),
                "Decide": ("ssssss", "ss"),
                "RequestHuman": ("sss", "ss"),
            },
        )
        self.assertEqual(
            observed(wire.CONSUMER_INTERFACE),
            {
                "ValidateAndBind": ("ssss", "ss"),
                "ApprovalStatus": ("s", "ss"),
            },
        )
        self.assertEqual(
            observed(wire.HUMAN_INGRESS_INTERFACE),
            {"HumanDecision": ("ssssss", "ss")},
        )
        # no broadcast signals and no properties on any interface
        # (ORG-052 §4; dbus-python cannot mint a property without
        # explicit machinery neither provider wires, but that was a
        # library-implied claim — pin it explicitly, T-2937)
        for name in pantheon:
            self.assertEqual(interfaces[name].findall("signal"), [])
            self.assertEqual(interfaces[name].findall("property"), [])

    def test_review_then_decide_grants_an_opaque_handle(self) -> None:
        handle, state, digest, summary = self._call(
            "Review",
            "sss",
            (self.state["plan_handle"], self.state["plan_digest"], ""),
        )
        self.assertRegex(str(handle), r"^[0-9a-f]{64}$")
        self.assertEqual(str(state), "open")
        self.assertEqual(str(digest), self.state["plan_digest"])
        self.assertTrue(str(summary).startswith("redacted-effects:"))
        decide_state, approval_handle = self._call(
            "Decide",
            "ssssss",
            (
                str(handle),
                self.state["plan_digest"],
                "granted",
                "approved",
                "ticket:T-2913-bus",
                "bus-grant-key-1",
            ),
        )
        self.assertEqual(str(decide_state), "granted")
        self.assertRegex(str(approval_handle), r"^[0-9a-f]{64}$")

    def test_decide_denied_returns_an_empty_handle_slot(self) -> None:
        handle, _state, _digest, _summary = self._call(
            "Review",
            "sss",
            (self.state["plan_b_handle"], self.state["plan_b_digest"], ""),
        )
        decide_state, approval_handle = self._call(
            "Decide",
            "ssssss",
            (
                str(handle),
                self.state["plan_b_digest"],
                "denied",
                "rejected",
                "ticket:T-2913-bus",
                "bus-deny-key-1",
            ),
        )
        self.assertEqual(str(decide_state), "denied")
        self.assertEqual(str(approval_handle), "")

    def test_review_get_round_trips_the_closed_summary(self) -> None:
        handle, _state, _digest, _summary = self._call(
            "Review",
            "sss",
            (self.state["plan_handle"], self.state["plan_digest"], ""),
        )
        state, payload = self._call("ReviewGet", "s", (str(handle),))
        raw = json.loads(str(payload))
        self.assertEqual(raw["schema"], "pantheon-approval-review-summary/v1")
        self.assertEqual(set(raw), REVIEW_SUMMARY_KEYS)
        self.assertEqual(raw["review_handle"], str(handle))
        self.assertEqual(str(state), raw["state"])

    def test_request_human_without_a_registered_ingress_refuses_bounded(self) -> None:
        # T-2919: RequestHuman is live; this driver registers no human
        # ingress, so the domain refuses with the closed code and creates
        # nothing (the full path is tools/test_host_control_telegram_ingress*)
        handle, _state, _digest, _summary = self._call(
            "Review",
            "sss",
            (self.state["plan_handle"], self.state["plan_digest"], ""),
        )
        error = self.assert_wire_error(
            wire.FAILED_ERROR,
            "RequestHuman",
            "sss",
            (str(handle), self.state["plan_digest"], "bus-human-key-1"),
        )
        self.assertEqual(error.get_dbus_message(), "human_ingress_unconfigured")

    def test_error_names_round_trip_verbatim(self) -> None:
        error = self.assert_wire_error(
            wire.UNKNOWN_HANDLE_ERROR,
            "Review",
            "sss",
            ("e" * 64, "b" * 64, ""),
        )
        self.assertEqual(error.get_dbus_message(), "unknown plan handle")

    def test_consumer_and_ingress_interfaces_refuse_the_approver_uid(
        self,
    ) -> None:
        # interface separation: approver standing is not consumer or
        # ingress standing
        for method, signature, args, interface in _all_bus_calls()[4:]:
            with self.subTest(method=method):
                error = self.assert_wire_error(
                    wire.POLICY_DENIED_ERROR,
                    method,
                    signature,
                    args,
                    interface,
                )
                self.assertEqual(
                    error.get_dbus_message(),
                    "subject_denied",
                )


@requires_private_bus
class RequesterOnlyUidBusTests(ApprovalPrivateBusCase):
    """HEADLINE PROOF 1 (ticket Ask / gate 5 server half): a
    requester-only uid — registered as a subject, NO approver
    standing — calling ANY method on ANY of the three interfaces
    directly over the bus is refused with the exact authorization
    error, on a fully PERMISSIVE bus (bus policy is not the
    perimeter, gate 10)."""

    SCENARIO = "requester_only"

    def test_every_method_refuses_a_requester_only_uid(self) -> None:
        for method, signature, args, interface in _all_bus_calls():
            with self.subTest(method=f"{interface}.{method}"):
                error = self.assert_wire_error(
                    wire.POLICY_DENIED_ERROR,
                    method,
                    signature,
                    args,
                    interface,
                )
                self.assertEqual(
                    error.get_dbus_message(),
                    "subject_denied",
                )

    def test_refusal_precedes_existence_disclosure(self) -> None:
        # even a VALID plan handle + digest gets the same refusal: the
        # gate fires before any domain read
        error = self.assert_wire_error(
            wire.POLICY_DENIED_ERROR,
            "Review",
            "sss",
            (self.state["plan_handle"], self.state["plan_digest"], ""),
        )
        self.assertEqual(error.get_dbus_message(), "subject_denied")


@requires_private_bus
class SelfApprovalOwnerBusTests(ApprovalPrivateBusCase):
    """HEADLINE PROOF 2, arm (i) (gate 11): the caller's uid resolves
    to the approver subject that OWNS the plan — this very test client
    is 'a second process under the same registered uid', and it cannot
    approve that uid's own plan."""

    SCENARIO = "self_owner"

    def test_second_process_cannot_review_its_own_plan(self) -> None:
        error = self.assert_wire_error(
            wire.APPROVER_DENIED_ERROR,
            "Review",
            "sss",
            (self.state["plan_handle"], self.state["plan_digest"], ""),
        )
        # the body is EXACTLY the closed reason code: no summary bytes,
        # no topology (gate 12 disclosure posture on the denial path)
        self.assertEqual(
            error.get_dbus_message(),
            "approver_is_plan_owner",
        )


@requires_private_bus
class SelfApprovalTrustDomainBusTests(ApprovalPrivateBusCase):
    """HEADLINE PROOF 2, arm (ii) (gate 11): the caller resolves to a
    DISTINCT approver subject that shares the plan owner's Unix uid —
    the same-uid trust domain. Neither opening a review nor deciding a
    legitimately-opened one is possible."""

    SCENARIO = "self_trust_domain"

    def test_same_uid_shadow_cannot_review_the_plan(self) -> None:
        error = self.assert_wire_error(
            wire.APPROVER_DENIED_ERROR,
            "Review",
            "sss",
            (self.state["plan_handle"], self.state["plan_digest"], ""),
        )
        self.assertEqual(
            error.get_dbus_message(),
            "approver_shares_trust_domain",
        )

    def test_same_uid_shadow_cannot_decide_an_open_review(self) -> None:
        # the review was opened driver-side by 'mason', a legitimate
        # approver on a third uid; the bus caller holds the review
        # handle AND the digest and is still refused — decide is the
        # authoritative gate
        error = self.assert_wire_error(
            wire.APPROVER_DENIED_ERROR,
            "Decide",
            "ssssss",
            (
                self.state["open_review_handle"],
                self.state["plan_digest"],
                "granted",
                "approved",
                "ticket:T-2913-theft",
                "bus-theft-key-1",
            ),
        )
        self.assertEqual(
            error.get_dbus_message(),
            "approver_shares_trust_domain",
        )


@requires_private_bus
class ConsumerBusTests(ApprovalPrivateBusCase):
    """The configured consumer (broker) uid on the Consumer interface
    (gate 16 wire half; the full denial matrix is landed domain
    coverage)."""

    SCENARIO = "consumer"

    OPERATION_HANDLE = "ab" * 32

    def test_approval_status_round_trips(self) -> None:
        state, payload = self._call(
            "ApprovalStatus",
            "s",
            (self.state["approval_b_handle"],),
            wire.CONSUMER_INTERFACE,
        )
        self.assertEqual(str(state), "issued")
        raw = json.loads(str(payload))
        self.assertEqual(raw["schema"], "pantheon-approval-status/v1")
        self.assertEqual(
            raw["approval_handle"],
            self.state["approval_b_handle"],
        )

    def test_mismatched_digest_is_bind_denied_with_exact_body(self) -> None:
        error = self.assert_wire_error(
            wire.BIND_DENIED_ERROR,
            "ValidateAndBind",
            "ssss",
            (
                self.state["approval_b_handle"],
                self.state["plan_b_handle"],
                self.state["plan_digest"],  # digest of plan A: wrong
                "cd" * 32,
            ),
            wire.CONSUMER_INTERFACE,
        )
        self.assertEqual(error.get_dbus_message(), "digest_mismatch")

    def test_cross_grant_is_bind_denied_as_plan_mismatch(self) -> None:
        error = self.assert_wire_error(
            wire.BIND_DENIED_ERROR,
            "ValidateAndBind",
            "ssss",
            (
                self.state["approval_handle"],
                self.state["plan_b_handle"],
                self.state["plan_b_digest"],
                "cd" * 32,
            ),
            wire.CONSUMER_INTERFACE,
        )
        self.assertEqual(error.get_dbus_message(), "plan_mismatch")

    def test_validate_and_bind_binds_then_replays_identically(self) -> None:
        first = self._call(
            "ValidateAndBind",
            "ssss",
            (
                self.state["approval_handle"],
                self.state["plan_handle"],
                self.state["plan_digest"],
                self.OPERATION_HANDLE,
            ),
            wire.CONSUMER_INTERFACE,
        )
        self.assertEqual(str(first[0]), "bound")
        self.assertRegex(str(first[1]), r"^[0-9a-f]{64}$")
        replay = self._call(
            "ValidateAndBind",
            "ssss",
            (
                self.state["approval_handle"],
                self.state["plan_handle"],
                self.state["plan_digest"],
                self.OPERATION_HANDLE,
            ),
            wire.CONSUMER_INTERFACE,
        )
        # gate 16: identical retry returns the ORIGINAL binding,
        # byte-identical on the wire
        self.assertEqual(
            tuple(str(item) for item in first),
            tuple(str(item) for item in replay),
        )

    def test_reviewer_and_ingress_refuse_the_consumer_uid(self) -> None:
        calls = _all_bus_calls()
        for method, signature, args, interface in calls[:4] + calls[6:]:
            with self.subTest(method=f"{interface}.{method}"):
                error = self.assert_wire_error(
                    wire.POLICY_DENIED_ERROR,
                    method,
                    signature,
                    args,
                    interface,
                )
                self.assertEqual(
                    error.get_dbus_message(),
                    "subject_denied",
                )


@requires_private_bus
class HumanIngressBusTests(ApprovalPrivateBusCase):
    """The configured ingress uid on the HumanIngress interface."""

    SCENARIO = "ingress"

    def test_human_decision_passes_transport_but_domain_requires_registration(self) -> None:
        # T-2919: the transport gate admits the configured ingress uid;
        # the domain rechecks the same live uid against the root-owned
        # HumanIngressRegistration, which this driver does not configure
        error = self.assert_wire_error(
            wire.POLICY_DENIED_ERROR,
            "HumanDecision",
            "ssssss",
            ("a" * 64, "b" * 64, "granted", "e" * 64, "f" * 64, "key-1"),
            wire.HUMAN_INGRESS_INTERFACE,
        )
        self.assertEqual(error.get_dbus_message(), "subject_denied")

    def test_reviewer_and_consumer_refuse_the_ingress_uid(self) -> None:
        for method, signature, args, interface in _all_bus_calls()[:6]:
            with self.subTest(method=f"{interface}.{method}"):
                error = self.assert_wire_error(
                    wire.POLICY_DENIED_ERROR,
                    method,
                    signature,
                    args,
                    interface,
                )
                self.assertEqual(
                    error.get_dbus_message(),
                    "subject_denied",
                )


@requires_private_bus
class CrossServiceMetadataClientBusTests(ApprovalPrivateBusCase):
    """SEVERABLE (OQ5 disposition): the REAL T-2912 broker service and
    this unit's approval service on ONE private bus, the approval
    service's metadata source being the real ``BrokerPlanMetadataClient``
    — the ADR §5 metadata-first flow across two real services."""

    SCENARIO = "cross_service"
    WITH_BROKER = True

    def test_review_round_trips_real_broker_metadata(self) -> None:
        """Regression provenance (T-2913): this test DISCOVERED that
        the domain's original ``MAX_PLAN_SUMMARY_LENGTH = 1024``
        refused the real broker plan summary (measured 3134 bytes
        against the T-2912 fixture manifest) — ``Review`` failed
        closed on every real plan. The PO-authorized fix aligned the
        bound with the wire-tier JSON limit;
        ``SummaryBoundAlignmentTests`` pins the relationship so the
        constant cannot silently re-tighten."""

        # 'specialist' (caller uid) reviews seat-bravo's plan (uid 60001):
        # authorization runs against LIVE broker metadata, then the
        # REAL bounded redacted summary is disclosed
        handle, state, digest, summary = self._call(
            "Review",
            "sss",
            (
                self.broker_state["seat_b_plan_handle"],
                self.broker_state["seat_b_plan_digest"],
                "",
            ),
        )
        self.assertRegex(str(handle), r"^[0-9a-f]{64}$")
        self.assertEqual(str(state), "open")
        self.assertEqual(str(digest), self.broker_state["seat_b_plan_digest"])
        raw = json.loads(str(summary))
        self.assertEqual(raw["schema"], "pantheon-host-plan-summary/v1")
        self.assertEqual(
            raw["plan_handle"],
            self.broker_state["seat_b_plan_handle"],
        )

    def test_same_uid_plan_denied_through_real_metadata(self) -> None:
        # gate 11 END-TO-END across two real services: the plan owned
        # by seat 'alpha' (the caller's own uid, per the broker's live
        # registry) is not reviewable by 'specialist' on that same uid
        error = self.assert_wire_error(
            wire.APPROVER_DENIED_ERROR,
            "Review",
            "sss",
            (
                self.broker_state["subject_plan_handle"],
                self.broker_state["subject_plan_digest"],
                "",
            ),
        )
        self.assertEqual(
            error.get_dbus_message(),
            "approver_shares_trust_domain",
        )

    def test_unknown_plan_stays_unknown_through_the_client(self) -> None:
        error = self.assert_wire_error(
            wire.UNKNOWN_HANDLE_ERROR,
            "Review",
            "sss",
            ("e" * 64, "b" * 64, ""),
        )
        self.assertEqual(error.get_dbus_message(), "unknown plan handle")


if __name__ == "__main__":  # must stay LAST (count-stability, T-2170)
    unittest.main()
