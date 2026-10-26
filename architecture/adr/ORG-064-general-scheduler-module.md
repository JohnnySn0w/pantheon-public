---
aliases: [ORG-064]
title: General Scheduler — the fleet-wide "when things run" module
status: "Ratified 2026-08-12 by the operator (Telegram 7762, 'Consider it signed') — sketch closed at the same-night spitball (TG 7755-7760): entry schema, exception semantics (when/whether-never-what), reconciler safety (plan/apply, self-heal-owned, orphan=flag), health surface, phase-split build (T-2896 render-only / T-2662 apply). Substrate = systemd-timers (TG 7625). Completes the three-isolate naming (Harness Manager / Action Broker / General Scheduler all ratified)."
date: 2026-08-07
author: Orchestrator (Orchestrator)
relates: ORG-063, ORG-025, ORG-052, ORG-049
build_disposition: needs-build
scoping_ticket: T-2662
# elements: — ORG-065 machine index of ORG-064's TWO native numbering
# groups (T-3205 Tranche B, "numbered-sections-only" decomposition
# class per notes/2026-08-12-org065-walk-census.md). Group 1 (E1-E3)
# mirrors "## Decision"'s three top-level numbered sections 1:1 (native
# "### N." headings); the 1a/1b/2a/2b/2c lettered sub-points stay
# folded into their parent element — no ticket in this pass needs that
# finer granularity. Group 2 (E4-E6) continues sequentially, document
# order, over "## Open questions — RESOLVED"'s three declarative
# resolutions (native "1./2./3." — genuinely resolved decision content,
# not open interrogatives, per the multi-group normalization
# extension). Each entry's text opens with its native id verbatim.
# Added by HO; no obligation content changed.
elements:
  - {id: E1, text: "1. Declarative fleet-schedule spec (per-persona + all-persona + exceptions): a single declarative spec is the source of truth for scheduling policy — all-persona schedules, per-persona schedules, per-persona exceptions (persona-level wins over all-persona)"}
  - {id: E2, text: "2. Substrate — systemd-timer-backed, NOT a new daemon: the Scheduler module is a stateless renderer that reads the spec and renders/reconciles the concrete .timer units per account; systemd remains the execution substrate"}
  - {id: E3, text: "3. Isolate properties (ORG-063 §4b, mandatory): health-legible (§7 property surface), restart-fixable (reconcile-from-spec is idempotent), stateless (no session-continuity state)"}
  - {id: E4, text: "1. Substrate: systemd-timer-backed, confirmed. The Scheduler needs install/remove/disable on all timer units; that timer-management is a host action, so it routes through the Action Broker as a brokered capability"}
  - {id: E5, text: "2. Spec home: MODULAR, not instance.yaml. A schedules/ directory (global/all-persona file + per-persona files) merged by a loader — touches none of the ~25 existing instance.yaml consumers"}
  - {id: E6, text: "3. Migration scope: ALL timers in the spec, classified by SCOPE. All 51 timers (census on T-2662) go in the spec, tagged per-persona / all-persona / host; none excluded"}
---
# ORG-064 — General Scheduler: the fleet-wide "when things run" module

**Status:** **RATIFIED 2026-08-12 by the operator (TG 7762), same night as the spitball that closed the sketch (TG 7755–7760).** Sketch closed: entry schema (§1a), exception semantics (§1b), reconciler safety (§2a), health surface (§2b), and the phase-split build sequencing (§2c) are settled. Substrate SETTLED = systemd-timers over a cron controller (the operator TG 7625: "no migration, let's keep what we have", per the Persistent=/health/OnFailure/journald rationale, TG 7622–7624). NOT yet formally ratified: this ADR was sketch-status without a ratify ticket targeted at the operator (his catch, TG 7629), so a proper ratify surface (**T-2836**) is now filed for a clean sign-off. Direction set by the operator TG 7255 (2026-08-07); sketch open-questions **and** the substrate residual resolved (the operator TG 7258 + 7260 + 7625): timer-backed + brokered timer-management, modular `schedules/` spec, all-timers-in-spec-by-scope (host chores included, for reproducible seeding). Build tracked in **T-2662** (labeled org-064 + build; depends on the Action Broker T-2652). Spun out of ORG-063 §Scheduler.

## Intent

One fleet-wide subsystem that owns **when** things run — the third isolate alongside the Harness Manager (how a seat runs) and the Action Broker (host-capability authority), per ORG-063. It replaces today's scattered, hand-authored scheduling with a single declarative schedule that supports per-persona items, all-persona items, and per-persona exceptions.

## Context — the current sprawl

Scheduling today is ~30+ independently hand-authored systemd timers plus per-persona wake, with no unified policy layer and only an implicit, inconsistent per-persona-vs-all-persona distinction:
- **Per-persona runtime:** `pantheon-persona-consume@<persona>.timer` (wake/consume — the ORG-063 §1 seam), `pantheon-keep-going@<persona>`, `pantheon-context-fill-monitor@<persona>`, `pantheon-mailbox-wake@<persona>`.
- **Persona-specific cadence:** Specialist's ORG-025 watch-loop scheduling; Recovery-specific monitors (restart-drain, agent-monitor healthcheck).
- **Fleet/infra:** knowledge-sweep, watch-digest, policy-outflow, s3star-audit, config-sync, plus pure-host timers (apt-update, disk-usage).

Adding or changing a schedule means hand-writing/editing a `.timer` unit and installing it per account. There is no one place that answers "what runs, for whom, when," and no first-class way to say "this runs for every persona *except* specialist."

### Relationship to ORG-049 (manifest-driven installation + convergent bringup, ratified)
The Scheduler is **not** a new provisioning story — ORG-049 already ratifies manifest-driven installation and convergent (idempotent) bringup, and its Layer-1 connective substrate already includes "timers/cron jobs" that the installer provisions. The Scheduler is where that timer-provisioning **evolves from an installer step into a first-class declarative subsystem**: ORG-049's installer stands up the substrate and, going forward, **delegates timer management to the Scheduler** (which owns the `schedules/` spec + the reconcile/render/apply loop). Boundary: **ORG-049 installs the box; the Scheduler owns the ongoing schedule.** The "all timers in the spec, classified by scope, host chores included" decision (§Open Q3) is what makes ORG-049's convergent bringup reproducible for *scheduling* specifically — a fresh box applies the spec and converges to the full timer set. (ORG-049 is Ratified; a forward-pointer amendment to it is deferred until the Scheduler firms Proposed→decision, per supersede-don't-edit.)

## Decision (firmed — sketch closed at the 2026-08-12 spitball, the operator TG 7755–7760)

### 1. Declarative fleet-schedule spec (per-persona + all-persona + exceptions)
A single declarative spec is the source of truth for scheduling policy:
- **all-persona schedules** — items that run for every persona (e.g. a daily health self-report, a memory-pin sweep).
- **per-persona schedules** — items scoped to one persona (e.g. specialist's research cadence, a persona's wake interval).
- **per-persona exceptions** — a persona opts out of, or overrides the timing of, an all-persona item. Merge rule: **persona-level wins over all-persona** (defaults + overrides, the same shape as `instance.yaml`'s role config).

**1a. Entry schema (firmed).** Each spec entry carries: `name`, `scope` (host / all-persona / per-persona), `calendar`, unit payload ref (what it runs), `OnFailure` wiring, `owner` (steward), a `serves:` line (why this timer exists — schedule provenance), and a `system:` field tying the entry to its **[[ORG-081]] registry `id`** (the ORG-068 coverage matrix retired into that registry on 2026-08-26; legacy titles alias through [[ORG-081]] D-13's one-time migration) — which makes the lifecycle contract's *monitor/cadence* column **derivable from the schedule spec** rather than separately maintained.

**1b. Exception semantics (firmed — the operator's Q at TG 7757, ruled TG 7757/7760).** *Exceptions may touch WHEN or WHETHER, never WHAT.* Exactly two forms:
- **Opt-out** — "this persona doesn't run the fleet job": an **explicit record with a reason** (waiver-with-rationale, the ORG-068 shape), never a silent absence — so the reconciler distinguishes *deliberately absent* from *missing = drift*.
- **Time-override** — same job, shifted calendar.
A persona wanting a **different implementation** is NOT an exception: it is an opt-out **plus** its own per-persona entry — two visible records. Payoff: "who runs the standard X" always has a clean answer, every entry's payload stays uniform, and variants are first-class discoverable schedules rather than surprises buried in exceptions.

### 2. Substrate — systemd-timer-backed, NOT a new daemon (my lean; §Open Q1)
The Scheduler module is a **stateless renderer**: it reads the spec and renders/reconciles the concrete `.timer` units per account; systemd remains the execution substrate that persists and fires. The module itself holds no runtime state and runs no long-lived loop.
- **Why this over a scheduler daemon:** it falls straight out of ORG-063 §4b — **stateless** (policy in the spec, firing-state in systemd), **restart-fixable** (re-render from the spec, nothing to recover), **health-legible** (reports via the ORG-063 §7 D-Bus property surface). A long-running scheduler daemon owning an in-memory schedule is the harder thing to make stateless/restart-blind, and re-invents persistence + misfire-recovery systemd already does.
- The module = **spec + deterministic reconciler (render/diff/apply timers) + health reporter.** It owns "when"; it does NOT own job logic — jobs stay where they are, the Scheduler only schedules them.

**2a. Reconciler safety semantics (firmed at the spitball).**
- **Plan/apply split** — render + diff against installed state is a privilege-free *plan* mode (the same shape as the installer's plan/check); *apply* is a separate, brokered step.
- **Drift self-heal on OWNED units** — a hand-edited unit the spec owns is reverted to spec at reconcile (the spec is the source of truth for what it manages; same posture as the hooks guard).
- **Orphan policy: FLAG, never auto-touch** — a timer present in systemd but absent from the spec is *reported*, not adopted or removed. Bringing it into the spec (or retiring it) is a deliberate census/migration decision, not a reconciler side-effect.
- **Idempotent + safe-to-rerun** throughout; a failed mid-apply leaves a diffable state, re-running converges.

**2b. Health surface (firmed).** The Scheduler reports on the ORG-068 D-Bus property surface: last-reconcile time + result, current drift count (owned-unit divergence + orphan count), and per-timer last-fire status. That is its own *monitor* leg.

**2c. Build sequencing (the operator TG 7757 — "needs a ticket", filed).** Phase 1 = spec schema + render/dry-run reconciler, **broker-independent, zero privilege** — **T-2896**, fires on this ADR's ratify (T-2836). Phase 2 = apply + live-timer migration one class at a time — **T-2662**, gated on the Action Broker (T-2652) *and* T-2896. The critical path no longer waits on the broker to start.

**2d. Germination seam ([[ORG-080]] §Amendment 2026-10-17, A-3).** The Scheduler is a registered ORG-080 day-one component: the engine calls its render + apply to install timers. In the other direction, Scheduler timers are the only unattended trigger for `germinate` (auto-update, periodic verify). A scheduled run carries only `privilege: none | seat` legs; for privileged components it may auto-stage but never cut over — cutover stays supervised.

### 3. Isolate properties (ORG-063 §4b, mandatory)
Health-legible (§7 property surface), restart-fixable (reconcile-from-spec is idempotent), stateless (no session-continuity state). An isolate, monitorable by the future forward-looking control component (ORG-063 §Forward-looking).

## Open questions — RESOLVED (the operator, TG 7258 + 7260, 2026-08-07)
1. **Substrate: systemd-timer-backed, confirmed.** The Scheduler needs **install / remove / disable** on all timer units; that timer-management is a **host action**, so it routes through the **Action Broker** as a brokered capability — the Scheduler stays low-privilege, the ORG-063 cordon model intact, no new privileged UID. **RESOLVED — broker it** (the operator TG 7260 "clever!"). Relates T-2652 (the broker provides this).
2. **Spec home: MODULAR, not `instance.yaml`.** `instance.yaml` is the central identity/topology manifest read by ~25 tools (mostly via `agent_role_map.py`, the canonical resolver) — persona bringup/runtime/restart/cutover, ticket tooling, monitors, and the provisioning/seed tools. A `schedules:` section would bloat that one heavily-depended-on file and mix scheduling policy into the identity manifest. So the schedule spec is its own modular file(s): a `schedules/` directory (global/all-persona file + per-persona files) merged by a loader — touches none of the 25 existing consumers, keeps `instance.yaml` lean.
3. **Migration scope: ALL timers in the spec, classified by SCOPE** (the operator TG 7260 — supersedes the earlier "pure-host chores stay plain" lean). Rationale: host chores must be *provisioned* on a fresh box (seeding a new Ubuntu box with Pantheon needs them), so they belong in the declarative spec too — that is what makes seeding **reproducible**. All 51 timers (census on T-2662) go in the spec, tagged **per-persona / all-persona / host**; none excluded. Dovetails with the existing seed/provisioning tools that already read `instance.yaml` (`provision_repos`, `neutralize_seed`, `seed_rebrand`) — the Scheduler becomes part of reproducible box-seeding.

Build/design tracked in **T-2662** (depends_on T-2657 ratify; relates T-2652 Action Broker for the brokered timer-management path). All three sketch questions + the substrate residual are now closed; ORG-064 is ready to firm from Proposed → decision.

## Consequences
- Positive: one place answering "what runs, for whom, when"; first-class per-persona exceptions; new schedules are a spec edit, not hand-authored units; uniform health for the scheduling layer.
- Cost/risk: a reconciler that renders systemd units cross-account is a real surface (must be idempotent + safe to re-run); migration touches live timers (stage behind verification, one class at a time).

## Re-evaluate / next
Firmed. On ratification (T-2836): phase 1 (T-2896, spec + render/dry-run) fires immediately; phase 2 (T-2662, apply + class-at-a-time migration) follows the Action Broker. Re-evaluate the orphan policy once the 51-timer census is fully in-spec (a zero-orphan steady state may justify tightening flag→error).
