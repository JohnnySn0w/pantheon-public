"""ORG-052 gate 36: broker plans never amend the broker's own authority (T-2918).

ORG-052 §Bootstrap and self-amendment boundary: after bootstrap, typed
operations "may not modify the broker/approval/helper executables, their own
systemd units or bus ownership rules, the root operation/template registries,
approver policy, human allowlist, or the definition of their operation enums.
Those self-amendment surfaces require a new root-run reviewed installer plan."

The broker's plan composer refuses, whole-plan and before any handle is
minted, any operation that could change state (every class but
``diagnostics.read``) whose target lies in or under a protected surface. The
static roots below cover the staged deployment layout (``tools/host_control/
dist``: ``/usr/local/libexec`` executables, system units, D-Bus policy,
``/etc/pantheon`` root registries, templates, approver policy and allowlists,
sudoers, and the broker/approval journals' StateDirectory); the dynamic roots
add the directories the running broker actually loaded its op enums from (the
``tools.host_control`` and ``tools.germinate`` packages) and any trusted-config
roots the daemon is configured with.

Seat-side containment (the executor's seat-root confinement, the target uid's
own permissions) still stands behind this; this is the plan-time refusal the
gate names. The bootstrap in-process root path is NOT governed here: ORG-052
reserves these surfaces to the supervised, reviewed root installer.
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Final, Iterable

import tools.germinate as _germinate_pkg
import tools.host_control as _host_control_pkg
from tools.host_control.contracts import PolicyDenied
from tools.host_control.envelope import EXECUTOR_KIND_CLASSES

READ_ONLY_CLASS: Final = "diagnostics.read"

#: Staged-deployment protected roots (tools/host_control/dist, ORG-052 §7).
STATIC_PROTECTED_ROOTS: Final[tuple[str, ...]] = (
    "/usr/local/libexec",            # broker / approval / executor / helper executables
    "/etc/systemd/system",           # their system units and drop-ins
    "/usr/lib/systemd/system",
    "/lib/systemd/system",
    "/run/systemd/system",
    "/etc/dbus-1",                   # bus ownership and member policy
    "/usr/share/dbus-1",
    "/etc/pantheon",                 # root registries, templates, approver policy, allowlists
    "/etc/sudoers",
    "/etc/sudoers.d",
    "/var/lib/pantheon-host-broker",   # broker journal (StateDirectory)
    "/var/lib/pantheon-host-approval",  # approval journal (StateDirectory)
)


def _package_root(module: object) -> str:
    return str(Path(module.__file__).resolve().parent)  # type: ignore[attr-defined]


def default_protected_roots(extra: Iterable[str | Path] = ()) -> tuple[str, ...]:
    """Static roots + the op-enum packages this broker loaded + ``extra``."""

    roots = [*STATIC_PROTECTED_ROOTS, _package_root(_host_control_pkg), _package_root(_germinate_pkg)]
    roots.extend(str(item) for item in extra)
    return tuple(dict.fromkeys(posixpath.normpath(root) for root in roots))


class SelfAmendmentRefused(PolicyDenied):
    """A plan operation targets a broker self-amendment surface (gate 36).

    Wire body stays the bounded reason code ``family_denied`` (no path leaks).
    """

    def __init__(self, detail: str = "self-amendment surface", *, operation_id: str = "?", target: str = "") -> None:
        # The wire reason is ALWAYS family_denied; ``detail`` stays server-side.
        super().__init__("family_denied")
        self.detail = detail
        self.operation_id = operation_id
        self.target = target


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def refuse_self_amendment(operations: Iterable[object], protected_roots: Iterable[str]) -> None:
    """Refuse when any state-changing operation targets a protected surface.

    A target that is not absolute (a unit or harness name) names no file and is
    out of scope here; an absolute target is normalized first, and one that
    contains ``..`` is refused outright.
    """

    roots = tuple(protected_roots)
    for operation in operations:
        kind = getattr(operation, "kind", "")
        target = getattr(operation, "target", "")
        if EXECUTOR_KIND_CLASSES.get(kind) == READ_ONLY_CLASS:
            continue
        if not isinstance(target, str) or not target.startswith("/"):
            continue
        if ".." in target.split("/"):
            raise SelfAmendmentRefused(operation_id=getattr(operation, "operation_id", "?"), target=target)
        normalized = posixpath.normpath(target)
        if normalized.startswith("//"):
            normalized = "/" + normalized.lstrip("/")
        if any(_within(normalized, root) for root in roots):
            raise SelfAmendmentRefused(operation_id=getattr(operation, "operation_id", "?"), target=normalized)
