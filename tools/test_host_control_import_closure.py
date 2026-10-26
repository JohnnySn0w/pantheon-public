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
* the broker's single sanctioned exception: ``tools/host_control/planner.py``
  plans in-process with ``PersonaInstaller`` (T-5533 removes it before T-2920
  enables the broker). With planner stubbed out, the broker entry is clean, so
  nothing else reaches the deny list;
* source level (AST, function-level imports included): only planner.py in
  tools/host_control imports ``tools.persona_installer``; no host_control module
  imports a denied module at module level, and the function-level ones are
  exactly ``SANCTIONED_LAZY_DENIED_IMPORTS`` (empty since T-5572 moved the
  executor's receipt validator into ``tools.runtime_invocation_receipt``); the
  new leaf modules import only the standard library and their allowed ``tools``
  modules;
* T-5572 (the T-5516 review's MED gap): no dynamic import anywhere in
  tools/host_control (``importlib``/``__import__``/spec loading/``exec``/
  ``eval``/``compile``/``builtins``) except the package's PEP 562 lazy loader,
  whose target table is pinned; and a STATIC closure walk per service follows
  function-level imports (and the lazy loader's table) transitively, holding every
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

#: Per service: entry modules, and the non-host_control ``tools.*`` modules allowed.
SERVICES = {
    "approval": (
        ("tools.host_control.approval.dbus_service", "tools.host_control.approval.service"),
        frozenset({"tools", CONTRACTS}),
    ),
    "executor": (
        ("tools.host_control.executor.__main__", "tools.host_control.executor.dbus_service"),
        frozenset({"tools", CONTRACTS, ACTIVATION, RECEIPT, "tools.user_systemd"}),
    ),
}

#: The broker with planner stubbed: its other modules also load the germinate
#: op-enum registry and WAL (gate 36 / op registry), which are not agent-facing.
BROKER_ENTRY = "tools.host_control.dbus_service"
BROKER_ALLOWED = frozenset({"tools", CONTRACTS, "tools.germinate"})
BROKER_ALLOWED_PREFIXES = ("tools.germinate.",)
SANCTIONED_INSTALLER_IMPORTERS = frozenset({"tools/host_control/planner.py"})
#: Function-level imports of a denied module inside tools/host_control, by file.
#: Empty: T-5516's one entry (executor engine -> runtime_controller, for the
#: invocation-receipt validator) went away when T-5572 moved the validator into
#: the stdlib-only leaf tools.runtime_invocation_receipt.
SANCTIONED_LAZY_DENIED_IMPORTS: frozenset[tuple[str, str]] = frozenset()

#: T-5572: (importing file, imported module) edges in a service's STATIC closure
#: (function-level imports included) that leave the service's allowlist. Empty.
SANCTIONED_FUNCTION_LEVEL_IMPORTS: frozenset[tuple[str, str]] = frozenset()

#: The broker walk stops at planner.py: its in-process PersonaInstaller is the
#: T-5533 exception (the runtime probe stubs the same module).
BROKER_STATIC_STOP = frozenset({"tools.host_control.planner"})

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
DYNAMIC_IMPORT_MODULES = frozenset({"importlib", "runpy", "imp", "pkgutil", "zipimport", "builtins"})
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
#: (file, enclosing function or "<module>", kind, name) -> count. Exactly the
#: lazy loader: its module-level ``from importlib import import_module`` and the
#: single ``import_module(module)`` call inside ``__getattr__``.
SANCTIONED_DYNAMIC_IMPORTS = {
    (LAZY_LOADER_FILE, "<module>", "module", "importlib"): 1,
    (LAZY_LOADER_FILE, "<module>", "name", "import_module"): 1,
    (LAZY_LOADER_FILE, "__getattr__", "name", "import_module"): 1,
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
if {stub_planner!r}:
    stub = types.ModuleType("tools.host_control.planner")
    class HostPlanService:  # stand-in: only the name is imported
        pass
    stub.HostPlanService = HostPlanService
    sys.modules["tools.host_control.planner"] = stub
import importlib
for name in {modules!r}:
    importlib.import_module(name)
{extra}
print(json.dumps(sorted(sys.modules)))
"""


def loaded_modules(modules, *, stub_planner=False, extra=""):
    probe = _PROBE.format(root=str(REPO_ROOT), modules=tuple(modules), stub_planner=stub_planner, extra=extra)
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


def module_imports(name: str, root: Path = REPO_ROOT):
    """Every module ``name``'s source loads when its code runs: module-level AND
    function-level imports (not TYPE_CHECKING blocks), parent packages included,
    ``from pkg import sub`` resolved to the submodule, and ``from
    tools.host_control import NAME`` resolved through the lazy loader's table.
    Yields ``(lineno, imported_module)``."""
    path = _module_file(name, root)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip = _type_checking_node_ids(tree)
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    exports = lazy_loader_exports()
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Import):
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


def static_closure_violations(entries, allowed, prefixes=(), stop=frozenset(), root: Path = REPO_ROOT):
    """Walk the service's tools.* closure from ``entries`` through every allowed
    module (tools.host_control.* plus ``allowed``/``prefixes``). Returns the set of
    ``(importing file, module)`` edges that leave the allowlist; the walk does not
    descend past them (each such edge is the regression to name)."""

    def is_allowed(module: str) -> bool:
        return (
            module in allowed
            or module == "tools.host_control"
            or module.startswith("tools.host_control.")
            or bool(prefixes) and module.startswith(prefixes)
        )

    violations: set[tuple[str, str]] = set()
    seen: set[str] = set()
    stack = list(entries)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        path = _module_file(name, root)
        if name in stop or path is None:
            continue
        relative = path.relative_to(root).as_posix()
        for _lineno, module in module_imports(name, root):
            if not (module == "tools" or module.startswith("tools.")):
                continue
            if is_allowed(module):
                stack.append(module)
            else:
                violations.add((relative, module))
    return violations


def dynamic_import_findings(root: Path = HOST_CONTROL):
    """Classify every dynamic-import surface in ``root``'s Python files.
    Returns ``{(file, enclosing function or "<module>", kind, name): count}``."""
    from collections import Counter

    findings: Counter = Counter()
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(REPO_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def visit(node: ast.AST, scope: str) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = node.name
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in DYNAMIC_IMPORT_MODULES:
                        findings[(relative, scope, "module", alias.name.split(".")[0])] += 1
                    if (alias.asname or "") in DYNAMIC_IMPORT_NAMES:
                        findings[(relative, scope, "name", alias.asname)] += 1
            elif isinstance(node, ast.ImportFrom):
                top = (node.module or "").split(".")[0]
                if not node.level and top in DYNAMIC_IMPORT_MODULES:
                    findings[(relative, scope, "module", top)] += 1
                for alias in node.names:
                    for bound in {alias.name, alias.asname} - {None}:
                        if bound in DYNAMIC_IMPORT_NAMES or bound in DYNAMIC_EVAL_BUILTINS:
                            findings[(relative, scope, "name", bound)] += 1
            elif isinstance(node, ast.Name) and node.id in DYNAMIC_IMPORT_NAMES:
                findings[(relative, scope, "name", node.id)] += 1
            elif isinstance(node, ast.Attribute) and node.attr in DYNAMIC_IMPORT_NAMES:
                findings[(relative, scope, "name", node.attr)] += 1
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in DYNAMIC_EVAL_BUILTINS
            ):
                findings[(relative, scope, "builtin", node.func.id)] += 1
            for child in ast.iter_child_nodes(node):
                visit(child, scope)

        visit(tree, "<module>")
    return dict(findings)


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


class ServiceImportClosureTest(unittest.TestCase):
    def test_approval_and_executor_closures_are_closed(self) -> None:
        for service, (entries, allowed) in SERVICES.items():
            with self.subTest(service=service):
                modules = loaded_modules(entries)
                for entry in entries:
                    self.assertIn(entry, modules)
                self.assertEqual(denied(modules), [])
                self.assertEqual(outside(modules, allowed), [])
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
        self.assertEqual(denied(modules), [])
        self.assertEqual(outside(modules, SERVICES["executor"][1]), [])

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

    def test_broker_sanctioned_exception_is_planner_only(self) -> None:
        # Unstubbed: the broker loads the installer through planner.py (T-5533).
        # If this starts failing because the installer is gone, T-5533 landed:
        # drop the exception and fold the broker into SERVICES.
        full = loaded_modules((BROKER_ENTRY,))
        self.assertIn("tools.persona_installer", full)
        # Planner stubbed: nothing else in the broker reaches the deny list.
        modules = loaded_modules((BROKER_ENTRY,), stub_planner=True)
        self.assertIn(BROKER_ENTRY, modules)
        self.assertEqual(denied(modules), [])
        self.assertEqual(outside(modules, BROKER_ALLOWED, BROKER_ALLOWED_PREFIXES), [])


class SourceImportClosureTest(unittest.TestCase):
    def test_only_planner_imports_persona_installer_in_host_control(self) -> None:
        importers = set()
        lazy_denied = set()
        for path in sorted(HOST_CONTROL.rglob("*.py")):
            relative = path.relative_to(REPO_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            top_level = {id(node) for node in tree.body}
            for lineno, name, node in _imports(tree):
                if name == "tools.persona_installer" or name.startswith("tools.persona_installer."):
                    importers.add(relative)
                    continue
                for prefix in DENIED_PREFIXES[1:]:
                    if name == prefix or name.startswith(prefix + "."):
                        if id(node) in top_level:
                            self.fail(f"{relative}:{lineno} imports denied module {name} at module level")
                        lazy_denied.add((relative, name))
        self.assertEqual(importers, SANCTIONED_INSTALLER_IMPORTERS)
        self.assertEqual(lazy_denied, SANCTIONED_LAZY_DENIED_IMPORTS)

    def test_no_dynamic_imports_outside_the_package_lazy_loader(self) -> None:
        # T-5516 review MED gap: a function-level importlib.import_module("tools.mailbox")
        # was invisible to the import-statement checks.
        self.assertEqual(dynamic_import_findings(), SANCTIONED_DYNAMIC_IMPORTS)

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
        cases = {
            service: (entries, allowed, (), frozenset())
            for service, (entries, allowed) in SERVICES.items()
        }
        cases["broker"] = ((BROKER_ENTRY,), BROKER_ALLOWED, BROKER_ALLOWED_PREFIXES, BROKER_STATIC_STOP)
        for service, (entries, allowed, prefixes, stop) in cases.items():
            with self.subTest(service=service):
                self.assertEqual(
                    static_closure_violations(entries, allowed, prefixes, stop),
                    SANCTIONED_FUNCTION_LEVEL_IMPORTS,
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

    def test_planner_docstring_records_the_exception(self) -> None:
        doc = ast.get_docstring(ast.parse((HOST_CONTROL / "planner.py").read_text(encoding="utf-8")))
        self.assertIn("T-5516", doc)
        self.assertIn("T-5533", doc)
        self.assertIn("ONLY module", doc)

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
        self.assertEqual(tuple(artifacts[:3]), self.CODE)
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
