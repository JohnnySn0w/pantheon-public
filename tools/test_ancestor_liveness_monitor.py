"""test_ancestor_liveness_monitor.py — the-ancestor wedge-recovery monitor (T-1134).

Pure-orchestration tests via an injected fake Ops + a tmp state dir — no sudo/tmux/alerts.
Covers the alive/not-alive signal, the consecutive-N transient guard, the cooldown, and the
load-bearing ship-dark invariant: the auto-restart NEVER fires unless the flag is armed, but
the Recovery alert ALWAYS fires on a confirmed wedge.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ancestor_liveness_monitor as alm  # noqa: E402


class FakeOps(alm.Ops):
    def __init__(self, *, pane="  ? for shortcuts", restart_ok=True, t=1000):
        self._pane = pane
        self._restart_ok = restart_ok
        self._t = t
        self.calls = []

    def capture_pane(self):
        self.calls.append(("capture",)); return self._pane

    def restart(self):
        self.calls.append(("restart",)); return self._restart_ok

    def alert(self, msg):
        self.calls.append(("alert", msg))

    def now(self):
        return self._t

    def _names(self):
        return [c[0] for c in self.calls]


class IsAliveTest(unittest.TestCase):
    def test_idle_sentinel_alive(self):
        self.assertTrue(alm.is_alive("foo\n  ? for shortcuts · ← for agents"))

    def test_busy_sentinel_alive(self):
        self.assertTrue(alm.is_alive("· Working…\n  esc to interrupt"))

    def test_none_not_alive(self):
        self.assertFalse(alm.is_alive(None))

    def test_no_sentinel_not_alive(self):
        self.assertFalse(alm.is_alive("some garbled\nstuck pane\nno footer"))

    def test_sentinel_only_in_scrollback_not_alive(self):
        # A sentinel far above the live bottom region (e.g. echoed in a transcript) must
        # NOT count — only the bottom 6 non-blank lines are the live status.
        body = "? for shortcuts\n" + "\n".join(f"line{i}" for i in range(20))
        self.assertFalse(alm.is_alive(body))


class CheckOnceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._tmp.name)
        self._orig_flag = alm.REMEDIATE_ENABLED

    def tearDown(self):
        alm.REMEDIATE_ENABLED = self._orig_flag
        self._tmp.cleanup()

    def _run(self, ops, **kw):
        return alm.check_once(ops=ops, state_dir=self.state_dir, threshold=3, cooldown=1800, **kw)

    def test_alive_resets_counter(self):
        ops = FakeOps(pane="  ? for shortcuts")
        self.assertEqual(self._run(ops), "alive")
        self.assertEqual(ops._names(), ["capture"])  # no alert/restart

    def test_not_alive_below_threshold_is_pending(self):
        ops = FakeOps(pane="garbled")
        self.assertEqual(self._run(ops), "wedge_pending")  # 1/3
        self.assertEqual(self._run(ops), "wedge_pending")  # 2/3
        self.assertNotIn("alert", ops._names())

    def test_threshold_with_flag_off_alerts_but_does_not_restart(self):
        alm.REMEDIATE_ENABLED = False
        ops = FakeOps(pane="garbled")
        self._run(ops); self._run(ops)
        self.assertEqual(self._run(ops), "wedge_disarmed")  # 3/3
        self.assertIn("alert", ops._names())
        self.assertNotIn("restart", ops._names(), "ship-dark: must NOT restart when flag off")

    def test_threshold_with_flag_on_alerts_and_restarts(self):
        alm.REMEDIATE_ENABLED = True
        ops = FakeOps(pane="garbled")
        self._run(ops); self._run(ops)
        self.assertEqual(self._run(ops), "wedge_remediated")
        self.assertIn("alert", ops._names())
        self.assertIn("restart", ops._names())

    def test_cooldown_suppresses_second_restart(self):
        alm.REMEDIATE_ENABLED = True
        ops = FakeOps(pane="garbled", t=1000)
        self._run(ops); self._run(ops); self._run(ops)  # first restart at t=1000
        ops._t = 1000 + 600  # 10 min later, within 1800s cooldown
        # counter was reset to 0 by the restart, so climb back to threshold
        self._run(ops); self._run(ops)
        self.assertEqual(self._run(ops), "wedge_cooldown")
        self.assertEqual([c for c in ops._names() if c == "restart"], ["restart"], "only one restart")

    def test_none_pane_counts_toward_wedge(self):
        # Session absent / unreadable → not-alive; persistent → confirmed wedge + alert.
        alm.REMEDIATE_ENABLED = False
        ops = FakeOps(pane=None)
        self._run(ops); self._run(ops)
        self.assertEqual(self._run(ops), "wedge_disarmed")
        self.assertIn("alert", ops._names())

    def test_dry_run_alerts_nothing(self):
        ops = FakeOps(pane="garbled")
        self._run(ops); self._run(ops)
        self.assertEqual(self._run(ops, dry_run=True), "wedge_alerted")
        self.assertNotIn("alert", ops._names(), "dry-run must not alert or restart")
        self.assertNotIn("restart", ops._names())

    def test_recovery_resets_after_wedge(self):
        ops = FakeOps(pane="garbled")
        self._run(ops); self._run(ops)
        ops._pane = "  ? for shortcuts"  # the-ancestor recovers
        self.assertEqual(self._run(ops), "alive")
        # a subsequent not-alive starts the count fresh, not at the prior 2
        ops._pane = "garbled"
        self.assertEqual(self._run(ops), "wedge_pending")


if __name__ == "__main__":
    unittest.main()
