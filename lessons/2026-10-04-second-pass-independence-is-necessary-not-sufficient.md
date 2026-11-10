# A second pass adds signal only if its errors are independent of the first, and independence is not enough

**Date authored:** 2026-10-04 (landed 2026-11-03, T-4586)
**Author:** Orchestrator (Orchestrator), from Specialist's draft (T-4571) grounded in three measured arcs
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015)
**Re-evaluate when:** a same-model, same-input second pass is measured to catch a meaningful share of first-pass errors on held-out data; OR the pre-registered confidence-gated gate is run on held-out data (T-4584, dropped 2026-10-18 for lack of held-out ground truth; reopen on its refile trigger); OR a second reader is found whose own error rate is low enough that raw disagreement clears a burden bar without gating.

## Observation

Pantheon keeps reaching for a "second pass" to catch what a first pass got wrong: a re-read of an OCR page, a re-check of a watch report's claim. Three measured arcs say the same two things.

**1. A second pass that shares the first pass's inputs and model reproduces its errors.** Rewording the prompt is not independence. In T-4291, the same Qwen2.5-VL re-reading the same render with a differently worded prompt reproduced pass-1's exact greedy tokens: it disagreed on 0 of 3 known-error pages. Greedy decoding on the same pixels is deterministic, so the "check" was a copy. In T-4248, the watch-report spot-check named the missing property the same way: independence, meaning a different agent, without the producer's reasoning, re-deriving the claim from a source the producer does not control. Run that way, it caught 1 genuinely over-broad claim in 3 recomputations.

**2. Independence buys recall, not precision. The second reader's own error rate sets the false-flag floor.** In T-4486, changing the input (native resolution instead of the 2.62×-downsampled render) made the second pass independent, and anchor recall went from 0/3 to 3/3. But ground truth scored its precision at 3/6, and on the full corpus it flagged 56.9% of pages, worse than the crude digit gate it was meant to replace (47.2%). A different model family (GLM-OCR) was more independent still and noisier: it flagged 77.2%. Two independent readers disagreeing at the same word only reached 45.5%. Every page where the second reader misreads a word the first got right becomes a flag. A disagreement gate is therefore only as good as the second reader's accuracy, and an independent reader is usually a different and often weaker one.

## What worked

The one gate that cut the burden used a signal that is about the first pass rather than about the second reader. It flagged a disagreement only where pass-1's own token confidence at the disagreeing word was low (T-4486 lever 3: 23.2% of pages at 3/3 anchor recall, versus 32.9% for pass-1 confidence alone). Real misreads sat on words pass-1 was unsure of, while the second reader's noise landed on words pass-1 read confidently. The threshold was read off the same seven anchors, so per `2026-09-27-a-tuned-against-set-is-validation-not-evidence.md` it is pre-registered for a held-out test (T-4584), not adopted. That test was dropped 2026-10-18 when no held-out ground truth materialised, so the gate remains unvalidated and SN_VERIFY stays off.

## How to apply

- Before building a verification pass, name what makes its errors independent of the thing it checks: a different input, a different model, or a reviewer without the producer's reasoning. "Different prompt, same model, same input" does not qualify.
- Measure the second reader's own error rate on cases the first pass got right, not just its catches. That rate, not its recall, decides the burden.
- Prefer gating a disagreement on the first pass's own uncertainty (token confidence, self-consistency) over raw disagreement. Validate any threshold on data it was not tuned on.
- A second pass is a disagreement signal for review, never a corrector. In T-4486 the native-resolution read was itself one token off ground truth on every flagged page.

## Grounding

- T-4233 / T-4291: same-model second pass, recall 0/3 (`tickets/done/T-4291.md`, `tickets/done/T-4233.md`).
- T-4486: levers 1-3 on the 274-page SuperNote corpus (`knowledge/episodic/2026-10-04-t4486-levers-2-3-production-flag-rate.md`, `knowledge/episodic/2026-10-04-t4486-independent-ocr-pass-resolution.md`).
- T-4248: independent recomputation for watch reports (`knowledge/episodic/2026-09-30-watch-independent-recomputation-eval.md`; pilot tracker T-4572).
- Related: `lessons/2026-09-27-a-tuned-against-set-is-validation-not-evidence.md` (why lever 3 is pre-registered, not adopted); `lessons/verification-tier-blind-spots-each-covers-what-the-previous-cant-see.md` (a different claim: integration-test tiers see different bug classes).
