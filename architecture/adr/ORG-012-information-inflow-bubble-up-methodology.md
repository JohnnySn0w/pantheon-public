---
aliases: [ORG-012]
title: Information-inflow bubble-up methodology
status: Ratified 2026-05-14
date: 2026-05-14
build_disposition: built
---
# ORG-012 — Information-inflow bubble-up methodology

**Status:** Ratified 2026-05-14T13:00:30Z per T-0275 (the operator resolve "Ratified"). Drafted 2026-05-11; revised 2026-05-14 addressing all 8 feedback points from the operator's 2026-05-13T02:34Z block. Phase B (Recovery + Specialist persona-brief amendments per Q5 resolution) tracked as follow-on ticket.

**Date authored:** 2026-05-11

**Author:** Orchestrator (Orchestrator)

**Revision history:**
- 2026-05-11 — Initial draft, 5 open questions to the operator.
- 2026-06-11 — Bookkeeping correction (Orchestrator): §Cadence 6 + §Phases 3 still read "design pending" five days after Phase C actually closed — T-0781 (Specialist, resolved 2026-06-06) operationalized the sweep as a DAILY scripted mechanism per the operator msgs 294/296. Updated to pointers at the landed mechanism. NOTE: earlier the same day Orchestrator, misled by the stale text, authored a duplicate quarterly-mechanism SOP + calendar ticket (ledger -179); retracted within the hour on discovering T-0781 (ledger -180). The error class is exactly ORG-012's own "unmarked supersedure."
- 2026-05-14 — Revised: (1) softened "lift to directive" default in §6 per directives-as-technical-temporary feedback; (2) added non-tautological second-condition for directive→principle threshold in §4; (3) clarified directives can fully retire (not just supersede) distinct from principles in Demotion §1; (4) rewrote "Subsumed by higher tier" wording in Demotion §3 (the demotion is the lesson's load-bearing status, not its content); (5) added "Telegram-conversation insights from the operator" as first-class inflow tier in §Source-tier mapping; (6) added weekly memory-pin sweep cadence to §Cadence per msg_~1271 framing; (7) confirmed cross-quarter automated-sweep design as Phase C scope; (8) fixed Q5 typo (was "HO-Recovery-Orchestrator" — Orchestrator IS HO; corrected to "HO-Recovery-Specialist") + extended methodology applicability to Recovery + Specialist persona briefs.

## Context

Pantheon's information surface is broad — internal (incidents, kerfuffles, lessons, arc retros, HO self-observation, ticket close-outs) and external (research dispatches, watch reports, vendor changelogs, public discussions). The output tiers are equally varied (bugs/, incidents/, notes/, lessons/, sop/standard, sop/remediation, rulebook/directives, rulebook/principles, adr/, feedback memory pins). What's been missing is the **methodology for how an observation at one tier moves up (or down) to another tier** — what triggers promotion, what evidence justifies it, who ratifies, what cadence the review runs at, how decay works against the append-only audit chain.

Without a methodology, the de-facto practice has been: HO authors lessons + bugs/ + notes/ ad-hoc, the operator ratifies SOPs / directives / principles / ADRs through Telegram conversations, and demotion happens implicitly via re-evaluate-date triggers (e.g., the HERMES discipline's 2026-06-01 re-eval) or via simple deletion. That works at audience-of-one scale but is opaque to future-Orchestrator context-loads and to any external auditor (or scale-out to more agents).

Filed as T-0168 asterisk-followup to T-0158 §4 ratification per the operator msg_863. Pairs with T-0157 (SOP folder), T-0158 (lessons place), T-0160 (SOP standard-bucket), T-0163 (operator-theories).

## Forces

- **D2 (audit-chain integrity)** — promotion/demotion must be traceable in the ledger; ratification must be explicit, not folkloric.
- **D1 (the operator's bandwidth)** — the methodology must NOT require the operator ratifying every lesson-to-SOP step; tier-jumps must have clear delegation rules.
- **P1 (architecture over policy)** — structural promotion rules scale; ad-hoc per-promotion judgment doesn't.
- **D8 (HO self-discipline)** — HO drafts most of the inflow surface (lessons, bugs/, incidents/); the methodology must let HO act autonomously within scope without flooding the operator's ratification queue.
- **D10 (context-frugality / minimum-traversal)** — over-defining the methodology creates its own brief-bloat; the methodology itself shouldn't be a 30-page document HO and the operator have to re-read every cycle.

## Tier map

The tiers, lowest-to-highest in "weight":

| Tier | Container | Permanence | Promotable upward? |
|---|---|---|---|
| **Working scratch** | `notes/`, ticket transition logs, ledger comments | Ephemeral | Rarely (becomes a lesson if pattern emerges) |
| **Concrete gotcha** | `bugs/` (technical symptom + surgical fix) | Permanent terminal | No (bugs stay bugs; archive at decay) |
| **Postmortem** | `incidents/` (one-shot fail postmortem) | Permanent terminal | No (incidents stay incidents; lessons abstract from them) |
| **Abstracted pattern** | `lessons/` (multi-incident generalization) | Persistent until contradicted | Yes → SOP / directive / ADR |
| **Operational playbook** | `sop/standard/` (what to DO at each stage); `sop/remediation/` (what to do when X breaks) | Persistent until contradicted | Yes → directive (if structurally enforceable) |
| **Org-level rule** | `rulebook/directives.md` (D-class) | Persistent; D2-protected | Yes → principle (if foundational) |
| **Org foundation** | `rulebook/principles.md` (P-class) | Persistent; D2-protected; rarely changes | No (principles are terminal-tier within the rule stack) |
| **Architectural decision** | `adr/ORG-NNN` | Persistent; D2-protected; supersedure chain | No (ADRs supersede other ADRs but don't promote) |
| **HO-self-preference** | `feedback_*.md` memory pins | Persistent until removed; HO-scope only | Yes → SOP (when the rule is role-defining for HO, not preference) |

**Two-axis distinction.** Vertical (severity/permanence) is what most promotion talk addresses. Horizontal (kind) matters too: ADRs are architectural decisions, not operational rules; SOPs are operational, not architectural; bugs are surgical-fix catalogs, not abstract lessons. A single observation can land in multiple tiers if it has both an architectural and operational shape (e.g., the T-0214 sync HEAD-vs-main bug landed both as a `bugs/` candidate AND as the structural T-0214 fix; ORG-011 is architectural but cites the prose-discipline SOP for operational layering).

## Promotion criteria

Promotion is the move from a lower-tier container to a higher-tier one. Each tier-jump has a threshold:

1. **scratch → bug / incident / lesson**:
   - `bugs/`: reproducible symptom + surgical fix + likely to recur. One-shot author judgment (HO writes; cross-reference at write time).
   - `incidents/`: one-shot failure with operator-visible impact worth a postmortem. HO writes; ratification implicit-on-write since the document is the audit chain.
   - `lessons/`: multi-incident pattern (2+ instances of the same shape) OR explicit the operator request. HO writes; surface for ratification if the lesson asserts a behavioral rule that touches D2 surfaces.

2. **lesson → SOP**:
   - **Threshold**: the lesson asserts a *role-defining operational sequence* (not just a discipline reminder). Test: would naming this in a worker persona's read-order be load-bearing? If yes, it's SOP-shaped, not lesson-shaped.
   - **Direction**: lessons abstract from incidents; SOPs codify the prescriptive answer the lesson points at. Often paired: lesson + SOP cite each other.
   - **Ratification**: the operator ratifies (D2 — SOPs are hand-curated org-level). HO drafts strawman.

3. **lesson / SOP → directive**:
   - **Threshold**: the rule is structurally enforceable AND is org-tier (not project-tier or worker-tier). Test: can this rule be encoded in tooling / hooks / monitor cadence? If yes, it's directive-eligible.
   - **Direction**: directives compress repeatable patterns into named D-class rules. The directive THEN motivates an SOP (operational) and possibly an ADR (architectural).
   - **Ratification**: the operator ratifies (D2). HO drafts amendment; surface batched (avoid per-rule ratification fatigue).

4. **directive → principle**:
   - **Threshold (two conditions, both required)**: (a) the directive captures something foundational — a load-bearing constraint that other directives implicitly assume; (b) the directive is explicitly cited in 2+ other directives' `justifies-via` chains as the ground reason (not just as a same-tier reference). The (b) test is what keeps (a) from being self-referentially tautological — "does removing it cascade-break" can be self-reinforcing because directives reference each other; (b) requires the citation to be *grounding*, not lateral.
   - **Direction**: very rare. Principles are the org's foundation; promotions to principle are major moves. Strong bias against by default — most directives stay directives forever (or retire).
   - **Ratification**: the operator ratifies explicitly (P-tier promotion is the most-load-bearing change possible to the rulebook).

5. **lesson / SOP / directive → ADR**:
   - **Threshold**: the observation is architectural — names a *decision among alternatives* with named tradeoffs, not just a behavioral rule. Test: does framing this require "Forces / Candidate mechanisms / Recommendation / DECISIONS" structure? If yes, it's ADR-shaped.
   - **Direction**: ADRs aren't promotions in the strict tier sense — they're a different *kind* of container. A lesson can spawn an ADR if the lesson's structural-fix needs the ADR shape (Forces + alternatives + decision); a directive can be paired with an ADR motivating it.
   - **Ratification**: the operator ratifies (D2 — ADRs are architectural-tier hand-curated).

6. **HO memory pin → SOP / (rarely) directive / (rarely) ADR**:
   - **Threshold**: per ORG-011 revision (msg_1298 framing) — memory is HO-self-preference; SOP/directive is org-tier role-defining. Test: is the rule role-defining for any agent (not just HO's habit)? If yes, lift to SOP minimum. **Promotion to directive is rare** — only when the rule is structurally enforceable AND will be load-bearing for multiple workflows (directives were originally conceived as technical/temporary objects per msg_~1271-window framing; they can demote or fully retire). **Promotion to ADR** when the rule names a *decision among alternatives* with named tradeoffs (Forces / candidate mechanisms / DECISIONS), not a behavioral rule. Most memory-pin promotions land at SOP-tier; rarely directive; ADR if architectural-shape.
   - **Direction**: HO drafts the memory pin first (lightweight); when promotion criteria meet, HO drafts the SOP/directive/ADR and (per the memory-vs-SOP rule) removes the redundant memory pin OR converts it to a reference pointer.
   - **Ratification**: the operator ratifies promotion (D2). HO's autonomous-pin-then-promote is the cheap-iteration pattern.

## Demotion criteria

Demotion is the loss of tier-status. The append-only audit chain doesn't *delete*; it supersedes-with-trail.

1. **Empirical contradiction**: an observation directly contradicts the rule. Example: a re-evaluate-date triggers on the HERMES byte-sequence discipline (file `bugs/HERMES\.md`), evidence shows the bug is gone, the discipline retires. **Directives can fully retire here** (mark `Retired: <date>` inline; do not delete) — distinct from principles, which are foundation-tier and only supersede-with-trail, never retire.
2. **Operating-context change**: scale shifts, audience changes, tooling moves. Example: when ORG-011 Phase D lands (structural Path 1 reliable), Path 4 prose discipline retires.
3. **Load-bearing-status transfer to a higher tier** (this is a demotion *of the lower-tier item's load-bearing status*, not of its content): when a lesson is promoted to a directive, the directive now carries the rule forward; the lesson stays as historical artifact in `lessons/` but is no longer the load-bearing reference (the directive is). The cross-reference at the top of the lesson reads `Superseded as load-bearing by D-NN (date)`. The promotion ARC contains both events — promotion of the rule + demotion of the lesson's load-bearing status — but they're the same arc, recorded once.

**Audit chain mechanics**:
- Rulebook items (directives + principles): mark as `Superseded: <date> per <id>` inline; do not delete. The next reader sees both the historical content + the supersedure pointer.
- ADRs: explicit `Superseded by ORG-NNN` block at top; same pattern.
- SOPs: similar inline supersedure note.
- Lessons + bugs + incidents: rarely demoted; they're historical artifacts. If genuinely wrong, add a `Retracted: <reason>` block at top; don't delete.
- Memory pins: HO removes (the pin's purpose is current behavior; historical state of memory doesn't matter to future-Orchestrator). The deletion is the demotion.

**Re-evaluate triggers** are the formal mechanism for cadence-driven demotion review. Pattern: any rule with empirical uncertainty gets a `Re-evaluate <date>` stamp + a `not_before <date>` calendar-trigger ticket. When the trigger fires, HO re-evaluates per the rule's evidence model.

## Source-tier mapping

Initial map (per the ticket's framing #3, refined):

| Source | Primary tier | Secondary tier | Notes |
|---|---|---|---|
| Incidents (one-shot ops fails) | `incidents/` (postmortem) | `bugs/` if surgical-fix surfaces; `lessons/` if pattern (2+ instances) | HO authors; D2 ratification on `lessons/` upgrade |
| Kerfuffles (smaller fails, in-arc) | Ticket transition log; ledger comments | `bugs/` if pattern; `lessons/` if pattern | HO judgment at arc-close |
| Lessons (arc retros) | `lessons/` | `sop/` if operational; `rulebook/directives.md` if structural; `adr/` if architectural | HO drafts; the operator ratifies promotion |
| Kino-arc retrospectives | `adr/` (if architectural decision recorded); `lessons/` if behavioral | Mixed | Arc-close discipline |
| Research dispatches (Specialist) | `knowledge/episodic/<date>-<topic>/` | `directives` or `adr` only if findings cross promotion threshold | Most research stays research; promotion is rare + operator-ratified |
| Watch reports (Specialist) | `knowledge/watch/reports/<date>/` | Auto-filed tickets via T-0215-shape actuation; promotion via ticket-pull | Specialist auto-actuates; HO triages per ticket pull |
| Vendor changelogs | Watch reports (Specialist surfaces) | Same as above | Same as above |
| Public discussions (HN, X, etc.) | Watch reports (Specialist surfaces) | Same as above | Same as above |
| HO self-observation (e.g., drift catches) | `feedback_*.md` memory pin (first pass) | `sop/` if role-defining; `directive` if structurally enforceable | The msg_1298 lesson: HO often gets the layer wrong first; correct is to draft pin → promote to SOP if role-defining |
| Telegram-conversation insights from the operator | Ledger entry (capture cite + framing); HO memory pin if rule-of-thumb-shape | `sop/` if operational guidance; `directive` if structurally enforceable; `adr/` if architectural decision-among-alternatives | First-class inflow surface, distinct from "operator-initiated" (which is the operator's *request* for documentation); these are *spontaneous* framing/calibration signals that HO must catch + route. Operator-initiated → HO drafts on request; Telegram-conversation insights → HO captures proactively at conversation-close. |
| Ticket close-outs (resolve summary) | Ledger entry | `lessons/` if pattern emerges across closes | Routine close → ledger; pattern across closes → lesson |

## Cadence

The bubble-up review happens at multiple cadences:

1. **Per-arc-close** (ticket resolve, b#-cycle close, ADR ratification): HO scans whether the arc surfaced anything promotion-worthy. Lightweight — most arcs surface nothing new. If they do, HO files the strawman immediately.
2. **Operator-initiated**: the operator surfaces "I want this written down" via Telegram → HO drafts strawman.
3. **Re-evaluate-date triggers**: Calendar tickets fire (`not_before <date>`); HO does the re-eval per the rule's evidence model.
4. **Watch-finding actuation** (Specialist-driven): research / watch reports auto-file tickets per T-0215 pattern; HO triages on consumer-pull; promotion to higher tier happens via the actuation queue.
5. **Weekly memory-pin sweep** (per the operator msg_~1271-window feedback): every 7 days, HO scans `~/.claude/projects/-home-claude/memory/feedback_*.md` for pins that are stale (rule no longer applies), should-have-been-promoted-already (pin is functionally an SOP), or duplicative (multiple pins covering the same rule). Outcome per pin: keep / promote-to-SOP / retire. Tight cadence reflects the memory-vs-SOP layering risk surfaced at msg_1298 (HO tends to over-pin instead of promoting).
6. **Cross-quarter automated artifact sweep** (Phase C scope — design TBD; ratified-in-principle 2026-05-14 per the operator feedback): every ~3 months, an automated process sweeps `lessons/` + `feedback_*.md` + `sop/` + `bugs/` + `incidents/` for throughlines — recurring patterns, contradictions, supersedure-candidates, abandoned drafts. Findings surface to HO + the operator as a batch report. **OPERATIONALIZED 2026-06-06 (T-0781), at DAILY cadence per the operator msgs 294/296 (supersedes the quarterly figure above):** `tools/knowledge_sweep.py` (content-fingerprint diff gate → sweep-dispatch ticket on corpus change) + `pantheon-knowledge-sweep.timer` (13:00 UTC daily, Specialist account) + in-session review fan-out with VERIFY-against-repo gate; results file per `sop/standard/throughline-sweep-result-filing.md` (parent index + discrete sub-tickets). Corpus includes `feedback_*.md` (Recovery scope note). First run landed T-0869 + 6 sub-tickets.

The per-arc + operator-initiated + watch-finding cadences are the steady-state inflow handlers. Weekly memory-pin sweep is the maintenance discipline at HO-self-scope. Cross-quarter sweep is the maintenance discipline at org-artifact-scope; operationalized 2026-06-06 as the daily scripted sweep (T-0781, `tools/knowledge_sweep.py`).

## Ratification gates

| Tier | Who ratifies | When | Notes |
|---|---|---|---|
| `bugs/` | HO autonomous | At write | Per D2 — `bugs/` is HO-curated technical catalog; no per-entry gate |
| `incidents/` | HO autonomous | At write | Postmortem is the audit chain; ratification is implicit |
| `lessons/` (creation) | HO autonomous | At write | Per D2 — HO authors |
| `lessons/` (asserting a behavioral rule that touches D2 surfaces) | the operator | Pre-merge | Surface for ratification with the rule cited |
| `sop/remediation/` | the operator | Pre-merge | Operational playbook is org-level |
| `sop/standard/` | the operator | Pre-merge | Same |
| Directive amendment | the operator | Pre-merge | D2-protected |
| Principle amendment | the operator | Pre-merge, explicit | P-tier; highest-load-bearing |
| ADR (new) | the operator | Pre-merge | D2-protected; HO surfaces strawman |
| ADR amendment (in-section edit) | the operator | Pre-merge | Even small edits touch D2 |
| ADR supersedure | the operator | Pre-merge | New ADR with explicit supersedure pointer |
| HO memory pin | HO autonomous | At write | Self-discipline; no gate |
| HO memory pin → SOP promotion | the operator | Pre-merge of the SOP | The promotion IS the ratification trigger |

Per msg_1271 calibrate-higher: HO should NOT surface per-bug or per-lesson for ratification unless the lesson asserts a rule that's D2-protected (touches a principle, directive, or ADR). Routine catalog writes are auto-Ratified-by-default with flag-if-revert. The list above respects that.

## DECISIONS

- **ADR shape over SOP shape for this methodology** — justifies via P5 (the methodology defines tier-relationships + decision-making among alternatives — Forces, options, ratification gates — that's ADR-structure; SOP is for operational sequence-of-actions). The methodology will likely *spawn* an SOP for the per-promotion-process operational steps; but the methodology itself is architectural.
- **Two-axis tier model (vertical + horizontal)** — justifies via D5 (literal-state-vs-paraphrase: a flat vertical model would hide that ADRs and directives are different *kinds* of containers, not just different tiers; the two-axis explicit framing prevents the conflation).
- **Demotion via supersedure-with-trail, not deletion** — justifies via D2 (append-only audit chain) + P5 (consistency with how the ledger handles state changes). Memory pins are the only exception; their lifecycle is internal-to-HO and removal is the demotion.
- **Re-evaluate triggers as the cadence-driven demotion mechanism** — justifies via P1 (structural enforcement of re-eval is calendar-tickets; prose "remember to re-evaluate" would degrade per HERMES-arc lesson) + the proven pattern from T-0031/T-0049 HERMES re-eval cycle.
- **HO autonomous for `bugs/` + `incidents/` + `lessons/` (creation) per msg_1271 calibration** — justifies via D1 (the operator bandwidth — routine catalog writes don't need ratification) + msg_1271 explicit framing (don't ratify by-the-book content).
- **Per-promotion ratification batched, not stream-of-singletons** — justifies via D10 (context-frugality on the operator's side; batched surfaces let him decide multiple promotions in one Telegram exchange rather than N separate decisions).
- **Cross-quarter review proposed but not gated** — justifies via P6 (preserve tension: too-frequent review burns context; too-infrequent lets cruft accumulate; calibrate empirically).

## Open questions — resolution log

The 5 open questions from the initial draft are now resolved per the operator's 2026-05-13T02:34Z feedback. Captured here as the resolution audit:

1. **Tier map accuracy**: the operator confirmed the tier map is mostly right + flagged the missing "Telegram-conversation insights from you" inflow surface. **Added** as a first-class row in §Source-tier mapping, distinguished from "operator-initiated" (which is the operator's *request* for documentation) — Telegram-conversation insights are *spontaneous* framing/calibration signals.
2. **Cross-quarter review cadence**: the operator leaned toward automated quarterly sweep — "reviewing every quarter in some automated fashion that sweeps the artifacts for throughlines. Might take some noodling to shape that right." **Resolved** to: cadence ratified in §Cadence #6; mechanism design pending Phase C scope.
3. **Two-axis model adoption**: the operator confirmed — "I love the branching of the model, it's quite good. ADRs are on their own path, that's really good." **Kept** as-is in §Tier map.
4. **Memory-pin promotion path**: the operator suggested weekly sweep — "memory pins, maybe we sweep them on a weekly basis?" **Resolved** to: §Cadence #5 weekly memory-pin sweep at HO-self-scope.
5. **Cross-agent alignment** (was: "HO-Recovery-Orchestrator alignment" — typo; Orchestrator IS HO, the doublet was wrong): the operator resolved — "I think the others should have it yes, or SOPs related to their portions." **Resolved** to: the methodology applies cross-agent (HO + Recovery + Specialist); each persona brief should reference it OR import the operational portions relevant to their role. Phase B scope item: amend Recovery + Specialist persona briefs accordingly.

**Newly open** after the operator's feedback (require his final ratification before this ADR closes):

- The Demotion §3 wording rewrite — does the new framing ("load-bearing-status transfer to a higher tier" as the demotion arc of the lesson, the rule itself promoting) match your read, or is the framing still confused?
- The directive→principle two-condition test (clarity check) — does the (b) "explicitly cited in 2+ other directives' justifies-via chains as the ground reason" rule out the tautology concern adequately?
- Phase B scope: amending Recovery + Specialist persona briefs is mechanical but needs your sign-off on what subset of this methodology each adopts. Default proposal: full text references; per-persona import of operational portions.

## Implementation arc (post-ratification)

1. **Phase A — Ratify methodology** (this ADR). Once ratified, the methodology itself becomes the audit chain reference for all future promotions.
2. **Phase B — Surface tier-map mismatches**: if Phase A finds any source-tier mapping is wrong, fix the ADR before B starts. Then HO authors the missing "promote-this-to-X" cross-references in existing lessons / SOPs / bugs / etc. to backfill the chain.
3. **Phase C — Operationalize cross-quarter review**: if the cadence is ratified, set up the calendar-trigger ticket. Else skip. **DONE 2026-06-06 (T-0781, Specialist)** — daily scripted sweep per the operator msgs 294/296; timer + diff-gate replace the calendar-trigger-ticket shape.
4. **Phase D — Operational SOP for the per-promotion process**: if the per-promotion mechanical steps need codification (file location, frontmatter, ledger entry shape), author `sop/standard/promotion-operational-steps.md` as a follow-on. Could be done now but better to land the methodology first + observe a few promotions before codifying the operational shape.

## Cross-references

- **Principles + directives**: D1 (the operator bandwidth), D2 (audit chain integrity), P1 (architecture over policy), D8 (HO self-discipline), D10 (context-frugality).
- **Lessons**: [`lessons/structural-enforcement-retires-the-prose-rule.md`](../lessons/structural-enforcement-retires-the-prose-rule.md) (the promotion lesson: prose → structural enforcement is a tier-jump).
- **Tickets**: T-0168 (this ticket), T-0158 (lessons place — parent), T-0157 (SOP folder), T-0160 (SOP standard-bucket), T-0163 (operator-theories), T-0049 (HERMES re-evaluate — the prototype re-eval-trigger pattern).
- **ADRs**: ORG-002 (principles-directives split — the foundational tier definition), ORG-010 (three-tier policies extension — recent tier-related work), ORG-011 (just-revised — example of the three-layer SOP+architecture+prose pattern this methodology generalizes).
- **Telegram refs**: msg_863 (the original asterisk-followup that filed T-0168), msg_1271 (calibrate-higher — explicit "don't ratify by-the-book content"), msg_1298 (memory-vs-SOP layer correction — example of an inflow surface mis-layering).
