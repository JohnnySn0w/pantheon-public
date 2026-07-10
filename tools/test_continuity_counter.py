"""tests for tools/continuity_counter.py — T-0445 Piece 1."""

from __future__ import annotations

import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure repo root on sys.path for module-style imports.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# TestLoadBreaks
# ---------------------------------------------------------------------------

class TestLoadBreaks(unittest.TestCase):
    """_load_breaks reads continuity_breaks.jsonl; skips malformed; missing → []."""

    def _call(self, path):
        from tools.continuity_counter import _load_breaks
        return _load_breaks(path)

    def test_empty_file_returns_empty_list(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("")
            tmp = Path(f.name)
        try:
            result = self._call(tmp)
            self.assertEqual(result, [])
        finally:
            tmp.unlink(missing_ok=True)

    def test_valid_single_break(self):
        entry = {
            "start_iso": "2026-05-01T00:00:00Z",
            "end_iso": "2026-05-03T00:00:00Z",
            "cause": "test outage",
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(entry) + "\n")
            tmp = Path(f.name)
        try:
            result = self._call(tmp)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["start_iso"], entry["start_iso"])
            self.assertEqual(result[0]["end_iso"], entry["end_iso"])
            self.assertEqual(result[0]["cause"], entry["cause"])
        finally:
            tmp.unlink(missing_ok=True)

    def test_malformed_line_skipped_silently(self):
        """One malformed line + one valid line → only valid line returned."""
        valid = {
            "start_iso": "2026-05-01T00:00:00Z",
            "end_iso": "2026-05-03T00:00:00Z",
            "cause": "real outage",
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("this is not json at all\n")
            f.write(json.dumps(valid) + "\n")
            tmp = Path(f.name)
        try:
            result = self._call(tmp)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["cause"], "real outage")
        finally:
            tmp.unlink(missing_ok=True)

    def test_missing_file_returns_empty_list(self):
        """Non-existent path → [] with no exception (missing == no breaks)."""
        result = self._call(Path("/tmp/no_such_continuity_breaks_file.jsonl"))
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# TestCompute
# ---------------------------------------------------------------------------

class TestCompute(unittest.TestCase):
    """_compute(now_ts, genesis_iso, breaks) → output dict with correct semantics."""

    # 2026-04-28T00:00:00Z as Unix epoch
    GENESIS_ISO = "2026-04-28T00:00:00+00:00"
    GENESIS_EPOCH = 1777334400  # verifiable: python3 -c "from datetime import datetime,timezone; print(int(datetime(2026,4,28,tzinfo=timezone.utc).timestamp()))"

    def _call(self, now_ts, genesis_iso=None, breaks=None):
        from tools.continuity_counter import _compute
        if genesis_iso is None:
            genesis_iso = self.GENESIS_ISO
        if breaks is None:
            breaks = []
        return _compute(now_ts, genesis_iso, breaks)

    def test_empty_breaks_streak_equals_total(self):
        """No breaks: streak_start == genesis, streak_days == total_days."""
        # 20 days after genesis
        now_ts = self.GENESIS_EPOCH + 20 * 86400
        result = self._call(now_ts)
        self.assertEqual(result["current_streak_days"], 20)
        self.assertEqual(result["total_days_since_genesis"], 20)
        self.assertEqual(result["breaks"], [])
        self.assertEqual(result["schema_version"], 1)

    def test_single_break_shifts_streak_start(self):
        """Break end_iso becomes new streak start; streak_days < total_days."""
        break_end_iso = "2026-05-03T00:00:00+00:00"
        breaks = [
            {
                "start_iso": "2026-05-01T00:00:00+00:00",
                "end_iso": break_end_iso,
                "cause": "test outage",
            }
        ]
        # now = 10 days after genesis; break end = 5 days after genesis
        now_ts = self.GENESIS_EPOCH + 10 * 86400
        result = self._call(now_ts, breaks=breaks)

        total_days = 10
        streak_days = 5  # now - break_end = 10d - 5d
        self.assertEqual(result["total_days_since_genesis"], total_days)
        self.assertEqual(result["current_streak_days"], streak_days)
        self.assertEqual(len(result["breaks"]), 1)
        # current_streak_start_iso must differ from genesis_iso
        self.assertNotEqual(
            result["current_streak_start_iso"],
            result["genesis_iso"],
        )

    def test_day_counting_floor(self):
        """23h59m elapsed = 0 days (floor division, not round)."""
        now_ts = self.GENESIS_EPOCH + (23 * 3600 + 59 * 60)
        result = self._call(now_ts)
        self.assertEqual(result["current_streak_days"], 0)
        self.assertEqual(result["total_days_since_genesis"], 0)

    def test_exactly_one_day(self):
        """86400 seconds = exactly 1 day."""
        now_ts = self.GENESIS_EPOCH + 86400
        result = self._call(now_ts)
        self.assertEqual(result["current_streak_days"], 1)

    def test_future_break_end_clamps_streak_to_zero(self):
        """Break with end_iso in the future: streak_days clamped to 0 (not negative)."""
        # now = 3 days after genesis; break end = 10 days after genesis (future)
        now_ts = self.GENESIS_EPOCH + 3 * 86400
        breaks = [
            {
                "start_iso": "2026-04-29T00:00:00+00:00",
                "end_iso": "2026-05-08T00:00:00+00:00",  # future relative to now_ts
                "cause": "ongoing/future break",
            }
        ]
        result = self._call(now_ts, breaks=breaks)
        # streak_days must not be negative
        self.assertGreaterEqual(result["current_streak_days"], 0)

    def test_schema_version_is_1(self):
        result = self._call(self.GENESIS_EPOCH + 86400)
        self.assertEqual(result["schema_version"], 1)

    def test_output_keys_present(self):
        """All 7 required keys present in output dict."""
        result = self._call(self.GENESIS_EPOCH + 86400)
        expected_keys = {
            "genesis_iso",
            "now_iso",
            "current_streak_start_iso",
            "current_streak_days",
            "total_days_since_genesis",
            "breaks",
            "schema_version",
        }
        self.assertEqual(set(result.keys()), expected_keys)


# ---------------------------------------------------------------------------
# TestFormat
# ---------------------------------------------------------------------------

class TestFormat(unittest.TestCase):
    """Format functions produce correct output shapes."""

    def _sample_data(self):
        from tools.continuity_counter import _compute
        genesis_iso = "2026-04-28T00:00:00+00:00"
        genesis_epoch = 1777334400
        now_ts = genesis_epoch + 20 * 86400
        return _compute(now_ts, genesis_iso, [])

    def test_format_json_is_valid_json_with_all_keys(self):
        from tools.continuity_counter import _format_json
        data = self._sample_data()
        output = _format_json(data)
        parsed = json.loads(output)
        self.assertIn("genesis_iso", parsed)
        self.assertIn("current_streak_days", parsed)
        self.assertIn("schema_version", parsed)
        self.assertEqual(parsed["schema_version"], 1)

    def test_format_jsonl_is_single_data_line(self):
        """jsonl output: one JSON object line + trailing newline, no mid-string newlines."""
        from tools.continuity_counter import _format_jsonl
        data = self._sample_data()
        output = _format_jsonl(data)
        # Strip trailing newline; remaining should be parseable single-line JSON
        lines = output.rstrip("\n").split("\n")
        self.assertEqual(len(lines), 1)
        parsed = json.loads(lines[0])
        self.assertIn("current_streak_days", parsed)

    def test_format_text_contains_streak_days(self):
        """text output mentions the streak day count as an integer string."""
        from tools.continuity_counter import _format_text
        data = self._sample_data()
        output = _format_text(data)
        streak = str(data["current_streak_days"])
        self.assertIn(streak, output)
        # Should be human-readable (non-empty, has some label context)
        self.assertGreater(len(output.strip()), len(streak))


# ---------------------------------------------------------------------------
# TestMain
# ---------------------------------------------------------------------------

class TestMain(unittest.TestCase):
    """Integration: main() wires argparse + subprocess + file reads correctly."""

    GENESIS_STDOUT = "2026-04-28T00:00:00+00:00\n"

    def _mock_subprocess(self, mock_sub):
        mock_sub.run.return_value = MagicMock(
            returncode=0,
            stdout=self.GENESIS_STDOUT,
            stderr="",
        )
        mock_sub.CalledProcessError = __import__("subprocess").CalledProcessError

    def test_main_json_format_returns_zero_and_valid_json(self):
        """main(["--format","json"]) exits 0 and stdout is parseable JSON."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("")
            breaks_path = Path(f.name)
        try:
            captured = io.StringIO()
            with patch("tools.continuity_counter.subprocess") as mock_sub, \
                 patch("tools.continuity_counter.BREAKS_PATH", breaks_path), \
                 patch("sys.stdout", captured):
                self._mock_subprocess(mock_sub)
                from tools.continuity_counter import main
                rc = main(["--format", "json"])
            self.assertEqual(rc, 0)
            parsed = json.loads(captured.getvalue())
            self.assertIn("current_streak_days", parsed)
            self.assertGreaterEqual(parsed["current_streak_days"], 0)
        finally:
            breaks_path.unlink(missing_ok=True)

    def test_main_default_text_returns_zero(self):
        """main([]) (default text format) exits 0, stdout non-empty."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("")
            breaks_path = Path(f.name)
        try:
            captured = io.StringIO()
            with patch("tools.continuity_counter.subprocess") as mock_sub, \
                 patch("tools.continuity_counter.BREAKS_PATH", breaks_path), \
                 patch("sys.stdout", captured):
                self._mock_subprocess(mock_sub)
                from tools.continuity_counter import main
                rc = main([])
            self.assertEqual(rc, 0)
            self.assertGreater(len(captured.getvalue().strip()), 0)
        finally:
            breaks_path.unlink(missing_ok=True)

    def test_main_jsonl_format_single_line(self):
        """main(["--format","jsonl"]) stdout is a single parseable JSON line."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write("")
            breaks_path = Path(f.name)
        try:
            captured = io.StringIO()
            with patch("tools.continuity_counter.subprocess") as mock_sub, \
                 patch("tools.continuity_counter.BREAKS_PATH", breaks_path), \
                 patch("sys.stdout", captured):
                self._mock_subprocess(mock_sub)
                from tools.continuity_counter import main
                rc = main(["--format", "jsonl"])
            self.assertEqual(rc, 0)
            lines = captured.getvalue().rstrip("\n").split("\n")
            self.assertEqual(len(lines), 1)
            json.loads(lines[0])  # must parse
        finally:
            breaks_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
