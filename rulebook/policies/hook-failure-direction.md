# Hook failure direction: security-gating hooks fail closed, advisory hooks fail open

**Status:** Adopted 2026-11-02 by Orchestrator (HO), autonomous under the operator msg 3733 (below-goal-level policy adopted under P/D justification + ledger, no ratify-ask). It writes down the HO ruling of 2026-09-30 (T-4392), including Recovery's break-glass amendments (T-4392 transition log, 2026-09-30), which until now lived only in that ticket and a `tools/hooks/README.md` row. Policy home requested by sweep 2026-10-04 item B2 (T-4560).
**Author:** Orchestrator (Orchestrator); drafted by mason.
**Re-evaluate when:** Claude Code changes PreToolUse timeout semantics (e.g. a timed-out hook starts blocking); or a security-gating hook is found that cannot run through `_failclosed` for a structural reason.
**Justifies via:** P1 (a structural deny-on-failure, not "be careful with regexes"), D3 (the autonomy bar: a guard that silently lets calls through gives the trust the bar assumes away).

## Why

In Claude Code, a PreToolUse hook that times out or errors is a NON-blocking error: the tool call PROCEEDS. Only exit 2 blocks. So a guard that crashes, hangs or hits a super-linear input fails OPEN on exactly the inputs it exists to stop (`lessons/2026-09-30-pattern-guards-in-fail-open-hooks-need-a-linear-time-gate.md`).

## The rule

1. **Security-gating hooks fail CLOSED on their own clock.** A hook whose job is to deny (worker-guard, protected-doc and push guards, allowlist gates) MUST run through `tools/hooks/_failclosed.py`. An exception, an ambiguous classification, its own wall-clock deadline (15s from process start, well under the wired harness timeout) or an oversized input gives a DENY (exit 2) with a clear message.
2. **Advisory hooks fail OPEN.** Memory recall, telemetry, context injectors and nudges must never block work when they fail.
3. **Every PreToolUse hook declares its class** (security-gating or advisory) in `tools/hooks/README.md`. A security-gating hook that doesn't go through `_failclosed` is a defect: it reopens the 09-30 bypass.
4. **Break-glass is the only override.** The worker-untriggerable, auto-expiring break-glass flag in `_failclosed` (Recovery is mailed at creation and before expiry) is the only sanctioned way past a fail-closed deny. No per-hook escape hatches.

## Consequence

With rule 1 in place, a super-linear path in a security-gating hook is an availability defect (latency, over-deny; e.g. T-4483), not a security bypass. Keep linear-time matching anyway: a 15s stall and a refused legitimate call are real costs.

## Cross-references

- T-4392 (the ruling and the fleet audit), T-4483 (a superlinear path now fails closed via the deadline), T-4560 (this policy home).
- `lessons/2026-09-30-pattern-guards-in-fail-open-hooks-need-a-linear-time-gate.md` (partially superseded by this rule).
- Enforcement follow-up: T-5939, a meta-test that every hook the README lists as security-gating runs through `_failclosed`.
