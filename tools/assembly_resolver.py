"""Strict ORG-055 assembly manifest, deterministic lock, and compatibility resolver.

A hand-authored manifest expresses a compatible possibility.  A generated lock
expresses one exact tested commitment; hand-editing the lock is invalid.  This
module parses manifests, runs the fixed eight-step compatibility resolver over
the T-3181 component catalogue, renders deterministic lock bytes, verifies a
generated lock against tampering/staleness/artifact drift, and plans (never
executes) an assembly update through injected adapter seams.

Scope (T-3182): resolver / lock / verify / update-planner logic plus the
adapter Protocol seams.  It never mutates a checkout, resolves a real remote, or
copies evidence bytes.  The logical-repo, lifecycle, and evidence seams are
injected: real implementations are T-1996 (ORG-046 ordered primary/mirror,
permanent-vs-transient self-heal, redirect-never-authority), T-2951 (checkout
lifecycle), and downstream evidence.  Fixture adapters live in the test module;
the CLI wires small static, policy-free data readers only.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import dataclasses
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Protocol

from tools.component_catalogue import (
    SCHEMA_VERSION,
    STABLE_ID_PATTERN,
    VERSION_PATTERN,
    CapabilityRequirement,
    ComponentCatalogueError,
    ComponentRecord,
    _array_schema,
    _atomic_write,
    _exact_mapping,
    _json_bytes,
    _load_yaml,
    _matching_providers,
    _nullable_string,
    _nullable_string_schema,
    _object_schema,
    _stable_id,
    _string,
    _string_tuple,
    _version,
    load_component_records,
)


GENERATOR_ID = "tools.assembly_resolver"
GENERATOR_VERSION = "1.0.0"

RESOLUTION_ORDER = (
    "parse-and-resolve-refs",
    "prove-uniqueness-and-closure",
    "match-contract-schema-version",
    "refuse-conflicts",
    "acknowledge-implications",
    "own-obligations",
    "resolve-source-identity",
    "emit-lock-and-report",
)

UPDATE_STAGES = (
    "validate-current-manifest-catalogue-lock",
    "resolve-candidate-lock",
    "lifecycle-plan",
    "stage-exact-sources",
    "run-smoke-contracts",
    "run-health-contracts",
    "publish-lock-and-receipt",
)

ROLLBACK_STAGES = (
    "restore-prior-lock",
    "restore-prior-sources",
    "rerun-prior-health",
    "retain-candidate-diagnostics",
)

MANIFEST_KEYS = (
    "schema_version",
    "assembly_id",
    "project_ref",
    "components",
    "deployment",
    "implication_acknowledgements",
    "smoke_contracts",
    "health_contracts",
    "update_policy_ref",
    "rollback_policy_ref",
)
MANIFEST_COMPONENT_KEYS = (
    "component_id",
    "contract_ranges",
    "version_ranges",
    "required_features",
    "enabled_optional_features",
)
IMPLICATION_ACK_KEYS = ("implication_id", "authority_ref", "owner_ref", "evidence_ref")
ASSEMBLY_DEPLOYMENT_KEYS = ("locus", "trust_zone")

LOCK_KEYS = (
    "schema_version",
    "assembly_id",
    "manifest_sha256",
    "catalogue_sha256",
    "generator_id",
    "generator_version",
    "resolution_order",
    "components",
    "acknowledged_implications",
    "owned_obligations",
    "smoke_evidence",
    "health_evidence",
    "prior_lock_sha256",
    "update_receipt_ref",
)
LOCKED_COMPONENT_KEYS = (
    "component_id",
    "logical_repo",
    "path",
    "artifact",
    "revision",
    "artifact_digest",
    "contract_versions",
    "schema_versions",
    "enabled_features",
    "requirement_closure",
    "compatibility_evidence",
    "submodules",
)
SUBMODULE_KEYS = ("path", "revision")
CONTRACT_VERSION_KEYS = ("contract", "version")
SCHEMA_VERSION_KEYS = ("schema", "version")

# The sha256 digest form the rest of the module emits (lock_digest, manifest and
# catalogue digests): the "sha256:" prefix followed by 64 lowercase hex chars.
_SHA256_FORM = re.compile(r"sha256:[0-9a-f]{64}")


class AssemblyResolverError(ValueError):
    """An assembly manifest, lock, resolution, or update plan cannot be accepted."""

    def __init__(self, message: str, *, refusal: "CompatibilityRefusal | None" = None):
        super().__init__(message)
        self.refusal = refusal


class RefusalKind(str, Enum):
    UNKNOWN_PROVIDER = "unknown-provider"
    AMBIGUOUS_PROVIDER = "ambiguous-provider"
    UNSATISFIED_HARD_REQUIREMENT = "unsatisfied-hard-requirement"
    CONTRACT_MISMATCH = "contract-mismatch"
    SCHEMA_MISMATCH = "schema-mismatch"
    VERSION_MISMATCH = "version-mismatch"
    FEATURE_CONFLICT = "feature-conflict"
    COMPONENT_CONFLICT = "component-conflict"
    UNACKNOWLEDGED_IMPLICATION = "unacknowledged-implication"
    UNOWNED_OBLIGATION = "unowned-obligation"
    UNRESOLVED_SOURCE = "unresolved-source"
    PROVENANCE_MISMATCH = "provenance-mismatch"
    SUBMODULE_MISMATCH = "submodule-mismatch"
    ARTIFACT_CHANGED_SAME_REVISION = "artifact-changed-same-revision"
    DIRTY_CHECKOUT = "dirty-checkout"
    DIVERGED_CHECKOUT = "diverged-checkout"
    FOREIGN_CHECKOUT = "foreign-checkout"
    MISSING_REF = "missing-ref"
    # T-2951: the checkout-lifecycle reconciler's remote-auth failure class. An
    # in-family extension to this T-3182-landed shared refusal surface so the
    # assembly plane and the repo-reconciler plane keep ONE refusal vocabulary
    # (no parallel enum). Not enumerated in any generated schema, so adding it
    # changes no schema bytes; verified by tools.test_assembly_resolver staying
    # green + the checkout-spec schema byte-compare.
    CREDENTIAL_FAILURE = "credential-failure"
    # T-3183: the repository-extraction executor's dual-writable-canonical-source
    # refusal class. Follows the CREDENTIAL_FAILURE precedent — an in-family
    # additive extension to this shared refusal surface so the extraction plane
    # reuses ONE refusal vocabulary (no parallel enum). Not enumerated in any
    # generated schema, so adding it changes no schema bytes; verified by
    # tools.test_assembly_resolver staying green + the schema byte-compare.
    DUAL_CANONICAL_SOURCE = "dual-canonical-source"
    SMOKE_FAILURE = "smoke-failure"
    HEALTH_FAILURE = "health-failure"
    ROLLBACK_FAILURE = "rollback-failure"
    LIFECYCLE_REFUSED = "lifecycle-refused"
    STALE_LOCK_INPUT = "stale-lock-input"
    HAND_EDITED_LOCK = "hand-edited-lock"


@dataclass(frozen=True)
class CompatibilityRefusal:
    kind: RefusalKind
    subject_id: str
    detail: str
    evidence_ref: str | None = None

    def as_text(self) -> str:
        return f"{self.kind.value}: {self.subject_id}: {self.detail}"


@dataclass(frozen=True)
class SubmoduleView:
    path: str
    revision: str


@dataclass(frozen=True)
class ResolvedSource:
    logical_repo: str
    resolved_source: str
    revision: str
    artifact_digest: str
    redirect_chain: tuple[str, ...] = ()
    submodules: tuple[SubmoduleView, ...] = ()


@dataclass(frozen=True)
class EvidencePresence:
    evidence_ref: str
    present: bool
    digest: str


@dataclass(frozen=True)
class LifecyclePlan:
    ok: bool
    stages: tuple[str, ...]
    refusal: CompatibilityRefusal | None = None


class LogicalRepoResolver(Protocol):
    """Resolve a logical repo id to a concrete source identity + revision + digest.

    The result object carries ``redirect_chain`` and ``submodules`` so an ORG-046
    §§1-3 implementation (ordered primary/mirror, permanent-vs-transient
    self-heal, redirect-never-authority) slots in without a signature change when
    T-1996 lands.  No remote-preference or failover policy is encoded here.
    """

    def resolve(self, logical_repo: str) -> ResolvedSource: ...


class RepositoryLifecyclePlanner(Protocol):
    """T-2951 seam: a no-mutation lifecycle plan for a candidate lock's sources."""

    def plan(self, sources: Sequence["LockedComponent"]) -> LifecyclePlan: ...


class EvidenceResolver(Protocol):
    """Resolve an evidence ref to presence + digest; never copies evidence bytes."""

    def resolve(self, evidence_ref: str) -> EvidencePresence: ...


@dataclass(frozen=True)
class AssemblyComponent:
    component_id: str
    contract_ranges: tuple[str, ...]
    version_ranges: tuple[str, ...]
    required_features: tuple[str, ...]
    enabled_optional_features: tuple[str, ...]


@dataclass(frozen=True)
class ImplicationAck:
    implication_id: str
    authority_ref: str
    owner_ref: str
    evidence_ref: str


@dataclass(frozen=True)
class AssemblyDeployment:
    locus: str
    trust_zone: str


@dataclass(frozen=True)
class AssemblyManifest:
    schema_version: int
    assembly_id: str
    project_ref: str | None
    components: tuple[AssemblyComponent, ...]
    deployment: AssemblyDeployment
    implication_acknowledgements: tuple[ImplicationAck, ...]
    smoke_contracts: tuple[str, ...]
    health_contracts: tuple[str, ...]
    update_policy_ref: str
    rollback_policy_ref: str
    source_sha256: str


@dataclass(frozen=True)
class LockedComponent:
    component_id: str
    logical_repo: str
    path: str | None
    artifact: str | None
    revision: str
    artifact_digest: str
    contract_versions: tuple[tuple[str, str], ...]
    schema_versions: tuple[tuple[str, str], ...]
    enabled_features: tuple[str, ...]
    requirement_closure: tuple[str, ...]
    compatibility_evidence: tuple[str, ...]
    submodules: tuple[SubmoduleView, ...]


@dataclass(frozen=True)
class AssemblyLock:
    schema_version: int
    assembly_id: str
    manifest_sha256: str
    catalogue_sha256: str
    generator_id: str
    generator_version: str
    resolution_order: tuple[str, ...]
    components: tuple[LockedComponent, ...]
    acknowledged_implications: tuple[str, ...]
    owned_obligations: tuple[str, ...]
    smoke_evidence: tuple[str, ...]
    health_evidence: tuple[str, ...]
    prior_lock_sha256: str | None
    update_receipt_ref: str | None


@dataclass(frozen=True)
class AssemblyPlan:
    manifest: AssemblyManifest
    catalogue_sha256: str
    locked_components: tuple[LockedComponent, ...]
    acknowledged_implications: tuple[str, ...]
    owned_obligations: tuple[str, ...]
    smoke_evidence: tuple[str, ...]
    health_evidence: tuple[str, ...]
    report: Mapping[str, object]
    refusals: tuple[CompatibilityRefusal, ...]
    prior_lock_sha256: str | None = None
    update_receipt_ref: str | None = None


@dataclass(frozen=True)
class UpdatePlan:
    prior_lock: AssemblyLock | None
    candidate_lock: AssemblyLock
    lifecycle_plan: LifecyclePlan
    stages: tuple[str, ...]
    rollback: tuple[str, ...]


def _refuse(refusal: CompatibilityRefusal) -> AssemblyResolverError:
    return AssemblyResolverError(refusal.as_text(), refusal=refusal)


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


def _parse_manifest_component(value: object, *, path: str) -> AssemblyComponent:
    item = _exact_mapping(value, path=path, keys=MANIFEST_COMPONENT_KEYS)
    version_ranges = _string_tuple(item["version_ranges"], path=f"{path}.version_ranges")
    for candidate in version_ranges:
        _version(candidate, path=f"{path}.version_ranges[]")
    return AssemblyComponent(
        component_id=_stable_id(item["component_id"], path=f"{path}.component_id"),
        contract_ranges=tuple(
            _stable_id(entry, path=f"{path}.contract_ranges[]")
            for entry in _string_tuple(item["contract_ranges"], path=f"{path}.contract_ranges")
        ),
        version_ranges=version_ranges,
        required_features=tuple(
            _stable_id(entry, path=f"{path}.required_features[]")
            for entry in _string_tuple(item["required_features"], path=f"{path}.required_features")
        ),
        enabled_optional_features=tuple(
            _stable_id(entry, path=f"{path}.enabled_optional_features[]")
            for entry in _string_tuple(
                item["enabled_optional_features"], path=f"{path}.enabled_optional_features"
            )
        ),
    )


def _parse_implication_ack(value: object, *, path: str) -> ImplicationAck:
    item = _exact_mapping(value, path=path, keys=IMPLICATION_ACK_KEYS)
    return ImplicationAck(
        implication_id=_stable_id(item["implication_id"], path=f"{path}.implication_id"),
        authority_ref=_string(item["authority_ref"], path=f"{path}.authority_ref"),
        owner_ref=_string(item["owner_ref"], path=f"{path}.owner_ref"),
        evidence_ref=_string(item["evidence_ref"], path=f"{path}.evidence_ref"),
    )


def _parse_manifest(data: object, *, raw: bytes) -> AssemblyManifest:
    manifest = _exact_mapping(data, path="assembly manifest", keys=MANIFEST_KEYS)
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != SCHEMA_VERSION:
        raise AssemblyResolverError(f"schema_version must equal {SCHEMA_VERSION}")
    components_raw = manifest["components"]
    if not isinstance(components_raw, list):
        raise AssemblyResolverError("components must be a list")
    components = tuple(
        _parse_manifest_component(value, path=f"components[{number}]")
        for number, value in enumerate(components_raw)
    )
    if len({item.component_id for item in components}) != len(components):
        raise AssemblyResolverError("components must not repeat a component_id")
    acks_raw = manifest["implication_acknowledgements"]
    if not isinstance(acks_raw, list):
        raise AssemblyResolverError("implication_acknowledgements must be a list")
    acks = tuple(
        _parse_implication_ack(value, path=f"implication_acknowledgements[{number}]")
        for number, value in enumerate(acks_raw)
    )
    if len({item.implication_id for item in acks}) != len(acks):
        raise AssemblyResolverError("implication_acknowledgements must not repeat an implication_id")
    deployment = _exact_mapping(
        manifest["deployment"], path="deployment", keys=ASSEMBLY_DEPLOYMENT_KEYS
    )
    return AssemblyManifest(
        schema_version=SCHEMA_VERSION,
        assembly_id=_stable_id(manifest["assembly_id"], path="assembly_id"),
        project_ref=_nullable_string(manifest["project_ref"], path="project_ref"),
        components=components,
        deployment=AssemblyDeployment(
            locus=_string(deployment["locus"], path="deployment.locus"),
            trust_zone=_string(deployment["trust_zone"], path="deployment.trust_zone"),
        ),
        implication_acknowledgements=acks,
        smoke_contracts=_string_tuple(manifest["smoke_contracts"], path="smoke_contracts"),
        health_contracts=_string_tuple(manifest["health_contracts"], path="health_contracts"),
        update_policy_ref=_string(manifest["update_policy_ref"], path="update_policy_ref"),
        rollback_policy_ref=_string(manifest["rollback_policy_ref"], path="rollback_policy_ref"),
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )


def parse_assembly_manifest(path: Path) -> AssemblyManifest:
    """Parse one strict canonical assembly manifest without resolving it."""
    try:
        data, raw = _load_yaml(path)
        return _parse_manifest(data, raw=raw)
    except ComponentCatalogueError as exc:
        raise AssemblyResolverError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Compatibility resolver (the fixed eight-step order)
# ---------------------------------------------------------------------------


def _catalogue_digest(records: Mapping[str, ComponentRecord]) -> str:
    semantic = {component_id: records[component_id].source_sha256 for component_id in sorted(records)}
    return hashlib.sha256(
        json.dumps(semantic, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _capability_provisions(
    requirement: CapabilityRequirement, records: Mapping[str, ComponentRecord]
) -> tuple[tuple[str, Any], ...]:
    return tuple(
        (component_id, provision)
        for component_id in sorted(records)
        for provision in records[component_id].provides
        if provision.capability == requirement.capability
    )


def _refine_requirement_refusal(
    requirement: CapabilityRequirement,
    records: Mapping[str, ComponentRecord],
    subject_id: str,
) -> CompatibilityRefusal:
    """Classify why a capability-matched requirement has no exact provider."""
    provisions = _capability_provisions(requirement, records)
    detail = (
        f"{requirement.capability}/{requirement.contract}/"
        f"{requirement.schema}/{requirement.version}"
        + (f"#{requirement.feature}" if requirement.feature else "")
    )
    if not provisions:
        return CompatibilityRefusal(RefusalKind.UNSATISFIED_HARD_REQUIREMENT, subject_id, detail)
    if not any(provision.contract == requirement.contract for _, provision in provisions):
        return CompatibilityRefusal(RefusalKind.CONTRACT_MISMATCH, subject_id, detail)
    if not any(
        provision.contract == requirement.contract and provision.schema == requirement.schema
        for _, provision in provisions
    ):
        return CompatibilityRefusal(RefusalKind.SCHEMA_MISMATCH, subject_id, detail)
    if not any(
        provision.contract == requirement.contract
        and provision.schema == requirement.schema
        and provision.version == requirement.version
        for _, provision in provisions
    ):
        return CompatibilityRefusal(RefusalKind.VERSION_MISMATCH, subject_id, detail)
    return CompatibilityRefusal(RefusalKind.FEATURE_CONFLICT, subject_id, detail)


def _active_requirements(
    record: ComponentRecord, enabled_features: frozenset[str]
) -> tuple[CapabilityRequirement, ...]:
    active = list(record.hard_requirements)
    for optional in record.optional_requirements:
        if optional.enabled:
            active.append(optional)
        elif optional.feature is not None and optional.feature in enabled_features:
            active.append(dataclasses.replace(optional, enabled=True))
    return tuple(active)


def _closure(component_id: str, edges: Mapping[str, set[str]]) -> tuple[str, ...]:
    seen: set[str] = set()
    stack = [component_id]
    while stack:
        node = stack.pop()
        for target in edges.get(node, ()):  # noqa: SIM118
            if target not in seen:
                seen.add(target)
                stack.append(target)
    return tuple(sorted(seen))


# ---------------------------------------------------------------------------
# Upkeep acknowledgement ack-id derivation (T-3931, ORG-055 §6 fidelity)
# ---------------------------------------------------------------------------
#
# ORG-055 §6 requires a seed to ACKNOWLEDGE -- not merely own -- each assembled
# component's data-retention, credential, and ongoing-cost implications before
# assembly.  The step-5 acknowledgement gate therefore requires an ImplicationAck
# (manifest.implication_acknowledgements) for every upkeep item in
# ``upkeep.cost`` / ``upkeep.storage_retention`` / ``upkeep.credential_rotation``,
# keyed on a DERIVED ack-id the seed author reproduces as the ack's
# ``implication_id``.  ``upkeep.monitoring`` / ``upgrade_rollback`` /
# ``review_obligations`` stay owner-gated only (step 6), per §6 line 167.
#
# Derivation (the seed author MUST reproduce this exactly):
#
#   cost <cost_id>              -> "cost-<cost_id>"
#   storage_retention <entry>   -> "storage-retention-<h>"
#   credential_rotation <entry> -> "credential-rotation-<h>"
#
# where <h> is the first 12 lowercase hex characters of the sha256 of the exact
# entry string encoded as UTF-8.  Rationale:
#
# * cost_id is already a per-component-unique stable-id, so it is used directly
#   under the ``cost-`` prefix -- no hash needed and the ack-id stays readable.
# * storage_retention / credential_rotation entries are free strings with no id
#   of their own.  A hash (not a slug) gives each DISTINCT entry its own stable
#   ack-id: two distinct retention/credential consequences that slugified to the
#   same token would let ONE ack silently cover BOTH and under-acknowledge, which
#   defeats the gate.  12 hex chars = 48 bits, far more than enough to keep an
#   accidental collision between the handful of entries per component
#   astronomically unlikely while keeping the id short.
# * The three family prefixes keep the derived ids disjoint from EACH OTHER: a
#   cost ack-id can never equal a storage_retention or credential_rotation ack-id,
#   nor two upkeep families collide.
#
# The derived ids and the ComponentImplication ids share ONE acknowledgement
# namespace -- both are matched against manifest.implication_acknowledgements by
# id, and accepted upkeep ids fold into the SAME ``acknowledged`` set.  The
# prefixes alone do NOT stop a catalogue author from naming a ComponentImplication
# ``implication_id`` such as "cost-<x>"; were that allowed, a single ack "cost-<x>"
# could discharge BOTH that implication AND a cost whose cost_id is "<x>" -- one
# acknowledgement silently covering two distinct obligations, which defeats §6's
# "acknowledge EACH".  The step-5 upkeep loop therefore ENFORCES the invariant at
# resolve-acceptance (F1, T-3931 rebound-1): a derived upkeep ack-id that
# coincides with ANY ComponentImplication ``implication_id`` in the assembled set
# is REFUSED as an ambiguous acknowledgement (RefusalKind.UNACKNOWLEDGED_IMPLICATION),
# so an accepted assembly's upkeep-id space and implication-id space never overlap
# and no single ack can ever cover two distinct obligations.
#
# Every derived id is a valid _stable_id (^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$): the
# prefixes are lowercase words, cost_id is stable-id-valid by catalogue rule
# (STABLE_ID_PATTERN, unique per component), and a 12-hex tail is a valid
# trailing ``-[a-z0-9]+`` segment.  This matters because the ack's implication_id
# is _stable_id-validated at manifest parse time, so an unreproducible id could
# never be written into a manifest.

_UPKEEP_ACK_HASH_WIDTH = 12


def _hashed_upkeep_ack_id(prefix: str, entry: str) -> str:
    """Derive a namespaced, stable-id-valid ack-id for a free-string upkeep entry."""
    digest = hashlib.sha256(entry.encode("utf-8")).hexdigest()[:_UPKEEP_ACK_HASH_WIDTH]
    return f"{prefix}-{digest}"


def resolve_assembly(
    manifest: AssemblyManifest,
    records: Mapping[str, ComponentRecord],
    repo_resolver: LogicalRepoResolver,
    evidence: EvidenceResolver,
) -> AssemblyPlan:
    """Run the fixed eight-step compatibility resolver.

    All knowable incompatibilities are accumulated as typed refusals before any
    lifecycle mutation (this module never mutates); the caller renders a lock
    only when there are no refusals.
    """
    refusals: list[CompatibilityRefusal] = []

    # Step 1: resolve component refs (dangling manifest component -> unknown-provider).
    present_roots: list[AssemblyComponent] = []
    for component in manifest.components:
        if component.component_id not in records:
            refusals.append(
                CompatibilityRefusal(
                    RefusalKind.UNKNOWN_PROVIDER,
                    component.component_id,
                    "manifest names a component absent from the catalogue",
                )
            )
        else:
            present_roots.append(component)

    enabled_by_root: dict[str, frozenset[str]] = {
        component.component_id: frozenset(component.required_features)
        | frozenset(component.enabled_optional_features)
        for component in present_roots
    }

    # Step 2/3: prove uniqueness + complete closure; refine capability-only
    # matches into typed contract/schema/version/feature mismatches.
    assembled: set[str] = {component.component_id for component in present_roots}
    edges: dict[str, set[str]] = {}
    visited: set[str] = set()
    queue = [component.component_id for component in present_roots]
    while queue:
        component_id = queue.pop(0)
        if component_id in visited:
            continue
        visited.add(component_id)
        record = records[component_id]
        enabled = enabled_by_root.get(component_id, frozenset())
        for requirement in _active_requirements(record, enabled):
            providers = _matching_providers(requirement, records)
            if len(providers) == 1:
                provider = providers[0]
                edges.setdefault(component_id, set()).add(provider)
                if provider not in assembled:
                    assembled.add(provider)
                    queue.append(provider)
            elif len(providers) > 1:
                refusals.append(
                    CompatibilityRefusal(
                        RefusalKind.AMBIGUOUS_PROVIDER,
                        component_id,
                        f"{requirement.capability} providers={providers!r}",
                    )
                )
            else:
                refusals.append(
                    _refine_requirement_refusal(requirement, records, component_id)
                )

    # Step 3 (manifest ranges): constrain each named root's own provisions.
    for component in present_roots:
        record = records[component.component_id]
        provided_features: set[str] = set()
        for provision in record.provides:
            provided_features.update(provision.features)
            if component.contract_ranges and provision.contract not in component.contract_ranges:
                refusals.append(
                    CompatibilityRefusal(
                        RefusalKind.CONTRACT_MISMATCH,
                        component.component_id,
                        f"provided contract {provision.contract!r} outside {list(component.contract_ranges)!r}",
                    )
                )
            if component.version_ranges and provision.version not in component.version_ranges:
                refusals.append(
                    CompatibilityRefusal(
                        RefusalKind.VERSION_MISMATCH,
                        component.component_id,
                        f"provided version {provision.version!r} outside {list(component.version_ranges)!r}",
                    )
                )
        for feature in component.required_features:
            if feature not in provided_features:
                refusals.append(
                    CompatibilityRefusal(
                        RefusalKind.FEATURE_CONFLICT,
                        component.component_id,
                        f"required feature {feature!r} not provided",
                    )
                )

    # Step 4: refuse component and feature conflicts across the assembled set.
    assembled_capabilities = {
        provision.capability for cid in assembled for provision in records[cid].provides
    }
    assembled_features = {
        feature
        for cid in assembled
        for provision in records[cid].provides
        for feature in provision.features
    }
    for cid in enabled_by_root:
        assembled_features.update(enabled_by_root[cid])
    for cid in sorted(assembled):
        for conflict in records[cid].compatibility.conflicts:
            target = conflict.target
            kind = conflict.kind.value
            if kind == "component" and target in assembled:
                refusals.append(
                    CompatibilityRefusal(
                        RefusalKind.COMPONENT_CONFLICT, cid, f"component {target!r}: {conflict.reason_ref}"
                    )
                )
            elif kind == "capability" and target in assembled_capabilities:
                refusals.append(
                    CompatibilityRefusal(
                        RefusalKind.COMPONENT_CONFLICT, cid, f"capability {target!r}: {conflict.reason_ref}"
                    )
                )
            elif kind == "feature" and target in assembled_features:
                refusals.append(
                    CompatibilityRefusal(
                        RefusalKind.FEATURE_CONFLICT, cid, f"feature {target!r}: {conflict.reason_ref}"
                    )
                )

    # Step 5: require acknowledgement of every implication with matching scope.
    ack_by_id = {ack.implication_id: ack for ack in manifest.implication_acknowledgements}
    acknowledged: set[str] = set()
    # Every ComponentImplication id in the assembled set, used below to keep the
    # derived upkeep ack-id space disjoint from the implication-id space (F1).
    implication_ids: set[str] = set()
    for cid in sorted(assembled):
        implications = records[cid].implications
        for implication in (
            implications.authority
            + implications.privacy_data
            + implications.hardware_network
            + implications.operational
        ):
            implication_ids.add(implication.implication_id)
            ack = ack_by_id.get(implication.implication_id)
            if ack is None:
                refusals.append(
                    CompatibilityRefusal(
                        RefusalKind.UNACKNOWLEDGED_IMPLICATION,
                        cid,
                        f"implication {implication.implication_id!r} not acknowledged",
                    )
                )
            elif ack.evidence_ref not in implication.evidence_refs:
                refusals.append(
                    CompatibilityRefusal(
                        RefusalKind.UNACKNOWLEDGED_IMPLICATION,
                        cid,
                        f"implication {implication.implication_id!r} acknowledged with wrong-scope evidence",
                        evidence_ref=ack.evidence_ref,
                    )
                )
            else:
                acknowledged.add(implication.implication_id)

    # Step 5 (upkeep, T-3931): ORG-055 §6 also requires the seed to ACKNOWLEDGE
    # each component's data-retention, credential, and ongoing-cost implications
    # -- not merely own them (step 6).  Require an ImplicationAck whose
    # implication_id equals the derived ack-id (see _hashed_upkeep_ack_id and the
    # derivation note above) for every upkeep.cost / storage_retention /
    # credential_rotation item, and fold accepted ids into the SAME acknowledged
    # set that flows to the lock.  Unlike a ComponentImplication these carry no
    # evidence_refs, so this is an ack-EXISTENCE check with no evidence-scope
    # check.  A component with none of these upkeep items adds zero ids, so its
    # acknowledged_implications -- and therefore its lock bytes -- are unchanged.
    for cid in sorted(assembled):
        upkeep = records[cid].upkeep
        # Collect each upkeep item's derived ack-id with a human-readable label,
        # then apply the same two gates: (1) the F1 disjointness invariant, then
        # (2) ack-EXISTENCE (no evidence-scope check -- these carry no evidence_refs).
        derived: list[tuple[str, str]] = [
            (f"cost-{cost.cost_id}", f"cost {cost.cost_id}") for cost in upkeep.cost
        ]
        for family, prefix, entries in (
            ("storage_retention", "storage-retention", upkeep.storage_retention),
            ("credential_rotation", "credential-rotation", upkeep.credential_rotation),
        ):
            derived.extend(
                (_hashed_upkeep_ack_id(prefix, entry), f"{family} {entry!r}") for entry in entries
            )
        for ack_id, item in derived:
            if ack_id in implication_ids:
                # F1 (T-3931 rebound-1): the derived id coincides with a
                # ComponentImplication id in the assembled set, so a single ack
                # would discharge BOTH obligations.  Refuse rather than silently
                # share the ack -- this keeps the two id spaces disjoint at
                # acceptance.  (No new RefusalKind: reuse the step-5 kind.)
                refusals.append(
                    CompatibilityRefusal(
                        RefusalKind.UNACKNOWLEDGED_IMPLICATION,
                        cid,
                        f"{item} (ack-id {ack_id!r}) collides with a ComponentImplication "
                        f"of the same id -- acknowledgement would be ambiguous",
                    )
                )
            elif ack_id in ack_by_id:
                acknowledged.add(ack_id)
            else:
                refusals.append(
                    CompatibilityRefusal(
                        RefusalKind.UNACKNOWLEDGED_IMPLICATION,
                        cid,
                        f"{item} (ack-id {ack_id!r}) not acknowledged",
                    )
                )

    # Step 6: require an owner for every operational/review/cost obligation.
    owned: set[str] = set()
    for cid in sorted(assembled):
        upkeep = records[cid].upkeep
        steward = upkeep.steward
        for category, refs in (
            ("monitoring", upkeep.monitoring),
            ("upgrade_rollback", upkeep.upgrade_rollback),
            ("credential_rotation", upkeep.credential_rotation),
            ("storage_retention", upkeep.storage_retention),
            ("review_obligations", upkeep.review_obligations),
        ):
            for ref in refs:
                if not steward.strip():
                    refusals.append(
                        CompatibilityRefusal(
                            RefusalKind.UNOWNED_OBLIGATION, cid, f"{category}:{ref} has no steward owner"
                        )
                    )
                else:
                    owned.add(f"{cid}:{category}:{ref}")
        for cost in upkeep.cost:
            if not cost.owner.strip():
                refusals.append(
                    CompatibilityRefusal(
                        RefusalKind.UNOWNED_OBLIGATION, cid, f"cost:{cost.cost_id} has no owner"
                    )
                )
            else:
                owned.add(f"{cid}:cost:{cost.cost_id}")

    # Step 7: resolve exact source/artifact identity through the adapter seam.
    locked: list[LockedComponent] = []
    enabled_features_by_cid: dict[str, tuple[str, ...]] = {
        component.component_id: tuple(sorted(enabled_by_root[component.component_id]))
        for component in present_roots
    }
    for cid in sorted(assembled):
        record = records[cid]
        logical_repo = record.source.logical_repo
        try:
            resolved = repo_resolver.resolve(logical_repo)
        except (KeyError, LookupError, AssemblyResolverError):
            resolved = None
        if resolved is None:
            refusals.append(
                CompatibilityRefusal(
                    RefusalKind.UNRESOLVED_SOURCE, cid, f"logical repo {logical_repo!r} did not resolve"
                )
            )
            continue
        tested = record.compatibility.tested_revision
        if resolved.revision != tested:
            refusals.append(
                CompatibilityRefusal(
                    RefusalKind.PROVENANCE_MISMATCH,
                    cid,
                    f"resolved revision {resolved.revision!r} != tested {tested!r}",
                )
            )
            continue
        contract_versions = tuple(
            sorted({(provision.contract, provision.version) for provision in record.provides})
        )
        schema_versions = tuple(
            sorted({(provision.schema, provision.version) for provision in record.provides})
        )
        compatibility_evidence = tuple(
            sorted(
                {f"tested-revision:{tested}", f"resolved-revision:{resolved.revision}"}
            )
        )
        locked.append(
            LockedComponent(
                component_id=cid,
                logical_repo=logical_repo,
                path=record.source.path,
                artifact=record.source.artifact,
                revision=resolved.revision,
                artifact_digest=resolved.artifact_digest,
                contract_versions=contract_versions,
                schema_versions=schema_versions,
                enabled_features=enabled_features_by_cid.get(cid, ()),
                requirement_closure=_closure(cid, edges),
                compatibility_evidence=compatibility_evidence,
                submodules=tuple(
                    sorted(resolved.submodules, key=lambda view: (view.path, view.revision))
                ),
            )
        )

    # Step 7 (evidence): resolve smoke/health evidence presence.
    smoke_evidence: list[str] = []
    for ref in manifest.smoke_contracts:
        presence = evidence.resolve(ref)
        if not presence.present:
            refusals.append(
                CompatibilityRefusal(RefusalKind.SMOKE_FAILURE, manifest.assembly_id, f"smoke evidence {ref!r} absent")
            )
        else:
            smoke_evidence.append(f"{ref}@{presence.digest}")
    health_evidence: list[str] = []
    for ref in manifest.health_contracts:
        presence = evidence.resolve(ref)
        if not presence.present:
            refusals.append(
                CompatibilityRefusal(RefusalKind.HEALTH_FAILURE, manifest.assembly_id, f"health evidence {ref!r} absent")
            )
        else:
            health_evidence.append(f"{ref}@{presence.digest}")

    ordered_refusals = tuple(
        sorted(refusals, key=lambda refusal: (refusal.kind.value, refusal.subject_id, refusal.detail))
    )
    report = {
        "assembly_id": manifest.assembly_id,
        "assembled_component_ids": sorted(assembled),
        "resolution_order": list(RESOLUTION_ORDER),
        "refusals": [refusal.as_text() for refusal in ordered_refusals],
    }
    return AssemblyPlan(
        manifest=manifest,
        catalogue_sha256=_catalogue_digest(records),
        locked_components=tuple(sorted(locked, key=lambda item: item.component_id)),
        acknowledged_implications=tuple(sorted(acknowledged)),
        owned_obligations=tuple(sorted(owned)),
        smoke_evidence=tuple(sorted(smoke_evidence)),
        health_evidence=tuple(sorted(health_evidence)),
        report=report,
        refusals=ordered_refusals,
    )


# ---------------------------------------------------------------------------
# Deterministic lock rendering + verification
# ---------------------------------------------------------------------------


def _submodule_json(view: SubmoduleView) -> dict[str, object]:
    return {"path": view.path, "revision": view.revision}


def _locked_component_json(component: LockedComponent) -> dict[str, object]:
    return {
        "component_id": component.component_id,
        "logical_repo": component.logical_repo,
        "path": component.path,
        "artifact": component.artifact,
        "revision": component.revision,
        "artifact_digest": component.artifact_digest,
        # A list of {contract/schema, version} pairs, sorted by (id, version), so
        # a component providing one contract/schema id at two versions records
        # BOTH -- an id-keyed map would silently drop all but the last version.
        "contract_versions": [
            {"contract": contract, "version": version}
            for contract, version in sorted(component.contract_versions)
        ],
        "schema_versions": [
            {"schema": schema, "version": version}
            for schema, version in sorted(component.schema_versions)
        ],
        "enabled_features": list(component.enabled_features),
        "requirement_closure": list(component.requirement_closure),
        "compatibility_evidence": list(component.compatibility_evidence),
        "submodules": [
            _submodule_json(view)
            for view in sorted(component.submodules, key=lambda item: (item.path, item.revision))
        ],
    }


def build_lock(plan: AssemblyPlan) -> AssemblyLock:
    """Build the exact lock object from a refusal-free plan.

    Note on ordering: the lock's own ``components`` array is order-independent
    (sorted by component_id), but the lock pins the exact manifest via
    ``manifest_sha256 = sha256(raw manifest bytes)``.  The manifest's byte order
    -- including the order of its ``components:`` list -- is therefore part of
    the pinned identity: reordering that list changes manifest_sha256 and so
    changes the lock.  This is by design, not "deterministic across all input
    ordering"; only the components ARRAY is ordering-independent.
    """
    if plan.refusals:
        raise _refuse(plan.refusals[0])
    return AssemblyLock(
        schema_version=SCHEMA_VERSION,
        assembly_id=plan.manifest.assembly_id,
        manifest_sha256=f"sha256:{plan.manifest.source_sha256}",
        catalogue_sha256=f"sha256:{plan.catalogue_sha256}",
        generator_id=GENERATOR_ID,
        generator_version=GENERATOR_VERSION,
        resolution_order=RESOLUTION_ORDER,
        components=tuple(sorted(plan.locked_components, key=lambda item: item.component_id)),
        acknowledged_implications=tuple(sorted(plan.acknowledged_implications)),
        owned_obligations=tuple(sorted(plan.owned_obligations)),
        smoke_evidence=tuple(sorted(plan.smoke_evidence)),
        health_evidence=tuple(sorted(plan.health_evidence)),
        prior_lock_sha256=plan.prior_lock_sha256,
        update_receipt_ref=plan.update_receipt_ref,
    )


def _lock_json(lock: AssemblyLock) -> dict[str, object]:
    return {
        "schema_version": lock.schema_version,
        "assembly_id": lock.assembly_id,
        "manifest_sha256": lock.manifest_sha256,
        "catalogue_sha256": lock.catalogue_sha256,
        "generator_id": lock.generator_id,
        "generator_version": lock.generator_version,
        "resolution_order": list(lock.resolution_order),
        "components": [
            _locked_component_json(component)
            for component in sorted(lock.components, key=lambda item: item.component_id)
        ],
        "acknowledged_implications": list(lock.acknowledged_implications),
        "owned_obligations": list(lock.owned_obligations),
        "smoke_evidence": list(lock.smoke_evidence),
        "health_evidence": list(lock.health_evidence),
        "prior_lock_sha256": lock.prior_lock_sha256,
        "update_receipt_ref": lock.update_receipt_ref,
    }


def render_lock(plan: AssemblyPlan) -> bytes:
    """Render deterministic canonical lock bytes.

    The rendering is independent of catalogue/record ordering and of the plan's
    locked-component order (components are sorted by component_id).  It is NOT
    independent of manifest byte order: ``manifest_sha256`` pins the raw manifest
    bytes, so reordering the manifest's ``components:`` list yields a different
    lock (differing only in manifest_sha256).  See ``build_lock``.
    """
    return _json_bytes(_lock_json(build_lock(plan)))


def _parse_submodule(value: object) -> SubmoduleView:
    item = _exact_mapping(value, path="lock submodule", keys=SUBMODULE_KEYS)
    return SubmoduleView(
        path=_string(item["path"], path="submodule.path"),
        revision=_string(item["revision"], path="submodule.revision"),
    )


def _parse_version_pairs(value: object, *, key: str, path: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise AssemblyResolverError(f"lock component {path} must be a list of {{{key}, version}} pairs")
    pairs = []
    for number, entry in enumerate(value):
        item = _exact_mapping(entry, path=f"{path}[{number}]", keys=(key, "version"))
        pairs.append(
            (
                _string(item[key], path=f"{path}[{number}].{key}"),
                _string(item["version"], path=f"{path}[{number}].version"),
            )
        )
    return tuple(sorted(pairs))


def _parse_locked_component(value: object) -> LockedComponent:
    item = _exact_mapping(value, path="lock component", keys=LOCKED_COMPONENT_KEYS)
    submodules_raw = item["submodules"]
    if not isinstance(submodules_raw, list):
        raise AssemblyResolverError("lock component submodules must be a list")
    return LockedComponent(
        component_id=_stable_id(item["component_id"], path="lock.component_id"),
        logical_repo=_string(item["logical_repo"], path="lock.logical_repo"),
        path=_nullable_string(item["path"], path="lock.path"),
        artifact=_nullable_string(item["artifact"], path="lock.artifact"),
        revision=_string(item["revision"], path="lock.revision"),
        artifact_digest=_string(item["artifact_digest"], path="lock.artifact_digest"),
        contract_versions=_parse_version_pairs(
            item["contract_versions"], key="contract", path="lock.contract_versions"
        ),
        schema_versions=_parse_version_pairs(
            item["schema_versions"], key="schema", path="lock.schema_versions"
        ),
        enabled_features=tuple(_string_tuple(item["enabled_features"], path="lock.enabled_features")),
        requirement_closure=tuple(
            _string_tuple(item["requirement_closure"], path="lock.requirement_closure")
        ),
        compatibility_evidence=tuple(
            _string_tuple(item["compatibility_evidence"], path="lock.compatibility_evidence")
        ),
        submodules=tuple(
            sorted(
                (_parse_submodule(entry) for entry in submodules_raw),
                key=lambda view: (view.path, view.revision),
            )
        ),
    )


def parse_assembly_lock(data: object) -> AssemblyLock:
    """Parse generated lock bytes (or a decoded object) into an AssemblyLock."""
    try:
        return _parse_assembly_lock(data)
    except ComponentCatalogueError as exc:
        raise AssemblyResolverError(str(exc)) from exc


def _parse_assembly_lock(data: object) -> AssemblyLock:
    if isinstance(data, (bytes, bytearray)):
        data = json.loads(bytes(data).decode("utf-8"))
    elif isinstance(data, str):
        data = json.loads(data)
    lock = _exact_mapping(data, path="assembly lock", keys=LOCK_KEYS)
    if type(lock["schema_version"]) is not int or lock["schema_version"] != SCHEMA_VERSION:
        raise AssemblyResolverError(f"schema_version must equal {SCHEMA_VERSION}")
    components_raw = lock["components"]
    if not isinstance(components_raw, list):
        raise AssemblyResolverError("lock components must be a list")
    return AssemblyLock(
        schema_version=SCHEMA_VERSION,
        assembly_id=_stable_id(lock["assembly_id"], path="lock.assembly_id"),
        manifest_sha256=_string(lock["manifest_sha256"], path="lock.manifest_sha256"),
        catalogue_sha256=_string(lock["catalogue_sha256"], path="lock.catalogue_sha256"),
        generator_id=_string(lock["generator_id"], path="lock.generator_id"),
        generator_version=_string(lock["generator_version"], path="lock.generator_version"),
        resolution_order=tuple(_string_tuple(lock["resolution_order"], path="lock.resolution_order")),
        components=tuple(
            sorted(
                (_parse_locked_component(entry) for entry in components_raw),
                key=lambda item: item.component_id,
            )
        ),
        acknowledged_implications=tuple(
            _string_tuple(lock["acknowledged_implications"], path="lock.acknowledged_implications")
        ),
        owned_obligations=tuple(_string_tuple(lock["owned_obligations"], path="lock.owned_obligations")),
        smoke_evidence=tuple(_string_tuple(lock["smoke_evidence"], path="lock.smoke_evidence")),
        health_evidence=tuple(_string_tuple(lock["health_evidence"], path="lock.health_evidence")),
        prior_lock_sha256=_nullable_string(lock["prior_lock_sha256"], path="lock.prior_lock_sha256"),
        update_receipt_ref=_nullable_string(lock["update_receipt_ref"], path="lock.update_receipt_ref"),
    )


def _validate_audit_fields(lock: AssemblyLock, subject_id: str) -> None:
    """Validate the SHAPE of the two publish-time audit-chain fields.

    ``prior_lock_sha256`` and ``update_receipt_ref`` are the only lock fields not
    recomputable from (manifest, catalogue, records, resolver, evidence), so
    verify cannot re-derive them.  It CAN reject a malformed value: a
    prior_lock_sha256 that is neither None nor the module's sha256:<hex> digest
    form, or an update_receipt_ref that is neither None nor a non-empty string.
    A well-formed value's LINEAGE (does it point at the real prior lock?) is
    deliberately outside verify's authority -- see verify_generated_lock.
    """
    prior = lock.prior_lock_sha256
    if prior is not None and _SHA256_FORM.fullmatch(prior) is None:
        raise _refuse(
            CompatibilityRefusal(
                RefusalKind.HAND_EDITED_LOCK,
                subject_id,
                f"prior_lock_sha256 {prior!r} is not None or a sha256:<hex> digest",
            )
        )
    receipt = lock.update_receipt_ref
    if receipt is not None and not receipt:
        raise _refuse(
            CompatibilityRefusal(
                RefusalKind.HAND_EDITED_LOCK,
                subject_id,
                "update_receipt_ref must be None or a non-empty string",
            )
        )


def verify_generated_lock(
    manifest: AssemblyManifest,
    lock_bytes: bytes,
    records: Mapping[str, ComponentRecord],
    repo_resolver: LogicalRepoResolver,
    evidence: EvidenceResolver,
) -> None:
    """Re-resolve and re-render; refuse tampering, staleness, and artifact drift.

    Returns None on a valid generated lock.  Raises AssemblyResolverError with a
    typed refusal otherwise.

    Audit-chain exemption (honest): every lock field is byte-compared against a
    deterministic regeneration EXCEPT the two publish-time audit fields,
    ``prior_lock_sha256`` and ``update_receipt_ref``.  These are not recomputable
    from (manifest, catalogue, records, resolver, evidence) -- verify has no
    prior lock in hand -- so their SHAPE is validated here while their LINEAGE is
    NOT.  A forged-but-well-formed prior_lock_sha256 therefore passes verify; the
    authoritative lineage check (candidate.prior_lock_sha256 == digest of the
    real prior lock) lives in ``plan_assembly_update``, where the prior lock IS
    available.
    """
    try:
        lock = parse_assembly_lock(lock_bytes)
    except (AssemblyResolverError, ValueError, KeyError, TypeError) as exc:
        raise _refuse(
            CompatibilityRefusal(
                RefusalKind.HAND_EDITED_LOCK, manifest.assembly_id, f"lock bytes do not parse: {exc}"
            )
        ) from exc

    # Shape-check the two unverifiable audit fields before anything else so a
    # malformed value is a typed refusal, not a silent pass.
    _validate_audit_fields(lock, manifest.assembly_id)

    plan = resolve_assembly(manifest, records, repo_resolver, evidence)
    if plan.refusals:
        raise _refuse(plan.refusals[0])

    if lock.manifest_sha256 != f"sha256:{manifest.source_sha256}" or lock.catalogue_sha256 != f"sha256:{plan.catalogue_sha256}":
        raise _refuse(
            CompatibilityRefusal(
                RefusalKind.STALE_LOCK_INPUT,
                manifest.assembly_id,
                "lock manifest/catalogue digest does not match current inputs",
            )
        )

    fresh = {component.component_id: component for component in plan.locked_components}
    for locked in lock.components:
        current = fresh.get(locked.component_id)
        if current is None:
            continue
        if current.revision == locked.revision and current.artifact_digest != locked.artifact_digest:
            raise _refuse(
                CompatibilityRefusal(
                    RefusalKind.ARTIFACT_CHANGED_SAME_REVISION,
                    locked.component_id,
                    f"artifact digest changed under revision {locked.revision!r}",
                )
            )
        if current.submodules != locked.submodules:
            raise _refuse(
                CompatibilityRefusal(
                    RefusalKind.SUBMODULE_MISMATCH,
                    locked.component_id,
                    "submodule view disagrees with the lock",
                )
            )

    for ref in lock.smoke_evidence:
        original = ref.rsplit("@", 1)[0]
        if not evidence.resolve(original).present:
            raise _refuse(
                CompatibilityRefusal(RefusalKind.SMOKE_FAILURE, manifest.assembly_id, f"smoke evidence {original!r} absent")
            )
    for ref in lock.health_evidence:
        original = ref.rsplit("@", 1)[0]
        if not evidence.resolve(original).present:
            raise _refuse(
                CompatibilityRefusal(RefusalKind.HEALTH_FAILURE, manifest.assembly_id, f"health evidence {original!r} absent")
            )

    # Copy the (already shape-validated) audit fields into the regeneration so the
    # byte-compare covers everything ELSE exactly.  This copy is the documented
    # exemption: verify cannot recompute these two values, so a well-formed value
    # is accepted here regardless of lineage (plan_assembly_update owns lineage).
    candidate = dataclasses.replace(
        plan,
        prior_lock_sha256=lock.prior_lock_sha256,
        update_receipt_ref=lock.update_receipt_ref,
    )
    if render_lock(candidate) != lock_bytes:
        raise _refuse(
            CompatibilityRefusal(
                RefusalKind.HAND_EDITED_LOCK,
                manifest.assembly_id,
                "lock bytes differ from deterministic regeneration",
            )
        )


# ---------------------------------------------------------------------------
# Assembly update planner + atomic publish primitive
# ---------------------------------------------------------------------------


def plan_assembly_update(
    old: AssemblyLock | None,
    candidate: AssemblyLock,
    lifecycle: RepositoryLifecyclePlanner,
    *,
    prior_lock_bytes: bytes | None = None,
) -> UpdatePlan:
    """Plan (never execute) an assembly update through the T-2951 lifecycle seam.

    When a prior lock is supplied this is the authoritative lineage gate:
    ``candidate.prior_lock_sha256`` must equal the sha256 digest of the real
    prior lock (``prior_lock_bytes`` when given, else the canonical bytes of
    ``old``).  verify_generated_lock cannot make this check -- it has no prior
    lock -- so a forged-but-well-formed back-pointer only fails here.
    """
    if old is not None:
        expected = lock_digest(
            prior_lock_bytes if prior_lock_bytes is not None else _json_bytes(_lock_json(old))
        )
        if candidate.prior_lock_sha256 != expected:
            raise _refuse(
                CompatibilityRefusal(
                    RefusalKind.STALE_LOCK_INPUT,
                    candidate.assembly_id,
                    f"candidate prior_lock_sha256 {candidate.prior_lock_sha256!r} "
                    f"does not match the supplied prior lock digest {expected!r}",
                )
            )
    result = lifecycle.plan(candidate.components)
    if not result.ok:
        refusal = result.refusal or CompatibilityRefusal(
            RefusalKind.LIFECYCLE_REFUSED,
            candidate.assembly_id,
            "lifecycle refused without a typed refusal",
        )
        raise _refuse(refusal)
    return UpdatePlan(
        prior_lock=old,
        candidate_lock=candidate,
        lifecycle_plan=result,
        stages=UPDATE_STAGES,
        rollback=ROLLBACK_STAGES,
    )


def lock_digest(lock_bytes: bytes) -> str:
    return f"sha256:{hashlib.sha256(lock_bytes).hexdigest()}"


def publish_lock(path: Path, lock_bytes: bytes, *, expected_prior_digest: str | None) -> str:
    """Atomically publish lock bytes with a compare-and-swap on the prior digest.

    The commit point is the temp-write + rename in ``_atomic_write``: a crash
    leaves either the exact prior file or the exact new file, never a partial.
    A prior-digest mismatch refuses before any write (stale-lock-input).
    """
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        current = None
    current_digest = None if current is None else lock_digest(current)
    if current_digest != expected_prior_digest:
        raise _refuse(
            CompatibilityRefusal(
                RefusalKind.STALE_LOCK_INPUT,
                path.name,
                f"prior-lock digest {current_digest!r} != expected {expected_prior_digest!r}",
            )
        )
    _atomic_write(path, lock_bytes)
    return lock_digest(lock_bytes)


# ---------------------------------------------------------------------------
# Generated schemas
# ---------------------------------------------------------------------------


def _assembly_manifest_schema() -> dict[str, object]:
    stable = {"type": "string", "pattern": STABLE_ID_PATTERN}
    text = {"type": "string", "minLength": 1}
    version = {"type": "string", "pattern": VERSION_PATTERN}
    component = _object_schema(
        MANIFEST_COMPONENT_KEYS,
        {
            "component_id": stable,
            "contract_ranges": _array_schema(stable),
            "version_ranges": _array_schema(version),
            "required_features": _array_schema(stable),
            "enabled_optional_features": _array_schema(stable),
        },
    )
    ack = _object_schema(
        IMPLICATION_ACK_KEYS,
        {
            "implication_id": stable,
            "authority_ref": text,
            "owner_ref": text,
            "evidence_ref": text,
        },
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://pantheon.local/schemas/assembly-manifest.schema.json",
        "title": "Pantheon ORG-055 assembly manifest",
        "type": "object",
        "required": list(MANIFEST_KEYS),
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "assembly_id": stable,
            "project_ref": _nullable_string_schema(),
            "components": _array_schema(component),
            "deployment": _object_schema(
                ASSEMBLY_DEPLOYMENT_KEYS, {"locus": text, "trust_zone": text}
            ),
            "implication_acknowledgements": _array_schema(ack),
            "smoke_contracts": _array_schema(),
            "health_contracts": _array_schema(),
            "update_policy_ref": text,
            "rollback_policy_ref": text,
        },
    }


def _assembly_lock_schema() -> dict[str, object]:
    stable = {"type": "string", "pattern": STABLE_ID_PATTERN}
    text = {"type": "string", "minLength": 1}
    version = {"type": "string", "pattern": VERSION_PATTERN}
    submodule = _object_schema(SUBMODULE_KEYS, {"path": text, "revision": text})
    contract_version = _object_schema(
        CONTRACT_VERSION_KEYS, {"contract": stable, "version": version}
    )
    schema_version = _object_schema(
        SCHEMA_VERSION_KEYS, {"schema": stable, "version": version}
    )
    component = _object_schema(
        LOCKED_COMPONENT_KEYS,
        {
            "component_id": stable,
            "logical_repo": stable,
            "path": _nullable_string_schema(),
            "artifact": _nullable_string_schema(),
            "revision": text,
            "artifact_digest": text,
            "contract_versions": _array_schema(contract_version),
            "schema_versions": _array_schema(schema_version),
            "enabled_features": _array_schema(stable),
            "requirement_closure": _array_schema(stable),
            "compatibility_evidence": _array_schema(),
            "submodules": _array_schema(submodule),
        },
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://pantheon.local/schemas/assembly-lock.schema.json",
        "title": "Pantheon ORG-055 assembly lock",
        "type": "object",
        "required": list(LOCK_KEYS),
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": SCHEMA_VERSION},
            "assembly_id": stable,
            "manifest_sha256": text,
            "catalogue_sha256": text,
            "generator_id": text,
            "generator_version": text,
            "resolution_order": _array_schema(),
            "components": _array_schema(component),
            "acknowledged_implications": _array_schema(),
            "owned_obligations": _array_schema(),
            "smoke_evidence": _array_schema(),
            "health_evidence": _array_schema(),
            "prior_lock_sha256": _nullable_string_schema(),
            "update_receipt_ref": _nullable_string_schema(),
        },
    }


def generate_assembly_manifest_schema() -> bytes:
    return _json_bytes(_assembly_manifest_schema())


def generate_assembly_lock_schema() -> bytes:
    return _json_bytes(_assembly_lock_schema())


# ---------------------------------------------------------------------------
# CLI (static, policy-free facts wire the injected seams for offline use)
# ---------------------------------------------------------------------------


class _StaticSourceResolver:
    """CLI-only inert reader of resolved-source facts; encodes no remote policy."""

    def __init__(self, facts: Mapping[str, object]):
        self._facts = facts

    def resolve(self, logical_repo: str) -> ResolvedSource:
        raw = self._facts.get(logical_repo)
        if not isinstance(raw, dict):
            raise KeyError(logical_repo)
        return ResolvedSource(
            logical_repo=logical_repo,
            resolved_source=_string(raw.get("resolved_source"), path="facts.resolved_source"),
            revision=_string(raw.get("revision"), path="facts.revision"),
            artifact_digest=_string(raw.get("artifact_digest"), path="facts.artifact_digest"),
            redirect_chain=tuple(raw.get("redirect_chain", ())),
            submodules=tuple(
                SubmoduleView(path=str(entry["path"]), revision=str(entry["revision"]))
                for entry in raw.get("submodules", ())
            ),
        )


class _StaticEvidenceResolver:
    """CLI-only inert reader of evidence presence facts."""

    def __init__(self, facts: Mapping[str, object]):
        self._facts = facts

    def resolve(self, evidence_ref: str) -> EvidencePresence:
        digest = self._facts.get(evidence_ref)
        if digest is None:
            return EvidencePresence(evidence_ref, present=False, digest="")
        return EvidencePresence(evidence_ref, present=True, digest=str(digest))


class _StaticLifecyclePlanner:
    """CLI-only inert lifecycle planner; a refusal fact refuses the plan."""

    def __init__(self, facts: Mapping[str, object]):
        self._facts = facts

    def plan(self, sources: Sequence[LockedComponent]) -> LifecyclePlan:
        refusal_fact = self._facts.get("refusal")
        if isinstance(refusal_fact, dict):
            return LifecyclePlan(
                ok=False,
                stages=(),
                refusal=CompatibilityRefusal(
                    RefusalKind(str(refusal_fact["kind"])),
                    str(refusal_fact.get("subject_id", "lifecycle")),
                    str(refusal_fact.get("detail", "lifecycle refused")),
                ),
            )
        return LifecyclePlan(ok=True, stages=UPDATE_STAGES, refusal=None)


def _load_facts(path: Path | None) -> Mapping[str, object]:
    if path is None:
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _print_refusals(refusals: Sequence[CompatibilityRefusal]) -> None:
    for refusal in refusals:
        print(refusal.as_text(), file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    schemas = subparsers.add_parser("generate-schemas")
    schemas.add_argument("--repo-root", type=Path, default=Path.cwd())

    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--manifest", type=Path, required=True)
    resolve.add_argument("--repo-root", type=Path, default=Path.cwd())
    resolve.add_argument("--facts", type=Path, default=None)
    resolve.add_argument("--out", type=Path, default=None)

    verify = subparsers.add_parser("verify-lock")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--repo-root", type=Path, default=Path.cwd())
    verify.add_argument("--facts", type=Path, default=None)
    verify.add_argument("--lock", type=Path, required=True)

    plan = subparsers.add_parser("plan-update")
    plan.add_argument("--candidate", type=Path, required=True)
    plan.add_argument("--prior", type=Path, default=None)
    plan.add_argument("--facts", type=Path, default=None)
    return parser


def _facts_adapters(
    facts: Mapping[str, object]
) -> tuple[_StaticSourceResolver, _StaticEvidenceResolver]:
    sources = facts.get("sources", {})
    evidence = facts.get("evidence", {})
    return (
        _StaticSourceResolver(sources if isinstance(sources, dict) else {}),
        _StaticEvidenceResolver(evidence if isinstance(evidence, dict) else {}),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "generate-schemas":
            _atomic_write(
                args.repo_root / "schemas" / "assembly-manifest.schema.json",
                generate_assembly_manifest_schema(),
            )
            _atomic_write(
                args.repo_root / "schemas" / "assembly-lock.schema.json",
                generate_assembly_lock_schema(),
            )
            return 0

        if args.command == "resolve":
            records = load_component_records(args.repo_root / "components" / "records")
            manifest = parse_assembly_manifest(args.manifest)
            repo_resolver, evidence = _facts_adapters(_load_facts(args.facts))
            plan = resolve_assembly(manifest, records, repo_resolver, evidence)
            if plan.refusals:
                _print_refusals(plan.refusals)
                return 1
            lock_bytes = render_lock(plan)
            if args.out is not None:
                _atomic_write(args.out, lock_bytes)
            else:
                sys.stdout.buffer.write(lock_bytes)
            return 0

        if args.command == "verify-lock":
            records = load_component_records(args.repo_root / "components" / "records")
            manifest = parse_assembly_manifest(args.manifest)
            repo_resolver, evidence = _facts_adapters(_load_facts(args.facts))
            verify_generated_lock(
                manifest, args.lock.read_bytes(), records, repo_resolver, evidence
            )
            return 0

        if args.command == "plan-update":
            candidate = parse_assembly_lock(args.candidate.read_bytes())
            prior = (
                parse_assembly_lock(args.prior.read_bytes()) if args.prior is not None else None
            )
            lifecycle = _StaticLifecyclePlanner(_load_facts(args.facts))
            update = plan_assembly_update(prior, candidate, lifecycle)
            print(json.dumps({"stages": list(update.stages), "rollback": list(update.rollback)}))
            return 0
    except AssemblyResolverError as exc:
        print(f"assembly-resolver-invalid: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
