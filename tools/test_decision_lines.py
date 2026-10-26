from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import check_decision_lines as check
from tools import decision_lines as dl
from tools import quant_calls_collect as qc
from tools import quant_delivery_collect as qd
from tools import ticket


class DecisionLineParserTests(unittest.TestCase):
    def test_parse_canonical_line_and_classify_via_ids(self):
        text = (
            "DECISION: keep calls collector separate | serves: D1 bandwidth | "
            "via: P1, D-7 ORG-102 ADR-230 T-4780 | ref: pantheon-config#820"
        )
        scan = dl.scan_decisions(text)
        self.assertEqual(scan.problems, ())
        self.assertEqual(len(scan.decisions), 1)
        got = scan.decisions[0]
        self.assertEqual(got.call, "keep calls collector separate")
        self.assertEqual(got.via_ids, ("P1", "D7", "ORG-102", "ADR-230", "T-4780"))
        self.assertEqual(got.via_classes, {"principle": 1, "directive": 1, "org": 1, "adr": 1, "ticket": 1})
        self.assertFalse(got.principle_only)

    def test_principle_only_means_only_p_ids(self):
        got = dl.scan_decisions("DECISION: choose x | serves: y | via: P1 P2 | ref: T-4780").decisions[0]
        self.assertTrue(got.principle_only)

    def test_marker_without_structured_line_is_unstructured_problem(self):
        scan = dl.scan_decisions("## DECISIONS\n\nchose x because P1")
        self.assertTrue(scan.marker_present)
        self.assertEqual(scan.decisions, ())
        self.assertEqual(scan.problems[0].message, "DECISIONS marker present but no structured DECISION line found")

    def test_lowercase_decision_word_is_not_marker(self):
        scan = dl.scan_decisions("this decision is ordinary prose")
        self.assertFalse(scan.marker_present)
        self.assertEqual(scan.problems, ())

    def test_lowercase_decision_line_is_not_structured(self):
        scan = dl.scan_decisions("decision: lowercase | serves: x | via: P1 | ref: T-4780")
        self.assertFalse(scan.marker_present)
        self.assertEqual(scan.decisions, ())
        self.assertEqual(scan.problems, ())

    def test_embedded_decision_substring_is_not_structured(self):
        scan = dl.scan_decisions("NODECISION: fake | serves: x | via: P1 | ref: T-4780")
        self.assertFalse(scan.marker_present)
        self.assertEqual(scan.decisions, ())
        self.assertEqual(scan.problems, ())

    def test_transition_log_decision_after_semicolon_is_structured(self):
        scan = dl.scan_decisions(
            "- 2026-10-01T00:00:00Z [mason] note; DECISION: use parser | serves: D1 | via: P1 | ref: T-4780"
        )
        self.assertEqual(len(scan.decisions), 1)

    def test_malformed_decision_line_reports_problem(self):
        scan = dl.scan_decisions("DECISION: missing fields")
        self.assertEqual(scan.problems[0].line_no, 1)
        self.assertIn("malformed", scan.problems[0].message)


class WarnOnlySurfaceTests(unittest.TestCase):
    def test_pr_check_warns_but_exits_zero(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = check.check_text("DECISIONS\nlegacy prose", surface="fixture")
        self.assertEqual(rc, 0)
        self.assertIn("::warning", out.getvalue())

    def test_pr_check_no_marker_is_silent(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = check.check_text("ordinary decision prose", surface="fixture")
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")

    def test_pr_check_self_gates_non_pull_request_event(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            event = Path(td) / "event.json"
            event.write_text(
                json.dumps(
                    {
                        "repository": {"name": "pantheon-config"},
                        "pull_request": {"number": 821, "body": "DECISION: malformed"},
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict("os.environ", {"GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(event)}, clear=False):
                with contextlib.redirect_stdout(out):
                    rc = check.main([])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "")

    def test_ticket_warning_path_never_raises_on_parser_crash(self):
        stderr = io.StringIO()
        fake = type("FakeDecisionLines", (), {})()
        fake.has_decision_marker = lambda _text: True
        fake.scan_decisions = lambda _text: (_ for _ in ()).throw(RuntimeError("boom"))
        with patch.object(ticket, "_decision_lines", fake):
            with contextlib.redirect_stderr(stderr):
                ticket._warn_decision_line_shape("DECISION: x", surface="fixture")
        self.assertIn("parser crashed", stderr.getvalue())


class CallsCollectorTests(unittest.TestCase):
    def test_ticket_records_cover_structured_unstructured_and_malformed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inbox = root / "inbox"
            plans = root / "plans"
            inbox.mkdir()
            plans.mkdir()
            (inbox / "T-1000.md").write_text(
                """---
id: T-1000
created: 2026-10-01T00:00:00Z
state: inbox
---

# Fixture

## Transition log

- 2026-10-01T00:00:00Z [mason] note; DECISION: use parser | serves: D1 | via: P1 T-1000 | ref: T-1000

## DECISIONS

legacy prose
DECISION: malformed
""",
                encoding="utf-8",
            )
            (inbox / "T-1001.md").write_text(
                """---
id: T-1001
created: 2026-10-01T00:00:00Z
state: inbox
---

# Legacy Fixture

## DECISIONS

legacy prose only
""",
                encoding="utf-8",
            )
            (plans / "ORG-052-implementation-plan.md").write_text(
                "DECISION: plan call | serves: D1 | via: ORG-102 | ref: T-4780\n",
                encoding="utf-8",
            )
            rows = qc.collect_ticket_records("run", "2026-10-09T00:00:00Z", root)
        statuses = [row["parse_status"] for row in rows]
        self.assertIn("structured", statuses)
        self.assertIn("unstructured", statuses)
        self.assertIn("malformed", statuses)
        structured = [row for row in rows if row["parse_status"] == "structured"][0]
        self.assertEqual(structured["via_classes"], {"principle": 1, "ticket": 1})
        self.assertEqual(structured["agent"], "mason")
        plan_rows = [row for row in rows if row["source_ref"].endswith("ORG-052-implementation-plan.md")]
        self.assertEqual(len(plan_rows), 1)
        self.assertEqual(plan_rows[0]["ticket_ids"], [])
        self.assertNotIn("ticket_id", plan_rows[0])

    def test_ledger_records_use_justifies_via_as_structured_source(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = Path(td)
            shard_dir = ledger / "2026-W41"
            shard_dir.mkdir()
            (shard_dir / "2026-W41.jsonl").write_text(
                json.dumps(
                    {
                        "id": "entry-1",
                        "ts": "2026-10-09T00:00:00Z",
                        "agent": "mason",
                        "action": "test_action",
                        "summary": "ledger summary",
                        "justifies_via": ["P4", "T-4780"],
                        "links": [{"type": "ticket", "ref": "T-4780"}],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            rows = qc.collect_ledger_records("run", "2026-10-09T00:00:00Z", ledger)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["parse_status"], "structured")
        self.assertEqual(rows[0]["via_classes"], {"principle": 1, "ticket": 1})
        self.assertFalse(rows[0]["principle_only"])

    def test_pr_records_parse_body_when_github_source_available(self):
        repo = qd.RepoSpec("pantheon-config", "owner/repo", None)
        fake_pr = {
            "repo": "pantheon-config",
            "number": 820,
            "body": "DECISION: parse PR | serves: D1 | via: ADR-230 | ref: pantheon-config#820",
            "title": "T-4780 (#820)",
            "head_ref": "agent/T-4780",
            "merged_at": "2026-10-09T00:00:00Z",
        }
        with patch.object(qd, "auth_token", return_value="token"):
            with patch.object(qd, "fetch_github_prs", return_value=([fake_pr], "ok", None)):
                rows, statuses, notes = qc.collect_pr_records(
                    "run", "2026-10-09T00:00:00Z", [repo], qd.parse_time("2026-10-01T00:00:00Z")
                )
        self.assertEqual(statuses, {"pantheon-config": "ok"})
        self.assertEqual(notes, {})
        self.assertEqual(rows[0]["source_ref"], "pantheon-config#820")
        self.assertEqual(rows[0]["via_classes"], {"adr": 1})


if __name__ == "__main__":
    unittest.main()
