#!/usr/bin/env python3
"""Tests for tools/echo-merge.sh: --no-verify-push (T-2714) + staged mode (T-2877).

Hermetic subprocess harness: `git` and `gh` are shimmed on a prepended PATH
(they log every invocation's argv to a logfile and answer just enough to walk
the script to its dry-run exit), so no real network/git/gh call is ever made.
TOKEN_ENV is env-overridable (T-2714) so this drives the REAL shipped script
against a fixture token file, on any seat/CI runner — not a copy of it, which
could pass while the real script drifts.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "echo-merge.sh"

# Logs "GIT <argv>" for every call, then answers just the subcommands
# echo-merge.sh's phase 1 (push) issues against a `-C <worktree>` invocation.
GIT_SHIM = r"""#!/usr/bin/env bash
echo "GIT $*" >> "$LOGFILE"
args=("$@")
i=0
if [[ "${args[0]:-}" == "-C" ]]; then
  i=2
fi
sub="${args[$i]:-}"
case "$sub" in
  branch) echo "$FAKE_BRANCH" ;;
  rev-parse) echo "$FAKE_LOCAL_SHA" ;;
  ls-remote) printf '%s\trefs/heads/%s\n' "$FAKE_ORIGIN_SHA" "$FAKE_BRANCH" ;;
  *) exit 0 ;;
esac
"""

# Logs "GH <argv>" for every call, then answers just enough of the PR-reuse /
# CI-poll / ci-gate sequence to reach the dry-run exit (phase 4, EXECUTE=0).
GH_SHIM = r"""#!/usr/bin/env bash
echo "GH $*" >> "$LOGFILE"
case "$1" in
  pr)
    case "$2" in
      list) echo "${FAKE_PR_NUM-42}" ;;
      *) exit 0 ;;
    esac
    ;;
  run)
    case "$2" in
      list) echo "999" ;;
      view)
        if [[ "$*" == *"status,conclusion"* ]]; then
          echo "completed success"
        elif [[ "$*" == *"jobs"* ]]; then
          echo "success"
        fi
        ;;
    esac
    ;;
  *) exit 0 ;;
esac
"""


class EchoMergeNoVerifyPushTest(unittest.TestCase):
    """Drives the real tools/echo-merge.sh through a full dry-run (git/gh
    shimmed) and asserts the exact push argv it constructs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        token = self.tmp / "gh_token.env"
        token.write_text("GH_TOKEN=fixture-not-real\n")

        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"
        self._write_shim("git", GIT_SHIM)
        self._write_shim("gh", GH_SHIM)

        # All git calls are shimmed, so this never needs to be a real repo.
        self.worktree = self.tmp / "worktree"
        self.worktree.mkdir()

        self.branch = "agent/T-9999"
        self.env = dict(os.environ)
        self.env["PATH"] = f"{self.bin}:{self.env.get('PATH', '')}"
        self.env["TOKEN_ENV"] = str(token)
        self.env["LOGFILE"] = str(self.log)
        self.env["FAKE_BRANCH"] = self.branch
        self.env["FAKE_LOCAL_SHA"] = "1" * 40
        self.env["FAKE_ORIGIN_SHA"] = "2" * 40  # differs from local -> real push, not auto-resume skip

    def _write_shim(self, name, body):
        p = self.bin / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def _run(self, *extra_args):
        cmd = ["bash", str(SCRIPT), "T-9999", "--worktree", str(self.worktree), *extra_args]
        return subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=30)

    def _push_line(self):
        lines = [l for l in self.log.read_text().splitlines()
                 if l.startswith("GIT ") and " push " in l]
        self.assertEqual(len(lines), 1, f"expected exactly one git push call, got: {lines}")
        return lines[0]

    def test_default_push_argv_is_byte_identical_to_before(self):
        """CRITICAL SAFETY CONSTRAINT: without the flag, push argv is unchanged."""
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self._push_line(),
            f"GIT -C {self.worktree} push -u origin {self.branch}",
        )

    def test_no_verify_push_flag_adds_no_verify_to_the_push(self):
        result = self._run("--no-verify-push")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--no-verify", self._push_line().split())

    def test_help_documents_the_flag(self):
        result = subprocess.run(["bash", str(SCRIPT), "-h"], capture_output=True, text=True)
        self.assertIn("--no-verify-push", result.stdout)


class EchoMergeStagedTest(unittest.TestCase):
    """T-2877: the --stage-push / --merge-when-green split. Drives the REAL
    shipped script through both stages with git/gh shimmed, asserting each stage
    does its half and NOT the other's (stage 1 never polls/merges; stage 2 never
    pushes)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        token = self.tmp / "gh_token.env"
        token.write_text("GH_TOKEN=fixture-not-real\n")

        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"
        for name, body in (("git", GIT_SHIM), ("gh", GH_SHIM)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        self.worktree = self.tmp / "worktree"
        self.worktree.mkdir()
        self.branch = "agent/T-9999"
        self.env = dict(os.environ)
        self.env["PATH"] = f"{self.bin}:{self.env.get('PATH', '')}"
        self.env["TOKEN_ENV"] = str(token)
        self.env["LOGFILE"] = str(self.log)
        self.env["FAKE_BRANCH"] = self.branch
        self.env["FAKE_LOCAL_SHA"] = "1" * 40
        self.env["FAKE_ORIGIN_SHA"] = "2" * 40  # differs -> real push, not auto-resume skip

    def _run(self, *extra_args):
        cmd = ["bash", str(SCRIPT), "T-9999", "--worktree", str(self.worktree), *extra_args]
        return subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=30)

    def _calls(self, needle):
        if not self.log.exists():
            return []
        return [l for l in self.log.read_text().splitlines() if needle in l]

    # ---- stage 1: --stage-push ----------------------------------------------
    def test_stage_push_pushes_prints_run_id_and_exits_before_polling(self):
        result = self._run("--stage-push", "--no-verify-push")
        self.assertEqual(result.returncode, 0, result.stderr)
        # prints the machine-greppable handoff on stdout
        self.assertRegex(result.stdout, r"run-id=999\b")
        # it pushed (established remote state) ...
        self.assertTrue(self._calls("GIT ") and any(" push " in l for l in self._calls("GIT ")),
                        "stage-push must push")
        # ... but did NOT poll CI (that is the half that outlasts the cap) ...
        self.assertEqual(self._calls("GH run view"), [], "stage-push must not poll CI")
        # ... and did NOT merge.
        self.assertEqual(self._calls("GH pr merge"), [], "stage-push must never merge")

    def test_stage_push_refuses_execute(self):
        result = self._run("--stage-push", "--execute", "--no-verify-push")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("never merges", result.stderr)

    def test_stage_push_and_merge_when_green_are_mutually_exclusive(self):
        result = self._run("--stage-push", "--merge-when-green")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mutually exclusive", result.stderr)

    # ---- stage 2: --merge-when-green ----------------------------------------
    def test_merge_when_green_with_run_id_merges_without_pushing(self):
        result = self._run("--merge-when-green", "12345", "--execute")
        self.assertEqual(result.returncode, 0, result.stderr)
        # never pushes (resumes from the remote state stage 1 left)
        self.assertFalse(any(" push " in l for l in self._calls("GIT ")),
                         "merge-when-green must not push")
        # trusts the provided run-id (no run-list relocate needed)
        self.assertEqual(self._calls("GH run list"), [],
                         "explicit run-id should skip the locate loop")
        # and merges with --admin
        merges = self._calls("GH pr merge")
        self.assertEqual(len(merges), 1, merges)
        self.assertIn("--admin", merges[0].split())

    def test_merge_when_green_without_run_id_relocates_by_branch(self):
        result = self._run("--merge-when-green", "--execute")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(" push " in l for l in self._calls("GIT ")))
        # no explicit id -> it locates the run by branch
        self.assertTrue(self._calls("GH run list"), "must relocate the run by branch")
        self.assertEqual(len(self._calls("GH pr merge")), 1)

    def test_merge_when_green_dry_run_default_does_not_merge(self):
        result = self._run("--merge-when-green", "12345")  # no --execute
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._calls("GH pr merge"), [],
                         "dry-run default must not merge (safety model preserved)")

    def test_merge_when_green_refuses_when_no_open_pr(self):
        self.env["FAKE_PR_NUM"] = ""  # gh pr list returns empty -> no PR
        result = self._run("--merge-when-green", "12345", "--execute")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("run --stage-push first", result.stderr)
        self.assertEqual(self._calls("GH pr merge"), [])

    def test_help_documents_the_staged_flags(self):
        result = subprocess.run(["bash", str(SCRIPT), "-h"], capture_output=True, text=True)
        self.assertIn("--stage-push", result.stdout)
        self.assertIn("--merge-when-green", result.stdout)


if __name__ == "__main__":
    unittest.main()
