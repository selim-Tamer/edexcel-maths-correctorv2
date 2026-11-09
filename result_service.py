"""Production result-persistence composition (BR-02A).

WHY THIS MODULE EXISTS
----------------------
``api/app.py`` used to compose only the development SQLite audit store. Jobs,
assignments, documents and membership already selected PostgreSQL from
``AI_MARKER_PG_DSN``, but result/review/audit persistence did not: a production
process answered ``dev_opt_in_required`` for every result route. This service
is the smallest factory that closes that gap:

* ``configured()`` is true exactly when a PostgreSQL DSN is present;
* ``open_service(identity)`` opens ONE tenant-bound connection through
  ``postgres_result_store`` and validates schema v1 + the v9 capability;
* every method translates the API's canonical owner (``google:<subject>``) to
  the raw membership subject that membership foreign keys require, and keeps
  the canonical owner for audit attribution;
* ``close()`` releases the connection on success and failure.

There is deliberately NO fallback to the development registry or SQLite store
when PostgreSQL is selected. A missing DSN, unreachable server, absent v9
capability or missing membership raises the existing ``store_unusable`` /
``tenant_membership_required`` vocabulary; the caller maps it to the existing
error envelope.

The worker path uses ``open_owner_service``: its owner comes from the persisted
marking job, and is mapped back to exactly one active membership subject for
that tenant. No client-supplied value participates.
"""
from __future__ import annotations

import os
from typing import Any, Optional

import audit_store
import identity_context
import postgres_result_store
import runtime_config
from audit_store import AuditStoreError, digest_value


def _dsn(environ: Optional[dict] = None) -> str:
    env = dict(os.environ if environ is None else environ)
    return (env.get(runtime_config.PG_DSN_ENV) or "").strip()


def configured(environ: Optional[dict] = None) -> bool:
    """True when this process is composed with the PostgreSQL result store."""
    return bool(_dsn(environ))


class ResultService:
    """One request-scoped, tenant-bound result/review/audit handle."""

    def __init__(self, store: postgres_result_store.PostgresResultStore,
                 identity: identity_context.IdentityContext,
                 fresh_completion_denied: bool = True):
        self._store = store
        self.identity = identity
        self.fresh_completion_denied = (
            runtime_config.is_production_runtime() or bool(fresh_completion_denied))

    # -- lifecycle --------------------------------------------------------
    @property
    def tenant_id(self) -> str:
        return self.identity.tenant_id

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "ResultService":
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        self.close()
        return False

    # -- identity ---------------------------------------------------------
    def _storage_owner(self, owner: Any) -> str:
        """Translate the canonical API owner to the membership FK value."""
        if owner != self.identity.owner_subject:
            raise AuditStoreError("tenant_membership_required",
                                  "owner does not match the verified identity")
        return self.identity.membership_subject

    # -- writes -----------------------------------------------------------
    def register(self, *, result_id: Any, original_output: Any, scope: Any,
                 review_key: Any, document_digest: Any = "",
                 job_id: Optional[str] = None,
                 assignment_id: Optional[str] = None,
                 classroom: Optional[dict] = None) -> dict:
        """Insert/validate the immutable context AND its audit anchor.

        Refuses fresh production registration during the foundations package,
        independently of route-level checks.
        """
        if runtime_config.is_production_runtime() or self.fresh_completion_denied:
            raise AuditStoreError("store_unusable",
                                  "fresh result completion is disabled during "
                                  "the foundations package")
        return self._store.register_with_context(
            result_id=result_id, original_output=original_output, scope=scope,
            review_key=review_key, document_digest=document_digest,
            job_id=job_id, assignment_id=assignment_id, classroom=classroom)

    def register_result(self, *, owner: Any, result_id: Any,
                        original_output: Any) -> dict:
        """Anchor-only read-only validation used by the decision core.

        Verifies an existing trusted original/anchor is present and that the
        provided original digest matches both the context and anchor digests.
        Never creates a missing anchor/stream and never invokes a write method.
        """
        storage_owner = self._storage_owner(owner)
        context = self._store.context_record(result_id=result_id)
        provided_digest = digest_value(original_output)
        context_digest = context["original_digest"]
        if context_digest != provided_digest:
            raise AuditStoreError(
                "original_output_immutable",
                "a different original output is already registered")
        record = self._store.original_record(owner=storage_owner,
                                              result_id=result_id)
        anchor_digest = record["original_digest"]
        if anchor_digest != provided_digest:
            raise AuditStoreError(
                "original_output_immutable",
                "a different original output is already registered")
        if record.get("original_scores") != audit_store.original_score_projection(original_output):
            raise AuditStoreError(
                "original_output_immutable",
                "the audit anchor marks do not match the original output")
        if context["owner_subject"] != owner:
            raise AuditStoreError(
                "original_output_immutable",
                "context canonical owner does not match verified owner")
        if context["membership_subject"] != storage_owner:
            raise AuditStoreError(
                "original_output_immutable",
                "context membership subject does not match verified identity")
        return {"created": False, "owner": owner,
                "result_id": result_id,
                "result_revision": record["result_revision"],
                "original_digest": provided_digest,
                "original_scores": record.get("original_scores")}

    def commit_correction(self, *, owner: Any, result_id: Any,
                          expected_revision: Any, idempotency_key: Any,
                          payload: Any, event: Any) -> dict:
        actor = (event or {}).get("actor") if hasattr(event, "get") else None
        if actor != self.identity.owner_subject:
            raise AuditStoreError("audit_record_invalid",
                                  "the audit actor is not the verified owner")
        return self._store.commit_correction(
            owner=self._storage_owner(owner), result_id=result_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key, payload=payload, event=event)

    # -- reads ------------------------------------------------------------
    def current_revision(self, *, owner: Any, result_id: Any) -> Optional[int]:
        return self._store.current_revision(
            owner=self._storage_owner(owner), result_id=result_id)

    def events(self, *, owner: Any, result_id: Any,
               limit: Optional[int] = None) -> list:
        return self._store.events(owner=self._storage_owner(owner),
                                  result_id=result_id, limit=limit)

    def replay(self, *, owner: Any, result_id: Any) -> list:
        return self._store.replay(owner=self._storage_owner(owner),
                                  result_id=result_id)

    def original_record(self, *, owner: Any, result_id: Any) -> dict:
        return self._store.original_record(owner=self._storage_owner(owner),
                                           result_id=result_id)

    def verify(self, *, owner: Any, result_id: Any) -> dict:
        return self._store.verify(owner=self._storage_owner(owner),
                                  result_id=result_id)

    def visible_row(self, result_id: Any) -> Optional[dict]:
        return self._store.visible_row(result_id)

    def history(self, *, result_id: Any, limit: Optional[int] = None) -> dict:
        return self._store.history(result_id=result_id, limit=limit)

    def snapshot(self, *, result_id: Any) -> dict:
        return self._store.snapshot(result_id=result_id)


def open_service(identity: identity_context.IdentityContext,
                 *, dsn: Optional[str] = None,
                 fresh_completion_denied: bool = True) -> ResultService:
    """Open the production store for one verified identity (fail closed)."""
    target = dsn if dsn is not None else _dsn()
    store = postgres_result_store.open_store(
        target, identity=identity,
        fresh_completion_denied=fresh_completion_denied)
    return ResultService(store, identity,
                         fresh_completion_denied=fresh_completion_denied)


def open_owner_service(owner: str, tenant_id: str,
                       *, dsn: Optional[str] = None,
                       fresh_completion_denied: bool = True) -> ResultService:
    """Worker/job path: map a TRUSTED stored owner to its membership subject."""
    target = dsn if dsn is not None else _dsn()
    store = postgres_result_store.open_owner_store(
        target, tenant_id=tenant_id, owner=owner,
        fresh_completion_denied=fresh_completion_denied)
    return ResultService(store, store.identity,
                         fresh_completion_denied=fresh_completion_denied)


def require_capability(environ: Optional[dict] = None):
    """Startup gate: connect once and verify schema v1 + the v9 capability.

    Returns the validated ``(schema_version, capability)`` pair. Raises
    ``RuntimeConfigError('store_unusable', ...)`` so a production revision
    fails instead of serving a half-composed API. Never prints the DSN.
    """
    target = _dsn(environ)
    if not target:
        raise runtime_config.RuntimeConfigError(
            "store_unusable",
            [{"code": "store_unusable",
              "detail": f"{runtime_config.PG_DSN_ENV} is required for the "
                        f"PostgreSQL result store"}])
    try:
        import psycopg  # lazy: only the production path needs the driver

        connection = psycopg.connect(target, prepare_threshold=None)
    except Exception as error:
        raise runtime_config.RuntimeConfigError(
            "store_unusable",
            [{"code": "store_unusable",
              "detail": "the PostgreSQL result store is unreachable"}]) from error
    try:
        with connection.transaction():
            schema = connection.execute(
                "SELECT value FROM schema_meta"
                " WHERE key = 'schema_version'").fetchone()
            capability = connection.execute(
                "SELECT value FROM schema_meta WHERE key = %s",
                (postgres_result_store.CAPABILITY_KEY,)).fetchone()
            table = connection.execute(
                "SELECT to_regclass('public.result_context')").fetchone()
    except Exception as error:
        raise runtime_config.RuntimeConfigError(
            "store_unusable",
            [{"code": "store_unusable",
              "detail": "the PostgreSQL result schema could not be read"}]
        ) from error
    finally:
        try:
            connection.close()
        except Exception:
            pass
    if (schema is None or str(schema[0]) != "1"):
        raise runtime_config.RuntimeConfigError(
            "store_unusable",
            [{"code": "store_unusable",
              "detail": "schema_version is not 1; apply the reviewed base "
                        "migrations"}])
    if (capability is None
            or str(capability[0]) != postgres_result_store.CAPABILITY_VERSION
            or table is None or table[0] is None):
        raise runtime_config.RuntimeConfigError(
            "store_unusable",
            [{"code": "store_unusable",
              "detail": "result_context capability missing: apply "
                        "schema/postgres_v9_result_context.sql explicitly"}])
    return str(schema[0]), str(capability[0])
