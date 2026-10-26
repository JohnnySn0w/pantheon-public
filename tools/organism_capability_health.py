"""tools/organism_capability_health.py — ORG-056 Unit E (T-3189).

Pure, read-only capability-path health and degradation projection plus a report
adapter over existing ORG-076 fleet-health evidence.

This module owns NO detector, timer, repair, recovery, selection, or live-mutation
authority. It projects health from the canonical topology
(:mod:`tools.organism_topology`) and a caller-supplied
:class:`~tools.organism_topology.EvidenceSnapshot`. Actuation and repair remain
with ORG-076; this module only reads and reports.

Design and falsifiers: ``tickets/plans/T-2416-implementation-plan.md`` (Unit E,
"Capability-path evaluation" and the "Health and dependency propagation"
acceptance matrix).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
from collections.abc import Mapping, Sequence
from enum import Enum

from tools.organism_topology import (
    DependencyRelationship,
    EvidenceSnapshot,
    FacetState,
    HealthFacet,
    OrganBinding,
    OrganContract,
    OrganDependency,
    OrganHealth,
    OrganismTopology,
    evaluate_organ_health,
    parse_evidence_ref,
)


class CapabilityState(str, Enum):
    """Evidence-derived state of a named capability path (plan §Health)."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    PARTITIONED = "partitioned"
    INCONSISTENT = "inconsistent"
    UNKNOWN = "unknown"


class MutationPosture(str, Enum):
    """Whether a capability path may authorize a mutation right now."""

    PERMITTED = "permitted"
    FAIL_CLOSED = "fail_closed"
    PROVISIONAL_READONLY = "provisional_readonly"


@dataclasses.dataclass(frozen=True)
class Contributor:
    """One organ's contribution to a capability path, with disclosure metadata."""

    organ_id: str
    relationship: DependencyRelationship
    organ_state: CapabilityState
    disclosed: bool
    provenance: str


@dataclasses.dataclass(frozen=True)
class CapabilityHealth:
    """Projected health of one named capability path.

    ``state`` is the evidence-derived capability state; ``mutation_posture`` is
    whether a mutation may be authorized; ``contributors`` retains every provider
    organ's identity, relationship, and state; ``disclosures`` names the subset
    that must be surfaced (unavailable / provisional / stale / partitioned /
    inconsistent). Synthesis may explain, never silently choose or invent
    consensus.
    """

    capability_id: str
    state: CapabilityState
    mutation_posture: MutationPosture
    contributors: tuple[Contributor, ...]
    disclosures: tuple[str, ...]


class CapabilityHealthError(ValueError):
    """A capability projection could not be formed from the given inputs."""


_DISCLOSED_STATES = frozenset(
    {
        CapabilityState.UNAVAILABLE,
        CapabilityState.STALE,
        CapabilityState.PARTITIONED,
        CapabilityState.INCONSISTENT,
    }
)

# Provider organ states that make an authority-bearing dependency unsafe to
# mutate through — the fail-closed set (plan §Capability-path evaluation).
_UNSAFE_AUTHORITY_STATES = frozenset(
    {CapabilityState.STALE, CapabilityState.INCONSISTENT, CapabilityState.UNKNOWN}
)

# Relationships that carry mutation authority when live.
_AUTHORITY_BEARING = frozenset(
    {DependencyRelationship.HARD, DependencyRelationship.FALLBACK}
)


def organ_state(health: OrganHealth) -> CapabilityState:
    """Collapse an organ's per-facet health into one capability-facing state.

    Precedence is worst-wins so a positive liveness probe can never mask a failed
    correctness or authority facet: any ``inconsistent`` facet -> inconsistent;
    any ``failed`` -> unavailable; any ``stale`` -> stale; any ``unknown`` ->
    unknown; only an all-healthy organ is healthy. An organ with no evaluated
    facets is ``unknown`` (absent evidence never implies health).
    """
    states = set(health.facets.values())
    if not states:
        return CapabilityState.UNKNOWN
    if FacetState.INCONSISTENT in states:
        return CapabilityState.INCONSISTENT
    if FacetState.FAILED in states:
        return CapabilityState.UNAVAILABLE
    if FacetState.STALE in states:
        return CapabilityState.STALE
    if FacetState.UNKNOWN in states:
        return CapabilityState.UNKNOWN
    return CapabilityState.HEALTHY


def _bindings_by_organ(
    topology: OrganismTopology,
) -> Mapping[str, tuple[OrganBinding, ...]]:
    index: dict[str, list[OrganBinding]] = {}
    for binding in topology.bindings:
        index.setdefault(binding.organ_id, []).append(binding)
    return {organ_id: tuple(items) for organ_id, items in index.items()}


def _provider_organ_state(
    contract: OrganContract,
    bindings: Sequence[OrganBinding],
    evidence: EvidenceSnapshot,
    *,
    now: dt.datetime,
) -> CapabilityState:
    """State of a provider organ, worst-wins across its bindings.

    An organ with a contract but no binding has no live embodiment: ``unknown``
    (retain, never infer health). Otherwise the worst binding state wins so a
    single failing embodiment is not hidden by a healthy sibling.
    """
    if not bindings:
        return CapabilityState.UNKNOWN
    states = [
        organ_state(evaluate_organ_health(contract, binding, evidence, now=now))
        for binding in bindings
    ]
    for candidate in (
        CapabilityState.INCONSISTENT,
        CapabilityState.UNAVAILABLE,
        CapabilityState.STALE,
        CapabilityState.UNKNOWN,
    ):
        if candidate in states:
            return candidate
    return CapabilityState.HEALTHY


def _edge_gate(
    edge: OrganDependency,
    provider_state: CapabilityState,
    fallback_authority_current: bool,
) -> CapabilityState:
    """Contribution of one dependency edge to the capability path.

    Applies the typed propagation rules (plan §Capability-path evaluation):

    - ``hard``: an unavailable/unknown provider makes the capability unavailable;
      stale/inconsistent propagate their own state.
    - ``optional``: an absent (unavailable/unknown) provider is a declared
      ``degraded`` outcome only when the edge names ``degraded_behavior``;
      otherwise the contract is invalid and the path is unavailable.
    - ``fallback``: usable only with current compatibility and authority; a
      stale/inconsistent/unknown or authority-lapsed fallback contributes
      ``degraded`` (it may not carry the path on its own).
    - ``informational``: never gates availability.
    - ``feedback``: a bounded feedback edge does not gate forward availability;
      its bounds are a validation concern, not a runtime downgrade.
    """
    rel = edge.relationship
    if rel is DependencyRelationship.INFORMATIONAL:
        return CapabilityState.HEALTHY
    if rel is DependencyRelationship.FEEDBACK:
        return CapabilityState.HEALTHY
    if rel is DependencyRelationship.HARD:
        return provider_state
    if rel is DependencyRelationship.OPTIONAL:
        if provider_state in (CapabilityState.UNAVAILABLE, CapabilityState.UNKNOWN):
            if edge.degraded_behavior:
                return CapabilityState.DEGRADED
            return CapabilityState.UNAVAILABLE
        return provider_state
    if rel is DependencyRelationship.FALLBACK:
        if provider_state is CapabilityState.HEALTHY and fallback_authority_current:
            return CapabilityState.HEALTHY
        return CapabilityState.DEGRADED
    raise CapabilityHealthError(f"unhandled relationship: {rel!r}")


# Worst-wins precedence used to fold edge contributions into a path state.
_STATE_PRECEDENCE = (
    CapabilityState.INCONSISTENT,
    CapabilityState.PARTITIONED,
    CapabilityState.UNAVAILABLE,
    CapabilityState.STALE,
    CapabilityState.DEGRADED,
    CapabilityState.UNKNOWN,
    CapabilityState.HEALTHY,
)


def _fold(states: Sequence[CapabilityState]) -> CapabilityState:
    if not states:
        return CapabilityState.UNKNOWN
    present = set(states)
    for candidate in _STATE_PRECEDENCE:
        if candidate in present:
            return candidate
    return CapabilityState.UNKNOWN


def evaluate_capability_health(
    capability_id: str,
    topology: OrganismTopology,
    evidence: EvidenceSnapshot,
    *,
    now: dt.datetime,
) -> CapabilityHealth:
    """Project the health of one named capability path — pure and read-only.

    Gathers the dependency edges that provide ``capability_id``, evaluates each
    provider organ from its bindings and the evidence, propagates per typed edge
    relationship, and derives the mutation posture (fail closed on a stale,
    inconsistent, or unknown authority-bearing dependency; provisional read-only
    only when the consumer organ contract declares it). A capability with no
    providing edge is ``unknown`` — absence is never health.
    """
    if now.tzinfo is None:
        raise CapabilityHealthError("now must be timezone-aware")

    edges = tuple(
        edge
        for edge in topology.dependencies
        if edge.provider.capability_ref == capability_id
    )
    bindings = _bindings_by_organ(topology)

    contributors: list[Contributor] = []
    edge_states: list[CapabilityState] = []
    fail_closed = False
    provisional_declared = False

    for edge in edges:
        provider_organ = edge.provider.organ_id
        contract = topology.organs.get(provider_organ)
        if contract is None:
            # A dependency naming an unknown provider organ cannot be resolved.
            state = CapabilityState.UNKNOWN
            fallback_authority = False
        else:
            state = _provider_organ_state(
                contract, bindings.get(provider_organ, ()), evidence, now=now
            )
            fallback_authority = _fallback_authority_current(edge, evidence, now=now)

        contribution = _edge_gate(edge, state, fallback_authority)
        edge_states.append(contribution)

        # Fail-closed for mutation: an authority-bearing dependency whose provider
        # is stale, inconsistent, or unknown cannot safely authorize a mutation.
        # Provisional read-only continuation is that fail-closed state's declared
        # refinement: reads may continue with disclosure ONLY when the consumer
        # organ contract names a read-only continuation for the shortfall.
        if edge.relationship in _AUTHORITY_BEARING and state in _UNSAFE_AUTHORITY_STATES:
            fail_closed = True
            consumer_contract = topology.organs.get(edge.consumer.organ_id)
            if consumer_contract is not None and _declares_readonly_continuation(
                consumer_contract
            ):
                provisional_declared = True

        # Disclose every contributor that is not currently healthy — synthesis may
        # explain but may not silently drop a degraded/unavailable/unknown source.
        disclosed = contribution is not CapabilityState.HEALTHY
        contributors.append(
            Contributor(
                organ_id=provider_organ,
                relationship=edge.relationship,
                organ_state=state,
                disclosed=disclosed,
                provenance=edge.dependency_id,
            )
        )

    state = _fold(edge_states)

    if fail_closed:
        posture = (
            MutationPosture.PROVISIONAL_READONLY
            if provisional_declared
            else MutationPosture.FAIL_CLOSED
        )
    else:
        posture = MutationPosture.PERMITTED

    disclosures = tuple(c.provenance for c in contributors if c.disclosed)
    return CapabilityHealth(
        capability_id=capability_id,
        state=state,
        mutation_posture=posture,
        contributors=tuple(contributors),
        disclosures=disclosures,
    )


def _fallback_authority_current(
    edge: OrganDependency, evidence: EvidenceSnapshot, *, now: dt.datetime
) -> bool:
    """A fallback edge is usable only with a current, passing authority facet."""
    from tools.organism_topology import _current_passing_authority  # local import: pure helper

    return any(
        _current_passing_authority(evidence_id, evidence, now)
        for evidence_id in edge.selection.evidence_refs
    )


# ---------------------------------------------------------------------------
# Report adapter — consume ORG-076 fleet-health evidence (read-only).
# ---------------------------------------------------------------------------

# Maps a tools/fleet_health.py agent name to its T-3187 organ binding. Sub-
# orchestrator seats (mason/artisan) carry no top-level organ binding and are
# intentionally absent. Callers may override for a different fleet shape.
DEFAULT_AGENT_TO_BINDING: Mapping[str, str] = {
    "orchestrator": "binding:pantheon-executive-orchestrator",
    "recovery": "binding:recovery-homeostasis-recovery",
    "specialist": "binding:intelligence-research-specialist",
    "codex": "binding:harness-adapters-codex",
    "archivist": "binding:exocortex-archivist",
}

_FLEET_STATUS_TO_RESULT: Mapping[str, str] = {"OK": "pass", "FAIL": "fail"}


@dataclasses.dataclass(frozen=True)
class OrganHealthReport:
    """One organ's collapsed health under a supplied evidence snapshot."""

    organ_id: str
    binding_id: str
    state: CapabilityState
    facets: Mapping[HealthFacet, FacetState]


def evidence_from_fleet_health(
    report: Mapping[str, object],
    *,
    observed_at: str,
    fresh_until: str | None,
    agent_to_binding: Mapping[str, str] = DEFAULT_AGENT_TO_BINDING,
) -> EvidenceSnapshot:
    """Map a parsed ``fleet_health.py --json`` report to a LIVENESS-facet snapshot.

    Pure and read-only: the caller runs ORG-076's sweep and supplies its parsed
    JSON; this module never spawns it and derives ONLY the liveness facet — a
    running seat proves liveness, never correctness/authority/continuity. OK->pass,
    FAIL->fail, anything else (WARN/PAUSED)->ambiguous, which cannot support a
    live/healthy claim. Actuation and repair stay with ORG-076.
    """
    agents = report.get("agents", []) if isinstance(report, Mapping) else []
    records: dict[str, object] = {}
    digests: dict[str, str] = {}
    for agent in agents:
        if not isinstance(agent, Mapping):
            continue
        name = str(agent.get("agent", ""))
        binding_id = agent_to_binding.get(name)
        if binding_id is None:
            continue
        status = str(agent.get("status", ""))
        result = _FLEET_STATUS_TO_RESULT.get(status, "ambiguous")
        evidence_id = f"evidence:fleet-health-{name}-liveness"
        canonical_ref = f"fleet-health:{name}"
        sha256 = "sha256:" + hashlib.sha256(f"{name}|{status}".encode("utf-8")).hexdigest()
        raw = {
            "schema_version": 1,
            "evidence_id": evidence_id,
            "subject_ref": {"kind": "binding", "id": binding_id},
            "facet": "liveness",
            "evidence_kind": "probe",
            "canonical_ref": canonical_ref,
            "sha256": sha256,
            "observer_ref": "role:head-system-orchestrator",
            "observed_at": observed_at,
            "fresh_until": fresh_until,
            "result": result,
        }
        records[evidence_id] = parse_evidence_ref(raw, source=evidence_id)
        digests[canonical_ref] = sha256
    return EvidenceSnapshot(records=records, canonical_ref_sha256=digests)


def project_topology_health(
    topology: OrganismTopology,
    evidence: EvidenceSnapshot,
    *,
    now: dt.datetime,
) -> tuple[OrganHealthReport, ...]:
    """Read-only projection of every bound organ's health under ``evidence``.

    The honest current-fleet outcome, with only liveness evidence available, is a
    field of ``unknown`` states — matching the T-3187 inventory, where no binding
    could be certified live. This never actuates, repairs, or mutates.
    """
    reports: list[OrganHealthReport] = []
    for binding in topology.bindings:
        contract = topology.organs.get(binding.organ_id)
        if contract is None:
            continue
        health = evaluate_organ_health(contract, binding, evidence, now=now)
        reports.append(
            OrganHealthReport(
                organ_id=binding.organ_id,
                binding_id=binding.binding_id,
                state=organ_state(health),
                facets=dict(health.facets),
            )
        )
    return tuple(reports)


def _declares_readonly_continuation(contract: OrganContract) -> bool:
    """Whether the organ contract names a read-only continuation under shortfall.

    The plan permits provisional read-only continuation only when the organ
    contract declares it; we read the ``unknown``/``stale`` degradation clauses
    for an explicit read-only continuation token.
    """
    clauses = tuple(contract.degradation.unknown) + tuple(contract.degradation.stale)
    return any("read-only" in clause.lower() or "read only" in clause.lower() for clause in clauses)
