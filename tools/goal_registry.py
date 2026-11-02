"""Canonical machine-readable goal keys (T-5679).

The north stars live in one hand-curated file, ``rulebook/goals.md``. Each
north-star ``## `` heading carries one or more explicit key markers on the
lines directly under it::

    ## echo-prismatic — the voice interface
    <!-- goal-key: echo-prismatic -->

This module is the ONLY parser of those markers. It is read-only and has no
generated index. Every tool that validates a ``goal:<key>`` reference
(ticket purpose validation, the project catalogue, the lineage CLI) reads
the keys through here.

Failure is loud: a duplicate key, a malformed marker, a marker that is not
directly under a ``## `` heading, or a file with no markers at all raises
``GoalRegistryError``. A missing file
raises ``GoalRegistryMissing``. Callers that must keep working without a
registry use ``registry_status``, which returns an empty set *together with*
the reason, so the reason can be surfaced instead of silently swallowed.

Standard library only: ``project_catalogue`` and ``ticket_purpose`` import
this module, so it must not import them back.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

GOALS_RELPATH = Path("rulebook") / "goals.md"

# Byte-identical to ticket_purpose.GOAL_KEY_RE (pinned by a parity test), so
# every registered key is a parseable ``goal:<key>`` ref.
GOAL_KEY_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

_MARKER_RE = re.compile(r"^<!-- goal-key: (?P<key>\S+) -->$")
# Anything that looks like an attempt at a marker; a line matching this but not
# _MARKER_RE (with a valid key) is malformed, never silently ignored.
_MARKER_LIKE_RE = re.compile(r"<!--\s*goal[-_ ]?key\b", re.IGNORECASE)


class GoalRegistryError(ValueError):
    """The goal registry exists but cannot be parsed honestly."""


class GoalRegistryMissing(GoalRegistryError):
    """The goal registry file does not exist or cannot be read."""


@dataclass(frozen=True)
class GoalEntry:
    key: str
    heading: str
    line: int  # 1-based line number of the marker


def goals_path(repo_root: Path) -> Path:
    return Path(repo_root) / GOALS_RELPATH


def parse_goal_entries(text: str, *, source: str = "goals.md") -> tuple[GoalEntry, ...]:
    """Parse key markers out of goals.md text. Raises GoalRegistryError."""
    entries: list[GoalEntry] = []
    first_seen: dict[str, int] = {}
    heading: str | None = None
    in_marker_run = False
    for number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("## "):
            heading = line[3:].strip()
            in_marker_run = True
            continue
        if not _MARKER_LIKE_RE.search(line):
            in_marker_run = False
            continue
        match = _MARKER_RE.fullmatch(line)
        if match is None or GOAL_KEY_RE.fullmatch(match.group("key")) is None:
            raise GoalRegistryError(
                f"{source}:{number}: malformed goal-key marker {line!r} "
                f"(expected '<!-- goal-key: <slug> -->', slug [a-z0-9-])"
            )
        if not in_marker_run or heading is None:
            raise GoalRegistryError(
                f"{source}:{number}: goal-key marker is not directly under a '## ' heading"
            )
        key = match.group("key")
        if key in first_seen:
            raise GoalRegistryError(
                f"{source}:{number}: duplicate goal key {key!r} "
                f"(first declared at line {first_seen[key]})"
            )
        first_seen[key] = number
        entries.append(GoalEntry(key=key, heading=heading, line=number))
    return tuple(entries)


def goal_entries(repo_root: Path, *, path: Path | None = None) -> tuple[GoalEntry, ...]:
    """Return every registered goal entry. Raises GoalRegistryError/Missing."""
    target = Path(path) if path is not None else goals_path(repo_root)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise GoalRegistryMissing(f"goal registry missing: {target}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise GoalRegistryMissing(f"goal registry unreadable: {target}: {exc}") from exc
    entries = parse_goal_entries(text, source=str(target))
    if not entries:
        # A registry file with zero markers is unusable, not "no goals".
        raise GoalRegistryError(f"{target}: no goal-key markers found")
    return entries


def load_goal_keys(repo_root: Path, *, path: Path | None = None) -> frozenset[str]:
    """Return the registered goal keys. Raises GoalRegistryError/Missing."""
    return frozenset(entry.key for entry in goal_entries(repo_root, path=path))


def registry_status(
    repo_root: Path, *, path: Path | None = None
) -> tuple[frozenset[str], str | None]:
    """Never-raising variant: (keys, None) or (frozenset(), reason)."""
    try:
        return load_goal_keys(repo_root, path=path), None
    except GoalRegistryError as exc:
        return frozenset(), str(exc)
