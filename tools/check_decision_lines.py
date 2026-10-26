#!/usr/bin/env python3
"""WARN-only ORG-102 structured DECISION line check for PR bodies."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import decision_lines as dl  # noqa: E402


def _warning(message: str) -> None:
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::warning title=ORG-102 DECISION line::{escaped}")


def check_text(text: str, *, surface: str = "PR body") -> int:
    try:
        scan = dl.scan_decisions(text)
    except BaseException as exc:  # fail-open: this is WARN-only in step 2.
        _warning(f"{surface}: decision-line parser crashed ({type(exc).__name__}); treating as warning-only")
        return 0
    if not scan.marker_present:
        return 0
    for problem in scan.problems:
        loc = f" line {problem.line_no}" if problem.line_no is not None else ""
        _warning(f"{surface}{loc}: {problem.message}")
    return 0


def body_from_event(path: Path) -> tuple[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    pr = raw.get("pull_request") or {}
    number = pr.get("number") or raw.get("number") or "unknown"
    repo = (raw.get("repository") or {}).get("name") or "repo"
    return pr.get("body") or "", f"{repo}#{number} body"


def should_check_event() -> bool:
    return os.environ.get("GITHUB_EVENT_NAME") == "pull_request"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", help="check this text instead of GITHUB_EVENT_PATH")
    parser.add_argument("--surface", default="PR body")
    parser.add_argument("--event-path", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.text is not None:
        return check_text(args.text, surface=args.surface)
    if not should_check_event():
        return 0
    event_path = args.event_path or Path(os.environ.get("GITHUB_EVENT_PATH", ""))
    if not event_path or not event_path.exists():
        _warning("GITHUB_EVENT_PATH missing; decision-line PR-body check skipped")
        return 0
    try:
        body, surface = body_from_event(event_path)
    except BaseException as exc:
        _warning(f"could not read PR body event ({type(exc).__name__}); decision-line check skipped")
        return 0
    return check_text(body, surface=surface)


if __name__ == "__main__":
    raise SystemExit(main())
