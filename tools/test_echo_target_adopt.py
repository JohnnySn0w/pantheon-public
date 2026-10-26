"""Tests for tools/echo_target_adopt.py (T-4071). Run: python3 -m unittest tools.test_echo_target_adopt

Ownership cannot be varied without a second account, so "foreign" is simulated
through the my_uid seam (my_uid = real uid + 1 makes every file foreign). The
real cross-user mechanism was verified live (suborch2 over a claude-owned file).
"""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import tools.echo_target_adopt as eta

OLD = time.time() - 3600
FOREIGN = os.getuid() + 1


class AdoptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.t = Path(self._tmp.name)

    def _file(self, rel: str, data: bytes = b"x", mode: int = 0o644, mtime: float = OLD) -> Path:
        p = self.t / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.chmod(p, mode)
        os.utime(p, (mtime, mtime))
        return p

    def test_adopts_known_copier_preserving_bytes_mode_and_mtime(self) -> None:
        f = self._file("debug/build/openssl-sys-0123456789abcdef/out/include/ssl.h", b"hdr", 0o644)
        ino = f.stat().st_ino
        t = eta.adopt(self.t, set(eta.KNOWN_COPIERS), my_uid=FOREIGN)
        st = f.stat()
        self.assertEqual(t.adopted, 1)
        self.assertNotEqual(st.st_ino, ino, "must be a NEW inode (owned by the adopter)")
        self.assertEqual(f.read_bytes(), b"hdr")
        self.assertEqual(st.st_mode & 0o777, 0o644)
        self.assertAlmostEqual(st.st_mtime, OLD, places=3)   # cargo freshness untouched
        self.assertEqual([p.name for p in f.parent.iterdir()], ["ssl.h"], "no temp left behind")

    def test_default_scope_skips_non_copiers(self) -> None:
        f = self._file("debug/build/serde-0123456789abcdef/out/private.rs")
        ino = f.stat().st_ino
        eta.adopt(self.t, set(eta.KNOWN_COPIERS), my_uid=FOREIGN)
        self.assertEqual(f.stat().st_ino, ino, "read-reused codegen OUT dirs are left alone")

    def test_all_scope_and_cross_compile_layout(self) -> None:
        a = self._file("debug/build/serde-0123456789abcdef/out/private.rs")
        b = self._file("x86_64-unknown-linux-gnu/release/build/ring-0123456789abcdef/out/lib.a")
        inos = (a.stat().st_ino, b.stat().st_ino)
        t = eta.adopt(self.t, None, my_uid=FOREIGN)
        self.assertEqual(t.adopted, 2)
        self.assertNotEqual(a.stat().st_ino, inos[0])
        self.assertNotEqual(b.stat().st_ino, inos[1])

    def test_own_files_untouched(self) -> None:
        f = self._file("debug/build/ring-0123456789abcdef/out/x.o")
        ino = f.stat().st_ino
        t = eta.adopt(self.t, None)   # real uid: the file is ours
        self.assertEqual((t.adopted, f.stat().st_ino), (0, ino))

    def test_young_files_skipped(self) -> None:
        f = self._file("debug/build/ring-0123456789abcdef/out/x.o", mtime=time.time())
        ino = f.stat().st_ino
        t = eta.adopt(self.t, None, my_uid=FOREIGN)
        self.assertEqual((t.adopted, t.young, f.stat().st_ino), (0, 1, ino))

    def test_symlinks_skipped(self) -> None:
        real = self._file("debug/build/ring-0123456789abcdef/out/real.h")
        link = real.parent / "link.h"
        link.symlink_to("real.h")
        t = eta.adopt(self.t, None, my_uid=FOREIGN)
        self.assertTrue(link.is_symlink())
        self.assertEqual(t.adopted, 1, "only the regular file")

    def test_dry_run_changes_nothing(self) -> None:
        f = self._file("debug/build/ring-0123456789abcdef/out/x.o", b"12345")
        ino = f.stat().st_ino
        t = eta.adopt(self.t, None, my_uid=FOREIGN, dry_run=True)
        self.assertEqual((t.adopted, t.bytes, f.stat().st_ino), (1, 5, ino))

    def test_file_rewritten_mid_copy_is_left_alone(self) -> None:
        f = self._file("debug/build/ring-0123456789abcdef/out/x.o", b"old")
        real_copy2 = shutil.copy2

        def racing_copy2(src, dst, **kw):
            real_copy2(src, dst, **kw)
            Path(src).write_bytes(b"peer's fresh output")   # a peer build writes meanwhile
        with patch.object(eta.shutil, "copy2", side_effect=racing_copy2):
            t = eta.adopt(self.t, None, my_uid=FOREIGN)
        self.assertEqual((t.adopted, t.changed), (0, 1))
        self.assertEqual(f.read_bytes(), b"peer's fresh output", "the peer's newer bytes win")
        self.assertEqual([p.name for p in f.parent.iterdir()], ["x.o"], "temp removed")

    def test_per_file_error_is_reported_not_raised(self) -> None:
        self._file("debug/build/ring-0123456789abcdef/out/x.o")
        with patch.object(eta.os, "replace", side_effect=PermissionError("EPERM")):
            t = eta.adopt(self.t, None, my_uid=FOREIGN)
        self.assertEqual(len(t.errors), 1)

    def test_cli_exit_codes(self) -> None:
        self._file("debug/build/ring-0123456789abcdef/out/x.o")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(eta.main(["--target", str(self.t), "--dry-run"]), 0)
        self.assertIn("would adopt 0 file(s)", out.getvalue())   # own file under real uid
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(eta.main(["--target", str(self.t / "missing")]), 2)


    # --- T-4955: adopt never lands inside a cargo build (holds the profile's .cargo-lock)

    def _hold_lock(self, profile: str = "debug") -> int:
        lock = self.t / profile / ".cargo-lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.touch()
        fd = os.open(lock, os.O_RDWR)
        self.addCleanup(os.close, fd)
        eta.fcntl.flock(fd, eta.fcntl.LOCK_EX)
        return fd

    def test_held_cargo_lock_skips_profile_and_adopts_nothing_T4955(self) -> None:
        f = self._file("debug/build/ring-0123456789abcdef/out/x.o")
        before = os.stat(f).st_ino
        fd = self._hold_lock()
        t = eta.adopt(self.t, None, my_uid=FOREIGN, lock_timeout=0.3)
        self.assertEqual((t.adopted, t.locked_out), (0, [str(self.t / "debug")]))
        self.assertEqual(os.stat(f).st_ino, before)
        eta.fcntl.flock(fd, eta.fcntl.LOCK_UN)
        t = eta.adopt(self.t, None, my_uid=FOREIGN, lock_timeout=0.3)
        self.assertEqual((t.adopted, t.locked_out), (1, []))
        self.assertNotEqual(os.stat(f).st_ino, before)

    def test_lock_is_per_profile_T4955(self) -> None:
        self._file("debug/build/ring-0123456789abcdef/out/x.o")
        self._file("release/build/ring-0123456789abcdef/out/x.o")
        (self.t / "release" / ".cargo-lock").touch()
        self._hold_lock("debug")
        t = eta.adopt(self.t, None, my_uid=FOREIGN, lock_timeout=0.3)
        self.assertEqual((t.adopted, t.locked_out), (1, [str(self.t / "debug")]))

    def test_dry_run_ignores_held_lock_T4955(self) -> None:
        self._file("debug/build/ring-0123456789abcdef/out/x.o")
        self._hold_lock()
        t = eta.adopt(self.t, None, my_uid=FOREIGN, dry_run=True, lock_timeout=0.3)
        self.assertEqual((t.adopted, t.locked_out), (1, []))

    def test_cli_reports_locked_out_profile_and_exits_0_T4955(self) -> None:
        self._file("debug/build/ring-0123456789abcdef/out/x.o")
        self._hold_lock()
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(eta.main(["--target", str(self.t), "--lock-timeout", "0.3"]), 0)
        self.assertIn("SKIPPED", err.getvalue())


if __name__ == "__main__":
    unittest.main()
