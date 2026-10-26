"""ORG-076 BT2(b): non-ff rebase edge wiring + push-scope guard for the episode store (T-4708).

Hermetic: temp repo + bare origin per test (tools/_episode_fixtures.py), isolated lock files,
injected pages. The production push step runs (``pusher=None``) with the ORG-101 mirror leg
disabled (``mirror_url=None``).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import _git_utils, delivery_rebase_controller as drc, episode_store as es
from tools._episode_fixtures import RepoFixture, FakeClock, git, make_ports, make_spec

B = lambda fid="f1", ev="evidence\n": es.Broken(fid, ev)  # noqa: E731

REFUSAL = "local-ahead commits besides episode; needs dedicated clone (BT3)"


class NonffCase(unittest.TestCase):
    def setUp(self):
        self.fx = RepoFixture()
        self.addCleanup(self.fx.cleanup)
        self.state = tempfile.TemporaryDirectory(prefix="ep-state-")
        self.addCleanup(self.state.cleanup)
        env = {k: self.fx.env[k] for k in ("PANTHEON_TICKET_ALLOC_LOCKFILE", "PANTHEON_GIT_MUTATION_LOCKFILE",
                                           "PANTHEON_ACTOR")}
        env["PANTHEON_STATE_DIR"] = self.state.name
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        self.pages: list = []
        self.clock = FakeClock()

    def edge(self):
        return es.make_rebase_edge(repo=self.fx.repo, state_dir=self.state.name,
                                   page=lambda s, b: self.pages.append((s, b)))

    def ports(self, with_edge=True, **kw):
        over = dict(pusher=None, mirror_url=None)
        if with_edge:
            e = self.edge()
            over.update(on_nonff=e.on_nonff, on_recover=e.on_recover)
        over.update(kw)
        return make_ports(self.fx, clock=self.clock, pages=self.pages, **over)

    def run_cycles(self, spec, ports, count=None):
        r = None
        for _ in range(count or spec.n):
            r = es.observe(spec, B(), ports=ports, state_dir=self.state.name)
        return r

    def episode(self, spec):
        (ep,) = es.load_domain(spec.domain_key, self.state.name)["episodes"].values()
        return ep

    def local_commit(self, name="unrelated"):
        (self.fx.repo / f"{name}.txt").write_text("x\n")
        git("add", "-A", cwd=self.fx.repo)
        git("commit", "-m", name, cwd=self.fx.repo)
        return git("rev-parse", "HEAD", cwd=self.fx.repo).stdout.strip()

    def origin_main(self):
        return git("rev-parse", "main", cwd=self.fx.origin).stdout.strip()

    def hold_at_push(self, spec):
        """Open the episode and park it at PUSH_INFLIGHT (push step refused by a failing pusher)."""
        fail = es.PushResult(ok=False, nonff=False, detail="held by test")
        return self.run_cycles(spec, self.ports(with_edge=False, pusher=lambda repo: fail))


class RebaseEdgeTests(NonffCase):
    def test_nonff_rebases_and_reaches_verified_without_failure_or_page(self):
        spec = make_spec()
        self.fx.peer_advance("p1")  # origin moves ahead -> the ticket push is non-ff
        self.run_cycles(spec, self.ports(), spec.n + 1)
        ep = self.episode(spec)
        self.assertEqual((ep["state"], ep["delivery"]["phase"]), ("EMITTED", "VERIFIED"))
        self.assertEqual(ep["delivery"]["push_failures"], 0)
        self.assertEqual(self.pages, [])
        self.assertEqual(self.fx.origin_log_count(f"episode_id: {ep['episode_id']}"), 1)

    def test_controller_state_dir_derives_from_store_state_dir(self):
        spec = make_spec()
        self.fx.peer_advance("p1")
        seen = []
        real = drc._deliver_locked

        def spy(*a, **kw):
            seen.append(kw["state_dir"])
            return real(*a, **kw)

        with mock.patch.object(drc, "_deliver_locked", spy):
            self.run_cycles(spec, self.ports(), spec.n + 1)
        self.assertEqual(seen and seen[0], Path(self.state.name) / "delivery-rebase")

    def test_lock_is_not_nested(self):
        spec = make_spec()
        self.fx.peer_advance("p1")
        real = _git_utils.git_mutation_lock
        with mock.patch.object(_git_utils, "git_mutation_lock", wraps=real) as lk:
            depth = {"n": 0, "max": 0}

            class Tracking:
                def __init__(self, *a, **kw):
                    self.cm = real(*a, **kw)

                def __enter__(self):
                    depth["n"] += 1
                    depth["max"] = max(depth["max"], depth["n"])
                    return self.cm.__enter__()

                def __exit__(self, *e):
                    depth["n"] -= 1
                    return self.cm.__exit__(*e)

            lk.side_effect = Tracking
            self.run_cycles(spec, self.ports(), spec.n + 1)
        self.assertEqual(depth["max"], 1)

    def _outcome_ports(self, outcome):
        return self.ports(on_nonff=lambda ep, res: outcome, with_edge=False)

    def _nonff_parked(self, spec, outcome):
        self.fx.peer_advance("p1")
        ports = make_ports(self.fx, clock=self.clock, pages=self.pages, mirror_url=None,
                           pusher=lambda repo: es.PushResult(ok=False, nonff=True, detail="rejected non-fast-forward"),
                           on_nonff=lambda ep, res: outcome)
        self.run_cycles(spec, ports)
        return ports

    def test_adopted_origin_goes_to_pushed_then_verify(self):
        spec = make_spec()

        def adopt(ep, res):  # a peer "adopted" the episode: origin now carries it
            git("push", "origin", "HEAD:main", cwd=self.fx.repo)
            return "ADOPTED_ORIGIN: adopted"

        ports = make_ports(self.fx, clock=self.clock, pages=self.pages, mirror_url=None,
                           pusher=lambda repo: es.PushResult(ok=False, nonff=True, detail="rejected"),
                           on_nonff=adopt)
        self.run_cycles(spec, ports)
        ep = self.episode(spec)
        self.assertEqual((ep["state"], ep["delivery"]["phase"]), ("EMITTED", "VERIFIED"))
        self.assertEqual(ep["delivery"]["push_failures"], 0)

    def test_reenter_is_bounded_then_counts_as_hard_failure(self):
        spec = make_spec()
        self.fx.peer_advance("p1")
        ports = make_ports(self.fx, clock=self.clock, pages=self.pages, mirror_url=None,
                           pusher=lambda repo: es.PushResult(ok=False, nonff=True, detail="rejected"),
                           on_nonff=lambda ep, res: "RE_ENTER: conflict")
        self.run_cycles(spec, ports)
        d = self.episode(spec)["delivery"]
        self.assertEqual((d["phase"], d["push_failures"], d["rebase_retries"]), ("PUSH_INFLIGHT", 0, 1))
        for _ in range(es.MAX_REBASE_RETRIES - 1):
            es.observe(spec, B(), ports=ports, state_dir=self.state.name)
        d = self.episode(spec)["delivery"]
        self.assertEqual((d["push_failures"], d["rebase_retries"]), (0, es.MAX_REBASE_RETRIES))
        es.observe(spec, B(), ports=ports, state_dir=self.state.name)
        self.assertEqual(self.episode(spec)["delivery"]["push_failures"], 1)

    def test_typed_failure_counts_and_pages_at_bound(self):
        spec = make_spec()
        ports = make_ports(self.fx, clock=self.clock, pages=self.pages, mirror_url=None,
                           pusher=lambda repo: es.PushResult(ok=False, nonff=True, detail="rejected"),
                           on_nonff=lambda ep, res: "TYPED_FAILURE: auth")
        self.run_cycles(spec, ports, spec.n + es.MAX_PUSH_FAILURES_BEFORE_PAGE)
        d = self.episode(spec)["delivery"]
        self.assertGreaterEqual(d["push_failures"], es.MAX_PUSH_FAILURES_BEFORE_PAGE)
        self.assertEqual(len([p for p in self.pages if "PUSH_INFLIGHT" in p[0]]), 1)

    def test_controller_exception_is_a_hard_failure_not_a_crash(self):
        spec = make_spec()

        def boom(ep, res):
            raise RuntimeError("controller exploded")

        ports = make_ports(self.fx, clock=self.clock, pages=self.pages, mirror_url=None,
                           pusher=lambda repo: es.PushResult(ok=False, nonff=True, detail="rejected"),
                           on_nonff=boom)
        self.run_cycles(spec, ports)
        d = self.episode(spec)["delivery"]
        self.assertEqual(d["push_failures"], 1)
        self.assertIn("controller exploded", d["last_error"])

    def test_recover_called_when_controller_state_exists(self):
        spec = make_spec()
        self.hold_at_push(spec)
        ep = self.episode(spec)
        drc.save_state(ep["episode_id"], drc.PHASE_INFLIGHT, "deadbeef",
                       state_dir=Path(self.state.name) / "delivery-rebase")
        called = []
        real = drc._recover_locked

        def spy(episode_id, **kw):
            called.append((episode_id, kw["state_dir"]))
            return real(episode_id, **kw)

        with mock.patch.object(drc, "_recover_locked", spy):
            es.observe(spec, B(), ports=self.ports(), state_dir=self.state.name)
        self.assertEqual([c[0] for c in called], [ep["episode_id"]])
        self.assertEqual(called[0][1], Path(self.state.name) / "delivery-rebase")

    def test_no_recover_when_no_controller_state(self):
        spec = make_spec()
        self.hold_at_push(spec)
        with mock.patch.object(drc, "_recover_locked") as rec:
            es.observe(spec, B(), ports=self.ports(), state_dir=self.state.name)
        rec.assert_not_called()


class RecoverEdgeTests(NonffCase):
    """Real-controller recovery paths (RFC-002 R6): guard coverage and liveness."""

    def _seed(self, spec, phase, *, prev=None, new=None, reenter=0):
        self.hold_at_push(spec)
        ep = self.episode(spec)
        sha = drc.find_episode_commit(ep["episode_id"], repo=self.fx.repo)
        drc.save_state(ep["episode_id"], phase, prev or sha, new_sha=new, reenter_count=reenter,
                       state_dir=Path(self.state.name) / "delivery-rebase")
        return ep, sha

    def test_recover_3b_push_refused_when_foreign_commit_above_episode(self):
        spec = make_spec()
        self.hold_at_push(spec)
        ep = self.episode(spec)
        sha = drc.find_episode_commit(ep["episode_id"], repo=self.fx.repo)
        drc.save_state(ep["episode_id"], drc.PHASE_COMPLETED, sha, new_sha=sha,
                       state_dir=Path(self.state.name) / "delivery-rebase")
        foreign = self.local_commit("foreign")
        before = self.origin_main()
        es.observe(spec, B(), ports=self.ports(), state_dir=self.state.name)
        self.assertEqual(self.origin_main(), before)
        self.assertNotEqual(git("merge-base", "--is-ancestor", foreign, "main", cwd=self.fx.origin,
                                check=False).returncode, 0)
        self.assertIn(REFUSAL, self.episode(spec)["delivery"]["last_error"])

    def test_recover_case_iii_push_refused_when_foreign_commit_above_episode(self):
        spec = make_spec()
        self.hold_at_push(spec)
        ep = self.episode(spec)
        # INFLIGHT with a prev_sha that is no longer HEAD, no rebase dir, episode found at a new sha
        drc.save_state(ep["episode_id"], drc.PHASE_INFLIGHT, "0" * 40,
                       state_dir=Path(self.state.name) / "delivery-rebase")
        foreign = self.local_commit("foreign")
        before = self.origin_main()
        es.observe(spec, B(), ports=self.ports(), state_dir=self.state.name)
        self.assertEqual(self.origin_main(), before)
        self.assertNotEqual(git("merge-base", "--is-ancestor", foreign, "main", cwd=self.fx.origin,
                                check=False).returncode, 0)
        self.assertIn(REFUSAL, self.episode(spec)["delivery"]["last_error"])

    def test_recover_3b_clean_head_still_pushes_and_verifies(self):
        spec = make_spec()
        self.hold_at_push(spec)
        ep = self.episode(spec)
        sha = drc.find_episode_commit(ep["episode_id"], repo=self.fx.repo)
        drc.save_state(ep["episode_id"], drc.PHASE_COMPLETED, sha, new_sha=sha,
                       state_dir=Path(self.state.name) / "delivery-rebase")
        es.observe(spec, B(), ports=self.ports(), state_dir=self.state.name)
        ep = self.episode(spec)
        self.assertEqual((ep["state"], ep["delivery"]["phase"]), ("EMITTED", "VERIFIED"))

    def test_recover_reenter_case_i_falls_through_and_reaches_verified(self):
        """Seeded INFLIGHT state with HEAD == prev_sha (what a rebase conflict leaves behind):
        recovery says RE_ENTER; the store must re-drive delivery in the SAME cycle."""
        spec = make_spec()
        self.hold_at_push(spec)
        ep = self.episode(spec)
        sha = drc.find_episode_commit(ep["episode_id"], repo=self.fx.repo)
        drc.save_state(ep["episode_id"], drc.PHASE_INFLIGHT, sha,
                       state_dir=Path(self.state.name) / "delivery-rebase")
        es.observe(spec, B(), ports=self.ports(), state_dir=self.state.name)
        ep = self.episode(spec)
        self.assertEqual((ep["state"], ep["delivery"]["phase"]), ("EMITTED", "VERIFIED"))
        self.assertEqual(ep["delivery"]["push_failures"], 0)
        self.assertEqual(self.fx.origin_log_count(f"episode_id: {ep['episode_id']}"), 1)

    def test_recover_reenter_with_origin_advanced_rebases_and_verifies(self):
        spec = make_spec()
        self.hold_at_push(spec)
        ep = self.episode(spec)
        sha = drc.find_episode_commit(ep["episode_id"], repo=self.fx.repo)
        drc.save_state(ep["episode_id"], drc.PHASE_INFLIGHT, sha,
                       state_dir=Path(self.state.name) / "delivery-rebase")
        self.fx.peer_advance("p1")
        es.observe(spec, B(), ports=self.ports(), state_dir=self.state.name)
        self.assertEqual(self.episode(spec)["delivery"]["phase"], "VERIFIED")

    def test_persistent_rebase_conflict_is_bounded_and_pushes_nothing(self):
        spec = make_spec()
        self.hold_at_push(spec)
        ep = self.episode(spec)
        # a peer edits the very README row region the ticket commit also edits -> real conflict
        peer = self.fx.root / "peer-conflict"
        git("clone", str(self.fx.origin), str(peer), cwd=self.fx.root)
        for k, v in (("user.email", "p@p"), ("user.name", "p"), ("commit.gpgsign", "false")):
            git("config", k, v, cwd=peer)
        readme = peer / "tickets" / "README.md"
        readme.write_text(readme.read_text().replace(
            "|---|---|---|---|---|\n", "|---|---|---|---|---|\n| T-9999 | inbox | peer row | a -> b | now |\n"))
        git("add", "-A", cwd=peer)
        git("commit", "-m", "peer readme", cwd=peer)
        git("push", "origin", "HEAD:main", cwd=peer)
        before = self.origin_main()
        for _ in range(12):
            es.observe(spec, B(), ports=self.ports(), state_dir=self.state.name)
        self.assertEqual(self.fx.origin_log_count(f"episode_id: {ep['episode_id']}"), 0)
        d = self.episode(spec)["delivery"]
        # retries are tolerated up to the bound, then count as hard failures that reach the page
        self.assertGreaterEqual(d["rebase_retries"], es.MAX_REBASE_RETRIES)
        self.assertGreaterEqual(d["push_failures"], 1)
        self.assertEqual(len([p for p in self.pages if "PUSH_INFLIGHT" in p[0]]), 1)
        self.assertEqual(git("rev-list", "--count", f"{before}..main", cwd=self.fx.origin).stdout.strip(), "0")


class PushScopeTests(NonffCase):
    def test_default_push_pushes_only_episode_sha_when_unrelated_commit_is_above(self):
        spec = make_spec()
        self.hold_at_push(spec)
        ep = self.episode(spec)
        sha = git("rev-parse", "HEAD", cwd=self.fx.repo).stdout.strip()
        extra = self.local_commit("above")
        self.assertNotEqual(sha, extra)
        es.observe(spec, B(), ports=self.ports(), state_dir=self.state.name)
        self.assertEqual(self.origin_main(), sha)  # NOT the unrelated commit
        self.assertEqual(self.episode(spec)["delivery"]["phase"], "VERIFIED")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.fx.repo).stdout.strip(), extra)
        self.assertEqual(self.fx.origin_log_count(f"episode_id: {ep['episode_id']}"), 1)

    def test_unrelated_commit_below_episode_blocks_push(self):
        spec = make_spec()
        before = self.origin_main()
        self.local_commit("below")  # local-ahead BEFORE the ticket commit lands on top
        self.run_cycles(spec, self.ports(), spec.n + 1)
        self.assertEqual(self.origin_main(), before)
        d = self.episode(spec)["delivery"]
        self.assertEqual(d["phase"], "PUSH_INFLIGHT")
        self.assertIn(REFUSAL, d["last_error"])
        self.assertGreaterEqual(d["push_failures"], 1)

    def test_scope_refusal_counts_toward_page_bound(self):
        spec = make_spec()
        self.local_commit("below")
        self.run_cycles(spec, self.ports(), spec.n + es.MAX_PUSH_FAILURES_BEFORE_PAGE)
        self.assertEqual(len([p for p in self.pages if "PUSH_INFLIGHT" in p[0]]), 1)

    def test_rebase_path_refused_when_head_has_extra_commits(self):
        spec = make_spec()
        self.hold_at_push(spec)
        self.local_commit("above")
        self.fx.peer_advance("p1")  # now the episode push is non-ff -> rebase path
        before = self.origin_main()
        failures_before = self.episode(spec)["delivery"]["push_failures"]
        es.observe(spec, B(), ports=self.ports(), state_dir=self.state.name)
        self.assertEqual(self.origin_main(), before)  # nothing pushed, not even the episode
        d = self.episode(spec)["delivery"]
        self.assertIn(REFUSAL, d["last_error"])
        self.assertEqual(d["push_failures"], failures_before + 1)

    def test_injected_pusher_also_goes_through_scope_guard(self):
        spec = make_spec()
        self.local_commit("below")
        calls = []
        ports = make_ports(self.fx, clock=self.clock, pages=self.pages, mirror_url=None,
                           pusher=lambda repo: calls.append(1) or es.PushResult(ok=True))
        self.run_cycles(spec, ports, spec.n + 1)
        self.assertEqual(calls, [])


class DocTests(unittest.TestCase):
    def test_docstring_names_what_is_wired(self):
        self.assertIn("make_rebase_edge", es.__doc__ or "")
        self.assertIn("ORG-096", es.__doc__ or "")


if __name__ == "__main__":
    unittest.main()
