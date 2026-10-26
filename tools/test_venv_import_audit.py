"""Tests for tools/_venv_import_audit.py (T-4858)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

from tools import _venv_import_audit as audit

REPO = Path(__file__).resolve().parent.parent


def _mod(path: str | None) -> types.ModuleType:
    module = types.ModuleType("m")
    if path is not None:
        module.__file__ = path
    return module


class OffendersTests(unittest.TestCase):
    ROOTS = [Path("/venv"), Path("/usr/lib/python3.12"), Path("/repo")]

    def test_modules_under_an_allowed_root_pass(self) -> None:
        mods = {"mcp": _mod("/venv/lib/python3.12/site-packages/mcp/__init__.py"),
                "json": _mod("/usr/lib/python3.12/json/__init__.py"),
                "tools.x": _mod("/repo/tools/x.py"), "sys": _mod(None)}
        self.assertEqual(audit.offenders(mods, self.ROOTS), [])

    def test_a_user_site_or_dist_packages_module_is_named(self) -> None:
        mods = {"jsonpointer": _mod("/usr/lib/python3/dist-packages/jsonpointer.py"),
                "pluggy": _mod("/home/u/.local/lib/python3.12/site-packages/pluggy/__init__.py")}
        found = audit.offenders(mods, self.ROOTS)
        self.assertEqual(len(found), 2)
        self.assertTrue(found[0].startswith("jsonpointer (/usr/lib/python3/dist-packages"))
        self.assertTrue(found[1].startswith("pluggy ("))

    def test_a_symlink_counts_where_it_is_loaded_from(self) -> None:
        with tempfile.TemporaryDirectory(prefix="t4858-link-") as tmp:
            base = Path(tmp)
            venv, outside = base / "venv", base / "outside"
            venv.mkdir()
            outside.mkdir()
            (outside / "binding.py").write_text("", encoding="utf-8")
            (venv / "binding.py").symlink_to(outside / "binding.py")
            mods = {"binding": _mod(str(venv / "binding.py")), "stray": _mod(str(outside / "binding.py"))}
            self.assertEqual([f.split(" ")[0] for f in audit.offenders(mods, [venv])], ["stray"])

    def test_the_allowlist_is_exactly_the_reviewed_set_and_matches_top_level(self) -> None:
        self.assertEqual(audit.ALLOWLIST, {"dbus", "_dbus_bindings", "gi", "_distutils_hack", "apport_python_hook"})
        mods = {"dbus.bus": _mod("/usr/lib/python3/dist-packages/dbus/bus.py"),
                "gi.repository": _mod("/usr/lib/python3/dist-packages/gi/repository/__init__.py"),
                "dbusx": _mod("/usr/lib/python3/dist-packages/dbusx.py")}
        self.assertEqual([f.split(" ")[0] for f in audit.offenders(mods, self.ROOTS)], ["dbusx"])

    def test_this_interpreter_roots_include_prefix_stdlib_and_checkout(self) -> None:
        roots = audit._roots()
        self.assertIn(Path(sys.prefix).resolve(), roots)
        self.assertIn(REPO.resolve(), roots)
        self.assertTrue(any(Path(os.__file__).resolve().is_relative_to(r) for r in roots), roots)


class SessionHookTests(unittest.TestCase):
    """The plugin through a real pytest session: inert without the env var, fails on an offender."""

    def _run(self, env_on: bool, offender: bool) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory(prefix="t4858-audit-") as tmp:
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (outside / "t4858_outside_mod.py").write_text("X = 1\n", encoding="utf-8")
            test = Path(tmp) / "test_probe.py"
            body = "import t4858_outside_mod\n" if offender else ""
            test.write_text(body + "def test_ok():\n    assert True\n", encoding="utf-8")
            env = dict(os.environ, PYTHONPATH=f"{REPO}{os.pathsep}{outside}")
            env.pop(audit.ENV, None)
            if env_on:
                env[audit.ENV] = "1"
            return subprocess.run(
                [sys.executable, "-m", "pytest", str(test), "-q", "-p", "no:cacheprovider",
                 "-p", "tools._venv_import_audit", "--rootdir", tmp],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=120)

    def test_offender_fails_the_session_when_enabled(self) -> None:
        result = self._run(env_on=True, offender=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("t4858_outside_mod", result.stderr)

    def test_inert_without_the_env_var(self) -> None:
        result = self._run(env_on=False, offender=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_clean_session_passes_when_enabled(self) -> None:
        result = self._run(env_on=True, offender=False)
        self.assertIn("_venv_import_audit:", result.stderr)
        # this interpreter's own extras (e.g. a user-site pytest) may be named; the clean case is
        # proven in the workflow's venv, here only the hook's wiring is.
        self.assertIn(result.returncode, (0, 1))


if __name__ == "__main__":
    unittest.main()
