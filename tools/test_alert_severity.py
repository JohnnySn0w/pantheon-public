#!/usr/bin/env python3
"""T-2237(a): the alerting senders must classify at send time.

The operator, TG 20:35Z: "age of mailbox mail is objectively a bad measure of harm to
the system ... unless mail marked urgent is undelivered." He is right, and the
field to express it has existed since ORG-026 — `mailbox.send(..., severity=)` —
with ZERO non-test callers. A classification surface nobody classifies on.

These are WIRING tests, deliberately. Asserting that `send()` accepts and records
`severity` proves the library works and says nothing about whether any caller sets
it, which is the entire defect. So each test drives the real alerting function and
asserts the outgoing call carried severity="urgent". T-2220 taught this the hard
way an hour earlier: deleting a call site left every helper-level test green.

Scope note — the ticket's Ask names four senders and one of them is NOT a mailbox
sender at all. channel-liveness alerts route through
`inbox_consumer.route_failure_alert` -> `_send_alert_via_recovery` ->
`_send_to_recovery_pane`, i.e. a tmux INJECTION plus Telegram. Nothing enters a
maildir, so no severity on it could ever be read by a mailbox-staleness detector.
Verified at source rather than assumed from the ticket.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _severity_of(call) -> str | None:
    return call.kwargs.get("severity")


class AlertingSendersClassifyAsUrgent(unittest.TestCase):
    """Each of these is a case where the message sitting undrained IS the harm."""

    def test_unit_failure_alert_is_urgent(self):
        # A cron died and this notice is the only record it happened.
        from tools import unit_failure_alert as ufa
        with patch("tools.mailbox.send") as send:
            ufa._deliver_mailbox("pantheon-x.service", "body", "unit-failure-alert")
        send.assert_called_once()
        self.assertEqual(_severity_of(send.call_args), "urgent")

    def test_dlq_quarantine_alert_is_urgent(self):
        # A message was just dead-lettered; a silent alert makes the loss silent.
        from tools import mailbox
        with patch.object(mailbox, "send") as send:
            mailbox._alert_owner(Path("/tmp/nonexistent-inbox"),
                                 {"id": "x", "sender": "s", "subject": "t",
                                  "attempts": 3, "reason": "r"})
        send.assert_called_once()
        self.assertEqual(_severity_of(send.call_args), "urgent")

    def test_dlq_alert_still_swallows_its_own_failure(self):
        # The urgency must not have made it able to crash the drain.
        from tools import mailbox
        with patch.object(mailbox, "send", side_effect=RuntimeError("boom")):
            mailbox._alert_owner(Path("/tmp/nonexistent-inbox"), {"id": "x"})


class OrdinaryTrafficStaysNormal(unittest.TestCase):
    """the operator's constraint: ordinary agent traffic stays `normal`.

    If everything is urgent then nothing is, and the severity floor in part (b)
    becomes a guard that fires on every message — the mirror of the guard that
    can never fire. Pinning the two highest-volume routine senders.
    """

    def test_ticket_poke_is_not_urgent(self):
        src = (Path(__file__).resolve().parent / "inbox_consumer.py").read_text()
        i = src.index('subject="ticket poke"')
        self.assertNotIn("severity", src[max(0, i - 400):i + 200])

    def test_keep_going_nudge_is_not_urgent(self):
        src = (Path(__file__).resolve().parent / "keep_going.py").read_text()
        i = src.index('subject="keep-going nudge"')
        self.assertNotIn("severity", src[max(0, i - 400):i + 200])


class SeverityIsActuallyRecordedOnTheEnvelope(unittest.TestCase):
    """The known-positive for the tests above.

    Without this, every assertion here could pass against a `send()` that
    silently dropped the kwarg — the callers would be 'classifying' into nothing.
    bugs/ entry (h3): a check needs a known-positive before its result means
    anything.
    """

    def test_urgent_round_trips_to_disk(self):
        import json
        import shutil
        import tempfile
        from tools import mailbox
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        box = Path(tmp) / "box"
        mailbox.provision(box)
        mid = mailbox.send(box, "boom", "unit-failure-alert", subject="x",
                           severity="urgent")
        rec = json.loads((box / "new" / mid).read_text())
        self.assertEqual(rec.get("severity"), "urgent")

    def test_default_is_normal(self):
        import json
        import shutil
        import tempfile
        from tools import mailbox
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        box = Path(tmp) / "box"
        mailbox.provision(box)
        mid = mailbox.send(box, "hi", "someagent", subject="x")
        rec = json.loads((box / "new" / mid).read_text())
        self.assertEqual(rec.get("severity"), "normal")


if __name__ == "__main__":
    unittest.main()
