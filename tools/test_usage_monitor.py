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
  - scan_transcripts windowing + mtime prefilter + bad-row tolerance (incl. cache sums)
  - read_telegram_token, alert_in_cooldown, stamp_cooldown, write_snapshot (file IO -> tmp)
  - send_telegram: token-missing short-circuit (no urlopen), 2xx True, URLError False (all mocked)
  - main: collect-only (no send), dry-run alert (no send), live alert (send mocked), cooldown block
  - propose_threshold: no-state / insufficient / enough (5h, 7d, cache-ratio blocks)
  - T-1875: cache_health_ratio/_alert_needed, weekly_pacing_status/_alert_needed, per-kind
    cooldown isolation, main()'s weekly-pacing + cache-health alert paths, env-var helpers
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
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

    def test_cache_health_ratio(self):
        self.assertIsNone(um.cache_health_ratio(0, 0))  # no cache activity -> no noise
        self.assertEqual(um.cache_health_ratio(0, 100), 0.0)  # pure reuse, no creation
        self.assertEqual(um.cache_health_ratio(100, 0), float("inf"))  # writes, zero reads back
        self.assertEqual(um.cache_health_ratio(50, 200), 0.25)

    def test_cache_health_alert_needed(self):
        self.assertFalse(um.cache_health_alert_needed(None, 0.5))  # no data -> no alert
        self.assertFalse(um.cache_health_alert_needed(0.3, 0.5))  # below threshold
        self.assertTrue(um.cache_health_alert_needed(0.6, 0.5))  # above threshold
        self.assertTrue(um.cache_health_alert_needed(float("inf"), 0.5))  # worst case always fires

    def test_build_cache_health_alert_text_finite_and_inf(self):
        text = um.build_cache_health_alert_text(500, 1000, 0.5, 0.3, "24h")
        self.assertIn("0.50", text)
        self.assertIn("24h", text)
        text_inf = um.build_cache_health_alert_text(500, 0, float("inf"), 0.3, "24h")
        self.assertIn("no cache reads", text_inf)

    def test_weekly_pacing_status_under_over_and_exceeded(self):
        # Plenty of remaining budget, low daily rate -> no alert territory (pct low)
        under = um.weekly_pacing_status(tokens_7d=100, daily_rate=5, quota=1000)
        self.assertIsNone(under["reason"])
        self.assertAlmostEqual(under["pct"], 5 / 900 * 100)

        # Daily rate would eat >20% of what's left
        over = um.weekly_pacing_status(tokens_7d=100, daily_rate=300, quota=1000)
        self.assertIsNone(over["reason"])
        self.assertGreater(over["pct"], 20.0)

        # Already past quota
        exceeded = um.weekly_pacing_status(tokens_7d=1200, daily_rate=50, quota=1000)
        self.assertEqual(exceeded["reason"], "quota_exceeded")
        self.assertIsNone(exceeded["pct"])
        self.assertEqual(exceeded["remaining"], -200)

        # Zero burn -> zero pct even with tight remaining budget
        idle = um.weekly_pacing_status(tokens_7d=999, daily_rate=0, quota=1000)
        self.assertEqual(idle["pct"], 0.0)

    def test_weekly_pacing_alert_needed(self):
        self.assertTrue(um.weekly_pacing_alert_needed({"reason": "quota_exceeded", "pct": None, "remaining": -1}, 20.0))
        self.assertFalse(um.weekly_pacing_alert_needed({"reason": None, "pct": 10.0, "remaining": 500}, 20.0))
        self.assertTrue(um.weekly_pacing_alert_needed({"reason": None, "pct": 30.0, "remaining": 500}, 20.0))

    def test_build_weekly_pacing_alert_text_both_shapes(self):
        exceeded_status = {"reason": "quota_exceeded", "pct": None, "remaining": -200}
        text = um.build_weekly_pacing_alert_text(1200, 50, 1000, exceeded_status)
        self.assertIn("EXCEEDS", text)
        pacing_status = {"reason": None, "pct": 30.0, "remaining": 500}
        text2 = um.build_weekly_pacing_alert_text(500, 150, 1000, pacing_status)
        self.assertIn("30%", text2)

    def test_env_int_and_float_helpers(self):
        with patch.dict(os.environ, {"PANTHEON_TEST_INT": "42"}, clear=False):
            self.assertEqual(um._env_int("PANTHEON_TEST_INT"), 42)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PANTHEON_TEST_INT_MISSING", None)
            self.assertIsNone(um._env_int("PANTHEON_TEST_INT_MISSING"))
        with patch.dict(os.environ, {"PANTHEON_TEST_INT_BAD": "not-a-number"}, clear=False):
            self.assertIsNone(um._env_int("PANTHEON_TEST_INT_BAD"))
        with patch.dict(os.environ, {"PANTHEON_TEST_FLOAT": "1.5"}, clear=False):
            self.assertEqual(um._env_float("PANTHEON_TEST_FLOAT"), 1.5)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PANTHEON_TEST_FLOAT_MISSING", None)
            self.assertEqual(um._env_float("PANTHEON_TEST_FLOAT_MISSING", 9.9), 9.9)


class HourlyAnomalyPureFnTest(unittest.TestCase):
    """T-1897: hourly burn-anomaly detector — pure functions, no I/O."""

    def test_floor_hour_truncates_to_calendar_hour(self):
        dt = datetime(2026, 7, 21, 14, 37, 52, tzinfo=timezone.utc)
        self.assertEqual(um.floor_hour(dt), "2026-07-21T14")

    def test_merge_hourly_buckets_overwrites_and_prunes(self):
        now = datetime(2026, 7, 21, 14, 30, tzinfo=timezone.utc)
        old_key = um.floor_hour(now - timedelta(hours=800))  # > 720h -> pruned
        recent_key = um.floor_hour(now - timedelta(hours=2))
        existing = {old_key: 111, recent_key: 50}
        fresh = {recent_key: 999}  # fresh rescan wins (self-correcting overwrite)
        merged = um.merge_hourly_buckets(existing, fresh, now)
        self.assertNotIn(old_key, merged)
        self.assertEqual(merged[recent_key], 999)

    def test_merge_hourly_buckets_boundary_and_new_key(self):
        now = datetime(2026, 7, 21, 14, 30, tzinfo=timezone.utc)
        just_inside = um.floor_hour(now - timedelta(hours=719))
        just_outside = um.floor_hour(now - timedelta(hours=721))
        merged = um.merge_hourly_buckets({just_inside: 1, just_outside: 2}, {}, now)
        self.assertIn(just_inside, merged)
        self.assertNotIn(just_outside, merged)

    def test_merge_hourly_buckets_drops_malformed_keys(self):
        merged = um.merge_hourly_buckets({"not-an-hour-key": 5}, {}, um.utcnow())
        self.assertEqual(merged, {})

    def test_hourly_baseline_stats_none_when_empty_or_all_zero(self):
        self.assertIsNone(um.hourly_baseline_stats({}))
        self.assertIsNone(um.hourly_baseline_stats({"a": 0, "b": 0}))

    def test_hourly_baseline_stats_computes_mean_stddev_over_nonzero(self):
        import statistics as _stats

        buckets = {"a": 10, "b": 20, "c": 0, "d": 30}
        mean, stddev, n = um.hourly_baseline_stats(buckets)
        self.assertEqual(n, 3)
        self.assertAlmostEqual(mean, 20.0)
        self.assertAlmostEqual(stddev, _stats.pstdev([10, 20, 30]))

    def test_anomaly_hours_to_check_uses_fully_elapsed_hours(self):
        now = datetime(2026, 7, 21, 14, 37, tzinfo=timezone.utc)
        self.assertEqual(um.anomaly_hours_to_check(now), ["2026-07-21T13", "2026-07-21T12"])
        self.assertEqual(
            um.anomaly_hours_to_check(now, count=3),
            ["2026-07-21T13", "2026-07-21T12", "2026-07-21T11"],
        )
        # Exact hour boundary must not shift the "current in-progress hour" excluded set.
        now2 = now.replace(minute=0, second=0, microsecond=0)
        self.assertEqual(um.anomaly_hours_to_check(now2), ["2026-07-21T13", "2026-07-21T12"])

    def test_anomaly_alert_needed_insufficient_samples(self):
        buckets = {f"h{i}": 100 for i in range(5)}
        ok, details = um.anomaly_alert_needed(buckets, um.utcnow(), min_samples=30)
        self.assertFalse(ok)
        self.assertEqual(details["reason"], "insufficient_samples")

    def test_anomaly_alert_needed_no_data(self):
        ok, details = um.anomaly_alert_needed({}, um.utcnow())
        self.assertFalse(ok)
        self.assertEqual(details["reason"], "no_data")

    def test_anomaly_alert_needed_zero_variance_guard(self):
        buckets = {f"k{i}": 100 for i in range(40)}  # constant -> stddev == 0
        ok, details = um.anomaly_alert_needed(buckets, um.utcnow())
        self.assertFalse(ok)
        self.assertEqual(details["reason"], "zero_variance")

    def test_anomaly_alert_needed_single_spike_not_consecutive(self):
        now = datetime(2026, 7, 21, 14, 37, tzinfo=timezone.utc)
        buckets = {um.floor_hour(now - timedelta(hours=h)): 10 for h in range(3, 45)}
        buckets[um.floor_hour(now - timedelta(hours=1))] = 10000  # last complete hour spikes
        # hour=2 (the other evaluated slot) stays baseline -> not 2 consecutive
        ok, details = um.anomaly_alert_needed(buckets, now)
        self.assertFalse(ok)
        self.assertEqual(details["reason"], "not_anomalous")

    def test_anomaly_alert_needed_two_consecutive_fires(self):
        now = datetime(2026, 7, 21, 14, 37, tzinfo=timezone.utc)
        buckets = {um.floor_hour(now - timedelta(hours=h)): 10 for h in range(3, 45)}
        buckets[um.floor_hour(now - timedelta(hours=1))] = 10000
        buckets[um.floor_hour(now - timedelta(hours=2))] = 9000
        ok, details = um.anomaly_alert_needed(buckets, now)
        self.assertTrue(ok)
        self.assertEqual(details["reason"], "anomalous")
        self.assertEqual(details["hour_keys"], ["2026-07-21T13", "2026-07-21T12"])
        self.assertEqual(details["hour_values"], [10000, 9000])

    def test_anomaly_alert_needed_missing_hour_defaults_to_zero_not_anomalous(self):
        now = datetime(2026, 7, 21, 14, 37, tzinfo=timezone.utc)
        buckets = {um.floor_hour(now - timedelta(hours=h)): 10 for h in range(3, 45)}
        # No entries at all for hours 1/2 -> .get(...,0) -> well below any positive threshold.
        ok, details = um.anomaly_alert_needed(buckets, now)
        self.assertFalse(ok)

    def test_build_anomaly_alert_text_says_investigate_not_incident(self):
        details = {
            "hour_keys": ["2026-07-21T13", "2026-07-21T12"],
            "hour_values": [10000, 9000],
            "mean": 100.0,
            "stddev": 20.0,
            "sigma": 3.0,
            "threshold": 160.0,
            "n": 40,
        }
        text = um.build_anomaly_alert_text(details)
        self.assertIn("investigate", text.lower())
        self.assertNotIn("incident", text.lower())
        self.assertIn("2026-07-21T13", text)
        self.assertIn("2026-07-21T12", text)


class _StateRedirectMixin:
    """Redirect every module-global path to a per-test tmp dir; never touch real state."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, str(self.tmp), True)
        self._orig = {
            "STATE_DIR": um.STATE_DIR,
            "STATE_FILE": um.STATE_FILE,
            "ALERT_COOLDOWN_FILE": um.ALERT_COOLDOWN_FILE,
            "HOURLY_STATE_FILE": um.HOURLY_STATE_FILE,
            "TRANSCRIPT_ROOT": um.TRANSCRIPT_ROOT,
            "TELEGRAM_ENV": um.TELEGRAM_ENV,
        }
        um.STATE_DIR = self.tmp / "state"
        um.STATE_FILE = um.STATE_DIR / "usage.jsonl"
        um.ALERT_COOLDOWN_FILE = um.STATE_DIR / "alert_cooldown"
        um.HOURLY_STATE_FILE = um.STATE_DIR / "usage_hourly.json"
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

    def _assistant_rec(self, toks: int, when: datetime, cache_creation: int = 0, cache_read: int = 0) -> dict:
        usage = {"input_tokens": toks}
        if cache_creation:
            usage["cache_creation_input_tokens"] = cache_creation
        if cache_read:
            usage["cache_read_input_tokens"] = cache_read
        return {
            "type": "assistant",
            "timestamp": _iso(when),
            "message": {"usage": usage},
        }


class ScanTranscriptsTest(_StateRedirectMixin, unittest.TestCase):
    def test_counts_recent_assistant_usage_in_all_windows(self):
        now = um.utcnow()
        self._write_transcript("s1.jsonl", [self._assistant_rec(1000, now - timedelta(minutes=10))])
        tokens, sessions, scanned, cache_creation, cache_read, hourly_tokens = um.scan_transcripts(now)
        self.assertEqual(scanned, 1)
        for h in um.WINDOWS_HOURS:
            self.assertEqual(tokens[h], 1000)
            self.assertEqual(sessions[h], {"s1"})
            self.assertEqual(cache_creation[h], 0)
            self.assertEqual(cache_read[h], 0)

    def test_per_record_window_cutoff(self):
        now = um.utcnow()
        # Fresh file mtime (so it's scanned), but the row is 10h old -> in 24h/7d, not 1h/5h.
        self._write_transcript("s2.jsonl", [self._assistant_rec(700, now - timedelta(hours=10))])
        tokens, sessions, _, cache_creation, cache_read, hourly_tokens = um.scan_transcripts(now)
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
        tokens, sessions, scanned, cache_creation, cache_read, hourly_tokens = um.scan_transcripts(now)
        self.assertEqual(tokens[1], 50)
        self.assertEqual(sessions[1], {"s3"})

    def test_missing_transcript_root_is_safe(self):
        um.TRANSCRIPT_ROOT = self.tmp / "does-not-exist"
        tokens, sessions, scanned, cache_creation, cache_read, hourly_tokens = um.scan_transcripts(um.utcnow())
        self.assertEqual(scanned, 0)
        self.assertEqual(tokens[5], 0)
        self.assertEqual(cache_creation[5], 0)
        self.assertEqual(cache_read[5], 0)
        self.assertEqual(hourly_tokens, {})

    def test_sums_cache_creation_and_read_per_window(self):
        now = um.utcnow()
        self._write_transcript(
            "s4.jsonl",
            [
                self._assistant_rec(100, now - timedelta(minutes=5), cache_creation=40, cache_read=200),
                self._assistant_rec(100, now - timedelta(hours=10), cache_creation=10, cache_read=5),  # 24h/7d only
            ],
        )
        tokens, sessions, scanned, cache_creation, cache_read, hourly_tokens = um.scan_transcripts(now)
        self.assertEqual(cache_creation[1], 40)
        self.assertEqual(cache_read[1], 200)
        self.assertEqual(cache_creation[24], 50)
        self.assertEqual(cache_read[24], 205)

    def test_hourly_tokens_bucketed_by_calendar_hour(self):
        """T-1897: a byproduct of the existing walk — same scan, no extra transcript I/O."""
        now = um.utcnow()
        self._write_transcript("s5.jsonl", [self._assistant_rec(300, now)])
        tokens, sessions, scanned, cache_creation, cache_read, hourly_tokens = um.scan_transcripts(now)
        self.assertEqual(hourly_tokens[um.floor_hour(now)], 300)

    def test_hourly_tokens_multiple_records_same_hour_sum(self):
        now = um.utcnow().replace(minute=30, second=0, microsecond=0)
        self._write_transcript(
            "s7.jsonl",
            [
                self._assistant_rec(100, now),
                self._assistant_rec(50, now - timedelta(minutes=10)),
            ],
        )
        tokens, sessions, scanned, cache_creation, cache_read, hourly_tokens = um.scan_transcripts(now)
        self.assertEqual(hourly_tokens[um.floor_hour(now)], 150)
        self.assertEqual(len(hourly_tokens), 1)

    def test_hourly_tokens_excludes_records_older_than_168h_reach(self):
        now = um.utcnow()
        self._write_transcript("s6.jsonl", [self._assistant_rec(400, now - timedelta(days=10))])
        tokens, sessions, scanned, cache_creation, cache_read, hourly_tokens = um.scan_transcripts(now)
        self.assertEqual(hourly_tokens, {})


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

    def test_cooldown_kinds_are_isolated(self):
        """T-1875: weekly/cache cooldowns don't share a file with the tokens cooldown or
        each other — stamping one kind must not put the others in cooldown."""
        now = um.utcnow()
        self.assertEqual(um.cooldown_file("tokens"), um.ALERT_COOLDOWN_FILE)  # zero-migration default
        self.assertEqual(um.cooldown_file("weekly"), um.STATE_DIR / "alert_cooldown_weekly")
        self.assertEqual(um.cooldown_file("cache"), um.STATE_DIR / "alert_cooldown_cache")

        um.stamp_cooldown(now, kind="weekly")
        self.assertTrue(um.alert_in_cooldown(now, kind="weekly"))
        self.assertFalse(um.alert_in_cooldown(now, kind="tokens"))
        self.assertFalse(um.alert_in_cooldown(now, kind="cache"))

        um.stamp_cooldown(now, kind="cache")
        self.assertTrue(um.alert_in_cooldown(now, kind="cache"))
        self.assertFalse(um.alert_in_cooldown(now, kind="tokens"))

    def test_write_snapshot_appends_jsonl(self):
        snap = um.Snapshot(ts=_iso(um.utcnow()), tokens_by_window={"5h": 1}, sessions_by_window={"5h": 1}, transcripts_scanned=1)
        um.write_snapshot(snap)
        um.write_snapshot(snap)
        lines = um.STATE_FILE.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["tokens_by_window"]["5h"], 1)

    def test_write_snapshot_defaults_cache_fields_empty(self):
        """Backward-compat: constructing a Snapshot without the new T-1875 fields (as old
        call sites / historical rows would) must not error — default_factory=dict kicks in."""
        snap = um.Snapshot(ts=_iso(um.utcnow()), tokens_by_window={"5h": 1}, sessions_by_window={"5h": 1}, transcripts_scanned=1)
        self.assertEqual(snap.cache_creation_by_window, {})
        self.assertEqual(snap.cache_read_by_window, {})
        um.write_snapshot(snap)
        line = json.loads(um.STATE_FILE.read_text(encoding="utf-8").strip().splitlines()[0])
        self.assertEqual(line["cache_creation_by_window"], {})
        self.assertEqual(line["cache_read_by_window"], {})

    def test_load_hourly_state_missing_file_returns_empty(self):
        self.assertEqual(um.load_hourly_state(), {})

    def test_write_then_load_hourly_state_roundtrip(self):
        um.write_hourly_state({"2026-07-21T13": 500, "2026-07-21T14": 0})
        self.assertEqual(um.load_hourly_state(), {"2026-07-21T13": 500, "2026-07-21T14": 0})

    def test_load_hourly_state_malformed_fails_open_empty(self):
        um.STATE_DIR.mkdir(parents=True, exist_ok=True)
        um.HOURLY_STATE_FILE.write_text("not json", encoding="utf-8")
        self.assertEqual(um.load_hourly_state(), {})
        um.HOURLY_STATE_FILE.write_text(json.dumps({"buckets": "not-a-dict"}), encoding="utf-8")
        self.assertEqual(um.load_hourly_state(), {})
        um.HOURLY_STATE_FILE.write_text(json.dumps(["not", "a", "dict", "at", "top", "level"]), encoding="utf-8")
        self.assertEqual(um.load_hourly_state(), {})

    def test_write_hourly_state_uses_atomic_tmp_then_replace(self):
        """Rebound (T-1897): a crash mid-write on the 30-min cron must not truncate the sidecar.
        Assert the real shape — write lands in a sibling .tmp path, then os.replace swaps it in —
        by wrapping the real os.replace and inspecting what it was called with."""
        with patch("tools.usage_monitor.os.replace", wraps=os.replace) as replace_mock:
            um.write_hourly_state({"2026-07-21T13": 5})
        replace_mock.assert_called_once()
        src, dst = replace_mock.call_args[0]
        self.assertEqual(Path(dst), um.HOURLY_STATE_FILE)
        self.assertTrue(str(src).endswith(".tmp"))
        self.assertFalse(Path(src).exists())  # consumed by the real replace, not left behind
        self.assertEqual(um.load_hourly_state(), {"2026-07-21T13": 5})

    def test_write_hourly_state_leaves_prior_state_intact_on_simulated_failure(self):
        """A failure during the swap (disk full, killed cron, etc.) must not corrupt or truncate
        the PREVIOUS good state — the whole point of tmp+replace over a plain write_text."""
        um.write_hourly_state({"2026-07-21T12": 111})  # establish good prior state
        with patch("tools.usage_monitor.os.replace", side_effect=OSError("simulated crash")):
            with self.assertRaises(OSError):
                um.write_hourly_state({"2026-07-21T13": 999})
        # Prior state survives untouched -- no half-written/corrupted target file.
        self.assertEqual(um.load_hourly_state(), {"2026-07-21T12": 111})


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

    def test_weekly_quota_omitted_by_default_no_alert(self):
        """T-1875: no --weekly-quota-tokens (and no env var) -> feature stays off, matching
        the collect-only bootstrap philosophy of the pre-existing --alert-tokens flag."""
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(999999, now)])
        with patch("tools.usage_monitor.send_telegram") as send, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PANTHEON_WEEKLY_QUOTA_TOKENS", None)
            rc, _ = self._run(["usage_monitor"])
            self.assertEqual(rc, 0)
            send.assert_not_called()

    def test_weekly_pacing_dry_run_alert(self):
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(5000, now)])
        buf_err = io.StringIO()
        with patch("tools.usage_monitor.send_telegram") as send:
            with patch.object(sys, "argv", [
                "usage_monitor", "--weekly-quota-tokens", "6000", "--weekly-pacing-pct", "5",
            ]), redirect_stdout(io.StringIO()), redirect_stderr(buf_err):
                rc = um.main()
            self.assertEqual(rc, 0)
            send.assert_not_called()
            self.assertIn("WEEKLY-PACING ALERT", buf_err.getvalue())

    def test_weekly_pacing_live_send_and_cooldown_independent_of_tokens_kind(self):
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(5000, now)])
        with patch("tools.usage_monitor.send_telegram", return_value=True) as send:
            rc, _ = self._run([
                "usage_monitor", "--weekly-quota-tokens", "6000", "--weekly-pacing-pct", "5",
                "--send-telegram",
            ])
            self.assertEqual(rc, 0)
            self.assertEqual(send.call_count, 1)
            self.assertTrue(um.alert_in_cooldown(now, kind="weekly"))
            self.assertFalse(um.alert_in_cooldown(now, kind="tokens"))  # independent cooldown
            # Second run within cooldown -> blocked
            rc2, _ = self._run([
                "usage_monitor", "--weekly-quota-tokens", "6000", "--weekly-pacing-pct", "5",
                "--send-telegram",
            ])
            self.assertEqual(rc2, 0)
            self.assertEqual(send.call_count, 1)

    def test_cache_ratio_threshold_omitted_by_default_no_alert(self):
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(100, now, cache_creation=500, cache_read=1)])
        with patch("tools.usage_monitor.send_telegram") as send:
            rc, _ = self._run(["usage_monitor"])
            self.assertEqual(rc, 0)
            send.assert_not_called()

    def test_cache_ratio_live_send(self):
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(100, now, cache_creation=900, cache_read=100)])
        with patch("tools.usage_monitor.send_telegram", return_value=True) as send:
            rc, _ = self._run([
                "usage_monitor", "--cache-ratio-threshold", "0.5", "--cache-ratio-window", "24",
                "--send-telegram",
            ])
            self.assertEqual(rc, 0)
            self.assertEqual(send.call_count, 1)
            self.assertTrue(um.alert_in_cooldown(now, kind="cache"))

    def test_cache_ratio_send_failure_sets_rc_without_blocking_other_alerts(self):
        """rc accumulates across independent alert blocks rather than returning early."""
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(100, now, cache_creation=900, cache_read=100)])
        with patch("tools.usage_monitor.send_telegram", return_value=False) as send:
            rc, _ = self._run([
                "usage_monitor", "--cache-ratio-threshold", "0.5", "--send-telegram",
            ])
            self.assertEqual(rc, 1)
            send.assert_called_once()

    def test_print_summary_includes_cache_ratio_line(self):
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(100, now, cache_creation=50, cache_read=200)])
        with patch("tools.usage_monitor.send_telegram"):
            rc, out = self._run(["usage_monitor", "--print-summary"])
            self.assertEqual(rc, 0)
            self.assertIn("cache 24h creation:read ratio", out)

    # --- T-1897: hourly-anomaly detector ---

    def test_hourly_bucket_state_written_even_without_anomaly_flag(self):
        """Collection is unconditional every run — the flag only gates ALERTING."""
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(5000, now)])
        with patch("tools.usage_monitor.send_telegram"):
            rc, _ = self._run(["usage_monitor"])
            self.assertEqual(rc, 0)
        self.assertTrue(um.HOURLY_STATE_FILE.exists())
        buckets = um.load_hourly_state()
        self.assertEqual(buckets.get(um.floor_hour(now)), 5000)

    def test_anomaly_alert_cold_start_fresh_deployment_no_crash_no_alert(self):
        """Fresh deployment: no prior hourly state, a single transcript. Runs fleet-wide on a
        30-min cron — a crash here is an observability outage, so this MUST be safe even with
        the flag enabled and a huge single-run token count."""
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(999999999, now)])
        with patch("tools.usage_monitor.send_telegram") as send:
            rc, _ = self._run(["usage_monitor", "--enable-anomaly-alert", "--send-telegram"])
            self.assertEqual(rc, 0)
            send.assert_not_called()

    def test_anomaly_alert_short_history_insufficient_samples_no_alert(self):
        """A handful of hourly buckets (below ANOMALY_MIN_SAMPLES_DEFAULT) must stay collect-only
        even if two consecutive hours happen to look extreme relative to that tiny sample."""
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(100, now)])
        buckets = {um.floor_hour(now - timedelta(hours=h)): 50 for h in range(3, 10)}  # only 7
        buckets[um.floor_hour(now - timedelta(hours=1))] = 999999
        buckets[um.floor_hour(now - timedelta(hours=2))] = 888888
        um.write_hourly_state(buckets)
        with patch("tools.usage_monitor.send_telegram") as send:
            rc, _ = self._run(["usage_monitor", "--enable-anomaly-alert", "--send-telegram"])
            self.assertEqual(rc, 0)
            send.assert_not_called()

    def test_anomaly_alert_default_disabled_even_with_qualifying_data(self):
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(100, now)])
        buckets = {um.floor_hour(now - timedelta(hours=h)): 50 for h in range(3, 45)}
        buckets[um.floor_hour(now - timedelta(hours=1))] = 999999
        buckets[um.floor_hour(now - timedelta(hours=2))] = 888888
        um.write_hourly_state(buckets)
        with patch("tools.usage_monitor.send_telegram") as send:
            rc, _ = self._run(["usage_monitor", "--send-telegram"])  # no --enable-anomaly-alert
            self.assertEqual(rc, 0)
            send.assert_not_called()

    def test_anomaly_alert_dry_run_prints_no_send(self):
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(100, now)])
        buckets = {um.floor_hour(now - timedelta(hours=h)): 50 for h in range(3, 45)}
        buckets[um.floor_hour(now - timedelta(hours=1))] = 999999
        buckets[um.floor_hour(now - timedelta(hours=2))] = 888888
        um.write_hourly_state(buckets)
        buf_err = io.StringIO()
        with patch("tools.usage_monitor.send_telegram") as send:
            with patch.object(sys, "argv", ["usage_monitor", "--enable-anomaly-alert"]), \
                    redirect_stdout(io.StringIO()), redirect_stderr(buf_err):
                rc = um.main()
            self.assertEqual(rc, 0)
            send.assert_not_called()
            self.assertIn("HOURLY-ANOMALY ALERT", buf_err.getvalue())

    def test_anomaly_alert_live_send_and_cooldown_independent_of_other_kinds(self):
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(100, now)])
        buckets = {um.floor_hour(now - timedelta(hours=h)): 50 for h in range(3, 45)}
        buckets[um.floor_hour(now - timedelta(hours=1))] = 999999
        buckets[um.floor_hour(now - timedelta(hours=2))] = 888888
        um.write_hourly_state(buckets)
        with patch("tools.usage_monitor.send_telegram", return_value=True) as send:
            rc, _ = self._run(["usage_monitor", "--enable-anomaly-alert", "--send-telegram"])
            self.assertEqual(rc, 0)
            self.assertEqual(send.call_count, 1)
            self.assertTrue(um.alert_in_cooldown(now, kind="anomaly"))
            self.assertFalse(um.alert_in_cooldown(now, kind="tokens"))
            # Second run within cooldown -> blocked, no second send.
            rc2, _ = self._run(["usage_monitor", "--enable-anomaly-alert", "--send-telegram"])
            self.assertEqual(rc2, 0)
            self.assertEqual(send.call_count, 1)

    def test_anomaly_alert_send_failure_sets_rc_without_blocking_other_alerts(self):
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(100, now)])
        buckets = {um.floor_hour(now - timedelta(hours=h)): 50 for h in range(3, 45)}
        buckets[um.floor_hour(now - timedelta(hours=1))] = 999999
        buckets[um.floor_hour(now - timedelta(hours=2))] = 888888
        um.write_hourly_state(buckets)
        with patch("tools.usage_monitor.send_telegram", return_value=False) as send:
            rc, _ = self._run(["usage_monitor", "--enable-anomaly-alert", "--send-telegram"])
            self.assertEqual(rc, 1)
            send.assert_called_once()
            self.assertFalse(um.alert_in_cooldown(now, kind="anomaly"))

    def test_hourly_bucket_pruning_drops_stale_entries_on_run(self):
        now = um.utcnow()
        self._write_transcript("s.jsonl", [self._assistant_rec(100, now)])
        stale_key = um.floor_hour(now - timedelta(hours=um.HOURLY_RETENTION_HOURS + 5))
        um.write_hourly_state({stale_key: 12345})
        with patch("tools.usage_monitor.send_telegram"):
            self._run(["usage_monitor"])
        self.assertNotIn(stale_key, um.load_hourly_state())


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
        # No 7d, cache-ratio, or hourly-anomaly data present -> those blocks stay silent, no regression.
        self.assertNotIn("7d-window token consumption", out)
        self.assertNotIn("cache_creation:cache_read ratio", out)
        self.assertNotIn("Hourly token-burn distribution", out)

    def test_enough_samples_prints_7d_calibration_block_when_present(self):
        um.STATE_DIR.mkdir(parents=True, exist_ok=True)
        rows = [
            json.dumps({"tokens_by_window": {"5h": v, "168h": v * 20}}) for v in range(1, 21)
        ]
        um.STATE_FILE.write_text("\n".join(rows) + "\n", encoding="utf-8")
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("7d-window token consumption", out)
        self.assertIn("NOT a suggested value", out)

    def test_enough_samples_prints_cache_ratio_calibration_block_when_present(self):
        um.STATE_DIR.mkdir(parents=True, exist_ok=True)
        rows = [
            json.dumps({
                "tokens_by_window": {"5h": v},
                "cache_creation_by_window": {"24h": v},
                "cache_read_by_window": {"24h": v * 4},
            })
            for v in range(1, 21)
        ]
        um.STATE_FILE.write_text("\n".join(rows) + "\n", encoding="utf-8")
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("cache_creation:cache_read ratio", out)

    def test_hourly_anomaly_calibration_block_silent_when_insufficient(self):
        um.STATE_DIR.mkdir(parents=True, exist_ok=True)
        rows = [json.dumps({"tokens_by_window": {"5h": v}}) for v in range(1, 21)]
        um.STATE_FILE.write_text("\n".join(rows) + "\n", encoding="utf-8")
        # No usage_hourly.json written -> hourly_baseline_stats returns None -> block stays silent.
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertNotIn("Hourly token-burn distribution", out)

    def test_prints_hourly_anomaly_calibration_block_when_present(self):
        um.STATE_DIR.mkdir(parents=True, exist_ok=True)
        rows = [json.dumps({"tokens_by_window": {"5h": v}}) for v in range(1, 21)]
        um.STATE_FILE.write_text("\n".join(rows) + "\n", encoding="utf-8")
        buckets = {f"2026-07-{(i % 28) + 1:02d}T{i % 24:02d}": 1000 + i * 37 for i in range(20)}
        um.write_hourly_state(buckets)
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("Hourly token-burn distribution", out)
        self.assertIn("anomaly-sigma", out)
        self.assertIn("sigma threshold at current sample", out)


if __name__ == "__main__":
    unittest.main()
