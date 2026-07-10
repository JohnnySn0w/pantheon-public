# Lesson — restart/concurrency-path races masked by pre-arranged test timing

**Surfaced:** 2026-06-25/26, Echo Phase-3 (T-1313 / B1, twice). Filed by Orchestrator (HO); drafted by the `echo-phase3-orch` project-orchestrator from the live arc.

## The pattern

A concurrency/restart mechanism has a real race or unresponsiveness bug, but its tests PASS because they arrange the timing to avoid the failure window — typically by delivering the trigger BEFORE the system reaches its steady-state wait, so the bug's precondition never occurs in the test.

## Evidence (Echo Phase-3 B1, T-1313 — two REBOUNDs)

- **REBOUND #1 (deaf STT):** the VAD→STT re-wire left the new STT session deaf after a deliberate restart, but the test asserted "the factory builds a session" — never "audio reaches the new session." The functional gap was simply outside what the test exercised.
- **REBOUND #2 (unresponsive-while-healthy):** `monitor()` drained restart requests via `try_recv()` at loop-top, then blocked on `select_all` with no restart branch — so a restart arriving AFTER monitor parked was never processed. The test sent the restart BEFORE calling `monitor()`, so the first `try_recv` caught it before any `select_all` block happened. **The bug's precondition (monitor already parked) was never created by the test.**

## The tell

A restart/concurrency test that delivers its trigger at construction-time, or before the event loop starts, rather than mid-block after the system is in its steady-state wait. Tests that assert a thing was *built/constructed* rather than that the *effect propagates end-to-end*.

## Mitigation (verified — the fix-test times out under the buggy code)

Restart/concurrency tests MUST deliver the trigger AFTER the system reaches its steady-state wait: spawn the mechanism on a never-exiting subsystem, yield to let it park, THEN send the trigger, and assert it's handled within a short timeout. The B1 fix added exactly this (`deliberate_restart_responsive_while_monitor_parked`) — it would TIME OUT under the buggy code, so it's a real regression guard, not a construction check.

Make **"deliver the trigger mid-block, not pre-arranged"** an explicit requirement in any restart/concurrency dispatch brief, and assert the *propagated effect* (audio reaches the new session), not just construction.

## Cross-reference

One of three Echo-Phase-3 lessons sharing a root — **a test/affordance that *looks* like it validates but doesn't exercise the real failure condition**: see [`2026-06-26-ships-green-while-incomplete.md`](2026-06-26-ships-green-while-incomplete.md) (the required test was *absent*) and [`2026-06-26-silent-no-op-wrong-subsystem-wiring.md`](2026-06-26-silent-no-op-wrong-subsystem-wiring.md) (the affordance was wired to the *wrong scope*). Plan-first + grounded-review caught all three pre-operator — the load-bearing argument for keeping that discipline non-optional on concurrency + control-surface work.

**Superseded as load-bearing** by [`sop/standard/testing-depth-and-scope.md`](../sop/standard/testing-depth-and-scope.md) **Amendment G (G1 — concurrency/restart tests trigger after steady-state + assert effect-propagation)**, ratified T-1326 (2026-06-26). This lesson stays as the originating evidence; the live discipline now rides the org testing SOP + its reviewer-checklist (ORG-012 demotion).
