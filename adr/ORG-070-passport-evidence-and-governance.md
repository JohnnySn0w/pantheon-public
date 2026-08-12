---
aliases: [ORG-070]
id: ORG-070
title: "Cross-seed passport — evidence & governance: federation audit trail, dependency lineage into ORG-065, and the approval edges"
status: "Proposed — DRAFT awaiting the operator ratification. HO-drafted 2026-08-10 as ORG-047 Split E (T-2815), executing the T-2803 triage disposition. Specifies the federation evidence trail, the passport arc's dependency lineage into the ORG-065 graph, and the human-in-the-loop approval edges (allowlist enrollment, adapter exposure, rung-3 gating). Not yet built; the evidence trail lands on the existing audit substrate, not a parallel log."
date: 2026-08-10
build_disposition: needs-build
scoping_ticket: T-2815
---

# ORG-070 — Cross-seed passport: evidence & governance

**Status:** Proposed; DRAFT awaiting the operator ratification. **Author of record:** Orchestrator (HO).
**Relationship:** referenced split of **[[ORG-047]]** (cross-seed passport). ORG-047 owns identity + handshake; this ADR owns the *evidence* every federation event leaves and the *governance* by which counterparties, adapters, and exposure are approved. Filed per the **T-2803** triage (operator-named split #5, "evidence/dependency governance"; also absorbs the observability + governance-edges considerations); scoping ticket **T-2815**.
**Source of authority:** ORG-047 D1 (each handshake ABORT already logs its failing precondition) + the operator's ORG-047 walk disposition (2026-08-10) directing the narrower concerns into referenced ADRs.
**Relates to:** [[ORG-047]] D1/D4/D5; [[ORG-065]] (first-class dependency graph — ADR→obligation→ticket→evidence lineage, the substrate this feeds); [[ORG-069]] (Split D authorization — its authz decisions are audited *here*); [[ORG-047]] Split A/B/C (their lifecycle + network + recovery events are logged here); the standing ledger/audit-chain discipline (P4).

## Why this ADR exists

A federation layer that cannot say *what happened* is unauditable, and an unauditable trust boundary is one you cannot reason about after an incident. ORG-047 D1 already logs each handshake ABORT, and Split D (ORG-069) requires every authorization decision be audited — but those are scattered obligations, not a coherent evidence model. This ADR unifies them: one federation evidence trail, on the existing audit substrate, feeding the ORG-065 dependency graph so that "is this obligation actually implemented and exercised?" is a query, not a manual audit. It also names the governance edges — the human approvals that gate who enters the allowlist, what adapters are exposed, and when rung-3 opens — so those approvals are themselves audited events, not tribal knowledge.

## Context — what is already fixed (do not design over it)

1. **Handshake ABORTs already log (ORG-047 D1).** Each ABORT logs the failing precondition. This ADR generalizes that from ABORT-only to *every* federation outcome.
2. **Authz decisions must be audited (ORG-069 D5).** Split D already requires every authorization grant/deny + invocation be logged; this ADR is where that log is specified.
3. **The audit substrate exists.** Pantheon's ledger + the ORG-065 dependency graph are the audit-chain substrate (P4 traceability). Federation evidence lands there, not in a bespoke parallel store.
4. **Rung-3 exposure is operator-gated (ORG-047 status).** The governance edge for external-node exposure is already reserved to the operator; this ADR records it as an audited governance decision, it does not re-open the gate.
5. **Content stays untrusted (ORG-047 D4 / ORG-069 D4).** The evidence trail records the *fact and shape* of an exchange, never elevates the trust of its payload.

## Decision

### D1 — A federation evidence trail: every event is an append-only audit record

Every federation event emits a durable, append-only audit record: handshake outcomes (admit / abort + failing precondition), authorization decisions (grant / deny + the identity/action/scope tuple, from ORG-069 D5), credential-lifecycle events (mint/rotate/revoke/expire, from Split A), and de-authorizations. Each record carries at minimum: timestamp, counterparty DID, scheme used, event type, outcome, and — for actions — the tool/action id + scope. The trail is queryable along two axes the audit needs: **by counterparty** ("everything counterparty X has done/attempted") and **by grant** ("every use of this capability grant"). *Justifies-via:* P4 (traceability) + ORG-047 D1 / ORG-069 D5, unified.

### D2 — Dependency lineage into the ORG-065 graph: a federation event is closure-evidence

The passport arc's ADRs (ORG-047 + Splits A–E), their obligations, the build tickets (T-2016 / T-2018 / the split scoping tickets), and this evidence trail are **nodes in the ORG-065 first-class dependency graph** with typed edges: ADR → obligation → ticket → evidence. A federation event's audit record (D1) is the **evidence node** that closes a build obligation — e.g. "the mTLS adapter handshake succeeded against a real counterparty" is the evidence that closes the mTLS-adapter obligation. This lets ORG-065 closure-validation confirm every ratified passport obligation has both implementation *and* exercised evidence, rather than assuming a merged PR equals a working federation path. *Justifies-via:* ORG-065 (the lineage substrate) + P4.

### D3 — Governance / approval edges: the human-in-the-loop decisions, each an audited event

Three approval edges gate the federation, each a HITL decision recorded as a governance event in the evidence trail:
- **Allowlist enrollment** — adding a counterparty to the TG-869 allowlist (the D3/Split-D "route through the operator" first-contact bootstrap): who approved, the counterparty DID/card, the initial capability grant. Deliberately manual (no auto-enrollment).
- **Adapter / tool exposure** — exposing a new scheme adapter or making a tool invocable by counterparties: requires a provenance sign-off (the vetted/pinned/integrity-checked precondition from ORG-069 D5) recorded at approval time.
- **Rung-3 exposure** — opening an external (non-kin, or first-external-node) surface: **operator-gated** (per ORG-047 status), recorded as the highest-tier governance event.

*Justifies-via:* the standing access-control floor (approvals are the operator's, never a passport-holder's — ORG-069 D3) + P4 (each approval audited).

### D4 — Retention + minimization: log the decision, never the untrusted payload

The evidence trail retains what forensics needs — the (identity, event, scope, outcome) shape — and **does not log the untrusted message payload** (ORG-047 D4 / ORG-069 D4: the content is untrusted and may be sensitive; recording it would both violate minimization and risk treating logged content as trusted on replay). Retention is bounded by an explicit policy (a value-decision adjacent to Split A's TTLs; surfaced, not fixed here). *Justifies-via:* ORG-047 D3 minimization fold + the content-untrusted invariant.

### D5 — One audit substrate, not a parallel federation log

The evidence trail is written to the existing ledger / ORG-065 audit substrate, not a bespoke federation-only store. One substrate keeps federation events correlatable with internal events (a passport-authenticated action and the host action it triggered share one audit chain — matching ORG-069's broker convergence and ORG-048's audited-repair pattern). *Justifies-via:* P1 (one substrate over N) + P4.

## Normative additions (v2)

*(v2, 2026-08-11: closes the Codex ORG-041 round-1 review — the missing normative surfaces the evidence/governance model requires; record: notes/2026-08-11-org070-review-round1.md.)*

- **E1 (D1/D5) — Audit integrity + write/read authority.** The federation-evidence path on the substrate is append-only-ENFORCED (writers append, never mutate/delete), tamper-evident (records chained/signed/sequenced so missing ranges + write gaps are detectable), and write authority is NARROWER than read/query authority — a compromised seed/seat cannot silently rewrite or truncate its own federation history. (Without this, D1's central claim is unsound — the trail could become attacker-controlled incident fiction.)
- **E2 (D1) — Read access control on the evidence trail.** Read/query access is governed (the trail holds counterparty DIDs, grants, scopes, denials, governance approvals — sensitive operational/identity metadata); governance-approval records may carry stricter visibility than ordinary event records; counterparty identifiers + capability grants are confidential audit metadata; access to the trail is itself audited.
- **E3 (D4) — Bounded non-content forensic envelope.** Beyond "never log the untrusted payload": record a bounded NON-content envelope — payload content-hash (domain-separated/keyed where correlation risk warrants), byte length, declared schema/version/content-type, envelope/request id/nonce, validation result + parser-failure class, and a redaction marker — so "the same malicious payload attempted across three counterparties" is provable without storing content.
- **E4 (D3) — Revocation is a first-class audited governance edge.** A governance edge is approval AND its reversal — one edge type with lifecycle states (approved → superseded → revoked → expired → emergency-disabled): allowlist removal/suspension, grant narrowing, adapter/tool unexposure, rung-3 rollback, emergency disable/quarantine approval — each recording who approved, when effective, and what prior approval/grant it supersedes.
- **E5 (D2) — Interim closure semantics before ORG-065.** Until ORG-065 exists, an obligation's closure references immutable ledger record IDs directly; no obligation is fully closed without either an ORG-065 evidence edge OR a documented pending-edge placeholder tied to ledger evidence; the backfill is validation work, not best-effort cleanup.
- **E6 (D1) — Event-taxonomy completeness.** The evidence classes include, beyond handshake/authz/lifecycle/de-auth: scheme-downgrade attempt, unsupported/deprecated-scheme attempt, malformed-card / DID-resolution failure, replay/nonce/freshness failure, rate/quota trip (Split B), quarantine entry/exit (Split C), recovery-mode invocation, counterparty status change, and evidence-write-failure / degraded-audit-mode.
- **E7 (D5) — Correlation IDs.** Federation records carry correlation IDs linking handshake → grant → invocation → local host action → outcome, so "one substrate" yields usable causal traceability, not merely shared storage.
- **E8 (D1) — Stable grant identity.** "By-grant" queryability requires grants to have durable IDs + version/revocation lineage (not just scope strings), so historical grant use is unambiguous after grant edits.
- **E9 (D3/D4) — Counterparty card minimization.** Log the DID + card fingerprint/version + issuer/provenance metadata + approval-time normalized claims — not necessarily the full card (reconciles D3's "DID/card" with D4 minimization).
- **E10 (D1) — Audit-write-failure behavior.** If evidence emission fails, the action fails closed (or enters a named break-glass/degraded mode with its own audited marker); "allowed but not audited" is forbidden for authorization decisions except under an explicit break-glass mode.

## Alternatives considered

- **A bespoke federation-only audit log.** Rejected — it forks the audit chain, making federation events un-correlatable with the internal actions they cause (D5).
- **Log full payloads for "completeness."** Rejected — violates minimization and re-introduces the content-trust hazard on replay (D4); the *shape* of an exchange is the evidence, not its untrusted content.
- **Governance-by-convention (approvals as tribal knowledge).** Rejected — un-auditable; the whole point is that who-approved-what is a recorded event (D3).

## Consequences

- **Positive:** the federation is auditable by counterparty and by grant; obligations are closure-validated with exercised evidence via ORG-065, not assumed; approvals are recorded, not tribal; one correlatable audit chain.
- **Cost / risk:** every event emitting an audit record is real write volume + a retention policy to set (surfaced value-decision); the ORG-065 integration presupposes ORG-065 is built (a dependency, not a blocker — until then the trail lands on the ledger and back-fills the graph edges when ORG-065 lands).

## DECISIONS (justification)

- **Evidence trail (D1)** — justifies-via P4 (traceability) + the unification of ORG-047 D1 / ORG-069 D5.
- **ORG-065 lineage (D2)** — justifies-via ORG-065 + P4 (evidence closes obligations, not merged-PRs).
- **Governance edges (D3)** — justifies-via the standing access-control floor + P4.
- **Minimization (D4)** — justifies-via ORG-047 D3 + the content-untrusted invariant.
- **One substrate (D5)** — justifies-via P1 (one substrate over N) + P4 (correlatable chain).
- **Normative additions E1–E10 (v2)** — justifies-via P1/P4 (make the evidence trail tamper-evident + access-controlled + causally-correlatable + minimization-preserving, with revocation, interim-closure, and write-failure behavior explicit, so D1–D5 are enforceable-by-construction not builder-invented); added per the Codex ORG-041 round-1 review.

## Provenance / review trail

- 2026-08-10 — HO-drafted as ORG-047 Split E per the T-2803 triage (disposition: SPLIT; operator-named split-candidate #5). Absorbs the evidence (#8), observability-depth (#11), and governance-edges (#13) considerations.
- 2026-08-11 — Codex ORG-041 review **round 1**: not-clean (model sound, but missing normative surfaces: audit tamper-evidence + write/read authority, evidence-trail read access-control, interim-closure-before-ORG-065, revocation-as-governance-edge, bounded non-content forensic envelope, event-taxonomy completeness). HO integrated into **v2**: the **Normative additions** section (E1–E10). Record: `notes/2026-08-11-org070-review-round1.md`.
- PENDING — Codex ORG-041 **round 2** on v2, then the operator ratification via a ratify ticket. On ratify: file the build sub-ticket set (evidence-emit hooks, ORG-065 edge integration, governance-approval recording, retention policy) under scoping ticket T-2815, per the ADR-scoping rule.
