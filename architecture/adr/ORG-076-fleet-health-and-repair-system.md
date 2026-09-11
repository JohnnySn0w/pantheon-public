---
aliases: [ORG-076]
title: Fleet health-and-repair system — detectors, borked-flag dedup, repair-ticket emission, per-harness repair personas
status: Autonomous — rev 2 REBOUNDED by codex's second ORG-041 re-audit (2026-08-12, 3 BLOCKS + 4 FIX-SOON, HO accepted in full — see the rev-3 punch-list banner). DO NOT RATIFY rev 2. Rev 3 is deferred to post-quota-reset (2026-08-16). Ratification gate = T-2944; build tranche T-2941 is machine-gated on it (depends_on, per codex BLOCK 2). The operator's three rev-2 dispositions (TG 7875) stand and carry forward. Design DIRECTED by the operator across TG 7865/7868/7870/7872 (2026-08-12).
date: 2026-08-12
author: Orchestrator (Orchestrator)
steward: recovery
relates: ORG-003, ORG-019, ORG-041, ORG-048, ORG-052, ORG-053, ORG-063, ORG-065, ORG-068
build_disposition: needs-build
scoping_ticket: T-2941
---
# ORG-076 — Fleet health-and-repair system

> **⚠️ REBOUNDED AGAIN (rev 2, second codex ORG-041 re-audit 2026-08-12, HO accepted in full) — DO NOT RATIFY; rev 3 pending, deferred to post-quota-reset.** Rev-3 punch-list: **(B1)** emission idempotency: "ticket file exists" is NOT queue membership — ticket.py writes the working-tree file before commit/push, so a commit-failure leaves an untracked file the reconciliation grep would falsely read as EMITTED; define queue membership as committed/pushed, add an episode_id-keyed idempotent-create/uniqueness check, keep OPEN while delivery is unproven (§6.3 as written contradicts current behavior). **(B2)** the "T-2941 gated on ratification" claim was prose-only — FIXED STRUCTURALLY same-night: ratify ticket T-2944 filed, T-2941 depends_on it. **(B3)** meta-health is recursive — the timer-liveness detector rides the same primitive/store/timer infrastructure it watches; rev 3 must specify an external heartbeat + watcher in a DIFFERENT failure domain (the ORG-048 codex watcher is the natural home) and add primitive/store/reconciler liveness to acceptance. **(F4)** §3 overstates ORG-065 integration: the structured fields are frontmatter ATTRIBUTES, not closure-consumed edges; ticket schema + CLI + lint/index + episode_id uniqueness are an explicit build prerequisite; build tickets connect via implements/verifies/receipts. **(F5)** matrix row "single seat wedge — covered (T-1980 complement)" is FALSE: T-1980 is the model-stepper, autoswitch disarmed fleet-wide, remainder shelved — row becomes partial/planned with the actual live mechanism cited. **(F6)** healthy verdicts carry no fault_id: define the detector/domain key and close-target rule so one healthy sample cannot silently advance close counters for unrelated open episodes. **(F7)** evidence reachability: a cross-account repair persona needs a durable least-privilege evidence snapshot (or inline bounded evidence) with redaction + retention — evidence_ptr into another user's detector store is not readable across the §4 credential boundary. **(F0/lifecycle)** label Monitor and Update legs honestly (self-referential monitor; convergent-rerun-only update is partial per ORG-068) and give THIS system the externally legible health surface ORG-068 requires. The first draft was REBOUNDED by codex's ORG-041 cold-audit (3 BLOCK + 4 FIX-SOON; HO accepted all — raw audit at codex TG 1181). This revision folds the entire punch-list plus the operator's dispositions (TG 7875): §1 gains the `unknown` verdict + episode threshold/hysteresis (BLOCK 1); §2 is rebuilt on a durable episode store with idempotency-keyed emission + startup reconciliation (BLOCK 2); §4 defines repair authority on the ORG-048 constrained-access + ORG-063 broker-independence precedents (BLOCK 3 — shape ratified by the operator); §2b adds flap + stale-ticket reconciliation (FIX-SOON 4); §3 makes fault/episode/route fields structured per ORG-065 (FIX-SOON 5); §5 replaces the overclaimed wedge-immunity line with a fault-domain matrix (FIX-SOON 6 — narrowing approved by the operator); §6 is the new acceptance section (FIX-SOON 7). Steward + five-stage lifecycle added per ORG-053/068 (steward delegated to HO; call: recovery).

**Status:** Autonomous (agent-authored) → seeks Ratified. **HITL: pending** — codex re-audit of this revision, then the operator reviews and flips to Ratified. Design-first: this ADR fixes the model (the detector → episode → ticket → repair contract and the repair-persona topology); it does **not** commit to per-detector implementation — buildout is decomposed by the scoping ticket (T-2941 is the first concrete piece and is gated on ratification).

## Intent

Make the fleet **self-heal on measurable failure without a human in the loop**, and make the repair capability **survive the failure of any single harness**. Serves resilience (a system whose job is to run unattended must not depend on a human being awake when it breaks) and P1 structural-enforcement (health/repair becomes a mechanism, not a "someone should notice" discipline). It is the operational-resilience counterpart to ORG-068 (which names each system's five-stage lifecycle) — ORG-068 says every organ must be *monitorable and repairable*; this ADR says *how the fleet acts on that monitoring* when an organ breaks.

## Context — where page-and-wait fails

The consume control plane already has failover machinery (ORG-052/persona_bringup: quota probe → planned/forced swap to a codex fallback). But its handling of the case where it **cannot measure** a seat is the gap this ADR closes. Concretely (verified 2026-08-12):

- The Claude quota probe reads usage by **scraping the TUI's cap banner** (`CAP_SENTINELS` in `claude_quota_probe.py`), not an API — a UI-scrape with real drift risk (`bugs/quota-probe-cap-detection-is-a-ui-scrape-...`).
- When the probe **cannot read at all** (TUI drift, dead TUI, harness broken), `decide_failover` returns `"unknown"` and the design **does not swap** (correct — never swap on a blind guess). Persistent failure eventually pages (`EXIT_SOFT_PERSISTENT` at a 3-streak) — but the page **waits for a human**.
- For an unattended overnight cap window, page-and-wait is nearly the failure the failover exists to prevent: the seat sits on an unpollable, possibly-capped claude until someone wakes up.
- And the repair capability today lives partly on the seats that break: if the mechanism that would fix a broken claude *is* the broken claude, there is a single point of failure.

Two failure modes, one root shape: **the system cannot recover from a fault it can only detect, because recovery is deferred to a human and/or hosted on the faulted component.**

## Design

Five contracts, composed. Each is small; the value is the composition.

### 1. Detector contract — three verdicts, episode thresholds

A **detector** is any component that periodically *measures* a subsystem and returns one of **three** verdicts:

- `healthy`
- `broken(fault_id, evidence)` — the measurement **succeeded** and found a bad state;
- `unknown(fault_id, evidence)` — the measurement itself **failed** (cannot read). First-class, not an error path — for the quota probe, *this is the core case* (banner drift, dead TUI, wedged harness all land here).

A detector never repairs and never swaps — it only measures and reports. `fault_id` is a stable identifier for *the kind of break*; an `unknown` mints its own fault_id (e.g. `quota-probe-unreadable:specialist`), distinct from the broken-state faults it failed to measure. The quota probe is one detector; the pattern generalizes (mailbox reachability, timer liveness, git-clone currency, MCP availability, …).

Raw verdicts never act directly; they feed a per-fault_id **episode state machine**:

- **Open threshold:** an episode opens only after **N consecutive** non-healthy verdicts with the same fault_id (default N=3, inheriting the proven `SOFT_FAILURE_ALERT_STREAK` calibration; per-detector configurable).
- **Close hysteresis:** an open episode closes only after **M consecutive** `healthy` verdicts (default M=2).
- Single blips in either direction move counters, never episode state — a flapping measurement cannot storm transitions (acceptance §6.4).

`CLEAR --(N consec non-healthy)--> OPEN --(M consec healthy)--> CLEAR`. The verdict kind (`broken` vs `unknown`) is carried in the episode record and the ticket; it routes the *diagnosis* (unknown → diagnose-first; broken → playbook by fault_id) but does not change the machine.

### 2. Episode store — durable dedup, exactly-once-effective emission

The "borked flag," made durable. Per detector domain, a **single-writer durable episode store** (JSONL or sqlite is buildout's call; durability + atomic transition are the contract):

- **Record:** `(fault_id, episode_id, state: OPEN|EMITTED|RESOLVED, verdict_kind, evidence_ptr, counters, attempts[], timestamps)`.
- **`episode_id` is the idempotency key:** minted once at OPEN and durably persisted **before any emission attempt**; everything downstream (ticket, repair attempts, audit) carries it.
- **Write-ahead order:** durably OPEN (atomic temp-file+rename, or sqlite txn) → emit the repair ticket carrying `episode_id` → durably EMITTED(`ticket_id`).
- **Concurrency:** a per-store single-writer lock (flock); overlapping detector cycles serialize; a second opener observes OPEN and suppresses. One ticket per episode (acceptance §6.1).
- **Crash/startup reconciliation:** on start, every OPEN-without-EMITTED episode reconciles against the ticket surface by `episode_id` (tickets are repo files with the id in frontmatter — grep is the query): ticket exists → mark EMITTED; absent → emit now. Emission is *at-least-once + idempotency-keyed + reconciled* = **exactly-once effective** — one ticket per episode reaches the queue regardless of where a crash lands (each boundary is a kill-injection test, acceptance §6.2).
- **Suppression:** further non-healthy verdicts while OPEN/EMITTED update counters/evidence only — no new ticket. RESOLVED only via hysteresis-close; the next open mints a **fresh** `episode_id`.

Result unchanged from rev 1 and still the point the operator named (TG 7870): **one ticket per fault-episode, not per broken-event** — but now the property survives crashes, races, and retries instead of being assumed. The soft-streak counter (`bump_soft_streak`) and the `.urgent_pending` marker remain the design *ancestors* — extract, then **harden**; as-is they carry no idempotency key and no reconciliation, which is exactly codex's BLOCK 2 (do not build on them unhardened).

#### 2b. Reconciliation sweeps — flap and stale-ticket

A periodic **reconciler** (same cadence class as ticket grooming) closes the loops the emission path can't see:

- **Stale-EMITTED:** repair ticket RESOLVED but the episode still measures non-healthy → after a cool-down (default: one full open-threshold window), close the stale episode and open a **fresh** one (new episode_id; evidence links its predecessor). A closed-but-unfixed fault cannot stay suppressed forever.
- **Healed-under-ticket:** episode hits hysteresis-close while its ticket is still open → set `recovered_before_repair` on the ticket (structured field, §3). The repair persona then *verifies-and-closes* rather than repairing a healed fault blind — a flapping fault still deserves the diagnosis look.
- **Orphans:** EMITTED episodes whose ticket vanished (groomed, superseded) → re-emit under the **same** episode_id.

### 3. Repair-ticket emission — structured fields, ORG-065-aligned

On episode-open, the store emits **one repair ticket**. Its coordinates are **structured frontmatter, not prose** — aligned with the ORG-065 typed-graph shape so closure validation can consume them as edges later:

`fault_id`, `episode_id`, `subsystem`, `detector`, `verdict` (broken|unknown), `evidence` (pointer into the episode store / captured output), `repair_route` (target repair persona + harness, per §5 routing), `attempts_budget`, `recovered_before_repair` (reconciler-set). Labels: `repair`, `org-076`, subsystem tag. `requester`: the detector's owning persona; `target`: per repair_route.

Diagnose-over-blind-swap is unchanged: an `unknown` ticket is **diagnose-first** (banner-drift vs dead-TUI vs real-cap — a repair agent resolves the ambiguity a blind auto-swap would dump onto a possibly-equally-broken fallback); a `broken` ticket opens at the named playbook for its fault_id. The existing hard **page stays** as the backstop for when the repair tier is itself unavailable.

### 4. Repair authority — constrained access on the ORG-048/063 precedents

*(Shape ratified by the operator, TG 7875.)* A **repair persona** actions repair tickets under a grant that is **least-privilege by construction** — the ORG-048 §5 access model, generalized from codex to the whole tier:

- **Allowlist, not invention:** repair personas execute only procedures from the D3-classified `sop/remediation/` set — each already partitioned autonomous-vs-bubble-up at command level. New recovery procedures enter the SOP set first (with rollback named), then the allowlist. Never improvised mid-ticket.
- **Privilege boundary:** narrow per-persona `sudoers.d` command shapes (enumerated commands, never blanket sudo); **no read access to other agents' credential material** — the T-1734/T-1598 S3* posture is preserved, a repair grant must not reopen the reachability that audit closed.
- **Broker-independent:** per ORG-063 §4, the repair tier does **not** route through the Action Broker — recovery cannot depend on the thing it recovers. Standalone asymmetric grants, deliberately *above* the broker.
- **Rollback:** every allowlisted procedure names its proven rollback; an action with no proven rollback is bubble-up tier by definition (trust-posture act-vs-park), not autonomously repairable.
- **Max-attempts:** each ticket carries `attempts_budget` (default 2 autonomous attempts per episode). Every attempt appends to the episode record **and** the acting persona's ledger (audit chain). Budget exhausted → independent escalation.
- **Independent escalation:** escalation rides the repair persona's **own** channel (codex: its own bot; claude-side: the plugin bots) — never the faulted harness. The page backstop is unchanged.
- **Grants are operator-gate items:** this ADR fixes the *shape*; it grants nothing. Each repair persona's privilege grant is its own ratification on the ORG-048 pattern (spec-review-before-grant, supervised first-fire) — ORG-048's ratified codex grant is the precedent instance.

### 5. Repair topology and fault-domain matrix

The load-bearing topology rule survives from rev 1: **the repair capability must not live on the harness that is down.** Target: a repair persona per harness — claude broken → a **codex** repair persona actions the ticket; codex broken → a **claude** repair persona (Recovery today). Routing prefers a repair persona on a harness *other than* the faulted one; if none is live, fall through to any live repair persona, else page (acceptance §6.6).

**The narrowed claim** *(replacing rev 1's "no single offline service can wedge the whole system," which overclaimed — narrowing approved by the operator, TG 7875)*: this system removes single points of failure **at the harness/agent tier** — no single harness fault leaves the fleet without a live repairer. It claims nothing about shared substrate: both harnesses co-reside on one VPS and share the host, systemd, the git ticket path, the mailbox filesystem, and the network.

| Fault domain | Covered by | Residual after this ADR |
|---|---|---|
| claude harness (sub-cap, TUI/banner drift, harness bug) | codex repair persona (ORG-048) | **covered** — the design case |
| codex harness | claude repair tier (Recovery) | **covered** |
| single persona/seat wedge | same-harness repair tier (T-1980 complement) | **covered** |
| systemd --user scheduler, either account | cross-account: the other account's detectors see missed heartbeats | **partial** — detection cross-covers; repairing systemd itself is a sudo-tier SOP |
| git ticket path (origin unreachable, repo corrupt) | episode store is local + durable; emission retries; routing degrades to mailbox/page | **degraded** — no new tickets while down, but no episode loss |
| mailbox (shared filesystem) | page backstop | **degraded** |
| VPS host (power, kernel, disk) | nothing on-host survives — both harnesses colocate | **not covered** — the page infrastructure dies too; off-host uptime monitoring is future work, out of scope |
| network / Telegram egress | none — pages do not deliver either | **not covered** (acknowledged) |

The last two rows are the honest boundary: this is an **on-host mutual-repair system**, not an off-host DR system.

### 6. Acceptance

The buildout must prove, with tests at the named boundaries (not by construction):

1. **Concurrency** — two overlapping detector cycles on one fault_id → exactly one OPEN episode, one ticket; the lock/serialization demonstrated.
2. **Crash boundaries** — process killed (a) after OPEN before emission, (b) after emission before the EMITTED mark → startup reconciliation converges to exactly one ticket per episode (kill-injection at each boundary).
3. **Delivery failure** — ticket write/commit fails → retry under the same episode_id yields one ticket, no duplicate.
4. **Flap** — alternating verdicts under threshold N → zero episodes; oscillation across the boundary → episode transitions bounded by threshold+hysteresis, never a storm.
5. **Stale-ticket** — resolved-but-still-broken → fresh episode after cool-down; healed-under-ticket → `recovered_before_repair` set and no repair action fired.
6. **Repair-target-unavailable** — routing target down → falls to an alternate repair persona, else page; never silently dropped.
7. **Shared-dep degradation** — git path down → episodes accumulate durably and emission resumes on recovery (the §5 git-row behavior, demonstrated).
8. **Restart persistence** — detector-host restart loses no episode state and produces no duplicate emission (this is ORG-068 restart-fixable + stateless, proven for the health-repair system itself).

## 7. Operator plane — health view + per-persona notification governance

> **Folded in 2026-08-18 by Recovery (steward) from the retired standalone ORG-077, at the operator's direction (TG 6093–6102). Rev-3 should review + incorporate this section.** ORG-076 §1–6 are the *autonomous* leg (detect → episode → repair without a human); this section is the *operator* leg — the human-facing view of the same health signals plus governance over what those signals page. Same steward; one system, two legs.

**Motivation.** Fleet health is legible to the operator only as a stream of per-monitor Telegram alerts (un-mutable except by editing systemd drop-ins) plus ad-hoc CLI probes — there is no glanceable "how is the fleet doing" view, and no way to say "this persona is intentionally offline — stop paging me about it" short of a per-monitor edit. Two ad-hoc per-monitor skips already exist (`CHANNEL_LIVENESS_SKIP`; `PANTHEON_CRED_ALERT_SKIP`, T-2973) — the second is the tell that the piecemeal approach doesn't scale. Serves D1 (operator bandwidth) + ORG-068 (externally-legible health).

**7.1 Per-persona notification mute (the control).** A single per-persona mute registry in a cross-account-readable location, plus a shared helper `is_muted(persona)` that **every persona-scoped alert path consults before sending**. Muting a persona silences **all** its fleet alerts uniformly (MVP: uniform/all-classes per the operator; per-class granularity later). **Supersedes and absorbs** the two ad-hoc skips (their members migrate in; the per-monitor envs are removed). **Invariant — mute ≠ blind:** a muted persona is still classified + trace-logged at every monitor (only the operator page is withheld), so a persona that comes back online was never silently invisible (cf. §"correct no-op hides the next failure").

**7.2 Per-persona health view (the observation).** A glanceable rollup of each persona's health (up / wedged / auth-dead / offline / cliff-approaching / …), fed by the existing monitor signals (`agent_monitor_healthcheck.py` + per-monitor state) — the externally-legible health ORG-068 requires, aggregated per persona.

**7.3 Home — a Health Plane section in kino (ORG-006).** The health view + the mute toggle live as a new section in the operator PWA, not a separate dashboard. A CLI (`notify_mute.py mute|unmute|list`) is the headless equivalent.

**Boundary.** The mute registry governs operator-facing pages only — it does **not** disable detection or §1–6 autonomous repair (a muted persona is still detected + auto-repaired, just not paged about). The §132 "page backstop is retained" is what the mute governs.

**Build tranche (decomposed by grant — kino/`pantheon-pwa` is HO-only rw):** **T-2978** (→ recovery): the mute registry + `is_muted` + retrofit of the ~25 alert paths (gate at shared chokepoints — `route_failure_alert` + per-monitor sends) + CLI; unify the two ad-hoc skips. **T-2979** (→ orchestrator, depends_on T-2978): the kino Health Plane section — health view + mute toggle, reading T-2978's registry.

## Non-goals / boundaries

- Not a replacement for the ORG-052 quota failover — this is the layer *above* it (what to do when the seat can't be measured, vs. when it measures capped).
- Not auto-swap-on-unknown — the repair ticket (diagnose-then-act) is deliberately chosen over blind swapping (TG 7865/7868 reasoning).
- The page backstop is retained, not removed.
- Off-host DR and external uptime monitoring are out of scope (§5, last two matrix rows).
- Detector *implementations* per subsystem are buildout, not this ADR — this fixes the contract shapes only.

## Steward and lifecycle (ORG-053/068)

**Steward: recovery** — the recovery persona; recovery is its whole role, so the fit is structural. *(Call delegated by the operator to HO, TG 7875; made 2026-08-12; cheaply reassignable per ORG-068.)* Recovery answers for the system's health, reviews changes to it, and owns closing its lifecycle-leg gaps in the ORG-068 matrix once built.

The health-repair system is itself an organ; its five stages:

- **Install** — each detector + the reconciler ship as installable systemd `--user` units via the standard installer shape (plan/apply/verify); an acceptance item of the buildout, not an afterthought.
- **Configure** — thresholds (N/M), cool-downs, allowlists, and the routing table are committed config, not code constants.
- **Monitor** — the monitor is itself monitored: a timer-liveness detector watches the detector/reconciler timers (meta-detection is just another detector on the same primitive), and the page backstop covers the remainder.
- **Update** — installer re-runs are convergent; the episode-store schema carries a version field.
- **Remove** — an uninstall leg (units + store + flags) named in the buildout tickets.

## Buildout

Decomposed by scoping ticket **T-2941** (gated on ratification). Tranches:

1. **Episode-store primitive** — the durable `(detect → episode → emit-idempotent → reconcile)` module, extracting-and-hardening the soft-streak + urgent-marker ancestors; proves acceptance §6.1–.4 and §6.8.
2. **Quota-probe detector + emission** (T-2941) — the first detector on the primitive; persistent-unknown emits the deduped repair ticket; proves §6.2 and §6.5 for the live path.
3. **Reconciler (§2b) + meta-detector** (timer-liveness) — proves §6.5–.6.
4. **Repair-persona tier** — formalize the role + routing; each persona's privilege grant is its own the operator ratification on the ORG-048 shape (§4); a codex repair persona is first (dovetails ORG-048's ratified role + the T-2940 codex-telegram-MCP work).
5. **Additional detectors** — mailbox, git-clone currency, MCP availability, banner-sentinel drift (the `bugs/quota-probe-cap-detection` monitor), each on the same primitive.

Every buildout ticket carries `build` + `org-076` labels and the §3 structured fields.
