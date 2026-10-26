"""Falsifiers for ORG-056 Unit E capability-path health (T-3189).

Synthetic topologies drive the typed-edge propagation and mutation-posture rules
from tickets/plans/T-2416-implementation-plan.md (§Capability-path evaluation and
the "Health and dependency propagation" acceptance matrix). Red-before-green:
each case asserts an EXACT typed outcome, not merely "not healthy".
"""
from __future__ import annotations

import datetime as dt
import unittest

from tools import organism_topology as ot
from tools import organism_capability_health as ch
from tools.test_organism_topology import _binding, _contract, _dependency, _evidence

NOW = dt.datetime(2026, 8, 26, 12, 0, 0, tzinfo=dt.timezone.utc)
ALL_FACETS = (
    "reachability",
    "liveness",
    "readiness",
    "correctness",
    "freshness",
    "authority",
    "continuity",
)


def _organ_bundle(organ_id: str, binding_id: str, facet_results: dict[str, str]):
    """Build (contract, binding, {eid: evidence}, digests) so the organ collapses
    to a chosen state. ``facet_results`` maps a facet to pass/fail/stale; any
    facet omitted defaults to pass; "absent" means no evidence for that facet
    (yielding an unknown facet). Evidence subject is the binding.
    """
    contract = ot.parse_organ_contract(_contract(organ_id), source=organ_id)
    records: dict[str, object] = {}
    digests: dict[str, str] = {}
    evidence_refs: list[str] = []
    for facet in ALL_FACETS:
        outcome = facet_results.get(facet, "pass")
        if outcome == "absent":
            continue
        eid = f"evidence:{binding_id.split(':', 1)[1]}-{facet}"
        fresh = "2026-08-26T09:00:00Z" if outcome == "stale" else "2026-08-26T13:00:00Z"
        result = "fail" if outcome == "fail" else "pass"
        raw = _evidence(eid, binding_id, facet, result=result, fresh_until=fresh)
        records[eid] = ot.parse_evidence_ref(raw, source=eid)
        digests[raw["canonical_ref"]] = raw["sha256"]
        evidence_refs.append(eid)
    binding = ot.parse_organ_binding(
        _binding(binding_id, organ_id, "service:impl", lifecycle="partial", evidence_refs=evidence_refs),
        source=binding_id,
    )
    return contract, binding, records, digests


def _project(*, provider_facets, relationship="hard", degraded_behavior=None,
             consumer_readonly=False, capability="capability:example"):
    """Assemble a consumer->provider topology + snapshot and project the capability."""
    p_contract, p_binding, records, digests = _organ_bundle(
        "organ:provider", "binding:provider", provider_facets
    )
    consumer_raw = _contract("organ:consumer")
    if consumer_readonly:
        consumer_raw["degradation"]["unknown"] = ["continue read-only and disclose provenance"]
    c_contract = ot.parse_organ_contract(consumer_raw, source="organ:consumer")

    dep_raw = _dependency("dependency:edge", "organ:consumer", "organ:provider", relationship=relationship)
    dep_raw["provider"]["capability_ref"] = capability
    if degraded_behavior is not None:
        dep_raw["degraded_behavior"] = degraded_behavior
    dependency = ot.parse_organ_dependency(dep_raw, source="dependency:edge")

    organism = ot.OrganismRecord(1, "organism:test", ("organ:consumer", "organ:provider"), "x")
    topology = ot.OrganismTopology(
        organism=organism,
        organs={"organ:consumer": c_contract, "organ:provider": p_contract},
        bindings=(p_binding,),
        dependencies=(dependency,),
        surfaces=(),
    )
    snapshot = ot.EvidenceSnapshot(records=records, canonical_ref_sha256=digests)
    return ch.evaluate_capability_health(capability, topology, snapshot, now=NOW)


class OrganStateCollapseTests(unittest.TestCase):
    def test_all_facets_pass_is_healthy(self) -> None:
        _, binding, records, digests = _organ_bundle("organ:provider", "binding:provider", {})
        contract = ot.parse_organ_contract(_contract("organ:provider"), source="organ:provider")
        health = ot.evaluate_organ_health(
            contract, binding, ot.EvidenceSnapshot(records, digests), now=NOW
        )
        self.assertIs(ch.organ_state(health), ch.CapabilityState.HEALTHY)

    def test_liveness_pass_but_correctness_fail_is_not_healthy(self) -> None:
        # Process-up (liveness/reachability pass) with failed correctness/authority
        # must NOT read healthy — a positive probe proves liveness only.
        result = _project(provider_facets={"correctness": "fail", "authority": "fail"})
        self.assertIs(result.state, ch.CapabilityState.UNAVAILABLE)

    def test_missing_facet_evidence_is_unknown_not_healthy(self) -> None:
        result = _project(provider_facets={"authority": "absent"})
        self.assertIs(result.state, ch.CapabilityState.UNKNOWN)


class TypedPropagationTests(unittest.TestCase):
    def test_hard_provider_healthy_is_healthy(self) -> None:
        result = _project(provider_facets={}, relationship="hard")
        self.assertIs(result.state, ch.CapabilityState.HEALTHY)
        self.assertIs(result.mutation_posture, ch.MutationPosture.PERMITTED)

    def test_hard_provider_unavailable_makes_capability_unavailable(self) -> None:
        result = _project(provider_facets={"correctness": "fail"}, relationship="hard")
        self.assertIs(result.state, ch.CapabilityState.UNAVAILABLE)

    def test_optional_absent_with_declared_degradation_is_degraded(self) -> None:
        result = _project(
            provider_facets={"correctness": "fail"},
            relationship="optional",
            degraded_behavior=["serve last-known read-only"],
        )
        self.assertIs(result.state, ch.CapabilityState.DEGRADED)

    def test_optional_absent_without_declared_degradation_is_unavailable(self) -> None:
        result = _project(
            provider_facets={"correctness": "fail"},
            relationship="optional",
            degraded_behavior=[],
        )
        self.assertIs(result.state, ch.CapabilityState.UNAVAILABLE)

    def test_fallback_stale_is_degraded_not_healthy(self) -> None:
        # A fallback needs current compatibility+authority; a stale provider (or no
        # selection authority evidence) can only degrade, never carry the path.
        result = _project(provider_facets={"freshness": "stale"}, relationship="fallback")
        self.assertIs(result.state, ch.CapabilityState.DEGRADED)

    def test_informational_failure_does_not_gate(self) -> None:
        result = _project(provider_facets={"correctness": "fail"}, relationship="informational")
        self.assertIs(result.state, ch.CapabilityState.HEALTHY)

    def test_capability_with_no_providing_edge_is_unknown(self) -> None:
        result = _project(provider_facets={}, capability="capability:example")
        # A different capability id has no providing edge -> unknown, never healthy.
        organism = ot.OrganismRecord(1, "organism:test", (), "x")
        empty = ot.OrganismTopology(organism, {}, (), (), ())
        out = ch.evaluate_capability_health(
            "capability:absent", empty, ot.EvidenceSnapshot.empty(), now=NOW
        )
        self.assertIs(out.state, ch.CapabilityState.UNKNOWN)


class MutationPostureTests(unittest.TestCase):
    def test_stale_authority_bearing_dependency_fails_closed(self) -> None:
        result = _project(provider_facets={"freshness": "stale"}, relationship="hard")
        self.assertIs(result.mutation_posture, ch.MutationPosture.FAIL_CLOSED)

    def test_inconsistent_authority_bearing_dependency_fails_closed(self) -> None:
        # Build a provider with conflicting current authority evidence -> inconsistent.
        p_contract = ot.parse_organ_contract(_contract("organ:provider"), source="organ:provider")
        records: dict[str, object] = {}
        digests: dict[str, str] = {}
        refs: list[str] = []
        for facet in ALL_FACETS:
            for tag, res in (("ok", "pass"),) + ((("bad", "fail"),) if facet == "authority" else ()):
                eid = f"evidence:provider-{facet}-{tag}"
                raw = _evidence(eid, "binding:provider", facet, result=res)
                records[eid] = ot.parse_evidence_ref(raw, source=eid)
                digests[raw["canonical_ref"]] = raw["sha256"]
                refs.append(eid)
        p_binding = ot.parse_organ_binding(
            _binding("binding:provider", "organ:provider", "service:impl", lifecycle="partial", evidence_refs=refs),
            source="binding:provider",
        )
        c_contract = ot.parse_organ_contract(_contract("organ:consumer"), source="organ:consumer")
        dependency = ot.parse_organ_dependency(
            _dependency("dependency:edge", "organ:consumer", "organ:provider", relationship="hard"),
            source="dependency:edge",
        )
        topology = ot.OrganismTopology(
            ot.OrganismRecord(1, "organism:test", ("organ:consumer", "organ:provider"), "x"),
            {"organ:consumer": c_contract, "organ:provider": p_contract},
            (p_binding,), (dependency,), (),
        )
        out = ch.evaluate_capability_health(
            "capability:example", topology, ot.EvidenceSnapshot(records, digests), now=NOW
        )
        self.assertIs(out.state, ch.CapabilityState.INCONSISTENT)
        self.assertIs(out.mutation_posture, ch.MutationPosture.FAIL_CLOSED)

    def test_provisional_readonly_only_when_consumer_declares_it(self) -> None:
        result = _project(
            provider_facets={"authority": "absent"},
            relationship="hard",
            consumer_readonly=True,
        )
        self.assertIs(result.mutation_posture, ch.MutationPosture.PROVISIONAL_READONLY)
        self.assertTrue(result.disclosures)


class FleetHealthAdapterTests(unittest.TestCase):
    _REPORT = {
        "agents": [
            {"agent": "orchestrator", "status": "OK", "checks": []},
            {"agent": "recovery", "status": "PAUSED", "checks": []},
            {"agent": "codex", "status": "FAIL", "checks": []},
            {"agent": "mason", "status": "OK", "checks": []},  # no organ binding -> excluded
        ]
    }

    def test_adapter_maps_only_liveness_and_only_bound_agents(self) -> None:
        snapshot = ch.evidence_from_fleet_health(
            self._REPORT, observed_at="2026-08-26T11:00:00Z", fresh_until="2026-08-26T13:00:00Z"
        )
        self.assertEqual(len(snapshot.records), 3)  # orchestrator, recovery, codex; mason excluded
        by_binding = {r.subject_ref.id: r for r in snapshot.records.values()}
        self.assertEqual(
            {r.facet for r in snapshot.records.values()}, {ot.HealthFacet.LIVENESS}
        )
        self.assertIs(
            by_binding["binding:pantheon-executive-orchestrator"].result, ot.EvidenceResult.PASS
        )
        self.assertIs(by_binding["binding:harness-adapters-codex"].result, ot.EvidenceResult.FAIL)
        # PAUSED is neither pass nor fail -> ambiguous, cannot support a live claim.
        self.assertIs(
            by_binding["binding:recovery-homeostasis-recovery"].result, ot.EvidenceResult.AMBIGUOUS
        )

    def test_liveness_only_evidence_keeps_organs_unknown(self) -> None:
        # A running seat proves liveness only; with the other six required facets
        # absent, the projected organ state must stay UNKNOWN — matching T-3187.
        contract = ot.parse_organ_contract(_contract("organ:pantheon-executive"), source="x")
        binding = ot.parse_organ_binding(
            _binding(
                "binding:pantheon-executive-orchestrator", "organ:pantheon-executive", "persona:orchestrator",
                implementation_kind="persona", lifecycle="partial",
                evidence_refs=["evidence:fleet-health-orchestrator-liveness"],
            ),
            source="x",
        )
        topology = ot.OrganismTopology(
            ot.OrganismRecord(1, "organism:test", ("organ:pantheon-executive",), "x"),
            {"organ:pantheon-executive": contract}, (binding,), (), (),
        )
        snapshot = ch.evidence_from_fleet_health(
            self._REPORT, observed_at="2026-08-26T11:00:00Z", fresh_until="2026-08-26T13:00:00Z"
        )
        reports = ch.project_topology_health(topology, snapshot, now=NOW)
        self.assertEqual(len(reports), 1)
        self.assertIs(reports[0].state, ch.CapabilityState.UNKNOWN)
        # ...yet the liveness facet itself is healthy — the projection is honest, not blind.
        self.assertIs(reports[0].facets[ot.HealthFacet.LIVENESS], ot.FacetState.HEALTHY)


if __name__ == "__main__":
    unittest.main()
