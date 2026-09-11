---
aliases: [ORG-027]
id: ORG-027
title: Reclass the always-on agents to orchestrator-tier-with-domains (retire the worker-vs-orchestrator split for persistent agents)
status: Ratified 2026-06-05 by the operator via T-0789. Drafted 2026-06-04 by Recovery (T-0785, spawned from noodle T-0764).
date: 2026-06-04
build_disposition: no-build-needed
---

# ORG-027 — Orchestrator-tier-with-domains reclass for the always-on agents

**Status:** **Ratified 2026-06-05 by the operator.** **Drafted 2026-06-04 by Recovery (`claude-fallback`).** via [[T-0789]]. Per ORG-015 `adr/` is the ratifiable substrate.

**Ticket:** [[T-0785]] (the operator close-noodled [[T-0764]] `--spawn-to recovery --as-action`, then confirmed "draft it" 2026-06-04). T-0764 floated the reclass (the operator msg 2323/2325).

**Author:** Recovery (`claude-fallback`) — orchestrator-drafted; the operator ratifies.

**Related:** [[ORG-025]] (Specialist orchestration model — **already** elevated Specialist to orchestrator-class *operationally* and explicitly **deferred** the "formal agent-registry orchestrator-class elevation" as a "decide if/when it matters" open question (ORG-025 §Consequences/deferred); this ADR **resolves that deferral** — it formalizes the orchestrator tier for all three agents at the **ADR + `repos.yaml`** level, while the *machine-readable registry tier-field* stays deferred per §deferred below), `rulebook/policies/trust-posture.md` (the per-tier autonomy calibration this ADR rewrites into per-domain — amended in the same ratification scope), `repos.yaml` ([[T-0761]] — the structural surface a domain is expressed on), [[ORG-010]] (three-tier policies extension — the framework the trust-posture policy lives under), [[ORG-002]] (principles/directives split — lets directives be calibrated without amending principles), P5 (roles-don't-hop — the principle this reclass re-frames without contradicting), D2 + D3 (the directives whose worker-orchestrator framing this updates).

## Context

The org currently describes its agents with a **worker-vs-orchestrator tier split**: the Orchestrator (Orchestrator) is orchestrator-tier; everything else is worker-tier. That framing no longer matches reality for the three *always-on* agents:

- **Orchestrator (HO, `claude`)** — orchestrator-tier. Orchestrates all project delivery, cross-agent dispatch, infra. Uncontested.
- **Specialist (`claude-research`)** — **ORG-025 already elevated Specialist to orchestrator-class**: it runs a persistent interactive session, drives its own watch cadence, dispatches `researcher` subagents via the Agent tool (the HO→worker model, applied at Specialist's tier), and **consumes its own `target=specialist`/`specialist` queue via `pantheon-inbox-consumer@specialist.timer` (T-0102)**. Specialist is not a worker — it *has* workers.
- **Recovery (`claude-fallback`)** — runs its own inbox-consumer + ticket queue (`target=recovery`), owns the ticket-system tooling (lint/grooming/inbox-consumer), the **recovery/restart machinery for all agents — including cross-account repair of the `claude`/Orchestrator account, Recovery's defining function** — and cross-account ops-discipline, and dispatches its own subagents/teams (`CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`, T-0730). Recovery is not a worker either.

So the persistent agents are **all orchestrators**; what actually differentiates them is **scope**, not tier. Calling Specialist/Recovery "workers" forces awkward framing (e.g. ORG-025 had to spend a section establishing Specialist-is-actually-an-orchestrator) and leaves the per-agent **autonomy bar** (`trust-posture.md`) written HO-centric ("HO direct-action calibration") with no principled home for Specialist's and Recovery's autonomy.

Concurrently, [[T-0761]] built `repos.yaml` — a per-agent repo-access registry whose grants are *descriptive of current access reality* but explicitly "built to carry the domain restrictions surface IF the operator lands the orchestrator-tier-with-domains reclass: an agent's domain = the set of repos (+ later, targets) it may act on." T-0761's remaining program (grant **policy**, provisioning, audit) is gated on this reclass: without a definition of "domain," the grant policy has no principled basis.

The operator's rationale (msg 2323/2325): the reclass "makes more sense," and it rationalizes the Specialist parity work already done. T-0764 classified the decision as ADR-tier / principle-touching (rewrites the per-tier autonomy bar into a per-domain one; touches P5 framing) → the operator's call; he confirmed "draft it."

## Decision

**Reclass the three always-on Pantheon agents (Orchestrator/HO, Specialist, Recovery) from the worker-vs-orchestrator split to ALL orchestrator-tier, each constrained by an explicit DOMAIN.** The worker tier is *retained* for ephemeral dispatched subagents (implementer / reviewer / researcher / project-orchestrator), unchanged. The reclass is a re-description of the *persistent* agents, not a collapse of roles.

### 1. A domain is the agent's permitted action surface

A **domain** = the set of **repos** an agent may act on (the `repos.yaml` grant surface, T-0761) **plus** the set of **ticket targets / operational responsibilities** it owns. The proposed initial domains:

| Agent | Domain | Repos (rw unless noted) | Owns (targets / responsibilities) |
|---|---|---|---|
| **Orchestrator (HO)** | **org + delivery + infra** (head domain — broadest) | all project repos (rw) | org governance, all project-delivery orchestration, cross-agent dispatch, infra, the bridge/substrate; `target=orchestrator`/`orchestrator` |
| **Specialist** | **research + watch** | pantheon-config (rw, current); additional research repos grantable `ro`/`rw` via ticket-declared T-0761 grants once the grant policy lands | the watch loop + `knowledge/watch/`, research orchestration, `researcher` dispatch; `target=specialist`/`specialist` (consumed by Specialist's own `pantheon-inbox-consumer@specialist.timer`) |
| **Recovery** | **recovery + ticket-ops** | pantheon-config (rw) | recovery/restart machinery for **all** agents incl. cross-account repair (see §5), the ticket-system tooling (lint/grooming/inbox-consumer), cross-account ops-discipline; `target=recovery` |

HO's domain is the **head** domain — strictly the broadest, and the cross-domain routing/escalation hub. The reclass makes the three agents peers **in tier**, not peers **in authority**: HO retains primacy.

### 2. The autonomy bar becomes per-domain

`trust-posture.md`'s three buckets (**auto-approve** / **hold-and-ping** / **never-permitted**) are retained verbatim in *shape*, but **scoped by domain**:

- **In-domain + below-the-bar** (reversible, in the agent's repo/target set) → **auto-approve**, exactly as HO does today. This is the variance-absorption layer (D3 disposition) applied at each orchestrator's tier.
- **In-domain + above-the-bar** (ADR-tier / principle-touching / vault-adjacent / cost-or-secret-bearing / low-confidence) → **hold-and-ping the operator**, unchanged.
- **Cross-domain** (an action whose repo/target falls in *another* orchestrator's domain) → **route through the ticket queue to the domain-owning orchestrator**; never act directly. This is the lateral case of P5's roles-don't-hop and of trust-posture's standing "cross-instance commands that bypass the ticket queue" prohibition — now stated as a *domain* boundary, not just an *instance* boundary.
- **never-permitted** — unchanged for all orchestrators (`--no-verify`/hook bypass, queue-bypassing cross-instance commands, subagent→user direct comms, self-granting permission scope, speaking for the operator externally, reading user-private state).

### 3. P5 is re-framed, not contradicted

P5 ("subagents do, orchestrators decide, humans ratify; roles don't hop") **still holds in full**. The reclass:
- keeps the worker tier (ephemeral subagents *do*; they don't decide architecture — escalate);
- keeps humans-ratify (this ADR is itself drafted-awaiting-ratification under D2);
- adds a **lateral** roles-don't-hop boundary: an orchestrator acting *outside its domain* is the new role-hop — it must escalate/route, not reach across. Domains make P5's "right altitude" claim precise: each persistent agent decides at orchestrator altitude **within its domain**, and escalates across domains.

**Preserved tension (P6).** P5's *why* is "put each decision at the right altitude" — the prior model had a single orchestrator altitude (HO) above the workers, a clean two-level accountability gradient. This reclass creates **three peer orchestrator altitudes**. The containment is real *laterally* (cross-domain → route through the queue) and HO holds the broadest domain + the cross-domain routing/escalation role — but **HO-primacy is presently prose-enforced, not structural**: nothing in the substrate stops a peer orchestrator from widening its own domain except the `repos.yaml` gate (repos half) + audit tripwire and the operator's ratification. Poles: *uniform orchestrator tier* (matches reality, simplifies the model) vs *single-accountability-altitude* (P5's original safety property). Rebalance trigger: a peer orchestrator's domain-creep, or a cross-domain routing dispute, that the prose primacy can't resolve → revisit whether HO-primacy needs a structural form (e.g. a registry tier/precedence field).

### 4. The domain surface is structurally enforced (P1)

Per P1 (architecture over policy), the **repos half** of a domain is not a prose promise — it is the `repos.yaml` registry (T-0761): repo access is gated by the registry + lint + provisioning + the present-but-undeclared audit tripwire. This reclass **defines** the policy T-0761's grant layer was waiting for: *an agent's repo grants are exactly its domain's repo set; widen a domain only via ticket + the operator's ratification, never silently.*

The **targets half** is weaker today: the inbox-consumer *routes* each `target=` to the right agent's session, but routing is not an ownership *gate* — nothing structurally stops an orchestrator from acting on a concern it noticed out-of-band. So the targets boundary is **convention-backed (routing + this ADR) today, structural later**: a first-class targets-allowlist in `repos.yaml` is the deferred T-0761 step (§deferred). The ADR states this honestly rather than claiming full P1 enforcement: repos half = architecture; targets half = policy-awaiting-architecture.

### 5. Cross-account recovery is in Recovery's domain (not a cross-domain hop)

Recovery's defining function — repairing the `claude`/Orchestrator account when the prime bot self-breaks (`handoff_and_restart.sh`, `sudo -u claude`, killing/restarting Orchestrator's session) — touches infra that otherwise sits in HO's domain. That is **not** a cross-domain violation: **recovery/restart of all agents, including cross-account repair, IS Recovery's recovery domain.** The domain is defined by *responsibility*, not by *which account's files are touched* — Recovery owns "restore a wedged/broken agent," so acting on Orchestrator's substrate to restore it is *in-domain* for Recovery. This is also why the cross-domain "route through the queue" rule can't apply here: the recovery case exists precisely when the domain-owner (HO) is wedged/dead and *cannot* receive a queued ask. The recovery domain is the designed exception, encoded as ownership rather than as an ad-hoc carve-out: Recovery acts to restore, then files the post-hoc record (consistent with D3's hold-and-ping wait-time-bound + the existing restart SOPs).

## Options considered

| | Option | Verdict |
|---|---|---|
| **A** | Keep the worker-vs-orchestrator split (status quo) | **Rejected.** Mis-describes reality — Specialist (ORG-025) and Recovery already operate at orchestrator altitude with their own queues + subagent dispatch. Forces per-ADR "actually-an-orchestrator" framing and leaves Specialist/Recovery autonomy without a principled bar. |
| **B** | **All orchestrator-tier, each with an explicit domain** (repo + target set), per-domain autonomy bar, HO retains head primacy | **Adopted.** Matches reality, rationalizes ORG-025, gives T-0761's grant layer its definition, and scopes the autonomy bar coherently. Domain containment is what makes the uniformly-broader tier safe. |
| **C** | All orchestrator-tier, **no formal domains** (flat peer orchestrators) | **Rejected.** Drops the containment — the autonomy bar would have nothing to scope, repos.yaml grants would be unprincipled (any orchestrator → any repo), and HO primacy would be lost. Autonomy without containment is the failure mode B's domains exist to prevent. |
| **D** | Reclass only Specialist (formalize ORG-025), leave Recovery a worker | **Rejected.** Recovery already runs the ticket-ops + recovery domain with its own queue + subagents; leaving it "worker" reproduces the same mis-description the reclass exists to fix, one agent later. |

## Consequences

**positive:**
- The tier model matches operational reality; ORG-025's Specialist elevation stops being a special case and becomes an instance of the general model.
- T-0761's remaining program (grant policy + provisioning + audit) gets its principled definition: **domain = repo/target set**; the grant layer can land on a ratified basis rather than ad hoc. **Unblocks the T-0761 B-remainder.**
- `trust-posture.md` becomes a coherent per-domain calibration instead of an HO-only one — Specialist and Recovery autonomy finally has a written home.
- Lateral roles-don't-hop (cross-domain → route through queue) hardens the existing "no queue-bypassing cross-instance commands" rule with a positive structural definition.

**negative:**
- Amends a **ratified** policy (`trust-posture.md`) and touches P5's framing → requires the operator's re-ratification (this ADR + the trust-posture amendment, one scope). Until ratified, the per-tier bar remains in force.
- Domain boundaries must be defined and maintained; risk of **domain-creep** (an orchestrator quietly widening its own domain). *Mitigated by P1:* repos.yaml is the structural gate + the present-but-undeclared audit tripwire (T-0761) makes silent widening loud.
- Three agents at orchestrator-tier could be misread as three co-equal authorities. *Mitigated:* the decision explicitly preserves HO head-primacy (broadest domain + cross-domain routing hub) — peers in tier, not in authority.

**deferred:**
- Echo's **project-orchestrator** (spawned by HO per-arc, not always-on) — already covered by §Decision (the worker tier is retained for project-orchestrators; their "domain" is their dispatched arc, distinct from the always-on agents' standing domains). The deferral is to **re-confirm, not re-decide**, at Echo resume that nothing changed — folded into the Echo trust-posture sibling (T-0038 lineage).
- A formal agent-registry "tier" field per agent (vs the prose + repos.yaml definition here) — decide if/when tooling needs to read it.
- Extending the domain surface from repos to a first-class **targets** allowlist in repos.yaml (T-0761 notes this as a later step).

## Re-evaluate when

- A fourth always-on agent is added (does the domain model generalize cleanly?).
- An orchestrator's domain proves too narrow/broad in practice (escalation-rate or routing-friction signal).
- `repos.yaml` gains the targets allowlist (the domain definition's second half becomes structural too).
- P5 or D2/D3 are amended (re-read this ADR's framing claims).
- Echo resumes (the deferred project-orchestrator domain question reopens).
