"""Bounded uniform health contract for Action Broker services.

The ORG-068 steady state before T-2917/T-2919 is honest degraded:
broker reachability legs that are not built yet remain machine-legible
as ``not_configured`` subcomponents, so the monitor can distinguish
"known absent leg" from a live probe failure instead of treating every
degraded broker as an incident.

T-2917 5d.4 (ruling T-2917:D21; HO ruling on the roster): the broker roster
gains ``operation_driver`` (PROVISIONAL name), and the operation driver's
cache (``tools.host_control.driver``) supplies real ``operation_driver``,
``executor_reachability`` and ``approval_reachability`` probes. A leg whose
source is not wired in a broker instance stays ``not_configured``.

Roster growth rule (for every consumer, e.g. the B7 recovery seat, T-4884):
legs are only ever APPENDED, existing legs keep their names and order, and a
reader must key by leg name and tolerate legs it does not know. A pinned
test holds the pre-5d prefix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Iterable, Literal, Mapping, Protocol

from tools.host_control.contracts import HostControlError

ServiceName = Literal["broker", "approval"]
OverallStatus = Literal["ready", "degraded", "unhealthy"]
ComponentStatus = Literal["ready", "degraded", "unhealthy", "not_configured"]

HEALTH_INTERFACE: Final[str] = "org.pantheon.Health1"
CONTRACT_REVISION: Final[str] = "pantheon-health/v1"
HEALTH_PROPERTIES: Final[tuple[str, ...]] = (
    "ContractRevision",
    "Status",
    "Subcomponents",
    "Reasons",
    "Revisions",
)
HEALTH_PROPERTY_SIGNATURES: Final[Mapping[str, str]] = {
    "ContractRevision": "s",
    "Status": "s",
    "Subcomponents": "a{ss}",
    "Reasons": "a{ss}",
    "Revisions": "a{ss}",
}

BROKER_ROSTER: Final[tuple[str, ...]] = (
    "journal",
    "registry",
    "policy",
    "approval_reachability",
    "executor_reachability",
    # T-2917 5d.4 (PROVISIONAL, HO ruling): the broker operation driver
    "operation_driver",
)
APPROVAL_ROSTER: Final[tuple[str, ...]] = (
    "journal",
    "approver_policy",
    "broker_metadata_reachability",
)
ROSTERS: Final[Mapping[ServiceName, tuple[str, ...]]] = {
    "broker": BROKER_ROSTER,
    "approval": APPROVAL_ROSTER,
}

_OVERALL_STATUSES = frozenset(("ready", "degraded", "unhealthy"))
_COMPONENT_STATUSES = frozenset(
    ("ready", "degraded", "unhealthy", "not_configured")
)
_PROBE_FAILED_REASON = "probe failed"


class HealthProbe(Protocol):
    def __call__(self) -> "HealthProbeResult": ...


@dataclass(frozen=True)
class HealthProbeResult:
    status: ComponentStatus
    reason: str = ""
    revision: str = ""


@dataclass(frozen=True)
class SubcomponentHealth:
    name: str
    status: ComponentStatus
    reason: str
    revision: str


@dataclass(frozen=True)
class HealthSnapshot:
    service: ServiceName
    contract_revision: str
    status: OverallStatus
    subcomponents: tuple[SubcomponentHealth, ...]


class HealthProvider:
    def __init__(
        self,
        *,
        service: ServiceName,
        probes: Mapping[str, HealthProbe],
    ) -> None:
        if service not in ROSTERS:
            raise HostControlError("unknown health service")
        roster = ROSTERS[service]
        if tuple(probes.keys()) != roster or set(probes) != set(roster):
            raise HostControlError("health probes must match closed roster")
        self._service = service
        self._roster = roster
        self._probes = dict(probes)

    @property
    def roster(self) -> tuple[str, ...]:
        return self._roster

    def snapshot(self) -> HealthSnapshot:
        components = tuple(
            self._component(name, self._probes[name]) for name in self._roster
        )
        return HealthSnapshot(
            service=self._service,
            contract_revision=CONTRACT_REVISION,
            status=derive_overall(components),
            subcomponents=components,
        )

    def capabilities_health(self) -> Literal["ready", "degraded"]:
        return "ready" if self.snapshot().status == "ready" else "degraded"

    def _component(self, name: str, probe: HealthProbe) -> SubcomponentHealth:
        try:
            result = probe()
        except Exception:
            result = HealthProbeResult(
                status="unhealthy",
                reason=_PROBE_FAILED_REASON,
                revision="probe-failed",
            )
        if not isinstance(result, HealthProbeResult):
            result = HealthProbeResult(
                status="unhealthy",
                reason=_PROBE_FAILED_REASON,
                revision="probe-failed",
            )
        try:
            return SubcomponentHealth(
                name=name,
                status=_component_status(result.status),
                reason=_reason(result.reason, result.status),
                revision=_revision(result.revision),
            )
        except HostControlError:
            return SubcomponentHealth(
                name=name,
                status="unhealthy",
                reason=_PROBE_FAILED_REASON,
                revision="probe-failed",
            )


def unavailable_probe(*, reason: str, revision: str) -> HealthProbe:
    def probe() -> HealthProbeResult:
        return HealthProbeResult(
            status="not_configured",
            reason=reason,
            revision=revision,
        )

    return probe


def constant_probe(
    *,
    status: ComponentStatus = "ready",
    reason: str = "",
    revision: str,
) -> HealthProbe:
    def probe() -> HealthProbeResult:
        return HealthProbeResult(status=status, reason=reason, revision=revision)

    return probe


def derive_overall(
    components: Iterable[SubcomponentHealth],
) -> OverallStatus:
    statuses = tuple(component.status for component in components)
    if any(status == "unhealthy" for status in statuses):
        return "unhealthy"
    if any(status in ("degraded", "not_configured") for status in statuses):
        return "degraded"
    return "ready"


def serialize_health(snapshot: HealthSnapshot) -> dict[str, object]:
    if snapshot.contract_revision != CONTRACT_REVISION:
        raise HostControlError("invalid health contract revision")
    if snapshot.service not in ROSTERS:
        raise HostControlError("unknown health service")
    status = _overall_status(snapshot.status)
    names = tuple(component.name for component in snapshot.subcomponents)
    if names != ROSTERS[snapshot.service]:
        raise HostControlError("health snapshot must match closed roster")
    subcomponents: dict[str, str] = {}
    reasons: dict[str, str] = {}
    revisions: dict[str, str] = {}
    for component in snapshot.subcomponents:
        name = _component_name(component.name)
        subcomponents[name] = _component_status(component.status)
        reasons[name] = _reason(component.reason, component.status)
        revisions[name] = _revision(component.revision)
    return {
        "ContractRevision": CONTRACT_REVISION,
        "Status": status,
        "Subcomponents": subcomponents,
        "Reasons": reasons,
        "Revisions": revisions,
    }


def serialized_changed_properties(
    current: HealthSnapshot,
    previous: HealthSnapshot | None = None,
) -> dict[str, object]:
    current_payload = serialize_health(current)
    if previous is None:
        return current_payload
    previous_payload = serialize_health(previous)
    return {
        key: value
        for key, value in current_payload.items()
        if previous_payload.get(key) != value
    }


def make_broker_health_provider(
    *,
    journal_probe: HealthProbe,
    registry_probe: HealthProbe,
    policy_probe: HealthProbe,
    approval_reachability_probe: HealthProbe | None = None,
    executor_reachability_probe: HealthProbe | None = None,
    operation_driver_probe: HealthProbe | None = None,
) -> HealthProvider:
    return HealthProvider(
        service="broker",
        probes={
            "journal": journal_probe,
            "registry": registry_probe,
            "policy": policy_probe,
            "approval_reachability": approval_reachability_probe
            or unavailable_probe(
                reason="approval reachability probe is not wired in this broker (T-2917 driver cache)",
                revision="T-2917",
            ),
            "executor_reachability": executor_reachability_probe
            or unavailable_probe(
                reason="executor reachability probe is not wired in this broker (T-2917 driver cache)",
                revision="T-2917",
            ),
            "operation_driver": operation_driver_probe
            or unavailable_probe(
                reason="operation driver is not wired in this broker (T-2917 driver cache)",
                revision="T-2917",
            ),
        },
    )


def make_approval_health_provider(
    *,
    journal_probe: HealthProbe,
    approver_policy_probe: HealthProbe,
    broker_metadata_reachability_probe: HealthProbe,
) -> HealthProvider:
    return HealthProvider(
        service="approval",
        probes={
            "journal": journal_probe,
            "approver_policy": approver_policy_probe,
            "broker_metadata_reachability": broker_metadata_reachability_probe,
        },
    )


def _printable_ascii(value: object, field_name: str, *, min_bytes: int, max_bytes: int) -> str:
    if not isinstance(value, str):
        raise HostControlError(f"invalid health {field_name}")
    encoded = value.encode("ascii", errors="strict")
    if not min_bytes <= len(encoded) <= max_bytes:
        raise HostControlError(f"invalid health {field_name}")
    if any(byte < 32 or byte > 126 for byte in encoded):
        raise HostControlError(f"invalid health {field_name}")
    return value


def _component_name(value: object) -> str:
    name = _printable_ascii(value, "component name", min_bytes=1, max_bytes=64)
    if not any(name in roster for roster in ROSTERS.values()):
        raise HostControlError("health component outside closed roster")
    return name


def _component_status(value: object) -> ComponentStatus:
    if value not in _COMPONENT_STATUSES:
        raise HostControlError("invalid health component status")
    return value  # type: ignore[return-value]


def _overall_status(value: object) -> OverallStatus:
    if value not in _OVERALL_STATUSES:
        raise HostControlError("invalid health status")
    return value  # type: ignore[return-value]


def _reason(value: object, status: object) -> str:
    reason = _printable_ascii(value, "reason", min_bytes=0, max_bytes=200)
    if status == "ready" and reason:
        raise HostControlError("ready health reason must be empty")
    if status != "ready" and not reason:
        raise HostControlError("non-ready health reason must not be empty")
    return reason


def _revision(value: object) -> str:
    return _printable_ascii(value, "revision", min_bytes=1, max_bytes=128)
