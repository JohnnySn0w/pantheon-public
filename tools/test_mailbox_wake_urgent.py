"""test_mailbox_wake_urgent.py — mailbox_wake's urgent debounce-bypass (T-1641 Ph2).

Covers `_has_urgent()` and the new bypass branch in `decide()`. Gated by
`mailbox.urgent_phase2_enabled()` (same flag as the send-side mechanics in
tools/test_mailbox_urgent_phase2.py) — the load-bearing property proven here is
that with the flag OFF, `decide()` is byte-for-byte identical to its pre-T-1641
behavior (the existing test_mailbox_wake.py suite already covers that shape
exhaustively; this file only adds urgent-specific cases and re-proves the
flag-off floor for them specifically).
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mailbox  # noqa: E402
import mailbox_wake as mw  # noqa: E402


def _enabled():
    return mock.patch.dict(os.environ, {mailbox._URGENT_PHASE2_ENV: "1"})


def _disabled():
    ctx = mock.patch.dict(os.environ, {}, clear=False)
    return ctx


class HasUrgentTest(unittest.TestCase):
    """Pure-ish unit tests for `_has_urgent` — no waker/decide involved."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "inbox"
        mailbox.provision(self.inbox)
        self.new = self.inbox / "new"

    def tearDown(self):
        self._tmp.cleanup()

    def test_true_when_any_message_urgent(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator", severity="normal")
        mailbox.send(self.inbox, {"x": 2}, sender="orchestrator", subject="s2",
                     severity="urgent", dedupe_pending=False)
        names = mw.peek(self.new)
        self.assertTrue(mw._has_urgent(self.new, names))

    def test_false_when_all_normal(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")
        names = mw.peek(self.new)
        self.assertFalse(mw._has_urgent(self.new, names))

    def test_older_urgent_behind_newer_normal_still_true(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator", subject="s1",
                     severity="urgent", dedupe_pending=False)
        mailbox.send(self.inbox, {"x": 2}, sender="orchestrator", subject="s2",
                     severity="normal", dedupe_pending=False)
        names = mw.peek(self.new)
        self.assertEqual(len(names), 2)
        # "newest" (names[-1]) is the NORMAL one — a newest-only check would miss
        # the older urgent message still sitting unconsumed.
        self.assertTrue(mw._has_urgent(self.new, names))

    def test_malformed_message_fails_closed(self):
        (self.new / "00000000000000000001.1.000000.x").write_text("not json\n")
        names = mw.peek(self.new)
        self.assertFalse(mw._has_urgent(self.new, names),
                         "an unreadable message must never manufacture a bypass")

    def test_empty_names_false(self):
        self.assertFalse(mw._has_urgent(self.new, []))


class DebounceBypassTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self._tmp.name) / "inbox"
        mailbox.provision(self.inbox)
        self.injected = []

    def tearDown(self):
        self._tmp.cleanup()

    def _ok_inject(self, pane, prompt, harness=mw._DEFAULT_HARNESS):
        self.injected.append((pane, prompt, harness))
        return True

    def _run(self, now=1000.0, session="S1"):
        # T-4292: UNKNOWN now needs positive evidence to inject. These cases test
        # the debounce/urgent-bypass logic, not that policy, so the fallback is
        # stubbed permissive (as in test_mailbox_wake.py). pane_idle stays None:
        # True would add decide()'s one-shot idle re-kick and change the counts.
        return mw.run("recovery", inbox=self.inbox, do_inject=self._ok_inject,
                      session_key_fn=lambda pane: session, now=now,
                      pane_idle_fn=lambda pane: None,
                      unknown_pane_ok_fn=lambda pane, harness: True)

    def test_flag_off_urgent_still_debounces_like_today(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator", subject="wedge",
                    severity="urgent")
        with _disabled():
            os.environ.pop(mailbox._URGENT_PHASE2_ENV, None)
            self._run(now=1000.0)   # wake (1) — condition 1, newer message
            self._run(now=1001.0)   # nothing newer, flag off → debounced
        self.assertEqual(len(self.injected), 1,
                         "flag-off must reproduce pre-T-1641 debounce exactly")

    def test_flag_on_urgent_bypasses_debounce_every_sweep(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator", subject="wedge",
                    severity="urgent")
        with _enabled():
            self._run(now=1000.0)   # wake (1) — condition 1
            self._run(now=1001.0)   # would debounce normally; urgent bypasses → wake (2)
            self._run(now=1002.0)   # still urgent, still unconsumed → wake (3)
        self.assertEqual(len(self.injected), 3,
                         "urgent unconsumed mail must re-kick every sweep, not wait "
                         "out REKICK_TTL_SECONDS")

    def test_flag_on_normal_mail_still_debounces(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator")  # normal
        with _enabled():
            self._run(now=1000.0)   # wake (1)
            self._run(now=1001.0)   # nothing newer, NOT urgent → still debounced
        self.assertEqual(len(self.injected), 1,
                         "the bypass must not broaden to normal mail")

    def test_flag_on_bypass_stops_once_drained(self):
        mid = mailbox.send(self.inbox, {"x": 1}, sender="orchestrator", subject="wedge",
                           severity="urgent")
        with _enabled():
            self._run(now=1000.0)   # wake (1)
            mailbox.consume(self.inbox, lambda m: None)  # agent drains it
            self._run(now=1001.0)   # inbox empty → no wake
        self.assertEqual(len(self.injected), 1,
                         "a drained inbox must not keep waking")
        self.assertTrue((self.inbox / "cur" / mid).exists())

    def test_decide_reason_names_the_bypass(self):
        mailbox.send(self.inbox, {"x": 1}, sender="orchestrator", subject="wedge",
                    severity="urgent")
        with _enabled():
            mw._write_marker(self.inbox / mw._MARKER_NAME,
                             mw.peek(self.inbox / "new")[-1], "S1", 1000.0)
            action, names, reason = mw.decide(self.inbox / "new",
                                              self.inbox / mw._MARKER_NAME,
                                              "S1", 1001.0, pane_idle=None)
        self.assertEqual(action, "wake")
        self.assertIn("urgent", reason.lower())


if __name__ == "__main__":
    unittest.main()
