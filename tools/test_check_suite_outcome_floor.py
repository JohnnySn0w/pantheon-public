"""Tests for tools/check_suite_outcome_floor.py -- the OUTCOME backstop under CI's Python suite (T-4372).

tools/test_ci_integrity.py (T-4333) guards the workflow TEXT against the spellings that neuter
`pytest tools/`; that class is open. The checker judges what pytest REPORTED against the
committed baseline (tools/ci_outcome_baseline.json): fewer executed tests than the floor, or more
skipped than the budget, fails CI however the suite was neutered. This file tests the checker on
synthetic reports, that the committed baseline is well-formed, and that ci.yml wires the checker
as its own un-forgivable step after the whole-tree pytest step on the same report.

Fixture provenance (D11): the testcase shapes below were CAPTURED from real pytest 9.1.1 output
(`--junitxml` with `-o junit_family=xunit1`, the family CI uses) over a probe module holding one
each of pass / fail / skip mark / xfail / setup error / teardown error / deselected test. The
report had 6 testcases: the deselected test has no testcase and no attribute (pytest never
reports deselection in junit); a teardown error after a passing call is ONE testcase with an
<error> child; an xfail is <skipped type="pytest.xfail">; the <testsuite> carries
tests/skipped/failures/errors attributes which the checker does not trust over the testcases.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tools import check_suite_outcome_floor as floor
from tools.test_ci_integrity import backstop_step_problems
from tools.test_shell_suite_coverage import (_argv_ambiguities, _code, _invocations, _is_whole_tree, _junit_path,
                                             _load_workflows, _pytest_invocations)

TOOLS = Path(__file__).resolve().parent
_CHECKER = "check_suite_outcome_floor.py"

_SHAPES = {
    "pass": "",
    "fail": '<failure message="assert 0">E   assert 0</failure>',
    "skip": '<skipped type="pytest.skip" message="s">tools/test_probe.py:4: s</skipped>',
    "xfail": '<skipped type="pytest.xfail" message="x" />',
    "error": '<error message="failed on setup with &quot;RuntimeError: b&quot;">RuntimeError</error>',
    "teardown_error": '<error message="failed on teardown with &quot;RuntimeError: td&quot;">RuntimeError</error>',
    "xpass_strict": '<failure message="[XPASS(strict)] probe strict">[XPASS(strict)] probe strict</failure>',
}


def _report(**counts: int) -> str:
    """A junit report with `counts[shape]` testcases of each shape (captured shapes above)."""
    cases = []
    for shape, n in counts.items():
        for i in range(n):
            body = _SHAPES[shape]
            head = f'<testcase classname="tools.test_probe" name="test_{shape}_{i}" file="tools/test_probe.py" line="1" time="0.001"'
            cases.append(f"{head}>{body}</testcase>" if body else f"{head} />")
    return ('<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest" '
            f'errors="0" failures="0" skipped="0" tests="{len(cases)}" time="0.3">' + "".join(cases)
            + "</testsuite></testsuites>")


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory(prefix="t4372_")
        self.addCleanup(self._dir.cleanup)
        self.dir = Path(self._dir.name)

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def baseline(self, executed_min: Any = 10, skipped_max: Any = 3, ratchet_margin: Any = 5,
                 **extra: Any) -> Path:
        return self.write("baseline.json", json.dumps({"executed_min": executed_min, "skipped_max": skipped_max,
                                                       "ratchet_margin": ratchet_margin, **extra}))

    def run_main(self, report: Path, baseline: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = floor.main([str(report), "--baseline", str(baseline)])
        return code, out.getvalue(), err.getvalue()


class Counting(_Tmp):
    def test_every_shape_is_counted_the_way_the_contract_says(self):
        report = self.write("r.xml", _report(**{"pass": 4, "fail": 1, "error": 1, "teardown_error": 1,
                                                "skip": 2, "xfail": 3}))
        counts = floor.count(report)
        self.assertEqual({k: counts[k] for k in ("executed", "skipped", "total")},
                         {"executed": 7, "skipped": 5, "total": 12})
        self.assertEqual(len(counts["failed"]), 3)

    def test_a_duplicated_testcase_counts_once(self):
        """N3: copies of one (classname, name) must not pad the executed figure over a drop."""
        case = '<testcase classname="tools.test_a" name="test_x" />'
        report = self.write("r.xml", f"<testsuites><testsuite>{case * 50}"
                                     '<testcase classname="tools.test_b" name="test_x" /></testsuite></testsuites>')
        self.assertEqual(floor.count(report)["executed"], 2)

    def test_a_pair_skipped_in_one_copy_and_run_in_another_counts_as_executed(self):
        report = self.write("r.xml", '<testsuites><testsuite><testcase classname="a" name="t"><skipped /></testcase>'
                                     '<testcase classname="a" name="t" /></testsuite></testsuites>')
        self.assertEqual((floor.count(report)["executed"], floor.count(report)["skipped"]), (1, 0))

    def test_a_bare_testsuite_root_is_read_too(self):
        report = self.write("r.xml", '<testsuite tests="1"><testcase classname="a" name="t" /></testsuite>')
        self.assertEqual(floor.count(report)["executed"], 1)

    def test_testsuite_attributes_are_not_trusted_over_the_testcases(self):
        report = self.write("r.xml", '<testsuites><testsuite tests="9999" skipped="0">'
                                     '<testcase classname="a" name="t" /></testsuite></testsuites>')
        self.assertEqual(floor.count(report)["executed"], 1)


class Verdicts(_Tmp):
    def test_within_the_baseline_passes_and_reports_the_slack(self):
        code, out, err = self.run_main(self.write("r.xml", _report(**{"pass": 12, "skip": 2})), self.baseline())
        self.assertEqual(code, 0, err)
        self.assertIn("executed 12 >= executed_min 10", out)
        self.assertIn("slack 2", out)
        self.assertNotIn("::warning", out)

    def test_slack_past_twice_the_margin_warns_but_passes(self):
        """N4: the ratchet gets raised without failing CI."""
        code, out, err = self.run_main(self.write("r.xml", _report(**{"pass": 21})), self.baseline())
        self.assertEqual(code, 0, err)
        self.assertIn("::warning title=Raise the test floor::", out)
        self.assertIn("to about 16", out)
        code, out, _ = self.run_main(self.write("r.xml", _report(**{"pass": 20})), self.baseline())
        self.assertEqual(code, 0)
        self.assertNotIn("::warning", out, "exactly 2 x margin is not past it")

    def test_exactly_at_the_floor_and_the_budget_passes(self):
        code, _, err = self.run_main(self.write("r.xml", _report(**{"pass": 10, "skip": 3})), self.baseline())
        self.assertEqual(code, 0, err)

    def test_failures_and_errors_count_as_executed_for_the_floor(self):
        report = self.write("r.xml", _report(**{"pass": 7, "fail": 1, "error": 1, "teardown_error": 1}))
        self.assertEqual(floor.count(report)["executed"], 10)

    def test_any_failure_fails_because_a_green_pytest_hid_it(self):
        """M1: a pytest_sessionfinish that sets exitstatus 0 leaves failures in the report."""
        for shape in ("fail", "error", "teardown_error", "xpass_strict"):
            with self.subTest(shape=shape):
                code, _, err = self.run_main(self.write("r.xml", _report(**{"pass": 20, shape: 1})), self.baseline())
                self.assertEqual(code, 1, err)
                self.assertIn("1 testcase(s) FAILED or ERRORED", err)
                self.assertIn(f"tools.test_probe::test_{shape}_0", err)
                self.assertIn("exit status was overridden", err)
                self.assertNotIn("in THIS PR", err, "a hidden failure is not a baseline matter")

    def test_many_failures_are_named_a_few_at_a_time(self):
        code, _, err = self.run_main(self.write("r.xml", _report(**{"pass": 20, "fail": 8})), self.baseline())
        self.assertEqual(code, 1)
        self.assertIn("8 testcase(s)", err)
        self.assertIn("(3 more)", err)
        self.assertNotIn("test_fail_7", err)

    def test_below_the_floor_fails_and_says_how_to_update_the_baseline(self):
        code, _, err = self.run_main(self.write("r.xml", _report(**{"pass": 9})), self.baseline())
        self.assertEqual(code, 1)
        self.assertIn("below the floor executed_min=10 (1 short)", err)
        self.assertIn("baseline.json in THIS PR", err)

    def test_a_whole_suite_neutered_to_one_test_fails(self):
        """The -p no:unittest / conftest-deselect-everything shape: a handful ran, green."""
        self.assertEqual(self.run_main(self.write("r.xml", _report(**{"pass": 1})), self.baseline())[0], 1)

    def test_a_skip_jump_fails_even_when_the_floor_holds(self):
        code, _, err = self.run_main(self.write("r.xml", _report(**{"pass": 20, "skip": 4})), self.baseline())
        self.assertEqual(code, 1)
        self.assertIn("over the budget skipped_max=3 (1 over)", err)

    def test_a_mass_xfail_counts_against_the_skip_budget(self):
        code, _, err = self.run_main(self.write("r.xml", _report(**{"pass": 20, "xfail": 4})), self.baseline())
        self.assertEqual(code, 1)
        self.assertIn("skipped 4", err)

    def test_skips_that_replace_executed_tests_trip_both(self):
        code, _, err = self.run_main(self.write("r.xml", _report(**{"pass": 5, "skip": 9})), self.baseline())
        self.assertEqual(code, 1)
        self.assertIn("below the floor", err)
        self.assertIn("over the budget", err)


class FailsClosed(_Tmp):
    def assert_unusable(self, report: Path, baseline: Path, needle: str) -> None:
        code, out, err = self.run_main(report, baseline)
        self.assertEqual(code, 2, (out, err))
        self.assertIn(needle, err)
        self.assertIn("refusing to report success", err)
        self.assertEqual(out, "")

    def test_a_missing_report(self):
        self.assert_unusable(self.dir / "nope.xml", self.baseline(), "cannot read pytest report")

    def test_a_malformed_report(self):
        self.assert_unusable(self.write("r.xml", "<testsuites><testcase"), self.baseline(), "cannot read pytest report")

    def test_an_empty_report_file(self):
        self.assert_unusable(self.write("r.xml", ""), self.baseline(), "cannot read pytest report")

    def test_a_report_with_no_testcases(self):
        self.assert_unusable(self.write("r.xml", "<testsuites><testsuite tests='0' /></testsuites>"),
                             self.baseline(), "no testcases at all")

    def test_xml_that_is_not_a_junit_report(self):
        self.assert_unusable(self.write("r.xml", "<html><testcase /></html>"), self.baseline(), "not a junit report")

    def test_a_missing_baseline(self):
        self.assert_unusable(self.write("r.xml", _report(**{"pass": 20})), self.dir / "nope.json",
                             "cannot read baseline")

    def test_a_malformed_baseline(self):
        self.assert_unusable(self.write("r.xml", _report(**{"pass": 20})), self.write("b.json", "{not json"),
                             "cannot read baseline")

    def test_baseline_shapes_that_would_weaken_the_check_are_refused(self):
        report = self.write("r.xml", _report(**{"pass": 20}))
        for text, needle in (
            ("[10, 3]", "must be a JSON object"),
            ('{"skipped_max": 3}', "executed_min must be an integer"),
            ('{"executed_min": 10}', "skipped_max must be an integer"),
            ('{"executed_min": 10, "skipped_max": 3}', "ratchet_margin must be an integer"),
            ('{"executed_min": 10, "skipped_max": 3, "ratchet_margin": 0}', "ratchet_margin must be >= 1"),
            ('{"executed_min": "10", "skipped_max": 3, "ratchet_margin": 5}', "executed_min must be an integer"),
            ('{"executed_min": 10.5, "skipped_max": 3, "ratchet_margin": 5}', "executed_min must be an integer"),
            ('{"executed_min": true, "skipped_max": 3, "ratchet_margin": 5}', "executed_min must be an integer"),
            ('{"executed_min": 0, "skipped_max": 3, "ratchet_margin": 5}', "executed_min must be >= 1"),
            ('{"executed_min": 10, "skipped_max": -1, "ratchet_margin": 5}', "skipped_max must be >= 0"),
            ('{"executed_mn": 10, "executed_min": 10, "skipped_max": 3, "ratchet_margin": 5}', "unknown key"),
        ):
            with self.subTest(text=text):
                self.assert_unusable(report, self.write("b.json", text), needle)

    def test_underscore_keys_are_commentary(self):
        code, _, err = self.run_main(self.write("r.xml", _report(**{"pass": 20})),
                                     self.baseline(_provenance={"observed_executed": 20}, _comment="x"))
        self.assertEqual(code, 0, err)

    def test_the_script_runs_standalone_with_the_committed_baseline(self):
        """The CI invocation shape: `python tools/check_suite_outcome_floor.py <report>`, no flags."""
        report = self.write("r.xml", _report(**{"pass": 1}))
        proc = subprocess.run([sys.executable, str(TOOLS / _CHECKER), str(report)],
                              capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": ""})
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("ci_outcome_baseline.json in THIS PR", proc.stderr)


class TheCommittedBaseline(unittest.TestCase):
    def test_it_parses_and_is_a_real_floor(self):
        base = floor.load_baseline(floor.BASELINE)
        self.assertGreaterEqual(base["executed_min"], 1000,
                                "the floor is far below the real suite (~12.8K tests); a floor this low checks little")
        self.assertLess(base["skipped_max"], base["executed_min"])

    def test_it_records_its_provenance(self):
        data = json.loads(floor.BASELINE.read_text(encoding="utf-8"))
        prov = data.get("_provenance")
        self.assertIsInstance(prov, dict, "the baseline must say where its numbers came from")
        self.assertLessEqual(data["executed_min"], prov["observed_executed"],
                             "the floor sits AT OR BELOW the observed executed count, never above it")
        self.assertGreaterEqual(data["skipped_max"], prov["observed_skipped"],
                                "the skip budget sits AT OR ABOVE the observed skipped count")


# --- ci.yml wiring -------------------------------------------------------------------------


def _floor_invocations(run: str) -> list[list[str]]:
    return _invocations(run, lambda tok: tok.endswith("/" + _CHECKER) or tok == _CHECKER)


def _step_problems(step: dict, path: str, doc: dict, job: dict) -> list[str]:
    """The floor step's exit status must reach the job untouched: `run:` is exactly
    `<python> tools/check_suite_outcome_floor.py <report>` on one line, no `if:`, no truthy
    `continue-on-error`, and none of the keys that disarm it without touching `run:` (`shell:`,
    `working-directory`, step `env`: test_ci_integrity.backstop_step_problems, shared with the
    T-4280 shell-suite outcome step). The job-level keys are test_shell_suite_coverage.py's
    _job_problems, which already covers this job because it holds the whole-tree pytest step."""
    why: list[str] = backstop_step_problems(doc, job, step)
    if step.get("continue-on-error") not in (None, False, "false"):
        why.append("has continue-on-error, so a red floor would not fail the job")
    if "if" in step:
        why.append("has an `if:` condition, so the floor can be skipped")
    code = _code(step.get("run") or "").strip()
    try:
        tokens = shlex.split(code, comments=True)
    except ValueError:
        tokens = []
    exact = (len(tokens) == 3 and len(code.splitlines()) == 1
             and os.path.basename(tokens[0]).startswith("python")
             and os.path.basename(tokens[1]) == _CHECKER and tokens[2] == path)
    if not exact:
        why.append(f"must run exactly `python tools/{_CHECKER} {path}` and nothing else (a flag such as "
                   f"--baseline, a pipe, `||`, `;`, redirect or trailing command can defeat it)")
    return why


def floor_wiring_violations(docs: dict[str, dict]) -> list[str]:
    """Every whole-tree pytest step that writes a junit report must be followed, in the SAME job,
    by a step running the floor checker on that SAME path. [] = wired. Whether there is exactly
    one whole-tree step, and that it writes a report, is test_shell_suite_coverage.py's job."""
    out: list[str] = []
    for name, doc in docs.items():
        for job_name, job in (doc.get("jobs") or {}).items():
            steps = job.get("steps") or []
            for i, step in enumerate(steps):
                for args in _pytest_invocations(step.get("run") or ""):
                    label = f"{name}: job {job_name!r} step {step.get('name')!r}"
                    if _is_whole_tree(args):  # T-4367 belt: an argv the shared parser reads ambiguously
                        out += [f"{label} {w}" for w in _argv_ambiguities(args)]
                    path = _junit_path(args) if _is_whole_tree(args) else None
                    if not path:
                        continue
                    later = [s for s in steps[i + 1:]
                             if any(path in inv for inv in _floor_invocations(_code(s.get("run") or "")))]
                    if not later:
                        out.append(f"{label} writes {path!r} but no LATER step in the same job runs {_CHECKER} "
                                   f"on it: nothing checks that the Python suite actually ran")
                    for s in later:
                        out += [f"{name}: job {job_name!r} step {s.get('name')!r} {why}"
                                for why in _step_problems(s, path, doc, job)]
    return out


class TheLiveWorkflow(unittest.TestCase):
    def test_ci_yml_wires_the_floor_after_the_pytest_step(self):
        self.assertEqual(floor_wiring_violations(_load_workflows()), [])

    def test_the_wiring_is_really_in_ci_yml(self):
        ci = _load_workflows()["ci.yml"]
        runs = [s.get("run") or "" for s in ci["jobs"]["test"]["steps"]]
        self.assertTrue(any(_floor_invocations(_code(r)) for r in runs), "ci.yml job 'test' never runs the floor")


class TheWiringLogic(unittest.TestCase):
    PYTEST = 'python -m pytest tools/ -q -n auto -o junit_family=xunit1 --junitxml="${{ runner.temp }}/r.xml"'
    SHELL = 'python tools/check_shell_suite_outcomes.py "${{ runner.temp }}/r.xml"'
    FLOOR = 'python tools/check_suite_outcome_floor.py "${{ runner.temp }}/r.xml"'

    @staticmethod
    def doc(*runs: str, **floor_keys: Any) -> dict:
        steps = [{"name": f"s{i}", "run": r} for i, r in enumerate(runs)]
        for s in steps:
            if _CHECKER in s["run"]:
                s.update(floor_keys)
        return {"jobs": {"test": {"steps": steps}}}

    def found(self, *runs: str, **floor_keys: Any) -> list[str]:
        return floor_wiring_violations({"ci.yml": self.doc(*runs, **floor_keys)})

    def test_the_real_shape_passes(self):
        self.assertEqual(self.found(self.PYTEST, self.SHELL, self.FLOOR), [])

    def test_no_floor_step_is_caught(self):
        self.assertTrue(self.found(self.PYTEST, self.SHELL))

    def test_a_floor_before_pytest_is_caught(self):
        self.assertTrue(self.found(self.FLOOR, self.PYTEST))

    def test_a_floor_on_another_report_is_caught(self):
        self.assertTrue(self.found(self.PYTEST, self.FLOOR.replace("r.xml", "other.xml")))

    def test_a_floor_only_in_a_comment_is_caught(self):
        self.assertTrue(self.found(self.PYTEST, "# " + self.FLOOR))

    def test_an_argv_the_shared_parser_reads_ambiguously_is_caught(self):
        """T-4367 belt: the report path is read through the shared parser, so a `--junitxml` it reads
        as a path after `--` or as another option's value is refused, never silently skipped."""
        for extra in ("-- --junitxml=x.xml", "-k --junitxml=x.xml"):
            with self.subTest(extra=extra):
                self.assertTrue(self.found(f"{self.PYTEST} {extra}", self.SHELL, self.FLOOR))

    def test_a_tree_token_swallowed_by_a_repeated_exclusion_flag_is_caught(self):
        """T-4367 r1 M1: `--junitxml --ignore --ignore tools` still counts as a whole-tree step, and is
        refused (the old reader took `--ignore` as its report path and demanded a floor on it)."""
        self.assertTrue(self.found(self.PYTEST, self.SHELL, self.FLOOR,
                                   "python -m pytest --junitxml --ignore --ignore tools"))

    def test_a_floor_in_another_job_is_caught(self):
        doc = {"jobs": {"test": {"steps": [{"name": "p", "run": self.PYTEST}]},
                        "other": {"steps": [{"name": "f", "run": self.FLOOR}]}}}
        self.assertTrue(floor_wiring_violations({"ci.yml": doc}))

    def test_the_floor_step_must_be_exactly_the_invocation(self):
        for run in (self.FLOOR + " || true", self.FLOOR + "; echo ok", self.FLOOR + " | tee x",
                    self.FLOOR + " --baseline /tmp/zero.json", "FOO=1 " + self.FLOOR, self.FLOOR + "\necho after",
                    f"sh -c '{self.FLOOR}'"):
            with self.subTest(run=run):
                self.assertTrue(self.found(self.PYTEST, run), run)

    def test_plain_spellings_are_fine(self):
        for run in (self.FLOOR, 'python3 ./tools/check_suite_outcome_floor.py "${{ runner.temp }}/r.xml"',
                    'python tools/check_suite_outcome_floor.py \\\n  "${{ runner.temp }}/r.xml"'):
            with self.subTest(run=run):
                self.assertEqual(self.found(self.PYTEST, run), [])

    def test_a_floor_step_that_can_be_skipped_or_forgiven_is_caught(self):
        for keys in ({"continue-on-error": True}, {"continue-on-error": "true"}, {"if": "always()"},
                     {"if": "false"}):
            with self.subTest(keys=keys):
                self.assertTrue(self.found(self.PYTEST, self.FLOOR, **keys))

    def test_step_keys_that_switch_the_floor_off_without_touching_run_are_caught(self):
        """r1 M2: `shell:`, `working-directory` and step `env` disarm the step while its `run:` stays
        exact (shared rule: test_ci_integrity.backstop_step_problems)."""
        for keys in ({"shell": "bash -c true {0}"}, {"shell": "python {0}"}, {"shell": "sh -n {0}"},
                     {"working-directory": "tools/hooks"}, {"working-directory": "${{ github.workspace }}/x"},
                     {"env": {"PYTHONPATH": "tools/shim"}}, {"env": {"PYTHONSTARTUP": "x.py"}},
                     {"env": {"BASH_ENV": "tools/x.sh"}}, {"env": {"SHELLOPTS": "noexec"}},
                     {"env": {"PATH": "tools/bin"}}, {"env": {"BASH_FUNC_python%%": "() { :; }"}},
                     {"env": "${{ fromJSON(vars.E) }}"}):
            with self.subTest(keys=keys):
                self.assertTrue(self.found(self.PYTEST, self.SHELL, self.FLOOR, **keys), keys)

    def test_an_expression_on_a_comment_line_of_the_floor_step_is_caught(self):
        """T-4385 r3 MF-A: GitHub substitutes `${{ }}` on a comment line before bash runs, so it can
        inject `exit 0` above the exact floor line; _code() drops the line before the exact check."""
        for comment in ("# ${{ fromJSON('\"\\nexit 0\\n\"') }}", "  # ${{ github.event.head_commit.message }}"):
            with self.subTest(comment=comment):
                found = self.found(self.PYTEST, self.SHELL, f"{comment}\n{self.FLOOR}")
                self.assertTrue(any("(T-4385 r3)" in v for v in found), found)

    def test_an_inherited_default_shell_or_directory_is_caught(self):
        for level in ("job", "workflow"):
            for run in ({"shell": "bash -c true {0}"}, {"working-directory": "sub"}):
                with self.subTest(level=level, run=run):
                    doc = self.doc(self.PYTEST, self.FLOOR)
                    (doc["jobs"]["test"] if level == "job" else doc)["defaults"] = {"run": run}
                    self.assertTrue(floor_wiring_violations({"ci.yml": doc}))

    def test_benign_step_keys_on_the_floor_are_fine(self):
        for keys in ({"shell": "bash"}, {"shell": "bash --noprofile --norc -eo pipefail {0}"},
                     {"working-directory": "."}, {"env": {"LANG": "C.UTF-8"}}):
            with self.subTest(keys=keys):
                self.assertEqual(self.found(self.PYTEST, self.FLOOR, **keys), [])

    def test_a_single_file_pytest_step_needs_no_floor(self):
        self.assertEqual(self.found('pytest tools/test_x.py --junitxml=r.xml'), [])


if __name__ == "__main__":
    unittest.main()
