"""Disposable, directory-rooted ORG-058 memory store with generation-scoped,
rebuildable projections (T-3198).

This is the storage half of the ORG-058 correction / supersession /
verified-forgetting slice (report ``2026-08-25-org-058-memory-lifecycle-plan``
§5/§8/§9). The operations that judge policy and write receipts live in
``memory_lifecycle_ops``; this module owns only the on-disk layout, the fold
that computes current lifecycle state, admission, and the projections. It is a
DISPOSABLE store for real rebuild paths (plan §13 step 3), not the memex and not
any live Exocortex store.

Layout (files are truth; projections are rebuildable)::

    <root>/
      store_manifest.json                      declared surfaces (init_store)
      versions/<memory_id>/<version_id>.json   immutable envelope, canonical bytes
      payloads/<memory_id>/<version_id>        derived payload text, per VERSION
      payloads/<memory_id>/<version_id>.reason optional correction note: 16-byte salt + text (purgeable)
      events.jsonl                             append-only lifecycle events
      tombstones.jsonl                         append-only non-retrievable tombstones
      receipts/<operation_id>.json             every op's receipt (write-once)
      ops/<operation_id>.request.json          the op's request digest (write-once; ids/digests only)
      ops/<operation_id>.journal.jsonl         source-request intent/sent records (append-only)
      projections/<name>/gen-<N>/...  + projections/<name>/CURRENT
      .lock                                    fcntl.flock for CAS critical sections

Invariants (structural, not "remember to"):

* ``locked()`` serializes processes with ``flock`` and threads sharing one
  ``MemoryStore`` object with a per-instance ``RLock``; re-entrancy depth is
  per thread, so no thread can enter on another thread's hold. An at-fork
  hook resets both in a forked child and closes the inherited flock fd, so a
  child never inherits (or deadlocks on) its parent's hold.
* A version file is written ONCE (``os.link`` of a temp file, which fails if the
  path exists) and never rewritten. Current state is a FOLD over
  ``events.jsonl``; ``current_envelope`` returns the stored version with the
  folded ``lifecycle_state``/``superseded_by`` substituted.
* ``append_event`` records ``from`` itself from the fold and refuses an illegal
  §5 transition, so the log cannot chain to a state the map forbids.
* A torn FINAL log line (a crash mid-append, no newline) is crash residue and
  VOID, even if it parses: readers ignore it, and the next append terminates
  it and starts its own line, naming the voided bytes (``torn_predecessor``:
  offset + length + digest) so every later read skips exactly them. Any other
  unparseable line is corruption and raises ``StoreIntegrityError``. It is
  repaired only EXPLICITLY and offline by ``memory_store_repair`` (T-4503),
  which APPENDS one ``repair_void`` record naming the corrupt bytes (offset +
  length + digest); readers then skip exactly those bytes. The log is never
  rewritten, and a ``repair_void`` whose digest does not match the bytes at
  its offset voids nothing and is itself a ``StoreIntegrityError``.
* Admission pins the lifecycle policy's IDENTITY (``policy_ref@revision`` +
  content digest) into the retained event. A revision string names a policy;
  only the pinned digest says which content it meant, and a successor must
  carry its prior's pin.
* Payloads are per-version and never content-addressed: deleting one can
  neither break nor retain another memory's content. Reads verify the bytes
  against the envelope's ``payload_digest``.
* The retrievable set is RETAINED_* + payload present and verified + source
  object not tombstoned for that memory. Projections are built ONLY from it.
* A source ``object_id`` is admitted only in canonical form, and tombstones
  are written and compared in canonical form, so an alias spelling of a
  tombstoned object cannot be re-ingested. A ``source_system`` is admitted
  only from a closed alphabet with no ``/``, so the ``system/object`` names
  receipts use are injective (internal keys are ``(system, object)`` tuples).
* A projection generation is published by ``os.replace`` of ``CURRENT`` only
  after its manifest (per-file sha256 + event-log offset) is complete; a build
  that raises leaves ``CURRENT`` on the prior complete generation (§9).
* The query path re-filters every hit against the retrievable set (defence in
  depth); ``raw_*`` lookups expose what a generation physically holds.

Admission refusals are typed ``LifecycleRefusal`` RETURN values, never
exceptions. ``StoreIntegrityError`` is raised only for a store whose own files
contradict each other (a corrupt log, a version file that names another id): a
fault in the substrate, not a policy denial.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import unicodedata
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)

from memory_envelope import (  # noqa: E402
    LifecycleState,
    MemoryEnvelope,
    SchemaError,
    TrustAdjudicationReceipt,
    TrustPolicy,
    canonical_json_bytes,
    envelope_from_json,
    envelope_to_json,
    is_legal_transition,
)
from memory_policy_evaluator import validate_envelope  # noqa: E402

STORE_MANIFEST = "store_manifest.json"
STORE_MANIFEST_SCHEMA = "org058-store-manifest/v1"
PROJECTION_MANIFEST_SCHEMA = "org058-projection-manifest/v1"
PROJECTIONS: tuple[str, ...] = ("content", "subject")
# A correction's optional free-text note is a purgeable per-version derived
# payload at payloads/<memory_id>/<version_id>.reason (never receipt text).
REASON_NOTE_SUFFIX = ".reason"
# The note payload is <16 random salt bytes> + <note utf-8>. Receipts carry
# sha256(salt + note): it checks integrity while the note exists, and once the
# note (and its salt) is purged it can no longer confirm a guessed text.
REASON_NOTE_SALT_BYTES = 16
RETRIEVABLE_STATES = frozenset(
    {LifecycleState.RETAINED_SOURCE_REF, LifecycleState.RETAINED_DERIVED}
)

# Ids become path components, so they are a closed, traversal-free alphabet.
# "+" is deliberately excluded: receipt retries use it as a separator.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_GEN_RE = re.compile(r"^gen-([0-9]+)$")
# A closed alphabet with no "/", so "system/object" names are injective.
_SOURCE_SYSTEM_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class LifecycleRefusal(str, Enum):
    """Closed refusal vocabulary for admission and the lifecycle ops (the ops
    module re-exports it). Defined here because the store is the lower layer."""

    STALE_VERSION = "stale-version"
    SOURCE_REWRITE_ATTEMPT = "source-rewrite-attempt"
    SOURCE_IDENTITY_CHANGE = "source-identity-change"
    POLICY_FIELD_CHANGE = "policy-field-change"
    OPERATION_ID_REUSE = "operation-id-reuse"
    ILLEGAL_TRANSITION = "illegal-transition"
    UNKNOWN_LIFECYCLE_POLICY = "unknown-lifecycle-policy"
    AUTHORITY_MISMATCH = "authority-mismatch"
    MODE_NOT_OWNED = "mode-not-owned"
    MODE_NOT_ALLOWED = "mode-not-allowed"
    RETENTION_NOT_ELAPSED = "retention-not-elapsed"
    TOMBSTONED = "tombstoned"
    VERSION_EXISTS = "version-exists"
    ENVELOPE_INVALID = "envelope-invalid"
    MALFORMED_INPUT = "malformed-input"
    WIDENING_UNAUTHORIZED = "widening-unauthorized"
    TRUST_POLICY_MISMATCH = "trust-policy-mismatch"


class StoreIntegrityError(RuntimeError):
    """The store's own files contradict each other (substrate fault)."""


# ---------------------------------------------------------------------------
# Lifecycle policy vocabulary. The ops module owns the policy semantics and
# re-exports these names; they are defined here because admission pins the
# resolved policy's identity into the event log, and the store is the lower
# layer (same reason as LifecycleRefusal).
# ---------------------------------------------------------------------------
class LifecycleMode(str, Enum):
    """Verbatim from report §8."""

    SUPERSEDE = "supersede"
    REMOVE_DERIVED = "remove-derived"
    REQUEST_SOURCE_CORRECTION = "request-source-correction"
    REQUEST_SOURCE_ERASURE = "request-source-erasure"
    EXPIRE = "expire"
    DESTROY_STORE = "destroy-store"


EXPIRE_PURGE_VALUES = frozenset({"now", "scheduled"})


def parse_zoned_time(value: object) -> _dt.datetime | None:
    """An ISO-8601 timestamp WITH a zone, else ``None`` (a naive time cannot be
    compared against a policy boundary without guessing a zone)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        t = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


@dataclass(frozen=True)
class ForgettingRule:
    required_authority: str
    allowed_modes: frozenset[LifecycleMode]
    tombstone_required: bool = False
    expire_purge: str = "now"
    purge_after: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.required_authority, str) or not self.required_authority:
            raise SchemaError("forgetting rule needs a required_authority")
        modes = frozenset(self.allowed_modes)
        if any(not isinstance(m, LifecycleMode) for m in modes):
            raise SchemaError("allowed_modes must be LifecycleMode members")
        object.__setattr__(self, "allowed_modes", modes)
        if not isinstance(self.tombstone_required, bool):
            raise SchemaError("tombstone_required must be a bool")
        if self.expire_purge not in EXPIRE_PURGE_VALUES:
            raise SchemaError(f"expire_purge must be one of {sorted(EXPIRE_PURGE_VALUES)}")
        if self.expire_purge == "scheduled" and not self.purge_after:
            raise SchemaError("a scheduled purge must state purge_after")
        if self.purge_after is not None and parse_zoned_time(self.purge_after) is None:
            raise SchemaError("purge_after must be an ISO-8601 timestamp with a zone")


@dataclass(frozen=True)
class LifecyclePolicy:
    """Named, immutable-revision lifecycle policy. ``correction_policies`` maps a
    correction-policy name to its required authority_ref;
    ``forgetting_policies`` maps a forgetting-policy name to its rule. Separate
    from ``TrustPolicy``: integrity labels never decide lifecycle authority."""

    policy_ref: str
    revision: str
    digest: str
    correction_policies: Mapping[str, str]
    forgetting_policies: Mapping[str, ForgettingRule]

    def __post_init__(self) -> None:
        for label in ("policy_ref", "revision", "digest"):
            value = getattr(self, label)
            if not isinstance(value, str) or not value:
                raise SchemaError(f"lifecycle policy {label} must be a non-empty string")
        cp = dict(self.correction_policies)
        for name, authority in cp.items():
            if not isinstance(name, str) or not isinstance(authority, str) or not authority:
                raise SchemaError("correction_policies maps name -> non-empty authority_ref")
        fp = dict(self.forgetting_policies)
        for name, rule in fp.items():
            if not isinstance(name, str) or not isinstance(rule, ForgettingRule):
                raise SchemaError("forgetting_policies maps name -> ForgettingRule")
        object.__setattr__(self, "correction_policies", MappingProxyType(cp))
        object.__setattr__(self, "forgetting_policies", MappingProxyType(fp))


def lifecycle_policy_digest(
    policy_ref: str,
    revision: str,
    correction_policies: Mapping[str, str],
    forgetting_policies: Mapping[str, ForgettingRule],
) -> str:
    """``sha256`` over the canonical bytes of the policy CONTENT."""
    body = {
        "policy_ref": policy_ref,
        "revision": revision,
        "correction_policies": dict(sorted(correction_policies.items())),
        "forgetting_policies": {
            name: {
                "required_authority": rule.required_authority,
                "allowed_modes": sorted(m.value for m in rule.allowed_modes),
                "tombstone_required": rule.tombstone_required,
                "expire_purge": rule.expire_purge,
                "purge_after": rule.purge_after,
            }
            for name, rule in sorted(forgetting_policies.items())
        },
    }
    return "sha256:" + hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def make_lifecycle_policy(
    policy_ref: str,
    revision: str,
    *,
    correction_policies: Mapping[str, str],
    forgetting_policies: Mapping[str, ForgettingRule],
) -> LifecyclePolicy:
    """Construct a policy whose ``digest`` is computed from its content."""
    return LifecyclePolicy(
        policy_ref,
        revision,
        lifecycle_policy_digest(policy_ref, revision, correction_policies, forgetting_policies),
        correction_policies,
        forgetting_policies,
    )


@dataclass(frozen=True)
class PolicyPin:
    """Which policy CONTENT a revision meant when a version was admitted:
    ``ref`` is ``policy_ref@revision``, ``digest`` the content digest. Every
    later op must present a policy with exactly this digest."""

    ref: str
    digest: str


def policy_pin(policy: LifecyclePolicy) -> PolicyPin:
    return PolicyPin(f"{policy.policy_ref}@{policy.revision}", policy.digest)


def lifecycle_policy_problem(policy: object, revision: object) -> str | None:
    """``None`` iff ``policy`` is a LifecyclePolicy whose own revision is
    ``revision`` and whose digest recomputes over its content. This proves the
    policy is self-consistent, NOT that it is the policy a memory was admitted
    under; that is the pin's job."""
    if not isinstance(policy, LifecyclePolicy):
        return "not a LifecyclePolicy"
    if not isinstance(revision, str) or not revision:
        return "no lifecycle policy_revision named"
    if policy.revision != revision:
        return (
            f"registry key {revision!r} != policy revision {policy.revision!r} "
            "(a moving alias is not an identity)"
        )
    expected = lifecycle_policy_digest(
        policy.policy_ref, policy.revision, policy.correction_policies, policy.forgetting_policies
    )
    if policy.digest != expected:
        return "lifecycle policy digest does not match its content"
    return None


@dataclass(frozen=True)
class AdmitResult:
    """Admission verdict. ``refusal is None`` means admitted; there is no
    separate success flag to drift out of step with it."""

    memory_id: str
    version_id: str
    refusal: LifecycleRefusal | None
    detail: str = ""
    version_digest: str | None = None


@dataclass(frozen=True)
class BackupSurface:
    name: str
    retention: str | None
    verifiable: bool


@dataclass(frozen=True)
class StoreManifest:
    """Declared residue surfaces. ``None`` means UNKNOWN (key missing or
    malformed), which is never the same as an explicit empty declaration."""

    backup_surfaces: tuple[BackupSurface, ...] | None
    media_scrubbed: bool | None


@dataclass(frozen=True)
class FoldedVersion:
    state: LifecycleState
    superseded_by: tuple[str, ...]
    payload_purged: bool
    policy_pin: PolicyPin
    admitted_by: str  # operation_id of the event that admitted this version


def is_safe_id(value: object) -> bool:
    """True iff ``value`` may be used as a single path component."""
    return isinstance(value, str) and bool(_ID_RE.match(value))


def is_valid_source_system(value: object) -> bool:
    return isinstance(value, str) and _SOURCE_SYSTEM_RE.fullmatch(value) is not None


def canonical_object_id(object_id: str) -> str:
    """The ``PurePosixPath`` spelling of a source object id: ``./``, a trailing
    ``/`` and doubled ``/`` dropped. Tombstones are written and checked in it."""
    return str(PurePosixPath(object_id))


def is_canonical_object_id(object_id: object) -> bool:
    """True iff ``object_id`` is already its own canonical spelling: relative,
    no empty, ``.`` or ``..`` segment, no trailing ``/``, Unicode NFC, no
    control character (``\n`` included) or backslash, no leading/trailing
    whitespace. Admission refuses any other spelling, so one source object has
    one id and an alias cannot slip past a tombstone written under the
    canonical form. Case and ``%``-encoding are NOT folded: whether they alias
    is source-specific."""
    if not isinstance(object_id, str) or not object_id:
        return False
    if unicodedata.normalize("NFC", object_id) != object_id or object_id != object_id.strip():
        return False
    if "\\" in object_id or any(unicodedata.category(ch) == "Cc" for ch in object_id):
        return False
    if any(part in ("", ".", "..") for part in object_id.split("/")):
        return False
    return canonical_object_id(object_id) == object_id


def payload_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def envelope_version_digest(env: MemoryEnvelope) -> str:
    """``sha256`` over the canonical bytes of the envelope AS STORED (unfolded).
    Stable across lifecycle events, so it is the compare-and-swap token."""
    return "sha256:" + hashlib.sha256(canonical_json_bytes(envelope_to_json(env))).hexdigest()


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_new(path: Path, data: bytes) -> None:
    """Write-once: a complete temp file is hard-linked into place. ``os.link``
    fails with ``FileExistsError`` if the path exists, so immutability is a
    property of the syscall, not of a prior existence check."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    _fsync_dir(path.parent)


def _write_replace(path: Path, data: bytes) -> None:
    """Atomic publish: a complete temp file is ``os.replace``d over ``path``,
    so a reader observes the old bytes or the new bytes, never a partial file."""
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    _fsync_dir(path.parent)


def _voided_line(record: dict, line_at: int, line: bytes) -> bool:
    """True iff ``record`` (the next line) voids exactly ``line``: the torn
    bytes at that offset, of that length and digest."""
    healed = record.get("torn_predecessor")
    return isinstance(healed, dict) and healed == {
        "offset": line_at,
        "length": len(line),
        "digest": "sha256:" + hashlib.sha256(line).hexdigest(),
    }


def _digest_of(line: bytes) -> str:
    return "sha256:" + hashlib.sha256(line).hexdigest()


def _repair_void_set(
    lines: Sequence[tuple[int, bytes]], parsed: Sequence[object], label: str
) -> frozenset[tuple[int, int, str]]:
    """``(offset, length, digest)`` of every line a ``repair_void`` record
    voids. A record counts only if it is exactly ``{"repair_void": {...}}``
    (plus the heal's ``torn_predecessor``); each named line must exist at that
    offset with that length and digest, EARLIER than the record, and be CORRUPT
    (unparseable, or JSON that is not an object): a void never hides a valid
    event, tombstone or void. Anything else is corruption, not a licence."""
    by_offset = {at: (line, rec) for (at, line), rec in zip(lines, parsed)}
    out: set[tuple[int, int, str]] = set()
    for i, ((at, _line), rec) in enumerate(zip(lines, parsed)):
        if not (isinstance(rec, dict) and "repair_void" in rec):
            continue
        body = rec["repair_void"]
        voids = body.get("voids") if isinstance(body, dict) else None
        if set(rec) - {"repair_void", "torn_predecessor"} or not isinstance(voids, list) or not voids:
            raise StoreIntegrityError(f"{label} line {i + 1} is a malformed repair_void")
        for v in voids:
            ok = (
                isinstance(v, dict)
                and set(v) == {"offset", "length", "digest"}
                and isinstance(v["offset"], int)
                and isinstance(v["length"], int)
                and isinstance(v["digest"], str)
                and not isinstance(v["offset"], bool)
                and not isinstance(v["length"], bool)
            )
            hit = by_offset.get(v["offset"]) if ok else None
            if (
                hit is None
                or v["offset"] >= at
                or len(hit[0]) != v["length"]
                or _digest_of(hit[0]) != v["digest"]
                # a void may only ever hide CORRUPT bytes (unparseable, or JSON
                # that is not an object): never a valid event/tombstone/void
                or not (isinstance(hit[1], ValueError) or (hit[1] is not None and not isinstance(hit[1], dict)))
            ):
                raise StoreIntegrityError(
                    f"{label} line {i + 1}: repair_void names bytes that do not match (voids nothing)"
                )
            out.add((v["offset"], v["length"], v["digest"]))
    return frozenset(out)


def _read_jsonl(path: Path, label: str) -> tuple[dict, ...]:
    """Records of an append-only log. A FINAL line with no terminating newline
    is a torn write (crash residue): it is VOID, whether or not it parses. The
    append that heals it names exactly those bytes (``torn_predecessor``:
    offset + length + digest), and every later read skips them, so a torn line
    never resurrects. A line named by a ``repair_void`` record (the explicit,
    offline repair, T-4503) is void the same way; ``repair_void`` records are
    never returned. Any other unparseable line is corruption and raises."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ()
    lines: list[tuple[int, bytes]] = []
    offset = 0
    for line in data.split(b"\n")[:-1]:  # the last segment is "" or a torn tail
        lines.append((offset, line))
        offset += len(line) + 1
    parsed: list[object] = []
    for n, (_at, line) in enumerate(lines, 1):
        try:
            parsed.append(json.loads(line) if line.strip() else None)
        except ValueError:
            parsed.append(ValueError(f"{label} line {n} is not JSON"))
    repaired = _repair_void_set(lines, parsed, label)
    out: list[dict] = []
    for i, ((at, line), record) in enumerate(zip(lines, parsed)):
        nxt = parsed[i + 1] if i + 1 < len(parsed) else None
        if isinstance(nxt, dict) and _voided_line(nxt, at, line):
            continue  # a healed torn line: void
        if repaired and (at, len(line), _digest_of(line)) in repaired:
            continue  # voided by an explicit repair
        if record is None:
            continue
        if isinstance(record, ValueError):
            raise StoreIntegrityError(f"{record} (and not a healed torn line)")
        if not isinstance(record, dict):
            raise StoreIntegrityError(f"{label} line {i + 1} is not an object")
        if "repair_void" in record:
            continue  # bookkeeping, not an event or tombstone
        out.append(record)
    return tuple(out)


def _torn_tail(path: Path) -> bool:
    """True iff the log ends in an unterminated (torn) line."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return False
    return bool(data) and not data.endswith(b"\n")


def _append_jsonl(path: Path, record: Mapping[str, object]) -> None:
    """Append one record on its OWN line. After a torn tail (a crash
    mid-append) the torn bytes are terminated first, never concatenated onto,
    and the record names them (``torn_predecessor``) so readers can tell
    healed crash residue from corruption. Nothing is ever rewritten."""
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        size = os.fstat(fd).st_size
        prefix = b""
        if size and os.pread(fd, 1, size - 1) != b"\n":
            data = os.pread(fd, size, 0)
            start = data.rfind(b"\n") + 1
            torn = data[start:]
            record = {
                **record,
                "torn_predecessor": {
                    "offset": start,
                    "length": len(torn),
                    "digest": "sha256:" + hashlib.sha256(torn).hexdigest(),
                },
            }
            prefix = b"\n"
        line = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        os.write(fd, prefix + line)
        os.fsync(fd)
    finally:
        os.close(fd)


def _gen_num(gen: str) -> int:
    m = _GEN_RE.match(gen)
    return int(m.group(1)) if m else -1


def _ro_connect(db: Path) -> sqlite3.Connection:
    # Read-only URI open: a lookup never creates or touches generation files.
    return sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)


def _fts_phrase(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# Projection builders. Each writes its data file(s) into a fresh generation
# directory from the retrievable rows ONLY and returns the file names that the
# generation manifest must digest.
# ---------------------------------------------------------------------------
_Row = tuple[str, str, str, tuple[str, ...]]  # memory_id, version_id, body, subjects


def _build_content(gen_dir: Path, rows: Sequence[_Row]) -> tuple[str, ...]:
    con = sqlite3.connect(gen_dir / "content.sqlite")
    try:
        con.execute(
            "CREATE VIRTUAL TABLE content USING "
            "fts5(memory_id UNINDEXED, version_id UNINDEXED, body)"
        )
        con.executemany(
            "INSERT INTO content(memory_id, version_id, body) VALUES (?, ?, ?)",
            [(m, v, body) for m, v, body, _subjects in rows],
        )
        con.commit()
    finally:
        con.close()
    return ("content.sqlite",)


def _build_subject(gen_dir: Path, rows: Sequence[_Row]) -> tuple[str, ...]:
    index: dict[str, list[list[str]]] = {}
    for m, v, _body, subjects in rows:
        for s in subjects:
            index.setdefault(s, []).append([m, v])
    for refs in index.values():
        refs.sort()
    _write_new(gen_dir / "subject.json", canonical_json_bytes(index))
    return ("subject.json",)


_BUILDERS: Mapping[str, Callable[[Path, Sequence[_Row]], tuple[str, ...]]] = {
    "content": _build_content,
    "subject": _build_subject,
}


def _no_fault(_step: str) -> None:
    return None


def init_store(
    root: Path | str,
    *,
    backup_surfaces: Sequence[Mapping[str, object] | BackupSurface],
    media_scrubbed: bool,
) -> "MemoryStore":
    """Create an empty store and write ``store_manifest.json``.

    ``backup_surfaces`` is a list of ``{name, retention, verifiable}`` records;
    an EXPLICIT empty list declares "none". ``media_scrubbed`` is the store
    operator's declaration that unlinked blocks on this medium are not
    recoverable; the lifecycle ops cite it and never claim a scrub themselves.
    Refuses (raises) on a malformed declaration or an existing manifest.
    """
    if not isinstance(media_scrubbed, bool):
        raise SchemaError("media_scrubbed must be a bool")
    if isinstance(backup_surfaces, (str, bytes)) or not isinstance(backup_surfaces, Sequence):
        raise SchemaError("backup_surfaces must be a list of records")
    declared: list[dict[str, object]] = []
    for b in backup_surfaces:
        if isinstance(b, BackupSurface):
            b = {"name": b.name, "retention": b.retention, "verifiable": b.verifiable}
        if not isinstance(b, Mapping):
            raise SchemaError("backup surface must be a record")
        name, retention, verifiable = b.get("name"), b.get("retention"), b.get("verifiable")
        if not isinstance(name, str) or not name:
            raise SchemaError("backup surface needs a name")
        if retention is not None and not isinstance(retention, str):
            raise SchemaError("backup retention must be a string or null")
        if not isinstance(verifiable, bool):
            raise SchemaError("backup verifiable must be a bool")
        declared.append({"name": name, "retention": retention, "verifiable": verifiable})
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for sub in ("versions", "payloads", "receipts", "projections"):
        (root / sub).mkdir(exist_ok=True)
    for name in ("events.jsonl", "tombstones.jsonl", ".lock"):
        (root / name).touch(exist_ok=True)
    manifest = {
        "schema": STORE_MANIFEST_SCHEMA,
        "backup_surfaces": declared,
        "media_scrubbed": media_scrubbed,
    }
    _write_new(root / STORE_MANIFEST, canonical_json_bytes(manifest))
    return MemoryStore(root)


_LIVE_STORES: "weakref.WeakSet[MemoryStore]" = weakref.WeakSet()


def _reset_stores_after_fork() -> None:
    for store in list(_LIVE_STORES):
        store._after_fork_in_child()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_stores_after_fork)


def fold_events(events: Sequence[Mapping[str, object]]) -> dict[tuple[str, str], FoldedVersion]:
    """The pure fold of an event-record list into per-version lifecycle state
    (what ``MemoryStore.folded`` computes over the log; also used by the
    repair tool to fold an independently-derived survivor list)."""
    state: dict[tuple[str, str], FoldedVersion] = {}
    try:
        for ev in events:
            for t in ev.get("transitions", []):
                key = (t["memory_id"], t["version_id"])
                prev = state.get(key)
                if t.get("from") != (prev.state.value if prev else None):
                    raise StoreIntegrityError(f"event {ev.get('seq')} does not chain for {key}")
                extra = tuple(t.get("superseded_by", ()))
                had = prev.superseded_by if prev else ()
                if prev is None:  # admission: the pin is fixed here, once
                    pin = t["lifecycle_policy"]
                    if not (isinstance(pin, dict) and set(pin) == {"ref", "digest"}):
                        raise StoreIntegrityError(f"event {ev.get('seq')} admits {key} without a policy pin")
                    pinned = PolicyPin(str(pin["ref"]), str(pin["digest"]))
                    admitted_by = str(ev.get("operation_id", ""))
                else:
                    pinned, admitted_by = prev.policy_pin, prev.admitted_by
                state[key] = FoldedVersion(
                    state=LifecycleState(t["to"]),
                    superseded_by=had + tuple(x for x in extra if x not in had),
                    payload_purged=prev.payload_purged if prev else False,
                    policy_pin=pinned,
                    admitted_by=admitted_by,
                )
            for p in ev.get("payload_purged", []):
                key = (p["memory_id"], p["version_id"])
                if key in state and p.get("file", p["version_id"]) == p["version_id"]:
                    state[key] = dataclasses.replace(state[key], payload_purged=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise StoreIntegrityError("events.jsonl record malformed") from exc
    return state


class MemoryStore:
    """One directory-rooted store. Construct as many objects on a root as you
    like (each process its own); ``locked()`` serializes them with ``flock``,
    and serializes threads sharing one object with a per-instance ``RLock``."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._thread_lock = threading.RLock()
        self._local = threading.local()
        self._held_fd: int | None = None  # the flock fd while this object holds it
        self._fork_gen = 0  # bumped in a forked child: a hold from before the fork is not ours
        _LIVE_STORES.add(self)

    def _after_fork_in_child(self) -> None:
        """Called in a forked child. A lock held at fork time belongs to the
        PARENT: a fresh RLock (the old one may be owned by a thread that does
        not exist here), a fresh per-thread depth (the forking thread's would
        say "already held"), and the inherited flock descriptor is closed (the
        flock lives on the open file description the parent still holds), so
        the child must take its own flock."""
        fd, self._held_fd = self._held_fd, None
        self._fork_gen += 1
        self._thread_lock = threading.RLock()
        self._local = threading.local()
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    # ------------------------------------------------------------------ lock
    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Exclusive ``fcntl.flock`` on ``.lock``, re-entrant within one THREAD.

        The ``RLock`` is held around the whole flock critical section, so a
        second thread using the same object blocks here instead of reading
        the holder's re-entrancy depth as its own (the depth is per thread)."""
        with self._thread_lock:
            depth = getattr(self._local, "depth", 0)
            if depth:
                self._local.depth = depth + 1
                try:
                    yield
                finally:
                    self._local.depth = depth
                return
            gen = self._fork_gen
            fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                self._held_fd = fd
                self._local.depth = 1
                try:
                    yield
                finally:
                    # In a forked child the at-fork hook already dropped this
                    # hold (and closed the fd): unlocking the parent's shared
                    # open file description from here would free the PARENT.
                    if self._fork_gen == gen:
                        self._local.depth = 0
                        self._held_fd = None
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                if self._fork_gen == gen:
                    os.close(fd)

    # -------------------------------------------------------------- manifest
    def read_manifest(self) -> StoreManifest | None:
        """The declared surfaces, or ``None`` when the manifest is missing or
        unparseable. A missing/malformed key is UNKNOWN (``None`` field)."""
        try:
            raw = json.loads((self.root / STORE_MANIFEST).read_bytes())
        except (OSError, ValueError):
            return None
        if not isinstance(raw, Mapping):
            return None
        backups: tuple[BackupSurface, ...] | None = None
        declared = raw.get("backup_surfaces")
        if isinstance(declared, list):
            parsed: list[BackupSurface] = []
            for b in declared:
                if not (
                    isinstance(b, Mapping)
                    and isinstance(b.get("name"), str)
                    and b.get("name")
                    and (b.get("retention") is None or isinstance(b.get("retention"), str))
                    and isinstance(b.get("verifiable"), bool)
                ):
                    parsed = []
                    declared = None
                    break
                parsed.append(BackupSurface(b["name"], b.get("retention"), b["verifiable"]))
            if declared is not None:
                backups = tuple(parsed)
        scrubbed = raw.get("media_scrubbed")
        return StoreManifest(backups, scrubbed if isinstance(scrubbed, bool) else None)

    # ------------------------------------------------------------- listings
    def _version_path(self, memory_id: str, version_id: str) -> Path:
        return self.root / "versions" / memory_id / f"{version_id}.json"

    def _payload_dir(self, memory_id: str) -> Path:
        return self.root / "payloads" / memory_id

    def memory_ids(self) -> tuple[str, ...]:
        ids: set[str] = set()
        for sub in ("versions", "payloads"):
            d = self.root / sub
            if d.is_dir():
                ids.update(p.name for p in d.iterdir() if p.is_dir() and is_safe_id(p.name))
        return tuple(sorted(ids))

    def version_ids(self, memory_id: str) -> tuple[str, ...]:
        d = self.root / "versions" / memory_id
        if not is_safe_id(memory_id) or not d.is_dir():
            return ()
        vids = [
            p.name[: -len(".json")]
            for p in d.iterdir()
            if p.is_file() and p.name.endswith(".json") and is_safe_id(p.name[: -len(".json")])
        ]
        return tuple(sorted(vids))

    def payload_files(self, memory_id: str) -> tuple[str, ...]:
        """EVERY entry under ``payloads/<memory_id>/``, including crash-left temp
        files: what a purge must cover is enumerated, never assumed."""
        d = self._payload_dir(memory_id)
        if not is_safe_id(memory_id) or not d.is_dir():
            return ()
        return tuple(sorted(p.name for p in d.iterdir()))

    # -------------------------------------------------------------- versions
    def stored_envelope(self, memory_id: str, version_id: str) -> MemoryEnvelope | None:
        """The envelope exactly as retained (unfolded), or ``None`` if absent."""
        if not (is_safe_id(memory_id) and is_safe_id(version_id)):
            return None
        try:
            raw = json.loads(self._version_path(memory_id, version_id).read_bytes())
            env = envelope_from_json(raw)
        except FileNotFoundError:
            return None
        except (ValueError, SchemaError) as exc:
            raise StoreIntegrityError(f"version file {memory_id}/{version_id} unreadable") from exc
        if env.memory_id != memory_id or env.version_id != version_id:
            raise StoreIntegrityError(f"version file {memory_id}/{version_id} names another id")
        return env

    def version_digest(self, memory_id: str, version_id: str) -> str | None:
        env = self.stored_envelope(memory_id, version_id)
        return None if env is None else envelope_version_digest(env)

    # ---------------------------------------------------------------- events
    def events(self) -> tuple[dict, ...]:
        return _read_jsonl(self.root / "events.jsonl", "events.jsonl")

    def event_log_torn_tail(self) -> bool:
        """True iff events.jsonl ends in a torn (unterminated) line."""
        return _torn_tail(self.root / "events.jsonl")

    def event_log_offset(self) -> int:
        try:
            return (self.root / "events.jsonl").stat().st_size
        except FileNotFoundError:
            return 0

    def folded(self) -> dict[tuple[str, str], FoldedVersion]:
        """Current lifecycle state of every ADMITTED version: a fold over the
        event log. A version file with no event was never admitted and is
        absent here (so it is never retrievable)."""
        return fold_events(self.events())

    def append_event(
        self,
        kind: str,
        *,
        operation_id: str,
        now: str,
        transitions: Sequence[Mapping[str, object]] = (),
        payload_purged: Sequence[Mapping[str, str]] = (),
        mode: str | None = None,
    ) -> dict:
        """Append ONE event. ``from`` is filled from the fold, and every
        transition must be legal under the §5 map (admission is
        ``None -> RETAINED_*`` and must carry the ``lifecycle_policy``
        ``PolicyPin``); an illegal one raises before anything is written, so
        the log can never record it. ``mode`` records the lifecycle mode that
        caused the event (``None`` for admission)."""
        with self.locked():
            fold = self.folded()
            recorded: list[dict[str, object]] = []
            for t in transitions:
                mid, vid = str(t["memory_id"]), str(t["version_id"])
                to = LifecycleState(t["to"])
                prev = fold.get((mid, vid))
                rec: dict[str, object] = {
                    "memory_id": mid,
                    "version_id": vid,
                    "from": prev.state.value if prev else None,
                    "to": to.value,
                }
                if prev is None:
                    if to not in RETRIEVABLE_STATES:
                        raise StoreIntegrityError(f"{mid}/{vid}: first event must retain, not {to.value}")
                    pin = t.get("lifecycle_policy")
                    if not isinstance(pin, PolicyPin):
                        raise StoreIntegrityError(f"{mid}/{vid}: admission must pin its lifecycle policy")
                    rec["lifecycle_policy"] = {"ref": pin.ref, "digest": pin.digest}
                elif not is_legal_transition(prev.state, to):
                    raise StoreIntegrityError(f"{mid}/{vid}: {prev.state.value} -> {to.value} is illegal")
                if t.get("superseded_by"):
                    rec["superseded_by"] = [str(x) for x in t["superseded_by"]]  # type: ignore[union-attr]
                recorded.append(rec)
            event = {
                "seq": len(self.events()) + 1,
                "kind": kind,
                "mode": mode,
                "operation_id": operation_id,
                "at": now,
                "transitions": recorded,
                "payload_purged": [
                    {
                        "memory_id": str(p["memory_id"]),
                        "version_id": str(p["version_id"]),
                        "file": str(p.get("file", p["version_id"])),
                    }
                    for p in payload_purged
                ],
            }
            _append_jsonl(self.root / "events.jsonl", event)
            return event

    def policy_pin(self, memory_id: str, version_id: str) -> PolicyPin | None:
        """The lifecycle policy pinned when this version was admitted."""
        fv = self.folded().get((memory_id, version_id))
        return None if fv is None else fv.policy_pin

    def current_envelope(self, memory_id: str, version_id: str) -> MemoryEnvelope | None:
        """The stored version with its FOLDED state; ``None`` if not admitted."""
        fv = self.folded().get((memory_id, version_id))
        if fv is None:
            return None
        stored = self.stored_envelope(memory_id, version_id)
        if stored is None:
            raise StoreIntegrityError(f"event log names {memory_id}/{version_id} with no version file")
        return dataclasses.replace(
            stored, lifecycle_state=fv.state, superseded_by=fv.superseded_by
        )

    # -------------------------------------------------------------- payloads
    def reason_note_digest(self, memory_id: str, version_id: str, note: str) -> str | None:
        """``sha256(salt + note)`` of the stored note payload, iff it holds
        exactly ``note``; ``None`` when absent or different."""
        path = self._payload_dir(memory_id) / f"{version_id}{REASON_NOTE_SUFFIX}"
        if not (is_safe_id(memory_id) and is_safe_id(version_id)):
            return None
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        if data[REASON_NOTE_SALT_BYTES:] != note.encode("utf-8"):
            return None
        return "sha256:" + hashlib.sha256(data).hexdigest()

    def payload_file_version(self, name: str) -> str:
        """The version a ``payloads/<memory_id>/`` entry belongs to (the name
        itself for a payload, the name minus ``.reason`` for a reason note)."""
        return name[: -len(REASON_NOTE_SUFFIX)] if name.endswith(REASON_NOTE_SUFFIX) else name

    def read_payload(self, memory_id: str, version_id: str) -> str | None:
        """Payload text verified against the stored ``payload_digest``; ``None``
        if absent. A digest mismatch raises (the bytes are not the retained
        evidence)."""
        if not (is_safe_id(memory_id) and is_safe_id(version_id)):
            return None
        path = self._payload_dir(memory_id) / version_id
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        env = self.stored_envelope(memory_id, version_id)
        if env is None:
            raise StoreIntegrityError(f"payload {memory_id}/{version_id} has no version to verify against")
        if "sha256:" + hashlib.sha256(data).hexdigest() != env.payload_digest:
            raise StoreIntegrityError(f"payload {memory_id}/{version_id} does not match payload_digest")
        return data.decode("utf-8")

    def purge_payload_file(self, memory_id: str, name: str) -> bool:
        """Unlink one entry under ``payloads/<memory_id>/``; True if it existed."""
        d = self._payload_dir(memory_id)
        path = d / name
        if not is_safe_id(memory_id) or "/" in name or name in ("", ".", ".."):
            return False
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                return False
        _fsync_dir(d)
        if not any(d.iterdir()):
            d.rmdir()
            _fsync_dir(d.parent)
        return True

    # ------------------------------------------------------------ tombstones
    def tombstones(self) -> tuple[dict, ...]:
        return _read_jsonl(self.root / "tombstones.jsonl", "tombstones.jsonl")

    def _tombstone_keys(self) -> set[tuple[object, str, str]]:
        """``(memory_id, source_system, canonical object_id)`` of every tombstone."""
        return {
            (t.get("memory_id"), str(t.get("source_system")), canonical_object_id(str(t.get("object_id"))))
            for t in self.tombstones()
        }

    def is_tombstoned(
        self, source_system: str, object_id: str, memory_id: str | None = None
    ) -> bool:
        """Compared in canonical form, whatever spelling either side used."""
        want = (source_system, canonical_object_id(object_id))
        return any(
            (s, o) == want and (memory_id is None or m == memory_id) for m, s, o in self._tombstone_keys()
        )

    def add_tombstone(
        self,
        *,
        memory_id: str,
        source_system: str,
        object_id: str,
        operation_id: str,
        now: str,
        policy_revision: str,
        forgetting_policy: str,
    ) -> dict:
        """Append a non-retrievable tombstone: ids only, never payload text. The
        object id is recorded in canonical form."""
        record = {
            "memory_id": memory_id,
            "source_system": source_system,
            "object_id": canonical_object_id(object_id),
            "operation_id": operation_id,
            "at": now,
            "policy_revision": policy_revision,
            "forgetting_policy": forgetting_policy,
        }
        with self.locked():
            _append_jsonl(self.root / "tombstones.jsonl", record)
        return record

    # ---------------------------------------------------------- retrievable
    def retrievable_envelopes(self) -> tuple[MemoryEnvelope, ...]:
        """Folded envelopes that may be retrieved: RETAINED_*, payload present
        and digest-verified, source object not tombstoned for that memory."""
        fold = self.folded()
        tombs = self._tombstone_keys()
        out: list[MemoryEnvelope] = []
        for key in sorted(fold):
            fv = fold[key]
            if fv.state not in RETRIEVABLE_STATES:
                continue
            mid, vid = key
            stored = self.stored_envelope(mid, vid)
            if stored is None:
                raise StoreIntegrityError(f"event log names {mid}/{vid} with no version file")
            try:
                if self.read_payload(mid, vid) is None:
                    continue
            except StoreIntegrityError:
                continue  # unverifiable bytes are never served
            src = stored.canonical_source_ref
            if (mid, src.source_system, canonical_object_id(src.object_id)) in tombs:
                continue
            out.append(
                dataclasses.replace(stored, lifecycle_state=fv.state, superseded_by=fv.superseded_by)
            )
        return tuple(out)

    def retrievable_version_ids(self, memory_id: str) -> tuple[str, ...]:
        return tuple(e.version_id for e in self.retrievable_envelopes() if e.memory_id == memory_id)

    # ------------------------------------------------------------- admission
    def append_version(
        self,
        env: object,
        payload: object,
        *,
        lifecycle_policy: LifecyclePolicy,
        trust_policy: TrustPolicy | None,
        trust_receipts: Mapping[str, TrustAdjudicationReceipt]
        | Sequence[TrustAdjudicationReceipt]
        | None = None,
        now: str = "",
        operation_id: str = "",
        supersedes_version: str | None = None,
        reason_note: str | None = None,
    ) -> AdmitResult:
        """Admit an immutable version (seeding, or a successor via ``correct``).

        ``lifecycle_policy`` is the resolved policy for ``env.policy_revision``;
        its identity (``PolicyPin``) is recorded in the retained event, and
        every later op must present a policy with exactly that digest.
        ``reason_note`` (a correction's free text) is written as the purgeable
        derived payload ``<version_id>.reason`` beside the version's payload.

        Refuses (typed, no mutation) if the lifecycle policy is not a
        self-consistent policy for ``env.policy_revision`` (or, for a
        successor, is not the prior's pinned policy), the envelope fails
        ``validate_envelope``, is not a RETAINED_* retention, arrives already
        superseded, carries a payload that does not match its digest, the
        version is already admitted, the memory already has admitted versions
        and this is not a supersession, a successor cites another source object
        than its prior (``source-identity-change``), or its source object is
        tombstoned (re-ingestion block). Otherwise writes the payload, then the version
        file, then ONE event (for a successor that event also closes
        ``supersedes_version`` as SUPERSEDED). A version file with no event (a
        crash before the event) is completed when the retry's bytes are
        identical, and refused otherwise.
        """
        mid = str(getattr(env, "memory_id", ""))
        vid = str(getattr(env, "version_id", ""))

        def refuse(code: LifecycleRefusal, detail: str) -> AdmitResult:
            return AdmitResult(mid, vid, code, detail)

        if not isinstance(env, MemoryEnvelope) or not isinstance(payload, str):
            return refuse(LifecycleRefusal.MALFORMED_INPUT, "need a MemoryEnvelope and a str payload")
        if not isinstance(now, str) or not isinstance(operation_id, str):
            return refuse(LifecycleRefusal.MALFORMED_INPUT, "now and operation_id must be strings")
        if trust_receipts is not None and not (
            (isinstance(trust_receipts, Mapping) and all(isinstance(v, TrustAdjudicationReceipt) for v in trust_receipts.values()))
            or (
                isinstance(trust_receipts, Sequence)
                and not isinstance(trust_receipts, (str, bytes))
                and all(isinstance(v, TrustAdjudicationReceipt) for v in trust_receipts)
            )
        ):
            return refuse(LifecycleRefusal.MALFORMED_INPUT, "trust_receipts must be a Mapping/Sequence of receipts or None")
        if not (is_safe_id(mid) and is_safe_id(vid)):
            return refuse(LifecycleRefusal.MALFORMED_INPUT, "memory_id/version_id not a safe id")
        if vid.endswith(REASON_NOTE_SUFFIX):
            return refuse(LifecycleRefusal.MALFORMED_INPUT, f"version_id may not end with {REASON_NOTE_SUFFIX!r}")
        if reason_note is not None and (not isinstance(reason_note, str) or not reason_note):
            return refuse(LifecycleRefusal.MALFORMED_INPUT, "reason_note must be a non-empty string or None")
        if not is_valid_source_system(env.canonical_source_ref.source_system):
            return refuse(
                LifecycleRefusal.MALFORMED_INPUT,
                "canonical_source_ref.source_system must fullmatch [a-z0-9][a-z0-9._-]{0,63}",
            )
        if not is_canonical_object_id(env.canonical_source_ref.object_id):
            return refuse(
                LifecycleRefusal.MALFORMED_INPUT,
                "canonical_source_ref.object_id is not in canonical form (relative; no './', '..', "
                "empty segment or trailing '/')",
            )
        problem = lifecycle_policy_problem(lifecycle_policy, env.policy_revision)
        if problem is not None:
            return refuse(LifecycleRefusal.UNKNOWN_LIFECYCLE_POLICY, problem)
        pin = policy_pin(lifecycle_policy)
        decision = validate_envelope(env, trust_policy, receipts=trust_receipts)
        if not decision.allowed:
            return refuse(
                LifecycleRefusal.ENVELOPE_INVALID,
                f"{decision.reason_code.value}: {decision.detail}",
            )
        if env.lifecycle_state not in RETRIEVABLE_STATES:
            return refuse(
                LifecycleRefusal.ILLEGAL_TRANSITION,
                f"admission retains into RETAINED_*, not {env.lifecycle_state.value}",
            )
        if env.superseded_by:
            return refuse(LifecycleRefusal.MALFORMED_INPUT, "a new version cannot arrive superseded")
        if payload_digest(payload) != env.payload_digest:
            return refuse(LifecycleRefusal.ENVELOPE_INVALID, "payload does not match payload_digest")
        with self.locked():
            fold = self.folded()
            version_path = self._version_path(mid, vid)
            version_bytes = canonical_json_bytes(envelope_to_json(env))
            orphan = version_path.exists()
            if orphan:
                if (mid, vid) in fold:
                    return refuse(LifecycleRefusal.VERSION_EXISTS, "version already stored; versions are immutable")
                # A version file with no event is crash residue of an admission
                # that never reached its event: the SAME admission (identical
                # bytes) is completed, anything else refused.
                if version_path.read_bytes() != version_bytes:
                    return refuse(
                        LifecycleRefusal.VERSION_EXISTS,
                        "a different, never-admitted version file (crash residue) occupies this version",
                    )
            transitions: list[dict[str, object]] = []
            if supersedes_version is None:
                if any((mid, v) in fold for v in self.version_ids(mid)):
                    return refuse(
                        LifecycleRefusal.VERSION_EXISTS,
                        "memory already has versions; a successor is admitted only by supersession",
                    )
            else:
                prior = fold.get((mid, supersedes_version))
                if prior is None:
                    return refuse(LifecycleRefusal.STALE_VERSION, "superseded version was never admitted")
                if supersedes_version not in env.supersedes:
                    return refuse(LifecycleRefusal.MALFORMED_INPUT, "successor.supersedes must name the prior")
                if not is_legal_transition(prior.state, LifecycleState.SUPERSEDED):
                    return refuse(
                        LifecycleRefusal.ILLEGAL_TRANSITION,
                        f"{prior.state.value} -> SUPERSEDED is illegal",
                    )
                prior_env = self.stored_envelope(mid, supersedes_version)
                prior_src = prior_env.canonical_source_ref if prior_env is not None else None
                if prior_src is None or (prior_src.source_system, prior_src.object_id) != (
                    env.canonical_source_ref.source_system,
                    env.canonical_source_ref.object_id,
                ):
                    # The source OBJECT is fixed across supersession; the #16
                    # tombstone scoping below relies on it.
                    return refuse(
                        LifecycleRefusal.SOURCE_IDENTITY_CHANGE,
                        "a successor must cite its prior's (source_system, object_id); re-homing is not a correction",
                    )
                if prior.policy_pin != pin:
                    return refuse(
                        LifecycleRefusal.UNKNOWN_LIFECYCLE_POLICY,
                        f"a successor is admitted under the prior's pinned policy {prior.policy_pin.ref} "
                        f"({prior.policy_pin.digest}), not {pin.ref} ({pin.digest})",
                    )
                transitions.append(
                    {
                        "memory_id": mid,
                        "version_id": supersedes_version,
                        "to": LifecycleState.SUPERSEDED.value,
                        "superseded_by": [vid],
                    }
                )
            # Re-ingestion block: a NEW memory is blocked by any tombstone on its
            # source object (global); a successor only by a tombstone on its OWN
            # memory, so forgetting one memory never freezes another live one.
            src = env.canonical_source_ref
            scope = None if supersedes_version is None else mid
            if self.is_tombstoned(src.source_system, src.object_id, memory_id=scope):
                return refuse(
                    LifecycleRefusal.TOMBSTONED,
                    f"source {src.source_system}/{src.object_id} is tombstoned (re-ingestion block)",
                )
            # Crash-left derived files from an admission that never reached its
            # event: identical bytes are completed, anything else refused.
            # A retried note keeps its first salt: only the text is compared.
            derived = [(self._payload_dir(mid) / vid, payload, 0)]
            if reason_note is not None:
                derived.append(
                    (self._payload_dir(mid) / f"{vid}{REASON_NOTE_SUFFIX}", reason_note, REASON_NOTE_SALT_BYTES)
                )
            for path, text, salt in derived:
                if path.exists() and path.read_bytes()[salt:] != text.encode("utf-8"):
                    return refuse(LifecycleRefusal.VERSION_EXISTS, f"a different {path.name} already occupies this version")
            for path, text, salt in derived:
                if not path.exists():
                    _write_new(path, os.urandom(salt) + text.encode("utf-8"))
            if not orphan:
                _write_new(version_path, version_bytes)
            transitions.append(
                {"memory_id": mid, "version_id": vid, "to": env.lifecycle_state.value, "lifecycle_policy": pin}
            )
            self.append_event(
                "supersede" if supersedes_version is not None else "retain",
                operation_id=operation_id or f"admit:{mid}/{vid}",
                now=now,
                transitions=transitions,
            )
            return AdmitResult(mid, vid, None, "admitted", self.version_digest(mid, vid))

    # ------------------------------------------------------------- receipts
    def write_receipt(self, operation_id: str, record: Mapping[str, object]) -> Path:
        """Write-once receipt. The first attempt of an operation lands at
        ``receipts/<operation_id>.json``; a re-run never overwrites it and lands
        at ``receipts/<operation_id>+attempt-<n>.json`` (append-only, P4)."""
        if not is_safe_id(operation_id):
            raise ValueError(f"operation_id {operation_id!r} is not a safe id")
        d = self.root / "receipts"
        d.mkdir(exist_ok=True)
        body = canonical_json_bytes(record)
        with self.locked():
            path = d / f"{operation_id}.json"
            n = 1
            while path.exists():
                n += 1
                path = d / f"{operation_id}+attempt-{n}.json"
            _write_new(path, body)
        return path

    # ------------------------------------------------------------ op journal
    def op_request(self, operation_id: str) -> dict | None:
        """The op's bound request record, or ``None`` if it never mutated."""
        if not is_safe_id(operation_id):
            return None
        try:
            raw = json.loads((self.root / "ops" / f"{operation_id}.request.json").read_bytes())
        except FileNotFoundError:
            return None
        except ValueError as exc:
            raise StoreIntegrityError(f"ops/{operation_id}.request.json unreadable") from exc
        if not isinstance(raw, dict):
            raise StoreIntegrityError(f"ops/{operation_id}.request.json is not an object")
        return raw

    def bind_op_request(self, operation_id: str, record: Mapping[str, object]) -> None:
        """Write the op's request record ONCE, atomically (a complete, fsynced
        temp file linked into place; the directory is fsynced)."""
        if not is_safe_id(operation_id):
            raise ValueError(f"operation_id {operation_id!r} is not a safe id")
        with self.locked():
            path = self.root / "ops" / f"{operation_id}.request.json"
            if not path.exists():
                _write_new(path, canonical_json_bytes(record))

    def op_journal(self, operation_id: str) -> tuple[dict, ...]:
        if not is_safe_id(operation_id):
            return ()
        return _read_jsonl(self.root / "ops" / f"{operation_id}.journal.jsonl", f"ops/{operation_id}.journal.jsonl")

    def append_op_journal(self, operation_id: str, record: Mapping[str, object]) -> None:
        if not is_safe_id(operation_id):
            raise ValueError(f"operation_id {operation_id!r} is not a safe id")
        with self.locked():
            (self.root / "ops").mkdir(exist_ok=True)
            _append_jsonl(self.root / "ops" / f"{operation_id}.journal.jsonl", record)

    def receipt_paths(self, operation_id: str) -> tuple[Path, ...]:
        """Every receipt written for ``operation_id``, oldest first."""
        d = self.root / "receipts"
        first = d / f"{operation_id}.json"
        if not is_safe_id(operation_id) or not first.exists():
            return ()
        retries = sorted(
            (p for p in d.glob(f"{operation_id}+attempt-*.json")),
            key=lambda p: int(p.name[len(operation_id) + len("+attempt-") : -len(".json")]),
        )
        return (first, *retries)

    # ----------------------------------------------------------- projections
    def _proj_dir(self, name: str) -> Path:
        if name not in _BUILDERS:
            raise ValueError(f"unknown projection {name!r}")
        return self.root / "projections" / name

    def current_generation(self, name: str) -> str | None:
        try:
            text = (self._proj_dir(name) / "CURRENT").read_text().strip()
        except FileNotFoundError:
            return None
        if not _GEN_RE.match(text):
            raise StoreIntegrityError(f"projections/{name}/CURRENT is not a generation name")
        return text

    def generations_on_disk(self, name: str) -> tuple[str, ...]:
        """EVERY generation directory present, complete or not."""
        d = self._proj_dir(name)
        if not d.is_dir():
            return ()
        gens = [p.name for p in d.iterdir() if p.is_dir() and _GEN_RE.match(p.name)]
        return tuple(sorted(gens, key=_gen_num))

    def generation_manifest(self, name: str, gen: str) -> dict | None:
        try:
            raw = json.loads((self._proj_dir(name) / gen / "manifest.json").read_bytes())
        except (OSError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None

    def verify_generation(self, name: str, gen: str) -> bool:
        """True iff the generation's manifest exists, names this projection and
        generation, and every listed file matches its sha256."""
        m = self.generation_manifest(name, gen)
        if m is None or m.get("projection") != name or m.get("generation") != gen:
            return False
        files = m.get("files")
        if not isinstance(files, dict) or not files:
            return False
        gen_dir = self._proj_dir(name) / gen
        for fname, digest in files.items():
            path = gen_dir / str(fname)
            if not path.is_file() or _sha256_file(path) != digest:
                return False
        return True

    def complete_generations(self, name: str) -> tuple[str, ...]:
        return tuple(g for g in self.generations_on_disk(name) if self.verify_generation(name, g))

    def _publish(self, name: str, gen: str) -> None:
        _write_replace(self._proj_dir(name) / "CURRENT", f"{gen}\n".encode("utf-8"))

    def rebuild_projections(
        self, *, _fault: Callable[[str], None] | None = None
    ) -> dict[str, tuple[str | None, str]]:
        """Build a NEW generation of every projection from the retrievable set,
        write each manifest last, then swap every ``CURRENT``. A build that
        raises removes its partial generations and re-raises with ``CURRENT``
        untouched. Keeps at most one prior complete generation (for rollback).
        Returns ``{name: (generation_before, generation_after)}``."""
        fault = _fault or _no_fault
        with self.locked():
            for name in PROJECTIONS:
                cur = self.current_generation(name)
                for g in self.generations_on_disk(name):
                    if g != cur and not self.verify_generation(name, g):
                        shutil.rmtree(self._proj_dir(name) / g)  # crash leftovers never linger
            rows: list[_Row] = []
            for env in self.retrievable_envelopes():
                body = self.read_payload(env.memory_id, env.version_id)
                if body is not None:
                    rows.append((env.memory_id, env.version_id, body, tuple(env.subjects)))
            offset, count = self.event_log_offset(), len(self.events())
            built: dict[str, str] = {}
            try:
                for name in PROJECTIONS:
                    d = self._proj_dir(name)
                    d.mkdir(parents=True, exist_ok=True)
                    nums = [_gen_num(g) for g in self.generations_on_disk(name)]
                    cur = self.current_generation(name)
                    if cur:
                        nums.append(_gen_num(cur))
                    gen = f"gen-{max(nums, default=0) + 1}"
                    gen_dir = d / gen
                    gen_dir.mkdir()
                    built[name] = gen
                    files = _BUILDERS[name](gen_dir, rows)
                    fault(f"build:{name}:before-manifest")
                    manifest = {
                        "schema": PROJECTION_MANIFEST_SCHEMA,
                        "projection": name,
                        "generation": gen,
                        "files": {f: _sha256_file(gen_dir / f) for f in files},
                        "event_log_offset": offset,
                        "event_count": count,
                        "entries": sorted([m, v] for m, v, _b, _s in rows),
                    }
                    _write_new(gen_dir / "manifest.json", canonical_json_bytes(manifest))
                fault("publish:before-swap")
            except BaseException:
                for name, gen in built.items():
                    shutil.rmtree(self._proj_dir(name) / gen, ignore_errors=True)
                raise
            result: dict[str, tuple[str | None, str]] = {}
            try:
                for name in PROJECTIONS:
                    before = self.current_generation(name)
                    self._publish(name, built[name])
                    result[name] = (before, built[name])
            except BaseException:
                # An unpublished generation must never become a rollback target.
                for name, gen in built.items():
                    if self.current_generation(name) != gen:
                        shutil.rmtree(self._proj_dir(name) / gen, ignore_errors=True)
                raise
            for name, (before, after) in result.items():
                for g in self.generations_on_disk(name):
                    if g not in (after, before):
                        shutil.rmtree(self._proj_dir(name) / g)
            return result

    def rollback_projections(self) -> dict[str, str | None]:
        """Make the prior complete generation current. Returns the generation
        now current per projection, or ``None`` where no prior one exists."""
        out: dict[str, str | None] = {}
        with self.locked():
            for name in PROJECTIONS:
                cur = self.current_generation(name)
                prior = [g for g in self.complete_generations(name) if g != cur]
                if not prior:
                    out[name] = None
                    continue
                self._publish(name, prior[-1])
                out[name] = prior[-1]
        return out

    def delete_noncurrent_generations(self) -> dict[str, tuple[str, ...]]:
        """Remove every generation directory that is not ``CURRENT``."""
        out: dict[str, tuple[str, ...]] = {}
        with self.locked():
            for name in PROJECTIONS:
                cur = self.current_generation(name)
                gone: list[str] = []
                for g in self.generations_on_disk(name):
                    if g != cur:
                        shutil.rmtree(self._proj_dir(name) / g)
                        gone.append(g)
                out[name] = tuple(gone)
        return out

    # --------------------------------------------------------------- lookups
    def raw_refs(self, name: str, generation: str) -> frozenset[tuple[str, str]]:
        """Every ``(memory_id, version_id)`` a generation PHYSICALLY holds."""
        gen_dir = self._proj_dir(name) / generation
        if name == "content":
            db = gen_dir / "content.sqlite"
            if not db.exists():
                return frozenset()
            con = _ro_connect(db)
            try:
                rows = con.execute("SELECT memory_id, version_id FROM content").fetchall()
            finally:
                con.close()
            return frozenset((str(m), str(v)) for m, v in rows)
        path = gen_dir / "subject.json"
        if not path.exists():
            return frozenset()
        index = json.loads(path.read_bytes())
        return frozenset((m, v) for refs in index.values() for m, v in refs)

    def raw_lookup_content(
        self, term: str, *, generation: str | None = None
    ) -> tuple[tuple[str, str], ...]:
        """UNFILTERED full-text hits in one generation (default ``CURRENT``)."""
        gen = generation or self.current_generation("content")
        if gen is None:
            return ()
        db = self._proj_dir("content") / gen / "content.sqlite"
        if not db.exists():
            return ()
        con = _ro_connect(db)
        try:
            rows = con.execute(
                "SELECT memory_id, version_id FROM content WHERE content MATCH ?",
                (_fts_phrase(term),),
            ).fetchall()
        finally:
            con.close()
        return tuple(sorted({(str(m), str(v)) for m, v in rows}))

    def raw_lookup_subject(
        self, subject: str, *, generation: str | None = None
    ) -> tuple[tuple[str, str], ...]:
        """UNFILTERED subject-index hits in one generation (default ``CURRENT``)."""
        gen = generation or self.current_generation("subject")
        if gen is None:
            return ()
        path = self._proj_dir("subject") / gen / "subject.json"
        if not path.exists():
            return ()
        index = json.loads(path.read_bytes())
        return tuple(sorted({(m, v) for m, v in index.get(subject, [])}))

    def _filtered(self, name: str, hits: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
        gen = self.current_generation(name)
        if gen is not None and not self.verify_generation(name, gen):
            raise StoreIntegrityError(f"projections/{name}/{gen} fails its manifest")
        live = {(e.memory_id, e.version_id) for e in self.retrievable_envelopes()}
        return tuple(h for h in hits if h in live)

    def lookup_content(self, term: str) -> tuple[tuple[str, str], ...]:
        """CURRENT-generation hits re-filtered against the retrievable set."""
        return self._filtered("content", self.raw_lookup_content(term))

    def lookup_subject(self, subject: str) -> tuple[tuple[str, str], ...]:
        """CURRENT-generation hits re-filtered against the retrievable set."""
        return self._filtered("subject", self.raw_lookup_subject(subject))


__all__ = [
    "STORE_MANIFEST",
    "STORE_MANIFEST_SCHEMA",
    "PROJECTION_MANIFEST_SCHEMA",
    "PROJECTIONS",
    "REASON_NOTE_SUFFIX",
    "RETRIEVABLE_STATES",
    "LifecycleRefusal",
    "StoreIntegrityError",
    "LifecycleMode",
    "EXPIRE_PURGE_VALUES",
    "parse_zoned_time",
    "ForgettingRule",
    "LifecyclePolicy",
    "lifecycle_policy_digest",
    "make_lifecycle_policy",
    "PolicyPin",
    "policy_pin",
    "lifecycle_policy_problem",
    "AdmitResult",
    "BackupSurface",
    "StoreManifest",
    "FoldedVersion",
    "is_safe_id",
    "is_valid_source_system",
    "canonical_object_id",
    "is_canonical_object_id",
    "payload_digest",
    "envelope_version_digest",
    "init_store",
    "MemoryStore",
]
