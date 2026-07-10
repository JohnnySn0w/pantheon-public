# Lesson — "bounded, effectiveness-aware remediation": a cadence-fired fix needs a did-it-work check, a cap, a pre-action gate, and a bounded escalation

**Surfaced:** 2026-06-28/29, across the keep_going nudge (T-1364), the codex idle nudge (T-1365/T-1366), and the smart-poke redesign (T-1363). Filed by Recovery (`claude-fallback`). Operator trigger: the operator TG 3473 ("we'll be sending pokes until a session resets… burns tokens… how do we differentiate stuck ones?") + TG 3508 ("the keep-going nudge is wasting a lot of usage").

## The pattern

A remediation fired on a **cadence** (a timer/cron) re-runs every cycle *while its trigger condition holds*, with **no check on whether the action is actually working**. Against a target that **cannot** productively respond — rate/spend-capped, cyber-classifier-blocked, genuinely wedged — the trigger condition holds forever, so the remediation fires forever: pure waste (tokens, money on usage billing) and operator-awkward, with no progress. The remediation's *firing* is conflated with its *working*.

This is the failure mode behind: keep-going nudging a perpetually-idle agent every 15 min; activity_monitor poking a session every 30-min cooldown; any "restart/retry/poke on a schedule" loop.

## The tell

A timer-fired action whose only gate is "does the trigger condition still hold?" — with no recorded answer to "did the *last* firing produce the intended effect?" If you can't point at the observable that distinguishes *idle-and-recoverable* (the action will help) from *stuck* (the action is futile), the loop is unbounded by construction.

## Mitigation (verified — shipped + live across three tools)

Design **four** things into every cadence-fired remediation:

1. **Effectiveness signal** — one concrete observable answering "did the last firing work?" Poke → did the session's main jsonl advance after it? Nudge → did the agent produce *real* work (a ledger entry / commit), not just a reflexive reply? This is the idle-vs-stuck discriminator.
2. **Bounded attempts** — count *consecutive ineffective* firings; after N (default 1-2), STOP. Caps the burn at ~1-2 actions instead of unbounded.
3. **Pre-action state-gate** — *before* acting, classify known-unrecoverable states and route each to its real remedy instead of the default action: cyber-blocked → restart (not poke); spend/rate-capped → alert + hold; only genuine recoverable-idle gets the default action.
4. **Bounded escalation** — when bounded out, escalate to the real remedy (→ Recovery / a human-in-loop) **once**, on the transition, NOT every cycle. The escalation must not become the new unbounded loop (the original anti-pattern, one level up).

## The non-obvious bit — the effectiveness signal can be FOOLED

A cyber-classifier-blocked session keeps **emitting refusals**, so its jsonl *advances* after a poke and looks "responsive" to the effectiveness check (#1) — which would then keep poking forever. So the **pre-action state-gate (#3) must run BEFORE the effectiveness check (#2)**: detect the known-stuck signature structurally (e.g. `stop_reason:refusal` + `stop_details.category:cyber`) and suppress up front, rather than trusting an activity signal that the stuck state itself manufactures.

## Where applied

- **keep_going (T-1364):** exponential backoff keyed on whether nudges produce real (ledger) work — idle-with-nothing backs off ~15min→8h, resets on real work.
- **codex (T-1365/T-1366):** the nudge fires only when codex's mailbox has *undrained* work (its queue); + a Recovery-side monitor that detects+alerts on codex's rate-limit modal instead of poking blindly.
- **smart-poke (T-1363):** all four — effectiveness via jsonl-advance, bounded attempts, cyber pre-poke gate, bounded escalation-to-Recovery.

## Cross-reference

Captured operationally in the agent-autonomy / trust-posture lineage and Recovery's memory `feedback_bounded_repeating_remediation`. Sibling to "ships green while incomplete" in spirit: both are about a *signal that looks like success but isn't* — there, smoke-green ≠ done; here, the-loop-fired ≠ the-fix-worked.

**Promoted as load-bearing** to [`sop/standard/cadence-fired-remediation-design.md`](../sop/standard/cadence-fired-remediation-design.md) (Adopted 2026-06-30 by Orchestrator (HO) under ORG-012 lesson→SOP, principles-as-ratification; T-1463). This lesson stays as the originating evidence; the live design contract now rides the org SOP + a tool-author's read-order.
