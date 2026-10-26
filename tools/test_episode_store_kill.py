"""ORG-076 BT1: real-process kill-injection + concurrency (RFC-004 §6.1, §6.2, §6.3a-e, §6.8, heartbeat).

Mechanism: the store runs in a forked child that calls ``os._exit(137)`` at a NAMED kill point
(``Ports.kill_hook``). An in-process exception would unwind ``with`` blocks, release the flock
and run cleanup a SIGKILL never runs; ``os._exit`` skips all of that, so the on-disk state the
next process sees is the real post-crash state (flock released only by the kernel, no flush,
temp files left behind). The ``ticket.py`` child can additionally be killed INSIDE its commit
pipeline (before add / after add / stale index.lock) via ``EPISODE_TEST_TICKET_KILL``.
Restart == a fresh ``Ports`` + ``reconcile()`` in the parent against the same state dir.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tools import episode_store as es
from tools._episode_fixtures import RepoFixture, FakeClock, git, make_ports, make_spec

CTX = mp.get_context("fork")
B = es.Broken("f1", "evidence line\n")


class KillCase(unittest.TestCase):
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
        self.spec = make_spec(n=1)

    # -- child runner --------------------------------------------------------
    def run_child(self, *, kill_at: str = "", ticket_kill: str = "", verdict=B, extra=None) -> int:
        def target():
            fx = self.fx
            ports = make_ports(fx, kill_at=kill_at, clock=FakeClock("2026-10-08T02:00:00+00:00"),
                               filer=(lambda argv: fx.ticket_cli(argv, kill=ticket_kill)))
            if extra:
                extra(ports)
            es.observe(self.spec, verdict, ports=ports, state_dir=self.state.name)
            os._exit(0)
        proc = CTX.Process(target=target)
        proc.start()
        proc.join(120)
        return proc.exitcode

    def restart(self):
        ports = make_ports(self.fx, clock=FakeClock("2026-10-08T03:00:00+00:00"))
        return es.reconcile([self.spec], ports=ports, state_dir=self.state.name)

    def episode(self):
        (ep,) = es.load_domain(self.spec.domain_key, self.state.name)["episodes"].values()
        return ep

    def assert_exactly_one_verified(self):
        ep = self.episode()
        self.assertEqual((ep["state"], ep["delivery"]["phase"]), ("EMITTED", "VERIFIED"))
        self.assertEqual(self.fx.origin_log_count(f"episode_id: {ep['episode_id']}"), 1)
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)
        self.assertEqual(git("status", "--porcelain", cwd=self.fx.repo).stdout.strip(), "")  # tree clean
        self.assertFalse((self.fx.repo / ".git" / "index.lock").exists())
        return ep


class TicketChildIsolation(KillCase):
    def test_ticket_child_git_defaults_to_the_temp_clone_not_the_real_checkout(self):  # T-4728
        """Bare ``_git(...)`` in ticket.py (ID-allocation fetch) must run in the fixture clone.
        Otherwise it fetches the REAL checkout's origin and concurrent children (xdist) fail
        with 'cannot lock ref refs/remotes/origin/main', so ticket.py files nothing."""
        r = self.fx.ticket_cli([], env_extra={"EPISODE_TEST_PROBE_GIT_CWD": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(Path(r.stdout.strip()).resolve(), self.fx.repo.resolve())


class CrashBoundaries(KillCase):
    def test_2a_crash_after_open_before_emit(self):  # §6.2(a)
        self.assertEqual(self.run_child(kill_at="after_open_before_emit"), 137)
        ep = self.episode()
        self.assertEqual((ep["state"], ep["delivery"]["phase"]), ("OPEN", "NONE"))  # fsync'd before emit
        self.assertEqual(self.fx.origin_ticket_files(), [])
        self.restart()
        self.assert_exactly_one_verified()

    def test_2b_crash_after_emit_before_emitted_mark(self):  # §6.2(b) == §6.3(a) commit landed
        self.assertEqual(self.run_child(kill_at="after_file_before_local_commit"), 137)
        ep = self.episode()
        self.assertEqual((ep["state"], ep["delivery"]["phase"]), ("OPEN", "COMMIT_INFLIGHT"))
        local = git("log", "--format=%H", "-S", f"episode_id: {ep['episode_id']}", "origin/main..HEAD",
                    "--", "tickets/", cwd=self.fx.repo).stdout.split()
        self.assertEqual(len(local), 1)  # the commit really landed locally
        self.restart()
        ep = self.assert_exactly_one_verified()
        self.assertEqual(ep["delivery"]["commit_sha"], local[0])

    def test_3a_commit_not_landed_untracked_file_cleaned_and_reemitted(self):  # §6.3(a)
        self.assertEqual(self.run_child(kill_at="after_file_before_local_commit", ticket_kill="before_add"), 137)
        inbox = [p.name for p in (self.fx.repo / "tickets" / "inbox").iterdir() if p.name != ".gitkeep"]
        self.assertEqual(len(inbox), 1)  # the untracked residue is really there
        eid = self.episode()["episode_id"]
        self.restart()
        self.assertEqual(self.assert_exactly_one_verified()["episode_id"], eid)  # SAME episode_id

    def test_3a_commit_not_landed_staged_file_unstaged_and_removed(self):  # amendment C
        self.assertEqual(self.run_child(kill_at="after_file_before_local_commit", ticket_kill="after_add"), 137)
        staged = git("diff", "--cached", "--name-only", cwd=self.fx.repo).stdout.split()
        self.assertTrue(any(p.startswith("tickets/inbox/T-") for p in staged), staged)
        eid = self.episode()["episode_id"]
        self.restart()
        self.assertEqual(self.assert_exactly_one_verified()["episode_id"], eid)

    def test_3a_stale_index_lock_cleared(self):  # amendment C
        self.assertEqual(self.run_child(kill_at="after_file_before_local_commit", ticket_kill="stale_lock"), 137)
        self.assertTrue((self.fx.repo / ".git" / "index.lock").exists())
        self.restart()
        self.assert_exactly_one_verified()

    def test_cleanup_never_deletes_unrelated_files(self):  # amendment C: "never delete any other file"
        self.assertEqual(self.run_child(kill_at="after_file_before_local_commit", ticket_kill="before_add"), 137)
        other = self.fx.repo / "tickets" / "inbox" / "T-9998.md"
        other.write_text("---\nid: T-9998\nepisode_id: ep-someone-else\n---\n")
        stray = self.fx.repo / "notes.txt"
        stray.write_text("keep me")
        self.restart()
        self.assertTrue(other.exists())
        self.assertTrue(stray.exists())
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)

    def test_cleanup_matches_whole_episode_id_line_not_prefix(self):  # review fix 1
        self.assertEqual(self.run_child(kill_at="after_file_before_local_commit", ticket_kill="before_add"), 137)
        eid = self.episode()["episode_id"]
        lookalike = self.fx.repo / "tickets" / "inbox" / "T-9997.md"
        lookalike.write_text(f"---\nid: T-9997\nepisode_id: {eid}-x\n---\n")  # eid is a prefix of it
        self.restart()
        self.assertTrue(lookalike.exists())  # only the exact match was cleaned
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)
        self.assertEqual(self.episode()["delivery"]["phase"], "VERIFIED")

    def test_cleanup_keeps_unrelated_unstaged_readme_edit(self):  # review fix 2
        self.assertEqual(self.run_child(kill_at="after_file_before_local_commit", ticket_kill="after_add"), 137)
        readme = self.fx.repo / "tickets" / "README.md"
        readme.write_text(readme.read_text() + "\n- operator note OPS-KEEP-ME\n")  # unstaged edit
        self.restart()
        self.assertIn("OPS-KEEP-ME", readme.read_text())
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)
        self.assertEqual(self.episode()["delivery"]["phase"], "VERIFIED")

    def test_readme_row_removal_is_anchored_on_ticket_id(self):  # review fix 2
        head = "| T-1 | inbox | a |\n"
        work = head + "| T-4707 | inbox | mine |\n| T-47070 | inbox | other |\n| T-4707 | inbox | in head |\n"
        out = es._strip_ticket_rows(work, head + "| T-4707 | inbox | in head |\n", "T-4707")
        self.assertEqual(out, head + "| T-47070 | inbox | other |\n| T-4707 | inbox | in head |\n")
        self.assertIsNone(es._strip_ticket_rows(head, head, "T-4707"))

    def test_3b_crash_in_local_commit_resumes_push_with_same_sha(self):  # §6.3(b)
        self.assertEqual(self.run_child(kill_at="after_local_commit_persist"), 137)
        ep = self.episode()
        self.assertEqual(ep["delivery"]["phase"], "LOCAL_COMMIT")
        sha = ep["delivery"]["commit_sha"]
        self.assertEqual(self.fx.origin_ticket_files(), [])
        self.restart()
        ep = self.assert_exactly_one_verified()
        self.assertEqual(ep["delivery"]["commit_sha"], sha)
        self.assertEqual(git("merge-base", "--is-ancestor", sha, "main", cwd=self.fx.origin, check=False).returncode, 0)

    def test_3c_crash_before_push_retries_push(self):  # §6.3(c) push never left
        self.assertEqual(self.run_child(kill_at="before_push"), 137)
        self.assertEqual(self.episode()["delivery"]["phase"], "PUSH_INFLIGHT")
        self.assertEqual(self.fx.origin_ticket_files(), [])
        self.restart()
        self.assert_exactly_one_verified()

    def test_3c_crash_during_push_after_remote_accepted_retries_idempotently(self):  # §6.3(c)
        self.assertEqual(self.run_child(kill_at="after_push_before_pushed"), 137)
        self.assertEqual(self.episode()["delivery"]["phase"], "PUSH_INFLIGHT")
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)  # remote already has it
        self.restart()
        self.assert_exactly_one_verified()

    def test_3d_crash_in_pushed_verifies_on_next_reconcile(self):  # §6.3(d)
        self.assertEqual(self.run_child(kill_at="after_pushed_persist"), 137)
        self.assertEqual(self.episode()["delivery"]["phase"], "PUSHED")
        self.restart()
        self.assert_exactly_one_verified()

    def test_3e_crash_after_verified_no_reemit(self):  # §6.3(e)
        self.assertEqual(self.run_child(kill_at="after_verified_persist"), 137)
        ep = self.episode()
        self.assertEqual((ep["state"], ep["delivery"]["phase"]), ("EMITTED", "VERIFIED"))
        tid = ep["delivery"]["ticket_id"]
        self.restart()
        self.restart()
        ep = self.assert_exactly_one_verified()
        self.assertEqual(ep["delivery"]["ticket_id"], tid)

    def test_8_restart_persistence_new_process_same_state_dir(self):  # §6.8
        self.assertEqual(self.run_child(), 0)
        self.assert_exactly_one_verified()
        before = self.episode()
        self.assertEqual(self.run_child(), 0)  # same verdict again in a brand-new process
        self.restart()
        self.assertEqual(self.episode(), {**before, "counters": self.episode()["counters"],
                                          "last_evidence": self.episode()["last_evidence"]})
        self.assert_exactly_one_verified()

    def test_store_survives_kill_between_open_and_every_later_write(self):  # atomic-rename durability
        self.assertEqual(self.run_child(kill_at="after_open_before_emit"), 137)
        path = es.store_path(self.spec.domain_key, self.state.name)
        json.loads(path.read_text())  # never torn
        self.assertEqual([p for p in path.parent.iterdir() if p.name.endswith(".tmp")], [])


class Concurrency(KillCase):
    def test_1_two_overlapping_cycles_one_episode_one_ticket(self):
        barrier = CTX.Barrier(2)
        out = Path(self.state.name) / "results"
        out.mkdir()

        def worker(i):
            ports = make_ports(self.fx, clock=FakeClock(f"2026-10-08T04:00:0{i}+00:00"), lock_timeout=90.0)
            barrier.wait(30)
            r = es.observe(self.spec, B, ports=ports, state_dir=self.state.name)
            (out / f"{i}.txt").write_text(r.status)  # os._exit below would drop a queued put
            os._exit(0)

        procs = [CTX.Process(target=worker, args=(i,)) for i in range(2)]
        [p.start() for p in procs]
        [p.join(180) for p in procs]
        statuses = sorted(p.read_text() for p in out.iterdir())
        self.assertEqual(statuses, ["opened", "suppressed"])  # serialized: second sees OPEN/EMITTED
        doc = es.load_domain(self.spec.domain_key, self.state.name)
        self.assertEqual(len(doc["episodes"]), 1)
        self.assert_exactly_one_verified()

    def test_F_flock_timeout_returns_busy_suppressed_instead_of_wedging(self):  # amendment F
        holding = CTX.Event()

        def holder():
            def slow(ports):
                inner = ports.kill_hook
                ports.kill_hook = lambda p: (holding.set(), time.sleep(4)) if p == "after_open_before_emit" else inner(p)
            ports = make_ports(self.fx, clock=FakeClock("2026-10-08T05:00:00+00:00"))
            slow(ports)
            es.observe(self.spec, B, ports=ports, state_dir=self.state.name)
            os._exit(0)

        h = CTX.Process(target=holder)
        h.start()
        self.assertTrue(holding.wait(60))
        t0 = time.monotonic()
        r = es.observe(self.spec, B, ports=make_ports(self.fx, lock_timeout=0.3), state_dir=self.state.name)
        self.assertEqual(r.status, "busy_suppressed")
        self.assertLess(time.monotonic() - t0, 3.0)
        h.join(120)
        self.restart()
        self.assert_exactly_one_verified()


class HeartbeatKill(KillCase):
    def test_killed_process_leaves_heartbeat_unadvanced(self):  # RFC-004 §6.9 / Recovery constraint
        hb = es.heartbeat_path(self.state.name)

        def target():
            p = es.Ports(repo=self.fx.repo, kill_hook=lambda pt: os._exit(137) if pt == "after_open_before_emit" else None)
            es.observe(self.spec, B, ports=p, state_dir=self.state.name)  # real clock
            os._exit(0)

        proc = CTX.Process(target=target)
        proc.start()
        proc.join(60)
        self.assertEqual(proc.exitcode, 137)
        e1 = json.loads(hb.read_text())
        time.sleep(1.2)
        e2 = json.loads(hb.read_text())
        self.assertEqual(e1, e2)  # dead process: nothing advances it
        self.assertGreaterEqual(time.time() - e2["epoch"], 1)


if __name__ == "__main__":
    unittest.main()
