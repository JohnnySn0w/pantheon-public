"""The executor's closed operation loop (T-2917 5b).

Two halves:

* ``parse_spec`` validates the spec the executor fetched from the broker's
  ``ExecutionSource``: canonical bytes, the digest RECOMPUTED here, the
  closed schema, the target seat resolving to THIS executor's uid through
  the root-owned registry, every operation owned by this seat's account,
  the executor envelope, and the classes this revision implements.
* ``ExecutorEngine.run`` executes the parsed operations through a closed
  per-kind handler table and returns ONLY typed mutation and evidence
  records. It never writes a receipt and never touches the broker journal.

The user manager is reached ONLY through ``UserManagerPort`` (in the
executor: the user-bus ``DBusUserManager``), never through the operator
command-line client and its private control socket (the founding incident,
gate 39) and never through a bus named by the environment; no git either.
The one child process is direct-fire's diagnose run: ``sys.executable`` on
the root-owned framework copy, never a shell and never a seat checkout.
Repo-sourced checks read ONLY the root-owned framework copy through
``RootOwnedFileLoader`` and refuse a target inside the seat root (HO ruling
T-4861: never a seat checkout).

Activation (T-2917 5c.2, ``user_systemd.activate``): an apply first runs a
read-only PRE-PASS over every operation (the installer's preflight
gates: manager reachable, timer/link states, the B9 legacy-convergence
branch, the receiver checks), and nothing is changed unless every gate
passes. Every unit named in a port call must be named by the plan AND match
the unit allowlist. One execution is capped at
``EXECUTOR_RUN_BUDGET_SECONDS``.

Verify is read-only by construction: diagnostics run as-is, seat-root file
kinds are only observed, activation kinds observe live state through the
port's read methods (direct-fire re-runs diagnose, ruling D3), and no
mutation record is produced.

T-2917 5d.2 adds two execution kinds:

* ``reconcile`` (ruling T-2917:D13): the read-only live-state probe of an
  apply whose result was lost. Per operation it reports the target's live
  observation (file state, fingerprint, at-desired; a timer-enable's link
  proof and unit classification); it never changes anything and returns no
  mutation record.
* ``rollback`` (ruling T-2917:D15): restores the receipt-owned changed
  mutations the broker's spec carries, mirroring the installer's
  ``InstallerFlow.rollback`` / ``rollback_mutation``: every mutation is
  classified first (owned / not-applied / drifted) and ANY drift refuses the
  whole rollback before any change; then owned mutations are restored in
  reverse order (file bytes and mode from the before-snapshot; a created
  file unlinked; a created directory removed only if empty; an owned
  timer-enable undone through the port with the installer's exact
  sequence). A timer-enable without the executor's owned record is refused
  again here (D6). Each restore is returned as a mutation record (before =
  the pre-restore snapshot) with the same never-drop guarantee as apply.

T-2917 5d.3 implements ``mailbox.probe`` (``executor/mailbox_probe.py``):
the round-trip and writable-root probes run with installer parity, confined
to the ROOT-CONFIGURED mailbox hub (ruling T-2917:D19). Apply records a
``changed=False`` mutation (the probe files are transient); verify re-runs
the live probe (ADR section 4: verify "may run active probes") and records
none; the apply pre-pass refuses any mailbox target outside the hub before
anything changes; the reconcile probe reports deterministic probe residue.
Without a configured hub every mailbox operation fails closed.
"""

from __future__ import annotations

import base64
import grp
import hashlib
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final, Mapping

from tools.host_control.contracts import (
    RECORD_STRING_MAX,
    RECORD_TARGET_MAX,
    HostControlError,
    PolicyDenied,
    RegistryError,
    Unsupported,
    _mutation_from_dict,
    canonical_json,
    closed_record_string,
    sha256_json,
)
from tools.host_control.envelope import (
    EXECUTOR_KIND_CLASSES,
    require_executor_envelope,
    require_revision_implemented,
)
from tools.host_control.executor.identity import ExecutorIdentity
from tools.host_control.executor.mailbox_probe import (
    ROUND_TRIP_KIND,
    MailboxProbe,
)
from tools.host_control.executor.seat_files import (
    SeatChangeIncomplete,
    SeatFiles,
    SeatPathRefused,
    parse_mode,
)
from tools.host_control.executor.user_bus import DEFAULT_CALL_TIMEOUT
from tools.host_control.executor.user_manager import UserManagerPort
from tools.host_control.executor.wire import (
    EVIDENCE_RESERVE_BYTES,
    EXECUTOR_RUN_BUDGET_SECONDS,
    RECORD_BUDGET_BYTES,
    ExecutionResult,
    ExecutorDenied,
    InvalidSpec,
    record_size,
)
from tools.host_control.registry import SeatRegistry
from tools.host_control.trusted_files import RootOwnedFileLoader
from tools.persona_installer_contracts import (
    InstallOperation,
    MutationRecord,
    OperationEvidence,
    OperationFailed,
    _sha256_bytes,
)
from tools.persona_installer_activation import (
    LEGACY_CONVERGENCE_SERVICE_SETTLE_POLL_INTERVAL_SECONDS,
    LEGACY_CONVERGENCE_SERVICE_SETTLE_WINDOW_SECONDS,
    RUNTIME_VIABILITY_TEARDOWN_MARGIN_SECONDS,
    UNIT_FIRE_POLL_INTERVAL_SECONDS,
    TimerEnablementLinkProof,
    _claude_mcp_verdict,
    _classify_stale_unit_failure_reset_snapshot,
    _consume_timer_enablement_link_path,
    _consume_timer_template_path,
    _is_installer_managed,
    _lexically_normalize_absolute,
    _parse_unit_fire_initial_viability_desired,
    _parse_unit_fire_timeout_budget,
    _resolve_timer_enablement_readlink,
    _snapshot_fingerprint,
    _stale_unit_failure_reset_failure,
    _stale_unit_failure_reset_snapshot_evidence,
    _timer_enablement_link_evidence,
    _timer_enablement_link_matches_canonical,
    _timer_unit_state_from_snapshot,
)
from tools import runtime_invocation_receipt as _receipt
from tools import user_systemd as _us
from tools.user_systemd import ReadPurpose, UnitSnapshot

# Transcribed from tools.host_control.execution_source (the executor must
# not import that module: it imports the broker journal). A test pins it.
EXECUTION_SPEC_SCHEMA: Final = "pantheon-host-execution-spec/v1"
EXECUTION_KINDS: Final = frozenset(("apply", "verify", "rollback", "reconcile"))
_SPEC_KEYS: Final = frozenset(
    (
        "schema",
        "operation_handle",
        "execution_kind",
        "target_seat_id",
        "plan_handle",
        "plan_digest",
        "effect_digest",
        "manifest_revision",
        "framework_revision",
        "receipt_handle",
        "receipt_digest",
        "allowed_roots",
        "operations",
        "rollback_mutations",
    )
)
# T-2917:D28: a reconcile spec carries exactly one more key, its journal-
# derived probe nonce (transcribed from execution_source; a test pins it)
_RECONCILE_SPEC_KEYS: Final = _SPEC_KEYS | {"probe_nonce"}
MAX_PROBE_NONCE: Final = 2**31 - 1
_OPERATION_KEYS: Final = frozenset(
    (
        "operation_id",
        "layer",
        "kind",
        "target",
        "precondition",
        "desired",
        "inverse",
        "privilege",
        "owner",
        "provenance",
        "execution_scope",
    )
)
_MAX_OPERATIONS: Final = 256
_DETAIL_LIMIT: Final = 200
_FRAMEWORK_READ_LIMIT: Final = 4 * 1024 * 1024
_FILE_KINDS: Final = frozenset(
    ("atomic-write", "user-unit-install", "codex-standing-instructions-install")
)
DEFAULT_LINGER_DIR: Final = Path("/var/lib/systemd/linger")
ACTIVATION_KINDS: Final = frozenset(
    ("daemon-reload", "stale-unit-failure-reset", "direct-fire", "unit-fire", "timer-enable", "timer-observe")
)
# The per-kind unit allowlist (T-2917 5c.2, review M1): each (kind, field)
# that names a unit for the port may name ONLY the unit shapes listed here,
# and the unit must also be the one THIS operation names. The persona
# consume service/timer shapes serve the persona kinds; the codex receiver
# is reachable ONLY through disabled-first's inspection and the daemon-reload
# unit read. Enforced at parse time (spec refused) and at every call.
UNIT_SHAPES: Final = {
    "persona-service": re.compile(r"pantheon-persona-consume@[a-z0-9][a-z0-9_-]{0,63}\.service\Z"),
    "persona-timer": re.compile(r"pantheon-persona-consume@[a-z0-9][a-z0-9_-]{0,63}\.timer\Z"),
    "receiver": re.compile(r"codex-telegram-poller\.service\Z"),
}
UNIT_ROLES: Final = {
    ("stale-unit-failure-reset", "target"): frozenset({"persona-service"}),
    ("unit-fire", "target"): frozenset({"persona-service"}),
    ("unit-fire", "timer"): frozenset({"persona-timer"}),
    ("timer-enable", "target"): frozenset({"persona-timer"}),
    ("timer-observe", "target"): frozenset({"persona-timer"}),
    ("timer-observe", "service"): frozenset({"persona-service"}),
    ("codex-telegram-disabled-first", "target"): frozenset({"receiver"}),
    ("daemon-reload", "unit"): frozenset({"receiver"}),
    ("timer-enablement-absence-check", "timer"): frozenset({"persona-timer"}),
}
# Kinds whose named units are checked at PARSE time. The 5b
# timer-enablement-absence-check keeps its 5b admission: its timer reaches
# the port only on the B9 present-link branch, where the call-time check
# applies the same role table.
_PARSE_CHECKED_KINDS: Final = frozenset(kind for kind, _field in UNIT_ROLES) - {"timer-enablement-absence-check"}
DIRECT_FIRE_TIMEOUT_SECONDS: Final = 120.0
# Review M2: the diagnose child runs ISOLATED (``-I``: no user site, no
# PYTHON* variables, no script dir or cwd on sys.path; persona_bringup.py
# bootstraps its own repo root from __file__) with an explicit minimal
# environment: nothing is inherited from the executor process. Diagnose
# resolves harness binaries through PATH (shutil.which) and the account
# home through HOME; it reads no runtime-dir variable, so none is passed.
DIRECT_FIRE_PATH: Final = "/usr/local/bin:/usr/bin:/bin"
DIRECT_FIRE_ENV_KEYS: Final = ("PATH", "HOME", "LANG")
_PERSONA_NAME: Final = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_DIRECT_FIRE_HARNESSES: Final = frozenset(("claude", "codex"))


class UnitRefused(HostControlError):
    """A port call named a unit outside the plan-named allowlist."""


def unit_shape_ok(unit: object, shapes: frozenset[str]) -> bool:
    return isinstance(unit, str) and any(UNIT_SHAPES[shape].fullmatch(unit) for shape in shapes)


def role_units(operation: "InstallOperation") -> tuple[tuple[str, str], ...]:
    """(field, unit) for every unit field this operation names."""

    named = []
    for kind, field in UNIT_ROLES:
        if kind != operation.kind:
            continue
        value = operation.target if field == "target" else operation.desired.get(field, "")
        if value:
            named.append((field, value))
    return tuple(named)


_CONSUME_INSTANCE: Final = re.compile(r"pantheon-persona-consume@(?P<instance>[a-z0-9][a-z0-9_-]{0,63})\.(service|timer)\Z")


def _paired(timer: str) -> str:
    return timer[: -len(".timer")] + ".service" if timer.endswith(".timer") else ""


def consume_instance_problem(operations: "tuple[InstallOperation, ...]") -> str:
    """Review N1(a): every persona consume unit a spec names belongs to ONE
    consume instance, unit-fire's target is the service its timer fires, and
    timer-observe's service is the service its timer fires. Empty when fine."""

    instances: set[str] = set()
    for operation in operations:
        for (kind, field), shapes in UNIT_ROLES.items():
            if kind != operation.kind or "receiver" in shapes:
                continue
            unit = operation.target if field == "target" else operation.desired.get(field, "")
            match = _CONSUME_INSTANCE.fullmatch(unit or "")
            if match:
                instances.add(match.group("instance"))
        if operation.kind == "unit-fire" and operation.target != _paired(operation.desired.get("timer", "")):
            return "unit-fire target is not the service its timer fires"
        if operation.kind == "timer-observe" and operation.desired.get("service", "") != _paired(operation.target):
            return "timer-observe service is not the service its timer fires"
    if len(instances) > 1:
        return "spec names more than one consume instance: " + ",".join(sorted(instances))
    return ""


def refused_role_units(operations: "tuple[InstallOperation, ...]") -> tuple[str, ...]:
    """Parse-time M1 check: every unit a parse-checked kind names must match
    its (kind, field) shapes."""

    refused = []
    for operation in operations:
        if operation.kind not in _PARSE_CHECKED_KINDS:
            continue
        for field, unit in role_units(operation):
            if not unit_shape_ok(unit, UNIT_ROLES[(operation.kind, field)]):
                refused.append(f"{operation.kind}.{field}={unit}")
    return tuple(sorted(refused))


CODEX_INSTALL_KIND: Final = "codex-standing-instructions-install"
CODEX_SOURCE_CHECK_KIND: Final = "codex-standing-instructions-source-check"
CODEX_TARGET_CHECK_KIND: Final = "codex-standing-instructions-target-check"


def codex_pin_problem(operations: "tuple[InstallOperation, ...]") -> str:
    """Carry-in N5d, ruling T-2917:D23, the PARSE half: the Codex standing
    instructions install, its source check and its target check pin ONE
    hash. The install's ``desired.sha256`` must equal every source check's
    ``desired.source_sha256`` and every target check's ``desired.sha256``,
    and a spec carries at most one install. Empty when fine.

    A spec without a source check still parses (a verify-only spec, the 5b
    unit tests); the APPLY half refuses such an install, because it needs
    the hash the source check observed on the framework copy in this run."""

    installs = [op for op in operations if op.kind == CODEX_INSTALL_KIND]
    if not installs:
        return ""
    if len(installs) > 1:
        return "spec carries more than one Codex standing instructions install"
    pin = installs[0].desired.get("sha256", "")
    for operation in operations:
        if operation.kind == CODEX_SOURCE_CHECK_KIND and operation.desired.get("source_sha256", "") != pin:
            return "Codex install pin does not equal the source check's source_sha256"
        if operation.kind == CODEX_TARGET_CHECK_KIND and operation.desired.get("sha256", "") != pin:
            return "Codex install pin does not equal the target check's sha256"
    return ""


class ResultBudgetExceeded(HostControlError):
    """The operation's record would not fit the remaining result budget;
    refused BEFORE anything is committed (review R1)."""


@dataclass(frozen=True)
class ParsedSpec:
    operation_handle: str
    execution_kind: str
    spec_digest: str
    operations: tuple[InstallOperation, ...]
    # rollback only (T-2917 5d.2): the receipt mutations to restore
    rollback_mutations: tuple[MutationRecord, ...] = ()


def _rollback_mutations(
    raw: object,
    operations: tuple[InstallOperation, ...],
    account: str,
) -> tuple[MutationRecord, ...]:
    """Decode a rollback spec's mutations with the broker's own record
    decoder (N1 bounds) and bind each to a spec operation with the same
    id, kind, target and owner (the seat account); only changed records."""

    if not isinstance(raw, list) or not 1 <= len(raw) <= _MAX_OPERATIONS:
        raise InvalidSpec("invalid rollback mutation list")
    by_id = {operation.operation_id: operation for operation in operations}
    mutations = []
    for item in raw:
        try:
            mutation = _mutation_from_dict(item)
        except HostControlError as exc:
            raise InvalidSpec(f"invalid rollback mutation: {exc}") from exc
        operation = by_id.get(mutation.operation_id)
        if operation is None or (operation.kind, operation.target, operation.owner) != (
            mutation.kind,
            mutation.target,
            mutation.owner,
        ):
            raise InvalidSpec("rollback mutation names no operation of this spec")
        if mutation.owner != account:
            raise ExecutorDenied("rollback mutation is owned by another account")
        if not mutation.changed:
            raise InvalidSpec("rollback mutation is not a changed record")
        mutations.append(mutation)
    if len({item.operation_id for item in mutations}) != len(mutations):
        raise InvalidSpec("duplicate rollback mutation")
    return tuple(mutations)


def _string_map(value: object, context: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise InvalidSpec(f"invalid operation {context}")
    return dict(value)


def _operation(raw: object) -> InstallOperation:
    if not isinstance(raw, dict) or set(raw) != _OPERATION_KEYS:
        raise InvalidSpec("invalid operation fields")
    layer = raw["layer"]
    if not isinstance(layer, int) or isinstance(layer, bool):
        raise InvalidSpec("invalid operation layer")
    for name in ("operation_id", "kind", "target", "privilege", "owner", "execution_scope"):
        if not isinstance(raw[name], str) or not raw[name]:
            raise InvalidSpec(f"invalid operation {name}")
    # carry-in N1: the broker's record decoders' bounds, through the SAME
    # predicate, so a record this executor commits always decodes back at
    # the broker (an over-long id is refused here, before anything runs)
    for name, maximum in (
        ("operation_id", RECORD_STRING_MAX),
        ("kind", RECORD_STRING_MAX),
        ("owner", RECORD_STRING_MAX),
        ("target", RECORD_TARGET_MAX),
    ):
        try:
            closed_record_string(raw[name], f"operation {name}", maximum=maximum)
        except HostControlError as exc:
            raise InvalidSpec(str(exc)) from exc
    inverse = raw["inverse"]
    return InstallOperation(
        operation_id=raw["operation_id"],
        layer=layer,
        kind=raw["kind"],
        target=raw["target"],
        precondition=_string_map(raw["precondition"], "precondition"),
        desired=_string_map(raw["desired"], "desired"),
        inverse=None if inverse is None else _string_map(inverse, "inverse"),
        privilege=raw["privilege"],
        owner=raw["owner"],
        provenance=_string_map(raw["provenance"], "provenance"),
        execution_scope=raw["execution_scope"],
    )


def parse_spec(
    *,
    schema_version: str,
    execution_kind: str,
    returned_digest: str,
    spec_json: str,
    expected_digest: str,
    operation_handle: str,
    identity: ExecutorIdentity,
    registry: SeatRegistry,
) -> ParsedSpec:
    """Validate a fetched ``OperationSpec`` reply. Raises ``InvalidSpec``,
    ``ExecutorDenied`` (target is not this seat) or ``Unsupported``."""

    if schema_version != EXECUTION_SPEC_SCHEMA:
        raise InvalidSpec("unsupported execution spec schema")
    try:
        payload = json.loads(spec_json)
    except (TypeError, ValueError) as exc:
        raise InvalidSpec("execution spec is not JSON") from exc
    if not isinstance(payload, dict) or canonical_json(payload) != spec_json:
        raise InvalidSpec("execution spec is not canonical")
    digest = sha256_json(payload)
    if digest != expected_digest or returned_digest != expected_digest:
        raise InvalidSpec("execution spec digest mismatch")
    keys = _RECONCILE_SPEC_KEYS if execution_kind == "reconcile" else _SPEC_KEYS
    if set(payload) != keys or payload["schema"] != EXECUTION_SPEC_SCHEMA:
        raise InvalidSpec("invalid execution spec fields")
    if execution_kind == "reconcile":
        nonce = payload["probe_nonce"]
        if isinstance(nonce, bool) or not isinstance(nonce, int) or not 0 <= nonce <= MAX_PROBE_NONCE:
            raise InvalidSpec("invalid reconcile probe nonce")
    if payload["operation_handle"] != operation_handle:
        raise InvalidSpec("execution spec names another operation")
    if payload["execution_kind"] != execution_kind:
        raise InvalidSpec("execution spec kind mismatch")
    try:
        target = registry.seat(payload["target_seat_id"])
    except RegistryError as exc:
        raise ExecutorDenied("spec target is not a registered seat") from exc
    if target.uid != identity.uid or target.seat_id != identity.seat.seat_id:
        raise ExecutorDenied("spec target seat is not this executor's seat")
    if execution_kind not in EXECUTION_KINDS:
        raise Unsupported(f"{execution_kind} is not executable in this revision")
    if execution_kind != "rollback" and payload["rollback_mutations"] != []:
        raise InvalidSpec("rollback mutations on a non-rollback spec")
    if execution_kind == "rollback" and (
        not isinstance(payload["receipt_handle"], str) or not isinstance(payload["receipt_digest"], str)
    ):
        raise InvalidSpec("rollback spec names no receipt")
    raw_operations = payload["operations"]
    if not isinstance(raw_operations, list) or not 1 <= len(raw_operations) <= _MAX_OPERATIONS:
        raise InvalidSpec("invalid execution spec operation list")
    operations = tuple(_operation(item) for item in raw_operations)
    if len({op.operation_id for op in operations}) != len(operations):
        raise InvalidSpec("duplicate operation id")
    for operation in operations:
        if operation.owner != identity.seat.account:
            raise ExecutorDenied("operation is owned by another account")
    try:
        require_executor_envelope(op.kind for op in operations)
    except PolicyDenied as exc:
        raise InvalidSpec(str(exc)) from exc
    require_revision_implemented(execution_kind, (op.kind for op in operations))
    roots = payload["allowed_roots"]
    if not isinstance(roots, list) or str(identity.seat_root) not in roots:
        raise InvalidSpec("spec allowed roots do not include the seat root")
    refused_units = refused_role_units(operations)
    if refused_units:
        raise InvalidSpec(("unit outside the per-kind unit allowlist: " + ",".join(refused_units))[:_DETAIL_LIMIT])
    instance_problem = consume_instance_problem(operations)
    if instance_problem:
        raise InvalidSpec(instance_problem[:_DETAIL_LIMIT])
    pin_problem = codex_pin_problem(operations)
    if pin_problem:
        raise InvalidSpec(pin_problem[:_DETAIL_LIMIT])
    rollback_mutations: tuple[MutationRecord, ...] = ()
    if execution_kind == "rollback":
        rollback_mutations = _rollback_mutations(
            payload["rollback_mutations"], operations, identity.seat.account
        )
    return ParsedSpec(
        operation_handle=operation_handle,
        execution_kind=execution_kind,
        spec_digest=digest,
        operations=operations,
        rollback_mutations=rollback_mutations,
    )


def _bounded(text: object) -> str:
    try:
        return str(text)[:_DETAIL_LIMIT]
    except Exception:  # carry-in N3: an unprintable value never escapes
        return "<unprintable>"


def _safe_detail(exc: BaseException) -> str:
    """Carry-in N3: the per-operation failure text, with a bounded fallback
    when the exception's ``__str__`` itself raises."""

    name = type(exc).__name__
    try:
        text = str(exc)
    except Exception:
        text = "<unprintable exception>"
    return _bounded(f"{name}: {text}")


def _safe_record_size(mutation: MutationRecord | None, observed: OperationEvidence | None) -> int:
    """Carry-in N3: ``record_size`` with a bounded fallback (the per-
    operation evidence reserve), so a sizing failure can never drop a
    committed mutation."""

    try:
        return record_size(mutation, observed)
    except Exception:
        return EVIDENCE_RESERVE_BYTES


def _no_mutation(operation: InstallOperation) -> MutationRecord:
    return MutationRecord(
        operation.operation_id,
        operation.kind,
        operation.target,
        operation.owner,
        False,
        {"state": "not-applicable"},
        None,
    )


class ExecutorEngine:
    def __init__(
        self,
        *,
        identity: ExecutorIdentity,
        framework_files: RootOwnedFileLoader,
        framework_root: Path,
        user_manager: UserManagerPort,
        seat_files: SeatFiles | None = None,
        which: Callable[[str], str | None] = shutil.which,
        pwd_lookup: Callable[[int], object] = pwd.getpwuid,
        group_lookup: Callable[[str], object] = grp.getgrnam,
        grouplist: Callable[[str, int], list[int]] = os.getgrouplist,
        linger_dir: Path = DEFAULT_LINGER_DIR,
        record_budget: int = RECORD_BUDGET_BYTES,
        run_budget: float = EXECUTOR_RUN_BUDGET_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        run_child: Callable[..., Any] = subprocess.run,
        python: str = sys.executable,
        mailbox_probe: MailboxProbe | None = None,
    ) -> None:
        # T-2917 5d.3: built from the root-owned executor config only
        self._mailbox = mailbox_probe
        self._operation_handle = ""
        self._run_budget = min(float(run_budget), EXECUTOR_RUN_BUDGET_SECONDS)
        self._clock = clock
        self._sleep = sleep
        self._run_child = run_child
        self._python = python
        self._deadline = 0.0
        # per-run activation state: evidence of earlier operations in THIS
        # run, and the B9 legacy-convergence seeds, both keyed by kind
        self._run_evidence: dict[str, OperationEvidence] = {}
        self._seeded: dict[str, dict[str, dict[str, str]]] = {}
        self._record_budget = int(record_budget)
        self._remaining = self._record_budget
        self._identity = identity
        self._framework = framework_files
        self._framework_root = framework_root
        self._user_manager = user_manager
        self._files = seat_files or SeatFiles(
            seat_root=identity.seat_root,
            owner_uid=identity.uid,
        )
        self._which = which
        self._pwd = pwd_lookup
        self._grp = group_lookup
        self._grouplist = grouplist
        self._linger_dir = linger_dir

    # ------------------------------------------------------------------
    # the loop
    # ------------------------------------------------------------------

    @property
    def run_budget(self) -> float:
        """This engine's per-execution wall-clock budget (seconds); the
        service arms its watchdog at budget + grace (T-2917:D11)."""

        return self._run_budget

    def run(self, spec: ParsedSpec) -> ExecutionResult:
        """Run the operations in plan order.

        * Any exception from one operation fails THAT operation (review
          R2: not only the expected classes), and apply stops there; every
          mutation committed earlier is still returned. Nothing on the
          error path itself (an exception whose ``__str__`` raises, a
          record-size failure) can drop a committed mutation (N3).
        * The canonical-JSON size of the returned records is budgeted
          (``RECORD_BUDGET_BYTES``, arithmetic in ``executor/wire.py``):
          a file operation whose projected record would not fit is refused
          before it commits; an over-budget record that was already
          committed is kept and the run ends failed.
        * The execution deadline is HARD (T-2917:D11): it is bound into
          the user-manager port for the whole run, and every timed port
          call gets a timeout within the time left.
        """

        mutations: list[MutationRecord] = []
        evidence: list[OperationEvidence] = []
        self._deadline = self._clock() + self._run_budget
        self._operation_handle = spec.operation_handle
        self._run_evidence = {}
        self._seeded = {}
        self._bind_port_deadline(self._deadline)
        try:
            return self._run_operations(spec, mutations, evidence)
        except Exception as exc:  # N3: never lose what already committed
            return ExecutionResult(
                ok=False,
                mutations=tuple(mutations),
                evidence=tuple(evidence),
                detail=_bounded(f"{spec.execution_kind} aborted: {_safe_detail(exc)}"),
            )
        finally:
            self._bind_port_deadline(None)

    def _bind_port_deadline(self, deadline: float | None) -> None:
        """Bind (or clear) the hard deadline in a port that supports it
        (the executor's ``DBusUserManager``; not on the shared protocol, so
        the installer's port parity is untouched)."""

        bind = getattr(self._user_manager, "bind_deadline", None)
        if callable(bind):
            try:
                bind(deadline)
            except Exception:
                pass

    def _run_operations(
        self,
        spec: ParsedSpec,
        mutations: list[MutationRecord],
        evidence: list[OperationEvidence],
    ) -> ExecutionResult:
        if spec.execution_kind == "reconcile":
            return self._run_reconcile(spec, evidence)
        if spec.execution_kind == "rollback":
            return self._run_rollback(spec, mutations, evidence)
        verify = spec.execution_kind == "verify"
        used = 0
        exhausted = False
        if not verify:
            refusal = self._pre_pass(spec.operations)
            if refusal is not None:
                return ExecutionResult(
                    ok=False,
                    mutations=(),
                    evidence=(refusal,),
                    detail="apply refused by the pre-pass: nothing was changed",
                )
        for operation in spec.operations:
            if verify and operation.execution_scope != "all":
                # parity with PersonaInstaller.verify: staged-cutover
                # boundary checks are apply-time only
                continue
            self._remaining = self._record_budget - used
            try:
                if self._clock() >= self._deadline:
                    raise OperationFailed("executor run budget exhausted before this operation")
                mutation, observed = self._dispatch(operation, verify=verify)
            except Exception as exc:  # review R2: every failure is per-operation
                mutation = None
                values = getattr(exc, "evidence", None) if isinstance(exc, OperationFailed) else None
                try:
                    observed = self._evidence(operation, False, _safe_detail(exc), values)
                except Exception:  # N3: malformed evidence values
                    observed = self._evidence(operation, False, _safe_detail(exc))
            self._run_evidence[operation.kind] = observed
            size = _safe_record_size(mutation, observed)
            if used + size > self._record_budget:
                observed = self._evidence(
                    operation,
                    False,
                    "result budget exhausted"
                    + ("; the committed mutation is kept" if mutation is not None else ""),
                )
                size = _safe_record_size(mutation, observed)
                exhausted = True
            used += size
            if mutation is not None:
                mutations.append(mutation)
            evidence.append(observed)
            if exhausted or (not verify and not observed.ok):
                # apply stops at the first failure; mutations so far are
                # returned so the broker's failed receipt records them
                break
        ok = bool(evidence) and all(item.ok for item in evidence)
        detail = (
            f"{spec.execution_kind} {'succeeded' if ok else 'failed'}: "
            f"{sum(item.ok for item in evidence)}/{len(evidence)} checks ok"
        )
        return ExecutionResult(
            ok=ok,
            mutations=tuple(mutations),
            evidence=tuple(evidence),
            detail=detail,
        )

    def _dispatch(
        self,
        operation: InstallOperation,
        *,
        verify: bool,
    ) -> tuple[MutationRecord | None, OperationEvidence]:
        capability = EXECUTOR_KIND_CLASSES.get(operation.kind)
        if capability == "files.seat_root":
            if verify:
                return None, self._verify_file(operation)
            return self._apply_file(operation)
        if capability == "diagnostics.read":
            handler = self._DIAGNOSTICS.get(operation.kind)
            if handler is None:
                raise Unsupported(f"no diagnostic handler for {operation.kind}")
            observed = handler(self, operation)
            return (None if verify else _no_mutation(operation)), observed
        if capability == "user_systemd.activate":
            if verify:
                observer = self._ACTIVATION_VERIFY.get(operation.kind)
                if observer is None:
                    raise Unsupported(f"no verify observation for {operation.kind}")
                return None, observer(self, operation)
            applier = self._ACTIVATION_APPLY.get(operation.kind)
            if applier is None:
                raise Unsupported(f"no activation handler for {operation.kind}")
            return applier(self, operation)
        if capability == "mailbox.probe":
            observed = self._mailbox_probe(operation)
            return (None if verify else _no_mutation(operation)), observed
        raise Unsupported(f"{operation.kind} is not executable in this revision")

    # ------------------------------------------------------------------
    # mailbox.probe (T-2917 5d.3)
    # ------------------------------------------------------------------

    def _require_mailbox(self) -> MailboxProbe:
        if self._mailbox is None:
            raise OperationFailed("mailbox hub is not configured for this executor")
        return self._mailbox

    def _mailbox_probe(self, operation: InstallOperation) -> OperationEvidence:
        mailbox = self._require_mailbox()
        if operation.kind == ROUND_TRIP_KIND:
            values = mailbox.round_trip(
                operation_handle=self._operation_handle, operation=operation, deadline=self._deadline
            )
            detail = "mailbox receive/send durable round trip passed"
        else:
            values = mailbox.writable_root(
                operation_handle=self._operation_handle, operation=operation, deadline=self._deadline
            )
            detail = "mailbox writable root probe passed"
        return self._evidence(operation, True, detail, values)

    @staticmethod
    def _evidence(
        operation: InstallOperation,
        ok: bool,
        detail: str,
        values: Mapping[str, str] | None = None,
    ) -> OperationEvidence:
        return OperationEvidence(
            operation.operation_id,
            operation.layer,
            operation.kind,
            ok,
            _bounded(detail),
            {key: _bounded(value) for key, value in (values or {}).items()},
        )

    # ------------------------------------------------------------------
    # repo-sourced reads: the root-owned framework copy ONLY
    # ------------------------------------------------------------------

    def _framework_path(self, target: str) -> Path:
        if not isinstance(target, str) or not os.path.isabs(target):
            raise SeatPathRefused("framework target must be absolute")
        if target.startswith("//") or os.path.normpath(target) != target or ".." in Path(target).parts:
            raise SeatPathRefused("framework target must be normalized")
        path = Path(target)
        if path != self._framework_root and self._framework_root not in path.parents:
            raise SeatPathRefused(
                "repo-sourced target is not the root-owned framework copy"
            )
        if self._files.contains(target) or path == self._identity.seat_root:
            raise SeatPathRefused("repo-sourced target lies inside the seat root")
        return path

    def _framework_relative(self, raw: str) -> Path:
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise SeatPathRefused("framework artifact must be a relative path")
        return self._framework_root / relative

    # ------------------------------------------------------------------
    # diagnostics.read
    # ------------------------------------------------------------------

    def _identity_check(self, operation: InstallOperation) -> OperationEvidence:
        account = self._identity.seat.account
        problems: list[str] = []
        if operation.target != account or operation.desired.get("account") != account:
            problems.append("account mismatch")
        group_name = operation.desired.get("mailbox_group", "")
        values = {"uid": str(self._identity.uid), "account": account, "mailbox_group": group_name}
        try:
            entry = self._pwd(self._identity.uid)
            if getattr(entry, "pw_name") != account:
                problems.append("uid does not resolve to the seat account")
            gid = getattr(self._grp(group_name), "gr_gid")
            member = gid in self._grouplist(account, getattr(entry, "pw_gid"))
        except (KeyError, OSError):
            member = False
            problems.append("account or mailbox group lookup failed")
        values["group_member"] = str(member).lower()
        if not member:
            problems.append("not a member of the mailbox group")
        linger = (self._linger_dir / account).exists()
        values["linger"] = "enabled" if linger else "disabled"
        if not linger:
            problems.append("login linger is not enabled")
        try:
            reachable = self._user_manager.manager_state().reachable
        except HostControlError:
            reachable = False
        values["user_manager"] = "active" if reachable else "unreachable"
        if not reachable:
            problems.append("user manager unreachable")
        return self._evidence(
            operation,
            not problems,
            "identity ready" if not problems else "; ".join(problems),
            values,
        )

    def _manager_check(self, operation: InstallOperation) -> OperationEvidence:
        try:
            state = self._user_manager.manager_state()
        except HostControlError as exc:
            return self._evidence(operation, False, str(exc), {"reachable": "false"})
        return self._evidence(
            operation,
            state.reachable,
            "target user manager reachable" if state.reachable else "user manager unreachable",
            {"reachable": str(state.reachable).lower(), "system_state": state.system_state},
        )

    def _harness_check(self, operation: InstallOperation) -> OperationEvidence:
        if operation.target not in ("claude", "codex"):
            return self._evidence(operation, False, "unknown harness")
        found = self._which(operation.target) is not None
        return self._evidence(
            operation,
            found,
            f"{operation.target} {'found' if found else 'missing'}",
            {"command": operation.target, "found": str(found).lower()},
        )

    def _source_check(self, operation: InstallOperation) -> OperationEvidence:
        # Ruling B6: no git. Revision equality is pinned by the planner's
        # effect digest (declared_revision == registry framework_revision);
        # the executor proves the ARTIFACT DIGEST over the root-owned copy.
        root = self._framework_path(operation.target)
        if root != self._framework_root:
            raise SeatPathRefused("source-check target is not the framework root")
        artifacts = [item for item in operation.desired.get("artifacts", "").split(",") if item]
        digest = hashlib.sha256()
        for relative in sorted(artifacts):
            data = self._framework.read_bytes(
                self._framework_relative(relative), max_bytes=_FRAMEWORK_READ_LIMIT
            )
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(data)
            digest.update(b"\0")
        actual = digest.hexdigest()
        ok = bool(artifacts) and actual == operation.desired.get("artifact_sha256")
        return self._evidence(
            operation,
            ok,
            "framework artifact digest matches" if ok else "framework source drifted from reviewed plan",
            {
                "artifact_sha256": actual,
                "declared_revision": operation.desired.get("revision", ""),
                "revision_proof": "planner-effect-digest",
                "git": "not-used",
            },
        )

    def _policy_check(self, operation: InstallOperation) -> OperationEvidence:
        root = self._framework_path(operation.target)
        if root != self._framework_root:
            raise SeatPathRefused("policy-check target is not the framework root")
        missing = []
        for entry in (item for item in operation.desired.get("bundle", "").split(",") if item):
            path = self._framework_relative(entry.rstrip("/"))
            if self._framework.entry_kind(path) == "absent":
                missing.append(entry)
        return self._evidence(
            operation,
            not missing,
            "policy bundle present" if not missing else f"missing {','.join(missing)}",
        )

    def _brief_check(self, operation: InstallOperation) -> OperationEvidence:
        # Ruling B7: a repo-default brief is read from the root-owned
        # framework copy; an account-home brief is a READABILITY-ONLY check
        # under the seat root. It derives NO trust: its bytes are never
        # compared, digested, executed or used as authority.
        if self._files.contains(operation.target):
            readable = self._files.read_bytes(operation.target) is not None
            return self._evidence(
                operation,
                readable,
                "persona brief present" if readable else "persona brief missing",
                {"source": "seat-root", "trust": "readability-only"},
            )
        path = self._framework_path(operation.target)
        self._framework.read_bytes(path, max_bytes=_FRAMEWORK_READ_LIMIT)
        return self._evidence(
            operation, True, "persona brief present", {"source": "framework-root"}
        )

    def _codex_source_check(self, operation: InstallOperation) -> OperationEvidence:
        # Ruling B6: the installer's `git cat-file` commit proof is dropped;
        # the hash over the root-owned copy is the executor's proof.
        path = self._framework_path(operation.target)
        actual = hashlib.sha256(
            self._framework.read_bytes(path, max_bytes=_FRAMEWORK_READ_LIMIT)
        ).hexdigest()
        ok = actual == operation.desired.get("source_sha256")
        return self._evidence(
            operation,
            ok,
            "Codex standing instructions source hash matches" if ok else "Codex standing instructions source hash mismatch",
            {
                "source_sha256": actual,
                "source_commit": operation.desired.get("source_commit", ""),
                "commit_proof": "not-checked-no-git",
            },
        )

    def _codex_target_check(self, operation: InstallOperation) -> OperationEvidence:
        data = self._files.read_bytes(operation.target)
        actual = "" if data is None else hashlib.sha256(data).hexdigest()
        ok = data is not None and actual == operation.desired.get("sha256")
        return self._evidence(
            operation,
            ok,
            "Codex standing instructions target hash matches" if ok else "Codex standing instructions target hash mismatch",
            {"target_sha256": actual, "expected_sha256": operation.desired.get("sha256", "")},
        )

    def _codex_override_check(self, operation: InstallOperation) -> OperationEvidence:
        data = self._files.read_bytes(operation.target)
        ok = data is None or not data.decode("utf-8", "replace").strip()
        return self._evidence(
            operation,
            ok,
            "Codex override absent or empty" if ok else "Codex override file is non-empty",
            {"override_state": "absent" if data is None else ("empty" if ok else "non-empty")},
        )

    def _claude_mcp_check(self, operation: InstallOperation) -> OperationEvidence:
        for key in ("home", "config_path"):
            value = operation.desired.get(key, "")
            if value and not (value == str(self._identity.seat_root) or self._files.contains(value)):
                raise SeatPathRefused(f"desired {key} is outside the seat root")
        data = self._files.read_bytes(operation.target)
        servers: Mapping[str, object] = {}
        if data is not None:
            try:
                document = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise OperationFailed("cannot parse Claude MCP config") from exc
            if not isinstance(document, dict):
                raise OperationFailed("Claude MCP config root is not a mapping")
            raw = document.get("mcpServers", {})
            if not isinstance(raw, dict):
                raise OperationFailed("Claude MCP config mcpServers is not a mapping")
            servers = raw
        verdict = _claude_mcp_verdict(servers, operation)
        # read-only audit: always ok, the verdict rides the evidence
        return self._evidence(
            operation,
            True,
            f"Claude MCP audit verdict: {verdict}",
            {
                "name": operation.desired.get("name", ""),
                "scope": operation.desired.get("scope", ""),
                "verdict": verdict,
            },
        )

    def _timer_enablement_absence_check(self, operation: InstallOperation) -> OperationEvidence:
        canonical = operation.desired.get("canonical_template_path", "")
        if canonical and not self._files.contains(canonical):
            raise SeatPathRefused("canonical template path is outside the seat root")
        state = self._files.kind(operation.target)
        if state == "absent":
            return self._evidence(
                operation,
                True,
                "consume timer enablement link is absent",
                {"link_state": "absent", "historical_boundary_absent": "true"},
            )
        # Ruling B9 (5c.2): a present link is admitted ONLY as converged
        # legacy state, judged through the user-manager port exactly as the
        # installer's legacy-convergence probe judges it; the probe seeds the
        # run's unit-fire/timer-observe evidence. Anything else fails closed.
        seeded = self._legacy_convergence(operation)
        if seeded is None:
            return self._evidence(
                operation,
                False,
                f"{operation.operation_id}: consume timer enablement link already exists",
                {"link_state": state, "classification": "conflicting"},
            )
        return self._evidence(
            operation,
            True,
            "existing canonical enablement matches converged shape",
            {"link_state": state, "classification": "converged-legacy", **self._port_labels()},
        )

    def _disabled_first_check(self, operation: InstallOperation) -> OperationEvidence:
        """codex-telegram-disabled-first as user-systemd INSPECTION
        (T-4861; 5c.2). D4: the EnvironmentFile gate reads the TYPED
        EnvironmentFiles list over the port; the operator path matched a
        substring of the unit's file text, so the two differ when the word
        appears only in a comment or another directive's value."""

        unit = self._unit(operation, "target")
        receiver = self._user_manager.read_unit(unit, ReadPurpose.RECEIVER_ACTIVE, timeout=self._call_timeout())
        enabled = self._user_manager.unit_file_enablement(unit, timeout=self._call_timeout())
        environment_file = self._user_manager.environment_file_declared(unit)
        values = {
            "active": receiver.active_state or "",
            "enabled": enabled.state if enabled.state else "disabled",
            "main_pid": _us.render_main_pid(receiver.main_pid) or "0",
            "environment_file": "declared" if environment_file else "absent",
            **self._port_labels(),
        }
        if enabled.enabled:
            return self._evidence(operation, False, "codex telegram receiver is enabled before cutover", values)
        if not _us.receiver_idle(receiver.active_state, receiver.main_pid):
            return self._evidence(operation, False, "codex telegram receiver is active before cutover", values)
        if environment_file:
            return self._evidence(operation, False, "codex telegram unit must not embed an EnvironmentFile", values)
        return self._evidence(operation, True, "codex telegram receiver is disabled before cutover", values)

    _DIAGNOSTICS: Final = {
        "identity-check": _identity_check,
        "manager-check": _manager_check,
        "harness-check": _harness_check,
        "source-check": _source_check,
        "policy-check": _policy_check,
        "brief-check": _brief_check,
        "codex-standing-instructions-source-check": _codex_source_check,
        "codex-standing-instructions-target-check": _codex_target_check,
        "codex-standing-instructions-override-check": _codex_override_check,
        "claude-mcp-server-check": _claude_mcp_check,
        "timer-enablement-absence-check": _timer_enablement_absence_check,
        "codex-telegram-disabled-first": _disabled_first_check,
    }

    # ------------------------------------------------------------------
    # files.seat_root
    # ------------------------------------------------------------------

    def _desired_bytes(self, operation: InstallOperation) -> bytes:
        content = operation.desired.get("content")
        if content is None:
            raise OperationFailed("content missing")
        data = content.encode("utf-8")
        # hash-pinned: the bytes carried in the plan must match their pin
        if hashlib.sha256(data).hexdigest() != operation.desired.get("sha256"):
            raise OperationFailed("content does not match its pinned sha256")
        return data

    def _apply_file(self, operation: InstallOperation) -> tuple[MutationRecord, OperationEvidence]:
        """Pre-commit failures raise (nothing changed, no mutation). Once the
        rename or mkdir has COMMITTED, the mutation is always returned, with
        its before-snapshot, even when a later step (directory sync, chmod,
        the after-snapshot) fails; the evidence then marks the operation
        failed (review M2: a real change must never miss the receipt)."""

        target = operation.target
        if operation.kind == "mkdir":
            mode = parse_mode(operation.desired.get("mode"), "0755")
            before = self._files.snapshot(target)
            change = lambda: self._files.mkdir(target, mode)  # noqa: E731
        else:
            mode = parse_mode(operation.desired.get("mode"), "0644")
            data = self._desired_bytes(operation)
            if operation.kind == CODEX_INSTALL_KIND:
                self._require_codex_source_observed(operation)
            before = self._files.snapshot(target)
            change = None
            if before.get("fingerprint") != operation.desired["sha256"]:
                if before["state"] == "file":
                    existing = self._files.read_bytes(target) or b""
                    # managed-marker guard: never overwrite an unknown file
                    if not _is_installer_managed(existing.decode("utf-8", "replace")):
                        raise SeatPathRefused("existing file is not installer-managed")
                elif before["state"] != "absent":
                    raise SeatPathRefused("target is not a regular file")
                change = lambda: self._files.write_atomic(target, data, mode)  # noqa: E731
        if change is not None:
            # review R1: refuse BEFORE committing when this operation's
            # record (its before-snapshot embeds the old content) would not
            # fit the remaining result budget
            projected = record_size(
                MutationRecord(
                    operation_id=operation.operation_id,
                    kind=operation.kind,
                    target=target,
                    owner=operation.owner,
                    changed=True,
                    before=before,
                    after_fingerprint="f" * 64,
                ),
                None,
            ) + EVIDENCE_RESERVE_BYTES
            if projected > self._remaining:
                raise ResultBudgetExceeded(
                    f"record of {projected} bytes exceeds the remaining result budget "
                    f"({self._remaining} bytes); refused before any change"
                )
        created = False
        failure = ""
        if change is not None:
            try:
                created = change()
            except SeatChangeIncomplete as exc:
                created = True
                failure = _bounded(exc)
        after_fingerprint: str | None = None
        after_state = ""
        try:
            after = self._files.snapshot(target)
            after_fingerprint = _snapshot_fingerprint(after)
            after_state = after.get("state", "")
        except Exception as exc:  # review R2: once committed, never lose it
            if not created:
                raise
            failure = failure or f"after-snapshot failed: {type(exc).__name__}"
        mutation = MutationRecord(
            operation_id=operation.operation_id,
            kind=operation.kind,
            target=target,
            owner=operation.owner,
            changed=created,
            before=before,
            after_fingerprint=after_fingerprint,
        )
        if failure:
            return mutation, self._evidence(
                operation,
                False,
                f"changed, then failed: {failure}",
                {"after_state": after_state or "unknown"},
            )
        return mutation, self._evidence(
            operation,
            True,
            "changed" if created else "already converged",
            {"after_state": after_state},
        )

    def _require_codex_source_observed(self, operation: InstallOperation) -> None:
        """Ruling T-2917:D23, the APPLY half: install only the bytes whose
        hash THIS run's source check just observed on the root-owned
        framework copy. Raises (pre-commit, nothing changed) otherwise."""

        observed = self._run_evidence.get(CODEX_SOURCE_CHECK_KIND)
        if observed is None or not observed.ok:
            raise OperationFailed(
                "Codex install refused: no passing source check observed the framework copy in this run"
            )
        if observed.evidence.get("source_sha256", "") != operation.desired.get("sha256", ""):
            raise OperationFailed(
                "Codex install refused: the framework copy's observed hash differs from the install pin"
            )

    def _verify_file(self, operation: InstallOperation) -> OperationEvidence:
        snapshot = self._files.snapshot(operation.target)
        if operation.kind == "mkdir":
            ok = snapshot["state"] == "directory"
            return self._evidence(
                operation, ok, "directory present" if ok else "directory missing",
                {"state": snapshot["state"]},
            )
        ok = snapshot["state"] == "file" and snapshot.get("fingerprint") == operation.desired.get("sha256")
        return self._evidence(
            operation,
            ok,
            "desired content installed" if ok else "content absent or drifted",
            {"state": snapshot["state"]},
        )

    # ------------------------------------------------------------------
    # user_systemd.activate (T-2917 5c.2): shared helpers
    # ------------------------------------------------------------------

    def _port_labels(self) -> dict[str, str]:
        """Every activation record names the transport and where its
        address came from; fixture runs say ``test-override`` (never
        relabelled as live)."""

        return {
            "user_manager_transport": str(getattr(self._user_manager, "transport", "unknown")),
            "user_manager_address_source": str(getattr(self._user_manager, "address_source", "unknown")),
        }

    @staticmethod
    def _unit(operation: InstallOperation, field: str) -> str:
        """Call-time M1 check: the unit THIS operation names in ``field``,
        refused unless (kind, field) admits its shape."""

        shapes = UNIT_ROLES.get((operation.kind, field))
        unit = operation.target if field == "target" else operation.desired.get(field)
        if shapes is None or not unit_shape_ok(unit, shapes):
            raise UnitRefused(
                f"{operation.kind}.{field} unit is outside the per-kind allowlist: {str(unit)[:80]}"
            )
        return str(unit)

    @staticmethod
    def _paired_service(timer: str) -> str:
        """The consume service a persona consume timer fires (B9)."""

        service = timer[: -len(".timer")] + ".service"
        if not unit_shape_ok(service, frozenset({"persona-service"})):
            raise UnitRefused("paired service is outside the per-kind allowlist")
        return service

    def _time_left(self, deadline: float | None = None) -> float:
        limit = self._deadline if deadline is None else min(deadline, self._deadline)
        remaining = limit - self._clock()
        if remaining <= 0:
            raise OperationFailed("execution budget exhausted")
        return remaining

    def _call_timeout(self, deadline: float | None = None) -> float:
        """T-2917:D11 shared clamp: one port call's timeout, never longer
        than the adapter's per-call timeout nor than the time left in this
        execution (raises once nothing is left)."""

        return min(DEFAULT_CALL_TIMEOUT, self._time_left(deadline))

    def _seat_link_proof(self, link: Path, canonical: Path) -> TimerEnablementLinkProof:
        """The installer's enablement-link proof, read through the
        descriptor-confined seat files (never followed)."""

        canonical = _lexically_normalize_absolute(canonical)
        canonical_sha = _sha256_bytes(str(canonical).encode("utf-8"))
        kind = self._files.kind(str(link))
        if kind == "symlink":
            actual = self._files.readlink(str(link)) or ""
            resolved = _resolve_timer_enablement_readlink(link, actual)
            return TimerEnablementLinkProof(
                link_path=link,
                canonical_template_path=canonical,
                snapshot_state="symlink",
                actual_readlink=actual,
                resolved_target=resolved,
                actual_readlink_sha256=_sha256_bytes(actual.encode("utf-8")),
                canonical_template_path_sha256=canonical_sha,
                resolved_target_sha256=_sha256_bytes(str(resolved).encode("utf-8")),
            )
        return TimerEnablementLinkProof(
            link_path=link,
            canonical_template_path=canonical,
            snapshot_state=kind,
            actual_readlink="",
            resolved_target=None,
            actual_readlink_sha256="",
            canonical_template_path_sha256=canonical_sha,
            resolved_target_sha256="",
        )

    def _timer_paths(self, operation: InstallOperation, timer: str) -> tuple[Path, Path]:
        """Link and canonical template paths, derived from THIS seat root and
        required to equal any path the plan declares."""

        seat_root = self._identity.seat_root
        derived_link = _consume_timer_enablement_link_path(seat_root, timer)
        derived_canonical = _consume_timer_template_path(seat_root, timer)
        declared_link = operation.desired.get("enablement_link_path") or (
            operation.target if operation.kind == "timer-enablement-absence-check" else ""
        )
        declared_canonical = operation.desired.get("canonical_template_path", "")
        if declared_link and _lexically_normalize_absolute(Path(declared_link)) != derived_link:
            raise OperationFailed("timer enablement link path does not match the seat-derived path")
        if declared_canonical and _lexically_normalize_absolute(Path(declared_canonical)) != derived_canonical:
            raise OperationFailed("timer canonical template path does not match the seat-derived path")
        for path in (derived_link, derived_canonical):
            if not self._files.contains(str(path)):
                raise SeatPathRefused("timer enablement path is outside the seat root")
        return derived_link, derived_canonical

    def _settle_service(self, service: str, initial: UnitSnapshot) -> UnitSnapshot:
        deadline = min(self._clock() + LEGACY_CONVERGENCE_SERVICE_SETTLE_WINDOW_SECONDS, self._deadline)
        state = initial
        while _us.snapshot_service_in_flight(state) and self._clock() < deadline:
            self._sleep(LEGACY_CONVERGENCE_SERVICE_SETTLE_POLL_INTERVAL_SECONDS)
            state = self._user_manager.read_unit(service, ReadPurpose.SERVICE_SETTLE, timeout=self._call_timeout())
        return state

    def _legacy_convergence(self, operation: InstallOperation) -> dict[str, dict[str, str]] | None:
        """Ruling B9: the installer's legacy-convergence probe through the
        port. Returns (and seeds this run with) the unit-fire and
        timer-observe evidence, or None when any invariant fails."""

        try:
            timer = self._unit(operation, "timer")
            service = self._paired_service(timer)
            if operation.kind == "unit-fire" and operation.target != service:
                # review N1: a seed only ever vouches for the timer's own service
                return None
            link, canonical = self._timer_paths(operation, timer)
            proof = self._seat_link_proof(link, canonical)
        except (HostControlError, OperationFailed):
            return None
        if not _timer_enablement_link_matches_canonical(proof):
            return None
        try:
            service_state = self._user_manager.read_unit(service, ReadPurpose.SERVICE_SETTLE, timeout=self._call_timeout())
            if _us.snapshot_service_in_flight(service_state):
                service_state = self._settle_service(service, service_state)
            timer_state = self._user_manager.read_unit(timer, ReadPurpose.TIMER_ELAPSE, timeout=self._call_timeout())
        except (HostControlError, OperationFailed, _us.UserManagerError):
            return None
        if timer_state.active_state != "active" or timer_state.sub_state != "waiting":
            return None
        basis = _us.snapshot_finite_next_elapse_basis(timer_state)
        if basis is None:
            return None
        if (
            service_state.active_state != "inactive"
            or service_state.result != "success"
            or not _us.exit_status_within_policy(service_state.exec_main_status, service_state.success_exit)
        ):
            return None
        current = service_state.invocation_id or ""
        seeds = {
            "unit-fire": {
                "invocation_id": current,
                "active_state": service_state.active_state or "",
                "result": service_state.result or "",
                "exec_main_status": _us.render_exec_status(service_state.exec_main_status),
                "success_exit_status": _us.render_success_exit(service_state.success_exit),
                "post_exit_invocation_id_state": "legacy-unknown",
                "timer_enabled": "true",
                **_timer_enablement_link_evidence("legacy", proof),
                **self._port_labels(),
            },
            "timer-observe": {
                "next_elapse_basis": basis,
                "next_elapse_finite": "true",
                "first_fire_invocation_observed": "true",
                "second_consume_absent": "true",
                "mailbox_effect_correlated": "legacy-unknown",
                "mailbox_probe_sha256": "",
                "unit_fire_invocation_id": current,
                "post_exit_invocation_id_state": "legacy-unknown",
                "initial_viability_receipt_sha256": "",
                **self._port_labels(),
            },
        }
        # review N1(b): keyed by the TIMER unit whose shape was proved, so
        # a seed can only vouch for operations on that same timer
        self._seeded[timer] = seeds
        return seeds

    # ------------------------------------------------------------------
    # the apply pre-pass (read-only; the installer's preflight gates)
    # ------------------------------------------------------------------

    def _pre_pass(self, operations: tuple[InstallOperation, ...]) -> OperationEvidence | None:
        needs_manager = [
            op for op in operations
            if op.kind in ACTIVATION_KINDS or op.kind == "codex-telegram-disabled-first"
        ]
        if needs_manager:
            try:
                self._user_manager.manager_environment()
            except Exception as exc:
                return self._evidence(
                    needs_manager[0],
                    False,
                    f"pre-pass: user manager unavailable: {type(exc).__name__}",
                    {"phase": "pre-pass", **self._port_labels()},
                )
        for operation in operations:
            try:
                problem = self._preflight(operation)
            except Exception as exc:
                problem = f"{type(exc).__name__}: {exc}"
            if problem:
                return self._evidence(
                    operation,
                    False,
                    f"pre-pass: {problem}",
                    {"phase": "pre-pass", **self._port_labels()},
                )
        return None

    def _preflight(self, operation: InstallOperation) -> str:
        kind = operation.kind
        if kind == "timer-enablement-absence-check":
            observed = self._timer_enablement_absence_check(operation)
            return "" if observed.ok else f"conflicting: {observed.detail}"
        if kind == "codex-telegram-disabled-first":
            observed = self._disabled_first_check(operation)
            return "" if observed.ok else f"conflicting: {observed.detail}"
        if kind == "unit-fire":
            budget = _parse_unit_fire_timeout_budget(operation)
            self._viability(operation)
            if budget.seconds > self._time_left():
                return "unit-fire: budget exceeds the executor run budget"
            timer = self._unit(operation, "timer")
            self._unit(operation, "target")
            if self._user_manager.unit_file_enablement(timer, timeout=self._call_timeout()).enabled and timer not in self._seeded:
                if self._legacy_convergence(operation) is None:
                    return "conflicting: timer is already enabled without this executor's convergence evidence"
            return ""
        if kind == "timer-enable":
            unit = self._unit(operation, "target")
            link, canonical = self._timer_paths(operation, unit)
            state = _timer_unit_state_from_snapshot(
                self._user_manager.read_unit(unit, ReadPurpose.UNIT_FILE_ACTIVE, timeout=self._call_timeout())
            )
            proof = self._seat_link_proof(link, canonical)
            if state.classification == "not-found":
                # the apply handler's gate, mirrored before any mutation
                return "conflicting: timer unit is not loaded"
            if state.classification == "enabled":
                # review L1: the SAME gate the apply handler enforces, so an
                # already-enabled timer without a legacy seed is refused here,
                # before any earlier operation (e.g. a mkdir) commits
                if unit not in self._seeded:
                    return "conflicting: timer already enabled at the activation boundary"
                if proof.snapshot_state != "absent" and not _timer_enablement_link_matches_canonical(proof):
                    return "conflicting: enabled timer link does not resolve to canonical template"
            elif proof.snapshot_state != "absent":
                return "conflicting: consume timer enablement link already exists"
            return ""
        if kind in ("stale-unit-failure-reset", "timer-observe"):
            self._unit(operation, "target")
            if kind == "timer-observe":
                self._unit(operation, "service")
            return ""
        if kind == "direct-fire":
            self._diagnose_argv(operation)
            return ""
        if EXECUTOR_KIND_CLASSES.get(kind) == "mailbox.probe":
            # T-2917 5d.3: confinement to the configured hub, before any write
            self._require_mailbox().check_targets(operation)
            return ""
        return ""

    # ------------------------------------------------------------------
    # apply handlers
    # ------------------------------------------------------------------

    def _apply_daemon_reload(self, operation: InstallOperation) -> tuple[MutationRecord, OperationEvidence]:
        outcome = self._user_manager.reload()
        values = {**self._port_labels(), "reload_ok": str(outcome.ok).lower()}
        if not outcome.ok:
            raise OperationFailed(
                outcome.detail or "daemon-reload failed",
                evidence={**values, "error_name": outcome.error_name},
            )
        return _no_mutation(operation), self._evidence(operation, True, "user manager reloaded", values)

    def _stale_read(self, unit: str, **failure: Any) -> UnitSnapshot:
        try:
            return self._user_manager.read_unit(unit, ReadPurpose.STALE_RESET, timeout=self._call_timeout())
        except _us.UserManagerCallFailed as exc:
            raise _stale_unit_failure_reset_failure(exc.detail or "stale unit state unavailable", **failure) from None
        except _us.UserManagerProtocolError as exc:
            raise _stale_unit_failure_reset_failure(f"stale unit state is malformed: {exc.reason}", **failure) from exc

    def _apply_stale_reset(self, operation: InstallOperation) -> tuple[MutationRecord, OperationEvidence]:
        # only for a unit the plan names (ruling: §8a via the port only)
        unit = self._unit(operation, "target")
        pre = self._stale_read(unit)
        try:
            classification = _classify_stale_unit_failure_reset_snapshot(pre)
        except OperationFailed as exc:
            raise _stale_unit_failure_reset_failure(str(exc), pre=pre) from exc
        evidence = _stale_unit_failure_reset_snapshot_evidence("pre", pre)
        if classification == "clean-noop":
            before = {"state": "clean-noop"}
            evidence.update({"classification": "noop", "pre_state": "clean-noop", "post_state": "clean-noop"})
        else:
            outcome = self._user_manager.reset_failed_unit(unit)
            if not outcome.ok:
                raise _stale_unit_failure_reset_failure(
                    outcome.detail or "stale unit reset-failed failed",
                    pre=pre,
                )
            post = self._stale_read(unit, pre=pre)
            try:
                post_classification = _classify_stale_unit_failure_reset_snapshot(post)
            except OperationFailed as exc:
                raise _stale_unit_failure_reset_failure(str(exc), pre=pre, post=post) from exc
            if post_classification != "clean-noop":
                raise _stale_unit_failure_reset_failure(
                    "stale unit reset-failed left residual state", pre=pre, post=post
                )
            before = {"state": "reset-required"}
            evidence.update(_stale_unit_failure_reset_snapshot_evidence("post", post))
            evidence.update({"classification": "reset", "pre_state": "reset-required", "post_state": "clean-noop"})
        evidence.update(self._port_labels())
        mutation = MutationRecord(
            operation_id=operation.operation_id,
            kind=operation.kind,
            target=operation.target,
            owner=operation.owner,
            changed=False,
            before=before,
            after_fingerprint="evidence-only",
        )
        return mutation, self._evidence(
            operation, True, "consume service stale failure reset gate passed", evidence
        )

    def _diagnose_argv(self, operation: InstallOperation) -> list[str]:
        """direct-fire: ``sys.executable`` on the ROOT-OWNED framework copy's
        persona_bringup diagnose (the plan's repo/manifest paths name a seat
        checkout and are never used)."""

        harness = operation.desired.get("harness", "")
        if harness not in _DIRECT_FIRE_HARNESSES:
            raise OperationFailed("direct-fire harness is not claude or codex")
        if not _PERSONA_NAME.fullmatch(operation.target):
            raise OperationFailed("direct-fire persona name is malformed")
        script = self._framework_relative("tools/persona_bringup.py")
        manifest = self._framework_relative("instance.yaml")
        for path in (script, manifest):
            if self._framework.entry_kind(path) != "file":
                raise OperationFailed("framework copy lacks the diagnose entrypoint or manifest")
        return [
            self._python,
            "-I",
            str(script),
            "diagnose",
            "--manifest",
            str(manifest),
            "--persona",
            operation.target,
            "--harness",
            harness,
        ]

    def _diagnose_env(self) -> dict[str, str]:
        """The child's whole environment (review M2): fixed PATH, the seat
        root as HOME, C.UTF-8; nothing inherited, no PYTHON* variables."""

        return {
            "PATH": DIRECT_FIRE_PATH,
            "HOME": str(self._identity.seat_root),
            "LANG": "C.UTF-8",
        }

    def _diagnose(self, operation: InstallOperation, observation: str) -> tuple[bool, str, dict[str, str]]:
        argv = self._diagnose_argv(operation)
        timeout = min(DIRECT_FIRE_TIMEOUT_SECONDS, self._time_left())
        try:
            result = self._run_child(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(self._framework_root),
                stdin=subprocess.DEVNULL,
                env=self._diagnose_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise OperationFailed("direct boot smoke timed out") from exc
        stdout = (result.stdout or "").encode("utf-8")
        stderr = (result.stderr or "").encode("utf-8")
        values = {
            "observation": observation,
            "interpreter": "sys.executable -I",
            "child_env_keys": ",".join(DIRECT_FIRE_ENV_KEYS),
            "framework_root": str(self._framework_root),
            "plan_repo_used": "false",
            "returncode": str(result.returncode),
            "stdout_length": str(len(stdout)),
            "stdout_sha256": _sha256_bytes(stdout),
            "stderr_length": str(len(stderr)),
            "stderr_sha256": _sha256_bytes(stderr),
        }
        if result.returncode != 0:
            text = (result.stderr or result.stdout or "").strip()
            return False, (text[:120] or "direct boot smoke failed"), values
        return True, "direct boot smoke passed", values

    def _apply_direct_fire(self, operation: InstallOperation) -> tuple[MutationRecord, OperationEvidence]:
        ok, detail, values = self._diagnose(operation, "fire")
        if not ok:
            raise OperationFailed(detail, evidence=values)
        return _no_mutation(operation), self._evidence(operation, True, detail, values)

    def _viability(self, operation: InstallOperation) -> Any:
        viability = _parse_unit_fire_initial_viability_desired(operation)
        if _lexically_normalize_absolute(viability.account_home) != self._identity.seat_root:
            raise SeatPathRefused("unit-fire account home is not this seat root")
        if not self._files.contains(str(_lexically_normalize_absolute(viability.persona_state_root))):
            raise SeatPathRefused("unit-fire persona state root is outside the seat root")
        return viability

    # -- unit-fire ------------------------------------------------------

    def _uf_failed(
        self,
        message: str,
        *,
        phase: str,
        classification: str,
        snapshot: UnitSnapshot | None,
        start_count: int,
        cleanup_state: str,
    ) -> OperationFailed:
        invocation = (snapshot.invocation_id or "") if snapshot is not None else ""
        evidence = {
            "phase": phase,
            "classification": classification,
            "active_state": (snapshot.active_state or "") if snapshot is not None else "",
            "substate": (snapshot.sub_state or "") if snapshot is not None else "",
            "result": (snapshot.result or "") if snapshot is not None else "",
            "exec_main_status": _us.render_exec_status(snapshot.exec_main_status) if snapshot is not None else "",
            "invocation_id_length": str(len(invocation)),
            "invocation_id_sha256": _sha256_bytes(invocation.encode("utf-8")) if invocation else "",
            **(
                _us.snapshot_digest_evidence("", snapshot.source)
                if snapshot is not None
                else {"snapshot_length": "0", "snapshot_sha256": "", "snapshot_encoding": ""}
            ),
            "start_count": str(start_count),
            "cleanup_state": cleanup_state,
            **self._port_labels(),
        }
        return OperationFailed(f"{message}; unit_fire_diagnostics={canonical_json(evidence)}", evidence=evidence)

    def _uf_show(
        self,
        unit: str,
        *,
        deadline: float,
        phase: str,
        classification: str,
        start_count: int,
        cleanup_state: str,
    ) -> UnitSnapshot:
        try:
            return self._user_manager.read_unit(
                unit, ReadPurpose.UNIT_FIRE_POLL, timeout=self._call_timeout(deadline)
            )
        except _us.UserManagerTimeout as exc:
            raise self._uf_failed(
                "unit-fire user-manager call timed out",
                phase=phase,
                classification=classification,
                snapshot=None,
                start_count=start_count,
                cleanup_state=cleanup_state,
            ) from exc
        except _us.UserManagerCallFailed as exc:
            raise OperationFailed(exc.detail or "unit-fire state unavailable") from None
        except _us.UserManagerUnavailable:
            raise OperationFailed("unit-fire state unavailable: user manager unreachable") from None
        except _us.UserManagerProtocolError as exc:
            raise OperationFailed(f"unit-fire state is malformed: {exc.reason}") from None

    def _uf_sleep(self, deadline: float) -> None:
        remaining = min(deadline, self._deadline) - self._clock()
        if remaining > 0:
            self._sleep(min(UNIT_FIRE_POLL_INTERVAL_SECONDS, remaining))

    def _uf_stop_and_prove(self, unit: str, total_deadline: float) -> UnitSnapshot:
        try:
            outcome = self._user_manager.stop_unit(unit, timeout=self._time_left(total_deadline))
        except _us.UserManagerTimeout as exc:
            raise self._uf_failed(
                "unit-fire stop timed out",
                phase="cleanup",
                classification="unsafe-cleanup",
                snapshot=None,
                start_count=1,
                cleanup_state="stop-timeout",
            ) from exc
        except (_us.UserManagerCallFailed, _us.UserManagerUnavailable) as exc:
            # review L4: a refused or unreachable stop keeps the
            # unsafe-cleanup classification and its cleanup_state evidence
            raise self._uf_failed(
                f"unit-fire stop failed: {type(exc).__name__}",
                phase="cleanup",
                classification="unsafe-cleanup",
                snapshot=None,
                start_count=1,
                cleanup_state="stop-failed",
            ) from exc
        if not outcome.ok:
            raise OperationFailed(outcome.detail or "failed to stop unit-fire service")
        last: UnitSnapshot | None = None
        while self._clock() < min(total_deadline, self._deadline):
            try:
                last = self._uf_show(
                    unit,
                    deadline=total_deadline,
                    phase="cleanup",
                    classification="unsafe-cleanup",
                    start_count=1,
                    cleanup_state="show-timeout",
                )
            except OperationFailed as exc:
                if exc.evidence:
                    raise
                raise self._uf_failed(
                    str(exc), phase="cleanup", classification="unsafe-cleanup",
                    snapshot=None, start_count=1, cleanup_state="unavailable",
                ) from exc
            if last.active_state == "inactive":
                return last
            if last.active_state == "failed":
                raise self._uf_failed(
                    "unit-fire cleanup proved failed state instead of inactive",
                    phase="cleanup", classification="unsafe-cleanup",
                    snapshot=last, start_count=1, cleanup_state="failed",
                )
            self._uf_sleep(total_deadline)
        raise self._uf_failed(
            "unit-fire cleanup could not prove inactive",
            phase="cleanup", classification="unsafe-cleanup",
            snapshot=last, start_count=1, cleanup_state="unsafe-active",
        )

    def _apply_unit_fire(self, operation: InstallOperation) -> tuple[MutationRecord, OperationEvidence]:
        budget = _parse_unit_fire_timeout_budget(operation)
        viability = self._viability(operation)
        timer = self._unit(operation, "timer")
        unit = self._unit(operation, "target")
        start_time = self._clock()
        total_deadline = start_time + budget.seconds
        if total_deadline > self._deadline:
            raise OperationFailed("unit-fire budget exceeds the executor run budget")
        observation_deadline = total_deadline - RUNTIME_VIABILITY_TEARDOWN_MARGIN_SECONDS
        if observation_deadline <= start_time:
            raise OperationFailed("unit-fire timeout budget cannot preserve teardown reserve")
        enabled = self._user_manager.unit_file_enablement(timer, timeout=self._call_timeout(observation_deadline))
        if enabled.enabled and timer in self._seeded:
            return _no_mutation(operation), self._evidence(
                operation, True, "existing canonical enablement matches converged shape",
                dict(self._seeded[timer]["unit-fire"]),
            )
        if enabled.enabled:
            raise OperationFailed("timer was enabled before disabled first fire")
        started = False
        start_count = 0
        last: UnitSnapshot | None = None
        try:
            baseline = self._uf_show(
                unit, deadline=observation_deadline, phase="pre-enqueue",
                classification="state-unavailable", start_count=0, cleanup_state="not-owned",
            )
            if not _us.unit_fire_idle_before_enqueue(baseline):
                raise self._uf_failed(
                    "consume service is not idle before disabled first fire",
                    phase="pre-enqueue", classification="pre-existing-invocation",
                    snapshot=baseline, start_count=0, cleanup_state="not-owned",
                )
            try:
                start = self._user_manager.start_unit(unit, wait=False, timeout=self._call_timeout(observation_deadline))
            finally:
                # a start call that went out may have queued a job: own the cleanup
                start_count = 1
                started = True
            if not start.ok:
                raise self._uf_failed(
                    start.detail or "disabled first fire enqueue failed",
                    phase="enqueue", classification="start-failed",
                    snapshot=baseline, start_count=start_count, cleanup_state="pending",
                )
            invocation_id = ""
            capture_attempts = 0
            while self._clock() < min(observation_deadline, self._deadline):
                try:
                    last = self._uf_show(
                        unit, deadline=observation_deadline, phase="capture",
                        classification="state-unavailable", start_count=start_count, cleanup_state="pending",
                    )
                except OperationFailed as exc:
                    if exc.evidence:
                        raise
                    raise self._uf_failed(
                        str(exc), phase="capture", classification="state-unavailable",
                        snapshot=None, start_count=start_count, cleanup_state="pending",
                    ) from exc
                capture_attempts += 1
                if _us.unit_fire_has_failed(last):
                    raise self._uf_failed(
                        "disabled first fire entered failed state before InvocationID capture",
                        phase="capture", classification="unit-failed",
                        snapshot=last, start_count=start_count, cleanup_state="pending",
                    )
                if last.invocation_id:
                    if not last.invocation_id_valid:
                        raise self._uf_failed(
                            "disabled first fire returned malformed InvocationID",
                            phase="capture", classification="malformed-invocation-id",
                            snapshot=last, start_count=start_count, cleanup_state="pending",
                        )
                    if last.active_state not in {"activating", "active"}:
                        raise self._uf_failed(
                            "disabled first fire InvocationID was not captured while active",
                            phase="capture", classification="missing-active-invocation-id",
                            snapshot=last, start_count=start_count, cleanup_state="pending",
                        )
                    invocation_id = last.invocation_id
                    break
                if last.active_state == "inactive":
                    raise self._uf_failed(
                        "disabled first fire completed before InvocationID capture",
                        phase="capture", classification="missing-invocation-id",
                        snapshot=last, start_count=start_count, cleanup_state="pending",
                    )
                self._uf_sleep(observation_deadline)
            if not invocation_id:
                raise self._uf_failed(
                    "disabled first fire timed out before InvocationID capture",
                    phase="capture", classification="capture-timeout",
                    snapshot=last, start_count=start_count, cleanup_state="pending",
                )
            completion: UnitSnapshot | None = None
            while self._clock() < min(observation_deadline, self._deadline):
                try:
                    completion = self._uf_show(
                        unit, deadline=observation_deadline, phase="completion",
                        classification="state-unavailable", start_count=start_count, cleanup_state="pending",
                    )
                except OperationFailed as exc:
                    if exc.evidence:
                        raise
                    raise self._uf_failed(
                        str(exc), phase="completion", classification="state-unavailable",
                        snapshot=None, start_count=start_count, cleanup_state="pending",
                    ) from exc
                if _us.unit_fire_has_failed(completion):
                    raise self._uf_failed(
                        "disabled first fire completed unsuccessfully",
                        phase="completion", classification="completion-failed",
                        snapshot=completion, start_count=start_count, cleanup_state="pending",
                    )
                if completion.invocation_id:
                    if not completion.invocation_id_valid:
                        raise self._uf_failed(
                            "disabled first fire returned malformed post-exit InvocationID",
                            phase="completion", classification="malformed-post-exit-invocation-id",
                            snapshot=completion, start_count=start_count, cleanup_state="pending",
                        )
                    if completion.invocation_id != invocation_id:
                        raise self._uf_failed(
                            "disabled first fire InvocationID changed before completion",
                            phase="completion", classification="changed-invocation-id",
                            snapshot=completion, start_count=start_count, cleanup_state="pending",
                        )
                if completion.active_state == "inactive":
                    break
                self._uf_sleep(observation_deadline)
            if completion is None or completion.active_state != "inactive":
                raise self._uf_failed(
                    "disabled first fire timed out before completion",
                    phase="completion", classification="completion-timeout",
                    snapshot=completion, start_count=start_count, cleanup_state="pending",
                )
            if completion.result != "success" or not _us.exit_status_within_policy(
                completion.exec_main_status, completion.success_exit
            ):
                raise self._uf_failed(
                    "disabled first fire completed unsuccessfully",
                    phase="completion", classification="completion-failed",
                    snapshot=completion, start_count=start_count, cleanup_state="pending",
                )
            if viability.require_invocation_receipt and not _us.valid_invocation_id(invocation_id):
                raise OperationFailed("disabled first fire returned malformed InvocationID")
            # T-5572: the pure receipt validator lives in the stdlib-only leaf
            # tools.runtime_invocation_receipt (runtime_controller re-exports it), so
            # this path keeps the executor's closed module set.
            receipt = _receipt.validate_initial_viability_invocation_receipt(
                operation.provenance["persona"],
                invocation_id=invocation_id,
                state_root=viability.persona_state_root,
                allowed_runtime_refs=viability.allowed_runtime_refs,
                require_receiver_receipt=viability.receiver_required,
                receiver_operations_by_runtime=viability.receiver_operations_by_runtime,
            )
            observed = {
                "invocation_id": invocation_id,
                "active_state": completion.active_state or "",
                "result": completion.result or "",
                "exec_main_status": _us.render_exec_status(completion.exec_main_status),
                "success_exit_status": _us.render_success_exit(completion.success_exit),
                "post_exit_invocation_id_state": "same" if completion.invocation_id else "empty",
                "pre_enqueue_active_state": baseline.active_state or "",
                "pre_enqueue_job": _us.render_job(baseline.job_id),
                "capture_active_state": (last.active_state or "") if last is not None else "",
                "capture_attempts": str(capture_attempts),
                "timer_enabled": "false",
                "unit_fire_timeout_seconds": str(budget.seconds),
                "unit_fire_timeout_candidate_count": str(budget.candidate_count),
                "unit_fire_timeout_post_start_count": str(budget.post_start_count),
                "selected_runtime": str(receipt["selected_runtime"]),
                "selected_harness": str(receipt["selected_harness"]),
                "selected_profile": str(receipt["selected_profile"]),
                "provider_completion_status": str(receipt["provider_completion_status"]),
                "initial_viability_receipt_sha256": str(receipt["invocation_receipt_sha256"]),
                **self._port_labels(),
            }
        except Exception as exc:
            if started:
                try:
                    cleanup = self._uf_stop_and_prove(unit, total_deadline)
                    cleanup_state = cleanup.active_state or ""
                except OperationFailed as cleanup_exc:
                    if cleanup_exc.evidence:
                        raise cleanup_exc from exc
                    raise self._uf_failed(
                        str(cleanup_exc), phase="cleanup", classification="unsafe-cleanup",
                        snapshot=last, start_count=start_count, cleanup_state="unsafe-active",
                    ) from cleanup_exc
                if isinstance(exc, OperationFailed) and exc.evidence:
                    evidence = dict(exc.evidence)
                    evidence["cleanup_state"] = cleanup_state
                    raise OperationFailed(str(exc), evidence=evidence) from exc
            raise
        return _no_mutation(operation), self._evidence(operation, True, "disabled first fire observed", observed)

    # -- timer-enable ----------------------------------------------------

    def _apply_timer_enable(self, operation: InstallOperation) -> tuple[MutationRecord, OperationEvidence]:
        unit = self._unit(operation, "target")
        link, canonical = self._timer_paths(operation, unit)
        try:
            snapshot = self._user_manager.read_unit(unit, ReadPurpose.UNIT_FILE_ACTIVE, timeout=self._call_timeout())
        except _us.UserManagerCallFailed as exc:
            raise OperationFailed(exc.detail or "timer state unavailable") from None
        state = _timer_unit_state_from_snapshot(snapshot)
        if state.classification == "not-found":
            raise OperationFailed("timer unit is not loaded")
        before_enabled = state.classification == "enabled"
        if before_enabled and unit not in self._seeded:
            # fail closed (the installer's staged-activate boundary): an
            # enabled timer is admitted only as converged legacy state
            raise OperationFailed("timer already enabled at the activation boundary")
        if before_enabled:
            proof = self._seat_link_proof(link, canonical)
            if proof.snapshot_state not in {"absent", "symlink"} or (
                proof.snapshot_state == "symlink" and not _timer_enablement_link_matches_canonical(proof)
            ):
                raise OperationFailed("enabled timer link is not canonical")
            mutation = MutationRecord(
                operation.operation_id, operation.kind, operation.target, operation.owner,
                False, {"state": "enabled"}, "timer-enabled",
            )
            return mutation, self._evidence(
                operation, True, "timer already enabled (converged legacy)",
                {"timer_enabled": "true", **_timer_enablement_link_evidence("post", proof), **self._port_labels()},
            )
        if self._seat_link_proof(link, canonical).snapshot_state != "absent":
            raise OperationFailed(f"{operation.operation_id}: consume timer enablement link already exists")
        # From here the enable call is ISSUED: the mutation record is owned
        # and always returned, whatever happens next (D6: cleanup authority
        # comes from this record, never from the broker's intent alone).
        failure = ""
        outcome: _us.UserManagerCallOutcome | None = None
        try:
            outcome = self._user_manager.enable_now(unit)
        except Exception as exc:  # the call went out; keep the owned record
            failure = f"timer enable call failed: {type(exc).__name__}: {exc}"
        proof: TimerEnablementLinkProof | None = None
        try:
            proof = self._seat_link_proof(link, canonical)
        except Exception as exc:
            failure = failure or f"post-enable link proof failed: {type(exc).__name__}"
        before = {
            "state": "disabled",
            "activation_intent": "false",
            "owned": "true",
            "enablement_link_state": "absent",
            "enablement_link_path": str(link),
            "canonical_template_path": str(canonical),
            "canonical_template_path_sha256": _sha256_bytes(str(canonical).encode("utf-8")),
            "enablement_link_actual_readlink": proof.actual_readlink if proof else "",
            "enablement_link_actual_readlink_sha256": proof.actual_readlink_sha256 if proof else "",
            "enablement_link_resolved_target": str(proof.resolved_target or "") if proof else "",
            "enablement_link_resolved_target_sha256": proof.resolved_target_sha256 if proof else "",
        }
        mutation = MutationRecord(
            operation.operation_id, operation.kind, operation.target, operation.owner,
            True, before, "timer-enabled",
        )
        values = {
            "enable_command_issued": "true",
            "enable_command_unit": unit,
            "enable_ok": str(bool(outcome and outcome.ok)).lower(),
            "enable_error_name": outcome.error_name if outcome else "",
            **(_timer_enablement_link_evidence("post", proof) if proof else {}),
            **self._port_labels(),
        }
        if not failure and outcome is not None and not outcome.ok:
            failure = f"timer enable command failed: {outcome.detail or outcome.error_name}"
        if not failure and (proof is None or not _timer_enablement_link_matches_canonical(proof)):
            failure = "timer enablement link did not resolve to the canonical template after enable"
        if failure:
            return mutation, self._evidence(operation, False, failure, values)
        values["timer_enabled"] = "true"
        return mutation, self._evidence(operation, True, "timer enabled", values)

    # -- timer-observe ---------------------------------------------------

    def _apply_timer_observe(self, operation: InstallOperation) -> tuple[MutationRecord, OperationEvidence]:
        if operation.target in self._seeded:
            self._unit(operation, "target")
            return _no_mutation(operation), self._evidence(
                operation, True, "existing live convergence evidence retained",
                dict(self._seeded[operation.target]["timer-observe"]),
            )
        service = self._unit(operation, "service")
        timer = self._unit(operation, "target")
        unit_fire = self._run_evidence.get("unit-fire")
        if unit_fire is None or not unit_fire.ok:
            raise OperationFailed("timer observation lacks disabled first-fire evidence")
        first_invocation = unit_fire.evidence.get("invocation_id", "")
        receipt_sha = unit_fire.evidence.get("initial_viability_receipt_sha256", "")
        if not _us.valid_invocation_id(first_invocation) or not receipt_sha:
            raise OperationFailed("timer observation lacks immutable first-fire receipt evidence")
        service_state = self._user_manager.read_unit(service, ReadPurpose.SERVICE_OBSERVE, timeout=self._call_timeout())
        current = service_state.invocation_id or ""
        if current and current != first_invocation:
            raise OperationFailed("consume service InvocationID changed after timer arm")
        if (
            service_state.active_state != "inactive"
            or service_state.result != "success"
            or not _us.exit_status_within_policy(service_state.exec_main_status, service_state.success_exit)
        ):
            raise OperationFailed("consume service is not inactive successful after timer arm")
        timer_state = self._user_manager.read_unit(timer, ReadPurpose.TIMER_ELAPSE, timeout=self._call_timeout())
        if timer_state.active_state != "active" or timer_state.sub_state != "waiting":
            raise OperationFailed("timer is not active and waiting after arm")
        basis = _us.snapshot_finite_next_elapse_basis(timer_state)
        if basis is None:
            raise OperationFailed("next timer elapse is not finite after inactivity")
        mailbox = self._run_evidence.get("mailbox-probe")
        probe_sha = mailbox.evidence.get("sha256", "") if mailbox is not None and mailbox.ok else ""
        if not probe_sha:
            raise OperationFailed("timer arm lacks correlated durable mailbox-probe evidence")
        return _no_mutation(operation), self._evidence(
            operation,
            True,
            "timer armed and observed",
            {
                "next_elapse_basis": basis,
                "next_elapse_finite": "true",
                "first_fire_invocation_observed": "true",
                "second_consume_absent": "true",
                "mailbox_effect_correlated": "true",
                "mailbox_probe_sha256": probe_sha,
                "unit_fire_invocation_id": first_invocation,
                "post_exit_invocation_id_state": "same" if current else "empty",
                "initial_viability_receipt_sha256": receipt_sha,
                **self._port_labels(),
            },
        )

    _ACTIVATION_APPLY: Final = {
        "daemon-reload": _apply_daemon_reload,
        "stale-unit-failure-reset": _apply_stale_reset,
        "direct-fire": _apply_direct_fire,
        "unit-fire": _apply_unit_fire,
        "timer-enable": _apply_timer_enable,
        "timer-observe": _apply_timer_observe,
    }

    # ------------------------------------------------------------------
    # Verify: read-only live observations (no mutation record)
    # ------------------------------------------------------------------

    def _verify_daemon_reload(self, operation: InstallOperation) -> OperationEvidence:
        unit = operation.desired.get("unit", "")
        if unit:
            snapshot = self._user_manager.read_unit(self._unit(operation, "unit"), ReadPurpose.RELOAD_STATE, timeout=self._call_timeout())
            ok = snapshot.need_daemon_reload is False
            return self._evidence(
                operation,
                ok,
                "no daemon reload pending" if ok else "daemon reload pending for the named unit",
                {
                    "observation": "need-daemon-reload",
                    "need_daemon_reload": "false" if ok else "true-or-unknown",
                    **self._port_labels(),
                },
            )
        entries = self._user_manager.manager_environment()
        return self._evidence(
            operation, True, "user manager reachable",
            {"observation": "manager-environment", "environment_entries": str(entries), **self._port_labels()},
        )

    def _verify_stale_reset(self, operation: InstallOperation) -> OperationEvidence:
        snapshot = self._stale_read(self._unit(operation, "target"))
        values = {**_stale_unit_failure_reset_snapshot_evidence("live", snapshot), "observation": "live-state", **self._port_labels()}
        try:
            classification = _classify_stale_unit_failure_reset_snapshot(snapshot)
        except OperationFailed as exc:
            return self._evidence(operation, False, str(exc), values)
        ok = classification == "clean-noop"
        return self._evidence(
            operation, ok, "consume service carries no stale failure" if ok else "consume service still carries a stale failure", values
        )

    def _verify_direct_fire(self, operation: InstallOperation) -> OperationEvidence:
        # ruling D3: re-run diagnose (read-only)
        ok, detail, values = self._diagnose(operation, "rerun-diagnose")
        return self._evidence(operation, ok, detail, values)

    def _verify_unit_fire(self, operation: InstallOperation) -> OperationEvidence:
        snapshot = self._user_manager.read_unit(self._unit(operation, "target"), ReadPurpose.UNIT_FIRE_POLL, timeout=self._call_timeout())
        ok = (
            snapshot.active_state == "inactive"
            and snapshot.result == "success"
            and not _us.unit_fire_has_failed(snapshot)
            and _us.exit_status_within_policy(snapshot.exec_main_status, snapshot.success_exit)
        )
        return self._evidence(
            operation,
            ok,
            "consume service is inactive after a successful fire" if ok else "consume service is not inactive successful",
            {
                "observation": "live-state",
                "active_state": snapshot.active_state or "",
                "result": snapshot.result or "",
                "exec_main_status": _us.render_exec_status(snapshot.exec_main_status),
                **_us.snapshot_digest_evidence("", snapshot.source),
                **self._port_labels(),
            },
        )

    def _verify_timer_enable(self, operation: InstallOperation) -> OperationEvidence:
        unit = self._unit(operation, "target")
        link, canonical = self._timer_paths(operation, unit)
        state = _timer_unit_state_from_snapshot(self._user_manager.read_unit(unit, ReadPurpose.UNIT_FILE_ACTIVE, timeout=self._call_timeout()))
        proof = self._seat_link_proof(link, canonical)
        ok = state.classification == "enabled" and _timer_enablement_link_matches_canonical(proof)
        return self._evidence(
            operation,
            ok,
            "timer enabled with the canonical link" if ok else "timer not enabled with the canonical link",
            {"observation": "live-state", "classification": state.classification,
             **_timer_enablement_link_evidence("live", proof), **self._port_labels()},
        )

    def _verify_timer_observe(self, operation: InstallOperation) -> OperationEvidence:
        service = self._unit(operation, "service")
        timer = self._unit(operation, "target")
        service_state = self._user_manager.read_unit(service, ReadPurpose.SERVICE_OBSERVE, timeout=self._call_timeout())
        timer_state = self._user_manager.read_unit(timer, ReadPurpose.TIMER_ELAPSE, timeout=self._call_timeout())
        basis = _us.snapshot_finite_next_elapse_basis(timer_state)
        ok = (
            service_state.active_state == "inactive"
            and service_state.result == "success"
            and _us.exit_status_within_policy(service_state.exec_main_status, service_state.success_exit)
            and timer_state.active_state == "active"
            and timer_state.sub_state == "waiting"
            and basis is not None
        )
        return self._evidence(
            operation,
            ok,
            "timer active and waiting with a finite next elapse" if ok else "timer or consume service not in the armed shape",
            {"observation": "live-state", "next_elapse_basis": basis or "", **self._port_labels()},
        )

    _ACTIVATION_VERIFY: Final = {
        "daemon-reload": _verify_daemon_reload,
        "stale-unit-failure-reset": _verify_stale_reset,
        "direct-fire": _verify_direct_fire,
        "unit-fire": _verify_unit_fire,
        "timer-enable": _verify_timer_enable,
        "timer-observe": _verify_timer_observe,
    }

    # ------------------------------------------------------------------
    # reconcile: the read-only live-state probe (T-2917 5d.2, T-2917:D13)
    # ------------------------------------------------------------------

    def _run_reconcile(self, spec: ParsedSpec, evidence: list[OperationEvidence]) -> ExecutionResult:
        for operation in spec.operations:
            try:
                if self._clock() >= self._deadline:
                    raise OperationFailed("executor run budget exhausted before this observation")
                observed = self._observe(operation)
            except Exception as exc:
                observed = self._evidence(operation, False, _safe_detail(exc), {"observation": "failed"})
            evidence.append(observed)
        ok = bool(evidence) and all(item.ok for item in evidence)
        return ExecutionResult(
            ok=ok,
            mutations=(),
            evidence=tuple(evidence),
            detail=f"reconcile probe {'complete' if ok else 'incomplete'}: "
            f"{sum(item.ok for item in evidence)}/{len(evidence)} observed",
        )

    def _observe(self, operation: InstallOperation) -> OperationEvidence:
        capability = EXECUTOR_KIND_CLASSES.get(operation.kind)
        if capability == "files.seat_root":
            snapshot = self._files.snapshot(operation.target)
            state = snapshot.get("state", "")
            if operation.kind == "mkdir":
                at_desired = state == "directory"
            else:
                at_desired = state == "file" and snapshot.get("fingerprint") == operation.desired.get("sha256")
            return self._evidence(
                operation,
                True,
                "live target observed",
                {
                    "observation": "file-target",
                    "state": state,
                    "fingerprint": str(_snapshot_fingerprint(snapshot) or ""),
                    "at_desired": str(at_desired).lower(),
                },
            )
        if operation.kind == "timer-enable":
            unit = self._unit(operation, "target")
            link, canonical = self._timer_paths(operation, unit)
            proof = self._seat_link_proof(link, canonical)
            state = _timer_unit_state_from_snapshot(
                self._user_manager.read_unit(unit, ReadPurpose.UNIT_FILE_ACTIVE, timeout=self._call_timeout())
            )
            return self._evidence(
                operation,
                True,
                "live timer enablement observed",
                {
                    "observation": "timer-enable",
                    "classification": state.classification,
                    "active_state": state.active_state,
                    "link_state": proof.snapshot_state,
                    "link_canonical": str(_timer_enablement_link_matches_canonical(proof)).lower(),
                    **self._port_labels(),
                },
            )
        if capability == "mailbox.probe":
            values = self._require_mailbox().residue(
                operation_handle=self._operation_handle, operation=operation
            )
            return self._evidence(operation, True, "mailbox probe residue observed", values)
        return self._evidence(operation, True, "not probed", {"observation": "not-probed"})

    # ------------------------------------------------------------------
    # rollback: receipt-bound restore (T-2917 5d.2, T-2917:D15)
    # ------------------------------------------------------------------

    def _run_rollback(
        self,
        spec: ParsedSpec,
        mutations: list[MutationRecord],
        evidence: list[OperationEvidence],
    ) -> ExecutionResult:
        operations = {operation.operation_id: operation for operation in spec.operations}
        checks: list[tuple[MutationRecord, InstallOperation, str, str]] = []
        for mutation in spec.rollback_mutations:
            operation = operations[mutation.operation_id]
            try:
                classification, detail = self._classify_restore(mutation, operation)
            except Exception as exc:
                classification, detail = "refused", _safe_detail(exc)
            checks.append((mutation, operation, classification, detail))
        blocked = [item for item in checks if item[2] not in ("owned", "not-applied")]
        if blocked:
            # the installer's rule: any drift refuses the WHOLE rollback
            # before any change
            refusals = tuple(
                self._evidence(operation, False, f"{classification}: {detail}", {"classification": classification})
                for _mutation, operation, classification, detail in blocked
            )
            return ExecutionResult(
                ok=False,
                mutations=(),
                evidence=refusals,
                detail="rollback refused before any change: "
                + ",".join(sorted({item[2] for item in blocked})),
            )
        used = 0
        for mutation, operation, classification, detail in reversed(checks):
            if classification == "not-applied":
                observed = self._evidence(
                    operation, True, f"not-applied: {detail}", {"classification": "not-applied"}
                )
                evidence.append(observed)
                used += _safe_record_size(None, observed)
                continue
            self._remaining = self._record_budget - used
            record: MutationRecord | None = None
            try:
                if self._clock() >= self._deadline:
                    raise OperationFailed("executor run budget exhausted before this restore")
                record, observed = self._restore(mutation, operation)
            except Exception as exc:
                values = getattr(exc, "evidence", None) if isinstance(exc, OperationFailed) else None
                try:
                    observed = self._evidence(operation, False, _safe_detail(exc), values)
                except Exception:
                    observed = self._evidence(operation, False, _safe_detail(exc))
            used += _safe_record_size(record, observed)
            if record is not None:
                mutations.append(record)
            evidence.append(observed)
            if not observed.ok:
                break
        ok = bool(evidence) and all(item.ok for item in evidence)
        restored = len(mutations)
        return ExecutionResult(
            ok=ok,
            mutations=tuple(mutations),
            evidence=tuple(evidence),
            detail=f"rollback {'succeeded' if ok else 'failed'}: {restored} restored, "
            f"{sum(1 for item in checks if item[2] == 'not-applied')} not applied",
        )

    def _classify_restore(self, mutation: MutationRecord, operation: InstallOperation) -> tuple[str, str]:
        """The installer's ``can_rollback`` over the executor's confined
        reads: ``owned`` | ``not-applied`` | ``drifted`` | ``refused``."""

        if mutation.kind == "timer-enable":
            return self._classify_timer_restore(mutation, operation)
        if EXECUTOR_KIND_CLASSES.get(mutation.kind) != "files.seat_root":
            return "refused", f"{mutation.kind} is not restorable by the executor"
        before_state = mutation.before.get("state")
        if before_state == "file":
            raw = mutation.before.get("content_b64")
            if raw is None:
                return "refused", "file before-snapshot carries no content"
            try:
                content = base64.b64decode(raw, validate=True)
            except ValueError:
                return "refused", "file before-snapshot content is not base64"
            if hashlib.sha256(content).hexdigest() != mutation.before.get("fingerprint"):
                return "refused", "file before-snapshot content does not match its fingerprint"
        elif before_state != "absent":
            return "refused", f"unsupported before-state {before_state!r}"
        current = _snapshot_fingerprint(self._files.snapshot(mutation.target))
        if current == _snapshot_fingerprint(mutation.before):
            return "not-applied", "target already matches the pre-apply receipt state"
        if current != mutation.after_fingerprint:
            return "drifted", "post-apply fingerprint changed"
        return "owned", "post-apply fingerprint matches receipt"

    def _receipt_timer_paths(self, mutation: MutationRecord, operation: InstallOperation) -> tuple[Path, Path]:
        link, canonical = self._timer_paths(operation, self._unit(operation, "target"))
        if (
            mutation.before.get("enablement_link_path") != str(link)
            or mutation.before.get("canonical_template_path") != str(canonical)
        ):
            raise OperationFailed("timer receipt link ownership evidence does not match the seat-derived paths")
        return link, canonical

    def _receipt_link_matches(self, mutation: MutationRecord, proof: TimerEnablementLinkProof) -> bool:
        if proof.snapshot_state != "symlink" or not _timer_enablement_link_matches_canonical(proof):
            return False
        recorded = mutation.before.get("enablement_link_actual_readlink", "")
        recorded_sha = mutation.before.get("enablement_link_actual_readlink_sha256", "")
        if recorded and proof.actual_readlink != recorded:
            return False
        if recorded_sha and proof.actual_readlink_sha256 != recorded_sha:
            return False
        return True

    def _classify_timer_restore(self, mutation: MutationRecord, operation: InstallOperation) -> tuple[str, str]:
        before = mutation.before
        if before.get("state") != "disabled":
            return "refused", "timer receipt before-state is not an owned enable"
        if before.get("owned") != "true" or before.get("activation_intent") != "false":
            # D6, the executor layer: an intent is never cleanup authority
            return "refused", "timer enablement is not this executor's owned record (D6)"
        link, canonical = self._receipt_timer_paths(mutation, operation)
        proof = self._seat_link_proof(link, canonical)
        link_owned = self._receipt_link_matches(mutation, proof)
        if proof.snapshot_state != "absent" and not link_owned:
            return "drifted", "timer enablement link does not match the receipt"
        state = _timer_unit_state_from_snapshot(
            self._user_manager.read_unit(mutation.target, ReadPurpose.UNIT_FILE_ACTIVE, timeout=self._call_timeout())
        )
        inactive = state.active_state == "inactive" and state.sub_state == "dead"
        if link_owned:
            return "owned", "timer enablement link matches receipt"
        if state.classification in ("disabled", "not-found") and inactive:
            return "not-applied", "timer still matches the write-ahead state"
        if state.classification == "not-found":
            return "owned", "timer unit file is removed but the receipt-owned timer is active"
        if state.classification != "enabled":
            return "drifted", "timer is not in the receipt-owned enabled state"
        return "owned", "timer enable state matches receipt"

    def _restore(self, mutation: MutationRecord, operation: InstallOperation) -> tuple[MutationRecord, OperationEvidence]:
        if mutation.kind == "timer-enable":
            return self._restore_timer(mutation, operation)
        return self._restore_file(mutation, operation)

    def _restore_file(self, mutation: MutationRecord, operation: InstallOperation) -> tuple[MutationRecord, OperationEvidence]:
        target = mutation.target
        pre = self._files.snapshot(target)
        current = _snapshot_fingerprint(pre)
        projected = record_size(
            MutationRecord(mutation.operation_id, mutation.kind, target, mutation.owner, True, pre, "f" * 64),
            None,
        ) + EVIDENCE_RESERVE_BYTES
        if projected > self._remaining:
            raise ResultBudgetExceeded(
                f"restore record of {projected} bytes exceeds the remaining result budget; refused before any change"
            )
        before_state = mutation.before.get("state")
        changed = False
        failure = ""
        action = "none"
        try:
            if before_state == "absent":
                if pre.get("state") == "directory":
                    action = "rmdir-if-empty"
                    changed = self._files.rmdir_if_empty(target)
                else:
                    action = "unlink"
                    changed = self._files.remove_file(target, str(current))
            else:
                action = "restore-bytes"
                data = base64.b64decode(mutation.before["content_b64"], validate=True)
                mode = parse_mode(mutation.before.get("mode"), "0644")
                changed = self._files.restore_file(target, data, mode, current)
        except SeatChangeIncomplete as exc:
            changed = True
            failure = _bounded(exc)
        after_state = ""
        after_fingerprint: str | None = None
        try:
            after = self._files.snapshot(target)
            after_state = after.get("state", "")
            after_fingerprint = _snapshot_fingerprint(after)
        except Exception as exc:  # committed: never lose the record
            if not changed:
                raise
            failure = failure or f"after-snapshot failed: {type(exc).__name__}"
        record = MutationRecord(
            operation_id=mutation.operation_id,
            kind=mutation.kind,
            target=target,
            owner=mutation.owner,
            changed=changed,
            before=pre,
            after_fingerprint=after_fingerprint,
        )
        values = {"action": action, "after_state": after_state or "unknown", "restored_from": "receipt-before-snapshot"}
        if failure:
            return record, self._evidence(operation, False, f"restored, then failed: {failure}", values)
        if not changed:
            # installer parity: a non-empty created directory is preserved
            return record, self._evidence(operation, True, "kept: directory not empty", values)
        return record, self._evidence(operation, True, "restored", values)

    def _prove_inactive_dead(self, unit: str, *, not_found: bool = False) -> None:
        snapshot = self._user_manager.read_unit(unit, ReadPurpose.UNIT_FILE_ACTIVE, timeout=self._call_timeout())
        proved = _us.proves_notfound_inactive_dead(snapshot) if not_found else _us.proves_inactive_dead(snapshot)
        if not proved:
            raise OperationFailed(
                f"{unit} did not prove {'not-found/' if not_found else ''}inactive/dead: "
                f"LoadState={snapshot.load_state} ActiveState={snapshot.active_state} SubState={snapshot.sub_state}"
            )

    def _stop_or_converged(self, unit: str) -> None:
        outcome = self._user_manager.stop_unit(unit, timeout=self._time_left())
        if outcome.ok:
            self._prove_inactive_dead(unit)
            return
        try:
            self._prove_inactive_dead(unit, not_found=True)
        except OperationFailed as exc:
            raise OperationFailed(
                f"{unit} stop failed and did not converge to not-found/inactive/dead: {exc}",
                evidence={"error_name": outcome.error_name},
            ) from exc

    def _restore_timer(self, mutation: MutationRecord, operation: InstallOperation) -> tuple[MutationRecord, OperationEvidence]:
        """The installer's owned timer-enable rollback, through the port:
        stop timer and service, prove inactive, remove the receipt-owned
        link, disable, reload, reset-failed both, then re-prove."""

        timer = self._unit(operation, "target")
        if timer != mutation.target:
            raise UnitRefused("timer restore names a unit other than its operation's")
        service = self._paired_service(timer)
        link, canonical = self._receipt_timer_paths(mutation, operation)
        pre_proof = self._seat_link_proof(link, canonical)
        before = {
            "state": "enabled",
            "owned": "true",
            "restored_by": "rollback",
            "enablement_link_state": pre_proof.snapshot_state,
            "enablement_link_path": str(link),
            "enablement_link_actual_readlink": pre_proof.actual_readlink,
        }
        record = MutationRecord(mutation.operation_id, mutation.kind, timer, mutation.owner, True, before, "timer-disabled")
        values = {"timer": timer, "service": service, **self._port_labels()}
        # from the first stop on, a host change may have happened: the
        # restore record is owned and always returned (never dropped)
        try:
            self._stop_or_converged(timer)
            self._stop_or_converged(service)
            self._prove_inactive_dead(service)
            self._prove_inactive_dead(timer)
            proof = self._seat_link_proof(link, canonical)
            if proof.snapshot_state != "absent":
                if not self._receipt_link_matches(mutation, proof):
                    raise OperationFailed("timer enablement link changed since it was classified")
                self._files.remove_symlink(str(link), proof.actual_readlink)
            disabled = self._user_manager.disable_now(timer)
            if not disabled.ok:
                self._prove_inactive_dead(timer)
                self._prove_inactive_dead(service)
                if disabled.error_name not in _DISABLE_ABSENT_ERRORS:
                    raise OperationFailed(disabled.detail or "timer disable failed", evidence={"error_name": disabled.error_name})
            for name, outcome in (
                ("reload", self._user_manager.reload()),
                ("reset_failed_timer", self._user_manager.reset_failed_unit(timer)),
                ("reset_failed_service", self._user_manager.reset_failed_unit(service)),
            ):
                if not outcome.ok:
                    raise OperationFailed(f"{name} failed: {outcome.detail or outcome.error_name}")
            if self._seat_link_proof(link, canonical).snapshot_state != "absent":
                raise OperationFailed("timer enablement link still exists after rollback")
            self._prove_inactive_dead(timer)
            self._prove_inactive_dead(service)
        except Exception as exc:
            return record, self._evidence(operation, False, f"timer restore failed: {_safe_detail(exc)}", values)
        return record, self._evidence(operation, True, "owned timer enablement rolled back", values)


# a disable of an absent unit file is converged, not a failure (the
# installer tolerated "not exist"/"not loaded"/"No such file" output)
_DISABLE_ABSENT_ERRORS: Final = frozenset(
    (
        "org.freedesktop.systemd1.NoSuchUnit",
        "org.freedesktop.DBus.Error.FileNotFound",
        "org.freedesktop.systemd1.LoadFailed",
    )
)
