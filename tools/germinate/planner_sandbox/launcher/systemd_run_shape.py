"""Enforced-properties builder for the C5 sandbox launch profile.

Builds the ``--property=`` list ``harness.sandbox_runner.run_in_sandbox()``
takes as its ``properties`` kwarg (per T-3295 brief). Follows
``adr/ORG-080-germination-engine.md`` Component contract (L103-177) with
empirically-required deviations from the documentary text, each logged
below and in this arc's DECISIONS (T-3295 Phase 1+2, host: Ubuntu 24.04
noble, kernel 6.8.0, systemd 255, x86_64, KVM guest).

**Deviation 1 -- multi-value properties must be one ``--property=`` flag
per value, never space-joined.** Empirically verified: passing
``SystemCallFilter=~@process ~@privileged ~@resources ...`` as ONE
space-joined value silently drops every group but the first (confirmed via
``journalctl --user``: ``System call ~@privileged is not known,
ignoring.`` for 11 of 12 groups) while the unit still reports a clean
start -- exactly the "silently and gracefully turned off" failure mode
``systemd.exec(5)`` warns about. Splitting into one flag per value fixes
it. Applied uniformly here to ``SystemCallFilter=``, ``BindReadOnlyPaths=``,
and ``RestrictAddressFamilies=`` (the last tested fine space-joined, but
one-per-value is applied anyway for consistency/robustness).

**Deviation 2 -- ``RestrictAddressFamilies=none`` is REJECTED** via the
transient/D-Bus property-setting path (``Failed to set unit properties:
Invalid argument``) -- the bareword ``none`` only parses in unit-*file*
text, not ``--property=``. Use the deny-list form instead (every AF_* name
Python's own ``socket`` module registers on this kernel, minus
``AF_UNSPEC``/``AF_ROUTE`` which alias other entries).

**Deviation 3 -- ``CapabilityBoundingSet=`` (empty) is OMITTED for
user-scope.** Empirically fails to start (``EXIT_CAPABILITIES``/218,
journal: ``Failed to drop capabilities: Operation not permitted``) --
shrinking a process's own bounding set needs ``CAP_SETPCAP``, which an
unprivileged ``--user`` systemd manager instance doesn't have on this host.
PO-approved (T-3295 Phase 1 open question 1, ADR §Architectural-tier scope
carve authority): mirrors the ADR's own existing ``ProtectControlGroups=``
user-scope carve-out shape. Privilege containment for user-scope instead
relies on ``NoNewPrivileges=yes`` + ``AmbientCapabilities=`` (empty,
confirmed working) + the seccomp/namespace layers.

**Deviation 4 -- ``PrivateDevices=yes`` is OMITTED for user-scope.**
Empirically fails to start the same way ``CapabilityBoundingSet=`` does
(``EXIT_CAPABILITIES``/218) -- setting up a minimal private ``/dev`` needs
capability operations (device-node creation) an unprivileged ``--user``
manager instance doesn't have on this host either. Not currently load-
bearing for this tranche's 5 canaries (none needs ``/dev`` node access
inside the sandbox; stdin for the ``plan_<leg>`` path is handled via an
already-open, EOF-primed fd rather than a `/dev/null` path lookup -- see
``tests/test_launcher_c5.py``). Same reasoning shape as deviation 3
(mechanism substitute via namespace/seccomp layers, outcome unaffected)
but NOT yet separately PO-ratified -- surfaced as an ADR-amendment
candidate for the PO's synthesis rather than assumed pre-approved.

**Finding 5 (major -- ADR-amendment candidate) -- ``TemporaryFileSystem=``
itself is unavailable on this host, independent of ``PrivateUsers=``/
``PrivateMounts=``.** Empirically verified: ``TemporaryFileSystem=/tmp:
mode=755`` alone (no other namespace properties) fails unit start with
``code=exited, status=226/NAMESPACE``, journal: "Failed to set up mount
namespacing: Operation not supported" (ENOTSUP, not EPERM -- a kernel/
mount-subsystem capability gap, not a privilege gap). Isolated the cause:
plain ``BindReadOnlyPaths=``/``BindPaths=`` (bind-mounts over the existing
tree, no tmpfs), ``ProtectSystem=strict``, ``ProtectHome=yes``,
``ReadOnlyPaths=``, and ``InaccessiblePaths=`` all start cleanly on this
host; only the ``TemporaryFileSystem=`` directive (tmpfs-mount-then-
bind-over) fails, on this KVM-guest host regardless of target path (tested
both ``/`` and ``/tmp``).

This is NOT treated as license to swap the launcher's canonical
filesystem-confinement mechanism from default-deny
(``TemporaryFileSystem=/`` + allowlisted ``BindReadOnlyPaths=``) to
default-allow-with-denylist (``ProtectSystem=strict`` +
``InaccessiblePaths=`` for specific paths) -- the latter IS a viable
substitute for THIS ref-impl's specific, closed-set test targets, but
shipping it as ``systemd_run_shape.py``'s canonical property list would
silently change the PRODUCTION launcher's security posture to one that
cannot hide an open-ended set of paths it doesn't know in advance (exactly
the property default-deny exists to give, and exactly why the ADR chose
it). That's an architecture-level call, not a test-plumbing one -- left to
the PO's synthesis rather than decided unilaterally mid-implementation.
``build_properties()`` therefore keeps ``TemporaryFileSystem=`` + allowlist
as the canonical, documentary-faithful mechanism; ``probe_temporary_
filesystem_support()`` below lets callers typed-skip the specific tests
that need it on hosts (like this one) where it doesn't work, mirroring the
existing ``require_systemd_user``/nsjail typed-skip convention rather than
inventing a new one.

**Note (not a deviation, but worth recording):** ``PrivateNetwork=yes`` and
``PrivateIPC=yes`` both silently degrade on this host ("kernel does not
support or we lack privileges for network namespace, proceeding without" /
"IPC namespace setup failed, ignoring: Operation not permitted"),
consistent with Ubuntu 24.04's default ``apparmor_restrict_unprivileged_
userns=1``. The seccomp-level ``RestrictAddressFamilies`` filter still
enforces network denial independently of namespace isolation, so C5's
*outcome* holds; only the ADR's own mechanism attribution ("blocked by
PrivateNetwork + address-family filter") is half-true on this class of
host. Kept in the profile (harmless when it silently no-ops; effective on
hosts where unprivileged netns/ipcns creation IS permitted).
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from collections.abc import Iterable, Sequence

#: Non-path-dependent properties (system-scope AND user-scope both).
_BASE_PROPERTIES: tuple[str, ...] = (
    "NoNewPrivileges=yes",
    "AmbientCapabilities=",
    "RestrictSUIDSGID=yes",
    "LockPersonality=yes",
    "MemoryDenyWriteExecute=yes",
    "PrivateNetwork=yes",
    "SystemCallErrorNumber=EPERM",
    "RestrictNamespaces=yes",
    "PrivateTmp=yes",
    "PrivateMounts=yes",
    "PrivateUsers=yes",
    "PrivateIPC=yes",
    "RuntimeMaxSec=10",
)

#: Kernel-surface protection -- system-scope only (ADR: user-service path
#: drops ProtectControlGroups=; kept here as the FULL system-scope set for
#: documentary completeness even though this ref-impl only exercises
#: user-scope).
_SYSTEM_SCOPE_ONLY_PROPERTIES: tuple[str, ...] = (
    "ProtectKernelTunables=yes",
    "ProtectKernelModules=yes",
    "ProtectKernelLogs=yes",
    "ProtectHostname=yes",
    "ProtectClock=yes",
    "ProtectProc=invisible",
    "ProcSubset=pid",
    "ProtectControlGroups=yes",
    "CapabilityBoundingSet=",
)

#: SystemCallFilter groups -- one property per group (deviation 1).
_SYSCALL_FILTER_GROUPS: tuple[str, ...] = (
    "~@process",
    "~@privileged",
    "~@resources",
    "~@mount",
    "~@debug",
    "~@reboot",
    "~@raw-io",
    "~@swap",
    "~@cpu-emulation",
    "~@clock",
    "~@module",
    "~@obsolete",
)


def _deny_all_address_families() -> tuple[str, ...]:
    """One ``RestrictAddressFamilies=~AF_X`` per family Python's own
    ``socket`` module registers on this kernel (deviation 1 + 2).
    ``AF_UNSPEC`` (not a real family) and ``AF_ROUTE`` (Linux alias for
    AF_NETLINK) are excluded. TODO(T-3295 open question 4): cross-check
    against systemd 255's own af-list.c rather than Python's registry.
    """

    names = sorted(n for n in dir(socket) if n.startswith("AF_"))
    excluded = {"AF_UNSPEC", "AF_ROUTE"}
    return tuple(f"RestrictAddressFamilies=~{n}" for n in names if n not in excluded)


def _stdlib_bind_read_only_paths() -> list[str]:
    """The Python-interpreter + shared-library closure, per ADR L119.

    Derives the python3.<minor> path from the running interpreter rather
    than hardcoding "3.12" -- this host happens to run 3.12, but the
    launcher shouldn't assume that.
    """

    major, minor = sys.version_info.major, sys.version_info.minor
    return [
        "/usr/bin/python3",
        f"/usr/lib/python{major}.{minor}",
        "/lib64/ld-linux-x86-64.so.2",
        "/lib/x86_64-linux-gnu",
        "/usr/lib/x86_64-linux-gnu",
        "/etc/ld.so.conf.d",
    ]


def build_properties(
    *,
    scratch_dir: Path,
    launcher_path: Path,
    seccomp_vendor_dir: Path,
    component_module_dir: Path | None = None,
    system_scope: bool = False,
    include_filesystem_root: bool = True,
) -> list[str]:
    """Build the full ``--property=`` list for one sandboxed invocation.

    ``launcher_path`` and ``seccomp_vendor_dir`` are bind-mounted at their
    real repo-checkout paths (NOT ``/opt/pantheon/germinate/...`` -- that's
    the production bootstrap-install path; ``germinate install
    --bootstrap`` doesn't exist in this ref-impl. Documentary-vs-mechanism
    divergence, explicitly licensed by the ADR's own scope-carve text).

    ``include_filesystem_root=False`` omits ``TemporaryFileSystem=`` +
    the ``BindReadOnlyPaths=``/``BindPaths=`` allowlist entirely (finding 5
    above -- ``TemporaryFileSystem=`` fails to start on this host class).
    Process/network confinement (``SystemCallFilter=``,
    ``RestrictAddressFamilies=``, capability/namespace properties) is
    unaffected either way -- callers proving ONLY those outcomes (e.g. C5
    canaries 1-2) don't need the filesystem-visibility-root primitive and
    shouldn't have their unit fail to start over an unrelated, unavailable
    mechanism. Callers that DO need it (canaries 3-5) should probe first
    via ``probe_temporary_filesystem_support()`` and typed-skip if absent,
    rather than pass ``include_filesystem_root=True`` blind.
    """

    props: list[str] = list(_BASE_PROPERTIES)
    if system_scope:
        props.extend(_SYSTEM_SCOPE_ONLY_PROPERTIES)
    # else: CapabilityBoundingSet= + the Protect*/ProcSubset= set are
    # omitted for user-scope -- deviation 3 (CapabilityBoundingSet=) plus
    # the ADR's own documented ProtectControlGroups=/kernel-surface carve.

    props.extend(f"SystemCallFilter={g}" for g in _SYSCALL_FILTER_GROUPS)
    props.extend(_deny_all_address_families())

    if include_filesystem_root:
        props.append("TemporaryFileSystem=/:mode=755")
        bind_ro = _stdlib_bind_read_only_paths()
        bind_ro.append(str(launcher_path))
        bind_ro.append(str(seccomp_vendor_dir))
        if component_module_dir is not None:
            bind_ro.append(str(component_module_dir))
        props.extend(f"BindReadOnlyPaths={p}" for p in bind_ro)
        props.append(f"BindPaths={scratch_dir}")

    return props


class PrimitiveUnavailable(Exception):
    """Typed refusal when a systemd-run primitive this module probes for
    isn't usable on the current host (C4 fail-closed).
    """

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        self.reason = reason
        super().__init__(f"{name} unavailable: {reason}")


def probe_temporary_filesystem_support(*, scratch_dir: Path, timeout: float = 10.0) -> None:
    """Raises PrimitiveUnavailable unless ``TemporaryFileSystem=`` can
    actually start a unit on this host (finding 5 above).

    A throwaway probe unit with the minimal properties needed to isolate
    the mechanism (no other namespace/capability properties, to avoid
    conflating this probe's own failure modes with an unrelated one).
    """

    from tools.germinate.planner_sandbox.harness.sandbox_runner import run_in_sandbox
    from tools.germinate.planner_sandbox.harness.exceptions import HarnessUnavailable

    try:
        result = run_in_sandbox(
            "print('tfs-probe-ok')",
            scratch_dir=scratch_dir,
            properties=["TemporaryFileSystem=/tmp:mode=755", f"BindPaths={scratch_dir}"],
            timeout=timeout,
        )
    except HarnessUnavailable as exc:
        raise PrimitiveUnavailable(
            "temporary-filesystem", f"probe could not run: {exc}"
        ) from exc

    if result.returncode != 0:
        raise PrimitiveUnavailable(
            "temporary-filesystem",
            f"probe unit failed to start (rc={result.returncode}); "
            f"stderr={result.stderr.strip()!r}",
        )
