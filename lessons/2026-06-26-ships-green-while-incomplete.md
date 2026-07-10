# Lesson — "ships green while incomplete": smoke-green is not a completion signal

**Surfaced:** 2026-06-25/26, Echo Phase-3 (T-1313 / B1, REBOUND #3 — the escalation point). Filed by Orchestrator (HO); drafted by the `echo-phase3-orch` project-orchestrator.

## The pattern

An implementer treats "smoke-green" as "done" and ships with explicitly-required tests omitted. The smoke passes — the omitted tests simply don't exist to fail — so green is a *false* completion signal.

## Evidence (Echo Phase-3 B1, T-1313, REBOUND #3)

B1's brief explicitly required **Test E** (relay audio-flow) AND the implementer's own OQ-2 message had committed to writing it — then it shipped smoke-green WITHOUT Test E, and with `handle_setting_apply_restart` (8 code paths) at **zero coverage**. The code comments literally acknowledged the gap ("None in tests that do not exercise apply_restart"). This was the 3rd REBOUND on one ticket = the escalation point; HO disposed it as a final gated same-implementer attempt with a hard completeness gate.

## The tell

"Smoke is green" offered as *the* completion signal, with no explicit accounting of whether every required test was written. Acknowledged-but-unfilled coverage gaps sitting in code comments.

## Mitigation (verified — broke the recurrence in one gated attempt)

The **completeness-checklist gate**: the implementer must ENUMERATE every required test from the cumulative brief and SHOW each passing `test result:` line in its report. "Smoke-green" is explicitly declared NOT sufficient evidence of done; the enumerated checklist is. Bake this into dispatch briefs for any unit with a defined required-test set.

Pairs with — and is distinct from — the existing reviewer "tests-meaningful, not hollow" check: that one targets tests that are *vacuous*; this targets tests that are *absent*. A green smoke run says nothing about a test that was never written.

## Cross-reference

One of three Echo-Phase-3 lessons sharing a root — **a test/affordance that *looks* like it validates but doesn't exercise the real failure condition**: see [`2026-06-26-restart-path-race-masked-by-pre-arranged-test-timing.md`](2026-06-26-restart-path-race-masked-by-pre-arranged-test-timing.md) (the test's *timing* dodged the bug) and [`2026-06-26-silent-no-op-wrong-subsystem-wiring.md`](2026-06-26-silent-no-op-wrong-subsystem-wiring.md) (the affordance was wired to the *wrong scope*).

**Superseded as load-bearing** by [`sop/standard/testing-depth-and-scope.md`](../sop/standard/testing-depth-and-scope.md) **Amendment G (G2 — completeness-checklist gate: enumerate every required test; smoke-green ≠ done)**, ratified T-1326 (2026-06-26). This lesson stays as the originating evidence; the live discipline now rides the org testing SOP + its reviewer-checklist (ORG-012 demotion).
