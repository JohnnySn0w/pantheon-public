---
aliases: [ORG-003]
title: "Ticket system: format, lifecycle, and grooming"
status: Ratified 2026-05-02
date: 2026-05-02
# re_evaluate: machine-checkable mirror of this ADR's inline re-eval prose,
# consumed by tools/adr_reeval_walk.py (T-2527) during the weekly-review walk.
# type: calendar|metric|event ; fires: auto-ticket (deterministic only) | surface.
# Backfilled per T-2530 (inline-only trigger was previously uncaught).
re_evaluate:
  - id: R1
    type: event
    when: "a second project comes online"
    fires: surface
    text: "v0 assumes a single shared ticket tree; revisit shared-vs-per-project ticket trees when a second project comes online"
build_disposition: built
---
# ORG-003 — Ticket system: format, lifecycle, and grooming

**Status:** Ratified 2026-05-02 by the operator (Telegram, msg_240).
**HITL:** Ratified.
**Date:** 2026-05-02.
**Justifies via:** P3 (decisions justify upward — tickets become the structural surface for cross-instance asks), P4 (traceability — ticket transition logs are the audit chain for delegated work), P1 (the operator's bandwidth — replaces "every ask routes through Telegram" with polled queues).

## Context

Pantheon's cross-instance work currently requires the operator to relay each ask: "Recovery, can you...?", "Science-team needs to...". Every routing is a bandwidth tax that scales linearly with org activity (P1 violation). Subagent dispatches inside HO are tracked only in transient subagent transcripts — if a dispatch crashes mid-flight, the brief and any iterations are lost (P4 violation).

The 2026-04-29 orchestration survey (`knowledge/episodic/2026-04-29-orchestration-survey/synthesis.md` §4 Tier 2) named WORKFLOW.md-style state-machine artifacts as the validated borrow from OpenAI Symphony. Symphony's tracker (Linear) is the durability layer; the orchestrator polls and sorts. Pantheon's gap is the tracker — Telegram-driven dispatch has neither durability nor poll-based discovery.

The operator's 2026-05-02 design exchange (msg_223 → msg_236 → msg_238) settled the shape:
- Substrate-agnostic — filesystem on git, not Linear or GitHub Issues. Ports across hosts. Single audit chain with the ledger and ADRs.
- Unified for cross-instance asks AND subagent tracking — not just "Recovery, please...". Anything BACKLOG-tier, multi-step, or worth resuming gets a ticket; one-shot subagents stay inline.
- Cleanup agent runs as Recovery, not as a new user (Recovery already has the recovery role and tmux-send capability; adding a cron'd grooming responsibility is cheaper than spinning a fourth user).

This ADR ratifies the format, lifecycle, and grooming policy.

## Decision

### Storage and layout

`pantheon-config/tickets/` directory, git-tracked. State is encoded by sub-directory; transitions are `git mv` operations:

```
tickets/
├── README.md          # spine; link to this ADR; format reference; current-state index
├── inbox/             # filed; awaiting target's pickup (also: time-gated or dep-gated via frontmatter)
├── active/            # claimed; an agent is actively working
└── done/              # resolved
```

Closed tickets remain in `done/` indefinitely at v0; archive policy deferred until volume justifies it (P2 — don't build the archiver before the manual cost is real).

### File format

One file per ticket: `T-NNNN.md`. Name is `T-` prefix + four-digit zero-padded sequence number, parallel to `ORG-NNN` for ADRs. Example: `T-0001.md`.

Each file: YAML front matter + Markdown body. Front matter is the structured surface; body is the conversation + transition log.

```markdown
---
id: T-NNNN
created: 2026-05-02T01:30:00Z
requester: orchestrator
target: recovery
state: inbox
parent_ticket: null
depends_on: []
labels: [eviction-remediation, watch-loop]
stale_after: 4h
---

# <one-line title>

## Ask

What the requester is asking for. Concrete deliverable. Acceptance criteria.

## Context

What the target needs to know to act on the ask. Pointers to relevant files, commits, prior tickets.

## Transition log

- 2026-05-02T01:30:00Z [orchestrator] filed in inbox
```

### Front-matter field semantics

| Field | Required | Notes |
|---|---|---|
| `id` | yes | `T-NNNN`. Globally unique within `tickets/`. Monotonic at filing. |
| `created` | yes | ISO 8601 UTC. Time of filing, never updated. |
| `requester` | yes | Controlled vocabulary, same as ledger `agent` field: `orchestrator`, `human:operator`, `recovery`, `specialist`, `worker:implementer`, `worker:reviewer`, `project:<slug>:orchestrator`. |
| `target` | yes | Same vocabulary. The instance/agent expected to claim. May be a class (`worker:implementer`) for "any available worker." |
| `state` | yes | One of: `inbox`, `active`, `done`. Must match the parent directory. Inconsistency = grooming agent flag. (Note: `blocked` state retired per ORG-019 2026-05-19.) |
| `parent_ticket` | optional | Ticket ID this descends from (e.g., a re-dispatch of a closed ticket). `null` for roots. |
| `depends_on` | optional | Array of ticket IDs that must reach `done` before this one becomes eligible. Tickets with pending deps stay in `inbox/`; cascade-unblock removes satisfied deps from frontmatter when parents resolve (ORG-013). Empty array if none. |
| `labels` | optional | Free vocabulary. Used for grouping and the `tickets/README.md` index. |
| `stale_after` | optional | Override the default staleness threshold. Format: `<N>h` or `<N>d`. Default: `4h` for `active`; never for `done`. |
| `not_before` | optional | ISO 8601 UTC datetime. Inbox tickets with `not_before` in the future are skipped by the inbox auto-consumer (vocab `not_before_unmet`) and surface in grooming digests as time-locked. Format: `YYYY-MM-DDTHH:MM:SSZ` (full datetime) or `YYYY-MM-DD` (bare date, normalized to `YYYY-MM-DDT00:00:00Z` at write time). Empty/absent = no time gate. CLI: `ticket.py file --not-before <value>`. Validated at write time only; malformed values stored via hand-edit are treated as missing (fail-open) by the consumer, and the ticket becomes eligible. Added 2026-05-04 by T-0061; design ratified at T-0060. |

### Lifecycle

1. **Filing.** Requester writes `tickets/inbox/T-NNNN.md` with `state: inbox`, body and transition log seeded. Optionally set `not_before: <ISO ts>` (calendar gate) or `depends_on: [T-NNNN, ...]` (dep gate). Commits + pushes.
2. **Claiming.** Target moves the file `inbox/ → active/` via `git mv`, updates `state: active`, appends a transition-log entry `[<target>] claimed; moving to active`. Commits + pushes.
3. **Waiting (calendar or dep gate).** A ticket needing a gate stays in `inbox/` with `not_before` or `depends_on` set in frontmatter. The inbox-consumer skips it until the gate clears; no separate state. When a dep ticket resolves, the ORG-013 cascade removes the dep from frontmatter automatically.
4. **Resolution.** Active agent moves `active/ → done/`, sets `state: done`, appends transition log `resolved` with optional summary. Commits + pushes.
5. **Stale detection (grooming).** Daily cron'd grooming agent (Recovery) scans `tickets/active/`, compares time since last transition-log entry against `stale_after`. Stale tickets get an appended transition-log entry `[grooming] stale (no activity since <ts>); flagging for review`. Grooming does NOT auto-move; surfacing is the action. The next human read decides resume / re-dispatch / close.

All state transitions are `git mv` followed by an in-file edit (front matter `state` field + transition log append) in the same commit. The git history is the immutable transition record; the in-file log is the human-readable form.

**Note:** The `blocked/` state and `block`/`unblock` lifecycle steps were retired per ORG-019 (2026-05-19). See ORG-019 for the rationale and ORG-013 (amended) for the updated cascade-unblock mechanics.

### Transition-log format

Append-only, one entry per line, in the body's `## Transition log` section:

```
- <ISO ts> [<actor>] <action verb>; <free-form detail>
```

- `<actor>` matches the `agent` controlled vocabulary, OR is `[grooming]` for the cleanup agent.
- `<action verb>` examples: `filed`, `claimed`, `resolved`, `stale`, `commented`.
- Detail is freeform; aim for one line, expand to multiple bullets if needed for substance.

### Grooming agent (Recovery)

Runs as a cron'd dispatch on `claude-fallback`, daily at 12:00 UTC (interleaved with specialist's 08:00 UTC watch fire to avoid dispatch contention). Implementation: `tools/ticket_grooming.py` in pantheon-config, structurally similar to `watch_runner.py`:

- Reads every file under `tickets/active/`.
- For each ticket: parses transition log, extracts last-entry timestamp, compares to `stale_after` (or default 4h).
- For each stale ticket: appends a transition-log entry with the grooming flag.
- Auto-commits and auto-pushes per the same pattern as `watch_runner.py` (HERMES guard, narrow scope to `tickets/active/`, plain push, no force).
- Sends a digest summary via Bot API direct-curl identifying stale tickets, similar to `watch_digest.py`.

Recovery's responsibilities expand to: existing recovery + new grooming. Both responsibilities are cron'd or event-driven; Recovery is otherwise idle. No conflict with the recovery role.

### Tooling

`tools/ticket.py` CLI provides the lifecycle operations:

- `ticket.py file --requester <X> --target <Y> --title "..." [--labels A,B] [--depends-on T-NNNN,...]` — creates `inbox/T-NNNN.md` with auto-incremented ID. Stages + commits with template `tickets: file T-NNNN — <title>`.
- `ticket.py claim T-NNNN` — moves `inbox/ → active/`, updates front matter, appends log. Stages + commits with template `tickets: claim T-NNNN`.
- `ticket.py block T-NNNN [--not-before <ISO ts>] [--depends-on T-NNNN ...]` — sets `not_before` or `depends_on` in frontmatter (in-place; no file move). (Retired: `--reason`, `--blocked-by`, `--unblock-when`, `--unblock-check`; state-change `block/unblock` replaced by frontmatter-only gates per ORG-019.)
- `ticket.py resolve T-NNNN [--with "summary"]` — moves `active/ → done/`.
- `ticket.py grooming` — runs the staleness scan; intended for cron, also runnable manually for testing.

Each operation is a single commit with narrow scope (only the ticket file moved). Same auto-commit discipline as `watch_runner.py`: HERMES guard, scope filter, plain push.

### Interaction with existing artifacts

- **BACKLOG.md** is the strategic intent registry; **tickets/** is the operational work surface. One BACKLOG item can spawn many tickets over its lifecycle. They are complementary, not redundant. ID schemes (`B-N` vs `T-NNNN`) stay distinct.
- **Ledger entries** about ticket activity reference ticket IDs in `links` (`{"type": "ticket", "ref": "T-0001"}`). HO continues to log substantive ticket transitions to the ledger.
- **Subagent dispatches** that meet any of (BACKLOG-tier, multi-step, cross-instance, worth resuming) get a ticket; one-shot dispatches stay inline via the Agent tool. The threshold question for choosing: "is this worth a file in the repo?"
- **Telegram remains the human-in-the-loop channel** for HO ↔ the operator. Tickets are the inter-agent surface, not the the operator surface. Tickets that need the operator are filed in `inbox/` with `target: operator` and surfaced via digest.

## Consequences

**Positive:**
- Cross-instance asks no longer require the operator as router. HO files; Recovery polls. Same shape for any future user.
- Subagent dispatches gain durability — brief + each iteration's response live in one ticket file rather than scattered across transient subagent contexts. A crashed implementer is recoverable.
- Single audit chain (git history of `tickets/`) for every delegated work unit. Backs up P4 by structural enforcement, not discipline alone.
- Stale detection prevents the orphan failure mode (claimed-but-never-completed tickets). Surface-not-action keeps live work safe from auto-closure.
- Substrate portable: the system survives a git-host migration unchanged.

**Negative:**
- Adds a write surface that must respect the auto-commit discipline (HERMES guard, narrow scope, no force). Mitigation: the discipline is already established in `watch_runner.py`; `ticket.py` reuses it.
- Stale-threshold default (`4h` for active) is a calibrated guess. Mitigation: `stale_after` field per ticket lets us override the default any time; defaults can be revised in an ADR amendment. (`blocked/` state and its `5d` default retired per ORG-019.)
- Grooming agent + watch loop both run on cron'd Claude Code subprocesses. Each fire briefly evicts the prime Telegram bot under the v2.1.101+ classifier (`knowledge/episodic/2026-04-29-anthropic-flagging-diy-harnesses/notes.md`) — but path-(c) closes that issue: each user runs in its own session, no plugin sync. Confirmed working post-2026-05-02.
- More git noise (each transition is a commit). Acceptable — the noise IS the audit value, same as ledger.

**Open questions deferred:**
- Whether project-tier orchestrators (Echo, future) write to a shared `pantheon-config/tickets/` or per-project ticket trees. v0 assumes single tree; revisit when a second project comes online.
- Auto-archive of `done/` tickets after N months. Defer until volume noise justifies the rule.
- Webhook / push-notification mechanism if poll-based discovery proves too slow for some lifecycle. Defer until evidence of friction.

## Related

- ORG-001 (ledger format) — same append-only / git-tracked / single-source-of-truth shape.
- ORG-002 (principles + directives split) — establishes the two-tier framework that justifies-via fields point at.
- BACKLOG B7 (ticket system) — this ADR closes the design portion of B7; implementation is the follow-on.
- `knowledge/episodic/2026-04-29-orchestration-survey/synthesis.md` §4 Tier 2 — Symphony WORKFLOW.md pattern as origin.
