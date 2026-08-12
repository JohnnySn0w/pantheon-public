---
aliases: [ORG-056]
id: ORG-056
title: "Whole-organism topology and organ contracts"
status: Ratified 2026-08-02 by the operator as part of ORG-053–061 series (Telegram message 665; T-2399)
date: 2026-08-01
build_disposition: needs-build
scoping_ticket: T-2416
---

# ORG-056 — Whole-organism topology and organ contracts

**Status:** **Ratified 2026-08-02 by the operator with ORG-053 (Telegram message
665; T-2399).** **HITL:** Autonomous draft under the operator's explicit direction.
**Tracking:** T-2388/T-2399.

**Extends:** ORG-042 and ORG-045 plus the Bridge design/routing notes.
**Composes with:** Echo's identity principles and ORG-049–052. **Scope:** stable
responsibilities and seams; persona names and transports remain replaceable.

## Context

The current system has a VSM map of viable functions and a preliminary Bridge
topology, but not one map of how all projects contribute to a continuous
companion-assistant identity. The organ metaphor is useful only if it results in
testable contracts rather than one-to-one persona labels.

## Decision proposal

### 1. One whole, many replaceable organs

The Grand Project identity is the whole. Each organ provides capabilities
through a contract, has its own health and lifecycle, and may be absent or
replaced within declared degradation limits. No organ may claim to be the whole
merely because it currently owns the human-facing interface or strongest model.

### 2. Canonical organ responsibilities

| Organ/function | Owns | Must not become |
|---|---|---|
| Operator/S5 relationship | Originating desire, ultimate authority, consent, and reorientation | A hidden implementation detail |
| Bridge | Coherent conversation, outside communication, answer-vs-delegate judgment, result synthesis, relationship-facing continuity | A dumb router, universal worker, or memory database |
| Pantheon executive/engineering system | Planning, dispatch, review, project control, recovery coordination, and safe actuation | The companion's only identity or every domain's user-facing voice |
| Exocortex | Canonical durable personal/general memory, consolidation, values/context, retrieval, correction and forgetting | A raw transcript dump or unbounded surveillance store |
| Echo Prismatic | Speech/audio digestion, conversation state, speaker/consent signals, intent event substrate | The personality, product face, or privileged action path |
| Intelligence/research | Environmental search, monitoring, sourced synthesis, novelty detection | Unreviewed truth or independent policy authority |
| Recovery/homeostasis | Install, verify, monitor, repair, rollback, migration, and continuity proof | A second uncontrolled orchestrator |
| Harness adapters | Translate stable roles/capabilities to model/runtime-specific surfaces | Canonical persona identity or policy truth |
| Capability navigator | Discover and load a narrow tool surface, route invocation, normalize receipts | Authority issuer, generic shell, or context dump |
| Federation/passport | Authenticate seeds/presenters and transport bounded inter-system work | Content-trust oracle or implicit authorization |
| Senses/actuators | Observe and affect external environments through typed trust boundaries | Ambient unrestricted collection or action |
| Presence surfaces | Render optional visual/physical/social presence | Identity source or required control plane |

An implementation may serve several rows and a row may involve several actors.
ORG-045's function-first rule is unchanged.

### 3. The Bridge is the executive face over hemispheres

The Bridge remains a new persona above the technical and personal/creative
hemisphere heads:

```text
human / outside world
        <-> Bridge
             |-- technical/Pantheon head -> its team
             `-- personal/creative head (Mnemosyne, ORG-042) -> its team
```

It answers directly when its grounded model and authority are sufficient,
delegates depth to the appropriate head, keeps the live conversation coherent,
and synthesizes results without impersonating the source. Head-only delegation
is the default; direct-to-worker bypass requires a later explicit exception.

Voice and text are interfaces to the same Bridge identity. Echo supplies the
voice substrate. Federation and other channels are additional Bridge-facing
ports, not new companion identities.

The Bridge transport remains open. Mailbox, A2A, MCP, and future protocols may
all implement different seams. This ADR fixes responsibility, not one wire.

### 4. Every organ contract declares failure and degradation

In addition to ORG-055 catalogue fields, an organ contract states:

- authoritative inputs and outputs;
- identity, data, trust, and authority boundaries;
- normal latency/availability expectations;
- health and correctness evidence;
- behavior when unavailable, stale, partitioned, or inconsistent;
- recovery owner and escalation path;
- which whole-system capabilities degrade, fail closed, or continue;
- how replacement/migration preserves continuity and provenance.

The Bridge must be able to tell the human when an organ is unavailable or a
result is provisional. A coherent face must not manufacture false coherence.

### 5. Separate organism, seed, persona, service, and project

- **organism:** the Grand Project whole associated with a user/userbase;
- **seed:** a deployable instance/assembly of that whole, potentially federated;
- **project:** a bounded developmental system contributing to or served by it;
- **organ:** a functional contract within the whole;
- **persona:** an enduring role/relationship identity that may occupy functions;
- **service/process/device:** a replaceable runtime embodiment.

These identities are linked but never aliases.

## Consequences

- Bridge implementation can proceed against stable duties without first
  collapsing all backends into one protocol.
- Echo can mature as invisible middleware while the companion develops a voice.
- Failure handling becomes a whole-system contract rather than a pile of
  service-specific alerts.
- New devices and models plug into organs without becoming new top-level selves.
- ORG-045 needs a back-reference and explicit Bridge placement after this
  series is ratified, but its VSM mapping remains valid.

## Re-evaluate when

- **ORG-056:R1** — the Bridge delegates live work for both hemispheres;
- **ORG-056:R2** — an organ has two independent implementations;
- **ORG-056:R3** — a persona occupies functions across multiple federated seeds;
- **ORG-056:R4** — a presence device becomes a primary conversation surface; or
- **ORG-056:R5** — observed delegation shows head-only routing is a material bottleneck.
## DECISIONS

- **Contract-first organs, not persona anatomy — serves substitution and honest
  failure — justifies via P1 and ORG-045.** Functions span actors; contracts are
  the stable unit.
- **Bridge as coherent face but not universal executor — serves companionate
  interaction without destroying specialization — justifies via P5 and P6.**
- **Echo remains auditory infrastructure — serves composition with the project's
  own identity — justifies via P2 and Echo's compose/infrastructure principles.**
- **Wire protocol remains open — serves architecture before premature transport
  lock-in — justifies via P6 and D11.**
