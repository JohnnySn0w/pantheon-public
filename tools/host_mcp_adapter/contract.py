"""Pure contract tier of the host MCP adapter (ORG-052 Unit 4a, T-2915).

No MCP SDK, no D-Bus binding, no ``tools.host_control`` import: this
module is the closed tool table, the JSON Schema 2020-12 input/output
schemas, the adapter-side input validator, the D-Bus-reply-to-result
shaping, and the total error mapping. Everything here is falsifiable on
any Python 3.12 runner.

The adapter deliberately does NOT import ``tools.host_control.wire``:
that package's ``__init__`` loads the broker journal/planner/policy/
registry and ``contracts`` loads ``tools.persona_installer`` — none of
which may be reachable from a privilege-free adapter (ORG-052 §1,
gate 18). The constants below are mirrored instead, and a test pins
them to the landed ``wire`` module so they cannot drift.

Error codes (``ERROR_CODES``) are PROVISIONAL-PENDING-RATIFICATION
alongside the T-2912 OQ4 wire names they are mapped from.
"""

from __future__ import annotations

import enum
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Final, Mapping

# --------------------------------------------------------------------
# fixed host substrate (mirrors tools/host_control/wire.py; parity-tested)
# --------------------------------------------------------------------

SYSTEM_BUS_ADDRESS: Final[str] = "unix:path=/run/dbus/system_bus_socket"
BUS_NAME: Final[str] = "org.pantheon.HostBroker1"
OBJECT_PATH: Final[str] = "/org/pantheon/HostBroker1"
MANAGER_INTERFACE: Final[str] = "org.pantheon.HostBroker1.Manager"
ERROR_NAMESPACE: Final[str] = "org.pantheon.HostBroker1.Error"

ADAPTER_NAME: Final[str] = "pantheon-host"
ADAPTER_CONTRACT_REVISION: Final[str] = "pantheon-host-mcp-adapter/v1"
EXPECTED_RUNTIME: Final[Mapping[str, str]] = {"mcp": "2.3.0", "jeepney": "0.9.0"}
EXPECTED_PYTHON: Final[tuple[int, int]] = (3, 12)

JSON_SCHEMA_DIALECT: Final[str] = "https://json-schema.org/draft/2020-12/schema"

# Bounds mirrored from the landed broker validators
# (planner/operations ``_bounded_scalar``: printable ASCII 0x21-0x7E,
# length 1..128; ``_bounded_hex``: 64 lowercase hex) and the wire JSON
# limit (``wire._JSON_LIMIT`` = 65536 bytes).
SCALAR_PATTERN: Final[str] = "^[!-~]{1,128}$"
HEX64_PATTERN: Final[str] = "^[0-9a-f]{64}$"
JSON_LIMIT: Final[int] = 65536
DETAIL_LIMIT: Final[int] = 200

_SCALAR_RE = re.compile(r"[!-~]{1,128}\Z")
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")

# Bounds on the adapter's own waits (seconds).
CONNECT_TIMEOUT: Final[float] = 2.0
READ_CALL_TIMEOUT: Final[float] = 10.0
WRITE_CALL_TIMEOUT: Final[float] = 30.0

# In-memory, per-process limiter. NOT a security control: any same-uid
# process can call the broker over D-Bus directly; broker-side rate
# limiting is routed to T-2917.
RATE_BURST: Final[int] = 20
RATE_PER_SECOND: Final[float] = 5.0


class RateLimiter:
    """Per-process token bucket. In-memory only (not durable state) and
    NOT a security control: a same-uid process can reach the broker over
    D-Bus directly. Broker-side rate limiting is routed to T-2917."""

    def __init__(
        self,
        *,
        burst: int = RATE_BURST,
        per_second: float = RATE_PER_SECOND,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._burst = float(burst)
        self._rate = float(per_second)
        self._clock = clock
        self._tokens = float(burst)
        self._last = clock()

    def allow(self) -> bool:
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + max(0.0, now - self._last) * self._rate)
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


class Method(enum.Enum):
    """The closed set of D-Bus methods the adapter can ever call.

    ``bus_client`` accepts only members of this enum — there is no
    string-named method, interface, destination, or path parameter
    anywhere in the adapter (no generic D-Bus passthrough).
    """

    CAPABILITIES = ("Capabilities", "", "ss")
    PLAN = ("Plan", "sss", "sss")
    APPLY = ("Apply", "ssss", "s")
    VERIFY = ("Verify", "sss", "s")
    ROLLBACK = ("Rollback", "sss", "s")
    OPERATION_GET = ("OperationGet", "s", "ss")
    APPROVAL_GET = ("ApprovalGet", "s", "ss")

    @property
    def member(self) -> str:
        return self.value[0]

    @property
    def in_signature(self) -> str:
        return self.value[1]

    @property
    def out_signature(self) -> str:
        return self.value[2]


# --------------------------------------------------------------------
# tool table
# --------------------------------------------------------------------

_SCALAR = "scalar"
_HEX64 = "hex64"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    method: Method
    title: str
    description: str
    params: tuple[tuple[str, str], ...]
    outputs: tuple[tuple[str, str], ...]
    read_only: bool
    destructive: bool
    idempotent: bool

    @property
    def timeout(self) -> float:
        return READ_CALL_TIMEOUT if self.read_only else WRITE_CALL_TIMEOUT


_POLL = (
    "The returned operation is durable: a client timeout or disconnect "
    "never cancels it. Poll host.operation.get with the handle."
)

TOOLS: Final[tuple[ToolSpec, ...]] = (
    ToolSpec(
        name="host.capabilities",
        method=Method.CAPABILITIES,
        title="Host broker capabilities",
        description=(
            "Optional discovery: the host broker's contract, broker and "
            "policy revisions, health, host-supported operation families, "
            "and the operations THIS seat is already authorized to plan "
            "or apply. Use at bringup or after an unsupported/policy "
            "error, not on every task."
        ),
        params=(),
        outputs=(("contract_version", _SCALAR), ("capabilities", "object")),
        read_only=True,
        destructive=False,
        idempotent=True,
    ),
    ToolSpec(
        name="host.plan",
        method=Method.PLAN,
        title="Plan a host operation",
        description=(
            "Ask the host broker to plan a registered seat against an "
            "allowlisted manifest revision. Returns a server-owned plan "
            "handle, its digest, and a bounded summary. Reusing the same "
            "idempotency_key returns the same plan. Applying a plan needs "
            "a separately issued approval handle."
        ),
        params=(
            ("seat_id", _SCALAR),
            ("manifest_revision", _SCALAR),
            ("idempotency_key", _SCALAR),
        ),
        outputs=(
            ("plan_handle", _HEX64),
            ("plan_digest", _HEX64),
            ("summary", "object"),
        ),
        read_only=False,
        destructive=False,
        idempotent=True,
    ),
    ToolSpec(
        name="host.apply",
        method=Method.APPLY,
        title="Apply an approved plan",
        description=(
            "Submit the exact stored plan (handle plus digest) for "
            "execution under an approval handle issued by the separate "
            "approval authority. Returns a durable operation handle. "
            "Reusing the same idempotency_key returns the same operation. "
            + _POLL
        ),
        params=(
            ("plan_handle", _HEX64),
            ("plan_digest", _HEX64),
            ("idempotency_key", _SCALAR),
            ("approval_handle", _HEX64),
        ),
        outputs=(("operation_handle", _HEX64),),
        read_only=False,
        destructive=True,
        idempotent=True,
    ),
    ToolSpec(
        name="host.verify",
        method=Method.VERIFY,
        title="Verify a seat against a plan",
        description=(
            "Request bounded verification evidence for a seat and plan. "
            "May run active probes. Returns an operation handle. "
            + _POLL
        ),
        params=(
            ("seat_id", _SCALAR),
            ("plan_handle", _HEX64),
            ("idempotency_key", _SCALAR),
        ),
        outputs=(("operation_handle", _HEX64),),
        read_only=False,
        destructive=False,
        idempotent=True,
    ),
    ToolSpec(
        name="host.rollback",
        method=Method.ROLLBACK,
        title="Roll back a receipt",
        description=(
            "Request a receipt-bound restore under an approval handle "
            "issued by the separate approval authority. Returns a durable "
            "operation handle. Reusing the same idempotency_key returns "
            "the same operation. " + _POLL
        ),
        params=(
            ("receipt_handle", _HEX64),
            ("idempotency_key", _SCALAR),
            ("approval_handle", _HEX64),
        ),
        outputs=(("operation_handle", _HEX64),),
        read_only=False,
        destructive=True,
        idempotent=True,
    ),
    ToolSpec(
        name="host.operation.get",
        method=Method.OPERATION_GET,
        title="Get operation state",
        description=(
            "Read the state and bounded result of one of this seat's "
            "operations."
        ),
        params=(("operation_handle", _HEX64),),
        outputs=(("state", _SCALAR), ("result", "object")),
        read_only=True,
        destructive=False,
        idempotent=True,
    ),
    ToolSpec(
        name="host.approval.get",
        method=Method.APPROVAL_GET,
        title="Get approval state",
        description=(
            "Read the state and bounded non-secret summary of an approval "
            "handle bound to this seat."
        ),
        params=(("approval_handle", _HEX64),),
        outputs=(("state", _SCALAR), ("summary", "object")),
        read_only=True,
        destructive=False,
        idempotent=True,
    ),
)

TOOLS_BY_NAME: Final[Mapping[str, ToolSpec]] = {tool.name: tool for tool in TOOLS}


def _property_schema(kind: str) -> dict[str, Any]:
    if kind == _SCALAR:
        return {"type": "string", "pattern": SCALAR_PATTERN, "maxLength": 128}
    if kind == _HEX64:
        return {"type": "string", "pattern": HEX64_PATTERN}
    if kind == "object":
        return {"type": "object"}
    raise AssertionError(kind)


def input_schema(tool: ToolSpec) -> dict[str, Any]:
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "type": "object",
        "properties": {name: _property_schema(kind) for name, kind in tool.params},
        "required": [name for name, _ in tool.params],
        "additionalProperties": False,
    }


def output_schema(tool: ToolSpec) -> dict[str, Any]:
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "type": "object",
        "properties": {name: _property_schema(kind) for name, kind in tool.outputs},
        "required": [name for name, _ in tool.outputs],
        "additionalProperties": False,
    }


def annotations(tool: ToolSpec) -> dict[str, bool]:
    """ORG-052 §4 hints. Hints only: server-side policy enforces."""

    if tool.read_only:
        return {"readOnlyHint": True, "openWorldHint": False}
    return {
        "readOnlyHint": False,
        "destructiveHint": tool.destructive,
        "idempotentHint": tool.idempotent,
        "openWorldHint": False,
    }


# --------------------------------------------------------------------
# errors
# --------------------------------------------------------------------


@dataclass(frozen=True)
class ErrorSpec:
    retryable: bool
    message: str


ERROR_CODES: Final[Mapping[str, ErrorSpec]] = {
    # adapter-side
    "invalid_arguments": ErrorSpec(False, "arguments do not match the tool input schema; nothing was sent to the broker"),
    "rate_limited": ErrorSpec(True, "adapter call rate exceeded; retry shortly"),
    "broker_unavailable": ErrorSpec(True, "host broker is not reachable; nothing was submitted; retry later - no harness restart is needed"),
    "broker_no_reply": ErrorSpec(True, "outcome unknown: the broker did not reply; retry with the SAME idempotency_key or poll host.operation.get"),
    "broker_contract_mismatch": ErrorSpec(False, "host broker reply did not match the adapter contract; report to the operator"),
    "bus_access_denied": ErrorSpec(False, "system bus policy denied this seat; it is not registered with the host broker"),
    # broker closed namespace (T-2912 OQ4 names, provisional)
    "policy_denied": ErrorSpec(False, "host broker policy refused the request"),
    "idempotency_conflict": ErrorSpec(False, "idempotency_key was already used with different parameters"),
    "unknown_handle": ErrorSpec(False, "handle is unknown, expired, or not bound to this seat"),
    "subject_mismatch": ErrorSpec(False, "handle is bound to a different subject"),
    "invalid_transition": ErrorSpec(False, "operation is not in a state that allows this request"),
    "approval_unavailable": ErrorSpec(True, "approval authority is not available; retry later"),
    "recovery_refused": ErrorSpec(False, "broker refused recovery; manual recovery is required"),
    "registry_denied": ErrorSpec(False, "host registry refused the request; report to the operator"),
    "migration_required": ErrorSpec(True, "broker journal needs migration; retry after operator repair"),
    "failed": ErrorSpec(False, "host broker rejected the request"),
    "unsupported": ErrorSpec(False, "not supported by this broker revision"),
    "internal": ErrorSpec(True, "host broker internal error; retry later or report to the operator"),
    # T-2917 5a (ruling i1), provisional like the names above
    "broker_rate_limited": ErrorSpec(True, "host broker per-seat request budget exceeded; retry shortly"),
    # ORG-082 §2 (ratified; T-2918). The detail is the validated
    # pantheon-broker-error/v1 JSON payload.
    "substrate_family_not_exposed": ErrorSpec(False, "substrate families run only through the bootstrap in-process root path, never the host broker"),
    "registry_digest_mismatch": ErrorSpec(False, "host broker loaded a different operation registry; operator repair required"),
    "runtime_subset_mismatch": ErrorSpec(False, "host broker runtime table is not the reviewed subset; operator repair required"),
}

#: ORG-082 §2 names (directly in the versioned namespace, no ``.Error``).
ORG082_WIRE_ERROR_CODES: Final[Mapping[str, str]] = {
    "org.pantheon.HostBroker1.SubstrateFamilyNotExposed": "substrate_family_not_exposed",
    "org.pantheon.HostBroker1.RegistryDigestMismatch": "registry_digest_mismatch",
    "org.pantheon.HostBroker1.RuntimeSubsetMismatch": "runtime_subset_mismatch",
}

WIRE_ERROR_CODES: Final[Mapping[str, str]] = {
    f"{ERROR_NAMESPACE}.PolicyDenied": "policy_denied",
    f"{ERROR_NAMESPACE}.IdempotencyConflict": "idempotency_conflict",
    f"{ERROR_NAMESPACE}.UnknownHandle": "unknown_handle",
    f"{ERROR_NAMESPACE}.SubjectMismatch": "subject_mismatch",
    f"{ERROR_NAMESPACE}.InvalidTransition": "invalid_transition",
    f"{ERROR_NAMESPACE}.ApprovalUnavailable": "approval_unavailable",
    f"{ERROR_NAMESPACE}.RecoveryRefused": "recovery_refused",
    f"{ERROR_NAMESPACE}.RegistryDenied": "registry_denied",
    f"{ERROR_NAMESPACE}.MigrationRequired": "migration_required",
    f"{ERROR_NAMESPACE}.Failed": "failed",
    f"{ERROR_NAMESPACE}.Unsupported": "unsupported",
    f"{ERROR_NAMESPACE}.Internal": "internal",
    # T-2917 5a (ruling i1): broker-side per-uid budget. A distinct code
    # from the adapter-local "rate_limited" so each closed wire name keeps
    # exactly one code; same retryable posture.
    f"{ERROR_NAMESPACE}.RateLimited": "broker_rate_limited",
    **ORG082_WIRE_ERROR_CODES,
}


def _org082_detail(name: str, body: object) -> str | None:
    """ORG-082 §2 adapter-boundary presence rules; the canonical payload JSON
    (bounded) when they hold, ``None`` when the reply violates them."""

    if not (isinstance(body, (tuple, list)) and body and isinstance(body[0], str)):
        return None
    try:
        payload = json.loads(body[0])
    except ValueError:
        return None
    if not isinstance(payload, dict) or not all(isinstance(v, str) for v in payload.values()):
        return None
    if set(payload) - {"code", "family", "amendment", "retry_context", "manifest_revision"}:
        return None
    code = ORG082_WIRE_ERROR_CODES[name].upper()
    substrate = code == "SUBSTRATE_FAMILY_NOT_EXPOSED"
    if (payload.get("code") != code or substrate != ("family" in payload)
            or payload.get("amendment") != "ORG-082"
            or payload.get("retry_context") != ("bootstrap-in-process-root" if substrate else "not-retryable")):
        return None
    detail = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(detail) > DETAIL_LIMIT:  # keep valid JSON: drop the optional field first
        payload.pop("manifest_revision", None)
        detail = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return detail if len(detail) <= DETAIL_LIMIT else None

# Bus-daemon errors meaning the call was NOT delivered to a broker.
UNAVAILABLE_BUS_ERRORS: Final[frozenset[str]] = frozenset(
    (
        "org.freedesktop.DBus.Error.ServiceUnknown",
        "org.freedesktop.DBus.Error.NameHasNoOwner",
    )
)
UNAVAILABLE_BUS_ERROR_PREFIX: Final[str] = "org.freedesktop.DBus.Error.Spawn."
# Errors after which the call MAY have been delivered: outcome unknown.
NO_REPLY_BUS_ERRORS: Final[frozenset[str]] = frozenset(
    (
        "org.freedesktop.DBus.Error.NoReply",
        "org.freedesktop.DBus.Error.Timeout",
        "org.freedesktop.DBus.Error.TimedOut",
        "org.freedesktop.DBus.Error.Disconnected",
    )
)
ACCESS_DENIED_BUS_ERRORS: Final[frozenset[str]] = frozenset(
    ("org.freedesktop.DBus.Error.AccessDenied",)
)


class AdapterError(Exception):
    """A closed, bounded tool-execution error."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in ERROR_CODES:
            raise AssertionError(f"unknown adapter error code {code!r}")
        super().__init__(code)
        self.code = code
        self.detail = sanitize_detail(detail)

    def payload(self) -> dict[str, Any]:
        spec = ERROR_CODES[self.code]
        error: dict[str, Any] = {
            "code": self.code,
            "retryable": spec.retryable,
            "message": spec.message,
        }
        if self.detail:
            error["detail"] = self.detail
        return {"error": error}


def sanitize_detail(value: object) -> str:
    """Printable ASCII only, bounded; anything else is dropped."""

    if not isinstance(value, str):
        return ""
    cleaned = "".join(ch for ch in value if " " <= ch <= "~")
    return cleaned[:DETAIL_LIMIT]


def error_for_dbus_name(name: object, body: object = ()) -> AdapterError:
    """Total mapping of a D-Bus error reply onto the closed adapter codes.

    The remote message travels ONLY for the broker's closed namespace
    (server-authored bounded phrases); bus-daemon and unknown messages
    are dropped entirely.
    """

    name = name if isinstance(name, str) else ""
    if name in ORG082_WIRE_ERROR_CODES:
        detail = _org082_detail(name, body)
        if detail is None:  # a malformed ORG-082 reply is caught at this boundary
            return AdapterError("broker_contract_mismatch")
        return AdapterError(ORG082_WIRE_ERROR_CODES[name], detail)
    if name in WIRE_ERROR_CODES:
        detail = ""
        if isinstance(body, (tuple, list)) and body and isinstance(body[0], str):
            detail = body[0]
        return AdapterError(WIRE_ERROR_CODES[name], detail)
    if name in UNAVAILABLE_BUS_ERRORS or name.startswith(UNAVAILABLE_BUS_ERROR_PREFIX):
        return AdapterError("broker_unavailable")
    if name in NO_REPLY_BUS_ERRORS:
        return AdapterError("broker_no_reply")
    if name in ACCESS_DENIED_BUS_ERRORS:
        return AdapterError("bus_access_denied")
    return AdapterError("broker_contract_mismatch")


# --------------------------------------------------------------------
# input validation and result shaping
# --------------------------------------------------------------------


def _matches(kind: str, value: object) -> bool:
    if not isinstance(value, str):
        return False
    if kind == _SCALAR:
        return _SCALAR_RE.fullmatch(value) is not None
    if kind == _HEX64:
        return _HEX64_RE.fullmatch(value) is not None
    return False


def validate_arguments(tool: ToolSpec, arguments: object) -> tuple[str, ...]:
    """Adapter-side validation (the broker remains authoritative).

    Returns the positional D-Bus arguments in signature order.
    """

    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise AdapterError("invalid_arguments", "arguments must be an object")
    expected = [name for name, _ in tool.params]
    extra = sorted(set(arguments) - set(expected))
    if extra:
        raise AdapterError("invalid_arguments", "unexpected argument: " + str(extra[0]))
    values = []
    for name, kind in tool.params:
        if name not in arguments:
            raise AdapterError("invalid_arguments", f"missing argument: {name}")
        value = arguments[name]
        if not _matches(kind, value):
            raise AdapterError("invalid_arguments", f"invalid argument: {name}")
        values.append(value)
    return tuple(values)


def _reject_constant(value: str) -> Any:
    raise ValueError("non-finite number")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def parse_bounded_object(text: object) -> dict[str, Any]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > JSON_LIMIT:
        raise AdapterError("broker_contract_mismatch")
    try:
        value = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicates,
        )
    except (ValueError, RecursionError):
        raise AdapterError("broker_contract_mismatch") from None
    if not isinstance(value, dict):
        raise AdapterError("broker_contract_mismatch")
    return value


def shape_result(tool: ToolSpec, body: object) -> dict[str, Any]:
    """D-Bus reply body -> structuredContent, strictly per output schema."""

    if not isinstance(body, (tuple, list)) or len(body) != len(tool.outputs):
        raise AdapterError("broker_contract_mismatch")
    result: dict[str, Any] = {}
    for (name, kind), value in zip(tool.outputs, body):
        if kind == "object":
            result[name] = parse_bounded_object(value)
        elif _matches(kind, value):
            result[name] = value
        else:
            raise AdapterError("broker_contract_mismatch")
    return result


def canonical_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# --------------------------------------------------------------------
# self-check report (ORG-068 E10.C1 legibility path for a stdio component;
# accepted by HO ruling on T-2915 as legs.health partial_by_design)
# --------------------------------------------------------------------

HEALTH_CONTRACT_REVISION: Final[str] = "pantheon-health/v1"
SELF_CHECK_ROSTER: Final[tuple[str, ...]] = ("runtime", "system_bus", "broker")


def self_check_report(components: Mapping[str, tuple[str, str, str]]) -> dict[str, Any]:
    """Build the Health1-shaped self-check payload.

    ``components`` maps each roster name to (status, reason, revision)
    with status in ready/degraded/unhealthy/not_configured.
    """

    if tuple(components) != SELF_CHECK_ROSTER:
        raise AssertionError("self-check components must match the roster")
    statuses = [components[name][0] for name in SELF_CHECK_ROSTER]
    if "unhealthy" in statuses:
        overall = "unhealthy"
    elif any(status in ("degraded", "not_configured") for status in statuses):
        overall = "degraded"
    else:
        overall = "ready"
    return {
        "ContractRevision": HEALTH_CONTRACT_REVISION,
        "Service": ADAPTER_NAME,
        "Status": overall,
        "Subcomponents": {name: components[name][0] for name in SELF_CHECK_ROSTER},
        "Reasons": {name: sanitize_detail(components[name][1]) for name in SELF_CHECK_ROSTER},
        "Revisions": {name: sanitize_detail(components[name][2]) or "-" for name in SELF_CHECK_ROSTER},
    }
