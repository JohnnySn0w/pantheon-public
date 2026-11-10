#!/usr/bin/env python3
"""chronicle_prune.py -- receipt-driven prune of the Chronicle's memex-exported copies (T-6118 PR-5).

INTENT. Once archivist has ingested a session archive into the memex and written a receipt, the
VPS copy is transient (the operator TG 12989). This frees the disk the Chronicle and its export tree
hold. A stalled or wrong ingest must NEVER become data loss: every doubt resolves to KEEP, a
count in the run summary and (for doubts that name a defect) a Recovery alert. Normal waiting states
(no receipt yet, a partly exported record) are counted only, so ordinary ingest lag pages nobody.

Runs AS the seat, on its own export dir and its own Chronicle. DRY-RUN BY DEFAULT: nothing is
deleted without --execute, and an unflagged run says so. The systemd unit passes --execute
(flipped from the shipped dry-run after a supervised first prune). This tool never commits.

WHAT IS PRUNED. Discovery is driven from the EXPORT seat dir, never from receipts (a receipt can
only ever permit deleting something that is there). A record is one <stem>: its .md,
.summary.md, .transcript.jsonl[.N].gz and .session.json. The record is pruned as a unit, and only
when EVERY exported file has a receipt that validates (v1 schema; file == seat/date/basename;
seat; raw_sha256_verified is True; the receipt is a regular file owned by the ingest account and
not group/world-writable; every directory from .ingested/ down is owned by the ingest account and
not group/world-writable) AND whose size and sha256 equal the bytes on disk NOW. Each file's two
names (the export hardlink and the Chronicle copy: the same inode, verified) are unlinked, the
Chronicle name first so a killed run leaves the discoverable export name behind; the
.session.json goes LAST (the mirror of the export commit order). The newest 3 .md in the local
Chronicle keep their .md (only the export link goes); their .session.json is removed on both
sides, or export-backfill would re-archive and re-export the record.

NEVER TOUCHED: .pane.log.gz, records that were never exported, .claims/, index.md, another seat's
dir, .ingested/, the export root. A TTL backstop (35 days) ALERTS on an unreceipted exported file
and never deletes it. Stray ``.<name>.transcript.jsonl[.N].gz.tmp-<pid>`` files from a killed
session-end archive are swept after an hour. For a codex seat, a rotated-away original rollout
listed in thread-history is deleted only behind a valid receipt for its archive .gz.

SAFETY. Every directory is opened with an O_DIRECTORY|O_NOFOLLOW fd chain (a symlink at any
component is a refusal) and every file operation is relative to a held dir fd, with an inode
re-check right before each unlink. A dry run performs no write, rename, chmod or unlink under the
export root, the Chronicle or the codex dirs: its only writes are the manifest and the alert state
under --state-dir.

EXIT CODES: 0 ok (a dry run, "nothing to do", persisting already-alerted conditions); 2 refused
(nothing was done); 3 a NEWLY alerted condition or an operational error (so the unit's OnFailure
pages once per condition, not daily).
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import pwd
import re
import stat
import sys
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import chronicle, chronicle_export  # noqa: E402
from tools.agent_role_map import load_instance_document  # noqa: E402

RECEIPT_SCHEMA = "chronicle-ingest-receipt/v1"
RECEIPT_SUFFIX = ".receipt.json"
INGESTED = ".ingested"
TTL_DAYS = 35
KEEP_NEWEST_MD = 3
TMP_MIN_AGE_S = 3600
ALERT_REPEAT_S = 7 * 86400
RECEIPT_MAX_BYTES = 65536
JSON_MAX_BYTES = 1 << 20
RECEIPT_SCAN_CAP = 20000
EXIT_OK, EXIT_REFUSED, EXIT_ALERT = 0, 2, 3
ALERT_ENV = "PANTHEON_CHRONICLE_PRUNE_ALERT"
ALERT_VOCAB = "chronicle-prune-doubt"
STATE_SUBDIR = "chronicle-prune"
HISTORY_DIR = "thread-history"

STEM_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{6})(\.\d+)?Z-(.+)$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
GZ_RE = re.compile(r"^(?P<stem>.+)\.transcript\.jsonl(?:\.[0-9]+)?\.gz$")
#: ``.{dest.name}.tmp-{pid}`` from chronicle.archive_native_transcript, nothing looser.
TMP_RE = re.compile(r"^\.[A-Za-z0-9._-]+\.transcript\.jsonl(?:\.[0-9]+)?\.gz\.tmp-[0-9]+\Z")
HEX64 = re.compile(r"^[0-9a-f]{64}\Z")
HEX40 = re.compile(r"^[0-9a-f]{40}\Z")
SEAT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}\Z")
ACCOUNT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")  # == persona_installer._CHRONICLE_EXPORT_NAME

_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_REG = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
_KIND_ORDER = {"gz": 0, "md": 1, "summary": 2, "session": 3}


class Refused(Exception):
    """Nothing is done; exit 2."""


@dataclass
class Config:
    persona: str
    seat: str
    uid: int
    ingest_uid: int
    export_root: Path
    chronicle_root: Path
    state_dir: Path
    codex_state_dir: "Path | None" = None
    codex_home: "Path | None" = None
    execute: bool = False
    ttl_days: int = TTL_DAYS


@dataclass
class Result:
    exit: int
    counts: Counter = field(default_factory=Counter)
    lines: list = field(default_factory=list)
    manifest: "Path | None" = None


def default_unlink(name: str, dir_fd: int) -> None:
    os.unlink(name, dir_fd=dir_fd)


# ------------------------------------------------------------------ fd helpers
def open_dir(name: str, dir_fd: "int | None" = None) -> int:
    return os.open(name, _DIR, dir_fd=dir_fd)


def open_path_nofollow(path: "str | os.PathLike") -> int:
    """An fd on ``path`` reached one component at a time from / with O_NOFOLLOW: a symlink at ANY
    component (the leaf included) fails (ELOOP/ENOTDIR). ENOENT means a component is absent."""
    fd = os.open("/", _DIR)
    try:
        for part in Path(os.path.abspath(os.fspath(path))).parts[1:]:
            nxt = os.open(part, _DIR, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_fd(fd: int, limit: "int | None" = None) -> bytes:
    chunks, off = [], 0
    while True:
        block = os.pread(fd, 1 << 20, off)
        if not block:
            return b"".join(chunks)
        chunks.append(block)
        off += len(block)
        if limit is not None and off > limit:
            raise ValueError("too large")


def hash_fd(fd: int) -> str:
    digest, off = hashlib.sha256(), 0
    while True:
        block = os.pread(fd, 1 << 20, off)
        if not block:
            return digest.hexdigest()
        digest.update(block)
        off += len(block)


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# ------------------------------------------------------------------ config
def load_ingest_uid(repo_root: "str | os.PathLike", getpwnam: Callable = pwd.getpwnam) -> int:
    """The memex ingest account's uid: instance.yaml persona_installer.chronicle_export.ingest_account
    resolved through the passwd database. Any problem is a refusal, never a guess."""
    try:
        doc = load_instance_document(Path(repo_root) / "instance.yaml")
        account = doc["persona_installer"]["chronicle_export"]["ingest_account"]
    except Exception as exc:  # noqa: BLE001 -- every failure to learn the trust anchor refuses
        raise Refused(f"cannot read persona_installer.chronicle_export.ingest_account: {exc!r}") from None
    if not isinstance(account, str) or not ACCOUNT_RE.match(account):
        raise Refused(f"ingest_account is not a valid account name: {account!r}")
    try:
        return int(getpwnam(account).pw_uid)
    except KeyError:
        raise Refused(f"ingest account {account!r} does not exist") from None


def build_config(args: argparse.Namespace, *, repo_root: "Path | None" = None,
                 euid: "int | None" = None) -> Config:
    repo = Path(repo_root) if repo_root else Path(__file__).resolve().parent.parent
    euid = os.geteuid() if euid is None else euid
    try:
        home = Path(pwd.getpwuid(euid).pw_dir)
    except KeyError:
        raise Refused(f"no passwd entry for uid {euid}") from None
    seat, why = chronicle_export.resolve_seat(persona=args.persona, harness="codex", account_home=home,
                                              repo_root=repo)
    if not seat or not SEAT_RE.match(seat):
        raise Refused(f"cannot resolve the export seat for {args.persona}: {why or seat!r}")
    ingest_uid = load_ingest_uid(repo)
    if args.state_dir:
        state = Path(args.state_dir)
    elif os.environ.get("PANTHEON_STATE_DIR"):
        state = Path(os.environ["PANTHEON_STATE_DIR"])
    else:
        state = home / ".local/state/pantheon"
    chron = Path(args.chronicle_root) if args.chronicle_root else chronicle.chronicle_root()
    return Config(
        persona=args.persona, seat=seat, uid=euid, ingest_uid=ingest_uid,
        export_root=chronicle_export.export_root(args.export_root), chronicle_root=chron, state_dir=state,
        codex_state_dir=Path(args.codex_state_dir) if args.codex_state_dir
        else home / ".local/state/pantheon/codex-appserver",
        codex_home=Path(args.codex_home) if args.codex_home else home / ".codex",
        execute=bool(args.execute))


# ------------------------------------------------------------------ journal, alerts
class Journal:
    """Counters, manifest lines and the run's alert-class events."""

    def __init__(self, fh=None):
        self.counts: Counter = Counter()
        self.kept: Counter = Counter()  # keep counts by reason, printed on the run summary line
        self.alerts: dict = {}
        self.errors: list = []
        self.fh = fh

    def log(self, rec: dict, sync: bool = False) -> None:
        if self.fh is None:
            return
        self.fh.write(json.dumps(rec, sort_keys=True) + "\n")
        self.fh.flush()
        if sync:
            os.fsync(self.fh.fileno())

    def keep(self, rel: str, reason: str, *, alert: bool = False, detail: str = "") -> None:
        self.counts[reason] += 1
        self.kept[reason] += 1
        self.log({"op": "keep", "rel": rel, "reason": reason, **({"detail": detail} if detail else {})})
        if alert:
            self.alerts.setdefault(f"{reason}:{rel}", detail or reason)

    def alert(self, key: str, detail: str) -> None:
        self.alerts.setdefault(key, detail)

    def error(self, rel: str, detail: str) -> None:
        self.errors.append(f"{rel}: {detail}")
        self.counts["errors"] += 1
        self.log({"op": "error", "rel": rel, "detail": detail}, sync=True)


def _load_alert_state(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {k: float(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_alert_state(path: Path, state: dict) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def flush_alerts(cfg: Config, journal: Journal, route_fn, now: datetime, state_path: Path) -> int:
    """Route ONE aggregated alert for the keys not alerted in the last ALERT_REPEAT_S; return how many
    keys were newly alerted. Keys not seen this run drop out of the state (a cleared condition re-alerts
    if it recurs). Alerting is best-effort: a routing failure is an error and leaves the keys unstamped."""
    state = _load_alert_state(state_path)
    stamp = now.timestamp()
    new = [k for k in sorted(journal.alerts) if stamp - state.get(k, -ALERT_REPEAT_S * 2) > ALERT_REPEAT_S]
    next_state = {k: state[k] for k in journal.alerts if k in state and k not in new}
    sent = 0
    if new:
        head = (f"chronicle-prune {cfg.persona}: {len(new)} condition(s) left files in place instead of "
                f"pruning them (nothing was deleted on a doubt). ")
        body = "; ".join(f"{k} ({journal.alerts[k]})"[:160] for k in new[:8])
        more = f"; +{len(new) - 8} more" if len(new) > 8 else ""
        msg = (head + body + more + f". Manifest: {cfg.state_dir}/{STATE_SUBDIR}/")[:1500]
        try:
            fn = route_fn
            if fn is None:
                if os.environ.get(ALERT_ENV, "on") == "off":
                    print(f"[chronicle_prune] alert suppressed by {ALERT_ENV}=off: {msg}", file=sys.stderr)
                    fn = False
                else:
                    from tools.inbox_consumer import route_failure_alert as fn  # lazy: only when one routes
            if fn:
                fn(cfg.persona, msg, ALERT_VOCAB)
                sent = len(new)
                next_state.update({k: stamp for k in new})
        except Exception as exc:  # noqa: BLE001
            journal.error("alert", f"routing failed: {type(exc).__name__}: {exc}")
    try:
        _save_alert_state(state_path, next_state)
    except OSError as exc:
        journal.error("alert-state", f"cannot save: {exc}")
    return sent


# ------------------------------------------------------------------ receipts
@dataclass(frozen=True)
class Receipt:
    file: str
    size: int
    sha256: str
    supersedes: tuple


@dataclass
class ReceiptResult:
    kind: str  # ok | missing | invalid
    receipt: "Receipt | None" = None
    reason: str = ""
    detail: str = ""


def _parse_receipt(doc: Any, cfg: Config, date: str, basename: str):
    """(Receipt, '') or (None, 'which field'). Required keys present and typed; unknown extras ignored."""
    if not isinstance(doc, dict):
        return None, "not an object"
    if doc.get("schema") != RECEIPT_SCHEMA:
        return None, "schema"
    if doc.get("seat") != cfg.seat:
        return None, "seat"
    if doc.get("file") != f"{cfg.seat}/{date}/{basename}":
        return None, "file"
    if not _is_int(doc.get("size")) or doc["size"] < 0:
        return None, "size"
    if not isinstance(doc.get("sha256"), str) or not HEX64.match(doc["sha256"]):
        return None, "sha256"
    if doc.get("raw_sha256_verified") is not True:
        return None, "raw_sha256_verified"
    if not isinstance(doc.get("raw_dest"), str) or not doc["raw_dest"]:
        return None, "raw_dest"
    if not isinstance(doc.get("memex_commit"), str) or not HEX40.match(doc["memex_commit"]):
        return None, "memex_commit"
    ids = doc.get("memex_record_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
        return None, "memex_record_ids"
    when = doc.get("ingested_utc")
    try:
        if not isinstance(when, str):
            raise ValueError
        datetime.fromisoformat(when.replace("Z", "+00:00"))
    except ValueError:
        return None, "ingested_utc"
    sup = doc.get("supersedes")
    if not isinstance(sup, list):
        return None, "supersedes"
    entries = []
    for e in sup:
        if not (isinstance(e, dict) and isinstance(e.get("file"), str) and _is_int(e.get("size"))
                and e["size"] >= 0 and isinstance(e.get("sha256"), str) and HEX64.match(e["sha256"])):
            return None, "supersedes"
        entries.append((e["file"], e["size"], e["sha256"]))
    return Receipt(doc["file"], doc["size"], doc["sha256"], tuple(entries)), ""


def _trusted_dir(fd: int, cfg: Config) -> bool:
    st = os.fstat(fd)
    return st.st_uid == cfg.ingest_uid and not st.st_mode & 0o022


def read_receipt(cfg: Config, root_fd: int, date: str, basename: str) -> ReceiptResult:
    """Validate the receipt for ``<seat>/<date>/<basename>``. ENOENT anywhere is ``missing`` (not
    ingested yet); anything that makes the trust anchor look wrong is ``invalid`` (alert)."""
    with ExitStack() as stack:
        cur = root_fd
        for comp in (INGESTED, cfg.seat, date):
            try:
                nxt = open_dir(comp, cur)
            except FileNotFoundError:
                return ReceiptResult("missing")
            except OSError as exc:
                return ReceiptResult("invalid", reason="receipt-dir-unsafe", detail=f"{comp}: {exc.strerror}")
            stack.callback(os.close, nxt)
            if not _trusted_dir(nxt, cfg):
                return ReceiptResult("invalid", reason="receipt-dir-untrusted",
                                     detail=f"{comp} is not owned by the ingest account or is group/world-writable")
            cur = nxt
        try:
            fd = os.open(basename + RECEIPT_SUFFIX, _REG, dir_fd=cur)
        except FileNotFoundError:
            return ReceiptResult("missing")
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return ReceiptResult("invalid", reason="receipt-symlink", detail=basename)
            return ReceiptResult("invalid", reason="receipt-unreadable", detail=f"{basename}: {exc.strerror}")
        with ExitStack() as inner:
            inner.callback(os.close, fd)
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != cfg.ingest_uid or st.st_mode & 0o022:
                return ReceiptResult("invalid", reason="receipt-untrusted",
                                     detail=f"{basename}: not a regular ingest-owned non-group-writable file")
            try:
                doc = json.loads(read_fd(fd, RECEIPT_MAX_BYTES))
            except (OSError, ValueError):
                return ReceiptResult("invalid", reason="receipt-unreadable", detail=basename)
    receipt, why = _parse_receipt(doc, cfg, date, basename)
    if receipt is None:
        return ReceiptResult("invalid", reason="receipt-schema", detail=f"{basename}: {why}")
    return ReceiptResult("ok", receipt)


# ------------------------------------------------------------------ discovery helpers
def parse_name(name: str) -> "tuple[str, str] | None":
    """(stem, kind) for an exportable basename; None for anything else (never touched)."""
    if name.startswith("."):
        return None
    if name.endswith(".session.json"):
        return name[: -len(".session.json")], "session"
    if name.endswith(".summary.md"):
        return name[: -len(".summary.md")], "summary"
    if name.endswith(".md"):
        return name[: -len(".md")], "md"
    m = GZ_RE.match(name)
    if m and chronicle_export.is_transcript_gz(name):
        return m.group("stem"), "gz"
    return None


def _md_sort_key(date: str, name: str):
    m = STEM_RE.match(name[: -len(".md")])
    if not m:
        return (date, "000000", "0" * 9, name)
    return (m.group(1), m.group(2), (m.group(3) or ".")[1:].ljust(9, "0"), name)


def protected_md_set(chron_agent_fd: int) -> "set | None":
    """{(date, name)} of the newest KEEP_NEWEST_MD regular <stem>.md in the local Chronicle (summaries
    excluded). None when the listing fails: the caller then protects EVERY .md."""
    cands = []
    try:
        dates = [d for d in os.listdir(chron_agent_fd) if DATE_RE.match(d)]
        for d in dates:
            dfd = open_dir(d, chron_agent_fd)
            try:
                for n in os.listdir(dfd):
                    if n.endswith(".md") and not n.endswith(".summary.md") and not n.startswith("."):
                        st = os.stat(n, dir_fd=dfd, follow_symlinks=False)
                        if stat.S_ISREG(st.st_mode):
                            cands.append((_md_sort_key(d, n), (d, n)))
            finally:
                os.close(dfd)
    except OSError:
        return None
    cands.sort(reverse=True)
    return {item for _k, item in cands[:KEEP_NEWEST_MD]}


def load_open_claims(chron_agent_fd: int) -> "set | None":
    """Stems named by a claim that is NOT done: such a claim can still resume its record and re-write
    its .session.json (session_chronicle._predecessor_record). None when .claims cannot be read."""
    try:
        cfd = open_dir(".claims", chron_agent_fd)
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    stems = set()
    try:
        for n in os.listdir(cfd):
            if not n.endswith(".json") or n.startswith("."):
                continue
            fd = os.open(n, _REG, dir_fd=cfd)
            try:
                doc = json.loads(read_fd(fd, JSON_MAX_BYTES))
            finally:
                os.close(fd)
            if not isinstance(doc, dict):
                return None
            ts, uu = doc.get("stem_ts"), doc.get("stem_uuid")
            if doc.get("state") != "done" and isinstance(ts, str) and isinstance(uu, str):
                stems.add(f"{chronicle._fs_safe_ts(ts)}-{uu}")
    except (OSError, ValueError):
        return None
    finally:
        os.close(cfd)
    return stems


@dataclass
class Member:
    name: str
    kind: str
    rel: str
    fd: int
    st: os.stat_result
    sha: "str | None" = None
    keep_local: bool = False
    chron_unlink: bool = False
    via: "str | None" = None


# ------------------------------------------------------------------ the pruner
class Pruner:
    def __init__(self, cfg: Config, journal: Journal, now: datetime, unlink: Callable):
        self.cfg, self.j, self.now, self.unlink = cfg, journal, now, unlink
        self.stack = ExitStack()
        self.root_fd = self.seat_fd = self.chron_fd = -1
        self.hist_fd: Optional[int] = None
        self.state_fd: Optional[int] = None
        self.home_fd: Optional[int] = None
        self._ss_index: Optional[dict] = None
        self.protected: "set | None" = set()
        self.claims: "set | None" = set()

    # -- setup ------------------------------------------------------------
    def _open(self, what: str, path: Path) -> int:
        try:
            fd = open_path_nofollow(path)
        except OSError as exc:
            raise Refused(f"{what} {path}: {exc.strerror or exc} (a missing path, or a symlink at any component)") from None
        self.stack.callback(os.close, fd)
        return fd

    def _owned(self, what: str, fd: int) -> None:
        if os.fstat(fd).st_uid != self.cfg.uid:
            raise Refused(f"{what} is not owned by the running uid {self.cfg.uid}")

    def prepare(self) -> None:
        cfg = self.cfg
        if cfg.uid == 0:
            raise Refused("refusing to run as root: this tool runs AS the seat")
        self.root_fd = self._open("export root", cfg.export_root)
        try:
            self.seat_fd = open_dir(cfg.seat, self.root_fd)
        except OSError as exc:
            raise Refused(f"seat export dir {cfg.export_root / cfg.seat}: {exc.strerror or exc}") from None
        self.stack.callback(os.close, self.seat_fd)
        self._owned("the seat export dir", self.seat_fd)
        self.chron_fd = self._open("Chronicle agent dir", cfg.chronicle_root / chronicle.agent_dir_name(cfg.persona))
        self._owned("the Chronicle agent dir", self.chron_fd)
        if cfg.codex_state_dir is not None:
            try:
                self.state_fd = self._open("codex state dir", cfg.codex_state_dir)
            except Refused:
                if cfg.codex_state_dir.is_symlink() or os.path.lexists(cfg.codex_state_dir):
                    raise
                self.state_fd = None  # absent (a Claude seat): the phase is skipped
            if self.state_fd is not None:
                try:
                    self.hist_fd = open_dir(HISTORY_DIR, self.state_fd)
                    self.stack.callback(os.close, self.hist_fd)
                except FileNotFoundError:
                    self.hist_fd = None
                except OSError as exc:
                    raise Refused(f"codex thread-history: {exc.strerror or exc}") from None
        if self.hist_fd is not None and cfg.codex_home is not None:
            try:
                self.home_fd = self._open("codex home", cfg.codex_home)
            except Refused:
                if os.path.lexists(cfg.codex_home):
                    raise
                self.home_fd = None

    # -- receipts ---------------------------------------------------------
    def receipt(self, date: str, basename: str) -> ReceiptResult:
        return read_receipt(self.cfg, self.root_fd, date, basename)

    def supersede_index(self) -> dict:
        """rel -> [(size, sha256, via)] from every FULLY VALID receipt of this seat (capped, fail closed)."""
        if self._ss_index is not None:
            return self._ss_index
        index: dict = {}
        scanned = 0
        try:
            with ExitStack() as stack:
                ifd = open_dir(INGESTED, self.root_fd)
                stack.callback(os.close, ifd)
                sfd = open_dir(self.cfg.seat, ifd)
                stack.callback(os.close, sfd)
                for date in sorted(os.listdir(sfd)):
                    if not DATE_RE.match(date):
                        continue
                    dfd = open_dir(date, sfd)
                    try:
                        names = sorted(os.listdir(dfd))
                    finally:
                        os.close(dfd)
                    for n in names:
                        if not n.endswith(RECEIPT_SUFFIX):
                            continue
                        scanned += 1
                        if scanned > RECEIPT_SCAN_CAP:
                            self.j.counts["receipt-scan-capped"] += 1
                            self._ss_index = {}
                            return self._ss_index
                        res = self.receipt(date, n[: -len(RECEIPT_SUFFIX)])
                        if res.kind != "ok":
                            continue
                        for f, size, digest in res.receipt.supersedes:
                            if f.startswith(self.cfg.seat + "/"):
                                index.setdefault(f, []).append((size, digest, f"{self.cfg.seat}/{date}/{n}"))
        except OSError:
            index = {}
        self._ss_index = index
        return index

    # -- one record -------------------------------------------------------
    def _open_member(self, date: str, date_fd: int, name: str, kind: str) -> Optional[Member]:
        rel = f"{self.cfg.seat}/{date}/{name}"
        try:
            fd = os.open(name, _REG, dir_fd=date_fd)
        except OSError as exc:
            self.j.keep(rel, "unsafe-file", alert=True, detail=f"cannot open without following links: {exc.strerror}")
            return None
        try:
            st = os.fstat(fd)
        except BaseException:
            os.close(fd)
            raise
        if not stat.S_ISREG(st.st_mode) or st.st_uid != self.cfg.uid:
            os.close(fd)
            self.j.keep(rel, "unsafe-file", alert=True, detail="not a regular file owned by the running uid")
            return None
        return Member(name, kind, rel, fd, st)

    def _twin(self, chron_date_fd: Optional[int], m: Member) -> bool:
        """Verify the Chronicle name is the same inode; set m.chron_unlink. False = keep the record."""
        st2 = None
        if chron_date_fd is not None:
            try:
                st2 = os.stat(m.name, dir_fd=chron_date_fd, follow_symlinks=False)
            except FileNotFoundError:
                st2 = None
            except OSError as exc:
                self.j.keep(m.rel, "chronicle-copy-unsafe", alert=True, detail=str(exc.strerror))
                return False
        if st2 is None:
            if m.st.st_nlink != 1:
                self.j.keep(m.rel, "unexpected-nlink", alert=True, detail=f"no Chronicle copy but nlink={m.st.st_nlink}")
                return False
            self.j.counts["resume"] += 1
            m.chron_unlink = False
            return True
        if not stat.S_ISREG(st2.st_mode):
            self.j.keep(m.rel, "chronicle-copy-unsafe", alert=True, detail="Chronicle copy is not a regular file")
            return False
        if (st2.st_dev, st2.st_ino) != (m.st.st_dev, m.st.st_ino):
            self.j.keep(m.rel, "inode-mismatch", alert=True, detail="Chronicle copy is a different inode")
            return False
        if m.st.st_nlink != 2:
            self.j.keep(m.rel, "unexpected-nlink", alert=True, detail=f"nlink={m.st.st_nlink}, expected 2")
            return False
        m.chron_unlink = not m.keep_local
        return True

    def _covered(self, date: str, chron_date_fd: Optional[int], m: Member) -> bool:
        """True when this member has a matching, valid receipt (own, or a supersede entry)."""
        j, cfg = self.j, self.cfg
        res = self.receipt(date, m.name)
        if res.kind == "invalid":
            j.keep(m.rel, res.reason, alert=True, detail=res.detail)
            return False
        if res.kind == "ok":
            r = res.receipt
            if m.st.st_size != r.size:
                j.keep(m.rel, "size-mismatch", alert=True, detail=f"disk {m.st.st_size} != receipt {r.size}")
                return False
            m.sha = hash_fd(m.fd)
            if m.sha != r.sha256:
                j.keep(m.rel, "sha-mismatch", alert=True, detail="bytes on disk differ from the receipt")
                return False
        else:
            entries = self.supersede_index().get(m.rel)
            if entries:
                m.sha = hash_fd(m.fd)
                hit = next((via for size, digest, via in entries if size == m.st.st_size and digest == m.sha), None)
                if hit is None:
                    j.keep(m.rel, "supersede-mismatch", alert=True, detail="a receipt names this file with other bytes")
                    return False
                m.via = hit
            else:
                j.keep(m.rel, "no-receipt")
                if self.now.timestamp() - m.st.st_ctime > cfg.ttl_days * 86400:
                    j.counts["ttl"] += 1
                    j.alert(f"ttl:{m.rel}", f"exported over {cfg.ttl_days}d ago, still no receipt")
                return False
        return self._twin(chron_date_fd, m)

    def record(self, date: str, date_fd: int, chron_date_fd: Optional[int], stem: str, names: dict) -> None:
        j, cfg = self.j, self.cfg
        rrel = f"{cfg.seat}/{date}/{stem}"
        sm = STEM_RE.match(stem)
        if not sm or sm.group(1) != date:
            j.keep(rrel, "stem-unparseable" if not sm else "date-mismatch", alert=True,
                   detail="stem does not parse as <date>T<time>Z-<id>, or is in the wrong date dir")
            return
        if "session" not in {k for k in names.values()}:
            j.keep(rrel, "no-session-json")
            return
        if self.claims is None:
            j.keep(rrel, "claims-unreadable", alert=True, detail=".claims cannot be read; keeping every record")
            return
        if stem in self.claims:
            j.keep(rrel, "claim-not-done")
            return
        with ExitStack() as stack:
            members = []
            for name, kind in sorted(names.items(), key=lambda kv: (_KIND_ORDER[kv[1]], kv[0])):
                m = self._open_member(date, date_fd, name, kind)
                if m is None:
                    j.counts["record-kept"] += 1
                    return
                stack.callback(os.close, m.fd)
                members.append(m)
            session = next(m for m in members if m.kind == "session")
            try:
                doc = json.loads(read_fd(session.fd, JSON_MAX_BYTES))
                persona_ok = isinstance(doc, dict) and doc.get("persona") == cfg.persona
            except (OSError, ValueError):
                persona_ok = False
            if not persona_ok:
                j.keep(session.rel, "foreign-persona", alert=True,
                       detail=f"session.json is unreadable or not persona {cfg.persona}")
                return
            for m in members:
                m.keep_local = m.kind == "md" and (self.protected is None or (date, m.name) in self.protected)
            ok = [self._covered(date, chron_date_fd, m) for m in members]
            if not all(ok):
                j.counts["record-kept"] += 1
                j.kept["partial-record"] += 1
                bad = [m.name for m, good in zip(members, ok) if not good]
                j.log({"op": "keep", "rel": rrel, "reason": "partial-record", "unreceipted": bad})
                return
            self._execute(date, date_fd, chron_date_fd, members)

    def _unchanged(self, m: Member, dfd: int, nlink: int) -> str:
        try:
            st = os.stat(m.name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return "gone"
        now_fd = os.fstat(m.fd)
        same = ((st.st_dev, st.st_ino) == (m.st.st_dev, m.st.st_ino) and stat.S_ISREG(st.st_mode)
                and st.st_nlink == nlink and now_fd.st_size == m.st.st_size
                and now_fd.st_mtime_ns == m.st.st_mtime_ns)
        return "ok" if same else "changed"

    def _execute(self, date: str, date_fd: int, chron_date_fd: Optional[int], members: list) -> None:
        j, cfg = self.j, self.cfg
        for m in members:  # already gz, md, summary, session: the .session.json goes LAST
            sides = ([("chronicle", chron_date_fd)] if m.chron_unlink else []) + [("export", date_fd)]
            for side, dfd in sides:
                expect = m.st.st_nlink - (1 if side == "export" and m.chron_unlink else 0)
                label = f"{cfg.seat}/{date}/{m.name}"
                if not cfg.execute:
                    j.log({"op": "would-delete", "side": side, "rel": label, "bytes": m.st.st_size,
                           **({"via": m.via} if m.via else {})})
                    continue
                state = self._unchanged(m, dfd, expect)
                if state == "changed":
                    j.keep(label, "changed-during-prune", alert=True,
                           detail="the file changed (inode, links, size or mtime) between verification and unlink")
                    return
                if state == "gone":
                    continue
                j.log({"op": "delete", "phase": "begin", "side": side, "rel": label}, sync=True)
                try:
                    self.unlink(m.name, dfd)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    j.error(label, f"unlink ({side}) failed: {exc.strerror or exc}")
                    return
                j.log({"op": "delete", "phase": "done", "side": side, "rel": label, "bytes": m.st.st_size,
                       **({"via": m.via} if m.via else {})}, sync=True)
            j.counts["pruned-files" if cfg.execute else "would-prune-files"] += 1
            j.counts["pruned-bytes" if cfg.execute else "would-prune-bytes"] += m.st.st_size

    # -- passes -----------------------------------------------------------
    def sweep_export(self) -> None:
        j, cfg = self.j, self.cfg
        self.protected = protected_md_set(self.chron_fd)
        self.claims = load_open_claims(self.chron_fd)
        if self.protected is None:
            j.counts["keep3-unknown"] += 1
            j.alert("keep3-unknown", "the newest-3 .md listing failed; every .md was kept locally")
        for date in sorted(os.listdir(self.seat_fd)):
            if not DATE_RE.match(date):
                j.counts["foreign-entry"] += 1
                continue
            rel = f"{cfg.seat}/{date}"
            try:
                date_fd = open_dir(date, self.seat_fd)
            except OSError:
                j.keep(rel, "date-dir-symlink", alert=True, detail="not a plain directory")
                continue
            with ExitStack() as stack:
                stack.callback(os.close, date_fd)
                if os.fstat(date_fd).st_uid != cfg.uid:
                    j.keep(rel, "date-dir-foreign", alert=True, detail="not owned by the running uid")
                    continue
                try:
                    chron_date_fd = open_dir(date, self.chron_fd)
                    stack.callback(os.close, chron_date_fd)
                except FileNotFoundError:
                    chron_date_fd = None
                except OSError:
                    j.keep(f"chronicle/{date}", "chronicle-date-unsafe", alert=True, detail="not a plain directory")
                    continue
                groups: dict = {}
                for name in sorted(os.listdir(date_fd)):
                    parsed = parse_name(name)
                    if parsed is None:
                        j.counts["unrecognized"] += 1
                        continue
                    groups.setdefault(parsed[0], {})[name] = parsed[1]
                for stem in sorted(groups):
                    self.record(date, date_fd, chron_date_fd, stem, groups[stem])

    def aged_out_keep3(self) -> None:
        """Chronicle-side phase: a <stem>.md the newest-3 rule once protected has lost its export link and
        its Chronicle .session.json, so the export-driven sweep can never see it again. Once it is out of
        the newest 3 it is released by its OWN fully valid receipt alone (no supersede), matching the bytes
        now. Any doubt keeps it; an unknown newest-3 set or unreadable claims does nothing."""
        j, cfg = self.j, self.cfg
        if self.protected is None or self.claims is None:
            return
        for date in sorted(os.listdir(self.chron_fd)):
            if not DATE_RE.match(date):
                continue
            try:
                cfd = open_dir(date, self.chron_fd)
            except OSError:
                continue
            with ExitStack() as stack:
                stack.callback(os.close, cfd)
                try:
                    efd = open_dir(date, self.seat_fd)
                    stack.callback(os.close, efd)
                except FileNotFoundError:
                    efd = None
                except OSError:
                    continue  # a symlinked/odd export date dir: not ours to reason about
                for name in sorted(os.listdir(cfd)):
                    if not name.endswith(".md") or name.endswith(".summary.md") or name.startswith("."):
                        continue
                    if (date, name) in self.protected:
                        continue
                    try:
                        st = os.stat(name, dir_fd=cfd, follow_symlinks=False)
                    except OSError:
                        continue
                    if not stat.S_ISREG(st.st_mode) or st.st_uid != cfg.uid or st.st_nlink != 1:
                        continue
                    self._aged_out_one(date, cfd, efd, name)

    def _aged_out_one(self, date: str, cfd: int, efd: Optional[int], name: str) -> None:
        j, cfg = self.j, self.cfg
        stem = name[: -len(".md")]
        rel = f"{cfg.seat}/{date}/{name}"
        sm = STEM_RE.match(stem)
        if not sm or sm.group(1) != date:
            j.counts["aged-out-skipped-stem"] += 1
            return
        if efd is not None:  # the export name present: the export-driven path owns it
            try:
                os.stat(name, dir_fd=efd, follow_symlinks=False)
                j.counts["aged-out-export-present"] += 1
                return
            except FileNotFoundError:
                pass
            except OSError:
                return
        try:
            os.stat(stem + ".session.json", dir_fd=cfd, follow_symlinks=False)
            j.kept["aged-out-session-json-present"] += 1
            return
        except FileNotFoundError:
            pass
        except OSError:
            return
        if stem in self.claims:
            j.kept["aged-out-claim-not-done"] += 1
            return
        res = self.receipt(date, name)
        if res.kind == "missing":
            j.counts["aged-out-no-receipt"] += 1
            j.kept["aged-out-no-receipt"] += 1
            return
        if res.kind == "invalid":
            j.keep(rel, res.reason, alert=True, detail=res.detail)
            return
        m = self._open_member(date, cfd, name, "md")
        if m is None:
            return
        try:
            r = res.receipt
            if m.st.st_size != r.size:
                j.keep(rel, "size-mismatch", alert=True, detail=f"disk {m.st.st_size} != receipt {r.size}")
                return
            m.sha = hash_fd(m.fd)
            if m.sha != r.sha256:
                j.keep(rel, "sha-mismatch", alert=True, detail="bytes on disk differ from the receipt")
                return
            base = {"side": "chronicle", "rel": rel, "bytes": m.st.st_size, "reason": "aged-out-keep3"}
            if not cfg.execute:
                j.log({"op": "would-delete", **base})
                j.counts["would-prune-files"] += 1
                j.counts["would-prune-bytes"] += m.st.st_size
                j.counts["aged-out-keep3"] += 1
                return
            state = self._unchanged(m, cfd, 1)
            if state == "changed":
                j.keep(rel, "changed-during-prune", alert=True,
                       detail="the file changed (inode, links, size or mtime) between verification and unlink")
                return
            if state == "gone":
                return
            j.log({"op": "delete", "phase": "begin", **base}, sync=True)
            try:
                self.unlink(name, cfd)
            except FileNotFoundError:
                pass
            except OSError as exc:
                j.error(rel, f"unlink (chronicle) failed: {exc.strerror or exc}")
                return
            j.log({"op": "delete", "phase": "done", **base}, sync=True)
            j.counts["pruned-files"] += 1
            j.counts["pruned-bytes"] += m.st.st_size
            j.counts["aged-out-keep3"] += 1
        finally:
            os.close(m.fd)

    def sweep_tmp(self) -> None:
        """Stray archive temp files in the seat's OWN Chronicle date dirs; the helper's exact pattern only."""
        j, cfg = self.j, self.cfg
        for date in sorted(os.listdir(self.chron_fd)):
            if not DATE_RE.match(date):
                continue
            try:
                dfd = open_dir(date, self.chron_fd)
            except OSError:
                continue
            try:
                for name in sorted(os.listdir(dfd)):
                    if not TMP_RE.match(name):
                        continue
                    st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                    if (not stat.S_ISREG(st.st_mode) or st.st_uid != cfg.uid or st.st_nlink != 1):
                        j.counts["tmp-lookalike-kept"] += 1
                        continue
                    if self.now.timestamp() - st.st_mtime <= TMP_MIN_AGE_S:
                        j.counts["tmp-young"] += 1
                        continue
                    rel = f"chronicle/{cfg.persona}/{date}/{name}"
                    if not cfg.execute:
                        j.log({"op": "would-sweep", "rel": rel, "bytes": st.st_size})
                        j.counts["tmp-would-sweep"] += 1
                        continue
                    j.log({"op": "sweep", "rel": rel, "bytes": st.st_size}, sync=True)
                    try:
                        self.unlink(name, dfd)
                        j.counts["tmp-swept"] += 1
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        j.error(rel, f"sweep failed: {exc.strerror or exc}")
            finally:
                os.close(dfd)

    # -- codex thread history ---------------------------------------------
    def _read_json_at(self, dir_fd: int, name: str) -> "tuple[Any, os.stat_result]":
        fd = os.open(name, _REG, dir_fd=dir_fd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != self.cfg.uid:
                raise ValueError("not a regular file owned by the running uid")
            return json.loads(read_fd(fd, JSON_MAX_BYTES)), st
        finally:
            os.close(fd)

    def _rollout_fd(self, rollout: str) -> "tuple[str, Any]":
        """('unsafe'|'absent'|'ok', (parent_fd, fd) or None). Lexical containment under codex_home/sessions,
        then an O_NOFOLLOW fd chain from the codex home (never a path open)."""
        base = os.path.normpath(str(self.cfg.codex_home / "sessions")) + os.sep
        rp = os.path.normpath(rollout)
        if self.home_fd is None or not os.path.isabs(rollout) or not rp.startswith(base):
            return "unsafe", None
        parts = ["sessions"] + rp[len(base):].split(os.sep)
        cur = self.home_fd
        opened = []
        try:
            for comp in parts[:-1]:
                try:
                    cur = open_dir(comp, cur)
                except FileNotFoundError:
                    return "absent", None
                except OSError:
                    return "unsafe", None
                opened.append(cur)
            try:
                fd = os.open(parts[-1], _REG, dir_fd=cur)
            except FileNotFoundError:
                return "absent", None
            except OSError:
                return "unsafe", None
            try:
                parent = os.dup(cur)
            except BaseException:
                os.close(fd)
                raise
            return "ok", (parent, fd)
        finally:
            for o in opened:
                os.close(o)

    def _stamp(self, name: str, note: "str | None") -> None:
        """Set rollout_pruned_at on a history entry: temp + fsync + rename, aborted if the entry changed."""
        hfd = self.hist_fd
        doc, st0 = self._read_json_at(hfd, name)
        doc["rollout_pruned_at"] = _iso(self.now)
        if note:
            doc["rollout_pruned_note"] = note
        tmp = f".{name}.tmp-{os.getpid()}"
        tfd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                      0o600, dir_fd=hfd)
        try:
            with os.fdopen(tfd, "wb") as handle:
                handle.write((json.dumps(doc, indent=1, sort_keys=True) + "\n").encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            st1 = os.stat(name, dir_fd=hfd, follow_symlinks=False)
            if (st1.st_ino, st1.st_mtime_ns, st1.st_size) != (st0.st_ino, st0.st_mtime_ns, st0.st_size):
                raise OSError(errno.ESTALE, "history entry changed while stamping")
            os.replace(tmp, name, src_dir_fd=hfd, dst_dir_fd=hfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=hfd)
            except OSError:
                pass
            raise
        os.fsync(hfd)

    def codex_phase(self) -> None:
        j, cfg = self.j, self.cfg
        if self.hist_fd is None:
            j.counts["codex-phase-skipped"] += 1
            return
        entries = []
        for name in sorted(os.listdir(self.hist_fd)):
            if name.startswith("."):
                continue
            try:
                doc, _st = self._read_json_at(self.hist_fd, name)
                if not isinstance(doc, dict):
                    raise ValueError("not an object")
            except (OSError, ValueError):
                j.keep(f"thread-history/{name}", "history-unreadable", alert=True,
                       detail="a history entry cannot be parsed; the codex phase did nothing this run")
                return
            entries.append((name, doc))
        cands = [(n, d) for n, d in entries if n.endswith(".json")]
        if not cands:
            return
        try:
            cur_doc, _ = self._read_json_at(self.state_fd, "thread.json")
            current = cur_doc["thread_id"]
            if not isinstance(current, str) or not current:
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            j.counts["codex-thread-unknown"] += 1
            return
        newest_name, newest_doc = cands[-1]
        protected = {os.path.normpath(str(d.get("rollout"))) for n, d in entries
                     if n == newest_name or not n.endswith(".json")}
        for name, doc in cands[:-1]:
            self._codex_entry(name, doc, protected, current)

    def _codex_entry(self, name: str, doc: dict, protected: set, current: str) -> None:
        j, cfg = self.j, self.cfg
        rel = f"thread-history/{name}"
        tid, rollout, arc = doc.get("old_thread_id"), doc.get("rollout"), doc.get("archive")
        if not (isinstance(tid, str) and tid and isinstance(rollout, str) and isinstance(arc, dict)
                and isinstance(arc.get("path"), str) and _is_int(arc.get("bytes"))
                and isinstance(arc.get("sha256"), str) and HEX64.match(arc["sha256"])):
            j.keep(rel, "history-malformed", alert=True, detail="missing old_thread_id / rollout / archive pointer")
            return
        base = os.path.basename(rollout)
        if not re.fullmatch(r"rollout-.*-" + re.escape(tid) + r"\.jsonl", base):
            j.keep(rel, "rollout-path-unsafe", alert=True, detail="rollout name does not match the entry's thread id")
            return
        state, handles = self._rollout_fd(rollout)
        if state == "unsafe":
            j.keep(rel, "rollout-path-unsafe", alert=True,
                   detail="rollout is outside codex-home/sessions, behind a link, or not a plain path")
            return
        with ExitStack() as stack:
            if handles:
                stack.callback(os.close, handles[0])
                stack.callback(os.close, handles[1])
            stamped = "rollout_pruned_at" in doc
            if state == "absent":
                if stamped:
                    j.counts["codex-already-pruned"] += 1
                    return
                if not cfg.execute:
                    j.counts["codex-stamp-would-apply"] += 1
                    return
                try:
                    self._stamp(name, "rollout already absent")
                    j.counts["codex-stamped-absent"] += 1
                except (OSError, ValueError) as exc:
                    j.error(rel, f"stamp failed: {exc}")
                return
            if stamped:
                j.keep(rel, "stamped-but-present")
                return
            if os.path.normpath(rollout) in protected or base.endswith(f"-{current}.jsonl"):
                j.keep(rel, "rollout-protected")
                return
            parent_fd, fd = handles
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != cfg.uid:
                j.keep(rel, "rollout-path-unsafe", alert=True, detail="rollout is not a regular file owned by the running uid")
                return
            if not _is_int(arc.get("source_bytes")):
                j.keep(rel, "rollout-size-unrecorded", alert=True, detail="archive pointer has no source_bytes")
                return
            if st.st_size != arc["source_bytes"]:
                j.keep(rel, "rollout-size-mismatch", alert=True, detail=f"disk {st.st_size} != archived {arc['source_bytes']}")
                return
            src_sha = arc.get("source_sha256")
            if isinstance(src_sha, str) and HEX64.match(src_sha):
                if hash_fd(fd) != src_sha:
                    j.keep(rel, "rollout-sha-mismatch", alert=True, detail="rollout bytes differ from the archived source")
                    return
            else:
                j.keep(rel, "rollout-sha-not-recorded", detail="archive pointer has no source_sha256; bytes cannot be proven equal")
                return
            gz = Path(arc["path"])
            if not DATE_RE.match(gz.parent.name) or not chronicle_export.is_transcript_gz(gz.name):
                j.keep(rel, "archive-receipt-invalid", alert=True, detail="archive pointer is not a transcript .gz path")
                return
            res = self.receipt(gz.parent.name, gz.name)
            if res.kind == "missing":
                j.keep(rel, "archive-receipt-missing")
                return
            if res.kind == "invalid":
                j.keep(rel, "archive-receipt-invalid", alert=True, detail=f"{res.reason}: {res.detail}")
                return
            if res.receipt.size != arc["bytes"] or res.receipt.sha256 != arc["sha256"]:
                j.keep(rel, "archive-receipt-mismatch", alert=True, detail="receipt does not match the archive pointer")
                return
            label = f"{rel} -> {rollout}"
            if not cfg.execute:
                j.log({"op": "would-delete", "side": "codex-rollout", "rel": label, "bytes": st.st_size})
                j.counts["codex-rollout-would-prune"] += 1
                return
            try:
                now_st = os.stat(base, dir_fd=parent_fd, follow_symlinks=False)
                if (now_st.st_dev, now_st.st_ino) != (st.st_dev, st.st_ino):
                    j.keep(rel, "changed-during-prune", alert=True, detail="rollout inode changed before unlink")
                    return
                j.log({"op": "delete", "phase": "begin", "side": "codex-rollout", "rel": label}, sync=True)
                self.unlink(base, parent_fd)
                j.log({"op": "delete", "phase": "done", "side": "codex-rollout", "rel": label,
                       "bytes": st.st_size}, sync=True)
                j.counts["codex-rollout-pruned"] += 1
                self._stamp(name, None)
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as exc:
                j.error(rel, f"{exc}")


# ------------------------------------------------------------------ entry points
def run(cfg: Config, *, route_fn: "Callable | None" = None, now: "datetime | None" = None,
        unlink: "Callable | None" = None) -> Result:
    now = now or datetime.now(timezone.utc)
    journal = Journal()
    pruner = Pruner(cfg, journal, now, unlink or default_unlink)
    with pruner.stack:
        try:
            pruner.prepare()
        except Refused as exc:
            return Result(EXIT_REFUSED, lines=[f"REFUSED: {exc}"])
        out = Path(cfg.state_dir) / STATE_SUBDIR
        out.mkdir(parents=True, exist_ok=True, mode=0o700)
        manifest = out / (now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".jsonl")
        with open(manifest, "a", encoding="utf-8") as fh:
            journal.fh = fh
            journal.log({"op": "header", "persona": cfg.persona, "seat": cfg.seat, "execute": cfg.execute,
                         "at": _iso(now), "ttl_days": cfg.ttl_days})
            try:
                pruner.sweep_export()
                pruner.aged_out_keep3()
                pruner.sweep_tmp()
                pruner.codex_phase()
            except OSError as exc:
                journal.error("run", f"unexpected filesystem error: {exc!r}")
            new = flush_alerts(cfg, journal, route_fn, now, out / "alert-state.json")
            journal.counts["alerts-new"] = new
            journal.log({"op": "summary", "counts": dict(journal.counts), "kept": dict(journal.kept)}, sync=True)
    mode = "EXECUTE" if cfg.execute else "DRY-RUN"
    summary = ", ".join(f"{k}={v}" for k, v in sorted(journal.counts.items())) or "nothing to do"
    if journal.kept:
        summary += "; " + ", ".join(f"kept-{k}={v}" for k, v in sorted(journal.kept.items()))
    lines = [f"chronicle_prune persona={cfg.persona} seat={cfg.seat} {mode}: {summary}",
             f"manifest: {manifest}"] + [f"ERROR {e}" for e in journal.errors]
    return Result(EXIT_ALERT if (new or journal.errors) else EXIT_OK, journal.counts, lines, manifest)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--persona", required=True)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="the default: report, delete nothing")
    mode.add_argument("--execute", action="store_true", help="actually delete (what the systemd unit passes)")
    ap.add_argument("--export-root", help=argparse.SUPPRESS)
    ap.add_argument("--chronicle-root", help=argparse.SUPPRESS)
    ap.add_argument("--state-dir", help=argparse.SUPPRESS)
    ap.add_argument("--codex-state-dir", help=argparse.SUPPRESS)
    ap.add_argument("--codex-home", help=argparse.SUPPRESS)
    return ap


def main(argv: "list | None" = None, *, route_fn: "Callable | None" = None,
         now: "datetime | None" = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if os.geteuid() == 0:
            raise Refused("refusing to run as root: this tool runs AS the seat")
        from tools.session_chronicle import valid_persona
        if not valid_persona(args.persona):
            raise Refused(f"invalid persona {args.persona!r}")
        cfg = build_config(args)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if not cfg.execute:
        flagged = "" if args.dry_run else " (no --execute given)"
        print(f"DRY-RUN{flagged}: nothing will be deleted")
    res = run(cfg, route_fn=route_fn, now=now)
    for line in res.lines:
        print(line, file=sys.stderr if res.exit == EXIT_REFUSED else sys.stdout)
    return res.exit


if __name__ == "__main__":
    sys.exit(main())
