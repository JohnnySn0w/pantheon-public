"""ORG-076 BT3a: recovery-side single-store runner (T-4720). Hermetic: tmp drop dir, tmp clone + bare
tmp origin, injected page, tmp heartbeat/state. Nothing touches real /var/lib, $HOME, Telegram,
real tickets or a real push."""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import re
import stat
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import episode_store as es
from tools import org076_store_runner as run
from tools import quota_probe_detector as qpd
from tools._episode_fixtures import FakeClock, RepoFixture, git, make_ports

CTX = mp.get_context("fork")
ROOT = Path(__file__).resolve().parent.parent
SYSTEMD = ROOT / "tools" / "systemd"
SVC, TMR = "pantheon-org076-store-runner.service", "pantheon-org076-store-runner.timer"
T0 = datetime(2026, 10, 8, 1, 0, 0, tzinfo=timezone.utc)


class Rig(unittest.TestCase):
    def setUp(self):
        self.fx = RepoFixture()
        self.addCleanup(self.fx.cleanup)
        self.tmp = tempfile.TemporaryDirectory(prefix="runner-")
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

    # -- helpers
    def ports(self, **kw):
        kw.setdefault("heartbeat_path", self.hb)
        return make_ports(self.fx, pages=self.pages, **kw)

    def drop_sample(self, persona="recovery", kind=qpd.PROBE_ERROR, run_id="auto", account="claude-fallback"):
        self.seq += 1
        if run_id == "auto":
            run_id = f"20261008T{self.seq:06d}Z"
        qpd.feed(SimpleNamespace(persona=persona, account=account), qpd.ProbeOutcome(kind, run_id, "d"),
                 drop=self.drop, now=T0 + timedelta(seconds=self.seq))

    def cycle(self, **kw):
        return run.run_cycle(self.cfg, self.ports(**kw))

    def domain(self, persona="recovery"):
        return es.load_domain(f"{qpd.DETECTOR}:{persona}", self.state)

    def n(self, persona="recovery"):
        r = self.domain(persona)["runs"].get(qpd.DETECTOR)
        return r["n"] if r else 0

    def dropped(self):
        return sorted(p.name for p in self.drop.iterdir() if p.is_file())


class Ingest(Rig):
    def test_ingests_in_name_order_deletes_after_and_opens_one_ticket(self):
        for _ in range(3):
            self.drop_sample()
        seen = []
        real = es.observe

        def spy(spec, verdict, **kw):
            if spec.detector != qpd.DETECTOR:  # BT3b: meta-detector observes pass straight through
                return real(spec, verdict, **kw)
            seen.append(kw["sample_key"])
            self.assertEqual(len(self.dropped()), 3 - len(seen) + 1)  # file still present during observe
            return real(spec, verdict, **kw)

        with mock.patch.object(es, "observe", spy):
            rep = self.cycle()
        self.assertEqual(seen, sorted(seen))
        self.assertEqual((rep.ingested, rep.exit_code), (3, 0))
        self.assertEqual(self.dropped(), [])
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)
        (ep,) = self.domain()["episodes"].values()
        self.assertEqual(ep["delivery"]["phase"], es.VERIFIED)

    def test_same_run_id_twice_counts_once(self):
        self.drop_sample(run_id="B1")
        self.drop_sample(run_id="B1")
        rep = self.cycle()
        self.assertEqual((rep.ingested, rep.deduped, self.n()), (1, 1, 1))

    def test_same_run_id_from_two_personas_counts_in_both_domains(self):
        self.drop_sample(persona="recovery", run_id="B1")
        self.drop_sample(persona="specialist", run_id="B1", account="claude")
        rep = self.cycle()
        self.assertEqual((rep.ingested, rep.deduped, self.n("recovery"), self.n("specialist")), (2, 0, 1, 1))

    def test_missing_drop_dir_is_clean_noop_and_still_heartbeats(self):
        self.cfg.drop = self.root / "nope"
        rep = self.cycle()
        self.assertEqual(rep.exit_code, 0)
        self.assertTrue(self.hb.is_file())

    def test_busy_suppressed_keeps_file_and_holds_later_same_persona_files(self):
        self.drop_sample(run_id="B1")
        self.drop_sample(run_id="B2")
        path = es.store_path(f"{qpd.DETECTOR}:recovery", self.state)
        with es._DomainLock(path, 5):
            rep = self.cycle(lock_timeout=0.05)
        self.assertEqual(len(self.dropped()), 2)
        self.assertEqual((rep.ingested, rep.kept), (0, 2))
        rep = self.cycle()
        self.assertEqual((rep.ingested, self.n(), self.dropped()), (2, 2, []))

    def test_malformed_files_are_quarantined_paged_and_do_not_wedge(self):
        (self.drop / "00000000T000000000000__x__nr__1.json").write_text("{not json")
        (self.drop / "00000000T000000000001__x__nr__2.json").write_text(
            json.dumps({"schema": 99, "kind": "quota-probe-sample"}))
        (self.drop / "00000000T000000000002__x__nr__3.json").write_text(
            json.dumps({"schema": 1, "kind": "quota-probe-sample", "detector": qpd.DETECTOR, "persona": "../etc",
                        "account": "a", "outcome_kind": "read", "run_id": None, "sample_id": "s"}))
        self.drop_sample(run_id="B1")
        rep = self.cycle()
        self.assertEqual((rep.rejected, rep.ingested, self.n()), (3, 1, 1))
        self.assertEqual(len(list((self.drop / "rejected").iterdir())), 3)
        self.assertEqual(self.dropped(), [])
        self.assertEqual(len(self.pages), 3)
        self.assertEqual(self.cycle().rejected, 0)  # nothing re-paged next cycle

    def test_oversized_file_rejected(self):
        (self.drop / "00000000T000000000000__x__nr__1.json").write_text("x" * (run.MAX_SAMPLE_BYTES + 1))
        self.assertEqual(self.cycle().rejected, 1)

    def test_non_json_hidden_and_subdir_entries_ignored(self):
        (self.drop / "notes.txt").write_text("hi")
        (self.drop / ".hidden.json").write_text("{bad")
        (self.drop / "sub").mkdir()
        rep = self.cycle()
        self.assertEqual((rep.rejected, rep.ingested), (0, 0))
        self.assertTrue((self.drop / "notes.txt").exists())

    def test_observe_exception_keeps_file_holds_persona_but_not_others(self):
        self.drop_sample(persona="recovery", run_id="W1")
        self.drop_sample(persona="recovery", run_id="W2")
        self.drop_sample(persona="specialist", run_id="A1", account="claude")
        real = es.observe

        def flaky(spec, verdict, **kw):
            if spec.domain_key.endswith(":recovery"):
                raise es.StoreCorruptionError("boom")
            return real(spec, verdict, **kw)

        with mock.patch.object(es, "observe", flaky):
            rep = self.cycle()
        self.assertEqual((rep.ingested, rep.kept, rep.exit_code), (1, 2, 4))
        self.assertEqual(len(self.dropped()), 2)
        self.assertEqual(len(self.pages), 1)
        with mock.patch.object(es, "observe", flaky):
            self.cycle()
        self.assertEqual(len(self.pages), 1, "page deduped across cycles")

    def test_stale_writer_tmp_reaped_only_when_old_regular_and_prefixed(self):
        old = time.time() - 2 * 3600
        stale = self.drop / f"{qpd.TMP_PREFIX}1-aa"
        fresh = self.drop / f"{qpd.TMP_PREFIX}2-bb"
        other = self.drop / ".someone-elses-hidden"
        link = self.drop / f"{qpd.TMP_PREFIX}3-cc"
        for p in (stale, fresh, other):
            p.write_text("x")
        link.symlink_to(other)
        for p in (stale, other):
            os.utime(p, (old, old))
        os.utime(link, (old, old), follow_symlinks=False)
        rep = self.cycle()
        self.assertEqual(rep.reaped, 1)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists() and other.exists() and link.is_symlink())


def _child(target):
    proc = CTX.Process(target=target)
    proc.start()
    proc.join(120)
    return proc.exitcode


class CrashSafety(Rig):
    def test_kill_between_observe_durable_and_file_delete_does_not_double_count(self):
        self.drop_sample(run_id="B1")

        def target():
            def kill(point):
                if point == "after_observe_before_unlink":
                    os._exit(137)
            run.run_cycle(self.cfg, self.ports(), kill_hook=kill)
            os._exit(0)

        self.assertEqual(_child(target), 137)
        self.assertEqual(len(self.dropped()), 1, "file survives the crash")
        self.assertEqual(self.n(), 1, "observe was durable")
        rep = self.cycle()  # restart: replay
        self.assertEqual((rep.deduped, rep.ingested, self.n(), self.dropped()), (1, 0, 1, []))

    def test_kill_before_observe_is_durable_loses_nothing_and_counts_once(self):
        self.drop_sample(run_id="B1")

        def target():
            def die(*a, **k):
                os._exit(137)
            with mock.patch.object(es, "observe", die):
                run.run_cycle(self.cfg, self.ports())
            os._exit(0)

        self.assertEqual(_child(target), 137)
        self.assertEqual((len(self.dropped()), self.n()), (1, 0))
        rep = self.cycle()
        self.assertEqual((rep.ingested, rep.deduped, self.n(), self.dropped()), (1, 0, 1, []))

    def test_crash_replay_of_a_whole_batch_never_overcounts_the_episode_threshold(self):
        for _ in range(2):
            self.drop_sample()
        self.drop_sample(run_id="B3")

        def target():
            seen = []

            def kill(point):
                seen.append(point)
                if len(seen) == 2:  # die after the second observe, before its unlink
                    os._exit(137)
            run.run_cycle(self.cfg, self.ports(), kill_hook=kill)
            os._exit(0)

        self.assertEqual(_child(target), 137)
        self.cycle()
        (ep,) = self.domain()["episodes"].values()
        self.assertEqual(ep["counters"]["non_healthy"], 3)  # exactly N, not N+1
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)


class Guard(Rig):
    def test_dirty_clone_refused_paged_once_no_heartbeat_exit_3(self):
        (self.fx.repo / "stray.txt").write_text("x")
        self.drop_sample()
        rep = self.cycle()
        self.assertEqual(rep.exit_code, run.EXIT_REFUSED)
        self.assertIn("dirty", rep.refused)
        self.cycle()
        self.assertEqual(len(self.pages), 1)
        self.assertFalse(self.hb.exists())
        self.assertEqual(len(self.dropped()), 1, "nothing ingested while refused")

    def test_clean_again_resumes_and_clears_page_dedupe(self):
        (self.fx.repo / "stray.txt").write_text("x")
        self.cycle()
        (self.fx.repo / "stray.txt").unlink()
        self.assertEqual(self.cycle().exit_code, 0)
        (self.fx.repo / "stray.txt").write_text("x")
        self.cycle()
        self.assertEqual(len(self.pages), 2)

    def test_foreign_ahead_commit_refused(self):
        (self.fx.repo / "mine.txt").write_text("x")
        git("add", "-A", cwd=self.fx.repo)
        git("commit", "-q", "-m", "not the store's", cwd=self.fx.repo)
        rep = self.cycle()
        self.assertEqual(rep.exit_code, run.EXIT_REFUSED)
        self.assertIn("does not own", rep.refused)

    def test_ff_only_pull_when_behind(self):
        self.fx.peer_advance()
        self.cycle()
        head = git("rev-parse", "HEAD", cwd=self.fx.repo).stdout
        self.assertEqual(head, git("rev-parse", "origin/main", cwd=self.fx.repo).stdout)
        self.assertTrue((self.fx.repo / "peer.txt").exists())

    def test_owned_unpushed_episode_commit_is_accepted_then_delivered(self):
        for _ in range(3):
            self.drop_sample()
        down = lambda repo: es.PushResult(ok=False, detail="network down")
        self.cycle(pusher=down)
        (ep,) = self.domain()["episodes"].values()
        self.assertEqual(ep["delivery"]["phase"], es.PUSH_INFLIGHT)
        self.assertEqual(len(self.fx.origin_ticket_files()), 0)
        rep = self.cycle()  # owned local-ahead commit: guard passes, push succeeds
        self.assertEqual(rep.exit_code, 0)
        (ep,) = self.domain()["episodes"].values()
        self.assertEqual(ep["delivery"]["phase"], es.VERIFIED)
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)

    def _crash_in_commit_inflight(self, ticket_kill):
        for _ in range(3):
            self.drop_sample()
        fx = self.fx

        def target():
            ports = self.ports(kill_at="after_file_before_local_commit",
                               filer=lambda argv: fx.ticket_cli(argv, kill=ticket_kill))
            run.run_cycle(self.cfg, ports)
            os._exit(0)

        self.assertEqual(_child(target), 137)

    def test_crash_residue_untracked_ticket_is_explained_and_cleaned(self):
        self._crash_in_commit_inflight("before_add")
        self.assertTrue(git("status", "--porcelain", cwd=self.fx.repo).stdout.strip(), "dirty residue exists")
        rep = self.cycle()
        self.assertIsNone(rep.refused)
        (ep,) = self.domain()["episodes"].values()
        self.assertEqual(ep["delivery"]["phase"], es.VERIFIED)
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)

    def test_crash_residue_staged_ticket_is_explained(self):
        self._crash_in_commit_inflight("after_add")
        rep = self.cycle()
        self.assertIsNone(rep.refused)
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)

    def test_residue_for_an_unknown_episode_is_not_explained(self):
        self._crash_in_commit_inflight("before_add")
        for p in list((self.fx.repo / "tickets" / "inbox").glob("T-*.md")):
            p.write_text("---\nid: T-1\nepisode_id: ep-someone-else\n---\n")
        rep = self.cycle()
        self.assertIn("dirty", rep.refused or "")

    def test_fetch_failure_is_partial_not_refusal_and_still_ingests(self):
        git("remote", "set-url", "origin", str(self.fx.root / "gone.git"), cwd=self.fx.repo)
        self.drop_sample()
        rep = self.cycle(fetch=False)
        self.assertEqual((rep.exit_code, rep.ingested), (run.EXIT_PARTIAL, 1))


class OwnershipAnchoring(Guard):
    """BT3a review B1: 'owned' must be anchored (frontmatter, only-ticket files, single parent)."""

    def _inflight_clean(self):
        self._crash_in_commit_inflight("before_add")
        git("reset", "-q", "--hard", cwd=self.fx.repo)
        git("clean", "-qfd", cwd=self.fx.repo)
        (eid,) = [e["episode_id"] for e in self.domain()["episodes"].values()]
        self.assertEqual(git("status", "--porcelain", cwd=self.fx.repo).stdout.strip(), "")
        self.assertEqual(self.domain()["episodes"][eid]["delivery"]["phase"], es.COMMIT_INFLIGHT)
        return eid

    def _foreign(self, eid, *, frontmatter, extra):
        t = self.fx.repo / "tickets" / "inbox" / "T-99.md"
        if frontmatter:
            t.write_text(f"---\nid: T-99\nepisode_id: {eid}\n---\n\nx\n")
        else:
            t.write_text(f"---\nid: T-99\n---\n\n## Context\n\nepisode_id: {eid}\n")
        for name in extra:
            (self.fx.repo / name).write_text("print('evil')\n")
        git("add", "-A", cwd=self.fx.repo)
        git("commit", "-q", "-m", "foreign", cwd=self.fx.repo)
        return git("rev-parse", "HEAD", cwd=self.fx.repo).stdout.strip()

    def test_body_pasted_id_plus_stray_file_is_refused(self):
        eid = self._inflight_clean()
        self._foreign(eid, frontmatter=False, extra=["evil.py"])
        rep = self.cycle()
        self.assertEqual(rep.exit_code, run.EXIT_REFUSED)
        self.assertIn("does not own", rep.refused)

    def test_frontmatter_id_plus_stray_non_ticket_file_is_refused(self):
        eid = self._inflight_clean()
        self._foreign(eid, frontmatter=True, extra=["evil.py"])
        self.assertEqual(self.cycle().exit_code, run.EXIT_REFUSED)

    def test_body_pasted_id_alone_is_refused(self):
        eid = self._inflight_clean()
        self._foreign(eid, frontmatter=False, extra=[])
        self.assertEqual(self.cycle().exit_code, run.EXIT_REFUSED)

    def test_merge_commit_is_refused_and_not_a_store_commit(self):
        eid = self._inflight_clean()
        repo = self.fx.repo
        git("checkout", "-q", "-b", "side", cwd=repo)
        sha = self._foreign(eid, frontmatter=True, extra=[])  # a clean single-ticket commit...
        git("checkout", "-q", "main", cwd=repo)
        (repo / "other.txt").write_text("x")
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "main side", cwd=repo)
        git("merge", "-q", "--no-ff", "-m", "merge", "side", cwd=repo)
        merge = git("rev-parse", "HEAD", cwd=repo).stdout.strip()
        self.assertFalse(run._is_store_commit(merge, eid, repo, es._git_utils))
        self.assertEqual(self.cycle().exit_code, run.EXIT_REFUSED)
        self.assertTrue(run._is_store_commit(sha, eid, repo, es._git_utils))  # the plain ticket commit alone qualifies

    def test_legit_owned_local_commit_and_commit_inflight_residue_still_accepted(self):
        # (the pre-existing Guard tests re-run in this subclass too; this names the positive control)
        for _ in range(3):
            self.drop_sample()
        self.cycle(pusher=lambda repo: es.PushResult(ok=False, detail="down"))
        (ep,) = self.domain()["episodes"].values()
        sha = ep["delivery"]["commit_sha"]
        self.assertTrue(run._is_store_commit(sha, ep["episode_id"], self.fx.repo, es._git_utils))
        self.assertEqual(self.cycle().exit_code, 0)


class CycleScopedVerifyLoop(Rig):
    """BT3a review B2: the verify-loop counts once per RUNNER cycle, not once per observe()/drive."""

    def _stuck(self):
        failing = lambda argv: SimpleNamespace(returncode=1, stdout="", stderr="nope")
        for _ in range(3):
            self.drop_sample()
        self.cycle(filer=failing)
        path = es.store_path(f"{qpd.DETECTOR}:recovery", self.state)
        doc = es._load(path, f"{qpd.DETECTOR}:recovery")
        (ep,) = doc["episodes"].values()
        ep["delivery"]["phase"] = es.PUSHED
        es._save(path, doc)
        return ep["episode_id"]

    def loops(self, eid):
        return self.domain()["episodes"][eid]["delivery"].get("verify_loops", 0)

    def test_one_cycle_with_many_queued_files_counts_once_and_does_not_page(self):
        eid = self._stuck()
        for _ in range(6):
            self.drop_sample()
        rep = self.cycle()
        self.assertEqual(rep.ingested, 6)
        self.assertEqual(self.loops(eid), 1)
        self.assertEqual(self.pages, [])

    def test_threshold_cycles_page_exactly_once_then_verified_resets(self):
        eid = self._stuck()
        for i in range(es.VERIFY_LOOP_PAGE_THRESHOLD - 1):
            self.drop_sample()
            self.cycle()
            self.assertEqual(self.pages, [], f"cycle {i + 1}")
        self.drop_sample()
        self.cycle()
        self.assertEqual(len(self.pages), 1)
        for _ in range(3):
            self.drop_sample()
            self.cycle()
        self.assertEqual(len(self.pages), 1)
        t = self.fx.repo / "tickets" / "inbox" / "T-77.md"
        t.write_text(f"---\nid: T-77\nepisode_id: {eid}\n---\n\n# x\n")
        git("add", "-A", cwd=self.fx.repo)
        git("commit", "-q", "-m", "file", cwd=self.fx.repo)
        git("push", "-q", "origin", "HEAD:main", cwd=self.fx.repo)
        self.cycle()
        d = self.domain()["episodes"][eid]["delivery"]
        self.assertEqual((d["phase"], d["verify_loops"], d["verify_loop_paged"]), (es.VERIFIED, 0, False))

    def test_no_token_keeps_once_per_drive_behaviour(self):
        eid = self._stuck()
        for _ in range(2):
            es.reconcile([qpd.spec_for(SimpleNamespace(persona="recovery", account="claude-fallback"))],
                         ports=self.ports(), state_dir=self.state)
        self.assertEqual(self.loops(eid), 2)


class UnreadableHoldsPersona(Rig):
    def test_unreadable_file_holds_later_files_of_same_persona_only(self):
        self.drop_sample(persona="recovery", run_id="W1")
        self.drop_sample(persona="recovery", run_id="W2")
        self.drop_sample(persona="specialist", run_id="A1", account="claude")
        first = sorted(p for p in self.drop.iterdir() if "__recovery__" in p.name)[0]
        real = Path.read_bytes

        def rb(self_, *a, **k):
            if self_ == first:
                raise PermissionError("denied")
            return real(self_, *a, **k)

        with mock.patch.object(Path, "read_bytes", rb):
            rep = self.cycle()
        self.assertEqual((rep.ingested, rep.kept, rep.exit_code), (1, 2, 4))
        self.assertEqual(self.n("recovery"), 0)
        self.assertEqual(len(self.pages), 1)

    def test_unparseable_name_holds_nothing_but_pages(self):
        bad = self.drop / "garbage.json"
        bad.write_text("x")
        self.drop_sample(run_id="W1")
        real = Path.read_bytes

        def rb(self_, *a, **k):
            if self_ == bad:
                raise PermissionError("denied")
            return real(self_, *a, **k)

        with mock.patch.object(Path, "read_bytes", rb):
            rep = self.cycle()
        self.assertEqual((rep.ingested, len(self.pages)), (1, 1))


class CloneAssertion(Rig):
    def test_mismatch_refused(self):
        with self.assertRaises(run.CloneMismatch):
            run.assert_clone(self.cfg)  # script checkout (this worktree) != tmp clone

    def test_all_agree_passes(self):
        script = self.fx.repo / "tools" / "org076_store_runner.py"
        run.assert_clone(self.cfg, script=script, repo_root=self.fx.repo)

    def test_repo_root_disagreement_refused(self):
        script = self.fx.repo / "tools" / "org076_store_runner.py"
        with self.assertRaises(run.CloneMismatch):
            run.assert_clone(self.cfg, script=script, repo_root=ROOT)

    def test_non_git_dir_refused(self):
        d = self.root / "plain"
        (d / "tools").mkdir(parents=True)
        with self.assertRaises(run.CloneMismatch):
            run.assert_clone(run.Config(clone=d, drop=self.drop), script=d / "tools" / "x.py", repo_root=d)

    def test_main_exits_2_on_mismatch_and_never_sets_state_dir_env(self):
        before = os.environ.get("PANTHEON_STATE_DIR")
        rc = run.main(["--run", "--clone", str(self.fx.repo), "--drop-dir", str(self.drop)])
        self.assertEqual(rc, run.EXIT_MISMATCH)
        self.assertEqual(os.environ.get("PANTHEON_STATE_DIR"), before)

    def test_default_clone_resolves_from_home_not_hardcoded(self):
        with mock.patch.dict(os.environ, {"HOME": "/home/somebody"}):
            self.assertEqual(run.default_clone(),
                             Path("/home/somebody/.local/state/pantheon/org076/pantheon-config"))


class Heartbeat(Rig):
    def test_heartbeat_written_to_override_path_mode_0644_not_state_dir(self):
        self.cycle()
        self.assertEqual(stat.S_IMODE(self.hb.stat().st_mode), 0o644)
        self.assertFalse((self.state / "heartbeat").exists())


class DarkUnits(unittest.TestCase):
    """The unit pair ships DARK: present in the repo, installed/enabled nowhere, in no install path."""

    def setUp(self):
        self.svc = (SYSTEMD / SVC).read_text()
        self.tmr = (SYSTEMD / TMR).read_text()

    def test_service_contract(self):
        self.assertRegex(self.svc, r"(?m)^OnFailure=pantheon-unit-failure-alert@%n\.service\s*$")
        active = "\n".join(ln for ln in self.svc.splitlines() if not ln.lstrip().startswith("#"))
        self.assertNotIn("PANTHEON_STATE_DIR", active)  # comments may explain why; no active line sets it
        self.assertRegex(self.svc, r"(?m)^Type=oneshot$")
        m = re.search(r"(?m)^ExecStart=(.+)$", self.svc)
        self.assertIn("/.local/state/pantheon/org076/pantheon-config/tools/org076_store_runner.py", m.group(1))
        self.assertIn("--heartbeat-path /var/lib/pantheon/org076/heartbeat/org076-episode-store.json", m.group(1))
        self.assertNotIn("/home/", self.svc.replace("file:///home/%u", ""))  # no hard-coded home

    def test_execstart_runs_under_sg_systemd_journal(self):
        """T-4709 recovery steward change: user-mode systemd cannot set SupplementaryGroups, and
        recovery's user manager lacks systemd-journal, so without the sg wrap the freshness
        positive control reads 0 records and every enrolled seat is Unknown forever."""
        m = re.search(r"(?m)^ExecStart=(.+)$", self.svc)
        cmd = m.group(1)
        # T-5998: nested, so the page port can also reach orchestrator's mailbox hub (group pantheon-mailbox)
        self.assertTrue(cmd.startswith('/usr/bin/sg systemd-journal -c "/usr/bin/sg pantheon-mailbox -c '
                                       '\\"/usr/bin/python3 '), cmd)
        self.assertTrue(cmd.endswith('\\""'), cmd)
        self.assertEqual(len(re.findall(r"(?m)^ExecStart=", self.svc)), 1)

    def test_timer_names_the_service(self):
        self.assertIn(f"Unit={SVC}", self.tmr)

    def test_not_in_expected_units_manifest(self):
        manifest = (SYSTEMD / "expected-units.yaml").read_text()
        self.assertNotIn("org076", manifest)

    def test_no_installer_or_script_references_the_units(self):
        offenders = []
        for p in list((ROOT / "tools").glob("*.py")) + list((ROOT / "tools").glob("*.sh")) \
                + list((ROOT / "tools" / "hooks").glob("*")) + list((ROOT / ".githooks").glob("*")):
            if p.name.startswith("test_") or p.name == "org076_store_runner.py":
                continue
            try:
                text = p.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            if "org076-store-runner" in text:
                offenders.append(p.name)
        self.assertEqual(offenders, [])

    def test_persona_installer_installs_only_named_templates(self):
        text = (ROOT / "tools" / "persona_installer.py").read_text()
        self.assertNotIn("org076", text)
        self.assertNotRegex(text, r"glob\([^)]*systemd")

    def test_no_enable_symlinks_committed(self):
        wants = [p for p in SYSTEMD.rglob("*") if p.is_symlink()]
        self.assertEqual([p for p in wants if "org076" in p.name], [])
        self.assertEqual(list(SYSTEMD.glob("*.wants")), [])


if __name__ == "__main__":
    unittest.main()
