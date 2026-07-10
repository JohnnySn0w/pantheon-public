# Ticket flow — lifecycle, queues, and target/assignment discipline

Authored 2026-06-02 (T-0727 thread). Canonical reference for how tickets move through the system and — the load-bearing part — **who touches a ticket at each step**. Surfaced when HO `claim`ed a `target=operator` ratify ticket (wrong verb: `claim` is worker-takes-work; it misattributed the claim and pre-empted the operator's own step).

## States (ORG-019)

`inbox/ → active/ → done/`. Three directories; the directory IS the state. No `blocked/` dir — blocks are frontmatter only (`not_before:` calendar gate, or `depends_on:` dep gate; ORG-019). The lane shown in the PWA is derived from the directory.

## Verbs (`tools/ticket.py`)

`file` (→inbox) · `claim` (inbox→active) · `unclaim` (active→inbox) · `block` (set `not_before`/`depends_on` in place) · `resolve` (→done) · `retarget` (change `target`) · `close-noodle` / `close-action` (the operator's noodle/action close verbs) · `list`. All commit + push by default (`--no-push` to skip).

## `target` = assignment

`target` names who actions the ticket. The PWA surfaces **all** tickets sorted by status; a ticket is "assigned to" whoever its `target` is. `requester` is who filed it (audit only). Role IDs (`orchestrator`/`specialist`) normalize to persona names (`orchestrator`/`specialist`/`recovery`) on write; `operator` is the operator.

## The load-bearing rule: who claims what

- **Agent-targeted tickets (`target: orchestrator|specialist|recovery`)** — the per-agent inbox-consumer (`pantheon-inbox-consumer@<agent>.timer`) auto-delivers; the agent `claim`s → works → `resolve`s. **One active ticket per agent** is enforced structurally: T-0717's mutex stops *auto-dispatch* while an active ticket is held, and the T-0877 cap in `ticket.py claim` refuses a *manual* claim on the same condition (overridable via `PANTHEON_MAX_ACTIVE_TICKETS` for deliberate exceptions; `operator` is exempt). To take new work, first `resolve`, `retarget`, or `unclaim` the active one. A ticket blocked on the operator's decision should be `retarget`ed to `operator` (his lane), not parked in your active.

- **operator-targeted tickets (`target: operator`)** — HO/agents **file** them and **leave them assigned**. They land in `inbox/`, surface in the operator's queue (assigned via `target=operator`), and **the operator claims them himself** when he actions/ratifies — his claim records `[operator] claimed; moving to active`.
  - **HO MUST NOT `claim` a `target=operator` ticket.** `claim` is the worker-takes-this-to-work verb; HO claiming one misattributes the claim (`[claude] claimed`) and pre-empts the operator's step. There is no "assign-to-operator" verb because none is needed — `file` with `target: operator` already assigns it. After filing, HO's hands are off until the operator resolves it (or retargets it back).
  - If HO mistakenly claimed one: `unclaim` it back to inbox (target stays `operator`), then leave it.

## `intent` (target=operator only; T-0465)

`action` (operator do-X; default) · `ratify` (decision approval; **requires `ratifiable_files`** pointing at a committed file diff per ORG-015 — the PWA ratifies a diff, not prose) · `noodle` (chat-anchor; closed via `close-noodle`, optionally `--spawn-to` a child action ticket). Intent is not set for non-operator targets.

## Fan-out (D9 / ORG-019 / ORG-013)

Child tickets with non-empty `depends_on` file into `inbox/` with the dep gate set in frontmatter — they stay parked (grooming + consumer skip them) until the dep resolves. On parent `resolve`, ORG-013 cascade-unblock removes the resolved tid from each dependant's `depends_on` in place. `not_before` is reserved for **real calendar dependencies** only (release windows, scheduled events) — not HO-side pacing.

## Cross-references

- `adr/ORG-019-blocked-state-retirement.md` — frontmatter blocks, no `blocked/` dir
- ORG-013 (cascade-unblock) · ORG-015 (ratifiable_files) · T-0465 (intent) · T-0717 (one-active-ticket mutex)
- `rulebook/directives.md` D9 (orchestrator response contract + ticket discipline)

## Authoring discipline

Hand-curated per D2. HO drafts; ratified by the operator. Updates ride alongside the lifecycle lessons that surface them.
