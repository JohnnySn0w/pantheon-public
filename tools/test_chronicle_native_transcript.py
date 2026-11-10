"""T-6115 D-3 / T-6118 contract: archive a session's native transcript, compressed, into Chronicle.

Intent (the operator TG 12979): "Chronicle is concerned with saving the session transcript." Before a
codex thread is rotated, its rollout is archived raw-but-compressed beside the session's HANDOFF
snapshot, and the session record's native_transcript points at it with path, bytes and sha256.
Contract (archivist, T-6118): write-temp-then-rename (never in place), 0640 group pantheon-shared
from the start (hardlinked into the memex export later), source kept (rollback needs it).

Hermetic: tmp chronicle root and tmp source files only.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import chronicle


class NativeTranscriptArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "chronicle"
        self.src = Path(self.tmp.name) / "rollout-x.jsonl"
        self.src.write_bytes(b'{"type":"session_meta"}\n' + b'{"a":"' + b"x" * 200_000 + b'"}\n')
        self.ts = "2026-11-03T22:00:00Z"

    def archive(self, **kw):
        return chronicle.archive_native_transcript(
            "codex", self.src, jsonl_uuid="01a155c8-f81e-7901-a589-9421139d0423", ts=self.ts,
            root=self.root, group=kw.pop("group", None), **kw)

    def test_writes_a_gzip_beside_the_snapshot_and_reports_path_bytes_sha256(self) -> None:
        out = self.archive()
        p = Path(out["path"])
        self.assertTrue(p.is_file())
        self.assertEqual(p.parent, self.root / "codex" / "2026-11-03")
        self.assertTrue(p.name.endswith(".transcript.jsonl.gz"))
        self.assertIn("2026-11-03T220000Z-01a155c8", p.name)
        data = p.read_bytes()
        self.assertEqual(out["bytes"], len(data))
        self.assertEqual(out["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(gzip.decompress(data), self.src.read_bytes())  # raw, not scrubbed
        self.assertEqual(out["source_bytes"], self.src.stat().st_size)

    def test_source_bytes_and_sha_describe_what_was_compressed_when_the_source_grows(self) -> None:
        self.src.write_bytes(b"y" * (3 << 19))  # 1.5 MiB: more than one 1 MiB read block
        real_write = gzip.GzipFile.write
        grown = []

        def growing_write(gz_self, data):
            if not grown:
                grown.append(True)
                with open(self.src, "ab") as fh:
                    fh.write(b"APPENDED-DURING-COPY\n" * 50)
            return real_write(gz_self, data)

        with mock.patch.object(gzip.GzipFile, "write", growing_write):
            out = self.archive()
        self.assertTrue(grown)
        fed = gzip.decompress(Path(out["path"]).read_bytes())
        self.assertGreater(len(fed), 3 << 19)  # the growth really was copied
        self.assertEqual(out["source_bytes"], len(fed))
        self.assertEqual(out["source_sha256"], hashlib.sha256(fed).hexdigest())

    def test_source_is_kept(self) -> None:
        self.archive()
        self.assertTrue(self.src.exists())

    def test_mode_is_0640(self) -> None:
        p = Path(self.archive()["path"])
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o640)

    def test_group_is_applied_when_given(self) -> None:
        gid = os.getgid()
        name = __import__("grp").getgrgid(gid).gr_name
        p = Path(self.archive(group=name)["path"])
        self.assertEqual(p.stat().st_gid, gid)

    def test_unknown_group_raises_and_leaves_no_file(self) -> None:
        with self.assertRaises(chronicle.ChronicleDurabilityError):
            self.archive(group="no-such-group-t6115")
        self.assertEqual(list(self.root.rglob("*.gz")), [])

    def test_write_temp_then_rename_never_in_place(self) -> None:
        # The final name must never be opened for writing: only a temp sibling is, then renamed.
        opened: list[str] = []
        real_open = open

        def spy(path, mode="r", *a, **k):
            if "w" in mode or "a" in mode:
                opened.append(str(path))
            return real_open(path, mode, *a, **k)
        with mock.patch("builtins.open", side_effect=spy):
            out = self.archive()
        self.assertTrue(opened, "the archive must be written through open()")
        self.assertNotIn(out["path"], opened)
        self.assertTrue(all(Path(o).parent == Path(out["path"]).parent for o in opened))

    def test_never_overwrites_an_existing_archive(self) -> None:
        a = self.archive()
        b = self.archive()
        self.assertNotEqual(a["path"], b["path"])
        self.assertTrue(Path(a["path"]).exists() and Path(b["path"]).exists())

    def test_default_compresslevel_is_9(self) -> None:
        with mock.patch("gzip.GzipFile", wraps=gzip.GzipFile) as spy:
            self.archive()
        self.assertEqual(spy.call_args.kwargs["compresslevel"], 9)

    def test_explicit_compresslevel_is_passed_through_and_round_trips(self) -> None:
        with mock.patch("gzip.GzipFile", wraps=gzip.GzipFile) as spy:
            out = self.archive(compresslevel=6)
        self.assertEqual(spy.call_args.kwargs["compresslevel"], 6)
        self.assertEqual(gzip.decompress(Path(out["path"]).read_bytes()), self.src.read_bytes())

    def test_missing_or_empty_source_raises(self) -> None:
        self.src.write_bytes(b"")
        with self.assertRaises(chronicle.ChronicleDurabilityError):
            self.archive()
        self.src.unlink()
        with self.assertRaises(chronicle.ChronicleDurabilityError):
            self.archive()

    def test_failure_mid_write_leaves_no_partial_or_temp(self) -> None:
        with mock.patch("tools.chronicle.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(chronicle.ChronicleDurabilityError):
                self.archive()
        self.assertEqual([p for p in self.root.rglob("*") if p.is_file()], [])


if __name__ == "__main__":
    unittest.main()
