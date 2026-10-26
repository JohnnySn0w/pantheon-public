"""Component registration: the frozen surface a component adopts the engine through.

For each registry row with a pinned internal contract
``tools/germinate/components/<id>.yaml``, the engine looks for the sibling
executor-side module ``<id>_handlers.py`` (path-contained like the contract)
and reads two optional names from it:

* ``HANDLERS`` -- a sequence of ``backend.EffectHandler`` objects, one per
  group of op kinds the component brings;
* ``VERIFY`` -- optional ``callable(list_doc, observed, scope) -> {"results": [...], "state": str | None}``
  that evaluates the component's invariant list over the backend's observation
  (T-5135 S1); each result is ``{"id", "status": pass|fail|vacuous|alarm, "detail"}``;
* ``OP_KINDS`` -- ``{kind: (capability_class, privilege)}`` for kinds that are
  neither broker-envelope nor ORG-082 substrate kinds. It must be a literal
  (read with ``ast.literal_eval``, never by importing), so the validator can
  check it in CI without running component code. Component kinds are
  seat-privilege only and may not use a reserved capability class
  (``op_registry.component_kind_errors``, ORG-080 D-AC).

``OP_KINDS`` from every registered component is merged into the typed-op
registry at load, so the registry blob and its ORG-082 digest include them
(deterministically: the blob is sorted by kind). A kind declared twice, by two
components or against an existing kind, refuses. Nothing here needs an engine
edit per component.

The handlers module is executor code (it actuates), reviewed like the backend;
it is not a planner and runs outside the planner guard.

Import trust (T-5477): before any handlers module runs, ``discover`` checks the
chain registry row ``contract.digest`` -> contract module -> its
``handlers_digest`` (``engine/handlers_digest.py``, over the entry file and its
component-local helpers) and refuses (``ComponentLoadError``) on drift, on a
missing pin, or on a pin with no handlers file. The entry module then runs from
the exact bytes that were digested. Helpers are verified at discover time ONLY:
they are imported later by the normal import system (from disk, or a matching
``.pyc``), so a post-discover edit, ``.pyc`` shadowing, or a ``--repo-root``
checkout other than the one on ``sys.path`` is a residual for helpers, tracked
by T-5506. The validator path (``import_handlers=False``) runs no component
code; the registry validator checks the same pin through
``contract.handlers_pin_errors``.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from tools.germinate.engine.contract import (
    ContractError,
    check_handlers_pin,
    contained_module_path,
    handlers_path_for,
    read_component,
)
from tools.germinate.op_registry import ENGINE_KINDS, PRIVILEGES, component_kind_errors
from tools.germinate.registry import schema as RS
from tools.germinate.registry.contract_digest import ContractDigestError, compute_contract_digest
from tools.germinate.registry.load import RegistryLoadError, load_file

HANDLERS_ATTR = "HANDLERS"
KINDS_ATTR = "OP_KINDS"
VERIFY_ATTR = "VERIFY"


class ComponentLoadError(RuntimeError):
    """A component's registration module is refused."""


@dataclass(frozen=True)
class ComponentRegistration:
    component: str
    path: Path
    handlers: tuple[Any, ...]
    op_kinds: Mapping[str, tuple[str, str]]
    verify: Any = None


def _import(path: Path, component: str, source: bytes) -> Any:
    """Run the handlers module from ``source``: the exact bytes the pin verified (T-5477).

    The ENTRY module is never re-read from disk, so a swap between the digest
    check and the import cannot substitute other bytes for it. This does not
    extend to helpers: the entry's own ``import`` statements load them through
    the normal import system after verification (disk or a matching ``.pyc``,
    from the ``sys.path`` checkout), a residual tracked by T-5506.
    """

    tag = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(f"_germinate_handlers_{tag}", path)
    if spec is None or spec.loader is None:
        raise ComponentLoadError(f"{component}: cannot load {path.name}")
    module = importlib.util.module_from_spec(spec)
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        exec(compile(source, str(path), "exec", dont_inherit=True), module.__dict__)  # noqa: S102 -- pinned executor code
    except Exception as exc:  # noqa: BLE001 -- a broken registration module is a typed load refusal
        raise ComponentLoadError(f"{component}: {path.name} failed to import: {type(exc).__name__}: {exc}") from None
    finally:
        sys.dont_write_bytecode = saved
    return module


def _verified_handlers_source(cid: str, row: Mapping[str, Any], module_path: Path) -> bytes:
    """T-5477 chain, fail-closed, before any component code runs.

    registry row ``contract.digest`` == recomputed contract digest (so the
    contract's ``handlers_digest`` line cannot be edited alone), then the
    contract's ``handlers_digest`` == the recomputed handlers digest.
    """

    try:
        digest = compute_contract_digest(module_path)
    except ContractDigestError as exc:
        raise ComponentLoadError(f"{cid}: contract module unusable: {exc}") from None
    if digest != row["contract"].get("digest"):
        raise ComponentLoadError(f"{cid}: registry digest {row['contract'].get('digest')} != recomputed {digest}; handlers not imported")
    try:
        pinned = check_handlers_pin(read_component(module_path), module_path)
    except ContractError as exc:
        raise ComponentLoadError(f"{cid}: {exc}; handlers not imported") from None
    if pinned is None:  # pragma: no cover -- the caller only reaches here when the file exists
        raise ComponentLoadError(f"{cid}: handlers module vanished during load")
    return pinned.entry_source


def _refuse_orphan_pin(cid: str, module_path: Path) -> None:
    """No regular handlers file: refuse a declared ``handlers_digest`` (or a non-file in the
    handlers path), as ``load_contract`` and the validator do. Nothing is imported either way.

    A contract module that is absent or unreadable is left to ``load_contract``,
    which refuses it at plan; discover never imported anything for such a row.
    """

    if not module_path.is_file():
        return
    try:
        body = read_component(module_path)
    except ContractError:
        return
    try:
        check_handlers_pin(body, module_path)
    except ContractError as exc:
        raise ComponentLoadError(f"{cid}: {exc}") from None


def read_op_kinds(component: str, path: Path, source: bytes | None = None) -> Any:
    """The module-level ``OP_KINDS = <literal>`` of a handlers module, without importing it."""

    try:
        text = path.read_text(encoding="utf-8") if source is None else source.decode("utf-8")
        tree = ast.parse(text, filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        raise ComponentLoadError(f"{component}: {path.name} unreadable: {exc}") from None
    found = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == KINDS_ATTR for t in node.targets)
    ]
    if not found:
        return None
    if len(found) > 1:
        raise ComponentLoadError(f"{component}: {path.name} assigns OP_KINDS more than once")
    try:
        return ast.literal_eval(found[0].value)
    except ValueError:
        raise ComponentLoadError(f"{component}: OP_KINDS in {path.name} must be a literal mapping") from None


def _op_kinds(component: str, raw: Any) -> dict[str, tuple[str, str]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ComponentLoadError(f"{component}: OP_KINDS must be a mapping kind -> (capability_class, privilege)")
    out = {}
    for kind, value in raw.items():
        if not (isinstance(kind, str) and kind and isinstance(value, (tuple, list)) and len(value) == 2):
            raise ComponentLoadError(f"{component}: OP_KINDS[{kind!r}] must be (capability_class, privilege)")
        klass, privilege = value
        if not (isinstance(klass, str) and klass) or privilege not in PRIVILEGES:
            raise ComponentLoadError(f"{component}: OP_KINDS[{kind!r}] has an invalid class or privilege")
        problems = component_kind_errors(kind, klass, privilege)
        if problems:
            raise ComponentLoadError(f"{component}: OP_KINDS[{kind!r}] refused: " + "; ".join(problems))
        out[kind] = (klass, privilege)
    return out


def discover(repo_root: Path, registry_path: Path, *, import_handlers: bool = True) -> list[ComponentRegistration]:
    """Every registered component's registration module, in registry-id order.

    ``import_handlers=False`` (the validator) reads only the OP_KINDS literal
    and never executes component code.
    """

    try:
        doc = load_file(registry_path)
    except RegistryLoadError as exc:
        raise ComponentLoadError(str(exc)) from None
    return discover_doc(repo_root, doc, import_handlers=import_handlers)


def discover_doc(repo_root: Path, doc: Any, *, import_handlers: bool = True) -> list[ComponentRegistration]:
    regs = []
    rows = [r for r in (doc.get("systems") or []) if isinstance(r, dict)] if isinstance(doc, dict) else []
    for row in sorted(rows, key=lambda r: str(r.get("id"))):
        contract = row.get("contract")
        if row.get("kind") != RS.KIND_COMPONENT or not isinstance(contract, dict):
            continue
        if contract.get("repo") != RS.INTERNAL_REPO or not RS.DIGEST_RE.match(str(contract.get("digest"))):
            continue
        try:
            module_path = contained_module_path(repo_root, contract.get("module"))
        except ContractError as exc:
            raise ComponentLoadError(f"{row.get('id')}: {exc}") from None
        path = handlers_path_for(module_path)
        cid = str(row.get("id"))
        if not path.is_file():
            if import_handlers:
                _refuse_orphan_pin(cid, module_path)  # a pin with no handlers file refuses here too (T-5477)
            continue
        source = _verified_handlers_source(cid, row, module_path) if import_handlers else None
        kinds = _op_kinds(cid, read_op_kinds(cid, path, source))
        handlers: tuple[Any, ...] = ()
        verify = None
        if source is not None:
            module = _import(path, cid, source)
            handlers = tuple(getattr(module, HANDLERS_ATTR, ()) or ())
            verify = getattr(module, VERIFY_ATTR, None)
            if verify is not None and not callable(verify):
                raise ComponentLoadError(f"{cid}: {path.name} VERIFY must be a callable (list_doc, observed, scope) -> result")
        regs.append(ComponentRegistration(cid, path, handlers, kinds, verify))
    return regs


def load_repo_op_registry(repo_root: Path, registry_path: Path | None = None):
    """THE typed-op registry a checkout defines (T-2918, ORG-082 D-D / gate 32a).

    The single loader both invocation contexts use: ``Engine.from_checkout``
    (the bootstrap in-process root path) and the broker daemon's
    ``load_daemon_runtime``. It reads every registered component's ``OP_KINDS``
    LITERAL (``import_handlers=False``: no component code is imported or run),
    merges them, and loads the registry, so both contexts digest the same blob.
    Raises ``ComponentLoadError`` / ``OpRegistryError``.
    """

    from tools.germinate.op_registry import load_op_registry
    from tools.germinate.registry.validate import REGISTRY_REL

    registry_path = Path(registry_path) if registry_path else Path(repo_root) / REGISTRY_REL
    return load_op_registry(merged_kinds(discover(Path(repo_root), registry_path, import_handlers=False)))


def merged_kinds(regs: list[ComponentRegistration]) -> dict[str, tuple[str, str]]:
    merged: dict[str, tuple[str, str]] = dict(ENGINE_KINDS)
    owner: dict[str, str] = {kind: "ENGINE_KINDS" for kind in merged}
    for reg in regs:
        for kind, value in reg.op_kinds.items():
            if kind in merged:
                raise ComponentLoadError(f"op kind {kind!r} is declared by both {owner[kind]} and {reg.component}")
            merged[kind] = value
            owner[kind] = reg.component
    return merged
