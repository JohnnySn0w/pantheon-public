"""Unit tests for tools/geo_hook_health_monitor.py (T-3647).

Run from repo root:
    python3 -m unittest tools.test_geo_hook_health_monitor -v

Fully hermetic: `poll` is exercised against a stubbed urlopen (no real network),
and the `evaluate` hysteresis state machine is driven with an injected fake `send`
(no real mailbox). Nothing here touches pve2, the tailnet, or the mailbox hub.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from tools import geo_hook_health_monitor as ghm


class _FakeResp:
    """Minimal stand-in for the http response context manager urlopen returns."""

    def __init__(self, status: int, body: object):
        self.status = status
        self._body = json.dumps(body).encode() if not isinstance(body, bytes) else body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


@contextmanager
def _urlopen_returning(status: int, body: object):
    with patch.object(ghm.urllib.request, "urlopen", return_value=_FakeResp(status, body)):
        yield


class PollTests(unittest.TestCase):
    def test_ok_when_200_and_status_ok(self):
        with _urlopen_returning(200, {"status": "ok"}):
            self.assertEqual(ghm.poll("http://x/health"), (True, "ok"))

    def test_not_ok_when_body_status_not_ok(self):
        with _urlopen_returning(200, {"status": "degraded"}):
            ok, detail = ghm.poll("http://x/health")
        self.assertFalse(ok)
        self.assertIn("degraded", detail)

    def test_not_ok_on_non_200(self):
        with _urlopen_returning(503, {"status": "ok"}):
            ok, detail = ghm.poll("http://x/health")
        self.assertFalse(ok)
        self.assertIn("503", detail)

    def test_not_ok_on_exception(self):
        with patch.object(ghm.urllib.request, "urlopen", side_effect=OSError("conn refused")):
            ok, detail = ghm.poll("http://x/health")
        self.assertFalse(ok)
        self.assertIn("OSError", detail)


class _Recorder:
    """Injectable fake `send`; records (subject, payload) tuples."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, subject, payload, sender_dir=None):
        self.calls.append((subject, payload))


def _fresh():
    return {"consecutive_failures": 0, "alerting": False, "last_ok": None}


class EvaluateTests(unittest.TestCase):
    def _run_seq(self, results, threshold=3):
        """Feed a sequence of ok bools through evaluate; return (final_state, recorder)."""
        rec = _Recorder()
        state = _fresh()
        for ok in results:
            state = ghm.evaluate(ok, "detail", state, url="http://x/health",
                                 threshold=threshold, send=rec)
        return state, rec

    def test_single_blip_below_threshold_does_not_alert(self):
        state, rec = self._run_seq([False, False], threshold=3)
        self.assertEqual(state["consecutive_failures"], 2)
        self.assertFalse(state["alerting"])
        self.assertEqual(rec.calls, [])

    def test_alert_fires_exactly_at_threshold(self):
        state, rec = self._run_seq([False, False, False], threshold=3)
        self.assertTrue(state["alerting"])
        self.assertEqual(len(rec.calls), 1)
        self.assertIn("UNREACHABLE", rec.calls[0][0])

    def test_no_respam_while_sustained(self):
        state, rec = self._run_seq([False, False, False, False, False], threshold=3)
        self.assertTrue(state["alerting"])
        self.assertEqual(state["consecutive_failures"], 5)
        self.assertEqual(len(rec.calls), 1)  # still exactly one alert

    def test_recovery_after_alert_sends_clear_and_resets(self):
        state, rec = self._run_seq([False, False, False, True], threshold=3)
        self.assertFalse(state["alerting"])
        self.assertEqual(state["consecutive_failures"], 0)
        self.assertIsNotNone(state["last_ok"])
        self.assertEqual(len(rec.calls), 2)
        self.assertIn("RECOVERED", rec.calls[1][0])

    def test_ok_without_prior_alert_is_silent(self):
        state, rec = self._run_seq([True, True], threshold=3)
        self.assertFalse(state["alerting"])
        self.assertEqual(state["consecutive_failures"], 0)
        self.assertEqual(rec.calls, [])

    def test_re_alert_after_recovery(self):
        # fail→alert, recover, fail again → a SECOND alert must fire (not suppressed).
        state, rec = self._run_seq([False, False, False, True, False, False, False], threshold=3)
        self.assertTrue(state["alerting"])
        self.assertEqual(len(rec.calls), 3)  # alert, recovered, alert-again
        self.assertIn("UNREACHABLE", rec.calls[2][0])


class StateRoundTripTests(unittest.TestCase):
    def test_save_then_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sub" / "state.json"
            st = {"consecutive_failures": 4, "alerting": True, "last_ok": "2026-09-12T00:00:00+00:00"}
            ghm._save_state(p, st)
            self.assertEqual(ghm._load_state(p), st)

    def test_load_missing_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                ghm._load_state(Path(d) / "nope.json"),
                {"consecutive_failures": 0, "alerting": False, "last_ok": None},
            )


class OffTailnetT5385Test(unittest.TestCase):
    def _main(self, poll_result, off):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = Path(tmp.name) / "s.json"
        state.write_text(json.dumps({"consecutive_failures": 2, "alerting": False, "last_ok": None}))
        with patch.object(ghm, "poll", return_value=poll_result), \
                patch.object(ghm.tailnet_self, "off_tailnet", return_value=off), \
                patch.object(ghm.tailnet_self, "report_off_tailnet") as report, \
                patch.object(ghm.tailnet_self, "note_reachable") as reachable, \
                patch.object(ghm, "evaluate", side_effect=lambda ok, d, st, **kw: {
                    **st, "consecutive_failures": 0 if ok else st["consecutive_failures"] + 1,
                    "alerting": False}) as evaluate:
            rc = ghm.main(["--state", str(state)])
        return rc, json.loads(state.read_text()), report, reachable, evaluate

    def test_a_failed_poll_while_off_the_tailnet_leaves_the_count_and_sends_nothing(self):
        rc, st, report, _, evaluate = self._main((False, "URLError: timed out"), "NeedsLogin")
        self.assertEqual(rc, 0)
        self.assertEqual(st["consecutive_failures"], 2)  # not advanced to the threshold
        evaluate.assert_not_called()
        self.assertEqual(report.call_args.args[:2], ("geo-hook", "NeedsLogin"))

    def test_a_failed_poll_on_the_tailnet_still_counts(self):
        rc, st, report, _, evaluate = self._main((False, "URLError: timed out"), None)
        self.assertEqual(st["consecutive_failures"], 3)
        report.assert_not_called()
        evaluate.assert_called_once()

    def test_a_good_poll_rearms_the_off_tailnet_alert(self):
        _, _, _, reachable, _ = self._main((True, "ok"), None)
        reachable.assert_called_once()


if __name__ == "__main__":
    unittest.main()
