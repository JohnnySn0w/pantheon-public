"""Tests for tools/echo_target_prune.py (T-4852) -- synthetic cargo target in a temp dir."""
import contextlib
import fcntl
import io
import os
import tempfile
import time
import unittest
from unittest import mock

from tools import echo_target_prune as ep

DAY = 86400
H_OLD, H_NEW, H_MIX, H_ORPH = "a" * 16, "b" * 16, "c" * 16, "d" * 16


def touch(path, content=b"x", age_days=0.0, atime_age_days=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    set_age(path, age_days, atime_age_days)


def set_age(path, age_days, atime_age_days=None):
    m = time.time() - age_days * DAY
    a = time.time() - (age_days if atime_age_days is None else atime_age_days) * DAY
    os.utime(path, (a, m))


def make_unit(profile, name, h, age_days, crate=None):
    crate = crate or name.replace("-", "_")
    touch(os.path.join(profile, ".fingerprint", "%s-%s" % (name, h), "lib-%s.json" % crate), age_days=age_days)
    touch(os.path.join(profile, "build", "%s-%s" % (name, h), "output"), b"cargo:rustc-cfg=x\n", age_days)
    touch(os.path.join(profile, "build", "%s-%s" % (name, h), "out", "gen.h"), age_days=age_days)
    touch(os.path.join(profile, "deps", "lib%s-%s.rlib" % (crate, h)), age_days=age_days)
    touch(os.path.join(profile, "deps", "lib%s-%s.rmeta" % (crate, h)), age_days=age_days)
    touch(os.path.join(profile, "deps", "%s-%s.d" % (crate, h)), age_days=age_days)


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = os.path.realpath(self.tmp.name)
        self.p = os.path.join(self.target, "debug")
        touch(os.path.join(self.p, ".cargo-lock"), b"")
        for patcher in (mock.patch.object(ep, "atime_supported", return_value=True),):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_prune(self, **kw):
        kw.setdefault("lock_timeout", 0.2)
        return ep.prune_target(self.target, log=lambda *_: None, **kw)

    def exists(self, *parts):
        return os.path.lexists(os.path.join(self.p, *parts))

    def test_stale_unit_removed_from_all_three_places_fresh_kept(self):
        make_unit(self.p, "serde", H_OLD, 30)
        make_unit(self.p, "tokio", H_NEW, 1)
        res = self.run_prune()
        self.assertEqual(res[0]["units"], 1)
        self.assertGreater(res[0]["freed"], 0)
        self.assertFalse(self.exists(".fingerprint", "serde-" + H_OLD))
        self.assertFalse(self.exists("build", "serde-" + H_OLD))
        self.assertFalse(self.exists("deps", "libserde-%s.rlib" % H_OLD))
        self.assertFalse(self.exists("deps", "serde-%s.d" % H_OLD))
        self.assertTrue(self.exists(".fingerprint", "tokio-" + H_NEW))
        self.assertTrue(self.exists("build", "tokio-" + H_NEW, "out", "gen.h"))
        self.assertTrue(self.exists("deps", "libtokio-%s.rlib" % H_NEW))

    def test_mixed_age_unit_kept_whole(self):
        make_unit(self.p, "mixed", H_MIX, 30)
        touch(os.path.join(self.p, "deps", "libmixed-%s.so" % H_MIX), age_days=0.5)
        self.run_prune()
        self.assertTrue(self.exists(".fingerprint", "mixed-" + H_MIX))
        self.assertTrue(self.exists("build", "mixed-" + H_MIX, "out", "gen.h"))
        self.assertTrue(self.exists("deps", "libmixed-%s.rlib" % H_MIX))

    def test_old_mtime_recent_atime_is_kept(self):
        make_unit(self.p, "stable", H_OLD, 60)
        rlib = os.path.join(self.p, "deps", "libstable-%s.rlib" % H_OLD)
        set_age(rlib, 60, atime_age_days=0.5)  # linked recently, never rewritten
        self.run_prune()
        self.assertTrue(self.exists(".fingerprint", "stable-" + H_OLD))
        self.assertTrue(self.exists("build", "stable-" + H_OLD, "out", "gen.h"))

    def test_old_mtime_old_atime_removed(self):
        make_unit(self.p, "stable", H_OLD, 60)
        self.run_prune()
        self.assertFalse(self.exists(".fingerprint", "stable-" + H_OLD))

    def test_unit_without_fingerprint_and_hashless_entries_kept(self):
        touch(os.path.join(self.p, "deps", "libghost-%s.rlib" % H_ORPH), age_days=60)
        touch(os.path.join(self.p, "deps", "README-notes"), age_days=60)
        make_unit(self.p, "serde", H_OLD, 30)
        self.run_prune()
        self.assertTrue(self.exists("deps", "libghost-%s.rlib" % H_ORPH))
        self.assertTrue(self.exists("deps", "README-notes"))

    def test_incremental_session_dirs(self):
        make_unit(self.p, "serde", H_NEW, 1)
        touch(os.path.join(self.p, "incremental", "crate-0abc", "s-1-x", "dep-graph.bin"), age_days=30)
        touch(os.path.join(self.p, "incremental", "crate-1def", "s-2-y", "dep-graph.bin"), age_days=1)
        # session dir with one fresh file inside an old tree is kept whole
        touch(os.path.join(self.p, "incremental", "crate-2ghi", "s-3-z", "old.bin"), age_days=30)
        touch(os.path.join(self.p, "incremental", "crate-2ghi", "s-3-z", "new.bin"), age_days=0.1)
        res = self.run_prune()
        self.assertEqual(res[0]["incremental"], 1)
        self.assertFalse(self.exists("incremental", "crate-0abc"))
        self.assertTrue(self.exists("incremental", "crate-1def"))
        self.assertTrue(self.exists("incremental", "crate-2ghi", "s-3-z", "old.bin"))

    def test_held_lock_skips_profile_and_deletes_nothing(self):
        make_unit(self.p, "serde", H_OLD, 30)
        fd = os.open(os.path.join(self.p, ".cargo-lock"), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        res = self.run_prune()
        self.assertTrue(res[0]["skipped"])
        self.assertTrue(self.exists(".fingerprint", "serde-" + H_OLD))
        fcntl.flock(fd, fcntl.LOCK_UN)
        res = self.run_prune()
        self.assertFalse(res[0]["skipped"])
        self.assertFalse(self.exists(".fingerprint", "serde-" + H_OLD))

    def test_postcheck_repairs_broken_build_unit(self):
        make_unit(self.p, "tauri", H_NEW, 1)  # fresh: survives the age pass
        out = os.path.join(self.p, "build", "tauri-" + H_NEW, "output")
        missing = os.path.join(self.p, "build", "tauri-" + H_NEW, "out", "perm-files")
        with open(out, "w") as f:
            f.write("cargo:rustc-env=X=%s\n" % missing)
        make_unit(self.p, "ok", H_MIX, 1)
        logs = []
        res = ep.prune_target(self.target, lock_timeout=0.2, log=logs.append)
        self.assertEqual(res[0]["repaired"], ["tauri-" + H_NEW])
        self.assertFalse(self.exists("build", "tauri-" + H_NEW))
        self.assertFalse(self.exists(".fingerprint", "tauri-" + H_NEW))
        self.assertTrue(self.exists("build", "ok-" + H_MIX))
        self.assertTrue(any(l.startswith("REPAIR") for l in logs))
        with mock.patch("sys.stdout"):
            self.assertEqual(ep.main(["--target", self.target]), 0)  # nothing left to repair

    def test_postcheck_ignores_log_lines_and_non_consumed_directives(self):
        # First live run (2026-10-10): aws-lc-sys warning lines and openssl-sys make
        # logs name intermediates the build script deletes itself; not breakage.
        make_unit(self.p, "aws-lc-sys", H_NEW, 1)
        b = os.path.join(self.p, "build", "aws-lc-sys-" + H_NEW)
        gone = os.path.join(b, "out", "out-c11", "x-c11.o")
        with open(os.path.join(b, "output"), "w") as f:
            f.write("cargo:warning=Compilation of 'c11.c' succeeded - Ok([\"%s\"])\n" % gone)
            f.write("running cd \"%s\" && \"make\"\n" % os.path.join(b, "out", "build", "src"))
            f.write("make[1]: Entering directory '%s'\n" % os.path.join(b, "out", "build", "src"))
            f.write("cargo:rerun-if-changed=%s\n" % gone)
            f.write("cargo::rerun-if-env-changed=CC\n")
            f.write("cargo:rustc-link-search=native=%s\n" % os.path.join(b, "out"))
        res = self.run_prune()
        self.assertEqual(res[0]["repaired"], [])
        self.assertTrue(self.exists(".fingerprint", "aws-lc-sys-" + H_NEW))
        self.assertTrue(self.exists("build", "aws-lc-sys-" + H_NEW, "out", "gen.h"))

    def test_postcheck_flags_consumed_directives_and_missing_out_dir(self):
        make_unit(self.p, "meta", H_NEW, 1)
        make_unit(self.p, "link", H_MIX, 1)
        make_unit(self.p, "libc", H_OLD, 1)
        bm = os.path.join(self.p, "build", "meta-" + H_NEW)
        with open(os.path.join(bm, "output"), "w") as f:
            f.write("cargo:include=%s\n" % os.path.join(bm, "out", "include"))  # links metadata
        bl = os.path.join(self.p, "build", "link-" + H_MIX)
        with open(os.path.join(bl, "output"), "w") as f:
            f.write("cargo::rustc-link-search=native=%s\n" % os.path.join(bl, "out", "lib"))
        bc = os.path.join(self.p, "build", "libc-" + H_OLD)
        with open(os.path.join(bc, "root-output"), "w") as f:
            f.write(os.path.join(bc, "out-missing"))
        res = self.run_prune()
        self.assertEqual(sorted(res[0]["repaired"]),
                         sorted(["meta-" + H_NEW, "link-" + H_MIX, "libc-" + H_OLD]))

    def test_reference_into_another_units_build_dir_is_not_breakage(self):
        # recovery 2026-10-10: aws-lc-rs's output points into aws-lc-sys's out/. With
        # aws-lc-sys's build/ gone, aws-lc-rs was "repaired" too; but rebuilding
        # aws-lc-sys (same hash) recreates that path, so aws-lc-rs is not broken.
        make_unit(self.p, "aws-lc-rs", H_MIX, 1)
        sibling_out = os.path.join(self.p, "build", "aws-lc-sys-" + H_NEW, "out")
        with open(os.path.join(self.p, "build", "aws-lc-rs-" + H_MIX, "output"), "w") as f:
            f.write("cargo:rustc-link-search=native=%s\n" % sibling_out)
        res = self.run_prune()
        self.assertEqual(res[0]["repaired"], [])
        self.assertTrue(self.exists(".fingerprint", "aws-lc-rs-" + H_MIX))

    def test_dry_run_previews_repair_without_deleting(self):
        make_unit(self.p, "tauri", H_NEW, 1)
        b = os.path.join(self.p, "build", "tauri-" + H_NEW)
        with open(os.path.join(b, "output"), "w") as f:
            f.write("cargo:rustc-env=X=%s\n" % os.path.join(b, "out", "perm-files"))
        logs = []
        res = ep.prune_target(self.target, dry_run=True, lock_timeout=0.2, log=logs.append)
        self.assertEqual(res[0]["repaired"], ["tauri-" + H_NEW])
        self.assertTrue(any(l.startswith("would REPAIR") for l in logs))
        self.assertTrue(self.exists(".fingerprint", "tauri-" + H_NEW))
        self.assertTrue(self.exists("build", "tauri-" + H_NEW))

    def test_dry_run_deletes_nothing_and_reports_bytes(self):
        make_unit(self.p, "serde", H_OLD, 30)
        touch(os.path.join(self.p, "incremental", "c-1", "f"), age_days=30)
        res = self.run_prune(dry_run=True)
        self.assertEqual((res[0]["units"], res[0]["incremental"]), (1, 1))
        self.assertGreater(res[0]["freed"], 0)
        self.assertTrue(self.exists(".fingerprint", "serde-" + H_OLD))
        self.assertTrue(self.exists("incremental", "c-1"))

    def test_noatime_filesystem_refused(self):
        make_unit(self.p, "serde", H_OLD, 30)
        with mock.patch.object(ep, "atime_supported", return_value=False):
            with self.assertRaises(ep.Refused):
                ep.prune_target(self.target, log=lambda *_: None)
            self.assertEqual(ep.main(["--target", self.target]), 2)
        self.assertTrue(self.exists(".fingerprint", "serde-" + H_OLD))

    def test_evidence_targets_refused(self):
        # Every target here has a REAL stale profile, so only the evidence guard can
        # refuse it (without the guard the stale unit would be pruned).
        make_unit(self.p, "serde", H_OLD, 30)
        sub = os.path.join(self.target, "sub", "debug")
        touch(os.path.join(sub, ".cargo-lock"), b"")
        make_unit(sub, "serde", H_OLD, 30)
        link = self.tmp.name + "-link"
        os.symlink(self.target, link)
        self.addCleanup(os.unlink, link)
        with mock.patch.object(ep, "EVIDENCE_TARGETS", self.target):
            self.assertEqual(ep.main(["--target", self.target]), 2)       # equal
            self.assertEqual(ep.main(["--target", os.path.join(self.target, "sub")]), 2)  # child
            self.assertEqual(ep.main(["--target", link]), 2)              # symlink to it
        with mock.patch.object(ep, "EVIDENCE_TARGETS", sub):
            self.assertEqual(ep.main(["--target", self.target]), 2)       # parent of it
        self.assertTrue(self.exists(".fingerprint", "serde-" + H_OLD))
        self.assertTrue(os.path.lexists(os.path.join(sub, ".fingerprint", "serde-" + H_OLD)))

    def test_deps_symlink_escape_is_not_followed(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        victim = os.path.join(outside.name, "libserde-%s.rlib" % H_OLD)
        touch(victim, age_days=60)
        make_unit(self.p, "serde", H_OLD, 30)
        for f in os.listdir(os.path.join(self.p, "deps")):
            os.unlink(os.path.join(self.p, "deps", f))
        os.rmdir(os.path.join(self.p, "deps"))
        os.symlink(outside.name, os.path.join(self.p, "deps"))
        res = self.run_prune()
        self.assertTrue(os.path.exists(victim))
        self.assertEqual(res[0]["errors"], 1)
        self.assertEqual(res[0]["units"], 0)

    def test_deps_file_belongs_to_trailing_hash_token_only(self):
        # Decoy: a fresh unit whose hash appears mid-name in the stale unit's file.
        make_unit(self.p, "decoy", H_NEW, 1)
        make_unit(self.p, "serde", H_OLD, 30)
        weird = os.path.join(self.p, "deps", "libserde-%s-%s.rlib" % (H_NEW, H_OLD))
        touch(weird, age_days=30)
        touch(os.path.join(self.p, "deps", "libdecoy-%s.so" % H_NEW), age_days=1)
        units = ep.collect_units(self.p)
        self.assertIn(weird, units[H_OLD]["paths"])
        self.assertNotIn(weird, units[H_NEW]["paths"])
        self.run_prune()
        self.assertFalse(os.path.lexists(weird))
        self.assertTrue(self.exists("deps", "libdecoy-%s.so" % H_NEW))
        self.assertTrue(self.exists(".fingerprint", "decoy-" + H_NEW))

    def test_ambiguous_or_overlong_hash_tokens_keep_the_file(self):
        make_unit(self.p, "serde", H_OLD, 30)
        two = os.path.join(self.p, "deps", "libserde-%s.tmp-%s.rlib" % (H_NEW, H_OLD))
        long_ = os.path.join(self.p, "deps", "libserde-%s0.rlib" % H_OLD)  # 17 hex chars
        touch(two, age_days=30)
        touch(long_, age_days=30)
        units = ep.collect_units(self.p)
        self.assertNotIn(two, units[H_OLD]["paths"])
        self.assertNotIn(long_, units[H_OLD]["paths"])
        self.run_prune()
        self.assertTrue(os.path.lexists(two))
        self.assertTrue(os.path.lexists(long_))
        self.assertFalse(self.exists("deps", "libserde-%s.rlib" % H_OLD))

    def test_deps_file_with_unrelated_hash_untouched(self):
        # A stale-unit hash appearing only as a non-trailing token must not claim the file.
        make_unit(self.p, "serde", H_OLD, 30)
        other = os.path.join(self.p, "deps", "libzzz-%s-%s.rlib" % (H_OLD, H_ORPH))
        touch(other, age_days=30)
        self.run_prune()
        self.assertTrue(os.path.lexists(other))

    def test_deletion_order_fingerprint_first_and_partial_failure_is_rebuild_safe(self):
        make_unit(self.p, "serde", H_OLD, 30)
        make_unit(self.p, "tokio", H_MIX, 30)
        paths = ep.collect_units(self.p)[H_OLD]["paths"]
        self.assertTrue(paths[0].startswith(os.path.join(self.p, ".fingerprint")))
        self.assertTrue(paths[1].startswith(os.path.join(self.p, "build")))
        real_remove, calls = ep.safe_remove, []

        def flaky(path, profile):
            calls.append(path)
            if "serde" in path and "/build/" in path:
                raise OSError("injected failure after fingerprint removal")
            return real_remove(path, profile)

        logs = []
        with mock.patch.object(ep, "safe_remove", side_effect=flaky):
            res = ep.prune_target(self.target, lock_timeout=0.2, log=logs.append)
        self.assertEqual(res[0]["errors"], 1)
        self.assertFalse(self.exists(".fingerprint", "serde-" + H_OLD))   # rebuild-safe state
        self.assertTrue(self.exists("build", "serde-" + H_OLD))           # outputs not yet gone
        self.assertTrue(self.exists("deps", "libserde-%s.rlib" % H_OLD))
        self.assertFalse(self.exists(".fingerprint", "tokio-" + H_MIX))   # next unit still pruned
        self.assertEqual(res[0]["units"], 1)
        self.assertTrue(any(l.startswith("ERROR") for l in logs))
        with mock.patch("sys.stdout"), mock.patch.object(ep, "safe_remove", side_effect=flaky):
            make_unit(self.p, "serde2", H_ORPH, 30)
            self.assertEqual(ep.main(["--target", self.target]), 3)

    def test_repair_order_fingerprint_first_and_partial_failure_is_rebuild_safe(self):
        make_unit(self.p, "tauri", H_NEW, 1)  # fresh: survives the age pass, fails post-check
        with open(os.path.join(self.p, "build", "tauri-" + H_NEW, "output"), "w") as f:
            f.write("cargo:rustc-env=X=%s\n" % os.path.join(self.p, "build", "tauri-" + H_NEW, "out", "gone"))
        real_remove = ep.safe_remove

        def flaky(path, profile):
            if os.sep + "build" + os.sep + "tauri-" in path:
                raise OSError("injected failure after fingerprint removal")
            return real_remove(path, profile)

        with mock.patch.object(ep, "safe_remove", side_effect=flaky):
            logs = []
            res = ep.prune_target(self.target, lock_timeout=0.2, log=logs.append)
            self.assertEqual(res[0]["errors"], 1)
            self.assertEqual(res[0]["repaired"], [])
            self.assertFalse(self.exists(".fingerprint", "tauri-" + H_NEW))  # rebuild-safe
            self.assertTrue(self.exists("build", "tauri-" + H_NEW))
            self.assertTrue(any(l.startswith("ERROR") for l in logs))
        # Second run: fingerprint gone so the unit is no longer a pruned/repaired unit;
        # re-create the failing state to assert the exit code end to end.
        touch(os.path.join(self.p, ".fingerprint", "tauri-%s" % H_NEW, "lib-tauri.json"), age_days=1)
        with mock.patch.object(ep, "safe_remove", side_effect=flaky), mock.patch("sys.stdout"):
            self.assertEqual(ep.main(["--target", self.target]), 3)
        self.assertFalse(self.exists(".fingerprint", "tauri-" + H_NEW))

    def test_days_floor(self):
        make_unit(self.p, "serde", H_OLD, 30)
        with self.assertRaises(ep.Refused):
            ep.prune_target(self.target, days=0.5, log=lambda *_: None)
        with mock.patch("sys.stderr"):
            self.assertEqual(ep.main(["--target", self.target, "--days", "0"]), 2)
        self.assertTrue(self.exists(".fingerprint", "serde-" + H_OLD))

    def test_missing_or_profileless_target_refused(self):
        self.assertEqual(ep.main(["--target", self.target + "/nope"]), 2)
        empty = os.path.join(self.target, "empty")
        os.makedirs(empty)
        self.assertEqual(ep.main(["--target", empty]), 2)

    def test_atime_probe_detects_refreshing_fs(self):
        # Real probe on the tmp fs: result must be a bool and must clean up after itself.
        mock.patch.stopall()
        before = set(os.listdir(self.target))
        self.assertIsInstance(ep.atime_supported(self.target), bool)
        self.assertEqual(set(os.listdir(self.target)), before)

    def test_windows_style_nested_profile_found(self):
        nested = os.path.join(self.target, "x86_64-pc-windows-msvc", "debug")
        touch(os.path.join(nested, ".cargo-lock"), b"")
        make_unit(nested, "serde", H_OLD, 30)
        res = self.run_prune()
        self.assertEqual(sorted(r["profile"] for r in res if r["units"]), [nested])

    # --- T-5249: superseded units (echo's own crates rebuilt under a new hash per commit)

    def _superseded_fixture(self):
        hr = 1 / 24.0
        make_unit(self.p, "echo-core", H_OLD, 5 * hr)    # superseded, idle 5h
        make_unit(self.p, "echo-core", H_MIX, 1 * hr)    # superseded, but used 1h ago
        make_unit(self.p, "echo-core", H_NEW, 0.1 * hr)  # newest of its name
        make_unit(self.p, "serde", H_ORPH, 5 * hr)       # idle 5h but nothing replaced it

    def test_superseded_is_off_by_default_T5249(self):
        self._superseded_fixture()
        res = self.run_prune()
        self.assertEqual(res[0]["units"], 0)
        self.assertTrue(self.exists(".fingerprint", "echo-core-" + H_OLD))

    def test_superseded_prunes_only_idle_older_same_name_units_T5249(self):
        self._superseded_fixture()
        res = self.run_prune(superseded_hours=2)
        self.assertEqual(res[0]["units"], 1)
        self.assertFalse(self.exists(".fingerprint", "echo-core-" + H_OLD))
        self.assertFalse(self.exists("build", "echo-core-" + H_OLD))
        self.assertFalse(self.exists("deps", "libecho_core-%s.rlib" % H_OLD))
        self.assertTrue(self.exists("deps", "libecho_core-%s.rlib" % H_MIX))
        self.assertTrue(self.exists("deps", "libecho_core-%s.rlib" % H_NEW))
        self.assertTrue(self.exists("deps", "libserde-%s.rlib" % H_ORPH))

    def test_superseded_never_prunes_the_newest_even_when_all_idle_T5249(self):
        make_unit(self.p, "echo", H_OLD, 0.5)
        make_unit(self.p, "echo", H_NEW, 0.4)
        self.run_prune(superseded_hours=2)
        self.assertFalse(self.exists(".fingerprint", "echo-" + H_OLD))
        self.assertTrue(self.exists(".fingerprint", "echo-" + H_NEW))

    def test_superseded_incremental_dirs_grouped_by_crate_T5249(self):
        make_unit(self.p, "serde", H_NEW, 0.01)
        hr = 1 / 24.0
        touch(os.path.join(self.p, "incremental", "echo_core-0abc", "s-1", "g.bin"), age_days=5 * hr)
        touch(os.path.join(self.p, "incremental", "echo_core-1def", "s-2", "g.bin"), age_days=0.1 * hr)
        touch(os.path.join(self.p, "incremental", "lonely-2ghi", "s-3", "g.bin"), age_days=5 * hr)
        res = self.run_prune(superseded_hours=2)
        self.assertEqual(res[0]["incremental"], 1)
        self.assertFalse(self.exists("incremental", "echo_core-0abc"))
        self.assertTrue(self.exists("incremental", "echo_core-1def"))
        self.assertTrue(self.exists("incremental", "lonely-2ghi"))

    def test_superseded_hours_floor_and_cli_T5249(self):
        self._superseded_fixture()
        with self.assertRaises(ep.Refused):
            ep.prune_target(self.target, superseded_hours=1, log=lambda *_: None)
        with mock.patch("sys.stderr"):
            self.assertEqual(ep.main(["--target", self.target, "--superseded-hours", "1"]), 2)
        self.assertTrue(self.exists(".fingerprint", "echo-core-" + H_OLD))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ep.main(["--target", self.target, "--superseded-hours", "2"]), 0)
        self.assertFalse(self.exists(".fingerprint", "echo-core-" + H_OLD))
        self.assertTrue(self.exists(".fingerprint", "echo-core-" + H_NEW))


class CapTest(unittest.TestCase):
    """--max-gb (T-6121): whole-profile clear, only when over the cap AND every lock is free."""

    MB = 1024 * 1024

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = os.path.realpath(self.tmp.name)
        self.p = os.path.join(self.target, "debug")
        touch(os.path.join(self.p, ".cargo-lock"), b"")
        patcher = mock.patch.object(ep, "atime_supported", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        # hermetic: the real host may be building right now (the backstop scans /proc)
        busy = mock.patch.object(ep, "cargo_process_running", return_value=None)
        busy.start()
        self.addCleanup(busy.stop)

    def run_prune(self, **kw):
        kw.setdefault("lock_timeout", 0.2)
        return ep.prune_target(self.target, log=lambda *_: None, **kw)

    def exists(self, *parts):
        return os.path.lexists(os.path.join(self.p, *parts))

    def _fill(self, profile, mb, name="serde", h=H_NEW):
        make_unit(profile, name, h, 1)  # fresh: the normal prune keeps it
        touch(os.path.join(profile, "deps", "big-%s.bin" % h), b"x" * (mb * self.MB), age_days=1)

    def _cap(self, mb):
        return mb / 1024.0

    def _hold(self, profile):
        fd = os.open(os.path.join(profile, ".cargo-lock"), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def _release_ok(self, profile):
        fd = ep.acquire_lock(profile, 0.2)
        self.assertIsNotNone(fd, "lock leaked: %s" % profile)
        os.close(fd)

    def test_cap_keeps_every_cargo_lock_file(self):
        # review of #1634: a live profile carries .cargo-lock (shared) plus
        # .cargo-build-lock / .cargo-artifact-lock (exclusive builder-vs-builder);
        # unlinking any of them swaps the inode under a waiting builder.
        for name in (".cargo-build-lock", ".cargo-artifact-lock"):
            touch(os.path.join(self.p, name), b"")
        self._fill(self.p, 3)
        out = self.run_prune(max_gb=self._cap(1))
        self.assertEqual(out.cap["status"], "cleared")
        self.assertEqual(sorted(os.listdir(self.p)),
                         [".cargo-artifact-lock", ".cargo-build-lock", ".cargo-lock"])

    def test_vanishing_file_during_the_size_walk_is_a_logged_skip_not_a_crash(self):
        self._fill(self.p, 3)
        with mock.patch.object(ep, "_target_bytes", side_effect=FileNotFoundError("gone")):
            out = self.run_prune(max_gb=self._cap(1))
        self.assertEqual(out.cap["status"], "skipped-measure")
        self.assertTrue(self.exists("deps"))

    def test_cli_exits_zero_when_the_size_walk_fails(self):
        self._fill(self.p, 3)
        with mock.patch.object(ep, "_target_bytes", side_effect=FileNotFoundError("gone")), \
             mock.patch("sys.stdout", new_callable=io.StringIO):
            rc = ep.main(["--target", self.target, "--max-gb", "0.001", "--lock-timeout", "0.2"])
        self.assertEqual(rc, 0)

    def test_a_running_cargo_or_rustc_process_skips_the_cap(self):
        self._fill(self.p, 3)
        with mock.patch.object(ep, "cargo_process_running", return_value="rustc (pid 42)"):
            out = self.run_prune(max_gb=self._cap(1))
        self.assertEqual(out.cap["status"], "skipped-busy")
        self.assertTrue(self.exists("deps"))
        self._release_ok(self.p)

    def test_cargo_process_running_reads_comm_under_a_proc_root(self):
        with tempfile.TemporaryDirectory() as proc:
            for pid, comm in (("1", "systemd"), ("7", "python3"), ("self", "x")):
                touch(os.path.join(proc, pid, "comm"), (comm + "\n").encode())
            self.assertIsNone(ep._scan_cargo(proc))
            touch(os.path.join(proc, "99", "comm"), b"cargo\n")
            self.assertIn("99", ep._scan_cargo(proc))
            os.makedirs(os.path.join(proc, "100"))   # vanished/unreadable comm: ignored
            self.assertIn("99", ep._scan_cargo(proc))

    def test_over_cap_and_idle_clears_every_profile_but_keeps_lock_file(self):
        rel = os.path.join(self.target, "release")
        touch(os.path.join(rel, ".cargo-lock"), b"")
        self._fill(self.p, 3)
        self._fill(rel, 3, "tokio", H_OLD)
        res = self.run_prune(max_gb=self._cap(2))
        self.assertEqual(res.cap["status"], "cleared")
        for prof in (self.p, rel):
            self.assertEqual(sorted(os.listdir(prof)), [".cargo-lock"])
            self._release_ok(prof)

    def test_over_cap_with_one_lock_held_deletes_nothing_and_releases_others(self):
        rel = os.path.join(self.target, "release")
        touch(os.path.join(rel, ".cargo-lock"), b"")
        self._fill(self.p, 3)
        self._fill(rel, 3, "tokio", H_OLD)
        self._hold(rel)
        logs = []
        res = ep.prune_target(self.target, lock_timeout=0.2, max_gb=self._cap(2), log=logs.append)
        self.assertEqual(res.cap["status"], "skipped-locked")
        self.assertTrue(self.exists("deps", "big-%s.bin" % H_NEW))
        self.assertTrue(os.path.exists(os.path.join(rel, "deps", "big-%s.bin" % H_OLD)))
        self._release_ok(self.p)
        self.assertTrue(any("cap" in l and "build running" in l for l in logs))

    def test_under_cap_does_nothing(self):
        self._fill(self.p, 1)
        res = self.run_prune(max_gb=1.0)
        self.assertEqual(res.cap["status"], "under-cap")
        self.assertTrue(self.exists("deps", "big-%s.bin" % H_NEW))

    def test_cap_off_by_default(self):
        self._fill(self.p, 3)
        res = self.run_prune()
        self.assertIsNone(res.cap)
        self.assertTrue(self.exists("deps", "big-%s.bin" % H_NEW))

    def test_noatime_refusal_unchanged_and_cap_not_attempted(self):
        self._fill(self.p, 3)
        with mock.patch.object(ep, "atime_supported", return_value=False):
            with self.assertRaises(ep.Refused):
                ep.prune_target(self.target, max_gb=0.001, log=lambda *_: None)
            with mock.patch("sys.stderr"):
                self.assertEqual(ep.main(["--target", self.target, "--max-gb", "0.001"]), 2)
        self.assertTrue(self.exists("deps", "big-%s.bin" % H_NEW))

    def test_dry_run_reports_but_deletes_nothing(self):
        self._fill(self.p, 3)
        res = self.run_prune(max_gb=self._cap(2), dry_run=True)
        self.assertEqual(res.cap["status"], "would-clear")
        self.assertTrue(self.exists("deps", "big-%s.bin" % H_NEW))
        self._release_ok(self.p)

    def test_dry_run_with_lock_held_reports_skip(self):
        self._fill(self.p, 3)
        self._hold(self.p)
        res = self.run_prune(max_gb=self._cap(2), dry_run=True)
        self.assertEqual(res.cap["status"], "skipped-locked")

    def test_size_is_measured_after_normal_prune(self):
        # stale unit is dropped by the normal prune, bringing the target under the cap
        make_unit(self.p, "old", H_OLD, 30)
        touch(os.path.join(self.p, "deps", "old-%s.bin" % H_OLD), b"x" * (3 * self.MB), age_days=30)
        self._fill(self.p, 1)
        res = self.run_prune(max_gb=self._cap(2))
        self.assertEqual(res.cap["status"], "under-cap")
        self.assertTrue(self.exists("deps", "big-%s.bin" % H_NEW))

    def test_cli_locked_over_cap_exits_zero_and_clear_exits_zero(self):
        self._fill(self.p, 3)
        fd = self._hold(self.p)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ep.main(["--target", self.target, "--max-gb", "0.001", "--lock-timeout", "0.2"]), 0)
        self.assertIn("cap", out.getvalue())
        self.assertTrue(self.exists("deps", "big-%s.bin" % H_NEW))
        fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ep.main(["--target", self.target, "--max-gb", "0.001"]), 0)
        self.assertEqual(sorted(os.listdir(self.p)), [".cargo-lock"])

    def test_cap_clear_lock_only_layout_is_valid_on_the_next_fire(self):
        self._fill(self.p, 3)
        args = ["--target", self.target, "--max-gb", "0.001", "--lock-timeout", "0.2"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ep.main(args), 0)
        self.assertEqual(sorted(os.listdir(self.p)), [".cargo-lock"])

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ep.main(args), 0)
        self.assertEqual(sorted(os.listdir(self.p)), [".cargo-lock"])

    def test_lock_only_profile_with_unrecognized_content_is_refused(self):
        touch(os.path.join(self.p, "not-a-cargo-lock"), b"do not touch")
        with mock.patch("sys.stderr"), mock.patch("sys.stdout"):
            self.assertEqual(ep.main(["--target", self.target, "--max-gb", "0.001"]), 2)
        self.assertTrue(self.exists("not-a-cargo-lock"))

    def test_cap_must_be_positive(self):
        with self.assertRaises(ep.Refused):
            ep.prune_target(self.target, max_gb=0, log=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
