"""codex-recovery broker — ORG-048 §5 privilege surface, increment 1 (T-2805).

INERT in this increment: this module is a Python file in the repo. Nothing is
installed, no sudoers entry points at it, `codex` stays in group `sudo`. It
grants nothing until increment 2 (HO+Recovery-gated: install to
/usr/local/sbin/codex-recovery, the sudoers.d entry, the group-sudo removal,
the Recovery-supervised first-fire).

At increment 2 this program is invoked as root via a single sudoers line:
    codex ALL=(root) NOPASSWD: /usr/local/sbin/codex-recovery
`codex` passes an enum verb + a target key; this broker validates against a
root-owned manifest, drops to the target account via a hardened root->target
transition, executes exactly one fixed action, and writes a tamper-resistant
audit record. It is the sole privileged surface — the proto-Action-Broker
(ORG-063) this ADR converges toward.

Verb set THIS increment: `kill-session`, `unstick` (control-keys only,
`wake-drop-cancel` | `spend-modal-dismiss`), and `selftest` (manifest
preflight, touches nothing). `capture`, `restart`/`reset-failed`, `restore`
are explicitly deferred (build spec §Scope).

Design precedent this module follows deliberately, not by accident:
  - tools/credential_broker.py — O_NOFOLLOW + fstat-after-open validation
    (never stat-by-path), fail-closed flock'd audit appends, bounded
    identifier regexes. The manifest-loading half of this module is that
    pattern applied to a different artifact.
  - tools/_pane_predicates.py — I/O-gathering kept separate from the pure
    decision predicate. The unstick foreground-process-group check is split
    the same way: `PaneForegroundProbe` gathers OS facts (best-effort — real
    cross-account /proc+tmux fidelity is proven live at the Recovery-supervised
    increment-2 first-fire, ADR ORG-048 install-gate item 3, not here);
    `_validate_foreground_snapshot` is the exhaustively-unit-tested decision.
  - tools/_lease.py — flock-based, daemon-safe state coordination is the
    model for the rate-limit counter below (simpler: no staleness/reaping
    needed here, since each invocation is a single short-lived process).

Revalidate-before-act (no parse-once-act-later TOCTOU, install-gate item 2):
each broker invocation is a ONE-SHOT CLI process — one sudoers exec runs
exactly one verb. The manifest load happens once, immediately before the
single action it authorizes; there is no long-lived cache that could go
stale between "parse" and "act". `build_kill_session_argv` /
`build_unstick_argv` additionally re-resolve the account's live uid via
`getpwnam` at argv-build time and refuse if it no longer matches the
manifest-declared uid, as a second, paranoid close of the same window.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import pwd
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


# ---------------------------------------------------------------------------
# Absolute binary paths (contract item 1 — never resolved via $PATH search).
# ---------------------------------------------------------------------------

TMUX = "/usr/bin/tmux"
SETPRIV = "/usr/bin/setpriv"

# ---------------------------------------------------------------------------
# Defaults (all overridable — the real, root-owned paths at increment-2
# install time; tests inject tempdir equivalents throughout).
# ---------------------------------------------------------------------------

DEFAULT_MANIFEST_PATH = Path("/etc/codex-recovery/manifest.json")
DEFAULT_AUDIT_LOG_PATH = Path("/var/lib/codex-recovery/audit.jsonl")
DEFAULT_RATE_LIMIT_STATE_DIR = Path("/run/codex-recovery")

MANIFEST_EXPECTED_OWNER_UID = 0
MANIFEST_EXPECTED_MODE = 0o644
MANIFEST_PARENT_EXPECTED_MODE = 0o755
MAX_MANIFEST_BYTES = 65536

SUBPROCESS_TIMEOUT_SECS = 10
DEFAULT_UMASK = 0o077

RATE_LIMIT_WINDOW_SECS = 300
RATE_LIMIT_MAX_ACTIONS = 3

ENABLED_VERBS = ("kill-session", "unstick", "selftest")
UNSTICK_ACTIONS = ("wake-drop-cancel", "spend-modal-dismiss")

# Fixed, non-negotiable key sequences — control keys ONLY this increment (no
# retype-template; see ADR ORG-048 D3 + the access-model round-2 disposition).
# wake-drop-cancel mirrors the live wake-drop fix's first step (C-c) minus
# the deferred retype; spend-modal-dismiss is the live remediation recipe
# (sop/remediation/agent-spend-limit-wedge.md step 3: `tmux send-keys Escape`).
UNSTICK_KEY_SEQUENCES: dict[str, tuple[str, ...]] = {
    "wake-drop-cancel": ("C-c",),
    "spend-modal-dismiss": ("Escape",),
}

# Defense-in-depth on top of the allowlist: refuse outright if the foreground
# exe's basename is one of these, regardless of allowlist/ownership state.
DENYLIST_FOREGROUND_BASENAMES = frozenset({
    "bash", "sh", "zsh", "dash", "fish", "ksh", "tcsh", "csh",
    "vim", "vi", "nvim", "nano", "emacs", "pico",
    "less", "more", "most",
    "tmux", "screen",
    "ssh", "sudo", "su", "doas", "setpriv", "runuser",
    "python", "python3", "perl", "ruby", "node", "php", "lua", "irb",
})

# Root only by default. A real "deploy-owned, not root" wrapper install is a
# reconciliation the increment-2 install plan owns (see module docstring's
# sibling note in the T-2805 plan report): today's live claude.exe binaries
# are <account>:pantheon-shared 0755, NOT root-owned, so this check as
# written REFUSES against them — intentionally; it is not weakened to match.
DEFAULT_DEPLOY_OWNER_UIDS: frozenset[int] = frozenset({0})

NON_FLEET_ACCOUNT_NAMES = frozenset({"root", "codex"})
MIN_FLEET_UID = 1000
MAX_FLEET_UID = 60000  # excludes nobody/nogroup (65534) and other pseudo-uids

_AUDIT_FORBIDDEN_KEYS = frozenset({"pane_text", "pane_content", "cmdline", "stdout", "stderr"})


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------

class CodexRecoveryError(RuntimeError):
    """Base error for the codex-recovery broker."""


class ManifestValidationError(CodexRecoveryError):
    """The manifest failed structural, ownership, or content validation."""


class DispatchRefusedError(CodexRecoveryError):
    """A dispatch precondition failed; the caller must refuse to act."""


class AuditWriteError(CodexRecoveryError):
    """The audit sink could not be written — callers must fail closed."""


# ---------------------------------------------------------------------------
# Bounded, metachar-free identifier / path validation.
# ---------------------------------------------------------------------------

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,62}[A-Za-z0-9])?$")
_MAX_IDENTIFIER_LEN = 64

_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._@-]+$")
_MAX_PATH_LEN = 4096


def _validate_identifier(label: str, value: object) -> None:
    if not isinstance(value, str):
        raise ManifestValidationError(f"{label} must be a string")
    if len(value) > _MAX_IDENTIFIER_LEN or not _IDENTIFIER_RE.fullmatch(value):
        raise ManifestValidationError(f"{label} must be a bounded, safe identifier")


def _validate_abs_path(label: str, value: object) -> None:
    if not isinstance(value, str):
        raise ManifestValidationError(f"{label} must be a string")
    if len(value) > _MAX_PATH_LEN or not value.startswith("/"):
        raise ManifestValidationError(f"{label} must be a bounded absolute path")
    parts = value.split("/")[1:]
    if not parts or parts[-1] == "":
        raise ManifestValidationError(f"{label} must not be the filesystem root or end in a slash")
    for part in parts:
        if part in (".", ".."):
            raise ManifestValidationError(f"{label} must not contain traversal segments")
        if not _PATH_COMPONENT_RE.fullmatch(part):
            raise ManifestValidationError(f"{label} contains unsafe characters")


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _read_all(fd: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ManifestValidationError("manifest exceeds maximum size")
        chunks.append(chunk)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# Manifest data model.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TargetSpec:
    key: str
    account: str
    uid: int
    tmux_session: str
    user_unit: str
    foreground_exe_allowlist: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_identifier("target key", self.key)
        _validate_identifier("account", self.account)
        if not isinstance(self.uid, int) or isinstance(self.uid, bool) or self.uid < 0:
            raise ManifestValidationError("uid must be a non-negative integer")
        _validate_identifier("tmux_session", self.tmux_session)
        _validate_identifier("user_unit", self.user_unit)
        if not self.foreground_exe_allowlist:
            raise ManifestValidationError("foreground_exe_allowlist must not be empty")
        for entry in self.foreground_exe_allowlist:
            _validate_abs_path("foreground_exe_allowlist entry", entry)


# ---------------------------------------------------------------------------
# Strict JSON parsing: reject duplicate keys structurally.
# ---------------------------------------------------------------------------

class _DuplicateKeyError(ValueError):
    pass


def _no_dup_object_pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateKeyError(f"duplicate key {key!r} in manifest JSON")
        seen[key] = value
    return seen


# Reject any literal control byte other than the JSON whitespace set
# (tab/newline/CR — all of which are themselves disallowed *inside* JSON
# strings by the JSON spec, so this only ever trims structural padding).
_CONTROL_CHAR_RE = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_TOP_LEVEL_KEYS = frozenset({"targets"})
_TARGET_KEYS = frozenset({"account", "uid", "tmux_session", "user_unit", "foreground_exe_allowlist"})


def _parse_manifest_json(raw: bytes) -> dict[str, object]:
    if not raw:
        raise ManifestValidationError("manifest is empty")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestValidationError("manifest exceeds maximum size")
    if _CONTROL_CHAR_RE.search(raw):
        raise ManifestValidationError("manifest contains disallowed control bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ManifestValidationError("manifest is not valid utf-8") from exc
    try:
        obj = json.loads(text, object_pairs_hook=_no_dup_object_pairs_hook)
    except _DuplicateKeyError as exc:
        raise ManifestValidationError(str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise ManifestValidationError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ManifestValidationError("manifest root must be a JSON object")
    unknown = set(obj) - _TOP_LEVEL_KEYS
    if unknown:
        raise ManifestValidationError(f"manifest has unknown top-level fields: {sorted(unknown)}")
    missing = _TOP_LEVEL_KEYS - set(obj)
    if missing:
        raise ManifestValidationError(f"manifest is missing required fields: {sorted(missing)}")
    targets_obj = obj["targets"]
    if not isinstance(targets_obj, dict) or not targets_obj:
        raise ManifestValidationError("manifest 'targets' must be a non-empty object")
    return targets_obj


def _parse_target(key: str, raw_target: object, *, pwnam: Callable[[str], object]) -> TargetSpec:
    if not isinstance(raw_target, dict):
        raise ManifestValidationError(f"target {key!r} must be a JSON object")
    unknown = set(raw_target) - _TARGET_KEYS
    if unknown:
        raise ManifestValidationError(f"target {key!r} has unknown fields: {sorted(unknown)}")
    missing = _TARGET_KEYS - set(raw_target)
    if missing:
        raise ManifestValidationError(f"target {key!r} is missing fields: {sorted(missing)}")

    account = raw_target["account"]
    uid_field = raw_target["uid"]
    tmux_session = raw_target["tmux_session"]
    user_unit = raw_target["user_unit"]
    allowlist = raw_target["foreground_exe_allowlist"]

    if not isinstance(account, str) or not isinstance(tmux_session, str) or not isinstance(user_unit, str):
        raise ManifestValidationError(f"target {key!r} has non-string fields")
    if not isinstance(uid_field, int) or isinstance(uid_field, bool):
        raise ManifestValidationError(f"target {key!r} uid must be an integer")
    if not isinstance(allowlist, list) or not allowlist or not all(isinstance(p, str) for p in allowlist):
        raise ManifestValidationError(f"target {key!r} foreground_exe_allowlist must be a non-empty list of strings")

    if account in NON_FLEET_ACCOUNT_NAMES:
        raise ManifestValidationError(f"target {key!r} account {account!r} is not a fleet persona")

    try:
        pw = pwnam(account)
    except KeyError as exc:
        raise ManifestValidationError(
            f"target {key!r} account {account!r} does not resolve via getpwnam"
        ) from exc

    if pw.pw_uid < MIN_FLEET_UID or pw.pw_uid > MAX_FLEET_UID:
        raise ManifestValidationError(
            f"target {key!r} account {account!r} is a system/pseudo account (uid {pw.pw_uid})"
        )
    if pw.pw_uid != uid_field:
        raise ManifestValidationError(
            f"target {key!r} manifest uid {uid_field} does not match resolved uid {pw.pw_uid}"
        )

    return TargetSpec(
        key=key,
        account=account,
        uid=pw.pw_uid,
        tmux_session=tmux_session,
        user_unit=user_unit,
        foreground_exe_allowlist=tuple(allowlist),
    )


# ---------------------------------------------------------------------------
# Open-time validation: O_NOFOLLOW + fstat-after-open, never stat-by-path.
# Mirrors tools/credential_broker.py's single-validated-parent pattern.
# ---------------------------------------------------------------------------

def _open_flags(extra: int = 0) -> int:
    flags = os.O_RDONLY | extra
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _open_validated_parent(parent_dir: Path, *, expected_owner_uid: int, expected_mode: int) -> int:
    flags = _open_flags(os.O_DIRECTORY if hasattr(os, "O_DIRECTORY") else 0)
    try:
        fd = os.open(parent_dir, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ManifestValidationError("manifest parent directory must not be a symlink") from exc
        raise ManifestValidationError(f"cannot open manifest parent directory: {exc}") from exc
    try:
        st = os.fstat(fd)
        errors = []
        if not stat.S_ISDIR(st.st_mode):
            errors.append("manifest parent is not a directory")
        if st.st_uid != expected_owner_uid:
            errors.append("manifest parent is not owned by the expected owner")
        if stat.S_IMODE(st.st_mode) != expected_mode:
            errors.append("manifest parent mode does not match the expected mode")
        if errors:
            raise ManifestValidationError("; ".join(errors))
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_validated_leaf(parent_fd: int, name: str, *, expected_owner_uid: int, expected_mode: int) -> int:
    try:
        fd = os.open(name, _open_flags(), dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ManifestValidationError("manifest file must not be a symlink") from exc
        raise ManifestValidationError(f"cannot open manifest file: {exc}") from exc
    try:
        st = os.fstat(fd)
        errors = []
        if not stat.S_ISREG(st.st_mode):
            errors.append("manifest is not a regular file")
        if st.st_uid != expected_owner_uid:
            errors.append("manifest is not owned by the expected owner")
        mode = stat.S_IMODE(st.st_mode)
        if mode != expected_mode:
            errors.append("manifest mode does not match the expected mode")
        if errors:
            raise ManifestValidationError("; ".join(errors))
        return fd
    except Exception:
        os.close(fd)
        raise


def load_manifest(
    path: Path,
    *,
    expected_owner_uid: int = MANIFEST_EXPECTED_OWNER_UID,
    expected_mode: int = MANIFEST_EXPECTED_MODE,
    expected_parent_mode: int = MANIFEST_PARENT_EXPECTED_MODE,
    pwnam: Callable[[str], object] = pwd.getpwnam,
) -> dict[str, TargetSpec]:
    """Load + strictly validate the manifest. Fresh every call — see the
    module docstring's revalidate-before-act note; there is no cache."""
    path = Path(path)
    if not path.is_absolute():
        raise ManifestValidationError("manifest path must be absolute")
    if path.name in ("", ".", ".."):
        raise ManifestValidationError("manifest path must name a regular file")

    parent_fd = _open_validated_parent(
        path.parent, expected_owner_uid=expected_owner_uid, expected_mode=expected_parent_mode
    )
    try:
        leaf_fd = _open_validated_leaf(
            parent_fd, path.name, expected_owner_uid=expected_owner_uid, expected_mode=expected_mode
        )
    finally:
        os.close(parent_fd)
    try:
        raw = _read_all(leaf_fd, MAX_MANIFEST_BYTES)
    finally:
        os.close(leaf_fd)

    targets_obj = _parse_manifest_json(raw)
    targets: dict[str, TargetSpec] = {}
    for key, raw_target in targets_obj.items():
        _validate_identifier("target key", key)
        targets[key] = _parse_target(key, raw_target, pwnam=pwnam)
    return targets


# ---------------------------------------------------------------------------
# unstick: foreground-process-group validation.
#
# Split per the tools/_pane_predicates.py convention: gathering (I/O, real
# tmux + /proc reads, best-effort — proven live at the Recovery-supervised
# increment-2 first-fire, NOT here) vs. deciding (pure, exhaustively tested).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ForegroundSnapshot:
    """Facts about the pane's foreground process, already gathered."""

    pid: int
    pgid: int
    exe_path: str          # os.readlink(/proc/<pid>/exe) — already resolved
    exe_owner_uid: int
    exe_mode: int           # stat.S_IMODE of the exe file
    cmdline: tuple[str, ...]
    sid: int
    pane_sid: int            # the pane's own (session-leader) sid, for cross-check
    tty_matches_pane: bool


def _validate_foreground_snapshot(
    snapshot: ForegroundSnapshot,
    target: TargetSpec,
    *,
    deploy_owner_uids: frozenset[int] = DEFAULT_DEPLOY_OWNER_UIDS,
) -> None:
    """Raise DispatchRefusedError unless the pane's foreground process GROUP
    (never a descendant) is an exact, safely-owned, allowlisted wrapper."""
    if snapshot.pid != snapshot.pgid:
        raise DispatchRefusedError(
            "foreground pid is not its own process-group leader (a descendant, not the group)"
        )
    basename = os.path.basename(snapshot.exe_path)
    if basename in DENYLIST_FOREGROUND_BASENAMES:
        raise DispatchRefusedError(f"foreground process is denylisted: {basename}")
    if snapshot.exe_path not in target.foreground_exe_allowlist:
        raise DispatchRefusedError("foreground exe is not on the target's allowlist")
    if snapshot.exe_owner_uid not in deploy_owner_uids:
        raise DispatchRefusedError("foreground exe is not root/deploy-owned")
    if snapshot.exe_owner_uid == target.uid:
        raise DispatchRefusedError("foreground exe must not be owned by the target account")
    if snapshot.exe_mode & 0o022:
        raise DispatchRefusedError("foreground exe is group- or other-writable")
    if snapshot.sid != snapshot.pane_sid:
        raise DispatchRefusedError("foreground process session id does not match the pane session")
    if not snapshot.tty_matches_pane:
        raise DispatchRefusedError("foreground process controlling tty does not match the pane tty")


def _tty_match(foreground_tty_dev: int, pane_tty_dev: int) -> bool:
    """Compare two raw dev_t values by (major, minor), not by raw integer —
    the /proc/<pid>/stat tty_nr encoding and a stat()'d st_rdev use the same
    glibc dev_t convention, which os.major/os.minor decode correctly."""
    return (os.major(foreground_tty_dev), os.minor(foreground_tty_dev)) == (
        os.major(pane_tty_dev),
        os.minor(pane_tty_dev),
    )


class PaneForegroundProbe:
    """Best-effort gatherer of pane-foreground facts via tmux + /proc.

    Live fidelity against a real cross-account pane is proven at the
    Recovery-supervised increment-2 first-fire (ADR ORG-048 install-gate item 3),
    not here — this increment wires the structure and the (exhaustively
    tested) decision predicate above. Never invoked by the test suite against
    a real cross-account process; tests pass synthetic ForegroundSnapshot
    values straight to the predicate.
    """

    def __init__(self, *, tmux_bin: str = TMUX, proc_root: Path = Path("/proc")):
        self._tmux_bin = tmux_bin
        self._proc_root = proc_root

    def snapshot(self, tmux_session: str) -> ForegroundSnapshot:
        pane_pid, pane_tty = self._pane_facts(tmux_session)
        pane_fields = self._read_stat_fields(pane_pid)
        fg_pid = pane_fields["tpgid"]
        fg_fields = pane_fields if fg_pid == pane_pid else self._read_stat_fields(fg_pid)

        exe_path = os.readlink(self._proc_root / str(fg_pid) / "exe")
        exe_st = os.stat(exe_path)
        pane_dev = os.stat(pane_tty).st_rdev

        return ForegroundSnapshot(
            pid=fg_pid,
            pgid=fg_fields["pgrp"],
            exe_path=exe_path,
            exe_owner_uid=exe_st.st_uid,
            exe_mode=stat.S_IMODE(exe_st.st_mode),
            cmdline=self._read_cmdline(fg_pid),
            sid=fg_fields["session"],
            pane_sid=pane_fields["session"],
            tty_matches_pane=_tty_match(fg_fields["tty_nr"], pane_dev),
        )

    def _pane_facts(self, tmux_session: str) -> tuple[int, str]:
        out = subprocess.run(
            [self._tmux_bin, "display-message", "-p", "-t", tmux_session, "#{pane_pid}\t#{pane_tty}"],
            capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SECS, check=True,
        )
        pid_str, tty_path = out.stdout.strip().split("\t", 1)
        return int(pid_str), tty_path

    def _read_stat_fields(self, pid: int) -> dict[str, int]:
        text = (self._proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        # comm (field 2) is parenthesized and may itself contain ')' or
        # whitespace, so split on the LAST ')' to safely reach field 3+.
        after_comm = text.rsplit(")", 1)[1].split()
        # after_comm[0] is field 3 (state); fields below are 1-indexed from there.
        return {
            "ppid": int(after_comm[1]),     # field 4
            "pgrp": int(after_comm[2]),     # field 5
            "session": int(after_comm[3]),  # field 6
            "tty_nr": int(after_comm[4]),   # field 7
            "tpgid": int(after_comm[5]),    # field 8
        }

    def _read_cmdline(self, pid: int) -> tuple[str, ...]:
        raw = (self._proc_root / str(pid) / "cmdline").read_bytes()
        return tuple(p.decode("utf-8", "replace") for p in raw.split(b"\x00") if p)


# ---------------------------------------------------------------------------
# Root -> target transition + argv construction (contract item 1).
# ---------------------------------------------------------------------------

def _resolve_account(account: str, *, pwnam: Callable[[str], object]):
    return pwnam(account)


def _build_env(target: TargetSpec, *, pwnam: Callable[[str], object] = pwd.getpwnam) -> dict[str, str]:
    pw = _resolve_account(target.account, pwnam=pwnam)
    return {
        "HOME": pw.pw_dir,
        "USER": target.account,
        "LOGNAME": target.account,
        "SHELL": pw.pw_shell or "/bin/bash",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "XDG_RUNTIME_DIR": f"/run/user/{pw.pw_uid}",
    }


def _setpriv_prefix(uid: int, gid: int) -> list[str]:
    return [
        SETPRIV,
        "--reuid", str(uid),
        "--regid", str(gid),
        "--init-groups",
        "--reset-env",
        "--no-new-privs",
        "--",
    ]


def _revalidated_uid_gid(target: TargetSpec, *, pwnam: Callable[[str], object]) -> tuple[int, int]:
    pw = _resolve_account(target.account, pwnam=pwnam)
    if pw.pw_uid != target.uid:
        raise DispatchRefusedError(
            f"account {target.account!r} uid changed since manifest load — refusing"
        )
    return pw.pw_uid, pw.pw_gid


def build_kill_session_argv(target: TargetSpec, *, pwnam: Callable[[str], object] = pwd.getpwnam) -> list[str]:
    uid, gid = _revalidated_uid_gid(target, pwnam=pwnam)
    return _setpriv_prefix(uid, gid) + [TMUX, "kill-session", "-t", target.tmux_session]


def build_unstick_argv(
    target: TargetSpec, action: str, *, pwnam: Callable[[str], object] = pwd.getpwnam
) -> list[str]:
    if action not in UNSTICK_KEY_SEQUENCES:
        raise DispatchRefusedError(f"unknown unstick action: {action!r}")
    uid, gid = _revalidated_uid_gid(target, pwnam=pwnam)
    keys = UNSTICK_KEY_SEQUENCES[action]
    return _setpriv_prefix(uid, gid) + [TMUX, "send-keys", "-t", target.tmux_session, *keys]


# ---------------------------------------------------------------------------
# Audit — append-only, flock-serialized, fail-closed for the action it gates.
# Never logs pane content (contract §Audit).
# ---------------------------------------------------------------------------

class AuditSink:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.monotonic):
        self.path = Path(path)
        self._clock = clock

    def write(self, **fields: object) -> None:
        forbidden = _AUDIT_FORBIDDEN_KEYS & set(fields)
        if forbidden:
            raise ValueError(f"audit record must not carry pane/output content: {sorted(forbidden)}")
        record = {"monotonic_ts": self._clock(), **fields}
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise AuditWriteError(f"cannot prepare audit directory: {exc}") from exc
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise AuditWriteError(f"cannot open audit log: {exc}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            payload = (json.dumps(record, sort_keys=True, default=str) + "\n").encode("utf-8")
            _write_all(fd, payload)
            os.fsync(fd)
        except OSError as exc:
            raise AuditWriteError(f"cannot write audit log: {exc}") from exc
        finally:
            os.close(fd)


def _best_effort_audit(audit: "AuditSink | Callable", **fields: object) -> None:
    """For refusal paths: no action is happening either way, so an audit
    write failure here doesn't change the outcome — attempt it, don't crash."""
    try:
        audit.write(**fields)
    except AuditWriteError as exc:
        print(f"codex-recovery: audit write failed (refusal already applied): {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Rate limiting — flock-serialized sliding window, root-owned runtime dir.
# ---------------------------------------------------------------------------

class RateLimiter:
    def __init__(
        self,
        state_dir: Path,
        *,
        window_secs: int = RATE_LIMIT_WINDOW_SECS,
        max_actions: int = RATE_LIMIT_MAX_ACTIONS,
        clock: Callable[[], float] = time.time,
    ):
        self.state_dir = Path(state_dir)
        self.window_secs = window_secs
        self.max_actions = max_actions
        self._clock = clock

    def check_and_record(self, key: str) -> bool:
        _validate_identifier("rate-limit key", key)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        state_path = self.state_dir / f"{key}.json"
        fd = os.open(state_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = _read_all(fd, 1_000_000)
            try:
                history = json.loads(raw.decode("utf-8")) if raw else []
            except (json.JSONDecodeError, UnicodeDecodeError):
                history = []
            if not isinstance(history, list):
                history = []
            now = self._clock()
            history = [t for t in history if isinstance(t, (int, float)) and now - t < self.window_secs]
            if len(history) >= self.max_actions:
                return False
            history.append(now)
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            _write_all(fd, json.dumps(history).encode("utf-8"))
            os.fsync(fd)
            return True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


# ---------------------------------------------------------------------------
# Dispatch — the single entry point every verb runs through.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DispatchResult:
    outcome: str  # "executed" | "dry_run" | "refused"
    detail: str
    exit_code: int | None = None


class SubprocessRunner(Protocol):
    def __call__(self, argv: list[str], *, env: dict[str, str], timeout: float): ...


def _real_runner(argv: list[str], *, env: dict[str, str], timeout: float):
    return subprocess.run(
        argv,
        env=env,
        timeout=timeout,
        umask=DEFAULT_UMASK,
        close_fds=True,
        capture_output=True,
        text=True,
        shell=False,
    )


def dispatch(
    *,
    verb: str,
    target_key: str,
    action: str | None,
    manifest_path: Path,
    audit: AuditSink,
    rate_limiter: RateLimiter,
    runner: SubprocessRunner = _real_runner,
    foreground_probe: Callable[[str], ForegroundSnapshot] | None = None,
    deploy_owner_uids: frozenset[int] = DEFAULT_DEPLOY_OWNER_UIDS,
    dry_run: bool = False,
    manifest_loader: Callable[..., dict[str, TargetSpec]] = load_manifest,
    manifest_kwargs: dict | None = None,
    pwnam: Callable[[str], object] = pwd.getpwnam,
) -> DispatchResult:
    manifest_kwargs = dict(manifest_kwargs or {})
    manifest_kwargs.setdefault("pwnam", pwnam)

    def _refuse(reason: str) -> DispatchResult:
        _best_effort_audit(audit, event="refuse", verb=verb, target=target_key, action=action, reason=reason)
        return DispatchResult(outcome="refused", detail=reason)

    if verb not in ENABLED_VERBS or verb == "selftest":
        return _refuse(f"verb not dispatchable: {verb!r}")

    try:
        targets = manifest_loader(manifest_path, **manifest_kwargs)
    except ManifestValidationError as exc:
        return _refuse(f"manifest invalid: {exc}")

    target = targets.get(target_key)
    if target is None:
        return _refuse("unknown target")

    if verb == "kill-session":
        try:
            argv = build_kill_session_argv(target, pwnam=pwnam)
        except DispatchRefusedError as exc:
            return _refuse(str(exc))
    elif verb == "unstick":
        if action not in UNSTICK_ACTIONS:
            return _refuse(f"unknown unstick action: {action!r}")
        if foreground_probe is None:
            return _refuse("no foreground probe configured")
        try:
            snapshot = foreground_probe(target.tmux_session)
            _validate_foreground_snapshot(snapshot, target, deploy_owner_uids=deploy_owner_uids)
        except DispatchRefusedError as exc:
            return _refuse(str(exc))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            # The real PaneForegroundProbe reads /proc + shells out to tmux —
            # a vanished pane, a non-tty pid, or a tmux failure raises here,
            # not as DispatchRefusedError. Still fail-closed either way (no
            # action would run), but this keeps it a clean, audited refusal
            # instead of an uncaught traceback out of dispatch()/main().
            return _refuse(f"foreground probe failed: {exc}")
        try:
            argv = build_unstick_argv(target, action, pwnam=pwnam)
        except DispatchRefusedError as exc:
            return _refuse(str(exc))
    else:  # pragma: no cover — ENABLED_VERBS exhausted above
        return _refuse(f"unhandled verb: {verb!r}")

    rate_key = f"{verb}__{target_key}"
    if not rate_limiter.check_and_record(rate_key):
        return _refuse("rate limit exceeded")

    env = _build_env(target, pwnam=pwnam)

    if dry_run:
        _best_effort_audit(audit, event="dry_run", verb=verb, target=target_key, action=action, argv=argv)
        return DispatchResult(outcome="dry_run", detail="validated; not executed")

    # Fail-closed (contract item 5): the pre-action audit write MUST succeed
    # before we act. If it can't, refuse — never execute unaudited.
    try:
        audit.write(event="pre_action", verb=verb, target=target_key, action=action, argv=argv)
    except AuditWriteError as exc:
        return DispatchResult(outcome="refused", detail=f"audit sink unwritable: {exc}")

    result = runner(argv, env=env, timeout=SUBPROCESS_TIMEOUT_SECS)

    try:
        audit.write(event="post_action", verb=verb, target=target_key, action=action, exit_code=result.returncode)
    except AuditWriteError as exc:
        # Best-effort: the action already ran and can't be un-done. Loud on
        # stderr so this is never a silently-swallowed gap.
        print(f"codex-recovery: post-action audit write failed: {exc}", file=sys.stderr)

    outcome = "executed" if result.returncode == 0 else "refused"
    return DispatchResult(outcome=outcome, detail="subprocess completed", exit_code=result.returncode)


# ---------------------------------------------------------------------------
# selftest — manifest preflight only, touches nothing, needs no privilege.
# ---------------------------------------------------------------------------

def cmd_selftest(
    *,
    manifest_path: Path,
    pwnam: Callable[[str], object] = pwd.getpwnam,
    manifest_kwargs: dict | None = None,
) -> tuple[bool, dict]:
    manifest_kwargs = dict(manifest_kwargs or {})
    manifest_kwargs.setdefault("pwnam", pwnam)
    try:
        targets = load_manifest(manifest_path, **manifest_kwargs)
    except ManifestValidationError as exc:
        return False, {"ok": False, "error": str(exc)}
    binaries_present = all(os.path.exists(b) for b in (TMUX, SETPRIV))
    report = {
        "ok": binaries_present,
        "targets": sorted(targets),
        "binaries_present": binaries_present,
    }
    return report["ok"], report


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-recovery",
        description="ORG-048 scoped cross-account fleet-repair broker (increment 1: inert, unprivileged in-repo).",
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--audit-log", type=Path, default=DEFAULT_AUDIT_LOG_PATH)
    parser.add_argument("--rate-limit-dir", type=Path, default=DEFAULT_RATE_LIMIT_STATE_DIR)
    parser.add_argument("--dry-run", action="store_true")
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("selftest")
    p_kill = sub.add_parser("kill-session")
    p_kill.add_argument("target")
    p_unstick = sub.add_parser("unstick")
    p_unstick.add_argument("target")
    p_unstick.add_argument("action", choices=UNSTICK_ACTIONS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)

    if args.verb == "selftest":
        ok, report = cmd_selftest(manifest_path=args.manifest)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if ok else 1

    audit = AuditSink(args.audit_log)
    rate_limiter = RateLimiter(args.rate_limit_dir)
    probe = PaneForegroundProbe() if args.verb == "unstick" else None
    result = dispatch(
        verb=args.verb,
        target_key=args.target,
        action=getattr(args, "action", None),
        manifest_path=args.manifest,
        audit=audit,
        rate_limiter=rate_limiter,
        foreground_probe=probe.snapshot if probe else None,
        dry_run=args.dry_run,
    )
    print(json.dumps({"outcome": result.outcome, "detail": result.detail, "exit_code": result.exit_code}))
    return 0 if result.outcome in ("executed", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
