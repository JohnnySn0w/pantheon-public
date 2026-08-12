---
id: ORG-068
title: Universal system-lifecycle contract — first-class, cross-referenced, tracked for every major system
status: DRAFT — awaiting the operator ratify (S5 per ORG-038)
date: 2026-08-10
deciders: [operator, orchestrator]
supersedes: []
extends: [ORG-053, ORG-049]
labels: [org-design, lifecycle, org-053, tracking]
---

# ORG-068 — Universal system-lifecycle contract

> **STATUS: DRAFT — awaiting the operator ratify.** HO-drafted per ORG-012 (HO = primary ADR drafter). Org-identity/design ratification is S5's call per ORG-038. Coverage matrix fully populated: verified read-only audit (T-2739, 2026-08-10, file:line-evidenced) + Recovery's auth/credential-broker snapshot (ORG-051, steward Codex — to confirm at ratify).

## Context

**ORG-053 §6** (amended 2026-08-07, the operator TG 7266/7267) already establishes the universal system-lifecycle contract: *every* organ is a managed entity across its whole life — **install → configure → health-monitor → update → remove** — not a static artifact, and "a design that serves the grand goal but cannot be installed reproducibly, updated, configured, health-reported, or removed is **not done**." That text lives today as a *subsection* of the grand-goal ADR.

The operator directed (TG 2026-08-10) promoting the lifecycle idea to a **first-class, standalone, cross-referenced ADR for all major systems**, with two purposes beyond §6's statement of principle:

1. **Discoverability + enforceability** — a dedicated ADR is a design-gate agents actually cite, not a buried clause.
2. **Tracking** — "it'll be good for tracking what systems still need it too." The ADR carries a **coverage matrix** (system × lifecycle stage) that is a living punch-list of what still needs the contract.

**This ADR does NOT duplicate ORG-053 §6.** It **promotes and operationalizes** it. §6 remains the origin statement of the principle; ORG-068 is the enforceable, cross-referenced, tracked realization. (Lesson carried from the T-2683 arc, where a mailbox-specific lifecycle ADR draft — the local ORG-066 — was retired unpushed precisely because §6 already stated the principle: the value is not restating §6, it's *cross-referencing every system to it and tracking coverage*.)

## Decision

1. **The 5-stage lifecycle contract is an enforceable design-gate on every major system.** A system is **not done** until each stage is satisfied or explicitly waived-with-rationale:
   - **Install** — reproducible, convergent, manifest-driven (the ORG-049 mechanism); a clean box converges to it from the declarative substrate.
   - **Configure** — parameters are declared, not hand-set; a single source of truth.
   - **Health-monitor** — the system reports liveness/health; failure is observable (alerting wired).
   - **Update** — **auto-currency by default**: the system keeps itself current without manual intervention (auto-pull / convergent re-apply / self-update), preserving state, no rebuild-from-scratch. A manual-only upgrade path is **partial**. Per the operator (TG 2026-08-10), auto-update is a **cross-cutting requirement for every system** — a merged fix that doesn't propagate isn't in force (the T-2730/T-2743 case: the busy-defer fix existed on `main` but the consume seats ran stale clones, so the bug stayed live for them until a manual pull). A shared **fleet auto-update / currency mechanism (T-2743)** is the realization every system's update-leg plugs into; the many `◐` cells in the Update column below are exactly the "convergent re-run exists but is manual" state this closes. **Design shape** (the operator, TG 2026-08-10): *configs are isolated, everything else can be bumped up* — the mechanism auto-advances code/tooling/units while per-instance config + secrets + local state are **preserved, never clobbered by an update**, matching the ORG-049 config/code separation.
   - **Remove** — a proven teardown/decommission with rollback (the "remove leg"; ORG-067 is the worked example for the mailbox-hub identity).

2. **Every major system is enumerated and cross-referenced** to its lifecycle realization + steward (the matrix below).

3. **A coverage matrix is maintained in this ADR** as the living punch-list. Each `still-needed` / `partial` cell is a candidate follow-up ticket; the matrix is the fleet-wide analogue of the T-2683 survey's ORG-053 §6 gap table.

4. **ORG-049 is the install-leg mechanism** the contract leans on; ORG-053 §6 is the origin principle; this ADR is their cross-referenced enforcement + tracking surface.

## Major-system lifecycle coverage matrix

Legend: **✓** covered · **◐** partial · **✗** still-needed · **?** pending-input · **n/a** not-applicable. Cells are from the T-2739 verified audit (file:line evidence in that ticket's dispatch record); the matrix is a **living punch-list**, re-audited as systems evolve.

> **Consolidation note (2026-08-10, the operator TG re-cut).** The first-draft matrix over-split the runtime region into separate broker / control-plane / bringup / scheduler rows — because this ADR was drafted (8/10) without applying the **three-isolate** design its own author had already landed in **ORG-063 + ORG-064** (8/7). This matrix now reconciles to those three isolates: **Harness Manager** (how a seat runs — absorbs the old "control plane" + "persona bringup"), **Action Broker** (host-capability authority — ORG-063 renames the ORG-052 host-capability broker to this; the two are one system), and the **General Scheduler** (when things run — ORG-064; absorbs the wake/consume seam split off the Harness Manager). The credential broker stays its own row (distinct, and actually built); the mailbox identity/rename row folds into the mailbox organ (a lifecycle event, not a system). Net: 14 → 11 rows. The three-isolate names track the still-unratified ORG-063/064 — see Open items for co-ratification.

| Major system | Steward | Install | Configure | Health-mon | Update | Remove | Realization / cross-ref |
|---|---|---|---|---|---|---|---|
| Mailbox hub / organ | orchestrator | ✓ | ✓ | ◐ | ◐ | ✓ | `tools/mailbox_onboard.py` (hub-setup/provision/verify/teardown, manifest-driven), T-2683/PR #259; ORG-026. **Identity rename/cutover** (the-ancestor→post-office) is a lifecycle event on THIS organ, not its own system — ORG-067 (remove-leg design, DRAFT) + live cutover T-2693 (operator-gated) are its Remove-leg worked example |
| Harness Manager — persona seats: install/launch/supervise | orchestrator (specialist: consume) | ◐ | ✓ | ✓ | ◐ | ◐ | Per **ORG-063** (renamed from "control plane"): `persona_installer` (typed plan/apply/verify/rollback — CLI exposes plan/simulate only → Install ◐), `runtime_controller` (tmux scope), `persona_bringup.py` + `sop/standard/pantheon-bringup.md`, `consume_onboard.py` (T-2679, onboard/rollback), `pantheon-persona-consume@`, `harness_adapter`, `instance.yaml`; `consumer_liveness_monitor.py` + `agent_monitor_healthcheck`. **Wake/consume scheduling seam splits out → General Scheduler** (ORG-063 §Scheduler) |
| General Scheduler — when things run | orchestrator | ✗ | ✗ | ✗ | ✗ | ✗ | **ORG-064** (Proposed/sketch; build T-2662); no `schedules/` dir yet. The built persona wake/consume seam (`pantheon-persona-consume@`, split from Harness Manager per ORG-063) is the precursor it absorbs; the general fleet-wide scheduler is unbuilt |
| Action Broker — host-capability authority | codex/Cairn (T-2383) | ✗ | ◐ | ◐ | ✗ | ✗ | **ORG-052** (Ratified — harness-neutral host-capability broker, MCP adapters over D-Bus) **+ ORG-063** (the "Action Broker" rename + UID-cordon security model, unratified — ORG-063 defines the Action Broker AS the ORG-052 subsystem). `tools/host_control/` is a **non-actuating scaffold** (no live D-Bus/MCP). Distinct from the credential broker + Scheduler |
| Auth / credential broker | codex | ◐ | ◐ | ✓ | ◐ | ◐ | ORG-051 (Ratified 2026-07-30), T-2380 (DONE); `tools/credential_broker.py` (issue/rotate/revoke) + `credential_expiry_monitor.py` + `pantheon-credential-expiry.timer` (live/hourly, alerts the operator direct). Health-mon fully live; install/update/remove **built but not yet exercised — pending the secrets migration** to make the broker source-of-truth. Distinct from ORG-052 + ORG-063 |
| Echo (sense) | atlas | ✓ | ✓ | ✓ | ◐ | ◐ | echo-prismatic: `deploy/systemd/install-echo-unit.sh`, `config.rs`, `health.rs` (sd_notify watchdog). No CODEOWNERS — steward inferred |
| Memory / learning | orchestrator | ◐ | ◐ | ◐ | ✓ | ✗ | ORG-035 baseline built (`memory_index.py`, `memory_provenance_backfill.py`); ORG-058 exocortex design-only. Store provisioned by a **manual** bringup step, not manifest; `memory_lint.py` has no timer |
| Watch / research pipeline | specialist | ✓ | ✓ | ◐ | ◐ | ✗ | `watch_runner.py`, `knowledge_sweep`/`policy_outflow` timers; declarative topics. No scripted teardown |
| Pantheon (self-build organ) | orchestrator | ◐ | ◐ | ◐ | ◐ | ✗ | `pantheon_config_sync.py` (ff-only in-place), `install_hooks.sh`; monitor fleet (fleet_health, drift-sweep, ci-audit, `fleet_clone_currency_check` T-2798). Remove: likely n/a — needs an explicit waiver |
| Presence surfaces | unassigned | ✗ | ✗ | ✗ | ✗ | ✗ | ORG-060 (Ratified design 2026-08-02) — **not built** |
| Passport / identity / enrollment | orchestrator (arc) | ✗ | ✗ | ✗ | ✗ | ✗ | ORG-047 (Proposed/DRAFT, Recovery-drafted) — **not built**; no enrollment tool. Adjacent to the credential broker + Harness Manager seat-enrollment (three identity-flavored surfaces at different layers — group under an "identity" heading if desired; not merged) |

## Coverage gaps → candidate follow-up tickets

The living punch-list. Prioritized (built systems first — a gap on a shipped system is a real hole; unbuilt systems' gaps are just "not built yet"):

1. **Watch pipeline — Remove** (specialist): no scripted decommission of the watch/research pipeline.
2. **Memory baseline — Health + Remove** (orchestrator): `memory_lint.py` integrity check has no timer/alert (wire a monitor); no per-agent store teardown.
3. **Memory baseline — Install/Configure** (orchestrator): promote the `autoMemoryDirectory` manual harness step into the manifest so it's ORG-049-convergent (add a `memory` key to instance.yaml).
4. **Harness Manager (persona seats) — Install + Remove** (orchestrator): expose real-host `apply` in the `persona_installer` CLI (plan/simulate-only today); add a proven full-seat decommission (rollback only reverses a cutover).
5. **Action Broker (host-capability) — Install/Update/Remove** (codex/Cairn, T-2383): turn the ORG-052 scaffold into a deployed, actuating broker.
6. **Auth/credential broker — secrets migration** (codex, ORG-051): the broker is BUILT (T-2380) but not source-of-truth — install live creds → verify → revoke the per-account stopgap, exercising install/update/remove for real. Security-relevant and **gates T-2740** (encrypted off-site memory backup, sequenced after the broker is in daily use).
7. **Pantheon self-build — Remove** (orchestrator): decide n/a-with-waiver vs a real organ-teardown, and record the decision.
7. Lower priority: Echo Update + Remove (atlas — add upgrade + uninstall scripts); the convergent-re-run "Update ◐" cells (mailbox / Harness Manager) could each gain a dedicated state-preserving update leg.

## Consequences

- **Design-gate smell-test** (add to dispatch/design discipline): "for the system I'm designing/modifying, is each of install/configure/monitor/update/remove satisfied or explicitly waived-with-rationale?" A brief lifecycle line in the DECISIONS section, alongside the existing grand-goal+lifecycle intent line (ORG-053 §6).
- **The matrix drives work**: each `✗`/`◐` cell on a built system is a candidate ticket — "what still needs the contract" becomes a tracked queue, not tribal knowledge.
- **No new principle** — rests on ORG-053 §6 (principle) + ORG-049 (install mechanism); adds enforcement + cross-reference + tracking.

## Open items (to resolve at ratify)

The matrix is complete; these are confirmations for the operator's ratify pass, not blockers to reviewing it:

- **Codex confirmation** — the auth/credential-broker row (ORG-051) reflects Recovery's operational snapshot; steward is Codex, who should confirm/refine that row at ratify.
- **Steward gaps** — Presence + Passport are unassigned/arc-only; ratify is a natural moment to name stewards.
- **Formal §6 re-ratification** — ORG-053 §6 is flagged "for formal re-ratification confirmation at the operator's convenience"; ratifying ORG-068 is a natural moment to confirm §6 alongside.
- **Three-isolate naming (ORG-063/064 co-ratification)** — the runtime region now uses the **Harness Manager / Action Broker / General Scheduler** names from ORG-063 + ORG-064, both of which are Orchestrator-authored + operator-directed (8/7) but still **unratified**. Cleanest to ratify ORG-063 + ORG-064 alongside ORG-068 (or confirm the names here). If you'd rather this matrix stay on the pre-rename names until those ratify, say so and I revert the labels (the row *consolidation* stands either way).
- **Gap-tickets** — the coverage-gaps list is the tracked punch-list; individual follow-up tickets get filed post-ratify (so the operator can reprioritize/waive first).
