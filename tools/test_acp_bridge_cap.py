#!/usr/bin/env python3
"""Unittest coverage for the T-2038 usage-cap handling in tools/acp_bridge.py.

The defect (T-2038, specialist 2026-07-26): during a subscription usage-cap window
the wrapper returns `-32603 "Internal error: You've hit your limit · resets 5pm
(UTC)"` for EVERY session/prompt. The old bridge treated that per-message-poison:
innocent keep_going nudges were quarantined after 3 retries, and each quarantine
spawned a mailbox-dlq alert which — being quarantine-exempt — then retried FOREVER
against the still-capped wrapper (~50k iterations observed in the specialist log).

The fix: a cap error is ENVIRONMENTAL, not poison. handle_message classifies it
(AcpCapError, mailbox_transient=True) so mailbox._drain leaves the message in new/
WITHOUT counting it toward DLQ, and trips a circuit breaker so run_once() defers
drains for cap_cooldown_secs instead of re-hammering the capped seat.

unittest (NOT pytest) on purpose — pytest is not installed here; mirrors
tools/test_acp_bridge_fireforget.py.
"""
import sys
import time
import unittest
from datetime import datetime, timezone
from unittest import mock
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import acp_bridge
from tools import mailbox


_INIT_OK = {"protocolVersion": 1}
_NEW_SESSION_OK = {"sessionId": "sess-abc"}
# The exact wire error observed on the specialist bridge during the incident.
_CAP_MSG = "Internal error: You've hit your limit · resets 5pm (UTC)"


def _base_responses(**overrides):
    resp = {"initialize": _INIT_OK, "session/new": _NEW_SESSION_OK}
    resp.update(overrides)
    return resp


class CapDetectionTest(unittest.TestCase):
    def test_is_cap_error_matches_observed_wire_message(self):
        self.assertTrue(acp_bridge._is_cap_error(acp_bridge.AcpError(-32603, _CAP_MSG)))

    def test_is_cap_error_matches_variants(self):
        for msg in ("usage limit reached", "You have hit a rate limit",
                    "quota resets at midnight"):
            self.assertTrue(acp_bridge._is_cap_error(acp_bridge.AcpError(-32603, msg)),
                            f"should classify as cap: {msg!r}")

    def test_is_cap_error_rejects_non_cap_errors(self):
        self.assertFalse(acp_bridge._is_cap_error(
            acp_bridge.AcpError(-32000, "Authentication required")))
        self.assertFalse(acp_bridge._is_cap_error(
            acp_bridge.AcpError(-32603, "Internal error: something else")))
        self.assertFalse(acp_bridge._is_cap_error(RuntimeError("nope")))

    def test_cap_error_is_transport_error_subclass(self):
        self.assertTrue(issubclass(acp_bridge.AcpCapError, acp_bridge.AcpTransportError))

    def test_cap_error_carries_mailbox_transient(self):
        self.assertTrue(getattr(acp_bridge.AcpCapError("x"), "mailbox_transient", False))


class CapHandlingTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.inbox = self.tmp_path / "inbox"
        mailbox.provision(self.inbox)  # T-2216: consume() no longer auto-provisions

    def tearDown(self):
        self._tmp.cleanup()

    def _make_bridge(self, responses, cap_cooldown_secs=300.0):
        transport = acp_bridge.FakeAcpTransport(responses=responses)
        bridge = acp_bridge.AcpBridge(
            agent="test",
            cwd=str(self.tmp_path),
            inbox=self.inbox,
            transport=transport,
            state_path=self.tmp_path / "state" / "acp-bridge-test.json",
            cap_cooldown_secs=cap_cooldown_secs,
        )
        bridge.transport = transport
        bridge.session_id = "sess-test"
        return bridge, transport

    def _cap_responses(self):
        return _base_responses(**{
            "session/prompt": acp_bridge.AcpError(-32603, _CAP_MSG)})

    # -- core: a capped nudge is NOT counted toward DLQ, ever ------------------
    def test_capped_nudge_never_quarantines(self):
        mid = mailbox.send(self.inbox, "please keep going",
                           sender="keep_going", subject="nudge")
        # Drain far more than dlq_max_attempts (3) times. cap_cooldown_secs=0 so
        # the breaker never defers — every drain actually re-attempts and hits
        # the cap, the worst case for the DLQ counter.
        for _ in range(6):
            bridge, _ = self._make_bridge(self._cap_responses(), cap_cooldown_secs=0.0)
            result = bridge.run_once()
        self.assertEqual(result["quarantined"], [],
                         "a usage-cap must NEVER quarantine (it is environmental, not poison)")
        self.assertTrue(result["errors"], "the failure is still recorded for visibility")
        self.assertTrue((self.inbox / "new" / mid).exists(),
                        "the message waits in new/ to deliver on reset")
        self.assertFalse((self.inbox / "failed" / mid).exists())
        self.assertFalse((self.inbox / ".attempts" / mid).exists(),
                         "a transient failure must not create a DLQ attempt counter")

    # -- the mailbox-dlq alert sender is also protected (the loop's fuel) ------
    def test_capped_dlq_alert_message_stays_uncounted(self):
        mid = mailbox.send(self.inbox, {"id": "x", "attempts": 3},
                           sender="mailbox-dlq", subject="DLQ quarantine: x")
        bridge, _ = self._make_bridge(self._cap_responses(), cap_cooldown_secs=0.0)
        result = bridge.run_once()
        self.assertTrue((self.inbox / "new" / mid).exists())
        self.assertEqual(result["quarantined"], [])

    # -- circuit breaker: after a cap, the next drain is deferred --------------
    def test_breaker_defers_subsequent_drain(self):
        mailbox.send(self.inbox, "keep going", sender="keep_going", subject="nudge")
        bridge, transport = self._make_bridge(self._cap_responses(), cap_cooldown_secs=300.0)
        first = bridge.run_once()
        self.assertTrue(first["errors"], "first drain hits the cap and records it")
        calls_after_first = len(transport.calls)
        second = bridge.run_once()
        self.assertTrue(second.get("skipped"), "breaker must defer the next drain")
        self.assertEqual(second.get("reason"), "session_capped")
        self.assertEqual(len(transport.calls), calls_after_first,
                         "a deferred drain must not touch the wrapper at all")

    # -- breaker recovers once the cooldown lapses ----------------------------
    def test_breaker_recovers_after_cooldown(self):
        mid = mailbox.send(self.inbox, "keep going", sender="keep_going", subject="nudge")
        bridge, transport = self._make_bridge(self._cap_responses(), cap_cooldown_secs=300.0)
        bridge.run_once()  # trips the breaker
        # Simulate cooldown lapse + cap reset: rewind the deadline, swap in an OK.
        bridge._capped_until = 0.0
        transport.responses["session/prompt"] = {"stopReason": "end_turn"}
        result = bridge.run_once()
        self.assertIn(mid, result["processed"], "message delivers once the cap resets")
        self.assertTrue((self.inbox / "cur" / mid).exists())

    # -- breaker stops per-message hammering WITHIN one drain ------------------
    def test_breaker_short_circuits_remaining_messages_in_same_drain(self):
        mailbox.send(self.inbox, "a", sender="keep_going", subject="n1")
        mailbox.send(self.inbox, "b", sender="keep_going", subject="n2")
        mailbox.send(self.inbox, "c", sender="keep_going", subject="n3")
        bridge, transport = self._make_bridge(self._cap_responses(), cap_cooldown_secs=300.0)
        bridge.run_once()
        prompt_calls = [c for c in transport.calls if c[0] == "session/prompt"]
        self.assertEqual(len(prompt_calls), 1,
                         "only the first message should reach the wrapper; the breaker "
                         "short-circuits the rest of the drain")

    # -- regression: a NON-cap AcpError still quarantines (unchanged) ---------
    def test_non_cap_acp_error_still_quarantines(self):
        mid = mailbox.send(self.inbox, "keep going", sender="keep_going", subject="nudge")
        for _ in range(3):
            bridge, _ = self._make_bridge(_base_responses(**{
                "session/prompt": acp_bridge.AcpError(-32000, "Authentication required")}))
            result = bridge.run_once()
        self.assertEqual(len(result["quarantined"]), 1,
                         "a genuine (non-cap) error must still dead-letter after N strikes")
        self.assertTrue((self.inbox / "failed" / mid).exists())


class CapResetParseTest(unittest.TestCase):
    """T-2510: parse the reset clock time the cap message carries so the breaker
    can defer until the cap actually clears instead of re-probing every cooldown
    (each probe appends a banner to the wrapper session -> ~20x context burn)."""

    @staticmethod
    def _now(hour, minute=0):
        # A fixed UTC wall-clock instant (epoch secs) for deterministic deltas.
        return datetime(2026, 8, 3, hour, minute, 0, tzinfo=timezone.utc).timestamp()

    def test_parses_hh_mm_pm_to_future_delta(self):
        # observed at 15:00, "resets 5:10pm" -> 17:10 today = 2h10m + margin.
        delay = acp_bridge._parse_cap_reset_delay(
            "Internal error: You're out of extra usage · resets 5:10pm (UTC)",
            now_wall=self._now(15, 0))
        self.assertIsNotNone(delay)
        expected = (2 * 3600 + 10 * 60) + acp_bridge._CAP_RESET_MARGIN_SECS
        self.assertAlmostEqual(delay, expected, delta=1.0)

    def test_parses_bare_hour(self):
        delay = acp_bridge._parse_cap_reset_delay(
            "You've hit your limit · resets 5pm (UTC)", now_wall=self._now(15, 0))
        self.assertAlmostEqual(delay, 2 * 3600 + acp_bridge._CAP_RESET_MARGIN_SECS,
                               delta=1.0)

    def test_boundary_skew_returns_none(self):
        # seen at 17:05, "resets 5pm" -> 5 min past = clock skew / just cleared
        # -> None, so the caller re-probes on the fixed cooldown SOON rather than
        # over-deferring ~a day on a misread.
        self.assertIsNone(acp_bridge._parse_cap_reset_delay(
            "resets 5pm (UTC)", now_wall=self._now(17, 5)))

    def test_reset_hours_behind_rolls_to_next_occurrence(self):
        # seen at 22:00, "resets 12am" -> midnight is genuinely tomorrow (~2h),
        # not boundary skew -> roll forward, don't fall back to fixed cooldown.
        delay = acp_bridge._parse_cap_reset_delay(
            "resets 12am (UTC)", now_wall=self._now(22, 0))
        self.assertAlmostEqual(delay, 2 * 3600 + acp_bridge._CAP_RESET_MARGIN_SECS,
                               delta=1.0)

    def test_noon_and_midnight_meridiem(self):
        # 12pm == noon (12:00), 12am == midnight (00:00).
        d_noon = acp_bridge._parse_cap_reset_delay("resets 12pm (UTC)",
                                                   now_wall=self._now(10, 0))
        self.assertAlmostEqual(d_noon, 2 * 3600 + acp_bridge._CAP_RESET_MARGIN_SECS,
                               delta=1.0)
        d_mid = acp_bridge._parse_cap_reset_delay("resets 12am (UTC)",
                                                  now_wall=self._now(22, 0))
        self.assertAlmostEqual(d_mid, 2 * 3600 + acp_bridge._CAP_RESET_MARGIN_SECS,
                               delta=1.0)

    def test_no_reset_time_returns_none(self):
        for msg in ("usage limit reached", "You have hit a rate limit", "", None):
            self.assertIsNone(acp_bridge._parse_cap_reset_delay(msg, now_wall=self._now(15)))

    def test_absurd_reset_is_clamped(self):
        # A parse can never exceed _MAX_CAP_DEFER_SECS (bounds a mis-parse).
        delay = acp_bridge._parse_cap_reset_delay("resets 11pm (UTC)",
                                                  now_wall=self._now(1, 0))
        self.assertLessEqual(delay, acp_bridge._MAX_CAP_DEFER_SECS)


class CapBreakerResetDeferTest(unittest.TestCase):
    """The breaker must HONOR the parsed reset time (T-2510), fall back to the
    fixed cooldown without one, and stay OFF when cooldown<=0."""

    def _bridge(self, cap_cooldown_secs=300.0):
        return acp_bridge.AcpBridge(
            agent="t", cwd="/tmp", inbox="/tmp/none",
            wrapper_cmd=["true"], cap_cooldown_secs=cap_cooldown_secs)

    def test_trip_defers_until_reset_not_fixed_cooldown(self):
        b = self._bridge(cap_cooldown_secs=300.0)
        msg = "Internal error: You're out of extra usage · resets 5:10pm (UTC)"
        # T-2623: freeze the wall clock so "5:10pm (UTC)" is deterministically in
        # the future. The trip path calls _parse_cap_reset_delay with the real
        # time.time() (no now_wall injection), so without this the test failed
        # after ~17:10 UTC daily, when 5:10pm rolls past and the parse returns
        # None -> _capped_until falls back to the 300s cooldown -> the delta
        # asserts break. Pinning "now" to 15:00 UTC puts 17:10 ~2h10m ahead
        # (well clear of the 300s cooldown this test distinguishes).
        frozen_now = datetime(2026, 8, 3, 15, 0, 0, tzinfo=timezone.utc).timestamp()
        with mock.patch.object(acp_bridge.time, "time", return_value=frozen_now):
            expected = acp_bridge._parse_cap_reset_delay(msg)  # 15:00 -> 17:10 = 2h10m + margin
            b._trip_cap_breaker(acp_bridge.AcpError(-32603, msg))
            actual = b._capped_until - time.monotonic()
        self.assertAlmostEqual(actual, expected, delta=2.0)
        # And it is emphatically NOT the fixed 300s cooldown (unless reset is ~now).
        if abs(expected - 300.0) > 10:
            self.assertNotAlmostEqual(actual, 300.0, delta=10.0)

    def test_trip_falls_back_to_cooldown_without_reset_time(self):
        b = self._bridge(cap_cooldown_secs=300.0)
        b._trip_cap_breaker(acp_bridge.AcpError(-32603, "usage limit reached"))
        self.assertAlmostEqual(b._capped_until - time.monotonic(), 300.0, delta=2.0)

    def test_cooldown_zero_disables_breaker_even_with_reset_time(self):
        # Regression guard: the reset-parse must not silently re-arm a breaker the
        # caller disabled with cap_cooldown_secs=0 (the existing test contract).
        b = self._bridge(cap_cooldown_secs=0.0)
        b._trip_cap_breaker(acp_bridge.AcpError(-32603, "resets 5pm (UTC)"))
        self.assertLessEqual(b._capped_until - time.monotonic(), 0.01)


if __name__ == "__main__":
    unittest.main()
