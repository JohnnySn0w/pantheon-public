"""Tests for the ORG-059 capability navigator contract (T-3218)."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import yaml

from tools import assembly_resolver as ar
from tools import capability_navigator_contract as cn
from tools import component_catalogue as cc


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILES = {
    "capability-navigator-projection.schema.json": cn.generate_projection_schema,
    "capability-navigator-request.schema.json": cn.generate_request_schema,
    "capability-navigator-result.schema.json": cn.generate_result_schema,
    "capability-navigator-receipt.schema.json": cn.generate_receipt_schema,
}


class FixtureRepoResolver:
    def resolve(self, logical_repo: str) -> ar.ResolvedSource:
        return ar.ResolvedSource(
            logical_repo=logical_repo,
            resolved_source=f"mirror://{logical_repo}",
            revision=f"revision:{logical_repo}-1",
            artifact_digest=f"sha256:{logical_repo}-artifact",
        )


class FixtureEvidence:
    def resolve(self, evidence_ref: str) -> ar.EvidencePresence:
        return ar.EvidencePresence(evidence_ref, present=True, digest=f"digest:{evidence_ref}")


def _provision(
    capability: str = "alpha-capability",
    *,
    contract: str = "alpha-contract",
    schema: str = "alpha-schema",
    version: str = "1.0.0",
    features: list[str] | None = None,
    equivalence_class: str | None = "equiv-alpha",
    trust_class: str | None = "trust-local",
    subject_visibility: dict[str, object] | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {
        "capability": capability,
        "contract": contract,
        "schema": schema,
        "version": version,
        "features": [] if features is None else features,
        "equivalence_class": equivalence_class,
        "trust_class": trust_class,
    }
    if subject_visibility is not None:
        data["subject_visibility"] = subject_visibility
    return data


def _subject_visibility(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "subject_refs": ["subject:alpha"],
        "role_refs": ["role:worker"],
        "instance_refs": [],
        "trust_zones": ["subject-visible-zone"],
        "policy_refs": ["policy:alpha-visibility"],
        "evidence_refs": ["evidence:alpha-visibility"],
    }
    data.update(overrides)
    return data


def _navigator_selection(
    role: str = "primary",
    *,
    primary_provider_component_id: str | None = None,
    fallback_rank: int | None = None,
    provider_organ_id: str = "organ:alpha-provider",
    health_dependency_id: str = "dependency:alpha-provider-health",
) -> dict[str, object]:
    return {
        "role": role,
        "primary_provider_component_id": primary_provider_component_id,
        "fallback_rank": fallback_rank,
        "provider_organ_id": provider_organ_id,
        "health_dependency_id": health_dependency_id,
    }


def _requirement(
    capability: str = "alpha-capability",
    *,
    contract: str = "alpha-contract",
    schema: str = "alpha-schema",
    version: str = "1.0.0",
    feature: str | None = None,
    enabled: bool = True,
) -> dict[str, object]:
    return {
        "capability": capability,
        "contract": contract,
        "schema": schema,
        "version": version,
        "feature": feature,
        "enabled": enabled,
        "degradation_ref": None,
    }


def _implication(implication_id: str = "alpha-authority") -> dict[str, object]:
    return {
        "implication_id": implication_id,
        "scope": "seed:alpha",
        "evidence_refs": [f"evidence:{implication_id}"],
    }


def _cost(cost_id: str = "alpha-cost") -> dict[str, object]:
    return {
        "cost_id": cost_id,
        "category": "compute",
        "quantity": 1.0,
        "unit": "cpu-hour",
        "cadence": "monthly",
        "measurement_source": f"evidence:{cost_id}",
        "owner": "role:alpha-steward",
        "alternative_ref": None,
        "last_measured_at": "2026-09-22T00:00:00Z",
        "review_due_at": "2026-10-22T00:00:00Z",
    }


def _record(
    component_id: str = "alpha-component",
    *,
    provides: list[dict[str, object]] | None = None,
    hard: list[dict[str, object]] | None = None,
    optional: list[dict[str, object]] | None = None,
    implication_id: str = "alpha-authority",
    health_evidence: list[str] | None = None,
) -> dict[str, object]:
    repo = f"{component_id}-repo"
    return {
        "schema_version": 1,
        "component_id": component_id,
        "project_id": "alpha-project",
        "kind": "service",
        "source": {"logical_repo": repo, "path": None, "artifact": None},
        "provides": [_provision()] if provides is None else provides,
        "requires": {"hard": [] if hard is None else hard, "optional": [] if optional is None else optional},
        "compatibility": {
            "accepts": [],
            "conflicts": [],
            "tested_revision": f"revision:{repo}-1",
        },
        "deployment": {"loci": ["host:test"], "trust_zone": "test-zone"},
        "implications": {
            "authority": [_implication(implication_id)],
            "privacy_data": [],
            "hardware_network": [],
            "operational": [],
        },
        "upkeep": {
            "steward": "role:alpha-steward",
            "health_evidence": ["evidence:alpha-health"] if health_evidence is None else health_evidence,
            "monitoring": ["contract:alpha-monitor"],
            "upgrade_rollback": ["contract:alpha-rollback"],
            "credential_rotation": ["contract:alpha-credential"],
            "storage_retention": ["contract:alpha-retention"],
            "cost": [_cost()],
            "review_obligations": ["ORG-059:R2"],
        },
        "maturity": {"developmental_mode": "shaping", "replaceability": "required"},
        "tensions": [],
    }


def _upkeep_ack(implication_id: str) -> dict[str, object]:
    # ORG-055 §6 (T-3931): the resolver's step-5 gate now also requires an
    # acknowledgement for each upkeep cost/storage_retention/credential_rotation
    # item the _record fixture declares.  The derived ack-ids come from
    # ar._hashed_upkeep_ack_id / the "cost-<cost_id>" scheme so this fixture
    # tracks the resolver's derivation rather than hard-coding hashes.
    return {
        "implication_id": implication_id,
        "authority_ref": "authority:alpha-upkeep",
        "owner_ref": "role:alpha-steward",
        "evidence_ref": "evidence:alpha-upkeep",
    }


def _manifest(components: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "assembly_id": "alpha-assembly",
        "project_ref": None,
        "components": [] if components is None else components,
        "deployment": {"locus": "host:test", "trust_zone": "test-zone"},
        "implication_acknowledgements": [
            {
                "implication_id": "alpha-authority",
                "authority_ref": "authority:alpha",
                "owner_ref": "role:alpha-steward",
                "evidence_ref": "evidence:alpha-authority",
            },
            _upkeep_ack("cost-alpha-cost"),
            _upkeep_ack(ar._hashed_upkeep_ack_id("storage-retention", "contract:alpha-retention")),
            _upkeep_ack(ar._hashed_upkeep_ack_id("credential-rotation", "contract:alpha-credential")),
        ],
        "smoke_contracts": ["smoke:alpha"],
        "health_contracts": ["health:alpha"],
        "update_policy_ref": "policy:update",
        "rollback_policy_ref": "policy:rollback",
    }


def _manifest_component(
    component_id: str = "alpha-component",
    *,
    required_features: list[str] | None = None,
    enabled_optional_features: list[str] | None = None,
) -> dict[str, object]:
    return {
        "component_id": component_id,
        "contract_ranges": [],
        "version_ranges": [],
        "required_features": [] if required_features is None else required_features,
        "enabled_optional_features": [] if enabled_optional_features is None else enabled_optional_features,
    }


def _manifest_for_components(component_ids: list[str]) -> dict[str, object]:
    manifest = _manifest([_manifest_component(component_id) for component_id in component_ids])
    acknowledged = {
        item["implication_id"]  # type: ignore[index]
        for item in manifest["implication_acknowledgements"]  # type: ignore[index]
    }
    for component_id in component_ids:
        prefix = component_id.removesuffix("-component")
        implication_id = f"{prefix}-authority"
        if implication_id in acknowledged:
            continue
        manifest["implication_acknowledgements"].append(  # type: ignore[union-attr]
            {
                "implication_id": implication_id,
                "authority_ref": f"authority:{prefix}",
                "owner_ref": "role:alpha-steward",
                "evidence_ref": f"evidence:{implication_id}",
            }
        )
    return manifest


def _write_yaml(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _fixture_projection(
    *,
    records_data: list[dict[str, object]] | None = None,
    manifest_data: dict[str, object] | None = None,
) -> tuple[dict[str, cc.ComponentRecord], bytes, ar.AssemblyPlan, cn.NavigatorProjection]:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        records_root = root / "components" / "records"
        for record in records_data or [_record()]:
            _write_yaml(records_root / f"{record['component_id']}.yaml", record)
        records = cc.load_component_records(records_root)
        manifest_path = root / "assemblies" / "alpha-assembly.yaml"
        _write_yaml(manifest_path, manifest_data or _manifest([_manifest_component()]))
        manifest = ar.parse_assembly_manifest(manifest_path)
        plan = ar.resolve_assembly(manifest, records, FixtureRepoResolver(), FixtureEvidence())
        if plan.refusals:
            raise AssertionError([refusal.as_text() for refusal in plan.refusals])
        lock_bytes = ar.render_lock(plan)
        projection = cn.build_navigator_projection(records, lock_bytes)
        return records, lock_bytes, plan, projection


def _auth() -> cn.AuthenticatedContext:
    return cn.AuthenticatedContext(
        subject_ref="subject:alpha",
        uid=1000,
        persona_ref="persona:consultant",
        role_refs=("role:worker",),
        instance_ref="instance:test",
        trust_zone="test-zone",
        registry_revision="registry:1",
    )


def _budget(**overrides: int) -> dict[str, int]:
    data = {
        "max_candidates": 20,
        "max_summary_bytes": 256,
        "max_contract_bytes": 2048,
        "max_total_bytes": 8192,
        "max_provider_evidence_refs": 8,
    }
    data.update(overrides)
    return data


def _request(method: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "method": method,
        "request_id": "request-alpha",
        "context_budget": _budget(),
        "payload": payload,
    }


def _plan_payload(typed_input: object | None = None) -> dict[str, object]:
    return {
        "capability_id": "alpha-capability",
        "typed_input": {"value": "ok"} if typed_input is None else typed_input,
        "bounded_purpose": "bounded test purpose",
    }


def _valid_receipt() -> dict[str, object]:
    digest = "sha256:" + "a" * 64
    return {
        "schema_version": 1,
        "receipt_id": "receipt-alpha",
        "method": "invoke",
        "status": "succeeded",
        "subject_ref": "subject:alpha",
        "capability_id": "alpha-capability",
        "provider_component_id": "alpha-component",
        "contract": "alpha-contract",
        "schema": "alpha-schema",
        "version": "1.0.0",
        "projection_sha256": digest,
        "catalogue_sha256": digest,
        "lock_sha256": digest,
        "raw_lock_sha256": digest,
        "effect_digest": digest,
        "redacted_summary": "redacted",
        "provider_native_evidence_refs": ["evidence:native"],
        "authority_provenance_ref": "authority:native",
        "created_at": "2026-09-22T00:00:00Z",
        "updated_at": None,
        "prior_handle_ref": None,
        "prior_receipt_ref": None,
    }


def _valid_result(method: str = "invoke") -> dict[str, object]:
    digest = "sha256:" + "a" * 64
    identity = {
        "capability_id": "alpha-capability",
        "provider_component_id": "alpha-component",
        "contract": "alpha-contract",
        "schema": "alpha-schema",
        "version": "1.0.0",
    }
    payloads: dict[str, dict[str, object]] = {
        "search": {
            "candidates": [
                {
                    **identity,
                    "redacted_summary": "redacted",
                    "match_refs": ["match:alpha"],
                    "projection_ref": digest,
                }
            ],
            "truncated": False,
        },
        "describe": {
            **identity,
            "detail_level": "summary",
            "contract_slice": {
                "redacted_summary": "summary",
                "contract_digest": digest,
                "evidence_refs": ["evidence:native"],
            },
        },
        "plan": {
            **identity,
            "effect_digest": digest,
            "redacted_effect_summary": "summary",
            "handle_ref": None,
            "receipt_ref": None,
        },
        "invoke": {
            **identity,
            "result_kind": "receipt",
            "effect_digest": digest,
            "handle_ref": None,
            "receipt_ref": "receipt-alpha",
            "redacted_summary": "redacted",
        },
        "status": {
            **identity,
            "handle_ref": "handle-alpha",
            "lifecycle_status": "succeeded",
            "effect_digest": digest,
            "receipt_ref": "receipt-alpha",
            "updated_at": "2026-09-22T00:00:01Z",
            "redacted_summary": "redacted",
        },
        "cancel": {
            **identity,
            "handle_ref": "handle-alpha",
            "cancellation_status": "accepted",
            "effect_digest": digest,
            "receipt_ref": "receipt-alpha",
            "updated_at": "2026-09-22T00:00:01Z",
            "redacted_summary": "redacted",
        },
    }
    return {
        "schema_version": 1,
        "method": method,
        "request_id": "request-alpha",
        "status": "ok",
        "subject_ref": "subject:alpha",
        "projection_sha256": digest,
        "catalogue_sha256": digest,
        "lock_sha256": digest,
        "raw_lock_sha256": digest,
        "created_at": "2026-09-22T00:00:00Z",
        "provider_evidence_refs": ["evidence:native"],
        "payload": payloads[method],
    }


def _resign_projection(payload: dict[str, object]) -> dict[str, object]:
    payload["projection_sha256"] = None
    payload["projection_sha256"] = f"sha256:{cn._sha256_json(payload)}"
    return payload


class NavigatorProjectionTests(unittest.TestCase):
    def test_real_component_records_and_exact_assembly_lock_build_revision_bound_projection(self) -> None:
        records, lock_bytes, plan, projection = _fixture_projection()
        self.assertEqual(cn._canonical_lock_bytes(ar.parse_assembly_lock(lock_bytes)), ar.render_lock(plan))
        self.assertEqual(projection.assembly_id, "alpha-assembly")
        self.assertEqual(projection.raw_lock_sha256, f"sha256:{hashlib.sha256(lock_bytes).hexdigest()}")
        self.assertEqual(projection.catalogue_sha256, f"sha256:{plan.catalogue_sha256}")
        self.assertEqual(projection.resolution_order, ar.RESOLUTION_ORDER)
        self.assertEqual(projection.member_component_ids, ("alpha-component",))
        self.assertEqual(set(records) - set(projection.member_component_ids), set())

    def test_projection_preserves_contract_closure_implications_evidence_and_declared_classes_without_inference(self) -> None:
        records_data = [
            _record(
                provides=[
                    _provision(features=["feature-a", "feature-extra"], equivalence_class="equiv-alpha", trust_class="trust-local"),
                    _provision(
                        "beta-capability",
                        contract="alpha-contract",
                        schema="alpha-schema",
                        features=["feature-a"],
                        equivalence_class=None,
                        trust_class=None,
                    ),
                ],
                health_evidence=["evidence:record-health"],
            )
        ]
        _, _, _, projection = _fixture_projection(
            records_data=records_data,
            manifest_data=_manifest([_manifest_component(required_features=["feature-a"])]),
        )
        providers = {provider.capability_id: provider for provider in projection.capabilities}
        alpha = providers["alpha-capability"]
        beta = providers["beta-capability"]
        self.assertEqual(alpha.contract, "alpha-contract")
        self.assertEqual(alpha.schema, "alpha-schema")
        self.assertEqual(alpha.version, "1.0.0")
        self.assertEqual(alpha.enabled_features, ("feature-a",))
        self.assertNotIn("feature-extra", alpha.enabled_features)
        self.assertEqual(alpha.requirement_closure, ())
        self.assertEqual(alpha.equivalence_class, "equiv-alpha")
        self.assertEqual(alpha.trust_class, "trust-local")
        self.assertIsNone(beta.equivalence_class)
        self.assertIsNone(beta.trust_class)
        self.assertEqual(alpha.source_identity.tested_revision, alpha.source_identity.locked_revision)
        self.assertTrue(alpha.source_identity.artifact_digest.startswith("sha256:"))
        self.assertIn("tested-revision:", " ".join(alpha.compatibility_evidence))
        self.assertEqual(alpha.implications[0].implication_id, "alpha-authority")
        self.assertEqual(alpha.upkeep.health_evidence_refs, ("evidence:record-health",))
        self.assertEqual(alpha.upkeep.costs[0].owner, "role:alpha-steward")
        self.assertEqual(alpha.upkeep.costs[0].measurement_source, "evidence:alpha-cost")
        self.assertEqual(alpha.upkeep.review_obligations, ("ORG-059:R2",))
        self.assertEqual(projection.health_evidence, ("health:alpha@digest:health:alpha",))

    def test_projection_refuses_catalogue_lock_membership_revision_and_digest_mismatch(self) -> None:
        records, lock_bytes, _plan, _projection = _fixture_projection()
        tampered_catalogue = bytearray(lock_bytes)
        old = records["alpha-component"].source_sha256.encode("utf-8")
        if old in tampered_catalogue:
            tampered_catalogue = tampered_catalogue.replace(old, b"0" * len(old), 1)
        else:
            tampered_catalogue = tampered_catalogue.replace(b'"catalogue_sha256": "sha256:', b'"catalogue_sha256": "sha256:0', 1)
        with self.assertRaisesRegex(cn.NavigatorContractError, "catalogue-digest|noncanonical-lock"):
            cn.build_navigator_projection(records, bytes(tampered_catalogue))

        noncanonical = json.dumps(json.loads(lock_bytes), sort_keys=True, separators=(",", ":")).encode("utf-8")
        with self.assertRaisesRegex(cn.NavigatorContractError, "noncanonical-lock-bytes"):
            cn.build_navigator_projection(records, noncanonical)

        doc = json.loads(lock_bytes)
        doc["resolution_order"] = list(reversed(doc["resolution_order"]))
        bad_order = ar._json_bytes(doc)
        with self.assertRaisesRegex(cn.NavigatorContractError, "resolution-order-mismatch"):
            cn.build_navigator_projection(records, bad_order)

        doc = json.loads(lock_bytes)
        doc["components"].append(copy.deepcopy(doc["components"][0]))
        dup = ar._json_bytes(doc)
        with self.assertRaisesRegex(cn.NavigatorContractError, "duplicate-component"):
            cn.build_navigator_projection(records, dup)

        missing_records = dict(records)
        missing_records.pop("alpha-component")
        with self.assertRaisesRegex(cn.NavigatorContractError, "catalogue-digest-mismatch|membership-mismatch"):
            cn.build_navigator_projection(missing_records, lock_bytes)

        mutated_records = dict(records)
        original = mutated_records["alpha-component"]
        mutated_records["alpha-component"] = cc.ComponentRecord(
            **{**original.__dict__, "compatibility": cc.ComponentCompatibility(original.compatibility.accepts, original.compatibility.conflicts, "revision:other")}
        )
        with self.assertRaisesRegex(cn.NavigatorContractError, "source-identity-mismatch"):
            cn.build_navigator_projection(mutated_records, lock_bytes)

        doc = json.loads(lock_bytes)
        doc["components"][0]["logical_repo"] = "other-repo"
        source_mismatch = ar._json_bytes(doc)
        with self.assertRaisesRegex(cn.NavigatorContractError, "source-identity-mismatch"):
            cn.build_navigator_projection(records, source_mismatch)

        keyed_records = {"other-component": records["alpha-component"]}
        doc = json.loads(lock_bytes)
        doc["catalogue_sha256"] = f"sha256:{cn._catalogue_digest(keyed_records)}"
        with self.assertRaisesRegex(cn.NavigatorContractError, "source-identity-mismatch"):
            cn.build_navigator_projection(keyed_records, ar._json_bytes(doc))

        doc = json.loads(lock_bytes)
        doc["components"][0]["contract_versions"].append({"contract": "extra-contract", "version": "1.0.0"})
        with self.assertRaisesRegex(cn.NavigatorContractError, "contract/version"):
            cn.build_navigator_projection(records, ar._json_bytes(doc))

        doc = json.loads(lock_bytes)
        doc["components"][0]["schema_versions"].append({"schema": "extra-schema", "version": "1.0.0"})
        with self.assertRaisesRegex(cn.NavigatorContractError, "schema/version"):
            cn.build_navigator_projection(records, ar._json_bytes(doc))

        doc = json.loads(lock_bytes)
        doc["components"][0]["enabled_features"].append("missing-feature")
        with self.assertRaisesRegex(cn.NavigatorContractError, "feature"):
            cn.build_navigator_projection(records, ar._json_bytes(doc))

    def test_projection_refuses_missing_duplicate_ambiguous_and_inconsistent_provider_data(self) -> None:
        duplicate = _record(provides=[_provision(), _provision(equivalence_class="other-equiv")])
        with self.assertRaisesRegex(cn.NavigatorContractError, "duplicate|inconsistent"):
            _fixture_projection(records_data=[duplicate])

        optional_feature = _record(provides=[_provision(features=["feature-a"])])
        _records, _lock, _plan, optional_projection = _fixture_projection(records_data=[optional_feature])
        self.assertEqual(optional_projection.capabilities[0].enabled_features, ())

        empty_provider = _record(provides=[])
        with self.assertRaisesRegex(cn.NavigatorContractError, "missing-provider-data|provider"):
            _fixture_projection(records_data=[empty_provider])

        distinct = [
            _record("alpha-component"),
            _record(
                "beta-component",
                provides=[_provision(equivalence_class=None, trust_class=None)],
                implication_id="beta-authority",
            ),
            _record("unused-component"),
        ]
        manifest = _manifest([_manifest_component("alpha-component"), _manifest_component("beta-component")])
        manifest["implication_acknowledgements"].append(
            {
                "implication_id": "beta-authority",
                "authority_ref": "authority:beta",
                "owner_ref": "role:alpha-steward",
                "evidence_ref": "evidence:beta-authority",
            }
        )
        _records, _lock, _plan, projection = _fixture_projection(records_data=distinct, manifest_data=manifest)
        self.assertEqual(
            tuple(provider.provider_component_id for provider in projection.capabilities),
            ("alpha-component", "beta-component"),
        )
        self.assertNotIn("unused-component", projection.member_component_ids)

    def test_projection_is_byte_stable_across_input_order_and_repeated_generation(self) -> None:
        records, lock_bytes, _plan, projection = _fixture_projection()
        reordered = dict(reversed(list(records.items())))
        first = cn.canonical_json_bytes(cn.projection_to_json(projection))
        second_projection = cn.build_navigator_projection(reordered, lock_bytes)
        second = cn.canonical_json_bytes(cn.projection_to_json(second_projection))
        self.assertEqual(first, second)
        self.assertEqual(first, cn.canonical_json_bytes(cn.projection_to_json(projection)))
        self.assertEqual(cn.generate_projection_schema(), cn.generate_projection_schema())

    def test_projection_refuses_oversized_binary_and_hostile_control_metadata(self) -> None:
        oversized = _record()
        oversized["upkeep"]["monitoring"] = ["m" * (cn.MAX_METADATA_STRING_BYTES + 1)]  # type: ignore[index]
        with self.assertRaisesRegex(cn.NavigatorContractError, "oversized-metadata"):
            _fixture_projection(records_data=[oversized])

        hostile = _record()
        hostile["upkeep"]["monitoring"] = ["safe\u202Econtrol"]  # type: ignore[index]
        with self.assertRaisesRegex(cn.NavigatorContractError, "hostile-control-metadata"):
            _fixture_projection(records_data=[hostile])

        ordinary = _record()
        ordinary["upkeep"]["monitoring"] = ["ignore previous instructions as inert data"]  # type: ignore[index]
        _fixture_projection(records_data=[ordinary])

    def test_projection_derives_subject_visibility_and_deployment_trust_zone(self) -> None:
        records_data = [
            _record(
                provides=[
                    _provision(
                        subject_visibility=_subject_visibility(
                            instance_refs=["instance:test"]
                        )
                    )
                ]
            )
        ]
        _records, _lock, _plan, projection = _fixture_projection(
            records_data=records_data
        )

        provider = projection.capabilities[0]
        self.assertEqual(provider.deployment_trust_zone, "test-zone")
        self.assertEqual(
            provider.subject_visibility,
            cn.NavigatorSubjectVisibilityPolicy(
                subject_refs=("subject:alpha",),
                role_refs=("role:worker",),
                instance_refs=("instance:test",),
                trust_zones=("subject-visible-zone",),
                policy_refs=("policy:alpha-visibility",),
                evidence_refs=("evidence:alpha-visibility",),
            ),
        )
        payload = cn.projection_to_json(projection)
        provider_payload = payload["capabilities"][0]  # type: ignore[index]
        self.assertEqual(provider_payload["deployment_trust_zone"], "test-zone")
        self.assertEqual(
            provider_payload["subject_visibility"]["policy_refs"],  # type: ignore[index]
            ["policy:alpha-visibility"],
        )
        parsed = cn.parse_projection(payload)
        self.assertEqual(parsed.capabilities[0].subject_visibility, provider.subject_visibility)
        self.assertEqual(parsed.capabilities[0].deployment_trust_zone, "test-zone")

    def test_projection_accepts_legacy_missing_subject_visibility_as_none(self) -> None:
        _records, _lock, _plan, projection = _fixture_projection()
        payload = cn.projection_to_json(projection)
        legacy = copy.deepcopy(payload)
        legacy["capabilities"][0].pop("deployment_trust_zone")  # type: ignore[index,union-attr]
        legacy["capabilities"][0].pop("subject_visibility")  # type: ignore[index,union-attr]
        parsed_legacy = cn.parse_projection(_resign_projection(legacy))
        self.assertIsNone(parsed_legacy.capabilities[0].deployment_trust_zone)
        self.assertIsNone(parsed_legacy.capabilities[0].subject_visibility)

        nullable = copy.deepcopy(payload)
        nullable["capabilities"][0]["deployment_trust_zone"] = None  # type: ignore[index]
        nullable["capabilities"][0]["subject_visibility"] = None  # type: ignore[index]
        parsed_nullable = cn.parse_projection(_resign_projection(nullable))
        self.assertIsNone(parsed_nullable.capabilities[0].deployment_trust_zone)
        self.assertIsNone(parsed_nullable.capabilities[0].subject_visibility)

    def test_projection_does_not_treat_trust_class_as_trust_zone(self) -> None:
        record = _record(
            provides=[
                _provision(
                    trust_class="not-the-zone",
                    subject_visibility=_subject_visibility(
                        trust_zones=["subject-policy-zone"]
                    ),
                )
            ]
        )
        record["deployment"]["trust_zone"] = "actual-zone"  # type: ignore[index]
        _records, _lock, _plan, projection = _fixture_projection(records_data=[record])

        provider = projection.capabilities[0]
        self.assertEqual(provider.trust_class, "not-the-zone")
        self.assertEqual(provider.deployment_trust_zone, "actual-zone")
        self.assertNotEqual(provider.trust_class, provider.deployment_trust_zone)
        self.assertEqual(
            provider.subject_visibility.trust_zones,  # type: ignore[union-attr]
            ("subject-policy-zone",),
        )

    def test_projection_derives_navigator_provider_selection_and_health_join_refs_without_evaluating_health(
        self,
    ) -> None:
        primary = _provision()
        primary["navigator_provider_selection"] = _navigator_selection(
            provider_organ_id="organ:alpha-provider",
            health_dependency_id="dependency:alpha-provider-health",
        )
        fallback = _provision()
        fallback["navigator_provider_selection"] = _navigator_selection(
            "fallback",
            primary_provider_component_id="alpha-component",
            fallback_rank=1,
            provider_organ_id="organ:beta-provider",
            health_dependency_id="dependency:beta-provider-health",
        )
        _records, _lock, _plan, projection = _fixture_projection(
            records_data=[
                _record("alpha-component", provides=[primary]),
                _record(
                    "beta-component",
                    provides=[fallback],
                    implication_id="beta-authority",
                ),
            ],
            manifest_data=_manifest_for_components(["alpha-component", "beta-component"]),
        )

        providers = {
            provider.provider_component_id: provider for provider in projection.capabilities
        }
        self.assertEqual(
            providers["alpha-component"].navigator_provider_selection,
            cc.NavigatorProviderSelection(
                role=cc.NavigatorProviderRole.PRIMARY,
                primary_provider_component_id=None,
                fallback_rank=None,
                provider_organ_id="organ:alpha-provider",
                health_dependency_id="dependency:alpha-provider-health",
            ),
        )
        self.assertEqual(
            providers["beta-component"].navigator_provider_selection,
            cc.NavigatorProviderSelection(
                role=cc.NavigatorProviderRole.FALLBACK,
                primary_provider_component_id="alpha-component",
                fallback_rank=1,
                provider_organ_id="organ:beta-provider",
                health_dependency_id="dependency:beta-provider-health",
            ),
        )
        payload = cn.projection_to_json(projection)
        by_component = {
            provider["provider_component_id"]: provider  # type: ignore[index]
            for provider in payload["capabilities"]  # type: ignore[index]
        }
        beta_selection = by_component["beta-component"]["navigator_provider_selection"]  # type: ignore[index]
        self.assertEqual(beta_selection["provider_organ_id"], "organ:beta-provider")  # type: ignore[index]
        self.assertEqual(beta_selection["health_dependency_id"], "dependency:beta-provider-health")  # type: ignore[index]
        self.assertEqual(
            {
                (
                    provider.navigator_provider_selection.provider_organ_id,  # type: ignore[union-attr]
                    provider.navigator_provider_selection.health_dependency_id,  # type: ignore[union-attr]
                )
                for provider in projection.capabilities
                if provider.navigator_provider_selection is not None
            },
            {
                ("organ:alpha-provider", "dependency:alpha-provider-health"),
                ("organ:beta-provider", "dependency:beta-provider-health"),
            },
        )
        self.assertEqual(projection.health_evidence, ("health:alpha@digest:health:alpha",))

    def test_projection_refuses_duplicate_provider_health_join_pair_in_build_and_parse(
        self,
    ) -> None:
        primary = _provision()
        primary["navigator_provider_selection"] = _navigator_selection(
            provider_organ_id="organ:shared-provider",
            health_dependency_id="dependency:shared-provider-health",
        )
        fallback = _provision()
        fallback["navigator_provider_selection"] = _navigator_selection(
            "fallback",
            primary_provider_component_id="alpha-component",
            fallback_rank=1,
            provider_organ_id="organ:shared-provider",
            health_dependency_id="dependency:shared-provider-health",
        )
        with self.subTest(name="build"), self.assertRaisesRegex(
            cn.NavigatorContractError,
            "duplicate provider health join pair",
        ):
            _fixture_projection(
                records_data=[
                    _record("alpha-component", provides=[primary]),
                    _record(
                        "beta-component",
                        provides=[fallback],
                        implication_id="beta-authority",
                    ),
                ],
                manifest_data=_manifest_for_components(["alpha-component", "beta-component"]),
            )

        primary["navigator_provider_selection"] = _navigator_selection(
            provider_organ_id="organ:alpha-provider",
            health_dependency_id="dependency:alpha-provider-health",
        )
        fallback["navigator_provider_selection"] = _navigator_selection(
            "fallback",
            primary_provider_component_id="alpha-component",
            fallback_rank=1,
            provider_organ_id="organ:beta-provider",
            health_dependency_id="dependency:beta-provider-health",
        )
        _records, _lock, _plan, projection = _fixture_projection(
            records_data=[
                _record("alpha-component", provides=[primary]),
                _record(
                    "beta-component",
                    provides=[fallback],
                    implication_id="beta-authority",
                ),
            ],
            manifest_data=_manifest_for_components(["alpha-component", "beta-component"]),
        )
        payload = cn.projection_to_json(projection)
        providers = {
            provider["provider_component_id"]: provider  # type: ignore[index]
            for provider in payload["capabilities"]  # type: ignore[index]
        }
        providers["beta-component"]["navigator_provider_selection"][  # type: ignore[index]
            "provider_organ_id"
        ] = "organ:alpha-provider"
        providers["beta-component"]["navigator_provider_selection"][  # type: ignore[index]
            "health_dependency_id"
        ] = "dependency:alpha-provider-health"
        with self.subTest(name="parse"), self.assertRaisesRegex(
            cn.NavigatorContractError,
            "duplicate provider health join pair",
        ):
            cn.parse_projection(_resign_projection(payload))

    def test_projection_accepts_legacy_missing_or_null_selection_as_unavailable(self) -> None:
        _records, _lock, _plan, projection = _fixture_projection()
        payload = cn.projection_to_json(projection)
        missing = copy.deepcopy(payload)
        missing["capabilities"][0].pop("navigator_provider_selection")  # type: ignore[index,union-attr]
        parsed_missing = cn.parse_projection(_resign_projection(missing))
        self.assertIsNone(parsed_missing.capabilities[0].navigator_provider_selection)

        nullable = copy.deepcopy(payload)
        nullable["capabilities"][0]["navigator_provider_selection"] = None  # type: ignore[index]
        parsed_nullable = cn.parse_projection(_resign_projection(nullable))
        self.assertIsNone(parsed_nullable.capabilities[0].navigator_provider_selection)

    def test_projection_refuses_fallback_missing_primary_fallback_to_self_or_fallback_cycles(
        self,
    ) -> None:
        primary = _provision()
        primary["navigator_provider_selection"] = _navigator_selection()
        fallback = _provision()
        fallback["navigator_provider_selection"] = _navigator_selection(
            "fallback",
            primary_provider_component_id="alpha-component",
            fallback_rank=1,
            provider_organ_id="organ:beta-provider",
            health_dependency_id="dependency:beta-provider-health",
        )
        _records, _lock, _plan, projection = _fixture_projection(
            records_data=[
                _record("alpha-component", provides=[primary]),
                _record(
                    "beta-component",
                    provides=[fallback],
                    implication_id="beta-authority",
                ),
            ],
            manifest_data=_manifest_for_components(["alpha-component", "beta-component"]),
        )
        payload = cn.projection_to_json(projection)
        providers = {
            provider["provider_component_id"]: provider  # type: ignore[index]
            for provider in payload["capabilities"]  # type: ignore[index]
        }

        cases: list[tuple[str, dict[str, object]]] = []
        missing = copy.deepcopy(payload)
        missing_providers = {
            provider["provider_component_id"]: provider  # type: ignore[index]
            for provider in missing["capabilities"]  # type: ignore[index]
        }
        missing_providers["beta-component"]["navigator_provider_selection"][  # type: ignore[index]
            "primary_provider_component_id"
        ] = "missing-component"
        cases.append(("missing", missing))

        self_target = copy.deepcopy(payload)
        self_providers = {
            provider["provider_component_id"]: provider  # type: ignore[index]
            for provider in self_target["capabilities"]  # type: ignore[index]
        }
        self_providers["beta-component"]["navigator_provider_selection"][  # type: ignore[index]
            "primary_provider_component_id"
        ] = "beta-component"
        cases.append(("self", self_target))

        fallback_target = copy.deepcopy(payload)
        fallback_providers = {
            provider["provider_component_id"]: provider  # type: ignore[index]
            for provider in fallback_target["capabilities"]  # type: ignore[index]
        }
        fallback_providers["alpha-component"]["navigator_provider_selection"] = {  # type: ignore[index]
            "role": "fallback",
            "primary_provider_component_id": "beta-component",
            "fallback_rank": 2,
            "provider_organ_id": "organ:alpha-provider",
            "health_dependency_id": "dependency:alpha-provider-health",
        }
        cases.append(("fallback-target-cycle", fallback_target))

        for name, mutated in cases:
            with self.subTest(name=name), self.assertRaisesRegex(
                cn.NavigatorContractError,
                "fallback|primary|missing|self",
            ):
                cn.parse_projection(_resign_projection(mutated))

        self.assertEqual(
            providers["beta-component"]["navigator_provider_selection"]["fallback_rank"],  # type: ignore[index]
            1,
        )

    def test_projection_refuses_multiple_primaries_duplicate_ranks_and_class_or_contract_mismatch(
        self,
    ) -> None:
        primary = _provision()
        primary["navigator_provider_selection"] = _navigator_selection()
        second_primary = _provision()
        second_primary["navigator_provider_selection"] = _navigator_selection(
            provider_organ_id="organ:beta-provider",
            health_dependency_id="dependency:beta-provider-health",
        )
        with self.subTest(name="multiple-primaries"), self.assertRaisesRegex(
            cn.NavigatorContractError,
            "multiple primary",
        ):
            _fixture_projection(
                records_data=[
                    _record("alpha-component", provides=[primary]),
                    _record(
                        "beta-component",
                        provides=[second_primary],
                        implication_id="beta-authority",
                    ),
                ],
                manifest_data=_manifest_for_components(["alpha-component", "beta-component"]),
            )

        fallback_one = _provision()
        fallback_one["navigator_provider_selection"] = _navigator_selection(
            "fallback",
            primary_provider_component_id="alpha-component",
            fallback_rank=1,
            provider_organ_id="organ:beta-provider",
            health_dependency_id="dependency:beta-provider-health",
        )
        fallback_two = _provision()
        fallback_two["navigator_provider_selection"] = _navigator_selection(
            "fallback",
            primary_provider_component_id="alpha-component",
            fallback_rank=1,
            provider_organ_id="organ:gamma-provider",
            health_dependency_id="dependency:gamma-provider-health",
        )
        with self.subTest(name="duplicate-ranks"), self.assertRaisesRegex(
            cn.NavigatorContractError,
            "duplicate fallback rank",
        ):
            _fixture_projection(
                records_data=[
                    _record("alpha-component", provides=[primary]),
                    _record(
                        "beta-component",
                        provides=[fallback_one],
                        implication_id="beta-authority",
                    ),
                    _record(
                        "gamma-component",
                        provides=[fallback_two],
                        implication_id="gamma-authority",
                    ),
                ],
                manifest_data=_manifest_for_components(
                    ["alpha-component", "beta-component", "gamma-component"]
                ),
            )

        missing_class = _provision(equivalence_class=None)
        missing_class["navigator_provider_selection"] = _navigator_selection()
        with self.subTest(name="missing-class"), self.assertRaisesRegex(
            cn.NavigatorContractError,
            "equivalence_class|trust_class",
        ):
            _fixture_projection(records_data=[_record(provides=[missing_class])])

        mismatched_primary = _provision(contract="other-contract")
        mismatched_primary["navigator_provider_selection"] = _navigator_selection()
        fallback_contract = _provision()
        fallback_contract["navigator_provider_selection"] = _navigator_selection(
            "fallback",
            primary_provider_component_id="alpha-component",
            fallback_rank=1,
            provider_organ_id="organ:beta-provider",
            health_dependency_id="dependency:beta-provider-health",
        )
        with self.subTest(name="contract-mismatch"), self.assertRaisesRegex(
            cn.NavigatorContractError,
            "same group|selected primary|missing",
        ):
            _fixture_projection(
                records_data=[
                    _record("alpha-component", provides=[mismatched_primary]),
                    _record(
                        "beta-component",
                        provides=[fallback_contract],
                        implication_id="beta-authority",
                    ),
                ],
                manifest_data=_manifest_for_components(["alpha-component", "beta-component"]),
            )

    def test_projection_is_byte_stable_by_declared_fallback_rank_not_input_or_component_order(
        self,
    ) -> None:
        primary = _provision()
        primary["navigator_provider_selection"] = _navigator_selection()
        fallback = _provision()
        fallback["navigator_provider_selection"] = _navigator_selection(
            "fallback",
            primary_provider_component_id="alpha-component",
            fallback_rank=7,
            provider_organ_id="organ:beta-provider",
            health_dependency_id="dependency:beta-provider-health",
        )
        records_data = [
            _record("alpha-component", provides=[primary]),
            _record(
                "beta-component",
                provides=[fallback],
                implication_id="beta-authority",
            ),
        ]
        records, lock_bytes, _plan, projection = _fixture_projection(
            records_data=records_data,
            manifest_data=_manifest_for_components(["beta-component", "alpha-component"]),
        )
        reversed_records = dict(reversed(list(records.items())))
        first = cn.canonical_json_bytes(cn.projection_to_json(projection))
        second_projection = cn.build_navigator_projection(reversed_records, lock_bytes)
        second = cn.canonical_json_bytes(cn.projection_to_json(second_projection))

        self.assertEqual(first, second)
        self.assertEqual(
            tuple(provider.provider_component_id for provider in projection.capabilities),
            ("alpha-component", "beta-component"),
        )
        self.assertEqual(
            projection.capabilities[1].navigator_provider_selection.fallback_rank,  # type: ignore[union-attr]
            7,
        )


class NavigatorSchemaTests(unittest.TestCase):
    def test_generated_projection_request_result_and_receipt_schemas_are_closed_and_match_checked_bytes(self) -> None:
        for filename, generator in SCHEMA_FILES.items():
            self.assertEqual((REPO_ROOT / "schemas" / filename).read_bytes(), generator(), filename)
            schema = json.loads(generator())
            if "oneOf" in schema:
                self.assertNotEqual(schema.get("additionalProperties"), False)
                self.assertNotIn("properties", schema)
                for branch in schema["oneOf"]:
                    self.assertFalse(branch["additionalProperties"])
                    self.assertFalse(branch["properties"]["payload"]["additionalProperties"])
            else:
                self.assertFalse(schema["additionalProperties"])

        records, lock_bytes, _plan, projection = _fixture_projection()
        payload = cn.projection_to_json(projection)
        parsed = cn.parse_projection(payload)
        self.assertEqual(parsed.capabilities[0].implications, projection.capabilities[0].implications)
        self.assertEqual(parsed.capabilities[0].upkeep.costs, projection.capabilities[0].upkeep.costs)
        tampered = copy.deepcopy(payload)
        tampered["assembly_id"] = "other-assembly"
        with self.assertRaisesRegex(cn.NavigatorContractError, "projection-digest-mismatch"):
            cn.parse_projection(tampered)
        for mutate in (
            lambda item: item.update({"extra": True}),
            lambda item: item["capabilities"][0].update({"extra": True}),  # type: ignore[index,union-attr]
            lambda item: item["capabilities"][0]["source_identity"].update({"extra": True}),  # type: ignore[index,union-attr]
            lambda item: item["capabilities"][0]["implications"][0].update({"extra": True}),  # type: ignore[index,union-attr]
            lambda item: item["capabilities"][0]["upkeep"].update({"extra": True}),  # type: ignore[index,union-attr]
            lambda item: item["capabilities"][0]["upkeep"]["costs"][0].update({"extra": True}),  # type: ignore[index,union-attr]
        ):
            bad = copy.deepcopy(payload)
            mutate(bad)
            with self.assertRaisesRegex(cn.NavigatorContractError, "unexpected field"):
                cn.parse_projection(_resign_projection(bad))
        bad_sha = copy.deepcopy(payload)
        bad_sha["raw_lock_sha256"] = "sha256:not-a-real-digest"
        with self.assertRaisesRegex(cn.NavigatorContractError, "projection-digest-mismatch|expected sha256"):
            cn.parse_projection(_resign_projection(bad_sha))
        bad_stable = copy.deepcopy(payload)
        bad_stable["capabilities"][0]["capability_id"] = "Bad_Capability"  # type: ignore[index]
        with self.assertRaisesRegex(cn.NavigatorContractError, "expected stable id"):
            cn.parse_projection(_resign_projection(bad_stable))
        self.assertIn(b"provider_component_id", cn.generate_result_schema())
        self.assertIn(b"provider_component_id", cn.generate_receipt_schema())
        request_schema = json.loads(cn.generate_request_schema())
        for field_schema in request_schema["$defs"]["budget"]["properties"].values():
            self.assertIn("minimum", field_schema)
            self.assertNotIn("maximum", field_schema)
        result_schema = json.loads(cn.generate_result_schema())
        self.assertEqual(result_schema["$defs"]["summary"]["maxLength"], 256)
        self.assertIn("UTF-8-byte", result_schema["$defs"]["summary"]["description"])
        for branch in result_schema["oneOf"]:
            props = branch["properties"]
            self.assertIn("created_at", props)
            self.assertEqual(props["provider_evidence_refs"]["maxItems"], 8)
            self.assertNotIn("minItems", props["provider_evidence_refs"])
            method = props["method"]["const"]
            payload_props = props["payload"]["properties"]
            if method == "search":
                self.assertEqual(payload_props["candidates"]["maxItems"], 20)
            else:
                for field in ("capability_id", "provider_component_id", "contract", "schema", "version"):
                    self.assertIn(field, payload_props)
        receipt_schema = json.loads(cn.generate_receipt_schema())
        native_refs = receipt_schema["properties"]["provider_native_evidence_refs"]
        self.assertEqual(native_refs["minItems"], 1)
        self.assertEqual(native_refs["maxItems"], 8)

    def test_public_request_schema_exposes_exact_six_methods_without_identity_or_authority_fields(self) -> None:
        schema = json.loads(cn.generate_request_schema())
        methods = tuple(branch["properties"]["method"]["const"] for branch in schema["oneOf"])
        self.assertEqual(methods, ("search", "describe", "plan", "invoke", "status", "cancel"))
        self.assertEqual(cn.NAVIGATOR_METHODS, ("search", "describe", "plan", "invoke", "status", "cancel"))
        raw = cn.generate_request_schema().decode("utf-8")
        for forbidden in cn.IDENTITY_LOOKALIKE_KEYS:
            self.assertNotIn(f'"{forbidden}"', raw)
        describe = next(branch for branch in schema["oneOf"] if branch["properties"]["method"]["const"] == "describe")
        plan = next(branch for branch in schema["oneOf"] if branch["properties"]["method"]["const"] == "plan")
        self.assertEqual(
            set(describe["properties"]["payload"]["properties"]),
            {"capability_id", "requested_detail"},
        )
        self.assertEqual(
            set(plan["properties"]["payload"]["properties"]),
            {"capability_id", "typed_input", "bounded_purpose"},
        )

    def test_request_validator_rejects_identity_lookalikes_but_cannot_override_authenticated_context(self) -> None:
        clean = cn.parse_request(_request("plan", _plan_payload({"uid": 123, "value": "opaque"})), auth=_auth())
        self.assertEqual(clean.payload["typed_input"], {"uid": 123, "value": "opaque"})
        with self.assertRaisesRegex(cn.NavigatorContractError, "identity-lookalike"):
            cn.parse_request({**_request("describe", {"capability_id": "alpha-capability", "requested_detail": "summary"}), "uid": 0}, auth=_auth())
        with self.assertRaisesRegex(cn.NavigatorContractError, "identity-lookalike"):
            cn.parse_request(_request("search", {"query": {"text": "x"}, "constraints": {"capability_ids": [], "provider_component_ids": [], "contracts": [], "schemas": [], "versions": [], "features": [], "equivalence_classes": [], "trust_classes": [], "implication_scopes": [], "require_declared_equivalence": False, "role": "admin"}}), auth=_auth())
        with self.assertRaisesRegex(cn.NavigatorContractError, "unexpected field"):
            cn.parse_request(_request("plan", {**_plan_payload(), "provider_component_id": "alpha-component"}), auth=_auth())
        with self.assertRaisesRegex(cn.NavigatorContractError, "expected stable id"):
            cn.parse_request(_request("describe", {"capability_id": "Alpha_Capability", "requested_detail": "summary"}), auth=_auth())
        bad_version = _request("search", {"query": {"text": "x"}, "constraints": {"capability_ids": [], "provider_component_ids": [], "contracts": [], "schemas": [], "versions": ["one"], "features": [], "equivalence_classes": [], "trust_classes": [], "implication_scopes": [], "require_declared_equivalence": False}})
        with self.assertRaisesRegex(cn.NavigatorContractError, "expected semver"):
            cn.parse_request(bad_version, auth=_auth())
        with self.assertRaisesRegex(cn.NavigatorContractError, "non-finite"):
            cn.parse_request(_request("plan", _plan_payload({"value": float("nan")})), auth=_auth())
        bad_search = _request("search", {"query": {"text": "x"}, "constraints": {"capability_ids": "alpha-capability", "provider_component_ids": [], "contracts": [], "schemas": [], "versions": [], "features": [], "equivalence_classes": [], "trust_classes": [], "implication_scopes": [], "require_declared_equivalence": False}})
        with self.assertRaisesRegex(cn.NavigatorContractError, "expected array"):
            cn.parse_request(bad_search, auth=_auth())
        bad_bool = _request("search", {"query": {"text": "x"}, "constraints": {"capability_ids": [], "provider_component_ids": [], "contracts": [], "schemas": [], "versions": [], "features": [], "equivalence_classes": [], "trust_classes": [], "implication_scopes": [], "require_declared_equivalence": "false"}})
        with self.assertRaisesRegex(cn.NavigatorContractError, "expected boolean"):
            cn.parse_request(bad_bool, auth=_auth())
        digest = "sha256:" + "a" * 64
        cn.parse_request(_request("invoke", {"plan_or_handle": {"kind": "effect-plan", "handle_ref": None, "plan_digest": digest}}), auth=_auth())
        with self.assertRaisesRegex(cn.NavigatorContractError, "unsupported handle kind"):
            cn.parse_request(_request("invoke", {"plan_or_handle": {"kind": "delegate", "handle_ref": None, "plan_digest": digest}}), auth=_auth())
        with self.assertRaisesRegex(cn.NavigatorContractError, "expected sha256"):
            cn.parse_request(_request("status", {"handle": {"handle_ref": "handle-alpha", "handle_digest": "not-a-digest"}}), auth=_auth())
        with self.assertRaisesRegex(cn.NavigatorContractError, "identity-lookalike"):
            cn.parse_request(_request("cancel", {"handle": {"handle_ref": "handle-alpha", "handle_digest": digest, "uid": 0}, "bounded_reason": None}), auth=_auth())

    def test_server_budget_intersection_never_exceeds_measured_ceilings_and_refuses_below_minimum(self) -> None:
        clamped = cn.intersect_budget(
            {
                "max_candidates": 999,
                "max_summary_bytes": 999,
                "max_contract_bytes": 9999,
                "max_total_bytes": 99999,
                "max_provider_evidence_refs": 999,
            }
        )
        self.assertEqual(clamped, cn.SERVER_CEILINGS)
        for field in cn._BUDGET_FIELDS:
            bad = _budget()
            bad[field] = getattr(cn.MINIMUM_BUDGET, field) - 1
            with self.assertRaisesRegex(cn.NavigatorContractError, "budget-below-minimum"):
                cn.intersect_budget(bad)
        schema = json.loads(cn.generate_request_schema())
        for field_schema in schema["$defs"]["budget"]["properties"].values():
            self.assertNotIn("maximum", field_schema)

    def test_result_and_receipt_validators_refuse_secret_binary_raw_log_and_unbounded_content(self) -> None:
        for method in cn.NAVIGATOR_METHODS:
            cn.parse_result(_valid_result(method))
        cn.parse_receipt(_valid_receipt())
        empty_generic = _valid_result("search")
        empty_generic["provider_evidence_refs"] = []
        cn.parse_result(empty_generic)
        for key in ("secret", "raw_log", "binary"):
            bad_result = _valid_result()
            bad_result["payload"] = {**bad_result["payload"], key: "leak"}  # type: ignore[index]
            with self.assertRaisesRegex(cn.NavigatorContractError, "forbidden"):
                cn.parse_result(bad_result)
            bad_receipt = _valid_receipt()
            bad_receipt[key] = "leak"
            with self.assertRaisesRegex(cn.NavigatorContractError, "forbidden"):
                cn.parse_receipt(bad_receipt)
        bad_binary = _valid_result()
        bad_binary["payload"] = {**bad_binary["payload"], "redacted_summary": b"bytes"}  # type: ignore[index]
        with self.assertRaisesRegex(cn.NavigatorContractError, "forbidden"):
            cn.parse_result(bad_binary)
        bad_control = _valid_receipt()
        bad_control["redacted_summary"] = "bad\u202Esummary"
        with self.assertRaisesRegex(cn.NavigatorContractError, "hostile-control"):
            cn.parse_receipt(bad_control)
        bad_refs = _valid_receipt()
        bad_refs["provider_native_evidence_refs"] = []
        with self.assertRaisesRegex(cn.NavigatorContractError, "evidence refs|required|unique"):
            cn.parse_receipt(bad_refs)
        too_many_refs = _valid_result()
        too_many_refs["provider_evidence_refs"] = [f"evidence:{idx}" for idx in range(cn.SERVER_CEILINGS.max_provider_evidence_refs + 1)]
        with self.assertRaisesRegex(cn.NavigatorContractError, "too many evidence"):
            cn.parse_result(too_many_refs)
        too_many_receipt_refs = _valid_receipt()
        too_many_receipt_refs["provider_native_evidence_refs"] = [f"evidence:{idx}" for idx in range(cn.SERVER_CEILINGS.max_provider_evidence_refs + 1)]
        with self.assertRaisesRegex(cn.NavigatorContractError, "too many evidence"):
            cn.parse_receipt(too_many_receipt_refs)
        bad_status = _valid_result()
        bad_status["status"] = "succeeded"
        with self.assertRaisesRegex(cn.NavigatorContractError, "unsupported status"):
            cn.parse_result(bad_status)
        bad_version = _valid_receipt()
        bad_version["schema_version"] = 2
        with self.assertRaisesRegex(cn.NavigatorContractError, "unsupported schema version"):
            cn.parse_receipt(bad_version)
        bad_digest = _valid_receipt()
        bad_digest["projection_sha256"] = "sha256:bad"
        with self.assertRaisesRegex(cn.NavigatorContractError, "expected sha256"):
            cn.parse_receipt(bad_digest)
        bad_nested = _valid_result("describe")
        bad_nested["payload"] = {
            "capability_id": "alpha-capability",
            "provider_component_id": "alpha-component",
            "contract": "alpha-contract",
            "schema": "alpha-schema",
            "version": "1.0.0",
            "detail_level": "summary",
            "contract_slice": {
                "redacted_summary": "summary",
                "contract_digest": "sha256:" + "a" * 64,
                "evidence_refs": ["evidence:native"],
                "extra": True,
            },
        }
        with self.assertRaisesRegex(cn.NavigatorContractError, "unexpected field"):
            cn.parse_result(bad_nested)
        bad_branch_digest = _valid_result("plan")
        bad_branch_digest["payload"] = {
            "capability_id": "alpha-capability",
            "provider_component_id": "alpha-component",
            "contract": "alpha-contract",
            "schema": "alpha-schema",
            "version": "1.0.0",
            "effect_digest": "not-a-digest",
            "redacted_effect_summary": "summary",
            "handle_ref": None,
            "receipt_ref": None,
        }
        with self.assertRaisesRegex(cn.NavigatorContractError, "expected sha256"):
            cn.parse_result(bad_branch_digest)
        bad_stable = _valid_result("status")
        bad_stable["payload"] = {**bad_stable["payload"], "provider_component_id": "Alpha_Component"}  # type: ignore[index]
        with self.assertRaisesRegex(cn.NavigatorContractError, "expected stable id"):
            cn.parse_result(bad_stable)
        bad_semver = _valid_result("cancel")
        bad_semver["payload"] = {**bad_semver["payload"], "version": "v1"}  # type: ignore[index]
        with self.assertRaisesRegex(cn.NavigatorContractError, "expected semver"):
            cn.parse_result(bad_semver)
        exact_summary = _valid_result()
        exact_summary["payload"] = {**exact_summary["payload"], "redacted_summary": "x" * cn.MAX_RESULT_SUMMARY_BYTES}  # type: ignore[index]
        cn.parse_result(exact_summary)
        long_summary = _valid_result()
        long_summary["payload"] = {**long_summary["payload"], "redacted_summary": "x" * (cn.MAX_RESULT_SUMMARY_BYTES + 1)}  # type: ignore[index]
        with self.assertRaisesRegex(cn.NavigatorContractError, "summary exceeds"):
            cn.parse_result(long_summary)
        multibyte_summary = _valid_result()
        multibyte_summary["payload"] = {**multibyte_summary["payload"], "redacted_summary": "é" * 129}  # type: ignore[index]
        with self.assertRaisesRegex(cn.NavigatorContractError, "summary exceeds"):
            cn.parse_result(multibyte_summary)
        too_many_candidates = _valid_result("search")
        candidate = too_many_candidates["payload"]["candidates"][0]  # type: ignore[index]
        too_many_candidates["payload"] = {  # type: ignore[index]
            "candidates": [copy.deepcopy(candidate) for _ in range(cn.SERVER_CEILINGS.max_candidates + 1)],
            "truncated": True,
        }
        with self.assertRaisesRegex(cn.NavigatorContractError, "too many candidates"):
            cn.parse_result(too_many_candidates)
        bad_result_nan = _valid_result()
        bad_result_nan["payload"] = {**bad_result_nan["payload"], "redacted_summary": float("nan")}  # type: ignore[index]
        with self.assertRaisesRegex(cn.NavigatorContractError, "non-finite|expected nonempty string"):
            cn.parse_result(bad_result_nan)
        with self.assertRaisesRegex(cn.NavigatorContractError, "non-finite"):
            cn.canonical_json_bytes({"value": float("nan")})
        aggregate = _valid_result("search")
        aggregate_candidate = copy.deepcopy(aggregate["payload"]["candidates"][0])  # type: ignore[index]
        aggregate_candidate["redacted_summary"] = "x" * cn.MAX_RESULT_SUMMARY_BYTES  # type: ignore[index]
        aggregate["payload"] = {
            "candidates": [copy.deepcopy(aggregate_candidate) for _ in range(cn.SERVER_CEILINGS.max_candidates)],
            "truncated": False,
        }
        self.assertGreater(len(cn.canonical_json_bytes(aggregate)), cn.SERVER_CEILINGS.max_total_bytes)
        with self.assertRaisesRegex(cn.NavigatorContractError, "result exceeds"):
            cn.parse_result(aggregate)
        huge_receipt = _valid_receipt()
        huge_receipt["redacted_summary"] = "x" * cn.SERVER_CEILINGS.max_total_bytes
        with self.assertRaisesRegex(cn.NavigatorContractError, "exceeds"):
            cn.parse_receipt(huge_receipt)

    def test_ci_paths_cover_navigator_contract_and_generated_schemas(self) -> None:
        workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        for trigger in ("pull_request",):  # T-5001: no push trigger; the nightly covers main
            paths = workflow[True][trigger]["paths"]
            self.assertIn("tools/**", paths)
            for filename in SCHEMA_FILES:
                self.assertIn(f"schemas/{filename}", paths)

    def test_generated_projection_schema_matches_bytes_and_public_request_result_receipt_bytes_unchanged(
        self,
    ) -> None:
        self.assertEqual(
            (REPO_ROOT / "schemas" / "component-record.schema.json").read_bytes(),
            cc.generate_component_record_schema(),
        )
        self.assertEqual(
            (
                REPO_ROOT / "schemas" / "capability-navigator-projection.schema.json"
            ).read_bytes(),
            cn.generate_projection_schema(),
        )
        protected = {
            "capability-navigator-request.schema.json": "4b1442cb4217dce5b5451986b0f3e9ff6fb6b6f8cc95c65cd8bf23e814c3cdc5",
            "capability-navigator-result.schema.json": "d099d5135126f8fc96c393466a7b2c1fb55e409596e325a1d32c5c0411a9b1a1",
            "capability-navigator-receipt.schema.json": "fe48c0f95465970b2bd464313237a2259baf16eea3c7451f77adedae03556858",
        }
        for filename, expected in protected.items():
            actual = (REPO_ROOT / "schemas" / filename).read_bytes()
            self.assertEqual(hashlib.sha256(actual).hexdigest(), expected)
            self.assertEqual(actual, SCHEMA_FILES[filename]())
            self.assertNotIn(b"navigator_provider_selection", actual)
            self.assertNotIn(b"fallback_rank", actual)
            self.assertNotIn(b"primary_provider_component_id", actual)

        self.assertNotIn("effect_contract", cc.PROVISION_KEYS)
        component_schema = json.loads(cc.generate_component_record_schema())
        component_provider = component_schema["properties"]["provides"]["items"]
        self.assertNotIn("effect_contract", component_provider["properties"])
        projection_schema = json.loads(cn.generate_projection_schema())
        projection_provider = projection_schema["properties"]["capabilities"]["items"]
        self.assertNotIn("effect_contract", projection_provider["properties"])
        subject_visibility = projection_provider["properties"]["subject_visibility"]["anyOf"][1]
        for key in cc.SUBJECT_VISIBILITY_KEYS:
            self.assertIs(subject_visibility["properties"][key]["uniqueItems"], True)

    def test_projection_schema_selection_object_is_closed_optional_nullable_and_v1(
        self,
    ) -> None:
        schema = json.loads(cn.generate_projection_schema())
        projection_provider = schema["properties"]["capabilities"]["items"]
        selection = projection_provider["properties"]["navigator_provider_selection"]
        selection_object = selection["anyOf"][1]

        self.assertNotIn("navigator_provider_selection", projection_provider["required"])
        self.assertEqual(selection["anyOf"][0], {"type": "null"})
        self.assertIs(selection_object["additionalProperties"], False)
        self.assertEqual(
            tuple(selection_object["required"]),
            cc.NAVIGATOR_PROVIDER_SELECTION_KEYS,
        )
        self.assertEqual(
            selection_object["properties"]["role"]["enum"],
            ["primary", "fallback"],
        )
        self.assertEqual(
            selection_object["properties"]["provider_organ_id"]["pattern"],
            cc.ORGAN_REF_PATTERN,
        )
        self.assertEqual(
            selection_object["properties"]["health_dependency_id"]["pattern"],
            cc.DEPENDENCY_REF_PATTERN,
        )
        primary_conditional = selection_object["allOf"][0]
        fallback_conditional = selection_object["allOf"][1]
        self.assertEqual(
            primary_conditional["then"]["properties"]["primary_provider_component_id"],
            {"type": "null"},
        )
        self.assertEqual(
            primary_conditional["then"]["properties"]["fallback_rank"],
            {"type": "null"},
        )
        self.assertEqual(
            fallback_conditional["then"]["properties"]["primary_provider_component_id"],
            {"$ref": "#/$defs/stable"},
        )
        self.assertEqual(
            fallback_conditional["then"]["properties"]["fallback_rank"],
            {"type": "integer", "minimum": 1},
        )
        self.assertEqual(schema["properties"]["schema_version"]["const"], cn.SCHEMA_VERSION)
        self.assertEqual(cn.SCHEMA_VERSION, 1)


class AuthenticatedSubjectSeamTests(unittest.TestCase):
    def test_subject_resolver_is_server_owned_protocol_not_wire_input(self) -> None:
        self.assertTrue(hasattr(cn.SubjectResolver, "resolve"))
        auth = _auth()
        req = cn.parse_request(_request("describe", {"capability_id": "alpha-capability", "requested_detail": "summary"}), auth=auth)
        self.assertNotIn("subject_ref", cn.request_to_json(req))
        self.assertEqual(auth.subject_ref, "subject:alpha")


if __name__ == "__main__":
    unittest.main()
