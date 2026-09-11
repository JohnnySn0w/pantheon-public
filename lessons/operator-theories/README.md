# the operator's Theories

Tracking the operator's articulated intuitions vs empirical validation; scoring over time as an operator-calibration mechanism. Per T-0163 ratification (the operator msg_830: *"You can stick that in /operator's theories so we can keep score for how often my intuitions are correct lol"*).

This is a sub-folder of [`lessons/`](../README.md) because theories are a kind of meta-lesson about the operator's intuition calibration — distinct from operational lessons (which abstract patterns from many cases) and from one-off intuitions (which become specific tickets). A theory earns a file when (a) the operator articulates it explicitly enough to evaluate, and (b) there's a path to empirical validation (eventually).

The score-over-time aggregate is itself a calibration signal: it tells future-operator (and future-Orchestrator) how much weight to put on an operator-articulated intuition that hasn't yet been validated. High-calibration → trust the next intuition more autonomously; low-calibration → flag for empirical check more aggressively.

## Per-theory file shape

```markdown
# <Theory name — distinctive one-line>

**Date articulated:** YYYY-MM-DD
**Source:** Telegram msg_NNNN (verbatim quote in §Theory framing)
**Verdict:** Correct / Directionally-correct-magnitude-off / Partial / Wrong / Unresolved
**Score:** N (0-100; calibration estimate)
**Ratification status:** Drafted / operator-ratified at update time / Superseded by new evidence

## Theory framing

The intuition itself, 1-2 sentences. Verbatim quote from Telegram if applicable.

## Empirical question

What observation would validate or falsify the theory? State concrete enough that a research dispatch or real-world incident can answer it.

## Empirical resolution

The grounding. Cite research dispatches, ledger entries, real-world incidents,
external sources. Theories without empirical resolution stay Unresolved.

## Verdict reasoning

Why this verdict + score. Concretely: where the intuition was correct, where it
was off, and by how much.

## Cross-references

- Lessons / principles / ADRs / tickets that touch.
- Telegram refs.
- Related theories from this corpus.
```

## Maintenance discipline

- HO authors per-theory file when an intuition lands + has an evaluable shape.
- HO updates Empirical resolution + Verdict + Score when validating data arrives.
- Per-entry the operator ratification at update time (or batch-ratify in periodic theory-corpus reviews).
- Score-over-time aggregate computed periodically (manually or via a tools/-script if the corpus grows enough to warrant).

## Index

| Date | Theory | Verdict | Score |
|---|---|---|---|
| 2026-05-09 | [1M context not as useful as it would seem — degrades halfway in](2026-05-09-1m-context-halfway-hypothesis.md) | Directionally-correct-magnitude-off | ~80 |
| 2026-08-26 | [Intellectual arbitrage — LLM-native tool-arbitrage across disciplinary boundaries](2026-08-26-intuition-anchor-plus-giants-methodology.md) | Unresolved (first scored application in-flight: germination-arc layer-methodology corpus, post-POC) | pending |

## Future seed candidates (per T-0163 §4)

When these theories surface in HO's next inflight thread + earn evaluable shapes, file them as their own entries:

- **Bridge-as-structural-failure intuition** (msg around 2026-05-05) — the operator's read that the pantheon-bridge architecture was a structural mismatch with kino's eventual shape; informed the bridge-to-kino pivot. Empirically validated post-pivot. Score candidate: high.
- **Defer-with-ping-date pattern** (msg_801, T-0041) — the operator's intuition that scheduled-ping deferrals would beat naive backlog accumulation for operator bandwidth. Operationally validated. Score candidate: high.
- **"Specialist's messages are sufficient as read-only / shared bot" intuition** (msg_784/785, T-0145) — the operator's call that Specialist didn't need an interactive Telegram surface, scope-tightening T-0102 Phase 2 mid-implementation. Clean operationally; verdict: correct. Score candidate: high.

(More candidates surface as the operator articulates intuitions in future threads. The corpus is open-ended; the discipline is to file when articulation is concrete enough to evaluate.)

## Related

- [`lessons/`](../README.md) — parent folder; theories are a kind of meta-lesson.
- `rulebook/directives.md` D8 (HO self-discipline) — sticky-belief-at-N=2 rule is the inverse signal: when HO's belief stays sticky despite REBOUND, that's a cue to redispatch fresh; theories tracking is the corresponding outward-looking operator-calibration signal.
- T-0158 (lessons folder framework), T-0163 (this folder's filing ticket).
