"""Durable, fail-closed SQLite journal for broker plan authority."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from tools.host_control.contracts import (
    HostControlError,
    IdempotencyConflict,
    ManifestRecord,
    PlanEnvelope,
    SeatRecord,
    SubjectContext,
    SubjectMismatch,
    UnknownHandle,
    canonical_json,
    compute_effect_digest,
)


APPLICATION_ID = 1346913090
USER_VERSION = 1
SCHEMA_DIGEST = "82b1b18c874607a62119778fd92d7310ac75936c08e56fedd356fdc3fa8ee2fa"
DDL = """CREATE TABLE journal_meta (
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

_SELECT_PLAN = (
    "SELECT plan_handle, plan_digest, effect_digest, subject_uid, "
    "subject_seat_id, action, target_seat_id, manifest_revision, "
    "idempotency_key, request_digest, plan_json, created_at, expires_at "
    "FROM plans"
)


def _schema_objects(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    return tuple(
        connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        )
    )


def _expected_schema_objects() -> tuple[tuple[object, ...], ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(DDL)
        return _schema_objects(connection)
    finally:
        connection.close()


_EXPECTED_OBJECTS = _expected_schema_objects()


class BrokerJournal:
    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = path
        self._connection = connection
        self._closed = False

    @classmethod
    def open(cls, path: Path) -> "BrokerJournal":
        if hashlib.sha256(DDL.encode("utf-8")).hexdigest() != SCHEMA_DIGEST:
            raise HostControlError("compiled journal schema digest mismatch")
        if not path.is_absolute():
            path = path.resolve()
        parent = path.parent
        if not parent.is_dir():
            raise HostControlError("journal parent directory is absent")
        try:
            connection = sqlite3.connect(
                path,
                timeout=5.0,
                isolation_level=None,
            )
            connection.execute("PRAGMA busy_timeout=5000")
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
            cls._validate_schema(connection)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            if created:
                directory_fd = os.open(
                    parent,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                )
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return cls(path, connection)
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
    def _validate_schema(connection: sqlite3.Connection) -> None:
        if (
            connection.execute("PRAGMA application_id").fetchone()[0]
            != APPLICATION_ID
            or connection.execute("PRAGMA user_version").fetchone()[0]
            != USER_VERSION
        ):
            raise HostControlError("journal version marker mismatch")
        if _schema_objects(connection) != _EXPECTED_OBJECTS:
            raise HostControlError("journal schema object mismatch")
        rows = connection.execute(
            "SELECT singleton, schema_version, schema_digest FROM journal_meta"
        ).fetchall()
        if rows != [(1, USER_VERSION, SCHEMA_DIGEST)]:
            raise HostControlError("journal metadata mismatch")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connection.close()

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
        if self._closed:
            raise HostControlError("journal is closed")
        if not plan.validate_digest():
            raise HostControlError("invalid plan envelope digest")
        if (
            plan.owner != subject
            or plan.target_seat_id != target_seat_id
            or plan.manifest_revision != manifest_revision
        ):
            raise HostControlError("journal plan identity mismatch")
        plan_json = canonical_json(plan.to_dict())
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
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
        if self._closed:
            raise HostControlError("journal is closed")
        row = self._connection.execute(
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
