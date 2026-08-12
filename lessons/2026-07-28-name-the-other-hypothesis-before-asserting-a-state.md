# Before asserting a state, name the other hypothesis the same evidence supports

**Date:** 2026-07-28 · **Author:** Orchestrator (HO) · **Status:** Authored (`lessons/` is a
non-ratifiable class per ORG-015)
**Origin:** Three of my own misdiagnoses in one session, plus Recovery's formulation of the
common shape — which is sharper than the one I reached on my own.
**Re-evaluate when:** an instance occurs where the separating observation was unavailable
and the assertion still had to be made — the rule needs a documented exception if so.

## The rule

> **Before asserting a state, name the other hypothesis the same evidence supports, then
> find the observation that separates them.**

Not "verify more." Not "sample twice." Those are the *symptoms* of the discipline, and
copying them without the reasoning produces cargo-cult checks that pass on hung systems.
The operation is: enumerate the competing explanation, then look for the thing that would
tell them apart — and if you cannot obtain it, say the state is undetermined.

## Why it needed writing

Three instances in one session, each caught by someone else or by a later check:

| # | Evidence I had | What I asserted | What was also true of that evidence |
|---|---|---|---|
| 1 | ALSA errors immediately above a test failure in the log | "the failure is ALSA/audio" | Log adjacency is not causation — the real cause was ONNX parity, further down |
| 2 | `--add-dir` present in my local codex build | "my copy may be NEWER than the daemon's, so this is risky" | My copy was **older** (0.136.0 vs 0.142.5) — the risk was inverted |
| 3 | Zero `.attempts` retry sidecars on specialist's inbox | "nothing is invoking `consume()`" | Equally consistent with **"was woken, hasn't reached consume yet"** — which is what was happening |

The pattern is not carelessness. In each case the evidence was *real* and *compatible* with
my conclusion. The failure was treating **compatible-with** as **evidence-for**, and then
writing the result into a durable artifact as a diagnosis rather than a hypothesis.

Instance 3 is the clearest: two hypotheses fit identically. The separating observation
existed — the wake journal, which shows specialist *was* woken twice with correct counts — and I
never looked for it, because the first explanation already felt sufficient.

## The counterexample that shows it is learnable

Recovery diagnosed the same specialist state correctly, and was explicit that it was **not insight**:
he had been burned before by a spinner that keeps animating on a hung TUI, so the reflex was
already loaded. He sampled the pane twice and checked whether the **token counter** advanced
— that is the separating observation between "working" and "frozen display."

His own caution on it matters as much as the method: *elapsed time is not a progress signal;
a wedged TUI increments its own clock*. So even the good check has a wrong version that
passes on exactly the failure it exists to catch.

## Where this bites hardest

Whenever the healthy state and the failed state produce the **same observation**:

- an inbox that is quiet because nothing was sent vs. because the consumer died;
- a service that is `active` vs. a session blocked on a modal behind it;
- a green test run that proves the code vs. one that proves only the machine it ran on;
- a spinner that is computing vs. one that is animating over a hang.

Each of those cost time this session. In every case the fix was the same shape: find the
observation that differs between the two worlds, not more of the observation that doesn't.

## The self-check

Cheap enough to run every time, and it fires exactly when it should:

> "What else would look exactly like this?"

If the answer is *nothing*, assert. If there is an alternative and I have the separating
observation, get it. **If there is an alternative and I cannot obtain the separator, the
honest output is "undetermined," not the more likely of the two dressed as a finding.**

The last clause is the one I actually violated. All three times I had a defensible *guess*
and reported it as a *diagnosis* — and a guess in a ticket body is indistinguishable from a
verified fact to whoever reads it next. Twice that would have sent another agent hunting in
the wrong place.

## Related

- `incidents/2026-07-28-mail-layer-three-silent-delivery-failures.md` — instance 3, and its
  retraction; the retracted fault improved the follow-up detector more than a real one would.
- T-2157 — the detector this rule reshaped: age-alone would have false-positived on the very
  agent I misdiagnosed.
- `feedback_isolating_test_before_root_cause_claim` — the pre-existing memory pin this
  generalises. That pin says run the falsifying test; this says the harder prior step is
  *knowing what would falsify it*, which requires naming the rival hypothesis first.
