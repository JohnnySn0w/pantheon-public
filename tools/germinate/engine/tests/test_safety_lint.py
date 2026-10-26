"""AST lint over the engine's own modules: no process, socket or bus reach, and
no production path literals outside the backend's denylist (T-5074 safety proof).

The engine imports persona_installer and host_control, which import subprocess
at module level; this lint covers the code the engine itself adds, which must
never call those reaches.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

GERMINATE = Path(__file__).resolve().parents[2]
SCANNED = sorted((GERMINATE / "engine").glob("*.py")) + [GERMINATE / "op_registry.py"]
BANNED_MODULES = ("subprocess", "socket", "ctypes", "dbus", "jeepney", "http", "urllib", "multiprocessing", "pty", "shutil")
BANNED_OS_CALLS = ("system", "popen", "fork", "forkpty", "kill", "killpg")
BANNED_OS_PREFIXES = ("exec", "spawn")
PATH_PREFIXES = ("/var/lib/pantheon", "/run/pantheon", "/opt/pantheon", "/etc", "/home/", "/proc", "/sys")
DENYLIST_OWNER = ("backend.py", "PRODUCTION_PREFIXES")


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    allowed_strings: set[int] = set()
    for node in ast.walk(tree):
        if (
            path.name == DENYLIST_OWNER[0]
            and isinstance(node, ast.AnnAssign | ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == DENYLIST_OWNER[1] for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))
        ):
            allowed_strings.update(id(n) for n in ast.walk(node))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in BANNED_MODULES:
                    found.append(f"{path.name}:{node.lineno} imports {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in BANNED_MODULES:
            found.append(f"{path.name}:{node.lineno} imports from {node.module}")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
            if node.attr in BANNED_OS_CALLS or node.attr.startswith(BANNED_OS_PREFIXES):
                found.append(f"{path.name}:{node.lineno} uses os.{node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in allowed_strings:
            if node.value.startswith(PATH_PREFIXES):
                found.append(f"{path.name}:{node.lineno} names production path {node.value!r}")
    return found


def test_lint_scans_the_engine():
    names = {p.name for p in SCANNED}
    assert {"backend.py", "executor.py", "cli.py", "run_state.py", "op_registry.py"} <= names


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: p.name)
def test_engine_module_is_clean(path):
    assert _violations(path) == []


def test_lint_catches_each_violation_class(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import subprocess\nfrom socket import socket\nimport os\nos.system('x')\nos.execv('a', [])\nP = '/var/lib/pantheon/x'\n",
        encoding="utf-8",
    )
    assert len(_violations(bad)) == 5


def test_denylist_exemption_is_scoped_to_its_assignment(tmp_path):
    fake = tmp_path / "backend.py"
    fake.write_text("PRODUCTION_PREFIXES = ('/etc',)\nOTHER = '/etc/x'\n", encoding="utf-8")
    assert _violations(fake) == ["backend.py:2 names production path '/etc/x'"]
