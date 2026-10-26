"""Repository evidence contract validation for ORG-083.

This module implements only the local normalized-input adapter and validator.
It does not collect measurements, select fleet tooling, enforce thresholds, or
promote any repository outside the checked contract bytes.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import math
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - repository dependency guard
    yaml = None  # type: ignore[assignment]
    _YAML_IMPORT_ERROR = exc
else:
    _YAML_IMPORT_ERROR = None


SCHEMA_ID = "org083.repository-evidence-contract.v1"
NORMALIZED_INPUT_SCHEMA = "org083.normalized-input.v1"
CONTRACT_SCHEMA_VERSION = 1
ADR_PATH = "architecture/adr/ORG-083-code-coverage-governance-and-test-effectiveness-evidence.md"

MATURITY_ORDER = ("inventory_only", "baseline", "advisory", "enforcing")
REPO_CLASSES = ("live_code", "live_noncode", "retired", "ignored")
DIMENSIONS = ("line", "branch", "function", "changed_code", "mutation", "flake")
BOUNDARY_KINDS = (
    "production_subprocess",
    "shell_suite",
    "producer_consumer",
    "ipc",
    "network",
    "storage",
    "external_api",
    "ui_e2e",
)
COLLECTION_ORDER = (
    "dimension_dispositions",
    "quantitative_measurements",
    "changed_code_records",
    "mutation_records",
    "flake_observations",
    "boundary_records",
    "retry_policy_records",
    "deploy_tolerance_records",
    "test_surface_gaps",
    "effectiveness_receipts",
    "efficiency_records",
    "parity_records",
    "retention_records",
    "generated_source_records",
    "exclusion_records",
    "waiver_records",
    "maturity_deferrals",
    "quarantine_records",
    "artifact_records",
)
ROOT_ORDER = (
    "schema_version",
    "contract_id",
    "repository",
    "registry_revision",
    "evidence_revision",
    "collected_at",
    "repo_class",
    "arc_author",
    "fleet_steward_role",
    "repo_evidence_owner_role",
    "owner_resolution",
    "maturity",
    "target_maturity",
    "next_review",
    "inventory",
    *COLLECTION_ORDER,
)
ROOT_REQUIRED = frozenset(ROOT_ORDER)
ROOT_ALLOWED = ROOT_REQUIRED

ID_FIELDS = {
    "dimension_dispositions": "disposition_id",
    "quantitative_measurements": "measurement_id",
    "changed_code_records": "changed_code_record_id",
    "mutation_records": "mutation_record_id",
    "flake_observations": "flake_observation_id",
    "boundary_records": "boundary_record_id",
    "retry_policy_records": "retry_policy_id",
    "deploy_tolerance_records": "deploy_tolerance_id",
    "test_surface_gaps": "test_surface_gap_id",
    "effectiveness_receipts": "effectiveness_receipt_id",
    "efficiency_records": "efficiency_id",
    "parity_records": "parity_id",
    "retention_records": "retention_id",
    "generated_source_records": "generated_source_id",
    "exclusion_records": "exclusion_id",
    "waiver_records": "waiver_id",
    "maturity_deferrals": "maturity_deferral_id",
    "quarantine_records": "quarantine_id",
    "artifact_records": "artifact_id",
}
COLLECTION_FOR_ID = {value: key for key, value in ID_FIELDS.items()}

RECORD_ALLOWED_FIELDS = {
    "dimension_dispositions": frozenset(
        (
            "disposition_id",
            "repository",
            "revision",
            "dimension",
            "status",
            "authority",
            "reason",
            "scope",
            "effective_revision",
            "measurement_ids",
            "changed_code_record_id",
            "mutation_record_id",
            "flake_observation_ids",
            "deferral_id",
            "exclusion_id",
            "applicability_evidence_ids",
            "steward_approval",
            "applicability_decision",
            "approved_disposition_decision",
            "re_evaluate_trigger",
        )
    ),
    "quantitative_measurements": frozenset(
        (
            "measurement_id",
            "repository",
            "revision",
            "collected_at",
            "dimension",
            "unit",
            "collector",
            "scope",
            "numerator",
            "denominator",
            "value",
            "baseline",
            "current",
            "threshold",
            "ratchet",
            "comparability",
            "changed_code_record_id",
            "mutation_record_id",
            "flake_observation_ids",
            "artifact_id",
        )
    ),
    "changed_code_records": frozenset(
        (
            "changed_code_record_id",
            "repository",
            "revision",
            "base_ref",
            "base_revision",
            "head_ref",
            "head_revision",
            "merge_base_revision",
            "diff_tool",
            "diff_artifact_id",
            "eligible_units",
            "excluded_units",
            "exclusion_ids",
            "covered_eligible_units",
            "denominator_rule",
        )
    ),
    "mutation_records": frozenset(
        (
            "mutation_record_id",
            "repository",
            "revision",
            "tool",
            "operator_scope",
            "operator_set_digest",
            "seed",
            "test_selection",
            "timeout_policy",
            "artifact_id",
            "counts",
            "terminal_dispositions",
        )
    ),
    "flake_observations": frozenset(
        (
            "flake_observation_id",
            "repository",
            "revision",
            "environment_fingerprint",
            "runner_fingerprint",
            "test_set_fingerprint",
            "observation_window",
            "run_count",
            "pass_count",
            "fail_count",
            "sequence_artifact_id",
            "failure_signatures",
            "quarantine_id",
            "retry_policy_id",
            "numerator",
            "denominator",
            "rate",
            "artifact_id",
        )
    ),
    "boundary_records": frozenset(
        (
            "boundary_record_id",
            "repository",
            "revision",
            "boundary_kind",
            "producer",
            "transport",
            "consumer",
            "status",
            "tests",
            "preconditions",
            "asserted_effect",
            "artifact_ids",
            "owner",
            "deferral_id",
            "applicability_evidence_ids",
            "steward_approval",
            "applicability_decision",
            "approved_disposition_decision",
        )
    ),
    "retry_policy_records": frozenset(
        (
            "retry_policy_id",
            "repository",
            "revision",
            "command_fingerprint",
            "test_set_fingerprint",
            "policy_source",
            "max_attempts",
            "effect_on_evidence",
            "linked_flake_ids",
        )
    ),
    "deploy_tolerance_records": frozenset(
        (
            "deploy_tolerance_id",
            "repository",
            "revision",
            "workflow",
            "step",
            "command_identity",
            "approver",
            "started_at",
            "expiry",
            "trigger",
            "restoration_plan",
            "effect_on_evidence",
            "evidence_artifact_id",
        )
    ),
    "test_surface_gaps": frozenset(
        (
            "test_surface_gap_id",
            "repository",
            "revision",
            "affected_scope",
            "owner",
            "evidence_ids",
            "downstream_ticket",
            "started_at",
            "review_trigger",
            "target_state",
            "compensating_evidence_ids",
            "deferral_id",
        )
    ),
    "effectiveness_receipts": frozenset(
        (
            "effectiveness_receipt_id",
            "repository",
            "revision",
            "kind",
            "reviewed_at",
            "reviewer",
            "scope",
            "limitations",
            "payload",
            "review_decision",
            "independent_review_decision",
        )
    ),
    "efficiency_records": frozenset(
        (
            "efficiency_id",
            "repository",
            "revision",
            "environment_fingerprint",
            "command_fingerprint",
            "test_set_fingerprint",
            "observation_window",
            "sample_count",
            "costs",
            "cadence",
            "comparison_baseline_id",
            "comparability",
            "artifact_id",
            "limitations",
        )
    ),
    "parity_records": frozenset(
        (
            "parity_id",
            "repository",
            "revision",
            "verdict",
            "local_command",
            "ci_command",
            "local_environment",
            "ci_environment",
            "test_set_fingerprint",
            "workflow_revision",
            "artifact_ids",
            "differences",
            "equivalence_evidence_id",
            "owner",
            "waiver_id",
            "restoration_plan",
        )
    ),
    "retention_records": frozenset(
        (
            "retention_id",
            "repository",
            "revision",
            "artifact_id",
            "artifact_class",
            "created_at",
            "storage_locator",
            "digest_algorithm",
            "digest",
            "availability",
            "retrieval_receipts",
            "retention_end",
            "expiry_action",
            "tombstone_artifact_id",
            "owner",
            "superseded_by",
        )
    ),
    "generated_source_records": frozenset(
        (
            "generated_source_id",
            "repository",
            "revision",
            "path_selectors",
            "source_of_truth",
            "generator",
            "generated_revision",
            "generated_digest",
            "risk_acceptor",
            "affected_dimensions",
            "treatment",
            "reason",
            "exclusion_id",
            "re_evaluate_trigger",
        )
    ),
    "exclusion_records": frozenset(
        (
            "exclusion_id",
            "repository",
            "revision",
            "owner",
            "reviewer",
            "approver",
            "reason",
            "scope",
            "started_at",
            "expiry",
            "restoration_condition",
            "restoration_plan",
            "compensating_evidence_ids",
            "decision_ref",
            "active_state",
            "expiry_action",
            "authority_decision",
            "affected_measurement_ids",
        )
    ),
    "waiver_records": frozenset(
        (
            "waiver_id",
            "repository",
            "revision",
            "owner",
            "reviewer",
            "approver",
            "reason",
            "scope",
            "started_at",
            "expiry",
            "restoration_condition",
            "restoration_plan",
            "compensating_evidence_ids",
            "decision_ref",
            "active_state",
            "expiry_action",
            "authority_decision",
            "independent_review_decision",
            "obligation",
            "fallback",
        )
    ),
    "maturity_deferrals": frozenset(
        (
            "maturity_deferral_id",
            "repository",
            "revision",
            "owner",
            "reviewer",
            "approver",
            "reason",
            "scope",
            "started_at",
            "expiry",
            "restoration_condition",
            "restoration_plan",
            "compensating_evidence_ids",
            "decision_ref",
            "active_state",
            "expiry_action",
            "authority_decision",
            "current_stage",
            "target_stage",
            "blocked_prerequisite",
            "unsupported_dimensions",
            "downstream_ticket",
            "next_action",
            "review_trigger",
        )
    ),
    "quarantine_records": frozenset(
        (
            "quarantine_id",
            "repository",
            "revision",
            "owner",
            "reviewer",
            "approver",
            "reason",
            "scope",
            "started_at",
            "expiry",
            "restoration_condition",
            "restoration_plan",
            "compensating_evidence_ids",
            "decision_ref",
            "active_state",
            "expiry_action",
            "authority_decision",
            "classification",
            "tests",
            "evidence_ids",
            "flake_observation_id",
            "retry_policy_id",
            "coverage_impact",
            "boundary_impact",
        )
    ),
    "artifact_records": frozenset(
        (
            "artifact_id",
            "repository",
            "revision",
            "artifact_class",
            "created_at",
            "producer",
            "tool",
            "config_identity",
            "storage_locator",
            "media_type",
            "digest_algorithm",
            "digest",
            "size",
            "sensitivity",
            "access",
            "tombstone",
            "payload",
        )
    ),
}

AUTHORITY_SCOPE_KINDS = (
    "repository",
    "dimension",
    "boundary",
    "measurement",
    "obligation",
    "enforcement",
)
ACTIVE_STATES = ("active", "inactive", "expired", "indeterminate")
RETENTION_AVAILABILITY_STATES = ("verified_available", "unavailable", "deleted", "tombstoned")
EXPIRY_ACTIONS = ("fleet_drift", "revert_to_inventory", "fail_closed", "retain_tombstone", "review_required")
WAIVER_FALLBACKS = ("fail_closed", "revert_to_advisory", "revert_to_inventory")
THRESHOLD_COMPARATORS = ("gte", "lte", "eq")
RATCHET_DIRECTIONS = ("nondecreasing", "nonincreasing", "bounded")
COST_CLASSES = (
    "local_setup",
    "local_runtime",
    "ci_queue",
    "ci_runner",
    "rerun",
    "mutation",
    "cpu",
    "monetary",
)
COMPARABILITY_CHECKED_FIELDS = (
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
)
ISSUE_FACT_BLOCKERS = {
    "missing_root_field": ("structural_valid", "above_inventory"),
    "unknown_root_field": ("structural_valid", "above_inventory"),
    "schema_version": ("structural_valid", "above_inventory"),
    "repo_class": ("structural_valid", "above_inventory"),
    "maturity": ("structural_valid", "above_inventory"),
    "target_maturity": ("structural_valid", "above_inventory"),
    "inventory": ("structural_valid", "above_inventory"),
    "inventory_field": ("structural_valid", "above_inventory"),
    "collection_not_list": ("structural_valid", "above_inventory"),
    "collection_missing": ("structural_valid", "above_inventory"),
    "record_not_object": ("structural_valid", "above_inventory"),
    "unknown_record_field": ("structural_valid", "above_inventory"),
    "missing_id": ("structural_valid", "above_inventory"),
    "duplicate_id": ("structural_valid", "above_inventory"),
    "typed_ref_not_string": ("structural_valid", "above_inventory"),
    "typed_ref_unresolved": ("structural_valid", "above_inventory"),
    "repository_mismatch": ("structural_valid", "above_inventory"),
    "revision_mismatch": ("structural_valid", "above_inventory"),
    "exception_repository_revision": ("structural_valid", "above_inventory"),
    "owner_resolution_failed": ("authority_valid", "owner_resolved", "above_inventory"),
    "owner_resolution_missing": ("authority_valid", "owner_resolved", "above_inventory"),
    "owner_resolution_mismatch": ("authority_valid", "owner_resolved", "above_inventory"),
    "owner_persona_not_authority": ("authority_valid", "owner_resolved", "above_inventory"),
    "authority_decision_shape": ("role_decisions_valid", "above_inventory"),
    "authority_decision_role_unknown": ("role_decisions_valid", "above_inventory"),
    "authority_decision_role_mismatch": ("role_decisions_valid", "above_inventory"),
    "authority_decision_artifact_unretained": ("role_decisions_valid", "stage_artifacts_retained", "above_inventory"),
    "independent_reviewer_missing": ("independent_review_valid", "above_baseline"),
    "independent_reviewer_not_distinct": ("independent_review_valid", "above_baseline"),
    "artifact_locator_missing": ("artifact_refs_resolved", "above_inventory"),
    "artifact_external_not_retrieved": ("artifact_refs_resolved", "above_inventory"),
    "artifact_unsafe_locator": ("artifact_refs_resolved", "above_inventory"),
    "artifact_unretrieved": ("artifact_refs_resolved", "above_inventory"),
    "artifact_digest_algorithm": ("artifact_refs_resolved", "above_inventory"),
    "artifact_digest_mismatch": ("artifact_refs_resolved", "above_inventory"),
    "artifact_size_mismatch": ("artifact_refs_resolved", "above_inventory"),
    "support_artifact_missing": ("artifact_refs_resolved", "stage_artifacts_retained", "above_inventory"),
    "support_artifact_unretained": ("stage_artifacts_retained", "above_inventory"),
    "support_retention_mismatch": ("stage_artifacts_retained", "retention_valid", "above_inventory"),
    "retention_shape": ("retention_valid", "above_inventory"),
    "retention_unavailable": ("retention_valid", "above_inventory"),
    "retention_receipt_unresolved": ("retention_valid", "above_inventory"),
    "retention_expired": ("retention_valid", "above_inventory"),
    "retention_end": ("retention_valid", "above_inventory"),
    "retention_not_exactly_one": ("retention_valid", "stage_artifacts_retained", "above_inventory"),
    "exception_field": ("exceptions_live", "above_inventory"),
    "exception_not_live": ("exceptions_live", "above_inventory"),
    "event_pending_unproved": ("exceptions_live", "above_inventory"),
    "deferral_not_live": ("exceptions_live", "above_inventory"),
    "exclusion_not_live": ("exceptions_live", "above_inventory"),
    "boundary_deferral_not_live": ("exceptions_live", "above_inventory"),
    "generated_exclusion_not_live": ("exceptions_live", "above_inventory"),
    "parity_waiver_not_live": ("exceptions_live", "parity_eligible", "above_inventory"),
    "dimension_unknown": ("measurement_prerequisites", "above_inventory"),
    "dimension_status_unknown": ("measurement_prerequisites", "above_inventory"),
    "dimension_missing": ("measurement_prerequisites", "above_inventory"),
    "measured_missing_measurement": ("measurement_prerequisites", "above_inventory"),
    "measured_missing_flake": ("measurement_prerequisites", "above_inventory"),
    "not_applicable_unproved": ("measurement_prerequisites", "above_inventory"),
    "baseline_measurement_missing": ("measurement_prerequisites", "above_inventory"),
    "non_measured_dimension_authority_missing": ("measurement_prerequisites", "above_inventory"),
    "measurement_arithmetic": ("comparability_valid", "measurement_prerequisites", "above_inventory"),
    "baseline_current_mismatch": ("comparability_valid", "measurement_prerequisites", "above_inventory"),
    "comparability_reset_required": ("comparability_valid", "above_inventory"),
    "comparability_checked_fields_missing": ("comparability_valid", "above_inventory"),
    "comparability_material_change_unapproved": ("comparability_valid", "above_inventory"),
    "mutation_counts": ("measurement_prerequisites", "above_inventory"),
    "mutation_terminal_count": ("measurement_prerequisites", "above_inventory"),
    "mutation_outcome_count": ("measurement_prerequisites", "above_inventory"),
    "flake_counts": ("measurement_prerequisites", "above_inventory"),
    "flake_rate": ("measurement_prerequisites", "above_inventory"),
    "flake_not_inconsistent": ("measurement_prerequisites", "above_inventory"),
    "boundary_kind": ("boundary_prerequisites", "above_inventory"),
    "boundary_status": ("boundary_prerequisites", "above_inventory"),
    "boundary_missing": ("boundary_prerequisites", "above_inventory"),
    "boundary_not_applicable_unproved": ("boundary_prerequisites", "above_inventory"),
    "boundary_disposition_authority_missing": ("boundary_prerequisites", "above_inventory"),
    "retry_policy": ("structural_valid", "above_inventory"),
    "deploy_tolerance_field": ("structural_valid", "above_inventory"),
    "generated_treatment": ("generated_treatment_valid", "above_inventory"),
    "parity_verdict": ("parity_eligible", "above_inventory"),
    "effectiveness_payload": ("positive_effectiveness_or_waiver", "above_baseline"),
    "effectiveness_kind": ("positive_effectiveness_or_waiver", "above_baseline"),
    "effectiveness_payload_field": ("positive_effectiveness_or_waiver", "above_baseline"),
    "effectiveness_not_positive": ("positive_effectiveness_or_waiver", "above_baseline"),
    "effectiveness_not_independent": ("positive_effectiveness_or_waiver", "independent_review_valid", "above_baseline"),
    "effectiveness_artifact_unretained": ("positive_effectiveness_or_waiver", "stage_artifacts_retained", "above_baseline"),
    "effectiveness_waiver_not_live": ("positive_effectiveness_or_waiver", "above_baseline"),
    "waiver_scope_invalid": ("positive_effectiveness_or_waiver", "above_baseline"),
    "waiver_compensation_missing": ("positive_effectiveness_or_waiver", "stage_artifacts_retained", "above_baseline"),
    "waiver_authority_invalid": ("positive_effectiveness_or_waiver", "role_decisions_valid", "above_baseline"),
    "waiver_independent_review_invalid": ("positive_effectiveness_or_waiver", "independent_review_valid", "above_baseline"),
    "waiver_fallback_mismatch": ("positive_effectiveness_or_waiver", "exceptions_live", "above_baseline"),
    "efficiency_sample_count": ("efficiency_complete", "above_baseline"),
    "cost_class_missing": ("efficiency_complete", "above_baseline"),
    "cost_class_invalid": ("efficiency_complete", "above_baseline"),
    "threshold_proposal_missing": ("threshold_proposed", "above_baseline"),
    "threshold_approval_missing": ("threshold_approved", "above_baseline"),
    "threshold_role_mismatch": ("threshold_proposed", "threshold_approved", "above_baseline"),
    "ratchet_proposal_missing": ("ratchet_proposed", "above_baseline"),
    "dry_run_missing": ("dry_run_retained", "above_baseline"),
    "owner_proposal_missing": ("owner_proposal_valid", "above_baseline"),
    "steward_adoption_missing": ("steward_adoption_valid", "above_baseline"),
    "ratchet_adoption_missing": ("ratchet_adopted", "above_advisory"),
    "current_evidence_missing": ("current_evidence_valid", "above_advisory"),
    "enforcement_artifact_missing": ("enforcement_machinery_retained", "above_advisory"),
    "enforcement_payload_invalid": ("enforcement_machinery_retained", "above_advisory"),
    "enforcement_approval_missing": ("enforcement_machinery_retained", "above_advisory"),
    "failure_behavior_unproved": ("enforcement_failure_proof_retained", "above_advisory"),
    "coverage_only_enforcement": ("enforcement_failure_proof_retained", "above_advisory"),
    "maturity_claim_exceeds_derived": ("claim_valid",),
}


class RepositoryEvidenceError(Exception):
    """Raised when evidence bytes cannot be parsed or loaded."""


@dataclass(frozen=True)
class RepositoryEvidenceIssue:
    code: str
    path: str
    message: str
    severity: str = "error"

    def line(self) -> str:
        return f"{self.severity.upper()} {self.code} {self.path}: {self.message}"


@dataclass(frozen=True)
class OwnerResolution:
    repository: str
    registry_revision: str
    registry_blob_id: str
    instance_blob_id: str
    eligible_rw_roles: tuple[str, ...]
    requested_role: str | None
    resolved_role: str | None
    basis: str
    assignment_authority: str | None
    issues: tuple[str, ...]


@dataclass(frozen=True)
class RepositoryEvidenceFacts:
    structural_valid: bool
    authority_valid: bool
    owner_resolved: bool
    role_decisions_valid: bool
    artifact_refs_resolved: bool
    stage_artifacts_retained: bool
    retention_valid: bool
    exceptions_live: bool
    measurement_prerequisites: bool
    boundary_prerequisites: bool
    generated_treatment_valid: bool
    comparability_valid: bool
    parity_eligible: bool
    positive_effectiveness_or_waiver: bool
    efficiency_complete: bool
    dry_run_retained: bool
    threshold_proposed: bool
    threshold_approved: bool
    ratchet_proposed: bool
    ratchet_adopted: bool
    owner_proposal_valid: bool
    steward_adoption_valid: bool
    independent_review_valid: bool
    current_evidence_valid: bool
    enforcement_machinery_retained: bool
    enforcement_failure_proof_retained: bool
    claim_valid: bool
    blocking_issue_codes: tuple[str, ...]

    @property
    def artifacts_verified(self) -> bool:
        return self.artifact_refs_resolved

    @property
    def positive_effectiveness(self) -> bool:
        return self.positive_effectiveness_or_waiver

    @property
    def efficiency_present(self) -> bool:
        return self.efficiency_complete

    @property
    def dry_run_evidence(self) -> bool:
        return self.dry_run_retained

    @property
    def enforcement_verified(self) -> bool:
        return self.enforcement_machinery_retained and self.enforcement_failure_proof_retained


FactGate = RepositoryEvidenceFacts


@dataclass(frozen=True)
class RepositoryEvidenceContract:
    data: Mapping[str, Any]
    issues: tuple[RepositoryEvidenceIssue, ...]
    facts: RepositoryEvidenceFacts
    derived_maturity: str


@dataclass(frozen=True)
class AuthorityDocuments:
    registry_revision: str
    repos_yaml: Mapping[str, Any]
    instance_yaml: Mapping[str, Any]
    repos_blob_id: str
    instance_blob_id: str


@dataclass(frozen=True)
class EffectivenessAlternative:
    receipt_valid: bool
    live_waiver_valid: bool
    independent_review_valid: bool
    support_artifact_ids: tuple[str, ...]


def _issue(code: str, path: str, message: str) -> RepositoryEvidenceIssue:
    return RepositoryEvidenceIssue(code=code, path=path, message=message)


def _issue_sort_key(issue: RepositoryEvidenceIssue) -> tuple[str, str, str]:
    return issue.code, issue.path, issue.message


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _canonical_json_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise RepositoryEvidenceError("non-finite numeric value is not canonical")
        return value
    raise RepositoryEvidenceError(f"unsupported canonical value {type(value).__name__}")


def _canonical_contract(data: Mapping[str, Any]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for key in ROOT_ORDER:
        if key in data:
            value = data[key]
            if key in COLLECTION_ORDER and isinstance(value, list):
                id_field = ID_FIELDS[key]
                value = sorted(value, key=lambda record: str(record.get(id_field, "")))
            root[key] = _canonical_json_value(value)
    for key in sorted(set(data) - set(ROOT_ORDER)):
        root[key] = _canonical_json_value(data[key])
    return root


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            _canonical_json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def canonical_repository_evidence_bytes(contract: Mapping[str, Any] | RepositoryEvidenceContract) -> bytes:
    data = contract.data if isinstance(contract, RepositoryEvidenceContract) else contract
    return _canonical_json_bytes(_canonical_contract(data))


def _reject_duplicate_json_pairs(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RepositoryEvidenceError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _parse_json_bytes(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RepositoryEvidenceError("evidence is not valid UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(
                RepositoryEvidenceError(f"non-finite numeric value {token!r}")
            ),
        )
    except RepositoryEvidenceError:
        raise
    except json.JSONDecodeError as exc:
        raise RepositoryEvidenceError(f"invalid JSON: {exc}") from exc


def _parse_yaml_bytes(raw: bytes) -> Any:
    if yaml is None:
        raise RepositoryEvidenceError(f"PyYAML is unavailable: {_YAML_IMPORT_ERROR}")

    class Loader(yaml.SafeLoader):  # type: ignore[misc, valid-type]
        pass

    def construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise RepositoryEvidenceError("YAML mapping keys must be strings")
            if key in mapping:
                raise RepositoryEvidenceError(f"duplicate key {key!r}")
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    Loader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_mapping,
    )
    try:
        return yaml.load(raw.decode("utf-8"), Loader=Loader)
    except RepositoryEvidenceError:
        raise
    except (UnicodeDecodeError, yaml.YAMLError) as exc:  # type: ignore[union-attr]
        raise RepositoryEvidenceError(f"invalid YAML: {exc}") from exc


def _load_raw_document(path: Path) -> Any:
    raw = path.read_bytes()
    if path.suffix.lower() in (".yaml", ".yml"):
        return _parse_yaml_bytes(raw)
    return _parse_json_bytes(raw)


def _normalize_input(raw: Any) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise RepositoryEvidenceError("evidence root must be an object")
    if raw.get("schema") == NORMALIZED_INPUT_SCHEMA:
        contract = raw.get("contract")
        if not isinstance(contract, Mapping):
            raise RepositoryEvidenceError("normalized input must contain contract object")
        return dict(contract)
    if raw.get("schema_version") == CONTRACT_SCHEMA_VERSION:
        return dict(raw)
    raise RepositoryEvidenceError("unsupported evidence schema")


def parse_repository_evidence(raw: bytes | str, *, path_hint: str | None = None) -> Mapping[str, Any]:
    if isinstance(raw, str):
        raw_bytes = raw.encode("utf-8")
    else:
        raw_bytes = raw
    suffix = Path(path_hint or "").suffix.lower()
    parsed = _parse_yaml_bytes(raw_bytes) if suffix in (".yaml", ".yml") else _parse_json_bytes(raw_bytes)
    return _normalize_input(parsed)


def load_repository_evidence(path: str | Path) -> Mapping[str, Any]:
    source = Path(path)
    return _normalize_input(_load_raw_document(source))


def _run_git_show(repo_root: Path, revision: str, path: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"{revision}:{path}"],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise RepositoryEvidenceError(f"missing git object {revision}:{path}: {detail}") from exc
    return result.stdout


def _parse_authority_yaml(raw: bytes, label: str) -> Mapping[str, Any]:
    try:
        parsed = _parse_yaml_bytes(raw)
    except RepositoryEvidenceError as exc:
        raise RepositoryEvidenceError(f"{label}: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise RepositoryEvidenceError(f"{label}: root must be an object")
    return parsed


def load_authority_documents(repo_root: str | Path, registry_revision: str) -> AuthorityDocuments:
    root = Path(repo_root)
    repos_raw = _run_git_show(root, registry_revision, "repos.yaml")
    instance_raw = _run_git_show(root, registry_revision, "instance.yaml")
    return AuthorityDocuments(
        registry_revision=registry_revision,
        repos_yaml=_parse_authority_yaml(repos_raw, "repos.yaml"),
        instance_yaml=_parse_authority_yaml(instance_raw, "instance.yaml"),
        repos_blob_id=hashlib.sha1(b"blob " + str(len(repos_raw)).encode("ascii") + b"\0" + repos_raw).hexdigest(),
        instance_blob_id=hashlib.sha1(
            b"blob " + str(len(instance_raw)).encode("ascii") + b"\0" + instance_raw
        ).hexdigest(),
    )


def _instance_roles(instance_yaml: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    roles = instance_yaml.get("roles")
    return roles if isinstance(roles, Mapping) else {}


def _repo_grants(repos_yaml: Mapping[str, Any], repository: str) -> Mapping[str, str]:
    for entry in repos_yaml.get("repos", []):
        if isinstance(entry, Mapping) and entry.get("name") == repository:
            grants = entry.get("grants")
            return grants if isinstance(grants, Mapping) else {}
    return {}


def resolve_repository_owner(
    contract: Mapping[str, Any],
    repo_root: str | Path,
    *,
    requested_role: str | None = None,
) -> OwnerResolution:
    repository = str(contract.get("repository", ""))
    registry_revision = str(contract.get("registry_revision", ""))
    issues: list[str] = []
    try:
        authority = load_authority_documents(repo_root, registry_revision)
    except RepositoryEvidenceError as exc:
        return OwnerResolution(
            repository=repository,
            registry_revision=registry_revision,
            registry_blob_id="",
            instance_blob_id="",
            eligible_rw_roles=(),
            requested_role=requested_role,
            resolved_role=None,
            basis="authority-load-failed",
            assignment_authority=None,
            issues=(str(exc),),
        )

    roles = _instance_roles(authority.instance_yaml)
    grants = _repo_grants(authority.repos_yaml, repository)
    eligible = tuple(sorted(role for role, access in grants.items() if access == "rw"))
    for role in eligible:
        if role not in roles:
            issues.append(f"unknown rw role {role}")
    seen_personas: dict[str, str] = {}
    seen_accounts: dict[str, str] = {}
    for role, binding in roles.items():
        if not isinstance(binding, Mapping):
            issues.append(f"malformed role binding {role}")
            continue
        persona = binding.get("persona")
        account = binding.get("account")
        if isinstance(persona, str):
            if persona in seen_personas and seen_personas[persona] != role:
                issues.append(f"duplicate persona binding {persona}")
            seen_personas[persona] = role
        if isinstance(account, str):
            if account in seen_accounts and seen_accounts[account] != role:
                issues.append(f"duplicate account binding {account}")
            seen_accounts[account] = role
    if requested_role is not None and requested_role not in eligible:
        issues.append(f"requested role {requested_role} lacks rw grant")
    resolved = requested_role
    basis = "explicit-assignment" if requested_role else "unresolved"
    assignment_authority = None
    owner_resolution = contract.get("owner_resolution")
    if isinstance(owner_resolution, Mapping):
        raw_assignment = owner_resolution.get("assignment_authority")
        assignment_authority = raw_assignment if isinstance(raw_assignment, str) and raw_assignment else None
    if not eligible:
        issues.append("no rw owner candidate")
        resolved = None
        basis = "no-rw-candidate"
    elif requested_role is None and len(eligible) == 1:
        resolved = eligible[0]
        basis = "single-rw-default"
    elif requested_role is None:
        issues.append("multiple rw candidates require steward assignment")
        resolved = None
        basis = "requires-steward-assignment"
    elif len(eligible) > 1 and not assignment_authority:
        issues.append("multiple rw assignment requires assignment_authority")
    return OwnerResolution(
        repository=repository,
        registry_revision=registry_revision,
        registry_blob_id=authority.repos_blob_id,
        instance_blob_id=authority.instance_blob_id,
        eligible_rw_roles=eligible,
        requested_role=requested_role,
        resolved_role=resolved if not issues else None,
        basis=basis,
        assignment_authority=assignment_authority,
        issues=tuple(sorted(issues)),
    )


def _ids_by_collection(data: Mapping[str, Any]) -> tuple[dict[str, dict[str, Mapping[str, Any]]], list[RepositoryEvidenceIssue]]:
    indexes: dict[str, dict[str, Mapping[str, Any]]] = {collection: {} for collection in COLLECTION_ORDER}
    all_ids: dict[str, str] = {}
    issues: list[RepositoryEvidenceIssue] = []
    for collection in COLLECTION_ORDER:
        records = data.get(collection)
        if not isinstance(records, list):
            issues.append(_issue("collection_not_list", f"/{collection}", "collection must be a list"))
            continue
        id_field = ID_FIELDS[collection]
        for index, record in enumerate(records):
            path = f"/{collection}/{index}"
            if not isinstance(record, Mapping):
                issues.append(_issue("record_not_object", path, "record must be an object"))
                continue
            unknown = sorted(set(record) - RECORD_ALLOWED_FIELDS[collection])
            for field in unknown:
                issues.append(_issue("unknown_record_field", f"{path}/{field}", "field is not allowed"))
            record_id = record.get(id_field)
            if not isinstance(record_id, str) or not record_id:
                issues.append(_issue("missing_id", f"{path}/{id_field}", "record id is required"))
                continue
            if record_id in all_ids:
                issues.append(
                    _issue(
                        "duplicate_id",
                        f"{path}/{id_field}",
                        f"id already used by {all_ids[record_id]}",
                    )
                )
            all_ids[record_id] = collection
            indexes[collection][record_id] = record
    return indexes, issues


def _add_ref_issue(
    issues: list[RepositoryEvidenceIssue],
    indexes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    expected_collection: str,
    ref_id: Any,
    path: str,
) -> None:
    if ref_id in (None, ""):
        return
    if not isinstance(ref_id, str):
        issues.append(_issue("typed_ref_not_string", path, "reference id must be a string"))
        return
    matches = [collection for collection, values in indexes.items() if ref_id in values]
    if matches != [expected_collection]:
        issues.append(
            _issue(
                "typed_ref_unresolved",
                path,
                f"expected {expected_collection} ref, found {matches or 'none'}",
            )
        )


def _list_refs(value: Any) -> Sequence[Any]:
    return value if isinstance(value, list) else []


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _is_live_exception(record: Mapping[str, Any], indexes: Mapping[str, Mapping[str, Mapping[str, Any]]], now: datetime) -> bool:
    active = record.get("active_state")
    expiry = record.get("expiry")
    if not isinstance(active, Mapping) or not isinstance(expiry, Mapping):
        return False
    if active.get("status") != "active":
        return False
    kind = expiry.get("kind")
    if kind == "timestamp":
        expires = _parse_time(expiry.get("value"))
        return expires is not None and expires > now
    if kind == "event":
        evidence_id = expiry.get("pending_evidence_id")
        artifact = indexes["artifact_records"].get(evidence_id) if isinstance(evidence_id, str) else None
        if not artifact:
            return False
        content = _retrieve_artifact_bytes(artifact, Path("."), allow_repo_paths=False)
        return content is not None and b"pending" in content.lower()
    return False


def _exception_records(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    for collection in ("exclusion_records", "waiver_records", "maturity_deferrals", "quarantine_records"):
        for record in data.get(collection, []):
            if isinstance(record, Mapping):
                records.append(record)
    return records


def _storage_locator(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    locator = record.get("storage_locator")
    return locator if isinstance(locator, Mapping) else None


def _retrieve_artifact_bytes(
    artifact: Mapping[str, Any],
    repo_root: Path,
    *,
    allow_repo_paths: bool = True,
) -> bytes | None:
    locator = _storage_locator(artifact)
    if locator is None:
        return None
    kind = locator.get("kind")
    value = locator.get("value")
    if kind == "inline":
        if not isinstance(value, str):
            return None
        try:
            return base64.b64decode(value.encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError):
            return None
    if kind == "repo_path" and allow_repo_paths:
        if not isinstance(value, str) or "\x00" in value or value.startswith("/") or ".." in Path(value).parts:
            return None
        path = repo_root / value
        try:
            if path.is_symlink():
                return None
            resolved = path.resolve(strict=True)
            root = repo_root.resolve(strict=True)
            if os.path.commonpath((str(root), str(resolved))) != str(root):
                return None
            return resolved.read_bytes()
        except (OSError, ValueError):
            return None
    return None


def _validate_artifact_paths_and_digests(
    data: Mapping[str, Any],
    indexes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    repo_root: Path,
) -> list[RepositoryEvidenceIssue]:
    issues: list[RepositoryEvidenceIssue] = []
    for artifact_id, artifact in indexes["artifact_records"].items():
        path = f"/artifact_records/{artifact_id}"
        locator = _storage_locator(artifact)
        if locator is None:
            issues.append(_issue("artifact_locator_missing", path, "artifact storage_locator is required"))
            continue
        kind = locator.get("kind")
        value = locator.get("value")
        if kind == "external":
            issues.append(_issue("artifact_external_not_retrieved", path, "external artifacts are not promotion eligible in v1"))
            continue
        if kind == "repo_path" and (
            not isinstance(value, str) or "\x00" in value or value.startswith("/") or ".." in Path(value).parts
        ):
            issues.append(_issue("artifact_unsafe_locator", path, "repo_path locator is unsafe"))
            continue
        content = _retrieve_artifact_bytes(artifact, repo_root)
        if content is None:
            issues.append(_issue("artifact_unretrieved", path, "artifact bytes could not be retrieved safely"))
            continue
        algorithm = artifact.get("digest_algorithm")
        digest = artifact.get("digest")
        size = artifact.get("size")
        if algorithm != "sha256":
            issues.append(_issue("artifact_digest_algorithm", path, "only sha256 is supported"))
            continue
        observed = hashlib.sha256(content).hexdigest()
        if digest != observed:
            issues.append(_issue("artifact_digest_mismatch", path, "artifact digest does not match retrieved bytes"))
        if size != len(content):
            issues.append(_issue("artifact_size_mismatch", path, "artifact size does not match retrieved bytes"))
    return issues


def _validate_root(data: Mapping[str, Any]) -> list[RepositoryEvidenceIssue]:
    issues: list[RepositoryEvidenceIssue] = []
    for key in sorted(ROOT_REQUIRED - set(data)):
        issues.append(_issue("missing_root_field", f"/{key}", "root field is required"))
    for key in sorted(set(data) - ROOT_ALLOWED):
        issues.append(_issue("unknown_root_field", f"/{key}", "root field is not allowed"))
    if data.get("schema_version") != CONTRACT_SCHEMA_VERSION:
        issues.append(_issue("schema_version", "/schema_version", "schema_version must be 1"))
    if data.get("repo_class") not in REPO_CLASSES:
        issues.append(_issue("repo_class", "/repo_class", "unsupported repository class"))
    if data.get("maturity") not in MATURITY_ORDER:
        issues.append(_issue("maturity", "/maturity", "unsupported maturity"))
    if data.get("target_maturity") not in MATURITY_ORDER:
        issues.append(_issue("target_maturity", "/target_maturity", "unsupported target maturity"))
    inventory = data.get("inventory")
    if not isinstance(inventory, Mapping):
        issues.append(_issue("inventory", "/inventory", "inventory must be an object"))
    else:
        for field in ("language_substrates", "runner_manifests", "test_commands", "ci_workflows", "applicability_evidence_ids"):
            if not isinstance(inventory.get(field), list):
                issues.append(_issue("inventory_field", f"/inventory/{field}", "inventory field must be a list"))
    return issues


def _validate_repository_revision(data: Mapping[str, Any]) -> list[RepositoryEvidenceIssue]:
    issues: list[RepositoryEvidenceIssue] = []
    repository = data.get("repository")
    revision = data.get("evidence_revision")
    for collection in COLLECTION_ORDER:
        for index, record in enumerate(data.get(collection, [])):
            if not isinstance(record, Mapping):
                continue
            if collection in ("changed_code_records",):
                if record.get("repository") != repository:
                    issues.append(_issue("repository_mismatch", f"/{collection}/{index}/repository", "repository must match root"))
                if record.get("head_revision") != revision or record.get("revision") != revision:
                    issues.append(_issue("revision_mismatch", f"/{collection}/{index}/revision", "changed-code head revision must match root"))
                continue
            if record.get("repository") != repository:
                issues.append(_issue("repository_mismatch", f"/{collection}/{index}/repository", "repository must match root"))
            if record.get("revision") != revision:
                issues.append(_issue("revision_mismatch", f"/{collection}/{index}/revision", "revision must match root"))
    return issues


def _validate_refs_and_semantics(
    data: Mapping[str, Any],
    indexes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    repo_root: Path,
    now: datetime,
) -> list[RepositoryEvidenceIssue]:
    issues: list[RepositoryEvidenceIssue] = []
    live_deferrals = {
        key for key, record in indexes["maturity_deferrals"].items() if _is_live_exception(record, indexes, now)
    }
    live_exclusions = {
        key for key, record in indexes["exclusion_records"].items() if _is_live_exception(record, indexes, now)
    }
    live_waivers = {key for key, record in indexes["waiver_records"].items() if _is_live_exception(record, indexes, now)}
    live_quarantines = {
        key for key, record in indexes["quarantine_records"].items() if _is_live_exception(record, indexes, now)
    }

    for index, record in enumerate(data.get("dimension_dispositions", [])):
        if not isinstance(record, Mapping):
            continue
        path = f"/dimension_dispositions/{index}"
        dimension = record.get("dimension")
        status = record.get("status")
        if record.get("applicability_decision") is not None:
            issues.extend(
                _validate_authority_decision(
                    record.get("applicability_decision"),
                    path=f"{path}/applicability_decision",
                    data=data,
                    repo_root=repo_root,
                    indexes=indexes,
                    expected_scope_kind="dimension",
                )
            )
        if record.get("approved_disposition_decision") is not None:
            issues.extend(
                _validate_authority_decision(
                    record.get("approved_disposition_decision"),
                    path=f"{path}/approved_disposition_decision",
                    data=data,
                    repo_root=repo_root,
                    indexes=indexes,
                    expected_scope_kind="dimension",
                )
            )
        if dimension not in DIMENSIONS:
            issues.append(_issue("dimension_unknown", f"{path}/dimension", "unsupported dimension"))
        if status == "measured":
            measurements = _list_refs(record.get("measurement_ids"))
            if not measurements:
                issues.append(_issue("measured_missing_measurement", f"{path}/measurement_ids", "measured disposition requires measurement"))
            for ref in measurements:
                _add_ref_issue(issues, indexes, "quantitative_measurements", ref, f"{path}/measurement_ids")
            if dimension == "changed_code":
                _add_ref_issue(issues, indexes, "changed_code_records", record.get("changed_code_record_id"), f"{path}/changed_code_record_id")
            if dimension == "mutation":
                _add_ref_issue(issues, indexes, "mutation_records", record.get("mutation_record_id"), f"{path}/mutation_record_id")
            if dimension == "flake":
                refs = _list_refs(record.get("flake_observation_ids"))
                if not refs:
                    issues.append(_issue("measured_missing_flake", f"{path}/flake_observation_ids", "measured flake requires observations"))
                for ref in refs:
                    _add_ref_issue(issues, indexes, "flake_observations", ref, f"{path}/flake_observation_ids")
        elif status in ("unsupported", "deferred"):
            deferral_id = record.get("deferral_id")
            _add_ref_issue(issues, indexes, "maturity_deferrals", deferral_id, f"{path}/deferral_id")
            if deferral_id not in live_deferrals:
                issues.append(_issue("deferral_not_live", f"{path}/deferral_id", "unsupported/deferred requires live maturity deferral"))
        elif status == "excluded":
            exclusion_id = record.get("exclusion_id")
            _add_ref_issue(issues, indexes, "exclusion_records", exclusion_id, f"{path}/exclusion_id")
            if exclusion_id not in live_exclusions:
                issues.append(_issue("exclusion_not_live", f"{path}/exclusion_id", "excluded disposition requires live exclusion"))
        elif status == "not_applicable":
            refs = _list_refs(record.get("applicability_evidence_ids"))
            if not refs or not record.get("steward_approval"):
                issues.append(_issue("not_applicable_unproved", path, "not_applicable requires evidence and steward approval"))
            for ref in refs:
                _add_ref_issue(issues, indexes, "artifact_records", ref, f"{path}/applicability_evidence_ids")
        else:
            issues.append(_issue("dimension_status_unknown", f"{path}/status", "unsupported dimension status"))

    for index, record in enumerate(data.get("quantitative_measurements", [])):
        if not isinstance(record, Mapping):
            continue
        path = f"/quantitative_measurements/{index}"
        numerator, denominator, value = record.get("numerator"), record.get("denominator"), record.get("value")
        if not (type(numerator) is int and type(denominator) is int and denominator >= 0 and 0 <= numerator <= denominator):
            issues.append(_issue("measurement_arithmetic", path, "numerator and denominator must be finite consistent integers"))
        elif denominator > 0 and not (type(value) in (int, float) and math.isfinite(float(value)) and abs(float(value) - numerator / denominator) < 1e-9):
            issues.append(_issue("measurement_arithmetic", f"{path}/value", "value must equal numerator/denominator"))
        for section in ("baseline", "current"):
            nested = record.get(section)
            if isinstance(nested, Mapping):
                _add_ref_issue(issues, indexes, "quantitative_measurements", nested.get("measurement_id"), f"{path}/{section}/measurement_id")
        _add_ref_issue(issues, indexes, "artifact_records", record.get("artifact_id"), f"{path}/artifact_id")
        _add_ref_issue(issues, indexes, "changed_code_records", record.get("changed_code_record_id"), f"{path}/changed_code_record_id")
        _add_ref_issue(issues, indexes, "mutation_records", record.get("mutation_record_id"), f"{path}/mutation_record_id")
        for ref in _list_refs(record.get("flake_observation_ids")):
            _add_ref_issue(issues, indexes, "flake_observations", ref, f"{path}/flake_observation_ids")
        scope = record.get("scope")
        if isinstance(scope, Mapping):
            for ref in _list_refs(scope.get("exclusion_ids")):
                _add_ref_issue(issues, indexes, "exclusion_records", ref, f"{path}/scope/exclusion_ids")
            for ref in _list_refs(scope.get("generated_source_ids")):
                _add_ref_issue(issues, indexes, "generated_source_records", ref, f"{path}/scope/generated_source_ids")
        for section, proposal_field, expected_role in (
            ("threshold", "proposal_decision", data.get("repo_evidence_owner_role")),
            ("ratchet", "proposal_decision", data.get("repo_evidence_owner_role")),
        ):
            value = record.get(section)
            if isinstance(value, Mapping) and value.get(proposal_field) is not None:
                issues.extend(
                    _validate_authority_decision(
                        value.get(proposal_field),
                        path=f"{path}/{section}/{proposal_field}",
                        data=data,
                        repo_root=repo_root,
                        indexes=indexes,
                        expected_role=expected_role if isinstance(expected_role, str) else None,
                        expected_scope_kind="measurement",
                    )
                )
        threshold = record.get("threshold")
        if isinstance(threshold, Mapping):
            if threshold.get("comparator") not in THRESHOLD_COMPARATORS:
                issues.append(_issue("threshold_proposal_missing", f"{path}/threshold/comparator", "threshold comparator must be gte, lte, or eq"))
            if threshold.get("state") in ("proposed", "advisory", "enforcing") and threshold.get("proposal_decision") is None:
                issues.append(_issue("threshold_proposal_missing", f"{path}/threshold/proposal_decision", "threshold state requires owner proposal decision"))
            if threshold.get("state") == "enforcing" and threshold.get("approval_decision") is None:
                issues.append(_issue("threshold_approval_missing", f"{path}/threshold/approval_decision", "enforcing threshold requires steward approval decision"))
            if threshold.get("approval_decision") is not None:
                issues.extend(
                    _validate_authority_decision(
                        threshold.get("approval_decision"),
                        path=f"{path}/threshold/approval_decision",
                        data=data,
                        repo_root=repo_root,
                        indexes=indexes,
                        expected_role=data.get("fleet_steward_role") if isinstance(data.get("fleet_steward_role"), str) else None,
                        expected_scope_kind="measurement",
                    )
                )
        ratchet = record.get("ratchet")
        if isinstance(ratchet, Mapping):
            if ratchet.get("direction") not in RATCHET_DIRECTIONS:
                issues.append(_issue("ratchet_proposal_missing", f"{path}/ratchet/direction", "ratchet direction must be nondecreasing, nonincreasing, or bounded"))
            if ratchet.get("state") in ("proposed", "adopted") and ratchet.get("proposal_decision") is None:
                issues.append(_issue("ratchet_proposal_missing", f"{path}/ratchet/proposal_decision", "ratchet state requires owner proposal decision"))
            if ratchet.get("state") == "adopted" and ratchet.get("adoption_decision") is None:
                issues.append(_issue("ratchet_adoption_missing", f"{path}/ratchet/adoption_decision", "adopted ratchet requires steward adoption decision"))
            if ratchet.get("adoption_decision") is not None:
                issues.extend(
                    _validate_authority_decision(
                        ratchet.get("adoption_decision"),
                        path=f"{path}/ratchet/adoption_decision",
                        data=data,
                        repo_root=repo_root,
                        indexes=indexes,
                        expected_role=data.get("fleet_steward_role") if isinstance(data.get("fleet_steward_role"), str) else None,
                        expected_scope_kind="measurement",
                    )
                )
        comparability = record.get("comparability")
        if isinstance(comparability, Mapping):
            if comparability.get("verdict") not in ("comparable", "reset_required", "refused"):
                issues.append(_issue("comparability_reset_required", f"{path}/comparability/verdict", "unsupported comparability verdict"))
            checked_fields = set(_list_refs(comparability.get("checked_fields")))
            if comparability.get("verdict") in ("comparable", "reset_required") and checked_fields != set(COMPARABILITY_CHECKED_FIELDS):
                issues.append(
                    _issue(
                        "comparability_checked_fields_missing",
                        f"{path}/comparability/checked_fields",
                        "comparability must check the complete ORG-083 reset field set",
                    )
                )
            changed_fields = _list_refs(comparability.get("changed_fields"))
            if comparability.get("verdict") == "comparable" and changed_fields and (
                comparability.get("equivalence_decision") is None or not isinstance(comparability.get("equivalence_artifact_id"), str)
            ):
                issues.append(
                    _issue(
                        "comparability_material_change_unapproved",
                        f"{path}/comparability/equivalence_decision",
                        "material comparability changes require retained equivalence decision",
                    )
                )
            if comparability.get("verdict") == "reset_required" and (
                comparability.get("reset_authority_decision") is None or not isinstance(comparability.get("reset_artifact_id"), str)
            ):
                issues.append(
                    _issue(
                        "comparability_reset_required",
                        f"{path}/comparability/reset_authority_decision",
                        "reset_required comparability requires retained reset authority decision",
                    )
                )
            if comparability.get("equivalence_decision") is not None:
                issues.extend(
                    _validate_authority_decision(
                        comparability.get("equivalence_decision"),
                        path=f"{path}/comparability/equivalence_decision",
                        data=data,
                        repo_root=repo_root,
                        indexes=indexes,
                        expected_scope_kind="measurement",
                    )
                )
            _add_ref_issue(issues, indexes, "artifact_records", comparability.get("equivalence_artifact_id"), f"{path}/comparability/equivalence_artifact_id")
            if comparability.get("reset_authority_decision") is not None:
                issues.extend(
                    _validate_authority_decision(
                        comparability.get("reset_authority_decision"),
                        path=f"{path}/comparability/reset_authority_decision",
                        data=data,
                        repo_root=repo_root,
                        indexes=indexes,
                        expected_scope_kind="measurement",
                    )
                )
                reset_scope = comparability.get("reset_authority_decision", {}).get("scope")
                if not (
                    isinstance(reset_scope, Mapping)
                    and reset_scope.get("measurement_id") == record.get("measurement_id")
                    and reset_scope.get("dimension") == record.get("dimension")
                ):
                    issues.append(
                        _issue(
                            "comparability_reset_required",
                            f"{path}/comparability/reset_authority_decision/scope",
                            "reset authority must bind the current measurement id and dimension",
                        )
                    )
            _add_ref_issue(issues, indexes, "artifact_records", comparability.get("reset_artifact_id"), f"{path}/comparability/reset_artifact_id")

    for index, record in enumerate(data.get("mutation_records", [])):
        if not isinstance(record, Mapping):
            continue
        path = f"/mutation_records/{index}"
        counts = record.get("counts")
        terminal = record.get("terminal_dispositions")
        if not isinstance(counts, Mapping) or not isinstance(terminal, list):
            issues.append(_issue("mutation_counts", path, "mutation counts and terminal dispositions are required"))
        else:
            generated = counts.get("generated")
            terminal_counts = {
                status: sum(1 for item in terminal if isinstance(item, Mapping) and item.get("outcome") == status)
                for status in ("killed", "survived", "no_coverage", "timeout", "error", "equivalent", "invalid", "excluded")
            }
            if type(generated) is not int or generated != len(terminal):
                issues.append(_issue("mutation_terminal_count", path, "generated count must equal terminal dispositions"))
            for status, count in terminal_counts.items():
                if counts.get(status) != count:
                    issues.append(_issue("mutation_outcome_count", f"{path}/counts/{status}", "outcome count mismatch"))
            for offset, item in enumerate(terminal):
                if isinstance(item, Mapping) and item.get("outcome") in ("equivalent", "invalid", "excluded"):
                    _add_ref_issue(issues, indexes, "artifact_records", item.get("reviewer_evidence_id"), f"{path}/terminal_dispositions/{offset}/reviewer_evidence_id")
        _add_ref_issue(issues, indexes, "artifact_records", record.get("artifact_id"), f"{path}/artifact_id")

    for index, record in enumerate(data.get("flake_observations", [])):
        if not isinstance(record, Mapping):
            continue
        path = f"/flake_observations/{index}"
        run_count, pass_count, fail_count = record.get("run_count"), record.get("pass_count"), record.get("fail_count")
        numerator, denominator, rate = record.get("numerator"), record.get("denominator"), record.get("rate")
        if not (type(run_count) is int and type(pass_count) is int and type(fail_count) is int and run_count == pass_count + fail_count):
            issues.append(_issue("flake_counts", path, "run_count must equal pass_count plus fail_count"))
        if not (type(numerator) is int and type(denominator) is int and denominator == run_count and numerator == fail_count):
            issues.append(_issue("flake_rate", path, "flake numerator/denominator must match failures/runs"))
        elif denominator and not (type(rate) in (int, float) and abs(float(rate) - numerator / denominator) < 1e-9):
            issues.append(_issue("flake_rate", f"{path}/rate", "flake rate must match numerator/denominator"))
        if pass_count == 0 or fail_count == 0:
            issues.append(_issue("flake_not_inconsistent", path, "measured flake requires inconsistent outcomes"))
        _add_ref_issue(issues, indexes, "artifact_records", record.get("sequence_artifact_id"), f"{path}/sequence_artifact_id")
        _add_ref_issue(issues, indexes, "artifact_records", record.get("artifact_id"), f"{path}/artifact_id")
        _add_ref_issue(issues, indexes, "quarantine_records", record.get("quarantine_id"), f"{path}/quarantine_id")
        _add_ref_issue(issues, indexes, "retry_policy_records", record.get("retry_policy_id"), f"{path}/retry_policy_id")

    for index, record in enumerate(data.get("boundary_records", [])):
        if not isinstance(record, Mapping):
            continue
        path = f"/boundary_records/{index}"
        if record.get("applicability_decision") is not None:
            issues.extend(
                _validate_authority_decision(
                    record.get("applicability_decision"),
                    path=f"{path}/applicability_decision",
                    data=data,
                    repo_root=repo_root,
                    indexes=indexes,
                    expected_scope_kind="boundary",
                )
            )
        if record.get("approved_disposition_decision") is not None:
            issues.extend(
                _validate_authority_decision(
                    record.get("approved_disposition_decision"),
                    path=f"{path}/approved_disposition_decision",
                    data=data,
                    repo_root=repo_root,
                    indexes=indexes,
                    expected_scope_kind="boundary",
                )
            )
        if record.get("boundary_kind") not in BOUNDARY_KINDS:
            issues.append(_issue("boundary_kind", f"{path}/boundary_kind", "unsupported boundary kind"))
        if record.get("status") == "not_exercised":
            deferral_id = record.get("deferral_id")
            _add_ref_issue(issues, indexes, "maturity_deferrals", deferral_id, f"{path}/deferral_id")
            if deferral_id not in live_deferrals:
                issues.append(_issue("boundary_deferral_not_live", f"{path}/deferral_id", "not_exercised boundary requires live deferral"))
        elif record.get("status") == "not_applicable":
            refs = _list_refs(record.get("applicability_evidence_ids"))
            if not refs or not record.get("steward_approval"):
                issues.append(_issue("boundary_not_applicable_unproved", path, "not_applicable boundary requires evidence and steward approval"))
            for ref in refs:
                _add_ref_issue(issues, indexes, "artifact_records", ref, f"{path}/applicability_evidence_ids")
        elif record.get("status") not in ("exercised", "simulated"):
            issues.append(_issue("boundary_status", f"{path}/status", "unsupported boundary status"))
        for ref in _list_refs(record.get("artifact_ids")):
            _add_ref_issue(issues, indexes, "artifact_records", ref, f"{path}/artifact_ids")

    for index, record in enumerate(data.get("retry_policy_records", [])):
        if not isinstance(record, Mapping):
            continue
        if record.get("max_attempts") is not None and type(record.get("max_attempts")) is not int:
            issues.append(_issue("retry_policy", f"/retry_policy_records/{index}/max_attempts", "max_attempts must be integer"))
        for ref in _list_refs(record.get("linked_flake_ids")):
            _add_ref_issue(issues, indexes, "flake_observations", ref, f"/retry_policy_records/{index}/linked_flake_ids")

    for index, record in enumerate(data.get("deploy_tolerance_records", [])):
        if not isinstance(record, Mapping):
            continue
        for field in ("workflow", "step", "command_identity", "approver", "started_at", "expiry", "trigger", "restoration_plan"):
            if field not in record:
                issues.append(_issue("deploy_tolerance_field", f"/deploy_tolerance_records/{index}/{field}", "deploy tolerance field is required"))
        _add_ref_issue(issues, indexes, "artifact_records", record.get("evidence_artifact_id"), f"/deploy_tolerance_records/{index}/evidence_artifact_id")

    for index, record in enumerate(data.get("effectiveness_receipts", [])):
        if not isinstance(record, Mapping):
            continue
        path = f"/effectiveness_receipts/{index}"
        payload = record.get("payload")
        kind = record.get("kind")
        if not isinstance(payload, Mapping):
            issues.append(_issue("effectiveness_payload", f"{path}/payload", "payload must be an object"))
            continue
        required_payload_fields = {
            "behavioral_falsification": ("threat", "mutation", "observed_red", "restored_green", "assertion_effect"),
            "historical_fault_regression": ("fault_identity", "pre_fix_failure_artifact_id", "fixed_pass_artifact_id", "fault_specific_assertion"),
            "real_boundary": ("producer", "transport", "consumer", "real_components", "simulated_components", "reached_preconditions", "asserted_delivered_effect", "boundary_record_id"),
            "mutation": ("mutation_record_id", "operator_scope", "survived_mutant_review", "limitations"),
            "quarantine_review": ("quarantine_id", "reproduction_artifact_id", "restoration_attempt", "disposition"),
        }
        if kind not in required_payload_fields:
            issues.append(_issue("effectiveness_kind", f"{path}/kind", "unsupported effectiveness kind"))
            continue
        for field in required_payload_fields[kind]:
            if field not in payload:
                issues.append(_issue("effectiveness_payload_field", f"{path}/payload/{field}", "payload field is required"))
        if record.get("review_decision") is not None:
            issues.extend(
                _validate_authority_decision(
                    record.get("review_decision"),
                    path=f"{path}/review_decision",
                    data=data,
                    repo_root=repo_root,
                    indexes=indexes,
                    expected_scope_kind="repository",
                )
            )
        if kind in ("behavioral_falsification", "historical_fault_regression", "real_boundary", "mutation"):
            independent = record.get("independent_review_decision")
            if independent is None:
                issues.append(_issue("independent_reviewer_missing", f"{path}/independent_review_decision", "positive effectiveness requires independent review decision"))
            else:
                issues.extend(
                    _validate_authority_decision(
                        independent,
                        path=f"{path}/independent_review_decision",
                        data=data,
                        repo_root=repo_root,
                        indexes=indexes,
                        expected_scope_kind="repository",
                        distinct_from=(
                            str(record.get("review_decision", {}).get("role") if isinstance(record.get("review_decision"), Mapping) else record.get("reviewer")),
                            str(record.get("reviewer")),
                            str(data.get("repo_evidence_owner_role")),
                            str(data.get("fleet_steward_role")),
                        ),
                    )
                )
        _add_ref_issue(issues, indexes, "artifact_records", payload.get("pre_fix_failure_artifact_id"), f"{path}/payload/pre_fix_failure_artifact_id")
        _add_ref_issue(issues, indexes, "artifact_records", payload.get("fixed_pass_artifact_id"), f"{path}/payload/fixed_pass_artifact_id")
        _add_ref_issue(issues, indexes, "boundary_records", payload.get("boundary_record_id"), f"{path}/payload/boundary_record_id")
        _add_ref_issue(issues, indexes, "mutation_records", payload.get("mutation_record_id"), f"{path}/payload/mutation_record_id")
        _add_ref_issue(issues, indexes, "quarantine_records", payload.get("quarantine_id"), f"{path}/payload/quarantine_id")
        _add_ref_issue(issues, indexes, "artifact_records", payload.get("reproduction_artifact_id"), f"{path}/payload/reproduction_artifact_id")

    for index, record in enumerate(data.get("efficiency_records", [])):
        if not isinstance(record, Mapping):
            continue
        path = f"/efficiency_records/{index}"
        if type(record.get("sample_count")) is not int or record.get("sample_count") < 1:
            issues.append(_issue("efficiency_sample_count", f"{path}/sample_count", "sample_count must be positive"))
        costs = record.get("costs")
        if not isinstance(costs, Mapping):
            issues.append(_issue("cost_class_invalid", f"{path}/costs", "costs must be keyed by ORG-083 cost class"))
        else:
            missing = set(COST_CLASSES) - set(costs)
            for cost_class in sorted(missing):
                issues.append(_issue("cost_class_missing", f"{path}/costs/{cost_class}", "ORG-083 cost class is required"))
            for cost_class, cost in costs.items():
                if cost_class not in COST_CLASSES or not isinstance(cost, Mapping):
                    issues.append(_issue("cost_class_invalid", f"{path}/costs/{cost_class}", "unsupported cost class record"))
                    continue
                _add_ref_issue(issues, indexes, "artifact_records", cost.get("artifact_id"), f"{path}/costs/{cost_class}/artifact_id")
                if cost.get("authority_decision") is not None:
                    issues.extend(
                        _validate_authority_decision(
                            cost.get("authority_decision"),
                            path=f"{path}/costs/{cost_class}/authority_decision",
                            data=data,
                            repo_root=repo_root,
                            indexes=indexes,
                        )
                    )
        _add_ref_issue(issues, indexes, "artifact_records", record.get("artifact_id"), f"{path}/artifact_id")

    for index, record in enumerate(data.get("parity_records", [])):
        if not isinstance(record, Mapping):
            continue
        path = f"/parity_records/{index}"
        if record.get("verdict") not in ("exact", "evidenced_equivalent", "divergent_with_waiver", "local_only", "ci_only"):
            issues.append(_issue("parity_verdict", f"{path}/verdict", "unsupported parity verdict"))
        if record.get("verdict") == "divergent_with_waiver" and record.get("waiver_id") not in live_waivers:
            issues.append(_issue("parity_waiver_not_live", f"{path}/waiver_id", "divergent parity waiver must be live"))
        for ref in _list_refs(record.get("artifact_ids")):
            _add_ref_issue(issues, indexes, "artifact_records", ref, f"{path}/artifact_ids")
        _add_ref_issue(issues, indexes, "artifact_records", record.get("equivalence_evidence_id"), f"{path}/equivalence_evidence_id")
        _add_ref_issue(issues, indexes, "waiver_records", record.get("waiver_id"), f"{path}/waiver_id")

    for index, record in enumerate(data.get("retention_records", [])):
        if not isinstance(record, Mapping):
            continue
        path = f"/retention_records/{index}"
        _add_ref_issue(issues, indexes, "artifact_records", record.get("artifact_id"), f"{path}/artifact_id")
        availability = record.get("availability")
        receipts = record.get("retrieval_receipts")
        if not isinstance(availability, Mapping) or not isinstance(receipts, list):
            issues.append(_issue("retention_shape", path, "availability and retrieval_receipts are required"))
        else:
            receipt_ids = {receipt.get("receipt_id") for receipt in receipts if isinstance(receipt, Mapping)}
            receipt_id = availability.get("retrieval_receipt_id")
            if availability.get("state") != "verified_available":
                issues.append(_issue("retention_unavailable", path, "retained artifact is not verified available"))
            if receipt_id not in receipt_ids:
                issues.append(_issue("retention_receipt_unresolved", f"{path}/availability/retrieval_receipt_id", "receipt id must resolve inside retention record"))
        retention_end = record.get("retention_end")
        if isinstance(retention_end, Mapping) and retention_end.get("kind") == "timestamp":
            expires = _parse_time(retention_end.get("value"))
            if expires is None or expires <= now:
                issues.append(_issue("retention_expired", f"{path}/retention_end", "retention timestamp is expired or invalid"))
        elif not (isinstance(retention_end, Mapping) and retention_end.get("kind") == "event"):
            issues.append(_issue("retention_end", f"{path}/retention_end", "retention end must be timestamp or event"))
        _add_ref_issue(issues, indexes, "artifact_records", record.get("tombstone_artifact_id"), f"{path}/tombstone_artifact_id")

    for index, record in enumerate(data.get("generated_source_records", [])):
        if not isinstance(record, Mapping):
            continue
        path = f"/generated_source_records/{index}"
        if record.get("treatment") == "excluded":
            exclusion_id = record.get("exclusion_id")
            _add_ref_issue(issues, indexes, "exclusion_records", exclusion_id, f"{path}/exclusion_id")
            if exclusion_id not in live_exclusions:
                issues.append(_issue("generated_exclusion_not_live", f"{path}/exclusion_id", "generated treatment excluded requires live exclusion"))
        elif record.get("treatment") not in ("included", "separate"):
            issues.append(_issue("generated_treatment", f"{path}/treatment", "unsupported generated-source treatment"))

    for collection in ("exclusion_records", "waiver_records", "maturity_deferrals", "quarantine_records"):
        for index, record in enumerate(data.get(collection, [])):
            if not isinstance(record, Mapping):
                continue
            path = f"/{collection}/{index}"
            for field in ("repository", "revision", "scope", "active_state", "expiry", "expiry_action"):
                if field not in record:
                    issues.append(_issue("exception_field", f"{path}/{field}", "exception field is required"))
            if record.get("repository") != data.get("repository") or record.get("revision") != data.get("evidence_revision"):
                issues.append(_issue("exception_repository_revision", path, "exception repository/revision must match root"))
            expiry = record.get("expiry")
            if isinstance(expiry, Mapping) and expiry.get("kind") == "event":
                evidence_id = expiry.get("pending_evidence_id")
                _add_ref_issue(issues, indexes, "artifact_records", evidence_id, f"{path}/expiry/pending_evidence_id")
                if not _is_live_exception(record, indexes, now):
                    issues.append(_issue("event_pending_unproved", f"{path}/expiry", "event expiry lacks retrieved pending proof"))
            elif not _is_live_exception(record, indexes, now):
                issues.append(_issue("exception_not_live", path, "exception is expired, inactive, or indeterminate"))
            for ref in _list_refs(record.get("compensating_evidence_ids")):
                _add_ref_issue(issues, indexes, "artifact_records", ref, f"{path}/compensating_evidence_ids")
            if "authority_decision" not in record:
                issues.append(_issue("authority_decision_shape", f"{path}/authority_decision", "exception authority decision is required"))
            else:
                issues.extend(
                    _validate_authority_decision(
                        record.get("authority_decision"),
                        path=f"{path}/authority_decision",
                        data=data,
                        repo_root=repo_root,
                        indexes=indexes,
                        expected_role=record.get("approver") if isinstance(record.get("approver"), str) else None,
                        expected_scope_kind="obligation",
                    )
                )
            if collection == "waiver_records":
                if not _waiver_scope_valid(record):
                    issues.append(_issue("waiver_scope_invalid", f"{path}/scope", "waiver scope must narrowly bind positive_effectiveness"))
                if record.get("obligation") != "positive_effectiveness":
                    issues.append(_issue("waiver_scope_invalid", f"{path}/obligation", "waiver obligation must be positive_effectiveness"))
                if record.get("fallback") not in WAIVER_FALLBACKS:
                    issues.append(_issue("waiver_fallback_mismatch", f"{path}/fallback", "waiver fallback is unsupported"))
                expiry_action = record.get("expiry", {}).get("action") if isinstance(record.get("expiry"), Mapping) else None
                if record.get("fallback") != record.get("expiry_action") or record.get("fallback") != expiry_action:
                    issues.append(_issue("waiver_fallback_mismatch", path, "waiver fallback, expiry action, and expiry.action must match"))
                if not _list_refs(record.get("compensating_evidence_ids")):
                    issues.append(_issue("waiver_compensation_missing", f"{path}/compensating_evidence_ids", "effectiveness waiver requires compensation"))
                authority = record.get("authority_decision")
                if record.get("approver") != data.get("fleet_steward_role") or _decision_role(authority) != data.get("fleet_steward_role"):
                    issues.append(_issue("waiver_authority_invalid", f"{path}/authority_decision/role", "waiver approver and authority role must equal fleet steward"))
                if not _decision_matches_obligation_scope(
                    authority,
                    repository=str(data.get("repository", "")),
                    revision=str(data.get("evidence_revision", "")),
                    obligation="positive_effectiveness",
                    record_id=str(record.get("waiver_id", "")),
                ):
                    issues.append(_issue("waiver_authority_invalid", f"{path}/authority_decision/scope", "waiver authority must bind exact positive_effectiveness obligation"))
                independent = record.get("independent_review_decision")
                if independent is None:
                    issues.append(_issue("waiver_independent_review_invalid", f"{path}/independent_review_decision", "effectiveness waiver requires independent review"))
                else:
                    issues.extend(
                        _validate_authority_decision(
                            independent,
                            path=f"{path}/independent_review_decision",
                            data=data,
                            repo_root=repo_root,
                            indexes=indexes,
                            expected_scope_kind="obligation",
                            distinct_from=(
                                str(record.get("reviewer")),
                                str(record.get("owner")),
                                str(record.get("approver")),
                                str(data.get("repo_evidence_owner_role")),
                                str(data.get("fleet_steward_role")),
                                str(authority.get("role") if isinstance(authority, Mapping) else ""),
                            ),
                        )
                    )
                    if not _decision_matches_obligation_scope(
                        independent,
                        repository=str(data.get("repository", "")),
                        revision=str(data.get("evidence_revision", "")),
                        obligation="positive_effectiveness",
                        record_id=str(record.get("waiver_id", "")),
                    ):
                        issues.append(
                            _issue(
                                "waiver_independent_review_invalid",
                                f"{path}/independent_review_decision/scope",
                                "waiver independent review must bind exact positive_effectiveness obligation",
                            )
                        )
            if collection == "exclusion_records":
                for ref in _list_refs(record.get("affected_measurement_ids")):
                    _add_ref_issue(issues, indexes, "quantitative_measurements", ref, f"{path}/affected_measurement_ids")
            if collection == "quarantine_records":
                _add_ref_issue(issues, indexes, "flake_observations", record.get("flake_observation_id"), f"{path}/flake_observation_id")
                _add_ref_issue(issues, indexes, "retry_policy_records", record.get("retry_policy_id"), f"{path}/retry_policy_id")

    for artifact_id, record in indexes["artifact_records"].items():
        if record.get("artifact_class") != "enforcement_verification":
            continue
        path = f"/artifact_records/{artifact_id}/payload"
        payload = record.get("payload")
        if not isinstance(payload, Mapping):
            issues.append(_issue("enforcement_payload_invalid", path, "enforcement_verification artifact requires payload"))
            continue
        required = (
            "target",
            "mechanism",
            "command_fingerprint",
            "workflow_revision",
            "governed_scope",
            "expected_failure",
            "observed_failure_artifact_id",
            "observed_failure_digest",
            "restored_green_artifact_id",
            "escape_behavior",
            "rollback_or_waiver_path",
            "verified_at",
            "owner_decision",
            "fleet_decision",
            "failure_review_decision",
        )
        for field in required:
            if field not in payload:
                issues.append(_issue("enforcement_payload_invalid", f"{path}/{field}", "enforcement payload field is required"))
        if payload.get("target") != "enforcing":
            issues.append(_issue("enforcement_payload_invalid", f"{path}/target", "enforcement target must be enforcing"))
        for field, expected_role in (
            ("owner_decision", data.get("repo_evidence_owner_role")),
            ("fleet_decision", data.get("fleet_steward_role")),
        ):
            if payload.get(field) is None:
                issues.append(_issue("enforcement_approval_missing", f"{path}/{field}", "enforcement approval decision is required"))
            else:
                issues.extend(
                    _validate_authority_decision(
                        payload.get(field),
                        path=f"{path}/{field}",
                        data=data,
                        repo_root=repo_root,
                        indexes=indexes,
                        expected_role=expected_role if isinstance(expected_role, str) else None,
                        expected_scope_kind="enforcement",
                    )
                )
        independent = payload.get("failure_review_decision")
        if independent is None:
            issues.append(_issue("independent_reviewer_missing", f"{path}/failure_review_decision", "enforcement requires independent failure review decision"))
        else:
            issues.extend(
                _validate_authority_decision(
                    independent,
                    path=f"{path}/failure_review_decision",
                    data=data,
                    repo_root=repo_root,
                    indexes=indexes,
                    expected_scope_kind="enforcement",
                    distinct_from=(
                        str(data.get("repo_evidence_owner_role")),
                        str(data.get("fleet_steward_role")),
                    ),
                )
        )
        _add_ref_issue(issues, indexes, "artifact_records", payload.get("observed_failure_artifact_id"), f"{path}/observed_failure_artifact_id")
        _add_ref_issue(issues, indexes, "artifact_records", payload.get("restored_green_artifact_id"), f"{path}/restored_green_artifact_id")
        observed = indexes["artifact_records"].get(payload.get("observed_failure_artifact_id"))
        if not isinstance(observed, Mapping) or payload.get("observed_failure_digest") != observed.get("digest"):
            issues.append(_issue("failure_behavior_unproved", f"{path}/observed_failure_digest", "observed failure digest must match retained artifact"))
        if not payload.get("escape_behavior") or not payload.get("rollback_or_waiver_path"):
            issues.append(_issue("failure_behavior_unproved", path, "enforcement proof must show escape and rollback behavior"))

    return issues


def _validate_reachability(data: Mapping[str, Any], indexes: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> list[RepositoryEvidenceIssue]:
    issues: list[RepositoryEvidenceIssue] = []
    if data.get("repo_class") == "live_code":
        dimensions = {record.get("dimension") for record in data.get("dimension_dispositions", []) if isinstance(record, Mapping)}
        for dimension in DIMENSIONS:
            if dimension not in dimensions:
                issues.append(_issue("dimension_missing", "/dimension_dispositions", f"missing disposition for {dimension}"))
        boundaries = {record.get("boundary_kind") for record in data.get("boundary_records", []) if isinstance(record, Mapping)}
        for boundary in BOUNDARY_KINDS:
            if boundary not in boundaries:
                issues.append(_issue("boundary_missing", "/boundary_records", f"missing boundary disposition for {boundary}"))
    for collection in COLLECTION_ORDER:
        if data.get(collection) is None:
            issues.append(_issue("collection_missing", f"/{collection}", "root collection is required"))
    return issues


def _validate_owner_resolution(
    data: Mapping[str, Any],
    repo_root: Path,
) -> tuple[OwnerResolution, list[RepositoryEvidenceIssue]]:
    issues: list[RepositoryEvidenceIssue] = []
    owner = data.get("owner_resolution")
    requested_role = data.get("repo_evidence_owner_role") if isinstance(data.get("repo_evidence_owner_role"), str) else None
    resolution = resolve_repository_owner(data, repo_root, requested_role=requested_role)
    if resolution.issues:
        for detail in resolution.issues:
            issues.append(_issue("owner_resolution_failed", "/owner_resolution", detail))
    if not isinstance(owner, Mapping):
        issues.append(_issue("owner_resolution_missing", "/owner_resolution", "owner_resolution object is required"))
        return resolution, issues
    expected = {
        "eligible_rw_roles": list(resolution.eligible_rw_roles),
        "registry_revision": resolution.registry_revision,
        "registry_blob_id": resolution.registry_blob_id,
        "instance_blob_id": resolution.instance_blob_id,
        "requested_role": resolution.requested_role,
        "resolved_role": resolution.resolved_role,
        "basis": resolution.basis,
        "assignment_authority": resolution.assignment_authority,
    }
    for key, value in expected.items():
        if owner.get(key) != value:
            issues.append(_issue("owner_resolution_mismatch", f"/owner_resolution/{key}", "owner_resolution must match registry git-object bytes"))
    if owner.get("persona") or owner.get("account"):
        issues.append(_issue("owner_persona_not_authority", "/owner_resolution/persona", "persona/account strings are reporting only"))
    return resolution, issues


def _blocked(codes: set[str], fact: str) -> bool:
    for code in codes:
        targets = ISSUE_FACT_BLOCKERS.get(code, ("above_inventory",))
        if fact in targets:
            return True
    return False


def _artifact_ids_from_decision(decision: Any) -> list[str]:
    if isinstance(decision, Mapping) and isinstance(decision.get("artifact_id"), str):
        return [decision["artifact_id"]]
    return []


def _decision_role(decision: Any) -> str | None:
    if isinstance(decision, Mapping) and isinstance(decision.get("role"), str):
        return decision["role"]
    return None


def _decision_matches_obligation_scope(
    decision: Any,
    *,
    repository: str,
    revision: str,
    obligation: str,
    record_id: str,
) -> bool:
    if not isinstance(decision, Mapping):
        return False
    scope = decision.get("scope")
    return (
        isinstance(scope, Mapping)
        and scope.get("kind") == "obligation"
        and scope.get("repository") == repository
        and scope.get("revision") == revision
        and scope.get("obligation") == obligation
        and scope.get("record_id") == record_id
    )


def _waiver_scope_valid(record: Mapping[str, Any]) -> bool:
    scope = record.get("scope")
    include_selectors = scope.get("include_selectors") if isinstance(scope, Mapping) else None
    return (
        isinstance(scope, Mapping)
        and set(scope) == {"obligation", "include_selectors", "scope_fingerprint"}
        and scope.get("obligation") == "positive_effectiveness"
        and isinstance(include_selectors, list)
        and bool(include_selectors)
        and all(isinstance(item, str) and item for item in include_selectors)
        and isinstance(scope.get("scope_fingerprint"), str)
        and bool(scope.get("scope_fingerprint"))
    )


def _authority_roles(data: Mapping[str, Any], repo_root: Path) -> set[str]:
    try:
        authority = load_authority_documents(repo_root, str(data.get("registry_revision", "")))
    except RepositoryEvidenceError:
        return set()
    return set(_instance_roles(authority.instance_yaml))


def _validate_authority_decision(
    decision: Any,
    *,
    path: str,
    data: Mapping[str, Any],
    repo_root: Path,
    indexes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    expected_role: str | None = None,
    expected_scope_kind: str | None = None,
    distinct_from: Sequence[str] = (),
) -> list[RepositoryEvidenceIssue]:
    issues: list[RepositoryEvidenceIssue] = []
    if not isinstance(decision, Mapping):
        issues.append(_issue("authority_decision_shape", path, "authority decision must be a map"))
        return issues
    required = ("decision_id", "role", "decision_ref", "decided_at", "scope", "artifact_id")
    for field in required:
        if field not in decision:
            issues.append(_issue("authority_decision_shape", f"{path}/{field}", "authority decision field is required"))
    role = decision.get("role")
    if isinstance(role, str) and role not in _authority_roles(data, repo_root):
        issues.append(_issue("authority_decision_role_unknown", f"{path}/role", "authority role must resolve from instance.yaml"))
    if expected_role is not None and role != expected_role:
        issues.append(_issue("authority_decision_role_mismatch", f"{path}/role", f"authority role must equal {expected_role}"))
    if isinstance(role, str) and role in set(distinct_from):
        issues.append(_issue("independent_reviewer_not_distinct", f"{path}/role", "independent reviewer role must be distinct"))
    scope = decision.get("scope")
    if not isinstance(scope, Mapping):
        issues.append(_issue("authority_decision_shape", f"{path}/scope", "authority decision scope must be a map"))
    else:
        kind = scope.get("kind")
        if kind not in AUTHORITY_SCOPE_KINDS:
            issues.append(_issue("authority_decision_shape", f"{path}/scope/kind", "authority decision scope kind is unsupported"))
        if expected_scope_kind is not None and kind != expected_scope_kind:
            issues.append(_issue("authority_decision_role_mismatch", f"{path}/scope/kind", f"authority scope must be {expected_scope_kind}"))
        if scope.get("repository") != data.get("repository"):
            issues.append(_issue("authority_decision_shape", f"{path}/scope", "authority decision scope repository must match root"))
        if kind != "repository" and scope.get("revision") != data.get("evidence_revision"):
            issues.append(_issue("authority_decision_shape", f"{path}/scope", "authority decision scope revision must match root"))
        if kind == "repository":
            pass
        elif kind == "dimension":
            if scope.get("dimension") not in DIMENSIONS or not isinstance(scope.get("selectors"), list):
                issues.append(_issue("authority_decision_shape", f"{path}/scope", "dimension scope requires dimension and selectors"))
        elif kind == "boundary":
            if scope.get("boundary_kind") not in BOUNDARY_KINDS or not isinstance(scope.get("selectors"), list):
                issues.append(_issue("authority_decision_shape", f"{path}/scope", "boundary scope requires boundary_kind and selectors"))
        elif kind == "measurement":
            if not isinstance(scope.get("measurement_id"), str) or scope.get("dimension") not in DIMENSIONS:
                issues.append(_issue("authority_decision_shape", f"{path}/scope", "measurement scope requires measurement_id and dimension"))
        elif kind == "obligation":
            if not isinstance(scope.get("obligation"), str) or not isinstance(scope.get("record_id"), str):
                issues.append(_issue("authority_decision_shape", f"{path}/scope", "obligation scope requires obligation and record_id"))
        elif kind == "enforcement":
            if not isinstance(scope.get("governed_scope"), list) or not isinstance(scope.get("mechanism"), str):
                issues.append(_issue("authority_decision_shape", f"{path}/scope", "enforcement scope requires governed_scope and mechanism"))
    _add_ref_issue(issues, indexes, "artifact_records", decision.get("artifact_id"), f"{path}/artifact_id")
    artifact_id = decision.get("artifact_id")
    if (
        isinstance(artifact_id, str)
        and artifact_id in indexes["artifact_records"]
        and artifact_id not in {retention.get("artifact_id") for retention in indexes["retention_records"].values()}
    ):
        issues.append(_issue("authority_decision_artifact_unretained", f"{path}/artifact_id", "authority decision artifact must be retained"))
    return issues


def _authority_decision_issue_free(
    decision: Any,
    *,
    path: str,
    data: Mapping[str, Any],
    repo_root: Path,
    indexes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    expected_role: str | None = None,
    expected_scope_kind: str | None = None,
    distinct_from: Sequence[str] = (),
) -> bool:
    return not _validate_authority_decision(
        decision,
        path=path,
        data=data,
        repo_root=repo_root,
        indexes=indexes,
        expected_role=expected_role,
        expected_scope_kind=expected_scope_kind,
        distinct_from=distinct_from,
    )


def _decision_artifact_ids(record: Mapping[str, Any], fields: Sequence[str]) -> list[str]:
    ids: list[str] = []
    for field in fields:
        ids.extend(_artifact_ids_from_decision(record.get(field)))
    return ids


def support_artifact_ids(contract: Mapping[str, Any], target_stage: str) -> tuple[str, ...]:
    stage_index = _maturity_index(target_stage)
    if stage_index is None:
        stage_index = 0
    ids: set[str] = set()
    inventory = contract.get("inventory")
    if isinstance(inventory, Mapping):
        ids.update(ref for ref in _list_refs(inventory.get("applicability_evidence_ids")) if isinstance(ref, str))
    for collection in ("exclusion_records", "waiver_records", "maturity_deferrals", "quarantine_records"):
        for record in contract.get(collection, []):
            if not isinstance(record, Mapping):
                continue
            active = record.get("active_state")
            expiry = record.get("expiry")
            if isinstance(active, Mapping) and isinstance(active.get("evidence_id"), str):
                ids.add(active["evidence_id"])
            if isinstance(expiry, Mapping) and expiry.get("kind") == "event" and isinstance(expiry.get("pending_evidence_id"), str):
                ids.add(expiry["pending_evidence_id"])
            ids.update(ref for ref in _list_refs(record.get("compensating_evidence_ids")) if isinstance(ref, str))
            ids.update(_artifact_ids_from_decision(record.get("authority_decision")))
            if collection == "waiver_records":
                ids.update(_artifact_ids_from_decision(record.get("independent_review_decision")))
            if collection == "quarantine_records":
                ids.update(ref for ref in _list_refs(record.get("evidence_ids")) if isinstance(ref, str))
    for record in contract.get("deploy_tolerance_records", []):
        if isinstance(record, Mapping) and isinstance(record.get("evidence_artifact_id"), str):
            ids.add(record["evidence_artifact_id"])
    for record in contract.get("dimension_dispositions", []):
        if isinstance(record, Mapping):
            ids.update(ref for ref in _list_refs(record.get("applicability_evidence_ids")) if isinstance(ref, str))
            ids.update(_decision_artifact_ids(record, ("applicability_decision", "approved_disposition_decision")))
    for record in contract.get("boundary_records", []):
        if isinstance(record, Mapping):
            ids.update(ref for ref in _list_refs(record.get("applicability_evidence_ids")) if isinstance(ref, str))
            ids.update(_decision_artifact_ids(record, ("applicability_decision", "approved_disposition_decision")))
    if stage_index >= 1:
        for collection, fields in (
            ("quantitative_measurements", ("artifact_id",)),
            ("changed_code_records", ("diff_artifact_id",)),
            ("mutation_records", ("artifact_id",)),
            ("flake_observations", ("sequence_artifact_id", "artifact_id")),
        ):
            for record in contract.get(collection, []):
                if isinstance(record, Mapping):
                    ids.update(record[field] for field in fields if isinstance(record.get(field), str))
        for record in contract.get("mutation_records", []):
            if isinstance(record, Mapping):
                for item in _list_refs(record.get("terminal_dispositions")):
                    if isinstance(item, Mapping) and isinstance(item.get("reviewer_evidence_id"), str):
                        ids.add(item["reviewer_evidence_id"])
        for record in contract.get("boundary_records", []):
            if isinstance(record, Mapping):
                ids.update(ref for ref in _list_refs(record.get("artifact_ids")) if isinstance(ref, str))
        for record in contract.get("parity_records", []):
            if isinstance(record, Mapping):
                ids.update(ref for ref in _list_refs(record.get("artifact_ids")) if isinstance(ref, str))
                if isinstance(record.get("equivalence_evidence_id"), str):
                    ids.add(record["equivalence_evidence_id"])
        for record in contract.get("quantitative_measurements", []):
            if isinstance(record, Mapping):
                for section in ("threshold", "ratchet", "comparability"):
                    value = record.get(section)
                    if isinstance(value, Mapping):
                        ids.update(
                            _decision_artifact_ids(
                                value,
                                (
                                    "proposal_decision",
                                    "approval_decision",
                                    "adoption_decision",
                                    "equivalence_decision",
                                    "reset_authority_decision",
                                ),
                            )
                        )
                        if isinstance(value.get("equivalence_artifact_id"), str):
                            ids.add(value["equivalence_artifact_id"])
                        if isinstance(value.get("reset_artifact_id"), str):
                            ids.add(value["reset_artifact_id"])
    if stage_index >= 2:
        for record in contract.get("effectiveness_receipts", []):
            if isinstance(record, Mapping):
                ids.update(_decision_artifact_ids(record, ("review_decision", "independent_review_decision")))
                payload = record.get("payload")
                if isinstance(payload, Mapping):
                    for field in ("pre_fix_failure_artifact_id", "fixed_pass_artifact_id", "reproduction_artifact_id"):
                        if isinstance(payload.get(field), str):
                            ids.add(payload[field])
        for record in contract.get("efficiency_records", []):
            if isinstance(record, Mapping):
                if isinstance(record.get("artifact_id"), str):
                    ids.add(record["artifact_id"])
                costs = record.get("costs")
                if isinstance(costs, Mapping):
                    for cost in costs.values():
                        if isinstance(cost, Mapping):
                            if isinstance(cost.get("artifact_id"), str):
                                ids.add(cost["artifact_id"])
                            ids.update(_artifact_ids_from_decision(cost.get("authority_decision")))
        for artifact in contract.get("artifact_records", []):
            if isinstance(artifact, Mapping) and artifact.get("artifact_class") == "dry_run_validator":
                ids.add(str(artifact.get("artifact_id")))
    if stage_index >= 3:
        for artifact in contract.get("artifact_records", []):
            if not isinstance(artifact, Mapping) or artifact.get("artifact_class") != "enforcement_verification":
                continue
            if isinstance(artifact.get("artifact_id"), str):
                ids.add(artifact["artifact_id"])
            payload = artifact.get("payload")
            if isinstance(payload, Mapping):
                for field in ("observed_failure_artifact_id", "restored_green_artifact_id"):
                    if isinstance(payload.get(field), str):
                        ids.add(payload[field])
                ids.update(_decision_artifact_ids(payload, ("owner_decision", "fleet_decision", "failure_review_decision")))
    return tuple(sorted(ids))


def _retention_matches_artifact(retention: Mapping[str, Any], artifact: Mapping[str, Any]) -> bool:
    availability = retention.get("availability")
    receipts = retention.get("retrieval_receipts")
    if not isinstance(availability, Mapping) or not isinstance(receipts, list):
        return False
    receipt = next((item for item in receipts if isinstance(item, Mapping) and item.get("receipt_id") == availability.get("retrieval_receipt_id")), None)
    return (
        retention.get("artifact_class") == artifact.get("artifact_class")
        and retention.get("storage_locator") == artifact.get("storage_locator")
        and retention.get("digest_algorithm") == artifact.get("digest_algorithm") == "sha256"
        and retention.get("digest") == artifact.get("digest")
        and availability.get("state") == "verified_available"
        and isinstance(receipt, Mapping)
        and receipt.get("observed_digest_algorithm") == "sha256"
        and receipt.get("observed_digest") == artifact.get("digest")
        and receipt.get("observed_size") == artifact.get("size")
    )


def _validate_support_artifacts(
    data: Mapping[str, Any],
    indexes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    target_stage: str,
) -> list[RepositoryEvidenceIssue]:
    if _maturity_index(target_stage) == 0:
        return []
    issues: list[RepositoryEvidenceIssue] = []
    retentions_by_artifact: dict[str, list[Mapping[str, Any]]] = {}
    for retention in indexes["retention_records"].values():
        artifact_id = retention.get("artifact_id")
        if isinstance(artifact_id, str):
            retentions_by_artifact.setdefault(artifact_id, []).append(retention)
    for artifact_id in support_artifact_ids(data, target_stage):
        artifact = indexes["artifact_records"].get(artifact_id)
        if artifact is None:
            issues.append(_issue("support_artifact_missing", "/artifact_records", f"support artifact {artifact_id} is missing"))
            continue
        candidates = retentions_by_artifact.get(artifact_id, [])
        if len(candidates) != 1:
            issues.append(_issue("retention_not_exactly_one", f"/retention_records/{artifact_id}", "support artifact must have exactly one retention record"))
            continue
        if not _retention_matches_artifact(candidates[0], artifact):
            issues.append(_issue("support_retention_mismatch", f"/retention_records/{artifact_id}", "support retention does not match artifact bytes and receipt"))
    return issues


def _support_artifacts_retained(
    data: Mapping[str, Any],
    indexes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    target_stage: str,
) -> bool:
    if _maturity_index(target_stage) == 0:
        return True
    retentions_by_artifact: dict[str, list[Mapping[str, Any]]] = {}
    for retention in indexes["retention_records"].values():
        artifact_id = retention.get("artifact_id")
        if isinstance(artifact_id, str):
            retentions_by_artifact.setdefault(artifact_id, []).append(retention)
    for artifact_id in support_artifact_ids(data, target_stage):
        artifact = indexes["artifact_records"].get(artifact_id)
        if artifact is None:
            return False
        candidates = retentions_by_artifact.get(artifact_id, [])
        if len(candidates) != 1 or not _retention_matches_artifact(candidates[0], artifact):
            return False
    return True


def _artifact_ids_retained(
    artifact_ids: Sequence[str],
    indexes: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> bool:
    for artifact_id in artifact_ids:
        artifact = indexes["artifact_records"].get(artifact_id)
        if artifact is None:
            return False
        matches = [
            retention
            for retention in indexes["retention_records"].values()
            if retention.get("artifact_id") == artifact_id
        ]
        if len(matches) != 1 or not _retention_matches_artifact(matches[0], artifact):
            return False
    return True


def _all_error_codes(issues: Sequence[RepositoryEvidenceIssue]) -> set[str]:
    return {issue.code for issue in issues if issue.severity == "error"}


def _approved_disposition(record: Mapping[str, Any], dimension: str) -> bool:
    decision = record.get("approved_disposition_decision") or record.get("applicability_decision")
    if not isinstance(decision, Mapping):
        return False
    scope = decision.get("scope")
    return (
        isinstance(scope, Mapping)
        and scope.get("kind") == "dimension"
        and scope.get("dimension") == dimension
        and isinstance(scope.get("selectors"), list)
    )


def _measurement_baseline_current_valid(
    measurement: Mapping[str, Any],
    dimension: str,
    indexes: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> bool:
    if measurement.get("dimension") != dimension:
        return False
    baseline = measurement.get("baseline")
    current = measurement.get("current")
    comparability = measurement.get("comparability")
    if not isinstance(baseline, Mapping) or not isinstance(current, Mapping) or not isinstance(comparability, Mapping):
        return False
    baseline_record = indexes["quantitative_measurements"].get(baseline.get("measurement_id"))
    current_record = indexes["quantitative_measurements"].get(current.get("measurement_id"))
    if not isinstance(baseline_record, Mapping) or not isinstance(current_record, Mapping):
        return False
    same_dimension = baseline_record.get("dimension") == current_record.get("dimension") == dimension
    same_unit = baseline_record.get("unit") == current_record.get("unit") == measurement.get("unit")
    same_scope = baseline_record.get("scope") == current_record.get("scope") == measurement.get("scope")
    checked = set(_list_refs(comparability.get("checked_fields")))
    return same_dimension and same_unit and same_scope and comparability.get("verdict") == "comparable" and checked == set(COMPARABILITY_CHECKED_FIELDS)


def _baseline_measurements_or_dispositions(
    data: Mapping[str, Any],
    indexes: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> bool:
    dispositions = {
        record.get("dimension"): record
        for record in data.get("dimension_dispositions", [])
        if isinstance(record, Mapping)
    }
    if set(DIMENSIONS) - set(dispositions):
        return False
    for dimension in ("line", "branch"):
        record = dispositions[dimension]
        if record.get("status") == "measured":
            measurement_ids = _list_refs(record.get("measurement_ids"))
            if not measurement_ids:
                return False
            if not all(
                isinstance(measurement_id, str)
                and isinstance(indexes["quantitative_measurements"].get(measurement_id), Mapping)
                and _measurement_baseline_current_valid(indexes["quantitative_measurements"][measurement_id], dimension, indexes)
                for measurement_id in measurement_ids
            ):
                return False
        elif not _approved_disposition(record, dimension):
            return False
    for dimension in ("function", "changed_code"):
        record = dispositions[dimension]
        if record.get("status") not in ("measured", "deferred", "unsupported", "excluded", "not_applicable"):
            return False
        if record.get("status") != "measured" and not _approved_disposition(record, dimension):
            return False
    for dimension in ("mutation", "flake"):
        if dispositions[dimension].get("status") not in ("measured", "deferred", "unsupported", "excluded", "not_applicable"):
            return False
    return True


def _boundary_prerequisites(data: Mapping[str, Any]) -> bool:
    records = [record for record in data.get("boundary_records", []) if isinstance(record, Mapping)]
    if data.get("repo_class") == "live_code" and {record.get("boundary_kind") for record in records} != set(BOUNDARY_KINDS):
        return False
    for record in records:
        status = record.get("status")
        if status in ("exercised", "simulated"):
            if not _list_refs(record.get("artifact_ids")):
                return False
            continue
        if status in ("not_exercised", "not_applicable"):
            decision = record.get("approved_disposition_decision") or record.get("applicability_decision")
            if not isinstance(decision, Mapping):
                return False
            scope = decision.get("scope")
            if not isinstance(scope, Mapping) or scope.get("kind") != "boundary" or scope.get("boundary_kind") != record.get("boundary_kind"):
                return False
            continue
        return False
    return True


def _all_cost_classes_recorded(data: Mapping[str, Any]) -> bool:
    records = [record for record in data.get("efficiency_records", []) if isinstance(record, Mapping)]
    if not records:
        return False
    return all(isinstance(record.get("costs"), Mapping) and set(COST_CLASSES) <= set(record["costs"]) for record in records)


def _decision_present(record: Mapping[str, Any], field: str, scope_kind: str, role: str | None = None) -> bool:
    decision = record.get(field)
    if not isinstance(decision, Mapping):
        return False
    scope = decision.get("scope")
    if not isinstance(scope, Mapping) or scope.get("kind") != scope_kind:
        return False
    return role is None or decision.get("role") == role


def _positive_effectiveness_records(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    positive_kinds = ("behavioral_falsification", "historical_fault_regression", "real_boundary", "mutation")
    return [
        record
        for record in data.get("effectiveness_receipts", [])
        if isinstance(record, Mapping) and record.get("kind") in positive_kinds
    ]


def _positive_receipt_branch_valid(
    data: Mapping[str, Any],
    indexes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    repo_root: Path,
) -> tuple[bool, bool, tuple[str, ...]]:
    retained_ids = {record.get("artifact_id") for record in indexes["retention_records"].values()}
    for record in _positive_effectiveness_records(data):
        review = record.get("review_decision")
        independent = record.get("independent_review_decision")
        if not (
            _authority_decision_issue_free(
                review,
                path="/effectiveness_receipts/review_decision",
                data=data,
                repo_root=repo_root,
                indexes=indexes,
                expected_scope_kind="repository",
            )
            and _authority_decision_issue_free(
                independent,
                path="/effectiveness_receipts/independent_review_decision",
                data=data,
                repo_root=repo_root,
                indexes=indexes,
                expected_scope_kind="repository",
                distinct_from=(
                    str(_decision_role(review) or ""),
                    str(record.get("reviewer")),
                    str(data.get("repo_evidence_owner_role")),
                    str(data.get("fleet_steward_role")),
                ),
            )
        ):
            continue
        payload = record.get("payload")
        artifact_ids = _decision_artifact_ids(record, ("review_decision", "independent_review_decision"))
        if isinstance(payload, Mapping):
            for field in ("pre_fix_failure_artifact_id", "fixed_pass_artifact_id", "reproduction_artifact_id"):
                if isinstance(payload.get(field), str):
                    artifact_ids.append(payload[field])
        if artifact_ids and all(artifact_id in retained_ids for artifact_id in artifact_ids) and _artifact_ids_retained(artifact_ids, indexes):
            return True, True, tuple(sorted(set(artifact_ids)))
    return False, False, ()


def _live_waiver_branch_valid(
    data: Mapping[str, Any],
    indexes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    repo_root: Path,
    now: datetime,
) -> tuple[bool, bool, tuple[str, ...]]:
    repository = str(data.get("repository", ""))
    revision = str(data.get("evidence_revision", ""))
    owner_role = str(data.get("repo_evidence_owner_role", ""))
    steward_role = str(data.get("fleet_steward_role", ""))
    for record in data.get("waiver_records", []):
        if not isinstance(record, Mapping):
            continue
        waiver_id = str(record.get("waiver_id", ""))
        authority = record.get("authority_decision")
        independent = record.get("independent_review_decision")
        expiry = record.get("expiry")
        expiry_action = expiry.get("action") if isinstance(expiry, Mapping) else None
        compensation = [ref for ref in _list_refs(record.get("compensating_evidence_ids")) if isinstance(ref, str)]
        if not (
            _is_live_exception(record, indexes, now)
            and record.get("obligation") == "positive_effectiveness"
            and record.get("approver") == steward_role
            and _waiver_scope_valid(record)
            and record.get("fallback") in WAIVER_FALLBACKS
            and record.get("fallback") == record.get("expiry_action") == expiry_action
            and compensation
            and _artifact_ids_retained(compensation, indexes)
            and _decision_matches_obligation_scope(
                authority,
                repository=repository,
                revision=revision,
                obligation="positive_effectiveness",
                record_id=waiver_id,
            )
            and _decision_matches_obligation_scope(
                independent,
                repository=repository,
                revision=revision,
                obligation="positive_effectiveness",
                record_id=waiver_id,
            )
            and _authority_decision_issue_free(
                authority,
                path="/waiver_records/authority_decision",
                data=data,
                repo_root=repo_root,
                indexes=indexes,
                expected_role=steward_role,
                expected_scope_kind="obligation",
            )
            and _authority_decision_issue_free(
                independent,
                path="/waiver_records/independent_review_decision",
                data=data,
                repo_root=repo_root,
                indexes=indexes,
                expected_scope_kind="obligation",
                distinct_from=(
                    str(record.get("reviewer")),
                    str(record.get("owner")),
                    str(record.get("approver")),
                    owner_role,
                    steward_role,
                    str(_decision_role(authority) or ""),
                ),
            )
        ):
            continue
        artifact_ids = [
            *compensation,
            *_artifact_ids_from_decision(authority),
            *_artifact_ids_from_decision(independent),
        ]
        if artifact_ids and _artifact_ids_retained(artifact_ids, indexes):
            return True, True, tuple(sorted(set(artifact_ids)))
    return False, False, ()


def _effectiveness_alternative(
    data: Mapping[str, Any],
    indexes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    *,
    repo_root: Path,
    now: datetime,
) -> EffectivenessAlternative:
    receipt_valid, receipt_review, receipt_ids = _positive_receipt_branch_valid(data, indexes, repo_root=repo_root)
    waiver_valid, waiver_review, waiver_ids = _live_waiver_branch_valid(data, indexes, repo_root=repo_root, now=now)
    if receipt_valid:
        return EffectivenessAlternative(True, waiver_valid, receipt_review, receipt_ids)
    if waiver_valid:
        return EffectivenessAlternative(False, True, waiver_review, waiver_ids)
    return EffectivenessAlternative(False, False, False, ())


def _retained_artifact_classes(indexes: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> set[str]:
    retained = {record.get("artifact_id") for record in indexes["retention_records"].values()}
    return {
        artifact.get("artifact_class")
        for artifact_id, artifact in indexes["artifact_records"].items()
        if artifact_id in retained and isinstance(artifact.get("artifact_class"), str)
    }


def _threshold_ratchet_facts(data: Mapping[str, Any]) -> tuple[bool, bool, bool, bool]:
    measurements = [record for record in data.get("quantitative_measurements", []) if isinstance(record, Mapping)]
    threshold_records = [record.get("threshold") for record in measurements if isinstance(record.get("threshold"), Mapping)]
    ratchet_records = [record.get("ratchet") for record in measurements if isinstance(record.get("ratchet"), Mapping)]
    owner_role = data.get("repo_evidence_owner_role") if isinstance(data.get("repo_evidence_owner_role"), str) else None
    steward_role = data.get("fleet_steward_role") if isinstance(data.get("fleet_steward_role"), str) else None
    threshold_proposed = bool(threshold_records) and all(
        record.get("state") in ("proposed", "advisory")
        and record.get("comparator") in THRESHOLD_COMPARATORS
        and type(record.get("value")) in (int, float)
        and _decision_present(record, "proposal_decision", "measurement", owner_role)
        for record in threshold_records
    )
    threshold_approved = bool(threshold_records) and all(
        record.get("state") == "enforcing"
        and record.get("comparator") in THRESHOLD_COMPARATORS
        and type(record.get("value")) in (int, float)
        and _decision_present(record, "approval_decision", "measurement", steward_role)
        for record in threshold_records
    )
    ratchet_proposed = bool(ratchet_records) and all(
        record.get("state") == "proposed"
        and record.get("direction") in RATCHET_DIRECTIONS
        and type(record.get("proposed")) in (int, float)
        and _decision_present(record, "proposal_decision", "measurement", owner_role)
        for record in ratchet_records
    )
    ratchet_adopted = bool(ratchet_records) and all(
        record.get("state") == "adopted"
        and record.get("direction") in RATCHET_DIRECTIONS
        and type(record.get("adopted")) in (int, float)
        and _decision_present(record, "adoption_decision", "measurement", steward_role)
        for record in ratchet_records
    )
    return threshold_proposed, threshold_approved, ratchet_proposed, ratchet_adopted


def _current_evidence_valid(data: Mapping[str, Any]) -> bool:
    records = [record for record in data.get("quantitative_measurements", []) if isinstance(record, Mapping)]
    if not records:
        return False
    for record in records:
        comparability = record.get("comparability")
        if not isinstance(comparability, Mapping):
            return False
        if comparability.get("verdict") != "comparable":
            return False
    return True


def _enforcement_facts(indexes: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> tuple[bool, bool]:
    retained_artifact_ids = {record.get("artifact_id") for record in indexes["retention_records"].values()}
    for artifact in indexes["artifact_records"].values():
        if artifact.get("artifact_class") != "enforcement_verification":
            continue
        if artifact.get("artifact_id") not in retained_artifact_ids:
            continue
        payload = artifact.get("payload")
        if not isinstance(payload, Mapping):
            continue
        decisions = (
            payload.get("owner_decision"),
            payload.get("fleet_decision"),
            payload.get("failure_review_decision"),
        )
        machinery = payload.get("target") == "enforcing" and all(isinstance(decision, Mapping) for decision in decisions)
        proof = (
            isinstance(payload.get("observed_failure_artifact_id"), str)
            and isinstance(payload.get("observed_failure_digest"), str)
            and isinstance(payload.get("restored_green_artifact_id"), str)
            and isinstance(payload.get("escape_behavior"), str)
            and isinstance(payload.get("rollback_or_waiver_path"), str)
            and isinstance(payload.get("verified_at"), str)
        )
        if machinery or proof:
            return machinery, proof
    return False, False


def _compute_facts(
    data: Mapping[str, Any],
    issues: Sequence[RepositoryEvidenceIssue],
    indexes: Mapping[str, Mapping[str, Mapping[str, Any]]],
    owner_resolution: OwnerResolution,
    *,
    repo_root: Path,
    now: datetime,
) -> RepositoryEvidenceFacts:
    error_codes = _all_error_codes(issues)
    codes = tuple(sorted(error_codes))
    threshold_proposed, threshold_approved, ratchet_proposed, ratchet_adopted = _threshold_ratchet_facts(data)
    effectiveness = _effectiveness_alternative(data, indexes, repo_root=repo_root, now=now)
    artifact_classes = _retained_artifact_classes(indexes)
    machinery_retained, failure_proof_retained = _enforcement_facts(indexes)
    baseline_support_retained = _support_artifacts_retained(data, indexes, "baseline")
    advisory_support_retained = _support_artifacts_retained(data, indexes, "advisory")
    enforcing_support_retained = _support_artifacts_retained(data, indexes, "enforcing")
    parity_records = [record for record in data.get("parity_records", []) if isinstance(record, Mapping)]
    parity_eligible = bool(parity_records) and all(
        record.get("verdict") in ("exact", "evidenced_equivalent", "divergent_with_waiver")
        for record in parity_records
    ) and not _blocked(error_codes, "parity_eligible")
    positive_effectiveness_or_waiver = (
        effectiveness.receipt_valid or effectiveness.live_waiver_valid
    ) and not _blocked(error_codes, "positive_effectiveness_or_waiver")
    independent_review_valid = effectiveness.independent_review_valid and not _blocked(error_codes, "independent_review_valid")
    structural_valid = not _blocked(error_codes, "structural_valid")
    authority_valid = not _blocked(error_codes, "authority_valid")
    owner_resolved = owner_resolution.resolved_role is not None and not owner_resolution.issues and not _blocked(error_codes, "owner_resolved")
    role_decisions_valid = not _blocked(error_codes, "role_decisions_valid")
    artifact_refs_resolved = not _blocked(error_codes, "artifact_refs_resolved")
    retention_valid = not _blocked(error_codes, "retention_valid")
    return RepositoryEvidenceFacts(
        structural_valid=structural_valid,
        authority_valid=authority_valid,
        owner_resolved=owner_resolved,
        role_decisions_valid=role_decisions_valid,
        artifact_refs_resolved=artifact_refs_resolved,
        stage_artifacts_retained=baseline_support_retained and not _blocked(error_codes, "stage_artifacts_retained"),
        retention_valid=retention_valid,
        exceptions_live=not _blocked(error_codes, "exceptions_live"),
        measurement_prerequisites=_baseline_measurements_or_dispositions(data, indexes) and not _blocked(error_codes, "measurement_prerequisites"),
        boundary_prerequisites=_boundary_prerequisites(data) and not _blocked(error_codes, "boundary_prerequisites"),
        generated_treatment_valid=not _blocked(error_codes, "generated_treatment_valid"),
        comparability_valid=not _blocked(error_codes, "comparability_valid"),
        parity_eligible=parity_eligible,
        positive_effectiveness_or_waiver=positive_effectiveness_or_waiver and advisory_support_retained,
        efficiency_complete=_all_cost_classes_recorded(data) and advisory_support_retained and not _blocked(error_codes, "efficiency_complete"),
        dry_run_retained="dry_run_validator" in artifact_classes and advisory_support_retained and not _blocked(error_codes, "dry_run_retained"),
        threshold_proposed=threshold_proposed and not _blocked(error_codes, "threshold_proposed"),
        threshold_approved=threshold_approved and not _blocked(error_codes, "threshold_approved"),
        ratchet_proposed=ratchet_proposed and not _blocked(error_codes, "ratchet_proposed"),
        ratchet_adopted=ratchet_adopted and not _blocked(error_codes, "ratchet_adopted"),
        owner_proposal_valid=threshold_proposed and ratchet_proposed and not _blocked(error_codes, "owner_proposal_valid"),
        steward_adoption_valid=threshold_approved and ratchet_adopted and not _blocked(error_codes, "steward_adoption_valid"),
        independent_review_valid=independent_review_valid,
        current_evidence_valid=_current_evidence_valid(data) and not _blocked(error_codes, "current_evidence_valid"),
        enforcement_machinery_retained=machinery_retained and enforcing_support_retained and not _blocked(error_codes, "enforcement_machinery_retained"),
        enforcement_failure_proof_retained=failure_proof_retained and enforcing_support_retained and not _blocked(error_codes, "enforcement_failure_proof_retained"),
        claim_valid=not _blocked(error_codes, "claim_valid"),
        blocking_issue_codes=codes,
    )


def derive_repository_maturity(facts: RepositoryEvidenceFacts) -> str:
    if not (
        facts.structural_valid
        and facts.authority_valid
        and facts.owner_resolved
        and facts.role_decisions_valid
        and facts.exceptions_live
    ):
        return "inventory_only"
    if not (
        facts.artifact_refs_resolved
        and facts.stage_artifacts_retained
        and facts.retention_valid
        and facts.measurement_prerequisites
        and facts.boundary_prerequisites
        and facts.generated_treatment_valid
        and facts.comparability_valid
        and facts.parity_eligible
    ):
        return "inventory_only"
    stage = "baseline"
    if (
        facts.positive_effectiveness_or_waiver
        and facts.efficiency_complete
        and facts.dry_run_retained
        and facts.threshold_proposed
        and facts.ratchet_proposed
        and facts.owner_proposal_valid
        and facts.independent_review_valid
    ):
        stage = "advisory"
    if (
        stage == "advisory"
        and facts.threshold_approved
        and facts.ratchet_adopted
        and facts.steward_adoption_valid
        and facts.current_evidence_valid
        and facts.enforcement_machinery_retained
        and facts.enforcement_failure_proof_retained
    ):
        stage = "enforcing"
    return stage


def _maturity_index(stage: Any) -> int | None:
    if stage not in MATURITY_ORDER:
        return None
    return MATURITY_ORDER.index(stage)


def validate_repository_evidence(
    data: Mapping[str, Any],
    *,
    repo_root: str | Path = ".",
    now: datetime | None = None,
) -> RepositoryEvidenceContract:
    root = Path(repo_root)
    evaluated_at = now or datetime.now(timezone.utc)
    if evaluated_at.tzinfo is None:
        raise RepositoryEvidenceError("now must be timezone-aware")
    issues: list[RepositoryEvidenceIssue] = []
    issues.extend(_validate_root(data))
    indexes, id_issues = _ids_by_collection(data)
    issues.extend(id_issues)
    issues.extend(_validate_repository_revision(data))
    owner_resolution, owner_issues = _validate_owner_resolution(data, root)
    issues.extend(owner_issues)
    issues.extend(_validate_artifact_paths_and_digests(data, indexes, root))
    issues.extend(_validate_refs_and_semantics(data, indexes, root, evaluated_at))
    issues.extend(_validate_reachability(data, indexes))
    if isinstance(data.get("maturity"), str):
        issues.extend(_validate_support_artifacts(data, indexes, str(data.get("maturity"))))
    issues = sorted(issues, key=_issue_sort_key)
    facts = _compute_facts(data, issues, indexes, owner_resolution, repo_root=root, now=evaluated_at)
    derived = derive_repository_maturity(facts)
    claimed = _maturity_index(data.get("maturity"))
    ceiling = _maturity_index(derived)
    if claimed is not None and ceiling is not None and claimed > ceiling:
        issues.append(
            _issue(
                "maturity_claim_exceeds_derived",
                "/maturity",
                f"claimed maturity {data.get('maturity')} exceeds derived ceiling {derived}",
            )
        )
        issues = sorted(issues, key=_issue_sort_key)
        facts = _compute_facts(data, issues, indexes, owner_resolution, repo_root=root, now=evaluated_at)
    return RepositoryEvidenceContract(data=dict(data), issues=tuple(issues), facts=facts, derived_maturity=derived)


def check_repository_evidence(
    path: str | Path,
    *,
    repo_root: str | Path = ".",
    now: datetime | None = None,
) -> RepositoryEvidenceContract:
    data = load_repository_evidence(path)
    return validate_repository_evidence(data, repo_root=repo_root, now=now)


def _string_schema(*, nullable: bool = False) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string", "minLength": 1}
    if nullable:
        schema = {"type": ["string", "null"], "minLength": 1}
    return schema


def _array_schema(items: Mapping[str, Any] | None = None, *, min_items: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "array", "items": dict(items or {"type": "string"})}
    if min_items is not None:
        schema["minItems"] = min_items
    return schema


def _number_schema() -> dict[str, Any]:
    return {"type": "number"}


def _integer_schema() -> dict[str, Any]:
    return {"type": "integer"}


def _scope_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "selectors": _array_schema(),
            "paths": _array_schema(),
            "tests": _array_schema(),
            "dimensions": _array_schema({"enum": list(DIMENSIONS)}),
            "obligation": _string_schema(),
            "include_selectors": _array_schema(),
            "exclusion_ids": _array_schema(),
            "generated_source_ids": _array_schema(),
            "scope_fingerprint": _string_schema(),
        },
    }


def _waiver_scope_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["include_selectors", "obligation", "scope_fingerprint"],
        "properties": {
            "obligation": {"const": "positive_effectiveness"},
            "include_selectors": {"type": "array", "minItems": 1, "items": _string_schema()},
            "scope_fingerprint": _string_schema(),
        },
    }


def _tool_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": _string_schema(),
            "version": _string_schema(nullable=True),
            "adapter_id": _string_schema(nullable=True),
            "tool_name": _string_schema(nullable=True),
            "tool_version": _string_schema(nullable=True),
            "command_fingerprint": _string_schema(nullable=True),
            "config_digest": _string_schema(nullable=True),
            "runner_fingerprint": _string_schema(nullable=True),
        },
    }


SchemaFactory = Callable[[], dict[str, Any]]


def _ref_schema(def_name: str) -> dict[str, Any]:
    return {"$ref": f"#/$defs/{def_name}"}


def _nullable_ref_schema(def_name: str) -> dict[str, Any]:
    return {"oneOf": [_ref_schema(def_name), {"type": "null"}]}


def _nullable_string_schema() -> dict[str, Any]:
    return _string_schema(nullable=True)


def _string_array_schema(*, min_items: int | None = None) -> dict[str, Any]:
    return _array_schema(_string_schema(), min_items=min_items)


def _nullable_authority_schema() -> dict[str, Any]:
    return _nullable_ref_schema("AuthorityDecision")


def _authority_schema() -> dict[str, Any]:
    return _ref_schema("AuthorityDecision")


def _artifact_tool_name_schema() -> dict[str, Any]:
    return _string_schema()


def _tool_identity_schema() -> dict[str, Any]:
    return _tool_schema()


def _typed_timestamp_event_schema(*, include_action: bool) -> dict[str, Any]:
    timestamp_required = ["kind", "value"]
    timestamp_properties: dict[str, Any] = {
        "kind": {"const": "timestamp"},
        "value": _string_schema(),
    }
    event_required = ["kind", "pending_evidence_id", "value"]
    event_properties: dict[str, Any] = {
        "kind": {"const": "event"},
        "value": _string_schema(),
        "pending_evidence_id": _string_schema(),
    }
    if include_action:
        timestamp_required.append("action")
        event_required.append("action")
        timestamp_properties["action"] = {"enum": list(EXPIRY_ACTIONS)}
        event_properties["action"] = {"enum": list(EXPIRY_ACTIONS)}
    return {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": timestamp_required,
                "properties": timestamp_properties,
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": event_required,
                "properties": event_properties,
            },
        ]
    }


def _typed_expiry_schema() -> dict[str, Any]:
    return _typed_timestamp_event_schema(include_action=True)


def _typed_trigger_schema() -> dict[str, Any]:
    return _typed_timestamp_event_schema(include_action=False)


def _retention_end_schema() -> dict[str, Any]:
    return _typed_timestamp_event_schema(include_action=False)


def _quantitative_comparability_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "baseline_measurement_id",
            "changed_fields",
            "checked_fields",
            "current_measurement_id",
            "equivalence_artifact_id",
            "equivalence_decision",
            "reason",
            "reset_artifact_id",
            "reset_authority_decision",
            "verdict",
        ],
        "properties": {
            "verdict": {"enum": ["comparable", "reset_required", "refused"]},
            "baseline_measurement_id": {"type": ["string", "null"]},
            "current_measurement_id": {"type": ["string", "null"]},
            "reason": _string_schema(),
            "checked_fields": _array_schema({"enum": list(COMPARABILITY_CHECKED_FIELDS)}),
            "changed_fields": _array_schema({"enum": list(COMPARABILITY_CHECKED_FIELDS)}),
            "equivalence_decision": _nullable_ref_schema("AuthorityDecision"),
            "equivalence_artifact_id": {"type": ["string", "null"]},
            "reset_authority_decision": _nullable_ref_schema("AuthorityDecision"),
            "reset_artifact_id": {"type": ["string", "null"]},
        },
        "allOf": [
            {
                "if": {
                    "properties": {"changed_fields": {"minItems": 1}, "verdict": {"const": "comparable"}},
                    "required": ["changed_fields", "verdict"],
                },
                "then": {
                    "properties": {
                        "equivalence_artifact_id": _string_schema(),
                        "equivalence_decision": _ref_schema("AuthorityDecision"),
                    },
                    "required": ["equivalence_artifact_id", "equivalence_decision"],
                },
            },
            {
                "if": {"properties": {"verdict": {"const": "reset_required"}}},
                "then": {
                    "properties": {
                        "reset_artifact_id": _string_schema(),
                        "reset_authority_decision": _ref_schema("AuthorityDecision"),
                    },
                    "required": ["reset_artifact_id", "reset_authority_decision"],
                },
            },
        ],
    }


def _efficiency_comparability_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["reason", "verdict"],
        "properties": {
            "verdict": {"enum": ["comparable", "reset_required", "refused"]},
            "reason": _string_schema(),
        },
    }


def _authority_scope_schema() -> dict[str, Any]:
    return {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "repository"],
                "properties": {
                    "kind": {"const": "repository"},
                    "repository": _string_schema(),
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["dimension", "kind", "repository", "revision", "selectors"],
                "properties": {
                    "kind": {"const": "dimension"},
                    "repository": _string_schema(),
                    "revision": _string_schema(),
                    "dimension": {"enum": list(DIMENSIONS)},
                    "selectors": _array_schema(),
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["boundary_kind", "kind", "repository", "revision", "selectors"],
                "properties": {
                    "kind": {"const": "boundary"},
                    "repository": _string_schema(),
                    "revision": _string_schema(),
                    "boundary_kind": {"enum": list(BOUNDARY_KINDS)},
                    "selectors": _array_schema(),
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["dimension", "kind", "measurement_id", "repository", "revision"],
                "properties": {
                    "kind": {"const": "measurement"},
                    "repository": _string_schema(),
                    "revision": _string_schema(),
                    "measurement_id": _string_schema(),
                    "dimension": {"enum": list(DIMENSIONS)},
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "obligation", "record_id", "repository", "revision"],
                "properties": {
                    "kind": {"const": "obligation"},
                    "repository": _string_schema(),
                    "revision": _string_schema(),
                    "obligation": _string_schema(),
                    "record_id": _string_schema(),
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["governed_scope", "kind", "mechanism", "repository", "revision"],
                "properties": {
                    "kind": {"const": "enforcement"},
                    "repository": _string_schema(),
                    "revision": _string_schema(),
                    "governed_scope": _array_schema(),
                    "mechanism": _string_schema(),
                },
            },
        ]
    }


def _authority_decision_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["artifact_id", "decided_at", "decision_id", "decision_ref", "role", "scope"],
        "properties": {
            "decision_id": _string_schema(),
            "role": _string_schema(),
            "decision_ref": _string_schema(),
            "decided_at": _string_schema(),
            "artifact_id": _string_schema(),
            "scope": _authority_scope_schema(),
        },
    }


def _terminal_dispositions_schema() -> dict[str, Any]:
    return _array_schema({"type": ["object", "string"]})


def _effectiveness_payload_schema() -> dict[str, Any]:
    return {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["threat", "mutation", "observed_red", "restored_green", "assertion_effect"],
                "properties": {field: _string_schema() for field in ("threat", "mutation", "observed_red", "restored_green", "assertion_effect")},
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["fault_identity", "pre_fix_failure_artifact_id", "fixed_pass_artifact_id", "fault_specific_assertion"],
                "properties": {field: _string_schema() for field in ("fault_identity", "pre_fix_failure_artifact_id", "fixed_pass_artifact_id", "fault_specific_assertion")},
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "producer",
                    "transport",
                    "consumer",
                    "real_components",
                    "simulated_components",
                    "reached_preconditions",
                    "asserted_delivered_effect",
                    "boundary_record_id",
                ],
                "properties": {
                    "producer": _string_schema(),
                    "transport": _string_schema(),
                    "consumer": _string_schema(),
                    "real_components": _array_schema(),
                    "simulated_components": _array_schema(),
                    "reached_preconditions": _array_schema(),
                    "asserted_delivered_effect": _string_schema(),
                    "boundary_record_id": _string_schema(),
                },
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["mutation_record_id", "operator_scope", "survived_mutant_review", "limitations"],
                "properties": {field: _string_schema() for field in ("mutation_record_id", "operator_scope", "survived_mutant_review", "limitations")},
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["quarantine_id", "reproduction_artifact_id", "restoration_attempt", "disposition"],
                "properties": {field: _string_schema() for field in ("quarantine_id", "reproduction_artifact_id", "restoration_attempt", "disposition")},
            },
        ]
    }


def _enforcement_verification_payload_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "command_fingerprint",
            "escape_behavior",
            "expected_failure",
            "failure_review_decision",
            "fleet_decision",
            "governed_scope",
            "mechanism",
            "observed_failure_artifact_id",
            "observed_failure_digest",
            "owner_decision",
            "rollback_or_waiver_path",
            "restored_green_artifact_id",
            "target",
            "verified_at",
            "workflow_revision",
        ],
        "properties": {
            "target": {"const": "enforcing"},
            "mechanism": _string_schema(),
            "command_fingerprint": _string_schema(),
            "workflow_revision": _string_schema(),
            "governed_scope": _array_schema(),
            "expected_failure": _string_schema(),
            "observed_failure_artifact_id": _string_schema(),
            "observed_failure_digest": _string_schema(),
            "restored_green_artifact_id": _string_schema(),
            "escape_behavior": _string_schema(),
            "rollback_or_waiver_path": _string_schema(),
            "verified_at": _string_schema(),
            "owner_decision": _ref_schema("AuthorityDecision"),
            "fleet_decision": _ref_schema("AuthorityDecision"),
            "failure_review_decision": _ref_schema("AuthorityDecision"),
        },
    }


def _field_schema_factories() -> dict[str, dict[str, SchemaFactory]]:
    s = _string_schema
    ns = _nullable_string_schema
    sa = _string_array_schema
    i = _integer_schema
    n = _number_schema
    auth_null = _nullable_authority_schema
    auth = _authority_schema
    return {
        "dimension_dispositions": {
            "disposition_id": s, "repository": s, "revision": s, "dimension": lambda: {"enum": list(DIMENSIONS)},
            "status": lambda: {"enum": ["measured", "deferred", "unsupported", "excluded", "not_applicable"]},
            "authority": s, "reason": s, "scope": _scope_schema, "effective_revision": s,
            "measurement_ids": sa, "changed_code_record_id": ns, "mutation_record_id": ns,
            "flake_observation_ids": sa, "deferral_id": ns, "exclusion_id": ns,
            "applicability_evidence_ids": sa, "steward_approval": auth_null,
            "applicability_decision": auth_null, "approved_disposition_decision": auth_null,
            "re_evaluate_trigger": s,
        },
        "quantitative_measurements": {
            "measurement_id": s, "repository": s, "revision": s, "collected_at": s,
            "dimension": lambda: {"enum": list(DIMENSIONS)}, "unit": s, "collector": lambda: _ref_schema("ToolIdentity"),
            "scope": _scope_schema, "numerator": i, "denominator": i, "value": n,
            "baseline": lambda: _ref_schema("MeasurementPoint"), "current": lambda: _ref_schema("MeasurementPoint"),
            "threshold": lambda: _ref_schema("Threshold"), "ratchet": lambda: _ref_schema("Ratchet"),
            "comparability": lambda: _ref_schema("QuantitativeComparability"),
            "changed_code_record_id": ns, "mutation_record_id": ns, "flake_observation_ids": sa,
            "artifact_id": s,
        },
        "changed_code_records": {
            "changed_code_record_id": s, "repository": s, "revision": s, "base_ref": s,
            "base_revision": s, "head_ref": s, "head_revision": s, "merge_base_revision": s,
            "diff_tool": lambda: _ref_schema("ToolIdentity"), "diff_artifact_id": s, "eligible_units": sa,
            "excluded_units": sa, "exclusion_ids": sa, "covered_eligible_units": sa,
            "denominator_rule": s,
        },
        "mutation_records": {
            "mutation_record_id": s, "repository": s, "revision": s, "tool": lambda: _ref_schema("ToolIdentity"),
            "operator_scope": s, "operator_set_digest": s, "seed": s, "test_selection": s,
            "timeout_policy": s, "artifact_id": s, "counts": lambda: _ref_schema("MutationCounts"),
            "terminal_dispositions": _terminal_dispositions_schema,
        },
        "flake_observations": {
            "flake_observation_id": s, "repository": s, "revision": s, "environment_fingerprint": s,
            "runner_fingerprint": s, "test_set_fingerprint": s, "observation_window": s,
            "run_count": i, "pass_count": i, "fail_count": i, "sequence_artifact_id": s,
            "failure_signatures": sa, "quarantine_id": ns, "retry_policy_id": ns,
            "numerator": i, "denominator": i, "rate": n, "artifact_id": s,
        },
        "boundary_records": {
            "boundary_record_id": s, "repository": s, "revision": s,
            "boundary_kind": lambda: {"enum": list(BOUNDARY_KINDS)}, "producer": s,
            "transport": s, "consumer": s,
            "status": lambda: {"enum": ["exercised", "simulated", "not_exercised", "not_applicable"]},
            "tests": sa, "preconditions": sa, "asserted_effect": s, "artifact_ids": sa,
            "owner": s, "deferral_id": ns, "applicability_evidence_ids": sa,
            "steward_approval": auth_null, "applicability_decision": auth_null,
            "approved_disposition_decision": auth_null,
        },
        "retry_policy_records": {
            "retry_policy_id": s, "repository": s, "revision": s, "command_fingerprint": s,
            "test_set_fingerprint": s, "policy_source": s, "max_attempts": i,
            "effect_on_evidence": s, "linked_flake_ids": sa,
        },
        "deploy_tolerance_records": {
            "deploy_tolerance_id": s, "repository": s, "revision": s, "workflow": s,
            "step": s, "command_identity": s, "approver": s, "started_at": s,
            "expiry": lambda: _ref_schema("TypedExpiry"), "trigger": lambda: _ref_schema("TypedTrigger"),
            "restoration_plan": s, "effect_on_evidence": s, "evidence_artifact_id": s,
        },
        "test_surface_gaps": {
            "test_surface_gap_id": s, "repository": s, "revision": s,
            "affected_scope": _scope_schema, "owner": s, "evidence_ids": sa,
            "downstream_ticket": s, "started_at": s, "review_trigger": s, "target_state": s,
            "compensating_evidence_ids": sa, "deferral_id": ns,
        },
        "effectiveness_receipts": {
            "effectiveness_receipt_id": s, "repository": s, "revision": s,
            "kind": lambda: {"enum": ["behavioral_falsification", "historical_fault_regression", "real_boundary", "mutation", "quarantine_review"]},
            "reviewed_at": s, "reviewer": s, "scope": _scope_schema, "limitations": s,
            "payload": lambda: _ref_schema("EffectivenessPayload"), "review_decision": auth_null,
            "independent_review_decision": auth_null,
        },
        "efficiency_records": {
            "efficiency_id": s, "repository": s, "revision": s, "environment_fingerprint": s,
            "command_fingerprint": s, "test_set_fingerprint": s, "observation_window": s,
            "sample_count": i, "costs": lambda: _ref_schema("CostClassMap"), "cadence": s,
            "comparison_baseline_id": ns, "comparability": lambda: _ref_schema("EfficiencyComparability"),
            "artifact_id": s, "limitations": s,
        },
        "parity_records": {
            "parity_id": s, "repository": s, "revision": s,
            "verdict": lambda: {"enum": ["exact", "evidenced_equivalent", "divergent_with_waiver", "local_only", "ci_only"]},
            "local_command": s, "ci_command": ns, "local_environment": s, "ci_environment": ns,
            "test_set_fingerprint": s, "workflow_revision": ns, "artifact_ids": sa,
            "differences": sa, "equivalence_evidence_id": ns, "owner": s, "waiver_id": ns,
            "restoration_plan": s,
        },
        "retention_records": {
            "retention_id": s, "repository": s, "revision": s, "artifact_id": s,
            "artifact_class": lambda: {"enum": ["proof", "dry_run_validator", "enforcement_verification", "measurement", "decision"]},
            "created_at": s, "storage_locator": lambda: _ref_schema("StorageLocator"),
            "digest_algorithm": s, "digest": s, "availability": lambda: _ref_schema("RetentionAvailability"),
            "retrieval_receipts": lambda: _array_schema(_ref_schema("RetrievalReceipt")),
            "retention_end": lambda: _ref_schema("RetentionEnd"), "expiry_action": s,
            "tombstone_artifact_id": ns, "owner": s, "superseded_by": ns,
        },
        "generated_source_records": {
            "generated_source_id": s, "repository": s, "revision": s, "path_selectors": sa,
            "source_of_truth": s, "generator": ns, "generated_revision": ns,
            "generated_digest": ns, "risk_acceptor": s,
            "affected_dimensions": lambda: _array_schema({"enum": list(DIMENSIONS)}),
            "treatment": lambda: {"enum": ["included", "excluded", "separate"]},
            "reason": s, "exclusion_id": ns, "re_evaluate_trigger": s,
        },
        "exclusion_records": {
            "exclusion_id": s, "repository": s, "revision": s, "owner": ns, "reviewer": ns,
            "approver": ns, "reason": s, "scope": _scope_schema, "started_at": s,
            "expiry": lambda: _ref_schema("TypedExpiry"), "restoration_condition": s,
            "restoration_plan": s, "compensating_evidence_ids": sa, "decision_ref": s,
            "active_state": lambda: _ref_schema("ActiveState"), "expiry_action": s,
            "authority_decision": auth_null, "affected_measurement_ids": sa,
        },
        "waiver_records": {
            "waiver_id": s, "repository": s, "revision": s, "owner": ns, "reviewer": ns,
            "approver": ns, "reason": s, "scope": lambda: _ref_schema("WaiverScope"),
            "started_at": s, "expiry": lambda: _ref_schema("TypedExpiry"),
            "restoration_condition": s, "restoration_plan": s, "compensating_evidence_ids": sa,
            "decision_ref": s, "active_state": lambda: _ref_schema("ActiveState"),
            "expiry_action": s, "authority_decision": auth,
            "independent_review_decision": auth, "obligation": s, "fallback": s,
        },
        "maturity_deferrals": {
            "maturity_deferral_id": s, "repository": s, "revision": s, "owner": ns,
            "reviewer": ns, "approver": ns, "reason": s, "scope": _scope_schema,
            "started_at": s, "expiry": lambda: _ref_schema("TypedExpiry"),
            "restoration_condition": s, "restoration_plan": s, "compensating_evidence_ids": sa,
            "decision_ref": s, "active_state": lambda: _ref_schema("ActiveState"),
            "expiry_action": s, "authority_decision": auth_null, "current_stage": s,
            "target_stage": s, "blocked_prerequisite": s, "unsupported_dimensions": sa,
            "downstream_ticket": s, "next_action": s, "review_trigger": s,
        },
        "quarantine_records": {
            "quarantine_id": s, "repository": s, "revision": s, "owner": ns,
            "reviewer": ns, "approver": ns, "reason": s, "scope": _scope_schema,
            "started_at": s, "expiry": lambda: _ref_schema("TypedExpiry"),
            "restoration_condition": s, "restoration_plan": s, "compensating_evidence_ids": sa,
            "decision_ref": s, "active_state": lambda: _ref_schema("ActiveState"),
            "expiry_action": s, "authority_decision": auth_null, "classification": s,
            "tests": sa, "evidence_ids": sa, "flake_observation_id": ns,
            "retry_policy_id": ns, "coverage_impact": s, "boundary_impact": s,
        },
        "artifact_records": {
            "artifact_id": s, "repository": s, "revision": s,
            "artifact_class": lambda: {"enum": ["proof", "dry_run_validator", "enforcement_verification", "measurement", "decision"]},
            "created_at": s, "producer": s, "tool": lambda: _ref_schema("ArtifactToolName"),
            "config_identity": s, "storage_locator": lambda: _ref_schema("StorageLocator"),
            "media_type": s, "digest_algorithm": s, "digest": s, "size": i,
            "sensitivity": s, "access": s, "tombstone": ns,
            "payload": lambda: _nullable_ref_schema("EnforcementVerificationPayload"),
        },
    }


def _assert_schema_field_ownership_complete(factories: Mapping[str, Mapping[str, SchemaFactory]] | None = None) -> None:
    owned = _field_schema_factories() if factories is None else factories
    if set(owned) != set(RECORD_ALLOWED_FIELDS):
        raise RepositoryEvidenceError("schema field ownership collections do not match RECORD_ALLOWED_FIELDS")
    for collection, allowed_fields in RECORD_ALLOWED_FIELDS.items():
        if set(owned[collection]) != set(allowed_fields):
            raise RepositoryEvidenceError(f"schema field ownership mismatch for {collection}")


def _field_schema(collection: str, field: str) -> dict[str, Any]:
    _assert_schema_field_ownership_complete()
    return copy.deepcopy(_field_schema_factories()[collection][field]())


def _record_schema(collection: str) -> dict[str, Any]:
    _assert_schema_field_ownership_complete()
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(RECORD_ALLOWED_FIELDS[collection]),
        "properties": {field: _field_schema(collection, field) for field in sorted(RECORD_ALLOWED_FIELDS[collection])},
    }
    if collection == "parity_records":
        schema["allOf"] = [
            {
                "if": {"properties": {"verdict": {"const": "exact"}}},
                "then": {
                    "properties": {
                        "ci_command": _string_schema(),
                        "ci_environment": _string_schema(),
                        "workflow_revision": _string_schema(),
                    },
                    "required": ["ci_command", "ci_environment", "workflow_revision"],
                },
            },
            {
                "if": {"properties": {"verdict": {"const": "evidenced_equivalent"}}},
                "then": {
                    "properties": {
                        "ci_command": _string_schema(),
                        "ci_environment": _string_schema(),
                        "equivalence_evidence_id": _string_schema(),
                        "workflow_revision": _string_schema(),
                    },
                    "required": ["ci_command", "ci_environment", "equivalence_evidence_id", "workflow_revision"],
                },
            },
            {
                "if": {"properties": {"verdict": {"const": "divergent_with_waiver"}}},
                "then": {
                    "properties": {
                        "ci_command": _string_schema(),
                        "ci_environment": _string_schema(),
                        "waiver_id": _string_schema(),
                        "workflow_revision": _string_schema(),
                    },
                    "required": ["ci_command", "ci_environment", "waiver_id", "workflow_revision"],
                },
            },
        ]
    if collection == "generated_source_records":
        schema["oneOf"] = [
            {
                "properties": {
                    "generated_digest": {"type": "null"},
                    "generated_revision": {"type": "null"},
                    "generator": {"type": "null"},
                    "path_selectors": {"const": []},
                }
            },
            {
                "properties": {
                    "generated_digest": _string_schema(),
                    "generated_revision": _string_schema(),
                    "generator": _string_schema(),
                    "path_selectors": _array_schema(_string_schema(), min_items=1),
                }
            },
        ]
    return schema


def generate_repository_evidence_schema() -> Mapping[str, Any]:
    authority_decision = {
        "type": "object",
        "additionalProperties": False,
        "required": ["artifact_id", "decided_at", "decision_id", "decision_ref", "role", "scope"],
        "properties": {
            "decision_id": _string_schema(),
            "role": _string_schema(),
            "decision_ref": _string_schema(),
            "decided_at": _string_schema(),
            "artifact_id": _string_schema(),
            "scope": {
                "oneOf": [
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["kind", "repository"],
                        "properties": {
                            "kind": {"const": "repository"},
                            "repository": _string_schema(),
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["dimension", "kind", "repository", "revision", "selectors"],
                        "properties": {
                            "kind": {"const": "dimension"},
                            "repository": _string_schema(),
                            "revision": _string_schema(),
                            "dimension": {"enum": list(DIMENSIONS)},
                            "selectors": _array_schema(),
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["boundary_kind", "kind", "repository", "revision", "selectors"],
                        "properties": {
                            "kind": {"const": "boundary"},
                            "repository": _string_schema(),
                            "revision": _string_schema(),
                            "boundary_kind": {"enum": list(BOUNDARY_KINDS)},
                            "selectors": _array_schema(),
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["dimension", "kind", "measurement_id", "repository", "revision"],
                        "properties": {
                            "kind": {"const": "measurement"},
                            "repository": _string_schema(),
                            "revision": _string_schema(),
                            "measurement_id": _string_schema(),
                            "dimension": {"enum": list(DIMENSIONS)},
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["kind", "obligation", "record_id", "repository", "revision"],
                        "properties": {
                            "kind": {"const": "obligation"},
                            "repository": _string_schema(),
                            "revision": _string_schema(),
                            "obligation": _string_schema(),
                            "record_id": _string_schema(),
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["governed_scope", "kind", "mechanism", "repository", "revision"],
                        "properties": {
                            "kind": {"const": "enforcement"},
                            "repository": _string_schema(),
                            "revision": _string_schema(),
                            "governed_scope": _array_schema(),
                            "mechanism": _string_schema(),
                        },
                    },
                ]
            },
        },
    }
    defs: dict[str, Any] = {
        "ActiveState": {
            "type": "object",
            "additionalProperties": False,
            "required": ["status"],
            "properties": {
                "status": {"enum": list(ACTIVE_STATES)},
                "evaluated_at": _string_schema(nullable=True),
                "evidence_id": _string_schema(nullable=True),
            },
        },
        "AuthorityDecision": authority_decision,
        "Comparability": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "baseline_measurement_id",
                "changed_fields",
                "checked_fields",
                "current_measurement_id",
                "equivalence_artifact_id",
                "equivalence_decision",
                "reason",
                "reset_artifact_id",
                "reset_authority_decision",
                "verdict",
            ],
            "properties": {
                "verdict": {"enum": ["comparable", "reset_required", "refused"]},
                "baseline_measurement_id": {"type": ["string", "null"]},
                "current_measurement_id": {"type": ["string", "null"]},
                "reason": _string_schema(),
                "checked_fields": _array_schema({"enum": list(COMPARABILITY_CHECKED_FIELDS)}),
                "changed_fields": _array_schema({"enum": list(COMPARABILITY_CHECKED_FIELDS)}),
                "equivalence_decision": {"oneOf": [{"$ref": "#/$defs/AuthorityDecision"}, {"type": "null"}]},
                "equivalence_artifact_id": {"type": ["string", "null"]},
                "reset_authority_decision": {"oneOf": [{"$ref": "#/$defs/AuthorityDecision"}, {"type": "null"}]},
                "reset_artifact_id": {"type": ["string", "null"]},
            },
            "allOf": [
                {
                    "if": {
                        "properties": {"changed_fields": {"minItems": 1}, "verdict": {"const": "comparable"}},
                        "required": ["changed_fields", "verdict"],
                    },
                    "then": {
                        "properties": {
                            "equivalence_artifact_id": _string_schema(),
                            "equivalence_decision": {"$ref": "#/$defs/AuthorityDecision"},
                        },
                        "required": ["equivalence_artifact_id", "equivalence_decision"],
                    },
                },
                {
                    "if": {"properties": {"verdict": {"const": "reset_required"}}},
                    "then": {
                        "properties": {
                            "reset_artifact_id": _string_schema(),
                            "reset_authority_decision": {"$ref": "#/$defs/AuthorityDecision"},
                        },
                        "required": ["reset_artifact_id", "reset_authority_decision"],
                    },
                }
            ],
        },
        "CostClass": {
            "type": "object",
            "additionalProperties": False,
            "required": ["amount", "reason", "state"],
            "properties": {
                "state": {"enum": ["measured", "unavailable", "not_applicable"]},
                "reason": _string_schema(),
                "amount": {"type": ["number", "null"]},
                "unit": {"type": ["string", "null"]},
                "artifact_id": {"type": ["string", "null"]},
                "authority_decision": {"oneOf": [{"$ref": "#/$defs/AuthorityDecision"}, {"type": "null"}]},
            },
        },
        "CostClassMap": {
            "type": "object",
            "additionalProperties": False,
            "required": list(COST_CLASSES),
            "properties": {cost_class: {"$ref": "#/$defs/CostClass"} for cost_class in COST_CLASSES},
        },
        "EffectivenessPayload": {
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["threat", "mutation", "observed_red", "restored_green", "assertion_effect"],
                    "properties": {field: _string_schema() for field in ("threat", "mutation", "observed_red", "restored_green", "assertion_effect")},
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["fault_identity", "pre_fix_failure_artifact_id", "fixed_pass_artifact_id", "fault_specific_assertion"],
                    "properties": {field: _string_schema() for field in ("fault_identity", "pre_fix_failure_artifact_id", "fixed_pass_artifact_id", "fault_specific_assertion")},
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "producer",
                        "transport",
                        "consumer",
                        "real_components",
                        "simulated_components",
                        "reached_preconditions",
                        "asserted_delivered_effect",
                        "boundary_record_id",
                    ],
                    "properties": {
                        "producer": _string_schema(),
                        "transport": _string_schema(),
                        "consumer": _string_schema(),
                        "real_components": _array_schema(),
                        "simulated_components": _array_schema(),
                        "reached_preconditions": _array_schema(),
                        "asserted_delivered_effect": _string_schema(),
                        "boundary_record_id": _string_schema(),
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["mutation_record_id", "operator_scope", "survived_mutant_review", "limitations"],
                    "properties": {field: _string_schema() for field in ("mutation_record_id", "operator_scope", "survived_mutant_review", "limitations")},
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["quarantine_id", "reproduction_artifact_id", "restoration_attempt", "disposition"],
                    "properties": {field: _string_schema() for field in ("quarantine_id", "reproduction_artifact_id", "restoration_attempt", "disposition")},
                },
            ]
        },
        "EnforcementVerificationPayload": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "command_fingerprint",
                "escape_behavior",
                "expected_failure",
                "failure_review_decision",
                "fleet_decision",
                "governed_scope",
                "mechanism",
                "observed_failure_artifact_id",
                "observed_failure_digest",
                "owner_decision",
                "rollback_or_waiver_path",
                "restored_green_artifact_id",
                "target",
                "verified_at",
                "workflow_revision",
            ],
            "properties": {
                "target": {"const": "enforcing"},
                "mechanism": _string_schema(),
                "command_fingerprint": _string_schema(),
                "workflow_revision": _string_schema(),
                "governed_scope": _array_schema(),
                "expected_failure": _string_schema(),
                "observed_failure_artifact_id": _string_schema(),
                "observed_failure_digest": _string_schema(),
                "restored_green_artifact_id": _string_schema(),
                "escape_behavior": _string_schema(),
                "rollback_or_waiver_path": _string_schema(),
                "verified_at": _string_schema(),
                "owner_decision": {"$ref": "#/$defs/AuthorityDecision"},
                "fleet_decision": {"$ref": "#/$defs/AuthorityDecision"},
                "failure_review_decision": {"$ref": "#/$defs/AuthorityDecision"},
            },
        },
        "MeasurementPoint": {
            "type": "object",
            "additionalProperties": False,
            "required": ["denominator", "measurement_id", "numerator", "value"],
            "properties": {
                "measurement_id": _string_schema(),
                "numerator": _integer_schema(),
                "denominator": _integer_schema(),
                "value": _number_schema(),
            },
        },
        "MutationCounts": {
            "type": "object",
            "additionalProperties": False,
            "required": ["error", "equivalent", "excluded", "generated", "invalid", "killed", "no_coverage", "survived", "timeout"],
            "properties": {field: _integer_schema() for field in ("error", "equivalent", "excluded", "generated", "invalid", "killed", "no_coverage", "survived", "timeout")},
        },
        "Ratchet": {
            "type": "object",
            "additionalProperties": False,
            "required": ["direction", "state", "tolerance"],
            "properties": {
                "state": {"enum": ["unset", "proposed", "adopted"]},
                "direction": {"enum": list(RATCHET_DIRECTIONS)},
                "proposed": {"type": ["number", "null"]},
                "adopted": {"type": ["number", "null"]},
                "tolerance": {"type": "number"},
                "proposal_decision": {"oneOf": [{"$ref": "#/$defs/AuthorityDecision"}, {"type": "null"}]},
                "adoption_decision": {"oneOf": [{"$ref": "#/$defs/AuthorityDecision"}, {"type": "null"}]},
            },
            "allOf": [
                {
                    "if": {"properties": {"state": {"const": "proposed"}}},
                    "then": {"properties": {"proposal_decision": {"$ref": "#/$defs/AuthorityDecision"}}, "required": ["proposal_decision", "proposed"]},
                },
                {
                    "if": {"properties": {"state": {"const": "adopted"}}},
                    "then": {
                        "properties": {
                            "adoption_decision": {"$ref": "#/$defs/AuthorityDecision"},
                            "proposal_decision": {"$ref": "#/$defs/AuthorityDecision"},
                        },
                        "required": ["adopted", "adoption_decision", "proposal_decision"],
                    },
                },
            ],
        },
        "RetentionAvailability": {
            "type": "object",
            "additionalProperties": False,
            "required": ["retrieval_receipt_id", "state", "verified_at"],
            "properties": {
                "state": {"enum": list(RETENTION_AVAILABILITY_STATES)},
                "verified_at": _string_schema(nullable=True),
                "retrieval_receipt_id": _string_schema(nullable=True),
            },
        },
        "RetrievalReceipt": {
            "type": "object",
            "additionalProperties": False,
            "required": ["artifact_id", "observed_digest", "observed_digest_algorithm", "observed_locator", "observed_size", "receipt_id", "retrieved_at"],
            "properties": {
                "receipt_id": _string_schema(),
                "artifact_id": _string_schema(),
                "retrieved_at": _string_schema(),
                "observed_locator": _string_schema(),
                "observed_digest_algorithm": {"const": "sha256"},
                "observed_digest": _string_schema(),
                "observed_size": _integer_schema(),
            },
        },
        "StorageLocator": {
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "value"],
                    "properties": {"kind": {"const": "inline"}, "value": _string_schema()},
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "value"],
                    "properties": {"kind": {"const": "repo_path"}, "value": _string_schema()},
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "value"],
                    "properties": {"kind": {"const": "external"}, "value": _string_schema()},
                },
            ]
        },
        "Threshold": {
            "type": "object",
            "additionalProperties": False,
            "required": ["comparator", "state", "tolerance", "value"],
            "properties": {
                "state": {"enum": ["unset", "proposed", "advisory", "enforcing"]},
                "comparator": {"enum": list(THRESHOLD_COMPARATORS)},
                "proposal_decision": {"oneOf": [{"$ref": "#/$defs/AuthorityDecision"}, {"type": "null"}]},
                "approval_decision": {"oneOf": [{"$ref": "#/$defs/AuthorityDecision"}, {"type": "null"}]},
                "value": {"type": ["number", "null"]},
                "tolerance": {"type": "number"},
            },
            "allOf": [
                {
                    "if": {"properties": {"state": {"enum": ["proposed", "advisory", "enforcing"]}}},
                    "then": {"properties": {"proposal_decision": {"$ref": "#/$defs/AuthorityDecision"}}, "required": ["proposal_decision"]},
                },
                {
                    "if": {"properties": {"state": {"const": "enforcing"}}},
                    "then": {
                        "properties": {
                            "approval_decision": {"$ref": "#/$defs/AuthorityDecision"},
                            "proposal_decision": {"$ref": "#/$defs/AuthorityDecision"},
                        },
                        "required": ["approval_decision", "proposal_decision"],
                    },
                },
            ],
        },
        "TypedExpiry": {
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["action", "kind", "value"],
                    "properties": {
                        "kind": {"const": "timestamp"},
                        "value": _string_schema(),
                        "action": {"enum": list(EXPIRY_ACTIONS)},
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["action", "kind", "pending_evidence_id", "value"],
                    "properties": {
                        "kind": {"const": "event"},
                        "value": _string_schema(),
                        "pending_evidence_id": _string_schema(),
                        "action": {"enum": list(EXPIRY_ACTIONS)},
                    },
                },
            ]
        },
        "WaiverScope": _waiver_scope_schema(),
    }
    defs.update(
        {
            "ArtifactToolName": _artifact_tool_name_schema(),
            "ToolIdentity": _tool_identity_schema(),
            "QuantitativeComparability": _quantitative_comparability_schema(),
            "EfficiencyComparability": _efficiency_comparability_schema(),
            "TypedTrigger": _typed_trigger_schema(),
            "RetentionEnd": _retention_end_schema(),
            "TypedExpiry": _typed_expiry_schema(),
            "Comparability": _quantitative_comparability_schema(),
        }
    )
    record_defs = {collection: _record_schema(collection) for collection in COLLECTION_ORDER}
    record_defs["ParityRecord"] = copy.deepcopy(record_defs["parity_records"])
    record_defs["GeneratedSourceRecord"] = copy.deepcopy(record_defs["generated_source_records"])
    properties: dict[str, Any] = {
        "schema_version": {"const": 1},
        "contract_id": {"type": "string", "minLength": 1},
        "repository": {"type": "string", "minLength": 1},
        "registry_revision": {"type": "string", "minLength": 1},
        "evidence_revision": {"type": "string", "minLength": 1},
        "collected_at": {"type": "string", "minLength": 1},
        "repo_class": {"enum": list(REPO_CLASSES)},
        "arc_author": {"type": "string", "minLength": 1},
        "fleet_steward_role": {"type": "string", "minLength": 1},
        "repo_evidence_owner_role": {"type": "string", "minLength": 1},
        "owner_resolution": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "eligible_rw_roles",
                "registry_revision",
                "registry_blob_id",
                "instance_blob_id",
                "requested_role",
                "resolved_role",
                "basis",
                "assignment_authority",
                "issues",
            ],
            "properties": {
                "eligible_rw_roles": {"type": "array", "items": {"type": "string"}},
                "registry_revision": {"type": "string"},
                "registry_blob_id": {"type": "string"},
                "instance_blob_id": {"type": "string"},
                "requested_role": {"type": ["string", "null"]},
                "resolved_role": {"type": ["string", "null"]},
                "basis": {"type": "string"},
                "assignment_authority": {"type": ["string", "null"]},
                "issues": {"type": "array", "items": {"type": "string"}},
                "persona": {"type": ["string", "null"]},
                "account": {"type": ["string", "null"]},
            },
        },
        "maturity": {"enum": list(MATURITY_ORDER)},
        "target_maturity": {"enum": list(MATURITY_ORDER)},
        "next_review": {"type": "string", "minLength": 1},
        "inventory": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "language_substrates",
                "runner_manifests",
                "test_commands",
                "ci_workflows",
                "applicability_evidence_ids",
            ],
            "properties": {
                "language_substrates": {"type": "array"},
                "runner_manifests": {"type": "array"},
                "test_commands": {"type": "array"},
                "ci_workflows": {"type": "array"},
                "applicability_evidence_ids": {"type": "array"},
            },
        },
    }
    for collection in COLLECTION_ORDER:
        properties[collection] = {"type": "array", "items": {"$ref": f"#/$defs/{collection}"}}
    return {
        "$id": "https://pantheon.local/schemas/repository-evidence-contract.schema.json",
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ORG-083 Repository Evidence Contract",
        "type": "object",
        "additionalProperties": False,
        "required": list(ROOT_ORDER),
        "properties": properties,
        "$defs": {**defs, **record_defs},
    }


def _parse_now(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    parsed = _parse_time(raw)
    if parsed is None:
        raise RepositoryEvidenceError("--now must be an ISO-8601 UTC timestamp")
    return parsed


def _cmd_check(args: argparse.Namespace) -> int:
    try:
        result = check_repository_evidence(args.path, repo_root=args.repo_root, now=_parse_now(args.now))
    except RepositoryEvidenceError as exc:
        print(f"PARSE repository_evidence_parse: {exc}", file=sys.stderr)
        return 2
    for issue in result.issues:
        print(issue.line(), file=sys.stderr)
    print(json.dumps({"derived_maturity": result.derived_maturity, "issue_count": len(result.issues)}, sort_keys=True))
    return 1 if result.issues else 0


def _cmd_generate_schema(_: argparse.Namespace) -> int:
    sys.stdout.buffer.write(_canonical_json_bytes(generate_repository_evidence_schema()))
    return 0


def _cmd_canonicalize(args: argparse.Namespace) -> int:
    try:
        data = load_repository_evidence(args.path)
    except RepositoryEvidenceError as exc:
        print(f"PARSE repository_evidence_parse: {exc}", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(canonical_repository_evidence_bytes(data))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("path")
    check_parser.add_argument("--repo-root", default=".")
    check_parser.add_argument("--now")
    check_parser.set_defaults(func=_cmd_check)
    schema_parser = subparsers.add_parser("generate-schema")
    schema_parser.set_defaults(func=_cmd_generate_schema)
    canonical_parser = subparsers.add_parser("canonicalize")
    canonical_parser.add_argument("path")
    canonical_parser.set_defaults(func=_cmd_canonicalize)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
