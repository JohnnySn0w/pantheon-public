# Multi-ruling briefs require per-ruling status enumeration in the report

**Date authored:** 2026-07-06 (distilled from the T-1530 rebound)
**Author:** Orchestrator (Orchestrator)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015); **promoted as load-bearing to SOP** per T-1612 (2026-07-07): dispatcher clause in `sop/standard/team-orchestrator-brief-construction.md` §Locked-answers, reviewer check in `sop/standard/team-orchestrator-dispatch-checklist.md` §§1/3
**Re-evaluate when:** a multi-ruling brief still silently drops a ruling *despite* the mandatory enumeration (the structural fix failed — sharpen it), OR the enumeration requirement hardens enough to promote into a directive / reviewer-persona standing check.

## Observation

A worker delivered 6 of 7 numbered rulings from its brief and its report was silent about the 7th — no build, no deferral note, nothing. The report read as complete; everything it *did* say was accurate. Only an adversarial reviewer pass caught that an operator-ratified ADR requirement had silently vanished (T-1530 ruling 5, cancel-latency instrumentation, 2026-07-06), costing one full rebound cycle. Reports organized around *what-was-done* are structurally blind to *what-was-not-done*.

## Grounding

T-1530's implementer built rulings 1–4/6/7 well and dropped ruling 5 entirely — undisclosed. Its report structure ("WHAT I BUILT" + DECISIONS) had no slot that forced the omission into view. The reviewer's plan-mapped pass caught it (ESCALATE). The mechanism: when the brief is a numbered list, the report must be the same numbered list with statuses — completeness becomes checkable by *diffing two lists* instead of trusting a reader to notice an absence (P1: structural enforcement over vigilance).

## Design implications

- **Dispatcher side:** multi-ruling briefs end with an explicit requirement — "your report must enumerate all rulings 1–N with per-ruling status." A silent drop then becomes a visible hole in a mandatory list rather than an absence a reader must notice.
- **Reviewer side:** verify the enumeration exists and every status is one of the four legal values; a missing enumeration is itself REBOUND-able.
- **The four statuses:** built / deferred-with-disclosure (+where the disclosure lives) / unchanged-from-last-increment / infeasible-stopped.

## Open questions

- Does this generalize past *rulings* to any enumerated brief content (files-to-touch, test-cases-to-add), or is the ruling case special because rulings carry ratified authority? Leaning general, but only the ruling case has a recurrence.
- At what recurrence does this promote from brief-boilerplate into a directive or a reviewer-persona standing check (like the shape-vs-substance adversarial-review candidate)?

## Cross-references

- [`multi-ruling-briefs`] is one of a family of "the report structure didn't force the gap into view" lessons — see [`2026-06-26-ships-green-while-incomplete.md`](2026-06-26-ships-green-while-incomplete.md) (the required test was absent) and [`2026-07-07-shape-vs-substance-guards-match-text-not-meaning.md`](2026-07-07-shape-vs-substance-guards-match-text-not-meaning.md) (adversarial review as the catch of last resort).
- Directives: P1 (structural enforcement), P3/P4 (DECISIONS + traceability), D7 (reviewer enforcement).
