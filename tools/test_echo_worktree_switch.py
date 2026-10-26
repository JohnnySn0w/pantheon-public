"""Tests for tools/echo_worktree_switch.py (T-5152). Run: python3 -m unittest tools.test_echo_worktree_switch

The real cargo behaviour was reproduced live 2026-10-20 (two copies of a one-crate
workspace on one target: the older copy printed the newer copy's string under bare
cargo, and its own after this tool ran). These tests cover the tool's own logic.
"""
from __future__ import annotations

import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import tools.echo_worktree_switch as ews

OLD = time.time() - 7200


class SwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(os.path.realpath(self._tmp.name))
        self.target = base / "target"
        self.target.mkdir()
        self.a, self.b = self._worktree(base / "A"), self._worktree(base / "B")

    def _worktree(self, root: Path) -> Path:
        for rel in ("core/src/lib.rs", "core/tests/w.rs", "core/build.rs", "app/src/main.rs"):
            f = root / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("//\n")
            os.utime(f, (OLD, OLD))
        return root

    def meta(self, root: Path) -> dict:
        def tgt(rel, kind):
            return {"kind": [kind], "src_path": str(root / rel)}
        return {
            "workspace_root": str(root),
            "workspace_members": ["core 0.1.0", "app 0.1.0"],
            "packages": [
                {"id": "core 0.1.0", "targets": [tgt("core/src/lib.rs", "lib"), tgt("core/tests/w.rs", "test"),
                                                 tgt("core/build.rs", "custom-build")]},
                {"id": "app 0.1.0", "targets": [tgt("app/src/main.rs", "bin")]},
                {"id": "serde 1.0.0", "targets": [{"kind": ["lib"], "src_path": "/registry/serde/src/lib.rs"}]},
            ],
        }

    def mtime(self, p: Path) -> float:
        return os.stat(p).st_mtime

    def test_first_build_touches_roots_but_never_build_rs(self) -> None:
        prev, n = ews.switch(self.target, self.meta(self.a))
        self.assertEqual((prev, n), ("", 3))
        self.assertGreater(self.mtime(self.a / "core/src/lib.rs"), OLD + 60)
        self.assertGreater(self.mtime(self.a / "app/src/main.rs"), OLD + 60)
        self.assertAlmostEqual(self.mtime(self.a / "core/build.rs"), OLD, delta=1)
        self.assertEqual((self.target / ews.MARKER).read_text().strip(), str(self.a))

    def test_same_worktree_again_touches_nothing(self) -> None:
        ews.switch(self.target, self.meta(self.a))
        os.utime(self.a / "core/src/lib.rs", (OLD, OLD))
        self.assertEqual(ews.switch(self.target, self.meta(self.a)), (str(self.a), 0))
        self.assertAlmostEqual(self.mtime(self.a / "core/src/lib.rs"), OLD, delta=1)

    def test_switching_worktree_touches_only_the_new_one(self) -> None:
        ews.switch(self.target, self.meta(self.a))
        os.utime(self.a / "core/src/lib.rs", (OLD, OLD))
        self.assertEqual(ews.switch(self.target, self.meta(self.b)), (str(self.a), 3))
        self.assertGreater(self.mtime(self.b / "core/src/lib.rs"), OLD + 60)
        self.assertAlmostEqual(self.mtime(self.a / "core/src/lib.rs"), OLD, delta=1)
        self.assertEqual((self.target / ews.MARKER).read_text().strip(), str(self.b))

    def test_force_touches_even_for_the_same_worktree(self) -> None:
        ews.switch(self.target, self.meta(self.a))
        self.assertEqual(ews.switch(self.target, self.meta(self.a), force=True)[1], 3)

    def test_untouchable_file_is_reported_not_raised(self) -> None:
        with mock.patch.object(ews.os, "utime", side_effect=PermissionError("EPERM")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ews.switch(self.target, self.meta(self.a))[1], 0)
        self.assertIn("cannot touch", err.getvalue())

    def test_cli_fails_open_when_cargo_metadata_fails(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = ews.main(["--target", str(self.target), "--cargo", "/nonexistent/cargo"])
        self.assertEqual(rc, 0)
        self.assertIn("skipped", err.getvalue())
        self.assertFalse((self.target / ews.MARKER).exists())


if __name__ == "__main__":
    unittest.main()
