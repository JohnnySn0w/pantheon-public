# Repository extraction: moving existing code into its own repo

**Status:** STUB, not yet run (the operator TG 11309-11316, 2026-10-07: written ahead of need, so it stays a stub until the first real extraction, which writes the procedure; see `writing-sops-from-first-use.md`). The 2026-08-26 detailed draft (ratified briefly 2026-10-07, then demoted) is at `git show af90b4bbd:sop/standard/repository-extraction.md`. Treat it as notes, not procedure. **Authority:** ORG-055.

## When to use this

Existing code that already lives inside another repo needs to move into its own repository, because it passes both of ORG-055's boundary tests:

1. **`contract_ready`**: a named, versioned, testable contract exists (or lands with the move).
2. **At least one strong `independence_signal`**: separate release or rollback cadence, a separate authority or secret boundary, separate stewardship, reuse across assemblies, or a different runtime or trust zone.

"The repo feels too big", fast interface churn, and changes that normally land together are reasons to keep it together, not to extract.

Not for a brand-new project (`new-project-bootstrap.md`), and not for re-hosting or a remote move.

## Invariants known now

- **The decision record comes first.** The boundary-decision record (`schemas/repository-boundary-decision.schema.json`, no invented fields) lands before the migration commit; it is the authorization to move.
- **Audit before the first push.** Secrets that reach a remote cannot be reliably removed, so scan the extracted history before the destination repo is ever pushed.
- **Narrow history.** Carry only the extracted paths' history (`git filter-repo --path …`), and verify the result.
- **Consumers move in the same transaction.** No window where the source is cut and a consumer still imports the old path.
- **Rollback is multi-repo.** Restore the source first, then the consumers, then decommission the destination; it is never "one git revert".
- **Cadence lives in ORG-061.** Re-evaluation triggers go in the obligation index, not in this SOP or the record.

## First run: record the actual procedure here

*Empty until the first real extraction.* Whoever does it replaces this section with the steps actually taken, the commands that worked, and what surprised them. Then the status line flips from STUB to a normal status, and the operator ratifies at surface time (ORG-010).

## Cross-references

- `architecture/adr/ORG-055-project-repository-realization-and-compatibility-catalogue.md`: authority.
- `sop/standard/new-project-bootstrap.md`, `sop/standard/assembly-update.md`, `sop/standard/component-catalogue-upkeep.md`: neighbouring procedures.
- `sop/standard/writing-sops-from-first-use.md`: why this is a stub, and how it gets filled.
