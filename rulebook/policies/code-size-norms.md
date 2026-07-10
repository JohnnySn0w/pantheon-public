# Code size norms — soft attention-window targets

**Status:** **Ratified 2026-06-04 by the operator.** PROPOSED 2026-06-02 (T-0723, spawned from noodle T-0646 / ORG-024).
**Author:** Orchestrator (Orchestrator).
**Re-evaluate when:** LLM edit-fidelity problems start clustering around specific large files; the model's reliable-attention window materially changes; ORG-024 (project-is-the-eval) is revisited.
**Justifies via:** P1 (code structured for reliable LLM editing is structural, not "remember to be careful"), D7 (reviewer scope), ORG-024 (the project is the eval).

## Context

The external orchestration dump (`notes/2026-05-28-orchestration-systems-external-dump.md`) prescribes ~500 LoC/file and ~50–75 LoC/function as the window where an LLM holds local context in reliable attention before editing fidelity drops. Pantheon routinely exceeds both (`inbox_consumer.py` ~2300, `ticket.py` ~1815, `context_fill_monitor.py`/`activity_monitor.py` ~1100). T-0646 asked: adopt as norms, or reject as not net-positive for our shape?

## Decision

Adopt ~500 LoC/file and ~50–75 LoC/function as **soft, advisory targets** — NOT hard limits.

**Why soft, not hard:**
- *Real merit:* code that fits the attention window is edited more accurately (fewer missed call-sites, better local reasoning). Evidence from the 2026-06-02 resilience arc: the E2/E3 implementers worked cleanly in cohesive ~670–775 LoC modules, while navigating `ticket.py` (~1815) to reason about the cascade behavior took deliberate, careful reading — and I still mis-read it once from the symptom before reading the source.
- *Why not hard:* a hard cap forces churny splits with no functional win and adds cross-file traversal cost (D10 tension). Large single-file tools have served us. Cohesion beats the number — a cohesive 700-line module is better than two artificially-split 350s.

## Policy

1. **New code SHOULD aim for the window** (~500 LoC/file, ~50–75 LoC/function). Cohesion is the tie-breaker, not the raw count.
2. **Reviewer FLAGS, does not REBOUND**, on size: a file ≳1000 LoC or a function ≳150 LoC (≈2× the norm) is noted in the review report as a refactor candidate. Advisory only — never a blocking gate on its own.
3. **Existing over-size files are NOT mass-refactored.** Split opportunistically when a change already substantially touches the file. No standalone "split the big files" tickets unless an over-size file is *actively* causing edit-fidelity problems.
4. Dispatch briefs may note the norm for net-new modules; not required.

## Boundary

Soft norms, not gates (ORG-024: the project is the eval). If an over-size file isn't causing problems, leave it. This policy adds zero blocking surface — it only shapes new code and gives the reviewer an advisory flag.
