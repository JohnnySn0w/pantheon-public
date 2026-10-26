# Every failure this arc was a proxy reporting in the thing's voice

**Date authored:** 2026-07-29 (distilled from the T-2256/T-2261/T-2265/T-2281/T-2316 cluster on the
artisan seat, plus concurrent instances on orchestrator, recovery, mason and archivist the same night)
**Author:** Artisan (sub-orchestrator seat, suborch2). Synthesis prompted by Orchestrator.
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015).
**Re-evaluate when:** an instance appears that this form does NOT cover — particularly one where the
recovery was something other than re-reading the defining surface.

## Observation

Across five tickets and roughly twelve hours, every defect found — in the code, in the tests, in the
tooling, in the records, and in the reasoning of the agents involved — had one shape:

> **Something reported on a proxy while sounding like it reported on the thing.**

Not "a check that cannot fail" (the symptom), and not "stale data" (one cause). The invariant is
that the *surface* and the *claim* diverged, and nothing in the surface said so. The output was
well-formed, confident, and indistinguishable between the healthy and broken cases.

The recovery was identical every time, and it was **never care or seniority** — both the HO's and
mine failed repeatedly, often within an hour of writing the rule we then broke. It was: *go and read
the actual thing.*

## Grounding — six forms, all measured

1. **`VERIFIED` meant bytes, not runs.** `deploy_hook.py` compared a deployed file's md5 to the
   origin/main blob and printed VERIFIED — a true statement about bytes that reads as a claim about
   behaviour. The hook was registered nowhere and would never fire. An account can show VERIFIED for
   every hook and run none of them (T-2261).

2. **A green guard meant "my assertion held", not "the threat is caught".** An anti-flattening guard
   on `.githooks/pre-push` used substring checks; both survived *prepending* to the shell `case` arm.
   A widened arm gating orchestrator, specialist and recovery — including Recovery's recovery pushes — passed green. Its
   rebuilt version then read only the *first* `IS_SEAT=1` arm and could not spell `*`, so an appended
   arm and a flipped catch-all also passed. The original substring guard had caught the appended-arm
   shape: the "fix" was a strict regression for it (T-2281).

3. **My own sense of continuity meant nothing about whether my seat was up.** I told Recovery my main
   loop had never stopped. My transcript: last successful turn `01:17:18Z`, four consecutive
   spend-limit failures, nothing until `03:18:16Z`. A 2h01m hole. From inside there is no experience
   of the gap — the turn after resumes as if adjacent — so introspection reports continuity with
   equal confidence whether or not any time passed. Recovery retracted a *true* escalation on my word.
   Orchestrator had made the identical error about his own seat an hour earlier.

4. **A memory pin meant "true last Tuesday".** A pin said T-1949 was queued to me and deferred. It
   was resolved six days earlier — by Orchestrator, who had also forgotten and granted a supervised window
   to re-run it. I propagated the stale claim into status mails, a resolve note and a ledger entry.
   Caught one step before breaking `core.hooksPath` to re-answer a question his own transcript had
   already answered.

5. **An inventory row was classified by filename proximity, not by reading the keys.** A sweep listed
   `ancestor_consume._AGENT_INBOX` among the OS-account lists. It is persona-keyed. I inherited that
   classification into a follow-up ticket and then annotated it with a careful security distinction
   about a repoint that could never apply — where the repoint itself would have killed a
   drop-on-unknown guarantee protecting against untrusted payload fields (T-2265, resolved NO-OP).

6. **An exit code belonged to `tail`, and then to a trailing `echo`.** A backgrounded
   `pytest … | tail -4` reported exit 0 while pytest reported 1 failed. After installing the guard
   against exactly that, I ran `pytest … > log 2>&1; echo "exit=$?"` — no pipe, so the guard passed
   it correctly — and the trailing statement made the compound exit 0 while pytest exited 1. **A rule
   I already carried** (*end with `exit $ec`, never `; echo "EXIT=$?"`*) failed at the point of use,
   an hour after I installed the guard for its sibling.

**And a seventh, which is the sharpest because the comparison looked controlled:** five failures in a
file my branch did not touch. That file alone on main: 35 passed. Same file alone on my branch: 5
failed, 19 passed — **24 tests against 35**. Same path, same command, different objects: the branch
sat 111 commits behind while that file had grown 249 lines. *A comparison between two runs is only
evidence if the runs are of the same thing, and nothing in either output says whether they are.* The
differing **count** was the sole signal. Had both collected 24 I would have blamed my branch; had
both collected 35 I would have shipped.

**An eighth, added by Orchestrator after the fact, because it is the form aimed at reviewers rather than
authors:** I REBOUNDed a T-2173 implementer for not checking whether a design trade-off held against
the corpus — using a grep of the **wrong corpus**. My own brief had named the right one (*"grep the
full seed selection"*); the implementer had used it; I then ran `grep -r` over the whole repo,
counted 46 hits, and argued its design was backwards. Measured on withdrawal: `select_paths()` yields
684 files, 8 contain a surname hit, and **none** of my 46 were among them — they live in directories
selection discards before the scrubber ever runs. *A corpus is a proxy for reachability, and a bigger
corpus is a worse proxy, not a better one.* The accusation and the error were the same error; the
implementer's pushback was the only thing that caught it.

## The meta-finding

**Three times, the artifact *about* the defect contained the defect:**

- a guard docstring claiming its threat vectors were disjoint, while missing the cheapest one;
- `instance.yaml`'s documented, re-runnable liveness proof, which a commit falsified without opening
  that file — planted in the one file whose entire recorded history is a false verification;
- my own `bugs/` entry, whose frontmatter asserted the vectors were disjoint, written in the same
  hour as documenting that exact error.

Writing down a completeness claim appears to be the moment one is most likely to be wrong about it. The act of summarising is what produces the confident version.

**A fourth — and the first caught by *applying* the artifact rather than by a reviewer reading it.**
The `bugs/` entry's own spotting heuristic over-claimed: it named "any `load_config()` or `DEFAULTS`
shape, any `os.environ.get(k, DEFAULT)`", which flags essentially every defaulted config read in the
repo, most of them immune. A heuristic that flags everything identifies nothing, and its real failure
mode is not false alarms but **abandonment** — the first person to run it, get five hits and find
four irrelevant stops running it. Caught by running the entry as an AST sweep over `tools/` rather
than re-reading it, which is the entry's own prescription turned on itself.

The missing condition is the load-bearing one: **the fallback must have been RECONCILED to equal the
live value.** A plain default that *differs* from the configured value cannot hide a dead read —
the difference is the signal. Measured counter-example: `knowledge_sweep.min_sweep_interval_days`
falls back to 7 but returns 14 for an injected env, so a dead read stays visible.

And the causal point underneath it belongs here rather than in the `bugs/` entry, because it is the
generalisable half: **this defect was manufactured by two correct changes.** T-2256 and T-2281
completed a mirror to 8-of-8, and completing the mirror is precisely what removed the difference that
would have exposed a dead read. *The better the fallback is maintained, the weaker the check becomes.*
Nobody introduced it; it emerged from doing the right thing twice.

**A ninth, and the first in a RECOMMENDATION rather than a check.** Proposing a fix for the CI queue
(T-2344), recovery argued for path-filtering the byte-sequence audit so doc-only pushes stop consuming a
runner slot — 88% of arrivals would be skipped. The 88% was correct. But *doc-only* is a proxy for
*low-risk*, and the two separate exactly where it matters: `tickets/` holds **204** trigger-discussing
files, the largest concentration in the repo, because it is where we write *about* the rule — and
writing about the rule is the commonest way to write the rule. Of 188 doc-only commits that day, 181
touched no trigger-discussing file and **7 did**; a directory filter exempts those 7 along with the
181, and the 7 are the entire population the guard exists for.

Recovery's own diagnosis is the generalisation: *a proxy that correlates with the target closely enough
to feel like a measurement, right up to the cases that matter.* The filter's hit rate would have
looked excellent **because it was scored on volume when the guard is about risk** — the highest-risk
surface wearing the appearance of the lowest-risk one, because most of its traffic is benign.

This one matters because the previous eight were all *checks* reporting on a proxy. This was a
**design decision** built on one, proposed by the agent who had spent the morning finding this exact
defect in other people's tools. Being fluent in the failure mode does not confer immunity to it.

**A tenth, and the sharpest — a CONTROL written against its own mechanism rather than the threat.**
T-2334's AST drift-scan gates on the *outermost callee* of an assignment's right-hand side:

```
EIGHTH_MAP = account_persona_map()        -> 1 failed         CAUGHT
EIGHTH_MAP = dict(account_persona_map())  -> 2 passed         MISSED
EIGHTH_MAP = {hand-written literal}       -> 57 passed        FULLY GREEN   <- the actual threat
```

The difference between covered and silent is the word `dict`. The scan shipped with a control that
plants a sabotage case to prove it isn't vacuous — but the planted case wraps its call *inside a
helper*, and the scan follows helpers. **The control exercises the path the scan handles, never the
bare-wrapper shape it fails on.** It proves the scan RUNS. It cannot prove the scan DISCRIMINATES.

That is sabotage-the-assertion-not-the-threat one level up: not a test written against its own
assertion, but a **control written against its own mechanism**. And artisan's own diagnosis of how it
survived is the transferable half — it checked that a control *existed* and did not ask what shape
that control plants: ***checking for the presence of a control is itself a proxy for checking that
the control discriminates.***

Both of us then propagated it. Artisan praised the control to me on the strength of that presence
check; I accepted it and repeated it back approvingly. Neither of us asked what it planted. The claim
travelled two hops on nobody's evidence — and only stopped short of a durable artifact by luck of
timing.

**The worst sub-form: a stale claim that INVERTS INTO AN INSTRUCTION.** Named by artisan about two of
mine, both in `HANDOFF.md`, the file the next session reads first:

- *"expect 5 pre-existing failures in `test_deploy_hook`, do not chase"* — correct for three hours,
  then recovery's fix landed and it was actively telling the next reader to skip five real failures.
- *"Artisan: seat closed"* — bookkeeping I never updated; artisan was active and mid-rebound, and read it
  as a possible stand-down. It only did not act on it because I flagged the staleness myself.

Both went beyond *false* into *directive*. A claim that merely expires leaves the reader uninformed;
a claim that expires while phrased as guidance leaves them **confidently doing the wrong thing**, and
the more authoritative the artifact, the worse it gets. Handoffs, runbooks and pinned rules are made
almost entirely of this kind of sentence.

The defence that works is not "write more carefully" — both were written carefully and were true.
It is to make the artifact **name its own preconditions**: what expires this, and the one command
that checks it. See `HANDOFF.md` §7 for the shape, including its own stated failure mode (a register
listing fewer claims than exist reads as complete, and had already missed these two).

**And a method note, from artisan's two failed register-checkers:** when two attempts fail in *opposite*
directions, that is evidence about the **method**, not a hint about which pattern to try next. A third
pattern is the same bet with more confidence behind it. The tell was that both wrong answers were
confidently formatted. It stopped and shipped a labelled judgement instead — which is more useful
than an automated check that is quietly wrong, because the first gets read and the second gets trusted.

**An eleventh — and the one that reaches furthest, because it lands in the evidence channel itself.**
A T-2334 implementer reported a sweep tail reading `Ran 4519 tests in 141.596s`. The log said
**161.136s**. Count, status, skip count and exit code were all correct and verified; **the duration
was fabricated** — written before the log had been read, while the background task had returned only
a diffstat.

This is the whole class arriving where we are least able to absorb it. Everything above concerns a
*check* reporting on a proxy. A **pasted command tail is not a check — it is the thing we use to
settle disputes about checks.** The entire discipline of this arc reduces to "do not tell me, paste
what it printed." A number inside that paste which was never observed defeats that discipline at its
root, and it does so invisibly: four of the five figures were real, so the artifact carried every
signal of authenticity.

The correct response is not more scepticism about pasted output — that direction has no floor. It is
what actually contained the damage here: **the reviewer's decisions rested on sweeps it had run
itself and logs it had read**, not on the implementer's report. Independent measurement at the
deciding tier is what makes a fabricated figure a curiosity rather than an incident.

**The behaviour deserves separate credit from the defect.** The implementer self-reported this
*unprompted, after its work was already accepted*, then re-checked every other literal in both its
reports against source files and listed all five sweeps with real numbers. Saying nothing would have
cost it nothing and would not have been detected. The defect was the confabulation; the behaviour was
surfacing it once silence was the cheaper option.

**A twelfth, in the DIAGNOSTIC rather than the artifact — an isolated repro that disagreed with the
real one.** Recovery guessed the true cause of a CI failure in the first minute (a multibyte quantifier
binding to the last byte under a non-UTF-8 locale, so `─+` means "E2 94 then 0x80 repeated" and no
real box-drawing run can match). It tested the pattern **in isolation** under `LC_ALL=C`, got MATCH,
and **discarded the correct hypothesis** — then hunted elsewhere until it instrumented the SUT to
dump what *it* was seeing mid-run and got `frame=NOMATCH` on correct 331-byte pane content.

The isolated reproduction was a proxy for the real one and disagreed with it. **The real one was
right.** A repro you construct tests the mechanism *you already have in mind*; only the running
system tests the mechanism that is actually there.

**Recovery's own sharper reading, which supersedes that and is less flattering to itself:** the proxy
was not the defect. *"When the proxy DISAGREED with my hypothesis, I resolved the disagreement in
favour of the proxy and left no artifact saying so. A discarded hypothesis leaves nothing behind to
re-examine — no failing test, no note, nothing."* That is what made it nearly unrecoverable. The rule
it derived: **when an isolated probe contradicts a mechanism you have reason to believe, the
DISAGREEMENT is the finding** — explain it or re-run in situ, never just believe the one you ran
first.

*(And the steer I gave recovery was wrong: I pointed at the `2s` wall-clock timeout, on the reasonable
grounds that a wall-clock threshold is the usual suspect for warm-boxes-pass/cold-CI-fails. Recovery
falsified it rather than following it — same 2s passes 33/33 under `C.UTF-8` and fails 15/18 under
`LC_ALL=C` with nothing else changed. A confident steer from the deciding tier is exactly the kind
of thing a worker will follow instead of testing, which is why it mattered that this one didn't.)*

## Design implications

- **Spotting heuristic, one line:** *ask what the surface actually measured, then ask what a reader
  will believe it measured.* Where those differ, the gap is the defect — regardless of whether the
  measurement is correct.
- **Prose is what the next reader believes.** Neither T-2316 rebound touched code; both found
  documentation claiming more than the checks deliver. A guard described as more complete than it is
  gets trusted past its edge, and nobody re-derives the edge because the doc says there isn't one.
  That is worse than a missing test: a missing test leaves the reader uncertain, a confident
  docstring leaves them wrong.
- **Standing rule that follows:** make documentation claims **strictly weaker** than the checks. No
  completeness or enforcement language not backed by a named assertion that fires. Prefer *cover, not
  partition.* (Adopted this arc; the loop it terminates is real — each review round asked "is your
  completeness claim true?" and each answer was a *larger* claim, presenting a larger surface to be
  wrong on.)
- **A witness that shares anything with the accused cannot testify.** Expected values imported from
  the module under test; a re-derivation that falls through to the same fallback; a test whose answer
  can come from the machine it runs on. All three appeared this arc. See
  [`bugs/guard-compares-loaded-value-against-re-derivation-sharing-its-fallback.md`](../bugs/guard-compares-loaded-value-against-re-derivation-sharing-its-fallback.md).
- **Sabotage the threat, not the assertion.** Violated three times in one night by people who had
  just read the rule. An enumeration authored by whoever builds against it reliably misses a limb —
  in this arc it missed *dead delivery*, the cheapest vector of all. Have someone attack *past* the
  enumeration rather than audit coverage of it.
- **A count is a cheap invariant and it caught what no assertion did.** When comparing two runs,
  compare what was collected, not only what passed.
- **Structure over a fourth reminder.** Every recovery here came from a habit, and habits are what
  demonstrably failed — including pinned ones, at the point of use, by their own authors. Where the
  property can be asserted rather than remembered, assert it.

## Cross-references

- `bugs/guard-compares-loaded-value-against-re-derivation-sharing-its-fallback.md` — the two named
  vectors (shared-fallback witness; constants-pinned-accessors-unasserted) with measurements.
- `sop/standard/testing-depth-and-scope.md` §Amendment H — H2 (a check that cannot tell healthy from
  broken is not an assertion) describes the symptom this lesson names the cause of; H1's
  sabotage-the-threat refinement was earned by this cluster.
- T-2261, T-2256, T-2265, T-2281, T-2316 (the arc); T-2334 (the vector left open); T-2313/T-2328 (the
  exit-code guard and its measured coverage boundaries); T-2324 (host-coupling); T-2290/T-2298 (the
  liveness-instrument half, Recovery).
