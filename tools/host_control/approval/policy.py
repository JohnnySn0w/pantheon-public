"""Root-owned approver policy: roster, categories, separation of duty.

ORG-052 §5 ("Approver policy and separation of duty") — T-2911.

Root-owned, versioned policy maps each approver subject to exact operation
families, target seat classes, maximum TTL, and permitted decision source.
No implicit wildcard, no inheritance: the parser accepts exact key sets
only, so an inherited or wildcard grant is inexpressible, not merely
rejected.

Vocabulary split (PO override 1, T-2911): the operation-family NAMESPACE is
root-owned MANIFEST DATA — the broker's policy receives
``host_operation_families`` as a frozenset sourced from trusted files, and
no family identifier is hard-coded anywhere in the broker tree. This module
therefore does NOT hard-code family identifiers either (a code-level floor
keyed on invented family names would be vacuous against every real plan).
The closed vocabulary this module owns is the capability-CATEGORY enum
below, derived clause-by-clause from the §5 sentence; the root-owned policy
file maps each family to a category AS DATA, and the required decision
source derives from the category IN CODE — one source of truth, no second
declaration to disagree with the first.

Fail-closed semantics (locked input M11): an absent mapping is a DENIAL,
never a permit. Unknown approver subject, unknown or unmapped operation
family, unknown target seat, missing policy file, malformed policy, a
policy file failing root-ownership or mode validation, and any attempt to
express a wildcard or inherited grant all DENY. The shipped
``DENY_ALL_AUTHORIZER`` remains the service's module default; the real
authorizer here is opt-in by construction.

File trust: policy files are read through the shared
``tools.host_control.trusted_files.RootOwnedFileLoader`` (reclassified as
shared substrate per the T-2911 dispatch, M4 — it is a pure root-owned-file
validator carrying no journal, plan, operation, or receipt surface).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Literal, Mapping

from tools.host_control.approval.contracts import (
    GrantRecord,
    PlanMetadata,
    PolicyVerdict,
    ReviewRecord,
)
from tools.host_control.contracts import HostControlError
from tools.host_control.trusted_files import RootOwnedFileLoader


POLICY_SCHEMA = "pantheon-approval-policy/v1"

# Bounds — empirical, not ADR-fixed.
MAX_POLICY_BYTES = 65536
MAX_POLICY_TTL = timedelta(days=30)

# ---------------------------------------------------------------------------
# Closed capability-category vocabulary (ORG-052 §5).
#
# PROVISIONAL-PENDING-RATIFICATION: the category NAMES are minted here (no
# canonical enum existed anywhere in the tree); the PO is flagging the
# naming upward for ratification visibility. The DERIVATION is auditable:
# each entry carries the exact §5 phrase it comes from. The human-required
# set is a FLOOR, not a closed universe — future executor units may add
# families (data) without a code change, and may add categories only by
# amending this closed enum.
#
# The mapping category -> required decision source lives HERE, in code:
# the policy file cannot express a decision source at all, which is the
# strongest form of "a human-gated family cannot be downgraded by harness
# configuration" (gate 13 / locked input M1).
# ---------------------------------------------------------------------------
CATEGORY_CLASSES: Mapping[str, str] = {
    # §5: "Read-only diagnostics and planning may be pre-authorized by
    # role policy."
    "diagnostics_read": "none",
    # §5: "Reversible current-seat apply operations may use a distinct
    # orchestrator-issued grant when written policy permits it."
    "seat_apply_reversible": "agent",
    # §5: "Unix accounts ... require the human decision source in the
    # initial policy."
    "unix_accounts": "human",
    # §5: "group/ACL/linger changes"
    "access_control": "human",
    # §5: "system-level units"
    "system_units": "human",
    # §5: "sudoers fragments"
    "sudoers_fragments": "human",
    # §5: "credential installation/rotation"
    "credential_management": "human",
    # §5: "cross-seat repair"
    "cross_seat_repair": "human",
    # §5: "destructive rollback"
    "destructive_rollback": "human",
}
CAPABILITY_CATEGORIES = frozenset(CATEGORY_CLASSES)
APPROVAL_CLASSES = frozenset(("none", "agent", "human"))
HUMAN_REQUIRED_CATEGORIES = frozenset(
    category
    for category, decision_class in CATEGORY_CLASSES.items()
    if decision_class == "human"
)
# Strictness order for multi-family plans: the STRICTEST class governs.
APPROVAL_CLASS_ORDER: Mapping[str, int] = {"none": 0, "agent": 1, "human": 2}

# Import-time invariant, not a runtime check: every category resolves to a
# known class. A drifted edit fails at import, before any test runs.
for _category, _class in CATEGORY_CLASSES.items():
    if _class not in APPROVAL_CLASSES:
        raise AssertionError(f"category {_category} has unknown class")
del _category, _class


class InvalidApproverPolicy(HostControlError):
    """The policy document is semantically invalid; nothing loads.

    Fail-closed: a service that cannot load its policy keeps (or falls
    back to) the DENY_ALL default. Distinct from ``RegistryError``, which
    the trusted-file loader raises for trust/IO/JSON faults.
    """


@dataclass(frozen=True)
class FamilyGrant:
    """One approver's terms for ONE exact operation family."""

    family: str
    seat_classes: frozenset[str]
    max_ttl: timedelta


@dataclass(frozen=True)
class ApproverEntry:
    """One approver subject's roster entry.

    ``decision_source`` is the source this subject's decisions are
    PERMITTED to count as. In this unit only ``agent``-path decisions
    exist; a ``human`` approver's entries are standing for T-2919's
    provider-authenticated ingress and DENY through the agent path
    (``source_not_permitted``) — a roster label can never make an
    agent-path call count as human.
    """

    subject: str
    decision_source: Literal["agent", "human"]
    grants: Mapping[str, FamilyGrant]  # keyed by exact family


@dataclass(frozen=True)
class ApproverPolicy:
    """Parsed, validated, immutable approver policy (versioned)."""

    schema: str
    revision: str
    seat_classes: Mapping[str, str]  # seat id -> seat class (root-owned view)
    subject_uids: Mapping[str, int]  # ALL known subjects -> Unix uid domain
    family_categories: Mapping[str, str]  # family -> capability category
    approvers: Mapping[str, ApproverEntry]
    revokers: frozenset[str]

    def required_class(self, family: str) -> str | None:
        """Required decision source for a family; None = unmapped = DENY."""

        category = self.family_categories.get(family)
        if category is None:
            return None
        return CATEGORY_CLASSES[category]

    def uid_of(self, subject: str) -> int | None:
        return self.subject_uids.get(subject)


_TOP_LEVEL_KEYS = frozenset(
    (
        "schema",
        "revision",
        "seat_classes",
        "subjects",
        "family_categories",
        "approvers",
        "revokers",
    )
)
_APPROVER_KEYS = frozenset(("decision_source", "grants"))
_GRANT_KEYS = frozenset(("family", "seat_classes", "max_ttl_seconds"))
_WILDCARD = "*"


def _policy_name(value: object, slot: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise InvalidApproverPolicy(f"invalid {slot}")
    if _WILDCARD in value:
        raise InvalidApproverPolicy(f"wildcard is inexpressible in {slot}")
    return value


def _policy_uid(value: object, slot: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InvalidApproverPolicy(f"invalid {slot}")
    return value


def _require_keys(
    raw: object,
    expected: frozenset[str],
    where: str,
) -> Mapping[str, object]:
    if not isinstance(raw, Mapping) or set(raw) != expected:
        raise InvalidApproverPolicy(f"invalid {where} keys")
    return raw


def parse_approver_policy(raw: Mapping[str, object]) -> ApproverPolicy:
    """Strictly parse a policy document; refuse anything not exactly v1.

    Refusals (each tested): unknown keys at any level (inheritance is
    inexpressible), wildcard tokens in any subject/family/seat/class slot,
    unknown categories (including any attempt to write a decision CLASS
    where a category belongs — the downgrade floor), approvers without a
    subject uid, grants naming an uncategorized family, duplicate grant
    families, non-positive or over-bound TTLs, invalid uids, wrong schema,
    empty revision.
    """

    _require_keys(raw, _TOP_LEVEL_KEYS, "policy")
    if raw["schema"] != POLICY_SCHEMA:
        raise InvalidApproverPolicy("unsupported policy schema")
    revision = _policy_name(raw["revision"], "policy revision")

    subjects_raw = raw["subjects"]
    if not isinstance(subjects_raw, Mapping) or not subjects_raw:
        raise InvalidApproverPolicy("invalid subjects")
    subject_uids: dict[str, int] = {}
    for name, uid in subjects_raw.items():
        subject_uids[_policy_name(name, "subject")] = _policy_uid(
            uid, "subject uid"
        )

    seat_classes_raw = raw["seat_classes"]
    if not isinstance(seat_classes_raw, Mapping):
        raise InvalidApproverPolicy("invalid seat classes")
    seat_classes: dict[str, str] = {}
    for seat_id, seat_class in seat_classes_raw.items():
        seat_classes[_policy_name(seat_id, "seat id")] = _policy_name(
            seat_class, "seat class"
        )

    family_categories_raw = raw["family_categories"]
    if not isinstance(family_categories_raw, Mapping):
        raise InvalidApproverPolicy("invalid family categories")
    family_categories: dict[str, str] = {}
    for family, category in family_categories_raw.items():
        family = _policy_name(family, "operation family")
        # the downgrade floor: only closed CATEGORIES are expressible
        # here — a decision-class token ("agent"/"human"/"none") or any
        # unknown name refuses at parse
        if category not in CAPABILITY_CATEGORIES:
            raise InvalidApproverPolicy("unknown capability category")
        family_categories[family] = category

    approvers_raw = raw["approvers"]
    if not isinstance(approvers_raw, Mapping):
        raise InvalidApproverPolicy("invalid approvers")
    approvers: dict[str, ApproverEntry] = {}
    for name, entry_raw in approvers_raw.items():
        name = _policy_name(name, "approver subject")
        if name not in subject_uids:
            raise InvalidApproverPolicy(
                "approver subject has no uid trust domain"
            )
        entry_map = _require_keys(entry_raw, _APPROVER_KEYS, "approver entry")
        decision_source = entry_map["decision_source"]
        if decision_source not in ("agent", "human"):
            raise InvalidApproverPolicy("invalid decision source")
        grants_raw = entry_map["grants"]
        if not isinstance(grants_raw, (list, tuple)):
            raise InvalidApproverPolicy("invalid grants")
        grants: dict[str, FamilyGrant] = {}
        for grant_raw in grants_raw:
            grant_map = _require_keys(grant_raw, _GRANT_KEYS, "grant")
            family = _policy_name(grant_map["family"], "grant family")
            if family in grants:
                raise InvalidApproverPolicy("duplicate grant family")
            if family not in family_categories:
                raise InvalidApproverPolicy(
                    "grant names an uncategorized operation family"
                )
            seats_raw = grant_map["seat_classes"]
            if not isinstance(seats_raw, (list, tuple)) or not seats_raw:
                raise InvalidApproverPolicy("invalid grant seat classes")
            grant_seats = frozenset(
                _policy_name(seat_class, "grant seat class")
                for seat_class in seats_raw
            )
            if len(grant_seats) != len(seats_raw):
                raise InvalidApproverPolicy("duplicate grant seat class")
            ttl_raw = grant_map["max_ttl_seconds"]
            if (
                not isinstance(ttl_raw, int)
                or isinstance(ttl_raw, bool)
                or ttl_raw < 1
            ):
                raise InvalidApproverPolicy("invalid grant ttl")
            max_ttl = timedelta(seconds=ttl_raw)
            if max_ttl > MAX_POLICY_TTL:
                raise InvalidApproverPolicy("grant ttl exceeds policy bound")
            grants[family] = FamilyGrant(
                family=family,
                seat_classes=grant_seats,
                max_ttl=max_ttl,
            )
        approvers[name] = ApproverEntry(
            subject=name,
            decision_source=decision_source,
            grants=grants,
        )

    revokers_raw = raw["revokers"]
    if not isinstance(revokers_raw, (list, tuple)):
        raise InvalidApproverPolicy("invalid revokers")
    revokers: set[str] = set()
    for revoker in revokers_raw:
        revoker = _policy_name(revoker, "revoker")
        if revoker not in subject_uids:
            raise InvalidApproverPolicy("revoker has no uid trust domain")
        revokers.add(revoker)

    return ApproverPolicy(
        schema=POLICY_SCHEMA,
        revision=revision,
        seat_classes=seat_classes,
        subject_uids=subject_uids,
        family_categories=family_categories,
        approvers=approvers,
        revokers=frozenset(revokers),
    )


def load_approver_policy(
    loader: RootOwnedFileLoader,
    path: Path,
    *,
    max_bytes: int = MAX_POLICY_BYTES,
) -> ApproverPolicy:
    """Read a policy file through the root-owned trust boundary and parse.

    ``RegistryError`` propagates for trust faults (missing file, wrong
    owner, group/other-writable mode, symlink, malformed/duplicate-key
    JSON); ``InvalidApproverPolicy`` for semantic faults. Either way the
    caller ends with NO policy — fail closed to deny-all.
    """

    return parse_approver_policy(loader.read_json(path, max_bytes=max_bytes))


def _authorized(
    max_ttl: timedelta | None = None,
    policy_revision: str | None = None,
) -> PolicyVerdict:
    return PolicyVerdict(
        True,
        "authorized",
        max_ttl=max_ttl,
        policy_revision=policy_revision,
    )


def _denied(reason_code: str) -> PolicyVerdict:
    return PolicyVerdict(False, reason_code)


class PolicyApproverAuthorizer:
    """Real ApproverAuthorizer over a parsed root-owned policy.

    Denial precedence (first match wins; each code closed and tested):

    1. ``unknown_approver`` — subject not in the approver roster.
    2. ``unknown_subject`` — a plan owner/performer/requester/participant
       whose uid trust domain the policy cannot resolve (fail closed: an
       unverifiable separation check is a denial).
    3. Separation of duty (gate 13): ``approver_is_plan_owner``,
       ``approver_is_performer``, ``approver_is_request_plan_owner``,
       ``approver_shares_trust_domain`` (uid-level, catches distinct
       subjects sharing a Unix uid), ``approver_is_handoff_participant``
       (any owner/performer/requester anywhere in the same server-linked
       request-plan chain, by subject or by uid).
    4. Roster/routing: ``unknown_operation_family``, ``family_not_granted``,
       ``unknown_target_seat``, ``seat_class_not_granted``.
    5. Decide-only routing: ``human_decision_required`` (the strictest
       category class across the plan's families is ``human``; agent-path
       decisions — grant AND deny alike — are refused, conservatively:
       the decision source for such a family is human, full stop),
       ``source_not_permitted`` (the approver's permitted decision source
       is not ``agent``; T-2919's provider-authenticated ingress is the
       only human mint).

    Review-open deliberately does NOT apply the human gate: an agent
    approver with roster standing may OPEN a review on a human-gated
    family (that is how T-2919's RequestHuman flow starts); the gate lands
    on decide.
    """

    def __init__(self, policy: ApproverPolicy) -> None:
        self._policy = policy

    def may_review(
        self,
        approver_subject: str,
        metadata: PlanMetadata,
    ) -> PolicyVerdict:
        return self._authorize_common(
            approver_subject,
            owner=metadata.owner,
            performer=metadata.performer,
            target_seat_id=metadata.target_seat_id,
            families=tuple(metadata.operation_families),
        )

    def may_handoff(
        self,
        approver_subject: str,
        metadata: PlanMetadata,
        request_metadata: PlanMetadata,
        chain_participants: frozenset[str],
    ) -> PolicyVerdict:
        # standalone-safe: re-runs the common checks so the verdict never
        # depends on the caller having called may_review first
        common = self._authorize_common(
            approver_subject,
            owner=metadata.owner,
            performer=metadata.performer,
            target_seat_id=metadata.target_seat_id,
            families=tuple(metadata.operation_families),
        )
        if not common.allowed:
            return common
        approver_uid = self._policy.uid_of(approver_subject)
        handoff = self._check_requester_and_chain(
            approver_subject,
            approver_uid,
            request_metadata.owner,
            chain_participants,
        )
        if not handoff.allowed:
            return handoff
        return _authorized()

    def may_decide(
        self,
        approver_subject: str,
        review: ReviewRecord,
        chain_participants: frozenset[str],
    ) -> PolicyVerdict:
        common = self._authorize_common(
            approver_subject,
            owner=review.plan_owner,
            performer=review.performer,
            target_seat_id=review.target_seat_id,
            families=tuple(review.operation_families),
        )
        if not common.allowed:
            return common
        approver_uid = self._policy.uid_of(approver_subject)
        if review.handoff is not None:
            handoff = self._check_requester_and_chain(
                approver_subject,
                approver_uid,
                review.handoff.request_plan_owner,
                chain_participants,
            )
            if not handoff.allowed:
                return handoff
        # approval-class routing: the STRICTEST category class across the
        # plan's families governs. Human-gated families refuse agent-path
        # decisions ENTIRELY — grant and deny alike — conservatively: the
        # decision source for such a family is human, full stop.
        strictest = 0
        for family in review.operation_families:
            required = self._policy.required_class(family)
            if required is None:
                return _denied("unknown_operation_family")
            strictest = max(strictest, APPROVAL_CLASS_ORDER[required])
        if strictest >= APPROVAL_CLASS_ORDER["human"]:
            return _denied("human_decision_required")
        # every decision this service can mint is agent-sourced; a roster
        # entry whose permitted source is 'human' must come through the
        # T-2919 provider-authenticated ingress, never this path
        entry = self._policy.approvers[approver_subject]
        if entry.decision_source != "agent":
            return _denied("source_not_permitted")
        max_ttl = min(
            entry.grants[family].max_ttl
            for family in review.operation_families
        )
        return _authorized(
            max_ttl=max_ttl,
            policy_revision=self._policy.revision,
        )

    def may_revoke(self, subject: str, grant: GrantRecord) -> PolicyVerdict:
        if subject not in self._policy.revokers:
            return _denied("revoke_not_permitted")
        return _authorized()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _authorize_common(
        self,
        approver_subject: str,
        *,
        owner: str,
        performer: str,
        target_seat_id: str,
        families: tuple[str, ...],
    ) -> PolicyVerdict:
        policy = self._policy
        entry = policy.approvers.get(approver_subject)
        if entry is None:
            return _denied("unknown_approver")
        approver_uid = policy.uid_of(approver_subject)  # parse-guaranteed
        owner_uid = policy.uid_of(owner)
        performer_uid = policy.uid_of(performer)
        if owner_uid is None or performer_uid is None:
            # an unresolvable trust domain makes the separation check
            # unverifiable — fail closed, never benefit of the doubt
            return _denied("unknown_subject")
        if approver_subject == owner:
            return _denied("approver_is_plan_owner")
        if approver_subject == performer:
            return _denied("approver_is_performer")
        if approver_uid in (owner_uid, performer_uid):
            return _denied("approver_shares_trust_domain")
        if not families:
            return _denied("unknown_operation_family")
        seat_class = policy.seat_classes.get(target_seat_id)
        for family in families:
            if family not in policy.family_categories:
                return _denied("unknown_operation_family")
            grant = entry.grants.get(family)
            if grant is None:
                return _denied("family_not_granted")
            if seat_class is None:
                return _denied("unknown_target_seat")
            if seat_class not in grant.seat_classes:
                return _denied("seat_class_not_granted")
        return _authorized()

    def _check_requester_and_chain(
        self,
        approver_subject: str,
        approver_uid: int | None,
        requester: str,
        chain_participants: frozenset[str],
    ) -> PolicyVerdict:
        requester_uid = self._policy.uid_of(requester)
        if requester_uid is None:
            return _denied("unknown_subject")
        if approver_subject == requester:
            return _denied("approver_is_request_plan_owner")
        if approver_uid == requester_uid:
            return _denied("approver_shares_trust_domain")
        for participant in sorted(chain_participants):
            if participant == approver_subject:
                return _denied("approver_is_handoff_participant")
            participant_uid = self._policy.uid_of(participant)
            if participant_uid is None:
                return _denied("unknown_subject")
            if participant_uid == approver_uid:
                # a same-uid shadow of a participant IS the participant's
                # trust domain (gate 11 / gate 13)
                return _denied("approver_is_handoff_participant")
        return _authorized()
