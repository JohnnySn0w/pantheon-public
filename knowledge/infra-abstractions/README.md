---
title: Infra-abstractions reference map
status: Redesigned 2026-06-11 by Orchestrator (T-1076, on Recovery's operator-approved structure msg 3117, sharpened; original draft Recovery T-0999). Ratified 2026-06-12 by the operator (T-1040 — "Beautiful collection of terms").
purpose: A living, DESCRIPTIVE catalogue of the recurring infrastructure abstractions across all three agents (Orchestrator/Specialist/Recovery) + the shared substrate. Answers "where does a new tool fit / what pattern should it follow" deterministically. NOT an ADR — describes how Pantheon's infra is shaped today, not a decision about how it must be.
---

# Infra-abstractions reference map

Pantheon runs **~30 standing components** (timers, daemons, hooks) across three agents plus
the shared substrate, and a tail of on-demand tools. They are classified here by **one
axis — the work signal**: *what does a firing of this component mean?* Every component
has exactly one work signal, so placement is deterministic; the old per-family richness
survives as **subtypes** (cut by effect) inside each general class.

## The four generals — classification by work signal

| General | The work signal is… | Mechanical test |
|---|---|---|
| **[SCHEDULED](scheduled.md)** | **time itself** — the clock tick is the reason to act ("it's noon → scan/groom/summarize") | With every queue empty and no upstream state, does the fire still do its whole job? **Yes** → Scheduled. |
| **[HOOKED](hooked.md)** | **an intercepted action** — the harness fires it at an action/lifecycle boundary | Is it wired as a hook (settings.json, git hook) around someone else's action? **Yes** → Hooked. |
| **[REACTIVE](reactive.md)** | **a handoff artifact** — a thing a *nameable upstream component* deliberately produced for it (ticket in inbox, mailbox file, tombstone, limit-hit state file) | Can you name the producer component and the artifact? **Yes** → Reactive — *regardless of whether the wiring underneath is a polling timer or an inotify watch; the timer is transport, not signal.* |
| **[INVOKED](invoked.md)** | **a decision** — a human or agent runs it; no standing wiring fires it | `grep` the unit files, crontabs, hooks: nothing fires it? → Invoked. |

These four are exhaustive by construction: every execution starts for one of these
reasons — the time arrived, an action was intercepted, a handoff arrived, or somebody
decided. They are mutually exclusive via the tests above, applied in order:
**hook wiring? → HOOKED. Nameable producer+artifact? → REACTIVE. Standing timer whose
tick is itself the job? → SCHEDULED. Nothing fires it? → INVOKED.**

The deepest of the three tests is REACTIVE's: most reactive components are *implemented*
as polling timers (the inotify path is structurally dead under the group-gated hub — see
[user-systemd group constraint](cross-cutting-patterns.md#user-systemd-carries-only-the-primary-group)),
so wiring alone would misfile them as Scheduled. The producer test cuts cleanly:
`ticket_grooming`'s noon fire scans whatever exists (time-driven); `inbox_consumer`'s
10-minute fire is meaningless unless an agent *filed something for it* (handoff-driven).
A Monitor's watched signal (context %, pane state, channel liveness) is **exhaust, not a
handoff** — nobody produced it *for* the monitor — which is what keeps Monitors in
Scheduled and consumers in Reactive without judgment calls.

## Subtypes — the effect cut, inside each general

- **SCHEDULED** → `Monitor` (watch a world signal → alert/poke) · `Filer` (discover due/stale → file the work item) · `Reconciler` (discover inconsistency → fix in place, heavy write contract) · `Digester` (aggregate → human-facing summary) · `Runner` (no gate at all — the whole job runs unconditionally every fire).
- **HOOKED** → `Blocker` (refuse/allow the action) · `Injector` (add context/warning to it) · `Recorder` (persist state about it for a downstream Reactive consumer).
- **REACTIVE** → `Deliverer` (hand the artifact's work to an agent) · `Repairer` (execute the repair the artifact requests).
- **INVOKED** → `Front-door` (the clean entry point humans/agents call) · `One-off` (migrations, kept for repeatability) · `On-demand tool` (no trigger by design).

The **bail-and-poll litmus** (from the tightness review) separates the Scheduled
subtypes that gate from the one that doesn't: predicate-gated (bail when nothing's due)
→ Monitor/Filer/Reconciler/Digester per its effect; unconditional-every-fire → Runner.

## Handoff chains — composition is expected, not an overlap apology

Components compose into **producer → artifact → consumer chains**; the chain is the
unit of behavior, the components stay singly-classified. The standing chains:

| Chain | Producer | Artifact | Consumer |
|---|---|---|---|
| **Resume loop** | `ho_stop_failure` (Hooked/Recorder) | limit-hit state file | `ho_resumer` (Reactive/Repairer) — with `usage_monitor` (Scheduled/Monitor) surfacing the same condition for humans |
| **Restart loop** | `restart_request` (Invoked/Front-door) *or* `context_fill_monitor` (Scheduled/Monitor) | restart tombstone | `restart_drain` (Reactive/Repairer) → executes `handoff_and_restart.sh` |
| **Ticket flow** | agents + Scheduled/Filers | `tickets/inbox/` items | `inbox_consumer` (Reactive/Deliverer) |
| **Mail flow** | agents (`mailbox.py`) **+ repointed producers** (T-1826/T-1827: `keep_going` nudges where `DELIVERY_MODE=mailbox` — specialist first; `inbox_consumer` pokes follow at R2) | hub `new/` files | per-agent delivery layer: `mailbox_wake` pane-poke (classic) or `acp_bridge` native turn-intake (specialist, archivist) |

The old map described the resume loop in three places and anchored it nowhere; in this
model a chain is named once, here, and each member's file links back.

## How to use this map

1. **Adding a tool?** Run the placement algorithm above (hook? producer? tick-is-the-job? nothing?). Open that general's file; pick the subtype by effect; follow its contract notes. Then check [`cross-cutting-patterns.md`](cross-cutting-patterns.md) — the contract layer (bail-and-poll, default-safe, idempotent filing, auto-commit discipline, …) spans all four generals and is read-first.
2. **Looking for what exists?** Each general's file carries the component inventory with per-component trigger/action/notes. Many "new" needs are a config knob on an existing component.
3. **Wiring chains?** A new producer→consumer pair gets a row in the chains table above.

This is a **catalogue, not a gate.** Where a specific pattern is ratified as binding,
that becomes a narrow ADR referenced from here.

## Census honesty

The standing count is **~28–30 logical components** (templated `@`-units counted once),
not the "~40" the first draft claimed: on-demand tools, one-off migrations, and retired
units are fenced in [INVOKED](invoked.md)/retired notes rather than padding the standing
census. Known per-account wiring drift is flagged inline (hooks are wired per-account in
each agent's `settings.json`; presence in this map ≠ active everywhere — e.g.
`backlog-surface` is absent from Recovery's live SessionStart array).

## Scope notes

- **Shared libraries are not components** but are load-bearing: `_git_utils.py` (byte-guard + git primitives), `_lease.py` (flock leases), `_pane_predicates.py` (tmux pane state), `mailbox.py`, `ledger_shard.py`, `agent_role_map.py`. Each general's file names the libs its members lean on.
- **The systemd layer** (`tools/systemd/`) is the *delivery* mechanism — `.timer`+`.service` pairs, templated `@` units. See [`../../tools/systemd/README.md`](../../tools/systemd/README.md) for the unit inventory and timer-anchor discipline; this map references cadences, the units are the source of truth.
- **Out of scope: the interactive dispatch/orchestration surface.** Worktree staging, plan-first dispatch, TeamCreate/project-orchestrator, SendMessage, chain-of-justification — an interactive agent-driven process owned by the directives + SOPs (`rulebook/directives.md` D6/D7, `sop/standard/`, CLAUDE.md), not a standing component class. Its standing *remnants* that do fire on their own (`ho_resumer`, `keep_going`, `kick_verify`) are catalogued here in their generals.

## Relationship to the glossary

[`../pantheon-glossary.md`](../pantheon-glossary.md) names the *things* (personas, daemon
sessions, paths, historical vocabulary); this map catalogues the *component classes and
patterns*. They cross-reference; they do not duplicate.
