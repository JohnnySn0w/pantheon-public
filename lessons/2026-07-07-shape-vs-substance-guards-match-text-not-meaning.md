# Shape-vs-substance — a guard that matches text, not the operation it's guarding

**Date authored:** 2026-07-07
**Author:** Orchestrator (Orchestrator)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015; substantial ones get Telegram surfacing — this one's design-implication #2 (adversarial review on guard relaxations) is a candidate reviewer-persona amendment, flagged for the next rulebook touch)
**Re-evaluate when:** a fourth instance surfaces (promote toward a directive amendment on guard/lint design), OR a guard rewrite in this class ships without an adversarial pre-flight (the defense below failed to propagate).

## Observation

A guard, lint, or classifier that decides on the **textual shape** of its input rather than the **substance of the operation** fails in both directions at once, and the two failures look opposite so they're easy to treat as unrelated:

- **False positives** — it blocks/flags inputs that merely *mention* a protected token but perform no protected operation (a read-only `grep` of a ledger path; a commit whose *message* names a canonical doc; a prose restatement of a ratification that isn't itself a ratification).
- **False negatives** — it misses inputs that perform the protected operation without the trigger text on the surface it inspects (a write routed through `find -fprintf`; `git diff --output=<path>`; a runtime code path the scanner never reads).

The tell is the same every time: **the check's match surface is not the operation's effect surface.** Text-matching a command string is not the same as reasoning about what the command *writes*; anchor-presence in a document is not the same as a ratification *event*; a filename in `git status` is not the same as the byte sequence in `git log`.

## Grounding

Three instances in a single session (2026-07-06/07), each a distinct surface of the same root:

- **T-1589 — authority-claim corroboration lint.** An early cut flagged prose *restatements* of a ratification (shape: the words "Ratified by the operator" appear) as if they were uncorroborated authority *claims* (substance: a ratification event with a msg-id/ticket anchor). Fixed by an anchor-presence gate — the lint now keys on the anchored-marker substance, not the restatement text. (Ledger `…-ho-t1589-landed`.)
- **T-1600 filing — the worker-guard's own misfires.** The guard blocked read-only Bash commands that *mentioned* a protected path and clean-scope commits whose *message* mentioned one (false positives from text-matching the whole command string), while its text-match never caught a quote-split evasion (false negative). The fix: match the reconstructed *argument surface* (shlex tokenize → rejoin) with a read-only-verb allowlist and commit-message stripping. (Ledger `…-ho-t1600-landed`.)
- **T-1600 fix — the fix nearly re-committed the same error, inverted.** The relaxed matcher classified `git log/show/diff` and `find` as read-only on the *verb/subcommand shape* alone, missing the write-diverting *flags* (`--output=<path>`, `-fprintf`) — a fresh false-negative write primitive. Caught only because the review was explicitly adversarial (a security-surface relaxation): round-1 REBOUND with two live-verified write commands the new code allowed and the old code blocked. (Reviewer report, T-1600 round 1.)

Adjacent older instance for cross-reference: the `HERMES\.md` byte-sequence discipline (`~/CLAUDE.md`) is the same class handled well — the trigger is defined on the *exact surface that bills* (git-log/commit-message content, case-sensitive contiguous bytes), NOT on bare filenames, precisely because the effect surface and the match surface were pinned to each other after primary-source verification.

## Design implications

1. **When designing a guard/lint/classifier, name its effect surface first, then make the match surface equal to it.** "What operation am I actually trying to prevent, and where does that operation's effect show up?" — then match *there*. Matching a convenient proxy (the whole command string, a filename, document prose) is the defect in embryo.
2. **A guard *relaxation* is a security change and gets an adversarial review, not a correctness review.** The reviewer's job on a loosened guard is to *construct* the input the new code allows that the old code blocked and that actually performs the protected operation — not to confirm the happy paths. T-1600's two holes were invisible to a non-adversarial read; the explicit "construct a write the new code permits" brief found them in one pass. This is a candidate reviewer-persona standing check for any diff that widens an allowlist or narrows a guard.
3. **Allowlist-by-verb is a false-negative trap wherever the verb accepts an output-diverting flag.** `git log --output`, `find -fprintf`, and their kin mean "read-only verb" is not a safe classification on the head token alone. Any read-only-verb allowlist needs an "…and no output-diverting flag" clause, and the failure class deserves an inline warning at the allowlist definition so the next extender hits it before adding a verb (T-1600 added exactly this comment).
4. **Fail-closed on ambiguity is the correct default for this class** — when the match surface can't be cleanly reconstructed (compound shell, redirection, unparseable quoting), fall back to the broad text match. Precision is a false-positive reduction; it must never come at a false-negative cost, because the guard's threat model is (at minimum) confusion and (at most) evasion.

## Open questions

- Is this rule-shaped enough to become a directive (a standing "guards match effect-surface, and relaxations get adversarial review"), or does it stay a lesson that informs reviewer-persona checks and future guard designs? Leaning lesson until a fourth instance shows the design implication hardening.
- Does the adversarial-review-on-relaxation check generalize cleanly to non-security allowlist widenings (e.g. loosening a validation), or is the "construct the bad input the new code now permits" framing specific to guards with a clear protected-operation? The T-1589 lint had no adversarial round and didn't need one — the asymmetry is that a *false negative* on a security guard is a live hole, while a false negative on a warn-only lint is just a missed warning. The trigger for "this needs adversarial review" may be *false-negative severity*, not the shape-vs-substance pattern itself.
