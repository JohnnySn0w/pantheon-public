# Org churn is constant — methodology must be concise

**Date authored:** 2026-05-10
**Author:** Orchestrator (Orchestrator) — drafted from the operator's articulation msg_826 + msg_876
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015; the older "awaiting ratification" convention predated it)
**Re-evaluate when:** the underlying agent-context-degradation findings (T-0159 / `knowledge/watch/reports/2026-05-09/context-degradation-curves.md`) refresh; or when the org's substrate changes (model family swap; harness shape change; multi-account topology change).

## Observation

In an org composed largely of agents — where each agent has a finite context window, no persistent across-conversation memory beyond auto-memory + project state on disk, and is rotated frequently — **methodology must be concise + precisely-indexed + reusable, written for the lowest-capability agent that might be on the job**.

Concretely:

- **Agents memorize instantly but use selectively.** Their upside is they absorb the entire repo + every loaded skill on first read; their downside is that a 1M-token context degrades structurally — empirical evidence (BABILong, MRCR-v2) shows reasoning effectiveness drops to 10-20% of the advertised window for production-relevant tasks. (See T-0159 grounding.)
- **Org churn is constant.** Each fresh-context dispatch is an org-ramping event. There's no apprenticeship period; the agent reads the docs that exist, follows the rules they encode, and acts. If the docs are sprawling, prose-heavy, or transcluded across multiple places, the agent's effective-context budget gets eaten by re-reading rather than acting.
- **Discipline-by-prose loses to cognitive load.** Multi-violation single-emission patterns (b23 / b28 / b29 / b31 / b33) and the b9-incident teething catalog all surfaced agents *citing* the rules they were briefed on while *violating* them at the same step. The brief said the right thing; the agent absorbed it; and under load they emitted code that contradicted it. (The instruction-fidelity-collapse pattern itself is a separate lesson — this lesson is about why concise methodology is the structural answer.)

The military lessons-learned analogue (FM 3-0 doctrine + FM 6-0 SOPs + lessons-learned feedback loop) maps cleanly: the military operates with constant personnel rotation and varying-capability soldiers; their answer is concise doctrine + precise SOPs + a deliberate feedback loop where lessons promote into doctrine. Pantheon's three-tier (Principles + Directives + Policies, per T-0173 in flight) plus the SOP framework (T-0157) plus this lessons category (T-0158) is the same shape applied to an agent-org.

## Grounding

- **the operator msg_826** (2026-05-09) — articulating the load-bearing why: "*org churn is constant; methodology must be concise and recorded so even the dumbest person who might be on the job can do it. Agents are rotated frequently. Their upside is they memorize everything instantly. Their downside is their ability to use this memory is limited. 1m context is not as useful as it would seem, because context degrades halfway into that. So recording SOPs and having minimal index references to all types of information allows for quick access, and minimal mind bloat for agents, allowing them to do their jobs with fewer mistakes.*"
- **the operator msg_876** (2026-05-09 / 2026-05-10) — the structural extension: three-tier Principles + Directives + Policies, with HO authoring policies and the operator ratifying at surface time. Same load-bearing why; layered authority with calibrated authorship.
- **T-0159 research dispatch** (`knowledge/watch/reports/2026-05-09/context-degradation-curves.md`) — empirical context-degradation data: BABILong shows 10-20% effective use for reasoning; MRCR v2 cliff before 300K for everything except Claude Opus 4.6; 60% utilization rule converges across sources. Validates the operator's "halfway" intuition (directionally correct; magnitude understated — see `operator-theories/`).
- **The instruction-fidelity-collapse corpus** (b23/b28/b29/b31/b33 + T-0096 kerfuffles) — six empirical points where brief-prose-only enforcement failed under load. Lesson is in flight at T-0164.
- **D10 ratification** (2026-05-09; commit `615e83b` directives empirical-grounding citations) — explicit codification: "context-frugality / minimum-traversal-to-precise-info." Direct downstream of this lesson.
- **B9 incident report** (`incidents/2026-05-04-b9-team-pattern-first-run/`) — first-run teething showed the same pattern at the orchestrator-tier: brief had the rules; orchestrator transmitted them; gaps at tooling-shape + routing-shape surfaced anyway. Discipline-by-prose has a ceiling.

## Design implications

- **SOPs over prose.** The SOP framework (T-0157 + T-0160 standard-bucket extension) operationalizes this lesson at the operational tier. SOPs are short, named, indexed; agents read them once + cite by reference rather than re-deriving. Direct downstream.
- **References replace transclusion (D10).** Pantheon docs cross-link rather than repeat content. CLAUDE.md is the spine; project-scope skills + per-doc files hold detail. Directly downstream.
- **Three-tier structural framework (T-0173 in flight).** Principles + Directives + Policies — with explicit authoring/ratification gradient — is the structural shape that lets the operator write the durable rules without authoring every operational detail. HO authors operational policies under the operator surface-time ratification.
- **Lesson + bug catalogs as index-references.** `lessons/` (this folder) + `bugs/` + `incidents/` are themselves index-shaped: the index in each README is the high-traversal surface; the per-entry files are looked up by grep when relevant. Same pattern as `rulebook/directives.md` (single file, indexed sections) vs `adr/` (directory of per-entry files).
- **Brief authoring discipline.** Briefs to dispatchees should NOT re-state all of Pantheon's principles + directives — they should reference them by id and lean on skills (`pantheon-org-discipline`, `pantheon-dispatch`) loaded via persona frontmatter to populate the worker's context efficiently. Re-stating ledger-entries-of-rules in every brief is the inverse of this lesson.
- **The "lowest-capability agent who might be on the job" target.** When you write a doc, imagine the agent reading it has 30% effective context budget AND is one of the smaller models in the rotation. If the doc still parses cleanly, it's well-shaped. If not, simplify.

## Open questions

- **Where's the cliff between "concise enough" and "too compressed to be unambiguous"?** D5 (smoke verification literal-output discipline) suggests one answer: literal artifacts beat narrative claims. But not every domain has a smoke-test analogue. The bubble-up methodology (T-0168) may bear on this.
- **Should principles-index.md / glossary.md / phase-roadmap.md (referenced in CLAUDE.md but not yet authored) exist + be auto-loaded?** These are aspirational index-files. If authored well, they reduce traversal cost; if authored poorly, they add to the doc sprawl this lesson is fighting. Defer to T-0173's policies-tier discussion.
- **What's the right re-evaluation cadence?** Org-substrate changes (new model family, new harness shape, multi-account topology shifts) are the natural trigger. But the underlying empirical data (T-0159) refreshes too — context-degradation curves change as new model versions land. Re-evaluate at quarterly cadence + on substrate changes, whichever comes first. (Cadence to be tested over time.)

## Cross-references

- **Principles + directives**: P6 (operator comprehension), D2 (hand-curated docs), D10 (context-frugality / minimum-traversal-to-precise-info — direct downstream of this lesson).
- **CLAUDE.md sections**: §"Auto-resume on Max-window cap-hits" (the cap-hits themselves are an empirical signature of context-degradation pressure); the spine pattern (CLAUDE.md as index, not transclusion).
- **ADRs**: ORG-002 (two-tier framework — being amended to three-tier in T-0173 per this lesson's design implications); ORG-001 (ledger discipline — P4 traceability is what makes append-only audit trails worth their cost given context-frugality otherwise).
- **Tickets**: T-0158 (founding lessons-place ticket — this entry is its founding content), T-0157 (SOP framework — direct operational downstream), T-0160 (SOP standard-bucket re-routing — applies the lesson to existing notes/), T-0173 (three-tier framework — structural extension downstream of this lesson + msg_876), T-0159 (context-degradation research — empirical grounding), T-0164 (b-pattern misbehavior umbrella — sister lesson at the discipline-vs-architecture frontier).
- **Incidents**: `2026-05-04-b9-team-pattern-first-run/` (first-run teething; tooling-shape + routing-shape gaps), `2026-05-09-b33-ticket-state-sweep-multi-violation/` (instruction-fidelity collapse + orchestrator-blocked-on-silent-implementer; the discipline-vs-architecture incident).
- **Telegram refs**: msg_826, msg_859, msg_863, msg_876.
- **External**: Military FM analogue (FM 3-0 doctrine + FM 6-0 SOPs); cybernetics / VSM lens (T-0166 lesson-in-flight; Specialist 2026-04-29 dispatch); BABILong + MRCR-v2 + 60% utilization research findings (T-0159).
