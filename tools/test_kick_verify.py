"""test_kick_verify.py — post-kick verify + bounded re-fire (tools/kick_verify.py, T-0899).

Pure-orchestration tests via injected inject/capture/sleep — no tmux. Covers: a kick that
takes on the first try, a kick that drops then takes on re-fire, a kick that never takes
(bounded), a failing inject re-firing, and the turn-started detection itself.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kick_verify as kv  # noqa: E402

BUSY_PANE = "working…\n⎿ running\n  esc to interrupt"
IDLE_PANE = "╭────╮\n│ ❯  │\n╰────╯\n  ? for shortcuts"


class TurnStartedTest(unittest.TestCase):
    def test_busy_pane_means_started(self):
        self.assertTrue(kv.kicked_turn_started(BUSY_PANE))

    def test_idle_pane_means_not_started(self):
        self.assertFalse(kv.kicked_turn_started(IDLE_PANE))

    def test_empty_pane_not_started(self):
        self.assertFalse(kv.kicked_turn_started(""))


class DeliverTest(unittest.TestCase):
    def setUp(self):
        self.injects = []
        self.sleeps = []

    def _inject(self, ok=True):
        def f(pane, prompt):
            self.injects.append((pane, prompt)); return ok
        return f

    def _sleep(self, t):
        self.sleeps.append(t)

    def _capture_seq(self, panes):
        """Return successive panes; repeats the last once exhausted."""
        seq = list(panes)
        def f(pane):
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return f

    def test_kick_takes_first_attempt(self):
        # first poll after the inject sees a started turn
        res = kv.deliver_kick_verified(
            self._inject(), self._capture_seq([BUSY_PANE]), "pane", "go",
            sleep=self._sleep, max_attempts=3)
        self.assertTrue(res.ok)
        self.assertEqual(res.attempts, 1)
        self.assertEqual(len(self.injects), 1, "no re-fire when it takes immediately")

    def test_kick_drops_then_takes_on_refire(self):
        # attempt 1: idle for the whole poll window (drop) → re-fire; attempt 2: busy
        captures = [IDLE_PANE, IDLE_PANE, IDLE_PANE, IDLE_PANE, IDLE_PANE, IDLE_PANE, BUSY_PANE]
        res = kv.deliver_kick_verified(
            self._inject(), self._capture_seq(captures), "pane", "go",
            sleep=self._sleep, poll_secs=6, poll_interval=2, max_attempts=3)
        self.assertTrue(res.ok)
        self.assertGreaterEqual(res.attempts, 2)
        self.assertGreaterEqual(len(self.injects), 2, "must re-fire after a drop")

    def test_kick_never_takes_bounded(self):
        res = kv.deliver_kick_verified(
            self._inject(), self._capture_seq([IDLE_PANE]), "pane", "go",
            sleep=self._sleep, poll_secs=4, poll_interval=2, max_attempts=3)
        self.assertFalse(res.ok)
        self.assertEqual(res.attempts, 3)
        self.assertEqual(len(self.injects), 3, "bounded to max_attempts injects")

    def test_failing_inject_refires(self):
        res = kv.deliver_kick_verified(
            self._inject(ok=False), self._capture_seq([BUSY_PANE]), "pane", "go",
            sleep=self._sleep, max_attempts=2)
        self.assertFalse(res.ok, "a send that never succeeds can't confirm a turn")
        self.assertEqual(len(self.injects), 2, "re-fires on inject failure")

    def test_settle_applied_before_each_inject(self):
        kv.deliver_kick_verified(
            self._inject(), self._capture_seq([BUSY_PANE]), "pane", "go",
            sleep=self._sleep, settle_secs=3, max_attempts=1)
        self.assertIn(3, self.sleeps, "the readiness settle is applied")


class CLITest(unittest.TestCase):
    """The CLI (T-0899 wiring) that handoff_and_restart.sh step-8 calls: exit 0 when a
    turn dispatches, exit 8 when it drops; --send-keys-submit is plumbed to the injector.
    inject/capture are patched so no real tmux/subprocess runs."""

    def setUp(self):
        self._orig_build, self._orig_capture = kv._build_inject, kv._capture

    def tearDown(self):
        kv._build_inject, kv._capture = self._orig_build, self._orig_capture

    def test_cli_exit_0_when_turn_dispatches(self):
        calls = []
        kv._build_inject = lambda path: (lambda pane, prompt: calls.append((path, pane, prompt)) or True)
        kv._capture = lambda pane: BUSY_PANE
        rc = kv.main(["--pane", "s:0", "--prompt", "go", "--settle", "0",
                      "--poll-secs", "0.05", "--poll-interval", "0.01",
                      "--send-keys-submit", "/x/sks.sh"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][0], "/x/sks.sh", "--send-keys-submit plumbed to the injector")
        self.assertEqual(calls[0][1], "s:0", "--pane plumbed through")

    def test_cli_exit_8_when_kick_drops(self):
        kv._build_inject = lambda path: (lambda pane, prompt: True)
        kv._capture = lambda pane: IDLE_PANE
        rc = kv.main(["--pane", "s:0", "--prompt", "go", "--settle", "0",
                      "--poll-secs", "0.04", "--poll-interval", "0.01", "--max-attempts", "2"])
        self.assertEqual(rc, 8, "no dispatched turn after bounded re-fires → exit 8")


class CaptureDeclinesOnAnUntrustworthySurface(unittest.TestCase):
    """T-2277: the write and the read share a failure domain. We inject into a
    pane and confirm by capturing THE SAME pane, and a pane in copy-mode returns a
    FROZEN view — returncode 0, non-empty, stale. So during the one fault where
    the keystroke was most likely eaten, the verifier reads a snapshot of a
    previously-working pane and votes SUCCESS."""

    def setUp(self):
        self.calls = []

    def _fake_run(self, in_mode: str, pane_text: str = "esc to interrupt"):
        import types as _t

        def run(cmd, **kw):
            self.calls.append(cmd)
            if "display-message" in cmd:
                return _t.SimpleNamespace(returncode=0, stdout=in_mode + "\n", stderr="")
            return _t.SimpleNamespace(returncode=0, stdout=pane_text, stderr="")
        return run

    def test_a_pane_in_copy_mode_yields_no_text_so_the_caller_retries(self):
        import subprocess
        real = subprocess.run
        subprocess.run = self._fake_run("1")          # in a mode
        try:
            self.assertEqual(kv._capture("p:0"), "",
                             "a frozen surface must produce NO verdict — voting "
                             "on it is how a dropped kick reports as delivered")
        finally:
            subprocess.run = real
        self.assertTrue(any("display-message" in c for c in self.calls),
                        "trustworthiness must be checked BEFORE the capture")
        self.assertFalse(any("capture-pane" in c for c in self.calls),
                         "an untrusted surface must not even be read")

    def test_a_normal_pane_is_captured(self):
        import subprocess
        real = subprocess.run
        subprocess.run = self._fake_run("0", "esc to interrupt")
        try:
            self.assertIn("esc to interrupt", kv._capture("p:0"))
        finally:
            subprocess.run = real

    def test_an_unreadable_mode_probe_is_treated_as_in_mode(self):
        """Fail-SAFE: the only use of this is deciding whether to TRUST a capture,
        so an answer we cannot vouch for must not license trusting one."""
        import subprocess, types as _t
        real = subprocess.run

        def run(cmd, **kw):
            if "display-message" in cmd:
                return _t.SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return _t.SimpleNamespace(returncode=0, stdout="anything", stderr="")
        subprocess.run = run
        try:
            self.assertEqual(kv._capture("p:0"), "")
        finally:
            subprocess.run = real


if __name__ == "__main__":
    unittest.main()
