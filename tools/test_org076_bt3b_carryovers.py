"""ORG-076 BT3b carry-overs (T-4709): runner guard + ORG-096 rebase edge end-to-end; ingest window
sizing; exit-4 alert coalescing. Hermetic (real bare origin, tmp dirs, injected clocks/ports)."""
from __future__ import annotations

import dataclasses
from unittest import mock

from tools import episode_store as es
from tools import org076_store_runner as run
from tools import quota_probe_detector as qpd
from tools._episode_fixtures import git
from tools.test_org076_reconciler import Rig as _Rig


class OriginAdvancesWhileOwnedCommitAhead(_Rig):
    def test_guard_owns_ahead_commit_skips_ff_and_rebase_edge_lands_exactly_one_ticket(self):
        for _ in range(3):
            self.drop_sample()
        down = lambda repo: es.PushResult(ok=False, detail="network down")  # noqa: E731
        self.assertEqual(self.cycle(pusher=down).exit_code, 0)
        (ep,) = self.episodes()
        self.assertEqual(ep["delivery"]["phase"], es.PUSH_INFLIGHT)
        self.assertEqual(self.fx.origin_ticket_files(), [])
        ahead_before = git("rev-list", "origin/main..HEAD", cwd=self.fx.repo).stdout.split()
        self.assertEqual(len(ahead_before), 1)  # the store's own episode commit, unpushed
        self.fx.peer_advance("moved")           # origin advances underneath it
        edge = es.make_rebase_edge(repo=self.fx.repo, state_dir=self.state, page=lambda s, b: self.pages.append((s, b)))
        seen = []
        real_guard = run.guard

        def spy(*a, **k):
            out = real_guard(*a, **k)
            seen.append(out)
            return out

        merges = []
        real_git = run._git_utils._git

        def git_spy(*args, **kw):
            if args[:2] == ("merge", "--ff-only"):
                merges.append(args)
            return real_git(*args, **kw)

        with mock.patch.object(run, "guard", spy), mock.patch.object(run._git_utils, "_git", git_spy):
            ports = self.ports(pusher=None, on_nonff=edge.on_nonff, on_recover=edge.on_recover)
            ports = dataclasses.replace(ports, git=run._git_utils)
            rep = run.run_cycle(self.cfg, ports)
        self.assertEqual(seen, [(None, True)])   # guard did not refuse and reported "owns ahead"
        self.assertEqual(merges, [])             # ff-only merge skipped while the store owns ahead commits
        self.assertIsNone(rep.refused)
        self.assertEqual(rep.exit_code, 0, rep.errors)
        (ep,) = self.episodes()
        self.assertEqual(ep["delivery"]["phase"], es.VERIFIED)
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)  # exactly one ticket landed
        self.assertEqual(git("rev-list", "origin/main..HEAD", cwd=self.fx.repo).stdout.strip(), "")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.fx.repo).stdout.strip(), self.origin_sha())
        # and the next cycle is quiet: guard passes, nothing new
        again = self.cycle()
        self.assertIsNone(again.refused)
        self.assertEqual(len(self.fx.origin_ticket_files()), 1)


# --------------------------------------------------------------------------- #
# (e)2: the 256-key `ingested` window vs a backlog larger than the window, REAL crash
# --------------------------------------------------------------------------- #
import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import textwrap  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from tools._episode_fixtures import RepoFixture  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

_INGEST_CHILD = textwrap.dedent("""
    import json, os, sys
    from pathlib import Path
    from tools import episode_store as es
    from tools import org076_store_runner as run
    from tools import quota_probe_detector as qpd
    d = json.loads(sys.argv[1])
    cfg = run.Config(clone=Path(d["clone"]), drop=Path(d["drop"]), state_dir=Path(d["state"]),
                     heartbeat=Path(d["hb"]), uid_of=lambda a: 1, journal=None)
    ports = es.Ports(repo=cfg.clone, page=lambda s, b: None, mirror_url=None, heartbeat_path=cfg.heartbeat)
    orig = es._apply_verdict
    def counting(doc, spec, verdict, path, ports):
        if spec.detector == qpd.DETECTOR:
            with open(d["applied"], "a") as fh:
                fh.write("1\\n")
        return orig(doc, spec, verdict, path, ports)
    es._apply_verdict = counting
    calls = {"n": 0}
    def hook(point):
        if point == "after_observe_before_unlink":
            calls["n"] += 1
            if d["die_at"] and calls["n"] == d["die_at"]:
                os._exit(137)   # the crash window: observe is durable, the file is still there
    run.run_cycle(cfg, ports, kill_hook=hook)
""")


class IngestedWindow(_Rig):
    N = 300  # > INGESTED_KEYS_KEPT (256) distinct buckets queued for ONE persona

    def child(self, die_at):
        cfg = json.dumps({"clone": str(self.fx.repo), "drop": str(self.drop), "state": str(self.state),
                          "hb": str(self.hb), "applied": str(self.root / "applied.txt"), "die_at": die_at})
        return subprocess.run([sys.executable, "-c", _INGEST_CHILD, cfg], cwd=ROOT, env=self.fx.env,
                              capture_output=True, text=True, timeout=600)

    def test_backlog_over_the_window_with_a_real_crash_mid_ingest_replays_exactly_once(self):
        self.assertGreater(self.N, es.INGESTED_KEYS_KEPT)
        for i in range(self.N):
            qpd.feed(SimpleNamespace(persona="recovery", account="claude-fallback"),
                     qpd.ProbeOutcome(qpd.READ, f"20261008T{i:06d}Z", "d"), drop=self.drop,
                     now=datetime(2026, 10, 8, tzinfo=timezone.utc) + timedelta(seconds=i))
            if i % 40 == 7:  # several files for one bucket (quota-check can run more than once per bucket)
                qpd.feed(SimpleNamespace(persona="recovery", account="claude-fallback"),
                         qpd.ProbeOutcome(qpd.READ, f"20261008T{i:06d}Z", "dup"), drop=self.drop,
                         now=datetime(2026, 10, 8, tzinfo=timezone.utc) + timedelta(seconds=i, milliseconds=500))
        dups = len([p for p in self.drop.iterdir()]) - self.N
        self.assertGreater(dups, 0)
        first = self.child(die_at=270)
        self.assertEqual(first.returncode, 137, first.stderr[-500:])   # a real process died mid-ingest
        left = [p for p in self.drop.iterdir() if p.is_file()]
        self.assertGreater(len(left), 0)  # the crashed file (observe durable) + the unprocessed tail
        second = self.child(die_at=0)
        self.assertEqual(second.returncode, 0, second.stderr[-800:])
        self.assertEqual([p for p in self.drop.iterdir() if p.is_file()], [])
        applied = len((self.root / "applied.txt").read_text().splitlines())
        self.assertEqual(applied, self.N)  # every distinct bucket applied EXACTLY once across the crash
        doc = self.domain()
        self.assertEqual(len(doc["ingested"]), es.INGESTED_KEYS_KEPT)
        self.assertEqual(len(set(doc["ingested"])), es.INGESTED_KEYS_KEPT)
        self.assertEqual(doc["ingested"][-1], f"20261008T{self.N - 1:06d}Z")


# --------------------------------------------------------------------------- #
# (e)4: exit 4 on a sustained fetch outage -> unit_failure_alert coalesces
# --------------------------------------------------------------------------- #
from unittest import mock as _mock  # noqa: E402

from tools import unit_failure_alert as ufa  # noqa: E402

UNIT = "pantheon-org076-store-runner.service"


class Exit4AlertCoalescing(_Rig):
    def alerts(self, ticks):
        """ticks: seconds at which the unit FAILED (runner exit 4). Returns delivery timestamps."""
        delivered = []
        base = 1_800_000_000.0
        clock = {"t": base}
        fake_time = SimpleNamespace(time=lambda: clock["t"], strftime=ufa.time.strftime, gmtime=ufa.time.gmtime)
        sp = Path(self.root) / "ufa-state.json"
        with _mock.patch.object(ufa, "STATE_PATH", sp), _mock.patch.object(ufa, "STATE_DIR", sp.parent), \
                _mock.patch.object(ufa, "time", fake_time), \
                _mock.patch.object(ufa, "_suppressed_by_mute", return_value=False), \
                _mock.patch.object(ufa, "_journal_tail", return_value="(tail)"), \
                _mock.patch.object(ufa, "_deliver_mailbox", lambda unit, body, sender: delivered.append(clock["t"] - base)):
            for t in ticks:
                clock["t"] = base + float(t)
                self.assertEqual(ufa.run(UNIT), 0)
        return delivered

    def test_the_runner_really_exits_4_on_a_fetch_outage(self):
        away = self.root / "origin-away.git"
        self.fx.origin.rename(away)
        try:
            codes = [self.cycle().exit_code for _ in range(2)]
        finally:
            away.rename(self.fx.origin)
        self.assertEqual(codes, [4, 4])  # a partial failure, never masked (no SuccessExitStatus)

    def test_sustained_outage_is_one_alert_per_6h_cooldown(self):
        five_min = range(0, 6 * 3600, 300)  # 72 consecutive failed ticks, a 6 hour outage
        self.assertEqual(self.alerts(five_min), [0.0])

    def test_edge_i_outage_longer_than_the_cooldown_realerts_every_6h(self):
        ticks = range(0, 13 * 3600, 300)
        self.assertEqual(self.alerts(ticks), [0.0, 21600.0, 43200.0])

    def test_edge_ii_a_second_distinct_outage_inside_the_window_is_coalesced(self):
        outage_a = list(range(0, 3600, 300))                 # t=0 .. 55min, alerted at t=0
        outage_b = list(range(3 * 3600, 4 * 3600, 300))      # starts 3h after A's alert: still < 6h
        self.assertEqual(self.alerts(outage_a + outage_b), [0.0])  # B never alerts (honest edge)
