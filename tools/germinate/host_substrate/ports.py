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

PROPOSED (T-2920 U3b; awaiting Recovery D15 review): ``SubstratePort`` (the typed
protocol RootHelper drives), the READ-ONLY observe layer (``observe_argv`` /
``parse_observation`` / ``observe``; every argv it can emit is listed in
``OBSERVE_ARGV_TABLE``) and ``put_argv``, which only ever returns
``apply_argv`` / ``rollback_argv`` output. ``apply_argv`` and ``rollback_argv``
themselves are unchanged.

RULED (Recovery, T-5652): ONE failure-path argv, ``sudoers_tmp_cleanup_argv``
(``unlink <fragment>.pantheon-tmp`` when ``visudo -cf`` or ``mv`` fails before
the mv lands), listed in ``OBSERVE_ARGV_TABLE`` as mutating.

RULED (Recovery, T-5653 D15): the symlink guard. ``acl.converge`` runs
``setfacl --physical --set`` and ``owner_mode.set`` runs ``chown -h``; those are the
ONLY two argv changes. ``path_type`` (the row-4 stat, no new argv) types a subject so
``RootHelper`` refuses a symlink before any path-taking actuation.

RULED (Recovery, T-5657 D15): the ancestor walk. ``path_stat`` runs the SAME row-4 stat on
every ancestor of a path subject; no new argv template. The one new operand value is
``/``, accepted only by the walk-only ``_ancestor_path`` (the subject validator ``_path``
still refuses it).

RULED (Recovery, T-2920 U4b D15): the mixin refactor. ``HostSubstratePort``'s method bodies
moved VERBATIM into the ``__init__``-less ``_LiveSubstrateOps``; ``HostSubstratePort``'s
constructor and dark gate are byte-identical. The one other subclass is
``RehearsalSubstratePort`` (tools/host_broker_rehearsal_live.py), gated by an nspawn
attestation instead. No argv changed.
"""

from __future__ import annotations

import copy
import posixpath
import re
from typing import Any, Final, Mapping, Protocol, Sequence

from tools._organ_lifecycle import CommandRunner

from . import families as F
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

#: Read-only observation binaries (T-2920 U3b). Same paths as the T-2918
#: sandbox port's constants (asserted equal by a test).
GETENT: Final = "/usr/bin/getent"
STAT: Final = "/usr/bin/stat"
SHA256SUM: Final = "/usr/bin/sha256sum"
GETFACL: Final = "/usr/bin/getfacl"

#: ``helper.STAGED_PREFIX`` restated (helper imports this module; a test asserts equality).
STAGED_PREFIX: Final = "staged:"


class SubstratePort(Protocol):
    """What ``RootHelper`` calls on a port (helper.py: ``snapshot`` in preflight,
    ``get``, ``put``, ``visudo_check``). The simulated, sandbox and host ports all
    satisfy it structurally; nothing checks it at run time."""

    def get(self, subject: str) -> Any: ...

    def put(self, subject: str, value: Any) -> None: ...

    def visudo_check(self, sha256: str) -> str: ...

    def snapshot(self) -> Mapping[str, Any]: ...

    def path_type(self, path: str) -> str | None:
        """T-5653: the subject path's own file type as stat ``%F`` text (``symbolic
        link`` included, never followed), or None when the path is absent."""
        ...

    def path_stat(self, path: str) -> tuple[str, str, str, str] | None:
        """T-5657: an ANCESTOR's (``%F``, owner, group, 4-digit mode) from the same row-4
        stat (no -L), or None when absent. ``path`` may be exactly ``/`` (walk-only)."""
        ...


#: T-5657: what the simulated tier models for an ancestor nothing else describes: a
#: correctly installed host's root-owned, non-group/other-writable directory.
SIMULATED_TRUSTED_DIR: Final = ("directory", "root", "root", "0755")


class SimulatedSubstratePort:
    """Host substrate as data. ``visudo`` maps a fragment sha256 to a modelled
    verdict (``ok`` by default; tests set ``warning`` / ``error``). ``path_types``
    (T-5653) maps a path to its modelled stat ``%F`` type; an unlisted path has no
    special type (None). It sits beside the subjects, never inside a subject value,
    so verify's exact comparison and every existing value shape are unchanged.
    ``path_stats`` (T-5657) overrides an ancestor's modelled (%F, owner, group, mode)."""

    def __init__(self, state: Mapping[str, Any] | None = None):
        self.state: dict[str, Any] = {"subjects": {}, "visudo": {}, "path_types": {}, "path_stats": {}}
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

    def path_type(self, path: str) -> str | None:
        return self.state["path_types"].get(path)

    def path_stat(self, path: str) -> tuple[str, str, str, str] | None:
        """T-5657: an explicit ``path_stats`` override wins (None models absent); else an
        ``owner_mode:`` / ``dir:`` subject value at that path, as a directory, so a run that
        converges an ancestor is seen by the walk; else ``SIMULATED_TRUSTED_DIR``. A
        ``path_types`` entry other than ``directory`` (review LOW-3: e.g. an ancestor
        modelled as a symlink) wins over the derived and default type, so ``path_type`` and
        ``path_stat`` never disagree about what a path is."""

        stats = self.state["path_stats"]
        if path in stats:
            value = stats[path]
            return None if value is None else tuple(value)  # type: ignore[return-value]
        out: tuple[str, str, str, str] = SIMULATED_TRUSTED_DIR
        for kind in ("owner_mode", "dir"):
            v = self.state["subjects"].get(f"{kind}:{path}")
            if isinstance(v, dict):
                out = ("directory", v["owner"], v["group"], v["mode"])
                break
        declared = self.state["path_types"].get(path)
        if declared is not None and declared != "directory":
            out = (declared, *out[1:])  # type: ignore[assignment]
        return out

    def snapshot(self) -> dict[str, Any]:
        # Shape divergence (T-2920 U3b review): this returns every subject, so ``passwd:*``
        # carries ``locked``; HostSubstratePort.snapshot does NOT (no shadow enumeration,
        # Recovery D15). RootHelper.preflight reads only uid/gid/home/shell, so both serve it;
        # invariants.evaluate over a HOST snapshot would see HS1 drift (``after`` has locked).
        return copy.deepcopy(self.state["subjects"])

    def dump(self) -> dict[str, Any]:
        return copy.deepcopy(self.state)

    def load(self, state: Mapping[str, Any]) -> None:
        self.state = {"subjects": dict(copy.deepcopy(state.get("subjects") or {})), "visudo": dict(state.get("visudo") or {}),
                      "path_types": dict(state.get("path_types") or {}),
                      "path_stats": dict(copy.deepcopy(state.get("path_stats") or {}))}


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
        # T-5653 (Recovery, D15, APPROVED): --physical, so setfacl never follows a symlink
        # argument; this closes the race between the preflight stat and setfacl in the
        # sense that root never ACLs a swapped-in symlink's TARGET. Note: on a symlink
        # argument setfacl --physical returns rc 0 and skips silently (verified on the real
        # binary), so apply itself reports success; verify catches it, because getfacl
        # --physical on a link prints nothing and the ACL parser refuses empty output.
        return [[SETFACL, "--physical", "--set", ",".join(r.after["entries"]), name]]
    # RESIDUAL TOCTOU (T-5653, recorded per Recovery's ruling): RootHelper's preflight types the
    # subject with the row-4 stat (no -L) and refuses a symlink, but chmod has no no-follow
    # mode on Linux and `install -d` has none either. A principal that can write the parent
    # directory can swap the subject for a symlink BETWEEN that stat and owner_mode.set's
    # chmod, mailbox_dir.retire's chmod or mailbox_dir.ensure's install -d, and root then
    # follows it. Likewise neither the stat (no -L) nor setfacl --physical / chown -h stops
    # an INTERMEDIATE path component that is a symlink from being followed: only the final
    # component is guarded. Precondition: installer roots (and every directory above a
    # subject) are never writable by untrusted principals. That foothold stays in scope for
    # review (ORG-105 chain: writer in an installer root -> planted symlink -> root acts on
    # the target).
    # T-5657 (Recovery, D15) makes that precondition a CHECKED invariant: RootHelper walks every
    # ancestor with the same row-4 stat and refuses unless each is a real directory, owned
    # by root and not group/other-writable. ACCEPTED RESIDUAL (Recovery D15): the leaf's parent
    # alone may instead be owned by the subject's designated owner D (a declared, non-root
    # manifest account; still not g/o-writable), so D can still swap the subject under root's
    # chmod / install -d on that one directory.
    if f == "owner_mode.set":
        # chown -h (--no-dereference) is the belt; the preflight is chmod's only guard.
        return [[CHOWN, "-h", f"{r.after['owner']}:{r.after['group']}", name], [CHMOD, _exact_mode(r.after["mode"]), name]]
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


#: The sudoers.d temp-fragment suffix (a '.' name: sudoers.d never reads it).
SUDOERS_TMP_SUFFIX: Final = ".pantheon-tmp"


def sudoers_tmp_cleanup_argv(r: Resolved) -> list[list[str]]:
    """T-5652 (Recovery ruling, D15): the ONE failure-path cleanup for sudoers_fragment.ensure.

    Runs when the install landed the temp fragment but a later step failed BEFORE the
    mv landed (``visudo -cf`` or ``mv`` failed). The target is exactly the fragment's
    manifest path plus ``SUDOERS_TMP_SUFFIX``: derived here, never caller-supplied,
    never a glob. ENOENT counts as success (``cleanup_ok``)."""

    if r.family != "sudoers_fragment.ensure":
        raise ValueError(f"{r.family}: only sudoers_fragment.ensure has a temp-fragment cleanup")
    name = r.subject.split(":", 1)[1]
    tmp = name + SUDOERS_TMP_SUFFIX
    if not name.startswith(F.SUDOERS_DIR) or "/" in name[len(F.SUDOERS_DIR):] or set("*?[]\\") & set(tmp):
        raise ValueError(f"{tmp!r}: not a literal sudoers.d temp path")
    if apply_argv(r, staged_source="/x")[0][-1] != tmp:  # pragma: no cover - the same tmp the install writes
        raise ValueError(f"{tmp!r} is not the temp path apply_argv installs")
    return [[UNLINK, tmp]]


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


# ---------------------------------------------------------------------------
# Real-host OBSERVE layer (T-2920 U3b; PROPOSED, awaiting Recovery D15 review)
#
# READ-ONLY. ``observe_argv`` / ``snapshot_argv`` / ``visudo_argv`` build fixed
# argv lists from a shape-checked subject; ``parse_*`` turn the outputs into the
# SAME value shapes ``SimulatedSubstratePort`` holds, and raise
# ``ObservationError`` on anything they do not recognise (fail closed, never a
# guess). Absence of a path is read ONLY from the C-locale ENOENT text, so a
# runner in another locale fails closed instead of reading "present" as
# "absent"; the operator runner must run with LC_ALL=C.
# ---------------------------------------------------------------------------


class ObservationError(RuntimeError):
    """Host output could not be parsed into a subject value (fail closed)."""


class ArgvGap(RuntimeError):
    """No Recovery-approved argv converges this subject to this value (T-5510 class)."""


class _Result(Protocol):
    """The shape of ``tools._organ_lifecycle.CommandResult`` (not imported: this
    module imports only ``CommandRunner`` from there)."""

    returncode: int
    stdout: str
    stderr: str


LINGER_DIR: Final = "/var/lib/systemd/linger/"
STAT_FORMAT: Final = "--format=%F:%U:%G:%a"
#: ``--physical``: getfacl FOLLOWS a symlink argument without it; with it a symlink
#: argument prints nothing (rc 0), which the parser refuses (review MED, T-2920 U3b).
GETFACL_FLAGS: Final = ("--omit-header", "--absolute-names", "--no-effective", "--physical")
UNIT_FILE_STATE: Final = "--property=UnitFileState"
#: The C-locale strerror(ENOENT) text stat/getfacl print for a missing path.
ENOENT_TEXT: Final = "No such file or directory"

#: Every subject kind ``families.resolve`` produces (plus legacy ``file:``).
OBSERVE_KINDS: Final = frozenset({"passwd", "group", "member", "owner_mode", "dir", "file", "acl", "linger", "enabled"})

#: Every NEW argv this module can emit, for Recovery (D15). Pinned by a test: the
#: rendered argv of every kind, snapshot, visudo and the T-5652 cleanup must equal
#: these templates exactly. ``reach`` says who can run it: ``observe()`` is
#: callable OUTSIDE the dark gate (read-only argv only; mason-approved for Recovery to
#: rule on), the rest run only inside the dark ``HostSubstratePort``. Every row is
#: read-only except the one T-5652 failure-path cleanup.
OBSERVE_ARGV_TABLE: Final = (
    # (argv template, classification, purpose, subject kinds, reach)
    ((GETENT, "passwd", "<A>"), "read-only", "account identity (uid, gid, home, shell)", ("passwd",), "observe()"),
    ((GETENT, "shadow", "<A>"), "read-only (root reads shadow); projected to name + expiry at parse time",
     "retired = shadow expiry field is 1 (the hash field never leaves the parser)", ("passwd",), "observe()"),
    ((GETENT, "group", "<G>"), "read-only", "gid and supplementary members", ("group", "member"), "observe()"),
    ((STAT, STAT_FORMAT, "--", "<P>"), "read-only (no -L: a symlink is refused)", "file type, owner, group, mode",
     ("owner_mode", "dir", "file", "preflight path_type"), "observe()"),
    ((STAT, STAT_FORMAT, "--", LINGER_DIR + "<A>"), "read-only", "linger flag file", ("linger",), "observe()"),
    ((SHA256SUM, "--", "<P>"), "read-only", "content pin", ("file",), "observe()"),
    ((GETFACL, *GETFACL_FLAGS, "--", "<P>"), "read-only; modified after D15 review: +--physical (symlink refusal)",
     "access + default ACL entries", ("acl",), "observe()"),
    ((SYSTEMCTL, "show", UNIT_FILE_STATE, "--value", "--", "<U>"), "read-only (manager property query)",
     "unit enablement", ("enabled",), "observe()"),
    ((GETENT, "passwd"), "read-only enumeration", "collision preflight", ("snapshot",), "HostSubstratePort (dark)"),
    ((GETENT, "group"), "read-only enumeration", "collision preflight", ("snapshot",), "HostSubstratePort (dark)"),
    ((VISUDO, "-cf", "<STAGED>"), "read-only syntax check", "sudoers fragment verdict before install",
     ("visudo_check",), "HostSubstratePort (dark)"),
    ((UNLINK, "<P>" + SUDOERS_TMP_SUFFIX), "mutating, failure-path cleanup, ruled T-5652",
     "remove the temp fragment when visudo -cf or mv fails before the mv lands (ENOENT = success)",
     ("sudoers_fragment.ensure",), "HostSubstratePort (dark)"),
)

_UNIT_RE = re.compile(r"^[A-Za-z0-9@_.][A-Za-z0-9@_.-]*$")
_MODE_RE = re.compile(r"^[0-7]{1,4}$")
_INT_RE = re.compile(r"^[0-9]+$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_REGULAR = frozenset({"regular file", "regular empty file"})
#: T-2920 U3c review MED-1: the ONLY stat ``%F`` texts the parsers accept. They are the
#: C-locale strings of gnulib ``file_type()`` (lib/file-type.c, used by coreutils stat) for
#: the kinds the subjects can legitimately be, plus the one the guard must recognise; each
#: verified against the real binary (GNU coreutils 9.4, LC_ALL=C: /etc -> directory,
#: /etc/hostname -> regular file, an empty file -> regular empty file, /etc/localtime ->
#: symbolic link). Anything else (a localized string such as "symbolische Verknüpfung", or
#: a fifo/socket/device, which no family converges) is an ObservationError, so the
#: preflight REFUSES instead of reading an unknown type as "not a symlink".
STAT_FILE_TYPES: Final = frozenset({*_REGULAR, "directory", "symbolic link"})


def _path(what: str, path: Any) -> str:
    if (not isinstance(path, str) or not path.startswith("/") or path == "/" or posixpath.normpath(path) != path
            or "//" in path or ".." in path.split("/") or any(c in path for c in "\x00\n")):
        raise ObservationError(f"{what}: {path!r} is not an absolute, normalized path")
    return path


def _name(what: str, name: str) -> str:
    if not F._NAME_RE.match(name):
        raise ObservationError(f"{what}: {name!r} is not an account/group name shape")
    return name


def _subject(subject: Any) -> tuple[str, str]:
    """Shape-check a subject: no operand can be relative or option-like."""

    if not isinstance(subject, str) or ":" not in subject:
        raise ObservationError(f"{subject!r} is not a subject")
    if subject.startswith(STAGED_PREFIX):
        raise ObservationError(f"{subject}: staged subjects are never observed on the host")
    kind, key = subject.split(":", 1)
    if kind in ("passwd", "group", "linger"):
        _name(subject, key)
    elif kind == "member":
        group, sep, account = key.partition(":")
        if not sep:
            raise ObservationError(f"{subject}: member subjects are member:<group>:<account>")
        _name(subject, group)
        _name(subject, account)
    elif kind in ("owner_mode", "dir", "file", "acl"):
        _path(subject, key)
    elif kind == "enabled":
        if not _UNIT_RE.match(key) or key in (".", ".."):
            raise ObservationError(f"{subject}: {key!r} is not a unit name shape")
    else:
        raise ObservationError(f"{subject}: unknown subject kind {kind!r}")
    return kind, key


def observe_argv(subject: str) -> list[list[str]]:
    """The fixed, read-only argv that observes ``subject``; all of it runs, in order."""

    kind, key = _subject(subject)
    if kind == "passwd":
        return [[GETENT, "passwd", key], [GETENT, "shadow", key]]
    if kind in ("group", "member"):
        return [[GETENT, "group", key.split(":", 1)[0]]]
    if kind in ("owner_mode", "dir"):
        return [[STAT, STAT_FORMAT, "--", key]]
    if kind == "linger":
        return [[STAT, STAT_FORMAT, "--", LINGER_DIR + key]]
    if kind == "file":
        return [[STAT, STAT_FORMAT, "--", key], [SHA256SUM, "--", key]]
    if kind == "acl":
        return [[GETFACL, *GETFACL_FLAGS, "--", key]]
    return [[SYSTEMCTL, "show", UNIT_FILE_STATE, "--value", "--", key]]


def path_type_argv(path: str) -> list[list[str]]:
    """T-5653 preflight: the row-4 stat (``STAT_FORMAT``, no -L) on one path. NOT a new
    argv: the same template as ``owner_mode:`` / ``dir:`` observation."""

    return [[STAT, STAT_FORMAT, "--", _path("path_type", path)]]


def _ancestor_path(what: str, path: Any) -> str:
    """T-5657 (Recovery D15: approved, WALK-ONLY): an ancestor operand is exactly ``/`` or a
    path ``_path`` accepts. ``_path`` itself, the SUBJECT validator, still refuses ``/``."""

    return path if path == "/" else _path(what, path)


def path_stat_argv(path: str) -> list[list[str]]:
    """T-5657 ancestor walk: the row-4 stat (``STAT_FORMAT``, no -L) on one ANCESTOR. NOT a
    new argv template: the same as ``owner_mode:`` / ``dir:`` observation; the one new
    operand value is ``/`` (walk-only, ``_ancestor_path``)."""

    return [[STAT, STAT_FORMAT, "--", _ancestor_path("path_stat", path)]]


def snapshot_argv() -> list[list[str]]:
    return [[GETENT, "passwd"], [GETENT, "group"]]


def visudo_argv(staged_path: str) -> list[list[str]]:
    return [[VISUDO, "-cf", _path("visudo staged source", staged_path)]]


def _one_line(what: str, res: _Result) -> str:
    out = res.stdout
    if not out.endswith("\n") or "\n" in out[:-1]:
        raise ObservationError(f"{what}: expected exactly one output line, got {out[:200]!r}")
    return out[:-1]


def _lines(what: str, res: _Result) -> list[str]:
    if res.returncode != 0:
        raise ObservationError(f"{what}: exited {res.returncode}: {res.stderr.strip()[:200]}")
    out = res.stdout
    if out and not out.endswith("\n"):
        raise ObservationError(f"{what}: truncated output")
    return out[:-1].split("\n") if out else []


def _getent_key(what: str, res: _Result) -> str | None:
    """getent exits 2 when the key is not found (getent(1)); anything else non-zero is refused."""

    if res.returncode == 2 and res.stdout == "":
        return None
    if res.returncode != 0:
        raise ObservationError(f"{what}: getent exited {res.returncode}: {res.stderr.strip()[:200]}")
    return _one_line(what, res)


def _int(what: str, text: str) -> int:
    if not _INT_RE.match(text):
        raise ObservationError(f"{what}: {text!r} is not a decimal id")
    return int(text)


def _passwd_line(line: str) -> tuple[str, dict[str, Any]]:
    f = line.split(":")
    if len(f) != 7 or not f[0]:
        raise ObservationError(f"passwd: malformed entry {line[:200]!r}")
    return f[0], {"uid": _int("passwd uid", f[2]), "gid": _int("passwd gid", f[3]), "home": f[5], "shell": f[6]}


def _shadow_parse(returncode: int, stdout: str) -> tuple[str, str] | str | None:
    """The row-2 projection itself. NEVER raises: it returns (name, expiry), None (not
    found) or an error message, so no traceback ever holds this frame's locals."""

    if returncode == 2 and stdout == "":
        return None
    if returncode != 0:
        return f"shadow: getent exited {returncode}"
    if not stdout.endswith("\n") or "\n" in stdout[:-1]:
        return "shadow: expected exactly one newline-terminated line"
    fields = stdout[:-1].split(":")
    if len(fields) != 9:
        return f"shadow: expected 9 fields, got {len(fields)}"
    if fields[7] and not _INT_RE.match(fields[7]):
        return "shadow: field 8 (account expiry) is not a decimal day count"
    return fields[0], fields[7]


def _shadow_project(results: list[Any], index: int) -> tuple[str, str] | None:
    """Recovery D15 ruling on OBSERVE_ARGV_TABLE row 2: shadow field 2 is the password hash.
    Project ONLY (name, expiry) here, at parse time. No raised text ever carries the
    stdout, the stderr or a field's content: errors name a count or a position.

    T-2920 U3c (U3b review nit): the raw record is taken OUT of ``results`` first, parsed
    by ``_shadow_parse`` (which never raises), and dropped before anything is raised, so
    no traceback frame's locals hold it (pinned by a frame-walking test).

    retired == EXPIRED by ``usermod --expiredate 1`` (the T-2918 rule: Ubuntu useradd
    already writes '!' for a fresh account, so the '!' prefix is not a signal)."""

    res = results[index]
    results[index] = None  # the raw shadow record goes no further
    outcome = _shadow_parse(res.returncode, res.stdout)
    del res
    if isinstance(outcome, str):
        raise ObservationError(outcome)
    return outcome


def _group_line(line: str) -> tuple[str, int, list[str]]:
    f = line.split(":")
    if len(f) != 4 or not f[0]:
        raise ObservationError(f"group: malformed entry {line[:200]!r}")
    return f[0], _int("group gid", f[2]), (f[3].split(",") if f[3] else [])


def _absent(res: _Result) -> bool:
    return res.returncode == 1 and res.stdout == "" and ENOENT_TEXT in res.stderr


def _stat_fields(what: str, res: _Result) -> list[str] | None:
    """Row-4 stat output -> [%F, %U, %G, %a] (None = absent). %F must be one of the
    allowlisted C-locale ``STAT_FILE_TYPES`` (fail closed on any other text)."""

    if res.returncode != 0:
        if _absent(res):
            return None
        raise ObservationError(f"{what}: stat exited {res.returncode}: {res.stderr.strip()[:200]}")
    parts = _one_line(what, res).split(":")
    if len(parts) != 4:
        raise ObservationError(f"{what}: unexpected stat output {res.stdout[:200]!r}")
    if parts[0] not in STAT_FILE_TYPES:
        raise ObservationError(f"{what}: unrecognised file type {parts[0][:80]!r} (not a C-locale allowlisted %F); refused")
    return parts


def _stat(what: str, res: _Result) -> tuple[str, str, str, str] | None:
    parts = _stat_fields(what, res)
    if parts is None:
        return None
    ftype, owner, group, mode = parts
    if ftype == "symbolic link":
        raise ObservationError(f"{what}: is a symbolic link; refused")
    if not (F._NAME_RE.match(owner) and F._NAME_RE.match(group)):
        raise ObservationError(f"{what}: owner/group {owner!r}:{group!r} is not a resolvable name")
    if not _MODE_RE.match(mode):
        raise ObservationError(f"{what}: mode {mode!r} is not octal")
    return ftype, owner, group, mode.zfill(4)


def parse_observation(subject: str, results: Sequence[_Result]) -> Any:
    """``observe_argv(subject)``'s outputs -> the subject's value (None = absent)."""

    kind, key = _subject(subject)
    results = list(results)
    if len(results) != len(observe_argv(subject)):
        raise ObservationError(f"{subject}: expected {len(observe_argv(subject))} results, got {len(results)}")
    if kind == "passwd":
        shadow = _shadow_project(results, 1)  # FIRST: project the hash away (Recovery D15, row 2)
        line = _getent_key(subject, results[0])
        if line is None:
            if shadow is not None:
                raise ObservationError(f"{subject}: a shadow entry with no passwd entry")
            return None
        if shadow is None:
            raise ObservationError(f"{subject}: no shadow entry (not root, or an inconsistent database)")
        name, value = _passwd_line(line)
        if name != key:
            raise ObservationError(f"{subject}: getent passwd answered for {name!r}")
        if shadow[0] != key:
            raise ObservationError(f"{subject}: the shadow entry (field 1) is for a different account")
        return {**value, "locked": shadow[1] == "1"}
    if kind in ("group", "member"):
        group, _sep, account = key.partition(":")
        line = _getent_key(subject, results[0])
        if line is None:
            return None
        name, gid, members = _group_line(line)
        if name != group:
            raise ObservationError(f"{subject}: getent answered for {name!r}")
        if kind == "group":
            return {"gid": gid}
        return True if account in members else None
    if kind == "acl":
        res = results[0]
        if res.returncode != 0:
            if _absent(res):
                return None
            raise ObservationError(f"{subject}: getfacl exited {res.returncode}: {res.stderr.strip()[:200]}")
        entries = [line for line in _lines(subject, res) if line != ""]
        bad = [line for line in entries if not F._ACL_RE.match(line)]
        if bad or not entries or len(set(entries)) != len(entries):
            raise ObservationError(f"{subject}: unrecognised ACL output {bad[:3] or entries[:3]!r}")
        return {"entries": sorted(entries)}
    if kind == "enabled":
        res = results[0]
        if res.returncode != 0:
            raise ObservationError(f"{subject}: systemctl show exited {res.returncode}: {res.stderr.strip()[:200]}")
        state = _one_line(subject, res)
        if state == "enabled":
            return True
        if state in ("disabled", ""):  # "" = no unit file installed
            return None
        raise ObservationError(f"{subject}: UnitFileState {state!r} is neither enabled nor disabled")
    st = _stat(subject, results[0])
    if kind == "file":
        sha_res = results[1]
        if st is None:
            if sha_res.returncode == 0:
                raise ObservationError(f"{subject}: stat says absent but sha256sum read it")
            return None
        ftype, owner, _group, mode = st
        if ftype not in _REGULAR:
            raise ObservationError(f"{subject}: is a {ftype}, not a regular file")
        if sha_res.returncode != 0:
            raise ObservationError(f"{subject}: sha256sum exited {sha_res.returncode}")
        digest, sep, rest = _one_line(subject, sha_res).partition("  ")
        if not sep or not _SHA_RE.match(digest) or rest != key:
            raise ObservationError(f"{subject}: unexpected sha256sum output")
        return {"sha256": digest, "mode": mode, "owner": owner}
    if st is None:
        return None
    ftype, owner, group, mode = st
    if kind == "linger":
        if ftype not in _REGULAR:
            raise ObservationError(f"{subject}: linger flag is a {ftype}")
        return True
    if kind == "dir" and ftype != "directory":
        raise ObservationError(f"{subject}: is a {ftype}, not a directory")
    return {"owner": owner, "group": group, "mode": mode}


def parse_path_type(path: str, res: _Result) -> str | None:
    """``path_type_argv(path)``'s output -> the stat ``%F`` text, TYPED (``symbolic link``
    is a value here, not an error, so the preflight's refusal is loud and specific), or
    None when absent. Anything unrecognised raises ``ObservationError`` (fail closed)."""

    parts = _stat_fields(f"path_type:{path}", res)
    if parts is None:
        return None
    if not parts[0] or not _MODE_RE.match(parts[3]):
        raise ObservationError(f"path_type:{path}: unexpected stat output {res.stdout[:200]!r}")
    return parts[0]


def parse_path_stat(path: str, res: _Result) -> tuple[str, str, str, str] | None:
    """``path_stat_argv(path)``'s output -> (%F, owner, group, 4-digit mode), TYPED
    (``symbolic link`` is a value, so the walk names it), or None when absent. %F is the
    ``_stat_fields`` C-locale allowlist; an unresolvable owner/group (stat prints
    ``UNKNOWN``) or a non-octal mode raises ``ObservationError`` (fail closed)."""

    parts = _stat_fields(f"path_stat:{path}", res)
    if parts is None:
        return None
    ftype, owner, group, mode = parts
    if not (F._NAME_RE.match(owner) and F._NAME_RE.match(group)):
        raise ObservationError(f"path_stat:{path}: owner/group {owner[:40]!r}:{group[:40]!r} is not a resolvable name")
    if not _MODE_RE.match(mode):
        raise ObservationError(f"path_stat:{path}: mode {mode[:20]!r} is not octal")
    return ftype, owner, group, mode.zfill(4)


def parse_snapshot(results: Sequence[_Result]) -> dict[str, Any]:
    """``snapshot_argv()``'s outputs -> every ``passwd:`` and ``group:`` subject.

    Exactly what RootHelper.preflight reads (helper.py ``_passwd_entries`` takes uid,
    gid, home and shell; ``_group_collision`` takes gid): ``passwd:*`` carries
    {uid, gid, home, shell} and ``group:*`` carries {gid}. There is no shadow
    enumeration (Recovery D15 redirect of the former row 10), so no ``locked`` here:
    ``get("passwd:<A>")`` is the only path that reads one account's expiry."""

    results = list(results)
    if len(results) != 2:
        raise ObservationError(f"snapshot: expected 2 results, got {len(results)}")
    out: dict[str, Any] = {}
    for line in _lines("getent passwd", results[0]):
        name, value = _passwd_line(line)
        if f"passwd:{name}" in out:
            raise ObservationError(f"passwd: duplicate entry {name!r}")
        out[f"passwd:{name}"] = value
    for line in _lines("getent group", results[1]):
        name, gid, _members = _group_line(line)
        if f"group:{name}" in out:
            raise ObservationError(f"group: duplicate entry {name!r}")
        out[f"group:{name}"] = {"gid": gid}
    return out


def parse_visudo(res: _Result) -> str:
    """The T-2918 verdict rule: non-zero = error; zero with stderr = warning; else ok."""

    if res.returncode != 0:
        return "error"
    return "warning" if res.stderr.strip() else "ok"


def observe(runner: CommandRunner, subject: str) -> Any:
    """Observe one subject. READ-ONLY and callable OUTSIDE the dark gate (T-2920 U3b
    decision, for Recovery to rule on): it constructs no runner and runs only argv from
    ``observe_argv`` (pinned to ``OBSERVE_ARGV_TABLE``). Recovery accepted this reach (D15).
    The row-2 shadow projection applies here too (inside ``parse_observation``)."""

    # Any new caller of observe() outside ports.py/tests is a D15 change (recovery).

    return parse_observation(subject, [runner.run(argv) for argv in observe_argv(subject)])


def manifest_subjects(manifest: F.Manifest) -> frozenset[str]:
    """Every live subject a manifest can name (what the host port may observe)."""

    m, out = manifest, set()
    for a in m.section("accounts"):
        out |= {f"passwd:{a}", f"linger:{a}"} | {f"member:{g}:{a}" for g in m.section("groups")}
    out |= {f"group:{g}" for g in m.section("groups")}
    for p in m.section("paths"):
        out |= {f"owner_mode:{p}", f"acl:{p}"}
    out |= {f"dir:{spec['path']}" for spec in m.section("mailbox_dirs").values()}
    for spec in m.section("units").values():
        out.add(f"file:{spec['path']}")
        if ".d/" not in spec["path"]:
            out.add(f"enabled:{posixpath.basename(spec['path'])}")
    for section in ("dbus_policies", "sudoers_templates"):
        out |= {f"file:{spec['path']}" for spec in m.section(section).values()}
    out |= {f"file:{req['path']}" for req in m.doc.get("legacy_requirements") or []}
    return frozenset(out)


# ---------------------------------------------------------------------------
# put: composes ONLY the Recovery-approved apply_argv / rollback_argv (T-2920 U3b)
# ---------------------------------------------------------------------------

_FILE_FAMILIES: Final = {
    "units": ("unit.system.install", "unit.system.install"),
    "dbus_policies": ("dbus_policy.install", "dbus_policy.retire"),
    "sudoers_templates": ("sudoers_fragment.ensure", "sudoers_fragment.retire"),
}


def _owner_mode_value(subject: str, value: Any) -> None:
    if (not isinstance(value, dict) or set(value) != {"owner", "group", "mode"}
            or not all(isinstance(value[k], str) for k in value)
            or not (F._NAME_RE.match(value["owner"]) and F._NAME_RE.match(value["group"]))
            or not re.match(r"^[0-7]{4}$", value["mode"])):
        raise ArgvGap(f"{subject}: {value!r} is not an owner/group/mode value")


def put_argv(subject: str, current: Any, value: Any, manifest: F.Manifest,
             staged_sources: Mapping[str, str]) -> list[list[str]]:
    """The argv that moves ``subject`` from ``current`` to ``value``: the family is
    recovered from the subject kind and its manifest section (the T-2918 sandbox
    port's mapping), every value comes from the manifest entry or the value, and
    the result is ONLY ``apply_argv`` / ``rollback_argv`` output. A transition no
    approved argv covers raises ``ArgvGap``."""

    if not isinstance(subject, str) or subject.startswith(STAGED_PREFIX):
        raise ArgvGap(f"{subject!r}: staged subjects never touch the host")
    try:
        kind, key = _subject(subject)
    except ObservationError as exc:
        raise ArgvGap(str(exc)) from None
    m, R = manifest, Resolved

    def spec_of(section: str, k: str) -> Mapping[str, Any]:
        entries = m.section(section)
        if k not in entries:
            raise ArgvGap(f"{subject}: {k!r} is not in the manifest's {section}")
        return entries[k]

    if kind == "passwd":
        spec = spec_of("accounts", key)
        if value is None:
            return [] if current is None else rollback_argv(R("account.ensure", subject, None, spec), None)
        identity = {"uid": spec["uid"], "gid": spec["uid"], "home": spec["home"], "shell": spec["shell"]}
        if not isinstance(value, dict) or {k: value.get(k) for k in identity} != identity:
            raise ArgvGap(f"{subject}: {value!r} differs from the manifest identity; no argv converges it")
        argvs: list[list[str]] = []
        cur = current
        if cur is None:
            argvs += apply_argv(R("account.ensure", subject, value, spec))
            cur = {**identity, "locked": False}
        if value.get("locked") and not cur.get("locked"):
            argvs += apply_argv(R("account.retire", subject, value, spec))
        elif not value.get("locked") and cur.get("locked"):
            argvs += rollback_argv(R("account.retire", subject, cur, spec), value)
        return argvs
    if kind == "group":
        spec = spec_of("groups", key)
        if value != {"gid": spec["gid"]}:
            raise ArgvGap(f"{subject}: no approved argv removes a group or changes its gid")
        if current is not None:
            if current != value:
                raise ArgvGap(f"{subject}: exists with {current!r}; no approved argv changes a gid")
            return []
        return apply_argv(R("group.ensure", subject, value, spec))
    if kind == "member":
        group, account = key.split(":", 1)
        spec_of("groups", group)
        if value not in (True, None):
            raise ArgvGap(f"{subject}: membership is True or None")
        return apply_argv(R("group.member", subject, value, spec_of("accounts", account)))
    if kind == "acl":
        spec = spec_of("paths", key)
        if (not isinstance(value, dict) or set(value) != {"entries"} or not isinstance(value["entries"], list)
                or not value["entries"] or not all(isinstance(e, str) and F._ACL_RE.match(e) for e in value["entries"])):
            raise ArgvGap(f"{subject}: {value!r} is not a non-empty ACL value (no approved argv clears an ACL)")
        return apply_argv(R("acl.converge", subject, value, spec))
    if kind == "owner_mode":
        spec = spec_of("paths", key)
        _owner_mode_value(subject, value)
        return apply_argv(R("owner_mode.set", subject, value, spec))
    if kind == "dir":
        dirs = {s["path"]: s for s in m.section("mailbox_dirs").values()}
        if key not in dirs:
            raise ArgvGap(f"{subject}: not a manifest mailbox dir")
        if value is None:
            raise ArgvGap(f"{subject}: no approved argv removes a mailbox directory (gate 34)")
        _owner_mode_value(subject, value)
        family = "mailbox_dir.retire" if value["mode"] == "0000" else "mailbox_dir.ensure"
        return apply_argv(R(family, subject, value, dirs[key]))
    if kind == "linger":
        spec = spec_of("accounts", key)
        if value not in (True, None):
            raise ArgvGap(f"{subject}: linger is True or None")
        return apply_argv(R("linger.enable" if value else "linger.disable", subject, value, spec))
    if kind == "enabled":
        units = {posixpath.basename(s["path"]): s for s in m.section("units").values() if ".d/" not in s["path"]}
        if key not in units:
            raise ArgvGap(f"{subject}: not an enableable manifest unit")
        if value not in (True, None):
            raise ArgvGap(f"{subject}: enablement is True or None")
        return apply_argv(R("unit.system.enable", subject, value, units[key]))
    # file:
    owner = next(((section, s) for section in _FILE_FAMILIES for s in m.section(section).values() if s["path"] == key), None)
    if owner is None:
        raise ArgvGap(f"{subject}: not a manifest unit / D-Bus policy / sudoers file (legacy files are verify-only)")
    section, spec = owner
    install, retire = _FILE_FAMILIES[section]
    if value is None:
        return [] if current is None else apply_argv(R(retire, subject, None, spec))
    if not isinstance(value, dict) or not isinstance(value.get("sha256"), str):
        raise ArgvGap(f"{subject}: {value!r} is not a file value")
    staged = staged_sources.get(value["sha256"])
    if staged is None:
        raise ArgvGap(f"{subject}: no staged, hash-checked source for sha256 {value['sha256']}")
    try:
        _path("staged source", staged)
    except ObservationError as exc:
        raise ArgvGap(str(exc)) from None
    return apply_argv(R(install, subject, value, spec), staged_source=staged)


def failure_cleanup_argv(subject: str, value: Any, manifest: F.Manifest, argvs: Sequence[Sequence[str]],
                         failed: int) -> list[list[str]]:
    """T-5652: the cleanup to run when ``argvs[failed]`` (from ``put_argv``) failed.

    Only a sudoers_fragment.ensure whose ``visudo -cf <tmp>`` or ``mv <tmp> <name>``
    failed gets one (``sudoers_tmp_cleanup_argv``). An install failure, and a failure of
    the final ``visudo -c`` / ``sudo -l`` after the mv, stay on the existing restore path."""

    if value is None or not isinstance(subject, str) or not subject.startswith("file:"):
        return []
    name = subject.split(":", 1)[1]
    spec = next((s for s in manifest.section("sudoers_templates").values() if s["path"] == name), None)
    if spec is None:
        return []
    cleanup = sudoers_tmp_cleanup_argv(Resolved("sudoers_fragment.ensure", subject, value, spec))
    tmp = cleanup[0][1]
    step = list(argvs[failed]) if 0 <= failed < len(argvs) else None
    if step in ([VISUDO, "-cf", tmp], [MV, "-f", "--", tmp, name]):
        return cleanup
    return []


def cleanup_ok(result: _Result) -> bool:
    """A cleanup succeeded, or there was nothing to clean (ENOENT is a no-op success)."""

    return result.returncode == 0 or _absent(result)


class _LiveSubstrateOps:
    """T-2920 U4b (Recovery-APPROVED mixin refactor): the live port's method BODIES, moved
    VERBATIM out of ``HostSubstratePort``. It has no ``__init__``: a subclass's
    constructor IS its gate. Exactly two subclasses exist, both pinned by a test:
    ``HostSubstratePort`` (dark: ``LIVE_ACTUATION_ENABLED``) and
    ``tools.host_broker_rehearsal_live.RehearsalSubstratePort`` (an attested nspawn
    machine). The mixin itself can never be instantiated."""

    _runner: CommandRunner
    _manifest: F.Manifest
    _subjects: frozenset[str]
    _staged_sources: dict[str, str]
    _staged: dict[str, Any]

    def __new__(cls, *args: Any, **kwargs: Any):
        if cls is _LiveSubstrateOps:
            raise LiveActuationDisabled("_LiveSubstrateOps is a mixin; only its gated subclasses construct")
        return super().__new__(cls)

    def get(self, subject: str) -> Any:  # pragma: no cover - live only (attested nspawn rehearsal)
        if subject.startswith(STAGED_PREFIX):
            return copy.deepcopy(self._staged.get(subject))
        if subject not in self._subjects:
            raise ObservationError(f"{subject}: not a subject this manifest names")
        return observe(self._runner, subject)

    def put(self, subject: str, value: Any) -> None:  # pragma: no cover - live only (attested nspawn rehearsal)
        if subject.startswith(STAGED_PREFIX):
            if value is None:
                self._staged.pop(subject, None)
            else:
                self._staged[subject] = copy.deepcopy(value)
            return
        argvs = put_argv(subject, self.get(subject), value, self._manifest, self._staged_sources)
        for index, argv in enumerate(argvs):
            result = self._runner.run(list(argv))
            if result.returncode != 0:
                cleaned = all(cleanup_ok(self._runner.run(c))
                              for c in failure_cleanup_argv(subject, value, self._manifest, argvs, index))
                raise RuntimeError(f"{argv[0]} exited {result.returncode}" + ("" if cleaned else "; temp cleanup failed"))

    def visudo_check(self, sha256: str) -> str:  # pragma: no cover - live only (attested nspawn rehearsal)
        staged = self._staged_sources.get(sha256)
        if staged is None:
            return "error"
        (argv,) = visudo_argv(staged)
        return parse_visudo(self._runner.run(argv))

    def snapshot(self) -> dict[str, Any]:  # pragma: no cover - live only (attested nspawn rehearsal)
        # No ``locked`` on passwd:* (see SimulatedSubstratePort.snapshot): shape divergence.
        return parse_snapshot([self._runner.run(argv) for argv in snapshot_argv()])

    def path_type(self, path: str) -> str | None:  # pragma: no cover - live only (attested nspawn rehearsal)
        (argv,) = path_type_argv(path)
        return parse_path_type(path, self._runner.run(argv))

    def path_stat(self, path: str) -> tuple[str, str, str, str] | None:  # pragma: no cover - live only (attested nspawn rehearsal)
        (argv,) = path_stat_argv(path)
        return parse_path_stat(path, self._runner.run(argv))

    def run(self, argvs: Sequence[Sequence[str]]) -> None:  # pragma: no cover - live only (attested nspawn rehearsal)
        for argv in argvs:
            result = self._runner.run(list(argv))
            if result.returncode != 0:
                raise RuntimeError(f"{argv[0]} exited {result.returncode}")


class HostSubstratePort(_LiveSubstrateOps):
    """The real-host port. DARK: refuses to construct in this tranche.

    Once live it satisfies ``SubstratePort``: ``get`` observes (read-only), ``put``
    runs ``put_argv`` (Recovery-approved argv only), staged subjects stay in memory
    (staging never touches the host, as in the simulated and sandbox ports)."""

    def __init__(self, runner: CommandRunner, manifest: F.Manifest | None = None,
                 staged_sources: Mapping[str, str] | None = None):
        if LIVE_ACTUATION_ENABLED is not True:
            raise LiveActuationDisabled(
                "host-substrate live actuation is disabled in this tranche (T-5084 lands dark; "
                "a separate reviewed go after S3 + C5-on-prod, T-3296/T-5106)"
            )
        if runner is None or manifest is None:  # pragma: no cover - unreachable while dark
            raise LiveActuationDisabled("an explicit CommandRunner and manifest are required")
        self._runner = runner  # pragma: no cover - unreachable while dark
        self._manifest = manifest  # pragma: no cover - unreachable while dark
        self._subjects = manifest_subjects(manifest)  # pragma: no cover - unreachable while dark
        self._staged_sources = dict(staged_sources or {})  # pragma: no cover - unreachable while dark
        self._staged: dict[str, Any] = {}  # pragma: no cover - unreachable while dark
