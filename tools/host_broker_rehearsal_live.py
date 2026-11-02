#!/usr/bin/env python3
"""T-2920 U4b: the ATTESTED rehearsal live layer (the only actuating code in the arc).

Intent (T-2920, arc T-2652): the U4a harness drives the REAL U3a installer path end
to end inside a disposable ``systemd-nspawn`` machine (the D5 real-path proof that
gates the T-2920 enable). This module is that live layer, confined BY CONSTRUCTION to
such a machine. Both repo dark literals (``tools.host_broker_install`` and
``host_substrate.ports`` ``LIVE_ACTUATION_ENABLED``) stay False; this module reads
neither and has no switch of its own: its gate is the attestation.

Recovery-approved design (D15, U4 plan section 2, option (c)) with his BLOCKING additions:

* ``RehearsalAttestation``: constructible only through ``load``, which requires EVERY
  factor (container file, PID 1 environ, a root 0600 attestation binding the run to
  the reviewed plan, the nonce, a machine-id that is NOT the host's, the hostname, euid
  0, an exact environment, and (a) the MOUNT FACTOR over ``/proc/self/mountinfo``).
  ``recheck`` re-reads the container file, the machine-id and mountinfo before EVERY
  mutation and EVERY runner call.
* ``OperatorRunner``: the one subprocess call, behind (b) the full-argv gate.
* ``LiveHost``: FixtureHost's surface at the real ``/``.
* ``RehearsalSubstratePort``: ``ports._LiveSubstrateOps`` behind the attestation.
* (c) receipts: mode ``rehearsal-nspawn``, bound to this machine and run (U3a seam).
* All four classes are constructed only in ``main``, after ``load`` (AST-pinned).

NOT in the framework closure (pinned). Entrypoint (U4a's golden script)::

    /usr/bin/python3 -I /srv/rehearsal/repo/tools/host_broker_rehearsal_live.py \\
        {apply|verify|rollback|guard-probe|observe-probe} --nonce-sha256 <hex> \\
        --plan P --manifest M --registry R --tracked-files T --inputs I \\
        --receipt /var/lib/pantheon-install-receipts/<step>.json [--execute] [--expect-started]

with cwd ``/srv/rehearsal/repo``. ``guard-probe`` and ``observe-probe`` are OBSERVE-ONLY.
"""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import posixpath
import pwd
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import host_broker_install as H  # noqa: E402
from tools import host_broker_install_apply as A  # noqa: E402
from tools._organ_lifecycle import CommandResult  # noqa: E402
from tools.germinate.host_substrate import families as F  # noqa: E402
from tools.germinate.host_substrate import ports as P  # noqa: E402

# ---------------------------------------------------------------------------
# fixed literals
# ---------------------------------------------------------------------------

CONTAINER_FILE: Final = "/run/systemd/container"
PID1_ENVIRON: Final = "/proc/1/environ"
ATTESTATION_FILE: Final = "/etc/pantheon-rehearsal/attestation.json"
NONCE_FILE: Final = "/etc/pantheon-rehearsal/nonce"
MACHINE_ID_FILE: Final = "/etc/machine-id"
HOSTNAME_FILE: Final = "/etc/hostname"
MOUNTINFO_FILE: Final = "/proc/self/mountinfo"
#: Recovery D15 W3 (2026-11-01, T-5703): the userns root-mapped base is READ from these (never a literal)
UID_MAP_FILE: Final = "/proc/self/uid_map"
GID_MAP_FILE: Final = "/proc/self/gid_map"

CONTAINER_VALUE: Final = b"systemd-nspawn\n"
PID1_CONTAINER_RECORD: Final = b"container=systemd-nspawn"
ATTESTATION_SCHEMA: Final = "pantheon-rehearsal-attestation/v1"
#: mason ruling (Q1): plan_sha256 binds the run to the reviewed plan.
ATTESTATION_KEYS: Final = frozenset({"schema", "machine", "run_id", "nonce_sha256", "host_machine_id_sha256",
                                     "plan_sha256"})
REQUIRED_ENV: Final = {"LC_ALL": "C", "PATH": "/usr/bin:/bin"}
REPO_BIND: Final = "/srv/rehearsal/repo"
RECEIPT_DIR: Final = "/var/lib/pantheon-install-receipts"
MAX_FACT_BYTES: Final = 1 << 20

#: Recovery D15 (T-2920 U4b): the producer's setpriv form is RULED (the amended template in
#: tools/host_broker_install.py: --no-new-privs --inh-caps=-all --bounding-set=-all).
SETPRIV_TEMPLATE_RULED: Final = True

#: mason-approved bounds (seconds): the root build line, and every other argv.
BUILD_TIMEOUT: Final = 1800
DEFAULT_TIMEOUT: Final = 300

#: every validator here is ``re.fullmatch`` over an ANCHORLESS pattern: ``$`` with
#: ``.match`` also matches before a final newline (U4a review MED), so "x\n" would pass.
_HEX64 = re.compile(r"[0-9a-f]{64}")
_MACHINE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_RECEIPT_RE = re.compile(r"/var/lib/pantheon-install-receipts/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json")

#: The module-private construction key (the attestation and the root-'/' primitives).
_KEY: Final = object()


class AttestationRefused(RuntimeError):
    """``load`` refused: this is not an attested rehearsal machine (rc 3)."""

    def __init__(self, reasons: Sequence[str]):
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


class AttestationLost(RuntimeError):
    """``recheck`` failed between actions: stop before the next mutation or call."""


class ArgvRefused(RuntimeError):
    """The runner gate refused an argv (it is not a RULED template bound to the plan)."""


class LiveRefused(A.EntryFailed):
    """A live primitive refused (root '/' without the key, a symlink, a fixture-only op)."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# (a) the MOUNT FACTOR (Recovery, BLOCKING)
# ---------------------------------------------------------------------------


class MountinfoError(ValueError):
    """/proc/self/mountinfo did not parse (refuse the whole file)."""


@dataclass(frozen=True)
class Mount:
    mount_id: int
    root: str
    mountpoint: str
    read_only: bool
    fstype: str
    #: the raw mountinfo line, verbatim (Recovery's refusal text quotes it)
    line: str = ""
    #: Recovery D15 W3 (T-5703): the fields the RULED_MOUNTS entries are exact on
    parent_id: int = -1
    devno: str = ""
    opts: tuple[str, ...] = ()
    source: str = ""
    superopts: tuple[str, ...] = ()


_ESCAPE = re.compile(r"\\([0-7]{3})")


def _unescape(field: str) -> str:
    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(text: str) -> list[Mount]:
    """proc(5) mountinfo: ``id parent maj:min root mountpoint opts [optional...] - fstype
    source superopts``. Anything that does not parse refuses the whole file."""

    if not text.endswith("\n"):
        raise MountinfoError("mountinfo: truncated (no final newline)")
    out = []
    for n, line in enumerate(text[:-1].split("\n"), 1):
        fields = line.split(" ")
        if "-" not in fields:
            raise MountinfoError(f"mountinfo line {n}: no ' - ' separator")
        sep = fields.index("-")
        if sep < 6 or len(fields) - sep != 4:
            raise MountinfoError(f"mountinfo line {n}: wrong field count")
        mid, parent, devno, root, mountpoint, opts = fields[:6]
        if not (re.fullmatch(r"[0-9]+", mid) and re.fullmatch(r"[0-9]+", parent) and re.fullmatch(r"[0-9]+:[0-9]+", devno)):
            raise MountinfoError(f"mountinfo line {n}: malformed ids")
        flags = opts.split(",")
        if flags[0] not in ("ro", "rw") or ("ro" in flags) == ("rw" in flags):
            raise MountinfoError(f"mountinfo line {n}: per-mount options carry no single ro/rw")
        mountpoint, root = _unescape(mountpoint), _unescape(root)
        if not mountpoint.startswith("/") or not root.startswith("/"):
            raise MountinfoError(f"mountinfo line {n}: a non-absolute mountpoint or root")
        fstype = fields[sep + 1]
        if not fstype:
            raise MountinfoError(f"mountinfo line {n}: empty fstype")
        out.append(Mount(int(mid), root, mountpoint, flags[0] == "ro", fstype, line, int(parent), devno, tuple(flags),
                         fields[sep + 2], tuple(fields[sep + 3].split(","))))
    if not out:
        raise MountinfoError("mountinfo: empty")
    return out


#: Rule 3: FRESH virtual mounts (field-4 root '/') at these exact mountpoints (rw allowed).
#:
#: KNOWN LIMIT (review F1): rule 3 cannot tell a FRESH tmpfs / cgroup2 instance from a
#: rw bind of a WHOLE host tmpfs or cgroup2 (both show field-4 root '/'). U4a's pinned
#: nspawn argv (-U, no --bind=, only --bind-ro=/srv/rehearsal/repo, no --volatile) is the
#: PRIMARY control there; this factor is the second line. ``/dev`` is tmpfs ONLY: nspawn
#: always mounts a tmpfs at /dev, so a devtmpfs there can only be a host bind.
VIRTUAL_EXACT: Final = {
    "/proc": frozenset({"proc"}),
    "/sys/fs/cgroup": frozenset({"cgroup2", "tmpfs"}),
    "/dev": frozenset({"tmpfs"}),
    "/dev/shm": frozenset({"tmpfs"}),
    "/dev/pts": frozenset({"devpts"}),
    "/dev/mqueue": frozenset({"mqueue"}),
    "/dev/hugepages": frozenset({"hugetlbfs"}),
    "/run": frozenset({"tmpfs"}),
    "/tmp": frozenset({"tmpfs"}),
}
#: Rule 3 (prefix form): fresh virtual mounts under these prefixes, (fstypes, ro required).
VIRTUAL_PREFIX: Final = (
    ("/sys/fs/cgroup/", frozenset({"cgroup", "cgroup2"}), False),
    ("/run/systemd/", frozenset({"tmpfs"}), False),
    ("/run/user/", frozenset({"tmpfs"}), False),
    ("/run/credentials/", frozenset({"tmpfs", "ramfs"}), True),
    ("/sys/kernel/", frozenset({"debugfs", "tracefs", "securityfs", "configfs", "fusectl"}), True),
)
#: Rule 4: nspawn's read-only masks / binds: ANY fstype, but ro is mandatory.
RO_MASK_PREFIXES: Final = ("/proc/", "/sys/", "/run/host/")
#: Rule 5: the console (a bind of the container's pty).
CONSOLE: Final = ("/dev/console", "devpts")


#: Recovery D15 W3 ruling (2026-11-01, T-5703): the mounts REAL systemd-nspawn 255 creates that
#: rules 1-5 refused in rehearsal 1 (run 1990856d106c, P0-mountinfo.txt). Recovery's first real
#: widening, and the ONLY one: anything new in rehearsal 2 goes back to Recovery. A mount at a
#: ruled (mountpoint, fstype) pair must match ONE entry exactly or it refuses (it never falls
#: through to rules 3-4). Every entry is exact on mountpoint, fstype, root ("root" verbatim or
#: "root_re" fullmatch), the ro/rw flag and the nosuid/nodev/noexec set ("flags"). Further:
#:   "super":    superblock options that must each appear verbatim (a size or mode change refuses);
#:   "userns":   the tmpfs superblock uid= and gid= must EQUAL the run's userns root-mapped base,
#:               READ from the machine's /proc/self/uid_map and gid_map, never a literal;
#:   "source":   field 3 (major:minor) must equal that RULED_ANCHORS mount's;
#:   "fresh":    its own major:minor, shared with no other mount (a separate fresh tmpfs);
#:   "parent":   field 2 must be that RULED_ANCHORS mount's id;
#:   "max":      lines accepted per entry (the next one refuses);
#:   "requires": a flag the pinned nspawn argv must carry (NSPAWN_PINNED_FLAGS).
RULED_MOUNTS: Final = (
    {"id": "W3-1", "mountpoint": "/sys", "fstype": "tmpfs", "root": "/", "root_re": None, "ro": True,
     "flags": ("nosuid", "nodev", "noexec"), "super": ("mode=555", "size=4096k"), "userns": True,
     "source": None, "fresh": False, "parent": None, "max": 1, "requires": None},
    {"id": "W3-2", "mountpoint": "/run/host", "fstype": "tmpfs", "root": "/host", "root_re": None, "ro": True,
     "flags": ("nosuid", "nodev", "noexec"), "super": (), "userns": True,
     "source": "/run", "fresh": False, "parent": None, "max": 1, "requires": None},
    {"id": "W3-3a", "mountpoint": "/proc/sys/net", "fstype": "proc", "root": "/sys/net", "root_re": None, "ro": False,
     "flags": ("nosuid", "nodev", "noexec"), "super": (), "userns": False,
     "source": "/proc", "fresh": False, "parent": "/proc/sys", "max": 1, "requires": "--private-network"},
    {"id": "W3-3b", "mountpoint": "/proc/sys/net", "fstype": "proc", "root": "/sys/net", "root_re": None, "ro": False,
     "flags": ("nosuid", "nodev", "noexec"), "super": (), "userns": False,
     "source": "/proc", "fresh": False, "parent": "/proc", "max": 1, "requires": "--private-network"},
    {"id": "W3-4a", "mountpoint": "/proc/sys/kernel/random/boot_id", "fstype": "tmpfs", "root": None,
     "root_re": r"/\.#proc-sys-kernel-random-boot-id[0-9a-f]{16}//deleted", "ro": True,
     "flags": ("nosuid", "nodev", "noexec"), "super": (), "userns": True,
     "source": "/run", "fresh": False, "parent": "/proc/sys", "max": 1, "requires": None},
    {"id": "W3-4b", "mountpoint": "/proc/sys/kernel/random/boot_id", "fstype": "tmpfs", "root": None,
     "root_re": r"/\.#proc-sys-kernel-random-boot-id[0-9a-f]{16}//deleted", "ro": False,
     "flags": ("nosuid", "nodev"), "super": (), "userns": True,
     "source": "/run", "fresh": False, "parent": "/proc", "max": 1, "requires": None},
    {"id": "W3-5", "mountpoint": "/proc/kmsg", "fstype": "tmpfs", "root": None,
     "root_re": r"/\.#proc-kmsg[0-9a-f]{16}//deleted", "ro": False,
     "flags": ("nosuid", "nodev"), "super": (), "userns": True,
     "source": "/run", "fresh": False, "parent": "/proc", "max": 1, "requires": None},
    {"id": "W3-6", "mountpoint": "/run/lock", "fstype": "tmpfs", "root": "/", "root_re": None, "ro": False,
     "flags": ("nosuid", "nodev", "noexec"), "super": ("size=5120k",), "userns": True,
     "source": None, "fresh": True, "parent": "/run", "max": 1, "requires": None},
)
#: the machine's own mounts the ruled entries anchor on: mountpoint -> fstype (exactly one each)
RULED_ANCHORS: Final = {"/run": "tmpfs", "/proc": "proc", "/proc/sys": "proc"}
#: the run-independent flags of U4a's PINNED nspawn argv (parity-tested against
#: host_broker_rehearsal.nspawn_argv): W3-3 is accepted ONLY while it carries --private-network.
NSPAWN_PINNED_FLAGS: Final = ("--quiet", "--boot", "--notify-ready=yes", "-U", "--private-network")
#: the per-mount flags a RULED_MOUNTS "flags" set is exact on
RULED_FLAG_SET: Final = ("nosuid", "nodev", "noexec")


def parse_id_map(data: bytes | None) -> int | None:
    """``/proc/self/{uid,gid}_map``: exactly ONE line ``0 <base> <count>`` (whitespace-padded
    by the kernel). The base of a user namespace is never 0 (``-U``). None = refuse."""

    if data is None:
        return None
    try:
        text = data.decode("ascii", "strict")
    except UnicodeDecodeError:
        return None
    if not text.endswith("\n") or text.count("\n") != 1:
        return None
    parts = text.split()
    if len(parts) != 3 or not all(re.fullmatch(r"[0-9]{1,10}", p) for p in parts):
        return None
    inside, base, count = (int(p) for p in parts)
    if inside != 0 or base == 0 or count == 0 or base + count > 1 << 32:
        return None
    return base


def _ruled_anchors(mounts: Sequence[Mount]) -> dict[str, Mount]:
    out = {}
    for point, fstype in RULED_ANCHORS.items():
        at = [m for m in mounts if m.mountpoint == point]
        if len(at) == 1 and at[0].fstype == fstype:
            out[point] = at[0]
    return out


def _superopt_values(m: Mount, key: str) -> list[str]:
    return [o[len(key) + 1:] for o in m.superopts if o.startswith(key + "=")]


def _ruled_match(m: Mount, e: Mapping[str, Any], anchors: Mapping[str, Mount], devno_count: Mapping[str, int],
                 ids: tuple[int, int] | None) -> bool:
    """Recovery D15 W3: does mount ``m`` match RULED_MOUNTS entry ``e`` EXACTLY?"""

    if (m.mountpoint, m.fstype) != (e["mountpoint"], e["fstype"]) or m.read_only != e["ro"]:
        return False
    if e["root"] is not None and m.root != e["root"]:
        return False
    if e["root_re"] is not None and not re.fullmatch(e["root_re"], m.root):
        return False
    if {f for f in m.opts if f in RULED_FLAG_SET} != set(e["flags"]):
        return False
    if not all(o in m.superopts for o in e["super"]):
        return False
    if e["userns"]:
        if ids is None or _superopt_values(m, "uid") != [str(ids[0])] or _superopt_values(m, "gid") != [str(ids[1])]:
            return False
    if e["source"] is not None and (e["source"] not in anchors or m.devno != anchors[e["source"]].devno):
        return False
    if e["fresh"] and devno_count.get(m.devno) != 1:
        return False
    if e["parent"] is not None and (e["parent"] not in anchors or m.parent_id != anchors[e["parent"]].mount_id):
        return False
    if e["requires"] is not None and e["requires"] not in NSPAWN_PINNED_FLAGS:
        return False
    return True


#: Recovery's exact refusal wording for a mount the allowlist does not list (U4a's checker
#: and the operator see this same text); the raw mountinfo line follows verbatim.
UNLISTED_MOUNT: Final = "attestation refused: unlisted mount "


def mount_refusals(mounts: Sequence[Mount], ids: tuple[int, int] | None = None) -> list[str]:
    """The allowlist, first match wins; anything unmatched refuses. A writable bind of a
    host path (any rw mount of a non-virtual fs other than '/') always refuses. Every
    per-mount refusal is Recovery's exact text, ``UNLISTED_MOUNT`` + the raw line verbatim.
    ``ids`` = the (uid, gid) userns root-mapped base read from the machine's id maps; with
    None every RULED_MOUNTS "userns" entry refuses."""

    out: list[str] = []
    anchors = _ruled_anchors(mounts)
    devno_count: dict[str, int] = {}
    for m in mounts:
        devno_count[m.devno] = devno_count.get(m.devno, 0) + 1
    ruled_points = {e["mountpoint"] for e in RULED_MOUNTS}
    used = {e["id"]: 0 for e in RULED_MOUNTS}
    roots = [m for m in mounts if m.mountpoint == "/"]
    repos = [m for m in mounts if m.mountpoint == REPO_BIND]
    if len(roots) != 1:
        out.append(f"mountinfo: {len(roots)} mounts at '/' (exactly one: the machine's own root)")
    if len(repos) != 1:
        out.append(f"mountinfo: {len(repos)} mounts at {REPO_BIND} (exactly one read-only bind)")
    for m in mounts:
        if m.mountpoint == "/":  # rule 1
            continue
        if m.mountpoint == REPO_BIND:  # rule 2
            if not m.read_only:
                out.append(UNLISTED_MOUNT + m.line)  # a rw repo bind is not listed
            continue
        if m.mountpoint in ruled_points:  # Recovery D15 W3 (T-5703): exact on the MOUNTPOINT, no fall-through
            hit = next((e for e in RULED_MOUNTS if used[e["id"]] < e["max"]
                        and _ruled_match(m, e, anchors, devno_count, ids)), None)
            if hit is None:
                out.append(UNLISTED_MOUNT + m.line)
            else:
                used[hit["id"]] += 1
            continue
        if m.mountpoint in VIRTUAL_EXACT:  # rule 3
            if m.fstype in VIRTUAL_EXACT[m.mountpoint] and m.root == "/":
                continue
            if m.read_only and m.mountpoint.startswith(RO_MASK_PREFIXES):
                continue
            out.append(UNLISTED_MOUNT + m.line)  # a listed mountpoint with an unlisted fs / root
            continue
        matched = False
        for prefix, fstypes, ro_required in VIRTUAL_PREFIX:  # rule 3 (prefix form)
            if m.mountpoint.startswith(prefix) and m.fstype in fstypes and m.root == "/":
                if ro_required and not m.read_only:
                    out.append(UNLISTED_MOUNT + m.line)  # listed ro-only
                matched = True
                break
        if matched:
            continue
        if m.mountpoint.startswith(RO_MASK_PREFIXES):  # rule 4
            if not m.read_only:
                out.append(UNLISTED_MOUNT + m.line)  # a rw mask is not listed
            continue
        if (m.mountpoint, m.fstype) == CONSOLE:  # rule 5
            continue
        out.append(UNLISTED_MOUNT + m.line)
    return out


# ---------------------------------------------------------------------------
# attestation facts (captured bytes) and the PURE decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileFact:
    data: bytes
    uid: int
    mode: int
    regular: bool


@dataclass(frozen=True)
class Facts:
    container: FileFact | None
    pid1_environ: FileFact | None
    attestation: FileFact | None
    nonce: FileFact | None
    machine_id: FileFact | None
    hostname: FileFact | None
    mountinfo: FileFact | None
    euid: int
    environ: Mapping[str, str]
    #: Recovery D15 W3 (T-5703): the machine's id maps (the userns root-mapped base)
    uid_map: FileFact | None = None
    gid_map: FileFact | None = None


def _read_fact(path: str) -> FileFact | None:
    """A fixed literal path, O_RDONLY|O_NOFOLLOW, bounded. None = unreadable (refuse)."""

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FACT_BYTES:
                return None
            chunks.append(chunk)
        return FileFact(b"".join(chunks), st.st_uid, stat.S_IMODE(st.st_mode), stat.S_ISREG(st.st_mode))
    except OSError:
        return None
    finally:
        os.close(fd)


def _read_facts() -> Facts:
    return Facts(_read_fact(CONTAINER_FILE), _read_fact(PID1_ENVIRON), _read_fact(ATTESTATION_FILE),
                 _read_fact(NONCE_FILE), _read_fact(MACHINE_ID_FILE), _read_fact(HOSTNAME_FILE),
                 _read_fact(MOUNTINFO_FILE), os.geteuid(), dict(os.environ),
                 _read_fact(UID_MAP_FILE), _read_fact(GID_MAP_FILE))


def _container_refusals(container: FileFact | None) -> list[str]:
    if container is None or container.data != CONTAINER_VALUE:
        return [f"{CONTAINER_FILE}: not exactly 'systemd-nspawn' (not an nspawn machine)"]
    return []


def _userns_ids(uid_map: FileFact | None, gid_map: FileFact | None) -> tuple[int, int] | None:
    uid = parse_id_map(uid_map.data if uid_map is not None else None)
    gid = parse_id_map(gid_map.data if gid_map is not None else None)
    return None if uid is None or gid is None else (uid, gid)


def _mount_factor(mountinfo: FileFact | None, uid_map: FileFact | None = None,
                  gid_map: FileFact | None = None) -> list[str]:
    """The mount factor (load AND recheck). The userns base comes from the id maps read in
    the SAME pass; an unreadable or malformed map refuses every RULED_MOUNTS userns entry."""

    if mountinfo is None:
        return [f"{MOUNTINFO_FILE}: unreadable"]
    try:
        return mount_refusals(parse_mountinfo(mountinfo.data.decode("utf-8", "strict")),
                              _userns_ids(uid_map, gid_map))
    except (MountinfoError, UnicodeDecodeError) as exc:
        return [f"{MOUNTINFO_FILE}: {exc}"]


def _machine_id_sha(machine_id: FileFact | None) -> str | None:
    if machine_id is None or not machine_id.data:
        return None
    return sha256_bytes(machine_id.data)


def _evaluate(facts: Facts, argv_nonce_sha256: str | None,
              plan_sha256: str | None) -> tuple[list[str], dict[str, Any] | None]:
    """The PURE decision over captured facts: (refusal reasons, the attestation doc).
    Empty reasons = every factor holds."""

    out = list(_container_refusals(facts.container))
    env = facts.pid1_environ
    if env is None or PID1_CONTAINER_RECORD not in env.data.split(b"\0"):
        out.append(f"{PID1_ENVIRON}: no container=systemd-nspawn record")
    doc: dict[str, Any] | None = None
    att = facts.attestation
    if att is None:
        out.append(f"{ATTESTATION_FILE}: missing or unreadable")
    else:
        if not att.regular or att.uid != 0 or att.mode != 0o600:
            out.append(f"{ATTESTATION_FILE}: must be a regular file, root-owned, mode 0600")
        try:
            loaded = json.loads(att.data.decode("utf-8", "strict"))
        except (UnicodeDecodeError, ValueError):
            loaded = None
        if not isinstance(loaded, dict) or set(loaded) != ATTESTATION_KEYS or not all(
                isinstance(loaded[k], str) for k in ATTESTATION_KEYS):
            out.append(f"{ATTESTATION_FILE}: must hold exactly the string fields {sorted(ATTESTATION_KEYS)}")
        elif loaded["schema"] != ATTESTATION_SCHEMA:
            out.append(f"{ATTESTATION_FILE}: schema must be {ATTESTATION_SCHEMA}")
        else:
            doc = loaded
            for k in ("nonce_sha256", "host_machine_id_sha256", "plan_sha256"):
                if not _HEX64.fullmatch(doc[k]):
                    out.append(f"{ATTESTATION_FILE}: {k} is not a sha256")
            if not _MACHINE_RE.fullmatch(doc["machine"]):
                out.append(f"{ATTESTATION_FILE}: machine is not a machine-name shape")
            if not _RUN_ID_RE.fullmatch(doc["run_id"]):
                out.append(f"{ATTESTATION_FILE}: run_id is not a run-id shape")
    nonce = facts.nonce
    nonce_sha = sha256_bytes(nonce.data) if nonce is not None and nonce.data else None
    if nonce_sha is None:
        out.append(f"{NONCE_FILE}: missing or empty")
    if not isinstance(argv_nonce_sha256, str) or not _HEX64.fullmatch(argv_nonce_sha256):
        out.append("--nonce-sha256: not a sha256")
    if doc is not None and not (nonce_sha == doc["nonce_sha256"] == argv_nonce_sha256):
        out.append("nonce: sha256(nonce file), the attested value and --nonce-sha256 differ")
    mid = _machine_id_sha(facts.machine_id)
    if mid is None:
        out.append(f"{MACHINE_ID_FILE}: missing or empty")
    elif doc is not None and mid == doc["host_machine_id_sha256"]:
        out.append(f"{MACHINE_ID_FILE}: this IS the host's machine-id (a host run refuses by construction)")
    hostname = facts.hostname
    name = hostname.data.decode("ascii", "replace") if hostname is not None else None
    if name is not None and name.endswith("\n"):
        name = name[:-1]
    if name is None or not _MACHINE_RE.fullmatch(name):
        out.append(f"{HOSTNAME_FILE}: missing or not a machine-name shape")
    elif doc is not None and name != doc["machine"]:
        out.append(f"{HOSTNAME_FILE}: differs from the attested machine")
    if doc is not None and (plan_sha256 is None or plan_sha256 != doc["plan_sha256"]):
        out.append("plan: the rebuilt plan's plan_sha256 is not the attested plan")
    if facts.euid != 0:
        out.append("euid: not 0")
    if dict(facts.environ) != REQUIRED_ENV:
        out.append(f"environment: must be exactly {REQUIRED_ENV}")
    out.extend(_mount_factor(facts.mountinfo, facts.uid_map, facts.gid_map))
    return out, (doc if not out else None)


def _recheck_reasons(container: FileFact | None, machine_id: FileFact | None, mountinfo: FileFact | None,
                     machine_id_sha256: str, uid_map: FileFact | None = None,
                     gid_map: FileFact | None = None) -> list[str]:
    out = _container_refusals(container)
    if _machine_id_sha(machine_id) != machine_id_sha256:
        out.append(f"{MACHINE_ID_FILE}: changed since load")
    out.extend(_mount_factor(mountinfo, uid_map, gid_map))
    return out


class RehearsalAttestation:
    """Proof that this process runs as root inside the attested nspawn machine for the
    attested plan. Only ``load`` constructs one (the module-private key)."""

    __slots__ = ("machine", "run_id", "nonce_sha256", "plan_sha256", "_machine_id_sha256", "_sealed")

    def __init__(self, key: object, doc: Mapping[str, str], machine_id_sha256: str):
        if key is not _KEY:
            raise AttestationRefused(["RehearsalAttestation: construct only through RehearsalAttestation.load"])
        object.__setattr__(self, "machine", doc["machine"])
        object.__setattr__(self, "run_id", doc["run_id"])
        object.__setattr__(self, "nonce_sha256", doc["nonce_sha256"])
        object.__setattr__(self, "plan_sha256", doc["plan_sha256"])
        object.__setattr__(self, "_machine_id_sha256", machine_id_sha256)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("RehearsalAttestation is immutable")

    @staticmethod
    def load(nonce_sha256: str, plan_sha256: str | None) -> "RehearsalAttestation":
        facts = _read_facts()
        reasons, doc = _evaluate(facts, nonce_sha256, plan_sha256)
        mid = _machine_id_sha(facts.machine_id)
        if reasons or doc is None or mid is None:
            raise AttestationRefused(reasons or ["attestation: incomplete"])
        return RehearsalAttestation(_KEY, doc, mid)

    def recheck(self) -> None:
        """Before EVERY mutation and EVERY runner call."""

        reasons = _recheck_reasons(_read_fact(CONTAINER_FILE), _read_fact(MACHINE_ID_FILE),
                                   _read_fact(MOUNTINFO_FILE), self._machine_id_sha256,
                                   _read_fact(UID_MAP_FILE), _read_fact(GID_MAP_FILE))
        if reasons:
            raise AttestationLost("; ".join(reasons))


# ---------------------------------------------------------------------------
# (b) the RUNNER ARGV GATE (Recovery, BLOCKING): derived from the RULED tables
# ---------------------------------------------------------------------------

_SLOT = re.compile(r"<([A-Z]+)>")
#: sentinels used to RENDER the ruled argv functions into templates
_S = {name: f"<{name}>" for name in ("A", "G", "P", "U", "N", "ID", "MODE", "ACL", "SRC", "STAGED", "PKG", "SH")}


@dataclass(frozen=True)
class Template:
    tokens: tuple[tuple[re.Pattern[str], tuple[str, ...]] | str, ...]
    mutating: bool
    origin: str

    @property
    def binary(self) -> str:
        first = self.tokens[0]
        assert isinstance(first, str)
        return first


#: review F3: a path slot is P_OBSERVE in a read-only template (subjects plus every
#: ancestor: the row-4 walk) and P_MUTATE in a mutating one (leaf subjects, plus the
#: account homes the plan names). Owner/group (N) and MODE are contextual: checked
#: against the template's one P_MUTATE value (see ``ArgvAllowlist._context_ok``).
P_OBSERVE: Final = "PO"
P_MUTATE: Final = "PM"
#: delta-review L1: an account's LOGIN SHELL is never a mutated path. It is its own slot,
#: rendered ONLY into useradd's ``--shell`` position (account.ensure), so no generic
#: unlink / install / chmod / chown / setfacl template can name /usr/bin/bash.
P_SHELL: Final = "SH"
#: delta-review F3 + Recovery D15 (2026-10-27): a MODE with any of these bits (setuid, setgid,
#: sticky; group or other WRITE) on a mutated path needs that exact mode planned for that
#: path, or (rollback) in its receipt preimage. The 0o022 half matches the ancestor walk.
GUARDED_MODE_BITS: Final = 0o7022


def acl_canonical(value: str) -> tuple[str, ...] | None:
    """Recovery D15 (ACL slot, 2026-10-27): the canonical form both sides of the ACL check are
    compared in. No new rule: each entry is an allowlisted ``F._ACL_RE`` shape, the
    T-5665 one-entry-per-key rule holds (key = the entry with its perms stripped, exactly
    families.py's manifest check), and the entries are SORTED, the order ``F.resolve``
    (acl.converge) and the getfacl observe parse both use. None = not canonical: refuse."""

    entries = value.split(",")
    if not all(F._ACL_RE.fullmatch(e) for e in entries):
        return None
    keys = [e.rsplit(":", 1)[0] for e in entries]  # the T-5665 key (families.py, paths.*.acl)
    if len(set(keys)) != len(keys):
        return None
    return tuple(sorted(entries))


def _template(argv: Sequence[str], mutating: bool, origin: str) -> Template:
    toks: list[tuple[re.Pattern[str], tuple[str, ...]] | str] = []
    for tok in argv:
        names = tuple((P_MUTATE if mutating else P_OBSERVE) if n == "P" else n for n in _SLOT.findall(tok))
        if not names:
            toks.append(tok)
            continue
        pattern = "".join(re.escape(part) if i % 2 == 0 else "(.+)" for i, part in enumerate(_SLOT.split(tok)))
        toks.append((re.compile(pattern, re.S), names))
    if not isinstance(toks[0], str):
        raise ValueError(f"{origin}: a template's binary must be literal")
    return Template(tuple(toks), mutating, origin)


def _family_renders() -> list[tuple[str, list[list[str]]]]:
    """Every family's ``ports.apply_argv`` / ``rollback_argv`` RENDERED with sentinels:
    the ruled functions themselves, not a parallel hand list."""

    R, s = F.Resolved, _S
    owner = {"owner": s["N"], "group": s["N"], "mode": s["MODE"]}
    acct = {"uid": s["ID"], "home": s["P"], "shell": s[P_SHELL], "grantee": s["A"]}
    out = [
        ("group.ensure", P.apply_argv(R("group.ensure", f"group:{s['G']}", {"gid": 0}, {"gid": s["ID"]}))),
        ("account.ensure", P.apply_argv(R("account.ensure", f"passwd:{s['A']}", {}, acct))),
        ("account.retire", P.apply_argv(R("account.retire", f"passwd:{s['A']}", {}, acct))),
        ("account.ensure rollback", P.rollback_argv(R("account.ensure", f"passwd:{s['A']}", None, acct), None)),
        ("account.retire rollback", P.rollback_argv(R("account.retire", f"passwd:{s['A']}", {}, acct), {})),
        ("acl.converge", P.apply_argv(R("acl.converge", f"acl:{s['P']}", {"entries": [s["ACL"]]}, {}))),
        ("owner_mode.set", P.apply_argv(R("owner_mode.set", f"owner_mode:{s['P']}", owner, {}))),
        ("mailbox_dir.ensure", P.apply_argv(R("mailbox_dir.ensure", f"dir:{s['P']}", owner, {}))),
        ("mailbox_dir.retire", P.apply_argv(R("mailbox_dir.retire", f"dir:{s['P']}", owner, {}))),
        ("linger.enable", P.apply_argv(R("linger.enable", f"linger:{s['A']}", True, {}))),
        ("linger.disable", P.apply_argv(R("linger.disable", f"linger:{s['A']}", None, {}))),
    ]
    for on in (True, None):
        out.append((f"group.member {on}", P.apply_argv(R("group.member", f"member:{s['G']}:{s['A']}", on, {}))))
        out.append((f"unit.system.enable {on}", P.apply_argv(R("unit.system.enable", f"enabled:{s['U']}", on, {}))))
    for fam in ("unit.system.install", "dbus_policy.install", "dbus_policy.retire"):
        out.append((f"{fam} present", P.apply_argv(R(fam, f"file:{s['P']}", {"sha256": "x"}, {}), staged_source=s["SRC"])))
        out.append((f"{fam} absent", P.apply_argv(R(fam, f"file:{s['P']}", None, {}))))
    out.append(("sudoers_fragment.ensure",
                P.apply_argv(R("sudoers_fragment.ensure", f"file:{s['P']}", {}, acct), staged_source=s["SRC"])))
    out.append(("sudoers_fragment.retire", P.apply_argv(R("sudoers_fragment.retire", f"file:{s['P']}", None, acct))))
    return out


def _observe_rows(observe_only: bool) -> list[Template]:
    out = []
    for argv, cls, _purpose, _kinds, reach in P.OBSERVE_ARGV_TABLE:
        if observe_only and reach != "observe()":
            continue
        out.append(_template(argv, not cls.startswith("read-only"), f"OBSERVE_ARGV_TABLE {argv[:2]}"))
    return out


def ruled_templates(observe_only: bool = False) -> list[Template]:
    """Every template the generic gate accepts. ``observe_only``: only the read-only
    ``observe()`` reach of OBSERVE_ARGV_TABLE (the observe-probe verb)."""

    out = _observe_rows(observe_only)
    if observe_only:
        return out
    for origin, argvs in _family_renders():
        out += [_template(argv, True, origin) for argv in argvs]
    # U3a's own literal (dpkg-query is verify-and-record only, Q7)
    out.append(_template([A.DPKG_QUERY, "-W", "-f", A.DPKG_FORMAT, _S["PKG"]], False, "U3a ex_package"))
    unique: dict[tuple[Any, ...], Template] = {}
    for t in out:
        key = tuple(tok if isinstance(tok, str) else tok[0].pattern for tok in t.tokens)
        unique.setdefault(key, t)
    return list(unique.values())


#: argv[0] of the three binaries that can run anything: FULL-argv templates only.
FULL_ARGV_BINARIES: Final = frozenset({H.ENV, H.PYTHON3, H.SETPRIV})


def allowed_binaries() -> frozenset[str]:
    """ALLOWED_BINARIES, DERIVED: the binaries of every ruled template plus the three
    full-argv binaries (ENV, PYTHON3, SETPRIV)."""

    return frozenset({t.binary for t in ruled_templates()} | FULL_ARGV_BINARIES)


def _ancestors(path: str) -> list[str]:
    parts = path.strip("/").split("/")
    return ["/" + "/".join(parts[:i]) for i in range(len(parts) + 1)]


class ArgvAllowlist:
    """The gate, bound to ONE attested plan. Every slot is bound to the plan/manifest.

    Contextual slots (review F3), keyed by the template's ONE P_MUTATE path:

    * N (owner / group in chown, install): a manifest account or group, or root; in a
      ROLLBACK gate also a name recorded in THAT path's receipt preimage.
    * MODE: no setuid / setgid / sticky bit and no group / other write
      (``GUARDED_MODE_BITS``) unless that exact mode is the plan's for that path (or, in a
      rollback gate, that path's receipt preimage mode)."""

    def __init__(self, templates: Sequence[Template], slots: Mapping[str, Callable[[str], bool]],
                 exact: Mapping[tuple[str, ...], str | None], *, names: Iterable[str] = (),
                 modes: Mapping[str, Iterable[int]] | None = None,
                 preimage_names: Mapping[str, Iterable[str]] | None = None,
                 acls: Mapping[str, Iterable[str]] | None = None):
        self._names = frozenset(names)
        #: delta-review M1: path -> the EXACT setfacl --set values allowed on it
        self._acls = {k: frozenset(c for c in map(acl_canonical, v) if c is not None) for k, v in (acls or {}).items()}
        self._modes = {k: frozenset(v) for k, v in (modes or {}).items()}
        self._preimage_names = {k: frozenset(v) for k, v in (preimage_names or {}).items()}
        self._templates = list(templates)
        self._by_binary: dict[str, list[Template]] = {}
        for t in self._templates:
            self._by_binary.setdefault(t.binary, []).append(t)
        self._slots = dict(slots)
        #: full argv -> the ONLY cwd it may run with (ENV build line, PYTHON3 probe, SETPRIV)
        self._exact = dict(exact)

    # --- construction ----------------------------------------------------------

    @staticmethod
    def _manifest_slots(fmanifest: F.Manifest, extra_paths: Iterable[str] = ()) -> dict[str, Callable[[str], bool]]:
        accounts = set(fmanifest.section("accounts"))
        groups = set(fmanifest.section("groups"))
        ids = {str(a["uid"]) for a in fmanifest.section("accounts").values()}
        ids |= {str(g["gid"]) for g in fmanifest.section("groups").values()}
        units = {posixpath.basename(u["path"]) for u in fmanifest.section("units").values()}
        observe_paths: set[str] = set()
        mutate_paths: set[str] = set()
        for subject in P.manifest_subjects(fmanifest):
            kind, key = subject.split(":", 1)
            if kind in ("owner_mode", "dir", "file", "acl"):
                observe_paths |= set(_ancestors(key))
                mutate_paths.add(key)
        shells: set[str] = set()
        for a in fmanifest.section("accounts").values():
            observe_paths |= {a["home"], a["shell"]} | set(_ancestors(a["home"]))
            mutate_paths.add(a["home"])  # delta-review L1: homes stay, shells leave P_MUTATE
            shells.add(a["shell"])
        for p in extra_paths:
            observe_paths |= set(_ancestors(p))
        return {
            "A": accounts.__contains__,
            "G": (groups | accounts).__contains__,
            P_OBSERVE: observe_paths.__contains__,
            P_MUTATE: mutate_paths.__contains__,
            P_SHELL: shells.__contains__,
            "U": units.__contains__,
            "N": lambda v: bool(F._NAME_RE.fullmatch(v)),
            "ID": ids.__contains__,
            "MODE": lambda v: bool(re.fullmatch(r"[0-7]{4,5}", v)),
            "ACL": lambda v: bool(v) and all(F._ACL_RE.fullmatch(e) for e in v.split(",")),
            "PKG": lambda v: v in A.VERIFY_PACKAGES,
        }

    @classmethod
    def observe_only(cls, fmanifest: F.Manifest) -> "ArgvAllowlist":
        return cls(ruled_templates(observe_only=True), cls._manifest_slots(fmanifest), {})

    @classmethod
    def for_plan(cls, plan: Mapping[str, Any], fmanifest: F.Manifest, wheelhouse: str, rcd: str, *,
                 rollback_preimages: Mapping[str, Mapping[str, Any]] | None = None) -> "ArgvAllowlist":
        """The apply / verify / rollback gate for ONE attested plan. ``rollback_preimages``
        (rollback only; ``rollback_preimages_of``): path -> {"names", "modes"} from the
        attested receipt's family preimages."""

        fw = plan["framework"]["root"]
        names = {"root"} | set(fmanifest.section("accounts")) | set(fmanifest.section("groups"))
        modes: dict[str, set[int]] = {}
        acls: dict[str, set[str]] = {}
        for e in plan["entries"]:
            src = e["source"]
            if src.get("route") != "family":
                continue
            r = F.resolve(src["family"], dict(src["params"]), fmanifest)
            if isinstance(r.after, dict) and isinstance(r.after.get("mode"), str):
                modes.setdefault(r.subject.split(":", 1)[1], set()).add(int(r.after["mode"], 8))
            if r.family == "acl.converge" and r.after["entries"]:  # M1: the exact value apply_argv renders
                acls.setdefault(r.subject.split(":", 1)[1], set()).add(",".join(r.after["entries"]))
        pre_names: dict[str, set[str]] = {}
        for path, rec in (rollback_preimages or {}).items():
            pre_names[path] = set(rec.get("names", ()))
            modes.setdefault(path, set()).update(rec.get("modes", ()))
            acls.setdefault(path, set()).update(rec.get("acls", ()))
        slots = cls._manifest_slots(fmanifest)
        staged_prefixes = (rcd + "/substrate-staging/", rcd + "/saved/")

        def src(v: str) -> bool:
            try:
                P._path("staged source", v)
            except P.ObservationError:
                return False
            return v.startswith(staged_prefixes)

        slots["SRC"] = slots["STAGED"] = src
        exact: dict[tuple[str, ...], str | None] = {tuple(A.PYYAML_PROBE): None}
        for key in H.RUNTIME_ADAPTERS:  # the U3a build line (ex_runtime), exactly
            exact[("/usr/bin/env", "-i", *A.BUILD_ENV, H.PYTHON3, "-I", posixpath.join(fw, H.BUILD_RUNTIME_REL),
                   "--root", "/", "--wheelhouse", wheelhouse, "--python", H.PYTHON3, "--adapter", key)] = fw
        if SETPRIV_TEMPLATE_RULED:
            for e in plan["entries"]:
                if e["kind"] != "plan_registration":
                    continue
                argv = tuple(e["source"]["producer_argv"])
                if not producer_argv_matches_entry(argv, e, fw):
                    raise ArgvRefused(f"{e['id']}: the plan's producer argv is not the RULED setpriv template")
                exact[argv] = e["source"]["producer_cwd"]
        return cls(ruled_templates(), slots, exact, names=names, modes=modes, preimage_names=pre_names, acls=acls)

    # --- the gate ----------------------------------------------------------------

    def classify(self, argv: Sequence[str], cwd: str | None) -> bool:
        """Raise ArgvRefused unless ``argv`` (with ``cwd``) is allowed; return whether it
        MUTATES (False = read-only observation)."""

        argv = list(argv)
        if not argv or not all(isinstance(a, str) and "\x00" not in a for a in argv):
            raise ArgvRefused("argv must be a non-empty list of NUL-free strings")
        key = tuple(argv)
        if argv[0] in FULL_ARGV_BINARIES:
            if argv[0] == H.SETPRIV and not SETPRIV_TEMPLATE_RULED:
                raise ArgvRefused("setpriv: the producer template is not ruled")
            if key not in self._exact:
                raise ArgvRefused(f"{argv[0]}: only a RULED full-argv template may run (ENV / PYTHON3 / SETPRIV)")
            if cwd != self._exact[key]:
                raise ArgvRefused(f"{argv[0]}: cwd {cwd!r} is not the template's")
            return key != tuple(A.PYYAML_PROBE)
        if cwd is not None:
            raise ArgvRefused(f"{argv[0]}: the ruled family / observe argv run with no cwd")
        for t in self._by_binary.get(argv[0], ()):
            if self._matches(t, argv):
                return t.mutating
        raise ArgvRefused(f"{argv[0]}: argv matches no RULED template bound to this plan")

    def _matches(self, t: Template, argv: Sequence[str]) -> bool:
        if len(t.tokens) != len(argv):
            return False
        values: dict[str, list[str]] = {}
        for tok, value in zip(t.tokens, argv):
            if isinstance(tok, str):
                if tok != value:
                    return False
                continue
            pattern, names = tok
            m = pattern.fullmatch(value)
            if m is None:
                return False
            for name, part in zip(names, m.groups()):
                check = self._slots.get(name)
                if check is None or not check(part):
                    return False
                values.setdefault(name, []).append(part)
        return self._context_ok(values)

    def _context_ok(self, values: Mapping[str, Sequence[str]]) -> bool:
        """Review F3 (+ Recovery D15 g/o-write): N and MODE against the template's ONE mutated path."""

        paths = values.get(P_MUTATE, [])
        path = paths[0] if len(paths) == 1 else None
        for name in values.get("N", ()):
            if name not in self._names and (path is None or name not in self._preimage_names.get(path, ())):
                return False
        for mode in values.get("MODE", ()):
            bits = int(mode, 8)
            if bits & GUARDED_MODE_BITS and (path is None or bits not in self._modes.get(path, ())):
                return False
        # delta-review M1 (mason ruling, in the spirit of Recovery's D15 MODE rule): an ACL
        # value equals the plan's (or, rollback, the preimage's) for its one path.
        # N1: with no single mutated path, ANY ACL refuses (fail closed; every ruled
        # setfacl template carries exactly one P_MUTATE path, pinned by a test).
        # Recovery D15 (ACL slot): compared AFTER the T-5665 canonicalisation, both sides.
        # STANDING RULE (Recovery): if a real plan ever needs an unplanned setfacl form (e.g.
        # -x / -b in rollback), bring that template to Recovery under D15; never widen here.
        for acl in values.get("ACL", ()):
            c = acl_canonical(acl)
            if path is None or c is None or c not in self._acls.get(path, ()):
                return False
        return True


def rollback_preimages_of(doc: Mapping[str, Any], attestation: "RehearsalAttestation") -> dict[str, dict[str, Any]]:
    """Review F3 plumbing: path -> {"names", "modes", "acls"} from the family preimages of THIS
    machine's attested rehearsal receipt (the names / modes a rollback restores). Any
    other receipt yields nothing (``A.rollback`` then refuses it on its mode anyway)."""

    ident = {"machine": attestation.machine, "run_id": attestation.run_id, "nonce_sha256": attestation.nonce_sha256}
    if doc.get("mode") != A.REHEARSAL_MODE or doc.get("rehearsal") != ident:
        return {}
    if doc.get("plan_sha256") != attestation.plan_sha256:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in doc.get("entries") or []:
        fam = r.get("family") if isinstance(r, dict) else None
        pre = fam.get("preimage") if isinstance(fam, dict) else None
        subject = fam.get("subject") if isinstance(fam, dict) else None
        if not (isinstance(pre, dict) and isinstance(subject, str) and ":" in subject):
            continue
        rec = out.setdefault(subject.split(":", 1)[1], {"names": set(), "modes": set(), "acls": set()})
        ents = pre.get("entries")
        if (subject.startswith("acl:") and isinstance(ents, list) and ents
                and all(isinstance(x, str) and F._ACL_RE.fullmatch(x) for x in ents)):
            rec["acls"].add(",".join(ents))  # M1: exactly what the restore's setfacl --set renders
        for k in ("owner", "group"):
            if isinstance(pre.get(k), str) and F._NAME_RE.fullmatch(pre[k]):
                rec["names"].add(pre[k])
        if isinstance(pre.get("mode"), str) and re.fullmatch(r"[0-7]{4}", pre["mode"]):
            rec["modes"].add(int(pre["mode"], 8))
    return out


def receipt_file_refusal(st: os.stat_result) -> str | None:
    """Delta-review L2: the receipt (or its progress journal) must look like
    attestation.json does: a regular file, uid 0 and gid 0, no group / other write."""

    if not stat.S_ISREG(st.st_mode):
        return "not a regular file"
    if st.st_uid != 0 or st.st_gid != 0:
        return f"owned {st.st_uid}:{st.st_gid}, not 0:0"
    if st.st_mode & 0o022:
        return f"mode {stat.S_IMODE(st.st_mode):04o} is group/other writable"
    return None


def _read_root_owned(path: str) -> bytes:
    """O_RDONLY|O_NOFOLLOW, then ``receipt_file_refusal`` on the SAME fd, then the bytes."""

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise A.ApplyRefused(f"{path}: unreadable without following a symlink ({exc.strerror})") from None
    try:
        why = receipt_file_refusal(os.fstat(fd))
        if why is not None:
            raise A.ApplyRefused(f"{path}: {why}")
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


#: delta-review L3: every way a malformed receipt / journal fails to load or parse
RECEIPT_ERRORS: Final = (A.ApplyRefused, OSError, ValueError, AttributeError, TypeError, IndexError, KeyError)


def rollback_receipt_preimages(host: Any, receipt: str, attestation: "RehearsalAttestation") -> dict[str, dict[str, Any]]:
    """Read-only. Pin the receipt and its progress journal (a present one: ownership
    checked on the fd, sha recorded; an absent one: pinned ABSENT, L5), load it exactly as ``A.rollback`` will (its read
    is compared to the pin, so both reads see the same bytes), and return its
    preimages. Raises one of ``RECEIPT_ERRORS`` on a malformed or unowned receipt."""

    for p in (receipt, posixpath.join(receipt + ".d", "progress.jsonl")):
        host.pin(p)  # L5: an absent path is pinned absent, so it cannot appear unverified
    doc = A._load_receipt(host, receipt)
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), list):
        raise A.ApplyRefused("receipt: not an object with an entries list")
    return rollback_preimages_of(doc, attestation)


def producer_argv_matches_entry(argv: Sequence[str], e: Mapping[str, Any], fw: str) -> bool:
    """Recovery D15 RULED setpriv template, bound to the plan entry: the U3a shape
    (``producer_argv_shape_ok``: the three hardening flags in order), the framework root,
    the two JSON args equal to the entry's own records, and --out equal to the entry's
    ``producer_writes``."""

    argv = list(argv)
    if not A.producer_argv_shape_ok(argv) or argv[15] != fw or e["source"].get("producer_cwd") != fw:
        return False
    user, group = e["inputs"]["expected_writer"].split(":")
    if argv[1] != f"--reuid={user}" or argv[2] != f"--regid={group}":
        return False
    try:
        seat, record = json.loads(argv[17]), json.loads(argv[19])
    except ValueError:
        return False
    if H.canonical(seat) != argv[17] or H.canonical(record) != argv[19]:
        return False
    seat_id, revision = e["id"].split(":")[1:3]
    if seat.get("seat_id") != seat_id or record.get("revision") != revision:
        return False
    return argv[21] == e["inputs"]["producer_writes"]


# ---------------------------------------------------------------------------
# OperatorRunner: the ONE subprocess call
# ---------------------------------------------------------------------------


def _subprocess_kwargs(argv: Sequence[str], cwd: str | None) -> dict[str, Any]:
    args = list(argv)
    return {"args": args, "cwd": cwd, "env": dict(REQUIRED_ENV), "stdin": subprocess.DEVNULL,
            "capture_output": True, "text": True, "errors": "replace", "check": False, "close_fds": True,
            "shell": False, "timeout": BUILD_TIMEOUT if args[:1] == [H.ENV] else DEFAULT_TIMEOUT}


class OperatorRunner:
    """The attested runner. recheck, then the full-argv gate, then exactly one
    ``subprocess.run(**_subprocess_kwargs(argv, cwd))``."""

    def __init__(self, attestation: RehearsalAttestation, allowlist: ArgvAllowlist,
                 mutation_log: list[tuple[str, str]] | None = None):
        if not isinstance(attestation, RehearsalAttestation):
            raise AttestationRefused(["OperatorRunner: needs a loaded RehearsalAttestation"])
        if not isinstance(allowlist, ArgvAllowlist):
            raise ArgvRefused("OperatorRunner: needs an ArgvAllowlist")
        self._att = attestation
        self._allow = allowlist
        self.mutations = mutation_log if mutation_log is not None else []
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: Sequence[str], *, cwd: str | None = None) -> CommandResult:
        self._att.recheck()
        if self._allow.classify(argv, cwd):
            self.mutations.append(("exec:" + argv[0], argv[-1]))
        self.calls.append(tuple(argv))
        try:
            done = subprocess.run(**_subprocess_kwargs(argv, cwd))
        except subprocess.TimeoutExpired:
            return CommandResult(124, "", f"{argv[0]}: timed out")
        except OSError as exc:
            return CommandResult(127, "", f"{argv[0]}: {exc.strerror}")
        return CommandResult(done.returncode, done.stdout, done.stderr)


# ---------------------------------------------------------------------------
# live primitives (module-level, rooted; root '/' only with the key) and LiveHost
# ---------------------------------------------------------------------------

Hook = Callable[[str, str], None] | None


def _root(root: str, key: object) -> Path:
    """mason Q3 pin: a primitive at the REAL root needs the private key (LiveHost only)."""

    real = os.path.realpath(root)
    if real == "/" and key is not _KEY:
        raise LiveRefused("live primitive: root '/' needs the attested LiveHost")
    return Path(real)


def _p_path(root: str, host_path: str, *, key: object = None) -> Path:
    """Re-root ``host_path``; refuse a symlink in any ANCESTOR component (U3a)."""

    A._norm_host_path(host_path)
    base = _root(root, key)
    parts = [p for p in host_path.strip("/").split("/") if p]
    current = base
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise A.ApplyRefused(f"{host_path}: symlink component {current}")
    return base.joinpath(*parts)


def _p_wpath(root: str, host_path: str, *, allow_leaf_symlink: bool = False, key: object = None) -> Path:
    """U3a MED-3: also refuse a LEAF symlink for a write or meta op."""

    p = _p_path(root, host_path, key=key)
    if not allow_leaf_symlink and p.is_symlink():
        raise LiveRefused(f"{host_path}: a symlink at the leaf is refused for a write or meta op")
    return p


def _ids(owner: str, group: str) -> tuple[int, int]:
    try:
        return pwd.getpwnam(owner).pw_uid, grp.getgrnam(group).gr_gid
    except KeyError:
        raise LiveRefused(f"{owner}:{group}: not a resolvable account/group") from None


def _names(st: os.stat_result) -> tuple[str, str]:
    try:
        return pwd.getpwuid(st.st_uid).pw_name, grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        raise LiveRefused(f"uid {st.st_uid} / gid {st.st_gid}: not resolvable (refused)") from None


def _fd_meta(fd: int, owner: str, group: str, mode: str) -> None:
    uid, gid = _ids(owner, group)
    os.fchown(fd, uid, gid)
    os.fchmod(fd, int(mode, 8))


def _hook(hook: Hook, op: str, host_path: str) -> None:
    if hook is not None:
        hook(op, host_path)


def _p_read_bytes(root: str, host_path: str, *, key: object = None) -> bytes:
    fd = os.open(_p_path(root, host_path, key=key), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _p_observe(root: str, host_path: str, *, hash_content: bool = True, key: object = None) -> dict[str, Any]:
    p = _p_path(root, host_path, key=key)
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return {"exists": False}
    owner, group = _names(st)
    out: dict[str, Any] = {"exists": True, "owner": owner, "group": group, "mode": A._mode_str(st.st_mode)}
    if stat.S_ISLNK(st.st_mode):
        out.update(type="symlink", link_target=os.readlink(p))
        out.pop("mode")
    elif stat.S_ISDIR(st.st_mode):
        out["type"] = "dir"
    elif stat.S_ISREG(st.st_mode):
        out["type"] = "file"
        if hash_content:
            out.update(sha256=sha256_bytes(_p_read_bytes(root, host_path, key=key)), size=st.st_size)
    else:
        out["type"] = "other"
    return out


def _p_mkdir(root: str, host_path: str, owner: str, group: str, mode: str, *, key: object = None,
             hook: Hook = None) -> None:
    p = _p_wpath(root, host_path, key=key)
    if p.parent.is_symlink() or not p.parent.is_dir():
        raise LiveRefused(f"{host_path}: parent directory is missing")
    _ids(owner, group)
    _hook(hook, "mkdir", host_path)
    os.mkdir(p, 0o700)
    fd = os.open(p, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        _fd_meta(fd, owner, group, mode)
    finally:
        os.close(fd)


def _p_write_file(root: str, host_path: str, data: bytes, owner: str, group: str, mode: str, *,
                  key: object = None, hook: Hook = None) -> None:
    """An O_EXCL temp sibling, fchown/fchmod/fsync on the fd, then one rename."""

    p = _p_wpath(root, host_path, key=key)
    if p.parent.is_symlink() or not p.parent.is_dir():
        raise LiveRefused(f"{host_path}: parent directory is missing")
    if p.is_symlink() or (p.exists() and not p.is_file()):
        raise LiveRefused(f"{host_path}: refusing to replace a non-regular file")
    _ids(owner, group)
    _hook(hook, "write", host_path)
    tmp = p.parent / f".{p.name}.pantheon-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        _fd_meta(fd, owner, group, mode)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(tmp)
        raise
    os.close(fd)
    os.replace(tmp, p)


def _p_chown(root: str, host_path: str, owner: str, group: str, *, key: object = None, hook: Hook = None) -> None:
    p = _p_wpath(root, host_path, key=key)
    if not os.path.lexists(p):
        raise LiveRefused(f"{host_path}: chown of a missing path")
    uid, gid = _ids(owner, group)
    _hook(hook, "chown", host_path)
    os.chown(p, uid, gid, follow_symlinks=False)


def _p_chmod(root: str, host_path: str, mode: str, *, key: object = None, hook: Hook = None) -> None:
    p = _p_wpath(root, host_path, key=key)
    _hook(hook, "chmod", host_path)
    fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fchmod(fd, int(mode, 8))
    finally:
        os.close(fd)


def _p_unlink(root: str, host_path: str, *, allow_leaf_symlink: bool = False, key: object = None,
              hook: Hook = None) -> None:
    p = _p_wpath(root, host_path, allow_leaf_symlink=allow_leaf_symlink, key=key)
    _hook(hook, "unlink", host_path)
    os.unlink(p)


def _p_rmdir(root: str, host_path: str, *, key: object = None, hook: Hook = None) -> None:
    p = _p_wpath(root, host_path, key=key)
    _hook(hook, "rmdir", host_path)
    os.rmdir(p)


def _p_rename(root: str, src_host: str, dst_host: str, *, key: object = None, hook: Hook = None) -> None:
    src, dst = _p_wpath(root, src_host, key=key), _p_wpath(root, dst_host, key=key)
    if os.path.lexists(dst):
        raise LiveRefused(f"{dst_host}: rename target exists; refused")
    _hook(hook, "rename", src_host)
    _hook(hook, "rename", dst_host)
    os.rename(src, dst)


def _p_symlink_replace(root: str, host_path: str, target: str, *, key: object = None, hook: Hook = None) -> None:
    p = _p_wpath(root, host_path, allow_leaf_symlink=True, key=key)
    if os.path.lexists(p) and not p.is_symlink():
        raise LiveRefused(f"{host_path}: exists and is not a symlink; refusing to replace it")
    tmp_host = posixpath.join(posixpath.dirname(host_path), f".{posixpath.basename(host_path)}.pantheon-tmp")
    tmp = _p_path(root, tmp_host, key=key)
    if os.path.lexists(tmp):
        raise LiveRefused(f"{tmp_host}: a stale temp link exists")
    _hook(hook, "symlink", tmp_host)
    os.symlink(target, tmp)
    try:
        _hook(hook, "replace", host_path)
        os.replace(tmp, p)
    except BaseException:
        os.unlink(tmp)
        raise


def _p_remove_tree_contained(root: str, host_path: str, base: str, *, key: object = None, hook: Hook = None) -> None:
    if posixpath.dirname(host_path) != base:
        raise LiveRefused(f"{host_path}: tree removal is only allowed for a direct child of {base}")
    top = _p_path(root, host_path, key=key)
    if top.is_symlink() or not top.is_dir():
        raise LiveRefused(f"{host_path}: not a real directory; refusing tree removal")
    _hook(hook, "remove_tree", host_path)
    for dirpath, dirnames, filenames in os.walk(top, topdown=False, followlinks=False):
        for name in filenames:
            os.unlink(os.path.join(dirpath, name))
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                os.unlink(full)
            else:
                os.rmdir(full)
    os.rmdir(top)


#: every rooted primitive (the Q3 pin calls each one at root '/' without the key)
PRIMITIVES: Final = ("_p_path", "_p_wpath", "_p_read_bytes", "_p_observe", "_p_mkdir", "_p_write_file", "_p_chown",
                     "_p_chmod", "_p_unlink", "_p_rmdir", "_p_rename", "_p_symlink_replace",
                     "_p_remove_tree_contained")


class LiveHost:
    """FixtureHost's method surface at the REAL '/'. Ownership is real (lstat via
    pwd/grp, os.fchown); every write/meta op refuses ancestor and leaf symlinks (U3a
    MED-3); ``recheck`` runs before EVERY mutation (``_mutate`` / ``precheck``)."""

    def __init__(self, attestation: RehearsalAttestation):
        if not isinstance(attestation, RehearsalAttestation):
            raise AttestationRefused(["LiveHost: needs a loaded RehearsalAttestation"])
        self._att = attestation
        self.root = Path("/")
        self.mutations: list[tuple[str, str]] = []
        self.exempt: tuple[str, ...] = ()
        self.faults: frozenset[tuple[str, str]] = frozenset()
        self.race_hook: Callable[[], None] | None = None
        #: delta-review L2 / L5: host path -> sha256 of the bytes ``pin`` read, or None (pinned ABSENT)
        self._pins: dict[str, str | None] = {}

    # --- attestation ------------------------------------------------------------

    def precheck(self) -> None:
        self._att.recheck()

    def _mutate(self, op: str, host_path: str) -> None:
        self._att.recheck()
        if not any(host_path == p or host_path.startswith(p) for p in self.exempt):
            self.mutations.append((op, host_path))

    # --- paths / observation ------------------------------------------------------

    def path(self, host_path: str) -> Path:
        p = _p_path("/", host_path, key=_KEY)
        self._check_absent(host_path, p)
        return p

    def _check_absent(self, host_path: str, p: Path) -> None:
        """Delta-review L5: a path pinned ABSENT must stay absent at every later
        path / exists / is_file / read through this host."""

        if host_path in self._pins and self._pins[host_path] is None and os.path.lexists(p):
            raise A.ApplyRefused(f"{host_path}: appeared after it was pinned absent")

    def wpath(self, host_path: str, *, allow_leaf_symlink: bool = False) -> Path:
        self._check_absent(host_path, _p_path("/", host_path, key=_KEY))  # N2
        return _p_wpath("/", host_path, allow_leaf_symlink=allow_leaf_symlink, key=_KEY)

    def observe(self, host_path: str, *, hash_content: bool = True) -> dict[str, Any]:
        self._check_absent(host_path, _p_path("/", host_path, key=_KEY))  # N2
        return _p_observe("/", host_path, hash_content=hash_content, key=_KEY)

    def read_bytes(self, host_path: str) -> bytes:
        self._check_absent(host_path, _p_path("/", host_path, key=_KEY))
        data = _p_read_bytes("/", host_path, key=_KEY)
        want = self._pins.get(host_path)
        if want is not None and sha256_bytes(data) != want:  # delta-review L2: the double read
            raise A.ApplyRefused(f"{host_path}: changed since it was pinned (two reads, different bytes)")
        return data

    def pin(self, host_path: str) -> None:
        """Delta-review L2: read once through ``_read_root_owned`` and record the sha;
        every later ``read_bytes`` of this path must return the same bytes. L5: an
        absent path is pinned ABSENT (None) and must stay absent."""

        p = _p_path("/", host_path, key=_KEY)
        if not os.path.lexists(p):
            self._pins[host_path] = None
            return
        self._pins[host_path] = sha256_bytes(_read_root_owned(str(p)))

    def exists(self, host_path: str) -> bool:
        return os.path.lexists(self.path(host_path))

    def owner_of(self, host_path: str) -> tuple[str, str]:
        return _names(os.lstat(self.path(host_path)))

    def uid_of(self, name: str) -> int:
        try:
            return pwd.getpwnam(name).pw_uid
        except KeyError:
            raise LiveRefused(f"{name}: not an account") from None

    def owner_uid(self, host_path: str) -> int | None:
        try:
            return os.lstat(self.path(host_path)).st_uid
        except FileNotFoundError:
            return None

    def passwd(self) -> dict[str, dict[str, Any]]:
        return {p.pw_name: {"uid": p.pw_uid, "gid": p.pw_gid, "home": p.pw_dir, "shell": p.pw_shell}
                for p in pwd.getpwall()}

    def groups(self) -> dict[str, int]:
        return {g.gr_name: g.gr_gid for g in grp.getgrall()}

    def shadow_field(self, name: str) -> str | None:
        """Recovery D15 row-2 discipline: the hash never leaves here. Returns '!' or '*'
        (locked), the sentinel '<set>' for anything else, or None (no entry)."""

        for line in self.read_bytes("/etc/shadow").decode("utf-8", "replace").splitlines():
            fields = line.split(":")
            if fields[0] == name and len(fields) > 1:
                return fields[1] if fields[1] in ("!", "*") else "<set>"
        return None

    # --- mutations (recheck first, via the hook) --------------------------------------

    def fchown(self, fd: int, host_path: str, owner: str, group: str) -> None:
        uid, gid = _ids(owner, group)
        self._mutate("fchown", host_path)
        os.fchown(fd, uid, gid)

    def mkdir(self, host_path: str, owner: str, group: str, mode: str) -> None:
        _p_mkdir("/", host_path, owner, group, mode, key=_KEY, hook=self._mutate)

    def write_file(self, host_path: str, data: bytes, owner: str, group: str, mode: str) -> None:
        _p_write_file("/", host_path, data, owner, group, mode, key=_KEY, hook=self._mutate)

    def copy_file(self, src_host: str, dst_host: str, owner: str, group: str, mode: str) -> None:
        self.write_file(dst_host, self.read_bytes(src_host), owner, group, mode)

    def chown(self, host_path: str, owner: str, group: str) -> None:
        _p_chown("/", host_path, owner, group, key=_KEY, hook=self._mutate)

    def chmod(self, host_path: str, mode: str) -> None:
        _p_chmod("/", host_path, mode, key=_KEY, hook=self._mutate)

    def unlink(self, host_path: str, *, allow_leaf_symlink: bool = False) -> None:
        _p_unlink("/", host_path, allow_leaf_symlink=allow_leaf_symlink, key=_KEY, hook=self._mutate)

    def rmdir(self, host_path: str) -> None:
        _p_rmdir("/", host_path, key=_KEY, hook=self._mutate)

    def rename(self, src_host: str, dst_host: str) -> None:
        _p_rename("/", src_host, dst_host, key=_KEY, hook=self._mutate)

    def symlink_replace(self, host_path: str, target: str) -> None:
        _p_symlink_replace("/", host_path, target, key=_KEY, hook=self._mutate)

    def remove_tree_contained(self, host_path: str, base: str) -> None:
        _p_remove_tree_contained("/", host_path, base, key=_KEY, hook=self._mutate)

    # --- fixture-only surface: refused ---------------------------------------------------

    def _fixture_only(self, *_args: Any, **_kwargs: Any) -> Any:
        raise LiveRefused("fixture-only operation (the live machine routes it through the port)")

    enabled = set_enabled = facts = _table = _write_table = _fixture_only


# ---------------------------------------------------------------------------
# the port and the context
# ---------------------------------------------------------------------------


class RehearsalSubstratePort(P._LiveSubstrateOps):
    """``ports._LiveSubstrateOps`` (the live bodies, verbatim) behind the attestation:
    every host contact goes through the attested OperatorRunner (recheck + gate)."""

    def __init__(self, attestation: RehearsalAttestation, runner: OperatorRunner, manifest: F.Manifest,
                 staged: Mapping[str, str] | None):
        if not isinstance(attestation, RehearsalAttestation):
            raise AttestationRefused(["RehearsalSubstratePort: needs a loaded RehearsalAttestation"])
        if not isinstance(runner, OperatorRunner) or runner._att is not attestation:
            raise AttestationRefused(["RehearsalSubstratePort: needs THIS attestation's OperatorRunner"])
        self._runner = runner
        self._manifest = manifest
        self._subjects = P.manifest_subjects(manifest)
        self._staged_sources = dict(staged or {})
        self._staged: dict[str, Any] = {}


@dataclass(frozen=True)
class RehearsalContext:
    """What the U3a seam (``apply`` / ``verify`` / ``rollback``) receives."""

    attestation: RehearsalAttestation
    host: LiveHost
    runner: OperatorRunner
    port: RehearsalSubstratePort

    def __post_init__(self) -> None:
        if not (isinstance(self.attestation, RehearsalAttestation) and isinstance(self.host, LiveHost)
                and isinstance(self.runner, OperatorRunner) and isinstance(self.port, RehearsalSubstratePort)):
            raise AttestationRefused(["RehearsalContext: wrong component types"])
        if self.host._att is not self.attestation or self.runner._att is not self.attestation:
            raise AttestationRefused(["RehearsalContext: components from different attestations"])


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

VERBS: Final = ("apply", "verify", "rollback", "guard-probe", "observe-probe")
OBSERVE_ONLY_VERBS: Final = ("guard-probe", "observe-probe")


@dataclass(frozen=True)
class Rebuilt:
    plan_raw: bytes
    manifest_raw: bytes
    registry_raw: bytes
    tracked_raw: bytes
    inputs_raw: bytes
    plan: dict[str, Any]
    inputs: A.Inputs


def _rebuild(args: argparse.Namespace) -> Rebuilt:
    """Read the inputs (copied into the machine at R2) and rebuild the plan exactly as
    U3a does. Pure reads: nothing is constructed and nothing on the host is touched."""

    raws = [Path(getattr(args, k)).read_bytes() for k in ("plan", "manifest", "registry", "tracked_files", "inputs")]
    plan = A._plan_doc(raws[0])
    doc = A.rebuild_plan(plan, raws[1], raws[2], raws[3])[0]
    return Rebuilt(*raws, plan=doc, inputs=A.parse_inputs(raws[4]))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("verb", choices=VERBS)
    parser.add_argument("--nonce-sha256", required=True)
    for flag in ("--plan", "--manifest", "--registry", "--tracked-files", "--inputs", "--receipt"):
        parser.add_argument(flag, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--expect-started", action="store_true")
    return parser


def _emit(doc: Mapping[str, Any]) -> None:
    print(H.canonical(dict(doc)))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    rebuilt: Rebuilt | None = None
    rebuild_error = None
    try:
        rebuilt = _rebuild(args)
    except (A.ApplyRefused, H.InstallPlanError, OSError, ValueError) as exc:
        rebuild_error = f"plan rebuild: {type(exc).__name__}: {exc}"
    try:
        attestation = RehearsalAttestation.load(args.nonce_sha256, rebuilt.plan["plan_sha256"] if rebuilt else None)
    except AttestationRefused as exc:
        reasons = exc.reasons + ([rebuild_error] if rebuild_error else [])
        _emit({"verb": args.verb, "verdict": "refused", "reasons": reasons})
        return 3
    # ---- below here: an attested machine, for the attested plan ----
    assert rebuilt is not None
    if os.getcwd() != REPO_BIND:
        _emit({"verb": args.verb, "verdict": "refused", "reasons": [f"cwd must be {REPO_BIND}"]})
        return 3
    identity = {"machine": attestation.machine, "run_id": attestation.run_id,
                "nonce_sha256": attestation.nonce_sha256, "plan_sha256": attestation.plan_sha256}
    if args.verb == "guard-probe":
        _emit({"verb": args.verb, "verdict": "attested", **identity})
        return 0
    fmanifest = F.load_manifest(A.fragment_from_plan(rebuilt.plan))
    if args.verb == "observe-probe":
        # OBSERVE-ONLY: no LiveHost, no context; the runner's gate admits ONLY the
        # read-only observe() rows, and only the port's get (ports.observe) is called
        runner = OperatorRunner(attestation, ArgvAllowlist.observe_only(fmanifest))
        probe = RehearsalSubstratePort(attestation, runner, fmanifest, {})
        for subject in sorted(P.manifest_subjects(fmanifest)):
            try:
                _emit({"subject": subject, "value": probe.get(subject)})
            except P.ObservationError as exc:
                _emit({"subject": subject, "error": str(exc)})
        return 0
    if not _RECEIPT_RE.fullmatch(args.receipt):
        _emit({"verb": args.verb, "verdict": "refused", "reasons": [f"--receipt must sit directly in {RECEIPT_DIR}"]})
        return 2
    host = LiveHost(attestation)
    preimages = {}
    if args.verb == "rollback":  # review F3: read-only; the receipt's own preimages widen N / MODE per path
        try:
            preimages = rollback_receipt_preimages(host, args.receipt, attestation)
        except RECEIPT_ERRORS as exc:  # delta-review L3: a malformed receipt refuses, never crashes
            _emit({"verb": args.verb, "verdict": "refused", "reasons": [f"receipt: {type(exc).__name__}: {exc}"]})
            return 2
    allow = ArgvAllowlist.for_plan(rebuilt.plan, fmanifest, rebuilt.inputs.wheelhouse, args.receipt + ".d",
                                   rollback_preimages=preimages)
    runner = OperatorRunner(attestation, allow, mutation_log=host.mutations)
    port = RehearsalSubstratePort(attestation, runner, fmanifest, {})
    ctx = RehearsalContext(attestation, host, runner, port)
    try:
        if args.verb == "apply":
            outcome = A.apply(plan_raw=rebuilt.plan_raw, manifest_raw=rebuilt.manifest_raw,
                              registry_raw=rebuilt.registry_raw, tracked_raw=rebuilt.tracked_raw,
                              inputs_raw=rebuilt.inputs_raw, root="/", receipt=args.receipt, execute=args.execute,
                              rehearsal=ctx)
            if outcome.receipt is None:
                for a in outcome.actions:
                    _emit({k: a[k] for k in ("seq", "id", "kind", "action", "argv")})
            else:
                _emit({"status": outcome.receipt["status"], "receipt": args.receipt, "mode": outcome.receipt["mode"],
                       "receipt_sha256": outcome.receipt["receipt_sha256"],
                       "failed_entry": outcome.receipt["failed_entry"], **identity})
            return outcome.rc
        if args.verb == "verify":
            receipt_raw = host.read_bytes(args.receipt) if host.exists(args.receipt) else None
            rc, report = A.verify(plan_raw=rebuilt.plan_raw, root="/", receipt_raw=receipt_raw,
                                  expect_started=args.expect_started, rehearsal=ctx)
            for row in report:
                _emit(row)
            return rc
        rc, out = A.rollback(receipt=args.receipt, root="/", rehearsal=ctx, plan_sha256=attestation.plan_sha256)
        _emit({"actions": len(out["actions"]), "errors": out["errors"], "residuals": len(out["residuals"])})
        return rc
    except (A.ApplyRefused, H.InstallPlanError, AttestationLost, ArgvRefused, OSError) as exc:
        print(f"host_broker_rehearsal_live: refused: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
