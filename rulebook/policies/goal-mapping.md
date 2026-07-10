# Goal-mapping — the unmapped-work rule

**Status:** Adopted 2026-06-11 by Orchestrator (HO) — autonomous under the operator msg 3733 (principles-as-ratification; below-goal-level policy adopted under P/D justification + ledger, no ratify-ask). Originally drafted per the operator msg 3737/3738 ("many things may not have a defined goal level. I think that might be our gap in structure policy-wise"); in normative force since. Operationalizes the amended D3 bar's goal-level test.
**Author:** Orchestrator (Orchestrator)
**Re-evaluate when:** the goals registry (`rulebook/goals.md`) changes shape; OR a mapping dispute with the operator shows the reversibility test miscalibrated; OR ≥2 incidents where work proceeded under a proposed mapping he then rejected.
**Justifies via:** D3 (amended 2026-06-11 — bar at goal-level), D1 (digest surface, not asks), P4 (traceability), P6 (the ambiguity is a real tension; this preserves it as a named mechanism instead of collapsing to always-ask or always-adopt).

## The rule

Every piece of inbound work (ticket, idea, discovered need, dispatch request) gets a **goal mapping**: which `rulebook/goals.md` entry does it serve?

1. **Maps cleanly** → below the bar. Adopt under P/D justification, ledger as always. (This is the overwhelming majority — "the projects themselves are the goals," msg 3733; "get these projects done" covers all bolstering work, msg 3738.)
2. **Maps to no registry entry** → the work is **goal-shaped by definition**. HO does NOT silently adopt (that would be implicit goal-setting — the operator's tier). Instead:
   - **Reversible work:** HO drafts a proposed mapping (either "serves existing goal X because…" or a new DRAFT registry entry), **proceeds under the proposal**, and flags the mapping prominently in the operator's next digest for bless/correct. Idle-while-waiting loses; act-with-adjustment wins (msg_1118 lineage).
   - **Irreversible / outward-facing / spend-bearing work:** the proposed mapping goes to the operator as a genuine goal-level ask and the work **waits**. These were already above the bar; an unmapped goal makes them doubly so.
3. **Mapping disputes are cheap by design:** The operator corrects a mapping at digest-read time; HO re-files the work under the corrected goal (or kills it) and notes the correction in the registry's phase lines. No ceremony.

## What this is NOT

- Not a re-introduction of ratify-asks: a proposed mapping inside a digest is information with a default, not a blocking question.
- Not a tax on routine work: anything that obviously bolsters a registered project (bugfix, infra, process, research for it) maps in one clause of the ledger entry's justifies-via — no separate artifact.
- Not HO authoring north stars: registry **North star** lines stay the operator's voice; HO drafts them only as clearly-marked DRAFT placeholders.

## Worked examples (from the week this was written)

- *T-1068 server-grant write path* → maps to echo-prismatic (tool-policy surface of the voice interface). Clean case 1; no one needed to think about it.
- *ORG-032 orchestration shape* → maps to pantheon-org-system (org shape below goal-level) **with one foot above the bar**: its STATE-2 trigger is revenue-project's un-defer, which stays the operator's. Mixed items split exactly this way — mechanism adopted, trigger reserved.
- *A hypothetical "start a blog about the org"* → maps to nothing → goal-shaped → new DRAFT registry entry + it's outward-facing, so case 2b: waits for the operator.
