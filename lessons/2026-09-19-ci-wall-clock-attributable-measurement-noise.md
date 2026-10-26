# CI wall-clock is too noisy for single-datapoint attributable-measurement

Filed 2026-09-19 (orchestrator) after artisan's T-3629 REBOUND arc closed. Artisan surfaced the discipline gap via mailbox 01789788809027474228 upon receiving the post-merge baseline-confirm datapoint; captured here as a Pantheon lesson so it generalizes beyond artisan's own persona memory pin.

## The pattern

Echo CI-timing arcs (T-3627 nextest, T-3629 sccache) followed a 3-ticket attributable-measurement split: each falsifier lever gets its own PR + ticket + measurement window, so a mixed-signal arc doesn't hide which lever is helping vs hurting. Correct in spirit.

The failure mode: **a single wall-clock number in a single CI run was treated as decisive evidence for a regression claim**. On T-3629, n1 measured 867s at 99.9% sccache hits vs a 828s no-sccache baseline — cited as "+4.7% regression." Post-merge no-sccache baseline-confirm hit 536s — meaning the no-sccache test-job spans 536-828s, a ~55% noise band. The +4.7% delta was inside the noise, not the clean steady-state signal it was framed as.

The rebound STILL stood, but on **variance-independent grounds** that should have led:
- **No positive case**: sccache's 867s was the slowest of all four observed runs; it never once beat no-sccache.
- **Structural mechanism**: CARGO_INCREMENTAL=0 (auto-set by sccache) defeats cargo's incremental-skip on Swatinem-warm runners; sccache fetch-per-unit costs more than incremental-skip even at 99.9% hits. This is a mechanism claim, not a wall-clock measurement.
- **Covered niche**: cache-cold case doubly covered by Swatinem + T-3604 shared CARGO_TARGET_DIR; sccache adds no unique capability.
- **Complexity-for-nothing**: keeping ci.yml wiring + runner install with zero demonstrated benefit is standing tax.

The correct rebound argument doesn't need the wall-clock number at all. Leading with "+4.7%" made it look wall-clock-driven; the real basis is the mechanism + no-positive-case + covered-niche.

## The rule

For any GitHub-Actions-runner-hosted CI-timing arc:

- **n=1 wall-clock is too noisy for a regression verdict.** For a real speedup/regression claim, need n≥3/condition (both arms) OR a computed confidence interval that clears the observed spread.
- **When arguing for a lever change, lead with variance-independent evidence when it exists.** Mechanism claims (why the change works or doesn't), no-positive-case observations, coverage-gap analysis, and complexity-cost accounting are all variance-independent. Wall-clock numbers are supporting evidence, not headline evidence, until n≥3.
- **48h-quiet contingency doesn't excuse single-datapoint framing.** If organic n≥3 isn't going to arrive, the honest close is "n1 + mechanism analysis + no-positive-case" — not "n1 is decisive." The T-3629 rebound was still right; the framing of the evidence was overconfident.

## The trigger

Any future echo CI-timing work (or any Pantheon CI-timing measurement — Recovery's own repos, sseed-presence, echo-vault, others):

- Before citing a specific % delta as decisive, check: what's the noise band? A single datapoint against a single baseline is not a delta; it's a difference between two points that may both be inside a wider distribution.
- Before framing a rebound around a wall-clock number, ask: is there a mechanism argument that's variance-independent? If yes, LEAD with it; the wall-clock number is confirmation, not verdict.

## When it's fine to ship n=1

- **Directional signal + mechanism argument**: n=1 is enough to confirm mechanism direction is not wildly wrong. But not enough to attribute a specific percentage.
- **Change-of-shape verdicts**: "test job used to fail, now passes" is n=1 sufficient (it's a categorical change, not a wall-clock delta).
- **Rebound on non-timing grounds**: complexity-cost / coverage-duplication / no-positive-case rebounds don't need timing evidence at all; n=0 wall-clock is fine.

## What NOT to do

- Cite n=1 as "measured" instead of "observed." Measurement implies noise-band-characterization; observation doesn't.
- Frame a rebound as timing-driven when it's actually mechanism-driven. Downstream readers will interpret the wall-clock number as the decision basis and inherit the framing.
- Skip the noise-band check just because the arc has a deadline (48h-quiet contingency, cadence pressure). If the honest answer is "n1 inside noise, but rebound stands on mechanism," say that.

## Related

- `lessons/2026-09-16-reviewer-live-repro-when-code-has-real-side-effects.md` — sibling review-discipline lesson from the same arc-close window.
- `lessons/2026-09-16-reviewer-pre-review-co-spec.md` — sibling review-discipline lesson.
- T-3629 ticket resolution + post-close addendum (artisan correction) carries the specific arc history.
- Ledger `2026-09-19T03:31:00Z-t3629-rebound-arc-closure` documents the arc-level outcome.
