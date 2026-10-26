"""Tests for the T-2842 / ORG-065 BT2 closure-validation gate.

Run from repo root:
    python3 -m unittest tools.test_closure_gate -v

Covers:
  - tools/ticket_lint.py: receipt shape decoding (decode_receipt), the ADR
    obligation detail index (load_adr_obligation_index), and the core
    closure-validation algorithm (evaluate_closure_fields /
    evaluate_closure_for_ticket) — the T-2163 two-clause leak scenario is
    the acceptance test.
  - tools/ticket.py: --receipt CLI wiring on `resolve`, shape validation
    (_parse_receipt_entries), and the warn/gate mode-flag enforcement hook
    inside _do_transition (PANTHEON_CLOSURE_GATE_MODE).
  - T-2845 / ORG-065 BT5 flip prerequisites: three regression-guard tests for
    already-correct behavior the T-2842 reviewer named as nice-to-haves
    (garbage mode-value fallback, mixed self+ci provenance, clause-level
    high_risk override) — see each test's own docstring. tools/ticket_lint.py:
    detect_closure_gate_health corpus-health aggregation is covered in its
    own test class here; the standalone tools/closure_gate_health_monitor.py
    CLI has its own test module (test_closure_gate_health_monitor.py).

Fixture-string discipline: no test constructs the contiguous unescaped
HERMES trigger bytes. Per /home/claude/CLAUDE.md "HERMES\\.md byte-sequence
discipline" (escape form in prose, mentally drop the backslash) — moot here
since nothing in this module references that filename at all, but noted per
the dispatch brief's standing instruction to workers.

Local fixture helpers (own _write_ticket / _write_adr), NOT shared with
test_typed_edges.py / test_ticket.py / test_ticket_lint.py — mirrors the
per-file-local-helper convention those modules already established (see
test_typed_edges.py's own docstring), so BT1's landed test file stays
untouched.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tools import ticket, ticket_lint
from tools._test_fixtures import contained_path


# ---------------------------------------------------------------------------
# Fixture helpers (local to this module).
# ---------------------------------------------------------------------------


def _write_ticket(
    path: Path,
    *,
    tid: str,
    state: str = "active",
    target: str = "recovery",
    requester: str = "operator",
    created: str = "2026-06-08T00:00:00Z",
    implements: list[str] | None = None,
    verifies: list[str] | None = None,
    receipts: list[str] | None = None,
) -> None:
    """Write a minimal, well-formed ticket file — including implements/
    verifies/receipts when given — directly to disk (bypassing ticket.py's
    own insertion-time validation, matching test_typed_edges.py's
    _write_ticket convention)."""
    lines = [
        "---",
        f"id: {tid}",
        f"created: {created}",
        f"requester: {requester}",
        f"target: {target}",
        f"state: {state}",
        "parent_ticket: null",
        "depends_on: []",
    ]
    if implements:
        lines.append(f"implements: [{', '.join(implements)}]")
    if verifies:
        lines.append(f"verifies: [{', '.join(verifies)}]")
    if receipts:
        lines.append(f"receipts: [{', '.join(receipts)}]")
    lines.append("---")
    body = (
        f"\n# Test ticket {tid}\n\n"
        "## Ask\n\nx.\n\n"
        "## Transition log\n\n"
        f"- {created} [{requester}] filed\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + body, encoding="utf-8")


def _write_adr(path: Path, *, adr_id: str, elements_block: str) -> None:
    """Write a minimal ADR file with a literal `elements:` YAML block
    (mirrors test_typed_edges.py's _write_adr)."""
    fm = ["---", f"id: {adr_id}", f"title: Test ADR {adr_id}", "elements:", elements_block, "---"]
    body = f"\n# {adr_id} — test ADR\n\nBody text.\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(fm) + body, encoding="utf-8")


# The T-2163 scenario: one element (E1) with two clauses (C1, C2).
_T2163_ELEMENTS_BLOCK = (
    "  - id: E1\n"
    "    title: two-clause obligation\n"
    "    clauses:\n"
    "      - {id: E1.C1, text: \"clause one\"}\n"
    "      - {id: E1.C2, text: \"clause two\"}\n"
)

_HIGH_RISK_ELEMENTS_BLOCK = (
    "  - id: E1\n"
    "    title: high-risk obligation\n"
    "    high_risk: true\n"
    "    clauses:\n"
    "      - {id: E1.C1, text: \"sensitive clause\"}\n"
)

# T-2845 / ORG-065 BT5 flip prerequisite (c): element high_risk: true, one
# clause EXPLICITLY overriding to false (C1), one clause with no override so
# it inherits the element's true (C2) — both directions of the override rule
# in one fixture.
_MIXED_HIGH_RISK_ELEMENTS_BLOCK = (
    "  - id: E1\n"
    "    title: mixed high-risk overrides\n"
    "    high_risk: true\n"
    "    clauses:\n"
    "      - {id: E1.C1, text: \"explicitly low-risk clause\", high_risk: false}\n"
    "      - {id: E1.C2, text: \"inherits element high-risk\"}\n"
)

# T-3578 / ORG-065 cross-repo closure-gate fix: an ECHO-shaped ADR obligation
# (ADR-NNN namespace, disjoint from pantheon's ORG-NNN) living in a SIBLING
# repo's adr/ — e.g. echo-prismatic's ADR-216#E4 (T-1213's cross-repo target).
# Single element, no clauses (its own atomic obligation unit), not high-risk.
_ECHO_E4_ELEMENTS_BLOCK = (
    "  - id: E4\n"
    "    title: echo cross-repo obligation\n"
)

# A pantheon-namespace (ORG-*) obligation, for the integrity test that a
# genuinely-unsatisfied pantheon obligation STILL blocks even when the echo
# dir is unscannable (the warn-fallback must only touch echo-namespace ids).
_ORG_E1_ELEMENTS_BLOCK = (
    "  - id: E1\n"
    "    title: pantheon obligation\n"
)


# ---------------------------------------------------------------------------
# ticket_lint.py: decode_receipt shape validation.
# ---------------------------------------------------------------------------


class TestReceiptEntryShape(unittest.TestCase):
    def test_well_formed_receipt_decodes(self) -> None:
        r = ticket_lint.decode_receipt("ADR-2842#E1.C1=ci-run:12345=ci")
        self.assertEqual(r.clause, "ADR-2842#E1.C1")
        self.assertEqual(r.receipt_id, "ci-run:12345")
        self.assertEqual(r.provenance, "ci")

    def test_missing_parts_rejected(self) -> None:
        for bad in ("ADR-2842#E1.C1=ci-run:12345", "a=b=c=d", "", "justastring"):
            with self.subTest(bad=bad):
                self.assertIsNone(ticket_lint.decode_receipt(bad))

    def test_bad_namespace_rejected(self) -> None:
        self.assertIsNone(
            ticket_lint.decode_receipt("ADR-2842#E1.C1=bogus-ns:12345=ci")
        )

    def test_bad_provenance_rejected(self) -> None:
        self.assertIsNone(
            ticket_lint.decode_receipt("ADR-2842#E1.C1=ci-run:12345=made-up")
        )

    def test_malformed_clause_id_rejected(self) -> None:
        self.assertIsNone(
            ticket_lint.decode_receipt("not-a-clause=ci-run:12345=ci")
        )


# ---------------------------------------------------------------------------
# ticket_lint.py: load_adr_obligation_index.
# ---------------------------------------------------------------------------


class TestObligationIndex(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.adr_dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_element_with_clauses_indexed(self) -> None:
        _write_adr(self.adr_dir / "ADR-2842-test.md", adr_id="ADR-2842",
                    elements_block=_T2163_ELEMENTS_BLOCK)
        idx = ticket_lint.load_adr_obligation_index(self.adr_dir)
        self.assertEqual(
            idx.element_clauses["ADR-2842#E1"],
            ["ADR-2842#E1.C1", "ADR-2842#E1.C2"],
        )
        self.assertEqual(idx.details["ADR-2842#E1.C1"].text, "clause one")
        self.assertEqual(idx.details["ADR-2842#E1.C2"].text, "clause two")

    def test_element_without_clauses_is_its_own_atomic_unit(self) -> None:
        _write_adr(self.adr_dir / "ADR-2900-test.md", adr_id="ADR-2900",
                    elements_block="  - id: E1\n    title: no sub-clauses\n")
        idx = ticket_lint.load_adr_obligation_index(self.adr_dir)
        self.assertEqual(idx.element_clauses["ADR-2900#E1"], [])
        self.assertEqual(idx.details["ADR-2900#E1"].text, "no sub-clauses")

    def test_high_risk_flag_on_clause(self) -> None:
        _write_adr(self.adr_dir / "ADR-3000-test.md", adr_id="ADR-3000",
                    elements_block=_HIGH_RISK_ELEMENTS_BLOCK)
        idx = ticket_lint.load_adr_obligation_index(self.adr_dir)
        self.assertTrue(idx.details["ADR-3000#E1.C1"].high_risk)
        self.assertTrue(idx.details["ADR-3000#E1"].high_risk)

    def test_high_risk_inherited_from_element_when_clause_unset(self) -> None:
        _write_adr(self.adr_dir / "ADR-3001-test.md", adr_id="ADR-3001",
                    elements_block=_HIGH_RISK_ELEMENTS_BLOCK)
        idx = ticket_lint.load_adr_obligation_index(self.adr_dir)
        # The clause dict in _HIGH_RISK_ELEMENTS_BLOCK never sets high_risk
        # itself — it must inherit the element's high_risk: true.
        self.assertTrue(idx.details["ADR-3001#E1.C1"].high_risk)

    def test_missing_adr_dir_yields_empty_index(self) -> None:
        idx = ticket_lint.load_adr_obligation_index(self.adr_dir / "does-not-exist")
        self.assertEqual(idx.details, {})
        self.assertEqual(idx.element_clauses, {})

    def test_clause_level_high_risk_false_overrides_element_true(self) -> None:
        """T-2845 / ORG-065 BT5 flip prerequisite (c): a clause explicitly
        marking high_risk: false must NOT inherit the element's high_risk:
        true — the clause-level marker overrides. A sibling clause with no
        override still inherits (test_high_risk_inherited_from_element_when_
        clause_unset covers that direction alone; this fixture puts both in
        play together so a future regression in either direction is caught)."""
        _write_adr(self.adr_dir / "ADR-3002-test.md", adr_id="ADR-3002",
                    elements_block=_MIXED_HIGH_RISK_ELEMENTS_BLOCK)
        idx = ticket_lint.load_adr_obligation_index(self.adr_dir)
        expectations = {
            "ADR-3002#E1": True,       # element itself
            "ADR-3002#E1.C1": False,   # explicit clause-level override
            "ADR-3002#E1.C2": True,    # no override -> inherits element
        }
        for oblig_id, expected in expectations.items():
            with self.subTest(oblig_id=oblig_id):
                self.assertEqual(idx.details[oblig_id].high_risk, expected)


# ---------------------------------------------------------------------------
# ticket_lint.py: evaluate_closure_fields — the core algorithm.
# ---------------------------------------------------------------------------


class TestEvaluateClosureFields(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.adr_dir = Path(self._tmp.name) / "architecture" / "adr"
        self.adr_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_t2163_adr(self) -> None:
        _write_adr(self.adr_dir / "ADR-2842-test.md", adr_id="ADR-2842",
                    elements_block=_T2163_ELEMENTS_BLOCK)

    def test_no_implements_no_findings(self) -> None:
        self.assertEqual(
            ticket_lint.evaluate_closure_fields({}, self.adr_dir), []
        )

    def test_ticket_only_implements_no_findings(self) -> None:
        # implements: [T-2840] — "implements parent work", not an ADR
        # obligation (per BT1's own field semantics). Not a clause-shaped
        # target, so it is skipped entirely.
        fields = {"implements": ["T-2840"]}
        self.assertEqual(
            ticket_lint.evaluate_closure_fields(fields, self.adr_dir), []
        )

    def test_element_level_implements_expands_to_all_clauses(self) -> None:
        self._write_t2163_adr()
        fields = {"implements": ["ADR-2842#E1"], "receipts": []}
        findings = ticket_lint.evaluate_closure_fields(fields, self.adr_dir)
        unsatisfied = {f.detail.split(" ")[0] for f in findings if f.kind == "unsatisfied_clause"}
        self.assertEqual(unsatisfied, {"ADR-2842#E1.C1", "ADR-2842#E1.C2"})

    def test_clause_level_implements_requires_only_that_clause(self) -> None:
        self._write_t2163_adr()
        fields = {"implements": ["ADR-2842#E1.C1"], "receipts": []}
        findings = ticket_lint.evaluate_closure_fields(fields, self.adr_dir)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "unsatisfied_clause")
        self.assertIn("ADR-2842#E1.C1", findings[0].detail)

    def test_t2163_two_clause_leak_one_satisfied_flags_the_other(self) -> None:
        """THE acceptance scenario: element E1 has two clauses; only C1 has
        a satisfying receipt. C2 must be named precisely, with its element
        text, so coverage is checkable against the obligation body."""
        self._write_t2163_adr()
        fields = {
            "implements": ["ADR-2842#E1"],
            "receipts": ["ADR-2842#E1.C1=ci-run:111=ci"],
        }
        findings = ticket_lint.evaluate_closure_fields(fields, self.adr_dir)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "unsatisfied_clause")
        self.assertIn("ADR-2842#E1.C2", findings[0].detail)
        self.assertIn("clause two", findings[0].detail)
        self.assertNotIn("ADR-2842#E1.C1", findings[0].detail)

    def test_t2163_two_clause_clean_fixture_both_satisfied_no_findings(self) -> None:
        self._write_t2163_adr()
        fields = {
            "implements": ["ADR-2842#E1"],
            "receipts": [
                "ADR-2842#E1.C1=ci-run:111=ci",
                "ADR-2842#E1.C2=test-result:222=reviewer",
            ],
        }
        self.assertEqual(
            ticket_lint.evaluate_closure_fields(fields, self.adr_dir), []
        )

    def test_verifies_edge_alone_without_receipt_is_not_satisfying(self) -> None:
        """A commit link is not a receipt (§3.5.2): a `verifies` entry
        naming the clause, with NO corresponding receipts entry, must still
        flag the clause as unsatisfied."""
        self._write_t2163_adr()
        fields = {
            "implements": ["ADR-2842#E1.C1"],
            "verifies": ["ADR-2842#E1.C1"],
            "receipts": [],
        }
        findings = ticket_lint.evaluate_closure_fields(fields, self.adr_dir)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "unsatisfied_clause")

    def test_high_risk_clause_self_asserted_denied(self) -> None:
        _write_adr(self.adr_dir / "ADR-3000-test.md", adr_id="ADR-3000",
                    elements_block=_HIGH_RISK_ELEMENTS_BLOCK)
        fields = {
            "implements": ["ADR-3000#E1.C1"],
            "receipts": ["ADR-3000#E1.C1=ci-run:1=self"],
        }
        findings = ticket_lint.evaluate_closure_fields(fields, self.adr_dir)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "evidence_authority_denied")
        self.assertIn("ADR-3000#E1.C1", findings[0].detail)

    def test_high_risk_clause_ci_provenance_satisfies(self) -> None:
        _write_adr(self.adr_dir / "ADR-3000-test.md", adr_id="ADR-3000",
                    elements_block=_HIGH_RISK_ELEMENTS_BLOCK)
        fields = {
            "implements": ["ADR-3000#E1.C1"],
            "receipts": ["ADR-3000#E1.C1=ci-run:1=ci"],
        }
        self.assertEqual(
            ticket_lint.evaluate_closure_fields(fields, self.adr_dir), []
        )

    def test_high_risk_clause_reviewer_provenance_satisfies(self) -> None:
        _write_adr(self.adr_dir / "ADR-3000-test.md", adr_id="ADR-3000",
                    elements_block=_HIGH_RISK_ELEMENTS_BLOCK)
        fields = {
            "implements": ["ADR-3000#E1.C1"],
            "receipts": ["ADR-3000#E1.C1=test-result:1=reviewer"],
        }
        self.assertEqual(
            ticket_lint.evaluate_closure_fields(fields, self.adr_dir), []
        )

    def test_mixed_self_and_ci_provenance_satisfies_high_risk_clause(self) -> None:
        """T-2845 / ORG-065 BT5 flip prerequisite (b): a high-risk clause
        carrying BOTH a self receipt and a ci receipt satisfies via the ci
        one — the self receipt neither satisfies on its own
        (test_high_risk_clause_self_asserted_denied already proves that) nor
        POISONS an otherwise-authorized receipt set. Checked in both list
        orders since the denial rule is `any(...)`, not order-sensitive — a
        future rewrite that silently narrows it to `all(...)` (or otherwise
        makes it order-dependent) must not slip through."""
        _write_adr(self.adr_dir / "ADR-3000-test.md", adr_id="ADR-3000",
                    elements_block=_HIGH_RISK_ELEMENTS_BLOCK)
        orders = {
            "self_then_ci": ["ADR-3000#E1.C1=ci-run:1=self", "ADR-3000#E1.C1=ci-run:2=ci"],
            "ci_then_self": ["ADR-3000#E1.C1=ci-run:2=ci", "ADR-3000#E1.C1=ci-run:1=self"],
        }
        for label, receipts in orders.items():
            with self.subTest(order=label):
                fields = {"implements": ["ADR-3000#E1.C1"], "receipts": receipts}
                self.assertEqual(
                    ticket_lint.evaluate_closure_fields(fields, self.adr_dir), []
                )

    def test_dangling_obligation_flagged(self) -> None:
        # No ADR files at all — ADR-9999#E1.C1 resolves to nothing.
        fields = {"implements": ["ADR-9999#E1.C1"], "receipts": []}
        findings = ticket_lint.evaluate_closure_fields(fields, self.adr_dir)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "dangling_obligation")
        self.assertIn("ADR-9999#E1.C1", findings[0].detail)

    def test_malformed_receipt_entry_flagged(self) -> None:
        self._write_t2163_adr()
        fields = {
            "implements": ["ADR-2842#E1.C1"],
            "receipts": ["garbage-not-a-receipt"],
        }
        findings = ticket_lint.evaluate_closure_fields(fields, self.adr_dir)
        kinds = {f.kind for f in findings}
        self.assertIn("malformed_receipt", kinds)
        # The clause itself is STILL unsatisfied — a malformed entry never
        # counts as evidence for anything.
        self.assertIn("unsatisfied_clause", kinds)


# ---------------------------------------------------------------------------
# ticket_lint.py: evaluate_closure_for_ticket — the single-ticket entry point.
# ---------------------------------------------------------------------------


class TestEvaluateClosureForTicket(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tickets = self.root / "tickets"
        self.adr_dir = self.root / "architecture" / "adr"
        for st in ("inbox", "active", "done"):
            (self.tickets / st).mkdir(parents=True)
        self.adr_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_invokes_against_named_ticket_in_any_state(self) -> None:
        _write_adr(self.adr_dir / "ADR-2842-test.md", adr_id="ADR-2842",
                    elements_block=_T2163_ELEMENTS_BLOCK)
        _write_ticket(
            self.tickets / "done" / "T-0001.md", tid="T-0001", state="done",
            implements=["ADR-2842#E1"],
            receipts=["ADR-2842#E1.C1=ci-run:1=ci"],
        )
        findings = ticket_lint.evaluate_closure_for_ticket(
            "T-0001", self.tickets, self.adr_dir
        )
        self.assertEqual(len(findings), 1)
        self.assertIn("ADR-2842#E1.C2", findings[0].detail)

    def test_missing_ticket_raises(self) -> None:
        with self.assertRaises(ticket.TicketError):
            ticket_lint.evaluate_closure_for_ticket(
                "T-9999", self.tickets, self.adr_dir
            )


# ---------------------------------------------------------------------------
# ticket_lint.py: detect_closure_gate_health — T-2845 / ORG-065 BT5 flip-
# prerequisite corpus-health aggregation (the ORG-068 "health-monitor" gap
# the T-2842 reviewer named: nothing observed gate-mode drift or finding-
# volume trend ahead of the warn->gate flip).
# ---------------------------------------------------------------------------


class TestDetectClosureGateHealth(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tickets = self.root / "tickets"
        self.adr_dir = self.root / "architecture" / "adr"
        for st in ("inbox", "active", "done"):
            (self.tickets / st).mkdir(parents=True)
        self.adr_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_t2163_adr(self) -> None:
        _write_adr(self.adr_dir / "ADR-2842-test.md", adr_id="ADR-2842",
                    elements_block=_T2163_ELEMENTS_BLOCK)

    def test_empty_corpus_all_zero(self) -> None:
        report = ticket_lint.detect_closure_gate_health(
            self.tickets, self.adr_dir, effective_mode="warn", intended_mode="warn"
        )
        self.assertEqual(report.corpus_tickets_scanned, 0)
        self.assertEqual(report.corpus_tickets_with_obligations, 0)
        self.assertEqual(report.corpus_tickets_with_findings, 0)
        self.assertEqual(report.corpus_total_findings, 0)
        self.assertEqual(report.open_tickets_scanned, 0)
        self.assertEqual(report.open_total_findings, 0)
        self.assertFalse(report.mode_drift)
        for st in ("inbox", "active", "done"):
            with self.subTest(state=st):
                self.assertEqual(report.by_state[st]["scanned"], 0)

    def test_mode_drift_flagged_when_effective_differs_from_intended(self) -> None:
        report = ticket_lint.detect_closure_gate_health(
            self.tickets, self.adr_dir, effective_mode="gate", intended_mode="warn"
        )
        self.assertTrue(report.mode_drift)
        self.assertEqual(report.effective_mode, "gate")
        self.assertEqual(report.intended_mode, "warn")

    def test_no_drift_when_modes_match(self) -> None:
        report = ticket_lint.detect_closure_gate_health(
            self.tickets, self.adr_dir, effective_mode="warn", intended_mode="warn"
        )
        self.assertFalse(report.mode_drift)

    def test_ticket_without_implements_not_counted_as_obligation(self) -> None:
        _write_ticket(self.tickets / "active" / "T-0001.md", tid="T-0001")
        report = ticket_lint.detect_closure_gate_health(
            self.tickets, self.adr_dir, effective_mode="warn", intended_mode="warn"
        )
        self.assertEqual(report.corpus_tickets_scanned, 1)
        self.assertEqual(report.corpus_tickets_with_obligations, 0)
        self.assertEqual(report.corpus_tickets_with_findings, 0)

    def test_clean_obligation_ticket_counted_but_no_findings(self) -> None:
        self._write_t2163_adr()
        _write_ticket(
            self.tickets / "active" / "T-0002.md", tid="T-0002",
            implements=["ADR-2842#E1"],
            receipts=["ADR-2842#E1.C1=ci-run:1=ci", "ADR-2842#E1.C2=test-result:2=reviewer"],
        )
        report = ticket_lint.detect_closure_gate_health(
            self.tickets, self.adr_dir, effective_mode="warn", intended_mode="warn"
        )
        self.assertEqual(report.corpus_tickets_with_obligations, 1)
        self.assertEqual(report.corpus_tickets_with_findings, 0)
        self.assertEqual(report.corpus_total_findings, 0)

    def test_unsatisfied_clause_counted_in_corpus_and_open(self) -> None:
        self._write_t2163_adr()
        _write_ticket(
            self.tickets / "active" / "T-0003.md", tid="T-0003",
            implements=["ADR-2842#E1"],
            receipts=["ADR-2842#E1.C1=ci-run:1=ci"],
        )
        report = ticket_lint.detect_closure_gate_health(
            self.tickets, self.adr_dir, effective_mode="warn", intended_mode="warn"
        )
        self.assertEqual(report.corpus_tickets_with_findings, 1)
        self.assertEqual(report.corpus_total_findings, 1)
        self.assertEqual(report.corpus_findings_by_kind, {"unsatisfied_clause": 1})
        # active is an open state -> shows up in open_* too.
        self.assertEqual(report.open_tickets_with_findings, 1)
        self.assertEqual(report.open_total_findings, 1)

    def test_done_state_findings_counted_in_corpus_but_not_open(self) -> None:
        """The open-only slice is what the flip leg reads for a go/no-go
        signal; a legacy finding on an already-closed ticket is corpus-wide
        migration-walk debt, not something the gate will ever re-fire on."""
        self._write_t2163_adr()
        _write_ticket(
            self.tickets / "done" / "T-0004.md", tid="T-0004", state="done",
            implements=["ADR-2842#E1"],
            receipts=["ADR-2842#E1.C1=ci-run:1=ci"],
        )
        report = ticket_lint.detect_closure_gate_health(
            self.tickets, self.adr_dir, effective_mode="warn", intended_mode="warn"
        )
        self.assertEqual(report.corpus_tickets_with_findings, 1)
        self.assertEqual(report.corpus_total_findings, 1)
        self.assertEqual(report.open_tickets_with_findings, 0)
        self.assertEqual(report.open_total_findings, 0)
        self.assertEqual(report.by_state["done"]["with_findings"], 1)
        self.assertEqual(report.by_state["active"]["with_findings"], 0)

    def test_findings_by_kind_aggregates_multiple_kinds(self) -> None:
        # One dangling obligation (no ADR at all) + one malformed receipt,
        # on separate tickets, to prove the kind-count dict aggregates
        # across tickets, not just within one.
        _write_ticket(
            self.tickets / "active" / "T-0005.md", tid="T-0005",
            implements=["ADR-9999#E1.C1"],
        )
        self._write_t2163_adr()
        _write_ticket(
            self.tickets / "active" / "T-0006.md", tid="T-0006",
            implements=["ADR-2842#E1.C1"],
            receipts=["garbage-not-a-receipt"],
        )
        report = ticket_lint.detect_closure_gate_health(
            self.tickets, self.adr_dir, effective_mode="warn", intended_mode="warn"
        )
        self.assertEqual(report.corpus_findings_by_kind.get("dangling_obligation"), 1)
        self.assertEqual(report.corpus_findings_by_kind.get("malformed_receipt"), 1)
        self.assertEqual(report.corpus_findings_by_kind.get("unsatisfied_clause"), 1)


# ---------------------------------------------------------------------------
# ticket_lint.py: detect_duplicate_receipt_ids (T-2926 / ORG-065 §3.5.3 —
# corpus-wide receipt-id uniqueness, the follow-up T-2842 disposition 1
# explicitly deferred). Analogous to scan_adr_elements's duplicate_id check,
# but cross-file/cross-ticket rather than within-one-ADR — a receipt id
# names one artifact, and no two tickets may claim the same one.
# ---------------------------------------------------------------------------


class TestDetectDuplicateReceiptIds(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.tickets = self.root / "tickets"
        for st in ("inbox", "active", "done"):
            (self.tickets / st).mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _scan(self):
        return ticket_lint.detect_duplicate_receipt_ids(self.tickets)

    def test_empty_corpus_no_findings(self) -> None:
        self.assertEqual(self._scan(), [])

    def test_same_receipt_id_two_tickets_flagged(self) -> None:
        _write_ticket(
            self.tickets / "active" / "T-0001.md", tid="T-0001",
            receipts=["ADR-2842#E1.C1=ci-run:1=ci"],
        )
        _write_ticket(
            self.tickets / "active" / "T-0002.md", tid="T-0002",
            receipts=["ADR-2842#E1.C2=ci-run:1=ci"],
        )
        findings = self._scan()
        dupes = [f for f in findings if f.kind == "duplicate_receipt_id"]
        self.assertEqual(len(dupes), 1)
        self.assertIn("ci-run:1", dupes[0].detail)
        self.assertIn("T-0001", dupes[0].detail)
        self.assertIn("T-0002", dupes[0].detail)

    def test_unique_receipt_ids_not_flagged(self) -> None:
        _write_ticket(
            self.tickets / "active" / "T-0001.md", tid="T-0001",
            receipts=["ADR-2842#E1.C1=ci-run:1=ci"],
        )
        _write_ticket(
            self.tickets / "active" / "T-0002.md", tid="T-0002",
            receipts=["ADR-2842#E1.C1=ci-run:2=ci"],
        )
        self.assertEqual(self._scan(), [])

    def test_same_ticket_reusing_own_receipt_across_clauses_not_flagged(self) -> None:
        """One CI run legitimately backing two obligations of the SAME
        ticket is not a cross-ticket duplicate claim."""
        _write_ticket(
            self.tickets / "active" / "T-0001.md", tid="T-0001",
            receipts=[
                "ADR-2842#E1.C1=ci-run:1=ci",
                "ADR-2842#E1.C2=ci-run:1=ci",
            ],
        )
        self.assertEqual(self._scan(), [])

    def test_malformed_receipt_entry_skipped_not_flagged(self) -> None:
        _write_ticket(
            self.tickets / "active" / "T-0001.md", tid="T-0001",
            receipts=["garbage-not-a-receipt"],
        )
        _write_ticket(
            self.tickets / "active" / "T-0002.md", tid="T-0002",
            receipts=["garbage-not-a-receipt"],
        )
        self.assertEqual(self._scan(), [])

    def test_duplicate_across_three_tickets_names_all_three(self) -> None:
        for tid in ("T-0001", "T-0002", "T-0003"):
            _write_ticket(
                self.tickets / "active" / f"{tid}.md", tid=tid,
                receipts=["ADR-2842#E1.C1=ci-run:1=ci"],
            )
        findings = self._scan()
        dupes = [f for f in findings if f.kind == "duplicate_receipt_id"]
        self.assertEqual(len(dupes), 1)
        for tid in ("T-0001", "T-0002", "T-0003"):
            self.assertIn(tid, dupes[0].detail)

    def test_done_state_tickets_included_in_scan(self) -> None:
        """A receipt id is a corpus-wide identity regardless of ticket
        state — mirrors detect_closure_gate_health's all-states scope."""
        _write_ticket(
            self.tickets / "active" / "T-0001.md", tid="T-0001",
            receipts=["ADR-2842#E1.C1=ci-run:1=ci"],
        )
        _write_ticket(
            self.tickets / "done" / "T-0002.md", tid="T-0002", state="done",
            receipts=["ADR-2842#E1.C1=ci-run:1=ci"],
        )
        findings = self._scan()
        dupes = [f for f in findings if f.kind == "duplicate_receipt_id"]
        self.assertEqual(len(dupes), 1)
        self.assertIn("T-0001", dupes[0].detail)
        self.assertIn("T-0002", dupes[0].detail)

    def test_build_lint_digest_renders_duplicate_receipt_section(self) -> None:
        finding = ticket_lint.TypedGraphFinding(
            kind="duplicate_receipt_id",
            detail="receipt id 'ci-run:1' is claimed by multiple tickets: T-0001, T-0002",
        )
        msg = ticket_lint.build_lint_digest(
            "2026-08-24T00:00:00Z", [], [], 2,
            typed_graph_findings=[finding],
        )
        self.assertIn("Duplicate receipt ids", msg)
        self.assertIn("ci-run:1", msg)
        self.assertIn("T-0001, T-0002", msg)


# ---------------------------------------------------------------------------
# ticket.py: integration — --receipt CLI wiring, mode-flag enforcement, via
# cmd_resolve / _do_transition.
# ---------------------------------------------------------------------------


def _resolve_args(tid: str, receipt: list[str] | None = None, with_summary: str = "ok") -> MagicMock:
    args = MagicMock()
    args.ticket = tid
    args.with_summary = with_summary
    args.no_push = True
    args.receipt = receipt
    return args


class TestClosureGateModeResolve(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "tickets" / "active").mkdir(parents=True)
        (self.root / "tickets" / "done").mkdir(parents=True)
        (self.root / "architecture" / "adr").mkdir(parents=True)

        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root / "tickets")
        self._tickets_patch.start()
        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()

        # git mv / pull / commit are mocked — this suite exercises the
        # frontmatter-mutation + gate logic, not real git plumbing (mirrors
        # TestCascadeUnblockOnResolve's shape in test_ticket.py, minus the
        # real-subprocess fake_run since we don't need cascade side effects).
        self._git_patch = patch.object(ticket, "_git")

        def fake_git(*argv, **kwargs):
            import shutil
            from unittest.mock import MagicMock as _MM
            if argv[:1] == ("mv",):
                src = contained_path(self.root, argv[1])
                dst = contained_path(self.root, argv[2])
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
            return _MM(returncode=0, stdout="", stderr="")

        self.mock_git = self._git_patch.start()
        self.mock_git.side_effect = fake_git
        self._pull_patch = patch.object(ticket, "_pull_if_needed")
        self._pull_patch.start()
        self._commit_patch = patch.object(ticket, "_commit_and_push")
        self._commit_patch.start()
        self._lock_patch = patch.object(ticket, "git_mutation_lock", lambda **kw: _NullCtx())
        self._lock_patch.start()

        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop(ticket._CLOSURE_GATE_MODE_ENV, None)

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._lock_patch.stop()
        self._commit_patch.stop()
        self._pull_patch.stop()
        self._git_patch.stop()
        self._repo_patch.stop()
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def _write_t2163_adr(self) -> None:
        _write_adr(self.root / "architecture" / "adr" / "ADR-2842-test.md", adr_id="ADR-2842",
                    elements_block=_T2163_ELEMENTS_BLOCK)

    def _seed(self, tid: str, **kwargs) -> Path:
        path = self.root / "tickets" / "active" / f"{tid}.md"
        _write_ticket(path, tid=tid, state="active", **kwargs)
        return path

    def test_warn_mode_allows_resolve_with_findings(self) -> None:
        # Post-flip (T-2934): warn is no longer the default — pin it explicitly.
        os.environ[ticket._CLOSURE_GATE_MODE_ENV] = "warn"
        self._write_t2163_adr()
        self._seed("T-0001", implements=["ADR-2842#E1"],
                    receipts=["ADR-2842#E1.C1=ci-run:1=ci"])
        with patch("sys.stderr") as mock_stderr:
            rc = ticket.cmd_resolve(_resolve_args("T-0001"))
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "tickets" / "done" / "T-0001.md").exists())
        printed = "".join(c.args[0] for c in mock_stderr.write.call_args_list if c.args)
        self.assertIn("[closure-gate]", printed)
        self.assertIn("T-0001", printed)
        self.assertIn("unsatisfied_clause", printed)
        self.assertIn("ADR-2842#E1.C2", printed)

    def test_warn_output_names_clause_and_element_text(self) -> None:
        """T-2842 plan addition B: the warn line must be greppable and carry
        ticket id + clause id + finding kind + element text."""
        os.environ[ticket._CLOSURE_GATE_MODE_ENV] = "warn"
        self._write_t2163_adr()
        self._seed("T-0002", implements=["ADR-2842#E1"],
                    receipts=["ADR-2842#E1.C1=ci-run:1=ci"])
        with patch("sys.stderr") as mock_stderr:
            ticket.cmd_resolve(_resolve_args("T-0002"))
        printed = "".join(c.args[0] for c in mock_stderr.write.call_args_list if c.args)
        self.assertIn("[closure-gate] T-0002 unsatisfied_clause:", printed)
        self.assertIn("ADR-2842#E1.C2", printed)
        self.assertIn("clause two", printed)

    def test_garbage_gate_mode_value_falls_back_to_warn(self) -> None:
        """T-2845 / ORG-065 BT5 flip prerequisite (a): an unrecognized
        PANTHEON_CLOSURE_GATE_MODE value must never crash — it fails SAFE
        to warn (fail-open), with the health monitor's drift line as the
        catch for a silent demotion (post-flip framing, T-2934: gate is the
        default; a typo'd value demotes to warn and alarms as drift rather
        than half-gating unpredictably). Empty string is NOT garbage — it is
        unset-equivalent (falsy through the `or` default) and takes the gate
        default, covered by the gate-mode tests. Several nonsense values,
        not just one; proven end-to-end through cmd_resolve, not just the
        mode-string return."""
        self._write_t2163_adr()
        garbage_values = ("banana", "enforce", "GATE!", "   ", "1")
        for i, garbage in enumerate(garbage_values):
            with self.subTest(garbage=repr(garbage)):
                os.environ[ticket._CLOSURE_GATE_MODE_ENV] = garbage
                tid = f"T-01{i:02d}"
                self._seed(tid, implements=["ADR-2842#E1"],
                           receipts=["ADR-2842#E1.C1=ci-run:1=ci"])
                with patch("sys.stderr") as mock_stderr:
                    rc = ticket.cmd_resolve(_resolve_args(tid))
                self.assertEqual(rc, 0)
                self.assertTrue((self.root / "tickets" / "done" / f"{tid}.md").exists())
                printed = "".join(c.args[0] for c in mock_stderr.write.call_args_list if c.args)
                self.assertIn("[closure-gate]", printed)
                self.assertIn("unsatisfied_clause", printed)

    def test_gate_mode_env_refuses_resolve_with_findings_no_mutation(self) -> None:
        os.environ[ticket._CLOSURE_GATE_MODE_ENV] = "gate"
        self._write_t2163_adr()
        path = self._seed("T-0003", implements=["ADR-2842#E1"],
                           receipts=["ADR-2842#E1.C1=ci-run:1=ci"])
        original = path.read_text(encoding="utf-8")
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket.cmd_resolve(_resolve_args("T-0003"))
        self.assertIn("closure-gate", str(ctx.exception))
        self.assertIn("ADR-2842#E1.C2", str(ctx.exception))
        # No mutation: file untouched, still in active/, nothing in done/.
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertFalse((self.root / "tickets" / "done" / "T-0003.md").exists())

    def test_gate_mode_clean_fixture_resolves(self) -> None:
        os.environ[ticket._CLOSURE_GATE_MODE_ENV] = "gate"
        self._write_t2163_adr()
        self._seed("T-0004", implements=["ADR-2842#E1"],
                    receipts=["ADR-2842#E1.C1=ci-run:1=ci",
                              "ADR-2842#E1.C2=test-result:2=reviewer"])
        rc = ticket.cmd_resolve(_resolve_args("T-0004"))
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "tickets" / "done" / "T-0004.md").exists())

    def test_receipt_cli_flag_attaches_and_satisfies_clause(self) -> None:
        os.environ[ticket._CLOSURE_GATE_MODE_ENV] = "gate"
        self._write_t2163_adr()
        self._seed("T-0005", implements=["ADR-2842#E1"],
                    receipts=["ADR-2842#E1.C1=ci-run:1=ci"])
        rc = ticket.cmd_resolve(_resolve_args(
            "T-0005", receipt=["ADR-2842#E1.C2=test-result:2=reviewer"],
        ))
        self.assertEqual(rc, 0)
        done_text = (self.root / "tickets" / "done" / "T-0005.md").read_text(encoding="utf-8")
        self.assertIn("ADR-2842#E1.C2=test-result:2=reviewer", done_text)
        self.assertIn("ADR-2842#E1.C1=ci-run:1=ci", done_text)

    def test_malformed_receipt_cli_flag_refuses_before_mutation(self) -> None:
        self._write_t2163_adr()
        path = self._seed("T-0006", implements=["ADR-2842#E1.C1"])
        original = path.read_text(encoding="utf-8")
        with self.assertRaises(ticket.TicketError):
            ticket.cmd_resolve(_resolve_args("T-0006", receipt=["not-a-valid-receipt"]))
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertFalse((self.root / "tickets" / "done" / "T-0006.md").exists())

    def test_no_implements_ticket_resolves_unaffected_by_gate(self) -> None:
        """Regression safety net: a ticket that never declared an ADR
        obligation must resolve exactly as before, gate mode or not."""
        os.environ[ticket._CLOSURE_GATE_MODE_ENV] = "gate"
        self._seed("T-0007")
        rc = ticket.cmd_resolve(_resolve_args("T-0007"))
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "tickets" / "done" / "T-0007.md").exists())

    def test_close_action_gate_also_applies(self) -> None:
        """Falsifies a resolve-only reading: the gate hook lives in
        _do_transition, so close-action (target=operator, intent=action)
        is gated identically, even with no --receipt CLI path there."""
        os.environ[ticket._CLOSURE_GATE_MODE_ENV] = "gate"
        self._write_t2163_adr()
        path = self.root / "tickets" / "active" / "T-0008.md"
        _write_ticket(path, tid="T-0008", state="active", target="operator",
                       requester="recovery", implements=["ADR-2842#E1"],
                       receipts=["ADR-2842#E1.C1=ci-run:1=ci"])
        # target=operator + intent=action fields aren't in _write_ticket's
        # shape; hand-append them (mirrors other local-fixture patterns that
        # write straight to disk for fields the helper doesn't model).
        text = path.read_text(encoding="utf-8")
        text = text.replace("state: active\n", "state: active\nintent: action\n")
        path.write_text(text, encoding="utf-8")

        args = MagicMock()
        args.ticket = "T-0008"
        args.with_summary = "did it"
        args.no_push = True
        from tools.test_ticket import with_ruling  # T-5015 register flags
        with_ruling(args, self.root)
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket.cmd_close_action(args)
        self.assertIn("closure-gate", str(ctx.exception))
        self.assertFalse((self.root / "tickets" / "done" / "T-0008.md").exists())


class _NullCtx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# ticket.py: _parse_receipt_entries shape validation (unit-level, no I/O).
# ---------------------------------------------------------------------------


class TestParseReceiptEntries(unittest.TestCase):
    def test_well_formed_entries_pass_through(self) -> None:
        result = ticket._parse_receipt_entries(
            "receipts", ["ADR-2842#E1.C1=ci-run:1=ci"]
        )
        self.assertEqual(result, ["ADR-2842#E1.C1=ci-run:1=ci"])

    def test_malformed_entry_raises(self) -> None:
        with self.assertRaises(ticket.TicketError):
            ticket._parse_receipt_entries("receipts", ["garbage"])

    def test_empty_entry_raises(self) -> None:
        with self.assertRaises(ticket.TicketError):
            ticket._parse_receipt_entries("receipts", ["   "])


# ---------------------------------------------------------------------------
# T-3578 / ORG-065: cross-repo echo ADR obligation resolution (union of the
# pantheon architecture/adr/ corpus with echo-prismatic's adr/), single-Path
# back-compat, and the actor-relative warn-fallback for a clone-less actor.
# ---------------------------------------------------------------------------


class TestCrossRepoObligationUnion(unittest.TestCase):
    """load_adr_obligation_index / evaluate_closure_fields must accept an
    ITERABLE of ADR dirs and UNION them, so a cross-repo echo obligation
    (ADR-216#E4, T-1213-class) resolves against the sibling repo's corpus while
    a pantheon ORG-* obligation resolves against this repo's — AND the pre-T-3578
    bare-Path signature keeps working (back-compat)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.pantheon_dir = base / "architecture" / "adr"
        self.echo_dir = base / "echo-prismatic" / "adr"
        self.pantheon_dir.mkdir(parents=True)
        self.echo_dir.mkdir(parents=True)
        _write_adr(self.pantheon_dir / "ORG-999-test.md", adr_id="ORG-999",
                    elements_block=_ORG_E1_ELEMENTS_BLOCK)
        _write_adr(self.echo_dir / "ADR-216 — echo.md", adr_id="ADR-216",
                    elements_block=_ECHO_E4_ELEMENTS_BLOCK)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # (a) UNION resolves a cross-repo obligation.
    def test_index_unions_both_dirs(self) -> None:
        idx = ticket_lint.load_adr_obligation_index([self.pantheon_dir, self.echo_dir])
        self.assertIn("ADR-216#E4", idx.details)
        self.assertIn("ORG-999#E1", idx.details)

    def test_union_resolves_cross_repo_echo_obligation_no_dangling(self) -> None:
        fields = {
            "implements": ["ADR-216#E4"],
            "receipts": ["ADR-216#E4=ci-run:1=ci"],
        }
        findings = ticket_lint.evaluate_closure_fields(
            fields, [self.pantheon_dir, self.echo_dir]
        )
        self.assertEqual(findings, [])

    def test_union_echo_obligation_without_receipt_is_unsatisfied_not_dangling(self) -> None:
        # The obligation RESOLVES against the echo corpus (no dangling), so the
        # gap surfaces as a normal unsatisfied_clause — proving the union worked.
        fields = {"implements": ["ADR-216#E4"], "receipts": []}
        findings = ticket_lint.evaluate_closure_fields(
            fields, [self.pantheon_dir, self.echo_dir]
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "unsatisfied_clause")
        self.assertIn("ADR-216#E4", findings[0].detail)

    def test_union_pantheon_obligation_still_resolves(self) -> None:
        fields = {
            "implements": ["ORG-999#E1"],
            "receipts": ["ORG-999#E1=ci-run:2=ci"],
        }
        findings = ticket_lint.evaluate_closure_fields(
            fields, [self.pantheon_dir, self.echo_dir]
        )
        self.assertEqual(findings, [])

    def test_echo_obligation_dangling_when_echo_dir_absent_from_list(self) -> None:
        # Only the pantheon dir → the echo id has nowhere to resolve → dangling.
        fields = {"implements": ["ADR-216#E4"], "receipts": ["ADR-216#E4=ci-run:1=ci"]}
        findings = ticket_lint.evaluate_closure_fields(fields, [self.pantheon_dir])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].kind, "dangling_obligation")

    # (b) SINGLE-PATH BACK-COMPAT: a bare Path must still work for both APIs.
    def test_index_bare_path_backcompat(self) -> None:
        idx = ticket_lint.load_adr_obligation_index(self.echo_dir)  # bare Path
        self.assertIn("ADR-216#E4", idx.details)

    def test_evaluate_bare_path_backcompat(self) -> None:
        fields = {"implements": ["ADR-216#E4"], "receipts": ["ADR-216#E4=ci-run:1=ci"]}
        # bare Path arg (pre-T-3578 signature) — no dangling.
        findings = ticket_lint.evaluate_closure_fields(fields, self.echo_dir)
        self.assertEqual(findings, [])


class TestGateEchoAdrDir(unittest.TestCase):
    """_gate_echo_adr_dir is ACTOR-RELATIVE and env-overridable (T-3578)."""

    def setUp(self) -> None:
        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop("ECHO_PRISMATIC_ADR_DIR", None)

    def tearDown(self) -> None:
        self._env_patch.stop()

    def test_env_override_honored(self) -> None:
        os.environ["ECHO_PRISMATIC_ADR_DIR"] = "/fixture/echo/adr"
        self.assertEqual(ticket._gate_echo_adr_dir(), Path("/fixture/echo/adr"))

    def test_defaults_to_home_echo_prismatic_adr(self) -> None:
        self.assertEqual(
            ticket._gate_echo_adr_dir(), Path.home() / "echo-prismatic" / "adr"
        )


class TestClosureGateCrossRepoEcho(unittest.TestCase):
    """End-to-end at the closure-gate (ticket._check_closure_gate via
    cmd_resolve): the echo dir is pointed at a fixture via the
    ECHO_PRISMATIC_ADR_DIR override. Proves (c) graceful warn-fallback when the
    echo clone is unscannable and (d) that pantheon integrity is preserved."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "tickets" / "active").mkdir(parents=True)
        (self.root / "tickets" / "done").mkdir(parents=True)
        (self.root / "architecture" / "adr").mkdir(parents=True)
        # A separate, real echo corpus fixture (used by the union path).
        self.echo_dir = self.root / "echo-prismatic" / "adr"
        self.echo_dir.mkdir(parents=True)
        _write_adr(self.echo_dir / "ADR-216 — echo.md", adr_id="ADR-216",
                    elements_block=_ECHO_E4_ELEMENTS_BLOCK)

        self._tickets_patch = patch.object(ticket, "TICKETS_ROOT", self.root / "tickets")
        self._tickets_patch.start()
        self._repo_patch = patch.object(ticket, "REPO_ROOT", self.root)
        self._repo_patch.start()

        self._git_patch = patch.object(ticket, "_git")

        def fake_git(*argv, **kwargs):
            import shutil
            from unittest.mock import MagicMock as _MM
            if argv[:1] == ("mv",):
                src = contained_path(self.root, argv[1])
                dst = contained_path(self.root, argv[2])
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
            return _MM(returncode=0, stdout="", stderr="")

        self.mock_git = self._git_patch.start()
        self.mock_git.side_effect = fake_git
        self._pull_patch = patch.object(ticket, "_pull_if_needed")
        self._pull_patch.start()
        self._commit_patch = patch.object(ticket, "_commit_and_push")
        self._commit_patch.start()
        self._lock_patch = patch.object(ticket, "git_mutation_lock", lambda **kw: _NullCtx())
        self._lock_patch.start()

        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop(ticket._CLOSURE_GATE_MODE_ENV, None)
        os.environ.pop("ECHO_PRISMATIC_ADR_DIR", None)
        # Default: gate mode ON for this whole class (the demanding case).
        os.environ[ticket._CLOSURE_GATE_MODE_ENV] = "gate"

    def tearDown(self) -> None:
        self._env_patch.stop()
        self._lock_patch.stop()
        self._commit_patch.stop()
        self._pull_patch.stop()
        self._git_patch.stop()
        self._repo_patch.stop()
        self._tickets_patch.stop()
        self._tmp.cleanup()

    def _seed(self, tid: str, **kwargs) -> Path:
        path = self.root / "tickets" / "active" / f"{tid}.md"
        _write_ticket(path, tid=tid, state="active", **kwargs)
        return path

    def _write_org_adr(self) -> None:
        _write_adr(self.root / "architecture" / "adr" / "ORG-999-test.md",
                    adr_id="ORG-999", elements_block=_ORG_E1_ELEMENTS_BLOCK)

    def _write_pantheon_adr_nnn(self) -> None:
        # pantheon-config's OWN corpus uses ADR-NNN ids too (e.g. ADR-2842) —
        # NOT only ORG-NNN. This fixture proves such an id resolves in
        # architecture/adr and must NOT be mistaken for an (unreadable) echo
        # obligation just because it is ADR-prefixed. T-3578 rebound regression.
        _write_adr(self.root / "architecture" / "adr" / "ADR-7000-test.md",
                    adr_id="ADR-7000", elements_block=_ORG_E1_ELEMENTS_BLOCK)

    # (a-e2e) With a readable echo clone (env override → fixture), a cross-repo
    # echo obligation validates and resolves cleanly in GATE mode (T-1213-class).
    def test_echo_obligation_resolves_when_clone_readable(self) -> None:
        os.environ["ECHO_PRISMATIC_ADR_DIR"] = str(self.echo_dir)
        self._seed("T-1213", implements=["ADR-216#E4"],
                    receipts=["ADR-216#E4=ci-run:1=ci"])
        rc = ticket.cmd_resolve(_resolve_args("T-1213"))
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "tickets" / "done" / "T-1213.md").exists())

    # (c) WARN-FALLBACK — THE KEY TEST. Echo dir unscannable (nonexistent path)
    # + an echo-namespace obligation → the gate must NOT hard-raise; it degrades
    # to a loud stderr warning and resolves.
    def test_warn_fallback_when_echo_unreadable_does_not_raise(self) -> None:
        os.environ["ECHO_PRISMATIC_ADR_DIR"] = str(self.root / "no-such-echo" / "adr")
        self._seed("T-2001", implements=["ADR-216#E4"],
                    receipts=["ADR-216#E4=ci-run:1=ci"])
        with patch("sys.stderr") as mock_stderr:
            rc = ticket.cmd_resolve(_resolve_args("T-2001"))  # must NOT raise
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "tickets" / "done" / "T-2001.md").exists())
        printed = "".join(c.args[0] for c in mock_stderr.write.call_args_list if c.args)
        self.assertIn("[closure-gate] T-2001", printed)
        self.assertIn("echo_adr_unreadable", printed)
        self.assertIn("ADR-216#E4", printed)
        self.assertIn("UNVALIDATED", printed)
        self.assertIn("T-3578", printed)

    # (d) INTEGRITY PRESERVED — a genuinely-unsatisfied PANTHEON (ORG-*)
    # obligation STILL blocks in gate mode even when the echo dir is unscannable.
    def test_pantheon_obligation_still_gates_when_echo_unreadable(self) -> None:
        os.environ["ECHO_PRISMATIC_ADR_DIR"] = str(self.root / "no-such-echo" / "adr")
        self._write_org_adr()
        path = self._seed("T-2002", implements=["ORG-999#E1"], receipts=[])
        original = path.read_text(encoding="utf-8")
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket.cmd_resolve(_resolve_args("T-2002"))
        self.assertIn("ORG-999#E1", str(ctx.exception))
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertFalse((self.root / "tickets" / "done" / "T-2002.md").exists())

    # (d-mixed) A ticket mixing an unsatisfied ORG-* obligation with an echo
    # obligation, echo dir unscannable: still block on the ORG finding (the
    # per-namespace fallback demotes ONLY the echo obligation, not the pantheon).
    def test_mixed_org_and_echo_still_gates_on_org_when_echo_unreadable(self) -> None:
        os.environ["ECHO_PRISMATIC_ADR_DIR"] = str(self.root / "no-such-echo" / "adr")
        self._write_org_adr()
        path = self._seed("T-2003",
                          implements=["ORG-999#E1", "ADR-216#E4"], receipts=[])
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket.cmd_resolve(_resolve_args("T-2003"))
        # Blocks on the pantheon obligation...
        self.assertIn("ORG-999#E1", str(ctx.exception))
        # ...but the unvalidatable echo obligation is NOT in the raise (demoted).
        self.assertNotIn("ADR-216#E4", str(ctx.exception))
        self.assertFalse((self.root / "tickets" / "done" / "T-2003.md").exists())

    # A no-echo-obligation ticket with a satisfied pantheon obligation resolves
    # normally even when the echo dir is unscannable (fallback never engages).
    def test_clean_pantheon_ticket_resolves_when_echo_unreadable(self) -> None:
        os.environ["ECHO_PRISMATIC_ADR_DIR"] = str(self.root / "no-such-echo" / "adr")
        self._write_org_adr()
        self._seed("T-2004", implements=["ORG-999#E1"],
                    receipts=["ORG-999#E1=ci-run:9=ci"])
        rc = ticket.cmd_resolve(_resolve_args("T-2004"))
        self.assertEqual(rc, 0)
        self.assertTrue((self.root / "tickets" / "done" / "T-2004.md").exists())

    # (d-regression, T-3578 REBOUND) A RESOLVABLE pantheon obligation carrying an
    # ADR-NNN id must STILL gate when the echo dir is unscannable. It resolved in
    # architecture/adr, so its unsatisfied_clause is a real pantheon defect and
    # must NOT be demoted by the echo warn-fallback. The pre-fix code keyed the
    # fallback on the id prefix ALONE (`detail.startswith("ADR-")`), which wrongly
    # demoted this to a warn on a clone-less actor (uid=runner, no echo clone) —
    # the CI-runner rebound. The fix keys on `kind == "dangling_obligation"`
    # (only a dangle can be spuriously caused by an unreadable clone). This test
    # FAILS without the fix regardless of whether the runner has an echo clone,
    # because it forces the unscannable-echo path via the env override.
    def test_resolvable_pantheon_adr_nnn_obligation_still_gates_when_echo_unreadable(self) -> None:
        os.environ["ECHO_PRISMATIC_ADR_DIR"] = str(self.root / "no-such-echo" / "adr")
        self._write_pantheon_adr_nnn()
        path = self._seed("T-2005", implements=["ADR-7000#E1"], receipts=[])
        original = path.read_text(encoding="utf-8")
        with self.assertRaises(ticket.TicketError) as ctx:
            ticket.cmd_resolve(_resolve_args("T-2005"))
        self.assertIn("ADR-7000#E1", str(ctx.exception))
        # It gates as a real finding, NOT the echo warn-fallback.
        self.assertNotIn("echo_adr_unreadable", str(ctx.exception))
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertFalse((self.root / "tickets" / "done" / "T-2005.md").exists())


if __name__ == "__main__":
    unittest.main()
