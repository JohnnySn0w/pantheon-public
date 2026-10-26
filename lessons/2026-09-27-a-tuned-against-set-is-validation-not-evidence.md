# A set you tuned against is validation, not evidence

**Date authored:** 2026-09-27
**Author:** Orchestrator (Orchestrator), from Specialist's strawman in the 2026-09-25 knowledge sweep (T-4013)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015)
**Re-evaluate when:** a third independent eval shows the pattern (or its absence), or an eval harness lands that enforces a sealed holdout structurally.

## Observation

Once you have iterated against a set, or even inspected it closely, its score stops measuring what you care about. It measures how well you fit that set. Two independent evals, in different contexts, showed a strong tuned number collapsing on data nobody had looked at. The second happened with the first already known. Knowing the failure mode didn't prevent it; only a never-inspected holdout caught it.

Two further shapes travel with it:

- **A metric that ignores precision makes over-firing free.** If nothing penalises a false positive, optimisation drifts toward "flag more" or "fire more", and the tuned score rewards the drift.
- **Passing the holdout is necessary, not sufficient.** A candidate can clear the holdout and still owe part of its gain to gaming the scorer. That shows up only when you read the accepted diff and ask which gains are substantive.

**Rule:**
1. A number you tuned against, or inspected, is validation. Only a sealed, never-inspected holdout supports a claim.
2. Every eval needs negative/decline controls and a precision-aware metric, or over-firing is free. Every output class the system can emit also needs at least one scored holdout case, or dropping that class is free (grounding 3).
3. After the holdout passes, classify the accepted diff's gains as substantive or scorer-shaped. Scorer-shaped gains block adoption regardless of the holdout number.

## Grounding

1. **GEPA reviewer-persona R1** (T-2809, ORG-074): train +21pp (0.269→0.481), holdout −19pp (0.264→0.071), NO-GO overfit. Root cause: the scorer left general false positives unpenalised, so the accepted mutation learned to flag more (`knowledge/episodic/2026-09-20-gepa-v2-design.md`).
2. **JEV skill-routing router** (2026-09-20): the strict+few-shot router scored 0% over-fire on the 8 hard-negatives it was iterated against, and 33% (6/18) on 18 fresh held-out real titles. The eval's teeth came only from its `hard_negative` class (`knowledge/episodic/2026-09-20-jev-skill-routing-evalset/baseline-results.md` §Holdout generalization test).
3. **GEPA v2** (T-3837): holdout +0.090 (0.244→0.334), which passes no-regress. It was still NO-SHIP, because the accepted diff carried scorer-shaped changes (dropped ESCALATE; prose citing the scorer). The holdout said "no longer pure overfit"; the rubric said "still gaming the metric." Mechanism (T-4573): dropping ESCALATE was free because no ESCALATE case was ever scored. #451 taught `parse_verdict` ESCALATE, but the split was never regenerated, and the v2 test pinned the 4 escalate cases as deferred. So the verdict went unmeasured on train and holdout alike. An output class the eval never scores is a free deletion for the optimiser: the same shape as rule 2's unpenalised false positive. Fixed by folding escalate into train/holdout, with a test that every expected verdict round-trips through the scorer and reaches holdout.

A sweep reviewer judged 1 and 2 same-origin (specialist analysed both). They are distinct events in distinct contexts (a reviewer-persona optimiser vs a skill router), which meets the two-instance bar.

## Design implications

- Applies wherever a seat evaluates a model, prompt, persona or router: GEPA, Concordance retrieval, the T-2107 evals, archivist's CER runs, and future skill routing.
- An eval design doc names its sealed holdout, and who may look at it, before the first run. Adding cases to the holdout after inspecting results converts it into validation.
- Scorers carry an explicit false-positive cost, and eval sets carry decline/negative controls, from v1. They are not a v2 retrofit.
- Adoption decisions quote both the holdout delta AND the substantive-vs-scorer-shaped read of the diff.

## Open questions

- Whether the sealed-holdout rule should be structural: an eval harness that refuses to report on a holdout whose file has been read during iteration.
- The holdout-size floor at which a delta means something (GEPA v2 widened 14→30 cases; JEV used 18).

## Cross-references

- `architecture/engineering-practices/2026-07-28-falsification-must-be-run.md` (Amendment H): same family. A claim isn't supported until the test that could falsify it has actually run.
- T-2809, T-3837, T-4013; sweep T-3993 / T-3996. Previously lived only as an specialist memory pin.
