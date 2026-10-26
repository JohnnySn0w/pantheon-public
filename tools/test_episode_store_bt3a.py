"""ORG-076 BT3a seams in the episode store (T-4720): sample_key dedupe, heartbeat override,
cross-cycle verify-loop counter. Hermetic: tmp state dir, tmp repo + bare origin, injected page."""
from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import episode_store as es
from tools._episode_fixtures import FakeClock, RepoFixture, git, make_ports, make_spec

B = es.Broken("f1", "evidence\n")
H = es.Healthy()


class Base(unittest.TestCase):
    def setUp(self):
        self.fx = RepoFixture()
        self.addCleanup(self.fx.cleanup)
        self.state = tempfile.TemporaryDirectory(prefix="ep-state-")
        self.addCleanup(self.state.cleanup)
        p = mock.patch.dict(os.environ, {k: self.fx.env[k] for k in (
            "PANTHEON_TICKET_ALLOC_LOCKFILE", "PANTHEON_GIT_MUTATION_LOCKFILE", "PANTHEON_ACTOR")})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop(es.HEARTBEAT_PATH_ENV, None)
        self.pages: list = []
        self.spec = make_spec(n=3)

    def ports(self, **kw):
        return make_ports(self.fx, pages=self.pages, **kw)

    def doc(self):
        return es.load_domain(self.spec.domain_key, self.state.name)


class SampleKeyDedupe(Base):
    def test_replayed_key_is_not_double_counted(self):
        r1 = es.observe(self.spec, B, ports=self.ports(), state_dir=self.state.name, sample_key="run-1")
        r2 = es.observe(self.spec, B, ports=self.ports(), state_dir=self.state.name, sample_key="run-1")
        self.assertEqual((r1.status, r2.status), ("counted", "deduped"))
        self.assertEqual(self.doc()["runs"][self.spec.detector]["n"], 1)

    def test_key_is_persisted_in_same_save_as_the_count(self):
        es.observe(self.spec, B, ports=self.ports(), state_dir=self.state.name, sample_key="run-1")
        self.assertEqual(self.doc()["ingested"], ["run-1"])

    def test_no_key_keeps_old_behaviour(self):
        for _ in range(2):
            es.observe(self.spec, B, ports=self.ports(), state_dir=self.state.name)
        self.assertEqual(self.doc()["runs"][self.spec.detector]["n"], 2)
        self.assertNotIn("ingested", self.doc())

    def test_healthy_sample_is_keyed_too(self):
        es.observe(self.spec, B, ports=self.ports(), state_dir=self.state.name, sample_key="a")
        es.observe(self.spec, H, ports=self.ports(), state_dir=self.state.name, sample_key="b")
        r = es.observe(self.spec, H, ports=self.ports(), state_dir=self.state.name, sample_key="b")
        self.assertEqual(r.status, "deduped")

    def test_keys_are_bounded(self):
        for i in range(es.INGESTED_KEYS_KEPT + 10):
            es.observe(self.spec, H, ports=self.ports(), state_dir=self.state.name, sample_key=f"k{i}")
        keys = self.doc()["ingested"]
        self.assertEqual(len(keys), es.INGESTED_KEYS_KEPT)
        self.assertEqual(keys[-1], f"k{es.INGESTED_KEYS_KEPT + 9}")

    def test_busy_suppressed_does_not_record_key(self):
        path = es.store_path(self.spec.domain_key, self.state.name)
        with es._DomainLock(path, 5):
            r = es.observe(self.spec, B, ports=self.ports(lock_timeout=0.05), state_dir=self.state.name,
                           sample_key="run-1")
        self.assertEqual(r.status, "busy_suppressed")
        r = es.observe(self.spec, B, ports=self.ports(), state_dir=self.state.name, sample_key="run-1")
        self.assertEqual(r.status, "counted")

    def test_old_doc_without_ingested_loads(self):
        es.observe(self.spec, B, ports=self.ports(), state_dir=self.state.name)
        r = es.observe(self.spec, B, ports=self.ports(), state_dir=self.state.name, sample_key="x")
        self.assertEqual(r.status, "counted")


class HeartbeatOverride(Base):
    def test_default_unchanged(self):
        self.assertEqual(es.heartbeat_path(self.state.name),
                         Path(self.state.name) / "heartbeat" / "org076-episode-store.json")

    def test_ports_override_wins_and_is_independent_of_state_dir(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "hb" / "x.json"
            es.observe(self.spec, H, ports=self.ports(heartbeat_path=target), state_dir=self.state.name)
            self.assertTrue(target.is_file())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
            self.assertFalse((Path(self.state.name) / "heartbeat").exists())

    def test_env_override(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "env.json"
            with mock.patch.dict(os.environ, {es.HEARTBEAT_PATH_ENV: str(target)}):
                self.assertEqual(es.heartbeat_path(self.state.name), target)
                es.reconcile([self.spec], ports=self.ports(), state_dir=self.state.name)
            self.assertTrue(target.is_file())

    def test_arg_beats_env(self):
        with mock.patch.dict(os.environ, {es.HEARTBEAT_PATH_ENV: "/nope/env.json"}):
            self.assertEqual(es.heartbeat_path(self.state.name, "/x/arg.json"), Path("/x/arg.json"))

    def test_default_path_keeps_private_mode(self):
        es.observe(self.spec, H, ports=self.ports(), state_dir=self.state.name)
        mode = stat.S_IMODE(es.heartbeat_path(self.state.name).stat().st_mode)
        self.assertEqual(mode, 0o600)


class VerifyLoopCounter(Base):
    """An episode neither local-ahead nor on origin cycles PUSH_INFLIGHT -> PUSHED -> verify."""

    def _stuck_pushed_episode(self):
        spec = make_spec(n=1)
        self.spec = spec
        failing = lambda argv: type("R", (), {"returncode": 1, "stdout": "", "stderr": "nope"})()
        es.observe(spec, B, ports=self.ports(filer=failing), state_dir=self.state.name)
        path = es.store_path(spec.domain_key, self.state.name)
        doc = es._load(path, spec.domain_key)
        ep = next(iter(doc["episodes"].values()))
        ep["delivery"]["phase"] = es.PUSHED
        es._save(path, doc)
        return spec, ep["episode_id"]

    def cycle(self):
        es.reconcile([self.spec], ports=self.ports(), state_dir=self.state.name)
        return next(iter(self.doc()["episodes"].values()))

    def test_one_cycle_never_pages_even_though_it_spins_many_loops(self):
        self._stuck_pushed_episode()
        ep = self.cycle()
        self.assertEqual(self.pages, [])
        self.assertEqual(ep["delivery"]["verify_loops"], 1)

    def test_pages_exactly_once_after_threshold_cycles_then_verified_resets(self):
        spec, eid = self._stuck_pushed_episode()
        for i in range(es.VERIFY_LOOP_PAGE_THRESHOLD - 1):
            self.cycle()
            self.assertEqual(self.pages, [], f"cycle {i + 1}")
        self.cycle()
        self.assertEqual(len(self.pages), 1)
        self.assertIn(eid, self.pages[0][0])
        for _ in range(3):
            self.cycle()
        self.assertEqual(len(self.pages), 1, "no spam after the page")
        self.assertTrue(self.doc()["episodes"][eid]["delivery"]["verify_loop_paged"])
        # the ticket finally lands on origin -> VERIFIED clears the counter
        p = self.fx.repo / "tickets" / "inbox" / "T-77.md"
        p.write_text(f"---\nid: T-77\nepisode_id: {eid}\n---\n\n# x\n")
        git("add", "-A", cwd=self.fx.repo)
        git("commit", "-q", "-m", "file", cwd=self.fx.repo)
        git("push", "-q", "origin", "HEAD:main", cwd=self.fx.repo)
        ep = self.cycle()
        self.assertEqual(ep["delivery"]["phase"], es.VERIFIED)
        self.assertEqual((ep["delivery"]["verify_loops"], ep["delivery"]["verify_loop_paged"]), (0, False))


if __name__ == "__main__":
    unittest.main()
