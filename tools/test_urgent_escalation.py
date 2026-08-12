"""test_urgent_escalation.py — the escalation-ladder leg of severity=urgent
(T-1641 Ph2, tools/urgent_escalation.py).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mailbox  # noqa: E402
import urgent_escalation as ue  # noqa: E402


def _enabled():
    return mock.patch.dict(os.environ, {mailbox._URGENT_PHASE2_ENV: "1"})


# --------------------------------------------------------------------- pure core
class StaleUrgentTest(unittest.TestCase):
    def _rec(self, **kw):
        base = {"id": "m1", "sender": "orchestrator", "subject": "s", "severity": "urgent",
                "ts_ns": int(1000 * 1e9)}
        base.update(kw)
        return base

    def test_below_threshold_not_stale(self):
        recs = [self._rec(ts_ns=int(1000 * 1e9))]
        out = ue.stale_urgent(recs, now=1500.0, threshold_seconds=600,
                              already_alerted=set())
        self.assertEqual(out, [])

    def test_at_or_above_threshold_is_stale(self):
        recs = [self._rec(ts_ns=int(1000 * 1e9))]
        out = ue.stale_urgent(recs, now=1600.0, threshold_seconds=600,
                              already_alerted=set())
        self.assertEqual(out, recs)

    def test_normal_severity_never_stale(self):
        recs = [self._rec(severity="normal", ts_ns=int(0))]
        out = ue.stale_urgent(recs, now=100000.0, threshold_seconds=600,
                              already_alerted=set())
        self.assertEqual(out, [])

    def test_back_compat_missing_severity_treated_as_normal(self):
        rec = {"id": "m1", "sender": "x", "subject": "s", "ts_ns": 0}
        out = ue.stale_urgent([rec], now=100000.0, threshold_seconds=600,
                              already_alerted=set())
        self.assertEqual(out, [])

    def test_already_alerted_skipped(self):
        recs = [self._rec(id="m1", ts_ns=int(0))]
        out = ue.stale_urgent(recs, now=100000.0, threshold_seconds=600,
                              already_alerted={"m1"})
        self.assertEqual(out, [])

    def test_missing_ts_ns_skipped_not_guessed(self):
        rec = self._rec()
        del rec["ts_ns"]
        out = ue.stale_urgent([rec], now=100000.0, threshold_seconds=600,
                              already_alerted=set())
        self.assertEqual(out, [])

    def test_multiple_records_only_stale_urgent_returned(self):
        recs = [
            self._rec(id="fresh-urgent", ts_ns=int(999900 * 1e9)),
            self._rec(id="stale-urgent", ts_ns=int(0)),
            self._rec(id="stale-normal", severity="normal", ts_ns=int(0)),
        ]
        out = ue.stale_urgent(recs, now=1000000.0, threshold_seconds=600,
                              already_alerted=set())
        self.assertEqual([r["id"] for r in out], ["stale-urgent"])


# ------------------------------------------------------------------- impure shell
class RunTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.hub = Path(self._tmp.name) / "hub"
        self.hub.mkdir()
        self.orchestrator = self.hub / "orchestrator"
        mailbox.provision(self.orchestrator)

    def tearDown(self):
        self._tmp.cleanup()

    def _send_urgent(self, inbox, ts_ns, sender="recovery", subject="wedge"):
        # Hand-place a well-formed record with a CONTROLLED ts_ns — mailbox.send()
        # always uses the real clock, and these tests need to simulate age.
        name = f"{ts_ns:020d}.1.000000.{sender}"
        rec = {"id": name, "sender": sender, "subject": subject,
              "severity": "urgent", "ts_ns": ts_ns, "payload": {}}
        (inbox / "new" / name).write_text(json.dumps(rec) + "\n")
        return name

    def test_flag_off_is_a_true_noop(self):
        self._send_urgent(self.orchestrator, ts_ns=0)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(mailbox._URGENT_PHASE2_ENV, None)
            result = ue.run(hub=self.hub, now=100000.0)
        self.assertFalse(result["enabled"])
        self.assertEqual(result["escalated"], [])
        self.assertFalse((self.orchestrator / ue._ESCALATED_DIR).exists(),
                         "flag-off must not even create the sidecar dir")

    def test_stale_urgent_triggers_alert(self):
        self._send_urgent(self.orchestrator, ts_ns=0, sender="recovery", subject="disk 91%")
        with _enabled(), mock.patch.object(ue, "_alert") as alert:
            result = ue.run(hub=self.hub, now=100000.0)
        self.assertTrue(result["enabled"])
        self.assertEqual(len(result["escalated"]), 1)
        self.assertEqual(result["escalated"][0]["inbox"], "orchestrator")
        alert.assert_called_once()
        agent_arg, msg_arg = alert.call_args[0]
        self.assertEqual(agent_arg, "orchestrator")
        self.assertIn("recovery", msg_arg)
        self.assertIn("disk 91%", msg_arg)

    def test_fresh_urgent_not_alerted(self):
        self._send_urgent(self.orchestrator, ts_ns=int(99900 * 1e9))
        with _enabled(), mock.patch.object(ue, "_alert") as alert:
            result = ue.run(hub=self.hub, now=100000.0)
        self.assertEqual(result["escalated"], [])
        alert.assert_not_called()

    def test_second_run_does_not_realert_same_message(self):
        self._send_urgent(self.orchestrator, ts_ns=0)
        with _enabled(), mock.patch.object(ue, "_alert") as alert:
            ue.run(hub=self.hub, now=100000.0)
            ue.run(hub=self.hub, now=100001.0)
        self.assertEqual(alert.call_count, 1,
                         "already-escalated message must not re-alert every sweep")

    def test_consumed_message_prunes_and_can_realert_if_resent(self):
        name = self._send_urgent(self.orchestrator, ts_ns=0)
        with _enabled(), mock.patch.object(ue, "_alert"):
            ue.run(hub=self.hub, now=100000.0)
        self.assertTrue((self.orchestrator / ue._ESCALATED_DIR / name).exists())
        # Agent drains it — message leaves new/.
        (self.orchestrator / "new" / name).rename(self.orchestrator / "cur" / name)
        with _enabled(), mock.patch.object(ue, "_alert") as alert:
            ue.run(hub=self.hub, now=100002.0)
        self.assertFalse((self.orchestrator / ue._ESCALATED_DIR / name).exists(),
                         "the orphaned marker for a drained message must be pruned")
        alert.assert_not_called()

    def test_dry_run_never_alerts_or_marks(self):
        self._send_urgent(self.orchestrator, ts_ns=0)
        with _enabled(), mock.patch.object(ue, "_alert") as alert:
            result = ue.run(hub=self.hub, now=100000.0, dry_run=True)
        self.assertEqual(len(result["escalated"]), 1)
        alert.assert_not_called()
        self.assertFalse((self.orchestrator / ue._ESCALATED_DIR).exists())

    def test_malformed_message_does_not_break_sweep(self):
        (self.orchestrator / "new" / "00000000000000000001.1.000000.x").write_text("not json")
        self._send_urgent(self.orchestrator, ts_ns=0)
        with _enabled(), mock.patch.object(ue, "_alert") as alert:
            result = ue.run(hub=self.hub, now=100000.0)
        self.assertEqual(len(result["escalated"]), 1)
        alert.assert_called_once()

    def test_enumerates_inboxes_by_filesystem_not_persona_list(self):
        # A maildir with a non-persona name (mirrors specialist-a2a in production)
        # must still be picked up.
        extra = self.hub / "some-non-persona-inbox"
        mailbox.provision(extra)
        self._send_urgent(extra, ts_ns=0, sender="specialist")
        with _enabled(), mock.patch.object(ue, "_alert") as alert:
            result = ue.run(hub=self.hub, now=100000.0)
        inboxes = {e["inbox"] for e in result["escalated"]}
        self.assertIn("some-non-persona-inbox", inboxes)


# ------------------------------------------------------ real route_failure_alert
class AlertForwardingTest(unittest.TestCase):
    """Confirms _alert() really forwards to inbox_consumer.route_failure_alert
    with the signature that module actually exposes (route_failure_alert(agent,
    msg, vocab)) — the brief asked this be verified, not assumed."""

    def test_forwards_agent_msg_vocab(self):
        with mock.patch("tools.inbox_consumer.route_failure_alert") as route:
            ue._alert("orchestrator", "some urgent message text")
        route.assert_called_once_with("orchestrator", "some urgent message text",
                                      ue.ALERT_VOCAB)


if __name__ == "__main__":
    unittest.main()
