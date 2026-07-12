# Seed own-suite expected failures — triage record + standing disposition (T-1677)

**Status:** RECORDED 2026-07-12 (pantheon-config T-1677, mirroring the T-1295 disposition pattern).
**Applies to:** the refreshed role-generic seed (T-1676 build) and every future refresh until the
skip-manifest lands. **None of these block sharing** — they are structural to seedhood, not defects.

## The 28 failures, by class

1. **Role-vocabulary self-collision — 15 tests**
   `ticket_lint` TestReposFieldGate ×12, `ticket_normalization` ×3.
   Mechanism: neutralization collapses the persona vocabulary (thoth/argus/ward) and the role-key
   vocabulary (head_orchestrator/science_team/…) into one generic token set, so tests that
   `assertIn` one vocabulary while `assertNotIn` the other end up asserting on the SAME literal —
   self-contradictory by construction after neutralization.
2. **CLEAR-set-content-dependent — 11 tests**
   `policy_outflow_sync` ×2, `s3star_audit_runner` ×9.
   Mechanism: these read ledger entries / proposals / audit artifacts that are deliberately absent
   from any seed (content is CLEARed by design). The tests verify behavior over data seedhood
   removes.
3. **Cross-repo claude-config coupling — 2 tests**
   Already tracked as pantheon-config T-1294; they require the sibling claude-config checkout.

Itemized per-test list: pantheon-config T-1676 build-triage notes (transition log) — kept there
rather than duplicated; the class → module/class granularity above is sufficient to re-recognize
a refresh's failure set at a glance.

## Standing disposition (the decision, so refreshes stop re-litigating)

**Chosen: Option A — skip-manifest emitted by the neutralizer** (implement inside the T-1678
tool-unification; the manifest marks classes 1 and 2 as expected-skip at generation time, with
this file as the rationale pointer).

**Rejected: Option B — porting T-1295-style predicate test-rewrites forward.** Rationale: hand
rewrites live in seed OUTPUT and are regenerated away on every refresh — durability requires the
generator to apply them, at which point a skip-manifest is the same neutralizer feature with less
surface. Rewrites also encode live-repo vocabulary semantics into seed tests, which is exactly
what neutralization removes; a seed without live content SHOULD skip content-dependent suites
rather than pretend-pass rewritten versions of them.

Class 3 stays with T-1294 (real coupling to fix, not to skip).

## Re-recognition rule for future refreshes

After any regeneration, run the suite; the expected-failure set should match classes 1+2 above
(≤28, shrinking as T-1678/T-1294 land). NEW failures outside these modules = real regression in
the neutralizer — triage fresh, don't wave them through under this record.
