"""Dev-only registry of INGESTED results (the API's default row provider).

Why this exists: ``POST /v1/results`` registers an immutable original in the
audit store, but every read/write path in this API resolves results through
the owner-scoped *row* visibility rule (``rows_for``). An ingested result
needs a row or its own history would be unreachable — exactly the O-1
consistency rule. This registry supplies that row.

POSTURE (honest): local-dev storage only (explicit opt-in env pair
``AI_MARKER_INGEST_DB`` + ``AI_MARKER_INGEST_DEV_ONLY=1``), same discipline
as the audit/membership stores; labelled NOT school storage. It holds what a
Streamlit session already holds — the full original output needed to render
a review — and is consolidated into the approved (B) persistence store later.
Tenant-filtered reads and a permanent owner/result-to-tenant binding prevent
rebinding development audit streams across tenants. Never stores tokens.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing

from typing import Optional

PATH_ENV = "AI_MARKER_INGEST_DB"
ACK_ENV = "AI_MARKER_INGEST_DEV_ONLY"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingest_rows (
    tenant_id       TEXT NOT NULL,
    owner_subject   TEXT NOT NULL,
    result_id       TEXT NOT NULL,
    scope           TEXT NOT NULL,
    review_key      TEXT NOT NULL,
    document_digest TEXT NOT NULL DEFAULT '',
    row_json        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (owner_subject, result_id)
)
"""


class RegistryError(Exception):
    """Fail-closed registry failure with a static, envelope-safe detail."""

    def __init__(self, detail: str):
        self.detail = str(detail)
        super().__init__(self.detail)


def open_store(path: str) -> sqlite3.Connection:
    if not isinstance(path, str) or not path.strip():
        raise RegistryError("store_unusable")
    try:
        connection = sqlite3.connect(path, timeout=10)
        connection.execute(_SCHEMA)
        connection.commit()
    except sqlite3.Error:
        raise RegistryError("store_unusable")
    return connection


def put_row(connection: sqlite3.Connection, *, tenant_id: str, owner: str, result_id: str,
            scope: str, review_key: str, document_digest: str,
            row: dict) -> None:
    """Bind a stream permanently to its first tenant and immutable row.

    The SQLite audit store has no tenant column. A global owner/result key
    here deliberately forbids rebinding that audit stream to a second tenant.
    PostgreSQL consolidation is still required for the production path.
    """
    payload = json.dumps(row, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    try:
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute(
            "SELECT tenant_id, row_json FROM ingest_rows"
            " WHERE owner_subject = ? AND result_id = ?",
            (owner, result_id)).fetchone()
        if prior is not None:
            if prior[0] != tenant_id:
                raise RegistryError("result_not_registered")
            if prior[1] != payload:
                raise RegistryError("original_output_immutable")
        else:
            connection.execute(
                "INSERT INTO ingest_rows (tenant_id, owner_subject, result_id,"
                " scope, review_key, document_digest, row_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tenant_id, owner, result_id, scope, review_key, document_digest,
                 payload, f"{time.time():.0f}"))
        connection.commit()
    except (sqlite3.Error, RegistryError) as error:
        connection.rollback()
        if isinstance(error, RegistryError):
            raise
        raise RegistryError("store_unusable") from error


def find_rows(connection: sqlite3.Connection, owner: str, tenant_id: str) -> list:
    """Every visible row for one owner (the rows_for shape)."""
    try:
        rows = connection.execute(
            "SELECT row_json FROM ingest_rows WHERE owner_subject = ?"
            " AND tenant_id = ? ORDER BY created_at", (owner, tenant_id)).fetchall()
    except sqlite3.Error as error:
        raise RegistryError("store_unusable") from error
    out = []
    for (payload,) in rows:
        try:
            row = json.loads(payload)
        except (TypeError, ValueError):
            continue  # a corrupt row is skipped, never served
        if isinstance(row, dict):
            out.append(row)
    return out


def require_connection() -> sqlite3.Connection:
    """Open (or reuse) the registry per env, failing closed when unconfigured."""
    path = (os.environ.get(PATH_ENV) or "").strip()
    acknowledged = (os.environ.get(ACK_ENV) or "").strip() == "1"
    if not path or not acknowledged:
        raise RegistryError("store_unusable")
    # Connections belong to one request/thread. Reusing a global SQLite
    # connection across FastAPI workers causes cross-thread failures.
    return open_store(path)


def default_provider():
    """Tenant- and owner-scoped development rows, or an empty fail-closed set."""
    def rows_for(subject: str, owner: str, tenant_id: str) -> list:
        if not isinstance(subject, str) or not subject.strip():
            return []
        try:
            with closing(require_connection()) as connection:
                return find_rows(connection, owner, tenant_id)
        except RegistryError:
            return []
    return rows_for


def close_quietly(connection) -> None:
    if connection is not None:
        try:
            connection.close()
        except Exception:
            pass
