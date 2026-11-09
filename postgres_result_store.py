"""PostgreSQL result-context persistence (BR-02B).

The v1 audit adapter (``postgres_store.py``) deliberately stores only the
original DIGEST and a marks-only projection. BR-02 adds the complete original
marking output and its authoritative context in a SEPARATE immutable table,
still behind the same tenant-scoped, append-only posture.

WHAT IS ATOMIC HERE
-------------------
``register_with_context`` owns ONE PostgreSQL transaction and, inside it:

1. inserts or validates the immutable audit anchor row in ``results``;
2. inserts or validates the matching ``result_context`` row;
3. commits both together, or rolls both back.

It never calls ``PostgresAuditStore.register_result`` (that method opens its
own transaction) - the shared validation helpers are reused directly so there
is still one transaction owner and one copy of the digest/immutability rules.

READS
-----
``snapshot`` reads the original output, the anchor revision and the full event
chain in ONE transaction, so a projection can never combine original output
from one state with events from another. ``visible_row`` is a direct
(tenant, membership subject, result_id) lookup - never a registry scan.

CAPABILITY
----------
``open_store`` fails closed unless ``schema_meta.result_context_version = '1'``
and the table exists. ``schema_version`` stays ``'1'``; the v9 capability marker
is separate, exactly like the earlier numbered migrations.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Optional

import audit_store
import runtime_config
from audit_store import (
    AuditStoreError,
    _chain_problems,
    _identity,
    _row_to_event,
    canonical_json,
    digest_value,
    original_score_projection,
)
from postgres_store import (
    _EVENT_COLUMNS,
    PostgresAuditStore,
    _bind_tenant,
    _map_constraint,
)

CAPABILITY_KEY = "result_context_version"
CAPABILITY_VERSION = "1"

_RESULT_CONTEXT_COLUMNS = (
    "original_output, original_digest, result_scope, review_key, "
    "document_digest, job_id, assignment_id, classroom_course_id, "
    "classroom_assignment_id, classroom_submission_id, classroom_revision, "
    "owner_subject, membership_subject, EXTRACT(EPOCH FROM created_at)::text"
)


def _context_row(row: Any) -> dict:
    (original_output, original_digest, scope, review_key, document_digest,
     job_id, assignment_id, course, assignment, submission, revision,
     owner_subject, membership_subject, created_at) = row
    try:
        original = json.loads(original_output)
    except (TypeError, ValueError) as error:
        raise AuditStoreError("audit_record_invalid",
                              "the stored original output is unreadable") from error
    if digest_value(original) != original_digest:
        raise AuditStoreError("audit_record_invalid",
                              "the stored original output digest does not match")
    return {
        "original_output": original,
        "original_digest": original_digest,
        "result_scope": scope,
        "review_key": review_key,
        "document_digest": document_digest or "",
        "job_id": job_id,
        "assignment_id": assignment_id,
        "classroom_course_id": course,
        "classroom_assignment_id": assignment,
        "classroom_submission_id": submission,
        "classroom_revision": revision or "",
        "owner_subject": owner_subject,
        "membership_subject": membership_subject,
        "created_at": created_at,
    }


class PostgresResultStore(PostgresAuditStore):
    """Tenant + membership-subject scoped result/audit store (v9 capability)."""

    def __init__(self, connection, *, identity, owns_connection: bool = False,
                 fresh_completion_denied: bool = True):
        super().__init__(connection, tenant_id=identity.tenant_id,
                         owns_connection=owns_connection)
        self.identity = identity
        self.membership_subject = identity.membership_subject
        self.owner_subject = identity.owner_subject
        self.fresh_completion_denied = (
            runtime_config.is_production_runtime() or bool(fresh_completion_denied))
        self._require_capability()

    # -- capability -------------------------------------------------------
    def _require_capability(self) -> None:
        try:
            with self._conn.transaction():
                _bind_tenant(self._conn, self.tenant_id)
                marker = self._conn.execute(
                    "SELECT value FROM schema_meta WHERE key = %s",
                    (CAPABILITY_KEY,)).fetchone()
                table = self._conn.execute(
                    "SELECT to_regclass('public.result_context')").fetchone()
                membership = self._conn.execute(
                    "SELECT 1 FROM tenant_membership WHERE tenant_id = %s"
                    " AND subject = %s AND active IS TRUE",
                    (self.tenant_id, self.membership_subject)).fetchone()
        except AuditStoreError:
            raise
        except Exception as error:
            raise AuditStoreError(
                "store_unusable",
                "the result-context capability could not be checked") from error
        if (marker is None or str(marker[0]) != CAPABILITY_VERSION
                or table is None or table[0] is None):
            raise AuditStoreError(
                "store_unusable",
                "result-context capability missing: apply "
                "schema/postgres_v9_result_context.sql explicitly")
        if membership is None:
            raise AuditStoreError(
                "tenant_membership_required",
                "no active membership row for this owner+tenant")

    # -- writes -----------------------------------------------------------
    def register_with_context(self, *, result_id: Any, original_output: Any,
                              scope: Any, review_key: Any,
                              document_digest: Any = "",
                              job_id: Optional[str] = None,
                              assignment_id: Optional[str] = None,
                              classroom: Optional[dict] = None) -> dict:
        """Insert/validate the anchor AND the immutable context atomically.

        Refuses fresh production registration during the foundations package,
        ensuring normal production composition is deny-by-default.
        """
        if runtime_config.is_production_runtime() or self.fresh_completion_denied:
            raise AuditStoreError("store_unusable",
                                  "fresh result completion is disabled during "
                                  "the foundations package")
        result_id = _identity(result_id, "result_id")
        review_key = _identity(review_key, "review_key")
        scope = str(scope or "")
        if scope not in ("manual", "classroom"):
            raise AuditStoreError("payload_invalid",
                                  "scope must be manual or classroom")
        if not isinstance(original_output, dict) and not hasattr(
                original_output, "keys"):
            raise AuditStoreError("original_output_invalid",
                                  "original output must be a mapping")
        digest = digest_value(original_output)
        scores_json = canonical_json(original_score_projection(original_output))
        scores_digest = digest_value(original_score_projection(original_output))
        document_digest = str(document_digest or "")
        classroom = dict(classroom or {}) if scope == "classroom" else {}

        with self._transaction():
            try:
                cursor = self._conn.execute(
                    "INSERT INTO results (tenant_id, owner_subject, result_id,"
                    " result_revision, original_digest, original_scores,"
                    " original_scores_digest, created_at)"
                    " VALUES (%s, %s, %s, 0, %s, %s, %s, now())"
                    " ON CONFLICT (tenant_id, owner_subject, result_id)"
                    " DO NOTHING",
                    (self.tenant_id, self.membership_subject, result_id, digest,
                     scores_json, scores_digest))
                anchor_created = bool(cursor and cursor.rowcount == 1)
            except Exception as error:
                raise self._map(error) from error
            anchor = self._select_result(self.membership_subject, result_id,
                                         for_update=True)
            if anchor is None:
                raise AuditStoreError(
                    "tenant_membership_required",
                    "no membership row for this owner+tenant")
            if anchor[1] != digest:
                raise AuditStoreError(
                    "original_output_immutable",
                    "a different original output is already registered")

            try:
                cursor = self._conn.execute(
                    "INSERT INTO result_context (tenant_id, membership_subject,"
                    " owner_subject, result_id, result_scope, review_key,"
                    " original_output, original_digest, document_digest,"
                    " job_id, assignment_id, classroom_course_id,"
                    " classroom_assignment_id, classroom_submission_id,"
                    " classroom_revision, created_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                    " %s, %s, %s, %s, now())"
                    " ON CONFLICT (tenant_id, membership_subject, result_id)"
                    " DO NOTHING",
                    (self.tenant_id, self.membership_subject,
                     self.owner_subject, result_id, scope, review_key,
                     canonical_json(original_output), digest, document_digest,
                     job_id, assignment_id,
                     classroom.get("course"), classroom.get("assignment"),
                     classroom.get("submission"),
                     classroom.get("revision") or ""))
                context_created = bool(cursor and cursor.rowcount == 1)
            except Exception as error:
                raise self._map(error) from error
            stored = self._select_context(result_id)
            if stored is None:  # FK refusal would have raised above
                raise AuditStoreError(
                    "storage_constraint_violation",
                    "the result context was not written")
            self._validate_context(_context_row(stored), digest=digest, scope=scope,
                                   review_key=review_key,
                                   document_digest=document_digest,
                                   job_id=job_id, assignment_id=assignment_id,
                                   classroom=classroom)
            return {"created": anchor_created or context_created,
                    "owner": self.owner_subject,
                    "membership_subject": self.membership_subject,
                    "result_id": result_id,
                    "result_revision": int(anchor[0]),
                    "original_digest": digest}

    @staticmethod
    def _map(error: Exception) -> AuditStoreError:
        return _map_constraint(error)

    def _validate_context(self, stored: dict, *, digest: str, scope: str,
                          review_key: str, document_digest: str,
                          job_id: Optional[str], assignment_id: Optional[str],
                          classroom: dict) -> None:
        if stored["original_digest"] != digest:
            raise AuditStoreError(
                "original_output_immutable",
                "a different original output is already registered")
        if (stored["result_scope"] != scope
                or stored["review_key"] != review_key
                or stored["document_digest"] != document_digest):
            raise AuditStoreError(
                "original_output_immutable",
                "different result context is already registered")
        # Trusted links are immutable once stored. A missing stored link is
        # not backfilled (no guessed provenance); a conflicting one refuses.
        if job_id is not None and stored["job_id"] not in (None, job_id):
            raise AuditStoreError("original_output_immutable",
                                  "a different trusted job is already linked")
        if (assignment_id is not None
                and stored["assignment_id"] not in (None, assignment_id)):
            raise AuditStoreError("original_output_immutable",
                                  "a different assignment is already linked")
        for name in ("course", "assignment", "submission", "revision"):
            value = classroom.get(name) or ("" if name == "revision" else None)
            if stored.get("classroom_" + name) != value:
                raise AuditStoreError(
                    "original_output_immutable",
                    "different Classroom provenance is already registered")

    # -- reads ------------------------------------------------------------
    @contextmanager
    def _read_transaction(self):
        """One read transaction at REPEATABLE READ for consistent snapshots.

        Overrides PostgresAuditStore._read_transaction (default READ COMMITTED
        takes a fresh statement-level snapshot per SELECT). ``snapshot`` and
        ``history`` read anchor + original context + event chain as separate
        SELECTs; without a pinned snapshot a concurrent commit between them can
        mix states. REPEATABLE READ pins every read in the transaction to the
        first-read point so original output, anchor revision and event chain
        stay mutually consistent.
        """
        with self._lock:
            if self._closed:
                raise AuditStoreError("store_closed", "store is closed")
            with self._conn.transaction():
                self._conn.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                _bind_tenant(self._conn, self.tenant_id)
                yield

    def _select_context(self, result_id: str):
        return self._conn.execute(
            "SELECT " + _RESULT_CONTEXT_COLUMNS + " FROM result_context"
            " WHERE tenant_id = %s AND membership_subject = %s"
            " AND result_id = %s",
            (self.tenant_id, self.membership_subject, result_id)).fetchone()

    def context_record(self, *, result_id: Any) -> dict:
        result_id = _identity(result_id, "result_id")
        with self._read_transaction():
            row = self._select_context(result_id)
            if row is None:
                raise AuditStoreError("result_not_registered",
                                      "no result context for this owner")
            return _context_row(row)

    @staticmethod
    def _row_from_context(context: dict, tenant_id: str) -> dict:
        original = context["original_output"]
        row = {
            "status": "Marked",
            "review_key": context["review_key"],
            "report_owner": context["owner_subject"],
            "snapshot_context": context["document_digest"],
            "tenant_id": tenant_id,
            "revision": context["classroom_revision"],
            "result": original,
            "ai_result": original,
            "student_name": "",
            "marking_model": "ingested",
            "audit_model": "Not run",
            "result_scope": context["result_scope"],
            "membership_subject": context["membership_subject"],
            "trusted_job_id": context["job_id"],
        }
        if context["assignment_id"] is not None:
            row["assignment_id"] = context["assignment_id"]
        if context["result_scope"] == "classroom":
            row.update({
                "classroom_course_id": context["classroom_course_id"],
                "classroom_assignment_id":
                    context["classroom_assignment_id"],
                "submission_id": context["classroom_submission_id"],
            })
        return row

    def visible_row(self, result_id: Any) -> Optional[dict]:
        """Direct context lookup shaped like the API's owner-visible row."""
        try:
            context = self.context_record(result_id=result_id)
        except AuditStoreError as failure:
            if failure.code == "result_not_registered":
                return None
            raise
        return self._row_from_context(context, self.tenant_id)

    def history(self, *, result_id: Any,
                limit: Optional[int] = None) -> dict:
        """Revision + event chain from ONE read transaction.

        The whole chain is validated before an optional ``limit`` is applied,
        so a truncated page can never hide an ordering/gap problem.
        """
        result_id = _identity(result_id, "result_id")
        with self._read_transaction():
            anchor = self._select_result(self.membership_subject, result_id)
            if anchor is None:
                raise AuditStoreError("result_not_registered",
                                      "no result registered for this owner")
            rows = self._conn.execute(
                "SELECT " + _EVENT_COLUMNS + " FROM audit_events"
                " WHERE tenant_id = %s AND owner_subject = %s"
                " AND result_id = %s ORDER BY sequence",
                (self.tenant_id, self.membership_subject, result_id)).fetchall()
            events = [_row_to_event(row) for row in rows]
            problems = _chain_problems(events,
                                       result_revision=int(anchor[0]))
            if problems:
                raise AuditStoreError("audit_order_invalid",
                                      "; ".join(problems))
            if limit is not None:
                events = events[:int(limit)]
            return {"result_revision": int(anchor[0]), "events": events}

    def snapshot(self, *, result_id: Any) -> dict:
        """One-consistency-point read of context, anchor and event chain."""
        result_id = _identity(result_id, "result_id")
        with self._read_transaction():
            anchor = self._select_result(self.membership_subject, result_id)
            if anchor is None:
                raise AuditStoreError("result_not_registered",
                                      "no result registered for this owner")
            context_row = self._select_context(result_id)
            if context_row is None:
                raise AuditStoreError("result_not_registered",
                                      "no result context for this owner")
            context = _context_row(context_row)
            if context["original_digest"] != anchor[1]:
                raise AuditStoreError(
                    "audit_record_invalid",
                    "the anchor and context original digests differ")
            rows = self._conn.execute(
                "SELECT " + _EVENT_COLUMNS + " FROM audit_events"
                " WHERE tenant_id = %s AND owner_subject = %s"
                " AND result_id = %s ORDER BY sequence",
                (self.tenant_id, self.membership_subject, result_id)).fetchall()
            events = [_row_to_event(row) for row in rows]
            problems = _chain_problems(events,
                                       result_revision=int(anchor[0]))
            if problems:
                raise AuditStoreError("audit_order_invalid",
                                      "; ".join(problems))
            return {"result_id": result_id,
                    "result_revision": int(anchor[0]),
                    "original_digest": context["original_digest"],
                    "original_output": context["original_output"],
                    "row": self._row_from_context(context, self.tenant_id),
                    "events": events,
                    "created_at": context["created_at"]}


def resolve_membership_subject(connection, *, tenant_id: str,
                               owner: str) -> str:
    """Map a TRUSTED stored canonical owner to its active membership subject.

    Used only by the worker/job path, where ``owner`` was read from
    ``marking_jobs``. Exactly one active row must match; zero or several
    fail closed. The mapping is an exact ``google:`` + subject equality, not a
    prefix strip of an untrusted value.

    PRIVILEGE NOTE (owner decision required, not owner-approved): this resolver
    reads ``tenant_membership`` subject/role/active, but
    ``schema/postgres_v9_result_context.sql`` deliberately grants marker_worker
    no column on ``tenant_membership`` (stop condition 17.6: schema privilege
    expansion needs owner direction). With existing privileges,
    ``open_owner_store`` cannot resolve owner -> subject, so the worker path
    fails closed and stays blocked until the owner decides how ownership is
    resolved without expanding marker_worker privileges. The narrow membership
    SELECT grant to marker_worker is deliberately NOT restored here.

    The two no-expanded-privilege alternatives are broader job-schema or
    protocol changes and are NOT implemented here; each requires specific owner
    authorization before the worker can use the PostgreSQL result store:

    * Store ``membership_subject`` on ``marking_jobs`` (new migration column +
      new migration file, IR approval) so the worker reads the raw subject from
      the job row it already selects instead of joining tenant_membership.
    * Resolve subject at the API layer (marker_app holds the membership SELECT
      via 0005) and pass it to the worker out-of-band (job payload or a
      separate trusted channel).

    Exact prerequisite: the owner must authorize one of the above (or an
    explicit privilege-expansion decision) before the worker's owner ->
    subject resolution can succeed. Until then the worker is blocked - it does
    not write results through PostgreSQL and does not fabricate or weaken the
    active-membership check.
    """
    owner = _identity(owner, "owner_subject")
    tenant_id = _identity(tenant_id, "tenant_id")
    with connection.transaction():
        _bind_tenant(connection, tenant_id)
        rows = connection.execute(
            "SELECT subject FROM tenant_membership WHERE tenant_id = %s"
            " AND active IS TRUE AND ('google:' || subject) = %s",
            (tenant_id, owner)).fetchall()
    if len(rows) != 1:
        raise AuditStoreError("tenant_membership_required",
                              "no single active membership for the job owner")
    return _identity(rows[0][0], "membership_subject")


def open_store(dsn: str, *, identity,
               fresh_completion_denied: bool = True) -> PostgresResultStore:
    """Connect and validate schema v1 + the v9 result-context capability."""
    import psycopg  # lazy: only the Postgres path needs the driver

    if not isinstance(dsn, str) or not dsn.strip():
        raise AuditStoreError("store_unusable",
                              "a PostgreSQL DSN is required (none configured)")
    try:
        connection = psycopg.connect(dsn.strip(), prepare_threshold=None)
    except Exception as error:
        raise AuditStoreError("store_unusable",
                              "cannot connect to PostgreSQL") from error
    try:
        return PostgresResultStore(connection, identity=identity,
                                   owns_connection=True,
                                   fresh_completion_denied=
                                   fresh_completion_denied)
    except Exception:
        try:
            connection.close()
        except Exception:
            pass
        raise


def open_owner_store(dsn: str, *, tenant_id: str, owner: str,
                     fresh_completion_denied: bool = True):
    """Open the store for a TRUSTED stored canonical owner (worker path)."""
    import os

    import psycopg

    import identity_context
    from runtime_config import PG_DSN_ENV

    target = dsn if dsn is not None else (os.environ.get(PG_DSN_ENV) or "")
    target = (target or "").strip()
    if not target:
        raise AuditStoreError("store_unusable",
                              "a PostgreSQL DSN is required (none configured)")
    try:
        connection = psycopg.connect(target.strip(), prepare_threshold=None)
    except Exception as error:
        raise AuditStoreError("store_unusable",
                              "cannot connect to PostgreSQL") from error
    try:
        subject = resolve_membership_subject(connection, tenant_id=tenant_id,
                                             owner=owner)
        identity = identity_context.from_trusted_owner(owner, tenant_id,
                                                       subject)
        return PostgresResultStore(connection, identity=identity,
                                   owns_connection=True,
                                   fresh_completion_denied=
                                   fresh_completion_denied)
    except Exception:
        try:
            connection.close()
        except Exception:
            pass
        raise
