"""The ingress's OWN private durable state (ORG-052 §6, T-2919).

"The Telegram ingress keeps only the provider cursor, pending callback
correlation, and redacted delivery evidence needed to validate its source
event." One SQLite file under the ingress's private state directory
(mode 0600, WAL, synchronous=FULL). It is NOT an authority journal: the
approval journal is authoritative; this file only correlates cards to
(chat id, message id), dedupes provider callbacks, and carries decision
intents across a crash so a replay reuses the same idempotency key.

Shares no path, cursor, or file with any agent-facing Telegram
integration (gate 14).
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

STATE_FILE = "ingress-state.sqlite"
_APPLICATION_ID = 1346917961  # "PHTI": Pantheon Host Telegram Ingress

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    cursor INTEGER NOT NULL CHECK (cursor >= 0)
) STRICT;
INSERT OR IGNORE INTO meta (singleton, cursor) VALUES (1, 0);
CREATE TABLE IF NOT EXISTS cards (
    review_handle TEXT PRIMARY KEY,
    nonce TEXT NOT NULL UNIQUE,
    plan_digest TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('sending', 'open', 'closed')),
    chat_id INTEGER,
    message_id INTEGER,
    text_sha256 TEXT,
    closed_reason TEXT,
    UNIQUE (chat_id, message_id)
) STRICT;
CREATE TABLE IF NOT EXISTS seen_callbacks (
    callback_id TEXT PRIMARY KEY,
    outcome TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS intents (
    idempotency_key TEXT PRIMARY KEY,
    review_handle TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('granted', 'denied')),
    source_event TEXT NOT NULL,
    callback_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'done')),
    outcome TEXT
) STRICT;
"""


@dataclass(frozen=True)
class Card:
    review_handle: str
    nonce: str
    plan_digest: str
    expires_at: str
    state: str
    chat_id: int | None
    message_id: int | None
    text_sha256: str | None
    closed_reason: str | None


@dataclass(frozen=True)
class Intent:
    idempotency_key: str
    review_handle: str
    decision: str
    source_event: str
    callback_id: str
    state: str
    outcome: str | None


_CARD_COLUMNS = (
    "review_handle, nonce, plan_digest, expires_at, state, chat_id, "
    "message_id, text_sha256, closed_reason"
)
_INTENT_COLUMNS = (
    "idempotency_key, review_handle, decision, source_event, callback_id, "
    "state, outcome"
)


class IngressState:
    def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
        self._connection = connection
        self.path = path

    @classmethod
    def open(cls, state_dir: Path) -> "IngressState":
        if not state_dir.is_dir():
            raise OSError("ingress state directory is absent")
        path = state_dir / STATE_FILE
        # create 0600 before sqlite touches it (no umask window)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        connection = sqlite3.connect(path, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        if application_id not in (0, _APPLICATION_ID):
            connection.close()
            raise OSError("refusing a foreign state file")
        connection.executescript(
            f"BEGIN IMMEDIATE;{_DDL}"
            f"PRAGMA application_id={_APPLICATION_ID};COMMIT;"
        )
        return cls(connection, path)

    def close(self) -> None:
        self._connection.close()

    # -- transactions ---------------------------------------------------

    def begin(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self._connection.execute("COMMIT")

    def rollback(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    # -- cursor ---------------------------------------------------------

    def cursor(self) -> int:
        return int(
            self._connection.execute(
                "SELECT cursor FROM meta WHERE singleton=1"
            ).fetchone()[0]
        )

    def set_cursor(self, value: int) -> None:
        # monotonic: a stale/duplicate update can never move it backwards
        self._connection.execute(
            "UPDATE meta SET cursor=max(cursor, ?) WHERE singleton=1",
            (int(value),),
        )

    # -- cards ----------------------------------------------------------

    def card_for_review(self, review_handle: str) -> Card | None:
        row = self._connection.execute(
            f"SELECT {_CARD_COLUMNS} FROM cards WHERE review_handle=?",
            (review_handle,),
        ).fetchone()
        return None if row is None else Card(*row)

    def card_for_message(self, chat_id: int, message_id: int) -> Card | None:
        row = self._connection.execute(
            f"SELECT {_CARD_COLUMNS} FROM cards WHERE chat_id=? AND message_id=?",
            (int(chat_id), int(message_id)),
        ).fetchone()
        return None if row is None else Card(*row)

    def insert_sending_card(
        self,
        review_handle: str,
        nonce: str,
        plan_digest: str,
        expires_at: str,
    ) -> None:
        self._connection.execute(
            "INSERT INTO cards (review_handle, nonce, plan_digest, expires_at, "
            "state) VALUES (?,?,?,?, 'sending')",
            (review_handle, nonce, plan_digest, expires_at),
        )

    def mark_card_open(
        self,
        review_handle: str,
        chat_id: int,
        message_id: int,
        text_sha256: str,
    ) -> None:
        self._connection.execute(
            "UPDATE cards SET state='open', chat_id=?, message_id=?, "
            "text_sha256=? WHERE review_handle=? AND state='sending'",
            (int(chat_id), int(message_id), text_sha256, review_handle),
        )

    def close_card(self, review_handle: str, reason: str) -> None:
        self._connection.execute(
            "UPDATE cards SET state='closed', closed_reason=? "
            "WHERE review_handle=? AND state != 'closed'",
            (reason, review_handle),
        )

    def set_closed_reason(self, review_handle: str, reason: str) -> None:
        """Record the FINAL reason on an already-closed card (a decision
        finished): only 'decision_pending' cards accept an identical
        Present replay, so a decided card must not keep that reason."""

        self._connection.execute(
            "UPDATE cards SET closed_reason=? WHERE review_handle=? "
            "AND state='closed'",
            (reason, review_handle),
        )

    def open_cards(self) -> list[Card]:
        rows = self._connection.execute(
            f"SELECT {_CARD_COLUMNS} FROM cards WHERE state='open'"
        ).fetchall()
        return [Card(*row) for row in rows]

    # -- callbacks / intents ---------------------------------------------

    def seen(self, callback_id: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM seen_callbacks WHERE callback_id=?",
                (callback_id,),
            ).fetchone()
            is not None
        )

    def mark_seen(self, callback_id: str, outcome: str) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO seen_callbacks (callback_id, outcome) "
            "VALUES (?, ?)",
            (callback_id, outcome),
        )

    def insert_intent(self, intent: Intent) -> None:
        self._connection.execute(
            f"INSERT INTO intents ({_INTENT_COLUMNS}) VALUES (?,?,?,?,?,?,?)",
            (
                intent.idempotency_key,
                intent.review_handle,
                intent.decision,
                intent.source_event,
                intent.callback_id,
                intent.state,
                intent.outcome,
            ),
        )

    def finish_intent(self, idempotency_key: str, outcome: str) -> bool:
        """pending -> done, ONCE. Returns whether this call finished it;
        an intent already done is never overwritten (T-5518: a late or
        duplicate async completion is a no-op)."""

        cursor = self._connection.execute(
            "UPDATE intents SET state='done', outcome=? "
            "WHERE idempotency_key=? AND state='pending'",
            (outcome, idempotency_key),
        )
        return cursor.rowcount == 1

    def intent(self, idempotency_key: str) -> Intent | None:
        row = self._connection.execute(
            f"SELECT {_INTENT_COLUMNS} FROM intents WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        return None if row is None else Intent(*row)

    def pending_intents(self) -> list[Intent]:
        rows = self._connection.execute(
            f"SELECT {_INTENT_COLUMNS} FROM intents WHERE state='pending' "
            "ORDER BY rowid"
        ).fetchall()
        return [Intent(*row) for row in rows]

    def intents(self) -> list[Intent]:
        rows = self._connection.execute(
            f"SELECT {_INTENT_COLUMNS} FROM intents ORDER BY rowid"
        ).fetchall()
        return [Intent(*row) for row in rows]
