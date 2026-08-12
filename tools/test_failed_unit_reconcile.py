#!/usr/bin/env python3
"""Tests for failed_unit_reconcile (T-2362).

The load-bearing property is the TRI-STATE: "could not ask the seat" must never
become "no failed units". This tool exists to detect silence, so a blind read that
collapsed to an empty set would manufacture precisely the all-clear it was built to
catch — T-2309's defect, in the one place it would be least visible.

The live positive control (a planted failing unit, detected and attributed) was run
by hand before these were written; these pin the branches a live run cannot stage
safely — in particular ACCOUNTED, which needs a failed unit that DID alert.
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from tools import failed_unit_reconcile as fur  # noqa: E402


def _write_msg(d: pathlib.Path, sender: str, subject: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    n = len(list(d.iterdir()))
    (d / f"{n}.msg").write_text(
        json.dumps({"sender": sender, "subject": subject, "payload": "x"}),
        encoding="utf-8",
    )


class TestAlertedUnits(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inbox = pathlib.Path(self.tmp.name)

    def test_reads_both_cur_and_new(self):
        """An alert sitting undrained in new/ still ARRIVED. Counting only cur/
        would report a false asymmetry for anything delivered since the last
        consume — i.e. invent a silent failure out of ordinary queue latency."""
        _write_msg(self.inbox / "cur", "specialist", "unit-failure: a.service")
        _write_msg(self.inbox / "new", "specialist", "unit-failure: b.service")
        got = fur.alerted_units(self.inbox)
        self.assertEqual(got["specialist"], {"a.service", "b.service"})

    def test_ignores_non_alert_subjects(self):
        _write_msg(self.inbox / "cur", "orchestrator", "some other subject entirely")
        self.assertEqual(fur.alerted_units(self.inbox), {})

    def test_survives_a_corrupt_message(self):
        (self.inbox / "cur").mkdir(parents=True)
        (self.inbox / "cur" / "bad.msg").write_text("{not json", encoding="utf-8")
        _write_msg(self.inbox / "cur", "specialist", "unit-failure: a.service")
        self.assertEqual(fur.alerted_units(self.inbox)["specialist"], {"a.service"})


class TestReconcileTriState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inbox = pathlib.Path(self.tmp.name)

    def _reconcile(self, failed_by_account):
        def fake(acct, **kw):
            return failed_by_account[acct]
        with patch.object(fur, "failed_units", side_effect=fake):
            return fur.reconcile(list(failed_by_account), inbox=self.inbox)

    def test_UNKNOWN_is_never_reported_as_clean(self):
        """THE DEFECT THIS TOOL MUST NOT HAVE. An unreachable seat is unmeasured,
        not healthy."""
        r = self._reconcile({"claude-research": fur.UNKNOWN})
        self.assertEqual(r["unreachable"], ["claude-research"])
        self.assertEqual(r["clean"], [],
                         "an unreachable seat must NOT be counted clean — that is "
                         "a confident all-clear built from a blind read")
        self.assertEqual(r["SILENT"], {})

    def test_empty_set_IS_clean_and_is_a_different_answer(self):
        """The contrast that makes the above meaningful: an ANSWERED probe with no
        failures is genuinely clean. If both branches produced the same verdict the
        tri-state would be decorative."""
        r = self._reconcile({"claude-research": set()})
        self.assertEqual(r["clean"], ["claude-research"])
        self.assertEqual(r["unreachable"], [])

    def test_failed_without_an_alert_is_SILENT(self):
        r = self._reconcile({"claude-research": {"x.service"}})
        self.assertEqual(r["SILENT"], {"claude-research": ["x.service"]})

    def test_failed_WITH_an_alert_is_accounted_not_silent(self):
        """The branch a live run cannot stage safely. An alert arrives under the
        PERSONA name (specialist), while the probe is keyed by unix ACCOUNT
        (claude-research) — if those are not reconciled, every already-reported
        failure is re-reported as silent and the tool becomes pure noise."""
        _write_msg(self.inbox / "cur", "specialist", "unit-failure: x.service")
        r = self._reconcile({"claude-research": {"x.service"}})
        self.assertEqual(r["SILENT"], {})
        self.assertEqual(r["accounted"], {"claude-research": ["x.service"]})

    def test_partial_coverage_splits_correctly(self):
        _write_msg(self.inbox / "cur", "specialist", "unit-failure: seen.service")
        r = self._reconcile({"claude-research": {"seen.service", "unseen.service"}})
        self.assertEqual(r["accounted"], {"claude-research": ["seen.service"]})
        self.assertEqual(r["SILENT"], {"claude-research": ["unseen.service"]})


class TestFailedUnitsProbe(unittest.TestCase):
    """The probe must fail to UNKNOWN, never to 'nothing failed'."""

    def _run(self, **kw):
        class R:
            returncode = kw.get("rc", 0)
            stdout = kw.get("out", "")
            stderr = ""
        return R()

    def test_nonzero_exit_is_UNKNOWN(self):
        with patch.object(fur.subprocess, "run", lambda *a, **k: self._run(rc=1)):
            self.assertIs(fur.failed_units("x"), fur.UNKNOWN)

    def test_timeout_is_UNKNOWN(self):
        import subprocess as sp

        def boom(*a, **k):
            raise sp.TimeoutExpired(cmd="x", timeout=1)
        with patch.object(fur.subprocess, "run", boom):
            self.assertIs(fur.failed_units("x"), fur.UNKNOWN)

    def test_oserror_is_UNKNOWN(self):
        def boom(*a, **k):
            raise OSError("no sudo")
        with patch.object(fur.subprocess, "run", boom):
            self.assertIs(fur.failed_units("x"), fur.UNKNOWN)

    def test_clean_probe_is_an_empty_set_not_UNKNOWN(self):
        with patch.object(fur.subprocess, "run", lambda *a, **k: self._run(rc=0, out="")):
            self.assertEqual(fur.failed_units("x"), set())

    def test_parses_unit_names(self):
        out = ("a.service loaded failed failed Some description here\n"
               "b.timer   loaded failed failed Another one\n")
        with patch.object(fur.subprocess, "run", lambda *a, **k: self._run(rc=0, out=out)):
            self.assertEqual(fur.failed_units("x"), {"a.service", "b.timer"})


if __name__ == "__main__":
    unittest.main()
