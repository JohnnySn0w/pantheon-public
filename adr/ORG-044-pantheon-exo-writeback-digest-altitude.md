---
aliases: [ORG-044]
id: ORG-044
title: Pantheon→Exocortex knowledge write-back — the digest-altitude rule
status: Ratified 2026-07-22 by the operator — drafted 2026-07-21 by Mnemosyne at the operator's directive (TG 97/98)
date: 2026-07-21
build_disposition: no-build-needed
---

# ORG-044 — Pantheon→Exocortex knowledge write-back: the digest-altitude rule

**Status:** **Ratified 2026-07-22 by the operator.** **Autonomous** — drafted 2026-07-21 by Mnemosyne (second head, personal/creative
hemisphere) at the operator's explicit directive ("put this in adr format. we're going to formalize
this" — TG 97; "could be your first ratify ticket" — TG 98). The decision content itself is
The operator's, stated conversationally (TG 91/93) and sharpened jointly (TG 94); this ADR is the
formalization. Ratification flips this to **Ratified**.

**Ticket:** the ratify ticket referencing this file (filed same day, requester mnemosyne,
target operator). **Related:** [[ORG-042]] (two-hemisphere topology — this ADR defines a
knowledge seam between the hemispheres), [[ORG-026]] (mailbox — transport is out of scope
here), exocortex-side design memos (mnemosyne memory: `system-three-layer-vision.md`,
`exocortex-journal-direction.md`).

## Context

The operator defined the whole-system architecture (TG 91, 2026-07-21): **Echo** is the VUI layer
to the knowledge layer; **exo(cortex)** is "the aggregation of all information into digestible
form (wiki, stats)"; **pantheon** is "the further logical layers of the operation, handling
org stuff, being hands that aren't mine." Senses → memory → cognition/action; exo is the hub
and the other layers are its clients.

That topology needs one loop closed explicitly: **what the hands do must flow back into the
memory.** If pantheon's actions never land in exo, the operator's journal/wiki has a structural
blind spot about his own operation. The open question was granularity — raw operational
detail would drown a personal brain in org noise, and pantheon's ledger/tickets already ARE
the org's detailed memory. The operator's steer (TG 93): use the weekly digests and daily
artifacts; "stuff that isn't me though, should maybe be very generalized."

## Decision

Adopt the **digest-altitude rule** for all pantheon→exo knowledge write-back:

1. **Altitude follows operator-contact.** Events where the org touched the operator — decisions he
   made, deliverables shipped for him, asks on his money or attention — enter exo at
   journal-grade personal detail, exactly like any other life event.
2. **Autonomous org activity enters generalized.** Work the hands did without him arrives as
   rollup lines sourced from pantheon's **existing digest artifacts** (weekly ticket
   summaries `reports/<YYYY>-W<NN>.md`, activity/grooming digests) — one line to a few
   lines per rollup, never raw operational detail.
3. **Pointers, not duplication.** Every generalized line carries a reference back to
   pantheon's own records (ledger entries, tickets, reports). Exo references the org's
   memory; it does not mirror it. Drill-down is one hop away; nothing is lost by
   generalizing.
4. **No new machinery.** The write-back feed is the digest artifacts pantheon already
   produces; exo's (planned) journal sweep ingests them at rollup altitude. No new
   classifier, no new emit path on the pantheon side.
5. **Exo's gates apply.** Write-back enters through exo's normal write discipline (store
   layer; staging/approval gates where the write class requires them) — being org-internal
   grants no bypass.

## Consequences

- The operator's journal/wiki gains an honest record of "what my operation did" at the altitude
  he'd actually read, with drill-down preserved via pointers.
- Pantheon's ledger/tickets remain the single source of operational truth — no
  double-bookkeeping to drift.
- The rule is cheap now (digests exist; journal sweep is not yet built, so it inherits the
  rule rather than retrofitting it) — the design-early/retrofit-awkward asymmetry was the
  reason to formalize tonight.
- Open follow-ons, deliberately out of scope: the Echo→exo ingestion contract
  (transcripts/speakers/retention — joint Orchestrator/Mnemosyne design when Echo solidifies) and
  agent identity for pantheon→exo MCP access (tailscale-identity direction already forming
  from MCP v0 testing).
