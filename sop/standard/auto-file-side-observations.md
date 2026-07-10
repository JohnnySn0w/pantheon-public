# Auto-file side-observations as tickets

**Status:** **Ratified 2026-06-07 by the operator.** Drafted 2026-05-11 (per the operator msg_1311).

This is the operational SOP for handling observations that surface during normal work but aren't the current focus — bugs, possible lessons-learned, feature ideas, follow-on tasks, gotchas, retrospective items. The discipline: **file as a ticket, don't try to handle inline, don't lose the observation.** Applies to all three agents (Orchestrator/Recovery/Specialist); the message from the operator was "it's important the team feels fine to submit their own tickets."

## When to use this

Fires whenever an agent (during normal work) notices something that:

- **Needs work** at some point, OR
- **Is worth thinking about** later, OR
- **Would side-track current work** if handled inline.

Concretely, the trigger phrases that signal an auto-file opportunity:

- "...we should probably also..." → file an idea-capture ticket.
- "...this would be cleaner if X..." → file a refactor candidate.
- "...interesting that Y happened..." → file a possible-lesson observation.
- "...I notice Z is broken but it's not blocking..." → file a bug.
- "...we keep doing W manually..." → file a tooling-candidate.
- "...the spec said A but the substrate has B..." → file a §clarification-candidate.
- "...this research-arc deliverable surfaces above-the-bar items..." → file one ticket per item BEFORE resolving the parent research ticket (see §Research-arc deliverables below).

**Do NOT use this for** (handle inline instead):

- Observations that are part of the current ticket's scope — fold into the active work.
- Errors blocking the current cycle — surface to the orchestrator or HO immediately, don't quietly file-and-continue.
- Items the autonomy bar says need the operator's input now — escalate via Telegram per the autonomy-bar SOP, don't ticket-and-defer.

## Procedure

### 1. Recognize the observation

When the trigger fires (you notice something side-track-worthy), pause for one beat to ask:

- **Is this the current ticket's scope?** → handle inline; not auto-file material.
- **Is this blocking?** → escalate immediately; not auto-file material.
- **Is this something we want to remember + come back to, but not now?** → auto-file.

The instinct to keep going (don't break the flow) is the right one for the current work. The auto-file is the discipline that lets you NOT lose the observation while you keep going.

### 2. File the ticket

Use `tools/ticket.py file` with the appropriate target:

```bash
python3 tools/ticket.py file \
  --requester <your-agent-id> \
  --target <appropriate-owner> \
  --title "<short descriptive title>" \
  --labels "<comma-separated-labels>" \
  --ask "<one-paragraph description of the observation + what it implies>" \
  --context "<one-paragraph context: when/how surfaced, what arc/cycle, what triggered the observation>"
```

### 3. Target selection

| Observation kind | Default target | Notes |
|---|---|---|
| Bug in current agent's domain | `orchestrator` (or `orchestrator`) | HO triages + decides who handles |
| Bug in another agent's domain | the relevant agent (`recovery`, `specialist`, `specialist`) | They'll inherit the autonomy-bar check on pull |
| Possible lesson / pattern observation | `orchestrator` | Lessons authoring is HO-tier per ORG-012 methodology |
| Feature idea / spec-question | `operator` (label `interactive-required`) or `orchestrator` (HO drafts strawman, surfaces to the operator) | Pick by whether the idea needs the operator's framing OR HO can draft autonomously |
| Tooling gap (script you wish existed) | `orchestrator` | HO triages: HO-direct OR Recovery-target depending on the tool |
| Spec §clarification-candidate (the spec says X, substrate has Y) | `orchestrator` | HO batches into resolve-summary at cycle close |

### 4. Naming + labels

**Title**: short + descriptive (< 80 chars). Pattern: `<category>: <observation>` e.g. `bug: ticket.py claim races with grooming push`, `idea: cache_read share as margin lever for B2C surfaces`, `refactor: Message type duplication between api.ts and stores.ts`.

**Labels** (canonical set; add new sparingly):
- `bug` — broken or wrong behavior.
- `idea` — design / feature thought.
- `refactor` — known-cleaner shape; not broken but worth tightening.
- `lesson-candidate` — pattern observation that might warrant a lessons/ entry.
- `spec-clarification` — spec-vs-substrate mismatch worth surfacing.
- `tooling` — wishful-tool / script-you-wished-existed.
- `from-<arc>` — cycle attribution (e.g. `from-b36`, `from-T-0206`).
- `low-priority` — explicit deferral marker (separate from `not_before` calendar gate).

### 5. Body shape

**`## Ask`**: one paragraph naming the observation + what it implies. Don't write a 5-page spec; the point is to *capture*, not *fully solve*. The puller does the deeper scoping.

**`## Context`**: when/how surfaced, what arc/cycle/work prompted the observation, what triggered the catch. This lets the puller assess freshness + relevance without having to dig.

**`## Pairs with`** (optional): if the observation cross-references existing tickets / SOPs / ADRs / bugs entries, link them. Helps the puller see the lineage.

**`## Out of scope`** (optional): if the observation has obvious tangential ideas you noticed but want to firewall off, name them. Prevents scope-creep at pull-time.

### 6. Do NOT block on the auto-file

The auto-file is a side-effect of your work, not a pause. Run the `ticket.py file` invocation, watch for the file-success line, and return to your current task. Don't read your own ticket back. Don't draft a more-thorough version. Don't write the design notes. **Capture-and-continue is the discipline; the puller can scope further.**

If the file invocation fails (e.g., git push conflict): log the failure, retry once with `--no-push`, continue. If both fail, write the body to a notes scratch file with a timestamp + filename, surface to HO via SendMessage or Telegram as a single line. Don't let auto-file failures derail current work.

## Agent-specific notes

### Orchestrator (HO)

- HO is the natural triage point for many observations; HO-target auto-files are common.
- HO can also self-target tickets for own future work — completely fine. The auto-consumer will surface them on the next pull cycle.
- For lessons-candidates, HO drafts the lessons/ entry inline when the observation is ripe + HO-autonomous per ORG-012. Filing as `lesson-candidate` ticket is for the cases where HO isn't ready to author yet.

### Recovery (claude-fallback)

- Recovery observes a lot during grooming + system-monitor work. Most observations are tooling/bug/refactor candidates in pantheon-config (or another live project repo — e.g. kino-pwa, codex-config).
- Recovery-target tickets for own work, HO-target for items needing triage. Don't hesitate to file — the cost of an extra ticket Recovery could have done inline is much lower than the cost of losing the observation.
- Use `requester: recovery` so the ledger thread is clean.

### Specialist (claude-research / specialist)

- Specialist surfaces external observations (research findings, watch-report deltas, etc). The cron-fired auto-actuation loop (B11 / T-0215 pattern) files these as tickets when the daily README index gets built — that covers topics-list-driven watch_runner dispatches.
- For **episodic research arcs** (ad-hoc HO-dispatched surveys, NOT watch_runner topic dispatches), the B11 path does NOT fire — those deliverables land in `knowledge/episodic/<date>-<topic>/` but bypass the auto-actuation index. Specialist must **manually file individual tickets for each above-the-bar finding BEFORE resolving the parent research ticket**. See §Research-arc deliverables below for the canonical shape.
- For *internal* observations during research dispatch (e.g., "the topic-list itself has a gap here"), Specialist files just like Recovery — use `requester: specialist` + `target: specialist` (own work) or `orchestrator` (cross-cutting).

## Research-arc deliverables — above-the-bar item filing

Applies to: any agent (Specialist primarily, Orchestrator secondarily) completing a research-arc deliverable that surfaces actionable / pilot-worthy / ratify-worthy items.

**The rule:** before resolving the parent research ticket, file one ticket per above-the-bar item. Each ticket cites the deliverable as source via `--context` reference. Targets per item type:

- **Primitive to borrow / Pantheon eval candidate / discipline-anchor** → `target: orchestrator` for triage + design-tier ratification draft.
- **Pilot direction / mechanism choice / architectural decision** → `target: operator` with `ratifiable_files` attached IF the deliverable points at an ADR; otherwise `target: orchestrator` to draft the ADR per ORG-015's allow-list.
- **Followup research deeper-dive** → `target: specialist` for Specialist's own queue.

**Labels:** include `b11-manual` (distinguishes from cron-fired B11 actuations) AND `from-<agent>-arc-<NNN>` (cross-references the parent research ticket).

**Why not just Telegram-relay:** Telegram messages are ephemeral; tickets are durable + groomed + queryable. Above-the-bar findings WILL come up again 30+ days later when the operator or HO is making a related decision; a ticket queue makes that retrieval cheap, Telegram-only loses it. Plus P7: filing is near-zero cost; the deliberation tax of "is this worth a ticket" outweighs the file action itself.

**Concrete shape — applies retroactively to today's manual-lift backfill (T-0384/T-0385/T-0386/T-0387 from Specialist's T-0378 deliverables):** at resolve time of T-0378, Specialist would have filed those four tickets herself rather than Telegram-relaying. Recovery's manual lift was correct triage but the discipline should make it automatic.

## DECISIONS (SOP-tier)

- **Auto-file as the default response** to side-observations (vs. inline-or-discard) — justifies via D1 (the operator bandwidth: tickets are the canonical capture surface; lost observations cost MORE than the auto-file overhead) + msg_1311 explicit framing ("it's important the team feels fine to submit their own tickets").
- **Capture-and-continue, NOT capture-and-scope** — justifies via D10 (context-frugality: filing is cheap, scoping is expensive; the puller has fresher context for scoping) + P1 (architecture: the auto-consumer pull flow IS the scoping surface).
- **Target-selection table per observation kind** — justifies via D9 (categorical answer + format + timing): the table avoids the "where do I file this?" decision burden, which is itself a side-track.
- **Don't escalate-via-Telegram for routine observations** — justifies via msg_1271 calibrate-higher (Telegram bandwidth is for decisions the operator actually needs to make; routine observation-capture goes via the ticket queue + auto-consumer).
- **Label canonical-set + add new sparingly** — justifies via P5 (consistency): the canonical set is the search surface; proliferating labels degrades that.
- **Research-arc deliverable above-the-bar items get individual tickets at resolve time** (not Telegram-relay-only) — justifies via P7 (cheap action: filing is near-zero cost; deliberation tax of "is this worth a ticket" outweighs the file action) + D1 (the operator bandwidth: durable ticket queue beats ephemeral Telegram for items retrievable 30+ days later) + T-0388 (this discipline-amendment was filed after T-0378's deliverables surfaced 4 above-the-bar items that Recovery had to manually lift as T-0384/T-0385/T-0386/T-0387; rule retroactive-applies).

## Cross-references

- **`bugs/`** — the canonical destination for surgical-fix-with-repro bugs (small + lookup-via-grep). Some auto-filed tickets become bugs/ entries when ripe; the ticket → bugs/ path is HO-authored per ORG-012.
- **`lessons/`** — abstract patterns. Auto-filed `lesson-candidate` tickets feed this when patterns emerge across multiple observations.
- **`incidents/`** — postmortems. Distinct from auto-file (incidents are full retros, not side-observations).
- **`sop/standard/ho-absent-operator-keep-working.md`** — the SOP this complements; auto-filing is one mechanism for "swap to next completable" — observations that would have side-tracked become queue items.
- **`ORG-012 — Information-inflow bubble-up methodology`** — names the tier-map this SOP feeds into; routine `bug` / `idea` / `refactor` auto-files land at the lowest tier; the bubble-up methodology covers when they promote upward.
- **T-0215-shape watch-actuation pattern** — Specialist's auto-file flow for external findings. This SOP extends that pattern to internal observations across all three agents.
- **Telegram msg_1311** — the operator framing that originated this SOP.
