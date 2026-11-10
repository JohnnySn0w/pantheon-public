"""tools/chronicle_export.py: the Chronicle -> memex export tree (T-6118 PR-2).

Archivist (the memex) ingests session archives from ONE host directory,
``/var/lib/pantheon/chronicle-export/<seat>/<YYYY-MM-DD>/<basename>``. A session end hardlinks the
archive's files into it; archivist writes only under ``.ingested/`` (receipts). The contract is
/home/suborch2/artisan-drafts/t6118/DESIGN.md "DECISIONS LOCKED" and the T-6118 ticket annotations.

What is exported, and what is not:

  * ``<stem>.md``, ``<stem>.summary.md`` (T-6118 PR-4: the synthesized tail summary of a record whose
    HANDOFF was stale; a separate file, the .md is never replaced), ``<stem>.session.json`` and
    ``<stem>.transcript.jsonl.gz`` (or the counter form ``<stem>.transcript.jsonl.<N>.gz`` that
    chronicle.archive_native_transcript mints on a name collision) only (ALLOWED_SUFFIXES +
    TRANSCRIPT_GZ_RE). A hollow record (no transcript, no pane log, stale HANDOFF, lifetime < 120 s)
    exports nothing at all.
    ``.pane.log.gz`` is NEVER exported (FORBIDDEN_SUFFIXES): it is a credential-scrubbed human
    side-log, not a transcript, and archivist does not ingest it.
  * A hardlink to the FINAL name, never a copy: the export shares the Chronicle file's inode, so
    the archive is the single copy and a receipt-gated prune removes both names. The Chronicle
    writers create the files 0640 and group ``pantheon-shared`` from the start, so the link is
    readable by the ingest account.

Safety (the export tree is a group-writable setgid tree, so a same-host agent could plant a
symlink): every directory below the root is opened with an O_DIRECTORY|O_NOFOLLOW fd chain and
every operation is relative to a held dir fd (T-4855's narrow instance). A missing root or seat
directory is ``Unprovisioned`` (PR-1 provisions them; non-fatal). Nothing is ever unlinked or
overwritten: an existing name with other bytes is ``ExportRefused``.

Light on purpose: stdlib plus tools.chronicle and tools.appserver_seats. It must not import
persona_installer (the session-end path stays cheap).
"""
from __future__ import annotations

import errno
import gzip
import hashlib
import os
import pwd
import re
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

DEFAULT_EXPORT_ROOT = Path("/var/lib/pantheon/chronicle-export")
EXPORT_ROOT_ENV = "PANTHEON_CHRONICLE_EXPORT_ROOT"
#: ``.summary.md`` is listed explicitly although ``.md`` already matches it: the summary is a deliberate
#: member of the contract. It is NOT a HANDOFF: the handoff-identical dedupe ignores it (SUMMARY_SUFFIX).
SUMMARY_SUFFIX = ".summary.md"
ALLOWED_SUFFIXES = (".md", SUMMARY_SUFFIX, ".session.json")
#: ``<x>.transcript.jsonl.gz`` and the counter form ``<x>.transcript.jsonl.<N>.gz`` (N decimal digits).
TRANSCRIPT_GZ_RE = re.compile(r".+\.transcript\.jsonl(?:\.[0-9]+)?\.gz")
FORBIDDEN_SUFFIXES = (".pane.log.gz",)
DATE_DIR_MODE = 0o2750
EXPORT_FILE_MODE = 0o640
INGESTED_DIRNAME = ".ingested"

STATUS_EXPORTED = "exported"
STATUS_PARTIAL = "partial"
STATUS_UNPROVISIONED = "export-unprovisioned"
STATUS_SEAT_UNRESOLVED = "export-seat-unresolved"
STATUS_REFUSED = "export-refused"
STATUS_FAILED = "export-failed"
STATUS_SKIPPED = "skipped"
#: A HANDOFF-only record whose .md bytes equal the seat's most recently exported .md: nothing is dropped.
STATUS_HANDOFF_IDENTICAL = "handoff-identical"
#: Statuses that page Recovery. Unprovisioned is a known pre-PR-1 / fresh-host state: recorded, not paged.
ALERT_STATUSES = frozenset({STATUS_PARTIAL, STATUS_SEAT_UNRESOLVED, STATUS_REFUSED, STATUS_FAILED})

SUPERSEDE_MAX_DAYS = 60
SUPERSEDE_MAX_RECORDS = 500
SUPERSEDE_DEADLINE_SECONDS = 20.0
#: Bounds on the handoff-dedupe scan of the export tree (newest date dirs first).
DEDUPE_MAX_DATE_DIRS = 30
DEDUPE_MAX_ENTRIES = 5000
_DATE_DIR_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


class ExportError(Exception):
    """Base of the export failures; ``status`` is the vocabulary word recorded in session.json."""

    status = STATUS_FAILED


class ExportUnprovisioned(ExportError):
    status = STATUS_UNPROVISIONED


class ExportRefused(ExportError):
    status = STATUS_REFUSED


class ExportFailed(ExportError):
    status = STATUS_FAILED


def export_root(explicit: "str | os.PathLike | None" = None) -> Path:
    """kwarg > PANTHEON_CHRONICLE_EXPORT_ROOT > DEFAULT_EXPORT_ROOT (read at call time)."""
    if explicit:
        return Path(explicit)
    configured = os.environ.get(EXPORT_ROOT_ENV)
    return Path(configured) if configured else DEFAULT_EXPORT_ROOT


def is_transcript_gz(name: str) -> bool:
    """True for ``<x>.transcript.jsonl.gz`` and ``<x>.transcript.jsonl.<N>.gz`` basenames."""
    return isinstance(name, str) and TRANSCRIPT_GZ_RE.fullmatch(name) is not None


def exportable(name: str) -> bool:
    """True for a plain basename with an allowed suffix and no forbidden one."""
    if not isinstance(name, str) or not name or "/" in name or name in (".", "..") or "\x00" in name:
        return False
    if name.endswith(FORBIDDEN_SUFFIXES):
        return False
    return name.endswith(ALLOWED_SUFFIXES) or is_transcript_gz(name)


def _sha256_fd_or_path(path: "str | os.PathLike", *, dir_fd: "int | None" = None) -> str:
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_file(path: "str | os.PathLike") -> str:
    return _sha256_fd_or_path(path)


def ensure_group_readable(path: "str | os.PathLike", gid: "int | None") -> bool:
    """Make ``path`` mode 0640 and group ``gid``, IN PLACE (the inode, so every hardlink sees it).

    Only a regular file owned by the running euid; anything else is ExportRefused. Returns True
    when something changed. The runtime swap writes the paired .md 0600 on its strict fail-closed
    path, so the export fixes it here rather than risk an EPERM there."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise ExportRefused(f"cannot open {path} for mode fix: {exc.strerror or exc}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ExportRefused(f"{path} is not a regular file")
        if st.st_uid != os.geteuid():
            raise ExportRefused(f"{path} is not owned by this account (uid {st.st_uid})")
        changed = False
        try:
            if gid is not None and st.st_gid != gid:
                os.fchown(fd, -1, gid)
                changed = True
            if stat.S_IMODE(st.st_mode) != EXPORT_FILE_MODE:
                os.fchmod(fd, EXPORT_FILE_MODE)
                changed = True
        except OSError as exc:
            raise ExportFailed(f"cannot fix mode/group of {path}: {exc.strerror or exc}") from None
        return changed
    finally:
        os.close(fd)


@dataclass
class LinkResult:
    name: str
    size: int
    state: str  # 'linked' | 'already'
    sha256: str = ""  # of the exact bytes now at <seat>/<date>/<name> (the manifest archivist verifies)


class ExportDir:
    """The held dir-fd chain root -> <seat> -> <date>. Use as a context manager."""

    def __init__(self, root: "str | os.PathLike", seat: str, date: "str | None", *,
                 create_date: bool = True) -> None:
        """``date=None`` opens only root -> seat (a read-only probe: no date dir is created)."""
        self.root, self.seat, self.date = Path(root), seat, date
        self._fds: list[int] = []
        self.gid: int = -1
        self.fd: int = -1
        self.seat_fd: int = -1
        if not seat or "/" in seat or seat in (".", "..") or seat == INGESTED_DIRNAME:
            raise ExportRefused(f"bad export seat {seat!r}")
        if date is not None and (not date or "/" in date or date in (".", "..")):
            raise ExportRefused(f"bad export date {date!r}")
        try:
            root_fd = self._open_dir(None, str(self.root), what="export root")
            seat_fd = self._open_dir(root_fd, seat, what=f"seat dir {seat}")
            self.seat_fd = seat_fd
            self.gid = os.fstat(seat_fd).st_gid
            self.fd = self._date_dir(seat_fd, date, create=create_date) if date is not None else -1
        except BaseException:
            self.close()
            raise

    def _open_dir(self, parent: "int | None", name: str, *, what: str) -> int:
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            raise ExportUnprovisioned(f"{what} is not provisioned: {self.root}") from None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise ExportRefused(f"{what} is a symlink or not a directory") from None
            if exc.errno in (errno.EACCES, errno.EPERM):
                raise ExportFailed(f"{what} is not accessible: {exc.strerror}") from None
            raise ExportFailed(f"{what} open failed: {exc!r}") from None
        self._fds.append(fd)
        return fd

    def _date_dir(self, seat_fd: int, date: str, *, create: bool) -> int:
        if create:
            try:
                os.mkdir(date, DATE_DIR_MODE, dir_fd=seat_fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise ExportFailed(f"cannot create date dir {date}: {exc.strerror or exc}") from None
        fd = self._open_dir(seat_fd, date, what=f"date dir {date}")
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise ExportRefused(f"date dir {date} is not a directory")
        if st.st_uid != os.geteuid():
            raise ExportRefused(f"date dir {date} is owned by uid {st.st_uid}, not this account")
        if create and stat.S_IMODE(st.st_mode) != DATE_DIR_MODE:
            try:
                os.fchmod(fd, DATE_DIR_MODE)
            except OSError as exc:
                raise ExportFailed(f"cannot set date dir mode: {exc.strerror or exc}") from None
        return fd

    def close(self) -> None:
        while self._fds:
            try:
                os.close(self._fds.pop())
            except OSError:
                pass

    def __enter__(self) -> "ExportDir":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def ensure_readable(self, path: "str | os.PathLike") -> bool:
        return ensure_group_readable(path, self.gid)

    def link(self, src: "str | os.PathLike", name: "str | None" = None) -> LinkResult:
        """Hardlink ``src`` to ``<seat>/<date>/<name>``. Idempotent; never unlinks or overwrites."""
        src = Path(src)
        name = name or src.name
        if not exportable(name):
            raise ExportRefused(f"{name} is not an exportable file name")
        try:
            st = os.lstat(src)
        except OSError as exc:
            raise ExportFailed(f"cannot stat {src}: {exc.strerror or exc}") from None
        if not stat.S_ISREG(st.st_mode):
            raise ExportRefused(f"{src.name} is not a regular file")
        if st.st_uid != os.geteuid():
            raise ExportRefused(f"{src.name} is not owned by this account (uid {st.st_uid})")
        if st.st_gid != self.gid:
            raise ExportRefused(f"{src.name} is group {st.st_gid}, not the seat dir's group {self.gid}")
        if not st.st_mode & stat.S_IRGRP:
            raise ExportRefused(f"{src.name} is not group-readable (mode {stat.S_IMODE(st.st_mode):o})")
        try:
            os.link(src, name, dst_dir_fd=self.fd, follow_symlinks=False)
            return LinkResult(name, st.st_size, "linked", self._sha_of(name))
        except FileExistsError:
            pass
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise ExportFailed(
                    f"{name}: export tree is on another filesystem (EXDEV); hardlinks cannot cross it") from None
            raise ExportFailed(f"link {name} failed: {exc.strerror or exc}") from None
        try:
            have = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except OSError as exc:
            raise ExportFailed(f"{name} exists but cannot be inspected: {exc.strerror or exc}") from None
        if (have.st_dev, have.st_ino) == (st.st_dev, st.st_ino):
            return LinkResult(name, st.st_size, "already", self._sha_of(name))
        if stat.S_ISREG(have.st_mode) and have.st_size == st.st_size:
            try:
                have_sha = _sha256_fd_or_path(name, dir_fd=self.fd)
                if have_sha == sha256_file(src):
                    return LinkResult(name, st.st_size, "already", have_sha)
            except OSError as exc:
                raise ExportFailed(f"{name} exists but cannot be read: {exc.strerror or exc}") from None
        raise ExportRefused(f"{name} already exists in the export with different bytes")

    def _sha_of(self, name: str) -> str:
        """sha256 of the file as it sits in the export dir (read back through the held fd, O_NOFOLLOW)."""
        try:
            return _sha256_fd_or_path(name, dir_fd=self.fd)
        except OSError as exc:
            raise ExportFailed(f"{name} was linked but cannot be read back: {exc.strerror or exc}") from None

    def exists(self, name: str) -> bool:
        try:
            os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            return True
        except OSError:
            return False


@dataclass
class ExportOutcome:
    status: str
    seat: "str | None"
    date: "str | None"
    root: str
    files: list[dict[str, Any]] = field(default_factory=list)
    detail: "str | None" = None

    def as_meta(self) -> dict[str, Any]:
        return {"status": self.status, "seat": self.seat, "date": self.date, "files": list(self.files),
                "root": self.root, "detail": self.detail}


def export_files(files: Sequence["str | os.PathLike | None"], *, root: "str | os.PathLike",
                 seat: str, date: str) -> ExportOutcome:
    """Fix modes in place, then hardlink each file. Never raises (the caller is fail-open).

    exported: every file is in the tree. partial: some are, some were refused/failed.
    export-unprovisioned / export-refused / export-failed: none were."""
    outcome = ExportOutcome(STATUS_EXPORTED, seat, date, str(root))
    present = [Path(f) for f in files if f is not None]
    try:
        with ExportDir(root, seat, date) as target:
            problems: list[str] = []
            first_failure: "ExportError | None" = None
            for src in present:
                try:
                    target.ensure_readable(src)
                    result = target.link(src)
                    outcome.files.append({"name": result.name, "size": result.size, "sha256": result.sha256,
                                          "state": result.state})
                except ExportError as exc:
                    problems.append(f"{src.name}: {exc}")
                    first_failure = first_failure or exc
                except Exception as exc:  # noqa: BLE001 -- fail-open
                    problems.append(f"{src.name}: {type(exc).__name__}: {exc}")
                    first_failure = first_failure or ExportFailed(str(exc))
            if problems:
                outcome.detail = "; ".join(problems)[:600]
                outcome.status = STATUS_PARTIAL if outcome.files else first_failure.status  # type: ignore[union-attr]
    except ExportError as exc:
        outcome.status, outcome.detail = exc.status, str(exc)[:600]
    except Exception as exc:  # noqa: BLE001 -- fail-open
        outcome.status, outcome.detail = STATUS_FAILED, f"{type(exc).__name__}: {exc}"[:600]
    return outcome


def link_one(src: "str | os.PathLike", *, root: "str | os.PathLike", seat: str, date: str) -> ExportOutcome:
    """Export a single file (the session.json commit marker, linked last)."""
    return export_files((src,), root=root, seat=seat, date=date)


# ------------------------------------------------------------------ handoff dedupe
@dataclass
class ExportedMd:
    """An .md that is (or, in a backfill dry-run, would be) in the export tree."""
    date: str
    name: str
    size: int
    sha256: str

    @classmethod
    def of(cls, date: str, name: str, path: "str | os.PathLike") -> "ExportedMd":
        return cls(date, name, os.stat(path).st_size, sha256_file(path))

    def rel(self, seat: str) -> str:
        return f"{seat}/{self.date}/{self.name}"


def _sha_same(path: "str | os.PathLike", want: str, dir_fd: "int | None" = None) -> bool:
    return _sha256_fd_or_path(path, dir_fd=dir_fd) == want


def identical_to_newest(root: "str | os.PathLike", seat: str, md: "str | os.PathLike", *, date: str,
                        planned: "ExportedMd | None" = None,
                        max_dates: int = DEDUPE_MAX_DATE_DIRS,
                        max_entries: int = DEDUPE_MAX_ENTRIES) -> "str | None":
    """``<seat>/<date>/<name>`` of the seat's MOST RECENTLY EXPORTED .md when ``md`` is byte-identical
    to it, else None. Never raises (None means: export, the safe direction).

    "Most recent" is read from the EXPORT TREE: the newest date dir, then the newest .md name in it
    (stems start with a sortable UTC stamp, so name order is time order; mtime is not used: a hardlink
    carries the Chronicle file's mtime, which any rewrite moves). ``planned`` is a backfill dry-run's
    simulated drop, compared as if exported when it sorts after the tree's newest. Everything is read
    through the seat dir fd with O_NOFOLLOW: a symlinked date dir or a non-regular newest entry gives
    None (no dedupe), a link is never followed. The scan is bounded (``max_dates`` date dirs,
    ``max_entries`` names). Size is compared first; sha256 is streamed only on equal sizes. A record
    already present in the tree under its own name is never "identical" (a retry must finish it)."""
    md = Path(md)
    try:
        want_size = os.stat(md).st_size
        with ExportDir(root, seat, None) as probe:
            if _already_exported(probe.seat_fd, date, md.name):
                return None
            newest = _newest_md_entry(probe.seat_fd, max_dates, max_entries)
            tree_key = (newest[0], newest[1]) if newest else None
            if planned is not None and (tree_key is None or (planned.date, planned.name) > tree_key):
                if (planned.date, planned.name) == (date, md.name) or planned.size != want_size:
                    return None
                return planned.rel(seat) if _sha_same(md, planned.sha256) else None
            if newest is None:
                return None
            n_date, n_name, n_size = newest
            if n_size != want_size:
                return None
            date_fd = os.open(n_date, _DIR_FLAGS, dir_fd=probe.seat_fd)
            try:
                same = _sha_same(n_name, sha256_file(md), dir_fd=date_fd)
            finally:
                os.close(date_fd)
            return f"{seat}/{n_date}/{n_name}" if same else None
    except (ExportError, OSError):
        return None


def _already_exported(seat_fd: int, date: str, name: str) -> bool:
    try:
        date_fd = os.open(date, _DIR_FLAGS, dir_fd=seat_fd)
    except OSError:
        return False
    try:
        os.stat(name, dir_fd=date_fd, follow_symlinks=False)
        return True
    except OSError:
        return False
    finally:
        os.close(date_fd)


def _is_committed(date_fd: int, md_name: str) -> bool:
    """True when ``<stem>.session.json`` (the commit marker, linked last) sits beside the .md in the
    same date dir as a regular file. Opened O_NOFOLLOW through the held dir fd: a link is no marker."""
    try:
        fd = os.open(md_name[: -len(".md")] + ".session.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=date_fd)
    except OSError:
        return False
    try:
        return stat.S_ISREG(os.fstat(fd).st_mode)
    except OSError:
        return False
    finally:
        os.close(fd)


def _newest_md_entry(seat_fd: int, max_dates: int, max_entries: int):
    """(date, name, size) of the newest COMMITTED regular .md (one with its sibling .session.json in the
    same date dir), or None when there is none within the bounds. An uncommitted .md (stage 2 failed, or
    a concurrent drop is mid-way) is skipped in favour of the next older one. A non-regular .md, or a
    date dir that cannot be opened without following a link, ends the scan with None (no dedupe)."""
    try:
        dates = sorted((n for n in os.listdir(seat_fd) if _DATE_DIR_RE.fullmatch(n)), reverse=True)
    except OSError:
        return None
    seen = 0
    for date in dates[:max_dates]:
        try:
            date_fd = os.open(date, _DIR_FLAGS, dir_fd=seat_fd)
        except OSError:
            return None
        try:
            names = os.listdir(date_fd)
            seen += len(names)
            if seen > max_entries:
                return None
            for name in sorted((n for n in names if n.endswith(".md") and not n.endswith(SUMMARY_SUFFIX)),
                               reverse=True):
                st = os.stat(name, dir_fd=date_fd, follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode):
                    return None
                if _is_committed(date_fd, name):
                    return date, name, st.st_size
        except OSError:
            return None
        finally:
            os.close(date_fd)
    return None


# ------------------------------------------------------------------ seat resolution
def _account_of(home: Path) -> "str | None":
    try:
        return pwd.getpwuid(os.stat(home).st_uid).pw_name
    except (OSError, KeyError):
        return None


def resolve_seat(*, persona: str, harness: "str | None", account_home: Path,
                 repo_root: "Path | None" = None) -> tuple["str | None", "str | None"]:
    """(seat, detail). Claude exports under its persona. Codex exports under the appserver seat
    whose account owns ``account_home`` (the persona ``codex`` is the seat ``consultant``); zero seats
    for the account fall back to the persona; an ambiguous or unreadable seats file gives
    (None, why) so the caller records export-seat-unresolved and exports nothing."""
    if harness != "codex":
        return persona, None
    from tools.appserver_seats import AppserverSeatsError, appserver_seat_for_account

    repo = repo_root or Path(__file__).resolve().parent.parent
    account = _account_of(Path(account_home))
    if account is None:
        return None, f"cannot resolve the account that owns {account_home}"
    try:
        return appserver_seat_for_account(repo, account), None
    except AppserverSeatsError as exc:
        if exc.kind == "count" and not exc.matches:
            return persona, None
        return None, str(exc)[:300]


# ------------------------------------------------------------------ supersedes
def _open_maybe_gz(path: Path):
    return gzip.open(path, "rb") if path.name.endswith(".gz") else open(path, "rb")


def _strict_prefix(old: Path, new: Path, deadline: float, clock: Callable[[], float]) -> "bool | None":
    """True when old's decompressed bytes are a STRICT prefix of new's (``new`` may be a plain jsonl:
    the backfill dry-run compares against the source); False when not; None when the deadline
    passed first or a file cannot be read."""
    try:
        with _open_maybe_gz(old) as a, _open_maybe_gz(new) as b:
            while True:
                if clock() > deadline:
                    return None
                block = a.read(1 << 20)
                if not block:
                    return bool(b.read(1))  # old exhausted: strict only if new has more
                other = b.read(len(block))
                if other != block:
                    return False
    except (OSError, EOFError, gzip.BadGzipFile):
        return None


def find_supersedes(*, agent: str, chron_root: Path, export_root_: Path, new_gz: Path, source_path: str,
                    skip_stem: "str | None" = None, now: "datetime | None" = None,
                    max_days: int = SUPERSEDE_MAX_DAYS, max_records: int = SUPERSEDE_MAX_RECORDS,
                    deadline_seconds: float = SUPERSEDE_DEADLINE_SECONDS,
                    clock: Callable[[], float] = time.monotonic) -> tuple[list[dict[str, Any]], "str | None"]:
    """Earlier EXPORTED copies of the same source path that ``new_gz`` strictly extends.

    Newest-first over the agent's index, bounded to ``max_days`` / ``max_records``, with a
    wall deadline. A candidate is a record whose session.json names the same native_transcript
    source_path and whose .gz was actually exported and still exists in the tree. Returns
    ([{file: '<seat>/<date>/<basename>', size, sha256}], detail); the size and sha256 are the old
    .gz's ON-DISK bytes (that is what archivist received). On timeout both copies ship and the
    detail says so. Never deletes anything."""
    from tools import chronicle

    started = clock()
    deadline = started + deadline_seconds
    current = now or datetime.now(timezone.utc)
    floor = current - timedelta(days=max_days)
    found: list[dict[str, Any]] = []
    timed_out = False
    scanned = 0
    for entry in reversed(chronicle.entries(agent, root=chron_root)):
        ts = str(entry.get("ts") or "")
        try:
            at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if at < floor:
            break
        scanned += 1
        if scanned > max_records:
            break
        if clock() > deadline:
            timed_out = True
            break
        stem = f"{chronicle._fs_safe_ts(ts)}-{entry.get('uuid')}"
        if skip_stem and stem == skip_stem:
            continue
        meta = _load_json(chron_root / agent / ts[:10] / f"{stem}.session.json")
        pointer = meta.get("native_transcript") if isinstance(meta.get("native_transcript"), dict) else {}
        exp = meta.get("export") if isinstance(meta.get("export"), dict) else {}
        if pointer.get("source_path") != source_path or exp.get("status") not in (STATUS_EXPORTED, STATUS_PARTIAL):
            continue
        old_gz_name = Path(str(pointer.get("path") or "")).name
        names = {f.get("name") for f in exp.get("files", []) if isinstance(f, dict)}
        if not old_gz_name or old_gz_name not in names or old_gz_name == new_gz.name:
            continue
        seat, date = exp.get("seat"), exp.get("date")
        if not isinstance(seat, str) or not isinstance(date, str):
            continue
        exported = export_root_ / seat / date / old_gz_name
        if exported.is_symlink() or not exported.is_file():
            continue
        verdict = _strict_prefix(exported, new_gz, deadline, clock)
        if verdict is None:
            timed_out = clock() > deadline or timed_out
            continue
        if verdict:
            try:
                found.append({"file": f"{seat}/{date}/{old_gz_name}", "size": exported.stat().st_size,
                              "sha256": sha256_file(exported)})
            except OSError:
                continue
    detail = (f"supersede scan hit its {deadline_seconds:g}s deadline; superseded copies not "
              f"determined, both copies ship") if timed_out else None
    return found, detail


def _load_json(path: Path) -> dict[str, Any]:
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
