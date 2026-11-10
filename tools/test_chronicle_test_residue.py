"""T-6118 PR-0: tools/chronicle_test_residue.py. Tmp fixtures only; roots are passed in."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
from pathlib import Path

from tools import chronicle_test_residue as res

NOW = datetime(2026, 11, 4, 12, 0, 0, tzinfo=timezone.utc)
DAY = "2026-11-03"


class ResidueBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "pantheon"
        self.chron = self.state / "chronicle"
        self.pers = self.state / "personas"
        self.chron.mkdir(parents=True)
        self.pers.mkdir(parents=True)

    def record(self, agent: str, uuid: str, handoff: str, *, ts: str = "2026-11-03T010000Z") -> list[Path]:
        adir = self.chron / agent
        day = adir / DAY
        day.mkdir(parents=True, exist_ok=True)
        stem = f"{ts}-{uuid}"
        files = []
        for suffix, body in (("md", "# handoff"), ("session.json", json.dumps({"handoff_file": handoff}))):
            p = day / f"{stem}.{suffix}"
            p.write_text(body, encoding="utf-8")
            files.append(p)
        with (adir / "index.md").open("a", encoding="utf-8") as fh:
            fh.write(f"- {ts} | session-end/death | {uuid} | summary {uuid}\n")
        claims = adir / ".claims"
        claims.mkdir(exist_ok=True)
        claim = claims / f"key-{uuid}.json"
        claim.write_text(json.dumps({"stem_uuid": uuid, "state": "done"}), encoding="utf-8")
        return files + [claim]

    def run_tool(self, *, own: str | None, execute: bool) -> tuple[int, str]:
        out = io.StringIO()
        rc = res.run(self.chron, self.pers, self.state, own=own, execute=execute, now=NOW, out=out)
        return rc, out.getvalue()


class ScanAndDryRunTests(ResidueBase):
    def test_dry_run_lists_and_deletes_nothing(self) -> None:
        files = self.record("recovery", "uu-1", "/tmp/tmpabcd1234/HANDOFF.md")
        (self.pers / "recovery" / "runtime-controller").mkdir(parents=True)
        rc, out = self.run_tool(own="artisan", execute=False)
        self.assertEqual(rc, 0)
        self.assertIn("DRY-RUN", out)
        self.assertIn("would-delete\tchronicle-foreign-persona-dir", out)
        self.assertIn("would-delete\tpersona-dir", out)
        self.assertTrue(all(f.exists() for f in files))
        self.assertTrue((self.pers / "recovery").is_dir())
        self.assertFalse((self.state / "residue-cleanup").exists())

    def test_real_recovery_record_with_non_tmp_handoff_is_kept(self) -> None:
        files = self.record("recovery", "uu-real", "/home/claude-fallback/HANDOFF.md")
        self.assertEqual(res.scan(self.chron, self.pers, own="recovery"), [])
        self.assertTrue(all(f.exists() for f in files))

    def test_non_own_persona_chronicle_dir_is_fixture_residue_even_with_in_home_handoff(self) -> None:
        files = self.record("recovery", "uu-cross", "/home/claude-fallback/HANDOFF.md")
        targets = res.scan(self.chron, self.pers, own="artisan")
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].kind, "chronicle-foreign-persona-dir")
        self.assertEqual(targets[0].paths, [self.chron / "recovery"])
        self.assertTrue(all(path.exists() for path in files))

    def test_unresolved_own_does_not_classify_named_persona_dir_as_whole_residue(self) -> None:
        self.record("recovery", "uu-real", "/home/claude-fallback/HANDOFF.md")
        self.assertEqual(res.scan(self.chron, self.pers, own=None), [])

    def test_non_fixture_persona_with_tmp_handoff_is_not_touched(self) -> None:
        self.record("artisan", "uu-a", "/tmp/tmpzzzz/HANDOFF.md")
        self.assertEqual(res.scan(self.chron, self.pers, own="artisan"), [])

    def test_own_persona_dir_never_listed_even_when_fixture_named(self) -> None:
        (self.pers / "recovery").mkdir()
        targets = res.scan(self.chron, self.pers, own="recovery")
        self.assertEqual([t for t in targets if t.kind == "persona-dir"], [])

    def test_unresolved_own_refuses_persona_dirs(self) -> None:
        (self.pers / "recovery").mkdir()
        self.record("recovery", "uu-1", "/tmp/tmpq/HANDOFF.md")
        kinds = {t.kind for t in res.scan(self.chron, self.pers, own=None)}
        self.assertEqual(kinds, {"chronicle-record"})
        _, out = self.run_tool(own=None, execute=False)
        self.assertIn("REFUSED", out)

    def test_fixture_only_chronicle_dirs_listed(self) -> None:
        (self.chron / "swap-acceptance-persona").mkdir()
        (self.chron / "bringup-acceptance-codex-1").mkdir()
        (self.chron / "artisan").mkdir()
        names = sorted(t.paths[0].name for t in res.scan(self.chron, self.pers, own="artisan"))
        self.assertEqual(names, ["bringup-acceptance-codex-1", "swap-acceptance-persona"])


class ExecuteTests(ResidueBase):
    def test_execute_removes_whole_unit_index_line_and_claim_and_logs(self) -> None:
        leaked = self.record("recovery", "uu-leak", "/tmp/tmpleak/HANDOFF.md", ts="2026-11-03T010000Z")
        kept = self.record("recovery", "uu-keep", "/home/claude-fallback/HANDOFF.md", ts="2026-11-03T020000Z")
        rc, out = self.run_tool(own="recovery", execute=True)
        self.assertEqual(rc, 0, out)
        self.assertFalse(any(p.exists() for p in leaked))
        self.assertTrue(all(p.exists() for p in kept))
        index = (self.chron / "recovery" / "index.md").read_text(encoding="utf-8")
        self.assertNotIn("uu-leak", index)
        self.assertIn("uu-keep", index)
        log = self.state / "residue-cleanup" / "20261104T120000Z.jsonl"
        events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["event"] for e in events], ["planned", "deleted"])
        self.assertEqual(len(events[1]["removed_index_lines"]), 1)
        self.assertIn("uu-leak", events[1]["removed_index_lines"][0])

    def test_execute_removes_fixture_persona_and_fixture_only_dirs_but_not_own(self) -> None:
        (self.pers / "recovery" / "runtime-controller").mkdir(parents=True)
        (self.pers / "recovery" / "runtime-controller" / "controller-evidence.jsonl").write_text("{}\n")
        (self.pers / "artisan").mkdir()
        (self.chron / "swap-acceptance-persona" / DAY).mkdir(parents=True)
        rc, _ = self.run_tool(own="artisan", execute=True)
        self.assertEqual(rc, 0)
        self.assertFalse((self.pers / "recovery").exists())
        self.assertTrue((self.pers / "artisan").is_dir())
        self.assertFalse((self.chron / "swap-acceptance-persona").exists())

    def test_symlink_inside_persona_dir_refuses_and_leaves_everything(self) -> None:
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (outside / "precious").write_text("keep")
        recovery = self.pers / "recovery"
        recovery.mkdir()
        os.symlink(outside, recovery / "link")
        rc, out = self.run_tool(own="artisan", execute=True)
        self.assertEqual(rc, 1)
        self.assertIn("FAILED", out)
        self.assertTrue((recovery / "link").is_symlink())
        self.assertEqual((outside / "precious").read_text(), "keep")

    def test_symlinked_persona_dir_is_never_a_target(self) -> None:
        outside = Path(self._tmp.name) / "outside2"
        outside.mkdir()
        os.symlink(outside, self.pers / "recovery")
        self.assertEqual(res.scan(self.chron, self.pers, own="artisan"), [])

    def test_shared_claim_across_records_removes_all_and_marks_already_gone(self) -> None:
        a = self.record("recovery", "uu-shared", "/tmp/tmpa/HANDOFF.md", ts="2026-11-03T010000Z")
        b = self.record("recovery", "uu-shared", "/tmp/tmpb/HANDOFF.md", ts="2026-11-03T020000Z")
        c = self.record("recovery", "uu-shared", "/tmp/tmpc/HANDOFF.md", ts="2026-11-03T030000Z")
        claim = a[-1]
        self.assertEqual(claim, b[-1])
        rc, out = self.run_tool(own="recovery", execute=True)
        self.assertEqual(rc, 0, out)
        self.assertFalse(any(p.exists() for p in a + b + c))
        log = self.state / "residue-cleanup" / "20261104T120000Z.jsonl"
        events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        deleted = [e for e in events if e["event"] == "deleted"]
        self.assertEqual(len(deleted), 3)
        self.assertEqual([e for e in events if e["event"] == "failed"], [])
        self.assertNotIn("claims_already_gone", deleted[0])
        for e in deleted[1:]:
            self.assertEqual(e["claims_already_gone"], [str(claim)])

    def test_symlinked_shared_claim_still_refused(self) -> None:
        a = self.record("recovery", "uu-sc", "/tmp/tmpa/HANDOFF.md", ts="2026-11-03T010000Z")
        claim = a[-1]
        outside = Path(self._tmp.name) / "victim2"
        outside.write_text("keep")
        claim.unlink()
        os.symlink(outside, claim)  # symlinked claims are not matched by scan, so build the target directly
        t = res.Target(kind="chronicle-record", reason="x", root=self.chron / "recovery", paths=a[:2], size=0,
                       claims=[claim])
        with self.assertRaises(res.Refused):
            res._delete_target(t)
        self.assertEqual(outside.read_text(), "keep")
        self.assertTrue(all(p.exists() for p in a[:2]))

    def test_missing_record_path_stays_strict(self) -> None:
        a = self.record("recovery", "uu-miss", "/tmp/tmpa/HANDOFF.md")
        a[0].unlink()
        t = res.Target(kind="chronicle-record", reason="x", root=self.chron / "recovery", paths=a[:2], size=0,
                       claims=[a[2]])
        with self.assertRaises(FileNotFoundError):
            res._delete_target(t)

    def test_symlinked_record_file_refuses(self) -> None:
        files = self.record("recovery", "uu-s", "/tmp/tmpq/HANDOFF.md")
        md = files[0]
        outside = Path(self._tmp.name) / "victim"
        outside.write_text("keep")
        md.unlink()
        os.symlink(outside, md)
        rc, _ = self.run_tool(own="artisan", execute=True)
        self.assertEqual(rc, 1)
        self.assertEqual(outside.read_text(), "keep")

    def test_idempotent_second_run_finds_nothing(self) -> None:
        self.record("recovery", "uu-1", "/tmp/tmpx/HANDOFF.md")
        self.run_tool(own="artisan", execute=True)
        _, out = self.run_tool(own="artisan", execute=False)
        self.assertIn("0 target(s)", out)


class CliTests(unittest.TestCase):
    def test_cli_accepts_no_path_or_home_argument(self) -> None:
        for flag in ("--home", "--root", "--chronicle", "--personas", "--state-dir"):
            with self.assertRaises(SystemExit):
                with unittest.mock.patch("sys.stderr", io.StringIO()):
                    res.main([flag, "/tmp/x"])


if __name__ == "__main__":
    unittest.main()
