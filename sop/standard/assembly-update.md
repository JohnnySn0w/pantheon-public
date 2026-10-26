# Assembly update: moving one assembly's lock to a new tested commitment

**Status:** STUB, not yet run (the operator TG 11309-11316, 2026-10-07). The tooling exists (`tools/assembly_resolver.py`, `tools/assembly_transaction.py`), but no assembly exists yet, so the first real update writes the procedure; see `writing-sops-from-first-use.md`. The 2026-08-26 detailed draft (ratified briefly 2026-10-07, then demoted) is at `git show af90b4bbd:sop/standard/assembly-update.md`. Treat it as notes, not procedure. **Authority:** ORG-055.

## When to use this

An **assembly** (a multi-repo composition: a hand-authored manifest plus a generated lock) needs to change. A component bumps, a component enters or leaves, or a compatibility range narrows.

Not for adding a component to the catalogue (`component-catalogue-upkeep.md`), extracting a repo (`repository-extraction.md`), or decommissioning a capability (operator-gated).

## Invariants known now

- **The lock is generated, never hand-edited.** If a compatibility gate refuses, fix the cause or roll back; don't edit the lock around it.
- **One atomic commit point.** The new lock and its receipt publish together, in one operation. A lock without its receipt is a defect to roll back, never to backfill.
- **Snapshot the prior lock + receipt pair first,** somewhere the update can't overwrite, so rollback is always possible.
- **Check the checkouts before touching them.** Run the T-2951 no-mutation plan (it refuses dirty, diverged, foreign or rewound clones), even when it "looks safe".
- **Cross-repo order: provider first,** then consumers, then narrow the range.
- **Edits go through the manifest.** Committing inside a nested checkout doesn't change the assembly.

## First run: record the actual procedure here

*Empty until the first real assembly update.* Whoever does it replaces this section with the steps actually taken, the commands that worked, and what surprised them. Then the status flips from STUB, and the operator ratifies at surface time (ORG-010).

## Cross-references

- `architecture/adr/ORG-055-project-repository-realization-and-compatibility-catalogue.md`: authority.
- `tools/assembly_resolver.py`, `tools/assembly_transaction.py`: the existing machinery.
- `sop/standard/repository-extraction.md`, `sop/standard/component-catalogue-upkeep.md`: neighbouring procedures.
- `sop/standard/writing-sops-from-first-use.md`: why this is a stub, and how it gets filled.
