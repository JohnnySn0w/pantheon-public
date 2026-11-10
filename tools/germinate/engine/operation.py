"""Typed operations (ORG-080 §2 "Operation-level fields").

Every engine op is persona_installer's ``InstallOperation`` shape plus the two
ORG-080 fields ``crash_contract`` and ``side_effect_free`` (and an optional
``gate`` naming a declared contract gate or reserved authority). Planners
return these as plain data; ``parse_operation`` is the plan-time check.

PROVISIONAL reading (T-5074 bubble 4): ORG-080 §1's ``privilege: none | seat |
root`` maps onto the installer's levels via ``INSTALLER_PRIVILEGE`` (none and
seat run unprivileged; root needs the privileged helper). The table is the one
place that reading lives.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Mapping

from tools.germinate.op_registry import LLM_PATH_KINDS, PRIVILEGES, OpRegistry
from tools.persona_installer import InstallOperation

if TYPE_CHECKING:  # pragma: no cover
    from tools.germinate.engine.contract import LoadedContract

INSTALLER_PRIVILEGE: Final[Mapping[str, str]] = {
    "none": "unprivileged",
    "seat": "unprivileged",
    "root": "privileged-helper",
}
CRASH_CONTRACTS: Final = frozenset({"stageable", "write-ahead-inverse-durable"})
EXECUTION_SCOPES: Final = frozenset({"all", "staged-cutover"})
_OP_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # the WAL's op_id alphabet

REQUIRED: Final = frozenset(
    {
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
        "crash_contract",
        "side_effect_free",
    }
)
OPTIONAL: Final = frozenset({"gate", "execution_scope", "rollback_on_halt"})


class OperationError(ValueError):
    """An op a planner returned is refused at plan."""


@dataclass(frozen=True)
class Operation:
    operation_id: str
    layer: int
    kind: str
    target: str
    precondition: Mapping[str, str]
    desired: Mapping[str, str]
    inverse: Mapping[str, str] | None
    privilege: str
    owner: str
    provenance: Mapping[str, str]
    crash_contract: str | None
    side_effect_free: bool
    gate: str | None = None
    execution_scope: str = "all"
    #: T-5866: opt-in. When this op HALTS, the engine runs rollback(run_id) at once (see
    #: executor ``_rollback_on_halt``). Written to JSON only when True, so the plan bytes
    #: and plan digest of every op that does not opt in are unchanged.
    rollback_on_halt: bool = False

    def to_json_obj(self) -> dict[str, object]:
        out: dict[str, object] = {
            "crash_contract": self.crash_contract,
            "desired": dict(self.desired),
            "execution_scope": self.execution_scope,
            "gate": self.gate,
            "inverse": None if self.inverse is None else dict(self.inverse),
            "kind": self.kind,
            "layer": self.layer,
            "operation_id": self.operation_id,
            "owner": self.owner,
            "precondition": dict(self.precondition),
            "privilege": self.privilege,
            "provenance": dict(self.provenance),
            "side_effect_free": self.side_effect_free,
            "target": self.target,
        }
        if self.rollback_on_halt:
            out["rollback_on_halt"] = True
        return out

    def to_install_operation(self) -> InstallOperation:
        return InstallOperation(
            operation_id=self.operation_id,
            layer=self.layer,
            kind=self.kind,  # type: ignore[arg-type]
            target=self.target,
            precondition=dict(self.precondition),
            desired=dict(self.desired),
            inverse=None if self.inverse is None else dict(self.inverse),
            privilege=INSTALLER_PRIVILEGE[self.privilege],  # type: ignore[arg-type]
            owner=self.owner,
            provenance=dict(self.provenance),
            execution_scope=self.execution_scope,  # type: ignore[arg-type]
        )


def _str_map(where: str, value: Any, *, nullable: bool = False) -> Mapping[str, str] | None:
    if value is None and nullable:
        return None
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise OperationError(f"{where} must be a mapping of strings to strings")
    return dict(value)


def operation_from_json_obj(raw: Mapping[str, Any]) -> Operation:
    """Rebuild an op from its stored JSON (plan.json); shape only."""

    return Operation(
        operation_id=raw["operation_id"],
        layer=raw["layer"],
        kind=raw["kind"],
        target=raw["target"],
        precondition=dict(raw["precondition"]),
        desired=dict(raw["desired"]),
        inverse=None if raw["inverse"] is None else dict(raw["inverse"]),
        privilege=raw["privilege"],
        owner=raw["owner"],
        provenance=dict(raw["provenance"]),
        crash_contract=raw["crash_contract"],
        side_effect_free=raw["side_effect_free"],
        gate=raw.get("gate"),
        execution_scope=raw.get("execution_scope", "all"),
        rollback_on_halt=raw.get("rollback_on_halt", False) is True,
    )


def parse_operation(raw: Any, *, contract: "LoadedContract", op_registry: OpRegistry) -> Operation:
    """Plan-time validation of one planner-returned op; every refusal is an OperationError."""

    if not isinstance(raw, dict):
        raise OperationError("an operation must be a mapping")
    where = f"{contract.id}: op {raw.get('operation_id', '?')!r}"
    missing = REQUIRED - set(raw)
    if missing:
        raise OperationError(f"{where}: missing fields {sorted(missing)}")
    unknown = set(raw) - REQUIRED - OPTIONAL
    if unknown:
        raise OperationError(f"{where}: unknown fields {sorted(unknown)}")
    op_id = raw["operation_id"]
    if not (isinstance(op_id, str) and _OP_ID_RE.match(op_id)):
        raise OperationError(f"{where}: operation_id must match [A-Za-z0-9_-]{{1,64}}")
    kind = raw["kind"]
    if kind in LLM_PATH_KINDS:
        raise OperationError(f"{where}: kind {kind!r} puts a model turn on the execution path (ORG-080 D-N llm_free)")
    entry = op_registry.get(kind) if isinstance(kind, str) else None
    if entry is None:
        raise OperationError(f"{where}: kind {kind!r} is not in the typed-op registry")
    layer = raw["layer"]
    if not (isinstance(layer, int) and not isinstance(layer, bool) and layer >= 0):
        raise OperationError(f"{where}: layer must be a non-negative integer")
    for key in ("target", "owner"):
        if not (isinstance(raw[key], str) and raw[key]):
            raise OperationError(f"{where}: {key} must be a non-empty string")
    privilege = raw["privilege"]
    if privilege not in PRIVILEGES:
        raise OperationError(f"{where}: privilege {privilege!r} not in {PRIVILEGES}")
    if privilege != entry.privilege:
        raise OperationError(f"{where}: privilege {privilege!r} disagrees with the registry ({entry.privilege!r} for {kind!r})")
    lists = contract.privilege
    if privilege == "root" and kind not in lists.get("bootstrap_root", frozenset()):
        raise OperationError(f"{where}: root kind {kind!r} is not in the contract's bootstrap_root list")
    if privilege == "seat" and kind not in lists.get("seat", frozenset()) | lists.get("broker_runtime", frozenset()):
        raise OperationError(f"{where}: seat kind {kind!r} is not in the contract's seat/broker_runtime lists")
    side_effect_free = raw["side_effect_free"]
    crash_contract = raw["crash_contract"]
    if not isinstance(side_effect_free, bool):
        raise OperationError(f"{where}: side_effect_free must be a boolean")
    if entry.read_only:
        if side_effect_free is not True:
            raise OperationError(f"{where}: read-only kind {kind!r} must declare side_effect_free: true")
        if crash_contract is not None:
            raise OperationError(f"{where}: a side-effect-free op carries no crash_contract")
    else:
        if side_effect_free:
            raise OperationError(f"{where}: kind {kind!r} mutates, so it cannot declare side_effect_free (ORG-080 §2)")
        if crash_contract not in CRASH_CONTRACTS:
            raise OperationError(
                f"{where}: a mutating op must declare crash_contract stageable or write-ahead-inverse-durable (ORG-080 D-X)"
            )
    gate = raw.get("gate")
    if gate is not None and gate not in contract.gates and gate not in contract.reserved_authority:
        raise OperationError(f"{where}: gate {gate!r} is neither a declared gate nor reserved authority")
    scope = raw.get("execution_scope", "all")
    if scope not in EXECUTION_SCOPES:
        raise OperationError(f"{where}: execution_scope {scope!r} not in {sorted(EXECUTION_SCOPES)}")
    rollback_on_halt = raw.get("rollback_on_halt", False)
    if not isinstance(rollback_on_halt, bool):
        raise OperationError(f"{where}: rollback_on_halt must be a boolean")
    return Operation(
        operation_id=op_id,
        layer=layer,
        kind=kind,
        target=raw["target"],
        precondition=_str_map(f"{where}.precondition", raw["precondition"]),  # type: ignore[arg-type]
        desired=_str_map(f"{where}.desired", raw["desired"]),  # type: ignore[arg-type]
        inverse=_str_map(f"{where}.inverse", raw["inverse"], nullable=True),
        privilege=privilege,
        owner=raw["owner"],
        provenance=_str_map(f"{where}.provenance", raw["provenance"]),  # type: ignore[arg-type]
        crash_contract=crash_contract,
        side_effect_free=side_effect_free,
        gate=gate,
        execution_scope=scope,
        rollback_on_halt=rollback_on_halt,
    )
