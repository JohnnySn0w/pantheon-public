---
aliases: [ORG-095]
id: ORG-095
title: "Cross-seed passport — credential + key lifecycle (root + delegated + seat + VC): mint / rotate / revoke / expire / decommission + algorithm agility (ORG-047 Split A)"
status: 'Draft (Authored 2026-09-06 by Orchestrator (HO); §1 revised same day to two-tier custody model — automated-custody DEFAULT + offline-root OPTIONAL hardened tier — after the operator reopened V3 on TG 6765 as non-viable for non-technical operators, then endorsed the two-tier direction on TG 6767/6768. V1/V2 unchanged from T-2810 prefer-adr envelope. Awaiting the operator ratify on the concrete revised shape.)'
date: 2026-09-06
author: Orchestrator (HO)
appetite: 5
build_disposition: needs-build
scoping_ticket: T-2811
supersedes: []
extends: [ORG-047]
relates: [ORG-047, ORG-069, ORG-070, ORG-071, ORG-072, T-2803, T-2810, T-2811]
labels: [org-047, passport, security, credential-lifecycle, key-lifecycle, adr-split, split-a]
ratifiable_files: [pantheon-config:architecture/adr/ORG-095-passport-credential-key-lifecycle.md]
---

# ORG-095 — Cross-seed passport: credential + key lifecycle (ORG-047 Split A)

**Status:** Draft (Authored 2026-09-06 by Orchestrator (HO); V3 direction endorsed by the operator via recovery-relayed TG 6767/6768 — automated-custody DEFAULT, offline-root demoted to OPTIONAL hardened tier for technical operators; §1 revised 2026-09-06 to reflect the two-tier model). **Date:** 2026-09-06 **Author:** Orchestrator (HO) **HITL:** Ratify-required. **Related:** [[ORG-047]] D5 (this ADR extracts + fully specifies), [[ORG-069]] (Split D — content/tool authorization), [[ORG-070]] (Split E — evidence & governance), [[ORG-071]] (Split B — network capabilities), [[ORG-072]] (Split C — lifecycle health & recovery), [[T-2803]] (security-considerations triage), [[T-2810]] (V1+V2 envelope — DECIDED 2026-09-06 prefer-adr TG 6755/6757/6759), [[T-2811]] (this ADR's scoping ticket), [[T-3488]] (V3 redesign — direction endorsed TG 6767/6768; concrete model in §1 below).

**Absorbs (T-2803 triage):** consideration #3 (key lifecycle / revocation), #16 (algorithm agility as lifecycle event), #18 (decommission / data-erasure as terminal lifecycle).

## Context

[[ORG-047]] D5 named the credential + key lifecycle as one of ORG-047's five "narrower concerns" and pointed the full mechanism to a referenced ADR. This is that ADR. The load-bearing reason for the split: ORG-047's ratify boundary stays the *shape* (one DID, multilingual adapters, D1–D6 posture); lifecycle mechanism is a *substantial concern* that stands alone — three key tiers (root / delegated / seat) plus VC status, each with its own mint / rotate / revoke / expire / decommission surface, plus algorithm agility as a cross-cutting lifecycle event.

The value envelope was surfaced under [[T-2810]] and DECIDED by the operator via recovery-relayed Telegram 6755/6757/6759 (2026-09-06) as "prefer adr" — the three parameters land at ORG-047 D5's recommended shapes:

- **V1 (seat-cred TTL):** `<=24h` + published deny-list for sub-TTL revocation.
- **V2 (VC status mechanism):** StatusList2021 referenced via `credentialStatus` on the anon-resolvable apex (same host as the DID document).
- **V3 (key custody) — REVISED post-TG-6765/6767:** two-tier custody model. **Baseline (non-technical operator default):** platform-authenticator-backed key in the OS keystore (Keychain / CNG / libsecret), automated rotation, no manual ceremony. **Hardened tier (optional, technical operator):** offline root + operator-chosen custody. Both tiers share the delegated-key + seat-cred + VC substrate below. Quarterly root rotation cadence applies to both tiers; `<=monthly` delegated cadence applies to both. Corpus-anchored per WebAuthn/Passkeys, Signal Protocol identity keys, and Saltzer-Schroeder psychological-acceptability. Concrete model in §1 below.

Sub-parameters below D1–D6 (Cache-Control TTLs, re-mint cadences, tombstone retention, deprecation windows, key-version tagging) are HO-adopted under P/D + digest per [[feedback_adr_open_decisions_triage_before_decision_ask]] — reversible, non-secret, standard-practice defaults, all overridable at build time on evidence.

## Decision

### 1. Root key lifecycle — two-tier custody model (V3 anchor, revised)

The operator endorsed the two-tier direction on 2026-09-06 (TG 6767/6768 via recovery relay) after reopening the pure-offline-root baseline on TG 6765: non-technical operators cannot safely execute an offline-root ceremony as a default. The concrete model resolves along the corpus's canonical answer for consumer identity-key custody (WebAuthn/Passkeys, Signal Protocol identity keys, OS keychains) with the offline-root case demoted to an optional-hardened tier for technical operators.

**Tier BASELINE (non-technical operator, default):**

- **Custody:** the seed signing key lives in the OS platform keystore, specifically:
  - **macOS:** Keychain via `Security.framework` (kSecClass keys, Secure Enclave-backed where available for P-256).
  - **Windows:** CNG (Cryptography Next Generation) via `BCryptGenerateKeyPair` + DPAPI-protected persistent keys.
  - **Linux desktop:** libsecret via the freedesktop.org Secret Service API (GNOME Keyring / KWallet).
  - **Linux headless / container / WSL:** libsecret with a `pass`-backed or gnome-keyring-daemon-headless implementation as the fallback. **Unattested-platform posture:** treated as attestation-unavailable (see below).
- **Unlock:** the operator's existing device-authenticated session IS the unlock ceremony. No separate custody password. Biometric-lock where the platform offers it (Touch ID / Windows Hello / Android BiometricPrompt).
- **Rotation cadence:** QUARTERLY, AUTOMATED. Fires on the scheduled boundary + on-signal (device-reset detected via keystore-item-missing OR platform OS-reinstall marker). No manual ceremony.
- **Compromise-recovery:** if the platform keystore is unavailable (device reset, keystore-integrity anomaly) → seed spawns a NEW identity + re-enrolls via the [[ORG-047]] D1 out-of-band allowlist path (the "different DID is a different identity" posture from [[ORG-072]] D4). If the compromise is partial (delegated-key or seat scope) → rotate via the delegated-key path (§2), root untouched. **The `did:web` republish path is unchanged from ORG-047 D5** — recovery latency bounded by counterparty Cache-Control TTL (§5, 300s / 5min).
- **Blast radius on baseline root compromise:** identity is lost + trust is rebuilt via re-enrollment, not "total federation impersonation until the offline rotation ceremony completes." The trade-off: reduced blast-radius-duration + acceptance of identity-discontinuity as the recovery outcome, vs the hardened-tier's identity-continuity-with-ceremony.
- **Attestation-check (optional, config-driven):** where the platform authenticator supports attestation (WebAuthn TPM/Titan/Secure Enclave attestation surfaces), the seed can require attestation on custody-init + refuse to run on unattested/rooted platforms in high-security-mode config. Default posture: `accept-attestation-where-available, accept-softtoken-with-log-elsewhere`. Config-time-overridable.

**Tier HARDENED (technical operator, optional):**

- **Custody:** OFFLINE root, operator-chosen storage medium + location (operator-reserved off-doc for the fleet-canonical instance; per-operator-chosen for external seeds).
- **Rotation cadence:** QUARTERLY, MANUAL CEREMONY. Rotation is scheduled + audited (governance event, [[ORG-070]] D3 evidence trail).
- **Compromise-recovery path:** rotate root offline → republish new key in the `did:web:snowis.me` document (single propagation point; counterparties re-resolve on next handshake) → revoke every delegated key chained to the old root via the deny-list mechanism (§3) and StatusList2021 flip (§4) for any VCs delegated-issued under it. Identity CONTINUES across rotation (this is the tier's differentiator).
- **Recovery latency:** bounded by counterparty `did.json` cache TTL (§5, 300s / 5min); a hardened-tier root rotation propagates in minutes, not days.
- **Blast radius on hardened root compromise:** total federation impersonation until the recovery path completes — the crown-jewel identity. Everything downstream reduces to this event; the recovery path is normative + tested + drilled (see §Consequences).

**Tier selection:**

- Config-time operator toggle. Default = BASELINE. Reversible: an operator may switch from baseline → hardened via a documented migration path (mint hardened key, migrate delegated keys to chain from it, retire the baseline key via §7 decommission) — a bounded operation.
- **Fleet-canonical instance policy:** the Pantheon fleet on the VPS runs in HARDENED tier (this seed IS operator-managed by a technical operator — the operator + HO), preserving the ORG-047 D5 originally-decided posture for this specific instance. External seeds (a non-technical user's laptop) run in BASELINE tier by default.
- **Common substrate below tier:** both tiers use the same delegated-key + seat-cred + VC lifecycle from §2–§4. Tier affects only §1.

**Sub-decisions in this section, HO-adopted under P/D per calibration pin (all reversible, non-secret, standard-practice defaults):**

- Linux headless fallback keystore: `pass` via libsecret shim OR gnome-keyring-daemon in headless mode. ADOPT `pass` shim as first-preference (per-user filesystem, GPG-encrypted, no daemon).
- Attestation-check strictness levels: `accept-attestation-where-available, accept-softtoken-with-log` as baseline; `require-platform-attestation` as high-security-mode config. ADOPT.
- Device-reset detection heuristic: keystore-item-missing on startup OR OS-reinstall marker (`/var/lib/pantheon/last-boot-id` diverges from stored). ADOPT.
- Container/WSL/CI environments: mark as `unattested platform` + prompt operator to opt into hardened tier if the deployment is production. ADOPT default = baseline-with-warning.
- Baseline-to-hardened migration: documented as a §7 decommission-of-baseline-cred + fresh hardened-key issuance; not a per-key rotation. ADOPT.

### 2. Delegated key lifecycle (tier-agnostic)

Delegated-key lifecycle applies identically to both custody tiers from §1 — the delegated key is the ONLINE signing role in both cases, distinguished only by what mints it (§1 baseline: platform-keystore-held root mints; §1 hardened: offline root mints).

- **Custody:** ONLINE. Rotation is automated; keys live in the signing environment (same keystore mechanism as the baseline root in §1; a distinct key material inside the same keystore).
- **Rotation cadence:** `<=MONTHLY`. Cadence-inside-cadence: shorter windows for higher-risk schemes (e.g. adapter-adjacent VC issuance) at build-time discretion.
- **Per-scheme allowlist:** each delegated key advertises which adapter scheme it may sign for (mTLS / Bearer / VC issuance). A key MUST NOT sign for a scheme outside its declared allowlist (enforced at the adapter layer per [[ORG-047]] D2). The allowlist is a signed attestation from the root (baseline: platform-keystore-signed; hardened: offline-root-signed), propagated via `did.json` `verificationMethod` entries.
- **Compromise-recovery:** revoke the single delegated key (deny-list add + StatusList2021 flip for any VCs it issued) → re-mint from root → propagate the new `verificationMethod` via `did.json` (Cache-Control bounds visibility). **Blast radius:** the schemes on that key's allowlist, until propagation completes.
- **Chained revocation on root rotation (§1):** every delegated key derived under the rotated root is revoked as a chain; the new root re-mints delegated keys as needed. Applies to both tiers.

### 3. Seat credential lifecycle (V1 anchor)

- **TTL:** `<=24h` (V1-decided). Enforced at issue time; verifiers refuse an expired credential without a network round-trip.
- **Re-mint cadence:** 12h (halve-TTL, adopted under P/D — standard cadence-inside-TTL practice; a seat that fails to re-mint within its TTL simply loses access at expiry, no manual revoke needed).
- **Sub-TTL revocation (a seat compromised inside its TTL window):** published DENY-LIST fetched + cached by verifiers with a short TTL. A seat presenting a denied credential is REFUSED even while the credential is still time-valid.
- **Deny-list hosting:** anon-resolvable apex adjacent to `did:web`, at `/.well-known/passport-deny-list.json` (adopted under P/D — mirrors StatusList2021 hosting for operational coherence + single-host reachability).
- **Deny-list cache TTL:** 60s (adopted under P/D — tight because seat revocation is per-credential + high-signal; the trade-off with StatusList2021's 60s is deliberate, they share the ops surface).
- **Blast radius on compromise:** exactly one seat ([[ORG-047]] D2 clarification, re-affirmed here). A seat presents only its own credential; cannot present as another seat, cannot present the seed's full seat-roster.

### 4. VC (Verifiable Credential) lifecycle (V2 anchor)

- **Status mechanism:** [W3C StatusList2021](https://www.w3.org/TR/vc-status-list/) referenced via each VC's `credentialStatus` field (V2-decided).
- **Hosting:** anon-resolvable apex (same host as `did:web`, sibling path `/.well-known/passport-status-list.json`), so a counterparty holds a single resolution point for both identity + status.
- **Issuer flow:** the delegated key with VC-issuance scope on its allowlist (§2) signs the VC + emits a StatusList entry (initial state: valid). Revocation flips the entry (valid → revoked); the signed StatusList is re-published atomically.
- **Verifier flow:** on presentation, verifier fetches the StatusList from `credentialStatus.statusListCredential` (cache-honored per §5 TTL), checks the bitstring index, REFUSES on `revoked` OR on unfetchable-beyond-TTL (fail-closed; a stale-negative is safer than a stale-positive here).
- **VC compromise-recovery:** flip StatusList entry → propagate via next verifier resolve. Chained revocations on delegated-key rotation (§2) or root rotation (§1) flip every VC issued under the rotated key.

### 5. Algorithm agility (T-2803 consideration #16)

- **Crypto-agility as lifecycle event:** rotating a key to a new algorithm (e.g. Ed25519 → Ed448, or ECDSA P-256 → P-384) is a *lifecycle event*, not an ADR revision. Mechanism reuses the rotation path from §1/§2 — mint new key with new alg, publish via `did.json` `verificationMethod`, update per-scheme allowlist (§2), revoke old-alg key on the deprecation-window boundary.
- **Key version tagging:** `verificationMethod` id-suffix versioning (`:v1`, `:v2`, ...) per [DID Core §5.3.1](https://www.w3.org/TR/did-core/#verification-methods) precedent (adopted under P/D — matches spec examples + gives counterparties a deterministic reference).
- **Deprecation window:** `2 × rotation cadence` — 2 months for delegated (§2 monthly rotation) and 2 quarters for root (§1 quarterly rotation). Adopted under P/D — reversible; gives counterparties two natural re-resolution cycles to migrate before old-alg-verify is refused.
- **`did.json` Cache-Control TTL:** 300s / 5min. Adopted under P/D — satisfies ORG-047 D5's "minutes not days" bound with operational headroom.
- **StatusList2021 cache TTL:** 60s. Adopted under P/D — tighter than `did.json` because status changes are per-credential + higher-frequency than key rotation.
- **No new adapter build per alg swap:** existing adapters MUST accept the per-scheme allowlist (§2) as key input; adding a new algorithm updates the allowlist, not the adapter code.

### 6. Decommission + erasure (T-2803 consideration #18)

- **Terminal lifecycle event** for any seat / delegated / VC identity that is being permanently removed from the federation (not merely rotated):
  1. **REVOKE first** — deny-list add (§3 for seat, §2 for delegated) + StatusList2021 flip (§4 for VCs). Verifiers see revocation before any orderly-removal step lands.
  2. **ERASE credential material** — per-scheme-appropriate: file unlink for FS-held keys (with secure-delete where the medium supports; standard unlink otherwise), HSM-command for HSM-held. No credential material persists after erasure.
  3. **Remove from allowlists** — the identity is removed from own allowlists AND counterparties are notified via the same governance channel as enrollment (per [[ORG-070]] D3 governance edges).
  4. **TOMBSTONE the identifier** — the identifier (DID / cred id / VC id) is retained in a tombstone log with retention-window bound (§below) to prevent identifier re-use.
- **Tombstone retention:** 90 days (adopted under P/D — reversible; long enough to prevent identifier re-use in normal ops, short enough to bound the record set; reserved-forever is over-engineering absent a specific compliance mandate).
- **Root decommission** is a GOVERNANCE event (operator-gated), not a routine lifecycle op — pointer only; if it ever happens, treat as the compromise-recovery path from §1 executed deliberately + with full audit-chain review.

### 7. Recovery paths (cross-reference)

Compromise-recovery paths for each layer are specified inline in that layer's section (§1 root, §2 delegated, §3 seat, §4 VC). This section is the cross-reference index + names the escalation ladder:

- **Seat compromise:** §3 deny-list flip. Blast radius = 1 seat. Recovery latency = 60s (deny-list cache TTL).
- **Delegated key compromise:** §2 revoke + re-mint. Blast radius = schemes on that key's allowlist. Recovery latency = 300s (`did.json` Cache-Control TTL for allowlist propagation) + 60s (StatusList2021 for VCs).
- **Root compromise:** §1 offline rotate + republish + chained revocation. Blast radius = total federation impersonation. Recovery latency = 300s + operator time to execute the offline rotation ceremony. **operator-notify on any root compromise event, always.**
- **Algorithm compromise** (e.g. curve break disclosed publicly): §5 crypto-agility rotation. Blast radius = every counterparty using the compromised algorithm. Recovery latency = deprecation window (weeks) unless emergency-shortened via operator-gated escalation.

## Alternatives considered

- **Long-lived seat credentials with online revocation-only** (no TTL, deny-list-only). REJECTED — revocation-only means an offline deny-list host = permanent trust of any credential ever issued. Expiry-by-default is the fail-closed default; deny-list is the additional layer for sub-TTL events.
- **CRL-style X.509 revocation** instead of StatusList2021. REJECTED — CRL is the same primitive but in a wire format we'd have to hand-roll VC-side; StatusList2021 is the VC-native standard with reference impls + no protocol invention.
- **HSM-only key custody across all three tiers.** REJECTED as MANDATORY — HSM for root is compatible with the §1 HARDENED tier custody but not itself specified (the offline WHERE is operator-chosen). Baseline tier uses OS platform keystores (Secure Enclave / TPM / StrongBox where available) which subsume HSM guarantees on hardware-attested devices without operator ceremony. For delegated + seat keys, HSM is available at operator discretion at build time; mandating it forecloses the software-key fast-iteration path.
- **Pure offline-root baseline (the ORG-047 D5 originally-recommended custody) for all seeds.** REJECTED — the operator reopened this on 2026-09-06 TG 6765 as non-viable for non-technical seed operators. Whitten & Tygar 1999 ("Why Johnny Can't Encrypt") + subsequent Passkeys / WebAuthn corpus is the canonical evidence that offline-key-ceremony as a consumer default is a usability failure. Kept as OPTIONAL hardened tier per TG 6767/6768 for technical operators.
- **Fully cloud-managed custody** (Vault, cloud-KMS, escrow service) as baseline. REJECTED — introduces a third-party trust anchor + a network dependency for identity operations. OS platform keystores already give the "vendor-attested" property (Apple / Microsoft / Linux distro) without the cloud round-trip.
- **Immutable identifiers (no tombstone / infinite re-use prevention).** REJECTED — tombstone with retention window is the standard practice; infinite retention is over-engineering, zero retention risks accidental re-use.
- **Per-adapter key-lifecycle policy** (each adapter defines its own rotation). REJECTED — this ADR's whole point is unified lifecycle across mTLS + Bearer + VC. Adapters consume the per-scheme allowlist (§2) but do not define their own key policy.

## Consequences

**positive:** ORG-047 D5's three surfaces (custody+rotation, revocation, VC status) are structurally specified end-to-end; the baseline tier makes non-technical-operator seeds viable (Whitten-Tygar UX-failure class avoided); the hardened tier preserves identity-continuity for technical operators who need it; every key tier has a documented compromise-recovery path bounded by measurable latency; algorithm agility is a lifecycle event, not an ADR revision; decommission is a first-class terminal event, not implied by absence; the ADR does not force an HSM decision on the operator but does not preclude one; baseline tier ships identity-continuity trade-off honestly (compromise recovery = new identity, not preserved-identity-via-ceremony).

**negative / accepted:** two-tier surface adds a config-time decision (mitigated: default = baseline, migration between tiers documented); baseline TRUSTS the OS platform keystore vendor (Apple / Microsoft / Linux distro) — a real trust delegation vs the hardened tier's operator-held root; baseline identity-loss-on-device-reset requires re-enrollment via ORG-047 D1 out-of-band allowlist path (acceptable per the "different DID is a different identity" posture of [[ORG-072]] D4); operational overhead of a re-mint cadence at each tier (monthly delegated, 12h seat) — accepted as the cost of expiry-by-default; deny-list hosting is a new operational endpoint at the same apex as `did:web` (bounded — same host, sibling path); tombstone-log growth over time (bounded by 90d retention window).

**deferred to build (T-2018-adjacent + build tickets under this ADR post-ratify):**
- HSM adoption decisions per tier (operator + build-time)
- Deny-list + StatusList2021 wire-format specifics (JSON schema, signing envelope)
- Rotation-ceremony operator runbook for root (references operator-reserved WHERE)
- Metrics + alerting for rotation-cadence adherence
- Chained-revocation performance under root-compromise scale (all delegated + all VCs)

**explicitly out-of-scope:** OS/uid-level worker isolation (T-1347's domain per ORG-047 D2 clarification); cross-seed gossip for revocation (ORG-047 D5 says explicit "no cross-seed gossip"); non-passport credential lifecycle (this ADR is passport-scoped).

## DECISIONS (justifies-via)

- **Split-into-referenced-ADR shape** (this whole ADR) — justifies-via [[ORG-047]] D5 pointer + [[T-2803]] triage disposition (Split A named). Keeps ORG-047's ratify boundary at *shape*; lifecycle mechanism stands alone.
- **Three-tier key model (root + delegated + seat)** — justifies-via [[ORG-047]] D5 (recommended shape) + P1 (separation of custody/rotation-cadence/blast-radius across tiers is structural, not policy).
- **Two-tier root custody (baseline platform-keystore-backed + hardened offline-root optional)** — justifies-via corpus (WebAuthn/Passkeys, Signal Protocol identity keys, Saltzer-Schroeder psychological-acceptability, Whitten-Tygar 1999 UX-failure evidence for consumer-key manual ceremony) + operator-direction-endorsement TG 6767/6768 via recovery relay + [[T-3488]] design pass. Baseline is the industry-standard answer for consumer identity-key custody; hardened preserves identity-continuity for technical operators.
- **Quarterly root + `<=monthly` delegated + `<=24h` seat rotation cadences** — justifies-via V3/V1 operator-decided envelope (T-2810 close). Cadences apply to both custody tiers.
- **StatusList2021 + credentialStatus** — justifies-via V2 operator-decided envelope (T-2810 close) + W3C VC Data Model native shape (no protocol invention).
- **Sub-parameter adoptions under P/D** (12h re-mint, 300s `did.json` TTL, 60s deny-list/StatusList TTL, `:vN` id-suffix versioning, 2× deprecation window, 90d tombstone retention, Linux headless keystore = `pass`, attestation-strictness = accept-attestation-where-available, device-reset heuristic, container/WSL/CI baseline-with-warning, migration-via-decommission) — justifies-via [[feedback_adr_open_decisions_triage_before_decision_ask]] — all reversible, non-secret, standard-practice defaults. Each is overridable at build time on measured evidence.
- **Hardened-tier offline root WHERE stays off-doc** — justifies-via V3 operator-reserve for the fleet-canonical hardened instance (secret-bearing, irreversible per-operator). Baseline tier has no operator-chosen WHERE (platform-defined). ADR mechanism does not depend on the choice for either tier.
- **Fleet-canonical instance runs HARDENED** — justifies-via preservation of the ORG-047 D5 originally-decided posture for THIS specific instance (technical-operator-managed by the operator + HO). External seeds default to BASELINE per the operator's TG 6765/6767 non-technical-operator-viability requirement.
- **Recovery paths specified per-tier + rooted in bounded latency** — justifies-via P1 (structural bound over discipline) + P4 (audit-trail latency is measurable) + [[ORG-047]] D5 explicit "propagate in minutes, not days".
- **operator-notify on root compromise, always** — justifies-via D1 escalation trigger for the crown-jewel event (blast-radius = total federation impersonation).
- **Algorithm agility as lifecycle event, not ADR revision** — justifies-via P7 (minimal-footprint — reuse existing rotation path over new mechanism) + the per-scheme allowlist decoupling (§2) that makes it possible.

## Provenance / review trail

- 2026-09-06 — HO-drafted (Orchestrator) against the operator's V1/V2 decided envelope (T-2810 close via recovery-relayed TG 6755/6757/6759) + T-2803 triage disposition + ORG-047 D5 recommended shapes.
- 2026-09-06 (same day) — V3 REOPENED by the operator TG 6765 (non-technical-operator-viability); Recovery filed [[T-3488]] for redesign.
- 2026-09-06 (same day) — HO corpus-first design pass posted on T-3488; the operator endorsed the DIRECTION (automated-custody baseline + hardened optional) via recovery-relayed TG 6767/6768.
- 2026-09-06 (same day) — §1 revised in place to the two-tier concrete model; §2 clarified as tier-agnostic; §Alternatives + §Consequences + §DECISIONS updated to reflect the revised shape.
- Pending: the operator ratify decision (ratify ticket filed as sibling with ratifiable_files pointing here — replaces the retracted T-3489).
- Corpus references: [W3C DID Core](https://www.w3.org/TR/did-core/), [W3C VC Data Model 2.0](https://www.w3.org/TR/vc-data-model-2.0/), [StatusList2021](https://www.w3.org/TR/vc-status-list/), [did:web method spec](https://w3c-ccg.github.io/did-method-web/), [WebAuthn Level 3](https://www.w3.org/TR/webauthn-3/), [Signal Protocol Sesame + X3DH](https://signal.org/docs/), Whitten & Tygar 1999 "Why Johnny Can't Encrypt", Saltzer & Schroeder 1975 (design principles — psychological acceptability, least common mechanism, fail-safe defaults).

## Re-evaluate when

*(machine-checkable triggers per T-2527 `adr_reeval_walk.py`; all fuzzy events → surface-only, no auto-file.)*
