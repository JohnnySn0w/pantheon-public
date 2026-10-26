from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools import quant_outcomes_collect as qo


def call_row(**kw):
    base = {
        "source_type": "ticket",
        "source_ref": "tickets/done/T-1000.md",
        "parse_status": "structured",
        "call": "ship the thing",
        "ref": "T-1000",
        "ticket_ids": ["T-1000"],
        "via_ids": ["P4"],
        "via_classes": {"principle": 1},
        "line_no": 12,
        "raw_excerpt": "- 2026-10-01T00:00:00Z [mason] note; DECISION: ship | serves: x | via: P4 | ref: T-1000",
        "raw_excerpt_sha256": "abc",
    }
    base.update(kw)
    return base


def write_ticket(root: Path, tid: str, body: str, state: str = "done") -> Path:
    base = root / state
    base.mkdir(exist_ok=True)
    path = base / f"{tid}.md"
    path.write_text(
        f"""---
id: {tid}
created: 2026-10-01T00:00:00Z
state: {state}
---

# Fixture

## Transition log

{body}
""",
        encoding="utf-8",
    )
    return path


class OutcomeDetectorTests(unittest.TestCase):
    def test_hold_window_marks_pending_before_held(self):
        row = call_row()
        early = qo.classify_outcome(row, generated_at="2026-10-10T00:00:00Z", tickets={}, ledger_entries=[], prs=[])
        late = qo.classify_outcome(row, generated_at="2026-10-20T00:00:00Z", tickets={}, ledger_entries=[], prs=[])
        self.assertEqual(early["outcome"], "pending")
        self.assertFalse(early["eligible_for_h1"])
        self.assertEqual(late["outcome"], "held")
        self.assertTrue(late["eligible_for_h1"])

    def test_rebound_after_call_reverses_same_ticket_call(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_ticket(root, "T-1000", "- 2026-10-02T00:00:00Z [orchestrator] note @abc; REBOUND #1: fix it.\n")
            tickets = qo.load_ticket_docs(root)
        got = qo.classify_outcome(
            call_row(), generated_at="2026-10-20T00:00:00Z", tickets=tickets, ledger_entries=[], prs=[]
        )
        self.assertEqual(got["outcome"], "reversed")
        self.assertEqual(got["evidence"][0]["kind"], "rebound")

    def test_ledger_id_timestamp_filters_pre_call_ticket_prose(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_ticket(root, "T-1000", "- 2026-10-01T23:59:00Z [orchestrator] resolved; REBOUND #1: earlier review round.\n")
            tickets = qo.load_ticket_docs(root)
        got = qo.classify_outcome(
            call_row(
                source_type="ledger",
                source_ref="2026-10-02T00:00:00Z-sample",
                ledger_id="2026-10-02T00:00:00Z-sample",
                raw_excerpt=None,
            ),
            generated_at="2026-10-20T00:00:00Z",
            tickets=tickets,
            ledger_entries=[],
            prs=[],
        )
        self.assertEqual(got["call_occurred_at"], "2026-10-02T00:00:00Z")
        self.assertEqual(got["outcome"], "held")

    def test_explanatory_rebound_word_does_not_reverse_call(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_ticket(
                root,
                "T-1000",
                "- 2026-10-02T00:00:00Z [orchestrator] note @abc; Scope add: outcome per call -- held / reversed (REBOUND, rollback, superseded).\n"
                "- 2026-10-03T00:00:00Z [orchestrator] resolved @def; Landed after reviewer round 1 REBOUNDed on an earlier draft.\n"
                "- 2026-10-04T00:00:00Z [orchestrator] note @ghi; Verdict remains REBOUND: historical report only.\n",
            )
            tickets = qo.load_ticket_docs(root)
        got = qo.classify_outcome(
            call_row(), generated_at="2026-10-20T00:00:00Z", tickets=tickets, ledger_entries=[], prs=[]
        )
        self.assertEqual(got["outcome"], "held")
        self.assertEqual(got["evidence"], [])

    def test_empty_ledger_supersedes_key_is_not_reversal_evidence(self):
        index = qo.ledger_supersede_index(
            [
                {
                    "id": "2026-10-01T00:00:00Z-001",
                    "ts": "2026-10-01T00:00:00Z",
                    "action": "record_initial_decision",
                    "rationale": "stable",
                    "justifies_via": ["P4"],
                    "links": [{"type": "telegram", "ref": "msg_1"}],
                    "supersedes": [],
                }
            ]
        )
        self.assertEqual(index, [])

    def test_ledger_links_are_not_supersession_targets(self):
        index = qo.ledger_supersede_index(
            [
                {
                    "id": "2026-10-01T00:00:00Z-001",
                    "ts": "2026-10-01T00:00:00Z",
                    "action": "record_replacement",
                    "rationale": "stable",
                    "justifies_via": ["P4"],
                    "links": [{"type": "telegram", "ref": "msg_1"}],
                    "supersedes": ["T-0999"],
                }
            ]
        )
        row = call_row(source_type="ledger", source_ref="2026-09-30T00:00:00Z-001", ref="msg_1", ticket_ids=[])
        self.assertEqual(qo.ledger_evidence(row, index), [])
        row["ref"] = "T-0999"
        self.assertEqual(qo.ledger_evidence(row, index)[0].kind, "ledger_superseded")

    def test_prior_ledger_supersession_does_not_reverse_later_call(self):
        index = qo.ledger_supersede_index(
            [
                {
                    "id": "2026-10-01T00:00:00Z-001",
                    "ts": "2026-10-01T00:00:00Z",
                    "action": "record_replacement",
                    "summary": "replace the earlier call",
                    "supersedes": ["T-1000"],
                }
            ]
        )
        got = qo.classify_outcome(
            call_row(
                source_type="ledger",
                source_ref="2026-10-02T00:00:00Z-later",
                ledger_id="2026-10-02T00:00:00Z-later",
                raw_excerpt=None,
            ),
            generated_at="2026-10-20T00:00:00Z",
            tickets={},
            ledger_entries=index,
            prs=[],
        )
        self.assertEqual(got["outcome"], "held")
        self.assertEqual(got["evidence"], [])

    def test_self_ledger_supersession_does_not_reverse_own_call(self):
        index = qo.ledger_supersede_index(
            [
                {
                    "id": "2026-10-02T00:00:00Z-001",
                    "ts": "2026-10-02T00:00:00Z",
                    "action": "record_replacement",
                    "summary": "replace the earlier call",
                    "supersedes": ["T-1000"],
                }
            ]
        )
        got = qo.classify_outcome(
            call_row(
                source_type="ledger",
                source_ref="2026-10-02T00:00:00Z-001",
                ledger_id="2026-10-02T00:00:00Z-001",
                raw_excerpt=None,
            ),
            generated_at="2026-10-20T00:00:00Z",
            tickets={},
            ledger_entries=index,
            prs=[],
        )
        self.assertEqual(got["outcome"], "held")
        self.assertEqual(got["evidence"], [])

    def test_ticket_superseded_prose_only_reverses_same_ticket_call(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_ticket(root, "T-1000", "- 2026-10-02T00:00:00Z [orchestrator] resolved @abc; closed as SUPERSEDED by T-1001.\n")
            tickets = qo.load_ticket_docs(root)
        ledger = qo.classify_outcome(
            call_row(
                source_type="ledger",
                source_ref="2026-10-01T00:00:00Z-sample",
                ledger_id="2026-10-01T00:00:00Z-sample",
                ref="T-1000",
                raw_excerpt=None,
            ),
            generated_at="2026-10-20T00:00:00Z",
            tickets=tickets,
            ledger_entries=[],
            prs=[],
        )
        same_ticket = qo.classify_outcome(
            call_row(),
            generated_at="2026-10-20T00:00:00Z",
            tickets=tickets,
            ledger_entries=[],
            prs=[],
        )
        self.assertEqual(ledger["outcome"], "held")
        self.assertEqual(same_ticket["outcome"], "reversed")
        self.assertEqual(same_ticket["evidence"][0]["kind"], "superseded")

    def test_operator_override_requires_strong_cue_not_pending_or_bare_no(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            write_ticket(
                root,
                "T-1000",
                "- 2026-10-02T00:00:00Z [orchestrator] note @abc; the operator TG 1: pending answer, not ready yet.\n"
                "- 2026-10-03T00:00:00Z [orchestrator] note @def; the operator TG 2: ignore that, do the other path instead.\n",
            )
            tickets = qo.load_ticket_docs(root)
        first = qo.classify_outcome(
            call_row(), generated_at="2026-10-02T12:00:00Z", tickets=tickets, ledger_entries=[], prs=[]
        )
        second = qo.classify_outcome(
            call_row(), generated_at="2026-10-20T00:00:00Z", tickets=tickets, ledger_entries=[], prs=[]
        )
        self.assertEqual(first["outcome"], "pending")
        self.assertEqual(second["outcome"], "overridden")
        self.assertEqual(second["evidence"][0]["kind"], "override_operator_tg")

    def test_t4294_is_negative_and_named_positives_are_calibrated(self):
        summary = qo.calibration_summary(qo.load_ticket_docs())
        cases = {case["id"]: case for case in summary["cases"]}
        self.assertTrue(cases["T-4294-negative-reask"]["ok"])
        self.assertFalse(cases["T-4294-negative-reask"]["detected"])
        self.assertTrue(cases["TG-11534-T-4750-override"]["detected"])
        self.assertTrue(cases["TG-11543-T-4446-override"]["detected"])
        self.assertTrue(cases["TG-11543-T-4512-override"]["detected"])
        self.assertTrue(cases["rebound-805-T-4763"]["detected"])
        self.assertTrue(cases["rebound-815-T-4790"]["detected"])
        self.assertFalse(cases["rebound-821-T-4780"]["detected"])
        self.assertFalse(cases["rebound-T-4780-step1"]["detected"])
        self.assertIn("TG-11575-stale-sweep", summary["misses"])
        self.assertIn("rebound-821-T-4780", summary["misses"])
        self.assertIn("rebound-T-4780-step1", summary["misses"])

    def test_h1_groups_use_eligible_outcomes_and_report_insufficient_n(self):
        outcomes = [
            {"outcome": "held", "eligible_for_h1": True, "via_ids": ["P4"], "call_id": "a"},
            {"outcome": "reversed", "eligible_for_h1": True, "via_ids": ["D1"], "call_id": "b"},
            {"outcome": "pending", "eligible_for_h1": False, "via_ids": ["P4"], "call_id": "c"},
            {"outcome": "overridden", "eligible_for_h1": True, "via_ids": ["ORG-102"], "call_id": "d"},
            {"outcome": "held", "eligible_for_h1": True, "via_ids": ["P9", "T-4780"], "call_id": "e"},
        ]
        rows, never = qo.h1_group_records(outcomes, "run", "2026-10-20T00:00:00Z")
        by_group = {row["group"]: row for row in rows}
        self.assertEqual(by_group["ALL"]["calls"], 4)
        self.assertEqual(by_group["ALL"]["adverse"], 2)
        self.assertEqual(by_group["cited_pd"]["verdict"], "insufficient-n")
        self.assertEqual(by_group["uncited_pd"]["calls"], 2)
        self.assertIn("P1", never)
        self.assertEqual(by_group["P4"]["calls"], 1)

    def test_outcome_record_carries_agent_for_h2_stratification(self):
        record = qo.outcome_record(
            call_row(agent="mason"),
            {"outcome": "held", "eligible_for_h1": True, "evidence": []},
            "run",
            "2026-10-20T00:00:00Z",
        )
        self.assertEqual(record["agent"], "mason")


if __name__ == "__main__":
    unittest.main()
