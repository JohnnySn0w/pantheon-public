#!/usr/bin/env python3
"""T-5038: tests for tools/claude_exe_sync.py. Hermetic: a temp dest owned by
the running uid stands in for root's /opt, and an in-memory reader stands in
for `runuser -u <account> cat`."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import claude_exe_sync as ces

ELF = b"\x7fELF" + b"\0" * 60
UID, GID = os.getuid(), os.getgid()


class _Fake:
    def __init__(self, version: str = "2.3.4", exe: bytes = ELF) -> None:
        self.version, self.exe, self.calls = version, exe, []

    def __call__(self, account: str, path: Path) -> bytes:
        self.calls.append((account, path.name))
        if path.name == "package.json":
            return json.dumps({"version": self.version}).encode()
        return self.exe


class ClaudeExeSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        os.chmod(base, 0o755)
        self.dest = base / "claude"
        self.dest.mkdir(mode=0o755)
        self.home = base / "home"
        # bwrap maps root to the overflow uid; trust whatever owns the temp ancestors.
        self.trusted = frozenset(os.lstat(p).st_uid for p in self.dest.parents)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _sync(self, fake: _Fake, *, live=(), keep=2) -> dict:
        return ces.sync("acct", self.dest, account_home=self.home, reader=fake,
                        owner_uid=UID, owner_gid=GID, keep=keep, live=live,
                        trusted_uids=self.trusted)

    def test_first_sync_installs_owner_only_writable_copy_and_points_current(self) -> None:
        fake = _Fake()
        result = self._sync(fake)
        self.assertEqual(result, {"version": "2.3.4", "installed": True, "current_changed": True, "pruned": []})
        exe = self.dest / "2.3.4" / "claude.exe"
        self.assertEqual(exe.read_bytes(), ELF)
        st = exe.stat()
        self.assertEqual((st.st_uid, stat.S_IMODE(st.st_mode)), (UID, 0o755))
        self.assertEqual(stat.S_IMODE((self.dest / "2.3.4").stat().st_mode), 0o755)
        self.assertEqual(os.readlink(self.dest / "current"), "2.3.4")
        self.assertEqual((self.dest / "current" / "claude.exe").resolve(), exe.resolve())
        self.assertEqual({a for a, _ in fake.calls}, {"acct"})  # every read as the account

    def test_resync_of_same_version_is_a_noop_that_reads_no_exe(self) -> None:
        self._sync(_Fake())
        fake = _Fake()
        before = (self.dest / "2.3.4" / "claude.exe").stat().st_ino
        self.assertEqual(self._sync(fake), {"version": "2.3.4", "installed": False,
                                            "current_changed": False, "pruned": []})
        self.assertEqual([name for _, name in fake.calls], ["package.json"])
        self.assertEqual((self.dest / "2.3.4" / "claude.exe").stat().st_ino, before)

    def test_new_version_gets_its_own_dir_and_never_replaces_the_old_inode(self) -> None:
        self._sync(_Fake("2.3.4"))
        old_ino = (self.dest / "2.3.4" / "claude.exe").stat().st_ino
        result = self._sync(_Fake("2.3.5", ELF + b"new"))
        self.assertTrue(result["installed"] and result["current_changed"])
        self.assertEqual((self.dest / "2.3.4" / "claude.exe").stat().st_ino, old_ino)
        self.assertEqual(os.readlink(self.dest / "current"), "2.3.5")

    def test_prune_keeps_current_newest_and_live_versions(self) -> None:
        for i, v in enumerate(("1.0.0", "1.0.1", "1.0.2", "1.0.3")):
            self._sync(_Fake(v), keep=99)
            os.utime(self.dest / v, (1000 + i, 1000 + i))
        live = {str(self.dest / "1.0.0" / "claude.exe")}  # an old version a seat still runs
        result = self._sync(_Fake("1.0.3"), live=live, keep=2)
        self.assertEqual(result["pruned"], ["1.0.1"])
        self.assertEqual(sorted(p.name for p in ces.version_dirs(self.dest)), ["1.0.0", "1.0.2", "1.0.3"])

    def test_list_names_versioned_paths_never_current(self) -> None:
        self._sync(_Fake("1.0.0"))
        self.assertEqual(ces.list_exe_paths(self.dest), [str(self.dest / "1.0.0" / "claude.exe")])

    def test_refuses_bad_version_strings(self) -> None:
        for bad in ("../../etc", "1.0", "latest", "", None, "1.0.0/../x"):
            with self.subTest(bad=bad), self.assertRaises(ces.SyncError):
                self._sync(_Fake(bad))
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_refuses_non_elf_payload(self) -> None:
        with self.assertRaisesRegex(ces.SyncError, "non-ELF"):
            self._sync(_Fake(exe=b"#!/bin/sh\nexec bash\n"))
        self.assertFalse((self.dest / "2.3.4" / "claude.exe").exists())

    def test_refuses_writable_or_symlinked_dest(self) -> None:
        os.chmod(self.dest, 0o777)
        with self.assertRaisesRegex(ces.SyncError, "writable"):
            self._sync(_Fake())
        os.chmod(self.dest, 0o755)
        link = self.dest.parent / "link"
        link.symlink_to(self.dest)
        with self.assertRaisesRegex(ces.SyncError, "real directory"):
            ces.sync("acct", link, account_home=self.home, reader=_Fake(),
                     owner_uid=UID, owner_gid=GID, live=(), trusted_uids=self.trusted)

    def test_refuses_dest_not_owned_by_owner(self) -> None:
        with self.assertRaisesRegex(ces.SyncError, "owned by uid"):
            ces.sync("acct", self.dest, account_home=self.home, reader=_Fake(),
                     owner_uid=UID + 4242, owner_gid=GID, live=(), trusted_uids=self.trusted - {UID})

    def test_main_refuses_non_root(self) -> None:
        with mock.patch.object(ces.os, "geteuid", return_value=1001), \
                mock.patch("sys.stderr"):
            self.assertEqual(ces.main(["--account", "acct", "--dest", str(self.dest)]), 2)


class VersionAndReaderHardeningTests(unittest.TestCase):
    """orchestrator review findings 2 and 4."""

    def test_version_grammar_is_a_fullmatch(self) -> None:
        for good in ("2.1.89", "1.0.0-beta.1", "10.20.30+build.7"):
            with self.subTest(good=good):
                self.assertTrue(ces.VERSION_RE.fullmatch(good))
        for bad in ("1.2.3\n", "1.2.3-..", "1.2.3-a..b", "1.2.3-", "1.2.3+", "1.2.3-.a", " 1.2.3", "1.2.3/x"):
            with self.subTest(bad=bad):
                self.assertIsNone(ces.VERSION_RE.fullmatch(bad))
                with self.assertRaises(ces.SyncError):
                    ces.read_version("acct", Path("/p"), lambda a, p: json.dumps({"version": bad}).encode())

    def _stat(self, out: str, rc: int = 0) -> mock.Mock:
        return mock.Mock(returncode=rc, stdout=out.encode(), stderr=b"")

    def test_reader_refuses_non_regular_files_before_reading(self) -> None:
        for out in ("character special file|0", "fifo|0", "directory|4096", "|"):
            with self.subTest(out=out), \
                    mock.patch.object(ces.subprocess, "run", return_value=self._stat(out)), \
                    mock.patch.object(ces.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(ces.SyncError, "regular file"):
                    ces.runuser_reader("acct", Path("/x"))
                popen.assert_not_called()

    def test_reader_refuses_an_oversized_file_before_reading(self) -> None:
        with mock.patch.object(ces.subprocess, "run", return_value=self._stat("regular file|11")), \
                mock.patch.object(ces.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ces.SyncError, "larger than"):
                ces.runuser_reader("acct", Path("/x"), limit=10)
        popen.assert_not_called()

    def test_reader_stops_at_the_limit_when_the_file_grows(self) -> None:
        proc = mock.MagicMock()
        proc.stdout.read.side_effect = [b"x" * 8, b"x" * 8, b"x" * 8, b""]
        popen = mock.MagicMock()
        popen.return_value.__enter__.return_value = proc
        with mock.patch.object(ces.subprocess, "run", return_value=self._stat("regular file|5")), \
                mock.patch.object(ces.subprocess, "Popen", popen):
            with self.assertRaisesRegex(ces.SyncError, "grew past"):
                ces.runuser_reader("acct", Path("/x"), limit=10)
        proc.kill.assert_called_once()
        self.assertEqual(proc.stdout.read.call_count, 2)  # never read past the limit

    def test_reader_runs_both_steps_as_the_account(self) -> None:
        proc = mock.MagicMock()
        proc.stdout.read.side_effect = [b"abc", b""]
        proc.wait.return_value = 0
        popen = mock.MagicMock()
        popen.return_value.__enter__.return_value = proc
        with mock.patch.object(ces.subprocess, "run", return_value=self._stat("regular file|3")) as run, \
                mock.patch.object(ces.subprocess, "Popen", popen):
            self.assertEqual(ces.runuser_reader("acct", Path("/p/q")), b"abc")
        self.assertEqual(run.call_args.args[0][:4], ["runuser", "-u", "acct", "--"])
        self.assertEqual(popen.call_args.args[0], ["runuser", "-u", "acct", "--", "cat", "--", "/p/q"])


_LIVE_SHAPE = (
    '{\n  "targets": {\n    "claude-fallback": {\n      "account": "claude-fallback",\n'
    '      "uid": 1001,\n      "tmux_session": "claude-telegram-bot-fallback",\n'
    '      "user_unit": "pantheon-runtime-controller@recovery.service",\n'
    '      "foreground_exe_allowlist": [\n        "/home/x/claude.exe"\n      ]\n    }\n  },\n'
    '  "invokers": {\n    "codex-fallback": {\n      "verbs": ["kill-session", "unstick", "selftest", "lease"]\n'
    '    },\n    "claude-fallback": {\n      "verbs": ["lease"]\n    }\n  }\n}\n'
)


class AllowlistRegenerationTests(unittest.TestCase):
    """orchestrator's T-5038 constraints 1-5 for regenerating the unstick allowlist."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        os.chmod(base, 0o755)
        self.dest = base / "claude"
        self.dest.mkdir(mode=0o755)
        self.home = base / "home"
        self.etc = base / "etc"
        self.etc.mkdir(mode=0o755)
        self.manifest = self.etc / "manifest.json"
        self.manifest.write_text(_LIVE_SHAPE)
        os.chmod(self.manifest, 0o644)
        self.state = base / "state"
        self.trusted = frozenset(os.lstat(p).st_uid for p in self.dest.parents)
        self.validated: list[Path] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _validate(self, path: Path) -> None:
        self.validated.append(path)
        json.loads(path.read_text())  # the real broker loader is used in production

    def _rebaseline(self) -> dict:
        return ces.rebaseline(self.manifest, state_dir=self.state, validate=self._validate,
                              manifest_owner_uid=UID, manifest_owner_gid=GID)

    def _sync(self, version: str = "2.3.4", *, live=(), validate=None) -> dict:
        return ces.sync("claude-fallback", self.dest, account_home=self.home, reader=_Fake(version),
                        owner_uid=UID, owner_gid=GID, live=live, trusted_uids=self.trusted,
                        manifest=self.manifest, target_key="claude-fallback", state_dir=self.state,
                        validate=validate or self._validate)

    def _audit(self) -> list[dict]:
        path = self.state / ces.AUDIT_NAME
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def test_without_a_baseline_sync_fails_closed_before_touching_disk(self) -> None:
        with self.assertRaisesRegex(ces.SyncError, "rebaseline"):
            self._sync()
        self.assertEqual(list(self.dest.iterdir()), [])
        self.assertEqual(self.manifest.read_text(), _LIVE_SHAPE)

    def test_rebaseline_canonicalises_records_the_shadow_and_audits(self) -> None:
        event = self._rebaseline()
        raw = self.manifest.read_bytes()
        self.assertEqual(raw, ces.canonical(json.loads(_LIVE_SHAPE)))
        self.assertEqual(json.loads(raw), json.loads(_LIVE_SHAPE))  # same JSON
        self.assertEqual((self.state / ces.SHADOW_NAME).read_bytes(), raw)
        self.assertEqual(stat.S_IMODE(self.manifest.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual([e["op"] for e in self._audit()], ["rebaseline"])
        self.assertEqual(event["sha256_after"], ces._sha(raw))

    def test_c1_only_the_target_allowlist_changes(self) -> None:
        self._rebaseline()
        before = json.loads(self.manifest.read_text())
        result = self._sync()
        self.assertTrue(result["allowlist_changed"])
        after = json.loads(self.manifest.read_text())
        exe = str(self.dest / "2.3.4" / "claude.exe")
        self.assertEqual(after["targets"]["claude-fallback"]["foreground_exe_allowlist"], [exe])
        after["targets"]["claude-fallback"]["foreground_exe_allowlist"] = ["/home/x/claude.exe"]
        self.assertEqual(after, before)
        # Byte-level: put the old list back and the bytes are the old bytes.
        self.assertEqual(ces.canonical(after), (self.state / ces.SHADOW_NAME).read_bytes()
                         .replace(exe.encode(), b"/home/x/claude.exe"))

    def test_c1_a_hand_edit_since_the_last_root_write_fails_closed_before_any_change(self) -> None:
        self._rebaseline()
        edited = self.manifest.read_text().replace('"uid": 1001', '"uid": 1002', 1)
        self.assertNotEqual(edited, self.manifest.read_text())
        self.manifest.write_text(edited)
        with self.assertRaisesRegex(ces.SyncError, "changed since the last root write"):
            self._sync()
        self.assertEqual(self.manifest.read_text(), edited)
        self.assertEqual(list(self.dest.iterdir()), [])
        self.assertEqual([e["op"] for e in self._audit()], ["rebaseline"])

    def test_c2_entries_are_versioned_eligible_paths_never_current(self) -> None:
        self._rebaseline()
        for v in ("1.0.0", "1.0.1", "1.0.2"):
            self._sync(v, live={str(self.dest / x / "claude.exe") for x in ("1.0.0", "1.0.1", "1.0.2")})
        os.chmod(self.dest / "1.0.1" / "claude.exe", 0o775)  # group-writable: not eligible
        self._sync("1.0.2", live={str(self.dest / x / "claude.exe") for x in ("1.0.0", "1.0.1", "1.0.2")})
        listed = json.loads(self.manifest.read_text())["targets"]["claude-fallback"]["foreground_exe_allowlist"]
        self.assertEqual(sorted(listed), [str(self.dest / v / "claude.exe") for v in ("1.0.0", "1.0.2")])
        self.assertFalse(any("current" in p for p in listed))

    def test_c3_validation_runs_on_the_temp_file_and_a_failure_leaves_the_manifest(self) -> None:
        self._rebaseline()
        good = self.manifest.read_bytes()
        self.validated.clear()

        def refuse(path: Path) -> None:
            self.validated.append(path)
            raise ces.SyncError("broker loader refused it")
        with self.assertRaisesRegex(ces.SyncError, "broker loader refused"):
            self._sync(validate=refuse)
        self.assertEqual(self.manifest.read_bytes(), good)
        self.assertEqual(len(self.validated), 1)
        self.assertNotEqual(self.validated[0], self.manifest)
        self.assertEqual(self.validated[0].parent, self.manifest.parent)
        self.assertEqual(sorted(p.name for p in self.etc.iterdir()), ["manifest.json"])  # temp cleaned

    def test_c4_each_change_is_audited_and_a_noop_is_not(self) -> None:
        self._rebaseline()
        self._sync("2.3.4")
        self._sync("2.3.4")  # nothing new
        self._sync("2.3.5", live={str(self.dest / "2.3.4" / "claude.exe")})
        events = [e for e in self._audit() if e["op"] == "allowlist"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["old"], ["/home/x/claude.exe"])
        self.assertEqual(events[0]["versions"], ["2.3.4"])
        self.assertEqual(events[1]["old"], events[0]["new"])
        self.assertEqual(sorted(events[1]["versions"]), ["2.3.4", "2.3.5"])
        self.assertEqual(events[1]["sha256_before"], events[0]["sha256_after"])
        self.assertEqual(stat.S_IMODE((self.state / ces.AUDIT_NAME).stat().st_mode), 0o600)

    def test_c5_a_listed_version_a_process_still_runs_is_kept(self) -> None:
        self._rebaseline()
        for v in ("1.0.0", "1.0.1", "1.0.2", "1.0.3"):
            self._sync(v, live={str(self.dest / "1.0.0" / "claude.exe")})
        names = sorted(p.name for p in ces.version_dirs(self.dest))
        self.assertEqual(names, ["1.0.0", "1.0.2", "1.0.3"])
        listed = json.loads(self.manifest.read_text())["targets"]["claude-fallback"]["foreground_exe_allowlist"]
        self.assertIn(str(self.dest / "1.0.0" / "claude.exe"), listed)
        self.assertNotIn(str(self.dest / "1.0.1" / "claude.exe"), listed)

    def test_t5114_non_target_account_syncs_and_skips_only_the_allowlist(self) -> None:
        # B14 S5 order: the seat's root copy must exist and track updates BEFORE
        # it becomes a broker target. A non-target sync copies, repoints, exits
        # clean and leaves the manifest and its shadow byte-identical.
        self._rebaseline()
        before = self.manifest.read_bytes()
        shadow = (self.state / ces.SHADOW_NAME).read_bytes()
        result = ces.sync("claude", self.dest, account_home=self.home, reader=_Fake("2.9.9"),
                          owner_uid=UID, owner_gid=GID, live=(), trusted_uids=self.trusted,
                          manifest=self.manifest, target_key="claude", state_dir=self.state,
                          validate=self._validate)
        self.assertEqual(result["allowlist_skipped"], "not a broker target yet")
        self.assertNotIn("allowlist_changed", result)
        self.assertTrue((self.dest / "2.9.9" / "claude.exe").exists())
        self.assertEqual(self.manifest.read_bytes(), before)
        self.assertEqual((self.state / ces.SHADOW_NAME).read_bytes(), shadow)
        self.assertEqual([e["op"] for e in self._audit()], ["rebaseline"])

    def test_t5114_non_target_still_fails_closed_on_a_drifted_manifest(self) -> None:
        self._rebaseline()
        self.manifest.write_text(self.manifest.read_text().replace('"uid": 1001', '"uid": 1002', 1))
        with self.assertRaisesRegex(ces.SyncError, "changed since the last root write"):
            ces.sync("claude", self.dest, account_home=self.home, reader=_Fake("2.9.9"),
                     owner_uid=UID, owner_gid=GID, live=(), trusted_uids=self.trusted,
                     manifest=self.manifest, target_key="claude", state_dir=self.state,
                     validate=self._validate)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_empty_allowlist_is_refused(self) -> None:
        self._rebaseline()
        with self.assertRaisesRegex(ces.SyncError, "empty"):
            ces.regenerate_allowlist(self.manifest, "claude-fallback", [], state_dir=self.state,
                                     validate=self._validate, manifest_owner_uid=UID, manifest_owner_gid=GID)

    def test_manifest_must_be_an_owned_0644_regular_file(self) -> None:
        self._rebaseline()
        os.chmod(self.manifest, 0o664)
        with self.assertRaisesRegex(ces.SyncError, "0o644"):
            self._sync()



class BrokerValidatorLoadsRealBrokerTests(unittest.TestCase):
    """The first live --rebaseline crashed: the real broker defines dataclasses,
    which need the module in sys.modules while it executes."""

    def test_loads_the_real_broker_module(self):
        broker = Path(__file__).resolve().parent / "codex_recovery.py"
        real = os.lstat(broker)
        fake = os.stat_result((real.st_mode & ~0o022, real.st_ino, real.st_dev, real.st_nlink,
                               0, 0, real.st_size, int(real.st_atime), int(real.st_mtime),
                               int(real.st_ctime)))
        with mock.patch.object(ces.os, "lstat", return_value=fake):
            validate = ces.broker_validator(broker)
        self.assertTrue(callable(validate))

    def test_validate_reaches_the_real_broker_loader(self):
        # End to end through the REAL load_broker_manifest. A temp manifest
        # owned by the test uid must be refused by the broker's own owner check;
        # a stub validator could never produce that error.
        broker = Path(__file__).resolve().parent / "codex_recovery.py"
        real = os.lstat(broker)
        fake = os.stat_result((real.st_mode & ~0o022, real.st_ino, real.st_dev, real.st_nlink,
                               0, 0, real.st_size, int(real.st_atime), int(real.st_mtime),
                               int(real.st_ctime)))
        with mock.patch.object(ces.os, "lstat", return_value=fake):
            validate = ces.broker_validator(broker)
        broker_module = sys.modules["_codex_recovery_broker"]
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o755)
            manifest = Path(tmp) / "manifest.json"
            manifest.write_text("{}")
            os.chmod(manifest, 0o644)
            with self.assertRaises(broker_module.ManifestValidationError) as caught:
                validate(manifest)
        self.assertIn("owner", str(caught.exception))

    def test_version_digits_are_ascii_only(self):
        for bad in ("\u0661.\u0662.\u0663", "1.2.\u0663", "\uff11.2.3"):
            with self.subTest(bad=bad):
                self.assertIsNone(ces.VERSION_RE.fullmatch(bad))

if __name__ == "__main__":
    unittest.main()
