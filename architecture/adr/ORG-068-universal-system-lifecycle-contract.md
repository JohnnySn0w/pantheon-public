---
id: ORG-068
title: Universal system-lifecycle contract — first-class, cross-referenced, tracked for every major system
status: Ratified 2026-08-12 by the operator (Telegram 7738). Amended 2026-08-26 by the [[ORG-080]] + [[ORG-081]] + [[ORG-082]] bundle (operator-ratified via Telegram 8612). Matrix retires in favor of registry/systems.yaml ([[ORG-081]]); health-leg coverage predicate refined to a three-part rule (emitter install + reporting AND invariant-list-id/digest bound between verify and emitter AND collector attachment + proven alert delivery, OR an explicit waiver acknowledging E9.C3 non-cover), matching [[ORG-080]] D-AB and [[ORG-081]] D-11; E9.C3 ratified text is unchanged. No existing element ids change.
date: 2026-08-10
deciders: [operator, orchestrator]
supersedes: []
extends: [ORG-053, ORG-049]
labels: [org-design, lifecycle, org-053, tracking]
build_disposition: needs-build
scoping_ticket: T-2742
no_registry_row_waiver: >-
  Universal lifecycle-contract ADR + 2026-08-26 amendment. The amendment does
  not add or rename components; it retires the prose matrix in favor of the
  [[ORG-081]] registry and tightens the health-leg coverage predicate.
  Components are registered by the ADRs that name them ([[ORG-080]] F4).
  Enforced when [[ORG-081]]'s validator lands (T-2667 tranche a).
# elements: — ORG-065 machine index of "Coverage gaps -> candidate
# follow-up tickets" item 4 (T-2946 Tranche A, pair 7/8). E4 mirrors the
# native gap number; text opens with "gap 4" verbatim. Scope: gap 4
# only. NOTE: this same patch also renumbers the punch-list's duplicate
# "7." (two items both numbered 7) -- the second item, "Lower priority:
# Echo Update + Remove...", becomes "8." -- a non-semantic sequencing
# fix (no wording/scope change to either item), a precondition per the
# T-2946 dispatch brief, not itself an element. Added by HO; no
# obligation content changed.
#
# T-3205 Tranche B additions (2026-08-25): E8 continues the SAME
# native-mirrored "Coverage gaps" group as E4 (gap 8, the item the
# T-2946 pair-7 renumber created); text opens with "gap 8" verbatim.
# 1-8 stay RESERVED for that native-mirrored gap-list group (gaps 1-3
# and 5-7 are not yet minted; add them on demand, same rule). E9 and
# E10 are TWO NEW groups with no native numbers to mirror (E9 = "##
# Decision" item 1's 5-stage lifecycle contract, whose 5 bullets become
# clauses E9.C1..E9.C5; E10 = the unnumbered "Modular-subsystem
# invariants + uniform health interface" section, whose 3 named
# invariants become clauses E10.C1..E10.C3) — numbered to CONTINUE FROM
# 9, after the gap-list group's reserved 1-8 range, a deliberate
# deviation from document order (Decision item 1 appears earlier in the
# file than the coverage-gaps list) justified by the ALREADY-LANDED,
# locked E4: renumbering the gap-list group to make room would violate
# "never renumber existing ids". Decision items 2-4 are NOT elementized
# this pass (no ticket in this tranche cites them; add on demand per
# the same "elements only where a leg has none" rule). Each clause's
# text opens with its own bold-label lead verbatim ("Install —",
# "Externally legible —", etc.) since there is no numeric/letter native
# id at that granularity. Added by HO; no obligation content changed.
elements:
  - {id: E4, text: "gap 4 (Harness Manager — Install + Remove): expose real-host apply in the persona_installer CLI (plan/simulate-only today); add a proven full-seat decommission (rollback only reverses a cutover)"}
  - {id: E8, text: "gap 8 (Echo — Update + Remove, lower priority): add upgrade + uninstall scripts (atlas); the convergent-re-run \"Update ◐\" cells (mailbox / Harness Manager) could each gain a dedicated state-preserving update leg"}
  - {id: E9, text: "1. The 5-stage lifecycle contract is an enforceable design-gate on every major system: a system is not done until each stage is satisfied or explicitly waived-with-rationale", clauses: [{id: E9.C1, text: "Install — reproducible, convergent, manifest-driven (the ORG-049 mechanism); a clean box converges to it from the declarative substrate"}, {id: E9.C2, text: "Configure — parameters are declared, not hand-set; a single source of truth"}, {id: E9.C3, text: "Health-monitor — the system reports liveness/health; failure is observable (alerting wired)"}, {id: E9.C4, text: "Update — auto-currency by default: the system keeps itself current without manual intervention (auto-pull / convergent re-apply / self-update), preserving state, no rebuild-from-scratch; a manual-only upgrade path is partial. Design shape: configs are isolated, everything else can be bumped up — the mechanism auto-advances code/tooling/units while per-instance config + secrets + local state are preserved, never clobbered by an update, matching the ORG-049 config/code separation"}, {id: E9.C5, text: "Remove — a proven teardown/decommission with rollback (the \"remove leg\"; ORG-067 is the worked example for the mailbox-hub identity)"}]}
  - {id: E10, text: "Modular-subsystem invariants + uniform health interface: every self-contained/modular subsystem (the Action Broker; the Harness Manager and its subcomponents) must satisfy three invariants (TG 7236), with a uniform D-Bus health interface (PULL = Properties Get, PUSH = PropertiesChanged) as the externally-legible mechanism", clauses: [{id: E10.C1, text: "Externally legible — health-reporting on its subcomponents (a caller/monitor can read whether each part is healthy), not just per-unit systemd state buried in journals"}, {id: E10.C2, text: "Restart-fixable — if restarted without warning, it just recovers; no manual repair to come back up"}, {id: E10.C3, text: "Stateless across sessions — no runtime state that needs continuity between sessions; purely functional, state lives in the durable substrate"}]}
---

# ORG-068 — Universal system-lifecycle contract

> **STATUS: RATIFIED 2026-08-12 by the operator (TG 7738), open items acknowledged at ratify.** HO-drafted per ORG-012. Coverage matrix fully populated: verified read-only audit (T-2739, 2026-08-10, file:line-evidenced) + Recovery's auth/credential-broker snapshot (ORG-051, steward Codex).
>
> **Open-items disposition at ratify (T-2741):** Codex row-confirmation → routed post-ratify (T-2742); **CLOSED 2026-08-12 — Codex REFINED rather than confirmed** (mailbox 01786506033502528404): all five credential-broker cells now ◐, Health downgraded from ✓ (the live monitor watches legacy auth files, not broker runtime), prose reworded to separate tested primitives from operational legs; upgrade criteria folded into T-2870. Steward gaps (Presence/Passport) + ORG-053 §6 formal confirmation + ORG-063/064 naming co-ratification → surfaced to the operator as compact decisions alongside this ratify; matrix names stand unless he says otherwise. Gap-tickets → filed post-ratify per this ADR's own priority order (T-2742); the operator may waive/reprioritize any.

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

> **Steward** (editorial definition, added 2026-08-12 at the operator's question, TG 7742): the named accountable owner of a system's ongoing life — answers for its health, reviews changes to it, carries its roadmap, and owns closing its lifecycle-leg gaps in this matrix. Not necessarily its builder; reassignable cheaply. Unbuilt/arc-only systems may leave the steward unassigned until their build starts.

Legend: **✓** covered · **◐** partial · **✗** still-needed · **?** pending-input · **n/a** not-applicable. Cells are from the T-2739 verified audit (file:line evidence in that ticket's dispatch record); the matrix is a **living punch-list**, re-audited as systems evolve.

> **Consolidation note (2026-08-10, the operator TG re-cut).** The first-draft matrix over-split the runtime region into separate broker / control-plane / bringup / scheduler rows — because this ADR was drafted (8/10) without applying the **three-isolate** design its own author had already landed in **ORG-063 + ORG-064** (8/7). This matrix now reconciles to those three isolates: **Harness Manager** (how a seat runs — absorbs the old "control plane" + "persona bringup"), **Action Broker** (host-capability authority — ORG-063 renames the ORG-052 host-capability broker to this; the two are one system), and the **General Scheduler** (when things run — ORG-064; absorbs the wake/consume seam split off the Harness Manager). The credential broker stays its own row (distinct, and actually built); the mailbox identity/rename row folds into the mailbox organ (a lifecycle event, not a system). Net: 14 → 11 rows. The three-isolate names track the still-unratified ORG-063/064 — see Open items for co-ratification.

| Major system | Steward | Install | Configure | Health-mon | Update | Remove | Realization / cross-ref |
|---|---|---|---|---|---|---|---|
| Mailbox hub / organ | orchestrator | ✓ | ✓ | ◐ | ◐ | ✓ | `tools/mailbox_onboard.py` (hub-setup/provision/verify/teardown, manifest-driven), T-2683/PR #259; ORG-026. **Identity rename/cutover** (the-ancestor→post-office) is a lifecycle event on THIS organ, not its own system — ORG-067 (remove-leg design, DRAFT) + live cutover T-2693 (operator-gated) are its Remove-leg worked example |
| Harness Manager — persona seats: install/launch/supervise | orchestrator (specialist: consume) | ◐ | ✓ | ✓ | ◐ | ◐ | Per **ORG-063** (renamed from "control plane"): `persona_installer` (typed plan/apply/verify/rollback — CLI exposes plan/simulate only → Install ◐), `runtime_controller` (tmux scope), `persona_bringup.py` + `sop/standard/pantheon-bringup.md`, `consume_onboard.py` (T-2679, onboard/rollback), `pantheon-persona-consume@`, `harness_adapter`, `instance.yaml`; `consumer_liveness_monitor.py` + `agent_monitor_healthcheck`. **Wake/consume scheduling seam splits out → General Scheduler** (ORG-063 §Scheduler) |
| General Scheduler — when things run | orchestrator | ✗ | ✗ | ✗ | ✗ | ✗ | **ORG-064** (Proposed/sketch; build T-2662); no `schedules/` dir yet. The built persona wake/consume seam (`pantheon-persona-consume@`, split from Harness Manager per ORG-063) is the precursor it absorbs; the general fleet-wide scheduler is unbuilt |
| Action Broker — host-capability authority | codex/Cairn (T-2383) | ✗ | ◐ | ◐ | ✗ | ✗ | **ORG-052** (Ratified — harness-neutral host-capability broker, MCP adapters over D-Bus) **+ ORG-063** (the "Action Broker" rename + UID-cordon security model, unratified — ORG-063 defines the Action Broker AS the ORG-052 subsystem). `tools/host_control/` is a **non-actuating scaffold** (no live D-Bus/MCP). Distinct from the credential broker + Scheduler |
| Auth / credential broker | codex | ◐ | ◐ | ◐ | ◐ | ◐ | ORG-051; T-2380 host-file **primitives** (issue/verify/atomic rotate/cooperative lease revoke); T-2868 reference manifest + plan/check CLI. Legacy OAuth expiry monitoring is live/hourly, but broker runtime/consumer/reaper monitoring is not. Secret install, operational rotate/revoke, first real fd consumer, and legacy-material retirement remain gated on T-2869/T-2870. *(Row steward-refined by Codex at ratify, 2026-08-12 — Health downgraded ✓→◐: the live timer watches legacy auth files, not broker health; CLI install/rotate/revoke are gated stubs (`credential_broker_cli.py:36-42`), so "built-not-exercised" overstated primitives as operational legs. Cells upgrade only as T-2870's acceptance proves each leg.)* Distinct from ORG-052 + ORG-063 |
| Echo (sense) | atlas | ✓ | ✓ | ✓ | ◐ | ◐ | echo-prismatic: `deploy/systemd/install-echo-unit.sh`, `config.rs`, `health.rs` (sd_notify watchdog). No CODEOWNERS — steward inferred |
| Memory / learning | orchestrator | ◐ | ◐ | ◐ | ✓ | ✗ | ORG-035 baseline built (`memory_index.py`, `memory_provenance_backfill.py`); ORG-058 exocortex design-only. Store provisioned by a **manual** bringup step, not manifest; `memory_lint.py` has no timer |
| Watch / research pipeline | specialist | ✓ | ✓ | ◐ | ◐ | ✓ | `watch_runner.py`, `knowledge_sweep`/`policy_outflow` timers; declarative topics. Remove: `tools/watch_pipeline_decommission.py` (dry-run-default teardown, per-leg opt-in, rollback via tools/systemd/ reinstall), T-2887 |
| Pantheon (self-build organ) | orchestrator | ◐ | ◐ | ◐ | ◐ | ✗ | `pantheon_config_sync.py` (ff-only in-place), `install_hooks.sh`; monitor fleet (fleet_health, drift-sweep, ci-audit, `fleet_clone_currency_check` T-2798). Remove: likely n/a — needs an explicit waiver |
| Presence surfaces | unassigned | ✗ | ✗ | ✗ | ✗ | ✗ | ORG-060 (Ratified design 2026-08-02) — **not built** |
| Passport / identity / enrollment | orchestrator (arc) | ✗ | ✗ | ✗ | ✗ | ✗ | ORG-047 (Proposed/DRAFT, Recovery-drafted) — **not built**; no enrollment tool. Adjacent to the credential broker + Harness Manager seat-enrollment (three identity-flavored surfaces at different layers — group under an "identity" heading if desired; not merged) |

## Coverage gaps → candidate follow-up tickets

The living punch-list. Prioritized (built systems first — a gap on a shipped system is a real hole; unbuilt systems' gaps are just "not built yet"):

1. **Watch pipeline — Remove** (specialist): no scripted decommission of the watch/research pipeline. → **RESOLVED 2026-08-27 (Orchestrator, at T-2891 bookkeeping): T-2887 built the leg at `tools/watch_pipeline_decommission.py` + `tools/test_watch_pipeline_decommission.py` (13 tests, green), landed on main at commit `913e981be2`. Row above updated ✗→✓.**
2. **Memory baseline — Health + Remove** (orchestrator): `memory_lint.py` integrity check has no timer/alert (wire a monitor); no per-agent store teardown.
3. **Memory baseline — Install/Configure** (orchestrator): promote the `autoMemoryDirectory` manual harness step into the manifest so it's ORG-049-convergent (add a `memory` key to instance.yaml).
4. **Harness Manager (persona seats) — Install + Remove** (orchestrator): expose real-host `apply` in the `persona_installer` CLI (plan/simulate-only today); add a proven full-seat decommission (rollback only reverses a cutover).
5. **Action Broker (host-capability) — Install/Update/Remove** (codex/Cairn, T-2383): turn the ORG-052 scaffold into a deployed, actuating broker.
6. **Auth/credential broker — secrets migration** (codex, ORG-051): the broker is BUILT (T-2380) but not source-of-truth — install live creds → verify → revoke the per-account stopgap, exercising install/update/remove for real. Security-relevant and **gates T-2740** (encrypted off-site memory backup, sequenced after the broker is in daily use).
7. **Pantheon self-build — Remove** (orchestrator): decide n/a-with-waiver vs a real organ-teardown, and record the decision. → **RESOLVED 2026-08-12 (Orchestrator, at T-2742 execution): n/a-with-waiver.** Removing the org's own substrate is dissolving the org — an operator act on the whole host, already covered by DO snapshots + the T-2178 off-box state backup + per-repo GitHub/Forgejo replication. A scripted "remove pantheon" tool would have no legitimate caller and is pure footgun surface; the waiver is the safer contract. Revisit only if a partial-organ teardown (a *subsystem* remove) is ever actually needed — those are the per-system Remove legs this matrix already tracks.
8. Lower priority: Echo Update + Remove (atlas — add upgrade + uninstall scripts); the convergent-re-run "Update ◐" cells (mailbox / Harness Manager) could each gain a dedicated state-preserving update leg.

## Modular-subsystem invariants + uniform health interface (rehoused from the ORG-063 draft, operator-directed)

> **Provenance:** authored in the ORG-063 bundled draft as §4b + §7 (the operator TG 7236 / 7239–7244, recovered verbatim from `1f9c2d43ac^`); the ORG-063 rewrite kept only the security posture and flagged these for relocation. **the operator dispositioned at the ORG-063 ratify (T-2657 comment, 2026-08-12): "rehouse them under the rest of the lifecycle/health adr work instead of here, but point to them."** This ADR is that home — the invariants are the *monitor* leg's acceptance bar, and the health interface is its mechanism. operator-directed; carries his authority as ratifier.

**Invariants every modular subsystem MUST satisfy (TG 7236).** A self-contained/modular subsystem (the Action Broker; the Harness Manager and its subcomponents) is only actually modular if it is:
- **Externally legible** — health-reporting on its subcomponents (a caller/monitor can read whether each part is healthy). NOT just per-unit systemd state buried in journals — a legible health signal.
- **Restart-fixable** — if restarted without warning, it just recovers. No manual repair to come back up.
- **Stateless across sessions** — no runtime state that needs continuity between sessions; purely functional. State lives in the durable substrate (manifest, mailbox, ledger), not in a live process's memory.

These are acceptance criteria, not aspirations: a subsystem that can't be restarted blind, or that hides its health, or that carries session-continuity state, is not done. Known gap at rehouse-time: the Harness Manager only partially meets these (generic-interface→harness-adapter exists but isn't uniform; health is per-unit systemd + OnFailure with no unified layer) — closing that is control-structure-plan work.

**Uniform health interface (TG 7239–7244) — the externally-legible half, designed:** every subcomponent (eventually every grand-goal system process, TG 7242) exposes "am I healthy? + why" as **D-Bus properties** — PULL = `Get` (dashboard/remediation on-demand), PUSH = subscribe to `PropertiesChanged` (Recovery alerting on unhealthy transitions). One source of truth, two native read modes; after a restart a consumer re-`Get`s current truth, so stateless + restart-fixable fall out by construction. Health rides the same ORG-052/Action-Broker system D-Bus; a thin bridge feeds the web view, landing concretely as the kino **"Health" page** (T-2660) and scaling to all processes. The generic-interface-uniformity fix is the *same work* as this health interface: the generic interface both controls the subcomponent and answers its health.

## Amendment — 2026-08-26 (germination-engine arc, [[ORG-080]] / [[ORG-081]] / [[ORG-082]])

**Status: PROPOSED amendment, effective with bundle adoption.** The bundle is [[ORG-080]] + [[ORG-081]] + [[ORG-082]] + this amendment + the T-0753 supersession. The operator confirmed the germination-engine design structure on 2026-08-26 (TG 8541 / 8542) on `notes/2026-08-24-seed-installer-system-design-draft.md` v0.9 (pantheon-config main 5a4e23918); Cairn REBOUNDed the ADR bundle 2026-08-26 (T-3261, 6 BLOCK / 5 FIX-SOON / 2 NOTE, report at `knowledge/episodic/2026-08-26-germination-engine-adr-cairn-pass/report.md`); v2 fold answers Cairn's BLOCK-3 by tightening this amendment's item 2. Two obligations of this ADR are amended by that arc; **no existing element id (E4, E8, E9.C1–C5, E10.C1–C3) changes text or scope, and E9.C3's ratified text ("Health-monitor — the system reports liveness/health; failure is observable (alerting wired)") is preserved unchanged.**

**1. The prose coverage matrix retires in favor of a validated data plane.** The [[ORG-081]] System Registry ADR ratifies `registry/systems.yaml` — one record per component with a closed status enum (`covered` + evidence; `partial`/`missing` + `gap:` + `ticket:`; `partial_by_design`/`not_applicable` + `waiver:`), a schema validator that CI runs on every landing, and a render tool that produces the matrix on demand (`germinate registry render`). Once the [[ORG-081]] build lands (T-2667 tranche a), the "Major-system lifecycle coverage matrix" section above becomes a pointer to the rendered view; the ratified matrix text stays in place as an historical artifact, but `registry/systems.yaml` is authoritative for what any tool consumes. This is [[ORG-080]] F3 (system registry is a system) and D-D (registry is data; the matrix is rendered).

**2. The health-leg coverage predicate is three-part; emitter-install alone does NOT satisfy unchanged E9.C3.** The 5-stage principle in E9 stands: every system carries a lifecycle contract across install / configure / **health-monitor** / update / remove. What the amendment clarifies is the **coverage predicate** for the health cell: a component's `legs.health.status` in [[ORG-081]]'s registry promotes to `covered` only when **all three** of the following are proven simultaneously — a two-of-three shortcut is not admissible.

  - **(a) Emitter installed + reporting.** The component's emitter unit is installed with `OnFailure=` + a D-Bus exporter registered on the system bus; the emitter's `Get` returns a valid `pantheon-health/v1` property set (Status, Reasons, invariant_list_ref); an unhealthy transition produces a `PropertiesChanged` signal. This is the install-leg receipt.
  - **(b) Invariant-list binding.** The emitter's `invariant_list_ref` and the component's `verify` `invariant_list_ref` resolve to the same `<id>@<digest>`; the [[ORG-081]] validator refuses any drift between the two. This is the machine-enforced form of the single-vocabulary rule — one invariant list, two consumers (`verify` invokes it point-in-time; the emitter reports its evaluation continuously). Drift is not a warning; it is a landing block.
  - **(c) Collector attachment + proven alert delivery.** The health-collector ([[ORG-076]]) is subscribed to this emitter, AND at least one unhealthy transition has been observed at the operator surface (Recovery alert, kino Health page entry, page-route delivery — whichever the component's routes name). This proves the alert path is not orphaned. Until (c) is proven, the cell is `partial` with `gap: "collector attachment / alert delivery unproven" + ticket: <id>`; explicit `partial_by_design` requires a `waiver:` that names E9.C3 by id and acknowledges the ratified obligation is not being covered for this component.

  An orphan emitter — installed, reporting, invariant-bound, but with no collector subscription and no proven alert delivery — satisfies (a) and (b), but does NOT satisfy E9.C3 (which requires failure be observable AND alerting wired). Such a component's cell is `partial`, never `covered`. The uniform health interface described above (D-Bus properties, `Get` + `PropertiesChanged`) is the mechanism the emitter uses to satisfy (a); the [[ORG-076]] episode store is where the plane's continuous observation lives; the [[ORG-076]] collector is the subscriber that satisfies (c). Together these three predicates preserve E9.C3's ratified obligation intact across the engine's install-leg / plane-continuous-observation seam. [[ORG-080]] D-J + D-AB; [[ORG-081]] §Health field records emitter obligation + D-11.

  **Distinction from the health-plane's runtime verdict.** The `legs.health` cell above is the *installer's obligation coverage* — did the component wire itself into the plane correctly and prove one alert delivered end-to-end. The plane's *runtime verdict* on the component ("is it healthy right now") lives in the [[ORG-076]] episode store and is a different vocabulary. A component whose emitter is installed and correctly reporting `unhealthy` has `legs.health.status: covered` (all three predicates met); the "component is unhealthy right now" fact belongs to the plane, not this cell.

**3. The two amendments are congruent with E9 and E10, not in tension.** E9.C3 stands unchanged; item 2 above is a coverage-predicate refinement of what "failure is observable, alerting wired" *proves* for the installer-side obligation, not a rewrite of the clause. E10.C1 ("Externally legible — health-reporting on its subcomponents") is satisfied by the emitter's D-Bus surface — the (a) predicate above. E10.C2 (restart-fixable) and E10.C3 (stateless across sessions) apply to the component itself; the germination engine's `plan`-time enforcement of `crash_contract` on every mutating op ([[ORG-080]] D-X) is the executor-side complement of E10.C2 — a restart-fixable component whose install leg cannot survive a crash mid-execution is still incomplete, and the engine refuses such an op at `plan`.

**4. What does NOT change.** Every ratified element (E4, E8, E9, E10 and their clauses) keeps its text and scope; **E9.C3's text is preserved unchanged**; the ORG-065 machine index behavior is unchanged. The five-stage lifecycle principle is unchanged. The "waiver-with-rationale" discipline is unchanged and now maps cleanly onto [[ORG-081]]'s `partial_by_design` / `not_applicable` + `waiver:` fields. The E4 (harness-manager gap 4) and E8 (echo gap 8) obligations continue to track through [[ORG-081]]'s per-leg `partial` + `gap:` + `ticket:` cells as [[ORG-080]] is built out under T-2667.

**Build order for this amendment.** (a) the [[ORG-080]] + [[ORG-081]] + [[ORG-082]] + this amendment + T-0753 supersession bundle ratifies together; (b) [[ORG-081]] schema/validator land + `registry/systems.yaml` is populated from the current matrix (this IS the retrofit — the matrix text becomes the source for the yaml, not vice versa); (c) render tool lands + CI-wires an auto-published rendered view; (d) this section's matrix table is superseded by a pointer to the rendered view; (e) the ratified matrix text remains as an appendix so the historical shape is preserved.

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
