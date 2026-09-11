"""Tests for tools/watch_pipeline_decommission.py (T-2887).

The whole lifecycle is driven against a FakeCommandRunner + a per-test tmp
fixture home: no test ever touches this host's real systemd --user timers, the
real ~/.local/state/pantheon state dirs, or the real committed reports tree.
The git-status probe used by --archive-reports runs against a disposable
`git init` repo stood up under the tmp home, never the real pantheon-config.

Coverage:
  * dry-run default: nothing mutates, the plan names every leg, exit 0.
  * default --execute (PAUSE): disable --now the three timers in order +
    reset-failed; unit files and state left in place.
  * --remove-units: PAUSE plus rm of the six installed unit files + daemon-reload.
  * --purge-state: state dirs archived then removed.
  * --archive-reports: in-flight (uncommitted) reports surfaced; clean tree
    reported as nothing-to-hand-off (a no-op is SURFACED, T-2702).
  * wrong-account guard: an execute run from another account refuses to mutate.
  * ordering: timers are disabled before daemon-reload / state purge.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tools._organ_lifecycle import CommandResult, FakeCommandRunner

import tools.watch_pipeline_decommission as wd


def _make_home(root: Path, *, with_units: bool = True, with_state: bool = True) -> Path:
    """Build a fixture $HOME mirroring the live install shape."""

    home = root / "home"
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    if with_units:
        for stem in wd.WATCH_UNIT_STEMS:
            for suffix in (".timer", ".service"):
                (unit_dir / f"{stem}{suffix}").write_text(f"[Unit]\n# fixture {stem}{suffix}\n")
    if with_state:
        for rel in wd.STATE_DIRS:
            state = home / rel
            state.mkdir(parents=True)
            (state / "state.json").write_text('{"fixture": true}\n')
    return home


def _make_repo(root: Path, *, dirty_report: bool = False) -> Path:
    """Stand up a disposable git repo with the reports subtree, so the
    --archive-reports git-status probe has a real repo to read -- never the
    live pantheon-config."""

    repo = root / "repo"
    reports = repo / wd.REPORTS_SUBDIR / "2026-08-12"
    reports.mkdir(parents=True)
    (reports / "committed.md").write_text("# committed report\n")
    subprocess.run(["git", "-C", str(repo), "init", "--quiet"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "seed"], check=True)
    if dirty_report:
        (reports / "in-flight.md").write_text("# uncommitted in-flight report\n")
    return repo


def _enabled_active_runner() -> FakeCommandRunner:
    """A runner scripting the manifest read-backs to enabled/active; every other
    call (disable/reset-failed/daemon-reload) takes the default success."""

    scripted = {}
    for stem in wd.WATCH_UNIT_STEMS:
        timer = f"{stem}.timer"
        scripted[("systemctl", "--user", "is-enabled", timer)] = CommandResult(0, "enabled\n", "")
        scripted[("systemctl", "--user", "is-active", timer)] = CommandResult(0, "active\n", "")
    return FakeCommandRunner(scripted=scripted)


class DryRunTests(unittest.TestCase):
    def test_dry_run_plans_every_leg_and_mutates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root)
            repo = _make_repo(root)
            runner = _enabled_active_runner()

            report = wd.decommission(
                runner,
                home=home,
                repo_root=repo,
                execute=False,
                remove_units=True,
                purge_state=True,
                archive_reports=True,
                current_account=wd.PIPELINE_ACCOUNT,
            )

            self.assertTrue(report.dry_run)
            self.assertEqual(report.changed, ())
            plan = "\n".join(report.planned)
            # every timer named in the disable leg
            for stem in wd.WATCH_UNIT_STEMS:
                self.assertIn(f"disable --now {stem}.timer", plan)
            self.assertIn("reset-failed", plan)
            self.assertIn("rm ", plan)  # remove-units leg
            self.assertIn("archive + rm", plan)  # purge-state leg
            # no disable/daemon-reload actually issued -- only the read-only
            # manifest probes (is-enabled / is-active) ran.
            mutating = [
                c for c in runner.calls
                if c[:3] not in (("systemctl", "--user", "is-enabled"), ("systemctl", "--user", "is-active"))
            ]
            self.assertEqual(mutating, [], f"dry-run issued mutating calls: {mutating}")
            # unit files + state still present
            self.assertTrue((home / ".config/systemd/user/pantheon-watch.timer").is_file())
            self.assertTrue((home / wd.STATE_DIRS[0]).is_dir())

    def test_manifest_records_restore_list(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root)
            repo = _make_repo(root)
            report = wd.decommission(
                _enabled_active_runner(),
                home=home,
                repo_root=repo,
                execute=False,
                current_account=wd.PIPELINE_ACCOUNT,
            )
            manifest = "\n".join(report.manifest)
            self.assertIn("enabled=enabled", manifest)
            self.assertIn("active=active", manifest)
            self.assertIn("enable --now pantheon-watch.timer", manifest)


class ExecutePauseTests(unittest.TestCase):
    def test_default_execute_disables_timers_in_order_and_leaves_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root)
            repo = _make_repo(root)
            runner = _enabled_active_runner()

            report = wd.decommission(
                runner,
                home=home,
                repo_root=repo,
                execute=True,
                current_account=wd.PIPELINE_ACCOUNT,
            )

            self.assertFalse(report.dry_run)
            self.assertEqual(report.warnings, ())
            # the three disables happened, in watch -> sweep -> digest order
            disables = [c for c in runner.calls if c[:4] == ("systemctl", "--user", "disable", "--now")]
            self.assertEqual(
                disables,
                [("systemctl", "--user", "disable", "--now", f"{s}.timer") for s in wd.WATCH_UNIT_STEMS],
            )
            # reset-failed issued after the disables
            self.assertIn(
                ("systemctl", "--user", "reset-failed", *(f"{s}.timer" for s in wd.WATCH_UNIT_STEMS)),
                runner.calls,
            )
            # PAUSE: unit files + state untouched
            self.assertTrue((home / ".config/systemd/user/pantheon-watch.timer").is_file())
            self.assertTrue((home / wd.STATE_DIRS[0]).is_dir())
            # no daemon-reload without --remove-units
            self.assertNotIn(("systemctl", "--user", "daemon-reload"), runner.calls)

    def test_disable_failure_is_a_warning_not_a_crash(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root)
            repo = _make_repo(root)
            runner = _enabled_active_runner()
            # make the first disable fail
            runner.scripted[("systemctl", "--user", "disable", "--now", "pantheon-watch.timer")] = CommandResult(
                1, "", "Failed to disable\n"
            )
            report = wd.decommission(
                runner, home=home, repo_root=repo, execute=True, current_account=wd.PIPELINE_ACCOUNT
            )
            self.assertTrue(any("pantheon-watch.timer exited 1" in w for w in report.warnings))
            # the other two still got disabled
            self.assertIn(("systemctl", "--user", "disable", "--now", "pantheon-knowledge-sweep.timer"), runner.calls)


class RemoveUnitsTests(unittest.TestCase):
    def test_remove_units_deletes_files_and_reloads(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root)
            repo = _make_repo(root)
            runner = _enabled_active_runner()

            report = wd.decommission(
                runner,
                home=home,
                repo_root=repo,
                execute=True,
                remove_units=True,
                current_account=wd.PIPELINE_ACCOUNT,
            )

            unit_dir = home / ".config/systemd/user"
            for stem in wd.WATCH_UNIT_STEMS:
                for suffix in (".timer", ".service"):
                    self.assertFalse((unit_dir / f"{stem}{suffix}").exists(), f"{stem}{suffix} not removed")
            # daemon-reload after removal, and it came AFTER the disables
            self.assertIn(("systemctl", "--user", "daemon-reload"), runner.calls)
            disable_idx = max(
                i for i, c in enumerate(runner.calls) if c[:4] == ("systemctl", "--user", "disable", "--now")
            )
            reload_idx = runner.calls.index(("systemctl", "--user", "daemon-reload"))
            self.assertGreater(reload_idx, disable_idx)
            self.assertTrue(any("removed" in ch for ch in report.changed))

    def test_remove_units_with_no_files_is_surfaced_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root, with_units=False)
            repo = _make_repo(root)
            report = wd.decommission(
                _enabled_active_runner(),
                home=home,
                repo_root=repo,
                execute=True,
                remove_units=True,
                current_account=wd.PIPELINE_ACCOUNT,
            )
            self.assertTrue(any("no installed unit files" in ch for ch in report.changed))


class PurgeStateTests(unittest.TestCase):
    def test_purge_state_archives_then_removes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root)
            repo = _make_repo(root)
            report = wd.decommission(
                _enabled_active_runner(),
                home=home,
                repo_root=repo,
                execute=True,
                purge_state=True,
                current_account=wd.PIPELINE_ACCOUNT,
            )
            # originals gone
            for rel in wd.STATE_DIRS:
                self.assertFalse((home / rel).is_dir(), f"{rel} not removed")
            # an archive dir exists holding the copied state
            archive_parents = list((home / ".local/state/pantheon").glob("watch-decommission-archive-*"))
            self.assertEqual(len(archive_parents), 1)
            archived = list(archive_parents[0].rglob("state.json"))
            self.assertTrue(archived, "state.json not archived")
            self.assertTrue(any("archived" in ch for ch in report.changed))

    def test_purge_state_with_no_state_is_surfaced_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root, with_state=False)
            repo = _make_repo(root)
            report = wd.decommission(
                _enabled_active_runner(),
                home=home,
                repo_root=repo,
                execute=True,
                purge_state=True,
                current_account=wd.PIPELINE_ACCOUNT,
            )
            self.assertTrue(any("nothing purged" in ch for ch in report.changed))


class ArchiveReportsTests(unittest.TestCase):
    def test_in_flight_report_is_surfaced(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root)
            repo = _make_repo(root, dirty_report=True)
            report = wd.decommission(
                _enabled_active_runner(),
                home=home,
                repo_root=repo,
                execute=True,
                archive_reports=True,
                current_account=wd.PIPELINE_ACCOUNT,
            )
            self.assertTrue(
                any("in-flight" in w and "in-flight.md" in w for w in report.warnings),
                f"uncommitted report not surfaced: {report.warnings}",
            )

    def test_clean_tree_reports_nothing_to_hand_off(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root)
            repo = _make_repo(root, dirty_report=False)
            report = wd.decommission(
                _enabled_active_runner(),
                home=home,
                repo_root=repo,
                execute=True,
                archive_reports=True,
                current_account=wd.PIPELINE_ACCOUNT,
            )
            self.assertTrue(any("no in-flight files" in ch for ch in report.changed))


class WrongAccountGuardTests(unittest.TestCase):
    def test_execute_from_another_account_refuses_to_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root)
            repo = _make_repo(root)
            runner = _enabled_active_runner()
            report = wd.decommission(
                runner,
                home=home,
                repo_root=repo,
                execute=True,
                remove_units=True,
                purge_state=True,
                current_account="some-other-account",
            )
            self.assertTrue(any("wrong account" in w for w in report.warnings))
            self.assertEqual(report.changed, ())
            # no disable / rm / daemon-reload issued; files + state intact
            self.assertNotIn(("systemctl", "--user", "daemon-reload"), runner.calls)
            self.assertFalse(
                any(c[:4] == ("systemctl", "--user", "disable", "--now") for c in runner.calls)
            )
            self.assertTrue((home / ".config/systemd/user/pantheon-watch.timer").is_file())
            self.assertTrue((home / wd.STATE_DIRS[0]).is_dir())


class MainCliTests(unittest.TestCase):
    def test_main_dry_run_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root)
            repo = _make_repo(root)
            rc = wd.main(
                ["--home", str(home), "--repo-root", str(repo)],
                runner=_enabled_active_runner(),
                current_account=wd.PIPELINE_ACCOUNT,
            )
            self.assertEqual(rc, 0)

    def test_main_execute_wrong_account_exit_one(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = _make_home(root)
            repo = _make_repo(root)
            rc = wd.main(
                ["--execute", "--home", str(home), "--repo-root", str(repo)],
                runner=_enabled_active_runner(),
                current_account="some-other-account",
            )
            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
