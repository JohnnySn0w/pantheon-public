---
symptom: "You passed a flag the CLI does not have (`ticket.py block --note`, `ticket.py file --body`). Nothing was applied, but you believed it was. The output CONTAINS YOUR PAYLOAD TEXT verbatim — `error: unrecognized arguments: --note <your text>` — which skims as acceptance, and it is invisible to the two commonest invocation shapes: anything that pipes the command through a filter (the pipe replaces the exit code), and anything that reads stdout only (STDOUT IS EMPTY — not even an error lands there)."
fix: "GENERAL RULE — a tool that signals 'no result' by printing nothing makes a query-that-never-ran byte-identical to a query-that-found-nothing on stdout; only the exit code separates them (ripgrep: 0 hit / 1 clean miss / 2 broken pattern or bad flag). Branch on the exit code, or run the pattern against a known positive in the same invocation before believing a zero. For the argparse case: Check the STATE, never stdout. `grep '^depends_on:' tickets/**/T-NNNN.md` after a block; `list` after a file. Never pipe a mutating CLI call through `head`/`tail`/`grep` — redirect raw and filter at read time, or the exit code is the filter's. Confirm the flag exists with `<subcommand> --help` BEFORE relying on it; argparse subcommands do not share flags, so `--note` existing on `annotate` says nothing about `block`."
domain: "ticket.py, argparse, cli, exit-codes, silent-failure, stderr, shell-pipelines, ripgrep, rg, grep, search, absence-claims, stderr-suppression, dev-null, pantheon"
---

# An unknown CLI flag rejects on stderr with an EMPTY stdout — and your payload is in the error

**Measured 2026-08-02 (T-2411 staleness pass), not transcribed.** The inherited version of this rule
said the command *"exits 2 with an argparse error that echoes your text back, reading like success"*.
Half right, and the half it got wrong is the half that matters. Running it produces something
sharper.

## What actually happens

```
$ python3 tools/ticket.py block T-9999 --note "CANARY"
usage: ticket.py [-h] {file,claim,unclaim,annotate,supersede-ask,...} ...
ticket.py: error: unrecognized arguments: --note CANARY
$ echo $?
2
```

Measured on `ticket.py block --note` and `ticket.py file --body`, both identical:

| | value |
|---|---|
| exit code | **2** |
| stdout | **0 bytes** |
| stderr | 235 bytes, containing your payload verbatim |
| side effects | none — argparse rejects before any write |

So it is **not** silent, and it is **not** a success. A caller that checks `$?` catches it every
time. The danger is narrower and more specific than "reads like success".

## Why it still bites — two invocation shapes make it invisible

1. **Piped through a filter.** `... | head -6` reports the *filter's* status, so a genuine exit 2
   reads as `exit=0`. This is not hypothetical: it happened while measuring this very entry — the
   first run printed `exit=0` and I nearly wrote that down as the finding.
2. **stdout-only capture.** `subprocess.run(..., capture_output=True).stdout`, or `cmd > log`, gets
   **nothing at all**. Not a truncated error — an empty string. Code that logs stdout and moves on
   has no signal whatsoever that the flag was dropped.

Combine either with a payload appearing in the terminal and the human reads "my text went through".

## The rule

- **Check the STATE, not stdout.** After `block`: `grep '^depends_on:' tickets/**/T-NNNN.md`. After
  `file`: `list`. The mutation is the claim; the output is not.
- **Never pipe a mutating CLI call through `head`/`tail`/`grep`/`wc`.** Redirect raw, filter at read
  time. (Same mechanism as [[pipes-on-background-commands-eat-exit-codes-and-evidence]].)
- **Flags are per-subcommand.** `--note` exists on `annotate` and not on `block`; `--with` exists on
  `resolve` and not on `annotate`. Confirm with `<subcommand> --help` before relying on one.

## Known instances (verified against the live CLI 2026-08-02)

| claim | status |
|---|---|
| `block` has no `--note` | **TRUE** |
| `file` has no `--body` (it is `--ask` / `--context`) | **TRUE** |
| there is no `show` subcommand — read with `sed -n '13,30p' tickets/<state>/T-NNNN.md` | **TRUE** |
| `--intent` is `target=operator`-only | **TRUE** |
| **the `Ask` field has no correction path** | ⛔ **NOW FALSE — RETIRED.** `ticket.py supersede-ask` landed 2026-08-02 (T-2247, `533ee6c1f`) and corrects the Ask **in place**. Any doc still asserting "correctable only by an annotate landing below it" is stale. |

That last row is why this entry leads with "measured, not transcribed". The inherited rule set
carried the no-correction-path claim as its **headline** trap; lifting it verbatim into a durable
home would have committed a falsehood into a *more* trusted surface than the one it came from —
which is worse than leaving it in a scratch file, because the durable copy is the one people cite.

**Check every rule against the live surface before promoting it.** A rule's age is not evidence of
its truth; it is a reason to re-measure.

## The same shape in SEARCH tools — a broken query and a clean miss both print nothing

*Added 2026-08-03 (T-2411 slice 5). The generalisation is the point: this is not an argparse quirk.
Whenever a tool signals "no result" by printing nothing, **a query that never ran** and **a query
that ran and found nothing** are byte-identical on stdout, and only the exit code separates them.*

Measured on **ripgrep 14.1.0**, this box:

| invocation | exit | stdout | stderr |
|---|---|---|---|
| `rg 'beta' f.txt` — real hit | 0 | the match | — |
| `rg 'zzz' f.txt` — real miss | **1** | empty | — |
| `rg 'alpha(?!x)'` — lookahead | **2** | empty | `error: look-around ... is not supported` |
| `rg '(?<=al)pha'` — lookbehind | **2** | empty | same, suggests `--pcre2` |
| `rg '--dashline' f.txt` | **2** | empty | `rg: unrecognized flag --dashline` |
| `rg -e '--dashline' f.txt` | 0 | the match | — |

**The inherited rule said ripgrep reports lookaround as `No matches found`, not an error. That is
false at the binary** — rg is loud, exits **2**, and even suggests the fix. Corrected here rather
than transcribed, for the same reason as the row above.

**But the trap it was pointing at is real,** and the table shows exactly where: a broken query (2)
and a clean miss (1) have **identical empty stdout**. So every stdout-only shape erases the
distinction —

```bash
if [ -z "$(rg "$pat" "$f")" ]; then echo "absent"; fi   # "absent" on a BROKEN pattern
matches=$(rg "$pat" "$f" | wc -l)                        # 0 on a BROKEN pattern; wc's exit code wins
```

— and reports a confident absence about the world when what it measured was the query. **Exit 1 means
absent; exit 2 means you did not ask.** Branch on that, or run the pattern against a known positive
in the same invocation before believing any zero.

**One attribution left open, deliberately.** `No matches found` is not a string ripgrep emits — it is
the wording of the *harness* `Grep` tool. So the original observation was plausibly made through that
wrapper rather than through `rg` in a shell, which would make it a real silent-zero at the tool
surface even though the binary underneath is loud. **I could not test that from this seat** (no `Grep`
tool in this session), so it is recorded as unverified, not asserted. Anyone with the tool available
can settle it in one call: run a lookahead pattern and see whether the error surfaces or renders as
`No matches found`. If it renders as no-matches, that surface needs its own entry.

## `2>/dev/null` on a search erases BOTH the code and the text — the only fully-silent shape

*Added 2026-08-06 (T-2540, from specialist's 2026-07-29 flag-audit arc). The two shell shapes above
(`[ -z "$(rg ...)" ]`, `... | wc -l`) lose the exit **code** and keep the diagnostic **text** — a
human reading the terminal still sees `No such file or directory` and can catch it. `2>/dev/null`
removes **both**, and unlike the other two it is done **deliberately**, for tidiness.*

> **`2>/dev/null` on a search converts every broken-query mode in the pipeline into a clean
> negative.** No code to branch on, no text to read. It is the only one of the shapes that leaves
> literally no trace at the surface the operator is actually looking at — and the motive is what
> makes it stick: you add `2>/dev/null` to silence noise from a directory that *might not exist*,
> which is precisely the condition you then fail to detect.

**First-person instance, 2026-08-04, caught mid-T-2539.** A coverage sweep of 11 claims across
`bugs/` + `lessons/` returned ZERO for all 11. About to record "nothing is covered" — which
justifies **not** cutting, i.e. it erred **SAFE**, which is why nothing would have prompted a
re-check. The real cause: the shell cwd had reset to `/home/claude-research`, where `bugs/` and
`lessons/` do not exist; grep was exiting **2** on every call; a habitual `2>/dev/null` swallowed
the `No such file or directory` for all of them. What caught it was **not** the instrument — it was
knowing, from having *read* a specific entry earlier that session, that one claim was definitely
present. A zero the operator could personally falsify.

**The sharp edge:** the exit-1-vs-exit-2 rule above had been written into the operator's own handoff
~20 minutes earlier, and *knowing it did not help*, because `2>/dev/null` removes the very signal the
rule operates on. **A rule that requires a signal you have already discarded is not a defence.**

**Structural remedy (this is what now runs):**
- **Refuse on `returncode == 2`** — do not fold a broken query into a clean negative.
- **Run a known-POSITIVE and a known-NEGATIVE in the same sweep** — a zero on the positive proves
  the instrument, not the world.
- **Print the RESOLVED search root** — so a cwd/root surprise is visible, not silent. (That last one
  is clause (3) of T-2540 arriving from a completely independent direction — see
  [`transcript-command-sweep-segmentation-shatters-invocations.md`](transcript-command-sweep-segmentation-shatters-invocations.md)
  — which is some evidence the denominator/root rules belong together.)

Same lesson from the other side of this file's opening: **the text you believe you are reading is a
property of the instrument you asked through, not of the world.** Suppression is the degenerate
case — the instrument says nothing at all, and silence reads as clean.
