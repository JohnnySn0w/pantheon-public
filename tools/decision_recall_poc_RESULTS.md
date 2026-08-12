# T-2816 — Decision-recall PoC: results

**Question:** does surfacing the RELEVANT past ADR at point-of-need — without the agent
knowing to look for it — clear the bar the Dei recall-hook design
(`notes/dei-foundational-wip/recall-hook.md`) set when it DECLINED broad fuzzy recall as
bloat-positive? This is an offline retrieval-quality measurement, not a live hook.

**Substrate:** hand-rolled BM25 (no new dependency; `rank_bm25`/`sentence_transformers` are
both absent from this repo) over a candidate pool = 18 hand-authored trigger-sidecar ADRs
(`tools/decision_recall_triggers.json`) UNION a recency-window (5 most-recently-dated ADRs,
title-only fallback for entries the sidecar hasn't caught up to). BM25 params (k1=1.5,
b=0.75) are textbook defaults, chosen a priori. Full mechanism in `tools/decision_recall_poc.py`.

**Eval set:** `tools/decision_recall_eval_set.json` — 35 hand-labeled point-of-need scenarios
(23 positive across the 18 sidecar ADRs, 12 negative split between easy-unrelated and
topically-adjacent-hard). Authored and labeled **before** any retriever tuning against this
file — the one pre-existing tuning change (a stopword-list fix, below) was driven by a
synthetic unit-test fixture, not by this file's content or these numbers.

## Caveat (read this before the numbers)

This is **one author's judgment calls on 35 scenarios — a rough first signal, not ground
truth.** Two specific risks this carries into the read below:

- **Positive scenarios are optimistic.** I wrote both the trigger phrases and the "paraphrased"
  positive scenarios, so they still naturally share domain vocabulary (I am not an independent
  labeler stress-testing genuine phrasing drift). Real point-of-need language from a live agent
  may share far less vocabulary with a trigger phrase than these scenarios do — hit-rate@k here
  is likely a ceiling, not a realistic estimate.
- **35 scenarios is too small to lock an operational threshold on** (see the threshold-sensitivity
  appendix below) — any specific `min_score` picked from this set risks re-fitting to exactly
  the words these 35 sentences happen to use. Treat the appendix as "a threshold gets you
  there in principle," not "here is the value to ship."

## Headline numbers (default gate: `min_score=0.0`, the loosest possible — any lexical overlap surfaces)

```
corpus: 73 ADRs loaded, 18 in trigger sidecar, recency_k=5, min_score=0.0
scenarios: 35 (positive=23, negative=12)
hit_rate@2:   100.0%
precision@2:   38.7%
false_trigger_rate: 66.7%
```

Every positive scenario retrieved its expected ADR in the top 2. But at the loosest possible
gate, **8 of 12 negative scenarios surfaced something anyway** — mostly on shared generic
domain vocabulary ("ticket", "service", "repo") that recurs across many ADR titles in a
candidate pool this small (IDF alone doesn't suppress a word that appears in 4-5 of ~20 docs
enough to zero it out).

## Reading this against the right baseline (per HO's framing)

`recall-feedback.py`'s exact-token approach is near-zero-false-trigger **by design** — it only
fires on a literal substring match (a file path, a `T-NNNN`/`ORG-NNN` id, a distinctive
2-3-word command signature). Decision-recall's whole premise is broader: catching the
*invisible-unknown* means matching on paraphrased intent, not literal tokens, which
structurally trades some false-triggers for coverage. So the bar is **"clean enough that the
coverage gain outweighs the noise cost,"** not **"as clean as exact-token."** The `min_score=0.0`
gate is also, deliberately, the loosest point on that tradeoff curve — see below for where
raising it lands.

## 3-way read, applied

Per HO's framing (cheap-go / go-needs-embeddings / no-go), against the proposed bar
(hit-rate@2 ≥ 70% AND false-trigger-rate ≤ 15%):

- **cheap-go?** No — at the design's own default gate, false-trigger-rate (66.7%) misses the
  bar badly, even though hit-rate (100%) clears it comfortably.
- **no-go?** No — the miss is not a coverage/relevance failure (hit-rate is perfect at the
  loosest gate) and it does not persist under a trivial fix, below.
- **go-needs-embeddings, with a nuance:** the finding is more specific than "add embeddings."
  A plain score threshold on the **same dependency-free BM25 substrate** — no new model, no
  new dependency — gets most of the way to the bar on its own. See the sweep:

| `min_score` | hit_rate@2 | precision@2 | false_trigger_rate |
|---|---|---|---|
| 0.0 (default) | 100.0% | 38.7% | 66.7% |
| 2.0 | 100.0% | 40.0% | 58.3% |
| 2.5 | 100.0% | 53.7% | 33.3% |
| 4.0 | 95.7% | 64.0% | 25.0% |
| 4.5 | 91.3% | 64.6% | 16.7% |
| **5.0** | **91.3%** | **68.8%** | **16.7%** |
| 6.0 | 91.3% | 68.8% | 16.7% |
| 7.0 | 73.9% | 72.5% | 8.3% |
| 8.0 | 60.9% | 73.5% | 8.3% |

Two usable operating points on this same substrate: `min_score≈5.0` lands just outside the
proposed false-trigger bar (16.7% vs ≤15%) while holding hit-rate at 91.3%; `min_score≈7.0`
clears the false-trigger bar comfortably (8.3%) at the cost of hit-rate dropping to right
around the 70% line. Neither number should be read as "the" threshold to ship (see Caveat) —
the finding is that **the lever that closes most of the gap is a threshold, not a smarter
retriever.** Hit-rate stays flat at 100% all the way through `min_score=2.5`, meaning none of
this eval set's positive matches are "weak" — every expected ADR is the top or co-top scorer
for its scenario, so raising the gate costs nothing until ~4.0.

**Overall read: go-needs-tuning** (a cheaper variant of go-needs-embeddings). The
dependency-free BM25+recency substrate is not disqualified by these numbers — the naive
score>0 gate this PoC deliberately shipped as its baseline is what's disqualified.
Recommended next step, if this arc continues: validate a threshold on a larger, ideally
independently-labeled eval set (closing this PoC's optimism risk above) before locking one in
for any real build; treat vector/embedding retrieval as the fallback only if that larger
validation shows a ceiling a threshold can't fix (e.g. genuine paraphrase-drift scenarios that
share no vocabulary with their ADR's trigger phrase at all — this eval set, being
single-author, likely under-tests exactly that case).

## What this does NOT tell us

- Nothing about real agent point-of-need phrasing (see Caveat) — only about phrasing I
  generated myself, aware of the trigger sidecar's wording.
- Nothing about the 55 ADRs outside the trigger sidecar (no eval scenario targets them,
  by design — the recency-window's title-only fallback is covered by the unit tests on a
  synthetic corpus, not by this real-corpus eval).
- Nothing about live-hook cost (token overhead of the `additionalContext` injection, latency,
  interaction with the existing `feedback`/`project` recall hook) — out of scope per the brief.

## Reproduce

```
python3 -m tools.decision_recall_poc                      # headline (min_score=0.0)
python3 -m tools.decision_recall_poc --min-score 5.0       # a tuned operating point
python3 -m tools.decision_recall_poc --query "..." --k 2   # ad-hoc single query
```
