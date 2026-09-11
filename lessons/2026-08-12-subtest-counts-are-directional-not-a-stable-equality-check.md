# Subtest counts are directional, not a stable equality check across runs

**2026-08-12, T-2913 (ORG-052 AB-5) landing.**

## The observation

Two independent full-sweep runs of the same green tree read different subtest totals: the implementer's sweep reported `1216 subtests passed`, the PO's D5 sweep reported `1215`. The `passed / skipped / failed` totals were identical (`7586 / 85 / 0`) across both. No flakiness, no missing assertion — the difference is a benign, expected wobble.

## Mechanism (verified at source, not inferred)

`ApprovalErrorNamespaceTests::test_error_mapping_is_total_over_the_live_exception_hierarchy` emits one `subTest` per live exception subclass. Its `_hierarchy()` helper walks `HostControlError.__subclasses__()` **transitively**. A subclass only appears in `__subclasses__()` once its **defining module has been imported** into the interpreter. Under pytest, which `tools/host_control` modules are already imported when that test executes depends on collection/execution order relative to the rest of the `tools/` suite. So the live subclass count — and thus that test's subTest iteration count — can differ by one or more between runs.

The test's actual guarantee is unaffected: *every live subclass resolves into the closed error-name set*, holding for whatever set is live, with `len(hierarchy) >= 15` asserted as a floor before iterating. Only the iteration count wobbles.

## The rule for future units

When reconciling sweep deltas across two runs (baseline vs post-build, implementer vs PO D5, pre- vs post-merge):

- **Reconcile `passed` / `skipped` / `failed` as exact equality** — those are the invariants; a mismatch there is a real signal.
- **Treat `subtest` counts as directional, not an equality check.** A ±N subtest wobble between runs of the same green tree, with identical pass/skip/fail totals, is import-order non-determinism, not a defect. Do not spend a re-run "settling" it; explain the mechanism and move on.

A subtest total that moves *while pass/skip/fail also move* is a different situation — that is a real delta to attribute, and the [[feedback_smoke_grep_all_test_result_lines]] discipline applies.
