"""Tests for tools/fleet_usage_agg.py (T-5962, durable period aggregates of the usage ledger).

Run from repo root:
    python3 -m pytest tools/test_fleet_usage_agg.py -q

Fixtures: rows are built from the committed pantheon-fleet-usage-turn/v1 contract row
(tools/mods/fleet-usage/contract/fleet-usage-turn-row.ts) with synthetic values; the history
golden is this reader's output on the committed fixture ledger
(tools/fixtures/fleet_usage/ledger), which holds raw day files only.
"""
from __future__ import annotations

import json
import os
import random
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from fractions import Fraction
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools import fleet_usage_agg as agg  # noqa: E402
from tools import fleet_usage_ledger as ful  # noqa: E402

CONTRACT_TS = REPO_ROOT / "tools" / "mods" / "fleet-usage" / "contract" / "fleet-usage-turn-row.ts"
FIX_LEDGER = REPO_ROOT / "tools" / "fixtures" / "fleet_usage" / "ledger"
HISTORY_GOLDEN = REPO_ROOT / "tools" / "fixtures" / "fleet_usage" / "history.golden.json"
FIX_NOW = 1791143600  # 2026-10-04T19:53:20Z, as in test_fleet_usage_ledger
SCRIPT = REPO_ROOT / "tools" / "fleet_usage_ledger.py"


def ts(text: str) -> int:
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())


BASE = json.loads(CONTRACT_TS.read_text(encoding="utf-8").rpartition("export default")[2])


def row(seat: str, t: int, *, model="claude-opus-5-5", harness="claude", agent_id=None,
        i=10, o=20, cr=300, cw=40, dur=1000, turn=None) -> dict:
    r = dict(BASE, seat=seat, ts=t, model=model, harness=harness, agent_id=agent_id,
             input_tokens=i, output_tokens=o, cache_read_input_tokens=cr,
             cache_creation_input_tokens=cw, duration_ms=dur,
             turn_id=turn or f"t{t}-{i}-{o}", session_id="s1")
    if harness == "codex":
        r["reasoning_output_tokens"] = 0
    return r


def write_day(seat_dir: Path, day: str, rows) -> Path:
    seat_dir.mkdir(parents=True, exist_ok=True)
    path = seat_dir / f"{day}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def random_rows(rng: random.Random, n: int) -> list:
    out = []
    for _ in range(n):
        out.append({"input_tokens": rng.randint(0, 10**6), "output_tokens": rng.randint(0, 10**5),
                    "cache_read_input_tokens": rng.randint(0, 10**7),
                    "cache_creation_input_tokens": rng.randint(0, 10**6),
                    "duration_ms": None if rng.random() < 0.2 else rng.randint(0, 10**6)})
    return out


def cell_of(rows) -> list:
    c = agg.empty_cell()
    for r in rows:
        agg.cell_add_row(c, r)
    return c


class Tmp(unittest.TestCase):
    seat = "specialist"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.seat_dir = self.dir / "turns" / self.seat
        self.env = {"PANTHEON_FLEET_USAGE_SEAT": self.seat}

    def doc(self) -> dict:
        return json.loads((self.seat_dir / agg.AGG_FILE).read_text())


# ------------------------------------------------------------------------------- merge math


class MergeMathTests(unittest.TestCase):
    def test_merge_of_any_partition_equals_the_direct_cell(self):
        rng = random.Random(5962)
        for _ in range(200):
            rows = random_rows(rng, rng.randint(0, 40))
            direct = cell_of(rows)
            cuts = sorted(rng.sample(range(len(rows) + 1), min(len(rows) + 1, rng.randint(1, 5))))
            parts, prev = [], 0
            for c in cuts + [len(rows)]:
                parts.append(cell_of(rows[prev:c]))
                prev = c
            rng.shuffle(parts)
            self.assertEqual(agg.merge_cells(*parts), direct)
            self.assertTrue(agg.valid_cell(direct))

    def test_merge_is_associative_and_commutative(self):
        rng = random.Random(7)
        for _ in range(100):
            a, b, c = (cell_of(random_rows(rng, rng.randint(0, 8))) for _ in range(3))
            self.assertEqual(agg.merge_cells(agg.merge_cells(a, b), c),
                             agg.merge_cells(a, agg.merge_cells(b, c)))
            self.assertEqual(agg.merge_cells(a, b), agg.merge_cells(b, a))

    def test_variance_equals_chan_parallel_update_exactly(self):
        rng = random.Random(11)
        for _ in range(100):
            xs = [rng.randint(0, 10**9) for _ in range(rng.randint(2, 30))]
            k = rng.randint(1, len(xs) - 1)
            m2 = Fraction(0)
            n, mean = 0, Fraction(0)
            for part in (xs[:k], xs[k:]):  # Chan et al. pairwise combination with Fractions
                nb = len(part)
                mb = Fraction(sum(part), nb)
                m2b = sum((Fraction(x) - mb) ** 2 for x in part)
                delta = mb - mean
                m2 = m2 + m2b + delta * delta * n * nb / (n + nb)
                mean = mean + delta * nb / (n + nb)
                n += nb
            cell = cell_of([{"output_tokens": x} for x in xs])
            b = 1 + 5 * agg.FIELDS.index("output_tokens")
            cn, s, q = cell[b:b + 3]
            self.assertEqual(Fraction(cn * q - s * s, cn * (cn - 1)), m2 / (n - 1))
            st = agg.field_stats(cell, "output_tokens")
            self.assertAlmostEqual(st["var"], float(m2 / (n - 1)), delta=abs(float(m2)) * 1e-12 + 1e-9)

    def test_null_duration_counts_a_turn_but_not_the_field(self):
        c = cell_of([{"output_tokens": 5, "duration_ms": None}, {"output_tokens": 7, "duration_ms": 3}])
        self.assertEqual(c[0], 2)
        self.assertEqual(agg.field_stats(c, "duration_ms")["n"], 1)
        self.assertEqual(agg.field_stats(c, "output_tokens")["mean"], 6)
        self.assertIsNone(agg.field_stats(agg.empty_cell(), "output_tokens")["mean"])

    def test_large_values_round_trip_through_json_exactly(self):
        c = cell_of([{"cache_read_input_tokens": 10**12}] * 3)
        self.assertEqual(json.loads(json.dumps(c)), c)
        self.assertTrue(agg.valid_cell(json.loads(json.dumps(c))))

    def test_invalid_cells_are_rejected(self):
        good = cell_of([{"output_tokens": 3}])
        for bad in ([], good[:-1], [True] + good[1:], [-1] + good[1:],
                    [0] + good[1:]):  # n > turns
            self.assertFalse(agg.valid_cell(bad), bad)


# ------------------------------------------------------------------------------- alignment


class AlignmentTests(unittest.TestCase):
    def test_week_boundary_is_sunday_1700z(self):
        before, at = ts("2026-10-18T16:59:59Z"), ts("2026-10-18T17:00:00Z")
        self.assertEqual(date(2026, 10, 18).weekday(), 6)
        self.assertEqual(agg.week_start(before), ts("2026-10-11T17:00:00Z"))
        self.assertEqual(agg.week_start(at), at)
        self.assertEqual(agg.period_bounds("week", at), (at, at + agg.WEEK))

    def test_h6_grid_starts_on_the_quota_week(self):
        hours = {datetime.fromtimestamp(agg.h6_start(t), timezone.utc).hour
                 for t in range(ts("2026-10-01T00:00:00Z"), ts("2026-10-09T00:00:00Z"), 1800)}
        self.assertEqual(hours, {5, 11, 17, 23})
        ws = agg.week_start(ts("2026-10-20T00:00:00Z"))
        self.assertEqual(len(agg.series_spans("h6", ws, ws + agg.WEEK)), 28)

    def test_w_cell_splits_at_a_quarter_boundary(self):
        s, e = agg.w_span(ts("2026-12-30T00:00:00Z"))
        self.assertEqual((s, e), (ts("2026-12-27T17:00:00Z"), ts("2027-01-01T00:00:00Z")))
        s2, e2 = agg.w_span(ts("2027-01-02T00:00:00Z"))
        self.assertEqual((s2, e2), (ts("2027-01-01T00:00:00Z"), ts("2027-01-03T17:00:00Z")))
        # a mid-quarter week is not split
        s3, e3 = agg.w_span(ts("2026-11-04T00:00:00Z"))
        self.assertEqual(e3 - s3, agg.WEEK)

    def test_series_bucket_counts(self):
        def count(period, at):
            s, e = agg.period_bounds(period, ts(at))
            return len(agg.series_spans(agg.PERIOD_TIERS[period][0], s, e))
        self.assertEqual(count("day", "2026-11-02T05:00:00Z"), 24)
        self.assertEqual(count("week", "2026-11-02T05:00:00Z"), 28)
        self.assertEqual(count("month", "2027-02-10T00:00:00Z"), 28)
        self.assertEqual(count("month", "2026-10-10T00:00:00Z"), 31)
        self.assertIn(count("quarter", "2026-11-10T00:00:00Z"), (14, 15))
        self.assertIn(count("year", "2027-06-01T00:00:00Z"), range(53, 58))
        # the w spans of a year tile it exactly
        s, e = agg.period_bounds("year", ts("2027-06-01T00:00:00Z"))
        spans = agg.series_spans("w", s, e)
        self.assertEqual((spans[0][0], spans[-1][1]), (s, e))
        self.assertTrue(all(a[1] == b[0] for a, b in zip(spans, spans[1:])))

    def test_calendar_bounds(self):
        self.assertEqual(agg.quarter_bounds(ts("2026-11-15T00:00:00Z")),
                         (ts("2026-10-01T00:00:00Z"), ts("2027-01-01T00:00:00Z")))
        self.assertEqual(agg.month_bounds(ts("2026-12-15T00:00:00Z")),
                         (ts("2026-12-01T00:00:00Z"), ts("2027-01-01T00:00:00Z")))
        self.assertEqual(agg.year_bounds(ts("2027-03-01T00:00:00Z")),
                         (ts("2027-01-01T00:00:00Z"), ts("2028-01-01T00:00:00Z")))


# ------------------------------------------------------------------------------- fold


class FoldTests(Tmp):
    TODAY = date(2026, 11, 2)
    NOW = ts("2026-11-02T04:41:00Z")

    def setUp(self):
        super().setUp()
        write_day(self.seat_dir, "2026-10-30", [row("specialist", ts("2026-10-30T10:15:00Z")),
                                                row("specialist", ts("2026-10-30T10:45:00Z"), agent_id="a1"),
                                                row("specialist", ts("2026-10-31T00:00:01Z"))])
        write_day(self.seat_dir, "2026-11-01", [row("specialist", ts("2026-11-01T23:30:00Z"), o=99)])
        write_day(self.seat_dir, "2026-11-02", [row("specialist", ts("2026-11-02T01:00:00Z"))])

    def fold(self, commit=True):
        return agg.fold(self.dir, "specialist", commit=commit, today=self.TODAY, env=self.env, now=self.NOW)

    def test_folds_complete_days_only_with_hourly_cells(self):
        out = self.fold()
        self.assertEqual(out["folded"], ["2026-10-30.jsonl", "2026-11-01.jsonl"])
        doc = self.doc()
        self.assertNotIn("2026-11-02", doc["days"])
        rec = doc["days"]["2026-10-30"]
        self.assertEqual(rec["rows"], 2)
        self.assertEqual(rec["skipped"]["off_day"], 1)
        keys = sorted(rec["h1"])
        self.assertEqual([agg.parse_key(k)["agent_class"] for k in keys], ["main", "sub"])
        hour = str(ts("2026-10-30T10:00:00Z"))
        self.assertTrue(all(list(rec["h1"][k]) == [hour] for k in keys))

    def test_fold_twice_is_a_byte_identical_no_op(self):
        self.fold()
        first = (self.seat_dir / agg.AGG_FILE).read_bytes()
        out = agg.fold(self.dir, "specialist", commit=True, today=self.TODAY, env=self.env,
                       now=self.NOW + 3600)
        self.assertEqual(out["unchanged"], 2)
        self.assertFalse(out["changed"])
        self.assertEqual((self.seat_dir / agg.AGG_FILE).read_bytes(), first)

    def test_a_changed_day_is_refolded_by_replacement(self):
        self.fold()
        # the hourly codex export rewrites a day; a late append changes it too
        write_day(self.seat_dir, "2026-11-01", [row("specialist", ts("2026-11-01T23:30:00Z"), o=7),
                                                row("specialist", ts("2026-11-01T22:00:00Z"), o=8)])
        out = self.fold()
        self.assertEqual(out["refolded"], ["2026-11-01.jsonl"])
        rec = self.doc()["days"]["2026-11-01"]
        total = agg.merge_cells(*[c for hours in rec["h1"].values() for c in hours.values()])
        self.assertEqual(agg.field_sum(total, "output_tokens"), 15)
        self.assertEqual(total[0], 2)

    def test_dry_run_writes_nothing(self):
        out = self.fold(commit=False)
        self.assertEqual(len(out["folded"]), 2)
        self.assertFalse((self.seat_dir / agg.AGG_FILE).exists())
        self.assertFalse((self.seat_dir / agg.LOCK_FILE).exists())

    def test_file_mode_and_reserved_names_are_skipped_by_readers(self):
        self.fold()
        self.assertEqual(stat.S_IMODE(os.stat(self.seat_dir / agg.AGG_FILE).st_mode), 0o644)
        result = ful.read_rows(self.dir)
        self.assertEqual(result.skipped["bad_file"], 0)
        self.assertEqual(len(result.rows), 5)

    def test_refuses_another_seat_and_a_symlinked_seat_dir(self):
        with self.assertRaises(ful.PruneRefused):
            agg.fold(self.dir, "orchestrator", commit=True, today=self.TODAY, env=self.env)
        real = self.dir / "elsewhere"
        real.mkdir()
        link_root = self.dir / "linked"
        (link_root / "turns").mkdir(parents=True)
        os.symlink(real, link_root / "turns" / "specialist")
        with self.assertRaises(ful.PruneRefused):
            agg.fold(link_root, "specialist", commit=True, today=self.TODAY, env=self.env)
        self.assertEqual(os.listdir(real), [])

    def test_missing_seat_dir_is_no_data(self):
        fresh = self.dir / "fresh-root"  # under the setUp TemporaryDirectory (tempfile hygiene lint)
        fresh.mkdir()
        out = agg.fold(fresh, "specialist", commit=True, today=self.TODAY, env=self.env)
        self.assertEqual(out["status"], "no-data")
        p = ful.prune(fresh, "specialist", 35, commit=True, today=self.TODAY, env=self.env)
        self.assertEqual(p["status"], "no-data")
        self.assertEqual(p["deleted"], [])

    def test_an_invalid_aggregate_is_never_overwritten(self):
        (self.seat_dir / agg.AGG_FILE).write_text("{not json")
        with self.assertRaises(agg.AggregateError):
            self.fold()
        with self.assertRaises(agg.AggregateError):
            ful.prune(self.dir, "specialist", 35, commit=True, today=date(2027, 1, 1), env=self.env)
        self.assertEqual((self.seat_dir / agg.AGG_FILE).read_text(), "{not json")
        self.assertTrue((self.seat_dir / "2026-10-30.jsonl").exists())

    def test_another_seats_aggregate_is_refused(self):
        self.fold()
        doc = self.doc()
        doc["seat"] = "orchestrator"
        (self.seat_dir / agg.AGG_FILE).write_text(json.dumps(doc))
        with self.assertRaises(agg.AggregateError):
            self.fold()


# ------------------------------------------------------------------------------- prune


class PruneGuardTests(Tmp):
    TODAY = date(2026, 12, 15)
    NOW = ts("2026-12-15T04:41:00Z")

    def setUp(self):
        super().setUp()
        self.rows = {
            "2026-10-01": [row("specialist", ts("2026-10-01T10:00:00Z"), o=1),
                           row("specialist", ts("2026-10-01T23:10:00Z"), o=2, agent_id="x")],
            "2026-10-02": [row("specialist", ts("2026-10-02T03:00:00Z"), o=4)],
            "2026-12-14": [row("specialist", ts("2026-12-14T12:00:00Z"), o=8)],
        }
        for day, rows in self.rows.items():
            write_day(self.seat_dir, day, rows)

    def prune(self, commit=True, today=None):
        return ful.prune(self.dir, "specialist", 35, commit=commit, today=today or self.TODAY,
                         env=self.env, now=self.NOW)

    def history_total(self, period, at):
        doc = agg.history(self.dir, period, at=at, by=("seat",), now=self.NOW)
        groups = doc["periods"][0]["groups"]
        return groups[0]["totals"] if groups else None

    def test_prune_folds_seals_then_deletes(self):
        before = {p: self.history_total(p, "2026-10-01") for p in ("day", "week", "month", "quarter", "year")}
        out = self.prune()
        self.assertEqual(out["deleted"], ["2026-10-01.jsonl", "2026-10-02.jsonl"])
        self.assertEqual(out["sealed"], out["deleted"])
        self.assertEqual(out["refused"], [])
        doc = self.doc()
        self.assertIsNotNone(doc["days"]["2026-10-01"]["sealed_at"])
        self.assertIsNone(doc["days"]["2026-12-14"]["sealed_at"])
        after = {p: self.history_total(p, "2026-10-01") for p in before}
        self.assertEqual(before, after)  # sealing is exact: no period changes
        self.assertEqual(after["month"]["output_tokens"], 7)

    def test_dry_run_reports_and_writes_nothing(self):
        out = self.prune(commit=False)
        self.assertEqual(out["would_delete"], ["2026-10-01.jsonl", "2026-10-02.jsonl"])
        self.assertFalse((self.seat_dir / agg.AGG_FILE).exists())
        self.assertEqual(len([n for n in os.listdir(self.seat_dir) if n.endswith(".jsonl")]), 3)

    def test_crash_between_seal_and_delete_never_double_counts(self):
        real_unlink = os.unlink

        def boom(path, *a, **k):
            if str(path).endswith(".jsonl"):
                raise OSError("simulated crash")
            return real_unlink(path, *a, **k)

        with mock.patch.object(agg.os, "unlink", boom), self.assertRaises(OSError):
            self.prune()
        self.assertIsNotNone(self.doc()["days"]["2026-10-01"]["sealed_at"])
        self.assertTrue((self.seat_dir / "2026-10-01.jsonl").exists())
        out = self.prune()
        self.assertEqual(out["deleted"], ["2026-10-01.jsonl", "2026-10-02.jsonl"])
        self.assertEqual(out["sealed"], [])
        self.assertEqual(self.history_total("month", "2026-10-01")["output_tokens"], 7)
        d1 = self.doc()["sealed"]["d1"]
        self.assertEqual(sum(c[0] for cells in d1.values() for c in cells.values()), 3)

    def test_a_sealed_day_whose_file_changed_is_kept_and_refused(self):
        with mock.patch.object(agg.os, "unlink", side_effect=OSError("crash")), \
                self.assertRaises(OSError):
            self.prune()
        write_day(self.seat_dir, "2026-10-01", [row("specialist", ts("2026-10-01T10:00:00Z"), o=1000)])
        out = self.prune()
        self.assertIn("2026-10-01.jsonl", out["refused"])
        self.assertEqual(out["refused_reasons"]["2026-10-01.jsonl"], "changed after seal")
        self.assertEqual(out["fold"]["sealed_changed"], ["2026-10-01.jsonl"])
        self.assertTrue((self.seat_dir / "2026-10-01.jsonl").exists())
        self.assertEqual(self.history_total("month", "2026-10-01")["output_tokens"], 7)

    def test_a_day_with_off_day_rows_is_refused_not_deleted(self):
        """T-5962 review LOW-1: a row dated outside its file's day is never folded, so the
        file must survive prune or the row is lost from every view."""
        write_day(self.seat_dir, "2026-10-02", [row("specialist", ts("2026-10-02T09:00:00Z"), o=4),
                                                row("specialist", ts("2026-10-03T01:00:00Z"), o=500)])
        out = self.prune()
        self.assertEqual(out["refused_reasons"], {"2026-10-02.jsonl": "off-day rows"})
        self.assertEqual(out["deleted"], ["2026-10-01.jsonl"])
        self.assertTrue((self.seat_dir / "2026-10-02.jsonl").exists())
        self.assertEqual(self.prune()["refused_reasons"], {"2026-10-02.jsonl": "off-day rows"})

    def test_an_unreadable_old_day_is_refused_not_deleted(self):
        if os.getuid() == 0:
            self.skipTest("root reads mode-000 files")
        path = self.seat_dir / "2026-10-02.jsonl"
        os.chmod(path, 0)
        try:
            out = self.prune()
        finally:
            os.chmod(path, 0o644)
        self.assertEqual(out["deleted"], ["2026-10-01.jsonl"])
        self.assertEqual(out["refused_reasons"], {"2026-10-02.jsonl": "not folded"})
        self.assertTrue(path.exists())

    def test_cli_exit_codes(self):
        env = dict(os.environ, PANTHEON_FLEET_USAGE_SEAT="specialist")
        path = self.seat_dir / "2026-10-02.jsonl"
        base = [sys.executable, str(SCRIPT), "--dir", str(self.dir), "--now", str(self.NOW)]
        if os.getuid() != 0:
            os.chmod(path, 0)
            try:
                out = subprocess.run(base + ["prune", "--seat", "specialist", "--older-than-days", "35",
                                             "--commit"], capture_output=True, text=True,
                                     timeout=60, env=env)
            finally:
                os.chmod(path, 0o644)
            self.assertEqual(out.returncode, 3, out.stderr)
            self.assertIn("REFUSED 2026-10-02.jsonl", out.stderr)
        out = subprocess.run(base + ["prune", "--seat", "specialist", "--older-than-days", "35",
                                     "--commit"], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        (self.seat_dir / agg.AGG_FILE).write_text("garbage")
        out = subprocess.run(base + ["fold", "--seat", "specialist", "--commit"], capture_output=True,
                             text=True, timeout=60, env=env)
        self.assertEqual(out.returncode, 2)
        self.assertIn("fold refused", out.stderr)

    def test_retention_trims_hourly_and_six_hour_cells_only(self):
        self.prune()
        later = date(2028, 1, 1)
        write_day(self.seat_dir, "2027-12-30", [row("specialist", ts("2027-12-30T10:00:00Z"))])
        ful.prune(self.dir, "specialist", 35, commit=True, today=later, env=self.env,
                  now=ts("2028-01-01T04:41:00Z"))
        doc = self.doc()
        self.assertEqual(doc["days"]["2026-10-01"]["h1"], {})
        h6_starts = [int(s) for cells in doc["sealed"]["h6"].values() for s in cells]
        self.assertTrue(all(s >= ts("2026-12-01T00:00:00Z") for s in h6_starts))
        self.assertEqual(self.history_total("month", "2026-10-01")["output_tokens"], 7)
        self.assertEqual(self.history_total("quarter", "2026-10-01")["output_tokens"], 15)
        hist = agg.history(self.dir, "day", at="2026-10-01", series=True,
                           now=ts("2028-01-01T05:00:00Z"))
        series = hist["periods"][0]["groups"][0]["series"]
        self.assertIsNone(series["buckets"])
        self.assertIn("expired", series["reason"])


# ------------------------------------------------------------------------------- views


class HistoryTests(Tmp):
    NOW = ts("2026-11-20T12:00:00Z")

    def setUp(self):
        super().setUp()
        self.orchestrator = self.dir / "turns" / "orchestrator"
        self.consultant = self.dir / "turns" / "consultant"
        write_day(self.seat_dir, "2026-11-16", [
            row("specialist", ts("2026-11-16T09:00:00Z"), o=100, i=1000),
            row("specialist", ts("2026-11-16T09:30:00Z"), o=300, i=10, agent_id="sub1"),
            row("specialist", ts("2026-11-16T10:00:00Z"), model="claude-haiku-5-5", o=5)])
        write_day(self.orchestrator, "2026-11-17", [
            row("orchestrator", ts("2026-11-17T11:00:00Z"), model="claude-fable-5-1", o=50)])
        write_day(self.consultant, "2026-11-17", [
            row("consultant", ts("2026-11-17T12:00:00Z"), model="gpt-5.6-sol", harness="codex",
                o=10**6)])

    def test_matches_the_raw_rollup(self):
        agg.fold(self.dir, "specialist", commit=True, env=self.env, now=self.NOW)
        doc = agg.history(self.dir, "week", at="2026-11-18", by=("seat", "model"), now=self.NOW)
        groups = {(g["key"]["seat"], g["key"]["model"]): g for g in doc["periods"][0]["groups"]}
        raw = ful.rollup(ful.read_rows(self.dir).rows, ("seat", "model"))
        self.assertEqual(len(raw), len(groups))
        for r in raw:
            g = groups[(r["key"]["seat"], r["key"]["model"])]
            self.assertEqual(g["turns"], r["turns"])
            for k, v in r["tokens"].items():
                self.assertEqual(g["totals"][k], v)
            self.assertEqual(g["units"], r["units"])

    def test_codex_is_never_in_the_claude_share_and_usd_has_reasons(self):
        doc = agg.history(self.dir, "week", at="2026-11-18", by=("seat", "model"), now=self.NOW)
        p = doc["periods"][0]
        g = {(x["key"]["seat"], x["key"]["model"]): x for x in p["groups"]}
        codex = g[("consultant", "gpt-5.6-sol")]
        self.assertIsNone(codex["units"])
        self.assertIsNone(codex["share_of_claude_pool"])
        self.assertIsNone(codex["usd_est"])
        self.assertIn("OpenAI pool", codex["share_note"])
        self.assertIn("OpenAI pool", codex["usd_unpriced"]["gpt-5.6-sol"])
        claude_units = sum(x["units"] for x in p["groups"] if x["units"] is not None)
        self.assertAlmostEqual(p["pool"]["claude_units"], claude_units, places=2)
        shares = sum(x["share_of_claude_pool"] for x in p["groups"]
                     if x["share_of_claude_pool"] is not None)
        self.assertAlmostEqual(shares, 1.0, places=5)
        haiku = g[("specialist", "claude-haiku-5-5")]
        self.assertIsNone(haiku["usd_est"])
        self.assertIn("two rate cards", haiku["usd_unpriced"]["claude-haiku-5-5"])
        opus = g[("specialist", "claude-opus-5-5")]
        self.assertAlmostEqual(opus["usd_est"], opus["units"] * 4.0 / 1e6, places=6)
        self.assertIn("ESTIMATE", doc["estimate_note"])

    def test_group_by_agent_class_and_filters(self):
        doc = agg.history(self.dir, "week", at="2026-11-18", by=("agent_class",),
                          seats={"specialist"}, harness="claude", now=self.NOW)
        g = {x["key"]["agent_class"]: x for x in doc["periods"][0]["groups"]}
        self.assertEqual(g["sub"]["totals"]["output_tokens"], 300)
        self.assertEqual(g["main"]["turns"], 2)
        # the pool stays fleet-wide even under a seat filter
        self.assertLess(sum(x["share_of_claude_pool"] for x in g.values()), 1.0)

    def test_series_and_bucket_total_stats(self):
        doc = agg.history(self.dir, "day", at="2026-11-16", by=("seat",), seats={"specialist"},
                          series=True, now=self.NOW)
        s = doc["periods"][0]["groups"][0]["series"]
        self.assertEqual(len(s["buckets"]), 24)
        self.assertEqual(s["buckets"][9]["totals"]["output_tokens"], 400)
        self.assertEqual(s["buckets_counted"], 24)
        st = s["bucket_total_stats"]["output_tokens"]
        self.assertEqual(st["n"], 24)
        self.assertEqual(st["sum"], 405)
        self.assertAlmostEqual(st["mean"], 405 / 24)

    def test_last_n_periods_and_markers(self):
        doc = agg.history(self.dir, "week", at="2026-11-18", last=3, now=self.NOW)
        self.assertEqual(len(doc["periods"]), 3)
        self.assertEqual(doc["periods"][1]["end"], doc["periods"][2]["start"])
        labels = [m["label"] for m in doc["markers"]]
        self.assertIn("claude-fable-5-1 first seen", labels)
        self.assertIn("specialist first seen", labels)
        fable = next(m for m in doc["markers"] if m["label"] == "claude-fable-5-1 first seen")
        self.assertEqual(fable["ts"], ts("2026-11-17T11:00:00Z"))  # hour, not day, resolution

    def test_unknown_period_and_dimension_are_value_errors(self):
        with self.assertRaises(ValueError):
            agg.history(self.dir, "fortnight", now=self.NOW)
        with self.assertRaises(ValueError):
            agg.history(self.dir, "day", by=("worker",), now=self.NOW)

    def test_an_invalid_foreign_aggregate_is_reported_not_raised(self):
        (self.orchestrator / agg.AGG_FILE).write_text("[]")
        doc = agg.history(self.dir, "week", at="2026-11-18", now=self.NOW)
        self.assertIn("invalid", doc["seats"]["orchestrator"]["aggregate"])
        self.assertIn("orchestrator", [g["key"]["seat"] for g in doc["periods"][0]["groups"]])


class PricingTests(unittest.TestCase):
    def test_longest_key_wins(self):
        self.assertEqual(ful.input_usd_per_mtok("claude-opus-5-5")[0], 4.0)
        self.assertEqual(ful.input_usd_per_mtok("claude-opus-5-5[1m]")[0], 4.0)
        self.assertEqual(ful.input_usd_per_mtok("claude-opus-5")[0], 5.0)
        self.assertEqual(ful.input_usd_per_mtok("claude-sonnet-5-5")[0], 2.0)
        self.assertEqual(ful.input_usd_per_mtok("claude-mythos-5-1")[0], 10.0)
        self.assertIsNone(ful.input_usd_per_mtok("claude-opus-5-50")[0])
        self.assertIsNone(ful.input_usd_per_mtok("claude-opus-5-6")[0])  # unknown sibling
        self.assertEqual(ful.input_usd_per_mtok("claude-opus-4-8-20260101")[0], 5.0)
        self.assertIsNone(ful.input_usd_per_mtok("gpt-5.6-sol")[0])
        self.assertIsNotNone(ful.input_usd_per_mtok("claude-haiku-5-5")[1])

    def test_the_newest_pricing_marker_pins_the_price_table(self):
        markers = [m for m in agg.load_markers() if m["kind"] == "pricing"]
        self.assertTrue(markers)
        newest = max(markers, key=lambda m: m["ts"])
        self.assertEqual(newest.get("pricing_fingerprint"), ful.pricing_fingerprint(),
                         "weights or price table changed: append a 'pricing' marker to "
                         "tools/fleet_usage_markers.json carrying ful.pricing_fingerprint()")

    def test_markers_file_is_well_formed(self):
        markers = agg.load_markers()
        self.assertTrue(all(m["scope"] and m["ref"] for m in markers))

    def test_malformed_markers_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.json"
            p.write_text(json.dumps({"schema": agg.MARKERS_SCHEMA,
                                     "markers": [{"at": "2026-11-02", "scope": "fleet",
                                                  "kind": "weird", "label": "x", "ref": "T-1"}]}))
            with self.assertRaises(ValueError):
                agg.load_markers(p)


class GoldenTests(unittest.TestCase):
    def test_cli_history_json_equals_golden(self):
        out = subprocess.run(
            [sys.executable, str(SCRIPT), "--dir", str(FIX_LEDGER), "--json", "--now",
             str(FIX_NOW), "history", "--period", "week", "--by", "seat,model", "--series"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), json.loads(HISTORY_GOLDEN.read_text()))

    def test_cli_text_history_and_markers(self):
        for argv in (["history", "--period", "day"], ["markers"]):
            out = subprocess.run([sys.executable, str(SCRIPT), "--dir", str(FIX_LEDGER), "--now",
                                  str(FIX_NOW)] + argv, capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("pricing", out.stdout)


if __name__ == "__main__":
    unittest.main()
