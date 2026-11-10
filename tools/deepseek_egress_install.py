#!/usr/bin/env python3
"""Stage the inert, key-holding DeepSeek egress boundary transactionally.

The tool renders files only.  It never reloads a firewall or starts a service.
The separate T-6193 live gate owns those privileged actions.

Runtime prerequisite: the deployed Linux host must provide /usr/bin/openssl.
Certificate preflight refuses with a typed operator error when it is absent.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import pwd
import re
import secrets
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

from tools.deepseek_egress_firewall import BEGIN, END, render_firewall
from tools.deepseek_egress_proxy import ProxyPolicy, load_policy
RECEIPT_SCHEMA = "pantheon-deepseek-egress-install/v1"
_CAP = 8 * 1024 * 1024


class InstallError(RuntimeError):
    """The artifact transaction is unsafe or incomplete."""


@dataclasses.dataclass(frozen=True)
class InstallReceipt:
    policy_digest: str
    files: Mapping[str, Mapping[str, object]]


@dataclasses.dataclass(frozen=True)
class _Snapshot:
    data: bytes
    mode: int
    uid: int
    gid: int


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


_OPENSSL = "/usr/bin/openssl"


def _certificate_memfd(label: str, raw: bytes) -> int:
    try:
        fd = os.memfd_create(label, getattr(os, "MFD_CLOEXEC", 0))
    except (AttributeError, OSError):
        raise InstallError("certificate validator unavailable") from None
    try:
        offset = 0
        while offset < len(raw):
            try:
                written = os.write(fd, raw[offset:])
            except InterruptedError:
                continue
            if written <= 0:
                raise OSError("short certificate write")
            offset += written
        os.lseek(fd, 0, os.SEEK_SET)
        return fd
    except OSError:
        os.close(fd)
        raise InstallError("certificate validator unavailable") from None


def _openssl(args: list[str], pass_fds: tuple[int, ...]) -> bytes:
    try:
        result = subprocess.run(
            [_OPENSSL, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=10,
            pass_fds=pass_fds,
        )
    except (OSError, subprocess.SubprocessError):
        raise InstallError("certificate validator unavailable") from None
    if result.returncode != 0 or len(result.stdout) > _CAP:
        raise InstallError("certificate bundle invalid") from None
    return result.stdout


def _validate_certificate_bundle(certs: Mapping[str, bytes], hostname: str) -> None:
    try:
        ca_raw = certs["ca.pem"]
        leaf_raw = certs["proxy-cert.pem"]
        key_raw = certs["proxy-key.pem"]
    except (KeyError, TypeError):
        raise InstallError("certificate bundle invalid") from None
    fds: list[int] = []
    try:
        fds.append(_certificate_memfd("deepseek-ca", ca_raw))
        fds.append(_certificate_memfd("deepseek-leaf", leaf_raw))
        fds.append(_certificate_memfd("deepseek-key", key_raw))
        inherited = tuple(fds)
        ca_path, leaf_path, key_path = (f"/proc/self/fd/{fd}" for fd in fds)
        constraints = _openssl(["x509", "-in", ca_path, "-noout", "-ext", "basicConstraints"], inherited)
        if b"CA:TRUE" not in constraints.replace(b" ", b""):
            raise InstallError("certificate bundle invalid")
        sans = _openssl(["x509", "-in", leaf_path, "-noout", "-ext", "subjectAltName"], inherited)
        if re.findall(rb"DNS:([^,\s]+)", sans) != [hostname.encode("ascii")]:
            raise InstallError("certificate bundle invalid")
        leaf_public = _openssl(["x509", "-in", leaf_path, "-pubkey", "-noout"], inherited)
        key_public = _openssl(["pkey", "-in", key_path, "-pubout"], inherited)
        if leaf_public != key_public:
            raise InstallError("certificate bundle invalid")
        _openssl(
            [
                "verify",
                "-x509_strict",
                "-purpose",
                "sslserver",
                "-verify_hostname",
                hostname,
                "-CAfile",
                ca_path,
                leaf_path,
            ],
            inherited,
        )
    except (UnicodeEncodeError, TypeError):
        raise InstallError("certificate bundle invalid") from None
    finally:
        for fd in fds:
            os.close(fd)


def _service_unit() -> bytes:
    return (
        "[Unit]\nDescription=Pantheon DeepSeek key-holding egress proxy\nAfter=network-online.target pantheon-deepseek-egress-firewall-verify.service\n"
        "Requires=pantheon-deepseek-egress-firewall-verify.service\n"
        "StartLimitIntervalSec=3600\nStartLimitBurst=30\n"  # T-6352 bound
        "[Service]\nType=simple\nUser=pantheon-deepseek-proxy\nGroup=pantheon-deepseek-proxy\n"
        "LoadCredential=provider-key:/etc/pantheon/deepseek-egress/provider-key\n"
        "LoadCredential=proxy-cert:/etc/pantheon/deepseek-egress/proxy-cert.pem\n"
        "LoadCredential=proxy-key:/etc/pantheon/deepseek-egress/proxy-key.pem\n"
        "ExecStart=/usr/bin/python3 /opt/pantheon/tools/deepseek_egress_proxy.py serve "
        "--policy /etc/pantheon/deepseek-egress/policy.json "
        "--credential ${CREDENTIALS_DIRECTORY}/provider-key "
        "--cert ${CREDENTIALS_DIRECTORY}/proxy-cert "
        "--cert-key ${CREDENTIALS_DIRECTORY}/proxy-key\n"
        "Restart=on-failure\nRestartSec=5s\nRestartSteps=5\nRestartMaxDelaySec=120\nUMask=0077\n"  # T-6352 backoff
        "NoNewPrivileges=yes\nPrivateTmp=yes\nPrivateDevices=yes\nProtectSystem=strict\nProtectHome=yes\n"
        "ProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\n"
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX\nLockPersonality=yes\nMemoryDenyWriteExecute=yes\n"
        "[Install]\nWantedBy=multi-user.target\n"
    ).encode()


def _runtime_dropin(policy: ProxyPolicy) -> bytes:
    return (
        "[Service]\n"
        "ExecStartPre=/usr/bin/python3 /opt/pantheon/tools/deepseek_egress_proxy.py gate "
        "--policy /etc/pantheon/deepseek-egress/policy.json "
        "--ca /etc/pantheon/deepseek-egress/ca.pem "
        "--manifest /etc/pantheon/deepseek-egress/launch-gate.json\n"
        f"Environment=DEEPSEEK_BASE_URL=https://{policy.local_authority}/v1\n"
        "Environment=DEEPSEEK_API_KEY=proxy-held-dummy\n"
        "Environment=SSL_CERT_FILE=/etc/pantheon/deepseek-egress/ca.pem\n"
        "Environment=DEEPSEEK_EGRESS_POLICY=/etc/pantheon/deepseek-egress/policy.json\n"
    ).encode()


def _verify_unit() -> bytes:
    return (
        "[Unit]\nDescription=Verify active DeepSeek egress firewall boundary\nAfter=ufw.service\n"
        "[Service]\nType=oneshot\nUser=root\nGroup=root\nUMask=0027\n"
        "ExecStart=/usr/bin/python3 /opt/pantheon/tools/deepseek_egress_firewall_verify.py --verify "
        "--manifest /etc/pantheon/deepseek-egress/launch-gate.json "
        "--source-v4 /etc/ufw/before.rules --source-v6 /etc/ufw/before6.rules "
        "--output /run/pantheon-deepseek-egress/firewall.json\n"
        "RuntimeDirectory=pantheon-deepseek-egress\nRuntimeDirectoryMode=0755\nRuntimeDirectoryPreserve=yes\n"
        "NoNewPrivileges=yes\nPrivateTmp=yes\nProtectSystem=strict\nProtectHome=yes\n"
    ).encode()


def _verify_timer() -> bytes:
    return (
        "[Unit]\nDescription=Refresh DeepSeek egress firewall health\n"
        "[Timer]\nOnBootSec=15s\nOnUnitActiveSec=5s\nAccuracySec=1s\nUnit=pantheon-deepseek-egress-firewall-verify.service\n"
        "[Install]\nWantedBy=timers.target\n"
    ).encode()


def _strip_owned_block(raw: bytes, label: str) -> bytes:
    begin = (BEGIN + "\n").encode()
    end = (END + "\n").encode()
    if begin not in raw and end not in raw:
        return raw
    if raw.count(begin) != 1 or raw.count(end) != 1:
        raise InstallError(f"foreign {label} marker")
    start = raw.index(begin)
    finish = raw.index(end, start) + len(end)
    return raw[:start] + raw[finish:]


def _owned_block(raw: bytes, label: str) -> bytes:
    begin = (BEGIN + "\n").encode()
    end = (END + "\n").encode()
    if raw.count(begin) != 1 or raw.count(end) != 1:
        raise InstallError(f"{label} marker mismatch")
    start = raw.index(begin)
    finish = raw.index(end, start) + len(end)
    return raw[start:finish]


def _insert_ufw(raw: bytes, fragment: bytes, *, family: str, receipted: bool) -> bytes:
    if BEGIN.encode() in raw or END.encode() in raw:
        if not receipted:
            raise InstallError(f"foreign {family} firewall marker")
        raw = _strip_owned_block(raw, f"{family} firewall")
    prefix = b"-A ufw-before-output" if family == "v4" else b"-A ufw6-before-output"
    loopback = prefix + b" -o lo -j ACCEPT\n"
    established_prefix = prefix + b" -m conntrack --ctstate RELATED,ESTABLISHED"
    if raw.count(loopback) != 1 or raw.count(established_prefix) != 1:
        raise InstallError(f"{family} firewall insertion anchors mismatch")
    first_rule = raw.find(prefix + b" ")
    if first_rule < 0:
        raise InstallError(f"{family} firewall output chain missing")
    insertion = first_rule
    return raw[:insertion] + fragment + raw[insertion:]


class Installer:
    _RECEIPT = "var/lib/pantheon-deepseek-egress/install-receipt.json"
    _BLOCK_TARGETS = {"etc/hosts", "etc/ufw/before.rules", "etc/ufw/before6.rules"}
    _MODES = {
        "etc/pantheon/deepseek-egress/policy.json": 0o444,
        "etc/pantheon/deepseek-egress/ca.pem": 0o444,
        "etc/pantheon/deepseek-egress/proxy-cert.pem": 0o400,
        "etc/pantheon/deepseek-egress/proxy-key.pem": 0o400,
        "etc/systemd/system/pantheon-deepseek-egress-proxy.service": 0o444,
        "etc/systemd/system/pantheon-deepseek-egress-firewall-verify.service": 0o444,
        "etc/systemd/system/pantheon-deepseek-egress-firewall-verify.timer": 0o444,
        "home/deepseek-fallback/.config/systemd/user/pantheon-runtime-controller@.service.d/egress.conf": 0o444,
        "etc/pantheon/deepseek-egress/launch-gate.json": 0o444,
        "etc/pantheon/deepseek-egress/watcher-gate.json": 0o444,
        "etc/hosts": 0o644,
        "etc/ufw/before.rules": 0o640,
        "etc/ufw/before6.rules": 0o640,
        _RECEIPT: 0o400,
    }

    def __init__(
        self,
        root: Path,
        policy: ProxyPolicy,
        *,
        uid_lookup: Callable[[str], int],
        certificate_factory: Callable[[], Mapping[str, bytes]],
        allow_live_root: bool = False,
    ):
        self.root = Path(root)
        if self.root == Path("/") and not allow_live_root:
            raise InstallError("live root requires the separate explicit flag")
        if self.root == Path("/") and allow_live_root and os.geteuid() != 0:
            raise InstallError("live root mode requires euid 0")
        self.policy = policy
        self.uid_lookup = uid_lookup
        self.certificate_factory = certificate_factory
        self.owner_uid = os.geteuid()
        self.owner_gid = os.getegid()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            self._root_fd = os.open(self.root, flags)
        except OSError as exc:
            raise InstallError(f"target root unavailable:{type(exc).__name__}") from None

    def close(self) -> None:
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def __del__(self):
        fd = getattr(self, "_root_fd", -1)
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
            self._root_fd = -1

    def __enter__(self) -> "Installer":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @staticmethod
    def _parts(relative: str) -> tuple[str, ...]:
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise InstallError("managed path invalid")
        return path.parts

    def _parent_fd(self, relative: str, *, create: bool, created: list[str] | None = None) -> tuple[int, str]:
        parts = self._parts(relative)
        current = os.dup(self._root_fd)
        walked: list[str] = []
        try:
            for part in parts[:-1]:
                walked.append(part)
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, 0o755, dir_fd=current)
                    if created is not None:
                        created.append("/".join(walked))
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=current)
                except OSError as exc:
                    raise InstallError(f"unsafe managed parent: /{'/'.join(walked)}:{type(exc).__name__}") from None
                st = os.fstat(child)
                if not stat.S_ISDIR(st.st_mode) or not self._parent_owner_allowed("/".join(walked), st.st_uid):
                    os.close(child)
                    raise InstallError(f"managed parent owner/type mismatch: /{'/'.join(walked)}")
                os.close(current)
                current = child
            return current, parts[-1]
        except Exception:
            os.close(current)
            raise

    def _seat_uid(self) -> int:
        try:
            value = self.uid_lookup(self.policy.seat_account)
        except Exception:
            raise InstallError("seat uid unavailable") from None
        return value if isinstance(value, int) and not isinstance(value, bool) else -1

    def _parent_owner_allowed(self, relative: str, uid: int) -> bool:
        if uid == self.owner_uid:
            return True
        return uid == self._seat_uid() and (
            relative == "home/deepseek-fallback" or relative.startswith("home/deepseek-fallback/")
        )

    def _uids(self) -> tuple[int, int]:
        seat = self._seat_uid()
        try:
            proxy = self.uid_lookup(self.policy.proxy_account)
        except Exception:
            raise InstallError("proxy uid unavailable") from None
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in (seat, proxy)) or seat == proxy:
            raise InstallError("seat/proxy UIDs must be positive, non-root and distinct")
        return seat, proxy

    def _snapshot(self, relative: str) -> _Snapshot | None:
        try:
            parent, leaf = self._parent_fd(relative, create=False)
        except FileNotFoundError:
            return None
        try:
            try:
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0), dir_fd=parent)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise InstallError(f"owned target unavailable: /{relative}:{type(exc).__name__}") from None
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_uid != self.owner_uid:
                    raise InstallError(f"owned target owner/type mismatch: /{relative}")
                chunks: list[bytes] = []
                total = 0
                while total <= _CAP:
                    chunk = os.read(fd, min(65536, _CAP + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                if total > _CAP:
                    raise InstallError(f"owned target oversize: /{relative}")
                return _Snapshot(b"".join(chunks), stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid)
            finally:
                os.close(fd)
        finally:
            os.close(parent)

    def _atomic_write(self, relative: str, data: bytes, mode: int, *, uid: int | None = None, gid: int | None = None, created: list[str] | None = None) -> None:
        parent, leaf = self._parent_fd(relative, create=True, created=created)
        tmp = f".{leaf}.{secrets.token_hex(8)}.tmp"
        fd = -1
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), mode, dir_fd=parent)
            os.fchmod(fd, mode)
            os.fchown(fd, self.owner_uid if uid is None else uid, self.owner_gid if gid is None else gid)
            offset = 0
            while offset < len(data):
                written = os.write(fd, data[offset:])
                if written <= 0:
                    raise OSError("short write")
                offset += written
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(tmp, leaf, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp, dir_fd=parent)
            except FileNotFoundError:
                pass
            os.close(parent)

    def _unlink(self, relative: str) -> None:
        parent, leaf = self._parent_fd(relative, create=False)
        try:
            os.unlink(leaf, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)

    def _parse_receipt(self, *, expected_policy: bool) -> dict[str, object] | None:
        snap = self._snapshot(self._RECEIPT)
        if snap is None:
            return None
        if snap.mode != self._MODES[self._RECEIPT]:
            raise InstallError("install receipt mode mismatch")
        try:
            value = json.loads(snap.data.decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise InstallError("install receipt malformed") from None
        if not isinstance(value, dict) or set(value) != {"schema", "policy_digest", "files"} or value.get("schema") != RECEIPT_SCHEMA or not isinstance(value.get("files"), dict):
            raise InstallError("install receipt shape mismatch")
        if expected_policy and value["policy_digest"] != self.policy.digest:
            raise InstallError("install receipt policy mismatch")
        for absolute, metadata in value["files"].items():
            if not isinstance(absolute, str) or not absolute.startswith("/") or not isinstance(metadata, dict):
                raise InstallError("install receipt entry malformed")
            snap_file = self._snapshot(absolute[1:])
            if snap_file is None:
                raise InstallError(f"managed file drift: {absolute}")
            relative = absolute[1:]
            material = _owned_block(snap_file.data, relative) if metadata.get("action") == "block" else snap_file.data
            expected_owner = "root" if self.root == Path("/") else "target-root-owner"
            if (
                metadata.get("sha256") != hashlib.sha256(material).hexdigest()
                or metadata.get("mode") != format(snap_file.mode, "04o")
                or metadata.get("owner") != expected_owner
            ):
                raise InstallError(f"managed file drift: {absolute}")
        return value

    def _host_bytes(self, *, receipted: bool) -> bytes:
        snap = self._snapshot("etc/hosts")
        raw = b"" if snap is None else snap.data
        if BEGIN.encode() in raw or END.encode() in raw:
            if not receipted:
                raise InstallError("foreign hosts marker")
            raw = _strip_owned_block(raw, "hosts")
        if raw and not raw.endswith(b"\n"):
            raw += b"\n"
        return raw + f"{BEGIN}\n127.0.0.1 {self.policy.local_hostname}\n{END}\n".encode()

    def _desired(self) -> tuple[dict[str, bytes], bool]:
        seat_uid, proxy_uid = self._uids()
        receipt = self._parse_receipt(expected_policy=False)
        receipted = receipt is not None
        cert_paths = {
            "ca.pem": "etc/pantheon/deepseek-egress/ca.pem",
            "proxy-cert.pem": "etc/pantheon/deepseek-egress/proxy-cert.pem",
            "proxy-key.pem": "etc/pantheon/deepseek-egress/proxy-key.pem",
        }
        existing = {name: self._snapshot(rel) for name, rel in cert_paths.items()}
        if all(value is None for value in existing.values()):
            certs = dict(self.certificate_factory())
            if set(certs) != set(cert_paths) or any(not isinstance(v, bytes) or not v for v in certs.values()):
                raise InstallError("certificate factory shape mismatch")
        elif any(value is None for value in existing.values()):
            raise InstallError("partial certificate set")
        else:
            certs = {name: value.data for name, value in existing.items() if value is not None}
        _validate_certificate_bundle(certs, self.policy.local_hostname)
        firewall = render_firewall(self.policy, seat_uid=seat_uid, proxy_uid=proxy_uid)
        ufw4 = self._snapshot("etc/ufw/before.rules")
        ufw6 = self._snapshot("etc/ufw/before6.rules")
        if ufw4 is None or ufw6 is None:
            raise InstallError("target UFW files are required")
        v4_fragment = firewall["v4"].encode()
        v6_fragment = firewall["v6"].encode()
        gate = _canonical_json({
            "schema": "pantheon-deepseek-egress-gate/v1",
            "policy_digest": self.policy.digest,
            "required": "enforced",
            "v4_block_sha256": hashlib.sha256(v4_fragment).hexdigest(),
            "v6_block_sha256": hashlib.sha256(v6_fragment).hexdigest(),
            "firewall_health": "/run/pantheon-deepseek-egress/firewall.json",
            "client_auth": "proxy-held-dummy",
        })
        desired = {
            "etc/pantheon/deepseek-egress/policy.json": _canonical_json(self.policy.as_mapping()),
            **{rel: certs[name] for name, rel in cert_paths.items()},
            "etc/systemd/system/pantheon-deepseek-egress-proxy.service": _service_unit(),
            "etc/systemd/system/pantheon-deepseek-egress-firewall-verify.service": _verify_unit(),
            "etc/systemd/system/pantheon-deepseek-egress-firewall-verify.timer": _verify_timer(),
            "home/deepseek-fallback/.config/systemd/user/pantheon-runtime-controller@.service.d/egress.conf": _runtime_dropin(self.policy),
            "etc/pantheon/deepseek-egress/launch-gate.json": gate,
            "etc/pantheon/deepseek-egress/watcher-gate.json": gate,
            "etc/hosts": self._host_bytes(receipted=receipted),
            "etc/ufw/before.rules": _insert_ufw(ufw4.data, v4_fragment, family="v4", receipted=receipted),
            "etc/ufw/before6.rules": _insert_ufw(ufw6.data, v6_fragment, family="v6", receipted=receipted),
        }
        if not receipted:
            for relative in desired:
                if relative not in self._BLOCK_TARGETS and self._snapshot(relative) is not None:
                    raise InstallError(f"unreceipted managed target: /{relative}")
        return desired, receipted

    def install(self, *, fail_after_write: int | None = None) -> InstallReceipt:
        desired, _ = self._desired()
        tracked = [*desired, self._RECEIPT]
        snapshots = {relative: self._snapshot(relative) for relative in tracked}
        created_dirs: list[str] = []
        writes = 0
        try:
            for relative, data in desired.items():
                prior = snapshots[relative]
                mode = self._MODES[relative]
                if prior is not None and prior.data == data and prior.mode == mode:
                    continue
                writes += 1
                if fail_after_write == writes:
                    raise OSError("injected write fault")
                self._atomic_write(relative, data, mode, created=created_dirs)
            files: dict[str, dict[str, object]] = {}
            for relative in desired:
                current = self._snapshot(relative)
                assert current is not None
                material = _owned_block(current.data, relative) if relative in self._BLOCK_TARGETS else current.data
                files[f"/{relative}"] = {
                    "sha256": hashlib.sha256(material).hexdigest(),
                    "mode": format(current.mode, "04o"),
                    "owner": "root" if self.root == Path("/") else "target-root-owner",
                    "action": "block" if relative in self._BLOCK_TARGETS else "file",
                }
            receipt_bytes = _canonical_json({"schema": RECEIPT_SCHEMA, "policy_digest": self.policy.digest, "files": files})
            if snapshots[self._RECEIPT] is None or snapshots[self._RECEIPT].data != receipt_bytes:
                writes += 1
                if fail_after_write == writes:
                    raise OSError("injected write fault")
                self._atomic_write(self._RECEIPT, receipt_bytes, self._MODES[self._RECEIPT], created=created_dirs)
            return InstallReceipt(self.policy.digest, files)
        except Exception as original:
            failures: list[str] = []
            for relative in reversed(tracked):
                prior = snapshots[relative]
                try:
                    current = self._snapshot(relative)
                    if prior is None:
                        if current is not None:
                            self._unlink(relative)
                    else:
                        self._atomic_write(relative, prior.data, prior.mode, uid=prior.uid, gid=prior.gid)
                except Exception as exc:  # rollback failures are surfaced, never swallowed
                    failures.append(f"/{relative}:{type(exc).__name__}")
            for relative in reversed(created_dirs):
                try:
                    parent, leaf = self._parent_fd(relative, create=False)
                    try:
                        os.rmdir(leaf, dir_fd=parent)
                    finally:
                        os.close(parent)
                except OSError:
                    pass
            if failures:
                raise InstallError("rollback failed " + ",".join(failures)) from original
            if isinstance(original, InstallError):
                raise
            raise InstallError(f"install transaction rolled back:{type(original).__name__}") from None

    def remove(self, *, fail_after_write: int | None = None) -> bool:
        receipt = self._parse_receipt(expected_policy=True)
        if receipt is None:
            return False
        files = receipt["files"]
        assert isinstance(files, dict)
        tracked = [absolute[1:] for absolute in files] + [self._RECEIPT]
        snapshots = {relative: self._snapshot(relative) for relative in tracked}
        writes = 0
        try:
            for absolute, metadata in sorted(files.items(), reverse=True):
                relative = absolute[1:]
                snap = snapshots[relative]
                assert snap is not None
                writes += 1
                if fail_after_write == writes:
                    raise OSError("injected remove fault")
                if isinstance(metadata, dict) and metadata.get("action") == "block":
                    cleaned = _strip_owned_block(snap.data, relative)
                    self._atomic_write(relative, cleaned, snap.mode, uid=snap.uid, gid=snap.gid)
                else:
                    self._unlink(relative)
            writes += 1
            if fail_after_write == writes:
                raise OSError("injected remove fault")
            self._unlink(self._RECEIPT)
            return True
        except Exception as original:
            failures: list[str] = []
            for relative in reversed(tracked):
                snap = snapshots[relative]
                if snap is None:
                    continue
                try:
                    self._atomic_write(relative, snap.data, snap.mode, uid=snap.uid, gid=snap.gid)
                except Exception as exc:
                    failures.append(f"/{relative}:{type(exc).__name__}")
            if failures:
                raise InstallError("remove rollback failed " + ",".join(failures)) from original
            raise InstallError(f"remove transaction rolled back:{type(original).__name__}") from None


def _cert_source(directory: Path) -> Callable[[], Mapping[str, bytes]]:
    def read() -> Mapping[str, bytes]:
        out: dict[str, bytes] = {}
        for name in ("ca.pem", "proxy-cert.pem", "proxy-key.pem"):
            path = directory / name
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
            except OSError as exc:
                raise InstallError(f"certificate source unavailable:{type(exc).__name__}") from None
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) not in {0o400, 0o440, 0o444} or st.st_size > _CAP:
                    raise InstallError("certificate source owner/type/mode mismatch")
                chunks = []
                total = 0
                while total <= _CAP:
                    chunk = os.read(fd, min(65536, _CAP + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                if total > _CAP:
                    raise InstallError("certificate source oversize")
                out[name] = b"".join(chunks)
            finally:
                os.close(fd)
        return out
    return read


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("install", "remove"), nargs="?")
    parser.add_argument("--target-root", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--cert-source", type=Path)
    parser.add_argument("--allow-live-root", action="store_true")
    args = parser.parse_args(argv)
    if args.mode is None or args.target_root is None or args.policy is None or (args.mode == "install" and args.cert_source is None):
        return 64
    try:
        policy = load_policy(args.policy, expected_owner=os.geteuid())
        installer = Installer(
            args.target_root,
            policy,
            uid_lookup=lambda name: pwd.getpwnam(name).pw_uid,
            certificate_factory=_cert_source(args.cert_source or Path(".")),
            allow_live_root=args.allow_live_root,
        )
        installer.install() if args.mode == "install" else installer.remove()
    except (InstallError, OSError, KeyError):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
