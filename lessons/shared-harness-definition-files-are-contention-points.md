# Shared harness/definition files are contention points in a feature wave

**Date authored:** 2026-07-06 (distilled from the T-1524/25/27/30 echo harness wave)
**Author:** Orchestrator (Orchestrator)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015; adopted as dispatch practice)
**Re-evaluate when:** a shared-file collision actually lands broken (this wave only near-missed via serialized review), OR a structural primitive removes the contention (e.g. a scenario-registry that composes append-only fragments so no file is co-edited).

## Observation

A feature wave that instruments N features against one harness makes the harness's central definitions file the single file every ticket touches. Four parallel/adjacent echo tickets (T-1524, T-1525, T-1527, T-1530) all needed to edit the same `tools/audio-harness/scenarios/definitions.py`; one implementer correctly refused its harness ruling mid-build because a sibling had the file in flight, costing a clarification round-trip (2026-07-06). Code collision was avoided this wave *only* because lands serialized through review — the near-miss was an implementer stopping rather than guessing whether it could touch the contended file.

## Grounding

What already merges cleanly, observed this wave:
- Per-ticket CI-tier test files (the `test_ddsd_case_definitions.py` → `test_recent_actions_…` → `test_duck_then_decide_…` pattern) never collide — each ticket owns its own file.
- Scenario *additions* are naturally append-only blocks; merges stay clean as long as workers don't reformat surrounding code.

The contention is concentrated in the *shared structure* edits (the central definitions dict/registry), not in the per-feature additions.

## Design implications

- **Name the contention at DISPATCH time.** Every brief that touches a shared harness file says who else is touching it and in what order they land ("T-XXXX also edits this file and lands first/after you").
- **Prefer append-only over shared-structure edits.** Self-contained scenario blocks + per-ticket CI-tier test FILES (`test_<feature>_case_definitions.py`) over editing shared structures — append-only edits merge cleanly.
- **Sequence when order matters.** Route the shared-structure edit into the LAST-landing ticket or a follow-up commit after the collision clears; when a wave is planned up front, assign the shared-file edit landing order explicitly.

## Open questions

- Is the durable fix a scenario-registry that lets each feature register an append-only fragment (no co-edited file at all), retiring the dispatch-time coordination? That would move this from a discipline to a structural guarantee (P1).
- Does this generalize to any repo's "one central config/registry that a feature wave all touches," or is it specific to test-harness definition files? The shape looks general.

## Cross-references

- Sibling coordination lesson: [`2026-06-11-freeze-at-ratification-surface.md`](2026-06-11-freeze-at-ratification-surface.md) (parallel-arc HEAD-desync — same class of "N parallel workers, one shared surface").
- Directives: P1 (a registry would be the structural fix), D7 (dispatch-time scope-split + landing-order coordination).
