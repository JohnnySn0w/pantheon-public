"""Falsifiers for ORG-056 Unit F topology upkeep audit (T-3190)."""
from __future__ import annotations

import datetime as dt
import unittest

from tools import organism_topology as ot
from tools import organism_topology_audit as au
from tools.test_organism_topology import _binding, _contract, _dependency

NOW = dt.datetime(2026, 8, 26, 12, 0, 0, tzinfo=dt.timezone.utc)


def _topology(*, bindings=(), dependencies=(), unowned=False):
    raw = _contract("organ:solo")
    if unowned:
        raw["owners"] = []
    contract = ot.parse_organ_contract(raw, source="organ:solo")
    return ot.OrganismTopology(
        ot.OrganismRecord(1, "organism:test", ("organ:solo",), "x"),
        {"organ:solo": contract},
        tuple(bindings),
        tuple(dependencies),
        (),
    )


def _report(topology, *, issues=(), obligations=()):
    return au.audit_topology(topology, structural_issues=issues, obligations=obligations, now=NOW)


class AuditFindingTests(unittest.TestCase):
    def test_unowned_contract_is_flagged(self) -> None:
        report = _report(_topology(unowned=True))
        self.assertIn(au.FindingKind.UNOWNED, {f.kind for f in report.findings})

    def test_owned_contract_is_not_unowned(self) -> None:
        report = _report(_topology())
        self.assertNotIn(au.FindingKind.UNOWNED, {f.kind for f in report.findings})

    def test_binding_without_evidence_is_unsupported(self) -> None:
        binding = ot.parse_organ_binding(
            _binding("binding:x", "organ:solo", "service:i", lifecycle="partial", evidence_refs=[]),
            source="binding:x",
        )
        report = _report(_topology(bindings=(binding,)))
        unsupported = [f for f in report.findings if f.kind is au.FindingKind.UNSUPPORTED]
        self.assertEqual([f.subject_id for f in unsupported], ["binding:x"])

    def test_structural_issue_becomes_drifted(self) -> None:
        issue = ot.TopologyIssue("index-stale", "*", "organism/index.json")
        report = _report(_topology(), issues=(issue,))
        self.assertIn(au.FindingKind.DRIFTED, {f.kind for f in report.findings})
        self.assertTrue(report.cadence.startswith("on-change"))

    def test_armed_never_evaluated_obligation_is_stale(self) -> None:
        obl = au.AuditObligation("ORG-056:R1", "armed", None, None)
        report = _report(_topology(), obligations=(obl,))
        stale = [f for f in report.findings if f.kind is au.FindingKind.STALE]
        self.assertEqual([f.subject_id for f in stale], ["ORG-056:R1"])

    def test_evaluated_obligation_is_not_stale(self) -> None:
        obl = au.AuditObligation("ORG-056:R1", "armed", "2026-08-01T00:00:00Z", "2026-12-01T00:00:00Z")
        report = _report(_topology(), obligations=(obl,))
        self.assertNotIn(au.FindingKind.STALE, {f.kind for f in report.findings})

    def test_overdue_next_check_is_stale(self) -> None:
        obl = au.AuditObligation("ORG-056:R2", "armed", "2026-01-01T00:00:00Z", "2026-06-01T00:00:00Z")
        report = _report(_topology(), obligations=(obl,))
        self.assertIn(au.FindingKind.STALE, {f.kind for f in report.findings})


class CostAndCadenceTests(unittest.TestCase):
    def test_cost_counts_the_maintenance_surface(self) -> None:
        report = _report(_topology())
        self.assertEqual(report.cost.contracts, 1)
        self.assertEqual(report.cost.dependencies, 0)
        self.assertGreater(report.cost.schemas, 0)
        self.assertEqual(report.cost.maintenance_surface, report.cost.contracts + report.cost.schemas)

    def test_no_edges_recommends_event_driven_not_a_timer(self) -> None:
        report = _report(_topology())
        self.assertIn("event-driven", report.cadence)
        self.assertNotIn("timer", report.cadence.split("standing timer is not")[0] if "standing timer" in report.cadence else report.cadence)

    def test_zero_supported_bindings_is_uneconomic(self) -> None:
        binding = ot.parse_organ_binding(
            _binding("binding:x", "organ:solo", "service:i", lifecycle="partial", evidence_refs=[]),
            source="binding:x",
        )
        report = _report(_topology(bindings=(binding,)))
        self.assertIn(au.FindingKind.UNECONOMIC, {f.kind for f in report.findings})


if __name__ == "__main__":
    unittest.main()
