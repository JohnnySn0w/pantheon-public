---
aliases: [ORG-053]
id: ORG-053
title: "Grand Project architecture program — one developing companion-assistant whole"
status: Ratified 2026-08-02 by the operator (Telegram message 665; T-2399)
date: 2026-08-01
build_disposition: needs-build
scoping_ticket: T-2413
---

# ORG-053 — Grand Project architecture program

**Status:** **Ratified 2026-08-02 by the operator (Telegram message 665; T-2399).**
**Amended 2026-08-07** under the operator's direction (Telegram 7266/7267): added §6, the universal system-lifecycle contract + seed-provisioning posture (install / modify-update / configure / health-monitor / remove, on every organ). operator-directed; carries his authority as ratifier — flag for formal re-ratification confirmation at his convenience.
**HITL:** Autonomous draft under the operator's explicit direction (Telegram
492/506/511); ratified by the operator because this establishes the root goal and
reorients existing projects. **Tracking:** T-2388/T-2399.

**Source record:**
[`notes/2026-08-01-grand-project-architecture-capture.md`](../notes/2026-08-01-grand-project-architecture-capture.md).
That note preserves conversation provenance, external sources, current-system
references, and open tensions. This ADR records the proposed decision without
transcluding the full capture (D10).

**Extends:** ORG-034 (goal architecture), ORG-042/045 (current topology), and
the project north stars in `rulebook/goals.md`. **Does not supersede:** the VSM,
Echo's project principles, ORG-047 federation identity, or ORG-049–052
installation/security/host-control decisions.

## Context

Pantheon has accumulated strong organs—an orchestration system, voice
middleware, persona roles, memory work, federation identity, installation,
recovery, and capability control—but no canonical artifact says what living
whole they are becoming. Each project can justify locally while their joint
purpose remains implicit. That makes precise work possible but leaves the broad
angle vulnerable to drift.

The operator supplied the missing goal: bootstrap an attempt at artificial life in
the social form of a per-user companion and assistant. It should grow through
interaction, search, memory, reflection, and safe action; remain coherent while
its models, harnesses, devices, and services change; and develop a relationship
shaped by its user without becoming an opaque or uncontrollable authority.

## Decision proposal

### 1. Establish the Grand Project as this instance's root north star

Adopt this root north star:

> Bootstrap a living digital companion and assistant: one coherent,
> per-user whole that remembers, learns, communicates, and acts through
> replaceable organs while preserving identity, trust, and human authority.

For the operator's Pantheon instance, the Grand Project is a **project-of-projects**
and the architectural identity of the whole. It is developmental rather than
terminal: a stage can complete, but the whole continues to grow, reorient,
maintain itself, and deepen its relationship.

This is an instance-level goal decision, not a framework installation
requirement. Pantheon must also support an instance with no shared root north
star and an instance with several independent top-level projects. The generic
project and installer substrates therefore model an optional root; they do not
hard-code the Grand Project or require every user's work to compose into one
whole.

Existing project north stars become nested organ-project teloi where they serve
this root. ORG-034's two artifact types remain unchanged: north stars may nest
recursively, while arc-intents remain the operational layer beneath a project
north star. This adds a root, not a third kind of work artifact.

### 2. Treat projects as organs when they join the whole

A project becomes a Grand Project organ only when it declares:

- its telos and upward link to the root north star;
- the capabilities it provides and requires;
- its boundary, steward, lifecycle, and operational obligations;
- its trust, privacy, authority, and data implications;
- how its health and developmental contribution are evaluated;
- how it can be replaced, split, merged, retired, or temporarily absent
  without pretending the whole has vanished.

Not every project the operator undertakes must be an organ. External projects may be
served by Pantheon without being constitutive of the companion. Membership must
be explicit rather than reverse-engineered from repository proximity.

### 3. Adopt the organism map as architectural orientation

The current direction is:

- **Bridge:** coherent conversational and external face;
- **Pantheon:** executive and engineering intelligence;
- **Exocortex:** durable memory and learning;
- **Echo Prismatic:** auditory digestion and conversation substrate;
- **harness adapters:** replaceable reasoning-substrate interfaces;
- **capability navigation:** context-bounded tool nervous system;
- **passport/federation:** authenticated seed-to-seed communication;
- **installer, recovery, security, and host broker:** birth, homeostasis,
  repair, authority, and safe change;
- **sensors and actuators:** senses and hands;
- **presence surfaces:** optional embodiment.

This is not persona-to-organ bijection. ORG-045 remains the normative
function-first mapping of VSM functions across actors and artifacts. That
mapping describes functional placement, not a claim that every assigned
function is fully wired or continuously active. The organ orientation asks
how components contribute to one identity; the VSM asks how that whole remains
viable and controlled.

### 4. Preserve development as a feedback loop

The common Will directs a loop:

```text
desire / north star
    -> project shape
    -> architecture and implementation
    -> observed system and relationship
    -> memory and evaluation
    -> reorientation of desire and design
    -> next concrete change
```

During plastic **infolding**, implementation may reveal better possibilities
and reshape the project. During reliable **infilling**, established contracts
make missing pieces obvious. Each organ records which mode it is in and names
the evidence or trigger for reorientation. Review and safety still bind; agent
co-creation is judgment inside authority, not exemption from it.

### 5. Make the child ADRs one ratification surface

ORG-054–061 are the normative decomposition of this parent:

| ADR | Decision surface |
|---|---|
| ORG-054 | Project ontology and upward justification |
| ORG-055 | Repository realization and compatibility catalogue |
| ORG-056 | Whole-organism topology and organ contracts |
| ORG-057 | Continuity and harness-independent selfhood |
| ORG-058 | Memory and learning lifecycle |
| ORG-059 | Capability navigation and MCP scaling |
| ORG-060 | Sensory, actuation, and presence trust boundary |
| ORG-061 | Developmental sequence, evidence, and reorientation |

The series is reviewed and ratified as one goal-level architecture surface.
Child implementation can then proceed independently behind its own plan,
review, and deployment gates.

### 6. Every organ carries a full lifecycle contract (amendment — the operator, TG 7266/7267, 2026-08-07)

All design work in the whole is to be conscious, from the outset, of two things at once: **the grand goal** (this ADR's root north star) **and the full lifecycle every system necessarily has.** No organ or subsystem is designed as a static artifact; each is a managed entity across its whole life, and must be designed from the start for all of:

- **Installable** — brought into existence on a host reproducibly. This is the **seed-provisioning posture**: seeding a fresh box with Pantheon stands the organ up; install is a first-class, repeatable, **convergent** operation (repeated bringup produces one consistent state), not a one-off — see ORG-049.
- **Modifiable / updatable** — changed in place through a defined update path, without a rebuild-from-scratch.
- **Configurable** — behavior declared, not hardcoded (e.g. the Scheduler's declarative `schedules/` spec, ORG-064; the identity manifest `instance.yaml`).
- **Health-monitored** — externally legible ("am I healthy, and why") on one uniform surface (ORG-063 §7 health system; the ORG-063 §4b restart-fixable / stateless invariants are the modular subset of this).
- **Removable** — cleanly torn down and its footprint reclaimed, without orphaning state or leaving the whole inconsistent.

This is the **cradle-to-grave contract**: birth (install) → homeostasis (configure, health-monitor, update) → death (remove) — the lifecycle §3's organism map already gestures at ("installer, recovery, security, and host broker: birth, homeostasis, repair, authority, and safe change"), now made an explicit obligation on **every** organ, not only the installer organ. A design that serves the grand goal but cannot be installed reproducibly, updated, configured, health-reported, or removed is **not done**. The seed-provisioning posture includes host chores (they must be provisioned on a fresh box too — ORG-064 §Open Q3), so the declarative substrate (manifest + schedules + install contract) is the single source of truth a clean box converges from.

## Boundaries

- This ADR does not assert consciousness, sentience, legal personhood, or a
  scientific definition of life. “Artificial life” is the engineering and
  relational north star being attempted, not a completed claim.
- The companion is not a single LLM, model vendor, daemon, account, session,
  repository, or device.
- Echo remains infrastructure rather than the companion personality.
- The Bridge direction is recorded but not thereby built or assigned a final
  transport.
- No sensor, actuator, public federation exposure, or new authority is enabled
  by ratifying an architecture record.
- Existing Proposed ADRs, especially ORG-046 and ORG-047, do not silently become
  ratified through reference here.
- Other Pantheon instances are not required to adopt the Grand Project, any
  root north star, or a single composed project hierarchy.

## Consequences

- This instance's goals registry gains one Grand Project root entry after
  ratification and each participating project declares its parent/telos
  relationship. Generic installer and project schemas keep that root optional.
- New projects and components must say whether they are organs, supporting
  infrastructure, or external work served by the system.
- Architecture reviews can test local decisions against the intended whole,
  while preserving project-specific principles and contracts.
- The Grand Project can survive replacement of any one model, harness,
  interface, sensor, presence surface, or seed host.
- “Living” creates real maintenance obligations: continuity, consent, memory
  correction, security, observation, repair, and developmental evaluation are
  core architecture rather than polish.

## Re-evaluate when

- **ORG-053:R1** — the first Bridge runs as the operator's primary interface;
- **ORG-053:R2** — a clean seed can be installed and restore a persona through two harnesses;
- **ORG-053:R3** — the Exocortex becomes a live cross-conversation memory source;
- **ORG-053:R4** — seed federation carries authenticated work;
- **ORG-053:R5** — physical sensing or actuation becomes continuous;
- **ORG-053:R6** — the organism map no longer explains a major component; or
- **ORG-053:R7** — the operator changes the relational north star.
## DECISIONS

- **One chosen root for this instance, optional at framework level — serves
  coherent development without imposing the operator's telos on other users —
  justifies via P3 and ORG-034.** A project-of-projects gives every participating
  organ an upward end without making one implementation the whole; rootless and
  independently multi-project instances remain valid.
- **Organ orientation composes with VSM rather than replacing it — serves
  architectural continuity — justifies via P6 and ORG-045.** The two views
  answer different questions and preserve the productive tension between
  identity and control.
- **Development includes implementation feedback — serves a genuinely living
  trajectory — justifies via P6 and D12.** Concrete evidence may reshape
  direction at explicit gates; it does not bypass authority or review.
- **Goal-level direction ratified by the operator — serves legitimate source
  authority — justifies via D3.** The direction changes what existing projects
  are for, so it required explicit ratification rather than self-adoption as
  mere methodology.