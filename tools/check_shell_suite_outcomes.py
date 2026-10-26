#!/usr/bin/env python3
"""check_shell_suite_outcomes.py -- every tools/test_*.sh must have PASSED in the pytest report (T-4280).

    python tools/check_shell_suite_outcomes.py <pytest --junitxml report> [--tools-dir DIR]

WHY THIS EXISTS. CI runs the shell suites exactly once, as cases of
tools/test_shell_suites.py inside `pytest tools/`. The retired tools/run_shell_suites.sh
(T-2330) was a second, independent net whose guarantee was an OUTCOME: every suite ran and a
red suite was red, whatever pytest had been told. Guards that read the tree for the
SPELLINGS that switch the wrapper off (a skip mark, a body pytest.skip, a collect_ignore, a
failure converted to a skip, a skip keyed on the suite name, ...) close one spelling per
review round and can never be complete. This reads what pytest REPORTED instead, so it does
not care how a suite was neutered, only that it did not pass.

THE CONTRACT. Discover tools/test_*.sh from the directory (an enumeration independent of the
wrapper's own). For EACH suite the report must hold exactly ONE testcase
`test_shell_suite_passes[<suite>]` from a `test_shell_suites` module with no <skipped>,
<failure> or <error> child. Reported as a problem, each naming the suite: missing (never ran:
deselected, ignored, not collected), skipped (a skip, or a mark), xfail (<skipped
type=pytest.xfail>), failed / errored (incl. a strict XPASS, which pytest reports as a
failure), reported more than once, or reported for a suite that is not on disk. A report with
no wrapper cases at all is a problem; one that cannot be read is exit 2. No suite on disk is
refused, never a clean sweep.

AN INDEPENDENT JUDGE OF THE SUITE, NOT OF THE WRAPPER. A case pytest calls PASSED only says the
wrapper's own verdict was "pass". The wrapper therefore records the suite's REAL result on the
testcase with `record_property` (`suite_returncode`, `suite_killed`), right after run_suite
returns and BEFORE it judges anything. For a passed case this then requires, on that testcase,
exactly one `suite_returncode` that is the integer 0 and exactly one `suite_killed` that is
false. A missing property means the suite never ran (an early `return` keyed on the suite
name); a non-zero code fails whatever the wrapper decided (a weakened pass criterion). So
neither a rewritten verdict nor a skipped run can change what this reports, the way nothing
sat between the retired runner and its suites.

KNOWN LIMITS, all deliberate-forgery or cosmetic: (1) a report cannot show a FORGED result, i.e.
a `record_property` written by hand, a rewritten run_suite, or a hand-built SuiteResult fed
through the honest record (`res = SuiteResult(0, ...) if script.name == X else run_suite(
script)`) that reports a result the suite did not produce; that is deliberate forgery, which
code review must catch. (2) A NON-strict
XPASS is a passed case with the real code recorded, so it reads as a pass (the suite did run
and exit 0); test_shell_suite_coverage.py flags the xfail mark. (3) A suite file whose name is
not ASCII: pytest escapes the parametrize id (`test_é.sh` becomes `test_\\xe9.sh`), so the suite
reads as missing and the escaped id as not on disk. It fails closed and no such file exists.

Exit codes: 0 all passed; 1 at least one problem (each printed on stderr); 2 unusable input
(unreadable / unparseable report, or no suites on disk). Stdlib only.
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
_WRAPPER_MODULE = "test_shell_suites"
_CASE = re.compile(r"^test_shell_suite_passes\[(?P<suite>.+)\]$")


class ReportError(Exception):
    """The report (or the suite discovery) is unusable: exit 2, not a per-suite problem."""


def discover(tools_dir: Path = TOOLS) -> list[str]:
    """Names of the `test_*.sh` directly under `tools_dir`, sorted. By glob, never a list."""
    return sorted(p.name for p in Path(tools_dir).glob("test_*.sh"))


def _outcome(case: ET.Element) -> str:
    """One word for how a testcase ended, from its <failure>/<error>/<skipped> children."""
    if case.find("failure") is not None:
        return "failed"
    if case.find("error") is not None:
        return "error"
    skipped = case.find("skipped")
    if skipped is not None:
        return "xfail" if skipped.get("type") == "pytest.xfail" else "skipped"
    return "passed"


def _detail(case: ET.Element) -> str:
    for tag in ("failure", "error", "skipped"):
        child = case.find(tag)
        if child is not None:
            text = (child.get("message") or child.get("type") or "").strip()
            return text.splitlines()[0] if text else ""  # one line per suite in a CI log
    return ""


def _recorded(case: ET.Element) -> dict[str, list[str]]:
    """The record_property pairs on THIS testcase only (its own <properties>), name -> values."""
    found: dict[str, list[str]] = {}
    for block in case.findall("properties"):
        for prop in block.findall("property"):
            found.setdefault(prop.get("name") or "", []).append(prop.get("value") or "")
    return found


def _real_result_problem(case: ET.Element) -> str | None:
    """Why a case pytest called PASSED still does not show the suite passing, or None.

    The wrapper records the suite's REAL result (`suite_returncode`, `suite_killed`) right after
    run_suite returns and before it judges anything, so this is an independent verdict on the
    suite, like the retired runner's: a missing record means the suite never ran (an early
    return), and a non-zero code fails whatever the wrapper decided about it."""
    record = _recorded(case)
    for name in ("suite_returncode", "suite_killed"):
        values = record.get(name, [])
        if not values:
            return (f"passed but recorded no {name} (the suite never ran, or the wrapper did not "
                    f"record its result)")
        if len(values) > 1:
            return f"{name} recorded {len(values)} times (expected exactly 1)"
    killed = record["suite_killed"][0].strip()
    if killed.lower() != "false":
        return f"the watchdog killed the suite (suite_killed={killed})"
    raw = record["suite_returncode"][0].strip()
    try:
        code = int(raw)
    except ValueError:
        return f"suite_returncode={raw!r} is not an integer"
    if code != 0:
        return f"suite_returncode={code}: the wrapper reported it passed but the suite did not exit 0"
    return None


def _read(report: Path) -> dict[str, list[ET.Element]]:
    """suite -> its wrapper testcases in the report."""
    try:
        root = ET.parse(str(report)).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ReportError(f"cannot read pytest report {report}: {exc}") from exc
    cases: dict[str, list[ET.Element]] = {}
    for case in root.iter("testcase"):
        if (case.get("classname") or "").rsplit(".", 1)[-1] != _WRAPPER_MODULE:
            continue
        match = _CASE.match(case.get("name") or "")
        if match:
            cases.setdefault(match.group("suite"), []).append(case)
    return cases


def check(report: Path, suites: list[str]) -> list[str]:
    """Every way the report falls short of "each suite passed exactly once". [] = all passed."""
    cases = _read(Path(report))
    problems: list[str] = []
    if not cases:  # say so once, then name every suite below as well
        problems.append(f"no test_shell_suite_passes testcases in {report} at all: the wrapper did not run "
                        f"(deselected, ignored or not collected)")
    for suite in suites:
        found = cases.get(suite, [])
        if not found:
            problems.append(f"{suite}: no testcase test_shell_suite_passes[{suite}] in the report "
                            f"(never ran: deselected, ignored or not collected)")
        elif len(found) > 1:
            problems.append(f"{suite}: reported {len(found)} times (expected exactly 1)")
        else:
            outcome = _outcome(found[0])
            if outcome != "passed":
                detail = _detail(found[0])
                problems.append(f"{suite}: {outcome}" + (f" ({detail})" if detail else ""))
            else:
                why = _real_result_problem(found[0])
                if why:
                    problems.append(f"{suite}: {why}")
    for suite in sorted(set(cases) - set(suites)):
        problems.append(f"{suite}: in the report but no tools/{suite} exists on disk "
                        f"(discovery and the wrapper disagree)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail unless every tools/test_*.sh PASSED in a pytest junit report.")
    parser.add_argument("report", type=Path, help="the file given to pytest --junitxml")
    parser.add_argument("--tools-dir", type=Path, default=TOOLS, help="where to discover test_*.sh")
    args = parser.parse_args(argv)
    suites = discover(args.tools_dir)
    if not suites:
        print(f"check_shell_suite_outcomes: FOUND NO test_*.sh in {args.tools_dir} -- refusing to "
              f"report success", file=sys.stderr)
        return 2
    try:
        problems = check(args.report, suites)
    except ReportError as exc:
        print(f"check_shell_suite_outcomes: {exc}", file=sys.stderr)
        return 2
    if problems:
        print(f"check_shell_suite_outcomes: {len(problems)} problem(s), the shell suites did not all pass:",
              file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"shell suite outcomes: {len(suites)} of {len(suites)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
