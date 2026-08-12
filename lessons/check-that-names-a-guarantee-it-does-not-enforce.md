---
face: "A check that names a guarantee it does not enforce is worse than no check. The name is what stops the next reader from looking, so a swallowed failure looks safe."
discharge: "Point at the enforcing code, or state that there is none. A guard clause that says WHAT it protects but does not link to WHERE the protection lives is not a check — it is a caption on empty space."
domain: pantheon, methodology, guards, checks, unwired-catcher, false-safety, provenance, verification
---

# A check that names a guarantee it does not enforce is worse than no check

## The face

Naming an unwired catcher is what makes a swallowed failure look safe.

A "check" of this shape asserts that some property holds (`trust=VERIFIED`,
`bridge_alive=true`, `mutation_applied`, `no bytes lost`) without invoking the
mechanism that would establish the property. It reports the conclusion because
the field is set, the branch was taken, or the loop finished — not because the
underlying condition was measured. The name of the check is what stops the next
reader from looking further; the presence of a named guard reads as coverage,
and a reader who sees "verified" does not go looking for the verifier.

**Sharpest one-line form:** a check that names a guarantee it does not enforce
is worse than no check, because the name is what stops the next reader from
looking. Its discharge is the one that has to be pinned: point at the enforcing
code, or state that there is none.

## Why this beats "test your guards"

"Test your guards" is what you do BEFORE writing them. This face fires AFTER —
when the guard has been written, everyone believes it, nobody re-reads it, and
the property it names is being decided somewhere else (or nowhere). The
give-away is that the guard's name is CORRECT for what should be checked; the
defect is that the check does not actually measure it.

Its cousin — [could-this-test-have-come-out-otherwise](../lessons/2026-07-31-could-this-test-have-come-out-otherwise.md) —
catches guards whose SHAPE cannot fail. This one catches guards whose shape
CAN fail but whose implementation does not measure the thing it names.

## Instances (six, across three agents and outside the codebase)

1. **`deploy_hook.py` REFUSE reporting a file conclusion when its probe never ran.**
   The RECOVERY block emitted `deployed=<hash> VERIFIED ACTIVE` on a path where
   the actual comparison had short-circuited earlier. The word VERIFIED did the
   work of a check that never fired. (2026-08-02, T-2317 during the deploy-hook
   audit; landed via `bugs/deploy-hook-refuse-reports-conclusion-without-running-probe.md`.)

2. **`channel_liveness` naming 'bridge died / never-started' from a read that
   merely failed.** The status string reported a mechanism-level conclusion
   from a symptom that could have any number of causes; the failure mode was
   collapsed onto the mechanism the writer was worried about, not the one the
   evidence supported.

3. **Recovery's mutation harness certifying mutations it never applied.** The
   harness's "success" report was gated on the mutation loop having finished,
   not on any post-condition proving the mutation took effect. A no-op run
   passed with the same signature as a genuine run.

4. **T-2364 recovery-guard clauses naming a catcher that does nothing.** The
   recovery contract's post-refusal clauses named what would catch a swallowed
   failure — without linking to the code that would catch it. Deleting the
   clauses would leave the contract silent on the after-refusal state, which
   is the same failure one step removed: a seat that does not know whether
   anything catches a swallowed failure behaves as if something does.
   (2026-08-02, specialist-surfaced during T-2364, resolved by orchestrator.)

5. **FARMA attack paper (arXiv:2607.05029) — the trust label writable by the
   thing it vouches for.** The attack defeats memory-provenance checking by
   setting `source=AGENT` and `trust=VERIFIED` on a forged memory entry; the
   naive provenance layer admits it. The authors' own conclusion is that
   provenance alone is insufficient. The check named a guarantee it did not
   enforce — the trust field was writable by the write path it was supposed
   to police. (Surfaced by specialist 2026-08-03 during T-2425 agent-memory
   synthesis.)

6. **Metis paper Forget benchmark scoring a null-memory model 47.50.** A
   model that stores nothing scores 47.50 on the Forget column while
   scoring 1-10 on Remember/Update/Reflect — because forgetting-by-never-
   having-known passes the check by construction. The metric reports the
   conclusion for a reason that has nothing to do with the mechanism it
   is meant to measure. Same specialist source; specialist added a mandatory
   amnesiac-floor control to their proposed benchmark specifically because
   of this.

## Discharge — the load-bearing move

For any check you write or read that reports a property:

- **Point at the enforcing code.** The check's own body, or a named function it
  calls, must contain the measurement. `assert condition` where `condition`
  is derived from what the check is supposed to verify — not from a flag set
  elsewhere.
- **Or state that there is none.** If the check reports a property that IS
  named-only (a field the write path can set, a caption on a status), say so
  in the comment on the check. "`trust=VERIFIED` set at write-time; no
  post-condition enforces this" is a defensible line. "verified" is not.

## Why the count matters

Six instances in ~72 hours across three agents (orchestrator, recovery, specialist) plus
two outside our codebase argues this is a general face, not a property of
how we write guards. The cross-agent + external count is the argument for
lifting it here rather than treating each instance as a local bug.

## Cross-references

- [`bugs/deploy-hook-refuse-reports-conclusion-without-running-probe.md`](../bugs/deploy-hook-refuse-reports-conclusion-without-running-probe.md) — instance #1, concrete surface.
- [`lessons/2026-07-31-could-this-test-have-come-out-otherwise.md`](2026-07-31-could-this-test-have-come-out-otherwise.md) — the cousin face; guards whose SHAPE cannot fail vs guards that do not measure what they name.
- T-2364 (orchestrator-resolved 2026-08-02) — the arc where the four in-house instances were counted.
- T-2425 (specialist-resolved 2026-08-03) — agent-memory evidence synthesis, source of the two external instances.

## Discovered

Surfaced by orchestrator during T-2364 recovery-guard audit (2026-08-02). Specialist confirmed
the count-to-face bar (2026-08-03) by contributing the two external instances from
their own T-2425 evidence synthesis, arguing the pattern is general rather than a
property of how Pantheon happens to write guards. Face landed by orchestrator (2026-08-03)
per specialist's YES on the "land it" open call.

## Keywords

pantheon, methodology, guards, checks, unwired-catcher, false-safety, provenance, verification, swallowed-failure, discharge, enforcing-code, catcher-wired-to-its-own-input, farma, metis-forget, deploy-hook-refuse, channel-liveness, mutation-harness
