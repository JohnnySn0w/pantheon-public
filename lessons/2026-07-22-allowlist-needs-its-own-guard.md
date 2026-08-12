# An allowlist is a deliberate hole — it needs its own guard, or it silently widens

**Date authored:** 2026-07-22 (articulated by Metis at T-1930 closeout; lifted to a lesson by Orchestrator/HO)
**Author:** Orchestrator (Orchestrator), crediting Metis (suborch1)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015)
**Re-evaluate when:** a second guard ships an allowlist/exception mechanism without self-policing and a stale entry silently widens the hole — that recurrence promotes this from "articulated once" to "load-bearing."

## The pattern

Any allowlist / exception mechanism — a lint allowlist, a skip-list, an ACL exception, a "known-flaky" test list, a firewall permit rule — is a **deliberate hole in a guard**. The hole is correct when created. But without a guard *on the allowlist itself*, it **silently widens over time**, because a stale entry **fails nothing**:

- An allowlisted path that no longer exists → the guard skips a file that's gone; if a *new* file takes a similar role, it's unchecked and nothing errors.
- An entry whose justification has lapsed (the "temporary" exception that outlived its reason) → still honored; the hole is now unjustified but invisible.
- A debt-marker entry (a known-violation allowlisted "until fixed") whose tracking ticket closed or was renumbered → the debt is now permanent and unlabeled.

The failure is **invisible by construction**: nothing throws, no test goes red, coverage just quietly shrinks. This is the wrong-layer-GREEN family (`2026-07-21-wrong-layer-green-liveness-checks.md`) one turn in — the guard reads clean *because nothing checks the hole*.

## Grounding

T-1930's `pgrep_predicate_lint` allowlist. Rather than trust the allowlist as prose, Metis made it **unable to lie** with three meta-tests, in the same change that created it:
1. every allowlisted path still **exists** (a stale path is a widened blind spot);
2. every entry's **reason is substantive**, not a bare label;
3. every **KNOWN-VIOLATION debt entry cites a `T-NNNN` ticket** — the meta-test asserts the citation FORMAT (a `T-\d{4}` ref + the "not a blessing" marker), which stops an entry being added without a tracker. It does NOT yet read ticket state: a ticket resolving while the entry survives passes silently — the exact widening-hole failure this lesson is about, caught by Metis auditing this Grounding claim at source. T-1958 strengthens the meta-test to fail when the cited ticket is already in `done/` (turning hoped-for removal into enforced removal); until it lands, entry-vs-ticket liveness is a manual grooming responsibility.

## The rule

When you add an allowlist / exception mechanism, add its guard **in the same change** — never "we'll audit it later" (retrofitting is near-impossible once entries accumulate, and by then the hole has already widened). At minimum, test that:

- every entry's target still exists (stale target = widened hole);
- every entry carries a present, substantive justification (distinguish a *justified exception* from a *tracked defect* — the reason field is what keeps a debt marker from reading as a blessing, per the T-1945 curated-not-auto-derived rule);
- any "temporary" / debt entry references a tracking ticket, so its removal is enforced rather than hoped-for.

Cheap at authoring time, invisible-until-costly if skipped. The general form: **a hole you punch in a guard is itself a thing that must be guarded.**

## Scope caveat — the guard must live where the target is checkable (Metis sweep, 2026-07-22)

The "every target still exists" test only works when the allowlist names **repo-relative** targets a
repo test can stat. An **account-scoped** allowlist (e.g. `HOOK_ALLOWLIST` in
`context_fill_monitor.py`, whose entries are basenames of per-account files behind the T-1733 ACL
model) is structurally un-checkable from a repo test — cross-account reads are denied, correctly.
The per-scope variant of the rule: a repo-scoped allowlist gets a repo-test guard; an account-scoped
allowlist must be guarded **where the runtime checker already runs** — per account, with that
account's own visibility. This is why HOOK_ALLOWLIST has a membership test (T-1945) but no staleness
test: staleness there is not repo-checkable, and asserting otherwise would have produced false
findings (Metis's sweep initially flagged three entries as stale; one was a wrong-directory
assumption caught before reporting). Sharper corollary for basename-matched allowlists: a stale
entry doesn't just tolerate a missing file, it **pre-blesses that basename from any path** — see
the Recovery-lane ticket filed from the same sweep for the per-account guard shape.

## Cross-references

- `lessons/2026-07-21-wrong-layer-green-liveness-checks.md` — parent family (clean-because-the-check-can't-see-the-failure).
- `lessons/2026-07-21-cmdline-substring-process-predicates-unsound.md` — the lesson T-1930's lint enforces; its allowlist is the grounding case.
- T-1945 (curated-not-auto-derived allowlists) — the sibling rule this extends.
