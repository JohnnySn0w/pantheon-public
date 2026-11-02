"""Seat-root file materialization, descriptor-anchored (T-2917 5b).

Every path is resolved component by component from ``/`` with
``O_NOFOLLOW | O_DIRECTORY`` directory descriptors, and every write lands
relative to the parent's descriptor, so nothing here ever follows a symlink
or can be redirected by a concurrent path swap. Refused, fail closed:

* a target that is not strictly inside the seat root (``..``, relative,
  un-normalized, the seat root itself, or anything outside it);
* a symlink anywhere on the path, INCLUDING the final component (the
  installer's local backend replaces a final link; the executor refuses it);
* any existing component at or below the seat root, or the target itself,
  not owned by the executor uid (owner mismatch);
* modes with setuid/setgid/sticky bits; a write whose parent is missing (the
  plan creates directories with explicit ``mkdir`` operations first).

Snapshots use the persona installer's ``_snapshot`` shape so broker
receipts stay byte-compatible with receipt-snapshot rollback.

Rollback restores (T-2917 5d.2) use the same anchoring and act only on the
EXACT object the caller classified: ``restore_file`` and ``remove_file``
require the current file's sha256, ``remove_symlink`` the link's exact
readlink text, and ``rmdir_if_empty`` never removes a non-empty directory
(nothing here deletes recursively).

T-5608 (the residue quarantine) adds ``rename_within``: a move of one entry
to a new name, both ends inside the seat root and both parents walked the
same way; it refuses an existing destination and never deletes. Plus two
reads, ``lexists`` and ``list_dir``.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import os
import secrets
import stat
from pathlib import Path
from typing import Final

from tools.host_control.contracts import HostControlError

_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
# O_NONBLOCK: opening a FIFO (or other special file) a seat process planted
# at a read target must return at once so the S_ISREG check below refuses
# it; without it the open blocks the executor worker forever (review M1).
_READ_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_WRITE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
DEFAULT_MAX_FILE_BYTES: Final = 512 * 1024


class SeatPathRefused(HostControlError):
    """The path or object fails a seat-root containment/ownership rule."""


class _ParentMissing(Exception):
    pass


class SeatChangeIncomplete(HostControlError):
    """The change COMMITTED (rename or mkdir succeeded) but a later step
    failed (parent fsync, chmod, owner re-check). The caller must still
    record the mutation so the receipt can drive rollback (review M2)."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_mode(raw: object, default: str) -> int:
    text = default if raw in (None, "") else raw
    if not isinstance(text, str) or not text.isdigit() or len(text) > 4:
        raise SeatPathRefused("invalid file mode")
    try:
        mode = int(text, 8)
    except ValueError as exc:
        raise SeatPathRefused("invalid file mode") from exc
    if mode & ~0o777:
        raise SeatPathRefused("file mode carries setuid, setgid or sticky bits")
    return mode


class SeatFiles:
    def __init__(
        self,
        *,
        seat_root: Path,
        owner_uid: int,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        raw = os.fspath(seat_root)
        # "//"-prefixed paths survive normpath unchanged (POSIX); refused (D32)
        if not os.path.isabs(raw) or raw.startswith("//") or os.path.normpath(raw) != raw or raw == "/":
            raise SeatPathRefused("seat root must be an absolute, normalized, non-root path")
        self.seat_root = Path(raw)
        self.owner_uid = int(owner_uid)
        self.max_file_bytes = int(max_file_bytes)

    # ------------------------------------------------------------------
    # containment
    # ------------------------------------------------------------------

    def contains(self, target: str) -> bool:
        try:
            self._relative(target)
        except SeatPathRefused:
            return False
        return True

    def _relative(self, target: object) -> tuple[str, ...]:
        if not isinstance(target, str) or not os.path.isabs(target):
            raise SeatPathRefused("target must be an absolute path")
        if target.startswith("//") or os.path.normpath(target) != target or ".." in Path(target).parts:
            raise SeatPathRefused("target must be normalized")
        try:
            relative = Path(target).relative_to(self.seat_root)
        except ValueError as exc:
            raise SeatPathRefused("target is outside the seat root") from exc
        if not relative.parts:
            raise SeatPathRefused("target is the seat root itself")
        return relative.parts

    def _check_owner(self, value: os.stat_result, what: str) -> None:
        if value.st_uid != self.owner_uid:
            raise SeatPathRefused(f"{what} owner mismatch")

    def _open_parent(self, parts: tuple[str, ...]) -> int:
        """Descriptor of the target's parent; raises _ParentMissing."""

        descriptor = os.open("/", _DIRECTORY_FLAGS)
        try:
            for component in self.seat_root.parts[1:]:
                child = self._open_child(descriptor, component, owner_checked=False)
                os.close(descriptor)
                descriptor = child
            self._check_owner(os.fstat(descriptor), "seat root")
            for component in parts[:-1]:
                child = self._open_child(descriptor, component, owner_checked=True)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _open_child(self, descriptor: int, component: str, *, owner_checked: bool) -> int:
        """Open one directory component under ``descriptor`` (which the
        caller still owns and closes)."""

        try:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
        except FileNotFoundError as exc:
            raise _ParentMissing() from exc
        except OSError as exc:
            # ELOOP (a symlink under O_NOFOLLOW) or ENOTDIR: never traversed.
            raise SeatPathRefused("path component is a symlink or not a directory") from exc
        if owner_checked:
            try:
                self._check_owner(os.fstat(child), "path component")
            except BaseException:
                os.close(child)
                raise
        return child

    def _final_stat(self, parent: int, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def kind(self, target: str) -> str:
        """``absent`` | ``directory`` | ``file`` | ``symlink`` | ``other``;
        an existing directory/file must be owned by the executor uid."""

        parts = self._relative(target)
        try:
            parent = self._open_parent(parts)
        except _ParentMissing:
            return "absent"
        try:
            value = self._final_stat(parent, parts[-1])
        finally:
            os.close(parent)
        if value is None:
            return "absent"
        if stat.S_ISLNK(value.st_mode):
            return "symlink"
        if stat.S_ISDIR(value.st_mode):
            self._check_owner(value, "target")
            return "directory"
        if stat.S_ISREG(value.st_mode):
            self._check_owner(value, "target")
            return "file"
        return "other"

    def readlink(self, target: str) -> str | None:
        """The symlink's raw target text, ``None`` when absent (T-2917
        5c.2, the timer enablement link proofs). Read-only; the parent walk
        is the same descriptor-confined, owner-checked walk as every other
        read, and the link itself is never followed."""

        parts = self._relative(target)
        try:
            parent = self._open_parent(parts)
        except _ParentMissing:
            return None
        try:
            value = self._final_stat(parent, parts[-1])
            if value is None:
                return None
            if not stat.S_ISLNK(value.st_mode):
                raise SeatPathRefused("target is not a symlink")
            return os.readlink(parts[-1], dir_fd=parent)
        finally:
            os.close(parent)

    def read_bytes(self, target: str) -> bytes | None:
        """The file's bytes, ``None`` when absent. Refuses symlinks, other
        object types, foreign owners and oversize files."""

        parts = self._relative(target)
        try:
            parent = self._open_parent(parts)
        except _ParentMissing:
            return None
        try:
            try:
                descriptor = os.open(parts[-1], _READ_FLAGS, dir_fd=parent)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise SeatPathRefused("target is a symlink or unreadable") from exc
            try:
                value = os.fstat(descriptor)
                if not stat.S_ISREG(value.st_mode):
                    raise SeatPathRefused("target is not a regular file")
                self._check_owner(value, "target")
                if value.st_size > self.max_file_bytes:
                    raise SeatPathRefused("target exceeds the size limit")
                chunks = []
                total = 0
                while True:
                    chunk = os.read(descriptor, 65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.max_file_bytes:
                        raise SeatPathRefused("target exceeds the size limit")
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)

    def snapshot(self, target: str) -> dict[str, str]:
        """Installer-compatible before/after snapshot."""

        kind = self.kind(target)
        if kind == "absent":
            return {"state": "absent"}
        if kind == "directory":
            return {"state": "directory", "mode": f"{self._mode(target):04o}"}
        if kind == "file":
            content = self.read_bytes(target)
            if content is None:
                return {"state": "absent"}
            return {
                "state": "file",
                "content_b64": base64.b64encode(content).decode("ascii"),
                "fingerprint": _sha256(content),
                "mode": f"{self._mode(target):04o}",
            }
        raise SeatPathRefused(f"target is a {kind}, not a file or directory")

    def _mode(self, target: str) -> int:
        parts = self._relative(target)
        parent = self._open_parent(parts)
        try:
            value = self._final_stat(parent, parts[-1])
        finally:
            os.close(parent)
        if value is None:
            raise SeatPathRefused("target vanished")
        return value.st_mode & 0o7777

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def mkdir(self, target: str, mode: int) -> bool:
        """Create the directory if absent; True when created. The parent
        must already exist inside the seat root."""

        parts = self._relative(target)
        try:
            parent = self._open_parent(parts)
        except _ParentMissing as exc:
            raise SeatPathRefused("parent directory is missing") from exc
        try:
            existing = self._final_stat(parent, parts[-1])
            if existing is not None:
                if stat.S_ISLNK(existing.st_mode) or not stat.S_ISDIR(existing.st_mode):
                    raise SeatPathRefused("a non-directory exists at the target")
                self._check_owner(existing, "target")
                return False
            os.mkdir(parts[-1], 0o700, dir_fd=parent)
            # committed: every failure from here on is post-commit
            try:
                created = os.open(parts[-1], _DIRECTORY_FLAGS, dir_fd=parent)
                try:
                    self._check_owner(os.fstat(created), "created directory")
                    os.fchmod(created, mode)
                finally:
                    os.close(created)
                os.fsync(parent)
            except Exception as exc:  # committed: any failure is post-commit
                raise SeatChangeIncomplete(
                    f"directory created but finishing failed: {type(exc).__name__}"
                ) from exc
            return True
        finally:
            os.close(parent)

    def write_atomic(self, target: str, data: bytes, mode: int) -> bool:
        """Replace the file atomically; True when the content changed. An
        existing target must be a regular file owned by the executor uid."""

        if len(data) > self.max_file_bytes:
            raise SeatPathRefused("content exceeds the size limit")
        parts = self._relative(target)
        try:
            parent = self._open_parent(parts)
        except _ParentMissing as exc:
            raise SeatPathRefused("parent directory is missing") from exc
        try:
            existing = self._final_stat(parent, parts[-1])
            if existing is not None:
                if stat.S_ISLNK(existing.st_mode):
                    raise SeatPathRefused("target is a symlink")
                if not stat.S_ISREG(existing.st_mode):
                    raise SeatPathRefused("target is not a regular file")
                self._check_owner(existing, "target")
            self._replace_in(parent, parts[-1], data, mode)
            return True
        finally:
            os.close(parent)

    @staticmethod
    def _replace_in(parent: int, name: str, data: bytes, mode: int) -> None:
        temporary = f".{name}.{secrets.token_hex(8)}.tmp"
        descriptor = os.open(temporary, _WRITE_FLAGS, 0o600, dir_fd=parent)
        try:
            try:
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    view = view[written:]
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            raise
        # committed: the rename happened
        SeatFiles._sync_committed(parent, "file replaced")

    @staticmethod
    def _sync_committed(parent: int, what: str) -> None:
        try:
            os.fsync(parent)
        except Exception as exc:  # committed: any failure is post-commit
            raise SeatChangeIncomplete(
                f"{what} but directory sync failed: {type(exc).__name__}"
            ) from exc

    # ------------------------------------------------------------------
    # rollback restores (T-2917 5d.2)
    # ------------------------------------------------------------------

    def _existing_file_sha(self, parent: int, name: str) -> str:
        """sha256 of the regular, executor-owned file ``name`` under the
        parent descriptor (never followed, bounded)."""

        try:
            descriptor = os.open(name, _READ_FLAGS, dir_fd=parent)
        except OSError as exc:
            raise SeatPathRefused("restore target is a symlink or unreadable") from exc
        try:
            value = os.fstat(descriptor)
            if not stat.S_ISREG(value.st_mode):
                raise SeatPathRefused("restore target is not a regular file")
            self._check_owner(value, "restore target")
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(descriptor, 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > self.max_file_bytes:
                    raise SeatPathRefused("restore target exceeds the size limit")
                digest.update(chunk)
            return digest.hexdigest()
        finally:
            os.close(descriptor)

    def restore_file(
        self,
        target: str,
        data: bytes,
        mode: int,
        expected_fingerprint: str | None,
    ) -> bool:
        """Atomically put ``data`` (with ``mode``) back at ``target``, ONLY
        when the current object is exactly the one classified: a regular
        owned file whose sha256 is ``expected_fingerprint``, or nothing at
        all when it is None. True once replaced."""

        if len(data) > self.max_file_bytes:
            raise SeatPathRefused("content exceeds the size limit")
        parts = self._relative(target)
        try:
            parent = self._open_parent(parts)
        except _ParentMissing as exc:
            raise SeatPathRefused("parent directory is missing") from exc
        try:
            existing = self._final_stat(parent, parts[-1])
            if expected_fingerprint is None:
                if existing is not None:
                    raise SeatPathRefused("restore target appeared since it was classified")
            else:
                if existing is None:
                    raise SeatPathRefused("restore target vanished since it was classified")
                if self._existing_file_sha(parent, parts[-1]) != expected_fingerprint:
                    raise SeatPathRefused("restore target changed since it was classified")
            self._replace_in(parent, parts[-1], data, mode)
            return True
        finally:
            os.close(parent)

    def remove_file(self, target: str, expected_fingerprint: str) -> bool:
        """Unlink the regular owned file at ``target`` ONLY when its sha256
        is ``expected_fingerprint``; False when it is already absent."""

        parts = self._relative(target)
        try:
            parent = self._open_parent(parts)
        except _ParentMissing:
            return False
        try:
            if self._final_stat(parent, parts[-1]) is None:
                return False
            if self._existing_file_sha(parent, parts[-1]) != expected_fingerprint:
                raise SeatPathRefused("file changed since it was classified; not removed")
            os.unlink(parts[-1], dir_fd=parent)
            self._sync_committed(parent, "file removed")
            return True
        finally:
            os.close(parent)

    def rmdir_if_empty(self, target: str) -> bool:
        """Remove the owned directory at ``target`` only if it is EMPTY;
        False (nothing removed) when absent or not empty. Never recursive."""

        parts = self._relative(target)
        try:
            parent = self._open_parent(parts)
        except _ParentMissing:
            return False
        try:
            existing = self._final_stat(parent, parts[-1])
            if existing is None:
                return False
            if stat.S_ISLNK(existing.st_mode) or not stat.S_ISDIR(existing.st_mode):
                raise SeatPathRefused("rmdir target is not a directory")
            self._check_owner(existing, "rmdir target")
            try:
                os.rmdir(parts[-1], dir_fd=parent)
            except OSError as exc:
                if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    return False
                raise
            self._sync_committed(parent, "directory removed")
            return True
        finally:
            os.close(parent)

    def remove_symlink(self, target: str, expected_readlink: str) -> bool:
        """Unlink the owned symlink at ``target`` ONLY when its readlink
        text is exactly ``expected_readlink`` (the link is never followed);
        False when already absent."""

        parts = self._relative(target)
        try:
            parent = self._open_parent(parts)
        except _ParentMissing:
            return False
        try:
            existing = self._final_stat(parent, parts[-1])
            if existing is None:
                return False
            if not stat.S_ISLNK(existing.st_mode):
                raise SeatPathRefused("link target is not a symlink")
            self._check_owner(existing, "symlink")
            if os.readlink(parts[-1], dir_fd=parent) != expected_readlink:
                raise SeatPathRefused("symlink changed since it was classified; not removed")
            os.unlink(parts[-1], dir_fd=parent)
            self._sync_committed(parent, "symlink removed")
            return True
        finally:
            os.close(parent)

    # ------------------------------------------------------------------
    # T-5608: the residue quarantine's move (and its two reads)
    # ------------------------------------------------------------------

    def lexists(self, target: str) -> bool:
        """True when ANY object (a symlink included, never followed) is at
        ``target``; False when it or its parent is absent. A symlinked or
        foreign-owned path component is refused, never traversed."""

        parts = self._relative(target)
        try:
            parent = self._open_parent(parts)
        except _ParentMissing:
            return False
        try:
            return self._final_stat(parent, parts[-1]) is not None
        finally:
            os.close(parent)

    def list_dir(self, target: str) -> tuple[str, ...]:
        """Sorted entry names of the owned directory at ``target``; empty
        when it is absent. Read-only; the directory itself is opened with
        ``O_NOFOLLOW``, so a symlink there is refused."""

        parts = self._relative(target)
        try:
            parent = self._open_parent(parts)
        except _ParentMissing:
            return ()
        try:
            try:
                directory = self._open_child(parent, parts[-1], owner_checked=True)
            except _ParentMissing:
                return ()
        finally:
            os.close(parent)
        try:
            return tuple(sorted(os.listdir(directory)))
        finally:
            os.close(directory)

    def rename_within(self, source: str, destination: str) -> None:
        """Move the executor-owned entry at ``source`` to ``destination``
        (a new name; a symlink is moved as the link itself, never followed).
        Refused, nothing changed: either end outside the seat root, a
        missing or foreign-owned source, a missing destination parent, or
        an EXISTING destination (never replaced). Once the rename has
        committed, a failing directory sync raises ``SeatChangeIncomplete``.
        The destination check and the rename share the destination parent's
        descriptor; only the seat's own uid can write under its seat root."""

        source_parts = self._relative(source)
        destination_parts = self._relative(destination)
        try:
            source_parent = self._open_parent(source_parts)
        except _ParentMissing as exc:
            raise SeatPathRefused("move source parent is missing") from exc
        try:
            try:
                destination_parent = self._open_parent(destination_parts)
            except _ParentMissing as exc:
                raise SeatPathRefused("move destination parent is missing") from exc
            try:
                existing = self._final_stat(source_parent, source_parts[-1])
                if existing is None:
                    raise SeatPathRefused("move source is absent")
                self._check_owner(existing, "move source")
                if self._final_stat(destination_parent, destination_parts[-1]) is not None:
                    raise SeatPathRefused("move destination already exists; never replaced")
                os.rename(
                    source_parts[-1],
                    destination_parts[-1],
                    src_dir_fd=source_parent,
                    dst_dir_fd=destination_parent,
                )
                # committed: the entry moved
                self._sync_committed(destination_parent, "entry moved")
                self._sync_committed(source_parent, "entry moved")
            finally:
                os.close(destination_parent)
        finally:
            os.close(source_parent)
