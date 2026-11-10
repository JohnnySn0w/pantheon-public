"""watch component: the repo-side evidence collector (T-5964).

Builds the observation the watch planner reads (``watch.py``), from repo files
only -- the one surface any seat can read; claude-research's units, state and
journal are not readable from another account. It wraps the existing tools' own
readers and writers, never re-implementing them:

* ``tools.watch_runner.parse_topics`` reads ``knowledge/watch/topics.md``;
* the day-index row shape is the one ``tools.watch_runner.append_day_index_entry``
  writes and ``extract_findings`` reads (``| [slug](slug.md) | status | summary |``);
* ``tools._git_utils._hermes_scan_dir`` (the scan ``watch_runner`` runs before it
  commits reports) checks every directory under ``knowledge/watch/reports/`` for the
  forbidden byte sequence (W6);
* ``tools.watch_pipeline_decommission.DecommissionReport`` is converted as-is.

No live handler uses this yet (T-5964 D3; the live reader is T-5972): tests build
simulated-host seeds from committed fixtures with it. Pure: it reads the paths it
is given and runs nothing (the landing commit is passed in as data).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict
from datetime import date
from pathlib import Path

from tools import watch_pipeline_decommission as WD
from tools import watch_runner as WR
from tools._git_utils import _hermes_scan_dir

UNIT_SUFFIXES = (".timer", ".service")
#: the row regex of tools.watch_runner.extract_findings
ROW_RE = re.compile(r"^\|\s*\[([^\]]+)\]\(([^)]+)\)\s*\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*$")
#: the H1 tools.watch_runner.append_day_index_entry writes
HEADER_RE = re.compile(r"^# Watch reports — (\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)
DAY_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DAY_INDEX = "README.md"


def unit_names():
    return tuple(f"{stem}{suffix}" for stem in WD.WATCH_UNIT_STEMS for suffix in UNIT_SUFFIXES)


def day_of(name):
    """A date from a ``YYYY-MM-DD`` name that is a real calendar date, else None
    (2026-13-40, 9999-99-99 and the basic form 20261102 are all None)."""

    if not (isinstance(name, str) and DAY_DIR_RE.match(name)):
        return None
    try:
        return date.fromisoformat(name)
    except ValueError:
        return None


def _sha_file(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def templates(templates_dir: Path) -> dict:
    """sha256 of each unit template (None when absent)."""

    return {name: _sha_file(templates_dir / name) for name in unit_names()}


def newest_day_index(reports_root: Path):
    """The newest ``<YYYY-MM-DD>/README.md`` day index under ``reports_root`` (or None).
    Directory names that are not real dates are skipped."""

    if not reports_root.is_dir():
        return None
    days = sorted((day, p) for p in reports_root.iterdir() if p.is_dir() and (day := day_of(p.name)) is not None)
    for _day, path in reversed(days):
        if (path / DAY_INDEX).is_file():
            return path / DAY_INDEX
    return None


def day_index(text: str) -> dict:
    """``{date, rows: [{slug, status}]}`` from a day index's text."""

    header = HEADER_RE.search(text)
    rows = []
    for line in text.splitlines():
        match = ROW_RE.match(line)
        if match:
            rows.append({"slug": match.group(1), "status": match.group(3).strip()})
    return {"date": header.group(1) if header else None, "rows": rows}


def landed(day_index_path: Path | None, landing_author: str | None, landing_subject: str | None = None):
    """The landed-run evidence. The index must sit in a real-date day directory whose name
    equals its header date; otherwise ``unreadable`` says why and the planner refuses."""

    if day_index_path is None:
        return None
    out = day_index(day_index_path.read_text(encoding="utf-8"))
    out["author"] = landing_author
    out["subject"] = landing_subject
    dir_name = day_index_path.parent.name
    if day_of(dir_name) is None:
        out["unreadable"] = f"the day index's directory {dir_name!r} is not a YYYY-MM-DD date"
    elif out["date"] != dir_name:
        out["unreadable"] = f"the day index header date {out['date']!r} differs from its directory {dir_name!r}"
    return out


def topics(topics_path: Path) -> dict:
    """``knowledge/watch/topics.md`` as ``watch_runner.parse_topics`` reads it."""

    warnings: list[str] = []
    parsed = WR.parse_topics(topics_path, warnings=warnings)
    return {
        "sha256": _sha_file(topics_path),
        "parsed": sorted([t.slug, t.cadence] for t in parsed),
        "active": sorted(t.slug for t in parsed if t.cadence != "paused" and t.cadence in WR.VALID_CADENCES),
        "warnings": list(warnings),
    }


def byte_scan(reports_root: Path) -> dict:
    """W6: ``_hermes_scan_dir`` over ``reports_root`` and every directory below it (the
    helper is flat), in sorted order; the first hit is reported."""

    if not reports_root.is_dir():
        return {"ok": False, "detail": f"{reports_root} is not a directory"}
    for directory in [reports_root, *sorted(p for p in reports_root.rglob("*") if p.is_dir())]:
        ok, detail = _hermes_scan_dir(directory)
        if not ok:
            return {"ok": False, "detail": detail}
    return {"ok": True, "detail": None}


def collect(
    *,
    templates_dir: Path,
    topics_path: Path,
    reports_root: Path,
    day_index_path: Path | None,
    landing_author: str | None,
    landing_subject: str | None,
    repo_head: str,
    observed_at: str,
) -> dict:
    """The planner's ``evidence`` observation, from repo files (paths given explicitly)."""

    return {
        "observed_at": observed_at,
        "repo_head": repo_head,
        "templates": templates(templates_dir),
        "landed": landed(day_index_path, landing_author, landing_subject),
        "topics": topics(topics_path),
        "byte_scan": byte_scan(reports_root),
    }


def decommission_report(report: "WD.DecommissionReport") -> dict:
    """A legacy ``DecommissionReport`` as observation data (tuples become lists)."""

    return {key: list(value) if isinstance(value, tuple) else value for key, value in asdict(report).items()}
