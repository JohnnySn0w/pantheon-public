#!/usr/bin/env python3
"""Shared ledger-shard helpers for the weekly-subfolder layout (T-0148).

Per T-0148 (the operator msg 2125) the decision ledger moves from a single
`ledger/2026.jsonl` to per-ISO-week subfolders, each carrying its own
entries shard plus a dedicated per-segment spine:

    ledger/
      INDEX.md                 ← top-level global spine (groomer-maintained)
      2026-W18/
        2026-W18.jsonl          ← that ISO-week's entries
        SPINE.md                ← dedicated per-segment spine
      2026-W19/ … 2026-W22/
      2026.jsonl.archive-pre-d10-shard   ← (post-migration; flat at ledger/ root)

Segment id = `<ISO-year>-W<NN>` computed from `datetime.isocalendar()` —
ISO year (NOT calendar year) for year-boundary correctness; uppercase W,
two-digit zero-padded week.

This module is the backward-compatibility seam. `iter_ledger_entry_files`
returns the union of subfolder shards PLUS the legacy `2026.jsonl` if it
still exists, so every reader is correct pre-migration (just `[2026.jsonl]`),
during transition (both), and post-migration (just the shards). It is the
key Phase-1 primitive: readers route through it and need no further change
when the migration eventually runs.

Phase 1 shipped read-only backward-compat code (readers route through
`iter_ledger_entry_files`). Phase 2 adds `current_write_target` — the
atomic-flip primitive every WRITER routes through: it returns the legacy
`2026.jsonl` while that file exists (a runtime no-op vs today) and the
current ISO-week shard once Phase 3 archives the legacy file. No writer
flips at deploy time; the flip is driven solely by the on-disk presence of
`2026.jsonl` (that is Phase 3).
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# The legacy single-file shard name. ORG-001 §Format chose `YYYY.jsonl`;
# 2026 is the only year-file in existence. Kept explicit (not derived from
# now().year) so a year-boundary fire still finds the legacy file during the
# transition window.
LEGACY_LEDGER_FILENAME = "2026.jsonl"

# Substring that marks an archived legacy file (e.g.
# `2026.jsonl.archive-pre-d10-shard`). Any path containing this is excluded
# from the reader union.
_ARCHIVE_MARKER = "archive"

# Timestamp format used throughout the ledger (ORG-001 §Per-entry schema:
# "ISO 8601 UTC", concretely `%Y-%m-%dT%H:%M:%SZ`).
TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Fixed formatted width of TS_FORMAT (e.g. "2026-07-20T00:10:23Z" == 20 chars).
# Every ORG-001 id opens with this prefix followed by `-<suffix>` (T-1879),
# so the prefix can be sliced by length before validating.
_ID_TS_PREFIX_LEN = len("YYYY-MM-DDTHH:MM:SSZ")


def segment_for_ts(ts_str: str) -> str:
    """Return the `<ISO-year>-W<NN>` segment id for an ISO-8601 UTC timestamp.

    Uses ISO calendar (`datetime.isocalendar()`) so entries in the first/last
    days of a calendar year land in the correct ISO-week segment — e.g.
    2027-01-01 belongs to ISO week 53 of ISO-year 2026.

    Raises ValueError if ts_str is not in the expected `%Y-%m-%dT%H:%M:%SZ`
    form (callers that must not silently mis-shard rely on this).
    """
    dt = datetime.strptime(ts_str, TS_FORMAT)
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def entry_ts(entry: dict) -> str | None:
    """Return the best-effort ISO-8601 UTC ts for a ledger entry, or None.

    Prefers entry["ts"] when it is a str (the common case, untouched).
    Falls back to deriving it from entry["id"]'s leading `%Y-%m-%dT%H:%M:%SZ`
    prefix (every ORG-001 id opens with this, followed by `-<suffix>`) for
    the 86 hand-written entries across 2026-W27..W30 missing `ts` (T-1879).
    Validated via strptime against TS_FORMAT rather than a blind slice, so a
    malformed or absent id yields None instead of a bogus timestamp.
    """
    ts = entry.get("ts")
    if isinstance(ts, str):
        return ts

    eid = entry.get("id")
    if not isinstance(eid, str):
        return None

    candidate = eid[:_ID_TS_PREFIX_LEN]
    try:
        datetime.strptime(candidate, TS_FORMAT)
    except ValueError:
        return None
    return candidate


def current_segment_shard(ledger_dir: Path, now: datetime) -> Path:
    """Return the shard path for the segment containing `now` — pure path.

    `ledger_dir / <segment> / f"{<segment>}.jsonl"`. Does NOT create any
    directories; callers that intend to write are responsible for mkdir.
    `now` is taken as already being in the desired (UTC) frame; only its
    isocalendar() is consulted.
    """
    iso_year, iso_week, _ = now.isocalendar()
    segment = f"{iso_year}-W{iso_week:02d}"
    return ledger_dir / segment / f"{segment}.jsonl"


def current_write_target(ledger_dir: Path, now: datetime) -> Path:
    """Return the path a writer should append the NEXT ledger entry to.

    This is the Phase-2 atomic-flip primitive (T-0148):

      * If the legacy `ledger_dir/2026.jsonl` still EXISTS → return it.
        Pre-migration, every writer keeps appending to the single legacy
        file exactly as before — ZERO behavior change while 2026.jsonl is
        present in the repo.
      * Otherwise → return `current_segment_shard(ledger_dir, now)`, the
        current ISO-week subfolder shard. The moment the Phase-3 migration
        archives 2026.jsonl (renames it to `2026.jsonl.archive-pre-d10-shard`),
        this branch takes over and every writer switches to the week-shard on
        its NEXT write — no code change, no redeploy.

    The flip is therefore driven purely by the on-disk existence of the legacy
    file, so deploying Phase-2 (routing all writers through this helper) is a
    runtime no-op until Phase-3 runs. `current_segment_shard` returns a pure
    path; callers that intend to write are responsible for creating the parent
    segment dir first (see `ensure_segment_dir`).
    """
    legacy = ledger_dir / LEGACY_LEDGER_FILENAME
    if legacy.is_file():
        return legacy
    return current_segment_shard(ledger_dir, now)


def ensure_segment_dir(target: Path) -> None:
    """Create the parent directory of a write target if it does not exist.

    `current_write_target` may return a post-migration week-shard path whose
    `ledger/<seg>/` parent does not yet exist (a brand-new ISO week). Callers
    invoke this immediately before opening the target for append. Idempotent;
    also covers the legacy-file case (its parent `ledger/` already exists).
    """
    target.parent.mkdir(parents=True, exist_ok=True)


def existing_entry_ids(ledger_dir: Path) -> set[str]:
    """Every `id` currently present across the whole ledger (T-2407).

    Reads the same union every reader reads (`iter_ledger_entry_files`), so a
    duplicate cannot hide by living in a different shard from its twin — which
    is how the historical nine came about: same id, adjacent lines, one shard,
    nothing looking across the set.

    Malformed lines are SKIPPED rather than raised on. This function's only
    caller is a uniqueness guard, and a guard that crashes on a pre-existing
    bad line converts an unrelated defect into a total append outage. A line
    that will not parse also cannot collide.
    """
    ids: set[str] = set()
    for path in iter_ledger_entry_files(ledger_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line).get("id")
            except (json.JSONDecodeError, AttributeError):
                continue
            if isinstance(value, str) and value:
                ids.add(value)
    return ids


# ORG-001 per-entry rules (amended 2026-10-08, the operator TG 11474; T-4734).
# T-4739 flipped ENFORCE on: a rule failure raises ValueError before the file
# is opened. With ENFORCE off a failure is only reported on stderr and the
# entry is still written.
ENFORCE = True

# When the ORG-100 role vocabulary cannot be loaded, the `agent` rule cannot be
# checked. True (orchestrator's lean, P4 over P1): under ENFORCE report that on stderr
# as a loud WARN and let the entry through (every other rule still applies).
# False: treat it as a rule failure and refuse the entry (fail closed).
VOCAB_LOAD_FAIL_OPEN = True
_VOCAB_LOAD_PROBLEM = "cannot load the ORG-100 role vocabulary"

REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "ts",
    "agent",
    "action",
    "rationale",
    "justifies_via",
)

# `agent` values outside the ORG-100 role vocabulary that ORG-001 allows.
EXTRA_AGENT_VALUES: frozenset[str] = frozenset({"human:operator"})

# How far `ts` may sit from the writer's clock: it is measured in the same
# command that appends the entry, so anything further is a reused or
# extrapolated stamp.
TS_MAX_SKEW_SECONDS = 300

_ID_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _role_vocabulary() -> tuple[frozenset[str], frozenset[str]]:
    """(allowed `agent` values, role ids shared by more than one seat)."""
    try:
        from tools import agent_role_map
    except ImportError:  # run as a script from tools/
        import agent_role_map  # type: ignore[no-redef]
    roles = agent_role_map.known_role_ids()
    return roles | EXTRA_AGENT_VALUES, agent_role_map.NON_INJECTIVE_ROLE_IDS


def validate_entry(entry: dict, now: datetime | None = None) -> list[str]:
    """Check `entry` against ORG-001's rules for NEW entries; return the
    problems found (empty when it conforms).

    - every required field is present and non-empty (`justifies_via` a
      non-empty list);
    - `ts` is `TS_FORMAT` UTC and within `TS_MAX_SKEW_SECONDS` of `now`
      (write time);
    - `id` is `<ts>-<slug>`: the entry's own `ts`, then a kebab-case slug;
    - `agent` is an ORG-100 role id or `human:operator`;
    - `executor` is set when `agent` is a role shared by several seats.

    History is never checked: this runs only on the write path.
    """
    problems: list[str] = []
    for name in REQUIRED_FIELDS:
        value = entry.get(name)
        if value in (None, "", []):
            problems.append(f"missing required field {name!r}")
    jv = entry.get("justifies_via")
    if jv not in (None, "", []) and not (
        isinstance(jv, list) and all(isinstance(p, str) and p for p in jv)
    ):
        problems.append("'justifies_via' must be a non-empty list of principle ids")

    ts_raw = entry.get("ts")
    ts: datetime | None = None
    if isinstance(ts_raw, str) and ts_raw:
        try:
            ts = datetime.strptime(ts_raw, TS_FORMAT).replace(tzinfo=timezone.utc)
        except ValueError:
            problems.append(f"'ts' {ts_raw!r} is not {TS_FORMAT} UTC")
    if ts is not None:
        clock = now if now is not None else datetime.now(timezone.utc)
        skew = abs((ts - clock).total_seconds())
        if skew > TS_MAX_SKEW_SECONDS:
            problems.append(
                f"'ts' {ts_raw} is {skew:.0f}s from the writer's clock; ORG-001 "
                "ts is the write time (date -u in the appending command); an "
                "earlier decision time goes in 'decided_at'"
            )

    entry_id = entry.get("id")
    if isinstance(entry_id, str) and entry_id and isinstance(ts_raw, str):
        prefix = f"{ts_raw}-"
        if not entry_id.startswith(prefix) or not _ID_SLUG_RE.fullmatch(
            entry_id[len(prefix) :]
        ):
            problems.append(
                f"'id' {entry_id!r} is not '<ts>-<slug>' (the entry's ts, then a "
                "lowercase kebab-case slug)"
            )

    agent = entry.get("agent")
    if isinstance(agent, str) and agent:
        try:
            allowed, shared = _role_vocabulary()
        except Exception as exc:  # noqa: BLE001 - the vocabulary is advisory here
            problems.append(f"{_VOCAB_LOAD_PROBLEM}: {exc}")
        else:
            if agent not in allowed:
                problems.append(
                    f"'agent' {agent!r} is not an ORG-100 role id or human:operator "
                    "(persona names and orchestrator are history-only)"
                )
            elif agent in shared and not entry.get("executor"):
                problems.append(
                    f"'agent' {agent!r} is shared by several seats; set 'executor' "
                    "to your seat slot id (e.g. engineering-orchestrator-2)"
                )
    return problems


def split_fail_open_problems(problems: list[str]) -> tuple[list[str], list[str]]:
    """(blocking, warn-only) split of `validate_entry` problems under ENFORCE.

    A vocabulary-load failure is warn-only while `VOCAB_LOAD_FAIL_OPEN` is on
    (orchestrator ruling, T-4739); every other problem blocks. Shared by the write
    path (`check_new_entry`) and the pre-commit lint so the two cannot differ."""
    if not VOCAB_LOAD_FAIL_OPEN:
        return list(problems), []
    soft = [p for p in problems if p.startswith(_VOCAB_LOAD_PROBLEM)]
    return [p for p in problems if p not in soft], soft


def check_new_entry(entry: dict, now: datetime | None = None) -> None:
    """Report (WARN) or refuse (ENFORCE) an entry that breaks ORG-001's rules.

    The single write-path gate shared by `append_entry` and
    `tools/_audit.append_ledger_entries`.
    """
    problems = validate_entry(entry, now)
    if not problems:
        return
    label = entry.get("id") or "<no id>"
    if ENFORCE:
        problems, soft = split_fail_open_problems(problems)
        if soft:
            for problem in soft:
                print(
                    f"ledger: WARN ORG-001 {label}: {problem}; the agent rule was "
                    "NOT checked (VOCAB_LOAD_FAIL_OPEN)",
                    file=sys.stderr,
                )
        if problems:
            raise ValueError(f"ledger entry {label} breaks ORG-001: " + "; ".join(problems))
        return
    for problem in problems:
        print(f"ledger: WARN ORG-001 {label}: {problem}", file=sys.stderr)


def append_entry(ledger_dir: Path, entry: dict, now: datetime | None = None) -> Path:
    """Append a ledger entry to the shard its OWN timestamp belongs in (T-2077).

    Correct-by-construction hand-append path: the shard is chosen via
    `segment_for_ts(entry_ts(entry))` — the entry's own ts, NOT wall-clock
    `now` — so an entry written near/after a week rollover (or a backdated
    one) still lands in the right shard. This closes the boundary-spillover
    gap that `current_write_target` cannot: that helper keys off the caller-
    supplied `now`, and an ad-hoc hand-append that hardcodes a shard path
    bypasses it entirely (the actual cause of the historical misplacements).

    Honors the same legacy-file transition as `current_write_target`: while
    `ledger_dir/2026.jsonl` still exists, appends there. Creates the segment
    dir if needed. Serializes `entry` as one compact JSON line. Returns the
    path written to.

    Raises ValueError if the entry has no parseable ts (segment_for_ts's
    contract — callers that must not silently mis-shard rely on this), or if
    the entry's `id` is ALREADY PRESENT anywhere in the ledger (T-2407).

    The duplicate-id refusal is writer-side prevention, per
    `sop/remediation/auto-commit-collision-on-id-class.md`. An append-only
    chain whose ids are not unique cannot address its own records: lookup by
    id returns two rows, and a `supersedes` naming that id corrects both or
    neither. Nine such pairs exist historically (2026-05-14..05-20 sync-alerts,
    each pair byte-equivalent, so no conflicting DECISION content was lost —
    see ledger/README.md); they are frozen, not repaired, because the chain is
    append-only and rewriting it to fix an addressing defect would forfeit the
    property the addressing exists to serve.

    This refuses BEFORE opening the file, so a rejected append leaves the
    ledger byte-identical.

    T-4734: every entry is first checked against ORG-001's rules for new
    entries (`check_new_entry`): refused before the file is opened while
    `ENFORCE` is on (T-4739), reported on stderr only if it is turned off.

    `now` is the clock the ts-skew rule measures against (default: the wall
    clock). A caller that stamped the entry earlier and appends it later (the
    harness's delayed or spooled landing) passes the entry's own ts, so skew is
    not re-applied; every other rule is unaffected.
    """
    check_new_entry(entry, now)
    legacy = ledger_dir / LEGACY_LEDGER_FILENAME
    entry_id = entry.get("id")
    if isinstance(entry_id, str) and entry_id:
        if entry_id in existing_entry_ids(ledger_dir):
            raise ValueError(
                f"ledger entry id {entry_id!r} already exists — ids must be unique "
                "so lineage lookup and `supersedes` resolve to exactly one record. "
                "Re-stamp with `date -u` (a collision usually means a reused or "
                "hand-copied id, not a same-second write)."
            )
    if legacy.is_file():
        target = legacy
    else:
        ts = entry_ts(entry)
        if ts is None:
            raise ValueError(
                "ledger entry has no parseable ts (entry['ts'] or id prefix); "
                "cannot determine its ISO-week shard"
            )
        segment = segment_for_ts(ts)
        target = ledger_dir / segment / f"{segment}.jsonl"
    ensure_segment_dir(target)
    with target.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")
    return target


def iter_ledger_entry_files(ledger_dir: Path) -> list[Path]:
    """Return the union of all ledger entry files every reader should consume.

    The union is:
      * every subfolder shard `ledger_dir/<seg>/<seg>.jsonl` (sorted by
        segment id), PLUS
      * the legacy `ledger_dir/2026.jsonl` if it still exists,
    EXCLUDING any path whose name contains the archive marker (e.g.
    `2026.jsonl.archive-pre-d10-shard`).

    Pre-migration this returns `[2026.jsonl]`; post-migration just the
    shards; during the transition window, both. This is the backward-compat
    primitive — readers route through it and stay correct across the flip.

    Fail-soft: a missing `ledger_dir` yields `[]`.
    """
    if not ledger_dir.is_dir():
        return []

    shards: list[Path] = []
    for sub in sorted(ledger_dir.iterdir(), key=lambda p: p.name):
        if not sub.is_dir():
            continue
        shard = sub / f"{sub.name}.jsonl"
        if shard.is_file() and _ARCHIVE_MARKER not in shard.name:
            shards.append(shard)

    legacy = ledger_dir / LEGACY_LEDGER_FILENAME
    if legacy.is_file() and _ARCHIVE_MARKER not in legacy.name:
        shards.append(legacy)

    return shards
