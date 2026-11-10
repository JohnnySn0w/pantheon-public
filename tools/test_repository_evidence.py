"""Tests for the ORG-083 repository evidence validator."""

from __future__ import annotations

import base64
import copy
import ast
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def _sha(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


ANNOTATION_SCHEMA_KEYWORDS = frozenset({"$id", "$schema", "title"})
SUPPORTED_SCHEMA_KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "additionalProperties",
        "allOf",
        "const",
        "enum",
        "if",
        "items",
        "minItems",
        "minLength",
        "oneOf",
        "properties",
        "required",
        "then",
        "type",
        *ANNOTATION_SCHEMA_KEYWORDS,
    }
)


def _assert_supported_schema_keywords(schema: object, *, in_properties: bool = False, in_defs: bool = False) -> None:
    if isinstance(schema, dict):
        for key, value in schema.items():
            if in_properties or in_defs:
                _assert_supported_schema_keywords(value)
                continue
            if key not in SUPPORTED_SCHEMA_KEYWORDS:
                raise AssertionError(f"unsupported schema keyword {key}")
            _assert_supported_schema_keywords(value, in_properties=key == "properties", in_defs=key == "$defs")
    elif isinstance(schema, list):
        for item in schema:
            _assert_supported_schema_keywords(item)


def _resolve_local_ref(root_schema: dict, ref: str, seen: tuple[str, ...] = ()) -> dict:
    if not ref.startswith("#/$defs/"):
        raise AssertionError(f"unsupported ref {ref}")
    if ref in seen:
        raise AssertionError(f"cyclic ref {ref}")
    name = ref.removeprefix("#/$defs/")
    target = root_schema.get("$defs", {}).get(name)
    if not isinstance(target, dict):
        raise AssertionError(f"unresolved ref {ref}")
    return target


def _json_type_matches(expected: str, value: object) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return type(value) is bool
    if expected == "integer":
        return type(value) is int
    if expected == "number":
        return type(value) in (int, float)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    raise AssertionError(f"unsupported type {expected}")


def _schema_subset_errors(schema: dict, instance: object, *, root_schema: dict, path: str = "$", seen: tuple[str, ...] = ()) -> list[str]:
    _assert_supported_schema_keywords(schema)
    if "$ref" in schema:
        target = _resolve_local_ref(root_schema, schema["$ref"], seen)
        return _schema_subset_errors(target, instance, root_schema=root_schema, path=path, seen=(*seen, schema["$ref"]))
    errors: list[str] = []
    if "type" in schema:
        expected = schema["type"]
        choices = expected if isinstance(expected, list) else [expected]
        if not any(isinstance(choice, str) and _json_type_matches(choice, instance) for choice in choices):
            errors.append(f"{path}: type")
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: const")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: enum")
    if isinstance(instance, str) and "minLength" in schema and len(instance) < schema["minLength"]:
        errors.append(f"{path}: minLength")
    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: minItems")
        if "items" in schema:
            for offset, item in enumerate(instance):
                errors.extend(_schema_subset_errors(schema["items"], item, root_schema=root_schema, path=f"{path}/{offset}", seen=seen))
    if isinstance(instance, dict):
        required = schema.get("required", [])
        for field in required:
            if field not in instance:
                errors.append(f"{path}/{field}: required")
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for field, subschema in properties.items():
                if field in instance:
                    errors.extend(_schema_subset_errors(subschema, instance[field], root_schema=root_schema, path=f"{path}/{field}", seen=seen))
            if schema.get("additionalProperties") is False:
                for field in set(instance) - set(properties):
                    errors.append(f"{path}/{field}: additionalProperties")
            elif "additionalProperties" in schema:
                raise AssertionError("unsupported additionalProperties value")
    if "oneOf" in schema:
        matches = [
            not _schema_subset_errors(branch, instance, root_schema=root_schema, path=path, seen=seen)
            for branch in schema["oneOf"]
        ]
        if sum(matches) != 1:
            errors.append(f"{path}: oneOf")
    if "allOf" in schema:
        for branch in schema["allOf"]:
            errors.extend(_schema_subset_errors(branch, instance, root_schema=root_schema, path=path, seen=seen))
    if "if" in schema:
        if not _schema_subset_errors(schema["if"], instance, root_schema=root_schema, path=path, seen=seen):
            errors.extend(_schema_subset_errors(schema.get("then", {}), instance, root_schema=root_schema, path=path, seen=seen))
    return errors


def _assert_schema_rejects(schema: dict, instance: object, root_schema: dict) -> None:
    if not _schema_subset_errors(schema, instance, root_schema=root_schema):
        raise AssertionError(f"schema accepted forbidden instance {instance!r}")


def _literal_emitted_issue_codes(source_path: Path) -> tuple[set[str], list[tuple[int, str]]]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    codes: set[str] = set()
    dynamic: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_issue":
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                codes.add(node.args[0].value)
            else:
                dynamic.append((node.lineno, ast.unparse(node.args[0]) if node.args else "<missing>"))
    return codes, dynamic


def _artifact(artifact_id: str, content: bytes, *, artifact_class: str = "proof") -> dict:
    return {
        "artifact_id": artifact_id,
        "repository": "pantheon-config",
        "revision": "4d58f73d7c6a1e880a4baaefdbc5a125abaaac7f",
        "artifact_class": artifact_class,
        "created_at": "2026-09-07T00:00:00Z",
        "producer": "fixture",
        "tool": "normalized-input",
        "config_identity": "none",
        "storage_locator": {
            "kind": "inline",
            "value": base64.b64encode(content).decode("ascii"),
        },
        "media_type": "text/plain",
        "digest_algorithm": "sha256",
        "digest": _sha(content),
        "size": len(content),
        "sensitivity": "internal",
        "access": "repo",
        "tombstone": None,
        "payload": None,
    }


def _positive_effectiveness_waiver() -> dict:
    return {
        "waiver_id": "waiver-positive-effectiveness",
        "repository": "pantheon-config",
        "revision": "4d58f73d7c6a1e880a4baaefdbc5a125abaaac7f",
        "owner": "orchestrator",
        "reviewer": "recovery",
        "approver": "orchestrator",
        "reason": "fixture narrow waiver",
        "scope": {
            "obligation": "positive_effectiveness",
            "include_selectors": ["tools/"],
            "scope_fingerprint": "positive-effectiveness-tools",
        },
        "started_at": "2026-09-07T00:00:00Z",
        "expiry": {"kind": "timestamp", "value": "2026-12-31T00:00:00Z", "action": "fail_closed"},
        "restoration_condition": "positive receipt restored",
        "restoration_plan": "collect independent effectiveness receipt",
        "compensating_evidence_ids": ["art-pending"],
        "decision_ref": "T-3725",
        "active_state": _active_state(),
        "expiry_action": "fail_closed",
        "authority_decision": _authority_decision(
            "decision-waiver-authority",
            record_id="waiver-positive-effectiveness",
            obligation="positive_effectiveness",
            role="orchestrator",
        ),
        "independent_review_decision": _authority_decision(
            "decision-waiver-independent",
            record_id="waiver-positive-effectiveness",
            obligation="positive_effectiveness",
            role="second-head",
        ),
        "obligation": "positive_effectiveness",
        "fallback": "fail_closed",
    }


def _positive_effectiveness_receipt() -> dict:
    return {
        "effectiveness_receipt_id": "eff-positive-boundary",
        "repository": "pantheon-config",
        "revision": "4d58f73d7c6a1e880a4baaefdbc5a125abaaac7f",
        "kind": "real_boundary",
        "reviewed_at": "2026-09-07T00:00:00Z",
        "reviewer": "recovery",
        "scope": {"selectors": ["tools/repository_evidence.py"]},
        "limitations": "fixture positive receipt",
        "payload": {
            "producer": "fixture",
            "transport": "call",
            "consumer": "validator",
            "real_components": ["validator"],
            "simulated_components": [],
            "reached_preconditions": ["input parsed"],
            "asserted_delivered_effect": "semantic issue emitted",
            "boundary_record_id": "boundary-shell_suite",
        },
        "review_decision": _authority_decision("decision-positive-review", kind="repository", role="recovery"),
        "independent_review_decision": _authority_decision("decision-positive-independent", kind="repository", role="second-head"),
    }


def _active_state() -> dict:
    return {
        "status": "active",
        "evaluated_at": "2026-09-07T00:00:00Z",
        "evidence_id": "art-pending",
    }


def _expiry() -> dict:
    return {"kind": "timestamp", "value": "2026-12-31T00:00:00Z", "action": "fleet_drift"}


def _authority_decision(
    decision_id: str,
    *,
    kind: str = "obligation",
    record_id: str = "def-inventory",
    role: str = "orchestrator",
    dimension: str = "line",
    boundary_kind: str = "shell_suite",
    measurement_id: str = "measure-line",
    obligation: str = "maturity-deferral",
    mechanism: str = "mutation-gate",
) -> dict:
    revision = "4d58f73d7c6a1e880a4baaefdbc5a125abaaac7f"
    scopes = {
        "repository": {"kind": "repository", "repository": "pantheon-config"},
        "dimension": {
            "kind": "dimension",
            "repository": "pantheon-config",
            "revision": revision,
            "dimension": dimension,
            "selectors": ["tools/"],
        },
        "boundary": {
            "kind": "boundary",
            "repository": "pantheon-config",
            "revision": revision,
            "boundary_kind": boundary_kind,
            "selectors": ["tools/"],
        },
        "measurement": {
            "kind": "measurement",
            "repository": "pantheon-config",
            "revision": revision,
            "measurement_id": measurement_id,
            "dimension": dimension,
        },
        "obligation": {
            "kind": "obligation",
            "repository": "pantheon-config",
            "revision": revision,
            "obligation": obligation,
            "record_id": record_id,
        },
        "enforcement": {
            "kind": "enforcement",
            "repository": "pantheon-config",
            "revision": revision,
            "governed_scope": ["tools/"],
            "mechanism": mechanism,
        },
    }
    return {
        "decision_id": decision_id,
        "role": role,
        "decision_ref": "tickets/active/T-3380.md",
        "decided_at": "2026-09-07T00:00:00Z",
        "scope": scopes[kind],
        "artifact_id": "art-pending",
    }


def _pilot_costs() -> dict:
    return {
        cost_class: {
            "state": "unavailable",
            "reason": "pilot shape only",
            "amount": None,
            "unit": None,
            "artifact_id": None,
            "authority_decision": None,
        }
        for cost_class in (
            "local_setup",
            "local_runtime",
            "ci_queue",
            "ci_runner",
            "rerun",
            "mutation",
            "cpu",
            "monetary",
        )
    }


SEM_REVISION = "4d58f73d7c6a1e880a4baaefdbc5a125abaaac7f"
SEM_MAP_NAME = "semantic-map.resolved.json"
# Run-level (not record-bound) map instances; the 10 collector/scope keys are taken from the record.
SEM_RUN_LEVEL_DEFAULTS = {
    "dimension": ["branch", "line"],
    "unit": {"branch": "percent", "line": "percent"},
    "denominator": {"line": "totals.num_statements", "branch": "totals.num_branches"},
    "branch_semantics": {"effective_core": "ctrace", "has_arcs": True},
    "function_semantics": "deferred",
    "test_selection": {"argv_selection": ["tools/"], "extra_args": [], "narrowing_flags_present": False},
}


def _sem_json(obj: object) -> bytes:
    return (json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sem_bundle(
    prefix: str,
    record: dict,
    *,
    declarations: dict | None = None,
    instances: dict | None = None,
    map_mutator=None,
    manifest_mutator=None,
    manifest_revision: str | None = None,
    manifest_adapter_id: str | None = None,
    map_adapter_id: str | None = None,
) -> tuple[str, list[dict]]:
    """Real digest-verified run manifest + resolved map (adapter formats) as inline ArtifactRecords."""
    mod = importlib.import_module("tools.repository_evidence")
    adapter = record["collector"]["adapter_id"]
    values: dict = {}
    for key in mod.COMPARABILITY_CHECKED_FIELDS:
        if key.startswith("collector."):
            values[key] = record["collector"][key.split(".", 1)[1]]
        elif key.startswith("scope."):
            values[key] = record["scope"][key.split(".", 1)[1]]
        else:
            values[key] = SEM_RUN_LEVEL_DEFAULTS[key]
    values = copy.deepcopy(values)
    values.update(copy.deepcopy(instances or {}))
    compared = {
        key: {"declaration": (declarations or {}).get(key, f"declaration of {key}"), "instance": value}
        for key, value in values.items()
    }
    map_obj = {
        "schema": "org083.adapter-resolved-map.v1",
        "adapter_id": map_adapter_id or adapter,
        "static_map_sha256": "0" * 64,
        "compared": compared,
        "observations": {},
    }
    if map_mutator is not None:
        map_mutator(map_obj)
    map_bytes = _sem_json(map_obj)
    map_id, manifest_id = f"{prefix}-map", f"{prefix}-manifest"
    manifest_obj = {
        "schema": "org083.adapter-run-manifest.v1",
        "adapter_id": manifest_adapter_id or adapter,
        "revision": manifest_revision or record["revision"],
        "members": [{"name": SEM_MAP_NAME, "artifact_id": map_id, "sha256": _sha(map_bytes), "size": len(map_bytes)}],
    }
    if manifest_mutator is not None:
        manifest_mutator(manifest_obj)
    return manifest_id, [
        _artifact(manifest_id, _sem_json(manifest_obj)),
        _artifact(map_id, map_bytes),
    ]


def _set_artifact_bytes(artifact: dict, content: bytes, *, refresh: bool = True) -> None:
    artifact["storage_locator"] = {"kind": "inline", "value": base64.b64encode(content).decode("ascii")}
    if refresh:
        artifact["digest"] = _sha(content)
        artifact["size"] = len(content)


def _retention_for(contract: dict, artifact: dict) -> dict:
    retention = copy.deepcopy(contract["retention_records"][0])
    aid = artifact["artifact_id"]
    content = base64.b64decode(artifact["storage_locator"]["value"])
    retention.update(retention_id=f"ret-{aid}", artifact_id=aid, storage_locator=artifact["storage_locator"], digest=artifact["digest"])
    receipt = retention["retrieval_receipts"][0]
    receipt.update(receipt_id=f"receipt-{aid}", artifact_id=aid, observed_digest=artifact["digest"], observed_size=len(content))
    retention["availability"]["retrieval_receipt_id"] = f"receipt-{aid}"
    return retention


def _attach_semantics(contract: dict, record: dict, *, retain: bool = False, **bundle_kwargs) -> list[dict]:
    """Attach (or replace) the record's retained semantic evidence; points record.artifact_id at the manifest."""
    manifest_id, artifacts = _sem_bundle(f"sem-{record['measurement_id']}", record, **bundle_kwargs)
    ids = {artifact["artifact_id"] for artifact in artifacts}
    contract["artifact_records"] = [a for a in contract["artifact_records"] if a["artifact_id"] not in ids] + artifacts
    record["artifact_id"] = manifest_id
    if retain:
        contract["retention_records"] = [r for r in contract["retention_records"] if r["artifact_id"] != manifest_id]
        contract["retention_records"].append(_retention_for(contract, artifacts[0]))
    return artifacts


def _measurement(measurement_id: str, dimension: str, *, artifact_id: str = "art-pending", changed_fields: list[str] | None = None) -> dict:
    return {
        "measurement_id": measurement_id,
        "repository": "pantheon-config",
        "revision": "4d58f73d7c6a1e880a4baaefdbc5a125abaaac7f",
        "collected_at": "2026-09-07T00:00:00Z",
        "dimension": dimension,
        "unit": "percent",
        "collector": {
            "adapter_id": "fixture",
            "tool_name": "coverage",
            "tool_version": "1",
            "command_fingerprint": "fixture",
            "config_digest": "fixture",
            "runner_fingerprint": "fixture",
        },
        "scope": {
            "include_selectors": ["tools/"],
            "exclusion_ids": [],
            "generated_source_ids": [],
            "scope_fingerprint": "fixture",
        },
        "numerator": 8,
        "denominator": 10,
        "value": 0.8,
        "baseline": {"measurement_id": measurement_id, "numerator": 8, "denominator": 10, "value": 0.8},
        "current": {"measurement_id": measurement_id, "numerator": 8, "denominator": 10, "value": 0.8},
        "threshold": {"state": "unset", "comparator": "gte", "value": None, "tolerance": 0.0},
        "ratchet": {"state": "unset", "direction": "nondecreasing", "proposed": None, "adopted": None, "tolerance": 0.0},
        "comparability": {
            "verdict": "comparable",
            "baseline_measurement_id": measurement_id,
            "current_measurement_id": measurement_id,
            "checked_fields": [
                "collector.adapter_id",
                "collector.tool_name",
                "collector.tool_version",
                "collector.command_fingerprint",
                "collector.config_digest",
                "collector.runner_fingerprint",
                "scope.include_selectors",
                "scope.exclusion_ids",
                "scope.generated_source_ids",
                "scope.scope_fingerprint",
                "dimension",
                "unit",
                "denominator",
                "branch_semantics",
                "function_semantics",
                "test_selection",
            ],
            "changed_fields": list(changed_fields or []),
            "reason": "fixture",
            "equivalence_decision": None,
            "equivalence_artifact_id": None,
            "reset_authority_decision": None,
            "reset_artifact_id": None,
        },
        "changed_code_record_id": None,
        "mutation_record_id": None,
        "flake_observation_ids": [],
        "artifact_id": artifact_id,
    }


def _base_contract() -> dict:
    revision = "4d58f73d7c6a1e880a4baaefdbc5a125abaaac7f"
    deferral = {
        "maturity_deferral_id": "def-inventory",
        "repository": "pantheon-config",
        "revision": revision,
        "owner": "orchestrator",
        "reviewer": "orchestrator",
        "approver": "orchestrator",
        "reason": "pilot inventory only",
        "scope": {"dimensions": list(["line", "branch", "function", "changed_code", "mutation", "flake"])},
        "started_at": "2026-09-07T00:00:00Z",
        "expiry": _expiry(),
        "restoration_condition": "verified adapters and independent evidence",
        "restoration_plan": "T-3380 follow-up machinery before promotion",
        "compensating_evidence_ids": ["art-pending"],
        "decision_ref": "tickets/active/T-3380.md",
        "active_state": _active_state(),
        "expiry_action": "revert_to_inventory",
        "authority_decision": _authority_decision("decision-def-inventory"),
        "current_stage": "inventory_only",
        "target_stage": "baseline",
        "blocked_prerequisite": "independent measurement/parity prerequisites",
        "unsupported_dimensions": list(["line", "branch", "function", "changed_code", "mutation", "flake"]),
        "downstream_ticket": "T-3380",
        "next_action": "keep pilot non-enforcing",
        "review_trigger": "schema validator independently reviewed",
    }
    return {
        "schema_version": 1,
        "contract_id": "repo-evidence-pantheon-config-org083-pilot",
        "repository": "pantheon-config",
        "registry_revision": "4d58f73d7c6a1e880a4baaefdbc5a125abaaac7f",
        "evidence_revision": revision,
        "collected_at": "2026-09-07T00:00:00Z",
        "repo_class": "live_code",
        "arc_author": "consultant",
        "fleet_steward_role": "orchestrator",
        "repo_evidence_owner_role": "orchestrator",
        "owner_resolution": {
            "eligible_rw_roles": ["orchestrator", "recovery", "second-head", "specialist", "sub-orchestrator-1", "sub-orchestrator-2"],
            "registry_revision": "4d58f73d7c6a1e880a4baaefdbc5a125abaaac7f",
            "registry_blob_id": "REPOS",
            "instance_blob_id": "INSTANCE",
            "requested_role": "orchestrator",
            "resolved_role": "orchestrator",
            "basis": "explicit-assignment",
            "assignment_authority": "orchestrator:T-3380",
            "issues": [],
        },
        "maturity": "inventory_only",
        "target_maturity": "baseline",
        "next_review": "schema validator independent review",
        "inventory": {
            "language_substrates": ["python"],
            "runner_manifests": ["pyproject absent; pytest by convention"],
            "test_commands": ["python3 -m pytest tools/test_repository_evidence.py"],
            "ci_workflows": [],
            "applicability_evidence_ids": ["art-pending"],
        },
        "dimension_dispositions": [
            {
                "disposition_id": f"disp-{dimension}",
                "repository": "pantheon-config",
                "revision": revision,
                "dimension": dimension,
                "status": "deferred",
                "authority": "ORG-083",
                "reason": "pilot carries shape only",
                "scope": {"selectors": ["tools/"]},
                "effective_revision": revision,
                "measurement_ids": [],
                "changed_code_record_id": None,
                "mutation_record_id": None,
                "flake_observation_ids": [],
                "deferral_id": "def-inventory",
                "exclusion_id": None,
                "applicability_evidence_ids": [],
                "steward_approval": None,
                "applicability_decision": None,
                "approved_disposition_decision": None,
                "re_evaluate_trigger": "adapter verified",
            }
            for dimension in ("line", "branch", "function", "changed_code", "mutation", "flake")
        ],
        "quantitative_measurements": [],
        "changed_code_records": [],
        "mutation_records": [],
        "flake_observations": [],
        "boundary_records": [
            {
                "boundary_record_id": f"boundary-{kind}",
                "repository": "pantheon-config",
                "revision": revision,
                "boundary_kind": kind,
                "producer": "pantheon-config",
                "transport": "none",
                "consumer": "local validator",
                "status": "not_exercised",
                "tests": [],
                "preconditions": [],
                "asserted_effect": "not exercised in inventory pilot",
                "artifact_ids": [],
                "owner": "orchestrator",
                "deferral_id": "def-inventory",
                "applicability_evidence_ids": [],
                "steward_approval": None,
                "applicability_decision": None,
                "approved_disposition_decision": None,
            }
            for kind in ("production_subprocess", "shell_suite", "producer_consumer", "ipc", "network", "storage", "external_api", "ui_e2e")
        ],
        "retry_policy_records": [
            {
                "retry_policy_id": "retry-none",
                "repository": "pantheon-config",
                "revision": revision,
                "command_fingerprint": "none",
                "test_set_fingerprint": "repository-evidence-tests",
                "policy_source": "none",
                "max_attempts": 1,
                "effect_on_evidence": "no retry evidence claimed",
                "linked_flake_ids": [],
            }
        ],
        "deploy_tolerance_records": [
            {
                "deploy_tolerance_id": "deploy-none",
                "repository": "pantheon-config",
                "revision": revision,
                "workflow": "none",
                "step": "none",
                "command_identity": "none",
                "approver": "orchestrator",
                "started_at": "2026-09-07T00:00:00Z",
                "expiry": _expiry(),
                "trigger": {"kind": "timestamp", "value": "2026-12-31T00:00:00Z"},
                "restoration_plan": "not a deployment record",
                "effect_on_evidence": "none",
                "evidence_artifact_id": "art-pending",
            }
        ],
        "test_surface_gaps": [],
        "effectiveness_receipts": [
            {
                "effectiveness_receipt_id": "eff-quarantine-shape",
                "repository": "pantheon-config",
                "revision": revision,
                "kind": "quarantine_review",
                "reviewed_at": "2026-09-07T00:00:00Z",
                "reviewer": "orchestrator",
                "scope": {"selectors": ["tools/repository_evidence.py"]},
                "limitations": "shape only; quarantine_review is never positive",
                "payload": {
                    "quarantine_id": "quarantine-shape",
                    "reproduction_artifact_id": "art-pending",
                    "restoration_attempt": "not attempted",
                    "disposition": "non-positive shape receipt",
                },
                "review_decision": None,
                "independent_review_decision": None,
            }
        ],
        "efficiency_records": [
            {
                "efficiency_id": "efficiency-shape",
                "repository": "pantheon-config",
                "revision": revision,
                "environment_fingerprint": "fixture",
                "command_fingerprint": "fixture",
                "test_set_fingerprint": "fixture",
                "observation_window": "fixture",
                "sample_count": 1,
                "costs": _pilot_costs(),
                "cadence": "none",
                "comparison_baseline_id": None,
                "comparability": {"verdict": "refused", "reason": "shape only"},
                "artifact_id": "art-pending",
                "limitations": "not measured",
            }
        ],
        "parity_records": [
            {
                "parity_id": "parity-local-only",
                "repository": "pantheon-config",
                "revision": revision,
                "verdict": "local_only",
                "local_command": "python3 -m pytest tools/test_repository_evidence.py",
                "ci_command": None,
                "local_environment": "fixture",
                "ci_environment": None,
                "test_set_fingerprint": "repository-evidence-tests",
                "workflow_revision": None,
                "artifact_ids": ["art-pending"],
                "differences": ["no CI evidence in pilot"],
                "equivalence_evidence_id": None,
                "owner": "orchestrator",
                "waiver_id": None,
                "restoration_plan": "future parity evidence before promotion",
            }
        ],
        "retention_records": [
            {
                "retention_id": "ret-art-pending",
                "repository": "pantheon-config",
                "revision": revision,
                "artifact_id": "art-pending",
                "artifact_class": "proof",
                "created_at": "2026-09-07T00:00:00Z",
                "storage_locator": {"kind": "inline", "value": base64.b64encode(b"pending proof\n").decode("ascii")},
                "digest_algorithm": "sha256",
                "digest": _sha(b"pending proof\n"),
                "availability": {
                    "state": "verified_available",
                    "verified_at": "2026-09-07T00:00:00Z",
                    "retrieval_receipt_id": "receipt-art-pending",
                },
                "retrieval_receipts": [
                    {
                        "receipt_id": "receipt-art-pending",
                        "artifact_id": "art-pending",
                        "retrieved_at": "2026-09-07T00:00:00Z",
                        "observed_locator": "inline",
                        "observed_digest_algorithm": "sha256",
                        "observed_digest": _sha(b"pending proof\n"),
                        "observed_size": len(b"pending proof\n"),
                    }
                ],
                "retention_end": {"kind": "timestamp", "value": "2026-12-31T00:00:00Z"},
                "expiry_action": "retain_tombstone",
                "tombstone_artifact_id": None,
                "owner": "orchestrator",
                "superseded_by": None,
            }
        ],
        "generated_source_records": [
            {
                "generated_source_id": "generated-none",
                "repository": "pantheon-config",
                "revision": revision,
                "path_selectors": [],
                "source_of_truth": "not applicable to pilot",
                "generator": None,
                "generated_revision": None,
                "generated_digest": None,
                "risk_acceptor": "orchestrator",
                "affected_dimensions": [],
                "treatment": "included",
                "reason": "no generated exclusion claimed",
                "exclusion_id": None,
                "re_evaluate_trigger": "generated source appears",
            }
        ],
        "exclusion_records": [],
        "waiver_records": [],
        "maturity_deferrals": [deferral],
        "quarantine_records": [
            {
                "quarantine_id": "quarantine-shape",
                "repository": "pantheon-config",
                "revision": revision,
                "owner": "orchestrator",
                "reviewer": "orchestrator",
                "approver": "orchestrator",
                "reason": "shape-only non-positive receipt",
                "scope": {"tests": []},
                "started_at": "2026-09-07T00:00:00Z",
                "expiry": _expiry(),
                "restoration_condition": "real quarantine evidence exists",
                "restoration_plan": "none",
                "compensating_evidence_ids": ["art-pending"],
                "decision_ref": "tickets/active/T-3380.md",
                "active_state": _active_state(),
                "expiry_action": "revert_to_inventory",
                "authority_decision": _authority_decision("decision-quarantine-shape", record_id="quarantine-shape"),
                "classification": "suspected_flake",
                "tests": [],
                "evidence_ids": ["art-pending"],
                "flake_observation_id": None,
                "retry_policy_id": "retry-none",
                "coverage_impact": "none",
                "boundary_impact": "none",
            }
        ],
        "artifact_records": [_artifact("art-pending", b"pending proof\n")],
    }


class RepositoryEvidenceTests(unittest.TestCase):
    def _module(self):
        return importlib.import_module("tools.repository_evidence")

    def _contract(self) -> dict:
        contract = _base_contract()
        mod = self._module()
        resolution = mod.resolve_repository_owner(contract, Path.cwd(), requested_role="orchestrator")
        contract["owner_resolution"].update(
            {
                "eligible_rw_roles": list(resolution.eligible_rw_roles),
                "registry_blob_id": resolution.registry_blob_id,
                "instance_blob_id": resolution.instance_blob_id,
                "basis": resolution.basis,
                "resolved_role": resolution.resolved_role,
            }
        )
        return contract

    def _validate(self, contract: dict):
        return self._module().validate_repository_evidence(contract, repo_root=Path.cwd(), now=NOW)

    def _codes(self, contract: dict) -> set[str]:
        return {issue.code for issue in self._validate(contract).issues}

    def _make_baseline_ready(self, contract: dict) -> None:
        contract["parity_records"][0]["verdict"] = "exact"
        for measurement in (_measurement("measure-line", "line"), _measurement("measure-branch", "branch")):
            _attach_semantics(contract, measurement, retain=True)
            contract["quantitative_measurements"].append(measurement)
        for record in contract["dimension_dispositions"]:
            dimension = record["dimension"]
            if dimension in ("line", "branch"):
                record["status"] = "measured"
                record["measurement_ids"] = [f"measure-{dimension}"]
            elif dimension in ("function", "changed_code"):
                record["approved_disposition_decision"] = _authority_decision(
                    f"decision-{dimension}-baseline",
                    kind="dimension",
                    dimension=dimension,
                    record_id=record["disposition_id"],
                )
        for boundary in contract["boundary_records"]:
            boundary["approved_disposition_decision"] = _authority_decision(
                f"decision-{boundary['boundary_record_id']}",
                kind="boundary",
                boundary_kind=boundary["boundary_kind"],
                record_id=boundary["boundary_record_id"],
            )

    def test_planned_public_api_is_present(self) -> None:
        mod = self._module()
        expected = {
            "RepositoryEvidenceError",
            "RepositoryEvidenceIssue",
            "RepositoryEvidenceContract",
            "OwnerResolution",
            "load_repository_evidence",
            "parse_repository_evidence",
            "canonical_repository_evidence_bytes",
            "generate_repository_evidence_schema",
            "load_authority_documents",
            "resolve_repository_owner",
            "validate_repository_evidence",
            "derive_repository_maturity",
            "check_repository_evidence",
            "main",
        }
        self.assertEqual(expected - set(dir(mod)), set())

    def test_generated_schema_is_byte_stable_and_checked(self) -> None:
        mod = self._module()
        generated = mod._canonical_json_bytes(mod.generate_repository_evidence_schema())
        schema_path = Path("schemas/repository-evidence-contract.schema.json")
        self.assertEqual(generated, schema_path.read_bytes())
        schema = json.loads(generated)
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["$defs"]["effectiveness_receipts"]["additionalProperties"])
        self.assertEqual(
            schema["$defs"]["AuthorityDecision"]["required"],
            ["artifact_id", "decided_at", "decision_id", "decision_ref", "role", "scope"],
        )
        self.assertFalse(schema["$defs"]["AuthorityDecision"]["additionalProperties"])
        self.assertIn("oneOf", schema["$defs"]["AuthorityDecision"]["properties"]["scope"])
        self.assertEqual(set(schema["$defs"]["CostClassMap"]["required"]), set(mod.COST_CLASSES))
        self.assertFalse(schema["$defs"]["EnforcementVerificationPayload"]["additionalProperties"])
        self.assertEqual(schema["$defs"]["ArtifactToolName"], {"type": "string", "minLength": 1})
        self.assertFalse(schema["$defs"]["ToolIdentity"]["additionalProperties"])
        self.assertFalse(schema["$defs"]["EfficiencyComparability"]["additionalProperties"])
        self.assertFalse(schema["$defs"]["QuantitativeComparability"]["additionalProperties"])
        _assert_supported_schema_keywords(schema)
        bad_threshold = {"state": "enforcing", "comparator": "gte", "value": 0.8, "tolerance": 0.0}
        bad_threshold_alias = {"state": "unset", "comparator": ">=", "value": None, "tolerance": 0.0}
        bad_ratchet = {"state": "adopted", "direction": "nondecreasing", "adopted": 0.8, "tolerance": 0.0}
        bad_ratchet_alias = {"state": "unset", "direction": "increase", "proposed": None, "adopted": None, "tolerance": 0.0}
        bad_comparability = {
            "verdict": "comparable",
            "baseline_measurement_id": "m-base",
            "current_measurement_id": "m-current",
            "checked_fields": [],
            "changed_fields": ["denominator"],
            "reason": "changed",
            "equivalence_decision": None,
            "equivalence_artifact_id": None,
            "reset_authority_decision": None,
            "reset_artifact_id": None,
        }
        bad_reset = {
            "verdict": "reset_required",
            "baseline_measurement_id": "m-base",
            "current_measurement_id": "m-current",
            "checked_fields": [],
            "changed_fields": [],
            "reason": "reset",
            "equivalence_decision": None,
            "equivalence_artifact_id": None,
            "reset_authority_decision": None,
            "reset_artifact_id": None,
        }
        bad_active = {"status": "verified_available"}
        bad_expiry = {"kind": "timestamp", "value": "2026-12-31T00:00:00Z", "action": "anything"}
        for def_name, instance in (
            ("Threshold", bad_threshold),
            ("Threshold", bad_threshold_alias),
            ("Ratchet", bad_ratchet),
            ("Ratchet", bad_ratchet_alias),
            ("QuantitativeComparability", bad_comparability),
            ("QuantitativeComparability", bad_reset),
            ("ActiveState", bad_active),
            ("TypedExpiry", bad_expiry),
        ):
            with self.subTest(def_name=def_name, instance=instance):
                _assert_schema_rejects(schema["$defs"][def_name], instance, schema)
        threshold_without_approval = {
            "state": "enforcing",
            "comparator": "gte",
            "value": 0.8,
            "tolerance": 0.0,
            "proposal_decision": _authority_decision("decision-threshold-proposal", kind="measurement"),
        }
        self.assertIn(
            "$/approval_decision: required",
            _schema_subset_errors(schema["$defs"]["Threshold"], threshold_without_approval, root_schema=schema),
        )
        ratchet_without_proposal = {
            "state": "proposed",
            "direction": "nondecreasing",
            "proposed": 0.8,
            "adopted": None,
            "tolerance": 0.0,
            "adoption_decision": None,
        }
        self.assertIn(
            "$/proposal_decision: required",
            _schema_subset_errors(schema["$defs"]["Ratchet"], ratchet_without_proposal, root_schema=schema),
        )
        self.assertNotEqual(_schema_subset_errors({"type": "number"}, True, root_schema=schema), [])
        self.assertNotEqual(_schema_subset_errors({"type": "integer"}, False, root_schema=schema), [])

    def test_schema_field_ownership_map_is_complete_exact_and_fallback_free(self) -> None:
        mod = self._module()
        factories = mod._field_schema_factories()
        self.assertEqual(set(factories), set(mod.RECORD_ALLOWED_FIELDS))
        for collection, fields in mod.RECORD_ALLOWED_FIELDS.items():
            with self.subTest(collection=collection):
                self.assertEqual(set(factories[collection]), set(fields))
        missing = {collection: dict(fields) for collection, fields in factories.items()}
        del missing["artifact_records"]["tool"]
        with self.assertRaisesRegex(mod.RepositoryEvidenceError, "artifact_records"):
            mod._assert_schema_field_ownership_complete(missing)
        extra = {collection: dict(fields) for collection, fields in factories.items()}
        extra["artifact_records"]["future_id"] = mod._string_schema
        with self.assertRaisesRegex(mod.RepositoryEvidenceError, "artifact_records"):
            mod._assert_schema_field_ownership_complete(extra)
        with self.assertRaisesRegex(mod.RepositoryEvidenceError, "collections"):
            mod._assert_schema_field_ownership_complete({})
        with self.assertRaises(KeyError):
            mod._field_schema("artifact_records", "future_id")

    def test_schema_split_defs_reject_cross_collection_shape_aliases(self) -> None:
        mod = self._module()
        schema = mod.generate_repository_evidence_schema()
        self.assertEqual(schema["$defs"]["artifact_records"]["properties"]["tool"], {"$ref": "#/$defs/ArtifactToolName"})
        self.assertEqual(schema["$defs"]["quantitative_measurements"]["properties"]["collector"], {"$ref": "#/$defs/ToolIdentity"})
        self.assertEqual(schema["$defs"]["quantitative_measurements"]["properties"]["comparability"], {"$ref": "#/$defs/QuantitativeComparability"})
        self.assertEqual(schema["$defs"]["changed_code_records"]["properties"]["diff_tool"], {"$ref": "#/$defs/ToolIdentity"})
        self.assertEqual(schema["$defs"]["mutation_records"]["properties"]["tool"], {"$ref": "#/$defs/ToolIdentity"})
        self.assertEqual(schema["$defs"]["efficiency_records"]["properties"]["comparability"], {"$ref": "#/$defs/EfficiencyComparability"})
        self.assertEqual(schema["$defs"]["deploy_tolerance_records"]["properties"]["trigger"], {"$ref": "#/$defs/TypedTrigger"})
        self.assertEqual(schema["$defs"]["retention_records"]["properties"]["retention_end"], {"$ref": "#/$defs/RetentionEnd"})
        self.assertEqual(schema["$defs"]["retention_records"]["properties"]["superseded_by"]["type"], ["string", "null"])
        self.assertEqual(schema["$defs"]["ParityRecord"], schema["$defs"]["parity_records"])
        self.assertEqual(schema["$defs"]["GeneratedSourceRecord"], schema["$defs"]["generated_source_records"])
        _assert_schema_rejects(schema["$defs"]["ArtifactToolName"], {"name": "normalized-input"}, schema)
        _assert_schema_rejects(schema["$defs"]["ToolIdentity"], "normalized-input", schema)
        artifact = copy.deepcopy(self._contract()["artifact_records"][0])
        artifact["tool"] = {"name": "normalized-input"}
        _assert_schema_rejects(schema["$defs"]["artifact_records"], artifact, schema)
        mutation = {
            "mutation_record_id": "mutation-shape",
            "repository": "pantheon-config",
            "revision": "4d58f73d7c6a1e880a4baaefdbc5a125abaaac7f",
            "tool": "mutmut",
            "operator_scope": "fixture",
            "operator_set_digest": "fixture",
            "seed": "1",
            "test_selection": "fixture",
            "timeout_policy": "fixture",
            "artifact_id": "art-pending",
            "counts": {field: 0 for field in ("error", "equivalent", "excluded", "generated", "invalid", "killed", "no_coverage", "survived", "timeout")},
            "terminal_dispositions": [],
        }
        _assert_schema_rejects(schema["$defs"]["mutation_records"], mutation, schema)
        _assert_schema_rejects(
            schema["$defs"]["TypedTrigger"],
            {"kind": "timestamp", "value": "2026-12-31T00:00:00Z", "action": "fleet_drift"},
            schema,
        )
        _assert_schema_rejects(
            schema["$defs"]["TypedTrigger"],
            {"kind": "event", "value": "restored"},
            schema,
        )
        _assert_schema_rejects(
            schema["$defs"]["RetentionEnd"],
            {"kind": "timestamp", "value": "2026-12-31T00:00:00Z", "action": "retain_tombstone"},
            schema,
        )
        _assert_schema_rejects(
            schema["$defs"]["RetentionEnd"],
            {"kind": "event", "value": "restored"},
            schema,
        )
        _assert_schema_rejects(
            schema["$defs"]["TypedExpiry"],
            {"kind": "timestamp", "value": "2026-12-31T00:00:00Z"},
            schema,
        )
        self.assertEqual(
            _schema_subset_errors(schema["$defs"]["EfficiencyComparability"], {"verdict": "refused", "reason": "shape only"}, root_schema=schema),
            [],
        )
        _assert_schema_rejects(schema["$defs"]["EfficiencyComparability"], {"verdict": "refused", "reason": ""}, schema)
        _assert_schema_rejects(
            schema["$defs"]["EfficiencyComparability"],
            {"verdict": "refused", "reason": "shape only", "checked_fields": []},
            schema,
        )
        _assert_schema_rejects(schema["$defs"]["QuantitativeComparability"], {"verdict": "refused", "reason": "shape only"}, schema)
        for collection, field in (
            ("boundary_records", "owner"),
            ("deploy_tolerance_records", "approver"),
            ("test_surface_gaps", "owner"),
            ("effectiveness_receipts", "reviewer"),
            ("parity_records", "owner"),
            ("retention_records", "owner"),
            ("generated_source_records", "risk_acceptor"),
        ):
            with self.subTest(collection=collection, field=field):
                self.assertNotEqual(_schema_subset_errors(mod._field_schema(collection, field), None, root_schema=schema), [])
        for collection, field in (
            ("exclusion_records", "owner"),
            ("exclusion_records", "reviewer"),
            ("exclusion_records", "approver"),
            ("waiver_records", "owner"),
            ("waiver_records", "reviewer"),
            ("waiver_records", "approver"),
            ("maturity_deferrals", "owner"),
            ("maturity_deferrals", "reviewer"),
            ("maturity_deferrals", "approver"),
            ("quarantine_records", "owner"),
            ("quarantine_records", "reviewer"),
            ("quarantine_records", "approver"),
        ):
            with self.subTest(collection=collection, field=field):
                self.assertEqual(_schema_subset_errors(mod._field_schema(collection, field), None, root_schema=schema), [])

    def test_schema_parity_conditionals_keep_inventory_nulls_but_require_promotion_evidence(self) -> None:
        mod = self._module()
        schema = mod.generate_repository_evidence_schema()
        base = copy.deepcopy(self._contract()["parity_records"][0])
        self.assertEqual(_schema_subset_errors(schema["$defs"]["parity_records"], base, root_schema=schema), [])
        exact = copy.deepcopy(base)
        exact["verdict"] = "exact"
        errors = _schema_subset_errors(schema["$defs"]["parity_records"], exact, root_schema=schema)
        self.assertIn("$/ci_command: type", errors)
        self.assertIn("$/ci_environment: type", errors)
        self.assertIn("$/workflow_revision: type", errors)
        evidenced = copy.deepcopy(base)
        evidenced.update({"verdict": "evidenced_equivalent", "ci_command": "ci", "ci_environment": "ci", "workflow_revision": "wf"})
        self.assertIn("$/equivalence_evidence_id: type", _schema_subset_errors(schema["$defs"]["parity_records"], evidenced, root_schema=schema))
        divergent = copy.deepcopy(base)
        divergent.update({"verdict": "divergent_with_waiver", "ci_command": "ci", "ci_environment": "ci", "workflow_revision": "wf"})
        self.assertIn("$/waiver_id: type", _schema_subset_errors(schema["$defs"]["parity_records"], divergent, root_schema=schema))

    def test_schema_generated_source_tuple_and_selector_branches_are_exact(self) -> None:
        mod = self._module()
        schema = mod.generate_repository_evidence_schema()
        base = copy.deepcopy(self._contract()["generated_source_records"][0])
        self.assertEqual(_schema_subset_errors(schema["$defs"]["generated_source_records"], base, root_schema=schema), [])
        partial = copy.deepcopy(base)
        partial["generator"] = "fixture-generator"
        self.assertNotEqual(_schema_subset_errors(schema["$defs"]["generated_source_records"], partial, root_schema=schema), [])
        nonempty_null = copy.deepcopy(base)
        nonempty_null["path_selectors"] = ["generated/"]
        self.assertNotEqual(_schema_subset_errors(schema["$defs"]["generated_source_records"], nonempty_null, root_schema=schema), [])
        all_string = copy.deepcopy(base)
        all_string.update({"generator": "fixture-generator", "generated_revision": "rev", "generated_digest": "digest", "path_selectors": ["generated/"]})
        self.assertEqual(_schema_subset_errors(schema["$defs"]["generated_source_records"], all_string, root_schema=schema), [])
        empty_string_tuple = copy.deepcopy(all_string)
        empty_string_tuple["path_selectors"] = []
        self.assertNotEqual(_schema_subset_errors(schema["$defs"]["generated_source_records"], empty_string_tuple, root_schema=schema), [])
        scalar_path = copy.deepcopy(all_string)
        scalar_path["path_selectors"] = "generated/"
        self.assertNotEqual(_schema_subset_errors(schema["$defs"]["generated_source_records"], scalar_path, root_schema=schema), [])
        empty_item = copy.deepcopy(all_string)
        empty_item["path_selectors"] = [""]
        self.assertNotEqual(_schema_subset_errors(schema["$defs"]["generated_source_records"], empty_item, root_schema=schema), [])

    def test_canonicalize_is_deterministic_and_rejects_duplicate_keys(self) -> None:
        mod = self._module()
        contract = self._contract()
        shuffled = copy.deepcopy(contract)
        shuffled["dimension_dispositions"] = list(reversed(shuffled["dimension_dispositions"]))
        self.assertEqual(mod.canonical_repository_evidence_bytes(contract), mod.canonical_repository_evidence_bytes(shuffled))
        for nonfinite in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(nonfinite=repr(nonfinite)):
                with self.assertRaisesRegex(mod.RepositoryEvidenceError, "non-finite numeric value"):
                    mod.canonical_repository_evidence_bytes({"value": nonfinite})
        with self.assertRaisesRegex(mod.RepositoryEvidenceError, "duplicate key 'schema_version'"):
            mod.parse_repository_evidence(b'{"schema_version":1,"schema_version":1}')
        with self.assertRaisesRegex(mod.RepositoryEvidenceError, "duplicate key 'schema_version'"):
            mod.parse_repository_evidence(b"schema_version: 1\nschema_version: 1\n", path_hint="x.yaml")

    def test_schema_subset_evaluator_mechanics_are_fail_closed(self) -> None:
        schema = {
            "$defs": {
                "Thing": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"arbitrary_field": {"type": "string"}},
                },
                "Loop": {"$ref": "#/$defs/Loop"},
            }
        }
        self.assertEqual(
            _schema_subset_errors({"$ref": "#/$defs/Thing"}, {"arbitrary_field": "ok"}, root_schema=schema),
            [],
        )
        self.assertEqual(
            _schema_subset_errors(
                {"if": {"required": ["missing"]}, "then": {"required": ["other"]}},
                {},
                root_schema=schema,
            ),
            [],
        )
        self.assertNotEqual(_schema_subset_errors({"oneOf": [{"type": "string"}, {"type": "string"}]}, "x", root_schema=schema), [])
        for bad_ref in ("http://example.test/schema", "#/$defs/Missing", "#/$defs/Loop"):
            with self.subTest(bad_ref=bad_ref):
                with self.assertRaises(AssertionError):
                    _schema_subset_errors({"$ref": bad_ref}, {}, root_schema=schema)
        with self.assertRaises(AssertionError):
            _schema_subset_errors({"pattern": "x"}, "x", root_schema=schema)

    def test_root_collections_make_every_specialized_record_reachable(self) -> None:
        contract = self._contract()
        result = self._validate(contract)
        self.assertNotIn("collection_missing", {issue.code for issue in result.issues})
        for collection in self._module().COLLECTION_ORDER:
            self.assertIn(collection, contract)
        del contract["boundary_records"]
        self.assertIn("missing_root_field", self._codes(contract))

    def test_duplicate_ids_and_dangling_typed_refs_fail(self) -> None:
        contract = self._contract()
        contract["artifact_records"].append(copy.deepcopy(contract["artifact_records"][0]))
        contract["boundary_records"][0]["artifact_ids"] = ["missing-artifact"]
        codes = self._codes(contract)
        self.assertIn("duplicate_id", codes)
        self.assertIn("typed_ref_unresolved", codes)

    def test_repository_revision_mismatch_fails_except_changed_code_pair(self) -> None:
        contract = self._contract()
        changed = {
            "changed_code_record_id": "cc-1",
            "repository": "pantheon-config",
            "revision": contract["evidence_revision"],
            "base_ref": "main~1",
            "base_revision": "different-base",
            "head_ref": "HEAD",
            "head_revision": contract["evidence_revision"],
            "merge_base_revision": "different-base",
            "diff_tool": {"name": "git", "version": "fixture"},
            "diff_artifact_id": "art-pending",
            "eligible_units": [],
            "excluded_units": [],
            "exclusion_ids": [],
            "covered_eligible_units": [],
            "denominator_rule": "fixture",
        }
        contract["changed_code_records"].append(changed)
        self.assertNotIn("revision_mismatch", self._codes(contract))
        contract["boundary_records"][0]["revision"] = "other"
        self.assertIn("revision_mismatch", self._codes(contract))

    def test_measured_disposition_requires_measurement_and_specialized_links(self) -> None:
        contract = self._contract()
        disp = contract["dimension_dispositions"][4]
        disp["status"] = "measured"
        disp["measurement_ids"] = []
        disp["mutation_record_id"] = None
        codes = self._codes(contract)
        self.assertIn("measured_missing_measurement", codes)
        contract["mutation_records"].append(
            {
                "mutation_record_id": "mut-1",
                "repository": "pantheon-config",
                "revision": contract["evidence_revision"],
                "tool": {"name": "mutator"},
                "operator_scope": "fixture",
                "operator_set_digest": "sha256",
                "seed": "1",
                "test_selection": "fixture",
                "timeout_policy": "fixture",
                "artifact_id": "art-pending",
                "counts": {"generated": 0, "killed": 0, "survived": 0, "no_coverage": 0, "timeout": 0, "error": 0, "equivalent": 0, "invalid": 0, "excluded": 0},
                "terminal_dispositions": [],
            }
        )
        contract["quantitative_measurements"].append(
            {
                "measurement_id": "measure-mut",
                "repository": "pantheon-config",
                "revision": contract["evidence_revision"],
                "collected_at": "2026-09-07T00:00:00Z",
                "dimension": "mutation",
                "unit": "mutants",
                "collector": {},
                "scope": {},
                "numerator": 0,
                "denominator": 0,
                "value": 0,
                "baseline": {"measurement_id": "measure-mut", "numerator": 0, "denominator": 0, "value": 0},
                "current": {"measurement_id": "measure-mut", "numerator": 0, "denominator": 0, "value": 0},
                "threshold": {"state": "unset"},
                "ratchet": {"state": "unset"},
                "comparability": {"verdict": "refused"},
                "changed_code_record_id": None,
                "mutation_record_id": "mut-1",
                "flake_observation_ids": [],
                "artifact_id": "art-pending",
            }
        )
        disp["measurement_ids"] = ["measure-mut"]
        disp["mutation_record_id"] = "mut-1"
        self.assertNotIn("measured_missing_measurement", self._codes(contract))

    def test_retry_deploy_quarantine_and_flake_are_not_interchangeable(self) -> None:
        contract = self._contract()
        contract["retry_policy_records"][0]["linked_flake_ids"] = ["quarantine-shape"]
        contract["quarantine_records"][0]["retry_policy_id"] = "deploy-none"
        codes = self._codes(contract)
        self.assertIn("typed_ref_unresolved", codes)

    def test_artifact_digest_retrieval_mismatch_blocks_promotion(self) -> None:
        contract = self._contract()
        contract["artifact_records"][0]["digest"] = "0" * 64
        result = self._validate(contract)
        self.assertIn("artifact_digest_mismatch", {issue.code for issue in result.issues})
        self.assertFalse(result.facts.artifacts_verified)
        self.assertEqual(result.derived_maturity, "inventory_only")
        contract = self._contract()
        contract["artifact_records"][0]["storage_locator"] = {"kind": "repo_path", "value": "../secret"}
        self.assertIn("artifact_unsafe_locator", self._codes(contract))

    def test_retention_unavailable_expired_or_deleted_blocks_active_decision(self) -> None:
        contract = self._contract()
        contract["retention_records"][0]["availability"]["state"] = "deleted"
        contract["retention_records"][0]["retention_end"]["value"] = "2026-01-01T00:00:00Z"
        contract["retention_records"][0]["availability"]["retrieval_receipt_id"] = "missing"
        codes = self._codes(contract)
        self.assertIn("retention_unavailable", codes)
        self.assertIn("retention_expired", codes)
        self.assertIn("retention_receipt_unresolved", codes)

    def test_generated_source_not_excluded_without_record(self) -> None:
        contract = self._contract()
        contract["generated_source_records"][0]["treatment"] = "excluded"
        contract["generated_source_records"][0]["exclusion_id"] = None
        self.assertIn("generated_exclusion_not_live", self._codes(contract))

    def test_exclusion_changes_denominator_and_links_measurements(self) -> None:
        contract = self._contract()
        exclusion = {
            "exclusion_id": "ex-1",
            "repository": "pantheon-config",
            "revision": contract["evidence_revision"],
            "owner": "orchestrator",
            "reviewer": "orchestrator",
            "approver": "orchestrator",
            "reason": "fixture",
            "scope": {"paths": ["generated"]},
            "started_at": "2026-09-07T00:00:00Z",
            "expiry": _expiry(),
            "restoration_condition": "fixture",
            "restoration_plan": "fixture",
            "compensating_evidence_ids": ["art-pending"],
            "decision_ref": "T-3380",
            "active_state": _active_state(),
            "expiry_action": "revert_to_inventory",
            "authority_decision": _authority_decision("decision-ex-1", record_id="ex-1"),
            "affected_measurement_ids": ["missing-measure"],
        }
        contract["exclusion_records"].append(exclusion)
        self.assertIn("typed_ref_unresolved", self._codes(contract))

    def test_waiver_expiry_fallbacks_fail_closed_or_demote(self) -> None:
        contract = self._contract()
        waiver = {
            "waiver_id": "waiver-1",
            "repository": "pantheon-config",
            "revision": contract["evidence_revision"],
            "owner": "orchestrator",
            "reviewer": "orchestrator",
            "approver": "orchestrator",
            "reason": "fixture",
            "scope": {"obligation": "parity"},
            "started_at": "2026-09-07T00:00:00Z",
            "expiry": {"kind": "timestamp", "value": "2026-01-01T00:00:00Z", "action": "fail_closed"},
            "restoration_condition": "fixture",
            "restoration_plan": "fixture",
            "compensating_evidence_ids": ["art-pending"],
            "decision_ref": "T-3380",
            "active_state": _active_state(),
            "expiry_action": "fail_closed",
            "authority_decision": _authority_decision("decision-waiver-1", record_id="waiver-1"),
            "obligation": "parity",
            "fallback": "fail_closed",
        }
        contract["waiver_records"].append(waiver)
        self.assertIn("exception_not_live", self._codes(contract))

    def test_waiver_positive_effectiveness_substitution_requires_scope_retention_and_independence(self) -> None:
        contract = self._contract()
        contract["waiver_records"].append(_positive_effectiveness_waiver())
        result = self._validate(contract)
        self.assertEqual(result.issues, ())
        self.assertTrue(result.facts.positive_effectiveness_or_waiver)
        self.assertTrue(result.facts.independent_review_valid)

    def test_positive_receipt_branch_requires_retained_independent_review(self) -> None:
        contract = self._contract()
        contract["effectiveness_receipts"].append(_positive_effectiveness_receipt())
        result = self._validate(contract)
        self.assertEqual(result.issues, ())
        self.assertTrue(result.facts.positive_effectiveness_or_waiver)
        self.assertTrue(result.facts.independent_review_valid)
        contract["effectiveness_receipts"][-1]["independent_review_decision"] = None
        result = self._validate(contract)
        self.assertFalse(result.facts.positive_effectiveness_or_waiver)
        self.assertFalse(result.facts.independent_review_valid)

    def test_empty_positive_receipts_and_waivers_do_not_satisfy_effectiveness(self) -> None:
        result = self._validate(self._contract())
        self.assertFalse(result.facts.positive_effectiveness_or_waiver)
        self.assertFalse(result.facts.independent_review_valid)

    def test_waiver_without_independent_review_does_not_satisfy_effectiveness(self) -> None:
        contract = self._contract()
        waiver = _positive_effectiveness_waiver()
        waiver["independent_review_decision"] = None
        contract["waiver_records"].append(waiver)
        result = self._validate(contract)
        self.assertIn("waiver_independent_review_invalid", {issue.code for issue in result.issues})
        self.assertFalse(result.facts.positive_effectiveness_or_waiver)
        self.assertFalse(result.facts.independent_review_valid)

    def test_waiver_branch_rejects_expired_timestamp(self) -> None:
        contract = self._contract()
        waiver = _positive_effectiveness_waiver()
        waiver["expiry"]["value"] = "2026-01-01T00:00:00Z"
        contract["waiver_records"].append(waiver)
        result = self._validate(contract)
        self.assertIn("exception_not_live", {issue.code for issue in result.issues})
        self.assertFalse(result.facts.positive_effectiveness_or_waiver)

    def test_waiver_branch_rejects_unproved_event_expiry(self) -> None:
        contract = self._contract()
        waiver = _positive_effectiveness_waiver()
        waiver["expiry"] = {"kind": "event", "value": "pending-remediation", "pending_evidence_id": "missing", "action": "fail_closed"}
        contract["waiver_records"].append(waiver)
        result = self._validate(contract)
        self.assertIn("event_pending_unproved", {issue.code for issue in result.issues})
        self.assertFalse(result.facts.positive_effectiveness_or_waiver)

    def test_waiver_branch_rejects_unresolved_independent_role(self) -> None:
        contract = self._contract()
        waiver = _positive_effectiveness_waiver()
        waiver["independent_review_decision"]["role"] = "missing-role"
        contract["waiver_records"].append(waiver)
        result = self._validate(contract)
        self.assertIn("authority_decision_role_unknown", {issue.code for issue in result.issues})
        self.assertFalse(result.facts.independent_review_valid)

    def test_waiver_branch_rejects_wrong_authority_role_and_scope(self) -> None:
        contract = self._contract()
        waiver = _positive_effectiveness_waiver()
        waiver["authority_decision"]["role"] = "second-head"
        waiver["independent_review_decision"]["scope"]["record_id"] = "other-waiver"
        contract["waiver_records"].append(waiver)
        codes = {issue.code for issue in self._validate(contract).issues}
        self.assertIn("authority_decision_role_mismatch", codes)
        self.assertIn("waiver_independent_review_invalid", codes)

    def test_waiver_branch_rejects_missing_retention_for_decision_or_compensation(self) -> None:
        contract = self._contract()
        waiver = _positive_effectiveness_waiver()
        contract["artifact_records"].append(_artifact("art-unretained-waiver", b"waiver proof\n"))
        waiver["compensating_evidence_ids"] = ["art-unretained-waiver"]
        contract["waiver_records"].append(waiver)
        result = self._validate(contract)
        self.assertFalse(result.facts.positive_effectiveness_or_waiver)

    def test_waiver_requires_steward_approver_and_authority_role(self) -> None:
        contract = self._contract()
        waiver = _positive_effectiveness_waiver()
        waiver["approver"] = "second-head"
        waiver["authority_decision"]["role"] = "second-head"
        contract["waiver_records"].append(waiver)
        result = self._validate(contract)
        self.assertIn("waiver_authority_invalid", {issue.code for issue in result.issues})
        self.assertFalse(result.facts.positive_effectiveness_or_waiver)

    def test_unrelated_invalid_record_does_not_make_valid_waiver_branch_pass_by_shortcut(self) -> None:
        contract = self._contract()
        contract["waiver_records"].append(_positive_effectiveness_waiver())
        contract["retry_policy_records"][0]["max_attempts"] = "not-int"
        result = self._validate(contract)
        self.assertIn("retry_policy", {issue.code for issue in result.issues})
        self.assertTrue(self._module()._effectiveness_alternative(
            contract,
            self._module()._ids_by_collection(contract)[0],
            repo_root=Path.cwd(),
            now=NOW,
        ).live_waiver_valid)

    def test_maturity_deferral_expiry_refuses_promotion(self) -> None:
        contract = self._contract()
        contract["maturity_deferrals"][0]["expiry"]["value"] = "2026-01-01T00:00:00Z"
        result = self._validate(contract)
        self.assertIn("exception_not_live", {issue.code for issue in result.issues})
        self.assertEqual(result.derived_maturity, "inventory_only")

    def test_quarantine_does_not_remove_denominator_without_exclusion(self) -> None:
        contract = self._contract()
        contract["generated_source_records"][0]["treatment"] = "excluded"
        contract["generated_source_records"][0]["exclusion_id"] = "quarantine-shape"
        codes = self._codes(contract)
        self.assertIn("typed_ref_unresolved", codes)
        self.assertIn("generated_exclusion_not_live", codes)

    def test_parity_local_only_and_ci_only_block_advisory(self) -> None:
        contract = self._contract()
        contract["parity_records"][0]["verdict"] = "ci_only"
        result = self._validate(contract)
        self.assertNotIn("parity_not_promotion_eligible", {issue.code for issue in result.issues})
        self.assertFalse(result.facts.parity_eligible)
        self.assertEqual(result.derived_maturity, "inventory_only")

    def test_exact_parity_does_not_promote_all_deferred_zero_measurement_pilot(self) -> None:
        contract = self._contract()
        contract["parity_records"][0]["verdict"] = "exact"
        result = self._validate(contract)
        self.assertEqual(result.issues, ())
        self.assertTrue(result.facts.parity_eligible)
        self.assertFalse(result.facts.measurement_prerequisites)
        self.assertEqual(result.derived_maturity, "inventory_only")

    def test_lower_declared_inventory_can_derive_baseline_without_auto_promotion_claim_issue(self) -> None:
        contract = self._contract()
        self._make_baseline_ready(contract)
        result = self._validate(contract)
        self.assertEqual(result.issues, ())
        self.assertTrue(result.facts.measurement_prerequisites)
        self.assertTrue(result.facts.stage_artifacts_retained)
        self.assertEqual(result.derived_maturity, "baseline")
        self.assertNotIn("maturity_claim_exceeds_derived", result.facts.blocking_issue_codes)

    def test_stage_specific_support_retention_is_computed_above_declared_maturity(self) -> None:
        contract = self._contract()
        self._make_baseline_ready(contract)
        # Real (digest-verified) semantic evidence whose manifest is deliberately not retained.
        measurement = contract["quantitative_measurements"][0]
        unretained_id, unretained = _sem_bundle("sem-unretained", measurement)
        contract["artifact_records"].extend(unretained)
        measurement["artifact_id"] = unretained_id
        result = self._validate(contract)
        self.assertEqual(result.issues, ())
        self.assertFalse(result.facts.stage_artifacts_retained)
        self.assertEqual(result.derived_maturity, "inventory_only")

    def test_comparability_changed_fields_require_equivalence_decision_and_artifact(self) -> None:
        contract = self._contract()
        self._make_baseline_ready(contract)
        line = contract["quantitative_measurements"][0]
        line["comparability"]["changed_fields"] = ["denominator"]
        result = self._validate(contract)
        self.assertIn("comparability_material_change_unapproved", {issue.code for issue in result.issues})
        line["comparability"]["equivalence_artifact_id"] = "art-pending"
        line["comparability"]["equivalence_decision"] = _authority_decision(
            "decision-comparability-line",
            kind="measurement",
            dimension="line",
            measurement_id="measure-line",
        )
        self.assertNotIn("comparability_material_change_unapproved", self._codes(contract))

    def test_comparability_reset_requires_reset_authority_not_equivalence(self) -> None:
        contract = self._contract()
        self._make_baseline_ready(contract)
        line = contract["quantitative_measurements"][0]
        line["comparability"]["verdict"] = "reset_required"
        line["comparability"]["equivalence_artifact_id"] = "art-pending"
        line["comparability"]["equivalence_decision"] = _authority_decision(
            "decision-equivalence-not-reset",
            kind="measurement",
            dimension="line",
            measurement_id="measure-line",
        )
        self.assertIn("comparability_reset_required", self._codes(contract))
        line["comparability"]["reset_artifact_id"] = "art-pending"
        line["comparability"]["reset_authority_decision"] = _authority_decision(
            "decision-reset-line",
            kind="measurement",
            dimension="line",
            measurement_id="measure-line",
        )
        result = self._validate(contract)
        self.assertNotIn("comparability_reset_required", {issue.code for issue in result.issues})
        self.assertFalse(result.facts.current_evidence_valid)

    def test_comparability_reset_authority_scope_binds_current_measurement(self) -> None:
        contract = self._contract()
        self._make_baseline_ready(contract)
        line = contract["quantitative_measurements"][0]
        line["comparability"]["verdict"] = "reset_required"
        line["comparability"]["reset_artifact_id"] = "art-pending"
        line["comparability"]["reset_authority_decision"] = _authority_decision(
            "decision-reset-wrong-measurement",
            kind="measurement",
            dimension="line",
            measurement_id="measure-branch",
        )
        self.assertIn("comparability_reset_required", self._codes(contract))

    def test_comparability_unretained_reset_artifact_blocks_retained_support(self) -> None:
        contract = self._contract()
        self._make_baseline_ready(contract)
        contract["artifact_records"].append(_artifact("art-unretained-reset", b"reset proof\n"))
        line = contract["quantitative_measurements"][0]
        line["comparability"]["verdict"] = "reset_required"
        line["comparability"]["reset_artifact_id"] = "art-unretained-reset"
        line["comparability"]["reset_authority_decision"] = _authority_decision(
            "decision-reset-unretained",
            kind="measurement",
            dimension="line",
            measurement_id="measure-line",
        )
        result = self._validate(contract)
        self.assertFalse(result.facts.stage_artifacts_retained)
        self.assertFalse(result.facts.current_evidence_valid)

    def test_comparability_refused_or_reset_cannot_promote_current_evidence(self) -> None:
        contract = self._contract()
        self._make_baseline_ready(contract)
        result = self._validate(contract)
        self.assertTrue(result.facts.current_evidence_valid)
        contract["quantitative_measurements"][0]["comparability"]["verdict"] = "refused"
        self.assertFalse(self._validate(contract).facts.current_evidence_valid)

    def test_threshold_and_ratchet_use_reviewed_enums(self) -> None:
        contract = self._contract()
        self._make_baseline_ready(contract)
        line = contract["quantitative_measurements"][0]
        line["threshold"]["comparator"] = ">="
        line["ratchet"]["direction"] = "increase"
        codes = self._codes(contract)
        self.assertIn("threshold_proposal_missing", codes)
        self.assertIn("ratchet_proposal_missing", codes)

    def test_issue_fact_blockers_cover_declared_codes_and_unknown_errors_block_above_inventory(self) -> None:
        mod = self._module()
        emitted, dynamic = _literal_emitted_issue_codes(Path("tools/repository_evidence.py"))
        self.assertEqual(dynamic, [])
        self.assertEqual(emitted - set(mod.ISSUE_FACT_BLOCKERS), set())
        target_universe = set(mod.RepositoryEvidenceFacts.__dataclass_fields__) | {
            "above_inventory",
            "above_baseline",
            "above_advisory",
        }
        for code, targets in mod.ISSUE_FACT_BLOCKERS.items():
            with self.subTest(code=code):
                self.assertIsInstance(targets, tuple)
                self.assertNotEqual(targets, ())
                self.assertEqual(set(targets) - target_universe, set())
        self.assertTrue(mod._blocked({"future_unmapped_error"}, "above_inventory"))
        self.assertFalse(mod._blocked({"future_unmapped_error"}, "structural_valid"))

    def test_authority_decision_scope_role_and_retention_are_semantic_guards(self) -> None:
        contract = self._contract()
        contract["artifact_records"].append(_artifact("art-unretained", b"unretained decision\n"))
        decision = contract["maturity_deferrals"][0]["authority_decision"]
        decision["role"] = "second-head"
        decision["artifact_id"] = "art-unretained"
        codes = self._codes(contract)
        self.assertIn("authority_decision_role_mismatch", codes)
        self.assertIn("authority_decision_artifact_unretained", codes)

    def test_schema_malformed_nested_types_are_closed_and_discriminated(self) -> None:
        mod = self._module()
        schema = mod.generate_repository_evidence_schema()
        for name in ("AuthorityDecision", "CostClass", "EnforcementVerificationPayload", "RetrievalReceipt"):
            with self.subTest(name=name):
                self.assertFalse(schema["$defs"][name]["additionalProperties"])
        authority_scope = schema["$defs"]["AuthorityDecision"]["properties"]["scope"]["oneOf"]
        self.assertEqual([entry["properties"]["kind"]["const"] for entry in authority_scope], list(mod.AUTHORITY_SCOPE_KINDS))
        self.assertFalse(schema["$defs"]["StorageLocator"]["oneOf"][0]["additionalProperties"])

    def test_missing_higher_stage_prereqs_are_false_facts_not_inventory_errors(self) -> None:
        result = self._validate(self._contract())
        self.assertEqual(result.issues, ())
        self.assertFalse(result.facts.measurement_prerequisites)
        self.assertFalse(result.facts.positive_effectiveness_or_waiver)
        self.assertFalse(result.facts.enforcement_machinery_retained)
        self.assertEqual(result.derived_maturity, "inventory_only")

    def test_unretained_or_unapproved_enforcement_artifact_cannot_enforce(self) -> None:
        contract = self._contract()
        contract["maturity"] = "enforcing"
        enforcement = _artifact("art-enforcement", b"enforcement proof\n", artifact_class="enforcement_verification")
        enforcement["payload"] = {
            "target": "enforcing",
            "mechanism": "mutation-gate",
            "command_fingerprint": "fixture",
            "workflow_revision": "fixture",
            "governed_scope": ["tools/"],
            "expected_failure": "non-coverage failure blocks enforcement",
            "observed_failure_artifact_id": "art-pending",
            "observed_failure_digest": "0" * 64,
            "restored_green_artifact_id": "art-pending",
            "escape_behavior": "",
            "rollback_or_waiver_path": "",
            "verified_at": "2026-09-07T00:00:00Z",
            "owner_decision": None,
            "fleet_decision": None,
            "failure_review_decision": None,
        }
        contract["artifact_records"].append(enforcement)
        result = self._validate(contract)
        codes = {issue.code for issue in result.issues}
        self.assertIn("enforcement_approval_missing", codes)
        self.assertIn("failure_behavior_unproved", codes)
        self.assertIn("maturity_claim_exceeds_derived", codes)
        self.assertFalse(result.facts.enforcement_verified)

    def test_maturity_claim_above_derived_ceiling_is_reported_without_circular_demote(self) -> None:
        contract = self._contract()
        contract["maturity"] = "advisory"
        result = self._validate(contract)
        self.assertEqual(result.derived_maturity, "inventory_only")
        self.assertIn("maturity_claim_exceeds_derived", {issue.code for issue in result.issues})
        self.assertIn("maturity_claim_exceeds_derived", result.facts.blocking_issue_codes)

    def test_comparability_resets_on_tool_runner_scope_denominator_or_generated_change(self) -> None:
        contract = self._contract()
        contract["quantitative_measurements"].append(
            {
                "measurement_id": "m-line",
                "repository": "pantheon-config",
                "revision": contract["evidence_revision"],
                "collected_at": "2026-09-07T00:00:00Z",
                "dimension": "line",
                "unit": "lines",
                "collector": {"tool_name": "coverage"},
                "scope": {"exclusion_ids": [], "generated_source_ids": ["generated-none"]},
                "numerator": 8,
                "denominator": 10,
                "value": 0.7,
                "baseline": {"measurement_id": "m-line", "numerator": 8, "denominator": 10, "value": 0.8},
                "current": {"measurement_id": "m-line", "numerator": 8, "denominator": 10, "value": 0.8},
                "threshold": {"state": "unset"},
                "ratchet": {"state": "unset"},
                "comparability": {"verdict": "comparable"},
                "changed_code_record_id": None,
                "mutation_record_id": None,
                "flake_observation_ids": [],
                "artifact_id": "art-pending",
            }
        )
        self.assertIn("measurement_arithmetic", self._codes(contract))

    def test_owner_resolution_single_rw_default(self) -> None:
        mod = self._module()
        contract = self._contract()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            (root / "repos.yaml").write_text("schema_version: 1\nrepos:\n- name: only\n  grants: {solo: rw}\n", encoding="utf-8")
            (root / "instance.yaml").write_text("manifest_version: 2\nroles:\n  solo: {persona: one, account: one}\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "-c", "user.email=t@example.test", "-c", "user.name=Test", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
            rev = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            contract["repository"] = "only"
            contract["registry_revision"] = rev
            resolved = mod.resolve_repository_owner(contract, root)
        self.assertEqual(resolved.resolved_role, "solo")
        self.assertEqual(resolved.basis, "single-rw-default")

    def test_owner_resolution_multiple_rw_requires_steward_assignment(self) -> None:
        mod = self._module()
        contract = self._contract()
        resolved = mod.resolve_repository_owner(contract, Path.cwd())
        self.assertIsNone(resolved.resolved_role)
        self.assertIn("multiple rw candidates require steward assignment", resolved.issues)
        contract["owner_resolution"]["assignment_authority"] = ""
        resolved = mod.resolve_repository_owner(contract, Path.cwd(), requested_role="orchestrator")
        self.assertIn("multiple rw assignment requires assignment_authority", resolved.issues)

    def test_owner_resolution_refuses_persona_string_and_missing_role(self) -> None:
        contract = self._contract()
        contract["repo_evidence_owner_role"] = "codex"
        contract["owner_resolution"]["requested_role"] = "codex"
        contract["owner_resolution"]["persona"] = "codex"
        codes = self._codes(contract)
        self.assertIn("owner_resolution_failed", codes)
        self.assertIn("owner_persona_not_authority", codes)

    def test_pantheon_config_pilot_derives_inventory_only(self) -> None:
        mod = self._module()
        path = Path("tools/fixtures/repository_evidence/pantheon-config-org083-pilot.input.yaml")
        schema = mod.generate_repository_evidence_schema()
        from_yaml = mod.load_repository_evidence(path)
        canonical_path = Path("tools/fixtures/repository_evidence/pantheon-config-org083-pilot.canonical.json")
        from_canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
        self.assertEqual(_schema_subset_errors(schema, from_yaml, root_schema=schema), [])
        self.assertEqual(_schema_subset_errors(schema, from_canonical, root_schema=schema), [])
        result = mod.check_repository_evidence(path, repo_root=Path.cwd(), now=NOW)
        self.assertEqual(result.derived_maturity, "inventory_only")
        self.assertEqual(result.issues, ())
        self.assertFalse(result.facts.parity_eligible)
        canonical = canonical_path.read_bytes()
        self.assertEqual(mod.canonical_repository_evidence_bytes(result.data), canonical)

    def test_cli_check_outputs_issue_lines_and_exit_codes(self) -> None:
        valid = subprocess.run(
            [
                "python3",
                "tools/repository_evidence.py",
                "check",
                "tools/fixtures/repository_evidence/pantheon-config-org083-pilot.input.yaml",
                "--repo-root",
                ".",
                "--now",
                "2026-09-07T00:00:00Z",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(valid.returncode, 0)
        self.assertIn('"derived_maturity": "inventory_only"', valid.stdout)
        self.assertIn('"issue_count": 0', valid.stdout)
        self.assertEqual(valid.stderr, "")
        malformed = subprocess.run(
            ["python3", "tools/repository_evidence.py", "canonicalize", "tools/repository_evidence.py"],
            text=True,
            capture_output=True,
        )
        self.assertEqual(malformed.returncode, 2)
        self.assertIn("repository_evidence_parse", malformed.stderr)


DIRECT_MUTATIONS = {
    "collector.adapter_id": lambda r: r["collector"].update(adapter_id="other-adapter"),
    "collector.tool_name": lambda r: r["collector"].update(tool_name="other-tool"),
    "collector.tool_version": lambda r: r["collector"].update(tool_version="2"),
    "collector.command_fingerprint": lambda r: r["collector"].update(command_fingerprint="other-command"),
    "collector.config_digest": lambda r: r["collector"].update(config_digest="other-config"),
    "collector.runner_fingerprint": lambda r: r["collector"].update(runner_fingerprint="other-runner"),
    "scope.include_selectors": lambda r: r["scope"].update(include_selectors=["tools/", "docs/"]),
    "scope.exclusion_ids": lambda r: r["scope"].update(exclusion_ids=["ex-other"]),
    "scope.generated_source_ids": lambda r: r["scope"].update(generated_source_ids=["gen-other"]),
    "scope.scope_fingerprint": lambda r: r["scope"].update(scope_fingerprint="other-scope"),
    "dimension": lambda r: r.update(dimension="branch"),
    "unit": lambda r: r.update(unit="ratio"),
}
RUN_LEVEL_KEYS = ("dimension", "unit", "denominator", "branch_semantics", "function_semantics", "test_selection")


class ComparabilityDerivationTests(unittest.TestCase):
    """T-5840: the validator derives comparability changes; producer changed_fields is only a superset claim."""

    _module = RepositoryEvidenceTests._module
    _contract = RepositoryEvidenceTests._contract
    _validate = RepositoryEvidenceTests._validate

    def _pair(
        self,
        *,
        verdict: str = "comparable",
        changed: tuple[str, ...] = (),
        equivalence: bool = False,
        reset: bool = False,
        cur_mutate=None,
        base_kw: dict | None = None,
        cur_kw: dict | None = None,
        mutate_contract=None,
    ) -> dict:
        contract = self._contract()
        base = _measurement("m-base", "line")
        cur = _measurement("m-cur", "line")
        cur["baseline"]["measurement_id"] = "m-base"
        cur["current"]["measurement_id"] = "m-cur"
        cur["comparability"].update(baseline_measurement_id="m-base", current_measurement_id="m-cur", verdict=verdict, changed_fields=list(changed))
        if cur_mutate is not None:
            cur_mutate(cur)
        if equivalence:
            cur["comparability"].update(
                equivalence_artifact_id="art-pending",
                equivalence_decision=_authority_decision("decision-eq", kind="measurement", dimension=cur["dimension"], measurement_id="m-cur"),
            )
        if reset:
            cur["comparability"].update(
                reset_artifact_id="art-pending",
                reset_authority_decision=_authority_decision("decision-reset", kind="measurement", dimension=cur["dimension"], measurement_id="m-cur"),
            )
        _attach_semantics(contract, base, **(base_kw or {}))
        _attach_semantics(contract, cur, **(cur_kw or {}))
        contract["quantitative_measurements"].extend([base, cur])
        if mutate_contract is not None:
            mutate_contract(contract, base, cur)
        return contract

    def _issues(self, contract: dict) -> list:
        return [i for i in self._validate(contract).issues if i.code.startswith("comparability")]

    def _omissions(self, contract: dict) -> list:
        return [i for i in self._issues(contract) if i.path.endswith("/comparability/changed_fields")]

    def _fail_closed(self, contract: dict) -> list:
        return [i for i in self._issues(contract) if i.code == "comparability_reset_required" and "/semantic" in i.path]

    # --- no false positives -------------------------------------------------------------------
    def test_identical_inputs_distinct_ids_have_no_comparability_issue(self) -> None:
        self.assertEqual(self._issues(self._pair()), [])

    def test_self_baseline_still_resolves_and_has_no_issue(self) -> None:
        contract = self._contract()
        record = _measurement("m-self", "line")
        _attach_semantics(contract, record)
        contract["quantitative_measurements"].append(record)
        self.assertEqual(self._issues(contract), [])
        _set_artifact_bytes(next(a for a in contract["artifact_records"] if a["artifact_id"] == "sem-m-self-map"), b"{bad", refresh=True)
        self.assertTrue(self._fail_closed(contract), "self-baseline is not exempt from semantic evidence")

    def test_observations_only_difference_is_not_a_change(self) -> None:
        contract = self._pair(cur_kw={"map_mutator": lambda m: m["observations"].update(junit_node_id_count=99)})
        self.assertEqual(self._issues(contract), [])

    def test_refused_verdict_claims_nothing_and_is_skipped(self) -> None:
        contract = self._pair(verdict="refused", cur_mutate=lambda r: r["comparability"].update(checked_fields=[]))
        for art in contract["artifact_records"]:
            if art["artifact_id"].startswith("sem-m-cur"):
                _set_artifact_bytes(art, b"{bad", refresh=True)
        self.assertEqual(self._issues(contract), [])

    # --- every directly derived record field, mutated independently -----------------------------
    def test_each_direct_field_mutation_is_derived_and_omission_is_flagged(self) -> None:
        for field, mutate in DIRECT_MUTATIONS.items():
            with self.subTest(field=field):
                issues = self._omissions(self._pair(cur_mutate=mutate))
                self.assertEqual([i.code for i in issues], ["comparability_material_change_unapproved"])
                self.assertIn(field, issues[0].message)
                for other in DIRECT_MUTATIONS:
                    if other != field:
                        self.assertNotIn(f"'{other}'", issues[0].message)

    def test_each_direct_field_mutation_is_exactly_that_field_when_authorized(self) -> None:
        for field, mutate in DIRECT_MUTATIONS.items():
            with self.subTest(field=field):
                self.assertEqual(self._issues(self._pair(changed=(field,), equivalence=True, cur_mutate=mutate)), [])
                self.assertEqual(self._issues(self._pair(verdict="reset_required", changed=(field,), reset=True, cur_mutate=mutate)), [])

    def test_each_direct_field_mutation_without_equivalence_is_unapproved(self) -> None:
        for field, mutate in DIRECT_MUTATIONS.items():
            with self.subTest(field=field):
                codes = [(i.code, i.path.rsplit("/", 1)[-1]) for i in self._issues(self._pair(changed=(field,), cur_mutate=mutate))]
                self.assertEqual(codes, [("comparability_material_change_unapproved", "equivalence_decision")])

    # --- every key of the resolved map ------------------------------------------------------------
    def test_each_compared_key_declaration_change_is_derived(self) -> None:
        mod = self._module()
        for key in mod.COMPARABILITY_CHECKED_FIELDS:
            with self.subTest(key=key):
                kw = {"declarations": {key: "REVISED declaration"}}
                issues = self._omissions(self._pair(cur_kw=kw))
                self.assertEqual(len(issues), 1)
                self.assertIn(f"'{key}'", issues[0].message)
                self.assertEqual(self._issues(self._pair(changed=(key,), equivalence=True, cur_kw=kw)), [])

    def test_each_run_level_instance_change_is_derived(self) -> None:
        for key in RUN_LEVEL_KEYS:
            with self.subTest(key=key):
                kw = {"instances": {key: {"changed": key}}}
                issues = self._omissions(self._pair(cur_kw=kw))
                self.assertEqual(len(issues), 1)
                self.assertIn(f"'{key}'", issues[0].message)
                self.assertEqual(self._issues(self._pair(verdict="reset_required", changed=(key,), reset=True, cur_kw=kw)), [])

    def test_bound_instance_disagreeing_with_record_fails_closed(self) -> None:
        mod = self._module()
        for key in mod.COMPARABILITY_CHECKED_FIELDS:
            if key in RUN_LEVEL_KEYS:
                continue
            with self.subTest(key=key):
                self.assertTrue(self._fail_closed(self._pair(cur_kw={"instances": {key: "forged"}})))

    def test_wrong_declaration_instance_shape_fails_closed(self) -> None:
        def drop_instance(m):
            del m["compared"]["unit"]["instance"]
        self.assertTrue(self._fail_closed(self._pair(cur_kw={"map_mutator": drop_instance})))

    # --- producer claim is a conservative superset ------------------------------------------------------
    def test_producer_extra_claim_is_accepted_when_authorized(self) -> None:
        self.assertEqual(self._issues(self._pair(changed=("denominator",), equivalence=True)), [])

    def test_producer_partial_claim_is_flagged_as_omission(self) -> None:
        def two(r):
            r["collector"].update(tool_version="2", config_digest="x")
        issues = self._omissions(self._pair(changed=("collector.tool_version",), equivalence=True, cur_mutate=two))
        self.assertEqual(len(issues), 1)
        self.assertIn("collector.config_digest", issues[0].message)
        self.assertNotIn("collector.tool_version", issues[0].message)

    def test_unauthorized_comparable_with_hand_written_empty_changed_fields(self) -> None:
        issues = self._issues(self._pair(cur_mutate=DIRECT_MUTATIONS["collector.tool_version"]))
        self.assertTrue(issues)
        self.assertTrue(all(i.code == "comparability_material_change_unapproved" for i in issues))
        # The effective claim is (derived U producer): the equivalence requirement bites even though the producer claimed nothing.
        self.assertTrue(any(i.path.endswith("/comparability/equivalence_decision") for i in issues))

    def test_reset_required_without_reset_authority_still_fails(self) -> None:
        codes = {i.code for i in self._issues(self._pair(verdict="reset_required", changed=("denominator",)))}
        self.assertIn("comparability_reset_required", codes)

    # --- fail closed on missing / malformed / tampered / contradictory evidence ---------------------------
    def _art(self, contract: dict, aid: str) -> dict:
        return next(a for a in contract["artifact_records"] if a["artifact_id"] == aid)

    def test_map_bytes_differing_from_manifest_member_digest_fail_closed(self) -> None:
        contract = self._pair()
        art = self._art(contract, "sem-m-cur-map")
        original = base64.b64decode(art["storage_locator"]["value"])
        tampered = original.replace(b"declaration of unit", b"declaration of unix")  # same size: only the sha256 can tell
        self.assertEqual(len(tampered), len(original))
        _set_artifact_bytes(art, tampered, refresh=True)  # record refreshed, manifest member not
        self.assertTrue(self._fail_closed(contract))

    def test_map_bytes_differing_from_artifact_record_digest_fail_closed(self) -> None:
        def retarget(contract, base, cur):
            original = base64.b64decode(self._art(contract, "sem-m-cur-map")["storage_locator"]["value"])
            tampered = original.replace(b"declaration of unit", b"declaration of unix")  # same size: only the digest can tell
            self.assertEqual(len(tampered), len(original))
            self.assertNotEqual(tampered, original)
            manifest = self._art(contract, "sem-m-cur-manifest")
            obj = json.loads(base64.b64decode(manifest["storage_locator"]["value"]))
            obj["members"][0].update(sha256=_sha(tampered), size=len(tampered))
            _set_artifact_bytes(manifest, _sem_json(obj), refresh=True)
            _set_artifact_bytes(self._art(contract, "sem-m-cur-map"), tampered, refresh=False)  # member ok, record digest stale
        self.assertTrue(self._fail_closed(self._pair(mutate_contract=retarget)))

    def test_manifest_bytes_differing_from_artifact_record_digest_fail_closed(self) -> None:
        contract = self._pair()
        _set_artifact_bytes(self._art(contract, "sem-m-cur-manifest"), b"{}\n", refresh=False)
        self.assertTrue(self._fail_closed(contract))

    def test_missing_map_member_fails_closed(self) -> None:
        self.assertTrue(self._fail_closed(self._pair(cur_kw={"manifest_mutator": lambda m: m["members"].clear()})))

    def test_duplicate_map_member_fails_closed(self) -> None:
        self.assertTrue(self._fail_closed(self._pair(cur_kw={"manifest_mutator": lambda m: m["members"].append(dict(m["members"][0]))})))

    def test_wrong_manifest_schema_fails_closed(self) -> None:
        self.assertTrue(self._fail_closed(self._pair(cur_kw={"manifest_mutator": lambda m: m.update(schema="other.v1")})))

    def test_missing_manifest_artifact_record_fails_closed(self) -> None:
        def unlink(contract, base, cur):
            cur["artifact_id"] = "art-does-not-exist"
        self.assertTrue(self._fail_closed(self._pair(mutate_contract=unlink)))

    def test_missing_map_artifact_record_fails_closed(self) -> None:
        def drop(contract, base, cur):
            contract["artifact_records"] = [a for a in contract["artifact_records"] if a["artifact_id"] != "sem-m-cur-map"]
        self.assertTrue(self._fail_closed(self._pair(mutate_contract=drop)))

    def test_unretrievable_bytes_fail_closed(self) -> None:
        contract = self._pair()
        self._art(contract, "sem-m-cur-map")["storage_locator"] = {"kind": "inline", "value": "***not-base64***"}
        self.assertTrue(self._fail_closed(contract))

    def test_malformed_json_fails_closed(self) -> None:
        contract = self._pair()
        _set_artifact_bytes(self._art(contract, "sem-m-cur-map"), b"{not json", refresh=True)
        self.assertTrue(self._fail_closed(contract))

    def test_map_missing_a_compared_key_fails_closed(self) -> None:
        self.assertTrue(self._fail_closed(self._pair(cur_kw={"map_mutator": lambda m: m["compared"].pop("test_selection")})))

    def test_map_with_an_extra_compared_key_fails_closed(self) -> None:
        extra = lambda m: m["compared"].update(extra={"declaration": "x", "instance": 1})
        self.assertTrue(self._fail_closed(self._pair(cur_kw={"map_mutator": extra})))

    def test_baseline_side_evidence_is_also_required(self) -> None:
        contract = self._pair(base_kw={"manifest_mutator": lambda m: m["members"].clear()})
        # The CURRENT record (index 1) must fail closed because its baseline side is unverifiable, not merely the baseline's own record.
        self.assertTrue([i for i in self._fail_closed(contract) if i.path.startswith("/quantitative_measurements/1/")])

    def test_manifest_adapter_id_contradicting_record_fails_closed(self) -> None:
        self.assertTrue(self._fail_closed(self._pair(cur_kw={"manifest_adapter_id": "someone-else"})))

    def test_map_adapter_id_contradicting_record_fails_closed(self) -> None:
        self.assertTrue(self._fail_closed(self._pair(cur_kw={"map_adapter_id": "someone-else"})))

    def test_current_side_manifest_revision_mismatch_fails_closed(self) -> None:
        contract = self._pair(cur_kw={"manifest_revision": "0" * 40})
        self.assertTrue([i for i in self._fail_closed(contract) if i.path.startswith("/quantitative_measurements/1/")])

    def test_self_baseline_manifest_revision_mismatch_fails_closed(self) -> None:
        contract = self._contract()
        record = _measurement("m-self", "line")
        _attach_semantics(contract, record, manifest_revision="0" * 40)
        contract["quantitative_measurements"].append(record)
        issues = self._issues(contract)
        self.assertEqual(
            [(i.code, i.path) for i in issues],
            [("comparability_reset_required", "/quantitative_measurements/0/comparability/semantic_evidence")],
        )

    # --- every side binds its own record, whatever the verdict or authority (T-5840 REBOUND 2) ---------------
    CUR_PATH = "/quantitative_measurements/1/comparability/semantic_evidence"

    @staticmethod
    def _refuse_base(contract, base, cur):
        base["comparability"]["verdict"] = "refused"  # never derived itself

    def _assert_cur_fails_closed(self, contract: dict) -> None:
        hits = [(i.code, i.path) for i in self._issues(contract)]
        self.assertIn(("comparability_reset_required", self.CUR_PATH), hits)

    def test_consultant_probe_refused_baseline_with_stale_manifest_revision_fails_current_row(self) -> None:
        contract = self._pair(base_kw={"manifest_revision": "0" * 40}, mutate_contract=self._refuse_base)
        self.assertEqual(contract["quantitative_measurements"][0]["comparability"]["verdict"], "refused")
        self.assertEqual(contract["quantitative_measurements"][1]["comparability"]["verdict"], "comparable")
        self.assertEqual(
            [(i.code, i.path) for i in self._issues(contract)],
            [("comparability_reset_required", self.CUR_PATH)],
        )

    def test_refused_baseline_with_bad_adapter_id_fails_current_row(self) -> None:
        for kw in ({"manifest_adapter_id": "someone-else"}, {"map_adapter_id": "someone-else"}):
            with self.subTest(kw=kw):
                self._assert_cur_fails_closed(self._pair(base_kw=kw, mutate_contract=self._refuse_base))

    def test_refused_baseline_with_bad_instance_fails_current_row(self) -> None:
        mod = self._module()
        for key in mod.COMPARABILITY_CHECKED_FIELDS:
            if key in RUN_LEVEL_KEYS:
                continue
            with self.subTest(key=key):
                self._assert_cur_fails_closed(self._pair(base_kw={"instances": {key: "forged"}}, mutate_contract=self._refuse_base))

    def test_reset_required_current_row_with_stale_baseline_revision_fails_closed_despite_reset_authority(self) -> None:
        contract = self._pair(verdict="reset_required", changed=("denominator",), reset=True,
                              base_kw={"manifest_revision": "0" * 40}, mutate_contract=self._refuse_base)
        self._assert_cur_fails_closed(contract)

    def test_equivalence_authorized_current_row_with_stale_baseline_revision_fails_closed(self) -> None:
        contract = self._pair(changed=("denominator",), equivalence=True,
                              base_kw={"manifest_revision": "0" * 40}, mutate_contract=self._refuse_base)
        self._assert_cur_fails_closed(contract)

    def test_equivalence_authorized_current_row_with_own_stale_revision_fails_closed(self) -> None:
        self._assert_cur_fails_closed(self._pair(changed=("denominator",), equivalence=True, cur_kw={"manifest_revision": "0" * 40}))

    def test_reset_authorized_current_row_with_own_stale_revision_fails_closed(self) -> None:
        self._assert_cur_fails_closed(
            self._pair(verdict="reset_required", changed=("denominator",), reset=True, cur_kw={"manifest_revision": "0" * 40})
        )

    def test_authority_never_rescues_baseline_adapter_or_instance_contradiction(self) -> None:
        for base_kw in ({"manifest_adapter_id": "someone-else"}, {"instances": {"collector.tool_name": "forged"}}):
            with self.subTest(base_kw=base_kw):
                self._assert_cur_fails_closed(
                    self._pair(changed=("denominator",), equivalence=True, base_kw=base_kw, mutate_contract=self._refuse_base)
                )
                self._assert_cur_fails_closed(
                    self._pair(verdict="reset_required", changed=("denominator",), reset=True, base_kw=base_kw, mutate_contract=self._refuse_base)
                )

    def test_equivalence_authorized_current_row_with_own_adapter_or_instance_contradiction_fails_closed(self) -> None:
        for cur_kw in ({"map_adapter_id": "someone-else"}, {"instances": {"scope.scope_fingerprint": "forged"}}):
            with self.subTest(cur_kw=cur_kw):
                self._assert_cur_fails_closed(self._pair(changed=("denominator",), equivalence=True, cur_kw=cur_kw))

    # --- owner ruling (a): only manifests used as comparison evidence are resolved ------------------------------
    def test_unreferenced_refused_row_stays_unresolved_and_makes_no_claim(self) -> None:
        cases = {
            "missing manifest ArtifactRecord": lambda c, r: r.update(artifact_id="art-gone"),
            "stale revision": lambda c, r: _attach_semantics(c, r, manifest_revision="0" * 40),
            "mismatched adapter_id": lambda c, r: _attach_semantics(c, r, manifest_adapter_id="someone-else"),
            "mismatched instance": lambda c, r: _attach_semantics(c, r, instances={"collector.tool_name": "forged"}),
        }
        for name, break_evidence in cases.items():
            with self.subTest(case=name):
                contract = self._contract()
                record = _measurement("m-refused", "line")
                record["comparability"]["verdict"] = "refused"
                break_evidence(contract, record)
                contract["quantitative_measurements"].append(record)
                self.assertEqual(self._issues(contract), [])

    def test_same_refused_row_fails_closed_once_a_comparable_row_references_it(self) -> None:
        cases = {
            "stale revision": dict(base_kw={"manifest_revision": "0" * 40}),
            "missing manifest ArtifactRecord": dict(mutate_contract=lambda c, b, r: (self._refuse_base(c, b, r), b.update(artifact_id="art-gone"))),
            "mismatched adapter_id": dict(base_kw={"manifest_adapter_id": "someone-else"}),
            "mismatched instance": dict(base_kw={"instances": {"scope.scope_fingerprint": "forged"}}),
        }
        for name, kwargs in cases.items():
            with self.subTest(case=name):
                kwargs = dict(kwargs)
                kwargs.setdefault("mutate_contract", self._refuse_base)
                contract = self._pair(**kwargs)
                self.assertEqual(contract["quantitative_measurements"][0]["comparability"]["verdict"], "refused")
                self._assert_cur_fails_closed(contract)

    def test_distinct_baseline_and_current_sharing_one_manifest_fail_closed(self) -> None:
        def share(contract, base, cur):
            cur["artifact_id"] = base["artifact_id"]
        self.assertTrue(self._fail_closed(self._pair(mutate_contract=share)))

    def test_baseline_id_not_in_index_fails_closed(self) -> None:
        def dangling(contract, base, cur):
            cur["comparability"]["baseline_measurement_id"] = "m-missing"
            cur["baseline"]["measurement_id"] = "m-missing"
        self.assertTrue(self._fail_closed(self._pair(mutate_contract=dangling)))

    def test_record_baseline_disagreeing_with_comparability_baseline_fails_closed(self) -> None:
        def disagree(contract, base, cur):
            cur["baseline"]["measurement_id"] = "m-cur"
        self.assertTrue(self._fail_closed(self._pair(mutate_contract=disagree)))

    def test_comparability_current_id_not_the_record_fails_closed(self) -> None:
        def disagree(contract, base, cur):
            # Fully self-consistent ids that merely point at the OTHER record: only the record-id check can tell.
            cur["comparability"]["current_measurement_id"] = "m-base"
            cur["current"]["measurement_id"] = "m-base"
        contract = self._pair(mutate_contract=disagree)
        self.assertTrue([i for i in self._fail_closed(contract) if i.path.startswith("/quantitative_measurements/1/")])


class SelfBaselineStageTests(unittest.TestCase):
    """T-6044: a self-baseline may derive `baseline` but never satisfies the distinct comparable evidence above it.

    Distinctness is checked at id and content level only; run lineage is T-6090.
    """

    _module = RepositoryEvidenceTests._module
    _contract = RepositoryEvidenceTests._contract
    _validate = RepositoryEvidenceTests._validate
    _make_baseline_ready = RepositoryEvidenceTests._make_baseline_ready

    def _self_ready(self) -> dict:
        contract = self._contract()
        self._make_baseline_ready(contract)
        return contract

    def _distinct_ready(self) -> dict:
        contract = self._self_ready()
        for cur in list(contract["quantitative_measurements"]):
            base = _measurement(f"base-{cur['dimension']}", cur["dimension"])
            cur["baseline"]["measurement_id"] = base["measurement_id"]
            cur["comparability"]["baseline_measurement_id"] = base["measurement_id"]
            # Honest rerun: byte-identical resolved maps; the sides differ via their nonce-bearing member ids.
            _attach_semantics(contract, base, retain=True)
            contract["quantitative_measurements"].append(base)
        return contract

    def _artifact_by_id(self, contract: dict, artifact_id: str) -> dict:
        return next(a for a in contract["artifact_records"] if a["artifact_id"] == artifact_id)

    def _alias_current(self, contract: dict, rewrap, *, dimensions=("line", "branch"), edit_map: bool = False) -> None:
        """Re-point each current row at a NEW artifact id carrying a re-wrapped copy of its baseline's manifest."""
        for cur in contract["quantitative_measurements"]:
            dim = cur["dimension"]
            if not cur["measurement_id"].startswith("measure-") or dim not in dimensions:
                continue
            base_manifest = self._artifact_by_id(contract, f"sem-base-{dim}-manifest")
            raw = base64.b64decode(base_manifest["storage_locator"]["value"])
            extra: list[dict] = []
            if edit_map:
                manifest = json.loads(raw)
                member = manifest["members"][0]
                map_obj = json.loads(base64.b64decode(self._artifact_by_id(contract, member["artifact_id"])["storage_locator"]["value"]))
                map_obj["observations"]["junit_node_id_count"] = 12345  # not a compared field
                map_bytes = _sem_json(map_obj)
                member.update(artifact_id=f"alias-{dim}-map", sha256=_sha(map_bytes), size=len(map_bytes))
                extra.append(_artifact(f"alias-{dim}-map", map_bytes))
                raw = _sem_json(manifest)
            alias = _artifact(f"alias-{dim}-manifest", rewrap(raw))
            own = f"sem-{cur['measurement_id']}"
            contract["artifact_records"] = [a for a in contract["artifact_records"] if not a["artifact_id"].startswith(own)] + [alias, *extra]
            contract["retention_records"] = [r for r in contract["retention_records"] if not r["artifact_id"].startswith(own)]
            contract["retention_records"].append(_retention_for(contract, alias))
            cur["artifact_id"] = alias["artifact_id"]

    def _promote_everything_else(self, facts):
        import dataclasses

        flags = {
            f.name: True
            for f in dataclasses.fields(facts)
            if f.type in (bool, "bool") and f.name != "above_baseline_comparison_distinct"
        }
        return self._module().derive_repository_maturity(dataclasses.replace(facts, **flags))

    def test_valid_first_self_baseline_still_derives_baseline(self) -> None:
        result = self._validate(self._self_ready())
        self.assertEqual(result.issues, ())
        self.assertEqual(result.derived_maturity, "baseline")
        self.assertFalse(result.facts.above_baseline_comparison_distinct)

    def test_self_baseline_cannot_promote_with_every_other_fact_satisfied(self) -> None:
        result = self._validate(self._self_ready())
        self.assertEqual(self._promote_everything_else(result.facts), "baseline")

    def test_distinct_comparable_pair_satisfies_the_gate(self) -> None:
        contract = self._distinct_ready()
        self.assertEqual(
            self._artifact_by_id(contract, "sem-base-line-map")["digest"],
            self._artifact_by_id(contract, "sem-measure-line-map")["digest"],
        )
        result = self._validate(contract)
        self.assertEqual(result.issues, ())
        self.assertEqual(result.derived_maturity, "baseline")
        self.assertTrue(result.facts.above_baseline_comparison_distinct)
        self.assertEqual(self._promote_everything_else(result.facts), "enforcing")

    def test_borrowed_manifest_rewrapped_under_new_artifact_id_is_refused(self) -> None:
        rewraps = {
            "byte copy": lambda b: b,
            "whitespace": lambda b: json.dumps(json.loads(b), indent=4).encode("utf-8"),
            "key order": lambda b: json.dumps(dict(reversed(list(json.loads(b).items())))).encode("utf-8"),
        }
        for name, rewrap in rewraps.items():
            with self.subTest(rewrap=name):
                contract = self._distinct_ready()
                self._alias_current(contract, rewrap)
                result = self._validate(contract)
                self.assertEqual(result.issues, ())
                self.assertEqual(result.derived_maturity, "baseline")
                self.assertFalse(result.facts.above_baseline_comparison_distinct)
                self.assertEqual(self._promote_everything_else(result.facts), "baseline")

    def test_one_aliased_dimension_is_enough_to_refuse(self) -> None:
        contract = self._distinct_ready()
        self._alias_current(contract, lambda b: b, dimensions=("branch",))
        self.assertFalse(self._validate(contract).facts.above_baseline_comparison_distinct)

    def test_non_finite_number_in_current_manifest_fails_closed_without_raising(self) -> None:
        contract = self._distinct_ready()
        for cur in contract["quantitative_measurements"]:
            if not cur["measurement_id"].startswith("measure-"):
                continue
            dim = cur["dimension"]
            raw = base64.b64decode(self._artifact_by_id(contract, f"sem-base-{dim}-manifest")["storage_locator"]["value"])
            self.assertTrue(raw.endswith(b"}\n"))
            forged = _artifact(f"forged-{dim}-manifest", raw[:-2] + b',"x":1e999}\n')
            own = f"sem-{cur['measurement_id']}"
            contract["artifact_records"] = [a for a in contract["artifact_records"] if not a["artifact_id"].startswith(own)] + [forged]
            contract["retention_records"] = [r for r in contract["retention_records"] if not r["artifact_id"].startswith(own)]
            contract["retention_records"].append(_retention_for(contract, forged))
            cur["artifact_id"] = forged["artifact_id"]
        result = self._validate(contract)  # must not raise
        self.assertFalse(result.facts.above_baseline_comparison_distinct)
        self.assertEqual(result.derived_maturity, "baseline")
        self.assertEqual(self._promote_everything_else(result.facts), "baseline")

    def test_measured_dimension_without_measurements_is_not_vacuously_satisfied(self) -> None:
        contract = self._distinct_ready()
        for record in contract["dimension_dispositions"]:
            if record["dimension"] == "branch":
                record["measurement_ids"] = []
        self.assertFalse(self._validate(contract).facts.above_baseline_comparison_distinct)

    @unittest.expectedFailure  # T-6090: no run lineage in manifest (flips loudly when lineage lands)
    def test_borrowed_manifest_with_edited_member_and_map_is_refused(self) -> None:
        contract = self._distinct_ready()
        self._alias_current(contract, lambda b: b, edit_map=True)
        self.assertFalse(self._validate(contract).facts.above_baseline_comparison_distinct)


if __name__ == "__main__":
    unittest.main()
