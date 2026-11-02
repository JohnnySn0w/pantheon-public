"""T-5516: closed import sets for the ORG-052 privileged services.

Intent (T-2652 / ORG-052 section 1 and 3): the broker, the approval service and
the per-seat executor carry only the code they need. Before T-5516 every one of
them imported ``tools.persona_installer`` through ``tools.host_control.contracts``
and so loaded the agent-facing Codex Telegram bridge, the mailbox client and the
harness adapter. The pure contract types now live in
``tools.persona_installer_contracts`` and the executor-shared helpers in
``tools.persona_installer_activation``; ``tools.persona_installer`` re-exports both.

Pinned here:

* each service entry module, imported in a fresh ``python3 -I`` interpreter,
  loads none of ``DENIED_PREFIXES`` and no ``tools.*`` module outside its closed
  set (``tools.host_control.*`` plus the per-service extras);
* T-5533: the broker is an ordinary closed service (its planner reads a
  root-registered plan instead of running ``PersonaInstaller``), and a RUNTIME
  probe EXECUTES ``HostPlanService.plan()`` against a fixture registry and holds
  every ``tools.*`` module that loads (the import-only probe missed 9 lazily
  loaded modules) to the same allowlist;
* source level (AST, function-level imports included): no file in
  tools/host_control imports ``tools.persona_installer`` or a submodule of it
  (``tools.persona_installer_contracts`` stays allowed); no host_control module
  imports a denied module at module level, and the function-level ones are
  exactly ``SANCTIONED_LAZY_DENIED_IMPORTS`` (empty since T-5572 moved the
  executor's receipt validator into ``tools.runtime_invocation_receipt``); the
  new leaf modules import only the standard library and their allowed ``tools``
  modules;
* T-5572 (the T-5516 review's MED gap), widened by T-5533: no dynamic import in
  ANY file a service's static closure reaches, nor anywhere in tools/host_control
  (``importlib``/``__import__``/spec loading/``exec``/``eval``/``compile``/
  ``builtins``, an aliased reference to exec/eval/compile, a subscript or
  ``getattr`` naming a dynamic surface, any ``sys.modules`` access) except the
  package's PEP 562 lazy loader, whose target table is pinned, and the named
  ``SANCTIONED_DYNAMIC_IMPORTS``. Each shape is classified known-allowed (that
  table) or unknown-denied, and a benign fixture proves each is flagged; a
  STATIC closure walk per service follows
  function-level imports (and the lazy loader's table, including attribute
  access through an alias of ``tools.host_control``) transitively, holding every
  reached ``tools.*`` module to the same allowlist as the import-time probe, plus
  ``SANCTIONED_FUNCTION_LEVEL_IMPORTS`` (empty);
* the executor's unit-fire budget path and receipt-validation path do not load
  runtime_controller, and the transcribed budget values equal runtime_controller's;
* type identity and isinstance across both import paths; runtime_controller
  re-exports the receipt validator's names as the same objects, never redefines
  them, and nothing patches a moved name on runtime_controller (the moved code
  resolves its helpers in its own module, so such a patch would not reach it);
* the framework.source artifact digest covers all three installer code files.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from tools import persona_installer as PI
from tools import persona_installer_activation as ACT
from tools import persona_installer_contracts as CON

REPO_ROOT = Path(__file__).resolve().parent.parent
HOST_CONTROL = REPO_ROOT / "tools" / "host_control"

#: Agent-facing / harness-runtime modules no privileged service may load: the
#: measured pre-T-5516 closure of tools.persona_installer plus the modules it
#: imports lazily (mailbox_onboard, keep_going, runtime_controller, agent_role_map,
#: persona_bringup).
DENIED_PREFIXES = (
    "tools.persona_installer",  # exact module; the two closed modules are allowed below
    "tools.persona_installer_apply",
    "tools.persona_installer_verify_seat",
    "tools.codex_telegram_bridge",
    "tools.codex_telegram_actions",
    "tools.mailbox",
    "tools.mailbox_writable_roots",
    "tools.mailbox_onboard",
    "tools.harness_adapter",
    "tools.harness_permission_posture",
    "tools.persona_brief",
    "tools.persona_apply_path",
    "tools.runtime_controller",
    "tools.agent_role_map",
    "tools.keep_going",
    "tools.persona_bringup",
    "tools.bypass_consent",
    "tools.claude_quota_probe",
    "tools.claude_statusline_capture",
    "tools._lease",
)

CONTRACTS = "tools.persona_installer_contracts"
ACTIVATION = "tools.persona_installer_activation"
RECEIPT = "tools.runtime_invocation_receipt"

#: The broker also loads the germinate op-enum registry and WAL (gate 36 / op
#: registry), which are not agent-facing.
BROKER_ENTRY = "tools.host_control.dbus_service"
#: T-2920 U2: the production daemon entrypoints join their service's closure
#: (entry modules only; no allowlist widens).
BROKER_MAIN = "tools.host_control.__main__"
APPROVAL_MAIN = "tools.host_control.approval.__main__"
BROKER_ALLOWED = frozenset({"tools", CONTRACTS, "tools.germinate"})
BROKER_ALLOWED_PREFIXES = ("tools.germinate.",)

#: Per service: entry modules, the non-host_control ``tools.*`` modules allowed,
#: and allowed module-name prefixes. T-5533 folded the broker in (no exception).
SERVICES = {
    "approval": (
        ("tools.host_control.approval.dbus_service", "tools.host_control.approval.service", APPROVAL_MAIN),
        frozenset({"tools", CONTRACTS}),
        (),
    ),
    "executor": (
        ("tools.host_control.executor.__main__", "tools.host_control.executor.dbus_service"),
        frozenset({"tools", CONTRACTS, ACTIVATION, RECEIPT, "tools.user_systemd"}),
        (),
    ),
    "broker": ((BROKER_ENTRY, BROKER_MAIN), BROKER_ALLOWED, BROKER_ALLOWED_PREFIXES),
}
#: Function-level imports of a denied module inside tools/host_control, by file.
#: Empty: T-5516's one entry (executor engine -> runtime_controller, for the
#: invocation-receipt validator) went away when T-5572 moved the validator into
#: the stdlib-only leaf tools.runtime_invocation_receipt.
SANCTIONED_LAZY_DENIED_IMPORTS: frozenset[tuple[str, str]] = frozenset()

#: T-5572: (importing file, imported module) edges in a service's STATIC closure
#: (function-level imports included) that leave the service's allowlist. Empty.
SANCTIONED_FUNCTION_LEVEL_IMPORTS: frozenset[tuple[str, str]] = frozenset()

#: T-5572: tools/host_control/__init__.py's PEP 562 lazy loader -- the ONE sanctioned
#: dynamic import in the package -- and its exact target table.
LAZY_LOADER_FILE = "tools/host_control/__init__.py"
LAZY_LOADER_EXPORTS = {
    "BrokerJournal": "tools.host_control.journal",
    "CapabilitiesResult": "tools.host_control.contracts",
    "CapabilityGrant": "tools.host_control.contracts",
    "HostPlanService": "tools.host_control.planner",
    "HostPolicy": "tools.host_control.policy",
    "PlanEnvelope": "tools.host_control.contracts",
    "PlanResult": "tools.host_control.contracts",
    "SeatRegistry": "tools.host_control.registry",
    "SubjectContext": "tools.host_control.contracts",
}

#: Dynamic-import classification (T-5572). Any import of these modules, any
#: reference to these names, and any bare call of these builtins is a dynamic
#: import surface the static closure cannot see.
#: T-5533 review r1: operator (``attrgetter("modules")(sys)``), pickle and marshal
#: (code/objects from bytes) reach the same surfaces; no reached file imports them.
DYNAMIC_IMPORT_MODULES = frozenset(
    {"importlib", "runpy", "imp", "pkgutil", "zipimport", "builtins", "operator", "pickle", "marshal"}
)
DYNAMIC_IMPORT_NAMES = frozenset(
    {
        "__import__",
        "__builtins__",
        "import_module",
        "spec_from_file_location",
        "spec_from_loader",
        "module_from_spec",
        "exec_module",
        "load_module",
        "find_spec",
        "SourceFileLoader",
    }
)
DYNAMIC_EVAL_BUILTINS = frozenset({"exec", "eval", "compile"})
GERMINATE_COMPONENTS = "tools/germinate/engine/components.py"
#: (file, enclosing function or "<module>", kind, name) -> count. Exactly the
#: lazy loader (its module-level ``from importlib import import_module`` and the
#: single ``import_module(module)`` call inside ``__getattr__``) plus the named
#: germinate entries below.
SANCTIONED_DYNAMIC_IMPORTS = {
    (LAZY_LOADER_FILE, "<module>", "module", "importlib"): 1,
    (LAZY_LOADER_FILE, "<module>", "name", "import_module"): 1,
    (LAZY_LOADER_FILE, "__getattr__", "name", "import_module"): 1,
    # T-5533 (finding 3 widened the scan to every closure file): the germinate
    # component loader runs a digest-pinned handlers module from the verified
    # bytes (T-5477). The broker reaches the file only through
    # registry.validate._merged_op_registry, which calls discover_doc with
    # import_handlers=False, so _import never runs there; the runtime probe below
    # proves no _germinate_handlers_* module loads when plan() executes.
    (GERMINATE_COMPONENTS, "<module>", "module", "importlib"): 1,
    (GERMINATE_COMPONENTS, "_import", "name", "spec_from_file_location"): 1,
    (GERMINATE_COMPONENTS, "_import", "name", "module_from_spec"): 1,
    (GERMINATE_COMPONENTS, "_import", "builtin", "exec"): 1,
    (GERMINATE_COMPONENTS, "_import", "builtin", "compile"): 1,
}

#: T-5572: the receipt-validator closure moved verbatim out of runtime_controller.
RECEIPT_MOVED_NAMES = (
    "DEFAULT_STATE_ROOT",
    "INITIAL_VIABILITY_INVOCATION_MAX_BYTES",
    "INITIAL_VIABILITY_INVOCATION_SCHEMA",
    "SYSTEMD_INVOCATION_ID_RE",
    "_canonical_bytes",
    "_initial_viability_invocation_dir",
    "_initial_viability_invocation_path",
    "_persona_state",
    "_read_invocation_payload_fd",
    "_read_invocation_payload_no_follow_at",
    "_secure_directory_fd",
    "_state_root",
    "_validate_initial_viability_invocation_payload",
    "_validate_invocation_payload",
    "_validate_systemd_invocation_id",
    "validate_initial_viability_invocation_receipt",
)
#: Moved names that may still be patched ON runtime_controller: persona_installer
#: resolves the validator as a runtime_controller attribute at call time, so a patch
#: there still takes effect for that caller (tools/test_persona_installer_converge.py).
RC_PATCHABLE_MOVED_NAMES = frozenset({"validate_initial_viability_invocation_receipt"})

_PROBE = """
import json, sys, types
sys.path.insert(0, {root!r})
import importlib
for name in {modules!r}:
    importlib.import_module(name)
{extra}
print(json.dumps(sorted(sys.modules)))
"""


def loaded_modules(modules, *, extra=""):
    probe = _PROBE.format(root=str(REPO_ROOT), modules=tuple(modules), extra=extra)
    proc = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr[-1500:])
    return json.loads(proc.stdout.strip().splitlines()[-1])


def denied(modules):
    out = []
    for name in modules:
        for prefix in DENIED_PREFIXES:
            if name == prefix or (prefix != "tools.persona_installer" and name.startswith(prefix + ".")):
                out.append(name)
    return sorted(set(out))


def outside(modules, allowed, prefixes=()):
    return sorted(
        name
        for name in modules
        if (name == "tools" or name.startswith("tools."))
        and name not in allowed
        and name != "tools.host_control"
        and not name.startswith("tools.host_control.")
        and not name.startswith(prefixes or ("\0",))
    )


def imported_names(path: Path):
    """Every imported module name in ``path``, function-level imports included.

    ``from tools import user_systemd`` yields ``tools.user_systemd``.
    """
    for lineno, name, _node in _imports(ast.parse(path.read_text(encoding="utf-8"))):
        yield lineno, name


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name, node
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                yield node.lineno, "." * node.level + (node.module or ""), node
            elif node.module == "tools":
                for alias in node.names:
                    yield node.lineno, f"tools.{alias.name}", node
            else:
                yield node.lineno, node.module or "", node


# ---------------------------------------------------------------------------
# T-5572: static closure walk (function-level imports included)
# ---------------------------------------------------------------------------


def _module_file(name: str, root: Path = REPO_ROOT) -> Path | None:
    relative = Path(*name.split("."))
    for candidate in (root / relative.with_suffix(".py"), root / relative / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _type_checking_node_ids(tree: ast.AST) -> set[int]:
    """Nodes under ``if TYPE_CHECKING:`` never execute, so they load nothing."""
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            if (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
                isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
            ):
                for statement in node.body:
                    skip.update(id(sub) for sub in ast.walk(statement))
    return skip


def lazy_loader_exports() -> dict[str, str]:
    tree = ast.parse((REPO_ROOT / LAZY_LOADER_FILE).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_EXPORTS" for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("tools/host_control/__init__.py has no _EXPORTS table")


def _host_control_aliases(tree: ast.AST) -> frozenset[str]:
    """Local names bound to the ``tools.host_control`` package object."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "tools.host_control" and alias.asname:
                    names.add(alias.asname)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module == "tools":
            for alias in node.names:
                if alias.name == "host_control":
                    names.add(alias.asname or alias.name)
    return frozenset(names)


def _is_host_control_ref(node: ast.AST, aliases: frozenset[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in aliases
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "host_control"
        and isinstance(node.value, ast.Name)
        and node.value.id == "tools"
    )


def module_imports(name: str, root: Path = REPO_ROOT):
    """Every module ``name``'s source loads when its code runs: module-level AND
    function-level imports (not TYPE_CHECKING blocks), parent packages included,
    ``from pkg import sub`` resolved to the submodule, and ``from
    tools.host_control import NAME`` resolved through the lazy loader's table.
    T-5533 (carried-in finding 1): attribute access ``hc.NAME`` through an alias
    of the package (``import tools.host_control as hc``, ``from tools import
    host_control [as hc]``, or the bare ``tools.host_control.NAME`` chain) is the
    same loader-target edge. Yields ``(lineno, imported_module)``."""
    path = _module_file(name, root)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _type_checking_node_ids(tree)
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    exports = lazy_loader_exports()
    package_aliases = _host_control_aliases(tree)
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Attribute) and node.attr in exports and _is_host_control_ref(
            node.value, package_aliases
        ):
            yield node.lineno, exports[node.attr]
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                for index in range(1, len(parts) + 1):
                    yield node.lineno, ".".join(parts[:index])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            parts = module.split(".")
            for index in range(1, len(parts) + 1):
                yield node.lineno, ".".join(parts[:index])
            for alias in node.names:
                submodule = f"{module}.{alias.name}"
                if _module_file(submodule, root) is not None:
                    yield node.lineno, submodule
                elif module == "tools.host_control" and alias.name in exports:
                    yield node.lineno, exports[alias.name]


def static_closure_violations(entries, allowed, prefixes=(), root: Path = REPO_ROOT):
    """Walk the service's tools.* closure from ``entries`` through every allowed
    module (tools.host_control.* plus ``allowed``/``prefixes``). Returns the set of
    ``(importing file, module)`` edges that leave the allowlist; the walk does not
    descend past them (each such edge is the regression to name)."""
    return static_closure(entries, allowed, prefixes, root)[0]


def static_closure(entries, allowed, prefixes=(), root: Path = REPO_ROOT):
    """``(violations, reached)``: ``reached`` is every source file the walk
    entered (T-5533 finding 3: the dynamic-import scan covers all of them)."""

    def is_allowed(module: str) -> bool:
        return (
            module in allowed
            or module == "tools.host_control"
            or module.startswith("tools.host_control.")
            or bool(prefixes) and module.startswith(prefixes)
        )

    violations: set[tuple[str, str]] = set()
    reached: set[Path] = set()
    seen: set[str] = set()
    stack = list(entries)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        path = _module_file(name, root)
        if path is None:
            continue
        reached.add(path)
        relative = path.relative_to(root).as_posix()
        for _lineno, module in module_imports(name, root):
            if not (module == "tools" or module.startswith("tools.")):
                continue
            if is_allowed(module):
                stack.append(module)
            else:
                violations.add((relative, module))
    return violations, reached


#: T-5533 (carried-in finding 2): string keys whose subscript or ``getattr``
#: reaches a dynamic-import surface.
DYNAMIC_STRING_KEYS = DYNAMIC_IMPORT_NAMES | DYNAMIC_EVAL_BUILTINS | {"__builtins__", "modules"}


def dynamic_import_findings(paths=None, root: Path = REPO_ROOT):
    """Classify every dynamic-import surface in ``paths`` (default: every Python
    file in tools/host_control). Returns ``{(file, enclosing function or
    "<module>", kind, name): count}``; a key is known-allowed only when it is in
    ``SANCTIONED_DYNAMIC_IMPORTS``, everything else is unknown-denied. Kinds:

    * ``module``: an import of a ``DYNAMIC_IMPORT_MODULES`` module;
    * ``name``: a ``DYNAMIC_IMPORT_NAMES`` name (reference, attribute or import);
    * ``builtin``: a direct call of exec/eval/compile;
    * ``reference``: any OTHER use of the exec/eval/compile names (``run =
      exec``, passing it as an argument, importing it under another name);
    * ``subscript``: ``x["__builtins__"]``, ``x["exec"]``, ``x["import_module"]``...;
    * ``getattr``: ``getattr``/``setattr``/``hasattr`` naming such a string, or
      whose object is ``sys.modules``/``__builtins__``;
    * ``sys.modules``: any access to ``sys.modules`` (through any alias of
      ``sys``) or ``from sys import modules``;
    * ``vars``: ``vars(<sys alias>)`` or ``vars(builtins/__builtins__)``, the
      namespace dict that reaches ``modules`` or the builtins by string.
    """
    from collections import Counter

    if paths is None:
        paths = sorted(HOST_CONTROL.rglob("*.py"))
    findings: Counter = Counter()
    for path in sorted(paths):
        relative = Path(path).relative_to(root).as_posix()
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        sys_aliases = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
            if alias.name == "sys"
        }
        call_funcs = {
            id(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        def is_dynamic_object(node: ast.AST) -> bool:
            if isinstance(node, ast.Name):
                return node.id == "__builtins__"
            return (
                isinstance(node, ast.Attribute)
                and node.attr == "modules"
                and isinstance(node.value, ast.Name)
                and node.value.id in sys_aliases
            )

        def visit(node: ast.AST, scope: str) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = node.name
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in DYNAMIC_IMPORT_MODULES:
                        findings[(relative, scope, "module", alias.name.split(".")[0])] += 1
                    if (alias.asname or "") in DYNAMIC_IMPORT_NAMES:
                        findings[(relative, scope, "name", alias.asname)] += 1
                    if (alias.asname or "") in DYNAMIC_EVAL_BUILTINS:
                        findings[(relative, scope, "reference", alias.asname)] += 1
            elif isinstance(node, ast.ImportFrom):
                top = (node.module or "").split(".")[0]
                if not node.level and top in DYNAMIC_IMPORT_MODULES:
                    findings[(relative, scope, "module", top)] += 1
                for alias in node.names:
                    if not node.level and node.module == "sys" and alias.name == "modules":
                        findings[(relative, scope, "sys.modules", "modules")] += 1
                    for bound in {alias.name, alias.asname} - {None}:
                        if bound in DYNAMIC_IMPORT_NAMES:
                            findings[(relative, scope, "name", bound)] += 1
                        elif bound in DYNAMIC_EVAL_BUILTINS:
                            findings[(relative, scope, "reference", bound)] += 1
            elif isinstance(node, ast.Name) and node.id in DYNAMIC_IMPORT_NAMES:
                findings[(relative, scope, "name", node.id)] += 1
            elif isinstance(node, ast.Name) and node.id in DYNAMIC_EVAL_BUILTINS:
                kind = "builtin" if id(node) in call_funcs else "reference"
                findings[(relative, scope, kind, node.id)] += 1
            elif isinstance(node, ast.Attribute) and node.attr in DYNAMIC_IMPORT_NAMES:
                findings[(relative, scope, "name", node.attr)] += 1
            elif is_dynamic_object(node) and isinstance(node, ast.Attribute):
                findings[(relative, scope, "sys.modules", "modules")] += 1
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and node.slice.value in DYNAMIC_STRING_KEYS
            ):
                findings[(relative, scope, "subscript", node.slice.value)] += 1
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "vars"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in (sys_aliases | {"builtins", "__builtins__"})
            ):
                findings[(relative, scope, "vars", node.args[0].id)] += 1
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("getattr", "setattr", "hasattr")
                and node.args
            ):
                named = (
                    node.args[1].value
                    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
                    else None
                )
                if named in DYNAMIC_STRING_KEYS:
                    findings[(relative, scope, "getattr", named)] += 1
                elif is_dynamic_object(node.args[0]):
                    findings[(relative, scope, "getattr", ast.unparse(node.args[0]))] += 1
            for child in ast.iter_child_nodes(node):
                visit(child, scope)

        visit(tree, "<module>")
    return dict(findings)


def service_closure_files() -> set[Path]:
    """Every source file any service's static closure reaches (finding 3)."""
    files: set[Path] = set()
    for entries, allowed, prefixes in SERVICES.values():
        files |= static_closure(entries, allowed, prefixes)[1]
    return files


def persona_installer_importers(directory: Path, relative_to: Path = REPO_ROOT) -> set[str]:
    """Files under ``directory`` importing ``tools.persona_installer`` or a
    ``tools.persona_installer.*`` submodule (function-level imports included)."""
    importers = set()
    for path in sorted(directory.rglob("*.py")):
        for _lineno, name, _node in _imports(ast.parse(path.read_text(encoding="utf-8"))):
            if name == "tools.persona_installer" or name.startswith("tools.persona_installer."):
                importers.add(path.relative_to(relative_to).as_posix())
    return importers


def runtime_controller_moved_name_patches(root: Path = REPO_ROOT / "tools"):
    """Every place under ``root`` that rebinds a RECEIPT_MOVED_NAMES name ON
    runtime_controller: a ``"tools.runtime_controller.<name>"`` patch-target string,
    ``setattr``/``patch.object`` on a runtime_controller alias with the name, or an
    attribute assignment ``<alias>.<name> = ...``. Returns ``{(file, line, name)}``."""
    moved = set(RECEIPT_MOVED_NAMES)
    out: set[tuple[str, int, str]] = set()
    this_file = Path(__file__).resolve()
    for path in sorted(root.rglob("*.py")):
        if path.resolve() == this_file:
            continue
        text = path.read_text(encoding="utf-8")
        if "runtime_controller" not in text:
            continue
        relative = path.relative_to(root.parent).as_posix()
        tree = ast.parse(text)
        aliases = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "tools" and not node.level:
                aliases.update(a.asname or a.name for a in node.names if a.name == "runtime_controller")
            elif isinstance(node, ast.Import):
                aliases.update(a.asname for a in node.names if a.name == "tools.runtime_controller" and a.asname)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                prefix = "tools.runtime_controller."
                if node.value.startswith(prefix) and node.value[len(prefix):] in moved:
                    out.add((relative, node.lineno, node.value[len(prefix):]))
            elif isinstance(node, ast.Call) and len(node.args) >= 2:
                func = node.func
                called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
                target, attr = node.args[0], node.args[1]
                if (
                    called in {"setattr", "object"}
                    and isinstance(target, ast.Name)
                    and target.id in aliases
                    and isinstance(attr, ast.Constant)
                    and attr.value in moved
                ):
                    out.add((relative, node.lineno, attr.value))
            elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id in aliases
                        and target.attr in moved
                    ):
                        out.add((relative, node.lineno, target.attr))
    return out


RENAME_OR_SANCTION = (
    "rename or sanction: drop the import/edge, or add it to the named sanction "
    "table with its reason (T-5533)"
)


class ServiceImportClosureTest(unittest.TestCase):
    def test_service_closures_are_closed(self) -> None:
        for service, (entries, allowed, prefixes) in SERVICES.items():
            with self.subTest(service=service):
                modules = loaded_modules(entries)
                for entry in entries:
                    self.assertIn(entry, modules)
                self.assertEqual(denied(modules), [], RENAME_OR_SANCTION)
                self.assertEqual(outside(modules, allowed, prefixes), [], RENAME_OR_SANCTION)
                if service != "broker":
                    self.assertNotIn("tools.host_control.planner", modules)

    def test_approval_does_not_load_executor_side_code(self) -> None:
        modules = loaded_modules(SERVICES["approval"][0])
        self.assertNotIn(ACTIVATION, modules)
        self.assertNotIn("tools.user_systemd", modules)

    def test_executor_unit_fire_budget_path_stays_closed(self) -> None:
        # _parse_unit_fire_timeout_budget -> _unit_fire_timeout_seconds ->
        # _runtime_budget_constants used to lazily import runtime_controller.
        extra = (
            "from tools.persona_installer_activation import _unit_fire_timeout_seconds\n"
            "_unit_fire_timeout_seconds(candidate_count=2, post_start_count=1)\n"
        )
        modules = loaded_modules(SERVICES["executor"][0], extra=extra)
        self.assertEqual(denied(modules), [], RENAME_OR_SANCTION)
        self.assertEqual(outside(modules, SERVICES["executor"][1]), [], RENAME_OR_SANCTION)

    def test_executor_receipt_validation_path_stays_closed(self) -> None:
        # T-5572: engine validates the disabled-first-fire invocation receipt through
        # the leaf module; before, this op path lazily loaded runtime_controller.
        extra = (
            "from pathlib import Path\n"
            "from tools.host_control.executor import engine\n"
            "try:\n"
            "    engine._receipt.validate_initial_viability_invocation_receipt(\n"
            "        'recovery', invocation_id='0' * 32, state_root=Path('/nonexistent-t5572-state-root'),\n"
            "        allowed_runtime_refs=('claude/interactive-subscription',),\n"
            "    )\n"
            "except RuntimeError:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit('a missing receipt validated')\n"
        )
        modules = loaded_modules(SERVICES["executor"][0], extra=extra)
        self.assertIn(RECEIPT, modules)
        self.assertNotIn("tools.runtime_controller", modules)
        self.assertEqual(denied(modules), [])
        self.assertEqual(outside(modules, SERVICES["executor"][1]), [])

    def test_broker_plan_execution_stays_closed(self) -> None:
        # T-5533: EXECUTE HostPlanService.plan() (the import-only probe missed 9
        # modules persona_installer loaded lazily while planning) against a fixture
        # registry holding a producer-registered plan, and diff sys.modules.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_broker_fixture(root)
            extra = _BROKER_PLAN_PROBE.format(root=str(root))
            modules = loaded_modules((BROKER_ENTRY,), extra=extra)
            loaded_by_plan = json.loads((root / "loaded-by-plan.json").read_text(encoding="utf-8"))
        self.assertIn("tools.host_control.planner", modules)
        self.assertEqual(denied(modules), [], RENAME_OR_SANCTION)
        self.assertEqual(outside(modules, BROKER_ALLOWED, BROKER_ALLOWED_PREFIXES), [], RENAME_OR_SANCTION)
        self.assertEqual(denied(loaded_by_plan), [], RENAME_OR_SANCTION)
        self.assertEqual(
            outside(loaded_by_plan, BROKER_ALLOWED, BROKER_ALLOWED_PREFIXES), [], RENAME_OR_SANCTION
        )
        self.assertEqual([name for name in modules if name.startswith("_germinate_handlers_")], [])


#: Runs inside the probe interpreter after the broker entry is imported.
_BROKER_PLAN_PROBE = """
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
before = set(sys.modules)
from tools.host_control.journal import BrokerJournal
from tools.host_control.planner import HostPlanService
from tools.host_control.policy import HostPolicy
from tools.host_control.registry import SeatRegistry
from tools.host_control.trusted_files import RootOwnedFileLoader
root = Path({root!r})
files = RootOwnedFileLoader._open_for_test((root,), expected_owner_uid=os.getuid())
registry = SeatRegistry.load(root / "registry.json", files=files, framework_root=root / "framework")
service = HostPlanService(
    registry=registry,
    policy=HostPolicy.load(root / "policy.json", files=files),
    journal=BrokerJournal.open(root / "broker.sqlite3"),
    clock=lambda: datetime(2026, 7, 31, 1, 0, tzinfo=timezone.utc),
    handle_factory=lambda: "a" * 64,
    plan_ttl=timedelta(hours=1),
    contract_version="org.pantheon.HostBroker1/v1",
    broker_revision="closure-probe",
    host_operation_families=frozenset(("persona.install",)),
    health_check=lambda: "ready",
)
result = service.plan(1003, "recovery", "manifest-r1", "closure-probe")
assert result.plan_handle == "a" * 64, result
(root / "loaded-by-plan.json").write_text(json.dumps(sorted(set(sys.modules) - before)), encoding="utf-8")
"""


def _write_broker_fixture(root: Path) -> None:
    """A registry (with a producer-registered plan), a policy and a framework
    overlay under ``root`` for the runtime probe. Planning runs HERE, in the test
    process, never in the probe."""
    import hashlib

    from tools import host_plan_producer as producer
    from tools._host_control_plan_fixtures import framework_overlay, register_plan
    from tools.test_host_control_core import _manifest_dict

    root.chmod(0o700)
    templates = (
        "tools/systemd/pantheon-persona-consume@.service",
        "tools/systemd/pantheon-persona-consume@.timer",
        "tools/systemd/pantheon-persona-quota@.service",
        "tools/systemd/pantheon-persona-quota@.timer",
    )
    framework_overlay(root / "framework", REPO_ROOT, templates)
    manifest_path = root / "instance.yaml"
    manifest_path.write_text(yaml.safe_dump(_manifest_dict(), sort_keys=False), encoding="utf-8")
    manifest_path.chmod(0o600)
    seat = {
        "seat_id": "recovery", "uid": 1002, "persona": "recovery", "role": "recovery",
        "instance": "test-instance", "account": "claude-fallback", "harness": "codex",
        "install_root": "/projection",
    }
    manifest = {
        "revision": "manifest-r1",
        "path": str(manifest_path),
        "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "framework_revision": "test-rev",
        "framework_sha256": producer.framework_artifact_digest(templates),
        "template_ids": list(templates),
    }
    registry = {
        "schema": "pantheon-host-seat-registry/v1",
        "seats": [
            {**seat, "seat_id": "consultant", "uid": 1003, "persona": "consultant", "role": "worker",
             "account": "codex", "install_root": "/home/codex"},
            seat,
        ],
        "manifests": [manifest],
    }
    plan = producer.produce_plan(framework_root=REPO_ROOT, seat_record=seat, manifest_record=manifest)
    register_plan(registry, root, plan, seat_id="recovery", manifest_revision="manifest-r1")
    policy = {
        "schema": "pantheon-host-policy/v1",
        "revision": "policy-r1",
        "grants": [{
            "subject_seat_id": "consultant", "action": "plan", "target_seat_id": "recovery",
            "manifest_revisions": ["manifest-r1"], "operation_families": ["persona.install"],
            "approval_class": "human",
        }],
    }
    for name, value in (("registry.json", registry), ("policy.json", policy)):
        (root / name).write_text(json.dumps(value), encoding="utf-8")
        (root / name).chmod(0o600)


class SourceImportClosureTest(unittest.TestCase):
    def test_no_host_control_file_imports_persona_installer(self) -> None:
        # T-5533: the broker's planner exception is gone; tools.persona_installer
        # and any tools.persona_installer.* submodule are out of the package
        # (tools.persona_installer_contracts is a different module and stays allowed).
        self.assertEqual(persona_installer_importers(HOST_CONTROL), set(), RENAME_OR_SANCTION)
        lazy_denied = set()
        for path in sorted(HOST_CONTROL.rglob("*.py")):
            relative = path.relative_to(REPO_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            top_level = {id(node) for node in tree.body}
            for lineno, name, node in _imports(tree):
                for prefix in DENIED_PREFIXES[1:]:
                    if name == prefix or name.startswith(prefix + "."):
                        if id(node) in top_level:
                            self.fail(
                                f"{relative}:{lineno} imports denied module {name} at module level; "
                                f"{RENAME_OR_SANCTION}"
                            )
                        lazy_denied.add((relative, name))
        self.assertEqual(lazy_denied, SANCTIONED_LAZY_DENIED_IMPORTS, RENAME_OR_SANCTION)

    def test_persona_installer_import_check_flags_each_shape(self) -> None:
        shapes = {
            "plain.py": "import tools.persona_installer\n",
            "aliased.py": "import tools.persona_installer as pi\n",
            "from_tools.py": "from tools import persona_installer\n",
            "from_module.py": "from tools.persona_installer import Manifest\n",
            "submodule.py": "import tools.persona_installer.sub\n",
            "nested.py": "def f():\n    from tools.persona_installer import PersonaInstaller\n",
        }
        allowed = {
            "contracts_ok.py": "from tools.persona_installer_contracts import InstallPlan\n",
            "activation_ok.py": "from tools import persona_installer_activation\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, source in {**shapes, **allowed}.items():
                (root / name).write_text(source, encoding="utf-8")
            found = persona_installer_importers(root, relative_to=root)
        self.assertEqual(found, set(shapes))

    def test_no_dynamic_imports_outside_the_package_lazy_loader(self) -> None:
        # T-5516 review MED gap: a function-level importlib.import_module("tools.mailbox")
        # was invisible to the import-statement checks.
        sanctioned = {
            key: count
            for key, count in SANCTIONED_DYNAMIC_IMPORTS.items()
            if key[0].startswith("tools/host_control/")
        }
        self.assertEqual(dynamic_import_findings(), sanctioned, RENAME_OR_SANCTION)

    def test_no_dynamic_imports_in_any_file_a_service_closure_reaches(self) -> None:
        # T-5533 (carried-in finding 3): persona_installer_contracts,
        # persona_installer_activation, runtime_invocation_receipt, user_systemd and
        # the germinate modules are scanned too, not just tools/host_control.
        files = service_closure_files() | set(HOST_CONTROL.rglob("*.py"))
        for required in (CONTRACTS, ACTIVATION, RECEIPT, "tools.user_systemd", "tools.germinate.op_registry"):
            self.assertIn(_module_file(required), files)
        self.assertEqual(dynamic_import_findings(files), SANCTIONED_DYNAMIC_IMPORTS, RENAME_OR_SANCTION)

    def test_dynamic_scan_classifies_each_shape(self) -> None:
        # T-5533 (carried-in finding 2): each unknown-denied shape, in a small
        # benign fixture, is flagged with its kind; benign look-alikes are not.
        shapes = {
            "importlib_module.py": ("import importlib\n", ("<module>", "module", "importlib")),
            "import_module_alias.py": (
                "from importlib import import_module as load\n", ("<module>", "module", "importlib")
            ),
            "dunder_import.py": ("def f():\n    return __import__('json')\n", ("f", "name", "__import__")),
            "direct_exec.py": ("def f(code):\n    exec(code)\n", ("f", "builtin", "exec")),
            "aliased_exec.py": ("run = exec\n", ("<module>", "reference", "exec")),
            "eval_as_argument.py": ("def f(apply):\n    return apply(eval)\n", ("f", "reference", "eval")),
            "aliased_compile_import.py": (
                "from builtins import compile as build\n", ("<module>", "reference", "compile")
            ),
            "builtins_name.py": ("x = __builtins__\n", ("<module>", "name", "__builtins__")),
            "builtins_subscript.py": (
                "def f():\n    return globals()['__builtins__']\n", ("f", "subscript", "__builtins__")
            ),
            "exec_subscript.py": ("def f(table):\n    return table['exec']\n", ("f", "subscript", "exec")),
            "sys_modules_subscript.py": (
                "import sys\ndef f():\n    return sys.modules['importlib']\n", ("f", "sys.modules", "modules")
            ),
            "sys_modules_alias.py": (
                "import sys as system\nmods = system.modules\n", ("<module>", "sys.modules", "modules")
            ),
            "sys_modules_from.py": ("from sys import modules\n", ("<module>", "sys.modules", "modules")),
            "getattr_sys_modules.py": (
                "import sys\ndef f():\n    return getattr(sys.modules, 'get')\n",
                ("f", "getattr", "sys.modules"),
            ),
            "getattr_exec.py": ("def f(b):\n    return getattr(b, 'exec')\n", ("f", "getattr", "exec")),
            "getattr_on_builtins.py": (
                "def f():\n    return getattr(__builtins__, 'open')\n", ("f", "getattr", "__builtins__")
            ),
            # T-5533 review r1: the shapes that slipped past r0
            "operator_attrgetter.py": (
                "import operator\nimport sys\nmods = operator.attrgetter('modules')(sys)\n",
                ("<module>", "module", "operator"),
            ),
            "vars_sys.py": ("import sys\ndef f():\n    return vars(sys).get('modules')\n", ("f", "vars", "sys")),
            "pickle_loads.py": ("import pickle\ndef f(b):\n    return pickle.loads(b)\n", ("<module>", "module", "pickle")),
            "marshal_loads.py": ("from marshal import loads\n", ("<module>", "module", "marshal")),
        }
        benign = {
            "plain_vars.py": "def f(o):\n    return vars(o)\n",
            "regex_compile.py": "import re\nPATTERN = re.compile('x')\n",
            "plain_getattr.py": "def f(o):\n    return getattr(o, 'name')\n",
            "plain_subscript.py": "def f(d):\n    return d['key']\n",
            "other_modules_attr.py": "def f(pkg):\n    return pkg.modules\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, (source, _) in shapes.items():
                (root / name).write_text(source, encoding="utf-8")
            for name, source in benign.items():
                (root / name).write_text(source, encoding="utf-8")
            findings = dynamic_import_findings(sorted(root.glob("*.py")), root=root)
        for name, (_, (scope, kind, label)) in shapes.items():
            with self.subTest(shape=name):
                self.assertIn((name, scope, kind, label), findings)
        flagged_files = {key[0] for key in findings}
        self.assertEqual(flagged_files, set(shapes))

    def test_lazy_loader_target_table_is_pinned(self) -> None:
        self.assertEqual(lazy_loader_exports(), LAZY_LOADER_EXPORTS)
        tree = ast.parse((REPO_ROOT / LAZY_LOADER_FILE).read_text(encoding="utf-8"))
        (getattr_fn,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "__getattr__"]
        calls = [
            n for n in ast.walk(getattr_fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "import_module"
        ]
        self.assertEqual(len(calls), 1)
        self.assertEqual([ast.dump(a) for a in calls[0].args], [ast.dump(ast.Name("module", ast.Load()))])
        self.assertEqual(calls[0].keywords, [])
        bindings = [
            ast.unparse(n.value) for n in ast.walk(getattr_fn)
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "module" for t in n.targets)
        ]
        self.assertEqual(bindings, ["_EXPORTS.get(name)"])

    def test_static_service_closures_hold_function_level_imports_to_the_allowlist(self) -> None:
        # T-5516 review MED gap: a function-level `from tools import flip_policy_report`
        # (which loads denied modules) passed the import-time-only allowlist.
        for service, (entries, allowed, prefixes) in SERVICES.items():
            with self.subTest(service=service):
                self.assertEqual(
                    static_closure_violations(entries, allowed, prefixes),
                    SANCTIONED_FUNCTION_LEVEL_IMPORTS,
                    RENAME_OR_SANCTION,
                )

    def test_static_walk_sees_function_level_and_lazy_loader_edges(self) -> None:
        # The walker itself, on a scratch tree: a nested import and a lazy-loader
        # name both count, and the violation is keyed by the importing file.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools" / "svc").mkdir(parents=True)
            for init in ("tools/__init__.py", "tools/svc/__init__.py"):
                (root / init).write_text("", encoding="utf-8")
            (root / "tools" / "flip_policy_report.py").write_text("", encoding="utf-8")
            (root / "tools" / "svc" / "probe.py").write_text(
                "def f():\n"
                "    from tools import flip_policy_report\n"
                "    from tools.host_control import HostPlanService\n",
                encoding="utf-8",
            )
            found = {module for _line, module in module_imports("tools.svc.probe", root)}
            self.assertIn("tools.flip_policy_report", found)
            self.assertIn("tools.host_control.planner", found)
            self.assertEqual(
                static_closure_violations(
                    ("tools.svc.probe",), frozenset({"tools", "tools.svc"}), root=root
                ),
                {("tools/svc/probe.py", "tools.flip_policy_report")},
            )

    def test_static_walk_resolves_package_alias_attribute_access(self) -> None:
        # T-5533 (carried-in finding 1): hc.<_EXPORTS name> through an alias of
        # tools.host_control is the loader-target edge, in each binding shape.
        shapes = {
            "aliased": "import tools.host_control as hc\ndef f():\n    return hc.HostPlanService\n",
            "from_tools": "from tools import host_control\ndef f():\n    return host_control.HostPlanService\n",
            "from_tools_as": "from tools import host_control as pkg\nX = pkg.HostPlanService\n",
            "bare_chain": "import tools.host_control\nX = tools.host_control.HostPlanService\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools" / "svc").mkdir(parents=True)
            for init in ("tools/__init__.py", "tools/svc/__init__.py"):
                (root / init).write_text("", encoding="utf-8")
            for name, source in shapes.items():
                (root / "tools" / "svc" / f"{name}.py").write_text(source, encoding="utf-8")
            (root / "tools" / "svc" / "unrelated.py").write_text(
                "import tools.host_control as hc\nX = hc.not_an_export\n", encoding="utf-8"
            )
            for name in shapes:
                with self.subTest(shape=name):
                    found = {module for _line, module in module_imports(f"tools.svc.{name}", root)}
                    self.assertIn("tools.host_control.planner", found)
            unrelated = {module for _line, module in module_imports("tools.svc.unrelated", root)}
            self.assertNotIn("tools.host_control.planner", unrelated)

    def test_static_closure_reports_the_files_it_reached(self) -> None:
        violations, reached = static_closure(SERVICES["executor"][0], SERVICES["executor"][1])
        self.assertEqual(violations, set())
        self.assertIn(_module_file(RECEIPT), reached)
        self.assertIn(_module_file("tools.user_systemd"), reached)

    def test_new_modules_import_only_their_closed_sets(self) -> None:
        stdlib = sys.stdlib_module_names
        allowed = {
            "persona_installer_contracts.py": frozenset(),
            "persona_installer_activation.py": frozenset({CONTRACTS, "tools.user_systemd"}),
            "runtime_invocation_receipt.py": frozenset(),
        }
        for filename, tools_allowed in allowed.items():
            for lineno, name in imported_names(REPO_ROOT / "tools" / filename):
                with self.subTest(module=filename, imports=name, line=lineno):
                    if name == "__future__" or name.split(".")[0] in stdlib:
                        continue
                    self.assertIn(name, tools_allowed)


class TypeIdentityTest(unittest.TestCase):
    @staticmethod
    def _top_level_names(module) -> list[str]:
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        names = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, ast.Assign):
                names.extend(t.id for t in node.targets if isinstance(t, ast.Name))
        return names

    def test_every_moved_name_is_the_same_object_via_persona_installer(self) -> None:
        for module in (CON, ACT):
            names = self._top_level_names(module)
            self.assertGreater(len(names), 10)
            for name in names:
                if module is ACT and name in BudgetTranscriptionTest.NAMES:
                    continue  # the transcribed runtime budget values are activation-local
                with self.subTest(module=module.__name__, name=name):
                    self.assertIs(getattr(PI, name), getattr(module, name))

    def test_isinstance_holds_across_import_paths(self) -> None:
        op = PI.InstallOperation(
            operation_id="x.check",
            layer=0,
            kind="source-check",
            target="pantheon-framework",
            precondition={},
            desired={},
            inverse=None,
            privilege="unprivileged",
            owner="recovery",
            provenance={},
        )
        self.assertIsInstance(op, CON.InstallOperation)
        record = CON.MutationRecord(
            operation_id="x", kind="mkdir", target="/t", owner="recovery", changed=False, before={}, after_fingerprint=None
        )
        self.assertIsInstance(record, PI.MutationRecord)
        evidence = CON.OperationEvidence(operation_id="x", layer=0, kind="source-check", ok=True, detail="d")
        self.assertIsInstance(evidence, PI.OperationEvidence)
        self.assertIsInstance(PI.PreflightResult(True, "ok", "d"), CON.PreflightResult)
        with self.assertRaises(PI.InstallerError):
            raise CON.OperationFailed("boom")
        with self.assertRaises(CON.InstallerError):
            raise PI.ManifestError("bad")
        self.assertTrue(issubclass(PI.PlanRefused, CON.InstallerError))


class ReceiptValidatorMoveTest(unittest.TestCase):
    """T-5572: runtime_controller re-exports the moved receipt-validator closure."""

    def test_moved_names_are_the_same_objects_via_runtime_controller(self) -> None:
        from tools import runtime_controller as rc
        from tools import runtime_invocation_receipt as receipt

        names = TypeIdentityTest._top_level_names(receipt)
        self.assertEqual(tuple(sorted(names)), RECEIPT_MOVED_NAMES)
        for name in RECEIPT_MOVED_NAMES:
            with self.subTest(name=name):
                self.assertIs(getattr(rc, name), getattr(receipt, name))

    def test_runtime_controller_does_not_redefine_a_moved_name(self) -> None:
        # A redefinition would split the binding: rc's callers would see one
        # object and the moved validator another.
        tree = ast.parse((REPO_ROOT / "tools" / "runtime_controller.py").read_text(encoding="utf-8"))
        bound = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    bound.update(x.id for x in ast.walk(target) if isinstance(x, ast.Name))
            elif isinstance(node, ast.Global):
                bound.update(node.names)
        self.assertEqual(sorted(bound & set(RECEIPT_MOVED_NAMES)), [])
        (reexport,) = [
            node for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.module == RECEIPT
        ]
        self.assertEqual(sorted(alias.name for alias in reexport.names), list(RECEIPT_MOVED_NAMES))
        self.assertTrue(all(alias.asname is None for alias in reexport.names))

    def test_nothing_patches_a_moved_name_on_runtime_controller(self) -> None:
        # The moved code resolves its helpers in tools.runtime_invocation_receipt, so a
        # patch of e.g. rc.DEFAULT_STATE_ROOT or rc._state_root would silently miss it.
        # Patch the leaf module instead.
        patches = runtime_controller_moved_name_patches()
        self.assertEqual(
            sorted(p for p in patches if p[2] not in RC_PATCHABLE_MOVED_NAMES), []
        )

    def test_patch_scanner_detects_each_patch_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "tools"
            root.mkdir()
            (root / "probe.py").write_text(
                "from unittest import mock\n"
                "from tools import runtime_controller as rc\n"
                "def f(monkeypatch):\n"
                "    monkeypatch.setattr(rc, 'DEFAULT_STATE_ROOT', None)\n"
                "    mock.patch.object(rc, '_state_root')\n"
                "    mock.patch('tools.runtime_controller._persona_state')\n"
                "    rc._canonical_bytes = None\n",
                encoding="utf-8",
            )
            found = {name for _file, _line, name in runtime_controller_moved_name_patches(root)}
        self.assertEqual(found, {"DEFAULT_STATE_ROOT", "_state_root", "_persona_state", "_canonical_bytes"})

    def test_runtime_source_hash_paths_cover_the_moved_bytes(self) -> None:
        # The moved code was drift-covered inside runtime_controller.py; it stays so.
        from tools import runtime_controller as rc

        self.assertIn("tools/runtime_invocation_receipt.py", rc.RUNTIME_SOURCE_HASH_PATHS)
        self.assertIn("tools/runtime_controller.py", rc.RUNTIME_SOURCE_HASH_PATHS)
        self.assertEqual(len(set(rc.RUNTIME_SOURCE_HASH_PATHS)), len(rc.RUNTIME_SOURCE_HASH_PATHS))
        for relative in rc.RUNTIME_SOURCE_HASH_PATHS:
            with self.subTest(path=relative):
                self.assertTrue((REPO_ROOT / relative).is_file())


class BudgetTranscriptionTest(unittest.TestCase):
    NAMES = (
        "RUNTIME_START_TIMEOUT_SECONDS",
        "RUNTIME_INPUT_READY_TIMEOUT_SECONDS",
        "PROVIDER_COMPLETION_TIMEOUT_SECONDS",
        "POST_START_TIMEOUT_SECONDS",
        "POST_START_ASSESSMENT_CAPTURE_COUNT",
        "POST_START_ASSESSMENT_CAPTURE_INTERVAL_SECONDS",
    )

    def test_transcribed_values_equal_runtime_controller(self) -> None:
        from tools import runtime_controller

        for name in self.NAMES:
            with self.subTest(name=name):
                self.assertEqual(getattr(ACT, name), getattr(runtime_controller, name))
                self.assertIs(type(getattr(ACT, name)), type(getattr(runtime_controller, name)))


class FrameworkArtifactCoverageTest(unittest.TestCase):
    CODE = (
        "tools/persona_installer.py",
        "tools/persona_installer_contracts.py",
        "tools/persona_installer_activation.py",
        "tools/mcp_allowlist_registry.py",
        "tools/mcp_allowlist.yaml",
    )

    def _manifest(self, root: Path):
        document = {
            "manifest_version": 2,
            "persona_installer": {
                "schema": "pantheon-persona-install/v1",
                "framework": {"source": "pantheon-config", "revision": "test-rev"},
                "paths": {
                    "repo": "pantheon-config",
                    "unit_dir": ".config/systemd/user",
                    "state_dir": ".local/state/pantheon/persona-installer",
                },
                "mailbox": {"hub": "/mailbox", "group": "pantheon-mailbox", "probe_peer": "orchestrator"},
                "systemd": {
                    "service_template": "tools/systemd/pantheon-persona-consume@.service",
                    "timer_template": "tools/systemd/pantheon-persona-consume@.timer",
                },
                "policy": {"bundle": ["rulebook/", "sop/", "adr/"]},
                "security": {"credential_mode": "references-only", "prohibit_auth_copy": True},
                "lifecycle": {
                    "require_disabled_first_fire": True,
                    "require_mailbox_send_receive": True,
                    "require_finite_next_elapse": True,
                    "require_observed_timed_fire": True,
                },
                "provenance": {"binding_source": "instance.yaml"},
            },
            "instance": {"id": "test-instance", "harness_default": "claude-code"},
            "roles": {
                "recovery": {"persona": "recovery", "account": "claude-fallback", "harness": "claude-code"}
            },
        }
        path = root / "instance.yaml"
        path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return PI.Manifest.load(path)

    def test_framework_code_artifacts_names_all_three_files(self) -> None:
        self.assertEqual(CON.FRAMEWORK_CODE_ARTIFACTS, self.CODE)
        self.assertIs(PI.FRAMEWORK_CODE_ARTIFACTS, CON.FRAMEWORK_CODE_ARTIFACTS)

    def test_plan_source_operation_digests_all_three_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = PI.PersonaInstaller(REPO_ROOT).plan(self._manifest(root), "recovery", root=root / "projection")
        (source,) = [op for op in plan.operations if op.operation_id == "framework.source"]
        artifacts = source.desired["artifacts"].split(",")
        self.assertEqual(tuple(artifacts[:len(self.CODE)]), self.CODE)
        self.assertEqual(
            plan.provenance["source_artifact_sha256"],
            PI._artifact_digest(REPO_ROOT, tuple(Path(item) for item in artifacts)),
        )

    def test_digest_changes_when_any_code_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in self.CODE:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"# " + relative.encode() + b"\n")
            paths = tuple(Path(item) for item in self.CODE)
            base = PI._artifact_digest(root, paths)
            for relative in self.CODE:
                with self.subTest(changed=relative):
                    target = root / relative
                    original = target.read_bytes()
                    target.write_bytes(original + b"# drift\n")
                    self.assertNotEqual(PI._artifact_digest(root, paths), base)
                    target.write_bytes(original)
            self.assertEqual(PI._artifact_digest(root, paths), base)


if __name__ == "__main__":
    unittest.main()
