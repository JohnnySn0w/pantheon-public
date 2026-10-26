"""Tests for tools/check_dbus_tier_outcomes.py and the dbus-tiers job wiring in ci.yml (T-4931)."""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import yaml

from tools import check_dbus_tier_outcomes as chk

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
CI_YML = WORKFLOWS / "ci.yml"
DBUS_YML = WORKFLOWS / "dbus-tiers.yml"


def _report(cases: list[tuple[str, str, str | None]]) -> str:
    """cases: (classname, name, None | 'skip:<reason>' | 'fail' | 'error' | 'xfail')."""
    out = ['<?xml version="1.0"?><testsuites><testsuite name="pytest">']
    for cls, name, how in cases:
        out.append(f'<testcase classname="{cls}" name="{name}">')
        if how and how.startswith("skip:"):
            out.append(f'<skipped type="pytest.skip" message="{how[5:]}">x</skipped>')
        elif how == "xfail":
            out.append('<skipped type="pytest.xfail" message="xfail">x</skipped>')
        elif how == "fail":
            out.append('<failure message="boom">x</failure>')
        elif how == "error":
            out.append('<error message="boom">x</error>')
        out.append("</testcase>")
    out.append("</testsuite></testsuites>")
    return "".join(out)


class CheckTests(unittest.TestCase):
    def _run(self, cases, *args) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "r.xml"
            p.write_text(_report(cases), encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = chk.main([str(p), *args])
        return code, out.getvalue(), err.getvalue()

    GOOD = [("tools.test_a.A", "t1", None), ("tools.test_a.A", "t2", None), ("tools.test_b.B", "t3", None)]

    def test_all_executed_passes(self):
        code, out, _ = self._run(self.GOOD, "--min-executed", "3", "--require-module", "test_b")
        self.assertEqual(code, 0, out)
        self.assertIn("3 executed, 0 skipped, 0 failed", out)

    def test_any_skip_fails_by_default(self):
        cases = self.GOOD + [("tools.test_a.A", "t4", "skip:private-bus test needs dbus-python")]
        code, _, err = self._run(cases, "--min-executed", "3")
        self.assertEqual(code, 1)
        self.assertIn("must not skip", err)
        self.assertIn("needs dbus-python", err)

    def test_xfail_counts_as_a_skip(self):
        code, _, err = self._run(self.GOOD + [("tools.test_a.A", "t4", "xfail")], "--min-executed", "3")
        self.assertEqual(code, 1)
        self.assertIn("xfail", err)

    def test_allow_skip_tolerates_only_matching_reasons(self):
        cases = self.GOOD + [("tools.test_a.A", "t4", "skip:needs root"), ("tools.test_a.A", "t5", "skip:needs dbus")]
        code, _, err = self._run(cases, "--min-executed", "3", "--allow-skip", "needs root")
        self.assertEqual(code, 1)
        self.assertIn("t5", err)
        self.assertNotIn("t4", err)

    def test_failure_and_error_fail(self):
        for how in ("fail", "error"):
            with self.subTest(how=how):
                code, _, err = self._run(self.GOOD + [("tools.test_a.A", "t4", how)], "--min-executed", "3")
                self.assertEqual(code, 1)
                self.assertIn("t4", err)

    def test_floor_catches_narrowed_collection(self):
        code, _, err = self._run(self.GOOD, "--min-executed", "4")
        self.assertEqual(code, 1)
        self.assertIn("floor is 4", err)

    def test_required_module_with_nothing_executed_fails(self):
        code, _, err = self._run(self.GOOD, "--min-executed", "3", "--require-module", "test_missing")
        self.assertEqual(code, 1)
        self.assertIn("test_missing", err)

    def test_required_module_fully_skipped_fails(self):
        cases = self.GOOD + [("tools.test_c.C", "t9", "skip:x")]
        code, _, err = self._run(cases, "--min-executed", "3", "--require-module", "test_c", "--allow-skip", ".")
        self.assertEqual(code, 1)
        self.assertIn("test_c", err)

    def test_unreadable_or_empty_report_is_exit_2(self):
        with tempfile.TemporaryDirectory() as d:
            for text in ("not xml", _report([])):
                p = Path(d) / "r.xml"
                p.write_text(text, encoding="utf-8")
                err = io.StringIO()
                with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(chk.main([str(p), "--min-executed", "1"]), 2)
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(chk.main([str(Path(d) / "absent.xml"), "--min-executed", "1"]), 2)


class UnionTests(unittest.TestCase):
    """Several reports (T-4858): a skip is forgiven only where the same id executed elsewhere."""

    def _run(self, reports, *args) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for i, cases in enumerate(reports):
                p = Path(d) / f"r{i}.xml"
                p.write_text(_report(cases), encoding="utf-8")
                paths.append(str(p))
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = chk.main([*paths, *args])
        return code, out.getvalue(), err.getvalue()

    RT = [("tools.test_a.Sdk", "t1", None), ("tools.test_a.Par", "sig", "skip:needs dbus-python")]
    SYS = [("tools.test_a.Sdk", "t1", "skip:needs mcp 2.x"), ("tools.test_a.Par", "sig", None)]

    def test_complementary_skips_pass(self):
        code, out, err = self._run([self.RT, self.SYS], "--min-executed", "2", "--require-module", "test_a")
        self.assertEqual(code, 0, err)
        self.assertIn("2 distinct executed", out)

    def test_an_id_skipped_in_every_report_fails(self):
        rt = self.RT + [("tools.test_a.Bus", "b1", "skip:needs dbus-daemon")]
        sys_ = self.SYS + [("tools.test_a.Bus", "b1", "skip:needs mcp 2.x")]
        code, _, err = self._run([rt, sys_], "--min-executed", "2")
        self.assertEqual(code, 1)
        self.assertIn("tools.test_a.Bus::b1: executed in no report", err)
        self.assertIn("needs dbus-daemon", err)

    def test_a_failure_in_either_report_fails_even_if_it_passed_elsewhere(self):
        sys_ = [("tools.test_a.Sdk", "t1", "fail"), ("tools.test_a.Par", "sig", None)]
        code, _, err = self._run([self.RT, sys_], "--min-executed", "2")
        self.assertEqual(code, 1)
        self.assertIn("t1: failed", err)

    def test_zero_run_runtime_report_fails(self):
        # The mcp-2 venv collected its tests but ran none: every runtime id executed nowhere.
        rt = [("tools.test_a.Sdk", "t1", "skip:no runtime"), ("tools.test_a.Par", "sig", "skip:x")]
        code, _, err = self._run([rt, self.SYS], "--min-executed", "2")
        self.assertEqual(code, 1)
        self.assertIn("t1: executed in no report", err)
        self.assertIn("floor is 2", err)

    def test_union_floor_counts_distinct_ids(self):
        same = [("tools.test_a.Sdk", "t1", None)]
        code, _, err = self._run([same, same], "--min-executed", "2")
        self.assertEqual(code, 1)
        self.assertIn("only 1 distinct", err)

    def test_union_required_module_missing_fails(self):
        code, _, err = self._run([self.RT, self.SYS], "--min-executed", "2", "--require-module", "test_gone")
        self.assertEqual(code, 1)
        self.assertIn("test_gone", err)

    def test_an_empty_report_in_a_union_is_exit_2(self):
        code, _, _ = self._run([self.RT, []], "--min-executed", "1")
        self.assertEqual(code, 2)


class CiWiringTests(unittest.TestCase):
    """The dbus-tiers workflow must keep the gate that makes it mean something."""

    @classmethod
    def setUpClass(cls):
        cls.doc = yaml.safe_load(DBUS_YML.read_text(encoding="utf-8"))
        cls.job = cls.doc["jobs"]["dbus-tiers"]
        cls.runs = [str(s.get("run", "")) for s in cls.job["steps"]]

    def test_pytest_step_is_serial_system_python_with_junit(self):
        (pytest_run,) = [r for r in self.runs if "-m pytest" in r]
        self.assertNotIn(" -n ", pytest_run)  # RequesterOnlyUidBusTests breaks under xdist
        self.assertIn("dbus-venv/bin/python", pytest_run)
        self.assertIn("--junitxml=", pytest_run)
        self.assertIn("tools/test_host_control_*.py", pytest_run)
        self.assertNotIn("test_host_mcp_adapter", pytest_run)

    def test_gate_step_reads_the_same_report_and_comes_after_pytest(self):
        idx = {i: r for i, r in enumerate(self.runs)}
        pyt = next(i for i, r in idx.items() if "-m pytest" in r)
        gate = next(i for i, r in idx.items() if "check_dbus_tier_outcomes.py" in r)
        self.assertGreater(gate, pyt)
        self.assertIn("pytest-dbus.xml", idx[pyt])
        self.assertIn("pytest-dbus.xml", idx[gate])
        self.assertNotIn("--allow-skip", idx[gate])  # a skip means the tier did not run
        for key in ("continue-on-error", "if"):
            self.assertNotIn(key, self.job)
            for step in self.job["steps"]:
                self.assertNotIn(key, step)

    def test_gate_requires_the_core_modules_and_a_floor(self):
        gate = next(r for r in self.runs if "check_dbus_tier_outcomes.py" in r)
        for module in ("test_host_control_dbus", "test_host_control_approval_dbus",
                       "test_host_control_executor_user_bus", "test_host_control_executor_e39"):
            self.assertIn(f"--require-module {module}", gate)
        self.assertRegex(gate, r"--min-executed [1-9][0-9]{2,}")

    def test_required_modules_exist_and_are_gated_on_dbus(self):
        gate = next(r for r in self.runs if "check_dbus_tier_outcomes.py" in r)
        for module in set(__import__("re").findall(r"--require-module (\S+)", gate)):
            self.assertTrue((Path(__file__).parent / f"{module}.py").is_file(), module)

    def test_job_lives_in_its_own_workflow_not_in_ci_yml(self):
        # Review round 1: a permanently red job inside ci.yml would redden the ci.yml run
        # conclusion that ci_nightly_watch and the merge scripts key on.
        ci = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
        self.assertNotIn("dbus-tiers", ci["jobs"])
        self.assertEqual(list(self.doc["jobs"]), ["dbus-tiers"])
        self.assertNotEqual(self.doc["name"], ci["name"])  # distinct github.workflow => own concurrency group
        self.assertIn("${{ github.workflow }}", self.doc["concurrency"]["group"])

    def test_triggers_and_paths_match_ci_yml_plus_own_file(self):
        ci = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
        # PyYAML parses the key `on` as boolean True.
        mine, theirs = self.doc[True], ci[True]
        self.assertEqual(set(mine), set(theirs))
        # Nightly is deliberately staggered 30 min after ci.yml's (one pve2 runner).
        self.assertEqual(mine["schedule"], [{"cron": "30 9 * * *"}])
        self.assertEqual(theirs["schedule"], [{"cron": "0 9 * * *"}])
        # T-5001: neither workflow re-runs on the merge push any more; the nightlies cover main.
        self.assertNotIn("push", mine)
        for event in ("pull_request",):
            self.assertEqual(set(mine[event]["paths"]) - {".github/workflows/dbus-tiers.yml"},
                             set(theirs[event]["paths"]))
            self.assertIn(".github/workflows/dbus-tiers.yml", mine[event]["paths"])

    def test_repack_follows_checkout(self):
        steps = self.job["steps"]
        i = next(n for n, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/checkout@"))
        self.assertEqual(steps[i + 1]["name"], "Keep the workspace repo packed")

    def test_runs_on_the_suite_runner_and_installs_nothing(self):
        self.assertEqual(self.job["runs-on"], ["self-hosted", "linux", "x64", "pve2"])
        for run in self.runs:
            self.assertNotIn("apt-get install", run)
            self.assertNotIn("su" + "do", run)  # the runner has no root

    def test_first_step_preflights_the_prerequisites_and_names_the_remedy(self):
        first = self.job["steps"][0]
        run = str(first["run"])
        self.assertNotIn("uses", first)
        for needle in ("import dbus", "import gi", "command -v dbus-daemon", "exit 1",
                       "provision CT202: apt install python3-dbus python3-gi dbus python3-venv (ticket to recovery)"):
            self.assertIn(needle, run)
        for key in ("continue-on-error", "if"):
            self.assertNotIn(key, first)


if __name__ == "__main__":
    unittest.main()
