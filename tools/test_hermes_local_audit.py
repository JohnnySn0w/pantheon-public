#!/usr/bin/env python3
"""Tests for tools/hermes_local_audit.py (T-2392).

Source cleanliness: the trigger sequence is assembled at RUNTIME by
concatenation in every fixture below, so it never appears as contiguous bytes in
this file. `grep -cF` for the unescaped form against this file must return 0.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hermes_local_audit as hla  # noqa: E402

TRIGGER = b"HERME" + b"S.md"          # runtime-assembled; never contiguous in source
SAFE = b"hermes.md"


class InstrumentValidation(unittest.TestCase):
    """Design decision 3: never believe a zero from an unproven scanner."""

    def test_real_pattern_passes_validation(self):
        hla.validate_instrument()  # must not raise against the live pattern

    def test_raises_when_pattern_cannot_match_the_positive(self):
        dead = mock.Mock()
        dead.search.return_value = None          # matches nothing, ever
        with mock.patch.object(hla, "_HERMES_PATTERN", dead):
            with self.assertRaises(hla.AuditError) as cm:
                hla.validate_instrument()
        self.assertIn("known-POSITIVE", str(cm.exception))

    def test_raises_when_pattern_is_over_broad(self):
        greedy = mock.Mock()
        greedy.search.return_value = True        # matches everything, including the safe form
        with mock.patch.object(hla, "_HERMES_PATTERN", greedy):
            with self.assertRaises(hla.AuditError) as cm:
                hla.validate_instrument()
        self.assertIn("known-NEGATIVE", str(cm.exception))


class BaseResolution(unittest.TestCase):
    """Design decision 2: a base off main's history yields a meaningless green."""

    def test_refuses_a_base_that_is_not_an_ancestor(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=1)   # merge-base --is-ancestor fails
            with self.assertRaises(hla.AuditError) as cm:
                hla.resolve_base({"last_clean_sha": "deadbeef"}, None)
        self.assertIn("NOT an ancestor", str(cm.exception))

    def test_accepts_an_ancestor(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertEqual(hla.resolve_base({"last_clean_sha": "abc123"}, None), "abc123")

    def test_explicit_override_wins_over_state(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertEqual(hla.resolve_base({"last_clean_sha": "abc123"}, "override1"), "override1")

    def test_bootstrap_used_when_state_is_empty(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertEqual(hla.resolve_base({}, None), hla.BOOTSTRAP_BASE)


class Watermark(unittest.TestCase):
    """Design decision 4: a violation must never advance the watermark."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state.json"

    def _audit_with(self, surface: bytes, head="newhead", base="oldbase"):
        def fake_git(*args, **kw):
            if args[0] == "log":
                return surface
            if args[0] == "rev-parse":
                return head + "\n"
            if args[0] == "rev-list":
                return "3\n"
            return ""
        with mock.patch.object(hla, "_git", side_effect=fake_git), \
             mock.patch.object(hla, "resolve_base", return_value=base):
            return hla.audit(self.state, None, dry_run=False)

    def test_clean_advances_the_watermark(self):
        out = self._audit_with(b"+ a perfectly ordinary added line\n")
        self.assertEqual(out["result"], "clean")
        self.assertEqual(json.loads(self.state.read_text())["last_clean_sha"], "newhead")

    def test_violation_does_NOT_advance_the_watermark(self):
        hla.save_state(self.state, {"last_clean_sha": "oldbase"})
        out = self._audit_with(b"+ see " + TRIGGER + b" for details\n")
        self.assertEqual(out["result"], "violation")
        self.assertEqual(out["violations"], 1)
        # The whole point: the window is NOT audited away by a failing run.
        self.assertEqual(json.loads(self.state.read_text())["last_clean_sha"], "oldbase")

    def test_lowercase_form_is_not_a_violation(self):
        out = self._audit_with(b"+ see " + SAFE + b" for details\n")
        self.assertEqual(out["result"], "clean")

    def test_dry_run_never_writes_state(self):
        def fake_git(*args, **kw):
            return b"" if args[0] == "log" else ("newhead\n" if args[0] == "rev-parse" else "0\n")
        with mock.patch.object(hla, "_git", side_effect=fake_git), \
             mock.patch.object(hla, "resolve_base", return_value="oldbase"):
            hla.audit(self.state, None, dry_run=True)
        self.assertFalse(self.state.exists())

    def test_noop_when_head_equals_base(self):
        def fake_git(*args, **kw):
            return "samesha\n" if args[0] == "rev-parse" else b""
        with mock.patch.object(hla, "_git", side_effect=fake_git), \
             mock.patch.object(hla, "resolve_base", return_value="samesha"):
            out = hla.audit(self.state, None, dry_run=False)
        self.assertEqual(out["result"], "noop")


class ExitCodes(unittest.TestCase):
    """'Did not ask' (2) must never be conflated with 'asked and found nothing' (0)."""

    def test_violation_exits_1(self):
        with mock.patch.object(hla, "audit", return_value={
                "result": "violation", "base": "a" * 40, "head": "b" * 40,
                "commits": 2, "surface_bytes": 10, "violations": 1}):
            self.assertEqual(hla.main(["--json"]), 1)

    def test_clean_exits_0(self):
        with mock.patch.object(hla, "audit", return_value={
                "result": "clean", "base": "a" * 40, "head": "b" * 40,
                "commits": 2, "surface_bytes": 10, "violations": 0}):
            self.assertEqual(hla.main(["--json"]), 0)

    def test_audit_error_exits_2_not_0(self):
        with mock.patch.object(hla, "audit", side_effect=hla.AuditError("network down")):
            self.assertEqual(hla.main([]), 2)

    def test_git_failure_raises_rather_than_returning_partial(self):
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=128, stdout="", stderr="fatal: bad object")
            with self.assertRaises(hla.AuditError):
                hla._git("rev-parse", "nope")


if __name__ == "__main__":
    unittest.main()
