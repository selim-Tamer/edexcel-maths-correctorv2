"""Dev-only PRIVATE store for uploaded source documents (PDF bytes).

Why this exists: the minimum secure workflow needs a teacher's synthetic PDF
reachable server-side by the durable marking job without any public URL.
This module is that adapter - nothing more.

POSTURE (honest): local-dev storage only, behind the explicit opt-in env pair
``AI_MARKER_DOCUMENT_DB`` + ``AI_MARKER_DOCUMENT_DEV_ONLY=1`` - labelled NOT
school storage, same discipline as the audit/ingest/membership stores, and
consolidated into the approved (Supabase) private bucket later. There is no
anonymous route, no signed URL and no presigned-URL minting here: every read
requires the caller's verified owner + active tenant scope, so the bytes are
reachable only through the API's normal authenticated path.

Storage rules:
* Rows are keyed by ``(tenant_id, owner_subject, document_digest)`` - the same
  content may exist for another tenant as a separate row, so a stream is never
  REBOUND across tenants (the ingest-registry lesson, applied from the start).
* The digest is the sha256 of the bytes, computed server-side on upload; a
  repeat upload of identical content is idempotent (returns the stored row).
* Bytes live only in the ``content`` BLOB: never in envelopes, logs or error
  messages, and no token is ever stored.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import closing

PATH_ENV = "AI_MARKER_DOCUMENT_DB"
ACK_ENV = "AI_MARKER_DOCUMENT_DEV_ONLY"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    tenant_id       TEXT NOT NULL,
    owner_subject   TEXT NOT NULL,
    document_digest TEXT NOT NULL,
    filename        TEXT NOT NULL DEFAULT '',
    page_count      INTEGER,
    size_bytes      INTEGER NOT NULL,
    content         BLOB NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (tenant_id, owner_subject, document_digest)
)
"""


class DocumentStoreError(Exception):
    """Fail-closed store failure with a static, envelope-safe detail."""

    def __init__(self, detail: str):
        self.detail = str(detail)
        super().__init__(self.detail)


def open_store(path: str) -> sqlite3.Connection:
    if not isinstance(path, str) or not path.strip():
        raise DocumentStoreError("store_unusable")
    try:
        connection = sqlite3.connect(path, timeout=10)
        connection.execute(_SCHEMA)
        connection.commit()
    except sqlite3.Error:
        raise DocumentStoreError("store_unusable")
    return connection


def require_connection() -> sqlite3.Connection:
    """Open the document store per env, failing closed when unconfigured."""
    path = (os.environ.get(PATH_ENV) or "").strip()
    acknowledged = (os.environ.get(ACK_ENV) or "").strip() == "1"
    if not path or not acknowledged:
        raise DocumentStoreError("dev_opt_in_required")
    # One connection per request (same reasoning as ingest_registry): no
    # cross-thread reuse inside FastAPI's worker model.
    return open_store(path)


def put_document(connection: sqlite3.Connection, *, tenant_id: str,
                 owner: str, document_digest: str, filename: str,
                 page_count, content: bytes) -> bool:
    """Store one document idempotently. Returns True when newly stored.

    A repeat upload of the same content for the same owner+tenant returns
    False (already present) without touching the stored bytes - the stored
    original is authoritative. A size mismatch on an existing key means the
    store itself is inconsistent, so it fails closed instead of guessing.
    """
    payload = bytes(content)
    try:
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute(
            "SELECT size_bytes FROM documents WHERE tenant_id = ?"
            " AND owner_subject = ? AND document_digest = ?",
            (tenant_id, owner, document_digest)).fetchone()
        if prior is not None:
            connection.commit()
            if int(prior[0]) != len(payload):
                raise DocumentStoreError("store_unusable")
            return False
        connection.execute(
            "INSERT INTO documents (tenant_id, owner_subject, document_digest,"
            " filename, page_count, size_bytes, content, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, owner, document_digest, str(filename or ""),
             page_count, len(payload), payload, f"{time.time():.0f}"))
        connection.commit()
        return True
    except DocumentStoreError:
        connection.rollback()
        raise
    except sqlite3.Error as error:
        connection.rollback()
        raise DocumentStoreError("store_unusable") from error


def find_document(connection: sqlite3.Connection, *, tenant_id: str,
                  owner: str, document_digest: str):
    """The stored bytes for this exact owner+tenant, or None (never leaked)."""
    try:
        row = connection.execute(
            "SELECT content FROM documents WHERE tenant_id = ?"
            " AND owner_subject = ? AND document_digest = ?",
            (tenant_id, owner, document_digest)).fetchone()
    except sqlite3.Error as error:
        raise DocumentStoreError("store_unusable") from error
    if row is None:
        return None
    return bytes(row[0])


def close_quietly(connection) -> None:
    if connection is not None:
        try:
            connection.close()
        except Exception:
            pass
