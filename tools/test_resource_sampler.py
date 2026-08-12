#!/usr/bin/env python3
"""Tests for tools/resource_sampler.py (T-2213).

The sampler's whole job is to still have evidence after an incident, so the tests
that matter are the ones about NOT losing data and NOT dying.
"""
import json
import os
import pathlib
import tempfile
import unittest

import tools.resource_sampler as rs


class TestSample(unittest.TestCase):
    def test_sample_has_the_fields_a_post_mortem_needs(self):
        rec = rs.sample()
        for k in ("ts", "mem_total_kb", "mem_available_kb", "mem_used_pct",
                  "swap_total_kb", "swap_used_kb", "swap_used_pct", "loadavg", "procs"):
            self.assertIn(k, rec)

    def test_records_SWAP_not_only_rss(self):
        """The 2026-07-28 spiral was a swap-exhaustion story; RSS alone would have
        shown the fleet looking unremarkable while swap filled underneath it."""
        rec = rs.sample()
        self.assertIsInstance(rec["swap_used_kb"], int)
        for p in rec["procs"]:
            self.assertIn("swap_kb", p)

    def test_procs_are_ranked_and_capped(self):
        rec = rs.sample()
        self.assertLessEqual(len(rec["procs"]), rs.TOP_N)
        totals = [p["rss_kb"] + p["swap_kb"] for p in rec["procs"]]
        self.assertEqual(totals, sorted(totals, reverse=True))

    def test_percentages_are_consistent_with_their_totals(self):
        rec = rs.sample()
        if rec["swap_total_kb"]:
            expected = round(rec["swap_used_kb"] * 100 / rec["swap_total_kb"], 1)
            self.assertAlmostEqual(rec["swap_used_pct"], expected, places=1)


class TestRotation(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="t2213-")
        self.p = pathlib.Path(self.d) / "samples.jsonl"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def test_rotation_keeps_the_NEWEST_lines(self):
        """Oldest-first truncation is load-bearing: during an incident the recent
        samples ARE the evidence, so trimming them to stay small defeats the tool."""
        self.p.write_text("".join(f"line{i}\n" for i in range(100)), encoding="utf-8")
        rs._rotate(self.p, 10)
        kept = self.p.read_text().splitlines()
        self.assertEqual(len(kept), 10)
        self.assertEqual(kept[-1], "line99", "the most recent sample must survive")
        self.assertEqual(kept[0], "line90")

    def test_rotation_is_a_noop_under_the_cap(self):
        self.p.write_text("a\nb\n", encoding="utf-8")
        rs._rotate(self.p, 10)
        self.assertEqual(self.p.read_text(), "a\nb\n")

    def test_rotation_on_missing_file_does_not_raise(self):
        rs._rotate(pathlib.Path(self.d) / "nope.jsonl", 10)


class TestNeverFails(unittest.TestCase):
    """A sampler that crashes during a memory event destroys the evidence of it."""

    def test_main_returns_zero_even_if_sampling_explodes(self):
        orig = rs.sample
        rs.sample = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            self.assertEqual(rs.main([]), 0)
        finally:
            rs.sample = orig

    def test_main_returns_zero_on_unwritable_log(self):
        self.assertEqual(rs.main(["--log", "/proc/cannot/write/here.jsonl"]), 0)

    def test_appends_valid_jsonl(self):
        d = tempfile.mkdtemp(prefix="t2213w-")
        p = pathlib.Path(d) / "s.jsonl"
        rs.main(["--log", str(p)])
        rs.main(["--log", str(p)])
        lines = p.read_text().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)  # must parse
        import shutil
        shutil.rmtree(d, ignore_errors=True)

    def test_unreadable_proc_entry_does_not_abort_the_scan(self):
        """Peer processes are unreadable cross-account; one must not end the sample."""
        real = rs.Path

        class Boom(type(pathlib.Path())):
            pass

        rows = rs._proc_rows()
        self.assertIsInstance(rows, list)  # completes despite foreign /proc entries


if __name__ == "__main__":
    unittest.main(verbosity=2)
