"""test_ancestor_consume.py — the-ancestor mailbox→veneration dispatch (T-0765 glue).

Tests the handler's dispatch + write-back logic with injected resurrect/send seams — no
live mailbox, no resume. Covers: a good resume_request resurrects + delivers the harvested
handoff to the requester; a failed resume sends a failure notice; non-resume + malformed
messages are ignored without dispatching.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ancestor_consume as ac  # noqa: E402
import veneration as ven  # noqa: E402


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.staging = self._tmp.name
        self.sent = []  # (inbox, payload, sender)

    def tearDown(self):
        self._tmp.cleanup()

    def _send(self, inbox, payload, sender, **kw):
        self.sent.append((inbox, payload, sender))
        return "msgid"

    def _handler(self, resurrect_fn):
        return ac.make_handler(resurrect_fn=resurrect_fn, send_fn=self._send,
                               staging_dir=self.staging)

    def _msg(self, payload):
        return {"id": "m1", "sender": "recovery", "payload": payload}

    # ----------------------------------------------------------------- success
    def test_successful_resume_delivers_handoff(self):
        def fake_resurrect(agent, dead_jsonl, target):
            Path(target).write_text("# Resurrected Handoff\nnext: finish T-0772")
            return ven.Result("ok", "harvested")
        h = self._handler(fake_resurrect)
        h(self._msg({"type": "resume_request", "agent": "recovery", "dead_jsonl": "/d.jsonl"}))
        self.assertEqual(len(self.sent), 1)
        inbox, payload, sender = self.sent[0]
        self.assertEqual(inbox, "/home/claude-fallback/inbox")  # recovery's default reply inbox
        self.assertEqual(payload["type"], ac.HANDOFF_DELIVERY)
        self.assertIn("Resurrected Handoff", payload["handoff_text"])
        self.assertEqual(sender, "ancestor")

    def test_reply_inbox_override(self):
        def fake(agent, dj, t):
            Path(t).write_text("x"); return ven.Result("ok")
        h = self._handler(fake)
        h(self._msg({"type": "resume_request", "agent": "recovery", "dead_jsonl": "/d.jsonl",
                     "reply_inbox": "/custom/inbox"}))
        self.assertEqual(self.sent[0][0], "/custom/inbox")

    # ----------------------------------------------------------------- failure
    def test_failed_resume_sends_failure_notice(self):
        h = self._handler(lambda a, d, t: ven.Result("failed", "resumed session not ready"))
        h(self._msg({"type": "resume_request", "agent": "specialist", "dead_jsonl": "/d.jsonl"}))
        self.assertEqual(len(self.sent), 1)
        inbox, payload, _ = self.sent[0]
        self.assertEqual(inbox, "/home/claude-research/inbox")
        self.assertEqual(payload["type"], ac.RESUME_FAILURE)
        self.assertIn("not ready", payload["detail"])

    def test_ok_but_staging_unreadable_is_failure(self):
        # resurrect claims ok but wrote nothing readable → failure notice, not a crash
        h = self._handler(lambda a, d, t: ven.Result("ok", "harvested"))  # never writes file
        h(self._msg({"type": "resume_request", "agent": "recovery", "dead_jsonl": "/d.jsonl"}))
        self.assertEqual(self.sent[0][1]["type"], ac.RESUME_FAILURE)
        self.assertIn("unreadable", self.sent[0][1]["detail"])

    # -------------------------------------------------------------- ignored
    def test_non_resume_message_ignored(self):
        called = []
        h = self._handler(lambda *a: called.append(a) or ven.Result("ok"))
        h(self._msg({"type": "handoff_delivery", "agent": "recovery", "handoff_text": "hi"}))
        self.assertEqual(self.sent, [], "must not dispatch a non-resume message")
        self.assertEqual(called, [], "must not resurrect on a non-resume message")

    def test_malformed_request_sends_failure(self):
        # missing dead_jsonl → don't resurrect, but DON'T silently drop: notify the requester.
        called = []
        h = self._handler(lambda *a: called.append(a) or ven.Result("ok"))
        h(self._msg({"type": "resume_request", "agent": "recovery"}))  # missing dead_jsonl
        self.assertEqual(called, [], "malformed request must not resurrect")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]["type"], ac.RESUME_FAILURE)
        self.assertIn("malformed", self.sent[0][1]["detail"])

    # -------------------------------------------------------------- security
    def test_traversal_agent_rejected_before_side_effects(self):
        # a crafted agent (path traversal) must be rejected at the boundary — never reach
        # resurrect, and never create the staging file outside staging_dir.
        called = []
        h = self._handler(lambda a, d, t: called.append((a, d, t)) or ven.Result("ok"))
        h(self._msg({"type": "resume_request", "agent": "../../etc/evil",
                     "dead_jsonl": "/d.jsonl", "reply_inbox": "/custom/inbox"}))
        self.assertEqual(called, [], "traversal agent must not reach resurrect")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]["type"], ac.RESUME_FAILURE)
        self.assertIn("rejected", self.sent[0][1]["detail"])
        # nothing should have been created up the tree from the staging dir
        self.assertFalse((Path(self.staging).parent / "etc").exists(),
                         "no mkdir/unlink side effect outside staging_dir")

    def test_unsafe_reply_inbox_falls_back_to_trusted(self):
        # an unsafe explicit reply_inbox (traversal) is ignored in favor of the trusted
        # per-agent inbox — send() must never target the attacker-supplied path.
        def fake(agent, dj, t):
            Path(t).write_text("ok handoff"); return ven.Result("ok")
        h = self._handler(fake)
        h(self._msg({"type": "resume_request", "agent": "recovery", "dead_jsonl": "/d.jsonl",
                     "reply_inbox": "/home/the-ancestor/../../etc/cron.d"}))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "/home/claude-fallback/inbox",
                         "unsafe reply_inbox must fall back to the trusted derived inbox")

    def test_no_derivable_reply_inbox_drops(self):
        # unknown agent AND unknown sender → can't notify anyone → drop without resurrecting
        called = []
        h = self._handler(lambda *a: called.append(a) or ven.Result("ok"))
        h({"id": "m1", "sender": "stranger",
           "payload": {"type": "resume_request", "agent": "nobody", "dead_jsonl": "/d.jsonl"}})
        self.assertEqual(self.sent, [], "nothing to send when no reply inbox is derivable")
        self.assertEqual(called, [], "must not resurrect when it can't deliver the result")

    def test_stale_staging_not_delivered_as_fresh(self):
        # leftover content from a prior run must be cleared, never delivered as this harvest
        from pathlib import Path
        Path(self.staging, "recovery-HANDOFF.md").write_text("STALE from a previous run")
        h = self._handler(lambda a, d, t: ven.Result("ok", "harvested"))  # writes nothing new
        h(self._msg({"type": "resume_request", "agent": "recovery", "dead_jsonl": "/d.jsonl"}))
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][1]["type"], ac.RESUME_FAILURE, "no fresh content → failure")
        self.assertNotIn("STALE", str(self.sent[0][1]), "must not leak prior-run content")


if __name__ == "__main__":
    unittest.main()
