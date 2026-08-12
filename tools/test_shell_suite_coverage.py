"""test_shell_suite_coverage.py — the shell suites must stay EXECUTED (T-2330).

This file does not run the shell suites; `tools/run_shell_suites.sh` does, from CI.
What it guards is that they are still WIRED — because the defect being fixed was
never "a suite is red", it was "78 cases are executed by nothing and nobody can
tell". That state is invisible from inside a green Python sweep, which is exactly
why it survived: test_handoff_and_restart.sh sat at 4/33 through a commit and
several full-suite runs (T-2319), and test_verify_startup.sh went 39 days unrun
while passing on exactly one account (T-2322).

So the checks here are cheap, always collected, and structural:

  1. the runner discovers by GLOB, not by a hardcoded list — otherwise a NEW
     shell suite gets added, not listed, and goes unwatched exactly as these did;
  2. the runner refuses to report success on an empty discovery — a glob that
     matches nothing otherwise looks identical to a clean sweep;
  3. CI actually invokes the runner — a runner nothing calls is the original
     defect wearing a different name.

Presence is not activation. That is the whole lesson of T-2261/T-2312/T-2319/
T-2322/T-2324/T-2320, and this file is the smallest version of it that can fail.
"""
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
RUNNER = REPO / "tools" / "run_shell_suites.sh"
CI = REPO / ".github" / "workflows" / "ci.yml"


class ShellSuitesStayWired(unittest.TestCase):
    def setUp(self):
        self.assertTrue(RUNNER.is_file(), f"{RUNNER} is missing — the shell suites "
                                          "have nothing running them again")
        self.runner = RUNNER.read_text(encoding="utf-8")

    def test_there_are_shell_suites_to_run(self):
        """If this ever fails, the premise changed — do not delete the runner
        without checking whether the suites were moved rather than removed."""
        found = sorted(p.name for p in (REPO / "tools").glob("test_*.sh"))
        self.assertTrue(found, "no tools/test_*.sh found at all")

    def test_runner_discovers_by_glob_not_by_a_hardcoded_list(self):
        """A list is the same defect one level up: the next suite is added, not
        listed, and is unwatched from birth."""
        # ⚠️ Pin the ASSIGNMENT, not the string anywhere in the file. The first
        # version of this assertion matched `tools/test_*.sh` anywhere, so it was
        # satisfied by the explanatory COMMENT at the top — mutation testing
        # showed it passing while `suites=` had been swapped to a hardcoded list.
        # An assertion satisfiable by something other than the property it names
        # is not a guard; it is the defect this whole cluster is about, in the
        # file written to prevent it.
        self.assertRegex(
            self.runner, re.compile(r"^\s*suites=\(tools/test_\*\.sh\)\s*$", re.M),
            "run_shell_suites.sh must ASSIGN suites from the glob tools/test_*.sh; "
            "a hardcoded list silently excludes every suite added after it was "
            "written — which is this defect one level up")

    def test_runner_refuses_to_pass_on_an_empty_discovery(self):
        """'0 suites, all green' must never be a pass — it is what a broken
        harness reporting success looks like."""
        self.assertIn("refusing to report success", self.runner,
                      "an empty glob must exit non-zero, not report a clean sweep")

    def test_runner_is_executable(self):
        self.assertTrue(RUNNER.stat().st_mode & 0o111,
                        "run_shell_suites.sh is not executable")

    def test_ci_actually_invokes_the_runner(self):
        """The half that closes the loop. A runner nothing calls is exactly the
        state this ticket exists to end, wearing a different name."""
        self.assertTrue(CI.is_file(), f"{CI} not found")
        ci = CI.read_text(encoding="utf-8")
        self.assertIn("run_shell_suites.sh", ci,
                      "ci.yml does not invoke tools/run_shell_suites.sh — the "
                      "shell suites are unexecuted again (T-2330)")

    def test_ci_path_filter_still_covers_the_runner(self):
        """ci.yml only runs on changes under tools/** (T-1627, to stop the
        hosted matrix burning Actions minutes on ticket commits). The runner and
        the suites both live there, so this holds — but assert it, because a
        narrowed filter would silently stop running them without touching a
        single line of test code."""
        ci = CI.read_text(encoding="utf-8")
        paths = re.search(r"^\s*paths:\s*$(.*?)^\s*(?:branches|jobs|permissions):",
                          ci, re.S | re.M)
        self.assertIsNotNone(paths, "could not locate ci.yml's paths filter — "
                                    "THIS GUARD IS NOW BLIND; fix the parse, do "
                                    "not delete the assertion")
        self.assertIn("tools/**", paths.group(1),
                      "ci.yml no longer triggers on tools/**, so the shell "
                      "suites would stop running on tools changes")


if __name__ == "__main__":
    unittest.main()
