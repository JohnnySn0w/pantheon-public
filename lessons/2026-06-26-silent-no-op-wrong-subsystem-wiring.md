# Lesson — silent-no-op: wiring a control to the wrong-subsystem apply/restart op

**Surfaced:** 2026-06-25/26, Echo Phase-3 (B2 pre-review + A2 pre-dispatch; bit/nearly-bit 3×). Filed by Orchestrator (HO); drafted by the `echo-phase3-orch` project-orchestrator.

## The pattern

A GUI/control affordance is wired to a mechanism that LOOKS applicable but is scoped to a *different* subsystem, so triggering it *appears* to work but is a silent no-op — the intended effect never happens and no error surfaces. Especially common when one subsystem has a convenient apply/restart op and an adjacent config is wrongly assumed to share it.

## Evidence (Echo Phase-3, three times)

- **B2 plan (caught at HO pre-review):** wired ALL 3 settings restart-banners → `apply_restart(speechmatics-stt)`, but `apply_restart` is STT-only and the VAD-threshold knob is VAD-construction-bound — restarting STT does NOT apply a VAD change. The vad-banner would have been a silent no-op (button works, change never takes effect, STT needlessly bounced).
- **A2 (caught at pre-dispatch grounding):** scoped to wire the MCP-source-panel restart banner → `apply_restart(speechmatics-stt)`, but a registered MCP source needs a **daemon-scoped** restart (registry composed at startup) — an STT restart doesn't load a new source. A2-as-scoped was structurally a silent no-op; retired (→ T-1325). Notably **the ratified ADR-223-amendment's own "reuse B's restart mechanism" premise was wrong** — B's mechanism is STT-subsystem-scoped, so the design doc itself carried the error until code-grounding exposed it.
- **The daemon truth underneath both:** `apply_restart` is hard `speechmatics-stt`-only; VAD-apply and source-apply each need their own (currently absent) restart op (→ fast-follows T-1316, T-1325).

## The tell

"Reuse the existing apply/restart action for this other banner/config" *without verifying the action's subsystem scope matches what the config is bound to*. A single convenient restart op assumed to apply changes it doesn't actually reach. (A design doc/ADR amendment is not immune — the A2 case had the wrong premise *ratified* before code-grounding caught it.)

## Mitigation

Before wiring any control to an apply/restart mechanism, verify the mechanism's **scope** (which subsystem it actually restarts) MATCHES the config's **binding** (which subsystem constructs/consumes it). When they don't match:
1. Leave the affordance **manual**, with a boundary comment naming WHY.
2. Add a **not-wired regression-guard** test (assert the affordance/button is absent) so a future "wire all the banners" sweep can't silently re-introduce it.
3. File a **fast-follow** to build the correctly-scoped op.

The B2 not-wired Suite F + the A2 retirement + T-1316/T-1325 are the worked example. A plan-first pre-review that grounds the wiring against the daemon's *actual* op scope catches this before code — and, per the A2 case, before a wrong premise hardens into a ratified design.

## Cross-reference

One of three Echo-Phase-3 lessons sharing a root — **a test/affordance that *looks* like it validates/works but doesn't exercise the real failure condition**: see [`2026-06-26-restart-path-race-masked-by-pre-arranged-test-timing.md`](2026-06-26-restart-path-race-masked-by-pre-arranged-test-timing.md) (test *timing* dodged the bug) and [`2026-06-26-ships-green-while-incomplete.md`](2026-06-26-ships-green-while-incomplete.md) (the required test was *absent*). The common defense across all three is plan-first + grounded review on concurrency and control-surface work.

**Superseded as load-bearing** by [`sop/standard/testing-depth-and-scope.md`](../sop/standard/testing-depth-and-scope.md) **Amendment G (G3 — control-surface wiring scope-match + not-wired guard)**, ratified T-1326 (2026-06-26). This lesson stays as the originating evidence; the live discipline now rides the org testing SOP + its reviewer-checklist (ORG-012 demotion).
