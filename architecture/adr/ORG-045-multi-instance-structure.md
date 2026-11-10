---
aliases: [ORG-045]
id: ORG-045
title: "The multi-instance structure, as a Viable System Model: functions mapped to systems, actors occupying them (often cross-system)"
status: "Ratified 2026-07-27 by the operator (via kino T-1970 resolve + Telegram confirm 6501; the batched T-1963 seed-VSM ratify landed 2026-07-28, also ratified — the batch is complete). Reworked 2026-07-26 per the operator's rework directive (system-first structure; map FUNCTIONS to systems, not personas 1:1; five previously-missed assignments bundled in — the algedonic channel, S3*-as-channel, named cross-system actors, fuller S2, the S3↔S4 homeostat; genericized for external readers). Prior codex S3* review 2026-07-23: SOUND-WITH-CORRECTIONS, all 7 findings applied (carried forward)."
date: 2026-07-23
build_disposition: no-build-needed
---

# ORG-045 — The multi-instance structure (as a Viable System Model)

**Status:** **Ratified 2026-07-27 by the operator** (kino T-1970 resolve + Telegram confirm 6501). **Author of record:** Orchestrator (the Head
Orchestrator persona), curated from Specialist's draft
(`knowledge/episodic/2026-07-23-multi-instance-structure-adr/adr-proposal-draft.md`, rev2, per T-1969),
**reworked** to system-first per the operator's 2026-07-26 rework directive (T-1970 transition log).
**Source of authority:** The operator msgs 910/912 (2026-07-22, Specialist channel) + the TG-917 ruling
(2026-07-23) + the T-1921 drift-1 ruling + the 2026-07-26 rework directive + the TG-6455/6457
confirmations (system-first; bundle the missed assignments). **Ratification batch:** with T-1963
(seed VSM update) — same surface, one review, **both ratified by the operator**.

## Why this ADR exists

No single `adr/` artifact stated the whole multi-instance structure — the three-as-canonical-anchor
lived in a notes/ doc, the second-hemisphere and sub-orchestrator pieces in ORG-042/ORG-043, the
substrate's status in an episodic doc. The operator asked (msg 912) for it formalized as one ratifiable
ADR, codex-reviewed, then propagated to the public `pantheon-public` seed. **Anti-duplication:** this
ADR CITES ORG-042 / ORG-043 / the phase-B mapping
(`knowledge/episodic/2026-07-22-seed-vsm-design/persona-vsm-mapping.md`) rather than restating their
mechanics; its content is the consolidated taxonomy plus the msg-910/912/TG-917 + rework decisions.

**Written for external readers** (it propagates to the public seed): each daemon is named with its
persona on use — the `claude` daemon (persona **Orchestrator**, the Orchestrator / "HO"), `claude-research`
(**Specialist**), `claude-fallback` (**Recovery**), the `codex` worker-harness (**Consultant**), the sub-orchestrator
seats `suborch1`/`suborch2` (**Mason**/**Artisan**), the `exocortex` daemon (**Archivist**), and
**the-ancestor** (a shared hub host, not an agent).

## The modeling frame — how to read this ADR (the load-bearing correction)

VSM terminology is the **adopted, normative modeling frame** of this ADR (not a loose metaphor: the
placements below are real claims, with our extensions of Beer explicitly marked). The **central rule
of the frame**, per the operator's rework directive:

> **Map FUNCTIONS to systems, not personas to systems.** The five VSM systems (plus the channels) are
> *roles the org performs*. A given persona or artifact **occupies whichever functions it is performing**
> — and most occupy **several, across different systems**. Persona→system 1:1 is the wrong shape; it
> cannot come out cleanly, because actors are cross-system by nature.

So the sections below are organized **by system** (the function), each naming what serves it. The
recurring cross-system actors are then collected in their own section, because that spanning **is the
structure**, not an exception to it.

## The systems

### S5 — Policy / identity
**the operator** (the operator). Ultimate authority, ethos, closure; ratifies ADRs and the governing
canon; arbitrates the S3↔S4 balance (below). The operator is **inside the model** — S5 of Pantheon *and*
the level-+1 metasystem: Pantheon is itself one operational unit of his larger endeavour, so its S5
looks "up" as well as "down" (marked as our framing, consistent with the phase-B mapping).

### S4 — Intelligence (outside and future)
`claude-research` (**Specialist**). Environment-scanning and adaptation: the watch loop, research
dispatches, vendor/policy/billing monitoring — the org's read on the world it must stay viable
*within*. The **environment** Specialist scans (the AI/agent research frontier, platform and vendor
changes, the policy/billing landscape) is the S4-facing world; its products (watch reports,
research) feed S3 via the homeostat channel below.

### S3 — Control / management (inside and now)
Two things serve S3:
- **The written governing documents** — the rulebook (principles P1–P7, directives D1–D12, the
  policies) and **ratified ADRs functioning as governance**. These are the standing control surface:
  what the org has decided must hold.
- **The HO's orchestration function** — the `claude` daemon (**Orchestrator**) *when orchestrating*:
  dispatch, resource bargains, priority ordering, merge/push authority, accountability for the S1
  collective. (Orchestrator is cross-system — see below — this is only its S3 arm.)

#### S3* — Audit (the sporadic direct-inspection channel)
S3* is a **channel/class, not a single tool**: direct inspection of S1's output that bypasses normal
S3 reporting. Its instances: the **reviewer role** (adversarial review of an implementer's diff), the
**S3STAR-audit worker** (the `S3STAR-*` finding tickets), the **guard / leak-scan / byte-sequence
audit hooks**, and **Codex spec-review**. The different-vendor auditor (Codex) is **our explicit
extension of Beer's S3\***, not his spec — marked as such.

### S2 — Coordination (anti-oscillation substrate)
The damping layer that keeps S1 units from colliding. **the-ancestor** is the substrate the model's
channels run on (the mailbox hub + organizational memory); substrate failure degrades every channel
at once, which is precisely why it is **integral**. S2 is **more than the hub**, though — it is the
whole **serialization/anti-oscillation mechanism**: the ticket system's **WIP cap** (one active
ticket per agent) and **claim-locking**, the `agent/<task>`-branch + **HO-exclusive-push** convention
(two S1 units cannot collide on `main`), and the **wake/nudge cadence** (the `keep_going` /
`activity-monitor` timers that pace when units act). the-ancestor's rebalance trigger (T-1968,
operator-initiated) may later move it toward active S1/S2 curation duties — see Re-evaluate.

### S1 — Operations (the value-producing units)

> Read through (operator-ratified framing, not an ADR edit): the 2026-09-07 peer-orchestrator framing (the operator, TG 9294/9296/9298/9300; recorded in `pinned-rules/orchestrator.md` "Regular engineering orchestrators" and typed as role `engineering_orchestrator` in ORG-100): the sub-orchestrator seats listed here are peer orchestrators with a chain position, not workers.

The units that do the primary work; each is itself a **viable system one recursion level down**.
- **The sub-orchestrator seats** — `suborch1`/`suborch2` (**Mason**/**Artisan**), a horizontally
  scalable N+ tier under the HO (N per deployment, including N=0). Mason/Artisan are *current instances,
  not the pattern*. Seats are full ledger actors — but **full ledger actor ≠ canonical verification
  anchor** (anchor status follows authorship distribution, not tier membership). (Cite-only; mechanics
  in ORG-043.)
- **The HO's operational acts** — Orchestrator *when producing*: ADR/doc authorship, ledger entries, dispatch
  execution. (These acts are S1 even though the same persona's orchestration is S3.)
- **Archivist** (`exocortex`) — an **integral level-0 S1 unit, on par with the seats** (the operator,
  TG-917: "on par with artisan/mason, just separated for concern reasons"; msg 912: not an exception,
  not "outside," a specialized/optional unit he will "integrate closer" in future). This **retires
  "exception"/"outside" framing org-wide** (corrected on touch, including the T-1921 scope note).
  - *Separation is implementation, not topology:* own unix user (`exocortex`), own private-data domain
    (ORG-042 privacy gates), own operator-facing channel.
  - *Comms asymmetry, named:* unlike Mason/Artisan (no the operator channel — they route through the HO),
    Archivist keeps a direct operator channel. "On par" means **on par in VSM level / S1 status — not
    identical in channels or accountability routing.** (Our articulation, not a Beer citation.)
  - *S3-attachment:* the seats' accountability channel (resource bargain, dispatch briefs, merge
    review) attaches to the HO's S3; **Archivist's attaches directly to the operator** for their
    domain, by privacy design (routing personal-domain accountability through the HO would defeat the
    ORG-042 separation). So level-0 S1 units partition between two accountability holders — HO for the
    technical seats, operator for the private-domain unit.
  - *Guardrails on the operator's S3-attachment (codex review, applied):* for Archivist's unit the
    operator deliberately wears S3-attachment and S5 at once. To keep that distinct from the "S5
    collapsed into S3" pathology, the collapse is **BOUNDED:** **(a)** it applies ONLY to Archivist's
    private-domain resource/accountability channel; **(b)** it does NOT generalize to the technical
    seats; **(c)** it does NOT replace the HO's S3 for any Pantheon operation; **(d)** it MUST be
    re-evaluated if Archivist ever gains Pantheon dispatch, merge, or ledger authority. Any breach of
    (a)–(c) observed in practice is itself a **pathology signal to surface, not a precedent**.
  - *One-technical-HO invariant SURVIVES* (precision per codex review): the invariant is **one
    technical HO / one Pantheon dispatch authority per instance** — not "one accountability holder."
    Archivist is not an HO (no dispatch, merge, or ledger-curation role over Pantheon); this places her
    as S1, not a second S2/S3/S5. One instance, one HO, N+ seats, plus optionally one
    privacy-partitioned specialized unit. ORG-042's "second head / two-hemisphere" language reads
    henceforth as **domain/comms-surface description, not VSM placement.**
  - *"Integrate closer"* means deepening in-system integration (beyond the ORG-044 digest-altitude
    write-back) and **does not currently imply a level change** — she is already inside. (A future
    topology change lives in Re-evaluate, not here.)

## The algedonic channel (the alarm bypass)
The **pain/alarm signal that jumps straight to S5**, skipping normal S3/S4 filtering, when something
is critically wrong. Pantheon's algedonic channel is real and worth naming as its own element:
**mailbox severity + urgent injection** (T-1641), the **wedge-detection alert** (T-1980 — "session
alive but every turn erroring" → Recovery's mailbox), **Recovery's recovery pokes** into a non-responsive
agent, and the **money-event / critical-outage direct escalations** to the operator (the pve2 outage
was one). It is how the org *screams* — a low-latency channel distinct from the deliberate S3/S4 flow.

## Cross-system occupancy (this spanning IS the structure)
Per the frame's central rule, actors routinely occupy several systems. The load-bearing cases:
- **Orchestrator (the HO):** **S1** (authorship, ledger, dispatch execution) **+ S3** (orchestration, resource
  allocation, merge authority).
- **the operator (the operator):** **S5** (policy) **+ bounded S3** for Archivist's private unit (the
  guardrailed collapse above).
- **Codex:** **S1** (dispatched worker, ORG-031 Option-A harness) **+ S3\*** (spec-review auditor).
- **Recovery:** cross-system by role — **recovery/algedonic-response** (restoring wedged units — the
  operator arm of the alarm channel), **S2-construction** (building the substrate: the Proxmox
  cluster, the self-hosted git server, LXC layout), and **S3\*** (grooming / spine-index audits).
  (Our articulation; SETTLED — the operator ratified the ADR as written 2026-07-27, accepting this Recovery placement.)
- **ADRs / the governing docs:** **S1 as authored** (the act of writing) **→ S3 as governance** (once
  ratified and binding) — cross-system *by lifecycle*.
- **The exocortex / knowledge base:** **S1** (Archivist's operational domain) **+ S2-adjacent**
  (organizational-memory store).

This is why persona→system 1:1 fails: the systems are functions, and the actors wear several.

## The S3↔S4 homeostat (the great regulator)
The live tension between **present operations (S3)** and **future/environment (S4/Specialist)** is the
balance that keeps the org *viable*, not merely surviving. **S5 (the operator) arbitrates it.** The
**S4→S3 channel** is concrete here: Specialist's watch-findings become tickets and ADR amendments —
intelligence converted into control. Naming the homeostat (not just the two systems) is Beer's point
that viability lives in the *balance*, not either pole.

## The canonical-three anchor (an orthogonal property)
**Orchestrator, Specialist, Recovery** are the common canonical scope of every deployment narrative and the
**verification anchor**: claims about org history/structure verify against the three's ledger trail
first (authorship at 2026-07-23 curation, all shards: Orchestrator/orchestrator 1468, Recovery 233, Specialist
19, Mason 8, Artisan 1, Archivist 0 — the only set whose authorship verifies end-to-end today). New
seats join via the tiers above; they do not dilute the anchor. **This is an authorship/verification
property, orthogonal to VSM placement** — the three sit in different systems (Specialist S4; Orchestrator S1+S3;
Recovery the cross-system recovery/S2-construction/S3\* role), yet share anchor status by authorship
distribution, **not** by tier or system.

## Out of scope (deliberately)
The **bridge persona** The operator is designing (T-2025 — a new persona over both hemispheres as his single
voice-front) is a **future topology change** with its own forthcoming ADR. ORG-045 describes **today's**
structure and simply does not foreclose it.

**ORG-056 back-reference (added 2026-09-13, T-3185):** **ORG-056** (ratified 2026-08-02) is that
forthcoming ADR. It fixes the Bridge's canonical **organ-contract placement** — the executive face
above both hemisphere heads (ORG-056 §§2–3), matching the two-hemisphere structure already named here
(Orchestrator / Pantheon head + Archivist / personal-creative head) — and itself requires this back-reference
(ORG-056 Consequences: "ORG-045 needs a back-reference and explicit Bridge placement after this series
is ratified, but its VSM mapping remains valid"). The placement is a **contract, not an appointment**:
per ORG-056's own locked acknowledgement, this does not appoint, build, or enable the Bridge. The VSM
mapping above (the systems S1–S5, the channels, and cross-system occupancy) remains **today's ratified
structure, unchanged** until the operator's Echo-gated build (T-2025) actually instantiates the Bridge as a
system-occupying actor.

## Consequences
- One ratifiable artifact answers "what IS the multi-instance structure"; digests, seed docs, and the
  multi-instance-discipline note point here instead of triangulating across two ADRs, a notes file,
  and an episodic doc.
- "Exception" language for Archivist is retired org-wide; corrected on touch.
- The verification-anchor property of the three becomes a stated invariant, explicitly separated from
  VSM placement.
- The **function→system frame** (cross-system occupancy as the norm) becomes the standing way the org
  reads its own structure — superseding any persona→system 1:1 reading in prior docs (corrected on
  touch).
- T-1963 item (5) (second-head-as-multiple-seed-instances) is amended by the Archivist-as-S1 placement;
  **the operator's ratification of this ADR and of T-1963 are batched as one review.**
- Seed update after ratification (canonical-three anchor framing, Archivist integral-S1 language, the
  algedonic/S3*/cross-system additions, the new re-evaluate triggers; `vsm-instantiation.md` carries
  the rest).

## Re-evaluate when
- The operator's "integrate closer" trigger fires for Archivist.
- A fourth persona class emerges that is none of: canonical-three, substrate, privacy-partitioned
  specialized unit, N+ seat.
- the-ancestor's rebalance trigger resolves (T-1968) — its outcome may move it from substrate toward
  active S1/S2 curation, touching its S2 placement.
- The ledger-authorship distribution materially shifts (e.g. seats become primary authors) — re-derive
  the verification-anchor framing from the then-current counts, do not defend it.
- The **bridge persona** (T-2025) lands — it changes the metasystem/operator-interface and will need its
  placement stated (in its own ADR, with a back-reference here).
- (Narrowed 2026-09-13, T-3185) ORG-056 already states the Bridge's organ-contract placement and
  supplies this back-reference (see "Out of scope" above); the trigger above now narrows to the Bridge
  actually being **appointed, built, or enabled** — at that point this VSM needs the Bridge added as a
  system-occupying actor, not merely footnoted.
- The **algedonic channel** formalizes into dedicated tooling, or a **new channel** (beyond S1–S5 + S2
  substrate + S3* + algedonic) is identified that this mapping doesn't hold.

## Provenance / review trail
- 2026-07-23 — Specialist drafts (T-1969); Orchestrator curates ORG-045 capturing msgs 910/912 + TG-917 verbatim.
- 2026-07-23 — Codex S3* review: SOUND-WITH-CORRECTIONS, 7/7 applied (S3/S5-collapse guardrails,
  one-technical-HO precision, on-par scope, integrate-closer softening, frame normativity,
  anchor-vs-actor distinction) — all carried forward into this rework.
- 2026-07-26 — the operator **rework directive** (T-1970 retarget): genericize for external readers +
  concretize persona names + fix the VSM mapping (functions→systems, cross-system occupancy). Orchestrator
  surfaced the missed assignments (algedonic channel, S3*-as-channel, cross-system actors, fuller S2,
  S3↔S4 homeostat); the operator confirmed system-first (TG-6455) and "bundle those in" (TG-6457).
- 2026-07-26 — Orchestrator reworks to this system-first form.
- **2026-07-27 — RATIFIED by the operator** (T-1970 resolved via kino + Telegram confirm 6501).
- **2026-07-28 — the batched T-1963 seed-VSM ratify also RATIFIED by the operator** (T-1963 transition log,
  2026-07-28T03:44:26Z). The batch is complete; both halves stand ratified. Corrected on T-2215 —
  this ADR's last touch was 2026-07-27T22:04:37Z (`f9d7424f9`), 5h40m before T-1963 landed, so the
  "pending" wording above was true when written and outlived its window with no writer in it.
- **2026-09-13 — Orchestrator adds the ORG-056 back-reference/Bridge-placement pointer** (T-3185, the T-2416
  Unit A D2-writer amendment) to "Out of scope (deliberately)" + narrows one "Re-evaluate when" trigger.
  Additive only — the VSM mapping (§§S1–S5, channels, cross-system occupancy) is unchanged.
