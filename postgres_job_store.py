"""PostgreSQL port of ``job_store``: the SAME lease/attempt semantics.

The development job store (``job_store.py``) is SQLite. Production needs jobs
that survive process restarts and Cloud Run scale-down on the managed
database, with exactly the semantics already shipped and tested:

* a job is identified by ``(tenant_id, owner_subject, job_id)`` and keyed by
  ``(tenant_id, owner_subject, idempotency_key)``;
* replaying the SAME key with the SAME digest returns the stored job;
  the same key for a DIFFERENT document is refused
  (``idempotency_key_payload_mismatch``) - a client cannot retarget a job;
* a claim is atomic and lease-guarded: ``queued``, or ``running`` whose lease
  has EXPIRED (crash recovery). A live lease is never stolen;
* attempts are bounded by ``max_attempts``; exhausting them fails the job
  instead of looping, and every failure code is drawn from the existing
  static vocabulary (``store_unusable``, ``result_not_registered``).

Differences from the development store, deliberate:

* **No DDL here.** ``job_store.open_store`` creates its SQLite schema; this
  port fails closed unless ``schema/postgres_v3_jobs.sql`` was applied by an
  explicit operator step (``jobs_version = '1'``), mirroring
  ``postgres_store.open_store``.
* **Row-level security + least privilege.** ``marking_jobs`` carries the same
  ``tenant_isolation`` policy as the five tenant tables; the connection binds
  ``app.tenant_id`` transaction-locally from the store's own arguments, never
  from a request field. With no binding, reads see zero rows (fail closed).
* **`prepare_threshold=None`** on every connection, for the Supabase
  transaction pooler (see ``postgres_store.open_store``). The real pgBouncer
  pooler remains unverified.
* Runtime roles hold **no DELETE** on ``marking_jobs`` (0003): job rows are
  history. Retention/purge belongs to the separate elevated role.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from job_store import (DEFAULT_LEASE_SECONDS, DEFAULT_MAX_ATTEMPTS,
                       JobStoreError, mint_job_id)
from runtime_config import PG_DSN_ENV

TENANT_GUC = "app.tenant_id"

_JOB_COLUMNS = ("tenant_id", "owner_subject", "job_id", "idempotency_key",
                "document_digest", "status", "attempts", "max_attempts",
                "lease_until", "result_id", "error_code", "created_at",
                "updated_at", "assignment_id")


def _bind_tenant(connection, tenant_id: str) -> None:
    """Transaction-local identity binding (see postgres_store._bind_tenant)."""
    connection.execute("SELECT set_config(%s, %s, true)",
                       (TENANT_GUC, tenant_id))


def _rows_to_job(row) -> dict:
    return dict(zip(_JOB_COLUMNS, row))


def _assignment_link_error(error):
    """Translate ONLY the 0008 composite-FK refusal into a product error.

    The API validates assignment ownership before it writes, so this branch is
    defence in depth: it fires when a caller reached the store directly, or
    when the assignment was removed between the API check and the insert. It
    must not be reported as ``store_unusable`` (that would tell the client the
    whole store is down) and it must not be reported as success.

    Narrow on purpose: sqlstate 23503 AND the exact constraint name. Any other
    foreign-key refusal (for example the membership reference) keeps the
    existing generic mapping.
    """
    if getattr(error, "sqlstate", None) != "23503":
        return None
    diag = getattr(error, "diag", None)
    if getattr(diag, "constraint_name", None) != "fk_marking_jobs_assignment":
        return None
    return JobStoreError("assignment_not_found")


def open_store(dsn: str):
    """Connect and validate (never create) the 0003 job schema."""
    import psycopg  # lazy: only the Postgres path needs the driver

    if not isinstance(dsn, str) or not dsn.strip():
        raise JobStoreError("store_unusable")
    try:
        connection = psycopg.connect(dsn.strip(), prepare_threshold=None)
    except Exception as error:
        raise JobStoreError("store_unusable") from error
    try:
        with connection.transaction():
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'jobs_version'"
            ).fetchone()
            present = connection.execute(
                "SELECT to_regclass('public.marking_jobs')"
            ).fetchone()
            linkage = connection.execute(
                "SELECT value FROM schema_meta"
                " WHERE key = 'job_assignment_linkage_version'"
            ).fetchone()
    except Exception as error:
        try:
            connection.close()
        except Exception:
            pass
        raise JobStoreError("store_unusable") from error
    if (row is None or str(row[0]) != "1" or present is None
            or present[0] is None or linkage is None
            or str(linkage[0]) != "1"):
        try:
            connection.close()
        except Exception:
            pass
        raise JobStoreError("store_unusable")
    return connection


def configured() -> bool:
    """True when this process is composed with the production job store."""
    return bool((os.environ.get(PG_DSN_ENV) or "").strip())


def require_connection():
    """Open the production job store from env, failing closed when absent."""
    dsn = (os.environ.get(PG_DSN_ENV) or "").strip()
    if not dsn:
        raise JobStoreError("store_unusable")
    return open_store(dsn)


def find_runnable_job(connection, *, tenant_id: str,
                      now: Optional[int] = None):
    """Newest runnable job for this tenant, or ``None`` (worker discovery).

    Runnable = ``queued``, or ``running`` whose lease has EXPIRED (crash
    recovery). This only identifies a candidate; the claim itself is the
    atomic ``claim_job`` step, so two workers racing here cannot
    double-execute. Discovery is tenant-scoped on purpose: the worker is told
    which tenants it serves (``AI_MARKER_WORKER_TENANTS``) instead of being
    handed a privileged cross-tenant read.
    """
    now = int(time.time()) if now is None else int(now)
    try:
        with connection.transaction():
            _bind_tenant(connection, tenant_id)
            row = connection.execute(
                "SELECT " + ", ".join(_JOB_COLUMNS) + " FROM marking_jobs"
                " WHERE tenant_id = %s AND (status = 'queued' OR"
                " (status = 'running' AND lease_until <= %s))"
                " ORDER BY created_at ASC LIMIT 1",
                (tenant_id, now)).fetchone()
    except Exception as error:
        raise JobStoreError("store_unusable") from error
    return _rows_to_job(row) if row is not None else None


def create_job(connection, *, tenant_id: str, owner: str,
               idempotency_key: str, document_digest: str,
               assignment_id: str = None,
               max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> dict:
    """Create (or replay) a job. Returns ``job_id``, ``created``, digest.

    Replay of the SAME key for the SAME digest and assignment returns the
    stored job with ``created=False``; the same key for a DIFFERENT document
    or a different assignment link is refused.
    """
    now = int(time.time())
    job_id = mint_job_id(tenant_id, owner, idempotency_key)
    try:
        with connection.transaction():
            _bind_tenant(connection, tenant_id)
            prior = connection.execute(
                "SELECT document_digest, job_id, assignment_id FROM marking_jobs"
                " WHERE tenant_id = %s AND owner_subject = %s"
                " AND idempotency_key = %s",
                (tenant_id, owner, idempotency_key)).fetchone()
            if prior is not None:
                if prior[0] != document_digest or prior[2] != assignment_id:
                    raise JobStoreError("idempotency_key_payload_mismatch")
                return {"job_id": prior[1], "created": False,
                        "document_digest": prior[0],
                        "assignment_id": prior[2]}
            connection.execute(
                "INSERT INTO marking_jobs (tenant_id, owner_subject, job_id,"
                " idempotency_key, document_digest, status, attempts,"
                " max_attempts, lease_until, created_at, updated_at,"
                " assignment_id)"
                " VALUES (%s, %s, %s, %s, %s, 'queued', 0, %s, 0, %s, %s, %s)",
                (tenant_id, owner, job_id, idempotency_key, document_digest,
                 int(max_attempts), now, now, assignment_id))
        return {"job_id": job_id, "created": True,
                "document_digest": document_digest,
                "assignment_id": assignment_id}
    except JobStoreError:
        raise
    except Exception as error:
        raise _assignment_link_error(error) or JobStoreError(
            "store_unusable") from error


def get_job(connection, *, tenant_id: str, owner: str, job_id: str):
    """One owner+tenant-scoped job, or ``None`` (misses never leak)."""
    try:
        with connection.transaction():
            _bind_tenant(connection, tenant_id)
            row = connection.execute(
                "SELECT " + ", ".join(_JOB_COLUMNS) + " FROM marking_jobs"
                " WHERE tenant_id = %s AND owner_subject = %s"
                " AND job_id = %s", (tenant_id, owner, job_id)).fetchone()
    except Exception as error:
        raise JobStoreError("store_unusable") from error
    return _rows_to_job(row) if row is not None else None


def find_authorizable_job(connection, *, tenant_id: str, owner: str,
                          document_digest: str) -> Optional[str]:
    """A SUCCEEDED job for this exact owner+tenant+document, else ``None``.

    Deliberately NOT authorizing on ``running``: the in-flight window must not
    let an owner pre-empt a job's result with a fabricated ``original_output``
    (job_store.py, finding J-1).
    """
    try:
        with connection.transaction():
            _bind_tenant(connection, tenant_id)
            row = connection.execute(
                "SELECT job_id FROM marking_jobs WHERE tenant_id = %s"
                " AND owner_subject = %s AND document_digest = %s"
                " AND status = 'succeeded'"
                " ORDER BY created_at DESC LIMIT 1",
                (tenant_id, owner, document_digest)).fetchone()
    except Exception as error:
        raise JobStoreError("store_unusable") from error
    return row[0] if row is not None else None


def claim_job(connection, *, tenant_id: str, owner: str, job_id: str,
              now: Optional[int] = None,
              lease_seconds: Optional[int] = None):
    """Atomically claim a runnable job. Returns ``(job, claimed)``.

    Runnable = ``queued``, or ``running`` whose lease has EXPIRED (crash
    recovery). A live lease is never stolen. Exhausting ``max_attempts``
    fails the job instead of looping.
    """
    now = int(time.time()) if now is None else int(now)
    lease_seconds = (DEFAULT_LEASE_SECONDS if lease_seconds is None
                     else int(lease_seconds))
    try:
        with connection.transaction():
            _bind_tenant(connection, tenant_id)
            row = connection.execute(
                "SELECT " + ", ".join(_JOB_COLUMNS) + " FROM marking_jobs"
                " WHERE tenant_id = %s AND owner_subject = %s"
                " AND job_id = %s FOR UPDATE",
                (tenant_id, owner, job_id)).fetchone()
            if row is None:
                # A miss is not an error here (job_store parity): the caller
                # treats (None, False) as "nothing to do".
                return None, False
            job = _rows_to_job(row)
            if not (job["status"] == "queued"
                    or (job["status"] == "running"
                        and int(job["lease_until"]) <= now)):
                return job, False
            if int(job["attempts"]) >= int(job["max_attempts"]):
                connection.execute(
                    "UPDATE marking_jobs SET status = 'failed',"
                    " lease_until = 0, updated_at = %s WHERE tenant_id = %s"
                    " AND owner_subject = %s AND job_id = %s",
                    (now, tenant_id, owner, job_id))
                job.update({"status": "failed", "lease_until": 0,
                            "updated_at": now})
                return job, False
            attempts = int(job["attempts"]) + 1
            connection.execute(
                "UPDATE marking_jobs SET status = 'running', attempts = %s,"
                " lease_until = %s, updated_at = %s WHERE tenant_id = %s"
                " AND owner_subject = %s AND job_id = %s",
                (attempts, now + lease_seconds, now, tenant_id, owner,
                 job_id))
            job.update({"status": "running", "attempts": attempts,
                        "lease_until": now + lease_seconds})
            return job, True
    except JobStoreError:
        raise
    except Exception as error:
        raise JobStoreError("store_unusable") from error


def succeed_job(connection, *, tenant_id: str, owner: str, job_id: str,
                result_id: str) -> None:
    try:
        with connection.transaction():
            _bind_tenant(connection, tenant_id)
            connection.execute(
                "UPDATE marking_jobs SET status = 'succeeded',"
                " result_id = %s, error_code = NULL, lease_until = 0,"
                " updated_at = %s WHERE tenant_id = %s"
                " AND owner_subject = %s AND job_id = %s",
                (result_id, int(time.time()), tenant_id, owner, job_id))
    except Exception as error:
        raise JobStoreError("store_unusable") from error


def settle_failure(connection, *, tenant_id: str, owner: str, job_id: str,
                   error_code: str) -> str:
    """Record a failed attempt: requeue while attempts remain, else fail.

    Returns the resulting status. The error code is always drawn from the
    existing source vocabulary (static, content-free).
    """
    now = int(time.time())
    try:
        with connection.transaction():
            _bind_tenant(connection, tenant_id)
            row = connection.execute(
                "SELECT attempts, max_attempts FROM marking_jobs"
                " WHERE tenant_id = %s AND owner_subject = %s"
                " AND job_id = %s FOR UPDATE",
                (tenant_id, owner, job_id)).fetchone()
            if row is None:
                raise JobStoreError("result_not_registered")
            attempts, max_attempts = int(row[0]), int(row[1])
            status = "failed" if attempts >= max_attempts else "queued"
            connection.execute(
                "UPDATE marking_jobs SET status = %s, error_code = %s,"
                " lease_until = 0, updated_at = %s WHERE tenant_id = %s"
                " AND owner_subject = %s AND job_id = %s",
                (status, str(error_code), now, tenant_id, owner, job_id))
        return status
    except JobStoreError:
        raise
    except Exception as error:
        raise JobStoreError("store_unusable") from error
