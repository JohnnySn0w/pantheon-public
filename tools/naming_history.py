#!/usr/bin/env python3
"""Pantheon naming-history registry — canonical old→new resolver.

Reads rulebook/naming-history.yaml and answers "if you see old string X, current
is Y, ratified by Z" for renames landed across the fleet. Read-time only; the
registry file is hand-curated + appended-to by rename-ticket closure.

T-3815 (the operator TG 10145): "in order to ease tickets over, a mapping should
track old names for things in general." Replaces the ad-hoc per-namespace
continuity discipline (agent_role_map backward-resolve, per-rename lessons
filing) with a machine-queryable single surface.

Not-scope:
- Rewrite of historical audit-chain surfaces (ledger, tickets, notes are P4
  immutable). This resolver is a READ-time forward-lookup, not a mutation path.
- Reverse resolution (new → old). If a caller needs the reverse for audit-role
  disambiguation, agent_role_map is the surface (its D1 backward-resolve is
  intact).
- Automatic append on rename-ticket resolve. Write discipline is manual (part
  of the rename ticket's closure); the resolver refuses to invent entries.

Usage (CLI):
    python3 tools/naming_history.py resolve <old-name>
        → prints "current: <new>\\nratified_by: <T-NNNN>\\nkind: <role_id|...>\\nscope: <prose>"
        → exit 0 if found, exit 1 if not found (with "no entry" on stderr)
        → for ambiguous entries (same `old` appears in multiple entries — e.g.
          `orchestrator` was renamed differently for orchestrator vs archivist),
          all matches are printed as an "entries:" list; caller disambiguates
          by kind/scope.

Usage (import):
    from tools.naming_history import load_registry, resolve
    registry = load_registry()
    entries = resolve("sub_orchestrator", registry)
    # entries is a tuple of NamingEntry; empty if not found.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = REPO_ROOT / "rulebook" / "naming-history.yaml"

VALID_KINDS = frozenset({"role_id", "role_key", "persona", "instance", "agent_id"})


class NamingHistoryError(RuntimeError):
    """Registry-shape or parsing error. Caller expected to surface to user, not swallow."""


@dataclass(frozen=True)
class NamingEntry:
    old: str
    new: str
    kind: str
    retired_date: str
    ratified_by: str
    scope: str


def load_registry(path: Path | None = None) -> tuple[NamingEntry, ...]:
    """Read + validate the registry. Returns a tuple of NamingEntry.

    Raises NamingHistoryError on any shape violation (missing fields, invalid
    kind, wrong schema_version, non-list entries). Callers should NOT silently
    tolerate a malformed registry — that would degrade to "no match" for names
    that actually have entries.
    """
    path = path or REGISTRY_PATH
    if not path.exists():
        raise NamingHistoryError(f"registry file not found: {path}")
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise NamingHistoryError(f"registry root must be a mapping, got {type(data).__name__}")
    if data.get("schema_version") != 1:
        raise NamingHistoryError(
            f"registry schema_version must be 1, got {data.get('schema_version')!r}"
        )
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        raise NamingHistoryError("registry entries must be a list")
    parsed: list[NamingEntry] = []
    for i, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise NamingHistoryError(f"entry [{i}] must be a mapping, got {type(raw).__name__}")
        missing = {"old", "new", "kind", "retired_date", "ratified_by", "scope"} - set(raw.keys())
        if missing:
            raise NamingHistoryError(
                f"entry [{i}] ({raw.get('old', '?')}→{raw.get('new', '?')}) missing fields: {sorted(missing)}"
            )
        extra = set(raw.keys()) - {"old", "new", "kind", "retired_date", "ratified_by", "scope"}
        if extra:
            raise NamingHistoryError(
                f"entry [{i}] ({raw['old']}→{raw['new']}) has unknown fields: {sorted(extra)}"
            )
        if raw["kind"] not in VALID_KINDS:
            raise NamingHistoryError(
                f"entry [{i}] ({raw['old']}→{raw['new']}) has invalid kind {raw['kind']!r}; "
                f"valid: {sorted(VALID_KINDS)}"
            )
        # retired_date is written unquoted (`YYYY-MM-DD`) so PyYAML parses it as
        # a datetime.date; other fields must be non-empty strings.
        for field in ("old", "new", "kind", "ratified_by", "scope"):
            if not isinstance(raw[field], str) or not raw[field]:
                raise NamingHistoryError(
                    f"entry [{i}] ({raw.get('old', '?')}→{raw.get('new', '?')}) field {field!r} must be a non-empty string"
                )
        retired_date_raw = raw["retired_date"]
        retired_date = (
            retired_date_raw.isoformat()
            if hasattr(retired_date_raw, "isoformat")
            else retired_date_raw
        )
        if not isinstance(retired_date, str) or not retired_date:
            raise NamingHistoryError(
                f"entry [{i}] ({raw['old']}→{raw['new']}) field 'retired_date' must be YYYY-MM-DD"
            )
        parsed.append(
            NamingEntry(
                old=raw["old"],
                new=raw["new"],
                kind=raw["kind"],
                retired_date=retired_date,
                ratified_by=raw["ratified_by"],
                scope=raw["scope"],
            )
        )
    return tuple(parsed)


def resolve(
    old: str, registry: Iterable[NamingEntry], kind: str | None = None
) -> tuple[NamingEntry, ...]:
    """Return all entries whose `old` matches. Optionally filter by kind.

    Returns a tuple (possibly empty). The same `old` may map to different `new`
    depending on kind/scope (e.g. `orchestrator` role_id renamed for orchestrator
    → head_engineering_orchestrator and for archivist → head_memory_orchestrator
    — same old name, same kind, DIFFERENT new). Callers that need
    unambiguous resolution must disambiguate by scope/context.
    """
    hits = tuple(e for e in registry if e.old == old and (kind is None or e.kind == kind))
    return hits


def _format_entry(entry: NamingEntry) -> str:
    return (
        f"current: {entry.new}\n"
        f"kind: {entry.kind}\n"
        f"ratified_by: {entry.ratified_by}\n"
        f"retired_date: {entry.retired_date}\n"
        f"scope: {entry.scope}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pantheon naming-history resolver — canonical old→new lookup.",
    )
    subs = parser.add_subparsers(dest="cmd", required=True)
    p_resolve = subs.add_parser("resolve", help="look up an old name")
    p_resolve.add_argument("old", help="the retired name to resolve")
    p_resolve.add_argument(
        "--kind",
        choices=sorted(VALID_KINDS),
        default=None,
        help="restrict to a specific kind",
    )
    subs.add_parser("list", help="print every entry in the registry")

    args = parser.parse_args(argv)
    registry = load_registry()
    if args.cmd == "resolve":
        hits = resolve(args.old, registry, kind=args.kind)
        if not hits:
            print(f"no entry for {args.old!r}", file=sys.stderr)
            return 1
        if len(hits) == 1:
            print(_format_entry(hits[0]))
        else:
            print(f"entries: {len(hits)} matches for {args.old!r} (disambiguate by kind/scope)\n")
            for i, entry in enumerate(hits):
                print(f"--- match {i + 1} ---")
                print(_format_entry(entry))
                print()
        return 0
    if args.cmd == "list":
        for entry in registry:
            print(_format_entry(entry))
            print()
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
