#!/usr/bin/env python3
"""T-2920 U3a: apply, verify and rollback for the ORG-052 host-broker install plan, DARK.

Intent (T-2920, arc T-2652): a clean box converges to the whole authority
boundary from a reviewed root-run plan, reproducibly. U1
(``tools/host_broker_install.py``) builds the sha-pinned PLAN. This module
EXECUTES that plan, but only against a FIXTURE ROOT, and it writes a signed-off
receipt (``pantheon-host-broker-install-receipt/v1``) that ``rollback`` reverses.
The U4 nspawn rehearsal later drives this same code against a disposable root.

DARK by construction:

* There is NO live mode. ``--execute`` without ``--root`` is refused even if
  ``tools.host_broker_install.LIVE_ACTUATION_ENABLED`` (the one switch; this
  module has no flag of its own) were ever flipped. A live path is a later,
  separately reviewed change (U3b's OPERATOR_BOOTSTRAP proposal).
* Every command goes through an injected ``CommandRunner``
  (tools/_organ_lifecycle.py). The only runner this module constructs is
  ``FixtureCommandRunner``: it records the exact live argv and emulates a fixed
  table of binaries inside the fixture root. An argv outside the table FAILS.
  This module imports no subprocess and never names the subprocess runner.
* Every planned absolute path is re-rooted under the fixture root; a path that
  escapes it (``..``, a symlink component) is refused.
* T-2920 U4b seam (additive): ``apply`` / ``verify`` / ``rollback`` accept
  ``rehearsal=`` an attested ``tools.host_broker_rehearsal_live.RehearsalContext``
  (isinstance-checked, lazily imported, root exactly '/'); the receipt mode is then
  ``rehearsal-nspawn``, and each path refuses a receipt of any other mode (Recovery (c)).
  The CLI below stays fixture-only.

Shape (HO rulings 2026-10-27, recorded on T-2920):

* Q1 HYBRID: family entries go through ``RootHelper`` (bootstrap context) over a
  ``SimulatedSubstratePort`` seeded from a fresh fixture observation; the argv
  is ``host_substrate.ports.apply_argv`` unchanged (Recovery-approved). The manifest
  is U1's render-staging fragment, loaded through the host-substrate handlers'
  additive ``_manifest(doc, repo_root)`` seam. Tool-owned entries run here.
* Q6: ``framework/<rev>/`` is written whole (into ``.<rev>.partial``, then one
  directory rename), then ``active`` is switched by ONE ``os.replace`` of a temp
  symlink. The previous rev is kept; rollback points ``active`` back.
* Q7: dpkg packages are verify-and-record only (``dpkg-query``).
* Q10: before a rerun or rollback that finds ``/var/lib/pantheon-host-*``, the
  state dirs are backed up (stdlib tarfile, into the receipt dir). Never migrated,
  never deleted.
* Accounts are sticky: rollback never removes an account (residual, T-2921).

CLI (dark; fixture roots only; always ``python3 -I`` so no cwd or PYTHON* path is
ever on sys.path)::

    python3 -I tools/host_broker_install_apply.py apply --plan P --manifest M --registry R \\
        --tracked-files T --inputs I --root FIXTURE --receipt /host/path/receipt.json [--execute]
    python3 -I tools/host_broker_install_apply.py verify --plan P --root FIXTURE [--receipt RC] [--expect-started]
    python3 -I tools/host_broker_install_apply.py rollback --receipt RC --root FIXTURE
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import posixpath
import stat
import sys
import tarfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Final, Iterable, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import host_broker_install as H  # noqa: E402
from tools._organ_lifecycle import CommandResult, FakeCommandRunner  # noqa: E402
from tools.germinate.host_substrate import families as F  # noqa: E402
from tools.germinate.host_substrate import ports as P  # noqa: E402
from tools.germinate.host_substrate.helper import ACTIVATE, BOOTSTRAP, HelperRefusal, RootHelper  # noqa: E402
from tools.germinate.host_substrate.ports import SimulatedSubstratePort  # noqa: E402

RECEIPT_SCHEMA: Final = "pantheon-host-broker-install-receipt/v1"
INPUTS_SCHEMA: Final = "pantheon-host-broker-install-inputs/v1"
FIXTURE_META: Final = ".pantheon-fixture"
HANDLERS_PATH: Final = H.REPO_ROOT / "tools" / "germinate" / "components" / "host-substrate_handlers.py"
STATE_DIR_PREFIX: Final = "/var/lib/pantheon-host-"

# Fixed binaries of the tool-owned argv (the family argv is ports.py's).
DPKG_QUERY: Final = "/usr/bin/dpkg-query"
DPKG_FORMAT: Final = "${Status} ${Version}"
INSTALL: Final = P.INSTALL
#: argv that only observe; every other runner call is a host mutation
OBSERVE_BINARIES: Final = frozenset({DPKG_QUERY})
#: Recovery D15: dpkg-query's <pkg> comes ONLY from this literal, never from plan or caller data.
VERIFY_PACKAGES: Final = ("python3", "python3-dbus", "python3-gi", "python3-yaml")
#: Review L3: the PyYAML probe argv is a MODULE LITERAL; a plan whose probe differs is refused.
PYYAML_PROBE: Final = ("/usr/bin/python3", "-I", "-c", "import yaml; print(yaml.__version__)")
#: Review L4: never the nobody uid
NOBODY_UID: Final = 65534
#: Recovery D15: the root build runs with exactly this environment (env -i first).
BUILD_ENV: Final = ("LC_ALL=C", "PATH=/usr/bin:/bin")
TOKEN_MAX_BYTES: Final = 4096
_ACCOUNT_RE = H._NAME_RE


class ApplyRefused(RuntimeError):
    """Refused before the first action (rc 2); nothing was mutated."""


class EntryFailed(RuntimeError):
    """An entry failed mid-apply; the apply stops and writes a partial receipt."""


# ---------------------------------------------------------------------------
# canonical helpers
# ---------------------------------------------------------------------------


def canonical(value: Any) -> str:
    return H.canonical(value)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mode_str(st_mode: int) -> str:
    return format(stat.S_IMODE(st_mode), "04o")


def _norm_host_path(path: str, what: str = "path") -> str:
    if (not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "\x00" in path
            or posixpath.normpath(path) != path or ".." in path.split("/")):
        raise ApplyRefused(f"{what}: {path!r} is not a normalized absolute host path")
    return path


# ---------------------------------------------------------------------------
# the fixture host: re-rooted file system plus ownership sidecar
# ---------------------------------------------------------------------------


class FixtureHost:
    """A fixture root standing in for ``/``. Owner and group live in a sidecar
    (a non-root user cannot chown); modes and bytes are real. A path absent
    from the sidecar is root:root (the fixture models a root-run host)."""

    def __init__(self, root: Path | str | None, *, exempt_prefixes: Iterable[str] = ()):
        if root is None or str(root) == "":
            raise ApplyRefused("a fixture root is required (U3a has no live mode)")
        raw = Path(root)
        if not raw.is_absolute():
            raw = Path.cwd() / raw
        if str(raw) == "/" or os.path.realpath(raw) == "/":
            raise ApplyRefused("the fixture root must not be '/'")
        if raw.is_symlink() or not raw.is_dir():
            raise ApplyRefused(f"the fixture root {raw} must be an existing directory, not a symlink")
        self.root = Path(os.path.realpath(raw))
        self.meta = self.root / FIXTURE_META
        self.mutations: list[tuple[str, str]] = []
        self.exempt = tuple(exempt_prefixes)
        #: (op, host path) -> raise OSError there (fault injection for tests)
        self.faults: set[tuple[str, str]] = set()
        #: test seam: called between a no-follow lstat and the open that re-checks it
        self.race_hook: Callable[[], None] | None = None

    # --- paths ------------------------------------------------------------

    def path(self, host_path: str) -> Path:
        """Re-root ``host_path``; refuse a symlink in any ANCESTOR component."""

        _norm_host_path(host_path)
        if host_path.split("/")[1:2] == [FIXTURE_META]:
            raise ApplyRefused(f"{host_path}: the fixture sidecar is not a host path")
        parts = host_path.strip("/").split("/")
        current = self.root
        for part in parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise ApplyRefused(f"{host_path}: symlink component {current} (no path may leave the fixture root)")
        return self.root.joinpath(*parts)

    def wpath(self, host_path: str, *, allow_leaf_symlink: bool = False) -> Path:
        """``path`` for a WRITE or META intent: additionally refuse a symlink at the
        LEAF (review MED-3), so no write, chmod, chown, mkdir, rename, unlink or
        extraction can land outside through a swapped-in link."""

        p = self.path(host_path)
        if not allow_leaf_symlink and p.is_symlink():
            raise EntryFailed(f"{host_path}: a symlink at the leaf is refused for a write or meta op")
        return p

    def precheck(self) -> None:
        """T-2920 U4b seam: called before a mutation the apply flow makes with direct
        ``os`` calls (receipt, progress, backup, credential create). A fixture has
        nothing to re-attest; ``LiveHost.precheck`` re-runs the nspawn recheck."""

    def _mutate(self, op: str, host_path: str) -> None:
        if (op, host_path) in self.faults:
            raise OSError(f"fixture fault injected: {op} {host_path}")
        if not any(host_path == p or host_path.startswith(p) for p in self.exempt):
            self.mutations.append((op, host_path))

    # --- sidecar ------------------------------------------------------------

    def _load(self, name: str, default: Any) -> Any:
        f = self.meta / name
        return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else default

    def _save(self, name: str, value: Any) -> None:
        if self.meta.is_symlink():
            raise EntryFailed("the fixture sidecar dir is a symlink; refused")
        self.meta.mkdir(mode=0o700, exist_ok=True)
        fd = os.open(self.meta / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(canonical(value))

    def owner_of(self, host_path: str) -> tuple[str, str]:
        owner, group = self._load("owners.json", {}).get(host_path, ["root", "root"])
        return owner, group

    def _set_owner(self, host_path: str, owner: str | None, group: str | None) -> None:
        owners = self._load("owners.json", {})
        cur = owners.get(host_path, ["root", "root"])
        new = [owner if owner is not None else cur[0], group if group is not None else cur[1]]
        if new == ["root", "root"]:
            owners.pop(host_path, None)
        else:
            owners[host_path] = new
        self._save("owners.json", owners)

    def _forget(self, host_path: str) -> None:
        owners = self._load("owners.json", {})
        prefix = host_path.rstrip("/") + "/"
        for key in [k for k in owners if k == host_path or k.startswith(prefix)]:
            del owners[key]
        self._save("owners.json", owners)

    def enabled(self) -> set[str]:
        return set(self._load("enabled.json", []))

    def set_enabled(self, unit: str, on: bool) -> None:
        units = self.enabled()
        (units.add if on else units.discard)(unit)
        self._save("enabled.json", sorted(units))

    def facts(self) -> dict[str, Any]:
        return self._load("facts.json", {})

    def uid_of(self, _name: str) -> int:
        """Every fixture file is really owned by the test user (names are in the sidecar)."""

        return os.getuid()

    def owner_uid(self, host_path: str) -> int | None:
        """The modelled owner uid: the sidecar name looked up in the fixture passwd."""

        name = self.owner_of(host_path)[0]
        if name == "root":
            return 0
        entry = self.passwd().get(name)
        return None if entry is None else entry["uid"]

    def fchown(self, fd: int, host_path: str, owner: str, group: str) -> None:
        """Fixture fchown: the sidecar, keyed by the path the fd was opened at. A real
        os.fchown belongs to a live host class, which U3a does not have (dark)."""

        os.fstat(fd)  # the fd is live
        self._mutate("fchown", host_path)
        self._set_owner(host_path, owner, group)

    # --- observation -------------------------------------------------------

    def observe(self, host_path: str, *, hash_content: bool = True) -> dict[str, Any]:
        """``hash_content=False`` never reads the bytes (the token path: never hashed anywhere)."""

        p = self.path(host_path)
        try:
            st = os.lstat(p)
        except FileNotFoundError:
            return {"exists": False}
        owner, group = self.owner_of(host_path)
        out: dict[str, Any] = {"exists": True, "owner": owner, "group": group, "mode": _mode_str(st.st_mode)}
        if stat.S_ISLNK(st.st_mode):
            out.update(type="symlink", link_target=os.readlink(p))
            out.pop("mode")
        elif stat.S_ISDIR(st.st_mode):
            out["type"] = "dir"
        elif stat.S_ISREG(st.st_mode):
            out["type"] = "file"
            if hash_content:
                out.update(sha256=sha256_bytes(self.read_bytes(host_path)), size=st.st_size)
        else:
            out["type"] = "other"
        return out

    def read_bytes(self, host_path: str) -> bytes:
        fd = os.open(self.path(host_path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            chunks = []
            while True:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(fd)

    def exists(self, host_path: str) -> bool:
        return os.path.lexists(self.path(host_path))

    # --- mutations ---------------------------------------------------------

    def _fchmod_nofollow(self, p: Path, mode: int) -> None:
        fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fchmod(fd, mode)
        finally:
            os.close(fd)

    def mkdir(self, host_path: str, owner: str, group: str, mode: str) -> None:
        p = self.wpath(host_path)
        if p.parent.is_symlink() or not p.parent.is_dir():
            raise EntryFailed(f"{host_path}: parent directory is missing (base OS dirs are preconditions)")
        self._mutate("mkdir", host_path)
        os.mkdir(p, 0o700)
        self._fchmod_nofollow(p, int(mode, 8))
        self._set_owner(host_path, owner, group)

    def write_file(self, host_path: str, data: bytes, owner: str, group: str, mode: str) -> None:
        """Write via an O_EXCL temp sibling and one rename."""

        p = self.wpath(host_path)
        if p.parent.is_symlink() or not p.parent.is_dir():
            raise EntryFailed(f"{host_path}: parent directory is missing")
        if p.is_symlink() or (p.exists() and not p.is_file()):
            raise EntryFailed(f"{host_path}: refusing to replace a non-regular file")
        self._mutate("write", host_path)
        tmp = p.parent / f".{p.name}.pantheon-tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fchmod(fd, int(mode, 8))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, p)
        self._set_owner(host_path, owner, group)

    def copy_file(self, src_host: str, dst_host: str, owner: str, group: str, mode: str) -> None:
        """``install``-style copy. The bytes pass through without being kept."""

        self.write_file(dst_host, self.read_bytes(src_host), owner, group, mode)

    def chown(self, host_path: str, owner: str, group: str) -> None:
        if not os.path.lexists(self.wpath(host_path)):
            raise EntryFailed(f"{host_path}: chown of a missing path")
        self._mutate("chown", host_path)
        self._set_owner(host_path, owner, group)

    def chmod(self, host_path: str, mode: str) -> None:
        p = self.wpath(host_path)
        self._mutate("chmod", host_path)
        self._fchmod_nofollow(p, int(mode, 8))  # O_NOFOLLOW fd: never follows a leaf link

    def unlink(self, host_path: str, *, allow_leaf_symlink: bool = False) -> None:
        p = self.wpath(host_path, allow_leaf_symlink=allow_leaf_symlink)
        self._mutate("unlink", host_path)
        os.unlink(p)
        self._forget(host_path)

    def rmdir(self, host_path: str) -> None:
        p = self.wpath(host_path)
        self._mutate("rmdir", host_path)
        os.rmdir(p)
        self._forget(host_path)

    def rename(self, src_host: str, dst_host: str) -> None:
        src, dst = self.wpath(src_host), self.wpath(dst_host)
        if os.path.lexists(dst):
            raise EntryFailed(f"{dst_host}: rename target exists; refused")
        self._mutate("rename", src_host)
        self._mutate("rename", dst_host)
        os.rename(src, dst)
        owners = self._load("owners.json", {})
        prefix = src_host.rstrip("/") + "/"
        moved = {}
        for key in list(owners):
            if key == src_host or key.startswith(prefix):
                moved[dst_host + key[len(src_host):]] = owners.pop(key)
        owners.update(moved)
        self._save("owners.json", owners)

    def symlink_replace(self, host_path: str, target: str) -> None:
        """Switch ``host_path`` to ``target`` with ONE rename of a temp symlink (Q6)."""

        p = self.wpath(host_path, allow_leaf_symlink=True)  # the active pointer IS a symlink
        if os.path.lexists(p) and not p.is_symlink():
            raise EntryFailed(f"{host_path}: exists and is not a symlink; refusing to replace it")
        tmp_host = posixpath.join(posixpath.dirname(host_path), f".{posixpath.basename(host_path)}.pantheon-tmp")
        tmp = self.path(tmp_host)
        if os.path.lexists(tmp):
            raise EntryFailed(f"{tmp_host}: a stale temp link exists")
        self._mutate("symlink", tmp_host)
        os.symlink(target, tmp)
        try:
            self._mutate("replace", host_path)
            os.replace(tmp, p)
        except OSError:
            os.unlink(tmp)
            raise

    def remove_tree_contained(self, host_path: str, base: str) -> None:
        """Remove ``host_path`` (a direct child of ``base``) bottom-up. Symlinks are
        unlinked as entries and never followed; nothing outside is touched."""

        if posixpath.dirname(host_path) != base:
            raise EntryFailed(f"{host_path}: tree removal is only allowed for a direct child of {base}")
        top = self.path(host_path)
        if top.is_symlink() or not top.is_dir():
            raise EntryFailed(f"{host_path}: not a real directory; refusing tree removal")
        self._mutate("remove_tree", host_path)
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
        self._forget(host_path)

    # --- accounts (fixture /etc/passwd, /etc/group, /etc/shadow) -------------

    def _table(self, name: str) -> list[list[str]]:
        p = self.path(f"/etc/{name}")
        if not p.is_file():
            return []
        return [line.split(":") for line in p.read_text(encoding="utf-8").splitlines() if line]

    def _write_table(self, name: str, rows: list[list[str]]) -> None:
        host = f"/etc/{name}"
        mode = "0640" if name == "shadow" else "0644"
        self.write_file(host, "".join(":".join(r) + "\n" for r in rows).encode("utf-8"), "root",
                        "shadow" if name == "shadow" else "root", mode)

    def passwd(self) -> dict[str, dict[str, Any]]:
        return {r[0]: {"uid": int(r[2]), "gid": int(r[3]), "home": r[5], "shell": r[6]} for r in self._table("passwd")}

    def groups(self) -> dict[str, int]:
        return {r[0]: int(r[2]) for r in self._table("group")}

    def shadow_field(self, name: str) -> str | None:
        for r in self._table("shadow"):
            if r[0] == name:
                return r[1]
        return None


# ---------------------------------------------------------------------------
# the fixture command runner: a recording Fake with a FIXED emulation table
# ---------------------------------------------------------------------------

#: Recovery D15 (T-2920 U4b) RULED producer template, the fixed tokens by position (the
#: rest are <user>, <group>, <fw_root>/tools/host_plan_producer.py, <fw_root>, two
#: canonical JSON records and <staging>/<seat_id>-<revision>.json).
PRODUCER_ARGV_LEN: Final = 22
PRODUCER_FIXED: Final = {0: H.SETPRIV, 3: "--clear-groups", **{4 + i: f for i, f in enumerate(H.SETPRIV_HARDENING)},
                         7: "--", 8: H.ENV, 9: "-i", 10: "PATH=/usr/bin:/bin", 11: H.PYTHON3, 12: "-I",
                         14: "--framework-root", 16: "--seat-record", 18: "--manifest-record", 20: "--out"}


def producer_argv_shape_ok(argv: Sequence[str]) -> bool:
    """The amended (Recovery D15) setpriv producer form, token by token. The CALLER binds the
    variable slots to the plan (the fixture: a test handler; the rehearsal runner: exact
    equality with a plan entry's ``producer_argv``)."""

    argv = list(argv)
    if len(argv) != PRODUCER_ARGV_LEN or any(argv[i] != v for i, v in PRODUCER_FIXED.items()):
        return False
    user, group = argv[1].partition("--reuid=")[2], argv[2].partition("--regid=")[2]
    if not (argv[1].startswith("--reuid=") and argv[2].startswith("--regid=")
            and _ACCOUNT_RE.fullmatch(user) and _ACCOUNT_RE.fullmatch(group)):
        return False
    fw = argv[15]
    return argv[13] == posixpath.join(fw, "tools/host_plan_producer.py") and fw.startswith("/")

Handler = Callable[[Sequence[str], "FixtureHost"], CommandResult]


class FixtureCommandRunner(FakeCommandRunner):
    """Records every argv (``.calls``) exactly as a live host would receive it,
    executes NOTHING, and emulates a fixed table of binaries inside the fixture.

    * ``scripted`` (exact argv -> result) wins and is never emulated.
    * ``handlers`` ((predicate, handler) pairs) let a TEST emulate the producer
      and build_runtime argv; the CLI registers none.
    * Anything else FAILS (rc 127). FakeCommandRunner's default success is
      overridden on purpose: an unknown binary never "succeeds".
    """

    UNKNOWN: Final = CommandResult(127, "", "fixture: no emulation for this argv")

    def __init__(self, host: FixtureHost, *, scripted: Mapping[tuple[str, ...], CommandResult] | None = None,
                 handlers: Sequence[tuple[Callable[[Sequence[str]], bool], Handler]] = ()):
        super().__init__(scripted, default=self.UNKNOWN)
        self.host = host
        self.handlers = list(handlers)
        #: (argv, cwd) for every call; cwd None = inherited (Recovery D15 f: the root build
        #: always passes an explicit root-owned cwd)
        self.call_cwds: list[tuple[tuple[str, ...], str | None]] = []

    def run(self, argv: Sequence[str], *, cwd: str | None = None) -> CommandResult:
        key = tuple(argv)
        self.calls.append(key)
        self.call_cwds.append((key, cwd))
        if argv and argv[0] == H.SETPRIV and not producer_argv_shape_ok(argv):
            # Recovery D15 (T-2920 U4b): ONLY the amended producer form; the old one fails
            return self.UNKNOWN
        if key in self.scripted:
            return self.scripted[key]
        if argv and argv[0] not in OBSERVE_BINARIES and not self._is_probe(argv):
            self.host._mutate("exec:" + argv[0], argv[-1] if len(argv) > 1 else argv[0])
        for predicate, handler in self.handlers:
            if predicate(argv):
                return handler(argv, self.host)
        try:
            return self._emulate(list(argv))
        except (EntryFailed, OSError, ValueError, IndexError) as exc:
            return CommandResult(1, "", f"fixture: {exc}")

    @staticmethod
    def _is_probe(argv: Sequence[str]) -> bool:
        return list(argv[:3]) == [H.PYTHON3, "-I", "-c"]

    def _emulate(self, argv: list[str]) -> CommandResult:
        h = self.host
        ok = CommandResult(0, "", "")
        b = argv[0]
        if b == DPKG_QUERY and argv[1:3] == ["-W", "-f"] and len(argv) == 5 and argv[3] == DPKG_FORMAT:
            version = (h.facts().get("dpkg") or {}).get(argv[4])
            if not version:
                return CommandResult(1, "", f"dpkg-query: no packages found matching {argv[4]}")
            return CommandResult(0, f"install ok installed {version}", "")
        if self._is_probe(argv) and len(argv) == 4:
            version = h.facts().get("pyyaml")
            return CommandResult(0, f"{version}\n", "") if version else CommandResult(1, "", "No module named yaml")
        if b == P.GROUPADD and argv[1] == "--gid" and len(argv) == 4:
            gid, name = int(argv[2]), argv[3]
            rows = h._table("group")
            if any(r[0] == name or int(r[2]) == gid for r in rows):
                return CommandResult(9, "", f"groupadd: group '{name}' or gid {gid} already exists")
            h._write_table("group", rows + [[name, "x", str(gid), ""]])
            return ok
        if b == P.USERADD and len(argv) == 11:
            opts = dict(zip(argv[1:9:2], argv[2:10:2]))
            if set(opts) != {"--uid", "--gid", "--home-dir", "--shell"} and "--create-home" not in argv:
                raise ValueError("unexpected useradd shape")
            name = argv[-1]
            uid, gid = argv[argv.index("--uid") + 1], argv[argv.index("--gid") + 1]
            home, shell = argv[argv.index("--home-dir") + 1], argv[argv.index("--shell") + 1]
            rows = h._table("passwd")
            if any(r[0] == name or r[2] == uid for r in rows):
                return CommandResult(9, "", f"useradd: user '{name}' or uid {uid} already exists")
            h._write_table("passwd", rows + [[name, "x", uid, gid, "", home, shell]])
            h._write_table("shadow", h._table("shadow") + [[name, "!", "20000", "0", "99999", "7", "", "", ""]])
            if "--create-home" in argv and not h.exists(home):
                h.mkdir(home, name, name, "0700")
            return ok
        if b in (P.USERDEL, P.GROUPDEL) and len(argv) == 2:
            name = argv[1]
            for table in (("passwd", "shadow") if b == P.USERDEL else ("group",)):
                h._write_table(table, [r for r in h._table(table) if r[0] != name])
            return ok
        if b == P.CHOWN and len(argv) == 4 and argv[1] == "-h":
            # chown -h (no-dereference, U3c): the sidecar is keyed by the path itself
            owner, group = argv[2].split(":", 1)
            h.chown(argv[3], owner, group)
            return ok
        if b == P.CHMOD and len(argv) == 3:
            h.chmod(argv[2], argv[1])
            return ok
        if b == INSTALL and argv[1] == "-d" and len(argv) == 9:
            h.mkdir(argv[8], argv[3], argv[5], argv[7])
            return ok
        if b == INSTALL and argv[1] == "-o" and len(argv) == 9 and argv[3] == "-g" and argv[5] == "-m":
            # only the family route's hash-checked staged copies (in the receipt dir) are
            # ever a source; there is no token install argv (Recovery D15: in-process create)
            if not any(argv[7].startswith(prefix + ".d/") for prefix in h.exempt):
                return self.UNKNOWN
            h.copy_file(argv[7], argv[8], argv[2], argv[4], argv[6])
            return ok
        if b == P.UNLINK and len(argv) == 2:
            h.unlink(argv[1])
            return ok
        if b == P.SYSTEMCTL and len(argv) == 3 and argv[1] in ("enable", "disable"):
            h.set_enabled(argv[2], argv[1] == "enable")
            return ok
        if b == P.SYSTEMCTL and argv[1:] in (["daemon-reload"], ["reload", "dbus.service"]):
            return ok
        return self.UNKNOWN


# ---------------------------------------------------------------------------
# --inputs (closed, versioned): operator-batch references and fixture values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Inputs:
    token_source_path: str
    operator_uid: int
    telegram_user_id: int
    human_subject: str
    approver_policy_source: str
    broker_policy_source: str
    host_operation_families: tuple[str, ...]
    plan_ttl_seconds: int
    registered_manifests: Mapping[str, str]
    wheelhouse: str


def parse_inputs(raw: bytes) -> Inputs:
    """``pantheon-host-broker-install-inputs/v1``. Errors name FIELDS only, never
    a value (the operator-batch values are never logged)."""

    try:
        doc = H.load_json_strict(raw, "inputs")
    except H.InstallPlanError as exc:
        raise ApplyRefused(str(exc)) from None

    def closed(where: str, value: Any, keys: Iterable[str]) -> Mapping[str, Any]:
        keys = frozenset(keys)
        if not isinstance(value, dict) or set(value) != keys:
            raise ApplyRefused(f"inputs.{where}: must have exactly the fields {sorted(keys)}")
        return value

    closed("", doc, ("schema", "operator_batch", "broker", "registered_manifests", "wheelhouse"))
    if doc["schema"] != INPUTS_SCHEMA:
        raise ApplyRefused(f"inputs.schema: must be {INPUTS_SCHEMA}")
    batch = closed("operator_batch", doc["operator_batch"], ("1", "2", "3"))
    one = closed("operator_batch.1", batch["1"], ("token_source_path", "operator_uid"))
    two = closed("operator_batch.2", batch["2"], ("telegram_user_id", "human_subject"))
    three = closed("operator_batch.3", batch["3"], ("approver_policy_source", "broker_policy_source"))
    for where, value in (("operator_batch.1.token_source_path", one["token_source_path"]),
                         ("operator_batch.3.approver_policy_source", three["approver_policy_source"]),
                         ("operator_batch.3.broker_policy_source", three["broker_policy_source"]),
                         ("wheelhouse", doc["wheelhouse"])):
        try:
            _norm_host_path(value, where)
        except ApplyRefused:
            raise ApplyRefused(f"inputs.{where}: must be a normalized absolute host path") from None
    operator = one["operator_uid"]
    if not isinstance(operator, int) or isinstance(operator, bool) or operator < 0:
        raise ApplyRefused("inputs.operator_batch.1.operator_uid: must be a uid (the token source is root- or operator-owned)")
    if operator == NOBODY_UID:
        raise ApplyRefused("inputs.operator_batch.1.operator_uid: must not be the nobody uid")
    user = two["telegram_user_id"]
    if not isinstance(user, int) or isinstance(user, bool) or user <= 0:
        raise ApplyRefused("inputs.operator_batch.2.telegram_user_id: must be a positive int (private chat id == user id)")
    if not isinstance(two["human_subject"], str) or not H._IDENT_RE.match(two["human_subject"]):
        raise ApplyRefused("inputs.operator_batch.2.human_subject: must be an identifier")
    broker = closed("broker", doc["broker"], ("host_operation_families", "plan_ttl_seconds"))
    fams = broker["host_operation_families"]
    if not isinstance(fams, list) or not fams or not all(isinstance(f, str) and H._IDENT_RE.match(f) for f in fams):
        raise ApplyRefused("inputs.broker.host_operation_families: must be a non-empty list of identifiers")
    ttl = broker["plan_ttl_seconds"]
    if not isinstance(ttl, int) or isinstance(ttl, bool):
        raise ApplyRefused("inputs.broker.plan_ttl_seconds: must be an int")
    manifests = doc["registered_manifests"]
    if not isinstance(manifests, dict) or not manifests:
        raise ApplyRefused("inputs.registered_manifests: must map each registry manifest revision to a source path")
    for rev, src in manifests.items():
        if not isinstance(rev, str) or not H._REVISION_RE.fullmatch(rev):
            raise ApplyRefused("inputs.registered_manifests: keys must be manifest revisions")
        _norm_host_path(src, f"inputs.registered_manifests.{rev}")
    return Inputs(one["token_source_path"], operator, user, two["human_subject"], three["approver_policy_source"],
                  three["broker_policy_source"], tuple(fams), ttl, dict(manifests), doc["wheelhouse"])


# ---------------------------------------------------------------------------
# the fragment, reconstructed from the plan alone (verify / rollback need no repo)
# ---------------------------------------------------------------------------


def fragment_from_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild U1's ``substrate_manifest_fragment`` from the plan entries; its
    sha must equal the plan's ``substrate_fragment_sha256``."""

    accounts, paths, units, policies = {}, {}, {}, {}
    etc_root = None
    for e in plan["entries"]:
        src = e["source"]
        if e["kind"] == "account":
            accounts[src["params"]["account"]] = {"uid": e["inputs"]["uid"], "home": e["path"], "shell": e["inputs"]["shell"]}
        if src.get("route") != "family":
            continue
        if src["family"] == "owner_mode.set":
            paths[e["path"]] = {"owner": e["owner"], "group": e["group"], "mode": e["mode"]}
            if etc_root is None or len(e["path"]) < len(etc_root):
                etc_root = e["path"]
        elif src["family"] == "unit.system.install":
            units[src["params"]["unit"]] = {"path": e["path"], "source": src["staging"], "sha256": e["content_sha256"],
                                            "enable": src["params"]["unit"] in H.STATIC_UNITS}
        elif src["family"] == "dbus_policy.install":
            policies[src["params"]["policy"]] = {"path": e["path"], "source": src["staging"], "sha256": e["content_sha256"]}
    frag = {
        "schema": F.MANIFEST_SCHEMA, "status": "PROVISIONAL",
        "note": f"rendered by tools/host_broker_install.py from {H.MANIFEST_SCHEMA} revision {plan['inputs']['manifest_revision']}",
        "groups": {}, "accounts": accounts, "installer_roots": [etc_root] if etc_root else [], "paths": paths,
        "mailbox_dirs": {}, "units": units, "dbus_policies": policies, "sudoers_templates": {}, "legacy_requirements": [],
    }
    if sha256_bytes(canonical(frag).encode("ascii")) != plan["substrate_fragment_sha256"]:
        raise ApplyRefused("the plan's family entries do not reproduce substrate_fragment_sha256")
    return frag


def _handlers_module():
    spec = importlib.util.spec_from_file_location("_host_substrate_handlers_seam", HANDLERS_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def substrate_manifest(fragment: Mapping[str, Any], staging_dir: Path | None) -> F.Manifest:
    """Through the host-substrate handlers' additive seam (T-2920 Q1)."""

    if staging_dir is None:  # verify / rollback: pins only (no source re-hash)
        return F.load_manifest(dict(fragment))
    return _handlers_module()._manifest(dict(fragment), staging_dir)


# ---------------------------------------------------------------------------
# family observation -> SimulatedSubstratePort
# ---------------------------------------------------------------------------


def observe_subject(host: FixtureHost, subject: str, port: Any = None) -> Any:
    """``port`` (T-2920 U4b): the rehearsal's attested ``RehearsalSubstratePort``; the
    subject is then observed on the real machine (``ports.observe`` argv only)."""

    if port is not None:
        return port.get(subject)
    kind, _, rest = subject.partition(":")
    if kind == "passwd":
        entry = host.passwd().get(rest)
        return None if entry is None else {**entry, "locked": False}
    if kind == "group":
        gid = host.groups().get(rest)
        return None if gid is None else {"gid": gid}
    if kind == "owner_mode":
        obs = host.observe(rest)
        if not obs["exists"] or obs["type"] == "symlink":
            return None
        return {"owner": obs["owner"], "group": obs["group"], "mode": obs["mode"]}
    if kind == "file":
        obs = host.observe(rest)
        if not obs["exists"]:
            return None
        if obs["type"] != "file":
            return {"type": obs["type"]}
        return {"sha256": obs["sha256"], "mode": obs["mode"], "owner": obs["owner"]}
    if kind == "enabled":
        return True if rest in host.enabled() else None
    return None


def fixture_path_stat(host: FixtureHost, path: str) -> tuple[str, str, str, str] | None:
    """The row-4 stat (%F, owner, group, mode) of ``path`` in the fixture, no-follow,
    WITHOUT the ancestor refusal of ``host.path`` (the walk itself must see a link)."""

    full = host.root.joinpath(*[p for p in path.strip("/").split("/") if p])
    try:
        st = os.lstat(full)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        ftype = "symbolic link"
    elif stat.S_ISDIR(st.st_mode):
        ftype = "directory"
    elif stat.S_ISREG(st.st_mode):
        ftype = "regular file" if st.st_size else "regular empty file"
    else:
        ftype = "other"
    owner, group = host.owner_of(path) if path != "/" else ("root", "root")
    return (ftype, owner, group, _mode_str(st.st_mode))


def seeded_port(host: FixtureHost, subjects: Iterable[str], live: Any = None) -> SimulatedSubstratePort:
    """A SimulatedSubstratePort seeded from a FRESH fixture observation: accounts,
    groups, every family subject, and (U3d T-5657 / U3c T-5653) the real stat of every
    ancestor and leaf of each path subject, so the ancestor walk and the symlink
    guard see the fixture, never the simulated trusted-dir default.

    ``live`` (T-2920 U4b): the rehearsal port. The Q1 hybrid is KEPT (RootHelper still
    runs over this simulated port, and the argv still runs only through the runner),
    but the seed is the REAL machine's observation. An observation error fails closed."""

    if live is not None:
        return _seeded_from_live(live, subjects)
    port = SimulatedSubstratePort()
    for name, entry in host.passwd().items():
        port.put(f"passwd:{name}", {**entry, "locked": False})
    for name, gid in host.groups().items():
        port.put(f"group:{name}", {"gid": gid})
    for subject in subjects:
        try:
            port.put(subject, observe_subject(host, subject))
        except ApplyRefused:  # an ancestor symlink: the walk below models it and refuses
            port.put(subject, None)
        kind, _, path = subject.partition(":")
        if kind not in ("acl", "owner_mode", "dir"):
            continue
        parts = path.strip("/").split("/")
        for i in range(len(parts) + 1):
            p = "/" + "/".join(parts[:i])
            st = fixture_path_stat(host, p)
            port.state["path_stats"][p] = None if st is None else list(st)
            if st is not None:
                port.state["path_types"][p] = st[0]
    return port


def _seeded_from_live(live: Any, subjects: Iterable[str]) -> SimulatedSubstratePort:
    port = SimulatedSubstratePort()
    for subject, value in live.snapshot().items():
        port.put(subject, {**value, "locked": False} if subject.startswith("passwd:") else value)
    for subject in subjects:
        port.put(subject, live.get(subject))  # a real ObservationError propagates (fail closed)
        kind, _, path = subject.partition(":")
        if kind not in ("acl", "owner_mode", "dir"):
            continue
        parts = path.strip("/").split("/")
        for i in range(len(parts) + 1):
            p = "/" + "/".join(parts[:i])
            st = live.path_stat(p)
            port.state["path_stats"][p] = None if st is None else list(st)
            if st is not None:
                port.state["path_types"][p] = st[0]
    return port


# ---------------------------------------------------------------------------
# launchers
# ---------------------------------------------------------------------------


def render_launcher(entry: Mapping[str, Any], rev: str) -> bytes:
    """``python3 -I -m <module>`` with cwd = the concrete rev. ``-I`` drops the
    cwd from sys.path, so the launcher is a python3 -I script that puts ONLY the
    concrete rev on sys.path and runs the module as __main__ (runpy)."""

    exec_argv = list(entry["inputs"]["exec"])
    cwd = entry["inputs"]["cwd"]
    if exec_argv[:3] != [H.PYTHON3, "-I", "-m"] or len(exec_argv) < 4:
        raise EntryFailed(f"{entry['id']}: launcher exec must start {H.PYTHON3} -I -m")
    if posixpath.basename(cwd) != rev or posixpath.basename(cwd) == "active":
        raise EntryFailed(f"{entry['id']}: launcher cwd must be the concrete rev, never 'active'")
    module, extra = exec_argv[3], exec_argv[4:]
    text = (
        f"#!{H.PYTHON3} -I\n"
        f"# GENERATED by tools/host_broker_install_apply.py (T-2920 U3a) for framework rev {rev}; do not edit.\n"
        f"# Equivalent of: {' '.join(exec_argv)}  (cwd {cwd})\n"
        "import os\nimport runpy\nimport sys\n\n"
        "sys.dont_write_bytecode = True\n"
        f"FRAMEWORK_ROOT = {json.dumps(cwd)}\n"
        "os.chdir(FRAMEWORK_ROOT)\n"
        "sys.path.insert(0, FRAMEWORK_ROOT)\n"
        f"sys.argv = [{json.dumps(module)}, *{json.dumps(extra)}, *sys.argv[1:]]\n"
        f"runpy.run_module({json.dumps(module)}, run_name=\"__main__\", alter_sys=True)\n"
    )
    return text.encode("utf-8")


# ---------------------------------------------------------------------------
# the run context
# ---------------------------------------------------------------------------


@dataclass
class Ctx:
    host: FixtureHost
    runner: FixtureCommandRunner
    plan: dict[str, Any]
    fmanifest: F.Manifest
    contents: Mapping[str, bytes] = field(default_factory=dict)
    inputs: Inputs | None = None
    rcd: str | None = None
    repo_root: Path = H.REPO_ROOT
    execute: bool = False
    expect_started: bool = False
    framework_created: bool = False
    registry_records: list[dict[str, str]] = field(default_factory=list)
    packages: dict[str, str] = field(default_factory=dict)
    pyyaml_version: str | None = None
    saved_counter: int = 0
    #: T-2920 U4b: the attested RehearsalContext (None = the fixture path)
    rehearsal: Any = None

    @property
    def port(self) -> Any:
        return None if self.rehearsal is None else self.rehearsal.port

    @property
    def by_id(self) -> dict[str, dict[str, Any]]:
        return {e["id"]: e for e in self.plan["entries"]}

    def entries_of(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.plan["entries"] if e["kind"] == kind]

    def family_subjects(self) -> list[str]:
        out = []
        for e in self.plan["entries"]:
            src = e["source"]
            if src.get("route") == "family":
                out.append(F.resolve(src["family"], dict(src["params"]), self.fmanifest).subject)
        return out

    def helper(self) -> RootHelper:
        return RootHelper(seeded_port(self.host, self.family_subjects(), self.port), self.fmanifest, BOOTSTRAP)

    def save_copy(self, host_path: str) -> str:
        """Save the current bytes of ``host_path`` into RC.d/saved (root-only)."""

        assert self.rcd is not None
        self.saved_counter += 1
        dest = posixpath.join(self.rcd, "saved", f"{self.saved_counter:04d}-{posixpath.basename(host_path)}")
        self.host.write_file(dest, self.host.read_bytes(host_path), "root", "root", "0600")
        return dest


def _result(e: Mapping[str, Any], action: str, **kw: Any) -> dict[str, Any]:
    out = {"seq": e["seq"], "id": e["id"], "kind": e["kind"], "path": e.get("path"), "route": e["source"].get("route"),
           "action": action,
           "before": kw.pop("before", None), "after_sha256": kw.pop("after_sha256", None),
           "family": kw.pop("family", None), "argv": kw.pop("argv", []), "observations": kw.pop("observations", {})}
    if e.get("hold"):
        out["hold_acknowledged"] = f"fixture: {e['hold']}"
    out.update(kw)
    return out


def _meta(obs: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    return obs.get("owner"), obs.get("group"), obs.get("mode")


# ---------------------------------------------------------------------------
# generic executors
# ---------------------------------------------------------------------------


class _PartialEntry(EntryFailed):
    """An entry that may have mutated before failing; its record goes in the receipt."""

    def __init__(self, record: dict[str, Any], message: str):
        super().__init__(message)
        self.record = record


@contextmanager
def _guard(record: dict[str, Any]):
    """Review MED-2: every executor builds its record (with the before-state) BEFORE
    its first mutation and runs every mutation inside this guard, so ANY exception
    from that point on carries the record (action 'failed') for rollback."""

    try:
        yield
    except _PartialEntry:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised as a partial entry, never swallowed
        record["action"] = "failed"
        raise _PartialEntry(record, f"{type(exc).__name__}: {exc}") from None


def _install_file(ctx: Ctx, e: Mapping[str, Any], data: bytes, owner: str, group: str, mode: str,
                  **extra: Any) -> dict[str, Any]:
    path = e["path"]
    obs = ctx.host.observe(path)
    want = sha256_bytes(data)
    if obs["exists"] and obs["type"] != "file":
        raise EntryFailed(f"{path}: exists and is not a regular file")
    if obs["exists"] and obs["sha256"] == want and _meta(obs) == (owner, group, mode):
        return _result(e, "converged", before=obs, after_sha256=want, **extra)
    action = "replaced" if obs["exists"] else "created"
    if not ctx.execute:
        return _result(e, "would-" + action[:-1], before=obs, after_sha256=want, **extra)
    before = dict(obs)
    if obs["exists"]:
        before["saved"] = ctx.save_copy(path)
    record = _result(e, "failed", before=before, after_sha256=want, **extra)
    with _guard(record):
        ctx.host.write_file(path, data, owner, group, mode)
    record["action"] = action
    return record


def _family_owned_meta(ctx: Ctx, path: str) -> bool:
    return f"owner-mode:{path}" in ctx.by_id


def ex_dir(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    path, owner, group, mode = e["path"], e["owner"], e["group"], e["mode"]
    obs = ctx.host.observe(path)
    fresh = e["source"].get("must_not_exist")
    if fresh:
        if _registrations_converged(ctx):
            return _result(e, "converged", before=obs, observations={"why": "every plan registration is converged"})
        if obs["exists"]:
            raise EntryFailed(f"{path}: a pre-existing producer staging dir refuses the run (PRODUCER_ROOT_CHECKS)")
    if obs["exists"]:
        if obs["type"] != "dir":
            raise EntryFailed(f"{path}: exists and is not a directory")
        if _meta(obs) == (owner, group, mode) or _family_owned_meta(ctx, path):
            # owner/mode of a pre-existing family-owned path converges in owner_mode.set
            return _result(e, "converged", before=obs)
        if not ctx.execute:
            return _result(e, "would-replace", before=obs)
        record = _result(e, "failed", before=obs)
        with _guard(record):
            ctx.host.chown(path, owner, group)
            ctx.host.chmod(path, mode)
        record["action"] = "replaced"
        return record
    if not ctx.execute:
        return _result(e, "would-create", before=obs)
    record = _result(e, "failed", before=obs)
    with _guard(record):
        ctx.host.mkdir(path, owner, group, mode)
    record["action"] = "created"
    return record


# ---------------------------------------------------------------------------
# family route (Q1)
# ---------------------------------------------------------------------------


def ex_family(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    src = e["source"]
    family, params = src["family"], dict(src["params"])
    helper = ctx.helper()
    step = helper.admit(family, params, ACTIVATE)
    before = helper.before(step)
    record = {"family": family, "params": params, "subject": step.subject, "preimage": before,
              "resolved_after": step.resolved.after, "entry": dict(step.resolved.entry)}
    if helper.verify(step).ok:
        return _result(e, "converged", family=record, after_sha256=e.get("content_sha256"))
    staged = None
    if family in ("unit.system.install", "dbus_policy.install"):
        assert ctx.rcd is not None or not ctx.execute
        staged = posixpath.join(ctx.rcd or "/<receipt>.d", "substrate-staging", src["staging"])
    argvs = P.apply_argv(step.resolved, staged_source=staged)
    if not ctx.execute:
        return _result(e, "would-apply", family=record, argv=argvs)
    saved = None
    if step.subject.startswith("file:") and before is not None:
        saved = ctx.save_copy(step.subject[5:])
    record["saved"] = saved
    helper.apply(step)  # preflight (collision, CONFLICT/DRIFT) + the modelled put (no host write)
    ran: list[list[str]] = []
    result = _result(e, "failed", family=record, argv=ran, after_sha256=e.get("content_sha256"))
    with _guard(result):
        for argv in argvs:
            ran.append(list(argv))
            res = ctx.runner.run(argv)
            if res.returncode != 0:
                raise _PartialEntry(result, f"{argv[0]} exited {res.returncode}: {res.stderr.strip()[:200]}")
        check = ctx.helper()
        if not check.verify(check.admit(family, params, ACTIVATE)).ok:
            raise _PartialEntry(result, f"{step.subject} did not converge after its argv")
    result["action"] = "created" if before is None else "replaced"
    return result


# ---------------------------------------------------------------------------
# tool-owned executors
# ---------------------------------------------------------------------------


def ex_package(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    if e["inputs"]["package"] not in VERIFY_PACKAGES:
        raise EntryFailed(f"{e['id']}: not one of the verify-only packages {list(VERIFY_PACKAGES)} (Recovery D15)")
    package = VERIFY_PACKAGES[VERIFY_PACKAGES.index(e["inputs"]["package"])]  # the literal, not plan data
    argv = [DPKG_QUERY, "-W", "-f", DPKG_FORMAT, package]
    res = ctx.runner.run(argv)
    fields = res.stdout.split()
    if res.returncode != 0 or fields[:3] != ["install", "ok", "installed"] or len(fields) != 4:
        raise EntryFailed(f"package {package}: not installed (verify-and-record only, Q7; never installed)")
    ctx.packages[package] = fields[3]
    obs: dict[str, Any] = {"version": fields[3]}
    argvs = [argv]
    if "accepted_versions" in e["inputs"]:
        if tuple(e["inputs"]["probe"]) != PYYAML_PROBE:
            raise EntryFailed(f"{e['id']}: the plan's probe argv differs from the module literal PYYAML_PROBE")
        probe = list(PYYAML_PROBE)
        out = ctx.runner.run(probe)
        version = out.stdout.strip()
        argvs.append(probe)
        if out.returncode != 0 or version not in e["inputs"]["accepted_versions"]:
            raise EntryFailed(f"{package}: the system PyYAML {version or '(none)'} is not in accepted_versions "
                              f"{e['inputs']['accepted_versions']} (verify_time_refusals)")
        ctx.pyyaml_version = version
        obs["pyyaml_version"] = version
    return _result(e, "observed", observations=obs, argv=argvs)


def ex_precondition(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    if e["id"] == "precondition:mcp-allowlist":
        if e["id"] in ctx.plan["blocking_preconditions"]:
            raise EntryFailed("precondition:mcp-allowlist is unmet")
        return _result(e, "observed", observations={"observed": "met"})
    path = e["path"]
    try:
        obs = ctx.host.observe(path)
    except ApplyRefused as exc:
        raise EntryFailed(str(exc)) from None
    if not obs["exists"] or obs["type"] != "dir":
        raise EntryFailed(f"{path}: the mailbox hub must exist as a real directory before the executor unit (D33)")
    return _result(e, "observed", observations={"hub": "present, no symlink component"})


def ex_journal_schema(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    """From the framework copy once it exists (verify), else the repo (P0 precedes the copy)."""

    copy = ctx.host.path(ctx.plan["framework"]["root"])
    source = copy if (copy / e["inputs"]["module"]).is_file() else ctx.repo_root
    got = H._literal(source, e["inputs"]["module"], "SCHEMA_DIGEST")
    if got != e["inputs"]["schema_digest"]:
        raise EntryFailed(f"{e['inputs']['module']}: SCHEMA_DIGEST drifted from the plan (never migrate, Q10)")
    return _result(e, "observed", observations={"schema_digest": got})


def _fw(ctx: Ctx) -> tuple[str, str, str]:
    fw = ctx.plan["framework"]
    return fw["root"], posixpath.dirname(fw["root"]), fw["rev"]


def ex_framework_tree(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    root, base, rev = _fw(ctx)
    files = [x for x in ctx.plan["entries"] if x["kind"] == "framework_file"]
    obs = ctx.host.observe(root)
    if obs["exists"]:
        bad = [f["path"] for f in files if ctx.host.observe(f["path"]).get("sha256") != f["content_sha256"]]
        if obs["type"] != "dir" or bad:
            raise EntryFailed(f"{root}: the content-addressed rev exists but differs ({bad[:3]}); "
                              "a rev is never rewritten in place")
        return _result(e, "converged", before=obs, after_sha256=e["content_sha256"])
    digest = hashlib.sha256()
    for f in sorted(files, key=lambda x: x["path"]):
        data = ctx.contents.get(f["id"])
        if data is None or sha256_bytes(data) != f["content_sha256"]:
            raise EntryFailed(f"{f['id']}: no pinned bytes for this framework file")
        rel = f["path"][len(root) + 1:]
        digest.update(rel.encode("utf-8") + b"\0" + data + b"\0")
    if digest.hexdigest() != e["content_sha256"]:
        raise EntryFailed("the framework bytes do not reproduce the plan's closure_sha256")
    if not ctx.execute:
        return _result(e, "would-create", before=obs, after_sha256=e["content_sha256"])
    partial = posixpath.join(base, f".{rev}.partial")
    if ctx.host.exists(partial):
        raise EntryFailed(f"{partial}: a stale partial tree exists; inspect it by hand")
    record = _result(e, "failed", before=obs, observations={"files": len(files), "base": base, "partial": partial})
    with _guard(record):
        ctx.host.mkdir(partial, "root", "root", "0755")
        made = {partial}
        for f in sorted(files, key=lambda x: x["path"]):
            rel = f["path"][len(root) + 1:]
            parts = rel.split("/")
            for i in range(1, len(parts)):
                d = posixpath.join(partial, *parts[:i])
                if d not in made:
                    if not ctx.host.exists(d):
                        ctx.host.mkdir(d, "root", "root", "0755")
                    made.add(d)
            ctx.host.write_file(posixpath.join(partial, rel), ctx.contents[f["id"]], "root", "root", "0644")
        ctx.host.rename(partial, root)  # written whole, then visible under <rev> in one rename (Q6)
    ctx.framework_created = True
    record.update(action="created", after_sha256=e["content_sha256"])
    return record


def ex_framework_file(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    obs = ctx.host.observe(e["path"])
    if not ctx.execute and not obs["exists"]:
        return _result(e, "would-observe", observations={"note": "written by the framework tree entry"})
    if obs.get("sha256") != e["content_sha256"] or _meta(obs) != ("root", "root", "0644"):
        raise EntryFailed(f"{e['path']}: framework file differs after the tree write")
    return _result(e, "observed", after_sha256=e["content_sha256"])


def ex_active_link(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    _root, _base, rev = _fw(ctx)
    obs = ctx.host.observe(e["path"])
    if obs["exists"] and obs["type"] != "symlink":
        raise EntryFailed(f"{e['path']}: exists and is not a symlink")
    if obs.get("link_target") == rev:
        return _result(e, "converged", before=obs, after_sha256=e["content_sha256"])
    if not ctx.execute:
        return _result(e, "would-switch", before=obs)
    record = _result(e, "failed", before=obs, after_sha256=e["content_sha256"],
                     observations={"previous": obs.get("link_target"), "keep_previous": 1})
    with _guard(record):
        ctx.host.symlink_replace(e["path"], rev)
    record["action"] = "replaced" if obs["exists"] else "created"
    return record


def ex_launcher(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    return _install_file(ctx, e, render_launcher(e, ctx.plan["framework"]["rev"]), "root", "root", "0755")


def _runtime_paths(e: Mapping[str, Any]) -> tuple[str, str]:
    return e["path"], e["source"]["launcher"]


def runtime_entries(host: FixtureHost, prefix: str, launcher: str) -> dict[str, str]:
    """build_runtime.manifest_entries, over host paths (relative to /)."""

    out: dict[str, str] = {}
    manifest = posixpath.join(prefix, "BUILD-MANIFEST.json")
    top = host.path(prefix)
    if host.path(launcher).is_file():
        out[launcher.lstrip("/")] = "sha256:" + sha256_bytes(host.read_bytes(launcher))
    if top.is_dir():
        for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
            for name in sorted(dirnames + filenames):
                full = Path(dirpath) / name
                hostp = "/" + full.relative_to(host.root).as_posix()
                if hostp == manifest:
                    continue
                if full.is_symlink():
                    out[hostp.lstrip("/")] = "symlink:" + os.readlink(full)
                elif full.is_file():
                    out[hostp.lstrip("/")] = "sha256:" + sha256_bytes(host.read_bytes(hostp))
    return out


def _runtime_built(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any] | None:
    prefix, launcher = _runtime_paths(e)
    manifest = posixpath.join(prefix, "BUILD-MANIFEST.json")
    if not ctx.host.path(manifest).is_file():
        return None
    doc = json.loads(ctx.host.read_bytes(manifest))
    lock = posixpath.join(ctx.plan["framework"]["root"], "tools/host_mcp_adapter/dist/requirements.lock")
    if not ctx.host.path(lock).is_file() or doc.get("requirements_lock_sha256") != sha256_bytes(ctx.host.read_bytes(lock)):
        return None
    current = runtime_entries(ctx.host, prefix, launcher)
    if doc.get("files") != current:
        return None
    return {"tree_sha256": sha256_bytes(canonical(current).encode("ascii")), "manifest_sha256":
            sha256_bytes(ctx.host.read_bytes(manifest))}


def lock_pinned_hashes(lock_text: str) -> frozenset[str]:
    """Every ``--hash=sha256:<hex>`` in requirements.lock (pip --require-hashes)."""

    import re

    return frozenset(re.findall(r"--hash=sha256:([0-9a-f]{64})", lock_text))


def wheelhouse_manifest(ctx: Ctx, lock_host_path: str) -> list[dict[str, str]]:
    """Recovery D15 (a): the wheelhouse as a sorted name+sha manifest; every wheel must be
    pinned by the lock (which the plan's build-input digest covers)."""

    assert ctx.inputs is not None
    wh = ctx.inputs.wheelhouse
    top = ctx.host.path(wh)
    if top.is_symlink() or not top.is_dir():
        raise EntryFailed("inputs.wheelhouse: must be a real directory")
    pinned = lock_pinned_hashes(ctx.host.read_bytes(lock_host_path).decode("utf-8"))
    out = []
    for name in sorted(os.listdir(top)):
        hp = posixpath.join(wh, name)
        st = os.lstat(top / name)
        if not stat.S_ISREG(st.st_mode):
            raise EntryFailed(f"wheelhouse entry {name}: not a regular file")
        sha = sha256_bytes(ctx.host.read_bytes(hp))
        if sha not in pinned:
            raise EntryFailed(f"wheelhouse entry {name}: sha256 {sha} is not pinned by requirements.lock")
        out.append({"name": name, "sha256": sha})
    if not out:
        raise EntryFailed("inputs.wheelhouse: empty")
    return out


def _root_owned_no_gw(ctx: Ctx, host_path: str) -> None:
    """Recovery D15 (b): lstat (no-follow) in Python: root-owned, not group/other-writable."""

    try:
        st = os.lstat(ctx.host.path(host_path))
    except FileNotFoundError:
        raise EntryFailed(f"{host_path}: missing at exec time") from None
    if stat.S_ISLNK(st.st_mode):
        raise EntryFailed(f"{host_path}: a symlink at exec time")
    if st.st_uid != ctx.host.uid_of("root") or ctx.host.owner_of(host_path)[0] != "root":
        raise EntryFailed(f"{host_path}: not root-owned at exec time")
    if st.st_mode & 0o022:
        raise EntryFailed(f"{host_path}: group- or other-writable at exec time")


def _framework_components(fw_root: str) -> list[str]:
    parts = fw_root.strip("/").split("/")
    return ["/"] + ["/" + "/".join(parts[:i]) for i in range(1, len(parts) + 1)]


def ex_runtime(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    key = e["source"]["adapter"]
    fixed = H.RUNTIME_ADAPTERS.get(key)
    if fixed is None or (e["path"], e["source"]["launcher"]) != (fixed["prefix"], fixed["launcher"]):
        raise EntryFailed(f"{e['id']}: --adapter must be one of {sorted(H.RUNTIME_ADAPTERS)} with its fixed "
                          "output literals (Recovery D15)")
    fw_root = ctx.plan["framework"]["root"]
    covered = list(e["inputs"]["build_inputs"])
    files = {x["path"][len(fw_root) + 1:]: x for x in ctx.entries_of("framework_file")}
    digest = hashlib.sha256()
    for rel in covered:
        if rel not in files:
            raise EntryFailed(f"{e['id']}: build input {rel} is not in the framework closure")
        if ctx.host.path(posixpath.join(fw_root, rel)).is_file():
            data = ctx.host.read_bytes(posixpath.join(fw_root, rel))
        else:  # dry run before the copy exists: the pinned bytes the copy will hold
            data = ctx.contents.get(files[rel]["id"], b"")
        digest.update(rel.encode() + b"\0" + data + b"\0")
    if digest.hexdigest() != e["content_sha256"]:
        raise EntryFailed(f"{e['id']}: the build inputs in the framework copy differ from the plan's digest")
    built = _runtime_built(ctx, e)
    if built is not None:
        return _result(e, "converged", after_sha256=built["tree_sha256"], observations=built)
    prefix, launcher = _runtime_paths(e)
    argv = ["/usr/bin/env", "-i", *BUILD_ENV, H.PYTHON3, "-I", posixpath.join(fw_root, H.BUILD_RUNTIME_REL),
            "--root", "/", "--wheelhouse", ctx.inputs.wheelhouse if ctx.inputs else "<inputs.wheelhouse>",
            "--python", H.PYTHON3, "--adapter", key]
    before = {"prefix": ctx.host.observe(prefix), "launcher": ctx.host.observe(launcher)}
    for obs in before.values():
        obs.pop("sha256", None)
    if not ctx.execute:
        return _result(e, "would-build", before=before, argv=[argv])
    # Review MED-2: the record (and its before-state) exists BEFORE the first mutation;
    # the move-aside list and created parents are appended to it as each lands.
    moved: list[dict[str, str]] = []
    created_parents: list[str] = []
    before["moved_aside"] = moved
    before["created_parents"] = created_parents
    record = _result(e, "failed", before=before, argv=[argv], paths={"prefix": prefix, "launcher": launcher})
    with _guard(record):
        for what, path in (("prefix", prefix), ("launcher", launcher)):
            if before[what]["exists"]:  # an existing, non-matching build is moved aside, never overwritten
                aside = posixpath.join(ctx.rcd, "saved", f"runtime-{e['source']['adapter']}-{what}")
                ctx.host.rename(path, aside)
                moved.append({"path": path, "aside": aside})
        # create missing ancestors HERE (root 0755, recorded) instead of letting the
        # build create them invisibly to rollback
        for path in (prefix, launcher):
            missing = []
            parent = posixpath.dirname(path)
            while not ctx.host.exists(parent):
                missing.append(parent)
                parent = posixpath.dirname(parent)
            for d in reversed(missing):
                ctx.host.mkdir(d, "root", "root", "0755")
                created_parents.append(d)
        wheels = wheelhouse_manifest(ctx, posixpath.join(fw_root, "tools/host_mcp_adapter/dist/requirements.lock"))
        # Recovery D15 (b), literally (review L1): immediately before exec, every component of
        # the framework path, EVERY intermediate dir down to each covered file, and each
        # covered file is root-owned and not group/other-writable
        for path in _exec_stat_paths(fw_root, covered):
            _root_owned_no_gw(ctx, path)
        # Recovery D15 (f): an explicit root-owned cwd (the concrete rev, stat-checked above),
        # never the operator's; build_runtime.py runs every child interpreter with -I
        record["observations"] = {"covered": {"files": covered, "wheelhouse": wheels}, "env": list(BUILD_ENV),
                                  "cwd": fw_root}
        res = ctx.runner.run(argv, cwd=fw_root)
        if res.returncode != 0:
            raise _PartialEntry(record, f"build_runtime exited {res.returncode}")
        built = _runtime_built(ctx, e)
        if built is None:
            raise _PartialEntry(record, f"{prefix}: build_runtime output does not match its BUILD-MANIFEST")
        for path in (prefix, launcher):
            if ctx.host.owner_of(path) != ("root", "root"):
                raise _PartialEntry(record, f"{path}: runtime must be root-owned (verify_artifact --expect-owner 0)")
    record["observations"].update(built)
    record.update(action="created" if not moved else "replaced", after_sha256=built["tree_sha256"])
    return record


def _exec_stat_paths(fw_root: str, covered: Sequence[str]) -> list[str]:
    """Every framework path component, every intermediate dir under fw_root down to
    each covered file, and the covered files themselves (sorted, de-duplicated)."""

    out = list(_framework_components(fw_root))
    seen = set(out)
    for rel in covered:
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):
            path = posixpath.join(fw_root, *parts[:i])
            if path not in seen:
                seen.add(path)
                out.append(path)
    return out


def _fixture_loader(ctx: Ctx, roots: Sequence[str]):
    from tools.host_control.trusted_files import RootOwnedFileLoader

    return RootOwnedFileLoader._open_for_test(tuple(ctx.host.path(r) for r in roots), expected_owner_uid=os.getuid())


def _render_broker_config(ctx: Ctx, e: Mapping[str, Any]) -> bytes:
    kv = e["inputs"]["known_values"]
    if kv.get("compensate") is not False:
        raise EntryFailed("broker config: compensate must stay false for the first fire (HO Q4)")
    assert ctx.inputs is not None
    doc = {
        "schema": e["inputs"]["schema"], "trusted_roots": kv["trusted_roots"], "registry_path": kv["registry_path"],
        "policy_path": kv["policy_path"], "framework_root": kv["framework_root"], "journal_path": kv["journal_path"],
        "approval_uid": kv["approval_uid"], "monitor_uids": kv["monitor_uids"],
        "host_operation_families": list(ctx.inputs.host_operation_families),
        "plan_ttl_seconds": ctx.inputs.plan_ttl_seconds, "broker_revision": kv["broker_revision"], "compensate": False,
    }
    return (canonical(doc) + "\n").encode("ascii")


def _render_approval_config(ctx: Ctx, e: Mapping[str, Any]) -> bytes:
    kv = e["inputs"]["known_values"]
    assert ctx.inputs is not None
    ident = str(ctx.inputs.telegram_user_id)
    doc = {
        "schema": e["inputs"]["schema"], "trusted_roots": kv["trusted_roots"],
        "approver_policy_path": kv["approver_policy_path"], "journal_path": kv["journal_path"],
        "broker_uid": kv["broker_uid"], "monitor_uids": kv["monitor_uids"],
        "human_ingress": {"ingress_uid": kv["human_ingress.ingress_uid"], "human_subject": ctx.inputs.human_subject,
                          "provider_user_id": ident, "provider_chat_id": ident},
    }
    return (canonical(doc) + "\n").encode("ascii")


def _validate_u2(ctx: Ctx, e: Mapping[str, Any]) -> None:
    """Parse the INSTALLED bytes with U2's own loader (fixture loader)."""

    from tools.host_control.contracts import RegistryError

    kv = e["inputs"]["known_values"]
    path = ctx.host.path(e["path"])
    try:
        if e["inputs"]["schema"] == "pantheon-host-broker-config/v1":
            from tools.host_control.broker_config import load_broker_config

            with _fixture_loader(ctx, [posixpath.dirname(e["path"])]) as files:
                cfg = load_broker_config(path, files=files)
            if cfg.compensate or sorted(cfg.monitor_uids) != kv["monitor_uids"] or str(cfg.framework_root) != kv["framework_root"]:
                raise EntryFailed("broker config: the loaded values differ from the plan's known values")
        else:
            from tools.host_control.approval.config import load_approval_config

            with _fixture_loader(ctx, [posixpath.dirname(e["path"])]) as files:
                cfg = load_approval_config(path, files=files)
            if sorted(cfg.monitor_uids) != kv["monitor_uids"]:
                raise EntryFailed("approval config: monitor_uids differ from the plan")
    except RegistryError as exc:
        raise EntryFailed(f"{e['path']}: refused by its U2 loader: {exc}") from None


def ex_config(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    action = e["source"]["action"]
    if action == "render-at-apply":
        return ex_ingress_config(ctx, e)
    if e.get("content_sha256"):
        data = ctx.contents.get(e["id"])
        if data is None or sha256_bytes(data) != e["content_sha256"]:
            raise EntryFailed(f"{e['id']}: no pinned bytes")
        doc = json.loads(data)
        if isinstance(doc, dict) and doc.get("schema") == H.EXECUTOR_CONFIG_SCHEMA:
            from tools.host_control.contracts import RegistryError
            from tools.host_control.executor.config import parse_executor_config

            try:
                parse_executor_config(doc)
            except RegistryError as exc:
                raise EntryFailed(f"executor config refused by its parser: {exc}") from None
        return _install_file(ctx, e, data, e["owner"], e["group"], e["mode"])
    schema = e["inputs"]["schema"]
    data = _render_broker_config(ctx, e) if schema == "pantheon-host-broker-config/v1" else _render_approval_config(ctx, e)
    result = _install_file(ctx, e, data, e["owner"], e["group"], e["mode"])
    if ctx.execute:
        _validated(result, lambda: _validate_u2(ctx, e))
    return result


def _validated(result: dict[str, Any], check: Callable[[], None]) -> None:
    """A post-install validation failure is a PARTIAL entry: the bytes are on
    disk, so the receipt must carry the before-state for rollback."""

    try:
        check()
    except EntryFailed as exc:
        if result["action"] in ("created", "replaced"):
            result["action"] = "failed"
            raise _PartialEntry(result, str(exc)) from None
        raise


def ex_ingress_config(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    template_rel = e["source"]["template"]
    template = (ctx.repo_root / template_rel).read_bytes()
    if sha256_bytes(template) != e["inputs"]["template_sha256"]:
        raise EntryFailed(f"{template_rel}: drifted from the plan's template_sha256")
    name = posixpath.basename(template_rel)
    resolved = dict(e["inputs"]["resolved"])
    partial = H.render_template(name, template.decode("utf-8"), resolved, allow_deferred=True)
    if sha256_bytes(partial.encode("utf-8")) != e["inputs"]["partial_render_sha256"]:
        raise EntryFailed("ingress config: the partial render differs from the plan")
    assert ctx.inputs is not None
    values = {**resolved, "ALLOWLISTED_TELEGRAM_USER_ID": ctx.inputs.telegram_user_id,
              "ALLOWLISTED_TELEGRAM_CHAT_ID": ctx.inputs.telegram_user_id}
    text = H.render_template(name, template.decode("utf-8"), values)
    from tools.host_control.telegram_ingress.config import IngressConfigError, parse_ingress_config

    try:
        parse_ingress_config(json.loads(text))
    except IngressConfigError as exc:
        raise EntryFailed(f"ingress config refused by its parser: {exc}") from None
    return _install_file(ctx, e, text.encode("utf-8"), e["owner"], e["group"], e["mode"])


def _source_bytes(ctx: Ctx, host_path: str, what: str) -> bytes:
    try:
        return ctx.host.read_bytes(host_path)
    except (OSError, ApplyRefused) as exc:
        raise EntryFailed(f"{what}: the input source is unreadable ({type(exc).__name__})") from None


def ex_registry(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    if e.get("content_sha256"):  # the seat registry, pre-registration
        data = ctx.contents[e["id"]]
        obs = ctx.host.observe(e["path"])
        if obs.get("type") == "file" and obs["sha256"] != sha256_bytes(data):
            current = json.loads(ctx.host.read_bytes(e["path"]))
            if isinstance(current, dict) and "plans" in current and {
                    k: v for k, v in current.items() if k != "plans"} == json.loads(data):
                # already past registration with these seats: P6 owns the plans list
                return _result(e, "converged", before=obs, observations={"stage": "post-registration"})
        return _install_file(ctx, e, data, e["owner"], e["group"], e["mode"])
    assert ctx.inputs is not None
    from tools.host_control.contracts import HostControlError, RegistryError

    if e["inputs"]["schema"] == "pantheon-host-policy/v1":
        data = _source_bytes(ctx, ctx.inputs.broker_policy_source, "operator_batch.3.broker_policy_source")
    else:
        data = _source_bytes(ctx, ctx.inputs.approver_policy_source, "operator_batch.3.approver_policy_source")
    result = _install_file(ctx, e, data, e["owner"], e["group"], e["mode"])

    def check() -> None:
        try:
            with _fixture_loader(ctx, [posixpath.dirname(e["path"])]) as files:
                if e["inputs"]["schema"] == "pantheon-host-policy/v1":
                    from tools.host_control.policy import HostPolicy

                    HostPolicy.load(ctx.host.path(e["path"]), files=files)
                else:
                    from tools.host_control.approval.policy import load_approver_policy

                    load_approver_policy(files, ctx.host.path(e["path"]))
        except (RegistryError, HostControlError) as exc:
            raise EntryFailed(f"{e['path']}: refused by its loader: {exc}") from None

    if ctx.execute:
        _validated(result, check)
    result["observations"]["restart_after"] = e["source"].get("restart_after")
    return result


def ex_registered_manifest(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    assert ctx.inputs is not None
    rev = e["id"].split(":", 1)[1]
    src = ctx.inputs.registered_manifests.get(rev)
    if src is None:
        raise EntryFailed(f"inputs.registered_manifests has no source for revision {rev}")
    data = _source_bytes(ctx, src, f"registered_manifests.{rev}")
    if sha256_bytes(data) != e["content_sha256"]:
        raise EntryFailed(f"registered manifest {rev}: source bytes differ from the registered sha256")
    return _install_file(ctx, e, data, e["owner"], e["group"], e["mode"])


def ex_credential_ref(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the CredentialRef exactly as the ingress daemon will
    (telegram_ingress.main.credential_ref) and pin it against the plan."""

    from tools.host_control.telegram_ingress.config import parse_ingress_config
    from tools.host_control.telegram_ingress.main import credential_ref

    cfg_entry = next(x for x in ctx.plan["entries"] if x["source"].get("action") == "render-at-apply")
    if not ctx.host.path(cfg_entry["path"]).is_file():
        if not ctx.execute:
            return _result(e, "would-observe")
        raise EntryFailed("credential ref: the ingress config is not installed")
    ref = credential_ref(parse_ingress_config(json.loads(ctx.host.read_bytes(cfg_entry["path"]))))
    want = e["inputs"]
    got = {"credential_id": ref.credential_id, "location": str(ref.location), "owner_uid": ref.owner_uid}
    if got != {k: want[k] for k in ("credential_id", "location", "owner_uid")}:
        raise EntryFailed(f"credential ref differs from the plan: {got}")
    if ref.host_file is None or ref.host_file.parent_mode != 0o700 or ref.mode != 0o600:
        raise EntryFailed("credential ref: the token is 0600 under a 0700 parent")
    return _result(e, "observed", observations={**got, "mode": "0600", "parent_mode": "0700", "kind": ref.kind,
                                                "provider": ref.provider})


def _check_token_source(ctx: Ctx, st: os.stat_result, src: str, what: str) -> None:
    """Recovery D15: a regular file, root- or operator-owned, not g/o-readable, <= 4096 bytes.
    Messages never carry the bytes."""

    assert ctx.inputs is not None
    if not stat.S_ISREG(st.st_mode):
        raise EntryFailed(f"token source ({what}): not a regular file (a symlink or special file is refused)")
    if st.st_uid != ctx.host.uid_of("root") or ctx.host.owner_uid(src) not in (0, ctx.inputs.operator_uid):
        raise EntryFailed(f"token source ({what}): must be owned by root or the operator uid")
    if st.st_mode & 0o044:
        raise EntryFailed(f"token source ({what}): must not be group- or other-readable")
    if st.st_size > TOKEN_MAX_BYTES:
        raise EntryFailed(f"token source ({what}): larger than {TOKEN_MAX_BYTES} bytes")


def _observe_existing_token(ctx: Ctx, e: Mapping[str, Any], dfd: int, name: str) -> dict[str, Any]:
    st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode):
        raise EntryFailed(f"{e['path']}: an existing non-regular file or planted symlink is refused")
    obs = ctx.host.observe(e["path"], hash_content=False)
    if _meta(obs) != (e["owner"], e["group"], e["mode"]):
        raise EntryFailed(f"{e['path']}: an existing credential has the wrong owner/mode; rotation is out of scope")
    return _result(e, "observed", before=obs, observations={"note": "pre-existing credential kept (create-only)"})


def ex_credential(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    """Create the token file IN-PROCESS (Recovery D15; no install argv): an O_NOFOLLOW
    dir fd on the ingress-owned 0700 parent, an O_EXCL|O_NOFOLLOW create through it,
    and a source that is lstat-checked, then opened O_NOFOLLOW and re-checked on the fd.
    The bytes live only in process memory: never logged, hashed, repr'd, raised or
    persisted (no sha of the token is ever computed or recorded)."""

    path, owner, group = e["path"], e["owner"], e["group"]
    if not (_ACCOUNT_RE.fullmatch(owner) and _ACCOUNT_RE.fullmatch(group)):
        raise EntryFailed(f"{e['id']}: the ingress owner is not an account name")
    parent, name = posixpath.split(path)
    try:
        dfd = os.open(ctx.host.path(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise EntryFailed(f"{parent}: the credentials parent must be a real directory (no symlink)") from None
    try:
        pst = os.fstat(dfd)
        if (not stat.S_ISDIR(pst.st_mode) or pst.st_uid != ctx.host.uid_of(owner)
                or ctx.host.owner_of(parent) != (owner, group) or _mode_str(pst.st_mode) != "0700"):
            raise EntryFailed(f"{parent}: must be a directory {owner}:{group} 0700")
        try:
            os.stat(name, dir_fd=dfd, follow_symlinks=False)
            return _observe_existing_token(ctx, e, dfd, name)
        except FileNotFoundError:
            pass
        if not ctx.execute:
            return _result(e, "would-create", before={"exists": False}, observations={"method": "in-process O_EXCL"})
        assert ctx.inputs is not None
        src = ctx.inputs.token_source_path
        try:
            lst = os.lstat(ctx.host.path(src))
        except (OSError, ApplyRefused):
            raise EntryFailed("token source: missing or not a fixture path") from None
        _check_token_source(ctx, lst, src, "lstat")
        if ctx.host.race_hook is not None:
            ctx.host.race_hook()
        try:
            sfd = os.open(ctx.host.path(src), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            raise EntryFailed("token source: refused at open (became a symlink or vanished)") from None
        try:
            fst = os.fstat(sfd)
            _check_token_source(ctx, fst, src, "fstat")
            if (fst.st_dev, fst.st_ino) != (lst.st_dev, lst.st_ino):
                raise EntryFailed("token source: swapped between lstat and open")
            # Review MED-2: the record exists before the create; everything after the
            # create runs inside the guard (incl. fsync(dfd) and the landing check)
            record = _result(e, "failed-before-mutation", before={"exists": False},
                             observations={"method": "in-process O_EXCL|O_NOFOLLOW create via dir_fd"})
            ctx.host.precheck()
            try:
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600,
                             dir_fd=dfd)
            except FileExistsError:  # an existing file or a planted link: observe-only
                return _observe_existing_token(ctx, e, dfd, name)
            record["action"] = "failed"
            with _guard(record):
                ctx.host._mutate("create", path)
                try:
                    buf = os.read(sfd, TOKEN_MAX_BYTES + 1)
                    if len(buf) > TOKEN_MAX_BYTES:
                        buf = None
                        raise _PartialEntry(record, f"token source: larger than {TOKEN_MAX_BYTES} bytes")
                    view = memoryview(buf)
                    while view:
                        view = view[os.write(fd, view):]
                    del view
                    buf = None  # drop the only reference to the secret bytes
                    ctx.host.fchown(fd, path, owner, group)
                    os.fchmod(fd, 0o600)
                    os.fsync(fd)
                except OSError:
                    raise _PartialEntry(record, "token write failed") from None
                finally:
                    os.close(fd)
                os.fsync(dfd)
                after = ctx.host.observe(path, hash_content=False)
                if after.get("type") != "file" or _meta(after) != (owner, group, e["mode"]):
                    raise _PartialEntry(record, "credential did not land 0600 as the ingress account")
        finally:
            os.close(sfd)
    finally:
        os.close(dfd)
    record["action"] = "created"
    return record


def ex_activation(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    return _install_file(ctx, e, ctx.contents[e["id"]], e["owner"], e["group"], e["mode"])


def _seat_from_argv(argv: Sequence[str]) -> dict[str, Any]:
    return json.loads(argv[list(argv).index("--seat-record") + 1])


def _registry_entry(ctx: Ctx) -> dict[str, Any]:
    return next(x for x in ctx.plan["entries"] if x["kind"] == "registry" and x.get("content_sha256"))


def _registrations_converged(ctx: Ctx) -> bool:
    """Plans are re-registered on EVERY framework-copy update (T-5533 d); they are
    converged only if this rev pre-existed and every plan is recorded and present."""

    if ctx.framework_created:
        return False
    reg = _registry_entry(ctx)
    if not ctx.host.path(reg["path"]).is_file():
        return False
    current = json.loads(ctx.host.read_bytes(reg["path"]))
    plans = {r["path"]: r["sha256"] for r in current.get("plans", []) if isinstance(r, dict)} if isinstance(current, dict) else {}
    for e in ctx.entries_of("plan_registration"):
        obs = ctx.host.observe(e["path"])
        if plans.get(e["path"]) is None or obs.get("sha256") != plans[e["path"]]:
            return False
    return True


def ex_plan_registration(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    """T-5533 a-d with U1's PRODUCER_ROOT_CHECKS implemented literally."""

    argv = list(e["source"]["producer_argv"])
    if _registrations_converged(ctx):
        rec = {"seat_id": _seat_from_argv(argv)["seat_id"], "path": e["path"],
               "sha256": ctx.host.observe(e["path"])["sha256"]}
        ctx.registry_records.append({**rec, "manifest_revision": e["id"].rsplit(":", 1)[1]})
        return _result(e, "converged", after_sha256=rec["sha256"])
    if not ctx.execute:
        return _result(e, "would-produce", argv=[argv])
    out = e["inputs"]["producer_writes"]
    producer_cwd = e["source"].get("producer_cwd")
    if producer_cwd != ctx.plan["framework"]["root"]:
        raise EntryFailed(f"{e['id']}: producer_cwd must be the concrete framework rev")
    for path in _framework_components(producer_cwd):  # root-owned, readable by nobody, not writable by it
        _root_owned_no_gw(ctx, path)
    record = _result(e, "failed", before=ctx.host.observe(e["path"]), argv=[argv],
                     observations={"producer_out": out, "cwd": producer_cwd})
    with _guard(record):
        return _plan_registration_body(ctx, e, argv, out, record, producer_cwd)


def _plan_registration_body(ctx: Ctx, e: Mapping[str, Any], argv: list[str], out: str, record: dict[str, Any],
                            producer_cwd: str) -> dict[str, Any]:
    from tools.host_control.registry import PLAN_MAX_BYTES
    from tools.persona_installer_contracts import InstallPlan

    res = ctx.runner.run(argv, cwd=producer_cwd)  # review L3: the plan's producer_cwd, recorded
    if res.returncode != 0:
        raise _PartialEntry(record, f"host_plan_producer exited {res.returncode}")
    try:
        printed = json.loads(res.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise _PartialEntry(record, "the producer printed no plan record") from None
    seat = _seat_from_argv(argv)
    revision = e["id"].rsplit(":", 1)[1]
    if (printed.get("path") != out or printed.get("seat_id") != seat["seat_id"]
            or printed.get("manifest_revision") != revision):
        raise _PartialEntry(record, "the producer's printed record does not name this seat, revision and --out")
    producer_user, producer_group = e["inputs"]["expected_writer"].split(":")
    # 1. open as root, O_RDONLY|O_NOFOLLOW; refuse a symlink or a non-regular file
    try:
        fd = os.open(ctx.host.path(out), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise _PartialEntry(record, f"producer output refused at open: {exc.strerror}") from None
    try:
        st = os.fstat(fd)
        # 2. fstat: owner is the producer user, mode 0644, size <= PLAN_MAX_BYTES
        if not stat.S_ISREG(st.st_mode):
            raise _PartialEntry(record, "producer output is not a regular file")
        if st.st_uid != ctx.host.uid_of(producer_user) or ctx.host.owner_of(out)[0] != producer_user:
            raise _PartialEntry(record, "producer output is not owned by the producer user")
        if _mode_str(st.st_mode) != "0644" or st.st_size > PLAN_MAX_BYTES:
            raise _PartialEntry(record, "producer output mode/size refused")
        data = b""
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            data += chunk
            if len(data) > PLAN_MAX_BYTES:
                raise _PartialEntry(record, "producer output exceeds PLAN_MAX_BYTES")
    finally:
        os.close(fd)
    # 3. sha256 of the bytes read == the printed record
    if sha256_bytes(data) != printed.get("sha256"):
        raise _PartialEntry(record, "producer output sha256 differs from its printed record")
    # 4. InstallPlan.from_dict(bytes).validate_digest() and the target equals the seat record
    try:
        plan = InstallPlan.from_dict(json.loads(data))
    except (ValueError, KeyError, TypeError) as exc:
        raise _PartialEntry(record, f"producer output is not an InstallPlan: {type(exc).__name__}") from None
    if not plan.validate_digest():
        raise _PartialEntry(record, "producer output fails validate_digest")
    target = {"instance": plan.instance, "role": plan.role, "persona": plan.persona, "account": plan.account,
              "selected_harness": plan.selected_harness, "root": plan.root}
    if target != {"instance": seat["instance"], "role": seat["role"], "persona": seat["persona"],
                  "account": seat["account"], "selected_harness": seat["harness"], "root": seat["install_root"]}:
        raise _PartialEntry(record, "producer output targets a different seat")
    # 5. install those SAME bytes as root:root 0644, then re-hash the installed file
    before = ctx.host.observe(e["path"])
    if before["exists"]:
        record["before"] = {**before, "saved": ctx.save_copy(e["path"])}
    ctx.host.write_file(e["path"], data, "root", "root", "0644")
    if ctx.host.observe(e["path"]).get("sha256") != printed["sha256"]:
        raise _PartialEntry(record, "the installed plan re-hash differs")
    ctx.registry_records.append({"seat_id": seat["seat_id"], "manifest_revision": revision, "path": e["path"],
                                 "sha256": printed["sha256"]})
    # (the producer's own staging file is removed after registration, with the dir)
    ctx.host.unlink(out)
    record["observations"].update(producer_root_checks=len(H.PRODUCER_ROOT_CHECKS), printed=printed)
    record.update(action="replaced" if before["exists"] else "created", after_sha256=printed["sha256"])
    return record


def ex_registry_post(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    reg = _registry_entry(ctx)
    pre = json.loads(ctx.contents[reg["id"]])
    records = sorted(ctx.registry_records, key=lambda r: (r["seat_id"], r["manifest_revision"]))
    doc = {**pre, "plans": [{k: r[k] for k in ("seat_id", "manifest_revision", "path", "sha256")} for r in records]}
    data = (canonical(doc) + "\n").encode("ascii")
    record = _install_file(ctx, e, data, e["owner"], e["group"], e["mode"])
    if ctx.execute:
        staging = next(x for x in ctx.entries_of("dir") if x["source"].get("must_not_exist"))
        if ctx.host.exists(staging["path"]):
            try:
                ctx.host.rmdir(staging["path"])  # remove the staging dir after registration
            except Exception as exc:  # noqa: BLE001 - surfaced as a partial entry with this record
                # the record keeps its own action: a created/replaced registry is rolled back
                # from its saved before-state; a converged one was never touched
                raise _PartialEntry(record, f"staging removal failed: {type(exc).__name__}: {exc}") from None
            record["observations"]["staging_removed"] = staging["path"]
    return record


def ex_journal_root(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    obs = ctx.host.observe(e["path"])
    if not obs["exists"]:
        return _result(e, "observed", before=obs, observations={"state": "pending-start (systemd StateDirectory=)"})
    return _result(e, "observed", before=obs, observations={"state": "present"})


TOOL_EXECUTORS: Final[Mapping[str, Callable[[Ctx, Mapping[str, Any]], dict[str, Any]]]] = {
    "package_verify": ex_package, "precondition": ex_precondition, "journal_schema_check": ex_journal_schema,
    "dir": ex_dir, "runtime": ex_runtime, "framework_tree": ex_framework_tree, "framework_file": ex_framework_file,
    "framework_active_link": ex_active_link, "launcher": ex_launcher, "config": ex_config, "registry": ex_registry,
    "registered_manifest": ex_registered_manifest, "credential_ref_registration": ex_credential_ref,
    "plan_registration": ex_plan_registration, "registry_post_registration": ex_registry_post,
    "activation": ex_activation, "credential": ex_credential, "journal_root": ex_journal_root,
}

def execute_entry(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    if e["source"].get("route") == "family":
        return ex_family(ctx, e)
    return TOOL_EXECUTORS[e["kind"]](ctx, e)


# ---------------------------------------------------------------------------
# refusals before the first action
# ---------------------------------------------------------------------------


def _plan_doc(raw: bytes) -> dict[str, Any]:
    try:
        doc = H.load_json_strict(raw, "plan")
    except H.InstallPlanError as exc:
        raise ApplyRefused(str(exc)) from None
    if not isinstance(doc, dict) or doc.get("schema") != H.PLAN_SCHEMA:
        raise ApplyRefused(f"plan schema must be {H.PLAN_SCHEMA}")
    if not H.verify_plan_digest(doc):
        raise ApplyRefused("plan_sha256 mismatch: the plan bytes do not match their digest")
    return doc


def rebuild_plan(plan: Mapping[str, Any], manifest_raw: bytes, registry_raw: bytes, tracked_raw: bytes | None,
                 *, closure: H.Closure | None = None, repo_root: Path = H.REPO_ROOT
                 ) -> tuple[dict[str, Any], dict[str, bytes], dict[str, bytes], H.InstallManifest, H.SeatRegistryInput]:
    """Rebuild the plan from its inputs; it must be byte-identical (plan_sha256)."""

    try:
        manifest = H.parse_manifest(manifest_raw)
        registry = H.parse_registry(registry_raw)
        tracked = H.parse_tracked_files(tracked_raw) if closure is None else None
        contents: dict[str, bytes] = {}
        doc, staging = H.build_plan(manifest, registry, repo_root, tracked=tracked, closure=closure, contents=contents)
    except H.InstallPlanError as exc:
        raise ApplyRefused(f"plan rebuild refused: {exc}") from None
    if doc["plan_sha256"] != plan["plan_sha256"]:
        raise ApplyRefused("plan_sha256 mismatch: the manifest/registry/repo bytes no longer produce the reviewed plan")
    return doc, staging, contents, manifest, registry


def preflight_refusals(ctx: Ctx, manifest: H.InstallManifest, registry: H.SeatRegistryInput) -> None:
    plan = ctx.plan
    if plan["blocking_preconditions"]:
        raise ApplyRefused(f"blocking_preconditions is not empty: {plan['blocking_preconditions']}")
    # framework_sha256: every registry manifest record against the CLOSURE bytes
    root = plan["framework"]["root"]
    by_rel = {e["path"][len(root) + 1:]: e for e in ctx.entries_of("framework_file")}
    from tools.persona_installer_contracts import FRAMEWORK_CODE_ARTIFACTS

    for rec in registry.manifests:
        digest = hashlib.sha256()
        for rel in sorted((*FRAMEWORK_CODE_ARTIFACTS, *rec["template_ids"])):
            if rel not in by_rel:
                raise ApplyRefused(f"registry manifest {rec['revision']}: {rel} is not in the framework closure")
            digest.update(rel.encode("utf-8") + b"\0" + ctx.contents[by_rel[rel]["id"]] + b"\0")
        if digest.hexdigest() != rec["framework_sha256"]:
            raise ApplyRefused(f"registry manifest {rec['revision']}: framework_sha256 differs from the closure")
    # uid / name collisions against the fixture passwd and group
    passwd, groups = ctx.host.passwd(), ctx.host.groups()
    for e in ctx.entries_of("account"):
        name, uid = e["source"]["params"]["account"], e["inputs"]["uid"]
        holders = [n for n, v in passwd.items() if v["uid"] == uid and n != name]
        gholders = [n for n, g in groups.items() if g == uid and n != name]
        if holders or gholders:
            raise ApplyRefused(f"uid/gid {uid} for {name} collides with {sorted(holders + gholders)} in the fixture")
        if name in passwd and passwd[name]["uid"] != uid:
            raise ApplyRefused(f"account {name} exists with uid {passwd[name]['uid']}, the plan says {uid}")
    # monitor_uids: exactly the dedicated hostmonitor uid (U2 review LOW-3)
    monitor = [e["inputs"]["uid"] for e in ctx.entries_of("account") if e["inputs"].get("role") == "monitor"]
    for e in ctx.entries_of("config"):
        kv = (e.get("inputs") or {}).get("known_values") or {}
        if "monitor_uids" in kv and kv["monitor_uids"] != monitor:
            raise ApplyRefused(f"{e['id']}: monitor_uids must be exactly the hostmonitor uid {monitor}")
    # verify_time_refusals and dpkg (read-only probes); the package set is the literal
    named = sorted(e["inputs"]["package"] for e in ctx.entries_of("package_verify"))
    if named != sorted(VERIFY_PACKAGES):
        raise ApplyRefused(f"package_verify entries must name exactly {sorted(VERIFY_PACKAGES)} (Recovery D15)")
    for e in ctx.entries_of("package_verify"):
        try:
            ex_package(ctx, e)
        except EntryFailed as exc:
            raise ApplyRefused(str(exc)) from None


def _receipt_path_refusals(plan: Mapping[str, Any], receipt: str) -> None:
    _norm_host_path(receipt, "--receipt")
    base = posixpath.dirname(plan["framework"]["root"])
    forbidden = [base] + [e["path"] for e in plan["entries"] if e["kind"] == "journal_root"]
    for root in forbidden:
        if receipt == root or receipt.startswith(root + "/"):
            raise ApplyRefused(f"--receipt must not sit inside {root} (framework tree or a StateDirectory)")
    if receipt.startswith(STATE_DIR_PREFIX):
        raise ApplyRefused("--receipt must not sit inside a StateDirectory (/var/lib/pantheon-host-*)")


# ---------------------------------------------------------------------------
# receipts
# ---------------------------------------------------------------------------


#: T-2920 U4b (Recovery (c), receipt separation): the receipt ``mode`` of each path.
FIXTURE_MODE: Final = "fixture"
REHEARSAL_MODE: Final = "rehearsal-nspawn"
LIVE_MODE: Final = "live"
RECEIPT_MODES: Final = (FIXTURE_MODE, REHEARSAL_MODE, LIVE_MODE)


def require_receipt_mode(doc: Mapping[str, Any], want: str, *, rehearsal: Any = None) -> None:
    """Recovery (c): a receipt only ever serves the path that wrote it. The fixture path wants
    ``fixture`` (a mode-less crash-recovered head counts as fixture HERE ONLY, mason Q5);
    the rehearsal path wants ``rehearsal-nspawn`` AND this machine's attested run; the
    (future, separately reviewed) live path wants ``live``. A rehearsal-nspawn receipt
    copied out can never serve as live idempotency or rollback evidence."""

    if want not in RECEIPT_MODES:
        raise ApplyRefused(f"{want!r} is not a receipt mode")
    mode = doc.get("mode")
    if mode is None and want == FIXTURE_MODE and doc.get("crash_recovered"):
        mode = FIXTURE_MODE
    if mode != want:
        raise ApplyRefused(f"receipt mode {mode!r} cannot serve the {want} path")
    if want == REHEARSAL_MODE:
        att = rehearsal.attestation if rehearsal is not None else None
        rec = doc.get("rehearsal")
        if (att is None or not isinstance(rec, dict)
                or rec != {"machine": att.machine, "run_id": att.run_id, "nonce_sha256": att.nonce_sha256}):
            raise ApplyRefused("rehearsal receipt is not from this attested machine and run")


def _rehearsal_identity(rehearsal: Any) -> dict[str, str]:
    att = rehearsal.attestation
    return {"machine": att.machine, "run_id": att.run_id, "nonce_sha256": att.nonce_sha256}


def _check_rehearsal(rehearsal: Any, root: Any) -> Any:
    """The seam's gate: a lazily imported isinstance check, root exactly '/'."""

    from tools.host_broker_rehearsal_live import RehearsalContext

    if not isinstance(rehearsal, RehearsalContext):
        raise ApplyRefused(f"{type(rehearsal).__name__}: rehearsal must be an attested RehearsalContext")
    if str(root) != "/":
        raise ApplyRefused("a rehearsal runs at root '/' inside its attested machine only")
    return rehearsal


def receipt_digest(doc: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical({k: v for k, v in doc.items() if k != "receipt_sha256"}).encode("ascii"))


def _write_exclusive(host: FixtureHost, host_path: str, data: bytes) -> None:
    host.precheck()
    fd = os.open(host.path(host_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _append_progress(host: FixtureHost, rcd: str, record: Mapping[str, Any]) -> None:
    host.precheck()
    fd = os.open(host.path(posixpath.join(rcd, "progress.jsonl")),
                 os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, (canonical(record) + "\n").encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)


def _tar_create(host: FixtureHost, archive: str, state_dir: str) -> None:
    """Review L5: the archive is created O_EXCL|O_NOFOLLOW at mode 0600 from the start."""

    host.precheck()
    fd = os.open(host.wpath(archive), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "wb") as f, tarfile.open(fileobj=f, mode="w") as tar:
        tar.add(host.wpath(state_dir), arcname=posixpath.basename(state_dir), recursive=True)


def _backup_state_dirs(ctx: Ctx, now: str, label: str) -> list[dict[str, Any]]:
    """Q10: tar every existing /var/lib/pantheon-host-* state dir into RC.d/backups."""

    out = []
    for e in ctx.entries_of("journal_root"):
        obs = ctx.host.observe(e["path"])
        if not obs["exists"] or obs["type"] != "dir":
            continue
        stamp = now.replace(":", "").replace("-", "")
        archive = posixpath.join(ctx.rcd, "backups", f"{label}-{posixpath.basename(e['path'])}-{stamp}.tar")
        _tar_create(ctx.host, archive, e["path"])
        out.append({"state_dir": e["path"], "archive": archive, "sha256": sha256_bytes(ctx.host.read_bytes(archive)),
                    "taken_at": now, "meta": {"owner": obs["owner"], "group": obs["group"], "mode": obs["mode"]}})
    return out


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


@dataclass
class ApplyOutcome:
    rc: int
    receipt: dict[str, Any] | None
    actions: list[dict[str, Any]]


def apply(*, plan_raw: bytes, manifest_raw: bytes, registry_raw: bytes, tracked_raw: bytes | None, inputs_raw: bytes,
          root: Path | str | None, receipt: str, execute: bool, runner_factory: Callable[[FixtureHost], FixtureCommandRunner]
          | None = None, closure: H.Closure | None = None, now: Callable[[], str] = _utc_now,
          host_hook: Callable[[FixtureHost], None] | None = None, rehearsal: Any = None) -> ApplyOutcome:
    """Run (or, without ``execute``, print) the plan under a fixture root.

    ``rehearsal`` (T-2920 U4b): an attested ``RehearsalContext``; the host, runner and
    observation port are then its own, root must be '/', and the receipt mode is
    ``rehearsal-nspawn`` (never ``live``). The CLI never passes it (fixture only)."""

    if root is None or str(root) == "":
        # U3a has NO live mode: no root is a refusal even if the dark switch were flipped
        raise ApplyRefused("a fixture --root is required; U3a has no live mode "
                           f"(LIVE_ACTUATION_ENABLED={H.LIVE_ACTUATION_ENABLED})")
    plan = _plan_doc(plan_raw)
    _receipt_path_refusals(plan, receipt)
    if rehearsal is not None:
        _check_rehearsal(rehearsal, root)
        if runner_factory is not None or host_hook is not None:
            raise ApplyRefused("a rehearsal takes no runner factory or host hook")
        host, runner = rehearsal.host, rehearsal.runner
        host.exempt = (receipt,)
    else:
        host = FixtureHost(root, exempt_prefixes=(receipt,))
        if host_hook is not None:
            host_hook(host)
        runner = _fixture_runner(runner_factory, host)
    inputs = parse_inputs(inputs_raw)
    doc, staging, contents, manifest, registry = rebuild_plan(plan, manifest_raw, registry_raw, tracked_raw, closure=closure)
    service_uids = {e["inputs"]["uid"] for e in doc["entries"] if e["kind"] == "account"}
    if inputs.operator_uid in service_uids:
        raise ApplyRefused("inputs.operator_batch.1.operator_uid: must not be a planned service-account uid")
    fragment = H.substrate_manifest_fragment(manifest, doc["entries"])
    if canonical(fragment) != canonical(fragment_from_plan(doc)):
        raise ApplyRefused("the substrate fragment does not round-trip from the plan")
    if set(inputs.registered_manifests) != {m["revision"] for m in registry.manifests}:
        raise ApplyRefused("inputs.registered_manifests must name exactly the registry's manifest revisions")
    rcd = receipt + ".d"
    if host.exists(receipt) or host.exists(rcd):
        raise ApplyRefused(f"--receipt {receipt} (or its .d dir) already exists (O_EXCL)")
    if not host.path(posixpath.dirname(receipt)).is_dir():
        raise ApplyRefused("--receipt: its parent directory must exist")
    # the staging copies live in RC.d; until RC.d exists the family manifest loads from a scratch view
    ctx = Ctx(host, runner, doc, F.load_manifest(fragment), contents=contents, inputs=inputs, rcd=rcd,
              execute=False, rehearsal=rehearsal)
    preflight_refusals(ctx, manifest, registry)

    if not execute:  # dry run: the exact ordered actions, no mutation, no receipt
        actions = []
        for e in doc["entries"]:
            try:
                actions.append(execute_entry(ctx, e))
            except (EntryFailed, HelperRefusal) as exc:
                actions.append(_result(e, "would-fail", observations={"error": str(exc)}))
        return ApplyOutcome(0, None, actions)

    # --- first write: RC.d (root-only), the staged family bytes, Q10 backups
    started = now()
    host.mkdir(rcd, "root", "root", "0700")
    for sub in ("saved", "backups", "substrate-staging"):
        host.mkdir(posixpath.join(rcd, sub), "root", "root", "0700")
    for rel, data in staging.items():
        parts = rel.split("/")
        for i in range(1, len(parts)):
            d = posixpath.join(rcd, "substrate-staging", *parts[:i])
            if not host.exists(d):
                host.mkdir(d, "root", "root", "0700")
        host.write_file(posixpath.join(rcd, "substrate-staging", rel), data, "root", "root", "0644")
    ctx.fmanifest = substrate_manifest(fragment, host.path(posixpath.join(rcd, "substrate-staging")))
    ctx.execute = True
    backups = _backup_state_dirs(ctx, started, "pre-apply")
    # review L5: the progress head carries the backups, so a crash-recovered rollback restores them
    head = {"schema": RECEIPT_SCHEMA, "plan_sha256": doc["plan_sha256"], "started_at": started, "backups": backups,
            "mode": FIXTURE_MODE if rehearsal is None else REHEARSAL_MODE}
    if rehearsal is not None:
        head["rehearsal"] = _rehearsal_identity(rehearsal)
    _append_progress(host, rcd, head)

    results: list[dict[str, Any]] = []
    failed: dict[str, Any] | None = None
    reached = 0
    finished = False
    try:
        for e in doc["entries"]:
            reached = e["seq"]
            # review MED-1: the before-state is captured BEFORE the executor runs, so an
            # entry that fails in ANY way (even an unexpected KeyError) is recorded with it.
            # The capture itself runs INSIDE the guarded try (MED-1 residual, probes P5/P6):
            # a capture failure records the entry instead of escaping the loop.
            mark = len(host.mutations)
            pre: dict[str, Any] = {}
            try:
                pre = _capture_before(ctx, e)
                record = execute_entry(ctx, e)
            except _PartialEntry as exc:
                record = exc.record
                if record.get("before") is None and pre.get("before") is not None:
                    record["before"] = pre["before"]
                failed = {"seq": e["seq"], "id": e["id"], "error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - recorded, the run stops partial (rc 1)
                mutated = len(host.mutations) > mark
                record = _result(e, "failed-unknown" if mutated else "failed-before-mutation", **pre)
                expected = isinstance(exc, (EntryFailed, HelperRefusal, OSError, ApplyRefused))
                failed = {"seq": e["seq"], "id": e["id"], "error": f"{type(exc).__name__}: {exc}",
                          "unexpected": not expected, "mutated": mutated}
            results.append(record)
            _append_progress(host, rcd, record)
            if failed:
                break
        else:
            finished = True
    except Exception as exc:  # noqa: BLE001 - backstop: after RC.d exists this is ALWAYS partial, rc 1
        # (review MED-1 residual 3: an ApplyRefused raised after the first mutation must never
        # surface as "refused / nothing mutated"; only a refusal before RC.d is rc 2)
        if failed is None:
            failed = {"seq": reached, "id": doc["entries"][reached]["id"], "error": f"{type(exc).__name__}: {exc}",
                      "unexpected": True, "mutated": bool(host.mutations), "backstop": True}
    finally:
        if not finished and failed is None:  # a BaseException is propagating: still partial
            failed = {"seq": reached, "id": doc["entries"][reached]["id"], "error": "the apply loop did not finish",
                      "unexpected": True, "mutated": bool(host.mutations), "backstop": True}
        recorded = {r["id"] for r in results}
        for e in doc["entries"]:
            if e["id"] not in recorded:
                results.append(_result(e, "not-reached"))
        receipt_doc = _receipt(ctx, doc, started, now(), results, failed, backups)
        _write_exclusive(host, receipt, (canonical(receipt_doc) + "\n").encode("ascii"))
    return ApplyOutcome(1 if failed else 0, receipt_doc, results)


MUTATING_ACTIONS: Final = frozenset({"created", "replaced", "failed", "failed-unknown"})
#: kinds whose executor only observes (no pre-capture needed)
OBSERVE_ONLY_KINDS: Final = frozenset({"package_verify", "precondition", "journal_schema_check", "framework_file",
                                       "credential_ref_registration", "journal_root"})
#: kinds whose executor writes a regular file at e["path"]
FILE_KINDS: Final = frozenset({"launcher", "config", "registry", "registered_manifest", "activation",
                               "registry_post_registration", "plan_registration"})


def _capture_before(ctx: Ctx, e: Mapping[str, Any]) -> dict[str, Any]:
    """The before-state the loop records for an entry that fails in an unexpected way
    (MED-1). Saved copies go to RC.d (exempt; never a host mutation)."""

    kind = e["kind"]
    if kind in OBSERVE_ONLY_KINDS:
        return {}
    host = ctx.host
    src = e["source"]
    if src.get("route") == "family":
        r = F.resolve(src["family"], dict(src["params"]), ctx.fmanifest)
        pre = observe_subject(host, r.subject, ctx.port)
        fam = {"family": src["family"], "params": dict(src["params"]), "subject": r.subject, "preimage": pre,
               "resolved_after": r.after, "entry": dict(r.entry), "saved": None}
        if r.subject.startswith("file:") and isinstance(pre, dict) and "sha256" in pre:
            fam["saved"] = ctx.save_copy(r.subject[5:])
        return {"family": fam}
    if kind == "runtime":
        prefix, launcher = _runtime_paths(e)
        before = {"prefix": host.observe(prefix), "launcher": host.observe(launcher), "moved_aside": [],
                  "created_parents": []}
        for obs in (before["prefix"], before["launcher"]):
            obs.pop("sha256", None)
        return {"before": before, "paths": {"prefix": prefix, "launcher": launcher}}
    try:
        obs = host.observe(e["path"], hash_content=kind != "credential")
    except ApplyRefused:
        return {"before": {"exists": None, "unobservable": True}}
    if kind in FILE_KINDS and obs.get("type") == "file":
        obs["saved"] = ctx.save_copy(e["path"])
    out: dict[str, Any] = {"before": obs}
    if kind == "framework_tree":
        base = posixpath.dirname(ctx.plan["framework"]["root"])
        out["observations"] = {"base": base, "partial": posixpath.join(base, f".{ctx.plan['framework']['rev']}.partial")}
    if kind == "plan_registration":
        out["observations"] = {"producer_out": e["inputs"]["producer_writes"]}
    return out


def _fixture_runner(factory: Callable[[FixtureHost], Any] | None, host: FixtureHost) -> "FixtureCommandRunner":
    """Review L2: the dark runner rule is structural. Whatever a factory returns, only a
    FixtureCommandRunner is accepted (a real runner is refused, never used)."""

    runner = (factory or (lambda h: FixtureCommandRunner(h)))(host)
    if not isinstance(runner, FixtureCommandRunner):
        raise ApplyRefused(f"{type(runner).__name__}: only a FixtureCommandRunner may run (U3a has no live mode)")
    return runner


def _receipt(ctx: Ctx, plan: Mapping[str, Any], started: str, finished: str, results: list[dict[str, Any]],
             failed: dict[str, Any] | None, backups: list[dict[str, Any]]) -> dict[str, Any]:
    residuals = [dict(r) for r in plan["residuals"]]
    for r in results:
        if r["kind"] == "account" and r["action"] in MUTATING_ACTIONS:
            name = r["id"].split(":", 1)[1]
            residuals.append({"id": f"account-created:{name}", "detail": "accounts are sticky: rollback never removes "
                              "an account; teardown is T-2921", "ticket": "T-2921",
                              "paths": ["/etc/passwd", "/etc/group", "/etc/shadow", r["family"]["resolved_after"]["home"]]})
    mutated = any(r["action"] in MUTATING_ACTIONS for r in results)
    status = "partial" if failed else ("complete" if mutated else "noop")
    doc = {
        "schema": RECEIPT_SCHEMA, "plan_sha256": plan["plan_sha256"], "framework_rev": plan["framework"]["rev"],
        "framework_root": plan["framework"]["root"], "manifest_revision": plan["inputs"]["manifest_revision"],
        "started_at": started, "finished_at": finished,
        "mode": FIXTURE_MODE if ctx.rehearsal is None else REHEARSAL_MODE, "root": str(ctx.host.root),
        "receipt_dir": ctx.rcd, "status": status, "noop": status == "noop", "failed_entry": failed,
        "packages": dict(sorted(ctx.packages.items())), "pyyaml_version": ctx.pyyaml_version,
        "backups": backups, "residuals": residuals, "entries": results,
        "operator_batch_consumed": ["operator-batch:1 (token source path only)", "operator-batch:2", "operator-batch:3"],
        "live_actuation_enabled": H.LIVE_ACTUATION_ENABLED,
    }
    if ctx.rehearsal is not None:
        doc["rehearsal"] = _rehearsal_identity(ctx.rehearsal)
    doc["receipt_sha256"] = receipt_digest(doc)
    return doc


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def verify(*, plan_raw: bytes, root: Path | str, receipt_raw: bytes | None = None, expect_started: bool = False,
           runner_factory: Callable[[FixtureHost], FixtureCommandRunner] | None = None,
           rehearsal: Any = None) -> tuple[int, list[dict[str, Any]]]:
    """Re-observe every entry; rc 1 on any drift, with a per-entry report.
    ``rehearsal``: as for ``apply`` (T-2920 U4b)."""

    plan = _plan_doc(plan_raw)
    if rehearsal is not None:
        _check_rehearsal(rehearsal, root)
        if runner_factory is not None:
            raise ApplyRefused("a rehearsal takes no runner factory")
        host, runner = rehearsal.host, rehearsal.runner
    else:
        host = FixtureHost(root)
        runner = _fixture_runner(runner_factory, host)
    fragment = fragment_from_plan(plan)
    ctx = Ctx(host, runner, plan, substrate_manifest(fragment, None), execute=False, expect_started=expect_started,
              rehearsal=rehearsal)
    after = {}
    if receipt_raw is not None:
        rc_doc = json.loads(receipt_raw)
        if rc_doc.get("receipt_sha256") != receipt_digest(rc_doc) or rc_doc.get("plan_sha256") != plan["plan_sha256"]:
            raise ApplyRefused("receipt digest or plan_sha256 mismatch")
        require_receipt_mode(rc_doc, FIXTURE_MODE if rehearsal is None else REHEARSAL_MODE, rehearsal=rehearsal)
        after = {r["id"]: r.get("after_sha256") for r in rc_doc["entries"]}
    report = []
    for e in plan["entries"]:
        try:
            detail = _verify_entry(ctx, e, after.get(e["id"]))
            ok = detail.pop("ok", True)
        except (EntryFailed, HelperRefusal, ApplyRefused, OSError, ValueError, KeyError) as exc:
            ok, detail = False, {"error": f"{type(exc).__name__}: {exc}"}
        report.append({"seq": e["seq"], "id": e["id"], "kind": e["kind"], "ok": ok, **detail})
    return (0 if all(r["ok"] for r in report) else 1), report


def _expect_file(ctx: Ctx, path: str, sha: str | None, owner: str, group: str, mode: str) -> dict[str, Any]:
    obs = ctx.host.observe(path)
    if not obs["exists"] or obs["type"] != "file":
        return {"ok": False, "drift": "missing or not a regular file"}
    if sha is not None and obs["sha256"] != sha:
        return {"ok": False, "drift": f"content sha {obs['sha256']} != {sha}"}
    if _meta(obs) != (owner, group, mode):
        return {"ok": False, "drift": f"owner/group/mode {_meta(obs)} != {(owner, group, mode)}"}
    return {"ok": True, "content": "pinned" if sha else "unpinned (no receipt)"}


def _verify_entry(ctx: Ctx, e: Mapping[str, Any], receipt_sha: str | None) -> dict[str, Any]:
    kind, path = e["kind"], e.get("path")
    if e["source"].get("route") == "family":
        helper = ctx.helper()
        step = helper.admit(e["source"]["family"], dict(e["source"]["params"]), ACTIVATE)
        ev = helper.verify(step)
        if not ev.ok:
            return {"ok": False, "drift": ev.detail}
        if kind == "account":
            name = e["source"]["params"]["account"]
            field_ = ctx.host.shadow_field(name)
            if field_ not in ("!", "*"):
                return {"ok": False, "drift": f"{name}: shadow password field is not locked ('!' or '*')"}
        return {"ok": True}
    if kind == "package_verify":
        return {"ok": True, **ex_package(ctx, e)["observations"]}
    if kind in ("precondition", "journal_schema_check", "credential_ref_registration"):
        return {"ok": True, **TOOL_EXECUTORS[kind](ctx, e)["observations"]}
    if kind == "dir":
        obs = ctx.host.observe(path)
        if e["source"].get("must_not_exist"):
            return {"ok": not obs["exists"], **({} if not obs["exists"] else {"drift": "staging dir left behind"})}
        if not obs["exists"] or obs["type"] != "dir":
            return {"ok": False, "drift": "missing directory"}
        if _meta(obs) != (e["owner"], e["group"], e["mode"]):
            return {"ok": False, "drift": f"owner/group/mode {_meta(obs)}"}
        return {"ok": True}
    if kind == "runtime":
        built = _runtime_built(ctx, e)
        if built is None:
            return {"ok": False, "drift": "runtime tree missing or not matching its BUILD-MANIFEST"}
        if receipt_sha and built["tree_sha256"] != receipt_sha:
            return {"ok": False, "drift": "runtime tree sha differs from the receipt"}
        return {"ok": True}
    if kind == "framework_tree":
        obs = ctx.host.observe(path)
        return {"ok": obs.get("type") == "dir" and _meta(obs) == ("root", "root", "0755"),
                **({} if obs.get("type") == "dir" else {"drift": "rev dir missing"})}
    if kind == "framework_file":
        return _expect_file(ctx, path, e["content_sha256"], "root", "root", "0644")
    if kind == "framework_active_link":
        obs = ctx.host.observe(path)
        want = ctx.plan["framework"]["rev"]
        if obs.get("link_target") != want:
            return {"ok": False, "drift": f"active -> {obs.get('link_target')!r}, want {want!r}"}
        return {"ok": True}
    if kind == "launcher":
        return _expect_file(ctx, path, sha256_bytes(render_launcher(e, ctx.plan["framework"]["rev"])), "root", "root", "0755")
    if kind in ("config", "registry", "registered_manifest", "activation", "registry_post_registration"):
        sha = e.get("content_sha256") or receipt_sha
        if kind == "registry" and e.get("content_sha256"):
            # the seat registry: its post-registration bytes minus "plans" are the pinned bytes
            out = _expect_file(ctx, path, None, e["owner"], e["group"], e["mode"])
            if out["ok"]:
                current = json.loads(ctx.host.read_bytes(path))
                if isinstance(current, dict):
                    current.pop("plans", None)
                if sha256_bytes((canonical(current) + "\n").encode("ascii")) != e["content_sha256"]:
                    return {"ok": False, "drift": "seat registry seats/manifests differ from the plan"}
            return {**out, "content": "pinned"} if out["ok"] else out
        out = _expect_file(ctx, path, sha, e["owner"], e["group"], e["mode"])
        if out["ok"] and kind == "config" and not e.get("content_sha256"):
            if e["source"]["action"] == "render-at-apply":
                from tools.host_control.telegram_ingress.config import parse_ingress_config

                parse_ingress_config(json.loads(ctx.host.read_bytes(path)))
            else:
                _validate_u2(ctx, e)
        if out["ok"] and kind == "registry_post_registration":
            current = json.loads(ctx.host.read_bytes(path))
            recorded = {r["path"] for r in current.get("plans", [])}
            missing = [x["path"] for x in ctx.entries_of("plan_registration") if x["path"] not in recorded]
            if missing:
                return {"ok": False, "drift": f"plans not registered: {missing}"}
        return out
    if kind == "plan_registration":
        out = _expect_file(ctx, path, receipt_sha, "root", "root", "0644")
        reg = json.loads(ctx.host.read_bytes(_registry_entry(ctx)["path"]))
        recorded = {r["path"]: r["sha256"] for r in reg.get("plans", [])}
        if out["ok"] and recorded.get(path) != ctx.host.observe(path)["sha256"]:
            return {"ok": False, "drift": "the registry plans list does not record this plan's sha"}
        return out
    if kind == "credential":
        obs = ctx.host.observe(path, hash_content=False)
        if obs.get("type") != "file" or _meta(obs) != (e["owner"], e["group"], e["mode"]):
            return {"ok": False, "drift": "credential missing or wrong owner/mode"}
        return {"ok": True, "content": "never read"}
    if kind == "journal_root":
        obs = ctx.host.observe(path)
        if not obs["exists"]:
            return {"ok": not ctx.expect_started, "state": "pending-start",
                    **({"drift": "state dir missing after start"} if ctx.expect_started else {})}
        return {"ok": _meta(obs) == (e["owner"], e["group"], e["mode"]), "state": "present"}
    raise EntryFailed(f"no verifier for kind {kind}")


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------


def _load_receipt(host: FixtureHost, receipt: str) -> dict[str, Any]:
    if host.path(receipt).is_file():
        doc = json.loads(host.read_bytes(receipt))
        if doc.get("schema") != RECEIPT_SCHEMA or doc.get("receipt_sha256") != receipt_digest(doc):
            raise ApplyRefused("receipt schema or receipt_sha256 mismatch")
        return doc
    progress = posixpath.join(receipt + ".d", "progress.jsonl")
    if not host.path(progress).is_file():
        raise ApplyRefused(f"no receipt at {receipt} and no progress journal")
    lines = [json.loads(line) for line in host.read_bytes(progress).decode("ascii").splitlines() if line]
    head, entries = lines[0], lines[1:]
    out = {"schema": RECEIPT_SCHEMA, "plan_sha256": head["plan_sha256"], "entries": entries,
           "backups": head.get("backups", []), "receipt_dir": receipt + ".d", "crash_recovered": True,
           "framework_root": None, "status": "partial"}
    for key in ("mode", "rehearsal"):  # T-2920 U4b: the head's mode travels (Recovery (c))
        if key in head:
            out[key] = head[key]
    return out


def rollback(*, receipt: str, root: Path | str, runner_factory: Callable[[FixtureHost], FixtureCommandRunner] | None = None,
             now: Callable[[], str] = _utc_now, rehearsal: Any = None,
             plan_sha256: str | None = None) -> tuple[int, dict[str, Any]]:
    """Reverse the receipt's applied entries in reverse order.

    ``rehearsal`` (T-2920 U4b): as for ``apply``; the receipt must be this machine's
    ``rehearsal-nspawn`` receipt for ``plan_sha256`` (the attested plan)."""

    _norm_host_path(receipt, "--receipt")
    port = None
    if rehearsal is not None:
        _check_rehearsal(rehearsal, root)
        if runner_factory is not None:
            raise ApplyRefused("a rehearsal takes no runner factory")
        host, runner, port = rehearsal.host, rehearsal.runner, rehearsal.port
        host.exempt = (receipt,)
    else:
        host = FixtureHost(root, exempt_prefixes=(receipt,))
        runner = _fixture_runner(runner_factory, host)
    doc = _load_receipt(host, receipt)
    require_receipt_mode(doc, FIXTURE_MODE if rehearsal is None else REHEARSAL_MODE, rehearsal=rehearsal)
    if rehearsal is not None and doc.get("plan_sha256") != plan_sha256:
        raise ApplyRefused("the rehearsal receipt is not for the attested plan")
    rcd = doc["receipt_dir"]
    stamp = now()
    entries = sorted((r for r in doc["entries"] if r["action"] in MUTATING_ACTIONS), key=lambda r: -r["seq"])
    fw_created = {r["id"]: r for r in doc["entries"] if r["kind"] == "framework_tree" and r["action"] == "created"}
    actions, residuals, errors = [], [], []
    notes: list[str] = []
    # mason ruling (review L6): a noop receipt mutated nothing, so there is nothing to
    # restore; its state dirs and journals are never swapped
    restore_state = doc.get("status") != "noop"
    if not restore_state:
        notes.append("noop receipt: state restore skipped")
    # Q10: back up existing state dirs before acting
    state_dirs = sorted({b["state_dir"] for b in doc.get("backups", [])}) if restore_state else []
    pre_backups = []
    for sd in state_dirs:
        if host.observe(sd).get("type") == "dir":
            archive = posixpath.join(rcd, "backups", f"pre-rollback-{posixpath.basename(sd)}-{stamp.replace(':', '')}.tar")
            _tar_create(host, archive, sd)
            pre_backups.append({"state_dir": sd, "archive": archive, "sha256": sha256_bytes(host.read_bytes(archive))})
    for r in entries:
        try:
            done = _rollback_entry(host, runner, r, fw_created, port)
            actions.append({"seq": r["seq"], "id": r["id"], **done})
            residuals.extend(done.get("residuals", []))
        except Exception as exc:  # noqa: BLE001 - every failure is recorded; rollback returns rc 1
            errors.append({"seq": r["seq"], "id": r["id"], "error": f"{type(exc).__name__}: {exc}"})
    # Q10 restore: move the current state dir aside (kept) and extract the apply-time backup
    for b in (doc.get("backups", []) if restore_state else []):
        sd = b["state_dir"]
        if host.exists(sd):
            aside = f"{sd}.pre-rollback-{stamp.replace(':', '').replace('-', '')}"
            host.rename(sd, aside)
            residuals.append({"id": f"state-kept:{aside}", "detail": "current state moved aside, never deleted (Q10)",
                              "paths": [aside]})
        _restore_backup(host, b["archive"], sd)
        meta = b.get("meta") or {}
        if meta:
            host.chown(sd, meta["owner"], meta["group"])
            host.chmod(sd, meta["mode"])
        actions.append({"id": f"journal-restore:{sd}", "action": "restored", "archive": b["archive"]})
    out = {"schema": RECEIPT_SCHEMA.replace("receipt/v1", "rollback/v1"), "receipt": receipt,
           "plan_sha256": doc["plan_sha256"], "at": stamp, "actions": actions, "errors": errors,
           "residuals": residuals, "pre_rollback_backups": pre_backups, "crash_recovered": bool(doc.get("crash_recovered")),
           "notes": notes}
    out["rollback_sha256"] = sha256_bytes(canonical(out).encode("ascii"))
    _write_exclusive(host, f"{receipt}.rollback-{stamp.replace(':', '').replace('-', '')}.json",
                     (canonical(out) + "\n").encode("ascii"))
    return (1 if errors else 0), out


def _restore_backup(host: FixtureHost, archive: str, state_dir: str) -> None:
    """Q10 restore: validate every member (inside <state_dir>, no absolute path, no
    '..', no device/fifo, no link leaving the dir), then extract with filter='data'."""

    base = posixpath.basename(state_dir)
    with tarfile.open(host.path(archive)) as tar:
        members = tar.getmembers()
        for m in members:
            name = m.name
            if name.startswith("/") or ".." in name.split("/") or not (name == base or name.startswith(base + "/")):
                raise EntryFailed(f"backup {archive}: member {name!r} escapes {state_dir}")
            if not (m.isfile() or m.isdir() or m.issym() or m.islnk()):
                raise EntryFailed(f"backup {archive}: member {name!r} is a device/fifo; refused")
            if m.issym() or m.islnk():
                target = m.linkname if m.islnk() else posixpath.normpath(posixpath.join(posixpath.dirname(name), m.linkname))
                if m.linkname.startswith("/") or ".." in target.split("/") or not (target == base or target.startswith(base + "/")):
                    raise EntryFailed(f"backup {archive}: link {name!r} points outside {state_dir}")
        target_parent = host.wpath(posixpath.dirname(state_dir))  # review MED-3: no leaf link
        if not target_parent.is_dir() or os.path.lexists(host.path(state_dir)):
            raise EntryFailed(f"{state_dir}: the restore target must be absent under a real directory")
        host._mutate("restore", state_dir)
        tar.extractall(target_parent, members=members, filter="data")


def _restore_meta(host: FixtureHost, path: str, before: Mapping[str, Any]) -> None:
    host.chown(path, before["owner"], before["group"])
    host.chmod(path, before["mode"])


def _rollback_entry(host: FixtureHost, runner: FixtureCommandRunner, r: Mapping[str, Any],
                    fw_created: Mapping[str, Any], port: Any = None) -> dict[str, Any]:
    kind, before = r["kind"], r.get("before") or {}
    fam = r.get("family")
    if fam:
        return _rollback_family(host, runner, r, fam, port)
    if kind == "framework_tree":
        rev = r["id"].split(":", 1)[1]
        base = r["observations"]["base"]
        if before.get("exists"):
            raise EntryFailed(f"{base}/{rev}: pre-existed this apply; a kept rev is never removed")
        # ONLY <base>/<rev> (created: receipt-proven) or this apply's own <base>/.<rev>.partial
        targets = [posixpath.join(base, rev)] if r["id"] in fw_created else []
        if r["action"] in ("failed", "failed-unknown"):
            targets.append(r["observations"]["partial"])
        for target in targets:
            if posixpath.dirname(target) != base or posixpath.basename(target) not in (rev, f".{rev}.partial"):
                raise EntryFailed(f"{target}: outside <base>/<rev>; refused")
            if host.exists(target):
                host.remove_tree_contained(target, base)
        return {"action": "removed-rev", "removed": targets}
    if kind == "framework_active_link":
        link = r["id"].split(":", 1)[1]
        prev = before.get("link_target")
        if prev:
            host.symlink_replace(link, prev)
            return {"action": "active-restored", "target": prev}
        if host.exists(link):
            host.unlink(link, allow_leaf_symlink=True)
        return {"action": "active-removed"}
    if kind == "runtime":
        # the tree this apply built is removed (receipt-proven); a moved-aside build comes back
        moved = {m["path"]: m["aside"] for m in before.get("moved_aside", [])}
        for key in ("prefix", "launcher"):
            path = r["paths"][key]
            existed = (before.get(key) or {}).get("exists")
            if path in moved:  # this apply moved the old build aside: drop what was built, bring it back
                if host.exists(path):
                    _remove_built(host, path)
                host.rename(moved[path], path)
            elif not existed and host.exists(path):  # built by this apply
                _remove_built(host, path)
            # existed and was never moved: the old build is still in place, untouched
        residuals = []
        for d in reversed(before.get("created_parents", [])):
            if host.exists(d) and _is_empty_dir(host, d):
                host.rmdir(d)
            elif host.exists(d):
                residuals.append({"id": f"dir-kept:{d}", "detail": "runtime parent not empty at rollback", "paths": [d]})
        return {"action": "runtime-restored", "residuals": residuals}
    if kind == "credential":
        path = r["id"].split(":", 1)[1]
        if not before.get("exists") and host.exists(path):
            host.unlink(path)
            return {"action": "removed"}
        return {"action": "kept"}
    if kind == "dir":
        path = r["id"].split(":", 1)[1]
        if not before.get("exists"):
            if not host.exists(path):
                return {"action": "already-absent"}
            if not _is_empty_dir(host, path):
                return {"action": "kept-non-empty", "residuals": [{"id": f"dir-kept:{path}",
                        "detail": "created by apply but not empty at rollback (e.g. an account home)", "paths": [path]}]}
            host.rmdir(path)
            return {"action": "removed"}
        _restore_meta(host, path, before)
        return {"action": "meta-restored"}
    # files: config, registry, launcher, activation, registered manifest, plan registration, registry post
    path = _entry_path(r)
    producer_out = (r.get("observations") or {}).get("producer_out") if kind == "plan_registration" else None
    if producer_out and host.exists(producer_out):
        host.unlink(producer_out)  # the producer's own staging file, if a failure left it
    if before.get("exists"):
        saved = before.get("saved")
        if saved is None:
            raise EntryFailed(f"{path}: replaced without a saved copy")
        host.write_file(path, host.read_bytes(saved), before["owner"], before["group"], before["mode"])
        return {"action": "restored"}
    if host.exists(path):
        host.unlink(path)
    return {"action": "removed"}


def _is_empty_dir(host: FixtureHost, path: str) -> bool:
    """Review MED-3/L7: never follows a leaf link; the scandir iterator is closed."""

    with os.scandir(host.wpath(path)) as it:
        return next(it, None) is None


def _entry_path(r: Mapping[str, Any]) -> str:
    if "path" in r and r["path"]:
        return r["path"]
    return r["id"].split(":", 1)[1]


def _remove_built(host: FixtureHost, path: str) -> None:
    p = host.path(path)
    if p.is_dir() and not p.is_symlink():
        host.remove_tree_contained(path, posixpath.dirname(path))
    else:
        host.unlink(path)


def _rollback_family(host: FixtureHost, runner: FixtureCommandRunner, r: Mapping[str, Any],
                     fam: Mapping[str, Any], port: Any = None) -> dict[str, Any]:
    family, subject, preimage = fam["family"], fam["subject"], fam["preimage"]
    if family == "account.ensure":
        # accounts are STICKY: never userdel (T-2921 owns teardown); ports.rollback_argv would remove it
        # (ports.rollback_argv would return userdel/groupdel for a created account: deliberately NOT run)
        return {"action": "account-kept", "residuals": [{"id": f"account-created:{subject.split(':', 1)[1]}",
                "detail": "accounts are sticky; teardown is T-2921", "ticket": "T-2921"}]}
    current = observe_subject(host, subject, port)
    if current == preimage:
        return {"action": "already-at-preimage"}
    staged = None
    if family in ("unit.system.install", "dbus_policy.install") and preimage is not None:
        staged = fam.get("saved")
        if staged is None:
            raise EntryFailed(f"{subject}: replaced without a saved copy")
    if family == "owner_mode.set" and preimage is None:
        return {"action": "skip", "detail": "the path did not exist before; the dir entry removes it"}
    argvs = P.apply_argv(F.Resolved(family, subject, preimage, fam["entry"]), staged_source=staged)
    for argv in argvs:
        res = runner.run(argv)
        if res.returncode != 0:
            raise EntryFailed(f"rollback {argv[0]} exited {res.returncode}")
    return {"action": "restored-preimage", "argv": [list(a) for a in argvs]}


# ---------------------------------------------------------------------------
# CLI (dark)
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None, *,
         runner_factory: Callable[[FixtureHost], FixtureCommandRunner] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     usage="python3 -I tools/host_broker_install_apply.py {apply,verify,rollback} ...")
    sub = parser.add_subparsers(dest="command", required=True)
    ap = sub.add_parser("apply", help="dry-run (default) or --execute the plan under a FIXTURE root")
    for flag in ("--plan", "--manifest", "--registry", "--tracked-files", "--inputs"):
        ap.add_argument(flag, required=True, type=Path)
    ap.add_argument("--root", type=Path, help="the fixture root (required: there is no live mode)")
    ap.add_argument("--receipt", required=True, help="host path of the receipt (re-rooted; must not exist)")
    ap.add_argument("--execute", action="store_true")
    vp = sub.add_parser("verify", help="re-observe every plan entry under a fixture root")
    vp.add_argument("--plan", required=True, type=Path)
    vp.add_argument("--root", required=True, type=Path)
    vp.add_argument("--receipt", help="host path of a receipt (pins deferred content)")
    vp.add_argument("--expect-started", action="store_true")
    rp = sub.add_parser("rollback", help="reverse a receipt under a fixture root")
    rp.add_argument("--receipt", required=True)
    rp.add_argument("--root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "apply":
            if args.root is None:
                raise ApplyRefused("--root is required; U3a has no live mode (refused even with --execute)")
            outcome = apply(plan_raw=args.plan.read_bytes(), manifest_raw=args.manifest.read_bytes(),
                            registry_raw=args.registry.read_bytes(), tracked_raw=args.tracked_files.read_bytes(),
                            inputs_raw=args.inputs.read_bytes(), root=args.root, receipt=args.receipt,
                            execute=args.execute, runner_factory=runner_factory)
            if outcome.receipt is None:
                for a in outcome.actions:
                    print(canonical({k: a[k] for k in ("seq", "id", "kind", "action", "argv")}))
            else:
                print(canonical({"status": outcome.receipt["status"], "receipt": args.receipt,
                                 "receipt_sha256": outcome.receipt["receipt_sha256"],
                                 "failed_entry": outcome.receipt["failed_entry"]}))
            return outcome.rc
        if args.command == "verify":
            receipt_raw = None
            if args.receipt:
                receipt_raw = FixtureHost(args.root).read_bytes(_norm_host_path(args.receipt, "--receipt"))
            rc, report = verify(plan_raw=args.plan.read_bytes(), root=args.root, receipt_raw=receipt_raw,
                                expect_started=args.expect_started, runner_factory=runner_factory)
            for row in report:
                print(canonical(row))
            return rc
        rc, out = rollback(receipt=args.receipt, root=args.root, runner_factory=runner_factory)
        print(canonical({"actions": len(out["actions"]), "errors": out["errors"], "residuals": len(out["residuals"])}))
        return rc
    except (ApplyRefused, H.InstallPlanError, OSError) as exc:
        print(f"host_broker_install_apply: refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
