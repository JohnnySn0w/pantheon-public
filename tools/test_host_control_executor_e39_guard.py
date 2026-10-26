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
    ``self._python`` (``sys.executable``) seam; never ``shell=True``;
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
