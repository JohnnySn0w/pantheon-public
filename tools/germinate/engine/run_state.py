"""The run-state store (ORG-080 §4; schema pantheon-run-state/v1).

One directory per run, at the ORG-080 bijective path
``<scope_root>/schema-v1/<instance>/<component>/<scope>/<leg>/<run_id>/``.
It holds ``record.json`` (this schema), ``plan.json`` (the run's immutable plan
node), ``transcript.jsonl`` and the T-3336 WAL artifacts (``manifest.json``,
``intent-*.json``, ``receipt-*.json``) written through the same held directory.

The WAL manifest is authoritative for committed receipts; ``record.receipts``
is copied from it after each commit, and a disagreement refuses (one writer of
record per fact). Every state change is checked against the planner_sandbox
state machine's closed transition table (C2); there is no free-form status.
"""

from __future__ import annotations

import datetime as _dt
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from tools.germinate.planner_sandbox.state_machine.states import INITIAL_STATE, State
from tools.germinate.planner_sandbox.state_machine.transitions import TRANSITIONS
from tools.germinate.planner_sandbox.wal import (
    AuthorityError,
    HeldDir,
    WalStore,
    canonical_json_bytes,
    load_canonical_json,
)

SCHEMA = "pantheon-run-state/v1"
RECORD_KEYS = frozenset(
    {"schema", "tuple", "state", "operations", "receipts", "halted", "rollback_incomplete", "parking", "created_at", "updated_at"}
)
TUPLE_KEYS = ("instance", "component", "scope", "leg", "run_id")
DIR_MODE = 0o700
FILE_MODE = 0o600
_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEX_LOWER = "0123456789abcdef"
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_EDGES = frozenset((t.frm, t.to) for t in TRANSITIONS)
MAX_SEGMENT = 64
# The ONE bound on a run's plan.json, used by encode_plan/write_plan AND read_plan
# (T-5809): a full-seat install node is 86-123 KB, over the generic 64 KiB WAL
# artifact cap, which stays in force for every other run-state artifact.
MAX_PLAN_BYTES = 1 << 20


class RunStateError(RuntimeError):
    """A run-state record or path is refused (malformed, tampered, or an illegal transition)."""


def encode_plan(plan: Mapping[str, Any]) -> bytes:
    """Canonical plan.json bytes under MAX_PLAN_BYTES, or CanonicalJsonError("json-too-large").

    The executor calls this BEFORE claiming or admitting a run (T-5809), so an
    oversized plan refuses with no claim held and no run record created."""

    return canonical_json_bytes(plan, max_bytes=MAX_PLAN_BYTES)


# ---------------------------------------------------------------------------
# Segment encoding (ORG-080 §4 "Canonical segment encoding + rejecting decoder")
# ---------------------------------------------------------------------------


def encode_segment(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise RunStateError("empty path segment")
    out = []
    for byte in value.encode("utf-8"):
        out.append(chr(byte) if byte in _UNRESERVED else f"%{byte:02x}")
    encoded = "".join(out)
    if len(encoded) > MAX_SEGMENT:
        raise RunStateError(f"segment {value!r} encodes to more than {MAX_SEGMENT} bytes")
    if encoded in {".", ".."}:
        # Not in ORG-080's list, but "." and ".." are unreserved and would be traversal.
        raise RunStateError("segment may not be '.' or '..'")
    return encoded


def decode_segment(encoded: str) -> str:
    if not encoded:
        raise RunStateError("empty path segment")
    if len(encoded) > MAX_SEGMENT:
        raise RunStateError("segment longer than 64 bytes")
    if encoded in {".", ".."}:
        raise RunStateError("segment may not be '.' or '..'")
    raw = bytearray()
    i = 0
    while i < len(encoded):
        ch = encoded[i]
        if ch == "%":
            pair = encoded[i + 1 : i + 3]
            if len(pair) != 2 or any(c not in _HEX_LOWER for c in pair):
                raise RunStateError(f"segment {encoded!r}: percent-escapes must be two lowercase hex digits")
            byte = int(pair, 16)
            if byte in _UNRESERVED:
                raise RunStateError(f"segment {encoded!r}: unreserved byte {chr(byte)!r} must not be percent-encoded")
            raw.append(byte)
            i += 3
            continue
        if ord(ch) > 127 or ord(ch) not in _UNRESERVED:
            raise RunStateError(f"segment {encoded!r}: {ch!r} must be percent-encoded")
        raw.append(ord(ch))
        i += 1
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RunStateError(f"segment {encoded!r} decodes to malformed UTF-8") from None


# ---------------------------------------------------------------------------
# ULID run ids (ORG-080 §4 "Canonical run_id")
# ---------------------------------------------------------------------------

_last_ulid: list[int] = [0, 0]  # (ms, random) for in-process monotonicity


def new_ulid(now_ms: int | None = None) -> str:
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    if ms == _last_ulid[0]:
        rand = _last_ulid[1] + 1
    else:
        rand = secrets.randbits(80)
    _last_ulid[0], _last_ulid[1] = ms, rand
    value = (ms << 80) | (rand & ((1 << 80) - 1))
    return "".join(_CROCKFORD[(value >> (5 * (25 - i))) & 31] for i in range(26))


def validate_ulid(value: Any, *, now_ms: int | None = None) -> str:
    if not isinstance(value, str) or len(value) != 26 or any(c not in _CROCKFORD for c in value):
        raise RunStateError(f"run_id {value!r} is not a canonical 26-char uppercase Crockford ULID")
    number = 0
    for c in value:
        number = number * 32 + _CROCKFORD.index(c)
    if number >> 128:
        raise RunStateError(f"run_id {value!r} overflows 128 bits")
    ms = number >> 80
    now = int(time.time() * 1000) if now_ms is None else now_ms
    if ms > now + 60_000:
        raise RunStateError(f"run_id {value!r} is from the future")
    return value


# ---------------------------------------------------------------------------
# Record shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunTuple:
    instance: str
    component: str
    scope: str
    leg: str
    run_id: str

    def to_json_obj(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in TUPLE_KEYS}

    def relpath(self) -> Path:
        validate_ulid(self.run_id)
        parts = [encode_segment(getattr(self, k)) for k in TUPLE_KEYS[:-1]]
        return Path("schema-v1", *parts, self.run_id)


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def validate_record(record: Mapping[str, Any], expected: RunTuple | None = None) -> None:
    if set(record) != RECORD_KEYS:
        raise RunStateError(f"record fields {sorted(set(record) ^ RECORD_KEYS)} differ from {SCHEMA}")
    if record["schema"] != SCHEMA:
        raise RunStateError(f"record schema {record['schema']!r} is not {SCHEMA}")
    tup = record["tuple"]
    if not isinstance(tup, dict) or set(tup) != set(TUPLE_KEYS):
        raise RunStateError("record tuple must be {instance, component, scope, leg, run_id}")
    if expected is not None and tup != expected.to_json_obj():
        raise RunStateError("record tuple disagrees with its path (tampering signal)")
    try:
        state = State(record["state"])
    except ValueError:
        raise RunStateError(f"record state {record['state']!r} is outside the closed enum") from None
    halted = record["halted"]
    if (state is State.HALTED) != (halted is not None):
        raise RunStateError("halted is non-null exactly when state is HALTED")
    if halted is not None and (not isinstance(halted, dict) or set(halted) != {"op_id", "reason", "at"}):
        raise RunStateError("halted must be {op_id, reason, at}")
    if halted is not None:
        op_ids = {op.get("op_id") for op in record["operations"]}
        if halted["op_id"] != "system-reboot" and halted["op_id"] not in op_ids:
            raise RunStateError("halted.op_id must name one of the run's ops or be 'system-reboot'")
    incomplete = record["rollback_incomplete"]
    if (state is State.ROLLBACK_INCOMPLETE) != (incomplete is not None):
        raise RunStateError("rollback_incomplete is non-null exactly when state is ROLLBACK_INCOMPLETE")
    if record["parking"] is not None:
        raise RunStateError("parking is deferred past T-5074; a parked record is refused")
    for key in ("operations", "receipts"):
        if not isinstance(record[key], list):
            raise RunStateError(f"{key} must be a list")
    if record["updated_at"] < record["created_at"]:
        raise RunStateError("updated_at precedes created_at")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def _rmdir_quietly(path: Path) -> None:
    """rmdir (never recursive) a run directory a failed create made; a non-empty one is left in place."""

    try:
        os.rmdir(path)
    except OSError:
        pass


def _ensure_private_dir(path: Path, uid: int) -> None:
    try:
        path.mkdir(mode=DIR_MODE)
        os.chmod(path, DIR_MODE)  # mkdir's mode is umask-filtered
    except FileExistsError:
        pass
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != uid or stat.S_IMODE(st.st_mode) != DIR_MODE:
        # ORG-080 §4 "File modes": a wrong owner or mode is tampering, not something to repair silently.
        raise RunStateError(f"{path} is not a mode-0700 directory owned by uid {uid}")


class RunHandle:
    """One run's directory, held by fd (the WAL's authority model)."""

    def __init__(self, store: "RunStore", tup: RunTuple, path: Path) -> None:
        self.store = store
        self.tuple = tup
        self.path = path
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.held = HeldDir.from_fd(fd, expected_uid=store.uid, expected_mode=DIR_MODE)
        except AuthorityError as exc:
            raise RunStateError(f"run directory {path}: {exc}") from None
        finally:
            os.close(fd)

    def close(self) -> None:
        self.held.close()

    def __enter__(self) -> "RunHandle":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def wal(self) -> WalStore:
        return WalStore.from_fd(self.held.fileno(), run_key=self.tuple.run_id, expected_uid=self.store.uid)

    def read_record(self) -> dict[str, Any]:
        try:
            data = self.held.read_regular("record.json", expected_uid=self.store.uid, expected_mode=FILE_MODE, max_bytes=1 << 20)
            record = load_canonical_json(data)  # the generic artifact cap the record is written under (unchanged by T-5809)
        except Exception as exc:  # noqa: BLE001 -- every unreadable record refuses, typed
            raise RunStateError(f"record unreadable at {self.path}: {exc}") from None
        validate_record(record, self.tuple)
        return record

    def _write(self, record: Mapping[str, Any]) -> None:
        validate_record(record, self.tuple)
        try:
            self.held.replace_regular("record.json", canonical_json_bytes(record), expected_uid=self.store.uid, mode=FILE_MODE)
        except AuthorityError as exc:
            raise RunStateError(f"record write refused at {self.path}: {exc}") from None

    def update(self, *, state: State | None = None, **fields: Any) -> dict[str, Any]:
        record = self.read_record()
        current = State(record["state"])
        if state is not None and state is not current and (current, state) not in _EDGES:
            raise RunStateError(f"illegal transition {current.value} -> {state.value} (C2 closed table)")
        for key in fields:
            if key not in RECORD_KEYS or key in {"schema", "tuple", "created_at"}:
                raise RunStateError(f"field {key!r} is not writable")
        record.update(fields)
        if state is not None:
            record["state"] = state.value
        stamp = now_iso()
        record["updated_at"] = max(stamp, record["updated_at"])
        self._write(record)
        return record

    def write_plan(self, data: bytes) -> None:
        if len(data) > MAX_PLAN_BYTES:
            raise RunStateError(f"plan is {len(data)} bytes, over MAX_PLAN_BYTES ({MAX_PLAN_BYTES})")
        self.held.write_immutable("plan.json", data, expected_uid=self.store.uid, mode=FILE_MODE)

    def read_plan(self) -> dict[str, Any]:
        data = self.held.read_regular("plan.json", expected_uid=self.store.uid, expected_mode=FILE_MODE, max_bytes=MAX_PLAN_BYTES)
        return load_canonical_json(data, max_bytes=MAX_PLAN_BYTES)

    def append_transcript(self, line: bytes) -> None:
        fd = os.open(
            "transcript.jsonl",
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            FILE_MODE,
            dir_fd=self.held.fileno(),
        )
        try:
            os.write(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)

    def read_transcript(self) -> list[bytes]:
        try:
            fd = os.open("transcript.jsonl", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.held.fileno())
        except FileNotFoundError:
            return []
        with os.fdopen(fd, "rb") as fh:
            return fh.read().splitlines()


class RunStore:
    """Run records under one scope root, owned by ``uid`` (mode 0700/0600)."""

    def __init__(self, scope_root: Path, *, uid: int | None = None) -> None:
        self.root = Path(scope_root)
        self.uid = os.getuid() if uid is None else uid

    # What create writes, by name, in this order; a failed create unlinks exactly these (T-5810),
    # in REVERSE order, so a cleanup that stops early never leaves a record without its plan.
    _CREATE_FILES = ("plan.json", "manifest.json", "record.json")

    def create(self, tup: RunTuple, *, operations: list[dict[str, Any]], plan: bytes) -> RunHandle:
        """Admit a run: plan.json, then the WAL manifest, then record.json (state PENDING).

        record.json is the admission (ORG-080 §4 admission step 2), so it is written
        LAST: a record never exists without the plan rollback reads and the manifest
        reconcile/rollback classify against (T-5810). A failure before the record
        lands unlinks only the files named in ``_CREATE_FILES`` and rmdirs the run
        directory, so no record-less run directory is left behind.
        """

        _ensure_private_dir(self.root, self.uid)
        current = self.root
        for part in tup.relpath().parts[:-1]:
            current = current / part
            _ensure_private_dir(current, self.uid)
        path = current / tup.run_id
        path.mkdir(mode=DIR_MODE)
        try:
            os.chmod(path, DIR_MODE)
            handle = RunHandle(self, tup, path)
        except BaseException:
            _rmdir_quietly(path)
            raise
        try:
            self._admit(handle, tup, operations, plan)
        except BaseException:
            for name in reversed(self._CREATE_FILES):
                try:
                    os.unlink(name, dir_fd=handle.held.fileno())
                except FileNotFoundError:
                    pass
                except OSError:
                    break  # leave the directory for the operator rather than mask the original failure
            handle.close()
            _rmdir_quietly(path)
            raise
        return handle

    @staticmethod
    def _admit(handle: RunHandle, tup: RunTuple, operations: list[dict[str, Any]], plan: bytes) -> None:
        stamp = now_iso()
        handle.write_plan(plan)
        with handle.wal() as wal:
            wal.initialize_manifest(created_at=stamp, updated_at=stamp)
        record = {
            "schema": SCHEMA,
            "tuple": tup.to_json_obj(),
            "state": INITIAL_STATE.value,
            "operations": operations,
            "receipts": [],
            "halted": None,
            "rollback_incomplete": None,
            "parking": None,
            "created_at": stamp,
            "updated_at": stamp,
        }
        handle._write(record)

    def _tuple_from_path(self, record_dir: Path) -> RunTuple:
        rel = record_dir.relative_to(self.root)
        parts = rel.parts
        if len(parts) != 6 or parts[0] != "schema-v1":
            raise RunStateError(f"{record_dir} is not a schema-v1 run path")
        decoded = [decode_segment(p) for p in parts[1:5]]
        return RunTuple(*decoded, validate_ulid(parts[5]))

    def open(self, tup: RunTuple) -> RunHandle:
        return RunHandle(self, tup, self.root / tup.relpath())

    def find(self, run_id: str) -> RunTuple | None:
        validate_ulid(run_id)
        hits = sorted((self.root / "schema-v1").glob(f"*/*/*/*/{run_id}"))
        if len(hits) > 1:
            raise RunStateError(f"run_id {run_id} appears under {len(hits)} tuples")
        return self._tuple_from_path(hits[0]) if hits else None

    def has_record(self, tup: RunTuple) -> bool:
        """Whether the run directory holds record.json (the admission); lstat, never followed."""

        try:
            os.lstat(self.root / tup.relpath() / "record.json")
        except FileNotFoundError:
            return False
        return True

    def _dir_tuples(self) -> list[RunTuple]:
        base = self.root / "schema-v1"
        if not base.is_dir():
            return []
        return sorted(
            (self._tuple_from_path(p) for p in base.glob("*/*/*/*/*") if p.is_dir()),
            key=lambda t: t.run_id,
        )

    def all_tuples(self) -> list[RunTuple]:
        """Admitted runs only: a run directory without record.json is never admitted (see ``orphan_tuples``)."""

        return [t for t in self._dir_tuples() if self.has_record(t)]

    def orphan_tuples(self) -> list[RunTuple]:
        """Run directories with no record.json: create died between mkdir and the record write (T-5810)."""

        return [t for t in self._dir_tuples() if not self.has_record(t)]
