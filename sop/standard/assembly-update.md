# Assembly update — moving one assembly's lock to a new tested commitment

**Status:** Drafted 2026-08-26 by Orchestrator (HO) per T-3180 D2-writer contract; implements ORG-055 (`architecture/adr/ORG-055-project-repository-realization-and-compatibility-catalogue.md`). Rev-2 addresses T-3180 REBOUND (codex 2026-08-26). Awaiting the operator ratification.

## When to use this

When an **assembly** (a multi-repository composition expressed as a hand-authored manifest + a generated lock) needs to change: a component version bumps, a component enters or leaves, a compatibility range narrows, a provider retires, or a candidate feature adopts a new capability. The manifest expresses a compatible possibility; the lock expresses **one exact tested commitment**. This SOP moves the lock from the current commitment to a new one under transaction discipline.

**Do NOT use this SOP to**:
- **Hand-edit the lock** — a hand-authored lock is invalid by ORG-055's third locked acknowledgment. The lock is generated from the manifest + resolved candidate.
- **Add a component to the catalogue** — that's a component-record write (which routes through `component-catalogue-upkeep.md` or a direct component-record edit).
- **Extract a component into its own repo** — that's `repository-extraction.md`.
- **Decommission a capability** — that's the feature-usage SOP (operator-gated).

## Procedure

The update is a **transaction with a single atomic commit point** that publishes lock + immutable receipt together. Every step before commit is reversible without observable consequence; every step after commit falls back to the prior exact lock. The commit is one atomic event, not two writes — see "Crash semantics" below.

1. **Validate the current state**:
   - The current manifest parses cleanly.
   - The current catalogue's component records resolve.
   - The current lock matches the current manifest (re-verify with `verify_generated_lock`).
   - **Preserve the prior exact lock + receipt PAIR** — snapshot BOTH the current lock bytes AND the current receipt (the immutable receipt written by the prior transaction that produced this lock) under a stable rollback-only reference the transaction's subsequent steps cannot touch. The pair-snapshot is what step 7 restores; a lock-only snapshot cannot restore the authoritative pair.
   - Alternative pair-binding: if the receipt store already indexes receipts by lock-digest (immutable-receipt lookup by `current_lock_digest`), the snapshot MAY reference the current lock digest + the receipt-store lookup key instead of copying the receipt bytes. Either shape is acceptable; what matters is that step 7 restores the exact pre-transaction pair, not just an older lock derivable from a receipt's `prior_lock_digest` (which points to the pre-PRIOR lock, not the pre-CURRENT one).

2. **Resolve a deterministic candidate lock**:
   - Feed the manifest + component records + resolver + evidence into the resolver.
   - The candidate lock is deterministic in its inputs — same manifest + same catalogue + same evidence produces byte-identical bytes.
   - Any nondeterminism (timestamp injection, remote lookups on non-frozen refs) is a bug and refuses here.

3. **Ask T-2951 for a no-mutation lifecycle plan** for the candidate. T-2951 refuses on:
   - **Dirty** working tree in any candidate checkout.
   - **Diverged** from the expected upstream (local commits not in the resolved commit's ancestry).
   - **Foreign** clone (wrong logical identity).
   - **Rewound** (upstream has moved backward — permanent-vs-transient self-heal decides per ORG-046).
   - **Wrong identity** (clone URL matches an obsolete authority).
   - **Missing ref** (candidate points to a commit the clone cannot reach even after `fetch`).
   - T-2951 returns a NO-MUTATION plan; do not skip this step even for "obviously safe" updates.

4. **Stage exact candidate sources/artifacts without publishing the lock**:
   - Materialize the candidate checkouts / artifacts to their staging paths.
   - Do NOT overwrite the current lock file yet.
   - Do NOT touch the current running assembly.

5. **Run all declared cross-component smoke and health contracts**:
   - Each assembly-level smoke contract fires against the staged candidate.
   - Each component's declared health contract fires clean.
   - Compatibility gates fire: capability, contract, schema, feature, authority, privacy/data, hardware/network, cost, owned operational obligations. A refusal at any gate is a `CompatibilityRefusal`; the transaction ABORTS with the refusal recorded and rolls back with no publication.

6. **Publish candidate lock + immutable receipt as one atomic commit** — the sole commit point:
   - The publish operation writes BOTH the new lock bytes AND the immutable receipt as a single atomic transaction — either both are on disk together or neither is. This is the invariant; the implementation plan (T-3182) owns the exact mechanism (durable-rename staging directory, journaled write, or equivalent CAS) that guarantees it.
   - The receipt names: manifest digest, candidate lock digest, resolved commits per component, evidence digests, gate-pass records, prior lock digest (rollback address), operator identity + timestamp.
   - The receipt is append-only after commit; a subsequent update writes its own receipt but does not modify prior ones.

7. **On post-mutation failure, restore + retain diagnostics**:
   - Restore the **prior exact lock** and prior source/artifact set from the snapshot in step 1.
   - Rerun prior smoke + health to prove restoration succeeded.
   - Retain candidate diagnostics (staged trees, gate outputs) under a stable diagnostic path — do NOT delete them; they are the evidence for the failed transaction.
   - Record the failure in the assembly's health-evidence obligation.

## Crash semantics

The commit invariant from step 6 is: **lock + receipt are one atomic event; the "lock published but receipt absent" state is disallowed by design**. The implementation plan (T-3182) owns the mechanism that enforces this; this SOP does not admit that state as recoverable because it should not be reachable.

Crash windows resolve as follows:

- **Before step 6** — no lock change; the running assembly is untouched. Restart the transaction from step 1.
- **During step 6** — the atomic mechanism guarantees all-or-nothing. On restart, the lock is either the prior version (transaction failed cleanly, restart from step 1) or the new version + receipt together (transaction committed cleanly, proceed to post-commit verification).
- **After step 6** — the transaction is committed. A subsequent operator can verify the assembly matches the receipt and re-run health confirmation. Post-mutation failure at step 7 has separate rollback semantics (restore prior lock/source from the step-1 snapshot); it is NOT the same as a step-6 crash.

If an operator ever finds a state where step-6's invariant is violated (published lock exists but receipt does not, or vice versa), the invariant has been broken by a bug in the implementation. Do NOT paper over by backfilling the missing artifact from in-flight state — bless nothing that lacks its authoritative pair. Refuse to proceed, restore the pre-transaction lock+receipt pair from the step-1 snapshot (which pins the exact pair, per step 1), and file a defect against the implementation. Do NOT fall back to the most recent prior receipt's `prior_lock_digest` — that identifies an OLDER lock (the one that preceded the current one), not the current pre-transaction pair. The mechanism gets fixed; the SOP does not learn to tolerate the broken state.

## Cross-repo change ordering

When an assembly update spans multiple repositories with a shared contract:

- **Compatible provider first, consumer second** — the provider ships the compatibility-preserving change first, all consumer locks adopt it, then the range narrows or the old provider retires.
- **Intentionally atomic incompatibility** — a change that cannot be staged compatibly (breaking schema change, capability removal) uses **one staged candidate + one rollback unit** spanning all affected components. All-or-nothing publication.
- **Branch timing is NOT compatibility evidence** — the fact that a provider and consumer merged their PRs on the same day does not make the change compatible. Only the gates in step 5 prove compatibility.

## Nested checkouts and submodules

A lock entry MAY materialize as a nested checkout or submodule view. That is a **presentation choice**, not dependency truth. The manifest and lock remain the authority; the nested view is a convenience that must never be edited independently of the lock update transaction. A submodule SHA disagreeing with the lock fails; the view updates from the lock, never the reverse.

## Common failure modes

- **Skipping step 3** — the operator "knows" the checkouts are clean and skips T-2951's plan check. A dirty tree then gets clobbered by step 4. Fix: never skip T-2951; the plan check is cheap.

- **Treating step 6 as two writes** — an operator reads "publish lock, then write receipt" and implements it as sequential file writes. That leaks a broken state on crash. Fix: publish is ONE atomic operation; the implementation plan (T-3182) specifies the mechanism (durable-rename staging directory, journaled write, etc.) that guarantees it. Do not weaken the invariant.

- **Backfilling a missing receipt from in-flight state** — after a crash reveals a published lock without a receipt, an operator reconstructs the receipt from staged evidence still on disk. This blesses an unreceipted commit. Fix: refuse the state; restore the prior lock/receipt pair; file a defect against the mechanism that permitted the state.

- **Hand-editing the lock to unblock the transaction** — a compatibility gate refuses; the operator edits the lock to work around it. This produces an invalid assembly. Fix: NEVER hand-edit; fix the refusal or roll back.

- **Rollback path lost between steps** — the prior lock snapshot in step 1 is overwritten before step 6 commits. Recovery on failure becomes impossible. Fix: snapshot goes to a rollback-only address that step 4 cannot touch.

- **Cross-repo update in the wrong order** — consumer switches to the new provider capability before the provider's compatibility-preserving version ships everywhere. Consumers on old providers break. Fix: provider-first, consumer-second, then narrow.

- **Nested checkout edited independently** — an operator commits into a submodule view thinking they've updated the assembly. The lock is unchanged; the change disappears on the next transaction. Fix: all assembly-affecting edits go through the manifest + this SOP.

## Boundary

This SOP covers **one assembly's lock transaction**. It does NOT cover:

- **Manifest authoring** — the shape of a new manifest, which components to compose. That's a design decision recorded elsewhere.
- **Component-record content** — the record itself is maintained per `component-catalogue-upkeep.md`.
- **Boundary decisions** — captured in `RepositoryBoundaryDecision` records per `repository-extraction.md`.
- **Feature-usage decisions** — operator-gated per the existing feature-usage SOP.
- **The atomic-commit mechanism itself** — T-3182 (implementation plan) specifies the exact CAS/journaling/rename technique. This SOP names the invariant the mechanism must uphold.

## Cross-references

- `architecture/adr/ORG-055-project-repository-realization-and-compatibility-catalogue.md` — the ratified authority for assembly semantics.
- `architecture/adr/ORG-061-*.md` — owned obligation index (health evidence, review cadence refs live here).
- `sop/standard/repository-extraction.md` — how components enter the catalogue.
- `sop/standard/component-catalogue-upkeep.md` — how the catalogue's records stay current.
- `sop/standard/feature-usage-monitoring-and-pruning.md` — where capability decommission decisions route.
- T-2951 — checkout lifecycle authority consumed in step 3.
- T-3182 — assembly manifest/lock/resolver implementation plan; owns the atomic-commit mechanism this SOP requires.
- `architecture/adr/ORG-046-*.md` + T-1996 — logical repo resolver contract (adapter seam).
- `tickets/plans/T-2415-implementation-plan.md` — parent plan authority.
- D5 (smoke verification) — the gate discipline of step 5.
- P4 (traceability) — the receipt-immutability principle of step 6.
