"""Substrate ports: the simulated one (wired) and the DARK host one (T-5084, GE).

``SimulatedSubstratePort`` models host substrate as a subject -> value map (see
``families.resolve`` for the subjects) plus modelled ``visudo -cf`` verdicts.

``HostSubstratePort`` is the real-host shape, and it is DARK BY CONSTRUCTION:
``LIVE_ACTUATION_ENABLED`` is the literal ``False`` (no env var, no flag, no
override), and the constructor refuses while it is. Live activation is a
separate, reviewed change after S3 is decided and C5 holds on prod (HO ruling
2026-10-17; T-3296/T-5106). Every command is a FIXED argv list run through an
INJECTED ``CommandRunner`` (tools/_organ_lifecycle.py): no shell, no string-built
command, no default runner, and this module imports no subprocess.

CURRENT (co-author recovery; D15 approved at 71fd484cc): the argv below is Recovery-approved.
"""

from __future__ import annotations

import copy
from typing import Any, Final, Mapping, Sequence

from tools._organ_lifecycle import CommandRunner

from .families import Resolved

LIVE_ACTUATION_ENABLED: Final = False

#: Same fixed binaries as tools/codex_recovery.py (asserted equal by a test).
LOGINCTL: Final = "/usr/bin/loginctl"
SYSTEMCTL: Final = "/usr/bin/systemctl"

GROUPADD: Final = "/usr/sbin/groupadd"
USERADD: Final = "/usr/sbin/useradd"
USERMOD: Final = "/usr/sbin/usermod"
USERDEL: Final = "/usr/sbin/userdel"
GROUPDEL: Final = "/usr/sbin/groupdel"
GPASSWD: Final = "/usr/bin/gpasswd"
SETFACL: Final = "/usr/bin/setfacl"
CHOWN: Final = "/usr/bin/chown"
CHMOD: Final = "/usr/bin/chmod"
INSTALL: Final = "/usr/bin/install"
UNLINK: Final = "/usr/bin/unlink"
MV: Final = "/usr/bin/mv"
VISUDO: Final = "/usr/sbin/visudo"
SUDO: Final = "/usr/bin/sudo"


class SimulatedSubstratePort:
    """Host substrate as data. ``visudo`` maps a fragment sha256 to a modelled
    verdict (``ok`` by default; tests set ``warning`` / ``error``)."""

    def __init__(self, state: Mapping[str, Any] | None = None):
        self.state: dict[str, Any] = {"subjects": {}, "visudo": {}}
        if state:
            self.load(state)

    def get(self, subject: str) -> Any:
        return copy.deepcopy(self.state["subjects"].get(subject))

    def put(self, subject: str, value: Any) -> None:
        if value is None:
            self.state["subjects"].pop(subject, None)
        else:
            self.state["subjects"][subject] = copy.deepcopy(value)

    def visudo_check(self, sha256: str) -> str:
        return self.state["visudo"].get(sha256, "ok")

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self.state["subjects"])

    def dump(self) -> dict[str, Any]:
        return copy.deepcopy(self.state)

    def load(self, state: Mapping[str, Any]) -> None:
        self.state = {"subjects": dict(copy.deepcopy(state.get("subjects") or {})), "visudo": dict(state.get("visudo") or {})}


class LiveActuationDisabled(RuntimeError):
    """The host port is dark in this tranche (ORG-080 A-8 note, T-5081 GO condition)."""


def _exact_mode(mode: str) -> str:
    """A 5-digit numeric mode for chmod (T-5513). GNU chmod PRESERVES a directory's setuid/
    setgid bits for a 4-digit numeric mode (2770 -> `chmod 0000` -> 2000, so the helper's
    verify sees drift); a fifth leading zero makes the mode exact. `install -m` calls chmod(2)
    directly, so mailbox_dir.ensure needs no change."""
    return mode.zfill(5)


def apply_argv(r: Resolved, *, staged_source: str | None = None) -> list[list[str]]:
    """The fixed argv that converges ``r.subject`` to ``r.after`` on a real host.

    ``staged_source`` is a root-owned staging copy whose hash the caller already
    checked against the manifest pin (unit / D-Bus policy / sudoers bytes).
    """

    f, e, name = r.family, r.entry, r.subject.split(":", 1)[1]
    if f == "group.ensure":
        return [[GROUPADD, "--gid", str(e["gid"]), name]]
    if f == "account.ensure":
        uid = str(e["uid"])
        return [[GROUPADD, "--gid", uid, name],
                [USERADD, "--uid", uid, "--gid", uid, "--home-dir", e["home"], "--create-home", "--shell", e["shell"], name]]
    if f == "account.retire":
        return [[USERMOD, "--lock", "--expiredate", "1", name]]
    if f == "group.member":
        group, acct = name.split(":", 1)
        return [[GPASSWD, "--add" if r.after else "--delete", acct, group]]
    if f == "acl.converge":
        if not r.after["entries"]:  # `setfacl --set ""` is malformed; the planner never emits this
            raise ValueError("acl.converge: empty entry list (nothing to converge)")
        return [[SETFACL, "--set", ",".join(r.after["entries"]), name]]
    if f == "owner_mode.set":
        return [[CHOWN, f"{r.after['owner']}:{r.after['group']}", name], [CHMOD, _exact_mode(r.after["mode"]), name]]
    if f == "mailbox_dir.ensure":
        return [[INSTALL, "-d", "-o", r.after["owner"], "-g", r.after["group"], "-m", r.after["mode"], name]]
    if f == "mailbox_dir.retire":
        return [[CHMOD, _exact_mode("0000"), name]]
    if f == "linger.enable":
        return [[LOGINCTL, "enable-linger", name]]
    if f == "linger.disable":
        return [[LOGINCTL, "disable-linger", name]]
    if f == "unit.system.enable":
        return [[SYSTEMCTL, "enable" if r.after else "disable", name]]
    if f in ("unit.system.install", "dbus_policy.install", "dbus_policy.retire"):
        reload = [SYSTEMCTL, "daemon-reload"] if f == "unit.system.install" else [SYSTEMCTL, "reload", "dbus.service"]
        if r.after is None:
            return [[UNLINK, name], reload]
        if staged_source is None:
            raise ValueError(f"{f}: needs a staged, hash-checked source")
        return [[INSTALL, "-o", "root", "-g", "root", "-m", "0644", staged_source, name], reload]
    if f == "sudoers_fragment.ensure":
        if staged_source is None:
            raise ValueError("sudoers_fragment.ensure: needs a staged, hash-checked source")
        tmp = name + ".pantheon-tmp"  # a '.' name: sudoers.d never reads it
        return [[INSTALL, "-o", "root", "-g", "root", "-m", "0440", staged_source, tmp],
                [VISUDO, "-cf", tmp],
                [MV, "-f", "--", tmp, name],
                [VISUDO, "-c"],
                [SUDO, "-l", "-U", e["grantee"]]]
    if f == "sudoers_fragment.retire":
        return [[UNLINK, name], [VISUDO, "-c"]]
    raise ValueError(f"no argv for {f!r}")  # unreachable: families are closed


def rollback_argv(r: Resolved, before: Any) -> list[list[str]]:
    """Restore argv for an account this run CREATED: remove the passwd entry, never
    the home (no ``--remove``; gate 34). A retire is undone by unlocking: re-applying its
    preimage would resolve as account.ensure and run groupadd/useradd on an existing
    account (T-5510 F3). Other families restore by re-applying the preimage as a desired
    value (``apply_argv`` over a preimage-shaped Resolved)."""

    name = r.subject.split(":", 1)[1]
    if r.family == "account.ensure" and before is None:
        return [[USERDEL, name], [GROUPDEL, name]]
    if r.family == "account.retire":
        if before and before.get("locked"):
            return []  # already retired before this run: nothing to undo
        # Inverse of `usermod --lock --expiredate 1`. The model carries no prior expiry,
        # and fleet accounts have none, so clearing it restores the preimage.
        return [[USERMOD, "--unlock", "--expiredate", "", name]]
    raise ValueError(f"{r.family}: restore by re-applying the preimage")


class HostSubstratePort:
    """The real-host port. DARK: refuses to construct in this tranche."""

    def __init__(self, runner: CommandRunner):
        if LIVE_ACTUATION_ENABLED is not True:
            raise LiveActuationDisabled(
                "host-substrate live actuation is disabled in this tranche (T-5084 lands dark; "
                "a separate reviewed go after S3 + C5-on-prod, T-3296/T-5106)"
            )
        if runner is None:  # pragma: no cover - unreachable while dark
            raise LiveActuationDisabled("an explicit CommandRunner is required")
        self._runner = runner  # pragma: no cover - unreachable while dark

    def run(self, argvs: Sequence[Sequence[str]]) -> None:  # pragma: no cover - unreachable while dark
        for argv in argvs:
            result = self._runner.run(list(argv))
            if result.returncode != 0:
                raise RuntimeError(f"{argv[0]} exited {result.returncode}")
