"""T-6118 PR-0: the real-state tripwire's snapshot/diff logic (tools/_real_state_tripwire.py).

Tmp fixtures only: nothing here reads the seat's real state (the conftest hook that applies
the tripwire to the real roots is exercised by every pytest run, not re-tested here).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tools import _real_state_tripwire as tw


def _session(day_dir: Path, stem: str, handoff: str) -> Path:
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{stem}.session.json"
    path.write_text(json.dumps({"handoff_file": handoff}), encoding="utf-8")
    return path


class SnapshotDiffTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        roots = tw.real_roots(self.home)
        self.chron, self.pers = roots["chronicle"], roots["personas"]
        self.export = self.home / "chronicle-export"  # a tmp stand-in for the REAL /var/lib export root

    def _snap(self, persona: str | None = None) -> dict:
        return tw.take_snapshot(self.home, persona=persona or "", export_root=self.export)

    def test_roots_are_under_given_home(self) -> None:
        self.assertEqual(self.chron, self.home / ".local/state/pantheon/chronicle")
        self.assertEqual(self.pers, self.home / ".local/state/pantheon/personas")

    def test_absent_roots_snapshot_empty_and_diff_clean(self) -> None:
        before = self._snap()
        self.assertEqual(before, {"personas": {}, "chronicle": {}, "export": {}, "sent": {}})
        self.assertEqual(tw.diff(before, self._snap()), [])

    def test_sent_ledger_is_guarded(self) -> None:
        before = self._snap()
        ledger = self.home / ".local/state/pantheon/sent.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text('{"id":"fixture"}\n')
        self.assertEqual(tw.diff(before, self._snap()), ["added    sent/sent.jsonl"])

    def test_added_changed_removed_are_reported(self) -> None:
        f = self.pers / "x" / "runtime-controller" / "a.json"
        f.parent.mkdir(parents=True)
        f.write_text("1")
        gone = self.chron / "y" / "index.md"
        gone.parent.mkdir(parents=True)
        gone.write_text("old")
        before = self._snap()
        f.write_text("22")  # size change
        gone.unlink()
        (self.pers / "recovery").mkdir()
        lines = tw.diff(before, self._snap())
        self.assertIn("changed  personas/x/runtime-controller/a.json", lines)
        self.assertIn("removed  chronicle/y/index.md", lines)
        self.assertIn("added    personas/recovery", lines)

    def test_mtime_only_change_is_reported(self) -> None:
        f = self.pers / "x" / "a.json"
        f.parent.mkdir(parents=True)
        f.write_text("1")
        before = self._snap()
        st = os.stat(f)
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        self.assertEqual(tw.diff(before, self._snap()), ["changed  personas/x/a.json"])

    def test_own_persona_personas_subtree_is_ignored_but_others_are_guarded(self) -> None:
        own = self.pers / "artisan" / "runtime-controller"
        own.mkdir(parents=True)
        (own / "runtime.json").write_text("{}")
        before = self._snap("artisan")
        (own / "runtime.json").write_text('{"live": true}')  # the live controller, legitimate
        (own / "new.json").write_text("{}")
        self.assertEqual(tw.diff(before, self._snap("artisan")), [])
        (self.pers / "recovery" / "runtime-controller").mkdir(parents=True)  # fixture leak
        self.assertEqual(
            tw.diff(before, self._snap("artisan")),
            ["added    personas/recovery", "added    personas/recovery/runtime-controller"],
        )

    def test_own_chronicle_subtree_ignored_except_tmp_handoff_sessions(self) -> None:
        day = self.chron / "recovery" / "2026-11-04"
        _session(day, "2026-11-04T000000Z-real", "/home/claude-fallback/HANDOFF.md")
        before = self._snap("recovery")
        # a live seat archiving its own real session: not a finding
        _session(day, "2026-11-04T010000Z-real2", "/home/claude-fallback/HANDOFF.md")
        (self.chron / "recovery" / "index.md").write_text("- x\n")
        self.assertEqual(tw.diff(before, self._snap("recovery")), [])
        # a fixture session (handoff under /tmp) in the own subtree IS a finding
        leaked = _session(day, "2026-11-04T020000Z-rt-leak", "/tmp/tmpabc123/HANDOFF.md")
        self.assertEqual(tw.diff(before, self._snap("recovery")),
                         [f"added    chronicle/recovery/2026-11-04/{leaked.name}"])

    def test_non_own_chronicle_dir_fully_guarded(self) -> None:
        before = self._snap("artisan")
        _session(self.chron / "recovery" / "2026-11-04", "s", "/home/x/HANDOFF.md")
        self.assertTrue(tw.diff(before, self._snap("artisan")))

    def test_unresolved_own_persona_excludes_nothing(self) -> None:
        (self.pers / "artisan").mkdir(parents=True)
        before = self._snap()
        (self.pers / "artisan" / "x").write_text("1")
        self.assertEqual(tw.diff(before, self._snap()),
                         ["added    personas/artisan/x"])

    def test_export_tree_changes_are_reported_except_own_seat_and_ingested(self) -> None:
        for name in ("artisan", ".ingested", "recovery"):
            (self.export / name / "2026-11-04").mkdir(parents=True)
        before = self._snap("artisan")
        (self.export / "artisan" / "2026-11-04" / "a.md").write_text("own seat: exempt")
        (self.export / ".ingested" / "2026-11-04" / "r.json").write_text("receipt: exempt")
        self.assertEqual(tw.diff(before, self._snap("artisan")), [])
        (self.export / "recovery" / "2026-11-04" / "leak.md").write_text("another seat: a leak")
        self.assertEqual(tw.diff(before, self._snap("artisan")), ["added    export/recovery/2026-11-04/leak.md"])

    def test_export_snapshot_of_an_absent_or_unreadable_root_is_empty(self) -> None:
        self.assertEqual(self._snap()["export"], {})
        self.export.mkdir()
        self.export.chmod(0)
        try:
            self.assertEqual(self._snap()["export"], {})
        finally:
            self.export.chmod(0o700)

    def test_default_export_snapshot_reads_the_real_constant_not_the_env_root(self) -> None:
        from tools import chronicle_export as cx

        seen = []
        with mock.patch.object(tw, "snapshot_root", side_effect=lambda root, **kw: seen.append(root) or {}):
            tw.take_snapshot(self.home, persona="x")
        self.assertIn(cx.DEFAULT_EXPORT_ROOT, seen)
        self.assertNotIn(Path(os.environ["PANTHEON_CHRONICLE_EXPORT_ROOT"]), seen)

    def test_symlinks_are_not_followed(self) -> None:
        target = Path(self._tmp.name) / "elsewhere"
        target.mkdir()
        self.pers.mkdir(parents=True)
        os.symlink(target, self.pers / "link")
        before = self._snap()
        (target / "f").write_text("1")  # behind the link: must not be seen
        self.assertEqual(tw.diff(before, self._snap()), [])

    def test_real_home_is_the_passwd_home_not_HOME(self) -> None:
        saved = os.environ.get("HOME")
        os.environ["HOME"] = "/nonexistent-home-for-test"
        try:
            self.assertNotEqual(tw.real_home(), Path("/nonexistent-home-for-test"))
        finally:
            if saved is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved


def _loaded_conftest():
    for name in ("tools.conftest", "conftest"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "pytest_sessionfinish"):
            return module
    return None


class SessionFinishHookTests(unittest.TestCase):
    """The conftest hook, with the snapshots stubbed (no real-state reads). Skipped under plain
    unittest, where conftest is not loaded."""

    def setUp(self) -> None:
        self.conftest = _loaded_conftest()
        if self.conftest is None:
            self.skipTest("tools/conftest.py not loaded (plain unittest)")

    def _run(self, baseline, after, *, worker=False, env="") -> int:
        session = types.SimpleNamespace(exitstatus=0, config=types.SimpleNamespace())
        if worker:
            session.config.workerinput = {}
        with mock.patch.object(self.conftest, "_REAL_STATE_BASELINE", baseline), \
                mock.patch.object(tw, "take_snapshot", return_value=after), \
                mock.patch.dict(os.environ, {"PANTHEON_REAL_STATE_TRIPWIRE": env} if env else {}), \
                mock.patch("sys.stderr"):
            self.conftest.pytest_sessionfinish(session, 0)
        return int(session.exitstatus)

    def test_unchanged_state_keeps_exit_status(self) -> None:
        snap = {"personas": {"a": (1, 1)}, "chronicle": {}}
        self.assertEqual(self._run(snap, dict(snap)), 0)

    def test_changed_state_fails_the_session(self) -> None:
        before = {"personas": {}, "chronicle": {}}
        after = {"personas": {"recovery": (None, None)}, "chronicle": {}}
        self.assertEqual(self._run(before, after), 1)

    def test_warn_mode_reports_without_failing(self) -> None:
        before = {"personas": {}, "chronicle": {}}
        after = {"personas": {"recovery": (None, None)}, "chronicle": {}}
        self.assertEqual(self._run(before, after, env="warn"), 0)

    def test_xdist_worker_does_not_check(self) -> None:
        before = {"personas": {}, "chronicle": {}}
        after = {"personas": {"recovery": (None, None)}, "chronicle": {}}
        self.assertEqual(self._run(before, after, worker=True), 0)

    def test_no_baseline_means_no_check(self) -> None:
        self.assertEqual(self._run(None, {"personas": {"x": (1, 1)}, "chronicle": {}}), 0)


if __name__ == "__main__":
    unittest.main()
