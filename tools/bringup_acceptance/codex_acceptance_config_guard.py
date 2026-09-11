"""Reversible Codex config guard for bringup acceptance drivers.

The acceptance drivers intentionally launch the real Codex runtime path, and
``runtime_controller._start_runtime`` seeds user-level Codex config before
tmux starts. This guard makes those acceptance-only mutations explicitly
owned and reversible without weakening the installer rollback drift checks.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import shutil
import stat
import tempfile
import tomllib
from pathlib import Path
from typing import Callable, Sequence

from tools import runtime_controller as rc


@dataclasses.dataclass(frozen=True)
class ConfigBytesSnapshot:
    existed: bool
    content: bytes
    sha256: str | None
    mode: int | None
    uid: int | None
    gid: int | None


@dataclasses.dataclass(frozen=True)
class StartupConfigWriterResult:
    name: str
    prefix_index: int
    before: ConfigBytesSnapshot
    after: ConfigBytesSnapshot
    modified: bool


@dataclasses.dataclass(frozen=True)
class StartupConfigPrefixSnapshot:
    prefix_index: int
    writer_names: tuple[str, ...]
    snapshot: ConfigBytesSnapshot


@dataclasses.dataclass(frozen=True)
class AcceptanceConfigExpectation:
    config_path: Path
    project_cwd: Path
    before: ConfigBytesSnapshot
    prefixes: tuple[StartupConfigPrefixSnapshot, ...]
    writer_results: tuple[StartupConfigWriterResult, ...]
    owner: str

    @property
    def expected_after(self) -> ConfigBytesSnapshot:
        return self.prefixes[-1].snapshot


class AcceptanceConfigGuardError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        config_path: Path,
        before_sha256: str | None = None,
        current_sha256: str | None = None,
        accepted_prefixes: tuple[str, ...] = (),
        original_exception: BaseException | None = None,
        teardown_residue: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.config_path = config_path
        self.before_sha256 = before_sha256
        self.current_sha256 = current_sha256
        self.accepted_prefixes = accepted_prefixes
        self.original_exception = original_exception
        self.teardown_residue = tuple(teardown_residue)


_SEMVER_RE = re.compile(r"\bcodex-cli\s+(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b")


def _hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _validate_toml_bytes(content: bytes, *, label: str) -> None:
    if not content.strip():
        return
    try:
        tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise AcceptanceConfigGuardError(
            f"codex acceptance config guard refuses unparseable {label}",
            config_path=Path("config.toml"),
        ) from exc


def verify_regular_config_target(config_path: Path) -> None:
    parent = config_path.parent
    if parent.exists() and parent.is_symlink():
        raise AcceptanceConfigGuardError(
            "codex acceptance config guard refuses symlink config parent",
            config_path=config_path,
        )
    try:
        st = config_path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise AcceptanceConfigGuardError(
            "codex acceptance config guard refuses symlink config target",
            config_path=config_path,
        )
    if not stat.S_ISREG(st.st_mode):
        raise AcceptanceConfigGuardError(
            "codex acceptance config guard refuses non-regular config target",
            config_path=config_path,
        )


def snapshot_config_bytes(config_path: Path) -> ConfigBytesSnapshot:
    verify_regular_config_target(config_path)
    try:
        st = config_path.stat()
        content = config_path.read_bytes()
    except FileNotFoundError:
        return ConfigBytesSnapshot(False, b"", None, None, None, None)
    _validate_toml_bytes(content, label="config.toml")
    return ConfigBytesSnapshot(
        True,
        content,
        _hash_bytes(content),
        stat.S_IMODE(st.st_mode),
        st.st_uid,
        st.st_gid,
    )


def _snapshot_scratch_config(codex_home: Path) -> ConfigBytesSnapshot:
    return snapshot_config_bytes(codex_home / "config.toml")


def _startup_writers() -> tuple[tuple[str, Callable[[Path, Path], bool]], ...]:
    return (
        (
            "ensure_project_trusted",
            lambda codex_home, project_cwd: rc.ensure_project_trusted(codex_home, project_cwd),
        ),
        (
            "ensure_update_check_disabled",
            lambda codex_home, _project_cwd: rc.ensure_update_check_disabled(codex_home),
        ),
        (
            "ensure_model_migration_prompts_hidden",
            lambda codex_home, _project_cwd: rc.ensure_model_migration_prompts_hidden(codex_home),
        ),
    )


def replay_runtime_startup_config_writer_prefixes(
    *,
    codex_home: Path,
    project_cwd: Path,
) -> tuple[tuple[StartupConfigPrefixSnapshot, ...], tuple[StartupConfigWriterResult, ...]]:
    before = snapshot_config_bytes(codex_home / "config.toml")
    with tempfile.TemporaryDirectory(prefix="codex-acceptance-config-") as tmp:
        scratch_home = Path(tmp) / "codex-home"
        scratch_home.mkdir(parents=True)
        if before.existed:
            scratch_config = scratch_home / "config.toml"
            scratch_config.write_bytes(before.content)
            if before.mode is not None:
                os.chmod(scratch_config, before.mode)
            if before.uid is not None and before.gid is not None:
                try:
                    os.chown(scratch_config, before.uid, before.gid)
                except PermissionError as exc:
                    raise AcceptanceConfigGuardError(
                        "codex acceptance config guard cannot mirror before-"
                        "ownership into scratch replay -- refusing to compare "
                        "drift under a wrong ownership fingerprint",
                        config_path=scratch_config,
                    ) from exc
        models_cache = codex_home / "models_cache.json"
        if models_cache.exists() and not models_cache.is_symlink() and models_cache.is_file():
            shutil.copy2(models_cache, scratch_home / "models_cache.json")

        prefixes: list[StartupConfigPrefixSnapshot] = [
            StartupConfigPrefixSnapshot(0, (), _snapshot_scratch_config(scratch_home))
        ]
        results: list[StartupConfigWriterResult] = []
        writer_names: list[str] = []
        for index, (name, writer) in enumerate(_startup_writers(), start=1):
            writer_before = _snapshot_scratch_config(scratch_home)
            writer(scratch_home, project_cwd)
            writer_after = _snapshot_scratch_config(scratch_home)
            writer_names.append(name)
            prefixes.append(
                StartupConfigPrefixSnapshot(index, tuple(writer_names), writer_after)
            )
            results.append(
                StartupConfigWriterResult(
                    name=name,
                    prefix_index=index,
                    before=writer_before,
                    after=writer_after,
                    modified=writer_before.content != writer_after.content
                    or writer_before.existed != writer_after.existed,
                )
            )
        return tuple(prefixes), tuple(results)


def build_startup_config_expectation(
    *,
    codex_home: Path,
    project_cwd: Path,
    owner: str,
) -> AcceptanceConfigExpectation:
    config_path = codex_home / "config.toml"
    before = snapshot_config_bytes(config_path)
    prefixes, results = replay_runtime_startup_config_writer_prefixes(
        codex_home=codex_home,
        project_cwd=project_cwd,
    )
    return AcceptanceConfigExpectation(
        config_path=config_path,
        project_cwd=project_cwd,
        before=before,
        prefixes=prefixes,
        writer_results=results,
        owner=owner,
    )


def _snapshot_matches(left: ConfigBytesSnapshot, right: ConfigBytesSnapshot) -> bool:
    return (
        left.existed == right.existed
        and left.content == right.content
        and left.mode == right.mode
        and left.uid == right.uid
        and left.gid == right.gid
    )


def _safe_unlink_absent_restore(config_path: Path) -> ConfigBytesSnapshot:
    verify_regular_config_target(config_path)
    try:
        config_path.unlink()
    except FileNotFoundError:
        pass
    if config_path.parent.exists():
        _fsync_dir(config_path.parent)
    return snapshot_config_bytes(config_path)


def restore_config_snapshot_atomically(
    *,
    config_path: Path,
    desired: ConfigBytesSnapshot,
) -> ConfigBytesSnapshot:
    verify_regular_config_target(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not desired.existed:
        restored = _safe_unlink_absent_restore(config_path)
        if restored.existed:
            raise AcceptanceConfigGuardError(
                "codex acceptance config guard post-restore absence verification failed",
                config_path=config_path,
                before_sha256=desired.sha256,
                current_sha256=restored.sha256,
            )
        return restored

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.acceptance-restore.",
        dir=str(config_path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            if desired.mode is not None:
                os.fchmod(handle.fileno(), desired.mode)
            if desired.uid is not None and desired.gid is not None:
                current_owner = os.fstat(handle.fileno())
                if current_owner.st_uid != desired.uid or current_owner.st_gid != desired.gid:
                    try:
                        os.fchown(handle.fileno(), desired.uid, desired.gid)
                    except PermissionError as exc:
                        raise AcceptanceConfigGuardError(
                            "codex acceptance config guard cannot preserve config ownership",
                            config_path=config_path,
                            before_sha256=desired.sha256,
                        ) from exc
            handle.write(desired.content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, config_path)
        _fsync_dir(config_path.parent)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
    restored = snapshot_config_bytes(config_path)
    if not _snapshot_matches(restored, desired):
        raise AcceptanceConfigGuardError(
            "codex acceptance config guard post-restore fingerprint verification failed",
            config_path=config_path,
            before_sha256=desired.sha256,
            current_sha256=restored.sha256,
        )
    return restored


class ReversibleAcceptanceConfigGuard:
    def __init__(self, *, codex_home: Path, project_cwd: Path, owner: str) -> None:
        self.codex_home = codex_home
        self.project_cwd = project_cwd
        self.owner = owner
        self.expectation: AcceptanceConfigExpectation | None = None
        self._restored = False
        self._teardown_residue: tuple[str, ...] = ()

    def __enter__(self) -> AcceptanceConfigExpectation:
        self.expectation = build_startup_config_expectation(
            codex_home=self.codex_home,
            project_cwd=self.project_cwd,
            owner=self.owner,
        )
        return self.expectation

    def attach_teardown_residue(self, residue: Sequence[str]) -> None:
        self._teardown_residue = tuple(residue)

    def restore(self) -> None:
        if self.expectation is None:
            raise AcceptanceConfigGuardError(
                "codex acceptance config guard restore called before enter",
                config_path=self.codex_home / "config.toml",
            )
        current = snapshot_config_bytes(self.expectation.config_path)
        accepted = [
            prefix
            for prefix in self.expectation.prefixes
            if _snapshot_matches(current, prefix.snapshot)
        ]
        if not accepted:
            raise AcceptanceConfigGuardError(
                "codex acceptance config guard refuses unexpected config drift",
                config_path=self.expectation.config_path,
                before_sha256=self.expectation.before.sha256,
                current_sha256=current.sha256,
                accepted_prefixes=tuple(
                    f"{prefix.prefix_index}:{','.join(prefix.writer_names) or 'before'}"
                    for prefix in self.expectation.prefixes
                ),
                teardown_residue=self._teardown_residue,
            )
        restore_config_snapshot_atomically(
            config_path=self.expectation.config_path,
            desired=self.expectation.before,
        )
        self._restored = True

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        try:
            self.restore()
        except AcceptanceConfigGuardError as restore_exc:
            if isinstance(exc, BaseException):
                restore_exc.original_exception = exc
            raise
        return False


def parse_codex_cli_semver(text: str) -> str | None:
    match = _SEMVER_RE.search(text)
    return match.group(1) if match else None
