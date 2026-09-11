# ORG-012 per-agent operational portions

**Status:** Ratified 2026-05-14 alongside ORG-012 (T-0275 → "Ratified" per the operator). T-0327 Phase B follow-on.

ORG-012 (information-inflow bubble-up methodology) applies cross-agent. This SOP names the §portions each agent must operationalize on their side. Agent CLAUDE.md files reference this SOP rather than duplicating tier-map content.

## HO (Orchestrator)

**Load-bearing scope:** all of ORG-012. HO is the primary author of `lessons/`, `bugs/`, `incidents/`, `notes/`; the primary drafter of `sop/`, `adr/`, directive amendments + principle amendments; the maintainer of `feedback_*.md` memory pins. HO runs the per-arc-close scan + the operator-initiated translation + the weekly memory-pin sweep + (eventually) the cross-quarter automated artifact sweep.

**Specifically:**
- §Promotion criteria (all paths): HO drafts; the operator ratifies per the §Ratification-gates table.
- §Demotion criteria: HO authors supersedure-with-trail markers; for memory pins, HO removes (deletion is demotion).
- §Cadence #1–3 + #5–6: HO runs.
- §Source-tier mapping: HO routes incoming surfaces to the correct tier; HO catches mis-routing.

## Recovery

**Load-bearing scope:** the operational-actuation seams of ORG-012.

**Specifically:**
- §Demotion criteria via grooming-cron stale-flag: Recovery's `tools/ticket_grooming.py` surfaces tickets past their `stale_after` (per the `external-blocker-stale-after-default.md` SOP); these surfaces feed into ORG-012's demotion arc when the external substrate condition has changed. Recovery's auto-resolve discipline (re-check substrate before unblocking) is the in-tooling realization of ORG-012 §Demotion #1 ("Empirical contradiction").
- §Cadence #4 (Watch-finding actuation): Recovery's `tools/inbox_consumer.py` triages auto-filed tickets from Specialist's watch reports + relays to HO. The actuation queue's freshness is Recovery's operational responsibility.
- §Source-tier mapping (HO self-observation row): Recovery catches HO drift cases (silence-period predicate, queue-poke) and surfaces them — these often become `feedback_*.md` memory pins or escalate to `sop/`. Recovery's runbook for activity-monitor pokes IS the in-tooling realization.

**NOT load-bearing for Recovery:**
- §Promotion criteria authorship (HO authors).
- §Cadence #5 weekly memory-pin sweep (HO's own memory pins; Recovery doesn't maintain pins).

## Specialist (specialist)

**Load-bearing scope:** the producer-side of the research + watch surfaces in ORG-012's §Source-tier mapping.

**Specifically:**
- §Source-tier mapping rows: "Research dispatches", "Watch reports", "Vendor changelogs", "Public discussions" — Specialist produces these. The "primary tier" for each is its dispatch/report directory (`knowledge/episodic/<date>-<topic>/`, `knowledge/watch/reports/<date>/`); promotion to higher tier happens via the actuation queue Specialist auto-files (T-0215 shape).
- §Cadence #4 (Watch-finding actuation): Specialist is the producer-side actor. Watch reports auto-file tickets via watch_runner.py; HO triages on consumer-pull; Recovery relays.
- §Promotion criteria #5 (lesson/SOP/directive → ADR via Specialist surfacing): if a research dispatch surfaces an architectural decision-among-alternatives that warrants ADR-shape, Specialist flags this in the report; HO drafts the strawman; the operator ratifies.

**NOT load-bearing for Specialist:**
- §Promotion criteria authorship (HO drafts strawman from Specialist's report).
- §Cadence #5 memory-pin sweep.
- §Demotion criteria (HO + Recovery handle).

## Cross-references

- `architecture/adr/ORG-012-information-inflow-bubble-up-methodology.md` — the methodology this SOP operationalizes per-agent.
- `sop/standard/external-blocker-stale-after-default.md` — Recovery's stale-after discipline.
- `sop/standard/auto-resume-operator-runbook.md` — Recovery's auto-resume + activity-monitor mechanics.
- `sop/standard/agent-role-map.md` — persona ↔ role-id mapping.
- `~/CLAUDE.md` (per-agent root) — should reference this SOP at the agent's read-order.

## Implementation note

Each agent's root `CLAUDE.md` (Orchestrator: `/home/claude/CLAUDE.md`; Recovery: `/home/claude-fallback/CLAUDE.md`; Specialist: `/home/claude-research/CLAUDE.md`) should reference this SOP in its read-order. The shared-group `pantheon-shared` (per T-0281) means all three accounts can read `/home/claude/pantheon-config/sop/standard/` so the reference is structurally accessible.

HO has direct write access to `/home/claude/CLAUDE.md` only. Recovery + Specialist owners (or themselves at next maintenance session) add the cross-reference to their root `CLAUDE.md`. Tracked as follow-on coordination if needed; the SOP itself is the durable contract.
