from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

try:
    from tools import organism_topology as ot
except ImportError:
    ot = None  # type: ignore[assignment]


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _write_yaml(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def _port(port_id: str) -> dict[str, object]:
    return {
        "port_id": port_id,
        "contract_ref": "contract:example-v1",
        "schema_ref": "schema:example-v1",
        "version_range": "1",
        "authority_ref": "role:orchestrator",
        "confidentiality": "internal",
        "freshness_bound": "PT5M",
    }


def _contract(
    organ_id: str,
    *,
    inputs: list[dict[str, object]] | None = None,
    outputs: list[dict[str, object]] | None = None,
    evidence_refs: list[str] | None = None,
) -> dict[str, object]:
    inputs = inputs or []
    outputs = outputs or []
    return {
        "schema_version": 1,
        "organ_id": organ_id,
        "responsibility": f"Responsibility for {organ_id}.",
        "must_not_become": ["A replacement for the whole organism."],
        "ports": {"inputs": inputs, "outputs": outputs},
        "authoritative_inputs": [item["port_id"] for item in inputs],
        "authoritative_outputs": [item["port_id"] for item in outputs],
        "boundaries": {
            "identity": ["identity-boundary"],
            "data": ["data-boundary"],
            "trust": ["trust-boundary"],
            "authority": ["authority-boundary"],
        },
        "expectations": {"latency": [], "availability": []},
        "health": {
            "required_facets": [
                "reachability",
                "liveness",
                "readiness",
                "correctness",
                "freshness",
                "authority",
                "continuity",
            ],
            "evidence_requirements": evidence_refs or [],
        },
        "failure_modes": ["declared-failure"],
        "degradation": {
            "unavailable": ["disclose-unavailable"],
            "stale": ["disclose-stale"],
            "partitioned": ["disclose-partition"],
            "inconsistent": ["preserve-conflict"],
            "unknown": ["disclose-unknown"],
        },
        "recovery": {
            "owner_ref": "role:recovery",
            "escalation_ref": "role:operator",
            "verification_requirements": ["verify-correctness"],
        },
        "replacement": {
            "continuity_requirements": ["preserve-continuity"],
            "provenance_requirements": ["preserve-provenance"],
            "rollback_requirements": ["prove-rollback"],
        },
        "owners": ["role:orchestrator"],
        "project_refs": [],
        "component_refs": [],
        "obligation_refs": [],
        "re_evaluate_when": [],
    }


def _binding(
    binding_id: str,
    organ_id: str,
    implementation_id: str,
    *,
    implementation_kind: str = "service",
    lifecycle: str = "planned",
    evidence_refs: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "binding_id": binding_id,
        "organ_id": organ_id,
        "implementation_ref": {"kind": implementation_kind, "id": implementation_id},
        "seed_ref": "seed:test",
        "locus": "test",
        "ports": [],
        "lifecycle": lifecycle,
        "authority_owner_ref": "role:orchestrator",
        "evidence_refs": evidence_refs or [],
        "verified_at": "2026-08-26T11:00:00Z" if lifecycle == "live" else None,
        "fresh_until": "2026-08-26T13:00:00Z" if lifecycle == "live" else None,
    }


def _dependency(
    dependency_id: str,
    consumer: str,
    provider: str,
    *,
    relationship: str = "hard",
    rule: str = "sole",
    authority_ref: str | None = None,
    priority: int | None = None,
    selection_evidence: list[str] | None = None,
    feedback: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "dependency_id": dependency_id,
        "consumer": {"organ_id": consumer, "port_id": "port:in"},
        "provider": {
            "organ_id": provider,
            "port_id": "port:out",
            "capability_ref": "capability:example",
        },
        "relationship": relationship,
        "contract_ref": "contract:example-v1",
        "schema_ref": "schema:example-v1",
        "version_range": "1",
        "freshness_bound": "PT5M",
        "failure_propagation": ["unavailable"],
        "degraded_behavior": [],
        "owner_ref": "role:orchestrator",
        "evidence_refs": [],
        "selection": {
            "authority_ref": authority_ref,
            "rule": rule,
            "priority": priority,
            "evidence_refs": selection_evidence or [],
        },
        "feedback": feedback or {"bootstrap": [], "bound": None, "break_condition": []},
    }


def _evidence(
    evidence_id: str,
    subject_id: str,
    facet: str,
    *,
    subject_kind: str = "binding",
    result: str = "pass",
    digest: str | None = None,
    fresh_until: str | None = "2026-08-26T13:00:00Z",
    evidence_kind: str = "probe",
) -> dict[str, object]:
    canonical_ref = f"receipt:{evidence_id}"
    return {
        "schema_version": 1,
        "evidence_id": evidence_id,
        "subject_ref": {"kind": subject_kind, "id": subject_id},
        "facet": facet,
        "evidence_kind": evidence_kind,
        "canonical_ref": canonical_ref,
        "sha256": digest or "sha256:" + "a" * 64,
        "observer_ref": "role:recovery",
        "observed_at": "2026-08-26T11:00:00Z",
        "fresh_until": fresh_until,
        "result": result,
    }


def _fixture_tree(root: Path) -> None:
    _write_yaml(
        root / "organism" / "records" / "organism.yaml",
        {
            "schema_version": 1,
            "organism_id": "organism:test",
            "organ_ids": ["organ:consumer", "organ:provider"],
        },
    )
    _write_yaml(
        root / "organism" / "organs" / "consumer.yaml",
        _contract("organ:consumer", inputs=[_port("port:in")]),
    )
    _write_yaml(
        root / "organism" / "organs" / "provider.yaml",
        _contract("organ:provider", outputs=[_port("port:out")]),
    )
    for name in ("bindings", "dependencies", "surfaces"):
        (root / "organism" / name).mkdir(parents=True, exist_ok=True)
    (root / "projects" / "records").mkdir(parents=True)
    (root / "components" / "records").mkdir(parents=True)


class SchemaAndIdentityTests(unittest.TestCase):
    def test_typed_identity_prefixes_are_exact_and_equal_suffixes_do_not_alias(self) -> None:
        assert ot is not None
        ot.validate_identity_ref(ot.TypedIdentityRef(ot.IdentityKind.ORGAN, "organ:shared"))
        ot.validate_identity_ref(ot.TypedIdentityRef(ot.IdentityKind.SERVICE, "service:shared"))
        with self.assertRaises(ot.OrganismTopologyError):
            ot.validate_identity_ref(ot.TypedIdentityRef(ot.IdentityKind.ORGAN, "service:shared"))

    def test_strict_yaml_rejects_duplicate_anchor_tag_and_symlink(self) -> None:
        assert ot is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "consumer.yaml"
            path.write_text("schema_version: 1\nschema_version: 1\n", encoding="utf-8")
            with self.assertRaises(ot.OrganismTopologyError):
                ot.load_organ_contracts(root)
            path.write_text("x: &x 1\ny: *x\n", encoding="utf-8")
            with self.assertRaises(ot.OrganismTopologyError):
                ot.load_organ_contracts(root)
            path.unlink()
            target = root / "target.yaml"
            _write_yaml(target, _contract("organ:target"))
            (root / "consumer.yaml").symlink_to(target)
            with self.assertRaises(ot.OrganismTopologyError):
                ot.load_organ_contracts(root)

    def test_unknown_fields_and_filename_identity_mismatch_refuse(self) -> None:
        assert ot is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = _contract("organ:other")
            value["unknown"] = True
            _write_yaml(root / "consumer.yaml", value)
            with self.assertRaises(ot.OrganismTopologyError):
                ot.load_organ_contracts(root)

    def test_root_membership_must_equal_loaded_organs(self) -> None:
        assert ot is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_tree(root)
            topology = ot.load_organism_topology(root)
            topology = dataclasses.replace(
                topology,
                organism=dataclasses.replace(topology.organism, organ_ids=("organ:consumer",)),
            )
            issues = ot.validate_topology(
                topology, ot.ReferenceIndexes.empty(), ot.EvidenceSnapshot.empty(), now=NOW
            )
            self.assertIn("organ-membership-mismatch", {item.code for item in issues})

    def test_many_to_many_bindings_pass_without_identity_aliasing(self) -> None:
        assert ot is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_tree(root)
            _write_yaml(
                root / "organism" / "bindings" / "one.yaml",
                _binding(
                    "binding:one", "organ:consumer", "persona:shared",
                    implementation_kind="persona",
                ),
            )
            _write_yaml(
                root / "organism" / "bindings" / "two.yaml",
                _binding(
                    "binding:two", "organ:provider", "persona:shared",
                    implementation_kind="persona",
                ),
            )
            _write_yaml(
                root / "organism" / "bindings" / "three.yaml",
                _binding("binding:three", "organ:consumer", "service:alternate"),
            )
            topology = ot.load_organism_topology(root)
            issues = ot.validate_topology(
                topology, ot.ReferenceIndexes.empty(capability_ids={"example"}),
                ot.EvidenceSnapshot.empty(), now=NOW
            )
            self.assertEqual(issues, ())

    def test_duplicate_binding_and_dependency_ids_refuse(self) -> None:
        assert ot is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_tree(root)
            _write_yaml(
                root / "organism" / "bindings" / "one.yaml",
                _binding("binding:one", "organ:consumer", "service:one"),
            )
            _write_yaml(
                root / "organism" / "dependencies" / "one.yaml",
                _dependency("dependency:one", "organ:consumer", "organ:provider"),
            )
            topology = ot.load_organism_topology(root)
            topology = dataclasses.replace(
                topology,
                bindings=topology.bindings * 2,
                dependencies=topology.dependencies * 2,
            )
            issues = ot.validate_topology(
                topology,
                ot.ReferenceIndexes.empty(capability_ids={"example"}),
                ot.EvidenceSnapshot.empty(),
                now=NOW,
            )
            self.assertTrue(
                {"duplicate-binding", "duplicate-dependency"}
                <= {item.code for item in issues}
            )

    def test_malformed_canonical_digest_index_refuses_cleanly(self) -> None:
        assert ot is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_tree(root)
            for malformed in (
                {"not-a-ref": "sha256:" + "a" * 64},
                {"receipt:test": 1},
            ):
                with self.subTest(malformed=malformed):
                    with self.assertRaises(ot.OrganismTopologyError):
                        ot.build_reference_indexes(
                            root, canonical_ref_sha256=malformed  # type: ignore[arg-type]
                        )

    def test_implementation_kind_cannot_be_organism_or_organ(self) -> None:
        assert ot is not None
        value = _binding("binding:bad", "organ:consumer", "organ:bad")
        value["implementation_ref"] = {"kind": "organ", "id": "organ:bad"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_yaml(root / "bad.yaml", value)
            with self.assertRaises(ot.OrganismTopologyError):
                ot.load_organ_bindings(root)

    def test_external_project_component_and_obligation_refs_are_authoritative(self) -> None:
        assert ot is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_tree(root)
            path = root / "organism" / "organs" / "consumer.yaml"
            value = _contract("organ:consumer", inputs=[_port("port:in")])
            value["project_refs"] = ["project:missing"]
            value["component_refs"] = ["component:missing"]
            value["obligation_refs"] = ["ORG-056:R1"]
            _write_yaml(path, value)
            topology = ot.load_organism_topology(root)
            issues = ot.validate_topology(
                topology, ot.ReferenceIndexes.empty(), ot.EvidenceSnapshot.empty(), now=NOW
            )
            self.assertTrue(
                {"unknown-project", "unknown-component", "unknown-obligation"}
                <= {item.code for item in issues}
            )


class DependencyAndSelectionTests(unittest.TestCase):
    def _topology(self, *dependencies: dict[str, object]) -> Any:
        assert ot is not None
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        _fixture_tree(root)
        for value in dependencies:
            suffix = str(value["dependency_id"]).split(":", 1)[1]
            _write_yaml(root / "organism" / "dependencies" / f"{suffix}.yaml", value)
        return ot.load_organism_topology(root)

    def _issues(self, topology: Any, evidence: Any | None = None) -> set[str]:
        assert ot is not None
        return {
            issue.code
            for issue in ot.validate_topology(
                topology,
                ot.ReferenceIndexes.empty(capability_ids={"example"}),
                evidence or ot.EvidenceSnapshot.empty(),
                now=NOW,
            )
        }

    def test_one_sole_provider_is_valid(self) -> None:
        topology = self._topology(_dependency("dependency:one", "organ:consumer", "organ:provider"))
        self.assertEqual(self._issues(topology), set())

    def test_one_sole_provider_may_declare_selection_authority(self) -> None:
        topology = self._topology(
            _dependency(
                "dependency:one",
                "organ:consumer",
                "organ:provider",
                authority_ref="role:orchestrator",
            )
        )
        self.assertEqual(self._issues(topology), set())

    def test_dangling_port_capability_and_organ_refuse(self) -> None:
        assert ot is not None
        topology = self._topology(_dependency("dependency:one", "organ:consumer", "organ:provider"))
        refs = ot.ReferenceIndexes.empty()
        issues = ot.validate_topology(topology, refs, ot.EvidenceSnapshot.empty(), now=NOW)
        self.assertIn("unknown-capability", {item.code for item in issues})
        bad = dataclasses.replace(
            topology.dependencies[0],
            provider=dataclasses.replace(topology.dependencies[0].provider, port_id="port:missing"),
        )
        issues = ot.validate_topology(
            dataclasses.replace(topology, dependencies=(bad,)),
            ot.ReferenceIndexes.empty(capability_ids={"example"}),
            ot.EvidenceSnapshot.empty(),
            now=NOW,
        )
        self.assertIn("unknown-provider-port", {item.code for item in issues})

    def test_consumer_contract_mismatch_refuses(self) -> None:
        assert ot is not None
        topology = self._topology(_dependency("dependency:one", "organ:consumer", "organ:provider"))
        bad = dataclasses.replace(topology.dependencies[0], contract_ref="contract:wrong-v1")
        self.assertIn(
            "consumer-contract-mismatch",
            self._issues(dataclasses.replace(topology, dependencies=(bad,))),
        )

    def test_multiple_providers_require_shared_authority_and_rule(self) -> None:
        assert ot is not None
        first = _dependency(
            "dependency:one", "organ:consumer", "organ:provider",
            rule="priority", authority_ref="role:orchestrator", priority=1,
        )
        second = _dependency(
            "dependency:two", "organ:consumer", "organ:provider",
            rule="priority", authority_ref="role:other", priority=2,
        )
        self.assertIn("selection-authority-mismatch", self._issues(self._topology(first, second)))

    def test_priority_selection_requires_unique_integer_priorities(self) -> None:
        first = _dependency(
            "dependency:one", "organ:consumer", "organ:provider",
            rule="priority", authority_ref="role:orchestrator", priority=1,
        )
        second = _dependency(
            "dependency:two", "organ:consumer", "organ:provider",
            rule="priority", authority_ref="role:orchestrator", priority=1,
        )
        self.assertIn("selection-priority-duplicate", self._issues(self._topology(first, second)))

    def test_multiple_providers_reject_missing_authority_rule_or_priority(self) -> None:
        first = _dependency(
            "dependency:one", "organ:consumer", "organ:provider",
            rule="priority", authority_ref=None, priority=None,
        )
        second = _dependency(
            "dependency:two", "organ:consumer", "organ:provider",
            rule="explicit", authority_ref=None, priority=None,
        )
        issues = self._issues(self._topology(first, second))
        self.assertTrue(
            {"selection-authority-mismatch", "selection-rule-mismatch"} <= issues
        )

        second["selection"]["rule"] = "priority"  # type: ignore[index]
        issues = self._issues(self._topology(first, second))
        self.assertIn("selection-priority-missing", issues)

    def test_explicit_selection_requires_current_authority_evidence(self) -> None:
        first = _dependency(
            "dependency:one", "organ:consumer", "organ:provider",
            rule="explicit", authority_ref="role:orchestrator",
            selection_evidence=["evidence:select"],
        )
        second = _dependency(
            "dependency:two", "organ:consumer", "organ:provider",
            rule="explicit", authority_ref="role:orchestrator",
            selection_evidence=["evidence:select"],
        )
        self.assertIn("selection-evidence-invalid", self._issues(self._topology(first, second)))

    def test_explicit_selection_accepts_current_subject_bound_authority_evidence(self) -> None:
        assert ot is not None
        first = _dependency(
            "dependency:one", "organ:consumer", "organ:provider",
            rule="explicit", authority_ref="role:orchestrator",
            selection_evidence=["evidence:select-one"],
        )
        second = _dependency(
            "dependency:two", "organ:consumer", "organ:provider",
            rule="explicit", authority_ref="role:orchestrator",
            selection_evidence=["evidence:select-two"],
        )
        values = {
            "evidence:select-one": _evidence(
                "evidence:select-one", "dependency:one", "authority",
                subject_kind="dependency",
            ),
            "evidence:select-two": _evidence(
                "evidence:select-two", "dependency:two", "authority",
                subject_kind="dependency",
            ),
        }
        snapshot = ot.EvidenceSnapshot(
            records={
                key: ot.parse_evidence_ref(value, source="fixture")
                for key, value in values.items()
            },
            canonical_ref_sha256={
                str(value["canonical_ref"]): str(value["sha256"])
                for value in values.values()
            },
        )
        topology = self._topology(first, second)
        self.assertNotIn("selection-evidence-invalid", self._issues(topology, snapshot))

        wrong = dataclasses.replace(
            snapshot.records["evidence:select-two"],
            subject_ref=ot.EvidenceSubjectRef(
                ot.EvidenceSubjectKind.DEPENDENCY, "dependency:one"
            ),
        )
        records = dict(snapshot.records)
        records[wrong.evidence_id] = wrong
        self.assertIn(
            "selection-evidence-invalid",
            self._issues(
                topology,
                ot.EvidenceSnapshot(records, snapshot.canonical_ref_sha256),
            ),
        )

    def test_unmarked_cycle_refuses_and_bounded_feedback_cycle_passes(self) -> None:
        assert ot is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_tree(root)
            consumer_path = root / "organism" / "organs" / "consumer.yaml"
            provider_path = root / "organism" / "organs" / "provider.yaml"
            _write_yaml(
                consumer_path,
                _contract("organ:consumer", inputs=[_port("port:in")], outputs=[_port("port:out")]),
            )
            _write_yaml(
                provider_path,
                _contract("organ:provider", inputs=[_port("port:in")], outputs=[_port("port:out")]),
            )
            one = _dependency("dependency:one", "organ:consumer", "organ:provider")
            two = _dependency("dependency:two", "organ:provider", "organ:consumer")
            _write_yaml(root / "organism" / "dependencies" / "one.yaml", one)
            _write_yaml(root / "organism" / "dependencies" / "two.yaml", two)
            topology = ot.load_organism_topology(root)
            self.assertIn("dependency-cycle", self._issues(topology))
            bounded = {"bootstrap": ["initial-state"], "bound": "PT5M", "break_condition": ["stop"]}
            one.update(relationship="feedback", feedback=bounded)
            two.update(relationship="feedback", feedback=bounded)
            _write_yaml(root / "organism" / "dependencies" / "one.yaml", one)
            _write_yaml(root / "organism" / "dependencies" / "two.yaml", two)
            self.assertNotIn("dependency-cycle", self._issues(ot.load_organism_topology(root)))

    def test_feedback_semantics_on_nonfeedback_edge_refuse(self) -> None:
        value = _dependency(
            "dependency:one", "organ:consumer", "organ:provider",
            feedback={"bootstrap": ["x"], "bound": "PT1M", "break_condition": ["y"]},
        )
        self.assertIn("feedback-shape-invalid", self._issues(self._topology(value)))


class EvidenceAndLifecycleTests(unittest.TestCase):
    def _live_case(self, overrides: dict[str, dict[str, object]] | None = None) -> tuple[Any, Any, Any]:
        assert ot is not None
        contract = ot.parse_organ_contract(_contract("organ:consumer"), source="fixture")
        evidence_values: dict[str, dict[str, object]] = {}
        ids: list[str] = []
        for facet in ot.HealthFacet:
            evidence_id = f"evidence:{facet.value}"
            ids.append(evidence_id)
            value = _evidence(evidence_id, "binding:live", facet.value)
            if overrides and facet.value in overrides:
                value.update(overrides[facet.value])
            evidence_values[evidence_id] = value
        binding = ot.parse_organ_binding(
            _binding("binding:live", "organ:consumer", "service:one", lifecycle="live", evidence_refs=ids),
            source="fixture",
        )
        digests = {
            value["canonical_ref"]: value["sha256"]
            for value in evidence_values.values()
        }
        snapshot = ot.EvidenceSnapshot(
            records={
                evidence_id: ot.parse_evidence_ref(value, source="fixture")
                for evidence_id, value in evidence_values.items()
            },
            canonical_ref_sha256=digests,
        )
        return contract, binding, snapshot

    def test_complete_current_independent_facets_support_live(self) -> None:
        assert ot is not None
        contract, binding, snapshot = self._live_case()
        health = ot.evaluate_organ_health(contract, binding, snapshot, now=NOW)
        self.assertTrue(all(state is ot.FacetState.HEALTHY for state in health.facets.values()))

    def test_liveness_only_cannot_satisfy_correctness_or_authority(self) -> None:
        assert ot is not None
        contract, binding, snapshot = self._live_case()
        only = {key: value for key, value in snapshot.records.items() if key == "evidence:liveness"}
        health = ot.evaluate_organ_health(
            contract,
            binding,
            ot.EvidenceSnapshot(records=only, canonical_ref_sha256=snapshot.canonical_ref_sha256),
            now=NOW,
        )
        self.assertIs(health.facets[ot.HealthFacet.LIVENESS], ot.FacetState.HEALTHY)
        self.assertIs(health.facets[ot.HealthFacet.CORRECTNESS], ot.FacetState.UNKNOWN)
        self.assertIs(health.facets[ot.HealthFacet.AUTHORITY], ot.FacetState.UNKNOWN)

    def test_expired_ambiguous_digest_mismatch_and_declaration_refuse_live(self) -> None:
        assert ot is not None
        cases = [
            {"fresh_until": "2026-08-26T10:00:00Z"},
            {"result": "ambiguous"},
            {"sha256": "sha256:" + "b" * 64},
            {"evidence_kind": "declaration"},
        ]
        for override in cases:
            with self.subTest(override=override):
                contract, binding, snapshot = self._live_case({"authority": override})
                if "sha256" in override:
                    snapshot = dataclasses.replace(
                        snapshot,
                        canonical_ref_sha256={
                            key: ("sha256:" + "a" * 64 if key.endswith("authority") else value)
                            for key, value in snapshot.canonical_ref_sha256.items()
                        },
                    )
                health = ot.evaluate_organ_health(contract, binding, snapshot, now=NOW)
                self.assertIsNot(health.facets[ot.HealthFacet.AUTHORITY], ot.FacetState.HEALTHY)

    def test_conflicting_current_evidence_is_inconsistent(self) -> None:
        assert ot is not None
        contract, binding, snapshot = self._live_case()
        fail = ot.parse_evidence_ref(
            _evidence("evidence:authority-fail", "binding:live", "authority", result="fail"),
            source="fixture",
        )
        records = dict(snapshot.records)
        records[fail.evidence_id] = fail
        digests = dict(snapshot.canonical_ref_sha256)
        digests[fail.canonical_ref] = fail.sha256
        binding = dataclasses.replace(
            binding, evidence_refs=binding.evidence_refs + (fail.evidence_id,)
        )
        health = ot.evaluate_organ_health(
            contract,
            binding,
            ot.EvidenceSnapshot(records=records, canonical_ref_sha256=digests),
            now=NOW,
        )
        self.assertIs(health.facets[ot.HealthFacet.AUTHORITY], ot.FacetState.INCONSISTENT)

    def test_live_binding_without_complete_current_facets_emits_issue(self) -> None:
        assert ot is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_tree(root)
            _write_yaml(
                root / "organism" / "bindings" / "live.yaml",
                _binding("binding:live", "organ:consumer", "service:one", lifecycle="live"),
            )
            topology = ot.load_organism_topology(root)
            issues = ot.validate_topology(
                topology,
                ot.ReferenceIndexes.empty(capability_ids={"example"}),
                ot.EvidenceSnapshot.empty(),
                now=NOW,
            )
            self.assertIn("unsupported-live-binding", {item.code for item in issues})


class GeneratedArtifactsAndCanonicalTests(unittest.TestCase):
    def test_all_six_generated_schemas_are_closed_and_deterministic(self) -> None:
        assert ot is not None
        generated = ot.generate_topology_schemas()
        self.assertEqual(len(generated), 6)
        self.assertEqual(generated, ot.generate_topology_schemas())
        for payload in generated.values():
            schema = json.loads(payload)
            self.assertFalse(schema["additionalProperties"])

    def test_generated_index_is_byte_stable_and_binds_source_digests(self) -> None:
        assert ot is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_tree(root)
            topology = ot.load_organism_topology(root)
            one = ot.generate_topology_index(topology)
            two = ot.generate_topology_index(topology)
            self.assertEqual(one, two)
            decoded = json.loads(one)
            self.assertEqual(decoded["organs"]["organ:consumer"]["record_sha256"],
                             "sha256:" + topology.organs["organ:consumer"].source_sha256)

    def test_missing_or_tampered_schema_and_index_refuse(self) -> None:
        assert ot is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_tree(root)
            issues = ot.check_organism_topology(root, now=NOW)
            self.assertIn("schema-stale", {item.code for item in issues})
            ot.write_generated_artifacts(root)
            (root / "organism" / "index.json").write_text("{}\n", encoding="utf-8")
            issues = ot.check_organism_topology(root, now=NOW)
            self.assertIn("index-stale", {item.code for item in issues})

            ot.write_generated_artifacts(root)
            schema = root / "schemas" / "organ-contract.schema.json"
            copied = root / "copied-schema.json"
            copied.write_bytes(schema.read_bytes())
            schema.unlink()
            schema.symlink_to(copied)
            issues = ot.check_organism_topology(root, now=NOW)
            self.assertIn("schema-stale", {item.code for item in issues})

    def test_cli_generate_then_check_real_temp_tree(self) -> None:
        assert ot is not None
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _fixture_tree(root)
            env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
            generated = subprocess.run(
                [sys.executable, "-m", "tools.organism_topology", "generate", "--repo-root", str(root)],
                cwd=Path(__file__).resolve().parents[1], env=env, text=True, capture_output=True,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            checked = subprocess.run(
                [sys.executable, "-m", "tools.organism_topology", "check", "--repo-root", str(root)],
                cwd=Path(__file__).resolve().parents[1], env=env, text=True, capture_output=True,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_live_canonical_topology_has_exact_twelve_contracts_and_unit_c_bindings(self) -> None:
        assert ot is not None
        repo_root = Path(__file__).resolve().parents[1]
        topology = ot.load_organism_topology(repo_root)
        self.assertEqual(set(topology.organs), set(ot.CANONICAL_ORGAN_IDS))
        self.assertEqual(topology.organism.organism_id, "organism:grand-project")
        # T-3187 Unit C: the read-only live-binding inventory populated conservative
        # bindings (empty evidence_refs, non-live) for the evidenced organ
        # implementations. Dependencies and surfaces remain empty because the ratified
        # organ contracts declare no ports for them to attach to.
        self.assertEqual(
            {binding.binding_id for binding in topology.bindings},
            {
                "binding:pantheon-executive-orchestrator",
                "binding:recovery-homeostasis-recovery",
                "binding:intelligence-research-specialist",
                "binding:harness-adapters-claude",
                "binding:harness-adapters-codex",
                "binding:operator-relationship-operator",
                "binding:exocortex-mnemosyne",
            },
        )
        # A read-only inventory cannot certify every required facet, so no binding is live.
        self.assertTrue(all(binding.lifecycle.value != "live" for binding in topology.bindings))
        self.assertEqual(topology.dependencies, ())
        self.assertEqual(topology.surfaces, ())
        self.assertEqual(ot.check_organism_topology(repo_root, now=NOW), ())

    def test_ratified_responsibility_and_must_not_text_is_preserved(self) -> None:
        assert ot is not None
        repo_root = Path(__file__).resolve().parents[1]
        topology = ot.load_organism_topology(repo_root)
        expected = {
            "organ:operator-relationship": (
                "Originating desire, ultimate authority, consent, and reorientation",
                "A hidden implementation detail",
            ),
            "organ:bridge": (
                "Coherent conversation, outside communication, answer-vs-delegate judgment, result synthesis, relationship-facing continuity",
                "A dumb router, universal worker, or memory database",
            ),
            "organ:pantheon-executive": (
                "Planning, dispatch, review, project control, recovery coordination, and safe actuation",
                "The companion's only identity or every domain's user-facing voice",
            ),
            "organ:exocortex": (
                "Canonical durable personal/general memory, consolidation, values/context, retrieval, correction and forgetting",
                "A raw transcript dump or unbounded surveillance store",
            ),
            "organ:echo-prismatic": (
                "Speech/audio digestion, conversation state, speaker/consent signals, intent event substrate",
                "The personality, product face, or privileged action path",
            ),
            "organ:intelligence-research": (
                "Environmental search, monitoring, sourced synthesis, novelty detection",
                "Unreviewed truth or independent policy authority",
            ),
            "organ:recovery-homeostasis": (
                "Install, verify, monitor, repair, rollback, migration, and continuity proof",
                "A second uncontrolled orchestrator",
            ),
            "organ:harness-adapters": (
                "Translate stable roles/capabilities to model/runtime-specific surfaces",
                "Canonical persona identity or policy truth",
            ),
            "organ:capability-navigator": (
                "Discover and load a narrow tool surface, route invocation, normalize receipts",
                "Authority issuer, generic shell, or context dump",
            ),
            "organ:federation-passport": (
                "Authenticate seeds/presenters and transport bounded inter-system work",
                "Content-trust oracle or implicit authorization",
            ),
            "organ:senses-actuators": (
                "Observe and affect external environments through typed trust boundaries",
                "Ambient unrestricted collection or action",
            ),
            "organ:presence-surfaces": (
                "Render optional visual/physical/social presence",
                "Identity source or required control plane",
            ),
        }
        self.assertEqual(
            {
                organ_id: (record.responsibility, record.must_not_become[0])
                for organ_id, record in topology.organs.items()
            },
            expected,
        )

    def test_ci_paths_cover_organism_and_all_schemas(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        ci = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertGreaterEqual(ci.count("'organism/**'"), 2)
        for filename in (
            "organism-record.schema.json",
            "organ-contract.schema.json",
            "organ-binding.schema.json",
            "organ-dependency.schema.json",
            "organ-evidence-ref.schema.json",
            "external-surface.schema.json",
        ):
            self.assertGreaterEqual(ci.count(f"'schemas/{filename}'"), 2)


if __name__ == "__main__":
    unittest.main()
