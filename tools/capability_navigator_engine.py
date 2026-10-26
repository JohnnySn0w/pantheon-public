"""Pure ORG-059 capability navigator engine (T-3219).

This module consumes the strict T-3218 navigator projection/request contract
plus T-3189 health/evidence data. It owns no provider adapter, mutation path,
repair loop, registration, runtime, or live authority.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import datetime as dt
from enum import Enum
import hashlib

from tools.capability_navigator_contract import (
    AuthenticatedContext,
    DelegationProposal,
    NavigatorBudget,
    NavigatorCapabilityProvider,
    NavigatorContractError,
    NavigatorErrorCode,
    NavigatorProjection,
    NavigatorRequest,
    NavigatorResult,
    SCHEMA_VERSION,
    SERVER_CEILINGS,
    canonical_json_bytes,
    parse_projection,
    parse_request,
    parse_result,
    projection_to_json,
    request_to_json,
    result_to_json,
)
from tools.organism_capability_health import (
    CapabilityHealth,
    CapabilityState,
    MutationPosture,
)
from tools.organism_topology import EvidenceSnapshot, FacetState, _evidence_state


class EngineRefusalCode(str, Enum):
    UNSUPPORTED_METHOD = "unsupported-method"
    NO_MATCH = "no-match"
    AMBIGUOUS_PROVIDER = "ambiguous-provider"
    SUBJECT_VISIBILITY_MISSING = "subject-visibility-missing"
    SUBJECT_VISIBILITY_MISMATCH = "subject-visibility-mismatch"
    DEPLOYMENT_TRUST_ZONE_MISSING = "deployment-trust-zone-missing"
    DEPLOYMENT_TRUST_ZONE_MISMATCH = "deployment-trust-zone-mismatch"
    MISSING_HEALTH_EVIDENCE = "missing-health-evidence"
    HEALTH_CAPABILITY_MISMATCH = "health-capability-mismatch"
    DEGRADED_PROVIDER = "degraded-provider"
    UNKNOWN_HEALTH = "unknown-health"
    STALE_EVIDENCE = "stale-evidence"
    EVIDENCE_DIGEST_MISMATCH = "evidence-digest-mismatch"
    EVIDENCE_RESULT_REFUSED = "evidence-result-refused"
    FALLBACK_SELECTION_AUTHORITY_UNAVAILABLE = (
        "fallback-selection-authority-unavailable"
    )
    MISSING_EFFECT_CONTRACT_AUTHORITY = "missing-effect-contract-authority"
    REVISION_MISMATCH = "revision-mismatch"
    BUDGET_BELOW_MINIMUM = "budget-below-minimum"
    BYTE_BOUND_EXCEEDED = "byte-bound-exceeded"
    METADATA_REFUSED = "metadata-refused"


@dataclass(frozen=True)
class EngineRefusal:
    schema_version: int
    code: EngineRefusalCode
    method: str
    request_id: str
    subject_ref: str
    registry_revision: str
    capability_id: str | None
    provider_component_id: str | None
    projection_sha256: str
    catalogue_sha256: str
    lock_sha256: str
    raw_lock_sha256: str
    request_digest: str
    typed_input_digest: str | None
    bounded_purpose_digest: str | None
    derived_snapshot_digest: str | None
    provider_evidence_refs: tuple[str, ...]
    visibility_policy_refs: tuple[str, ...]
    visibility_evidence_refs: tuple[str, ...]
    health_disclosures: tuple[str, ...]
    created_at: str


EngineOutcome = NavigatorResult | EngineRefusal

# T-3222 (ORG-059 Child E): at an authority-unavailable refusal boundary the
# engine may instead emit a typed DelegationProposal — a NEW outcome branch, not
# a rewrite. The proposal is built by an injected emitter (tools/delegation_route
# .make_delegation_emitter) so the engine stays pure (contract-only deps, no
# adapter/transport). Absent an emitter the behaviour is byte-for-byte unchanged.
DelegationEmitter = Callable[..., DelegationProposal]
DelegationEngineOutcome = EngineOutcome | DelegationProposal

_METADATA_REFUSED_CODES = frozenset(
    {
        NavigatorErrorCode.OVERSIZED_METADATA,
        NavigatorErrorCode.BINARY_OR_NON_UTF8_METADATA,
        NavigatorErrorCode.HOSTILE_CONTROL_METADATA,
    }
)
_REVISION_REFUSED_CODES = frozenset(
    {
        NavigatorErrorCode.UNSUPPORTED_VERSION,
        NavigatorErrorCode.PROJECTION_DIGEST_MISMATCH,
    }
)


@dataclass(frozen=True)
class _ValidatedProvider:
    provider: NavigatorCapabilityProvider
    provider_json: Mapping[str, object]
    evidence_refs: tuple[str, ...]
    visibility_policy_refs: tuple[str, ...]
    visibility_evidence_refs: tuple[str, ...]
    derived_snapshot_digest: str


def handle_request(
    auth: AuthenticatedContext,
    request: NavigatorRequest,
    projection: NavigatorProjection,
    health: Mapping[str, CapabilityHealth],
    evidence: EvidenceSnapshot,
    evaluated_at: dt.datetime,
    *,
    delegation_emitter: DelegationEmitter | None = None,
) -> DelegationEngineOutcome:
    """Handle one already-parsed navigator request with fail-closed bounds.

    With `delegation_emitter` supplied (T-3222, ORG-059 Child E), an
    authority-unavailable refusal for which the catalogue declares an eligible
    OWNING role the subject does not hold is instead returned as a typed
    DelegationProposal. Without it, behaviour is unchanged.
    """
    if not isinstance(auth, AuthenticatedContext):
        raise TypeError("auth must be AuthenticatedContext")

    request = parse_request(request_to_json(request), auth=auth)
    try:
        projection = parse_projection(projection_to_json(projection))
    except NavigatorContractError as exc:
        if exc.code in _METADATA_REFUSED_CODES:
            code = EngineRefusalCode.METADATA_REFUSED
        elif exc.code in _REVISION_REFUSED_CODES:
            code = EngineRefusalCode.REVISION_MISMATCH
        else:
            raise
        return _refusal(
            code,
            auth=auth,
            request=request,
            projection=projection,
            evaluated_at=evaluated_at if evaluated_at.tzinfo is not None else None,
        )

    if evaluated_at.tzinfo is None:
        return _refusal(
            EngineRefusalCode.STALE_EVIDENCE,
            auth=auth,
            request=request,
            projection=projection,
            evaluated_at=None,
        )

    if _fallback_attempted(request):
        return _authority_unavailable(
            EngineRefusalCode.FALLBACK_SELECTION_AUTHORITY_UNAVAILABLE,
            auth=auth,
            request=request,
            projection=projection,
            evaluated_at=evaluated_at,
            capability_id=_request_capability_id(request),
            delegation_emitter=delegation_emitter,
        )

    if request.method == "plan":
        return _authority_unavailable(
            EngineRefusalCode.MISSING_EFFECT_CONTRACT_AUTHORITY,
            auth=auth,
            request=request,
            projection=projection,
            evaluated_at=evaluated_at,
            capability_id=_request_capability_id(request),
            delegation_emitter=delegation_emitter,
        )
    if request.method in ("invoke", "status", "cancel"):
        return _refusal(
            EngineRefusalCode.UNSUPPORTED_METHOD,
            auth=auth,
            request=request,
            projection=projection,
            evaluated_at=evaluated_at,
        )
    if request.method == "search":
        return _handle_search(auth, request, projection, health, evidence, evaluated_at)
    if request.method == "describe":
        return _handle_describe(auth, request, projection, health, evidence, evaluated_at)
    return _refusal(
        EngineRefusalCode.UNSUPPORTED_METHOD,
        auth=auth,
        request=request,
        projection=projection,
        evaluated_at=evaluated_at,
    )


def _handle_search(
    auth: AuthenticatedContext,
    request: NavigatorRequest,
    projection: NavigatorProjection,
    health: Mapping[str, CapabilityHealth],
    evidence: EvidenceSnapshot,
    evaluated_at: dt.datetime,
) -> EngineOutcome:
    first_refusal: EngineRefusal | None = None
    visible: list[NavigatorCapabilityProvider] = []
    for provider in projection.capabilities:
        if not _matches_search(request, provider):
            continue
        visibility = _validate_visibility(auth, request, projection, provider, evaluated_at)
        if isinstance(visibility, EngineRefusal):
            if first_refusal is None:
                first_refusal = visibility
            continue
        visible.append(provider)

    if not visible:
        return first_refusal or _refusal(
            EngineRefusalCode.NO_MATCH,
            auth=auth,
            request=request,
            projection=projection,
            evaluated_at=evaluated_at,
        )
    if _has_capability_ambiguity(visible):
        return _refusal(
            EngineRefusalCode.AMBIGUOUS_PROVIDER,
            auth=auth,
            request=request,
            projection=projection,
            evaluated_at=evaluated_at,
        )

    provider_json = _provider_json_by_key(projection)
    eligible: list[_ValidatedProvider] = []
    for provider in visible:
        validation = _validate_provider(
            auth, request, projection, provider, health, evidence, evaluated_at, provider_json
        )
        if isinstance(validation, EngineRefusal):
            return validation
        eligible.append(validation)

    ordered = sorted(eligible, key=lambda item: _provider_sort_key(item.provider))
    max_candidates = request.context_budget.max_candidates
    truncated = len(ordered) > max_candidates
    candidates = []
    evidence_refs: list[str] = []
    for selected in ordered[:max_candidates]:
        provider = selected.provider
        refs = _success_refs(selected)
        evidence_refs.extend(refs)
        match_refs = _match_refs(provider) + selected.visibility_policy_refs + selected.visibility_evidence_refs
        bounded_match_refs = _require_refs_within_budget(match_refs, request.context_budget)
        if bounded_match_refs is None:
            return _refusal(
                EngineRefusalCode.BYTE_BOUND_EXCEEDED,
                auth=auth,
                request=request,
                projection=projection,
                evaluated_at=evaluated_at,
                capability_id=provider.capability_id,
                provider_component_id=provider.provider_component_id,
            )
        summary = _summary(provider)
        if len(summary.encode("utf-8")) > request.context_budget.max_summary_bytes:
            return _refusal(
                EngineRefusalCode.BYTE_BOUND_EXCEEDED,
                auth=auth,
                request=request,
                projection=projection,
                evaluated_at=evaluated_at,
                capability_id=provider.capability_id,
                provider_component_id=provider.provider_component_id,
            )
        candidates.append(
            {
                "capability_id": provider.capability_id,
                "provider_component_id": provider.provider_component_id,
                "contract": provider.contract,
                "schema": provider.schema,
                "version": provider.version,
                "redacted_summary": summary,
                "match_refs": bounded_match_refs,
                "projection_ref": _provider_digest(selected, auth),
            }
        )
    bounded_evidence_refs = _require_refs_within_budget(evidence_refs, request.context_budget)
    if bounded_evidence_refs is None:
        return _refusal(
            EngineRefusalCode.BYTE_BOUND_EXCEEDED,
            auth=auth,
            request=request,
            projection=projection,
            evaluated_at=evaluated_at,
        )
    result = NavigatorResult(
        schema_version=SCHEMA_VERSION,
        method="search",
        request_id=request.request_id,
        status="ok",
        subject_ref=auth.subject_ref,
        projection_sha256=projection.projection_sha256,
        catalogue_sha256=projection.catalogue_sha256,
        lock_sha256=projection.lock_sha256,
        raw_lock_sha256=projection.raw_lock_sha256,
        created_at=_created_at(evaluated_at),
        payload={"candidates": candidates, "truncated": truncated},
        provider_evidence_refs=bounded_evidence_refs,
    )
    return _validated_result_or_refusal(result, auth, request, projection, evaluated_at)


def _handle_describe(
    auth: AuthenticatedContext,
    request: NavigatorRequest,
    projection: NavigatorProjection,
    health: Mapping[str, CapabilityHealth],
    evidence: EvidenceSnapshot,
    evaluated_at: dt.datetime,
) -> EngineOutcome:
    capability_id = _request_capability_id(request)
    matches = [
        provider for provider in projection.capabilities if provider.capability_id == capability_id
    ]
    if not matches:
        return _refusal(
            EngineRefusalCode.NO_MATCH,
            auth=auth,
            request=request,
            projection=projection,
            evaluated_at=evaluated_at,
            capability_id=capability_id,
        )
    eligible: list[tuple[NavigatorCapabilityProvider, tuple[str, ...]]] = []
    visible: list[NavigatorCapabilityProvider] = []
    first_refusal: EngineRefusal | None = None
    for provider in matches:
        visibility = _validate_visibility(auth, request, projection, provider, evaluated_at)
        if isinstance(visibility, EngineRefusal):
            if first_refusal is None:
                first_refusal = visibility
            continue
        visible.append(provider)
    if not visible:
        return first_refusal or _refusal(
            EngineRefusalCode.NO_MATCH,
            auth=auth,
            request=request,
            projection=projection,
            evaluated_at=evaluated_at,
            capability_id=capability_id,
        )
    if len(visible) > 1:
        return _refusal(
            EngineRefusalCode.AMBIGUOUS_PROVIDER,
            auth=auth,
            request=request,
            projection=projection,
            evaluated_at=evaluated_at,
            capability_id=capability_id,
        )
    provider_json = _provider_json_by_key(projection)
    validation = _validate_provider(
        auth, request, projection, visible[0], health, evidence, evaluated_at, provider_json
    )
    if isinstance(validation, EngineRefusal):
        return validation
    selected = validation
    provider = selected.provider
    refs = _success_refs(selected)
    preimage = _contract_slice_preimage(selected, auth)
    contract_slice = {
        "redacted_summary": _summary(provider),
        "contract_digest": _digest_json(preimage),
        "evidence_refs": list(refs),
    }
    if len(canonical_json_bytes(contract_slice)) > request.context_budget.max_contract_bytes:
        return _refusal(
            EngineRefusalCode.BYTE_BOUND_EXCEEDED,
            auth=auth,
            request=request,
            projection=projection,
            evaluated_at=evaluated_at,
            capability_id=capability_id,
            provider_component_id=provider.provider_component_id,
            provider_evidence_refs=refs,
            visibility_policy_refs=selected.visibility_policy_refs,
            visibility_evidence_refs=selected.visibility_evidence_refs,
            derived_snapshot_digest=selected.derived_snapshot_digest,
        )
    bounded_refs = _require_refs_within_budget(refs, request.context_budget)
    if bounded_refs is None:
        return _refusal(
            EngineRefusalCode.BYTE_BOUND_EXCEEDED,
            auth=auth,
            request=request,
            projection=projection,
            evaluated_at=evaluated_at,
            capability_id=capability_id,
            provider_component_id=provider.provider_component_id,
        )
    summary = _summary(provider)
    if len(summary.encode("utf-8")) > request.context_budget.max_summary_bytes:
        return _refusal(
            EngineRefusalCode.BYTE_BOUND_EXCEEDED,
            auth=auth,
            request=request,
            projection=projection,
            evaluated_at=evaluated_at,
            capability_id=capability_id,
            provider_component_id=provider.provider_component_id,
        )
    result = NavigatorResult(
        schema_version=SCHEMA_VERSION,
        method="describe",
        request_id=request.request_id,
        status="ok",
        subject_ref=auth.subject_ref,
        projection_sha256=projection.projection_sha256,
        catalogue_sha256=projection.catalogue_sha256,
        lock_sha256=projection.lock_sha256,
        raw_lock_sha256=projection.raw_lock_sha256,
        created_at=_created_at(evaluated_at),
        provider_evidence_refs=bounded_refs,
        payload={
            "capability_id": provider.capability_id,
            "provider_component_id": provider.provider_component_id,
            "contract": provider.contract,
            "schema": provider.schema,
            "version": provider.version,
            "detail_level": str(request.payload["requested_detail"]),
            "contract_slice": {
                "redacted_summary": summary,
                "contract_digest": contract_slice["contract_digest"],
                "evidence_refs": bounded_refs,
            },
        },
    )
    return _validated_result_or_refusal(result, auth, request, projection, evaluated_at)


def _validate_provider(
    auth: AuthenticatedContext,
    request: NavigatorRequest,
    projection: NavigatorProjection,
    provider: NavigatorCapabilityProvider,
    health: Mapping[str, CapabilityHealth],
    evidence: EvidenceSnapshot,
    evaluated_at: dt.datetime,
    provider_json: Mapping[tuple[str, str, str, str, str], Mapping[str, object]],
) -> _ValidatedProvider | EngineRefusal:
    visibility = _validate_visibility(auth, request, projection, provider, evaluated_at)
    if isinstance(visibility, EngineRefusal):
        return visibility
    provider_key = _provider_sort_key(provider)
    exact_provider_json = provider_json.get(provider_key)
    if exact_provider_json is None:
        return _provider_refusal(
            EngineRefusalCode.REVISION_MISMATCH,
            auth, request, projection, provider, evaluated_at
        )
    visibility_policy_refs = visibility[0]
    visibility_evidence_refs = visibility[1]
    selected_records = []
    if provider.deployment_trust_zone is None:
        return _provider_refusal(
            EngineRefusalCode.DEPLOYMENT_TRUST_ZONE_MISSING,
            auth, request, projection, provider, evaluated_at
        )
    if provider.deployment_trust_zone != auth.trust_zone:
        return _provider_refusal(
            EngineRefusalCode.DEPLOYMENT_TRUST_ZONE_MISMATCH,
            auth, request, projection, provider, evaluated_at
        )
    aggregate = health.get(provider.capability_id)
    if aggregate is None:
        return _provider_refusal(
            EngineRefusalCode.MISSING_HEALTH_EVIDENCE,
            auth, request, projection, provider, evaluated_at
        )
    if aggregate.capability_id != provider.capability_id:
        return _provider_refusal(
            EngineRefusalCode.HEALTH_CAPABILITY_MISMATCH,
            auth, request, projection, provider, evaluated_at,
            health_disclosures=aggregate.disclosures,
        )
    if aggregate.state is CapabilityState.UNKNOWN:
        return _provider_refusal(
            EngineRefusalCode.UNKNOWN_HEALTH,
            auth, request, projection, provider, evaluated_at,
            health_disclosures=aggregate.disclosures,
        )
    if (
        aggregate.state is not CapabilityState.HEALTHY
        or aggregate.mutation_posture is not MutationPosture.PERMITTED
        or aggregate.disclosures
    ):
        return _provider_refusal(
            EngineRefusalCode.DEGRADED_PROVIDER,
            auth, request, projection, provider, evaluated_at,
            health_disclosures=aggregate.disclosures,
        )
    if not provider.upkeep.health_evidence_refs:
        return _provider_refusal(
            EngineRefusalCode.MISSING_HEALTH_EVIDENCE,
            auth, request, projection, provider, evaluated_at,
        )

    selected_refs: list[str] = []
    for ref in provider.upkeep.health_evidence_refs:
        record = evidence.records.get(ref)
        if record is None:
            return _provider_refusal(
                EngineRefusalCode.MISSING_HEALTH_EVIDENCE,
                auth, request, projection, provider, evaluated_at,
            )
        if evidence.canonical_ref_sha256.get(record.canonical_ref) != record.sha256:
            return _provider_refusal(
                EngineRefusalCode.EVIDENCE_DIGEST_MISMATCH,
                auth, request, projection, provider, evaluated_at,
                provider_evidence_refs=tuple(selected_refs + [ref]),
            )
        state = _evidence_state(record, evidence, evaluated_at)
        if state is FacetState.STALE:
            return _provider_refusal(
                EngineRefusalCode.STALE_EVIDENCE,
                auth, request, projection, provider, evaluated_at,
                provider_evidence_refs=tuple(selected_refs + [ref]),
            )
        if state is not FacetState.HEALTHY:
            return _provider_refusal(
                EngineRefusalCode.EVIDENCE_RESULT_REFUSED,
                auth, request, projection, provider, evaluated_at,
                provider_evidence_refs=tuple(selected_refs + [ref]),
            )
        selected_refs.append(ref)
        selected_records.append(record)
    return _ValidatedProvider(
        provider=provider,
        provider_json=exact_provider_json,
        evidence_refs=tuple(selected_refs),
        visibility_policy_refs=visibility_policy_refs,
        visibility_evidence_refs=visibility_evidence_refs,
        derived_snapshot_digest=_derived_snapshot_digest(
            aggregate, selected_records, evidence
        ),
    )


def _validate_visibility(
    auth: AuthenticatedContext,
    request: NavigatorRequest,
    projection: NavigatorProjection,
    provider: NavigatorCapabilityProvider,
    evaluated_at: dt.datetime,
) -> tuple[tuple[str, ...], tuple[str, ...]] | EngineRefusal:
    if provider.deployment_trust_zone is None:
        return _provider_refusal(
            EngineRefusalCode.DEPLOYMENT_TRUST_ZONE_MISSING,
            auth, request, projection, provider, evaluated_at
        )
    if provider.deployment_trust_zone != auth.trust_zone:
        return _provider_refusal(
            EngineRefusalCode.DEPLOYMENT_TRUST_ZONE_MISMATCH,
            auth, request, projection, provider, evaluated_at
        )
    visibility = provider.subject_visibility
    if visibility is None:
        return _provider_refusal(
            EngineRefusalCode.SUBJECT_VISIBILITY_MISSING,
            auth, request, projection, provider, evaluated_at
        )
    if not _visibility_matches(auth, provider):
        return _provider_refusal(
            EngineRefusalCode.SUBJECT_VISIBILITY_MISMATCH,
            auth, request, projection, provider, evaluated_at
        )
    return visibility.policy_refs, visibility.evidence_refs


def _visibility_matches(
    auth: AuthenticatedContext, provider: NavigatorCapabilityProvider
) -> bool:
    visibility = provider.subject_visibility
    if visibility is None:
        return False
    if visibility.subject_refs and auth.subject_ref not in visibility.subject_refs:
        return False
    if visibility.role_refs and not set(auth.role_refs).intersection(visibility.role_refs):
        return False
    if visibility.instance_refs and auth.instance_ref not in visibility.instance_refs:
        return False
    if visibility.trust_zones and auth.trust_zone not in visibility.trust_zones:
        return False
    return True


def _matches_search(request: NavigatorRequest, provider: NavigatorCapabilityProvider) -> bool:
    payload = request.payload
    constraints = payload["constraints"]  # already reparsed by T-3218
    if not isinstance(constraints, Mapping):
        return False
    typed_filters = (
        ("capability_ids", provider.capability_id),
        ("provider_component_ids", provider.provider_component_id),
        ("contracts", provider.contract),
        ("schemas", provider.schema),
        ("versions", provider.version),
    )
    for key, value in typed_filters:
        requested = tuple(constraints[key])  # type: ignore[index]
        if requested and value not in requested:
            return False
    features = tuple(constraints["features"])  # type: ignore[index]
    if features and not set(features).issubset(provider.enabled_features):
        return False
    scopes = tuple(constraints["implication_scopes"])  # type: ignore[index]
    if scopes and not set(scopes).intersection(item.scope for item in provider.implications):
        return False
    text = str(payload["query"]["text"]).lower()  # type: ignore[index]
    if not text:
        return True
    haystack = " ".join(_match_refs(provider)).lower()
    return text in haystack


def _has_capability_ambiguity(
    providers: Sequence[NavigatorCapabilityProvider],
) -> bool:
    seen: set[str] = set()
    for provider in providers:
        key = provider.capability_id
        if key in seen:
            return True
        seen.add(key)
    return False


def _fallback_attempted(request: NavigatorRequest) -> bool:
    if request.method != "search":
        return False
    constraints = request.payload["constraints"]
    if not isinstance(constraints, Mapping):
        return False
    return bool(
        constraints["equivalence_classes"]
        or constraints["trust_classes"]
        or constraints["require_declared_equivalence"]
    )


def _provider_refusal(
    code: EngineRefusalCode,
    auth: AuthenticatedContext,
    request: NavigatorRequest,
    projection: NavigatorProjection,
    provider: NavigatorCapabilityProvider,
    evaluated_at: dt.datetime,
    *,
    provider_evidence_refs: Sequence[str] | None = None,
    health_disclosures: Sequence[str] = (),
    derived_snapshot_digest: str | None = None,
) -> EngineRefusal:
    visibility = provider.subject_visibility
    return _refusal(
        code,
        auth=auth,
        request=request,
        projection=projection,
        evaluated_at=evaluated_at,
        capability_id=provider.capability_id,
        provider_component_id=provider.provider_component_id,
        provider_evidence_refs=tuple(
            provider.upkeep.health_evidence_refs
            if provider_evidence_refs is None
            else provider_evidence_refs
        ),
        visibility_policy_refs=()
        if visibility is None
        else visibility.policy_refs,
        visibility_evidence_refs=()
        if visibility is None
        else visibility.evidence_refs,
        health_disclosures=tuple(health_disclosures),
        derived_snapshot_digest=derived_snapshot_digest,
    )


def _refusal(
    code: EngineRefusalCode,
    *,
    auth: AuthenticatedContext,
    request: NavigatorRequest,
    projection: NavigatorProjection,
    evaluated_at: dt.datetime | None,
    capability_id: str | None = None,
    provider_component_id: str | None = None,
    provider_evidence_refs: Sequence[str] = (),
    visibility_policy_refs: Sequence[str] = (),
    visibility_evidence_refs: Sequence[str] = (),
    health_disclosures: Sequence[str] = (),
    derived_snapshot_digest: str | None = None,
) -> EngineRefusal:
    provider_refs = _require_refs_within_budget(provider_evidence_refs, request.context_budget)
    policy_refs = _require_refs_within_budget(visibility_policy_refs, request.context_budget)
    visibility_refs = _require_refs_within_budget(visibility_evidence_refs, request.context_budget)
    overflow = provider_refs is None or policy_refs is None or visibility_refs is None
    refusal = EngineRefusal(
        schema_version=SCHEMA_VERSION,
        code=EngineRefusalCode.BYTE_BOUND_EXCEEDED if overflow else code,
        method=request.method,
        request_id=request.request_id,
        subject_ref=auth.subject_ref,
        registry_revision=auth.registry_revision,
        capability_id=capability_id,
        provider_component_id=provider_component_id,
        projection_sha256=projection.projection_sha256,
        catalogue_sha256=projection.catalogue_sha256,
        lock_sha256=projection.lock_sha256,
        raw_lock_sha256=projection.raw_lock_sha256,
        request_digest=_digest_json(request_to_json(request)),
        typed_input_digest=_typed_input_digest(request),
        bounded_purpose_digest=_bounded_purpose_digest(request),
        derived_snapshot_digest=None if overflow else derived_snapshot_digest,
        provider_evidence_refs=() if overflow else provider_refs,
        visibility_policy_refs=() if overflow else policy_refs,
        visibility_evidence_refs=() if overflow else visibility_refs,
        health_disclosures=tuple(sorted(health_disclosures)),
        created_at="" if evaluated_at is None else _created_at(evaluated_at),
    )
    if len(canonical_json_bytes(_refusal_to_json(refusal))) > request.context_budget.max_total_bytes:
        return _minimal_byte_bound_refusal(refusal)
    if len(canonical_json_bytes(_refusal_to_json(refusal))) > SERVER_CEILINGS.max_total_bytes:
        return _minimal_byte_bound_refusal(refusal)
    return refusal


def _capability_owning_roles(
    projection: NavigatorProjection, capability_id: str | None
) -> frozenset[str]:
    """The REAL set of roles the catalogue declares (via subject_visibility) for
    `capability_id` — the namespace the delegation validator checks against, so
    the check is never a vacuous self-singleton."""
    if capability_id is None:
        return frozenset()
    roles: set[str] = set()
    for provider in projection.capabilities:
        if provider.capability_id != capability_id:
            continue
        visibility = provider.subject_visibility
        if visibility is None:
            continue
        roles.update(visibility.role_refs)
    return frozenset(roles)


def _eligible_owning_role(
    projection: NavigatorProjection,
    auth: AuthenticatedContext,
    capability_id: str | None,
) -> str | None:
    """The lowest (deterministic) catalogue-declared owning role for
    `capability_id` that the current subject does NOT already hold, or None.

    A local mirror of delegation_route.eligible_owning_role_for kept here so the
    engine stays pure (contract-only deps — it must not import the adapter's
    transport layer). Cross-ref: tools/delegation_route.py (T-3222)."""
    if capability_id is None:
        return None
    held = set(auth.role_refs)
    candidates = {role for role in _capability_owning_roles(projection, capability_id) if role not in held}
    if not candidates:
        return None
    return sorted(candidates)[0]


def _authority_unavailable(
    code: EngineRefusalCode,
    *,
    auth: AuthenticatedContext,
    request: NavigatorRequest,
    projection: NavigatorProjection,
    evaluated_at: dt.datetime,
    capability_id: str | None,
    delegation_emitter: DelegationEmitter | None,
) -> DelegationEngineOutcome:
    """Return the bare authority-unavailable refusal, OR — when an emitter is
    supplied and the catalogue declares an eligible OWNING role the subject
    lacks — a typed DelegationProposal instead. The subject never inherits that
    role's authority here; the proposal is the round-trip's request, not a
    grant."""
    if delegation_emitter is not None:
        role = _eligible_owning_role(projection, auth, capability_id)
        if role is not None and capability_id is not None:
            intent = (
                request.payload.get("bounded_purpose")
                if request.method == "plan"
                else f"fallback-selection:{capability_id}"
            )
            if isinstance(intent, str) and intent:
                # Validate against the REAL catalogue role namespace (not a
                # self-singleton). If the catalogue's only owning target is
                # itself invalid (e.g. a persona seat or unknown role), the
                # emitter refuses; we fail closed to the bare authority refusal
                # rather than emit an invalid proposal or crash the navigator.
                try:
                    return delegation_emitter(
                        auth=auth,
                        capability_id=capability_id,
                        eligible_owning_role=role,
                        intent=intent,
                        trace_id=request.request_id,
                        known_role_ids=_capability_owning_roles(projection, capability_id),
                        contract_revision=auth.registry_revision,
                        constraints_ref=None,
                    )
                except NavigatorContractError:
                    pass
    return _refusal(
        code,
        auth=auth,
        request=request,
        projection=projection,
        evaluated_at=evaluated_at,
        capability_id=capability_id,
    )


def _refusal_to_json(refusal: EngineRefusal) -> dict[str, object]:
    if _is_minimal_byte_bound_refusal(refusal):
        return {
            "schema_version": refusal.schema_version,
            "code": refusal.code.value,
            "method": refusal.method,
            "request_digest": refusal.request_digest,
            "created_at": refusal.created_at,
        }
    return {
        "schema_version": refusal.schema_version,
        "code": refusal.code.value,
        "method": refusal.method,
        "request_id": refusal.request_id,
        "subject_ref": refusal.subject_ref,
        "registry_revision": refusal.registry_revision,
        "capability_id": refusal.capability_id,
        "provider_component_id": refusal.provider_component_id,
        "projection_sha256": refusal.projection_sha256,
        "catalogue_sha256": refusal.catalogue_sha256,
        "lock_sha256": refusal.lock_sha256,
        "raw_lock_sha256": refusal.raw_lock_sha256,
        "request_digest": refusal.request_digest,
        "typed_input_digest": refusal.typed_input_digest,
        "bounded_purpose_digest": refusal.bounded_purpose_digest,
        "derived_snapshot_digest": refusal.derived_snapshot_digest,
        "provider_evidence_refs": list(refusal.provider_evidence_refs),
        "visibility_policy_refs": list(refusal.visibility_policy_refs),
        "visibility_evidence_refs": list(refusal.visibility_evidence_refs),
        "health_disclosures": list(refusal.health_disclosures),
        "created_at": refusal.created_at,
    }


def _validated_result_or_refusal(
    result: NavigatorResult,
    auth: AuthenticatedContext,
    request: NavigatorRequest,
    projection: NavigatorProjection,
    evaluated_at: dt.datetime,
) -> EngineOutcome:
    if len(canonical_json_bytes(result_to_json(result))) > request.context_budget.max_total_bytes:
        return _refusal(
            EngineRefusalCode.BYTE_BOUND_EXCEEDED,
            auth=auth,
            request=request,
            projection=projection,
            evaluated_at=evaluated_at,
        )
    try:
        return parse_result(result_to_json(result))
    except NavigatorContractError as exc:
        code = (
            EngineRefusalCode.BYTE_BOUND_EXCEEDED
            if exc.code is NavigatorErrorCode.BYTE_BOUND_EXCEEDED
            else EngineRefusalCode.METADATA_REFUSED
        )
        return _refusal(
            code,
            auth=auth,
            request=request,
            projection=projection,
            evaluated_at=evaluated_at,
        )


def _require_refs_within_budget(
    refs: Sequence[str], budget: NavigatorBudget
) -> tuple[str, ...] | None:
    limit = min(budget.max_provider_evidence_refs, SERVER_CEILINGS.max_provider_evidence_refs)
    unique = tuple(dict.fromkeys(refs))
    if len(unique) > limit:
        return None
    return unique


def _success_refs(selected: _ValidatedProvider) -> tuple[str, ...]:
    return (
        selected.evidence_refs
        + selected.visibility_policy_refs
        + selected.visibility_evidence_refs
    )


def _minimal_byte_bound_refusal(refusal: EngineRefusal) -> EngineRefusal:
    return EngineRefusal(
        **{
            **refusal.__dict__,
            "code": EngineRefusalCode.BYTE_BOUND_EXCEEDED,
            "catalogue_sha256": "",
            "lock_sha256": "",
            "raw_lock_sha256": "",
            "derived_snapshot_digest": None,
            "provider_evidence_refs": (),
            "visibility_policy_refs": (),
            "visibility_evidence_refs": (),
            "health_disclosures": (),
        }
    )


def _is_minimal_byte_bound_refusal(refusal: EngineRefusal) -> bool:
    return (
        refusal.code is EngineRefusalCode.BYTE_BOUND_EXCEEDED
        and not refusal.catalogue_sha256
        and not refusal.lock_sha256
        and not refusal.raw_lock_sha256
    )


def _request_capability_id(request: NavigatorRequest) -> str | None:
    value = request.payload.get("capability_id")
    return value if isinstance(value, str) else None


def _typed_input_digest(request: NavigatorRequest) -> str | None:
    if request.method != "plan":
        return None
    return _digest_json(request.payload["typed_input"])


def _bounded_purpose_digest(request: NavigatorRequest) -> str | None:
    if request.method != "plan":
        return None
    return _digest_json({"bounded_purpose": request.payload["bounded_purpose"]})


def _created_at(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _digest_json(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _provider_sort_key(
    provider: NavigatorCapabilityProvider,
) -> tuple[str, str, str, str, str]:
    return (
        provider.capability_id,
        provider.provider_component_id,
        provider.contract,
        provider.schema,
        provider.version,
    )


def _summary(provider: NavigatorCapabilityProvider) -> str:
    return (
        f"{provider.capability_id} via {provider.provider_component_id} "
        f"{provider.contract}/{provider.schema}/{provider.version}"
    )


def _match_refs(provider: NavigatorCapabilityProvider) -> tuple[str, ...]:
    return (
        provider.capability_id,
        provider.provider_component_id,
        provider.contract,
        provider.schema,
        provider.version,
        *provider.upkeep.health_evidence_refs,
    )


def _provider_json_by_key(
    projection: NavigatorProjection,
) -> dict[tuple[str, str, str, str, str], Mapping[str, object]]:
    result: dict[tuple[str, str, str, str, str], Mapping[str, object]] = {}
    for raw in projection_to_json(projection)["capabilities"]:  # type: ignore[index]
        if not isinstance(raw, Mapping):
            continue
        key = (
            str(raw["capability_id"]),
            str(raw["provider_component_id"]),
            str(raw["contract"]),
            str(raw["schema"]),
            str(raw["version"]),
        )
        result[key] = raw
    return result


def _provider_digest(selected: _ValidatedProvider, auth: AuthenticatedContext) -> str:
    return _digest_json(
        {
            "provider": selected.provider_json,
            "registry_revision": auth.registry_revision,
            "derived_snapshot_digest": selected.derived_snapshot_digest,
        }
    )


def _contract_slice_preimage(
    selected: _ValidatedProvider, auth: AuthenticatedContext
) -> dict[str, object]:
    return {
        "provider": selected.provider_json,
        "registry_revision": auth.registry_revision,
        "derived_snapshot_digest": selected.derived_snapshot_digest,
        "selected_refs": list(_success_refs(selected)),
    }


def _derived_snapshot_digest(
    health: CapabilityHealth,
    records: Sequence[object],
    evidence: EvidenceSnapshot,
) -> str:
    return _digest_json(
        {
            "health": _health_json(health),
            "evidence": [_evidence_ref_json(record, evidence) for record in records],
        }
    )


def _health_json(health: CapabilityHealth) -> dict[str, object]:
    return {
        "capability_id": health.capability_id,
        "state": health.state.value,
        "mutation_posture": health.mutation_posture.value,
        "contributors": [
            {
                "organ_id": item.organ_id,
                "relationship": item.relationship.value,
                "organ_state": item.organ_state.value,
                "disclosed": item.disclosed,
                "provenance": item.provenance,
            }
            for item in health.contributors
        ],
        "disclosures": list(health.disclosures),
    }


def _evidence_ref_json(record: object, evidence: EvidenceSnapshot) -> dict[str, object]:
    return {
        "schema_version": record.schema_version,  # type: ignore[attr-defined]
        "evidence_id": record.evidence_id,  # type: ignore[attr-defined]
        "subject_ref": {
            "kind": record.subject_ref.kind.value,  # type: ignore[attr-defined]
            "id": record.subject_ref.id,  # type: ignore[attr-defined]
        },
        "facet": record.facet.value,  # type: ignore[attr-defined]
        "evidence_kind": record.evidence_kind.value,  # type: ignore[attr-defined]
        "canonical_ref": record.canonical_ref,  # type: ignore[attr-defined]
        "sha256": record.sha256,  # type: ignore[attr-defined]
        "canonical_ref_sha256": evidence.canonical_ref_sha256[record.canonical_ref],  # type: ignore[attr-defined]
        "observer_ref": record.observer_ref,  # type: ignore[attr-defined]
        "observed_at": record.observed_at,  # type: ignore[attr-defined]
        "fresh_until": record.fresh_until,  # type: ignore[attr-defined]
        "result": record.result.value,  # type: ignore[attr-defined]
    }


__all__ = [
    "DelegationEmitter",
    "DelegationEngineOutcome",
    "EngineOutcome",
    "EngineRefusal",
    "EngineRefusalCode",
    "handle_request",
]
