"""Unit tests for tools/fleet_usage_gate.py (T-4510).

Run from repo root:
    python3 -m unittest tools.test_fleet_usage_gate -v

Fixture provenance (D11): the payload shape is the statusLine ``rate_limits`` object as
OBSERVED LIVE on specialist 2026-10-04T18:2xZ (Orchestrator) — integer used_percentage, resets_at in
unix epoch seconds:
    {"five_hour": {"resets_at": 1791151200, "used_percentage": 13},
     "seven_day": {"resets_at": 1791738000, "used_percentage": 3}}
NOW below sits inside both of those windows (2026-10-04T18:53:20Z).
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools import fleet_usage_gate as fug  # noqa: E402

NOW = 1791140000
FIVE_RESET = 1791151200   # observed live: 2026-10-04T22:00:00Z
SEVEN_RESET = 1791738000  # observed live: 2026-10-11T17:00:00Z

LIVE_SPECIALIST_RATE_LIMITS = {
    "five_hour": {"resets_at": FIVE_RESET, "used_percentage": 13},
    "seven_day": {"resets_at": SEVEN_RESET, "used_percentage": 3},
}


def reading_doc(seat, ts, seven=None, five=None, harness=None):
    """A tap-shaped record. seven/five are (pct, resets_at) or None."""
    def win(w):
        return None if w is None else {"used_percentage": w[0], "resets_at": w[1]}
    doc = {"schema": fug.SCHEMA, "ts": ts, "seat": seat,
           "seven_day": win(seven), "five_hour": win(five)}
    if harness is not None:
        doc["harness"] = harness
    return doc


class _TmpDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name) / "fleet-usage"
        self.dir.mkdir()

    def put(self, seat, ts, seven=None, five=None):
        (self.dir / f"{seat}.json").write_text(
            json.dumps(reading_doc(seat, ts, seven, five)), encoding="utf-8")

    def cfg(self, **kw):
        # Most of this file pins T-4510's flat-threshold substrate. T-4889's
        # schedule is exercised separately below; production config enables it.
        kw.setdefault("schedule_enabled", False)
        return fug.GateConfig(dir=self.dir, **kw)

    def decide(self, **kw):
        return fug.evaluate(NOW, self.cfg(**kw))


class ThresholdTest(_TmpDirCase):
    def test_below_threshold_is_open(self):
        self.put("orchestrator", NOW - 5, seven=(94.9, SEVEN_RESET))
        self.assertFalse(self.decide().suppress)

    def test_at_threshold_suppresses(self):
        self.put("orchestrator", NOW - 5, seven=(95, SEVEN_RESET))
        d = self.decide()
        self.assertTrue(d.suppress)
        self.assertEqual(d.window_key, f"seven_day:{SEVEN_RESET}")

    def test_above_threshold_suppresses_and_reason_is_informative(self):
        self.put("recovery", NOW - 5, seven=(99, SEVEN_RESET))
        d = self.decide()
        self.assertTrue(d.suppress)
        for needle in ("99", "95", "recovery", "2026-10-11T17:00:00Z"):
            self.assertIn(needle, d.reason)

    def test_integer_and_float_percentages_are_both_accepted(self):
        # Live payloads carry INTEGER used_percentage (observed on specialist).
        self.put("a", NOW - 5, seven=(96, SEVEN_RESET))
        self.assertTrue(self.decide().suppress)
        self.put("a", NOW - 5, seven=(96.5, SEVEN_RESET))
        self.assertTrue(self.decide().suppress)

    def test_live_specialist_shape_round_trips_open_at_3_percent(self):
        (self.dir / "specialist.json").write_text(json.dumps({
            "schema": fug.SCHEMA, "ts": NOW - 5, "seat": "specialist",
            **{k: v for k, v in LIVE_SPECIALIST_RATE_LIMITS.items()}}), encoding="utf-8")
        d = self.decide()
        self.assertFalse(d.suppress)
        readings = fug.load_readings(self.dir, NOW)
        self.assertEqual(readings["seven_day"].pct, 3)
        self.assertEqual(readings["five_hour"].pct, 13)

    def test_bool_percentage_is_rejected(self):
        self.put("a", NOW - 5, seven=(True, SEVEN_RESET))
        self.assertFalse(self.decide().suppress)
        self.assertEqual(fug.load_readings(self.dir, NOW), {})


class ScheduledPacingTest(_TmpDirCase):
    CHICAGO = ZoneInfo("America/Chicago")

    @classmethod
    def epoch(cls, value: str) -> float:
        return datetime.fromisoformat(value).replace(tzinfo=cls.CHICAGO).timestamp()

    def scheduled(self, now: float, pct: float, *, harness="claude", seat="orchestrator"):
        reset = self.epoch("2026-10-11T12:00:00") if harness == "claude" else self.epoch("2026-10-10T14:15:00")
        path = self.dir / f"{harness}-{seat}.json"
        if harness == "codex":
            path = self.dir / "turns" / seat / "quota.json"
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            reading_doc(seat, now - 5, seven=(pct, reset), harness=harness)
        ), encoding="utf-8")
        cfg = fug.GateConfig(dir=self.dir, schedule_enabled=True)
        return fug.evaluate(now, cfg, harness=harness, seat=seat if harness == "codex" else None)

    def test_before_midpoint_50_is_the_slowdown_ceiling(self):
        now = self.epoch("2026-10-07T23:59:00")
        self.assertFalse(self.scheduled(now, 49.9).suppress)
        decision = self.scheduled(now, 50.0)
        self.assertTrue(decision.suppress)
        self.assertEqual(decision.band, "pre-midweek")
        self.assertEqual(decision.effective_threshold, 50.0)

    def test_between_midpoint_and_friday_ceiling_rises_linearly_to_95(self):
        midpoint = self.epoch("2026-10-08T00:00:00")
        friday = self.epoch("2026-10-09T18:00:00")
        now = midpoint + (friday - midpoint) / 2
        self.assertFalse(self.scheduled(now, 72.49).suppress)
        decision = self.scheduled(now, 72.5)
        self.assertTrue(decision.suppress)
        self.assertEqual(decision.band, "paced")
        self.assertAlmostEqual(decision.effective_threshold, 72.5)

    def test_friday_evening_starts_weekend_hold_even_below_the_curve(self):
        now = self.epoch("2026-10-09T18:00:00")
        decision = self.scheduled(now, 1.0)
        self.assertTrue(decision.suppress)
        self.assertEqual(decision.band, "weekend-hold")
        self.assertIn("weekend", decision.reason)

    def test_reset_releases_the_weekend_hold(self):
        reset = self.epoch("2026-10-11T12:00:00")
        self.assertTrue(self.scheduled(reset - 1, 1.0).suppress)
        # The old record expires exactly at reset, preserving fail-open semantics
        # until the first record from the new window arrives.
        self.assertFalse(self.scheduled(reset, 99.0).suppress)

    def test_codex_uses_its_own_midpoint_and_saturday_reset(self):
        midpoint = self.epoch("2026-10-07T02:15:00")
        before = self.scheduled(midpoint - 1, 50.0, harness="codex", seat="consultant")
        self.assertTrue(before.suppress)
        self.assertEqual(before.reading.seat, "consultant")
        reset = self.epoch("2026-10-10T14:15:00")
        self.assertFalse(self.scheduled(reset, 99.0, harness="codex", seat="consultant").suppress)

    def test_95_is_an_absolute_ceiling_even_when_curve_override_is_higher(self):
        now = self.epoch("2026-10-09T17:59:00")
        self.assertTrue(self.scheduled(now, 95.0).suppress)

    def test_friday_boundary_wins_if_provider_moves_reset_past_the_old_schedule(self):
        # Live Codex on 2026-11-04 reported a Tuesday reset, putting its cycle
        # midpoint on Friday evening. The weekend rule still begins at 18:00.
        now = self.epoch("2026-11-06T18:00:00")
        reset = self.epoch("2026-11-10T08:05:00")
        quota = self.dir / "turns" / "consultant" / "quota.json"
        quota.parent.mkdir(parents=True)
        quota.write_text(json.dumps(
            reading_doc("consultant", now - 5, seven=(1.0, reset), harness="codex")
        ), encoding="utf-8")
        decision = fug.evaluate(
            now, fug.GateConfig(dir=self.dir, schedule_enabled=True),
            harness="codex", seat="consultant",
        )
        self.assertTrue(decision.suppress)
        self.assertEqual(decision.band, "weekend-hold")


class ProviderIsolationTest(_TmpDirCase):
    def put_provider(self, filename, seat, harness, pct, ts=NOW - 5):
        path = self.dir / filename
        if harness == "codex":
            path = self.dir / "turns" / seat / "quota.json"
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            reading_doc(seat, ts, seven=(pct, SEVEN_RESET), harness=harness)
        ), encoding="utf-8")

    def test_claude_ignores_codex_records(self):
        self.put_provider("codex-consultant.json", "consultant", "codex", 99)
        self.put("orchestrator", NOW - 10, seven=(10, SEVEN_RESET))
        self.assertFalse(fug.evaluate(NOW, self.cfg(), harness="claude").suppress)

    def test_codex_pool_is_seat_specific(self):
        self.put_provider("codex-consultant.json", "consultant", "codex", 99)
        self.put_provider("codex-watchman.json", "watchman", "codex", 10, ts=NOW - 1)
        self.assertTrue(fug.evaluate(NOW, self.cfg(), harness="codex", seat="consultant").suppress)
        self.assertFalse(fug.evaluate(NOW, self.cfg(), harness="codex", seat="watchman").suppress)

    def test_codex_without_a_matching_pool_fails_open(self):
        self.put_provider("codex-consultant.json", "consultant", "codex", 99)
        self.assertFalse(fug.evaluate(NOW, self.cfg(), harness="codex", seat="artisan").suppress)


class ExpiryTest(_TmpDirCase):
    def test_expired_resets_at_fails_open_even_at_99(self):
        self.put("orchestrator", NOW - 5, seven=(99, NOW - 1))
        self.assertFalse(self.decide().suppress)

    def test_resets_at_equal_to_now_is_expired(self):
        self.put("orchestrator", NOW - 5, seven=(99, NOW))
        self.assertFalse(self.decide().suppress)

    def test_gate_releases_by_itself_at_the_reset(self):
        self.put("orchestrator", NOW - 5, seven=(99, SEVEN_RESET))
        self.assertTrue(fug.evaluate(SEVEN_RESET - 1, self.cfg()).suppress)
        self.assertFalse(fug.evaluate(SEVEN_RESET, self.cfg()).suppress)
        self.assertFalse(fug.evaluate(SEVEN_RESET + 3600, self.cfg()).suppress)

    def test_implausibly_far_resets_at_is_ignored(self):
        # A garbage reading must not be able to pin the fleet shut for years.
        self.put("orchestrator", NOW - 5, seven=(99, NOW + 365 * 86400))
        self.assertFalse(self.decide().suppress)

    def test_future_dated_ts_is_ignored(self):
        self.put("orchestrator", NOW + 3600, seven=(99, SEVEN_RESET))
        self.assertFalse(self.decide().suppress)


class FailOpenTest(_TmpDirCase):
    def test_missing_dir_is_open(self):
        d = fug.evaluate(NOW, fug.GateConfig(dir=self.dir / "nope"))
        self.assertFalse(d.suppress)

    def test_empty_dir_is_open(self):
        self.assertFalse(self.decide().suppress)

    def test_garbage_and_malformed_files_are_open(self):
        bad = {
            "garbage.json": "not json at all {{{",
            "truncated.json": '{"ts": 1791139990, "seven_day": {"used_perc',
            "list.json": "[1, 2, 3]",
            "scalar.json": "42",
            "empty.json": "",
            "strpct.json": json.dumps({"ts": NOW - 5, "seven_day":
                                       {"used_percentage": "99", "resets_at": SEVEN_RESET}}),
            "nan.json": '{"ts": %d, "seven_day": {"used_percentage": NaN, "resets_at": %d}}'
                        % (NOW - 5, SEVEN_RESET),
            "neg.json": json.dumps({"ts": NOW - 5, "seven_day":
                                    {"used_percentage": -1, "resets_at": SEVEN_RESET}}),
            "nots.json": json.dumps({"seven_day":
                                     {"used_percentage": 99, "resets_at": SEVEN_RESET}}),
            "nowindow.json": json.dumps({"ts": NOW - 5}),
        }
        for name, text in bad.items():
            (self.dir / name).write_text(text, encoding="utf-8")
        (self.dir / "binary.json").write_bytes(b"\xff\xfe\x00\x01")
        self.assertFalse(self.decide().suppress)

    def test_oversized_file_is_ignored(self):
        (self.dir / "big.json").write_text(
            json.dumps({"ts": NOW - 5, "pad": "x" * (fug.MAX_READING_BYTES + 10),
                        "seven_day": {"used_percentage": 99, "resets_at": SEVEN_RESET}}),
            encoding="utf-8")
        self.assertFalse(self.decide().suppress)

    def test_unreadable_file_is_open(self):
        self.put("orchestrator", NOW - 5, seven=(99, SEVEN_RESET))
        if os.geteuid() == 0:
            self.skipTest("root reads through chmod 000")
        os.chmod(self.dir / "orchestrator.json", 0)
        self.addCleanup(os.chmod, self.dir / "orchestrator.json", 0o644)
        self.assertFalse(self.decide().suppress)

    def test_unreadable_dir_is_open(self):
        if os.geteuid() == 0:
            self.skipTest("root reads through chmod 000")
        self.put("orchestrator", NOW - 5, seven=(99, SEVEN_RESET))
        os.chmod(self.dir, 0)
        self.addCleanup(os.chmod, self.dir, 0o755)
        self.assertFalse(self.decide().suppress)

    def test_one_bad_file_does_not_poison_a_good_one(self):
        (self.dir / "garbage.json").write_text("{{{", encoding="utf-8")
        self.put("orchestrator", NOW - 5, seven=(97, SEVEN_RESET))
        self.assertTrue(self.decide().suppress)

    def test_temp_and_non_json_files_are_ignored(self):
        doc = json.dumps(reading_doc("x", NOW - 5, seven=(99, SEVEN_RESET)))
        (self.dir / ".orchestrator.abc123").write_text(doc, encoding="utf-8")   # tap temp file
        (self.dir / "orchestrator.json.bak").write_text(doc, encoding="utf-8")
        (self.dir / ".hidden.json").write_text(doc, encoding="utf-8")
        self.assertFalse(self.decide().suppress)

    # -- REBOUND #1: one hostile file must never blind the gate to a valid sibling ----------
    def put_raw(self, name, text):
        (self.dir / name).write_text(text, encoding="utf-8")

    def test_huge_integer_percentage_does_not_blind_the_gate(self):
        # math.isfinite(10**400) raises OverflowError; it must read as "not a number".
        big = "9" * 400
        self.put_raw("evil.json", '{"ts": %d, "seven_day": {"used_percentage": %s, '
                                  '"resets_at": %d}}' % (NOW - 5, big, SEVEN_RESET))
        self.put("orchestrator", NOW - 10, seven=(97, SEVEN_RESET))
        d = self.decide()
        self.assertTrue(d.suppress, "the valid 97% sibling must still gate")
        self.assertEqual(d.reading.seat, "orchestrator")

    def test_huge_integer_ts_and_resets_at_do_not_blind_the_gate(self):
        big = "9" * 400
        self.put_raw("evil1.json", '{"ts": %s, "seven_day": {"used_percentage": 99, '
                                   '"resets_at": %d}}' % (big, SEVEN_RESET))
        self.put_raw("evil2.json", '{"ts": %d, "seven_day": {"used_percentage": 99, '
                                   '"resets_at": %s}}' % (NOW - 5, big))
        self.put("orchestrator", NOW - 10, seven=(97, SEVEN_RESET))
        self.assertTrue(self.decide().suppress)

    def test_deeply_nested_json_does_not_blind_the_gate(self):
        # json.loads raises RecursionError (not a ValueError) on deep nesting under the size cap.
        deep = "[" * 30000
        self.assertLess(len(deep), fug.MAX_READING_BYTES)
        self.put_raw("deep.json", deep)
        self.put_raw("deepobj.json", '{"a":' * 15000)
        self.put("orchestrator", NOW - 10, seven=(97, SEVEN_RESET))
        self.assertTrue(self.decide().suppress)

    def test_read_doc_itself_survives_deep_nesting(self):
        # _read_doc is self-contained: it returns None rather than leaning on the caller's catch.
        self.put_raw("deep.json", "[" * 30000)
        self.assertIsNone(fug._read_doc(self.dir / "deep.json"))

    def test_read_doc_refuses_a_non_regular_file_even_with_valid_content(self):
        # Defence in depth beyond "it did not hang": a FIFO/device feeding valid-looking JSON
        # is not a reading. Make fstat report a FIFO for a file that holds a good document.
        self.put("orchestrator", NOW - 5, seven=(99, SEVEN_RESET))
        real = os.fstat
        fake = lambda fd: os.stat_result((stat.S_IFIFO | 0o644,) + tuple(real(fd))[1:])  # noqa: E731
        with unittest.mock.patch.object(fug.os, "fstat", fake):
            self.assertIsNone(fug._read_doc(self.dir / "orchestrator.json"))
        self.assertIsNotNone(fug._read_doc(self.dir / "orchestrator.json"))

    def test_num_rejects_overflowing_ints_instead_of_raising(self):
        self.assertFalse(fug._num(10 ** 400))
        self.assertFalse(fug._num(-(10 ** 400)))
        self.assertTrue(fug._num(97))
        self.assertTrue(fug._num(97.5))
        self.assertFalse(fug._num(True))

    def test_load_readings_isolates_a_per_file_failure(self):
        # Belt and braces: even an exception nobody anticipated in one file's handling must
        # skip that file only. Patch the validator to blow up for one seat.
        self.put("evil", NOW - 5, seven=(99, SEVEN_RESET))
        self.put("orchestrator", NOW - 10, seven=(97, SEVEN_RESET))
        real = fug._valid_window

        def flaky(window, body, ts, seat, now):
            if seat == "evil":
                raise RuntimeError("unanticipated")
            return real(window, body, ts, seat, now)

        with unittest.mock.patch.object(fug, "_valid_window", flaky):
            readings = fug.load_readings(self.dir, NOW)
        self.assertEqual(readings["seven_day"].seat, "orchestrator")

    def test_a_fifo_named_json_does_not_hang_the_reader(self):
        # open(fifo, 'rb') blocks forever with no writer. Run in a subprocess with a bound so a
        # regression fails (TimeoutExpired) instead of hanging the whole suite.
        os.mkfifo(self.dir / "pipe.json")
        self.put("orchestrator", NOW - 10, seven=(97, SEVEN_RESET))
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "from pathlib import Path\n"
            "from tools import fleet_usage_gate as f\n"
            "d = f.evaluate(%d, f.GateConfig(dir=Path(%r)))\n"
            "print(d.suppress, d.reading.seat)\n" % (str(REPO_ROOT), NOW, str(self.dir)))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             timeout=15, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), ["True", "orchestrator"])

    def test_non_regular_files_named_json_are_skipped(self):
        # Same bounded-subprocess shape: an in-process FIFO read would hang the suite on a
        # regression.
        os.mkfifo(self.dir / "pipe.json")
        (self.dir / "adir.json").mkdir()
        os.symlink("/dev/null", self.dir / "devnull.json")
        self.put("orchestrator", NOW - 10, seven=(97, SEVEN_RESET))
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "from tools import fleet_usage_gate as f\n"
            "r = f.load_readings(%r, %d)\n"
            "print(sorted((w, x.seat) for w, x in r.items()))\n"
            % (str(REPO_ROOT), str(self.dir), NOW))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             timeout=15, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "[('seven_day', 'orchestrator')]")

    def test_evaluate_never_raises(self):
        class Boom:
            def __getattr__(self, name):
                raise RuntimeError("boom")
        d = fug.evaluate(NOW, Boom())  # a hostile cfg object
        self.assertFalse(d.suppress)


class FreshestWinsTest(_TmpDirCase):
    def test_newer_low_reading_beats_older_high_reading(self):
        self.put("orchestrator", NOW - 1000, seven=(99, SEVEN_RESET))
        self.put("recovery", NOW - 10, seven=(50, SEVEN_RESET))
        self.assertFalse(self.decide().suppress)

    def test_newer_high_reading_beats_older_low_reading(self):
        self.put("orchestrator", NOW - 1000, seven=(50, SEVEN_RESET))
        self.put("recovery", NOW - 10, seven=(99, SEVEN_RESET))
        d = self.decide()
        self.assertTrue(d.suppress)
        self.assertEqual(d.reading.seat, "recovery")

    def test_expired_but_newer_reading_does_not_mask_an_older_valid_one(self):
        self.put("orchestrator", NOW - 1000, seven=(99, SEVEN_RESET))
        self.put("recovery", NOW - 10, seven=(1, NOW - 5))   # fresher, but already expired
        self.assertTrue(self.decide().suppress)

    def test_freshest_is_chosen_per_window(self):
        # recovery is fresher overall but has no seven_day window; orchestrator's seven_day stands.
        self.put("orchestrator", NOW - 1000, seven=(99, SEVEN_RESET))
        self.put("recovery", NOW - 10, five=(10, FIVE_RESET))
        self.assertTrue(self.decide().suppress)


class FiveHourTest(_TmpDirCase):
    def test_five_hour_does_not_gate_by_default(self):
        self.put("orchestrator", NOW - 5, seven=(10, SEVEN_RESET), five=(99, FIVE_RESET))
        self.assertFalse(self.decide().suppress)

    def test_five_hour_gates_when_configured(self):
        self.put("orchestrator", NOW - 5, seven=(10, SEVEN_RESET), five=(96, FIVE_RESET))
        d = self.decide(five_hour_pct=95.0)
        self.assertTrue(d.suppress)
        self.assertEqual(d.window_key, f"five_hour:{FIVE_RESET}")

    def test_five_hour_expired_fails_open_when_configured(self):
        self.put("orchestrator", NOW - 5, seven=(10, SEVEN_RESET), five=(99, NOW - 1))
        self.assertFalse(self.decide(five_hour_pct=95.0).suppress)

    def test_five_hour_below_threshold_open_when_configured(self):
        self.put("orchestrator", NOW - 5, seven=(10, SEVEN_RESET), five=(80, FIVE_RESET))
        self.assertFalse(self.decide(five_hour_pct=95.0).suppress)


class ConfigTest(_TmpDirCase):
    def test_defaults(self):
        cfg = fug.config_from_env({})
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.seven_day_pct, 95.0)
        self.assertIsNone(cfg.five_hour_pct)
        self.assertEqual(cfg.dir, Path(fug.DEFAULT_DIR))
        self.assertTrue(cfg.schedule_enabled)

    def test_env_overrides(self):
        cfg = fug.config_from_env({
            fug.ENV_SEVEN_DAY_PCT: "90", fug.ENV_FIVE_HOUR_PCT: "97.5",
            fug.ENV_DIR: "/x/y", fug.ENV_ENABLED: "1",
            fug.ENV_SCHEDULE_ENABLED: "0"})
        self.assertEqual(cfg.seven_day_pct, 90.0)
        self.assertEqual(cfg.five_hour_pct, 97.5)
        self.assertEqual(cfg.dir, Path("/x/y"))
        self.assertFalse(cfg.schedule_enabled)

    def test_malformed_env_falls_back_to_defaults(self):
        cfg = fug.config_from_env({fug.ENV_SEVEN_DAY_PCT: "lots",
                                   fug.ENV_FIVE_HOUR_PCT: "nan",
                                   fug.ENV_DIR: ""})
        self.assertEqual(cfg.seven_day_pct, 95.0)
        self.assertIsNone(cfg.five_hour_pct)
        self.assertEqual(cfg.dir, Path(fug.DEFAULT_DIR))

    def test_kill_switch(self):
        for off in ("0", "false", "OFF", "no"):
            self.assertFalse(fug.config_from_env({fug.ENV_ENABLED: off}).enabled, off)
        self.put("orchestrator", NOW - 5, seven=(99, SEVEN_RESET))
        self.assertFalse(self.decide(enabled=False).suppress)

    def test_configured_threshold_is_honoured(self):
        self.put("orchestrator", NOW - 5, seven=(91, SEVEN_RESET))
        self.assertFalse(self.decide().suppress)
        self.assertTrue(self.decide(seven_day_pct=90.0).suppress)

    def test_evaluate_reads_config_from_env_when_not_given(self):
        self.put("orchestrator", NOW - 5, seven=(99, SEVEN_RESET))
        old = os.environ.get(fug.ENV_DIR)
        os.environ[fug.ENV_DIR] = str(self.dir)
        try:
            self.assertTrue(fug.evaluate(NOW).suppress)
        finally:
            if old is None:
                os.environ.pop(fug.ENV_DIR, None)
            else:
                os.environ[fug.ENV_DIR] = old


class LogOnceTest(_TmpDirCase):
    def test_logs_once_per_window_not_once_per_tick(self):
        sd = Path(self._tmp.name) / "state"
        key = f"seven_day:{SEVEN_RESET}"
        self.assertTrue(fug.should_log_once("keep_going:orchestrator", key, sd))
        self.assertFalse(fug.should_log_once("keep_going:orchestrator", key, sd))
        self.assertFalse(fug.should_log_once("keep_going:orchestrator", key, sd))

    def test_a_new_window_logs_again(self):
        sd = Path(self._tmp.name) / "state"
        self.assertTrue(fug.should_log_once("keep_going:orchestrator", "seven_day:1", sd))
        self.assertTrue(fug.should_log_once("keep_going:orchestrator", "seven_day:2", sd))

    def test_consumers_are_independent(self):
        sd = Path(self._tmp.name) / "state"
        self.assertTrue(fug.should_log_once("keep_going:orchestrator", "w", sd))
        self.assertTrue(fug.should_log_once("activity_monitor:orchestrator:p1a", "w", sd))
        self.assertTrue(fug.should_log_once("activity_monitor:orchestrator:p1b", "w", sd))
        self.assertFalse(fug.should_log_once("activity_monitor:orchestrator:p1a", "w", sd))

    def test_unwritable_state_logs_every_time(self):
        blocker = Path(self._tmp.name) / "file"
        blocker.write_text("x", encoding="utf-8")
        # state_dir is a regular file => cannot mkdir under it
        self.assertTrue(fug.should_log_once("c", "w", blocker))
        self.assertTrue(fug.should_log_once("c", "w", blocker))

    def test_a_non_oserror_failure_logs_instead_of_raising(self):
        # Path.home() raises RuntimeError when no home can be resolved; the marker is
        # best-effort and must never raise into a consumer (it would fire the unit's alert).
        with unittest.mock.patch.object(Path, "home", side_effect=RuntimeError("no home")), \
             unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PANTHEON_STATE_DIR", None)
            self.assertTrue(fug.should_log_once("c", "w"))

    def test_hostile_consumer_key_stays_inside_state_dir(self):
        sd = Path(self._tmp.name) / "state"
        fug.should_log_once("../../evil", "w", sd)
        self.assertFalse((Path(self._tmp.name) / "evil.json").exists())
        self.assertTrue(any((sd / "fleet_usage_gate").iterdir()))

    def test_default_state_dir_honours_pantheon_state_dir(self):
        old = os.environ.get("PANTHEON_STATE_DIR")
        os.environ["PANTHEON_STATE_DIR"] = "/some/state"
        try:
            self.assertEqual(fug.default_state_dir(), Path("/some/state"))
        finally:
            if old is None:
                os.environ.pop("PANTHEON_STATE_DIR", None)
            else:
                os.environ["PANTHEON_STATE_DIR"] = old


class TripMailOnceTest(_TmpDirCase):
    def decision(self):
        return fug.GateDecision(
            True, "codex weekly usage 60% >= 50% (pre-midweek)",
            "codex:seven_day:123:pre-midweek",
        )

    def test_distinct_per_user_state_roots_share_one_fleet_receipt(self):
        sent = []
        env = {"PANTHEON_FLEET_USAGE_DIR": str(self.dir)}
        with unittest.mock.patch.dict(os.environ, env | {"PANTHEON_STATE_DIR": "/tmp/user-a"}):
            fug.notify_usage_trip_once(
                "artisan", self.decision(), lambda recipient, *_: sent.append(recipient))
        with unittest.mock.patch.dict(os.environ, env | {"PANTHEON_STATE_DIR": "/tmp/user-b"}):
            fug.notify_usage_trip_once(
                "artisan", self.decision(), lambda recipient, *_: sent.append(recipient))
        self.assertEqual(sent, ["orchestrator", "artisan"])
        self.assertTrue((self.dir / ".gate-state" / "trip-mail" / "artisan_orchestrator.json").is_file())

    def test_concurrent_callers_cannot_double_send(self):
        sent = []
        sent_lock = threading.Lock()
        start = threading.Barrier(3)

        def send(recipient, _subject, _body):
            time.sleep(0.02)  # widen the old unlocked due -> send -> record race
            with sent_lock:
                sent.append(recipient)

        def caller():
            start.wait()
            fug.notify_usage_trip_once(
                "artisan", self.decision(), send,
                state_dir=self.dir / "shared-trip-state",
            )

        threads = [threading.Thread(target=caller) for _ in range(2)]
        for thread in threads:
            thread.start()
        start.wait()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(sorted(sent), ["artisan", "orchestrator"])

    def test_component_creation_race_reopens_the_winner_directory(self):
        state = self.dir / "raced-state"
        real_mkdir = os.mkdir
        raced = False

        def mkdir_with_competitor(component, mode=0o777, *, dir_fd=None):
            nonlocal raced
            if component == state.name and not raced:
                raced = True
                real_mkdir(component, mode=mode, dir_fd=dir_fd)
                raise FileExistsError(component)
            return real_mkdir(component, mode=mode, dir_fd=dir_fd)

        sent = []
        with unittest.mock.patch.object(fug.os, "mkdir", side_effect=mkdir_with_competitor):
            fug.notify_usage_trip_once(
                "artisan", self.decision(), lambda recipient, *_: sent.append(recipient),
                state_dir=state,
            )
        self.assertTrue(raced)
        self.assertEqual(sent, ["orchestrator", "artisan"])

    def test_failed_send_is_not_recorded_and_retries(self):
        attempts = []

        def send(recipient, _subject, _body):
            attempts.append(recipient)
            if recipient == "artisan" and attempts.count("artisan") == 1:
                raise RuntimeError("one delivery fault")

        state = self.dir / "shared-trip-state"
        fug.notify_usage_trip_once("artisan", self.decision(), send, state_dir=state)
        fug.notify_usage_trip_once("artisan", self.decision(), send, state_dir=state)
        self.assertEqual(attempts, ["orchestrator", "artisan", "artisan"])

    def test_restrictive_umask_still_writes_group_readable_receipts(self):
        intermediate = self.dir / ".gate-state"
        state = intermediate / "trip-mail"
        old_umask = os.umask(0o077)
        try:
            fug.notify_usage_trip_once("artisan", self.decision(), lambda *_: None,
                                       state_dir=state)
        finally:
            os.umask(old_umask)
        self.assertEqual(stat.S_IMODE(intermediate.stat().st_mode), 0o2770)
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o2770)
        for receipt in (state / "artisan_orchestrator.json", state / "artisan_artisan.json"):
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o660)

    def test_prepositioned_temp_symlink_is_never_followed_or_sent_through(self):
        state = self.dir / "shared-trip-state"
        state.mkdir()
        victim = self.dir / "victim.txt"
        victim.write_text("KEEP", encoding="utf-8")
        key = self.decision().window_key
        digest = fug.hashlib.sha256(f"artisan\0orchestrator\0{key}".encode()).hexdigest()[:16]
        token = "ab" * 16
        temp = state / f".artisan_orchestrator.json.tmp.{os.getpid()}.{digest}.{token}"
        temp.symlink_to(victim)
        sent, logs = [], []
        with unittest.mock.patch.object(fug.secrets, "token_hex", return_value=token):
            fug.notify_usage_trip_once(
                "artisan", self.decision(), lambda recipient, *_: sent.append(recipient),
                log=logs.append, state_dir=state,
            )
        self.assertEqual(victim.read_text(encoding="utf-8"), "KEEP")
        self.assertNotIn("orchestrator", sent)
        self.assertTrue(any("FileExistsError" in line for line in logs), logs)

    def test_prepositioned_lock_and_receipt_symlinks_are_not_followed(self):
        for hostile_name in ("artisan_orchestrator.lock", "artisan_orchestrator.json"):
            with self.subTest(hostile_name=hostile_name):
                state = self.dir / hostile_name.replace(".", "-")
                state.mkdir()
                victim = self.dir / f"victim-{hostile_name.replace('.', '-')}"
                victim.write_text("KEEP", encoding="utf-8")
                (state / hostile_name).symlink_to(victim)
                sent, logs = [], []
                fug.notify_usage_trip_once(
                    "artisan", self.decision(), lambda recipient, *_: sent.append(recipient),
                    log=logs.append, state_dir=state,
                )
                self.assertEqual(victim.read_text(encoding="utf-8"), "KEEP")
                self.assertNotIn("orchestrator", sent)
                self.assertTrue(logs)

    def test_prepositioned_root_symlink_is_rejected_before_chmod_or_send(self):
        victim = self.dir / "victim-dir"
        victim.mkdir(mode=0o700)
        state = self.dir / "shared-trip-state"
        state.symlink_to(victim, target_is_directory=True)
        sent, logs = [], []
        fug.notify_usage_trip_once(
            "artisan", self.decision(), lambda recipient, *_: sent.append(recipient),
            log=logs.append, state_dir=state,
        )
        self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o700)
        self.assertEqual(sent, [])
        self.assertTrue(logs)

    def test_prepositioned_intermediate_symlink_is_never_traversed(self):
        victim = self.dir / "outside-fleet-root"
        victim.mkdir()
        intermediate = self.dir / ".gate-state"
        intermediate.symlink_to(victim, target_is_directory=True)
        sent, logs = [], []
        fug.notify_usage_trip_once(
            "artisan", self.decision(), lambda recipient, *_: sent.append(recipient),
            log=logs.append, state_dir=intermediate / "trip-mail",
        )
        self.assertEqual(list(victim.iterdir()), [])
        self.assertEqual(sent, [])
        self.assertTrue(logs)


MOD_DIR = REPO_ROOT / "tools" / "mods" / "fleet-usage"
CONTRACT_TS = MOD_DIR / "contract" / "fleet-usage-record.ts"


def load_contract():
    """The committed contract fixture: strict JSON after the LAST `export default` (the
    header comment in the file mentions the marker too)."""
    return json.loads(CONTRACT_TS.read_text(encoding="utf-8").rpartition("export default")[2])


class ModContractTest(_TmpDirCase):
    """The producer is the Claude Code mod in tools/mods/fleet-usage (the operator TG 10723).
    Its test asserts the mod PRODUCES the committed fixture from the live specialist shape;
    this side asserts the gate CONSUMES that same fixture. One file pins both halves."""

    def write_fixture(self, **overrides):
        doc = load_contract()
        doc.update(overrides)
        (self.dir / f"{doc['seat']}.json").write_text(json.dumps(doc) + "\n", encoding="utf-8")
        return doc

    def test_fixture_has_the_gates_schema_and_shape(self):
        doc = load_contract()
        self.assertEqual(doc["schema"], fug.SCHEMA)
        self.assertEqual(set(doc), {"schema", "ts", "seat", "seven_day", "five_hour"})
        self.assertEqual(set(doc["seven_day"]), {"used_percentage", "resets_at"})
        self.assertEqual(set(doc["five_hour"]), {"used_percentage", "resets_at"})

    def test_the_gate_reads_the_fixture_back(self):
        doc = self.write_fixture()
        now = doc["ts"] + 10
        readings = fug.load_readings(self.dir, now)
        self.assertEqual(readings["seven_day"].pct, 3)
        self.assertEqual(readings["seven_day"].resets_at, 1791738000)
        self.assertEqual(readings["seven_day"].seat, "specialist")
        self.assertEqual(readings["five_hour"].pct, 13)
        self.assertFalse(fug.evaluate(now, self.cfg()).suppress)

    def test_the_same_fixture_at_99_percent_suppresses(self):
        doc = self.write_fixture(seven_day={"used_percentage": 99, "resets_at": 1791738000})
        d = fug.evaluate(doc["ts"] + 10, self.cfg())
        self.assertTrue(d.suppress)
        self.assertEqual(d.reading.seat, "specialist")

    def test_the_fixture_expires_at_its_reset(self):
        doc = self.write_fixture(seven_day={"used_percentage": 99, "resets_at": 1791738000})
        self.assertFalse(fug.evaluate(1791738000, self.cfg()).suppress)
        self.assertTrue(doc["ts"] < 1791738000)

    def test_the_mod_observes_the_session_and_touches_no_prompt(self):
        # the operator TG 10725: the producer only reads rateLimits and writes the seat file.
        # T-4536 adds ONE more observe-only hook, turn.complete (the per-turn usage ledger:
        # token counts only, never content); still nothing on the prompt.
        import re
        src = (MOD_DIR / "hooks" / "register.ts").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"""\bon\(\s*['"]([^'"]+)['"]""", src),
                         ["session.measure", "turn.complete"])
        code = re.sub(r"//.*", "", src)           # comments may mention it; code may not
        self.assertNotIn("prompt", code.lower())
        self.assertNotIn("$.session.append", code)


class StatusCliTest(_TmpDirCase):
    def test_status_reports_decision_and_readings(self):
        self.put("orchestrator", NOW - 5, seven=(97, SEVEN_RESET), five=(13, FIVE_RESET))
        st = fug.status(NOW, self.cfg())
        self.assertTrue(st["suppress"])
        self.assertEqual(st["seven_day_threshold"], 95.0)
        self.assertIsNone(st["five_hour_threshold"])
        self.assertEqual(st["readings"]["seven_day"]["pct"], 97)
        self.assertEqual(st["readings"]["seven_day"]["seat"], "orchestrator")
        self.assertEqual(st["readings"]["seven_day"]["age_s"], 5)
        json.dumps(st)  # JSON-able

    def test_status_cli_runs_as_script_and_prints_json(self):
        self.put("orchestrator", int(__import__("time").time()) - 5,
                 seven=(97, int(__import__("time").time()) + 3600))
        env = dict(os.environ, **{fug.ENV_DIR: str(self.dir)})
        out = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tools" / "fleet_usage_gate.py"), "status"],
            capture_output=True, text=True, env=env, timeout=30, check=False)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(json.loads(out.stdout)["suppress"])


class RecoveryCarveOutIsStructuralTest(unittest.TestCase):
    """The recovery layer must not ride (or be silenced by) the usage gate. Pin it:
    none of the recovery-carve-out modules may reference the gate or the tap
    (sop/standard/cross-agent-dispatch-discipline.md, recovery carve-out INVARIANT)."""

    RECOVERY = ("ho_resumer.py", "restart_drain.py", "context_fill_monitor.py",
                "handoff_and_restart.sh", "verify_startup.sh", "cap_modal_reaper.py",
                "send_keys_submit.sh")

    def test_recovery_modules_do_not_reference_the_gate(self):
        for name in self.RECOVERY:
            path = REPO_ROOT / "tools" / name
            self.assertTrue(path.exists(), f"{name} moved? update this pin")
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("fleet_usage", text, name)
            self.assertNotIn("USAGE_GATE", text, name)


if __name__ == "__main__":
    unittest.main()
