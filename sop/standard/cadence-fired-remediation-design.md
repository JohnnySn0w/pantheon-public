# Cadence-fired remediation design contract

**Status:** Adopted 2026-06-30 by Orchestrator (HO) under principles-as-ratification (below-goal-level design methodology per the operator msg 3733; ORG-012 lesson→SOP promotion — HO is the SOP drafter). The operator override-available. Promoted from [`lessons/2026-06-29-bounded-effectiveness-aware-remediation.md`](../../lessons/2026-06-29-bounded-effectiveness-aware-remediation.md) (Recovery, surfaced + shipped live across three tools). **Directive-adjacent:** the four checks are structurally encodable; if a reviewer ever needs to REBOUND a timer/cron tool that lacks them, escalate this to a hard directive gate (D-tier) — left at SOP tier for now.

## When to use this

Whenever you author or review **any remediation that fires on a cadence** — a timer, cron, watch loop, or periodic self-wakeup that re-runs a restart / retry / poke / nudge / re-dispatch while a trigger condition holds. Applies to tool authors (implementers), and to HO/Specialist/Recovery when designing or reviewing such a loop.

Not used: one-shot remediations, event-driven (non-cadence) handlers that fire once per real event, or fire-and-forget actions with no repeat.

## The failure mode this prevents

A remediation fired on a cadence re-runs every cycle *while its trigger condition holds*, with **no check on whether the action is actually working**. Against a target that **cannot** productively respond — rate/spend-capped, classifier-blocked, genuinely wedged — the trigger condition holds forever, so the remediation fires forever: pure waste (tokens, usage-billing money) and operator-awkward, with zero progress. **The remediation's *firing* gets conflated with its *working*.**

The tell: a timer-fired action whose only gate is "does the trigger condition still hold?" — with no recorded answer to "did the *last* firing produce the intended effect?" If you can't point at the observable that distinguishes *idle-and-recoverable* (the action will help) from *stuck* (the action is futile), the loop is unbounded by construction.

## The contract — design four things into every cadence-fired remediation

1. **Effectiveness signal** — one concrete observable answering "did the last firing work?" (poke → did the session's main jsonl advance after it? nudge → did the agent produce *real* work — a ledger entry / commit — not just a reflexive reply?). This is the idle-vs-stuck discriminator.
2. **Bounded attempts** — count *consecutive ineffective* firings; after N (default 1–2), STOP. Caps the burn at ~1–2 actions instead of unbounded.
3. **Pre-action state-gate** — *before* acting, classify known-unrecoverable states and route each to its real remedy instead of the default action: classifier-blocked → restart (not poke); spend/rate-capped → alert + hold; only genuine recoverable-idle gets the default action.
4. **Bounded escalation** — when bounded out, escalate to the real remedy (→ Recovery / a human-in-loop) **once, on the transition**, NOT every cycle. The escalation must not become the new unbounded loop (the original anti-pattern, one level up).

## The non-obvious ordering — state-gate (#3) runs BEFORE the effectiveness check (#1/#2)

The effectiveness signal can be **fooled**. A classifier-blocked session keeps *emitting refusals*, so its jsonl *advances* after a poke and looks "responsive" to the effectiveness check — which would then keep poking forever. So the **pre-action state-gate (#3) must run first**: detect the known-stuck signature structurally (e.g. `stop_reason:refusal` + `stop_details.category:cyber`) and suppress up front, rather than trusting an activity signal that the stuck state itself manufactures. Sequence per cycle: **state-gate → (if recoverable) act → record effectiveness → bound on consecutive-ineffective → escalate-once when bounded out.**

## Worked examples (all live)

- **keep_going (T-1364):** exponential backoff keyed on whether nudges produce real (ledger) work — idle-with-nothing backs off ~15min→8h, resets on real work.
- **codex (T-1365/T-1366):** the nudge fires only when codex's mailbox has *undrained* work (its queue); + a Recovery-side monitor that detects+alerts on codex's rate-limit modal instead of poking blindly.
- **smart-poke (T-1363):** all four — effectiveness via jsonl-advance, bounded attempts, cyber pre-poke gate, bounded escalation-to-Recovery.

## Cross-references

- **Lesson:** [`lessons/2026-06-29-bounded-effectiveness-aware-remediation.md`](../../lessons/2026-06-29-bounded-effectiveness-aware-remediation.md) — the originating evidence (this SOP is its load-bearing promotion per ORG-012).
- **Memory:** Recovery's `feedback_bounded_repeating_remediation` — the operational pin this generalizes.
- **Sibling discipline:** [`lessons/2026-06-26-ships-green-while-incomplete.md`](../../lessons/2026-06-26-ships-green-while-incomplete.md) — same spirit (a signal that *looks* like success but isn't: there smoke-green ≠ done; here loop-fired ≠ fix-worked).
- **Principles/directives:** P1 (architecture over policy — the bound is structural, not a "remember to stop" prose rule); P6 + D10 (context/cost-frugality — the unbounded loop is the burn this prevents); D1 (a perpetually-firing remediation surfaces as operator-awkward usage waste — a bandwidth + money withdrawal).
- **ADRs:** `architecture/adr/ORG-012` (information-inflow bubble-up — the lesson→SOP promotion mechanics this entry rode); `architecture/adr/ORG-011` (activity-monitor / cadence-poke surface where this most applies).
