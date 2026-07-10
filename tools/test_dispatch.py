#!/usr/bin/env python3
"""Hermetic tests for tools/dispatch.py (T-1086).

Points PANTHEON_DISPATCH_LEDGER at a tmp file and exercises validation, record,
the open→close evolution + resolution, queries, and the alloc-id monotonicity.
All record/close calls use commit=False (no git). No live ledger is touched.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import dispatch  # noqa: E402


def _good_payload(**over) -> dict:
    base = {
        "dispatcher": "recovery",
        "dispatchee": "implementer",
        "ticket": "T-9999",
        "serves": "ship the keystone",
        "brief": "Build X per spec; verify named symbols at source.",
        "locked_answers": ["flat jsonl"],
        "autonomy_partition": {"autonomous": ["naming"], "bubble_up": ["schema"]},
        "response_contract": {"answer_form": "PLAN then READY", "format": "DECISIONS", "timing": "next msg"},
        "isolation": "worktree",
    }
    base.update(over)
    return base


class ValidateTest(unittest.TestCase):
    def test_good_payload_valid(self):
        rec, warnings = dispatch.validate(_good_payload())
        self.assertEqual(rec["dispatcher"], "recovery")
        self.assertEqual(warnings, [])  # known agent + serves present

    def test_unknown_dispatchee_warns_not_fails(self):
        _rec, warnings = dispatch.validate(_good_payload(dispatchee="b33-implementer"))
        self.assertTrue(any("not a known agent" in w for w in warnings))

    def test_absent_serves_warns(self):
        p = _good_payload()
        del p["serves"]
        _rec, warnings = dispatch.validate(p)
        self.assertTrue(any("serves" in w for w in warnings))

    def test_missing_required_raises(self):
        for field in dispatch.REQUIRED_FIELDS:
            p = _good_payload()
            del p[field]
            with self.assertRaises(dispatch.DispatchError):
                dispatch.validate(p)

    def test_bad_slug_raises(self):
        with self.assertRaises(dispatch.DispatchError):
            dispatch.validate(_good_payload(dispatcher="T-1"))

    def test_bad_ticket_raises(self):
        with self.assertRaises(dispatch.DispatchError):
            dispatch.validate(_good_payload(ticket="1086"))

    def test_autonomy_partition_shape(self):
        with self.assertRaises(dispatch.DispatchError):
            dispatch.validate(_good_payload(autonomy_partition={"autonomous": []}))
        with self.assertRaises(dispatch.DispatchError):
            dispatch.validate(_good_payload(autonomy_partition={"autonomous": "x", "bubble_up": []}))

    def test_response_contract_requires_three_keys(self):
        with self.assertRaises(dispatch.DispatchError):
            dispatch.validate(_good_payload(response_contract={"answer_form": "a", "format": "f"}))
        with self.assertRaises(dispatch.DispatchError):
            dispatch.validate(_good_payload(response_contract={"answer_form": "a", "format": "f", "timing": ""}))

    def test_bad_enums_raise(self):
        with self.assertRaises(dispatch.DispatchError):
            dispatch.validate(_good_payload(isolation="vm"))
        with self.assertRaises(dispatch.DispatchError):
            dispatch.validate(_good_payload(result="done"))

    def test_rebound_count_int_nonneg(self):
        with self.assertRaises(dispatch.DispatchError):
            dispatch.validate(_good_payload(rebound_count=-1))
        with self.assertRaises(dispatch.DispatchError):
            dispatch.validate(_good_payload(rebound_count=True))  # bool is not a valid int here

    def test_unknown_keys_dropped(self):
        rec, _ = dispatch.validate(_good_payload(bogus="x"))
        self.assertNotIn("bogus", rec)


class LedgerLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "ledger.jsonl"
        os.environ["PANTHEON_DISPATCH_LEDGER"] = str(self.ledger)
        os.environ["PANTHEON_DISPATCH_ALLOC_LOCKFILE"] = str(Path(self.tmp.name) / "lock")

    def tearDown(self):
        os.environ.pop("PANTHEON_DISPATCH_LEDGER", None)
        os.environ.pop("PANTHEON_DISPATCH_ALLOC_LOCKFILE", None)
        self.tmp.cleanup()

    def test_record_stamps_and_appends(self):
        rec, _ = dispatch.cmd_record(_good_payload(), commit=False, push=False)
        self.assertEqual(rec["id"], "D-0001")
        self.assertEqual(rec["kind"], "open")
        self.assertIn("ts", rec)
        lines = [l for l in self.ledger.read_text().splitlines() if l.strip()]
        self.assertEqual(len(lines), 1)

    def test_ids_monotonic(self):
        r1, _ = dispatch.cmd_record(_good_payload(), commit=False, push=False)
        r2, _ = dispatch.cmd_record(_good_payload(ticket="T-8888"), commit=False, push=False)
        self.assertEqual(r1["id"], "D-0001")
        self.assertEqual(r2["id"], "D-0002")

    def test_open_then_close_resolution(self):
        rec, _ = dispatch.cmd_record(_good_payload(), commit=False, push=False)
        did = rec["id"]
        resolved = dispatch.resolve_dispatches(dispatch._read_records())
        self.assertEqual(resolved[did]["status"], "open")
        self.assertNotIn("result", resolved[did])

        dispatch.cmd_close(did, result="ready", rebound_count=1, commit=False, push=False)
        resolved = dispatch.resolve_dispatches(dispatch._read_records())
        self.assertEqual(resolved[did]["status"], "closed")
        self.assertEqual(resolved[did]["result"], "ready")
        self.assertEqual(resolved[did]["rebound_count"], 1)
        # Open ts preserved; closed_ts added; both raw rows persist (append-only).
        self.assertIn("ts", resolved[did])
        self.assertIn("closed_ts", resolved[did])
        self.assertEqual(len([l for l in self.ledger.read_text().splitlines() if l.strip()]), 2)

    def test_close_unknown_raises(self):
        with self.assertRaises(dispatch.DispatchError):
            dispatch.cmd_close("D-0099", result="ready", rebound_count=None, commit=False, push=False)

    def test_double_close_raises(self):
        rec, _ = dispatch.cmd_record(_good_payload(), commit=False, push=False)
        dispatch.cmd_close(rec["id"], result="ready", rebound_count=None, commit=False, push=False)
        with self.assertRaises(dispatch.DispatchError):
            dispatch.cmd_close(rec["id"], result="rebound", rebound_count=None, commit=False, push=False)

    def test_record_invalid_does_not_append(self):
        with self.assertRaises(dispatch.DispatchError):
            dispatch.cmd_record(_good_payload(ticket="bad"), commit=False, push=False)
        self.assertFalse(self.ledger.exists() and self.ledger.read_text().strip())

    def test_ticket_query(self):
        dispatch.cmd_record(_good_payload(ticket="T-1000"), commit=False, push=False)
        dispatch.cmd_record(_good_payload(ticket="T-1000"), commit=False, push=False)
        dispatch.cmd_record(_good_payload(ticket="T-2000"), commit=False, push=False)
        resolved = dispatch.resolve_dispatches(dispatch._read_records())
        hits = [d for d in resolved.values() if d["ticket"] == "T-1000"]
        self.assertEqual(len(hits), 2)

    def test_malformed_ledger_line_raises(self):
        self.ledger.write_text('{"id":"D-0001","kind":"open"}\nNOT JSON\n')
        with self.assertRaises(dispatch.DispatchError):
            dispatch._read_records()

    def test_corrupt_close_only_orphan_is_tolerated(self):
        # A close with no preceding open (hand-corrupted) resolves as a degenerate
        # closed record, never crashes.
        self.ledger.write_text('{"id":"D-0050","kind":"close","result":"ready","ts":"2026-01-01T00:00:00Z"}\n')
        resolved = dispatch.resolve_dispatches(dispatch._read_records())
        self.assertEqual(resolved["D-0050"]["status"], "closed")


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "ledger.jsonl"
        os.environ["PANTHEON_DISPATCH_LEDGER"] = str(self.ledger)
        os.environ["PANTHEON_DISPATCH_ALLOC_LOCKFILE"] = str(Path(self.tmp.name) / "lock")
        self.payload_file = Path(self.tmp.name) / "p.json"
        self.payload_file.write_text(json.dumps(_good_payload()))

    def tearDown(self):
        os.environ.pop("PANTHEON_DISPATCH_LEDGER", None)
        os.environ.pop("PANTHEON_DISPATCH_ALLOC_LOCKFILE", None)
        self.tmp.cleanup()

    def test_validate_cli_exit0(self):
        rc = dispatch.main(["validate", "--from-json", str(self.payload_file)])
        self.assertEqual(rc, 0)

    def test_validate_cli_bad_exit65(self):
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text('{"dispatcher":"recovery"}')
        rc = dispatch.main(["validate", "--from-json", str(bad)])
        self.assertEqual(rc, 65)

    def test_record_cli_no_commit(self):
        rc = dispatch.main(["record", "--from-json", str(self.payload_file), "--no-commit"])
        self.assertEqual(rc, 0)
        self.assertEqual(len([l for l in self.ledger.read_text().splitlines() if l.strip()]), 1)

    def test_show_unknown_exit1(self):
        rc = dispatch.main(["show", "D-0001"])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
