---
aliases: [ORG-106]
id: ORG-106
title: "Seat pause and deep sleep: one HC-owned seat state, always owned, reasoned and expiring by default"
status: "Ratified 2026-11-09 by the operator via Telegram 13198 (proposed 2026-10-26 by Orchestrator under T-5438 per the operator TG 12269; Decision 9 substance approved TG 12744)."
date: 2026-10-26
author: Orchestrator (head_engineering_orchestrator)
steward: orchestrator
appetite: 5
riskiest_assumption: "That every path which can hand a seat work or relaunch it can be routed through one reader without missing one. The inventory found that keep_going honours none of today's holds. One more unrouted injector or launcher (a codex poller start, a Recovery restore, a peer's tmux send-keys) turns DEEP-SLEEP into a sleep that wakes itself, or PAUSED-INTAKE into a pause that still delivers work."
build_disposition: needs-build
scoping_ticket: T-5438
relates: ORG-076, ORG-077, ORG-104, ORG-019, ORG-089
no_registry_row_waiver: "Operator-plane control for the already-registered harness-controller and health-plane components. Registers no new component; the record, the verbs and the reader are additions to tools/harness.py, tools/harness_state.py and the existing consumers."
ratifiable_files: [pantheon-config:architecture/adr/ORG-106-seat-pause-and-deep-sleep.md]
---

# ORG-106: Seat pause and deep sleep

**Status:** Ratified 2026-11-09 by the operator (TG 13198). Proposed 2026-10-26 (T-5438, the operator TG 12269). Decision 9 added 2026-11-02 (T-5868); the operator approved its substance (TG 12744). Edited 2026-11-02 for the operator's review questions (TG 12811): plain-language wording, early wake by system orchestrators (Decision 3) and a 15-minute HALTED ticket (Decision 9). Mechanism: [notes/2026-10-26-org106-seat-state-mechanism.md](../../notes/2026-10-26-org106-seat-state-mechanism.md).

## Decision

1. **One seat state, owned by the harness controller.** Each seat has at most one operator record saying which state it is in: ACTIVE (no record), PAUSED-INTAKE or DEEP-SLEEP. The record lives with the seat's own controller state, next to its runtime holds. It is the only place where a person or agent pauses or sleeps a seat. Every consumer asks one reader for the seat's effective state, and none keeps a private flag.
2. **PAUSED-INTAKE stops new work, not the seat.** The session stays up and healthy, and nothing hands it work unprompted: no ticket delivery, no keep-going nudges, no queue pokes and no wake for ordinary peer mail. Mail and tickets queue. The operator's direct messages, urgent mail, monitoring, repair, context restarts and cap-hit swaps all continue.
3. **DEEP-SLEEP takes the seat down and keeps it down.** The seat writes its handoff, then its session ends, and the end does not count as a failure. While the seat is asleep, no runtime launches on either control plane (the codex fallback included), and the system orchestrators do not treat it as broken or restore it on their own. A system orchestrator may wake it early, with a ledger entry, when the sleep's reason no longer holds or its owner can't be reached; waking never needs the operator in person. Mail queues for the wake. Monitors still look at the seat, but they report it as asleep rather than down, so nothing pages and nothing repairs it. If the sleep fails to hold and a session comes back, that is a real fault and it pages. (In short: the seat finishes and hands off its work, then runs no session at all until it is woken.)
4. **Every pause has an owner, a reason and an expiry.** The verb refuses a pause without a reason. Expiry is the default. Past its expiry a record reads as ACTIVE, so a forgotten pause fails toward a running seat. The owner is told before expiry and can extend it. A pause with no expiry has to be asked for explicitly. It shows as such on kino Health, with its age, and it is flagged when it gets old. The OOM hold keeps its ratified release-only rule (ORG-104 rule 2).
5. **One verb surface, run as the seat.** The verbs are `harness <seat> pause | sleep | wake | status`, beside the existing hold and restart verbs. They run as the seat account (the T-5483 guard) and land a ledger entry like every harness override. They are idempotent: setting the state a seat is already in refreshes the record, and waking an active seat does nothing. The Telegram harness surface gets the same verbs. A kino toggle can follow later through the existing dropbox pattern.
6. **Visible where the operator looks.** kino Health shows each seat's state, owner, reason and time left. A paused or sleeping seat is an expected state, not an incident.
7. **Wrap the old holds first, then retire them one at a time.** From day one the reader folds today's holds into the effective state, so consumers move onto the reader before any writer changes. The per-account off switch is retired in favour of PAUSED-INTAKE. Seat maintenance (T-5282) and the OOM hold (ORG-104) keep their writers and their meanings, and are read through the reader. The narrow, single-purpose suppressions stay as they are: alert mute, context-fill hold, wedge suppress, runtime holds, the hand-move poller hold and the bootstrap bypass token. The per-hold disposition and the migration order are in the mechanism note.
8. **Seat state is intent; cooldowns are facts.** A cap-hit cooldown says a runtime can't run. A seat state says the operator doesn't want the seat to run. The controller launches only when both allow it, and neither clears the other. The one-time maintenance override an operator can put in a seat-move plan, to start a seat during maintenance, never overrides DEEP-SLEEP.
9. **A pause that stops partway is a fault, not a pause.** *(Added 2026-11-02, T-5868; substance approved by the operator, TG 12744.)* When an automated pause (the harness-manager run-state pause, T-5858) halts partway, the seat is left with some wake paths off and its run waiting for rollback. That seat is in a third recorded state, HALTED, never PAUSED-INTAKE and never ACTIVE. HALTED blocks new work and withholds repair like a pause does, since rollback is the repair. It is never an expected state, though: every monitor reports it and it pages its owner and the recovery seat once, and if it is not cleared within 15 minutes a ticket is filed automatically to the system orchestrators. It does not expire, because a halted record that fell back to ACTIVE would hide a seat that cannot wake. Only the run's own rollback clears it, or an operator clearing it after recovering the seat by hand, which lands a ledger entry like every override.

## Why

The operator, TG 12269: "we do want a way to pause/deep sleep a seat." The T-5438 inventory found thirteen hold-like mechanisms, each in its own place. Each was built for one incident, and they differ in strength, ownership and expiry:

- **The off switch outlived its reason by ten weeks.** orchestrator's `autonomy_off` was set on 2026-08-13 for a weekend stand-down. It blocked every inbox cycle until T-5435 found it on 2026-10-24. Its reason was prose; it had no owner field and no expiry.
- **A standing marker masked three seats for days.** `expected_policy_skip` markers cited causes that had gone stale and hid real consume blocks (T-5282, T-5311).
- **The holds disagree about what "held" means.** Some stop intake, some stop repair, some stop launches, and one only relabels a disabled unit as "paused". keep_going honours none of them, so a held seat still gets keep-going nudges.
- **Most holds can't be seen.** kino shows alert mutes and runtime viability. It does not show maintenance, the OOM hold or the off switch.

The one seat-wide mechanism that has never gone stale is seat maintenance, because it always expires. That is the property to make universal.

Decision 9 comes from Recovery's D15 ruling on T-5858. A pause that times out while draining leaves the seat's wake timers off, so a mailbox-first seat stops waking for mail while its services still look up. Recording that as PAUSED-INTAKE would tell every monitor it is an authorised pause and silence them. Clearing the record would make a later restore read the seat as never paused.

## Alternatives considered

- **Add another flag for "pause".** This is how the current thirteen arose.
- **Build the pause from runtime holds (hold every runtime).** This blocks launches but says nothing about intake and has no expiry. It also leaves kino unable to tell a sleeping seat from one whose runtimes are all broken.
- **Extend seat maintenance into the pause.** Maintenance means "an operator is working on this seat, so don't repair it". A sleeping seat is the opposite: it should be watched. Maintenance's 4-hour cap is right for a cutover and wrong for a sleep.
- **Put the record in a shared cross-account registry.** That lets any account pause any seat, and it separates the authority from the controller that has to enforce it. Recovery's monitors already read per-seat state across accounts (the cap detector's file), so a shared location buys nothing.
- **No expiry; rely on review.** That is the `autonomy_off` failure.
- **Mark a halted pause with a `partial` flag on a PAUSED-INTAKE record (Decision 9).** Every reader would have to learn to inspect the flag, and one that missed it would fail silent, treating the seat as an authorised pause. A distinct state fails loud: a reader that doesn't know HALTED sees an invalid record, which is logged and paged (Consequence 1).

## Consequences

- One reader becomes a dependency of every intake path and every launch path. A fault in it fails toward today's behaviour: an unreadable record reads as ACTIVE, and the fault is logged and paged rather than silently blocking or silently waking.
- A seat with a Telegram receiver loses inbound messages if it sleeps past Telegram's 24-hour update retention. The verb warns, and it needs an explicit acknowledgement before it allows a longer sleep on such a seat.
- Waking is not instant. A woken seat relaunches on the next consume fire and reads its handoff.
- Retiring the off switch changes `rulebook/policies/autonomy.md`, which is ratified. That edit goes to the operator with this ADR.
- The mechanism is in [the mechanism note](../../notes/2026-10-26-org106-seat-state-mechanism.md) and the build tickets, not here: the record schema, state machine, reader contract, per-consumer behaviour, per-hold disposition, TTL values and migration order.
