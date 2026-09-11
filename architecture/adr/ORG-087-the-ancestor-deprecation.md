---
aliases: [ORG-087]
id: ORG-087
title: "Deprecate the-ancestor persona — final ratified form (post-design)"
status: Ratified 2026-09-04 by the operator via Telegram 8965 ("follow parenthetical leans and ratify"). Formalizes the design captured at notes/2026-08-11-the-ancestor-deprecation-design.md (T-2873 arc), applies the operator's four §7 open-question rulings, applies his earlier three body edits (2026-09-03 TG 8958), and authorizes spawning execution children.
date: 2026-09-04
author: Orchestrator
steward: orchestrator
hitl: Ratify
supersedes: [ORG-067]
extends: [ORG-042, ORG-045, ORG-058]
labels: [org-identity, the-ancestor, post-office, deprecation, s5-boundary, org-058, org-042]
build_disposition: needs-build
scoping_ticket: T-2873
ratifiable_files: [pantheon-config:adr/ORG-087-the-ancestor-deprecation.md]
---

# ORG-087 — Deprecate the-ancestor persona (ratified form)

**Status: Ratified 2026-09-04 by the operator via Telegram 8965.** Formal deprecation of the-ancestor as a distinct persona/user/VSM-entry. Its functions are re-homed, not lost: mailbox hub → post-office (T-2693 arc, load-bearing infra); memory/continuity semantic → Mnemosyne (per ORG-042/ORG-058, corrects T-1968's category error); judgment-bearing curation → Specialist (S4); personaless-crons stay personaless in execution but with Specialist as accountable owner. Formal supersession of ORG-067 (the earlier DRAFT with a narrower framing).

## 1. Provenance chain

- 2026-08-09: operator-directed via TG 7522 — deprecate the-ancestor + Specialist does the walks. ORG-067 DRAFT filed as first-cut design.
- 2026-08-11: Full design doc drafted at `notes/2026-08-11-the-ancestor-deprecation-design.md` (T-2873 arc), superseding ORG-067's narrower framing with the honest inventory + phased sequence + rollback plan.
- 2026-09-03T17:38Z: the operator returned T-2873 with 3 body-edit requests (TG 8958): 3a crons need owning persona, 3c memory-continuity is Mnemosyne's not post-office's, phase-3 co-observation-window not needed.
- 2026-09-03T17:41Z: Edits landed (commit `1bf8e6d2ba`); T-2873 re-filed to the operator with §7 open questions intact.
- 2026-09-04T23:08Z: the operator ratifies via TG 8965 ("follow parenthetical leans and ratify") — this ADR is the resulting formal record.

## 2. Rulings applied (the operator's four §7 open-question decisions)

Per the operator's "follow parenthetical leans" instruction, the design doc's four §7 open questions resolve to their parenthetical recommendations:

- **§7.1 Curation scope (3a):** REUSE the existing Specialist knowledge-sweep dispatch surface for the judgment-bearing curation items. NO new Specialist timer or standing session. Zero seat/cost added.
- **§7.2 Sequencing:** Step 2 (curation re-home) lands INDEPENDENTLY now (reversible, infra-free). The hub migration (§4 phase 3, T-2693) stays gated on T-2683 completion + Recovery-executed with operator-notified.
- **§7.3 VSM split (3c):** CONFIRMED per the operator's 2026-09-03 edit — memory + continuity semantic → Mnemosyne (per ORG-042/ORG-058), mailbox infra → post-office (separate row, mechanical hub only). This formally reverses T-1968's rebalance-trigger reading.
- **§7.4 Veneration:** KEEP T-2156 retirement as a separate arc, but SEQUENCE it before the-ancestor account retirement.

## 3. Substance (design body reference)

The full design body lives at `notes/2026-08-11-the-ancestor-deprecation-design.md` (T-2873 ratifiable_files legacy pointer; the notes/ file now carries a supersession pointer to this ADR per §5 below). Rather than duplicate the full body here, this ADR points at the design doc for the honest inventory (§1), the three strands (§3 with the 3a/3b/3c breakdown), the phased sequence (§4), the reversibility/rollback (§5), and what-stays-vs-what-goes (§6). This ADR carries only the ratified rulings + execution structure.

### Summary of what happens (design doc §2 table, ratified form)

| the-ancestor function | Re-homed to | VSM after |
|---|---|---|
| Mailbox hub (load-bearing) | **post-office** user (T-2693) | still **substrate** — renamed + re-owned, not eliminated |
| Judgment-bearing curation | **Specialist** (S4), via existing knowledge-sweep dispatch (§7.1 ruling — no new timer) | S4 (Specialist) — **no new unit** |
| Memory/continuity SEMANTIC (T-1968's mis-attribution) | **Mnemosyne** (S4, exocortex hemisphere per ORG-042/ORG-058) | S4 (Mnemosyne) — **corrects the mis-attribution** |
| Personaless curation crons | Stay personaless in execution; **Specialist is accountable owner** for the work they fire | S2 structural |
| Un-activated `peer-daemon` persona seat | Retired un-activated | removed |
| Veneration harness | Retired via T-2156, sequenced BEFORE account retirement per §7.4 ruling | removed |

Net: one fewer named entity; the mailbox substrate persists under an honest name; curation judgment consolidates in the existing S4 seats; memory/continuity semantic finds its correct home in Mnemosyne.

## 4. Execution children (post-ratify, to file as follow-on tickets)

Per the design doc's §4 phased sequence + the operator's rulings:

1. **T-2683 installer landing post-office-native** (already in flight, pre-req for step 3).
2. **Curation re-home to Specialist** — extend the existing Specialist knowledge-sweep dispatch to carry the T-1968-imagined-but-never-live curation items. §7.1 rules "no new timer" so this is a dispatch-surface extension only. Reversible, infra-free — can proceed independently of the hub migration per §7.2 ruling.
3. **Hub migration (T-2693 arc)** — its 7 phases, Recovery-executed with the operator notified on start + completion (per §4-phase-3 ruling). Sequence after T-2683 lands.
4. **Scrub** — grep-exhaustive removal of the-ancestor refs across code + docs (instance.yaml, `tools/*ancestor*`, ORG-026/045 references, agent_role_map, fleet_health, the VSM mapping doc).
5. **Retire the persona/user** — disable then remove the-ancestor account after the hub soak passes; drop the un-activated `peer-daemon` role.
6. **VSM doc update** — reconcile `persona-vsm-mapping.md` per §7.3 ruling: memory/continuity → Mnemosyne, mailbox → post-office (separate rows, no new unit).
7. **T-2156 Veneration retirement** — separate arc, sequenced BEFORE step 5 per §7.4 ruling.

Filing these as follow-on tickets in the next session slice.

## 5. Relationship to the notes/ design doc

The notes/ design doc `notes/2026-08-11-the-ancestor-deprecation-design.md` remains as the historical authored artifact carrying the full rationale, honest inventory, and phased-sequence rationale. Per ORG-015, `notes/` is NOT a ratifiable class — this ADR is the ratification surface, and the notes/ file's role is authored-source (feed) not decision (ratified). The notes/ doc gets a top-line supersession-pointer amendment noting "ratified via ORG-087 2026-09-04 with §7 rulings applied" but its body content stays intact as the authored provenance record.

## 6. Reversibility

Per design doc §5 (unchanged by ratify):
- **Curation (3a):** pure config/dispatch change — revert by removing the Specialist dispatch extension; the crons never moved.
- **Hub (3b):** load-bearing risk. Rollback = repoint every reference back to `/home/the-ancestor/mailbox`, kept intact through the soak (T-2693 §rollback). No deletion until proven round-trip + soak — proven-rollback posture per trust-posture §act-vs-park (this is (a)-hold-class until round-trip is proven, then (b)-surface-and-act with Recovery driving execution + the operator notified per §4 phase 3).
- **Account retirement (step 5):** disable-before-delete; a disabled account is trivially re-enabled; deletion only after the hub soak + scrub verify green.

## 7. Supersession relationship to ORG-067

ORG-067 DRAFT (2026-08-09) was the first-cut deprecation design — narrower framing, pre-honest-inventory. It correctly identified the deprecation intent but wrongly framed the "grooming/curation walks" as the-ancestor's today. T-2873's notes/ design doc (2026-08-11) corrected that framing via the honest inventory (§1 there): the walks are personaless-cron OR Specialist-dispatch already, so "Specialist absorbs them" is declining T-1968's promotion, not moving them off the-ancestor.

ORG-087 (this ADR) is the ratified successor. ORG-067 remains in the corpus per D-E.2-style "superseded conceptions remain visible" convention (which ORG-086 would formalize post-ratify), marked with a supersession pointer to this ADR.

## 8. Ledger + audit chain

- Ratify: the operator via Telegram msg 8965 (2026-09-04T23:08Z), "follow parenthetical leans and ratify."
- Body edits: commit `1bf8e6d2ba` (2026-09-03, TG 8958 3-edit-request).
- Ratifying commit: this file at commit landing this ADR (2026-09-04).
- Ledger entry to append on landing recording the ratify + linking ORG-087 + T-2873 resolve + notes/ design doc + this file.
