---
aliases: [ORG-060]
id: ORG-060
title: "Sensory, actuation, and presence trust boundary"
status: Ratified 2026-08-02 by the operator as part of ORG-053–061 series (Telegram message 665; T-2399)
date: 2026-08-01
build_disposition: needs-build
scoping_ticket: T-2420
---

# ORG-060 — Sensory, actuation, and presence trust boundary

**Status:** **Ratified 2026-08-02 by the operator with ORG-053 (Telegram message
665; T-2399).** **HITL:** Autonomous draft under the operator's explicit direction.
**Tracking:** T-2388/T-2399.

**Composes with:** Echo's project principles and consent/event decisions,
ORG-047/050/051/052, ORG-055/059, and the Bridge topology. **Scope:** trust and
composition rules for physical-world observation, action, and embodiment; no
device or IoT platform is selected here.

## Context

The intended organism may hear through fixed home microphones, see through
cameras or a following drone, sense through IoT devices, act through home and
computer controls, and appear through AR glasses, desktop pets, monicards,
badges, or later devices. These surfaces create presence and usefulness, but
also continuous privacy, safety, bystander, and authority risks. A platform such
as Home Assistant or ThingsBoard cannot be the architectural center.

## Decision proposal

### 1. Model observations and effects as typed capabilities

Every sensor and actuator is an ORG-055 component that exposes ORG-059
capabilities. The system interacts through typed contracts rather than direct
device-specific assumptions. Home Assistant, ThingsBoard, robotics frameworks,
and vendor clouds are adapters that may provide those contracts.

### 2. Require a sensory envelope

Every observation carries or resolves:

- sensor/capture-context identity and physical/logical location;
- observed time, quality, uncertainty, and calibration state;
- subject/owner/bystander scope where known;
- consent and lawful/household policy basis;
- raw/derived classification and transformation provenance;
- locality and whether data left the device/seed;
- retention, redaction, access, and deletion class;
- links to the exact model/algorithm versions that derived meaning.

Missing or stale provenance lowers the observation's trust; it must not be
silently upgraded by model confidence. Sensitive raw media remains local by
default where a viable local path exists.

### 3. Require an actuation envelope

Every physical or externally consequential action declares:

- target, requested effect, preconditions, and expected observable result;
- requesting persona, purpose, native authority, and approval class;
- blast radius, reversibility, cost, privacy impact, and bystander impact;
- timeout, idempotency/retry behavior, safe failure, and cancellation;
- verification signal, rollback/recovery path, and durable receipt.

Unknown target state, ambiguous authority, incompatible device state, or absent
verification fails closed for consequential action. Low-risk reversible actions
may be autonomous under explicit policy; higher-risk actions plan first and use
separate approval under ORG-052-like authority separation.

### 4. Stage ambient sensing deliberately

Initial Echo deployment assumes microphones with known static geometry.
Conversational management and the Bridge voice loop take priority. Dynamic
microphone calibration and onboarding are deferred until the static system has
measured evidence and a clear need.

Continuous cameras, mobile drones, biometric inference, and cross-room tracking
each require a dedicated threat/consent review and supervised first fire. A
device's availability does not authorize ambient collection.

### 5. Presence is an optional projection

Presence surfaces render Bridge/organism state and may accept interaction. They
must:

- identify which organism/seed/persona is represented;
- show degraded, disconnected, or provisional state rather than faking life;
- expose privacy/recording/action state appropriate to the surface;
- avoid becoming the canonical memory, identity, or authority store;
- remain replaceable and optional, with text/voice/control fallbacks;
- use a bounded presentation contract so personality and state remain coherent
  across desktop pets, AR, cards, badges, drones, and future forms.

The Bridge owns the relationship-facing presentation. Echo and device adapters
remain infrastructure.

### 6. Preserve the intimacy/control tension

Companionate presence benefits from continuity, proactivity, and awareness.
Trust requires legibility, quiet modes, consent, correction, and meaningful
off/away boundaries. The system records this tension and evaluates it with
users; it does not resolve it once for every home or person.

## Consequences

- IoT platform selection can change without changing the organism's action and
  observation contracts.
- Ambient data becomes traceable and governable before it reaches memory or
  models.
- Presence can proliferate across devices without fragmenting identity.
- Safety work scales with effect class instead of treating every tool as either
  harmless or forbidden.
- Static microphone geometry narrows the first Echo/Bridge deployment and
  postpones premature calibration/onboarding complexity.

## Re-evaluate when

- **ORG-060:R1** — the first always-on microphone deployment completes conversational trials;
- **ORG-060:R2** — a camera, drone, biometric, or mobile sensor is proposed for continuous use;
- **ORG-060:R3** — the system performs a non-trivial physical act autonomously;
- **ORG-060:R4** — more than one presence surface is live simultaneously;
- **ORG-060:R5** — local processing is materially worse than a cloud option; or
- **ORG-060:R6** — users/bystanders report that presence or sensing is unclear or intrusive.
## DECISIONS

- **Platform-neutral typed capabilities — serves long-lived senses/hands —
  justifies via P1 and Echo's compose principle.**
- **Provenance and consent travel with observations — serves trustworthy memory
  and privacy — justifies via P1, P4, and ORG-050.**
- **Effect-class actuation with separate approval — serves useful autonomy
  without confused authority — justifies via ORG-052 and P6.**
- **Static microphone geometry first — serves the conversational north star
  while keeping deferred complexity explicit — justifies via P6 and D12.**
- **Presence projects but does not own identity — serves coherent optional
  embodiment — justifies via ORG-057.**
