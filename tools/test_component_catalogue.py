"""Tests for the ORG-055 component catalogue substrate (T-3181)."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest

import yaml

from tools import component_catalogue as cc


REPO_ROOT = Path(__file__).resolve().parents[1]


def _project(project_id: str = "alpha-project") -> dict[str, object]:
    return {
        "schema_version": 1,
        "project_id": project_id,
        "telos": f"Own the outcome for {project_id}",
        "parent_goal_ref": None,
        "classification": "independent",
        "boundary": {"inside": [], "outside": [], "not_owned": []},
        "interfaces": {"provides": [], "requires": []},
        "stewardship": {
            "role": "project-steward",
            "authority_surface": "ticket:T-3181",
        },
        "lifecycle": {
            "developmental_mode": "shaping",
            "formed_at": "2026-08-26T00:00:00Z",
            "transition_refs": [],
        },
        "evidence": {
            "health": [],
            "user_value": [],
            "safety": [],
            "developmental": [],
        },
        "tensions": [],
        "operational_contract": {
            "obligations": {
                "deployment": [],
                "data": [],
                "security": [],
                "cost": [],
                "maintenance": [],
                "recovery": [],
            },
            "lifecycle": {
                "install": [],
                "configure": [],
                "health_monitor": [],
                "update": [],
                "remove": [],
            },
        },
    }


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


def _subject_visibility(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "subject_refs": ["subject:alpha"],
        "role_refs": [],
        "instance_refs": [],
        "trust_zones": ["test-zone"],
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
    logical_repo: str = "alpha-repo",
    path: str | None = "components/alpha",
    provides: list[dict[str, object]] | None = None,
    hard: list[dict[str, object]] | None = None,
    optional: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "component_id": component_id,
        "project_id": project_id,
        "kind": "service",
        "source": {
            "logical_repo": logical_repo,
            "path": path,
            "artifact": None,
        },
        "provides": [_provision()] if provides is None else provides,
        "requires": {
            "hard": [] if hard is None else hard,
            "optional": [] if optional is None else optional,
        },
        "compatibility": {
            "accepts": [],
            "conflicts": [],
            "tested_revision": "revision:alpha-1",
        },
        "deployment": {"loci": ["host:test"], "trust_zone": "test-zone"},
        "implications": {
            "authority": [],
            "privacy_data": [],
            "hardware_network": [],
            "operational": [],
        },
        "upkeep": {
            "steward": "role:alpha-steward",
            "health_evidence": ["evidence:alpha-health"],
            "monitoring": ["contract:alpha-monitor"],
            "upgrade_rollback": ["contract:alpha-rollback"],
            "credential_rotation": [],
            "storage_retention": [],
            "cost": [],
            "review_obligations": [],
        },
        "maturity": {
            "developmental_mode": "shaping",
            "replaceability": "required",
        },
        "tensions": [],
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


def _implication(implication_id: str = "alpha-authority") -> dict[str, object]:
    return {
        "implication_id": implication_id,
        "scope": "seed:test",
        "evidence_refs": ["evidence:alpha-authority"],
    }


def _decision(decision: str = "extract") -> dict[str, object]:
    return {
        "schema_version": 1,
        "decision_id": "repository-boundary:alpha",
        "project_id": "alpha-project",
        "component_id": "alpha-component",
        "current_source": {
            "logical_repo": "alpha-repo",
            "path": "components/alpha",
        },
        "contract": {
            "id": "alpha-contract",
            "version": "1.0.0",
            "schema_refs": ["schema:alpha"],
            "test_evidence_refs": ["evidence:contract-test"],
        },
        "independence_signals": ["independent-cadence"],
        "keep_together_reasons": [],
        "decision": decision,
        "migration": {
            "history": ["plan:history"],
            "data": ["plan:data"],
            "source_cutover": ["plan:cutover"],
            "old_path_disposition": ["plan:old-path"],
            "rollback": ["plan:rollback"],
        },
        "operational_contract": {
            "install": ["contract:install"],
            "update": ["contract:update"],
            "configure": ["contract:configure"],
            "monitor": ["contract:monitor"],
            "remove": ["contract:remove"],
        },
        "compatibility_impact": {
            "affected_assemblies": ["assembly:seed"],
            "catalogue_changes": ["change:alpha"],
        },
        "implication_acknowledgements": ["ack:alpha"],
        "upkeep_estimate_refs": ["estimate:alpha"],
        "authority": {
            "decision_ref": "ticket:T-3181",
            "reviewer_evidence_refs": ["evidence:review"],
        },
        "re_evaluate_when": ["trigger:contract-changes"],
    }


def _write_yaml(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _write_component(root: Path, data: dict[str, object]) -> Path:
    path = root / f"{data['component_id']}.yaml"
    _write_yaml(path, data)
    return path


def _write_project(repo: Path, project_id: str = "alpha-project") -> None:
    _write_yaml(repo / "projects" / "records" / f"{project_id}.yaml", _project(project_id))


def _write_generated(repo: Path) -> None:
    records = cc.load_component_records(repo / "components" / "records")
    (repo / "schemas").mkdir(parents=True, exist_ok=True)
    (repo / "components").mkdir(parents=True, exist_ok=True)
    (repo / "schemas" / "component-record.schema.json").write_bytes(
        cc.generate_component_record_schema()
    )
    (repo / "schemas" / "repository-boundary-decision.schema.json").write_bytes(
        cc.generate_repository_boundary_decision_schema()
    )
    (repo / "components" / "index.json").write_bytes(
        cc.generate_component_index(records)
    )


class ComponentCatalogueTests(unittest.TestCase):
    def test_empty_catalogue_generation_is_byte_stable_across_repeats(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "components" / "records").mkdir(parents=True)
            records = cc.load_component_records(repo / "components" / "records")
            first = cc.generate_component_index(records)
            for _ in range(5):
                self.assertEqual(cc.generate_component_index(records), first)
            index = json.loads(first)
            self.assertEqual(index["components"], {})
            _write_generated(repo)
            self.assertEqual(cc.check_component_catalogue(repo), ())

    def test_one_component_index_binds_exact_source_record_digest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _write_project(repo)
            path = _write_component(repo / "components" / "records", _record())
            records = cc.load_component_records(path.parent)
            self.assertEqual(
                cc.validate_component_graph(records, {"alpha-project"}, set()), ()
            )
            index = json.loads(cc.generate_component_index(records))
            entry = index["components"]["alpha-component"]
            self.assertEqual(entry["record"], "components/records/alpha-component.yaml")
            self.assertEqual(entry["project_ref"], "project:alpha-project")
            self.assertEqual(entry["provided_capabilities"], ["alpha-capability"])
            self.assertEqual(
                entry["record_sha256"], f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
            )

    def test_one_project_many_repos_and_shared_provider_are_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_component(root, _record(logical_repo="provider-repo"))
            for name in ("first-consumer", "second-consumer"):
                _write_component(
                    root,
                    _record(
                        name,
                        logical_repo=f"{name}-repo",
                        provides=[_provision(f"{name}-out")],
                        hard=[_requirement()],
                    ),
                )
            records = cc.load_component_records(root)
            self.assertEqual(
                cc.validate_component_graph(records, {"alpha-project"}, set()), ()
            )
            reversed_records = dict(reversed(tuple(records.items())))
            self.assertEqual(
                cc.generate_component_index(records),
                cc.generate_component_index(reversed_records),
            )

    def test_component_identity_does_not_derive_from_repo_path_or_remote(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            first = base / "first"
            second = base / "second"
            _write_component(first, _record(logical_repo="old-repo", path="old/location"))
            _write_component(second, _record(logical_repo="new-repo", path="new/location"))
            a = cc.load_component_records(first)["alpha-component"]
            b = cc.load_component_records(second)["alpha-component"]
            self.assertEqual(a.component_id, b.component_id)
            self.assertNotEqual(a.source, b.source)

    def test_unknown_project_and_mapping_identity_mismatch_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_component(root, _record())
            record = cc.load_component_records(root)["alpha-component"]
            issues = cc.validate_component_graph({"wrong-key": record}, set(), set())
            self.assertEqual(
                {issue.code for issue in issues},
                {"identity-key-mismatch", "unknown-project"},
            )

    def test_missing_ambiguous_and_self_provider_refuse(self) -> None:
        cases: list[tuple[str, list[dict[str, object]], str]] = []
        cases.append(
            (
                "missing",
                [_record("consumer", provides=[], hard=[_requirement()])],
                "unknown-provider",
            )
        )
        cases.append(
            (
                "ambiguous",
                [
                    _record("provider-one"),
                    _record("provider-two", logical_repo="provider-two-repo"),
                    _record("consumer", provides=[], hard=[_requirement()]),
                ],
                "ambiguous-provider",
            )
        )
        cases.append(
            (
                "self",
                [_record("self-component", hard=[_requirement()])],
                "self-requirement",
            )
        )
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for name, values, expected in cases:
                root = base / name
                for value in values:
                    _write_component(root, value)
                records = cc.load_component_records(root)
                codes = {
                    issue.code
                    for issue in cc.validate_component_graph(
                        records, {"alpha-project"}, set()
                    )
                }
                with self.subTest(name=name):
                    self.assertIn(expected, codes)

    def test_contract_schema_version_and_feature_match_are_separate_axes(self) -> None:
        mismatches = {
            "contract": {"contract": "wrong-contract"},
            "schema": {"schema": "wrong-schema"},
            "version": {"version": "2.0.0"},
            "feature": {"feature": "missing-feature"},
        }
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for name, change in mismatches.items():
                root = base / name
                _write_component(root, _record("provider"))
                requirement = _requirement()
                requirement.update(change)
                _write_component(
                    root,
                    _record("consumer", provides=[], hard=[requirement]),
                )
                issues = cc.validate_component_graph(
                    cc.load_component_records(root), {"alpha-project"}, set()
                )
                with self.subTest(name=name):
                    self.assertEqual({issue.code for issue in issues}, {"unknown-provider"})

    def test_capability_provision_classes_parse_and_preserve_distinct_opaque_tokens(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            provision = _provision()
            provision["equivalence_class"] = "vendor/Foo Equivalent v2"
            provision["trust_class"] = "tier:gold+human reviewed"
            data = _record(provides=[provision])
            data["deployment"]["trust_zone"] = "deployment-zone"  # type: ignore[index]
            _write_component(root, data)

            record = cc.load_component_records(root)["alpha-component"]
            parsed = record.provides[0]
            self.assertEqual(parsed.equivalence_class, "vendor/Foo Equivalent v2")
            self.assertEqual(parsed.trust_class, "tier:gold+human reviewed")
            self.assertNotEqual(parsed.equivalence_class, parsed.trust_class)
            self.assertNotEqual(parsed.equivalence_class, record.deployment.trust_zone)
            self.assertNotEqual(parsed.trust_class, record.deployment.trust_zone)

    def test_legacy_capability_provision_without_classes_remains_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _write_project(repo)
            _write_component(repo / "components" / "records", _record())

            records = cc.load_component_records(repo / "components" / "records")
            provision = records["alpha-component"].provides[0]
            self.assertIsNone(provision.equivalence_class)
            self.assertIsNone(provision.trust_class)

            _write_generated(repo)
            self.assertEqual(cc.check_component_catalogue(repo), ())

    def test_component_record_accepts_legacy_omission_and_parses_subject_visibility_when_present(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_component(root / "legacy", _record())
            legacy = cc.load_component_records(root / "legacy")["alpha-component"]
            self.assertIsNone(legacy.provides[0].subject_visibility)

            provision = _provision()
            provision["subject_visibility"] = _subject_visibility(
                subject_refs=["subject:alpha", "subject:all"],
                role_refs=["role:worker"],
                instance_refs=["instance:test"],
                trust_zones=["test-zone"],
            )
            _write_component(root / "present", _record(provides=[provision]))

            parsed = cc.load_component_records(root / "present")["alpha-component"]
            policy = parsed.provides[0].subject_visibility
            self.assertEqual(
                policy,
                cc.SubjectVisibilityPolicy(
                    subject_refs=("subject:alpha", "subject:all"),
                    role_refs=("role:worker",),
                    instance_refs=("instance:test",),
                    trust_zones=("test-zone",),
                    policy_refs=("policy:alpha-visibility",),
                    evidence_refs=("evidence:alpha-visibility",),
                ),
            )

    def test_component_record_rejects_subject_visibility_extra_missing_duplicates_bad_digest_refs(
        self,
    ) -> None:
        cases: list[tuple[str, dict[str, object]]] = []
        extra = _subject_visibility()
        extra["extra"] = "nope"
        cases.append(("extra", extra))
        missing = _subject_visibility()
        missing.pop("evidence_refs")
        cases.append(("missing", missing))
        cases.append(
            (
                "duplicate",
                _subject_visibility(subject_refs=["subject:alpha", "subject:alpha"]),
            )
        )
        cases.append(("empty-policy-ref", _subject_visibility(policy_refs=[""])))
        cases.append(("non-string-evidence-ref", _subject_visibility(evidence_refs=[7])))
        cases.append(("whitespace-role-ref", _subject_visibility(role_refs=[" role:worker "])))
        for token in sorted(cc.FORBIDDEN_SUBJECT_VISIBILITY_TOKENS):
            cases.append(
                (
                    f"forbidden-{token}",
                    _subject_visibility(subject_refs=[token]),
                )
            )
        cases.append(("empty-policy-refs", _subject_visibility(policy_refs=[])))
        cases.append(("empty-evidence-refs", _subject_visibility(evidence_refs=[])))
        cases.append(
            (
                "no-subject-dimension",
                _subject_visibility(
                    subject_refs=[],
                    role_refs=[],
                    instance_refs=[],
                    trust_zones=[],
                ),
            )
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for index, (name, policy) in enumerate(cases):
                provision = _provision()
                provision["subject_visibility"] = policy
                case_root = root / f"{index}-{name.replace('*', 'star')}"
                _write_component(case_root, _record(provides=[provision]))
                with self.subTest(name=name), self.assertRaises(
                    cc.ComponentCatalogueError
                ):
                    cc.load_component_records(case_root)

    def test_capability_provision_accepts_legacy_omission_null_and_parses_navigator_selection_when_present(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_component(root / "omitted", _record())
            omitted = cc.load_component_records(root / "omitted")["alpha-component"]
            self.assertIsNone(omitted.provides[0].navigator_provider_selection)

            explicit_null = _provision()
            explicit_null["navigator_provider_selection"] = None
            _write_component(root / "null", _record(provides=[explicit_null]))
            nullable = cc.load_component_records(root / "null")["alpha-component"]
            self.assertIsNone(nullable.provides[0].navigator_provider_selection)

            primary = _provision()
            primary["navigator_provider_selection"] = _navigator_selection()
            _write_component(root / "primary", _record(provides=[primary]))
            parsed_primary = (
                cc.load_component_records(root / "primary")["alpha-component"]
                .provides[0]
                .navigator_provider_selection
            )
            self.assertEqual(
                parsed_primary,
                cc.NavigatorProviderSelection(
                    role=cc.NavigatorProviderRole.PRIMARY,
                    primary_provider_component_id=None,
                    fallback_rank=None,
                    provider_organ_id="organ:alpha-provider",
                    health_dependency_id="dependency:alpha-provider-health",
                ),
            )

            fallback = _provision()
            fallback["navigator_provider_selection"] = _navigator_selection(
                "fallback",
                primary_provider_component_id="primary-component",
                fallback_rank=1,
                provider_organ_id="organ:fallback-provider",
                health_dependency_id="dependency:fallback-health",
            )
            _write_component(root / "fallback", _record(provides=[fallback]))
            parsed_fallback = (
                cc.load_component_records(root / "fallback")["alpha-component"]
                .provides[0]
                .navigator_provider_selection
            )
            self.assertEqual(parsed_fallback.role, cc.NavigatorProviderRole.FALLBACK)  # type: ignore[union-attr]
            self.assertEqual(parsed_fallback.primary_provider_component_id, "primary-component")  # type: ignore[union-attr]
            self.assertEqual(parsed_fallback.fallback_rank, 1)  # type: ignore[union-attr]
            self.assertEqual(parsed_fallback.provider_organ_id, "organ:fallback-provider")  # type: ignore[union-attr]
            self.assertEqual(parsed_fallback.health_dependency_id, "dependency:fallback-health")  # type: ignore[union-attr]

    def test_capability_provision_rejects_navigator_selection_extra_missing_role_field_mismatch_and_bad_refs(
        self,
    ) -> None:
        cases: list[tuple[str, dict[str, object]]] = []
        extra = _navigator_selection()
        extra["extra"] = "nope"
        cases.append(("extra", extra))
        missing = _navigator_selection()
        missing.pop("role")
        cases.append(("missing", missing))
        cases.append(("bad-role", _navigator_selection("standby")))
        cases.append(
            (
                "primary-target",
                _navigator_selection(primary_provider_component_id="primary-component"),
            )
        )
        cases.append(("primary-rank", _navigator_selection(fallback_rank=1)))
        cases.append(("fallback-null-target", _navigator_selection("fallback", fallback_rank=1)))
        cases.append(
            (
                "fallback-rank-zero",
                _navigator_selection(
                    "fallback",
                    primary_provider_component_id="primary-component",
                    fallback_rank=0,
                ),
            )
        )
        bad_bool_rank = _navigator_selection(
            "fallback",
            primary_provider_component_id="primary-component",
            fallback_rank=1,
        )
        bad_bool_rank["fallback_rank"] = True
        cases.append(("fallback-bool-rank", bad_bool_rank))
        cases.append(
            (
                "bad-component",
                _navigator_selection(
                    "fallback",
                    primary_provider_component_id="Primary_Component",
                    fallback_rank=1,
                ),
            )
        )
        cases.append(("bad-organ", _navigator_selection(provider_organ_id="alpha-provider")))
        cases.append(
            (
                "organ-slash",
                _navigator_selection(provider_organ_id="organ:alpha/provider"),
            )
        )
        cases.append(
            (
                "organ-space",
                _navigator_selection(provider_organ_id="organ:alpha provider"),
            )
        )
        cases.append(
            (
                "organ-double-colon",
                _navigator_selection(provider_organ_id="organ:alpha:provider"),
            )
        )
        cases.append(
            (
                "organ-case",
                _navigator_selection(provider_organ_id="organ:Alpha-provider"),
            )
        )
        cases.append(
            (
                "bad-dependency",
                _navigator_selection(health_dependency_id="health:alpha-provider"),
            )
        )
        cases.append(
            (
                "dependency-slash",
                _navigator_selection(
                    health_dependency_id="dependency:alpha/provider"
                ),
            )
        )
        cases.append(
            (
                "dependency-space",
                _navigator_selection(
                    health_dependency_id="dependency:alpha provider"
                ),
            )
        )
        cases.append(
            (
                "dependency-double-colon",
                _navigator_selection(
                    health_dependency_id="dependency:alpha:provider"
                ),
            )
        )
        cases.append(
            (
                "dependency-case",
                _navigator_selection(
                    health_dependency_id="dependency:Alpha-provider"
                ),
            )
        )

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name, selection in cases:
                provision = _provision()
                provision["navigator_provider_selection"] = selection
                case_root = root / name
                _write_component(case_root, _record(provides=[provision]))
                with self.subTest(name=name), self.assertRaises(
                    cc.ComponentCatalogueError
                ):
                    cc.load_component_records(case_root)

    def test_capability_provision_class_fields_accept_null_and_refuse_non_strings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accepted = _provision()
            accepted["equivalence_class"] = None
            accepted["trust_class"] = None
            _write_component(root / "null", _record(provides=[accepted]))
            parsed = cc.load_component_records(root / "null")["alpha-component"].provides[0]
            self.assertIsNone(parsed.equivalence_class)
            self.assertIsNone(parsed.trust_class)

            unexpected = _provision()
            unexpected["unexpected_class_authority"] = "class:alpha"
            _write_component(root / "unknown", _record(provides=[unexpected]))
            with self.assertRaisesRegex(cc.ComponentCatalogueError, "unknown"):
                cc.load_component_records(root / "unknown")

            invalid_values: tuple[object, ...] = (
                "",
                " whitespace ",
                True,
                7,
                ["class:alpha"],
                {"class": "alpha"},
            )
            for field in ("equivalence_class", "trust_class"):
                for value in invalid_values:
                    provision = _provision()
                    provision[field] = value
                    case = root / f"{field}-{type(value).__name__}-{len(str(value))}"
                    _write_component(case, _record(provides=[provision]))
                    with self.subTest(field=field, value=value), self.assertRaises(
                        cc.ComponentCatalogueError
                    ):
                        cc.load_component_records(case)

    def test_generated_component_schema_keeps_class_fields_optional_nullable_and_closed(
        self,
    ) -> None:
        schema = json.loads(cc.generate_component_record_schema())
        provision = schema["properties"]["provides"]["items"]
        nullable_text = {
            "anyOf": [{"type": "null"}, {"type": "string", "minLength": 1}]
        }

        self.assertEqual(
            provision["properties"]["equivalence_class"], nullable_text
        )
        self.assertEqual(provision["properties"]["trust_class"], nullable_text)
        self.assertNotIn("equivalence_class", provision["required"])
        self.assertNotIn("trust_class", provision["required"])
        self.assertIs(provision["additionalProperties"], False)
        self.assertEqual(schema["properties"]["schema_version"]["const"], cc.SCHEMA_VERSION)
        self.assertEqual(cc.SCHEMA_VERSION, 1)
        self.assertEqual(
            (REPO_ROOT / "schemas" / "component-record.schema.json").read_bytes(),
            cc.generate_component_record_schema(),
        )

    def test_generated_component_schema_keeps_navigator_selection_optional_nullable_closed_and_v1(
        self,
    ) -> None:
        schema = json.loads(cc.generate_component_record_schema())
        provision = schema["properties"]["provides"]["items"]
        selection = provision["properties"]["navigator_provider_selection"]
        selection_object = selection["anyOf"][1]

        self.assertNotIn("navigator_provider_selection", provision["required"])
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
            {"type": "string", "pattern": cc.STABLE_ID_PATTERN},
        )
        self.assertEqual(
            fallback_conditional["then"]["properties"]["fallback_rank"],
            {"type": "integer", "minimum": 1},
        )
        self.assertEqual(schema["properties"]["schema_version"]["const"], cc.SCHEMA_VERSION)
        self.assertEqual(cc.SCHEMA_VERSION, 1)
        self.assertEqual(
            (REPO_ROOT / "schemas" / "component-record.schema.json").read_bytes(),
            cc.generate_component_record_schema(),
        )

    def test_optional_disabled_declares_degradation_and_creates_no_edge(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_component(
                root,
                _record(
                    optional=[
                        _requirement(
                            "absent-capability",
                            enabled=False,
                            degradation_ref="contract:degraded-mode",
                        )
                    ]
                ),
            )
            records = cc.load_component_records(root)
            self.assertEqual(
                cc.validate_component_graph(records, {"alpha-project"}, set()), ()
            )

            invalid = _record("invalid-optional")
            invalid["requires"]["optional"] = [  # type: ignore[index]
                _requirement("absent-capability", enabled=False)
            ]
            _write_component(root, invalid)
            with self.assertRaisesRegex(cc.ComponentCatalogueError, "degradation_ref"):
                cc.load_component_records(root)

    def test_optional_enabled_resolves_like_hard_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_component(
                root,
                _record(
                    provides=[],
                    optional=[_requirement("absent-capability", enabled=True)],
                ),
            )
            issues = cc.validate_component_graph(
                cc.load_component_records(root), {"alpha-project"}, set()
            )
            self.assertEqual({issue.code for issue in issues}, {"unknown-provider"})

    def test_dependency_cycle_refuses(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_component(
                root,
                _record(
                    "alpha-component",
                    provides=[_provision("alpha-capability")],
                    hard=[_requirement("beta-capability")],
                ),
            )
            _write_component(
                root,
                _record(
                    "beta-component",
                    logical_repo="beta-repo",
                    provides=[_provision("beta-capability")],
                    hard=[_requirement("alpha-capability")],
                ),
            )
            issues = cc.validate_component_graph(
                cc.load_component_records(root), {"alpha-project"}, set()
            )
            self.assertEqual({issue.code for issue in issues}, {"dependency-cycle"})

    def test_present_component_capability_and_feature_conflicts_refuse(self) -> None:
        conflicts = (
            {"kind": "component", "target": "provider", "reason_ref": "decision:no-provider"},
            {
                "kind": "capability",
                "target": "alpha-capability",
                "reason_ref": "decision:no-capability",
            },
            {"kind": "feature", "target": "camera", "reason_ref": "decision:no-camera"},
        )
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for number, conflict in enumerate(conflicts):
                root = base / str(number)
                _write_component(
                    root,
                    _record(
                        "provider",
                        provides=[_provision(features=["camera"])],
                    ),
                )
                consumer = _record("consumer", provides=[])
                consumer["compatibility"]["conflicts"] = [conflict]  # type: ignore[index]
                _write_component(root, consumer)
                issues = cc.validate_component_graph(
                    cc.load_component_records(root), {"alpha-project"}, set()
                )
                with self.subTest(number=number):
                    self.assertIn("active-conflict", {issue.code for issue in issues})

    def test_acceptance_predicate_is_closed_and_typed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = _record()
            data["compatibility"]["accepts"] = [  # type: ignore[index]
                {
                    "capability": "alpha-capability",
                    "contract": "alpha-contract",
                    "schema": "alpha-schema",
                    "version": "1.0.0",
                }
            ]
            _write_component(root, data)
            self.assertEqual(
                cc.load_component_records(root)["alpha-component"]
                .compatibility.accepts[0]
                .schema,
                "alpha-schema",
            )
            data["compatibility"]["accepts"][0]["range"] = ">=1"  # type: ignore[index]
            _write_component(root, data)
            with self.assertRaisesRegex(cc.ComponentCatalogueError, "unknown"):
                cc.load_component_records(root)

    def test_raw_remote_and_unsafe_source_paths_refuse(self) -> None:
        cases = (
            ("https", {"logical_repo": "https://example.test/repo"}),
            ("ssh", {"logical_repo": "ssh://host/repo"}),
            ("scp", {"logical_repo": "user@host:repo"}),
            ("absolute", {"path": "/absolute/path"}),
            ("parent", {"path": "safe/../escape"}),
            ("dot", {"path": "safe/./path"}),
            ("backslash", {"path": r"safe\escape"}),
            ("empty-segment", {"path": "safe//path"}),
        )
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for name, change in cases:
                data = _record()
                data["source"].update(change)  # type: ignore[union-attr]
                root = base / name
                _write_component(root, data)
                with self.subTest(name=name), self.assertRaises(
                    cc.ComponentCatalogueError
                ):
                    cc.load_component_records(root)

    def test_strict_yaml_and_file_authority_refuse_unsafe_records(self) -> None:
        base = yaml.safe_dump(_record(), sort_keys=False)
        cases = {
            "duplicate": base + "component_id: duplicate\n",
            "anchor": base.replace("kind: service", "kind: &kind service").replace(
                "trust_zone: test-zone", "trust_zone: *kind"
            ),
            "tag": base.replace("kind: service", "kind: !!str service"),
            "merge": base.replace(
                "source:\n", "source:\n  <<: {logical_repo: bad, path: null, artifact: null}\n", 1
            ),
            "unknown": base + "telos: must-not-copy-project-authority\n",
        }
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name, text in cases.items():
                case = root / name
                case.mkdir()
                (case / "alpha-component.yaml").write_text(text, encoding="utf-8")
                with self.subTest(name=name), self.assertRaises(
                    cc.ComponentCatalogueError
                ):
                    cc.load_component_records(case)
            symlinks = root / "symlink"
            symlinks.mkdir()
            target = root / "target.yaml"
            target.write_text(base, encoding="utf-8")
            (symlinks / "alpha-component.yaml").symlink_to(target)
            with self.assertRaisesRegex(cc.ComponentCatalogueError, "symlink"):
                cc.load_component_records(symlinks)
            oversized = root / "oversized"
            oversized.mkdir()
            (oversized / "alpha-component.yaml").write_bytes(
                b"x" * (cc.MAX_RECORD_BYTES + 1)
            )
            with self.assertRaisesRegex(cc.ComponentCatalogueError, "exceeds"):
                cc.load_component_records(oversized)

    def test_implication_shape_uniqueness_and_evidence_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cases: list[dict[str, object]] = []
            duplicate = _record()
            duplicate["implications"]["authority"] = [  # type: ignore[index]
                _implication(),
                _implication(),
            ]
            cases.append(duplicate)
            empty_evidence = _record()
            item = _implication()
            item["evidence_refs"] = []
            empty_evidence["implications"]["authority"] = [item]  # type: ignore[index]
            cases.append(empty_evidence)
            wrong_scope = _record()
            item = _implication()
            item["scope"] = ""
            wrong_scope["implications"]["authority"] = [item]  # type: ignore[index]
            cases.append(wrong_scope)
            for number, data in enumerate(cases):
                root = base / str(number)
                _write_component(root, data)
                with self.subTest(number=number), self.assertRaises(
                    cc.ComponentCatalogueError
                ):
                    cc.load_component_records(root)

    def test_cost_owner_measurement_and_timestamps_are_strict(self) -> None:
        changes = (
            ("negative", {"quantity": -1}),
            ("nan", {"quantity": float("nan")}),
            ("owner", {"owner": ""}),
            ("measurement", {"measurement_source": ""}),
            ("timestamp", {"last_measured_at": "2026-08-26"}),
            ("due", {"review_due_at": "never"}),
        )
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            for name, change in changes:
                data = _record()
                cost = _cost()
                cost.update(change)
                data["upkeep"]["cost"] = [cost]  # type: ignore[index]
                root = base / name
                _write_component(root, data)
                with self.subTest(name=name), self.assertRaises(
                    cc.ComponentCatalogueError
                ):
                    cc.load_component_records(root)

    def test_unknown_org061_obligation_and_duplicate_cost_id_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = _record()
            data["upkeep"]["review_obligations"] = ["ORG-055:R1"]  # type: ignore[index]
            data["upkeep"]["cost"] = [_cost(), _cost()]  # type: ignore[index]
            _write_component(root, data)
            records = cc.load_component_records(root)
            issues = cc.validate_component_graph(records, {"alpha-project"}, set())
            self.assertEqual(
                {issue.code for issue in issues},
                {"duplicate-cost-id", "unknown-obligation"},
            )

    def test_boundary_keep_together_and_extract_are_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            extract = root / "extract.yaml"
            _write_yaml(extract, _decision())
            parsed = cc.parse_repository_boundary_decision(
                extract,
                project_ids={"alpha-project"},
                component_ids={"alpha-component"},
            )
            self.assertEqual(parsed.decision.value, "extract")

            keep = _decision("keep-together")
            keep["independence_signals"] = []
            keep["keep_together_reasons"] = ["rapid-interface-churn"]
            keep["migration"] = {key: [] for key in keep["migration"]}  # type: ignore[union-attr]
            path = root / "keep.yaml"
            _write_yaml(path, keep)
            parsed = cc.parse_repository_boundary_decision(
                path,
                project_ids={"alpha-project"},
                component_ids={"alpha-component"},
            )
            self.assertEqual(parsed.decision.value, "keep-together")

    def test_boundary_extract_requires_every_gate(self) -> None:
        cases: list[tuple[str, tuple[str, ...]]] = [
            ("contract", ("contract", "test_evidence_refs")),
            ("schema", ("contract", "schema_refs")),
            ("signal", ("independence_signals",)),
            ("migration", ("migration", "rollback")),
            ("operation", ("operational_contract", "monitor")),
            ("authority", ("authority", "reviewer_evidence_refs")),
            ("upkeep", ("upkeep_estimate_refs",)),
            ("reevaluate", ("re_evaluate_when",)),
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name, path_parts in cases:
                data = _decision()
                target: object = data
                for part in path_parts[:-1]:
                    target = target[part]  # type: ignore[index]
                target[path_parts[-1]] = []  # type: ignore[index]
                path = root / f"{name}.yaml"
                _write_yaml(path, data)
                with self.subTest(name=name), self.assertRaises(
                    cc.ComponentCatalogueError
                ):
                    cc.parse_repository_boundary_decision(
                        path,
                        project_ids={"alpha-project"},
                        component_ids={"alpha-component"},
                    )

    def test_boundary_weak_signal_unknown_identity_and_unsafe_source_refuse(self) -> None:
        changes = (
            ("weak", ("independence_signals", ["directory-size"])),
            ("project", ("project_id", "unknown-project")),
            ("component", ("component_id", "unknown-component")),
            ("url", ("current_source", {"logical_repo": "https://bad", "path": "safe"})),
            ("path", ("current_source", {"logical_repo": "alpha-repo", "path": "../bad"})),
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name, (key, value) in changes:
                data = _decision()
                data[key] = value
                path = root / f"{name}.yaml"
                _write_yaml(path, data)
                with self.subTest(name=name), self.assertRaises(
                    cc.ComponentCatalogueError
                ):
                    cc.parse_repository_boundary_decision(
                        path,
                        project_ids={"alpha-project"},
                        component_ids={"alpha-component"},
                    )

    def test_generated_schemas_match_closed_typed_contracts(self) -> None:
        component = json.loads(cc.generate_component_record_schema())
        boundary = json.loads(cc.generate_repository_boundary_decision_schema())
        self.assertEqual(set(component["required"]), set(cc.COMPONENT_RECORD_KEYS))
        self.assertEqual(set(boundary["required"]), set(cc.BOUNDARY_DECISION_KEYS))

        def assert_closed(value: object) -> None:
            if isinstance(value, dict):
                if value.get("type") == "object":
                    self.assertIs(value.get("additionalProperties"), False)
                for child in value.values():
                    assert_closed(child)
            elif isinstance(value, list):
                for child in value:
                    assert_closed(child)

        assert_closed(component)
        assert_closed(boundary)

    def test_generated_schema_and_index_tamper_refuse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _write_project(repo)
            _write_component(repo / "components" / "records", _record())
            _write_generated(repo)
            self.assertEqual(cc.check_component_catalogue(repo), ())
            index = repo / "components" / "index.json"
            index.write_bytes(index.read_bytes().replace(b'"schema_version": 1', b'"schema_version": 9'))
            self.assertEqual(
                {issue.code for issue in cc.check_component_catalogue(repo)},
                {"index-stale"},
            )
            _write_generated(repo)
            schema = repo / "schemas" / "component-record.schema.json"
            schema.write_bytes(schema.read_bytes() + b" ")
            self.assertEqual(
                {issue.code for issue in cc.check_component_catalogue(repo)},
                {"schema-stale"},
            )

    def test_cli_generate_then_check_real_temp_tree(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            _write_project(repo)
            _write_component(repo / "components" / "records", _record())
            self.assertEqual(cc.main(["generate", "--repo-root", str(repo)]), 0)
            self.assertEqual(cc.main(["check", "--repo-root", str(repo)]), 0)
            self.assertTrue((repo / "components" / "index.json").is_file())
            for path in (
                repo / "components" / "index.json",
                repo / "schemas" / "component-record.schema.json",
                repo / "schemas" / "repository-boundary-decision.schema.json",
            ):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_live_component_catalogue_is_valid_and_empty(self) -> None:
        self.assertEqual(cc.check_component_catalogue(REPO_ROOT), ())
        records = cc.load_component_records(REPO_ROOT / "components" / "records")
        self.assertEqual(records, {})

    def test_ci_paths_cover_component_records_and_schemas(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        # T-5001: ci.yml has no push trigger (the nightly covers main); only the PR list.
        _, pull_request = workflow.split("  pull_request:", 1)
        for section in (pull_request,):
            self.assertIn("'components/**'", section)
            self.assertIn("'schemas/component-record.schema.json'", section)
            self.assertIn(
                "'schemas/repository-boundary-decision.schema.json'", section
            )


if __name__ == "__main__":
    unittest.main()
