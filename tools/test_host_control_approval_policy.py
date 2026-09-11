"""T-2911 tests: approver policy, separation of duty, cross-persona handoff.

ORG-052 acceptance gates covered here:

- Gate 12 (metadata-first disclosure): CLOSED. The spy proves call COUNT
  and call ORDER — zero ``plan_summary`` and zero request-plan fetches on
  every denied path, summary strictly LAST on the authorized path.
- Gate 13 (separation of duty): CLOSED. Every denial is ADVERSARIAL —
  each separation rule, each downgrade vector, and the reciprocal /
  circular / recipient-mismatch attacks are attempted and DENIED with
  their own closed reason code.
- Gate 23 (cross-persona handoff): the LINKING / BINDING /
  human-gate-preservation half is closed here. The gate's opening clause —
  a test that traverses the dispatcher/mailbox plane — is NOT closed by
  this unit (no dispatcher or mailbox ingress exists here); that half is
  owned by the MCP/ingress units (T-2915/T-2916/T-2919) and the arc
  integration pass.

Fixture note: this module builds its OWN minimal fixture rather than
importing the sibling test module's (the AB-2 OQ7 precedent — fixture
duplication is cheaper than cross-module test coupling to a file another
dispatch may freeze).

Vocabulary note (PO override 1): the operation-family namespace is
root-owned manifest DATA, so the anti-downgrade tests anchor on the REAL
family identifier ``persona.install`` from the landed broker surface —
a floor proven only against invented names would be vacuous.
"""

from __future__ import annotations

import dataclasses
import inspect
import itertools
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.host_control.approval.contracts import (
    HANDOFF_LINK_REASON_CODES,
    POLICY_DENIAL_REASON_CODES,
    ApproverDenied,
    GrantRecord,
    HandoffLink,
    HandoffMismatch,
    PlanMetadata,
    PolicyVerdict,
    ReviewRecord,
)
from tools.host_control.approval.journal import ApprovalJournal
from tools.host_control.approval.policy import (
    APPROVAL_CLASSES,
    CAPABILITY_CATEGORIES,
    CATEGORY_CLASSES,
    HUMAN_REQUIRED_CATEGORIES,
    MAX_POLICY_BYTES,
    ApproverPolicy,
    InvalidApproverPolicy,
    PolicyApproverAuthorizer,
    load_approver_policy,
    parse_approver_policy,
)
from tools.host_control.approval.service import (
    DENY_ALL_AUTHORIZER,
    ApprovalService,
)
from tools.host_control.contracts import (
    HostControlError,
    IdempotencyConflict,
    RegistryError,
    UnknownHandle,
)
from tools.host_control.trusted_files import RootOwnedFileLoader


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXED_NOW = datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)
CONSUMER_UID = 2001

# The REAL operation-family identifier from the landed broker surface
# (see tools/test_host_control_core.py policy fixtures) — the
# anti-vacuity anchor for the human-gate tests.
REAL_FAMILY = "persona.install"


class SpyMetadataSource:
    """Programmable seam with an ORDERED (method, plan_handle) event log."""

    def __init__(self) -> None:
        self.plans: dict[str, PlanMetadata] = {}
        self.summaries: dict[str, str] = {}
        self.error: Exception | None = None
        self.events: list[tuple[str, str]] = []

    def plan_metadata(self, plan_handle: str) -> PlanMetadata:
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


def _grants(
    *families: str,
    ttl: int = 600,
    seats: tuple[str, ...] = ("persona_seat",),
) -> list[dict[str, object]]:
    return [
        {
            "family": family,
            "seat_classes": list(seats),
            "max_ttl_seconds": ttl,
        }
        for family in families
    ]


def base_policy() -> dict[str, object]:
    """Canonical valid v1 policy document (fresh dict per call).

    ``specialist-twin`` deliberately shares specialist's uid 1001: two registered
    subjects in ONE Unix trust domain (the gate-11 'deliberately the same
    trust domain' shape) — the uid-level separation tests key on it.
    """

    return {
        "schema": "pantheon-approval-policy/v1",
        "revision": "approver-policy-r1",
        "seat_classes": {"recovery": "persona_seat", "kino": "money_seat"},
        "subjects": {
            "specialist": 1001,
            "specialist-twin": 1001,
            "metis": 1002,
            "cairn": 1003,
            "hekate": 1004,
            "nyx": 1005,
            "human-operator": 1006,
            "operator": 2000,
        },
        "family_categories": {
            "persona.install": "seat_apply_reversible",
            "persona.verify": "diagnostics_read",
            "seat.repair": "cross_seat_repair",
        },
        "approvers": {
            "specialist": {
                "decision_source": "agent",
                "grants": _grants(
                    "persona.install", "persona.verify", "seat.repair"
                ),
            },
            "hekate": {
                "decision_source": "agent",
                "grants": _grants(
                    "persona.install", "persona.verify", "seat.repair"
                ),
            },
            "nyx": {
                "decision_source": "agent",
                "grants": _grants("persona.install"),
            },
            "metis": {
                "decision_source": "agent",
                "grants": _grants("persona.install"),
            },
            "human-operator": {
                "decision_source": "agent",
                "grants": _grants("persona.install", "seat.repair"),
            },
            "operator": {
                "decision_source": "human",
                "grants": _grants("persona.install"),
            },
        },
        "revokers": ["hekate"],
    }


class PolicyCaseBase(unittest.TestCase):
    """Temp-dir journal + spy seam + REAL PolicyApproverAuthorizer."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REPO_ROOT)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.journal_path = self.root / "approval.sqlite3"
        self.journal = ApprovalJournal.open(self.journal_path)
        self.handle_counter = itertools.count(0x2000)
        self.source = SpyMetadataSource()
        self.service = self.service_with()

    def tearDown(self) -> None:
        self.journal.close()
        self.temp.cleanup()

    def next_handle(self) -> str:
        return f"{next(self.handle_counter):064x}"

    def service_with(
        self,
        policy_dict: dict[str, object] | None = None,
        *,
        clock=None,
        grant_ttl: timedelta = timedelta(hours=1),
    ) -> ApprovalService:
        authorizer = PolicyApproverAuthorizer(
            parse_approver_policy(policy_dict or base_policy())
        )
        return ApprovalService(
            journal=self.journal,
            metadata_source=self.source,
            clock=clock or (lambda: FIXED_NOW),
            authorizer=authorizer,
            handle_factory=self.next_handle,
            registered_consumer_uids=frozenset((CONSUMER_UID,)),
            review_ttl=timedelta(hours=1),
            grant_ttl=grant_ttl,
        )

    def new_plan(
        self,
        *,
        owner: str = "cairn",
        performer: str | None = None,
        target: str = "recovery",
        families: tuple[str, ...] = (REAL_FAMILY,),
    ) -> str:
        plan_handle = self.next_handle()
        self.source.plans[plan_handle] = PlanMetadata(
            owner=owner,
            performer=owner if performer is None else performer,
            target_seat_id=target,
            operation_families=families,
            plan_digest=self.next_handle(),
            effect_digest=self.next_handle(),
            manifest_revision="manifest-r1",
            policy_revision="broker-policy-r1",
        )
        return plan_handle

    def aligned_request(self, plan_handle: str, owner: str = "metis") -> str:
        """A request plan matching plan_handle's full handoff tuple."""

        base = self.source.plans[plan_handle]
        request_handle = self.next_handle()
        self.source.plans[request_handle] = dataclasses.replace(
            base,
            owner=owner,
            performer=owner,
            plan_digest=self.next_handle(),
        )
        return request_handle

    def digest_of(self, plan_handle: str) -> str:
        return self.source.plans[plan_handle].plan_digest

    def open_review(
        self,
        approver: str,
        plan_handle: str,
        request_plan_handle: str | None = None,
        handoff_anchor: str | None = None,
        service: ApprovalService | None = None,
    ):
        service = service or self.service
        return service.review(
            approver,
            plan_handle,
            self.digest_of(plan_handle),
            request_plan_handle,
            handoff_anchor=handoff_anchor,
        )

    def decide(
        self,
        approver: str,
        review_handle: str,
        plan_handle: str,
        *,
        key: str,
        service: ApprovalService | None = None,
        anchor: str = "anchor-p",
    ):
        service = service or self.service
        return service.decide(
            approver,
            review_handle,
            self.digest_of(plan_handle),
            "granted",
            "approved",
            anchor,
            key,
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

    def assert_denied(
        self,
        reason_code: str,
        callable_,
        *args,
        **kwargs,
    ) -> ApproverDenied:
        with self.assertRaises(ApproverDenied) as caught:
            callable_(*args, **kwargs)
        self.assertEqual(caught.exception.reason_code, reason_code)
        return caught.exception

    def assert_handoff_mismatch(
        self,
        reason_code: str,
        callable_,
        *args,
        **kwargs,
    ) -> HandoffMismatch:
        with self.assertRaises(HandoffMismatch) as caught:
            callable_(*args, **kwargs)
        self.assertEqual(caught.exception.reason_code, reason_code)
        return caught.exception


class CategoryVocabularyTests(unittest.TestCase):
    def test_capability_categories_derive_from_org052_and_floor_is_fixed(
        self,
    ) -> None:
        # The §5 sentence enumerates seven human-gated capability classes;
        # the floor is exactly those seven, in code, underneath any policy
        # file. PROVISIONAL naming (flagged upward for ratification); the
        # DERIVATION comments sit beside each entry in policy.py.
        self.assertEqual(
            HUMAN_REQUIRED_CATEGORIES,
            frozenset(
                (
                    "unix_accounts",
                    "access_control",
                    "system_units",
                    "sudoers_fragments",
                    "credential_management",
                    "cross_seat_repair",
                    "destructive_rollback",
                )
            ),
        )
        self.assertEqual(CAPABILITY_CATEGORIES, frozenset(CATEGORY_CLASSES))
        self.assertEqual(APPROVAL_CLASSES, frozenset(("none", "agent", "human")))
        for category, decision_class in CATEGORY_CLASSES.items():
            self.assertIn(decision_class, APPROVAL_CLASSES, category)
        # every human-required category resolves to human, structurally
        for category in HUMAN_REQUIRED_CATEGORIES:
            self.assertEqual(CATEGORY_CLASSES[category], "human")
        # and the non-human remainder is exactly the two §5 relaxations
        self.assertEqual(
            CAPABILITY_CATEGORIES - HUMAN_REQUIRED_CATEGORIES,
            frozenset(("diagnostics_read", "seat_apply_reversible")),
        )


class PolicyParseTests(unittest.TestCase):
    def test_policy_parses_versioned_roster_happy_path(self) -> None:
        policy = parse_approver_policy(base_policy())
        self.assertIsInstance(policy, ApproverPolicy)
        self.assertEqual(policy.revision, "approver-policy-r1")
        self.assertEqual(policy.required_class("persona.install"), "agent")
        self.assertEqual(policy.required_class("persona.verify"), "none")
        self.assertEqual(policy.required_class("seat.repair"), "human")
        self.assertIsNone(policy.required_class("unmapped.family"))
        self.assertEqual(policy.uid_of("specialist"), 1001)
        self.assertEqual(policy.uid_of("specialist-twin"), 1001)
        self.assertIsNone(policy.uid_of("stranger"))
        entry = policy.approvers["specialist"]
        self.assertEqual(entry.decision_source, "agent")
        grant = entry.grants["persona.install"]
        self.assertEqual(grant.seat_classes, frozenset(("persona_seat",)))
        self.assertEqual(grant.max_ttl, timedelta(seconds=600))
        self.assertEqual(policy.revokers, frozenset(("hekate",)))

    def test_unknown_or_class_valued_category_refused(self) -> None:
        # The downgrade floor at parse tier: a category outside the closed
        # enum refuses — INCLUDING a decision-class token written where a
        # category belongs. The file maps family -> category as data; the
        # decision source derives from the category in code; there is no
        # slot in which a file can express "this family needs only agent".
        for bad in ("made_up", "agent", "human", "none", "*", ""):
            with self.subTest(category=bad):
                raw = base_policy()
                raw["family_categories"]["persona.install"] = bad
                with self.assertRaises(InvalidApproverPolicy):
                    parse_approver_policy(raw)

    def test_unknown_keys_refused_inheritance_inexpressible(self) -> None:
        # Exact key sets at every level: there is no key through which
        # inheritance, defaults, or a class map could enter.
        top_level = base_policy()
        top_level["family_classes"] = {"persona.install": "agent"}
        with_inherits = base_policy()
        with_inherits["inherits"] = "other-policy"
        with_default = base_policy()
        with_default["default"] = {"decision_source": "agent"}
        entry_extra = base_policy()
        entry_extra["approvers"]["specialist"]["wildcard"] = True
        grant_extra = base_policy()
        grant_extra["approvers"]["specialist"]["grants"][0]["inherit_from"] = "x"
        missing_key = base_policy()
        del missing_key["revokers"]
        for label, raw in (
            ("family_classes", top_level),
            ("inherits", with_inherits),
            ("default", with_default),
            ("entry_extra", entry_extra),
            ("grant_extra", grant_extra),
            ("missing_key", missing_key),
        ):
            with self.subTest(case=label):
                with self.assertRaises(InvalidApproverPolicy):
                    parse_approver_policy(raw)

    def test_wildcards_refused_everywhere(self) -> None:
        subject_star = base_policy()
        subject_star["subjects"]["*"] = 9999
        family_star = base_policy()
        family_star["family_categories"]["*"] = "seat_apply_reversible"
        grant_family_star = base_policy()
        grant_family_star["approvers"]["specialist"]["grants"][0]["family"] = "*"
        seat_star = base_policy()
        seat_star["seat_classes"]["*"] = "persona_seat"
        seat_class_star = base_policy()
        seat_class_star["seat_classes"]["recovery"] = "*"
        grant_seat_star = base_policy()
        grant_seat_star["approvers"]["specialist"]["grants"][0]["seat_classes"] = [
            "*"
        ]
        approver_star = base_policy()
        approver_star["subjects"]["star"] = 1
        approver_star["approvers"]["*"] = {
            "decision_source": "agent",
            "grants": _grants("persona.install"),
        }
        revoker_star = base_policy()
        revoker_star["revokers"] = ["*"]
        for label, raw in (
            ("subject", subject_star),
            ("family", family_star),
            ("grant_family", grant_family_star),
            ("seat_id", seat_star),
            ("seat_class", seat_class_star),
            ("grant_seat_class", grant_seat_star),
            ("approver", approver_star),
            ("revoker", revoker_star),
        ):
            with self.subTest(slot=label):
                with self.assertRaises(InvalidApproverPolicy):
                    parse_approver_policy(raw)

    def test_structural_faults_refused(self) -> None:
        def mutate(label: str, fn) -> tuple[str, dict[str, object]]:
            raw = base_policy()
            fn(raw)
            return label, raw

        cases = (
            mutate("wrong_schema", lambda r: r.update(schema="other/v9")),
            mutate("empty_revision", lambda r: r.update(revision="")),
            mutate(
                "approver_without_uid",
                lambda r: r["approvers"].update(
                    ghost={
                        "decision_source": "agent",
                        "grants": _grants("persona.install"),
                    }
                ),
            ),
            mutate(
                "grant_uncategorized_family",
                lambda r: r["approvers"]["specialist"]["grants"].append(
                    _grants("family.unmapped")[0]
                ),
            ),
            mutate(
                "duplicate_grant_family",
                lambda r: r["approvers"]["specialist"]["grants"].append(
                    _grants("persona.install")[0]
                ),
            ),
            mutate(
                "zero_ttl",
                lambda r: r["approvers"]["specialist"]["grants"][0].update(
                    max_ttl_seconds=0
                ),
            ),
            mutate(
                "negative_ttl",
                lambda r: r["approvers"]["specialist"]["grants"][0].update(
                    max_ttl_seconds=-5
                ),
            ),
            mutate(
                "bool_ttl",
                lambda r: r["approvers"]["specialist"]["grants"][0].update(
                    max_ttl_seconds=True
                ),
            ),
            mutate(
                "overbound_ttl",
                lambda r: r["approvers"]["specialist"]["grants"][0].update(
                    max_ttl_seconds=90 * 86400
                ),
            ),
            mutate(
                "bool_uid", lambda r: r["subjects"].update(specialist=True)
            ),
            mutate(
                "negative_uid", lambda r: r["subjects"].update(specialist=-1)
            ),
            mutate(
                "string_uid", lambda r: r["subjects"].update(specialist="1001")
            ),
            mutate(
                "bad_decision_source",
                lambda r: r["approvers"]["specialist"].update(
                    decision_source="operator"
                ),
            ),
            mutate(
                "revoker_not_subject",
                lambda r: r.update(revokers=["stranger"]),
            ),
            mutate(
                "empty_grant_seats",
                lambda r: r["approvers"]["specialist"]["grants"][0].update(
                    seat_classes=[]
                ),
            ),
            mutate("non_dict_subjects", lambda r: r.update(subjects=[])),
            mutate(
                "non_list_grants",
                lambda r: r["approvers"]["specialist"].update(
                    grants={"family": "persona.install"}
                ),
            ),
        )
        for label, raw in cases:
            with self.subTest(case=label):
                with self.assertRaises(InvalidApproverPolicy):
                    parse_approver_policy(raw)


class PolicyLoadTests(unittest.TestCase):
    """The root-owned file trust boundary (reused trusted_files, M4)."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REPO_ROOT)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.path = self.root / "approver-policy.json"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_policy(self, payload: object, mode: int = 0o600) -> None:
        text = (
            payload
            if isinstance(payload, str)
            else json.dumps(payload, sort_keys=True)
        )
        self.path.write_text(text, encoding="utf-8")
        self.path.chmod(mode)

    def loader(self, owner_uid: int | None = None) -> RootOwnedFileLoader:
        return RootOwnedFileLoader._open_for_test(
            (self.root,),
            expected_owner_uid=os.getuid() if owner_uid is None else owner_uid,
        )

    def test_policy_file_loads_through_root_owned_trust_boundary(self) -> None:
        self.write_policy(base_policy())
        with self.loader() as files:
            policy = load_approver_policy(files, self.path)
        self.assertEqual(policy.revision, "approver-policy-r1")

    def test_missing_policy_file_fails_closed_to_deny_all(self) -> None:
        # No file -> no policy object exists to authorize ANYTHING; and the
        # service's module default stays DENY_ALL (M11) — absence can never
        # become permission.
        with self.loader() as files:
            with self.assertRaises(RegistryError):
                load_approver_policy(files, self.path)
        self.assertIs(
            inspect.signature(ApprovalService.__init__)
            .parameters["authorizer"]
            .default,
            DENY_ALL_AUTHORIZER,
        )
        verdict = DENY_ALL_AUTHORIZER.may_review(
            "specialist",
            PlanMetadata(
                owner="cairn",
                performer="cairn",
                target_seat_id="recovery",
                operation_families=(REAL_FAMILY,),
                plan_digest="0" * 64,
                effect_digest="1" * 64,
                manifest_revision="manifest-r1",
                policy_revision="broker-policy-r1",
            ),
        )
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.reason_code, "deny_all_default")

    def test_malformed_policy_file_refused(self) -> None:
        for label, payload in (
            ("bad_json", "{not json"),
            ("non_object_root", "[1, 2]"),
            (
                "duplicate_keys",
                '{"schema": "pantheon-approval-policy/v1", '
                '"schema": "pantheon-approval-policy/v1"}',
            ),
        ):
            with self.subTest(case=label):
                self.write_policy(payload)
                with self.loader() as files:
                    with self.assertRaises(RegistryError):
                        load_approver_policy(files, self.path)
        # oversize refuses through the loader's size bound
        self.write_policy(base_policy())
        with self.loader() as files:
            with self.assertRaises(RegistryError):
                load_approver_policy(files, self.path, max_bytes=16)
        self.assertGreater(MAX_POLICY_BYTES, 0)

    def test_ownership_or_mode_validation_failure_refused(self) -> None:
        self.write_policy(base_policy(), mode=0o620)  # group-writable
        with self.loader() as files:
            with self.assertRaises(RegistryError):
                load_approver_policy(files, self.path)
        # wrong expected owner: the trust chain refuses at the root
        self.write_policy(base_policy())
        with self.assertRaises(RegistryError):
            RootOwnedFileLoader._open_for_test(
                (self.root,),
                expected_owner_uid=os.getuid() + 1,
            )


class RosterRoutingTests(PolicyCaseBase):
    def test_unknown_approver_denied(self) -> None:
        plan = self.new_plan()
        self.assert_denied(
            "unknown_approver", self.open_review, "stranger", plan
        )

    def test_unknown_operation_family_denied(self) -> None:
        # the family namespace is manifest data; an UNMAPPED family is a
        # denial, never a permit (M11)
        plan = self.new_plan(families=("family.unmapped",))
        self.assert_denied(
            "unknown_operation_family", self.open_review, "specialist", plan
        )

    def test_family_not_granted_denied(self) -> None:
        plan = self.new_plan(families=(REAL_FAMILY, "persona.verify"))
        # nyx holds persona.install only; the plan carries BOTH families
        self.assert_denied(
            "family_not_granted", self.open_review, "nyx", plan
        )

    def test_unknown_target_seat_denied(self) -> None:
        plan = self.new_plan(target="unregistered-seat")
        self.assert_denied(
            "unknown_target_seat", self.open_review, "specialist", plan
        )

    def test_seat_class_not_granted_denied(self) -> None:
        # kino maps to money_seat; every grant in the fixture is scoped to
        # persona_seat only
        plan = self.new_plan(target="kino")
        self.assert_denied(
            "seat_class_not_granted", self.open_review, "specialist", plan
        )

    def test_none_class_family_agent_decision_allowed(self) -> None:
        # diagnostics_read routes 'none': pre-authorizable, so an agent
        # decision (none <= agent) is permitted
        plan = self.new_plan(families=("persona.verify",))
        review = self.open_review("specialist", plan).record
        result = self.decide(
            "specialist", review.review_handle, plan, key="decide-none-1"
        )
        self.assertEqual(result.state, "granted")


class SeparationOfDutyTests(PolicyCaseBase):
    def test_approver_equal_to_plan_owner_denied(self) -> None:
        plan = self.new_plan(owner="specialist")
        self.assert_denied(
            "approver_is_plan_owner", self.open_review, "specialist", plan
        )

    def test_approver_equal_to_performer_denied(self) -> None:
        plan = self.new_plan(owner="cairn", performer="specialist")
        self.assert_denied(
            "approver_is_performer", self.open_review, "specialist", plan
        )

    def test_approver_sharing_uid_trust_domain_denied(self) -> None:
        # specialist-twin shares uid 1001 with specialist: distinct subject names,
        # ONE Unix trust domain (gate 11's "deliberately the same trust
        # domain"). Both owner-side and performer-side.
        owner_side = self.new_plan(owner="specialist-twin")
        self.assert_denied(
            "approver_shares_trust_domain",
            self.open_review,
            "specialist",
            owner_side,
        )
        performer_side = self.new_plan(owner="cairn", performer="specialist-twin")
        self.assert_denied(
            "approver_shares_trust_domain",
            self.open_review,
            "specialist",
            performer_side,
        )

    def test_unknown_subject_trust_domain_fails_closed(self) -> None:
        # the policy cannot resolve the owner's uid, so the separation
        # check is UNVERIFIABLE -> denial, never benefit of the doubt
        plan = self.new_plan(owner="stranger")
        self.assert_denied(
            "unknown_subject", self.open_review, "specialist", plan
        )

    def test_approver_equal_to_request_plan_owner_denied(self) -> None:
        plan = self.new_plan()
        request = self.aligned_request(plan, owner="specialist")
        self.assert_denied(
            "approver_is_request_plan_owner",
            self.open_review,
            "specialist",
            plan,
            request,
        )

    def test_approver_sharing_uid_with_request_plan_owner_denied(self) -> None:
        plan = self.new_plan()
        request = self.aligned_request(plan, owner="specialist-twin")
        self.assert_denied(
            "approver_shares_trust_domain",
            self.open_review,
            "specialist",
            plan,
            request,
        )

    def test_third_party_chain_participant_cannot_approve_sibling(self) -> None:
        # Chain: request plan RQ (owner metis) already links a review whose
        # performer plan is owned by NYX. A SECOND performer plan (owner
        # cairn) joins the same chain; nyx — a participant in the chain via
        # the first review — attempts to review and to decide the sibling.
        # Both are attacked and DENIED (gate 13 "any participant in the
        # same server-linked request-plan handoff").
        plan_one = self.new_plan(owner="nyx")
        request = self.aligned_request(plan_one, owner="metis")
        self.open_review("hekate", plan_one, request)
        plan_two = self.new_plan(owner="cairn")
        # align the second plan to the SAME request plan tuple
        self.source.plans[plan_two] = dataclasses.replace(
            self.source.plans[request],
            owner="cairn",
            performer="cairn",
            plan_digest=self.digest_of(plan_two),
        )
        self.assert_denied(
            "approver_is_handoff_participant",
            self.open_review,
            "nyx",
            plan_two,
            request,
        )
        # and at DECIDE time (the authoritative gate; chain re-queried)
        review_two = self.open_review("hekate", plan_two, request).record
        self.assert_denied(
            "approver_is_handoff_participant",
            self.decide,
            "nyx",
            review_two.review_handle,
            plan_two,
            key="decide-chain-1",
        )

    def test_reciprocal_pair_approvals_inside_linked_chain_denied(self) -> None:
        # A(specialist) requests, B(hekate) performs — and reciprocally. Two
        # personas can NEVER approve one another inside the same request
        # chain: every edge of the reciprocal pair denies with its own
        # code (§5: a future reciprocal policy would need a distinct
        # ratified rule; it is not present initially).
        plan_b = self.new_plan(owner="hekate")
        request_a = self.aligned_request(plan_b, owner="specialist")
        plan_a = self.new_plan(owner="specialist")
        request_b = self.aligned_request(plan_a, owner="hekate")
        self.assert_denied(
            "approver_is_request_plan_owner",
            self.open_review,
            "specialist",
            plan_b,
            request_a,
        )
        self.assert_denied(
            "approver_is_plan_owner",
            self.open_review,
            "hekate",
            plan_b,
            request_a,
        )
        self.assert_denied(
            "approver_is_request_plan_owner",
            self.open_review,
            "hekate",
            plan_a,
            request_b,
        )
        self.assert_denied(
            "approver_is_plan_owner",
            self.open_review,
            "specialist",
            plan_a,
            request_b,
        )

    def test_circular_approval_via_chain_closure_denied(self) -> None:
        # The circle: metis requests RQ; nyx performs P1 (granted by
        # hekate); now every member of the closed chain — the requester,
        # the performer, and any same-uid shadow — is barred from
        # approving anything else in that chain.
        plan_one = self.new_plan(owner="nyx")
        request = self.aligned_request(plan_one, owner="metis")
        review_one = self.open_review("hekate", plan_one, request).record
        self.decide(
            "hekate", review_one.review_handle, plan_one, key="decide-circ-1"
        )
        plan_two = self.new_plan(owner="cairn")
        self.source.plans[plan_two] = dataclasses.replace(
            self.source.plans[request],
            owner="cairn",
            performer="cairn",
            plan_digest=self.digest_of(plan_two),
        )
        self.assert_denied(
            "approver_is_request_plan_owner",
            self.open_review,
            "metis",
            plan_two,
            request,
        )
        self.assert_denied(
            "approver_is_handoff_participant",
            self.open_review,
            "nyx",
            plan_two,
            request,
        )

    def test_recipient_mismatched_approval_denied(self) -> None:
        # gate 13 "recipient-mismatched": the recipient's own plan must
        # MATCH the request tuple — target, manifest revision, operation
        # families, effect digest — each mismatch with its own code.
        plan = self.new_plan()
        base_request = self.source.plans[self.aligned_request(plan)]
        cases = (
            ("handoff_target_mismatch", {"target_seat_id": "kino"}),
            ("handoff_manifest_mismatch", {"manifest_revision": "manifest-r2"}),
            (
                "handoff_family_mismatch",
                {"operation_families": (REAL_FAMILY, "persona.verify")},
            ),
            ("handoff_effect_mismatch", {"effect_digest": "e" * 64}),
        )
        for code, changes in cases:
            with self.subTest(code=code):
                request_handle = self.next_handle()
                self.source.plans[request_handle] = dataclasses.replace(
                    base_request, **changes
                )
                self.assert_handoff_mismatch(
                    code,
                    self.open_review,
                    "hekate",
                    plan,
                    request_handle,
                )


class AntiDowngradeTests(PolicyCaseBase):
    """Locked input M1: the four downgrade vectors, each attacked."""

    def human_gated_policy(self) -> dict[str, object]:
        # ANTI-VACUITY ANCHOR (PO override 1): the REAL landed family id is
        # re-categorized human-gated; the gate must hold against reality,
        # not only against invented names.
        raw = base_policy()
        raw["family_categories"][REAL_FAMILY] = "cross_seat_repair"
        return raw

    def test_human_gated_family_denies_agent_decision_real_family(self) -> None:
        service = self.service_with(self.human_gated_policy())
        plan = self.new_plan()  # families=(persona.install,)
        # review-open is PERMITTED (that is how the T-2919 RequestHuman
        # flow starts); the human gate lands on decide — grant AND deny
        # alike are agent-path decisions and both refuse
        review = self.open_review("specialist", plan, service=service).record
        self.assert_denied(
            "human_decision_required",
            self.decide,
            "specialist",
            review.review_handle,
            plan,
            key="decide-hg-1",
            service=service,
        )
        with self.assertRaises(ApproverDenied):
            service.decide(
                "specialist",
                review.review_handle,
                self.digest_of(plan),
                "denied",
                "rejected",
                "anchor-p",
                "decide-hg-2",
            )
        # nothing was minted, the review is still open
        self.assertEqual(
            self.review_row(review.review_handle)["state"], "open"
        )
        with sqlite3.connect(self.journal_path) as connection:
            grants = connection.execute(
                "SELECT count(*) FROM grants"
            ).fetchone()[0]
        self.assertEqual(grants, 0)

    def test_handoff_cannot_downgrade_human_gate(self) -> None:
        # gate 23: "Human-gated capability families remain human-gated
        # after handoff." The handoff linkage changes NOTHING about the
        # required decision source.
        service = self.service_with(self.human_gated_policy())
        plan = self.new_plan()
        request = self.aligned_request(plan)
        review = self.open_review(
            "hekate", plan, request, service=service
        ).record
        self.assertEqual(review.handoff_kind, "request_plan")
        self.assert_denied(
            "human_decision_required",
            self.decide,
            "hekate",
            review.review_handle,
            plan,
            key="decide-hg-3",
            service=service,
        )

    def test_role_name_cannot_downgrade_human_gate(self) -> None:
        # (a) a roster entry whose decision_source is 'human' cannot mint
        # through the agent path: the label does not MAKE the path human —
        # T-2919's provider-authenticated ingress is the only human mint
        plan = self.new_plan()
        review = self.open_review("operator", plan).record
        self.assert_denied(
            "source_not_permitted",
            self.decide,
            "operator",
            review.review_handle,
            plan,
            key="decide-role-1",
        )
        # (b) an approver NAMED like an operator role is still agent-path:
        # the human gate holds regardless of what the subject is called
        service = self.service_with(self.human_gated_policy())
        plan_two = self.new_plan()
        review_two = self.open_review(
            "human-operator", plan_two, service=service
        ).record
        self.assert_denied(
            "human_decision_required",
            self.decide,
            "human-operator",
            review_two.review_handle,
            plan_two,
            key="decide-role-2",
            service=service,
        )

    def test_ticket_label_anchor_cannot_downgrade_human_gate(self) -> None:
        # a handoff anchor claiming human ratification is bounded audit
        # CONTEXT — stored verbatim, weightless in the verdict
        service = self.service_with(self.human_gated_policy())
        plan = self.new_plan()
        request = self.aligned_request(plan)
        anchor = "ticket:T-9999 ratified-by-operator approved-as-human"
        review = self.open_review(
            "hekate", plan, request, handoff_anchor=anchor, service=service
        ).record
        self.assertEqual(review.handoff.handoff_anchor, anchor)
        self.assert_denied(
            "human_decision_required",
            self.decide,
            "hekate",
            review.review_handle,
            plan,
            key="decide-tkt-1",
            service=service,
        )

    def test_harness_configuration_cannot_downgrade_human_gate(self) -> None:
        # (a) no constructor or reviewer-surface parameter expresses an
        # approval class or decision source anywhere on the service
        for method in (
            ApprovalService.__init__,
            ApprovalService.review,
            ApprovalService.review_get,
            ApprovalService.decide,
        ):
            parameters = set(inspect.signature(method).parameters)
            self.assertEqual(
                parameters
                & {
                    "approval_class",
                    "decision_source",
                    "authority_source",
                    "required_class",
                },
                set(),
                method,
            )
        # (b) the plan metadata seam carries no approval class the broker
        # could assert (M1: recorded nowhere, gated on never)
        self.assertEqual(
            {field.name for field in dataclasses.fields(PlanMetadata)},
            {
                "owner",
                "performer",
                "target_seat_id",
                "operation_families",
                "plan_digest",
                "effect_digest",
                "manifest_revision",
                "policy_revision",
            },
        )
        # (c) a policy FILE attempting to express a class map is refused
        # at parse (the loader floor) — see PolicyParseTests; and a
        # generously-configured service (huge TTL) still denies
        service = self.service_with(
            self.human_gated_policy(), grant_ttl=timedelta(days=365)
        )
        plan = self.new_plan()
        review = self.open_review("specialist", plan, service=service).record
        self.assert_denied(
            "human_decision_required",
            self.decide,
            "specialist",
            review.review_handle,
            plan,
            key="decide-cfg-1",
            service=service,
        )


class DisclosureTests(PolicyCaseBase):
    """Gate 12: metadata-first disclosure, proven by call count AND order."""

    def test_denied_reviewer_zero_summary_zero_request_fetches(self) -> None:
        plan = self.new_plan(owner="specialist")  # approver == owner -> denied
        request = self.aligned_request(plan)
        self.source.summaries[plan] = "SECRET-EFFECTS-TOPOLOGY"
        exception = self.assert_denied(
            "approver_is_plan_owner",
            self.open_review,
            "specialist",
            plan,
            request,
        )
        # THE gate-12 proof: the seam saw exactly ONE minimal-metadata
        # fetch for the performer plan — zero plan_summary calls, zero
        # request-plan fetches. Count and order, not withheld returns.
        self.assertEqual(self.source.events, [("plan_metadata", plan)])
        # and the denial leaks neither effects nor topology
        self.assertNotIn("SECRET-EFFECTS-TOPOLOGY", str(exception))
        self.assertNotIn(request, str(exception))
        # unknown approver: same zero-disclosure shape
        self.source.events.clear()
        self.assert_denied(
            "unknown_approver", self.open_review, "stranger", plan, request
        )
        self.assertEqual(self.source.events, [("plan_metadata", plan)])

    def test_authorized_review_fetches_summary_last(self) -> None:
        plan = self.new_plan()
        request = self.aligned_request(plan)
        self.source.summaries[plan] = "redacted summary for approver"
        result = self.open_review("hekate", plan, request)
        self.assertEqual(result.plan_summary, "redacted summary for approver")
        # exact event sequence: performer metadata -> request metadata ->
        # summary LAST, exactly once
        self.assertEqual(
            self.source.events,
            [
                ("plan_metadata", plan),
                ("plan_metadata", request),
                ("plan_summary", plan),
            ],
        )

    def test_handoff_link_mismatch_refuses_before_summary(self) -> None:
        plan = self.new_plan()
        request_handle = self.next_handle()
        self.source.plans[request_handle] = dataclasses.replace(
            self.source.plans[self.aligned_request(plan)],
            target_seat_id="kino",
        )
        self.source.events.clear()
        self.assert_handoff_mismatch(
            "handoff_target_mismatch",
            self.open_review,
            "hekate",
            plan,
            request_handle,
        )
        self.assertEqual(
            [event for event in self.source.events if event[0] == "plan_summary"],
            [],
        )


class HandoffLinkTests(PolicyCaseBase):
    """Gate 23, the linking/binding half owned by this unit."""

    def test_identity_derived_only_from_server_owned_plan_owners(self) -> None:
        # requester identity = request plan's owner as the BROKER attests
        # it; there exists no parameter through which a caller could
        # assert requester or performer identity
        plan = self.new_plan()
        request = self.aligned_request(plan, owner="metis")
        review = self.open_review("hekate", plan, request).record
        self.assertEqual(review.handoff.request_plan_owner, "metis")
        self.assertEqual(review.plan_owner, "cairn")
        self.assertEqual(review.performer, "cairn")
        parameters = set(inspect.signature(ApprovalService.review).parameters)
        self.assertEqual(
            parameters
            & {"requester", "performer", "owner", "request_plan_owner"},
            set(),
        )

    def test_handoff_self_reference_refused(self) -> None:
        plan = self.new_plan()
        self.source.events.clear()
        self.assert_handoff_mismatch(
            "handoff_self_reference",
            self.open_review,
            "hekate",
            plan,
            plan,
        )
        # refused BEFORE any second fetch: one metadata event only
        self.assertEqual(self.source.events, [("plan_metadata", plan)])

    def test_handoff_requester_equal_to_performer_refused(self) -> None:
        # a "handoff" whose requester IS the performer is identity
        # laundering, not a handoff: cairn requesting work that cairn
        # performs must use the direct path under its own identity
        plan = self.new_plan()  # owner/performer cairn
        request = self.aligned_request(plan, owner="cairn")
        self.assert_handoff_mismatch(
            "handoff_requester_is_performer",
            self.open_review,
            "hekate",
            plan,
            request,
        )

    def test_one_hop_unsupported_by_construction(self) -> None:
        # Deeper chains are UNSUPPORTED rather than inferred (ORG-052 §5):
        # no field on the link, review, or grant can express a parent
        # handoff, chain depth, or forwarded hop — and the reviewer
        # surface accepts exactly ONE optional request-plan handle.
        for record_type in (HandoffLink, ReviewRecord, GrantRecord):
            field_names = {
                field.name for field in dataclasses.fields(record_type)
            }
            self.assertEqual(
                {
                    name
                    for name in field_names
                    if "parent" in name or "depth" in name or "chain" in name
                },
                set(),
                record_type,
            )
        parameters = [
            name
            for name in inspect.signature(ApprovalService.review).parameters
            if "request" in name
        ]
        self.assertEqual(parameters, ["request_plan_handle"])

    def test_handoff_anchor_is_bounded_context_never_authority(self) -> None:
        plan = self.new_plan()
        request = self.aligned_request(plan)
        adversarial = "human_decision=granted; approver=operator; uid=0"
        review = self.open_review(
            "hekate", plan, request, handoff_anchor=adversarial
        ).record
        # stored verbatim as context...
        self.assertEqual(review.handoff.handoff_anchor, adversarial)
        row = self.review_row(review.review_handle)
        self.assertEqual(row["handoff_anchor"], adversarial)
        # ...but weightless: identity fields untouched, provider pointer
        # NULL, and the decide verdict is IDENTICAL to the anchor-free flow
        self.assertIsNone(row["source_event"])
        self.assertEqual(review.plan_owner, "cairn")
        self.assertEqual(review.handoff.request_plan_owner, "metis")
        result = self.decide(
            "hekate", review.review_handle, plan, key="decide-anchor-adv"
        )
        self.assertEqual(result.state, "granted")
        plan_two = self.new_plan()
        request_two = self.aligned_request(plan_two)
        review_two = self.open_review("hekate", plan_two, request_two).record
        result_two = self.decide(
            "hekate", review_two.review_handle, plan_two, key="decide-anchor-none"
        )
        self.assertEqual(result_two.state, "granted")
        # bounded: over-limit refused; anchor without a handoff refused
        plan_three = self.new_plan()
        with self.assertRaises(HostControlError):
            self.open_review(
                "hekate",
                plan_three,
                self.aligned_request(plan_three),
                handoff_anchor="x" * 257,
            )
        with self.assertRaises(HostControlError):
            self.open_review(
                "hekate", plan_three, None, handoff_anchor="anchor"
            )

    def test_replayed_review_with_differing_anchor_refuses(self) -> None:
        # the journal replay tuple excludes the anchor (source-verified),
        # so without this refusal a replay would silently swallow
        # different context — the M6 shape at the review tier
        plan = self.new_plan()
        request = self.aligned_request(plan)
        first = self.open_review(
            "hekate", plan, request, handoff_anchor="ticket:T-1"
        )
        self.assertFalse(first.replayed)
        replay = self.open_review(
            "hekate", plan, request, handoff_anchor="ticket:T-1"
        )
        self.assertTrue(replay.replayed)
        with self.assertRaises(IdempotencyConflict):
            self.open_review(
                "hekate", plan, request, handoff_anchor="ticket:T-2"
            )
        self.assertEqual(
            self.review_row(first.record.review_handle)["handoff_anchor"],
            "ticket:T-1",
        )

    def test_grant_binds_full_handoff_tuple_and_anchor(self) -> None:
        # the issued grant durably binds both plans and digests, the
        # broker-attested request-plan owner, the shared effect digest,
        # and the bounded anchor (gate 23)
        plan = self.new_plan()
        request = self.aligned_request(plan)
        review = self.open_review(
            "hekate", plan, request, handoff_anchor="ticket:T-2911"
        ).record
        result = self.decide(
            "hekate", review.review_handle, plan, key="decide-bind-1"
        )
        row = self.grant_row(result.approval_handle)
        self.assertEqual(row["handoff_kind"], "request_plan")
        self.assertEqual(row["request_plan_handle"], request)
        self.assertEqual(
            row["request_plan_digest"], self.digest_of(request)
        )
        self.assertEqual(row["request_plan_owner"], "metis")
        self.assertEqual(
            row["shared_effect_digest"],
            self.source.plans[plan].effect_digest,
        )
        self.assertEqual(row["handoff_anchor"], "ticket:T-2911")
        self.assertEqual(row["plan_handle"], plan)
        self.assertEqual(row["plan_owner"], "cairn")
        self.assertEqual(row["performer"], "cairn")

    def test_bind_vanished_request_plan_raises_grant_stays_issued(self) -> None:
        # PO override 2: the request-plan handle comes from the GRANT
        # record, not the caller — a vanished server-owned plan is
        # broker-side inconsistency and must be LOUD (UnknownHandle), not
        # a denial that would terminally refuse the broker's operation.
        # The grant stays ISSUED and retryable.
        plan = self.new_plan()
        request = self.aligned_request(plan)
        review = self.open_review("hekate", plan, request).record
        result = self.decide(
            "hekate", review.review_handle, plan, key="decide-vanish-1"
        )
        del self.source.plans[request]
        with self.assertRaises(UnknownHandle):
            self.service.validate_and_bind(
                CONSUMER_UID,
                result.approval_handle,
                plan,
                self.digest_of(plan),
                self.next_handle(),
            )
        self.assertEqual(
            self.grant_row(result.approval_handle)["state"], "issued"
        )

    def test_direct_grant_bind_makes_no_request_plan_fetches(self) -> None:
        plan = self.new_plan()
        review = self.open_review("hekate", plan).record
        result = self.decide(
            "hekate", review.review_handle, plan, key="decide-direct-1"
        )
        self.source.events.clear()
        outcome = self.service.validate_and_bind(
            CONSUMER_UID,
            result.approval_handle,
            plan,
            self.digest_of(plan),
            self.next_handle(),
        )
        self.assertEqual(outcome.state, "bound")
        self.assertEqual(self.source.events, [("plan_metadata", plan)])

    def test_chain_subjects_scoped_to_exactly_one_request_plan(self) -> None:
        # PO OQ4 requirement: the journal chain query never returns
        # participants from a DIFFERENT request plan; and it counts
        # participants regardless of review state (fail-closed, OQ8).
        plan_one = self.new_plan(owner="nyx")
        request_one = self.aligned_request(plan_one, owner="metis")
        review_one = self.open_review("hekate", plan_one, request_one).record
        # terminal state: deny it — participants must STILL count
        self.service.decide(
            "hekate",
            review_one.review_handle,
            self.digest_of(plan_one),
            "denied",
            "rejected",
            "anchor-p",
            "decide-scope-1",
        )
        plan_two = self.new_plan(owner="cairn")
        request_two = self.aligned_request(plan_two, owner="specialist")
        self.open_review("hekate", plan_two, request_two)
        chain_one = self.journal.handoff_chain_subjects(request_one)
        chain_two = self.journal.handoff_chain_subjects(request_two)
        self.assertEqual(chain_one, frozenset(("nyx", "metis")))
        self.assertEqual(chain_two, frozenset(("cairn", "specialist")))
        self.assertEqual(
            self.journal.handoff_chain_subjects(self.next_handle()),
            frozenset(),
        )


class TtlClampTests(PolicyCaseBase):
    def test_policy_max_ttl_clamps_grant_expiry(self) -> None:
        # ORG-052 §5: policy declares a MAXIMUM TTL; the issued grant gets
        # the TIGHTER of the policy ceiling and the service's configured
        # TTL — in both directions.
        plan = self.new_plan()
        review = self.open_review("specialist", plan).record
        result = self.decide(
            "specialist", review.review_handle, plan, key="decide-ttl-1"
        )
        row = self.grant_row(result.approval_handle)
        # policy 600s < service 3600s: policy wins
        self.assertEqual(
            row["expires_at"],
            (FIXED_NOW + timedelta(seconds=600)).isoformat(),
        )
        # service 300s < policy 600s: service wins
        tight_service = self.service_with(grant_ttl=timedelta(seconds=300))
        plan_two = self.new_plan()
        review_two = self.open_review(
            "specialist", plan_two, service=tight_service
        ).record
        result_two = self.decide(
            "specialist",
            review_two.review_handle,
            plan_two,
            key="decide-ttl-2",
            service=tight_service,
        )
        self.assertEqual(
            self.grant_row(result_two.approval_handle)["expires_at"],
            (FIXED_NOW + timedelta(seconds=300)).isoformat(),
        )
        # multi-family plan: the MINIMUM family ceiling governs
        raw = base_policy()
        raw["approvers"]["specialist"]["grants"] = [
            {
                "family": "persona.install",
                "seat_classes": ["persona_seat"],
                "max_ttl_seconds": 600,
            },
            {
                "family": "persona.verify",
                "seat_classes": ["persona_seat"],
                "max_ttl_seconds": 120,
            },
        ]
        service = self.service_with(raw)
        plan_three = self.new_plan(
            families=("persona.install", "persona.verify")
        )
        review_three = self.open_review(
            "specialist", plan_three, service=service
        ).record
        result_three = self.decide(
            "specialist",
            review_three.review_handle,
            plan_three,
            key="decide-ttl-3",
            service=service,
        )
        self.assertEqual(
            self.grant_row(result_three.approval_handle)["expires_at"],
            (FIXED_NOW + timedelta(seconds=120)).isoformat(),
        )

    def test_clamped_grant_expires_through_normal_lazy_path(self) -> None:
        # composition: the clamp reuses the SAME expires_at surface, so a
        # clamped grant expires through the existing lazy/durable path
        # with zero new machinery
        plan = self.new_plan()
        review = self.open_review("specialist", plan).record
        result = self.decide(
            "specialist", review.review_handle, plan, key="decide-ttl-4"
        )
        late = self.service_with(
            clock=lambda: FIXED_NOW + timedelta(seconds=601)
        )
        outcome = late.validate_and_bind(
            CONSUMER_UID,
            result.approval_handle,
            plan,
            self.digest_of(plan),
            self.next_handle(),
        )
        self.assertEqual(outcome.state, "denied")
        self.assertEqual(outcome.reason_code, "expired")
        self.assertEqual(
            self.grant_row(result.approval_handle)["state"], "expired"
        )


class DecisionAuditTests(PolicyCaseBase):
    def test_decision_event_records_approver_policy_revision(self) -> None:
        # P4: WHICH approver-policy version authorized a decision must be
        # reconstructable from the audit trail. The grant record's
        # policy_revision field is the BROKER's revision (from plan
        # metadata); the approver-policy revision rides the decision
        # event payload — no record-schema churn.
        plan = self.new_plan()
        review = self.open_review("specialist", plan).record
        self.decide("specialist", review.review_handle, plan, key="decide-audit-1")
        with sqlite3.connect(self.journal_path) as connection:
            payloads = [
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT payload_json FROM review_events "
                    "WHERE review_handle=? ORDER BY seq",
                    (review.review_handle,),
                ).fetchall()
            ]
        decided = [p for p in payloads if "decision" in p]
        self.assertEqual(len(decided), 1)
        self.assertEqual(
            decided[0]["approver_policy_revision"], "approver-policy-r1"
        )


class VerdictContractTests(unittest.TestCase):
    def test_policy_verdict_reason_codes_closed_at_construction(self) -> None:
        # P1: the closed set is enforced structurally — no authorizer can
        # smuggle an unregistered code, no allowed verdict can carry a
        # denial code, no denied verdict can carry a TTL
        PolicyVerdict(True, "authorized")
        PolicyVerdict(False, "unknown_approver")
        with self.assertRaises(HostControlError):
            PolicyVerdict(False, "some_new_code")
        with self.assertRaises(HostControlError):
            PolicyVerdict(True, "unknown_approver")
        with self.assertRaises(HostControlError):
            PolicyVerdict(False, "unknown_approver", max_ttl=timedelta(60))
        with self.assertRaises(HostControlError):
            ApproverDenied("x", reason_code="not_a_code")
        with self.assertRaises(HostControlError):
            HandoffMismatch("x", reason_code="not_a_code")
        self.assertIn("deny_all_default", POLICY_DENIAL_REASON_CODES)
        self.assertEqual(len(HANDOFF_LINK_REASON_CODES), 6)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
