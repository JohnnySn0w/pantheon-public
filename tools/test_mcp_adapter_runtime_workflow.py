"""The mcp-adapter-runtime workflow's contract (T-4858, ORG-052; gates T-2924).

.github/workflows/mcp-adapter-runtime.yml runs the host and approver MCP adapters' mcp-2 tiers
(SDK stdio, private bus, packaging, server-observed registration parity) in a hash-locked
mcp 2.x venv, and the dbus-dependent tiers in a system-python venv, and gates on the union of
the two junit reports (tools/check_dbus_tier_outcomes.py): every testcase executed somewhere,
none failed. The rules every workflow meets (lane label, bounded timeout, repack step, the mcp
install guard) are swept elsewhere (test_workflow_runner_lanes.py, test_workflow_timeouts.py,
test_ci_integrity.py, test_harness_workflow.py). This file pins what makes THIS job mean
something, parsed rather than byte-pinned.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "mcp-adapter-runtime.yml"
LOCK = "tools/host_mcp_adapter/dist/requirements.lock"
JOB = "mcp2-tiers"
CHECKER = "tools/check_dbus_tier_outcomes.py"
RT_VENV = "$RUNNER_TEMP/mcp2-venv/bin/python"
SYS_VENV = "$RUNNER_TEMP/sys-venv/bin/python"
# The scan's markers, spelled in pieces so this file never matches its own scan.
_PROBE = "version(" + '"mcp")'
_WANTS_2 = "startswith(" + '"2.")'
# ...or it reuses an adapter module's runtime guard (T-2919's telegram-ingress leg does).
_REUSES_GUARD = re.compile(r"^from tools\.test_\w*mcp_adapter\w* import [^\n]*\b" + "runtime_skip" + r"_reason\b", re.M)
KNOBS = ("T2915_WHEELHOUSE", "T2916_WHEELHOUSE", "T2915_BUILD_PYTHON", "PANTHEON_BROKER_PYTHON",
         "PYTHONNOUSERSITE", "PANTHEON_VENV_IMPORT_AUDIT")


def _doc() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _pytest_argv(run: str) -> list[str]:
    words = shlex.split(run)
    i = words.index("pytest")
    return words[i + 1:]


def _selection(argv: list[str]) -> list[str]:
    out, skip = [], False
    for word in argv:
        if skip:
            skip = False
            continue
        if word in ("-p", "-o"):
            skip = True
            continue
        if not word.startswith("-"):
            out.append(word)
    return out


def mcp2_runtime_modules(tools_dir: Path = REPO / "tools", base: Path = REPO) -> list[str]:
    """Repo-relative test modules that gate a tier on the pinned mcp 2.x runtime: their source
    probes the installed mcp version and requires a 2.x one, or imports an adapter test module's
    runtime guard."""
    found = []
    for path in sorted(tools_dir.glob("test_*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if (_PROBE in text and _WANTS_2 in text) or _REUSES_GUARD.search(text):
            found.append(path.relative_to(base).as_posix())
    return found


class McpAdapterRuntimeWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = _doc()
        cls.job = cls.doc["jobs"][JOB]
        cls.steps = cls.job["steps"]
        cls.runs = [str(s.get("run", "")) for s in cls.steps]
        cls.pytest_steps = [s for s in cls.steps if " -m pytest " in str(s.get("run", ""))]

    def test_one_job_on_the_suite_runner_with_a_bounded_timeout(self) -> None:
        self.assertEqual(list(self.doc["jobs"]), [JOB])
        self.assertEqual(self.job["runs-on"], ["self-hosted", "linux", "x64", "pve2"])
        self.assertLessEqual(int(self.job["timeout-minutes"]), 30)
        self.assertEqual(self.doc.get("permissions"), {"contents": "read"})
        self.assertIn("${{ github.workflow }}", self.doc["concurrency"]["group"])

    def test_nothing_can_skip_or_forgive_a_step(self) -> None:
        for key in ("continue-on-error", "if"):
            self.assertNotIn(key, self.job)
            for step in self.steps:
                self.assertNotIn(key, step, step.get("name"))

    def test_triggers_are_dispatch_nightly_and_path_filtered_prs(self) -> None:
        on = self.doc.get("on", self.doc.get(True))
        self.assertEqual(set(on), {"workflow_dispatch", "schedule", "pull_request"})
        self.assertEqual(on["schedule"], [{"cron": "30 10 * * *"}])  # after ci/dbus-tiers/harness
        paths = on["pull_request"]["paths"]
        for needed in ("tools/host_mcp_adapter/**", "tools/approval_mcp_adapter/**", "tools/host_control/**",
                       "tools/harness_adapter/**", CHECKER, ".github/workflows/mcp-adapter-runtime.yml",
                       "tools/_venv_import_audit.py",
                       "tools/test_mcp_adapter_runtime_workflow.py"):
            self.assertIn(needed, paths)
        self.assertFalse([p for p in paths if p in ("**", "*", "tools/**")])
        for module in mcp2_runtime_modules():
            with self.subTest(module=module):
                self.assertTrue(any(fnmatch.fnmatch(module, p) for p in paths), module)

    def test_preflight_comes_first_prints_the_python_and_names_the_remedy(self) -> None:
        first = self.steps[0]
        self.assertNotIn("uses", first)
        run = str(first["run"])
        for needle in ("/usr/bin/python3 --version", "import dbus", "import gi", "command -v dbus-daemon",
                       "command -v dbus-monitor", "exit 1",
                       "apt install python3-dbus python3-gi dbus dbus-bin python3-venv"):
            self.assertIn(needle, run)

    def test_checkout_has_full_history_then_repacks(self) -> None:
        i = next(n for n, s in enumerate(self.steps) if str(s.get("uses", "")).startswith("actions/checkout@"))
        self.assertEqual(self.steps[i].get("with", {}).get("fetch-depth"), 0)
        self.assertEqual(self.steps[i + 1]["name"], "Keep the workspace repo packed")

    def test_setup_python_is_the_locks_target_and_a_mismatch_fails_loud(self) -> None:
        header = re.search(r"^# target: CPython (\d+\.\d+) \(cp(\d+)\)", (REPO / LOCK).read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(header)
        setup = [s for s in self.steps if str(s.get("uses", "")).startswith("actions/setup-python@")]
        self.assertEqual(len(setup), 1)
        self.assertEqual(str(setup[0]["with"]["python-version"]), header.group(1))
        check = next(r for r in self.runs if "# target: CPython" in r)
        self.assertIn(f"^# target: CPython {header.group(1)} (cp{header.group(2)})", check)
        self.assertIn("::error::", check)
        self.assertIn("sys.version_info", check)

    def test_runtime_venv_is_built_only_from_the_lock_and_proven_intact(self) -> None:
        build = next(r for r in self.runs if "mcp2-venv\"" in r and "-m venv" in r)
        # T-4858 r1/r2: always isolated. From /usr/bin/python3 plus symlinks to exactly the apt
        # dbus/gi bindings ONLY when it is the lock's 3.12 (one interpreter with mcp 2.x AND
        # dbus-python, which T-2919's leg needs); else from setup-python, with a warning.
        self.assertIn("sys.version_info[:2] == (3, 12)", build)
        self.assertIn('/usr/bin/python3 -m venv --clear "$RUNNER_TEMP/mcp2-venv"', build)
        self.assertNotIn("--system-site-packages", build)
        self.assertIn("import os, dbus, _dbus_bindings, _dbus_glib_bindings, gi;", build)
        self.assertIn('ln -s "$src" "$site/"', build)
        self.assertIn('python -m venv --clear "$RUNNER_TEMP/mcp2-venv"', build)
        self.assertIn("::warning::", build)
        self.assertIn("test_approval_mcp_adapter_telegram_ingress.py cannot run", build)
        lock_line = next(line for line in build.splitlines() if f"-r {LOCK}" in line)
        for flag in ("--ignore-installed", "--no-index", "--require-hashes", "--no-deps", "--only-binary :all:"):
            self.assertIn(flag, lock_line)
        self.assertIn("pip check", build)
        # r1 item 2: after the unhashed pytest/pyyaml install, every locked dist still resolves
        # FROM THE VENV at its locked version (an upgrade, removal or system shadow fails).
        lines = build.splitlines()
        extras = next(i for i, line in enumerate(lines) if "pytest==9.1.1" in line)
        integrity = next(i for i, line in enumerate(lines) if "locked packages not intact" in line)
        self.assertGreater(integrity, extras)
        for needle in ("startswith(sys.prefix)", "d.version != v", "m.distribution(", f"' {LOCK}"):
            self.assertIn(needle, lines[integrity])
        self.assertIn('v.startswith("2.")', build)
        self.assertIn("mcp.__file__.startswith(sys.prefix)", build)
        fetch = next(r for r in self.runs if "pip download" in r)
        for flag in ("--require-hashes", "--no-deps", "--only-binary :all:", f"-r {LOCK}", "--retries"):
            self.assertIn(flag, fetch)

    def test_system_venv_sees_the_apt_dbus_bindings(self) -> None:
        build = next(r for r in self.runs if "sys-venv\"" in r and "-m venv" in r)
        self.assertIn('/usr/bin/python3 -m venv --clear --system-site-packages "$RUNNER_TEMP/sys-venv"', build)
        self.assertIn("import dbus, gi, yaml", build)

    def test_two_serial_pytest_steps_run_the_same_selection(self) -> None:
        self.assertEqual(len(self.pytest_steps), 2)
        interpreters = [shlex.split(str(s["run"]))[0] for s in self.pytest_steps]
        self.assertEqual(interpreters, [SYS_VENV, RT_VENV])
        argvs = [_pytest_argv(str(s["run"])) for s in self.pytest_steps]
        self.assertEqual(_selection(argvs[0]), _selection(argvs[1]))
        for argv in argvs:
            for flag in ("-n", "-k", "-m", "--deselect", "-x", "--lf", "--sw"):
                self.assertNotIn(flag, argv)
            self.assertFalse([w for w in argv if w.startswith("--ignore")])
            self.assertIn("junit_family=xunit1", argv)
        reports = [next(w for w in argv if w.startswith("--junitxml=")) for argv in argvs]
        self.assertEqual(len(set(reports)), 2)

    def test_every_mcp2_runtime_module_is_selected(self) -> None:
        # T-2919's tools/test_approval_mcp_adapter_telegram_ingress.py (and any later adapter
        # module with an mcp-2 tier) is picked up by the glob with no edit here.
        selection = _selection(_pytest_argv(str(self.pytest_steps[1]["run"])))
        modules = mcp2_runtime_modules()
        self.assertIn("tools/test_approval_mcp_adapter_telegram_ingress.py", modules)
        self.assertGreaterEqual(len(modules), 4)
        for module in modules:
            with self.subTest(module=module):
                self.assertTrue(any(fnmatch.fnmatch(module, pattern) for pattern in selection), module)
        self.assertTrue(fnmatch.fnmatch("tools/test_approval_mcp_adapter_telegram_ingress.py", "tools/test_approval_mcp_adapter*.py"))
        self.assertIn("tools/test_approval_mcp_adapter*.py", selection)

    def test_the_runtime_step_sets_every_knob_and_the_system_step_none(self) -> None:
        sys_step, rt_step = self.pytest_steps
        self.assertEqual(set(rt_step.get("env", {})), set(KNOBS))
        self.assertIn("sys-venv/bin/python", rt_step["env"]["PANTHEON_BROKER_PYTHON"])
        self.assertIn("pythonLocation", rt_step["env"]["T2915_BUILD_PYTHON"])
        self.assertEqual(rt_step["env"]["T2915_WHEELHOUSE"], rt_step["env"]["T2916_WHEELHOUSE"])
        self.assertEqual(rt_step["env"]["PANTHEON_VENV_IMPORT_AUDIT"], "1")
        self.assertIn("-p tools._venv_import_audit", str(rt_step["run"]))
        self.assertEqual(sys_step.get("env"), {"PYTHONNOUSERSITE": "1"})

    def test_no_user_site_in_either_venv_build_or_pytest_step(self) -> None:
        # T-4858 r2: a --system-site-packages venv also enables ~/.local; the builds and the
        # runs must not see it, and pytest/pyyaml must be installed INTO each venv.
        builds = [s for s in self.steps if "-m venv" in str(s.get("run", ""))]
        self.assertEqual(len(builds), 2)
        for step in builds + self.pytest_steps:
            with self.subTest(step=step["name"]):
                self.assertEqual(step.get("env", {}).get("PYTHONNOUSERSITE"), "1")
        for step in builds:
            extras = next(line for line in str(step["run"]).splitlines() if "pytest==9.1.1" in line)
            self.assertIn("--ignore-installed", extras)

    def test_the_gate_reads_both_reports_after_both_runs_and_forgives_nothing(self) -> None:
        gate_index = next(i for i, r in enumerate(self.runs) if CHECKER in r)
        pytest_indices = [self.steps.index(s) for s in self.pytest_steps]
        self.assertGreater(gate_index, max(pytest_indices))
        gate = self.runs[gate_index]
        for argv in (_pytest_argv(str(s["run"])) for s in self.pytest_steps):
            report = next(w for w in argv if w.startswith("--junitxml=")).split("=", 1)[1]
            self.assertIn(report, gate)
        self.assertNotIn("--allow-skip", gate)
        floor = int(re.search(r"--min-executed (\d+)", gate).group(1))
        self.assertGreaterEqual(floor, 165)  # the measured union after T-2919 (r1 item 3)
        required = re.findall(r"--require-module (\S+)", gate)
        self.assertEqual(set(required), {"test_host_mcp_adapter", "test_approval_mcp_adapter",
                                         "test_harness_host_mcp_registration",
                                         "test_approval_mcp_adapter_telegram_ingress"})
        # every module the scan finds is required by name, not only selected
        for module in mcp2_runtime_modules():
            self.assertIn(Path(module).stem, required)
        for module in required:
            self.assertTrue((REPO / "tools" / f"{module}.py").is_file(), module)

    def test_the_tools_suites_dbus_tiers_job_does_not_also_run_these(self) -> None:
        dbus = yaml.safe_load((REPO / ".github" / "workflows" / "dbus-tiers.yml").read_text(encoding="utf-8"))
        runs = " ".join(str(s.get("run", "")) for s in dbus["jobs"]["dbus-tiers"]["steps"])
        for module in mcp2_runtime_modules():
            self.assertNotIn(module, runs)


class TelegramIngressLegNeedsNoDbus(unittest.TestCase):
    """T-4858: T-2919's adapter leg runs in the runtime venv, which has no dbus-python on a
    runner whose system python is not the lock's 3.12; its D-Bus side lives in a helper run
    under the broker python. Pin that the test module imports nothing dbus-side."""

    def test_the_test_module_imports_no_dbus_side_module(self) -> None:
        import ast

        tree = ast.parse((REPO / "tools" / "test_approval_mcp_adapter_telegram_ingress.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        forbidden = {"dbus", "gi", "_dbus_bindings", "tools.test_host_control_telegram_ingress_dbus",
                     "tools._telegram_ingress_bus_helper", "tools.host_control.telegram_ingress.dbus_service"}
        self.assertEqual({m for m in imported if m in forbidden or m.split(".", 1)[0] in {"dbus", "gi"}}, set())
        self.assertIn("tools.test_approval_mcp_adapter", imported)  # SYSTEM_PYTHON = PANTHEON_BROKER_PYTHON

    def test_the_helper_is_outside_the_dbus_tiers_glob_and_reuses_the_shared_stack(self) -> None:
        helper = REPO / "tools" / "_telegram_ingress_bus_helper.py"
        self.assertFalse(fnmatch.fnmatch(helper.name, "test_host_control_*.py"))
        text = helper.read_text(encoding="utf-8")
        self.assertIn("import tools.test_host_control_telegram_ingress_dbus as ingress_bus", text)
        self.assertIn("class _Stack(ingress_bus._IngressBusCase):", text)


class RuntimeModuleScan(unittest.TestCase):
    def test_finds_a_module_with_an_mcp2_probe_and_ignores_others(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="t4858-scan-") as tmp:
            tools = Path(tmp)
            (tools / "test_new_adapter.py").write_text(
                f"v = importlib.metadata.{_PROBE}\nok = v.{_WANTS_2}\n", encoding="utf-8")
            (tools / "test_seat_mcp.py").write_text(f"v = importlib.metadata.{_PROBE}\n", encoding="utf-8")
            (tools / "test_reuses_guard.py").write_text(
                "from tools.test_approval_mcp_adapter import " + "runtime_skip" + "_reason\n", encoding="utf-8")
            names = mcp2_runtime_modules(tools, tools)
        self.assertEqual(names, ["test_new_adapter.py", "test_reuses_guard.py"])


if __name__ == "__main__":
    unittest.main()
