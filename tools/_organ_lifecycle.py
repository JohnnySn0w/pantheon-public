"""tools/_organ_lifecycle.py -- shared ORG-049 organ-lifecycle engine (T-2696).

Extracted from tools/consume_onboard.py (T-2679, the consume-plane seat organ)
and tools/mailbox_onboard.py (T-2683, the mailbox organ) once the mailbox tool
became the second proven instance of the same shape. T-2683 deliberately
COPIED (not imported) its CommandRunner seam from consume_onboard.py as a
rule-of-three defer ("a narrow per-organ tool now, no shared engine until a
third instance proves the rule of three" -- see mailbox_onboard.py's own
comment at the time). This module is that third-instance extraction: the
command seam and the small set of filesystem/git/identity primitives that
were byte-identical between the two tools move here; each tool imports them
instead of keeping its own copy.

Deliberately NOT extracted (T-2696 drift-verification, see the ticket's
DECISIONS): consume_onboard.GroupMembership (1-method Protocol) and
mailbox_onboard.HostIdentity (3-method Protocol) are NOT the same type --
HostIdentity is a documented generalization of GroupMembership (mailbox_onboard
needs "does this user/group exist" in addition to membership), with a
different Fake constructor shape that both tools' test suites build directly
by name. Unifying the Protocol would be a real interface change, not a pure
extraction. What WAS identical between the two tools' Real implementations is
the underlying algorithm -- see group_membership_lookup() below, which both
RealGroupMembership.is_member() (consume_onboard.py) and
RealHostIdentity.user_in_group() (mailbox_onboard.py) delegate to. Likewise
_unit_banner() (different field sets per tool) and each tool's uid-gate
control flow (consume_onboard raises on mismatch in a single gate;
mailbox_onboard skips-and-continues per independently-gated leg) stay
tool-local -- forcing either into a shared function would change one tool's
actual behavior.
"""

from __future__ import annotations

import grp
import os
import pwd
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence

MANAGED_MARKER = "Managed by pantheon persona installer"


# --------------------------------------------------------------------------
# Command seam. A single-method Protocol (mirroring subprocess.run 1:1)
# rather than a multi-method HostBackend-style Protocol -- smallest surface
# that's fully fakeable; one seam covers systemctl + tmux + pgrep + useradd
# etc uniformly. Real impl shells out; Fake impl is a scripted, call-
# recording double so no test ever touches the live host.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, argv: Sequence[str]) -> CommandResult: ...


class SubprocessCommandRunner:
    def run(self, argv: Sequence[str]) -> CommandResult:
        completed = subprocess.run(list(argv), capture_output=True, text=True, check=False)
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class FakeCommandRunner:
    """Test double. `scripted` maps an exact argv tuple to the CommandResult
    it should return; unscripted calls get `default` (success, empty output)
    so tests only need to script the calls whose return value they actually
    care about. Every call is recorded in `.calls` in order, so tests can
    assert both invocation and sequencing."""

    def __init__(
        self,
        scripted: Mapping[tuple[str, ...], CommandResult] | None = None,
        default: CommandResult = CommandResult(0, "", ""),
    ) -> None:
        self.scripted = dict(scripted or {})
        self.default = default
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str]) -> CommandResult:
        key = tuple(argv)
        self.calls.append(key)
        return self.scripted.get(key, self.default)


class OnboardError(RuntimeError):
    """Bounded, user-facing failure: bad manifest, blocked precondition,
    wrong uid, refused mutation. Never raised for an unexpected bug."""


@dataclass(frozen=True)
class PreconditionCheck:
    name: str
    ok: bool
    detail: str


# --------------------------------------------------------------------------
# Group-membership algorithm. Byte-identical between
# consume_onboard.RealGroupMembership.is_member() and
# mailbox_onboard.RealHostIdentity.user_in_group() before this extraction
# (verified via AST diff, T-2696) -- the actual duplication risk this closes
# is a fix landing in one copy and not the other. The two tools' own
# Protocol/Fake wrapper types are NOT unified (see module docstring); only
# this shared inner algorithm moves.
# --------------------------------------------------------------------------


def group_membership_lookup(account: str, group: str) -> tuple[bool, str]:
    try:
        group_entry = grp.getgrnam(group)
    except KeyError:
        return False, f"group {group!r} does not exist on this host"
    if account in group_entry.gr_mem:
        return True, f"{account} is a member of {group}"
    try:
        pw = pwd.getpwnam(account)
    except KeyError:
        return False, f"account {account!r} does not exist on this host"
    if pw.pw_gid == group_entry.gr_gid:
        return True, f"{account} has {group} as its primary group"
    return False, f"{account} is not a member of {group}"


# --------------------------------------------------------------------------
# Filesystem helpers. Byte-identical between the two tools before extraction.
# --------------------------------------------------------------------------


def _ensure_dir(path: Path, mode: int) -> bool:
    existed = path.is_dir()
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    return not existed


def _write_if_changed(path: Path, content: bytes) -> bool:
    if path.is_file() and path.read_bytes() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def _rendered_unit(template_path: Path, banner: str) -> bytes:
    return (banner + template_path.read_text(encoding="utf-8")).encode("utf-8")


# --------------------------------------------------------------------------
# Git / host-identity / manifest-path helpers. Byte-identical between the two
# tools before extraction.
# --------------------------------------------------------------------------


def _git_head(repo_root: Path) -> str:
    result = subprocess.run(
        ("git", "-C", str(repo_root), "rev-parse", "HEAD"),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return "unversioned"
    return result.stdout.strip()


def _current_account() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def _default_manifest_path() -> Path:
    return Path.home() / "pantheon-config" / "instance.yaml"
