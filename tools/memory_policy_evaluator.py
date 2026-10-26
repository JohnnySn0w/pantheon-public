"""Deny-by-default ORG-058 memory policy evaluator + lint CLI (T-3197).

Pure functions over the ``memory_envelope`` typed schema that judge provenance
completeness, trust-floor inheritance/immutability, adjudication-receipt exact
binding, retention authority, and purpose-bounded retrieval. Every judgement is
a typed ``Decision`` RETURN VALUE (str-Enum reason code), never an exception:
policy denials are values, and MALFORMED input also denies (fail-closed). The
module owns no mutation path, runtime, or live authority — ``evaluate_retention``
returns a decision and does NOT write; ``evaluate_retrieval`` returns a
``MemoryView`` and confers no action authority.

Invariants enforced (each has a test that goes RED when the guard is removed):
INV-1 four dimensions never collapse; INV-2 retrieval is never an action token;
INV-3 unknown policy / missing metadata / absent meet -> conservative-or-refuse;
INV-4 ``inherited_trust_floor`` is immutable recompute output and
``effective != inherited`` needs a valid bound receipt; INV-5 canonical source
authority preserved (envelope holds a ref, never rewrites source); INV-6
retrieval authorization is evaluated BEFORE ranking so denials leak only as
aggregate counters; INV-7 ``MemoryView`` never omits floors/``use_disposition``
for returned content.

Citations to Mason or T-2425 anywhere below carry a T-2425 evidence label
(measured | vendor claim | inference | roadmap), distinct from the envelope's
epistemic ``confidence_kind`` (observed | asserted | inferred | generated).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from memory_envelope import (  # noqa: E402
    CANONICALIZATION_REF,
    RECEIPT_SCHEMA_VERSION,
    ClaimTrust,
    ConfidenceKind,
    MemoryEnvelope,
    MemoryView,
    MemoryViewItem,
    RetentionProposal,
    RetrievalRequest,
    SchemaError,
    SourceIntegrity,
    TrustAdjudicationReceipt,
    TrustPolicy,
    UseDisposition,
    compute_receipt_digest,
    envelope_from_json,
    policy_from_json,
    receipt_from_json,
)


class MemoryPolicyReason(str, Enum):
    """Closed reason vocabulary. ``AUTHORIZED`` is the only allow value; every
    other member is a denial code."""

    AUTHORIZED = "authorized"
    MALFORMED_INPUT = "malformed-input"
    # --- policy resolution ---
    MISSING_TRUST_POLICY_REF = "missing-trust-policy-ref"
    UNKNOWN_POLICY = "unknown-policy"
    POLICY_DIGEST_MISMATCH = "policy-digest-mismatch"
    # --- provenance / dimension separation ---
    MISSING_CANONICAL_SOURCE = "missing-canonical-source-ref"
    MISSING_RETENTION_FIELDS = "missing-retention-fields"
    MISSING_INTEGRITY_METADATA = "missing-integrity-metadata"
    NO_MEET = "no-meet-conservative-refuse"
    DIMENSION_CROSS_CONTAMINATION = "dimension-cross-contamination"
    # --- trust floor inheritance ---
    INHERITED_FLOOR_MISMATCH = "inherited-floor-mismatch"
    EFFECTIVE_FLOOR_INCONSISTENT = "effective-floor-inconsistent"
    EFFECTIVE_WITHOUT_RECEIPT = "effective-floor-without-receipt"
    # --- receipt binding ---
    RECEIPT_POLICY_MISMATCH = "receipt-policy-mismatch"
    RECEIPT_WRONG_CANONICALIZATION = "receipt-wrong-canonicalization"
    RECEIPT_VERSION_MISMATCH = "receipt-version-mismatch"
    RECEIPT_CLAIM_UNBOUND = "receipt-claim-unbound"
    RECEIPT_DOES_NOT_BIND_CLAIM = "receipt-does-not-bind-claim"
    RECEIPT_SUPERSEDED = "receipt-superseded-or-inactive"
    RECEIPT_AUTHORITY_INVALID = "receipt-authority-invalid"
    RECEIPT_SELF_ADJUDICATION = "receipt-self-adjudication"
    RECEIPT_EVIDENCE_MISMATCH = "receipt-evidence-binding-mismatch"
    RECEIPT_PAYLOAD_DIGEST_MISMATCH = "receipt-payload-digest-mismatch"
    RECEIPT_PURPOSE_MISMATCH = "receipt-purpose-mismatch"
    RECEIPT_EXPIRED = "receipt-expired-or-bad-window"
    RECEIPT_NOT_YET_VALID = "receipt-not-yet-valid"
    RECEIPT_BEFORE_FLOOR_MISMATCH = "receipt-before-floor-mismatch"
    RECEIPT_AFTER_FLOOR_MISMATCH = "receipt-after-effective-floor-mismatch"
    RECEIPT_DIGEST_MISMATCH = "receipt-digest-mismatch"
    # --- retention ---
    RETENTION_AUTHORITY_MISSING = "retention-authority-missing"
    RETENTION_MISSING_CANONICAL_SOURCE = "retention-missing-canonical-source"
    # --- view ---
    VIEW_INCOMPLETE = "memory-view-incomplete"


@dataclass(frozen=True)
class Decision:
    """Typed verdict. ``allowed`` is the boolean gate; ``reason_code`` is closed
    and machine-checkable; ``detail`` is human context only. The floor carriers
    are populated on the trust-floor paths for auditability."""

    allowed: bool
    reason_code: MemoryPolicyReason
    detail: str = ""
    inherited_floor: str | None = None
    effective_floor: str | None = None


def _allow(
    *,
    detail: str = "",
    inherited_floor: str | None = None,
    effective_floor: str | None = None,
) -> Decision:
    return Decision(
        True,
        MemoryPolicyReason.AUTHORIZED,
        detail,
        inherited_floor,
        effective_floor,
    )


def _deny(reason: MemoryPolicyReason, detail: str = "") -> Decision:
    return Decision(False, reason, detail)


# ---------------------------------------------------------------------------
# Inherited-floor derivation (INV-3/INV-4).
# ---------------------------------------------------------------------------
def _inherited_floor_with_reason(
    claim: ClaimTrust,
    source_integrities: Sequence[SourceIntegrity],
    policy: TrustPolicy | None,
) -> tuple[str | None, MemoryPolicyReason | None]:
    """Recompute a claim's inherited floor as the meet of its CONTRIBUTING
    evidence labels. Contradicting evidence stays independently visible and may
    block use, but does not enter the meet (T-2447: it "does not change the
    claim's computed floor"). Returns ``(label, None)`` or ``(None, reason)``."""
    if policy is None:
        return None, MemoryPolicyReason.UNKNOWN_POLICY
    label_by_ref: dict[str, str] = {si.evidence_ref: si.integrity_label for si in source_integrities}
    contributing = claim.contributing_evidence_refs
    if not contributing:
        # A derived claim with no contributing evidence has no floor to inherit.
        return None, MemoryPolicyReason.MISSING_INTEGRITY_METADATA
    labels: list[str] = []
    for ref in contributing:
        label = label_by_ref.get(ref)
        if label is None or label not in policy.labels:
            return None, MemoryPolicyReason.MISSING_INTEGRITY_METADATA
        labels.append(label)
    floor = policy.meet(labels)
    if floor is None:
        return None, MemoryPolicyReason.NO_MEET
    return floor, None


def compute_inherited_floor(
    claim: ClaimTrust,
    source_integrities: Sequence[SourceIntegrity],
    policy: TrustPolicy | None,
) -> str | None:
    """Public form: the meet label, or ``None`` (REFUSE — fail closed per INV-3)."""
    floor, _reason = _inherited_floor_with_reason(claim, source_integrities, policy)
    return floor


def _resolve_policy(
    ref: str, revision: str, digest: str, policy: TrustPolicy | None
) -> Decision | None:
    """Shared deny-by-default policy resolution. Returns a denial Decision, or
    ``None`` when the named+revisioned+digested policy resolves exactly."""
    if not ref:
        return _deny(MemoryPolicyReason.MISSING_TRUST_POLICY_REF, "empty trust_policy_ref")
    if policy is None:
        return _deny(MemoryPolicyReason.UNKNOWN_POLICY, "policy not resolvable")
    if ref != policy.policy_ref or revision != policy.revision:
        return _deny(
            MemoryPolicyReason.UNKNOWN_POLICY,
            f"policy ref/revision {ref}@{revision} != {policy.policy_ref}@{policy.revision}",
        )
    if digest != policy.digest:
        return _deny(MemoryPolicyReason.POLICY_DIGEST_MISMATCH, "trust_policy_digest mismatch")
    return None


def _resolve_receipts(
    receipts: Mapping[str, TrustAdjudicationReceipt]
    | Sequence[TrustAdjudicationReceipt]
    | None,
) -> dict[str, TrustAdjudicationReceipt] | None:
    """Normalize the caller-supplied receipts to an id->receipt map, or None
    when none were supplied (which forces a raised claim to fail closed)."""
    if receipts is None:
        return None
    if isinstance(receipts, Mapping):
        return dict(receipts)
    return {r.receipt_id: r for r in receipts}


# ---------------------------------------------------------------------------
# Envelope validation (INV-1, INV-4, INV-5).
# ---------------------------------------------------------------------------
def validate_envelope(
    env: object,
    policy: TrustPolicy | None,
    receipts: Mapping[str, TrustAdjudicationReceipt]
    | Sequence[TrustAdjudicationReceipt]
    | None = None,
) -> Decision:
    """Validate a memory envelope against its named policy, fail-closed.

    ``receipts`` (optional, positionally compatible) supplies the adjudication
    receipts available for this envelope so a raised (``effective != inherited``)
    claim can be verified end-to-end (F1). Absent receipts for a raised claim is
    a DENIAL — an unverifiable raise never passes.
    """
    if not isinstance(env, MemoryEnvelope):
        return _deny(MemoryPolicyReason.MALFORMED_INPUT, "not a MemoryEnvelope")

    resolved = _resolve_policy(
        env.trust_policy_ref, env.trust_policy_revision, env.trust_policy_digest, policy
    )
    if resolved is not None:
        return resolved
    assert policy is not None  # narrowed by _resolve_policy
    receipt_map = _resolve_receipts(receipts)

    # INV-5: a canonical source ref must actually locate a source.
    src = env.canonical_source_ref
    if not src.source_system or not src.object_id:
        return _deny(MemoryPolicyReason.MISSING_CANONICAL_SOURCE, "canonical_source_ref incomplete")

    # Retention/correction/forgetting behavior must be present (ORG-058 §4).
    if not env.correction_policy or not env.forgetting_policy:
        return _deny(MemoryPolicyReason.MISSING_RETENTION_FIELDS, "correction/forgetting policy empty")

    # INV-1: no value from another dimension may sit in a trust field. The
    # structural guard is "every trust label is a declared integrity label";
    # when the offending value is a known OTHER-dimension value we name the
    # cross-contamination explicitly.
    foreign = {ck.value for ck in ConfidenceKind}
    foreign.update(
        {env.confidence_kind.value, env.privacy_class, env.audience, env.retrieval_authority}
    )
    if env.action_authority_ref:
        foreign.add(env.action_authority_ref)
    trust_labels: list[str] = [env.inherited_trust_floor, env.effective_trust_floor]
    for c in env.claim_trust:
        trust_labels.extend([c.inherited_trust_floor, c.effective_trust_floor])
    for si in env.source_integrity:
        trust_labels.append(si.integrity_label)
    for label in trust_labels:
        if label not in policy.labels:
            if label in foreign:
                return _deny(
                    MemoryPolicyReason.DIMENSION_CROSS_CONTAMINATION,
                    f"non-integrity value {label!r} in a trust field",
                )
            return _deny(
                MemoryPolicyReason.MISSING_INTEGRITY_METADATA,
                f"trust label {label!r} not a declared integrity label",
            )

    # INV-4: per-claim inherited-floor recompute + immutability.
    for c in env.claim_trust:
        recomputed, reason = _inherited_floor_with_reason(c, env.source_integrity, policy)
        if recomputed is None:
            return _deny(reason or MemoryPolicyReason.NO_MEET, f"claim {c.claim_id}: no inherited floor")
        if c.inherited_trust_floor != recomputed:
            return _deny(
                MemoryPolicyReason.INHERITED_FLOOR_MISMATCH,
                f"claim {c.claim_id}: stored {c.inherited_trust_floor!r} != recomputed {recomputed!r}",
            )
        # effective != inherited requires a VALID bound receipt. F1: compose the
        # whole check here rather than trusting the ref to exist — resolve the
        # receipt, run validate_receipt, and require it to bind THIS claim.
        if c.effective_trust_floor != c.inherited_trust_floor:
            ref = c.adjudication_receipt_ref
            if not ref or ref not in env.trust_adjudication_receipt_refs:
                return _deny(
                    MemoryPolicyReason.EFFECTIVE_WITHOUT_RECEIPT,
                    f"claim {c.claim_id}: effective != inherited without a bound receipt",
                )
            if receipt_map is None:
                return _deny(
                    MemoryPolicyReason.EFFECTIVE_WITHOUT_RECEIPT,
                    f"claim {c.claim_id}: raised but no receipts supplied to verify",
                )
            receipt = receipt_map.get(ref)
            if receipt is None:
                return _deny(
                    MemoryPolicyReason.EFFECTIVE_WITHOUT_RECEIPT,
                    f"claim {c.claim_id}: bound receipt {ref} not available to verify",
                )
            rd = validate_receipt(receipt, env, policy)
            if not rd.allowed:
                return _deny(rd.reason_code, f"claim {c.claim_id}: bound receipt invalid: {rd.detail}")
            if c.claim_id not in receipt.claim_ids:
                return _deny(
                    MemoryPolicyReason.RECEIPT_DOES_NOT_BIND_CLAIM,
                    f"claim {c.claim_id}: receipt {ref} does not bind it",
                )

    # Envelope conservative whole-item floors = meet of claim floors (ORG-058 §4).
    if env.claim_trust:
        exp_inh = policy.meet([c.inherited_trust_floor for c in env.claim_trust])
        if exp_inh is None:
            return _deny(MemoryPolicyReason.NO_MEET, "no meet over claim inherited floors")
        if env.inherited_trust_floor != exp_inh:
            return _deny(
                MemoryPolicyReason.INHERITED_FLOOR_MISMATCH,
                f"envelope inherited {env.inherited_trust_floor!r} != meet {exp_inh!r}",
            )
        exp_eff = policy.meet([c.effective_trust_floor for c in env.claim_trust])
        if exp_eff is None:
            return _deny(MemoryPolicyReason.NO_MEET, "no meet over claim effective floors")
        if env.effective_trust_floor != exp_eff:
            return _deny(
                MemoryPolicyReason.EFFECTIVE_FLOOR_INCONSISTENT,
                f"envelope effective {env.effective_trust_floor!r} != meet {exp_eff!r}",
            )

    # Envelope-level effective != inherited also requires adjudication. When
    # claims exist, each raise was verified per-claim above (env floors are the
    # meet of the claim floors); a no-claim envelope has nothing to bind a raise
    # to, so it fails closed (F1).
    if env.effective_trust_floor != env.inherited_trust_floor:
        if not env.trust_adjudication_receipt_refs:
            return _deny(
                MemoryPolicyReason.EFFECTIVE_WITHOUT_RECEIPT,
                "envelope effective != inherited without any adjudication receipt",
            )
        if not env.claim_trust:
            return _deny(
                MemoryPolicyReason.EFFECTIVE_WITHOUT_RECEIPT,
                "envelope-level raise with no claim to bind it",
            )

    return _allow(
        inherited_floor=env.inherited_trust_floor,
        effective_floor=env.effective_trust_floor,
    )


# ---------------------------------------------------------------------------
# Receipt validation (INV-4). EXACT binding to (memory version, claim set,
# policy revision, purpose, evidence set, transition).
# ---------------------------------------------------------------------------
def validate_receipt(
    receipt: object,
    env: object,
    policy: TrustPolicy | None,
    *,
    now: str | None = None,
) -> Decision:
    if not isinstance(receipt, TrustAdjudicationReceipt) or not isinstance(env, MemoryEnvelope):
        return _deny(MemoryPolicyReason.MALFORMED_INPUT, "receipt/env wrong type")

    resolved = _resolve_policy(
        receipt.trust_policy_ref,
        receipt.trust_policy_revision,
        receipt.trust_policy_digest,
        policy,
    )
    if resolved is not None:
        # Re-map policy-resolution denials to the receipt namespace.
        return _deny(MemoryPolicyReason.RECEIPT_POLICY_MISMATCH, resolved.detail)
    assert policy is not None

    if (
        receipt.canonicalization_ref != CANONICALIZATION_REF
        or receipt.receipt_schema_version != RECEIPT_SCHEMA_VERSION
    ):
        return _deny(
            MemoryPolicyReason.RECEIPT_WRONG_CANONICALIZATION,
            f"schema/canon {receipt.receipt_schema_version}/{receipt.canonicalization_ref}",
        )

    if receipt.memory_id != env.memory_id or receipt.version_id != env.version_id:
        return _deny(MemoryPolicyReason.RECEIPT_VERSION_MISMATCH, "memory_id/version_id mismatch")

    env_claim_ids = {c.claim_id for c in env.claim_trust}
    if not receipt.claim_ids or not set(receipt.claim_ids).issubset(env_claim_ids):
        return _deny(MemoryPolicyReason.RECEIPT_CLAIM_UNBOUND, "claim_ids not a subset of envelope")
    bound_claims = [c for c in env.claim_trust if c.claim_id in set(receipt.claim_ids)]
    for c in bound_claims:
        if c.adjudication_receipt_ref != receipt.receipt_id:
            return _deny(
                MemoryPolicyReason.RECEIPT_CLAIM_UNBOUND,
                f"claim {c.claim_id} does not point back to {receipt.receipt_id}",
            )
    # F1 defense-in-depth: a claim that POINTS to this receipt but is NOT among
    # its claim_ids is a laundered raise — the receipt adjudicates other claims
    # and cannot vouch for this one. Deny even when called standalone.
    for c in env.claim_trust:
        if c.adjudication_receipt_ref == receipt.receipt_id and c.claim_id not in set(receipt.claim_ids):
            return _deny(
                MemoryPolicyReason.RECEIPT_DOES_NOT_BIND_CLAIM,
                f"claim {c.claim_id} references {receipt.receipt_id} but is not in its claim_ids",
            )
    # Supersession / active-state: the receipt must be an active envelope receipt.
    if receipt.receipt_id not in env.trust_adjudication_receipt_refs:
        return _deny(
            MemoryPolicyReason.RECEIPT_SUPERSEDED,
            "receipt_id is not among the envelope's active adjudication receipts",
        )

    # Authority: present and independent of BOTH the proposer and the
    # adjudicator (the receipt POINTS to authority, it is not itself authority,
    # and neither party may name itself the authority — ORG-040/ORG-058 §6).
    if not receipt.authority_ref or receipt.authority_ref in (
        receipt.proposer_ref,
        receipt.adjudicator_ref,
    ):
        return _deny(MemoryPolicyReason.RECEIPT_AUTHORITY_INVALID, "authority absent or not independent")

    # Separation of duty: proposer may not self-adjudicate unless policy permits.
    if receipt.proposer_ref == receipt.adjudicator_ref and not policy.self_adjudication_allowed:
        return _deny(MemoryPolicyReason.RECEIPT_SELF_ADJUDICATION, "proposer == adjudicator")

    # Evidence bindings match the bound claims' contributing evidence EXACTLY —
    # no missing, no duplicate (list-of-records, never parallel arrays).
    contributing_refs: set[str] = set()
    for c in bound_claims:
        contributing_refs.update(c.contributing_evidence_refs)
    expected = {
        (si.evidence_ref, si.source_revision, si.source_digest)
        for si in env.source_integrity
        if si.evidence_ref in contributing_refs
    }
    actual_list = [(b.evidence_ref, b.source_revision, b.digest) for b in receipt.input_evidence_bindings]
    if len(actual_list) != len(set(actual_list)):
        return _deny(MemoryPolicyReason.RECEIPT_EVIDENCE_MISMATCH, "duplicate evidence binding")
    if set(actual_list) != expected:
        return _deny(
            MemoryPolicyReason.RECEIPT_EVIDENCE_MISMATCH,
            "evidence bindings do not exactly match the bound claims' contributing evidence",
        )

    if receipt.derived_payload_digest != env.payload_digest:
        return _deny(MemoryPolicyReason.RECEIPT_PAYLOAD_DIGEST_MISMATCH, "derived_payload_digest mismatch")

    if not receipt.purpose_scope:
        return _deny(MemoryPolicyReason.RECEIPT_PURPOSE_MISMATCH, "empty purpose_scope")

    # Time window: structural (expiry strictly after issue) + optional clock.
    if not receipt.issued_at or not receipt.expires_at or receipt.expires_at <= receipt.issued_at:
        return _deny(MemoryPolicyReason.RECEIPT_EXPIRED, "non-positive validity window")
    if now is not None:
        if now < receipt.issued_at:
            return _deny(MemoryPolicyReason.RECEIPT_NOT_YET_VALID, f"now {now} < issued {receipt.issued_at}")
        if now > receipt.expires_at:
            return _deny(MemoryPolicyReason.RECEIPT_EXPIRED, f"now {now} > expires {receipt.expires_at}")

    # Transition floors bind to the claim's recorded pre/post floors.
    for c in bound_claims:
        if receipt.before_inherited_floor != c.inherited_trust_floor:
            return _deny(
                MemoryPolicyReason.RECEIPT_BEFORE_FLOOR_MISMATCH,
                f"before_inherited {receipt.before_inherited_floor!r} != claim inherited {c.inherited_trust_floor!r}",
            )
        if receipt.after_effective_floor != c.effective_trust_floor:
            return _deny(
                MemoryPolicyReason.RECEIPT_AFTER_FLOOR_MISMATCH,
                f"after_effective {receipt.after_effective_floor!r} != claim effective {c.effective_trust_floor!r}",
            )
    if receipt.before_inherited_floor not in policy.labels or receipt.after_effective_floor not in policy.labels:
        return _deny(MemoryPolicyReason.MISSING_INTEGRITY_METADATA, "receipt floor not a declared label")
    # F2: before_effective must be a declared label and cannot sit BELOW the
    # inherited floor (a prior effective is always at or above inherited).
    if receipt.before_effective_floor not in policy.labels or not policy.leq(
        receipt.before_inherited_floor, receipt.before_effective_floor
    ):
        return _deny(
            MemoryPolicyReason.RECEIPT_BEFORE_FLOOR_MISMATCH,
            "before_effective_floor absent, unknown, or below the inherited floor",
        )

    # Non-self-referential digest, correct canonicalization: recompute over the
    # preimage that EXCLUDES receipt_digest and compare.
    expected_digest = compute_receipt_digest(receipt)
    if receipt.receipt_digest != expected_digest:
        return _deny(
            MemoryPolicyReason.RECEIPT_DIGEST_MISMATCH,
            "receipt_digest does not match canonical exclusive-preimage digest",
        )

    return _allow(
        detail="receipt validly binds the effective floor",
        inherited_floor=receipt.before_inherited_floor,
        effective_floor=receipt.after_effective_floor,
    )


# ---------------------------------------------------------------------------
# Retention (returns a decision; DOES NOT write) and retrieval.
# ---------------------------------------------------------------------------
def evaluate_retention(
    proposal: object, authority: str | None, policy: TrustPolicy | None
) -> Decision:
    if not isinstance(proposal, RetentionProposal):
        return _deny(MemoryPolicyReason.MALFORMED_INPUT, "not a RetentionProposal")
    if not proposal.required_authority:
        return _deny(MemoryPolicyReason.RETENTION_AUTHORITY_MISSING, "proposal names no required authority")
    if not authority or authority != proposal.required_authority:
        return _deny(
            MemoryPolicyReason.RETENTION_AUTHORITY_MISSING,
            "presented authority does not satisfy required_authority",
        )
    src = proposal.canonical_source_ref
    if not src.source_system or not src.object_id:
        return _deny(MemoryPolicyReason.RETENTION_MISSING_CANONICAL_SOURCE, "canonical source incomplete")
    return _allow(detail="retention authorized (no write performed)")


def _disposition(
    effective_floor: str, purpose_floor: str | None, policy: TrustPolicy
) -> UseDisposition:
    """Purpose-derived disposition (ORG-058 §5). Fail closed: an unmapped
    purpose, an unknown label, or an incomparable pair REFUSES. Below-floor but
    comparable content is QUARANTINED (returned WITH its floor, never used as
    sole support). Model confidence never appears here (INV-1)."""
    if purpose_floor is None:
        return UseDisposition.REFUSED
    if effective_floor not in policy.labels or purpose_floor not in policy.labels:
        return UseDisposition.REFUSED
    if policy.leq(purpose_floor, effective_floor):
        return UseDisposition.ALLOWED
    if policy.leq(effective_floor, purpose_floor):
        return UseDisposition.QUARANTINED
    return UseDisposition.REFUSED


def _view_item(env: MemoryEnvelope, disposition: UseDisposition) -> MemoryViewItem:
    # INV-2: no action-authority field is ever copied into the view.
    # INV-7: floors, policy ref, receipt refs, contradiction notice, and the
    # disposition are ALWAYS populated for returned content.
    return MemoryViewItem(
        memory_id=env.memory_id,
        version_id=env.version_id,
        bounded_content=env.payload_ref,
        source_refs=tuple(env.supporting_refs),
        claim_ids=tuple(c.claim_id for c in env.claim_trust),
        confidence_kind=env.confidence_kind,
        freshness=env.observed_at,
        contradiction_notice="contradicted" if env.contradicting_refs else "",
        trust_policy_ref=env.trust_policy_ref,
        trust_policy_revision=env.trust_policy_revision,
        trust_policy_digest=env.trust_policy_digest,
        inherited_trust_floor=env.inherited_trust_floor,
        effective_trust_floor=env.effective_trust_floor,
        adjudication_receipt_refs=tuple(env.trust_adjudication_receipt_refs),
        use_disposition=disposition,
    )


def evaluate_retrieval(
    request: object, envelopes: Sequence[object], policy: TrustPolicy | None
) -> MemoryView:
    """Authorize BEFORE ranking (INV-6), then bound the view.

    What THIS evaluator authorizes on: subject_scope match, memory_class match,
    privacy_ceiling admissibility, the purpose-derived trust-floor disposition,
    and the item/token budgets. Authorization denials and floor-based omissions
    surface only as aggregate counters, never as items/snippets/timing. No
    action authority is ever conferred (INV-2).

    Deferred to T-3200 (user-visibility + reader-class authority — NOT
    implemented here to avoid conflicting with that charter): per-caller /
    persona reader-authorization, and recency/depth ranking. ``caller_identity``,
    ``persona``, ``recency`` and ``depth`` are carried on the request but are not
    yet authorization or ranking inputs at this layer.

    CONTENT-BLIND CONTRACT (T-4810): this evaluator never dereferences a payload.
    ``MemoryViewItem.bounded_content`` is the envelope's payload REFERENCE
    (``payload_ref``), not content. ``token_budget`` and ``MemoryView.token_count``
    are therefore measured in CHARACTERS OF THE REF STRINGS, not content tokens.
    Ordering is deterministic by ``(memory_id, version_id)``, NOT relevance, and
    ``item_budget`` keeps the first N in that order. A caller that needs a content
    budget or relevance ranking must dereference the payloads AFTER authorization
    and meter and rank them itself (reference caller: ``tools/pmb1/governed.py``).
    Authorization still happens before any ordering (INV-6).
    """
    if not isinstance(request, RetrievalRequest):
        return MemoryView(
            request_id="",
            view_id="",
            purpose="",
            generated_at="",
            expires_at=None,
            items=(),
            denied_count=0,
            omitted_reason_counts={"malformed_request": 1},
            token_count=0,
        )

    omitted: dict[str, int] = {}
    denied = 0

    def _view(items: tuple[MemoryViewItem, ...], token_count: int) -> MemoryView:
        return MemoryView(
            request_id=request.request_id,
            view_id=f"view-{request.request_id}",
            purpose=request.purpose,
            generated_at=request.request_time,
            expires_at=None,
            items=items,
            denied_count=denied,
            omitted_reason_counts=dict(omitted),
            token_count=token_count,
        )

    if policy is None:
        omitted["unknown_policy"] = len(list(envelopes))
        return _view((), 0)

    purpose_floor = policy.purpose_floors.get(request.purpose)

    # Step 1 — authorization (privacy/subject/class), evaluated before ranking.
    authorized: list[MemoryEnvelope] = []
    for env in envelopes:
        if not isinstance(env, MemoryEnvelope):
            omitted["malformed_envelope"] = omitted.get("malformed_envelope", 0) + 1
            continue
        if request.subject_scope and not (set(env.subjects) & set(request.subject_scope)):
            omitted["out_of_subject_scope"] = omitted.get("out_of_subject_scope", 0) + 1
            continue
        if request.memory_classes and env.memory_class not in request.memory_classes:
            omitted["class_excluded"] = omitted.get("class_excluded", 0) + 1
            continue
        if not policy.privacy_admits(request.privacy_ceiling, env.privacy_class):
            denied += 1  # aggregate only — no floor/snippet/timing leak
            continue
        authorized.append(env)

    # Step 2 — rank AFTER authorization (deterministic).
    authorized.sort(key=lambda e: (e.memory_id, e.version_id))

    # Step 3 — per-item disposition; REFUSED omitted, others returned complete.
    items: list[MemoryViewItem] = []
    for env in authorized:
        disp = _disposition(env.effective_trust_floor, purpose_floor, policy)
        if disp is UseDisposition.REFUSED:
            omitted["below_purpose_floor"] = omitted.get("below_purpose_floor", 0) + 1
            continue
        items.append(_view_item(env, disp))

    # Step 4 — budget truncation (a budget cut, not an authorization denial).
    budget = request.item_budget if request.item_budget > 0 else len(items)
    if len(items) > budget:
        omitted["item_budget_truncated"] = omitted.get("item_budget_truncated", 0) + (len(items) - budget)
        items = items[:budget]

    # F3 — ref-length budget: keep cumulative len(bounded_content) within
    # token_budget (when >0). bounded_content is the payload REF string, so this
    # meters characters of refs, not content tokens (content-blind, T-4810). An item that would overflow is dropped, not content-truncated,
    # and counted as an aggregate; token_count reports the retained total.
    token_count = sum(len(i.bounded_content) for i in items)
    if request.token_budget > 0 and token_count > request.token_budget:
        kept: list[MemoryViewItem] = []
        used = 0
        dropped = 0
        for it in items:
            length = len(it.bounded_content)
            if used + length <= request.token_budget:
                kept.append(it)
                used += length
            else:
                dropped += 1
        if dropped:
            omitted["token_budget_truncated"] = omitted.get("token_budget_truncated", 0) + dropped
        items = kept
        token_count = used

    return _view(tuple(items), token_count)


def validate_memory_view(view: object) -> Decision:
    """INV-7: a returned item may not hide its floors/policy/disposition from a
    consumer that can use the content."""
    if not isinstance(view, MemoryView):
        return _deny(MemoryPolicyReason.MALFORMED_INPUT, "not a MemoryView")
    for item in view.items:
        if (
            not item.inherited_trust_floor
            or not item.effective_trust_floor
            or not item.trust_policy_ref
            or item.use_disposition is None
        ):
            return _deny(
                MemoryPolicyReason.VIEW_INCOMPLETE,
                f"item {item.memory_id} omits a floor / policy ref / use_disposition",
            )
    return _allow(detail="view items are complete")


# ---------------------------------------------------------------------------
# Lint / evaluator CLI (mirrors tools/ledger_placement_lint.py).
# ---------------------------------------------------------------------------
def _load_json(path: str) -> object:
    return json.loads(Path(path).read_text())


def lint_files(
    policy_path: str,
    envelope_path: str,
    receipt_path: str | None = None,
    *,
    now: str | None = None,
) -> list[dict]:
    """Run validate_envelope (+ validate_receipt) over fixture files. Returns a
    list of violation dicts (empty == clean). SchemaError (structural fault,
    incl. parallel evidence arrays) is itself a violation — fail closed."""
    findings: list[dict] = []
    try:
        policy = policy_from_json(_load_json(policy_path))  # type: ignore[arg-type]
    except (SchemaError, ValueError, OSError) as exc:
        return [{"source": policy_path, "reason": "policy-parse-error", "detail": str(exc)}]

    try:
        env = envelope_from_json(_load_json(envelope_path))  # type: ignore[arg-type]
    except (SchemaError, ValueError, OSError) as exc:
        findings.append({"source": envelope_path, "reason": "envelope-parse-error", "detail": str(exc)})
        return findings

    # Parse the receipt first (if any) so the composed F1 verification runs
    # end-to-end inside validate_envelope.
    receipt: TrustAdjudicationReceipt | None = None
    if receipt_path:
        try:
            receipt = receipt_from_json(_load_json(receipt_path))  # type: ignore[arg-type]
        except (SchemaError, ValueError, OSError) as exc:
            findings.append({"source": receipt_path, "reason": "receipt-parse-error", "detail": str(exc)})
            return findings

    receipts = {receipt.receipt_id: receipt} if receipt is not None else None
    decision = validate_envelope(env, policy, receipts=receipts)
    if not decision.allowed:
        findings.append(
            {"source": envelope_path, "reason": decision.reason_code.value, "detail": decision.detail}
        )

    if receipt is not None:
        rdecision = validate_receipt(receipt, env, policy, now=now)
        if not rdecision.allowed:
            findings.append(
                {"source": receipt_path, "reason": rdecision.reason_code.value, "detail": rdecision.detail}
            )

    return findings


def format_lines(findings: list[dict]) -> list[str]:
    return [f"{f['source']}: {f['reason']} -- {f['detail']}" for f in findings]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Deny-by-default ORG-058 memory envelope/receipt lint (T-3197). "
        "Exit 0 clean, 1 on any violation (incl. malformed/parallel-array input).",
    )
    ap.add_argument("--policy", required=True, help="trust policy fixture JSON")
    ap.add_argument("--envelope", required=True, help="memory envelope fixture JSON")
    ap.add_argument("--receipt", help="optional adjudication receipt fixture JSON")
    ap.add_argument("--now", help="optional ISO clock for receipt time-window checks")
    ap.add_argument("--json", action="store_true", help="emit JSON findings")
    args = ap.parse_args(argv)

    findings = lint_files(args.policy, args.envelope, args.receipt, now=args.now)
    if args.json:
        print(json.dumps(findings, indent=2))
    else:
        for line in format_lines(findings):
            print(line)
    return 1 if findings else 0


__all__ = [
    "MemoryPolicyReason",
    "Decision",
    "compute_inherited_floor",
    "validate_envelope",
    "validate_receipt",
    "evaluate_retention",
    "evaluate_retrieval",
    "validate_memory_view",
    "lint_files",
    "format_lines",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
