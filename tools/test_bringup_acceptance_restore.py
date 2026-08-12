"""Tests for tools.bringup_acceptance.restore (T-2556 phase 3 / T-2560).

Disposable fixtures only, per the binding: a fake CommandRunner over an
in-memory unit-state dict stands in for systemctl (no live systemd touched),
and real temp dirs stand in for settings/mailbox/marker paths (no live
mailbox or persona state touched).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from typing import Sequence
from unittest.mock import patch

import tools.bringup_acceptance.restore as restore_module
from tools.bringup_acceptance.evidence import RunPaths, read_restore, write_restore
from tools.bringup_acceptance.restore import (
    BaselineTarget,
    record_baseline,
    restore_payload,
    restore_to,
    verify_zero_residue,
)
from tools.bringup_acceptance.types import HardStop


def _unit_state(*, active: str, enabled: str) -> dict[str, str]:
    return {
        "LoadState": "loaded",
        "ActiveState": active,
        "SubState": "running" if active == "active" else "dead",
        "UnitFileState": enabled,
    }


def _make_fake_systemctl(state: dict[str, dict[str, str]]):
    """A CommandRunner over `state`, standing in for systemctl show/start/stop/enable/disable."""

    def _runner(command: Sequence[str]) -> CompletedProcess[str]:
        command = tuple(command)
        assert command[0] == "systemctl"
        verb, unit = command[3], command[4]
        if verb == "show":
            values = state.get(unit)
            if values is None:
                return CompletedProcess(command, 0, stdout="", stderr="")
            stdout = "\n".join(f"{key}={value}" for key, value in values.items())
            return CompletedProcess(command, 0, stdout=stdout, stderr="")
        if verb in ("start", "stop", "enable", "disable"):
            values = state.setdefault(unit, _unit_state(active="inactive", enabled="disabled"))
            if verb == "start":
                values["ActiveState"], values["SubState"] = "active", "running"
            elif verb == "stop":
                values["ActiveState"], values["SubState"] = "inactive", "dead"
            elif verb == "enable":
                values["UnitFileState"] = "enabled"
            else:
                values["UnitFileState"] = "disabled"
            return CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected systemctl verb: {verb}")

    return _runner


class RecordBaselineTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state = {
            "writer-a.timer": _unit_state(active="active", enabled="enabled"),
            "recovery-scope.scope": _unit_state(active="inactive", enabled="enabled"),
        }
        self.runner = _make_fake_systemctl(self.state)
        self.settings_path = self.root / "settings.json"
        self.settings_path.write_bytes(b'{"allow": ["Bash"]}')
        self.mailbox_dir = self.root / "mailbox" / "new"
        self.mailbox_dir.mkdir(parents=True)
        (self.mailbox_dir / "m1").write_text("one")
        (self.mailbox_dir / "m2").write_text("two")
        self.marker_path = self.root / "persona_state.tar"
        self.backup_root = self.root / "backups"
        self.target = BaselineTarget(
            account="test-account",
            unit_names=("writer-a.timer",),
            runtime_scope_units=("recovery-scope.scope",),
            mailbox_dirs={"new": self.mailbox_dir},
            settings_paths=(self.settings_path,),
            disposable_markers=(self.marker_path,),
            backup_root=self.backup_root,
        )

    def test_baseline_captures_every_category(self) -> None:
        baseline = record_baseline(self.target, runner=self.runner)
        self.assertEqual(baseline.unit_states["writer-a.timer"]["ActiveState"], "active")
        self.assertEqual(baseline.unit_states["writer-a.timer"]["UnitFileState"], "enabled")
        self.assertEqual(baseline.runtime_scope_states["recovery-scope.scope"]["ActiveState"], "inactive")
        self.assertEqual(baseline.mailbox_counts["new"], 2)
        settings = baseline.settings[str(self.settings_path)]
        self.assertTrue(settings.existed)
        self.assertTrue(Path(settings.backup_path).is_file())
        self.assertEqual(Path(settings.backup_path).read_bytes(), self.settings_path.read_bytes())
        self.assertFalse(baseline.disposable_markers[str(self.marker_path)])

    def test_absent_settings_file_records_not_existed(self) -> None:
        missing = self.root / "absent.json"
        target = BaselineTarget(account="a", settings_paths=(missing,), backup_root=self.backup_root)
        baseline = record_baseline(target, runner=self.runner)
        snapshot = baseline.settings[str(missing)]
        self.assertFalse(snapshot.existed)
        self.assertEqual(snapshot.sha256, "")
        self.assertEqual(snapshot.backup_path, "")

    def test_absent_mailbox_dir_counts_zero_not_error(self) -> None:
        target = BaselineTarget(account="a", mailbox_dirs={"new": self.root / "no-such-dir"})
        baseline = record_baseline(target, runner=self.runner)
        self.assertEqual(baseline.mailbox_counts["new"], 0)

    def test_query_failure_is_a_hard_stop(self) -> None:
        def _failing_runner(command: Sequence[str]) -> CompletedProcess[str]:
            return CompletedProcess(tuple(command), 1, stdout="", stderr="unit not found")

        target = BaselineTarget(account="a", unit_names=("missing.service",))
        with self.assertRaises(HardStop):
            record_baseline(target, runner=_failing_runner)


class SelfProvingCycleTest(unittest.TestCase):
    """The primitive's own proof: record -> mutate -> restore -> assert zero-residue,
    plus one RED case per residue category (T-2560's binding requirement)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state = {
            "writer-a.timer": _unit_state(active="active", enabled="enabled"),
            "recovery-scope.scope": _unit_state(active="inactive", enabled="disabled"),
        }
        self.runner = _make_fake_systemctl(self.state)
        self.settings_path = self.root / "settings.json"
        self.original_bytes = b'{"allow": ["Bash"]}\x00binary-safe'
        self.settings_path.write_bytes(self.original_bytes)
        self.mailbox_dir = self.root / "mailbox" / "new"
        self.mailbox_dir.mkdir(parents=True)
        (self.mailbox_dir / "m1").write_text("one")
        self.marker_path = self.root / "persona_state.tar"
        self.backup_root = self.root / "backups"
        self.target = BaselineTarget(
            account="test-account",
            unit_names=("writer-a.timer",),
            runtime_scope_units=("recovery-scope.scope",),
            mailbox_dirs={"new": self.mailbox_dir},
            settings_paths=(self.settings_path,),
            disposable_markers=(self.marker_path,),
            backup_root=self.backup_root,
        )
        self.baseline = record_baseline(self.target, runner=self.runner)

    def _mutate(self) -> None:
        # Simulate the anomalies a live module run might leave behind.
        self.state["writer-a.timer"]["ActiveState"] = "inactive"
        self.state["writer-a.timer"]["SubState"] = "dead"
        self.state["recovery-scope.scope"]["UnitFileState"] = "enabled"
        self.settings_path.write_bytes(b"tampered")
        self.marker_path.write_text("residue")

    def test_record_mutate_restore_verify_is_clean(self) -> None:
        self._mutate()
        restore_to(self.baseline, runner=self.runner)
        post = record_baseline(self.target, runner=self.runner)
        verify_zero_residue(self.baseline, post)  # must not raise
        # Every channel actually reverted, not just "no exception raised".
        self.assertEqual(self.state["writer-a.timer"]["ActiveState"], "active")
        self.assertEqual(self.state["recovery-scope.scope"]["UnitFileState"], "disabled")
        self.assertEqual(self.settings_path.read_bytes(), self.original_bytes)
        self.assertFalse(self.marker_path.exists())

    def test_settings_backup_is_binary_safe_byte_for_byte(self) -> None:
        self.settings_path.write_bytes(b"\x00\x01clobbered\xff")
        restore_to(self.baseline, runner=self.runner)
        self.assertEqual(self.settings_path.read_bytes(), self.original_bytes)

    def test_residue_left_in_a_unit_is_a_hard_stop_naming_it(self) -> None:
        self._mutate()
        restore_to(self.baseline, runner=self.runner)
        self.state["writer-a.timer"]["ActiveState"] = "inactive"  # restore's own action didn't stick
        post = record_baseline(self.target, runner=self.runner)
        with self.assertRaisesRegex(HardStop, "writer-a.timer"):
            verify_zero_residue(self.baseline, post)

    def test_residue_left_in_settings_is_a_hard_stop_naming_the_path(self) -> None:
        self._mutate()
        restore_to(self.baseline, runner=self.runner)
        self.settings_path.write_bytes(b"post-restore-corruption")
        post = record_baseline(self.target, runner=self.runner)
        with self.assertRaisesRegex(HardStop, "settings.json"):
            verify_zero_residue(self.baseline, post)

    def test_residue_left_as_a_marker_is_a_hard_stop_naming_the_path(self) -> None:
        self._mutate()
        restore_to(self.baseline, runner=self.runner)
        self.marker_path.write_text("still here")  # simulate a restore_to bug leaving it behind
        post = record_baseline(self.target, runner=self.runner)
        with self.assertRaisesRegex(HardStop, "persona_state.tar"):
            verify_zero_residue(self.baseline, post)

    def test_mailbox_residue_is_caught_even_though_restore_to_never_touches_it(self) -> None:
        (self.mailbox_dir / "m2-leftover").write_text("residue")
        restore_to(self.baseline, runner=self.runner)  # units/settings/markers clean; mailbox untouched by design
        post = record_baseline(self.target, runner=self.runner)
        with self.assertRaisesRegex(HardStop, "mailbox new"):
            verify_zero_residue(self.baseline, post)

    def test_multiple_simultaneous_mismatches_are_all_named(self) -> None:
        self._mutate()
        # do NOT restore -- everything stays mutated
        post = record_baseline(self.target, runner=self.runner)
        with self.assertRaises(HardStop) as ctx:
            verify_zero_residue(self.baseline, post)
        message = str(ctx.exception)
        for expected in ("writer-a.timer", "recovery-scope.scope", "settings.json", "persona_state.tar"):
            self.assertIn(expected, message)

    def test_disposable_marker_created_during_a_run_is_cleaned_by_restore(self) -> None:
        self.marker_path.write_text("created mid-run")
        restore_to(self.baseline, runner=self.runner)
        self.assertFalse(self.marker_path.exists())


class RestorePayloadEvidenceIntegrationTest(unittest.TestCase):
    def test_restore_payload_round_trips_through_evidence_write_restore(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = {"writer-a.timer": _unit_state(active="active", enabled="enabled")}
            runner = _make_fake_systemctl(state)
            target = BaselineTarget(
                account="test-account", unit_names=("writer-a.timer",), backup_root=root / "backups"
            )
            baseline = record_baseline(target, runner=runner)
            post = record_baseline(target, runner=runner)
            payload = restore_payload(baseline, post, restored=True)
            paths = RunPaths(root=root / "evidence", run_id="run-1")
            write_restore(paths, {"restore-primitive": payload})
            observed = read_restore(paths)
            self.assertTrue(observed["modules"]["restore-primitive"]["restored"])
            self.assertEqual(
                observed["modules"]["restore-primitive"]["baseline"]["unit_states"]["writer-a.timer"][
                    "ActiveState"
                ],
                "active",
            )
            self.assertIn("post", observed["modules"]["restore-primitive"])


class PathScopingSafetyTest(unittest.TestCase):
    """The primitive must be fully scoped to what BaselineTarget names -- no
    hardcoded real-mailbox / canonical-clone / real-account path or account
    name anywhere in record_baseline/restore_to/verify_zero_residue. This is
    the safety guarantee every live module's disposable-hub caller rests on
    (per orch design-review, relayed by team-lead)."""

    def test_only_target_declared_resources_are_touched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account = "disposable-test-account-zzz"  # deliberately not any real Pantheon account
            state = {
                "disposable-writer.timer": _unit_state(active="active", enabled="enabled"),
                "disposable-scope.scope": _unit_state(active="inactive", enabled="disabled"),
            }
            inner_runner = _make_fake_systemctl(state)
            calls: list[tuple[str, ...]] = []

            def recording_runner(command: Sequence[str]) -> CompletedProcess[str]:
                command = tuple(command)
                calls.append(command)
                return inner_runner(command)

            settings_path = root / "settings.json"
            settings_path.write_bytes(b"original-disposable-bytes")
            mailbox_dir = root / "mailbox" / "new"
            mailbox_dir.mkdir(parents=True)
            marker_path = root / "marker.flag"
            backup_root = root / "backups"

            target = BaselineTarget(
                account=account,
                unit_names=("disposable-writer.timer",),
                runtime_scope_units=("disposable-scope.scope",),
                mailbox_dirs={"new": mailbox_dir},
                settings_paths=(settings_path,),
                disposable_markers=(marker_path,),
                backup_root=backup_root,
            )

            written_paths: list[Path] = []
            unlinked_paths: list[Path] = []
            rmtree_paths: list[Path] = []

            original_atomic_write = restore_module._atomic_write_bytes
            original_unlink = Path.unlink
            original_rmtree = restore_module.shutil.rmtree

            def recording_atomic_write(path: Path, data: bytes) -> None:
                written_paths.append(Path(path))
                return original_atomic_write(path, data)

            def recording_unlink(self: Path, *args: object, **kwargs: object) -> None:
                unlinked_paths.append(self)
                return original_unlink(self, *args, **kwargs)

            def recording_rmtree(path: object, *args: object, **kwargs: object) -> None:
                rmtree_paths.append(Path(path))
                return original_rmtree(path, *args, **kwargs)

            with (
                patch.object(restore_module, "_atomic_write_bytes", side_effect=recording_atomic_write),
                patch.object(Path, "unlink", new=recording_unlink),
                patch.object(restore_module.shutil, "rmtree", side_effect=recording_rmtree),
            ):
                baseline = record_baseline(target, runner=recording_runner)
                # Mutate every channel the cycle can revert, to force restore_to
                # to actually exercise every write/unlink/rmtree path.
                state["disposable-writer.timer"]["ActiveState"] = "inactive"
                state["disposable-scope.scope"]["UnitFileState"] = "enabled"
                settings_path.write_bytes(b"tampered-disposable-bytes")
                marker_path.write_text("residue")
                restore_to(baseline, runner=recording_runner)
                post = record_baseline(target, runner=recording_runner)
                verify_zero_residue(baseline, post)

            # 1. every systemctl call named exactly the passed-in account -- never
            #    a different (e.g. real) account silently substituted in.
            allowed_units = set(target.unit_names) | set(target.runtime_scope_units)
            self.assertTrue(calls, "expected at least one systemctl call")
            for command in calls:
                self.assertIn(f"--machine={account}@.host", command)
                self.assertIn(command[4], allowed_units)

            # 2. every file write stayed under backup_root or was the declared settings path.
            self.assertTrue(written_paths, "expected at least one settings write")
            for path in written_paths:
                self.assertTrue(path.is_relative_to(root), f"write escaped target root: {path}")
                self.assertTrue(
                    path.is_relative_to(backup_root) or path == settings_path,
                    f"write outside declared target: {path}",
                )

            # 3. every unlink was the declared settings path or a declared marker.
            self.assertTrue(unlinked_paths, "expected at least one unlink (the created marker)")
            allowed_unlink = {settings_path, marker_path}
            for path in unlinked_paths:
                self.assertTrue(path.is_relative_to(root), f"unlink escaped target root: {path}")
                self.assertIn(path, allowed_unlink, f"unlink outside declared target: {path}")

            # 4. no directory marker in this fixture -- rmtree must never fire.
            self.assertEqual(rmtree_paths, [])


if __name__ == "__main__":
    unittest.main()
