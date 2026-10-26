---
aliases: [ORG-059]
id: ORG-059
title: "Capability nervous system and context-bounded MCP navigation"
status: Ratified 2026-08-02 by the operator as part of ORG-053–061 series (Telegram message 665; T-2399)
date: 2026-08-01
build_disposition: needs-build
scoping_ticket: T-2419
# elements: — ORG-065 machine index (T-3205 Tranche B whole-ADR policy
# call: the nine T-2413..T-2421 "elaborate ORG-0NN into a
# program/implementation plan" tickets each plan a WHOLE ADR, not one
# numbered section — see T-3205 DECISIONS for the edge-per-section-vs-
# whole-ADR-element reasoning). E0 is a single reserved whole-document
# element (zero cannot collide with any native numbering, which starts
# at 1, and sorts first) — the obligation genuinely is "plan the entire
# ADR". A future per-section elementization of this ADR (if ever
# performed) can add its own native-numbered elements (E1, E2, ...)
# without touching E0. Added by HO; no obligation content changed.
elements:
  - {id: E0, text: "ADR as a whole: Capability nervous system and context-bounded MCP navigation — whole-document element for plan/elaboration tickets (ORG-065 pass-2 policy, T-3205)"}
---

# ORG-059 — Capability nervous system and context-bounded MCP navigation

**Status:** **Ratified 2026-08-02 by the operator with ORG-053 (Telegram message
665; T-2399).** **HITL:** Autonomous draft under the operator's explicit direction.
**Tracking:** T-2388/T-2399.

**Extends:** ORG-052. **Composes with:** ORG-047/050/051/055 and existing MCP,
mailbox, and A2A surfaces. **Scope:** scalable discovery and invocation; exact
service/protocol implementation follows in a reviewed build plan.

## Context

As tools, services, sensors, and federated seeds accumulate, exposing every MCP
server and schema to every model turn will consume context, confuse selection,
and expand authority accidentally. The number of capabilities should scale far
beyond what one harness can preload. The abstraction must remain easy for
agents to use or they will bypass it, while never turning convenience into a
generic privilege grant.

## Decision proposal

### 1. Add a capability navigator between intent and concrete tools

The navigator is a logical organ with a small, stable interface:

Its operations are search (bounded candidates), describe (a contract slice), plan (an effect summary or handle), invoke (a typed result and receipt), status, and cancel. The signature sketch is in `notes/2026-10-08-org-059-navigator-interface-relocated.md`.

Harnesses may access this through MCP because most support it natively. The
underlying capability may be MCP, D-Bus, A2A, mailbox, HTTP, local library, or a
device protocol. Protocol is an adapter property, not the capability identity.

### 2. Derive discovery from the compatibility catalogue

ORG-055 component records provide the base graph:

- capability identity and typed contract;
- provider component, health, version, and deployment locus;
- prerequisites and compatible feature sets;
- authority, privacy, data, hardware, network, cost, and upkeep implications;
- latency, reversibility, blast radius, and approval class where relevant.

Search returns only candidates compatible with the current seed, persona role,
purpose, trust zone, and constraints. Ranking and explanations are auditable.
Capability descriptions are loaded just in time and capped to the requested
detail rather than globally injected.

### 3. Separate discovery, planning, authority, and execution

Discovery reveals that a capability exists only to the extent the caller may
know. Planning validates inputs and produces an exact effect description.
Authority is evaluated by the native authority system. Execution happens only
after those gates.

`bounded_purpose` is descriptive request metadata, never an authority claim.
Caller uid, persona, role, instance, capability grants, credentials, approver,
policy revision, and authority source are derived server-side from the
authenticated substrate and server-owned registry/policy. A public caller
cannot supply or override them. Providers re-derive the identity/authority they
need at their own native boundary rather than trusting navigator assertions.

The navigator:

- never mints approval, credentials, role membership, or content trust;
- never exposes a generic shell/sudo/file-write escape;
- never treats discovery visibility as invocation authority;
- forwards authenticated subject and purpose without impersonation;
- uses opaque, subject-bound handles for deferred or privileged operations;
- returns native denial and provenance without silently trying a stronger actor.

ORG-052 remains the host-operation boundary: its MCP adapter, broker policy,
operator approval surface, and D-Bus authority are not bypassed. ORG-047 remains
identity rather than content/authority trust. ORG-051 remains secret custody.

### 4. Support collaboration by routing, not authority laundering

When one persona lacks a capability but another role legitimately owns it, the
navigator may propose a **delegation route**:

```text
requesting persona
  -> durable typed request with intent and constraints
  -> authorized persona/head accepts under its own role
  -> native capability planning/approval/execution
  -> signed/audited receipt returns to requester
```

The receiving role evaluates and owns the operation. The requester cannot
cause it merely by naming the stronger persona. Mailbox/tickets or A2A may carry
the request; they are delivery substrates, while the capability and authority
contracts remain canonical.

### 5. Use bounded context and normalized receipts

The model sees, per step:

- a small candidate list with why each matched;
- one selected contract slice and declared implications;
- an exact plan/effect summary before consequential invocation;
- a typed result or durable handle;
- a normalized receipt pointing to provider-native evidence.

Large schemas, logs, binary data, and unrelated servers stay outside context.
Receipts include capability/provider versions, subject, plan/effect digest,
authority provenance, timestamps, status, redacted result summary, and evidence
references. Secrets and unauthorized data never enter the receipt.

### 6. Make health and lifecycle part of selection

Unavailable, stale, incompatible, degraded, or unaudited providers are not
silently selected. Fallback is allowed only among providers declared equivalent
for the requested contract and trust class; a changed provider is visible in
the plan and receipt. The navigator itself must degrade to direct, explicitly
configured essential capabilities for bootstrap/recovery rather than becoming a
single point that prevents repair.

## Consequences

- Capability count can grow without linearly growing every model context.
- MCP remains a convenient harness-native front while the system can compose
  non-MCP backends.
- Personas can collaborate across authority boundaries without sharing secrets
  or laundering privilege.
- The ORG-055 catalogue becomes operationally load-bearing and needs health,
  schema, and policy validation.
- A future implementation needs adversarial evaluation for prompt injection in
  catalogue metadata, ambiguous tool selection, stale providers, confused
  deputy attacks, and receipt leakage.

## Re-evaluate when

- **ORG-059:R1** — one seed exposes more than fifty capabilities;
- **ORG-059:R2** — two providers claim semantic equivalence but produce different effects;
- **ORG-059:R3** — a harness cannot consume the navigator's MCP surface;
- **ORG-059:R4** — delegation becomes the dominant route and overwhelms heads;
- **ORG-059:R5** — discovery latency or catalogue freshness harms conversation; or
- **ORG-059:R6** — the navigator itself is unavailable during recovery.
## DECISIONS

- **Small navigator surface over global MCP registration — serves scalable
  agency within finite context — justifies via D10 and P1.**
- **Capability identity independent of protocol — serves substitution and
  heterogeneous senses/actuators — justifies via P1 and P6.**
- **Native authority remains authoritative — serves foolproof convenience
  without privilege collapse — justifies via ORG-050–052.**
- **Delegation routes work, not credentials — serves collaboration with audit —
  justifies via P5 and P4.**
- **Bootstrap slice survives navigator failure — serves recoverability —
  justifies via P1 and ORG-052.**
