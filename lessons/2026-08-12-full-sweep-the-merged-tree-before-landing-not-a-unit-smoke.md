# Full-sweep the merged tree before landing — a unit smoke is not enough

**2026-08-12, twice in one session (T-2913 land, T-2927 land).**

**Promoted 2026-08-27 to [`sop/standard/testing-depth-and-scope.md`](../sop/standard/testing-depth-and-scope.md) §Amendment I (Pre-land verification: full-sweep the merged tree)** — Specialist T-2977 filing → Orchestrator adopts below-goal per D3/msg-3733. The SOP amendment is the operational form; this lesson stays as the pattern origin.

## The miss

When landing an agent branch, the HO owns the `origin/main` merge. After merging main into the branch (or landing a direct commit), the tree that ships is the **combination** of the branch's work and everything main accumulated since the branch point. That combination is a tree **nobody has swept** — the worker verified the pre-merge branch, and main's changes were verified on main, but not the two together.

Two instances the same evening:
- **T-2913**: merged origin/main into the branch, ran only a *unit* smoke (`pytest tools/test_host_control_approval_dbus.py`, 81 passed), and landed. The integration happened to be clean, but that was luck, not verification — the PO (broker-po-3) flagged the gap, and the authoritative post-land full sweep is what actually confirmed it.
- **T-2927**: landed an `instance.yaml` edit after running only `test_instance_role_grants` (the obvious guard). A *different* test — `test_t2633_specialist_launch_shape::test_no_fallbacks_key` — encoded a prior decision (T-2633: "specialist gets no fallback") that this edit reversed. It went red on main and needed a fix-forward (PR 304).

## The rule

**Before landing, run the full `python3 -m pytest tools/ -q` on the exact tree that will ship** — the post-merge branch tree, or (equivalently) main immediately after a direct commit. Not the unit's own test file. Not the one guard test you expect to be affected. The full suite, because:

- A change can break a test in a *different* subsystem that encodes an assumption your change reverses (the T-2927 case: an `instance.yaml` edit broke a launch-shape test, not a role-grant test).
- Main's intervening work may interact with the branch in ways neither side's isolated verification covers (the T-2913 integration case).

A unit smoke answers "does my code work in isolation." The landing question is "is the shipped tree green," and only the full sweep answers that.

## Corollary — reconcile the delta

The full sweep's `passed/skipped/failed` totals must reconcile against the baseline plus the known additions (see [[2026-08-12-subtest-counts-are-directional-not-a-stable-equality-check]] for the subtest wobble). An unexplained failure count is the signal this rule exists to surface. A test that passes isolated but fails in-suite (the T-2382 case here) is flakiness/ordering, not your regression — confirm by isolated re-run before dismissing, and confirm by isolated re-run before *blaming* your change either.
