---
symptom: "A root-run enumeration over N items prints N-1 rows with no error, no blank line and no gap — the missing row is always the FIRST. `sudo -S` writes its `[sudo] password for ...` prompt to stderr with NO trailing newline, so under `2>&1` it concatenates with the first line of real output, and the conventional `| grep -v 'sudo\\]'` cleanup then discards prompt and first data row together."
fix: "Pass `-p ''` to sudo and NOTHING ELSE — keep `2>&1`, drop the `grep -v`. Do NOT reach for `2>/dev/null`: that fixes the first-row loss and silently discards every FAILING row instead, which is worse for a sweep. Verify over a known-length list containing one deliberate failure and count rows."
domain: pantheon, sudo, shell, stderr, enumeration, silent-data-loss, measurement-integrity
---

## Symptom

A root-run loop over N items prints N-1 rows. No error, no blank line, no gap — the output looks
complete and well-formed. The missing row is always the **first** one.

## Reproduction

```bash
tr -d '\n' < /home/claude/root.env | sudo -S -k bash -c \
  'for i in A B C; do echo "line-$i"; done' 2>&1 | grep -v "sudo\]"
```

```
line-B
line-C
```

`line-A` is gone.

## Mechanism

`sudo -S` reads the password from stdin but still writes its prompt — `[sudo] password for claude:`
— to **stderr**, and that prompt has **no trailing newline**. Under `2>&1` the prompt and the first
line of real output become a single line:

```
[sudo] password for claude: line-A
```

The conventional cleanup, `| grep -v "sudo\]"`, then discards that whole line — prompt *and* the
first row of data with it. The filter written to hide the prompt is what destroys the evidence.

## Fix

```bash
tr -d '\n' < /home/claude/root.env | sudo -S -k -p '' <cmd> 2>&1
```

`-p ''` sets an empty prompt, so there is nothing to concatenate against — which makes `2>&1` safe
and the `grep -v` unnecessary. That is the whole fix.

**Do NOT "improve" it with `2>/dev/null`.** That was this entry's original prescription and it is
wrong: it trades the first-row loss for a worse one. Measured with a wrapper that emits a
no-trailing-newline prompt exactly as `sudo -S` does, over a six-item sweep where one item FAILS:

```
A  2>&1 | grep -v 'sudo]'    -> 5 rows; loses the first row, but KEEPS the failing row
B  -p '' + 2>/dev/null       -> 5 rows; keeps the first row, LOSES THE FAILING ROW
C  -p '' + 2>&1  (no grep)   -> 6 rows; all six accounted for
```

A and B both print five rows and both look complete and well-formed. Only C accounts for everything.

**When stdout must stay machine-parseable** (the one legitimate reason to want stderr out of the
stream): send it to a FILE and check the file — `2>/tmp/sweep.err` followed by a non-empty test —
never to `/dev/null`. The difference is not cosmetic: a file keeps the failing rows recoverable and
makes "the sweep was clean" a checkable claim; `/dev/null` makes it an unfalsifiable one. (This
variant was part of the T-2331 correction and originally fell out of the fold-in.)

**B is worse than A for the purpose the idiom serves.** A loses the alphabetically-first row, which
is bad — but a sweep hunting breakage can still *find* breakage. B loses the failing row and only
the failing row, making an enumeration whose entire job is to surface broken things structurally
incapable of surfacing them — and it fails silently in the ordinary case where nothing is broken, so
it looks correct every time until the one run that mattered.

Once `-p ''` has removed the prompt, `2>/dev/null` is hiding nothing that is still there; it does no
work except discarding diagnostics. In a sweep, a failing item belongs in the enumeration beside the
ones that succeeded.

*(Correction credit: mnemosyne, T-2331 — this entry originally prescribed `2>/dev/null` and I had
broadcast that to five seats before the second half was measured.)*

## Why it is worse than it looks

The loss is **deterministic, not random**: iterate a sorted list and you lose the alphabetically
first item, every time, forever. It is invisible precisely where enumerations are used as evidence —
"I checked all six seats" is a claim the output appears to support while one seat was never shown.

Found during the T-2313 fleet rollout, where every six-seat sweep silently dropped its first row.
That row was always `claude`/orchestrator — which happened to be the seat that *was* the T-2313 coverage
gap, and the only failing seat in T-2324. Those answers came out right because the seat was also
checked separately out of habit, not because the method was sound.

## The general form

`sudo -S` is one instance, not the class. Stated wider (mnemosyne, T-2331):

> **A filter written to suppress a prompt will eat any data the prompt fused with, and
> prompt-fusion happens whenever a writer omits the trailing newline.**

Anything that prints a bare prompt to a stream you are merging has this shape — a password prompt, a
`Continue? [y/N]`, a progress spinner, a REPL banner. Two rules follow:

1. **Suppress the prompt at the source** (`-p ''`, `--no-progress`, `--batch`) rather than filtering
   it downstream. A filter cannot tell the prompt from what it fused with.
2. **Never verify a filtering fix against all-passing data.** Both defective idioms above print a
   plausible, well-formed, complete-looking list when nothing is broken. The failing row is the only
   thing that distinguishes them, so a test without one cannot see the difference.

## Related

- `feedback_health_claims_need_comprehensive_not_curated_checks` — "verified healthy" needs
  exhaustive enumeration; this is an enumeration that silently isn't one.
- `bugs/sudo-S-heredoc-stdin-collision-false-auth-failure.md` — the other way this same idiom
  goes wrong (heredoc replaces the password on stdin).
- T-2320 / T-2328 — same week, same family: an instrument narrower than its stated purpose, with
  nothing in the instrument saying so.
