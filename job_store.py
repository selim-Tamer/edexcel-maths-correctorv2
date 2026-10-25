"""Dev-only durable marking-job store (queue + status + bounded retries).

POSTURE (honest): local-dev storage only behind the explicit opt-in env pair
``AI_MARKER_JOB_DB`` + ``AI_MARKER_JOB_DEV_ONLY=1`` - labelled NOT school
storage, the same discipline as the audit/ingest/document stores, and
consolidated into the approved (Supabase) PostgreSQL later. No token, no
document bytes and no pupil data live here: only the job's identity, the
document digest it points at, its state machine and its result reference.

Model:
* A job is created by an authenticated owner with a client ``idempotency_key``
  - the UNIQUE (tenant, owner, key) constraint makes creation replay-safe and
  a key reused for a DIFFERENT document is refused (payload mismatch), which
  is the same idempotency-before-write discipline as the audit store.
* ``job_id`` is minted SERVER-side (deterministic hash of tenant+owner+key),
  so a client never chooses the resource identity.
* State machine: ``queued -> running -> (succeeded | failed)``. ``running``
  carries a lease (``lease_until``): a crashed worker's expired lease is
  reclaimable, so jobs survive restarts. Attempts are bounded by
  ``max_attempts``; exhausting them fails the job with a static error code
  instead of retrying forever.
* Rows are tenant- and owner-scoped for every read (indistinguishable 404).
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import time

PATH_ENV = "AI_MARKER_JOB_DB"
ACK_ENV = "AI_MARKER_JOB_DEV_ONLY"

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 60

_STATUSES = ("queued", "running", "succeeded", "failed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS marking_jobs (
    tenant_id       TEXT NOT NULL,
    owner_subject   TEXT NOT NULL,
    job_id          TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    document_digest TEXT NOT NULL,
    status          TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL,
    lease_until     INTEGER NOT NULL DEFAULT 0,
    result_id       TEXT,
    error_code      TEXT,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, owner_subject, job_id),
    UNIQUE (tenant_id, owner_subject, idempotency_key)
)
"""


class JobStoreError(Exception):
    """Fail-closed job-store failure with a static, envelope-safe detail."""

    def __init__(self, detail: str):
        self.detail = str(detail)
        super().__init__(self.detail)


def open_store(path: str) -> sqlite3.Connection:
    if not isinstance(path, str) or not path.strip():
        raise JobStoreError("store_unusable")
    try:
        connection = sqlite3.connect(path, timeout=10)
        connection.execute(_SCHEMA)
        connection.commit()
    except sqlite3.Error:
        raise JobStoreError("store_unusable")
    return connection


def require_connection() -> sqlite3.Connection:
    """Open the job store per env, failing closed when unconfigured."""
    path = (os.environ.get(PATH_ENV) or "").strip()
    acknowledged = (os.environ.get(ACK_ENV) or "").strip() == "1"
    if not path or not acknowledged:
        raise JobStoreError("dev_opt_in_required")
    # One connection per request (same reasoning as ingest_registry).
    return open_store(path)


def mint_job_id(tenant_id: str, owner: str, idempotency_key: str) -> str:
    """Server-minted resource identity: the client never chooses it."""
    material = f"{tenant_id}\n{owner}\n{idempotency_key}".encode("utf-8")
    return "job:" + hashlib.sha256(material).hexdigest()[:24]


def _row_to_job(row) -> dict:
    keys = ("tenant_id", "owner_subject", "job_id", "idempotency_key",
            "document_digest", "status", "attempts", "max_attempts",
            "lease_until", "result_id", "error_code", "created_at",
            "updated_at")
    return dict(zip(keys, row))


def create_job(connection: sqlite3.Connection, *, tenant_id: str, owner: str,
                idempotency_key: str, document_digest: str,
                max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> dict:
    """Create (or replay) a job. Returns the job plus ``created``.

    Replay of the SAME key for the SAME digest returns the stored job with
    created=False; the same key for a DIFFERENT document is refused - a
    client cannot silently retarget an existing job.
    """
    now = int(time.time())
    job_id = mint_job_id(tenant_id, owner, idempotency_key)
    try:
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute(
            "SELECT document_digest, job_id FROM marking_jobs"
            " WHERE tenant_id = ? AND owner_subject = ?"
            " AND idempotency_key = ?", (tenant_id, owner, idempotency_key)
        ).fetchone()
        if prior is not None:
            connection.commit()
            if prior[0] != document_digest:
                raise JobStoreError("idempotency_key_payload_mismatch")
            return {"job_id": prior[1], "created": False,
                    "document_digest": prior[0]}
        connection.execute(
            "INSERT INTO marking_jobs (tenant_id, owner_subject, job_id,"
            " idempotency_key, document_digest, status, attempts, max_attempts,"
            " lease_until, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, 0, ?, ?)",
            (tenant_id, owner, job_id, idempotency_key, document_digest,
             int(max_attempts), now, now))
        connection.commit()
        return {"job_id": job_id, "created": True,
                "document_digest": document_digest}
    except JobStoreError:
        connection.rollback()
        raise
    except sqlite3.Error as error:
        connection.rollback()
        raise JobStoreError("store_unusable") from error


def get_job(connection: sqlite3.Connection, *, tenant_id: str, owner: str,
            job_id: str):
    """One owner+tenant-scoped job, or None (misses never leak)."""
    try:
        row = connection.execute(
            "SELECT tenant_id, owner_subject, job_id, idempotency_key,"
            " document_digest, status, attempts, max_attempts, lease_until,"
            " result_id, error_code, created_at, updated_at"
            " FROM marking_jobs WHERE tenant_id = ? AND owner_subject = ?"
            " AND job_id = ?", (tenant_id, owner, job_id)).fetchone()
    except sqlite3.Error as error:
        raise JobStoreError("store_unusable") from error
    return _row_to_job(row) if row is not None else None


def find_authorizable_job(connection: sqlite3.Connection, *, tenant_id: str,
                          owner: str, document_digest: str):
    """A SUCCEEDED job for this exact owner+tenant+document.

    This is the production shape of PR57's trusted-provenance gate: client
    ingest is authorised only by a job that has COMPLETED and already
    registered the genuine engine output. The in-flight ``running`` state is
    deliberately NOT authorizing for clients - the lease-holding runner uses
    its own authority instead (see ``finish_ingest`` in api/app.py), so an
    authenticated owner cannot pre-empt a job's result with a fabricated
    ``original_output`` during the claim->succeed window (finding J-1).
    """
    try:
        row = connection.execute(
            "SELECT job_id FROM marking_jobs WHERE tenant_id = ?"
            " AND owner_subject = ? AND document_digest = ?"
            " AND status = 'succeeded'"
            " ORDER BY created_at DESC LIMIT 1",
            (tenant_id, owner, document_digest)).fetchone()
    except sqlite3.Error as error:
        raise JobStoreError("store_unusable") from error
    return row[0] if row is not None else None


def claim_job(connection: sqlite3.Connection, *, tenant_id: str, owner: str,
              job_id: str, now: int = None, lease_seconds: int = None):
    """Atomically claim a runnable job. Returns (job, claimed).

    Runnable = ``queued``, or ``running`` whose lease has EXPIRED (crash
    recovery). A live lease is never stolen. Exhausting ``max_attempts``
    fails the job instead of looping.
    """
    now = int(time.time()) if now is None else int(now)
    lease_seconds = (DEFAULT_LEASE_SECONDS if lease_seconds is None
                     else int(lease_seconds))
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT tenant_id, owner_subject, job_id, idempotency_key,"
            " document_digest, status, attempts, max_attempts, lease_until,"
            " result_id, error_code, created_at, updated_at"
            " FROM marking_jobs WHERE tenant_id = ? AND owner_subject = ?"
            " AND job_id = ?", (tenant_id, owner, job_id)).fetchone()
        if row is None:
            connection.rollback()
            return None, False
        job = _row_to_job(row)
        runnable = (job["status"] == "queued" or
                    (job["status"] == "running" and
                     int(job["lease_until"]) <= now))
        if not runnable:
            connection.commit()
            return job, False
        if int(job["attempts"]) >= int(job["max_attempts"]):
            connection.execute(
                "UPDATE marking_jobs SET status = 'failed', lease_until = 0,"
                " updated_at = ? WHERE tenant_id = ? AND owner_subject = ?"
                " AND job_id = ?", (now, tenant_id, owner, job_id))
            connection.commit()
            job["status"] = "failed"
            return job, False
        attempts = int(job["attempts"]) + 1
        connection.execute(
            "UPDATE marking_jobs SET status = 'running', attempts = ?,"
            " lease_until = ?, updated_at = ? WHERE tenant_id = ?"
            " AND owner_subject = ? AND job_id = ?",
            (attempts, now + lease_seconds, now, tenant_id, owner, job_id))
        connection.commit()
        job.update({"status": "running", "attempts": attempts,
                    "lease_until": now + lease_seconds})
        return job, True
    except sqlite3.Error as error:
        connection.rollback()
        raise JobStoreError("store_unusable") from error


def succeed_job(connection: sqlite3.Connection, *, tenant_id: str,
                owner: str, job_id: str, result_id: str) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE marking_jobs SET status = 'succeeded', result_id = ?,"
            " error_code = NULL, lease_until = 0, updated_at = ?"
            " WHERE tenant_id = ? AND owner_subject = ? AND job_id = ?",
            (result_id, int(time.time()), tenant_id, owner, job_id))
        connection.commit()
    except sqlite3.Error as error:
        connection.rollback()
        raise JobStoreError("store_unusable") from error


def settle_failure(connection: sqlite3.Connection, *, tenant_id: str,
                   owner: str, job_id: str, error_code: str) -> str:
    """Record a failed attempt: requeue while attempts remain, else fail.

    Returns the resulting status. The error code is always drawn from the
    existing source vocabulary (static, content-free).
    """
    now = int(time.time())
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT attempts, max_attempts FROM marking_jobs"
            " WHERE tenant_id = ? AND owner_subject = ? AND job_id = ?",
            (tenant_id, owner, job_id)).fetchone()
        if row is None:
            connection.rollback()
            raise JobStoreError("result_not_registered")
        attempts, max_attempts = int(row[0]), int(row[1])
        status = "failed" if attempts >= max_attempts else "queued"
        connection.execute(
            "UPDATE marking_jobs SET status = ?, error_code = ?,"
            " lease_until = 0, updated_at = ? WHERE tenant_id = ?"
            " AND owner_subject = ? AND job_id = ?",
            (status, str(error_code), now, tenant_id, owner, job_id))
        connection.commit()
        return status
    except JobStoreError:
        connection.rollback()
        raise
    except sqlite3.Error as error:
        connection.rollback()
        raise JobStoreError("store_unusable") from error
