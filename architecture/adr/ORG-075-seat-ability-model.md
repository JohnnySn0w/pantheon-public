---
adr: ORG-075
title: "Sub-orchestrator seat ability model: context + access, not competence"
status: "Adopted 2026-08-12 — HO-adopted autonomously per the auto-approve ADR-tier policy (rulebook/policies/trust-posture.md §What-HO-auto-approves, 2026-06-11 amendment). The model is the operator's OWN articulation from the 2026-08-11 TG design conversation (msgs 7683–7692); he granted 'take care of things as they come' (TG 7700, 2026-08-12) as the general go-ahead. Amendable if the operator wants changes. HO-drafted 2026-08-11. Extends ORG-043 (seat definition) with the ability MODEL — how to reason about what a seat can do, and what growing one into a full HO you can hand off to actually means. Not yet built; scoping/build under T-2882."
build_disposition: needs-build
scoping_ticket: T-2882
date: 2026-08-11
# elements: — ORG-065 machine index of the "Decision" section (T-2946
# Tranche A, pair 2/8). E<n> == D<n>, 1:1; text opens with the native
# decision id verbatim — the ADR body's D<n> subsections remain
# authoritative. Added by HO; no obligation content changed.
elements:
  - {id: E1, text: "D1: ability = model-capability conditioned on context + access, not a competence tier"}
  - {id: E2, text: "D2: context is the provisionable variable — an information gap closed by seeding pins/SOPs/brief, not training-up"}
  - {id: E3, text: "D3: access is gated by the operation's blast-radius/reversibility, not the actor — least-privilege, answered identically for any actor incl. HO"}
  - {id: E4, text: "D4: handoff protocol is task-context transfer — brief + memories + access, not a promotion; concrete protocol is a T-2882 build item"}
  - {id: E5, text: "D5: growing a seat into a full HO = union of D2-D4 (provision context, grant access per operation-risk, run handoff protocol) — not a competence ladder"}
---

# ORG-075 — Sub-orchestrator seat ability model: context + access, not competence

## Why this ADR exists

ORG-043 established the sub-orchestrator seats (Mason, Artisan) as subordinate daemon reports under the HO. It did not model their **ability** — what a seat can be trusted to do, and what it means to grow one into a full HO the operator can hand a project off to.

In practice the HO drifted into a wrong-shaped mental model: treating the seats like junior hires who *earn* capability by *proving out*. The operator flagged this directly (TG 2026-08-11): the seats run the **same model** as the HO, so a competence-based trust ladder is the wrong frame. This ADR replaces it with the correct model and names the three mechanisms that follow.

## Context — what is already fixed (do not design over it)

- **ORG-043**: seats are subordinate reports; comms route the operator → Orchestrator → seat; no operator-facing channel; full ticket/ledger actors; each a viable system one recursion level down (VSM S1); an N+ horizontally-scalable class.
- **The load-bearing new fact** (the operator, TG): a seat runs the SAME model as the HO (same Opus tier). Its only standing differences are accumulated **memory/context** and granted **access** — not raw capability.
- **Why the seats exist**: throughput. One HO can hold only so many concurrent project-orchestrators + implementer/reviewer pairs. A seat is another whole tree — an HO you hand off to (the operator's original intent, TG).

## Decision

### D1 — Ability is not competence-gated; it is model-capability conditioned on context + access

A seat's ability to do a task = the model's capability (equal to the HO's) **conditioned on** two variables it may lack, neither innate:

- **Context** — the memories, SOPs, and task-specific brief available to it.
- **Access** — the credentials and repo/write permissions it holds.

Given equal context + access, a seat performs a task as well as the HO would. There is no junior tier of competence to grow through; the growable variables are context and access.

### D2 — Context is the provisionable variable (an information problem, not a capability one)

A seat holds fewer memory-pins + less session history than the HO. This is an **information** gap, closed by provisioning — not by "training up":

- Relevant memories are shared/seeded to the seat.
- The HO playbook / role SOPs the task needs are loaded.
- The task arrives as a full dispatch brief (the HO-altitude analog of a worker brief).

On the context axis, "growing a seat into an HO-proxy" means making sure it **holds** the context an HO task needs — not waiting for it to become more capable.

### D3 — Access is gated by the OPERATION's risk, not by the actor

Which credentials / write-access a seat holds is a security + blast-radius decision about the **operation**, answered identically for any actor including the HO:

- **Least-privilege**: hold the credential the current work needs, no more.
- **High-blast-radius / low-reversibility operations** — e.g. pushing scrubbed content to public or external mirrors, a leak-surface event — stay controlled *regardless of who* would perform them; the control is the operation's audit + reversibility design, not the actor's "trust level."
- The question is never "has this seat earned mirror-write" — it is "do we want that credential + that blast radius living on that account," a posture decision the operator makes once, per operation-class.

### D4 — The handoff protocol is task-context transfer

Handing a seat an HO-level task = transferring the task context (as the HO already does for workers) + ensuring the seat holds the memories + access the task needs. The handoff is a provisioning + briefing act, not a promotion. The concrete protocol — brief shape, context bundle, access grant, report-back contract — is a build item under T-2882.

### D5 — What "growing a seat into a full HO" concretely means

The union of D2–D4, not a competence ladder:

1. **Provision the context** (memories, SOPs, playbook) for the HO role.
2. **Grant access** per the operation-risk policy (D3) — high-blast-radius credentials come last because of **blast radius**, not because the seat must earn them.
3. **Run the handoff protocol** (D4) for real HO-level tasks and let the seat drive with its own worker tree.

The only thing that genuinely accrues over time is the operator's (and HO's) **calibration** about handing off high-blast-radius work with less supervision — and even that is an audit-and-reversibility control-design question, not a measure of the seat's ability.

## Alternatives considered

- **Trust/competence ladder (rejected).** The HO's initial framing: seats earn HO capabilities by proving out competence across rungs. Rejected because it mismodels the architecture — same model = same competence; the real variables are context + access, both provisionable/decidable, not earned-competence. The operator flagged it as wrong-shaped (TG). Retained only as the D5 residue: operator calibration about *unsupervised* high-blast-radius handoff genuinely accrues — but that is a control-design detail, not the ability model.

## Consequences

- The speculative seat read-only PAT (T-2010 / T-1995) is retired as the wrong instrument: it patched a "blind to 4 repos" symptom with a speculative grant. Under D3, access follows the operation, so no credential is minted until a task needs it — and its read-vs-write scope is then defined by that task.
- **Context-provisioning** becomes a first-class mechanism to build (memory-sharing/seeding + the HO playbook a seat loads) — T-2882.
- **Access-policy** becomes an operation-risk-indexed posture (which credential-class, gated by blast-radius + reversibility) — T-2882.
- A **handoff protocol** (HO-altitude dispatch) becomes a defined procedure — T-2882.
- ORG-043 stands unchanged; this ADR is its ability-model layer.

## DECISIONS (justification)

- **Model ability as capability-conditioned-on-(context, access), not a competence tier** — serves the seats-as-HO-throughput intent — justifies via the same-model fact (the operator TG) + P2 (principles-first: name the real variable). The prior ladder collapsed context + access into a fake competence axis.
- **Gate access by operation-risk, not actor** — serves keeping the leak-surface controlled while still growing seats — justifies via least-privilege + the mirror-publish blast radius (a security posture, not a trust score).
- **Defer the T-2010 PAT rather than scope it now** — justifies via D3 + no-speculative-credential-without-a-task-that-defines-its-scope.
- **HO-direct authorship of this ADR** — justifies via ORG-012 (HO is the primary drafter of adr/) + the design context living in the live conversation; the ratify gate is preserved (the operator ratifies).

## Provenance / review trail

- Drafted by Orchestrator (HO), 2026-08-11, from the Telegram design conversation with the operator (msgs 7683–7692): the seats-purpose question → the same-model correction → the throughput rationale → the context + access reframe.
- Extends ORG-043. Scoping/build: T-2882. Ratify: filed to the operator (see the ratify ticket).
- Status: **Proposed**, awaiting the operator ratification.
