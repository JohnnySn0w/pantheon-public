---
id: ORG-084
title: Executable runtime contract projection
status: "Proposed refresh 2026-10-17; pending the operator human review through T-4681. Historical provenance: the prior revision was agent-Adopted 2026-09-07 by Consultant during the T-3388 bounded autonomy experiment."
date: 2026-08-31
hitl: Autonomous
build_disposition: needs-build
scoping_ticket: T-2956
appetite: 5
---

# ORG-084 — Executable runtime contract projection

## Intent

Make Pantheon's end-to-end runtime architecture traversable and falsifiable without creating another source of truth. Human-facing architecture remains explanatory; a derived runtime trace lets agents and CI reject missing dependencies, ambiguous ownership, incomplete delivery claims, unreachable rollback, stale evidence, and lifecycle traces that skip a required boundary.

This ADR responds to T-2953's repeated component-green/full-path-red failures. It does not declare the fleet healthy, migrated, installed, or observed. It defines how existing authorities may be composed into a non-authoritative runtime trace.

## Decision

Adopt an orthogonal, digest-bound runtime-trace profile that composes the architecture atlas and existing authorities. The projection is derived and read-only. It never writes back to an ADR, component contract, desired-state manifest, registry, evidence record, or observed-state store.

A projection refuses conflicting authority; document order and precedence never choose a winner. Generated graphs and operator sheets are disposable views. They may explain or validate a trace, but cannot become authority or actuate a change.

## Authority partition

- [[ORG-080]] alone owns component-contract fields, including component identity, dependencies, bindings, lifecycle operations, receipts, and rollback.
- [[ORG-081]] and registry/systems.yaml own contract-adoption and per-leg coverage data. They do not own installed or runtime truth.
- [[ORG-068]] owns lifecycle obligations and the coverage predicate.
- [[ORG-083]] owns evidence vocabulary and evidence-policy semantics.
- Desired-state manifests remain authoritative only for fields admitted by their validators.
- Identity, capability, continuity, isolation, scheduling, completeness, and broker-boundary authorities remain with [[ORG-050]], [[ORG-052]], [[ORG-057]], [[ORG-063]], [[ORG-064]], [[ORG-079]], and [[ORG-082]].
- ORG-084 owns only the composition semantics needed to trace those authorities across fact planes, delivery, applicability, failure, and rollback.

The 2026-10-17 ORG-080 buildout-charter amendment is pending the operator review. This ADR does not treat that amendment as ratified.

## Runtime-trace semantics and invariants

### Fact planes

Each projected fact is atomic and occupies exactly one plane:

- desired;
- declared contract;
- landed mechanism;
- installed or adopted;
- runtime observed;
- retained evidence;
- generated view;
- open work.

A fact records one subject, one field, one source, one value digest, applicable time and freshness, and limitations. Facts in one plane never overwrite or imply facts in another. Code existence does not prove installation; installation does not prove current runtime state; observation does not rewrite desired state; a ticket is not implementation; evidence is not intent; and generated output is not authority.

### Identity and composition

Every trace identity resolves to the atlas identity authority or carries an explicit typed deferral owned by open work. A deferred identity blocks executable use and atlas emission. Runtime identifiers never masquerade as atlas identifiers.

Every source reference is pinned to immutable source bytes and the authority field it imports. Projection-specific freshness and limitation metadata may qualify use of that source; it cannot alter the source's authority.

### Delivery

Delivery milestones are distinct:

- fetched;
- durably queued;
- submitted;
- turn started;
- consumed or acknowledged;
- externally responded;
- asserted effect proven.

No milestone implies a later one. A wake proves an attempted signal, not consumption. Maildir movement proves handler completion, not response or effect. Turn start proves neither completion nor external response. An end-to-end effect claim requires positive effect evidence.

### Applicability and rollback

Every relevant lifecycle leg and cross-layer boundary has an explicit authority-backed disposition: applicable, waived, or not applicable. Silence is invalid. Waivers require live authority and deterministic expiry or trigger. Not-applicable requires an authority and a reason; it cannot mean merely unimplemented.

Every mutating failure state must reach either a receipt-anchored inverse or explicitly authorized compensation. If neither is available, mutation is refused before it begins. Rollback removes only receipt-proven owned state and preserves user-owned state.

### Trace completeness and drift

A trace is complete only when:

1. every referenced identity, object, source, and authority resolves at its frozen revision;
2. every applicable ORG-068 lifecycle leg and every relevant identity, security, credential, receiver, delivery, health, continuity, and rollback obligation appears or has an authority-backed disposition;
3. each step's preconditions are established before it runs and its postconditions are carried forward;
4. every mutating failure exit has a reachable owned inverse or authorized compensation;
5. receiver handoff proves prior ownership absent before new ownership begins;
6. claimed delivery preserves each milestone and reaches asserted effect evidence;
7. source, schema, graph, request, evidence, and generated-artifact digests are current.

Changed source pins, authority digests, desired bytes, installed artifact digests, receiver ownership, evidence freshness, applicability, or use of open work as runtime truth cause refusal before mutation or generation.

An operator sheet may be generated only from an accepted trace. It cannot add, reorder, or omit steps, has no write-back surface, and refuses use when its inputs drift.

## Current implementation and authority facts

registry/systems.yaml now exists as ORG-081's authoritative git-tracked contract-adoption and per-leg coverage plane. All 17 component rows currently declare contract_adoption: not_adopted. The health-plane row is an external plane and has no contract-adoption field. legacy_coverage records migrated historical coverage; it is neither ORG-080 adoption nor installed or runtime truth.

The registry validator, renderer, ADR-link lint, rendered coverage view, and CI drift check exist. Their existence proves those mechanisms, not component adoption or fleet runtime state.

ORG-083 is ratified evidence-policy authority. Its versioned schema, strict validator and canonicalizer, bounded normalized-input adapter, and non-enforcing inventory-only pilot have landed. Fleet collection, selected thresholds or ratchets, CI enforcement, policy activation, and repository or fleet rollout remain unbuilt. ORG-084 may reference conforming retained evidence; it neither creates evidence authority nor treats machinery existence as proof that a runtime edge was exercised.

No registry row, source artifact, ticket, retained receipt, or generated output by itself establishes current installed or runtime truth.

## Relocated historical build reference

The prior revision's closed declarations, exact source-pin shape, serialized contract, cardinalities, state-machine detail, and complete frozen inventory fixture are preserved byte-for-byte in [the ORG-084 historical build-tier relocation note](../../notes/2026-10-17-org084-build-tier-contract-relocation.md).

That note is non-authoritative historical build input. It cannot override this ADR, establish adoption or runtime state, or independently constrain an implementation. Any future normative interface or state specification requires its own RFC review and the operator ratification.

## Security boundary

- Identity comes from the desired-state manifest, OS or bus-attested caller, and the applicable identity contract, never caller-supplied prose.
- Credentials remain references; the projection never copies secret values.
- Planners return data; typed executors alone actuate operations.
- Cross-UID, host-state, D-Bus, receiver-ownership, Telegram, mailbox, health, and continuity crossings are explicit trace objects.
- Single-poller ownership is structural: outbound send surfaces do not poll; receiver handoff proves old ownership absent before new ownership starts; sustained HTTP 409 is split-brain evidence.
- Runtime observations and health evidence are read-only inputs. They cannot grant capabilities or rewrite desired state.
- Generated artifacts carry no authority and expose no mutation surface.

## Alternatives

1. A parallel authoritative architecture graph is rejected because it duplicates existing authorities and makes conflicts irresolvable.
2. Prose or diagrams alone are rejected because they cannot be traversed or falsified mechanically.
3. Inferring architecture from tests or coverage is rejected because observation cannot define intent, ownership, or authority.
4. Overloading the atlas is rejected because its navigation views do not own runtime preconditions, acknowledgements, failures, receipts, or rollback.
5. An atlas-composed orthogonal runtime profile is selected because it reuses existing identities and source pins while owning only trace semantics.

## Build and rollout boundary

This refresh authorizes no schema, validator, canonicalizer, generator, collector, operator sheet, evidence adapter, CI gate, registry mutation, production probe, service change, or deployment.

T-3382 remains blocked on the operator's disposition of T-4681. A later build may derive a current normative RFC if implementation needs interface or state-machine detail. That is a separate reviewed and ratified act; the historical relocation note is not its specification.

## Acceptance and falsifiers

An implementation does not conform if any of these survives validation:

- duplicated authority, copied authoritative values, or generated write-back;
- unresolved, duplicate, or wrong-type identities or references;
- prohibited dependency cycles or unbounded recovery loops;
- conflation of desired, declared, landed, installed, observed, evidence, generated, or open-work planes;
- missing or expired applicability;
- missing identity, security, capability, wake-to-turn, receiver, delivery, health, continuity, or rollback boundaries;
- split receiver ownership or sustained Telegram 409;
- wake, queue acknowledgement, or turn start treated as response or effect;
- mutation without a reachable owned inverse or authorized compensation;
- a supposedly complete trace that omits an applicable lifecycle or boundary obligation;
- line, branch, function, or changed-code coverage treated as edge-effect proof;
- a pin, generated artifact, test, ticket, or code file treated as current runtime truth;
- generation or execution from stale source, request, graph, schema, or evidence digests.

## Appetite

**5/10.** This bounded refresh is worth doing now because the operator needs a concise and current decision surface before the gated build can move. The ceiling is this ADR, one historical reference note, and index parity. No RFC, schema, implementation, registry mutation, rollout, or live probe belongs here.

## Riskiest assumption

The riskiest assumption is that the altitude split can remove the closed machine contract without losing a decision-level invariant. Test it cheaply with an explicit old-to-new invariant map, byte-exact preservation checks for relocated detail, and an independent antagonistic review of the exact candidate.

## Re-evaluate when

- the ORG-080, ORG-081, ORG-068, or ORG-083 authority partition changes;
- a production-shaped pilot finds an unrepresentable fact, delivery, applicability, boundary, or rollback invariant;
- runtime evidence requires separately owned retention or freshness authority;
- a future normative RFC supersedes the historical reference;
- T-3382 lands behavior that falsifies a decision-level assumption in this ADR.

## DECISIONS

- **Intent:** make runtime architecture traversable without adding a second authority. **Call:** use a digest-bound, atlas-composed, derived read-only trace with explicit field authority and no write-back. **justified-via:** P1, P3, P4, D10.
- **Intent:** stop component-green evidence from masquerading as full-path success. **Call:** separate fact planes and delivery milestones, requiring positive effect evidence for an end-to-end effect claim. **justified-via:** P1, P4, D5, D11.
- **Intent:** make lifecycle and recovery mechanically decidable. **Call:** require authority-backed applicability, explicit boundary coverage, and a receipt-anchored inverse or authorized compensation for every mutating failure. **justified-via:** P1, P4, D6.
- **Intent:** preserve technical history without letting obsolete mechanics masquerade as current authority. **Call:** move exact build-tier detail to one non-authoritative historical note; any future normative specification requires its own RFC. **justified-via:** P3, P4, P5, D2, D7.
- **Intent:** preserve honest lifecycle provenance. **Call:** retain the 2026-09-07 agent adoption as a historical fact and treat these refreshed bytes as Proposed until the operator acts through T-4681. **justified-via:** P4, P5, D2, D3, D7.
