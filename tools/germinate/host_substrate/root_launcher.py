"""The root-to-seat launcher: a PURE argv builder behind two literal pins (T-6167 PR-4). DARK.

Root's own systemd manager runs a transient unit as the seat's uid (``systemd-run`` in
the SYSTEM manager with ``User=<account>``): no sudoers entry, no ``sudo -u``, no sysctl
flip, no AppArmor profile (ORG-080 :241; T-3296, the operator TG 12051). Two profiles exist:

* ``seat-run``: the child is ``tools.germinate.seat_child`` from the rev-pinned framework
  copy (``install`` or the mutation-free ``selfcheck``). A light property table; it keeps
  ``/run/user`` visible because the seat's own ``systemctl --user`` needs it.
* ``c5-canary``: the ORG-080 C5 sandbox canaries on a SYSTEM-manager transport, where
  ``TemporaryFileSystem=/:mode=755`` plus the ``BindReadOnlyPaths=`` allowlist is
  expressible (the user manager cannot start it: systemd_run_shape finding 5). The
  property set is ``systemd_run_shape.build_properties(system_scope=True)`` plus a closed
  few more.

DARK BY CONSTRUCTION: ``ROOT_LAUNCHER_LIVE`` is the literal ``False`` and
``LIVE_PROFILES`` maps every profile to the literal ``False``. Both must be ``True`` for a
profile, and ``require_live`` is the FIRST statement of ``build_launch`` and ``launch``, so a
dark launcher touches nothing: no stat, no passwd lookup, no runner call. There is no env
var, flag or argv override. Flipping a pin is a separate reviewed one-line change at the
supervised real-host step (T-3296).

Purity (the host_substrate/ports.py precedent): every command is a FIXED argv list run
through an INJECTED ``CommandRunner``. This module imports no subprocess, reads no
``os.environ`` and has no default runner, stat or passwd seam; the caller (live_run.py)
injects them. The environment the unit gets is a fixed table, never inherited.

Refusals (each a typed ``LauncherRefused.code``): ``live-actuation-disabled``,
``bad-profile``, ``bad-mode``, ``bad-seat-name``, ``scope-mismatch``, ``non-root-caller``,
``framework-path-refused``, ``account-not-in-instance``, ``system-uid-refused``,
``uid-mismatch``, ``root-equivalent-group``, ``group-drift``, ``bad-plan-digest``, ``bad-nonce``,
``bad-gates-dir``.

``-I -B``: ``python3 -I -m tools.x`` does not put the cwd on ``sys.path`` (Python 3.12), so
the child is ``python3 -I -B -c <fixed template>``; the template inserts ONLY the validated
rev-pinned framework root. ``-B`` stops the interpreter writing ``__pycache__`` into the
root-owned tree. The ``c5-*`` profiles set an empty ``SupplementaryGroups=``; whether that
drops the initgroups set is unverified until the supervised run (the selfcheck prints
``groups`` as the proof).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Final, Mapping, Sequence

from tools._organ_lifecycle import CommandRunner
from tools.germinate.components.harness_manager_root import (
    MAX_FLEET_UID,
    MIN_FLEET_UID,
    ROOT_EQUIVALENT_GROUPS,
    Identity,
)
from tools.germinate.planner_sandbox.launcher import systemd_run_shape

#: The launcher-wide pin. A literal; AST-pinned by a test. No override of any kind.
ROOT_LAUNCHER_LIVE: Final = False

#: Per-profile pins. All literal False; both this and the global pin must be True.
LIVE_PROFILES: Final = MappingProxyType({"seat-run": False, "c5-canary": False})

SEAT_RUN: Final = "seat-run"
C5_CANARY: Final = "c5-canary"
PROFILES: Final = (SEAT_RUN, C5_CANARY)

SYSTEMD_RUN: Final = "/usr/bin/systemd-run"
#: Equal to host_broker_install.PYTHON3 (asserted by a test).
PYTHON3: Final = "/usr/bin/python3"

SEAT_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")
FRAMEWORK_BASE: Final = "/usr/local/lib/pantheon/framework"
FRAMEWORK_RE: Final = re.compile(r"^/usr/local/lib/pantheon/framework/[0-9a-f]{12,64}$")
#: Every directory from ``/`` down to the rev: each must be root-owned and not group/other-writable.
FRAMEWORK_ANCESTORS: Final = (
    "/",
    "/usr",
    "/usr/local",
    "/usr/local/lib",
    "/usr/local/lib/pantheon",
    FRAMEWORK_BASE,
)
PLAN_DIGEST_RE: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
NONCE_RE: Final = re.compile(r"^[0-9a-f]{12}$")
GATES_DIR_RE: Final = re.compile(r"^/[A-Za-z0-9._/-]{1,200}$")

SEAT_MODES: Final = ("install", "selfcheck")
CANARY_MODES: Final = ("subprocess-creation", "network", "host-mutation", "authority-holding", "data-only-return")

#: The fixed environment. Nothing is inherited from the caller.
BASE_ENV: Final = (("PATH", "/usr/bin:/bin"), ("LC_ALL", "C.UTF-8"), ("TZ", "UTC"))

#: The seat-run property table (closed). ``User=`` and ``WorkingDirectory=`` are added per seat.
SEAT_RUN_PROPERTIES: Final = (
    "NoNewPrivileges=yes",
    "PrivateTmp=yes",
    "ProtectKernelTunables=yes",
    "ProtectKernelModules=yes",
    "ProtectKernelLogs=yes",
    "ProtectControlGroups=yes",
    "RestrictSUIDSGID=yes",
    "LockPersonality=yes",
    "RuntimeMaxSec=1800",
)

#: The only property NAMES a launch may carry (the closed set a test enforces).
ALLOWED_PROPERTY_NAMES: Final = frozenset(
    {
        "User",
        "WorkingDirectory",
        "SupplementaryGroups",
        "RuntimeDirectory",
        "NoNewPrivileges",
        "PrivateTmp",
        "ProtectKernelTunables",
        "ProtectKernelModules",
        "ProtectKernelLogs",
        "ProtectControlGroups",
        "RestrictSUIDSGID",
        "LockPersonality",
        "RuntimeMaxSec",
        "AmbientCapabilities",
        "MemoryDenyWriteExecute",
        "PrivateNetwork",
        "SystemCallErrorNumber",
        "RestrictNamespaces",
        "PrivateMounts",
        "PrivateUsers",
        "PrivateIPC",
        "ProtectHostname",
        "ProtectClock",
        "ProtectProc",
        "ProcSubset",
        "CapabilityBoundingSet",
        "SystemCallFilter",
        "RestrictAddressFamilies",
        "TemporaryFileSystem",
        "BindReadOnlyPaths",
        "BindPaths",
    }
)

#: The child entry points. Templates are fixed text; only a validated path is interpolated (repr).
SEAT_CHILD_TEMPLATE: Final = (
    "import sys\n"
    "sys.dont_write_bytecode = True\n"
    "FW = {fw}\n"
    "sys.path.insert(0, FW)\n"
    "from tools.germinate.seat_child import main\n"
    "sys.exit(main(sys.argv[1:]))\n"
)

#: Inside the C5 sandbox: prime stdin to an EOF fd (no /dev lookup), then re-exec the real launcher.
CANARY_TEMPLATE: Final = (
    "import os, sys\n"
    "r, w = os.pipe()\n"
    "os.close(w)\n"
    "os.dup2(r, 0)\n"
    "os.execv(sys.executable, [sys.executable, '-SEI', {launcher}, {args}])\n"
)

#: C5-5's planner stand-in component (data only): the root side parses the launcher's JSON.
CANARY_COMPONENT_SOURCE: Final = (
    "def plan_install(request):\n"
    "    return [\n"
    "        {'id': 'noop', 'payload': {}},\n"
    "        {'id': 'totally-unknown-op-id', 'payload': {'danger': True}},\n"
    "    ]\n"
)
CANARY_PLAN_TEMPLATE: Final = (
    "import os, sys\n"
    "r, w = os.pipe()\n"
    "os.close(w)\n"
    "os.dup2(r, 0)\n"
    "p = {scratch} + '/canary_component.py'\n"
    "with open(p, 'w') as f:\n"
    "    f.write({source})\n"
    "os.execv(sys.executable, [sys.executable, '-SEI', {launcher}, 'plan_install', p])\n"
)

LAUNCHER_REL: Final = "tools/germinate/planner_sandbox/launcher/launcher.py"
SECCOMP_REL: Final = "tools/germinate/planner_sandbox/launcher/seccomp"


class LauncherRefused(RuntimeError):
    """A typed refusal. ``code`` is stable; the message is for humans."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class LiveActuationDisabled(LauncherRefused):
    def __init__(self, message: str) -> None:
        super().__init__("live-actuation-disabled", message)


@dataclass(frozen=True)
class StatInfo:
    """One lstat result as the launcher needs it. ``kind``: ``dir``, ``symlink`` or ``other``."""

    kind: str
    uid: int
    mode: int


@dataclass(frozen=True)
class LaunchSpec:
    profile: str
    seat: str
    mode: str
    uid: int
    unit: str
    argv: tuple[str, ...]
    properties: tuple[str, ...]
    env: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class LaunchResult:
    returncode: int
    stdout: str
    stderr: str
    unit: str


def is_live(profile: str) -> bool:
    """True only when the global pin AND this profile's pin are exactly ``True``."""

    return ROOT_LAUNCHER_LIVE is True and LIVE_PROFILES.get(profile) is True


def require_live(profile: str) -> None:
    """Refuse before any I/O while either pin is off. The first statement of every entry point."""

    if not is_live(profile):
        raise LiveActuationDisabled(
            f"the root launcher is dark for profile {profile!r} (T-6167 PR-4 lands dark; "
            "a reviewed flip of the pins at the supervised real-host step, T-3296)"
        )


# --- validators (pure over injected seams) ------------------------------------------


def check_framework_path(path: object, stat: Callable[[str], StatInfo | None]) -> str:
    """The rev-pinned framework root: the exact regex, then every directory from ``/`` to it
    is a real directory (never a symlink), root-owned and not group/other-writable."""

    if not isinstance(path, str) or not FRAMEWORK_RE.fullmatch(path):
        raise LauncherRefused(
            "framework-path-refused", f"{path!r} is not {FRAMEWORK_BASE}/<hex rev> (never 'active', never a checkout)"
        )
    for level in (*FRAMEWORK_ANCESTORS, path):
        info = stat(level)
        if info is None:
            raise LauncherRefused("framework-path-refused", f"{level} is absent")
        if info.kind != "dir":
            raise LauncherRefused("framework-path-refused", f"{level} is a {info.kind}, not a plain directory")
        if info.uid != 0:
            raise LauncherRefused("framework-path-refused", f"{level} is owned by uid {info.uid}, not root")
        if info.mode & 0o022:
            raise LauncherRefused("framework-path-refused", f"{level} is group- or other-writable ({info.mode & 0o7777:04o})")
    return path


def _seat_name(seat: object) -> str:
    if not isinstance(seat, str) or not SEAT_NAME_RE.fullmatch(seat):
        raise LauncherRefused("bad-seat-name", f"{seat!r} does not match {SEAT_NAME_RE.pattern}")
    return seat


def _identity(
    seat: str,
    identities: Mapping[str, Identity],
    getpwnam: Callable[[str], Any],
    live_groups: Callable[[str], tuple[str, frozenset[str]]],
) -> Identity:
    if seat == "root":
        raise LauncherRefused("system-uid-refused", "root is never a seat")
    matches = [i for i in identities.values() if i.account == seat]
    if len(matches) != 1:
        raise LauncherRefused("account-not-in-instance", f"{seat!r} has no single identity block in instance.yaml")
    ident = matches[0]
    if ident.uid == 0 or ident.uid < MIN_FLEET_UID or ident.uid > MAX_FLEET_UID:
        raise LauncherRefused("system-uid-refused", f"{seat!r}: uid {ident.uid} is outside {MIN_FLEET_UID}..{MAX_FLEET_UID}")
    bad = sorted(set(ident.groups) & ROOT_EQUIVALENT_GROUPS)
    if bad:
        raise LauncherRefused("root-equivalent-group", f"{seat!r}: {bad} is root-equivalent; never a seat membership")
    try:
        pw = getpwnam(seat)
    except KeyError:
        raise LauncherRefused("uid-mismatch", f"{seat!r} is not in the passwd database") from None
    if pw.pw_uid != ident.uid:
        raise LauncherRefused("uid-mismatch", f"{seat!r}: passwd uid {pw.pw_uid} != instance.yaml uid {ident.uid}")
    # LIVE membership (Recovery D15 row 5): ``User=`` runs initgroups, so what the unit gets is the account's
    # groups NOW, not what instance.yaml declares. ``live_groups`` is an injected seam returning
    # (primary group name, supplementary group names). instance.yaml ``groups`` are supplementary names
    # only (the primary group is not declared), so drift compares supplementary against declared.
    primary, supplementary = live_groups(seat)
    live_bad = sorted(({primary} | set(supplementary)) & ROOT_EQUIVALENT_GROUPS)
    if live_bad:
        raise LauncherRefused("root-equivalent-group", f"{seat!r}: live groups {live_bad} are root-equivalent")
    if set(supplementary) != set(ident.groups):
        raise LauncherRefused(
            "group-drift",
            f"{seat!r}: live supplementary groups {sorted(supplementary)} != instance.yaml {sorted(ident.groups)}",
        )
    return ident


# --- builders -------------------------------------------------------------------------


def _unit_name(profile: str, seat: str, nonce: str) -> str:
    if not isinstance(nonce, str) or not NONCE_RE.fullmatch(nonce):
        raise LauncherRefused("bad-nonce", f"nonce {nonce!r} is not 12 lowercase hex")
    kind = "seat" if profile == SEAT_RUN else "c5"
    return f"pantheon-germinate-{kind}-{seat}-{nonce}.service"


def _seat_run(
    mode: str, ident: Identity, fw: str, plan_digest: str | None
) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """-> (child argv tail, properties, extra env)"""

    if mode == "install":
        if not isinstance(plan_digest, str) or not PLAN_DIGEST_RE.fullmatch(plan_digest):
            raise LauncherRefused("bad-plan-digest", f"{plan_digest!r} is not sha256:<64 hex>")
        tail = ["install", ident.account, plan_digest]
    else:
        tail = ["selfcheck", ident.account]
    props = [*SEAT_RUN_PROPERTIES, f"User={ident.account}", f"WorkingDirectory={ident.home}"]
    env = [
        ("HOME", ident.home),
        ("USER", ident.account),
        ("LOGNAME", ident.account),
        ("XDG_RUNTIME_DIR", f"/run/user/{ident.uid}"),
    ]
    return tail, props, env


def _c5(mode: str, ident: Identity, fw: str, unit: str, gates_dir: str | None) -> tuple[list[str], list[str], str]:
    scratch_name = unit.removesuffix(".service")
    scratch = f"/run/{scratch_name}"
    launcher = f"{fw}/{LAUNCHER_REL}"
    seccomp = f"{fw}/{SECCOMP_REL}"
    props = systemd_run_shape.build_properties(
        scratch_dir=Path(scratch),
        launcher_path=Path(launcher),
        seccomp_vendor_dir=Path(seccomp),
        system_scope=True,
    )
    props += [
        f"User={ident.account}",
        "SupplementaryGroups=",
        f"RuntimeDirectory={scratch_name}",
        "WorkingDirectory=/",
    ]
    if mode == "data-only-return":
        template = CANARY_PLAN_TEMPLATE.format(
            scratch=repr(scratch), source=repr(CANARY_COMPONENT_SOURCE), launcher=repr(launcher)
        )
    else:
        args = [f"--canary={mode}", scratch]
        if mode == "authority-holding":
            if not isinstance(gates_dir, str) or not GATES_DIR_RE.fullmatch(gates_dir) or ".." in gates_dir.split("/"):
                raise LauncherRefused("bad-gates-dir", f"{gates_dir!r} is not a plain absolute path")
            args.append(gates_dir)
        template = CANARY_TEMPLATE.format(launcher=repr(launcher), args=", ".join(repr(a) for a in args))
    return [PYTHON3, "-SEI", "-c", template], props, scratch


def build_launch(
    profile: str,
    seat: str,
    *,
    scope: str,
    mode: str,
    identities: Mapping[str, Identity],
    framework_root: str,
    stat: Callable[[str], StatInfo | None],
    euid: Callable[[], int],
    getpwnam: Callable[[str], Any],
    live_groups: Callable[[str], tuple[str, frozenset[str]]],
    nonce: str,
    plan_digest: str | None = None,
    gates_dir: str | None = None,
) -> LaunchSpec:
    require_live(profile)  # FIRST: a dark launcher does no I/O at all
    if profile not in PROFILES:
        raise LauncherRefused("bad-profile", f"{profile!r} is not one of {list(PROFILES)}")
    allowed_modes = SEAT_MODES if profile == SEAT_RUN else CANARY_MODES
    if mode not in allowed_modes:
        raise LauncherRefused("bad-mode", f"{mode!r} is not one of {list(allowed_modes)} for {profile}")
    seat = _seat_name(seat)
    if scope != f"seat-{seat}":
        raise LauncherRefused("scope-mismatch", f"scope {scope!r} is not seat-{seat}; a launch serves one seat's scope")
    if euid() != 0:
        raise LauncherRefused("non-root-caller", "the root launcher runs only as root")
    fw = check_framework_path(framework_root, stat)
    ident = _identity(seat, identities, getpwnam, live_groups)
    unit = _unit_name(profile, seat, nonce)

    env = list(BASE_ENV)
    if profile == SEAT_RUN:
        tail, props, extra_env = _seat_run(mode, ident, fw, plan_digest)
        env += extra_env
        template = SEAT_CHILD_TEMPLATE.format(fw=repr(fw))
        child = [PYTHON3, "-I", "-B", "-c", template, *tail]
    else:
        child, props, _scratch = _c5(mode, ident, fw, unit, gates_dir)

    argv = [
        SYSTEMD_RUN,
        "--wait",
        "--pipe",
        "--collect",
        "--quiet",
        f"--unit={unit}",
        *(f"--property={p}" for p in props),
        *(f"--setenv={k}={v}" for k, v in env),
        "--",
        *child,
    ]
    return LaunchSpec(profile, seat, mode, ident.uid, unit, tuple(argv), tuple(props), tuple(env))


def launch(spec: LaunchSpec, runner: CommandRunner) -> LaunchResult:
    """Run one built spec through the injected runner. Refuses while dark, before the runner."""

    require_live(spec.profile)
    result = runner.run(list(spec.argv))
    return LaunchResult(result.returncode, result.stdout, result.stderr, spec.unit)


def argv_table() -> Sequence[str]:
    """Every argv SHAPE this module can emit, for Recovery's D15 review (a documentary table)."""

    return (
        f"{SYSTEMD_RUN} --wait --pipe --collect --quiet --unit=pantheon-germinate-seat-<seat>-<nonce>.service "
        f"--property=<SEAT_RUN_PROPERTIES|User|WorkingDirectory> --setenv=<BASE_ENV|HOME|USER|LOGNAME|XDG_RUNTIME_DIR> "
        f"-- {PYTHON3} -I -B -c <SEAT_CHILD_TEMPLATE> (install <seat> <sha256:digest> | selfcheck <seat>)",
        f"{SYSTEMD_RUN} --wait --pipe --collect --quiet --unit=pantheon-germinate-c5-<seat>-<nonce>.service "
        f"--property=<build_properties(system_scope=True)|User|SupplementaryGroups=|RuntimeDirectory|WorkingDirectory=/> "
        f"--setenv=<BASE_ENV> -- {PYTHON3} -SEI -c <CANARY_TEMPLATE|CANARY_PLAN_TEMPLATE>",
    )
