---
symptom: "A non-vacuity test that fetches its 'old behavior' reference from a moving target (origin/main, a live branch, the installed copy) goes RED the moment its own change merges — the reference BECOMES the new behavior, the test compares the fix to itself, and main is red-corrupted by the very tests that proved the fix."
fix: "Embed the old-behavior reference STABLY: a frozen fixture (the pre-fix file committed into the test's own fixtures) or skip-when-superseded (detect the fix's markers in the fetched reference and skip-with-reason pointing at the pre-merge proof in the PR/ticket record). Never fetch a moving target as the baseline of a differential assertion. The non-vacuity proof only needs to RUN once, pre-merge — its job afterward is to be findable in the record, not to re-run against a reference that no longer exists."
domain: pantheon, tests, non-vacuity, differential, moving-target, temporal
---

# Non-vacuity references fetched from a moving target self-destruct at merge

## Mechanism

Non-vacuity discipline says: prove the guard fails against the PRE-fix code. The natural
implementation — fetch `origin/main:<file>` as "the pre-fix version" — carries a hidden temporal
assumption: that origin/main predates the fix. That assumption is destroyed BY THE MERGE ITSELF.
Post-merge, the fetched reference IS the fixed code, the differential compares the fix to itself,
and the assertion fails — red on main, introduced by the tests whose purpose was proving rigor.

## Why both the author and the reviewer miss it

The tests are green at authoring time and green at review time BY CONSTRUCTION — origin/main is
still pre-fix during both. The red only manifests after the merge lands, i.e. after every check
that normally catches a bad change has already passed. The only catch points are (a) noticing the
temporal assumption while writing, or (b) re-verifying on the MERGED tree before declaring the
arc closed — which is how the live instance was caught (T-1961 follow-up, 2026-07-23: Mason ran
the suite on merged main as her resolve-time green-check instead of assuming; 2F; fix was
skip-when-superseded, PR #108).

## The rule

A differential test's baseline must be TEMPORALLY STABLE. Acceptable shapes:
- **Frozen fixture** — commit the pre-fix file into the test fixtures (best when small).
- **Skip-when-superseded** — fetch, detect the fix's own markers, skip-with-reason citing where
  the pre-merge proof lives (best when the pre-fix file is large or the proof only matters once).
- **Synthetic reconstruction** — build the old behavior in the test (a fixture repo, a stub) —
  what every other test in the same suite did, which is why only the two fetching tests broke.

And the closing habit that catches the whole class: **the arc's green-check runs on the merged
tree, not the branch** — "resolved + green" must be true, not asserted.

## Cross-refs

- `lessons/2026-07-22-false-red-verifier-must-call-prod-parser.md` — sibling false-red class
  (there: the verifier's copy drifts from prod; here: the verifier's baseline drifts INTO prod).
- `tickets/inbox/T-1961.md` + PR #107/#108 — the live instance and fix provenance.
