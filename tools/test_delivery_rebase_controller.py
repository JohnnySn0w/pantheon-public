"""Unit tests for tools/delivery_rebase_controller.py (RFC-002 / T-3651).

Hermetic real-git integration tests (throwaway temp repos + a bare origin; no
network), mirroring tools/test_pull_or_rebase_strict.py's philosophy. This first
suite covers the implemented primitives (persistence, is_rebase_in_progress,
find_episode_commit, classify, ancestry_proof); the full 22-case kill-matrix
(RFC-002 R9) lands with the _deliver_locked/_recover_locked cores.

Run from repo root:
    python3 -m unittest tools.test_delivery_rebase_controller -v
"""
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import delivery_rebase_controller as drc


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _write_ticket(repo: Path, name: str, episode_id: str) -> Path:
    d = repo / "tickets"
    d.mkdir(exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(f"---\nid: {name}\nepisode_id: {episode_id}\n---\n\n# {name}\n")
    return p


class _RepoFixture(unittest.TestCase):
    """A bare origin + a working clone with origin/main at a base commit."""

    def setUp(self):
        # HERMETICITY GUARD: _page() falls back to the real ORG-026 mailbox when no
        # send_page is injected. A recovery test that hits a PAGE path without injecting
        # a recorder (or a regression/mutation that reaches one) would otherwise deliver
        # a REAL page to Recovery's inbox — non-hermetic (cf. feedback_test_with_real_pane_probe
        # _is_nonhermetic). Patch the real sink so NO test can ever leak, regardless of
        # whether it injects send_page. (Tests that assert on paging still inject their own
        # recorder, which short-circuits before this sink.)
        self._mailbox_guard = patch("tools.mailbox.send")
        self._mailbox_guard.start()
        self.addCleanup(self._mailbox_guard.stop)
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.origin = root / "origin.git"
        self.repo = root / "work"
        _git("init", "--bare", str(self.origin), cwd=root)
        _git("init", str(self.repo), cwd=root)
        _git("config", "user.email", "t@t", cwd=self.repo)
        _git("config", "user.name", "t", cwd=self.repo)
        _git("config", "commit.gpgsign", "false", cwd=self.repo)
        (self.repo / "README.md").write_text("base\n")
        _git("add", "-A", cwd=self.repo)
        _git("commit", "-m", "base", cwd=self.repo)
        _git("branch", "-M", "main", cwd=self.repo)
        _git("remote", "add", "origin", str(self.origin), cwd=self.repo)
        _git("push", "-u", "origin", "main", cwd=self.repo)
        # T-3753 (child): the bare origin's HEAD symref still points at
        # refs/heads/master (git init --bare default on hosts whose
        # init.defaultBranch is unset or `master`), because pushing `main`
        # doesn't update HEAD. Any downstream `git clone` then can't resolve
        # the remote HEAD, falls back to a fresh local `master` branch, and
        # a later `git push HEAD:main` fails as non-fast-forward. Point the
        # bare's HEAD at main once, at setUp time, so every peer clone lands
        # on main and its pushes fast-forward.
        _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.origin)

    def tearDown(self):
        self._tmp.cleanup()

    def _commit_ticket(self, name: str, episode_id: str, msg: str | None = None) -> str:
        _write_ticket(self.repo, name, episode_id)
        _git("add", "-A", cwd=self.repo)
        _git("commit", "-m", msg or f"file {name}", cwd=self.repo)
        return _git("rev-parse", "HEAD", cwd=self.repo).stdout.strip()

    def _advance_origin(self, name: str, episode_id: str | None) -> None:
        """A peer clone pushes a commit to origin/main."""
        peer = Path(self._tmp.name) / f"peer-{name}"
        _git("clone", str(self.origin), str(peer), cwd=Path(self._tmp.name))
        _git("config", "user.email", "p@p", cwd=peer)
        _git("config", "user.name", "p", cwd=peer)
        _git("config", "commit.gpgsign", "false", cwd=peer)
        if episode_id is not None:
            _write_ticket(peer, name, episode_id)
        else:
            (peer / f"{name}.txt").write_text("unrelated\n")
        _git("add", "-A", cwd=peer)
        _git("commit", "-m", f"peer {name}", cwd=peer)
        _git("push", "origin", "HEAD:main", cwd=peer)


class PersistenceTests(_RepoFixture):
    def test_save_load_roundtrip_and_created_at_preserved(self):
        with tempfile.TemporaryDirectory() as sd:
            sdp = Path(sd)
            d1 = drc.save_state("EP1", drc.PHASE_INFLIGHT, "aa", origin_sha_at_entry="oo", state_dir=sdp)
            loaded = drc.load_state("EP1", sdp)
            self.assertEqual(loaded["phase"], drc.PHASE_INFLIGHT)
            self.assertEqual(loaded["prev_sha"], "aa")
            self.assertIsNone(loaded["new_sha"])
            self.assertEqual(loaded["reenter_count"], 0)
            d2 = drc.save_state("EP1", drc.PHASE_COMPLETED, "aa", new_sha="bb", state_dir=sdp)
            self.assertEqual(d2["created_at"], d1["created_at"])  # preserved across update
            self.assertEqual(drc.load_state("EP1", sdp)["new_sha"], "bb")

    def test_load_missing_returns_none(self):
        with tempfile.TemporaryDirectory() as sd:
            self.assertIsNone(drc.load_state("NOPE", Path(sd)))

    def test_torn_json_raises_state_corruption(self):
        with tempfile.TemporaryDirectory() as sd:
            p = drc._state_path("EP1", Path(sd))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('{"schema": 1, "epi')  # truncated
            with self.assertRaises(drc.StateCorruptionError):
                drc.load_state("EP1", Path(sd))

    def test_schema_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as sd:
            p = drc._state_path("EP1", Path(sd))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('{"schema": 999}')
            with self.assertRaises(drc.StateCorruptionError):
                drc.load_state("EP1", Path(sd))

    def test_delete(self):
        with tempfile.TemporaryDirectory() as sd:
            drc.save_state("EP1", drc.PHASE_INFLIGHT, "aa", state_dir=Path(sd))
            drc.delete_state("EP1", Path(sd))
            self.assertIsNone(drc.load_state("EP1", Path(sd)))


class RebaseInProgressTests(_RepoFixture):
    def test_false_on_clean_repo(self):
        self.assertFalse(drc.is_rebase_in_progress(self.repo))

    def test_true_when_rebase_merge_dir_present(self):
        (self.repo / ".git" / "rebase-merge").mkdir()
        self.assertTrue(drc.is_rebase_in_progress(self.repo))


class FindEpisodeCommitTests(_RepoFixture):
    def test_zero_matches_returns_none(self):
        self.assertIsNone(drc.find_episode_commit("EPX", repo=self.repo))

    def test_exactly_one_introducing_commit(self):
        sha = self._commit_ticket("T-1", "EP-uniq")
        self.assertEqual(drc.find_episode_commit("EP-uniq", repo=self.repo), sha)

    def test_episode_below_head_is_found(self):
        sha = self._commit_ticket("T-1", "EP-below")   # episode commit
        self._commit_ticket("T-2", "EP-other")          # a later, unrelated local-ahead commit on top
        # episode sits below HEAD; scan (not rev-parse HEAD) must still find it (H4/D-6)
        self.assertEqual(drc.find_episode_commit("EP-below", repo=self.repo), sha)

    def test_more_than_one_introducing_raises(self):
        self._commit_ticket("T-1", "EP-dup")
        self._commit_ticket("T-2", "EP-dup")  # same episode_id introduced in a second commit
        with self.assertRaises(drc.AmbiguousEpisodeError):
            drc.find_episode_commit("EP-dup", repo=self.repo)


class ClassifyTests(_RepoFixture):
    def test_add_add_when_origin_carries_episode(self):
        self._commit_ticket("T-1", "EP-collide")     # local episode commit
        self._advance_origin("T-9", "EP-collide")     # peer publishes the SAME episode_id
        c = drc.classify("EP-collide", "prev", repo=self.repo)
        self.assertEqual(c.kind, drc.ClassKind.ADD_ADD)
        self.assertIsNotNone(c.origin_commit_sha)

    def test_origin_advance_when_unrelated(self):
        self._commit_ticket("T-1", "EP-mine")
        self._advance_origin("T-9", None)  # unrelated commit on origin
        c = drc.classify("EP-mine", "prev", repo=self.repo)
        self.assertEqual(c.kind, drc.ClassKind.ORIGIN_ADVANCE)

    def test_typed_failure_on_fetch_error(self):
        _git("remote", "set-url", "origin", str(Path(self._tmp.name) / "does-not-exist.git"), cwd=self.repo)
        c = drc.classify("EP-mine", "prev", repo=self.repo)
        self.assertEqual(c.kind, drc.ClassKind.TYPED_FAILURE)

    def test_add_add_ambiguous_on_origin_raises(self):
        self._advance_origin("T-a", "EP-dup2")
        self._advance_origin("T-b", "EP-dup2")  # two origin commits introduce the same episode_id
        with self.assertRaises(drc.AmbiguousEpisodeError):
            drc.classify("EP-dup2", "prev", repo=self.repo)


class AncestryProofTests(_RepoFixture):
    def test_proof_passes_after_push(self):
        sha = self._commit_ticket("T-1", "EP-proof")
        _git("push", "origin", "HEAD:main", cwd=self.repo)
        self.assertTrue(drc.ancestry_proof("EP-proof", sha, repo=self.repo))

    def test_proof_fails_when_not_on_origin(self):
        sha = self._commit_ticket("T-1", "EP-noproof")  # committed locally, NOT pushed
        self.assertFalse(drc.ancestry_proof("EP-noproof", sha, repo=self.repo))


class _Pages:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, subject, body):
        self.calls.append((subject, body))


class CoreDeliveryTests(_RepoFixture):
    def _sd(self):
        d = Path(self._tmp.name) / "state"
        return d

    def test_origin_advance_pushes_and_verifies(self):
        prev = self._commit_ticket("T-1", "EP-adv")   # local episode commit (prev_sha)
        self._advance_origin("T-9", None)              # unrelated commit on origin
        pages = _Pages()
        res = drc._deliver_locked("EP-adv", prev, repo=self.repo, state_dir=self._sd(), send_page=pages)
        self.assertEqual(res.outcome, drc.RebaseOutcome.PUSHED, res.detail)
        self.assertIsNone(drc.load_state("EP-adv", self._sd()))   # state deleted on VERIFIED
        self.assertEqual(pages.calls, [])
        # origin/main now carries the episode
        self.assertEqual(len(drc._find_introducing_commits("EP-adv", "origin/main", repo=self.repo)), 1)

    def test_add_add_at_entry_adopts_origin(self):
        prev = self._commit_ticket("T-1", "EP-dup")            # local
        self._advance_origin("T-1", "EP-dup")                  # peer publishes the SAME ticket (patch-identical)
        pages = _Pages()
        res = drc._deliver_locked("EP-dup", prev, repo=self.repo, state_dir=self._sd(), send_page=pages)
        self.assertEqual(res.outcome, drc.RebaseOutcome.ADOPTED_ORIGIN, res.detail)
        # local no longer introduces the episode
        self.assertIsNone(drc.find_episode_commit("EP-dup", repo=self.repo))

    def test_typed_failure_on_broken_remote(self):
        prev = self._commit_ticket("T-1", "EP-net")
        _git("remote", "set-url", "origin", str(Path(self._tmp.name) / "gone.git"), cwd=self.repo)
        res = drc._deliver_locked("EP-net", prev, repo=self.repo, state_dir=self._sd())
        self.assertEqual(res.outcome, drc.RebaseOutcome.TYPED_FAILURE, res.detail)


class CoreRecoveryTests(_RepoFixture):
    def _sd(self):
        return Path(self._tmp.name) / "state"

    def _rebase_onto_advanced_origin(self, episode_id: str) -> tuple[str, str]:
        """Commit the episode (prev), advance origin unrelated, rebase forward.
        Returns (prev_sha, new_sha) with HEAD == new_sha (a rewrite != prev)."""
        prev = self._commit_ticket("T-1", episode_id)
        self._advance_origin("T-9", None)
        _git("fetch", "origin", "main", cwd=self.repo)
        _git("rebase", "origin/main", cwd=self.repo)
        new = _git("rev-parse", "HEAD", cwd=self.repo).stdout.strip()
        self.assertNotEqual(prev, new)
        return prev, new

    def test_case_i_pre_rebase_re_enter(self):
        prev = self._commit_ticket("T-1", "EP-i")
        drc.save_state("EP-i", drc.PHASE_INFLIGHT, prev, state_dir=self._sd())
        # HEAD == prev, no rebase dir, origin has no colliding episode
        res = drc._recover_locked("EP-i", repo=self.repo, state_dir=self._sd())
        self.assertEqual(res.outcome, drc.RebaseOutcome.RE_ENTER, res.detail)

    def test_case_iii_post_rebase_completes(self):
        prev, new = self._rebase_onto_advanced_origin("EP-iii")
        drc.save_state("EP-iii", drc.PHASE_INFLIGHT, prev, state_dir=self._sd())  # crash before COMPLETED persist
        res = drc._recover_locked("EP-iii", repo=self.repo, state_dir=self._sd())
        self.assertEqual(res.outcome, drc.RebaseOutcome.PUSHED, res.detail)
        self.assertIsNone(drc.load_state("EP-iii", self._sd()))

    def test_3b_resume_push(self):
        prev, new = self._rebase_onto_advanced_origin("EP-3b")
        drc.save_state("EP-3b", drc.PHASE_COMPLETED, prev, new_sha=new, state_dir=self._sd())  # crash before push
        res = drc._recover_locked("EP-3b", repo=self.repo, state_dir=self._sd())
        self.assertEqual(res.outcome, drc.RebaseOutcome.PUSHED, res.detail)

    def test_new2_self_push_not_mislabelled_as_adoption(self):
        prev, new = self._rebase_onto_advanced_origin("EP-self")
        _git("push", "origin", "HEAD:main", cwd=self.repo)   # our push landed
        drc.save_state("EP-self", drc.PHASE_COMPLETED, prev, new_sha=new, state_dir=self._sd())  # crash before proof
        res = drc._recover_locked("EP-self", repo=self.repo, state_dir=self._sd())
        self.assertEqual(res.outcome, drc.RebaseOutcome.PUSHED, res.detail)  # NOT ADOPTED_ORIGIN

    def test_h1_crash_then_peer_publishes_same_episode_adopts(self):
        prev = self._commit_ticket("T-1", "EP-h1")
        drc.save_state("EP-h1", drc.PHASE_INFLIGHT, prev, state_dir=self._sd())
        self._advance_origin("T-1", "EP-h1")   # peer publishes the SAME ticket while we were "down"
        res = drc._recover_locked("EP-h1", repo=self.repo, state_dir=self._sd())
        self.assertEqual(res.outcome, drc.RebaseOutcome.ADOPTED_ORIGIN, res.detail)

    def test_torn_state_not_addadd_pages(self):
        p = drc._state_path("EP-torn", self._sd())
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"schema":1,"epi')  # torn
        pages = _Pages()
        res = drc._recover_locked("EP-torn", repo=self.repo, state_dir=self._sd(), send_page=pages)
        self.assertEqual(res.outcome, drc.RebaseOutcome.PAGED, res.detail)
        self.assertTrue(pages.calls)

    def test_reenter_cap_pages(self):
        prev = self._commit_ticket("T-1", "EP-cap")
        drc.save_state("EP-cap", drc.PHASE_INFLIGHT, prev, reenter_count=drc.MAX_REENTERS + 1, state_dir=self._sd())
        pages = _Pages()
        res = drc._recover_locked("EP-cap", repo=self.repo, state_dir=self._sd(), send_page=pages)
        self.assertEqual(res.outcome, drc.RebaseOutcome.PAGED, res.detail)


class _PartialFakeGit:
    """Delegates to real _git_utils except for one (args-prefix -> forced CompletedProcess)."""

    def __init__(self, real, override_prefix, rc, stderr=""):
        import subprocess as _sp
        self._real = real
        self._prefix = tuple(override_prefix)
        self._forced = _sp.CompletedProcess(args=list(override_prefix), returncode=rc, stdout="", stderr=stderr)

    def _git(self, *args, **kw):
        if tuple(args[: len(self._prefix)]) == self._prefix:
            return self._forced
        return self._real._git(*args, **kw)

    def git_mutation_lock(self, **k):
        return self._real.git_mutation_lock(**k)


class MoreKillMatrixTests(_RepoFixture):
    def _sd(self):
        return Path(self._tmp.name) / "state"

    def _conflicting_rebase_in_progress(self, episode_id: str) -> str:
        """Leave a real rebase mid-conflict (rebase-merge dir present). Returns prev_sha."""
        (self.repo / "README.md").write_text("local-line\n")
        _write_ticket(self.repo, "T-1", episode_id)
        _git("add", "-A", cwd=self.repo)
        _git("commit", "-m", "local w/ readme change", cwd=self.repo)
        prev = _git("rev-parse", "HEAD", cwd=self.repo).stdout.strip()
        # peer changes the SAME line on origin -> rebase conflict
        peer = Path(self._tmp.name) / "peer-conf"
        _git("clone", str(self.origin), str(peer), cwd=Path(self._tmp.name))
        _git("config", "user.email", "p@p", cwd=peer)
        _git("config", "user.name", "p", cwd=peer)
        (peer / "README.md").write_text("origin-line\n")
        _git("add", "-A", cwd=peer)
        _git("commit", "-m", "peer readme change", cwd=peer)
        _git("push", "origin", "HEAD:main", cwd=peer)
        _git("fetch", "origin", "main", cwd=self.repo)
        r = subprocess.run(["git", "rebase", "origin/main"], cwd=self.repo, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)  # conflicted
        self.assertTrue(drc.is_rebase_in_progress(self.repo))
        return prev

    def test_case_ii_live_mid_conflict_re_enters(self):
        prev = self._conflicting_rebase_in_progress("EP-ii")
        drc.save_state("EP-ii", drc.PHASE_INFLIGHT, prev, state_dir=self._sd())
        res = drc._recover_locked("EP-ii", repo=self.repo, state_dir=self._sd())
        self.assertEqual(res.outcome, drc.RebaseOutcome.RE_ENTER, res.detail)
        self.assertFalse(drc.is_rebase_in_progress(self.repo))  # aborted

    def test_push_dual_writes_to_the_mirror_after_origin(self):
        """T-4691: a successful HEAD:main push runs the ORG-101 mirror leg and
        reports it; the ancestry proof stays against origin (no failover here)."""
        with patch("tools._git_utils.mirror_push_after_origin", return_value="mirror pushed") as m:
            ok, detail, is_non_ff = drc._push_head_main(self.repo)
        self.assertTrue(ok, detail)
        self.assertIn("mirror pushed", detail)
        m.assert_called_once_with(self.repo, "HEAD:main")

    def test_refused_push_never_touches_the_mirror(self):
        (self.repo / ".git" / "rebase-merge").mkdir()
        with patch("tools._git_utils.mirror_push_after_origin") as m:
            ok, _detail, _ = drc._push_head_main(self.repo)
        self.assertFalse(ok)
        m.assert_not_called()

    def test_d5_push_refused_while_rebasing(self):
        (self.repo / ".git" / "rebase-merge").mkdir()
        ok, detail, is_non_ff = drc._push_head_main(self.repo)
        self.assertFalse(ok)
        self.assertIn("D-5", detail)

    # T-4388: the pre-push freshness hook can stop its `git pull --rebase` on a
    # conflict and exit 1 without aborting. _push_head_main must not strand that.
    class _HookStrandsOnPush:
        def __init__(self, real, on_push, abort_rc=None):
            self._real, self._on_push, self._abort_rc = real, on_push, abort_rc

        def _git(self, *args, **kw):
            import subprocess as _sp
            if args[:1] == ("push",):
                self._on_push()
                return _sp.CompletedProcess(list(args), 1, "", "pre-push: conflict\nerror: failed to push some refs")
            if self._abort_rc is not None and args[:2] == ("rebase", "--abort"):
                return _sp.CompletedProcess(list(args), self._abort_rc, "", "abort boom")
            return self._real._git(*args, **kw)

        def git_mutation_lock(self, **k):
            return self._real.git_mutation_lock(**k)

    def _strand(self):
        """What the hook leaves: the local commit mid-conflict over a peer's change."""
        self._conflicting_rebase_in_progress("EP-hook")

    def test_hook_stranded_rebase_is_aborted_and_reported_non_ff(self):
        fake = self._HookStrandsOnPush(drc._git_utils, self._strand)
        ok, detail, is_non_ff = drc._push_head_main(self.repo, git=fake)
        self.assertFalse(ok)
        self.assertTrue(is_non_ff, "origin moved ahead: the caller must re-classify (RE_ENTER)")
        self.assertIn("aborted", detail)
        self.assertFalse(drc.is_rebase_in_progress(self.repo), "the hook's rebase must not be left behind")

    def test_hook_stranded_rebase_abort_failure_is_loud_and_not_non_ff(self):
        fake = self._HookStrandsOnPush(drc._git_utils, self._strand, abort_rc=1)
        ok, detail, is_non_ff = drc._push_head_main(self.repo, git=fake)
        self.assertFalse(ok)
        self.assertFalse(is_non_ff)
        self.assertIn("STILL MID-REBASE", detail)

    def test_abort_failure_pages(self):
        prev = self._conflicting_rebase_in_progress("EP-af")
        drc.save_state("EP-af", drc.PHASE_INFLIGHT, prev, state_dir=self._sd())
        fake = _PartialFakeGit(drc._git_utils, ("rebase", "--abort"), rc=1, stderr="abort boom")
        pages = _Pages()
        res = drc._recover_locked("EP-af", repo=self.repo, state_dir=self._sd(), git=fake, send_page=pages)
        self.assertEqual(res.outcome, drc.RebaseOutcome.PAGED, res.detail)
        self.assertTrue(any("ABORT-FAILED" in s for s, _ in pages.calls))
        _git("rebase", "--abort", cwd=self.repo)  # clean up for tearDown

    def test_ancestry_fail_yields_unverified(self):
        from unittest.mock import patch
        prev = self._commit_ticket("T-1", "EP-unv")
        self._advance_origin("T-9", None)
        with patch.object(drc, "ancestry_proof", return_value=False):
            res = drc._deliver_locked("EP-unv", prev, repo=self.repo, state_dir=self._sd())
        self.assertEqual(res.outcome, drc.RebaseOutcome.UNVERIFIED, res.detail)
        self.assertIsNotNone(drc.load_state("EP-unv", self._sd()))  # state retained

    def test_foreign_head_move_pages(self):
        from unittest.mock import patch
        prev = self._conflicting_rebase_in_progress("EP-fh")
        # record a prev_sha that does NOT match reality, and make the episode unfindable after abort
        drc.save_state("EP-fh", drc.PHASE_INFLIGHT, "0" * 40, state_dir=self._sd())
        pages = _Pages()
        with patch.object(drc, "find_episode_commit", return_value=None):
            res = drc._recover_locked("EP-fh", repo=self.repo, state_dir=self._sd(), send_page=pages)
        self.assertEqual(res.outcome, drc.RebaseOutcome.PAGED, res.detail)
        self.assertTrue(any("HEAD MOVED" in s for s, _ in pages.calls))

    def test_worktree_correct_rebase_detection(self):
        # is_rebase_in_progress must detect a per-worktree rebase dir via git rev-parse --git-path.
        wt = Path(self._tmp.name) / "linked-wt"
        _git("worktree", "add", "--detach", str(wt), "HEAD", cwd=self.repo)
        self.assertFalse(drc.is_rebase_in_progress(wt))
        # simulate a rebase-state dir for this linked worktree at its real --git-path location
        gp = subprocess.run(["git", "rev-parse", "--git-path", "rebase-merge"],
                            cwd=wt, capture_output=True, text=True).stdout.strip()
        target = Path(gp) if Path(gp).is_absolute() else (wt / gp)
        target.mkdir(parents=True, exist_ok=True)
        self.assertTrue(drc.is_rebase_in_progress(wt))
        # a bare .git/rebase-merge check would look at the wrong path -> this proves worktree-correctness


class PublicWrapperLockTests(_RepoFixture):
    def test_wrapper_acquires_lock_once(self):
        import contextlib

        class _FakeGit:
            def __init__(self, real):
                self._real = real
                self.lock_acquires = 0

            def _git(self, *a, **k):
                return self._real._git(*a, **k)

            @contextlib.contextmanager
            def git_mutation_lock(self, **k):
                self.lock_acquires += 1
                yield

        fake = _FakeGit(drc._git_utils)
        prev = self._commit_ticket("T-1", "EP-lock")
        self._advance_origin("T-9", None)
        res = drc.on_nonff_rejection("EP-lock", prev, repo=self.repo, state_dir=Path(self._tmp.name) / "st", git=fake)
        self.assertEqual(fake.lock_acquires, 1)  # exactly once (R7 non-reentrancy)
        self.assertEqual(res.outcome, drc.RebaseOutcome.PUSHED, res.detail)


if __name__ == "__main__":
    unittest.main()
