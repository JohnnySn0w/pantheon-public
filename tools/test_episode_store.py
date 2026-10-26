"""ORG-076 BT1 episode store: machine, delivery, routing, redaction wiring, seams (T-4707).

RFC-004 §6 numbers are named in the test docstrings. Hermetic: temp repo + bare origin per
test, isolated lock files + PANTHEON_STATE_DIR, injected page/liveness/clock. Real
``ticket.py file --no-push`` runs in a child process against the temp clone.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import _git_utils, episode_store as es, repair_evidence as rev, ticket
from tools._episode_fixtures import RepoFixture, FakeClock, git, hermetic_pusher, make_ports, make_spec

B = lambda fid="f1", ev="evidence\n": es.Broken(fid, ev)  # noqa: E731
U = lambda fid="f1", ev="evidence\n": es.Unknown(fid, ev)  # noqa: E731
H = es.Healthy()


class StoreCase(unittest.TestCase):
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

    def ports(self, **kw):
        return make_ports(self.fx, clock=self.clock, pages=self.pages, **kw)

    def noop_ports(self, **kw):
        """Delivery that always fails fast: for machine-only tests."""
        return self.ports(filer=lambda argv: es.FileResult(1, "", "fake filer down"), **kw)

    def obs(self, spec, verdict, ports=None):
        return es.observe(spec, verdict, ports=ports or self.ports(), state_dir=self.state.name)

    def doc(self, spec):
        return es.load_domain(spec.domain_key, self.state.name)

    def episodes(self, spec, state=None):
        return [e for e in self.doc(spec)["episodes"].values() if state in (None, e["state"])]

    def origin_ticket_text(self):
        (name,) = self.fx.origin_ticket_files()
        return git("show", f"main:{name}", cwd=self.fx.origin).stdout


class ConstantsTests(unittest.TestCase):
    def test_open_threshold_mirrors_persona_bringup(self):
        from tools import persona_bringup
        self.assertEqual(es.DEFAULT_N, persona_bringup.SOFT_FAILURE_ALERT_STREAK)
        self.assertEqual(es.DEFAULT_M, 2)

    def test_state_dir_never_defaults_to_tmp(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PANTHEON_STATE_DIR", None)
            d = es.resolve_state_dir()
        self.assertFalse(str(d).startswith("/tmp"))
        self.assertTrue(str(d).endswith(".local/state/pantheon"))


class MachineTests(StoreCase):
    def test_flap_under_threshold_opens_nothing(self):  # §6.4
        spec = make_spec()
        for v in [B(), H, B(), H, B(), H, U(), H]:
            self.obs(spec, v, self.noop_ports())
        self.assertEqual(self.episodes(spec), [])

    def test_oscillation_across_boundary_is_bounded(self):  # §6.4
        spec = make_spec()
        seq = [B(), B(), B(), H, B(), H, B(), H, B(), B(), H, B(), H, H] * 2
        for v in seq:
            self.obs(spec, v, self.noop_ports())
        opened = self.episodes(spec)
        # every H run is length <2 until the final HH; the single episode opens once and
        # closes once at the very end -> never a storm
        self.assertLessEqual(len(opened), 2)
        self.assertGreaterEqual(len(opened), 1)

    def test_open_after_n_then_suppression_then_close_then_fresh_id(self):
        spec = make_spec()
        p = self.noop_ports
        self.assertEqual(self.obs(spec, B(), p()).status, "counted")
        self.assertEqual(self.obs(spec, B(), p()).status, "counted")
        r = self.obs(spec, B(), p())
        self.assertEqual(r.status, "opened")
        first = r.episode_ids[0]
        self.assertTrue(first.startswith("ep-quota-probe-specialist-"))
        r = self.obs(spec, B(ev="newer\n"), p())
        self.assertEqual((r.status, r.episode_ids), ("suppressed", [first]))
        ep = self.doc(spec)["episodes"][first]
        self.assertEqual(ep["counters"]["non_healthy"], 4)
        self.assertEqual(ep["last_evidence"], "newer\n")
        self.assertEqual(ep["evidence"], "evidence\n")  # OPEN-time evidence is what the ticket carries
        self.assertEqual(len(self.episodes(spec)), 1)  # suppression: no second episode
        # hysteresis: one healthy is a blip; a non-healthy resets; then M=2 closes
        self.obs(spec, H, p())
        self.assertEqual(self.doc(spec)["episodes"][first]["state"], "OPEN")
        self.obs(spec, B(), p())
        self.obs(spec, H, p())
        self.assertEqual(self.doc(spec)["episodes"][first]["state"], "OPEN")
        r = self.obs(spec, H, p())
        self.assertEqual((r.status, r.episode_ids), ("resolved", [first]))
        for _ in range(3):
            last = self.obs(spec, B(), p())
        second = last.episode_ids[0]
        self.assertNotEqual(first, second)  # fresh episode_id after RESOLVED

    def test_different_fault_id_resets_the_open_run(self):
        spec = make_spec()
        for fid in ("a", "b", "a", "b", "a", "b"):
            self.obs(spec, B(fid), self.noop_ports())
        self.assertEqual(self.episodes(spec), [])

    def test_unknown_is_first_class_and_kind_recorded(self):
        spec = make_spec()
        for _ in range(3):
            self.obs(spec, U("quota-probe-unreadable:specialist"), self.noop_ports())
        (ep,) = self.episodes(spec)
        self.assertEqual((ep["verdict_kind"], ep["fault_id"]), ("unknown", "quota-probe-unreadable:specialist"))

    def test_healthy_from_detector_a_never_closes_detector_b_episode(self):  # §1 close-target rule
        a = make_spec(detector="det-a")
        b = make_spec(detector="det-b")  # same domain_key, different detector instance
        for _ in range(3):
            self.obs(a, B("fa"), self.noop_ports())
        (ep,) = self.episodes(a)
        for _ in range(5):
            self.obs(b, H, self.noop_ports())
        ep = self.doc(a)["episodes"][ep["episode_id"]]
        self.assertEqual((ep["state"], ep["close_streak"]), ("OPEN", 0))
        for _ in range(2):
            self.obs(a, H, self.noop_ports())
        self.assertEqual(self.doc(a)["episodes"][ep["episode_id"]]["state"], "RESOLVED")

    def test_store_is_durable_and_atomic_file_layout(self):
        spec = make_spec()
        for _ in range(3):
            self.obs(spec, B(), self.noop_ports())
        path = es.store_path(spec.domain_key, self.state.name)
        self.assertEqual(path.parent.name, "episode-store")
        json.loads(path.read_text())
        self.assertEqual([p.name for p in path.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_corrupt_store_is_loud(self):
        spec = make_spec()
        path = es.store_path(spec.domain_key, self.state.name)
        path.parent.mkdir(parents=True)
        path.write_text("{ torn")
        with self.assertRaises(es.StoreCorruptionError):
            self.obs(spec, B(), self.noop_ports())


class DeliveryTests(StoreCase):
    def open_and_deliver(self, spec, verdict=None, ports=None):
        verdict = verdict or B()
        r = None
        for _ in range(spec.n):
            r = self.obs(spec, verdict, ports)
        return r

    def test_happy_path_one_ticket_verified_on_origin(self):  # §6.3 e2e
        spec = make_spec()
        self.open_and_deliver(spec)
        (ep,) = self.episodes(spec)
        self.assertEqual((ep["state"], ep["delivery"]["phase"]), ("EMITTED", "VERIFIED"))
        self.assertRegex(ep["delivery"]["ticket_id"], r"^T-\d+$")
        self.assertEqual(self.fx.origin_log_count(f"episode_id: {ep['episode_id']}"), 1)
        fm, _ = ticket.parse_front_matter(self.origin_ticket_text())
        for k, v in (("fault_id", "f1"), ("verdict", "broken"), ("subsystem", "quota"),
                     ("detector", "quota-probe"), ("domain_key", "quota-probe:specialist"),
                     ("attempts_budget", "2"), ("evidence_deferred", "false"),
                     ("episode_id", ep["episode_id"])):
            self.assertEqual(fm[k], v, k)
        self.assertEqual(fm["target"], "role:system_orchestrator")
        self.assertIn("repair", fm["labels"])
        # suppression: more non-healthy verdicts never add a second ticket
        self.obs(spec, B())
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)

    def test_git_path_down_accumulates_then_resumes_with_one_ticket(self):  # §6.7 (fetch/lookup UNKNOWN)
        spec = make_spec()
        good = self.fx.origin
        git("remote", "set-url", "origin", str(self.fx.root / "gone.git"), cwd=self.fx.repo)
        self.open_and_deliver(spec)
        (ep,) = self.episodes(spec)
        self.assertEqual((ep["state"], ep["delivery"]["phase"]), ("OPEN", "NONE"))
        self.assertIn("UNKNOWN", ep["delivery"]["last_error"])
        self.assertEqual(self.fx.origin_ticket_files(), [])  # nothing filed while unknown
        self.assertEqual([p.name for p in (self.fx.repo / "tickets" / "inbox").iterdir()], [".gitkeep"])
        git("remote", "set-url", "origin", str(good), cwd=self.fx.repo)
        self.obs(spec, B())  # next cycle resumes
        (ep,) = self.episodes(spec)
        self.assertEqual((ep["state"], ep["delivery"]["phase"]), ("EMITTED", "VERIFIED"))
        self.assertEqual(self.fx.origin_log_count(f"episode_id: {ep['episode_id']}"), 1)

    def test_push_failure_stays_push_inflight_then_recovers_without_duplicate(self):  # §6.7 (push down)
        spec = make_spec()
        down = es.PushResult(False, False, "network down")
        self.open_and_deliver(spec, ports=self.ports(pusher=lambda repo: down))
        (ep,) = self.episodes(spec)
        self.assertEqual((ep["state"], ep["delivery"]["phase"]), ("OPEN", "PUSH_INFLIGHT"))
        self.assertEqual(self.fx.origin_ticket_files(), [])
        # restart (new Ports, same state dir): resumes the push, no second ticket
        es.reconcile([spec], ports=self.ports(), state_dir=self.state.name)
        (ep,) = self.episodes(spec)
        self.assertEqual((ep["state"], ep["delivery"]["phase"]), ("EMITTED", "VERIFIED"))
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)
        local = git("log", "--format=%H", "-S", f"episode_id: {ep['episode_id']}", "origin/main..HEAD",
                    "--", "tickets/", cwd=self.fx.repo).stdout.split()
        self.assertEqual(local, [])  # no stranded local-ahead copy

    def test_nonff_never_rebases_and_pages_after_bound(self):  # ORG-096 seam + rebase containment
        spec = make_spec()
        self.fx.peer_advance("p1")  # origin moves ahead of our clone
        seen = []
        git("fetch", "origin", cwd=self.fx.repo)
        calls = []
        real = _git_utils._git

        def spy(*a, **kw):
            calls.append(a[:2])
            return real(*a, **kw)

        ports = self.ports(on_nonff=lambda ep, res: seen.append(res.detail))
        # the clone is behind: pre-seed a fast-forward-impossible state by making local diverge
        with mock.patch.object(_git_utils, "_git", spy):
            self.open_and_deliver(spec, ports=ports)
        # first delivery: ticket.py file --no-push fetches; our clone is behind origin so the
        # push is non-ff
        (ep,) = self.episodes(spec)
        self.assertEqual(ep["delivery"]["phase"], "PUSH_INFLIGHT")
        self.assertTrue(seen, "on_nonff seam was not called")
        self.assertFalse([c for c in calls if c and c[0] == "rebase"], "BT1 must never rebase")
        head_before = git("rev-parse", "HEAD", cwd=self.fx.repo).stdout
        for _ in range(es.MAX_PUSH_FAILURES_BEFORE_PAGE + 1):
            self.obs(spec, B(), ports)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.fx.repo).stdout, head_before)
        self.assertEqual(len([p for p in self.pages if "PUSH_INFLIGHT" in p[0]]), 1)  # paged once
        self.assertEqual(self.fx.origin_ticket_files(), [])
        self.assertIn("ORG-096", (es.__doc__ or ""))

    def test_restart_new_ports_same_state_dir_no_duplicate(self):  # §6.8 (in-process flavour)
        spec = make_spec()
        self.open_and_deliver(spec)
        before = self.doc(spec)
        es.reconcile([spec], ports=self.ports(), state_dir=self.state.name)
        self.obs(spec, B(), self.ports())
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)
        self.assertEqual(list(before["episodes"]), list(self.doc(spec)["episodes"]))

    def test_already_committed_corpus_wins_over_store_state(self):  # idempotent-create check
        spec = make_spec()
        self.open_and_deliver(spec)
        (ep,) = self.episodes(spec)
        # lose the store's memory of delivery: back to NONE; the committed corpus says delivered
        path = es.store_path(spec.domain_key, self.state.name)
        doc = json.loads(path.read_text())
        doc["episodes"][ep["episode_id"]]["delivery"].update(phase="NONE", ticket_id=None)
        doc["episodes"][ep["episode_id"]]["state"] = "OPEN"
        path.write_text(json.dumps(doc))
        es.reconcile([spec], ports=self.ports(), state_dir=self.state.name)
        (ep2,) = self.episodes(spec)
        self.assertEqual((ep2["state"], ep2["delivery"]["phase"]), ("EMITTED", "VERIFIED"))
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)


class EvidenceWiringTests(StoreCase):
    def _deliver(self, evidence, **spec_kw):
        spec = make_spec(**spec_kw)
        for _ in range(3):
            self.obs(spec, es.Broken("f1", evidence))
        return spec

    def test_real_strip_end_to_end(self):  # §6.15
        from tools.test_repair_evidence import BENIGN, _fixture, _SK, _GH, _BEARER, _PATHS
        self._deliver(_fixture())
        text = self.origin_ticket_text()
        fm, _ = ticket.parse_front_matter(text)
        for line in BENIGN:
            self.assertIn(line, fm["evidence_snapshot"])
        for needle in (_SK, _GH, _BEARER, "other-acct", "credentials.json", "claude-fallback"):
            self.assertNotIn(needle.encode(), text.encode())

    def test_over_cap_defers_and_omits_snapshot_key(self):  # §6.16
        self._deliver(("y" * 99 + "\n") * 400)
        text = self.origin_ticket_text()
        fm, _ = ticket.parse_front_matter(text)
        self.assertEqual(fm["evidence_deferred"], "true")
        self.assertNotIn("evidence_snapshot", fm)
        self.assertNotIn("evidence_snapshot", text)

    def test_under_cap_has_snapshot_and_not_deferred(self):  # §6.16
        self._deliver("small\n")
        fm, _ = ticket.parse_front_matter(self.origin_ticket_text())
        self.assertEqual(fm["evidence_snapshot"], "small\n")
        self.assertIn(fm["evidence_deferred"], ("false", None))

    def test_redactor_throw_defers_with_no_partial_snapshot(self):  # §6.17
        def boom(_):
            raise RuntimeError("mid-emit")
        self._deliver("secret sk-" + "ant-TESTISSUED0123456789abcd\n", redactor=boom)
        text = self.origin_ticket_text()
        fm, _ = ticket.parse_front_matter(text)
        self.assertEqual(fm["evidence_deferred"], "true")
        self.assertNotIn("evidence_snapshot", fm)
        self.assertNotIn("TESTISSUED", text)


CAND_X = rev.Candidate("consultant", "codex", "claude-codex")     # other harness, other account
CAND_S = rev.Candidate("orchestrator", "claude", "claude")          # same harness, same account


class RoutingTests(StoreCase):
    def _run(self, live, evidence="small\n", **kw):
        spec = make_spec(candidates=(CAND_X, CAND_S), **kw)
        ports = self.ports(liveness=lambda c: c.name in live)
        for _ in range(3):
            self.obs(spec, es.Broken("f1", evidence), ports)
        fm, _ = ticket.parse_front_matter(self.origin_ticket_text())
        return fm

    def test_prefers_other_harness(self):  # §6.6
        fm = self._run({"consultant", "orchestrator"})
        self.assertEqual(fm["repair_route"], "consultant@codex")
        self.assertEqual(fm["target"], "role:auditor")  # T-5239: consultant/codex pool role (route stays consultant@codex)

    def test_falls_back_to_any_live(self):  # §6.6
        fm = self._run({"orchestrator"})
        self.assertEqual(fm["target"], "role:engineering_orchestrator")

    def test_pages_but_still_emits_when_nothing_live(self):  # §6.6
        fm = self._run(set())
        self.assertEqual(fm["repair_route"], "page")
        self.assertEqual(fm["target"], "role:system_orchestrator")  # detector owner: never silently dropped
        self.assertEqual(len(self.pages), 1)

    def test_deferred_refuses_cross_account_reroutes_same_account(self):  # §6.18
        fm = self._run({"consultant", "orchestrator"}, evidence=("z" * 99 + "\n") * 400)
        self.assertEqual(fm["evidence_deferred"], "true")
        self.assertEqual(fm["target"], "role:engineering_orchestrator")
        self.assertEqual(self.pages, [])

    def test_deferred_pages_when_no_same_account_live(self):  # §6.18
        fm = self._run({"consultant"}, evidence=("z" * 99 + "\n") * 400)
        self.assertEqual(fm["repair_route"], "page")
        self.assertNotEqual(fm["target"], "consultant")
        self.assertEqual(len(self.pages), 1)


class BT3SeamTests(StoreCase):
    def _delivered(self):
        spec = make_spec()
        for _ in range(3):
            self.obs(spec, B())
        (ep,) = self.episodes(spec)
        return spec, ep["episode_id"]

    def test_mark_recovered_before_repair(self):  # §6.5 seam
        spec, eid = self._delivered()
        es.mark_recovered_before_repair(spec.domain_key, eid, ports=self.ports(), state_dir=self.state.name)
        self.assertTrue(self.doc(spec)["episodes"][eid]["recovered_before_repair"])

    def test_close_stale_and_reopen_links_predecessor_and_emits_fresh_ticket(self):  # §6.5 seam
        spec, eid = self._delivered()
        new = es.close_stale_and_reopen(spec, eid, ports=self.ports(), state_dir=self.state.name)
        d = self.doc(spec)["episodes"]
        self.assertNotEqual(new, eid)
        self.assertEqual((d[eid]["state"], d[eid]["resolve_reason"]), ("RESOLVED", "stale-closed"))
        self.assertEqual(d[new]["predecessor_episode_id"], eid)
        self.assertEqual((d[new]["state"], d[new]["delivery"]["phase"]), ("EMITTED", "VERIFIED"))
        self.assertEqual(len(self.fx.origin_ticket_files()), 2)

    def test_reemit_orphan_reuses_episode_id(self):  # §2b seam
        spec, eid = self._delivered()
        (name,) = self.fx.origin_ticket_files()
        peer = self.fx.root / "peer-del"
        git("clone", str(self.fx.origin), str(peer), cwd=self.fx.root)
        for k, v in (("user.email", "p@p"), ("user.name", "p"), ("commit.gpgsign", "false")):
            git("config", k, v, cwd=peer)
        git("rm", "-q", name, cwd=peer)
        git("commit", "-q", "-m", "groom away", cwd=peer)
        git("push", "-q", "origin", "HEAD:main", cwd=peer)
        git("pull", "-q", "--rebase", "origin", "main", cwd=self.fx.repo)
        self.assertEqual(self.fx.origin_ticket_files(), [])
        es.reemit_orphan(spec, eid, ports=self.ports(), state_dir=self.state.name)
        (ep,) = [e for e in self.episodes(spec) if e["episode_id"] == eid]
        self.assertEqual((ep["state"], ep["delivery"]["phase"]), ("EMITTED", "VERIFIED"))
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)
        self.assertIn(f"episode_id: {eid}", self.origin_ticket_text())


class HeartbeatTests(StoreCase):
    def test_cycle_writes_schema1_heartbeat_and_second_cycle_advances(self):
        spec = make_spec()
        hb = es.heartbeat_path(self.state.name)
        self.assertEqual(hb, Path(self.state.name) / "heartbeat" / "org076-episode-store.json")
        self.assertFalse(hb.exists())
        self.obs(spec, H, self.noop_ports())
        d1 = json.loads(hb.read_text())
        self.assertEqual(d1["schema_version"], 1)
        self.assertEqual(d1["agent"], "org076-episode-store")
        self.assertEqual(d1["pid"], os.getpid())
        self.assertEqual(set(d1), {"agent", "ts", "epoch", "pid", "source", "schema_version"})
        self.obs(spec, H, self.noop_ports())
        d2 = json.loads(hb.read_text())
        self.assertGreater(d2["epoch"], d1["epoch"])
        es.reconcile([spec], ports=self.noop_ports(), state_dir=self.state.name)
        self.assertGreater(json.loads(hb.read_text())["epoch"], d2["epoch"])  # reconcile beats too

    def test_heartbeat_failure_does_not_fail_the_cycle(self):
        spec = make_spec()
        (Path(self.state.name) / "heartbeat").write_text("i am a file, not a dir")
        r = self.obs(spec, B(), self.noop_ports())
        self.assertEqual(r.status, "counted")


if __name__ == "__main__":
    unittest.main()
