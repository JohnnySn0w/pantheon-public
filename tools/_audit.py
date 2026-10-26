#!/usr/bin/env python3
"""Shared ORG-001 ledger-entry construction + append (T-1088 §Auditable, part a).

The Dei `Auditable` interface (notes/dei-foundational-wip/interfaces.md) declares
an append-only `{ts, actor, action}` log as a first-class shape. Inspection
(T-1088 part-a scoping) found the genuine, already-drifted duplication is NOT the
ticket.py markdown transition-log (which shares only the *concept* with the
ledger — different storage, format, and idempotency posture, and whose one
idempotent caller, ticket_grooming, already reuses ticket.py.append_transition).
The real 3× duplication is the ORG-001 *ledger* writers, which each hand-rolled
the same kernel:

    {build the 10-field ORG-001 entry} -> current_write_target(ledger_dir, now)
    -> ensure_segment_dir(target) -> open("a") + json.dumps + "\n"

…and had drifted: `activity_digest.write_ledger_entry` (writer since retired,
T-4289) and `watch_runner.write_actuation_ledger` emitted `json.dumps(entry)`
(no key sort), `session_reset_reconcile.append_to_ledger` emitted
`json.dumps(e, sort_keys=True)`, and watch_runner's entry OMITTED the
`cost_usd`/`parent_id`/`supersedes` provenance fields (8 of 10). This module is
the single canonical implementation the remaining writers route through.

Design (kept deliberately thin so it doesn't leak caller specifics):
  * `build_ledger_entry` is a pure field-assembler — it enforces the full
    ORG-001 10-field key set and nothing else. Caller-specific inputs (the
    `id` suffix, `links`, `justifies_via`) are arguments, not absorbed logic.
  * `append_ledger_entries` owns the resolve→ensure→append kernel, serializes
    with `sort_keys=True` (deterministic; matches the most careful prior
    writer), accepts an iterable (single = list-of-one, preserving
    session_reset_reconcile's batch append), and RETURNS the path it wrote so a
    commit-paired caller can stage exactly what was appended without
    re-resolving the shard.

ORG-001 §Per-entry schema is the source of truth for the field set + ordering.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from tools.ledger_shard import check_new_entry, current_write_target, ensure_segment_dir

# Canonical ORG-001 field order. build_ledger_entry emits exactly these keys;
# serialization is sort_keys=True so on-disk order is deterministic regardless.
ORG001_FIELDS = (
    "id",
    "ts",
    "agent",
    "action",
    "rationale",
    "justifies_via",
    "links",
    "cost_usd",
    "parent_id",
    "supersedes",
)


def build_ledger_entry(
    *,
    entry_id: str,
    ts: str,
    agent: str,
    action: str,
    rationale: str,
    justifies_via: Iterable[str],
    links: Iterable[dict] | None = None,
    cost_usd: float | None = None,
    parent_id: str | None = None,
    supersedes: str | None = None,
    executor: str | None = None,
    decided_at: str | None = None,
) -> dict[str, Any]:
    """Build one canonical ORG-001 ledger entry (all 10 fields present).

    `entry_id` maps to the `id` key (named to avoid shadowing the builtin at
    call sites). `justifies_via` and `links` are materialized into fresh lists
    so callers can pass any iterable without aliasing. Optional provenance
    fields (`cost_usd`, `parent_id`, `supersedes`) default to None and are
    ALWAYS present — this is what closes watch_runner's prior 8-field gap.

    T-4737: `executor` (the writer's seat slot) and `decided_at` (an earlier
    decision time; `ts` stays the write time) are ORG-001's optional fields,
    included only when given.
    """
    entry: dict[str, Any] = {
        "id": entry_id,
        "ts": ts,
        "agent": agent,
        "action": action,
        "rationale": rationale,
        "justifies_via": list(justifies_via),
        "links": list(links) if links is not None else [],
        "cost_usd": cost_usd,
        "parent_id": parent_id,
        "supersedes": supersedes,
    }
    if executor is not None:
        entry["executor"] = executor
    if decided_at is not None:
        entry["decided_at"] = decided_at
    return entry


def append_ledger_entries(
    entries: Iterable[dict],
    *,
    ledger_dir: Path,
    shard: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Append `entries` (one JSON line each) to the current ledger shard.

    The write target is `shard` if given (tests / explicit override), else
    `current_write_target(ledger_dir, now)` — the legacy `ledger/2026.jsonl`
    while it exists, else the current ISO-week shard (T-0148 Phase 2). The
    segment dir is created first (`ensure_segment_dir`). Each entry is written
    as `json.dumps(e, sort_keys=True)` + newline.

    Returns the resolved target path. A commit-paired caller should stage THIS
    returned path rather than re-resolving `current_write_target`, so the
    append and the stage cannot diverge across an ISO-week boundary.
    """
    target = shard if shard is not None else current_write_target(
        ledger_dir, now if now is not None else datetime.now(timezone.utc)
    )
    entries = list(entries)
    for e in entries:  # T-4734: the same ORG-001 write-path gate as append_entry
        check_new_entry(e, now)
    ensure_segment_dir(target)
    with target.open("a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, sort_keys=True) + "\n")
    return target
