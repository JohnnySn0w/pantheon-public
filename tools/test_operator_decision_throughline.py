"""Tests for tools/operator_decision_throughline.py (T-5015 phase 3). Fixtures only; no live data."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import operator_decision_throughline as tl  # noqa: E402

HEADER = ("| Date | Ticket | Ask (short) | Ruling | Category | Why it reached the operator | "
          "Rule it implies | Delegable next time? |\n|---|---|---|---|---|---|---|---|\n")


def row(date, ticket, ask="ask", cat="product", rule="rule", deleg="Yes"):
    return f"| {date} | {ticket} | {ask} | ruling | {cat} | why | {rule} | {deleg} |\n"


def register(*rows):
    return "# Register\n\nprose | with pipe\n\n" + HEADER + "".join(rows) + "\n## Open questions\n- x\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.reg = self.dir / "reg.md"
        self.state = self.dir / "state"
        self.wm = self.state / "throughline.json"

    def write(self, *rows):
        self.reg.write_text(register(*rows))

    def run_main(self, *extra, rc=0):
        argv = ["--register", str(self.reg), "--state-dir", str(self.state), *extra]
        with mock.patch.object(tl.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], rc, "", "boom")) as m:
            code = tl.main(argv)
        return code, m


class ParseTests(unittest.TestCase):
    def test_parses_rows_skips_header_and_prose(self):
        rows = tl.parse_register(register(row("2026-10-14", "T-1"), row("2026-10-15", "T-2")))
        self.assertEqual([r["ticket"] for r in rows], ["T-1", "T-2"])
        self.assertEqual(rows[0]["delegable"], "Yes")

    def test_escaped_pipe_stays_in_cell(self):
        rows = tl.parse_register(register(row("2026-10-14", "T-1", rule=r"use a \| b form")))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["rule"], "use a | b form")
        self.assertEqual(rows[0]["delegable"], "Yes")

    def test_wrong_cell_count_skipped(self):
        self.assertEqual(tl.parse_register("| 2026-10-14 | T-1 | only |\n"), [])

    def test_row_key_ignores_prose_edits_to_rule(self):
        a = tl.parse_register(register(row("2026-10-14", "T-1", rule="one")))[0]
        b = tl.parse_register(register(row("2026-10-14", "T-1", rule="two")))[0]
        self.assertEqual(tl.row_key(a), tl.row_key(b))


class RunTests(Base):
    def test_below_threshold_no_ticket_no_watermark(self):
        self.write(row("2026-10-14", "T-1"), row("2026-10-15", "T-2"))
        code, m = self.run_main()
        self.assertEqual(code, 0)
        m.assert_not_called()
        self.assertFalse(self.wm.exists())

    def test_at_threshold_files_one_ticket_and_advances(self):
        self.write(row("2026-10-14", "T-1", rule="R1"), row("2026-10-15", "T-2"), row("2026-10-16", "T-3"))
        code, m = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(m.call_count, 1)
        cmd = m.call_args[0][0]
        self.assertIn("file", cmd)
        self.assertEqual(cmd[cmd.index("--target") + 1], "role:engineering_orchestrator")
        self.assertEqual(cmd[cmd.index("--project-id") + 1], "null")
        self.assertEqual(cmd[cmd.index("--nearest-intent-ref") + 1], "maintenance:decision-register")
        self.assertIn("--serves", cmd)
        body = cmd[cmd.index("--ask") + 1]
        self.assertIn("3 new row(s)", body)
        self.assertIn("2026-10-14 to 2026-10-16", body)
        self.assertIn("T-1 — R1", body)
        self.assertIn("product:", body)
        self.assertIn("cluster by implied rule", body)
        saved = json.loads(self.wm.read_text())
        self.assertEqual(saved["count"], 3)
        self.assertEqual(saved["last_date"], "2026-10-16")

    def test_second_run_without_new_rows_is_quiet(self):
        self.write(row("2026-10-14", "T-1"), row("2026-10-15", "T-2"), row("2026-10-16", "T-3"))
        self.run_main()
        code, m = self.run_main()
        self.assertEqual(code, 0)
        m.assert_not_called()

    def test_only_new_rows_counted_after_pass(self):
        base = [row("2026-10-14", "T-1"), row("2026-10-15", "T-2"), row("2026-10-16", "T-3")]
        self.write(*base)
        self.run_main()
        self.write(*base, row("2026-10-17", "T-4"), row("2026-10-18", "T-5"))
        code, m = self.run_main()
        m.assert_not_called()  # 2 new < 3
        self.write(*base, row("2026-10-17", "T-4"), row("2026-10-18", "T-5"), row("2026-10-19", "T-6"))
        code, m = self.run_main()
        body = m.call_args[0][0][m.call_args[0][0].index("--ask") + 1]
        self.assertIn("3 new row(s)", body)
        self.assertNotIn("T-1 —", body)

    def test_failure_leaves_watermark_unchanged(self):
        self.write(row("2026-10-14", "T-1"), row("2026-10-15", "T-2"), row("2026-10-16", "T-3"))
        code, m = self.run_main(rc=1)
        self.assertEqual(code, 1)
        self.assertEqual(m.call_count, 1)
        self.assertFalse(self.wm.exists())

    def test_dry_run_files_nothing_writes_nothing(self):
        self.write(row("2026-10-14", "T-1"), row("2026-10-15", "T-2"), row("2026-10-16", "T-3"))
        code, m = self.run_main("--dry-run")
        self.assertEqual(code, 0)
        m.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_min_rows_flag(self):
        self.write(row("2026-10-14", "T-1"))
        code, m = self.run_main("--min-rows", "1")
        self.assertEqual(m.call_count, 1)

    def test_missing_register_returns_2(self):
        code, m = self.run_main()
        self.assertEqual(code, 2)
        m.assert_not_called()

    def test_corrupt_watermark_treated_as_empty(self):
        self.state.mkdir()
        self.wm.write_text("{not json")
        self.assertEqual(tl.load_watermark(self.wm), set())


if __name__ == "__main__":
    unittest.main()
