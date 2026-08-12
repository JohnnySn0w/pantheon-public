import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools import mailbox
from tools.harness_adapter.delivery import inspect_delivery, record_submitted
from tools.harness_adapter.quota import (
    FailoverPolicy,
    PrimaryHealthProbe,
    QuotaSnapshot,
    QuotaWindow,
    decide_failover,
    decide_fallback_recovery,
)
from tools.harness_adapter.types import DeliveryReceipt, HarnessEvent


def _event(kind: str = "turn_started") -> HarnessEvent:
    return HarnessEvent(
        harness="codex",
        kind=kind,  # type: ignore[arg-type]
        session_id="sess-1",
        message=None,
        usage=None,
        raw={"provider_event": kind},
    )


def _snapshot(
    remaining: float,
    *,
    freshness: str = "fresh",
    reset_delta: timedelta = timedelta(hours=1),
    window_name: str = "weekly",
) -> QuotaSnapshot:
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    return QuotaSnapshot(
        profile="claude/interactive-subscription",
        windows=(
            QuotaWindow(
                name=window_name,
                used_percent=100.0 - remaining,
                remaining_percent=remaining,
                resets_at=now + reset_delta,
            ),
        ),
        observed_at=now,
        freshness=freshness,  # type: ignore[arg-type]
        evidence={"source": "fixture"},
    )


class QuotaDecisionTest(unittest.TestCase):
    def test_fresh_weekly_remaining_at_five_percent_selects_planned_failover(self):
        self.assertEqual(
            decide_failover(_snapshot(5.0), FailoverPolicy(weekly_remaining_percent=5.0)),
            "planned-failover",
        )

    def test_fresh_weekly_remaining_above_five_percent_does_not_hesitate(self):
        policy = FailoverPolicy(weekly_remaining_percent=5.0)
        self.assertEqual(decide_failover(_snapshot(5.1), policy), "stay")

        with self.assertRaises(TypeError):
            decide_failover(_snapshot(5.1), policy, prompt="will this fit?")  # type: ignore[call-arg]

    def test_stale_or_missing_quota_is_unknown_not_zero(self):
        self.assertEqual(decide_failover(_snapshot(0.0, freshness="stale"), FailoverPolicy()), "unknown")
        missing = QuotaSnapshot(
            profile="codex/interactive-chatgpt",
            windows=(),
            observed_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            freshness="fresh",
        )
        self.assertEqual(decide_failover(missing, FailoverPolicy()), "unknown")
        non_weekly = _snapshot(0.0, window_name="daily")
        self.assertEqual(decide_failover(non_weekly, FailoverPolicy()), "unknown")

    def test_reset_hysteresis_prevents_profile_flap_near_threshold(self):
        now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
        healthy_probe = PrimaryHealthProbe(
            healthy=True,
            observed_at=now,
            freshness="fresh",
            evidence={"probe": "ok"},
        )
        before_reset = _snapshot(9.0, reset_delta=timedelta(minutes=5))
        self.assertEqual(
            decide_fallback_recovery(before_reset, healthy_probe, now=now),
            "stay-on-fallback",
        )

        after_reset = _snapshot(90.0, reset_delta=timedelta(minutes=-1))
        self.assertEqual(
            decide_fallback_recovery(after_reset, healthy_probe, now=now),
            "return-to-primary",
        )

        pre_reset_probe = PrimaryHealthProbe(
            healthy=True,
            observed_at=now - timedelta(minutes=2),
            freshness="fresh",
            evidence={"probe": "healthy-but-observed-before-reset"},
        )
        self.assertEqual(
            decide_fallback_recovery(after_reset, pre_reset_probe, now=now),
            "stay-on-fallback",
        )

        stale_probe = PrimaryHealthProbe(healthy=True, observed_at=now, freshness="stale")
        self.assertEqual(
            decide_fallback_recovery(after_reset, stale_probe, now=now),
            "stay-on-fallback",
        )


class DeliveryReceiptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = Path(self.tmp.name) / "inbox"
        mailbox.provision(self.inbox)

    def tearDown(self):
        self.tmp.cleanup()

    def test_delivery_is_queued_before_structural_turn_start(self):
        receipt = record_submitted(
            self.inbox,
            message_id="m1",
            correlation_id="c1",
            event=_event("progress"),
        )
        self.assertEqual(receipt.state, "durably_queued")
        self.assertFalse((self.inbox / ".delivery" / "m1.submitted.json").exists())

    def test_turn_started_records_submitted_but_not_acked(self):
        receipt = record_submitted(
            self.inbox,
            message_id="m1",
            correlation_id="c1",
            event=_event(),
        )
        self.assertEqual(receipt.state, "submitted")
        sidecar = self.inbox / ".delivery" / "m1.submitted.json"
        self.assertTrue(sidecar.exists())
        payload = json.loads(sidecar.read_text())
        self.assertEqual(payload["state"], "submitted")
        self.assertNotIn("raw", payload)
        self.assertEqual(inspect_delivery(receipt).state, "submitted")

    def test_ack_requires_source_message_in_cur(self):
        receipt = record_submitted(
            self.inbox,
            message_id="m1",
            correlation_id="c1",
            event=_event(),
        )
        (self.inbox / "cur" / "m1").write_text("{}", encoding="utf-8")
        inspected = inspect_delivery(receipt)
        self.assertEqual(inspected.state, "acked")
        self.assertEqual(inspected.retryable, False)

    def test_ack_log_without_cur_does_not_ack(self):
        receipt = record_submitted(
            self.inbox,
            message_id="m1",
            correlation_id="c1",
            event=_event(),
        )
        (self.inbox / ".acks.jsonl").write_text(
            json.dumps({"id": "m1", "label": "bridge", "pid": 123, "ts_ns": 456}) + "\n",
            encoding="utf-8",
        )
        inspected = inspect_delivery(receipt)
        self.assertEqual(inspected.state, "submitted")
        self.assertEqual(inspected.evidence["ack_log_seen"], "true")

    def test_result_reply_does_not_imply_ack(self):
        receipt = DeliveryReceipt(
            harness="codex",
            correlation_id="c1",
            state="submitted",
            mechanism="maildir-sidecar",
            retryable=True,
            evidence={
                "message_id": "m1",
                "inbox": str(self.inbox),
                "result_reply": "completed",
            },
        )
        self.assertEqual(inspect_delivery(receipt).state, "submitted")

    def test_callback_failure_leaves_source_unacked_for_retry(self):
        message_id = mailbox.send(self.inbox, {"task": "run"}, sender="orchestrator")

        def handler(_message):
            record_submitted(
                self.inbox,
                message_id=message_id,
                correlation_id="c1",
                event=_event(),
            )
            raise RuntimeError("callback failed")

        result = mailbox.consume(self.inbox, handler)
        self.assertEqual(result["processed"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertTrue((self.inbox / "new" / message_id).exists())
        self.assertFalse((self.inbox / "cur" / message_id).exists())

    def test_duplicate_delivery_reuses_correlation_and_receipt_state(self):
        first = record_submitted(
            self.inbox,
            message_id="m1",
            correlation_id="c1",
            event=_event(),
        )
        second = record_submitted(
            self.inbox,
            message_id="m1",
            correlation_id="c1",
            event=_event(),
        )
        self.assertEqual(first.state, "submitted")
        self.assertEqual(second.state, "submitted")
        self.assertEqual(second.correlation_id, "c1")
        self.assertEqual(first.evidence["sidecar"], second.evidence["sidecar"])

    def test_duplicate_delivery_rejects_conflicting_correlation(self):
        record_submitted(
            self.inbox,
            message_id="m1",
            correlation_id="c1",
            event=_event(),
        )
        receipt = record_submitted(
            self.inbox,
            message_id="m1",
            correlation_id="c2",
            event=_event(),
        )
        self.assertEqual(receipt.state, "rejected")


if __name__ == "__main__":
    unittest.main()
