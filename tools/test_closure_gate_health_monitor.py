"""Tests for tools/closure_gate_health_monitor.py (T-2845 / ORG-065 BT5 flip
prerequisite: the ORG-068 "health-monitor" gap for the closure-validation
gate — nothing observed gate-mode drift or finding-volume trend ahead of the
warn->gate flip until this landed).

Local fixture helpers (own _write_ticket / _write_adr), NOT shared with
test_closure_gate.py — mirrors that file's own documented per-file-local-
helper convention (in turn mirroring test_typed_edges.py's).

STATE_PATH is isolated per test via mock.patch.object (same convention as
test_credential_expiry_monitor.py / test_disk_usage_monitor.py) so no test
ever touches the real ~/.local/state/pantheon/closure_gate_health.jsonl.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import closure_gate_health_monitor as cghm
from tools import ticket


def _write_ticket(
    path: Path,
    *,
    tid: str,
    state: str = "active",
    implements: list[str] | None = None,
    receipts: list[str] | None = None,
) -> None:
    lines = [
        "---",
        f"id: {tid}",
        "created: 2026-06-08T00:00:00Z",
        "requester: operator",
        "target: recovery",
        f"state: {state}",
        "parent_ticket: null",
        "depends_on: []",
    ]
    if implements:
        lines.append(f"implements: [{', '.join(implements)}]")
    if receipts:
        lines.append(f"receipts: [{', '.join(receipts)}]")
    lines.append("---")
    body = "\n# Test ticket\n\n## Ask\n\nx.\n\n## Transition log\n\n- filed\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + body, encoding="utf-8")


def _write_adr(path: Path, *, adr_id: str, elements_block: str) -> None:
    fm = ["---", f"id: {adr_id}", f"title: Test ADR {adr_id}", "elements:", elements_block, "---"]
    body = f"\n# {adr_id} — test ADR\n\nBody text.\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(fm) + body, encoding="utf-8")


_T2163_ELEMENTS_BLOCK = (
    "  - id: E1\n"
    "    title: two-clause obligation\n"
    "    clauses:\n"
    "      - {id: E1.C1, text: \"clause one\"}\n"
    "      - {id: E1.C2, text: \"clause two\"}\n"
)


class MonitorTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo_root = Path(self._tmp.name)
        (self.repo_root / "tickets" / "inbox").mkdir(parents=True)
        (self.repo_root / "tickets" / "active").mkdir(parents=True)
        (self.repo_root / "tickets" / "done").mkdir(parents=True)
        (self.repo_root / "architecture" / "adr").mkdir(parents=True)

        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop(ticket._CLOSURE_GATE_MODE_ENV, None)

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._tmp.cleanup()


class TestRun(MonitorTestBase):
    def test_clean_corpus_no_drift(self) -> None:
        report = cghm.run(self.repo_root)
        self.assertEqual(report.effective_mode, "gate")
        self.assertEqual(report.intended_mode, cghm.INTENDED_MODE)
        self.assertFalse(report.mode_drift)
        self.assertEqual(report.open_total_findings, 0)

    def test_effective_mode_reads_real_env_var(self) -> None:
        os.environ[ticket._CLOSURE_GATE_MODE_ENV] = "warn"
        report = cghm.run(self.repo_root)
        self.assertEqual(report.effective_mode, "warn")
        self.assertTrue(report.mode_drift)  # intended is "gate" post-flip (T-2934)

    def test_unsatisfied_clause_surfaces_in_open_rollup(self) -> None:
        _write_adr(self.repo_root / "architecture" / "adr" / "ADR-2842-test.md", adr_id="ADR-2842",
                    elements_block=_T2163_ELEMENTS_BLOCK)
        _write_ticket(
            self.repo_root / "tickets" / "active" / "T-0001.md", tid="T-0001",
            implements=["ADR-2842#E1"],
            receipts=["ADR-2842#E1.C1=ci-run:1=ci"],
        )
        report = cghm.run(self.repo_root)
        self.assertEqual(report.open_total_findings, 1)
        self.assertEqual(report.corpus_total_findings, 1)


class TestPrintReport(MonitorTestBase):
    def test_lines_carry_stable_prefix_and_expected_tokens(self) -> None:
        report = cghm.run(self.repo_root)
        with patch("sys.stdout") as mock_stdout:
            cghm._print_report(report)
        printed = "".join(c.args[0] for c in mock_stdout.write.call_args_list if c.args)
        for line_start in (
            "[closure-gate-health] mode effective=",
            "[closure-gate-health] open ",
            "[closure-gate-health] corpus ",
        ):
            with self.subTest(line_start=line_start):
                self.assertIn(line_start, printed)
        for st in ("inbox", "active", "done"):
            with self.subTest(state=st):
                self.assertIn(f"[closure-gate-health] by_state state={st}", printed)

    def test_drift_token_reflects_report_state(self) -> None:
        os.environ[ticket._CLOSURE_GATE_MODE_ENV] = "warn"
        report = cghm.run(self.repo_root)
        with patch("sys.stdout") as mock_stdout:
            cghm._print_report(report)
        printed = "".join(c.args[0] for c in mock_stdout.write.call_args_list if c.args)
        self.assertIn("drift=yes", printed)


class TestRecord(MonitorTestBase):
    def test_record_appends_one_jsonl_line_with_expected_fields(self) -> None:
        state_path = Path(self._tmp.name) / "state" / "closure_gate_health.jsonl"
        report = cghm.run(self.repo_root)
        cghm._record(report, state_path)
        lines = state_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertIn("ts", entry)
        self.assertEqual(entry["effective_mode"], "gate")
        self.assertEqual(entry["intended_mode"], cghm.INTENDED_MODE)
        self.assertEqual(entry["mode_drift"], False)
        self.assertEqual(entry["open_total_findings"], 0)
        self.assertIn("by_state", entry)

    def test_record_appends_across_multiple_runs_for_trend(self) -> None:
        state_path = Path(self._tmp.name) / "state" / "closure_gate_health.jsonl"
        cghm._record(cghm.run(self.repo_root), state_path)
        cghm._record(cghm.run(self.repo_root), state_path)
        lines = state_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            json.loads(line)  # each line independently parseable


class TestMain(MonitorTestBase):
    def test_default_invocation_does_not_write_state_file(self) -> None:
        state_path = Path(self._tmp.name) / "state" / "closure_gate_health.jsonl"
        with patch.object(cghm, "STATE_PATH", state_path):
            with patch("sys.stdout"):
                rc = cghm.main(["--repo-root", str(self.repo_root)])
        self.assertEqual(rc, 0)
        self.assertFalse(state_path.exists())

    def test_record_flag_writes_state_file(self) -> None:
        state_path = Path(self._tmp.name) / "state" / "closure_gate_health.jsonl"
        with patch.object(cghm, "STATE_PATH", state_path):
            with patch("sys.stdout"):
                rc = cghm.main(["--repo-root", str(self.repo_root), "--record"])
        self.assertEqual(rc, 0)
        self.assertTrue(state_path.exists())
        self.assertEqual(len(state_path.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
