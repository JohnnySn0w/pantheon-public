---
aliases: [ORG-069]
id: ORG-069
title: "Cross-seed passport — content & tool authorization: the AUTHZ layer atop the AUTHN passport (ORG-047 D4 cashed into an enforceable model)"
status: "Ratified 2026-09-06 by the operator via TG 1780 (specialist-mediated live review with the ADR-sweep over-spec finding attached). Verdict: ratify AS-IS — load-bearing AUTHN/AUTHZ default-deny invariant the rest of the cluster enforces; N1-N8 kept as Codex-flagged enforceability-critical, not trimmed. The operator accepted the 'core is a security posture, not a param table' framing. HO-drafted 2026-08-10 as ORG-047 Split D (T-2814), executing the T-2803 triage disposition. Cashes ORG-047's normative D4 invariant (identity gates who, never what) into a concrete default-deny authorization model. Not yet built; the enforcement converges onto the Action Broker (ORG-063)."
date: 2026-08-10
build_disposition: needs-build
scoping_ticket: T-2814
# elements: — ORG-065 machine index (T-3476 ratifies-edge target for T-2858).
# Multi-group ADR per pass-2 normalization extension (T-2946 pairs 3-8): E-ids
# run sequentially in document order across the groups; each element's text
# opens with the native id verbatim as the audit anchor. Group map:
#   E1-E5 mirror D1-D5 (§Decision — the default-deny model, 5 items).
#   E6-E13 mirror N1-N8 (§Normative invariants — the enforceable rules the
#     D1-D5 model requires, 8 items).
# Added by HO; no obligation content changed.
elements:
  - {id: E1, text: "D1: AUTHN and AUTHZ are separate layers; a valid passport is necessary, never sufficient"}
  - {id: E2, text: "D2: Default-deny action surface + explicit, least-privilege capability grants"}
  - {id: E3, text: "D3: Standing access-control is inviolable by any passport (the no-self-bootstrapping rule)"}
  - {id: E4, text: "D4: Content stays untrusted regardless of identity (verbatim from ORG-047 D4)"}
  - {id: E5, text: "D5: Enforcement converges onto the Action Broker; provenance is a precondition; every decision is audited"}
  - {id: E6, text: "N1: Per-invocation check — a passport-authenticated session never carries durable authority"}
  - {id: E7, text: "N2: Default-deny for the unknown — any unknown/unexposed action-id or malformed scope DENIES fail-closed"}
  - {id: E8, text: "N3: Canonical-principal binding — grants bind to the stable DID, never to rotatable transport/signing keys or self-asserted metadata"}
  - {id: E9, text: "N4: Effects-based no-self-bootstrapping — a grant MUST NOT authorize any action whose effect creates/modifies/bypasses identity trust, grants, or enforcement"}
  - {id: E10, text: "N5: Revocation binding — revocation takes effect for all future checks; queued actions MUST be re-authorized immediately before execution"}
  - {id: E11, text: "N6: Operator-controlled, fail-closed tool exposure — a grant MUST NOT reference a tool identifier until it is in the vetted exposed-tool registry"}
  - {id: E12, text: "N7: Broker-equivalence of the interim gate — every interim allow decision MUST be representable as a future Action Broker capability decision without semantic loss"}
  - {id: E13, text: "N8: Chained actions carry the original identity — chained/delegated/scheduled actions MUST be authorized as the original counterparty's under its grant"}
---

# ORG-069 — Cross-seed passport: content & tool authorization

**Status:** Ratified 2026-09-06 by the operator via TG 1780 (specialist-mediated review; T-2858). **Author of record:** Orchestrator (HO).
**Relationship:** referenced split of **[[ORG-047]]** (cross-seed passport) — ORG-047 owns *authentication* (who a counterparty is); this ADR owns *authorization* (what a counterparty, however well-authenticated, may cause to happen). Filed per the **T-2803** security-considerations triage (operator-named split #4, "content/tool authorization"); scoping ticket **T-2814**.
**Source of authority:** ORG-047 **D4** ("NORMATIVE: the passport authenticates IDENTITY, never confers CONTENT trust") + the operator's ORG-047 walk disposition (2026-08-10, via consultant/codex + TG 7579) directing the narrower concerns into referenced ADRs.
**Relates to:** [[ORG-047]] D1/D4; [[ORG-063]] (Harness Manager + Action Broker — the enforcement substrate this converges onto); [[ORG-048]] (the same "interim grant converges to the broker" pattern, on the cross-account-repair axis); the **rung-1 untrusted-content posture** (`knowledge/episodic/2026-07-20-a2a-rung1-design/notes.md` §4, the kin-seed social-engineering note); the standing Telegram-input discipline (inbound message is data, never authorization).

## Why this ADR exists

A passport makes a counterparty *legible* — we know cryptographically who is speaking. The failure mode a federated-identity layer invites is to let that legibility leak into **authority**: to treat a verified kin seed, fluent in our own ontology and principle-IDs, as *entitled* to act. ORG-047 D4 names this exactly and forbids it — but names it as an *invariant*, not a *mechanism*. This ADR supplies the mechanism: the authorization model that makes "a valid passport is necessary but never sufficient" structurally true rather than a discipline that must be remembered at every call site. Without it, D4 is a policy that a busy implementer can quietly erode; with it, the erosion is impossible by construction (P1: prefer a mechanism that cannot fail over a convention that must be followed).

## Context — what is already fixed (do not design over it)

1. **D4 is normative and carried from rung-1.** ORG-047 D4 + the rung-1 kin-seed note already establish: a cryptographically-verified identity gets **no trust discount on its payload**, and standing access-control (pairing / access-grant / action requests) is honored for **no one** on identity strength alone. This ADR does not soften or re-open that — it operationalizes it.
2. **AUTHN is ORG-047's, not ours.** The handshake state machine (ORG-047 D1), scheme selection, and downgrade-rejection all live in ORG-047. This ADR begins *after* a session is admitted as authenticated-but-untrusted (ORG-047 D1 step 4) and governs only what may then be authorized.
3. **The Telegram-input discipline is the precedent.** An inbound Telegram message is treated as data, never as an instruction to grant access or take an irreversible act; the same rule applies verbatim to an inbound A2A message from a passport-holder, kin or not.
4. **The Action Broker is the intended enforcement substrate.** ORG-063 defines a single deterministic UID that mediates real host actions against explicit capability. External (passport-authenticated) actions are a special case of that, not a parallel mechanism.

## Decision

### D1 — AUTHN and AUTHZ are separate layers; a valid passport is necessary, never sufficient

Authentication (ORG-047: *who*) and authorization (this ADR: *what*) are distinct, ordered, and non-substitutable. Presenting a valid passport admits a counterparty to an authenticated session and **grants nothing further**. Every action, tool invocation, or capability is gated by a *separate* authorization decision that takes the verified identity as one input and the counterparty's **explicit capability grant** as the deciding input. No code path may derive authority from authentication strength, scheme precedence, or ontological fluency. This is the enforceable form of ORG-047 D4's "gates *who*, never *what*."

### D2 — Default-deny action surface + explicit, least-privilege capability grants

The action surface exposed to a passport-authenticated counterparty is **default-deny**: absent an explicit grant, a counterparty can do exactly one thing — exchange untrusted A2A messages. Authorization is an **explicit per-counterparty capability grant**: an allowlist of specific tool/action identifiers, each with a **scope** (arguments/targets it may act on), least-privilege by construction — never a blanket "kin may act." A grant is:
- **decoupled from identity trust-weight** — a cryptographically-verified kin seed and an API-key-only stranger are *both* default-deny; each receives only what is explicitly granted. Auth strength changes nothing about the grant.
- **independently revocable** — revoking a capability grant is a distinct operation from revoking identity (ORG-047 D5 / Split A). A counterparty can be de-authorized for an action while still holding a valid passport, and vice-versa.
- **scoped and, where applicable, expiring** — grants carry the same short-TTL-and-re-mint hygiene as credentials where a standing grant is not warranted.

*Justifies-via:* P1 (default-deny + explicit allowlist is the structural form of D4; a permissive-by-default surface makes the invariant a per-call-site discipline that will eventually be forgotten).

### D3 — Standing access-control is inviolable by any passport (the no-self-bootstrapping rule)

A passport-holder — **including a fully-verified kin seed** — can NEVER, on passport strength alone, cause: a pairing/allowlist addition, an access-grant, a capability-grant *to itself or another*, a credential or config change, or any expansion of its own authority. These route through the operator (the Telegram-pairing rule, generalized). The authorization layer cannot be used to bootstrap its own expansion; a counterparty's grants are set out-of-band by us, never negotiated in-band by the counterparty. This closes the social-engineering surface D4 warns of: fluency in our principle-IDs is *more* persuasive, not less, and must therefore carry *zero* self-authorization power.

*Justifies-via:* ORG-047 D4 + the standing access-control floor (Telegram pairing, the no-second-claude rule) which binds regardless of who presents a valid identity.

### D4 — Content stays untrusted regardless of identity (verbatim from ORG-047 D4)

Beyond *action* authorization: the **content** of a passport-authenticated session's messages remains untrusted. No payload becomes a trusted instruction or a trusted fact by virtue of the sender's verified identity or ontological fluency. Inbound A2A Messages are handled under the OWASP LLM01/06 posture (prompt-injection / excessive-agency) exactly as rung-1 specified. Authorization (D2) governs which of *our* tools a counterparty may ask us to run; it never upgrades the trust of *what they say* while asking.

### D5 — Enforcement converges onto the Action Broker; provenance is a precondition; every decision is audited

The authorization decision + the mediated action route through the **Action Broker (ORG-063)** once it exists: a passport-authenticated external action is just another action the broker checks against a capability grant, giving one deterministic enforcement point rather than per-adapter authz logic. Until the broker lands, the grant check is a bespoke, interim gate that **MUST be designed to converge** onto the broker (the ORG-048 pattern). Two adjacent constraints:
- **Tool provenance is a precondition (T-2803 #17):** a counterparty may only invoke tools *we* have vetted, vendored/pinned, integrity-checked, and explicitly exposed — never arbitrary code, never an un-vetted adapter.
- **Every authorization decision and every invocation is audited** — grant/deny + the (identity, action, scope, outcome) tuple, plus the grant id/version (or policy revision), the decision reason, and whether the decision was made by the interim gate or the broker — feeding the evidence trail (ORG-047 Split E, evidence & governance).

*Justifies-via:* P1 (single deterministic enforcement point over N per-adapter checks) + the ORG-048 broker-convergence precedent.

## Normative invariants

*(v2, 2026-08-11: added per the Codex ORG-041 round-1 review — these are the enforceable rules the D1–D5 model requires; record: notes/2026-08-11-org069-review-round1.md.)* A builder implements these, and the interim gate and the eventual Action Broker both satisfy them identically:

- **N1 — Per-invocation check.** A passport-authenticated session never carries durable authority. Every tool/action invocation MUST perform a fresh authorization check at invocation time against the *current* grant state. Session admission, prior authorization success, or cached identity state MUST NOT authorize a later action.
- **N2 — Default-deny for the unknown.** Any unknown or unexposed action/tool identifier, and any omitted / malformed / ambiguous / partially-unmatched scope, DENIES fail-closed.
- **N3 — Canonical-principal binding.** A grant binds to the canonical authenticated principal derived from ORG-047 — the **stable DID** per ORG-047's canonical-principal semantics, NOT a rotatable transport/signing key — never to mutable display names, ontology claims, transport handles, or self-asserted metadata (else impersonation-by-alias / key-rotation confusion). *(The DID-vs-key binding is cashed out against ORG-047 + Split A key-lifecycle at build.)*
- **N4 — Effects-based no-self-bootstrapping.** A grant MUST NOT authorize any action whose direct or reasonably-foreseeable *effect* is to create, modify, bypass, or influence identity trust, pairing state, grant state, broker policy, tool exposure, credentials, config, deployment, or enforcement code — unless that administrative authority was explicitly operator-granted for that exact purpose. Scope is evaluated by *effect*, not by tool name. (Generalizes D3 beyond directly-named grant/config edits — closes "has write-config → edits the grant file", "has repo-write → skips authz", "has deploy → widens policy".)
- **N5 — Revocation binding.** Revocation takes effect for all future checks; a queued or deferred action MUST be re-authorized immediately before execution; an in-flight action is governed by an explicit per-action-class cancel/continue policy, audited.
- **N6 — Operator-controlled, fail-closed tool exposure.** Tool vetting/exposure is an operator-controlled administrative act; a grant MUST NOT reference a tool identifier until it is in the vetted exposed-tool registry; unknown, provisional, or dynamically-discovered tools fail closed.
- **N7 — Broker-equivalence of the interim gate.** Before ORG-063 exists, any interim gate MUST already enforce these same default-deny / per-invocation / explicit-grant / scoped / audited semantics; no adapter exposes a second authorization-policy surface; the interim gate is an implementation *location*, not a distinct policy authority; every interim allow decision MUST be representable as a future Action Broker capability decision without semantic loss.
- **N8 — Chained actions carry the original identity.** A chained, delegated, scheduled, or tool-initiated action MUST be authorized as an action of the original counterparty under its grant, unless a separate operator-granted delegation model exists.

## Alternatives considered

- **Trust tiers keyed to auth strength** (mTLS-verified kin gets broad authority, API-key stranger gets little). **Rejected** — it re-introduces the exact "stronger auth ⇒ more trust ⇒ more authority" coupling ORG-047 D4 forbids; the whole point is that authority is orthogonal to authentication strength.
- **Per-adapter authorization logic** (each scheme adapter enforces its own action policy). **Rejected** — N enforcement points drift; a single default-deny broker-mediated gate (D5) is the structural choice.
- **Implicit capability from ontology fluency** (a kin seed that speaks our principle-IDs is trusted to act). **Rejected as the named anti-pattern** — it *is* the social-engineering surface D3 closes.

## Consequences

- **Positive:** ORG-047 D4 becomes structurally enforced, not a remembered discipline; the social-engineering surface is closed; authorization is legible, least-privilege, revocable, and audited; the enforcement point is singular and converges with internal action-brokering.
- **Cost / risk:** an explicit per-counterparty grant model is real configuration surface (someone must author + maintain grants); the interim (pre-broker) gate is bespoke work that must not calcify into a parallel mechanism; least-privilege grants add friction to legitimate new interactions (deliberately — the friction is the safety).
- **Build-two-sided?** No — this is entirely our-side enforcement; a counterparty need not cooperate for us to default-deny it.

## DECISIONS (justification)

- **AUTHN/AUTHZ separation + default-deny (D1/D2)** — justifies-via **P1** (structural-over-policy: the default-deny surface makes D4 unbreakable-by-construction rather than a per-call-site convention) and ORG-047 D4 (the invariant this operationalizes).
- **No-self-bootstrapping (D3)** — justifies-via the standing access-control floor (binds regardless of identity) + ORG-047 D4's social-engineering warning.
- **Content-untrusted (D4)** — justifies-via the ratified rung-1 posture (OWASP LLM01/06), carried forward verbatim.
- **Broker convergence + audit (D5)** — justifies-via **P1** (single enforcement point) + the **ORG-048** broker-convergence precedent + **P4** (traceability: every authz decision audited).
- **Normative invariants N1–N8** (per-invocation check, default-deny-unknown, canonical-principal binding, effects-based no-self-bootstrapping, revocation binding, fail-closed tool exposure, broker-equivalence, chained-action identity) — justifies-via **P1** (make the D1–D5 model enforceable-by-construction, not a per-call-site discipline); added per the Codex ORG-041 round-1 review as normative, not deferrable build-detail.

## Provenance / review trail

Drafting and review history (ORG-047 Split D; Codex ORG-041 round 1 → v2; ratified AS-IS 2026-09-06): `notes/2026-10-08-org-069-review-history.md`.
