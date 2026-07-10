# Defer with trigger — calendar OR event, choose to match the blocker

**Date authored:** 2026-05-10
**Author:** Orchestrator (Orchestrator)
**Status:** Authored (lessons/ is an Authored, non-ratifiable class per ORG-015; the older "awaiting ratification" convention predated it)
**Re-evaluate when:** A net-new defer category surfaces that doesn't fit calendar or event triggers (e.g., budget-trigger deferral if cost-ceilings ever ratify); or when ticket-walk routine cadence changes shape enough to alter the bandwidth math.

## Observation

When a piece of work is **not actionable yet** but is also not abandoned — the path forward depends on a future condition (a date arriving; a structural signal like a cap-hit cycle firing; a vendor deploy landing) — the worst pattern is to leave it in routine ticket-walks. Each walk re-surfaces it; each surface burns operator bandwidth on "still not actionable, leave it." Across N walks × M deferred items, this is a measurable D1-tier cost.

The right pattern is **defer with a trigger that matches the actual blocker.** Two dominant trigger shapes:

1. **Calendar triggers** — "fire reminder on 2026-MM-DD." The block dissolves on a known date (a vendor deploy ETA, a re-evaluation cadence, a quarterly sweep). Implemented via `not_before` field + a reminder ticket OR a cron-fired calendar-check.
2. **Event triggers** — "fire when X happens." The block dissolves on a structural signal whose timing isn't known in advance (a cap-hit cycle, an external acknowledgment, an arc-completion). Implemented via watching the signal source; the deferral surfaces when the event fires, not on a routine walk.

The choice between the two is determined by the actual blocker shape. Picking the wrong trigger either over-surfaces (calendar trigger fires before the event arrives, requires re-deferral) or under-surfaces (event-trigger waits indefinitely when a calendar trigger would have given a sane ceiling).

## Grounding

### Calendar-trigger cases

- **T-0041 → T-0154** (2026-05-09): the operator msg_801 articulated the defer-with-ping-date pattern. T-0041's path forward depended on a research-validation window (~3 weeks of token-distribution data); not_before + reminder ticket fires 2026-05-30. Operationally validated: the operator doesn't see T-0041 in routine walks until the data window closes, at which point the reminder surfaces it once with the empirical answer.
- **T-0061's not_before mechanic** — generalized the pattern across other deferrals; refactored T-0048 → T-0069 + T-0071 to use the same shape.
- **T-0175** (Sub-A Recovery branch protection, deferred to 2026-05-11 per the operator msg_885): calendar-triggered hold on a fan-out item that pairs with same-day operator availability.

### Event-trigger cases

- **T-0067 (graceful wind-down)**: blocked on first-cap-hit-cycle data; not a date but a structural signal. Surfaces when the cap-hit fires + observability captures the wind-down patterns. Per HANDOFF, "deferred on cap-hit cycle data trigger."
- **T-0155 (fresh-session-via-navigation ADR)**: blocked on cap-hit empirical data; same trigger family as T-0067. Pairs with T-0178 (this is the WHEN; T-0155 is the WHAT).
- **T-0185 (verify j17 v0.7.9+ deploy)**: filed today; depends on Elija's deploy. Event trigger: external vendor's release. Verified by re-running T-0128 once deployed.
- **T-0102 / T-0147 drift cases**: passively event-triggered by the b33 ticket-state sweep tool's first fire; same shape, different surface.

### What both trigger families share

- Operator gets surfaced **once when actionable**, not per routine walk.
- The deferral state is auditable: the ticket carries the trigger explicitly in its frontmatter (`not_before`, `depends_on`, or transition-log disposition), so a future operator can verify the deferral wasn't just dropped on the floor.
- The trigger itself can fail (calendar reminder doesn't fire because of a cron break; event signal doesn't propagate because of an integration gap) — but the failure modes are **diagnosable** rather than silently-ignored. Bug catalog entries (`bugs/`) accumulate around trigger failure shapes.

## Design implications

### Pick the trigger that matches the blocker

- If the path forward depends on **time elapsing** (research window; vendor deploy ETA; re-evaluation cadence) → **calendar trigger** + `not_before` field + optional reminder ticket.
- If the path forward depends on **a structural signal firing** (cap-hit cycle; external ack; deploy landing; integration test passing) → **event trigger** + watching the signal source explicitly (depends_on relationship; passive monitoring; reactive ticket from the signal source).
- If the path forward is **both date-bounded AND event-conditional** (vendor deploys "by end of Q2 OR sooner if the bug is critical") → **calendar ceiling + event-fires-earlier-if-applicable**. Calendar gives a safety floor; event lets the deferral end early. T-0049 (calendar trigger 2026-06-01 — re-evaluation of HERMES discipline) is closest to this shape today.

### Routine ticket-walks should skip deferred items by default

The auto-consumer's `not_before_unmet` skip check + `interactive-required` label discipline both honor this. A deferred ticket that re-surfaces in walks is either (a) the trigger fired and the surface IS the actionable signal, or (b) the trigger broke and surfacing is the diagnostic. Either way, the surface is meaningful — not noise.

### `depends_on` is the structural shape for event triggers

When event-trigger work depends on another ticket landing first (T-0185 depends_on T-0133's deploy-fix landing; T-0176 + T-0177 depend_on T-0175), the dependency is the trigger. D9 mandates `depends_on` items go in `blocked/`, not `inbox/` — operator's routine walk skips them automatically.

### `not_before` is the structural shape for calendar triggers

`tools/ticket.py` honors `not_before` in frontmatter; the auto-consumer's `not_before_unmet` skip check fires on it. Setting a `not_before` is the cheapest possible defer-with-trigger; no separate reminder ticket needed.

### When both are needed, file a thin reminder ticket

If the deferred work is significant enough to warrant a fresh-context surface at trigger-fire time (rather than just unblocking an existing ticket), a thin reminder ticket targets the operator and fires on the calendar OR depends_on signal. Mirrors T-0154's shape (T-0041 reminder, fires 2026-05-30 with the empirical-question payload).

### Don't over-engineer; pick the right primitive

- Single-line not_before? Use the field, no ticket needed.
- Multi-line context that operator needs at trigger-fire? File a reminder ticket; thin body with payload.
- Trigger is event-shaped + dependency is on another ticket? Use depends_on; D9 routes to blocked/.
- Trigger is calendar-shaped + dependency is on time? Use not_before; auto-consumer routes via skip.
- Trigger is dual-shaped? Use both fields together (depends_on + not_before).
- Trigger is fuzzy ("when it's clear we need it")? You don't have a trigger — that's a held-open ticket, which fails the discipline of this lesson. Sharpen the trigger or close + re-file when actionable.

## Open questions

- **What's the bandwidth cost of an over-eager calendar trigger that fires before the actual blocker dissolves?** Has happened occasionally (re-deferral burn); how often is it acceptable vs how often is it a sign of mis-picked-trigger?
- **Can event triggers be made structural at the auto-consumer tier?** `depends_on` already is. Could "watch this commit on a remote branch" or "wait for this systemd unit to fire" be first-class trigger types in `tools/ticket.py`'s field schema? Currently they're encoded ad-hoc in transition-log disposition prose. Making them structural would let operator-tools generate trigger reports without prose-parsing.
- **Trigger-failure diagnostics** — `bugs/` catalog should include the recurring trigger-failure shapes (cron break that ate a reminder; depends_on cycle that never resolved). Today's catalog has none yet; surface as they're observed.
- **How does this lesson interact with T-0168 (information-inflow bubble-up methodology)?** The bubble-up question is about *promotion stature* (lesson → directive → principle). The defer-with-trigger question is about *cadence* (when does the deferred work get re-surfaced). They're orthogonal but adjacent — bubble-up promotion may itself be deferred-with-trigger ("re-evaluate this lesson's directive-promotion candidacy after 3 incidents corroborate").

## Cross-references

- **Principles + directives**: D1 (the operator's bandwidth — direct lens; this lesson is the operational-cost-reduction discipline), D9 (orchestrator response contract — categorical answer + format + timing; the defer-with-trigger pattern is what enables D9's "timing" component), D10 (context-frugality — the lesson is the cadence-frugality companion to D10's content-frugality).
- **CLAUDE.md sections**: §Auto-resume on Max-window cap-hits (the cap-hit cycle is itself an event-trigger surface; T-0067/T-0155 ride on it).
- **ADRs**: ORG-003 (ticket system — the substrate this lesson operationalizes; not_before + depends_on are the primitives).
- **Lessons**: [`org-churn-is-constant-methodology-must-be-concise.md`](org-churn-is-constant-methodology-must-be-concise.md) (sister; cadence-frugality fits the broader org-churn discipline). [`instruction-fidelity-collapse-discipline-vs-architecture.md`](instruction-fidelity-collapse-discipline-vs-architecture.md) (sister; both lessons argue for *structural* primitives over discipline-by-prose — this lesson's `not_before` + `depends_on` are exactly the structural surface that replaces "remember to defer manually").
- **Tickets that ground**: T-0041 (msg_801 articulation; date-trigger founding case), T-0154 (T-0041's reminder; fires 2026-05-30), T-0061 (not_before mechanic generalization), T-0067 (event-trigger; cap-hit cycle), T-0155 (event-trigger; cap-hit data), T-0185 (event-trigger; vendor deploy), T-0175 (calendar-trigger via msg_885), T-0049 (calendar trigger 2026-06-01 — re-evaluation of HERMES discipline). T-0168 (bubble-up methodology; sibling lesson-in-flight).
- **Recovery auto-memory**: `feedback_defer_with_ping_date_pattern.md`, `feedback_event_driven_defer_pattern.md` (the founding pin pair that surfaced this pattern in Recovery's auto-memory; lesson here generalizes them into the org-tier corpus).
- **Telegram refs**: msg_801 (the operator's articulation of the defer-with-ping-date pattern), msg_863 (T-0158 ratification + per-lesson surface-time discipline), msg_885 (T-0175 deferral example).
- **External**: GTD (Getting Things Done) "tickler file" practice — calendar triggers; same load-bearing why. Issue trackers' "snooze" feature (Linear, GitHub) — same pattern at vendor-tool tier; Pantheon's `not_before` + reminder-ticket combination is the same pattern at our scale.
