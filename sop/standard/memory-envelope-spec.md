# Memory envelope trust and adjudication spec (normative)

**Deciding ADR:** [ORG-058](../../architecture/adr/ORG-058-exocortex-memory-and-learning-lifecycle.md) §4-§6. The ADR prevails on intent; this spec is the normative schema authority.
**Author / steward:** archivist (head_memory_orchestrator). **D3 reviewer:** Consultant (the 2026-08-30 trust-floor amendment).
**Provenance:** relocated verbatim from ORG-058 §4-§6 by T-6052 (T-4730 shape pass; orchestrator ruling on T-4747, steward approval 2026-11-03). operator-ratified text (TG 665) plus Consultant's D3 amendment; wording changes go through a separate amendment. Review trail: [`notes/2026-10-08-org-058-review-history.md`](../../notes/2026-10-08-org-058-review-history.md).

## §1 Trust-floor derivation

Every durable or derived memory also exposes an integrity/trust derivation
under a named `trust_policy_ref` bound to an immutable policy revision and
content digest (`trust_policy_digest`). A moving alias is not a policy identity. Each source carries
its source revision, `source_digest`, integrity label, and role in the
derivation. Each
independently usable claim carries the evidence references that contributed
to it, its immutable `inherited_trust_floor`, its
`effective_trust_floor`, and any `adjudication_receipt_ref`. The envelope
exposes the meet of its claim floors as its conservative whole-item floor and
retains claim-to-source lineage sufficient to recompute every inherited
floor.

Ordinary derivation computes `inherited_trust_floor` as the greatest lower
bound (meet) of every evidence item that contributed content, selection,
ranking, conflict resolution, or inference to that claim. A scalar minimum is
valid only when the named policy explicitly defines a total order. Unknown
policy, missing integrity metadata, incomparable labels without a defined
meet, or an absent meet resolves to the policy's named conservative state or
refuses the derivation. The system never invents an order, silently drops a
contributor, or substitutes model confidence.

Integrity/trust, epistemic confidence, privacy and retrieval permission, and
action authority are separate dimensions. Observation kind, repetition,
model confidence, consolidation, authenticated speaker identity, a digest, or
a high trust floor does not by itself change another dimension or grant
retrieval or action authority.

## §2 MemoryView contract

A `MemoryView` returns, for every item or independently usable claim, its
source references, `trust_policy_ref`, immutable
`inherited_trust_floor`, current `effective_trust_floor`, applicable
adjudication receipt references, contradiction notice, and a
purpose-derived `use_disposition`. Retrieval policy may omit or refuse an
item whose effective floor does not satisfy the named purpose, or return it as
`corroboration_required` or `quarantined`; it may not hide the floor from a
downstream consumer that can use the content.

Bridge-facing use enforces the disposition. Content below the purpose's floor
may be quoted or summarized only within the allowed disposition, with lineage
and disagreement preserved; it cannot be the sole support for an unqualified
factual assertion, identity-canon change, policy change, action proposal, or
external effect. No integrity level, including the highest one, is action
authority.

## §3 Consolidation and adjudication receipt

Consolidation is an integrity-preserving transformation. It may reorganize,
summarize, or derive claims, but it does not raise an
`inherited_trust_floor`. Any `effective_trust_floor` that differs from the
inherited floor requires a separately authorized adjudication permitted by
the immutable named-policy revision. Purpose policy may impose a more
restrictive `use_disposition` without rewriting either floor.

The adjudication receipt binds `receipt_id`, `adjudication_id`, exact memory
and version, claim ids, proposer, adjudicator, independently valid
`authority_ref`, trust-policy reference, immutable revision and digest,
bound `input_evidence_bindings` entries containing evidence reference,
source revision, and digest, derived-payload digest, inherited and prior
effective floors,
resulting effective floor, purpose scope, rationale, decision, issue and
expiry times, supersession reference, and receipt digest. The receipt points
to adjudication authority; it is not itself authority. The consolidator or
proposer may not self-adjudicate unless the named policy explicitly permits
that actor, transformation, claim set, and scope. Missing, invalid, expired,
revoked, superseded, wrong-version, wrong-evidence, wrong-purpose, or tampered
receipts leave the effective floor at the inherited floor or cause refusal.
Parallel evidence-reference and evidence-digest arrays are invalid because
order or length ambiguity could rebind evidence. `receipt_digest` is the
digest of canonical receipt bytes excluding the `receipt_digest` field, under
a named schema version and canonicalization rule; a detached envelope digest
with the same exact binding is an acceptable equivalent. The digest is never
defined self-referentially.
Adjudication never rewrites source labels, source records, contributing
lineage, or the immutable inherited floor.
