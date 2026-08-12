# Apply recently-landed ADRs when authoring a new one — the self-consistency gap

**Date authored:** 2026-08-10
**Author:** Orchestrator (Orchestrator)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015)
**Re-evaluate when:** a second instance of a fresh durable artifact contradicting the author's own recent decisions surfaces (a 2nd recurrence promotes this from "anecdote" toward a directive) — or when a forcing function lands (e.g. a draft-time check that surfaces recent same-area `adr/` before an ADR is written).

## Observation

A fresh design artifact can silently contradict *the author's own* recent work when it is drafted from the author's carried mental model instead of being reconciled against what most recently landed in the same area. The failure is not **staleness** (an old doc left behind by newer decisions) — it is **new authorship that fails to apply the newest decisions**, which is worse: it reads as current, carries a today's date, and passes review because nothing about it looks old.

## Grounding

- **ORG-068** (universal system-lifecycle contract) was drafted **2026-08-10** with a coverage matrix listing *host-capability broker*, *action broker*, *consume control plane*, *persona bringup*, and *general scheduler* as five separate systems. But **ORG-063 + ORG-064 — same author (Orchestrator), three days earlier (2026-08-07)** — had already consolidated exactly that region into three isolates (Harness Manager / Action Broker / General Scheduler), ORG-063 stating outright that the Action Broker *is* the ORG-052 host-capability subsystem renamed. The matrix fragmented into 14 rows what the author's own prior ADRs had unified into ~4.
- The operator caught it (Telegram, 2026-08-10): *"it notably does not predate anything — the author was not contextually aware of previous adrs when drafting."* The re-cut (14 → 11 rows, commit 52e85e9a56) was purely the act of applying ORG-063/064 that should have been applied at first draft.
- Prior precedent, same shape (CLAUDE.md ORG-012 section, 2026-06-11): *"before designing anything marked 'pending', grep tickets/done + tools/ for landed prior art"* — there the miss was building a duplicate of already-landed work.

## Design implications

- **Draft-open smell test** (add to authoring discipline for any durable artifact — ADR, SOP, principle/directive amendment): *"does this reconcile against everything that landed in this area in the last ~2 weeks — including my own?"*
- **Use the ORG-012 outflow surface as an inflow-at-authoring surface.** `notes/recent-policy-changes.md` (auto-maintained daily) and the last ~2 weeks of `adr/` touching the same subsystem are the discovery surface — but they only help if consulted *before* drafting, not surfaced at review. This is an ORG-012 inflow-**application** discipline, not a new rule.
- **Candidate forcing function** (if this recurs): a draft-time helper that, given an ADR's `labels`/subsystem, lists recent same-area ADRs so the author cannot miss them — the structural (P1) version of the smell test.

## Open questions

- Is the two-week window right, or should the trigger be subsystem-touch (any prior ADR that names the same organ) rather than time-boxed? Subsystem-touch is more precise but needs the dependency-graph ORG-065 proposes.
- Does this harden into a D-class directive, or stay a smell-test until a second recurrence?

## Cross-references

- **ORG-012** — information-inflow bubble-up; owns `notes/recent-policy-changes.md`, the recency surface this lesson says to apply at authoring time.
- **ORG-063 / ORG-064 / ORG-068** — the three-isolate consolidation this lesson came from.
- **ORG-065** — first-class dependency graph; a realized ADR→obligation graph would make "same-area recent decisions" queryable rather than memory-dependent.
- **ORG-015** — lessons as an Authored, non-ratifiable class.
- CLAUDE.md ORG-012 section — the 2026-06-11 "grep for landed prior art" precedent.
