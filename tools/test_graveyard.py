"""test_graveyard.py — durable HANDOFF archive (tools/graveyard.py, T-0766 Veneration).

Covers the archive→index→read cycle, day-chunking, predecessor selection (latest),
and the non-fatal contract (a bad write returns None, never raises — a restart must
never block on the graveyard).
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import graveyard as gy  # noqa: E402


class GraveyardTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "graveyard"

    def tearDown(self):
        self._tmp.cleanup()

    def _archive(self, agent, text, *, uuid, reason="ctx-fill", ts="2026-06-06T23:40:00Z"):
        return gy.archive(agent, text, jsonl_uuid=uuid, reason=reason, ts=ts, root=self.root)

    # ----------------------------------------------------------------- archive
    def test_archive_writes_snapshot_and_index(self):
        p = self._archive("recovery", "# Recovery Handoff\nBuilding the mailbox.", uuid="abc123")
        self.assertIsNotNone(p)
        self.assertTrue(p.exists())
        self.assertEqual(p.read_text(), "# Recovery Handoff\nBuilding the mailbox.")
        # day-chunked under <agent>/<YYYY-MM-DD>/
        self.assertEqual(p.parent.name, "2026-06-06")
        self.assertEqual(p.parent.parent.name, "recovery")
        # index line written
        idx = (self.root / "recovery" / "index.md").read_text()
        self.assertIn("abc123", idx)
        self.assertIn("ctx-fill", idx)
        self.assertIn("Recovery Handoff", idx)  # summary = first meaningful line, header-stripped

    # ------------------------------------------------------------ companion (T-1636)
    def test_companion_gzips_beside_snapshot_and_consumes_source(self):
        import gzip
        snap = self._archive("recovery", "# H\ncontent", uuid="abc123")
        src = Path(self._tmp.name) / "pane-timed-recovery.log"
        src.write_text("[2026-07-07 23:00:01] hello\n[2026-07-07 23:00:02] world\n")
        dest = gy.archive_companion("recovery", src, jsonl_uuid="abc123",
                                    ts="2026-06-06T23:40:00Z", root=self.root)
        self.assertIsNotNone(dest)
        # Same day-dir + same <ts>-<uuid> stem as the handoff snapshot, .pane.log.gz suffix.
        self.assertEqual(dest.parent, snap.parent)
        self.assertTrue(dest.name.startswith(snap.stem))
        self.assertTrue(dest.name.endswith(".pane.log.gz"))
        with gzip.open(dest, "rt") as fh:
            self.assertIn("hello", fh.read())
        # Move semantics: the live source is consumed so the successor starts fresh.
        self.assertFalse(src.exists())

    def test_companion_absent_or_empty_source_is_benign_none(self):
        self.assertIsNone(gy.archive_companion(
            "recovery", Path(self._tmp.name) / "nope.log", jsonl_uuid="u", ts="2026-06-06T00:00:00Z", root=self.root))
        empty = Path(self._tmp.name) / "empty.log"
        empty.write_text("")
        self.assertIsNone(gy.archive_companion(
            "recovery", empty, jsonl_uuid="u", ts="2026-06-06T00:00:00Z", root=self.root))
        self.assertTrue(empty.exists())  # never consumed on skip

    def test_companion_nonfatal_on_bad_root(self):
        src = Path(self._tmp.name) / "p.log"
        src.write_text("x\n")
        dest = gy.archive_companion("recovery", src, jsonl_uuid="u",
                                    ts="2026-06-06T00:00:00Z",
                                    root=Path("/proc/definitely/not/writable"))
        self.assertIsNone(dest)  # never raises
        self.assertTrue(src.exists())  # not consumed on failure

    def test_companion_never_overwrites_existing(self):
        src1 = Path(self._tmp.name) / "a.log"; src1.write_text("first\n")
        src2 = Path(self._tmp.name) / "b.log"; src2.write_text("second\n")
        d1 = gy.archive_companion("recovery", src1, jsonl_uuid="u", ts="2026-06-06T00:00:00Z", root=self.root)
        d2 = gy.archive_companion("recovery", src2, jsonl_uuid="u", ts="2026-06-06T00:00:00Z", root=self.root)
        self.assertIsNotNone(d1); self.assertIsNotNone(d2)
        self.assertNotEqual(d1, d2)
        self.assertTrue(d2.name.endswith(".pane.log.2.gz"))

    def test_companion_unlink_failure_still_counts_as_archived(self):
        from unittest import mock
        src = Path(self._tmp.name) / "c.log"; src.write_text("content\n")
        with mock.patch.object(type(src), "unlink", side_effect=OSError("perm")):
            dest = gy.archive_companion("recovery", src, jsonl_uuid="u",
                                        ts="2026-06-06T00:00:00Z", root=self.root)
        # Archived (dest returned) even though consumption failed — returning
        # None would trigger duplicate re-archival on every later restart.
        self.assertIsNotNone(dest)
        self.assertTrue(dest.exists())
        self.assertTrue(src.exists())

    def test_filename_is_fs_safe_and_sortable(self):
        p = self._archive("recovery", "x", uuid="u1", ts="2026-06-06T23:40:00Z")
        self.assertNotIn(":", p.name)
        self.assertTrue(p.name.startswith("2026-06-06T234000Z-"))

    def test_summary_strips_header_and_collapses(self):
        self.assertEqual(gy._one_line_summary("##  Hello   world  \nmore"), "Hello world")
        self.assertEqual(gy._one_line_summary("\n\n   \nFirst real line"), "First real line")
        self.assertEqual(gy._one_line_summary(""), "")

    # ----------------------------------------------------------- entries/latest
    def test_entries_parsed_in_order(self):
        self._archive("specialist", "h1", uuid="u1", ts="2026-06-06T10:00:00Z")
        self._archive("specialist", "h2", uuid="u2", ts="2026-06-06T11:00:00Z")
        rows = gy.entries("specialist", root=self.root)
        self.assertEqual([r["uuid"] for r in rows], ["u1", "u2"])
        self.assertEqual(rows[0]["reason"], "ctx-fill")

    def test_latest_is_predecessor(self):
        self._archive("orchestrator", "old", uuid="old", ts="2026-06-05T09:00:00Z")
        self._archive("orchestrator", "newest handoff", uuid="new", ts="2026-06-06T09:00:00Z")
        rec = gy.latest("orchestrator", root=self.root)
        self.assertEqual(rec["uuid"], "new")
        self.assertEqual(gy.latest_handoff_text("orchestrator", root=self.root), "newest handoff")

    def test_latest_across_day_dirs(self):
        # predecessor may live in a different day-chunk than today
        self._archive("recovery", "yday", uuid="y", ts="2026-06-05T23:59:00Z")
        self._archive("recovery", "today", uuid="t", ts="2026-06-06T00:01:00Z")
        self.assertEqual(gy.latest("recovery", root=self.root)["uuid"], "t")

    def test_empty_graveyard(self):
        self.assertEqual(gy.entries("nobody", root=self.root), [])
        self.assertIsNone(gy.latest("nobody", root=self.root))
        self.assertIsNone(gy.latest_handoff_text("nobody", root=self.root))

    def test_summary_with_pipes_does_not_break_parse(self):
        # a handoff summary containing '|' must not corrupt the 4-field split
        self._archive("recovery", "state: a | b | c pipes everywhere", uuid="pipe")
        rec = gy.latest("recovery", root=self.root)
        self.assertEqual(rec["uuid"], "pipe")
        self.assertIn("pipes everywhere", rec["summary"])

    def test_snapshot_path_roundtrips(self):
        p = self._archive("specialist", "body", uuid="rt", ts="2026-06-06T12:34:56Z")
        rec = gy.latest("specialist", root=self.root)
        self.assertEqual(gy.snapshot_path("specialist", rec, root=self.root), p)

    # --------------------------------------------------------------- non-fatal
    def test_archive_nonfatal_on_bad_root(self):
        # An unwritable/invalid root must return None, NEVER raise (restart must not block).
        bad = Path("/proc/cannot/write/here")
        self.assertIsNone(
            gy.archive("recovery", "x", jsonl_uuid="u", reason="r", ts="2026-06-06T00:00:00Z", root=bad)
        )

    def test_malformed_index_lines_skipped(self):
        self._archive("recovery", "good", uuid="g", ts="2026-06-06T01:00:00Z")
        with (self.root / "recovery" / "index.md").open("a") as fh:
            fh.write("not an index line\n- only | two\n")
        rows = gy.entries("recovery", root=self.root)
        self.assertEqual([r["uuid"] for r in rows], ["g"], "malformed lines skipped, good kept")


class CliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.root = self.dir / "graveyard"

    def tearDown(self):
        self._tmp.cleanup()

    def test_agent_from_handoff_path(self):
        self.assertEqual(gy.agent_from_handoff_path("/home/claude/HANDOFF.md"), "orchestrator")
        self.assertEqual(gy.agent_from_handoff_path("/home/claude-research/HANDOFF.md"), "specialist")
        self.assertEqual(gy.agent_from_handoff_path("/home/claude-fallback/HANDOFF.md"), "recovery")
        self.assertEqual(gy.agent_from_handoff_path("/home/the-ancestor/HANDOFF.md"), "ancestor")
        self.assertIsNone(gy.agent_from_handoff_path("/tmp/whatever.md"))

    def test_cli_archive_from_file(self):
        hf = self.dir / "HANDOFF.md"
        hf.write_text("# Specialist Handoff\nidle, 0 active")
        rc = gy.main(["archive", "--handoff-file", str(hf), "--reason", "ctx-fill",
                      "--ts", "2026-06-06T22:00:00Z", "--uuid", "u9",
                      "--agent", "specialist", "--root", str(self.root)])
        self.assertEqual(rc, 0)
        rec = gy.latest("specialist", root=self.root)
        self.assertEqual(rec["uuid"], "u9")
        self.assertEqual(gy.latest_handoff_text("specialist", root=self.root), "# Specialist Handoff\nidle, 0 active")

    def test_cli_archive_missing_handoff_nonfatal(self):
        rc = gy.main(["archive", "--handoff-file", "/no/such/HANDOFF.md",
                      "--reason", "r", "--agent", "recovery", "--root", str(self.root)])
        self.assertEqual(rc, 1)  # non-zero but caller (bash) guards it; no raise

    def test_cli_archive_unresolvable_agent(self):
        hf = self.dir / "HANDOFF.md"
        hf.write_text("body")
        rc = gy.main(["archive", "--handoff-file", str(hf), "--reason", "r",
                      "--root", str(self.root)])  # no --agent, path not under /home/<known>
        self.assertEqual(rc, 1)


class SessionUuidDetectTest(unittest.TestCase):
    """T-1030: detect the dying session's jsonl uuid (newest *.jsonl under the agent's
    Claude Code projects dir) so the graveyard index can map an entry → its session file.
    Strictly fail-safe — any miss falls back to the pre-T-1030 "unknown" default."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.projects = self.dir / ".claude" / "projects"  # what the restart hook passes
        self.slug = self.projects / "-home-claude-fallback"
        self.slug.mkdir(parents=True)
        self.root = self.dir / "graveyard"

    def tearDown(self):
        self._tmp.cleanup()

    def _jsonl(self, uuid, mtime):
        p = self.slug / f"{uuid}.jsonl"
        p.write_text("{}")
        os.utime(p, (mtime, mtime))
        return p

    def test_newest_jsonl_wins(self):
        self._jsonl("older-sess", 1000)
        self._jsonl("newest-sess", 2000)
        self._jsonl("middle-sess", 1500)
        # caller passes the projects root; rglob descends into the slug dir
        self.assertEqual(gy.detect_session_uuid(self.projects), "newest-sess")

    def test_missing_dir_returns_unknown(self):
        self.assertEqual(gy.detect_session_uuid(self.dir / "nope"), "unknown")

    def test_no_jsonls_returns_unknown(self):
        self.assertEqual(gy.detect_session_uuid(self.projects), "unknown")

    def test_none_returns_unknown(self):
        self.assertEqual(gy.detect_session_uuid(None), "unknown")

    def test_cli_detects_uuid_when_uuid_absent(self):
        self._jsonl("detected-uuid", 3000)
        hf = self.dir / "HANDOFF.md"
        hf.write_text("# Recovery Handoff\nctx-fill restart")
        rc = gy.main(["archive", "--handoff-file", str(hf), "--reason", "graceful_restart",
                      "--ts", "2026-06-09T15:00:00Z", "--agent", "recovery",
                      "--projects-dir", str(self.projects), "--root", str(self.root)])
        self.assertEqual(rc, 0)
        idx = (self.root / "recovery" / "index.md").read_text()
        self.assertIn("detected-uuid", idx)
        self.assertNotIn("unknown", idx)
        snap = next((self.root / "recovery" / "2026-06-09").glob("*.md"))
        self.assertTrue(snap.name.endswith("-detected-uuid.md"))

    def test_cli_explicit_uuid_overrides_detection(self):
        self._jsonl("detected-uuid", 3000)
        hf = self.dir / "HANDOFF.md"
        hf.write_text("# Recovery Handoff")
        rc = gy.main(["archive", "--handoff-file", str(hf), "--reason", "graceful_restart",
                      "--ts", "2026-06-09T15:00:00Z", "--agent", "recovery", "--uuid", "explicit-win",
                      "--projects-dir", str(self.projects), "--root", str(self.root)])
        self.assertEqual(rc, 0)
        idx = (self.root / "recovery" / "index.md").read_text()
        self.assertIn("explicit-win", idx)
        self.assertNotIn("detected-uuid", idx)


if __name__ == "__main__":
    unittest.main()
