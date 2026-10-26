"""Tests for tools/check_shell_suite_outcomes.py -- the OUTCOME net under the shell suites (T-4280).

The retired tools/run_shell_suites.sh guaranteed an outcome: every suite actually ran, and a
red one was red, whatever pytest had been told. Guards that read the tree for the SPELLINGS
that switch tools/test_shell_suites.py off (a skip mark, a body pytest.skip, collect_ignore,
...) can be beaten by the next spelling. This checker reads what pytest REPORTED instead.

Fixture provenance (D11): the XML shapes below were CAPTURED from real pytest 9.0.3 output
(`--junitxml` with `-o junit_family=xunit1`, the family CI uses, run under xdist `-n 2`, a
package named `tools` so the classname is
`tools.test_shell_suites`), not hand-invented. A passing case carries the wrapper's
<properties> (record_property: suite_returncode, suite_killed) and no <skipped>/<failure>/
<error> child; a skip (body call or mark) is <skipped type="pytest.skip">; an xfail is
<skipped type="pytest.xfail">; a strict XPASS is <failure message="[XPASS(strict)] ...">; a
failed fixture is <error>. A NON-strict XPASS is a passed case with the real code recorded,
indistinguishable from a pass (the suite did run and exit 0); the source guards in
test_shell_suite_coverage.py flag the xfail mark itself.
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools import check_shell_suite_outcomes as chk

TOOLS = Path(__file__).resolve().parent
CLASSNAME = "tools.test_shell_suites"

_SHAPES = {
    "pass": "",
    "skip": '<skipped type="pytest.skip" message="probe body skip">test_shell_suites.py:25: probe body skip</skipped>',
    "markskip": '<skipped type="pytest.skip" message="probe markskip">test_shell_suites.py:10: probe markskip</skipped>',
    "xfail": '<skipped type="pytest.xfail" message="probe xfail" />',
    "fail": '<failure message="Failed: probe body fail">Failed: probe body fail</failure>',
    "xpass_strict": '<failure message="[XPASS(strict)] probe strict">[XPASS(strict)] probe strict</failure>',
    "error": '<error message="failed on setup with &quot;RuntimeError: fixture boom&quot;">RuntimeError</error>',
}


def _props(rc: object = 0, killed: object = False) -> str:
    """The <properties> block pytest writes for record_property (captured shape)."""
    return (f'<properties><property name="suite_returncode" value="{rc}" />'
            f'<property name="suite_killed" value="{killed}" /></properties>')


def _case(suite: str, shape: str = "pass", classname: str = CLASSNAME, name: str | None = None,
          props: str | None = None) -> str:
    """`props=None` -> the wrapper's normal record (rc 0, not killed) on a passing case, none on
    the other shapes; `props=""` -> no properties at all; any other string is used verbatim."""
    if props is None:
        props = _props() if shape == "pass" else ""
    body = props + _SHAPES[shape]
    name = name or f"test_shell_suite_passes[{suite}]"
    if not body:
        return f'<testcase classname="{classname}" name="{name}" time="0.005" />'
    return f'<testcase classname="{classname}" name="{name}" time="0.003">{body}</testcase>'


def _report(*cases: str) -> str:
    return ('<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest" '
            f'tests="{len(cases)}" time="1.0" hostname="h">{"".join(cases)}</testsuite></testsuites>')


# Captured from a real run under xdist -n 2 with -o junit_family=xunit1, the family CI
# uses (bodies trimmed); see the module docstring. The
# scratch test records suite_returncode / suite_killed first thing in its body, the way the
# wrapper does, so a skip/xfail/fail case carries the properties too; test_norec.sh "passes"
# having recorded nothing (an early return) and test_error.sh errors in setup.
_PROPS_0 = ('<properties><property name="suite_returncode" value="0" />'
            '<property name="suite_killed" value="False" /></properties>')
_PROPS_1 = _PROPS_0.replace('value="0"', 'value="1"')
_REAL_CAPTURE = (
    '<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest" '
    'errors="1" failures="2" skipped="3" tests="9" time="2.262" timestamp="2026-09-29T23:23:49.003150+00:00" '
    'hostname="swarm">'
    '<testcase classname="tools.test_shell_suites" name="test_shell_suite_passes[test_xfail.sh]" file="tools/test_shell_suites.py" line="9" time="0.003">'
    + _PROPS_0 + '<skipped type="pytest.xfail" message="probe xfail" /></testcase>'
    '<testcase classname="tools.test_shell_suites" name="test_shell_suite_passes[test_pass.sh]" file="tools/test_shell_suites.py" line="9" time="0.012">'
    + _PROPS_0 + '</testcase>'
    '<testcase classname="tools.test_shell_suites" name="test_shell_suite_passes[test_skip.sh]" file="tools/test_shell_suites.py" line="9" time="0.005">'
    + _PROPS_0 + '<skipped type="pytest.skip" message="probe body skip">..</skipped></testcase>'
    '<testcase classname="tools.test_shell_suites" name="test_shell_suite_passes[test_xpass_loose.sh]" file="tools/test_shell_suites.py" line="9" time="0.001">'
    + _PROPS_0 + '</testcase>'
    '<testcase classname="tools.test_shell_suites" name="test_shell_suite_passes[test_xpass_strict.sh]" file="tools/test_shell_suites.py" line="9" time="0.003">'
    + _PROPS_0 + '<failure message="[XPASS(strict)] probe strict">..</failure></testcase>'
    '<testcase classname="tools.test_shell_suites" name="test_shell_suite_passes[test_fail.sh]" file="tools/test_shell_suites.py" line="9" time="0.004">'
    + _PROPS_1 + '<failure message="Failed: probe body fail">..</failure></testcase>'
    '<testcase classname="tools.test_shell_suites" name="test_shell_suite_passes[test_error.sh]" file="tools/test_shell_suites.py" line="9" time="0.002">'
    '<error message="failed on setup with &quot;RuntimeError: fixture boom&quot;">..</error></testcase>'
    '<testcase classname="tools.test_shell_suites" name="test_shell_suite_passes[test_norec.sh]" file="tools/test_shell_suites.py" line="9" time="0.002" />'
    '<testcase classname="tools.test_shell_suites" name="test_shell_suite_passes[test_markskip.sh]" file="tools/test_shell_suites.py" line="9" time="0.001">'
    '<skipped type="pytest.skip" message="probe markskip">..</skipped></testcase>'
    '</testsuite></testsuites>'
)

SUITES = ["test_a.sh", "test_b.sh", "test_c.sh"]


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def write(self, text: str, name: str = "report.xml") -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path


class CheckTests(_Tmp):
    def problems(self, *cases: str, suites=SUITES) -> list[str]:
        return chk.check(self.write(_report(*cases)), list(suites))

    def assertNames(self, problems: list[str], suite: str, word: str) -> None:
        hits = [p for p in problems if suite in p]
        self.assertTrue(hits, f"{suite} is not named in {problems}")
        self.assertTrue(any(word in p for p in hits), f"{word!r} not in {hits}")

    def test_every_suite_passed_is_clean(self):
        self.assertEqual(self.problems(*(_case(s) for s in SUITES)), [])

    def test_a_skipped_suite_is_named(self):
        for shape in ("skip", "markskip"):
            with self.subTest(shape=shape):
                found = self.problems(_case("test_a.sh"), _case("test_b.sh", shape), _case("test_c.sh"))
                self.assertNames(found, "test_b.sh", "skipped")
                self.assertEqual(len(found), 1, found)

    def test_an_xfailed_suite_is_named(self):
        found = self.problems(_case("test_a.sh"), _case("test_b.sh"), _case("test_c.sh", "xfail"))
        self.assertNames(found, "test_c.sh", "xfail")
        self.assertEqual(len(found), 1, found)

    def test_a_multiline_message_is_reported_on_one_line(self):
        """The wrapper's skip/fail messages carry the last 40 lines of the suite; the report
        must stay one line per suite so a CI log is scannable."""
        multi = ('<testcase classname="tools.test_shell_suites" name="test_shell_suite_passes[test_a.sh]" time="1">'
                 '<skipped type="pytest.skip" message="test_a.sh exited 1&#10;--- last 40 lines ---&#10;boom" />'
                 '</testcase>')
        found = self.problems(multi, _case("test_b.sh"), _case("test_c.sh"))
        self.assertEqual(len(found), 1)
        self.assertNotIn("\n", found[0])
        self.assertIn("test_a.sh exited 1", found[0])

    def test_a_strict_xpass_is_a_failure_and_is_named(self):
        found = self.problems(_case("test_a.sh", "xpass_strict"), _case("test_b.sh"), _case("test_c.sh"))
        self.assertNames(found, "test_a.sh", "failed")

    def test_a_missing_suite_is_named(self):
        found = self.problems(_case("test_a.sh"), _case("test_c.sh"))
        self.assertNames(found, "test_b.sh", "no testcase")
        self.assertEqual(len(found), 1, found)

    def test_a_failed_suite_is_named(self):
        found = self.problems(_case("test_a.sh", "fail"), _case("test_b.sh"), _case("test_c.sh"))
        self.assertNames(found, "test_a.sh", "failed")

    def test_an_errored_suite_is_named(self):
        found = self.problems(_case("test_a.sh"), _case("test_b.sh", "error"), _case("test_c.sh"))
        self.assertNames(found, "test_b.sh", "error")

    def test_an_unknown_suite_in_the_report_is_named(self):
        found = self.problems(*(_case(s) for s in SUITES), _case("test_ghost.sh"))
        self.assertNames(found, "test_ghost.sh", "no tools/")
        self.assertEqual(len(found), 1, found)

    def test_a_suite_reported_twice_is_named(self):
        found = self.problems(*(_case(s) for s in SUITES), _case("test_a.sh"))
        self.assertNames(found, "test_a.sh", "2 times")

    def test_a_report_with_no_wrapper_cases_at_all_is_a_problem(self):
        self.assertTrue(any("no test_shell_suite_passes" in p for p in self.problems()))
        other = _case("x", name="test_something_else", classname="tools.test_other")
        self.assertTrue(any("no test_shell_suite_passes" in p for p in self.problems(other)))

    def test_a_same_named_case_from_another_module_does_not_count(self):
        impostor = [_case(s, classname="tools.test_impostor") for s in SUITES]
        found = self.problems(*impostor)
        for s in SUITES:
            self.assertNames(found, s, "no testcase")

    def test_every_bad_suite_is_named_in_one_pass(self):
        found = self.problems(_case("test_a.sh", "skip"), _case("test_b.sh", "xfail"), _case("test_c.sh", "fail"),
                              _case("test_ghost.sh"))
        for suite in ("test_a.sh", "test_b.sh", "test_c.sh", "test_ghost.sh"):
            self.assertNames(found, suite, suite)

    def test_a_passing_case_may_carry_other_properties_and_captured_output(self):
        noisy = ('<testcase classname="tools.test_shell_suites" name="test_shell_suite_passes[test_a.sh]" time="1">'
                 + _props().replace("</properties>", '<property name="k" value="v"/></properties>')
                 + '<system-out>hi</system-out></testcase>')
        self.assertEqual(self.problems(noisy, _case("test_b.sh"), _case("test_c.sh")), [])

    def test_a_passed_case_that_recorded_no_returncode_is_never_ran(self):
        """R-4a: an early `return` keyed on the suite name reports a pass without running the
        suite, so nothing was recorded."""
        found = self.problems(_case("test_a.sh"), _case("test_b.sh", props=""), _case("test_c.sh"))
        self.assertNames(found, "test_b.sh", "suite_returncode")
        self.assertNames(found, "test_b.sh", "never ran")
        self.assertEqual(len(found), 1, found)

    def test_a_passed_case_with_a_nonzero_returncode_fails_whatever_the_wrapper_judged(self):
        """R-4b: the wrapper's pass criterion was weakened; the suite really exited non-zero."""
        for rc in (1, 2, 137, -9):
            with self.subTest(rc=rc):
                found = self.problems(_case("test_a.sh"), _case("test_b.sh", props=_props(rc=rc)), _case("test_c.sh"))
                self.assertNames(found, "test_b.sh", f"suite_returncode={rc}")
                self.assertEqual(len(found), 1, found)

    def test_a_returncode_of_zero_is_clean(self):
        self.assertEqual(self.problems(*(_case(s, props=_props(rc=0)) for s in SUITES)), [])

    def test_a_watchdog_killed_suite_fails_even_if_the_wrapper_passed_it(self):
        for rc, killed in ((None, True), (0, True)):
            with self.subTest(rc=rc, killed=killed):
                found = self.problems(_case("test_a.sh", props=_props(rc=rc, killed=killed)),
                                      _case("test_b.sh"), _case("test_c.sh"))
                self.assertNames(found, "test_a.sh", "killed")

    def test_a_missing_killed_flag_is_never_ran_too(self):
        no_killed = '<properties><property name="suite_returncode" value="0" /></properties>'
        found = self.problems(_case("test_a.sh", props=no_killed), _case("test_b.sh"), _case("test_c.sh"))
        self.assertNames(found, "test_a.sh", "suite_killed")

    def test_an_unreadable_returncode_is_a_problem(self):
        for value in ("", "abc", "0.5", "None"):
            with self.subTest(value=value):
                found = self.problems(_case("test_a.sh", props=_props(rc=value)), _case("test_b.sh"), _case("test_c.sh"))
                self.assertNames(found, "test_a.sh", "suite_returncode")

    def test_a_returncode_recorded_twice_is_a_problem_not_first_wins(self):
        twice = _props(rc=0).replace("</properties>", '<property name="suite_returncode" value="1" /></properties>')
        found = self.problems(_case("test_a.sh", props=twice), _case("test_b.sh"), _case("test_c.sh"))
        self.assertNames(found, "test_a.sh", "2 times")

    def test_properties_on_the_wrong_testcase_do_not_count(self):
        """Suite A's case is bare; suite B's case carries a (second) block. A stays flagged, and
        properties at testsuite level or on another module's case satisfy nobody."""
        found = self.problems(_case("test_a.sh", props=""), _case("test_b.sh", props=_props() + _props()),
                              _case("test_c.sh"))
        self.assertNames(found, "test_a.sh", "suite_returncode")
        suite_level = ('<?xml version="1.0"?><testsuites><testsuite name="pytest">' + _props()
                       + "".join(_case(s, props="") for s in SUITES) + "</testsuite></testsuites>")
        found = chk.check(self.write(suite_level), SUITES)
        for s in SUITES:
            self.assertNames(found, s, "suite_returncode")
        impostor = _case("test_a.sh", classname="tools.test_impostor")
        found = self.problems(impostor, _case("test_b.sh"), _case("test_c.sh"))
        self.assertNames(found, "test_a.sh", "no testcase")

    def test_a_skipped_case_is_reported_by_its_outcome_not_its_properties(self):
        found = self.problems(_case("test_a.sh", "skip", props=_props()), _case("test_b.sh"), _case("test_c.sh"))
        self.assertNames(found, "test_a.sh", "skipped")
        self.assertEqual(len(found), 1, found)

    def test_the_captured_real_pytest_report_is_understood(self):
        suites = ["test_pass.sh", "test_skip.sh", "test_xfail.sh", "test_xpass_strict.sh",
                  "test_xpass_loose.sh", "test_fail.sh", "test_error.sh", "test_markskip.sh",
                  "test_norec.sh"]
        found = chk.check(self.write(_REAL_CAPTURE), suites)
        for bad in ("test_skip.sh", "test_xfail.sh", "test_xpass_strict.sh", "test_fail.sh", "test_error.sh",
                    "test_markskip.sh", "test_norec.sh"):
            self.assertNames(found, bad, bad)
        # A non-strict XPASS is a passed case with rc 0 recorded, i.e. indistinguishable from a
        # pass (documented limit); the plain pass is of course clean.
        self.assertFalse([p for p in found if "test_pass.sh" in p or "test_xpass_loose.sh" in p], found)
        self.assertEqual(len(found), 7, found)

    def test_a_report_that_is_not_xml_raises_a_usage_error(self):
        for text in ("", "not xml at all", "<testsuites><testsuite>"):
            with self.subTest(text=text), self.assertRaises(chk.ReportError):
                chk.check(self.write(text), SUITES)

    def test_a_missing_report_file_raises_a_usage_error(self):
        with self.assertRaises(chk.ReportError):
            chk.check(self.dir / "nope.xml", SUITES)


class MainTests(_Tmp):
    def setUp(self):
        super().setUp()
        self.tools = self.dir / "tools"
        self.tools.mkdir()
        for s in ("test_a.sh", "test_b.sh"):
            (self.tools / s).write_text("#!/bin/bash\n")
        (self.tools / "helper.sh").write_text("")  # not a suite

    def run_main(self, report_text: str) -> tuple[int, str, str]:
        path = self.write(report_text)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chk.main([str(path), "--tools-dir", str(self.tools)])
        return rc, out.getvalue(), err.getvalue()

    def test_all_passed_exits_zero_and_says_how_many(self):
        rc, out, err = self.run_main(_report(_case("test_a.sh"), _case("test_b.sh")))
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("2 of 2", out)

    def test_a_non_passing_suite_exits_one_and_is_named_on_stderr(self):
        for shape in ("skip", "xfail", "fail", "error"):
            with self.subTest(shape=shape):
                rc, _out, err = self.run_main(_report(_case("test_a.sh"), _case("test_b.sh", shape)))
                self.assertEqual(rc, 1)
                self.assertIn("test_b.sh", err)
                self.assertNotIn("test_a.sh", err)

    def test_a_missing_suite_exits_one(self):
        rc, _out, err = self.run_main(_report(_case("test_a.sh")))
        self.assertEqual(rc, 1)
        self.assertIn("test_b.sh", err)

    def test_an_unknown_suite_exits_one(self):
        rc, _out, err = self.run_main(_report(_case("test_a.sh"), _case("test_b.sh"), _case("test_ghost.sh")))
        self.assertEqual(rc, 1)
        self.assertIn("test_ghost.sh", err)

    def test_an_empty_report_exits_nonzero(self):
        rc, _out, err = self.run_main(_report())
        self.assertNotEqual(rc, 0)
        self.assertIn("no test_shell_suite_passes", err)

    def test_an_unparseable_or_absent_report_exits_two(self):
        for text in ("", "garbage", "<testsuites>"):
            with self.subTest(text=text):
                rc, _out, err = self.run_main(text)
                self.assertEqual(rc, 2)
                self.assertIn("cannot", err)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = chk.main([str(self.dir / "absent.xml"), "--tools-dir", str(self.tools)])
        self.assertEqual(rc, 2)

    def test_no_suites_on_disk_is_refused_not_a_clean_sweep(self):
        for f in self.tools.glob("test_*.sh"):
            f.unlink()
        rc, _out, err = self.run_main(_report(_case("test_a.sh")))
        self.assertEqual(rc, 2)
        self.assertIn("refusing", err)

    def test_the_script_itself_runs_as_a_command(self):
        """The CI step runs `python tools/check_shell_suite_outcomes.py <report>`; exercise that
        entry point, not just the function."""
        path = self.write(_report(_case("test_a.sh"), _case("test_b.sh", "skip")))
        proc = subprocess.run(
            [sys.executable, str(TOOLS / "check_shell_suite_outcomes.py"), str(path), "--tools-dir", str(self.tools)],
            capture_output=True, text=True, cwd=str(self.dir))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("test_b.sh", proc.stderr)


class LiveTreeTests(unittest.TestCase):
    def test_discovery_matches_an_independent_enumeration_of_tools(self):
        independent = sorted(e.name for e in os.scandir(TOOLS)
                             if e.is_file() and e.name.startswith("test_") and e.name.endswith(".sh"))
        self.assertTrue(independent, "no tools/test_*.sh at all")
        self.assertEqual(chk.discover(TOOLS), independent)


if __name__ == "__main__":
    unittest.main()
