"""Descriptor-anchored readers for root-owned host-control configuration."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Mapping

from tools.host_control.contracts import RegistryError


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW


def _validate_stat(
    value: os.stat_result,
    *,
    expected_owner_uid: int,
    kind: str,
) -> None:
    if value.st_uid != expected_owner_uid:
        raise RegistryError(f"untrusted {kind} owner")
    if value.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RegistryError(f"untrusted {kind} mode")
    if kind == "directory" and not stat.S_ISDIR(value.st_mode):
        raise RegistryError("trusted path component is not a directory")
    if kind == "file" and not stat.S_ISREG(value.st_mode):
        raise RegistryError("trusted final object is not a regular file")


def _normalized_absolute(path: Path) -> Path:
    raw = os.fspath(path)
    if not os.path.isabs(raw):
        raise RegistryError("trusted path must be absolute")
    normalized = os.path.normpath(raw)
    if normalized != raw or ".." in Path(raw).parts:
        raise RegistryError("trusted path must be normalized")
    return Path(normalized)


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RegistryError("duplicate JSON key")
        result[key] = value
    return result


class RootOwnedFileLoader:
    """Retain trusted root descriptors and read descendants without symlinks."""

    def __init__(
        self,
        roots: Mapping[Path, int],
        *,
        expected_owner_uid: int,
    ) -> None:
        self._roots = dict(roots)
        self._expected_owner_uid = expected_owner_uid
        self._closed = False

    @classmethod
    def open(cls, trusted_roots: tuple[Path, ...]) -> "RootOwnedFileLoader":
        return cls._open_roots(
            trusted_roots,
            expected_owner_uid=0,
            verify_ancestors=True,
        )

    @classmethod
    def _open_for_test(
        cls,
        trusted_roots: tuple[Path, ...],
        *,
        expected_owner_uid: int,
    ) -> "RootOwnedFileLoader":
        return cls._open_roots(
            trusted_roots,
            expected_owner_uid=expected_owner_uid,
            verify_ancestors=False,
        )

    @classmethod
    def _open_roots(
        cls,
        trusted_roots: tuple[Path, ...],
        *,
        expected_owner_uid: int,
        verify_ancestors: bool,
    ) -> "RootOwnedFileLoader":
        if not trusted_roots:
            raise RegistryError("at least one trusted root is required")
        opened: dict[Path, int] = {}
        try:
            for candidate in trusted_roots:
                root = _normalized_absolute(candidate)
                if root in opened:
                    raise RegistryError("duplicate trusted root")
                if verify_ancestors:
                    descriptor = cls._open_root_by_components(
                        root,
                        expected_owner_uid=expected_owner_uid,
                    )
                else:
                    descriptor = os.open(root, _DIRECTORY_FLAGS)
                    try:
                        _validate_stat(
                            os.fstat(descriptor),
                            expected_owner_uid=expected_owner_uid,
                            kind="directory",
                        )
                    except BaseException:
                        os.close(descriptor)
                        raise
                opened[root] = descriptor
            return cls(opened, expected_owner_uid=expected_owner_uid)
        except OSError as exc:
            for descriptor in opened.values():
                os.close(descriptor)
            raise RegistryError("cannot open trusted root") from exc
        except BaseException:
            for descriptor in opened.values():
                os.close(descriptor)
            raise

    @staticmethod
    def _open_root_by_components(
        root: Path,
        *,
        expected_owner_uid: int,
    ) -> int:
        descriptor = os.open("/", _DIRECTORY_FLAGS)
        try:
            _validate_stat(
                os.fstat(descriptor),
                expected_owner_uid=expected_owner_uid,
                kind="directory",
            )
            for component in root.parts[1:]:
                next_descriptor = os.open(
                    component,
                    _DIRECTORY_FLAGS,
                    dir_fd=descriptor,
                )
                try:
                    _validate_stat(
                        os.fstat(next_descriptor),
                        expected_owner_uid=expected_owner_uid,
                        kind="directory",
                    )
                except BaseException:
                    os.close(next_descriptor)
                    raise
                os.close(descriptor)
                descriptor = next_descriptor
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for descriptor in self._roots.values():
            os.close(descriptor)
        self._roots.clear()

    def __enter__(self) -> "RootOwnedFileLoader":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def is_allowed(self, path: Path) -> bool:
        try:
            self._select_root(_normalized_absolute(path))
        except RegistryError:
            return False
        return True

    def _select_root(self, path: Path) -> tuple[Path, int, tuple[str, ...]]:
        if self._closed:
            raise RegistryError("trusted file loader is closed")
        matches: list[tuple[Path, int, tuple[str, ...]]] = []
        for root, descriptor in self._roots.items():
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if not relative.parts:
                raise RegistryError("trusted root is not a regular file")
            matches.append((root, descriptor, relative.parts))
        if not matches:
            raise RegistryError("path is outside trusted roots")
        return max(matches, key=lambda item: len(item[0].parts))

    def read_bytes(self, path: Path, *, max_bytes: int) -> bytes:
        if not isinstance(max_bytes, int) or max_bytes < 1:
            raise RegistryError("invalid read size limit")
        normalized = _normalized_absolute(path)
        _, root_descriptor, components = self._select_root(normalized)
        parent = os.dup(root_descriptor)
        file_descriptor: int | None = None
        try:
            for component in components[:-1]:
                next_descriptor = os.open(
                    component,
                    _DIRECTORY_FLAGS,
                    dir_fd=parent,
                )
                try:
                    _validate_stat(
                        os.fstat(next_descriptor),
                        expected_owner_uid=self._expected_owner_uid,
                        kind="directory",
                    )
                except BaseException:
                    os.close(next_descriptor)
                    raise
                os.close(parent)
                parent = next_descriptor

            final_name = components[-1]
            file_descriptor = os.open(
                final_name,
                _FILE_FLAGS,
                dir_fd=parent,
            )
            opened_stat = os.fstat(file_descriptor)
            _validate_stat(
                opened_stat,
                expected_owner_uid=self._expected_owner_uid,
                kind="file",
            )
            if opened_stat.st_size > max_bytes:
                raise RegistryError("trusted file exceeds size limit")

            chunks: list[bytes] = []
            remaining = opened_stat.st_size
            while remaining:
                chunk = os.read(file_descriptor, min(remaining, 65536))
                if not chunk:
                    raise RegistryError("short read from trusted file")
                chunks.append(chunk)
                remaining -= len(chunk)
                if remaining < 0 or sum(map(len, chunks)) > max_bytes:
                    raise RegistryError("trusted file exceeds size limit")
            if os.read(file_descriptor, 1):
                raise RegistryError("trusted file grew while reading")

            final_stat = os.fstat(file_descriptor)
            path_stat = os.stat(
                final_name,
                dir_fd=parent,
                follow_symlinks=False,
            )
            identity = (opened_stat.st_dev, opened_stat.st_ino)
            if (
                (final_stat.st_dev, final_stat.st_ino) != identity
                or (path_stat.st_dev, path_stat.st_ino) != identity
                or final_stat.st_size != opened_stat.st_size
            ):
                raise RegistryError("trusted path changed while reading")
            return b"".join(chunks)
        except (OSError, ValueError) as exc:
            raise RegistryError("cannot read trusted file") from exc
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            os.close(parent)

    def read_json(
        self,
        path: Path,
        *,
        max_bytes: int,
    ) -> Mapping[str, object]:
        raw = self.read_bytes(path, max_bytes=max_bytes)
        try:
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_no_duplicate_object,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegistryError("invalid trusted JSON") from exc
        if not isinstance(value, dict):
            raise RegistryError("trusted JSON root must be an object")
        return value
