#!/usr/bin/env python3
"""Tests for pantheon_oom_killer (T-5227). FAKE process tables only: nothing here reads the
real /proc, allocates memory, signals a real pid, or touches a real registry/mailbox."""
from __future__ import annotations

import re
import signal
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pantheon_oom_killer as ok  # noqa: E402
from pantheon_oom_killer import Pane, Proc  # noqa: E402

MB = 1024
SELF = 99999
TOTAL = 8 * 1024 * 1024  # 8 GB in kB
LATE = 10 * 60 * ok.CLK_TCK + 10_000  # started ~10 min after the seat session


def P(pid, ppid=1, uid=2000, comm="x", rss=10 * MB, swap=0, start=None, tpgid=-1, cmd=None):
    return Proc(pid, ppid, uid, comm, cmd or comm, rss, swap, start if start is not None else pid * 10,
                tpgid)


def table(*procs):
    return {p.pid: p for p in procs}


class FakeEnv(ok.Env):
    def __init__(self, procs=None, panes=None, accounts=None, sessions=None, held=None):
        super().__init__("/nonexistent", Path("/nonexistent-state"))
        self.procs = dict(procs or {})
        self.panes = panes if panes is not None else {}       # uid -> list[Pane] | None
        self.cfg = ok.SeatConfig(accounts or {"thothacct": "orchestrator", "wardacct": "recovery"},
                                 sessions or ["claude-telegram-bot", "pantheon-{persona}"])
        self.uids = {"thothacct": 1000, "wardacct": 1001}
        self.mem = {"MemTotal": TOTAL, "MemAvailable": TOTAL // 2, "SwapTotal": 4 << 20,
                    "SwapFree": 4 << 20}
        self.holds = dict(held or {})
        self.t = 1000.0
        self.signals = []
        self.placed, self.rearmed, self.notices, self.records = [], [], [], []
        self.notify_raises = False
        self.vanish_on_signal = False
        self.dies_on_term = True
        self.read_uid_filters = []
        self.tmux_calls = 0

    # clocks
    def mono(self): return self.t
    def sleep(self, s): self.t += s
    def now_iso(self): return "2026-10-19T00:00:00Z"
    # reads
    def meminfo(self): return dict(self.mem)
    def psi_full_avg10(self): return 1.5
    def read_procs(self, uids=None):
        self.read_uid_filters.append(uids)
        return {k: v for k, v in self.procs.items() if uids is None or v.uid in uids}
    def tmux_panes(self, uid):
        self.tmux_calls += 1
        return self.panes.get(uid, [])
    def load_seats(self): return self.cfg
    def uid_of(self, account): return self.uids.get(account)
    def start_of(self, pid):
        p = self.procs.get(pid)
        return p.start if p else None
    # effects
    def send_signal(self, pid, sig):
        self.signals.append((pid, sig))
        if self.vanish_on_signal or pid not in self.procs:
            self.procs.pop(pid, None)
            raise ProcessLookupError
        if sig == signal.SIGKILL or self.dies_on_term:
            del self.procs[pid]
    def write_record(self, rec): self.records.append(rec)
    def held(self): return dict(self.holds)
    def place_hold(self, persona, reason):
        self.placed.append((persona, reason))
        self.holds[persona] = {"reason": reason}
    def rearm_hold(self, persona): self.rearmed.append(persona)
    def notify(self, persona, subject, body):
        if self.notify_raises:
            raise RuntimeError("mailbox down")
        self.notices.append((persona, subject, body))

    def pressure(self):
        self.mem["MemAvailable"] = TOTAL // 50
        self.mem["SwapFree"] = (4 << 20) // 50


def seat_world(session_kb=500 * MB // 1):
    """orchestrator (uid 1000) with session claude-telegram-bot: tmux server 10, pane shell 11,
    foreground claude 12; plumbing systemd 5 + dbus 6; a worker tmux session (server shared)."""
    procs = table(
        P(5, 1, 1000, "systemd", 20 * MB), P(6, 5, 1000, "dbus-daemon", 5 * MB),
        P(10, 1, 1000, "tmux: server", 30 * MB),
        P(11, 10, 1000, "bash", 5 * MB, tpgid=12), P(12, 11, 1000, "claude", session_kb),
        P(13, 10, 1000, "bash", 5 * MB, tpgid=14), P(14, 13, 1000, "python3", 20 * MB),
    )
    panes = {1000: [Pane(10, "claude-telegram-bot", 11), Pane(10, "worker-scratch", 13)]}
    return procs, panes


class ProtectedSetTests(unittest.TestCase):
    def test_protects_server_pane_foreground_and_plumbing_only(self):
        procs, panes = seat_world()
        env = FakeEnv(procs, panes)
        prot = ok.compute_protected(procs, env.cfg, env)
        self.assertEqual(set(prot.pids), {5, 6, 10, 11, 12})
        self.assertNotIn(13, prot.pids)      # worker session pane not protected
        self.assertNotIn(14, prot.pids)

    def test_template_session_name_expands_per_persona(self):
        procs = table(P(20, 1, 1001, "tmux: server"), P(21, 20, 1001, "claude", tpgid=21))
        env = FakeEnv(procs, {1001: [Pane(20, "pantheon-recovery", 21)]})
        prot = ok.compute_protected(procs, env.cfg, env)
        self.assertEqual(set(prot.pids), {20, 21})
        self.assertEqual(prot.owner[21], "recovery")

    def test_other_persona_template_name_alone_is_treated_as_unnamed_and_protected(self):
        # recovery's only live session carries orchestrator's template name: the manifest names NONE of
        # recovery's live sessions, so the generic fail-safe protects them all (never leave naked).
        procs = table(P(20, 1, 1001, "tmux: server"), P(21, 20, 1001, "claude"))
        env = FakeEnv(procs, {1001: [Pane(20, "pantheon-orchestrator", 21)]})
        prot = ok.compute_protected(procs, env.cfg, env)
        self.assertIn(21, prot.pids)
        self.assertTrue(prot.failsafe)

    def test_inherited_minus_850_does_not_protect_a_child(self):
        # oom_score_adj is not even a field of Proc: a child of a protected pid inherits nothing.
        procs, panes = seat_world()
        procs[15] = P(15, 12, 1000, "probe", 6 * 1024 * MB, start=LATE)   # child of protected claude
        env = FakeEnv(procs, panes)
        prot = ok.compute_protected(procs, env.cfg, env)
        self.assertNotIn(15, prot.pids)
        self.assertEqual(ok.select_victim(procs, set(prot.pids), SELF).pid, 15)

    def test_failsafe_protects_servers_and_pane_children(self):
        procs, panes = seat_world()
        env = FakeEnv(procs, panes)
        env.cfg = ok.SeatConfig({"thothacct": "orchestrator"}, [])          # no session names resolvable
        prot = ok.compute_protected(procs, env.cfg, env)
        self.assertTrue(prot.failsafe)
        self.assertTrue({10, 11, 12, 13, 14} <= set(prot.pids))

    def test_failsafe_when_tmux_cannot_be_asked_protects_servers_by_comm(self):
        procs, _ = seat_world()
        env = FakeEnv(procs, {1000: None})
        prot = ok.compute_protected(procs, env.cfg, env)
        self.assertIn(10, prot.pids)
        self.assertTrue(prot.failsafe)
        self.assertTrue({11, 12, 13, 14} <= set(prot.pids))       # pane children too (C)


class UnlistableTmuxTests(unittest.TestCase):
    def test_live_server_with_no_listable_sockets_falls_back_to_failsafe(self):
        procs, _ = seat_world()
        env = FakeEnv(procs, {1000: []})          # no sockets found, yet a tmux server runs
        prot = ok.compute_protected(procs, env.cfg, env)
        self.assertTrue(prot.failsafe)
        self.assertIn(10, prot.pids)


class LauncherChainTests(unittest.TestCase):
    """Generic session rule: pane process + descendants started within SESSION_STARTUP_WINDOW."""

    def codex_world(self):
        # codex seat: server 60, pane = node wrapper 61, vendor binary 62 (the real session),
        # an MCP server 63 started a few seconds later; session name is NOT in the manifest.
        procs = table(
            P(60, 1, 1003, "tmux: server", 5 * MB),
            P(61, 60, 1003, "node", 40 * MB, start=1000, tpgid=61),
            P(62, 61, 1003, "codex", 300 * MB, start=1010),
            P(63, 62, 1003, "mcp-server", 80 * MB, start=1200),
        )
        env = FakeEnv(procs, {1003: [Pane(60, "codex-session", 61)]},
                      accounts={"codexacct": "codex"})
        env.uids["codexacct"] = 1003
        return procs, env

    def test_unnamed_session_on_seat_account_is_protected_wholesale(self):
        procs, env = self.codex_world()
        prot = ok.compute_protected(procs, env.cfg, env)
        self.assertTrue({60, 61, 62, 63} <= set(prot.pids))
        self.assertTrue(prot.failsafe)
        self.assertIsNone(ok.select_victim(procs, set(prot.pids), SELF))

    def test_wrapper_to_binary_chain_protected_but_late_child_is_not(self):
        procs, env = self.codex_world()
        procs[64] = P(64, 62, 1003, "cargo", 900 * MB, start=1000 + LATE, tpgid=-1)
        procs[65] = P(65, 64, 1003, "rustc", 900 * MB, start=1000 + LATE + 5)
        prot = ok.compute_protected(procs, env.cfg, env)
        self.assertIn(62, prot.pids)
        self.assertNotIn(64, prot.pids)
        self.assertNotIn(65, prot.pids)

    def test_late_child_not_protected_even_with_inherited_minus_850(self):
        procs, panes = seat_world()
        procs[15] = P(15, 12, 1000, "task", 100 * MB, start=LATE)    # oom_score_adj is not read
        env = FakeEnv(procs, panes)
        self.assertNotIn(15, ok.compute_protected(procs, env.cfg, env).pids)

    def test_startup_child_counts_toward_session_footprint_and_hard_line(self):
        procs, panes = seat_world(session_kb=500 * MB)
        procs[15] = P(15, 12, 1000, "mcp-leak", 3 * 1024 * MB, start=procs[12].start + 100)
        env = FakeEnv(procs, panes)
        prot = ok.compute_protected(procs, env.cfg, env)
        self.assertGreater(prot.sessions[("orchestrator", "claude-telegram-bot")]["kb"], ok.HARD_LINE_KB)
        self.assertNotIn(15, prot.pids)                               # session dropped off
        self.assertNotIn(12, prot.pids)

    def test_startup_window_is_measured_from_the_root_start(self):
        procs, panes = seat_world()
        edge = ok.SESSION_STARTUP_WINDOW * ok.CLK_TCK
        procs[15] = P(15, 12, 1000, "in", 1 * MB, start=int(procs[12].start + edge))
        procs[16] = P(16, 12, 1000, "out", 1 * MB, start=int(procs[12].start + edge + 1))
        prot = ok.compute_protected(procs, FakeEnv(procs, panes).cfg, FakeEnv(procs, panes))
        self.assertIn(15, prot.pids)
        self.assertNotIn(16, prot.pids)

    def test_named_session_present_keeps_worker_sessions_unprotected(self):
        procs, panes = seat_world()                                   # orchestrator's name IS in manifest
        env = FakeEnv(procs, panes)
        prot = ok.compute_protected(procs, env.cfg, env)
        self.assertNotIn(14, prot.pids)
        self.assertFalse(prot.failsafe)


class VictimTests(unittest.TestCase):
    def test_largest_unprotected_is_chosen_even_over_a_bigger_protected_one(self):
        procs, panes = seat_world(session_kb=2000 * MB)
        procs[30] = P(30, 1, 2000, "build", 700 * MB)
        procs[31] = P(31, 1, 2000, "tiny", 1 * MB)
        env = FakeEnv(procs, panes)
        prot = ok.compute_protected(procs, env.cfg, env)
        self.assertEqual(ok.select_victim(procs, set(prot.pids), SELF).pid, 30)

    def test_root_children_of_init_and_self_and_kthreads_never_victims(self):
        procs = table(P(40, 1, 0, "sshd", 900 * MB), P(41, 2, 0, "kworker", 900 * MB),
                      P(SELF, 1, 0, "killer", 900 * MB), P(42, 1, 2000, "ok", 5 * MB))
        self.assertEqual(ok.select_victim(procs, set(), SELF).pid, 42)

    def test_swap_counts_toward_size(self):
        procs = table(P(50, rss=100 * MB, swap=600 * MB), P(51, rss=500 * MB))
        self.assertEqual(ok.select_victim(procs, set(), SELF).pid, 50)

    def test_session_over_hard_line_becomes_eligible(self):
        procs, panes = seat_world(session_kb=4 * 1024 * MB)          # 4 GB > 3 GB hard line
        env = FakeEnv(procs, panes)
        prot = ok.compute_protected(procs, env.cfg, env)
        self.assertNotIn(12, prot.pids)
        self.assertNotIn(11, prot.pids)
        self.assertNotIn(10, prot.pids)
        self.assertEqual(ok.select_victim(procs, set(prot.pids), SELF).pid, 12)
        self.assertIn(5, prot.pids)                                   # plumbing stays protected

    def test_session_under_hard_line_stays_protected_and_soft_warning_rate_limited(self):
        procs, panes = seat_world(session_kb=2000 * MB)              # between 1.5 and 3 GB
        env = FakeEnv(procs, panes)
        k = ok.Killer(env, self_pid=SELF)
        with self.assertLogs("pantheon-oom-killer", "WARNING") as cm:
            k.refresh(procs, 0.0)
            k.refresh(procs, 5.0)                                     # inside WARN_INTERVAL
        self.assertEqual(len([m for m in cm.output if "soft line" in m]), 1)
        self.assertIn(12, k.prot.pids)


class TriggerTests(unittest.TestCase):
    def test_no_trigger_when_memory_or_swap_healthy(self):
        m = {"MemTotal": 100, "MemAvailable": 5, "SwapTotal": 100, "SwapFree": 50}
        self.assertFalse(ok.is_over(m))                               # swap healthy
        m.update(MemAvailable=50, SwapFree=1)
        self.assertFalse(ok.is_over(m))                               # memory healthy
        m.update(MemAvailable=5, SwapFree=5)
        self.assertTrue(ok.is_over(m))
        m.update(SwapTotal=0, SwapFree=0)
        self.assertTrue(ok.is_over(m))                                # no swap: memory alone

    def test_fires_on_live_balloon_shape(self):
        # 2026-10-19 17:32Z live test: kernel OOM struck at MemAvailable ~19%, SwapFree 0%.
        # The trigger must fire before that, while swap still has headroom.
        m = {"MemTotal": 100, "MemAvailable": 19, "SwapTotal": 100, "SwapFree": 7}
        self.assertTrue(ok.is_over(m))
        m.update(SwapFree=31)
        self.assertFalse(ok.is_over(m))                               # earlier sample: not yet

    def test_healthy_box_kills_nothing(self):
        procs, panes = seat_world()
        procs[30] = P(30, 1, 2000, "big", 3000 * MB)
        env = FakeEnv(procs, panes)
        k = ok.Killer(env, self_pid=SELF)
        for _ in range(5):
            self.assertEqual(k.step(), [])
            env.t += 1
        self.assertEqual(env.signals, [])

    def test_debounce_requires_two_consecutive_ticks(self):
        procs, panes = seat_world()
        procs[30] = P(30, 1, 2000, "big", 3000 * MB)
        env = FakeEnv(procs, panes)
        k = ok.Killer(env, self_pid=SELF)
        env.pressure()
        env.mem["SwapFree"] = (4 << 20) * 15 // 100                    # over the line, not urgent
        self.assertEqual(k.step(), [])
        env.t += 1
        self.assertEqual(len(k.step()), 1)

    def test_urgent_swap_kills_on_first_tick_and_polls_fast(self):
        procs, panes = seat_world()
        procs[30] = P(30, 1, 2000, "big", 3000 * MB)
        env = FakeEnv(procs, panes)
        k = ok.Killer(env, self_pid=SELF)
        self.assertFalse(k.pressured)
        env.pressure()                                                # SwapFree 2% < URGENT
        self.assertEqual(len(k.step()), 1)
        self.assertTrue(k.pressured)

    def test_pressure_kill_sigterm_then_sigkill_after_grace(self):
        procs, panes = seat_world()
        procs[30] = P(30, 1, 2000, "stubborn", 3000 * MB)
        env = FakeEnv(procs, panes)
        env.dies_on_term = False
        k = ok.Killer(env, debounce=1, self_pid=SELF)
        env.pressure()
        k.step()
        self.assertEqual([s for _, s in env.signals], [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(env.records[0]["signal"], "KILL")
        self.assertGreaterEqual(env.t - 1000.0, ok.GRACE_SECONDS)

    def test_cooldown_prevents_double_kill_next_tick(self):
        procs, panes = seat_world()
        procs[30] = P(30, 1, 2000, "a", 3000 * MB)
        procs[31] = P(31, 1, 2000, "b", 2000 * MB)
        env = FakeEnv(procs, panes)
        k = ok.Killer(env, debounce=1, self_pid=SELF)
        env.pressure()
        self.assertEqual(len(k.step()), 1)
        env.t = env.t - (env.t - 1000.0) + 1.0                         # 1s after the first tick
        self.assertEqual(k.step(), [])


class AttributionAndHoldTests(unittest.TestCase):
    def test_attribution_by_ancestry_to_protected_session(self):
        procs, panes = seat_world()
        procs[15] = P(15, 12, 2000, "sh", 1 * MB, start=LATE)                     # different uid, child of claude
        procs[16] = P(16, 15, 2000, "probe", 5000 * MB, start=LATE)
        env = FakeEnv(procs, panes)
        k = ok.Killer(env, debounce=1, self_pid=SELF)
        env.pressure()
        (rec,) = k.step()
        self.assertEqual((rec["pid"], rec["seat"], rec["attribution"]), (16, "orchestrator", "ancestry"))
        self.assertEqual([c["pid"] for c in rec["ancestry"]][:3], [16, 15, 12])

    def test_uid_match_is_recorded_but_never_holds_the_seat(self):
        # E1 ruling: the echo daemon runs as `claude` (orchestrator's uid) but is not orchestrator's work.
        procs, panes = seat_world()
        procs[30] = P(30, 5, 1000, "echo-daemon", 5000 * MB)          # child of systemd --user
        env = FakeEnv(procs, panes)
        k = ok.Killer(env, debounce=1, self_pid=SELF)
        env.pressure()
        (rec,) = k.step()
        self.assertIsNone(rec["seat"])
        self.assertEqual(rec["attribution"], "uid:thothacct")
        self.assertEqual(env.placed, [])
        self.assertEqual(env.notices, [])

    def test_ancestry_into_seat_session_does_hold(self):
        procs, panes = seat_world()
        procs[30] = P(30, 12, 1000, "probe", 5000 * MB, start=LATE)
        env = FakeEnv(procs, panes)
        k = ok.Killer(env, debounce=1, self_pid=SELF)
        env.pressure()
        (rec,) = k.step()
        self.assertEqual((rec["seat"], rec["attribution"]), ("orchestrator", "ancestry"))
        self.assertEqual(env.placed[0][0], "orchestrator")

    def test_unattributed_victim_gets_no_hold(self):
        procs, panes = seat_world()
        procs[30] = P(30, 1, 4242, "stranger", 5000 * MB)
        env = FakeEnv(procs, panes)
        k = ok.Killer(env, debounce=1, self_pid=SELF)
        env.pressure()
        (rec,) = k.step()
        self.assertIsNone(rec["seat"])
        self.assertEqual(env.placed, [])
        self.assertEqual(env.notices, [])

    def test_hold_and_urgent_notice_on_pressure_kill(self):
        procs, panes = seat_world()
        procs[30] = P(30, 12, 1000, "probe", 5000 * MB, cmd="python3 lexer_probe.py --big", start=LATE)
        env = FakeEnv(procs, panes)
        k = ok.Killer(env, debounce=1, self_pid=SELF)
        env.pressure()
        k.step()
        self.assertEqual(env.placed[0][0], "orchestrator")
        persona, subject, body = env.notices[0]
        self.assertEqual(persona, "orchestrator")
        self.assertIn("lexer_probe.py", body)
        self.assertIn("HOLD", body)
        self.assertEqual(env.records[0]["reason"], "pressure")

    def test_mailbox_failure_is_logged_not_fatal(self):
        procs, panes = seat_world()
        procs[30] = P(30, 12, 1000, "probe", 5000 * MB, start=LATE)
        env = FakeEnv(procs, panes)
        env.notify_raises = True
        k = ok.Killer(env, debounce=1, self_pid=SELF)
        env.pressure()
        with self.assertLogs("pantheon-oom-killer", "ERROR"):
            recs = k.step()
        self.assertEqual(len(recs), 1)
        self.assertEqual(env.placed[0][0], "orchestrator")                   # hold still placed
        self.assertEqual(len(env.records), 1)                         # kill still recorded


class HeldSeatTests(unittest.TestCase):
    def test_held_seat_process_over_line_killed_without_host_pressure(self):
        procs, panes = seat_world(session_kb=2000 * MB)               # protected session may be big
        procs[30] = P(30, 1, 1000, "respun", 1200 * MB)               # orchestrator's unprotected, > 1 GB
        procs[31] = P(31, 1, 1000, "small", 300 * MB)
        procs[32] = P(32, 1, 2000, "other-seat-bigger", 2000 * MB)    # not held: untouched
        env = FakeEnv(procs, panes, held={"orchestrator": {}})
        k = ok.Killer(env, self_pid=SELF)
        recs = k.step()                                               # healthy host
        self.assertEqual([(r["pid"], r["reason"]) for r in recs], [(30, "held-seat-line")])
        self.assertIn(31, env.procs)
        self.assertIn(32, env.procs)
        self.assertIn(12, env.procs)                                  # protected session untouched
        self.assertEqual(env.placed, [])                              # already held: no new hold
        self.assertEqual(len(env.notices), 1)                         # seat told why

    def test_held_seat_notices_are_rate_limited(self):
        procs, panes = seat_world()
        procs[30] = P(30, 1, 1000, "respin1", 1200 * MB)
        env = FakeEnv(procs, panes, held={"orchestrator": {}})
        k = ok.Killer(env, self_pid=SELF)
        k.step()
        env.procs[31] = P(31, 1, 1000, "respin2", 1300 * MB)
        env.t += 1
        k.step()
        self.assertEqual(len(env.records), 2)
        self.assertEqual(len(env.notices), 1)

    def test_hold_is_rearmed_until_released(self):
        procs, panes = seat_world()
        env = FakeEnv(procs, panes, held={"orchestrator": {}})
        k = ok.Killer(env, self_pid=SELF)
        k.step()
        self.assertEqual(env.rearmed, ["orchestrator"])
        env.t += 5
        k.step()
        self.assertEqual(env.rearmed, ["orchestrator"])                      # not every tick
        env.t += ok.REARM_SECONDS
        k.step()
        self.assertEqual(env.rearmed, ["orchestrator", "orchestrator"])

    def test_held_only_tick_scans_just_held_uids(self):
        procs, panes = seat_world()
        env = FakeEnv(procs, panes, held={"orchestrator": {}})
        k = ok.Killer(env, self_pid=SELF)
        k.step()                                                      # first tick: full refresh
        env.t += 1
        k.step()
        self.assertEqual(env.read_uid_filters[-1], {1000})


class CacheAndRaceTests(unittest.TestCase):
    def test_tmux_not_forked_every_tick_and_reused_pids_checked_by_starttime(self):
        procs, panes = seat_world()
        procs[30] = P(30, 1, 2000, "big", 3000 * MB)
        env = FakeEnv(procs, panes)
        k = ok.Killer(env, debounce=3, self_pid=SELF)
        env.pressure()
        env.mem["SwapFree"] = (4 << 20) * 15 // 100                    # not urgent: debounce applies
        k.step()
        calls = env.tmux_calls
        env.t += 1
        k.step()
        self.assertEqual(env.tmux_calls, calls)                       # reused cache, no re-fork
        # pid 12 dies and is reused by a hog with a new start-time: no longer protected
        env.procs[12] = P(12, 1, 2000, "reused-hog", 6000 * MB, start=777777)
        env.t += 1
        recs = k.step()
        self.assertEqual(recs[0]["pid"], 12)

    def test_refresh_after_interval_reforks_tmux(self):
        procs, panes = seat_world()
        env = FakeEnv(procs, panes)
        k = ok.Killer(env, self_pid=SELF)
        k.step()
        calls = env.tmux_calls
        env.t += ok.PROTECT_REFRESH_SECONDS + 1
        k.step()
        self.assertGreater(env.tmux_calls, calls)

    def test_vanished_pid_before_signal_is_skipped_not_crashed(self):
        procs, panes = seat_world()
        procs[30] = P(30, 1, 2000, "gone-soon", 5000 * MB)
        env = FakeEnv(procs, panes)
        env.vanish_on_signal = True
        k = ok.Killer(env, debounce=1, self_pid=SELF)
        env.pressure()
        (rec,) = k.step()
        self.assertEqual(rec["signal"], "gone")

    def test_pid_reuse_between_snapshot_and_kill_sends_no_signal(self):
        procs, panes = seat_world()
        procs[30] = P(30, 1, 2000, "victim", 5000 * MB)
        env = FakeEnv(procs, panes)
        k = ok.Killer(env, debounce=1, self_pid=SELF)
        env.pressure()
        victim = procs[30]
        env.procs[30] = P(30, 1, 2000, "innocent", 1 * MB, start=victim.start + 1)
        self.assertEqual(k.terminate(victim), "gone")
        self.assertEqual(env.signals, [])

    def test_real_proc_reader_survives_vanished_pids(self):
        env = ok.Env("/nonexistent", Path("/nonexistent-state"))
        self.assertIsNone(env.start_of(2 ** 22 + 12345))              # no such pid -> None, no raise


class EmptySeatConfigTests(unittest.TestCase):
    """B2: no seat list at all must fail toward protection, never protect nothing."""

    def test_empty_accounts_protect_all_nonroot_tmux_trees_and_plumbing(self):
        procs, panes = seat_world()
        procs[40] = P(40, 1, 0, "tmux: server", 5 * MB)               # root's: not protected
        procs[41] = P(41, 12, 1000, "mcp", 50 * MB, start=procs[12].start + 50)   # startup child
        procs[42] = P(42, 12, 1000, "task", 50 * MB, start=LATE)
        env = FakeEnv(procs, panes)
        env.cfg = ok.SeatConfig()
        with self.assertLogs("pantheon-oom-killer", "ERROR"):
            prot = ok.compute_protected(procs, env.cfg, env)
        self.assertTrue(prot.failsafe)
        self.assertTrue({5, 6, 10, 11, 12, 13, 14, 41} <= set(prot.pids))
        self.assertNotIn(42, prot.pids)
        self.assertNotIn(40, prot.pids)

    def test_pressure_with_empty_seat_list_kills_unprotected_not_a_session(self):
        procs, panes = seat_world(session_kb=2000 * MB)
        procs[30] = P(30, 1, 2000, "hog", 1500 * MB)
        env = FakeEnv(procs, panes)
        env.cfg = ok.SeatConfig()
        k = ok.Killer(env, debounce=1, self_pid=SELF)
        env.pressure()
        with self.assertLogs("pantheon-oom-killer", "ERROR"):
            (rec,) = k.step()
        self.assertEqual(rec["pid"], 30)
        self.assertIn(12, env.procs)


class DegradedPathTests(unittest.TestCase):
    def broken(self, env):
        k = ok.Killer(env, debounce=1, self_pid=SELF)

        def boom():
            raise RuntimeError("tick fault")
        k.step = boom
        return k

    def test_tick_swallows_exceptions_and_counts_failures(self):
        env = FakeEnv(*seat_world())
        k = self.broken(env)
        with self.assertLogs("pantheon-oom-killer", "ERROR"):
            k.tick()
            k.tick()
        self.assertEqual(k.fails, 2)
        self.assertEqual(env.signals, [])

    def test_after_three_failures_degraded_kills_with_emergency_set(self):
        procs, panes = seat_world(session_kb=2000 * MB)
        procs[30] = P(30, 1, 2000, "hog", 1500 * MB)
        env = FakeEnv(procs, panes)
        k = self.broken(env)
        env.pressure()
        with self.assertLogs("pantheon-oom-killer", "ERROR") as cm:
            for _ in range(3):
                k.tick()
        self.assertEqual([s for p, s in env.signals][:1], [signal.SIGTERM])
        self.assertEqual(env.records[0]["pid"], 30)
        self.assertEqual(env.records[0]["reason"], "pressure-degraded")
        self.assertIn(12, env.procs)                                  # session survived
        self.assertTrue(any("DEGRADED" in m for m in cm.output))

    def test_degraded_prefers_last_good_protected_set(self):
        procs, panes = seat_world(session_kb=2000 * MB)
        procs[30] = P(30, 1, 2000, "hog", 1500 * MB)
        env = FakeEnv(procs, panes)
        k = ok.Killer(env, debounce=1, self_pid=SELF)
        k.step()                                                      # healthy: builds protected set
        self.assertTrue(k.have_prot)
        k.step = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        env.pressure()
        with self.assertLogs("pantheon-oom-killer", "ERROR") as cm:
            for _ in range(3):
                k.tick()
        self.assertTrue(any("last-good" in m for m in cm.output))
        self.assertEqual(env.records[0]["pid"], 30)

    def test_success_resets_failure_count(self):
        env = FakeEnv(*seat_world())
        k = ok.Killer(env, self_pid=SELF)
        k.fails = 2
        k.tick()
        self.assertEqual(k.fails, 0)

    def test_healthy_degraded_step_is_a_noop(self):
        env = FakeEnv(*seat_world())
        self.assertEqual(ok.Killer(env, self_pid=SELF).degraded_step(), [])


class ReleaseRaceAndLoopTests(unittest.TestCase):
    def test_release_between_read_and_rearm_does_not_rearm(self):
        procs, panes = seat_world()

        class Racy(FakeEnv):
            calls = 0

            def held(self):
                Racy.calls += 1
                return {"orchestrator": {}} if Racy.calls == 1 else {}
        env = Racy(procs, panes)
        ok.Killer(env, self_pid=SELF).step()
        self.assertEqual(env.rearmed, [])

    def test_same_cmdline_killed_three_times_then_loop_recorded_once(self):
        procs, panes = seat_world()
        env = FakeEnv(procs, panes, held={"orchestrator": {}})
        k = ok.Killer(env, self_pid=SELF)
        for i in range(6):
            env.procs[100 + i] = P(100 + i, 12, 1000, "respawner", 1200 * MB, start=LATE,
                                   cmd="/usr/bin/echo-daemon --serve")
            k.step()
            env.t += 1
        reasons = [r["reason"] for r in env.records]
        self.assertEqual(reasons.count("held-seat-line"), 3)
        self.assertEqual(reasons.count("held-line-loop"), 1)
        self.assertIn(105, env.procs)                                 # no longer killed

    def test_loop_window_expires(self):
        procs, panes = seat_world()
        env = FakeEnv(procs, panes, held={"orchestrator": {}})
        k = ok.Killer(env, self_pid=SELF)
        for i in range(3):
            env.procs[100 + i] = P(100 + i, 12, 1000, "r", 1200 * MB, start=LATE, cmd="same")
            k.step()
        env.t += ok.HELD_LOOP_WINDOW + 1
        env.procs[200] = P(200, 12, 1000, "r", 1200 * MB, start=LATE, cmd="same")
        recs = k.step()
        self.assertEqual([r["reason"] for r in recs], ["held-seat-line"])


class RecordRotationTests(unittest.TestCase):
    def test_append_then_trim_only_past_slack(self):
        import tempfile
        old = (ok.MAX_RECORD_LINES, ok.RECORD_TRIM_SLACK)
        ok.MAX_RECORD_LINES, ok.RECORD_TRIM_SLACK = 10, 5
        try:
            with tempfile.TemporaryDirectory() as d:
                env = ok.Env("/nonexistent", Path(d))
                path = Path(d) / "kills.jsonl"
                for i in range(15):
                    env.write_record({"i": i})
                self.assertEqual(len(path.read_text().splitlines()), 15)   # no per-append rewrite
                env.write_record({"i": 15})
                lines = path.read_text().splitlines()
                self.assertEqual(len(lines), 10)
                self.assertIn('"i": 15', lines[-1])
        finally:
            ok.MAX_RECORD_LINES, ok.RECORD_TRIM_SLACK = old


class WalkSessionsTests(unittest.TestCase):
    def test_collects_both_key_shapes(self):
        doc = {"a": {"personas": {"x": {"tmux_session": "s1"}}},
               "b": {"tmux": {"session": "pantheon-{persona}", "window": "harness"}},
               "c": [{"tmux": {"session": "s2"}}, {"session": "prose-only"}]}
        self.assertEqual(ok._walk_sessions(doc), {"s1", "pantheon-{persona}", "s2"})


class DryRunAndScriptTests(unittest.TestCase):
    def test_dry_run_prints_and_kills_nothing(self):
        procs, panes = seat_world()
        procs[30] = P(30, 1, 2000, "big", 3000 * MB)
        env = FakeEnv(procs, panes)
        lines = []
        ok.describe(env, ok.Killer(env, self_pid=SELF), out=lines.append)
        text = "\n".join(lines)
        self.assertIn("protected pids (5)", text)
        self.assertIn("would kill if triggered: pid 30", text)
        self.assertEqual(env.signals, [])
        self.assertEqual(env.records, [])
        self.assertEqual(env.placed, [])

    def test_scripts_have_no_cmdline_substring_predicates(self):
        here = Path(__file__).resolve().parent
        for name in ("install.sh", "uninstall.sh"):
            text = (here / name).read_text()
            self.assertFalse(re.search(r"\b(pgrep|pkill)\s+-\w*f", text), name)

    def test_no_forbidden_byte_sequence(self):
        here = Path(__file__).resolve().parent
        needle = "HERME" + "S.md"
        for f in here.iterdir():
            if f.is_file() and f.suffix in {".py", ".sh", ".service"}:
                self.assertNotIn(needle, f.read_text(), f.name)


class ReleaseCliTests(unittest.TestCase):
    """2026-10-22: a non-root --release printed "released" and exited 0 while the hold stayed."""

    def setUp(self):
        import json, os, tempfile
        self.json, self.os = json, os
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / "oom"
        self.state.mkdir()
        (self.state / "holds.json").write_text(json.dumps({"artisan": {"reason": "x", "since": "t"}}))
        self._env = os.environ.get("PANTHEON_MAINTENANCE_REGISTRY")
        os.environ["PANTHEON_MAINTENANCE_REGISTRY"] = str(self.tmp / "maint.json")

    def tearDown(self):
        import shutil
        (self.state / "holds.json").chmod(0o644)
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._env is None:
            self.os.environ.pop("PANTHEON_MAINTENANCE_REGISTRY", None)
        else:
            self.os.environ["PANTHEON_MAINTENANCE_REGISTRY"] = self._env

    def _release(self):
        import io, contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ok.main(["--release", "artisan", "--state-dir", str(self.state)])
        return rc, out.getvalue(), err.getvalue()

    def test_a_writable_release_clears_the_hold(self):
        rc, out, _ = self._release()
        self.assertEqual((rc, out.strip()), (0, "released"))
        self.assertEqual(self.json.loads((self.state / "holds.json").read_text()), {})

    def test_an_unwritable_hold_file_is_a_failed_release(self):
        if self.os.geteuid() == 0:
            self.skipTest("root can write a 0444 file")
        (self.state / "holds.json").chmod(0o444)
        rc, out, err = self._release()
        self.assertEqual(rc, 1)
        self.assertNotIn("released", out)
        self.assertIn("NOT released", err)
        self.assertIn("artisan", self.json.loads((self.state / "holds.json").read_text()))


if __name__ == "__main__":
    unittest.main()
