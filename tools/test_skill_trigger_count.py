"""Tests for tools/skill_trigger_count.py (T-4782)."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools import skill_trigger_count as stc


def _rec(item, ts="2026-10-20T00:00:00Z", sid="s1"):
    rec = {"sessionId": sid, "message": {"content": [dict(item, type="tool_use")]}}
    if ts is not None:
        rec["timestamp"] = ts
    return json.dumps(rec)


def _skill(skill, tid, **kw):
    return _rec({"id": tid, "name": "Skill", "input": {"skill": skill}}, **kw)


def _read(path, tid, **kw):
    return _rec({"id": tid, "name": "Read", "input": {"file_path": path}}, **kw)


def _bash(cmd, tid, **kw):
    return _rec({"id": tid, "name": "Bash", "input": {"command": cmd}}, **kw)


class SkillTriggerCount(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = Path(self._td.name) / "projects"
        (self.root / "p").mkdir(parents=True)
        self.manifest = Path(self._td.name) / "sop-skills.yaml"
        self.manifest.write_text(
            "skills:\n"
            "  - slug: alpha\n    sop: sop/standard/alpha.md\n    description: a one\n"
            "  - slug: beta\n    sop: sop/standard/beta.md\n    description: beta desc\n"
            "  - slug: gamma\n    sop: sop/remediation/gamma.md\n    description: g\n",
            encoding="utf-8")
        self.paths = {"pantheon-sop-alpha": "sop/standard/alpha.md",
                      "pantheon-sop-beta": "sop/standard/beta.md",
                      "pantheon-sop-gamma": "sop/remediation/gamma.md"}

    def _write(self, name, lines):
        p = self.root / "p" / name
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def _run(self):
        out = io.StringIO()
        with redirect_stdout(out):
            stc.main([str(self.root), "--since", "2026-10-16T00:00:00", "--manifest", str(self.manifest)])
        return out.getvalue()

    def test_calls_reads_and_classes(self):
        self._write("a.jsonl", [
            _skill("pantheon-sop-alpha", "t1", sid="s1"),
            _skill("pantheon-sop-alpha", "t2", sid="s2"),
            _read("/home/x/pantheon-config/sop/standard/beta.md", "t3"),
            _bash("sed -n 1,40p sop/standard/beta.md", "t4"),
            _skill("claude-api", "t5"),
            _bash("echo Skill", "t6"),
            "not json at all \"tool_use\"",
        ])
        text = self._run()
        self.assertIn("pantheon-sop-alpha  [used]", text)
        self.assertIn("pantheon-sop-beta  [read-only: description-fix candidate]", text)
        self.assertIn("pantheon-sop-gamma  [PRUNE candidate (zero on both)]", text)
        self.assertIn("never_fired=2/3 prune_candidates=1/3 prune_desc_bytes=1", text)
        self.assertIn("claude-api  (not a rendered sop skill)", text)
        calls, _cs, reads, _rs = stc.tally([str(self.root)], "2026-10-16T00:00:00", self.paths)
        self.assertEqual(calls["pantheon-sop-alpha"], 2)
        self.assertEqual(reads["pantheon-sop-beta"], 2)

    def test_a_mirrored_tool_use_id_is_counted_once(self):
        """The same tool_use id twice (e.g. a timestamped mirror) counts once (dedup by id)."""
        self._write("m.jsonl", [_skill("pantheon-sop-alpha", "dup"), _skill("pantheon-sop-alpha", "dup")])
        calls, *_ = stc.tally([str(self.root)], "2026-10-16T00:00:00", self.paths)
        self.assertEqual(calls["pantheon-sop-alpha"], 1)

    def test_a_timestamp_less_blob_record_is_ignored(self):
        """api-request-blob mirrors carry no timestamp: never counted, even alone."""
        self._write("b.jsonl", [_skill("pantheon-sop-alpha", "blob", ts=None)])
        calls, *_ = stc.tally([str(self.root)], "2026-10-16T00:00:00", self.paths)
        self.assertEqual(calls.get("pantheon-sop-alpha", 0), 0)

    def test_calls_before_the_window_are_ignored(self):
        self._write("w.jsonl", [_skill("pantheon-sop-beta", "x", ts="2026-10-01T00:00:00Z"),
                                _read("sop/standard/beta.md", "y", ts="2026-10-01T00:00:00Z")])
        calls, _cs, reads, _rs = stc.tally([str(self.root)], "2026-10-16T00:00:00", self.paths)
        self.assertEqual((calls.get("pantheon-sop-beta", 0), reads.get("pantheon-sop-beta", 0)), (0, 0))

    def test_file_untouched_since_window_is_skipped(self):
        p = self._write("c.jsonl", [_skill("pantheon-sop-beta", "z")])
        old = 1_700_000_000  # 2023: before the window
        os.utime(p, (old, old))
        calls, *_ = stc.tally([str(self.root)], "2026-10-16T00:00:00", self.paths)
        self.assertEqual(calls.get("pantheon-sop-beta", 0), 0)

    def test_reads_in_a_sop_maintenance_session_are_excluded(self):
        """A session that edits under sop/ or runs the renderer is SOP maintenance: its
        reads are not use. Skill calls in that session still count."""
        self._write("maint.jsonl", [
            _read("sop/standard/beta.md", "r1", sid="m"),
            _rec({"id": "e1", "name": "Edit", "input": {"file_path": "/home/x/pc/sop/standard/beta.md"}}, sid="m"),
            _skill("pantheon-sop-alpha", "k1", sid="m"),
            _bash("cat sop/remediation/gamma.md", "r2", sid="r"),
            _bash("python3 tools/persona_orientation/render.py --check", "e2", sid="r"),
            _read("sop/standard/beta.md", "r3", sid="u"),  # a real use, in a clean session
        ])
        calls, _cs, reads, _rs = stc.tally([str(self.root)], "2026-10-16T00:00:00", self.paths)
        self.assertEqual(calls["pantheon-sop-alpha"], 1)
        self.assertEqual(reads.get("pantheon-sop-beta", 0), 1)   # only the clean session's read
        self.assertEqual(reads.get("pantheon-sop-gamma", 0), 0)  # the render session is maintenance
        import collections as _c
        maint = _c.Counter()
        stc.tally([str(self.root)], "2026-10-16T00:00:00", self.paths, maint)
        self.assertEqual((maint["pantheon-sop-beta"], maint["pantheon-sop-gamma"]), (1, 1))  # labelled, not lost
        out = self._run()
        self.assertIn("|           1 |", out)  # the maint_reads column is printed

    def test_a_non_sop_edit_does_not_mark_maintenance(self):
        self._write("n.jsonl", [_read("sop/standard/beta.md", "r1", sid="n"),
                                _rec({"id": "e1", "name": "Edit", "input": {"file_path": "tools/x.py"}}, sid="n")])
        _c, _cs, reads, _rs = stc.tally([str(self.root)], "2026-10-16T00:00:00", self.paths)
        self.assertEqual(reads.get("pantheon-sop-beta", 0), 1)

    def test_real_manifest_lists_pantheon_sop_skills_with_sop_paths(self):
        entries = stc.manifest_entries(stc.DEFAULT_MANIFEST)
        self.assertTrue(entries)
        for e in entries:
            self.assertTrue(str(e.get("sop", "")).startswith(("sop/standard/", "sop/remediation/")), e)


if __name__ == "__main__":
    unittest.main()
