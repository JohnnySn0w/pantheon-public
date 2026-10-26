#!/usr/bin/env python3
"""check_suite_outcome_floor.py -- the Python suite must have RUN, judged by outcome (T-4372).

    python tools/check_suite_outcome_floor.py <pytest --junitxml report> [--baseline FILE]

WHY THIS EXISTS. tools/test_ci_integrity.py (T-4333) reads the workflow TEXT for the spellings
that neuter CI's `pytest tools/` step (a -p no:X, a -k, an addopts, a conftest hook, ...). That
class is open: each review round found a new spelling, and its named residual (an EARLIER step
that writes tools/conftest.py) cannot be read statically at all. This reads what pytest
REPORTED instead, so it does not care HOW the suite was neutered, only that fewer tests ran or
more were skipped than the committed baseline allows: step config, conftest hooks, plugin
disabling, collection narrowing and an earlier-step writer all show up here as a count.

THE CONTRACT. Over the <testcase> elements in the report, each (classname, name) pair counted
ONCE, so a duplicated testcase cannot pad a drop:
  executed = pairs with no <skipped> child (passed + failed + error; a failure still RAN),
  skipped  = pairs with a <skipped> child (a skip, a skip mark, or an xfail: pytest writes
             xfail as <skipped type="pytest.xfail">, and a mass xfail is a neutering route),
  failed   = testcases with a <failure> or <error> child (a strict XPASS is a <failure>).
ANY failed testcase fails this check (T-4372 r1 M1). The step runs only after every earlier step
passed, pytest included, so a failure in the report next to pytest's exit 0 proves the exit
status was overridden (a conftest `pytest_sessionfinish` setting `session.exitstatus = 0`, say):
the failure is hidden, not absent. There are no false positives in CI; a LOCAL report of a red
run will show them, which is correct.
Deselected tests never reach a junit report (pytest 9 writes no testcase and no attribute for
them), so a deselect shows up as a drop in `executed`, which is why the floor is the primary
number. The committed baseline (tools/ci_outcome_baseline.json) holds:
  executed_min    CI fails if executed < executed_min,
  skipped_max     CI fails if skipped  > skipped_max,
  ratchet_margin  the margin executed_min was set below the observed count; when the slack
                  (executed - executed_min) exceeds 2 x ratchet_margin the check still passes
                  but prints a GitHub `::warning::` asking for the floor to be raised.

THE RATCHET. Both numbers sit a margin away from the observed counts (see the baseline's
`_provenance`), so ordinary variance (environment-dependent skips, a test or two removed) stays
green. Adding tests leaves the floor where it is: raise it deliberately (the pass line prints
the slack, and a `::warning::` fires past 2 x ratchet_margin). A drop BELOW the floor, or a skip jump over the budget, needs the
baseline edited in the SAME PR, which puts the change in front of review; that edit is the
explicit override, and there is no other one (no flag, no env var).

KNOWN LIMITS (deliberate forgery, for code review): a hook that reports tests as passed without
running them, or a step that rewrites this checker or its baseline, keeps the counts intact; and
a neutering smaller than the margin (one small file dropped) passes by design.

Exit codes: 0 within the baseline; 1 a failed/errored testcase, below the floor, or over the skip
budget (each printed on stderr, with how to update the baseline deliberately); 2 unusable input (unreadable or
unparseable report or baseline, a report with no testcases, a malformed baseline). Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
BASELINE = TOOLS / "ci_outcome_baseline.json"
_KEYS = ("executed_min", "skipped_max", "ratchet_margin")
_NAMED_FAILURES = 5  # how many failed testcases a message names
_PROG = "check_suite_outcome_floor"


class InputError(Exception):
    """The report or the baseline is unusable: exit 2 (fail closed), never a pass."""


def load_baseline(path: Path) -> dict[str, int]:
    """{executed_min, skipped_max} from the committed baseline. Keys starting with `_` are
    commentary; any other unknown key is refused (a typo such as `executed_mn` must not silently
    drop the floor)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InputError(f"cannot read baseline {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise InputError(f"baseline {path} must be a JSON object")
    unknown = sorted(k for k in data if k not in _KEYS and not str(k).startswith("_"))
    if unknown:
        raise InputError(f"baseline {path} has unknown key(s) {unknown}; expected {list(_KEYS)}")
    out: dict[str, int] = {}
    for key in _KEYS:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise InputError(f"baseline {path}: {key} must be an integer, got {value!r}")
        out[key] = value
    if out["executed_min"] < 1:
        raise InputError(f"baseline {path}: executed_min must be >= 1 (a zero floor checks nothing)")
    if out["skipped_max"] < 0:
        raise InputError(f"baseline {path}: skipped_max must be >= 0")
    if out["ratchet_margin"] < 1:
        raise InputError(f"baseline {path}: ratchet_margin must be >= 1")
    return out


def count(report: Path) -> dict:
    """{executed, skipped, total, failed: [ids]} over a pytest junit report: executed/skipped per
    unique (classname, name) pair (a pair counts as skipped only if every copy is skipped),
    failed lists each testcase with a <failure> or <error> child."""
    try:
        root = ET.parse(str(report)).getroot()
    except (OSError, ET.ParseError) as exc:
        raise InputError(f"cannot read pytest report {report}: {exc}") from exc
    if root.tag not in ("testsuites", "testsuite"):
        raise InputError(f"{report} is not a junit report (root element <{root.tag}>)")
    ran: dict[tuple[str, str], bool] = {}
    failed: list[str] = []
    for case in root.iter("testcase"):
        key = (case.get("classname") or "", case.get("name") or "")
        ran[key] = ran.get(key, False) or case.find("skipped") is None
        if case.find("failure") is not None or case.find("error") is not None:
            failed.append(f"{key[0]}::{key[1]}")
    if not ran:
        raise InputError(f"{report} holds no testcases at all: pytest collected or ran nothing")
    executed = sum(ran.values())
    return {"executed": executed, "skipped": len(ran) - executed, "total": len(ran), "failed": failed}


def check(counts: dict[str, int], baseline: dict[str, int], baseline_path: Path) -> list[str]:
    """Every way the counts fall outside the baseline. [] = within it."""
    problems: list[str] = []
    failed = counts.get("failed") or []
    if failed:
        named = ", ".join(failed[:_NAMED_FAILURES]) + (f", ... ({len(failed) - _NAMED_FAILURES} more)"
                                                      if len(failed) > _NAMED_FAILURES else "")
        problems.append(f"{len(failed)} testcase(s) FAILED or ERRORED in the report although pytest exited 0 "
                        f"(this step runs only after a green pytest), so the exit status was overridden, e.g. "
                        f"by a pytest_sessionfinish hook or a file an earlier step wrote: {named}")
    executed, floor = counts["executed"], baseline["executed_min"]
    if executed < floor:
        problems.append(f"executed {executed} tests, below the floor executed_min={floor} "
                        f"({floor - executed} short)")
    skipped, budget = counts["skipped"], baseline["skipped_max"]
    if skipped > budget:
        problems.append(f"skipped {skipped} tests (incl. xfail), over the budget skipped_max={budget} "
                        f"({skipped - budget} over)")
    if len(problems) > (1 if failed else 0):
        problems.append(
            f"If fewer tests running is INTENDED (tests deleted or deliberately skipped), edit "
            f"{baseline_path.name} in THIS PR (executed_min / skipped_max, and `_provenance` with the "
            f"new observed counts and why) so review sees the drop. If it is NOT intended, something "
            f"stopped tests running: a conftest hook, a -p no:/-k/-m/--deselect/--ignore, an addopts, "
            f"a file an earlier step wrote, or a collection error. Compare pytest's summary line with "
            f"the baseline.")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail unless a pytest junit report ran at least the "
                                                 "committed floor of tests and skipped no more than its budget.")
    parser.add_argument("report", type=Path, help="the file given to pytest --junitxml")
    parser.add_argument("--baseline", type=Path, default=BASELINE, help="the committed baseline JSON")
    args = parser.parse_args(argv)
    try:
        baseline = load_baseline(args.baseline)
        counts = count(args.report)
    except InputError as exc:
        print(f"{_PROG}: {exc} -- refusing to report success", file=sys.stderr)
        return 2
    problems = check(counts, baseline, args.baseline)
    if problems:
        print(f"{_PROG}: the Python suite's outcome falls short of its committed baseline:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    slack = counts["executed"] - baseline["executed_min"]
    print(f"suite outcome floor: executed {counts['executed']} >= executed_min {baseline['executed_min']} "
          f"(slack {slack}); skipped {counts['skipped']} <= skipped_max {baseline['skipped_max']}; 0 failed")
    if slack > 2 * baseline["ratchet_margin"]:
        print(f"::warning title=Raise the test floor::executed {counts['executed']} is {slack} above "
              f"executed_min {baseline['executed_min']}, more than 2 x ratchet_margin "
              f"({baseline['ratchet_margin']}). Raise executed_min in {args.baseline.name} to about "
              f"{counts['executed'] - baseline['ratchet_margin']} so a future drop is caught.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
