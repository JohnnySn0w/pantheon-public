"""ORG-076 BT3b: reconciler sweeps (RFC-004 section 2b, acceptance 6.5), T-4709. Hermetic: a real bare
origin per test, tmp clone/state/drop dir, the REAL ticket.py filer, injected pages. Nothing touches
real /var/lib, $HOME, Telegram or a real remote."""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import episode_store as es
from tools import org076_reconciler as rec
from tools import org076_store_runner as run
from tools import quota_probe_detector as qpd
from tools._episode_fixtures import FakeClock, RepoFixture, git, make_ports

T0 = datetime(2026, 10, 8, 1, 0, 0, tzinfo=timezone.utc)


class Rig(unittest.TestCase):
    def setUp(self):
        self.fx = RepoFixture()
        self.addCleanup(self.fx.cleanup)
        self.tmp = tempfile.TemporaryDirectory(prefix="reconciler-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.drop = self.root / "samples"
        self.drop.mkdir()
        self.state = self.root / "state"
        self.hb = self.root / "hb" / "org076-episode-store.json"
        p = mock.patch.dict(os.environ, {k: self.fx.env[k] for k in (
            "PANTHEON_TICKET_ALLOC_LOCKFILE", "PANTHEON_GIT_MUTATION_LOCKFILE", "PANTHEON_ACTOR")})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop(es.HEARTBEAT_PATH_ENV, None)
        self.pages: list = []
        self.cfg = run.Config(clone=self.fx.repo, drop=self.drop, state_dir=self.state, heartbeat=self.hb)
        self.seq = 0
        self.peers = 0
        self.clock = FakeClock()  # ONE clock across cycles: episode ids never collide

    # -- helpers
    def ports(self, **kw):
        kw.setdefault("heartbeat_path", self.hb)
        kw.setdefault("mirror_url", None)
        kw.setdefault("clock", self.clock)
        return make_ports(self.fx, pages=self.pages, **kw)

    def drop_sample(self, persona="recovery", kind=qpd.PROBE_ERROR, account="claude-fallback"):
        self.seq += 1
        qpd.feed(SimpleNamespace(persona=persona, account=account),
                 qpd.ProbeOutcome(kind, f"20261008T{self.seq:06d}Z", "d"),
                 drop=self.drop, now=T0 + timedelta(seconds=self.seq))

    def cycle(self, **kw):
        return run.run_cycle(self.cfg, self.ports(**kw))

    def domain(self, persona="recovery"):
        return es.load_domain(f"{qpd.DETECTOR}:{persona}", self.state)

    def episodes(self, persona="recovery"):
        return sorted(self.domain(persona)["episodes"].values(), key=lambda e: e["opened_at"] + e["episode_id"])

    def open_episode(self, persona="recovery", account="claude-fallback"):
        """3 unreadable samples -> episode opened, ticket filed + pushed (VERIFIED/EMITTED)."""
        for _ in range(3):
            self.drop_sample(persona, account=account)
        rep = self.cycle()
        self.assertEqual(rep.exit_code, 0, rep)
        (ep,) = [e for e in self.episodes(persona) if e["state"] == es.EMITTED]
        self.assertEqual(ep["delivery"]["phase"], es.VERIFIED)
        return ep

    def origin_edit(self, fn):
        self.peers += 1
        peer = self.root / f"peer-edit-{self.peers}"
        git("clone", str(self.fx.origin), str(peer), cwd=self.root)
        for k, v in (("user.email", "p@p"), ("user.name", "p"), ("commit.gpgsign", "false")):
            git("config", k, v, cwd=peer)
        fn(peer)
        git("add", "-A", cwd=peer)
        git("commit", "-m", f"peer edit {self.peers}", cwd=peer)
        git("push", "origin", "HEAD:main", cwd=peer)

    def ticket_rel(self, ep):
        (rel,) = [r for r in self.fx.origin_ticket_files() if f"{ep['delivery']['ticket_id']}.md" in r]
        return rel

    def resolve_on_origin(self, ep):
        rel = self.ticket_rel(ep)
        self.origin_edit(lambda peer: git("mv", rel, rel.replace("/inbox/", "/done/").replace("/active/", "/done/"),
                                          cwd=peer))

    def origin_text(self, rel):
        return git("show", f"main:{rel}", cwd=self.fx.origin).stdout

    def origin_sha(self):
        return git("rev-parse", "main", cwd=self.fx.origin).stdout.strip()


class StaleEmitted(Rig):
    def test_open_ticket_is_never_closed_or_duplicated(self):
        ep = self.open_episode()
        for _ in range(4):
            self.drop_sample()
        rep = self.cycle()
        self.assertEqual(rep.sweeps.stale_reopened, 0)
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)
        self.assertEqual([e["episode_id"] for e in self.episodes()], [ep["episode_id"]])
        self.assertIsNone(self.episodes()[0].get("stale_watch"))

    def test_resolved_ticket_still_broken_reopens_after_n_nonhealthy_post_resolution(self):
        ep = self.open_episode()
        self.resolve_on_origin(ep)
        rep = self.cycle()  # first sight: arm the cool-down, count nothing yet
        self.assertEqual(rep.sweeps.stale_reopened, 0)
        self.assertEqual(self.episodes()[0]["stale_watch"]["nh_run"], 0)
        for _ in range(es.DEFAULT_N - 1):
            self.drop_sample()
        rep = self.cycle()
        self.assertEqual((rep.sweeps.stale_reopened, self.episodes()[0]["stale_watch"]["nh_run"]), (0, 2))
        self.drop_sample()
        rep = self.cycle()
        self.assertEqual((rep.exit_code, rep.sweeps.stale_reopened), (0, 1))
        old, new = self.episodes()
        self.assertEqual((old["state"], old["resolve_reason"]), (es.RESOLVED, "stale-closed"))
        self.assertFalse(old["healed_under_ticket"])  # stale-closed is NOT a heal
        self.assertNotEqual(new["episode_id"], old["episode_id"])
        self.assertEqual(new["predecessor_episode_id"], old["episode_id"])
        self.assertEqual((new["state"], new["delivery"]["phase"]), (es.EMITTED, es.VERIFIED))
        files = self.fx.origin_ticket_files()
        self.assertEqual(len(files), 2)
        newtxt = self.origin_text(self.ticket_rel(new))
        self.assertIn(f"predecessor_episode_id: {old['episode_id']}", newtxt)
        # idempotent: another cycle changes nothing
        sha = self.origin_sha()
        self.assertEqual(self.cycle().sweeps.mutations, 0)
        self.assertEqual(self.origin_sha(), sha)

    def test_healthy_sample_resets_the_post_resolution_run(self):
        ep = self.open_episode()
        self.resolve_on_origin(ep)
        self.cycle()
        for kind in (qpd.PROBE_ERROR, qpd.PROBE_ERROR, qpd.READ, qpd.PROBE_ERROR, qpd.PROBE_ERROR):
            self.drop_sample(kind=kind)
        rep = self.cycle()
        self.assertEqual((rep.sweeps.stale_reopened, self.episodes()[0]["stale_watch"]["nh_run"]), (0, 2))
        self.drop_sample()
        self.assertEqual(self.cycle().sweeps.stale_reopened, 1)

    def test_ticket_reopened_disarms_the_watch(self):
        ep = self.open_episode()
        self.resolve_on_origin(ep)
        self.cycle()
        self.assertIsNotNone(self.episodes()[0]["stale_watch"])
        rel = self.ticket_rel(ep)
        self.origin_edit(lambda peer: git("mv", rel, rel.replace("/done/", "/active/"), cwd=peer))
        self.cycle()
        self.assertIsNone(self.episodes()[0].get("stale_watch"))

    def test_chain_depth_three_pages_once_and_never_blocks_the_reopen(self):
        ep = self.open_episode()
        for round_ in range(3):
            self.resolve_on_origin(ep)
            self.cycle()
            for _ in range(3):
                self.drop_sample()
            rep = self.cycle()
            self.assertEqual(rep.sweeps.stale_reopened, 1)
            (ep,) = [e for e in self.episodes() if e["state"] == es.EMITTED]
        chain = [p for p in self.pages if "reopened" in p[0]]
        self.assertEqual(len(chain), 1)
        self.assertIn("3 times", chain[0][0])
        self.assertEqual(len(self.fx.origin_ticket_files()), 4)  # the reopen was not blocked


class HealedUnderTicket(Rig):
    def heal(self, ep):
        self.drop_sample(kind=qpd.READ)
        self.drop_sample(kind=qpd.READ)

    def test_flag_set_on_open_ticket_one_file_one_line_and_ticket_stays_open(self):
        ep = self.open_episode()
        rel = self.ticket_rel(ep)
        before_text = self.origin_text(rel)
        self.assertIn("recovered_before_repair: false", before_text)
        self.assertIn("evidence_snapshot: |", before_text)
        before_sha = self.origin_sha()
        self.heal(ep)
        rep = self.cycle()
        self.assertEqual((rep.exit_code, rep.sweeps.healed_flagged), (0, 1))
        after = self.origin_text(rel)
        self.assertEqual(self.ticket_rel(ep), rel)  # still where it was: not closed, not moved
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)  # no repair action / second ticket
        d = git("diff", "--numstat", before_sha, "main", cwd=self.fx.origin).stdout.split("\n")
        self.assertEqual([ln.split("\t")[2] for ln in d if ln], [rel])  # exactly ONE file
        diff = git("diff", "-U0", before_sha, "main", "--", rel, cwd=self.fx.origin).stdout.splitlines()
        added = [ln[1:] for ln in diff if ln.startswith("+") and not ln.startswith("+++")]
        removed = [ln[1:] for ln in diff if ln.startswith("-") and not ln.startswith("---")]
        self.assertEqual(removed, ["recovered_before_repair: false"])
        self.assertEqual(len(added), 2)
        self.assertEqual(added[0], "recovered_before_repair: true")
        self.assertRegex(added[1], r"^- \d{4}-\d\d-\d\dT[\d:]+Z \[ep-test\] note")
        self.assertIn(ep["episode_id"], added[1])
        subject = git("log", "-1", "--format=%s", "main", cwd=self.fx.origin).stdout.strip()
        self.assertTrue(subject.startswith(f"tickets: annotate {ep['delivery']['ticket_id']} — "), subject)
        self.assertNotIn("HERME" + "S.md", subject)
        (done,) = [e for e in self.episodes() if e["state"] == es.RESOLVED]
        self.assertEqual((done["recovered_before_repair"], done["rbr_applied"]), (True, "flagged"))

    def test_every_other_byte_unchanged_and_ticket_lint_clean(self):
        from tools import staged_content_gate, ticket_lint
        ep = self.open_episode()
        rel = self.ticket_rel(ep)
        before = self.origin_text(rel)
        self.heal(ep)
        self.cycle()
        after = self.origin_text(rel)
        b_lines, a_lines = before.splitlines(keepends=True), after.splitlines(keepends=True)
        self.assertEqual(len(a_lines), len(b_lines) + 1)
        flag = b_lines.index("recovered_before_repair: false\n")
        self.assertEqual(a_lines[:flag], b_lines[:flag])
        self.assertEqual(a_lines[flag], "recovered_before_repair: true\n")
        self.assertEqual(a_lines[flag + 1:-1], b_lines[flag + 1:])  # evidence_snapshot block etc. identical
        self.assertTrue(a_lines[-1].startswith("- "))  # one appended transition entry
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            results = {}
            for label, txt in (("before", before), ("after", after)):
                d = root / label / "tickets" / "inbox"
                d.mkdir(parents=True)
                p = d / Path(rel).name
                p.write_text(txt)
                results[label] = ticket_lint.detect_malformations(p, repo_root=root / label)
            self.assertEqual(results["after"].action, results["before"].action)
            self.assertEqual(results["after"].description, results["before"].description)
            self.assertEqual(results["after"].action, ticket_lint.LintAction.CLEAN)
            self.assertEqual(staged_content_gate.content_gate_findings(
                [rel], read_staged=lambda p: after.encode(), read_head=lambda p: before.encode(),
                rename_source=lambda p: None, tickets_root=root / "after" / "tickets", warn=lambda m: None), [])

    def test_already_true_on_origin_is_a_noop_and_next_cycle_guard_passes_level(self):
        ep = self.open_episode()
        self.heal(ep)
        self.cycle()
        sha = self.origin_sha()
        # simulate a lost persist: forget that it was applied; origin already carries the flag
        doc = self.domain()
        for e in doc["episodes"].values():
            e.pop("rbr_applied", None)
        es._atomic_write(es.store_path(f"{qpd.DETECTOR}:recovery", self.state), doc)
        rep = self.cycle()
        self.assertEqual((rep.refused, rep.exit_code, rep.sweeps.healed_settled), (None, 0, 1))
        self.assertEqual(self.origin_sha(), sha)  # nothing pushed
        # the NEXT cycle's guard: no refusal, clone fast-forwarded level with origin, nothing ahead
        rep = self.cycle()
        self.assertIsNone(rep.refused)
        head = git("rev-parse", "HEAD", cwd=self.fx.repo).stdout.strip()
        self.assertEqual(head, self.origin_sha())
        self.assertEqual(git("rev-list", "origin/main..HEAD", cwd=self.fx.repo).stdout.strip(), "")

    def test_guard_passes_after_flag_push_and_clone_ffs_level(self):
        ep = self.open_episode()
        self.heal(ep)
        self.cycle()
        self.assertNotEqual(git("rev-parse", "HEAD", cwd=self.fx.repo).stdout.strip(), self.origin_sha())
        self.assertEqual(git("rev-list", "origin/main..HEAD", cwd=self.fx.repo).stdout.strip(), "")  # none ahead
        rep = self.cycle()
        self.assertIsNone(rep.refused)
        self.assertEqual(rep.exit_code, 0)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.fx.repo).stdout.strip(), self.origin_sha())

    def test_push_failure_leaves_nothing_ahead_and_converges_next_cycle(self):
        ep = self.open_episode()
        self.heal(ep)
        real = es._default_push
        calls = []

        def flaky(repo, refspec="HEAD:main", mirror_url=None):
            calls.append(refspec)
            return es.PushResult(ok=False, detail="network down") if len(calls) == 1 else real(repo, refspec, None)

        with mock.patch.object(es, "_default_push", flaky):
            rep = self.cycle()
        self.assertEqual((rep.exit_code, rep.sweeps.healed_flagged), (4, 0))
        self.assertIn("network down", rep.errors[0])
        self.assertEqual(git("rev-list", "origin/main..HEAD", cwd=self.fx.repo).stdout.strip(), "")
        self.assertEqual(git("status", "--porcelain", cwd=self.fx.repo).stdout.strip(), "")
        self.assertIn("recovered_before_repair: false", self.origin_text(self.ticket_rel(ep)))
        with mock.patch.object(es, "_default_push", flaky):
            rep = self.cycle()
        self.assertEqual((rep.exit_code, rep.sweeps.healed_flagged), (0, 1))
        self.assertIn("recovered_before_repair: true", self.origin_text(self.ticket_rel(ep)))

    def test_nonff_rebuilds_against_new_origin_bounded(self):
        ep = self.open_episode()
        self.heal(ep)
        real = es._default_push
        n = []

        def racing(repo, refspec="HEAD:main", mirror_url=None):
            if not n:
                n.append(1)
                self.fx.peer_advance("race")  # origin moves under the first push
            return real(repo, refspec, None)

        with mock.patch.object(es, "_default_push", racing):
            rep = self.cycle()
        self.assertEqual((rep.exit_code, rep.sweeps.healed_flagged), (0, 1))
        self.assertIn("recovered_before_repair: true", self.origin_text(self.ticket_rel(ep)))
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)

    def test_ticket_already_resolved_is_settled_without_mutation(self):
        ep = self.open_episode()
        self.heal(ep)
        self.resolve_on_origin(ep)
        sha = self.origin_sha()
        rep = self.cycle()
        self.assertEqual((rep.sweeps.healed_flagged, rep.sweeps.healed_settled), (0, 1))
        self.assertEqual(self.origin_sha(), sha)
        (done,) = [e for e in self.episodes() if e["state"] == es.RESOLVED]
        self.assertEqual(done["rbr_applied"], "ticket-closed")


class Orphans(Rig):
    def test_deleted_ticket_is_reemitted_under_the_same_episode_id_exactly_once(self):
        ep = self.open_episode()
        rel = self.ticket_rel(ep)
        # a body-pasted id in an unrelated ticket must not suppress the re-emit (recovery note a)
        decoy = rel.rsplit("/", 1)[0] + "/T-9999.md"
        self.origin_edit(lambda peer: (git("rm", "-q", rel, cwd=peer), (peer / decoy).write_text(
            f"---\nid: T-9999\n---\n\n# decoy\n\nepisode_id: {ep['episode_id']}\n")))
        rep = self.cycle()
        self.assertEqual((rep.exit_code, rep.sweeps.orphans_reemitted), (0, 1))
        files = [f for f in self.fx.origin_ticket_files() if f != decoy]
        self.assertEqual(len(files), 1)
        self.assertIn(f"episode_id: {ep['episode_id']}", self.origin_text(files[0]))
        (cur,) = [e for e in self.episodes() if e["state"] == es.EMITTED]
        self.assertEqual((cur["episode_id"], cur["delivery"]["phase"]), (ep["episode_id"], es.VERIFIED))
        self.assertEqual(self.cycle().sweeps.mutations, 0)  # idempotent
        self.assertEqual(len([f for f in self.fx.origin_ticket_files() if f != decoy]), 1)

    def test_ticket_moved_to_done_is_not_an_orphan(self):
        ep = self.open_episode()
        self.resolve_on_origin(ep)
        sha = self.origin_sha()
        rep = self.cycle()
        self.assertEqual(rep.sweeps.orphans_reemitted, 0)
        self.assertEqual(self.origin_sha(), sha)

    def test_unknown_corpus_fails_closed_no_mutation(self):
        ep = self.open_episode()
        rel = self.ticket_rel(ep)
        self.origin_edit(lambda peer: git("rm", "-q", rel, cwd=peer))
        self.fx.origin.rename(self.root / "origin-away.git")  # fetch fails: view is stale
        try:
            rep = self.cycle()
        finally:
            (self.root / "origin-away.git").rename(self.fx.origin)
        self.assertFalse(rep.fetch_ok)
        self.assertIsNone(rep.sweeps)
        self.assertEqual(rep.exit_code, 4)
        # and a git failure inside the sweeps themselves reports UNKNOWN, mutating nothing
        bad = SimpleNamespace(_git=lambda *a, **k: SimpleNamespace(returncode=128, stdout="", stderr="boom"))
        out = rec.run_sweeps([qpd.spec_for(SimpleNamespace(persona="recovery", account="claude-fallback"))],
                             ports=self.ports(git=bad), state_dir=self.state)
        self.assertEqual((out.skipped_unknown, out.mutations), (1, 0))

    def test_mutation_cap_defers_and_resumes_next_cycle(self):
        eps = [self.open_episode("recovery"), self.open_episode("specialist", account="claude")]
        rels = [self.ticket_rel(e) for e in eps]
        self.origin_edit(lambda peer: [git("rm", "-q", r, cwd=peer) for r in rels])
        specs = [qpd.spec_for(SimpleNamespace(persona=p, account=a))
                 for p, a in (("recovery", "claude-fallback"), ("specialist", "claude"))]
        git("fetch", "origin", "main", cwd=self.fx.repo)
        git("merge", "--ff-only", "origin/main", cwd=self.fx.repo)
        out = rec.run_sweeps(specs, ports=self.ports(), state_dir=self.state, max_mutations=1)
        self.assertEqual((out.orphans_reemitted, out.deferred), (1, 1))
        out = rec.run_sweeps(specs, ports=self.ports(), state_dir=self.state, max_mutations=1)
        self.assertEqual((out.orphans_reemitted, out.deferred), (1, 0))
        self.assertEqual(len(self.fx.origin_ticket_files()), 2)


class RefusedCycle(Rig):
    def test_sweeps_do_not_run_on_a_refused_cycle(self):
        self.open_episode()
        (self.fx.repo / "stray.txt").write_text("x")
        rep = self.cycle()
        self.assertEqual(rep.exit_code, 3)
        self.assertIsNone(rep.sweeps)


if __name__ == "__main__":
    unittest.main()
