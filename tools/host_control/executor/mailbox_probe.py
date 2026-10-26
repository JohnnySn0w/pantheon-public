"""``mailbox.probe``: the executor's mailbox probes (T-2917 5d.3; ORG-052
section 8 mailbox family; rulings T-2917:D19, D20).

Two installer operation kinds, executed with installer parity
(``PersonaInstaller._mailbox_probe`` and
``mailbox_writable_roots.verify_maildir_writable_root``) but CONFINED:

* ``mailbox-probe``: a durable round trip. A payload is created with
  ``O_CREAT|O_EXCL|O_NOFOLLOW``, written, fsynced, read back and
  sha-compared in the seat's own ``hub/<persona>/tmp`` (receive) and the
  peer's ``hub/<peer>/tmp`` (send), then unlinked in a ``finally``;
* ``mailbox-writable-root-check``: a transient create-and-unlink in
  ``hub/<persona>`` and its ``tmp``, ``new`` and ``cur``.

Confinement, fail closed and checked BEFORE any write:

* the hub comes from the ROOT-OWNED executor config (``mailbox_hub``,
  ruling T-2917:D19), never from the environment or the plan alone; a plan
  value that names another hub is refused;
* round-trip targets must be exactly ``hub/<persona>/tmp``; a writable-root
  target exactly ``hub/<persona>``; persona components are simple names;
* every directory is opened descriptor-anchored from ``/`` with
  ``O_NOFOLLOW|O_DIRECTORY`` on EVERY component, and every file operation is
  relative to that descriptor, so no symlink is ever followed and a path
  swap cannot redirect a write.

Probe names are deterministic (ruling T-2917:D20):
``.installer-probe-<32 hex>`` / ``.pantheon-rw-probe-<32 hex>``, the hex
being ``sha256(operation_handle|operation_id|direction)``. A name that
already exists is unlinked ONCE and the create retried, but only when it is
this executor's own residue (owned by this uid, a regular file, within the
size bound); anything else is refused and never unlinked. Cleanup is
check-then-unlink BY NAME: the name is stat'ed and unlinked only when its
(dev, inode) is the file this probe created. That is not atomic: a swap
between the stat and the unlink needs write access to the directory, and
write access to the directory already grants deleting any entry in it, so
the race gives the swapper no privilege it did not already hold. In a STICKY
directory write access alone does not grant deleting another user's entry,
but the kernel then equally blocks renaming over or away this executor's
file, so no swap is possible there and the conclusion stands.

Every write checks the execution deadline first (ruling T-2917:D11).
``residue`` is the reconcile probe's read-only observation of leftover
deterministic probe files.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Callable, Final, Mapping

from tools.host_control.contracts import HostControlError
from tools.persona_installer_contracts import InstallOperation

ROUND_TRIP_KIND: Final = "mailbox-probe"
WRITABLE_ROOT_KIND: Final = "mailbox-writable-root-check"
MAILBOX_KINDS: Final = frozenset((ROUND_TRIP_KIND, WRITABLE_ROOT_KIND))
ROUND_TRIP_PREFIX: Final = ".installer-probe-"
WRITABLE_ROOT_PREFIX: Final = ".pantheon-rw-probe-"
MAILDIR_SUBDIRS: Final = ("tmp", "new", "cur")
HUB_SOURCE: Final = "executor-config"
# a file at a probe name larger than this is never this executor's residue
RESIDUE_MAX_BYTES: Final = 4096
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
_CREATE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
_READ_FLAGS: Final = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_PERSONA: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


class MailboxProbeRefused(HostControlError):
    """A mailbox probe target or object fails a confinement rule, or the
    probe could not complete (bounded detail)."""


def probe_name(prefix: str, operation_handle: str, operation_id: str, direction: str) -> str:
    material = f"{operation_handle}|{operation_id}|{direction}".encode("utf-8")
    return prefix + hashlib.sha256(material).hexdigest()[:32]


def _errno_name(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno or 0, "OSError")


class MailboxProbe:
    def __init__(self, *, hub: Path, uid: int, clock: Callable[[], float]) -> None:
        raw = os.fspath(hub)
        # "//"-prefixed paths survive normpath unchanged (POSIX); refused (D32)
        if (
            not os.path.isabs(raw)
            or raw.startswith("//")
            or os.path.normpath(raw) != raw
            or raw == "/"
            or ".." in Path(raw).parts
        ):
            raise MailboxProbeRefused("mailbox hub must be an absolute, normalized, non-root path")
        self.hub = Path(raw)
        self.uid = int(uid)
        self._clock = clock

    # ------------------------------------------------------------------
    # confinement (pure: no I/O)
    # ------------------------------------------------------------------

    def _relative(self, raw: object, what: str) -> tuple[str, ...]:
        if not isinstance(raw, str) or not os.path.isabs(raw):
            raise MailboxProbeRefused(f"{what} must be an absolute path")
        if raw.startswith("//") or os.path.normpath(raw) != raw or ".." in Path(raw).parts:
            raise MailboxProbeRefused(f"{what} must be normalized")
        try:
            parts = Path(raw).relative_to(self.hub).parts
        except ValueError as exc:
            raise MailboxProbeRefused(f"{what} is outside the configured mailbox hub") from exc
        if not parts or _PERSONA.fullmatch(parts[0]) is None:
            raise MailboxProbeRefused(f"{what} does not name a persona mailbox")
        return parts

    def _require_hub_value(self, operation: InstallOperation) -> None:
        declared = operation.desired.get("mailbox_hub")
        if declared is not None and declared != str(self.hub):
            raise MailboxProbeRefused("plan mailbox hub does not match the configured hub")

    def round_trip_dirs(self, operation: InstallOperation) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """(receive, send) directories below the hub; refuses anything but
        ``<persona>/tmp`` (the receive side inside the operation's target)."""

        self._require_hub_value(operation)
        target = self._relative(operation.target, "mailbox probe target")
        receive = self._relative(operation.desired.get("receive_tmp"), "receive directory")
        send = self._relative(operation.desired.get("send_tmp"), "send directory")
        if len(target) != 1:
            raise MailboxProbeRefused("mailbox probe target must be a persona mailbox")
        for parts, what in ((receive, "receive directory"), (send, "send directory")):
            if len(parts) != 2 or parts[1] != "tmp":
                raise MailboxProbeRefused(f"{what} must be exactly <hub>/<persona>/tmp")
        if receive[0] != target[0]:
            raise MailboxProbeRefused("receive directory is not the target mailbox's tmp")
        return receive, send

    def writable_root_dirs(self, operation: InstallOperation) -> tuple[tuple[str, ...], ...]:
        self._require_hub_value(operation)
        target = self._relative(operation.target, "mailbox writable root")
        if len(target) != 1:
            raise MailboxProbeRefused("mailbox writable root must be exactly <hub>/<persona>")
        subdirs = operation.desired.get("maildir_subdirs", ",".join(MAILDIR_SUBDIRS))
        if subdirs != ",".join(MAILDIR_SUBDIRS):
            raise MailboxProbeRefused("mailbox writable root declares unsupported maildir subdirs")
        return (target, *((target[0], name) for name in MAILDIR_SUBDIRS))

    def check_targets(self, operation: InstallOperation) -> None:
        """The pre-pass gate: confinement only, nothing is touched."""

        if operation.kind == ROUND_TRIP_KIND:
            self.round_trip_dirs(operation)
        elif operation.kind == WRITABLE_ROOT_KIND:
            self.writable_root_dirs(operation)
        else:
            raise MailboxProbeRefused(f"{operation.kind} is not a mailbox probe kind")

    # ------------------------------------------------------------------
    # descriptor-anchored primitives
    # ------------------------------------------------------------------

    def _open_dir(self, parts: tuple[str, ...]) -> int:
        descriptor = os.open("/", _DIR_FLAGS)
        try:
            for component in (*self.hub.parts[1:], *parts):
                child = os.open(component, _DIR_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except OSError as exc:
            os.close(descriptor)
            raise MailboxProbeRefused(
                f"mailbox directory {'/'.join(parts)} unavailable: {_errno_name(exc)}"
            ) from None
        except BaseException:
            os.close(descriptor)
            raise

    def _require_time(self, deadline: float) -> None:
        if self._clock() >= deadline:
            raise MailboxProbeRefused("execution deadline reached before a mailbox write")

    def _sweep_own(self, directory: int, name: str) -> None:
        try:
            value = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_uid != self.uid
            or value.st_size > RESIDUE_MAX_BYTES
        ):
            raise MailboxProbeRefused("a foreign object holds the probe name; refused, never unlinked")
        os.unlink(name, dir_fd=directory)

    def _create(self, directory: int, name: str, deadline: float) -> tuple[int, tuple[int, int]]:
        """Create the probe file (sweeping own residue ONCE); returns the
        open write descriptor and the created inode identity."""

        for attempt in (0, 1):
            self._require_time(deadline)
            try:
                handle = os.open(name, _CREATE_FLAGS, 0o600, dir_fd=directory)
            except FileExistsError:
                if attempt:
                    raise MailboxProbeRefused("probe name still taken after one own-residue sweep") from None
                self._sweep_own(directory, name)
                continue
            value = os.fstat(handle)
            return handle, (value.st_dev, value.st_ino)
        raise MailboxProbeRefused("probe file could not be created")  # pragma: no cover

    @staticmethod
    def _unlink_if_ours(directory: int, name: str, identity: tuple[int, int]) -> None:
        try:
            value = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (value.st_dev, value.st_ino) == identity:
            os.unlink(name, dir_fd=directory)

    # ------------------------------------------------------------------
    # the two probes
    # ------------------------------------------------------------------

    def round_trip(
        self,
        *,
        operation_handle: str,
        operation: InstallOperation,
        deadline: float,
    ) -> Mapping[str, str]:
        receive, send = self.round_trip_dirs(operation)
        payload = (
            "pantheon-executor-mailbox-probe:"
            f"{operation.provenance.get('instance', '')}:{operation.provenance.get('persona', '')}:"
            f"{operation_handle}:{operation.operation_id}\n"
        ).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        created: list[tuple[int, str, tuple[int, int]]] = []
        opened: list[int] = []
        try:
            for parts, direction in ((receive, "receive"), (send, "send")):
                directory = self._open_dir(parts)
                opened.append(directory)
                name = probe_name(ROUND_TRIP_PREFIX, operation_handle, operation.operation_id, direction)
                handle, identity = self._create(directory, name, deadline)
                created.append((directory, name, identity))
                try:
                    view = memoryview(payload)
                    while view:
                        written = os.write(handle, view)
                        view = view[written:]
                    os.fsync(handle)
                finally:
                    os.close(handle)
                reader = os.open(name, _READ_FLAGS, dir_fd=directory)
                try:
                    value = os.fstat(reader)
                    if (
                        not stat.S_ISREG(value.st_mode)
                        or value.st_uid != self.uid
                        or (value.st_dev, value.st_ino) != identity
                    ):
                        raise MailboxProbeRefused(f"mailbox {direction} probe file was replaced")
                    data = os.read(reader, RESIDUE_MAX_BYTES + 1)
                finally:
                    os.close(reader)
                if hashlib.sha256(data).hexdigest() != digest:
                    raise MailboxProbeRefused(f"mailbox {direction} round-trip hash mismatch")
        except OSError as exc:
            raise MailboxProbeRefused(f"mailbox round trip failed: {_errno_name(exc)}") from None
        finally:
            for directory, name, identity in created:
                try:
                    self._unlink_if_ours(directory, name, identity)
                except OSError:
                    pass
            for directory in opened:
                os.close(directory)
        return {
            "receive": "true",
            "send": "true",
            "durable_round_trip": "true",
            "sha256": digest,
            "mailbox_hub_source": HUB_SOURCE,
        }

    def writable_root(
        self,
        *,
        operation_handle: str,
        operation: InstallOperation,
        deadline: float,
    ) -> Mapping[str, str]:
        directories = self.writable_root_dirs(operation)
        for parts in directories:
            label = "/".join(parts[1:]) or "root"
            directory = self._open_dir(parts)
            try:
                name = probe_name(WRITABLE_ROOT_PREFIX, operation_handle, operation.operation_id, label)
                handle, identity = self._create(directory, name, deadline)
                os.close(handle)
                self._unlink_if_ours(directory, name, identity)
            except OSError as exc:
                raise MailboxProbeRefused(
                    f"mailbox directory {label} is not writable: {_errno_name(exc)}"
                ) from None
            finally:
                os.close(directory)
        return {
            "root": operation.target,
            "maildir_subdirs": ",".join(MAILDIR_SUBDIRS),
            "transient_write_probe": "true",
            "harness": operation.desired.get("harness", ""),
            "ref_kind": operation.desired.get("ref_kind", ""),
            "ref_persona": operation.desired.get("ref_persona", ""),
            "mailbox_hub_source": HUB_SOURCE,
        }

    # ------------------------------------------------------------------
    # reconcile: read-only residue observation
    # ------------------------------------------------------------------

    def _probe_files(
        self, operation_handle: str, operation: InstallOperation
    ) -> tuple[tuple[tuple[str, ...], str], ...]:
        if operation.kind == ROUND_TRIP_KIND:
            receive, send = self.round_trip_dirs(operation)
            return tuple(
                (parts, probe_name(ROUND_TRIP_PREFIX, operation_handle, operation.operation_id, direction))
                for parts, direction in ((receive, "receive"), (send, "send"))
            )
        return tuple(
            (parts, probe_name(WRITABLE_ROOT_PREFIX, operation_handle, operation.operation_id, "/".join(parts[1:]) or "root"))
            for parts in self.writable_root_dirs(operation)
        )

    def residue(self, *, operation_handle: str, operation: InstallOperation) -> Mapping[str, str]:
        """Read-only: which deterministic probe files of this operation are
        left behind (``absent``, ``own`` or ``foreign``); never unlinks."""

        found = "absent"
        for parts, name in self._probe_files(operation_handle, operation):
            directory = self._open_dir(parts)
            try:
                try:
                    value = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                own = (
                    stat.S_ISREG(value.st_mode)
                    and value.st_uid == self.uid
                    and value.st_size <= RESIDUE_MAX_BYTES
                )
                found = "foreign" if not own else ("own" if found == "absent" else found)
                if found == "foreign":
                    break
            finally:
                os.close(directory)
        return {
            "observation": "mailbox-residue",
            "residue": found,
            "mailbox_hub_source": HUB_SOURCE,
        }
