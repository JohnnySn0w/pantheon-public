#!/usr/bin/env python3
"""Tests for seed_genericize.py (T-1327).

Locks the public-repo SAFETY property: the fail-on-leak gate must abort the run
(exit 2) and must NEVER write or print a file whose scrub leaves a leak marker.
Also covers the core substitution classes (operator identity, ticket/msg removal,
instance-project nouns, commit hashes, descriptive-paren unwrap).

Run: python3 -m unittest tools.test_seed_genericize   (pytest is not installed)
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import seed_genericize as sg  # noqa: E402


class TestFindLeaks(unittest.TestCase):
    def test_detects_each_marker_class(self):
        samples = {
            "operator-name": "Michael ran it",
            "ticket-id": "per T-1234 we did",
            "message-id": "see msg_991",
            "iso-date": "on 2026-01-02",
            "commit-hash": "commit `6f6a465`",
            "host-swarm": "the swarm box",
            "account-name": "claude-fallback account",
            "orchestrator-abbrev": "HO did it",
            "sudoers-file": "fallback-rescues-prime",
        }
        for label, text in samples.items():
            leaks = sg.find_leaks(text)
            self.assertTrue(any(lbl == label for lbl, _, _ in leaks),
                            f"{label} not detected in {text!r}: {leaks}")

    def test_clean_text_has_no_leaks(self):
        self.assertEqual(sg.find_leaks("the operator restarted the Recovery agent"), [])


class TestScrub(unittest.TestCase):
    def test_operator_identity(self):
        out = sg.scrub("Michael and Michael's plan")
        self.assertNotIn("Michael", out)
        self.assertIn("the operator", out)
        self.assertIn("the operator's", out)

    def test_ticket_and_msg_removed(self):
        self.assertEqual(sg.find_leaks(sg.scrub("ratified (T-0467) per msg_1739")), [])

    def test_descriptive_paren_unwrap_keeps_prose(self):
        out = sg.scrub("T-0309 (this ADR's authoring ticket) matters")
        self.assertIn("this ADR's authoring ticket", out)
        self.assertNotIn("T-0309", out)

    def test_instance_project_names_kept(self):
        # operator decision: project codenames are NOT scrubbed (not sensitive) and
        # are NOT a leak class — they survive the scrub and pass the gate.
        out = sg.scrub("the kino pwa, the codex daemon, the echo-prismatic project")
        self.assertEqual(sg.find_leaks(out), [])
        self.assertIn("kino", out)
        self.assertIn("codex", out)
        self.assertIn("echo", out)

    def test_commit_hash_genericized(self):
        self.assertEqual(sg.find_leaks(sg.scrub("landed `ac55f461` today")), [])

    def test_incident_codename_caught_and_scrubbed(self):
        # internal batch/incident labels (b33, b36) leak — the gate must flag them...
        self.assertTrue(any(lbl == "incident-codename"
                            for lbl, _, _ in sg.find_leaks("the b33 postmortem named Shape 2")))
        # ...and the scrub must remove them so a clean file is producible.
        self.assertEqual(sg.find_leaks(sg.scrub("the b33 cycle and the b36 failure")), [])
        # the `<date-Bnn>` format placeholder (capital B, no digits) must NOT be flagged.
        self.assertEqual(sg.find_leaks("incidents/<date-Bnn>/ is a format placeholder"), [])

    def test_dates_dropped(self):
        self.assertEqual(sg.find_leaks(sg.scrub("Ratified 2026-05-20 (2026-05-19)")), [])


class TestGateMainFlow(unittest.TestCase):
    def _write(self, text: str) -> Path:
        fd, p = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        path = Path(p)
        path.write_text(text, encoding="utf-8")
        return path

    def test_clean_file_check_exit0(self):
        p = self._write("---\ntitle: x\n---\n# H\n\nMichael ratified it (T-0467).\n")
        try:
            self.assertEqual(sg.main(["--check", str(p)]), 0)
        finally:
            p.unlink()

    def test_inplace_writes_only_clean_scrub(self):
        p = self._write("---\ntitle: x\n---\n# H\n\nMichael ratified it (T-0467).\n")
        try:
            self.assertEqual(sg.main(["--in-place", str(p)]), 0)
            out = p.read_text(encoding="utf-8")
            self.assertNotIn("Michael", out)
            self.assertEqual(sg.find_leaks(out), [])
        finally:
            p.unlink()

    def test_residual_leak_forces_exit2_and_never_emits(self):
        """The core safety guarantee: if scrub leaves a leak, the gate aborts and
        the file is neither written (--in-place) nor printed (--print)."""
        orig_subs, orig_clean = sg.SUBSTITUTIONS, sg.CLEANUPS
        sg.SUBSTITUTIONS, sg.CLEANUPS = [], []  # neuter the scrub so a leak survives
        try:
            content = "---\ntitle: x\n---\n# H\n\nMichael did it.\n"
            p = self._write(content)
            try:
                self.assertEqual(sg.main(["--check", str(p)]), 2)
                # --in-place must NOT modify a leaking file
                self.assertEqual(sg.main(["--in-place", str(p)]), 2)
                self.assertEqual(p.read_text(encoding="utf-8"), content)
                # --print must NOT emit the leaking body to stdout
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = sg.main(["--print", str(p)])
                self.assertEqual(rc, 2)
                self.assertNotIn("Michael did it", buf.getvalue())
            finally:
                p.unlink()
        finally:
            sg.SUBSTITUTIONS, sg.CLEANUPS = orig_subs, orig_clean

    def test_missing_file_exits_3(self):
        self.assertEqual(sg.main(["--check", "/nonexistent/path/ORG-999.md"]), 3)


if __name__ == "__main__":
    unittest.main()
