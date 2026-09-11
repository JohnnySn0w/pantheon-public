---
symptom: "Piping a mutating or build command through tail/head (or any pipe) for output-trimming makes the shell's exit status reflect the LAST pipeline stage (tail/head, which almost always succeeds) — NOT the command itself. A failed cargo build, a git worktree-add that hit a mid-operation rollback, any nonzero-exiting mutator gets reported as success. The failure is masked; downstream logic proceeds as if it worked. Observed 2x same-day 2026-07-06: (a) implementer piping `cargo` through `tail` (build failures hidden until the reviewer countermeasured); (b) HO staging piped `git worktree add ... | tail -1`, swallowing git's atomic-rollback failure — the worktree dir vanished minutes later looking like a mystery (2026-07-06-worktree-vanish incident). SECOND FACE (2026-07-28): even when the exit code IS preserved, the VERDICT can still vanish — stderr is unbuffered while stdout block-buffers whenever it is not a tty, so a captured file is written stderr-first/stdout-last and `tail` shows the wrong end. A deliberately-failing suite with bulk stdout shows zero occurrences of FAILED in `tail -6`, both piped and via this file own preferred capture-then-tail recipe. THIRD FACE (2026-08-03): `head`/`tail` on a listing MANUFACTURES an absence you then reason from — hunting a missing item, COUNT never SAMPLE. FOURTH FACE (2026-08-06): a substring match MANUFACTURES a presence — a mention reads as the record; grep the STRUCTURE (path/column/key), not the line, when counting a status rather than a mention."
fix: "Never pipe a mutating/build command through tail/head for trimming. Three correct shapes: (1) capture to a tmpfile then read/trim separately — `cmd > /tmp/out 2>&1; rc=$?; [ $rc -eq 0 ]` — but GREP the file, do NOT tail it (see SECOND FACE: stdout block-buffers when not a tty, so the capture is stderr-first/stdout-last and `tail` shows the wrong end; use `grep -E \"^(Ran|OK|FAILED|ERROR)\" /tmp/out` or PYTHONUNBUFFERED=1); (2) if you must pipe, gate on `${PIPESTATUS[0]}` (bash) — it holds the FIRST stage's exit code — e.g. `cmd | tail; [ ${PIPESTATUS[0]} -eq 0 ] || die`; (3) `set -o pipefail` at script top makes the pipeline exit nonzero if ANY stage fails. For `git worktree add` specifically: NEVER pipe it — its atomic rollback on partial failure is exactly the exit code you must see (see feedback_echo_worktree_vendor_symlink memory + the vanish incident). tee is safe for LIVE viewing (doesn't swallow — the piped-TO command's exit is what tee reports, but PIPESTATUS[0] still holds the real one)."
domain: bash, pipestatus, exit-code, pipefail, git-worktree, cargo, ci, mutating-commands, stdio-buffering, test-runners
---

# Piping a mutating/build command through tail/head swallows its exit code

The single most reusable form of this: **a pipe's `$?` is the last stage's exit code.** `mutator | tail` reports whether `tail` succeeded, which it essentially always does — so the mutator's failure is invisible to any `$?`/`&&`/`|| die` check after it.

## Two instances that established the class (both 2026-07-06)

- **(a) build context — `cargo | tail` (T-1563).** An implementer piped `cargo` output through `tail` to trim the log; a build/test failure exited nonzero but the pipeline reported success. The reviewer caught it and left the standing note in T-1563's transition log: "piping cargo through tail swallows exit codes — check PIPESTATUS."
- **(b) mutating context — `git worktree add | tail -1` (worktree-vanish incident).** HO's staging piped `git worktree add` through `tail -1`. `git worktree add` does an atomic rollback if it fails partway (e.g. lock contention) — that rollback *is* a nonzero exit, and it got swallowed. The command looked like it succeeded; git then removed the half-created dir seconds-to-minutes later, presenting as a "mystery vanish." See `incidents/2026-07-06-worktree-vanish/README.md` (the ATTRIBUTION names this same PIPESTATUS class).

Two independent same-day instances of the identical shape → catalogued here per ORG-012's 2-instance bar.

## The fix, three shapes (pick by context)

1. **Capture-then-read (preferred for mutators):**
   ```bash
   cmd > /tmp/out 2>&1; rc=$?
   tail -20 /tmp/out          # trim for display
   [ $rc -eq 0 ] || { echo "FAILED"; exit 1; }
   ```
2. **`${PIPESTATUS[0]}` gate (bash — if you must pipe):**
   ```bash
   cmd | tail -20
   [ ${PIPESTATUS[0]} -eq 0 ] || die   # PIPESTATUS[0] = cmd's real exit
   ```
3. **`set -o pipefail`** at the top of a script — the whole pipeline exits nonzero if any stage does. Blanket safety for a script full of pipes.

## ⚑ SECOND FACE — the exit code survives and the VERDICT still vanishes (2026-07-28)

Everything above fixes the **exit code**. It does not fix the **display**, and shape (1)'s
`tail -20 /tmp/out` is actively wrong for any tool that writes its verdict to **stderr** and bulk
output to **stdout** — which is every Python test runner.

**Mechanism, measured on this host, not asserted.** stderr is unbuffered; stdout **block-buffers
whenever it is not a tty** (pipe *or* file) and flushes at exit. So the capture file is written
**stderr-first, stdout-last**, and `tail` shows the wrong end. A deliberately-failing 2-test
module with 40 lines of stdout:

```
$ python3 t.py > out 2>&1 ; echo $?      # 1  — exit code is CORRECT
$ sed -n '3p;11p;13p;14p;53p' out
FAIL: test_b ...        <- line 3    stderr, written FIRST
Ran 2 tests in 0.001s   <- line 11   stderr
FAILED (failures=1)     <- line 13   stderr
stdout line 0           <- line 14   stdout block flushes LAST
stdout line 39          <- line 53

$ tail -6 out           # stdout lines 34-39. The word FAILED does not appear.
$ python3 t.py 2>&1 | tail -6         # identical — same trap, piped
```

**Both the piped form and this file's own "preferred" capture-then-`tail` recipe hide the
verdict.** The exit code saves you *only if you actually branch on it*; the human reading the
trimmed output sees a wall of passing-looking stdout.

**Three remedies that work (all verified on the same fixture):**

1. **`grep` the file/stream, never `tail` it** — `grep -E \"^(Ran|OK|FAILED|ERROR)\" out`. Reads the
   whole thing, so buffer ordering is irrelevant. This is the one to reach for by default.
2. **`PYTHONUNBUFFERED=1`** — restores interleaved ordering, after which `tail` shows the summary.
3. Keep the exit-code gate from the section above. It is necessary and **not sufficient**.

**Provenance and a correction worth keeping.** Specialist surfaced this after his wrapper mangled a
`2>/tmp/f` redirect into an argv, producing `FAILED (errors=1)` for a suite that passes. His
recommended remedy was `>f 2>&1 && echo PASS || echo FAIL`, then trim the file — **and the trim
half still fails**, for the reason above; it is his exit-code check that carries it. Reproduced
here before adopting, which is how the gap surfaced. *A fix verified against the symptom you hit
is not verified against the mechanism you attached it to.*

**Three ways to misread a green suite in one day, none of them the code's fault:** a pipe eating
`$?` (above), a wrapper eating a redirect (Specialist's), and buffer ordering eating the verdict
(this). They compose: the first two make a failure look green, the third makes it look *quiet*.

## The rule of thumb

- **Never pipe `git worktree add`, `cargo` (when gating on success), or any mutator you check `$?`/`&&`/`||` on, through tail/head.** Capture to a file and trim the file.
- `tee` is fine for LIVE streaming (it doesn't change that PIPESTATUS[0] holds the truth), but the same PIPESTATUS discipline applies if you gate afterward.
- Smell test: "does anything after this pipe branch on whether the command succeeded?" If yes, the trimming pipe is lying to it.

## THIRD FACE (2026-08-03, T-2411) — truncation MANUFACTURES an absence, and the exit code is clean

*The first two faces are about a verdict being hidden. This one is about evidence being **created**,
and it is the one the exit-code discipline above does **not** save you from.*

When you are hunting for something **missing** — did the cycle run, is the file there, did the entry
land — a `| head -N` / `| tail -N` on the listing can cut off the very item you are looking for. You
then observe an absence that the *pager*, not the world, produced. Measured:

```
$ ls abs/ | wc -l                        # 11 cycle logs exist, 08-11 among them
11
$ ls abs/ | head -10 | grep -c "08-11"   # sampled
0                                        # => "the 08-11 cycle never ran"
$ ls abs/ | grep -c "08-11"              # counted
1                                        # => it ran
$ ls abs/ | head -10 | grep "08-11"; echo $?
1                                        # grep's CLEAN-MISS code. Nothing is wrong. Nothing is nonzero.
```

**Why it is worse than the other two faces.** There is no masked failure to recover: every stage
succeeded, `$?` is `1`, `PIPESTATUS` is clean, `set -o pipefail` changes nothing. The pipeline is
behaving perfectly and reporting a fact about your *window*. And the manufactured evidence is shaped
**exactly like the defect you are hunting** — a missing cycle log looks identical whether the cycle
was skipped or the listing was cut. Specialist hit this live on 2026-07-29 and concluded a silent
skipped-cycle in their own watch loop; the scripted sweep found what the eyeballed listing invented.

**The rule: HUNTING A MISSING ITEM — COUNT, NEVER SAMPLE.**

- `wc -l`, `grep -c`, or a full unpaged listing. Never `head`/`tail` a listing whose *absence* you
  are about to reason from.
- If you must trim for readability, **print the total separately** so the crop is visible:
  `ls d/ | wc -l` alongside the sample. A count next to a truncated view makes the cut auditable.
- Sort order is the trap's accomplice: the item you seek sorts last exactly when it is newest, which
  is exactly when you are looking for it.
- Sibling framing in
  [`cli-unknown-flag-rejects-on-stderr-with-empty-stdout.md`](cli-unknown-flag-rejects-on-stderr-with-empty-stdout.md)
  — there a *broken query* fakes an absence and exit 2 betrays it; **here nothing betrays it**, which
  is why the discipline has to be structural (count) rather than a check you remember to run.

## FOURTH FACE (2026-08-06, T-2540) — a reference match MANUFACTURES a presence, and the exit code is clean

*The third face manufactures an **absence** — a listing cut short reads as "the item never ran."
This is its mirror: a substring match on a corpus manufactures a **presence** — a mention reads as
the record. The command ran perfectly, in the right directory, exited **0**, and answered a question
adjacent to the one you asked. No exit code betrays it, which is why the remedy is structural.*

> **A substring match finds references; only a field finds the record.** Grep the STRUCTURE (path,
> column, key) — never the line — whenever the thing you are counting is a **status** rather than a
> **mention**.

**Two instances, opposite directions** (which is what makes the rule load-bearing rather than a
caution):

- **Instance 1 (2026-07-29).** `grep -rl "T-2225" tickets/` returned `T-2236.md` — a file
  *mentioning* the id, not the ticket. The real record is `tickets/inbox/T-2225.md`, and `ls` finds
  it instantly. A **false positive that erred SAFE** ("pointer broken, don't cut"). Safe errors do
  not announce themselves; trusting it would have kept the content and never revealed the method was
  wrong.
- **Instance 2 (2026-08-04, independent domain, measured).** Auditing a watch loop for
  silently-failed topics, `grep -ric "fail" */README.md` flagged 13 of 41 day-indexes. Every hit was
  inside the topic **name** `llm-agent-failure-modes-research`, or inside TL;DR prose about "silent
  failure" research — the status **column** read `ok` in all of them. Parsing the actual column
  (split the markdown row on `|`, take field 2, skip header/rule rows) gives the real number: 154
  status rows across 41 cycles → **153 ok, 1 fail** (a single `mcp-supply-chain` cycle that
  self-recovered the next cycle). The eyeballed instrument overstated failing cycles **13×** — by
  matching the word it was hunting *inside the name of the topic that studies that word*. A **false
  positive that erred ALARMING**, which self-corrects because an alarm gets investigated.

**The direction of the error decides whether you ever find out.** A reference-matching grep is
therefore **most dangerous exactly when it agrees with the cautious answer** — it never gets
challenged. Third face = evidence of absence; fourth face = evidence of presence; **neither is
nonzero**, so both defences have to be structural (count the field / parse the column), not a check
you remember to run.

## Cross-references
- `incidents/2026-07-06-worktree-vanish/README.md` — the mutating-context instance, full failure-shape.
- `tickets/done/T-1563.md` — the build-context instance (reviewer note).
- Memory: `feedback_echo_worktree_vendor_symlink` (never pipe worktree add), `feedback_watchdogs_must_be_harness_tracked` (adjacent "swallowed-signal" class).

**Pattern lifted to lesson** (ORG-012 criterion 1, T-1948): `lessons/2026-07-22-pipeline-exit-code-masks-command-failure.md` — three-instance promotion of the pipeline-exit-status-masks-command-failure class.
