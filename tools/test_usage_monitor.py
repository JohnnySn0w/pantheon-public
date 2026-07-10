"""Unit tests for tools/usage_monitor.py — the api-liveness/token-usage monitor.

Run from repo root:
    python3 -m unittest tools.test_usage_monitor -v

SAFETY (T-1023 + feedback_new_sideeffect_audit_existing_test_mocks + full-suite-leak trap):
usage_monitor has its OWN send path — `send_telegram()` does a direct urllib POST to the
Bot API and does NOT honor PANTHEON_DISABLE_LIVE_TELEGRAM. So the env var alone will NOT
stop a leak. Every test here either patches `tools.usage_monitor.send_telegram` or patches
`tools.usage_monitor.urllib.request.urlopen` — no test path reaches the real network. All
STATE/TELEGRAM_ENV/TRANSCRIPT_ROOT module globals are redirected to a tmp dir in setUp so
real state is never read or written.

Covers:
  - parse_ts, turn_tokens, fmt_tokens, build_alert_text (pure)
  - scan_transcripts windowing + mtime prefilter + bad-row tolerance
  - read_telegram_token, alert_in_cooldown, stamp_cooldown, write_snapshot (file IO -> tmp)
  - send_telegram: token-missing short-circuit (no urlopen), 2xx True, URLError False (all mocked)
  - main: collect-only (no send), dry-run alert (no send), live alert (send mocked), cooldown block
  - propose_threshold: no-state / insufficient / enough
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import tools.usage_monitor as um


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class PureFnTest(unittest.TestCase):
    def test_parse_ts(self):
        self.assertEqual(
            um.parse_ts("2026-06-16T00:00:00+00:00"),
            datetime(2026, 6, 16, tzinfo=timezone.utc),
        )
        # Z suffix is normalized to +00:00
        self.assertEqual(
            um.parse_ts("2026-06-16T00:00:00Z"),
            datetime(2026, 6, 16, tzinfo=timezone.utc),
        )
        self.assertIsNone(um.parse_ts(""))
        self.assertIsNone(um.parse_ts("not-a-timestamp"))

    def test_turn_tokens_sums_all_classes(self):
        usage = {
            "input_tokens": 10,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 30,
            "output_tokens": 40,
        }
        self.assertEqual(um.turn_tokens(usage), 100)

    def test_turn_tokens_tolerates_missing_and_nondict(self):
        self.assertEqual(um.turn_tokens({"input_tokens": 5}), 5)
        self.assertEqual(um.turn_tokens({"input_tokens": None}), 0)
        self.assertEqual(um.turn_tokens(None), 0)
        self.assertEqual(um.turn_tokens("nope"), 0)

    def test_fmt_tokens(self):
        self.assertEqual(um.fmt_tokens(500), "500")
        self.assertEqual(um.fmt_tokens(1500), "1.5k")
        self.assertEqual(um.fmt_tokens(2_500_000), "2.50M")

    def test_build_alert_text_contains_all_windows(self):
        tokens = {1: 100, 5: 5000, 24: 20000, 168: 90000}
        text = um.build_alert_text(tokens, threshold=4000)
        self.assertIn("5h", text)
        self.assertIn("24h", text)
        self.assertIn("7d", text)
        self.assertIn("1h", text)
        # 5000 vs 4000 threshold => +25%
        self.assertIn("+25%", text)


class _StateRedirectMixin:
    """Redirect every module-global path to a per-test tmp dir; never touch real state."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, str(self.tmp), True)
        self._orig = {
            "STATE_DIR": um.STATE_DIR,
            "STATE_FILE": um.STATE_FILE,
            "ALERT_COOLDOWN_FILE": um.ALERT_COOLDOWN_FILE,
            "TRANSCRIPT_ROOT": um.TRANSCRIPT_ROOT,
            "TELEGRAM_ENV": um.TELEGRAM_ENV,
        }
        um.STATE_DIR = self.tmp / "state"
        um.STATE_FILE = um.STATE_DIR / "usage.jsonl"
        um.ALERT_COOLDOWN_FILE = um.STATE_DIR / "alert_cooldown"
        um.TRANSCRIPT_ROOT = self.tmp / "projects"
        um.TELEGRAM_ENV = self.tmp / "telegram.env"
        um.TRANSCRIPT_ROOT.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(um, k, v)

    def _write_transcript(self, name: str, records: list[dict]):
        path = um.TRANSCRIPT_ROOT / name
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        return path

    def _assistant_rec(self, toks: int, when: datetime) -> dict:
        return {
            "type": "assistant",
            "timestamp": _iso(when),
            "message": {"usage": {"input_tokens": toks}},
        }


class ScanTranscriptsTest(_StateRedirectMixin, unittest.TestCase):
    def test_counts_recent_assistant_usage_in_all_windows(self):
        now = um.utcnow()
        self._write_transcript("s1.jsonl", [self._assistant_rec(1000, now - timedelta(minutes=10))])
        tokens, sessions, scanned = um.scan_transcripts(now)
        self.assertEqual(scanned, 1)
        for h in um.WINDOWS_HOURS:
            self.assertEqual(tokens[h], 1000)
            self.assertEqual(sessions[h], {"s1"})

    def test_per_record_window_cutoff(self):
        now = um.utcnow()
        # Fresh file mtime (so it's scanned), but the row is 10h old -> in 24h/7d, not 1h/5h.
        self._write_transcript("s2.jsonl", [self._assistant_rec(700, now - timedelta(hours=10))])
        tokens, sessions, _ = um.scan_transcripts(now)
        self.assertEqual(tokens[1], 0)
        self.assertEqual(tokens[5], 0)
        self.assertEqual(tokens[24], 700)
        self.assertEqual(tokens[168], 700)

    def test_ignores_non_assistant_no_usage_and_bad_json(self):
        now = um.utcnow()
        recs = [
            {"type": "user", "timestamp": _iso(now), "message": {"usage": {"input_tokens": 999}}},
            {"type": "assistant", "timestamp": _iso(now), "message": {}},  # no usage
            self._assistant_rec(50, now),  # the only countable row
        ]
        path = um.TRANSCRIPT_ROOT / "s3.jsonl"
        # Append a malformed line that must be tolerated.
        path.write_text("\n".join(json.dumps(r) for r in recs) + "\n{ broken json\n", encoding="utf-8")
        tokens, sessions, scanned = um.scan_transcripts(now)
        self.assertEqual(tokens[1], 50)
        self.assertEqual(sessions[1], {"s3"})

    def test_missing_transcript_root_is_safe(self):
        um.TRANSCRIPT_ROOT = self.tmp / "does-not-exist"
        tokens, sessions, scanned = um.scan_transcripts(um.utcnow())
        self.assertEqual(scanned, 0)
        self.assertEqual(tokens[5], 0)


class FileIOTest(_StateRedirectMixin, unittest.TestCase):
    def test_read_telegram_token(self):
        self.assertIsNone(um.read_telegram_token())  # no file
        um.TELEGRAM_ENV.write_text("OTHER=x\nTELEGRAM_BOT_TOKEN=abc123\n", encoding="utf-8")
        self.assertEqual(um.read_telegram_token(), "abc123")
        um.TELEGRAM_ENV.write_text("NO_TOKEN_HERE=1\n", encoding="utf-8")
        self.assertIsNone(um.read_telegram_token())

    def test_cooldown_semantics(self):
        now = um.utcnow()
        self.assertFalse(um.alert_in_cooldown(now))  # no file
        um.stamp_cooldown(now)
        self.assertTrue(um.alert_in_cooldown(now))  # just stamped
        # Old stamp -> not in cooldown
        um.ALERT_COOLDOWN_FILE.write_text(_iso(now - timedelta(hours=um.ALERT_COOLDOWN_HOURS + 1)))
        self.assertFalse(um.alert_in_cooldown(now))
        # Malformed stamp -> treated as not-in-cooldown (fails open)
        um.ALERT_COOLDOWN_FILE.write_text("garbage")
        self.assertFalse(um.alert_in_cooldown(now))

    def test_write_snapshot_appends_jsonl(self):
        snap = um.Snapshot(ts=_iso(um.utcnow()), tokens_by_window={"5h": 1}, sessions_by_window={"5h": 1}, transcripts_scanned=1)
        um.write_snapshot(snap)
        um.write_snapshot(snap)
        lines = um.STATE_FILE.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["tokens_by_window"]["5h"], 1)


class SendTelegramTest(_StateRedirectMixin, unittest.TestCase):
    def test_no_token_short_circuits_without_network(self):
        # No TELEGRAM_ENV file -> read_telegram_token None -> returns False, urlopen NEVER called.
        with patch("tools.usage_monitor.urllib.request.urlopen") as urlopen:
            self.assertFalse(um.send_telegram("hi"))
            urlopen.assert_not_called()

    def test_2xx_returns_true(self):
        um.TELEGRAM_ENV.write_text("TELEGRAM_BOT_TOKEN=tok\n", encoding="utf-8")
        resp = MagicMock()
        resp.status = 200
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: False
        with patch("tools.usage_monitor.urllib.request.urlopen", return_value=resp):
            self.assertTrue(um.send_telegram("hi"))

    def test_urlerror_returns_false(self):
        um.TELEGRAM_ENV.write_text("TELEGRAM_BOT_TOKEN=tok\n", encoding="utf-8")
        with patch("tools.usage_monitor.urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
            self.assertFalse(um.send_telegram("hi"))


class MainTest(_StateRedirectMixin, unittest.TestCase):
    """main() alert paths — send_telegram is ALWAYS patched; the real network is never reached."""

    def _run(self, argv):
        buf = io.StringIO()
        with patch.object(sys, "argv", argv), redirect_stdout(buf):
            rc = um.main()
        return rc, buf.getvalue()

    def test_collect_only_never_sends(self):
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(5000, now)])
        with patch("tools.usage_monitor.send_telegram") as send:
            rc, _ = self._run(["usage_monitor"])  # no --alert-tokens
            self.assertEqual(rc, 0)
            send.assert_not_called()
        # snapshot was still written
        self.assertTrue(um.STATE_FILE.exists())

    def test_dry_run_alert_does_not_send(self):
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(5000, now)])
        with patch("tools.usage_monitor.send_telegram") as send:
            # over threshold, but no --send-telegram => dry-run, no send
            rc, _ = self._run(["usage_monitor", "--alert-tokens", "100"])
            self.assertEqual(rc, 0)
            send.assert_not_called()

    def test_live_alert_sends_once_then_cooldown_blocks(self):
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(5000, now)])
        with patch("tools.usage_monitor.send_telegram", return_value=True) as send:
            rc1, _ = self._run(["usage_monitor", "--alert-tokens", "100", "--send-telegram"])
            self.assertEqual(rc1, 0)
            self.assertEqual(send.call_count, 1)
            self.assertTrue(um.ALERT_COOLDOWN_FILE.exists())  # cooldown stamped
            # Second run within cooldown -> blocked, no second send
            rc2, _ = self._run(["usage_monitor", "--alert-tokens", "100", "--send-telegram"])
            self.assertEqual(rc2, 0)
            self.assertEqual(send.call_count, 1)

    def test_live_alert_send_failure_returns_1_and_no_cooldown(self):
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(5000, now)])
        with patch("tools.usage_monitor.send_telegram", return_value=False) as send:
            rc, _ = self._run(["usage_monitor", "--alert-tokens", "100", "--send-telegram"])
            self.assertEqual(rc, 1)
            send.assert_called_once()
            self.assertFalse(um.ALERT_COOLDOWN_FILE.exists())  # no cooldown stamp on failure

    def test_below_threshold_no_send(self):
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(50, now)])
        with patch("tools.usage_monitor.send_telegram") as send:
            rc, _ = self._run(["usage_monitor", "--alert-tokens", "100000", "--send-telegram"])
            self.assertEqual(rc, 0)
            send.assert_not_called()


class ProposeThresholdTest(_StateRedirectMixin, unittest.TestCase):
    def _run(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = um.propose_threshold()
        return rc, buf.getvalue()

    def test_no_state_returns_1(self):
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("No state", out)

    def test_insufficient_samples_returns_1(self):
        um.STATE_DIR.mkdir(parents=True, exist_ok=True)
        um.STATE_FILE.write_text("\n".join(json.dumps({"tokens_by_window": {"5h": 10}}) for _ in range(5)) + "\n", encoding="utf-8")
        rc, out = self._run()
        self.assertEqual(rc, 1)
        self.assertIn("Keep collecting", out)

    def test_enough_samples_prints_percentiles(self):
        um.STATE_DIR.mkdir(parents=True, exist_ok=True)
        rows = [json.dumps({"tokens_by_window": {"5h": v}}) for v in range(1, 21)]  # 20 nonzero
        um.STATE_FILE.write_text("\n".join(rows) + "\n", encoding="utf-8")
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("Suggested --alert-tokens", out)


if __name__ == "__main__":
    unittest.main()
