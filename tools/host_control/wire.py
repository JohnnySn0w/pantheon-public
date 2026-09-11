"""Wire-tier contract for the ``org.pantheon.HostBroker1`` D-Bus service.

This module deliberately imports NO dbus binding: the closed error
namespace, its total exception mapping, and the bounded JSON
serializations must be testable on a runner without dbus-python
(T-2912 plan decision — the guard shrinks to the one module that
genuinely needs it, ``tools.host_control.dbus_service``).

Error names are PROVISIONAL-PENDING-RATIFICATION: ORG-052 §4 ratifies a
closed, versioned error namespace but does not name the strings; the
twelve names below were minted in T-2912 and surfaced upward for
ratification visibility.
"""

from __future__ import annotations

from typing import Mapping

from tools.host_control.contracts import (
    ApprovalUnavailable,
    CapabilitiesResult,
    HostControlError,
    IdempotencyConflict,
    InvalidTransition,
    MigrationRequired,
    OperationRecord,
    PolicyDenied,
    RecoveryRefused,
    RegistryError,
    SubjectMismatch,
    UnknownHandle,
    canonical_json,
    resolve_error_name,
)

BUS_NAME = "org.pantheon.HostBroker1"
OBJECT_PATH = "/org/pantheon/HostBroker1"
MANAGER_INTERFACE = "org.pantheon.HostBroker1.Manager"
APPROVAL_SOURCE_INTERFACE = "org.pantheon.HostBroker1.ApprovalSource"

ERROR_NAMESPACE = "org.pantheon.HostBroker1.Error"

POLICY_DENIED_ERROR = f"{ERROR_NAMESPACE}.PolicyDenied"
IDEMPOTENCY_CONFLICT_ERROR = f"{ERROR_NAMESPACE}.IdempotencyConflict"
UNKNOWN_HANDLE_ERROR = f"{ERROR_NAMESPACE}.UnknownHandle"
SUBJECT_MISMATCH_ERROR = f"{ERROR_NAMESPACE}.SubjectMismatch"
INVALID_TRANSITION_ERROR = f"{ERROR_NAMESPACE}.InvalidTransition"
APPROVAL_UNAVAILABLE_ERROR = f"{ERROR_NAMESPACE}.ApprovalUnavailable"
RECOVERY_REFUSED_ERROR = f"{ERROR_NAMESPACE}.RecoveryRefused"
REGISTRY_DENIED_ERROR = f"{ERROR_NAMESPACE}.RegistryDenied"
MIGRATION_REQUIRED_ERROR = f"{ERROR_NAMESPACE}.MigrationRequired"
FAILED_ERROR = f"{ERROR_NAMESPACE}.Failed"
UNSUPPORTED_ERROR = f"{ERROR_NAMESPACE}.Unsupported"
INTERNAL_ERROR = f"{ERROR_NAMESPACE}.Internal"

CLOSED_ERROR_NAMES = frozenset(
    (
        POLICY_DENIED_ERROR,
        IDEMPOTENCY_CONFLICT_ERROR,
        UNKNOWN_HANDLE_ERROR,
        SUBJECT_MISMATCH_ERROR,
        INVALID_TRANSITION_ERROR,
        APPROVAL_UNAVAILABLE_ERROR,
        RECOVERY_REFUSED_ERROR,
        REGISTRY_DENIED_ERROR,
        MIGRATION_REQUIRED_ERROR,
        FAILED_ERROR,
        UNSUPPORTED_ERROR,
        INTERNAL_ERROR,
    )
)

# Nearest-ancestor resolution over this table makes the mapping TOTAL
# across the live exception hierarchy: any HostControlError subclass —
# including ones added after this unit — resolves via its closest mapped
# ancestor, and anything outside the hierarchy is a bounded Internal.
_ERROR_NAME_BY_TYPE: Mapping[type, str] = {
    PolicyDenied: POLICY_DENIED_ERROR,
    IdempotencyConflict: IDEMPOTENCY_CONFLICT_ERROR,
    UnknownHandle: UNKNOWN_HANDLE_ERROR,
    SubjectMismatch: SUBJECT_MISMATCH_ERROR,
    InvalidTransition: INVALID_TRANSITION_ERROR,
    ApprovalUnavailable: APPROVAL_UNAVAILABLE_ERROR,
    RecoveryRefused: RECOVERY_REFUSED_ERROR,
    RegistryError: REGISTRY_DENIED_ERROR,
    MigrationRequired: MIGRATION_REQUIRED_ERROR,
    HostControlError: FAILED_ERROR,
}

_INTERNAL_BODY = "internal error"
# RegistryError bodies can name trusted-file mode/path/owner facts —
# server configuration a caller must not learn. Fixed body, like
# Internal (PO-required change at T-2912 plan approval). The transport
# tier maps an UNREGISTERED caller uid to PolicyDenied("subject_denied")
# BEFORE any domain call, so this name covers only genuine
# root-owned-config faults.
_REGISTRY_DENIED_BODY = "registry refused the request"
_MESSAGE_LIMIT = 200
_JSON_LIMIT = 65536


def error_name_for(exc: BaseException) -> str:
    """Resolve any exception to exactly one closed error name.

    Delegates to the shared nearest-ancestor resolver in the value-level
    contracts module (T-2913 OQ2 disposition) so this mapping and the
    approval service's cannot drift apart in resolution semantics.
    """

    return resolve_error_name(exc, _ERROR_NAME_BY_TYPE, INTERNAL_ERROR)


def error_body_for(exc: BaseException) -> str:
    """Bounded, non-disclosing error body for the wire.

    Domain messages are server-authored bounded phrases (the package's
    ``_closed_string`` discipline); everything else gets a constant so a
    traceback, path, or credential can never cross the bus.
    """

    if isinstance(exc, PolicyDenied):
        return str(exc.reason_code)[:_MESSAGE_LIMIT]
    if isinstance(exc, RegistryError):
        return _REGISTRY_DENIED_BODY
    if isinstance(exc, HostControlError):
        return str(exc)[:_MESSAGE_LIMIT] or "failed"
    return _INTERNAL_BODY


def flatten_families(families: tuple[str, ...]) -> str:
    """Flatten operation families into the single scalar wire slot.

    Unambiguous ONLY because family names are ``_IDENTIFIER``-constrained
    (no commas); the defensive refusal below keeps that pairing loud if
    the charset is ever relaxed.
    """

    if not families:
        raise HostControlError("empty operation family set")
    for family in families:
        if (
            not isinstance(family, str)
            or not family
            or "," in family
        ):
            raise HostControlError("invalid operation family for wire")
    return ",".join(families)


def _bounded(encoded: str, context: str) -> str:
    if len(encoded.encode("utf-8")) > _JSON_LIMIT:
        raise HostControlError(f"{context} exceeds size limit")
    return encoded


def capabilities_json(result: CapabilitiesResult) -> str:
    """Canonical bounded capabilities payload for ``Capabilities()``.

    Carries only the bus-derived subject's own intersection — the
    domain's ``capability_grants`` is already subject-scoped; this
    serialization adds no roster surface (ORG-052 gate 9).
    """

    payload = {
        "schema": "pantheon-host-capabilities/v1",
        "contract_version": result.contract_version,
        "broker_revision": result.broker_revision,
        "policy_revision": result.policy_revision,
        "health": result.health,
        "host_operation_families": list(result.host_operation_families),
        "grants": [
            {
                "action": grant.action,
                "target_seat_id": grant.target_seat_id,
                "operation_families": list(grant.operation_families),
                "approval_class": grant.approval_class,
            }
            for grant in result.grants
        ],
    }
    return _bounded(canonical_json(payload), "capabilities payload")


def operation_json(record: OperationRecord) -> str:
    """Canonical bounded operation payload for ``OperationGet()``.

    The full durable record (all scalars, digest-validated at read) IS
    the bounded result JSON; the read is subject-bound upstream, so the
    caller sees only its own record.
    """

    if not record.validate_digest():
        raise HostControlError("invalid operation record digest")
    return _bounded(canonical_json(record.to_dict()), "operation payload")
