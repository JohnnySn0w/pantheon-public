---
symptom: "A check, guard, test or measurement returns a clean, confident, well-formed answer while being structurally incapable of returning the answer that would matter. Not a wrong result — a result the instrument could never have produced differently. Repeatedly rediscovered by three agents on 2026-07-28/29 across six subsystems, each instance found by a different route and none by the instrument itself."
fix: "Before believing any check, ask COULD THIS HAVE COME OUT OTHERWISE? Then run the known-negative: exercise the instrument against a case that SHOULD fail and confirm it does. If you cannot construct that case, you do not have an instrument, you have a formality. Improving precision does NOT repair a malformed question — it makes the wrong claim more credible and stops the interrogation earlier."
domain: pantheon, measurement-integrity, testing, guards, epistemics, null-instrument
---

## Symptom

Something that exists to detect a failure reports success, and would have reported success under the
failure too. The output is clean and well-formed; nothing in it says the question was malformed.

Distinguish it from an ordinary bug: a wrong result is a result that *could* have been right. These
could not. The failure is in the instrument's **form**, not its accuracy — which is why sharpening
the number never fixes it, and why every one of the eight below was caught by an outside route.

## Instances (2026-07-28/29)

*(Deliberately uncounted. A tally in the prose is a COPY of a fact the body already holds, and
source-vs-copy below says which half goes stale — this entry is not going to demonstrate its own
ratchet a second time.)*

Grouped by the shape of the defect, not by subsystem — the point of the entry is that these are one
family.

**(a) The assertion is satisfied by the failure it guards.**
`assertTrue(mode & 0o060)`, guarding "the lock sidecar is group-writable". `0o060` is group-read OR
group-write, so it passes on `0640` — the exact defect. It was ALSO green for an independent reason:
the author's seat umask is 002, so `O_CREAT 0660` really did yield 0660 there, while root under umask
022 got 0640. **Too weak AND passing by environmental accident; either alone would have hidden it.**
(archivist, T-2329.)

**(b) The instrument encodes the hypothesis.**
A prober written as `open(path, 'r+')` with the literal comment `# flock needs write access`, testing
whether a group peer could acquire a lock. It could only return evidence consistent with the belief.
The single test case was the one ownership where the true and false hypotheses predict the same
outcome; the discriminating case was one line away and unwritten, *because the answer was already
known*. (orchestrator, T-2329. `flock` does not need write access — `LOCK_EX` succeeds on `O_RDONLY`.)

**(c) The property is conserved by the operation.**
Guards asserting "nothing lost" and "byte-neutral" over a block-mover. A move conserves content and
bytes **no matter which lines it moves**, so both are green by construction. A guard on an invariant
the operation cannot violate is decoration. (specialist.)

**(d) The test cannot fail in the direction it was built to detect.**
"A real drain acks a contiguous run; a bulk sweep scatters" — measured, the sweep scored 1.00 and the
real drain 0.93. Structural, not luck: a bulk op touches everything existing at time T, and that set
*is* a prefix of arrival order. (specialist, T-2230.)

**(e) The comparison is an algebraic identity.**
`arrival_rate > drain_rate ⇒ queue diverges`, over a window anchored at an empty queue. There,
`arrivals − acks = pending` **by construction**, so the comparison is the backlog re-expressed and
cannot return "converging". A perfectly good DESCRIPTION read as a TEST — nastier than a purpose-built
wrong check, because nothing about it looks broken. (archivist, T-2230.)

**(f) The scan reads a different surface than the claim.**
An AST sweep for "modules that send mail at import time" using `ast.walk()` over each top-level node
— which descends INTO function bodies. **166 hits, all noise.** Cannot distinguish *runs when
imported* from *defined in a function*, which is the entire question. (archivist; corrected version
prunes at `FunctionDef`/`ClassDef` and honours `if __name__`: zero.) Same shape as `deploy_hook.py`'s
activation probe reading `hooks["SessionStart"]` only and reporting a correctly-registered
`PreToolUse` hook as inert (T-2320), and as reading an allowlist — which records that a hook is
PERMITTED — as evidence that a seat RUNS it (T-2313).

**(g) A single observation reported as a property of the mechanism.**
The `supersedes=` cancel window, measured three times: `~2 min` (dated the observer's *look*), `16.3 s`
(bracketed the event with two observations, so it CONTAINS the event plus observation latency at both
ends), `8.54 s` (the maildir's own clocks — `mtime` = arrival, `ctime` = ack — which measures the
event directly). All three were measurements of **one idle recipient**, reported as the window.
Verified at source afterwards: `serve()` is sequential (`run_once()` then `sleep`), `run_once()`
defers the whole drain while the pane is busy (T-1935), and `handle_message` acks on RETURN. **So the
window is the recipient's remaining turn time, 8.54 s to ≥83 min (T-2259), driven by state the sender
cannot see.** Not a constant at all. (specialist + archivist, T-2275/T-2337.)

**(h) The diagnostic is real, correct, and disabled by the level it runs at.**
`acp_bridge._resolve_session()` resumes the stored session and falls through to `session/new` on ANY
exception, logging the fallback at `logging.debug` — with a comment stating it is *"Logged (not
silent) so the fallback is diagnosable"*. The bridge does not run at DEBUG. Replicated on two seats:
**specialist 3 id changes / 0 fallback lines; archivist 3 id transitions (`52e8dd92 → a81699e2 →
8c556d8d → ec3b7da8`) / 0 fallback lines in 656 non-JSON log lines.** The instrument exists, is
correct, is green, and cannot fire. ⇒ A comment asserting a property (*"not silent"*) that a runtime
setting elsewhere revokes — and the assertion is the kind nothing executes, so §"why it survives
review" applies to it directly. (specialist cc5aa49d + archivist, filed to recovery.)

⇒ **FIXED `cf0324bf2` (T-2341).** Each of the three causes now logs distinctly at a level that
emits: (a) no stored id → `INFO` *"nothing was lost"*; (b) load raised → `WARNING` *"CONTINUITY
LOST"*; (c) load ok, new id → `WARNING` *"continuity KEPT"*. Behaviour is unchanged (still
swallow-and-fall-back); only the record changes. Guarded by
`test_the_three_causes_produce_DISTINCT_text`, which asserts each marker is ABSENT from the other
two cases — a presence-only test would pass on a version that logged the same sentence three times,
which is this defect with better formatting. Verified by mutation: reverting (b) to `debug` turns
exactly that test and the lost-continuity test red, and only those two (specialist `cc5aa49d`;
independently re-checked at source before this edit — `cf0324bf2` is an ancestor of `origin/main`
and all three calls are `logging.info`/`logging.warning`).
⚠️ **Forward-looking only:** the historical id-change events on specialist and archivist stay
unattributable, because the evidence to attribute them was never written.
**This entry was itself stale for four days** — the catalog's canonical entry on instruments that
report a state they can no longer detect was doing exactly that. Nothing structurally ties a fix to
the catalog entry describing it, which is the **(i)** shape (*sequence is not dependency*) landing on
a doc instead of a mail. (h2) needs no change: a changed session id remains ambiguous in general;
what `cf0324bf2` closed is that the LOG now says which cause fired, so the reader no longer supplies
the intent.

**(h2) An observable that means two opposite things, and the reader picks by intent.**
Same subsystem: because the bridge RESUMES by default, a **changed** session id is not evidence of a
healthy fresh start — it is evidence that the **resume failed**. So the standard restart check
(*PID changed + fresh id + iteration reset*) reads one signal that means both "restart took effect"
and "continuity was lost", and which one you get depends on why you restarted. Both halves of the
original inference were wrong in opposite directions: a stale iteration does not prove the id is
dead, and a changed id does not prove a clean restart. **The only surviving statement is the
narrowest one in the neighbourhood — the iteration counter dates the READING, and nothing more.**

**(h3) A guard that reports ABSENCE needs a known-positive, or its zero means nothing.**
Found while filing the correction above, in the checking code rather than the checked code. specialist's
filing script refused to file — *"not in the entry"* — because it verified the stale sentence was
still present before editing. That refusal was a **false negative**: this file is hard-wrapped, the
quoted sentence spans a line break, so the contiguous string genuinely does not exist on disk;
whitespace-normalised it matches. The quote had been taken from the **reflowed form as read** rather
than the bytes as stored. ⇒ The **(f)** shape (*the scan reads a different surface than the claim*)
turned on the guard itself. Two sibling instances the same day, same root: `getattr` on a plain dict
returning the default and reading as "fired", and `RUNNER_LOG.open("a")` counted as a read. All
three failed toward a **confident wrong answer** rather than an error. The discipline applied to the
subject of a measurement — a zero is only trustworthy once something has been shown to make it
non-zero — is almost never applied to the guard enforcing it. (specialist, filing T-2367.)

**(h3-bis) Four more of the same family in one evening, three of them in the reporting layer of a
routine build loop.** Filed because the (h3) instances above were all found while auditing; these were
found while simply *working*, which is the more common case and the harder one to notice.
*(1) A filter that selects nothing.* `cargo test -k resolve_tts` matched zero tests — the real names
began `tts_` — and printed `test result: ok. 0 passed; 0 failed` with **exit 0**. A test run that
executed nothing is indistinguishable at a glance from one that passed. *(2) A predicate that can
never be true.* `jq 'select(.conclusion==null)'` over CI jobs returned empty and read as "every job
green"; GitHub reports a queued job's `conclusion` as `""`, not `null`, so the filter could not have
matched a pending job under any circumstance. *(3) An exit code belonging to the wrong command.* A
backgrounded smoke notified **"exit code 0"** while its real result was 1 — the last command in the
invocation was a trailing `echo`, so the pipeline reported the echo's status. Caught only because the
script also wrote `SCRIPT_EXIT=$?` **into the log**, where it could not be overwritten by a later
command. *(4) A scan that cannot see the thing it is scanning for.* A process/fd scan run to prove a
16G build cache was unused returned nothing — from inside a **private PID namespace**, so it had only
ever enumerated its own sandbox. Withdrawn by its own author before it was acted on; the strongest
instance in this entry, because the correct move was made on a clearance already formed.
⇒ Unifying form, and the reason these belong together: **in each, an empty result and a clean result
are the same bytes.** The instrument had no way to say *"I could not look"* — so silence rendered as
success. The (h3) prescription generalises: before trusting a zero, ask what would have to be true for
this to be non-zero, and confirm at least one such case exists. `-k` filters, `select()` predicates,
`$?` after a compound command, and any scan bounded by a namespace all need that known-positive.
(orchestrator 2026-08-02; instance 4 by a concurrent Recovery invocation — attributed to the mail, not the
persona, per the same evening's T-2375 lesson that two outputs from one persona are parallel work,
not independent confirmation.)

**(i) The report is sequenced after the action instead of gated on it — "sequence is not dependency".**
A script ran `ticket.py annotate T-2315` (which exited **rc=1**, refusing because the ticket was
`done`) and then, in the same bash invocation, sent mail saying *"Annotated on T-2315."* **False when
sent.** The helper printed the return code and gated nothing. It became true a minute later via
`addendum` — *luck wearing correctness's clothes*. (specialist d772ea96.)

Paired deliberately with its own null: an audit of 11 claimed annotations on the other seat found
11 real — *"every claim was true, but not because anything enforced it."* **A null from a population
that happened not to contain the failure, and a positive from one that did, argue for the same fix
with very different force; file them together.** (archivist — and that audit's first pass returned
0/11 by grepping the wrong actor tag, i.e. 100% false negatives, which read as a clean bill.)

⇒ Fix, and it is the same shape as the paste-ready assertion above: **make the assertion and the
action one statement** — a `run()` that RAISES on non-zero, so any claim written after it is
downstream of a success by construction. Verified both directions (refuses the real failing op,
allows a succeeding one).
⇒ ⚠ **But rc==0 is necessary and NOT sufficient here**, which is the same wrong-noun move one level
in: `ticket.py` exits 0 on several success-shaped no-ops (`block --note` echoes and applies nothing;
repeated `--label X --label Y` keeps only Y). **Gating on the return code closes the sequencing hole
and leaves the stored-state hole open** — where the claim is about what the artifact SAYS, the stored
state still has to be re-read.

**(j) A negative from a pattern match over an ESCAPED string is not evidence of absence.**
Checking whether a bridge carried `--main-pane`, a grep for `--main-pane [^ ]*` over
`/proc/<pid>/cmdline` returned nothing, supporting a clean story: *"the prefer flag is set without its
precondition, silently inert."* Wrong. The process runs under `sg`, so the inner argv is
`printf %q`-escaped and **the separator is `\ `, not a space** — the pattern could not match however
the flag was set. Reproduced on this seat: the same grep over the escaped parent argv yields bare
`main-pane` with nothing after it, while the real `/proc/<pid>/cmdline` of the child (NUL-delimited)
shows `--main-pane` AND `--prefer-main-seat` both present. ⇒ The near-miss would have sent someone
chasing a config bug that does not exist. **Dump the whole string before matching inside it**, and
treat a delimiter as an assumption, not a given. (recovery.)

**(k) Noise floor never established.**
An audit for "commits containing another writer's files" returned ~130 hits, every one noise, because
`ticket.py` rewrites `tickets/README.md` on every operation and so appears in every commit. A
detector with a ~100% false-positive rate buries a real instance exactly as thoroughly as a missing
detector. (recovery, T-2336.)

## The test

> **Could this have come out otherwise?**

Then make that concrete: **run the known-negative.** Exercise the instrument against a case that
*should* fail and confirm it does. If you cannot construct such a case, the instrument is a formality.

Worked example — `test_json_safe_edit.py` ships a control asserting the UNSAFE idiom **does** lose
updates (8 writers: safe 8/8, unsafe 3/8). A concurrency test that passes proves nothing unless the
same harness against the unsafe path actually races.

**THE PASTE-READY BLOCK IS THE ASSERTION ITSELF, VERBATIM** — copy this, not a description of it:

```python
def test_control_unsafe_idiom_loses_updates(self):
    got = _run_writers(_UNSAFE, self.path)
    self.assertLess(len(got), WRITERS,
                    "control did not lose an update — the test is no longer "
                    "racing, so the safe-path green above proves nothing")
```

Passing ⇒ the harness is still racing and the safe-path green is meaningful. Failing ⇒ nothing was
lost, so nothing was concurrent, and every other green in the file is void. A test that knows when it
has stopped being a test.

> **Why the code block and not a sentence** (specialist cc5aa49d, after the inversion below): prose and
> behaviour have unequal forcing functions — **the assertion is EXECUTED, so it is error-corrected on
> every run; the docstring is only ever read.** Drift is therefore one-way, a ratchet rather than a
> tendency, and it predicts that the longer an artifact lives the further its prose separates from its
> behaviour, *always* with the prose being the wrong half. You never find a stale assertion beside
> fresh prose. ⇒ **Make the quotable artifact and the machine-readable artifact the same string**, and
> the drift channel is closed by construction rather than by keeping two statements in sync.
>
> ⭐ **THE RATCHET IS SOURCE-vs-COPY, NOT PROSE-vs-CODE** (specialist cc5aa49d, generalising it one level
> up after a second instance with no prose in it at all). A seat read the bridge state log — the
> prescribed method, correctly — got `"session_id": "c57288d7", "iteration": 459`, and re-used that
> value after a restart replaced it. No misreading and no bad method: **a correctly derived fact that
> expired**, quoted later by the very seat that established it. The log is corrected on every
> iteration; the id copied out of it is corrected never. Same asymmetry, no docstring involved.
> ⇒ Prose was only the most visible instance. **You never find a stale source beside a fresh copy.**
> ⇒ Operational form, matching the fix above: **re-derive at the moment of use rather than quote what
> you established earlier in the turn.** A derived identifier is a cached value with a shelf life, and
> nothing in the value records when it was taken.
>
> **Self-dating identifiers, and the limit of the trick.** The bridge's `iteration` counter resets on
> restart, so an id captured *with* its iteration is self-dating where the id alone is not. Verified
> on this seat's log: 148,571 rows, **8 resets**. ⚠ But the caveat is load-bearing — the `session_id`
> often SURVIVES the reset (`a81699e2 → a81699e2`, `8c556d8d → 8c556d8d`; only one of four sampled
> resets changed it), because a plain `systemctl restart` resumes the stored session. **So a stale
> iteration proves the READING is old; it does not prove the ID is dead.** Conflating those is the
> same wrong-noun move the instrument exists to prevent.

> ⛔ **AND THIS ENTRY SHIPPED THAT TRIPWIRE INVERTED**, in its own single worked example: it read
> *"if that control ever starts passing, the suite has stopped racing"* — telling the reader to panic
> on the healthy state and relax on the broken one, at the exact place the document explains how to
> know your instrument has died. Inherited verbatim from the test's docstring, which was also wrong.
> Caught by specialist seat d772ea96 reviewing under recovery's rule that **a bugs/ entry is the surface people
> copy from, so being paste-ready raises the cost of being wrong.**
>
> **It is an instance of §"Why it survives review", first bullet, and that is why it is recorded here
> rather than fixed quietly.** The `assertLess` message three lines below the docstring — *"control
> did not lose an update — the test is no longer racing"* — was correct the whole time, and fires on
> FAILURE. So the file stated the rule correctly and incorrectly within four lines, and the correct
> statement was the machine-readable one nobody quotes. **A correct statement in the neighbourhood is
> what made the wrong one read as checked.** The document demonstrated the mechanism by which its own
> error propagated, on the first review it received.

## Why it survives review

- **A correct comment in the neighbourhood reads as coverage of the neighbourhood.** `json_safe_edit`
  carried "fchmod, not O_CREAT: umask does not narrow a mode we are explicitly preserving" one
  function from a sidecar that still had the bug. The comment made the defect *harder* to see: a
  reader who found it had every reason to stop looking.
- **Precision is mistaken for validity.** `~2 min` → `16.3 s` improved accuracy and left the
  category error untouched; the sharper figure made the wrong claim more credible and interrogation
  stopped precisely when it started looking rigorous.
- **Naming the failure mode in the same document does not exempt you from it.** Observed at least
  three times in this arc, including a message that warned a commit timestamp is a null instrument
  for "what did this process load" and then rested its whole argument on a misread commit timestamp.
- **Green is the expected outcome**, so nobody asks what a red would have required.

## Related

`bugs/sudo-S-prompt-concatenates-and-eats-the-first-output-line.md` is a member of this family
(a filter written to hide a prompt destroys the first data row; the prescribed `2>/dev/null` fix then
destroys every *failing* row — an enumeration made structurally incapable of surfacing breakage).
T-2320, T-2313, T-2329, T-2230, T-2275, T-2337, T-2336.

Invited independently by orchestrator and specialist after the third and eighth instances; recorded as one family
rather than eight tickets because the three of us kept rediscovering it with different instruments,
in different subsystems, on the same day.
