#!/usr/bin/env python3
"""Tests for tools/soak_leg.py (T-3393), the per-account suite-health soak leg runner.

Every subprocess (venv creation, pip, pytest, the shell-outcomes check) goes through an
injected fake runner: no test creates a real venv, installs anything, or runs the suite.
All paths are TemporaryDirectory dirs.
"""
from __future__ import annotations

import io
import signal
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from tools import soak_leg

REPO = Path(__file__).resolve().parent.parent
CI_TEXT = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

PINS = ("pyyaml==6.0.3", "pytest==9.1.1", "pytest-xdist==3.8.0")

_CASE_PASS = '<testcase classname="tools.test_a.A" name="test_ok" time="0.1"/>'
_CASE_SKIP = ('<testcase classname="tools.test_a.A" name="test_skip" time="0">'
              '<skipped message="no"/></testcase>')
_CASE_FAIL = ('<testcase classname="tools.test_b.B" name="test_bad" time="0.1">'
              '<failure message="boom">x</failure></testcase>')
_CASE_ERROR = ('<testcase classname="tools.test_c.C" name="test_err" time="0.1">'
               '<error message="setup">x</error></testcase>')


def _junit(*cases: str) -> str:
    return ('<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
            + "".join(cases) + "</testsuite></testsuites>")


def _workflow(run_line: str) -> str:
    return ("jobs:\n  test:\n    steps:\n"
            "      - name: Install test dependencies\n"
            "        # comment\n"
            f"        run: {run_line}\n\n"
            "      - name: Run tools test suite\n"
            "        run: python -m pytest tools/ -q\n")


class FakeRunner:
    """Records each argv; returns per-call rc by first-matching argv substring."""

    def __init__(self, rcs: dict[str, int] | None = None, on_call=None,
                 stdouts: dict[str, str] | None = None):
        self.calls: list[tuple[list[str], dict]] = []
        self.rcs = rcs or {}
        self.on_call = on_call
        self.stdouts = stdouts or {}

    def __call__(self, argv, **kw):
        argv = list(argv)
        self.calls.append((argv, kw))
        if self.on_call:
            self.on_call(argv, kw)
        rc = 0
        joined = " ".join(argv)
        for key, value in self.rcs.items():
            if key in joined:
                rc = value
                break
        stdout = next((v for k, v in self.stdouts.items() if k in joined), "")
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="err" if rc else "")

    def argvs(self) -> list[list[str]]:
        return [a for a, _ in self.calls]


class PinParsingTests(unittest.TestCase):
    def test_real_ci_workflow_parses_to_the_current_pins(self):
        pins = soak_leg.parse_ci_pins(CI_TEXT)
        self.assertEqual(pins, ("pyyaml==6.0.3", "pytest==9.1.1", "pytest-xdist==3.8.0"))
        self.assertEqual(soak_leg.load_ci_pins(), pins)

    def test_missing_step_is_an_error(self):
        with self.assertRaisesRegex(soak_leg.SetupError, "no 'Install test dependencies' step"):
            soak_leg.parse_ci_pins("jobs: {}\n")

    def test_step_without_run_line_is_an_error(self):
        text = ("      - name: Install test dependencies\n        uses: x\n"
                "      - name: Next\n        run: pip install pytest==1.0\n")
        with self.assertRaisesRegex(soak_leg.SetupError, "no run line"):
            soak_leg.parse_ci_pins(text)

    def test_changed_pins_line_is_followed(self):
        text = _workflow("python -m pip install --upgrade pip pyyaml==7.0 pytest==10.2.0 "
                         "pytest-xdist==4.0.1 hypothesis[cli]==6.1")
        self.assertEqual(soak_leg.parse_ci_pins(text),
                         ("pyyaml==7.0", "pytest==10.2.0", "pytest-xdist==4.0.1", "hypothesis[cli]==6.1"))

    def test_short_upgrade_flag_and_bare_pip_install(self):
        self.assertEqual(soak_leg.parse_ci_pins(_workflow("pip install -U pip pytest==9.1.1")),
                         ("pytest==9.1.1",))

    def test_every_unrecognised_install_token_is_refused_by_name(self):
        base = "python -m pip install --upgrade pip pytest==9.1.1 "
        for bad in ("pyyaml", "-r", "pyyaml>=6.0", '"pyyaml==6.0.3"', "'pyyaml==6.0.3'", "&&",
                    "${EXTRA}", "pyyaml==${V}", "--index-url", "pyyaml~=6.0", "#"):
            with self.subTest(bad=bad), self.assertRaisesRegex(soak_leg.SetupError, "not a `pkg==ver` pin") as cm:
                soak_leg.parse_ci_pins(_workflow(base + bad + " pytest-xdist==3.8.0"))
            self.assertIn(repr(bad), str(cm.exception))

    def test_requirements_file_is_refused(self):
        with self.assertRaisesRegex(soak_leg.SetupError, "'-r'"):
            soak_leg.parse_ci_pins(_workflow("pip install -r requirements.txt"))

    def test_unpinned_only_is_refused(self):
        with self.assertRaisesRegex(soak_leg.SetupError, "'pytest'"):
            soak_leg.parse_ci_pins(_workflow("python -m pip install --upgrade pip pytest pyyaml"))

    def test_no_pins_at_all_is_refused(self):
        with self.assertRaisesRegex(soak_leg.SetupError, "pins nothing"):
            soak_leg.parse_ci_pins(_workflow("python -m pip install --upgrade pip"))

    def test_pytest_pin_is_required(self):
        with self.assertRaisesRegex(soak_leg.SetupError, "no pytest== pin"):
            soak_leg.parse_ci_pins(_workflow("pip install pyyaml==6.0.3 pytest-xdist==3.8.0"))

    def test_a_shell_prefix_or_non_install_command_is_refused(self):
        for line in ("cd x && pip install pytest==9.1.1", "echo hi", "uv pip install pytest==9.1.1",
                     "| \n"):
            with self.subTest(line=line), self.assertRaises(soak_leg.SetupError):
                soak_leg.parse_ci_pins(_workflow(line))

    def test_unreadable_workflow_is_a_setup_error(self):
        with TemporaryDirectory() as d:
            with self.assertRaises(soak_leg.SetupError):
                soak_leg.load_ci_pins(Path(d) / "missing.yml")


PY = soak_leg.python_tag()


def _marker(pins=PINS, py=PY) -> str:
    return f"python={py}\n" + "\n".join(pins) + "\n"


class VenvTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.venv = Path(self._tmp.name) / "cache" / "soak-venv"

    def _make_venv(self, version=None, owned=True):
        py = soak_leg.venv_python(self.venv)
        py.parent.mkdir(parents=True, exist_ok=True)
        py.write_text("", encoding="utf-8")
        (self.venv / "pyvenv.cfg").write_text(f"home = /usr/bin\nversion = {version or PY + '.0'}\n",
                                              encoding="utf-8")
        if owned:  # as a venv an earlier soak_leg run built
            (self.venv / soak_leg.OWNED_MARKER).write_text("x", encoding="utf-8")

    def _creating_runner(self, rcs=None):
        return FakeRunner(rcs=rcs, on_call=lambda argv, kw: self._make_venv() if "venv" in argv else None)

    def test_fresh_venv_is_created_then_pinned(self):
        runner = self._creating_runner()
        self.assertEqual(soak_leg.ensure_venv(self.venv, PINS, runner=runner, base_python="py3"),
                         "created")
        create, pip = runner.argvs()
        self.assertEqual(create, ["py3", "-m", "venv", str(self.venv)])
        self.assertEqual(pip[:4], [str(soak_leg.venv_python(self.venv)), "-m", "pip", "install"])
        self.assertEqual(tuple(pip[-3:]), PINS)
        self.assertTrue(soak_leg.venv_is_current(self.venv, PINS))
        self.assertEqual((self.venv / soak_leg.PINS_MARKER).read_text(encoding="utf-8"), _marker())

    def test_a_created_venv_carries_the_ownership_marker(self):
        runner = FakeRunner(on_call=lambda argv, kw: self._make_venv(owned=False) if "venv" in argv else None)
        soak_leg.ensure_venv(self.venv, PINS, runner=runner)
        self.assertTrue((self.venv / soak_leg.OWNED_MARKER).is_file())

    def test_a_real_looking_venv_without_the_ownership_marker_is_refused(self):
        # A user's own venv passed via --venv: interpreter + pyvenv.cfg, but soak_leg never built it.
        self._make_venv(owned=False)
        (self.venv / "site").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(soak_leg.SetupError, "no .soak-owned"):
            soak_leg.ensure_venv(self.venv, PINS, runner=FakeRunner(rcs={"pip --version": 1}))
        self.assertTrue((self.venv / "site").exists())
        with self.assertRaisesRegex(soak_leg.SetupError, "no .soak-owned"):
            soak_leg.remove_venv(self.venv, self.venv)
        self.assertTrue(self.venv.is_dir())

    def test_unusable_venv_path_is_a_setup_error(self):
        with self.assertRaises(soak_leg.SetupError):
            soak_leg.ensure_venv(Path("/"), PINS, runner=FakeRunner())

    def test_marker_is_rechecked_after_the_lock_is_taken(self):
        # Another leg finishes building while this one waits on the flock: reuse, run nothing.
        runner = FakeRunner()
        with mock.patch.object(soak_leg, "venv_is_current", side_effect=[False, True]) as current:
            self.assertEqual(soak_leg.ensure_venv(self.venv, PINS, runner=runner), "reused")
        self.assertEqual(current.call_count, 2)
        self.assertEqual(runner.calls, [])

    def test_matching_marker_reuses_without_any_subprocess(self):
        self._make_venv()
        (self.venv / soak_leg.PINS_MARKER).write_text(_marker(), encoding="utf-8")
        runner = FakeRunner()
        self.assertEqual(soak_leg.ensure_venv(self.venv, PINS, runner=runner), "reused")
        self.assertEqual(runner.calls, [])

    def test_changed_pins_reinstall_into_the_existing_venv(self):
        self._make_venv()
        (self.venv / soak_leg.PINS_MARKER).write_text(_marker(("pytest==8.0",)), encoding="utf-8")
        runner = FakeRunner()
        self.assertEqual(soak_leg.ensure_venv(self.venv, PINS, runner=runner), "installed")
        check, install = runner.argvs()
        self.assertEqual(check[1:], ["-m", "pip", "--version"])
        self.assertEqual(install[1:4], ["-m", "pip", "install"])
        self.assertEqual((self.venv / soak_leg.PINS_MARKER).read_text(encoding="utf-8"), _marker())

    def test_missing_marker_reinstalls(self):
        self._make_venv()
        self.assertEqual(soak_leg.ensure_venv(self.venv, PINS, runner=FakeRunner()), "installed")

    def test_other_interpreter_minor_rebuilds_the_venv(self):
        self._make_venv(version="2.7.18")
        (self.venv / soak_leg.PINS_MARKER).write_text(_marker(py="2.7"), encoding="utf-8")
        (self.venv / "stale").write_text("x", encoding="utf-8")
        self.assertFalse(soak_leg.venv_is_current(self.venv, PINS))
        runner = self._creating_runner()
        self.assertEqual(soak_leg.ensure_venv(self.venv, PINS, runner=runner), "created")
        self.assertFalse((self.venv / "stale").exists())
        self.assertIn("venv", runner.argvs()[0])

    def test_half_built_venv_whose_pip_fails_is_rebuilt(self):
        self._make_venv()
        (self.venv / "stale").write_text("x", encoding="utf-8")
        runner = self._creating_runner(rcs={"pip --version": 1})
        self.assertEqual(soak_leg.ensure_venv(self.venv, PINS, runner=runner), "created")
        self.assertFalse((self.venv / "stale").exists())
        self.assertEqual([a[1:3] for a in runner.argvs()],
                         [["-m", "pip"], ["-m", "venv"], ["-m", "pip"]])

    def test_dir_without_an_interpreter_or_pyvenv_cfg_is_refused_not_removed(self):
        self.venv.mkdir(parents=True)
        (self.venv / "precious").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(soak_leg.SetupError, "no pyvenv.cfg"):
            soak_leg.ensure_venv(self.venv, PINS, runner=FakeRunner())
        self.assertTrue((self.venv / "precious").exists())

    def test_remove_venv_refuses_any_path_but_the_configured_one(self):
        self._make_venv()
        other = Path(self._tmp.name) / "other"
        other.mkdir()
        (other / "pyvenv.cfg").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(soak_leg.SetupError, "not the configured venv"):
            soak_leg.remove_venv(other, self.venv)
        self.assertTrue(other.exists())

    def test_remove_venv_refuses_a_symlink(self):
        self._make_venv()
        link = Path(self._tmp.name) / "link"
        link.symlink_to(self.venv)
        with self.assertRaisesRegex(soak_leg.SetupError, "not the configured venv"):
            soak_leg.remove_venv(link, link)
        self.assertTrue(self.venv.exists())

    def test_pip_failure_raises_and_leaves_no_marker(self):
        self._make_venv()
        runner = FakeRunner(rcs={"pip install": 1})
        with self.assertRaisesRegex(soak_leg.SetupError, "network"):
            soak_leg.ensure_venv(self.venv, PINS, runner=runner)
        self.assertFalse((self.venv / soak_leg.PINS_MARKER).exists())

    def test_venv_creation_failure_names_python3_venv(self):
        runner = FakeRunner(rcs={"-m venv": 1})
        with self.assertRaisesRegex(soak_leg.SetupError, "python3-venv"):
            soak_leg.ensure_venv(self.venv, PINS, runner=runner)

    def test_create_and_install_hold_the_venv_lock(self):
        import fcntl
        held = []

        def probe(argv, kw):
            with open(soak_leg.lock_path(self.venv), "a", encoding="utf-8") as fh:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held.append(True)
                else:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                    held.append(False)
            if "venv" in argv:
                self._make_venv()

        soak_leg.ensure_venv(self.venv, PINS, runner=FakeRunner(on_call=probe))
        self.assertEqual(held, [True, True])
        self.assertEqual(soak_leg.lock_path(self.venv), self.venv.parent / "soak-venv.lock")


class PytestArgvTests(unittest.TestCase):
    def test_argv_shape(self):
        out = Path("/x/out")
        argv = soak_leg.build_pytest_argv(Path("/v/bin/python"), out, 2)
        self.assertEqual(argv[:6], ["nice", "-n", "10", "/v/bin/python", "-m", "pytest"])
        self.assertEqual(argv[6], "tools/")
        self.assertIn("-q", argv)
        self.assertEqual(argv[argv.index("-n", 3) + 1], "2")
        self.assertIn("faulthandler_timeout=600", argv)
        self.assertIn("junit_family=xunit1", argv)
        self.assertIn("--junitxml=/x/out/pytest-tools.xml", argv)
        self.assertIn("--basetemp=/x/out/pytest-basetemp", argv)

    def test_paths_override(self):
        argv = soak_leg.build_pytest_argv(Path("/p"), Path("/o"), 3, ("tools/a.py", "tools/oom/"))
        self.assertEqual(argv[6:8], ["tools/a.py", "tools/oom/"])
        self.assertNotIn("tools/", argv)

    def test_ci_options_still_match_the_workflow_pytest_step(self):
        run_line = next(line for line in CI_TEXT.splitlines()
                        if "python -m pytest tools/" in line and "run:" in line)
        for opt in soak_leg.CI_PYTEST_OPTIONS:
            self.assertIn(opt, run_line)
        self.assertIn(" -q ", run_line)

    def test_env_sets_the_telegram_flag(self):
        env = soak_leg.build_pytest_env({"PATH": "/bin"})
        self.assertEqual(env["PANTHEON_DISABLE_LIVE_TELEGRAM"], "1")
        self.assertEqual(env["PATH"], "/bin")

    def test_env_strips_seat_only_variables(self):
        seat = {name: "x" for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTHONPATH",
                                       "PYTHONHOME", "VIRTUAL_ENV", "TMUX")}
        seat.update(HOME="/h", PANTHEON_DISABLE_LIVE_TELEGRAM="0")
        env = soak_leg.build_pytest_env(seat)
        for name in soak_leg.STRIPPED_ENV:
            self.assertNotIn(name, env)
        self.assertEqual(env["HOME"], "/h")
        self.assertEqual(env["PANTHEON_DISABLE_LIVE_TELEGRAM"], "1")

    def test_env_never_carries_a_repo_redirect_variable(self):
        # r2 HIGH: a GIT_CONFIG_* injection made pantheon_config_git's sandbox guard refuse
        # (6 failed + 57 errors). The tuple is imported, not copied, so an addition is caught.
        from tools.germinate.components.pantheon_config_git import REPO_REDIRECT_ENV
        for base in ({"PATH": "/bin"}, None):
            env = soak_leg.build_pytest_env(base)
            self.assertEqual(sorted(set(env) & set(REPO_REDIRECT_ENV)), [])
        self.assertNotIn("GIT_CONFIG_KEY_0", soak_leg.build_pytest_env({}))
        # A seat env that sets every one of them (CI sets none): all stripped, the rest kept.
        seeded = {name: "/seat/value" for name in REPO_REDIRECT_ENV}
        seeded.update(HOME="/h", GIT_AUTHOR_NAME="seat")
        env = soak_leg.build_pytest_env(seeded)
        self.assertEqual(sorted(set(env) & set(REPO_REDIRECT_ENV)), [])
        self.assertEqual((env["HOME"], env["GIT_AUTHOR_NAME"]), ("/h", "seat"))

    def test_redirect_set_covers_the_handler_list(self):
        # soak_leg asks git (it may not import the handler); the handler's list must stay a
        # subset, so an addition there that git does not report is caught here.
        from tools.germinate.components.pantheon_config_git import REPO_REDIRECT_ENV
        names = soak_leg.repo_redirect_env()
        self.assertTrue(set(REPO_REDIRECT_ENV) <= names, sorted(set(REPO_REDIRECT_ENV) - names))

    def test_redirect_set_failure_is_a_setup_error(self):
        with self.assertRaises(soak_leg.SetupError):
            soak_leg.repo_redirect_env(runner=FakeRunner(rcs={"local-env-vars": 128}))

        def boom(argv, **kw):
            raise FileNotFoundError("git")
        with self.assertRaises(soak_leg.SetupError):
            soak_leg.repo_redirect_env(runner=boom)

    def test_env_adds_no_git_identity(self):
        env = soak_leg.build_pytest_env({})
        self.assertEqual([k for k in env if k.startswith("GIT_")], [])


class DefaultBranchTests(unittest.TestCase):
    def test_reads_the_value_read_only(self):
        runner = FakeRunner(stdouts={"init.defaultBranch": "main\n"})
        self.assertEqual(soak_leg.default_branch(runner=runner), "main")
        self.assertEqual(runner.argvs(), [list(soak_leg.DEFAULT_BRANCH_ARGV)])
        self.assertIn("--get", soak_leg.DEFAULT_BRANCH_ARGV)

    def test_unset_is_none(self):
        self.assertIsNone(soak_leg.default_branch(runner=FakeRunner(rcs={"init.defaultBranch": 1})))

    def test_missing_git_is_none(self):
        def boom(argv, **kw):
            raise FileNotFoundError("git")
        self.assertIsNone(soak_leg.default_branch(runner=boom))


    def test_run_pytest_logs_to_file_from_repo_root(self):
        with TemporaryDirectory() as d:
            runner = FakeRunner(rcs={"pytest": 1})
            rc = soak_leg.run_pytest(["pytest"], Path(d), runner=runner, repo_root=Path("/repo"))
            self.assertEqual(rc, 1)
            _, kw = runner.calls[0]
            self.assertEqual(kw["cwd"], "/repo")
            self.assertEqual(kw["env"]["PANTHEON_DISABLE_LIVE_TELEGRAM"], "1")
            self.assertEqual(kw["stdout"].name, str(Path(d) / "pytest.log"))


class EvaluateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.xml = Path(self._tmp.name) / "r.xml"

    def _eval(self, body: str | None, pytest_rc=0, shell_rc=0, subset=False, sha="?", py=None,
              defbranch="main"):
        if body is not None:
            self.xml.write_text(body, encoding="utf-8")
        return soak_leg.evaluate(self.xml, pytest_rc, shell_rc, subset=subset, sha=sha, py=py,
                                 defbranch=defbranch)

    def test_all_pass_is_green(self):
        green, lines = self._eval(_junit(_CASE_PASS))
        self.assertTrue(green)
        self.assertEqual(lines, [f"SOAK LEG: sha=? py={PY} executed=1 skipped=0 failed=0 pytest_rc=0 "
                                 "shell_outcomes_rc=0 -> GREEN"])

    def test_a_failure_is_red_and_named(self):
        green, lines = self._eval(_junit(_CASE_PASS, _CASE_FAIL), pytest_rc=1)
        self.assertFalse(green)
        self.assertIn("failed=1", lines[0])
        self.assertTrue(lines[0].endswith("-> RED"))
        self.assertIn("  FAILED tools.test_b.B::test_bad", lines)

    def test_an_error_is_red(self):
        green, lines = self._eval(_junit(_CASE_PASS, _CASE_ERROR), pytest_rc=1)
        self.assertFalse(green)
        self.assertIn("  FAILED tools.test_c.C::test_err", lines)

    def test_a_failure_with_pytest_rc_zero_is_still_red(self):
        green, _ = self._eval(_junit(_CASE_FAIL), pytest_rc=0)
        self.assertFalse(green)

    def test_skip_only_run_is_red_and_reports_the_skips(self):
        green, lines = self._eval(_junit(_CASE_SKIP))
        self.assertFalse(green)
        self.assertIn("executed=0 skipped=1 failed=0 pytest_rc=0 shell_outcomes_rc=0 -> RED", lines[0])
        self.assertIn("no test executed", lines[1])

    def test_zero_executed_with_every_rc_zero_is_red(self):
        green, lines = self._eval(_junit(_CASE_SKIP, _CASE_SKIP.replace("test_skip", "test_skip2")),
                                  pytest_rc=0, shell_rc=0)
        self.assertFalse(green)
        self.assertTrue(lines[0].endswith("-> RED"))

    def test_one_executed_beside_skips_is_green(self):
        green, lines = self._eval(_junit(_CASE_SKIP, _CASE_PASS))
        self.assertTrue(green)
        self.assertIn("executed=1 skipped=1 failed=0", lines[0])

    def test_shell_outcomes_rc_propagates(self):
        green, lines = self._eval(_junit(_CASE_PASS), shell_rc=1)
        self.assertFalse(green)
        self.assertIn("shell_outcomes_rc=1 -> RED", lines[0])

    def test_nonzero_pytest_rc_alone_is_red(self):
        green, lines = self._eval(_junit(_CASE_PASS), pytest_rc=2)
        self.assertFalse(green)
        self.assertIn("pytest_rc=2", lines[0])

    def test_missing_junit_is_red(self):
        green, lines = self._eval(None)
        self.assertFalse(green)
        self.assertIn("executed=? skipped=? failed=?", lines[0])
        self.assertIn("junit report unusable", lines[1])

    def test_summary_names_the_soaked_sha(self):
        _, lines = self._eval(_junit(_CASE_PASS), sha="abc123def456")
        self.assertTrue(lines[0].startswith(f"SOAK LEG: sha=abc123def456 py={PY} executed=1 "))

    def test_defbranch_shows_only_when_not_main_and_never_decides(self):
        _, lines = self._eval(_junit(_CASE_PASS))
        self.assertNotIn("defbranch", lines[0])
        green, lines = self._eval(_junit(_CASE_PASS), defbranch="master")
        self.assertTrue(green)
        self.assertIn(" defbranch=master ", lines[0])
        green, lines = self._eval(_junit(_CASE_PASS), defbranch=None)
        self.assertTrue(green)
        self.assertIn(" defbranch=unset ", lines[0])

    def test_summary_names_the_interpreter(self):
        _, lines = self._eval(_junit(_CASE_PASS), sha="s", py="3.11")
        self.assertIn(" py=3.11 ", lines[0])

    def test_subset_skips_the_shell_check_and_says_so(self):
        green, lines = self._eval(_junit(_CASE_PASS), shell_rc=None, subset=True)
        self.assertTrue(green)
        self.assertIn("shell_outcomes_rc=skipped scope=subset -> GREEN", lines[0])


SHA = "0123456789ab"


def _write_ci(root: Path) -> None:
    ci = root / ".github" / "workflows" / "ci.yml"
    ci.parent.mkdir(parents=True, exist_ok=True)
    ci.write_text(CI_TEXT, encoding="utf-8")


class MainTests(unittest.TestCase):
    """End to end through main() with every subprocess (git included) faked. A fake repo
    root holds a copy of ci.yml; the fake `git worktree add` materialises <out>/repo with
    one, the way a real worktree of origin/main would."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name).resolve()  # main() resolves its paths
        self.repo = root / "checkout"
        _write_ci(self.repo)
        self.venv = root / "venv"
        self.out = root / "out"
        self.wt = self.out / "repo"
        py = soak_leg.venv_python(self.venv)
        py.parent.mkdir(parents=True)
        py.write_text("", encoding="utf-8")
        (self.venv / soak_leg.PINS_MARKER).write_text(_marker(soak_leg.load_ci_pins()),
                                                      encoding="utf-8")
        # main() installs a SIGTERM handler; put the test process's own back afterwards.
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))

    def _runner(self, junit_body: str | None, rcs=None, explode_on_pytest=False):
        def on_call(argv, kw):
            if argv[:5] == ["git", "-C", str(self.repo), "worktree", "add"]:
                _write_ci(self.wt)
            if "pytest" in argv:
                if explode_on_pytest:
                    raise RuntimeError("pytest blew up")
                if junit_body is not None:
                    soak_leg.junit_path(self.out).write_text(junit_body, encoding="utf-8")
        return FakeRunner(rcs=rcs, on_call=on_call, stdouts={"local-env-vars": "GIT_DIR\nGIT_CONFIG_COUNT\n", "rev-parse": SHA + "\n",
                                    "init.defaultBranch": "main\n"})

    def _main(self, junit_body: str | None, rcs=None, extra=(), runner=None):
        runner = runner or self._runner(junit_body, rcs)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = soak_leg.main(["--venv", str(self.venv), "--out-dir", str(self.out), *extra],
                               runner=runner, repo_root=self.repo)
        return rc, stdout.getvalue(), runner

    def _git(self, runner) -> list[list[str]]:
        return [a for a in runner.argvs() if a[0] == "git"]

    # -- default: fresh detached worktree of origin/main -------------------------------
    def test_default_soaks_a_fresh_worktree_of_origin_main(self):
        rc, out, runner = self._main(_junit(_CASE_PASS))
        self.assertEqual(rc, 0, out)
        g = self._git(runner)
        self.assertEqual(g[0], ["git", "-C", str(self.repo), "fetch", "--quiet", "origin"])
        self.assertEqual(g[1], ["git", "-C", str(self.repo), "worktree", "add", "--detach",
                                str(self.wt), "origin/main"])
        self.assertEqual(g[2], ["git", "rev-parse", "--local-env-vars"])
        self.assertEqual(g[3], list(soak_leg.DEFAULT_BRANCH_ARGV))
        self.assertEqual(g[4], ["git", "-C", str(self.wt), "rev-parse", "--short=12", "HEAD"])
        self.assertEqual(g[-1], ["git", "-C", str(self.repo), "worktree", "remove", "--force",
                                 str(self.wt)])
        pytest_argv, pytest_kw = next((a, k) for a, k in runner.calls if "pytest" in a and a[0] == "nice")
        self.assertEqual(pytest_kw["cwd"], str(self.wt))
        self.assertIn(f"--junitxml={soak_leg.junit_path(self.out)}", pytest_argv)
        self.assertIn(f"--basetemp={self.out / 'pytest-basetemp'}", pytest_argv)
        shell_argv, shell_kw = next((a, k) for a, k in runner.calls if a[1].endswith("check_shell_suite_outcomes.py"))
        self.assertEqual(shell_argv[1], str(self.wt / "tools" / "check_shell_suite_outcomes.py"))
        self.assertEqual(shell_kw["cwd"], str(self.wt))
        self.assertIn(f"SOAK LEG: sha={SHA} py={PY} executed=1", out)
        self.assertIn("-> GREEN", out)
        self.assertIn(f"removed worktree {self.wt}", out)

    def test_the_pytest_run_gets_the_seat_env_without_redirect_vars(self):
        with mock.patch.dict("os.environ", {"GIT_DIR": "/seat/.git", "GIT_CONFIG_COUNT": "1",
                                            "PYTHONPATH": "/x"}):
            rc, out, runner = self._main(_junit(_CASE_PASS))
        self.assertEqual(rc, 0, out)
        _, kw = next((a, k) for a, k in runner.calls if a[0] == "nice")
        for name in ("GIT_DIR", "GIT_CONFIG_COUNT", "PYTHONPATH"):
            self.assertNotIn(name, kw["env"])
        self.assertEqual(kw["env"]["PANTHEON_DISABLE_LIVE_TELEGRAM"], "1")

    def test_ref_and_no_fetch(self):
        rc, _, runner = self._main(_junit(_CASE_PASS), extra=("--ref", "abc123", "--no-fetch"))
        self.assertEqual(rc, 0)
        g = self._git(runner)
        self.assertFalse(any("fetch" in a for a in g))
        self.assertEqual(g[0][-1], "abc123")

    def test_worktree_is_removed_on_red(self):
        rc, out, runner = self._main(_junit(_CASE_FAIL), rcs={"nice": 1})
        self.assertEqual(rc, 1)
        self.assertEqual(self._git(runner)[-1][3:5], ["worktree", "remove"])
        self.assertTrue(soak_leg.junit_path(self.out).is_file())

    def test_worktree_is_removed_when_the_run_raises(self):
        runner = self._runner(None, explode_on_pytest=True)
        with self.assertRaises(RuntimeError), redirect_stdout(io.StringIO()):
            soak_leg.main(["--venv", str(self.venv), "--out-dir", str(self.out)],
                          runner=runner, repo_root=self.repo)
        self.assertEqual(self._git(runner)[-1][3:5], ["worktree", "remove"])

    def test_main_installs_the_sigterm_handler(self):
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        self._main(_junit(_CASE_PASS), extra=("--in-place",))
        self.assertIs(signal.getsignal(signal.SIGTERM), soak_leg._on_sigterm)
        with self.assertRaises(SystemExit) as cm:
            soak_leg._on_sigterm(signal.SIGTERM, None)
        self.assertEqual(cm.exception.code, 143)

    def test_sigterm_mid_run_removes_the_worktree_and_exits_143(self):
        def on_call(argv, kw):
            if argv[:5] == ["git", "-C", str(self.repo), "worktree", "add"]:
                _write_ci(self.wt)
            if argv[0] == "nice":
                soak_leg._on_sigterm(signal.SIGTERM, None)  # what the handler does on SIGTERM
        runner = FakeRunner(on_call=on_call, stdouts={"local-env-vars": "GIT_DIR\nGIT_CONFIG_COUNT\n", "rev-parse": SHA + "\n",
                                    "init.defaultBranch": "main\n"})
        with self.assertRaises(SystemExit) as cm, redirect_stdout(io.StringIO()):
            soak_leg.main(["--venv", str(self.venv), "--out-dir", str(self.out)],
                          runner=runner, repo_root=self.repo)
        self.assertEqual(cm.exception.code, 143)
        self.assertEqual(self._git(runner)[-1][3:5], ["worktree", "remove"])

    def test_worktree_is_removed_on_a_setup_error_inside_it(self):
        bad = Path(self._tmp.name) / "ci.yml"
        bad.write_text(_workflow("pip install pytest"), encoding="utf-8")
        rc, _, runner = self._main(_junit(_CASE_PASS), extra=("--ci-workflow", str(bad)))
        self.assertEqual(rc, 2)
        self.assertEqual(self._git(runner)[-1][3:5], ["worktree", "remove"])
        self.assertFalse(any(a[0] == "nice" for a in runner.argvs()))

    def test_fetch_failure_exits_two_and_adds_no_worktree(self):
        rc, _, runner = self._main(_junit(_CASE_PASS), rcs={"fetch": 1})
        self.assertEqual(rc, 2)
        self.assertEqual(len(runner.calls), 1)

    def test_worktree_add_failure_exits_two_and_removes_nothing(self):
        rc, _, runner = self._main(_junit(_CASE_PASS), rcs={"worktree add": 128})
        self.assertEqual(rc, 2)
        self.assertFalse(any("remove" in a for a in runner.argvs()))

    def test_pins_come_from_the_soaked_root(self):
        (self.repo / ".github" / "workflows" / "ci.yml").write_text(
            _workflow("pip install pytest"), encoding="utf-8")  # live checkout: no pins
        rc, out, _ = self._main(_junit(_CASE_PASS))  # the worktree's ci.yml has them
        self.assertEqual(rc, 0, out)

    # -- --in-place --------------------------------------------------------------------
    def test_in_place_runs_in_the_checkout_with_no_worktree(self):
        rc, out, runner = self._main(_junit(_CASE_PASS), extra=("--in-place",))
        self.assertEqual(rc, 0, out)
        g = self._git(runner)
        self.assertEqual(g, [["git", "rev-parse", "--local-env-vars"], list(soak_leg.DEFAULT_BRANCH_ARGV),
                             ["git", "-C", str(self.repo), "rev-parse", "--short=12", "HEAD"]])
        argvs = runner.argvs()
        self.assertEqual(len(argvs), 5)  # local-env-vars + defaultBranch + rev-parse + pytest + shell; venv reused
        _, kw = next((a, k) for a, k in runner.calls if a[0] == "nice")
        self.assertEqual(kw["cwd"], str(self.repo))
        self.assertIn("venv " + str(self.venv) + " reused", out)

    def test_basetemp_is_removed_on_green_and_kept_on_red(self):
        basetemp = self.out / "pytest-basetemp"

        def make(body):
            def on_call(argv, kw):
                if argv[0] == "nice":
                    (basetemp / "t0").mkdir(parents=True, exist_ok=True)
                    soak_leg.junit_path(self.out).write_text(body, encoding="utf-8")
            return FakeRunner(on_call=on_call, stdouts={"local-env-vars": "GIT_DIR\n"})

        argv = ["--venv", str(self.venv), "--out-dir", str(self.out), "--in-place"]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(soak_leg.main(argv, runner=make(_junit(_CASE_PASS)), repo_root=self.repo), 0)
            self.assertFalse(basetemp.exists())
            self.assertEqual(soak_leg.main(argv, runner=make(_junit(_CASE_FAIL)), repo_root=self.repo), 1)
            self.assertTrue(basetemp.is_dir())
        self.assertTrue(soak_leg.junit_path(self.out).is_file())

    def test_shell_check_failure_exits_one(self):
        rc, out, _ = self._main(_junit(_CASE_PASS), rcs={"check_shell_suite_outcomes": 1})
        self.assertEqual(rc, 1)
        self.assertIn("shell_outcomes_rc=1 -> RED", out)

    def test_missing_report_exits_one(self):
        rc, out, _ = self._main(None, rcs={"nice": 2})
        self.assertEqual(rc, 1)
        self.assertIn("-> RED", out)

    def test_paths_subset_skips_the_shell_check(self):
        rc, out, runner = self._main(_junit(_CASE_PASS), extra=("--paths", "tools/test_x.py"))
        self.assertEqual(rc, 0)
        self.assertFalse(any(a[1].endswith("check_shell_suite_outcomes.py") for a in runner.argvs()))
        self.assertIn("scope=subset", out)

    def test_venv_env_override(self):
        with mock.patch.dict("os.environ", {soak_leg.VENV_ENV: str(self.venv)}):
            runner = self._runner(_junit(_CASE_PASS))
            with redirect_stdout(io.StringIO()):
                rc = soak_leg.main(["--out-dir", str(self.out), "--in-place"], runner=runner,
                                   repo_root=self.repo)
        self.assertEqual(rc, 0)
        nice = next(a for a in runner.argvs() if a[0] == "nice")
        self.assertEqual(nice[3], str(soak_leg.venv_python(self.venv)))

    def test_non_main_default_branch_warns_but_stays_green(self):
        runner = FakeRunner(stdouts={"rev-parse": SHA + "\n", "init.defaultBranch": "master\n"},
                            on_call=lambda a, k: soak_leg.junit_path(self.out).write_text(
                                _junit(_CASE_PASS), encoding="utf-8") if a[0] == "nice" else None)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            rc = soak_leg.main(["--venv", str(self.venv), "--out-dir", str(self.out), "--in-place"],
                               runner=runner, repo_root=self.repo)
        self.assertEqual(rc, 0)
        self.assertIn("WARNING: init.defaultBranch is master", stderr.getvalue())
        self.assertIn(" defbranch=master ", stdout.getvalue())
        self.assertIn("-> GREEN", stdout.getvalue())

    def test_unusable_venv_path_exits_two(self):
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            rc = soak_leg.main(["--venv", "/", "--out-dir", str(self.out), "--in-place"],
                               runner=FakeRunner(), repo_root=self.repo)
        self.assertEqual(rc, 2)
        self.assertIn("setting up the venv", stderr.getvalue())

    def test_os_error_in_worktree_setup_exits_two(self):
        def boom(argv, **kw):
            raise FileNotFoundError("git")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = soak_leg.main(["--venv", str(self.venv), "--out-dir", str(self.out)],
                               runner=boom, repo_root=self.repo)
        self.assertEqual(rc, 2)

    def test_workers_must_be_positive(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            soak_leg.parse_args(["--workers", "0"])


if __name__ == "__main__":
    unittest.main()
