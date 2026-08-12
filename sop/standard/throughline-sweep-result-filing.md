# Filing a throughline / cross-folder sweep result

## When to use this

Whenever an agent runs an ORG-012 cross-quarter throughline sweep (Cadence #6)
or any other cross-folder review that produces a **menu of proposed
promotions / demotions / methodology gaps / design throughlines** for operator
decision. The output of such a sweep is the subject of this SOP — *not* how the
sweep is conducted (that's ORG-012 + `org-012-per-agent-portions.md`).

This is the **output-format contract for the recurring ORG-012 Cadence #6
sweep**, in both its forms:
- **Manual run** — done by hand (T-0780 was the first such run, 2026-06-04).
- **Scripted/periodic run** — the daily automated harness (T-0781, ORG-012
  Phase C). **The scripted sweep's auto-filer MUST emit per this SOP** — an
  index ticket + discrete sub-tickets — and must NOT collapse the result into a
  single rolling ratify-ticket / menu (that reintroduces exactly the
  unreviewable-monolith anti-pattern this SOP exists to kill, and would
  auto-bounce on the no-single-ratifiable-artifact rule, T-0556).

The anti-pattern this exists to kill: dumping the whole sweep as a single
monolithic "decision menu" ticket. A menu is unreviewable — the operator can't
see, track, ratify, or act on any single item independently, and an
`intent=ratify` menu auto-bounces because it has no single ratifiable artifact
(T-0556). T-0780 was the motivating case (the operator, 2026-06-05: "this action
menu kinda sucks as a format … it should be broken out").

## Procedure

### 1. File a parent INDEX ticket

One `intent=action` ticket that holds the sweep's framing + methodology + a
linked list of its child sub-tickets (by `T-NNNN` id, which renders as a
navigable reference). The index carries no decisions of its own — it is the
table of contents. Its job is lineage + a single place to see the whole sweep.

### 2. One discrete sub-ticket per actionable item

Every promotion / demotion / gap / throughline that needs an action or a
decision becomes its **own** ticket, `parent_ticket` set to the index, routed
to the owner who actions it:

| Item class | Target | Intent | Notes |
|---|---|---|---|
| **Goal-level** decision/direction (which-projects · externals · money · secrets/payments · account-and-access grants · un-defer of an operator-deferred phase · vault ranges he authored — per the goal-level bar) | `operator` | `action` (or `ratify` once a ratifiable artifact exists) | **MUST ALSO be lean-surfaced to the operator (Telegram/digest).** As of `278573602` (T-2199) there IS a fallback surface: `watch_digest` renders a "Parked on you" section from any `target: operator` ticket in `inbox/`/`active/` older than 2 days, 5 per fire, oldest-first, suppressed 7 days after a receipt. It is a SAFETY NET, not a substitute — it drips 5/week against a backlog that has run to 23, so a goal-level item you leave to it may wait weeks. Surface it yourself if it matters now. **The other three gaps still hold:** `inbox_consumer` is agent-only, `activity_digest` posts only RESOLVED, `ticket_state_sweep` surfaces only operator-MOOT. ⚑ **IF YOU HAND-RELAY, YOU MUST WRITE THE RECEIPT** — `ticket.py annotate <tid> --note "lean-surfaced; <what you sent, where, when>"`. `last_lean_surface()` is the ONLY suppressor, and it needs `lean-surfaced` as a whole `;`-delimited segment. No receipt → the digest re-sends the same item within 7 days and **the operator receives it twice**, which is a D1 cost out of the document whose purpose is protecting D1. The ticket is the durable record; the lean-surface is how he actually learns of it. (Root cause of the T-1195–1198 rot, surfaced by Orchestrator's mailbox flag 2026-06-16.) |
| **Below-goal "decision"** (methodology · SOP · lesson-curation · ADR-tier · architecture · org-shape · infra-enforcement · remediation) | the **owning agent** — ADOPT; do NOT route to `operator` | `action` | Per principles-as-ratification (msg-3733 / amended-D3): the owning agent ADOPTS it under P/D justification + ledger; route the BUILD to that agent; the operator's surface is a **digest, not a ratify-ask**. **NEVER park a below-goal "decision" as `target:operator`** — that mislabel + the no-auto-delivery gap above is exactly what rotted T-1195–1198. |
| Build a concrete artifact (new SOP, lesson, ADR, promotion) | the owning agent | `action` | A goal-level/ratifiable-tier artifact then files its OWN `intent=ratify` ticket with `--ratifiable-file` (two-step; don't pre-file ratify for a not-yet-built artifact). A below-bar artifact terminates at **Adopted** (no ratify ticket). |
| Demotion / supersedure-trail bookkeeping | the owning agent | `action` | HO-autonomous where applicable; group a single bookkeeping class into one ticket. |

Group only where the menu itself already grouped a single class (e.g. "the
4 supersedure trails" → one bookkeeping ticket). Do NOT group distinct
decisions — the operator needing to see them separately is the requirement.

### 3. Don't duplicate existing tickets

If a menu item already maps to an open ticket (e.g. a cadence-health item that
== an existing backlog ticket), cross-reference it in the index instead of
filing a duplicate.

### 4. Two-step for promotions to a ratifiable tier

A promotion line item is a **greenlight request**, not a ratification — but per
msg-3733, *whose* greenlight depends on tier. **Below-bar promotions**
(methodology/SOP/lesson/ADR-tier) need NO operator greenlight: the owning agent
adopts the direction under P/D + ledger and builds. **Goal-level promotions**
(per the bar above) need the operator's greenlight — file the sub-ticket `target=operator`
AND lean-surface it (it won't auto-deliver). Once a goal-level/ratifiable-tier
artifact is approved and built, it files its own `intent=ratify` ticket
(`--ratifiable-file pantheon-config:<path>`) — and only *then* does it appear in
the operator's ratify queue (mirrors the ORG-022 file-tier pairing the tooling
enforces). A below-bar artifact terminates at **Adopted**, no ratify step.

## Common failure modes

- **Monolithic menu ticket.** Unreviewable; auto-bounces if `intent=ratify`
  (no single ratifiable artifact). File an index + children instead.
- **Pre-filing a ratify ticket for an unbuilt artifact.** The ratify ticket has
  no `--ratifiable-file` to point at. Greenlight first (step 4), build, then
  ratify.
- **Grouping distinct decisions to "reduce noise."** Defeats the purpose — the
  operator must be able to act on each independently. Group only same-class
  bookkeeping.
- **Parking a below-goal "decision" as `target:operator`.** The msg-3733 rot
  root-cause: it's mislabeled (below-bar = ADOPT, not operator-decide) AND
  `target:operator` has no auto-delivery surface, so it sits unseen forever
  (T-1195–1198 sat until Orchestrator's mailbox flag found them). Adopt it + route the
  build to the owning agent instead.
- **Filing a goal-level decision to `operator` with no lean-surface.** The ticket
  is correct but nearly invisible — `target:operator` has no *prompt* delivery.
  T-2199's digest slot is a weeks-deep safety net, not delivery. Always pair a
  goal-level filing with a Telegram/digest surface; the ticket is the record,
  the surface is delivery.
- **Hand-relaying without writing the receipt.** You told him; the digest does
  not know that, so it tells him again inside 7 days. Stamp it:
  `ticket.py annotate <tid> --note "lean-surfaced; ..."`.

## Cross-references

- ORG-012 (information-inflow bubble-up methodology) — Cadence #6 is the sweep
  this SOP files the result of.
- `sop/standard/org-012-per-agent-portions.md` — who operationalizes which
  §portion of ORG-012.
- T-0780 — the manual first-run; the menu that prompted this SOP.
- T-0781 — the scripted/periodic version of the sweep (ORG-012 Phase C); its
  auto-filer is bound by this SOP's emit format (index + sub-tickets, not a
  rolling menu).
- T-0556 — why an `intent=ratify` menu auto-bounces (no single ratifiable
  artifact).
- msg-3733 / amended-D3 / `rulebook/policies/goal-mapping.md` — principles-as-ratification:
  the goal-level bar that decides operator-route vs owning-agent-adopt.
- T-1195–T-1198 + T-1205 — the rot this amendment fixes (sweep greenlights parked
  as `target:operator` with no auto-delivery surface, unseen until Orchestrator's mailbox
  flag). T-1215 — this filing-rule amendment; Orchestrator's companion fix was the
  delivery-surface half.

---

**Status:** Drafted 2026-06-05 by Recovery per the operator's directive (Telegram, T-0780 breakout); the original
header carried a contradictory "Ratified … / Awaiting ratification" pair (resolved here). **Amended + adopted
2026-06-16 (Recovery, T-1215) per principles-as-ratification (msg-3733 / amended-D3):** reconciled the routing rule
so only **goal-level** decisions route to `operator` (and those require an explicit lean-surface, since
`target:operator` has no auto-delivery), while **below-goal** "decisions" are ADOPTED by the owning agent +
build-routed + ledgered — the fix for the T-1195–1198 rot Orchestrator's mailbox flag surfaced. This SOP is itself
methodology (below the goal-level bar), so the amendment is adopted-as-proven, not held for a ratify-stamp.
