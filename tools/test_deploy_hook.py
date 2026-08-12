#!/usr/bin/env python3
"""Tests for deploy_hook — the mechanism that replaces a remembered procedure.

The point of this file: specialist's argument that the `[ -L ]` check must live in the deploy
PATH with a test, not in a memory. "A mechanical assertion beats reading it over" was the
day's conclusion; a test suite for the decision is what makes that true here.
"""
import contextlib
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import pathlib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import deploy_hook as dh  # noqa: E402


class PlanStepTest(unittest.TestCase):
    """Decision tests. These drive the REAL probe against REAL paths on this account
    (probe_is_symlink runs bash locally when the account is $USER), so they exercise the
    actual `[ -L ]` predicate rather than a mock of it — which matters, because the bug
    this tool exists to prevent came from a WRONG predicate (pathlib, which raises on a
    700-mode peer home), not from wrong branching on a right one."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.me = os.environ.get("USER", "")

    def _decide(self, path):
        return dh.plan_step(self.me, path, is_symlink=dh.probe_is_symlink(self.me, path))

    def test_symlink_target_pulls_never_copies(self):
        """THE WHOLE POINT: cp over a symlink writes through into a peer's worktree."""
        target = self.d / "repo_copy.sh"
        target.write_text("x")
        link = self.d / "deployed.sh"
        link.symlink_to(target)
        step = self._decide(link)
        self.assertEqual(step.action, dh.ACTION_PULL)
        self.assertIn("write", step.reason)

    def test_regular_file_target_copies(self):
        f = self.d / "deployed.sh"
        f.write_text("x")
        self.assertEqual(self._decide(f).action, dh.ACTION_COPY)

    def test_absent_target_copies(self):
        """A fresh account has no symlink to write through and needs the file."""
        self.assertEqual(self._decide(self.d / "nope.sh").action, dh.ACTION_COPY)

    def test_broken_symlink_still_pulls(self):
        """A dangling symlink is still a symlink — `-e` is false but `-L` is true, and
        cp would still write through to wherever it points."""
        link = self.d / "dangling.sh"
        link.symlink_to(self.d / "does-not-exist")
        self.assertEqual(self._decide(link).action, dh.ACTION_PULL)

    def test_decision_is_injectable_for_testing(self):
        self.assertEqual(dh.plan_step("x", self.d / "any", is_symlink=True).action, dh.ACTION_PULL)
        self.assertEqual(dh.plan_step("x", self.d / "any", is_symlink=False).action, dh.ACTION_COPY)


class ProbeTest(unittest.TestCase):
    """The probe must shell out AS THE ACCOUNT. Peer homes are mode 700, so
    Path.is_symlink() raises PermissionError rather than returning False — my own pinned
    memory (T-1867 killed a grooming run that way, silently), which I nonetheless wrote
    into the first version of this file. A live dry-run caught it in seconds."""

    def _run(self, out):
        class R:
            stdout = out
            stderr = ""
            returncode = 0
        return lambda *a, **k: R

    def test_symlink_output(self):
        self.assertIs(dh.probe_is_symlink("claude", "/p", run=self._run("SYMLINK\n")), True)

    def test_regular_output(self):
        self.assertIs(dh.probe_is_symlink("claude", "/p", run=self._run("REGULAR\n")), False)

    def test_absent_output_is_copyable_not_unknown(self):
        """Absent means a fresh account that needs the file — not an unknown."""
        self.assertIs(dh.probe_is_symlink("claude", "/p", run=self._run("ABSENT\n")), False)

    def test_unexpected_output_is_unknown(self):
        self.assertIsNone(dh.probe_is_symlink("claude", "/p", run=self._run("sudo: denied")))

    def test_empty_output_is_unknown(self):
        self.assertIsNone(dh.probe_is_symlink("claude", "/p", run=self._run("")))


class FailClosedTest(unittest.TestCase):
    def test_unknown_symlink_state_refuses(self):
        """FAIL CLOSED. If we cannot tell whether it is a symlink, a cp might write
        through — and not doing that on a guess is the entire point of this tool."""
        step = dh.plan_step("claude", Path("/nope"), is_symlink=None)
        self.assertEqual(step.action, dh.ACTION_REFUSE)
        self.assertIn("refusing rather than guessing", step.reason)

    def test_unknown_account_does_not_block_the_others(self):
        steps = dh.plan("h.sh", ["a", "b"], symlink_map={"a": None, "b": False}, pushed=True)
        self.assertEqual([s.action for s in steps], [dh.ACTION_REFUSE, dh.ACTION_COPY])


class PlanTest(unittest.TestCase):
    def test_unpushed_canonical_refuses_every_account(self):
        """Ordering discipline enforced, not remembered: deploying before pushing makes
        every peer's integrity check fire on a difference a pull cannot resolve."""
        steps = dh.plan("h.sh", ["claude", "claude-research"], pushed=False)
        self.assertEqual({s.action for s in steps}, {dh.ACTION_REFUSE})
        self.assertIn("push the canonical BEFORE", steps[0].reason)

    def test_mixed_per_account_is_respected(self):
        """Deployment is mixed per FILE and per ACCOUNT — the map is not uniform.

        This is the case I got wrong twice by generalising from one account's listing,
        including on my own account. The plan must read each pair independently.
        """
        steps = dh.plan("h.sh", ["claude", "claude-research", "claude-fallback"],
                        symlink_map={"claude": True, "claude-research": True,
                                     "claude-fallback": False},
                        pushed=True)
        self.assertEqual([s.action for s in steps],
                         [dh.ACTION_PULL, dh.ACTION_PULL, dh.ACTION_COPY])

    def test_all_regular_files_plan_all_copies(self):
        steps = dh.plan("h.sh", ["a", "b"], symlink_map={"a": False, "b": False}, pushed=True)
        self.assertEqual({s.action for s in steps}, {dh.ACTION_COPY})

    def test_step_serialises(self):
        s = dh.plan("h.sh", ["a"], symlink_map={"a": True}, pushed=True)[0]
        self.assertEqual(set(s.to_dict()), {"account", "action", "reason"})


class CanonicalPushedTest(unittest.TestCase):
    def test_matching_blob_is_pushed(self):
        class R:
            returncode = 0
            stdout = (dh.CANON_DIR / "handoff-auto-read.sh").read_bytes()
        self.assertTrue(dh.canonical_is_pushed("handoff-auto-read.sh", run=lambda *a, **k: R()))

    def test_differing_blob_is_not_pushed(self):
        class R:
            returncode = 0
            stdout = b"something else entirely"
        self.assertFalse(dh.canonical_is_pushed("handoff-auto-read.sh", run=lambda *a, **k: R()))

    def test_git_failure_is_not_pushed(self):
        """Fail CLOSED here: if we cannot prove it is pushed, do not deploy."""
        class R:
            returncode = 128
            stdout = b""
        self.assertFalse(dh.canonical_is_pushed("handoff-auto-read.sh", run=lambda *a, **k: R()))


class ExecuteTest(unittest.TestCase):
    """Execution is stubbed — these assert the CONTRACT, not live accounts."""

    def _runner(self, md5, *, record=None):
        def run(cmd, **kw):
            joined = " ".join(cmd)
            class R:
                returncode = 0
                stdout = md5 if "md5sum" in joined else "PULLED"
                stderr = ""
            if record is not None:
                record.append(joined)
            if isinstance(R.stdout, bytes):
                pass
            return R
        return run

    def test_refuse_steps_are_counted_as_failures(self):
        steps = [dh.Step("claude", dh.ACTION_REFUSE, "nope")]
        self.assertEqual(dh.execute("h.sh", steps, dry_run=False,
                                    run=self._runner("abc")), 1)

    def test_dry_run_executes_nothing(self):
        calls = []
        steps = [dh.Step("claude", dh.ACTION_PULL, "r")]
        dh.execute("h.sh", steps, dry_run=True, run=self._runner("abc", record=calls))
        self.assertEqual([c for c in calls if "cp " in c or "pull" in c], [],
                         "dry-run must not pull or copy")

    def test_pull_path_never_invokes_cp(self):
        """Regression guard for the actual incident: a symlinked account must never cp."""
        calls = []
        steps = [dh.Step("claude-research", dh.ACTION_PULL, "r")]
        dh.execute("h.sh", steps, dry_run=False, run=self._runner("x", record=calls))
        self.assertEqual([c for c in calls if " cp " in c], [],
                         "the pull path must never shell out to cp")


class TestT2227OriginFreshness(unittest.TestCase):
    """Finding 1 (specialist): precondition and verification both read a CACHED origin/main."""

    def _runner(self, *, fetch_rc=0, local="aaa111", remote="aaa111", lsr_rc=0):
        def run(cmd, **kw):
            joined = " ".join(cmd)
            if "fetch" in cmd:
                return types.SimpleNamespace(returncode=fetch_rc, stdout=b"", stderr=b"")
            if "rev-parse" in cmd:
                return types.SimpleNamespace(returncode=0, stdout=local + "\n", stderr="")
            if "ls-remote" in cmd:
                return types.SimpleNamespace(returncode=lsr_rc, stdout=remote + "\trefs/heads/main\n", stderr="")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return run

    def test_fresh_when_local_matches_remote(self):
        ok, detail = dh.origin_is_fresh(run=self._runner(local="deadbeef1", remote="deadbeef1"))
        self.assertTrue(ok)
        self.assertIn("deadbeef1", detail)

    def test_REFUSES_when_cached_ref_is_stale(self):
        """The live case: my own clone was 78ef1350 while the remote was 73d8e362."""
        ok, detail = dh.origin_is_fresh(run=self._runner(local="78ef13504", remote="73d8e362e"))
        self.assertFalse(ok, "a stale cached ref must REFUSE, not pass")
        self.assertIn("STALE", detail)

    def test_refuses_when_fetch_fails_rather_than_using_cache(self):
        ok, detail = dh.origin_is_fresh(run=self._runner(fetch_rc=1))
        self.assertFalse(ok)
        self.assertIn("fetch", detail.lower())

    def test_refuses_when_remote_unreachable(self):
        ok, _ = dh.origin_is_fresh(run=self._runner(lsr_rc=128))
        self.assertFalse(ok)

    def test_refuses_on_empty_ref(self):
        ok, _ = dh.origin_is_fresh(run=self._runner(local="", remote=""))
        self.assertFalse(ok)


class TestT2227StagingNoSymlinkFollow(unittest.TestCase):
    """Finding 2 (specialist): the staging write followed symlinks at a PREDICTABLE path."""

    def test_stage_canonical_does_not_follow_a_planted_symlink(self):
        """Exercises deploy_hook's OWN staging function, not tempfile's semantics."""
        with tempfile.TemporaryDirectory() as td:
            victim = os.path.join(td, "victim")
            with open(victim, "w") as fh:
                fh.write("ORIGINAL")
            src = os.path.join(td, "canon.sh")
            with open(src, "wb") as fh:
                fh.write(b"DEPLOYED BYTES")
            planted = "/tmp/recovery-deploy-t2227probe.sh"      # the OLD predictable path
            try:
                if os.path.lexists(planted):
                    os.unlink(planted)
                os.symlink(victim, planted)
                stage = dh.stage_canonical("t2227probe.sh", source=pathlib.Path(src))
                self.assertNotEqual(stage, planted,
                                    "stage must not be the predictable path")
                with open(victim) as fh:
                    self.assertEqual(fh.read(), "ORIGINAL",
                                     "stage_canonical must NOT write through a planted symlink")
                with open(stage, "rb") as fh:
                    self.assertEqual(fh.read(), b"DEPLOYED BYTES")
                self.assertEqual(oct(os.stat(stage).st_mode & 0o777), oct(0o644))
                os.unlink(stage)
            finally:
                try:
                    os.unlink(planted)
                except OSError:
                    pass

    def test_stage_canonical_survives_a_preexisting_predictable_path(self):
        """The old code raised PermissionError if the predictable path existed."""
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "c.sh")
            with open(src, "wb") as fh:
                fh.write(b"X")
            blocker = "/tmp/recovery-deploy-t2227blocker.sh"
            try:
                with open(blocker, "w") as fh:
                    fh.write("in the way")
                stage = dh.stage_canonical("t2227blocker.sh", source=pathlib.Path(src))
                self.assertTrue(os.path.isfile(stage))
                os.unlink(stage)
            finally:
                try:
                    os.unlink(blocker)
                except OSError:
                    pass


class TestT2227CurrentUser(unittest.TestCase):
    """Finding 3 (specialist): $USER is commonly unset under systemd/cron."""

    def test_resolves_user_with_USER_unset(self):
        saved = os.environ.pop("USER", None)
        try:
            self.assertTrue(dh._current_user(),
                            "_current_user() must resolve even with $USER unset")
        finally:
            if saved is not None:
                os.environ["USER"] = saved

    def test_self_account_takes_local_branch_without_USER(self):
        saved = os.environ.pop("USER", None)
        try:
            me = dh._current_user()
            calls = []
            def run(cmd, **kw):
                calls.append(cmd)
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            dh._as_user(me, "true", run=run)
            self.assertNotIn("sudo", calls[0], "own account must NOT go through sudo")
        finally:
            if saved is not None:
                os.environ["USER"] = saved


CANON_BYTES = b"#!/bin/bash\n# canonical hook bytes\n"
CANON_MD5 = hashlib.md5(CANON_BYTES).hexdigest()


def _settings(commands, *, key="SessionStart"):
    """Build a settings.json body registering `commands` under a SessionStart entry.

    Mirrors the real shape measured on suborch2 2026-07-28: hooks -> SessionStart -> a list
    of matcher groups, each with an inner `hooks` list of {"type","command"}.
    """
    return json.dumps({"hooks": {key: [{"hooks": [{"type": "command", "command": c}
                                                  for c in commands]}]}})


class FleetStub:
    """One injectable `run=` that answers every shell deploy_hook issues, per account.

    THIS IS THE ONLY AVAILABLE SEAM and that is a measured constraint, not a preference:
    this seat has no passwordless sudo to any peer account (`sudo -n -u <acct>` returns
    "sudo: a password is required" for all five), so a cross-account wet run cannot be
    performed at all. deploy_hook was written for this — every function takes `run=` — so
    the contract is asserted here and the single-account behaviour is wet-run against
    suborch2 itself, where _as_user short-circuits sudo entirely.
    """

    def __init__(self, *, md5=None, settings=None, blob=CANON_BYTES, copy_note="COPIED"):
        self.md5 = md5 or {}
        self.settings = settings or {}
        self.blob = blob
        self.copy_note = copy_note
        self.calls: list[str] = []

    def __call__(self, cmd, **kw):
        joined = " ".join(str(c) for c in cmd)
        self.calls.append(joined)
        if "show" in cmd:                       # git show origin/main:tools/hooks/<hook>
            return types.SimpleNamespace(returncode=0, stdout=self.blob, stderr=b"")
        acct = cmd[3] if cmd[0] == "sudo" else "SELF"
        shell = str(cmd[-1])
        if "md5sum" in shell:
            return types.SimpleNamespace(returncode=0, stdout=self.md5.get(acct, ""), stderr="")
        if "settings.json" in shell:
            return types.SimpleNamespace(
                returncode=0, stdout=self.settings.get(acct, dh._SETTINGS_ABSENT), stderr="")
        return types.SimpleNamespace(returncode=0, stdout=self.copy_note, stderr="")

    def shells(self):
        """Just the bash -c payloads, which is where the deploy commands live."""
        return [c for c in self.calls if "bash -c" in c]


# ---------------------------------------------------------------- T-2324
# An account that is provably NOT whoever is running this suite.
#
# probe_registration/_as_user branch on `account == _current_user()`: a peer's
# 700-mode home is only readable through sudo, one's own is read directly. The
# tests below assert the CROSS-ACCOUNT branch, and they used to do it by pinning
# the literal "claude" — which is a real seat, so on THAT seat the SUT correctly
# took the self branch and five tests failed. Same bytes, 73/73 as recovery, 5 failed
# as orchestrator, and CI green because the runner is a third user entirely. A test that
# names a real account is asserting "I am not that account", which is a fact about
# the machine rather than about the code.
#
# Pick a real-looking seat that is not the current user, so the cross-account
# branch is exercised identically on every seat and in CI.
_KNOWN_SEATS = ("claude", "claude-research", "claude-fallback",
                "exocortex", "suborch1", "suborch2")
FOREIGN = next(a for a in _KNOWN_SEATS if a != dh._current_user())
FOREIGN_HOME = f"/home/{FOREIGN}"


class TestT2261VirginSeatNeedsMkdir(unittest.TestCase):
    """DEFECT 1. The COPY branch ran a bare `cp` into ~/.claude/hooks/, a directory that
    does not exist on a seat that has never received a hook — so the FIRST deploy to every
    new persona failed. Measured on suborch2 2026-07-28: `cp /dev/null ~/.claude/hooks/probe`
    -> "No such file or directory"; the tool printed `COPY deployed=? MISMATCH`, exit 1.
    """

    def test_copy_command_creates_the_parent_dir_before_copying(self):
        """Unit: the emitted command must contain the mkdir, AHEAD of the cp."""
        stub = FleetStub(md5={"suborch1": CANON_MD5})
        steps = [dh.Step("suborch1", dh.ACTION_COPY, "r")]
        with contextlib.redirect_stdout(io.StringIO()):
            dh.execute("h.sh", steps, dry_run=False, stage="/tmp/stage", run=stub)
        cp_shell = [s for s in stub.shells() if " cp " in s]
        self.assertTrue(cp_shell, "the COPY branch must shell out to cp")
        shell = cp_shell[0]
        self.assertIn("mkdir -p ~/.claude/hooks", shell,
                      "a virgin seat has no ~/.claude/hooks — the bare cp exits 1")
        self.assertLess(shell.index("mkdir -p ~/.claude/hooks"), shell.index(" cp "),
                        "mkdir must run BEFORE the cp, not after it")

    def test_pull_branch_still_never_mkdirs_or_copies(self):
        """The mkdir belongs to COPY only — PULL must stay a pure `git pull`."""
        stub = FleetStub(md5={FOREIGN: CANON_MD5})
        with contextlib.redirect_stdout(io.StringIO()):
            dh.execute("h.sh", [dh.Step(FOREIGN, dh.ACTION_PULL, "r")],
                       dry_run=False, run=stub)
        self.assertEqual([s for s in stub.shells() if "mkdir" in s or " cp " in s], [])

    def test_real_shell_lands_the_file_in_a_home_that_has_no_claude_dir(self):
        """Integ (same-process): run the ACTUAL emitted command through a REAL bash against
        a REAL virgin HOME. The unit test above asserts the string; this asserts the string
        works — the defect was that a plausible-looking command did not."""
        with tempfile.TemporaryDirectory() as home:
            stage = os.path.join(home, "stage.sh")
            with open(stage, "wb") as fh:
                fh.write(CANON_BYTES)
            self.assertFalse(os.path.exists(os.path.join(home, ".claude")),
                             "precondition: this HOME is virgin, exactly like a new seat")
            captured = {}

            def run(cmd, **kw):
                shell = str(cmd[-1])
                captured.setdefault("shells", []).append(shell)
                r = subprocess.run(["bash", "-c", shell], capture_output=True, text=True,
                                   env={"HOME": home, "PATH": os.environ.get("PATH", "")})
                return r
            stub_blob = FleetStub(md5={}, blob=CANON_BYTES)

            def dispatch(cmd, **kw):
                if "show" in cmd:
                    return stub_blob(cmd, **kw)
                return run(cmd, **kw)

            with contextlib.redirect_stdout(io.StringIO()) as out:
                failures = dh.execute("h.sh", [dh.Step(FOREIGN, dh.ACTION_COPY, "r")],
                                      dry_run=False, stage=stage, run=dispatch)
            landed = os.path.join(home, ".claude", "hooks", "h.sh")
            self.assertTrue(os.path.isfile(landed),
                            f"first deploy must land on a virgin seat; got:\n{out.getvalue()}")
            with open(landed, "rb") as fh:
                self.assertEqual(fh.read(), CANON_BYTES)
            self.assertEqual(oct(os.stat(landed).st_mode & 0o777), oct(0o755))
            self.assertEqual(failures, 0, out.getvalue())


class TestT2261VerifyStillFailsClosed(unittest.TestCase):
    """The md5 verify is what made DEFECT 1 visible instead of silent. Fixing the copy must
    not soften it — so this pins the failure direction, not just the success direction."""

    def test_wrong_bytes_on_disk_still_report_MISMATCH_and_count_as_failure(self):
        stub = FleetStub(md5={FOREIGN: "deadbeefdeadbeefdeadbeefdeadbeef"},
                         settings={FOREIGN: _settings([f"{FOREIGN_HOME}/.claude/hooks/h.sh"])})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            failures = dh.execute("h.sh", [dh.Step(FOREIGN, dh.ACTION_COPY, "r")],
                                  dry_run=False, stage="/tmp/s", run=stub)
        self.assertEqual(failures, 1, "wrong bytes MUST fail the run")
        self.assertIn("MISMATCH", out.getvalue())
        self.assertNotIn("VERIFIED", out.getvalue())

    def test_a_registered_hook_with_wrong_bytes_is_still_a_failure(self):
        """ADVERSARIAL: the new activation axis must not be able to rescue a byte failure.
        ACTIVE + wrong bytes is still exit-nonzero — the two axes are independent."""
        stub = FleetStub(md5={FOREIGN: "0" * 32},
                         settings={FOREIGN: _settings([f"{FOREIGN_HOME}/.claude/hooks/h.sh"])})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            failures = dh.execute("h.sh", [dh.Step(FOREIGN, dh.ACTION_COPY, "r")],
                                  dry_run=False, stage="/tmp/s", run=stub)
        self.assertEqual(failures, 1)
        self.assertIn("MISMATCH", out.getvalue())
        self.assertIn(dh.STATUS_ACTIVE, out.getvalue())

    def test_unresolvable_canonical_digest_is_a_failure_not_a_pass(self):
        """want=None must not compare equal to anything."""
        def run(cmd, **kw):
            if "show" in cmd:
                return types.SimpleNamespace(returncode=128, stdout=b"", stderr=b"")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dh.execute("h.sh", [dh.Step("claude", dh.ACTION_COPY, "r")],
                                        dry_run=False, stage="/tmp/s", run=run), 1)


class TestT2261RegistrationParsing(unittest.TestCase):
    """DEFECT 2's decision table. Pure function, so the whole table is testable without a
    peer account — which is the point, because no peer account is reachable from this seat.

    Amendment H2: the old verify could not distinguish "this hook will run" from "this hook
    is inert" and printed VERIFIED for both. Every case below names the input that makes the
    check produce a different answer.
    """

    HOOK = "handoff-auto-read.sh"
    ACCT = "suborch2"
    DEPLOYED = "/home/suborch2/.claude/hooks/handoff-auto-read.sh"

    def _p(self, text):
        return dh.parse_registration(text, self.HOOK, self.ACCT)

    # ---- the two poles
    def test_registered_at_the_deployed_path_is_ACTIVE(self):
        status, detail = self._p(_settings([self.DEPLOYED]))
        self.assertEqual(status, dh.STATUS_ACTIVE)
        self.assertIn(self.DEPLOYED, detail)

    def test_bytes_present_but_a_different_hook_registered_is_INERT(self):
        """THE MEASURED CASE. metis's and atlas's seats both had this exact settings.json —
        one SessionStart entry, ensure-hooks-active.sh — while handoff-auto-read.sh sat on
        disk byte-perfect, mode 755, VERIFIED, and was never invoked by anything."""
        status, detail = self._p(_settings(
            ["/home/suborch2/pantheon-config/tools/hooks/ensure-hooks-active.sh"]))
        self.assertEqual(status, dh.STATUS_INERT)
        self.assertIn("never be invoked", detail)

    # ---- the shapes a real registration takes
    def test_tilde_path_is_ACTIVE(self):
        self.assertEqual(self._p(_settings(["~/.claude/hooks/handoff-auto-read.sh"]))[0],
                         dh.STATUS_ACTIVE)

    def test_HOME_variable_path_is_ACTIVE(self):
        self.assertEqual(self._p(_settings(['"$HOME/.claude/hooks/handoff-auto-read.sh"']))[0],
                         dh.STATUS_ACTIVE)

    def test_interpreter_prefix_and_arguments_are_ACTIVE(self):
        self.assertEqual(
            self._p(_settings(["bash /home/suborch2/.claude/hooks/handoff-auto-read.sh --quiet"]))[0],
            dh.STATUS_ACTIVE)

    def test_registered_at_a_different_path_is_ELSEWHERE_not_ACTIVE(self):
        """Something runs — but not the bytes just verified. Collapsing this into ACTIVE
        would rebuild the defect one level in: a green report for a file nothing invokes."""
        status, detail = self._p(_settings(
            ["/home/suborch2/pantheon-config/tools/hooks/handoff-auto-read.sh"]))
        self.assertEqual(status, dh.STATUS_ELSEWHERE)
        self.assertIn("NOT the deployed", detail)

    # ---- the malformed table
    def test_settings_absent_is_INERT_not_unknown(self):
        """A file that is not there cannot register anything — that is a MEASUREMENT, and
        it is the virgin-seat case the mkdir fix creates."""
        self.assertEqual(self._p(dh._SETTINGS_ABSENT)[0], dh.STATUS_INERT)

    def test_settings_unreadable_is_UNKNOWN(self):
        self.assertEqual(self._p(dh._SETTINGS_UNREADABLE)[0], dh.STATUS_UNKNOWN)

    def test_malformed_json_is_UNKNOWN_never_active(self):
        self.assertEqual(self._p('{"hooks": {"SessionStart": [')[0], dh.STATUS_UNKNOWN)

    def test_json_that_is_not_an_object_is_UNKNOWN(self):
        self.assertEqual(self._p("[1, 2, 3]")[0], dh.STATUS_UNKNOWN)

    def test_empty_settings_is_UNKNOWN(self):
        self.assertEqual(self._p("   ")[0], dh.STATUS_UNKNOWN)

    def test_none_is_UNKNOWN(self):
        self.assertEqual(self._p(None)[0], dh.STATUS_UNKNOWN)

    def test_no_hooks_key_is_INERT(self):
        self.assertEqual(self._p('{"permissions": {"allow": []}}')[0], dh.STATUS_INERT)

    def test_T2320_a_PreToolUse_registration_is_ACTIVE_not_INERT(self):
        """WAS `test_hooks_present_but_no_SessionStart_is_INERT`, and it pinned the
        DEFECT: the probe read only hooks['SessionStart'], so a PreToolUse hook that
        was registered, executable and firing reported INERT — a false negative
        shaped exactly like a failed deploy, emitted by the one tool built to stop
        presence being mistaken for activation.

        Measured consequence (T-2313): background-pipe-gate.py is PreToolUse(Bash),
        so verifying that rollout with this tool would have said INERT on all five
        seats AFTER a successful deploy."""
        status, detail = self._p(_settings([self.DEPLOYED], key="PreToolUse"))
        self.assertEqual(status, dh.STATUS_ACTIVE)
        self.assertIn("PreToolUse", detail,
                      "the verdict must name the event it was found under")

    def test_T2320_registered_under_no_event_is_still_INERT(self):
        """Widening must not make INERT unreachable — an empty hooks object still
        registers nothing, and that is a positive measurement, not an unknown."""
        self.assertEqual(self._p('{"hooks": {}}')[0], dh.STATUS_INERT)
        self.assertEqual(self._p('{"hooks": {"PreToolUse": [], "Stop": []}}')[0],
                         dh.STATUS_INERT)

    def test_T2320_a_different_hook_under_another_event_is_INERT_and_says_so(self):
        status, detail = self._p(_settings(["/usr/local/bin/other.sh"], key="PreToolUse"))
        self.assertEqual(status, dh.STATUS_INERT)
        self.assertIn("PreToolUse", detail,
                      "INERT must name the events it looked at, so 'not found' is "
                      "distinguishable from 'not looked for'")

    def test_empty_SessionStart_list_is_INERT(self):
        self.assertEqual(self._p('{"hooks": {"SessionStart": []}}')[0], dh.STATUS_INERT)

    # ---- adversarial + property rungs
    def test_a_similarly_named_script_does_not_count_as_registration(self):
        """ADVERSARIAL: substring matching would call this ACTIVE. Basename equality after
        expansion is the predicate, so a backup file registers nothing."""
        status, _ = self._p(_settings(
            ["/home/suborch2/.claude/hooks/handoff-auto-read.sh.bak",
             "/home/suborch2/.claude/hooks/old-handoff-auto-read.sh"]))
        self.assertEqual(status, dh.STATUS_INERT)

    def test_unrecognised_entry_shapes_are_skipped_toward_INERT_never_ACTIVE(self):
        """A shape we fail to understand must degrade LOUD (INERT), not silent (ACTIVE)."""
        body = json.dumps({"hooks": {"SessionStart": [42, None, {"weird": "shape"}, "x"]}})
        self.assertEqual(self._p(body)[0], dh.STATUS_INERT)

    def test_PROPERTY_nothing_but_the_deployed_path_ever_yields_ACTIVE(self):
        """The invariant that makes UNKNOWN worth having: across the whole malformed space,
        no input reads as ACTIVE. Reporting ACTIVE off a config we could not read would be
        a fresh instance of the very defect class this ticket closes."""
        non_active_inputs = [
            None, "", "   ", "not json at all", "[1,2,3]", '{"hooks": []}',
            '{"hooks": {"SessionStart": []}}', '{"hooks": {"SessionStart": "nope"}}',
            dh._SETTINGS_ABSENT, dh._SETTINGS_UNREADABLE,
            _settings(["/usr/local/bin/something-else.sh"]),
            _settings(["/home/suborch2/.claude/hooks/handoff-auto-read.sh.bak"]),
            # T-2320: `_settings([self.DEPLOYED], key="PreToolUse")` USED to live here.
            # It was never a malformed input — it is a correctly-registered hook under
            # a different event, and asserting it is not ACTIVE was asserting the bug.
            # Its replacement above asserts ACTIVE. The invariant this property
            # defends — nothing UNREADABLE or MALFORMED ever reads as ACTIVE — is
            # unchanged, and every remaining entry is genuinely one of those.
            json.dumps({"hooks": {"SessionStart": [{"hooks": [{"type": "command"}]}]}}),
        ]
        for body in non_active_inputs:
            with self.subTest(body=str(body)[:60]):
                self.assertNotEqual(self._p(body)[0], dh.STATUS_ACTIVE)


class TestT2261ProbeRegistration(unittest.TestCase):
    """The probe must shell out AS THE ACCOUNT (peer homes are 700) and must distinguish
    absent from unreadable IN THE SHELL — `cat` failing cannot tell those apart, and they
    are INERT and UNKNOWN respectively."""

    def _run(self, out, rc=0):
        return lambda *a, **k: types.SimpleNamespace(returncode=rc, stdout=out, stderr="")

    def test_probe_reads_settings_json_as_the_account(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout=dh._SETTINGS_ABSENT, stderr="")
        dh.probe_registration(FOREIGN, "h.sh", run=run)
        self.assertEqual(calls[0][:4], ["sudo", "-n", "-u", FOREIGN],
                         "a peer's 700-mode home is only readable AS that peer")
        self.assertIn("settings.json", calls[0][-1])

    def test_shell_distinguishes_absent_from_unreadable(self):
        shell = []

        def run(cmd, **kw):
            shell.append(str(cmd[-1]))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        dh.probe_registration("claude", "h.sh", run=run)
        self.assertIn("-r ", shell[0])
        self.assertIn(dh._SETTINGS_UNREADABLE, shell[0])
        self.assertIn(dh._SETTINGS_ABSENT, shell[0])

    def test_sudo_denied_is_UNKNOWN_not_inert(self):
        """FAIL SAFE, and the distinction matters: 'I could not look' is not 'I looked and
        it was not there'. Measured constraint — this seat's sudo to every peer is denied."""
        status, detail = dh.probe_registration("claude", "h.sh", run=self._run("", rc=1))
        self.assertEqual(status, dh.STATUS_UNKNOWN)
        self.assertIn("sudo", detail)

    def test_absent_sentinel_becomes_INERT(self):
        self.assertEqual(
            dh.probe_registration("claude", "h.sh", run=self._run(dh._SETTINGS_ABSENT))[0],
            dh.STATUS_INERT)


class TestT2342SelfBranchProbeRegistration(unittest.TestCase):
    """T-2342: the SELF branch of probe_registration had no assertion of its own.

    The asymmetry is what makes this worth doing: an agent deploying a hook TO ITSELF
    is the common case, and it was the case with zero coverage. T-2324 de-coupled the
    cross-account tests from a literal account (they had pinned "claude", passing on
    five seats and failing on the sixth) — that made the existing coverage honest, but
    it added none.

    ⚠️ WRITTEN SO THEY CANNOT BECOME THE SAME DEFECT INVERTED. The self account is
    always `dh._current_user()` and never a literal, so these assert the same thing on
    every seat. FOREIGN is likewise derived. A test that pinned "claude-fallback" here
    would reproduce exactly the bug T-2324 removed, with the branches swapped.
    """

    HOOK = "handoff-auto-read.sh"

    def _run_capturing(self, out, rc=0, sink=None):
        def run(cmd, **kw):
            if sink is not None:
                sink.append(cmd)
            return types.SimpleNamespace(returncode=rc, stdout=out, stderr="")
        return run

    def _deployed_for(self, account):
        return f"/home/{account}/.claude/hooks/{self.HOOK}"

    # ---- 1: the branch is actually taken -----------------------------------
    def test_self_account_probe_takes_the_LOCAL_branch(self):
        """The transport half. `sudo -n -u <me>` would also usually work, so this is
        not merely cosmetic — going through sudo for our own account is the T-2227
        failure (USER unset under systemd/cron makes the tool sudo to itself), and the
        local branch is what avoids it."""
        me = dh._current_user()
        calls = []
        dh.probe_registration(me, self.HOOK,
                              run=self._run_capturing(dh._SETTINGS_ABSENT, sink=calls))
        self.assertEqual(calls[0][:2], ["bash", "-c"],
                         "own account must run directly, not through sudo")
        self.assertNotIn("sudo", calls[0])
        self.assertIn("settings.json", calls[0][-1])

    def test_self_and_foreign_issue_the_SAME_shell_program(self):
        """Only the transport may differ. If the two branches ever ran different shell
        text, identical settings could produce different verdicts and no single-account
        run would reveal it."""
        me, other = dh._current_user(), FOREIGN
        mine, theirs = [], []
        dh.probe_registration(me, self.HOOK,
                              run=self._run_capturing(dh._SETTINGS_ABSENT, sink=mine))
        dh.probe_registration(other, self.HOOK,
                              run=self._run_capturing(dh._SETTINGS_ABSENT, sink=theirs))
        self.assertEqual(mine[0][-1], theirs[0][-1],
                         "the shell program must be identical across branches — only "
                         "the transport (bash -c vs sudo -u) may differ")
        self.assertEqual(theirs[0][:4], ["sudo", "-n", "-u", other])

    # ---- 2: the branches must agree on MEANING -----------------------------
    def _verdict_pair(self, stdout, rc=0):
        me, other = dh._current_user(), FOREIGN
        mine = dh.probe_registration(me, self.HOOK,
                                     run=self._run_capturing(stdout, rc=rc))
        theirs = dh.probe_registration(other, self.HOOK,
                                       run=self._run_capturing(stdout, rc=rc))
        return mine, theirs

    def test_ACTIVE_agrees_across_branches(self):
        """Same bytes in, same verdict out. Each branch is asked about ITS OWN account's
        deployed path, which is the real-world comparison — a shared literal path would
        make one of the two answer about a path it does not own."""
        me, other = dh._current_user(), FOREIGN
        mine = dh.probe_registration(
            me, self.HOOK,
            run=self._run_capturing(_settings([self._deployed_for(me)])))
        theirs = dh.probe_registration(
            other, self.HOOK,
            run=self._run_capturing(_settings([self._deployed_for(other)])))
        self.assertEqual(mine[0], dh.STATUS_ACTIVE)
        self.assertEqual(mine[0], theirs[0])

    def test_INERT_agrees_across_branches(self):
        other_hook = "/home/somewhere/.claude/hooks/ensure-hooks-active.sh"
        mine, theirs = self._verdict_pair(_settings([other_hook]))
        self.assertEqual(mine[0], dh.STATUS_INERT)
        self.assertEqual(mine[0], theirs[0])

    def test_absent_sentinel_is_INERT_on_the_self_branch_too(self):
        mine, theirs = self._verdict_pair(dh._SETTINGS_ABSENT)
        self.assertEqual(mine[0], dh.STATUS_INERT)
        self.assertEqual(mine[0], theirs[0])

    # ---- 3: the UNKNOWN cases, where a wrong answer SUPPRESSES a finding ----
    def test_unreadable_is_UNKNOWN_on_both_branches(self):
        """The direction that matters. UNKNOWN vs INERT is 'I could not look' vs 'I
        looked and it is not there'; collapsing them on the self branch would hide a
        real misregistration on the seat running the check."""
        mine, theirs = self._verdict_pair(dh._SETTINGS_UNREADABLE)
        self.assertEqual(mine[0], dh.STATUS_UNKNOWN)
        self.assertEqual(mine[0], theirs[0])

    def test_invalid_json_is_UNKNOWN_on_both_branches(self):
        mine, theirs = self._verdict_pair("{not json at all")
        self.assertEqual(mine[0], dh.STATUS_UNKNOWN)
        self.assertEqual(mine[0], theirs[0])

    def test_nonzero_returncode_is_UNKNOWN_on_both_branches(self):
        """A failing shell is UNKNOWN even locally — the self branch has no sudo to be
        denied, but bash can still fail, and 'the probe broke' must never read as
        'nothing is registered'."""
        mine, theirs = self._verdict_pair("", rc=1)
        self.assertEqual(mine[0], dh.STATUS_UNKNOWN)
        self.assertEqual(mine[0], theirs[0])


class TestT2261ExecuteReportsActivation(unittest.TestCase):
    """The operator surface. The word VERIFIED is what stopped two agents checking further,
    so what execute() PRINTS is load-bearing and is asserted here as behaviour."""

    def _row(self, *, md5, settings, action=dh.ACTION_COPY, acct=None, dry_run=False):
        acct = FOREIGN if acct is None else acct
        stub = FleetStub(md5={acct: md5}, settings={acct: settings})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            failures = dh.execute("h.sh", [dh.Step(acct, action, "r")],
                                  dry_run=dry_run, stage="/tmp/s", run=stub)
        return out.getvalue(), failures

    def test_INERT_deploy_is_reported_as_INERT_not_VERIFIED_alone(self):
        """THE CORE REGRESSION. Bytes correct, registration absent. Before T-2261 this row
        read `deployed=<md5> VERIFIED` and nothing else — which is what a hook that will
        never run looked like."""
        text, failures = self._row(
            md5=CANON_MD5,
            settings=_settings([f"{FOREIGN_HOME}/pantheon-config/tools/hooks/ensure-hooks-active.sh"]))
        self.assertIn("VERIFIED", text, "the bytes axis is unchanged and still says VERIFIED")
        self.assertIn(dh.STATUS_INERT, text, "the activation axis must say INERT")
        self.assertIn("NOT confirmed ACTIVE", text, "and must be summarised, not just a column")
        self.assertEqual(failures, 0, "INERT is visible and NON-FATAL, per the ruling")

    def test_ACTIVE_deploy_is_reported_as_ACTIVE_with_no_warning(self):
        text, failures = self._row(md5=CANON_MD5,
                                   settings=_settings([f"{FOREIGN_HOME}/.claude/hooks/h.sh"]))
        self.assertIn("VERIFIED", text)
        self.assertIn(dh.STATUS_ACTIVE, text)
        self.assertNotIn(dh.STATUS_INERT, text)
        self.assertNotIn("NOT confirmed ACTIVE", text)
        self.assertEqual(failures, 0)

    def test_unreadable_config_reports_UNKNOWN_and_warns(self):
        text, failures = self._row(md5=CANON_MD5, settings=dh._SETTINGS_UNREADABLE)
        self.assertIn(dh.STATUS_UNKNOWN, text)
        self.assertIn("NOT confirmed ACTIVE", text)
        self.assertEqual(failures, 0)

    def test_pull_path_is_checked_for_activation_too(self):
        """A symlinked deployment can be just as inert as a copied one."""
        text, _ = self._row(md5=CANON_MD5, settings=_settings(["/bin/true"]),
                            action=dh.ACTION_PULL)
        self.assertIn(dh.STATUS_INERT, text)

    def test_dry_run_reports_registration_without_deploying(self):
        """Read-only, so it runs in dry-run: the operator learns the hook is unregistered
        BEFORE spending the deploy, rather than being told VERIFIED afterwards."""
        stub = FleetStub(settings={"claude": _settings(["/bin/true"])})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            dh.execute("h.sh", [dh.Step("claude", dh.ACTION_COPY, "r")],
                       dry_run=True, run=stub)
        self.assertIn("registration: " + dh.STATUS_INERT, out.getvalue())
        self.assertEqual([s for s in stub.shells() if " cp " in s or "git pull" in s], [],
                         "dry-run must still not copy or pull")


class TestT2261SummaryNoteClaimsOnlyWhatItMeasured(unittest.TestCase):
    """REVIEW FIX 1. The end-of-run NOTE used to open "correct bytes on disk" and printed
    UNCONDITIONALLY — so a row that had just FAILED its md5 check was followed by a summary
    asserting its bytes were correct. That is this ticket's own defect shape (a word reading
    as a claim about something it did not measure) reproduced inside the fix for it."""

    def _run_rows(self, rows):
        """rows: {account: (md5, settings)} -> printed output."""
        stub = FleetStub(md5={a: m for a, (m, _) in rows.items()},
                         settings={a: s for a, (_, s) in rows.items()})
        with contextlib.redirect_stdout(io.StringIO()) as out:
            dh.execute("h.sh", [dh.Step(a, dh.ACTION_COPY, "r") for a in rows],
                       dry_run=False, stage="/tmp/s", run=stub)
        return out.getvalue()

    def test_note_does_not_assert_byte_correctness_under_a_MISMATCH(self):
        """THE REPRODUCED CASE: wrong bytes AND unreadable config."""
        text = self._run_rows({"claude": ("0" * 32, dh._SETTINGS_UNREADABLE)})
        self.assertIn("MISMATCH", text)
        self.assertIn("NOT confirmed ACTIVE", text, "the activation summary must still print")
        self.assertNotIn("correct bytes on disk", text,
                         "the summary must not claim correct bytes for a row that MISMATCHed")

    def test_note_never_claims_byte_correctness_in_any_mix(self):
        """PROPERTY: the axes mix freely across accounts, which is why this was reworded
        rather than made conditional — no single summary line can speak for all rows."""
        good = _settings(["/home/x/.claude/hooks/h.sh"])
        inert = _settings(["/bin/true"])
        for name, rows in {
            "all mismatched": {"a": ("0" * 32, inert)},
            "all verified":   {"a": (CANON_MD5, inert)},
            "mixed axes":     {"a": (CANON_MD5, inert), "b": ("0" * 32, dh._SETTINGS_UNREADABLE)},
            "verified+active beside mismatched": {"a": (CANON_MD5, good), "b": ("0" * 32, inert)},
        }.items():
            with self.subTest(mix=name):
                text = self._run_rows(rows)
                self.assertNotIn("correct bytes on disk", text)

    def test_note_still_says_what_it_did_measure(self):
        """Reworded, not deleted — the activation finding must survive the correction."""
        text = self._run_rows({"claude": (CANON_MD5, _settings(["/bin/true"]))})
        self.assertIn("NOT confirmed ACTIVE", text)
        self.assertIn("ACTIVATION axis ONLY", text)
        self.assertIn("SessionStart", text)


class TestT2261DocumentedShapesAreActuallyPinned(unittest.TestCase):
    """REVIEW FIX 2. Three branches whose docstrings claim to handle a case that no test
    exercised. Behaviour was confirmed correct by the reviewer and independently by me
    before these were written — so this is a COVERAGE absence being closed, not a defect
    being fixed. A documented branch that no test reaches is a claim, not a mechanism."""

    HOOK = "handoff-auto-read.sh"
    ACCT = "suborch2"
    DEPLOYED = "/home/suborch2/.claude/hooks/handoff-auto-read.sh"

    def test_group_with_a_TOP_LEVEL_command_key_is_read(self):
        """_session_start_commands' docstring names three wild shapes — matcher-group with
        an inner `hooks` list, a group with a top-level `command`, and a bare string. Only
        two were covered; this pins the middle one."""
        self.assertEqual(dh._session_start_commands([{"command": self.DEPLOYED}]),
                         [self.DEPLOYED])
        status, _ = dh.parse_registration(
            json.dumps({"hooks": {"SessionStart": [{"command": self.DEPLOYED}]}}),
            self.HOOK, self.ACCT)
        self.assertEqual(status, dh.STATUS_ACTIVE)

    def test_bare_string_entry_is_read(self):
        """The third documented shape, pinned alongside it."""
        self.assertEqual(dh._session_start_commands([self.DEPLOYED]), [self.DEPLOYED])

    def test_unbalanced_quotes_fall_back_instead_of_raising(self):
        """_hook_command_tokens' ValueError fallback. shlex.split RAISES on an unbalanced
        quote; without the fallback a single malformed command string would take down the
        whole registration read for that account — an exception where the design promises a
        status. Pins that the fallback is reached AND that it still yields tokens."""
        bad = f'bash "{self.DEPLOYED}'          # deliberately unbalanced
        with self.assertRaises(ValueError):
            shlex.split(bad)                    # control: this input really does raise
        tokens = dh._hook_command_tokens(bad)
        self.assertEqual(tokens, ["bash", f'"{self.DEPLOYED}'])
        # End-to-end: a status comes back rather than an exception escaping.
        status, _ = dh.parse_registration(_settings([bad]), self.HOOK, self.ACCT)
        # MEASURED, and deliberately asserted as measured rather than as preferred: the
        # quote is stripped by _expand_home, so this reads ACTIVE. The config does genuinely
        # name the deployed path — this is the same documented precision limit as a command
        # that merely MENTIONS the path, recorded by the reviewer as a known limit and
        # explicitly out of scope to change here. Pinned so a future change to the predicate
        # has to face it deliberately.
        self.assertEqual(status, dh.STATUS_ACTIVE)

    def test_bare_tilde_token_expands_to_the_accounts_home(self):
        """_expand_home's `t == "~"` branch. Distinct from the `~/...` branch: a bare tilde
        has no trailing slash to preserve, and without this branch it would fall through
        unexpanded and compare as the literal string '~'."""
        self.assertEqual(dh._expand_home("~", "/home/suborch2"), "/home/suborch2")
        self.assertEqual(dh._expand_home("~/x.sh", "/home/suborch2"), "/home/suborch2/x.sh")
        self.assertEqual(dh._expand_home("'~'", "/home/suborch2"), "/home/suborch2")

    def test_none_of_the_three_shapes_can_manufacture_a_false_ACTIVE(self):
        """ADVERSARIAL: the invariant these branches must not breach. Each shape, pointed at
        something that is NOT the deployed path, must still refuse to say ACTIVE."""
        other = "/home/suborch2/.claude/hooks/something-else.sh"
        for label, body in {
            "top-level command": json.dumps({"hooks": {"SessionStart": [{"command": other}]}}),
            "bare string": json.dumps({"hooks": {"SessionStart": [other]}}),
            "unbalanced quotes": _settings([f'bash "{other}']),
            "bare tilde": _settings(["~"]),
        }.items():
            with self.subTest(shape=label):
                self.assertNotEqual(
                    dh.parse_registration(body, self.HOOK, self.ACCT)[0], dh.STATUS_ACTIVE)


class TestT2261PlanWording(unittest.TestCase):
    def test_copy_reason_names_the_command_that_will_actually_run(self):
        """The plan line used to read 'cp is correct' for an account whose parent dir did
        not exist, where a bare cp was in fact wrong. The probe stays three-state (see the
        comment in plan_step); the wording is what changed."""
        step = dh.plan_step("suborch2", Path("/home/suborch2/.claude/hooks/h.sh"),
                            is_symlink=False)
        self.assertEqual(step.action, dh.ACTION_COPY)
        self.assertIn("mkdir -p", step.reason)
        self.assertIn("never received a hook", step.reason)


# ---------------------------------------------------------------- T-2208
class PasteReadyIdentifiersTest(unittest.TestCase):
    """Two consecutive deploy reports cited shas that resolve to NOTHING while the
    prose around them was correct, and one cited an md5 matching no file anywhere.
    The substance was reliable; the identifiers were not — and identifiers are the
    part a reader uses to VERIFY, so a report whose sha does not resolve is a claim
    rather than evidence. A sha and an md5 cannot be recalled; they must be
    retrieved, so the tool that has them prints them."""

    def _capture(self, rows, **kw):
        import io, contextlib
        def run(cmd, **kwargs):
            out = {"HEAD": "a" * 40, "origin/main": "b" * 40}.get(
                cmd[-1], "c" * 40)
            return types.SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dh._print_report_block("h.sh", rows, run=run)
        return buf.getvalue()

    def test_emits_full_md5_not_the_truncated_display_form(self):
        """The 8-char form printed in the rows cannot be checked against anything —
        that is half of why the reports it appeared in were uncheckable."""
        full = "0123456789abcdef0123456789abcdef"
        out = self._capture([("acct", dh.ACTION_COPY, full, dh.STATUS_ACTIVE)])
        self.assertIn(full, out, "the block must carry the FULL 32-char md5")
        self.assertNotRegex(out, r"deployed_md5=[0-9a-f]{8}\b",
                            "a truncated md5 in the paste block would reproduce "
                            "the defect this closes")

    def test_emits_head_and_origin_main_and_the_blob(self):
        out = self._capture([])
        self.assertIn("a" * 40, out, "HEAD must be captured, not recalled")
        self.assertIn("b" * 40, out, "origin/main must be captured")
        self.assertIn("canonical blob", out)

    def test_every_account_appears_with_its_own_identifiers(self):
        out = self._capture([
            ("one", dh.ACTION_COPY, "1" * 32, dh.STATUS_ACTIVE),
            ("two", dh.ACTION_PULL, "2" * 32, dh.STATUS_INERT),
        ])
        for acct, md5 in (("one", "1" * 32), ("two", "2" * 32)):
            self.assertIn(acct, out)
            self.assertIn(md5, out)

    def test_a_missing_md5_is_shown_as_none_not_omitted(self):
        """An absent datum must be visible. Silently dropping the field would let a
        report be assembled with a gap the reader cannot see."""
        out = self._capture([("acct", dh.ACTION_REFUSE, "", dh.STATUS_UNKNOWN)])
        self.assertIn("<none>", out)

    def test_dry_run_prints_no_block(self):
        """Nothing was deployed, so there are no identifiers to report — printing
        them anyway would invite pasting a block describing a deploy that did not
        happen."""
        import io, contextlib
        stub = FleetStub(md5={FOREIGN: CANON_MD5})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            dh.execute("h.sh", [dh.Step(FOREIGN, dh.ACTION_COPY, "r")],
                       dry_run=True, run=stub)
        self.assertNotIn("PASTE-READY", buf.getvalue())


class ProbeDiagnosisT2317(unittest.TestCase):
    """A probe that could not RUN must not report a conclusion about the thing it
    was going to probe.

    Raised by orchestrator from the claude seat: it hit REFUSE on both peers reading
    "could NOT determine whether the deployed path is a symlink". True, and it
    MISDIRECTS — it reads as a statement about the deployed FILE, so the operator
    inspects the file. Nothing was wrong with the file. `sudo -n` from a seat
    without NOPASSWD returns "a password is required", so the probe never ran:
    the file's state was never in question because the question was never asked.

    Verdict stays fail-closed in both cases; only the DIAGNOSIS is split.
    """

    def _run(self, out="", err="", rc=0):
        class R:
            stdout = out
            stderr = err
            returncode = rc
        return lambda *a, **k: R

    def test_sudo_password_required_is_UNREACHABLE_not_unparseable(self):
        val, why = dh.probe_symlink_state(
            "claude-fallback", "/p",
            run=self._run(err="sudo: a password is required", rc=1))
        self.assertIsNone(val)
        self.assertEqual(why, dh.PROBE_UNREACHABLE)

    def test_not_allowed_to_execute_is_UNREACHABLE(self):
        val, why = dh.probe_symlink_state(
            "claude", "/p",
            run=self._run(err="sudo: user x is not allowed to execute", rc=1))
        self.assertIsNone(val)
        self.assertEqual(why, dh.PROBE_UNREACHABLE)

    def test_probe_RAN_but_said_something_odd_is_UNPARSEABLE(self):
        """The control for the pair. Without a case that still lands on
        UNPARSEABLE, the split could be a blanket rename of every no-answer to
        UNREACHABLE and every test above would look identical."""
        val, why = dh.probe_symlink_state(
            "claude", "/p", run=self._run(out="MAYBE?\n", rc=0))
        self.assertIsNone(val)
        self.assertEqual(why, dh.PROBE_UNPARSEABLE)

    def test_answers_still_classify_normally(self):
        self.assertEqual(dh.probe_symlink_state("c", "/p", run=self._run("SYMLINK\n")),
                         (True, dh.PROBE_SYMLINK))
        self.assertEqual(dh.probe_symlink_state("c", "/p", run=self._run("REGULAR\n")),
                         (False, dh.PROBE_REGULAR))
        self.assertEqual(dh.probe_symlink_state("c", "/p", run=self._run("ABSENT\n")),
                         (False, dh.PROBE_REGULAR))

    def test_probe_is_symlink_keeps_its_old_three_state_contract(self):
        """Existing callers and tests must not have to care about the split."""
        self.assertIs(dh.probe_is_symlink("c", "/p", run=self._run("SYMLINK\n")), True)
        self.assertIs(dh.probe_is_symlink("c", "/p", run=self._run("REGULAR\n")), False)
        self.assertIsNone(dh.probe_is_symlink(
            "c", "/p", run=self._run(err="sudo: a password is required", rc=1)))


class RefuseMessageNamesTheRealFailureT2317(unittest.TestCase):
    """Both no-answer cases REFUSE. They must not say the same thing — the
    remedies differ: fix your credentials, versus inspect the file."""

    def test_unreachable_refuse_blames_CREDENTIALS_not_the_file(self):
        step = dh.plan_step("claude-fallback", pathlib.Path("/p"),
                            is_symlink=None, why=dh.PROBE_UNREACHABLE)
        self.assertEqual(step.action, dh.ACTION_REFUSE)
        self.assertIn("could not REACH", step.reason)
        self.assertIn("never ran", step.reason)
        self.assertIn("CREDENTIALS", step.reason)
        self.assertIn("claude-fallback", step.reason)
        # The exact misdirection being removed: it must NOT assert that the
        # file's symlink-ness is what could not be determined.
        self.assertNotIn("could NOT determine whether the deployed path", step.reason)

    def test_unparseable_refuse_still_points_at_the_FILE(self):
        step = dh.plan_step("claude", pathlib.Path("/p"),
                            is_symlink=None, why=dh.PROBE_UNPARSEABLE)
        self.assertEqual(step.action, dh.ACTION_REFUSE)
        self.assertIn("RAN but", step.reason)
        self.assertIn("Inspect the deployed path", step.reason)

    def test_both_no_answer_cases_still_FAIL_CLOSED(self):
        """The split is a wording fix. Neither case may become a deploy."""
        for why in (dh.PROBE_UNREACHABLE, dh.PROBE_UNPARSEABLE, None):
            with self.subTest(why=why):
                self.assertEqual(
                    dh.plan_step("a", pathlib.Path("/p"), is_symlink=None,
                                 why=why).action,
                    dh.ACTION_REFUSE)

    def test_real_answers_are_unaffected(self):
        self.assertEqual(dh.plan_step("a", pathlib.Path("/p"), is_symlink=True).action,
                         dh.ACTION_PULL)
        self.assertEqual(dh.plan_step("a", pathlib.Path("/p"), is_symlink=False).action,
                         dh.ACTION_COPY)


if __name__ == "__main__":
    unittest.main()
