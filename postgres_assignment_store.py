"""PostgreSQL port of ``assignment_store``: the SAME server-owned semantics.

Production assignments must survive process restarts and Cloud Run scale-down
on the managed database. This adapter follows the established Postgres
discipline (``postgres_job_store``):

* **No DDL here.** It fails closed unless ``schema/postgres_v7_assignments.sql``
  was applied by an explicit operator step (``assignments_version = '1'``).
* **RLS + least privilege.** ``assignments`` carries the same
  ``tenant_isolation`` policy; the adapter binds ``app.tenant_id``
  transaction-locally from its OWN argument, and every statement also carries
  an explicit ``tenant_id``/``owner_subject`` predicate. ``app.tenant_id`` is
  a policy input, not an authenticity proof: a compromised database
  credential can set it, so identity remains a server-side token decision.
* **No DELETE.** Rows are history; retention is the separate elevated role.
* **``prepare_threshold=None``** for the Supabase transaction pooler, exactly
  like the other adapters. The real pooler remains unverified.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from assignment_store import (AssignmentStoreError, IDEMPOTENCY_KEY_MAX_CHARS,
                              MAX_PAGE_SIZE, mint_assignment_id)
from runtime_config import PG_DSN_ENV

TENANT_GUC = "app.tenant_id"

_RETURN_COLUMNS = "assignment_id, title, created_at"


def _bind_tenant(connection, tenant_id: str) -> None:
    connection.execute("SELECT set_config(%s, %s, true)",
                       (TENANT_GUC, tenant_id))


def _row_to_assignment(row) -> dict:
    return {"assignment_id": row[0], "title": row[1],
            "created_at": int(row[2])}


def open_store(dsn: str):
    """Connect and validate (never create) the 0007 assignment schema."""
    import psycopg  # lazy: only the Postgres path needs the driver

    if not isinstance(dsn, str) or not dsn.strip():
        raise AssignmentStoreError("store_unusable")
    try:
        connection = psycopg.connect(dsn.strip(), prepare_threshold=None)
    except Exception as error:
        raise AssignmentStoreError("store_unusable") from error
    try:
        with connection.transaction():
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'assignments_version'"
            ).fetchone()
            present = connection.execute(
                "SELECT to_regclass('public.assignments')").fetchone()
    except Exception as error:
        try:
            connection.close()
        except Exception:
            pass
        raise AssignmentStoreError("store_unusable") from error
    if row is None or str(row[0]) != "1" or present is None or present[0] is None:
        try:
            connection.close()
        except Exception:
            pass
        raise AssignmentStoreError("store_unusable")
    return connection


def configured() -> bool:
    return bool((os.environ.get(PG_DSN_ENV) or "").strip())


def require_connection():
    dsn = (os.environ.get(PG_DSN_ENV) or "").strip()
    if not dsn:
        raise AssignmentStoreError("store_unusable")
    return open_store(dsn)


def _replay(prior, title: str) -> dict:
    if prior[1] != title:
        raise AssignmentStoreError("idempotency_key_payload_mismatch")
    payload = _row_to_assignment(prior)
    payload["created"] = False
    return payload


def _lookup(connection, tenant_id: str, owner: str, idempotency_key: str):
    return connection.execute(
        "SELECT " + _RETURN_COLUMNS + " FROM assignments"
        " WHERE tenant_id = %s AND owner_subject = %s"
        " AND idempotency_key = %s",
        (tenant_id, owner, idempotency_key)).fetchone()


def create_assignment(connection, *, tenant_id: str, owner: str,
                      idempotency_key: str, title: str) -> dict:
    """Create or replay one assignment, safe under a same-key race.

    A concurrent request with the same key is expected, not exceptional: the
    INSERT uses ``ON CONFLICT DO NOTHING`` and, when it lost the race, the
    winner's committed row is re-read and compared. A unique violation must
    never surface as ``store_unusable`` (IR finding).
    """
    if (not isinstance(idempotency_key, str) or not idempotency_key
            or len(idempotency_key) > IDEMPOTENCY_KEY_MAX_CHARS):
        raise AssignmentStoreError("payload_invalid")
    now = int(time.time())
    try:
        with connection.transaction():
            _bind_tenant(connection, tenant_id)
            prior = _lookup(connection, tenant_id, owner, idempotency_key)
            if prior is not None:
                return _replay(prior, title)
            assignment_id = mint_assignment_id()
            inserted = connection.execute(
                "INSERT INTO assignments (tenant_id, owner_subject,"
                " assignment_id, idempotency_key, title, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (tenant_id, owner_subject, idempotency_key)"
                " DO NOTHING",
                (tenant_id, owner, assignment_id, idempotency_key, title, now))
            if inserted.rowcount == 0:
                # Another transaction won the insert. Its row is committed by
                # the time ON CONFLICT DO NOTHING returns; compare and replay.
                prior = _lookup(connection, tenant_id, owner, idempotency_key)
                if prior is None:
                    raise AssignmentStoreError("store_unusable")
                return _replay(prior, title)
    except AssignmentStoreError:
        raise
    except Exception as error:
        raise AssignmentStoreError("store_unusable") from error
    return {"assignment_id": assignment_id, "title": title,
            "created_at": now, "created": True}


def get_assignment(connection, *, tenant_id: str, owner: str,
                   assignment_id: str):
    try:
        with connection.transaction():
            _bind_tenant(connection, tenant_id)
            row = connection.execute(
                "SELECT " + _RETURN_COLUMNS + " FROM assignments"
                " WHERE tenant_id = %s AND owner_subject = %s"
                " AND assignment_id = %s",
                (tenant_id, owner, assignment_id)).fetchone()
    except Exception as error:
        raise AssignmentStoreError("store_unusable") from error
    return _row_to_assignment(row) if row is not None else None


def list_assignments(connection, *, tenant_id: str, owner: str,
                     limit: int = 50, after_created_at: int = 0,
                     after_assignment_id: str = ""):
    limit = int(limit)
    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise AssignmentStoreError("payload_invalid")
    try:
        with connection.transaction():
            _bind_tenant(connection, tenant_id)
            if after_assignment_id:
                rows = connection.execute(
                    "SELECT " + _RETURN_COLUMNS + " FROM assignments"
                    " WHERE tenant_id = %s AND owner_subject = %s"
                    " AND (created_at < %s OR"
                    " (created_at = %s AND assignment_id < %s))"
                    " ORDER BY created_at DESC, assignment_id DESC LIMIT %s",
                    (tenant_id, owner, int(after_created_at),
                     int(after_created_at), after_assignment_id,
                     limit)).fetchall()
            else:
                rows = connection.execute(
                    "SELECT " + _RETURN_COLUMNS + " FROM assignments"
                    " WHERE tenant_id = %s AND owner_subject = %s"
                    " ORDER BY created_at DESC, assignment_id DESC LIMIT %s",
                    (tenant_id, owner, limit)).fetchall()
    except Exception as error:
        raise AssignmentStoreError("store_unusable") from error
    return [_row_to_assignment(row) for row in rows]
