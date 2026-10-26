"""Neutral typed port to a seat's user service manager (T-2917 5c.1, ORG-052 Unit 5).

This module is transport-neutral. It defines:

* the typed errors every user-manager adapter raises;
* the typed value model of a unit-state read (``UnitSnapshot``) and the
  closed set of read purposes (``ReadPurpose``), each with its exact,
  ordered property tuple;
* ``UserManagerPort``, the protocol both the operator command-line adapter
  (``tools.persona_installer.SystemctlUserManager``) and the 5c.2 user-bus
  adapter implement;
* a pure converter from native typed property values (as a bus layer
  delivers them after type checking) to ``UnitSnapshot``;
* per-property converters from the manager's ``show`` text form, used by the
  command-line adapter after it has split its stdout into key/value pairs;
* the pure classifiers the installer applies to a snapshot.

It deliberately imports no process-spawning or bus module and names no
transport-specific command, socket or environment variable: a 5c.2 AST
guard scans it. Transport text parsing (line splitting and its historical
strictness) lives with the command-line adapter; transport naming of the
text encoding lives there too.

Value fidelity rule: a property read from the text form becomes a typed
value only when the typed value renders back to the exact original text.
Otherwise it becomes ``NonCanonical(text)``, and every classifier applies
the historical string rule to that text, so a text value the typed model
cannot represent is still accepted or rejected exactly as before 5c.1, with
the historical error text.
"""

from __future__ import annotations

import enum
import hashlib
import json
import signal as _signal
from dataclasses import dataclass, field
from typing import Literal, Mapping, Protocol, Union

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------

_DETAIL_LIMIT = 512


def _bounded(text: str) -> str:
    text = str(text or "")
    return text if len(text) <= _DETAIL_LIMIT else text[:_DETAIL_LIMIT]


class UserManagerError(Exception):
    """Base of every typed user-manager failure.

    ``transport_evidence`` carries the adapter's own bounded evidence object
    (for the command-line adapter: the completed process), so a caller can
    keep its historical evidence dict byte-identical.
    """

    transport_evidence: object = None

    def __init__(self, message: str = "", *, transport_evidence: object = None) -> None:
        super().__init__(message)
        self.transport_evidence = transport_evidence


class UserManagerUnavailable(UserManagerError):
    """The user manager could not be reached; fail closed."""


class UserManagerCallFailed(UserManagerError):
    """The manager answered a call with an error (bounded detail)."""

    def __init__(
        self,
        error_name: str,
        detail: str = "",
        *,
        transport_evidence: object = None,
    ) -> None:
        bounded = _bounded(detail)
        super().__init__(
            f"{error_name}: {bounded}" if bounded else error_name,
            transport_evidence=transport_evidence,
        )
        self.error_name = error_name
        self.detail = bounded


class UserManagerTimeout(UserManagerError):
    """A user-manager call did not complete within its budget."""


class UserManagerProtocolError(UserManagerError):
    """The manager's answer did not match the typed model.

    ``reason`` is a short machine phrase (for text reads: "is malformed",
    "contains unexpected keys", "contains duplicate keys", "missing required
    keys") that callers prefix with their historical subject.
    """

    def __init__(self, message: str, *, reason: str = "", transport_evidence: object = None) -> None:
        super().__init__(message, transport_evidence=transport_evidence)
        self.reason = reason or message


class UnitStateRefused(Exception):
    """A classifier refused a unit state. The message is the historical
    installer refusal text; the installer re-raises it as its own
    ``OperationFailed`` with the identical message."""


# --------------------------------------------------------------------------
# Value types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NonCanonical:
    """A text-form property value with no exact typed rendering (for example
    a decimal with leading zeros, or an empty status). Classifiers apply the
    historical string rule to ``text``."""

    text: str


ElapseKind = Literal["unset", "infinity", "finite"]


@dataclass(frozen=True)
class Elapse:
    kind: ElapseKind
    usec: int | None = None

    def __post_init__(self) -> None:
        if self.kind not in ("unset", "infinity", "finite"):
            raise ValueError(f"invalid elapse kind {self.kind!r}")
        if self.kind != "finite" and self.usec is not None:
            raise ValueError("only a finite elapse carries usec")


@dataclass(frozen=True)
class ExitPolicy:
    codes: tuple[int, ...] = ()
    signals: tuple[str, ...] = ()


TYPED_CANONICAL_JSON = "typed-canonical-json"
# Text-form encodings are named by their adapter as "<tool>-show-stdout".
TEXT_SHOW_STDOUT_SUFFIX = "-show-stdout"


@dataclass(frozen=True)
class SnapshotSource:
    transport: str
    encoding: str
    length: int
    sha256: str

    def __post_init__(self) -> None:
        if self.encoding != TYPED_CANONICAL_JSON and not self.encoding.endswith(TEXT_SHOW_STDOUT_SUFFIX):
            raise ValueError(f"unknown snapshot encoding {self.encoding!r}")

    @property
    def is_text_stdout(self) -> bool:
        return self.encoding != TYPED_CANONICAL_JSON


@dataclass(frozen=True)
class UnitFileEnablement:
    state: str
    enabled: bool


@dataclass(frozen=True)
class ManagerState:
    reachable: bool
    # Manager.SystemState, e.g. "running" or "degraded"; bounded.
    system_state: str
    version: str = ""


@dataclass(frozen=True)
class UserManagerCallOutcome:
    """Result of a write call. ``ok`` False means the manager refused it;
    ``transport_evidence`` is the adapter's bounded evidence object."""

    ok: bool
    error_name: str = ""
    detail: str = ""
    transport_evidence: object = field(default=None, compare=False, repr=False)


# --------------------------------------------------------------------------
# Read purposes
# --------------------------------------------------------------------------


class ReadPurpose(enum.Enum):
    """The closed set of unit reads. Each purpose's property tuple is in the
    exact historical request order."""

    STALE_RESET = "stale-reset"
    UNIT_FILE_ACTIVE = "unit-file-active"
    UNIT_FIRE_POLL = "unit-fire-poll"
    SERVICE_SETTLE = "service-settle"
    SERVICE_OBSERVE = "service-observe"
    TIMER_ELAPSE = "timer-elapse"
    RECEIVER_ACTIVE = "receiver-active"
    RELOAD_STATE = "reload-state"
    SERVICE_BRINGUP_OBSERVE = "service-bringup-observe"

    @property
    def properties(self) -> tuple[str, ...]:
        return _PURPOSE_PROPERTIES[self]


_PURPOSE_PROPERTIES: dict[ReadPurpose, tuple[str, ...]] = {
    ReadPurpose.STALE_RESET: (
        "LoadState",
        "ActiveState",
        "SubState",
        "Result",
        "ExecMainStatus",
        "InvocationID",
        "Job",
    ),
    ReadPurpose.UNIT_FILE_ACTIVE: ("LoadState", "UnitFileState", "ActiveState", "SubState"),
    ReadPurpose.UNIT_FIRE_POLL: (
        "InvocationID",
        "ActiveState",
        "SubState",
        "Result",
        "ExecMainStatus",
        "SuccessExitStatus",
        "Job",
    ),
    ReadPurpose.SERVICE_SETTLE: (
        "InvocationID",
        "ActiveState",
        "Result",
        "ExecMainStatus",
        "SuccessExitStatus",
        "Job",
    ),
    ReadPurpose.SERVICE_OBSERVE: (
        "InvocationID",
        "ActiveState",
        "Result",
        "ExecMainStatus",
        "SuccessExitStatus",
    ),
    ReadPurpose.TIMER_ELAPSE: (
        "ActiveState",
        "SubState",
        "NextElapseUSecRealtime",
        "NextElapseUSecMonotonic",
    ),
    ReadPurpose.RECEIVER_ACTIVE: ("ActiveState", "MainPID"),
    ReadPurpose.RELOAD_STATE: ("NeedDaemonReload",),
    ReadPurpose.SERVICE_BRINGUP_OBSERVE: ("ActiveState", "SubState", "MainPID", "NRestarts"),
}

KNOWN_PROPERTIES = frozenset(name for props in _PURPOSE_PROPERTIES.values() for name in props)

# The documented exit-0 states of the unit-file enablement query; 5c.2 maps
# GetUnitFileState onto ``UnitFileEnablement.enabled`` with this set.
IS_ENABLED_ZERO_EXIT_STATES = frozenset(
    {"enabled", "enabled-runtime", "static", "alias", "indirect", "generated", "transient"}
)

ExecStatus = Union[int, NonCanonical]
JobValue = Union[int, NonCanonical]
PidValue = Union[int, NonCanonical]
PolicyValue = Union[ExitPolicy, NonCanonical]
BoolValue = Union[bool, NonCanonical]


@dataclass(frozen=True)
class UnitSnapshot:
    """A typed unit-state read. ``None`` means the property was not requested
    (or, for a lenient text read, not returned; ``present`` tells which)."""

    unit: str
    purpose: ReadPurpose
    source: SnapshotSource
    present: frozenset[str]
    load_state: str | None = None
    active_state: str | None = None
    sub_state: str | None = None
    unit_file_state: str | None = None
    result: str | None = None
    exec_main_status: ExecStatus | None = None
    success_exit: PolicyValue | None = None
    invocation_id: str | None = None
    job_id: JobValue | None = None
    main_pid: PidValue | None = None
    n_restarts: PidValue | None = None
    next_elapse_realtime: Elapse | None = None
    next_elapse_monotonic: Elapse | None = None
    need_daemon_reload: BoolValue | None = None

    @property
    def invocation_id_valid(self) -> bool:
        return valid_invocation_id(self.invocation_id or "")


_FIELD_FOR_PROPERTY = {
    "LoadState": "load_state",
    "ActiveState": "active_state",
    "SubState": "sub_state",
    "UnitFileState": "unit_file_state",
    "Result": "result",
    "ExecMainStatus": "exec_main_status",
    "SuccessExitStatus": "success_exit",
    "InvocationID": "invocation_id",
    "Job": "job_id",
    "MainPID": "main_pid",
    "NRestarts": "n_restarts",
    "NextElapseUSecRealtime": "next_elapse_realtime",
    "NextElapseUSecMonotonic": "next_elapse_monotonic",
    "NeedDaemonReload": "need_daemon_reload",
}


# --------------------------------------------------------------------------
# The port
# --------------------------------------------------------------------------


class UserManagerPort(Protocol):
    transport: str
    address: str | None
    address_source: str

    # reads
    def manager_state(self) -> ManagerState: ...

    def manager_environment(self) -> int: ...

    def read_unit(
        self,
        unit: str,
        purpose: ReadPurpose,
        *,
        timeout: float | None = None,
    ) -> UnitSnapshot: ...

    def unit_file_enablement(self, unit: str, *, timeout: float | None = None) -> UnitFileEnablement: ...

    def environment_file_declared(self, unit: str) -> bool: ...

    # writes
    def reload(self) -> UserManagerCallOutcome: ...

    def reset_failed_unit(self, unit: str) -> UserManagerCallOutcome: ...

    def start_unit(self, unit: str, *, wait: bool, timeout: float | None = None) -> UserManagerCallOutcome: ...

    def stop_unit(self, unit: str, *, timeout: float | None = None) -> UserManagerCallOutcome: ...

    def enable_now(self, unit: str) -> UserManagerCallOutcome: ...

    def disable_now(self, unit: str) -> UserManagerCallOutcome: ...


# --------------------------------------------------------------------------
# Small predicates
# --------------------------------------------------------------------------


def valid_invocation_id(value: str) -> bool:
    """32 lowercase hex characters (historical installer rule, unchanged)."""

    return len(value) == 32 and value.islower() and all(item in "0123456789abcdef" for item in value)


# A canonical decimal is at most 20 digits (2**64-1 has 20). Longer digit
# strings stay NonCanonical: int() on more than 4300 digits raises in
# Python 3.12 (the int/str conversion limit), and no property this module
# models can exceed uint64 (5c.1 review fixup 1).
_MAX_CANONICAL_DIGITS = 20


def _is_canonical_decimal(text: str) -> bool:
    return (
        0 < len(text) <= _MAX_CANONICAL_DIGITS
        and all(ch in "0123456789" for ch in text)
        and str(int(text)) == text
    )


def _legacy_canonical_positive_decimal(value: str) -> bool:
    # Historical rule, verbatim (including its str.isdigit semantics).
    return value.isdigit() and value != "0" and str(int(value)) == value


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------
# Text-form property converters and renderers
# --------------------------------------------------------------------------


def exec_status_from_text(text: str) -> ExecStatus:
    return int(text) if _is_canonical_decimal(text) else NonCanonical(text)


def job_from_text(text: str) -> JobValue:
    """"" is "no job" (0); a canonical positive decimal is a job id; anything
    else (including "0") is NonCanonical."""

    if text == "":
        return 0
    if _is_canonical_decimal(text) and text != "0":
        return int(text)
    return NonCanonical(text)


def main_pid_from_text(text: str) -> PidValue:
    return int(text) if _is_canonical_decimal(text) else NonCanonical(text)


def n_restarts_from_text(text: str) -> PidValue:
    return int(text) if _is_canonical_decimal(text) else NonCanonical(text)


def render_exit_policy(policy: ExitPolicy) -> str:
    return " ".join([str(code) for code in policy.codes] + list(policy.signals))


def exit_policy_from_text(text: str) -> PolicyValue:
    codes: list[int] = []
    signals: list[str] = []
    for token in text.split():
        if _is_canonical_decimal(token) and not signals:
            codes.append(int(token))
            continue
        try:
            int(token)
        except ValueError:
            signals.append(token)
            continue
        return NonCanonical(text)
    policy = ExitPolicy(tuple(codes), tuple(signals))
    if render_exit_policy(policy) != text:
        return NonCanonical(text)
    return policy


_ELAPSE_UNSET_TEXT = frozenset({"", "0", "n/a"})


def elapse_from_text(text: str) -> Elapse:
    normalized = text.strip().lower()
    if normalized in _ELAPSE_UNSET_TEXT:
        return Elapse("unset")
    if normalized == "infinity":
        return Elapse("infinity")
    return Elapse("finite", int(text) if _is_canonical_decimal(text) else None)


def bool_from_text(text: str) -> BoolValue:
    if text == "yes":
        return True
    if text == "no":
        return False
    return NonCanonical(text)


def render_exec_status(value: ExecStatus | None) -> str:
    if value is None:
        return ""
    if isinstance(value, NonCanonical):
        return value.text
    return str(value)


def render_job(value: JobValue | None) -> str:
    if value is None or (_is_int(value) and value == 0):
        return ""
    if isinstance(value, NonCanonical):
        return value.text
    return str(value)


def render_main_pid(value: PidValue | None) -> str:
    if value is None:
        return ""
    if isinstance(value, NonCanonical):
        return value.text
    return str(value)


def render_success_exit(value: PolicyValue | None) -> str:
    if value is None:
        return ""
    if isinstance(value, NonCanonical):
        return value.text
    return render_exit_policy(value)


_TEXT_CONVERTERS = {
    "LoadState": str,
    "ActiveState": str,
    "SubState": str,
    "UnitFileState": str,
    "Result": str,
    "InvocationID": str,
    "ExecMainStatus": exec_status_from_text,
    "SuccessExitStatus": exit_policy_from_text,
    "Job": job_from_text,
    "MainPID": main_pid_from_text,
    "NRestarts": n_restarts_from_text,
    "NextElapseUSecRealtime": elapse_from_text,
    "NextElapseUSecMonotonic": elapse_from_text,
    "NeedDaemonReload": bool_from_text,
}


def unit_snapshot_from_text_values(
    unit: str,
    purpose: ReadPurpose,
    values: Mapping[str, str],
    *,
    source: SnapshotSource,
) -> UnitSnapshot:
    """Typed conversion of already-split text-form key/value pairs. Keys must
    be a subset of the purpose's properties (the adapter filters first)."""

    allowed = set(purpose.properties)
    unexpected = set(values) - allowed
    if unexpected:
        raise UserManagerProtocolError(
            "unit read returned properties outside its purpose",
            reason="contains unexpected keys",
        )
    fields: dict[str, object] = {}
    for name, text in values.items():
        fields[_FIELD_FOR_PROPERTY[name]] = _TEXT_CONVERTERS[name](text)
    return UnitSnapshot(
        unit=unit,
        purpose=purpose,
        source=source,
        present=frozenset(values),
        **fields,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------
# Native typed converter (5c.2's bus layer calls this)
# --------------------------------------------------------------------------

ELAPSE_INFINITY_USEC = 2**64 - 1


def _native_elapse(name: str, value: object) -> Elapse:
    if not _is_int(value) or value < 0 or value > ELAPSE_INFINITY_USEC:  # type: ignore[operator]
        raise UserManagerProtocolError(f"{name} is not a uint64")
    if value == 0:
        return Elapse("unset")
    if value == ELAPSE_INFINITY_USEC:
        return Elapse("infinity")
    return Elapse("finite", int(value))  # type: ignore[arg-type]


def _native_invocation_id(value: object) -> str:
    if not isinstance(value, (bytes, bytearray)):
        raise UserManagerProtocolError("InvocationID is not a byte array")
    raw = bytes(value)
    if len(raw) == 0 or raw == b"\x00" * 16:
        return ""
    if len(raw) != 16:
        raise UserManagerProtocolError("InvocationID is not 0 or 16 bytes")
    return raw.hex()


def _signal_name(value: object) -> str:
    if isinstance(value, str) and value:
        return value
    if _is_int(value):
        try:
            return _signal.Signals(value).name  # type: ignore[arg-type]
        except ValueError:
            return str(value)
    raise UserManagerProtocolError("SuccessExitStatus signal is not an int or name")


def _native_exit_policy(value: object) -> ExitPolicy:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise UserManagerProtocolError("SuccessExitStatus is not a (codes, signals) pair")
    codes, signals = value
    if not isinstance(codes, (tuple, list)) or not all(_is_int(code) for code in codes):
        raise UserManagerProtocolError("SuccessExitStatus codes are not ints")
    if not isinstance(signals, (tuple, list)):
        raise UserManagerProtocolError("SuccessExitStatus signals are not a list")
    return ExitPolicy(tuple(int(code) for code in codes), tuple(_signal_name(item) for item in signals))


def _native_job(value: object) -> int:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise UserManagerProtocolError("Job is not a (id, path) pair")
    job_id, path = value
    if not _is_int(job_id) or job_id < 0 or not isinstance(path, str):
        raise UserManagerProtocolError("Job is not a (uint32, object path) pair")
    return int(job_id)


def _native_int(name: str) -> object:
    def convert(value: object) -> int:
        if not _is_int(value):
            raise UserManagerProtocolError(f"{name} is not an int")
        return int(value)  # type: ignore[arg-type]

    return convert


def _native_uint(name: str) -> object:
    """A D-Bus ``u`` (uint32) property: an int in [0, 2**32)."""

    def convert(value: object) -> int:
        if not _is_int(value) or value < 0 or value > 0xFFFFFFFF:  # type: ignore[operator]
            raise UserManagerProtocolError(f"{name} is not a uint32")
        return int(value)  # type: ignore[arg-type]

    return convert


def _native_str(name: str) -> object:
    def convert(value: object) -> str:
        if not isinstance(value, str):
            raise UserManagerProtocolError(f"{name} is not a string")
        return value

    return convert


def _native_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise UserManagerProtocolError("NeedDaemonReload is not a bool")
    return value


_NATIVE_CONVERTERS = {
    "LoadState": _native_str("LoadState"),
    "ActiveState": _native_str("ActiveState"),
    "SubState": _native_str("SubState"),
    "UnitFileState": _native_str("UnitFileState"),
    "Result": _native_str("Result"),
    "InvocationID": _native_invocation_id,
    "ExecMainStatus": _native_int("ExecMainStatus"),
    "SuccessExitStatus": _native_exit_policy,
    "Job": _native_job,
    "MainPID": _native_int("MainPID"),
    "NRestarts": _native_uint("NRestarts"),
    "NextElapseUSecRealtime": lambda value: _native_elapse("NextElapseUSecRealtime", value),
    "NextElapseUSecMonotonic": lambda value: _native_elapse("NextElapseUSecMonotonic", value),
    "NeedDaemonReload": _native_bool,
}


def _typed_json(value: object) -> object:
    if isinstance(value, Elapse):
        return {"kind": value.kind, "usec": value.usec}
    if isinstance(value, ExitPolicy):
        return {"codes": list(value.codes), "signals": list(value.signals)}
    if isinstance(value, NonCanonical):
        return {"non_canonical": value.text}
    return value


def typed_canonical_json(unit: str, purpose: ReadPurpose, fields: Mapping[str, object]) -> bytes:
    document = {
        "unit": unit,
        "purpose": purpose.value,
        "properties": {name: _typed_json(value) for name, value in fields.items()},
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def unit_snapshot_from_native(
    unit: str,
    purpose: ReadPurpose,
    values: Mapping[str, object],
    *,
    transport: str,
) -> UnitSnapshot:
    """Convert native typed property values (str, int, bytes for
    InvocationID, (job_id, path) for Job, (codes, signals) for
    SuccessExitStatus, int usec for NextElapse*, bool) into a UnitSnapshot.

    Every key must be one of the purpose's properties; a value of the wrong
    native type raises UserManagerProtocolError. The source digest is the
    sha256 of the canonical JSON of the typed values.
    """

    allowed = set(purpose.properties)
    unexpected = sorted(set(values) - allowed)
    if unexpected:
        raise UserManagerProtocolError(
            f"unit read returned properties outside its purpose: {', '.join(unexpected)}",
            reason="contains unexpected keys",
        )
    typed: dict[str, object] = {}
    for name in purpose.properties:
        if name in values:
            typed[name] = _NATIVE_CONVERTERS[name](values[name])  # type: ignore[operator]
    raw = typed_canonical_json(unit, purpose, typed)
    source = SnapshotSource(
        transport=transport,
        encoding=TYPED_CANONICAL_JSON,
        length=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    return UnitSnapshot(
        unit=unit,
        purpose=purpose,
        source=source,
        present=frozenset(typed),
        **{_FIELD_FOR_PROPERTY[name]: value for name, value in typed.items()},  # type: ignore[arg-type]
    )


def environment_file_declared_from_native(value: object) -> bool:
    """EnvironmentFiles arrives as a list of (path, ignore-errors) pairs."""

    if not isinstance(value, (tuple, list)) or not all(
        isinstance(item, (tuple, list)) and len(item) == 2 and isinstance(item[0], str) and isinstance(item[1], bool)
        for item in value
    ):
        raise UserManagerProtocolError("EnvironmentFiles is not a list of (path, bool) pairs")
    return len(value) > 0


def unit_file_enablement_from_state(state: str) -> UnitFileEnablement:
    return UnitFileEnablement(state=state, enabled=state in IS_ENABLED_ZERO_EXIT_STATES)


# --------------------------------------------------------------------------
# Evidence digest keys
# --------------------------------------------------------------------------


def snapshot_digest_evidence(prefix: str, source: SnapshotSource) -> dict[str, str]:
    """Digest keys for a snapshot's source. A text-stdout source keeps the
    historical ``{prefix}_stdout_length`` / ``{prefix}_stdout_sha256`` keys;
    a typed source is labelled as a snapshot, never as stdout."""

    lead = f"{prefix}_" if prefix else ""
    if source.is_text_stdout:
        return {
            f"{lead}stdout_length": str(source.length),
            f"{lead}stdout_sha256": source.sha256,
        }
    return {
        f"{lead}snapshot_length": str(source.length),
        f"{lead}snapshot_sha256": source.sha256,
        f"{lead}snapshot_encoding": source.encoding,
    }


# --------------------------------------------------------------------------
# Classifiers (pure; typed values in, historical verdicts and texts out)
# --------------------------------------------------------------------------

STALE_LOADED_REFUSAL = "pre-existing loaded service"
STALE_UNSUPPORTED_REFUSAL = "unsupported stale unit state"
TIMER_UNSUPPORTED_REFUSAL = "timer show output has unsupported load/unit-file state"


def _is_exact_int(value: object, expected: int) -> bool:
    return _is_int(value) and value == expected


def _canonical_positive_status(value: ExecStatus | None) -> bool:
    if value is None:
        return False
    if isinstance(value, NonCanonical):
        return _legacy_canonical_positive_decimal(value.text)
    return _is_int(value) and value > 0


def classify_stale_reset(snapshot: UnitSnapshot) -> Literal["clean-noop", "reset-required"]:
    """Stale unit-failure reset classification (pre-5c.1 rule, same order and
    short-circuiting). Raises UnitStateRefused with the historical text."""

    if (
        snapshot.load_state == "not-found"
        and snapshot.active_state == "inactive"
        and snapshot.sub_state == "dead"
        and snapshot.result == "success"
        and _is_exact_int(snapshot.exec_main_status, 0)
        and snapshot.invocation_id == ""
        and _is_exact_int(snapshot.job_id, 0)
    ):
        return "clean-noop"
    if snapshot.load_state == "loaded":
        raise UnitStateRefused(STALE_LOADED_REFUSAL)
    if (
        snapshot.load_state == "not-found"
        and snapshot.active_state == "failed"
        and snapshot.sub_state == "failed"
        and snapshot.result == "exit-code"
        and _canonical_positive_status(snapshot.exec_main_status)
        and valid_invocation_id(snapshot.invocation_id or "")
        and _is_exact_int(snapshot.job_id, 0)
    ):
        return "reset-required"
    raise UnitStateRefused(STALE_UNSUPPORTED_REFUSAL)


def classify_timer_unit_file(snapshot: UnitSnapshot) -> Literal["enabled", "disabled", "not-found"]:
    """Strict timer classification (UNIT_FILE_ACTIVE)."""

    if snapshot.load_state == "loaded" and snapshot.unit_file_state in {"enabled", "disabled"}:
        return "enabled" if snapshot.unit_file_state == "enabled" else "disabled"
    if snapshot.load_state == "not-found" and snapshot.unit_file_state in {"", "not-found"}:
        return "not-found"
    raise UnitStateRefused(TIMER_UNSUPPORTED_REFUSAL)


def classify_unit_file_presence(snapshot: UnitSnapshot) -> Literal["enabled", "disabled", "not-found"]:
    """Lenient presence classification used by the inactive/dead proofs."""

    if snapshot.load_state == "not-found":
        return "not-found"
    return "enabled" if snapshot.unit_file_state == "enabled" else "disabled"


def proves_inactive_dead(snapshot: UnitSnapshot) -> bool:
    return snapshot.active_state == "inactive" and snapshot.sub_state == "dead"


def proves_notfound_inactive_dead(snapshot: UnitSnapshot) -> bool:
    return (
        snapshot.load_state == "not-found"
        and snapshot.unit_file_state in {"", "not-found"}
        and snapshot.active_state == "inactive"
        and snapshot.sub_state == "dead"
    )


def exit_status_within_policy(exec_status: ExecStatus | None, policy: PolicyValue | None) -> bool:
    """True if an exit code is 0 or one of the policy's exit codes. Signal
    names never match. A missing or non-numeric status is not in policy."""

    if exec_status is None:
        return False
    if isinstance(exec_status, NonCanonical):
        try:
            code = int(exec_status.text)
        except (TypeError, ValueError):
            return False
    elif _is_int(exec_status):
        code = int(exec_status)
    else:
        return False
    if code == 0:
        return True
    allowed: set[int] = set()
    if isinstance(policy, NonCanonical):
        for token in (policy.text or "").split():
            try:
                allowed.add(int(token))
            except ValueError:
                continue
    elif isinstance(policy, ExitPolicy):
        allowed.update(policy.codes)
    return code in allowed


def _exec_status_present(value: ExecStatus | None) -> bool:
    if value is None:
        return False
    if isinstance(value, NonCanonical):
        return value.text != ""
    return True


def unit_fire_has_failed(snapshot: UnitSnapshot) -> bool:
    """Failed state, a non-success Result, or a present exit outside policy."""

    return (
        snapshot.active_state == "failed"
        or ((snapshot.result or "") not in {"", "success"})
        or (
            _exec_status_present(snapshot.exec_main_status)
            and not exit_status_within_policy(snapshot.exec_main_status, snapshot.success_exit)
        )
    )


_UNIT_FIRE_NO_JOB_TEXT = frozenset({"", "0", "n/a", "none", "None"})


def unit_fire_job_idle(job: JobValue | None) -> bool:
    if job is None:
        return True
    if isinstance(job, NonCanonical):
        return job.text.strip() in _UNIT_FIRE_NO_JOB_TEXT
    return job == 0


def unit_fire_idle_before_enqueue(snapshot: UnitSnapshot) -> bool:
    return (
        snapshot.active_state in {"inactive", "failed"}
        and not snapshot.invocation_id
        and unit_fire_job_idle(snapshot.job_id)
    )


def finite_next_elapse_basis(
    realtime: Elapse | None,
    monotonic: Elapse | None,
) -> Literal["realtime", "monotonic"] | None:
    """The clock basis with a finite scheduled next elapse, realtime first."""

    if realtime is not None and realtime.kind == "finite":
        return "realtime"
    if monotonic is not None and monotonic.kind == "finite":
        return "monotonic"
    return None


def snapshot_finite_next_elapse_basis(snapshot: UnitSnapshot) -> Literal["realtime", "monotonic"] | None:
    return finite_next_elapse_basis(snapshot.next_elapse_realtime, snapshot.next_elapse_monotonic)


SERVICE_IN_FLIGHT_STATES = frozenset({"activating", "active", "deactivating"})


def _job_queued(job: JobValue | None) -> bool:
    if job is None:
        return False
    if isinstance(job, NonCanonical):
        return bool(job.text.strip())
    return job != 0


def service_in_flight(active_state: str | None, job: JobValue | None) -> bool:
    """A non-terminal ActiveState or a queued job means mid-fire."""

    if (active_state or "") in SERVICE_IN_FLIGHT_STATES:
        return True
    return _job_queued(job)


def snapshot_service_in_flight(snapshot: UnitSnapshot) -> bool:
    return service_in_flight(snapshot.active_state, snapshot.job_id)


def receiver_idle(active_state: str | None, main_pid: PidValue | None) -> bool:
    """Receiver is inactive/failed with no main process."""

    if (active_state or "") not in {"inactive", "failed"}:
        return False
    if main_pid is None:
        return True
    if isinstance(main_pid, NonCanonical):
        return main_pid.text in {"", "0"}
    return main_pid == 0
