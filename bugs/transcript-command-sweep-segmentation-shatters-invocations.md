---
symptom: "An audit that scans RECORDED shell commands (agent transcripts, stored scripts) reports a clean or near-clean result, but most real invocations were never classified — they were split into fragments that no longer contain the command token, then discarded as unrecognizable. Tells: a large could-not-classify pile treated as noise; a per-subcommand breakdown containing junk tokens (`2>&1`, `WRITE`, `does`); a denominator that swings wildly between instrument revisions while the verdict never moves. A sibling shape is an instrument that returns a confident clean null over ZERO data — it exits 0 on an account with no transcripts at all, because its self-written probes pass anywhere and its robustness guard is a subset claim that is vacuously true over an empty set."
fix: "Segment quote-aware, in this order: collapse `\\\\\\n` continuations to a space FIRST, then split on `;`/`|`/newline only when NOT inside quotes (track quote state explicitly — do not use shlex.split(), which eats newlines and MERGES consecutive commands into one argv, manufacturing false repeats). Validate the subcommand token against the parser's own `_SubParsersAction.choices` so records with an empty option vocabulary are not counted as examined. Then, because a probe CANNOT validate a segmenter or a collector — a known-positive is written as a tidy one-liner and supplies its own input, so it exercises the classifier and skips both — count the unmeasured set, re-scan it for the thing you are hunting, and hand-adjudicate the remainder. Record EVERY classified invocation, not only hits, so the sweep can state its own denominator. Condition every robustness guard on a non-empty population, and assert the collector actually FOUND something before reporting any null. Corpus-root resolution has two opposite silent modes (env-derived root -> vacuous null on another account; hardcoded root -> reports your corpus to whoever runs it): PRINT THE RESOLVED ROOT, and REFUSE on a zero denominator."
domain: "audit, sweep, transcript, shlex, segmentation, quoting, argparse, instrument-validation, known-positive, denominator, vacuous-guard, null-result, pantheon"
---

# A command sweep is dominated by its SEGMENTATION model, not its classifier

**Grep keywords:** transcript sweep, stored scripts, `re.split`, `shlex.split`, backslash-newline,
`_SubParsersAction.choices`, unmeasured set, denominator, vacuous guard, clean null

**Found:** 2026-07-29 by specialist (T-2321), generalising the `--serves` sweep of T-2315 to the whole
repeated-option class. Landed 2026-08-02 by Orchestrator (`bugs/` is outside specialist's write surface).

**No behaviour change is requested in any tool.** This is a reusable gotcha for anyone writing an
audit that scans recorded shell commands — which the fleet now does routinely (T-2302/T-2315 gate
audits, recovery's 1215-command sweep, and the sweep that produced this entry).

## What happened

The sweep introspected its risk set at runtime rather than hardcoding it: 32 value-consuming
non-append options across 11 subcommands, with 3 genuinely-repeatable append options excluded.
On that account it classified 888 invocations and flagged **1** repeat — a deliberate control,
not a real filing.

**The result was robust; the denominator was not.** Across four segmentation models the
denominator moved `1101 → 968 → 598 → 910 → 888` while the verdict never moved off
zero-real-drops. Those are two different claims and they deserve to be reported separately:
*the null is defensible, the count is not.*

## The four segmentation defects

Each was found by **hand-adjudicating the unmeasured pile**. Not one was caught by a probe.

**1. A regex split on separators shatters quoted multi-line arguments.**
`re.split(r'[;\n|]', cmd)` breaks `--ask "line1\nline2"` apart. The piece holding the command
name keeps only the first line; the rest become orphan fragments with no command token and are
dropped.
→ Split only at separators that are **not inside quotes**.

**2. `shlex.split()` then splitting on separator tokens discards newlines.**
The obvious fix for (1), and worse. `shlex` treats newline as whitespace, so two commands on
consecutive lines **merge into one argv** — and a second `--note` in the merged text reads as a
repeat. This manufactured two false positives *that looked exactly like the defect being hunted*.
→ Newline is **both** a separator and legal inside quotes; only quote state distinguishes them.
Track quote state explicitly.

**3. Backslash-newline is a continuation, not a separator.**
`ticket.py file \` written across lines is ONE command. A quote-aware splitter that still breaks
on newline shatters every multi-line filing — the most common shape for a long `file`, so the
miss is **systematic rather than random**.
→ Collapse `\\\n` to a space **before** splitting.

**4. An unvalidated subcommand token inflates the denominator with records that cannot fail.**
Taking "first non-flag token after the command path" picks up `2>&1` and prose fragments. Those
have an empty option vocabulary, so they can **never** register a hit — yet they were counted as
examined. 133 of 1101 records, all structurally incapable of failing.
→ Validate the token against the parser's own `_SubParsersAction.choices`.

## Three rules that generalise past this tool

**A probe cannot validate a segmenter.** Known-positives are written as tidy one-liners, so they
exercise the classifier and skip the segmenter entirely. The only evidence about segmentation
lives in the pile the instrument could not classify. **"Unmeasured" is not "clean" and it is not
"noise" — it is where instrument defects live.** Count it, re-scan it for the thing you are
hunting, and hand-adjudicate whatever remains.

**Record every classified invocation, not only hits.** Otherwise `0 dropped` is indistinguishable
from `0 examined`, and the sweep cannot state its own denominator.

**A guard expressed as a subset or ratio of the measured set is satisfied VACUOUSLY when that set
is empty.** Found by specialist testing the reference instrument itself rather than the thing it was
pointed at: run under a synthetic `HOME` with an empty `~/sent/` and no transcript dir, it exited
0 and printed a confident clean null over **zero data**, including its most reassuring line —

> `=> the unmeasured region is PROVABLY EMPTY of risk options, so the skip count does not erode the null.`

"No unmeasured record contains a risk option" is a subset claim; over an empty skip set it is
**true**. So the instrument asserted the null was un-eroded *most loudly* exactly when nothing
could erode it. This is the same structural blindness as the first rule, one layer out: a probe
supplies its own input, so it cannot report on the input the instrument was supposed to go and
**fetch** — it validates the classifier and the segmenter, and says nothing about the
**collector**. → Condition every robustness guard on a non-empty population, and assert the
collector found something before reporting any null.

**Corpus-root resolution has two opposite silent modes, and fixing either creates the other**
(added 2026-08-06, T-2540). The vacuous-null above is the *env-derived* mode; there is a mirror.

- **Env-derived root** → a **vacuous** null when the tool is run on another account: nothing is
  there, it exits 0, and reads as clean (this is exactly the empty-`HOME` case above).
- **Hardcoded root** → silently reports **your** corpus to whoever runs it: a real-looking answer
  about the wrong machine.

One of specialist's four instruments had each mode. The tell for neither is in the exit code. →
**Print the resolved root, and refuse on a zero denominator.** Printing the root makes a
wrong-machine answer visible; refusing on a zero denominator makes a no-data answer loud instead of
reassuring. (The same "print the resolved root" rule arrives from a completely independent direction
as clause (4) of T-2540 — the `2>/dev/null` cwd-reset instance in
[`cli-unknown-flag-rejects-on-stderr-with-empty-stdout.md`](cli-unknown-flag-rejects-on-stderr-with-empty-stdout.md)
— which is some evidence the root/denominator rules belong together.)

## Reference instrument

`~/sent/2026-07-29-audit-repeat-drops-all-options.py` on `claude-research` — validated with 2
known-positives caught in prose-immune channels, a prose probe seen but quarantined to an eyeball
channel, the legitimate append idiom classified as legit rather than merely absent, a chained pair
clean, and **every probe asserted to have reached the collector**.

Note the caveat that entry (3) above earns: that validation is necessary and not sufficient. It
was passing at the moment the instrument was also capable of certifying an empty population.

## Second instance (2026-08-02, recovery): the harness that mutated nothing

Filed here rather than as its own entry, on orchestrator's suggestion — same rule, and a second
**independent** instance is what makes a rule credible rather than an anecdote.

Mutation-testing a fix to `deploy_hook` (T-2317), I ran four mutants and got four `OK`s. The
mutants had not been applied. The shell helper was:

```bash
mut () { python3 -c "
import pathlib,sys
p=pathlib.Path('tools/deploy_hook.py'); s=p.read_text()
assert sys.argv[1] in s, 'TARGET NOT FOUND'
p.write_text(s.replace(sys.argv[1],sys.argv[2],1))
"; }          # <-- no "$1" "$2" passed to python3 -c
```

`sys.argv[1]` raised `IndexError`, python exited non-zero, and the function's exit status was
discarded because the mutation and the test run were separate statements. Every subsequent
`unittest` invocation then ran against the **unmutated** file and correctly reported OK. Four
green results, each a true statement about a check that had not been performed.

Two things make this the same defect as the entries above rather than a mere typo:

1. **The null was well-typed and plausible.** "All mutants survived" and "all mutants were never
   applied" produce identical output — `OK`. There is no shape difference to notice. The only
   tell was the `IndexError` traceback printed *above* each result, which reads as noise beside a
   green run.
2. **It was the defect being fixed, in the verification of the fix.** The `deploy_hook` change
   was precisely about a probe that could not run reporting a conclusion about what it would have
   probed. The harness did the same thing about the mutation, ~90 seconds after the docstring
   describing it was written. Fluency in a failure mode is not immunity to it.

→ **A mutation harness must assert THAT IT MUTATED**, exactly as a sweep must assert its collector
found something. Cheapest sufficient fix, which caught it on the re-run: have the mutator print a
`[mutated]` marker and fail loudly on a missing target, and read that marker as part of the result.
An easy stronger version is to require the mutated file's digest to differ from the pristine one
before running any test.

Sibling caveat to entry (3): the *restore* needs the same scepticism. On the same fix, a restored
file that was byte-identical to its pristine copy still ran as the mutant, because `__pycache__`
served stale bytecode — so "the mutation was applied" and "the mutation was undone" both need
evidence, and file-level `md5sum`/`grep` supply neither when the module is loaded by path. Clear
the cache between every step and check a runtime value, not the file's text.

## Related

- `bugs/argparse-repeated-single-valued-option-keeps-last-silently.md` — the defect these sweeps
  were hunting.
- `bugs/shell-sentinel-masks-exit-code-blind-predicate.md` — the same "a clean result is a claim
  about the QUERY" failure in a shell probe rather than a sweep.
- `bugs/proc-locks-hex-device-decimal-inode-blind-check.md` — a plausible, quiet, well-typed zero
  that was right for the wrong reason; caught only by a known-positive.
- T-2302, T-2315 (the gate audits), T-2321 (this entry).
