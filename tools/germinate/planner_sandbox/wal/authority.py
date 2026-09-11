from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import stat
from dataclasses import dataclass


class AuthorityError(Exception):
    """Raised when held-dirfd filesystem authority is invalid or refused."""


_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,96}$")


@dataclass(frozen=True)
class FileEvidence:
    name: str
    size: int
    mode: int
    uid: int
    nlink: int
    sha256: str


def validate_safe_name(name: str) -> str:
    if not isinstance(name, str):
        raise AuthorityError("name-not-str")
    if name in {"", ".", ".."}:
        raise AuthorityError("unsafe-name")
    if "/" in name or "\x00" in name:
        raise AuthorityError("unsafe-name")
    if not _SAFE_NAME_RE.fullmatch(name):
        raise AuthorityError("unsafe-name")
    return name


def _validate_file_stat(st: os.stat_result, *, expected_uid: int, expected_mode: int) -> None:
    if not stat.S_ISREG(st.st_mode):
        raise AuthorityError("not-regular")
    if st.st_uid != expected_uid:
        raise AuthorityError("wrong-uid")
    if stat.S_IMODE(st.st_mode) != expected_mode:
        raise AuthorityError("wrong-mode")
    if st.st_nlink != 1:
        raise AuthorityError("wrong-nlink")


class HeldDir:
    def __init__(self, fd: int, *, expected_uid: int, expected_mode: int) -> None:
        self._fd = fd
        self._expected_uid = expected_uid
        self._expected_mode = expected_mode
        self._closed = False

    @classmethod
    def from_fd(
        cls,
        fd: int,
        *,
        expected_uid: int,
        expected_mode: int = 0o700,
    ) -> "HeldDir":
        try:
            dup_fd = os.dup(fd)
            os.set_inheritable(dup_fd, False)
            st = os.fstat(dup_fd)
        except OSError as exc:
            raise AuthorityError(f"dirfd-invalid:{exc.errno}") from exc
        if not stat.S_ISDIR(st.st_mode):
            os.close(dup_fd)
            raise AuthorityError("dirfd-not-directory")
        if st.st_uid != expected_uid:
            os.close(dup_fd)
            raise AuthorityError("dirfd-wrong-uid")
        if stat.S_IMODE(st.st_mode) != expected_mode:
            os.close(dup_fd)
            raise AuthorityError("dirfd-wrong-mode")
        return cls(dup_fd, expected_uid=expected_uid, expected_mode=expected_mode)

    def fileno(self) -> int:
        self._require_open()
        return self._fd

    def close(self) -> None:
        if not self._closed:
            os.close(self._fd)
            self._closed = True

    def __enter__(self) -> "HeldDir":
        self._require_open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise AuthorityError("dirfd-closed")

    def fsync_dir(self) -> None:
        self._require_open()
        os.fsync(self._fd)

    def read_regular(
        self,
        name: str,
        *,
        expected_uid: int,
        expected_mode: int = 0o600,
        max_bytes: int,
    ) -> bytes:
        self._require_open()
        validate_safe_name(name)
        try:
            fd = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=self._fd,
            )
        except OSError as exc:
            raise AuthorityError(f"open-regular:{exc.errno}") from exc
        try:
            st = os.fstat(fd)
            _validate_file_stat(st, expected_uid=expected_uid, expected_mode=expected_mode)
            if st.st_size > max_bytes:
                raise AuthorityError("file-too-large")
            data = b""
            while len(data) < st.st_size:
                chunk = os.read(fd, st.st_size - len(data))
                if not chunk:
                    break
                data += chunk
            if len(data) != st.st_size:
                raise AuthorityError("short-read")
            return data
        finally:
            os.close(fd)

    def stat_regular(
        self,
        name: str,
        *,
        expected_uid: int,
        expected_mode: int = 0o600,
    ) -> FileEvidence:
        data = self.read_regular(
            name,
            expected_uid=expected_uid,
            expected_mode=expected_mode,
            max_bytes=1 << 30,
        )
        st = os.stat(name, dir_fd=self._fd, follow_symlinks=False)
        _validate_file_stat(st, expected_uid=expected_uid, expected_mode=expected_mode)
        return FileEvidence(
            name=name,
            size=st.st_size,
            mode=stat.S_IMODE(st.st_mode),
            uid=st.st_uid,
            nlink=st.st_nlink,
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def write_immutable(
        self,
        name: str,
        data: bytes,
        *,
        expected_uid: int,
        mode: int = 0o600,
    ) -> FileEvidence:
        self._require_open()
        validate_safe_name(name)
        temp_name = self._temp_name(name)
        fd = self._create_temp(temp_name, mode=mode)
        try:
            self._write_all_and_validate(fd, data, expected_uid=expected_uid, mode=mode)
            try:
                os.link(temp_name, name, src_dir_fd=self._fd, dst_dir_fd=self._fd, follow_symlinks=False)
            except FileExistsError:
                raise
            except OSError as exc:
                raise AuthorityError(f"publish-immutable:{exc.errno}") from exc
            os.unlink(temp_name, dir_fd=self._fd)
            self.fsync_dir()
        except BaseException:
            self._cleanup_temp(temp_name)
            raise
        return self.stat_regular(name, expected_uid=expected_uid, expected_mode=mode)

    def replace_regular(
        self,
        name: str,
        data: bytes,
        *,
        expected_uid: int,
        mode: int = 0o600,
    ) -> FileEvidence:
        self._require_open()
        validate_safe_name(name)
        temp_name = self._temp_name(name)
        fd = self._create_temp(temp_name, mode=mode)
        try:
            self._write_all_and_validate(fd, data, expected_uid=expected_uid, mode=mode)
            os.replace(temp_name, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
            self.fsync_dir()
        except BaseException:
            self._cleanup_temp(temp_name)
            raise
        return self.stat_regular(name, expected_uid=expected_uid, expected_mode=mode)

    def open_record_lock(
        self,
        name: str = "manifest.lock",
        *,
        expected_uid: int,
        mode: int = 0o600,
    ) -> "RecordLock":
        self._require_open()
        validate_safe_name(name)
        try:
            fd = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                mode,
                dir_fd=self._fd,
            )
        except FileExistsError:
            try:
                fd = os.open(
                    name,
                    os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=self._fd,
                )
            except OSError as exc:
                raise AuthorityError(f"open-lock:{exc.errno}") from exc
        except OSError as exc:
            raise AuthorityError(f"open-lock:{exc.errno}") from exc
        try:
            st = os.fstat(fd)
            _validate_file_stat(st, expected_uid=expected_uid, expected_mode=mode)
        except BaseException:
            os.close(fd)
            raise
        return RecordLock(fd)

    def _temp_name(self, final_name: str) -> str:
        base = f".tmp-{final_name}-{os.getpid()}-{id(self):x}"
        if len(base) <= 96:
            return base
        digest = hashlib.sha256(base.encode("ascii")).hexdigest()[:16]
        return f".tmp-{digest}-{os.getpid()}"

    def _create_temp(self, temp_name: str, *, mode: int) -> int:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(temp_name, flags, mode, dir_fd=self._fd)
        except FileExistsError as exc:
            raise AuthorityError("temp-exists") from exc
        except OSError as exc:
            raise AuthorityError(f"create-temp:{exc.errno}") from exc
        return fd

    def _write_all_and_validate(self, fd: int, data: bytes, *, expected_uid: int, mode: int) -> None:
        try:
            os.fchmod(fd, mode)
            st = os.fstat(fd)
            _validate_file_stat(st, expected_uid=expected_uid, expected_mode=mode)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

    def _cleanup_temp(self, temp_name: str) -> None:
        try:
            os.unlink(temp_name, dir_fd=self._fd)
        except FileNotFoundError:
            return
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise


class RecordLock:
    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._closed = False

    def acquire(self) -> None:
        self._require_open()
        fcntl.flock(self._fd, fcntl.LOCK_EX)

    def release(self) -> None:
        self._require_open()
        fcntl.flock(self._fd, fcntl.LOCK_UN)

    def close(self) -> None:
        if not self._closed:
            os.close(self._fd)
            self._closed = True

    def __enter__(self) -> "RecordLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.release()
        finally:
            self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise AuthorityError("lock-closed")
