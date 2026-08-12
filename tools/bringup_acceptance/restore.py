"""tools/bringup_acceptance/restore.py — shared exact-restore + baseline primitive (T-2556 phase 3).

Every live bringup-acceptance module calls this before it does anything and
after it is done: record a quiet baseline, do whatever the module needs to
do, then restore the target byte-for-byte and verify zero residue. Any
anomaly -- a unit that won't revert, a settings file that won't match its
original bytes, a marker the module left behind, a mailbox count that moved
-- is a HardStop, never a soft warning, per T-2556's fail-closed rigor bar.

Generalized from the T-2382 live-E acceptance run's manual baseline/restore
discipline (record unit active/enabled states + mailbox counts + settings
byte-backups + runtime scope -> restore -> diff -> zero residue or abort;
see tickets/done/T-2382.md's transition log). This module reimplements its
own small systemctl-property parser and sha256 helper rather than importing
recorder.py's or t2382_live_acceptance.py's private ones -- the same
independence recorder.py's own docstring claims for its relationship to
t2382_live_acceptance.py: a clean generalization, not a shared-private-
helper coupling (tracked-acceptable duplication, same spirit as T-2558).

WHAT restore_to CANNOT DO, ON PURPOSE. Reverting a systemd unit's
active/enabled state or a settings file's bytes is mechanical and generic.
Reverting a drained mailbox message is not -- nobody can generically
fabricate the original message content back into a maildir, only the
calling module (which knows what it drained and how) can undo that. So
restore_to() never touches mailbox_dirs; they are still baseline-recorded,
and verify_zero_residue() still catches any count drift as residue. Fail-
closed survives this gap because verify still raises; it just can't
self-heal every category.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from tools.bringup_acceptance.types import HardStop


CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

UNIT_PROPERTIES: tuple[str, ...] = ("LoadState", "ActiveState", "SubState", "UnitFileState")


def default_command_runner(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        tuple(command), check=False, capture_output=True, text=True, timeout=20,
    )


def _parse_key_value_lines(raw: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key] = value
    return values


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """tmpfile + fsync + rename + dir fsync -- same atomicity as recorder.write_json_evidence,
    but for raw bytes since settings files aren't always JSON."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


@dataclasses.dataclass(frozen=True)
class BaselineTarget:
    """What one baseline call snapshots and restores.

    Purely descriptive: naming which units/dirs/files/markers matter is the
    calling module's job; this primitive owns the mechanics of capturing
    and reverting them. ``runtime_scope_units`` is mechanically identical to
    ``unit_names`` (both are systemd units queried the same way) but kept as
    its own field because T-2560's ask names runtime-scope state as its own
    baseline category, and a distinct field keeps that category legible in
    the recorded evidence.
    """

    account: str
    unit_names: tuple[str, ...] = ()
    runtime_scope_units: tuple[str, ...] = ()
    mailbox_dirs: Mapping[str, Path] = dataclasses.field(default_factory=dict)
    settings_paths: tuple[Path, ...] = ()
    disposable_markers: tuple[Path, ...] = ()
    backup_root: Path = Path(".")


@dataclasses.dataclass(frozen=True)
class FileSnapshot:
    """One settings file's baseline: whether it existed, its content hash, and where
    the byte-for-byte backup copy lives (empty strings when it didn't exist)."""

    existed: bool
    sha256: str
    backup_path: str


@dataclasses.dataclass(frozen=True)
class Baseline:
    target: BaselineTarget
    unit_states: Mapping[str, Mapping[str, str]]
    runtime_scope_states: Mapping[str, Mapping[str, str]]
    mailbox_counts: Mapping[str, int]
    settings: Mapping[str, FileSnapshot]
    disposable_markers: Mapping[str, bool]


def _query_unit(runner: CommandRunner, *, account: str, unit: str) -> Mapping[str, str]:
    command = ["systemctl", "--user", f"--machine={account}@.host", "show", unit]
    command.extend(f"--property={name}" for name in UNIT_PROPERTIES)
    command.append("--no-pager")
    result = runner(tuple(command))
    if result.returncode != 0:
        raise HardStop(f"cannot inspect systemd unit {unit}: {result.stderr.strip()}")
    values = _parse_key_value_lines(result.stdout)
    missing = [name for name in UNIT_PROPERTIES if name not in values]
    if missing:
        raise HardStop(f"systemd observation of {unit} lacks: " + ", ".join(missing))
    return {name: values[name] for name in UNIT_PROPERTIES}


def _apply_unit_action(runner: CommandRunner, *, account: str, unit: str, action: str) -> None:
    command = ("systemctl", "--user", f"--machine={account}@.host", action, unit)
    result = runner(command)
    if result.returncode != 0:
        raise HardStop(f"restore action {action} failed for {unit}: {result.stderr.strip()}")


def _mailbox_count(directory: Path) -> int:
    if not directory.exists():
        return 0
    return sum(1 for entry in directory.iterdir() if entry.is_file())


def _snapshot_settings_file(path: Path, *, backup_root: Path) -> FileSnapshot:
    if not path.exists():
        return FileSnapshot(existed=False, sha256="", backup_path="")
    data = path.read_bytes()
    digest = _sha256_bytes(data)
    backup_path = backup_root / f"{path.name}.{digest[:16]}.bak"
    _atomic_write_bytes(backup_path, data)
    return FileSnapshot(existed=True, sha256=digest, backup_path=str(backup_path))


def record_baseline(target: BaselineTarget, *, runner: CommandRunner = default_command_runner) -> Baseline:
    """Snapshot every category `target` names. Never mutates anything."""

    unit_states = {
        name: _query_unit(runner, account=target.account, unit=name) for name in target.unit_names
    }
    runtime_scope_states = {
        name: _query_unit(runner, account=target.account, unit=name)
        for name in target.runtime_scope_units
    }
    mailbox_counts = {
        label: _mailbox_count(directory) for label, directory in target.mailbox_dirs.items()
    }
    settings = {
        str(path): _snapshot_settings_file(path, backup_root=target.backup_root)
        for path in target.settings_paths
    }
    disposable_markers = {str(path): path.exists() for path in target.disposable_markers}
    return Baseline(
        target=target,
        unit_states=unit_states,
        runtime_scope_states=runtime_scope_states,
        mailbox_counts=mailbox_counts,
        settings=settings,
        disposable_markers=disposable_markers,
    )


def _restore_settings(settings: Mapping[str, FileSnapshot]) -> None:
    for path_str, snapshot in settings.items():
        path = Path(path_str)
        if snapshot.existed:
            data = Path(snapshot.backup_path).read_bytes()
            _atomic_write_bytes(path, data)
            if _sha256_bytes(path.read_bytes()) != snapshot.sha256:
                raise HardStop(f"restore of {path_str} did not reproduce the recorded bytes")
        elif path.exists():
            path.unlink()


def _restore_units(
    states: Mapping[str, Mapping[str, str]], *, account: str, runner: CommandRunner
) -> None:
    for unit, want in states.items():
        _apply_unit_action(
            runner, account=account, unit=unit,
            action="start" if want["ActiveState"] == "active" else "stop",
        )
        _apply_unit_action(
            runner, account=account, unit=unit,
            action="enable" if want["UnitFileState"] == "enabled" else "disable",
        )


def _restore_disposable_markers(markers: Mapping[str, bool]) -> None:
    for path_str, existed in markers.items():
        path = Path(path_str)
        if not existed and path.exists():
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()


def restore_to(baseline: Baseline, *, runner: CommandRunner = default_command_runner) -> None:
    """Revert everything the baseline recorded, except mailbox_dirs (see module docstring)."""

    _restore_settings(baseline.settings)
    _restore_units(baseline.unit_states, account=baseline.target.account, runner=runner)
    _restore_units(baseline.runtime_scope_states, account=baseline.target.account, runner=runner)
    _restore_disposable_markers(baseline.disposable_markers)


def verify_zero_residue(baseline: Baseline, post: Baseline) -> None:
    """Diff every recorded category; raise HardStop naming every mismatch, not just the first."""

    residue: list[str] = []

    for name, want in baseline.unit_states.items():
        got = post.unit_states.get(name)
        if got != want:
            residue.append(f"unit {name}: expected {want} got {got}")

    for name, want in baseline.runtime_scope_states.items():
        got = post.runtime_scope_states.get(name)
        if got != want:
            residue.append(f"runtime_scope {name}: expected {want} got {got}")

    for mbox_label, want_count in baseline.mailbox_counts.items():
        got_count = post.mailbox_counts.get(mbox_label)
        if got_count != want_count:
            residue.append(f"mailbox {mbox_label}: expected count {want_count} got {got_count}")

    for path_str, want_snapshot in baseline.settings.items():
        got_snapshot = post.settings.get(path_str)
        if (
            got_snapshot is None
            or got_snapshot.existed != want_snapshot.existed
            or got_snapshot.sha256 != want_snapshot.sha256
        ):
            residue.append(f"settings {path_str}: expected {want_snapshot} got {got_snapshot}")

    for path_str, want_existed in baseline.disposable_markers.items():
        got_existed = post.disposable_markers.get(path_str)
        if got_existed != want_existed:
            residue.append(f"marker {path_str}: expected existed={want_existed} got {got_existed}")

    if residue:
        raise HardStop("restore residue: " + "; ".join(residue))


def restore_payload(baseline: Baseline, post: Baseline, *, restored: bool) -> Mapping[str, Any]:
    """JSON-safe payload for one module's restore.json entry (baseline vs post).

    Not written to disk here -- the caller merges this into the run's module-keyed
    restore_by_module mapping and passes it to evidence.write_restore, keeping this
    primitive callable outside a full RunContext (the self-proving tests need that).
    """

    return {"baseline": baseline, "post": post, "restored": restored}
