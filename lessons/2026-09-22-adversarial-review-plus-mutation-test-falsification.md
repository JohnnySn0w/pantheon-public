# Adversarial-reviewer + mutation-test-falsification as a load-bearing pre-merge discipline

Filed 2026-09-22 (orchestrator) after 4 artisan-authored arcs this session (T-3182, T-2951, T-3183, plus the T-3543 haiku audit) surfaced 6 load-bearing bugs pre-merge via the same review discipline — a real methodological win worth persisting.

## The pattern

For a build arc (implementer produces working code + passing tests), the orchestrator runs an **ADVERSARIAL REVIEWER** as a separate step before handback, whose job is not to confirm correctness but to actively look for silent-defensive-defaults, silent-wrong-branch behavior, silent-latest-not-pin, silent-rmtree-of-dirty, silent-partial-commit — the class of bug where the code appears to work + tests appear to pass but a critical guard defaults unsafe.

Every fix the reviewer surfaces is then **MUTATION-TEST-FALSIFIED**: neuter the fix (make it a no-op), re-run the specific test that supposedly covered it, verify the test now FAILS. If neutering the fix doesn't break the test, the test was hollow — the fix landed but the coverage was vacuous. This forces every reviewer-caught bug to be paired with a genuinely non-vacuous test.

## The pattern's yield this session

Six load-bearing hazards caught pre-merge across four arcs, each of which would have surfaced only in real conditions:

- **T-3543 haiku migration audit (artisan):** `model_accepts_sampling_params` (echo-prismatic adapter-claude/lib.rs:253) is a prefix-allowlist with no haiku-successor prefix — a 5-family haiku successor would 400 every classify call unless its prefix is added. Codified as `bugs/model-family-prefix-predicate-defaults-unknown-id-to-unsafe-branch.md`. Silent-defensive-defaults class.

- **T-3182 assembly resolver — verify-tamper #1 (artisan):** `verify` silently copied the publish-time audit fields `prior_lock_sha256`/`update_receipt_ref`, so forged values passed. Fix: verify validates their SHAPE + explicit exemption; real lineage check moved to `plan_assembly_update`. Silent-trust-of-serialized-input class.

- **T-2951 repo reconciler F1 (artisan):** `_do_ff_advance` did a refspec-less `git fetch <url>` — FETCH_HEAD = remote's DEFAULT branch, so a `tracking:<branch>` checkout would fast-forward onto the WRONG branch's content and record success. Fix: fetch specific `refs/heads/<branch>`, advance to exactly that ref. Silent-wrong-branch class.

- **T-2951 repo reconciler F2 (artisan):** `_reclaim` (rollback/remove) checked only origin-URL then `rmtree` — deleted uncommitted/untracked/unpushed content. Fix: record `installed_sha`, refuse on dirty/untracked/HEAD≠recorded-sha; only pristine identity+sha-matched clone is reaped. **Would have destroyed a seat's unpushed work on first live cutover.** Silent-rmtree-of-dirty class.

- **T-3183 assembly transaction F1-shape (artisan, preimpl-caught):** staging was silently going to stage LATEST-not-pin by reusing the reconciler's ff-only apply whose merge return code is ignored + non-tip pins refuse `MISSING_REF`. Fix: distinct staging primitive `clone + checkout --detach <pin> + assert HEAD==pin`. Silent-latest-not-pin class (same shape as T-2951 F1 but caught PRE-BUILD by preimpl review, faster).

- **T-3183 repository extraction MAJOR-1 (artisan, postimpl mutation-tested):** extraction rollback left a `.canonical-source` marker on the new remote → dual-canonical state + broke retry. Fix: defer marker publish until post-cutover. RED/GREEN mutation-confirmed. Silent-partial-commit-on-rollback class.

## Why the discipline works

The **adversarial framing** shifts the reviewer from "does this work?" (default: yes, tests pass) to "how does this silently do the wrong thing?" (default: assume the code defaults unsafe until proven otherwise). Passing tests are evidence of a specific path being exercised; the reviewer looks for paths NOT exercised where the guard defaults unsafe.

The **mutation-test-falsification** step is what keeps the discipline honest. It's cheap (each fix takes ~1 minute to neuter + re-run one test) and it exposes tests-that-don't-actually-test — the "test passes but neutering the fix still passes" case that would otherwise silently land as coverage-that-didn't-cover.

Together they close the "code looks right + tests pass + reviewer eyeballs it" loop where a defensive-defaults bug can hide until real conditions expose it. All 6 hazards above would have passed a naive "green tests → ship" review; the adversarial framing + mutation-test forced them out.

## When to apply

Any arc where:
- **Phase-2 real-actuation is downstream.** The class of bug the discipline catches is exactly the class that only surfaces under real-world conditions. If phase-1 is fixture-only, adversarial review is where phase-2 hazards get caught.
- **Silent-defensive-defaults is architecturally possible.** Any guard where the "default" branch of an if/switch/allowlist could be silently wrong. Common shapes: prefix-allowlists, feature-flag defaults, error-swallowing catches, refspec/pin defaults, ownership-check defaults, deletion-check defaults.
- **The code touches destructive operations.** rmtree/git-reset/DROP TABLE/anything-irreversible has an F2-class hazard by construction — adversarial review is basically mandatory.
- **Multi-step transactions with rollback semantics.** Rollback is where MAJOR-1-class silent-partial-commit bugs hide — the "we tried to unwind but left one marker" case.

Skip when: the change is genuinely additive + doesn't touch defaults / destructive ops / transactions (e.g. adding a new opaque-string field to a schema like T-3935 — nothing to silently default wrong).

## The workflow shape

Orchestrator dispatches implementer → implementer produces working code + tests → orchestrator runs adversarial-reviewer (separate subagent, cold-read of the code with the adversarial framing brief) → reviewer surfaces N candidate bugs → orchestrator fixes each + mutation-test-falsifies each → orchestrator runs postimpl adversarial-reviewer for round-2 (mutation-test the postimpl code to catch fixes that regressed coverage) → handback with the fix trail + falsification evidence in the delivery message.

The critical piece is that **each fix's mutation-test-falsification evidence goes in the handback message** — the receiver (orchestrator in this session, but the pattern generalizes to any reviewer-of-a-reviewer) can then re-verify the falsification claim without redoing the reviewer's cognition.

## Related

- `feedback_isolating_test_before_root_cause_claim.md` (per-user pin) — the mutation-test-falsification step is the same-shape discipline applied to fixes rather than root-cause claims: prove the fix is non-vacuous before shipping.
- `bugs/model-family-prefix-predicate-defaults-unknown-id-to-unsafe-branch.md` — T-3543 artisan find, codified as a reusable-gotcha bug.
- [[ORG-065]] clause-level obligation identity (`ADR-NNN#E1.C1` form; built under T-2842): the closure-validation gate framework this discipline strengthens.
- `architecture/engineering-practices/2026-09-06-mutation-testing-gate.md` (Amendment J) + `tools/mutation_gate.py`: the standing home of the mutation-test half of this lesson. This lesson's genuinely new content is the adversarial-reviewer brief.
- `architecture/engineering-practices/2026-07-28-falsification-must-be-run.md` (Amendment H): the falsification-must-be-run rule this lesson restates.
