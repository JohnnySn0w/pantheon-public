---
aliases: [ORG-076]
title: "Fleet health-and-repair system — detect, dedupe into episodes, emit one repair ticket, repair from a different harness"
status: "Ratified 2026-10-07 by the operator (Telegram 11391-11395, ticket walk, carrier T-2944). Ratified at rev 4 together with RFC-004; ORG-096 and ORG-097 were split out of the package at his direction (TG 11395, 'should not ride-along') and ratify or stay adopted on their own track. Open question 1 ruled TG 11361/11367: Consultant keeps the claude-side repair duty for now. Build tranche T-2941 is unblocked."
date: 2026-08-12
revised: 2026-10-07
author: Orchestrator (head_engineering_orchestrator)
steward: recovery
relates: ORG-003, ORG-019, ORG-041, ORG-048, ORG-052, ORG-053, ORG-063, ORG-065, ORG-068, ORG-080, ORG-096, ORG-097, ORG-100
build_disposition: needs-build
scoping_ticket: T-2941
ratifiable_files: [pantheon-config:architecture/adr/ORG-076-fleet-health-and-repair-system.md, pantheon-config:architecture/rfc/RFC-004-fleet-health-and-repair-mechanism.md]
---
# ORG-076 — Fleet health-and-repair system

**Status:** **Ratified** 2026-10-07 by the operator (TG 11391-11395; carrier T-2944), at rev 4 with [[RFC-004]] (the mechanism). [[ORG-096]] (the rebase edge of delivery) and [[ORG-097]] (the external-authority signal leg) were split out of this package at his direction (TG 11395). **Steward:** recovery. **Build:** T-2941 is unblocked.

## Intent

The fleet should **heal itself when a measurable failure occurs, without a human in the loop**, and its ability to repair should **survive the loss of any single harness**. This serves resilience: a system meant to run unattended cannot depend on someone being awake when it breaks. It also serves P1: health and repair become a mechanism rather than a habit of noticing. [[ORG-068]] says every organ must be monitorable and repairable; this ADR says how the fleet acts on that monitoring.

## Context

The consume control plane can already fail over when it *measures* a seat as capped (ORG-052). The gap is the case where it **cannot measure** at all. The quota probe scrapes a TUI banner. When the read fails, the correct response is not to swap, but the fallback is a page that waits for a human. Part of today's repair capability also lives on the seats that break. The root problem: **the fleet cannot recover from a fault it can only detect, because recovery is either deferred to a human or hosted on the faulty component.**

## Decision

The system is five contracts plus an operator plane. The sections below state the decisions. [[RFC-004]] holds the state machines, field lists, mappings and tests, under the same section numbers.

### 1. Detectors measure; they never act

A detector measures a subsystem and returns one of three verdicts: `healthy`, `broken` (the measurement worked and found a bad state) or `unknown` (the measurement itself failed). `unknown` is a first-class verdict, not an error path. Verdicts never act directly. They feed a per-fault **episode** that opens only after N consecutive non-healthy verdicts and closes only after M consecutive healthy ones, so a flapping signal cannot cause a storm of transitions. A healthy verdict closes only episodes that the same detector opened. For components the germination engine installs, the detector's input is the [[ORG-080]] health collector (§5.5).

### 2. One durable episode, one ticket

A single-writer, durable episode store is the deduplication record. Each episode gets an idempotency key before any emission is attempted. A ticket counts as delivered only once it is **committed and pushed**, not when it exists in the working tree. Startup reconciliation checks the committed ticket corpus by that key. The result is **exactly-once-effective** emission: one repair ticket per fault episode, whatever point a crash lands on. A periodic reconciler also catches three cases: tickets resolved while the fault persists, faults that healed while their ticket was open, and orphaned tickets. The rebase edge of delivery belongs to [[ORG-096]].

### 3. Repair tickets carry structured coordinates

The episode store emits runtime repair tickets with structured frontmatter: fault, episode key, domain, invariant binding, verdict, the exact repair action, redacted evidence and routing. Build tickets (filed against tranches) are a separate class. They link to this ADR through [[ORG-065]] edges and never carry the runtime fields. An `unknown` ticket is diagnosed before anything else; a `broken` ticket opens at its fault's playbook. The hard page stays as the backstop.

### 4. Responsibility model — repair is a System-domain duty, held by seats under grants

Rev 4 replaces rev 3.1's "per-harness repair persona" tier with [[ORG-100]]'s vocabulary. **Repair is not a role class or a persona.** It is a **duty**, which a seat on an ORG-100 role holds under a ratified grant. Ownership sits in the **System domain**, whose key seat is `head_system_orchestrator`.

| Responsibility | ORG-100 role class | Seating | Seat today |
|---|---|---|---|
| Steward: answers for the system, reviews changes, closes its [[ORG-068]] lifecycle gaps | `head_system_orchestrator` (key) | full | Recovery |
| Detect and reconcile: detectors, the health collector, the episode store, the reconciler | System domain, owned by `head_system_orchestrator` | full | Recovery |
| Repair on the same harness (a wedged peer while its harness is up) | `head_system_orchestrator`; `system_orchestrator` regulars add capacity | full / partial | Recovery |
| Repair across harnesses, when **codex** is down | `head_system_orchestrator` (claude-hosted) | full | Recovery |
| Repair across harnesses, when **claude** is down | the repair duty on a codex-hosted seat | per seat | Consultant, under the [[ORG-048]] grant (see Open question 1) |
| Out-of-band witness: watches the episode-store heartbeat from another failure domain | `head_auditor` (observes from outside, does not repair through this path) | full | Consultant ([[ORG-048]] D2 watcher) |
| Build: tranche tickets, implementation, merge | `head_engineering_orchestrator`, with `engineering_orchestrator` regulars | full / partial | Orchestrator (artisan and mason when unpaused) |
| Grant ratification, pages, mute | Operator | — | the operator |

Rules that follow from the model:

- **A duty binds by grant, not by role.** A seat holds the repair duty only through its own ratified privilege grant on the [[ORG-048]] pattern: spec review before the grant, then a supervised first fire. This ADR fixes the shape of the grant and grants nothing itself.
- **Least privilege.** Duty holders run only procedures from `sop/remediation/` that have a proven rollback, through narrow per-seat sudoers. They get no read access to other seats' credentials (the T-1734/T-1598 S3* posture) and do not go through the Action Broker ([[ORG-063]] §4). An action without a proven rollback is a bubble-up by definition.
- **Bounded attempts, independent escalation.** Each episode allows a set number of attempts. Each attempt is recorded in the episode and in the acting seat's ledger. When attempts run out, escalation uses a channel other than the failed harness.
- **Escalation follows seating.** `system_orchestrator` regulars (partial seating) escalate to the key seat by ticket and mailbox (ORG-100 §2), not to the Operator. The page backstop is an alert mechanism, not seat chatter, so it applies to every seat.
- **Growth path.** Repair capacity grows by adding `system_orchestrator` regulars, each with its own grant. No new role class is needed.

### 5. Topology — repair never lives on the harness that is down

A repair ticket routes to a duty holder on a harness **other than** the faulted one. If none is live, it goes to any live duty holder, and if none of those is live either, to the page. The claim is deliberately narrow (the operator, TG 7875): the system removes single points of failure **at the harness/agent tier**. It is an on-host mutual-repair system. The shared substrate (host, systemd, git path, mailbox, network) is not covered, and off-host disaster recovery is out of scope. The fault-domain matrix is in [[RFC-004]] §5.

### 5.5. Health-plane seam

For components the germination engine installs, ORG-076 reads the [[ORG-080]] collector's subscription rather than polling them itself. The collector's five-value status maps onto the three verdicts (mapping in [[RFC-004]] §5.5). The seam carries data inward only: repair never travels back through the collector. ORG-076 holds itself to this seam too: its own detectors, store and reconciler report into the collector like any other component.

### 6. Acceptance — proven by tests at the boundaries

The build has to prove these behaviors with boundary tests, not argue them from the design: concurrency, crash boundaries, kill-injection at every delivery phase, flapping, stale tickets, an unavailable repair target, degraded shared dependencies, restart persistence, an external liveness witness (§6.9; its signal leg is [[ORG-097]]), the collector's initial read, restart and missed signals, loss of one seat's user bus, fail-closed behavior while the collector is down, and evidence redaction, size cap and deferred routing. The 18 numbered tests are in [[RFC-004]] §6.

## 7. Operator plane — health view + per-persona notification governance

*(Carried verbatim from rev 3.1. This section is shipped behavior, cited by the tools as "ORG-076 §7".)*

> **Folded in 2026-08-18 by Recovery (steward) from the retired standalone ORG-077, at the operator's direction (TG 6093–6102). Rev-3 should review + incorporate this section.** ORG-076 §1–6 are the *autonomous* leg (detect → episode → repair without a human); this section is the *operator* leg — the human-facing view of the same health signals plus governance over what those signals page. Same steward; one system, two legs.

**Motivation.** Fleet health is legible to the operator only as a stream of per-monitor Telegram alerts (un-mutable except by editing systemd drop-ins) plus ad-hoc CLI probes — there is no glanceable "how is the fleet doing" view, and no way to say "this persona is intentionally offline — stop paging me about it" short of a per-monitor edit. Two ad-hoc per-monitor skips already exist (`CHANNEL_LIVENESS_SKIP`; `PANTHEON_CRED_ALERT_SKIP`, T-2973) — the second is the tell that the piecemeal approach doesn't scale. Serves D1 (operator bandwidth) + ORG-068 (externally-legible health).

**7.1 Per-persona notification mute (the control).** A single per-persona mute registry in a cross-account-readable location, plus a shared helper `is_muted(persona)` that **every persona-scoped alert path consults before sending**. Muting a persona silences **all** its fleet alerts uniformly (MVP: uniform/all-classes per the operator; per-class granularity later). **Supersedes and absorbs** the two ad-hoc skips (their members migrate in; the per-monitor envs are removed). **Invariant — mute ≠ blind:** a muted persona is still classified + trace-logged at every monitor (only the operator page is withheld), so a persona that comes back online was never silently invisible (cf. §"correct no-op hides the next failure").

**7.2 Per-persona health view (the observation).** A glanceable rollup of each persona's health (up / wedged / auth-dead / offline / cliff-approaching / …), fed by the existing monitor signals (`agent_monitor_healthcheck.py` + per-monitor state) — the externally-legible health ORG-068 requires, aggregated per persona.

**7.3 Home — a Health Plane section in kino (ORG-006).** The health view + the mute toggle live as a new section in the operator PWA, not a separate dashboard. A CLI (`notify_mute.py mute|unmute|list`) is the headless equivalent. The health view aggregates from §5.5's collector subscription (the same data ORG-076's autonomous §1-6 leg consumes) — kino's Health Plane section is an **operator legibility affordance on top of the collector's subscription**, not a distinct plane element; one collector, two consumers.

**Boundary.** The mute registry governs operator-facing pages only — it does **not** disable detection or §1–6 autonomous repair (a muted persona is still detected + auto-repaired, just not paged about). The §132 "page backstop is retained" is what the mute governs.

**Build tranche (decomposed by grant — kino/`pantheon-pwa` is HO-only rw):** **T-2978** (→ recovery): the mute registry + `is_muted` + retrofit of the ~25 alert paths (gate at shared chokepoints — `route_failure_alert` + per-monitor sends) + CLI; unify the two ad-hoc skips. **T-2979** (→ orchestrator, depends_on T-2978): the kino Health Plane section — health view + mute toggle, reading T-2978's registry.

## Non-goals

- Not a replacement for ORG-052 quota failover. This is the layer above it.
- Not swap-on-unknown. A repair ticket (diagnose, then act) is chosen over blind swapping (TG 7865/7868).
- The page backstop stays.
- Off-host disaster recovery and external uptime monitoring are out of scope.
- Per-subsystem detector implementations are build work, not decisions.
- No new ORG-100 role class, and no new seat (see Open question 1).

## Steward and lifecycle ([[ORG-053]] / [[ORG-068]])

**Steward: recovery**, the `head_system_orchestrator`. The System domain is its whole remit, so the fit is structural. The operator delegated the call to HO (TG 7875), and it can be reassigned cheaply under ORG-068. The system is itself an organ:

- **Install.** Detectors and the reconciler ship as `systemd --user` units through the standard plan/apply/verify installer.
- **Configure.** Thresholds, cool-downs, allowlists and routing are committed config.
- **Monitor.** Two layers. In-band, through the §5.5 seam, on the same terms as every other component. Externally, from a different failure domain: the `head_auditor` witness polls the episode-store heartbeat. The second layer matters because every other system's health coverage depends on this one, so watching itself is not enough.
- **Update.** Installer reruns are convergent. This counts as **partial** under ORG-068 E9.C4 until the system plugs into the shared fleet auto-currency mechanism (T-2743).
- **Remove.** The build tickets name an uninstall leg.

## Buildout

The scoping ticket **T-2941** splits the build into tranches. Each needs ratification first:

1. Episode-store primitive.
2. Quota-probe detector and emission.
3. Reconciler and meta-detector.
4. Repair-duty grants, each ratified on its own on the [[ORG-048]] shape. Under rev 4 this tranche **grants duties to seats**; it does not create a persona tier.
5. Additional detectors.

Build tickets link to this ADR through [[ORG-065]] edges ([[RFC-004]] §3(b)).

## Open questions for ratify (resolved)

1. **Who holds the duty to repair claude seats from codex, for when claude is down?** *Ruled 2026-10-07 (TG 11361/11367): keep it with Consultant for now.* Today the [[ORG-048]] grant gives it to Consultant, whose ORG-100 role is `head_auditor`. An auditor that also repairs weakens the independence of its outside view.
   - **Lean: keep the ORG-048 grant as it is for now.** It is ratified and costs no new seat. The fleet is also under "do not turn on more seats" (TG 10752).
   - Move the duty to a codex-seated `system_orchestrator` regular once a second System seat is justified.
   - The witness role (§6.9) stays with `head_auditor` either way. Observing from outside is what an auditor is for.

## Alternatives considered

- **Keep "repair persona" as a tier (rev 3.1).** Rejected. ORG-100 retired ad-hoc class labels in favor of roles plus seating. A persona tier would reintroduce a role-like class outside that vocabulary.
- **Make `repairer` an ORG-100 role class.** Rejected. Repair is a responsibility of the System domain, and ORG-100 already scales a domain through regulars. A separate class would split one domain across two vocabularies.
- **Keep the mechanism text in the ADR.** Rejected. The operator prefers abstract ADRs with the detail in referenced files. [[ORG-092]]'s RFC tier is that referenced file.

## Revision history

- **Rev 4 (2026-10-07, T-4677).** Responsibility model rewritten onto ORG-100 (§4). "Repair persona" became "repair duty held by a seat under a grant", stewarded by `head_system_orchestrator`. Mechanism detail (§§1–6) moved word for word to [[RFC-004]] with the same section numbers, so cites like "ORG-076 §2" and "§6.9" now resolve in RFC-004. §7 stays in the ADR word for word. [[ORG-096]] and [[ORG-097]] were set to Proposed to go to ratify with this ADR. Prompted by the operator on the 2026-10-07 ticket walk (TG 11293).
- **Rev 3 / 3.1 (2026-09-11).** Folded in codex's re-audit punch-list and the T-3263 additions that cleared R1–R4. The full changelog is in the [[RFC-004]] appendix.
- **Rev 1–2 (2026-08-12).** Original design; the operator's dispositions in TG 7875.
