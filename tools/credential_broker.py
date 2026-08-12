"""Provider-neutral credential leases for Pantheon (ORG-051, T-2380).

The first provider is deliberately local and small: it validates an existing
host file, opens it without following symlinks, and hands the consumer a
short-lived file descriptor. Secret bytes never enter manifests, audit records,
argv, diagnostics, or object representations.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Protocol


LeaseDelivery = Literal["fd"]
RevocationStrength = Literal["cooperative-host-fd", "strong-provider"]

IDENTIFIER_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.:@-]{0,62}[A-Za-z0-9])?"
    r"(?:/[A-Za-z0-9](?:[A-Za-z0-9_.:@-]{0,62}[A-Za-z0-9])?)*$"
)
DIAGNOSTIC_RE = re.compile(r"^[A-Za-z0-9 .:_;-]{1,200}$")
MAX_IDENTIFIER_LENGTH = 128


class CredentialError(RuntimeError):
    """Base error for broker policy and provider failures."""


class CredentialPolicyError(CredentialError):
    """The caller or requested use is outside the credential's scope."""


class CredentialValidationError(CredentialError):
    """The provider material fails ownership, mode, or path validation."""


def _validate_identifier(label: str, value: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string identifier")
    if len(value) > MAX_IDENTIFIER_LENGTH or not IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{label} must be a bounded non-secret identifier")


def _validate_mode(label: str, mode: int) -> None:
    if mode < 0 or mode > 0o777:
        raise ValueError(f"{label} must be a Unix permission mode")


@dataclass(frozen=True)
class ProviderCapabilities:
    provider: str
    supported_kinds: tuple[str, ...]
    lease_delivery: tuple[LeaseDelivery, ...]
    revocation_strength: RevocationStrength

    def __post_init__(self) -> None:
        _validate_identifier("provider", self.provider)
        for kind in self.supported_kinds:
            _validate_identifier("kind", kind)
        if not self.supported_kinds:
            raise ValueError("provider must support at least one credential kind")
        if self.lease_delivery != ("fd",):
            raise ValueError("host broker currently supports fd delivery only")
        if self.revocation_strength not in (
            "cooperative-host-fd",
            "strong-provider",
        ):
            raise ValueError("unknown revocation strength")
        if (
            self.provider == "host-file"
            and self.revocation_strength != "cooperative-host-fd"
        ):
            raise ValueError("host-file provider revocation must be cooperative")


HOST_FILE_CAPABILITIES = ProviderCapabilities(
    provider="host-file",
    supported_kinds=("bearer-token", "password", "deploy-key", "signing-key"),
    lease_delivery=("fd",),
    revocation_strength="cooperative-host-fd",
)


@dataclass(frozen=True)
class HostFileConstraints:
    parent_dir: Path
    parent_owner_uid: int
    parent_owner_gid: int
    parent_mode: int = 0o700

    def __post_init__(self) -> None:
        if not isinstance(self.parent_dir, Path):
            object.__setattr__(self, "parent_dir", Path(self.parent_dir))
        _validate_mode("parent_mode", self.parent_mode)


@dataclass(frozen=True)
class CredentialRef:
    credential_id: str
    kind: str
    provider: str
    location: Path
    owner_uid: int
    owner_gid: int
    mode: int = 0o600
    roles: tuple[str, ...] = ()
    instances: tuple[str, ...] = ()
    services: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    host_file: HostFileConstraints | None = None
    expires_at: float | None = None

    def __post_init__(self) -> None:
        _validate_identifier("credential_id", self.credential_id)
        _validate_identifier("kind", self.kind)
        _validate_identifier("provider", self.provider)
        if not isinstance(self.location, Path):
            object.__setattr__(self, "location", Path(self.location))
        for label, values in (
            ("role", self.roles),
            ("instance", self.instances),
            ("service", self.services),
            ("action", self.actions),
        ):
            for value in values:
                _validate_identifier(label, value)
        _validate_mode("mode", self.mode)
        if self.provider == HOST_FILE_CAPABILITIES.provider:
            if self.host_file is None:
                raise ValueError("host-file credentials require host_file constraints")
            if self.location.parent != self.host_file.parent_dir:
                raise ValueError("host-file parent constraint must match location")


@dataclass(frozen=True)
class LeaseRequest:
    credential_id: str
    caller_uid: int
    role: str
    instance: str
    service: str
    action: str
    ttl_seconds: int

    def __post_init__(self) -> None:
        _validate_identifier("credential_id", self.credential_id)
        _validate_identifier("role", self.role)
        _validate_identifier("instance", self.instance)
        _validate_identifier("service", self.service)
        _validate_identifier("action", self.action)


@dataclass(frozen=True)
class CredentialStatus:
    credential_id: str
    available: bool
    owner_uid: int | None
    owner_gid: int | None
    mode: int | None
    expires_at: float | None
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class SecretSource:
    """An already-open descriptor supplying replacement material."""

    fd: int = field(repr=False)


@dataclass(frozen=True)
class CredentialLease:
    lease_id: str
    credential_id: str
    fd: int = field(repr=False)
    caller_uid: int
    role: str
    instance: str
    service: str
    action: str
    issued_at: float
    expires_at: float
    provider: str
    revocation_strength: RevocationStrength


class ConsumerVerifier(Protocol):
    def __call__(self, ref: CredentialRef, fd: int) -> bool: ...


class CredentialBroker(Protocol):
    def issue(self, request: LeaseRequest) -> CredentialLease: ...

    def revoke(self, lease_id: str) -> None: ...

    def rotate(
        self,
        credential_id: str,
        source: SecretSource,
        verifier: ConsumerVerifier,
    ) -> CredentialRef: ...

    def verify(self, ref: CredentialRef) -> CredentialStatus: ...


class HostFileCredentialBroker:
    """Issue descriptor leases for strictly validated local credential files."""

    def __init__(
        self,
        refs: tuple[CredentialRef, ...],
        *,
        audit_path: Path,
        clock: Callable[[], float] = time.time,
        caller_uid: Callable[[], int] = os.geteuid,
        max_ttl_seconds: int = 3600,
        capabilities: tuple[ProviderCapabilities, ...] = (HOST_FILE_CAPABILITIES,),
    ) -> None:
        if max_ttl_seconds < 1:
            raise ValueError("max_ttl_seconds must be positive")
        self._validate_host_file_capabilities(capabilities)
        self._capabilities = {cap.provider: cap for cap in capabilities}
        if len(self._capabilities) != len(capabilities):
            raise ValueError("provider capabilities must be unique")
        self._refs: dict[str, CredentialRef] = {}
        for ref in refs:
            self._validate_ref_compatibility(ref)
            if ref.credential_id in self._refs:
                raise ValueError("credential ids must be unique")
            self._refs[ref.credential_id] = ref
        self._audit_path = audit_path
        self._clock = clock
        self._caller_uid = caller_uid
        self._max_ttl_seconds = max_ttl_seconds
        self._leases: dict[str, CredentialLease] = {}
        self._lock = threading.RLock()

    def verify(self, ref: CredentialRef) -> CredentialStatus:
        with self._lock:
            errors: list[str] = []
            owner_uid: int | None = None
            owner_gid: int | None = None
            mode: int | None = None
            try:
                self._validate_ref_compatibility(ref)
                self._validate_not_expired(ref)
                fd = self._open_validated_host_file(ref)
            except CredentialValidationError as exc:
                errors.extend(str(exc).split("; "))
            except OSError as exc:
                errors.append(f"credential path is unavailable: {exc.__class__.__name__}")
            else:
                try:
                    opened = os.fstat(fd)
                    owner_uid = opened.st_uid
                    owner_gid = opened.st_gid
                    mode = stat.S_IMODE(opened.st_mode)
                finally:
                    os.close(fd)
            status = CredentialStatus(
                credential_id=ref.credential_id,
                available=not errors,
                owner_uid=owner_uid,
                owner_gid=owner_gid,
                mode=mode,
                expires_at=ref.expires_at,
                errors=tuple(errors),
            )
            self._audit(
                "verify",
                credential_id=ref.credential_id,
                outcome="pass" if status.available else "fail",
                errors=list(status.errors),
            )
            return status

    def issue(self, request: LeaseRequest) -> CredentialLease:
        with self._lock:
            ref = self._refs.get(request.credential_id)
            if ref is None:
                raise CredentialPolicyError("unknown credential reference")
            self._authorize(ref, request)
            self._validate_not_expired(ref)
            fd = self._open_validated_host_file(ref)
            try:
                now = self._clock()
                capabilities = self._capabilities[ref.provider]
                lease = CredentialLease(
                    lease_id=secrets.token_hex(16),
                    credential_id=ref.credential_id,
                    fd=fd,
                    caller_uid=request.caller_uid,
                    role=request.role,
                    instance=request.instance,
                    service=request.service,
                    action=request.action,
                    issued_at=now,
                    expires_at=now + request.ttl_seconds,
                    provider=ref.provider,
                    revocation_strength=capabilities.revocation_strength,
                )
                self._leases[lease.lease_id] = lease
                self._audit(
                    "issue",
                    credential_id=ref.credential_id,
                    lease_id=lease.lease_id,
                    caller_uid=request.caller_uid,
                    role=request.role,
                    instance=request.instance,
                    service=request.service,
                    action=request.action,
                    expires_at=lease.expires_at,
                    provider=lease.provider,
                    revocation_strength=lease.revocation_strength,
                    outcome="issued",
                )
                return lease
            except Exception:
                os.close(fd)
                raise

    def revoke(self, lease_id: str) -> None:
        with self._lock:
            lease = self._leases.pop(lease_id, None)
            if lease is None:
                raise CredentialPolicyError("unknown or already revoked lease")
            try:
                os.close(lease.fd)
            finally:
                self._audit(
                    "revoke",
                    credential_id=lease.credential_id,
                    lease_id=lease.lease_id,
                    outcome="revoked",
                    revocation_strength=lease.revocation_strength,
                )

    def reap_expired(self) -> tuple[str, ...]:
        with self._lock:
            now = self._clock()
            expired = tuple(
                lease_id
                for lease_id, lease in self._leases.items()
                if lease.expires_at <= now
            )
            for lease_id in expired:
                self.revoke(lease_id)
            return expired

    def rotate(
        self,
        credential_id: str,
        source: SecretSource,
        verifier: ConsumerVerifier,
    ) -> CredentialRef:
        with self._lock:
            ref = self._refs.get(credential_id)
            if ref is None:
                raise CredentialPolicyError("unknown credential reference")
            if ref.owner_uid != os.geteuid():
                raise CredentialPolicyError(
                    "rotation requires the broker to own the credential"
                )
            self._validate_parent(ref)
            tmp_fd, tmp_name = tempfile.mkstemp(
                prefix=f".{ref.location.name}.",
                dir=ref.location.parent,
            )
            tmp_path = Path(tmp_name)
            backup_path = ref.location.with_name(
                f".{ref.location.name}.{secrets.token_hex(8)}.old"
            )
            backup_created = False
            try:
                os.fchmod(tmp_fd, ref.mode)
                while True:
                    chunk = os.read(source.fd, 64 * 1024)
                    if not chunk:
                        break
                    self._write_all(tmp_fd, chunk)
                os.fsync(tmp_fd)
                os.close(tmp_fd)
                tmp_fd = -1

                replacement_fd = self._open_validated_path(ref, tmp_path)
                try:
                    if not verifier(ref, replacement_fd):
                        raise CredentialValidationError(
                            "consumer verifier rejected replacement material"
                        )
                finally:
                    os.close(replacement_fd)

                if ref.location.exists():
                    os.replace(ref.location, backup_path)
                    backup_created = True
                os.replace(tmp_path, ref.location)
                try:
                    final_fd = self._open_validated_host_file(ref)
                    os.close(final_fd)
                    self._fsync_dir(ref.location.parent)
                except Exception:
                    if backup_created:
                        os.replace(backup_path, ref.location)
                        backup_created = False
                    raise
                if backup_created:
                    backup_path.unlink()
                    backup_created = False
                self._audit(
                    "rotate",
                    credential_id=credential_id,
                    outcome="rotated",
                )
                return ref
            except Exception as exc:
                self._audit(
                    "rotate",
                    credential_id=credential_id,
                    outcome="failed",
                    error=exc.__class__.__name__,
                )
                raise
            finally:
                if tmp_fd >= 0:
                    os.close(tmp_fd)
                try:
                    tmp_path.unlink()
                except FileNotFoundError:
                    pass
                if backup_created:
                    os.replace(backup_path, ref.location)

    def _authorize(self, ref: CredentialRef, request: LeaseRequest) -> None:
        if request.caller_uid != self._caller_uid():
            raise CredentialPolicyError(
                "claimed caller uid does not match authenticated local uid"
            )
        if request.ttl_seconds < 1 or request.ttl_seconds > self._max_ttl_seconds:
            raise CredentialPolicyError("requested lease ttl is outside policy")
        scopes = (
            ("role", ref.roles, request.role),
            ("instance", ref.instances, request.instance),
            ("service", ref.services, request.service),
            ("action", ref.actions, request.action),
        )
        for label, allowed, actual in scopes:
            if allowed and actual not in allowed:
                raise CredentialPolicyError(f"{label} is outside credential scope")

    @staticmethod
    def _validate_host_file_capabilities(
        capabilities: tuple[ProviderCapabilities, ...],
    ) -> None:
        for capability in capabilities:
            if (
                capability.provider != HOST_FILE_CAPABILITIES.provider
                or capability.revocation_strength != "cooperative-host-fd"
            ):
                raise ValueError(
                    "HostFileCredentialBroker capabilities must be "
                    "host-file cooperative-host-fd"
                )

    def _validate_ref_compatibility(self, ref: CredentialRef) -> None:
        capabilities = self._capabilities.get(ref.provider)
        if capabilities is None:
            raise CredentialValidationError("credential provider is not supported")
        if ref.kind not in capabilities.supported_kinds:
            raise CredentialValidationError(
                "credential kind is not supported by provider"
            )
        if ref.provider == HOST_FILE_CAPABILITIES.provider and ref.host_file is None:
            raise CredentialValidationError("host-file constraints are required")

    def _validate_not_expired(self, ref: CredentialRef) -> None:
        if ref.expires_at is not None and ref.expires_at <= self._clock():
            raise CredentialValidationError("credential reference has expired")

    def _open_validated_host_file(self, ref: CredentialRef) -> int:
        if ref.host_file is None:
            raise CredentialValidationError("host-file constraints are required")
        self._validate_parent(ref)
        parent_fd = self._open_parent(ref.host_file)
        try:
            fd = self._open_child(parent_fd, ref.location.name)
            try:
                self._validate_material_fd(ref, fd)
                return fd
            except Exception:
                os.close(fd)
                raise
        finally:
            os.close(parent_fd)

    def _open_validated_path(self, ref: CredentialRef, path: Path) -> int:
        fd = os.open(path, _open_flags())
        try:
            self._validate_material_fd(ref, fd)
            return fd
        except Exception:
            os.close(fd)
            raise

    def _validate_parent(self, ref: CredentialRef) -> None:
        constraints = ref.host_file
        if constraints is None:
            raise CredentialValidationError("host-file constraints are required")
        if ref.location.parent != constraints.parent_dir:
            raise CredentialValidationError("credential parent is outside constraint")

    def _open_parent(self, constraints: HostFileConstraints) -> int:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            fd = os.open(constraints.parent_dir, flags)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise CredentialValidationError(
                    "credential parent must not be a symlink"
                ) from exc
            raise
        try:
            parent = os.fstat(fd)
            errors: list[str] = []
            if not stat.S_ISDIR(parent.st_mode):
                errors.append("credential parent must be a directory")
            if parent.st_uid != constraints.parent_owner_uid:
                errors.append("credential parent owner does not match reference")
            if parent.st_gid != constraints.parent_owner_gid:
                errors.append("credential parent group does not match reference")
            if stat.S_IMODE(parent.st_mode) != constraints.parent_mode:
                errors.append("credential parent mode does not match reference")
            if errors:
                raise CredentialValidationError("; ".join(errors))
            return fd
        except Exception:
            os.close(fd)
            raise

    def _open_child(self, parent_fd: int, name: str) -> int:
        try:
            return os.open(name, _open_flags(), dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise CredentialValidationError(
                    "credential path must not be a symlink"
                ) from exc
            raise

    def _validate_material_fd(self, ref: CredentialRef, fd: int) -> None:
        opened = os.fstat(fd)
        errors: list[str] = []
        if not stat.S_ISREG(opened.st_mode):
            errors.append("credential path must be a regular file")
        if opened.st_uid != ref.owner_uid:
            errors.append("opened credential owner does not match reference")
        if opened.st_gid != ref.owner_gid:
            errors.append("opened credential group does not match reference")
        opened_mode = stat.S_IMODE(opened.st_mode)
        if opened_mode != ref.mode:
            errors.append("opened credential mode does not match reference")
        if opened_mode & 0o077:
            errors.append("credential is accessible by group or other")
        if errors:
            raise CredentialValidationError("; ".join(errors))

    def _audit(self, event: str, **fields: object) -> None:
        _validate_identifier("event", event)
        _validate_audit_fields(fields)
        record = {"ts": self._clock(), "event": event, **fields}
        self._audit_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        fd = os.open(self._audit_path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            payload = (
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            self._write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while persisting credential state")
            view = view[written:]

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _validate_audit_fields(fields: dict[str, object]) -> None:
    for key, value in fields.items():
        _validate_identifier("audit field", key)
        _validate_audit_value(key, value)


def _validate_audit_value(label: str, value: object) -> None:
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, str):
        if label == "errors":
            _validate_diagnostic(label, value)
        else:
            _validate_identifier(label, value)
        return
    if isinstance(value, list):
        for item in value:
            _validate_audit_value(label, item)
        return
    raise ValueError(f"{label} is not safe for credential audit")


def _validate_diagnostic(label: str, value: str) -> None:
    if not DIAGNOSTIC_RE.fullmatch(value):
        raise ValueError(f"{label} contains unsafe diagnostic text")
