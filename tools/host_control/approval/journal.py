"""Physically separate durable approval journal (ORG-052 §5/§6 — T-2910).

A SEPARATE SQLite database file from the broker journal, in a separate
module tree. The broker cannot mint or edit approval records; the approval
service cannot edit plans, operations, or receipts. Distinct
``application_id`` markers make each journal loudly refuse the other's
database file at open() time (the DATA-level separation layer; see
``approval/contracts.py`` for the full labeled claim).

Deliberately does NOT import ``tools.host_control.journal`` (L3): the tiny
fsync directory-durability wrapper is duplicated here rather than reaching
into the broker journal module — an import edge across the authority
boundary is worse than a handful of duplicated lines.

Lifecycle (ORG-068): this module CREATES a durable database file. Its
removal path is owned by T-2921; configuration (root-owned unit, paths,
bus policy) by T-2913; monitoring wiring by T-2914 + T-2923; future schema
migrations by T-2922 (the application_id + user_version + literal schema
digest guard below is the forward migration contract — this unit is a
fresh v1 with no migration).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from tools.host_control.approval.contracts import (
    GRANT_TRANSITIONS,
    REVIEW_TRANSITIONS,
    ApprovalCounts,
    GrantRecord,
    OperationConsumed,
    ReviewRecord,
    StaleAuthorityState,
)
from tools.host_control.approval.human import (
    HUMAN_REQUEST_TRANSITIONS,
    AttestationRecord,
    HumanRequestRecord,
)
from tools.host_control.contracts import (
    HostControlError,
    IdempotencyConflict,
    InvalidTransition,
    JournalBusy,
    UnknownHandle,
    canonical_json,
)


# Default write-lock wait. Injectable through ApprovalJournal.open so a test
# can force lock contention without depending on host load.
BUSY_TIMEOUT_MS = 5000

# 0x50484341 ("PHCA" — Pantheon Host Control Approval). The broker journal
# is 0x50484342 ("PHCB", = 1346913090, verified at source in
# tools/host_control/journal.py). Distinct markers are the DATA-level
# separation guarantee: neither journal can even open the other's file.
APPLICATION_ID = 1346913089
USER_VERSION = 1

DDL = """CREATE TABLE journal_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    schema_digest TEXT NOT NULL CHECK (
        length(schema_digest) = 64
        AND schema_digest NOT GLOB '*[^0-9a-f]*'
    ),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE reviews (
    review_handle TEXT PRIMARY KEY
        CHECK (
            length(review_handle) = 64
            AND review_handle NOT GLOB '*[^0-9a-f]*'
        ),
    plan_handle TEXT NOT NULL CHECK (
        length(plan_handle) = 64
        AND plan_handle NOT GLOB '*[^0-9a-f]*'
    ),
    plan_digest TEXT NOT NULL CHECK (
        length(plan_digest) = 64
        AND plan_digest NOT GLOB '*[^0-9a-f]*'
    ),
    plan_owner TEXT NOT NULL CHECK (length(plan_owner) BETWEEN 1 AND 128),
    performer TEXT NOT NULL CHECK (length(performer) BETWEEN 1 AND 128),
    target_seat_id TEXT NOT NULL CHECK (length(target_seat_id) BETWEEN 1 AND 128),
    operation_families TEXT NOT NULL CHECK (
        length(operation_families) BETWEEN 2 AND 4096
    ),
    effect_digest TEXT NOT NULL CHECK (
        length(effect_digest) = 64
        AND effect_digest NOT GLOB '*[^0-9a-f]*'
    ),
    manifest_revision TEXT NOT NULL CHECK (
        length(manifest_revision) BETWEEN 1 AND 128
    ),
    policy_revision TEXT NOT NULL CHECK (
        length(policy_revision) BETWEEN 1 AND 128
    ),
    handoff_kind TEXT NOT NULL CHECK (
        handoff_kind IN ('direct_no_handoff', 'request_plan')
    ),
    request_plan_handle TEXT CHECK (
        request_plan_handle IS NULL
        OR (
            length(request_plan_handle) = 64
            AND request_plan_handle NOT GLOB '*[^0-9a-f]*'
        )
    ),
    request_plan_digest TEXT CHECK (
        request_plan_digest IS NULL
        OR (
            length(request_plan_digest) = 64
            AND request_plan_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    request_plan_owner TEXT CHECK (
        request_plan_owner IS NULL
        OR length(request_plan_owner) BETWEEN 1 AND 128
    ),
    shared_effect_digest TEXT CHECK (
        shared_effect_digest IS NULL
        OR (
            length(shared_effect_digest) = 64
            AND shared_effect_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    handoff_anchor TEXT CHECK (
        handoff_anchor IS NULL OR length(handoff_anchor) BETWEEN 1 AND 256
    ),
    requested_by TEXT NOT NULL CHECK (length(requested_by) BETWEEN 1 AND 128),
    state TEXT NOT NULL CHECK (
        state IN ('open', 'granted', 'denied', 'expired')
    ),
    decision TEXT CHECK (decision IS NULL OR decision IN ('granted', 'denied')),
    reason_code TEXT CHECK (
        reason_code IS NULL OR length(reason_code) BETWEEN 1 AND 64
    ),
    approver_subject TEXT CHECK (
        approver_subject IS NULL OR length(approver_subject) BETWEEN 1 AND 128
    ),
    authority_source TEXT CHECK (
        authority_source IS NULL
        OR authority_source IN ('agent', 'human', 'operator')
    ),
    audit_anchor TEXT CHECK (
        audit_anchor IS NULL OR length(audit_anchor) BETWEEN 1 AND 256
    ),
    source_event TEXT CHECK (
        source_event IS NULL OR length(source_event) BETWEEN 1 AND 256
    ),
    decide_idempotency_key TEXT CHECK (
        decide_idempotency_key IS NULL
        OR length(decide_idempotency_key) BETWEEN 1 AND 128
    ),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    review_json TEXT NOT NULL CHECK (length(review_json) BETWEEN 2 AND 1048576),
    CHECK ((handoff_kind = 'request_plan') = (request_plan_handle IS NOT NULL)),
    CHECK ((request_plan_handle IS NULL) = (request_plan_digest IS NULL)),
    CHECK ((request_plan_handle IS NULL) = (request_plan_owner IS NULL)),
    CHECK ((request_plan_handle IS NULL) = (shared_effect_digest IS NULL)),
    CHECK (handoff_anchor IS NULL OR handoff_kind = 'request_plan'),
    CHECK ((state IN ('granted', 'denied')) = (decision IS NOT NULL)),
    CHECK (
        decision IS NULL
        OR (state = 'granted' AND decision = 'granted')
        OR (state = 'denied' AND decision = 'denied')
    ),
    CHECK ((decision IS NOT NULL) = (reason_code IS NOT NULL)),
    CHECK ((decision IS NOT NULL) = (approver_subject IS NOT NULL)),
    CHECK ((decision IS NOT NULL) = (authority_source IS NOT NULL)),
    CHECK ((decision IS NOT NULL) = (audit_anchor IS NOT NULL)),
    CHECK ((decision IS NOT NULL) = (decide_idempotency_key IS NOT NULL)),
    CHECK ((decision IS NOT NULL) = (decided_at IS NOT NULL)),
    CHECK (
        authority_source IS NULL
        OR authority_source != 'agent'
        OR source_event IS NULL
    )
) STRICT;

CREATE UNIQUE INDEX reviews_open_request_idx
    ON reviews(
        requested_by,
        plan_handle,
        plan_digest,
        handoff_kind,
        ifnull(request_plan_handle, '')
    )
    WHERE state = 'open';

CREATE UNIQUE INDEX reviews_decide_key_idx
    ON reviews(approver_subject, decide_idempotency_key)
    WHERE decide_idempotency_key IS NOT NULL;

CREATE INDEX reviews_expiry_idx ON reviews(state, expires_at);

CREATE TABLE grants (
    approval_handle TEXT PRIMARY KEY
        CHECK (
            length(approval_handle) = 64
            AND approval_handle NOT GLOB '*[^0-9a-f]*'
        ),
    review_handle TEXT NOT NULL UNIQUE REFERENCES reviews(review_handle) CHECK (
        length(review_handle) = 64
        AND review_handle NOT GLOB '*[^0-9a-f]*'
    ),
    plan_handle TEXT NOT NULL CHECK (
        length(plan_handle) = 64
        AND plan_handle NOT GLOB '*[^0-9a-f]*'
    ),
    plan_digest TEXT NOT NULL CHECK (
        length(plan_digest) = 64
        AND plan_digest NOT GLOB '*[^0-9a-f]*'
    ),
    plan_owner TEXT NOT NULL CHECK (length(plan_owner) BETWEEN 1 AND 128),
    performer TEXT NOT NULL CHECK (length(performer) BETWEEN 1 AND 128),
    target_seat_id TEXT NOT NULL CHECK (length(target_seat_id) BETWEEN 1 AND 128),
    operation_families TEXT NOT NULL CHECK (
        length(operation_families) BETWEEN 2 AND 4096
    ),
    effect_digest TEXT NOT NULL CHECK (
        length(effect_digest) = 64
        AND effect_digest NOT GLOB '*[^0-9a-f]*'
    ),
    manifest_revision TEXT NOT NULL CHECK (
        length(manifest_revision) BETWEEN 1 AND 128
    ),
    policy_revision TEXT NOT NULL CHECK (
        length(policy_revision) BETWEEN 1 AND 128
    ),
    handoff_kind TEXT NOT NULL CHECK (
        handoff_kind IN ('direct_no_handoff', 'request_plan')
    ),
    request_plan_handle TEXT CHECK (
        request_plan_handle IS NULL
        OR (
            length(request_plan_handle) = 64
            AND request_plan_handle NOT GLOB '*[^0-9a-f]*'
        )
    ),
    request_plan_digest TEXT CHECK (
        request_plan_digest IS NULL
        OR (
            length(request_plan_digest) = 64
            AND request_plan_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    request_plan_owner TEXT CHECK (
        request_plan_owner IS NULL
        OR length(request_plan_owner) BETWEEN 1 AND 128
    ),
    shared_effect_digest TEXT CHECK (
        shared_effect_digest IS NULL
        OR (
            length(shared_effect_digest) = 64
            AND shared_effect_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    handoff_anchor TEXT CHECK (
        handoff_anchor IS NULL OR length(handoff_anchor) BETWEEN 1 AND 256
    ),
    approver_subject TEXT NOT NULL CHECK (
        length(approver_subject) BETWEEN 1 AND 128
    ),
    authority_source TEXT NOT NULL CHECK (
        authority_source IN ('agent', 'human', 'operator')
    ),
    audit_anchor TEXT NOT NULL CHECK (length(audit_anchor) BETWEEN 1 AND 256),
    source_event TEXT CHECK (
        source_event IS NULL OR length(source_event) BETWEEN 1 AND 256
    ),
    decision TEXT NOT NULL CHECK (decision = 'granted'),
    reason_code TEXT NOT NULL CHECK (length(reason_code) BETWEEN 1 AND 64),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    nonce TEXT NOT NULL UNIQUE CHECK (
        length(nonce) = 64
        AND nonce NOT GLOB '*[^0-9a-f]*'
    ),
    idempotency_key TEXT NOT NULL CHECK (
        length(idempotency_key) BETWEEN 1 AND 128
    ),
    state TEXT NOT NULL CHECK (
        state IN ('issued', 'bound', 'revoked', 'expired')
    ),
    consuming_operation_handle TEXT UNIQUE CHECK (
        consuming_operation_handle IS NULL
        OR (
            length(consuming_operation_handle) = 64
            AND consuming_operation_handle NOT GLOB '*[^0-9a-f]*'
        )
    ),
    authority_record_id TEXT UNIQUE CHECK (
        authority_record_id IS NULL
        OR (
            length(authority_record_id) = 64
            AND authority_record_id NOT GLOB '*[^0-9a-f]*'
        )
    ),
    updated_at TEXT NOT NULL,
    grant_json TEXT NOT NULL CHECK (length(grant_json) BETWEEN 2 AND 1048576),
    CHECK ((handoff_kind = 'request_plan') = (request_plan_handle IS NOT NULL)),
    CHECK ((request_plan_handle IS NULL) = (request_plan_digest IS NULL)),
    CHECK ((request_plan_handle IS NULL) = (request_plan_owner IS NULL)),
    CHECK ((request_plan_handle IS NULL) = (shared_effect_digest IS NULL)),
    CHECK (handoff_anchor IS NULL OR handoff_kind = 'request_plan'),
    CHECK (authority_source != 'agent' OR source_event IS NULL),
    CHECK ((state = 'bound') = (consuming_operation_handle IS NOT NULL)),
    CHECK ((state = 'bound') = (authority_record_id IS NOT NULL))
) STRICT;

CREATE INDEX grants_expiry_idx ON grants(state, expires_at);

CREATE TABLE review_events (
    review_handle TEXT NOT NULL REFERENCES reviews(review_handle),
    seq INTEGER NOT NULL CHECK (seq >= 1),
    from_state TEXT NOT NULL CHECK (
        from_state IN ('none', 'open', 'granted', 'denied', 'expired')
    ),
    to_state TEXT NOT NULL CHECK (
        to_state IN ('open', 'granted', 'denied', 'expired')
    ),
    event_kind TEXT NOT NULL CHECK (length(event_kind) BETWEEN 1 AND 64),
    payload_json TEXT NOT NULL CHECK (length(payload_json) BETWEEN 2 AND 1048576),
    created_at TEXT NOT NULL,
    PRIMARY KEY (review_handle, seq)
) STRICT;

CREATE TABLE grant_events (
    approval_handle TEXT NOT NULL REFERENCES grants(approval_handle),
    seq INTEGER NOT NULL CHECK (seq >= 1),
    from_state TEXT NOT NULL CHECK (
        from_state IN ('none', 'issued', 'bound', 'revoked', 'expired')
    ),
    to_state TEXT NOT NULL CHECK (
        to_state IN ('issued', 'bound', 'revoked', 'expired')
    ),
    event_kind TEXT NOT NULL CHECK (length(event_kind) BETWEEN 1 AND 64),
    payload_json TEXT NOT NULL CHECK (length(payload_json) BETWEEN 2 AND 1048576),
    created_at TEXT NOT NULL,
    PRIMARY KEY (approval_handle, seq)
) STRICT;

CREATE TABLE human_requests (
    request_handle TEXT PRIMARY KEY
        CHECK (
            length(request_handle) = 64
            AND request_handle NOT GLOB '*[^0-9a-f]*'
        ),
    review_handle TEXT NOT NULL UNIQUE REFERENCES reviews(review_handle),
    requested_by TEXT NOT NULL CHECK (length(requested_by) BETWEEN 1 AND 128),
    request_idempotency_key TEXT NOT NULL CHECK (
        length(request_idempotency_key) BETWEEN 1 AND 128
    ),
    plan_digest TEXT NOT NULL CHECK (
        length(plan_digest) = 64
        AND plan_digest NOT GLOB '*[^0-9a-f]*'
    ),
    target_seat_id TEXT NOT NULL CHECK (length(target_seat_id) BETWEEN 1 AND 128),
    operation_families TEXT NOT NULL CHECK (
        length(operation_families) BETWEEN 2 AND 4096
    ),
    redacted_summary TEXT NOT NULL CHECK (
        length(redacted_summary) BETWEEN 1 AND 4096
    ),
    nonce TEXT NOT NULL UNIQUE CHECK (
        length(nonce) = 64
        AND nonce NOT GLOB '*[^0-9a-f]*'
    ),
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'presented', 'decided', 'expired', 'withdrawn')
    ),
    provider_message_ref TEXT CHECK (
        provider_message_ref IS NULL
        OR length(provider_message_ref) BETWEEN 1 AND 32
    ),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    presented_at TEXT,
    updated_at TEXT NOT NULL,
    CHECK ((provider_message_ref IS NULL) = (presented_at IS NULL)),
    CHECK (state != 'pending' OR provider_message_ref IS NULL),
    CHECK (
        state NOT IN ('presented', 'decided')
        OR provider_message_ref IS NOT NULL
    )
) STRICT;

CREATE UNIQUE INDEX human_requests_key_idx
    ON human_requests(requested_by, request_idempotency_key);

CREATE TABLE provider_attestations (
    attestation_handle TEXT PRIMARY KEY
        CHECK (
            length(attestation_handle) = 64
            AND attestation_handle NOT GLOB '*[^0-9a-f]*'
        ),
    review_handle TEXT NOT NULL UNIQUE REFERENCES reviews(review_handle),
    request_handle TEXT NOT NULL UNIQUE
        REFERENCES human_requests(request_handle),
    decision TEXT NOT NULL CHECK (decision IN ('granted', 'denied')),
    human_subject TEXT NOT NULL CHECK (length(human_subject) BETWEEN 1 AND 128),
    idempotency_key TEXT NOT NULL CHECK (
        length(idempotency_key) BETWEEN 1 AND 128
    ),
    provider TEXT NOT NULL CHECK (provider IN ('telegram')),
    provider_user_id TEXT NOT NULL CHECK (
        length(provider_user_id) BETWEEN 1 AND 128
    ),
    provider_chat_id TEXT NOT NULL CHECK (
        length(provider_chat_id) BETWEEN 1 AND 128
    ),
    provider_message_id TEXT NOT NULL CHECK (
        length(provider_message_id) BETWEEN 1 AND 128
    ),
    callback_nonce TEXT NOT NULL UNIQUE CHECK (
        length(callback_nonce) = 64
        AND callback_nonce NOT GLOB '*[^0-9a-f]*'
    ),
    source_event TEXT NOT NULL CHECK (length(source_event) BETWEEN 1 AND 256),
    attestation_json TEXT NOT NULL CHECK (
        length(attestation_json) BETWEEN 2 AND 1048576
    ),
    created_at TEXT NOT NULL,
    UNIQUE (human_subject, idempotency_key)
) STRICT;
"""

# Literal pin of the compiled DDL text (AB-1 idiom): a drifted DDL string
# fails loudly at open() before any file is touched.
SCHEMA_DIGEST = "bf169e0452fd0d1ccc3c012afdd2f12de8acda58ae8131545e878023b03617ff"

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


_EXPECTED_OBJECTS = _expected_schema_objects(DDL)


def _fsync_dir(directory: Path) -> None:
    # Duplicated from the broker journal on purpose (L3): no import edge
    # across the authority boundary. (The broker's sibling _fsync_file is
    # migration-path-only and a fresh v1 has no migration — not carried.)
    descriptor = os.open(
        directory,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


REVIEW_COLUMNS = (
    "review_handle",
    "plan_handle",
    "plan_digest",
    "plan_owner",
    "performer",
    "target_seat_id",
    "operation_families",
    "effect_digest",
    "manifest_revision",
    "policy_revision",
    "handoff_kind",
    "request_plan_handle",
    "request_plan_digest",
    "request_plan_owner",
    "shared_effect_digest",
    "handoff_anchor",
    "requested_by",
    "state",
    "decision",
    "reason_code",
    "approver_subject",
    "authority_source",
    "audit_anchor",
    "source_event",
    "decide_idempotency_key",
    "created_at",
    "expires_at",
    "decided_at",
    "review_json",
)

GRANT_COLUMNS = (
    "approval_handle",
    "review_handle",
    "plan_handle",
    "plan_digest",
    "plan_owner",
    "performer",
    "target_seat_id",
    "operation_families",
    "effect_digest",
    "manifest_revision",
    "policy_revision",
    "handoff_kind",
    "request_plan_handle",
    "request_plan_digest",
    "request_plan_owner",
    "shared_effect_digest",
    "handoff_anchor",
    "approver_subject",
    "authority_source",
    "audit_anchor",
    "source_event",
    "decision",
    "reason_code",
    "issued_at",
    "expires_at",
    "nonce",
    "idempotency_key",
    "state",
    "consuming_operation_handle",
    "authority_record_id",
    "updated_at",
    "grant_json",
)

_SELECT_REVIEW = (
    f"SELECT {', '.join(REVIEW_COLUMNS)} FROM reviews"
)
_INSERT_REVIEW = (
    f"INSERT INTO reviews ({', '.join(REVIEW_COLUMNS)}) "
    f"VALUES ({','.join('?' * len(REVIEW_COLUMNS))})"
)
_SELECT_GRANT = (
    f"SELECT {', '.join(GRANT_COLUMNS)} FROM grants"
)
_INSERT_GRANT = (
    f"INSERT INTO grants ({', '.join(GRANT_COLUMNS)}) "
    f"VALUES ({','.join('?' * len(GRANT_COLUMNS))})"
)


HUMAN_COLUMNS = (
    "request_handle",
    "review_handle",
    "requested_by",
    "request_idempotency_key",
    "plan_digest",
    "target_seat_id",
    "operation_families",
    "redacted_summary",
    "nonce",
    "state",
    "provider_message_ref",
    "created_at",
    "expires_at",
    "presented_at",
    "updated_at",
)
_SELECT_HUMAN = f"SELECT {', '.join(HUMAN_COLUMNS)} FROM human_requests"
_INSERT_HUMAN = (
    f"INSERT INTO human_requests ({', '.join(HUMAN_COLUMNS)}) "
    f"VALUES ({','.join('?' * len(HUMAN_COLUMNS))})"
)

ATTESTATION_COLUMNS = (
    "attestation_handle",
    "review_handle",
    "request_handle",
    "decision",
    "human_subject",
    "idempotency_key",
    "provider",
    "provider_user_id",
    "provider_chat_id",
    "provider_message_id",
    "callback_nonce",
    "source_event",
    "attestation_json",
    "created_at",
)
_SELECT_ATTESTATION = (
    f"SELECT {', '.join(ATTESTATION_COLUMNS)} FROM provider_attestations"
)
_INSERT_ATTESTATION = (
    f"INSERT INTO provider_attestations ({', '.join(ATTESTATION_COLUMNS)}) "
    f"VALUES ({','.join('?' * len(ATTESTATION_COLUMNS))})"
)


def _human_row_values(record: HumanRequestRecord) -> tuple:
    return (
        record.request_handle,
        record.review_handle,
        record.requested_by,
        record.request_idempotency_key,
        record.plan_digest,
        record.target_seat_id,
        canonical_json(list(record.operation_families)),
        record.redacted_summary,
        record.nonce,
        record.state,
        record.provider_message_ref,
        record.created_at,
        record.expires_at,
        record.presented_at,
        record.updated_at,
    )


def _attestation_row_values(record: AttestationRecord) -> tuple:
    return (
        record.attestation_handle,
        record.review_handle,
        record.request_handle,
        record.decision,
        record.human_subject,
        record.idempotency_key,
        record.provider,
        record.provider_user_id,
        record.provider_chat_id,
        record.provider_message_id,
        record.callback_nonce,
        record.source_event,
        canonical_json({"plan_digest": record.plan_digest}),
        record.created_at,
    )


def _handoff_columns(record: ReviewRecord | GrantRecord) -> tuple:
    handoff = record.handoff
    if handoff is None:
        return (None, None, None, None, None)
    return (
        handoff.request_plan_handle,
        handoff.request_plan_digest,
        handoff.request_plan_owner,
        handoff.shared_effect_digest,
        handoff.handoff_anchor,
    )


def _review_row_values(record: ReviewRecord) -> tuple:
    return (
        record.review_handle,
        record.plan_handle,
        record.plan_digest,
        record.plan_owner,
        record.performer,
        record.target_seat_id,
        canonical_json(list(record.operation_families)),
        record.effect_digest,
        record.manifest_revision,
        record.policy_revision,
        record.handoff_kind,
        *_handoff_columns(record),
        record.requested_by,
        record.state,
        record.decision,
        record.reason_code,
        record.approver_subject,
        record.authority_source,
        record.audit_anchor,
        record.source_event,
        record.decide_idempotency_key,
        record.created_at,
        record.expires_at,
        record.decided_at,
        canonical_json(record.to_dict()),
    )


def _grant_row_values(record: GrantRecord) -> tuple:
    return (
        record.approval_handle,
        record.review_handle,
        record.plan_handle,
        record.plan_digest,
        record.plan_owner,
        record.performer,
        record.target_seat_id,
        canonical_json(list(record.operation_families)),
        record.effect_digest,
        record.manifest_revision,
        record.policy_revision,
        record.handoff_kind,
        *_handoff_columns(record),
        record.approver_subject,
        record.authority_source,
        record.audit_anchor,
        record.source_event,
        record.decision,
        record.reason_code,
        record.issued_at,
        record.expires_at,
        record.nonce,
        record.idempotency_key,
        record.state,
        record.consuming_operation_handle,
        record.authority_record_id,
        record.updated_at,
        canonical_json(record.to_dict()),
    )


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


class ApprovalJournal:
    """Durable, fail-closed approval truth. Never opened by the broker."""

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
    def _check_compiled_digest() -> None:
        if hashlib.sha256(DDL.encode("utf-8")).hexdigest() != SCHEMA_DIGEST:
            raise HostControlError(
                "compiled approval journal schema digest mismatch"
            )

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        busy_timeout_ms: int = BUSY_TIMEOUT_MS,
    ) -> "ApprovalJournal":
        cls._check_compiled_digest()
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
            raise HostControlError(
                "approval journal parent directory is absent"
            )
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
                raise HostControlError(
                    "refusing nonempty or partial v0 approval journal"
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
            raise HostControlError("cannot open approval journal") from exc
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

    @classmethod
    def _validate_schema(cls, connection: sqlite3.Connection) -> None:
        if (
            connection.execute("PRAGMA application_id").fetchone()[0]
            != APPLICATION_ID
            or connection.execute("PRAGMA user_version").fetchone()[0]
            != USER_VERSION
        ):
            raise HostControlError(
                "approval journal version marker mismatch"
            )
        if _schema_objects(connection) != _EXPECTED_OBJECTS:
            raise HostControlError(
                "approval journal schema object mismatch"
            )
        rows = connection.execute(
            "SELECT singleton, schema_version, schema_digest FROM journal_meta"
        ).fetchall()
        if rows != [(1, USER_VERSION, SCHEMA_DIGEST)]:
            raise HostControlError("approval journal metadata mismatch")

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
            raise HostControlError("approval journal is closed")
        return self._connection

    # ------------------------------------------------------------------
    # reviews
    # ------------------------------------------------------------------

    def put_review_or_replay(
        self,
        *,
        record: ReviewRecord,
    ) -> tuple[ReviewRecord, bool]:
        connection = self._require_open()
        if not record.validate_digest():
            raise HostControlError("invalid review record digest")
        if record.state != "open":
            raise HostControlError("new reviews must be open")
        request_handle = (
            "" if record.handoff is None
            else record.handoff.request_plan_handle
        )
        try:
            self._begin_immediate(connection, "put_review_or_replay")
            row = connection.execute(
                f"{_SELECT_REVIEW} WHERE requested_by=? AND plan_handle=? "
                "AND plan_digest=? AND handoff_kind=? "
                "AND ifnull(request_plan_handle, '')=? AND state='open'",
                (
                    record.requested_by,
                    record.plan_handle,
                    record.plan_digest,
                    record.handoff_kind,
                    request_handle,
                ),
            ).fetchone()
            if row is not None:
                stored = self._decode_review_row(row)
                connection.execute("COMMIT")
                return stored, True
            connection.execute(_INSERT_REVIEW, _review_row_values(record))
            self._insert_event(
                connection,
                table="review_events",
                handle_column="review_handle",
                handle=record.review_handle,
                from_state="none",
                to_state="open",
                event_kind="opened",
                payload={"requested_by": record.requested_by},
                created_at=record.created_at,
            )
            connection.execute("COMMIT")
            return record, False
        except sqlite3.IntegrityError as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise HostControlError(
                "review uniqueness constraint failed"
            ) from exc
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def get_review(self, review_handle: str) -> ReviewRecord:
        connection = self._require_open()
        row = connection.execute(
            f"{_SELECT_REVIEW} WHERE review_handle=?",
            (review_handle,),
        ).fetchone()
        if row is None:
            raise UnknownHandle("unknown review handle")
        return self._decode_review_row(row)

    def find_decided_by_key(
        self,
        approver_subject: str,
        decide_idempotency_key: str,
    ) -> ReviewRecord | None:
        connection = self._require_open()
        row = connection.execute(
            f"{_SELECT_REVIEW} WHERE approver_subject=? "
            "AND decide_idempotency_key=?",
            (approver_subject, decide_idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return self._decode_review_row(row)

    def record_decision(
        self,
        *,
        current: ReviewRecord,
        new: ReviewRecord,
        grant: GrantRecord | None,
        payload: dict[str, object],
    ) -> None:
        connection = self._require_open()
        self._check_review_pair(current, new)
        if new.state not in ("granted", "denied"):
            raise InvalidTransition("record_decision targets granted/denied")
        if (new.state == "granted") != (grant is not None):
            raise HostControlError(
                "a granted decision requires exactly one grant record"
            )
        if grant is not None:
            if not grant.validate_digest():
                raise HostControlError("invalid grant record digest")
            if grant.review_handle != current.review_handle:
                raise HostControlError("grant review handle mismatch")
            if grant.state != "issued":
                raise HostControlError("new grants must be issued")
        try:
            self._begin_immediate(connection, "record_decision")
            self._insert_event(
                connection,
                table="review_events",
                handle_column="review_handle",
                handle=current.review_handle,
                from_state=current.state,
                to_state=new.state,
                event_kind="decided",
                payload=payload,
                created_at=new.decided_at or new.created_at,
            )
            self._guarded_review_update(connection, current=current, new=new)
            if grant is not None:
                connection.execute(_INSERT_GRANT, _grant_row_values(grant))
                self._insert_event(
                    connection,
                    table="grant_events",
                    handle_column="approval_handle",
                    handle=grant.approval_handle,
                    from_state="none",
                    to_state="issued",
                    event_kind="issued",
                    payload={"review_handle": grant.review_handle},
                    created_at=grant.issued_at,
                )
            connection.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise HostControlError(
                "decision uniqueness constraint failed"
            ) from exc
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def transition_review(
        self,
        *,
        current: ReviewRecord,
        new: ReviewRecord,
        event_kind: str,
        payload: dict[str, object],
        created_at: str,
    ) -> None:
        connection = self._require_open()
        self._check_review_pair(current, new)
        try:
            self._begin_immediate(
                connection,
                f"transition_review[{event_kind}]",
            )
            self._insert_event(
                connection,
                table="review_events",
                handle_column="review_handle",
                handle=current.review_handle,
                from_state=current.state,
                to_state=new.state,
                event_kind=event_kind,
                payload=payload,
                created_at=created_at,
            )
            self._guarded_review_update(connection, current=current, new=new)
            connection.execute("COMMIT")
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    # ------------------------------------------------------------------
    # grants
    # ------------------------------------------------------------------

    def handoff_chain_subjects(self, request_plan_handle: str) -> frozenset[str]:
        """Subjects participating in ONE request plan's handoff chain.

        Owners, performers, and the request-plan owner across ALL reviews
        server-linked to ``request_plan_handle`` — regardless of review
        state (fail-closed, T-2911 OQ8: a denied review's participants
        stay barred; the set grows monotonically, a recorded tension).
        Read-only; scoped strictly to the one request plan (never returns
        participants from a different chain).
        """

        connection = self._require_open()
        rows = connection.execute(
            "SELECT plan_owner, performer, request_plan_owner FROM reviews "
            "WHERE request_plan_handle=?",
            (request_plan_handle,),
        ).fetchall()
        subjects: set[str] = set()
        for plan_owner, performer, request_plan_owner in rows:
            subjects.add(plan_owner)
            subjects.add(performer)
            if request_plan_owner is not None:
                subjects.add(request_plan_owner)
        return frozenset(subjects)

    def get_grant(self, approval_handle: str) -> GrantRecord:
        connection = self._require_open()
        row = connection.execute(
            f"{_SELECT_GRANT} WHERE approval_handle=?",
            (approval_handle,),
        ).fetchone()
        if row is None:
            raise UnknownHandle("unknown approval handle")
        return self._decode_grant_row(row)

    def get_grant_for_review(self, review_handle: str) -> GrantRecord | None:
        connection = self._require_open()
        row = connection.execute(
            f"{_SELECT_GRANT} WHERE review_handle=?",
            (review_handle,),
        ).fetchone()
        if row is None:
            return None
        return self._decode_grant_row(row)

    def transition_grant(
        self,
        *,
        current: GrantRecord,
        new: GrantRecord,
        event_kind: str,
        payload: dict[str, object],
    ) -> None:
        """Serialized grant transition (L7).

        BEGIN IMMEDIATE plus the state-guarded conditional UPDATE
        (``WHERE approval_handle=? AND state=?``) is the single
        serialization point for bind, revoke, AND lazy expiry: rowcount 1
        is the winner; rowcount 0 lost the race and raises
        StaleAuthorityState for the caller to re-read and classify.
        """

        connection = self._require_open()
        self._check_grant_pair(current, new)
        try:
            self._begin_immediate(
                connection,
                f"transition_grant[{event_kind}]",
            )
            self._insert_event(
                connection,
                table="grant_events",
                handle_column="approval_handle",
                handle=current.approval_handle,
                from_state=current.state,
                to_state=new.state,
                event_kind=event_kind,
                payload=payload,
                created_at=new.updated_at,
            )
            cursor = connection.execute(
                "UPDATE grants SET state=?, consuming_operation_handle=?, "
                "authority_record_id=?, updated_at=?, grant_json=? "
                "WHERE approval_handle=? AND state=?",
                (
                    new.state,
                    new.consuming_operation_handle,
                    new.authority_record_id,
                    new.updated_at,
                    canonical_json(new.to_dict()),
                    current.approval_handle,
                    current.state,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleAuthorityState("stale grant state")
            connection.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if "consuming_operation_handle" in str(exc):
                raise OperationConsumed(
                    "operation handle already consumed by another grant"
                ) from exc
            raise HostControlError(
                "grant uniqueness constraint failed"
            ) from exc
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def append_grant_event(
        self,
        *,
        current: GrantRecord,
        event_kind: str,
        payload: dict[str, object],
        created_at: str,
    ) -> None:
        """Informational same-state event (denial audit trail, P4)."""

        connection = self._require_open()
        try:
            self._begin_immediate(
                connection,
                f"append_grant_event[{event_kind}]",
            )
            row = connection.execute(
                "SELECT state FROM grants WHERE approval_handle=?",
                (current.approval_handle,),
            ).fetchone()
            if row is None:
                raise UnknownHandle("unknown approval handle")
            if row[0] != current.state:
                raise StaleAuthorityState("stale grant state for event")
            self._insert_event(
                connection,
                table="grant_events",
                handle_column="approval_handle",
                handle=current.approval_handle,
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

    # ------------------------------------------------------------------
    # human authority ingress (T-2919)
    # ------------------------------------------------------------------

    def put_human_request_or_replay(
        self,
        *,
        record: HumanRequestRecord,
    ) -> tuple[HumanRequestRecord, bool]:
        """Insert the ONE human request for a review, or return the stored
        one (replay). Same (requester, key) for a different review is an
        idempotency conflict; any second request for the same review —
        whatever its key — returns the existing record (exactly one card
        and one nonce per review, ever)."""

        connection = self._require_open()
        if record.state != "pending":
            raise HostControlError("new human requests must be pending")
        try:
            self._begin_immediate(connection, "put_human_request_or_replay")
            row = connection.execute(
                f"{_SELECT_HUMAN} WHERE requested_by=? "
                "AND request_idempotency_key=?",
                (record.requested_by, record.request_idempotency_key),
            ).fetchone()
            if row is not None:
                stored = self._decode_human_row(row)
                connection.execute("COMMIT")
                if stored.review_handle != record.review_handle:
                    raise IdempotencyConflict(
                        "human request key reused for a different review"
                    )
                return stored, True
            row = connection.execute(
                f"{_SELECT_HUMAN} WHERE review_handle=?",
                (record.review_handle,),
            ).fetchone()
            if row is not None:
                stored = self._decode_human_row(row)
                connection.execute("COMMIT")
                return stored, True
            connection.execute(_INSERT_HUMAN, _human_row_values(record))
            connection.execute("COMMIT")
            return record, False
        except sqlite3.IntegrityError as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise HostControlError(
                "human request uniqueness constraint failed"
            ) from exc
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def get_human_request(self, request_handle: str) -> HumanRequestRecord:
        connection = self._require_open()
        row = connection.execute(
            f"{_SELECT_HUMAN} WHERE request_handle=?",
            (request_handle,),
        ).fetchone()
        if row is None:
            raise UnknownHandle("unknown human request handle")
        return self._decode_human_row(row)

    def get_human_request_for_review(
        self,
        review_handle: str,
    ) -> HumanRequestRecord | None:
        connection = self._require_open()
        row = connection.execute(
            f"{_SELECT_HUMAN} WHERE review_handle=?",
            (review_handle,),
        ).fetchone()
        if row is None:
            return None
        return self._decode_human_row(row)

    def transition_human_request(
        self,
        *,
        current: HumanRequestRecord,
        new: HumanRequestRecord,
    ) -> None:
        """State-guarded human-request transition (never to ``decided``:
        nonce consumption is ``consume_human_nonce_and_decide`` only)."""

        connection = self._require_open()
        self._check_human_pair(current, new)
        if new.state == "decided":
            raise InvalidTransition(
                "decided is reachable only through nonce consumption"
            )
        try:
            self._begin_immediate(connection, "transition_human_request")
            cursor = connection.execute(
                "UPDATE human_requests SET state=?, provider_message_ref=?, "
                "presented_at=?, updated_at=? "
                "WHERE request_handle=? AND state=?",
                (
                    new.state,
                    new.provider_message_ref,
                    new.presented_at,
                    new.updated_at,
                    current.request_handle,
                    current.state,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleAuthorityState("stale human request state")
            connection.execute("COMMIT")
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def _after_human_nonce_consumed(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        """No-op seam between the nonce-consuming UPDATE and the rest of
        the decision transaction. Production never overrides it; the
        T-2919 crash test overrides it in a TEST SUBCLASS to kill the
        process mid-transaction (no production fault flag exists)."""

    def consume_human_nonce_and_decide(
        self,
        *,
        request: HumanRequestRecord,
        review_current: ReviewRecord,
        review_new: ReviewRecord,
        grant: GrantRecord | None,
        attestation: AttestationRecord,
        payload: dict[str, object],
    ) -> None:
        """ONE durable transaction (ORG-052 §6): consume the nonce
        (state-guarded ``presented -> decided`` keyed on the nonce),
        record the provider attestation (UNIQUE nonce / review / request /
        subject+key), transition the review, and issue the grant if
        granted. All or nothing: a crash anywhere before COMMIT leaves
        the nonce unconsumed and no decision; after COMMIT the decision
        stands and the nonce is never reusable."""

        connection = self._require_open()
        self._check_review_pair(review_current, review_new)
        if review_new.state not in ("granted", "denied"):
            raise InvalidTransition("human decision targets granted/denied")
        if (review_new.state == "granted") != (grant is not None):
            raise HostControlError(
                "a granted decision requires exactly one grant record"
            )
        if grant is not None:
            if not grant.validate_digest():
                raise HostControlError("invalid grant record digest")
            if grant.review_handle != review_current.review_handle:
                raise HostControlError("grant review handle mismatch")
        if (
            attestation.review_handle != review_current.review_handle
            or attestation.request_handle != request.request_handle
            or attestation.callback_nonce != request.nonce
        ):
            raise HostControlError("attestation does not match request")
        try:
            self._begin_immediate(connection, "consume_human_nonce_and_decide")
            cursor = connection.execute(
                "UPDATE human_requests SET state='decided', updated_at=? "
                "WHERE request_handle=? AND state='presented' AND nonce=?",
                (
                    attestation.created_at,
                    request.request_handle,
                    attestation.callback_nonce,  # presented nonce (defense in depth)
                ),
            )
            if cursor.rowcount != 1:
                raise StaleAuthorityState("human nonce already consumed")
            self._after_human_nonce_consumed(connection)
            connection.execute(
                _INSERT_ATTESTATION,
                _attestation_row_values(attestation),
            )
            self._insert_event(
                connection,
                table="review_events",
                handle_column="review_handle",
                handle=review_current.review_handle,
                from_state=review_current.state,
                to_state=review_new.state,
                event_kind="human_decided",
                payload=payload,
                created_at=review_new.decided_at or review_new.created_at,
            )
            self._guarded_review_update(
                connection, current=review_current, new=review_new
            )
            if grant is not None:
                connection.execute(_INSERT_GRANT, _grant_row_values(grant))
                self._insert_event(
                    connection,
                    table="grant_events",
                    handle_column="approval_handle",
                    handle=grant.approval_handle,
                    from_state="none",
                    to_state="issued",
                    event_kind="issued",
                    payload={"review_handle": grant.review_handle},
                    created_at=grant.issued_at,
                )
            connection.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise StaleAuthorityState(
                "human decision uniqueness constraint failed"
            ) from exc
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def find_attestation_by_key(
        self,
        human_subject: str,
        idempotency_key: str,
    ) -> AttestationRecord | None:
        connection = self._require_open()
        row = connection.execute(
            f"{_SELECT_ATTESTATION} WHERE human_subject=? "
            "AND idempotency_key=?",
            (human_subject, idempotency_key),
        ).fetchone()
        return None if row is None else self._decode_attestation_row(row)

    def get_attestation_for_review(
        self,
        review_handle: str,
    ) -> AttestationRecord | None:
        connection = self._require_open()
        row = connection.execute(
            f"{_SELECT_ATTESTATION} WHERE review_handle=?",
            (review_handle,),
        ).fetchone()
        return None if row is None else self._decode_attestation_row(row)

    @staticmethod
    def _check_human_pair(
        current: HumanRequestRecord,
        new: HumanRequestRecord,
    ) -> None:
        if current.request_handle != new.request_handle:
            raise HostControlError("human request handle mismatch")
        immutable = (
            "review_handle",
            "requested_by",
            "request_idempotency_key",
            "plan_digest",
            "target_seat_id",
            "operation_families",
            "redacted_summary",
            "nonce",
            "created_at",
            "expires_at",
        )
        for name in immutable:
            if getattr(current, name) != getattr(new, name):
                raise HostControlError("human request immutable field changed")
        if new.state not in HUMAN_REQUEST_TRANSITIONS[current.state]:
            raise InvalidTransition(
                f"illegal human request transition {current.state} -> "
                f"{new.state}"
            )

    @staticmethod
    def _decode_human_row(row: tuple[object, ...]) -> HumanRequestRecord:
        values = dict(zip(HUMAN_COLUMNS, row))
        try:
            families = json.loads(values["operation_families"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise HostControlError("corrupt human request families") from exc
        if not isinstance(families, list):
            raise HostControlError("corrupt human request families")
        values["operation_families"] = tuple(families)
        return HumanRequestRecord(**values)

    @staticmethod
    def _decode_attestation_row(row: tuple[object, ...]) -> AttestationRecord:
        values = dict(zip(ATTESTATION_COLUMNS, row))
        raw = values.pop("attestation_json")
        try:
            body = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise HostControlError("corrupt attestation JSON") from exc
        if not isinstance(body, dict) or set(body) != {"plan_digest"}:
            raise HostControlError("corrupt attestation shape")
        return AttestationRecord(plan_digest=body["plan_digest"], **values)

    # ------------------------------------------------------------------
    # maintenance / monitor
    # ------------------------------------------------------------------

    def pending_human_request_handles(
        self,
        now_iso: str,
        *,
        limit: int,
    ) -> tuple[str, ...]:
        """Still-undelivered, unexpired human requests (T-5518: the
        service re-arms their ``Present`` on start). Read-only."""

        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise HostControlError("invalid pending human request limit")
        connection = self._require_open()
        rows = connection.execute(
            "SELECT request_handle FROM human_requests "
            "WHERE state='pending' AND expires_at > ? "
            "ORDER BY created_at, request_handle LIMIT ?",
            (now_iso, limit),
        ).fetchall()
        return tuple(row[0] for row in rows)

    def due_for_expiry(
        self,
        now_iso: str,
        *,
        limit: int,
    ) -> tuple[tuple[str, str], ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise HostControlError("invalid expiry sweep limit")
        connection = self._require_open()
        rows = connection.execute(
            "SELECT 'review' AS kind, review_handle AS handle, expires_at "
            "FROM reviews WHERE state='open' AND expires_at <= ? "
            "UNION ALL "
            "SELECT 'grant' AS kind, approval_handle AS handle, expires_at "
            "FROM grants WHERE state='issued' AND expires_at <= ? "
            "ORDER BY expires_at, handle LIMIT ?",
            (now_iso, now_iso, limit),
        ).fetchall()
        return tuple((row[0], row[1]) for row in rows)

    def counts(self) -> ApprovalCounts:
        connection = self._require_open()
        open_reviews = connection.execute(
            "SELECT count(*) FROM reviews WHERE state='open'"
        ).fetchone()[0]
        issued = connection.execute(
            "SELECT count(*) FROM grants WHERE state='issued'"
        ).fetchone()[0]
        bound = connection.execute(
            "SELECT count(*) FROM grants WHERE state='bound'"
        ).fetchone()[0]
        return ApprovalCounts(
            open_reviews=open_reviews,
            issued_grants=issued,
            bound_grants=bound,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @staticmethod
    def _check_review_pair(current: ReviewRecord, new: ReviewRecord) -> None:
        if not current.validate_digest() or not new.validate_digest():
            raise HostControlError("invalid review record digest")
        if current.review_handle != new.review_handle:
            raise HostControlError("review handle mismatch")
        if new.state not in REVIEW_TRANSITIONS[current.state]:
            raise InvalidTransition(
                f"illegal review transition {current.state} -> {new.state}"
            )

    @staticmethod
    def _check_grant_pair(current: GrantRecord, new: GrantRecord) -> None:
        if not current.validate_digest() or not new.validate_digest():
            raise HostControlError("invalid grant record digest")
        if current.approval_handle != new.approval_handle:
            raise HostControlError("approval handle mismatch")
        if new.state not in GRANT_TRANSITIONS[current.state]:
            raise InvalidTransition(
                f"illegal grant transition {current.state} -> {new.state}"
            )

    @staticmethod
    def _guarded_review_update(
        connection: sqlite3.Connection,
        *,
        current: ReviewRecord,
        new: ReviewRecord,
    ) -> None:
        cursor = connection.execute(
            "UPDATE reviews SET state=?, decision=?, reason_code=?, "
            "approver_subject=?, authority_source=?, audit_anchor=?, "
            "source_event=?, decide_idempotency_key=?, decided_at=?, "
            "review_json=? WHERE review_handle=? AND state=?",
            (
                new.state,
                new.decision,
                new.reason_code,
                new.approver_subject,
                new.authority_source,
                new.audit_anchor,
                new.source_event,
                new.decide_idempotency_key,
                new.decided_at,
                canonical_json(new.to_dict()),
                current.review_handle,
                current.state,
            ),
        )
        if cursor.rowcount != 1:
            raise StaleAuthorityState("stale review state")

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        *,
        table: str,
        handle_column: str,
        handle: str,
        from_state: str,
        to_state: str,
        event_kind: str,
        payload: dict[str, object],
        created_at: str,
    ) -> None:
        if table not in ("review_events", "grant_events"):
            raise HostControlError("invalid event table")
        seq = connection.execute(
            f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {table} "
            f"WHERE {handle_column}=?",
            (handle,),
        ).fetchone()[0]
        connection.execute(
            f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?)",
            (
                handle,
                seq,
                from_state,
                to_state,
                event_kind,
                canonical_json(payload),
                created_at,
            ),
        )

    @staticmethod
    def _decode_review_row(row: tuple[object, ...]) -> ReviewRecord:
        try:
            raw = json.loads(row[-1])
        except (TypeError, json.JSONDecodeError) as exc:
            raise HostControlError("corrupt journal review JSON") from exc
        if not isinstance(raw, dict):
            raise HostControlError("corrupt journal review shape")
        record = ReviewRecord.from_dict(raw)
        if not record.validate_digest():
            raise HostControlError("corrupt journal review digest")
        if tuple(row) != _review_row_values(record):
            raise HostControlError(
                "journal row does not match review record"
            )
        return record

    @staticmethod
    def _decode_grant_row(row: tuple[object, ...]) -> GrantRecord:
        try:
            raw = json.loads(row[-1])
        except (TypeError, json.JSONDecodeError) as exc:
            raise HostControlError("corrupt journal grant JSON") from exc
        if not isinstance(raw, dict):
            raise HostControlError("corrupt journal grant shape")
        record = GrantRecord.from_dict(raw)
        if not record.validate_digest():
            raise HostControlError("corrupt journal grant digest")
        if tuple(row) != _grant_row_values(record):
            raise HostControlError(
                "journal row does not match grant record"
            )
        return record


