#!/usr/bin/env python3
"""check_dbus_tier_outcomes.py -- fail unless the Action Broker's D-Bus test tiers EXECUTED (T-4931).

    python tools/check_dbus_tier_outcomes.py <pytest --junitxml report>... --min-executed N
                                             [--allow-skip REGEX]... [--require-module NAME]...

WHY THIS EXISTS. Every requires_dbus / requires_private_bus / skipUnless(DBUS) tier of
tools/test_host_control_*.py (the privileged host-control paths, ORG-052) skips when the
interpreter has no dbus-python or the host has no dbus-daemon. In the main CI job that is
always the case, so those tiers ran only on the canonical host. The `dbus-tiers` job in
ci.yml provisions them; this is its non-skip gate. A green pytest is not enough there: a
missing apt package turns every one of those tiers into a skip and pytest still exits 0.

THE CONTRACT. Reading the report pytest wrote, it fails (exit 1) on any of:
  - a skipped testcase (incl. xfail) whose reason matches none of the --allow-skip patterns;
  - a failed / errored testcase (pytest's own exit status already covers this; kept so a
    status overridden by a wrapper cannot hide one);
  - fewer EXECUTED (neither skipped, failed nor errored) testcases than --min-executed;
  - a --require-module (a test module's basename, e.g. test_host_control_dbus) with zero
    executed testcases: the file was dropped from collection, or all of it skipped.
Exit 2: unusable input (unreadable report, no testcases at all). Stdlib only.

SEVERAL REPORTS (T-4858, the mcp-adapter-runtime workflow). Given more than one report, the
check is a UNION over them, keyed by testcase id (classname, name): an id that failed or errored
in ANY report is a problem; an id that skipped is a problem only if it EXECUTED in NO report;
--min-executed counts distinct ids executed in at least one report; --require-module needs at
least one executed id of that module across the reports. That job runs the same files under two
interpreters (the hash-locked mcp 2.x venv, and the system python carrying dbus-python), each
proving tiers the other cannot import; a skip is forgiven only where the same test is proven to
have run in the other report, never by reason text. One report behaves exactly as before.

The default is zero allowed skips: the job runs only files whose every tier CAN run on a stock
ubuntu runner. A tier that cannot (real systemd as PID 1, a real system bus, root, the pinned
mcp 2.x runtime) must be left out of the job's file list, with a comment, not allow-listed.
"""
from __future__ import annotations

import argparse
import collections
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


class ReportError(Exception):
    """The report is unusable: exit 2, not a per-test problem."""


def _module(case: ET.Element) -> str:
    parts = (case.get("classname") or "").split(".")
    # tools.test_x.Class -> test_x ; tools.test_x (module-level function) -> test_x
    named = [p for p in parts if p.startswith("test_")]
    return named[0] if named else (parts[0] if parts else "")


def _outcome(case: ET.Element) -> str:
    if case.find("failure") is not None:
        return "failed"
    if case.find("error") is not None:
        return "error"
    skipped = case.find("skipped")
    if skipped is not None:
        return "xfail" if skipped.get("type") == "pytest.xfail" else "skipped"
    return "executed"


def _skip_reason(case: ET.Element) -> str:
    skipped = case.find("skipped")
    return ((skipped.get("message") if skipped is not None else "") or "").strip()


def _load(report: Path) -> ET.Element:
    try:
        return ET.parse(str(report)).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ReportError(f"cannot read pytest report {report}: {exc}") from exc


def summarize(report: Path) -> dict[str, collections.Counter]:
    """module -> Counter of outcomes."""
    root = _load(report)
    out: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for case in root.iter("testcase"):
        out[_module(case)][_outcome(case)] += 1
    if not out:
        raise ReportError(f"no testcases in {report}")
    return out


def _case_id(case: ET.Element) -> tuple[str, str]:
    return (case.get("classname") or "", case.get("name") or "")


def check(report: Path, min_executed: int, allow_skip: list[str], require_modules: list[str]) -> list[str]:
    """Every way the report falls short of "the D-Bus tiers ran". [] = they did."""
    try:
        allowed = [re.compile(p) for p in allow_skip]
    except re.error as exc:
        raise ReportError(f"bad --allow-skip pattern: {exc}") from exc
    root = _load(report)
    stats = summarize(report)
    problems: list[str] = []
    for case in root.iter("testcase"):
        outcome = _outcome(case)
        who = f"{case.get('classname')}::{case.get('name')}"
        if outcome in ("failed", "error"):
            problems.append(f"{who}: {outcome}")
        elif outcome in ("skipped", "xfail"):
            reason = _skip_reason(case)
            if not any(p.search(reason) for p in allowed):
                problems.append(f"{who}: {outcome} ({reason or 'no reason'}) -- the D-Bus tiers must not skip")
    executed = sum(c["executed"] for c in stats.values())
    if executed < min_executed:
        problems.append(f"only {executed} testcases executed, floor is {min_executed} "
                        f"(collection narrowed, or tiers silently skipped)")
    for module in require_modules:
        if stats.get(module, collections.Counter())["executed"] == 0:
            problems.append(f"{module}: required module has no executed testcase (dropped or fully skipped)")
    return problems


def check_union(reports: list[Path], min_executed: int, allow_skip: list[str],
                require_modules: list[str]) -> list[str]:
    """The several-report form (T-4858): every testcase id must execute in SOME report and fail in
    none. [] = every tier ran somewhere and nothing failed."""
    if len(reports) == 1:
        return check(reports[0], min_executed, allow_skip, require_modules)
    try:
        allowed = [re.compile(p) for p in allow_skip]
    except re.error as exc:
        raise ReportError(f"bad --allow-skip pattern: {exc}") from exc
    executed_ids: set[tuple[str, str]] = set()
    skipped: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    problems: list[str] = []
    for report in reports:
        root = _load(report)
        summarize(report)  # exit 2 on a report with no testcases at all
        for case in root.iter("testcase"):
            outcome, cid = _outcome(case), _case_id(case)
            if outcome == "executed":
                executed_ids.add(cid)
            elif outcome in ("failed", "error"):
                problems.append(f"{cid[0]}::{cid[1]}: {outcome} (in {report.name})")
            else:
                skipped[cid].append(f"{outcome} in {report.name} ({_skip_reason(case) or 'no reason'})")
    for cid in sorted(set(skipped) - executed_ids):
        reasons = skipped[cid]
        if not any(p.search(r) for p in allowed for r in reasons):
            problems.append(f"{cid[0]}::{cid[1]}: executed in no report -- {'; '.join(reasons)}")
    if len(executed_ids) < min_executed:
        problems.append(f"only {len(executed_ids)} distinct testcases executed across {len(reports)} reports, "
                        f"floor is {min_executed} (collection narrowed, or tiers silently skipped)")
    executed_modules = collections.Counter(_module(ET.Element("testcase", classname=c)) for c, _ in executed_ids)
    for module in require_modules:
        if executed_modules[module] == 0:
            problems.append(f"{module}: required module has no executed testcase in any report "
                            f"(dropped or fully skipped)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail unless the D-Bus test tiers executed (no skips).")
    parser.add_argument("report", type=Path, nargs="+",
                        help="the file(s) given to pytest --junitxml; several = a union (T-4858)")
    parser.add_argument("--min-executed", type=int, required=True, help="floor on executed testcases")
    parser.add_argument("--allow-skip", action="append", default=[], metavar="REGEX",
                        help="a skip reason (regex, searched) that is tolerated; default none")
    parser.add_argument("--require-module", action="append", default=[], metavar="NAME",
                        help="a test module basename that must have >=1 executed testcase")
    args = parser.parse_args(argv)
    try:
        problems = check_union(args.report, args.min_executed, args.allow_skip, args.require_module)
        per_report = [(r, summarize(r)) for r in args.report]
    except ReportError as exc:
        print(f"check_dbus_tier_outcomes: {exc}", file=sys.stderr)
        return 2
    for report, stats in per_report:
        if len(per_report) > 1:
            print(f"{report.name}:")
        for module in sorted(stats):
            c = stats[module]
            print(f"  {module}: executed={c['executed']} skipped={c['skipped'] + c['xfail']} "
                  f"failed={c['failed'] + c['error']}")
    if problems:
        print(f"check_dbus_tier_outcomes: {len(problems)} problem(s), the D-Bus tiers did not all run:",
              file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    if len(per_report) == 1:
        stats = per_report[0][1]
        print(f"dbus tier outcomes: {sum(c['executed'] for c in stats.values())} executed, 0 skipped, 0 failed")
    else:
        union = set()
        for report in args.report:
            union |= {_case_id(c) for c in _load(report).iter("testcase") if _outcome(c) == "executed"}
        print(f"union outcomes over {len(per_report)} reports: {len(union)} distinct executed, "
              f"0 executed in no report, 0 failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
