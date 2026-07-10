"""test_mailbox_wake.py — the wake half of the ORG-026 mailbox (T-0772).

Covers the decision logic (peek / debounce / wake), the marker advance-only-on-success
rule, and the wake-only invariant (the waker NEVER moves/acks a message). The live
tmux inject is stubbed — these are pure-logic tests, no pane required.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mailbox  # noqa: E402
import mailbox_wake as mw  # noqa: E402


class WakeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "inbox"
        mailbox.provision(self.inbox)
        self.new = self.inbox / "new"
        self.marker = self.inbox / mw._MARKER_NAME
        self.injected = []  # records (pane, prompt, harness) the stub was asked to send

    def tearDown(self):
        self._tmp.cleanup()

    def _ok_inject(self, pane, prompt, harness=mw._DEFAULT_HARNESS):
        self.injected.append((pane, prompt, harness))
        return True

    def _fail_inject(self, pane, prompt, harness=mw._DEFAULT_HARNESS):
        self.injected.append((pane, prompt, harness))
        return False

    def _run(self, inject):
        return mw.run("recovery", inbox=self.inbox, do_inject=inject)

    # -------------------------------------------------------------- empty/peek
    def test_empty_inbox_no_wake(self):
        rc = self._run(self._ok_inject)
        self.assertEqual(rc, 0)
        self.assertEqual(self.injected, [], "empty inbox must not kick")

    def test_peek_skips_dotfiles_and_is_sorted(self):
        mailbox.send(self.inbox, {"i": 1}, sender="orchestrator")
        mailbox.send(self.inbox, {"i": 2}, sender="specialist")
        (self.new / ".hidden").write_text("x")  # lock/marker-style noise
        names = mw.peek(self.new)
        self.assertEqual(len(names), 2)
        self.assertEqual(names, sorted(names))
        self.assertFalse(any(n.startswith(".") for n in names))

    # --------------------------------------------------------------- wake path
    def test_wake_injects_and_advances_marker(self):
        mailbox.send(self.inbox, {"task": "T-0767"}, sender="orchestrator")
        rc = self._run(self._ok_inject)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1, "a fresh message must kick once")
        self.assertIn("orchestrator", self.injected[0][1])
        self.assertEqual(self.injected[0][0], "claude-telegram-bot-fallback:0")
        self.assertTrue(self.marker.exists(), "marker advances on a landed kick")

    def test_codex_pane_passes_codex_harness(self):
        # T-1106: codex's pane must select the codex submit-verify profile, else
        # send_keys_submit false-passes on the always-absent claude-code sentinel.
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        mw.run("codex", inbox=self.inbox, do_inject=self._ok_inject)
        self.assertEqual(len(self.injected), 1)
        self.assertEqual(self.injected[0][2], "codex", "codex pane must pass --harness codex")
        self.assertEqual(self.injected[0][0], "codex-session")

    def test_claude_persona_passes_default_harness(self):
        # A Claude-Code persona (recovery) keeps the default claude-code profile.
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        mw.run("recovery", inbox=self.inbox, do_inject=self._ok_inject)
        self.assertEqual(self.injected[0][2], "claude-code")

    def test_wake_only_never_acks(self):
        # The load-bearing invariant: the waker leaves every message in new/ (no move).
        mid = mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self._run(self._ok_inject)
        self.assertTrue((self.new / mid).exists(), "waker must NOT move the message out of new/")
        self.assertFalse((self.inbox / "cur" / mid).exists(), "waker must NOT ack to cur/")

    # ----------------------------------------------------------------- debounce
    def test_debounce_no_rekick_while_unchanged(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self._run(self._ok_inject)            # first → wake
        self._run(self._ok_inject)            # second, nothing newer → debounced
        self.assertEqual(len(self.injected), 1, "must not re-kick while newest is unchanged")

    def test_newer_message_rekicks(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        self._run(self._ok_inject)            # wake (1)
        mailbox.send(self.inbox, {"x": 2}, sender="specialist")  # genuinely newer
        self._run(self._ok_inject)            # wake again (2)
        self.assertEqual(len(self.injected), 2, "a newer message must re-nudge")

    # ----------------------------------------------------------- failed inject
    def test_failed_kick_does_not_advance_marker(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        rc = self._run(self._fail_inject)
        self.assertEqual(rc, 8, "a failed kick returns non-zero")
        self.assertFalse(self.marker.exists(), "marker must NOT advance on a dropped kick")
        # Next fire retries (marker never advanced) — message still in new/.
        rc2 = self._run(self._ok_inject)
        self.assertEqual(rc2, 0)
        self.assertEqual(len(self.injected), 2, "dropped kick is retried on the next fire")

    # --------------------------------------------------------------- malformed
    def test_malformed_message_does_not_break_waker(self):
        (self.new / "00000000000000000001.1.0.x").write_text("not json\n")
        rc = self._run(self._ok_inject)  # _senders must swallow the parse error
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.injected), 1, "still wakes despite an unparseable message")

    def test_unknown_agent_rejected(self):
        self.assertEqual(mw.run("nobody", inbox=self.inbox, do_inject=self._ok_inject), 64)


if __name__ == "__main__":
    unittest.main()
