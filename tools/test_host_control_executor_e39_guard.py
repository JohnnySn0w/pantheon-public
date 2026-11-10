"""ORG-052 gate 39, the STATIC half (T-2917 5c.2): an AST guard over the
executor package and the neutral port module, with negative controls.

Rules, applied to every ``tools/host_control/executor/*.py`` and
``tools/user_systemd.py``:

(a) no string constant (docstrings included) names the operator command-line
    clients, the manager's private control socket, or the environment
    variables that select a session bus or runtime dir;
(b) no ``SessionBus`` / ``StarterBus`` attribute anywhere; ``SystemBus`` only
    in ``executor/__main__.py`` (the executor's own service bus);
(b2) in ``executor/user_bus.py`` every bus construction connects to
    ``self.address`` and no bus-type constant (``BUS_SESSION`` /
    ``BUS_SYSTEM`` / ``BUS_STARTER``) appears (review L2(a));
(c) the operator adapter (``SystemctlUserManager``) and its backend
    (``LocalHostBackend``) are never imported or named;
(d) ``subprocess`` is imported only by ``executor/engine.py``, and every
    child-process call there passes an argv whose first element is the
    ``self._python`` (``sys.executable``) seam; never ``shell=True``.
    T-6375 (Recovery's gate-39 ruling) admits exactly ONE further child, the
    read-only seat pane probe: from ONE call site, the argv list is
    ``[self._tmux, "-S", <socket>, "has-session", "-t", f"={session}"]``
    with the literal verb, ``<socket>`` derived from
    ``seat_tmux_socket(self._identity.uid)`` (never the environment),
    ``self._tmux`` an absolute-path constructor seam (no PATH lookup),
    ``stdin=subprocess.DEVNULL``, a ``timeout=``, and an explicit dict-literal
    ``env=`` that names no ``TMUX*`` variable and inherits nothing;
(e) ``executor/user_bus.py`` never reads ``os.environ`` / ``os.getenv``.

Ruling D7: every ``DBusUserManager(...)`` constructed by a test or a test
helper passes ``runtime_root=``; ``connect=`` alone is NOT enough (review
L2(b)): the address would still be the real ``/run/user/<uid>/bus``.

Every rule has a negative control (a synthetic snippet that must be
flagged) and a near-miss that must pass. Pure AST; no dbus needed.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXECUTOR_DIR = REPO_ROOT / "tools" / "host_control" / "executor"
PORT_MODULE = REPO_ROOT / "tools" / "user_systemd.py"

# Built by concatenation so THIS file's own constants never look like a
# violation to a source-level scan of the tree.
BANNED_SUBSTRINGS = (
    "system" + "ctl",
    "login" + "ctl",
    "bus" + "ctl",
    "systemd/" + "private",
    "DBUS_SESSION_" + "BUS_ADDRESS",
    "XDG_RUNTIME" + "_DIR",
)
BANNED_BUS_ATTRIBUTES = ("SessionBus", "StarterBus")
OPERATOR_NAMES = ("System" + "ctlUserManager", "LocalHostBackend")
# review L2(a): bus constructors in user_bus.py must take self.address
BUS_CONSTRUCTORS = ("BusConnection", "Bus", "Connection", "SessionBus", "SystemBus", "StarterBus", "_connect")
BUS_TYPE_CONSTANTS = ("BUS_SESSION", "BUS_SYSTEM", "BUS_STARTER")
SUBPROCESS_CALLS = ("run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput")


def _relative(filename: str) -> str:
    path = Path(filename)
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return filename


def _is_self_python(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "_python"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _returns_python_argv(function: ast.FunctionDef) -> bool:
    returns = [node for node in ast.walk(function) if isinstance(node, ast.Return)]
    return bool(returns) and all(
        isinstance(item.value, ast.List) and item.value.elts and _is_self_python(item.value.elts[0])
        for item in returns
    )


def _is_self_tmux(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "_tmux"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _is_seat_socket_call(node: ast.AST) -> bool:
    """``seat_tmux_socket(self._identity.uid)``, the only admitted socket source."""

    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "seat_tmux_socket"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Attribute)
        and node.args[0].attr == "uid"
        and isinstance(node.args[0].value, ast.Attribute)
        and node.args[0].value.attr == "_identity"
        and isinstance(node.args[0].value.value, ast.Name)
        and node.args[0].value.value.id == "self"
    )


def _socket_is_derived(node: ast.AST, tree: ast.AST) -> bool:
    if _is_seat_socket_call(node):
        return True
    if not isinstance(node, ast.Name):
        return False
    # every binding of the name must be a plain ``name = seat_tmux_socket(self._identity.uid)``;
    # an augmented/annotated/walrus/for/with/tuple/parameter binding disqualifies it
    plain = [
        item for item in ast.walk(tree)
        if isinstance(item, ast.Assign)
        and len(item.targets) == 1
        and isinstance(item.targets[0], ast.Name)
        and item.targets[0].id == node.id
    ]
    stores = [
        item for item in ast.walk(tree)
        if isinstance(item, ast.Name) and item.id == node.id and isinstance(item.ctx, (ast.Store, ast.Del))
    ]
    params = [item for item in ast.walk(tree) if isinstance(item, ast.arg) and item.arg == node.id]
    return (
        bool(plain)
        and len(stores) == len(plain)
        and not params
        and all(_is_seat_socket_call(item.value) for item in plain)
    )


def _tmux_argv_problems(argv: ast.List, call: ast.Call, tree: ast.AST) -> list[str]:
    """Everything wrong with a ``[self._tmux, ...]`` child call; empty == the one allowed shape."""

    problems: list[str] = []
    elts = argv.elts

    def const(index: int, value: str) -> bool:
        return len(elts) > index and isinstance(elts[index], ast.Constant) and elts[index].value == value

    if len(elts) != 6:
        problems.append("tmux argv is not exactly 6 elements")
    if not const(1, "-S"):
        problems.append("tmux argv[1] is not the literal -S")
    if len(elts) > 2 and not _socket_is_derived(elts[2], tree):
        problems.append("tmux socket is not seat_tmux_socket(self._identity.uid)")
    if not const(3, "has-session"):
        problems.append("tmux verb is not the literal has-session")
    if not const(4, "-t"):
        problems.append("tmux argv[4] is not the literal -t")
    target = elts[5] if len(elts) > 5 else None
    if not (
        isinstance(target, ast.JoinedStr)
        and len(target.values) == 2
        and isinstance(target.values[0], ast.Constant)
        and target.values[0].value == "="
        and isinstance(target.values[1], ast.FormattedValue)
        and isinstance(target.values[1].value, ast.Name)
    ):
        problems.append("tmux target is not f'={session}'")
    keywords = {item.arg: item.value for item in call.keywords}
    if None in keywords:
        problems.append("tmux call splats **kwargs")
    stdin = keywords.get("stdin")
    if not (isinstance(stdin, ast.Attribute) and stdin.attr == "DEVNULL"):
        problems.append("tmux call lacks stdin=subprocess.DEVNULL")
    timeout = keywords.get("timeout")
    if timeout is None:
        problems.append("tmux call lacks a timeout")
    elif isinstance(timeout, ast.Constant) and timeout.value is None:
        problems.append("tmux call timeout is None (no timeout)")
    elif isinstance(timeout, ast.Name) and any(
        isinstance(item, (ast.Assign, ast.AnnAssign))
        and isinstance(item.value, ast.Constant)
        and item.value.value is None
        and any(
            isinstance(t, ast.Name) and t.id == timeout.id
            for t in (item.targets if isinstance(item, ast.Assign) else [item.target])
        )
        for item in ast.walk(tree)
    ):
        problems.append("tmux call timeout is a name bound to None (no timeout)")
    env = keywords.get("env")
    if not isinstance(env, ast.Dict):
        problems.append("tmux call lacks an explicit dict-literal env")
    else:
        for key in env.keys:
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                problems.append("tmux env has a non-literal key or a ** splat")
            elif key.value.upper().startswith("TMUX"):
                problems.append(f"tmux env names {key.value}")
        if any(
            isinstance(inner, ast.Attribute) and inner.attr in ("environ", "environb", "getenv", "getenvb")
            for inner in ast.walk(env)
        ):
            problems.append("tmux env reads the process environment")
    return problems


def _tmux_seam_problems(tree: ast.AST) -> list[str]:
    """``self._tmux`` is bound exactly once: ``self._tmux = tmux`` in ``__init__``, whose ``tmux=``
    parameter defaults to an ABSOLUTE path constant (no PATH lookup). Any other binding of it
    anywhere in the module is a violation. ``seat_tmux_socket`` must be the imported shared helper,
    never a local definition."""

    problems: list[str] = []
    store_counts: dict[str, int] = {}
    for leaf in ast.walk(tree):
        if isinstance(leaf, ast.Name) and isinstance(leaf.ctx, (ast.Store, ast.Del)):
            store_counts[leaf.id] = store_counts.get(leaf.id, 0) + 1
        if isinstance(leaf, (ast.Global, ast.Nonlocal)):
            for declared in leaf.names:
                store_counts[declared] = store_counts.get(declared, 0) + 1
    # a module-level name qualifies as the default's constant only if it is bound EXACTLY ONCE in
    # the whole module, to a string Constant (a later non-constant rebind disqualifies it)
    module_constants = {
        target.id: item.value.value
        for item in getattr(tree, "body", [])
        if isinstance(item, (ast.Assign, ast.AnnAssign)) and isinstance(item.value, ast.Constant)
        for target in ([item.target] if isinstance(item, ast.AnnAssign) else item.targets)
        if isinstance(target, ast.Name) and isinstance(item.value.value, str)
        and store_counts.get(target.id, 0) == 1
    }
    good_assign: ast.AST | None = None
    good_function: ast.AST | None = None
    for function in ast.walk(tree):
        if not (isinstance(function, ast.FunctionDef) and function.name == "__init__"):
            continue
        args = function.args
        names = [a.arg for a in args.args + args.kwonlyargs]
        defaults = [None] * (len(args.args) - len(args.defaults)) + list(args.defaults) + list(args.kw_defaults)
        default = dict(zip(names, defaults)).get("tmux")
        if default is None:
            continue
        value = default.value if isinstance(default, ast.Constant) else module_constants.get(getattr(default, "id", ""))
        for stmt in ast.walk(function):
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and _is_self_tmux(stmt.targets[0])
                and isinstance(stmt.value, ast.Name)
                and stmt.value.id == "tmux"
                and isinstance(value, str)
                and value.startswith("/")
            ):
                good_assign = stmt
                good_function = function
    if good_assign is None:
        problems.append("self._tmux is not an __init__ tmux= seam with an absolute-path default")
    elif good_function is not None:
        # the local ``tmux`` must reach the assignment untouched: no store, del, nonlocal or global
        # of that name anywhere in the __init__ (nested functions included)
        for inner in ast.walk(good_function):
            if isinstance(inner, ast.Name) and inner.id == "tmux" and isinstance(inner.ctx, (ast.Store, ast.Del)):
                problems.append(f"the tmux parameter is rebound or deleted at line {inner.lineno}")
            if isinstance(inner, (ast.Nonlocal, ast.Global)) and "tmux" in inner.names:
                problems.append(f"the tmux parameter is declared nonlocal/global at line {inner.lineno}")

    def target_names(node: ast.AST) -> list[ast.AST]:
        if isinstance(node, (ast.Tuple, ast.List)):
            return [leaf for element in node.elts for leaf in target_names(element)]
        if isinstance(node, ast.Starred):
            return target_names(node.value)
        return [node]

    for node in ast.walk(tree):
        bound: list[ast.AST] = []
        if isinstance(node, ast.Assign):
            bound = [leaf for t in node.targets for leaf in target_names(t)]
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr)):
            bound = target_names(node.target)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            bound = target_names(node.target)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            bound = [leaf for item in node.items if item.optional_vars for leaf in target_names(item.optional_vars)]
        if node is not good_assign and any(_is_self_tmux(leaf) for leaf in bound):
            problems.append(f"self._tmux is rebound at line {getattr(node, 'lineno', 0)}")
        if isinstance(node, ast.Delete) and any(_is_self_tmux(t) for t in node.targets):
            problems.append(f"self._tmux is deleted at line {getattr(node, 'lineno', 0)}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("setattr", "delattr"):
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and node.args[1].value == "_tmux":
                problems.append(f"self._tmux is set via {node.func.id} at line {getattr(node, 'lineno', 0)}")

    # (b): seat_tmux_socket is the shared helper, imported ONCE from the installer activation
    # module; ANY other binding of the name at any scope is a shadow
    name = "seat_tmux_socket"
    good_imports = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) != name:
                    continue
                if (
                    node in getattr(tree, "body", [])
                    and alias.asname is None
                    and node.module in ("tools.persona_installer_activation", "tools.persona_installer")
                    and node.level == 0
                ):
                    good_imports += 1
                else:
                    problems.append(f"{name} is re-imported/aliased at line {node.lineno}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if (alias.asname or alias.name.split(".")[0]) == name:
                    problems.append(f"{name} is bound by an import at line {node.lineno}")
        elif isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, (ast.Store, ast.Del)):
            problems.append(f"{name} is rebound at line {node.lineno}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
            problems.append(f"{name} is redefined at line {node.lineno}")
        elif isinstance(node, ast.arg) and node.arg == name:
            problems.append(f"{name} is a parameter at line {node.lineno}")
        elif isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
            problems.append(f"{name} is declared global/nonlocal at line {node.lineno}")
        elif isinstance(node, ast.ExceptHandler) and node.name == name:
            problems.append(f"{name} is an except-handler name at line {node.lineno}")
    if good_imports != 1:
        problems.append(f"{name} is not the helper imported from tools.persona_installer_activation")
    return problems


def _is_self_address(node: ast.AST) -> bool:
    """``self.address`` or ``str(self.address)``."""

    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "str" and len(node.args) == 1:
        node = node.args[0]
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "address"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def scan(source: str, filename: str) -> list[str]:
    """Violations of rules (a)-(e) in one module's source."""

    rel = _relative(filename).replace("\\", "/")
    name = rel.rsplit("/", 1)[-1]
    in_executor = "host_control/executor/" in rel
    is_main = in_executor and name == "__main__.py"
    is_engine = in_executor and name == "engine.py"
    is_user_bus = in_executor and name == "user_bus.py"
    tree = ast.parse(source, filename)
    violations: list[str] = []
    tmux_sites = 0
    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in ast.walk(tree):
        line = getattr(node, "lineno", 0)
        # (a) banned strings, docstrings included
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for banned in BANNED_SUBSTRINGS:
                if banned in node.value:
                    violations.append(f"{rel}:{line}: (a) string names {banned!r}")
        # (b) environment-selected or system buses
        if isinstance(node, ast.Attribute):
            if node.attr in BANNED_BUS_ATTRIBUTES:
                violations.append(f"{rel}:{line}: (b) {node.attr}")
            if node.attr == "SystemBus" and not is_main:
                violations.append(f"{rel}:{line}: (b) SystemBus outside executor/__main__.py")
            # (e) environment reads in the user-bus adapter
            if is_user_bus and isinstance(node.value, ast.Name) and node.value.id == "os" and node.attr in (
                "environ", "environb", "getenv", "getenvb"
            ):
                violations.append(f"{rel}:{line}: (e) os.{node.attr} in user_bus.py")
        if isinstance(node, ast.Name) and node.id in BANNED_BUS_ATTRIBUTES:
            violations.append(f"{rel}:{line}: (b) {node.id}")
        # (b2) review L2(a): in user_bus.py every bus construction connects
        # to self.address, and the bus-type constants never appear
        if is_user_bus:
            if (isinstance(node, ast.Name) and node.id in BUS_TYPE_CONSTANTS) or (
                isinstance(node, ast.Attribute) and node.attr in BUS_TYPE_CONSTANTS
            ):
                violations.append(f"{rel}:{line}: (b2) bus-type constant in user_bus.py")
            if isinstance(node, ast.Call):
                callee = node.func
                callee_name = (
                    callee.id if isinstance(callee, ast.Name)
                    else callee.attr if isinstance(callee, ast.Attribute) else ""
                )
                if callee_name in BUS_CONSTRUCTORS:
                    address = node.args[0] if node.args else next(
                        (item.value for item in node.keywords if item.arg in ("address", "address_or_type")), None
                    )
                    if address is None or not _is_self_address(address):
                        violations.append(f"{rel}:{line}: (b2) {callee_name}(...) address is not self.address")
        # (c) the operator adapter / backend
        if isinstance(node, ast.Name) and node.id in OPERATOR_NAMES:
            violations.append(f"{rel}:{line}: (c) names {node.id}")
        if isinstance(node, ast.Attribute) and node.attr in OPERATOR_NAMES:
            violations.append(f"{rel}:{line}: (c) names {node.attr}")
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in OPERATOR_NAMES:
                    violations.append(f"{rel}:{line}: (c) imports {alias.name}")
                if is_user_bus and node.module == "os" and alias.name in ("environ", "environb", "getenv", "getenvb"):
                    violations.append(f"{rel}:{line}: (e) imports os.{alias.name}")
        # (d) child processes
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess" and not is_engine:
                    violations.append(f"{rel}:{line}: (d) imports subprocess")
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess" and not is_engine:
            violations.append(f"{rel}:{line}: (d) imports from subprocess")
        if isinstance(node, ast.keyword) and node.arg == "shell":
            if not (isinstance(node.value, ast.Constant) and node.value.value is False):
                violations.append(f"{rel}:{line}: (d) shell= is not False")
        if isinstance(node, ast.Call):
            func = node.func
            is_subprocess_call = (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
                and func.attr in SUBPROCESS_CALLS
            )
            is_child_seam = (
                isinstance(func, ast.Attribute)
                and func.attr == "_run_child"
                and isinstance(func.value, ast.Name)
                and func.value.id == "self"
            )
            if is_subprocess_call or is_child_seam:
                if not node.args:
                    violations.append(f"{rel}:{line}: (d) child-process call without an argv")
                    continue
                argv = node.args[0]
                ok = False
                if isinstance(argv, ast.List) and argv.elts and _is_self_tmux(argv.elts[0]):
                    # T-6375: the one admitted tmux child (engine.py only, one call site)
                    tmux_sites += 1
                    if not is_engine:
                        violations.append(f"{rel}:{line}: (d) tmux child outside executor/engine.py")
                    if not is_child_seam:
                        violations.append(f"{rel}:{line}: (d) tmux child is not run through self._run_child")
                    for problem in _tmux_argv_problems(argv, node, tree):
                        violations.append(f"{rel}:{line}: (d) {problem}")
                    if tmux_sites > 1:
                        violations.append(f"{rel}:{line}: (d) a second tmux call site")
                    continue
                if isinstance(argv, ast.List):
                    ok = bool(argv.elts) and _is_self_python(argv.elts[0])
                elif isinstance(argv, ast.Name):
                    # the argv must be assigned, in the same function, from
                    # a method whose every return is [self._python, ...]
                    for function in functions.values():
                        for statement in ast.walk(function):
                            if (
                                isinstance(statement, ast.Assign)
                                and any(isinstance(t, ast.Name) and t.id == argv.id for t in statement.targets)
                                and isinstance(statement.value, ast.Call)
                                and isinstance(statement.value.func, ast.Attribute)
                                and isinstance(statement.value.func.value, ast.Name)
                                and statement.value.func.value.id == "self"
                                and statement.value.func.attr in functions
                                and _returns_python_argv(functions[statement.value.func.attr])  # type: ignore[arg-type]
                                and node in ast.walk(function)
                            ):
                                ok = True
                if not ok:
                    violations.append(f"{rel}:{line}: (d) child-process argv[0] is not self._python")
    if tmux_sites:
        violations.extend(f"{rel}:0: (d) {problem}" for problem in _tmux_seam_problems(tree))
    return violations


def guarded_modules() -> list[Path]:
    return sorted(EXECUTOR_DIR.glob("*.py")) + [PORT_MODULE]


def scan_d7(source: str, filename: str) -> list[str]:
    """Ruling D7: a DBusUserManager(...) in a test or helper must pass
    ``runtime_root=`` (review L2(b): ``connect=`` alone is NOT enough, the
    address would still be the real /run/user one)."""

    rel = _relative(filename)
    violations = []
    for node in ast.walk(ast.parse(source, filename)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name != "DBusUserManager":
            continue
        keywords = {item.arg for item in node.keywords}
        if "runtime_root" not in keywords and None not in keywords:
            violations.append(f"{rel}:{node.lineno}: (D7) bare DBusUserManager in a test")
    return violations


def _d7_sources() -> list[Path]:
    tools = REPO_ROOT / "tools"
    return sorted(set(tools.glob("test_*.py")) | set(tools.glob("_*.py")))


class RealTreeTests(unittest.TestCase):
    def test_guarded_modules_exist(self) -> None:
        names = {path.name for path in guarded_modules()}
        # T-2917 5d.3: mailbox_probe.py is guarded too
        for expected in ("engine.py", "user_bus.py", "user_manager.py", "__main__.py", "user_systemd.py", "mailbox_probe.py"):
            self.assertIn(expected, names)

    def test_executor_and_port_are_clean(self) -> None:
        violations: list[str] = []
        for path in guarded_modules():
            violations.extend(scan(path.read_text(encoding="utf-8"), str(path)))
        self.assertEqual(violations, [])

    def test_engine_child_process_seam_is_actually_checked(self) -> None:
        # anti-vacuity: the engine has a child-process call the rule inspects
        source = (EXECUTOR_DIR / "engine.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        seam_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_run_child"
        ]
        self.assertTrue(seam_calls)

    def test_engine_has_exactly_one_tmux_probe_site(self) -> None:
        # anti-vacuity for the T-6375 amendment: the allowed shape exists once and is the one checked
        tree = ast.parse((EXECUTOR_DIR / "engine.py").read_text(encoding="utf-8"))
        sites = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_run_child"
            and node.args
            and isinstance(node.args[0], ast.List)
            and node.args[0].elts
            and _is_self_tmux(node.args[0].elts[0])
        ]
        self.assertEqual(len(sites), 1)

    def test_no_bare_dbus_user_manager_in_tests(self) -> None:
        violations: list[str] = []
        sources = _d7_sources()
        self.assertIn(REPO_ROOT / "tools" / "test_host_control_executor_user_bus.py", sources)
        for path in sources:
            violations.extend(scan_d7(path.read_text(encoding="utf-8"), str(path)))
        self.assertEqual(violations, [])


ENGINE = str(EXECUTOR_DIR / "engine.py")
USER_BUS = str(EXECUTOR_DIR / "user_bus.py")
MAIN = str(EXECUTOR_DIR / "__main__.py")
OTHER = str(EXECUTOR_DIR / "config.py")


class NegativeControlTests(unittest.TestCase):
    """Each rule flags a synthetic violation; each near-miss passes."""

    def flagged(self, source: str, filename: str, rule: str) -> None:
        violations = scan(source, filename)
        self.assertTrue(any(f"({rule})" in item for item in violations), (source, violations))

    def clean(self, source: str, filename: str) -> None:
        self.assertEqual(scan(source, filename), [], source)

    def test_a_banned_strings(self) -> None:
        for banned in BANNED_SUBSTRINGS:
            with self.subTest(banned=banned):
                self.flagged(f'X = "run {banned} now"\n', OTHER, "a")
                self.flagged(f'"""docstring mentioning {banned}"""\n', OTHER, "a")
        self.clean('X = "the operator command-line client"\n', OTHER)
        self.clean('X = "systemd1 over the user bus"\n', OTHER)

    def test_b_buses(self) -> None:
        self.flagged("import dbus\nc = dbus.SessionBus()\n", USER_BUS, "b")
        self.flagged("import dbus\nc = dbus.StarterBus()\n", USER_BUS, "b")
        self.flagged("from dbus import SessionBus\nc = SessionBus()\n", USER_BUS, "b")
        self.flagged("import dbus\nc = dbus.SystemBus(private=True)\n", USER_BUS, "b")
        self.clean("import dbus\nc = dbus.SystemBus(private=True)\n", MAIN)
        self.clean("import dbus.bus\nc = dbus.bus.BusConnection('unix:path=/x')\n", OTHER)

    def test_b2_user_bus_connects_only_to_self_address(self) -> None:
        # review L2(a): negative controls
        for source in (
            "import dbus.bus\nc = dbus.bus.BusConnection('unix:path=/x')\n",
            "import dbus.bus\nc = dbus.bus.BusConnection(dbus.bus.BUS_SESSION)\n",
            "import dbus.bus\nc = dbus.bus.BusConnection(dbus.bus.BUS_SYSTEM)\n",
            "from dbus.bus import BUS_STARTER\nx = BUS_STARTER\n",
            "import dbus.bus\nc = dbus.bus.BusConnection()\n",
            "class M:\n    def f(self, other):\n        return self._connect(other)\n",
            "import dbus.bus\nclass M:\n    def f(self):\n        return dbus.bus.BusConnection(address_or_type=self.socket_path)\n",
            "import dbus\nc = dbus.Bus(dbus.Bus.TYPE_SESSION)\n",
        ):
            with self.subTest(source=source):
                self.flagged(source, USER_BUS, "b2")
        # near-misses that pass
        self.clean("import dbus.bus\nclass M:\n    def f(self):\n        return dbus.bus.BusConnection(str(self.address))\n", USER_BUS)
        self.clean("class M:\n    def f(self):\n        return self._connect(str(self.address))\n", USER_BUS)
        self.clean("import dbus.bus\nclass M:\n    def f(self):\n        return dbus.bus.BusConnection(self.address)\n", USER_BUS)

    def test_c_operator_adapter(self) -> None:
        name = OPERATOR_NAMES[0]
        self.flagged(f"from tools.persona_installer import {name}\n", ENGINE, "c")
        self.flagged(f"import tools.persona_installer as p\nm = p.{name}(run)\n", ENGINE, "c")
        self.flagged("from tools.persona_installer import LocalHostBackend\n", OTHER, "c")
        self.clean("from tools.persona_installer import InstallOperation\n", ENGINE)

    def test_d_child_processes(self) -> None:
        self.flagged("import subprocess\n", USER_BUS, "d")
        self.flagged("from subprocess import run\n", OTHER, "d")
        self.flagged(
            "import subprocess\nclass E:\n    def f(self):\n        subprocess.run(['x', '--user'])\n",
            ENGINE, "d",
        )
        self.flagged(
            "import subprocess\nclass E:\n    def f(self):\n        subprocess.run([self._python], shell=True)\n",
            ENGINE, "d",
        )
        self.flagged(
            "class E:\n    def a(self):\n        return ['x']\n"
            "    def f(self):\n        argv = self.a()\n        self._run_child(argv)\n",
            ENGINE, "d",
        )
        self.clean(
            "import subprocess\nclass E:\n    def a(self):\n        return [self._python, 'm.py']\n"
            "    def f(self):\n        argv = self.a()\n        self._run_child(argv, stdin=subprocess.DEVNULL)\n",
            ENGINE,
        )
        self.clean(
            "import subprocess\nclass E:\n    def f(self):\n        subprocess.run([self._python, '-c', 'pass'])\n",
            ENGINE,
        )

    # T-6375: the admitted tmux probe. The allowed shape is the near-miss; every
    # variant below changes exactly one thing and must be flagged.
    TMUX_OK = (
        "import subprocess\n"
        "from tools.persona_installer_activation import seat_tmux_socket\n"
        "DEFAULT_TMUX: str = '/usr/bin/tmux'\n"
        "class E:\n"
        "    def __init__(self, tmux=DEFAULT_TMUX):\n"
        "        self._tmux = tmux\n"
        "    def probe(self, session):\n"
        "        socket = seat_tmux_socket(self._identity.uid)\n"
        "        return self._run_child(\n"
        "            [self._tmux, '-S', socket, 'has-session', '-t', f'={session}'],\n"
        "            check=False, timeout=5.0, stdin=subprocess.DEVNULL,\n"
        "            env={'PATH': '/usr/bin', 'HOME': '/h', 'LANG': 'C.UTF-8'},\n"
        "        )\n"
    )

    def tmux_variant(self, old: str, new: str) -> str:
        self.assertIn(old, self.TMUX_OK)
        return self.TMUX_OK.replace(old, new, 1)

    def test_d_tmux_probe_allowed_shape_passes(self) -> None:
        self.clean(self.TMUX_OK, ENGINE)
        # inline socket call is the same derivation
        self.clean(
            self.tmux_variant("socket = seat_tmux_socket(self._identity.uid)\n        return", "return")
            .replace("'-S', socket,", "'-S', seat_tmux_socket(self._identity.uid),"),
            ENGINE,
        )

    def test_d_tmux_probe_negative_controls(self) -> None:
        variants = {
            "another tmux verb": self.tmux_variant("'has-session'", "'kill-server'"),
            "a non-literal verb": self.tmux_variant("'has-session'", "verb"),
            "relative binary": self.tmux_variant("[self._tmux, '-S'", "['tmux', '-S'"),
            "PATH-looked-up default": self.tmux_variant("'/usr/bin/tmux'", "'tmux'"),
            "no tmux seam": self.tmux_variant("self._tmux = tmux", "self._tmux = 'x'"),
            "late rebind of self._tmux": self.TMUX_OK + "    def late(self):\n        self._tmux = shutil.which('tmux')\n",
            "augmented rebind of self._tmux": self.TMUX_OK + "    def late(self):\n        self._tmux += 'x'\n",
            "annotated rebind of self._tmux": self.TMUX_OK + "    def late(self):\n        self._tmux: str = 'tmux'\n",
            "tuple rebind of self._tmux": self.TMUX_OK + "    def late(self):\n        self._tmux, y = 'tmux', 1\n",
            "setattr of _tmux": self.TMUX_OK + "    def late(self):\n        setattr(self, '_tmux', 'tmux')\n",
            "second __init__ assignment of self._tmux": self.tmux_variant(
                "self._tmux = tmux\n", "self._tmux = tmux\n        self._tmux = shutil.which('tmux')\n"
            ),
            "plain rebind of the tmux parameter": self.tmux_variant(
                "self._tmux = tmux", "tmux = 'tmux'\n        self._tmux = tmux"
            ),
            "tmux parameter from __import__ which": self.tmux_variant(
                "self._tmux = tmux", "tmux = __import__('shutil').which('tmux')\n        self._tmux = tmux"
            ),
            "augmented rebind of the tmux parameter": self.tmux_variant(
                "self._tmux = tmux", "tmux += 'x'\n        self._tmux = tmux"
            ),
            "walrus rebind of the tmux parameter": self.tmux_variant(
                "self._tmux = tmux", "(tmux := 'tmux')\n        self._tmux = tmux"
            ),
            "del tmux": self.tmux_variant("self._tmux = tmux", "del tmux\n        self._tmux = tmux"),
            "nested nonlocal rebind of the tmux parameter": self.tmux_variant(
                "self._tmux = tmux",
                "def inner():\n            nonlocal tmux\n            tmux = 'tmux'\n        inner()\n        self._tmux = tmux",
            ),
            "for-rebound tmux parameter": self.tmux_variant(
                "self._tmux = tmux", "for tmux in ('tmux',):\n            pass\n        self._tmux = tmux"
            ),
            "tmux parameter from os.environ": self.tmux_variant(
                "self._tmux = tmux", "tmux = os.environ['TMUX_BIN']\n        self._tmux = tmux"
            ),
            "annotated rebind of the tmux parameter": self.tmux_variant(
                "self._tmux = tmux", "tmux: str = 'tmux'\n        self._tmux = tmux"
            ),
            "with-rebound tmux parameter": self.tmux_variant(
                "self._tmux = tmux", "with open('x') as tmux:\n            pass\n        self._tmux = tmux"
            ),
            "tuple rebind of the tmux parameter": self.tmux_variant(
                "self._tmux = tmux", "tmux, other = 'tmux', 1\n        self._tmux = tmux"
            ),
            "del tmux then rebind": self.tmux_variant(
                "self._tmux = tmux", "del tmux\n        tmux = 'tmux'\n        self._tmux = tmux"
            ),
            "global tmux": self.tmux_variant("self._tmux = tmux", "global tmux\n        self._tmux = tmux"),
            "comprehension-rebound tmux parameter": self.tmux_variant(
                "self._tmux = tmux", "[0 for tmux in ('tmux',)]\n        self._tmux = tmux"
            ),
            "default constant rebound non-constantly": self.tmux_variant(
                "DEFAULT_TMUX: str = '/usr/bin/tmux'\n",
                "DEFAULT_TMUX: str = '/usr/bin/tmux'\nDEFAULT_TMUX = __import__('shutil').which('tmux')\n",
            ),
            "default constant bound twice to constants": self.tmux_variant(
                "DEFAULT_TMUX: str = '/usr/bin/tmux'\n", "DEFAULT_TMUX: str = '/usr/bin/tmux'\nDEFAULT_TMUX = 'tmux'\n"
            ),
            "timeout name bound to a None constant": self.tmux_variant(
                "timeout=5.0", "timeout=NO_TIMEOUT"
            ).replace("import subprocess\n", "import subprocess\nNO_TIMEOUT = None\n", 1),
            "seat_tmux_socket function-local lambda": self.tmux_variant(
                "return self._run_child(", "seat_tmux_socket = lambda uid: os.environ['X']\n        return self._run_child("
            ),
            "seat_tmux_socket later aliasing import": self.TMUX_OK
            + "from tools.elsewhere import other as seat_tmux_socket\n",
            "seat_tmux_socket function-local aliasing import": self.tmux_variant(
                "return self._run_child(", "from x import other as seat_tmux_socket\n        return self._run_child("
            ),
            "seat_tmux_socket global reassignment": self.TMUX_OK
            + "def swap():\n    global seat_tmux_socket\n    seat_tmux_socket = lambda uid: 'x'\n",
            "seat_tmux_socket shadowed by a class": self.TMUX_OK + "class seat_tmux_socket:\n    pass\n",
            "timeout=None": self.tmux_variant("timeout=5.0", "timeout=None"),
            "socket augmented from the environment": self.tmux_variant(
                "return self._run_child(", "socket += os.environ['X']\n        return self._run_child("
            ),
            "socket annotated rebind": self.tmux_variant(
                "return self._run_child(", "socket: str = os.environ['X']\n        return self._run_child("
            ),
            "socket walrus rebind": self.tmux_variant(
                "return self._run_child(", "(socket := os.environ['X'])\n        return self._run_child("
            ),
            "socket for-rebound": self.tmux_variant(
                "return self._run_child(", "for socket in os.environ:\n            pass\n        return self._run_child("
            ),
            "socket with-rebound": self.tmux_variant(
                "return self._run_child(", "with open('x') as socket:\n            pass\n        return self._run_child("
            ),
            "socket tuple-rebound": self.tmux_variant(
                "return self._run_child(", "socket, other = os.environ['X'], 1\n        return self._run_child("
            ),
            "socket is a parameter": self.tmux_variant("def probe(self, session)", "def probe(self, session, socket)").replace(
                "        socket = seat_tmux_socket(self._identity.uid)\n", ""
            ),
            "direct subprocess.run of the tmux argv": self.tmux_variant("self._run_child(", "subprocess.run("),
            "direct subprocess.Popen of the tmux argv": self.tmux_variant("self._run_child(", "subprocess.Popen("),
            "seat_tmux_socket shadowed by a local def": self.TMUX_OK + "def seat_tmux_socket(uid):\n    return os.environ['X']\n",
            "seat_tmux_socket not imported": self.tmux_variant(
                "from tools.persona_installer_activation import seat_tmux_socket\n", ""
            ),
            "seat_tmux_socket imported from elsewhere": self.tmux_variant(
                "tools.persona_installer_activation", "tools.elsewhere"
            ),
            "shell=True": self.tmux_variant("check=False,", "check=False, shell=True,"),
            "env-derived socket": self.tmux_variant(
                "seat_tmux_socket(self._identity.uid)", "os.environ['TMUX_SOCK']"
            ),
            "socket from another uid": self.tmux_variant("self._identity.uid", "self._caller.uid"),
            "extra argv element": self.tmux_variant("f'={session}']", "f'={session}', '-F', 'x']"),
            "stdin inherited": self.tmux_variant("stdin=subprocess.DEVNULL", "stdin=None"),
            "no timeout": self.tmux_variant("timeout=5.0, ", ""),
            "inherited env": self.tmux_variant("env={'PATH': '/usr/bin', 'HOME': '/h', 'LANG': 'C.UTF-8'}", "env=os.environ"),
            "env splat": self.tmux_variant("'LANG': 'C.UTF-8'}", "'LANG': 'C.UTF-8', **os.environ}"),
            "env names TMUX_TMPDIR": self.tmux_variant("'LANG': 'C.UTF-8'}", "'LANG': 'C.UTF-8', 'TMUX_TMPDIR': '/x'}"),
            "env names TMUX": self.tmux_variant("'LANG': 'C.UTF-8'}", "'LANG': 'C.UTF-8', 'TMUX': 'x'}"),
        }
        for label, source in variants.items():
            with self.subTest(label):
                self.flagged(source, ENGINE, "d")
        # a second call site, even an otherwise identical one
        second = self.TMUX_OK + (
            "    def probe2(self, session):\n"
            "        return self._run_child(\n"
            "            [self._tmux, '-S', seat_tmux_socket(self._identity.uid), 'has-session', '-t', f'={session}'],\n"
            "            check=False, timeout=5.0, stdin=subprocess.DEVNULL, env={'PATH': '/usr/bin'},\n"
            "        )\n"
        )
        self.flagged(second, ENGINE, "d")
        # the tmux child is the engine's alone
        self.flagged(self.TMUX_OK, OTHER, "d")

    def test_d_tmux_amendment_leaves_the_python_child_rule_intact(self) -> None:
        self.flagged(
            "import subprocess\nclass E:\n    def f(self):\n        subprocess.run(['x', '--user'])\n", ENGINE, "d"
        )
        self.flagged(
            "import subprocess\nclass E:\n    def f(self):\n        subprocess.run([self._tmuxx, 'a'])\n", ENGINE, "d"
        )

    def test_e_environment_reads_in_user_bus(self) -> None:
        self.flagged("import os\naddress = os.environ['X']\n", USER_BUS, "e")
        self.flagged("import os\naddress = os.getenv('X')\n", USER_BUS, "e")
        self.flagged("from os import environ\n", USER_BUS, "e")
        self.clean("import os\ninfo = os.stat('/x')\n", USER_BUS)
        self.clean("import os\nx = os.environ.get('Y')\n", ENGINE)

    def test_d7_bare_manager_in_tests(self) -> None:
        bad = "m = DBusUserManager(uid=1000)\n"
        self.assertTrue(scan_d7(bad, "tools/test_x.py"))
        self.assertTrue(scan_d7("import x\nm = x.DBusUserManager(uid=1000)\n", "tools/test_x.py"))
        self.assertEqual(scan_d7("m = DBusUserManager(uid=1000, runtime_root=root)\n", "tools/test_x.py"), [])
        # review L2(b): connect= without runtime_root= is flagged
        self.assertTrue(scan_d7("m = DBusUserManager(uid=1000, connect=fake)\n", "tools/test_x.py"))
        self.assertEqual(
            scan_d7("m = DBusUserManager(uid=1000, runtime_root=root, connect=fake)\n", "tools/test_x.py"), []
        )


if __name__ == "__main__":
    unittest.main()
