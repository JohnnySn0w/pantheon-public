#!/usr/bin/env python3
"""T-5038: keep a root-owned copy of a seat's claude.exe under /opt.

Watchman's unstick verb (codex_recovery._validate_foreground_snapshot) only types
into a pane whose foreground exe is root-owned, not owned by the target account,
and not group- or other-writable. A seat's npm-installed claude.exe is owned by
the seat and rewritten by self-update, so this tool (run as ROOT by a timer) keeps
a per-version root copy that the seat's runtime launches instead:

    <dest>/<version>/claude.exe   root:root 0755, never replaced in place
    <dest>/current -> <version>   repointed atomically

One directory per version, because replacing a running exe makes its
/proc/<pid>/exe read "... (deleted)", which fails unstick's exact allowlist match.
npm's self-updater stays on: it updates the seat's (no longer running) npm copy,
and the next sync picks the new version up.

Security: everything under the seat's npm tree is controlled by the seat, so
root never opens it. Every read goes through `runuser -u <account>` with the
seat's own privileges, so a planted symlink (say, to /etc/shadow) can only leak
what the seat could already read. Only <dest> is written, and every component
of it must be root-owned and not group- or other-writable.

Operator rule (T-5114, orchestrator 2026-10-17): ANY hand edit of the broker manifest
(a new verb, unit list or target) trips this tool's tamper check ("changed since
the last root write") until root reviews the edit and runs
`--rebaseline --manifest /etc/codex-recovery/manifest.json`. Every broker
install or manifest recipe ends with that rebaseline.
An account may be synced before it is a broker target (B14 S5 adds the target
after the seat relaunches onto the copy): the copy is kept current and only the
allowlist step is skipped, with exit 0.
Usage (root):  claude_exe_sync.py --account claude-fallback --dest /opt/pantheon/claude
               claude_exe_sync.py --dest /opt/pantheon/claude --list   # versioned exe paths
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable

PACKAGE_REL = Path(".npm-global/lib/node_modules/@anthropic-ai/claude-code")
EXE_NAME = "claude.exe"
CURRENT = "current"
# Used with fullmatch only: ASCII digits ([0-9], never \\d, which matches Unicode
# digits), no trailing newline, no empty or dot-only segments.
VERSION_RE = re.compile(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}(?:[-+][0-9A-Za-z]{1,20}(?:[.-][0-9A-Za-z]{1,20}){0,4})?")
MAX_EXE_BYTES = 1 << 30  # 1 GiB; today's exe is ~267 MB
DEFAULT_KEEP = 2

# Unstick allowlist regeneration (orchestrator's T-5038 constraints 1-5, 2026-10-15).
MANIFEST_PATH = Path("/etc/codex-recovery/manifest.json")
BROKER_PATH = Path("/usr/local/sbin/codex-recovery")
STATE_DIR = Path("/var/lib/pantheon-claude-exe-sync")
SHADOW_NAME = "manifest.last.json"   # the bytes this tool last wrote (or an operator baselined)
AUDIT_NAME = "audit.jsonl"           # sibling root log; the broker's audit log has one writer
MANIFEST_MODE = 0o644
MAX_MANIFEST_BYTES = 1 << 20

# validator(path) raises unless `path` loads through the broker's own manifest loader.
Validator = Callable[[Path], object]

# reader(account, path) -> bytes, read WITH THE ACCOUNT'S PRIVILEGES.
Reader = Callable[[str, Path], bytes]


class SyncError(RuntimeError):
    pass


def runuser_reader(account: str, path: Path, *, limit: int = MAX_EXE_BYTES) -> bytes:
    """Read `path` WITH THE ACCOUNT'S PRIVILEGES and never buffer more than
    `limit` bytes in root. First stat it as the account: it must be a regular
    file within the limit (so /dev/zero or a FIFO is refused). Then stream it
    through a bounded read, because the file can change between stat and cat."""
    probe = subprocess.run(
        ["runuser", "-u", account, "--", "stat", "-L", "-c", "%F|%s", "--", str(path)],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False,
    )
    kind, _, size = probe.stdout.decode(errors="replace").strip().partition("|")
    if probe.returncode != 0 or kind != "regular file" or not size.isdigit():
        raise SyncError(f"{path}: not a regular file readable by {account} ({kind or probe.returncode})")
    if int(size) > limit:
        raise SyncError(f"{path} is larger than {limit} bytes")
    with subprocess.Popen(
        ["runuser", "-u", account, "--", "cat", "--", str(path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    ) as proc:
        chunks, total = [], 0
        try:
            while chunk := proc.stdout.read(1 << 20):
                total += len(chunk)
                if total > limit:
                    raise SyncError(f"{path} grew past {limit} bytes while reading")
                chunks.append(chunk)
        except BaseException:
            proc.kill()
            raise
        if proc.wait(timeout=300) != 0:
            raise SyncError(f"read as {account} failed: {path}")
    return b"".join(chunks)


def read_version(account: str, package_dir: Path, reader: Reader) -> str:
    try:
        version = json.loads(reader(account, package_dir / "package.json")).get("version")
    except (ValueError, AttributeError) as exc:
        raise SyncError(f"unreadable package.json: {exc}") from exc
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise SyncError(f"refusing package version {version!r}")
    return version


def check_dest_safe(dest: Path, *, owner_uid: int, trusted_uids: frozenset[int] = frozenset({0})) -> None:
    """dest and every ancestor must be a real directory (no symlink) owned by
    a trusted uid (root) or owner_uid, and dest itself by owner_uid. None may be group- or
    other-writable, except a sticky ancestor (e.g. /tmp): there, others cannot
    rename or remove an entry they do not own."""
    if not dest.is_absolute():
        raise SyncError(f"{dest} must be absolute")
    for part in [*reversed(dest.parents), dest]:
        st = os.lstat(part)
        if not stat.S_ISDIR(st.st_mode):
            raise SyncError(f"{part} is not a real directory")
        if (st.st_uid not in trusted_uids and st.st_uid != owner_uid) or (part == dest and st.st_uid != owner_uid):
            raise SyncError(f"{part} is owned by uid {st.st_uid}")
        if st.st_mode & 0o022 and (part == dest or not st.st_mode & stat.S_ISVTX):
            raise SyncError(f"{part} is group- or other-writable")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def install_version(dest: Path, version: str, payload: bytes, *, owner_uid: int, owner_gid: int) -> Path:
    """Write <dest>/<version>/claude.exe atomically; never replace an existing one."""
    vdir = dest / version
    target = vdir / EXE_NAME
    if target.exists():
        return target
    vdir.mkdir(mode=0o755, exist_ok=True)
    os.chown(vdir, owner_uid, owner_gid)
    os.chmod(vdir, 0o755)
    fd, tmp = tempfile.mkstemp(prefix=".claude.exe.", dir=vdir)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chown(tmp, owner_uid, owner_gid)
        os.chmod(tmp, 0o755)
        os.rename(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _fsync_dir(vdir)
    return target


def point_current(dest: Path, version: str) -> bool:
    """Atomically repoint <dest>/current -> <version>. True if it changed."""
    link = dest / CURRENT
    if link.is_symlink() and os.readlink(link) == version:
        return False
    tmp = dest / f".{CURRENT}.{os.getpid()}"
    tmp.unlink(missing_ok=True)
    os.symlink(version, tmp)
    os.replace(tmp, link)
    _fsync_dir(dest)
    return True


def live_exe_paths(proc: Path = Path("/proc")) -> set[str]:
    paths: set[str] = set()
    for entry in proc.iterdir() if proc.is_dir() else ():
        if not entry.name.isdigit():
            continue
        try:
            paths.add(os.readlink(entry / "exe").removesuffix(" (deleted)"))
        except OSError:
            continue
    return paths


def version_dirs(dest: Path) -> list[Path]:
    return sorted(
        (p for p in dest.iterdir()
         if not p.name.startswith(".") and p.name != CURRENT
         and not p.is_symlink() and p.is_dir() and VERSION_RE.fullmatch(p.name)),
        key=lambda p: p.stat().st_mtime,
    )


def prune(dest: Path, *, keep: int, live: Iterable[str]) -> list[str]:
    """Remove version dirs that are not current, not run by a live process, and
    not among the newest `keep`. Returns the removed version names."""
    live = set(live)
    current = os.readlink(dest / CURRENT) if (dest / CURRENT).is_symlink() else None
    dirs = version_dirs(dest)
    newest = {p.name for p in dirs[-keep:]} if keep > 0 else set()
    removed = []
    for vdir in dirs:
        if vdir.name == current or vdir.name in newest or str(vdir / EXE_NAME) in live:
            continue
        shutil.rmtree(vdir)
        removed.append(vdir.name)
    return removed


def list_exe_paths(dest: Path) -> list[str]:
    return [str(p / EXE_NAME) for p in version_dirs(dest) if (p / EXE_NAME).is_file()]


def sync(
    account: str,
    dest: Path,
    *,
    account_home: Path,
    reader: Reader = runuser_reader,
    owner_uid: int = 0,
    owner_gid: int = 0,
    keep: int = DEFAULT_KEEP,
    live: Iterable[str] | None = None,
    trusted_uids: frozenset[int] = frozenset({0}),
    manifest: Path | None = None,
    target_key: str | None = None,
    state_dir: Path = STATE_DIR,
    validate: Validator | None = None,
) -> dict:
    """Copy, repoint, prune, then (if `manifest`) regenerate the unstick
    allowlist. The manifest baseline is checked FIRST, so a drifted manifest
    fails closed before anything on disk changes."""
    check_dest_safe(dest, owner_uid=owner_uid, trusted_uids=trusted_uids)
    is_target = True
    if manifest is not None:
        if validate is None or not target_key:
            raise SyncError("manifest regeneration needs a validator and a target key")
        raw = check_manifest_baseline(manifest, state_dir, manifest_owner_uid=owner_uid)
        # T-5114: an account can be synced before it is a broker target (B14 S5
        # adds the target after the seat relaunches onto the copy). Keep the copy
        # current and skip only the allowlist step; the baseline check above
        # still fails closed on a drifted manifest.
        targets = json.loads(raw).get("targets")
        is_target = isinstance(targets, dict) and target_key in targets
    package_dir = account_home / PACKAGE_REL
    version = read_version(account, package_dir, reader)
    existed = (dest / version / EXE_NAME).exists()
    if not existed:
        payload = reader(account, package_dir / "bin" / EXE_NAME)
        if not payload.startswith(b"\x7fELF"):
            raise SyncError("refusing a non-ELF claude.exe")
        install_version(dest, version, payload, owner_uid=owner_uid, owner_gid=owner_gid)
    changed = point_current(dest, version)
    # Constraint 5: prune keeps current, the newest `keep` and every version a
    # live process runs, and the allowlist is regenerated from what survives.
    removed = prune(dest, keep=keep, live=live_exe_paths() if live is None else live)
    result = {"version": version, "installed": not existed, "current_changed": changed, "pruned": removed}
    if manifest is not None and not is_target:
        result["allowlist_skipped"] = "not a broker target yet"
        print(f"claude_exe_sync: {target_key!r} is not a broker target yet; "
              "copy kept, allowlist step skipped", file=sys.stderr)
    elif manifest is not None:
        event = regenerate_allowlist(
            manifest, target_key, eligible_exe_paths(dest, owner_uid=owner_uid, trusted_uids=trusted_uids),
            state_dir=state_dir, validate=validate, manifest_owner_uid=owner_uid,
            manifest_owner_gid=owner_gid, account=account,
        )
        result["allowlist_changed"] = event is not None
    return result


# ---------------------------------------------------------------------------
# Unstick allowlist regeneration (constraints 1-5).
# ---------------------------------------------------------------------------

def canonical(obj: object) -> bytes:
    return (json.dumps(obj, indent=2) + "\n").encode()


def eligible_exe_paths(dest: Path, *, owner_uid: int, trusted_uids: frozenset[int] = frozenset({0})) -> list[str]:
    """Constraint 2: exactly <dest>/<version>/claude.exe for versions on disk whose
    whole chain passes the broker's owner/mode rule (owner_uid, no group/other
    write, real dirs, a single-link regular file). Never `current`."""
    check_dest_safe(dest, owner_uid=owner_uid, trusted_uids=trusted_uids)
    paths = []
    for vdir in version_dirs(dest):
        exe = vdir / EXE_NAME
        try:
            dst, est = os.lstat(vdir), os.lstat(exe)
        except FileNotFoundError:
            continue
        if (
            stat.S_ISDIR(dst.st_mode) and dst.st_uid == owner_uid and not dst.st_mode & 0o022
            and stat.S_ISREG(est.st_mode) and est.st_uid == owner_uid and not est.st_mode & 0o022
            and est.st_nlink == 1
        ):
            paths.append(str(exe))
    return paths


def _read_manifest(path: Path, *, owner_uid: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != owner_uid or stat.S_IMODE(st.st_mode) != MANIFEST_MODE:
            raise SyncError(f"{path} is not a {oct(MANIFEST_MODE)} regular file owned by uid {owner_uid}")
        raw = os.read(fd, MAX_MANIFEST_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise SyncError(f"{path} is too large")
    return raw


def _atomic_write(path: Path, data: bytes, *, mode: int, owner_uid: int, owner_gid: int,
                  validate: Validator | None = None) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fchown(fh.fileno(), owner_uid, owner_gid)
            os.fchmod(fh.fileno(), mode)
            os.fsync(fh.fileno())
        if validate is not None:
            validate(Path(tmp))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def _state_dir(state_dir: Path, *, owner_uid: int) -> Path:
    state_dir.mkdir(mode=0o700, exist_ok=True)
    st = os.lstat(state_dir)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != owner_uid or stat.S_IMODE(st.st_mode) & 0o077:
        raise SyncError(f"{state_dir} must be a {owner_uid}-owned 0700 directory")
    return state_dir


def _audit(state_dir: Path, event: dict) -> None:
    """Constraint 4: one JSON line per change, in a sibling root log."""
    fd = os.open(state_dir / AUDIT_NAME, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, (json.dumps(event, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def check_manifest_baseline(manifest: Path, state_dir: Path, *, manifest_owner_uid: int) -> bytes:
    """Constraint 1 (drift): the live manifest must be byte-identical to the copy
    this tool last wrote. Anything else, a hand edit included, fails closed
    until an operator reviews it and runs --rebaseline."""
    raw = _read_manifest(manifest, owner_uid=manifest_owner_uid)
    shadow = state_dir / SHADOW_NAME
    if not shadow.is_file():
        raise SyncError(f"no baseline at {shadow}; an operator must run --rebaseline")
    if raw != shadow.read_bytes():
        raise SyncError(f"{manifest} changed since the last root write; review it, then run --rebaseline")
    return raw


def regenerate_allowlist(
    manifest: Path, target_key: str, paths: list[str], *, state_dir: Path, validate: Validator,
    manifest_owner_uid: int = 0, manifest_owner_gid: int = 0, account: str = "",
) -> dict | None:
    """Rewrite ONLY targets[target_key].foreground_exe_allowlist (constraints
    1-4). Returns the audit event, or None when the list is already right."""
    state_dir = _state_dir(state_dir, owner_uid=manifest_owner_uid)
    raw = check_manifest_baseline(manifest, state_dir, manifest_owner_uid=manifest_owner_uid)
    obj = json.loads(raw)
    target = obj["targets"][target_key]
    old = list(target["foreground_exe_allowlist"])
    if old == paths:
        return None
    if not paths:
        raise SyncError("refusing to write an empty allowlist")
    target["foreground_exe_allowlist"] = list(paths)
    new = canonical(obj)
    proof = json.loads(new)
    proof["targets"][target_key]["foreground_exe_allowlist"] = old
    if canonical(proof) != raw:
        raise SyncError("the rewrite would change more than the allowlist; refusing")
    _atomic_write(manifest, new, mode=MANIFEST_MODE, owner_uid=manifest_owner_uid,
                  owner_gid=manifest_owner_gid, validate=validate)
    _atomic_write(state_dir / SHADOW_NAME, new, mode=0o600, owner_uid=manifest_owner_uid,
                  owner_gid=manifest_owner_gid)
    event = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "op": "allowlist",
        "account": account, "target": target_key, "old": old, "new": list(paths),
        "versions": [Path(p).parent.name for p in paths],
        "sha256_before": _sha(raw), "sha256_after": _sha(new),
    }
    _audit(state_dir, event)
    return event


def rebaseline(
    manifest: Path, *, state_dir: Path, validate: Validator,
    manifest_owner_uid: int = 0, manifest_owner_gid: int = 0,
) -> dict:
    """Operator action (never the timer): accept the manifest as it is now.
    It is validated, rewritten in canonical form (same JSON, normalised
    whitespace) and recorded as the baseline."""
    state_dir = _state_dir(state_dir, owner_uid=manifest_owner_uid)
    raw = _read_manifest(manifest, owner_uid=manifest_owner_uid)
    validate(manifest)
    new = canonical(json.loads(raw))
    if new != raw:
        _atomic_write(manifest, new, mode=MANIFEST_MODE, owner_uid=manifest_owner_uid,
                      owner_gid=manifest_owner_gid, validate=validate)
    _atomic_write(state_dir / SHADOW_NAME, new, mode=0o600, owner_uid=manifest_owner_uid,
                  owner_gid=manifest_owner_gid)
    event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "op": "rebaseline",
             "sha256_before": _sha(raw), "sha256_after": _sha(new)}
    _audit(state_dir, event)
    return event


def broker_validator(broker: Path = BROKER_PATH) -> Validator:
    """Validate through the INSTALLED broker's own loader (constraint 3). It is
    imported only if it is a root-owned regular file that is not group- or
    other-writable."""
    st = os.lstat(broker)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
        raise SyncError(f"{broker} is not a root-owned, non-writable regular file")
    loader = importlib.machinery.SourceFileLoader("_codex_recovery_broker", str(broker))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules[cls.__module__], so
    # the module must be registered before it executes (else AttributeError).
    sys.modules[loader.name] = module
    loader.exec_module(module)

    def validate(path: Path) -> object:
        return module.load_broker_manifest(path)
    return validate


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dest", type=Path, required=True)
    ap.add_argument("--account")
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    ap.add_argument("--list", action="store_true", help="print the versioned exe paths present")
    ap.add_argument("--manifest", type=Path, help=f"regenerate this unstick manifest's allowlist (e.g. {MANIFEST_PATH})")
    ap.add_argument("--target-key", help="manifest target to rewrite (default: --account)")
    ap.add_argument("--state-dir", type=Path, default=STATE_DIR)
    ap.add_argument("--broker", type=Path, default=BROKER_PATH)
    ap.add_argument("--rebaseline", action="store_true",
                    help="OPERATOR ONLY: accept the manifest as it is now (after review) as the baseline")
    args = ap.parse_args(argv)
    if args.list:
        print("\n".join(list_exe_paths(args.dest)))
        return 0
    if os.geteuid() != 0:
        print("claude_exe_sync: must run as root", file=sys.stderr)
        return 2
    try:
        if args.rebaseline:
            if args.manifest is None:
                ap.error("--rebaseline needs --manifest")
            result = rebaseline(args.manifest, state_dir=args.state_dir, validate=broker_validator(args.broker))
        else:
            if not args.account:
                ap.error("--account is required unless --list or --rebaseline")
            home = Path(pwd.getpwnam(args.account).pw_dir)
            result = sync(
                args.account, args.dest, account_home=home, keep=max(args.keep, 1),
                manifest=args.manifest, target_key=args.target_key or args.account,
                state_dir=args.state_dir,
                validate=broker_validator(args.broker) if args.manifest else None,
            )
    except (SyncError, OSError, KeyError, ValueError) as exc:
        print(f"claude_exe_sync: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0

if __name__ == "__main__":
    sys.exit(main())
