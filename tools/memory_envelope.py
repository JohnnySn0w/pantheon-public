"""Typed ORG-058 memory envelope + lifecycle schema and trust policy (T-3197).

This module is the schema half of the ORG-058 Exocortex memory organ's
lifecycle-controls slice: the closed enum sets, the immutable-revision
``TrustPolicy`` (integrity label lattice + meet), every schema dataclass named
in the T-2447 typed surface (report ``2026-08-25-org-058-memory-lifecycle-plan``
§6 UNION ORG-058 §4 UNION the T-2447 trust additions), the §5 lifecycle
transition map, and the canonical-bytes / receipt-digest primitives. It owns no
policy judgement, mutation path, runtime, or live authority; the deny-by-default
evaluator that consumes these types lives in ``memory_policy_evaluator``.

Normative schema authority: sop/standard/memory-envelope-spec.md (ORG-058 §4-§6)

Design posture (T-2447-derived, all structural rather than "remember not to"):

* Four dimensions never collapse (INV-1): integrity/trust labels are OPEN
  policy-defined strings interpreted only through a ``TrustPolicy`` lattice;
  epistemic ``ConfidenceKind``, privacy class, and action authority are distinct
  fields whose values are never read as trust labels.
* ``inherited_trust_floor`` is immutable derivation output; only
  ``effective_trust_floor`` may differ and only under a bound adjudication
  receipt (enforced by the evaluator).
* Evidence bindings are a LIST OF RECORDS (``EvidenceBinding``), never parallel
  ``refs[]``/``digests[]`` arrays — order/length ambiguity could rebind
  evidence, so ``receipt_from_json`` rejects the parallel-array shape at parse.
* ``receipt_digest`` is the digest of canonical receipt bytes EXCLUDING the
  ``receipt_digest`` field, under a named schema version + canonicalization rule;
  a self-referential digest cannot round-trip and is rejected by the evaluator.

stdlib only. ``from_json`` parsers fail closed by raising ``SchemaError`` on any
structural malformation (missing key, wrong type, parallel arrays); the boundary
that turns that into a deny verdict is the evaluator / lint CLI.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json


class SchemaError(ValueError):
    """A structural schema violation in untrusted input; parsers fail closed.

    Distinct from a policy denial (which is a Decision RETURN VALUE, never an
    exception): this is raised only by the ``*_from_json`` parsers and by
    ``TrustPolicy`` construction for input that is not even shaped like the
    contract. The lint CLI catches it and renders it as a violation.
    """


# Named schema/canonicalization identities. A moving alias is not an identity
# (ORG-058 §4/§6): the receipt carries these verbatim and the evaluator refuses
# any other value.
RECEIPT_SCHEMA_VERSION = "org058-trust-adjudication-receipt/v1"
CANONICALIZATION_REF = "pantheon-canonical-json/v1"


# ---------------------------------------------------------------------------
# Closed enum sets (str-Enum so pytest-xdist/execnet serialize cleanly and
# JSON round-trips through `.value`; see tools/conftest.py).
# ---------------------------------------------------------------------------
class MemoryClass(str, Enum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    RELATIONAL = "relational"
    IDENTITY_CANON = "identity_canon"


class ConfidenceKind(str, Enum):
    # ORG-058 §4: observed|asserted|inferred|generated — NEVER collapsed into a
    # trust label. Note "inferred" here is EPISTEMIC and is a different label
    # set from the "inference" evidence-citation label used in docstrings.
    OBSERVED = "observed"
    ASSERTED = "asserted"
    INFERRED = "inferred"
    GENERATED = "generated"


class RetentionShape(str, Enum):
    REFERENCE = "reference"
    EXCERPT = "excerpt"
    DERIVED = "derived"


class EvidenceRole(str, Enum):
    SUPPORT = "support"
    CONTRADICTION = "contradiction"
    QUOTED = "quoted"
    CONTEXT = "context"


class UseDisposition(str, Enum):
    ALLOWED = "allowed"
    # Reserved vocabulary value (ORG-058 §5 permits it): content usable only if
    # independently corroborated. The T-3197 evaluator's fixture policy expresses
    # below-floor-but-comparable content as QUARANTINED rather than emitting this;
    # it is defined here so a future policy can return it without a schema change.
    CORROBORATION_REQUIRED = "corroboration_required"
    QUARANTINED = "quarantined"
    REFUSED = "refused"


class LifecycleState(str, Enum):
    OBSERVED = "OBSERVED"
    REJECTED = "REJECTED"
    EXPIRES_UNRETAINED = "EXPIRES_UNRETAINED"
    RETENTION_PROPOSED = "RETENTION_PROPOSED"
    RETAINED_SOURCE_REF = "RETAINED_SOURCE_REF"
    RETAINED_DERIVED = "RETAINED_DERIVED"
    CONSOLIDATION_PROPOSED = "CONSOLIDATION_PROPOSED"
    SUPERSEDED = "SUPERSEDED"
    DECAYED = "DECAYED"
    FORGOTTEN_DERIVED = "FORGOTTEN_DERIVED"
    RETRIEVAL_CANDIDATE = "RETRIEVAL_CANDIDATE"
    DENIED = "DENIED"
    MEMORY_VIEW = "MEMORY_VIEW"
    NOT_USED = "NOT_USED"
    USED_WITHOUT_ACTION = "USED_WITHOUT_ACTION"
    ACTION_REQUESTED = "ACTION_REQUESTED"
    USE_EVALUATED = "USE_EVALUATED"


# ---------------------------------------------------------------------------
# §5 lifecycle transition map (report §5 / ORG-058 §3). Encoded as the explicit
# allowed-transitions set; anything not present is an illegal transition and the
# evaluator rejects it. RETAINED_SOURCE_REF and RETAINED_DERIVED are the two
# "RETAINED_*" origins for RETRIEVAL_CANDIDATE. CONSOLIDATION_PROPOSED resolves
# per §5 item 5 (promotion reviewed -> back to RETAINED_DERIVED, else
# supersede/decay/forget). Terminal states map to the empty frozenset.
#
# FORGOTTEN_DERIVED means the MEMORY artifact was forgotten at the subject's
# request; the canonical source is untouched (source erasure is a separate
# request with its own receipt). A RETAINED_SOURCE_REF memory is a derived
# artifact too (a reference to the source, not the source), so a subject-
# initiated forget of one ends in FORGOTTEN_DERIVED, not DECAYED ("expired by
# policy"). The FROM state already records source-ref vs derived (T-4275).
# ---------------------------------------------------------------------------
LIFECYCLE_TRANSITIONS: Mapping[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.OBSERVED: frozenset(
        {
            LifecycleState.REJECTED,
            LifecycleState.EXPIRES_UNRETAINED,
            LifecycleState.RETENTION_PROPOSED,
        }
    ),
    LifecycleState.RETENTION_PROPOSED: frozenset(
        {
            LifecycleState.RETAINED_SOURCE_REF,
            LifecycleState.RETAINED_DERIVED,
            LifecycleState.REJECTED,
            LifecycleState.EXPIRES_UNRETAINED,
        }
    ),
    LifecycleState.RETAINED_SOURCE_REF: frozenset(
        {
            LifecycleState.RETRIEVAL_CANDIDATE,
            LifecycleState.SUPERSEDED,
            LifecycleState.DECAYED,
            LifecycleState.FORGOTTEN_DERIVED,
        }
    ),
    LifecycleState.RETAINED_DERIVED: frozenset(
        {
            LifecycleState.CONSOLIDATION_PROPOSED,
            LifecycleState.SUPERSEDED,
            LifecycleState.DECAYED,
            LifecycleState.FORGOTTEN_DERIVED,
            LifecycleState.RETRIEVAL_CANDIDATE,
        }
    ),
    LifecycleState.CONSOLIDATION_PROPOSED: frozenset(
        {
            LifecycleState.RETAINED_DERIVED,
            LifecycleState.SUPERSEDED,
            LifecycleState.DECAYED,
            LifecycleState.FORGOTTEN_DERIVED,
        }
    ),
    LifecycleState.RETRIEVAL_CANDIDATE: frozenset(
        {LifecycleState.DENIED, LifecycleState.MEMORY_VIEW}
    ),
    LifecycleState.MEMORY_VIEW: frozenset(
        {
            LifecycleState.NOT_USED,
            LifecycleState.USED_WITHOUT_ACTION,
            LifecycleState.ACTION_REQUESTED,
        }
    ),
    LifecycleState.NOT_USED: frozenset({LifecycleState.USE_EVALUATED}),
    LifecycleState.USED_WITHOUT_ACTION: frozenset({LifecycleState.USE_EVALUATED}),
    LifecycleState.ACTION_REQUESTED: frozenset({LifecycleState.USE_EVALUATED}),
    # terminal
    LifecycleState.REJECTED: frozenset(),
    LifecycleState.EXPIRES_UNRETAINED: frozenset(),
    LifecycleState.SUPERSEDED: frozenset(),
    LifecycleState.DECAYED: frozenset(),
    LifecycleState.FORGOTTEN_DERIVED: frozenset(),
    LifecycleState.DENIED: frozenset(),
    LifecycleState.USE_EVALUATED: frozenset(),
}


def is_legal_transition(src: LifecycleState, dst: LifecycleState) -> bool:
    """True iff ``src -> dst`` is an allowed §5 lifecycle transition."""
    return dst in LIFECYCLE_TRANSITIONS.get(src, frozenset())


# ---------------------------------------------------------------------------
# Integrity-lattice primitives. The lattice is OPEN and lives in policy DATA,
# never hardcoded here (ORG-058 §4 / T-2447): a universal scalar trust score is
# exactly the thing this refuses to invent.
# ---------------------------------------------------------------------------
def _closure(
    labels: frozenset[str], pairs: frozenset[tuple[str, str]]
) -> frozenset[tuple[str, str]]:
    """Reflexive-transitive closure of ``<=`` (pairs are ``(lower, higher)``)."""
    leq: set[tuple[str, str]] = {(x, x) for x in labels}
    leq.update(pairs)
    changed = True
    while changed:
        changed = False
        for a, b in list(leq):
            for c, d in list(leq):
                if b == c and (a, d) not in leq:
                    leq.add((a, d))
                    changed = True
    return frozenset(leq)


def meet_labels(
    labels: frozenset[str],
    leq: frozenset[tuple[str, str]],
    subset: Sequence[str],
) -> str | None:
    """Greatest lower bound (meet) of ``subset`` under the ``<=`` closure ``leq``.

    Returns the unique GLB label, or ``None`` when the meet is absent/undefined:
    an empty subset, any label unknown to ``labels``, no common lower bound, or
    no UNIQUE greatest lower bound. ``None`` is the fail-closed signal (INV-3):
    callers resolve it to the policy's named conservative state or a refusal.
    Never invents an order, sorts lexically, or picks a branch.
    """
    members = list(subset)
    if not members:
        return None
    if any(s not in labels for s in members):
        return None
    lower = {x for x in labels if all((x, s) in leq for s in members)}
    if not lower:
        return None
    greatest = [x for x in lower if all((y, x) in leq for y in lower)]
    if len(greatest) == 1:
        return greatest[0]
    return None


@dataclass(frozen=True)
class TrustPolicy:
    """Named, immutable-revision integrity policy: the lattice + meet + the
    conservative state, plus the fixture-supplied retrieval/adjudication data
    the evaluator needs.

    Trust-lattice core (ORG-058 §4 / T-2447 §"Concept and field model"):
    ``policy_ref``+``revision``+``digest`` are the immutable identity (a moving
    alias is not an identity); ``labels`` + ``order_pairs`` (``(lower, higher)``
    meaning ``lower <= higher``) define the partial order; ``conservative_state``
    is the named fail-closed label; ``total_order`` records whether a scalar
    minimum is even valid (it is valid ONLY then).

    Extensions beyond the enumerated trust-lattice list, carried here because
    they are squarely POLICY data and the locked evaluator signatures give them
    no other home (FLAGGED to the orchestrator):
    * ``self_adjudication_allowed`` — ORG-058 §6: a proposer may self-adjudicate
      only when "the named policy explicitly permits that actor".
    * ``purpose_floors`` — ORG-058 §5: retrieval ``use_disposition`` is
      "purpose-derived"; this maps a purpose to its required minimum integrity
      label. An unmapped purpose fails closed.
    * ``privacy_labels``/``privacy_order_pairs`` — a SEPARATE dimension's lattice
      (INV-1), used only to authorize ``privacy_ceiling`` before ranking; empty
      => exact-match fallback (still fail-closed). Never conflated with trust.
    """

    policy_ref: str
    revision: str
    digest: str
    labels: frozenset[str]
    order_pairs: frozenset[tuple[str, str]]
    conservative_state: str
    total_order: bool = False
    self_adjudication_allowed: bool = False
    purpose_floors: Mapping[str, str] = field(default_factory=dict)
    privacy_labels: frozenset[str] = frozenset()
    privacy_order_pairs: frozenset[tuple[str, str]] = frozenset()

    def __post_init__(self) -> None:
        if not self.labels:
            raise SchemaError("trust policy has no integrity labels")
        if self.conservative_state not in self.labels:
            raise SchemaError("conservative_state must be a declared label")
        for lo, hi in self.order_pairs:
            if lo not in self.labels or hi not in self.labels:
                raise SchemaError("order pair references an unknown label")
        for purpose, floor in self.purpose_floors.items():
            if floor not in self.labels:
                raise SchemaError(f"purpose floor for {purpose!r} unknown")
        for lo, hi in self.privacy_order_pairs:
            if lo not in self.privacy_labels or hi not in self.privacy_labels:
                raise SchemaError("privacy order pair references unknown label")
        leq = _closure(self.labels, self.order_pairs)
        # total_order is a CLAIM about the lattice; verify it rather than trust
        # it, so a scalar-minimum shortcut can never be justified by a lie.
        if self.total_order:
            for a in self.labels:
                for b in self.labels:
                    if (a, b) not in leq and (b, a) not in leq:
                        raise SchemaError("total_order declared but labels incomparable")
        object.__setattr__(self, "_leq", leq)
        object.__setattr__(
            self, "_privacy_leq", _closure(self.privacy_labels, self.privacy_order_pairs)
        )

    def leq(self, lower: str, higher: str) -> bool:
        """True iff ``lower <= higher`` in the integrity order (both known)."""
        return (lower, higher) in self._leq  # type: ignore[attr-defined]

    def meet(self, subset: Sequence[str]) -> str | None:
        """Greatest lower bound of ``subset``; ``None`` fails closed (INV-3)."""
        return meet_labels(self.labels, self._leq, subset)  # type: ignore[attr-defined]

    def privacy_admits(self, ceiling: str, privacy_class: str) -> bool:
        """True iff a caller cleared to ``ceiling`` may receive ``privacy_class``.

        i.e. the item is no more sensitive than the ceiling
        (``privacy_class <= ceiling``). Fail-closed: unknown labels or an
        incomparable pair deny; with no declared privacy lattice, only an exact
        match admits (never a silent lattice invention).
        """
        if not self.privacy_labels:
            return privacy_class == ceiling
        if ceiling not in self.privacy_labels or privacy_class not in self.privacy_labels:
            return False
        return (privacy_class, ceiling) in self._privacy_leq  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Schema dataclasses. All frozen; array fields are tuples (immutable + hashable
# + JSON-serializable via list()). Field lists are verbatim from the brief's
# union of report §6 + ORG-058 §4 + the T-2447 typed surface.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SourceRef:
    source_system: str
    object_id: str
    source_revision: str
    source_digest: str
    observed_at: str
    verifier: str


@dataclass(frozen=True)
class SourceIntegrity:
    evidence_ref: str
    source_revision: str
    source_digest: str
    integrity_label: str
    trust_policy_ref: str
    trust_policy_revision: str
    trust_policy_digest: str
    evidence_role: EvidenceRole


@dataclass(frozen=True)
class ClaimTrust:
    claim_id: str
    contributing_evidence_refs: tuple[str, ...]
    contradicting_evidence_refs: tuple[str, ...]
    inherited_trust_floor: str
    effective_trust_floor: str
    adjudication_receipt_ref: str | None


@dataclass(frozen=True)
class EvidenceBinding:
    """One receipt->evidence binding record. The LIST-OF-RECORDS form is
    mandatory (T-2447): parallel ``refs[]``/``digests[]`` arrays are rejected at
    parse because order/length ambiguity could rebind evidence."""

    evidence_ref: str
    source_revision: str
    digest: str


@dataclass(frozen=True)
class ObservationCandidate:
    candidate_id: str
    source_ref: SourceRef
    observed_at: str
    observer: str
    subject_hints: tuple[str, ...]
    class_hint: MemoryClass
    sensitivity_hint: str
    bounded_payload_ref: str
    dedupe_key: str
    expires_at: str | None


@dataclass(frozen=True)
class RetentionProposal:
    candidate_id: str
    proposed_class: MemoryClass
    canonical_source_ref: SourceRef
    retention_shape: RetentionShape
    owner: str
    subjects: tuple[str, ...]
    audience: str
    privacy_class: str
    confidence_kind: ConfidenceKind
    retention_until: str | None
    correction_policy: str
    forgetting_policy: str
    required_authority: str
    rationale: str


@dataclass(frozen=True)
class MemoryEnvelope:
    memory_id: str
    version_id: str
    memory_class: MemoryClass
    lifecycle_state: LifecycleState
    canonical_source_ref: SourceRef
    created_at: str
    observed_at: str
    valid_from: str
    valid_to: str | None
    owner: str
    subjects: tuple[str, ...]
    audience: str
    privacy_class: str
    confidence_kind: ConfidenceKind
    confidence: float
    supporting_refs: tuple[str, ...]
    contradicting_refs: tuple[str, ...]
    retention_until: str | None
    correction_policy: str
    forgetting_policy: str
    retrieval_authority: str
    action_authority_ref: str | None
    supersedes: tuple[str, ...]
    superseded_by: tuple[str, ...]
    last_consolidated_at: str | None
    payload_ref: str
    payload_digest: str
    policy_revision: str
    # --- T-2447 trust additions ---
    trust_policy_ref: str
    trust_policy_revision: str
    trust_policy_digest: str
    source_integrity: tuple[SourceIntegrity, ...]
    claim_trust: tuple[ClaimTrust, ...]
    inherited_trust_floor: str
    effective_trust_floor: str
    trust_adjudication_receipt_refs: tuple[str, ...]


@dataclass(frozen=True)
class RetrievalRequest:
    request_id: str
    caller_identity: str
    persona: str
    purpose: str
    subject_scope: tuple[str, ...]
    memory_classes: tuple[MemoryClass, ...]
    recency: str
    depth: int
    token_budget: int  # chars of payload-ref strings (content-blind), not content tokens
    item_budget: int
    privacy_ceiling: str
    request_time: str


@dataclass(frozen=True)
class MemoryViewItem:
    memory_id: str
    version_id: str
    bounded_content: str  # the payload REF string, not dereferenced content
    source_refs: tuple[str, ...]
    claim_ids: tuple[str, ...]
    confidence_kind: ConfidenceKind
    freshness: str
    contradiction_notice: str
    trust_policy_ref: str
    trust_policy_revision: str
    trust_policy_digest: str
    # INV-7: floors + disposition are never omitted for RETURNED content. These
    # stay `str`/`| None` (empty/None = "omitted") so an incomplete item is
    # CONSTRUCTIBLE from JSON and validate_memory_view can catch it — the guard
    # is a check, not merely the type system.
    inherited_trust_floor: str
    effective_trust_floor: str
    adjudication_receipt_refs: tuple[str, ...]
    use_disposition: UseDisposition | None


@dataclass(frozen=True)
class MemoryView:
    request_id: str
    view_id: str
    purpose: str
    generated_at: str
    expires_at: str | None
    items: tuple[MemoryViewItem, ...]
    denied_count: int
    omitted_reason_counts: Mapping[str, int]
    token_count: int  # sum of len(payload ref) over items, not content tokens


@dataclass(frozen=True)
class TrustAdjudicationReceipt:
    receipt_schema_version: str
    canonicalization_ref: str
    receipt_id: str
    adjudication_id: str
    memory_id: str
    version_id: str
    claim_ids: tuple[str, ...]
    proposer_ref: str
    adjudicator_ref: str
    authority_ref: str
    trust_policy_ref: str
    trust_policy_revision: str
    trust_policy_digest: str
    input_evidence_bindings: tuple[EvidenceBinding, ...]
    derived_payload_digest: str
    before_inherited_floor: str
    before_effective_floor: str
    after_effective_floor: str
    purpose_scope: tuple[str, ...]
    rationale: str
    decision: str
    issued_at: str
    expires_at: str
    supersedes_receipt_ref: str | None
    receipt_digest: str


# ---------------------------------------------------------------------------
# Canonicalization + receipt digest. Canonical bytes match the repo convention
# (tools/capability_navigator_contract.canonical_json_bytes): sort_keys, indent,
# unicode preserved, no NaN, trailing newline.
# ---------------------------------------------------------------------------
def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def trust_policy_content_digest(policy: TrustPolicy) -> str:
    """Digest recomputed over EVERYTHING that defines a policy's behaviour:
    ref, revision, labels, order_pairs (the lattice), conservative_state,
    total_order, self_adjudication_allowed, purpose_floors and the privacy
    lattice. It EXCLUDES the declared ``digest`` field (a digest cannot cover
    itself), so a policy whose declared digest is not this value has had its
    content changed behind its identity."""
    body = {
        "policy_ref": policy.policy_ref,
        "revision": policy.revision,
        "labels": sorted(policy.labels),
        "order_pairs": sorted([lo, hi] for lo, hi in policy.order_pairs),
        "conservative_state": policy.conservative_state,
        "total_order": policy.total_order,
        "self_adjudication_allowed": policy.self_adjudication_allowed,
        "purpose_floors": {k: policy.purpose_floors[k] for k in sorted(policy.purpose_floors)},
        "privacy_labels": sorted(policy.privacy_labels),
        "privacy_order_pairs": sorted([lo, hi] for lo, hi in policy.privacy_order_pairs),
    }
    return "sha256:" + hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def receipt_to_json(
    receipt: TrustAdjudicationReceipt, *, include_digest: bool = True
) -> dict[str, object]:
    """Canonical JSON mapping for a receipt.

    ``include_digest=False`` OMITS the ``receipt_digest`` key entirely — that is
    the exact preimage the digest is defined over ("canonical receipt bytes
    excluding the receipt_digest field"). A digest computed over the
    ``include_digest=True`` form is self-referential and cannot round-trip.
    """
    body: dict[str, object] = {
        "receipt_schema_version": receipt.receipt_schema_version,
        "canonicalization_ref": receipt.canonicalization_ref,
        "receipt_id": receipt.receipt_id,
        "adjudication_id": receipt.adjudication_id,
        "memory_id": receipt.memory_id,
        "version_id": receipt.version_id,
        "claim_ids": list(receipt.claim_ids),
        "proposer_ref": receipt.proposer_ref,
        "adjudicator_ref": receipt.adjudicator_ref,
        "authority_ref": receipt.authority_ref,
        "trust_policy_ref": receipt.trust_policy_ref,
        "trust_policy_revision": receipt.trust_policy_revision,
        "trust_policy_digest": receipt.trust_policy_digest,
        "input_evidence_bindings": [
            {
                "evidence_ref": b.evidence_ref,
                "source_revision": b.source_revision,
                "digest": b.digest,
            }
            for b in receipt.input_evidence_bindings
        ],
        "derived_payload_digest": receipt.derived_payload_digest,
        "before_inherited_floor": receipt.before_inherited_floor,
        "before_effective_floor": receipt.before_effective_floor,
        "after_effective_floor": receipt.after_effective_floor,
        "purpose_scope": list(receipt.purpose_scope),
        "rationale": receipt.rationale,
        "decision": receipt.decision,
        "issued_at": receipt.issued_at,
        "expires_at": receipt.expires_at,
        "supersedes_receipt_ref": receipt.supersedes_receipt_ref,
    }
    if include_digest:
        body["receipt_digest"] = receipt.receipt_digest
    return body


def compute_receipt_digest(receipt: TrustAdjudicationReceipt) -> str:
    """Digest over canonical receipt bytes EXCLUDING ``receipt_digest``.

    Returns ``sha256:<hex>``. Because the preimage omits the field, a receipt
    whose stored digest was computed over bytes that include the field can never
    equal this — that is how "the digest is never defined self-referentially" is
    enforced structurally rather than by inspection.
    """
    preimage = receipt_to_json(receipt, include_digest=False)
    return "sha256:" + hashlib.sha256(canonical_json_bytes(preimage)).hexdigest()


# ---------------------------------------------------------------------------
# from_json parsers. Fail closed by RAISING SchemaError on structural
# malformation (used by the lint CLI and by tests that must reject a shape the
# typed dataclasses cannot even express, e.g. parallel evidence arrays).
# ---------------------------------------------------------------------------
def _req(m: Mapping[str, object], key: str) -> object:
    if key not in m:
        raise SchemaError(f"missing required key: {key}")
    return m[key]


def _str(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise SchemaError(f"{key} must be a string")
    return value


def _opt_str(value: object, key: str) -> str | None:
    if value is None:
        return None
    return _str(value, key)


def _tuple_str(value: object, key: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise SchemaError(f"{key} must be a list")
    return tuple(_str(v, f"{key}[]") for v in value)


def _enum(enum_cls: type, value: object, key: str):
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise SchemaError(f"{key}: invalid {enum_cls.__name__} {value!r}") from exc


def _source_ref_from_json(m: object, key: str) -> SourceRef:
    if not isinstance(m, Mapping):
        raise SchemaError(f"{key} must be an object")
    return SourceRef(
        source_system=_str(_req(m, "source_system"), "source_system"),
        object_id=_str(_req(m, "object_id"), "object_id"),
        source_revision=_str(_req(m, "source_revision"), "source_revision"),
        source_digest=_str(_req(m, "source_digest"), "source_digest"),
        observed_at=_str(_req(m, "observed_at"), "observed_at"),
        verifier=_str(_req(m, "verifier"), "verifier"),
    )


def _source_integrity_from_json(m: object) -> SourceIntegrity:
    if not isinstance(m, Mapping):
        raise SchemaError("source_integrity entry must be an object")
    return SourceIntegrity(
        evidence_ref=_str(_req(m, "evidence_ref"), "evidence_ref"),
        source_revision=_str(_req(m, "source_revision"), "source_revision"),
        source_digest=_str(_req(m, "source_digest"), "source_digest"),
        integrity_label=_str(_req(m, "integrity_label"), "integrity_label"),
        trust_policy_ref=_str(_req(m, "trust_policy_ref"), "trust_policy_ref"),
        trust_policy_revision=_str(
            _req(m, "trust_policy_revision"), "trust_policy_revision"
        ),
        trust_policy_digest=_str(_req(m, "trust_policy_digest"), "trust_policy_digest"),
        evidence_role=_enum(EvidenceRole, _req(m, "evidence_role"), "evidence_role"),
    )


def _claim_trust_from_json(m: object) -> ClaimTrust:
    if not isinstance(m, Mapping):
        raise SchemaError("claim_trust entry must be an object")
    return ClaimTrust(
        claim_id=_str(_req(m, "claim_id"), "claim_id"),
        contributing_evidence_refs=_tuple_str(
            _req(m, "contributing_evidence_refs"), "contributing_evidence_refs"
        ),
        contradicting_evidence_refs=_tuple_str(
            _req(m, "contradicting_evidence_refs"), "contradicting_evidence_refs"
        ),
        inherited_trust_floor=_str(
            _req(m, "inherited_trust_floor"), "inherited_trust_floor"
        ),
        effective_trust_floor=_str(
            _req(m, "effective_trust_floor"), "effective_trust_floor"
        ),
        adjudication_receipt_ref=_opt_str(
            m.get("adjudication_receipt_ref"), "adjudication_receipt_ref"
        ),
    )


def policy_from_json(m: Mapping[str, object]) -> TrustPolicy:
    if not isinstance(m, Mapping):
        raise SchemaError("policy must be an object")
    return TrustPolicy(
        policy_ref=_str(_req(m, "policy_ref"), "policy_ref"),
        revision=_str(_req(m, "revision"), "revision"),
        digest=_str(_req(m, "digest"), "digest"),
        labels=frozenset(_tuple_str(_req(m, "labels"), "labels")),
        order_pairs=frozenset(
            (_str(p[0], "order_pair"), _str(p[1], "order_pair"))
            for p in _req(m, "order_pairs")  # type: ignore[union-attr]
            if isinstance(p, (list, tuple)) and len(p) == 2
        ),
        conservative_state=_str(_req(m, "conservative_state"), "conservative_state"),
        total_order=bool(m.get("total_order", False)),
        self_adjudication_allowed=bool(m.get("self_adjudication_allowed", False)),
        purpose_floors={
            _str(k, "purpose"): _str(v, "purpose_floor")
            for k, v in dict(m.get("purpose_floors", {})).items()
        },
        privacy_labels=frozenset(
            _tuple_str(m.get("privacy_labels", []), "privacy_labels")
        ),
        privacy_order_pairs=frozenset(
            (_str(p[0], "privacy_pair"), _str(p[1], "privacy_pair"))
            for p in m.get("privacy_order_pairs", [])  # type: ignore[union-attr]
            if isinstance(p, (list, tuple)) and len(p) == 2
        ),
    )


def envelope_from_json(m: Mapping[str, object]) -> MemoryEnvelope:
    if not isinstance(m, Mapping):
        raise SchemaError("envelope must be an object")
    si_raw = _req(m, "source_integrity")
    ct_raw = _req(m, "claim_trust")
    if not isinstance(si_raw, (list, tuple)):
        raise SchemaError("source_integrity must be a list")
    if not isinstance(ct_raw, (list, tuple)):
        raise SchemaError("claim_trust must be a list")
    return MemoryEnvelope(
        memory_id=_str(_req(m, "memory_id"), "memory_id"),
        version_id=_str(_req(m, "version_id"), "version_id"),
        memory_class=_enum(MemoryClass, _req(m, "memory_class"), "memory_class"),
        lifecycle_state=_enum(
            LifecycleState, _req(m, "lifecycle_state"), "lifecycle_state"
        ),
        canonical_source_ref=_source_ref_from_json(
            _req(m, "canonical_source_ref"), "canonical_source_ref"
        ),
        created_at=_str(_req(m, "created_at"), "created_at"),
        observed_at=_str(_req(m, "observed_at"), "observed_at"),
        valid_from=_str(_req(m, "valid_from"), "valid_from"),
        valid_to=_opt_str(m.get("valid_to"), "valid_to"),
        owner=_str(_req(m, "owner"), "owner"),
        subjects=_tuple_str(_req(m, "subjects"), "subjects"),
        audience=_str(_req(m, "audience"), "audience"),
        privacy_class=_str(_req(m, "privacy_class"), "privacy_class"),
        confidence_kind=_enum(
            ConfidenceKind, _req(m, "confidence_kind"), "confidence_kind"
        ),
        confidence=float(_req(m, "confidence")),  # type: ignore[arg-type]
        supporting_refs=_tuple_str(_req(m, "supporting_refs"), "supporting_refs"),
        contradicting_refs=_tuple_str(
            _req(m, "contradicting_refs"), "contradicting_refs"
        ),
        retention_until=_opt_str(m.get("retention_until"), "retention_until"),
        correction_policy=_str(_req(m, "correction_policy"), "correction_policy"),
        forgetting_policy=_str(_req(m, "forgetting_policy"), "forgetting_policy"),
        retrieval_authority=_str(
            _req(m, "retrieval_authority"), "retrieval_authority"
        ),
        action_authority_ref=_opt_str(
            m.get("action_authority_ref"), "action_authority_ref"
        ),
        supersedes=_tuple_str(_req(m, "supersedes"), "supersedes"),
        superseded_by=_tuple_str(_req(m, "superseded_by"), "superseded_by"),
        last_consolidated_at=_opt_str(
            m.get("last_consolidated_at"), "last_consolidated_at"
        ),
        payload_ref=_str(_req(m, "payload_ref"), "payload_ref"),
        payload_digest=_str(_req(m, "payload_digest"), "payload_digest"),
        policy_revision=_str(_req(m, "policy_revision"), "policy_revision"),
        trust_policy_ref=_str(_req(m, "trust_policy_ref"), "trust_policy_ref"),
        trust_policy_revision=_str(
            _req(m, "trust_policy_revision"), "trust_policy_revision"
        ),
        trust_policy_digest=_str(_req(m, "trust_policy_digest"), "trust_policy_digest"),
        source_integrity=tuple(_source_integrity_from_json(x) for x in si_raw),
        claim_trust=tuple(_claim_trust_from_json(x) for x in ct_raw),
        inherited_trust_floor=_str(
            _req(m, "inherited_trust_floor"), "inherited_trust_floor"
        ),
        effective_trust_floor=_str(
            _req(m, "effective_trust_floor"), "effective_trust_floor"
        ),
        trust_adjudication_receipt_refs=_tuple_str(
            _req(m, "trust_adjudication_receipt_refs"),
            "trust_adjudication_receipt_refs",
        ),
    )


def envelope_to_json(env: MemoryEnvelope) -> dict[str, object]:
    """Exact inverse of ``envelope_from_json`` (T-3198).

    Every key ``envelope_from_json`` reads is emitted, optional ones as
    ``None``; nested ``SourceRef``/``SourceIntegrity``/``ClaimTrust`` become
    objects, tuples become lists, and enums become their ``.value``. So
    ``envelope_from_json(envelope_to_json(e)) == e`` for every envelope, and
    ``canonical_json_bytes`` over this mapping is the stored-version preimage
    the lifecycle store digests for compare-and-swap.
    """
    src = env.canonical_source_ref
    return {
        "memory_id": env.memory_id,
        "version_id": env.version_id,
        "memory_class": env.memory_class.value,
        "lifecycle_state": env.lifecycle_state.value,
        "canonical_source_ref": {
            "source_system": src.source_system,
            "object_id": src.object_id,
            "source_revision": src.source_revision,
            "source_digest": src.source_digest,
            "observed_at": src.observed_at,
            "verifier": src.verifier,
        },
        "created_at": env.created_at,
        "observed_at": env.observed_at,
        "valid_from": env.valid_from,
        "valid_to": env.valid_to,
        "owner": env.owner,
        "subjects": list(env.subjects),
        "audience": env.audience,
        "privacy_class": env.privacy_class,
        "confidence_kind": env.confidence_kind.value,
        "confidence": env.confidence,
        "supporting_refs": list(env.supporting_refs),
        "contradicting_refs": list(env.contradicting_refs),
        "retention_until": env.retention_until,
        "correction_policy": env.correction_policy,
        "forgetting_policy": env.forgetting_policy,
        "retrieval_authority": env.retrieval_authority,
        "action_authority_ref": env.action_authority_ref,
        "supersedes": list(env.supersedes),
        "superseded_by": list(env.superseded_by),
        "last_consolidated_at": env.last_consolidated_at,
        "payload_ref": env.payload_ref,
        "payload_digest": env.payload_digest,
        "policy_revision": env.policy_revision,
        "trust_policy_ref": env.trust_policy_ref,
        "trust_policy_revision": env.trust_policy_revision,
        "trust_policy_digest": env.trust_policy_digest,
        "source_integrity": [
            {
                "evidence_ref": si.evidence_ref,
                "source_revision": si.source_revision,
                "source_digest": si.source_digest,
                "integrity_label": si.integrity_label,
                "trust_policy_ref": si.trust_policy_ref,
                "trust_policy_revision": si.trust_policy_revision,
                "trust_policy_digest": si.trust_policy_digest,
                "evidence_role": si.evidence_role.value,
            }
            for si in env.source_integrity
        ],
        "claim_trust": [
            {
                "claim_id": c.claim_id,
                "contributing_evidence_refs": list(c.contributing_evidence_refs),
                "contradicting_evidence_refs": list(c.contradicting_evidence_refs),
                "inherited_trust_floor": c.inherited_trust_floor,
                "effective_trust_floor": c.effective_trust_floor,
                "adjudication_receipt_ref": c.adjudication_receipt_ref,
            }
            for c in env.claim_trust
        ],
        "inherited_trust_floor": env.inherited_trust_floor,
        "effective_trust_floor": env.effective_trust_floor,
        "trust_adjudication_receipt_refs": list(env.trust_adjudication_receipt_refs),
    }


def receipt_from_json(m: Mapping[str, object]) -> TrustAdjudicationReceipt:
    if not isinstance(m, Mapping):
        raise SchemaError("receipt must be an object")
    # Parallel-array shape is rejected outright (T-2447): a receipt MUST carry a
    # list of {evidence_ref, source_revision, digest} records.
    if "evidence_refs" in m or "evidence_digests" in m:
        raise SchemaError(
            "parallel evidence-reference/digest arrays are invalid; "
            "use input_evidence_bindings records"
        )
    bindings_raw = _req(m, "input_evidence_bindings")
    if not isinstance(bindings_raw, (list, tuple)):
        raise SchemaError("input_evidence_bindings must be a list of records")
    bindings: list[EvidenceBinding] = []
    for entry in bindings_raw:
        if not isinstance(entry, Mapping):
            raise SchemaError("evidence binding must be an object")
        if set(entry) != {"evidence_ref", "source_revision", "digest"}:
            raise SchemaError(
                "evidence binding must have exactly evidence_ref, "
                "source_revision, digest"
            )
        bindings.append(
            EvidenceBinding(
                evidence_ref=_str(entry["evidence_ref"], "evidence_ref"),
                source_revision=_str(entry["source_revision"], "source_revision"),
                digest=_str(entry["digest"], "digest"),
            )
        )
    return TrustAdjudicationReceipt(
        receipt_schema_version=_str(
            _req(m, "receipt_schema_version"), "receipt_schema_version"
        ),
        canonicalization_ref=_str(
            _req(m, "canonicalization_ref"), "canonicalization_ref"
        ),
        receipt_id=_str(_req(m, "receipt_id"), "receipt_id"),
        adjudication_id=_str(_req(m, "adjudication_id"), "adjudication_id"),
        memory_id=_str(_req(m, "memory_id"), "memory_id"),
        version_id=_str(_req(m, "version_id"), "version_id"),
        claim_ids=_tuple_str(_req(m, "claim_ids"), "claim_ids"),
        proposer_ref=_str(_req(m, "proposer_ref"), "proposer_ref"),
        adjudicator_ref=_str(_req(m, "adjudicator_ref"), "adjudicator_ref"),
        authority_ref=_str(_req(m, "authority_ref"), "authority_ref"),
        trust_policy_ref=_str(_req(m, "trust_policy_ref"), "trust_policy_ref"),
        trust_policy_revision=_str(
            _req(m, "trust_policy_revision"), "trust_policy_revision"
        ),
        trust_policy_digest=_str(_req(m, "trust_policy_digest"), "trust_policy_digest"),
        input_evidence_bindings=tuple(bindings),
        derived_payload_digest=_str(
            _req(m, "derived_payload_digest"), "derived_payload_digest"
        ),
        before_inherited_floor=_str(
            _req(m, "before_inherited_floor"), "before_inherited_floor"
        ),
        before_effective_floor=_str(
            _req(m, "before_effective_floor"), "before_effective_floor"
        ),
        after_effective_floor=_str(
            _req(m, "after_effective_floor"), "after_effective_floor"
        ),
        purpose_scope=_tuple_str(_req(m, "purpose_scope"), "purpose_scope"),
        rationale=_str(_req(m, "rationale"), "rationale"),
        decision=_str(_req(m, "decision"), "decision"),
        issued_at=_str(_req(m, "issued_at"), "issued_at"),
        expires_at=_str(_req(m, "expires_at"), "expires_at"),
        supersedes_receipt_ref=_opt_str(
            m.get("supersedes_receipt_ref"), "supersedes_receipt_ref"
        ),
        receipt_digest=_str(_req(m, "receipt_digest"), "receipt_digest"),
    )


def memory_view_from_json(m: Mapping[str, object]) -> MemoryView:
    """Parse a MemoryView. Item floors/disposition are OPTIONAL at parse (an
    incomplete item becomes empty-string/None) so ``validate_memory_view`` can
    catch the INV-7 omission — the completeness rule is a check, not a type."""
    if not isinstance(m, Mapping):
        raise SchemaError("memory view must be an object")
    items_raw = _req(m, "items")
    if not isinstance(items_raw, (list, tuple)):
        raise SchemaError("items must be a list")
    items: list[MemoryViewItem] = []
    for entry in items_raw:
        if not isinstance(entry, Mapping):
            raise SchemaError("view item must be an object")
        disp_raw = entry.get("use_disposition")
        items.append(
            MemoryViewItem(
                memory_id=_str(_req(entry, "memory_id"), "memory_id"),
                version_id=_str(_req(entry, "version_id"), "version_id"),
                bounded_content=_str(entry.get("bounded_content", ""), "bounded_content"),
                source_refs=_tuple_str(entry.get("source_refs", []), "source_refs"),
                claim_ids=_tuple_str(entry.get("claim_ids", []), "claim_ids"),
                confidence_kind=_enum(
                    ConfidenceKind,
                    entry.get("confidence_kind", ConfidenceKind.OBSERVED.value),
                    "confidence_kind",
                ),
                freshness=_str(entry.get("freshness", ""), "freshness"),
                contradiction_notice=_str(
                    entry.get("contradiction_notice", ""), "contradiction_notice"
                ),
                trust_policy_ref=_str(entry.get("trust_policy_ref", ""), "trust_policy_ref"),
                trust_policy_revision=_str(
                    entry.get("trust_policy_revision", ""), "trust_policy_revision"
                ),
                trust_policy_digest=_str(
                    entry.get("trust_policy_digest", ""), "trust_policy_digest"
                ),
                inherited_trust_floor=_str(
                    entry.get("inherited_trust_floor", ""), "inherited_trust_floor"
                ),
                effective_trust_floor=_str(
                    entry.get("effective_trust_floor", ""), "effective_trust_floor"
                ),
                adjudication_receipt_refs=_tuple_str(
                    entry.get("adjudication_receipt_refs", []),
                    "adjudication_receipt_refs",
                ),
                use_disposition=(
                    None
                    if disp_raw is None
                    else _enum(UseDisposition, disp_raw, "use_disposition")
                ),
            )
        )
    counts_raw = m.get("omitted_reason_counts", {})
    if not isinstance(counts_raw, Mapping):
        raise SchemaError("omitted_reason_counts must be an object")
    return MemoryView(
        request_id=_str(_req(m, "request_id"), "request_id"),
        view_id=_str(_req(m, "view_id"), "view_id"),
        purpose=_str(_req(m, "purpose"), "purpose"),
        generated_at=_str(_req(m, "generated_at"), "generated_at"),
        expires_at=_opt_str(m.get("expires_at"), "expires_at"),
        items=tuple(items),
        denied_count=int(_req(m, "denied_count")),  # type: ignore[arg-type]
        omitted_reason_counts={
            _str(k, "reason"): int(v) for k, v in counts_raw.items()  # type: ignore[arg-type]
        },
        token_count=int(m.get("token_count", 0)),  # type: ignore[arg-type]
    )


__all__ = [
    "CANONICALIZATION_REF",
    "RECEIPT_SCHEMA_VERSION",
    "trust_policy_content_digest",
    "SchemaError",
    "MemoryClass",
    "ConfidenceKind",
    "RetentionShape",
    "EvidenceRole",
    "UseDisposition",
    "LifecycleState",
    "LIFECYCLE_TRANSITIONS",
    "is_legal_transition",
    "TrustPolicy",
    "meet_labels",
    "SourceRef",
    "SourceIntegrity",
    "ClaimTrust",
    "EvidenceBinding",
    "ObservationCandidate",
    "RetentionProposal",
    "MemoryEnvelope",
    "RetrievalRequest",
    "MemoryViewItem",
    "MemoryView",
    "TrustAdjudicationReceipt",
    "canonical_json_bytes",
    "receipt_to_json",
    "compute_receipt_digest",
    "policy_from_json",
    "envelope_from_json",
    "receipt_from_json",
    "memory_view_from_json",
]
