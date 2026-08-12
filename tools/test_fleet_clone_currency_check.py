"""Tests for fleet_clone_currency_check.py (T-2798, T-2794 option-b).

Read-only per-seat drift check: seat-discovery derivation, behind-count against
REAL fixture git repos (bare "origin" + a clone, real commits/push — not a
mock, since the git plumbing itself is the thing worth proving), the
threshold/alert decision with a mocked mailbox, and the no-mutation guarantee
(no argv aimed at a seat clone is ever anything but `rev-parse HEAD`).
"""
from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.fleet_clone_currency_check as fccc


def _git(*args, cwd):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


def _init_repo_pair(tmp: Path) -> tuple[Path, Path, list[str]]:
    """Build a real bare 'origin' + a clone with 5 commits pushed to main.
    Returns (bare_path, work_path, [c1..c5] shas oldest-first)."""
    bare = tmp / "origin.git"
    work = tmp / "work"
    _git("init", "--bare", "--initial-branch=main", str(bare), cwd=tmp)
    _git("clone", str(bare), str(work), cwd=tmp)
    _git("config", "user.email", "test@example.invalid", cwd=work)
    _git("config", "user.name", "Test", cwd=work)
    shas = []
    for i in range(5):
        (work / "f.txt").write_text(f"rev {i}\n")
        _git("add", "f.txt", cwd=work)
        _git("commit", "-m", f"commit {i}", cwd=work)
        shas.append(_git("rev-parse", "HEAD", cwd=work))
    _git("push", "origin", "main", cwd=work)
    return bare, work, shas


class SeatDiscovery(unittest.TestCase):
    def test_derives_from_fleet_accounts_not_hardcoded(self):
        with mock.patch.object(fccc.arm, "fleet_accounts", return_value=("claude", "suborch1")), \
             mock.patch.object(fccc.arm, "account_persona",
                                side_effect=lambda a: {"claude": "orchestrator", "suborch1": "metis"}[a]):
            seats = fccc.discover_seats()
        self.assertEqual(
            seats,
            [
                fccc.Seat(persona="orchestrator", account="claude", clone_path="/home/claude/pantheon-config"),
                fccc.Seat(persona="metis", account="suborch1", clone_path="/home/suborch1/pantheon-config"),
            ],
        )

    def test_unknown_persona_falls_back_to_account_name(self):
        # account_persona() returning None (an unmapped/edge-case account) must
        # not crash discovery -- falls back to the account name as the label.
        with mock.patch.object(fccc.arm, "fleet_accounts", return_value=("ghost",)), \
             mock.patch.object(fccc.arm, "account_persona", return_value=None):
            seats = fccc.discover_seats()
        self.assertEqual(seats[0].persona, "ghost")


class BehindCountFixtureRepos(unittest.TestCase):
    """Real git plumbing -- no mocked subprocess here."""

    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_ctx.name)
        self.addCleanup(self._tmp_ctx.cleanup)

    def test_behind_count_against_real_repo(self):
        bare, work, shas = _init_repo_pair(self.tmp)
        verdict, count = fccc._behind_count(shas[1], repo_root=work)
        self.assertEqual(verdict, "")
        self.assertEqual(count, 3)  # commits 2,3,4 (0-indexed shas[2:5])

    def test_behind_count_zero_when_current(self):
        bare, work, shas = _init_repo_pair(self.tmp)
        verdict, count = fccc._behind_count(shas[4], repo_root=work)
        self.assertEqual((verdict, count), ("", 0))

    def test_behind_count_unknown_sha_is_UNKNOWN(self):
        bare, work, shas = _init_repo_pair(self.tmp)
        verdict, count = fccc._behind_count("f" * 40, repo_root=work)
        self.assertEqual(verdict, fccc.UNKNOWN)
        self.assertIsNone(count)

    def test_fetch_picks_up_new_remote_commit(self):
        bare, work, shas = _init_repo_pair(self.tmp)
        # A second clone pushes one more commit to origin.
        other = self.tmp / "other"
        _git("clone", str(bare), str(other), cwd=self.tmp)
        _git("config", "user.email", "test@example.invalid", cwd=other)
        _git("config", "user.name", "Test", cwd=other)
        (other / "g.txt").write_text("new\n")
        _git("add", "g.txt", cwd=other)
        _git("commit", "-m", "extra", cwd=other)
        _git("push", "origin", "main", cwd=other)

        # work's origin/main is stale until fetched.
        verdict, count = fccc._behind_count(shas[4], repo_root=work)
        self.assertEqual((verdict, count), ("", 0))
        ok = fccc._fetch_origin_main(repo_root=work)
        self.assertTrue(ok)
        verdict, count = fccc._behind_count(shas[4], repo_root=work)
        self.assertEqual((verdict, count), ("", 1))


class SeatHeadResolution(unittest.TestCase):
    def _seat(self, account="suborch1"):
        return fccc.Seat(persona="metis", account=account, clone_path=f"/home/{account}/pantheon-config")

    def test_success_local_account_no_sudo(self):
        seat = self._seat(account="claude")
        fake = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout="a" * 40 + "\n", stderr=""))
        with mock.patch.object(fccc.getpass, "getuser", return_value="claude"):
            verdict, head = fccc._seat_head(seat, run=fake)
        self.assertEqual((verdict, head), ("", "a" * 40))
        argv = fake.call_args[0][0]
        self.assertNotIn("sudo", argv)
        self.assertEqual(argv, ["git", "-C", seat.clone_path, "rev-parse", "HEAD"])

    def test_success_remote_account_uses_sudo_with_stdin_password(self):
        seat = self._seat(account="suborch1")
        fake = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout="b" * 40 + "\n", stderr=""))
        with mock.patch.object(fccc.getpass, "getuser", return_value="claude"), \
             mock.patch.object(fccc, "_read_sudo_password", return_value="hunter2"):
            verdict, head = fccc._seat_head(seat, run=fake)
        self.assertEqual((verdict, head), ("", "b" * 40))
        argv, kwargs = fake.call_args[0][0], fake.call_args[1]
        self.assertEqual(argv, ["sudo", "-S", "-k", "-u", "suborch1", "git", "-C", seat.clone_path,
                                 "rev-parse", "HEAD"])
        # Password travels via stdin (`input=`), never embedded in argv.
        self.assertNotIn("hunter2", argv)
        self.assertIn("hunter2", kwargs.get("input", ""))

    def test_no_such_directory_is_NO_CLONE(self):
        seat = self._seat(account="exocortex")
        fake = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 128, stdout="", stderr="fatal: cannot change to '/home/exocortex/pantheon-config': "
                                        "No such file or directory\n"))
        with mock.patch.object(fccc.getpass, "getuser", return_value="claude"), \
             mock.patch.object(fccc, "_read_sudo_password", return_value="pw"):
            verdict, head = fccc._seat_head(seat, run=fake)
        self.assertEqual(verdict, fccc.NO_CLONE)
        self.assertIsNone(head)

    def test_not_a_git_repository_is_NO_CLONE(self):
        seat = self._seat(account="the-ancestor")
        fake = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 128, stdout="", stderr="fatal: not a git repository (or any of the parent directories): .git\n"))
        with mock.patch.object(fccc.getpass, "getuser", return_value="claude"), \
             mock.patch.object(fccc, "_read_sudo_password", return_value="pw"):
            verdict, head = fccc._seat_head(seat, run=fake)
        self.assertEqual(verdict, fccc.NO_CLONE)

    def test_bad_sudo_password_is_UNKNOWN(self):
        seat = self._seat(account="suborch1")
        fake = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 1, stdout="", stderr="sudo: 1 incorrect password attempt\n"))
        with mock.patch.object(fccc.getpass, "getuser", return_value="claude"), \
             mock.patch.object(fccc, "_read_sudo_password", return_value="wrong"):
            verdict, head = fccc._seat_head(seat, run=fake)
        self.assertEqual(verdict, fccc.UNKNOWN)

    def test_timeout_is_UNKNOWN(self):
        seat = self._seat(account="suborch1")
        fake = mock.Mock(side_effect=subprocess.TimeoutExpired(cmd="git", timeout=20))
        with mock.patch.object(fccc.getpass, "getuser", return_value="claude"), \
             mock.patch.object(fccc, "_read_sudo_password", return_value="pw"):
            verdict, head = fccc._seat_head(seat, run=fake)
        self.assertEqual(verdict, fccc.UNKNOWN)

    def test_password_never_appears_in_a_raised_exception_message(self):
        """Prove the invariant, don't just restate OSError->UNKNOWN (that shape is
        already covered by test_timeout_is_UNKNOWN). `boom` simulates a worst-case
        leak AT THE SUBPROCESS BOUNDARY -- its exception message is built from the
        FULL kwargs _seat_head passed it, so it CONTAINS the piped password
        (kwargs["input"]) -- and this asserts the password is absent from every
        observable output of _seat_head: the returned tuple, and anything written
        to stdout/stderr during the call. The trailing mutation self-check proves
        this test bed would actually go red if _seat_head ever folded the raw
        exception text into its output -- without it, a passing test here would be
        unfalsifiable (it could pass for a reason unrelated to the invariant)."""
        seat = self._seat(account="suborch1")
        password = "hunter2"  # a recognizable sentinel, not the real root.env content

        def boom(argv, **kwargs):
            # Worst case: the subprocess layer's own error text embeds every kwarg
            # it was called with, including input= (the piped password).
            raise OSError(f"exec failed: argv={argv} kwargs={kwargs}")

        with mock.patch.object(fccc.getpass, "getuser", return_value="claude"), \
             mock.patch.object(fccc, "_read_sudo_password", return_value=password):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                verdict, head = fccc._seat_head(seat, run=boom)

        self.assertEqual(verdict, fccc.UNKNOWN)
        self.assertIsNone(head)
        self.assertNotIn(password, repr((verdict, head)))
        self.assertNotIn(password, out.getvalue())
        self.assertNotIn(password, err.getvalue())

        # Mutation self-check: a _seat_head that DID fold the caught exception's
        # text into its return value would leak the password through `detail` at
        # the check_seat()/alert() layer -- confirm this exact assertion shape
        # would catch that bug, so a passing test above is actually meaningful.
        def leaky_seat_head(seat, *, run):
            try:
                run(["sudo", "-S", "-k", "-u", seat.account, "git"], input=password + "\n")
            except OSError as exc:  # the bug this test guards against
                return f"{fccc.UNKNOWN}: {exc}", None
            return "", None

        leaky_verdict, _ = leaky_seat_head(seat, run=boom)
        self.assertIn(password, leaky_verdict,
                       "mutation self-check failed: the leaky variant should leak "
                       "the password, proving the assertions above have teeth")


class NoMutationGuarantee(unittest.TestCase):
    def test_sweep_never_sends_a_mutating_verb_to_a_seat_clone(self):
        seats = [
            fccc.Seat(persona="orchestrator", account="claude", clone_path="/home/claude/pantheon-config"),
            fccc.Seat(persona="metis", account="suborch1", clone_path="/home/suborch1/pantheon-config"),
            fccc.Seat(persona="atlas", account="suborch2", clone_path="/home/suborch2/pantheon-config"),
        ]
        captured_argvs = []

        def fake_head_run(argv, **kwargs):
            captured_argvs.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="c" * 40 + "\n", stderr="")

        def fake_count_run(argv, **kwargs):
            captured_argvs.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="0\n", stderr="")

        with mock.patch.object(fccc.getpass, "getuser", return_value="claude"), \
             mock.patch.object(fccc, "_read_sudo_password", return_value="pw"):
            fccc.sweep(seats, threshold=20, head_run=fake_head_run, count_run=fake_count_run)

        mutating = {"pull", "fetch", "merge", "reset", "checkout", "clone", "push"}
        for argv in captured_argvs:
            if "-C" in argv:
                idx = argv.index("-C")
                path = argv[idx + 1]
                if path in {s.clone_path for s in seats if s.account != "claude"}:
                    self.assertEqual(argv[-2:], ["rev-parse", "HEAD"],
                                      f"non-rev-parse command reached a seat clone: {argv}")
            self.assertFalse(mutating & set(argv), f"mutating verb in argv reaching git: {argv}")


class ThresholdAlertDecision(unittest.TestCase):
    def test_at_threshold_is_current_not_flagged(self):
        results = [{"persona": "specialist", "account": "claude-research", "verdict": fccc.CURRENT,
                    "detail": "", "behind": 20}]
        self.assertEqual(fccc.needs_alert(results), [])

    def test_over_threshold_is_drifted_and_flagged(self):
        r = {"persona": "specialist", "account": "claude-research", "verdict": fccc.DRIFTED,
             "detail": "", "behind": 21}
        self.assertEqual(fccc.needs_alert([r]), [r])

    def test_no_clone_never_flagged(self):
        r = {"persona": "mnemosyne", "account": "exocortex", "verdict": fccc.NO_CLONE, "detail": ""}
        self.assertEqual(fccc.needs_alert([r]), [])

    def test_unknown_always_flagged(self):
        r = {"persona": "metis", "account": "suborch1", "verdict": fccc.UNKNOWN, "detail": "sudo failed"}
        self.assertEqual(fccc.needs_alert([r]), [r])

    def test_check_seat_boundary_exactly_at_threshold_is_current(self):
        def head_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="d" * 40 + "\n", stderr="")

        def count_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="20\n", stderr="")

        seat = fccc.Seat(persona="orchestrator", account="claude", clone_path="/home/claude/pantheon-config")
        with mock.patch.object(fccc.getpass, "getuser", return_value="claude"):
            r = fccc.check_seat(seat, threshold=20, head_run=head_run, count_run=count_run)
        self.assertEqual(r["verdict"], fccc.CURRENT)

    def test_check_seat_boundary_one_over_threshold_is_drifted(self):
        def head_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="d" * 40 + "\n", stderr="")

        def count_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="21\n", stderr="")

        seat = fccc.Seat(persona="orchestrator", account="claude", clone_path="/home/claude/pantheon-config")
        with mock.patch.object(fccc.getpass, "getuser", return_value="claude"):
            r = fccc.check_seat(seat, threshold=20, head_run=head_run, count_run=count_run)
        self.assertEqual(r["verdict"], fccc.DRIFTED)


class AlertMailboxMock(unittest.TestCase):
    def test_alert_sends_expected_shape(self):
        flagged = [
            {"persona": "metis", "account": "suborch1", "verdict": fccc.DRIFTED,
             "detail": "HEAD=abc1234567, 332 commit(s) behind origin/main"},
            {"persona": "codex", "account": "codex", "verdict": fccc.UNKNOWN,
             "detail": "could not read HEAD (unknown)"},
        ]
        with mock.patch("tools.mailbox.send") as msend:
            fccc.alert(flagged)
        msend.assert_called_once()
        args, kwargs = msend.call_args
        self.assertEqual(args[0], f"{fccc.arm.mailbox_hub()}/orchestrator")
        self.assertIn("metis", args[1])
        self.assertIn("332", args[1])
        self.assertIn("codex", args[1])
        self.assertEqual(args[2], "fleet_clone_currency_check")
        self.assertIn("2", kwargs.get("subject", ""))


class MainDryRunAndModeGate(unittest.TestCase):
    def test_flagless_run_refuses(self):
        rc = fccc.main([])
        self.assertEqual(rc, 64)

    def test_dry_run_never_alerts(self):
        flagged = [{"persona": "metis", "account": "suborch1", "verdict": fccc.DRIFTED, "detail": "x"}]
        with mock.patch.object(fccc, "discover_seats", return_value=[]), \
             mock.patch.object(fccc, "_fetch_origin_main", return_value=True), \
             mock.patch.object(fccc, "sweep", return_value=flagged), \
             mock.patch.object(fccc, "needs_alert", return_value=flagged), \
             mock.patch.object(fccc, "alert") as malert:
            rc = fccc.main(["--dry-run"])
        self.assertEqual(rc, 0)
        malert.assert_not_called()

    def test_check_alerts_when_flagged(self):
        flagged = [{"persona": "metis", "account": "suborch1", "verdict": fccc.DRIFTED, "detail": "x"}]
        with mock.patch.object(fccc, "discover_seats", return_value=[]), \
             mock.patch.object(fccc, "_fetch_origin_main", return_value=True), \
             mock.patch.object(fccc, "sweep", return_value=flagged), \
             mock.patch.object(fccc, "needs_alert", return_value=flagged), \
             mock.patch.object(fccc, "alert") as malert:
            rc = fccc.main(["--check"])
        self.assertEqual(rc, 0)
        malert.assert_called_once_with(flagged)

    def test_check_no_alert_when_clean(self):
        with mock.patch.object(fccc, "discover_seats", return_value=[]), \
             mock.patch.object(fccc, "_fetch_origin_main", return_value=True), \
             mock.patch.object(fccc, "sweep", return_value=[]), \
             mock.patch.object(fccc, "needs_alert", return_value=[]), \
             mock.patch.object(fccc, "alert") as malert:
            rc = fccc.main(["--check"])
        self.assertEqual(rc, 0)
        malert.assert_not_called()

    def test_fetch_failure_sends_distinct_alert_and_skips_sweep(self):
        with mock.patch.object(fccc, "discover_seats", return_value=[]), \
             mock.patch.object(fccc, "_fetch_origin_main", return_value=False), \
             mock.patch.object(fccc, "sweep") as msweep, \
             mock.patch("tools.mailbox.send") as msend:
            rc = fccc.main(["--check"])
        self.assertEqual(rc, 0)
        msweep.assert_not_called()
        msend.assert_called_once()
        self.assertIn("fetch", msend.call_args[0][1].lower())


if __name__ == "__main__":
    unittest.main()
