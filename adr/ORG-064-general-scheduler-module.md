---
aliases: [ORG-064]
title: General Scheduler — the fleet-wide "when things run" module
status: "Proposed — firmed; substrate settled = systemd-timers (the operator TG 7625, no cron). Awaiting formal ratification via T-2836. Authored 2026-08-07 by Orchestrator (HO); spun out of ORG-063."
date: 2026-08-07
author: Orchestrator (Orchestrator)
relates: ORG-063, ORG-025, ORG-052, ORG-049
build_disposition: needs-build
scoping_ticket: T-2662
---
# ORG-064 — General Scheduler: the fleet-wide "when things run" module

**Status:** **Proposed — firmed; awaiting formal ratification (ratify ticket [[T-2836]]).** Substrate SETTLED = systemd-timers over a cron controller (the operator TG 7625: "no migration, let's keep what we have", per the Persistent=/health/OnFailure/journald rationale, TG 7622–7624). NOT yet formally ratified: this ADR was sketch-status without a ratify ticket targeted at the operator (his catch, TG 7629), so a proper ratify surface (**T-2836**) is now filed for a clean sign-off. Direction set by the operator TG 7255 (2026-08-07); sketch open-questions **and** the substrate residual resolved (the operator TG 7258 + 7260 + 7625): timer-backed + brokered timer-management, modular `schedules/` spec, all-timers-in-spec-by-scope (host chores included, for reproducible seeding). Build tracked in **T-2662** (labeled org-064 + build; depends on the Action Broker T-2652). Spun out of ORG-063 §Scheduler.

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

## Decision (SKETCH — the shape)

### 1. Declarative fleet-schedule spec (per-persona + all-persona + exceptions)
A single declarative spec is the source of truth for scheduling policy:
- **all-persona schedules** — items that run for every persona (e.g. a daily health self-report, a memory-pin sweep).
- **per-persona schedules** — items scoped to one persona (e.g. specialist's research cadence, a persona's wake interval).
- **per-persona exceptions** — a persona opts out of, or overrides the timing of, an all-persona item. Merge rule: **persona-level wins over all-persona** (defaults + overrides, the same shape as `instance.yaml`'s role config).

### 2. Substrate — systemd-timer-backed, NOT a new daemon (my lean; §Open Q1)
The Scheduler module is a **stateless renderer**: it reads the spec and renders/reconciles the concrete `.timer` units per account; systemd remains the execution substrate that persists and fires. The module itself holds no runtime state and runs no long-lived loop.
- **Why this over a scheduler daemon:** it falls straight out of ORG-063 §4b — **stateless** (policy in the spec, firing-state in systemd), **restart-fixable** (re-render from the spec, nothing to recover), **health-legible** (reports via the ORG-063 §7 D-Bus property surface). A long-running scheduler daemon owning an in-memory schedule is the harder thing to make stateless/restart-blind, and re-invents persistence + misfire-recovery systemd already does.
- The module = **spec + deterministic reconciler (render/diff/apply timers) + health reporter.** It owns "when"; it does NOT own job logic — jobs stay where they are, the Scheduler only schedules them.

### 3. Isolate properties (ORG-063 §4b, mandatory)
Health-legible (§7 property surface), restart-fixable (reconcile-from-spec is idempotent), stateless (no session-continuity state). An isolate, monitorable by the future forward-looking control component (ORG-063 §Forward-looking).

## Open questions — RESOLVED (the operator, TG 7258 + 7260, 2026-08-07)
1. **Substrate: systemd-timer-backed, confirmed.** The Scheduler needs **install / remove / disable** on all timer units; that timer-management is a **host action**, so it routes through the **Action Broker** as a brokered capability — the Scheduler stays low-privilege, the ORG-063 cordon model intact, no new privileged UID. **RESOLVED — broker it** (the operator TG 7260 "clever!"). Relates T-2652 (the broker provides this).
2. **Spec home: MODULAR, not `instance.yaml`.** `instance.yaml` is the central identity/topology manifest read by ~25 tools (mostly via `agent_role_map.py`, the canonical resolver) — persona bringup/runtime/restart/cutover, ticket tooling, monitors, and the provisioning/seed tools. A `schedules:` section would bloat that one heavily-depended-on file and mix scheduling policy into the identity manifest. So the schedule spec is its own modular file(s): a `schedules/` directory (global/all-persona file + per-persona files) merged by a loader — touches none of the 25 existing consumers, keeps `instance.yaml` lean.
3. **Migration scope: ALL timers in the spec, classified by SCOPE** (the operator TG 7260 — supersedes the earlier "pure-host chores stay plain" lean). Rationale: host chores must be *provisioned* on a fresh box (seeding a new Ubuntu box with Pantheon needs them), so they belong in the declarative spec too — that is what makes seeding **reproducible**. All 51 timers (census on T-2662) go in the spec, tagged **per-persona / all-persona / host**; none excluded. Dovetails with the existing seed/provisioning tools that already read `instance.yaml` (`provision_repos`, `neutralize_seed`, `seed_rebrand`) — the Scheduler becomes part of reproducible box-seeding.

Build/design tracked in **T-2662** (depends_on T-2657 ratify; relates T-2652 Action Broker for the brokered timer-management path). All three sketch questions + the substrate residual are now closed; ORG-064 is ready to firm from Proposed → decision.

## Consequences (sketch)
- Positive: one place answering "what runs, for whom, when"; first-class per-persona exceptions; new schedules are a spec edit, not hand-authored units; uniform health for the scheduling layer.
- Cost/risk: a reconciler that renders systemd units cross-account is a real surface (must be idempotent + safe to re-run); migration touches live timers (stage behind verification, one class at a time).

## Re-evaluate / next
On the operator's steer of the three open questions, this firms from sketch → decision, and implementation tickets follow (spec schema → reconciler → migrate one timer-class at a time, each verified).
