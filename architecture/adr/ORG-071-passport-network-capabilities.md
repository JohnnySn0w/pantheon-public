---
aliases: [ORG-071]
id: ORG-071
title: "Cross-seed passport — provider-neutral network capabilities: DoS bounds, quotas, timeouts, and resolution/status caching"
status: "Ratified-with-trim 2026-09-06 by the operator via TG 1784 (specialist-mediated live review). Verdict: ratify D1-D6 (the DoS-bounds taxonomy + STRICT resolution/status caching + timeouts-are-aborts + per-counterparty quotas + provider-neutral policy + abort semantics) BUT STRUCK the '## Build-time parameter table' section — those numeric values relocated verbatim into build ticket T-2857 (first-fire tuning belongs at build, not in a ratified ADR of a Stage-7 unbuilt surface). D1-D6 keep parameter NAMES as taxonomy; numeric values live in T-2857. HO-drafted 2026-08-10 as ORG-047 Split B (T-2812), executing the T-2803 triage disposition. Specifies the our-side network-hygiene bounds of the passport handshake — rate/DoS limits, timeouts, per-counterparty quotas, and DID-resolution/status-list caching — uniform across all scheme adapters. Not yet built."
date: 2026-08-10
build_disposition: needs-build
scoping_ticket: T-2812
# elements: — ORG-065 machine index (T-3476 ratifies-edge target for T-2856).
# ORG-071's ratified body carries D1-D6 (the DoS-bounds + caching + timeout +
# quota + provider-neutral + abort-semantics decisions); the build-time
# parameter table was RELOCATED to build ticket T-2857 per the operator's altitude
# ruling (TG 1784, 2026-09-06 ratify-with-trim), so only D1-D6 elementize
# here. Added by HO; no obligation content changed.
elements:
  - {id: E1, text: "D1: DoS bounds, fail-closed under load — connection taxonomy + tiered pre/post-auth budgets + amplification bounds; excess is rejected, never degraded"}
  - {id: E2, text: "D2: Resolution + status caching — STRICT freshness bounded by ORG-047 D5 rotation ceiling, failure-classed negative caching, singleflight, anti-poisoning"}
  - {id: E3, text: "D3: Timeouts everywhere; a timeout is an ABORT — named taxonomy (DNS-lookup, TCP-connect, TLS-handshake, per-fetch read, per-handshake round-trip, total handshake deadline)"}
  - {id: E4, text: "D4: Per-counterparty quotas + counterparty identity keys — phase-specific identity key (pre-verify vs post-verify) so 'per-counterparty' is unambiguous"}
  - {id: E5, text: "D5: Provider-neutral: uniform policy, not identical budgets — shared minimum policy centrally enforced; adapter-specific stricter bounds allowed; weighted cost accounting"}
  - {id: E6, text: "D6: ABORT semantics + observability — coarse error vocabulary (anti-oracle) + named audit events into ORG-070 (rate_limit_rejected, quota_exhausted, resolver_timeout, etc.)"}
---

# ORG-071 — Cross-seed passport: provider-neutral network capabilities

**Status:** Ratified-with-trim 2026-09-06 by the operator via TG 1784 (specialist-mediated review; T-2856). D1-D6 keep parameter NAMES as taxonomy; numeric values struck from the ADR and relocated to build ticket T-2857 (first-fire tuning). **Author of record:** Orchestrator (HO).
**Relationship:** referenced split of **[[ORG-047]]** (cross-seed passport). ORG-047 owns identity + handshake composition; this ADR owns the *network hygiene* enforced at our boundary regardless of the counterparty's provider or scheme. Filed per the **T-2803** triage (operator-named split #2, "provider-neutral network capabilities"; absorbs the DoS-bounds and cost/quota considerations); scoping ticket **T-2812**.
**Source of authority:** the T-2803 triage disposition + ORG-047 D5's short-DID-cache-TTL requirement (which this ADR reconciles against availability).
**Relates to:** [[ORG-047]] D1 (handshake), D5 (short cache TTL for rotation propagation); [[ORG-069]] (Split D — a capability grant carries a quota, enforced here); [[ORG-070]] (Split E — rate/quarantine events are audited there); [[ORG-047]] Split A (revocation freshness the caching bounds must not defeat).

## Why this ADR exists

The handshake and the identity model say nothing about *load*. A federation surface that will accept connections from other seeds is an internet-facing surface, and an internet-facing surface without rate limits, timeouts, and quotas is a denial-of-service target — and a resolution amplifier (every handshake fetches a DID document + a revocation status list). ORG-047 D5 also creates a live tension this ADR must resolve: it wants a **short** DID-document cache TTL so a key rotation propagates in minutes, but short TTLs mean more fetches — worse availability + more amplification. This ADR sets the our-side bounds and resolves that tension explicitly, uniformly across every scheme adapter so there is one rate-limiter, not one per adapter.

## Context — what is already fixed (do not design over it)

1. **The handshake is fail-closed (ORG-047 D1).** No partial-trust state; each precondition failure ABORTs. Network bounds extend that: a bound breach is an ABORT, never a degraded-but-accepted session.
2. **Rotation needs a short DID-doc cache TTL (ORG-047 D5).** Root rotation propagates only as fast as counterparties re-resolve; `did.json` is served with a short `Cache-Control`. This ADR's caching must not defeat that freshness.
3. **Grants carry scope (ORG-069 D2).** Authorization is least-privilege; quotas are the load-dimension of a grant and are enforced here.
4. **Events are audited (ORG-070).** Rate-limit trips, quota breaches, and quarantines emit evidence records; this ADR produces those events, Split E stores them.

## Decision

*(v2, 2026-08-11: expanded to close the Codex ORG-041 round-1 review — connection-taxonomy + tiered pre/post-auth budgets, an explicit STRICT stale-on-error policy, failure-classed negative caching, resolution-amplification bounds, ABORT/observability semantics, and a named parameter table. Record: notes/2026-08-11-org071-review-round1.md.)*

### D1 — DoS bounds, fail-closed under load (connection taxonomy + tiered budgets + amplification)

Bounds are enforced at three layers, all fail-closed (excess is **rejected, never degraded** — shed connections before weakening authentication; a rejection is an ABORT with a logged reason, distinct from an auth failure):

- **(a) Connection-level (pre-any-work):** max open TCP/TLS connections; TLS-handshake timeout; header-size limit; request-queue length + explicit backpressure; slowloris protection (body read/stream + idle timeouts); max concurrent handshakes global + per-source.
- **(b) Budgeted work tiers — metered against a budget keyed to the counterparty's *verification state*, because the heaviest work is pre-authorization:** a **pre-auth budget** (accept→Agent-Card→DID-resolution→status-fetch→crypto-verify→negotiation — the primary DoS target, tightest budget); an **authenticated-but-ungranted** budget; a **granted-counterparty** budget (the ORG-069 quota, D4); a **known-counterparty degraded/outage** budget; and an **unknown-source** budget **isolated** so unknown parties cannot starve known counterparties.
- **(c) Per-handshake resource bound:** max message size, max negotiation steps, max redirects.

**Outbound resolution-amplification bounds** (caching alone does NOT bound an attacker presenting many *distinct* DIDs/anchors): global outbound-resolution rate limit; per-source unresolved-DID rate limit; per-anchor-host resolution rate limit; max-distinct-DIDs and max-distinct-anchor-hosts per source/window; DNS-lookup limit; response-size limits (DID doc + status list, incl. decompressed); a per-anchor **circuit breaker** with audit + recovery-probe. *Justifies-via:* ORG-047 D1 fail-closed, extended to the load + amplification dimensions.

### D2 — Resolution + status caching: STRICT freshness + failure-classed negative caching

**Positive caching:** DID-doc and StatusList2021 fetches cached with a short positive TTL bounded by the ORG-047 D5 rotation-propagation ceiling; HTTP `Cache-Control` is an *input* clamped to the local max, never authority beyond it; cache only *validated* material, each entry bound to the expected DID/issuer/anchor (anti-poisoning); **singleflight/request-coalescing** so concurrent handshakes for one DID trigger a single fetch; jittered refresh against expiry stampedes.

**Stale-on-error policy — STRICT:** expired identity or status material is **never** used; a resolution/status failure at handshake time is a fail-closed ABORT. We accept the self-inflicted-outage blast radius (anchor unreachable ⇒ handshakes fail closed) because a stale-but-available anchor that misses a rotation/revocation is a *security* failure while a briefly-unavailable anchor is a *liveness* failure, and this arc is fail-closed by thesis (ORG-047 D1).

**Failure-classed negative caching** (a single negative behavior is unsafe — a transient outage cached as "failed" becomes a local denial window; an attacker-triggered failure keyed on DID/host could suppress legit handshakes):
- **Definitive-absent** (404/410, malformed doc, invalid signature/key-mismatch): cache negatively for a short class-TTL, scoped to the *DID/document identity* — the material is genuinely bad.
- **Transient-infra** (DNS/TLS failure, timeout, anchor-rate-limited-us, local-resolver failure): **advisory fetch-suppression only** (brief backoff), NOT handshake-denial state, scoped to the *anchor host + our source*, **never** to the counterparty; the per-anchor circuit breaker (D1) governs repeated transient failures.
- A negative entry never overrides a subsequent *successful* validated fetch.

*Justifies-via:* ORG-047 D5 (short positive TTL authoritative) + P1 (negative-cache classes + singleflight are structural anti-amplification/anti-self-denial).

### D3 — Timeouts everywhere; a timeout is an ABORT (named taxonomy)

Every network step carries a bounded timeout; a timeout is a fail-closed ABORT with a logged reason. Named categories: DNS-lookup, TCP-connect, TLS-handshake, per-fetch read (Agent Card / DID resolution / status-list), per-handshake round-trip, and a **total handshake deadline** (a wall-clock cap on the whole exchange, so a slow counterparty cannot walk the per-step timeouts indefinitely). Values are first-fire tuning surfaced to the operator at build time (relocated to build ticket T-2857 per ORG-071 ratify-with-trim, the operator TG 1784, 2026-09-06). *Justifies-via:* ORG-047 D1 fail-closed.

### D4 — Per-counterparty quotas + counterparty identity keys

A grant (ORG-069 D2) carries a quota (the post-auth load dimension). Because "per-counterparty" is ambiguous before verification, the ADR names the **identity key** per phase:
- **pre-auth key:** source transport identity (IP/subnet with IPv6-prefix + NAT/shared-egress handling) + the *claimed* DID — but a claimed DID may NOT consume that DID's *authenticated* quota before proof-of-possession (else an attacker exhausts a victim's quota by claiming their DID); pre-auth load counts only against the source + claimed-DID's *pre-auth* budget.
- **post-auth key:** the verified DID.
- A failed handshake counts against the *source* and *anchor host* (amplification accounting), not the claimed counterparty's authenticated quota.
Quota exhaustion is a soft-deny (throttle) + audited event, distinct from de-authorization. *Justifies-via:* ORG-069 D2.

### D5 — Provider-neutral: uniform policy, not identical budgets

Common policy is enforced centrally, before/independent of scheme verification; no adapter bypasses the central budget. "Uniform" = shared *minimum* policy, not identical budgets: adapter-specific *stricter* bounds are allowed where a scheme's cost profile warrants (VC verify/parse costs more than an API-key check), and expensive operations are normalized via **weighted cost accounting** (a VC verify or status-list decode consumes more budget units than a token check). *Justifies-via:* P1 (one enforcement layer over N) + ORG-047 provider-neutrality.

### D6 — ABORT semantics + observability

A bound-breach ABORT uses a **coarse** error vocabulary deliberately (rate-limit/quota/timeout ABORTs are not distinguishable from auth failure to the peer, avoiding an oracle); optional `Retry-After` only where it aids a legitimate kin without leaking budget state. Every outcome emits an audit event to ORG-070 with a named type: `rate_limit_rejected`, `quota_exhausted`, `resolver_timeout`, `status_fetch_failed`, `negative_cache_hit`, `stale_denied`, `circuit_breaker_open`, `unknown_did_budget_exceeded` — plus the admit/abort + failing-precondition already in ORG-047 D1. *Justifies-via:* ORG-070 (audit sink) + P1 (coarse errors are a structural anti-oracle).

## Alternatives considered

- **Per-adapter rate/timeout logic.** Rejected — N enforcement points drift and leave gaps (the same reasoning as ORG-069 D5's single-broker choice).
- **Long DID-doc cache TTL for availability.** Rejected — it defeats ORG-047 D5's rotation-propagation guarantee; a missed rotation is a security failure that outweighs the liveness cost of a short TTL (D2).
- **No negative caching.** Rejected — lets a hostile counterparty force repeated upstream resolution fetches (an amplification vector).
- **Bounded stale-if-error** (a grace window for previously-validated counterparties, never after revocation evidence). **NOT adopted** — recorded as the knob to revisit if ORG-072 (Split C) anchor-DR work shows the STRICT-outage blast radius (D2) is unacceptable. Deliberately not the default: it trades a security property for liveness.
- **Flat request counts instead of weighted cost accounting.** Rejected — lets cheap-path floods and expensive-path floods share one budget the expensive path exhausts first (D5).

## Consequences

- **Positive:** the federation surface is DoS-resistant, non-amplifying, and fail-closed under load; freshness of the anchor is preserved; quotas make load a revocable per-counterparty dimension; one uniform enforcement layer.
- **Cost / risk:** rate limits + quotas are tuning surface (too tight throttles legitimate kin, too loose invites abuse — surfaced as a value-decision adjacent to Split A's TTLs); the short cache TTL is a deliberate availability cost paid for freshness.

## DECISIONS (justification)

- **DoS bounds: connection-layer + tiered pre/post-auth budgets + amplification (D1)** — justifies-via ORG-047 D1 fail-closed, extended to load; pre-auth budget isolation because the heaviest work precedes authorization.
- **STRICT stale-on-error + failure-classed negative caching (D2)** — justifies-via ORG-047 D5 (short positive TTL authoritative) + P1 (negative-cache classes + singleflight are structural anti-amplification/anti-self-denial). STRICT chosen over bounded-stale-if-error because the arc is fail-closed by thesis; the outage blast radius is the accepted cost.
- **Named timeout taxonomy incl. total-handshake deadline (D3)** — justifies-via ORG-047 D1 fail-closed.
- **Quotas + phase-specific counterparty identity keys (D4)** — justifies-via ORG-069 D2; a claimed DID cannot consume its authenticated quota pre-proof-of-possession.
- **Uniform policy but not identical budgets, weighted cost accounting (D5)** — justifies-via P1 (one layer over N) + ORG-047 provider-neutrality.
- **Coarse ABORT vocabulary (anti-oracle) + named ORG-070 audit events (D6)** — justifies-via ORG-070 + P1.
- **All bounds are named build-time parameters (parameter table)** — justifies-via P4 (a builder wires the named surface; values are deployment defaults, tuning is an operator value-decision).

## Provenance / review trail

- 2026-08-10 — HO-drafted as ORG-047 Split B per the T-2803 triage (disposition: SPLIT; operator-named split-candidate #2). Absorbs the DoS-bounds (#6) and cost/quota (#14) considerations.
- 2026-08-11 — Codex ORG-041 review **round 1**: not-clean (directionally sound but too much security-critical network behavior left for builders to infer). HO integrated into **v2** (this revision): connection taxonomy + tiered pre/post-auth budgets (D1), STRICT stale-on-error + failure-classed negative caching + singleflight (D2), named timeout taxonomy (D3), phase-specific counterparty identity keys (D4), uniform-not-identical + weighted cost (D5), coarse-ABORT + named audit events (D6), and a build-time parameter table. Review record: `notes/2026-08-11-org071-review-round1.md`.
- RATIFIED 2026-09-06 (the operator, per architecture/adr/README index + status frontmatter). On ratify: file the build sub-ticket set (rate-limiter, timeout policy, resolution cache with negative-caching + singleflight + jitter, quota enforcer, circuit breaker) under scoping ticket T-2812, per the ADR-scoping rule.
