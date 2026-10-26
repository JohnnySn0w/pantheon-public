"""Component contract modules (ORG-080 §2; schema pantheon-component-contract/v0).

A contract is a YAML module with exactly one top-level ``component:`` mapping.
It is the single authority for the ORG-080 §2 fields; the ORG-081 registry row
only references it by ``contract_digest`` (D-AA).

PROVISIONAL reading (T-5074 bubble 6): the planner is the Python module beside
the YAML with the same stem (``<id>.yaml`` + ``<id>.py``); no new contract field.

Digest pins inside ``component:`` (build-altitude detail beneath ORG-080 §2;
each one is covered by ``contract_digest``, so the registry row pins it too):

* ``invariant_list_id`` + ``invariant_list_digest`` -- the invariant list bound
  to the verify leg and the emitter; required once either is declared, and
  recomputed by ``invariant_digest``.
* ``handlers_digest`` (T-5477) -- the executor module ``<id>_handlers.py``
  together with the component-local helpers it imports, recomputed by
  ``handlers_digest``. It is required when that module exists and refused when
  it does not. The engine will not import handlers whose bytes do not match, and
  the validator refuses drift. To re-pin after an edit, run
  ``python3 -m tools.germinate registry repin <id>``. Helpers are verified only
  at discover time; T-5506 tracks that residual.

Two layers:

* static helpers (``invariant_binding_errors``, ``static_predicates``) need only
  the parsed mapping and the planner's AST. The ORG-081 validator uses them
  (D-8 presence, D-11(b) binding) without importing component code, which would
  run component code outside the planner boundary (ORG-080 C5).
* ``load_contract`` is the engine's plan-time load: the static checks plus the
  digest recompute (D-9), the closed ``planner_kind`` enum and the privilege
  lists against the op registry.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from tools.germinate.engine import handlers_digest as HD
from tools.germinate.engine.invariant_digest import InvariantDigestError, compute as compute_list_digest
from tools.germinate.registry.contract_digest import ContractDigestError, compute_contract_digest
from tools.germinate.registry.load import RegistryLoadError, load_file

if TYPE_CHECKING:  # pragma: no cover
    from tools.germinate.op_registry import OpRegistry

SCHEMA = "pantheon-component-contract/v0"
RECEIPT_SCHEMA = "pantheon-receipt/v1"
PLAN_LEGS = ("install", "configure", "update", "remove", "repair")
LEG_KEYS = frozenset(PLAN_LEGS + ("verify",))
SCOPES = frozenset({"system", "seat"})
PRIVILEGE_LISTS = ("bootstrap_root", "seat", "broker_runtime")
GATE_CLASSES = frozenset({"operator", "independent-agent"})

REQUIRED = frozenset(
    {"id", "steward", "version", "planner_kind", "contract_digest", "depends_on", "scopes", "privilege", "legs", "receipts"}
)
OPTIONAL = frozenset(
    {
        "bindings",
        "desired_schema",
        "invariant_list_id",
        "invariant_list_digest",
        "handlers_digest",
        "gates",
        "interactions",
        "reserved_authority",
        "rollback",
        "emitter",
    }
)
_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

PLANNER_KIND_REFUSAL = (
    "component `{id}` declares `planner_kind: {value}`; only `python` is accepted. External-executable "
    "planners are deferred to a follow-on ADR revision; contract-schema field is closed to prevent silent admission."
)


class ContractError(ValueError):
    """A contract module is refused at load (a plan refusal, ORG-080 §2)."""


@dataclass(frozen=True)
class Gate:
    id: str
    approval_class: str
    irreversible: bool


@dataclass(frozen=True)
class DesiredField:
    enum: tuple[str, ...] | None
    default: str | None


def _desired_schema(where: str, raw: Any) -> dict[str, DesiredField]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ContractError(f"{where}: desired_schema must be a mapping of key -> {{enum?, default?}}")
    out = {}
    for key, spec in raw.items():
        if not (isinstance(key, str) and _ID_RE.match(key.replace("_", "-"))) or not isinstance(spec, dict) or not set(spec) <= {"enum", "default"}:
            raise ContractError(f"{where}: desired_schema.{key} must be {{enum?: [str], default?: str}}")
        enum = spec.get("enum")
        if enum is not None and not (isinstance(enum, list) and enum and all(isinstance(v, str) for v in enum)):
            raise ContractError(f"{where}: desired_schema.{key}.enum must be a non-empty list of strings")
        default = spec.get("default")
        if default is not None and (not isinstance(default, str) or (enum is not None and default not in enum)):
            raise ContractError(f"{where}: desired_schema.{key}.default must be a string in its enum")
        out[key] = DesiredField(tuple(enum) if enum is not None else None, default)
    return out


@dataclass(frozen=True)
class LoadedContract:
    path: Path
    planner_path: Path
    digest: str
    id: str
    steward: str
    depends_on: tuple[str, ...]
    scopes: frozenset[str]
    privilege: Mapping[str, frozenset[str]]
    legs: Mapping[str, Any]
    gates: Mapping[str, Gate]
    reserved_authority: frozenset[str]
    body: Mapping[str, Any]
    desired_schema: Mapping[str, "DesiredField"] = None  # type: ignore[assignment]

    def resolve_desired(self, given: Mapping[str, str]) -> dict[str, str]:
        """ORG-080 §1: desired state enters only through the declared, typed schema.

        A key the schema does not declare is a plan error; an enum is enforced;
        declared defaults fill the rest.
        """

        schema = self.desired_schema or {}
        unknown = sorted(set(given) - set(schema))
        if unknown:
            raise ContractError(f"{self.id}: desired keys {unknown} are not declared in desired_schema")
        out: dict[str, str] = {}
        for key, spec in sorted(schema.items()):
            value = given.get(key, spec.default)
            if value is None:
                continue
            if spec.enum is not None and value not in spec.enum:
                raise ContractError(f"{self.id}: desired.{key}={value!r} is not one of {list(spec.enum)}")
            out[key] = value
        return out

    @property
    def broker_runtime(self) -> frozenset[str]:
        return self.privilege.get("broker_runtime", frozenset())

    def declares_leg(self, leg: str) -> bool:
        return leg in self.legs


def planner_path_for(module_path: Path) -> Path:
    return module_path.with_suffix(".py")


def handlers_path_for(module_path: Path) -> Path:
    """The component's executor-side registration module (``<id>_handlers.py``)."""

    return module_path.with_name(module_path.stem + "_handlers.py")


def contained_module_path(repo_root: Path, module: str) -> Path:
    """Resolve a registry ``contract.module`` strictly inside ``repo_root``.

    Refuses absolute paths, any ``..`` component, and anything (including a
    symlink, or a planner/handler sibling) that resolves outside the checkout,
    so a registry row can never point the engine at arbitrary host code.
    """

    if not isinstance(module, str) or not module:
        raise ContractError("contract.module must be a non-empty relative path")
    rel = Path(module)
    if rel.is_absolute() or module.startswith("/"):
        raise ContractError(f"contract.module {module!r} is absolute; it must be relative to the checkout")
    if ".." in rel.parts:
        raise ContractError(f"contract.module {module!r} contains '..'")
    root = Path(repo_root).resolve()
    path = root / rel
    for candidate in (path, planner_path_for(path), handlers_path_for(path)):
        resolved = candidate.resolve()
        if resolved != root and root not in resolved.parents:
            raise ContractError(f"contract.module {module!r} resolves outside the checkout ({resolved})")
    return path


def read_component(module_path: Path) -> Mapping[str, Any]:
    try:
        document = load_file(module_path)
    except RegistryLoadError as exc:
        raise ContractError(str(exc)) from None
    if not isinstance(document, dict) or set(document) != {"component"} or not isinstance(document["component"], dict):
        raise ContractError(f"{module_path}: top level must be exactly one `component:` mapping")
    return document["component"]


# ---------------------------------------------------------------------------
# Static helpers (no component code runs)
# ---------------------------------------------------------------------------


def invariant_binding_errors(body: Mapping[str, Any]) -> list[str]:
    """ORG-081 D-11(b) / ORG-080 §6 touch-point 2: one invariant list, bound on both surfaces."""

    legs = body.get("legs") if isinstance(body.get("legs"), dict) else {}
    verify = legs.get("verify")
    emitter = body.get("emitter")
    list_id, list_digest = body.get("invariant_list_id"), body.get("invariant_list_digest")
    if verify is None and emitter is None and list_id is None and list_digest is None:
        return []
    errors: list[str] = []
    if not (isinstance(list_id, str) and list_id and isinstance(list_digest, str) and _DIGEST_RE.match(list_digest)):
        return ["invariant_list_id and invariant_list_digest (sha256:<64 hex>) are both required once verify or emitter is declared"]
    expected = f"{list_id}@{list_digest}"
    for label, block in (("legs.verify", verify), ("emitter", emitter)):
        if block is None:
            continue
        ref = block.get("invariant_list_ref") if isinstance(block, dict) else None
        if ref != expected:
            errors.append(f"{label}.invariant_list_ref {ref!r} drifts from {expected!r} (ORG-081 D-11(b))")
    return errors


def invariant_digest_errors(body: Mapping[str, Any], module_path: Path) -> list[str]:
    """T-5135 S4: recompute the declared list's digest and id from its file (ORG-080 §2)."""

    declared = body.get("invariant_list_digest")
    if declared is None:
        return []
    try:
        digest, list_id = compute_list_digest(module_path)
    except InvariantDigestError as exc:
        return [str(exc)]
    errors = []
    if digest != declared:
        errors.append(f"invariant_list_digest {declared!r} != recomputed {digest} (ORG-080 §2)")
    if list_id != body.get("invariant_list_id"):
        errors.append(f"invariant_list_id {body.get('invariant_list_id')!r} != the list file's id {list_id!r}")
    return errors


def check_handlers_pin(body: Mapping[str, Any], module_path: Path) -> HD.HandlersDigest | None:
    """T-5477: the contract's ``handlers_digest`` must pin the handlers module that exists.

    Returns the verified digest (with the exact entry bytes it covered), or None
    when the component ships no handlers module. Every refusal is a ContractError.
    """

    path = handlers_path_for(Path(module_path))
    declared = body.get("handlers_digest")
    if not path.exists() and not path.is_symlink():
        if declared is not None:
            raise ContractError(f"handlers_digest is declared but {path.name} does not exist (T-5477)")
        return None
    if declared is None:
        raise ContractError(f"{path.name} exists but the contract pins no handlers_digest (T-5477)")
    if not (isinstance(declared, str) and _DIGEST_RE.match(declared)):
        raise ContractError(f"handlers_digest {declared!r} is not sha256:<64 hex>")
    try:
        pinned = HD.compute(path)
    except HD.HandlersDigestError as exc:
        raise ContractError(f"handlers module unpinnable: {exc}") from None
    if pinned.digest != declared:
        raise ContractError(
            f"handlers_digest {declared!r} != recomputed {pinned.digest} over {', '.join(pinned.files)} (T-5477); "
            "re-pin with `python3 -m tools.germinate registry repin <id>`"
        )
    return pinned


def handlers_pin_errors(body: Mapping[str, Any], module_path: Path) -> list[str]:
    try:
        check_handlers_pin(body, module_path)
    except ContractError as exc:
        return [str(exc)]
    return []


def _planner_functions(planner_path: Path) -> set[str] | None:
    try:
        tree = ast.parse(planner_path.read_text(encoding="utf-8"), filename=str(planner_path))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None
    return {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


def _leg_method_ok(leg: str, value: Any) -> bool:
    method = f"plan_{leg}"
    return isinstance(value, str) and (value == method or value.startswith(method + "("))


def static_predicates(body: Mapping[str, Any], planner_path: Path) -> dict[str, bool]:
    """Presence of the statically checkable ORG-081 D-8 non-evidence predicates.

    ``crash_contract`` is not statically checkable (it is a property of every
    op a planner returns); the engine enforces it on every op at plan time.
    """

    functions = _planner_functions(planner_path) if planner_path.is_file() else None
    legs = body.get("legs") if isinstance(body.get("legs"), dict) else {}
    present: dict[str, bool] = {}
    for leg in PLAN_LEGS:
        present[f"plan_{leg}"] = (
            _leg_method_ok(leg, legs.get(leg)) and functions is not None and f"plan_{leg}" in functions
        )
    verify = legs.get("verify")
    present["verify_contract"] = isinstance(verify, dict) and isinstance(verify.get("invariant_list_ref"), str)
    receipts = body.get("receipts")
    present["receipt_schema"] = isinstance(receipts, dict) and receipts.get("schema") == RECEIPT_SCHEMA
    present["sandbox"] = body.get("planner_kind") == "python" and functions is not None
    return present


# ---------------------------------------------------------------------------
# Plan-time load
# ---------------------------------------------------------------------------


def _str_list(where: str, value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ContractError(f"{where} must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise ContractError(f"{where} has duplicate entries")
    return tuple(value)


def _privilege(where: str, value: Any, op_registry: "OpRegistry") -> dict[str, frozenset[str]]:
    if not isinstance(value, dict) or not set(value) <= set(PRIVILEGE_LISTS):
        raise ContractError(f"{where}: privilege must be a mapping with keys from {PRIVILEGE_LISTS}")
    lists = {key: frozenset(_str_list(f"{where}.privilege.{key}", value[key])) for key in value}
    for key, kinds in lists.items():
        for kind in sorted(kinds):
            if op_registry.get(kind) is None:
                raise ContractError(f"{where}.privilege.{key}: {kind!r} is not an op-registry kind")
    root = lists.get("bootstrap_root", frozenset())
    seat = lists.get("seat", frozenset())
    runtime = lists.get("broker_runtime", frozenset())
    # PROVISIONAL reading (T-5074 bubble 5): bootstrap_root is disjoint from
    # seat ∪ broker_runtime; seat and broker_runtime may overlap (the ORG-080
    # §2 example does).
    if root & (seat | runtime):
        raise ContractError(f"{where}: bootstrap_root overlaps seat/broker_runtime: {sorted(root & (seat | runtime))}")
    substrate = op_registry.substrate_kinds
    if runtime & substrate:
        # ORG-082 §2a(c): a substrate family in broker_runtime is a component-load refusal.
        raise ContractError(f"{where}: broker_runtime names substrate families {sorted(runtime & substrate)} (ORG-082 §1)")
    # RE-EVALUATE POINT (T-5074 bubble 13): broker_runtime must be a subset of
    # the broker envelope's admitted kinds ("subset of the runtime enum",
    # ORG-080 §2). This may over-constrain later components; revisit when a
    # component legitimately needs a runtime kind the envelope does not admit.
    outside = runtime - op_registry.broker_admitted_kinds
    if outside:
        raise ContractError(f"{where}: broker_runtime kinds outside the broker envelope: {sorted(outside)}")
    misplaced = (seat & substrate)
    if misplaced:
        raise ContractError(f"{where}: substrate kinds must be bootstrap_root only: {sorted(misplaced)}")
    return lists


def _gates(where: str, value: Any) -> dict[str, Gate]:
    if value is None:
        return {}
    if not isinstance(value, list):
        raise ContractError(f"{where}: gates must be a list")
    gates: dict[str, Gate] = {}
    for index, raw in enumerate(value):
        gwhere = f"{where}.gates[{index}]"
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
            raise ContractError(f"{gwhere}: a gate needs an id")
        approval = raw.get("approval")
        if not isinstance(approval, dict) or approval.get("class") not in GATE_CLASSES:
            raise ContractError(f"{gwhere}: approval.class must be one of {sorted(GATE_CLASSES)}")
        if approval["class"] == "independent-agent" and not isinstance(approval.get("grant_source"), str):
            raise ContractError(f"{gwhere}: an independent-agent gate names its grant_source")
        if not isinstance(raw.get("irreversible", False), bool):
            raise ContractError(f"{gwhere}: irreversible must be a boolean")
        if raw["id"] in gates:
            raise ContractError(f"{gwhere}: duplicate gate id {raw['id']!r}")
        gates[raw["id"]] = Gate(raw["id"], approval["class"], bool(raw.get("irreversible", False)))
    return gates


def load_contract(module_path: Path, op_registry: "OpRegistry") -> LoadedContract:
    """Load and validate one contract module; every refusal is a ContractError."""

    module_path = Path(module_path)
    body = read_component(module_path)
    where = f"contract {module_path.name}"
    missing = REQUIRED - set(body)
    if "planner_kind" in missing or body.get("planner_kind") != "python":
        raise ContractError(
            PLANNER_KIND_REFUSAL.format(id=body.get("id", "?"), value=body.get("planner_kind", "<omitted>"))
        )
    if missing:
        raise ContractError(f"{where}: missing required fields {sorted(missing)}")
    unknown = set(body) - REQUIRED - OPTIONAL
    if unknown:
        raise ContractError(f"{where}: fields not in the contract schema: {sorted(unknown)}")
    cid = body["id"]
    if not (isinstance(cid, str) and _ID_RE.match(cid)):
        raise ContractError(f"{where}: id {cid!r} is not a lower-kebab-case id")
    if not (isinstance(body["steward"], str) and body["steward"].strip()):
        raise ContractError(f"{where}: steward must be a non-empty string")
    if not (isinstance(body["version"], int) and not isinstance(body["version"], bool) and body["version"] >= 1):
        raise ContractError(f"{where}: version must be an integer >= 1")
    try:
        digest = compute_contract_digest(module_path)
    except ContractDigestError as exc:
        raise ContractError(str(exc)) from None
    if body["contract_digest"] != digest:
        raise ContractError(f"{where}: written contract_digest {body['contract_digest']!r} != recomputed {digest} (ORG-081 D-9)")
    depends_on = _str_list(f"{where}.depends_on", body["depends_on"]) if body["depends_on"] else ()
    if cid in depends_on:
        raise ContractError(f"{where}: a component cannot depend on itself")
    scopes = frozenset(_str_list(f"{where}.scopes", body["scopes"]))
    if not scopes or not scopes <= SCOPES:
        raise ContractError(f"{where}: scopes must be a non-empty subset of {sorted(SCOPES)}")
    privilege = _privilege(where, body["privilege"], op_registry)
    legs = body["legs"]
    if not isinstance(legs, dict) or not legs or not set(legs) <= LEG_KEYS:
        raise ContractError(f"{where}: legs must be a non-empty mapping over {sorted(LEG_KEYS)}")
    planner_path = planner_path_for(module_path)
    functions = _planner_functions(planner_path) if planner_path.is_file() else None
    for leg in PLAN_LEGS:
        if leg not in legs:
            continue
        if not _leg_method_ok(leg, legs[leg]):
            raise ContractError(f"{where}: legs.{leg} must name plan_{leg}")
        if functions is None or f"plan_{leg}" not in functions:
            raise ContractError(f"{where}: planner module {planner_path.name} does not define plan_{leg}")
    binding = (
        invariant_binding_errors(body) or invariant_digest_errors(body, module_path) or handlers_pin_errors(body, module_path)
    )
    if binding:
        raise ContractError(f"{where}: " + "; ".join(binding))
    receipts = body["receipts"]
    if not (isinstance(receipts, dict) and receipts.get("schema") == RECEIPT_SCHEMA):
        raise ContractError(f"{where}: receipts.schema must be {RECEIPT_SCHEMA}")
    if body.get("interactions"):
        raise ContractError(
            f"{where}: declares necessary external interactions; parking/resume is deferred past T-5074, so the plan is refused"
        )
    gates = _gates(where, body.get("gates"))
    reserved_raw = body.get("reserved_authority") or []
    if not isinstance(reserved_raw, list) or not all(isinstance(r, dict) and isinstance(r.get("id"), str) for r in reserved_raw):
        raise ContractError(f"{where}: reserved_authority must be a list of {{id}}")
    reserved = frozenset(r["id"] for r in reserved_raw)
    if reserved & set(gates):
        raise ContractError(f"{where}: an id cannot be both an operation gate and reserved authority")
    return LoadedContract(
        path=module_path,
        planner_path=planner_path,
        digest=digest,
        id=cid,
        steward=body["steward"],
        depends_on=depends_on,
        scopes=scopes,
        privilege=privilege,
        legs=legs,
        gates=gates,
        reserved_authority=reserved,
        desired_schema=_desired_schema(where, body.get("desired_schema")),
        body=body,
    )
