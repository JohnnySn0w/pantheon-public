"""Sample watch reports for uncertain negatives that may hide early stopping.

The watch brief already asks researchers to try a second instrument before
recording a negative. This read-only tool audits landed reports for phrases
such as "could not determine" and separates stronger negatives from thin ones.

Usage:
    python3 -m tools.watch_premature_termination
    python3 -m tools.watch_premature_termination --json --limit 20
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence


DEFAULT_REPORTS_ROOT = Path("knowledge/watch/reports")
WINDOW_RADIUS = 2

UNCERTAIN_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bcould not (?:be )?(?:determine|determined|find|locate|verify|confirm)\b",
        r"\b(?:cannot|can't) (?:determine|find|locate|verify|confirm)\b",
        r"\b(?:unable|wasn't able) to (?:determine|find|locate|verify|confirm)\b",
        r"\bnot (?:found|determined|verified|confirmed|located)\b",
        r"\bno (?:fresh|public|direct|primary|clear|clean)?\s*(?:evidence|source|report|curve|table|thread|release|documentation) (?:found|located|surfaced)\b",
        r"\bdid not (?:find|locate|verify|confirm|complete)\b",
    )
)

GIVE_UP_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bgive up\b",
        r"\bgave up\b",
        r"\binsufficient (?:context|evidence|signal)\b",
        r"\btoo little (?:context|evidence|signal)\b",
        r"\bnot enough (?:context|evidence|signal)\b",
        r"\bunknown\b",
        r"\bunclear\b",
        r"\bopen question\b",
    )
)

INSTRUMENT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bweb ?search\b",
        r"\bweb ?fetch\b",
        r"\bweb open\b",
        r"\bgoogle\b",
        r"\bbing\b",
        r"\bhacker news\b",
        r"\bhn\b",
        r"\bgithub\b",
        r"\barxiv\b",
        r"\bnvd\b",
        r"\bcurl\b",
        r"\bapi\b",
        r"\brg\b",
        r"\bgrep\b",
        r"\bpdftotext\b",
        r"\bpdf extraction\b",
        r"\bwayback\b",
        r"\blocal read\b",
        r"\bsources checked\b",
        r"\binstruments used\b",
        r"\bsearch\b",
        r"\bfetch\b",
        r"\bopened?\b",
        r"\bread\b",
        r"\bchecked\b",
        r"\btried\b",
    )
)

MULTI_INSTRUMENT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:two|2) instruments?\b",
        r"\bsecond instrument\b",
        r"\bmultiple (?:instruments|sources|searches|passes)\b",
        r"\bboth\b",
        r"\bacross\b",
        r"\bcross-?check(?:ed|ing)?\b",
        r"\btriangulat(?:e|ed|ion)\b",
        r"\bseparate (?:instrument|source|search|pass)\b",
        r"\bdedicated .*(?:instrument|pass|search)\b",
    )
)

INSTRUMENT_FAILURE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bfailed\b",
        r"\btimed? out\b",
        r"\b403\b",
        r"\b404\b",
        r"\bblocked\b",
        r"\bempty\b",
        r"\bonly navigation\b",
        r"\bnot readable\b",
        r"\bextraction failed\b",
        r"\bcould not access\b",
    )
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    classification: str
    tags: tuple[str, ...]
    excerpt: str


@dataclass(frozen=True)
class ScanSummary:
    reports_scanned: int
    candidate_count: int
    suspect_count: int
    clear_negative_count: int
    instrument_failure_count: int
    findings: tuple[Finding, ...]


def iter_report_paths(root: Path) -> Iterable[Path]:
    for path in sorted(root.glob("**/*.md")):
        if path.name == "README.md" or path.name.startswith("_"):
            continue
        if path.is_file():
            yield path


def _matches_any(patterns: Sequence[re.Pattern[str]], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def _match_count(patterns: Sequence[re.Pattern[str]], text: str) -> int:
    return sum(1 for pattern in patterns if pattern.search(text))


def _line_window(lines: Sequence[str], index: int, *, radius: int = WINDOW_RADIUS) -> str:
    start = max(0, index - radius)
    end = min(len(lines), index + radius + 1)
    return "\n".join(line.rstrip() for line in lines[start:end]).strip()


def classify_excerpt(excerpt: str) -> tuple[str, tuple[str, ...]]:
    """Classify one uncertainty-bearing excerpt.

    Returns a broad classification plus explanatory tags. The heuristic is
    intentionally biased toward review: missing evidence for the second
    instrument makes an uncertain negative "suspect", not false.
    """

    tags: list[str] = []
    instrument_count = _match_count(INSTRUMENT_PATTERNS, excerpt)

    if _matches_any(UNCERTAIN_PATTERNS, excerpt):
        tags.append("uncertain-negative")
    if _matches_any(GIVE_UP_PATTERNS, excerpt):
        tags.append("give-up-or-unclear")
    if _matches_any(INSTRUMENT_FAILURE_PATTERNS, excerpt):
        tags.append("instrument-failure")
    if _matches_any(MULTI_INSTRUMENT_PATTERNS, excerpt) or instrument_count >= 2:
        tags.append("multi-instrument-evidence")
    elif instrument_count == 1:
        tags.append("single-instrument-evidence")
    else:
        tags.append("no-instrument-evidence")

    if "instrument-failure" in tags and "multi-instrument-evidence" not in tags:
        return "premature-termination-suspect", tuple(tags)
    if "multi-instrument-evidence" in tags:
        return "clear-negative", tuple(tags)
    return "premature-termination-suspect", tuple(tags)


def scan_report(path: Path, *, root: Path | None = None) -> tuple[Finding, ...]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    findings: list[Finding] = []
    seen_windows: set[tuple[int, str]] = set()
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        if not (_matches_any(UNCERTAIN_PATTERNS, line) or _matches_any(GIVE_UP_PATTERNS, line)):
            continue
        excerpt = _line_window(lines, index)
        key = (index, excerpt)
        if key in seen_windows:
            continue
        seen_windows.add(key)
        classification, tags = classify_excerpt(excerpt)
        display_path = path
        if root is not None:
            try:
                display_path = path.relative_to(root)
            except ValueError:
                pass
        findings.append(
            Finding(
                path=str(display_path),
                line=index + 1,
                classification=classification,
                tags=tags,
                excerpt=excerpt,
            )
        )
    return tuple(findings)


def scan_reports(
    root: Path,
    *,
    include_clear: bool = False,
    latest_first: bool = True,
    limit: int | None = None,
) -> ScanSummary:
    report_paths = tuple(iter_report_paths(root))
    all_findings: list[Finding] = []
    for path in report_paths:
        all_findings.extend(scan_report(path, root=root))
    all_findings.sort(key=lambda finding: (finding.path, finding.line), reverse=latest_first)
    selected = [finding for finding in all_findings if include_clear or finding.classification != "clear-negative"]
    if limit is not None:
        selected = selected[:limit]
    return ScanSummary(
        reports_scanned=len(report_paths),
        candidate_count=len(all_findings),
        suspect_count=sum(1 for finding in all_findings if finding.classification == "premature-termination-suspect"),
        clear_negative_count=sum(1 for finding in all_findings if finding.classification == "clear-negative"),
        instrument_failure_count=sum(1 for finding in all_findings if "instrument-failure" in finding.tags),
        findings=tuple(selected),
    )


def as_jsonable(summary: ScanSummary) -> dict[str, object]:
    payload = asdict(summary)
    payload["findings"] = [asdict(finding) for finding in summary.findings]
    return payload


def render_text(summary: ScanSummary) -> str:
    lines = [
        f"reports_scanned: {summary.reports_scanned}",
        f"candidate_count: {summary.candidate_count}",
        f"suspect_count: {summary.suspect_count}",
        f"clear_negative_count: {summary.clear_negative_count}",
        f"instrument_failure_count: {summary.instrument_failure_count}",
    ]
    if summary.findings:
        lines.append("")
        lines.append("sample:")
    for finding in summary.findings:
        excerpt = " / ".join(line.strip() for line in finding.excerpt.splitlines() if line.strip())
        if len(excerpt) > 260:
            excerpt = excerpt[:257] + "..."
        lines.append(f"- {finding.path}:{finding.line} [{finding.classification}; {', '.join(finding.tags)}] {excerpt}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-root", type=Path, default=DEFAULT_REPORTS_ROOT)
    parser.add_argument("--include-clear", action="store_true", help="include clear multi-instrument negatives in the sample")
    parser.add_argument("--oldest-first", action="store_true", help="print the sample from oldest to newest instead of newest first")
    parser.add_argument("--limit", type=int, default=50, help="maximum findings to print (default: 50)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    limit = None if args.limit < 0 else args.limit
    summary = scan_reports(args.reports_root, include_clear=args.include_clear, latest_first=not args.oldest_first, limit=limit)
    if args.json:
        print(json.dumps(as_jsonable(summary), indent=2, sort_keys=True))
    else:
        print(render_text(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
