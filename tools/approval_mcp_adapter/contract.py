"""Pure contract tier of the approver MCP adapter (ORG-052 Unit 4b, T-2916).

No MCP SDK, no D-Bus binding, no ``tools.host_control`` import: this
module is the closed four-tool table, the JSON Schema 2020-12 input and
output schemas, the adapter-side input validator, the D-Bus-reply-to-
result shaping, and the total error mapping for the
``org.pantheon.HostApproval1.Reviewer`` interface (ORG-052 §5).

It mirrors ``tools/host_mcp_adapter/contract.py`` (T-2915) on purpose
and does not import it: each adapter binary ships a closed module set
that names exactly one D-Bus destination (gate 18). The shared generic
behaviour (rate limiter, bounded JSON parse, canonical text, detail
sanitizer, self-check derivation) is pinned identical by
``tools/test_approval_mcp_adapter.py`` so the two copies cannot drift.

The adapter deliberately does NOT import
``tools.host_control.approval.wire``: that package's ``__init__`` loads the
approval journal and service, none of which may be reachable from a
privilege-free adapter (ORG-052 §1, gate 18). The constants below are
mirrored instead and a test pins them to the landed module.

Error codes (``ERROR_CODES``) are PROVISIONAL-PENDING-RATIFICATION
alongside the T-2913 approval wire names they are mapped from.
"""

from __future__ import annotations

import enum
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Final, Mapping

# --------------------------------------------------------------------
# fixed host substrate (mirrors tools/host_control/approval/wire.py;
# parity-tested)
# --------------------------------------------------------------------

SYSTEM_BUS_ADDRESS: Final[str] = "unix:path=/run/dbus/system_bus_socket"
BUS_NAME: Final[str] = "org.pantheon.HostApproval1"
OBJECT_PATH: Final[str] = "/org/pantheon/HostApproval1"
REVIEWER_INTERFACE: Final[str] = "org.pantheon.HostApproval1.Reviewer"
ERROR_NAMESPACE: Final[str] = "org.pantheon.HostApproval1.Error"

ADAPTER_NAME: Final[str] = "pantheon-approval"
ADAPTER_CONTRACT_REVISION: Final[str] = "pantheon-approval-mcp-adapter/v1"
EXPECTED_RUNTIME: Final[Mapping[str, str]] = {"mcp": "2.3.0", "jeepney": "0.9.0"}
EXPECTED_PYTHON: Final[tuple[int, int]] = (3, 12)

JSON_SCHEMA_DIALECT: Final[str] = "https://json-schema.org/draft/2020-12/schema"

# Bounds. Every adapter bound is equal to or STRICTER than the landed
# approval-service validator it fronts (pinned by LandedParityTests):
# handles/digests -> ``_closed_hex`` (64 lowercase hex); idempotency key ->
# ``closed_record_string`` 1..128 (adapter: printable ASCII 1..128);
# audit anchor -> ``closed_record_string`` 1..256 (adapter: printable
# ASCII incl. space 1..256); decision/reason_code -> the closed sets in
# ``ApprovalService.decide`` / ``DECIDE_REASON_CODES``.
SCALAR_PATTERN: Final[str] = "^[!-~]{1,128}$"
HEX64_PATTERN: Final[str] = "^[0-9a-f]{64}$"
# D-Bus has no null: the wire uses "" for "absent" in two slots
# (Review's request_plan_handle = direct review; Decide's approval
# handle on a denial). The MCP schema keeps the wire's representation.
HEX64_OR_EMPTY_PATTERN: Final[str] = "^([0-9a-f]{64})?$"
ANCHOR_PATTERN: Final[str] = "^[ -~]{1,256}$"
DECISIONS: Final[tuple[str, ...]] = ("granted", "denied")
REASON_CODES: Final[tuple[str, ...]] = ("approved", "rejected")
JSON_LIMIT: Final[int] = 65536
DETAIL_LIMIT: Final[int] = 200

_SCALAR_RE = re.compile(r"[!-~]{1,128}\Z")
_HEX64_RE = re.compile(r"[0-9a-f]{64}\Z")
_HEX64_OR_EMPTY_RE = re.compile(r"(?:[0-9a-f]{64})?\Z")
_ANCHOR_RE = re.compile(r"[ -~]{1,256}\Z")

# Bounds on the adapter's own waits (seconds).
CONNECT_TIMEOUT: Final[float] = 2.0
READ_CALL_TIMEOUT: Final[float] = 10.0
WRITE_CALL_TIMEOUT: Final[float] = 30.0

# In-memory, per-process limiter. NOT a security control: any same-uid
# process can call the approval service over D-Bus directly.
RATE_BURST: Final[int] = 20
RATE_PER_SECOND: Final[float] = 5.0


class RateLimiter:
    """Per-process token bucket. In-memory only (not durable state) and
    NOT a security control: a same-uid process can reach the approval
    service over D-Bus directly."""

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
    """The closed set of Reviewer methods the adapter can ever call.

    ``bus_client`` accepts only members of this enum - there is no
    string-named method, interface, destination, or path parameter
    anywhere in the adapter (no generic D-Bus passthrough). The
    Consumer and HumanIngress interfaces are unreachable by construction.
    """

    REVIEW = ("Review", "sss", "ssss")
    REVIEW_GET = ("ReviewGet", "s", "ss")
    DECIDE = ("Decide", "ssssss", "ss")
    REQUEST_HUMAN = ("RequestHuman", "sss", "ss")

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
_HEX64_OR_EMPTY = "hex64_or_empty"
_ANCHOR = "anchor"
_DECISION = "decision"
_REASON_CODE = "reason_code"
_OBJECT = "object"


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


_AUTHORITY = (
    "The approval service authorizes THIS seat from root-owned approver "
    "policy on every call; registering this tool grants nothing."
)

TOOLS: Final[tuple[ToolSpec, ...]] = (
    ToolSpec(
        name="approval.review",
        method=Method.REVIEW,
        title="Open a review of a host plan",
        description=(
            "Open (or replay) a review of another seat's server-owned host "
            "plan by handle and expected digest. Returns a review handle, "
            "its state, the recomputed plan digest, and the bounded "
            "redacted plan summary. request_plan_handle is the requesting "
            "seat's own plan for a cross-persona handoff, or the empty "
            "string for a direct review. " + _AUTHORITY
        ),
        params=(
            ("plan_handle", _HEX64),
            ("expected_digest", _HEX64),
            ("request_plan_handle", _HEX64_OR_EMPTY),
        ),
        outputs=(
            ("review_handle", _HEX64),
            ("state", _SCALAR),
            ("plan_digest", _HEX64),
            ("summary", _OBJECT),
        ),
        read_only=False,
        destructive=False,
        idempotent=True,
    ),
    ToolSpec(
        name="approval.review.get",
        method=Method.REVIEW_GET,
        title="Get review state",
        description=(
            "Read the state and bounded non-secret summary of a review "
            "this seat may see. " + _AUTHORITY
        ),
        params=(("review_handle", _HEX64),),
        outputs=(("state", _SCALAR), ("summary", _OBJECT)),
        read_only=True,
        destructive=False,
        idempotent=True,
    ),
    ToolSpec(
        name="approval.decide",
        method=Method.DECIDE,
        title="Grant or deny a review",
        description=(
            "Decide an open review: decision 'granted' pairs with "
            "reason_code 'approved', 'denied' with 'rejected'. Returns the "
            "review state and an opaque approval handle ONLY when granted "
            "(the empty string on a denial). audit_anchor is bounded audit "
            "context, never authority. Reusing the same idempotency_key "
            "returns the same decision. " + _AUTHORITY
        ),
        params=(
            ("review_handle", _HEX64),
            ("expected_digest", _HEX64),
            ("decision", _DECISION),
            ("reason_code", _REASON_CODE),
            ("audit_anchor", _ANCHOR),
            ("idempotency_key", _SCALAR),
        ),
        outputs=(("state", _SCALAR), ("approval_handle", _HEX64_OR_EMPTY)),
        read_only=False,
        destructive=True,
        idempotent=True,
    ),
    ToolSpec(
        name="approval.request_human",
        method=Method.REQUEST_HUMAN,
        title="Request a human approval card",
        description=(
            "Ask the approval service to present a human approval card "
            "for a review through its separately supervised human "
            "ingress. Returns a request handle and state (pending until "
            "delivered, then presented). Requesting a card never grants "
            "anything: only the allowlisted human's provider-authenticated "
            "callback can decide; poll approval.review.get for the outcome "
            "and, once granted, the approval handle. "
            + _AUTHORITY
        ),
        params=(
            ("review_handle", _HEX64),
            ("expected_digest", _HEX64),
            ("idempotency_key", _SCALAR),
        ),
        outputs=(("request_handle", _HEX64), ("state", _SCALAR)),
        read_only=False,
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
    if kind == _HEX64_OR_EMPTY:
        return {"type": "string", "pattern": HEX64_OR_EMPTY_PATTERN, "maxLength": 64}
    if kind == _ANCHOR:
        return {"type": "string", "pattern": ANCHOR_PATTERN, "maxLength": 256}
    if kind == _DECISION:
        return {"type": "string", "enum": list(DECISIONS)}
    if kind == _REASON_CODE:
        return {"type": "string", "enum": list(REASON_CODES)}
    if kind == _OBJECT:
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
    """Hints only (ORG-052 §4 posture): server-side policy enforces."""

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
    "invalid_arguments": ErrorSpec(False, "arguments do not match the tool input schema; nothing was sent to the approval service"),
    "rate_limited": ErrorSpec(True, "adapter call rate exceeded; retry shortly"),
    "authority_unavailable": ErrorSpec(True, "approval service is not reachable; nothing was submitted; retry later - no harness restart is needed"),
    "authority_no_reply": ErrorSpec(True, "outcome unknown: the approval service did not reply; retry with the SAME idempotency_key or poll approval.review.get"),
    "authority_contract_mismatch": ErrorSpec(False, "approval service reply did not match the adapter contract; report to the operator"),
    "bus_access_denied": ErrorSpec(False, "system bus policy denied this call"),
    # approval closed namespace (T-2913 names, provisional)
    "approver_denied": ErrorSpec(False, "approver policy does not authorize this seat for this plan"),
    "handoff_mismatch": ErrorSpec(False, "the request-plan handoff does not match this plan"),
    "policy_denied": ErrorSpec(False, "approval service refused this seat"),
    "unknown_handle": ErrorSpec(False, "handle is unknown, expired, or not visible to this seat"),
    "idempotency_conflict": ErrorSpec(False, "idempotency_key was already used with different parameters"),
    "invalid_transition": ErrorSpec(False, "review is not in a state that allows this request"),
    "metadata_unavailable": ErrorSpec(True, "host broker plan metadata is not available; retry later"),
    "registry_denied": ErrorSpec(False, "approval registry refused the request; report to the operator"),
    "bind_denied": ErrorSpec(False, "approval binding was refused"),
    "failed": ErrorSpec(False, "approval service rejected the request"),
    "unsupported": ErrorSpec(False, "not supported by this approval service revision"),
    "internal": ErrorSpec(True, "approval service internal error; retry later or report to the operator"),
}

WIRE_ERROR_CODES: Final[Mapping[str, str]] = {
    f"{ERROR_NAMESPACE}.ApproverDenied": "approver_denied",
    f"{ERROR_NAMESPACE}.HandoffMismatch": "handoff_mismatch",
    f"{ERROR_NAMESPACE}.PolicyDenied": "policy_denied",
    f"{ERROR_NAMESPACE}.UnknownHandle": "unknown_handle",
    f"{ERROR_NAMESPACE}.IdempotencyConflict": "idempotency_conflict",
    f"{ERROR_NAMESPACE}.InvalidTransition": "invalid_transition",
    f"{ERROR_NAMESPACE}.MetadataUnavailable": "metadata_unavailable",
    f"{ERROR_NAMESPACE}.RegistryDenied": "registry_denied",
    # Never expected on the Reviewer interface (Consumer-only); mapped so
    # the mapping stays total over the closed namespace.
    f"{ERROR_NAMESPACE}.BindDenied": "bind_denied",
    f"{ERROR_NAMESPACE}.Failed": "failed",
    f"{ERROR_NAMESPACE}.Unsupported": "unsupported",
    f"{ERROR_NAMESPACE}.Internal": "internal",
}

# Bus-daemon errors meaning the call was NOT delivered to the service.
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

    The remote message travels ONLY for the approval service's closed
    namespace (server-authored bounded phrases or closed reason codes);
    bus-daemon and unknown messages are dropped entirely.
    """

    name = name if isinstance(name, str) else ""
    if name in WIRE_ERROR_CODES:
        detail = ""
        if isinstance(body, (tuple, list)) and body and isinstance(body[0], str):
            detail = body[0]
        return AdapterError(WIRE_ERROR_CODES[name], detail)
    if name in UNAVAILABLE_BUS_ERRORS or name.startswith(UNAVAILABLE_BUS_ERROR_PREFIX):
        return AdapterError("authority_unavailable")
    if name in NO_REPLY_BUS_ERRORS:
        return AdapterError("authority_no_reply")
    if name in ACCESS_DENIED_BUS_ERRORS:
        return AdapterError("bus_access_denied")
    return AdapterError("authority_contract_mismatch")


# --------------------------------------------------------------------
# input validation and result shaping
# --------------------------------------------------------------------

_MATCHERS: Final[Mapping[str, Callable[[str], bool]]] = {
    _SCALAR: lambda value: _SCALAR_RE.fullmatch(value) is not None,
    _HEX64: lambda value: _HEX64_RE.fullmatch(value) is not None,
    _HEX64_OR_EMPTY: lambda value: _HEX64_OR_EMPTY_RE.fullmatch(value) is not None,
    _ANCHOR: lambda value: _ANCHOR_RE.fullmatch(value) is not None,
    _DECISION: lambda value: value in DECISIONS,
    _REASON_CODE: lambda value: value in REASON_CODES,
}


def _matches(kind: str, value: object) -> bool:
    if not isinstance(value, str):
        return False
    matcher = _MATCHERS.get(kind)
    return matcher is not None and matcher(value)


def validate_arguments(tool: ToolSpec, arguments: object) -> tuple[str, ...]:
    """Adapter-side validation (the approval service remains authoritative).

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
        raise AdapterError("authority_contract_mismatch")
    try:
        value = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicates,
        )
    except (ValueError, RecursionError):
        raise AdapterError("authority_contract_mismatch") from None
    if not isinstance(value, dict):
        raise AdapterError("authority_contract_mismatch")
    return value


def shape_result(tool: ToolSpec, body: object) -> dict[str, Any]:
    """D-Bus reply body -> structuredContent, strictly per output schema."""

    if not isinstance(body, (tuple, list)) or len(body) != len(tool.outputs):
        raise AdapterError("authority_contract_mismatch")
    result: dict[str, Any] = {}
    for (name, kind), value in zip(tool.outputs, body):
        if kind == _OBJECT:
            result[name] = parse_bounded_object(value)
        elif _matches(kind, value):
            result[name] = value
        else:
            raise AdapterError("authority_contract_mismatch")
    return result


def canonical_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# --------------------------------------------------------------------
# self-check report (ORG-068 E10.C1 legibility path for a stdio
# component; same partial_by_design posture as the host adapter, T-2915)
# --------------------------------------------------------------------

HEALTH_CONTRACT_REVISION: Final[str] = "pantheon-health/v1"
SELF_CHECK_ROSTER: Final[tuple[str, ...]] = ("runtime", "system_bus", "approval_authority")


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
