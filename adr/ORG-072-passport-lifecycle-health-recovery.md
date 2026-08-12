---
aliases: [ORG-072]
id: ORG-072
title: "Cross-seed passport — lifecycle health & recovery: handshake-failure handling, session quarantine, seat/seed recovery, and DID-host DR"
status: "Proposed — DRAFT awaiting the operator ratification. HO-drafted 2026-08-10 as ORG-047 Split C (T-2813), executing the T-2803 triage disposition. Specifies how the federation surface fails safely and recovers — failure handling, mid-session quarantine, seat/seed recovery, DID-host disaster-recovery, and health signals. Not yet built."
date: 2026-08-10
build_disposition: needs-build
scoping_ticket: T-2813
---

# ORG-072 — Cross-seed passport: lifecycle health & recovery

**Status:** Proposed; DRAFT awaiting the operator ratification. **Author of record:** Orchestrator (HO).
**Relationship:** referenced split of **[[ORG-047]]** (cross-seed passport). ORG-047 owns identity + handshake; this ADR owns *what happens when things go wrong* — failed handshakes, a misbehaving live counterparty, a compromised seat/seed, and an unreachable anchor. Filed per the **T-2803** triage (operator-named split #3, "lifecycle health/recovery"; absorbs the failure/quarantine/recovery and DID-host-DR considerations); scoping ticket **T-2813**.
**Source of authority:** the T-2803 triage disposition + ORG-047 D5's seed-root rotation-recovery (which this references, not duplicates).
**Relates to:** [[ORG-047]] D1 (handshake ABORT), D5 (seed-root recovery, seat-cred revocation); [[ORG-047]] Split A (credential lifecycle — revoke/re-mint mechanics this invokes); [[ORG-070]] (Split E — quarantine + recovery events are audited); [[ORG-071]] (Split B — rate/quota trips that feed quarantine); the fleet health-monitoring substrate; [[ORG-053]] (universal system-lifecycle contract — the install/monitor/remove framing this instantiates for the federation surface).

## Why this ADR exists

ORG-047 specifies the *happy path* (a clean mutually-authenticated handshake) and the *root-compromise* path (D5 rotation-recovery), but the space between is unspecified: a handshake that keeps failing, a counterparty that authenticates cleanly then misbehaves mid-session, a single seat whose credential is compromised, and — the sharpest gap — an **anchor that is unreachable**. `did:web:snowis.me` is a single point of verification for the entire federation: if the apex is down, *no* passport verifies. This ADR names the failure-handling, quarantine, recovery, and DR so the federation degrades safely rather than unpredictably.

## Context — what is already fixed (do not design over it)

1. **Handshakes fail-closed (ORG-047 D1).** A failure ABORTs with a logged precondition; there is no partial-trust state.
2. **Seed-root recovery is specified (ORG-047 D5).** Root compromise → rotate offline, publish in `did.json`, revoke delegated keys; propagation bounded by cache TTL. This ADR references it, does not redo it.
3. **Seat credentials are short-lived + revocable (ORG-047 D5 / Split A).** Expiry-by-default + a deny-path for sub-TTL revocation. Seat recovery invokes those mechanics.
4. **De-authorization is a governance decision (ORG-069 D3 / ORG-070 D3).** Permanent allowlist removal is the operator's call; this ADR's quarantine is the *automatic, reversible* precursor, not a substitute.
5. **The anchor must be anon-resolvable at the apex (ORG-047 D2).** It cannot hide behind the login-gated git subdomain — which also means its availability is its own concern (this ADR).

## Decision

### D1 — Handshake-failure handling: clean ABORT, then bounded backoff

A failed handshake is a clean ABORT (ORG-047 D1) with a logged precondition (ORG-070). **Repeated** failures from a counterparty trigger **exponential backoff + temporary throttle** (a Split B rate-dimension), NOT an immediate permanent de-auth — a transient failure (network, a mid-rotation cache miss) must not evict a legitimate kin. Backoff is automatic + reversible; permanent removal stays a governance decision (D-below). *Justifies-via:* ORG-047 D1 + the reversibility-first posture.

### D2 — Session quarantine: automatic, reversible isolation of a misbehaving live counterparty

A counterparty that authenticated cleanly but then misbehaves — a downgrade attempt (ORG-047 D1), a quota breach (ORG-071 D4), an auth anomaly, or a denied-action storm (ORG-069) — is **quarantined**: its session is frozen/terminated and it is flagged for governance review (an audited event, ORG-070 D3), **without** an automatic allowlist removal. Quarantine is the automatic, reversible precursor; the operator decides permanent de-authorization. This bounds damage from a compromised-mid-session kin without letting the automatic path make an irreversible governance call. *Justifies-via:* ORG-069 D3 / ORG-070 D3 (permanent de-auth is the operator's) + reversibility-first.

### D3 — Seat and seed recovery runbooks

- **Seat recovery** — a compromised seat: revoke its delegated credential (Split A deny-path), re-mint a fresh one, rotate any seat-specific keys. Bounded by the seat-cred TTL + deny-list propagation. Blast-radius = one seat (ORG-047 D2 per-seat delegation).
- **Seed-root recovery** — **references ORG-047 D5** (rotate offline, publish in `did.json`, revoke delegated keys; propagation bounded by cache TTL). This ADR does not restate the mechanism; it ensures the runbook is written, tested, and owns the escalation (seed-root recovery is a crown-jewel event → operator-involved).

*Justifies-via:* ORG-047 D5 + Split A (the mechanics) + P4 (a recovery path that is specified and tested, not assumed).

### D4 — DID-host disaster-recovery: the anchor is a SPOF; bound the failure

`did:web:snowis.me` at the apex is a single point of verification for the whole federation. DR:
- **Availability target + restore path** — the anon-resolvable apex vhost (the ORG-046 / caddy substrate) has a documented, tested restore path; the DID document + StatusList are static files, so restoration is fast and does not require the offline root.
- **Behavior when unreachable (fail-closed within cache)** — counterparties serve the DID doc from cache within its short TTL (ORG-047 D5 / ORG-071 D2); **beyond** the TTL, a handshake that cannot resolve the anchor **fails closed** (a passport cannot be honored against an unverifiable anchor). This is the deliberate cost of the freshness-first caching choice (ORG-071 D2): a brief anchor outage degrades to failed handshakes, never to accepted-but-unverified sessions.
- **No hot-swap of the identity** — DR restores the *same* `did:web:snowis.me`; it never silently fails over to a different DID (which would be an identity change, not a recovery).

*Justifies-via:* ORG-047 D2 (anon-resolvable apex) + ORG-071 D2 (freshness-first) + P1 (a specified, tested restore path over an assumed one).

### D5 — Health signals feed fleet monitoring

The federation surface emits health signals — handshake success/abort rate, resolution latency, **apex reachability**, quarantine count — into the fleet health-monitoring substrate, so a degrading surface (an intermittently-unreachable anchor, a rising abort rate) is **detected proactively**, not discovered at the next failed handshake. Instantiates the ORG-053 lifecycle-contract "monitor" obligation for the federation organ. *Justifies-via:* ORG-053 (every system is monitored) + P4.

## Normative additions (v2)

*(v2, 2026-08-11: closes the Codex ORG-041 round-1 review — the missing normative surfaces the D1–D5 decisions require so builders don't invent them independently; record: notes/2026-08-11-org072-review-round1.md.)*

- **A1 (D4) — Named anchor availability target.** The apex DR restore-time objective MUST be well within the shortest deployed cache TTL minus an alert margin, with monitored paging BEFORE TTL exhaustion and a tested restore path on a defined cadence. (ORG-072 owns the operational blast radius of ORG-071's STRICT stale-on-error policy; it does not reopen the policy.)
- **A2 (D4) — Partial / bad-origin anchor material = verification failure.** Any unverifiable, malformed, mismatched (DID-doc↔StatusList generation), expired, stale-served-with-HTTP-200, wrong-content-type/truncated, or partially-available anchor material (DID doc reachable but StatusList not, or vice-versa; CDN/proxy serving older-than-origin) is a verification FAILURE, fail-closed within the ORG-071 TTL bounds; reachability and validity emit DISTINCT health signals.
- **A3 (D4) — Pre-positioned last-known-good artifacts (integrity + monotonic freshness).** Restoration relies on a pre-positioned, integrity-verified copy of the last-good DID document + StatusList and a known restore target, reproducibly restorable — never an in-incident decision about which artifact is authoritative. The restored **StatusList MUST be freshness/monotonicity-verified, not merely integrity-verified**: a restore *below* the last-accepted generation/revocation state — which would resurrect revoked seat credentials or undo a compromise response — is treated as an anchor-validity failure, UNLESS an operator-approved root-recovery procedure explicitly declares the newer state untrusted. (Rollback is the sharp edge of static-file DR.)
- **A4 (D1) — Two-phase backoff keying (no victim-DID lockout).** Pre-auth failures MUST NOT throttle solely by claimed DID (an attacker could claim a victim's DID to poison its backoff bucket); pre-auth backoff keys on source/network attributes, verified-failure backoff keys on the verified DID/seed/seat, and mixed buckets are bounded so one bad source cannot suppress legitimate handshakes for the same DID from another source.
- **A5 (D1) — Failed-handshake log detail.** A failed-handshake ABORT log distinguishes claimed identity, verified identity (if any), source attributes, failure class, and cache/anchor state (feeding ORG-070).
- **A6 (D2) — Quarantine state machine.** Quarantine is a defined state machine (active → quarantined → review-pending → cleared / de-authorized / expired) specifying: what it applies to (session vs seat vs seed vs DID vs allowlist entry), consistent fleet-wide session termination, who/what may unquarantine, mandatory re-handshake on release, how repeated quarantine interacts with governance de-auth, and a timeout/escalation if review never completes (no stuck state).
- **A7 (D3) — Seed-root recovery triggers + escalation.** Named triggers (suspected offline-root compromise, unauthorized DID-doc key change, delegated-key compromise beyond seat scope, unexplained StatusList mutation, inability to restore trusted anchor state) with a page/severity level and any operator-approval/quorum requirement — detailed steps deferred to the runbook.
- **A8 (D5) — Health signals are alert-bearing.** The D5 signals are ALERTING health signals with thresholds, not passive counters — at minimum alerting on apex-reachability degradation, anchor-validity failures, cache-TTL-exhaustion risk during an outage, handshake-abort spikes, quarantine rate above baseline, and DID-doc/StatusList fetch latency or integrity failures.

## Alternatives considered

- **Auto-de-auth on repeated failure.** Rejected — evicts legitimate kin on transient failures; quarantine (reversible) + governance de-auth (deliberate) is the right split (D1/D2).
- **Long cache TTL so the anchor outage is invisible.** Rejected — defeats ORG-047 D5 freshness (ORG-071 D2 already settled this); fail-closed beyond the short TTL is the correct degradation (D4).
- **Fail-over to a backup DID on apex outage.** Rejected — a different DID is a different identity, not a recovery; DR restores the same anchor (D4).

## Consequences

- **Positive:** the federation degrades safely (fail-closed) and recoverably; a misbehaving kin is contained without an irreversible automatic call; the anchor SPOF has a tested restore path + defined outage behavior; degradation is monitored, not discovered.
- **Cost / risk:** quarantine + backoff are tuning surface (with Split B's limits); the fail-closed-beyond-TTL behavior means an apex outage longer than the cache TTL halts new handshakes — the deliberate price of freshness-first, mitigated by the fast static-file restore path.

## DECISIONS (justification)

- **Backoff not auto-de-auth (D1) + reversible quarantine (D2)** — justifies-via reversibility-first + ORG-069/ORG-070 D3 (permanent de-auth is the operator's).
- **Recovery runbooks (D3)** — justifies-via ORG-047 D5 + Split A + P4 (tested, not assumed).
- **DID-host DR fail-closed (D4)** — justifies-via ORG-047 D2 + ORG-071 D2 (freshness-first) + P1.
- **Health signals (D5)** — justifies-via ORG-053 (monitor obligation) + P4.
- **Normative additions A1–A8 (v2)** — justifies-via P1/P4 (name the anchor availability target, two-phase backoff keying, quarantine state machine, partial-failure handling, recovery triggers, pre-positioned artifacts, and alert thresholds so the D1–D5 decisions are enforceable-by-construction, not builder-invented); added per the Codex ORG-041 round-1 review.

## Provenance / review trail

- 2026-08-10 — HO-drafted as ORG-047 Split C per the T-2803 triage (disposition: SPLIT; operator-named split-candidate #3). Absorbs the failure/quarantine/recovery (#5) and DID-host-DR (#15) considerations.
- 2026-08-11 — Codex ORG-041 review **round 1**: not-clean (core decisions sound — rejecting auto-de-auth + backup-DID-hotswap — but missing normative surfaces: named availability target, two-phase backoff keying, quarantine state machine, partial-anchor-failure handling, recovery triggers, alert thresholds). HO integrated into **v2**: the **Normative additions** section (A1–A8). Record: `notes/2026-08-11-org072-review-round1.md`.
- PENDING — Codex ORG-041 **round 2** on v2, then the operator ratification via a ratify ticket. On ratify: file the build sub-ticket set (backoff/quarantine logic, seat/seed recovery runbooks, apex DR restore + fail-closed behavior, health-signal emitters) under scoping ticket T-2813, per the ADR-scoping rule.
