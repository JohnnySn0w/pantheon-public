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


# T-4193: specialist's successor pane, 2026-09-28 21:25Z (reconstructed line-for-line from the
# live capture; long lines shortened). It was WORKING (a turn from the SessionStart
# auto-read), but a tip line and a transient notice pushed the spinner out of the
# 6-line bottom window, and the footer carried no "esc to interrupt". The restart
# was reported as a failed kick (exit 8) and counted towards a false "wedged" alert.
SPECIALIST_WORKING_2026_09_28 = """\
● Memory pin landed (owed part 5). Now building the code change in the working tree.
  Finding digest/send tests in the test suite
  ⎿  tools/ticket_grooming.py
· Manifesting… (5m 58s · ↓ 18.4k tokens · thinking some more with xhigh effort)
  ⎿  Tip: Use /btw to ask a quick side question without interrupting Claude's current work
                                                                         Settings dialog dismissed
──────────────────────────────────────────────────
❯ 
──────────────────────────────────────────────────
  ⏸ manual mode on · ← for agents
"""
FINISHED_TURN_IDLE = """\
● Done. Resolved T-9999.
✻ Crunched for 5m 3s
──────────────────────────────────────────────────
❯ 
──────────────────────────────────────────────────
  ? for shortcuts
"""
SPINNER_DEEP_IN_SCROLLBACK = ("· Quoted… (1m 2s · ↓ 1.2k tokens)\n" + "● line\n" * 8
                              + FINISHED_TURN_IDLE)


# T-5139: three real busy renders the T-4193 rule missed. Each produced a false
# mailbox_wake exit 8 ("submit unconfirmed") while the pane was generating.
# (a) recovery 2026-10-17 06:40:25Z: a hook-status prefix before the elapsed time.
RECOVERY_HOOK_PREFIX_2026_10_17 = """\
● Checking the ticket state.
✻ Caramelizing… (running PreToolUse hooks… 1/3 · 6s · ↓ 63 tokens)
──────────────────────────────────────────────────
❯ 
──────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""
# (b) recovery 2026-10-16 22:51:41Z: turn start, before any elapsed/token counter renders.
RECOVERY_TURN_START_2026_10_16 = """\
● Draining the mailbox.
· Synthesizing…
  ⎿  Running 1 shell command…
──────────────────────────────────────────────────
❯ 
──────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""
# (c) orchestrator 2026-10-20 05:17:48Z (journal capture, verbatim bottom): the wake turn was in
# orchestrator's jsonl at 05:17:46.761Z; a hook-status paren with an elapsed time, no tokens.
ORCHESTRATOR_HOOK_NO_TOKENS_2026_10_20 = """\
  is lost if you finish your current turn first.
✻ Deciphering… (running UserPromptSubmit hook · 3s)
────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  paste again to expand                  ✔ Update installed · Restart to apply
"""
# Must stay IDLE: an assistant message (● bullet) that happens to end in an ellipsis,
# directly above an empty composer.
IDLE_MESSAGE_WITH_ELLIPSIS = """\
● Waiting on orchestrator's merge…
✻ Worked for 2m 10s
──────────────────────────────────────────────────
❯ 
──────────────────────────────────────────────────
  ? for shortcuts
"""


class SpinnerAboveComposerTest(unittest.TestCase):
    def test_specialist_working_successor_reads_as_started(self):
        self.assertTrue(kv.kicked_turn_started(SPECIALIST_WORKING_2026_09_28))

    def test_a_finished_turn_is_not_started(self):
        self.assertFalse(kv.kicked_turn_started(FINISHED_TURN_IDLE))

    def test_a_spinner_line_deep_in_scrollback_is_not_live(self):
        self.assertFalse(kv.kicked_turn_started(SPINNER_DEEP_IN_SCROLLBACK))

    def test_hook_prefix_before_elapsed_reads_as_started_T5139(self):
        self.assertTrue(kv.kicked_turn_started(RECOVERY_HOOK_PREFIX_2026_10_17))

    def test_turn_start_spinner_without_counter_reads_as_started_T5139(self):
        self.assertTrue(kv.kicked_turn_started(RECOVERY_TURN_START_2026_10_16))

    def test_hook_status_without_tokens_reads_as_started_T5139(self):
        self.assertTrue(kv.kicked_turn_started(ORCHESTRATOR_HOOK_NO_TOKENS_2026_10_20))

    def test_assistant_message_ending_in_ellipsis_is_not_started_T5139(self):
        self.assertFalse(kv.kicked_turn_started(IDLE_MESSAGE_WITH_ELLIPSIS))


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

    def test_accept_running_turn_stops_refire_into_a_busy_pane(self):
        # T-4088: another injector started a turn just before the kick, so the pasted
        # kick is QUEUED (inject reports unconfirmed). Opt-in: a running turn = the
        # session is not idle -> delivered, NO duplicate re-paste.
        res = kv.deliver_kick_verified(
            self._inject(ok=False), self._capture_seq([BUSY_PANE]), "pane", "go",
            sleep=self._sleep, max_attempts=3, accept_running_turn=True)
        self.assertTrue(res.ok)
        self.assertEqual(len(self.injects), 1, "must not stack duplicate kicks behind a running turn")

    def test_accept_running_turn_still_refires_on_an_idle_pane(self):
        # The opt-in only accepts evidence of a running turn; a failed send into an
        # IDLE pane still re-fires and ultimately fails.
        res = kv.deliver_kick_verified(
            self._inject(ok=False), self._capture_seq([IDLE_PANE]), "pane", "go",
            sleep=self._sleep, poll_secs=2, poll_interval=1, max_attempts=2,
            accept_running_turn=True)
        self.assertFalse(res.ok)
        self.assertEqual(len(self.injects), 2)

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
        kv._build_inject = lambda path, render_wait=0.0: (
            lambda pane, prompt: calls.append((path, pane, prompt, render_wait)) or True)
        kv._capture = lambda pane: BUSY_PANE
        rc = kv.main(["--pane", "s:0", "--prompt", "go", "--settle", "0",
                      "--poll-secs", "0.05", "--poll-interval", "0.01",
                      "--send-keys-submit", "/x/sks.sh"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][0], "/x/sks.sh", "--send-keys-submit plumbed to the injector")
        self.assertEqual(calls[0][1], "s:0", "--pane plumbed through")
        self.assertEqual(calls[0][3], 0.0, "no --render-wait: the gate stays off")

    def test_cli_render_wait_plumbed_to_the_injector(self):
        # T-4456: the kick lands on a fresh TUI, so the restart scripts opt in.
        calls = []
        kv._build_inject = lambda path, render_wait=0.0: (
            lambda pane, prompt: calls.append(render_wait) or True)
        kv._capture = lambda pane: BUSY_PANE
        rc = kv.main(["--pane", "s:0", "--prompt", "go", "--settle", "0",
                      "--poll-secs", "0.05", "--poll-interval", "0.01", "--render-wait", "8"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [8.0])

    def test_cli_exit_8_when_kick_drops(self):
        kv._build_inject = lambda path, render_wait=0.0: (lambda pane, prompt: True)
        kv._capture = lambda pane: IDLE_PANE
        rc = kv.main(["--pane", "s:0", "--prompt", "go", "--settle", "0",
                      "--poll-secs", "0.04", "--poll-interval", "0.01", "--max-attempts", "2"])
        self.assertEqual(rc, 8, "no dispatched turn after bounded re-fires → exit 8")


class InjectArgvTest(unittest.TestCase):
    """T-4456: the injector's argv to send_keys_submit.sh. A post-restart kick lands
    on a freshly booted TUI, whose paste render can lag the Enter; ungated, the
    verify then reads the empty composer as submitted (T-4362, T-4455). With
    render_wait > 0 the helper gets --render-wait ahead of <pane> <text>, the order
    its CLI parses; at 0 the argv is exactly the pre-T-4456 one."""

    def _argv(self, render_wait):
        import subprocess
        seen = []

        class _Done:
            returncode = 0

        real = subprocess.run
        subprocess.run = lambda argv, **kw: seen.append(argv) or _Done()
        try:
            ok = kv._build_inject("/x/sks.sh", render_wait=render_wait)("s:0", "go")
        finally:
            subprocess.run = real
        self.assertTrue(ok)
        return seen[0]

    def test_gated(self):
        self.assertEqual(self._argv(8.0), ["/x/sks.sh", "--render-wait", "8", "s:0", "go"])
        self.assertEqual(self._argv(0.5), ["/x/sks.sh", "--render-wait", "0.5", "s:0", "go"])

    def test_off_is_the_old_argv(self):
        self.assertEqual(self._argv(0.0), ["/x/sks.sh", "s:0", "go"])


class KickRefusedTest(unittest.TestCase):
    """T-4646: send_keys_submit exit 3 means the pane shows a startup prompt or a dialog,
    not a composer. A re-fire would paste into it again and press Enter on whatever is
    highlighted. On 2026-10-05 specialist's restart kick picked the auto-mode nudge's "Yes"."""

    def _rc(self, rc):
        import subprocess

        class _Done:
            returncode = rc

        real = subprocess.run
        subprocess.run = lambda argv, **kw: _Done()
        self.addCleanup(setattr, subprocess, "run", real)
        return kv._build_inject("/x/sks.sh", render_wait=8.0)

    def test_refusal_is_terminal_no_refire(self):
        injects = []

        def inject(pane, prompt):
            injects.append(pane)
            raise kv.KickRefused("send_keys_submit exit 3 on pane")

        res = kv.deliver_kick_verified(inject, lambda pane: IDLE_PANE, "pane", "go",
                                       sleep=lambda t: None, max_attempts=3,
                                       accept_running_turn=True)
        self.assertFalse(res.ok)
        self.assertEqual(res.attempts, 1)
        self.assertEqual(len(injects), 1, "a refused kick must not re-fire")
        self.assertIn("refused", res.detail)

    def test_injector_raises_on_exit_3(self):
        with self.assertRaises(kv.KickRefused):
            self._rc(3)("s:0", "go")

    def test_other_injector_failures_stay_retryable(self):
        self.assertFalse(self._rc(1)("s:0", "go"))
        self.assertFalse(self._rc(2)("s:0", "go"))


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
