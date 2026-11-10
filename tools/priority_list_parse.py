#!/usr/bin/env python3
"""Parse the sentinel-delimited YAML priority-list region (ORG-078 §2 —
priority-list is one operator-editable rulebook file). Reused by
`ticket.py list` (T-3020 BT3) + `ticket.py priority-list validate` + the
fleet-monitor hook; this module owns only the parse, not the consumer
fallback-to-FIFO behavior (that lives at the caller, per T-3020 BT1 spec).

Failure classes (each `PriorityListParseError` message is prefixed with
one, so downstream telemetry — ORG-078 §4's
`~/.local/log/pantheon/priority_list_health.jsonl` — can key on it):

    sentinel-missing   <!-- PRIORITY-LIST-START/END --> absent or out of order.
    fence-missing      sentinels present, but no ```yaml ... ``` fence inside them.
    yaml-parse-fail    the fenced YAML text does not parse.
    schema             parsed YAML doesn't match the priority-list shape
                        (top-level not a mapping; a key isn't `priority-N`;
                        a rank's value isn't a list; a label isn't a string).
    file-not-found     `parse_file()` was pointed at a path that doesn't exist.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PRIORITY_LIST_PATH = REPO_ROOT / "rulebook" / "priority-list.md"

_SENTINEL_START = "<!-- PRIORITY-LIST-START -->"
_SENTINEL_END = "<!-- PRIORITY-LIST-END -->"
_FENCE_RE = re.compile(r"```yaml\s*\n(.*?)```", re.DOTALL)
EXPECTED_BROAD_KEY = "expected-broad"
_PRIORITY_KEY_RE = re.compile(r"^priority-(\d+)$")


class PriorityListParseError(Exception):
    """Raised when the sentinel-delimited YAML region can't be parsed into
    a label -> rank mapping. Message is always `"<failure-class>: <detail>"`
    — see module docstring for the failure-class vocabulary."""


def _load_region(md_text: str):
    """Extract + YAML-load the sentinel-delimited fenced region of *md_text*.
    Returns the loaded object (may be None for an empty region)."""
    start = md_text.find(_SENTINEL_START)
    end = md_text.find(_SENTINEL_END)
    if start == -1 or end == -1 or end < start:
        raise PriorityListParseError(
            "sentinel-missing: <!-- PRIORITY-LIST-START --> / "
            "<!-- PRIORITY-LIST-END --> not found (or out of order) in text"
        )
    region = md_text[start + len(_SENTINEL_START) : end]

    fence_match = _FENCE_RE.search(region)
    if fence_match is None:
        raise PriorityListParseError(
            "fence-missing: no ```yaml ... ``` fence found within the "
            "sentinel region"
        )
    yaml_text = fence_match.group(1)

    try:
        return yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise PriorityListParseError(f"yaml-parse-fail: {exc}") from exc


def parse(md_text: str) -> dict[str, int]:
    """Parse *md_text* (the full contents of a priority-list.md-shaped file)
    into a flat `{label: rank}` mapping. Rank 1 = highest priority. Duplicate
    labels across ranks resolve to the LOWEST (ceiling) rank. An empty
    ranked-labels YAML region is a legal "no priorities defined" state and
    returns `{}`. The optional `expected-broad:` key is not a tier and never
    affects the result (see `parse_expected_broad`)."""
    loaded = _load_region(md_text)

    if loaded is None:
        return {}

    if not isinstance(loaded, dict):
        raise PriorityListParseError(
            f"schema: top-level must be a mapping, got {type(loaded).__name__}"
        )

    result: dict[str, int] = {}
    for key, value in loaded.items():
        if key == EXPECTED_BROAD_KEY:
            continue
        match = _PRIORITY_KEY_RE.match(key) if isinstance(key, str) else None
        if match is None or int(match.group(1)) < 1:
            raise PriorityListParseError(
                f"schema: priority-N key required, got {key!r}"
            )
        rank = int(match.group(1))

        if not isinstance(value, list):
            raise PriorityListParseError(
                f"schema: priority-N value must be a list, got "
                f"{type(value).__name__} for {key!r}"
            )

        for label in value:
            if not isinstance(label, str):
                raise PriorityListParseError(
                    f"schema: label must be a string, got "
                    f"{type(label).__name__} under {key!r}"
                )
            if label not in result or rank < result[label]:
                result[label] = rank

    return result


def parse_file(path: str | Path | None = None) -> dict[str, int]:
    """Read *path* (default: `rulebook/priority-list.md` at repo root) and
    `parse()` its contents. Strict — a missing file raises rather than
    falling back; the fallback-to-FIFO behavior belongs to the caller
    (`ticket.py list`, T-3020 BT3), not this parser."""
    resolved = Path(path) if path is not None else DEFAULT_PRIORITY_LIST_PATH
    if not resolved.exists():
        raise PriorityListParseError(f"file-not-found: {resolved}")
    text = resolved.read_text(encoding="utf-8")
    return parse(text)


def parse_expected_broad(md_text: str) -> dict[str, str]:
    """Optional top-level `expected-broad:` mapping (T-6239): `{label: reason}`
    for labels that are broad by design, exempt from the high-volume and
    semantic-drift validator heuristics. Absent key (or empty region) -> `{}`.
    Malformed value (not a mapping of string -> string) raises a `schema:`
    `PriorityListParseError`."""
    loaded = _load_region(md_text)
    if not isinstance(loaded, dict) or EXPECTED_BROAD_KEY not in loaded:
        return {}
    value = loaded[EXPECTED_BROAD_KEY]
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PriorityListParseError(
            f"schema: {EXPECTED_BROAD_KEY} must be a mapping of label -> "
            f"reason, got {type(value).__name__}"
        )
    for label, reason in value.items():
        if not isinstance(label, str) or not isinstance(reason, str):
            raise PriorityListParseError(
                f"schema: {EXPECTED_BROAD_KEY} entries must be string -> "
                f"string, got {label!r}: {type(reason).__name__}"
            )
    return dict(value)


def parse_file_expected_broad(path: str | Path | None = None) -> dict[str, str]:
    """File-reading wrapper for `parse_expected_broad`; same path default and
    strict missing-file behavior as `parse_file`."""
    resolved = Path(path) if path is not None else DEFAULT_PRIORITY_LIST_PATH
    if not resolved.exists():
        raise PriorityListParseError(f"file-not-found: {resolved}")
    return parse_expected_broad(resolved.read_text(encoding="utf-8"))
