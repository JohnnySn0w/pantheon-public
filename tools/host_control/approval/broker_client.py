"""D-Bus client of ``HostBroker1.ApprovalSource`` implementing the
approval domain's ``PlanMetadataSource`` seam (ORG-052 §5, T-2913 —
the landed ``approval_source.py`` docstring forward-assigns the
dataclass conversion to this unit).

Split per the T-2913 OQ5 disposition: the conversion and error-mapping
functions below are PURE (no dbus import) so their semantics stay
falsifiable on a dbus-less runner; only the thin proxy wrapper at the
bottom touches dbus, behind the same guarded-import discipline as
``dbus_service``.

Importing the BROKER's wire constants here is deliberate and confined
to this module (a placement test pins that): a client of a server's
wire contract must name that contract, and duplicating the strings
would drift. ``tools.host_control.wire`` is value-level (it imports
only the shared contracts module) — no broker journal, operations,
planner, policy, or registry surface becomes reachable through it, so
the landed separation checkers pass unchanged.

Error posture (fail closed, per the seam's Protocol contract):

- the broker's ``UnknownHandle`` wire error maps to ``UnknownHandle``
  with the broker's own fixed non-disclosing body — never-existed,
  malformed, and expired stay byte-indistinguishable (the T-2912 C1
  posture travels through this client untouched);
- EVERY other failure — transport faults (no owner, disconnect),
  broker-side faults, malformed replies — maps to
  ``MetadataUnavailable``: the caller must refuse WITHOUT writing, so
  the grant stays issued and retryable. A client cannot reliably
  distinguish broker fault classes, and misclassifying one as a denial
  would terminally refuse an operation for an infrastructure fault
  (the AB-2 decision-22/23 shape).
"""

from __future__ import annotations

from typing import Sequence

from tools.host_control.approval.contracts import (
    MetadataUnavailable,
    PlanMetadata,
)
from tools.host_control.contracts import UnknownHandle
from tools.host_control.wire import (
    APPROVAL_SOURCE_INTERFACE as BROKER_APPROVAL_SOURCE_INTERFACE,
)
from tools.host_control.wire import BUS_NAME as BROKER_BUS_NAME
from tools.host_control.wire import OBJECT_PATH as BROKER_OBJECT_PATH
from tools.host_control.wire import (
    UNKNOWN_HANDLE_ERROR as BROKER_UNKNOWN_HANDLE_ERROR,
)

try:  # pragma: no cover - exercised implicitly on every import
    import dbus

    DBUS_AVAILABLE = True
except ImportError:  # pragma: no cover - dbus-less runners only
    DBUS_AVAILABLE = False

# One message for never-existed, malformed, and expired — mirrors the
# broker provider's non-disclosing body byte-for-byte.
_UNKNOWN = "unknown plan handle"
_NAME_LIMIT = 200


def map_broker_error(error_name: str, message: str) -> Exception:
    """Map a remote D-Bus error onto the seam's two raise types.

    Only the structured error NAME routes; the remote MESSAGE is
    deliberately dropped (transport-library messages are unbounded and
    not server-authored — nothing from them may propagate)."""

    if error_name == BROKER_UNKNOWN_HANDLE_ERROR:
        return UnknownHandle(_UNKNOWN)
    return MetadataUnavailable(
        f"approval source unavailable: {str(error_name)[:_NAME_LIMIT]}"
    )


def plan_metadata_from_wire(values: Sequence[object]) -> PlanMetadata:
    """The eight ``PlanMetadata`` wire slots -> value dataclass.

    Inverse of the broker's family flattening: slot 4 is the
    comma-joined operation-family list (unambiguous because family
    names are identifier-constrained — the broker's
    ``flatten_families`` refuses comma-bearing names at source)."""

    slots = [str(item) for item in values]
    if len(slots) != 8 or not all(slots):
        raise MetadataUnavailable(
            "approval source returned a malformed metadata reply"
        )
    families = tuple(slots[3].split(","))
    if not all(families):
        raise MetadataUnavailable(
            "approval source returned a malformed family list"
        )
    return PlanMetadata(
        owner=slots[0],
        performer=slots[1],
        target_seat_id=slots[2],
        operation_families=families,
        plan_digest=slots[4],
        effect_digest=slots[5],
        manifest_revision=slots[6],
        policy_revision=slots[7],
    )


def plan_summary_from_wire(values: Sequence[object]) -> tuple[str, str]:
    """The two ``PlanSummary`` wire slots -> (recomputed digest,
    bounded redacted summary). The SERVICE owns the digest comparison;
    this conversion only refuses a structurally malformed reply.

    An EMPTY summary slot is treated as malformed (``not all(slots)``
    below), not as a valid empty result: the broker's canonical
    summary (``HostPlanService.plan_summary``) always serializes a
    JSON object with at minimum a ``schema`` key, so an empty string
    never reaches this conversion under the current broker contract,
    and this conversion cannot distinguish a legitimately-empty
    summary from a broken one. If a future broker legitimately emits
    an empty summary, this fail-closed mapping needs reconsidering —
    it is a deliberate assumption pinned by
    ``test_empty_summary_slot_is_treated_as_unavailable_not_valid``,
    not an oversight."""

    slots = [str(item) for item in values]
    if len(slots) != 2 or not all(slots):
        raise MetadataUnavailable(
            "approval source returned a malformed summary reply"
        )
    return slots[0], slots[1]


if DBUS_AVAILABLE:

    class BrokerPlanMetadataClient:
        """``PlanMetadataSource`` over a live bus connection.

        Handed a connected ``BusConnection`` (production wiring is
        T-2920's); holds no journal, no policy, no state — a pure
        read-side proxy."""

        def __init__(self, connection: "dbus.bus.BusConnection") -> None:
            self._connection = connection

        def plan_metadata(self, plan_handle: str) -> PlanMetadata:
            values = self._call("PlanMetadata", "s", (str(plan_handle),))
            return plan_metadata_from_wire(values)

        def plan_summary(
            self,
            plan_handle: str,
            expected_digest: str,
        ) -> tuple[str, str]:
            values = self._call(
                "PlanSummary",
                "ss",
                (str(plan_handle), str(expected_digest)),
            )
            return plan_summary_from_wire(values)

        def _call(
            self,
            method: str,
            signature: str,
            args: tuple,
        ) -> Sequence[object]:
            try:
                return self._connection.call_blocking(
                    BROKER_BUS_NAME,
                    BROKER_OBJECT_PATH,
                    BROKER_APPROVAL_SOURCE_INTERFACE,
                    method,
                    signature,
                    args,
                )
            except dbus.exceptions.DBusException as exc:
                raise map_broker_error(
                    exc.get_dbus_name(),
                    exc.get_dbus_message(),
                ) from exc
