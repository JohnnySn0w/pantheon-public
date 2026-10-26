"""Tests for the ORG-055 assembly manifest/lock generator + resolver (T-3182).

The logical-repo, lifecycle, and evidence seams are exercised through the
in-memory fixture adapters below (the T-3182 acceptance driver).  Real
implementations are T-1996 (ORG-046 resolver), T-2951 (checkout lifecycle), and
downstream evidence; the real three-temp-repo A/B/C cutover is T-3183.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import yaml

from tools import assembly_resolver as ar
from tools import component_catalogue as cc
from tools.assembly_resolver import (
    CompatibilityRefusal,
    EvidencePresence,
    LifecyclePlan,
    RefusalKind,
    ResolvedSource,
    SubmoduleView,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# In-memory fixture adapters (Protocol impls used only for T-3182 acceptance)
# ---------------------------------------------------------------------------


class FixtureRepoResolver:
    def __init__(
        self,
        sources: dict[str, ResolvedSource] | None = None,
        *,
        missing: frozenset[str] = frozenset(),
    ) -> None:
        self._sources = dict(sources or {})
        self._missing = frozenset(missing)

    def resolve(self, logical_repo: str) -> ResolvedSource:
        if logical_repo in self._missing:
            raise KeyError(logical_repo)
        if logical_repo in self._sources:
            return self._sources[logical_repo]
        return ResolvedSource(
            logical_repo=logical_repo,
            resolved_source=f"mirror://{logical_repo}",
            revision=f"revision:{logical_repo}-1",
            artifact_digest=f"sha256:{logical_repo}-artifact",
        )


class FixtureEvidence:
    def __init__(self, *, absent: frozenset[str] = frozenset()) -> None:
        self._absent = frozenset(absent)

    def resolve(self, evidence_ref: str) -> EvidencePresence:
        if evidence_ref in self._absent:
            return EvidencePresence(evidence_ref, present=False, digest="")
        return EvidencePresence(evidence_ref, present=True, digest=f"digest:{evidence_ref}")


class FixtureLifecycle:
    def __init__(self, refusal: CompatibilityRefusal | None = None) -> None:
        self._refusal = refusal

    def plan(self, sources) -> LifecyclePlan:  # noqa: ANN001
        if self._refusal is not None:
            return LifecyclePlan(ok=False, stages=(), refusal=self._refusal)
        return LifecyclePlan(ok=True, stages=ar.UPDATE_STAGES, refusal=None)


# ---------------------------------------------------------------------------
# Catalogue record + manifest builders
# ---------------------------------------------------------------------------


def _provision(
    capability: str = "alpha-capability",
    *,
    contract: str = "alpha-contract",
    schema: str = "alpha-schema",
    version: str = "1.0.0",
    features: list[str] | None = None,
) -> dict[str, object]:
    return {
        "capability": capability,
        "contract": contract,
        "schema": schema,
        "version": version,
        "features": [] if features is None else features,
    }


def _requirement(
    capability: str = "alpha-capability",
    *,
    contract: str = "alpha-contract",
    schema: str = "alpha-schema",
    version: str = "1.0.0",
    feature: str | None = None,
    enabled: bool = True,
    degradation_ref: str | None = None,
) -> dict[str, object]:
    return {
        "capability": capability,
        "contract": contract,
        "schema": schema,
        "version": version,
        "feature": feature,
        "enabled": enabled,
        "degradation_ref": degradation_ref,
    }


def _record(
    component_id: str = "alpha-component",
    *,
    project_id: str = "alpha-project",
    logical_repo: str | None = None,
    tested_revision: str | None = None,
    provides: list[dict[str, object]] | None = None,
    hard: list[dict[str, object]] | None = None,
    optional: list[dict[str, object]] | None = None,
    conflicts: list[dict[str, object]] | None = None,
    implications: dict[str, list[dict[str, object]]] | None = None,
    review_obligations: list[str] | None = None,
    credential_rotation: list[str] | None = None,
    storage_retention: list[str] | None = None,
    cost: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    repo = logical_repo or f"{component_id}-repo"
    return {
        "schema_version": 1,
        "component_id": component_id,
        "project_id": project_id,
        "kind": "service",
        "source": {"logical_repo": repo, "path": None, "artifact": None},
        "provides": [_provision()] if provides is None else provides,
        "requires": {
            "hard": [] if hard is None else hard,
            "optional": [] if optional is None else optional,
        },
        "compatibility": {
            "accepts": [],
            "conflicts": [] if conflicts is None else conflicts,
            "tested_revision": tested_revision or f"revision:{repo}-1",
        },
        "deployment": {"loci": ["host:test"], "trust_zone": "test-zone"},
        "implications": implications
        or {"authority": [], "privacy_data": [], "hardware_network": [], "operational": []},
        "upkeep": {
            "steward": "role:alpha-steward",
            "health_evidence": ["evidence:alpha-health"],
            "monitoring": ["contract:alpha-monitor"],
            "upgrade_rollback": ["contract:alpha-rollback"],
            "credential_rotation": [] if credential_rotation is None else credential_rotation,
            "storage_retention": [] if storage_retention is None else storage_retention,
            "cost": [] if cost is None else cost,
            "review_obligations": [] if review_obligations is None else review_obligations,
        },
        "maturity": {"developmental_mode": "shaping", "replaceability": "required"},
        "tensions": [],
    }


def _manifest(
    *,
    assembly_id: str = "alpha-assembly",
    components: list[dict[str, object]] | None = None,
    acks: list[dict[str, object]] | None = None,
    smoke: list[str] | None = None,
    health: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "assembly_id": assembly_id,
        "project_ref": None,
        "components": [] if components is None else components,
        "deployment": {"locus": "host:test", "trust_zone": "test-zone"},
        "implication_acknowledgements": [] if acks is None else acks,
        "smoke_contracts": [] if smoke is None else smoke,
        "health_contracts": [] if health is None else health,
        "update_policy_ref": "policy:update",
        "rollback_policy_ref": "policy:rollback",
    }


def _manifest_component(
    component_id: str = "alpha-component",
    *,
    contract_ranges: list[str] | None = None,
    version_ranges: list[str] | None = None,
    required_features: list[str] | None = None,
    enabled_optional_features: list[str] | None = None,
) -> dict[str, object]:
    return {
        "component_id": component_id,
        "contract_ranges": contract_ranges or [],
        "version_ranges": version_ranges or [],
        "required_features": required_features or [],
        "enabled_optional_features": enabled_optional_features or [],
    }


def _cost(cost_id: str = "alpha-cost") -> dict[str, object]:
    return {
        "cost_id": cost_id,
        "category": "compute",
        "quantity": 1.5,
        "unit": "cpu-hour",
        "cadence": "monthly",
        "measurement_source": "evidence:alpha-cost",
        "owner": "role:alpha-steward",
        "alternative_ref": None,
        "last_measured_at": "2026-08-26T00:00:00Z",
        "review_due_at": "2026-09-26T00:00:00Z",
    }


def _load_records(root: Path, records: list[dict[str, object]]) -> dict[str, cc.ComponentRecord]:
    root.mkdir(parents=True, exist_ok=True)
    for data in records:
        (root / f"{data['component_id']}.yaml").write_text(
            yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
        )
    return cc.load_component_records(root)


def _parse_manifest(root: Path, data: dict[str, object]) -> ar.AssemblyManifest:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{data['assembly_id']}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return ar.parse_assembly_manifest(path)


def _kinds(plan: ar.AssemblyPlan) -> set[RefusalKind]:
    return {refusal.kind for refusal in plan.refusals}


class AssemblyResolverTests(unittest.TestCase):
    # -- schemas -----------------------------------------------------------

    def test_generated_schemas_match_committed_bytes(self) -> None:
        for name, generator in (
            ("assembly-manifest.schema.json", ar.generate_assembly_manifest_schema),
            ("assembly-lock.schema.json", ar.generate_assembly_lock_schema),
        ):
            committed = (REPO_ROOT / "schemas" / name).read_bytes()
            self.assertEqual(committed, generator(), f"{name} is stale")

    def test_cli_generate_schemas_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self.assertEqual(ar.main(["generate-schemas", "--repo-root", str(repo)]), 0)
            for name, generator in (
                ("assembly-manifest.schema.json", ar.generate_assembly_manifest_schema),
                ("assembly-lock.schema.json", ar.generate_assembly_lock_schema),
            ):
                self.assertEqual((repo / "schemas" / name).read_bytes(), generator())

    def test_schemas_are_closed_where_properties_are_declared(self) -> None:
        def assert_closed(value: object) -> None:
            if isinstance(value, dict):
                if "properties" in value:
                    self.assertIs(value.get("additionalProperties"), False)
                for child in value.values():
                    assert_closed(child)
            elif isinstance(value, list):
                for child in value:
                    assert_closed(child)

        for generator in (
            ar.generate_assembly_manifest_schema,
            ar.generate_assembly_lock_schema,
        ):
            schema = json.loads(generator())
            self.assertIs(schema["additionalProperties"], False)
            assert_closed(schema)

    # -- manifest parsing --------------------------------------------------

    def test_manifest_round_trips_and_rejects_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component()]))
            self.assertEqual(manifest.assembly_id, "alpha-assembly")
            self.assertEqual(manifest.components[0].component_id, "alpha-component")
            bad = _manifest()
            bad["unexpected"] = 1
            with self.assertRaises(ar.AssemblyResolverError):
                _parse_manifest(root, bad)

    def test_manifest_refuses_duplicate_component_and_bad_version_range(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            dup = _manifest(components=[_manifest_component(), _manifest_component()])
            with self.assertRaisesRegex(ar.AssemblyResolverError, "component_id"):
                _parse_manifest(root, dup)
            bad_version = _manifest(
                components=[_manifest_component(version_ranges=["not-a-version"])]
            )
            with self.assertRaises(ar.AssemblyResolverError):
                _parse_manifest(root, bad_version)

    # -- catalogue / graph surfacing --------------------------------------

    def test_zero_component_assembly_locks_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(root / "records", [])
            manifest = _parse_manifest(root, _manifest(components=[]))
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            self.assertEqual(plan.refusals, ())
            first = ar.render_lock(plan)
            for _ in range(5):
                self.assertEqual(ar.render_lock(plan), first)
            lock = json.loads(first)
            self.assertEqual(lock["components"], [])
            self.assertEqual(lock["resolution_order"], list(ar.RESOLUTION_ORDER))

    def test_one_component_assembly_binds_exact_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(root / "records", [_record()])
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component()]))
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            self.assertEqual(plan.refusals, ())
            lock = json.loads(ar.render_lock(plan))
            component = lock["components"][0]
            self.assertEqual(component["component_id"], "alpha-component")
            self.assertEqual(component["logical_repo"], "alpha-component-repo")
            self.assertEqual(component["revision"], "revision:alpha-component-repo-1")
            self.assertEqual(
                component["contract_versions"],
                [{"contract": "alpha-contract", "version": "1.0.0"}],
            )

    def test_shared_reusable_component_pulled_into_closure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(
                root / "records",
                [
                    _record("provider", provides=[_provision("shared-capability")]),
                    _record(
                        "consumer-a",
                        provides=[_provision("a-out")],
                        hard=[_requirement("shared-capability")],
                    ),
                    _record(
                        "consumer-b",
                        provides=[_provision("b-out")],
                        hard=[_requirement("shared-capability")],
                    ),
                ],
            )
            manifest = _parse_manifest(
                root,
                _manifest(
                    components=[_manifest_component("consumer-a"), _manifest_component("consumer-b")]
                ),
            )
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            self.assertEqual(plan.refusals, ())
            locked_ids = [component.component_id for component in plan.locked_components]
            self.assertEqual(locked_ids, ["consumer-a", "consumer-b", "provider"])
            consumer_a = next(c for c in plan.locked_components if c.component_id == "consumer-a")
            self.assertEqual(consumer_a.requirement_closure, ("provider",))

    def test_unknown_component_ref_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(root / "records", [])
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component("ghost")]))
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            self.assertEqual(_kinds(plan), {RefusalKind.UNKNOWN_PROVIDER})

    # -- compatibility -----------------------------------------------------

    def test_unsatisfied_hard_requirement_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(
                root / "records",
                [_record("consumer", provides=[_provision("c-out")], hard=[_requirement("absent")])],
            )
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component("consumer")]))
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            self.assertEqual(_kinds(plan), {RefusalKind.UNSATISFIED_HARD_REQUIREMENT})

    def test_ambiguous_provider_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(
                root / "records",
                [
                    _record("provider-one", provides=[_provision("dup-capability")]),
                    _record("provider-two", provides=[_provision("dup-capability")]),
                    _record(
                        "consumer",
                        provides=[_provision("c-out")],
                        hard=[_requirement("dup-capability")],
                    ),
                ],
            )
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component("consumer")]))
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            self.assertEqual(_kinds(plan), {RefusalKind.AMBIGUOUS_PROVIDER})

    def test_contract_schema_version_feature_mismatch_are_separate_axes(self) -> None:
        cases = {
            "contract": ({"contract": "wrong-contract"}, RefusalKind.CONTRACT_MISMATCH),
            "schema": ({"schema": "wrong-schema"}, RefusalKind.SCHEMA_MISMATCH),
            "version": ({"version": "2.0.0"}, RefusalKind.VERSION_MISMATCH),
            "feature": ({"feature": "missing-feature"}, RefusalKind.FEATURE_CONFLICT),
        }
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for name, (change, expected) in cases.items():
                root = base / name
                requirement = _requirement("shared")
                requirement.update(change)
                records = _load_records(
                    root / "records",
                    [
                        _record("provider", provides=[_provision("shared")]),
                        _record(
                            "consumer", provides=[_provision("c-out")], hard=[requirement]
                        ),
                    ],
                )
                manifest = _parse_manifest(
                    root, _manifest(components=[_manifest_component("consumer")])
                )
                plan = ar.resolve_assembly(
                    manifest, records, FixtureRepoResolver(), FixtureEvidence()
                )
                with self.subTest(name=name):
                    self.assertEqual(_kinds(plan), {expected})

    def test_manifest_ranges_and_required_features_constrain_named_component(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(root / "records", [_record()])
            manifest = _parse_manifest(
                root,
                _manifest(
                    components=[
                        _manifest_component(
                            contract_ranges=["other-contract"],
                            version_ranges=["9.9.9"],
                            required_features=["missing-feature"],
                        )
                    ]
                ),
            )
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            self.assertEqual(
                _kinds(plan),
                {
                    RefusalKind.CONTRACT_MISMATCH,
                    RefusalKind.VERSION_MISMATCH,
                    RefusalKind.FEATURE_CONFLICT,
                },
            )

    def test_component_and_feature_conflicts_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            component_conflict = [
                {"kind": "component", "target": "provider", "reason_ref": "decision:no"}
            ]
            feature_conflict = [
                {"kind": "feature", "target": "camera", "reason_ref": "decision:no-camera"}
            ]
            for name, conflicts, expected in (
                ("component", component_conflict, RefusalKind.COMPONENT_CONFLICT),
                ("feature", feature_conflict, RefusalKind.FEATURE_CONFLICT),
            ):
                root = base / name
                records = _load_records(
                    root / "records",
                    [
                        _record(
                            "provider", provides=[_provision("p-out", features=["camera"])]
                        ),
                        _record(
                            "consumer",
                            provides=[_provision("c-out")],
                            hard=[_requirement("p-out")],
                            conflicts=conflicts,
                        ),
                    ],
                )
                manifest = _parse_manifest(
                    root, _manifest(components=[_manifest_component("consumer")])
                )
                plan = ar.resolve_assembly(
                    manifest, records, FixtureRepoResolver(), FixtureEvidence()
                )
                with self.subTest(name=name):
                    self.assertIn(expected, _kinds(plan))

    def test_disabled_optional_is_green_but_enabling_it_needs_a_provider(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            # disabled optional with declared degradation -> green, no edge.
            green_root = base / "green"
            records = _load_records(
                green_root / "records",
                [
                    _record(
                        "consumer",
                        provides=[_provision("c-out")],
                        optional=[
                            _requirement(
                                "absent",
                                feature="camera",
                                enabled=False,
                                degradation_ref="contract:degraded",
                            )
                        ],
                    )
                ],
            )
            manifest = _parse_manifest(
                green_root, _manifest(components=[_manifest_component("consumer")])
            )
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            self.assertEqual(plan.refusals, ())

            # manifest enables the optional feature -> requirement must resolve.
            enable_root = base / "enable"
            records = _load_records(
                enable_root / "records",
                [
                    _record(
                        "consumer",
                        provides=[_provision("c-out")],
                        optional=[
                            _requirement(
                                "absent",
                                feature="camera",
                                enabled=False,
                                degradation_ref="contract:degraded",
                            )
                        ],
                    )
                ],
            )
            manifest = _parse_manifest(
                enable_root,
                _manifest(
                    components=[
                        _manifest_component("consumer", enabled_optional_features=["camera"])
                    ]
                ),
            )
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            self.assertEqual(_kinds(plan), {RefusalKind.UNSATISFIED_HARD_REQUIREMENT})

    def test_missing_and_wrong_scope_implication_acknowledgements_refuse(self) -> None:
        implications = {
            "authority": [
                {
                    "implication_id": "auth-impl",
                    "scope": "seed:test",
                    "evidence_refs": ["evidence:auth"],
                }
            ],
            "privacy_data": [],
            "hardware_network": [],
            "operational": [],
        }
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            # missing acknowledgement
            missing_root = base / "missing"
            records = _load_records(
                missing_root / "records", [_record(implications=implications)]
            )
            manifest = _parse_manifest(
                missing_root, _manifest(components=[_manifest_component()])
            )
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            self.assertEqual(_kinds(plan), {RefusalKind.UNACKNOWLEDGED_IMPLICATION})

            # acknowledged but with evidence outside the implication scope
            scope_root = base / "scope"
            records = _load_records(
                scope_root / "records", [_record(implications=implications)]
            )
            manifest = _parse_manifest(
                scope_root,
                _manifest(
                    components=[_manifest_component()],
                    acks=[
                        {
                            "implication_id": "auth-impl",
                            "authority_ref": "authority:x",
                            "owner_ref": "owner:x",
                            "evidence_ref": "evidence:WRONG",
                        }
                    ],
                ),
            )
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            self.assertEqual(_kinds(plan), {RefusalKind.UNACKNOWLEDGED_IMPLICATION})

            # correct acknowledgement -> green and recorded
            ok_root = base / "ok"
            records = _load_records(ok_root / "records", [_record(implications=implications)])
            manifest = _parse_manifest(
                ok_root,
                _manifest(
                    components=[_manifest_component()],
                    acks=[
                        {
                            "implication_id": "auth-impl",
                            "authority_ref": "authority:x",
                            "owner_ref": "owner:x",
                            "evidence_ref": "evidence:auth",
                        }
                    ],
                ),
            )
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            self.assertEqual(plan.refusals, ())
            self.assertEqual(plan.acknowledged_implications, ("auth-impl",))

    # -- upkeep acknowledgement gate (T-3931, ORG-055 §6 fidelity) ---------

    @staticmethod
    def _hashed_ack_id(prefix: str, entry: str) -> str:
        # Independent re-derivation of the resolver's scheme so the assertion is a
        # real check (not a tautology against the SUT): sha256(entry, utf-8)[:12].
        return f"{prefix}-{hashlib.sha256(entry.encode('utf-8')).hexdigest()[:12]}"

    def _resolve_one(
        self, root: Path, record: dict[str, object], acks: list[dict[str, object]] | None = None
    ) -> ar.AssemblyPlan:
        records = _load_records(root / "records", [record])
        manifest = _parse_manifest(
            root, _manifest(components=[_manifest_component()], acks=acks)
        )
        return ar.resolve_assembly(manifest, records, FixtureRepoResolver(), FixtureEvidence())

    @staticmethod
    def _ack(implication_id: str, evidence_ref: str = "evidence:whatever") -> dict[str, object]:
        return {
            "implication_id": implication_id,
            "authority_ref": "authority:x",
            "owner_ref": "owner:x",
            "evidence_ref": evidence_ref,
        }

    def test_upkeep_cost_requires_acknowledgement(self) -> None:
        ack_id = "cost-alpha-cost"
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            # unacknowledged cost -> refusal whose detail names the derived ack-id
            plan = self._resolve_one(base / "missing", _record(cost=[_cost()]))
            self.assertEqual(_kinds(plan), {RefusalKind.UNACKNOWLEDGED_IMPLICATION})
            self.assertIn(ack_id, plan.refusals[0].detail)
            self.assertIn("alpha-cost", plan.refusals[0].detail)
            # acknowledged (ack id == cost-<cost_id>; no evidence-scope check) -> green
            plan = self._resolve_one(base / "ok", _record(cost=[_cost()]), acks=[self._ack(ack_id)])
            self.assertEqual(plan.refusals, ())
            self.assertIn(ack_id, plan.acknowledged_implications)

    def test_upkeep_storage_retention_requires_acknowledgement(self) -> None:
        entry = "retain-raw-events-for-395-days"
        ack_id = self._hashed_ack_id("storage-retention", entry)
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            plan = self._resolve_one(base / "missing", _record(storage_retention=[entry]))
            self.assertEqual(_kinds(plan), {RefusalKind.UNACKNOWLEDGED_IMPLICATION})
            self.assertIn(ack_id, plan.refusals[0].detail)
            self.assertIn(entry, plan.refusals[0].detail)
            plan = self._resolve_one(
                base / "ok", _record(storage_retention=[entry]), acks=[self._ack(ack_id)]
            )
            self.assertEqual(plan.refusals, ())
            self.assertIn(ack_id, plan.acknowledged_implications)

    def test_upkeep_credential_rotation_requires_acknowledgement(self) -> None:
        entry = "rotate-cloud-api-key-every-90-days"
        ack_id = self._hashed_ack_id("credential-rotation", entry)
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            plan = self._resolve_one(base / "missing", _record(credential_rotation=[entry]))
            self.assertEqual(_kinds(plan), {RefusalKind.UNACKNOWLEDGED_IMPLICATION})
            self.assertIn(ack_id, plan.refusals[0].detail)
            self.assertIn(entry, plan.refusals[0].detail)
            plan = self._resolve_one(
                base / "ok", _record(credential_rotation=[entry]), acks=[self._ack(ack_id)]
            )
            self.assertEqual(plan.refusals, ())
            self.assertIn(ack_id, plan.acknowledged_implications)

    def test_all_upkeep_families_acknowledged_locks_deterministically(self) -> None:
        retention = "retain-pii-30-days"
        credential = "rotate-signing-key-quarterly"
        cost_ack = "cost-alpha-cost"
        retention_ack = self._hashed_ack_id("storage-retention", retention)
        credential_ack = self._hashed_ack_id("credential-rotation", credential)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            record = _record(
                cost=[_cost()],
                storage_retention=[retention],
                credential_rotation=[credential],
            )
            plan = self._resolve_one(
                root,
                record,
                acks=[
                    self._ack(cost_ack),
                    self._ack(retention_ack),
                    self._ack(credential_ack),
                ],
            )
            self.assertEqual(plan.refusals, ())
            # all three derived ids folded into the single acknowledged set (sorted)
            self.assertEqual(
                plan.acknowledged_implications,
                tuple(sorted((cost_ack, retention_ack, credential_ack))),
            )
            first = ar.render_lock(plan)
            for _ in range(5):
                self.assertEqual(ar.render_lock(plan), first)

    def test_upkeep_ack_id_colliding_with_implication_id_refuses_ambiguous(self) -> None:
        # F1 (T-3931 rebound-1): a derived upkeep ack-id must not silently share an
        # acknowledgement with a ComponentImplication of the same id.  Here a cost
        # with cost_id "alpha-cost" derives ack-id "cost-alpha-cost", and the
        # component ALSO declares an authority implication whose implication_id is
        # exactly "cost-alpha-cost".  A single ack "cost-alpha-cost" must NOT
        # discharge both -- the resolver must refuse the collision.
        implications = {
            "authority": [
                {
                    "implication_id": "cost-alpha-cost",
                    "scope": "seed:test",
                    "evidence_refs": ["evidence:collide"],
                }
            ],
            "privacy_data": [],
            "hardware_network": [],
            "operational": [],
        }
        with tempfile.TemporaryDirectory() as td:
            plan = self._resolve_one(
                Path(td),
                _record(implications=implications, cost=[_cost("alpha-cost")]),
                acks=[self._ack("cost-alpha-cost", evidence_ref="evidence:collide")],
            )
            # Pre-fix this resolved clean with acknowledged=('cost-alpha-cost',);
            # post-fix the collision is refused as an ambiguous acknowledgement.
            self.assertEqual(_kinds(plan), {RefusalKind.UNACKNOWLEDGED_IMPLICATION})
            self.assertTrue(
                any(
                    "cost-alpha-cost" in r.detail and "ambiguous" in r.detail
                    for r in plan.refusals
                ),
                plan.refusals,
            )

    def test_empty_upkeep_lock_bytes_unchanged_and_no_new_refusals(self) -> None:
        # Regression: the T-3931 upkeep gate must not touch an assembly that
        # declares NO cost/storage_retention/credential_rotation items.  The gate
        # writes ONLY into acknowledged_implications, so an empty-upkeep assembly
        # keeping that array empty (and refusal-free) is exactly the byte-identity
        # guarantee -- acknowledged_implications is the sole lock field the gate
        # can affect.
        with tempfile.TemporaryDirectory() as td:
            plan = self._resolve_one(Path(td), _record())
            self.assertEqual(plan.refusals, ())
            self.assertEqual(plan.acknowledged_implications, ())
            lock = json.loads(ar.render_lock(plan))
            self.assertEqual(lock["acknowledged_implications"], [])

    def test_unowned_obligation_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(root / "records", [_record()])
            # Independently enforce ownership even if a record slipped past the
            # catalogue steward gate: blank the steward on the loaded record.
            record = records["alpha-component"]
            records = {
                "alpha-component": dataclasses.replace(
                    record, upkeep=dataclasses.replace(record.upkeep, steward="  ")
                )
            }
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component()]))
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            self.assertEqual(_kinds(plan), {RefusalKind.UNOWNED_OBLIGATION})

    # -- source identity ---------------------------------------------------

    def test_unresolved_source_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(root / "records", [_record(logical_repo="ghost-repo")])
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component()]))
            plan = ar.resolve_assembly(
                manifest,
                records,
                FixtureRepoResolver(missing=frozenset({"ghost-repo"})),
                FixtureEvidence(),
            )
            self.assertEqual(_kinds(plan), {RefusalKind.UNRESOLVED_SOURCE})

    def test_provenance_mismatch_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(
                root / "records", [_record(tested_revision="revision:expected")]
            )
            resolver = FixtureRepoResolver(
                {
                    "alpha-component-repo": ResolvedSource(
                        logical_repo="alpha-component-repo",
                        resolved_source="mirror://alpha",
                        revision="revision:DRIFTED",
                        artifact_digest="sha256:x",
                    )
                }
            )
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component()]))
            plan = ar.resolve_assembly(manifest, records, resolver, FixtureEvidence())
            self.assertEqual(_kinds(plan), {RefusalKind.PROVENANCE_MISMATCH})

    # -- smoke / health evidence ------------------------------------------

    def test_smoke_and_health_failure_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(root / "records", [_record()])
            manifest = _parse_manifest(
                root,
                _manifest(
                    components=[_manifest_component()],
                    smoke=["smoke:cross"],
                    health=["health:cross"],
                ),
            )
            plan = ar.resolve_assembly(
                manifest,
                records,
                FixtureRepoResolver(),
                FixtureEvidence(absent=frozenset({"smoke:cross", "health:cross"})),
            )
            self.assertEqual(
                _kinds(plan), {RefusalKind.SMOKE_FAILURE, RefusalKind.HEALTH_FAILURE}
            )

    # -- deterministic lock + verify --------------------------------------

    def test_lock_bytes_are_input_ordering_independent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(
                root / "records",
                [
                    _record("first", provides=[_provision("first-out")]),
                    _record("second", provides=[_provision("second-out")]),
                ],
            )
            forward = _parse_manifest(
                root / "a",
                _manifest(
                    components=[_manifest_component("first"), _manifest_component("second")]
                ),
            )
            reverse = _parse_manifest(
                root / "b",
                _manifest(
                    components=[_manifest_component("second"), _manifest_component("first")]
                ),
            )
            plan_forward = ar.resolve_assembly(
                forward, records, FixtureRepoResolver(), FixtureEvidence()
            )
            plan_reverse = ar.resolve_assembly(
                reverse, records, FixtureRepoResolver(), FixtureEvidence()
            )
            # Same manifest bytes (component list order differs only) => identical
            # locked-component sets; manifest digests differ so compare components.
            self.assertEqual(
                [c.component_id for c in plan_forward.locked_components],
                [c.component_id for c in plan_reverse.locked_components],
            )
            reversed_records = dict(reversed(tuple(records.items())))
            plan_reordered_catalogue = ar.resolve_assembly(
                forward, reversed_records, FixtureRepoResolver(), FixtureEvidence()
            )
            self.assertEqual(
                ar.render_lock(plan_forward), ar.render_lock(plan_reordered_catalogue)
            )

    def test_verify_accepts_freshly_generated_lock(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(root / "records", [_record()])
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component()]))
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            lock_bytes = ar.render_lock(plan)
            self.assertIsNone(
                ar.verify_generated_lock(
                    manifest, lock_bytes, records, FixtureRepoResolver(), FixtureEvidence()
                )
            )

    def test_hand_edited_lock_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(root / "records", [_record()])
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component()]))
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            tampered = ar.render_lock(plan).replace(
                b'"revision:alpha-component-repo-1"', b'"revision:hand-edited"'
            )
            with self.assertRaises(ar.AssemblyResolverError) as ctx:
                ar.verify_generated_lock(
                    manifest, tampered, records, FixtureRepoResolver(), FixtureEvidence()
                )
            self.assertEqual(ctx.exception.refusal.kind, RefusalKind.HAND_EDITED_LOCK)

    def test_unparseable_lock_refuses_as_hand_edited(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(root / "records", [_record()])
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component()]))
            with self.assertRaises(ar.AssemblyResolverError) as ctx:
                ar.verify_generated_lock(
                    manifest, b"{not json", records, FixtureRepoResolver(), FixtureEvidence()
                )
            self.assertEqual(ctx.exception.refusal.kind, RefusalKind.HAND_EDITED_LOCK)

    def test_stale_input_digest_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(root / "records", [_record()])
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component()]))
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            lock_bytes = ar.render_lock(plan)
            # A second, materially different manifest with the same assembly_id.
            drifted = _parse_manifest(
                root / "drift",
                _manifest(
                    components=[_manifest_component()],
                    acks=[
                        {
                            "implication_id": "unused",
                            "authority_ref": "a",
                            "owner_ref": "o",
                            "evidence_ref": "e",
                        }
                    ],
                ),
            )
            with self.assertRaises(ar.AssemblyResolverError) as ctx:
                ar.verify_generated_lock(
                    drifted, lock_bytes, records, FixtureRepoResolver(), FixtureEvidence()
                )
            self.assertEqual(ctx.exception.refusal.kind, RefusalKind.STALE_LOCK_INPUT)

    def test_same_revision_changed_artifact_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(root / "records", [_record()])
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component()]))
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            lock_bytes = ar.render_lock(plan)
            drifted_resolver = FixtureRepoResolver(
                {
                    "alpha-component-repo": ResolvedSource(
                        logical_repo="alpha-component-repo",
                        resolved_source="mirror://alpha",
                        revision="revision:alpha-component-repo-1",
                        artifact_digest="sha256:DIFFERENT",
                    )
                }
            )
            with self.assertRaises(ar.AssemblyResolverError) as ctx:
                ar.verify_generated_lock(
                    manifest, lock_bytes, records, drifted_resolver, FixtureEvidence()
                )
            self.assertEqual(
                ctx.exception.refusal.kind, RefusalKind.ARTIFACT_CHANGED_SAME_REVISION
            )

    def test_submodule_absent_is_green_and_disagreement_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(root / "records", [_record()])
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component()]))
            with_submodule = FixtureRepoResolver(
                {
                    "alpha-component-repo": ResolvedSource(
                        logical_repo="alpha-component-repo",
                        resolved_source="mirror://alpha",
                        revision="revision:alpha-component-repo-1",
                        artifact_digest="sha256:alpha-component-repo-artifact",
                        submodules=(SubmoduleView(path="vendor/x", revision="rev-a"),),
                    )
                }
            )
            plan = ar.resolve_assembly(manifest, records, with_submodule, FixtureEvidence())
            self.assertEqual(plan.refusals, ())
            lock_bytes = ar.render_lock(plan)
            # absent-submodule assembly locks green
            plain_plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            self.assertEqual(plain_plan.refusals, ())
            # a disagreeing submodule revision fails verify
            disagreeing = FixtureRepoResolver(
                {
                    "alpha-component-repo": ResolvedSource(
                        logical_repo="alpha-component-repo",
                        resolved_source="mirror://alpha",
                        revision="revision:alpha-component-repo-1",
                        artifact_digest="sha256:alpha-component-repo-artifact",
                        submodules=(SubmoduleView(path="vendor/x", revision="rev-B"),),
                    )
                }
            )
            with self.assertRaises(ar.AssemblyResolverError) as ctx:
                ar.verify_generated_lock(
                    manifest, lock_bytes, records, disagreeing, FixtureEvidence()
                )
            self.assertEqual(ctx.exception.refusal.kind, RefusalKind.SUBMODULE_MISMATCH)

    def test_prior_lock_and_receipt_linkage_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(root / "records", [_record()])
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component()]))
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            prior_bytes = ar.render_lock(plan)
            prior_digest = ar.lock_digest(prior_bytes)
            linked = dataclasses.replace(
                plan,
                prior_lock_sha256=prior_digest,
                update_receipt_ref="receipt:update-1",
            )
            linked_bytes = ar.render_lock(linked)
            parsed = ar.parse_assembly_lock(linked_bytes)
            self.assertEqual(parsed.prior_lock_sha256, prior_digest)
            self.assertEqual(parsed.update_receipt_ref, "receipt:update-1")
            # verify preserves the linkage (does not treat it as hand-edit)
            self.assertIsNone(
                ar.verify_generated_lock(
                    manifest, linked_bytes, records, FixtureRepoResolver(), FixtureEvidence()
                )
            )

    def test_render_lock_refuses_when_plan_has_refusals(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(root / "records", [])
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component("ghost")]))
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            with self.assertRaises(ar.AssemblyResolverError):
                ar.render_lock(plan)

    # -- update / rollback plan -------------------------------------------

    def _candidate_lock(self, root: Path) -> ar.AssemblyLock:
        records = _load_records(root / "records", [_record()])
        manifest = _parse_manifest(root, _manifest(components=[_manifest_component()]))
        plan = ar.resolve_assembly(manifest, records, FixtureRepoResolver(), FixtureEvidence())
        return ar.build_lock(plan)

    def test_update_plan_lists_transaction_and_rollback_stages(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            candidate = self._candidate_lock(Path(td))
            update = ar.plan_assembly_update(None, candidate, FixtureLifecycle())
            self.assertEqual(update.stages, ar.UPDATE_STAGES)
            self.assertEqual(update.rollback, ar.ROLLBACK_STAGES)
            self.assertEqual(update.stages[-1], "publish-lock-and-receipt")
            self.assertEqual(update.rollback[0], "restore-prior-lock")
            self.assertTrue(update.lifecycle_plan.ok)

    def test_update_plan_surfaces_lifecycle_refusals(self) -> None:
        kinds = (
            RefusalKind.DIRTY_CHECKOUT,
            RefusalKind.DIVERGED_CHECKOUT,
            RefusalKind.FOREIGN_CHECKOUT,
            RefusalKind.MISSING_REF,
            RefusalKind.ROLLBACK_FAILURE,
        )
        with tempfile.TemporaryDirectory() as td:
            candidate = self._candidate_lock(Path(td))
            for kind in kinds:
                lifecycle = FixtureLifecycle(
                    refusal=CompatibilityRefusal(kind, "alpha-component-repo", "lifecycle refused")
                )
                with self.subTest(kind=kind), self.assertRaises(ar.AssemblyResolverError) as ctx:
                    ar.plan_assembly_update(None, candidate, lifecycle)
                self.assertEqual(ctx.exception.refusal.kind, kind)

    # -- atomic publish + crash boundary ----------------------------------

    def test_publish_lock_cas_and_atomicity(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "assembly.lock.json"
            first = b'{"lock": 1}\n'
            digest = ar.publish_lock(path, first, expected_prior_digest=None)
            self.assertEqual(path.read_bytes(), first)
            self.assertEqual(digest, ar.lock_digest(first))
            # CAS with a stale expected prior digest refuses (no write).
            with self.assertRaises(ar.AssemblyResolverError) as ctx:
                ar.publish_lock(path, b'{"lock": 2}\n', expected_prior_digest="sha256:stale")
            self.assertEqual(ctx.exception.refusal.kind, RefusalKind.STALE_LOCK_INPUT)
            self.assertEqual(path.read_bytes(), first)
            # Correct CAS succeeds.
            second = b'{"lock": 2}\n'
            ar.publish_lock(path, second, expected_prior_digest=digest)
            self.assertEqual(path.read_bytes(), second)

    def test_publish_lock_crash_leaves_prior_intact_and_no_temp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td)
            path = directory / "assembly.lock.json"
            original = b'{"lock": "original"}\n'
            ar.publish_lock(path, original, expected_prior_digest=None)
            with mock.patch(
                "tools.component_catalogue.os.replace", side_effect=OSError("crash")
            ):
                with self.assertRaises(OSError):
                    ar.publish_lock(
                        path, b'{"lock": "new"}\n', expected_prior_digest=ar.lock_digest(original)
                    )
            # the commit point never landed: prior file is exactly intact
            self.assertEqual(path.read_bytes(), original)
            # no temporary artifact leaked into the directory
            leftovers = [p.name for p in directory.iterdir() if p.name != path.name]
            self.assertEqual(leftovers, [])

    # -- FIX 1: audit-field tamper gap (shape here, lineage in plan-update) --

    def test_forged_prior_lock_sha256_passes_verify_but_fails_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(root / "records", [_record()])
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component()]))
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            prior_bytes = ar.render_lock(plan)
            prior_lock = ar.build_lock(plan)
            prior_digest = ar.lock_digest(prior_bytes)

            # A candidate correctly linked back to the real prior lock is accepted.
            linked_plan = dataclasses.replace(
                plan, prior_lock_sha256=prior_digest, update_receipt_ref="receipt:1"
            )
            linked_candidate = ar.build_lock(linked_plan)
            update = ar.plan_assembly_update(
                prior_lock, linked_candidate, FixtureLifecycle(), prior_lock_bytes=prior_bytes
            )
            self.assertTrue(update.lifecycle_plan.ok)

            # A FORGED but well-formed prior_lock_sha256 passes verify (verify has
            # no prior lock; it validates shape only, not lineage) ...
            forged_plan = dataclasses.replace(plan, prior_lock_sha256="sha256:" + "0" * 64)
            forged_bytes = ar.render_lock(forged_plan)
            self.assertIsNone(
                ar.verify_generated_lock(
                    manifest, forged_bytes, records, FixtureRepoResolver(), FixtureEvidence()
                )
            )
            # ... but the lineage gate in plan_assembly_update catches it against
            # the real prior lock (re-rendered from `old` here, no bytes passed).
            forged_candidate = ar.build_lock(forged_plan)
            with self.assertRaises(ar.AssemblyResolverError) as ctx:
                ar.plan_assembly_update(prior_lock, forged_candidate, FixtureLifecycle())
            self.assertEqual(ctx.exception.refusal.kind, RefusalKind.STALE_LOCK_INPUT)

    def test_malformed_audit_field_refused_by_verify(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(root / "records", [_record()])
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component()]))
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            lock_doc = json.loads(ar.render_lock(plan))
            cases = {
                "prior_lock_sha256": ("prior_lock_sha256", "not-a-sha256-digest"),
                "update_receipt_ref": ("update_receipt_ref", ""),
            }
            for name, (field, bad_value) in cases.items():
                doc = dict(lock_doc)
                doc[field] = bad_value
                malformed = json.dumps(doc).encode("utf-8")
                with self.subTest(field=name):
                    with self.assertRaises(ar.AssemblyResolverError) as ctx:
                        ar.verify_generated_lock(
                            manifest,
                            malformed,
                            records,
                            FixtureRepoResolver(),
                            FixtureEvidence(),
                        )
                    self.assertEqual(ctx.exception.refusal.kind, RefusalKind.HAND_EDITED_LOCK)
                    # non-vacuous: the SHAPE validator fired, not the byte-compare.
                    self.assertIn(field, ctx.exception.refusal.detail)

    # -- FIX 2: multi-version contract/schema (no id-keyed collapse) --------

    def test_same_contract_and_schema_id_at_two_versions_are_both_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(
                root / "records",
                [
                    _record(
                        "multi",
                        provides=[
                            _provision(
                                "cap-a",
                                contract="shared-contract",
                                schema="shared-schema",
                                version="1.0.0",
                            ),
                            _provision(
                                "cap-b",
                                contract="shared-contract",
                                schema="shared-schema",
                                version="2.0.0",
                            ),
                        ],
                    )
                ],
            )
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component("multi")]))
            plan = ar.resolve_assembly(
                manifest, records, FixtureRepoResolver(), FixtureEvidence()
            )
            self.assertEqual(plan.refusals, ())
            lock_bytes = ar.render_lock(plan)
            component = json.loads(lock_bytes)["components"][0]
            self.assertEqual(
                component["contract_versions"],
                [
                    {"contract": "shared-contract", "version": "1.0.0"},
                    {"contract": "shared-contract", "version": "2.0.0"},
                ],
            )
            self.assertEqual(
                component["schema_versions"],
                [
                    {"schema": "shared-schema", "version": "1.0.0"},
                    {"schema": "shared-schema", "version": "2.0.0"},
                ],
            )
            # Round-trips through parse without collapse.
            parsed = ar.parse_assembly_lock(lock_bytes)
            self.assertEqual(
                parsed.components[0].contract_versions,
                (("shared-contract", "1.0.0"), ("shared-contract", "2.0.0")),
            )

    # -- FIX 3: refusal-kind specificity -----------------------------------

    def test_submodule_disagreement_yields_submodule_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(root / "records", [_record()])
            manifest = _parse_manifest(root, _manifest(components=[_manifest_component()]))
            with_submodule = FixtureRepoResolver(
                {
                    "alpha-component-repo": ResolvedSource(
                        logical_repo="alpha-component-repo",
                        resolved_source="mirror://alpha",
                        revision="revision:alpha-component-repo-1",
                        artifact_digest="sha256:alpha-component-repo-artifact",
                        submodules=(SubmoduleView(path="vendor/x", revision="rev-a"),),
                    )
                }
            )
            plan = ar.resolve_assembly(manifest, records, with_submodule, FixtureEvidence())
            lock_bytes = ar.render_lock(plan)
            disagreeing = FixtureRepoResolver(
                {
                    "alpha-component-repo": ResolvedSource(
                        logical_repo="alpha-component-repo",
                        resolved_source="mirror://alpha",
                        revision="revision:alpha-component-repo-1",
                        artifact_digest="sha256:alpha-component-repo-artifact",
                        submodules=(SubmoduleView(path="vendor/x", revision="rev-B"),),
                    )
                }
            )
            with self.assertRaises(ar.AssemblyResolverError) as ctx:
                ar.verify_generated_lock(
                    manifest, lock_bytes, records, disagreeing, FixtureEvidence()
                )
            self.assertEqual(ctx.exception.refusal.kind, RefusalKind.SUBMODULE_MISMATCH)

    def test_untyped_lifecycle_refusal_is_neutral_lifecycle_refused(self) -> None:
        class _UntypedLifecycle:
            def plan(self, sources) -> LifecyclePlan:  # noqa: ANN001
                return LifecyclePlan(ok=False, stages=(), refusal=None)

        with tempfile.TemporaryDirectory() as td:
            candidate = self._candidate_lock(Path(td))
            with self.assertRaises(ar.AssemblyResolverError) as ctx:
                ar.plan_assembly_update(None, candidate, _UntypedLifecycle())
            self.assertEqual(ctx.exception.refusal.kind, RefusalKind.LIFECYCLE_REFUSED)

    # -- FIX 4: manifest byte-order is part of the pinned identity ----------

    def test_manifest_component_reordering_changes_only_manifest_sha256(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = _load_records(
                root / "records",
                [
                    _record("first", provides=[_provision("first-out")]),
                    _record("second", provides=[_provision("second-out")]),
                ],
            )
            forward = _parse_manifest(
                root / "a",
                _manifest(
                    components=[_manifest_component("first"), _manifest_component("second")]
                ),
            )
            reverse = _parse_manifest(
                root / "b",
                _manifest(
                    components=[_manifest_component("second"), _manifest_component("first")]
                ),
            )
            bytes_forward = ar.render_lock(
                ar.resolve_assembly(forward, records, FixtureRepoResolver(), FixtureEvidence())
            )
            bytes_reverse = ar.render_lock(
                ar.resolve_assembly(reverse, records, FixtureRepoResolver(), FixtureEvidence())
            )
            # Reordering the manifest's components: list changes the lock ...
            self.assertNotEqual(bytes_forward, bytes_reverse)
            doc_forward = json.loads(bytes_forward)
            doc_reverse = json.loads(bytes_reverse)
            # ... but only via manifest_sha256; the components array is identical.
            self.assertNotEqual(doc_forward["manifest_sha256"], doc_reverse["manifest_sha256"])
            self.assertEqual(doc_forward["components"], doc_reverse["components"])
            del doc_forward["manifest_sha256"]
            del doc_reverse["manifest_sha256"]
            self.assertEqual(doc_forward, doc_reverse)

    # -- CLI ---------------------------------------------------------------

    def test_cli_resolve_verify_and_plan_update_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _load_records(repo / "components" / "records", [_record()])
            manifest_path = repo / "manifest.yaml"
            manifest_path.write_text(
                yaml.safe_dump(_manifest(components=[_manifest_component()]), sort_keys=False),
                encoding="utf-8",
            )
            facts = {
                "sources": {
                    "alpha-component-repo": {
                        "resolved_source": "mirror://alpha",
                        "revision": "revision:alpha-component-repo-1",
                        "artifact_digest": "sha256:alpha-artifact",
                    }
                },
                "evidence": {},
            }
            facts_path = repo / "facts.json"
            facts_path.write_text(json.dumps(facts), encoding="utf-8")
            lock_path = repo / "assembly.lock.json"
            rc = ar.main(
                [
                    "resolve",
                    "--manifest",
                    str(manifest_path),
                    "--repo-root",
                    str(repo),
                    "--facts",
                    str(facts_path),
                    "--out",
                    str(lock_path),
                ]
            )
            self.assertEqual(rc, 0)
            self.assertTrue(lock_path.is_file())
            rc = ar.main(
                [
                    "verify-lock",
                    "--manifest",
                    str(manifest_path),
                    "--repo-root",
                    str(repo),
                    "--facts",
                    str(facts_path),
                    "--lock",
                    str(lock_path),
                ]
            )
            self.assertEqual(rc, 0)
            with contextlib.redirect_stdout(io.StringIO()):
                rc = ar.main(["plan-update", "--candidate", str(lock_path)])
            self.assertEqual(rc, 0)

    def test_cli_resolve_reports_refusals_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "components" / "records").mkdir(parents=True)
            manifest_path = repo / "manifest.yaml"
            manifest_path.write_text(
                yaml.safe_dump(
                    _manifest(components=[_manifest_component("ghost")]), sort_keys=False
                ),
                encoding="utf-8",
            )
            rc = ar.main(
                ["resolve", "--manifest", str(manifest_path), "--repo-root", str(repo)]
            )
            self.assertEqual(rc, 1)

    def test_live_committed_schemas_are_current(self) -> None:
        self.assertEqual(
            (REPO_ROOT / "schemas" / "assembly-manifest.schema.json").read_bytes(),
            ar.generate_assembly_manifest_schema(),
        )
        self.assertEqual(
            (REPO_ROOT / "schemas" / "assembly-lock.schema.json").read_bytes(),
            ar.generate_assembly_lock_schema(),
        )


if __name__ == "__main__":
    unittest.main()
