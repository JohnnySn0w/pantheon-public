---
aliases: [ORG-072]
id: ORG-072
title: "Cross-seed passport — lifecycle health & recovery: handshake-failure handling, session quarantine, seat/seed recovery, and DID-host DR"
status: "Ratified-with-relocation 2026-09-06 by the operator via TG 1786-1791 (specialist-mediated live process discussion — 'is the over-spec good?'). Verdict: ratify D1-D5 (design-tier: D1 backoff-not-auto-de-auth + D2 reversible quarantine + D3 seed-root recovery + D4 DID-host SPOF fail-closed 'a different DID is a different identity, not a recovery' + D5 health signals). RELOCATED the build-tier A-item detail (A1-A8: numeric RTO/SLO framing, quarantine state-machine mechanics, two-phase backoff keying, log-field enumeration, alert thresholds) to notes/2026-09-06-org072-normative-additions-relocated.md — kept + linked, not deleted, per the operator's mechanism (TG 1787). Also SEEDED the general altitude rule (build-tier out of ratified ADRs) into the ADR SOPs — see separate SOP-amendment handoff ticket. HO-drafted 2026-08-10 as ORG-047 Split C (T-2813), executing the T-2803 triage disposition. Specifies how the federation surface fails safely and recovers — failure handling, mid-session quarantine, seat/seed recovery, DID-host disaster-recovery, and health signals. Not yet built."
date: 2026-08-10
build_disposition: needs-build
scoping_ticket: T-2813
# elements: — ORG-065 machine index (T-3476 ratifies-edge target for T-2860).
# ORG-072's ratified body carries D1-D5 (design-tier decisions); the A1-A8
# build-tier detail was RELOCATED to notes/2026-09-06-org072-normative-
# additions-relocated.md per the operator's altitude ruling (TG 1786-1791,
# 2026-09-06 ratify-with-relocation), so only D1-D5 elementize here — A1-A8
# is not in-ADR. Added by HO; no obligation content changed.
elements:
  - {id: E1, text: "D1: Handshake-failure handling — clean ABORT (ORG-047 D1) then bounded exponential backoff + temporary throttle; repeated failures do NOT trigger immediate permanent de-auth"}
  - {id: E2, text: "D2: Session quarantine — automatic, reversible isolation of a misbehaving live counterparty (downgrade attempt / quota breach / auth anomaly / denied-action storm); permanent de-auth stays operator-owned"}
  - {id: E3, text: "D3: Seat and seed recovery runbooks — seat recovery (revoke delegated cred, re-mint, rotate seat-specific keys) is per-seat; seed recovery is a governance decision"}
  - {id: E4, text: "D4: DID-host disaster-recovery — did:web:your-domain.example anchor is a SPOF; fail-closed beyond cache TTL; a different DID is a different identity, not a recovery (no identity hot-swap)"}
  - {id: E5, text: "D5: Health signals feed fleet monitoring — handshake success/abort rate, resolution latency, apex reachability, quarantine count emitted so degrading surfaces are detected proactively"}
---

# ORG-072 — Cross-seed passport: lifecycle health & recovery

**Status:** Ratified-with-relocation 2026-09-06 by the operator via TG 1786-1791 (specialist-mediated; T-2860). D1-D5 ratified; A1-A8 build-tier detail relocated to notes/2026-09-06-org072-normative-additions-relocated.md — kept + linked. **Author of record:** Orchestrator (HO).
**Relationship:** referenced split of **[[ORG-047]]** (cross-seed passport). ORG-047 owns identity + handshake; this ADR owns *what happens when things go wrong* — failed handshakes, a misbehaving live counterparty, a compromised seat/seed, and an unreachable anchor. Filed per the **T-2803** triage (operator-named split #3, "lifecycle health/recovery"; absorbs the failure/quarantine/recovery and DID-host-DR considerations); scoping ticket **T-2813**.
**Source of authority:** the T-2803 triage disposition + ORG-047 D5's seed-root rotation-recovery (which this references, not duplicates).
**Relates to:** [[ORG-047]] D1 (handshake ABORT), D5 (seed-root recovery, seat-cred revocation); [[ORG-047]] Split A (credential lifecycle — revoke/re-mint mechanics this invokes); [[ORG-070]] (Split E — quarantine + recovery events are audited); [[ORG-071]] (Split B — rate/quota trips that feed quarantine); the fleet health-monitoring substrate; [[ORG-053]] (universal system-lifecycle contract — the install/monitor/remove framing this instantiates for the federation surface).

## Why this ADR exists

ORG-047 specifies the *happy path* (a clean mutually-authenticated handshake) and the *root-compromise* path (D5 rotation-recovery), but the space between is unspecified: a handshake that keeps failing, a counterparty that authenticates cleanly then misbehaves mid-session, a single seat whose credential is compromised, and — the sharpest gap — an **anchor that is unreachable**. `did:web:your-domain.example` is a single point of verification for the entire federation: if the apex is down, *no* passport verifies. This ADR names the failure-handling, quarantine, recovery, and DR so the federation degrades safely rather than unpredictably.

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

`did:web:your-domain.example` at the apex is a single point of verification for the whole federation. DR:
- **Availability target + restore path** — the anon-resolvable apex vhost (the ORG-046 / caddy substrate) has a documented, tested restore path; the DID document + StatusList are static files, so restoration is fast and does not require the offline root.
- **Behavior when unreachable (fail-closed within cache)** — counterparties serve the DID doc from cache within its short TTL (ORG-047 D5 / ORG-071 D2); **beyond** the TTL, a handshake that cannot resolve the anchor **fails closed** (a passport cannot be honored against an unverifiable anchor). This is the deliberate cost of the freshness-first caching choice (ORG-071 D2): a brief anchor outage degrades to failed handshakes, never to accepted-but-unverified sessions.
- **No hot-swap of the identity** — DR restores the *same* `did:web:your-domain.example`; it never silently fails over to a different DID (which would be an identity change, not a recovery).

*Justifies-via:* ORG-047 D2 (anon-resolvable apex) + ORG-071 D2 (freshness-first) + P1 (a specified, tested restore path over an assumed one).

### D5 — Health signals feed fleet monitoring

The federation surface emits health signals — handshake success/abort rate, resolution latency, **apex reachability**, quarantine count — into the fleet health-monitoring substrate, so a degrading surface (an intermittently-unreachable anchor, a rising abort rate) is **detected proactively**, not discovered at the next failed handshake. Instantiates the ORG-053 lifecycle-contract "monitor" obligation for the federation organ. *Justifies-via:* ORG-053 (every system is monitored) + P4.

## Normative additions (v2) — RELOCATED

**A1–A8 elaborate D1–D5 with builder-facing detail (RTO framing, two-phase backoff keying, quarantine state machine, log fields, alert thresholds). The operator RATIFY-WITH-RELOCATION (TG 1786-1791, 2026-09-06) moved this section body to a referenced note; kept + linked, not deleted:**

- **See `notes/2026-09-06-org072-normative-additions-relocated.md`** for A1–A8 full text.
- Substance is unchanged; the relocation surfaces the design/build altitude split: D1–D5 = ratified design decisions; A1–A8 = the elaborations builders wire.
- Builder tickets under scoping ticket T-2813 implement per the referenced note + the operator's tuning judgment at first-fire.

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
- **Normative additions A1–A8 (v2, relocated 2026-09-06)** — justifies-via P1/P4 (name the anchor availability target, two-phase backoff keying, quarantine state machine, partial-failure handling, recovery triggers, pre-positioned artifacts, and alert thresholds so the D1–D5 decisions are enforceable-by-construction, not builder-invented). Full text at `notes/2026-09-06-org072-normative-additions-relocated.md` per the operator RATIFY-WITH-RELOCATION (TG 1786-1791).

## Provenance / review trail

Drafting and review history (ORG-047 Split C; Codex ORG-041 round 1 → v2; ratified with relocation 2026-09-06): `notes/2026-10-08-org-072-review-history.md`.
