"""PostgreSQL adapter for the M5 audit/correction contract (schema v1).

WHAT THIS IS: an implementation of the SAME transactional contract the
approved SQLite development store (``audit_store.py``) defines, against
PostgreSQL, with the tenant dimension added: every stream row is scoped by
(tenant_id, owner_subject) and FKs back to an existing membership row, so a
client claim can never mint ownership or tenancy.

INVARIANTS (pinned, mirrored exactly from the SQLite store):
* **idempotency key is looked up BEFORE the revision check** - a late retry
  of an already-committed operation replays the prior result even after the
  stream advanced (BCM-required ordering; tested);
* optimistic concurrency: a fresh-key write with a stale ``expected_revision``
  fails ``stale_revision`` and appends NOTHING;
* originals are immutable: re-registering a different original for the same
  stream fails ``original_output_immutable``; only canonical digests and a
  marks-only projection are stored - never answers or working text;
* one transaction writes the correction event AND advances the revision
  (``SELECT ... FOR UPDATE`` is the PostgreSQL translation of SQLite's
  ``BEGIN IMMEDIATE`` writer lock), so a failure never leaves a partial
  stream;
* identity limits and error codes come from ``audit_store`` itself
  (``owner_too_long``, ``result_id_too_long``, chain/evidence whitelists via
  the shared ``_chain_problems``/``_row_to_event`` helpers - one source of
  truth, no duplicated security logic).

POSTURE (honest): schema v1 in ``schema/postgres_v1.sql`` is applied
EXPLICITLY - no auto-migration; ``open_store`` fails closed on a missing or
unsupported ``schema_meta`` version. This adapter is tested ONLY through the
CI Postgres service container; **no instance is provisioned, deployed or
paid for**, and the local-dev fallback remains the development-only SQLite
store. Not school-approved storage until provisioning (B) approval exists.
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Optional

import audit_store
from audit_store import (
    AuditStoreError,
    canonical_json,
    digest_value,
    payload_digest,
    original_score_projection,
)
# Same-repo contract reuse: the chain rules and the evidence re-whitelist are
# security checks - deliberately NOT reimplemented here.
from audit_store import _chain_problems, _identity, _row_to_event

SCHEMA_VERSION = 1
MODE_LABEL = ("PostgreSQL adapter (schema v1): CI-service tested, NOT "
              "provisioned; local-dev fallback remains the development-only "
              "SQLite store; NOT school-approved storage")

# Exact column order expected by audit_store._row_to_event (18 fields).
_EVENT_COLUMNS = (
    "sequence, event_id, idempotency_key, payload_digest, event_type, "
    "question_number, action, review_status, old_score, new_score, reason, "
    "expected_revision, resulting_revision, supersedes_event_id, actor, "
    "binding, authorization_json, created_at"
)

FAULT_STAGES = ("begin", "after_event_insert", "before_commit")


def _map_constraint(error: Exception) -> AuditStoreError:
    """Translate PostgreSQL constraint failures into the store's code
    vocabulary (same codes the SQLite store produces for the same events)."""
    name = ""
    diag = getattr(error, "diag", None)
    if diag is not None and getattr(diag, "constraint_name", None):
        name += diag.constraint_name + " "
    name += str(error).lower()
    if "idempotency" in name:
        return AuditStoreError("idempotency_key_conflict",
                               "idempotency key already used in this stream")
    if "sequence" in name:
        return AuditStoreError("stream_sequence_conflict",
                               "stream sequence already used in this stream")
    if "membership" in name:
        # FK back to tenant_membership: the owner has no membership row.
        return AuditStoreError("tenant_membership_required",
                               "no membership row for this owner+tenant")
    if "foreign key" in name or "violates" in name:
        return AuditStoreError("storage_constraint_violation", str(error))
    return AuditStoreError("storage_constraint_violation", str(error))


class PostgresAuditStore:
    """Tenant-scoped handle over one PostgreSQL connection."""

    def __init__(self, connection, *, tenant_id: str,
                 owns_connection: bool = False):
        self._conn = connection
        self.tenant_id = _identity(str(tenant_id), "tenant_id")
        self._owns_connection = bool(owns_connection)
        self._closed = False
        self._fault: Optional[Callable[[str], Any]] = None
        # Fail closed on a missing/unsupported schema: no silent migration.
        with self._conn.transaction():
            try:
                row = self._conn.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
            except Exception as error:
                raise AuditStoreError(
                    "store_unusable",
                    "schema_meta missing - apply schema/postgres_v1.sql "
                    "explicitly first") from error
        if row is None or int(row[0]) != SCHEMA_VERSION:
            raise AuditStoreError("schema_version_unsupported",
                                  f"store schema version "
                                  f"{row[0] if row else 'missing'} is not "
                                  f"supported (expected {SCHEMA_VERSION})")

    # -- plumbing ---------------------------------------------------------
    @property
    def schema_version(self) -> int:
        return SCHEMA_VERSION

    def set_fault_hook(self, hook: Optional[Callable[[str], Any]]) -> None:
        """Failure injection for tests: ``hook(stage)`` may raise to abort."""
        if hook is not None and not callable(hook):
            raise AuditStoreError("fault_hook_invalid", "hook must be callable")
        self._fault = hook

    def _fault_point(self, stage: str) -> None:
        if self._fault is not None:
            self._fault(stage)

    @contextmanager
    def _transaction(self):
        if self._closed:
            raise AuditStoreError("store_closed", "store is closed")
        with self._conn.transaction():
            self._fault_point("begin")
            yield

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._owns_connection:
                try:
                    self._conn.close()
                except Exception:
                    pass

    def __enter__(self) -> "PostgresAuditStore":
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        self.close()
        return False

    def _select_result(self, owner: str, result_id: str, *, for_update=False):
        sql = ("SELECT result_revision, original_digest, original_scores, "
               "original_scores_digest, created_at FROM results "
               "WHERE tenant_id = %s AND owner_subject = %s "
               "AND result_id = %s")
        if for_update:
            sql += " FOR UPDATE"
        return self._conn.execute(
            sql, (self.tenant_id, owner, result_id)).fetchone()

    def _require_result(self, owner: str, result_id: str):
        row = self._select_result(owner, result_id)
        if row is None:
            # Owner/tenant-scoped miss is indistinguishable from "does not
            # exist": another stream is never visible, here or anywhere else.
            raise AuditStoreError("result_not_registered",
                                  "no result registered for this owner")
        return row

    # -- results (immutable original output reference) --------------------
    def register_result(self, *, owner: Any, result_id: Any,
                        original_output: Any) -> dict:
        owner = _identity(owner, "owner")
        result_id = _identity(result_id, "result_id")
        if not isinstance(original_output, dict) and not hasattr(
                original_output, "keys"):
            raise AuditStoreError("original_output_invalid",
                                  "original output must be a mapping")
        digest = digest_value(original_output)
        scores = original_score_projection(original_output)
        scores_json = canonical_json(scores)
        scores_digest = digest_value(scores)
        with self._transaction():
            try:
                cursor = self._conn.execute(
                    "INSERT INTO results (tenant_id, owner_subject, result_id,"
                    " result_revision, original_digest, original_scores,"
                    " original_scores_digest, created_at)"
                    " VALUES (%s, %s, %s, 0, %s, %s, %s, now())"
                    " ON CONFLICT (tenant_id, owner_subject, result_id)"
                    " DO NOTHING",
                    (self.tenant_id, owner, result_id, digest, scores_json,
                     scores_digest))
                created = bool(cursor and cursor.rowcount == 1)
            except Exception as error:
                raise _map_constraint(error) from error
            row = self._select_result(owner, result_id, for_update=True)
            if row is None:  # FK refusal should have raised above; fail closed
                raise AuditStoreError("tenant_membership_required",
                                      "no membership row for this owner+tenant")
            if row[1] != digest:
                raise AuditStoreError(
                    "original_output_immutable",
                    "a different original output is already registered")
            return {"created": created, "owner": owner,
                    "result_id": result_id, "result_revision": int(row[0]),
                    "original_digest": digest, "original_scores": scores}

    def current_revision(self, *, owner: Any, result_id: Any) -> Optional[int]:
        owner = _identity(owner, "owner")
        result_id = _identity(result_id, "result_id")
        if self._closed:
            raise AuditStoreError("store_closed", "store is closed")
        row = self._select_result(owner, result_id)
        return None if row is None else int(row[0])

    def original_record(self, *, owner: Any, result_id: Any) -> dict:
        owner = _identity(owner, "owner")
        result_id = _identity(result_id, "result_id")
        if self._closed:
            raise AuditStoreError("store_closed", "store is closed")
        row = self._require_result(owner, result_id)
        scores = json.loads(row[2])
        return {"owner": owner, "result_id": result_id,
                "result_revision": int(row[0]), "original_digest": row[1],
                "original_scores": scores, "created_at": row[4]}

    # -- corrections (atomic with their audit event) ----------------------
    def commit_correction(self, *, owner: Any, result_id: Any,
                          expected_revision: Any, idempotency_key: Any,
                          payload: Any, event: Any) -> dict:
        owner = _identity(owner, "owner")
        result_id = _identity(result_id, "result_id")
        key = _identity(idempotency_key, "idempotency_key")
        if not isinstance(payload, dict) and not hasattr(payload, "keys"):
            raise AuditStoreError("payload_invalid",
                                  "correction payload must be a mapping")
        digest = payload_digest(payload)
        fields = audit_store._event_fields(event)
        try:
            expected = int(expected_revision)
        except (TypeError, ValueError) as error:
            raise AuditStoreError("revision_invalid",
                                  "expected_revision must be an integer") from error
        if expected < 0:
            raise AuditStoreError("revision_invalid",
                                  "expected_revision must not be negative")

        with self._transaction():
            # PINNED ORDERING (BCM invariant): the idempotency key is checked
            # BEFORE the revision, so a late retry of a committed operation
            # replays the prior result even though its expected_revision is
            # now stale.
            prior = self._conn.execute(
                "SELECT sequence, event_id, payload_digest, resulting_revision"
                " FROM audit_events WHERE tenant_id = %s AND owner_subject = %s"
                " AND result_id = %s AND idempotency_key = %s",
                (self.tenant_id, owner, result_id, key)).fetchone()
            if prior is not None:
                if prior[2] != digest:
                    raise AuditStoreError(
                        "idempotency_key_payload_mismatch",
                        "idempotency key reused with a different payload")
                return {"status": "duplicate", "owner": owner,
                        "result_id": result_id, "sequence": int(prior[0]),
                        "event_id": prior[1], "idempotency_key": key,
                        "resulting_revision": int(prior[3]),
                        "idempotent": True}

            row = self._select_result(owner, result_id, for_update=True)
            if row is None:
                raise AuditStoreError("result_not_registered",
                                      "no result registered for this owner")
            if expected != int(row[0]):
                raise AuditStoreError(
                    "stale_revision",
                    f"expected revision {expected}, store holds {int(row[0])}")
            previous = self._conn.execute(
                "SELECT event_id FROM audit_events WHERE tenant_id = %s"
                " AND owner_subject = %s AND result_id = %s"
                " AND question_number = %s ORDER BY sequence DESC LIMIT 1",
                (self.tenant_id, owner, result_id,
                 fields["question_number"])).fetchone()
            next_seq = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM audit_events"
                " WHERE tenant_id = %s AND owner_subject = %s"
                " AND result_id = %s",
                (self.tenant_id, owner, result_id)).fetchone()
            sequence = int(next_seq[0])
            resulting = expected + 1
            event_id = uuid.uuid4().hex
            try:
                self._conn.execute(
                    "INSERT INTO audit_events (tenant_id, owner_subject,"
                    " result_id, sequence, event_id, idempotency_key,"
                    " payload_digest, event_type, question_number, action,"
                    " review_status, old_score, new_score, reason,"
                    " expected_revision, resulting_revision,"
                    " supersedes_event_id, actor, binding,"
                    " authorization_json, created_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                    " %s, %s, %s, %s, %s, %s, %s, %s, %s, now())",
                    (self.tenant_id, owner, result_id, sequence, event_id,
                     key, digest, fields["event_type"], fields["question_number"],
                     fields["action"], fields["review_status"],
                     fields["old_score"], fields["new_score"], fields["reason"],
                     expected, resulting,
                     previous[0] if previous is not None else None,
                     fields["actor"], fields["binding"],
                     canonical_json(fields["authorization_evidence"])))
            except Exception as error:
                raise _map_constraint(error) from error
            self._fault_point("after_event_insert")
            self._conn.execute(
                "UPDATE results SET result_revision = %s"
                " WHERE tenant_id = %s AND owner_subject = %s"
                " AND result_id = %s",
                (resulting, self.tenant_id, owner, result_id))
            self._fault_point("before_commit")
            return {"status": "committed", "owner": owner,
                    "result_id": result_id, "sequence": sequence,
                    "event_id": event_id, "idempotency_key": key,
                    "resulting_revision": resulting, "idempotent": False}

    # -- reads, replay, verification --------------------------------------
    def events(self, *, owner: Any, result_id: Any,
               limit: Optional[int] = None) -> list:
        owner = _identity(owner, "owner")
        result_id = _identity(result_id, "result_id")
        if self._closed:
            raise AuditStoreError("store_closed", "store is closed")
        self._require_result(owner, result_id)
        sql = (f"SELECT {_EVENT_COLUMNS} FROM audit_events"
               " WHERE tenant_id = %s AND owner_subject = %s"
               " AND result_id = %s ORDER BY sequence")
        params = [self.tenant_id, owner, result_id]
        if limit is not None:
            sql += " LIMIT %s"
            params.append(int(limit))
        rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [_row_to_event(row) for row in rows]

    def replay(self, *, owner: Any, result_id: Any) -> list:
        owner = _identity(owner, "owner")
        result_id = _identity(result_id, "result_id")
        if self._closed:
            raise AuditStoreError("store_closed", "store is closed")
        row = self._require_result(owner, result_id)
        events = self.events(owner=owner, result_id=result_id)
        problems = _chain_problems(events, result_revision=int(row[0]))
        if problems:
            raise AuditStoreError("audit_order_invalid", "; ".join(problems))
        return events

    def verify(self, *, owner: Any, result_id: Any) -> dict:
        """Integrity report: chain, projections and evidence whitelist.

        PostgreSQL has no SQLite ``PRAGMA integrity_check`` equivalent, so
        ``integrity_check`` reports ``not-applicable-postgresql`` honestly
        instead of pretending to a check that did not run; the chain,
        uniqueness, supersedes and evidence checks all still execute.
        """
        owner = _identity(owner, "owner")
        result_id = _identity(result_id, "result_id")
        problems: list = []
        if self._closed:
            raise AuditStoreError("store_closed", "store is closed")
        row = self._select_result(owner, result_id)
        events: list = []
        if row is None:
            problems.append("result_not_registered")
        else:
            try:
                stored = json.loads(row[2])
            except (TypeError, ValueError):
                stored = None
                problems.append("original_scores_unparseable")
            if stored is not None and digest_value(stored) != row[3]:
                problems.append("original_projection_digest_mismatch")
            try:
                events = self.events(owner=owner, result_id=result_id)
            except AuditStoreError as failure:
                problems.append(f"audit_events_unreadable:{failure.code}")
            else:
                problems.extend(_chain_problems(
                    events, result_revision=int(row[0])))
        return {"ok": not problems, "problems": problems,
                "mode": MODE_LABEL, "schema_version": SCHEMA_VERSION,
                "owner": owner, "result_id": result_id,
                "event_count": len(events),
                "sequences": [event["sequence"] for event in events],
                "original_digest": row[1] if row is not None else "",
                "integrity_check": "not-applicable-postgresql"}


def open_store(dsn: str, *, tenant_id: str) -> PostgresAuditStore:
    """Connect and validate (never create) schema v1 for one tenant.

    The DDL is applied by an explicit operator/CI step; this opener fails
    closed on a missing or unsupported schema version - no auto-migration.
    """
    import psycopg  # lazy: only the Postgres path needs the driver

    if not isinstance(dsn, str) or not dsn.strip():
        raise AuditStoreError("store_unusable",
                              "a PostgreSQL DSN is required (none configured)")
    try:
        connection = psycopg.connect(dsn.strip())
    except Exception as error:
        raise AuditStoreError("store_unusable",
                              "cannot connect to PostgreSQL") from error
    try:
        return PostgresAuditStore(connection, tenant_id=tenant_id,
                                  owns_connection=True)
    except Exception:
        try:
            connection.close()
        except Exception:
            pass
        raise
