"""T-2248: the delivered-message staleness hint.

The defect is NOT queue depth — it is that turn-serialised delivery makes
staleness invisible to the RECEIVER. Nothing in a delivered message says "three
later messages from this sender are already queued behind me", so a message and
its own RETRACTION are indistinguishable on arrival. Archivist measured the cost
(2026-07-28): she re-derived work a peer had already withdrawn, and once
corrected a figure the sender had retracted 16 minutes earlier.

Faster delivery was explicitly rejected as the fix — it narrows the window
without closing it — so these tests pin the HINT's content, not any latency
property.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

try:
    from tools.acp_bridge import AcpBridge
except ImportError:  # bare run from inside tools/
    from acp_bridge import AcpBridge


def _mk(i: int, sender: str, *, ts_off: float = 0.0, supersedes: str | None = None) -> dict:
    ts = int((time.time() - ts_off) * 1e9)
    return {"id": f"{ts:020d}.{i}.000000.{sender}", "sender": sender,
            "subject": f"s{i}", "payload": "x", "ts_ns": ts, "supersedes": supersedes}


class TestQueueHint(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inbox = Path(self.tmp.name)
        (self.inbox / "new").mkdir(parents=True)
        # __new__ rather than __init__: the hint needs only .inbox, and building a
        # real bridge would require a live ACP wrapper.
        self.bridge = AcpBridge.__new__(AcpBridge)
        self.bridge.inbox = self.inbox

    def _place(self, *msgs: dict) -> None:
        for m in msgs:
            (self.inbox / "new" / m["id"]).write_text(json.dumps(m), encoding="utf-8")

    def test_empty_queue_yields_no_hint(self) -> None:
        self.assertEqual(self.bridge._queue_hint(_mk(0, "specialist")), "")

    def test_only_the_current_message_queued_yields_no_hint(self) -> None:
        cur = _mk(1, "specialist")
        self._place(cur)
        self.assertEqual(self.bridge._queue_hint(cur), "")

    def test_names_the_same_sender_backlog(self) -> None:
        """The load-bearing line: a LATER message from the SAME sender is the one
        that can invalidate what the receiver is reading right now."""
        cur = _mk(1, "specialist", ts_off=3600)
        self._place(cur, _mk(2, "specialist"), _mk(3, "specialist"), _mk(9, "orchestrator"))
        hint = self.bridge._queue_hint(cur)
        self.assertIn("2 later message(s) from specialist", hint)
        self.assertIn("specialist x2", hint)
        self.assertIn("orchestrator x1", hint)
        self.assertIn("3 queued behind this", hint)  # current message excluded
        self.assertIn("oldest queued", hint)

    def test_supersession_is_the_headline_when_present(self) -> None:
        """T-2165 landed supersession; if the retraction is ALREADY sitting in the
        queue, the receiver must be told before it acts, not after."""
        cur = _mk(1, "specialist", ts_off=600)
        self._place(cur, _mk(2, "specialist", supersedes=cur["id"]))
        hint = self.bridge._queue_hint(cur)
        self.assertIn("ALREADY BEEN SUPERSEDED", hint)

    def test_peer_set_sender_names_are_neutralized(self) -> None:
        """T-4382: queued senders (and a superseding id) are peer-set free text in
        the prompt; an '@<path>' among them must not reach it bare."""
        # The id embeds the sender (a filename), so the sender carries no '/'.
        cur = _mk(1, "@passwd", ts_off=600)
        self._place(cur, _mk(2, "@passwd"), _mk(3, "orchestrator"))
        hint = self.bridge._queue_hint(cur)
        self.assertIn("1 later message(s) from \\@passwd are", hint)
        self.assertIn("\\@passwd x1", hint)
        self.assertNotIn(" @passwd", hint)
        sup = dict(_mk(4, "specialist"), id="@/etc/passwd", supersedes=cur["id"])
        (self.inbox / "new" / "m-sup").write_text(json.dumps(sup), encoding="utf-8")
        self.assertIn("(\\@/etc/passwd)", self.bridge._queue_hint(cur))

    def test_hint_never_raises_on_a_broken_inbox(self) -> None:
        """A hint is an optimisation; failing to compute one must never cost a
        delivery. Both a missing inbox and an unreadable queued file stay quiet."""
        self.bridge.inbox = Path("/nonexistent/inbox")
        self.assertEqual(self.bridge._queue_hint(_mk(1, "x")), "")

        self.bridge.inbox = self.inbox
        (self.inbox / "new" / "garbage").write_text("{not json", encoding="utf-8")
        self.assertIsInstance(self.bridge._queue_hint(_mk(1, "specialist")), str)

    def test_oversized_queue_still_reports_a_count(self) -> None:
        """Past the scan cap the breakdown is dropped, but the receiver is still
        warned — degrading to silence on a huge backlog would be exactly backwards."""
        cur = _mk(1, "specialist")
        self._place(cur, *[_mk(i, "specialist") for i in range(2, 2 + AcpBridge._HINT_SCAN_CAP + 2)])
        hint = self.bridge._queue_hint(cur)
        self.assertIn("queued behind this one", hint)
        self.assertIn("drain before acting", hint)

    def test_handle_message_actually_appends_the_hint(self) -> None:
        """WIRING, not the helper. A test that builds the hint by hand cannot
        detect the hint never being ATTACHED to the prompt — the exact failure
        shape logged repeatedly on 2026-07-28. Assert on the source of the call
        site so deleting it fails here."""
        import inspect
        self.assertIn("_queue_hint(msg)", inspect.getsource(AcpBridge.handle_message))


if __name__ == "__main__":
    unittest.main()
