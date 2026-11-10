#!/usr/bin/env python3
"""Verify source and active DeepSeek firewall blocks, then emit typed health."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import time
from pathlib import Path
from typing import Mapping

from tools.deepseek_egress_firewall import BEGIN as BEGIN_TEXT, END as END_TEXT
from tools.deepseek_egress_proxy import FIREWALL_SCHEMA, GATE_SCHEMA, FirewallState


BEGIN = (BEGIN_TEXT + "\n").encode()
END = (END_TEXT + "\n").encode()


class VerifyError(RuntimeError):
    pass


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _block(source: bytes, prefix: bytes) -> bytes:
    if source.count(BEGIN) != 1 or source.count(END) != 1:
        raise VerifyError("source marker mismatch")
    start = source.index(BEGIN)
    finish = source.index(END, start) + len(END)
    block = source[start:finish]
    loopback = prefix + b" -o lo -j ACCEPT\n"
    established = prefix + b" -m conntrack --ctstate RELATED,ESTABLISHED"
    if source.count(loopback) != 1 or source.count(established) != 1:
        raise VerifyError("source anchor mismatch")
    first_rule = source.find(prefix + b" ")
    owned_first_rule = start + block.find(prefix + b" ")
    if first_rule != owned_first_rule:
        # No earlier rule in this chain may bypass the owned block.
        raise VerifyError("source block is not first output-chain rule set")
    if start >= min(source.index(loopback), source.index(established)):
        raise VerifyError("source block misplaced")
    return block


def _active_ok(active: bytes, block: bytes, prefix: bytes) -> None:
    rules = [line for line in block.splitlines(keepends=True) if line.startswith(b"-A ")]
    if not rules:
        raise VerifyError("empty firewall block")
    active_chain = [line for line in active.splitlines(keepends=True) if line.startswith(prefix + b" ")]
    if active_chain[: len(rules)] != rules:
        raise VerifyError("active block is not exact and contiguous")
    positions = []
    for rule in rules:
        if active.count(rule) != 1:
            raise VerifyError("active rule mismatch")
        positions.append(active.index(rule))
    loopback = prefix + b" -o lo -j ACCEPT\n"
    established = prefix + b" -m conntrack --ctstate RELATED,ESTABLISHED"
    if active.count(loopback) != 1 or active.count(established) != 1:
        raise VerifyError("active anchor mismatch")
    first_rule = active.find(prefix + b" ")
    if positions != sorted(positions) or positions[0] != first_rule or positions[0] >= min(active.index(loopback), active.index(established)):
        raise VerifyError("active block misplaced")


def verify_firewall(
    manifest: Mapping[str, str],
    source_v4: bytes,
    source_v6: bytes,
    active_v4: bytes,
    active_v6: bytes,
    *,
    observed_at: int | None = None,
) -> FirewallState:
    expected = {"schema", "policy_digest", "required", "v4_block_sha256", "v6_block_sha256", "firewall_health", "client_auth"}
    if not isinstance(manifest, Mapping) or set(manifest) != expected or manifest.get("schema") != GATE_SCHEMA or manifest.get("required") != "enforced":
        raise VerifyError("manifest shape mismatch")
    if any(not isinstance(manifest.get(key), str) for key in expected):
        raise VerifyError("manifest value type mismatch")
    block4 = _block(source_v4, b"-A ufw-before-output")
    block6 = _block(source_v6, b"-A ufw6-before-output")
    if sha256(block4) != manifest.get("v4_block_sha256") or sha256(block6) != manifest.get("v6_block_sha256"):
        raise VerifyError("source block digest mismatch")
    _active_ok(active_v4, block4, b"-A ufw-before-output")
    _active_ok(active_v6, block6, b"-A ufw6-before-output")
    return FirewallState.enforced(
        manifest["policy_digest"],
        manifest["v4_block_sha256"],
        manifest["v6_block_sha256"],
        observed_at=int(time.time()) if observed_at is None else observed_at,
    )


def _read_verified(path: Path, *, cap: int = 8 * 1024 * 1024) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise VerifyError(f"input unavailable:{type(exc).__name__}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_size > cap:
            raise VerifyError("input owner/type/size mismatch")
        chunks = []
        total = 0
        while total <= cap:
            chunk = os.read(fd, min(65536, cap + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > cap:
            raise VerifyError("input oversize")
        return b"".join(chunks)
    finally:
        os.close(fd)


def write_state(path: Path, state: FirewallState) -> None:
    absolute = path.absolute()
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in absolute.parent.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            st = os.fstat(child)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid not in {0, os.geteuid()}:
                os.close(child)
                raise VerifyError("output parent owner/type mismatch")
            os.close(current)
            current = child
    except Exception:
        os.close(current)
        raise
    tmp = f".{path.name}.{os.getpid()}.tmp"
    raw = json.dumps(state.as_mapping(), sort_keys=True, separators=(",", ":")).encode() + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(tmp, flags, 0o444, dir_fd=current)
    try:
        os.fchmod(fd, 0o444)
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path.name, src_dir_fd=current, dst_dir_fd=current)
        os.fsync(current)
    finally:
        try:
            os.unlink(tmp, dir_fd=current)
        except FileNotFoundError:
            pass
        os.close(current)


def verify_and_write(
    output: Path,
    manifest: Mapping[str, str],
    source_v4: bytes,
    source_v6: bytes,
    active_v4: bytes,
    active_v6: bytes,
    *,
    observed_at: int | None = None,
) -> FirewallState:
    timestamp = int(time.time()) if observed_at is None else observed_at
    try:
        state = verify_firewall(manifest, source_v4, source_v6, active_v4, active_v6, observed_at=timestamp)
    except VerifyError:
        digest = manifest.get("policy_digest") if isinstance(manifest, Mapping) else "0" * 64
        write_state(output, FirewallState.unknown(digest, observed_at=timestamp))
        raise
    write_state(output, state)
    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--source-v4", type=Path)
    parser.add_argument("--source-v6", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.verify or None in (args.manifest, args.source_v4, args.source_v6, args.output):
        return 64
    manifest: Mapping[str, str] = {}
    try:
        parsed = json.loads(_read_verified(args.manifest).decode("ascii"))
        if not isinstance(parsed, Mapping):
            raise VerifyError("manifest is not an object")
        manifest = parsed  # type: ignore[assignment]
        active4 = subprocess.run(["/usr/sbin/iptables-save"], check=True, capture_output=True, timeout=10).stdout
        active6 = subprocess.run(["/usr/sbin/ip6tables-save"], check=True, capture_output=True, timeout=10).stdout
        verify_and_write(args.output, manifest, _read_verified(args.source_v4), _read_verified(args.source_v6), active4, active6)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError, VerifyError):
        try:
            write_state(args.output, FirewallState.unknown(manifest.get("policy_digest", "0" * 64)))
        except (OSError, VerifyError, ValueError):
            pass
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
