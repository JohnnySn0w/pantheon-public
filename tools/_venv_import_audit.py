"""pytest plugin: fail the session if a module loaded from outside the venv (T-4858).

Used by the mcp-adapter-runtime workflow's hash-locked runtime step only:
``-p tools._venv_import_audit`` with ``PANTHEON_VENV_IMPORT_AUDIT=1``. That venv is isolated
but, when the runner's python is the lock's 3.12, carries symlinks to the apt dbus/gi bindings
(dbus-python must be importable in-process). This proves nothing ELSE leaked in: an UNLOCKED
system or user-site package satisfying an import would let the adapter tests pass on code the
production runtime (isolated, ``-I``) does not have (r2: a dist-packages jsonpointer did).

At session end every loaded module with a file must come from one of: the venv (sys.prefix),
the interpreter's standard library, this repo checkout, or a top-level package on ALLOWLIST
(the apt dbus/gi bindings the dbus tiers need, plus two distro import hooks). A path counts as
inside a root if it is there either as loaded or after resolving symlinks: the venv's binding
symlinks load from the venv, and a distro stdlib symlink (Ubuntu's sitecustomize.py ->
/etc/python3.X) is part of the interpreter. Anything else
sets the session's exit status to 1 and names the module and its path. Without the env var
the plugin does nothing, so loading it elsewhere is inert.

Scope: the test process only. Drivers the tests spawn are separate interpreters; the broker
drivers deliberately run under the system venv (PANTHEON_BROKER_PYTHON).
"""

from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path

ENV = "PANTHEON_VENV_IMPORT_AUDIT"
ALLOWLIST = frozenset({"dbus", "_dbus_bindings", "gi", "_distutils_hack", "apport_python_hook"})
REPO_ROOT = Path(__file__).resolve().parent.parent


def _roots() -> list[Path]:
    paths = sysconfig.get_paths()
    roots = [Path(sys.prefix), REPO_ROOT]
    for key in ("stdlib", "platstdlib"):
        if paths.get(key):
            roots.append(Path(paths[key]))
    out: list[Path] = []
    for root in roots:
        for form in (Path(os.path.abspath(root)), root.resolve()):
            if form not in out:
                out.append(form)
    return out


def offenders(modules: dict | None = None, roots: list[Path] | None = None,
              allowlist: frozenset[str] = ALLOWLIST) -> list[str]:
    """'name (path)' for every loaded module whose file is outside every allowed root and whose
    top-level package is not allowlisted. Sorted; [] = clean."""
    modules = sys.modules if modules is None else modules
    roots = _roots() if roots is None else roots
    out = []
    for name, module in list(modules.items()):
        origin = getattr(module, "__file__", None)
        if not origin:
            continue  # builtins, namespace packages
        if name.split(".", 1)[0] in allowlist:
            continue
        loaded, real = Path(os.path.abspath(origin)), Path(origin).resolve()
        if not any(form.is_relative_to(root) for form in (loaded, real) for root in roots):
            out.append(f"{name} ({real})")
    return sorted(out)


def pytest_sessionfinish(session, exitstatus):  # pragma: no cover - exercised via the workflow
    if os.environ.get(ENV) != "1":
        return
    found = offenders()
    if found:
        sys.stderr.write(f"\n_venv_import_audit: {len(found)} module(s) loaded from outside {sys.prefix}, "
                         "the stdlib and the checkout (not allowlisted):\n")
        for line in found:
            sys.stderr.write(f"  {line}\n")
        session.exitstatus = 1
    else:
        sys.stderr.write(f"\n_venv_import_audit: every loaded module came from the venv, the stdlib, "
                         "the checkout or the allowlist\n")
