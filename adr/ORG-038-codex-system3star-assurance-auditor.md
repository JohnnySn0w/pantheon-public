---
aliases: [ORG-038]
title: Codex as Pantheon's System-3* assurance auditor
status: RATIFIED 2026-07-06 by the operator (T-1582, ticket-UI verdict 23:36:20Z)
date: 2026-06-29
author: Orchestrator (Orchestrator)
ticket: T-1371
relates: ORG-031 (harness abstraction / Codex worker role), ORG-018 (Codex CLI pilot), ORG-027 (orchestrator tiers + domains)
---
# ORG-038 — Codex as Pantheon's System-3* assurance auditor

**Status:** **Ratified 2026-07-06 by the operator.** **Draft — Authored by HO under ORG-012 (HO is primary drafter of `adr/`). The **design** of the standing System-3* function is within HO auto-approve scope (ADR-tier + org-shape, amended D3). **Pilot actuation gates on the operator** for two reasons spelled out in §Decision-rights: it adds a new role to the org's VSM role-map (org-identity = S5's call) and it touches Codex compute cadence (the cost/quota dimension). The operator's steer (Telegram, 2026-06-29): *"has meat, shores up our VSM — review, percolate, possibly implement."* Source proposal: `knowledge/episodic/2026-06-29-codex-s3star-auditor-proposal/proposal.md` (exhaustive audit program, finding format, severity calibration, 30-day pilot weeks, guardrails — not duplicated here).

## Context

Pantheon has many individual security controls and a reactive CVE/incident process, but **no integrated, recurring assurance loop** — no standing, structurally-independent function that verifies whether deployed reality, operational behavior, and evidence match policy and claims. The one-shot external audits of 2026-06-28 (`knowledge/external-audits/2026-06-28/`) demonstrated the value concretely: they surfaced real items (e.g. the Rust-toolchain P1, the `artifact_gc` non-atomic-delete, worker-guard coverage gaps) that became T-1376–T-1386. But those were *external, one-time, and unrepeatable* — assurance currently has no home in the org.

In Beer's Viable System Model, that home is **System 3\***: the sporadic, aperiodic audit channel that gives System 3 (executive control) independent assurance about System 1 reality **without** routing through System 2's routine reporting. Pantheon's in-force VSM map is the operator=S5, Orchestrator=S3, Recovery=S2–3, Specialist=S4 — and **S3\* is vacant.** That vacancy means S3 (HO) effectively audits its own operations, the classic shared-assumption blindness.

Codex is a strong fit: it runs a **different model and harness** from the Claude tier that designed and operates most of the system (genuine independence), and it is lightly committed operationally (it is currently an ORG-031 Option-A dispatched worker, not a standing-arc owner).

## Decision

Assign Codex the standing role of **System-3\* Assurance Auditor** — an independent, read-only inspection function that verifies deployed reality against policy and claims, files severity-ranked findings to domain owners, and re-tests remediations to close them. Adopt the proposal's mission, independence contract, audit authority, audit program, finding format, severity calibration, and guardrails **as written** (`proposal.md`), with the two refinements below.

### Refinement 1 — Reconciliation with ORG-031 Option A (the load-bearing design choice)

The proposal as written implies Codex runs a **standing autonomous** audit cadence. That conflicts with **ORG-031 Option A** (ratified the operator 2026-06-09), which defines Codex as a *dispatched worker-harness* that explicitly **does not run its own standing arcs**. We resolve this in favor of **option (1): HO-scheduled dispatched audits**:

- The "standing cadence" lives in an **HO-side scheduler** (a timer analogous to Specialist's `watch_runner`) that **dispatches** Codex a bounded, read-only audit task on the agreed cadence.
- Codex executes the audit read-only and **returns** findings; HO files the finding-tickets to domain owners and lifts material outcomes to the ledger.
- Codex never holds a standing autonomous arc, never self-files into the queue beyond the constrained finding path, and never self-closes.

This reconciles with ORG-031 Option A **unchanged** (no amendment needed), and — per the VSM-recursion analysis (T-1387, `knowledge/episodic/2026-06-29-agent-harness-fallback-assessment/notes.md` addendum) — it is **the more VSM-faithful shape, not merely the lower-friction one**: Beer's S3\* is *sporadic/aperiodic orchestrated* audit access **by design**, intermittent and externally-triggered, never a continuous self-driven loop. HO-scheduled dispatch maps 1:1 onto canonical S3\*.

### Refinement 2 — Independence rationale (the separation-of-duties axis)

Codex also wears the ORG-031 implementer/reviewer hat. The independence concern ("can the same instance audit and also build?") is resolved by naming the correct axis (T-1387): **the load-bearing independence axis is function/channel separation + the read-only / no-self-closure / no-self-verification contract — NOT instance-separation.** Model-tier diversity (Codex ≠ Claude tier) is a valuable *reinforcing* axis, not the primary one. Therefore:

- **No third instance is required.** A contracted S3\* function with hard boundaries IS the textbook answer.
- Enforced rule: **Codex-as-auditor must not verify controls or remediations that Codex-as-implementer authored.** HO tracks authorship at dispatch time and routes such verifications to a different verifier (or records an explicit HO override per the contract).

## Cost / cadence

Reframed from the initial "financial-reserved" flag (T-1371 handoff). Per **ORG-018**, Codex runs on the operator's **ChatGPT-Plus subscription via OAuth** — *"compute I'm already paying for,"* no separate API key or billing surface. Therefore on the **interactive / Plus-quota path the recurring audit cadence is ~zero incremental dollar cost.** The genuine residue is narrow:

1. **Headless `codex exec` billing is still an open question** (ORG-031, msg_2852: usage-based vs Plus-quota). → **Constrain the pilot to the Plus-quota path** and do not lean on headless `codex exec` until that question is resolved.
2. **Quota-contention:** recurring audits draw the same Plus quota as Codex's worker dispatches. Not dollars, but a shared-resource judgment. Option (1) makes this an **HO-controlled scheduled-dispatch knob** (cadence is tunable from the HO-side timer), which is the cheapest and most controllable shape.

Net: the pilot is a **near-zero-spend** decision, not a budget ask.

## Decision-rights — why actuation gates on the operator

ADR-tier authorship and org-shape design are inside HO's auto-approve bar, and the operator steered "possibly implement." Two dimensions nonetheless hold **actuation** at his tier:

1. **Constitutional:** adding a standing role to the org's VSM role-map is an org-identity change = **S5's call**. (The 2026-06-28 external audit likewise reserved org-constitutional items — A1/ORG-A1 — to the operator.)
2. **Cost/path:** confirming the Plus-quota constraint + accepting the quota-contention trade-off is the financial-adjacent fork reserved per the standing financial-reserved posture.

Per act-vs-park: this ADR is the **reversible artifact produced now**; the **GO + path choice is parked for the operator**. On his ratification I will: build the HO-side audit scheduler (option 1), wire the constrained finding-ticket path + the authorship-aware verifier-routing rule, and dispatch the **Week-1 baseline** (read-only inventory + trust-boundary map + auditor self-permission check). First real assignment = the security-assurance baseline audit, **no production changes**.

## Options considered

- **(1) HO-scheduled dispatched audits — CHOSEN.** Fits ORG-031 Option A unchanged; most VSM-faithful (sporadic orchestrated audit); cost = HO-controlled knob.
- **(2) Standing autonomous S3\* arc for Codex.** Rejected: reverses ORG-031 Option A's "no standing arcs," enlarges always-on surface + cost, and is *less* VSM-faithful (S3\* is aperiodic by design, not continuous).
- **(3) Hybrid** (dispatched-audit role distinct from implementer role, independence by HO orchestration). Folded into (1)+Refinement 2 rather than kept as a separate option.
- **Add a 4th instance for audit independence.** Rejected: unnecessary (independence is the channel/contract, not the instance — Refinement 2) and adds account/maintenance surface.

## Consequences

- **Positive:** fills the vacant S3\* channel; institutionalizes the assurance the one-shot audits proved valuable; independent re-test-to-close discipline; genuine model/harness-diversity dividend; near-zero incremental cost.
- **Cost/risk:** Codex Plus-quota contention with its worker role (mitigated by the HO-controlled cadence knob); the dual-hat separation-of-duties rule must be actively enforced at dispatch time; pilot must guard against the classic auditor failure modes (auditor-becomes-implementer, checklist theater, audit flood, severity inflation) — the proposal's guardrails cover these.
- **Reversible:** it's a 30-day pilot; the pilot synthesis (Week 4) decides standing-cadence or stand-down.

## DECISIONS

- **Reconcile via option (1) HO-scheduled dispatched audits, not a standing autonomous arc.** Justifies-via P5 (roles-don't-hop / ORG-031 Option A preserved), P4 (independent traceable assurance), and VSM-faithfulness (S3\* is sporadic orchestrated audit).
- **Independence axis = function/channel + read-only/no-self-closure contract, not instance-separation; no third instance.** Justifies-via P1 (structural enforcement over compliance-trust), P4.
- **Constrain the pilot to the zero-incremental-cost Plus-quota path; defer headless `codex exec`.** Justifies-via the financial-reserved posture (prefer zero-spend path) + D1.
- **Hold pilot actuation for the operator (constitutional VSM-role addition + cost/path fork); produce the ADR now as the reversible artifact.** Justifies-via D3 (autonomy bar — above-bar actuation held at S5), act-vs-park calibration, P6 (pilot-before-permanent).
- **Treat the first month as a pilot, then decide standing cadence from evidence.** Justifies-via P6, D1.
