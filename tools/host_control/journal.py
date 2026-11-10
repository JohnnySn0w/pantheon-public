"""Durable, fail-closed SQLite journal for broker plan and operation truth."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from tools.host_control.contracts import (
    COMPENSATION_APPROVAL_SENTINEL,
    COMPENSATION_KEY_PREFIX,
    OPERATION_TRANSITIONS,
    TERMINAL_OPERATION_STATES,
    HostControlError,
    IdempotencyConflict,
    InvalidTransition,
    JournalBusy,
    ManifestRecord,
    MigrationRequired,
    OperationReceipt,
    OperationRecord,
    PlanEnvelope,
    RecoveryRefused,
    RollbackIntent,
    SeatRecord,
    SubjectContext,
    SubjectMismatch,
    UnknownHandle,
    canonical_json,
    compute_effect_digest,
)


# Default write-lock wait. Injectable through BrokerJournal.open so a test
# can force lock contention without depending on host load.
BUSY_TIMEOUT_MS = 5000

APPLICATION_ID = 1346913090
USER_VERSION_V1 = 1
USER_VERSION = 2
SCHEMA_DIGEST_V1 = "82b1b18c874607a62119778fd92d7310ac75936c08e56fedd356fdc3fa8ee2fa"
DDL_V1 = """CREATE TABLE journal_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    schema_digest TEXT NOT NULL CHECK (
        length(schema_digest) = 64
        AND schema_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE plans (
    plan_handle TEXT PRIMARY KEY
        CHECK (
            length(plan_handle) = 64
            AND plan_handle NOT GLOB '*[^0-9a-f]*'
        ),
    plan_digest TEXT NOT NULL UNIQUE CHECK (
        length(plan_digest) = 64
        AND plan_digest NOT GLOB '*[^0-9a-f]*'
    ),
    effect_digest TEXT NOT NULL CHECK (
        length(effect_digest) = 64
        AND effect_digest NOT GLOB '*[^0-9a-f]*'
    ),
    subject_uid INTEGER NOT NULL CHECK (subject_uid >= 0),
    subject_seat_id TEXT NOT NULL CHECK (length(subject_seat_id) BETWEEN 1 AND 128),
    action TEXT NOT NULL CHECK (action = 'plan'),
    target_seat_id TEXT NOT NULL CHECK (length(target_seat_id) BETWEEN 1 AND 128),
    manifest_revision TEXT NOT NULL CHECK (length(manifest_revision) BETWEEN 1 AND 128),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
    request_digest TEXT NOT NULL CHECK (
        length(request_digest) = 64
        AND request_digest NOT GLOB '*[^0-9a-f]*'
    ),
    plan_json TEXT NOT NULL CHECK (length(plan_json) BETWEEN 2 AND 1048576),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE (
        subject_uid,
        subject_seat_id,
        action,
        target_seat_id,
        idempotency_key
    )
) STRICT;

CREATE INDEX plans_effect_digest_idx ON plans(effect_digest);
CREATE INDEX plans_subject_expiry_idx
    ON plans(subject_uid, subject_seat_id, expires_at);
"""

_OPERATIONS_DDL = """CREATE TABLE operations (
    operation_handle TEXT PRIMARY KEY
        CHECK (
            length(operation_handle) = 64
            AND operation_handle NOT GLOB '*[^0-9a-f]*'
        ),
    operation_kind TEXT NOT NULL CHECK (
        operation_kind IN ('apply', 'rollback', 'verify')
    ),
    state TEXT NOT NULL CHECK (
        state IN (
            'prepared', 'authorized', 'dispatch_intent', 'dispatched',
            'completed', 'failed', 'refused', 'manual_recovery'
        )
    ),
    subject_uid INTEGER NOT NULL CHECK (subject_uid >= 0),
    subject_seat_id TEXT NOT NULL CHECK (length(subject_seat_id) BETWEEN 1 AND 128),
    target_seat_id TEXT NOT NULL CHECK (length(target_seat_id) BETWEEN 1 AND 128),
    plan_handle TEXT NOT NULL REFERENCES plans(plan_handle) CHECK (
        length(plan_handle) = 64
        AND plan_handle NOT GLOB '*[^0-9a-f]*'
    ),
    plan_digest TEXT NOT NULL CHECK (
        length(plan_digest) = 64
        AND plan_digest NOT GLOB '*[^0-9a-f]*'
    ),
    receipt_handle TEXT CHECK (
        receipt_handle IS NULL
        OR (
            length(receipt_handle) = 64
            AND receipt_handle NOT GLOB '*[^0-9a-f]*'
        )
    ),
    receipt_digest TEXT CHECK (
        receipt_digest IS NULL
        OR (
            length(receipt_digest) = 64
            AND receipt_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    approval_handle TEXT NOT NULL CHECK (length(approval_handle) BETWEEN 1 AND 128),
    authority_record_id TEXT CHECK (
        authority_record_id IS NULL
        OR length(authority_record_id) BETWEEN 1 AND 128
    ),
    idempotency_key TEXT NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
    request_digest TEXT NOT NULL CHECK (
        length(request_digest) = 64
        AND request_digest NOT GLOB '*[^0-9a-f]*'
    ),
    operation_json TEXT NOT NULL CHECK (length(operation_json) BETWEEN 2 AND 1048576),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (
        subject_uid,
        subject_seat_id,
        operation_kind,
        target_seat_id,
        idempotency_key
    )
) STRICT;

CREATE INDEX operations_state_idx ON operations(state);
CREATE INDEX operations_plan_handle_idx ON operations(plan_handle);

CREATE TABLE operation_events (
    operation_handle TEXT NOT NULL REFERENCES operations(operation_handle),
    seq INTEGER NOT NULL CHECK (seq >= 1),
    from_state TEXT NOT NULL CHECK (
        from_state IN (
            'none', 'prepared', 'authorized', 'dispatch_intent', 'dispatched',
            'completed', 'failed', 'refused', 'manual_recovery'
        )
    ),
    to_state TEXT NOT NULL CHECK (
        to_state IN (
            'prepared', 'authorized', 'dispatch_intent', 'dispatched',
            'completed', 'failed', 'refused', 'manual_recovery'
        )
    ),
    event_kind TEXT NOT NULL CHECK (length(event_kind) BETWEEN 1 AND 64),
    payload_json TEXT NOT NULL CHECK (length(payload_json) BETWEEN 2 AND 1048576),
    created_at TEXT NOT NULL,
    PRIMARY KEY (operation_handle, seq)
) STRICT;

CREATE TABLE operation_receipts (
    receipt_handle TEXT PRIMARY KEY
        CHECK (
            length(receipt_handle) = 64
            AND receipt_handle NOT GLOB '*[^0-9a-f]*'
        ),
    operation_handle TEXT NOT NULL REFERENCES operations(operation_handle),
    phase TEXT NOT NULL CHECK (phase IN ('staged', 'complete', 'failed')),
    receipt_digest TEXT NOT NULL CHECK (
        length(receipt_digest) = 64
        AND receipt_digest NOT GLOB '*[^0-9a-f]*'
    ),
    receipt_json TEXT NOT NULL CHECK (length(receipt_json) BETWEEN 2 AND 1048576),
    created_at TEXT NOT NULL,
    UNIQUE (operation_handle, phase)
) STRICT;

CREATE TABLE rollback_intents (
    operation_handle TEXT NOT NULL REFERENCES operations(operation_handle),
    seq INTEGER NOT NULL CHECK (seq >= 1),
    intent_json TEXT NOT NULL CHECK (length(intent_json) BETWEEN 2 AND 1048576),
    resolved INTEGER NOT NULL CHECK (resolved IN (0, 1)),
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    PRIMARY KEY (operation_handle, seq)
) STRICT;

CREATE INDEX rollback_intents_unresolved_idx
    ON rollback_intents(resolved, operation_handle);
"""

# DDL (v2) embeds the v1 plans table and indexes VERBATIM by construction:
# the object-level validator compares sqlite_master SQL text byte-wise, so a
# migrated journal only validates if the carried-over v1 objects are
# byte-identical. Only journal_meta's schema_version CHECK changes.
DDL = (
    DDL_V1.replace("(schema_version = 1)", "(schema_version = 2)")
    + "\n"
    + _OPERATIONS_DDL
)
SCHEMA_DIGEST = "2ca96891ac59d0a519752985f3f7827f919d104bfd7ec8ff59bb58bc7278e4f1"

_SELECT_PLAN = (
    "SELECT plan_handle, plan_digest, effect_digest, subject_uid, "
    "subject_seat_id, action, target_seat_id, manifest_revision, "
    "idempotency_key, request_digest, plan_json, created_at, expires_at "
    "FROM plans"
)

_SELECT_OPERATION = (
    "SELECT operation_handle, operation_kind, state, subject_uid, "
    "subject_seat_id, target_seat_id, plan_handle, plan_digest, "
    "receipt_handle, receipt_digest, approval_handle, authority_record_id, "
    "idempotency_key, request_digest, operation_json, created_at, updated_at "
    "FROM operations"
)

_SELECT_RECEIPT = (
    "SELECT receipt_handle, operation_handle, phase, receipt_digest, "
    "receipt_json, created_at FROM operation_receipts"
)


def _schema_objects(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    )


def _expected_schema_objects(ddl: str) -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(ddl)
        return _schema_objects(connection)
    finally:
        connection.close()


_EXPECTED_OBJECTS_V1 = _expected_schema_objects(DDL_V1)
_EXPECTED_OBJECTS = _expected_schema_objects(DDL)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_dir(directory: Path) -> None:
    descriptor = os.open(
        directory,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_cutover(source: Path, target: Path) -> None:
    """Atomic rename plus directory fsync as ONE primitive.

    This is deliberately the only rename site in this module: os.replace is
    atomic within the directory's in-memory state but says nothing about the
    durability of the directory entry itself. Without the paired fsync a
    power-loss can resurrect the old inode under the target name or leave the
    new file unlinked. Forward migration and backup-restore both go through
    this function so neither arm can silently lack the durability of the
    other.
    """

    os.replace(source, target)
    _fsync_dir(target.parent)


def _sidecar_paths(path: Path) -> tuple[Path, Path]:
    return Path(f"{path}-wal"), Path(f"{path}-shm")


def _sidecars_present(path: Path) -> bool:
    wal, shm = _sidecar_paths(path)
    return wal.exists() or shm.exists()


# sqlite primary result codes for lock contention (sqlite3.h).
_SQLITE_BUSY = 5
_SQLITE_LOCKED = 6
_LOCK_CONTENTION_MESSAGES = frozenset(
    ("database is locked", "database table is locked")
)


def _is_lock_contention(exc: sqlite3.OperationalError) -> bool:
    """True when sqlite reports lock contention (SQLITE_BUSY / LOCKED).

    The primary result code (low byte of ``sqlite_errorcode``, present on
    Python 3.11+; extended codes such as BUSY_RECOVERY reduce to it)
    decides. Only when the interpreter attached no code does the exact
    message text stand in. Every other OperationalError is not contention.
    Duplicated in the sibling journal on purpose (L3: no import edge across
    the authority boundary); a parity test drives both copies.
    """

    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in (_SQLITE_BUSY, _SQLITE_LOCKED)
    return str(exc) in _LOCK_CONTENTION_MESSAGES


# T-2917 5d (ruling T-2917:D14): the events written BEFORE an Execute call.
PRE_EXECUTE_MARKERS = ("dispatch_intent", "spec_rearmed", "execute_retry", "redispatch_intent")
# T-2917 5d.2 fixup (ruling T-2917:D27): every Execute ISSUED for a mutating
# operation is preceded by exactly one of these journaled events (the first
# dispatch, an identical/definitive-refusal retry, a reconciler re-dispatch).
# Their lifetime count is the number of Executes ever issued.
EXECUTE_ISSUANCE_MARKERS = ("dispatch_intent", "execute_retry", "redispatch_intent")
# T-2917 5d.2 follow-up (ruling T-2917:D29): an Execute the executor answered
# with a DEFINITIVE ``Busy`` created no execution for that (handle, digest),
# so the broker journals ``execute_voided`` against that issuance's marker
# and the lifetime count excludes it. Closed refusal set: ``Denied`` is NOT
# here (it fires while a same-handle execution runs, so it proves nothing).
EXECUTE_VOIDED_EVENT = "execute_voided"
VOIDING_REFUSALS = frozenset(("Busy",))
# T-2917 5d.2 (ruling T-2917:D17): journaled spec re-arms per operation, and
# the closed set of reasons a re-arm may carry. Event kinds are free text in
# the v2 DDL, so neither adds a schema change (SCHEMA_DIGEST is pinned).
MAX_SPEC_REARMS = 2
REARM_REASONS = frozenset(("reconcile-probe-clean", "rollback-redispatch"))


class BrokerJournal:
    def __init__(
        self,
        path: Path,
        connection: sqlite3.Connection,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
    ) -> None:
        self.path = path
        self._connection = connection
        self._busy_timeout_ms = busy_timeout_ms
        self._closed = False

    def _begin_immediate(
        self,
        connection: sqlite3.Connection,
        operation: str,
    ) -> None:
        """Take the write lock up front; map lock contention here.

        Every runtime write transaction opens through this one seam. A
        lock still held by another connection past the busy timeout
        refuses with ``JournalBusy`` (a HostControlError) naming the
        operation that lost, instead of leaking the raw sqlite error; the
        transaction never began, so nothing was written. Any other
        OperationalError surfaces unchanged.
        """

        try:
            connection.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            if not _is_lock_contention(exc):
                raise
            raise JournalBusy(
                f"{operation}: journal write lock busy after "
                f"{self._busy_timeout_ms} ms (lock contention); "
                "nothing was written; retry later"
            ) from exc

    @staticmethod
    def _check_compiled_digests() -> None:
        if hashlib.sha256(DDL_V1.encode("utf-8")).hexdigest() != SCHEMA_DIGEST_V1:
            raise HostControlError("compiled v1 journal schema digest mismatch")
        if hashlib.sha256(DDL.encode("utf-8")).hexdigest() != SCHEMA_DIGEST:
            raise HostControlError("compiled journal schema digest mismatch")

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
    ) -> "BrokerJournal":
        cls._check_compiled_digests()
        if (
            not isinstance(busy_timeout_ms, int)
            or isinstance(busy_timeout_ms, bool)
            or busy_timeout_ms < 0
        ):
            raise HostControlError("invalid journal busy timeout")
        if not path.is_absolute():
            path = path.resolve()
        parent = path.parent
        if not parent.is_dir():
            raise HostControlError("journal parent directory is absent")
        try:
            connection = sqlite3.connect(
                path,
                timeout=busy_timeout_ms / 1000,
                isolation_level=None,
            )
            connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            connection.execute("PRAGMA foreign_keys=ON")
            objects = _schema_objects(connection)
            application_id = connection.execute(
                "PRAGMA application_id"
            ).fetchone()[0]
            user_version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            created = False
            if not objects and application_id == 0 and user_version == 0:
                cls._initialize(connection)
                created = True
            elif not objects or application_id == 0 or user_version == 0:
                raise HostControlError("refusing nonempty or partial v0 journal")
            elif (
                application_id == APPLICATION_ID
                and user_version == USER_VERSION_V1
            ):
                cls._validate_schema_v1(connection)
                raise MigrationRequired(
                    "journal is a valid v1 database; run "
                    "BrokerJournal.migrate_v1_to_v2 before serving"
                )
            cls._validate_schema(connection)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            if created:
                _fsync_dir(parent)
            return cls(path, connection, busy_timeout_ms)
        except (OSError, sqlite3.Error) as exc:
            try:
                connection.close()
            except (NameError, sqlite3.Error):
                pass
            raise HostControlError("cannot open broker journal") from exc
        except BaseException:
            try:
                connection.close()
            except (NameError, sqlite3.Error):
                pass
            raise

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        created_at = datetime.now(timezone.utc).isoformat()
        script = (
            "BEGIN EXCLUSIVE;\n"
            f"{DDL}"
            f"PRAGMA application_id={APPLICATION_ID};\n"
            f"PRAGMA user_version={USER_VERSION};\n"
            "INSERT INTO journal_meta "
            "(singleton, schema_version, schema_digest, created_at) VALUES "
            f"(1, {USER_VERSION}, '{SCHEMA_DIGEST}', '{created_at}');\n"
            "COMMIT;\n"
        )
        try:
            connection.executescript(script)
        except sqlite3.Error:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    @staticmethod
    def _validate_exact(
        connection: sqlite3.Connection,
        *,
        user_version: int,
        schema_digest: str,
        expected_objects: tuple[tuple[object, ...], ...],
    ) -> None:
        if (
            connection.execute("PRAGMA application_id").fetchone()[0]
            != APPLICATION_ID
            or connection.execute("PRAGMA user_version").fetchone()[0]
            != user_version
        ):
            raise HostControlError("journal version marker mismatch")
        if _schema_objects(connection) != expected_objects:
            raise HostControlError("journal schema object mismatch")
        rows = connection.execute(
            "SELECT singleton, schema_version, schema_digest FROM journal_meta"
        ).fetchall()
        if rows != [(1, user_version, schema_digest)]:
            raise HostControlError("journal metadata mismatch")

    @classmethod
    def _validate_schema(cls, connection: sqlite3.Connection) -> None:
        cls._validate_exact(
            connection,
            user_version=USER_VERSION,
            schema_digest=SCHEMA_DIGEST,
            expected_objects=_EXPECTED_OBJECTS,
        )

    @classmethod
    def _validate_schema_v1(cls, connection: sqlite3.Connection) -> None:
        cls._validate_exact(
            connection,
            user_version=USER_VERSION_V1,
            schema_digest=SCHEMA_DIGEST_V1,
            expected_objects=_EXPECTED_OBJECTS_V1,
        )

    # ------------------------------------------------------------------
    # v1 -> v2 migration (explicit, never inside open())
    # ------------------------------------------------------------------

    @classmethod
    def migrate_v1_to_v2(cls, path: Path) -> None:
        """Migrate a valid v1 journal to v2 by copy and atomic cutover.

        Elements: backup, copy/transform, verification, atomic cutover,
        directory sync, rollback. Fail-closed invariant: at any interruption
        the serving path is either a valid v1 journal (rerun converges) or a
        valid v2 journal; a half-migrated file never occupies the serving
        path because the only mutation of it is one atomic rename of a
        fully-verified file. A valid v2 source is an idempotent no-op.
        """

        cls._check_compiled_digests()
        if not path.is_absolute():
            path = path.resolve()
        backup = Path(f"{path}.v1-backup")
        tmp = Path(f"{path}.migrate-tmp")
        classification = cls._classify_for_migration(path)
        if classification == "v2":
            return
        try:
            rows = cls._migrate_backup(path, backup)
        except (OSError, sqlite3.Error) as exc:
            raise HostControlError("journal migration backup failed") from exc
        if _sidecars_present(path):
            raise HostControlError(
                "journal sidecar files persist after clean close; refusing"
            )
        try:
            cls._migrate_build_tmp(tmp, rows)
            cls._migrate_verify_tmp(tmp, rows)
            cls._migrate_verify_source_unchanged(path)
            if _sidecars_present(path):
                raise HostControlError(
                    "journal sidecar files appeared during migration; refusing"
                )
            _atomic_cutover(tmp, path)
        except HostControlError:
            cls._remove_tmp(tmp)
            raise
        except (OSError, sqlite3.Error) as exc:
            cls._remove_tmp(tmp)
            raise HostControlError("journal migration failed") from exc
        try:
            journal = cls.open(path)
            journal.close()
        except BaseException as exc:
            cls._restore_backup(backup, path)
            raise HostControlError(
                "post-cutover validation failed; v1 restored from backup"
            ) from exc

    @classmethod
    def _classify_for_migration(cls, path: Path) -> str:
        if not path.is_file():
            raise HostControlError("journal to migrate does not exist")
        connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        try:
            user_version = connection.execute(
                "PRAGMA user_version"
            ).fetchone()[0]
            if user_version == USER_VERSION:
                cls._validate_schema(connection)
                return "v2"
            cls._validate_schema_v1(connection)
            return "v1"
        except sqlite3.Error as exc:
            raise HostControlError(
                "cannot classify journal for migration"
            ) from exc
        finally:
            connection.close()

    @classmethod
    def _migrate_backup(
        cls,
        path: Path,
        backup: Path,
    ) -> tuple[tuple[object, ...], ...]:
        if backup.exists():
            os.remove(backup)
        source = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        try:
            cls._validate_schema_v1(source)
            rows = tuple(
                tuple(row) for row in source.execute(_SELECT_PLAN).fetchall()
            )
            destination = sqlite3.connect(backup)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        _fsync_file(backup)
        _fsync_dir(path.parent)
        return rows

    @classmethod
    def _migrate_build_tmp(
        cls,
        tmp: Path,
        rows: tuple[tuple[object, ...], ...],
    ) -> None:
        for stale in (tmp, *_sidecar_paths(tmp)):
            if stale.exists():
                os.remove(stale)
        connection = sqlite3.connect(tmp, timeout=5.0, isolation_level=None)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            cls._initialize(connection)
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            connection.execute("COMMIT")
        finally:
            connection.close()
        _fsync_file(tmp)
        _fsync_dir(tmp.parent)

    @classmethod
    def _migrate_verify_tmp(
        cls,
        tmp: Path,
        rows: tuple[tuple[object, ...], ...],
    ) -> None:
        connection = sqlite3.connect(tmp, timeout=5.0, isolation_level=None)
        try:
            cls._validate_schema(connection)
            stored = connection.execute(_SELECT_PLAN).fetchall()
            if len(stored) != len(rows):
                raise HostControlError("migrated plan row count mismatch")
            for row in stored:
                cls._decode_row(tuple(row))
        finally:
            connection.close()

    @classmethod
    def _migrate_verify_source_unchanged(cls, path: Path) -> None:
        connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
        try:
            cls._validate_schema_v1(connection)
        finally:
            connection.close()

    @staticmethod
    def _remove_tmp(tmp: Path) -> None:
        for stale in (tmp, *_sidecar_paths(tmp)):
            try:
                if stale.exists():
                    os.remove(stale)
            except OSError:
                pass

    @classmethod
    def _restore_backup(cls, backup: Path, path: Path) -> None:
        restore_tmp = Path(f"{path}.restore-tmp")
        if restore_tmp.exists():
            os.remove(restore_tmp)
        shutil.copyfile(backup, restore_tmp)
        _fsync_file(restore_tmp)
        _atomic_cutover(restore_tmp, path)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connection.close()

    def _require_open(self) -> sqlite3.Connection:
        if self._closed:
            raise HostControlError("journal is closed")
        return self._connection

    # ------------------------------------------------------------------
    # plans (unchanged v1 behavior)
    # ------------------------------------------------------------------

    def put_plan_or_replay(
        self,
        *,
        subject: SubjectContext,
        target_seat_id: str,
        manifest_revision: str,
        idempotency_key: str,
        request_digest: str,
        plan: PlanEnvelope,
    ) -> tuple[PlanEnvelope, bool]:
        connection = self._require_open()
        if not plan.validate_digest():
            raise HostControlError("invalid plan envelope digest")
        if (
            plan.owner != subject
            or plan.target_seat_id != target_seat_id
            or plan.manifest_revision != manifest_revision
        ):
            raise HostControlError("journal plan identity mismatch")
        plan_json = canonical_json(plan.to_dict())
        try:
            self._begin_immediate(connection, "put_plan_or_replay")
            row = connection.execute(
                f"{_SELECT_PLAN} WHERE subject_uid=? AND subject_seat_id=? "
                "AND action='plan' AND target_seat_id=? AND idempotency_key=?",
                (
                    subject.uid,
                    subject.seat_id,
                    target_seat_id,
                    idempotency_key,
                ),
            ).fetchone()
            if row is not None:
                if row[9] != request_digest:
                    raise IdempotencyConflict("idempotency request conflict")
                stored = self._decode_row(row)
                connection.execute("COMMIT")
                return stored, True
            connection.execute(
                "INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan.plan_handle,
                    plan.digest,
                    plan.effect_digest,
                    subject.uid,
                    subject.seat_id,
                    "plan",
                    target_seat_id,
                    manifest_revision,
                    idempotency_key,
                    request_digest,
                    plan_json,
                    plan.created_at,
                    plan.expires_at,
                ),
            )
            connection.execute("COMMIT")
            return plan, False
        except IdempotencyConflict:
            connection.execute("ROLLBACK")
            raise
        except sqlite3.IntegrityError as exc:
            connection.execute("ROLLBACK")
            raise HostControlError("journal uniqueness constraint failed") from exc
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def get_plan(
        self,
        *,
        subject: SubjectContext,
        plan_handle: str,
        expected_digest: str | None = None,
    ) -> PlanEnvelope:
        connection = self._require_open()
        row = connection.execute(
            f"{_SELECT_PLAN} WHERE plan_handle=?",
            (plan_handle,),
        ).fetchone()
        if row is None:
            raise UnknownHandle("unknown plan handle")
        if row[3] != subject.uid or row[4] != subject.seat_id:
            raise SubjectMismatch("plan belongs to another subject")
        plan = self._decode_row(row)
        if plan.owner != subject:
            raise SubjectMismatch("stored plan owner does not match subject")
        if expected_digest is not None and plan.digest != expected_digest:
            raise HostControlError("plan digest does not match expectation")
        return plan

    def load_plan_unbound(self, plan_handle: str) -> PlanEnvelope:
        """Unbound plan read serving the uid-restricted ``ApprovalSource``
        boundary (T-2912): the approval service must resolve plans it
        does not own, so this read carries no subject binding. No
        Manager path reaches it — Manager reads stay subject-bound via
        ``get_plan``, and the T-2912 structural test asserts the only
        production caller is ``approval_source.py``.
        """

        connection = self._require_open()
        row = connection.execute(
            f"{_SELECT_PLAN} WHERE plan_handle=?",
            (plan_handle,),
        ).fetchone()
        if row is None:
            raise UnknownHandle("unknown plan handle")
        return self._decode_row(row)

    @staticmethod
    def _decode_row(row: tuple[object, ...]) -> PlanEnvelope:
        try:
            raw = json.loads(row[10])
        except (TypeError, json.JSONDecodeError) as exc:
            raise HostControlError("corrupt journal plan JSON") from exc
        if not isinstance(raw, dict):
            raise HostControlError("corrupt journal plan shape")
        if row[10] != canonical_json(raw):
            raise HostControlError("corrupt journal plan canonical JSON")
        plan = PlanEnvelope.from_dict(raw)
        if not plan.validate_digest():
            raise HostControlError("corrupt journal plan digest")
        if (
            row[0] != plan.plan_handle
            or row[1] != plan.digest
            or row[2] != plan.effect_digest
            or row[3] != plan.owner.uid
            or row[4] != plan.owner.seat_id
            or row[5] != "plan"
            or row[6] != plan.target_seat_id
            or row[7] != plan.manifest_revision
            or row[11] != plan.created_at
            or row[12] != plan.expires_at
        ):
            raise HostControlError("journal row does not match plan envelope")
        target = SeatRecord(
            seat_id=plan.target_seat_id,
            uid=0,
            persona=plan.installer_plan.persona,
            role=plan.installer_plan.role,
            instance=plan.installer_plan.instance,
            account=plan.installer_plan.account,
            harness=plan.installer_plan.selected_harness,
            install_root=Path(plan.installer_plan.root),
        )
        manifest = ManifestRecord(
            revision=plan.manifest_revision,
            path=Path("/"),
            sha256=plan.manifest_sha256,
            framework_revision=plan.framework_revision,
            framework_sha256=plan.framework_sha256,
            template_ids=plan.template_ids,
        )
        if (
            compute_effect_digest(
                target=target,
                manifest=manifest,
                installer_plan=plan.installer_plan,
            )
            != plan.effect_digest
        ):
            raise HostControlError("corrupt journal effect digest")
        return plan

    # ------------------------------------------------------------------
    # operations (v2)
    # ------------------------------------------------------------------

    def put_operation_or_replay(
        self,
        *,
        record: OperationRecord,
    ) -> tuple[OperationRecord, bool]:
        connection = self._require_open()
        if not record.validate_digest():
            raise HostControlError("invalid operation record digest")
        if record.state != "prepared":
            raise HostControlError("new operations must be prepared")
        try:
            self._begin_immediate(connection, "put_operation_or_replay")
            row = connection.execute(
                f"{_SELECT_OPERATION} WHERE subject_uid=? AND "
                "subject_seat_id=? AND operation_kind=? AND "
                "target_seat_id=? AND idempotency_key=?",
                (
                    record.owner.uid,
                    record.owner.seat_id,
                    record.kind,
                    record.target_seat_id,
                    record.idempotency_key,
                ),
            ).fetchone()
            if row is not None:
                if row[13] != record.request_digest:
                    raise IdempotencyConflict(
                        "operation idempotency request conflict"
                    )
                stored = self._decode_operation_row(row)
                connection.execute("COMMIT")
                return stored, True
            self._insert_operation(connection, record)
            connection.execute("COMMIT")
            return record, False
        except IdempotencyConflict:
            connection.execute("ROLLBACK")
            raise
        except sqlite3.IntegrityError as exc:
            connection.execute("ROLLBACK")
            raise HostControlError(
                "operation uniqueness constraint failed"
            ) from exc
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def get_operation(
        self,
        *,
        subject: SubjectContext,
        operation_handle: str,
        expected_digest: str | None = None,
    ) -> OperationRecord:
        connection = self._require_open()
        row = connection.execute(
            f"{_SELECT_OPERATION} WHERE operation_handle=?",
            (operation_handle,),
        ).fetchone()
        if row is None:
            raise UnknownHandle("unknown operation handle")
        if row[3] != subject.uid or row[4] != subject.seat_id:
            raise SubjectMismatch("operation belongs to another subject")
        record = self._decode_operation_row(row)
        if record.owner != subject:
            raise SubjectMismatch(
                "stored operation owner does not match subject"
            )
        if expected_digest is not None and record.digest != expected_digest:
            raise HostControlError("operation digest does not match expectation")
        return record

    def subject_references_approval(
        self,
        *,
        subject: SubjectContext,
        approval_handle: str,
    ) -> bool:
        """True iff one of ``subject``'s own operations carries
        ``approval_handle`` (T-2917 5a: ApprovalGet subject binding).

        Read-only, single SELECT, no DDL and no schema-digest change. The
        operations table has no approval_handle index; the scan is bounded
        by the subject's own operation count on a lifecycle-only surface,
        and the predicate is exact equality so no other subject's row can
        ever satisfy it.
        """

        connection = self._require_open()
        row = connection.execute(
            "SELECT 1 FROM operations WHERE subject_uid=? AND "
            "subject_seat_id=? AND approval_handle=? LIMIT 1",
            (subject.uid, subject.seat_id, approval_handle),
        ).fetchone()
        return row is not None

    def subject_operation_binds_plan(
        self,
        *,
        subject: SubjectContext,
        approval_handle: str,
        plan_handle: str,
    ) -> bool:
        """True iff one of ``subject``'s own operations carries
        ``approval_handle`` AND is an operation on ``plan_handle`` (T-4875:
        the plan-bound half of ApprovalGet subject binding; the grant's
        plan comes from the approval service's status reply).

        Read-only, single SELECT, no DDL; exact equality on every column.
        """

        connection = self._require_open()
        row = connection.execute(
            "SELECT 1 FROM operations WHERE subject_uid=? AND "
            "subject_seat_id=? AND approval_handle=? AND plan_handle=? LIMIT 1",
            (subject.uid, subject.seat_id, approval_handle, plan_handle),
        ).fetchone()
        return row is not None

    def load_operation_unbound(self, operation_handle: str) -> OperationRecord:
        """Broker-internal recovery read; never expose on a public surface."""

        connection = self._require_open()
        row = connection.execute(
            f"{_SELECT_OPERATION} WHERE operation_handle=?",
            (operation_handle,),
        ).fetchone()
        if row is None:
            raise UnknownHandle("unknown operation handle")
        return self._decode_operation_row(row)

    def transition_operation(
        self,
        *,
        current: OperationRecord,
        new: OperationRecord,
        event_kind: str,
        payload: dict[str, object],
    ) -> None:
        connection = self._require_open()
        self._check_transition_pair(current, new)
        try:
            self._begin_immediate(
                connection,
                f"transition_operation[{event_kind}]",
            )
            self._insert_event(
                connection,
                operation_handle=current.operation_handle,
                from_state=current.state,
                to_state=new.state,
                event_kind=event_kind,
                payload=payload,
                created_at=new.updated_at,
            )
            self._guarded_update(connection, current=current, new=new)
            connection.execute("COMMIT")
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def append_event(
        self,
        *,
        current: OperationRecord,
        event_kind: str,
        payload: dict[str, object],
        created_at: str,
    ) -> None:
        """Informational same-state event (no transition)."""

        connection = self._require_open()
        try:
            self._begin_immediate(
                connection,
                f"append_event[{event_kind}]",
            )
            row = connection.execute(
                "SELECT state FROM operations WHERE operation_handle=?",
                (current.operation_handle,),
            ).fetchone()
            if row is None:
                raise UnknownHandle("unknown operation handle")
            if row[0] != current.state:
                raise HostControlError("stale operation state for event")
            self._insert_event(
                connection,
                operation_handle=current.operation_handle,
                from_state=current.state,
                to_state=current.state,
                event_kind=event_kind,
                payload=payload,
                created_at=created_at,
            )
            connection.execute("COMMIT")
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def record_dispatch_intent(
        self,
        *,
        current: OperationRecord,
        new: OperationRecord,
        receipt: OperationReceipt,
        intents: tuple[RollbackIntent, ...],
    ) -> None:
        """Durably sync staged receipt + write-ahead intents + DISPATCH_INTENT.

        One transaction with synchronous=FULL: everything is on disk before
        the caller may invoke an executor (ORG-052 section 6).
        """

        connection = self._require_open()
        self._check_transition_pair(current, new)
        if new.state != "dispatch_intent":
            raise InvalidTransition("record_dispatch_intent targets dispatch_intent")
        if receipt.phase != "staged":
            raise HostControlError("dispatch intent requires a staged receipt")
        if receipt.operation_handle != current.operation_handle:
            raise HostControlError("staged receipt operation mismatch")
        if not receipt.validate_digest():
            raise HostControlError("invalid staged receipt digest")
        for index, intent in enumerate(intents, start=1):
            if (
                intent.operation_handle != current.operation_handle
                or intent.seq != index
            ):
                raise HostControlError("rollback intent sequence mismatch")
        try:
            self._begin_immediate(connection, "record_dispatch_intent")
            connection.execute(
                "INSERT INTO operation_receipts VALUES (?,?,?,?,?,?)",
                (
                    receipt.receipt_handle,
                    receipt.operation_handle,
                    receipt.phase,
                    receipt.digest,
                    canonical_json(receipt.to_dict()),
                    receipt.created_at,
                ),
            )
            connection.executemany(
                "INSERT INTO rollback_intents VALUES (?,?,?,?,?,?)",
                tuple(
                    (
                        intent.operation_handle,
                        intent.seq,
                        canonical_json(intent.to_dict()),
                        0,
                        intent.created_at,
                        None,
                    )
                    for intent in intents
                ),
            )
            self._insert_event(
                connection,
                operation_handle=current.operation_handle,
                from_state=current.state,
                to_state=new.state,
                event_kind="dispatch_intent",
                payload={
                    "receipt_handle": receipt.receipt_handle,
                    "intent_count": len(intents),
                },
                created_at=new.updated_at,
            )
            self._guarded_update(connection, current=current, new=new)
            connection.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise HostControlError(
                "dispatch intent uniqueness constraint failed"
            ) from exc
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def record_terminal(
        self,
        *,
        current: OperationRecord,
        new: OperationRecord,
        receipt: OperationReceipt,
        payload: dict[str, object],
        compensation: OperationRecord | None = None,
    ) -> None:
        """Append the terminal receipt and state. T-4964: a ``compensation``
        (the broker-created rollback of a FAILED apply's restorable
        mutations) is inserted in the SAME transaction, so no failed receipt
        exists, even across a crash, without the compensation it licenses."""

        connection = self._require_open()
        self._check_transition_pair(current, new)
        if new.state not in ("completed", "failed"):
            raise InvalidTransition("record_terminal targets completed/failed")
        if compensation is not None:
            self._check_compensation(current, new, receipt, compensation)
        expected_phase = "complete" if new.state == "completed" else "failed"
        if receipt.phase != expected_phase:
            raise HostControlError("terminal receipt phase mismatch")
        if receipt.operation_handle != current.operation_handle:
            raise HostControlError("terminal receipt operation mismatch")
        if not receipt.validate_digest():
            raise HostControlError("invalid terminal receipt digest")
        try:
            self._begin_immediate(connection, "record_terminal")
            connection.execute(
                "INSERT INTO operation_receipts VALUES (?,?,?,?,?,?)",
                (
                    receipt.receipt_handle,
                    receipt.operation_handle,
                    receipt.phase,
                    receipt.digest,
                    canonical_json(receipt.to_dict()),
                    receipt.created_at,
                ),
            )
            connection.execute(
                "UPDATE rollback_intents SET resolved=1, resolved_at=? "
                "WHERE operation_handle=? AND resolved=0",
                (new.updated_at, current.operation_handle),
            )
            self._insert_event(
                connection,
                operation_handle=current.operation_handle,
                from_state=current.state,
                to_state=new.state,
                event_kind="result",
                payload=payload,
                created_at=new.updated_at,
            )
            self._guarded_update(connection, current=current, new=new)
            if compensation is not None:
                self._insert_operation(connection, compensation)
            connection.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise HostControlError(
                "terminal receipt uniqueness constraint failed"
            ) from exc
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def mark_manual_recovery(
        self,
        operation_handle: str,
        note: str,
        *,
        marked_at: str,
    ) -> None:
        """Park a corrupt-but-present operation row loudly and non-reusably.

        Deliberately does NOT rewrite operation_json: the corrupt bytes are
        evidence and stay in place (P4). Only the state column moves, so the
        idempotency tuple stays consumed and later reads stay loud.
        """

        connection = self._require_open()
        bounded_note = note[:512]
        try:
            self._begin_immediate(connection, "mark_manual_recovery")
            row = connection.execute(
                "SELECT state FROM operations WHERE operation_handle=?",
                (operation_handle,),
            ).fetchone()
            if row is None:
                raise UnknownHandle("unknown operation handle")
            state = row[0]
            if state == "manual_recovery":
                connection.execute("COMMIT")
                return
            if state in TERMINAL_OPERATION_STATES:
                raise HostControlError(
                    "cannot mark a terminal operation for manual recovery"
                )
            self._insert_event(
                connection,
                operation_handle=operation_handle,
                from_state=state,
                to_state="manual_recovery",
                event_kind="corruption",
                payload={"note": bounded_note},
                created_at=marked_at,
            )
            cursor = connection.execute(
                "UPDATE operations SET state='manual_recovery', updated_at=? "
                "WHERE operation_handle=? AND state=?",
                (marked_at, operation_handle, state),
            )
            if cursor.rowcount != 1:
                raise HostControlError("stale operation state")
            connection.execute("COMMIT")
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def get_receipt(
        self,
        *,
        subject: SubjectContext,
        receipt_handle: str,
    ) -> OperationReceipt:
        connection = self._require_open()
        row = connection.execute(
            "SELECT r.receipt_handle, r.operation_handle, r.phase, "
            "r.receipt_digest, r.receipt_json, r.created_at, "
            "o.subject_uid, o.subject_seat_id "
            "FROM operation_receipts r JOIN operations o "
            "ON r.operation_handle = o.operation_handle "
            "WHERE r.receipt_handle=?",
            (receipt_handle,),
        ).fetchone()
        if row is None:
            raise UnknownHandle("unknown receipt handle")
        if row[6] != subject.uid or row[7] != subject.seat_id:
            raise SubjectMismatch("receipt belongs to another subject")
        receipt = self._decode_receipt_row(row[:6])
        if receipt.subject != subject:
            raise SubjectMismatch(
                "stored receipt subject does not match caller"
            )
        return receipt

    def get_staged_receipt(self, operation_handle: str) -> OperationReceipt:
        connection = self._require_open()
        row = connection.execute(
            f"{_SELECT_RECEIPT} WHERE operation_handle=? AND phase='staged'",
            (operation_handle,),
        ).fetchone()
        if row is None:
            raise UnknownHandle("no staged receipt for operation")
        return self._decode_receipt_row(row)

    def unresolved_intents(
        self,
        operation_handle: str,
    ) -> tuple[RollbackIntent, ...]:
        connection = self._require_open()
        rows = connection.execute(
            "SELECT operation_handle, seq, intent_json FROM rollback_intents "
            "WHERE operation_handle=? AND resolved=0 ORDER BY seq",
            (operation_handle,),
        ).fetchall()
        intents = []
        for row in rows:
            try:
                raw = json.loads(row[2])
            except (TypeError, json.JSONDecodeError) as exc:
                raise HostControlError("corrupt rollback intent JSON") from exc
            if not isinstance(raw, dict) or row[2] != canonical_json(raw):
                raise HostControlError("corrupt rollback intent canonical JSON")
            intent = RollbackIntent.from_dict(raw)
            if intent.operation_handle != row[0] or intent.seq != row[1]:
                raise HostControlError(
                    "rollback intent row does not match record"
                )
            intents.append(intent)
        return tuple(intents)

    def claim_spec_serve(self, *, current: OperationRecord, created_at: str) -> bool:
        """Atomically record an ``ExecutionSource.OperationSpec`` serve of a
        MUTATING operation (T-2917 5b review S4). A spec is served iff
        ``count(spec_served) <= count(spec_rearmed)`` (T-2917 5d.2): exactly
        once, plus once more per journaled re-arm, so a broker-uid process
        cannot re-trigger an apply parked for the reconciler. Returns False
        when the serve is refused."""

        connection = self._require_open()
        try:
            self._begin_immediate(connection, "claim_spec_serve")
            row = connection.execute(
                "SELECT state FROM operations WHERE operation_handle=?",
                (current.operation_handle,),
            ).fetchone()
            if row is None:
                raise UnknownHandle("unknown operation handle")
            # the driver may record DISPATCHED between the executor's call
            # and this serve; both dispatchable states are fine
            state = row[0]
            if state not in ("dispatch_intent", "dispatched"):
                raise HostControlError("operation is not dispatchable for spec serve")
            served = self._event_count(connection, current.operation_handle, "spec_served")
            rearmed = self._event_count(connection, current.operation_handle, "spec_rearmed")
            if served > rearmed:
                connection.execute("COMMIT")
                return False
            self._insert_event(
                connection,
                operation_handle=current.operation_handle,
                from_state=state,
                to_state=state,
                event_kind="spec_served",
                payload={"served": True},
                created_at=created_at,
            )
            connection.execute("COMMIT")
            return True
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    @staticmethod
    def _event_count(connection: sqlite3.Connection, operation_handle: str, event_kind: str) -> int:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM operation_events WHERE operation_handle=? AND event_kind=?",
                (operation_handle, event_kind),
            ).fetchone()[0]
        )

    def rearm_spec_serve(self, *, current: OperationRecord, reason: str, created_at: str) -> int:
        """Journal ONE more permitted serve of a mutating spec (T-2917 5d.2,
        carry-in N5b; README n3). BEGIN IMMEDIATE; refused unless the
        operation is dispatchable, its spec was served before and that serve
        is consumed (no re-arm is outstanding), ``reason`` is in the closed
        ``REARM_REASONS`` set and fewer than ``MAX_SPEC_REARMS`` re-arms
        exist. Appends a ``spec_rearmed`` event (also a pre-Execute marker)
        and returns the new re-arm count.

        The ONLY caller is ``OperationService._reconcile``, after a live-
        state proof; no D-Bus handler reaches it (AST-tested)."""

        if reason not in REARM_REASONS:
            raise HostControlError("invalid spec re-arm reason")
        connection = self._require_open()
        try:
            self._begin_immediate(connection, "rearm_spec_serve")
            row = connection.execute(
                "SELECT state FROM operations WHERE operation_handle=?",
                (current.operation_handle,),
            ).fetchone()
            if row is None:
                raise UnknownHandle("unknown operation handle")
            state = row[0]
            if state != current.state or state not in ("dispatch_intent", "dispatched"):
                raise HostControlError("operation is not dispatchable for a spec re-arm")
            served = self._event_count(connection, current.operation_handle, "spec_served")
            rearmed = self._event_count(connection, current.operation_handle, "spec_rearmed")
            if served == 0:
                raise RecoveryRefused("spec was never served; nothing to re-arm")
            if served <= rearmed:
                raise RecoveryRefused("a spec re-arm is already outstanding")
            if rearmed >= MAX_SPEC_REARMS:
                raise RecoveryRefused("spec re-arm cap reached")
            self._insert_event(
                connection,
                operation_handle=current.operation_handle,
                from_state=state,
                to_state=state,
                event_kind="spec_rearmed",
                payload={"reason": reason, "rearm": rearmed + 1},
                created_at=created_at,
            )
            connection.execute("COMMIT")
            return rearmed + 1
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def intents_for(self, operation_handle: str) -> tuple[RollbackIntent, ...]:
        """Read-only (T-2917 5d.2): EVERY write-ahead intent of an operation,
        resolved and unresolved, in sequence order (the restorable-set
        filter of a rollback reads the SOURCE apply's intents)."""

        connection = self._require_open()
        rows = connection.execute(
            "SELECT operation_handle, seq, intent_json FROM rollback_intents "
            "WHERE operation_handle=? ORDER BY seq",
            (operation_handle,),
        ).fetchall()
        return tuple(self._decode_intent_row(row) for row in rows)

    @staticmethod
    def _decode_intent_row(row: tuple[object, ...]) -> RollbackIntent:
        try:
            raw = json.loads(row[2])
        except (TypeError, json.JSONDecodeError) as exc:
            raise HostControlError("corrupt rollback intent JSON") from exc
        if not isinstance(raw, dict) or row[2] != canonical_json(raw):
            raise HostControlError("corrupt rollback intent canonical JSON")
        intent = RollbackIntent.from_dict(raw)
        if intent.operation_handle != row[0] or intent.seq != row[1]:
            raise HostControlError("rollback intent row does not match record")
        return intent

    def dispatch_execution_ref(self, operation_handle: str) -> str | None:
        """Read-only (T-2917 5b, ruling B11): the execution reference the
        executor returned at the LATEST dispatch or re-dispatch of this
        operation, or None if it was never dispatched. Lets the broker's
        driver resume collecting after a broker restart without ever
        re-dispatching a mutating operation."""

        connection = self._require_open()
        row = connection.execute(
            "SELECT payload_json FROM operation_events "
            "WHERE operation_handle=? AND event_kind IN ('dispatched', 'redispatched') "
            "ORDER BY seq DESC LIMIT 1",
            (operation_handle,),
        ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise HostControlError("corrupt operation event JSON") from exc
        reference = payload.get("execution_ref") if isinstance(payload, dict) else None
        if not isinstance(reference, str) or not 1 <= len(reference) <= 128:
            raise HostControlError("corrupt dispatch execution reference")
        return reference

    def count_events(self, operation_handle: str, event_kinds: tuple[str, ...]) -> int:
        """Read-only (T-2917 5d.1): how many events of these kinds the
        operation has, over its whole lifetime (every drive, every broker
        restart). The lifetime verify re-dispatch cap counts with it."""

        kinds = tuple(str(kind) for kind in event_kinds)
        if not kinds:
            return 0
        connection = self._require_open()
        placeholders = ",".join("?" for _ in kinds)
        row = connection.execute(
            "SELECT COUNT(*) FROM operation_events WHERE operation_handle=? "
            f"AND event_kind IN ({placeholders})",
            (operation_handle, *kinds),
        ).fetchone()
        return int(row[0])

    def execute_issuance_count(self, operation_handle: str) -> int:
        """Read-only (T-2917:D27): how many Executes were ever issued for
        this operation, over its whole lifetime (every arm, every restart),
        less the issuances the executor definitively answered ``Busy``
        (T-2917:D29; each void is bound to one distinct marker)."""

        issued = self.count_events(operation_handle, EXECUTE_ISSUANCE_MARKERS)
        voided = self.count_events(operation_handle, (EXECUTE_VOIDED_EVENT,))
        return max(issued - voided, 0)

    def void_execute_issuance(self, *, current: OperationRecord, refusal: str, created_at: str) -> int:
        """Journal that the operation's LATEST Execute issuance created no
        execution (T-2917 5d.2 follow-up, ruling T-2917:D29): the executor
        answered it with a definitive ``Busy``. BEGIN IMMEDIATE; refused
        unless ``refusal`` is in the closed ``VOIDING_REFUSALS`` set, the
        operation is dispatchable in its recorded state, the latest issuance
        marker exists, is not already voided and was not followed by a
        ``dispatched``/``redispatched`` record (an Execute that was ACCEPTED
        is never voided). Returns the voided marker's sequence number.

        Broker-only: the ONLY caller is ``OperationService._execute_or_void``
        on the broker's own Execute reply; no D-Bus handler reaches it and
        the executor package imports no journal machinery (both tested), so
        the executor uid cannot write it."""

        if refusal not in VOIDING_REFUSALS:
            raise HostControlError("invalid execute-void refusal")
        connection = self._require_open()
        try:
            self._begin_immediate(connection, "void_execute_issuance")
            row = connection.execute(
                "SELECT state FROM operations WHERE operation_handle=?",
                (current.operation_handle,),
            ).fetchone()
            if row is None:
                raise UnknownHandle("unknown operation handle")
            state = row[0]
            if state != current.state or state not in ("dispatch_intent", "dispatched"):
                raise HostControlError("operation is not dispatchable for an execute void")
            marker = connection.execute(
                "SELECT MAX(seq) FROM operation_events WHERE operation_handle=? "
                f"AND event_kind IN ({','.join('?' for _ in EXECUTE_ISSUANCE_MARKERS)})",
                (current.operation_handle, *EXECUTE_ISSUANCE_MARKERS),
            ).fetchone()[0]
            if marker is None:
                raise HostControlError("no execute issuance to void")
            later = connection.execute(
                "SELECT COUNT(*) FROM operation_events WHERE operation_handle=? AND seq > ? "
                "AND event_kind IN ('dispatched', 'redispatched', ?)",
                (current.operation_handle, int(marker), EXECUTE_VOIDED_EVENT),
            ).fetchone()[0]
            if int(later):
                raise HostControlError("the latest execute issuance was accepted or is already voided")
            self._insert_event(
                connection,
                operation_handle=current.operation_handle,
                from_state=state,
                to_state=state,
                event_kind=EXECUTE_VOIDED_EVENT,
                payload={"refusal": refusal, "marker_seq": int(marker)},
                created_at=created_at,
            )
            connection.execute("COMMIT")
            return int(marker)
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def spec_served_after_latest_dispatch(self, operation_handle: str) -> bool:
        """Read-only (T-2917 5d.1, ruling T-2917:D14): True when a
        ``spec_served`` event follows the operation's LATEST pre-Execute
        marker. The markers are the events the broker writes BEFORE an
        Execute call (``dispatch_intent``; ``spec_rearmed`` and
        ``execute_retry`` from 5d.2), never ``dispatched``, which the driver
        may write after the executor has already fetched the spec.

        A refused execution whose spec was served after that marker was
        refused by the executor's own parse, before it touched the host. One
        whose spec was NOT served after it had the SERVE refused (e.g.
        already served to an earlier attempt), so that earlier attempt's
        outcome is unknown."""

        connection = self._require_open()
        marker = connection.execute(
            "SELECT MAX(seq) FROM operation_events WHERE operation_handle=? "
            f"AND event_kind IN ({','.join('?' for _ in PRE_EXECUTE_MARKERS)})",
            (operation_handle, *PRE_EXECUTE_MARKERS),
        ).fetchone()[0]
        served = connection.execute(
            "SELECT 1 FROM operation_events WHERE operation_handle=? "
            "AND event_kind='spec_served' AND seq > ? LIMIT 1",
            (operation_handle, int(marker or 0)),
        ).fetchone()
        return served is not None

    @staticmethod
    def _check_compensation(
        current: OperationRecord,
        new: OperationRecord,
        receipt: OperationReceipt,
        compensation: OperationRecord,
    ) -> None:
        if (
            current.kind != "apply"
            or new.state != "failed"
            or receipt.phase != "failed"
            or not compensation.validate_digest()
            or compensation.kind != "rollback"
            or compensation.state != "prepared"
            or compensation.approval_handle != COMPENSATION_APPROVAL_SENTINEL
            or compensation.receipt_handle != receipt.receipt_handle
            or compensation.receipt_digest != receipt.digest
            or compensation.owner != current.owner
            or compensation.target_seat_id != current.target_seat_id
            or compensation.plan_handle != current.plan_handle
            or compensation.plan_digest != current.plan_digest
            or compensation.idempotency_key
            != COMPENSATION_KEY_PREFIX + current.operation_handle
            or compensation.authority_record_id is not None
            or tuple(compensation.operation_families)
            != tuple(current.operation_families)
        ):
            raise HostControlError("invalid compensation for this terminal")

    def compensation_ref(
        self, source_operation_handle: str
    ) -> tuple[str, str, int, str] | None:
        """T-4964: ``(operation_handle, state, subject_uid, subject_seat_id)``
        of the broker-created compensation of a failed apply (the rollback
        bound to that apply's FAILED receipt, carrying the compensation
        sentinel), or None. Read from the COLUMNS only, so a compensation
        parked by ``mark_manual_recovery`` (whose operation_json is left
        untouched on purpose) still reads, and the source apply's
        OperationGet never fails on it. Read-only; no DDL change."""

        connection = self._require_open()
        row = connection.execute(
            "SELECT operation_handle, state, subject_uid, subject_seat_id "
            "FROM operations WHERE approval_handle=? AND receipt_handle IN "
            "(SELECT receipt_handle FROM operation_receipts "
            "WHERE operation_handle=? AND phase='failed')",
            (COMPENSATION_APPROVAL_SENTINEL, source_operation_handle),
        ).fetchone()
        if row is None:
            return None
        return (str(row[0]), str(row[1]), int(row[2]), str(row[3]))

    def compensation_for(self, source_operation_handle: str) -> OperationRecord | None:
        """T-4964: the decoded compensation record of a failed apply, or None."""

        ref = self.compensation_ref(source_operation_handle)
        return None if ref is None else self.load_operation_unbound(ref[0])

    def pending_operations(self) -> tuple[str, ...]:
        connection = self._require_open()
        placeholders = ",".join("?" for _ in TERMINAL_OPERATION_STATES)
        rows = connection.execute(
            "SELECT operation_handle FROM operations "
            f"WHERE state NOT IN ({placeholders}) ORDER BY created_at",
            tuple(sorted(TERMINAL_OPERATION_STATES)),
        ).fetchall()
        return tuple(row[0] for row in rows)

    # ------------------------------------------------------------------
    # operation internals
    # ------------------------------------------------------------------

    @staticmethod
    def _check_transition_pair(
        current: OperationRecord,
        new: OperationRecord,
    ) -> None:
        if not current.validate_digest() or not new.validate_digest():
            raise HostControlError("invalid operation record digest")
        if current.operation_handle != new.operation_handle:
            raise HostControlError("operation handle mismatch")
        if new.state not in OPERATION_TRANSITIONS[current.state]:
            raise InvalidTransition(
                f"illegal transition {current.state} -> {new.state}"
            )

    @classmethod
    def _insert_operation(
        cls,
        connection: sqlite3.Connection,
        record: OperationRecord,
    ) -> None:
        """INSERT one new PREPARED operation and its ``prepared`` event inside
        the caller's open transaction (shared by ``put_operation_or_replay``
        and the T-4964 compensation insert in ``record_terminal``)."""

        connection.execute(
            "INSERT INTO operations VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                record.operation_handle,
                record.kind,
                record.state,
                record.owner.uid,
                record.owner.seat_id,
                record.target_seat_id,
                record.plan_handle,
                record.plan_digest,
                record.receipt_handle,
                record.receipt_digest,
                record.approval_handle,
                record.authority_record_id,
                record.idempotency_key,
                record.request_digest,
                canonical_json(record.to_dict()),
                record.created_at,
                record.updated_at,
            ),
        )
        cls._insert_event(
            connection,
            operation_handle=record.operation_handle,
            from_state="none",
            to_state="prepared",
            event_kind="prepared",
            payload={"request_digest": record.request_digest},
            created_at=record.created_at,
        )

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        operation_handle: str,
        from_state: str,
        to_state: str,
        event_kind: str,
        payload: dict[str, object],
        created_at: str,
    ) -> None:
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM operation_events "
            "WHERE operation_handle=?",
            (operation_handle,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO operation_events VALUES (?,?,?,?,?,?,?)",
            (
                operation_handle,
                seq,
                from_state,
                to_state,
                event_kind,
                canonical_json(payload),
                created_at,
            ),
        )

    @staticmethod
    def _guarded_update(
        connection: sqlite3.Connection,
        *,
        current: OperationRecord,
        new: OperationRecord,
    ) -> None:
        cursor = connection.execute(
            "UPDATE operations SET state=?, authority_record_id=?, "
            "operation_json=?, updated_at=? "
            "WHERE operation_handle=? AND state=?",
            (
                new.state,
                new.authority_record_id,
                canonical_json(new.to_dict()),
                new.updated_at,
                current.operation_handle,
                current.state,
            ),
        )
        if cursor.rowcount != 1:
            raise HostControlError("stale operation state")

    @staticmethod
    def _decode_operation_row(row: tuple[object, ...]) -> OperationRecord:
        try:
            raw = json.loads(row[14])
        except (TypeError, json.JSONDecodeError) as exc:
            raise HostControlError("corrupt journal operation JSON") from exc
        if not isinstance(raw, dict):
            raise HostControlError("corrupt journal operation shape")
        if row[14] != canonical_json(raw):
            raise HostControlError("corrupt journal operation canonical JSON")
        record = OperationRecord.from_dict(raw)
        if not record.validate_digest():
            raise HostControlError("corrupt journal operation digest")
        if (
            row[0] != record.operation_handle
            or row[1] != record.kind
            or row[2] != record.state
            or row[3] != record.owner.uid
            or row[4] != record.owner.seat_id
            or row[5] != record.target_seat_id
            or row[6] != record.plan_handle
            or row[7] != record.plan_digest
            or row[8] != record.receipt_handle
            or row[9] != record.receipt_digest
            or row[10] != record.approval_handle
            or row[11] != record.authority_record_id
            or row[12] != record.idempotency_key
            or row[13] != record.request_digest
            or row[15] != record.created_at
            or row[16] != record.updated_at
        ):
            raise HostControlError(
                "journal row does not match operation record"
            )
        return record

    @staticmethod
    def _decode_receipt_row(row: tuple[object, ...]) -> OperationReceipt:
        try:
            raw = json.loads(row[4])
        except (TypeError, json.JSONDecodeError) as exc:
            raise HostControlError("corrupt journal receipt JSON") from exc
        if not isinstance(raw, dict):
            raise HostControlError("corrupt journal receipt shape")
        if row[4] != canonical_json(raw):
            raise HostControlError("corrupt journal receipt canonical JSON")
        receipt = OperationReceipt.from_dict(raw)
        if not receipt.validate_digest():
            raise HostControlError("corrupt journal receipt digest")
        if (
            row[0] != receipt.receipt_handle
            or row[1] != receipt.operation_handle
            or row[2] != receipt.phase
            or row[3] != receipt.digest
            or row[5] != receipt.created_at
        ):
            raise HostControlError(
                "journal row does not match receipt record"
            )
        return receipt
