---
aliases: [ORG-057]
id: ORG-057
title: "Continuity, identity, and harness-independent selfhood"
status: Ratified 2026-08-02 by the operator as part of ORG-053–061 series (Telegram message 665; T-2399)
date: 2026-08-01
build_disposition: needs-build
scoping_ticket: T-2417
---

# ORG-057 — Continuity and harness-independent selfhood

**Status:** **Ratified 2026-08-02 by the operator with ORG-053 (Telegram message
665; T-2399).** **HITL:** Autonomous draft under the operator's explicit direction.
**Tracking:** T-2388/T-2399.

**Extends:** ORG-028/031/035/049/050. **Scope:** what persists across sessions,
harnesses, hosts, and models. Long-term memory semantics are ORG-058.

## Context

Recent harness lockouts exposed a category error: a persona can be operationally
unavailable even while its system services are healthy, and recovering a user
account or old chat does not necessarily recover the role, commitments,
authority, and memory needed to continue. The Grand Project cannot identify an
enduring companion—or any persona—with one vendor session.

## Decision proposal

### 1. Identity is canonical state plus relationships, never a session

The organism and its personas persist through explicit, versioned records. A
session, model process, harness, Unix account, Telegram bot, voice, or device is
an embodiment that can be attached or replaced.

Each persona continuity bundle resolves these logical records by reference:

```yaml
persona_id: stable-logical-identity
organism_id: owning-whole
role_contract: canonical-role-and-boundaries
identity_canon: persona-description-and-relationship-position
values_and_goals: canonical-references
capability_profile: declared-provides-requires
authority_profile: role-and-credential-references-without-secrets
memory_roots: working-episodic-semantic-procedural-relational-references
commitments: active-tickets-promises-waits-and-escalations
runtime_binding: current-harness-model-account-and-interface
provenance: versions-authors-ratifications-migrations
```

The bundle is a resolver input, not a monolithic context prompt. Each canonical
record keeps its own authority and retention rules; the harness loads the
minimum view needed for its role and current work.

### 2. Define hydration, checkpoint, handoff, and verification

- **hydrate:** resolve the bundle, verify signatures/versions/authority, load
  role and current-work context, then declare the session ready;
- **checkpoint:** persist commitments, decisions, unresolved waits, and memory
  candidates at a report boundary without treating hidden model state as data;
- **handoff:** close or transfer ownership with exact current state and expected
  next action;
- **verify continuity:** require the new embodiment to identify itself, its role,
  current commitments, authority limits, and last canonical checkpoint, then
  perform a bounded functional smoke.

Hydration failure is explicit degraded/unavailable state. A blank but speaking
model must not masquerade as the continuing persona.

### 3. Make harness switching a reversible binding change

A harness adapter maps the stable continuity bundle and organ contract into the
harness's native files, MCP/tool configuration, sandbox, lifecycle, and message
surfaces. Switching harnesses:

1. plans the new binding without mutating canonical identity;
2. verifies capability and authority compatibility;
3. checkpoints the old embodiment;
4. starts the new one in a non-authoritative verification mode;
5. proves continuity and communication;
6. transfers the active binding atomically or rolls back.

Two embodiments may overlap only for verification with one declared active
authority holder. Split-brain action is refused.

### 4. Retire session resurrection as architecture

The former **veneration** concept—loading an old model session into a harness to
interrogate it—is permanently shelved. Transcripts and tool records may be read
as provenance or episodic evidence, but hidden model state is not canonical,
portable, auditable, or a safe source of authority.

Embodiment-local native or latent memory may support continuity behavior, but
it cannot replace the portable, inspectable, provenance-bearing continuity
bundle or become continuity authority merely by persisting inside a model.
ORG-058 carries the evidence and evaluation posture for such memory substrates.

Mailbox coordination is unrelated. Any former coupling through a Unix group
name is migration residue and must not preserve the retired concept.

### 5. Distinguish whole continuity from persona continuity

The organism continues when an organ/persona is temporarily unavailable if its
absence and degradation are visible and core identity/memory/authority remain
intact. A persona continues across embodiments when its logical bundle and
relationship role remain intact. Neither claim implies perfect behavioral
identity across models; observed variance is recorded and evaluated.

## Consequences

- Persona recovery becomes reproducible state hydration, not archaeological
  reconstruction from home directories and chats.
- Installers can verify a functional identity layer after laying down runtime
  files.
- Harness/model experiments become safer because rollback leaves canonical
  identity untouched.
- Continuity bundles require strict provenance, privacy, schema evolution, and
  conflict rules, but avoid duplicating the underlying canonical records.
- Behavioral differences across models become measurable compatibility data.

## Re-evaluate when

- **ORG-057:R1** — Recovery is restored through a second harness from the same continuity bundle;
- **ORG-057:R2** — two concurrent interfaces need the same persona identity;
- **ORG-057:R3** — a canonical identity record is too sensitive to load into a harness context;
- **ORG-057:R4** — model variance makes a nominally compatible switch relationally unacceptable; or
- **ORG-057:R5** — a seed migration crosses administrative trust domains.
## DECISIONS

- **Canonical logical identity over session continuity — serves an enduring
  companion across replaceable substrates — justifies via P1 and P4.** Sessions
  are neither portable nor authoritative.
- **Bundle references rather than copied mega-prompts — serves context economy
  and independent authority — justifies via D10 and ORG-050.**
- **One active binding during switch — serves safe reversible hot-swap —
  justifies via P1 and P6.** Verification overlap is allowed; split-brain action
  is not.
- **Retire veneration explicitly — serves architectural clarity — justifies via
  P4.** Historical evidence survives without preserving a misleading mechanism.
