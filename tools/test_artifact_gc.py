#!/usr/bin/env python3
"""Tests for artifact_gc.py (T-1360) — the ticket-scoped evidence janitor."""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import artifact_gc as gc  # noqa: E402


NOW = datetime(2026, 6, 28, tzinfo=timezone.utc)


def _write_ticket(repo: Path, state: str, ticket_id: str, last_log_ts: str | None) -> None:
    """Write a minimal ticket file with a transition log carrying last_log_ts."""
    d = repo / "tickets" / state
    d.mkdir(parents=True, exist_ok=True)
    log = ""
    if last_log_ts:
        log = f"\n## Transition log\n\n- {last_log_ts} [recovery] resolved; with done\n"
    (d / f"{ticket_id}.md").write_text(
        "---\n"
        f"id: {ticket_id}\n"
        "created: 2026-01-01T00:00:00Z\n"
        "requester: recovery\n"
        "target: recovery\n"
        f"state: {state}\n"
        "---\n\n"
        f"# {ticket_id}\n"
        f"{log}"
    )


def _make_artifact_dir(repo: Path, ticket_id: str) -> Path:
    d = repo / "screenshots" / ticket_id
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ticket_id}-shot.png").write_bytes(b"\x89PNG fake")
    return d


class TestParseGrace(unittest.TestCase):
    def test_days(self) -> None:
        self.assertEqual(gc.parse_grace("30d"), timedelta(days=30))

    def test_hours(self) -> None:
        self.assertEqual(gc.parse_grace("12h"), timedelta(hours=12))

    def test_invalid(self) -> None:
        with self.assertRaises(ValueError):
            gc.parse_grace("nope")


class TestNewestLogTs(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_picks_newest_log_entry(self) -> None:
        d = self.repo / "tickets" / "done"
        d.mkdir(parents=True)
        (d / "T-1.md").write_text(
            "---\nid: T-1\ncreated: 2026-01-01T00:00:00Z\nstate: done\n---\n\n"
            "## Transition log\n\n"
            "- 2026-06-01T00:00:00Z [recovery] filed in inbox\n"
            "- 2026-06-20T12:00:00Z [recovery] resolved; with done\n"
        )
        ts = gc.newest_log_ts(d / "T-1.md")
        self.assertEqual(ts, datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc))

    def test_falls_back_to_created_when_no_log(self) -> None:
        d = self.repo / "tickets" / "done"
        d.mkdir(parents=True)
        (d / "T-2.md").write_text(
            "---\nid: T-2\ncreated: 2026-02-02T00:00:00Z\nstate: done\n---\n\n# T-2\n"
        )
        ts = gc.newest_log_ts(d / "T-2.md")
        self.assertEqual(ts, datetime(2026, 2, 2, tzinfo=timezone.utc))


class TestClassify(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _classify(self, ticket_id: str) -> gc.Decision:
        art = _make_artifact_dir(self.repo, ticket_id)
        return gc.classify(art, self.repo, timedelta(days=30), NOW)

    def test_done_old_is_removed(self) -> None:
        _write_ticket(self.repo, "done", "T-0001", "2026-05-01T00:00:00Z")  # ~58d ago
        d = self._classify("T-0001")
        self.assertEqual(d.action, "remove")

    def test_done_recent_is_kept(self) -> None:
        _write_ticket(self.repo, "done", "T-0002", "2026-06-20T00:00:00Z")  # 8d ago
        d = self._classify("T-0002")
        self.assertEqual(d.action, "keep")
        self.assertIn("within grace", d.reason)

    def test_active_ticket_is_kept(self) -> None:
        _write_ticket(self.repo, "active", "T-0003", "2026-05-01T00:00:00Z")
        d = self._classify("T-0003")
        self.assertEqual(d.action, "keep")
        self.assertIn("active", d.reason)

    def test_orphan_ticket_is_kept(self) -> None:
        # No ticket file written at all.
        d = self._classify("T-0004")
        self.assertEqual(d.action, "keep")
        self.assertIn("not found", d.reason)

    def test_done_undatable_is_kept(self) -> None:
        # done ticket, but no log AND no parseable created → undatable.
        dd = self.repo / "tickets" / "done"
        dd.mkdir(parents=True, exist_ok=True)
        (dd / "T-0005.md").write_text(
            "---\nid: T-0005\ncreated: not-a-date\nstate: done\n---\n\n# T-0005\n"
        )
        d = self._classify("T-0005")
        self.assertEqual(d.action, "keep")
        self.assertIn("undatable", d.reason)

    def test_boundary_exactly_at_grace_is_kept(self) -> None:
        # age == grace is NOT > grace → kept (strict inequality).
        ts = (NOW - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_ticket(self.repo, "done", "T-0006", ts)
        d = self._classify("T-0006")
        self.assertEqual(d.action, "keep")


class TestSweep(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_dry_run_removes_nothing(self) -> None:
        _write_ticket(self.repo, "done", "T-0001", "2026-05-01T00:00:00Z")
        art = _make_artifact_dir(self.repo, "T-0001")
        res = gc.sweep(self.repo, timedelta(days=30), NOW, dry_run=True)
        self.assertEqual(len(res.removed), 1)  # reported as would-remove
        self.assertTrue(art.exists())  # but NOT actually removed
        self.assertFalse(res.committed)

    def test_do_commit_false_removes_on_disk_no_git(self) -> None:
        _write_ticket(self.repo, "done", "T-0001", "2026-05-01T00:00:00Z")  # old → remove
        _write_ticket(self.repo, "done", "T-0002", "2026-06-25T00:00:00Z")  # recent → keep
        old = _make_artifact_dir(self.repo, "T-0001")
        recent = _make_artifact_dir(self.repo, "T-0002")
        res = gc.sweep(self.repo, timedelta(days=30), NOW, dry_run=False, do_commit=False)
        self.assertEqual([d.ticket_id for d in res.removed], ["T-0001"])
        self.assertFalse(old.exists())   # removed from disk
        self.assertTrue(recent.exists())  # kept
        self.assertFalse(res.committed)   # no git interaction

    def test_no_stale_dirs_is_clean_noop(self) -> None:
        _write_ticket(self.repo, "active", "T-0003", "2026-05-01T00:00:00Z")
        _make_artifact_dir(self.repo, "T-0003")
        res = gc.sweep(self.repo, timedelta(days=30), NOW, dry_run=False, do_commit=False)
        self.assertEqual(res.removed, [])

    def test_pre_sync_failure_aborts_before_any_removal(self) -> None:
        """COMMIT-SAFETY: if the repo can't be synced, abort WITHOUT deleting
        anything — never orphan a working-tree deletion (found via integration
        test, 2026-06-28)."""
        _write_ticket(self.repo, "done", "T-0001", "2026-05-01T00:00:00Z")  # old → would remove
        art = _make_artifact_dir(self.repo, "T-0001")
        with patch.object(gc, "pull_or_rebase_strict", return_value=(False, "divergence")):
            res = gc.sweep(self.repo, timedelta(days=30), NOW, dry_run=False, do_commit=True)
        self.assertTrue(art.exists(), "pre-sync failure must NOT remove anything")
        self.assertEqual(res.removed, [])
        self.assertFalse(res.committed)
        self.assertIn("pre-sync failed", res.commit_detail)
        # Still CLASSIFIED as remove, so main() surfaces the abort as exit 1.
        self.assertIn("remove", [d.action for d in res.decisions])

    def test_commit_failure_after_rmtree_rolls_back(self) -> None:
        """COMMIT-SAFETY (T-1384): if the scoped commit fails AFTER the rmtree,
        restore the artifact roots from HEAD — a failed commit must never leave
        an irreversible deletion."""
        import subprocess

        repo = self.repo
        # Real git repo with the stale artifact committed at HEAD.
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "t@t"],
            ["git", "config", "user.name", "t"],
        ):
            subprocess.run(cmd, cwd=repo, check=True)
        _write_ticket(repo, "done", "T-0001", "2026-05-01T00:00:00Z")  # old → remove
        art = _make_artifact_dir(repo, "T-0001")
        shot = art / "T-0001-shot.png"
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "seed"], cwd=repo, check=True)
        # pre-sync OK, but the scoped commit fails AFTER the rmtree has run.
        with patch.object(gc, "pull_or_rebase_strict", return_value=(True, "ok")), patch.object(
            gc, "commit_scoped", return_value=(False, "commit blew up")
        ):
            res = gc.sweep(repo, timedelta(days=30), NOW, dry_run=False, do_commit=True)
        self.assertFalse(res.committed)
        self.assertTrue(shot.exists(), "failed commit must roll back the rmtree from HEAD")
        self.assertEqual(res.removed, [], "rolled-back removals are not reported as removed")
        self.assertIn("rolled back", res.commit_detail)


class TestFindArtifactDirs(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_only_ticket_shaped_dirs(self) -> None:
        _make_artifact_dir(self.repo, "T-0001")
        _make_artifact_dir(self.repo, "T-0123")
        # A non-ticket dir + a stray file must be ignored.
        (self.repo / "screenshots" / "misc").mkdir(parents=True, exist_ok=True)
        (self.repo / "screenshots" / "README.md").write_text("x")
        dirs = gc.find_artifact_dirs(self.repo)
        names = sorted(d.name for d in dirs)
        self.assertEqual(names, ["T-0001", "T-0123"])


class TestMainCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        # No echo-* dirs exist under this path — the tmp-target domain's glob
        # finds nothing. Explicit (not the real `/tmp`) so these CLI tests stay
        # hermetic now that the tmp domain runs by default (T-1506).
        self.tmp_root = self.repo / "no-tmp-workspaces"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_default_is_report_only_no_apply(self) -> None:
        """The safety guarantee: a bare invocation (no --apply) removes nothing."""
        _write_ticket(self.repo, "done", "T-0001", "2026-05-01T00:00:00Z")  # old
        art = _make_artifact_dir(self.repo, "T-0001")
        rc = gc.main(["--no-digest", "--repo-root", str(self.repo), "--tmp-root", str(self.tmp_root)])
        self.assertEqual(rc, 0)
        self.assertTrue(art.exists(), "default (no --apply) must NOT remove anything")

    def test_bad_grace_returns_2(self) -> None:
        rc = gc.main([
            "--no-digest", "--grace", "bogus",
            "--repo-root", str(self.repo), "--tmp-root", str(self.tmp_root),
        ])
        self.assertEqual(rc, 2)

    def test_bad_tmp_grace_returns_2(self) -> None:
        rc = gc.main([
            "--no-digest", "--tmp-grace", "bogus",
            "--repo-root", str(self.repo), "--tmp-root", str(self.tmp_root),
        ])
        self.assertEqual(rc, 2)

    def test_only_screenshots_skips_tmp_domain(self) -> None:
        """--only screenshots must not touch a stale tmp-target dir."""
        _write_ticket(self.repo, "done", "T-0007", "2026-05-01T00:00:00Z")
        ws = self.repo / "tmp-ws" / "echo-T-0007"
        target = ws / "target"
        target.mkdir(parents=True)
        (target / "big.bin").write_bytes(b"x" * 10)
        rc = gc.main([
            "--apply", "--no-digest", "--only", "screenshots",
            "--repo-root", str(self.repo), "--tmp-root", str(self.repo / "tmp-ws"),
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(target.exists(), "--only screenshots must not sweep tmp-targets")

    def test_only_tmp_targets_skips_screenshots_domain(self) -> None:
        """--only tmp-targets must not touch a stale screenshots dir."""
        _write_ticket(self.repo, "done", "T-0008", "2026-05-01T00:00:00Z")
        art = _make_artifact_dir(self.repo, "T-0008")
        rc = gc.main([
            "--apply", "--no-digest", "--only", "tmp-targets",
            "--repo-root", str(self.repo), "--tmp-root", str(self.tmp_root),
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(art.exists(), "--only tmp-targets must not sweep screenshots")


class TestDirSizeBytes(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_sums_nested_files(self) -> None:
        (self.root / "sub").mkdir()
        (self.root / "a.bin").write_bytes(b"x" * 10)
        (self.root / "sub" / "b.bin").write_bytes(b"y" * 20)
        self.assertEqual(gc._dir_size_bytes(self.root), 30)

    def test_empty_dir_is_zero(self) -> None:
        (self.root / "empty").mkdir()
        self.assertEqual(gc._dir_size_bytes(self.root / "empty"), 0)


class TestFindTmpTargetDirs(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ticket_shaped_with_target_is_candidate(self) -> None:
        (self.tmp_root / "echo-T-0001" / "target").mkdir(parents=True)
        candidates, unmanaged = gc.find_tmp_target_dirs(self.tmp_root)
        self.assertEqual([c.ticket_id for c in candidates], ["T-0001"])
        self.assertEqual(unmanaged, [])

    def test_ticket_shaped_without_target_is_skipped_not_unmanaged(self) -> None:
        (self.tmp_root / "echo-T-0002").mkdir(parents=True)
        candidates, unmanaged = gc.find_tmp_target_dirs(self.tmp_root)
        self.assertEqual(candidates, [])
        self.assertEqual(unmanaged, [])

    def test_non_ticket_shaped_echo_dir_is_unmanaged(self) -> None:
        (self.tmp_root / "echo-filibuster" / "target").mkdir(parents=True)
        candidates, unmanaged = gc.find_tmp_target_dirs(self.tmp_root)
        self.assertEqual(candidates, [])
        self.assertEqual([p.name for p in unmanaged], ["echo-filibuster"])

    def test_non_echo_dir_is_ignored_entirely(self) -> None:
        (self.tmp_root / "some-other-tmp-thing").mkdir(parents=True)
        candidates, unmanaged = gc.find_tmp_target_dirs(self.tmp_root)
        self.assertEqual(candidates, [])
        self.assertEqual(unmanaged, [])

    def test_missing_tmp_root_is_empty(self) -> None:
        candidates, unmanaged = gc.find_tmp_target_dirs(self.tmp_root / "does-not-exist")
        self.assertEqual(candidates, [])
        self.assertEqual(unmanaged, [])


class TestFindLiveBuildPids(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "echo-T-0001"
        (self.workspace / "target" / "debug" / "build").mkdir(parents=True)
        self.proc_root = self.root / "proc"
        self.proc_root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_proc(self, pid: str, comm: str, cwd_target: Path) -> None:
        d = self.proc_root / pid
        d.mkdir()
        (d / "comm").write_text(f"{comm}\n", encoding="utf-8")
        (d / "cwd").symlink_to(cwd_target)

    def test_cargo_cwd_exactly_workspace_is_busy(self) -> None:
        self._make_proc("111", "cargo", self.workspace)
        pids = gc.find_live_build_pids(self.workspace, proc_root=self.proc_root)
        self.assertEqual(pids, [111])

    def test_rustc_cwd_nested_under_target_is_busy(self) -> None:
        self._make_proc("222", "rustc", self.workspace / "target" / "debug" / "build")
        pids = gc.find_live_build_pids(self.workspace, proc_root=self.proc_root)
        self.assertEqual(pids, [222])

    def test_unrelated_process_name_is_not_busy(self) -> None:
        self._make_proc("333", "python3", self.workspace)
        pids = gc.find_live_build_pids(self.workspace, proc_root=self.proc_root)
        self.assertEqual(pids, [])

    def test_cargo_cwd_outside_workspace_is_not_busy(self) -> None:
        other = self.root / "elsewhere"
        other.mkdir()
        self._make_proc("444", "cargo", other)
        pids = gc.find_live_build_pids(self.workspace, proc_root=self.proc_root)
        self.assertEqual(pids, [])

    def test_no_procs_is_not_busy(self) -> None:
        pids = gc.find_live_build_pids(self.workspace, proc_root=self.proc_root)
        self.assertEqual(pids, [])

    def test_non_digit_proc_entries_are_skipped(self) -> None:
        (self.proc_root / "self").symlink_to(self.workspace)  # not a pid dir
        pids = gc.find_live_build_pids(self.workspace, proc_root=self.proc_root)
        self.assertEqual(pids, [])

    def test_missing_proc_root_is_not_busy(self) -> None:
        pids = gc.find_live_build_pids(self.workspace, proc_root=self.root / "no-proc")
        self.assertEqual(pids, [])


class TestClassifyTmp(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.proc_root = self.repo / "proc"  # empty — no live procs by default
        self.proc_root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _candidate(self, ticket_id: str) -> gc.TmpCandidate:
        ws = self.repo / "tmp-ws" / f"echo-{ticket_id}"
        target = ws / "target"
        target.mkdir(parents=True)
        return gc.TmpCandidate(workspace=ws, target=target, ticket_id=ticket_id)

    def test_done_old_no_live_proc_is_removed(self) -> None:
        _write_ticket(self.repo, "done", "T-0001", "2026-05-01T00:00:00Z")  # ~58d ago
        c = self._candidate("T-0001")
        d = gc.classify_tmp(c, self.repo, timedelta(hours=48), NOW, proc_root=self.proc_root)
        self.assertEqual(d.action, "remove")

    def test_done_recent_within_48h_is_kept(self) -> None:
        recent = (NOW - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_ticket(self.repo, "done", "T-0002", recent)
        c = self._candidate("T-0002")
        d = gc.classify_tmp(c, self.repo, timedelta(hours=48), NOW, proc_root=self.proc_root)
        self.assertEqual(d.action, "keep")
        self.assertIn("within grace", d.reason)

    def test_active_ticket_is_kept(self) -> None:
        _write_ticket(self.repo, "active", "T-0003", "2026-05-01T00:00:00Z")
        c = self._candidate("T-0003")
        d = gc.classify_tmp(c, self.repo, timedelta(hours=48), NOW, proc_root=self.proc_root)
        self.assertEqual(d.action, "keep")
        self.assertIn("active", d.reason)

    def test_orphan_ticket_is_kept(self) -> None:
        c = self._candidate("T-0004")
        d = gc.classify_tmp(c, self.repo, timedelta(hours=48), NOW, proc_root=self.proc_root)
        self.assertEqual(d.action, "keep")
        self.assertIn("not found", d.reason)

    def test_boundary_exactly_at_grace_is_kept(self) -> None:
        ts = (NOW - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_ticket(self.repo, "done", "T-0006", ts)
        c = self._candidate("T-0006")
        d = gc.classify_tmp(c, self.repo, timedelta(hours=48), NOW, proc_root=self.proc_root)
        self.assertEqual(d.action, "keep")

    def test_done_old_but_live_build_process_is_kept(self) -> None:
        _write_ticket(self.repo, "done", "T-0005", "2026-05-01T00:00:00Z")  # old
        c = self._candidate("T-0005")
        (self.proc_root / "999").mkdir()
        (self.proc_root / "999" / "comm").write_text("cargo\n", encoding="utf-8")
        (self.proc_root / "999" / "cwd").symlink_to(c.workspace)
        d = gc.classify_tmp(c, self.repo, timedelta(hours=48), NOW, proc_root=self.proc_root)
        self.assertEqual(d.action, "keep")
        self.assertIn("live build process", d.reason)


class TestSweepTmpTargets(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = __import__("tempfile").TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        self.tmp_root = self.repo / "tmp-ws"
        self.proc_root = self.repo / "proc"
        self.proc_root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed(self, ticket_id: str, state: str, last_log_ts: str | None, nbytes: int = 5) -> Path:
        _write_ticket(self.repo, state, ticket_id, last_log_ts)
        target = self.tmp_root / f"echo-{ticket_id}" / "target"
        target.mkdir(parents=True)
        (target / "blob.bin").write_bytes(b"z" * nbytes)
        return target

    def test_dry_run_removes_nothing_but_reports_would_remove(self) -> None:
        target = self._seed("T-0001", "done", "2026-05-01T00:00:00Z", nbytes=100)
        res = gc.sweep_tmp_targets(
            self.tmp_root, self.repo, timedelta(hours=48), NOW,
            proc_root=self.proc_root, dry_run=True,
        )
        self.assertEqual(len(res.removed), 1)
        self.assertTrue(target.exists())
        self.assertEqual(res.removed[0].size_bytes, 100)

    def test_apply_removes_stale_keeps_recent(self) -> None:
        old = self._seed("T-0001", "done", "2026-05-01T00:00:00Z")
        recent = self._seed("T-0002", "done", (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        res = gc.sweep_tmp_targets(
            self.tmp_root, self.repo, timedelta(hours=48), NOW,
            proc_root=self.proc_root, dry_run=False,
        )
        self.assertEqual([d.ticket_id for d in res.removed], ["T-0001"])
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_unmanaged_dirs_reported_and_never_touched(self) -> None:
        (self.tmp_root / "echo-filibuster" / "target").mkdir(parents=True)
        (self.tmp_root / "echo-filibuster" / "target" / "f.bin").write_bytes(b"x")
        res = gc.sweep_tmp_targets(
            self.tmp_root, self.repo, timedelta(hours=48), NOW,
            proc_root=self.proc_root, dry_run=False,
        )
        self.assertEqual([p.name for p in res.unmanaged], ["echo-filibuster"])
        self.assertTrue((self.tmp_root / "echo-filibuster" / "target").exists())

    def test_one_removal_failure_does_not_abort_others(self) -> None:
        old1 = self._seed("T-0001", "done", "2026-05-01T00:00:00Z")
        old2 = self._seed("T-0002", "done", "2026-05-01T00:00:00Z")
        real_rmtree = gc.shutil.rmtree

        def flaky_rmtree(path, *a, **kw):
            if str(path) == str(old1):
                raise OSError("simulated failure")
            return real_rmtree(path, *a, **kw)

        with patch.object(gc.shutil, "rmtree", side_effect=flaky_rmtree):
            res = gc.sweep_tmp_targets(
                self.tmp_root, self.repo, timedelta(hours=48), NOW,
                proc_root=self.proc_root, dry_run=False,
            )
        self.assertEqual([d.ticket_id for d in res.removed], ["T-0002"])
        self.assertTrue(old1.exists(), "failed removal must be left in place, not orphaned")
        self.assertFalse(old2.exists())


class TestBuildDigestMerged(unittest.TestCase):
    def test_merged_digest_includes_both_domains(self) -> None:
        shot = gc.Decision(Path("screenshots/T-0001"), "T-0001", "remove", "done 40d ago (> grace)")
        tmp = gc.TmpDecision(
            Path("/tmp/echo-T-0002"), Path("/tmp/echo-T-0002/target"), "T-0002",
            "remove", "done 3d ago (> grace)", size_bytes=2 * 1024**3,
        )
        text = gc.build_digest([shot], [shot], [tmp], [tmp], dry_run=False)
        self.assertIn("screenshots/T-0001/", text)
        self.assertIn("echo-T-0002", text)
        self.assertIn("2.0GB", text)

    def test_dry_run_uses_would_language(self) -> None:
        tmp = gc.TmpDecision(
            Path("/tmp/echo-T-0003"), Path("/tmp/echo-T-0003/target"), "T-0003",
            "remove", "done 3d ago (> grace)", size_bytes=1024**3,
        )
        text = gc.build_digest([], [], [tmp], [tmp], dry_run=True)
        self.assertIn("would", text)


if __name__ == "__main__":
    unittest.main()
