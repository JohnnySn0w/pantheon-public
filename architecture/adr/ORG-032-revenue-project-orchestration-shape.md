---
aliases: [ORG-032]
id: ORG-032
title: Money-engine orchestration shape — status-quo now, dedicated agent at actuation-un-defer (the ORG-031 onboarder retires the cost objection)
status: "Adopted 2026-06-11 by Orchestrator (HO) — autonomous under P/D justification per the operator msg 3733 (principles-as-ratification; ratify-asks below goal-level retired). Resolves T-0377. Decision is trigger-based: Option D/A now, committed to Option B at the named trigger. NOTE: the STATE-2 trigger (un-defer of revenue-project actuation) remains goal-level and the operator's."
date: 2026-06-08
---

# ORG-032 — Money-engine orchestration shape

**Status: Adopted 2026-06-11** (autonomous, P/D-justified, per the operator msg 3733 — principles-as-ratification posture; see rulebook D3 amendment same date). Drafted 2026-06-08 by Orchestrator (`claude`, HO). Per ORG-015, `adr/` is the ratifiable substrate. This ADR is the **T-0377 deliverable** — the "options A–E, HO drafts + the operator ratifies" orchestration-shape decision the operator asked for (msg_1455, 2026-05-17). It selects a *trigger-based* shape rather than a single static option, because the right answer is conditional on revenue-project's phase and that phase changed materially since T-0377 was filed.

**Author:** Orchestrator (HO) — orchestrator-drafted; the operator ratifies.

**Related:** [[ORG-027]] (orchestrator-tier-with-domains — the topology this slots a 4th orchestrator into, if/when), [[ORG-031]] (harness-abstraction adapter + generalized daemon-onboarding — **load-bearing here: it retires Option B's "significant infra cost" con**), [[ORG-028]]/[[ORG-030]] (portable-config topology — the role-keyed config a new agent would ride), [[ORG-020]] (self-triggered restart — part of the always-on stack a new agent needs), [[T-0377]] (this ticket), [[T-0376]] (revenue-project veins epic), [[T-0712]] (the harness-abstraction epic ORG-031 records), `project_echo_not_an_agent` (the project-orchestrator-per-arc pattern this compares against), revenue-project `notes/2026-06-08-F-harness-build-designs.md` + `notes/2026-06-08-V1-calibration-harness-design.md` (the build-designs whose eventual *actuation* is the load this ADR sizes for).

---

## Context

Per the operator msg_1455 (2026-05-17): *"Orchestrator is handling echo atm, but if it can only spin up so many team members, it may be worth having another team-spinning orchestrator for money engine, or figuring out a better layering/execution framework."* The concern is a **bottleneck**: HO is the spawn-point for echo-prismatic project-orchestrators *and* does org-tier pantheon-config work; if revenue-project arcs run in parallel with echo arcs, HO's single context juggles two project conversations plus org-tier.

**What changed since T-0377 was filed (2026-05-17 → 2026-06-08) — this is why the answer is now clean:**

1. **Money-engine actuation is DEFERRED** (the operator msg 3510, 2026-06-08). The research phase (V1–V4) is *complete*; build/implementation is explicitly gated behind the operator's per-harness un-defer. **There is no parallel actuation load today and none imminent.** The bottleneck the operator pattern-matched on is not currently active.
2. **The build-designs exist** (the four §F harness designs + the V1 calibration design, 2026-06-08). When un-deferred they become implementer dispatches — *that* is when parallel-arc load would materialize, and it is a discrete, foreseeable event (an un-defer signal), not a gradual creep.
3. **ORG-031 is retiring Option B's dominant con.** The original T-0377 framing rated Option B (dedicated always-on revenue-project agent) as "significant infra cost — new account, systemd, persona, deploy key, inbox-consumer, context-monitor, handoff/restart wiring, +ops surface for Recovery." ORG-031's **generalized daemon-onboarding framework** (the `PantheonHarnessAdapter` + harness-agnostic onboarder) is purpose-built to make standing up a new always-on agent *cheap and procedural*. As that lands, Option B's cost objection structurally shrinks — the calculus that made D/A the only sane near-term choice flips for the actuation phase.

So the decision is **conditional on phase**, and the phase transition is a *named, observable trigger* (the operator un-defers revenue-project actuation). That lets us commit now to both the cheap present and the foreseeable future, rather than re-opening the question later.

## Decision

**A trigger-based two-state shape:**

**STATE 1 — NOW (revenue-project in design/deferred phase): Option D/A — status quo, no new agent.**
- Money-engine **research** stays Specialist episodic dispatches (its existing multi-arc pattern; already proven on V1–V4).
- Money-engine **design** (current phase) is HO-direct (the build-designs were authored this way — appropriate, low parallel load).
- Any pre-actuation arc that needs a team uses the **project-orchestrator-per-arc** pattern HO already runs for echo (Option A), spawned by HO per cycle, transient.
- **No dedicated always-on revenue-project agent is stood up while actuation is deferred.** There is no parallel load to justify the (currently-still-nonzero) infra cost. This is cheapest and correct for the present.

**STATE 2 — AT THE TRIGGER (the operator un-defers revenue-project actuation AND it generates sustained parallel arc-load alongside echo): Option B — a dedicated always-on revenue-project orchestrator.**
- Stand up a 4th always-on orchestrator (a "treasurer"-class persona, named per `project_naming_convention` at stand-up) owning the revenue-project domain, slotting into the ORG-027 orchestrator-tier-with-domains topology as a peer-in-tier, domain-scoped agent (revenue-project = its domain; cross-domain routes through the queue per ORG-027).
- **Stand it up via the ORG-031 generalized onboarder** — that is the mechanism that makes B affordable; this ADR explicitly chains B's feasibility to ORG-031 landing.
- HO offloads revenue-project context entirely; echo and revenue-project actuation then run on independent orchestrators with no single-context bottleneck.

**The trigger is explicit and observable:** The operator un-defers revenue-project actuation (removes `interactive-required` from the build tickets / gives the build go) *and* the actuation work is sustained-parallel with echo (not a single one-off arc HO can absorb). Until both hold, STATE 1 stands.

## Alternatives considered

- **Option C (refactor HO into a pure 2-tier router that spawns a PO per active project).** Rejected as the primary path: heavier refactor of HO's role, needs new PO-lifecycle discipline (keep-alive vs respawn), and carries distributed-state-drift risk if POs go stale between arcs. Option B achieves the same parallel-capacity goal with *less* novel machinery once the ORG-031 onboarder makes a dedicated agent cheap — a new peer orchestrator is a known, well-trodden shape (Specialist/Recovery) where a 2-tier-router HO is not. C stays available as a fallback if standing up a 4th always-on agent proves heavier than ORG-031 projects.
- **Option E (task-router tier above HO / Telegram-router / pair-of-HOs).** Over-engineered for the load. The ORG-027 domain model already gives lateral routing between peer orchestrators without a new super-tier. Revisit only if the orchestrator count grows past ~4–5 and routing-by-hand becomes the bottleneck — not now.
- **Pure Option A forever (HO spawns revenue-project POs per arc indefinitely).** Rejected for STATE 2: it preserves exactly the single-context bottleneck the operator flagged; fine while load is low (it *is* STATE 1), but it does not scale to sustained parallel actuation.
- **Stand up the dedicated agent NOW (Option B immediately).** Rejected: actuation is deferred, so a dedicated revenue-project agent would sit idle burning ops surface (Recovery-watched pane, persona maintenance, a 4th cap-window) with no parallel load to absorb. Premature; STATE 1 is strictly cheaper until the trigger.

## Consequences

**Positive:**
- Cheapest correct posture now (no idle agent), with a pre-committed, well-justified answer for the foreseeable phase change — the question does not need re-litigating at un-defer time; the trigger fires and STATE 2 executes.
- Ties the orchestration shape to the ORG-031 arc: B becomes the answer *because* the onboarder makes it cheap, so the two efforts reinforce rather than duplicate.
- Slots cleanly into the existing ORG-027 topology (a 4th domain-scoped peer orchestrator) — no new conceptual tier.

**Negative / open:**
- A trigger-based decision needs someone to *notice the trigger fired*. Mitigation: the trigger is the same un-defer event the operator himself initiates (removing `interactive-required` / giving the build go), so HO sees it directly; add a check at revenue-project un-defer time ("does this un-defer create sustained parallel load → STATE 2?").
- The "sustained parallel" half of the trigger is a judgment call (one one-off actuation arc HO can absorb under Option A; it's *sustained* parallel load that justifies B). Left deliberately to HO's judgment at the trigger rather than a hard metric, per the autonomy-bar posture.
- Depends on ORG-031 landing for B to be cheap. If ORG-031 stalls and the trigger fires first, fall back to Option A (HO-spawned PO) for the first actuation arc while the onboarder catches up, or to Option C.

## Cross-references

- **Principles/directives:** D1 (bandwidth — the whole point is keeping HO's context unbottlenecked), P5 (decisions-driven), Pantheon D3 (this is ADR-worthy org-design → HO drafts, the operator ratifies).
- **ADRs:** [[ORG-027]] (the tier/domain topology B slots into), [[ORG-031]] (the onboarder that makes B cheap — the load-bearing dependency), [[ORG-028]]/[[ORG-030]] (portable role-keyed config a new agent rides), [[ORG-020]] (restart stack).
- **Tickets:** [[T-0377]] (this ask), [[T-0376]] (revenue-project veins), [[T-0712]] (harness-abstraction epic).
- **Money-engine:** `notes/2026-06-08-F-harness-build-designs.md`, `notes/2026-06-08-V1-calibration-harness-design.md` (the designs whose actuation is the STATE-2 load), `notes/HANDOFF.md` (deferred-actuation status).
