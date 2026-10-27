"""Server-backed teacher assignments (development store + shared contract).

WHY THIS EXISTS
---------------
The frontend's assignment list used to be device-local UI state. The beta now
needs an assignment to survive a reload and a second device, without the
client choosing any identity. This module is the DEV half (SQLite, explicit
opt-in) of the same contract ``postgres_assignment_store`` serves in
production:

* the identity is ``(tenant_id, owner_subject)`` derived from the VERIFIED
  token by ``api/app.py`` - never from the body, never from a header;
* ``assignment_id`` is minted here, server-side, and the client only sends an
  ``idempotency_key``. Replaying the same key with the same title returns the
  SAME assignment; reusing the key for a different title is refused with
  ``idempotency_key_payload_mismatch`` (an idempotency key is not a rename
  API);
* there is no DELETE in this slice: rows are history, retention belongs to
  the separate elevated role, and no route exposes it.

Schema (fresh, additive; no other table is touched):

    assignments(tenant_id, owner_subject, assignment_id, idempotency_key,
                title, created_at)
    PRIMARY KEY (tenant_id, owner_subject, assignment_id)
    UNIQUE      (tenant_id, owner_subject, idempotency_key)

Honest limit: SQLite here is NOT school storage. Production uses
``schema/postgres_v7_assignments.sql`` through the Postgres adapter; applying
that migration is a separate, explicitly authorised operator step.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import time

PATH_ENV = "AI_MARKER_ASSIGNMENT_DB"
ACK_ENV = "AI_MARKER_ASSIGNMENT_DEV_ONLY"

TITLE_MAX_CHARS = 200
IDEMPOTENCY_KEY_MAX_CHARS = 128
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


class AssignmentStoreError(Exception):
    """Fail-closed assignment-store failure with an envelope-safe detail."""

    def __init__(self, detail: str):
        self.detail = str(detail)
        super().__init__(self.detail)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS assignments (
    tenant_id       TEXT NOT NULL,
    owner_subject   TEXT NOT NULL,
    assignment_id   TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    title           TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, owner_subject, assignment_id),
    UNIQUE (tenant_id, owner_subject, idempotency_key)
)
"""

_RETURN_KEYS = ("assignment_id", "title", "created_at")


def open_store(path: str) -> sqlite3.Connection:
    if not isinstance(path, str) or not path.strip():
        raise AssignmentStoreError("store_unusable")
    try:
        connection = sqlite3.connect(path, timeout=10)
        connection.execute(_SCHEMA)
        connection.commit()
    except sqlite3.Error:
        raise AssignmentStoreError("store_unusable")
    return connection


def require_connection() -> sqlite3.Connection:
    """Open the dev store per env, failing closed when unconfigured."""
    path = (os.environ.get(PATH_ENV) or "").strip()
    acknowledged = (os.environ.get(ACK_ENV) or "").strip() == "1"
    if not path or not acknowledged:
        raise AssignmentStoreError("dev_opt_in_required")
    return open_store(path)


def mint_assignment_id() -> str:
    """Server-minted opaque identity: the client never chooses or predicts it."""
    return "asg_" + secrets.token_hex(12)


def _row_to_assignment(row) -> dict:
    return dict(zip(_RETURN_KEYS, row))


def normalize_title(value) -> str:
    """Trim and validate a title; raises ``payload_invalid`` on refusal."""
    if not isinstance(value, str):
        raise AssignmentStoreError("payload_invalid")
    title = value.strip()
    if not title or len(title) > TITLE_MAX_CHARS:
        raise AssignmentStoreError("payload_invalid")
    return title


def normalize_idempotency_key(value) -> str:
    if not isinstance(value, str):
        raise AssignmentStoreError("payload_invalid")
    key = value.strip()
    if (not key or len(key) > IDEMPOTENCY_KEY_MAX_CHARS
            or "/" in key or any(ord(c) < 32 for c in key)):
        raise AssignmentStoreError("payload_invalid")
    return key


def _replay(prior, title: str) -> dict:
    if prior[1] != title:
        raise AssignmentStoreError("idempotency_key_payload_mismatch")
    return {"assignment_id": prior[0], "title": prior[1],
            "created_at": int(prior[2]), "created": False}


def _lookup(connection: sqlite3.Connection, tenant_id: str, owner: str,
            idempotency_key: str):
    return connection.execute(
        "SELECT assignment_id, title, created_at FROM assignments"
        " WHERE tenant_id = ? AND owner_subject = ?"
        " AND idempotency_key = ?",
        (tenant_id, owner, idempotency_key)).fetchone()


def create_assignment(connection: sqlite3.Connection, *, tenant_id: str,
                      owner: str, idempotency_key: str, title: str) -> dict:
    """Create (or replay) one assignment. Returns the row plus ``created``.

    Same shape as the PostgreSQL adapter: ``ON CONFLICT DO NOTHING`` plus a
    re-read, so a same-key race is a replay/mismatch - never a raw constraint
    error surfaced as ``store_unusable``. ``BEGIN IMMEDIATE`` already
    serialises writers; the conflict clause keeps the semantics explicit.
    """
    now = int(time.time())
    try:
        connection.execute("BEGIN IMMEDIATE")
        prior = _lookup(connection, tenant_id, owner, idempotency_key)
        if prior is not None:
            connection.commit()
            return _replay(prior, title)
        assignment_id = mint_assignment_id()
        inserted = connection.execute(
            "INSERT INTO assignments (tenant_id, owner_subject, assignment_id,"
            " idempotency_key, title, created_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (tenant_id, owner_subject, idempotency_key)"
            " DO NOTHING",
            (tenant_id, owner, assignment_id, idempotency_key, title, now))
        if inserted.rowcount == 0:
            prior = _lookup(connection, tenant_id, owner, idempotency_key)
            connection.commit()
            if prior is None:
                raise AssignmentStoreError("store_unusable")
            return _replay(prior, title)
        connection.commit()
    except AssignmentStoreError:
        connection.rollback()
        raise
    except sqlite3.Error as error:
        connection.rollback()
        raise AssignmentStoreError("store_unusable") from error
    return {"assignment_id": assignment_id, "title": title,
            "created_at": now, "created": True}


def get_assignment(connection: sqlite3.Connection, *, tenant_id: str,
                   owner: str, assignment_id: str):
    try:
        row = connection.execute(
            "SELECT assignment_id, title, created_at FROM assignments"
            " WHERE tenant_id = ? AND owner_subject = ?"
            " AND assignment_id = ?",
            (tenant_id, owner, assignment_id)).fetchone()
    except sqlite3.Error as error:
        raise AssignmentStoreError("store_unusable") from error
    return _row_to_assignment(row) if row is not None else None


def list_assignments(connection: sqlite3.Connection, *, tenant_id: str,
                     owner: str, limit: int = DEFAULT_PAGE_SIZE,
                     after_created_at: int = 0,
                     after_assignment_id: str = ""):
    """Newest-first keyset page for exactly this owner+tenant."""
    limit = int(limit)
    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise AssignmentStoreError("payload_invalid")
    try:
        if after_assignment_id:
            rows = connection.execute(
                "SELECT assignment_id, title, created_at FROM assignments"
                " WHERE tenant_id = ? AND owner_subject = ?"
                " AND (created_at < ? OR"
                " (created_at = ? AND assignment_id < ?))"
                " ORDER BY created_at DESC, assignment_id DESC LIMIT ?",
                (tenant_id, owner, int(after_created_at),
                 int(after_created_at), after_assignment_id,
                 limit)).fetchall()
        else:
            rows = connection.execute(
                "SELECT assignment_id, title, created_at FROM assignments"
                " WHERE tenant_id = ? AND owner_subject = ?"
                " ORDER BY created_at DESC, assignment_id DESC LIMIT ?",
                (tenant_id, owner, limit)).fetchall()
    except sqlite3.Error as error:
        raise AssignmentStoreError("store_unusable") from error
    return [_row_to_assignment(row) for row in rows]
