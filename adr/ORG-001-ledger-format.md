---
aliases: [ORG-001]
title: Decision ledger format and storage
status: Ratified 2026-04-28
date: 2026-04-28
# re_evaluate: machine-checkable mirror of this ADR's inline re-eval prose,
# consumed by tools/adr_reeval_walk.py (T-2527) during the weekly-review walk.
# type: calendar|metric|event ; fires: auto-ticket (deterministic only) | surface.
# Backfilled per T-2530 (inline-only trigger was previously uncaught).
re_evaluate:
  - id: R1
    type: event
    when: "a second project comes online"
    fires: surface
    text: "v0 assumes a single shared ledger; revisit ledger-per-project vs periodic-merge when the second project comes online"
build_disposition: built
---
# ORG-001 — Decision ledger format and storage

**Status:** Ratified 2026-04-28 by the operator (Telegram).
**Amended 2026-05-29** (scope-D, operator-blessed per T-0148 / ORG-024 gap-1): added §Sub-year sharding (D10) — the single-year-file choice in §Format is superseded by weekly ISO-week subfolders. See that section.
**HITL:** Ratified.
**Date:** 2026-04-28.
**Justifies via:** P4 (the ledger IS the traceability mechanism), P3 (the schema's `justifies_via` field is the structural enforcement of upward justification). *Originally written 2026-04-28 referencing pre-ORG-002 P5/P4 numbering; updated in place to new numbering 2026-04-28.*

## Context

P4 ("Traceability is non-negotiable") requires every decision that shaped action to land in a ledger. P3 ("Decisions justify upward") requires every entry to name the principle(s) it serves. Without a concrete schema and storage, both principles are aspirations.

The orchestrator drafted three storage options across Telegram on 2026-04-26/27 (separate `pantheon-ledger` repo vs. `ledger/` directory in `pantheon-config` vs. database). The operator chose option B (directory in `pantheon-config`) on 2026-04-28: principles and the audit chain that justifies-via them stay co-located.

## Decision

**Storage:** `pantheon-config/ledger/` directory, git-tracked.

**Format:** Append-only JSONL — one entry per line, one file per calendar year (`YYYY.jsonl`). Year-sharding chosen over single-file because (a) GitHub renders multi-MB JSONL poorly, (b) annual rollover is a natural archive boundary, (c) supersedes single-file before it becomes painful.

**Index:** A human-readable `INDEX.md` with the most recent entries surfaced for grep. Manually maintained at v0; auto-generation deferred until churn justifies it (P2 — don't build the tool until the manual cost is real).

**Mutation policy:** Append-only. Entries are never edited or deleted. To correct an error, append a new entry that supersedes the old one (set `supersedes` field to the old `id`). The git history is the immutable backstop, but the in-file convention preserves the audit chain even for readers who never run `git log`.

## Sub-year sharding (D10)

**Amended 2026-05-29 (scope-D, operator-blessed per T-0148; ORG-024 frames this as gap-1).** This section supersedes the single-year-file choice recorded above in §Format.

The decision ledger is sharded into **weekly ISO-week subfolders**, not a single `YYYY.jsonl` per calendar year:

```
ledger/
  INDEX.md                 ← retained top-level cross-segment spine
  2026-W18/
    2026-W18.jsonl          ← that ISO-week's entries
    SPINE.md                ← dedicated per-segment spine
  2026-W19/ … 2026-W22/     (same shape)
  2026.jsonl.archive-pre-d10-shard   ← (post-migration) archived legacy file, flat at ledger/ root
```

- **Segment id** = `<ISO-year>-W<NN>` computed from `datetime.isocalendar()` — ISO year (NOT calendar year) for year-boundary correctness; uppercase `W`, two-digit zero-padded week. The segment for an entry is computed from that entry's `ts` at write time.
- **Per-segment shard** `ledger/<seg>/<seg>.jsonl` holds the entries for that ISO week; each segment carries its own dedicated `SPINE.md`.
- **Top-level `INDEX.md` remains** as the retained cross-segment spine — the global navigation surface across all shards. This two-level indexing (per-segment SPINE.md + global INDEX.md) is the D10 minimum-traversal-to-precise-info implementation.

Rationale: §Format's original year-rollover argument (b)/(c) under-shards at the observed pace (~85 entries/~150 KB per week); weekly keeps each shard readable while INDEX.md preserves cross-week navigation. This is the D10 (Context-frugality / minimum-traversal-to-precise-info) implementation at the record-file layer. The audit chain is preserved by amending in place rather than rewriting §Format.

## Per-entry schema

```json
{
  "id": "2026-04-28T01:50:00Z-001",
  "ts": "2026-04-28T01:50:00Z",
  "agent": "orchestrator",
  "action": "ratify_principles_v0",
  "rationale": "Free-form prose. The 'why' a future reader needs to reconstruct the decision.",
  "justifies_via": ["P5"],
  "links": [
    {"type": "commit", "ref": "39498ee"},
    {"type": "telegram", "ref": "msg_64"}
  ],
  "cost_usd": null,
  "parent_id": null,
  "supersedes": null
}
```

**Field semantics:**

| Field | Required | Notes |
|---|---|---|
| `id` | yes | `<ISO ts>-<NNN>` where NNN is monotonic within the second. Globally unique within the ledger. |
| `ts` | yes | ISO 8601 UTC. The time the decision was *made*, not the time the entry was written. |
| `agent` | yes | Controlled vocabulary. Current values: `orchestrator`, `human:operator`, `worker:implementer`, `worker:reviewer`, `project:<slug>:orchestrator`. New values must be added in an ADR amendment. |
| `action` | yes | snake_case verb_noun. Free vocabulary at v0; promote to controlled vocab when patterns emerge. |
| `rationale` | yes | Free prose. Required even when "obvious" — the obvious decay first. |
| `justifies_via` | yes | Array of principle IDs (`P1`–`P9` for org, project-prefix for project, e.g. `echo:WS3`). At least one entry. Empty array is a P4 violation — escalate. |
| `links` | optional | Provenance pointers. Each `{type, ref}`. Common types: `commit`, `pr`, `adr`, `file`, `telegram`, `ledger` (parent or sibling entry). |
| `cost_usd` | optional | Decimal USD if known. `null` if not measured. Cost-tracking mechanism is BACKLOG B2 — schema reserves the field now so the data shape is forward-compatible. |
| `parent_id` | optional | The entry id this one descends from (e.g. a worker's report entry references the dispatch entry that spawned it). `null` for root entries. |
| `supersedes` | optional | The entry id this one corrects or replaces. Use sparingly — most decisions evolve via parent_id chains, not supersedure. |
| `metrics` | optional (close-entries only) | Structured per-cycle metrics; see §Metrics field below. Ratified per T-0193. |
| `executor` | optional | Free text naming **which executor of `agent` acted** — session id, pid, tmux/unit label, or a plain description. Recommended whenever a persona can have more than one live executor; mandatory nowhere. Documented 2026-08-02 per T-2276; see §Executor attribution. |

### Executor attribution (T-2276, documented 2026-08-02)

`agent` identifies a **persona**, not an executor. When one persona has two live
executors, every reconstruction mechanism — ledger authorship, commit author,
reflog, working copy, persona name — resolves to the same value for both, and
the chain becomes **intact, append-only, and unable to answer the question it
exists to answer.** P4 is not weakened by a missing entry here; it is weakened by
an actor field that has stopped being a key.

The worked case (mnemosyne, T-2274/T-2276): entry
`20260729T002700Z-mnemosyne-manual-drain-29-operator-directed` (`be5c28b39`) is
authored `mnemosyne`, sits in her clone's reflog as a local commit from her
working copy, and records a Telegram-directed action — while the session that
found it had received zero Telegram inbounds all night. Her sentence is the whole
ticket: *"I cannot tell you whether I did it."* It resolved benignly, and the
benign resolution demonstrates the finding rather than shrinking it: **it took
three seats and a ledger grep to attribute one action.**

**This field was not designed; it was already in use.** Measured 2026-08-02:
45 of 1509 entries already carried `executor`, written independently by three
different agents, two of them already using exactly the session-id and
tmux-unit-label forms. Convergent invention under real need is stronger evidence
that a field is required than any argument for adding one. This ADR documents
what the ledger already does.

**Additive by construction.** Consumers ignore unknown keys, and that claim was
**run, not reasoned** (Amendment H2): `ledger_placement_lint --all`,
`spine_index_groom --dry-run`, `cycle_stats session-resets`, and
`activity_digest --agent <a> --dry-run` were each executed against an entry
carrying the field, with a deliberately corrupt line first to prove the harness
could see a consumer complain. None validates the key set. The field also
survives a parse-and-reserialise round trip. And the strongest evidence is not
the fixture: **45 entries carrying this field have already passed through every
daily consumer run in production.**

**Relation to T-2274's executor-mutex.** If one-executor-per-identity becomes
structural, this field documents history rather than disambiguating the present —
still worth having, because a mutex can fail and on the night in question it did
not exist. The mutex lowers this field's urgency; it does not retire it.

**Interim convention, effective without any schema change:** an entry written
where a second executor could plausibly share the persona should name the
executor **in the action or rationale text** if the field is not set. Costs
nothing, breaks nothing, and makes tonight's question answerable tomorrow.

## Metrics field (close-entries)

Close-entries — i.e. ledger entries whose `action` matches `t<NNNN>_(resolved|landed|closed|done)_*` — gain an optional `metrics` field carrying structured per-cycle data. Read tool: `tools/cycle_stats.py` (T-0203).

### Schema

```json
"metrics": {
  "wall_clock_min":     30,
  "plan_rebounds":      0,
  "code_rebounds":      0,
  "reviewer_rebounds":  0,
  "implementer_count":  1,
  "reviewer_count":     1,
  "scope_loc_delta":    180,
  "cycle_shape":        "feature",
  "outcome_class":      "clean"
}
```

### Field semantics

| Field | Type | Notes |
|---|---|---|
| `wall_clock_min` | int | TeamCreate→cycle-close elapsed minutes (or HO-direct equivalent). |
| `plan_rebounds` | int | Reviewer REBOUNDs at the plan-tier (0..3 per b9 protocol). |
| `code_rebounds` | int | Reviewer REBOUNDs at the code-tier. |
| `reviewer_rebounds` | int | REBOUNDs against the reviewer itself (escalation-tier). |
| `implementer_count` | int | Distinct implementer workers spawned (≥1). |
| `reviewer_count` | int | Distinct reviewer workers spawned (≥1). |
| `scope_loc_delta` | int | Net `+lines/-lines` rolled-up across the cycle's commits. |
| `cycle_shape` | enum | `feature` / `refactor` / `migration` / `authoring` / `drift-cleanup` / `bugfix`. |
| `outcome_class` | enum | `clean` (0/3 REBOUNDs) / `one-rebound` / `multi-rebound` (≥2) / `escalated` (3+REBOUND budget exhausted) / `aborted`. |

### Why these fields

- **Cycle-shape vocab** lets HO grep prior cycles of similar shape before estimating a new one (per `tools/cycle_stats.py --similar T-NNNN`).
- **Outcome-class vocab** surfaces recurrence patterns: if `migration` cycles consistently `multi-rebound`, that's a structural signal worth a lessons/ entry.
- **wall_clock_min + counts + scope_loc_delta** together let HO quote "this looks like a 3-hour migration" with empirical backing rather than vibes.

### Discipline

- Metrics are recorded **at close-time** by the entity emitting the close-entry (HO or Recovery). Don't retrofit historical entries — leaving `metrics` absent is the structural signal that the cycle predates the schema.
- The numeric fields are best-effort summaries; sub-minute precision isn't load-bearing. Round generously.
- Adding a new `cycle_shape` or `outcome_class` value requires an ADR amendment (extend ORG-001 § Metrics field, file ratification ticket). Don't drift the vocab silently.

## Consequences

**Positive:**
- Single source of truth for "what decisions have been made and why" across the hub.
- Co-location with `rulebook/principles.md` means the upward justification (P4) is verifiable without leaving the repo.
- JSONL is grep-able and stream-able; year-sharding keeps individual files manageable.
- `cost_usd` field reserved means we don't re-cut the schema when B2 lands.

**Negative:**
- Manual entry writing is friction. Mitigation: workers will emit a DECISIONS section (B5) that the orchestrator can lift directly into ledger format with minimal transformation.
- Append-only means correcting structural mistakes requires supersedure entries, which adds noise. Acceptable: the noise IS the audit value.
- `INDEX.md` will drift from the JSONL if maintenance lapses. Acceptable until v1 — when `INDEX.md` becomes a lie often enough to matter, write the auto-generator (P2 again).

**Open questions deferred to follow-up ADRs:**
- Cost-tracking mechanism (B2). Once the source is identified (likely the `Claude-Code-Agent-Monitor` PostToolUse hook), `cost_usd` gets populated retroactively for entries within the relevant window.
- Whether project orchestrators write to `pantheon-config/ledger/` directly or to per-project ledgers that get periodically merged. v0 assumes single ledger; revisit when the second project comes online.

## Related

- `rulebook/principles.md` P5 (traceability), P4 (upward justification)
- BACKLOG B1 (this ADR closes it), B2 (cost field reserved here), B5 (worker DECISIONS sections feed this)
- T-0193 (metrics field ratification); T-0203 (`tools/cycle_stats.py` read tool)
