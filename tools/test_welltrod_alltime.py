#!/usr/bin/env python3
"""Tests for welltrod_alltime.py and its welltrod_stats.py wiring (T-4490).

Hermetic: tmpdir repo roots and fabricated session reports. No sudo, no real
transcripts, no real knowledge/stats/ tree, no network.

Run: python3 -m unittest tools.test_welltrod_alltime -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import welltrod_alltime as wa
from tools import welltrod_stats as ws
from tools.test_welltrod_stats import fake_report, make_gov_repo

DAY = 86400


def rep(session_id, touches, mtime=None, handoff=True):
    r = fake_report(session_id, touches, handoff=handoff)
    if mtime is not None:
        r["mtime"] = mtime
    return r


def row(sid, doc, touches, agent="orchestrator", day="2026-09-30"):
    return {"session_id": sid, "agent": agent, "doc": doc, "touches": touches, "day": day}


def write_history(root, date, docs, window_days=14, agents=None):
    """docs: {path: (well_trod, {agent: (sessions, touches)})} or {path: well_trod}."""
    hist = Path(root) / wa.SNAPSHOT_DIR / "history"
    hist.mkdir(parents=True, exist_ok=True)
    out = []
    for path, spec in docs.items():
        if isinstance(spec, int):
            out.append({"path": path, "well_trod": spec, "touches": spec, "per_agent": {}})
        else:
            wt, per = spec
            out.append(
                {
                    "path": path,
                    "well_trod": wt,
                    "touches": sum(t for _, t in per.values()),
                    "per_agent": {a: {"sessions": s, "touches": t} for a, (s, t) in per.items()},
                }
            )
    snap = {
        "schema_version": 1,
        "generated_at": date + "T09:22:00Z",
        "window_days": window_days,
        "agents": agents or {"orchestrator": {"sessions_analyzed": 1}},
        "docs": out,
    }
    (hist / (date + ".json")).write_text(json.dumps(snap) + "\n")
    return snap


class RowsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_gov_repo(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rows_sum_raw_paths_that_normalize_to_one_key(self):
        reports = {
            "orchestrator": [
                rep(
                    "s1",
                    [
                        ("/home/claude/pantheon-config/sop/standard/alpha.md", 2),
                        ("/tmp/wt/sop/standard/alpha.md", 3),
                        ("sop/standard/beta.md", 1),
                    ],
                )
            ]
        }
        rows = wa.session_doc_rows(reports, str(self.root), "2026-10-01")
        got = {(r["session_id"], r["doc"]): r["touches"] for r in rows}
        self.assertEqual(
            got,
            {("s1", "sop/standard/alpha.md"): 5, ("s1", "sop/standard/beta.md"): 1},
        )
        self.assertTrue(all(r["agent"] == "orchestrator" for r in rows))

    def test_row_day_is_transcript_mtime_date_else_fallback(self):
        # 2026-09-06T12:00:00Z
        mtime = 1788696000
        reports = {
            "recovery": [
                rep("a", [("sop/standard/alpha.md", 1)], mtime=mtime),
                rep("b", [("sop/standard/alpha.md", 1)]),
            ]
        }
        by = {r["session_id"]: r["day"] for r in wa.session_doc_rows(reports, str(self.root), "2026-10-01")}
        self.assertEqual(by["a"], "2026-09-06")
        self.assertEqual(by["b"], "2026-10-01")

    def test_rows_agree_with_build_snapshot_per_doc_counts(self):
        reports = {
            "recovery": [rep("w1", [("sop/standard/alpha.md", 4)]), rep("w2", [("sop/standard/alpha.md", 1)])],
            "orchestrator": [rep("t1", [("sop/standard/alpha.md", 2), ("sop/standard/beta.md", 7)])],
        }
        snap = ws.build_snapshot(reports, str(self.root), 14)
        rows = wa.session_doc_rows(reports, str(self.root), "2026-10-01")
        for d in snap["docs"]:
            mine = [r for r in rows if r["doc"] == d["path"]]
            self.assertEqual(len({r["session_id"] for r in mine}), d["well_trod"], d["path"])
            self.assertEqual(sum(r["touches"] for r in mine), d["touches"], d["path"])


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = wa.store_path(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_store_lives_next_to_the_stats(self):
        self.assertEqual(self.path, self.root / wa.SNAPSHOT_DIR / wa.STORE_FILE)

    def test_first_ingest_appends_every_row(self):
        rows = [row("s1", "d1", 3), row("s1", "d2", 1), row("s2", "d1", 2)]
        self.assertEqual(wa.ingest(self.root, rows), 3)
        lines = self.path.read_text().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[0]), rows[0])

    def test_recounting_the_same_live_transcript_is_a_byte_identical_noop(self):
        rows = [row("s1", "d1", 3), row("s2", "d1", 2)]
        wa.ingest(self.root, rows)
        before = self.path.read_bytes()
        # the next daily run sees the same session again, same counts, later day
        again = [dict(r, day="2026-10-01") for r in rows]
        self.assertEqual(wa.ingest(self.root, again), 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_growth_appends_a_superseding_row_and_loader_takes_the_max(self):
        wa.ingest(self.root, [row("s1", "d1", 3)])
        self.assertEqual(wa.ingest(self.root, [row("s1", "d1", 5, day="2026-10-01")]), 1)
        self.assertEqual(len(self.path.read_text().splitlines()), 2)
        store = wa.load_store(self.path)
        self.assertEqual(store[("s1", "d1")]["touches"], 5)
        self.assertEqual(len(store), 1)

    def test_shrunken_or_equal_count_is_never_appended(self):
        wa.ingest(self.root, [row("s1", "d1", 5)])
        self.assertEqual(wa.ingest(self.root, [row("s1", "d1", 4), row("s1", "d1", 5)]), 0)

    def test_duplicates_inside_one_batch_collapse_to_the_highest(self):
        n = wa.ingest(self.root, [row("s1", "d1", 2), row("s1", "d1", 4), row("s1", "d1", 3)])
        self.assertEqual(n, 1)
        self.assertEqual(wa.load_store(self.path)[("s1", "d1")]["touches"], 4)

    def test_dedupe_key_is_session_and_doc_not_agent(self):
        wa.ingest(self.root, [row("s1", "d1", 2, agent="orchestrator")])
        self.assertEqual(wa.ingest(self.root, [row("s1", "d1", 2, agent="recovery")]), 0)

    def test_missing_store_loads_empty_and_ingest_of_nothing_creates_nothing(self):
        self.assertEqual(wa.load_store(self.path), {})
        self.assertEqual(wa.ingest(self.root, []), 0)
        self.assertFalse(self.path.exists())

    def test_malformed_lines_are_skipped_with_a_warning_not_a_crash(self):
        self.path.parent.mkdir(parents=True)
        good = json.dumps(row("s1", "d1", 2))
        self.path.write_text(good + "\nnot json at all\n[1,2]\n" + json.dumps({"session_id": "x"}) + "\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            store = wa.load_store(self.path)
        self.assertEqual(set(store), {("s1", "d1")})
        self.assertIn("alltime.jsonl", err.getvalue())

    def test_parseable_but_mistyped_rows_never_crash_the_run(self):
        # T-4490 review r1: these all parse as JSON objects with the right keys, so the
        # old guard let them through and the crash came later (a str/int comparison, an
        # unhashable dict key, int(inf)). A bad row is skipped with the usual warning; a
        # numeric string for `touches` is a recoverable count and is converted.
        good = json.dumps(row("s1", "d1", 2))
        cases = {
            "touches-as-string": '{"session_id":"s2","agent":"orchestrator","doc":"d1","touches":"5","day":"2026-09-30"}',
            "list-session_id": '{"session_id":["s3"],"agent":"orchestrator","doc":"d1","touches":1,"day":"2026-09-30"}',
            "dict-doc": '{"session_id":"s4","agent":"orchestrator","doc":{"x":1},"touches":1,"day":"2026-09-30"}',
            "touches-Infinity": '{"session_id":"s5","agent":"orchestrator","doc":"d1","touches":Infinity,"day":"2026-09-30"}',
            "touches-NaN": '{"session_id":"s6","agent":"orchestrator","doc":"d1","touches":NaN,"day":"2026-09-30"}',
            "touches-non-numeric-string": '{"session_id":"s7","agent":"orchestrator","doc":"d1","touches":"many","day":"2026-09-30"}',
            "touches-bool": '{"session_id":"s8","agent":"orchestrator","doc":"d1","touches":true,"day":"2026-09-30"}',
            "touches-list": '{"session_id":"s9","agent":"orchestrator","doc":"d1","touches":[1],"day":"2026-09-30"}',
        }
        for name, line in cases.items():
            with self.subTest(shape=name):
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(good + "\n" + line + "\n")
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    store = wa.load_store(self.path)
                    # the whole downstream chain must survive too, not just the loader
                    wa._merged(store, [row("sx", "d1", 1)])
                    wa.summarize_all_time(store)
                    wa.ingest(self.root, [row("s1", "d1", 1)])
                if name == "touches-as-string":
                    self.assertEqual(store[("s2", "d1")]["touches"], 5)
                    self.assertIsInstance(store[("s2", "d1")]["touches"], int)
                    self.assertEqual(err.getvalue(), "")
                else:
                    self.assertEqual(set(store), {("s1", "d1")})
                    self.assertIn("alltime.jsonl line 2", err.getvalue())

    def test_mistyped_optional_fields_are_coerced_not_fatal(self):
        # `agent` and `day` feed a dict key and a min(); a list or int there must not take
        # the summary down, and the (valid) count on the row is kept.
        self.path.parent.mkdir(parents=True)
        self.path.write_text(
            '{"session_id":"s1","agent":["orchestrator"],"doc":"d1","touches":3,"day":"2026-09-30"}\n'
            '{"session_id":"s2","agent":"orchestrator","doc":"d1","touches":2,"day":20260930}\n'
            '{"session_id":"s3","agent":"orchestrator","doc":"d1","touches":1,"day":"2026-09-01"}\n'
        )
        store = wa.load_store(self.path)
        self.assertEqual(set(store), {("s1", "d1"), ("s2", "d1"), ("s3", "d1")})
        out = wa.summarize_all_time(store)
        self.assertEqual(out["docs"][0]["touches"], 6)
        self.assertEqual(out["earliest_session_day"], "2026-09-01")
        self.assertEqual(out["docs"][0]["per_agent"]["?"]["sessions"], 1)

    def test_torn_final_line_is_repaired_before_append(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(row("s1", "d1", 2)))  # no trailing newline
        wa.ingest(self.root, [row("s2", "d1", 1)])
        lines = self.path.read_text().splitlines()
        self.assertEqual([json.loads(ln)["session_id"] for ln in lines], ["s1", "s2"])


class SummaryTest(unittest.TestCase):
    def test_all_time_sums_sessions_touches_and_splits_by_agent(self):
        store = {}
        for r in [
            row("t1", "docA", 3, agent="orchestrator", day="2026-09-02"),
            row("t2", "docA", 2, agent="orchestrator", day="2026-09-10"),
            row("w1", "docA", 1, agent="recovery", day="2026-09-20"),
            row("w1", "docB", 4, agent="recovery", day="2026-09-20"),
        ]:
            store[(r["session_id"], r["doc"])] = r
        out = wa.summarize_all_time(store)
        self.assertEqual(out["sessions_counted"], 3)
        self.assertEqual(out["earliest_session_day"], "2026-09-02")
        self.assertEqual(out["store"], wa.SNAPSHOT_DIR + "/" + wa.STORE_FILE)
        a, b = out["docs"]
        self.assertEqual((a["path"], a["sessions"], a["touches"]), ("docA", 3, 6))
        self.assertEqual(a["per_agent"], {"orchestrator": {"sessions": 2, "touches": 5}, "recovery": {"sessions": 1, "touches": 1}})
        self.assertEqual((b["path"], b["sessions"], b["touches"]), ("docB", 1, 4))

    def test_empty_store_is_a_renderable_empty_block(self):
        out = wa.summarize_all_time({})
        self.assertEqual(out["sessions_counted"], 0)
        self.assertIsNone(out["earliest_session_day"])
        self.assertEqual(out["docs"], [])

    def test_pre_store_estimate_is_reported_separately_and_labelled_approximate(self):
        store = {("t1", "docA"): row("t1", "docA", 3)}
        pre = {
            "approximate": True,
            "until": "2026-09-01",
            "tiles": [{"snapshot": "2026-09-01", "covers": ["2026-08-18", "2026-09-01"]}],
            "skipped": [],
            "caveat": "approx",
            "docs": [
                {"path": "docA", "sessions": 10, "touches": 20, "per_agent": {}},
                {"path": "docOld", "sessions": 4, "touches": 4, "per_agent": {}},
            ],
        }
        out = wa.summarize_all_time(store, pre_store=pre)
        by = {d["path"]: d for d in out["docs"]}
        # exact numbers untouched by the estimate
        self.assertEqual((by["docA"]["sessions"], by["docA"]["touches"]), (1, 3))
        self.assertEqual(by["docA"]["pre_store_sessions_est"], 10)
        self.assertEqual(by["docA"]["pre_store_touches_est"], 20)
        # a doc only the estimate knows about: exact 0, estimate carried
        self.assertEqual(by["docOld"]["sessions"], 0)
        self.assertEqual(by["docOld"]["pre_store_sessions_est"], 4)
        meta = out["pre_store_estimate"]
        self.assertTrue(meta["approximate"])
        self.assertEqual(meta["until"], "2026-09-01")
        self.assertEqual(meta["tiles"], ["2026-09-01"])
        # an estimate file written before per-agent counts existed has none to carry
        self.assertEqual(meta["tile_sessions_analyzed_by_agent"], {"2026-09-01": {}})

    def test_estimate_block_carries_who_was_measured_in_each_tile(self):
        # T-4490 r2: latest.json is what readers see; its caveat points at
        # sessions_analyzed_by_agent, so the numbers must be in the same file.
        pre = {
            "approximate": True,
            "until": "2026-09-01",
            "tiles": [
                {"snapshot": "2026-09-01", "sessions_analyzed_by_agent": {"specialist": 5, "recovery": 0}},
                {"snapshot": "2026-08-18", "sessions_analyzed_by_agent": {"specialist": 4, "recovery": 24}},
            ],
            "skipped": [],
            "caveat": "approx",
            "docs": [],
        }
        meta = wa.summarize_all_time({}, pre_store=pre)["pre_store_estimate"]
        self.assertEqual(meta["tiles"], ["2026-09-01", "2026-08-18"])
        self.assertEqual(
            meta["tile_sessions_analyzed_by_agent"],
            {"2026-09-01": {"specialist": 5, "recovery": 0}, "2026-08-18": {"specialist": 4, "recovery": 24}},
        )


class TrendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def by_path(self, trend):
        return {d["path"]: d for d in trend["docs"]}

    def test_now_vs_7_and_14_days_ago(self):
        write_history(self.root, "2026-09-23", {"a": 6, "b": 2})
        write_history(self.root, "2026-09-16", {"a": 3})
        now = [{"path": "a", "well_trod": 8}, {"path": "c", "well_trod": 1}]
        t = wa.build_trend(self.root, "2026-09-30", now)
        self.assertEqual(
            {k: v["snapshot"] for k, v in t["basis"].items()},
            {"7d_ago": "2026-09-23", "14d_ago": "2026-09-16"},
        )
        d = self.by_path(t)
        self.assertEqual((d["a"]["now"], d["a"]["7d_ago"], d["a"]["14d_ago"]), (8, 6, 3))
        # a doc absent from an existing snapshot was 0 there, not unknown
        self.assertEqual((d["c"]["now"], d["c"]["7d_ago"], d["c"]["14d_ago"]), (1, 0, 0))

    def test_decliner_that_fell_out_of_the_window_still_shows_with_now_zero(self):
        write_history(self.root, "2026-09-23", {"gone": 9})
        t = wa.build_trend(self.root, "2026-09-30", [{"path": "a", "well_trod": 2}])
        d = self.by_path(t)
        self.assertEqual((d["gone"]["now"], d["gone"]["7d_ago"]), (0, 9))

    def test_pre_move_adr_key_in_an_old_snapshot_lines_up_with_the_new_key(self):
        write_history(self.root, "2026-09-23", {"adr/README.md": 5})
        t = wa.build_trend(self.root, "2026-09-30", [{"path": "architecture/adr/README.md", "well_trod": 6}])
        self.assertEqual([d["path"] for d in t["docs"]], ["architecture/adr/README.md"])
        self.assertEqual(t["docs"][0]["7d_ago"], 5)

    def test_basis_carries_per_agent_sessions_so_an_unmeasured_agent_is_visible(self):
        # T-4490 review r1: history has recovery at 0 sessions for 30 straight snapshots
        # (2026-08-30..09-28) with no listing_failed flag. A baseline without recovery made
        # every doc read roughly doubled against a `now` that has recovery. The basis must say
        # who was measured in each baseline.
        write_history(
            self.root, "2026-09-23", {"a": 6},
            agents={"specialist": {"sessions_analyzed": 17}, "orchestrator": {"sessions_analyzed": 24}, "recovery": {"sessions_analyzed": 0}},
        )
        write_history(
            self.root, "2026-09-16", {"a": 3},
            agents={"specialist": {"sessions_analyzed": 9}, "recovery": {"sessions_analyzed": 0}},
        )
        t = wa.build_trend(self.root, "2026-09-30", [{"path": "a", "well_trod": 8}])
        self.assertEqual(
            t["basis"]["7d_ago"],
            {"snapshot": "2026-09-23", "sessions_analyzed_by_agent": {"specialist": 17, "orchestrator": 24, "recovery": 0}},
        )
        self.assertEqual(
            t["basis"]["14d_ago"],
            {"snapshot": "2026-09-16", "sessions_analyzed_by_agent": {"specialist": 9, "recovery": 0}},
        )

    def test_basis_tolerates_a_snapshot_with_no_agents_block(self):
        hist = self.root / wa.SNAPSHOT_DIR / "history"
        hist.mkdir(parents=True)
        (hist / "2026-09-23.json").write_text(json.dumps({"docs": [{"path": "a", "well_trod": 6}]}))
        t = wa.build_trend(self.root, "2026-09-30", [{"path": "a", "well_trod": 8}])
        self.assertEqual(t["basis"]["7d_ago"], {"snapshot": "2026-09-23", "sessions_analyzed_by_agent": {}})

    def test_trend_json_carries_the_overlap_and_unmeasured_caveat(self):
        # The docstring's 50% overlap note and the unmeasured-not-quiet warning must
        # travel WITH the data: a reader of latest.json never sees the docstring.
        t = wa.build_trend(self.root, "2026-09-30", [{"path": "a", "well_trod": 8}])
        caveat = t["caveat"]
        self.assertIn("50%", caveat)
        self.assertIn("7d_ago", caveat)
        self.assertIn("14d_ago", caveat)
        self.assertIn("unmeasured, not quiet", caveat)
        self.assertIn("sessions_analyzed_by_agent", caveat)

    def test_missing_snapshot_is_null_not_zero(self):
        write_history(self.root, "2026-09-23", {"a": 6})
        t = wa.build_trend(self.root, "2026-09-30", [{"path": "a", "well_trod": 8}])
        self.assertIsNone(t["basis"]["14d_ago"])
        d = self.by_path(t)["a"]
        self.assertEqual(d["7d_ago"], 6)
        self.assertIsNone(d["14d_ago"])

    def test_one_day_off_snapshot_is_used_two_days_off_is_not(self):
        write_history(self.root, "2026-09-22", {"a": 6})  # 8 days before: within tolerance of -7
        write_history(self.root, "2026-09-13", {"a": 1})  # 17 days before: out of tolerance of -14
        t = wa.build_trend(self.root, "2026-09-30", [{"path": "a", "well_trod": 8}])
        self.assertEqual(t["basis"]["7d_ago"]["snapshot"], "2026-09-22")
        self.assertIsNone(t["basis"]["14d_ago"])

    def test_no_history_at_all_is_all_null_and_never_raises(self):
        t = wa.build_trend(self.root, "2026-09-30", [{"path": "a", "well_trod": 8}])
        self.assertEqual(t["basis"], {"7d_ago": None, "14d_ago": None})
        d = self.by_path(t)["a"]
        self.assertEqual((d["now"], d["7d_ago"], d["14d_ago"]), (8, None, None))

    def test_unreadable_or_docless_history_file_is_treated_as_missing(self):
        hist = self.root / wa.SNAPSHOT_DIR / "history"
        hist.mkdir(parents=True)
        (hist / "2026-09-23.json").write_text("{not json")
        (hist / "2026-09-16.json").write_text(json.dumps({"schema_version": 1}))
        t = wa.build_trend(self.root, "2026-09-30", [{"path": "a", "well_trod": 8}])
        self.assertEqual(t["basis"], {"7d_ago": None, "14d_ago": None})


class PreStoreEstimateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def tile(self, date, a_sessions, a_touches=None):
        write_history(
            self.root,
            date,
            {"docA": (a_sessions, {"recovery": (a_sessions, a_touches or a_sessions)})},
        )

    def test_disjoint_14_day_tiles_are_summed_per_doc_and_agent(self):
        for d, n in (("2026-09-01", 3), ("2026-08-18", 2), ("2026-08-04", 5)):
            self.tile(d, n, n * 2)
        # in-between daily snapshots must NOT be double counted
        self.tile("2026-08-25", 99)
        self.tile("2026-08-31", 99)
        est = wa.build_pre_store_estimate(self.root, "2026-09-01", generated_at="2026-10-01T00:00:00Z")
        self.assertTrue(est["approximate"])
        self.assertEqual([t["snapshot"] for t in est["tiles"]], ["2026-09-01", "2026-08-18", "2026-08-04"])
        self.assertEqual(est["tiles"][0]["covers"], ["2026-08-18", "2026-09-01"])
        (doc,) = est["docs"]
        self.assertEqual((doc["path"], doc["sessions"], doc["touches"]), ("docA", 10, 20))
        self.assertEqual(doc["per_agent"], {"recovery": {"sessions": 10, "touches": 20}})

    def test_latest_snapshot_at_or_before_until_starts_the_chain(self):
        self.tile("2026-09-02", 50)  # after `until` — must be ignored
        self.tile("2026-08-30", 4)
        est = wa.build_pre_store_estimate(self.root, "2026-09-01")
        self.assertEqual(est["tiles"][0]["snapshot"], "2026-08-30")

    def test_chain_stops_at_a_gap(self):
        self.tile("2026-09-01", 3)
        self.tile("2026-08-04", 5)  # 2026-08-18 is missing: do not leap over the hole
        est = wa.build_pre_store_estimate(self.root, "2026-09-01")
        self.assertEqual([t["snapshot"] for t in est["tiles"]], ["2026-09-01"])

    def test_listing_failed_tile_is_skipped_and_reported_not_summed(self):
        self.tile("2026-09-01", 3)
        write_history(
            self.root, "2026-08-18", {"docA": (7, {"recovery": (7, 7)})},
            agents={"specialist": {"sessions_analyzed": 0, "listing_failed": True}},
        )
        self.tile("2026-08-04", 5)
        est = wa.build_pre_store_estimate(self.root, "2026-09-01")
        self.assertEqual([t["snapshot"] for t in est["tiles"]], ["2026-09-01", "2026-08-04"])
        self.assertEqual(est["skipped"][0]["snapshot"], "2026-08-18")
        self.assertIn("listing_failed", est["skipped"][0]["reason"])
        self.assertEqual(est["docs"][0]["sessions"], 8)

    def test_non_14_day_snapshot_is_skipped(self):
        write_history(self.root, "2026-09-01", {"docA": 3}, window_days=7)
        est = wa.build_pre_store_estimate(self.root, "2026-09-01")
        self.assertEqual(est["tiles"], [])
        self.assertEqual(est["docs"], [])
        self.assertIn("window_days", est["skipped"][0]["reason"])

    def test_pre_move_adr_keys_merge_into_the_architecture_adr_key(self):
        # `adr/` moved under `architecture/` on 2026-09-06 (T-3472): the same doc is keyed
        # `adr/X.md` in older snapshots and `architecture/adr/X.md` in newer ones.
        write_history(self.root, "2026-09-01", {"adr/README.md": (3, {"recovery": (3, 3)})})
        write_history(self.root, "2026-08-18", {"architecture/adr/README.md": (2, {"recovery": (2, 4)})})
        est = wa.build_pre_store_estimate(self.root, "2026-09-01")
        self.assertEqual([d["path"] for d in est["docs"]], ["architecture/adr/README.md"])
        self.assertEqual((est["docs"][0]["sessions"], est["docs"][0]["touches"]), (5, 7))

    def test_no_history_gives_an_empty_estimate(self):
        est = wa.build_pre_store_estimate(self.root, "2026-09-01")
        self.assertEqual((est["tiles"], est["docs"]), ([], []))

    def test_write_then_load_round_trips_and_missing_loads_none(self):
        self.assertIsNone(wa.load_pre_store_estimate(self.root))
        self.tile("2026-09-01", 3)
        est = wa.build_pre_store_estimate(self.root, "2026-09-01")
        wa.write_pre_store_estimate(self.root, est)
        self.assertTrue((self.root / wa.SNAPSHOT_DIR / wa.PRE_STORE_FILE).exists())
        self.assertEqual(wa.load_pre_store_estimate(self.root), est)

    def test_each_tile_carries_per_agent_sessions_so_an_unmeasured_agent_is_visible(self):
        # T-4490 review r1: recovery is at 0 in the 2026-09-01 tile (no listing_failed flag),
        # so the estimate silently has no recovery there. The tile must show it.
        write_history(
            self.root, "2026-09-01", {"docA": (3, {"specialist": (3, 3)})},
            agents={"specialist": {"sessions_analyzed": 5}, "orchestrator": {"sessions_analyzed": 37}, "recovery": {"sessions_analyzed": 0}},
        )
        write_history(
            self.root, "2026-08-18", {"docA": (7, {"recovery": (7, 7)})},
            agents={"specialist": {"sessions_analyzed": 4}, "orchestrator": {"sessions_analyzed": 43}, "recovery": {"sessions_analyzed": 24}},
        )
        est = wa.build_pre_store_estimate(self.root, "2026-09-01")
        self.assertEqual(
            est["tiles"][0]["sessions_analyzed_by_agent"], {"specialist": 5, "orchestrator": 37, "recovery": 0}
        )
        self.assertEqual(
            est["tiles"][1]["sessions_analyzed_by_agent"], {"specialist": 4, "orchestrator": 43, "recovery": 24}
        )

    def test_tile_without_an_agents_block_gets_an_empty_per_agent_map(self):
        hist = self.root / wa.SNAPSHOT_DIR / "history"
        hist.mkdir(parents=True)
        (hist / "2026-09-01.json").write_text(
            json.dumps({"window_days": 14, "docs": [{"path": "docA", "well_trod": 3, "touches": 3}]})
        )
        est = wa.build_pre_store_estimate(self.root, "2026-09-01")
        self.assertEqual(est["tiles"][0]["sessions_analyzed_by_agent"], {})

    def test_caveat_says_an_agent_at_zero_was_unmeasured_not_quiet(self):
        self.tile("2026-09-01", 3)
        est = wa.build_pre_store_estimate(self.root, "2026-09-01")
        self.assertIn("unmeasured, not quiet", est["caveat"])
        self.assertIn("sessions_analyzed_by_agent", est["caveat"])
        self.assertIn("unmeasured, not quiet", wa.ESTIMATE_CAVEAT)

    def test_caveat_states_what_cannot_be_recovered(self):
        self.tile("2026-09-01", 3)
        est = wa.build_pre_store_estimate(self.root, "2026-09-01")
        for needle in ("session id", "straddl"):
            self.assertIn(needle, est["caveat"])


class FinalizeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = {
            "schema_version": 1,
            "generated_at": "2026-09-30T09:22:00Z",
            "window_days": 14,
            "docs": [{"path": "docA", "well_trod": 8, "touches": 9, "per_agent": {}}],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_adds_trend_and_all_time_and_leaves_the_base_untouched(self):
        write_history(self.root, "2026-09-23", {"docA": 6})
        wa.ingest(self.root, [row("s1", "docA", 3)])
        before = json.dumps(self.base, sort_keys=True)
        out = wa.finalize_snapshot(self.base, self.root)
        self.assertEqual(json.dumps(self.base, sort_keys=True), before)
        self.assertEqual(out["docs"], self.base["docs"])
        self.assertEqual(out["trend"]["docs"][0]["7d_ago"], 6)
        self.assertEqual(out["all_time"]["docs"][0]["sessions"], 1)

    def test_pending_rows_preview_without_writing(self):
        out = wa.finalize_snapshot(self.base, self.root, pending_rows=[row("s9", "docA", 2)])
        self.assertEqual(out["all_time"]["sessions_counted"], 1)
        self.assertFalse(wa.store_path(self.root).exists())

    def test_picks_up_the_frozen_estimate_when_present(self):
        write_history(self.root, "2026-09-01", {"docA": (3, {"recovery": (3, 3)})})
        wa.write_pre_store_estimate(self.root, wa.build_pre_store_estimate(self.root, "2026-09-01"))
        out = wa.finalize_snapshot(self.base, self.root)
        self.assertEqual(out["all_time"]["docs"][0]["pre_store_sessions_est"], 3)
        self.assertTrue(out["all_time"]["pre_store_estimate"]["approximate"])

    def test_latest_json_says_who_was_measured_in_every_baseline_and_tile(self):
        # T-4490 r2 end to end: the served payload, not just the builders, must show that
        # recovery was unmeasured (0, no listing_failed) in the baseline and the estimate tile.
        agents = {
            "specialist": {"sessions_analyzed": 5},
            "orchestrator": {"sessions_analyzed": 37},
            "recovery": {"sessions_analyzed": 0},
        }
        write_history(self.root, "2026-09-23", {"docA": 6}, agents=agents)
        write_history(self.root, "2026-09-01", {"docA": (3, {"specialist": (3, 3)})}, agents=agents)
        wa.write_pre_store_estimate(self.root, wa.build_pre_store_estimate(self.root, "2026-09-01"))
        out = wa.finalize_snapshot(self.base, self.root)
        want = {"specialist": 5, "orchestrator": 37, "recovery": 0}
        self.assertEqual(out["trend"]["basis"]["7d_ago"]["sessions_analyzed_by_agent"], want)
        est = out["all_time"]["pre_store_estimate"]
        self.assertEqual(est["tile_sessions_analyzed_by_agent"], {"2026-09-01": want})
        for caveat in (out["trend"]["caveat"], est["caveat"]):
            self.assertIn("unmeasured, not quiet", caveat)
            self.assertIn("listing_failed", caveat)


class UnmeasuredCaveatTest(unittest.TestCase):
    def test_one_shared_note_rides_in_both_caveats(self):
        # One source for the recovery fact (P1): the trend and the estimate must not drift apart.
        self.assertIn(wa.UNMEASURED_NOTE, wa.ESTIMATE_CAVEAT)
        self.assertIn(wa.UNMEASURED_NOTE, wa.TREND_CAVEAT)

    def test_note_says_zero_is_unmeasured_and_nonzero_may_be_partial(self):
        note = wa.UNMEASURED_NOTE
        for needle in ("unmeasured, not quiet", "listing_failed", "2026-08-30", "2026-09-28", "T-4159", "partial"):
            self.assertIn(needle, note)


class WindowDerivationTest(unittest.TestCase):
    """One collection pass at the long window; 14d and 30d views derived by mtime."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_gov_repo(self.root)
        self.now = 2_000_000_000.0

    def tearDown(self):
        self.tmp.cleanup()

    def test_filter_reports_window_by_mtime_and_passes_missing_mtime(self):
        reports = {
            "recovery": [
                rep("new", [("sop/standard/alpha.md", 1)], mtime=self.now - 2 * DAY),
                rep("mid", [("sop/standard/alpha.md", 1)], mtime=self.now - 20 * DAY),
                rep("old", [("sop/standard/alpha.md", 1)], mtime=self.now - 40 * DAY),
                rep("nomtime", [("sop/standard/alpha.md", 1)]),
            ]
        }
        ids = lambda w: {r["session_id"] for r in ws.filter_reports_window(reports, self.now - w * DAY)["recovery"]}
        self.assertEqual(ids(14), {"new", "nomtime"})
        self.assertEqual(ids(30), {"new", "mid", "nomtime"})

    def test_collect_all_derives_14d_snapshot_and_30d_block_from_one_pass(self):
        calls = []

        def gather(agent, cfg, cutoff, mx, rr):
            calls.append((agent, cutoff, mx))
            if agent != "recovery":
                return []
            return [
                rep("new", [("sop/standard/alpha.md", 1)], mtime=self.now - 2 * DAY),
                rep("mid", [("sop/standard/alpha.md", 2)], mtime=self.now - 20 * DAY),
            ]

        with mock.patch.object(ws.time, "time", return_value=self.now):
            snap, rows = ws.collect_all(
                str(self.root), 14, 250, gather_fn=gather, long_window_days=30
            )
        # ONE pass per agent, reaching back to the long window
        self.assertEqual(len(calls), len(ws.dtr.AGENTS))
        self.assertTrue(all(abs(c[1] - (self.now - 30 * DAY)) < 1 for c in calls))
        self.assertEqual(snap["window_days"], 14)
        self.assertEqual(snap["agents"]["recovery"]["sessions_analyzed"], 1)
        self.assertEqual(snap["docs"][0]["well_trod"], 1)
        self.assertEqual(snap["long_window"]["window_days"], 30)
        self.assertEqual(snap["long_window"]["sessions_analyzed"], 2)
        self.assertEqual(snap["long_window"]["well_trod"], {"sop/standard/alpha.md": 2})
        # the store gets every session the pass saw, not just the 14d ones
        self.assertEqual({r["session_id"] for r in rows}, {"new", "mid"})

    def test_long_window_zero_disables_the_block_and_collects_at_the_primary_window(self):
        seen = []

        def gather(agent, cfg, cutoff, mx, rr):
            seen.append(cutoff)
            return []

        with mock.patch.object(ws.time, "time", return_value=self.now):
            snap, _ = ws.collect_all(str(self.root), 14, 250, gather_fn=gather, long_window_days=0)
        self.assertNotIn("long_window", snap)
        self.assertTrue(all(abs(c - (self.now - 14 * DAY)) < 1 for c in seen))

    def test_collect_still_returns_just_the_snapshot_for_existing_callers(self):
        snap = ws.collect(str(self.root), 14, 60, gather_fn=lambda *a: [])
        self.assertIn("docs", snap)

    def test_default_cap_does_not_truncate_a_30_day_pass(self):
        self.assertGreaterEqual(ws.DEFAULT_MAX_SESSIONS, 200)
        self.assertEqual(ws.DEFAULT_LONG_WINDOW_DAYS, 30)


class SessionEntriesTest(unittest.TestCase):
    def test_local_entries_carry_mtime_and_gather_attaches_it_to_reports(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            f = d / "sess1.jsonl"
            f.write_text(
                json.dumps(
                    {
                        "timestamp": "2026-09-30T00:00:00Z",
                        "message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "sop/standard/x.md"}}]},
                    }
                )
                + "\n"
            )
            os.utime(f, (1_800_000_000, 1_800_000_000))
            cfg = {"user": None, "projects_dir": td}
            entries = ws.list_session_entries("recovery", cfg, 0)
            self.assertEqual([(m, os.path.basename(p)) for m, p in entries], [(1_800_000_000.0, "sess1.jsonl")])
            reports = ws.gather_session_reports("recovery", cfg, 0, 10, td)
            self.assertEqual(reports[0]["session_id"], "sess1")
            self.assertEqual(reports[0]["mtime"], 1_800_000_000.0)

    def test_failed_listing_is_none_not_empty(self):
        cfg = {"user": "someone", "projects_dir": "/nope"}
        bad = subprocess.CompletedProcess([], 1, stdout="", stderr="denied")
        with mock.patch.object(ws.subprocess, "run", return_value=bad):
            self.assertIsNone(ws.list_session_entries("x", cfg, 0))


class WriteAndCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_gov_repo(self.root)
        self._lock_td = tempfile.TemporaryDirectory()
        self._old_lock = os.environ.get("PANTHEON_GIT_MUTATION_LOCKFILE")
        os.environ["PANTHEON_GIT_MUTATION_LOCKFILE"] = os.path.join(self._lock_td.name, "m.lock")

    def tearDown(self):
        if self._old_lock is None:
            os.environ.pop("PANTHEON_GIT_MUTATION_LOCKFILE", None)
        else:
            os.environ["PANTHEON_GIT_MUTATION_LOCKFILE"] = self._old_lock
        self._lock_td.cleanup()
        self.tmp.cleanup()

    def snap(self, day="2026-09-30"):
        return {
            "schema_version": 1,
            "generated_at": day + "T09:22:00Z",
            "window_days": 14,
            "docs": [{"path": "sop/standard/alpha.md", "well_trod": 1, "touches": 1, "per_agent": {}}],
        }

    def test_legacy_write_without_rows_is_unchanged(self):
        ws.write_snapshot({"schema_version": 1, "generated_at": "2026-07-19T00:00:00Z"}, str(self.root))
        latest = json.loads((self.root / ws.SNAPSHOT_DIR / "latest.json").read_text())
        self.assertNotIn("all_time", latest)
        self.assertFalse(wa.store_path(self.root).exists())

    def test_write_with_rows_ingests_and_latest_carries_trend_and_all_time_but_history_does_not(self):
        write_history(self.root, "2026-09-23", {"sop/standard/alpha.md": 4})
        ws.write_snapshot(self.snap(), str(self.root), rows=[row("s1", "sop/standard/alpha.md", 2)])
        base = self.root / ws.SNAPSHOT_DIR
        latest = json.loads((base / "latest.json").read_text())
        hist = json.loads((base / "history" / "2026-09-30.json").read_text())
        self.assertEqual(latest["all_time"]["sessions_counted"], 1)
        self.assertEqual(latest["trend"]["docs"][0]["7d_ago"], 4)
        self.assertNotIn("all_time", hist)
        self.assertNotIn("trend", hist)
        self.assertEqual(hist["docs"], latest["docs"])
        self.assertEqual(len(wa.store_path(self.root).read_text().splitlines()), 1)

    def test_second_identical_write_leaves_the_store_byte_identical(self):
        rows = [row("s1", "sop/standard/alpha.md", 2)]
        ws.write_snapshot(self.snap("2026-09-30"), str(self.root), rows=rows)
        before = wa.store_path(self.root).read_bytes()
        ws.write_snapshot(self.snap("2026-10-01"), str(self.root), rows=rows)
        self.assertEqual(wa.store_path(self.root).read_bytes(), before)

    def test_history_pruning_never_touches_the_store_or_estimate(self):
        base = self.root / ws.SNAPSHOT_DIR
        wa.ingest(self.root, [row("s1", "d", 1)])
        (base / wa.PRE_STORE_FILE).write_text("{}\n")
        ws.write_snapshot(self.snap("2027-06-01"), str(self.root), rows=[])
        self.assertTrue(wa.store_path(self.root).exists())
        self.assertTrue((base / wa.PRE_STORE_FILE).exists())

    def _run_main(self, argv, fake):
        with mock.patch.object(
            ws, "gather_session_reports",
            side_effect=lambda agent, cfg, cut, mx, rr: fake.get(agent, []),
        ):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = ws.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_dry_run_previews_all_time_and_writes_nothing(self):
        fake = {"recovery": [fake_report("s1", [("sop/standard/alpha.md", 2)])]}
        rc, out, err = self._run_main(["--collect", "--dry-run", "--repo-root", str(self.root)], fake)
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["all_time"]["sessions_counted"], 1)
        self.assertIn("long_window", payload)
        self.assertFalse((self.root / ws.SNAPSHOT_DIR).exists())

    def test_commit_prod_commits_store_with_the_snapshot_and_a_rerun_adds_no_store_diff(self):
        origin = self.root.parent / (self.root.name + "-origin.git")
        subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
        git = lambda *a: subprocess.run(["git", *a], cwd=self.root, check=True, capture_output=True, text=True)
        git("init", "-b", "main")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        git("remote", "add", "origin", str(origin))
        git("add", "-A")
        git("commit", "-m", "init")
        git("push", "origin", "HEAD:main")
        self.addCleanup(subprocess.run, ["rm", "-rf", str(origin)])

        fake = {"recovery": [fake_report("s1", [("sop/standard/alpha.md", 2)])]}
        rc, _, err = self._run_main(["--collect", "--commit-prod", "--repo-root", str(self.root)], fake)
        self.assertEqual(rc, 0, err)
        tracked = git("ls-tree", "-r", "--name-only", "HEAD").stdout
        self.assertIn(ws.SNAPSHOT_DIR + "/" + wa.STORE_FILE, tracked)
        store_blob = git("rev-parse", "HEAD:" + ws.SNAPSHOT_DIR + "/" + wa.STORE_FILE).stdout

        rc, _, err = self._run_main(["--collect", "--commit-prod", "--repo-root", str(self.root)], fake)
        self.assertEqual(rc, 0, err)
        self.assertEqual(git("rev-parse", "HEAD:" + ws.SNAPSHOT_DIR + "/" + wa.STORE_FILE).stdout, store_blob)

    def test_backfill_requires_until_and_one_of_dry_run_or_write(self):
        for argv in (
            ["--backfill-from-history", "--dry-run"],
            ["--backfill-from-history", "--until", "2026-09-01"],
            ["--backfill-from-history", "--until", "2026-09-01", "--dry-run", "--write"],
        ):
            with self.subTest(argv=argv):
                rc, _, _ = self._run_main(argv + ["--repo-root", str(self.root)], {})
                self.assertEqual(rc, ws.EX_USAGE)

    def test_backfill_dry_run_prints_and_writes_nothing_then_write_creates_only_the_estimate(self):
        write_history(self.root, "2026-09-01", {"docA": (3, {"recovery": (3, 3)})})
        rc, out, err = self._run_main(
            ["--backfill-from-history", "--until", "2026-09-01", "--dry-run", "--repo-root", str(self.root)], {}
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["docs"][0]["sessions"], 3)
        self.assertFalse((self.root / ws.SNAPSHOT_DIR / wa.PRE_STORE_FILE).exists())
        rc, _, err = self._run_main(
            ["--backfill-from-history", "--until", "2026-09-01", "--write", "--repo-root", str(self.root)], {}
        )
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.root / ws.SNAPSHOT_DIR / wa.PRE_STORE_FILE).exists())
        self.assertFalse(wa.store_path(self.root).exists())


class SubagentStoreTest(unittest.TestCase):
    """T-4509: subagent rows carry source; old rows load as main; separate column."""

    def test_rows_source_only_on_subagent(self):
        reps = {"orchestrator": [rep("agent-1", [("sop/standard/alpha.md", 1)], mtime=1.0)]}
        with tempfile.TemporaryDirectory() as td:
            make_gov_repo(Path(td))
            main = wa.session_doc_rows(reps, td, "2026-10-01")
            sub = wa.session_doc_rows(reps, td, "2026-10-01", source="subagent")
        self.assertNotIn("source", main[0])
        self.assertEqual(sub[0]["source"], "subagent")

    def test_old_row_without_source_loads_as_main(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.jsonl"
            p.write_text(json.dumps(row("s1", "docA", 2)) + "\n")
            rec = wa.load_store(p)[("s1", "docA")]
        self.assertEqual(rec["source"], "main")

    def test_mistyped_source_coerces_to_main(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.jsonl"
            r = row("s1", "docA", 2)
            r["source"] = 7
            p.write_text(json.dumps(r) + "\n")
            self.assertEqual(wa.load_store(p)[("s1", "docA")]["source"], "main")

    def test_subagent_rows_survive_ingest_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            r = dict(row("agent-1", "docA", 2), source="subagent")
            wa.ingest(td, [r])
            self.assertEqual(wa.load_store(wa.store_path(td))[("agent-1", "docA")]["source"], "subagent")

    def test_summary_main_numbers_unchanged_and_subagent_separate(self):
        base = [row("t1", "docA", 3), row("w1", "docA", 1, agent="recovery")]
        subs = [
            dict(row("agent-1", "docA", 5), source="subagent"),
            dict(row("agent-2", "docA", 1, agent="recovery"), source="subagent"),
            dict(row("agent-3", "docC", 2), source="subagent"),
        ]

        def load(rows):
            return {(r["session_id"], r["doc"]): dict(r, source=r.get("source", "main")) for r in rows}

        without = wa.summarize_all_time(load(base))
        with_sub = wa.summarize_all_time(load(base + subs))
        self.assertEqual(with_sub["sessions_counted"], without["sessions_counted"])
        a = [d for d in with_sub["docs"] if d["path"] == "docA"][0]
        b = [d for d in without["docs"] if d["path"] == "docA"][0]
        for k in ("sessions", "touches", "per_agent"):
            self.assertEqual(a[k], b[k])
        self.assertEqual((a["subagent_sessions"], a["subagent_touches"]), (2, 6))
        self.assertEqual(a["per_agent_subagent"], {"orchestrator": {"sessions": 1, "touches": 5}, "recovery": {"sessions": 1, "touches": 1}})
        self.assertEqual(with_sub["subagent_sessions_counted"], 3)
        c = [d for d in with_sub["docs"] if d["path"] == "docC"][0]
        self.assertEqual((c["sessions"], c["subagent_sessions"]), (0, 1))
        self.assertEqual(without["subagent_sessions_counted"], 0)


if __name__ == "__main__":
    unittest.main()
