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
import tools.seat_exec as se  # getuser lives here since the T-4066 seat_argv retrofit


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
                                side_effect=lambda a: {"claude": "orchestrator", "suborch1": "mason"}[a]):
            seats = fccc.discover_seats()
        self.assertEqual(
            seats,
            [
                fccc.Seat(persona="orchestrator", account="claude", clone_path="/home/claude/pantheon-config"),
                fccc.Seat(persona="mason", account="suborch1", clone_path="/home/suborch1/pantheon-config"),
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

    def test_behind_count_unresolvable_sha_is_PENDING_PUSH(self):
        """A SHA that the runner clone has never fetched (e.g., a seat's local
        unpushed commit) is classified PENDING_PUSH, not UNKNOWN — Recovery
        diagnosis 2026-09-14, sibling to T-3709: rev-list fails with
        'unknown revision' / 'not a valid object name' stderr and we want
        that failure class disambiguated from every-other-rev-list-error
        (which stays UNKNOWN)."""
        bare, work, shas = _init_repo_pair(self.tmp)
        verdict, count = fccc._behind_count("f" * 40, repo_root=work)
        self.assertEqual(verdict, fccc.PENDING_PUSH)
        self.assertIsNone(count)

    def test_behind_count_missing_compare_ref_is_UNKNOWN_not_PENDING_PUSH(self):
        """F2 (T-4061 rebound): a rev-list revision-range error where the COMPARE
        ref origin/<branch> is MISSING (e.g. _resolve_compare_branch fell back to
        a wrong 'main') must classify UNKNOWN (alerted), NOT PENDING_PUSH (which
        is never alerted). Disambiguation is by verifying origin/<branch> exists:
        here the seat HEAD (shas[4]) is a real, resolvable sha, but the branch
        'no-such-branch' has no origin ref, so the range errors and the verify
        fails -> UNKNOWN. Contrast test_behind_count_unresolvable_sha_is_PENDING_PUSH
        where origin/main DOES exist and the sha is the culprit."""
        bare, work, shas = _init_repo_pair(self.tmp)
        verdict, count = fccc._behind_count(shas[4], repo_root=work, branch="no-such-branch")
        self.assertEqual(verdict, fccc.UNKNOWN)
        self.assertIsNone(count)

    def test_behind_count_rev_list_oserror_is_UNKNOWN(self):
        """UNKNOWN is preserved for the every-other-failure case (subprocess
        OSError, timeout, non-int output) — PENDING_PUSH is specifically the
        unresolvable-SHA-in-runner-clone stderr shape, nothing broader."""
        def raising_run(*args, **kwargs):
            raise OSError("simulated exec failure")
        verdict, count = fccc._behind_count("f" * 40, run=raising_run)
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
        return fccc.Seat(persona="mason", account=account, clone_path=f"/home/{account}/pantheon-config")

    def test_success_local_account_no_sudo(self):
        seat = self._seat(account="claude")
        fake = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout="a" * 40 + "\n", stderr=""))
        with mock.patch.object(se.getpass, "getuser", return_value="claude"):
            verdict, head = fccc._seat_head(seat, run=fake)
        self.assertEqual((verdict, head), ("", "a" * 40))
        argv = fake.call_args[0][0]
        self.assertNotIn("sudo", argv)
        self.assertEqual(argv, ["git", "-C", seat.clone_path, "rev-parse", "HEAD", "--abbrev-ref", "HEAD"])

    def test_success_remote_account_uses_sudo_with_stdin_password(self):
        seat = self._seat(account="suborch1")
        fake = mock.Mock(return_value=subprocess.CompletedProcess([], 0, stdout="b" * 40 + "\n", stderr=""))
        with mock.patch.object(se.getpass, "getuser", return_value="claude"), \
             mock.patch.object(fccc, "_read_sudo_password", return_value="hunter2"):
            verdict, head = fccc._seat_head(seat, run=fake)
        self.assertEqual((verdict, head), ("", "b" * 40))
        argv, kwargs = fake.call_args[0][0], fake.call_args[1]
        self.assertEqual(argv, ["sudo", "-S", "-k", "-u", "suborch1", "git", "-C", seat.clone_path,
                                 "rev-parse", "HEAD", "--abbrev-ref", "HEAD"])
        # Password travels via stdin (`input=`), never embedded in argv.
        self.assertNotIn("hunter2", argv)
        self.assertIn("hunter2", kwargs.get("input", ""))

    def test_no_such_directory_is_NO_CLONE(self):
        seat = self._seat(account="exocortex")
        fake = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 128, stdout="", stderr="fatal: cannot change to '/home/exocortex/pantheon-config': "
                                        "No such file or directory\n"))
        with mock.patch.object(se.getpass, "getuser", return_value="claude"), \
             mock.patch.object(fccc, "_read_sudo_password", return_value="pw"):
            verdict, head = fccc._seat_head(seat, run=fake)
        self.assertEqual(verdict, fccc.NO_CLONE)
        self.assertIsNone(head)

    def test_not_a_git_repository_is_NO_CLONE(self):
        seat = self._seat(account="the-ancestor")
        fake = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 128, stdout="", stderr="fatal: not a git repository (or any of the parent directories): .git\n"))
        with mock.patch.object(se.getpass, "getuser", return_value="claude"), \
             mock.patch.object(fccc, "_read_sudo_password", return_value="pw"):
            verdict, head = fccc._seat_head(seat, run=fake)
        self.assertEqual(verdict, fccc.NO_CLONE)

    def test_bad_sudo_password_is_UNKNOWN(self):
        seat = self._seat(account="suborch1")
        fake = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 1, stdout="", stderr="sudo: 1 incorrect password attempt\n"))
        with mock.patch.object(se.getpass, "getuser", return_value="claude"), \
             mock.patch.object(fccc, "_read_sudo_password", return_value="wrong"):
            verdict, head = fccc._seat_head(seat, run=fake)
        self.assertEqual(verdict, fccc.UNKNOWN)

    def test_timeout_is_UNKNOWN(self):
        seat = self._seat(account="suborch1")
        fake = mock.Mock(side_effect=subprocess.TimeoutExpired(cmd="git", timeout=20))
        with mock.patch.object(se.getpass, "getuser", return_value="claude"), \
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

        with mock.patch.object(se.getpass, "getuser", return_value="claude"), \
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


class EmptyCloneVerdict(unittest.TestCase):
    """T-4116: an UNBORN clone (git-init'd, remote + credentials present, NO
    commits yet) classifies EMPTY_CLONE — a distinct, trivially-fixable state
    carrying a pull remedy — NOT UNKNOWN (which stays for genuine read
    failures). Real git plumbing for the empty fixture (git init + a remote, no
    fetch/commit — deterministic, no network); fakes for the failure-shape
    disambiguation cases."""

    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_ctx.name)
        self.addCleanup(self._tmp_ctx.cleanup)

    def _init_empty_clone(self) -> Path:
        """git init + a remote, NO commits -> an unborn clone (HEAD is a symref
        to an unborn refs/heads/main; `git rev-parse HEAD` fails). No network."""
        clone = self.tmp / "empty"
        _git("init", "--initial-branch=main", str(clone), cwd=self.tmp)
        _git("remote", "add", "origin", "https://example.invalid/x.git", cwd=clone)
        return clone

    def _seat_for(self, clone: Path, account="suborch1"):
        return fccc.Seat(persona="mason", account=account, clone_path=str(clone))

    def test_seat_head_classifies_empty_clone_real_fixture(self):
        # The core case: a real init'd-but-empty repo -> EMPTY_CLONE, not UNKNOWN.
        seat = self._seat_for(self._init_empty_clone())
        with mock.patch.object(se.getpass, "getuser", return_value="suborch1"):
            verdict, head = fccc._seat_head(seat)
        self.assertEqual(verdict, fccc.EMPTY_CLONE)
        self.assertIsNone(head)

    def test_is_unborn_true_for_real_empty_clone(self):
        seat = self._seat_for(self._init_empty_clone())
        with mock.patch.object(se.getpass, "getuser", return_value="suborch1"):
            self.assertTrue(fccc._is_unborn_clone(seat))

    def test_populated_clone_is_not_empty_no_regression(self):
        # A normal populated clone -> rev-parse HEAD succeeds -> "" verdict with
        # the real HEAD, and _is_unborn_clone is False even if probed directly.
        bare, work, shas = _init_repo_pair(self.tmp)
        seat = self._seat_for(work)
        with mock.patch.object(se.getpass, "getuser", return_value="suborch1"):
            verdict, head = fccc._seat_head(seat)
            self.assertEqual(verdict, "")
            self.assertEqual(head, shas[4])
            self.assertFalse(fccc._is_unborn_clone(seat))

    def test_missing_clone_still_no_clone_real_fixture(self):
        # A path that does not exist -> NO_CLONE marker short-circuits BEFORE the
        # empty-clone disambiguation ever runs (regression guard).
        seat = self._seat_for(self.tmp / "does-not-exist")
        with mock.patch.object(se.getpass, "getuser", return_value="suborch1"):
            verdict, head = fccc._seat_head(seat)
        self.assertEqual(verdict, fccc.NO_CLONE)
        self.assertIsNone(head)

    def test_genuine_read_failure_stays_unknown(self):
        # rev-parse HEAD fails with a NON-NO_CLONE error AND symbolic-ref also
        # fails (unreadable/corrupt .git/HEAD, or an auth/sudo failure) -> NOT
        # unborn -> UNKNOWN, never EMPTY_CLONE. This is the P1 invariant: a real
        # read failure must not be misclassified as a fixable empty clone.
        seat = fccc.Seat(persona="mason", account="suborch1",
                         clone_path="/home/suborch1/pantheon-config")

        def fake_run(argv, **kwargs):
            if "symbolic-ref" in argv:
                return subprocess.CompletedProcess(argv, 128, stdout="",
                    stderr="fatal: ref HEAD is not a symbolic ref\n")
            return subprocess.CompletedProcess(argv, 128, stdout="",
                stderr="fatal: unable to read HEAD\n")  # rev-parse HEAD

        with mock.patch.object(se.getpass, "getuser", return_value="claude"), \
             mock.patch.object(fccc, "_read_sudo_password", return_value="pw"):
            verdict, head = fccc._seat_head(seat, run=fake_run)
        self.assertEqual(verdict, fccc.UNKNOWN)
        self.assertIsNone(head)

    def test_corrupt_ref_present_is_unknown_not_empty(self):
        # Defensive P1 guard for a corrupt-but-non-empty repo: symbolic-ref HEAD
        # succeeds (a symref to a branch) but the branch ref DOES resolve
        # (show-ref rc 0) -> not unborn -> UNKNOWN, never EMPTY_CLONE.
        seat = fccc.Seat(persona="mason", account="suborch1",
                         clone_path="/home/suborch1/pantheon-config")

        def fake_run(argv, **kwargs):
            if "symbolic-ref" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="refs/heads/main\n", stderr="")
            if "show-ref" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")  # ref RESOLVES
            return subprocess.CompletedProcess(argv, 128, stdout="",
                stderr="fatal: bad object HEAD\n")  # rev-parse HEAD

        with mock.patch.object(se.getpass, "getuser", return_value="claude"), \
             mock.patch.object(fccc, "_read_sudo_password", return_value="pw"):
            verdict, head = fccc._seat_head(seat, run=fake_run)
        self.assertEqual(verdict, fccc.UNKNOWN)

    def test_check_seat_empty_clone_carries_pull_remedy_with_default_branch(self):
        # End-to-end through check_seat: the EMPTY_CLONE detail carries the
        # actionable pull remedy, substituting the repo's resolved default
        # branch (here 'trunk') for <default>.
        def head_run(argv, **kwargs):
            if "symbolic-ref" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="refs/heads/trunk\n", stderr="")
            if "show-ref" in argv:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")  # unborn
            return subprocess.CompletedProcess(argv, 128, stdout="",
                stderr="fatal: ambiguous argument 'HEAD': unknown revision\n")

        seat = fccc.Seat(persona="mason", account="suborch1",
                         clone_path="/home/suborch1/pantheon-config")
        with mock.patch.object(se.getpass, "getuser", return_value="suborch1"):
            r = fccc.check_seat(seat, repo="pantheon-inventory", threshold=20,
                                runner_repo_clone=Path("/unused"), branch="trunk",
                                head_run=head_run)
        self.assertEqual(r["verdict"], fccc.EMPTY_CLONE)
        self.assertIn("unborn", r["detail"])
        self.assertIn("git pull --ff-only origin trunk", r["detail"])
        self.assertIn("as the seat user", r["detail"])

    def test_empty_clone_is_flagged_distinct_from_unknown(self):
        r = {"persona": "mason", "account": "suborch1", "repo": "pantheon-inventory",
             "verdict": fccc.EMPTY_CLONE,
             "detail": "clone exists but HEAD is unborn (no commits yet) — "
                       "remedy: git pull --ff-only origin main as the seat user"}
        self.assertEqual(fccc.needs_alert([r]), [r])

    def test_render_alert_shows_empty_clone_verdict_and_remedy(self):
        flagged = [{"persona": "mason", "account": "suborch1", "repo": "pantheon-inventory",
                    "verdict": fccc.EMPTY_CLONE,
                    "detail": "clone exists but HEAD is unborn (no commits yet) — "
                              "remedy: git pull --ff-only origin main as the seat user"}]
        out = fccc.render_alert(flagged)
        self.assertIn("EMPTY_CLONE", out)
        self.assertIn("git pull --ff-only origin main", out)
        self.assertIn("mason/suborch1 [pantheon-inventory]", out)


class NoMutationGuarantee(unittest.TestCase):
    def test_sweep_never_sends_a_mutating_verb_to_a_seat_clone(self):
        seats = [
            fccc.Seat(persona="orchestrator", account="claude", clone_path="/home/claude/pantheon-config"),
            fccc.Seat(persona="mason", account="suborch1", clone_path="/home/suborch1/pantheon-config"),
            fccc.Seat(persona="artisan", account="suborch2", clone_path="/home/suborch2/pantheon-config"),
        ]
        captured_argvs = []

        def fake_head_run(argv, **kwargs):
            captured_argvs.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="c" * 40 + "\n", stderr="")

        def fake_count_run(argv, **kwargs):
            captured_argvs.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="0\n", stderr="")

        with mock.patch.object(se.getpass, "getuser", return_value="claude"), \
             mock.patch.object(fccc, "_read_sudo_password", return_value="pw"):
            fccc.sweep(seats, threshold=20, head_run=fake_head_run, count_run=fake_count_run)

        mutating = {"pull", "fetch", "merge", "reset", "checkout", "clone", "push"}
        for argv in captured_argvs:
            if "-C" in argv:
                idx = argv.index("-C")
                path = argv[idx + 1]
                if path in {s.clone_path for s in seats if s.account != "claude"}:
                    # T-4143: the ONE success-path command is the read-only
                    # rev-parse that also prints the checked-out branch.
                    self.assertEqual(argv[idx + 2:], ["rev-parse", "HEAD", "--abbrev-ref", "HEAD"],
                                      f"non-rev-parse command reached a seat clone: {argv}")
            self.assertFalse(mutating & set(argv), f"mutating verb in argv reaching git: {argv}")

    def test_empty_clone_failure_path_only_sends_read_only_verbs(self):
        # FIX-1 (T-4116): lock the no-mutation invariant on the FAILURE path,
        # where `rev-parse HEAD` fails and `_is_unborn_clone`'s sudo-invoked
        # probes (`symbolic-ref` / `show-ref`) execute against the seat clone.
        # The happy-path test above never reaches these verbs (rev-parse HEAD
        # succeeds -> the disambiguation is skipped). Since this tool runs git as
        # OTHER seat users via sudo, every git verb that reaches a clone on this
        # path must be read-only, and that must be test-LOCKED so a future edit
        # swapping a probe to a mutating verb is caught.
        seats = [
            fccc.Seat(persona="mason", account="suborch1", clone_path="/home/suborch1/pantheon-config"),
            fccc.Seat(persona="artisan", account="suborch2", clone_path="/home/suborch2/pantheon-config"),
        ]
        captured_argvs = []

        def fake_head_run(argv, **kwargs):
            captured_argvs.append(argv)
            if "symbolic-ref" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="refs/heads/main\n", stderr="")
            if "show-ref" in argv:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")  # unborn ref
            # rev-parse HEAD -> non-NO_CLONE failure, which drives disambiguation.
            return subprocess.CompletedProcess(
                argv, 128, stdout="",
                stderr="fatal: ambiguous argument 'HEAD': unknown revision or path "
                       "not in the working tree.\n")

        def fake_count_run(argv, **kwargs):
            # Never reached (each seat classifies EMPTY_CLONE before behind-count),
            # but capture defensively so an accidental behind-count leg is caught too.
            captured_argvs.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="0\n", stderr="")

        with mock.patch.object(se.getpass, "getuser", return_value="claude"), \
             mock.patch.object(fccc, "_read_sudo_password", return_value="pw"):
            results = fccc.sweep(seats, threshold=20, head_run=fake_head_run, count_run=fake_count_run)

        # Precondition: the failure path really classified EMPTY_CLONE, i.e. the
        # probe verbs genuinely executed -- a green test that skipped them is worthless.
        self.assertTrue(all(r["verdict"] == fccc.EMPTY_CLONE for r in results))

        read_only = {"rev-parse", "symbolic-ref", "show-ref"}
        mutating = {"pull", "fetch", "merge", "reset", "checkout", "clone", "push"}
        seat_clones = {s.clone_path for s in seats}
        saw_probe = False
        for argv in captured_argvs:
            if "-C" in argv:
                path = argv[argv.index("-C") + 1]
                if path in seat_clones:
                    verb = argv[argv.index("-C") + 2]
                    self.assertIn(verb, read_only,
                                  f"non-read-only git verb reached a seat clone: {argv}")
                    if verb in ("symbolic-ref", "show-ref"):
                        saw_probe = True
            self.assertFalse(mutating & set(argv), f"mutating verb in argv reaching git: {argv}")
        # Prove the disambiguation probes were exercised on this path (not just
        # rev-parse) -- otherwise the read-only assertion covers nothing new.
        self.assertTrue(saw_probe, "expected symbolic-ref/show-ref probes to run on the failure path")


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
        r = {"persona": "archivist", "account": "exocortex", "verdict": fccc.NO_CLONE, "detail": ""}
        self.assertEqual(fccc.needs_alert([r]), [])

    def test_unknown_always_flagged(self):
        r = {"persona": "mason", "account": "suborch1", "verdict": fccc.UNKNOWN, "detail": "sudo failed"}
        self.assertEqual(fccc.needs_alert([r]), [r])

    def test_pending_push_not_flagged(self):
        """PENDING_PUSH is a self-resolving transient (seat has a local
        unpushed commit; next cycle after their push + our fetch resolves
        it) — alerting on it is noise per T-3709's same-shape reasoning."""
        r = {"persona": "recovery", "account": "claude-fallback", "verdict": fccc.PENDING_PUSH,
             "head": "e" * 40, "detail": "seat has a local unpushed commit"}
        self.assertEqual(fccc.needs_alert([r]), [])

    def test_check_seat_boundary_exactly_at_threshold_is_current(self):
        def head_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="d" * 40 + "\n", stderr="")

        def count_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="20\n", stderr="")

        seat = fccc.Seat(persona="orchestrator", account="claude", clone_path="/home/claude/pantheon-config")
        with mock.patch.object(se.getpass, "getuser", return_value="claude"):
            r = fccc.check_seat(seat, threshold=20, head_run=head_run, count_run=count_run)
        self.assertEqual(r["verdict"], fccc.CURRENT)

    def test_check_seat_boundary_one_over_threshold_is_drifted(self):
        def head_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="d" * 40 + "\n", stderr="")

        def count_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="21\n", stderr="")

        seat = fccc.Seat(persona="orchestrator", account="claude", clone_path="/home/claude/pantheon-config")
        with mock.patch.object(se.getpass, "getuser", return_value="claude"):
            r = fccc.check_seat(seat, threshold=20, head_run=head_run, count_run=count_run)
        self.assertEqual(r["verdict"], fccc.DRIFTED)


class AlertMailboxMock(unittest.TestCase):
    def test_alert_sends_expected_shape(self):
        flagged = [
            {"persona": "mason", "account": "suborch1", "verdict": fccc.DRIFTED,
             "detail": "HEAD=abc1234567, 332 commit(s) behind origin/main"},
            {"persona": "codex", "account": "codex", "verdict": fccc.UNKNOWN,
             "detail": "could not read HEAD (unknown)"},
        ]
        with mock.patch("tools.mailbox.send") as msend:
            reached = fccc.alert(flagged)
        # T-4143: orchestrator first (unchanged shape), then recovery's copy, then each
        # flagged persona its own rows.
        self.assertEqual(reached, ["orchestrator", "recovery", "mason", "codex"])
        args, kwargs = msend.call_args_list[0]
        self.assertEqual(args[0], f"{fccc.arm.mailbox_hub()}/orchestrator")
        self.assertIn("mason", args[1])
        self.assertIn("332", args[1])
        self.assertIn("codex", args[1])
        self.assertEqual(args[2], "fleet_clone_currency_check")
        self.assertIn("2", kwargs.get("subject", ""))


class OffMainAndRouting(unittest.TestCase):
    """T-4143: a clone parked off its default branch is OFF_MAIN (alerted) with
    the branch + HEAD age named; alerts reach recovery and the owning persona."""

    SEAT = fccc.Seat(persona="codex", account="codex", clone_path="/home/codex/pantheon-config")

    def _check(self, rev_parse_stdout, *, count_stdout="3\n", count_rc=0, count_stderr="",
               fetch_ok=True, age_stdout=None):
        def head_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=rev_parse_stdout, stderr="")

        def count_run(argv, **kwargs):
            if "--verify" in argv:  # _ref_exists(origin/main): the compare ref is fine
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(argv, count_rc, stdout=count_stdout,
                                               stderr=count_stderr)

        def age_run(argv, **kwargs):
            if age_stdout is None:
                return subprocess.CompletedProcess(argv, 128, stdout="", stderr="bad object")
            return subprocess.CompletedProcess(argv, 0, stdout=age_stdout, stderr="")

        with mock.patch.object(se.getpass, "getuser", return_value="codex"):
            return fccc.check_seat(self.SEAT, threshold=20, fetch_ok=fetch_ok,
                                   head_run=head_run, count_run=count_run, age_run=age_run)

    def test_feature_branch_is_off_main_and_alerted(self):
        month_ago = str(int(fccc.time.time()) - 33 * 86400) + "\n"
        r = self._check("f" * 40 + "\nagent/T-3260-review\n", age_stdout=month_ago)
        self.assertEqual(r["verdict"], fccc.OFF_MAIN)
        self.assertEqual(r["branch"], "agent/T-3260-review")
        self.assertIn("branch agent/T-3260-review, not main", r["detail"])
        self.assertIn("3 commit(s) behind", r["detail"])
        self.assertIn("33.0d old", r["detail"])
        self.assertEqual(fccc.needs_alert([r]), [r])

    def test_detached_head_is_off_main(self):
        r = self._check("f" * 40 + "\nHEAD\n")
        self.assertEqual(r["verdict"], fccc.OFF_MAIN)
        self.assertIn("detached HEAD", r["detail"])

    def test_on_main_within_threshold_stays_current(self):
        r = self._check("f" * 40 + "\nmain\n")
        self.assertEqual(r["verdict"], fccc.CURRENT)

    def test_off_main_outranks_a_failed_fetch(self):
        r = self._check("f" * 40 + "\nagent/x\n", fetch_ok=False)
        self.assertEqual(r["verdict"], fccc.OFF_MAIN)
        self.assertNotIn("behind origin", r["detail"])

    def test_off_main_with_unpushed_head_is_still_off_main(self):
        # PENDING_PUSH alone is not alerted; parked on a branch it still is.
        r = self._check("f" * 40 + "\nagent/x\n", count_rc=128,
                        count_stderr=f"fatal: Invalid revision range {'f' * 40}..origin/main")
        self.assertEqual(r["verdict"], fccc.OFF_MAIN)
        self.assertIn("behind-count unavailable (pending_push)", r["detail"])

    def test_drifted_detail_names_head_age(self):
        week_ago = str(int(fccc.time.time()) - 7 * 86400) + "\n"
        r = self._check("f" * 40 + "\nmain\n", count_stdout="68\n", age_stdout=week_ago)
        self.assertEqual(r["verdict"], fccc.DRIFTED)
        self.assertIn("7.0d old", r["detail"])

    def test_skip_list_can_disposition_off_main(self):
        r = {"persona": "codex", "account": "codex", "repo": "pantheon-config",
             "verdict": fccc.OFF_MAIN, "detail": "on branch x"}
        entry = {"account": "codex", "disposition_ticket": "T-1", "expected_state": "done",
                 "reason": "r"}
        with mock.patch.object(fccc, "is_skip_active", return_value=(True, "active")):
            out = fccc.apply_skip_list([r], [entry])
        self.assertEqual(out[0]["verdict"], fccc.SKIPPED)
        self.assertEqual(out[0]["original_verdict"], fccc.OFF_MAIN)

    def test_real_git_clone_on_feature_branch_is_off_main(self):
        # Real rev-parse output (no mocked seat leg): pins the two-line parse.
        with tempfile.TemporaryDirectory() as tmp:
            bare, work, shas = _init_repo_pair(Path(tmp))
            _git("checkout", "-q", "-b", "agent/T-9999", cwd=work)
            seat = fccc.Seat(persona="p", account=se.getpass.getuser(), clone_path=str(work))
            verdict, head, branch = fccc._seat_head_ref(seat)
            self.assertEqual((verdict, head, branch), ("", shas[-1], "agent/T-9999"))
            _git("checkout", "-q", "--detach", cwd=work)
            self.assertEqual(fccc._seat_head_ref(seat)[2], "HEAD")
            _git("checkout", "-q", "main", cwd=work)
            self.assertEqual(fccc._seat_head_ref(seat)[2], "main")

    def test_routing_skips_duplicate_copies_and_survives_a_missing_inbox(self):
        flagged = [
            {"persona": "orchestrator", "account": "claude", "verdict": fccc.DRIFTED, "detail": "d1"},
            {"persona": "ghost", "account": "ghost", "verdict": fccc.OFF_MAIN, "detail": "d2"},
            {"persona": "mason", "account": "suborch1", "verdict": fccc.OFF_MAIN, "detail": "d3"},
        ]

        def send(inbox, text, sender, **kwargs):
            if inbox.endswith("/ghost"):
                raise FileNotFoundError("no such mailbox")

        with mock.patch("tools.mailbox.send", side_effect=send) as msend, \
             contextlib.redirect_stdout(io.StringIO()):
            reached = fccc.alert(flagged)
        # orchestrator is a flagged persona but already holds the fleet-wide copy; the
        # absent ghost inbox is logged and does not block mason.
        self.assertEqual(reached, ["orchestrator", "recovery", "mason"])
        inboxes = [c.args[0] for c in msend.call_args_list]
        self.assertEqual(sum(1 for i in inboxes if i.endswith("/orchestrator")), 1)
        mason_text = next(c.args[1] for c in msend.call_args_list if c.args[0].endswith("/mason"))
        self.assertIn("d3", mason_text)
        self.assertNotIn("d1", mason_text)  # a persona sees only its own rows


class MainDryRunAndModeGate(unittest.TestCase):
    def test_flagless_run_refuses(self):
        rc = fccc.main([])
        self.assertEqual(rc, 64)

    def test_dry_run_never_alerts(self):
        flagged = [{"persona": "mason", "account": "suborch1", "verdict": fccc.DRIFTED, "detail": "x"}]
        with mock.patch.object(fccc, "discover_seats", return_value=[]), \
             mock.patch.object(fccc, "_fetch_origin_main", return_value=True), \
             mock.patch.object(fccc, "sweep", return_value=flagged), \
             mock.patch.object(fccc, "needs_alert", return_value=flagged), \
             mock.patch.object(fccc, "alert") as malert:
            rc = fccc.main(["--dry-run"])
        self.assertEqual(rc, 0)
        malert.assert_not_called()

    def test_check_alerts_when_flagged(self):
        flagged = [{"persona": "mason", "account": "suborch1", "verdict": fccc.DRIFTED, "detail": "x"}]
        with mock.patch.object(fccc, "discover_seats", return_value=[]), \
             mock.patch.object(fccc, "_fetch_origin_main", return_value=True), \
             mock.patch.object(fccc, "sweep", return_value=flagged), \
             mock.patch.object(fccc, "apply_skip_list", return_value=flagged), \
             mock.patch.object(fccc, "needs_alert", return_value=flagged), \
             mock.patch.object(fccc, "alert") as malert:
            rc = fccc.main(["--check"])
        self.assertEqual(rc, 0)
        # T-3425 extended alert() with a keyword-only `skipped=` param; the
        # main flow always passes it (empty when nothing was suppressed).
        malert.assert_called_once_with(flagged, skipped=[])

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


class SkipListLoad(unittest.TestCase):
    """T-3425: fail-open at every failure mode. A broken skip-list must NEVER
    suppress an alert -- it must always default to zero skips."""

    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_ctx.name)
        self.addCleanup(self._tmp_ctx.cleanup)

    def test_missing_file_returns_empty(self):
        self.assertEqual(fccc.load_skip_list(self.tmp / "does-not-exist.yaml"), [])

    def test_happy_path_yaml(self):
        p = self.tmp / "skips.yaml"
        p.write_text("skips:\n  - account: codex\n    disposition_ticket: T-3304\n    expected_state: done\n    reason: hist\n")
        self.assertEqual(
            fccc.load_skip_list(p),
            [{"account": "codex", "disposition_ticket": "T-3304", "expected_state": "done", "reason": "hist"}],
        )

    def test_parse_error_returns_empty_not_raises(self):
        p = self.tmp / "broken.yaml"
        p.write_text(": :\n  - not\n  even yaml [\n")
        self.assertEqual(fccc.load_skip_list(p), [])

    def test_malformed_root_shape_returns_empty(self):
        # A YAML file whose top-level isn't a mapping -- must not crash.
        p = self.tmp / "list-root.yaml"
        p.write_text("- one\n- two\n")
        self.assertEqual(fccc.load_skip_list(p), [])

    def test_skips_not_a_list_returns_empty(self):
        p = self.tmp / "skips-scalar.yaml"
        p.write_text("skips: notalist\n")
        self.assertEqual(fccc.load_skip_list(p), [])

    def test_non_mapping_entries_are_dropped(self):
        p = self.tmp / "mixed.yaml"
        p.write_text("skips:\n  - account: codex\n    disposition_ticket: T-3304\n    expected_state: done\n    reason: r\n  - 'a bare string'\n  - null\n")
        result = fccc.load_skip_list(p)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["account"], "codex")

    def test_empty_file_returns_empty(self):
        p = self.tmp / "empty.yaml"
        p.write_text("")
        self.assertEqual(fccc.load_skip_list(p), [])

    def test_unreadable_file_returns_empty(self):
        p = self.tmp / "unreadable.yaml"
        p.write_text("skips: []\n")
        p.chmod(0o000)
        try:
            self.assertEqual(fccc.load_skip_list(p), [])
        finally:
            p.chmod(0o644)


class SkipListIsActive(unittest.TestCase):
    """T-3425: expiry-condition evaluation against ticket state."""

    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_ctx.name)
        self.addCleanup(self._tmp_ctx.cleanup)
        for state in ("inbox", "active", "done"):
            (self.tmp / "tickets" / state).mkdir(parents=True)

    def _touch(self, state: str, ticket_id: str):
        (self.tmp / "tickets" / state / f"{ticket_id}.md").write_text("stub\n")

    def test_active_when_ticket_matches_expected_state(self):
        self._touch("done", "T-3304")
        entry = {"account": "codex", "disposition_ticket": "T-3304", "expected_state": "done", "reason": "hist"}
        active, reason = fccc.is_skip_active(entry, repo_root=self.tmp)
        self.assertTrue(active)
        self.assertIn("T-3304", reason)

    def test_expired_when_ticket_in_different_state(self):
        # Ticket exists but has moved to a different state -- skip must expire.
        self._touch("active", "T-3009")
        entry = {"account": "exocortex", "disposition_ticket": "T-3009", "expected_state": "inbox", "reason": "window"}
        active, reason = fccc.is_skip_active(entry, repo_root=self.tmp)
        self.assertFalse(active)
        self.assertIn("EXPIRED", reason)
        self.assertIn("active", reason)

    def test_expired_when_ticket_missing_entirely(self):
        entry = {"account": "codex", "disposition_ticket": "T-9999", "expected_state": "done", "reason": "n/a"}
        active, reason = fccc.is_skip_active(entry, repo_root=self.tmp)
        self.assertFalse(active)
        self.assertIn("EXPIRED", reason)
        self.assertIn("not found", reason)

    def test_malformed_entry_missing_field_ignored(self):
        active, reason = fccc.is_skip_active({"account": "codex"}, repo_root=self.tmp)
        self.assertFalse(active)
        self.assertIn("malformed", reason)

    def test_invalid_expected_state_ignored(self):
        entry = {"account": "codex", "disposition_ticket": "T-3304", "expected_state": "blocked", "reason": "r"}
        active, reason = fccc.is_skip_active(entry, repo_root=self.tmp)
        self.assertFalse(active)
        self.assertIn("expected_state", reason)


class SkipListApply(unittest.TestCase):
    """T-3425: end-to-end suppression + fail-open behavior."""

    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_ctx.name)
        self.addCleanup(self._tmp_ctx.cleanup)
        for state in ("inbox", "active", "done"):
            (self.tmp / "tickets" / state).mkdir(parents=True)

    def _touch(self, state: str, ticket_id: str):
        (self.tmp / "tickets" / state / f"{ticket_id}.md").write_text("stub\n")

    def test_drifted_with_active_skip_becomes_skipped(self):
        self._touch("done", "T-3304")
        results = [{"persona": "codex", "account": "codex", "verdict": fccc.DRIFTED,
                    "detail": "HEAD=abc, 1677 commit(s) behind origin/main"}]
        skips = [{"account": "codex", "disposition_ticket": "T-3304", "expected_state": "done", "reason": "hist"}]
        out = fccc.apply_skip_list(results, skips, repo_root=self.tmp)
        self.assertEqual(out[0]["verdict"], fccc.SKIPPED)
        self.assertEqual(out[0]["original_verdict"], fccc.DRIFTED)
        self.assertIn("T-3304", out[0]["skip_reason"])
        # Original detail preserved so an operator can still see the behind-count.
        self.assertIn("1677", out[0]["detail"])

    def test_unknown_with_active_skip_becomes_skipped(self):
        self._touch("done", "T-3304")
        results = [{"persona": "codex", "account": "codex", "verdict": fccc.UNKNOWN,
                    "detail": "sudo failed"}]
        skips = [{"account": "codex", "disposition_ticket": "T-3304", "expected_state": "done", "reason": "hist"}]
        out = fccc.apply_skip_list(results, skips, repo_root=self.tmp)
        self.assertEqual(out[0]["verdict"], fccc.SKIPPED)
        self.assertEqual(out[0]["original_verdict"], fccc.UNKNOWN)

    def test_empty_clone_with_active_skip_becomes_skipped(self):
        # T-4116: EMPTY_CLONE is a needs-alert verdict, so a dispositioned one
        # is suppressible exactly like DRIFTED/UNKNOWN.
        self._touch("done", "T-3304")
        results = [{"persona": "mason", "account": "suborch1", "repo": "pantheon-inventory",
                    "verdict": fccc.EMPTY_CLONE, "detail": "unborn — remedy: git pull ..."}]
        skips = [{"account": "suborch1", "disposition_ticket": "T-3304", "expected_state": "done", "reason": "hist"}]
        out = fccc.apply_skip_list(results, skips, repo_root=self.tmp)
        self.assertEqual(out[0]["verdict"], fccc.SKIPPED)
        self.assertEqual(out[0]["original_verdict"], fccc.EMPTY_CLONE)

    def test_current_never_suppressed_even_with_skip_entry(self):
        # A CURRENT seat has nothing to suppress; the skip is a no-op here.
        self._touch("done", "T-3304")
        results = [{"persona": "codex", "account": "codex", "verdict": fccc.CURRENT,
                    "detail": "HEAD=abc, 5 commit(s) behind origin/main"}]
        skips = [{"account": "codex", "disposition_ticket": "T-3304", "expected_state": "done", "reason": "r"}]
        out = fccc.apply_skip_list(results, skips, repo_root=self.tmp)
        self.assertEqual(out[0]["verdict"], fccc.CURRENT)
        self.assertNotIn("SKIPPED", out[0]["detail"])

    def test_no_clone_never_suppressed(self):
        results = [{"persona": "archivist", "account": "exocortex", "verdict": fccc.NO_CLONE, "detail": "no clone"}]
        skips = [{"account": "exocortex", "disposition_ticket": "T-3009", "expected_state": "inbox", "reason": "r"}]
        out = fccc.apply_skip_list(results, skips, repo_root=self.tmp)
        self.assertEqual(out[0]["verdict"], fccc.NO_CLONE)

    def test_drifted_with_expired_skip_stays_drifted_with_note(self):
        # Ticket has moved -- skip must not apply, seat must re-alert.
        self._touch("done", "T-3009")  # was expected: inbox
        results = [{"persona": "archivist", "account": "exocortex", "verdict": fccc.DRIFTED,
                    "detail": "HEAD=abc, 546 commit(s) behind"}]
        skips = [{"account": "exocortex", "disposition_ticket": "T-3009", "expected_state": "inbox", "reason": "window"}]
        out = fccc.apply_skip_list(results, skips, repo_root=self.tmp)
        self.assertEqual(out[0]["verdict"], fccc.DRIFTED)
        self.assertIn("skip attempted but not applied", out[0]["detail"])

    def test_no_skip_entry_for_account_passes_through(self):
        results = [{"persona": "specialist", "account": "claude-research", "verdict": fccc.DRIFTED, "detail": "x"}]
        # skip list mentions a different account -- must not touch this one.
        skips = [{"account": "codex", "disposition_ticket": "T-3304", "expected_state": "done", "reason": "r"}]
        out = fccc.apply_skip_list(results, skips, repo_root=self.tmp)
        self.assertEqual(out[0]["verdict"], fccc.DRIFTED)

    def test_empty_skip_list_is_identity(self):
        results = [{"persona": "codex", "account": "codex", "verdict": fccc.DRIFTED, "detail": "x"}]
        out = fccc.apply_skip_list(results, [], repo_root=self.tmp)
        self.assertEqual(out, results)

    def test_apply_never_mutates_input(self):
        self._touch("done", "T-3304")
        results = [{"persona": "codex", "account": "codex", "verdict": fccc.DRIFTED, "detail": "x"}]
        original = [dict(r) for r in results]
        skips = [{"account": "codex", "disposition_ticket": "T-3304", "expected_state": "done", "reason": "r"}]
        fccc.apply_skip_list(results, skips, repo_root=self.tmp)
        self.assertEqual(results, original)

    def test_skipped_never_in_needs_alert(self):
        # The whole point: SKIPPED must not flow into the alert body.
        self._touch("done", "T-3304")
        results = [
            {"persona": "codex", "account": "codex", "verdict": fccc.DRIFTED, "detail": "x"},
            {"persona": "specialist", "account": "claude-research", "verdict": fccc.DRIFTED, "detail": "y"},
        ]
        skips = [{"account": "codex", "disposition_ticket": "T-3304", "expected_state": "done", "reason": "r"}]
        out = fccc.apply_skip_list(results, skips, repo_root=self.tmp)
        flagged = fccc.needs_alert(out)
        self.assertEqual(len(flagged), 1)
        self.assertEqual(flagged[0]["account"], "claude-research")


class RenderAlertWithSkipped(unittest.TestCase):
    def test_skipped_footer_renders_when_present(self):
        flagged = [{"persona": "specialist", "account": "claude-research", "verdict": fccc.DRIFTED,
                    "detail": "HEAD=abc, 25 behind"}]
        skipped = [{"persona": "codex", "account": "codex", "verdict": fccc.SKIPPED,
                    "original_verdict": fccc.DRIFTED,
                    "skip_reason": "skip active per T-3304 (state=done): historical",
                    "detail": "..."}]
        out = fccc.render_alert(flagged, skipped=skipped)
        self.assertIn("specialist/claude-research", out)
        self.assertIn("dispositioned-mitigation carve-outs", out)
        self.assertIn("codex/codex", out)
        self.assertIn("T-3304", out)

    def test_no_skipped_section_when_empty(self):
        flagged = [{"persona": "specialist", "account": "claude-research", "verdict": fccc.DRIFTED, "detail": "x"}]
        out = fccc.render_alert(flagged, skipped=None)
        self.assertNotIn("dispositioned-mitigation carve-outs", out)
        out2 = fccc.render_alert(flagged, skipped=[])
        self.assertNotIn("dispositioned-mitigation carve-outs", out2)

    def test_backwards_compat_call_without_skipped_kwarg(self):
        # Existing callers pass positional flagged only.
        flagged = [{"persona": "specialist", "account": "claude-research", "verdict": fccc.DRIFTED, "detail": "x"}]
        out = fccc.render_alert(flagged)
        self.assertIn("specialist/claude-research", out)


class MainSkipListIntegration(unittest.TestCase):
    def test_main_dry_run_applies_skip_list(self):
        # End-to-end: main() loads the skip-list, applies it against real
        # ticket-state on disk, and drops the skipped seats from the alert
        # while keeping non-skipped drift alerts.
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            (tmp / "tickets" / "done").mkdir(parents=True)
            (tmp / "tickets" / "done" / "T-3304.md").write_text("stub\n")
            skip_yaml = tmp / "skips.yaml"
            skip_yaml.write_text(
                "skips:\n  - account: codex\n    disposition_ticket: T-3304\n"
                "    expected_state: done\n    reason: hist\n"
            )
            raw = [
                {"persona": "codex", "account": "codex", "verdict": fccc.DRIFTED, "detail": "1677 behind"},
                {"persona": "specialist", "account": "claude-research", "verdict": fccc.DRIFTED, "detail": "21 behind"},
            ]
            with mock.patch.object(fccc, "discover_seats", return_value=[]), \
                 mock.patch.object(fccc, "_fetch_origin_main", return_value=True), \
                 mock.patch.object(fccc, "sweep", return_value=raw), \
                 mock.patch.object(fccc, "REPO_ROOT", tmp), \
                 mock.patch.object(fccc, "alert") as malert:
                rc = fccc.main(["--check", "--skip-list", str(skip_yaml)])
            self.assertEqual(rc, 0)
            malert.assert_called_once()
            call_flagged = malert.call_args[0][0]
            self.assertEqual([f["account"] for f in call_flagged], ["claude-research"])
            call_skipped = malert.call_args[1].get("skipped", [])
            self.assertEqual([s["account"] for s in call_skipped], ["codex"])

    def test_main_with_no_skip_file_falls_through_cleanly(self):
        # Fail-open: pointing --skip-list at a nonexistent path must not
        # crash and must not suppress any alerts.
        with tempfile.TemporaryDirectory() as td:
            raw = [{"persona": "codex", "account": "codex", "verdict": fccc.DRIFTED, "detail": "x"}]
            with mock.patch.object(fccc, "discover_seats", return_value=[]), \
                 mock.patch.object(fccc, "_fetch_origin_main", return_value=True), \
                 mock.patch.object(fccc, "sweep", return_value=raw), \
                 mock.patch.object(fccc, "alert") as malert:
                rc = fccc.main(["--check", "--skip-list", str(Path(td) / "nope.yaml")])
            self.assertEqual(rc, 0)
            # Alert fires normally -- codex NOT suppressed.
            malert.assert_called_once()
            self.assertEqual([f["account"] for f in malert.call_args[0][0]], ["codex"])


class RepoDiscovery(unittest.TestCase):
    """T-4061: repos are derived from repos.yaml always_available, not hardcoded."""

    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_ctx.name)
        self.addCleanup(self._tmp_ctx.cleanup)

    def test_reads_always_available_from_repos_yaml(self):
        (self.tmp / "repos.yaml").write_text(
            "schema_version: 1\n"
            "always_available:\n"
            "  - repo: pantheon-config\n    access: rw\n"
            "  - repo: pantheon-inventory\n    access: rw\n"
        )
        self.assertEqual(
            fccc.discover_repos(repo_root=self.tmp),
            ["pantheon-config", "pantheon-inventory"],
        )

    def test_missing_registry_falls_back_to_pantheon_config(self):
        # Fail-open: a registry we cannot read must never DROP the historical
        # pantheon-config check.
        self.assertEqual(fccc.discover_repos(repo_root=self.tmp), ["pantheon-config"])

    def test_empty_always_available_falls_back(self):
        (self.tmp / "repos.yaml").write_text("schema_version: 1\nalways_available: []\n")
        self.assertEqual(fccc.discover_repos(repo_root=self.tmp), ["pantheon-config"])


class ResolveCompareBranch(unittest.TestCase):
    """T-4061: origin default branch resolution, with fallback to main."""

    def test_symbolic_ref_resolves_branch(self):
        fake = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 0, stdout="refs/remotes/origin/trunk\n", stderr=""))
        self.assertEqual(fccc._resolve_compare_branch(Path("/x/repo"), run=fake), "trunk")

    def test_symbolic_ref_resolves_main(self):
        fake = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 0, stdout="refs/remotes/origin/main\n", stderr=""))
        self.assertEqual(fccc._resolve_compare_branch(Path("/x/repo"), run=fake), "main")

    def test_nonzero_returncode_falls_back_to_main(self):
        # No origin/HEAD set (a common state for a freshly-provisioned clone).
        fake = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 128, stdout="", stderr="fatal: ref refs/remotes/origin/HEAD is not a symbolic ref\n"))
        self.assertEqual(fccc._resolve_compare_branch(Path("/x/repo"), run=fake), "main")

    def test_oserror_falls_back_to_main(self):
        def raising(*a, **k):
            raise OSError("boom")
        self.assertEqual(fccc._resolve_compare_branch(Path("/x/repo"), run=raising), "main")

    def test_unexpected_shape_falls_back_to_main(self):
        fake = mock.Mock(return_value=subprocess.CompletedProcess(
            [], 0, stdout="something-unexpected\n", stderr=""))
        self.assertEqual(fccc._resolve_compare_branch(Path("/x/repo"), run=fake), "main")


class ResolveRepoTargets(unittest.TestCase):
    """T-4061: runner-clone-path injection — the runner clone is derived from an
    INJECTABLE base (default REPO_ROOT.parent) so tests never touch a real FS."""

    def test_injected_base_and_branch(self):
        def branch_run(argv, **kwargs):
            # origin/HEAD -> main for pantheon-config, trunk for the other.
            path = argv[argv.index("-C") + 1]
            if path.endswith("pantheon-inventory"):
                return subprocess.CompletedProcess(argv, 0, stdout="refs/remotes/origin/trunk\n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="refs/remotes/origin/main\n", stderr="")

        targets = fccc._resolve_repo_targets(
            ["pantheon-config", "pantheon-inventory"],
            runner_clone_base=Path("/runner-home"), branch_run=branch_run)
        self.assertEqual(targets["pantheon-config"]["runner_clone"], Path("/runner-home/pantheon-config"))
        self.assertEqual(targets["pantheon-config"]["branch"], "main")
        self.assertEqual(targets["pantheon-inventory"]["runner_clone"], Path("/runner-home/pantheon-inventory"))
        self.assertEqual(targets["pantheon-inventory"]["branch"], "trunk")


class MultiRepoSweep(unittest.TestCase):
    """T-4061: sweep iterates seats x repos, one result per (seat, repo)."""

    def _seats(self):
        return [
            fccc.Seat(persona="mason", account="acc_a", clone_path="/home/acc_a/pantheon-config"),
            fccc.Seat(persona="artisan", account="acc_b", clone_path="/home/acc_b/pantheon-config"),
        ]

    def _targets(self):
        return {
            "pantheon-config": {"runner_clone": Path("/runner/pantheon-config"), "branch": "main"},
            "pantheon-inventory": {"runner_clone": Path("/runner/pantheon-inventory"), "branch": "trunk"},
        }

    def test_per_repo_results_for_both_repos(self):
        def head_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="a" * 40 + "\n", stderr="")

        def count_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="0\n", stderr="")

        with mock.patch.object(se.getpass, "getuser", return_value="runner"), \
             mock.patch.object(fccc, "_read_sudo_password", return_value="pw"):
            results = fccc.sweep(self._seats(), self._targets(), threshold=20,
                                 head_run=head_run, count_run=count_run)

        # 2 seats x 2 repos = 4 results, each carrying a repo field.
        self.assertEqual(len(results), 4)
        seen = {(r["account"], r["repo"]) for r in results}
        self.assertEqual(seen, {
            ("acc_a", "pantheon-config"), ("acc_a", "pantheon-inventory"),
            ("acc_b", "pantheon-config"), ("acc_b", "pantheon-inventory"),
        })
        # This assertion FAILS against the pre-T-4061 single-repo code: the old
        # sweep(seats, *, threshold=...) has no positional repo_targets param
        # (TypeError) and its results never carried a pantheon-inventory repo.
        self.assertTrue(any(r["repo"] == "pantheon-inventory" for r in results))

    def test_per_repo_verdicts_matrix(self):
        """One seat can be NO_CLONE for one repo and CURRENT for the other;
        DRIFTED and PENDING_PUSH resolve per (seat, repo) independently."""
        def head_run(argv, **kwargs):
            path = argv[argv.index("-C") + 1]
            # acc_a is missing its pantheon-inventory clone entirely.
            if path == "/home/acc_a/pantheon-inventory":
                return subprocess.CompletedProcess(
                    argv, 128, stdout="",
                    stderr="fatal: cannot change to '/home/acc_a/pantheon-inventory': No such file or directory\n")
            heads = {
                "/home/acc_a/pantheon-config": "a" * 40,
                "/home/acc_b/pantheon-config": "b" * 40,
                "/home/acc_b/pantheon-inventory": "c" * 40,
            }
            return subprocess.CompletedProcess(argv, 0, stdout=heads[path] + "\n", stderr="")

        def count_run(argv, **kwargs):
            # _behind_count disambiguates a revision-range error by verifying the
            # compare ref exists (rev-parse --verify origin/<branch>). Here the
            # runner clone HAS origin/trunk, so the unresolvable half is the seat
            # HEAD sha -> genuine PENDING_PUSH.
            if "--verify" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="f" * 40 + "\n", stderr="")
            rng = argv[-1]  # "<head>..origin/<branch>"
            if rng.startswith("a" * 40):
                return subprocess.CompletedProcess(argv, 0, stdout="0\n", stderr="")   # CURRENT
            if rng.startswith("b" * 40):
                return subprocess.CompletedProcess(argv, 0, stdout="50\n", stderr="")  # DRIFTED
            # "c"*40 -> unresolvable SHA in runner clone -> PENDING_PUSH
            return subprocess.CompletedProcess(
                argv, 128, stdout="", stderr="fatal: Invalid revision range " + "c" * 40 + "..origin/trunk\n")

        with mock.patch.object(se.getpass, "getuser", return_value="runner"), \
             mock.patch.object(fccc, "_read_sudo_password", return_value="pw"):
            results = fccc.sweep(self._seats(), self._targets(), threshold=20,
                                 head_run=head_run, count_run=count_run)

        verdicts = {(r["account"], r["repo"]): r["verdict"] for r in results}
        self.assertEqual(verdicts[("acc_a", "pantheon-config")], fccc.CURRENT)
        self.assertEqual(verdicts[("acc_a", "pantheon-inventory")], fccc.NO_CLONE)
        self.assertEqual(verdicts[("acc_b", "pantheon-config")], fccc.DRIFTED)
        self.assertEqual(verdicts[("acc_b", "pantheon-inventory")], fccc.PENDING_PUSH)

    def test_compare_branch_flows_into_behind_count_argv(self):
        # The resolved per-repo branch (trunk for pantheon-inventory) must reach
        # the rev-list range — not a hardcoded origin/main.
        captured = []

        def head_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout="d" * 40 + "\n", stderr="")

        def count_run(argv, **kwargs):
            captured.append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, stdout="0\n", stderr="")

        with mock.patch.object(se.getpass, "getuser", return_value="runner"), \
             mock.patch.object(fccc, "_read_sudo_password", return_value="pw"):
            fccc.sweep(self._seats(), self._targets(), threshold=20,
                       head_run=head_run, count_run=count_run)
        self.assertTrue(any(rng.endswith("..origin/trunk") for rng in captured))
        self.assertTrue(any(rng.endswith("..origin/main") for rng in captured))


class RepoAwareSkipList(unittest.TestCase):
    """T-4061: repo-scoped skips + backward-compatible account-only skips."""

    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_ctx.name)
        self.addCleanup(self._tmp_ctx.cleanup)
        for state in ("inbox", "active", "done"):
            (self.tmp / "tickets" / state).mkdir(parents=True)
        (self.tmp / "tickets" / "done" / "T-3304.md").write_text("stub\n")

    def _results(self):
        return [
            {"persona": "codex", "account": "codex", "repo": "pantheon-config",
             "verdict": fccc.DRIFTED, "detail": "cfg drift"},
            {"persona": "codex", "account": "codex", "repo": "pantheon-inventory",
             "verdict": fccc.DRIFTED, "detail": "inv drift"},
        ]

    def test_repo_scoped_skip_suppresses_only_that_repo(self):
        skips = [{"account": "codex", "repo": "pantheon-inventory",
                  "disposition_ticket": "T-3304", "expected_state": "done", "reason": "inv only"}]
        out = fccc.apply_skip_list(self._results(), skips, repo_root=self.tmp)
        by_repo = {r["repo"]: r["verdict"] for r in out}
        self.assertEqual(by_repo["pantheon-config"], fccc.DRIFTED)      # untouched
        self.assertEqual(by_repo["pantheon-inventory"], fccc.SKIPPED)   # suppressed

    def test_account_only_skip_suppresses_all_repos(self):
        # Backward-compat: an entry with NO repo field applies fleet-wide across
        # every repo for that account (pre-T-4061 behaviour, unchanged).
        skips = [{"account": "codex", "disposition_ticket": "T-3304",
                  "expected_state": "done", "reason": "account-wide"}]
        out = fccc.apply_skip_list(self._results(), skips, repo_root=self.tmp)
        self.assertTrue(all(r["verdict"] == fccc.SKIPPED for r in out))

    def test_repo_specific_takes_precedence_over_account_wide(self):
        # An account-wide entry AND a repo-specific one for the same account:
        # the repo-specific entry wins for its repo. Here the repo-specific
        # entry is EXPIRED (ticket in the wrong state), so its repo re-alerts
        # even though an account-wide active skip also matches.
        skips = [
            {"account": "codex", "disposition_ticket": "T-3304",
             "expected_state": "done", "reason": "account-wide active"},
            {"account": "codex", "repo": "pantheon-inventory", "disposition_ticket": "T-9999",
             "expected_state": "done", "reason": "repo-specific expired"},
        ]
        out = fccc.apply_skip_list(self._results(), skips, repo_root=self.tmp)
        by_repo = {r["repo"]: r["verdict"] for r in out}
        self.assertEqual(by_repo["pantheon-config"], fccc.SKIPPED)       # account-wide applies
        self.assertEqual(by_repo["pantheon-inventory"], fccc.DRIFTED)    # repo-specific (expired) wins -> re-alerts


class PerRepoFetchFailureF1(unittest.TestCase):
    """F1 (T-4061 rebound): a per-repo fetch failure leaves the runner's local
    origin/<branch> STALE; a behind-count against it under-reports (false
    CURRENT — silent drift miss). fetch_ok=False must force that (seat, repo)
    to UNKNOWN. Real git plumbing so the stale-ref hazard is genuine."""

    def setUp(self):
        self._tmp_ctx = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_ctx.name)
        self.addCleanup(self._tmp_ctx.cleanup)

    def _stale_runner_and_behind_head(self):
        """Build a runner clone whose origin/main is 1 commit STALE vs the true
        remote, and return (work, stale_tip_sha). A seat sitting at stale_tip is
        genuinely behind the true remote, but a compare against the un-refreshed
        local ref reads behind==0."""
        bare, work, shas = _init_repo_pair(self.tmp)
        other = self.tmp / "other"
        _git("clone", str(bare), str(other), cwd=self.tmp)
        _git("config", "user.email", "test@example.invalid", cwd=other)
        _git("config", "user.name", "Test", cwd=other)
        (other / "g.txt").write_text("newer\n")
        _git("add", "g.txt", cwd=other)
        _git("commit", "-m", "remote moved ahead", cwd=other)
        _git("push", "origin", "main", cwd=other)
        # work has NOT fetched -> its origin/main still points at shas[4].
        return work, shas[4]

    def test_stale_ref_would_report_CURRENT_without_the_gate(self):
        # Demonstrates the F1 hazard: with fetch_ok=True but a stale local ref,
        # the seat (truly behind) reads behind==0 -> CURRENT (the silent miss).
        work, tip = self._stale_runner_and_behind_head()
        seat = fccc.Seat(persona="mason", account="suborch1", clone_path="/home/suborch1/pantheon-config")

        def head_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=tip + "\n", stderr="")

        with mock.patch.object(se.getpass, "getuser", return_value="suborch1"):
            r = fccc.check_seat(seat, repo="pantheon-inventory", threshold=20,
                                runner_repo_clone=work, branch="main", fetch_ok=True,
                                head_run=head_run)  # count_run defaults to real git in `work`
        self.assertEqual(r["verdict"], fccc.CURRENT)  # the bug the gate must prevent

    def test_fetch_failed_forces_UNKNOWN_not_CURRENT(self):
        # Same stale ref, but fetch_ok=False (main() reported the per-repo fetch
        # failed) -> UNKNOWN, never CURRENT and never PENDING_PUSH.
        work, tip = self._stale_runner_and_behind_head()
        seat = fccc.Seat(persona="mason", account="suborch1", clone_path="/home/suborch1/pantheon-config")

        def head_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, stdout=tip + "\n", stderr="")

        with mock.patch.object(se.getpass, "getuser", return_value="suborch1"):
            r = fccc.check_seat(seat, repo="pantheon-inventory", threshold=20,
                                runner_repo_clone=work, branch="main", fetch_ok=False,
                                head_run=head_run)
        self.assertEqual(r["verdict"], fccc.UNKNOWN)
        self.assertNotEqual(r["verdict"], fccc.CURRENT)
        self.assertNotEqual(r["verdict"], fccc.PENDING_PUSH)
        self.assertIn("pantheon-inventory", r["detail"])
        self.assertIn("fetch", r["detail"].lower())

    def test_no_clone_takes_precedence_over_fetch_failure(self):
        # A seat with no clone is honestly NO_CLONE regardless of the runner
        # fetch state — the fetch_ok gate must not mask it.
        seat = fccc.Seat(persona="mason", account="suborch1", clone_path="/home/suborch1/pantheon-config")

        def head_run(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, 128, stdout="",
                stderr="fatal: cannot change to '/home/suborch1/pantheon-inventory': No such file or directory\n")

        with mock.patch.object(se.getpass, "getuser", return_value="suborch1"):
            r = fccc.check_seat(seat, repo="pantheon-inventory", threshold=20,
                                runner_repo_clone=Path("/unused"), branch="main", fetch_ok=False,
                                head_run=head_run)
        self.assertEqual(r["verdict"], fccc.NO_CLONE)

    def test_main_captures_per_repo_fetch_and_flags_only_failed_repo(self):
        # End-to-end through main(): the inventory repo's fetch fails, config's
        # succeeds. main() must record fetch_ok per repo and pass it to sweep,
        # so inventory seats classify UNKNOWN while config seats do not.
        seats = [fccc.Seat(persona="mason", account="suborch1", clone_path="/home/suborch1/pantheon-config")]
        targets = {
            "pantheon-config": {"runner_clone": Path("/r/pantheon-config"), "branch": "main"},
            "pantheon-inventory": {"runner_clone": Path("/r/pantheon-inventory"), "branch": "trunk"},
        }
        captured = {}
        real_sweep = fccc.sweep  # bind before patching, so fake_sweep can delegate

        def fake_fetch(*, repo_root=fccc.REPO_ROOT, branch="main", run=subprocess.run):
            # Global gate call (no kwargs) hits REPO_ROOT -> passes; inventory fails.
            return "pantheon-inventory" not in str(repo_root)

        def fake_sweep(s, repo_targets=None, *, threshold, head_run=None, count_run=None):
            captured["targets"] = repo_targets

            def head_stub(argv, **kwargs):
                return subprocess.CompletedProcess(argv, 0, stdout="a" * 40 + "\n", stderr="")

            def count_stub(argv, **kwargs):
                # Would yield CURRENT if the fetch_ok gate did NOT intercept.
                return subprocess.CompletedProcess(argv, 0, stdout="0\n", stderr="")

            results = real_sweep(s, repo_targets, threshold=threshold,
                                 head_run=head_stub, count_run=count_stub)
            captured["results"] = results
            return results

        with mock.patch.object(fccc, "discover_seats", return_value=seats), \
             mock.patch.object(fccc, "discover_repos", return_value=["pantheon-config", "pantheon-inventory"]), \
             mock.patch.object(fccc, "_resolve_repo_targets", return_value=targets), \
             mock.patch.object(fccc, "_fetch_origin_main", side_effect=fake_fetch), \
             mock.patch.object(fccc, "sweep", side_effect=fake_sweep), \
             mock.patch.object(se.getpass, "getuser", return_value="suborch1"), \
             mock.patch.object(fccc, "alert"):
            rc = fccc.main(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertFalse(captured["targets"]["pantheon-inventory"]["fetch_ok"])
        self.assertTrue(captured["targets"]["pantheon-config"]["fetch_ok"])
        verdicts = {(r["account"], r["repo"]): r["verdict"] for r in captured["results"]}
        self.assertEqual(verdicts[("suborch1", "pantheon-inventory")], fccc.UNKNOWN)
        self.assertEqual(verdicts[("suborch1", "pantheon-config")], fccc.CURRENT)


class MultiRepoRenderAlert(unittest.TestCase):
    def test_repo_tag_rendered_when_present(self):
        flagged = [{"persona": "mason", "account": "suborch1", "repo": "pantheon-inventory",
                    "verdict": fccc.DRIFTED, "detail": "42 behind"}]
        out = fccc.render_alert(flagged)
        self.assertIn("mason/suborch1 [pantheon-inventory]:", out)


class MultiRepoMainIntegration(unittest.TestCase):
    def test_main_dry_run_sweeps_both_repos(self):
        # End-to-end: main() discovers two repos, resolves per-repo targets,
        # and the sweep it runs covers both — proving the generalization wires
        # through the real main() flow (no real clone dependency: subprocess is
        # not invoked because sweep/_fetch/_resolve are patched).
        seats = [fccc.Seat(persona="mason", account="suborch1", clone_path="/home/suborch1/pantheon-config")]
        captured = {}

        def fake_sweep(s, repo_targets=None, *, threshold, head_run=None, count_run=None):
            captured["repos"] = list(repo_targets.keys())
            return [{"persona": "mason", "account": "suborch1", "repo": r,
                     "verdict": fccc.CURRENT, "detail": "ok"} for r in repo_targets]

        with mock.patch.object(fccc, "discover_seats", return_value=seats), \
             mock.patch.object(fccc, "discover_repos", return_value=["pantheon-config", "pantheon-inventory"]), \
             mock.patch.object(fccc, "_resolve_repo_targets",
                                return_value={"pantheon-config": {"runner_clone": Path("/r/pantheon-config"), "branch": "main"},
                                              "pantheon-inventory": {"runner_clone": Path("/r/pantheon-inventory"), "branch": "trunk"}}), \
             mock.patch.object(fccc, "_fetch_origin_main", return_value=True), \
             mock.patch.object(fccc, "sweep", side_effect=fake_sweep), \
             mock.patch.object(fccc, "alert") as malert:
            rc = fccc.main(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured["repos"], ["pantheon-config", "pantheon-inventory"])
        malert.assert_not_called()


class TopologyAwareVerification(unittest.TestCase):
    """T-5149: the post-pull check follows the seat's declared runtime."""

    def _out(self, persona, account, binding):
        flagged = [{"persona": persona, "account": account, "verdict": fccc.DRIFTED,
                    "detail": "12 behind"}]
        with mock.patch.object(fccc.arm, "instance_binding", return_value=binding):
            return fccc.render_alert(flagged)

    def test_legacy_peer_daemon_is_never_told_to_fire_consume(self):
        out = self._out("codex", "codex", {"account": "codex"})
        self.assertNotIn("verify a clean consume fire", out)
        self.assertIn("Do NOT fire pantheon-persona-consume@codex by hand", out)
        self.assertIn("pantheon-inbox-consumer@codex.service", out)

    def test_control_plane_seat_keeps_consume_verification(self):
        out = self._out("artisan", "suborch2", {"account": "suborch2", "supervisor": "control-plane",
                                              "restart_service": "x.service", "restart_pane": "p:0"})
        self.assertIn("pantheon-persona-consume@artisan.service", out)
        self.assertNotIn("Do NOT fire", out)

    def test_bespoke_seat_names_its_session_unit(self):
        out = self._out("orchestrator", "claude", {"account": "claude", "restart_service": "claude-telegram.service",
                                            "restart_pane": "claude-telegram-bot:0"})
        self.assertIn("confirm claude-telegram.service is active", out)
        self.assertIn("Do NOT fire pantheon-persona-consume@orchestrator by hand", out)


if __name__ == "__main__":
    unittest.main()
