"""Minimum authenticated FastAPI surface — IMPLEMENTED, not a specification.

This is the first *running* HTTP path over the session-free decision core
(``decision_core``, PR #52). Everything is fail-closed and offline-testable;
nothing here is deployed, provisioned or paid for.

Honest limits (the contract in ``m5_api_contract.json`` (PR #50) stays the
source of truth for what is *promised*; this module records what *runs*):

* **Authentication** is a caller-injected bearer-token verifier. The default
  verifier rejects every token: with no verifier configured, all authenticated
  endpoints return 401. Google OIDC verification, session cookies and refresh
  flows do **not** exist here; wiring a real verifier is a separate slice, and
  it must compare tokens in constant time, never log or echo a token, and
  derive the subject server-side. The owner token is derived as ``google:`` +
  subject, the same convention ``app5_1_automated.sensitive_state_owner`` uses.
* **Bodies are bounded** before parsing (``MAX_BODY_BYTES``, write requests
  only), so an oversized or length-less body is refused with the same error
  envelope instead of being read into memory.
* **Rows** come from a caller-injected owner-scoped provider. There is no
  ingestion endpoint yet (``POST /v1/results`` remains SPEC_NOT_IMPLEMENTED);
  rows that only exist inside a Streamlit session are invisible here, and a
  manual row must carry its server-derived document digest
  (``snapshot_context``) to have a result identity at all.
* **Persistence** is the existing development-only opt-in SQLite audit store
  (``audit_store`` behind ``AI_MARKER_AUDIT_DEV_STORE`` +
  ``AI_MARKER_AUDIT_DEV_ONLY=1``). Without that opt-in, decision and event
  endpoints return ``dev_opt_in_required`` and write nothing. No
  Supabase/Postgres adapter exists; that is unchanged by this module.
* **Classroom rows** are refused: live Classroom re-verification is not wired
  (``reverify`` defaults to ``classroom_verification_unavailable``), and
  manual provenance defaults to failing (``manual_document_provenance_ok``
  defaults to False) until the API-side grant store exists. Tests inject both.
* **Prior decision state** (the sticky-flag rule) is seeded from the durable
  store's latest event for the question, so it survives across clients.
* Client ``expected_revision`` is enforced **by the store's transaction**
  (optimistic concurrency), and idempotent retries replay the prior commit
  instead of appending a second event. The client ``idempotency_key`` is
  accepted and echoed as a correlation token; the *authoritative* key stays
  owner + result + payload, derived server-side exactly as the application
  does. Making the client key authoritative would need one additive optional
  parameter in ``decision_core`` — recorded as an open contract question, not
  silently decided here.

No endpoint here is served by any process in CI: tests drive the app
in-process with ``fastapi.testclient``.
"""
from __future__ import annotations

import os
import hashlib
import time
from contextlib import closing
from typing import Any, Callable, Iterable, Optional, Tuple, Union

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, field_validator

import runtime_config

import audit_store
import decision_core
import document_limits
import document_store
import ingest_registry
import job_store
import postgres_job_store
import postgres_membership_store
import supabase_document_store
import marking_engine
import question_review
from auth_oidc import OidcError, OidcVerifier
from membership_store import MembershipError
import membership_store

DEV_STORE_ENV = "AI_MARKER_AUDIT_DEV_STORE"
DEV_ONLY_ENV = "AI_MARKER_AUDIT_DEV_ONLY"

# Untrusted-input guard: every contract body is a few hundred bytes at most
# (reason is capped at 2000 characters), so anything near this size is not a
# legitimate request. Declared Content-Length is checked before the body is
# read; chunked bodies without a length are rejected too. The single write
# route allowed a larger bound is POST /v1/documents, which is capped at the
# document limits instead (and validated again by document_limits).
MAX_BODY_BYTES = 32_768

# Sentinel distinguishing "build the real engine from configuration" (the
# argument is omitted) from an explicit ``engine=None`` ("no engine wired",
# used by tests to pin the 503 refusal).
_ENGINE_UNSET = object()

# Provisional HTTP mapping for the contract's error codes (the contract names
# codes, not statuses). Anything unmapped is a 400: it came from the source
# vocabulary at the validation gate.
_STATUS_BY_CODE = {
    "authentication_required": 401,
    "manual_document_provenance_required": 403,
    "tenant_membership_required": 403,
    "result_not_registered": 404,
    "review_context_unavailable": 404,
    "review_question_unavailable": 404,
    "review_question_ambiguous": 409,
    "stale_revision": 409,
    "override_write_rejected": 409,
    "idempotency_key_payload_mismatch": 409,
    "dev_opt_in_required": 503,
    "audit_store_unavailable": 503,
    "audit_result_identity_unavailable": 503,
    "store_closed": 503,
    "classroom_verification_unavailable": 503,
    "store_unusable": 503,
    "audit_order_invalid": 500,
    "audit_record_invalid": 500,
    "audit_persist_failed": 500,
    "original_output_immutable": 409,
    # document_limits vocabulary (existing source codes, not invented):
    "document_too_large": 413,
    # marking_requests vocabulary for an unconfigured marking engine:
    "model_unavailable": 503,
}


class ApiError(Exception):
    """One non-2xx response in the contract's error envelope."""

    def __init__(self, code: str, message: str = "", detail: str = "",
                 status: Optional[int] = None):
        self.code = str(code)
        self.message = str(message or self.code)
        self.detail = str(detail or "")
        self.status = int(status or _STATUS_BY_CODE.get(self.code, 400))
        super().__init__(self.code)


class DecisionRequest(BaseModel):
    """``POST /v1/results/{result_id}/decisions`` body (contract subset).

    ``review_status``/``old_score``/``binding`` are optional claims about the
    evidence the client reviewed; when present they are checked against the
    server's own derivation and a mismatch is refused as ``evidence_invalid``
    (server-authoritative, never trusted from the client).
    """

    question_number: str
    action: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=256)
    reason: str = Field(default="", max_length=2000)
    new_score: Optional[Union[int, str]] = None
    review_status: Optional[str] = Field(default=None, max_length=64)
    old_score: Optional[str] = Field(default=None, max_length=256)
    binding: Optional[str] = Field(default=None, max_length=512)

    @field_validator("question_number", mode="before")
    @classmethod
    def _normalise_question_number(cls, value: Any) -> str:
        """Accept the canvas's integer form; store the canonical string.

        Question identity is compared as a stripped string everywhere in the
        decision core (``question_review.question_number_key``). The contract
        asks clients for a string, but the frontend naturally sends
        ``questionNumber: 1``; refusing that was a 400 that stopped the whole
        review flow. Both forms now normalise to ``"1"`` - booleans, floats,
        empty values and over-long ids are still refused.
        """
        if isinstance(value, bool):
            raise ValueError("question_number must be a string or an integer")
        if isinstance(value, int):
            value = str(value)
        if not isinstance(value, str):
            raise ValueError("question_number must be a string or an integer")
        text = value.strip()
        if not text or len(text) > 256:
            raise ValueError("question_number must be 1-256 characters")
        return text


class ClassroomIdentity(BaseModel):
    course_id: str = Field(min_length=1, max_length=256)
    assignment_id: str = Field(min_length=1, max_length=256)
    submission_id: str = Field(min_length=1, max_length=256)
    revision: Optional[str] = Field(default=None, max_length=256)


class ManualIdentity(BaseModel):
    # Server-issued opaque reference; no pupil name is ever accepted here.
    submission_ref: str = Field(min_length=1, max_length=256)
    document_digest: str = Field(min_length=64, max_length=64)


class IngestRequest(BaseModel):
    """``POST /v1/results`` body (contract): register a reviewed result.

    ``result_id`` is NEVER accepted from the client: it is minted server-side
    in the same durable-identity format the decision core derives, so the
    registered stream is immediately addressable by decisions/events/
    projection without any trust in client-supplied identifiers.
    """

    scope: str = Field(min_length=1, max_length=32)
    original_output: dict
    classroom_identity: Optional[ClassroomIdentity] = None
    manual_identity: Optional[ManualIdentity] = None


class JobRequest(BaseModel):
    """``POST /v1/jobs`` body (PROPOSED contract addition).

    The client supplies an idempotency key and the document it wants marked;
    it never chooses the job id, the owner, the tenant or the result id.
    """

    idempotency_key: str = Field(min_length=8, max_length=128)
    document_digest: str = Field(min_length=64, max_length=64)


def _now() -> str:
    return f"{time.time():.0f}"


def _refuse_classroom(row):
    return None, "classroom_verification_unavailable"


def _default_store_provider() -> Callable[[], Tuple[Optional[object], Optional[str]]]:
    """Dev-only opt-in store, mirroring the application's env contract."""
    cache: dict = {}

    def provider():
        path = (os.environ.get(DEV_STORE_ENV) or "").strip()
        if not path:
            return None, None
        acknowledged = (os.environ.get(DEV_ONLY_ENV) or "").strip() == "1"
        if not acknowledged:
            return None, "dev_opt_in_required"
        key = (path, acknowledged)
        if cache.get("key") == key:
            return cache.get("entry"), None
        cache.clear()
        try:
            store = audit_store.open_store(path, dev_only_ack=acknowledged)
        except Exception:
            # Never cached: a transient fault retries and still fails closed.
            return None, "audit_store_unavailable"
        cache["key"] = key
        cache["entry"] = store
        return store, None

    return provider


def _job_api():
    """The job store module for THIS process.

    Production composition: when ``AI_MARKER_PG_DSN`` is configured, job rows
    are durable PostgreSQL (schema 0003) through ``postgres_job_store`` - the
    same lease/attempt semantics and the same error vocabulary as the dev
    store. Otherwise the opt-in SQLite store is used. The choice is per
    process and per configuration, never per request, so call sites can use
    this one facade; both modules share the ``JobStoreError`` class, so the
    error handling is identical either way.
    """
    return postgres_job_store if postgres_job_store.configured() else job_store


def _default_membership_resolver():
    """The membership resolver for THIS process.

    Production: when ``AI_MARKER_PG_DSN`` is configured, the verified subject
    is resolved from PostgreSQL through ``postgres_membership_store`` (schema
    0005, subject-scoped, transaction-local binding). Development: the opt-in
    SQLite membership store. A configured-but-unmigrated database fails closed
    with 403 ``membership_unavailable`` - never open.
    """
    if postgres_membership_store.configured():
        return postgres_membership_store.resolve
    return membership_store.default_resolver()


def _document_api():
    """The document store module for THIS process.

    Production: private Supabase Storage + ``pdf_references`` metadata rows
    (schema 0004) through ``supabase_document_store`` when the bucket
    composition is configured. Development: the opt-in SQLite store. Per
    process and per configuration, exactly like ``_job_api``.
    """
    return (supabase_document_store if supabase_document_store.configured()
            else document_store)


def create_app(*, verify_token: Optional[Callable[[str], Optional[str]]] = None,
               rows_for: Optional[Callable[[str, str], Iterable[dict]]] = None,
               store_provider: Optional[Callable] = None,
               manual_document_provenance_ok: Optional[Callable[[dict], bool]] = None,
               reverify: Optional[Callable[[dict], tuple]] = None,
               membership_resolver: Optional[Callable[[str], Any]] = None,
               authorize_ingest: Optional[Callable[[str, str, dict], bool]] = None,
               engine: Any = _ENGINE_UNSET,
               now: Callable[[], str] = _now,
               allowed_origins: Optional[Iterable[str]] = None) -> FastAPI:
    """Build the app with explicitly injected trust boundaries.

    Defaults are the REAL controls, not a bypass:
    * ``verify_token=None`` builds the Google OIDC **ID-token** verifier
      (``auth_oidc.OidcVerifier``) from ``GOOGLE_OIDC_AUDIENCE``; with no
      audience configured every protected call fails closed with 401
      ``oidc_not_configured``. Only tests pass a fake verifier.
    * ``membership_resolver=None`` uses the env-configured dev membership
      store (``membership_store``); unconfigured/unusable storage fails
      closed with 403 ``membership_unavailable``. Active tenant membership
      is verified server-side on EVERY endpoint that uses ``actor``.
    * ``rows_for(subject, owner)`` defaults to the opt-in development registry and
      ``store_provider() -> (store, code | None)`` the persistence; both
      still fail closed when absent.
    * ``authorize_ingest=None`` defaults to the JOB-BACKED provenance gate:
      client ingest is allowed only for an owner+tenant document whose
      marking job has SUCCEEDED (i.e. the genuine engine output is already
      registered), and denied (no store write) whenever the job store is
      unconfigured or has no matching succeeded job - i.e. the default fails
      closed, never open. The lease-holding runner does not go through this
      status check: it passes its claimed ``job_id`` as its own authority.
    * ``engine`` is omitted => the REAL wiring is attempted via
      ``marking_engine.build_engine()``, which returns a callable only when
      the operator has explicitly opted in (``AI_MARKER_ENGINE_REAL=1`` +
      API key + a ``%PDF`` mark scheme) - i.e. a configured dev box runs the
      existing marking path, and everything else gets ``None``. ``engine=None``
      means "no marking engine" explicitly (tests use this): the synchronous
      job runner then refuses with 503 ``model_unavailable`` instead of
      pretending a result exists. No test or CI run ever spends tokens.
    """
    app = FastAPI(title="ai-maths-marker-api", version="0.1.0-slice-a",
                  docs_url=None, redoc_url=None, openapi_url=None)
    registry_rows = ingest_registry.default_provider()
    store_provider = store_provider or _default_store_provider()
    provenance_ok = manual_document_provenance_ok or (lambda row: False)
    reverify_row = reverify or _refuse_classroom
    membership_resolver = membership_resolver or _default_membership_resolver()
    if engine is _ENGINE_UNSET:
        # Real wiring attempt: a configured opt-in dev box gets the existing
        # marking path; everywhere else this returns None and the runner
        # refuses 503 model_unavailable (never a fabricated result).
        engine = marking_engine.build_engine()
    if verify_token is None:
        audience = (os.environ.get("GOOGLE_OIDC_AUDIENCE") or "").strip()
        if audience:
            _oidc = OidcVerifier(audience=audience)

            def verify_token(token: str) -> str:
                return _oidc.verify(token)  # raises OidcError (static detail)
        else:
            def verify_token(token: str) -> str:
                raise OidcError("oidc_not_configured")

    def envelope(status: int, code: str, message: str, detail: str = ""):
        error = {"code": code, "message": message}
        if detail:
            error["detail"] = detail
        return JSONResponse(status_code=status, content={"error": error})

    @app.middleware("http")
    async def _limit_body_size(request, call_next):
        """Bound the body of write requests before any parsing."""
        if request.method not in ("POST", "PUT", "PATCH"):
            return await call_next(request)
        # Document uploads are bounded by the document limits (and re-checked
        # by document_limits.validate_document); every other write keeps the
        # strict contract cap.
        cap = (document_limits.MAX_DOCUMENT_BYTES
               if request.url.path == "/v1/documents" else MAX_BODY_BYTES)
        declared = request.headers.get("content-length")
        if declared is None:
            return envelope(413, "payload_invalid",
                            "content-length header is required")
        try:
            size = int(declared)
        except ValueError:
            return envelope(413, "payload_invalid",
                            "content-length header is not a number")
        if size < 0 or size > cap:
            return envelope(413, "payload_invalid", "request body too large")
        chunks = []
        observed = 0
        async for chunk in request.stream():
            observed += len(chunk)
            if observed > cap:
                return envelope(413, "payload_invalid", "request body too large")
            chunks.append(chunk)
        # Starlette reuses this bounded cached body for downstream parsing.
        request._body = b"".join(chunks)
        return await call_next(request)

    @app.exception_handler(ApiError)
    async def _api_error(request, exc: ApiError):
        return envelope(exc.status, exc.code, exc.message, exc.detail)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request, exc: RequestValidationError):
        return envelope(400, "payload_invalid", "request body failed validation")

    def actor(authorization: str = Header(default="", alias="Authorization")):
        """Verified subject + server-derived owner + active membership, or 401/403.

        Order: bearer parsing (missing header => detail token_missing) ->
        signature/claims verification (granular STATIC detail per failure
        class: token_expired, token_issuer_mismatch, token_audience_mismatch,
        token_algorithm_not_allowed, token_signature_invalid, ...) ->
        server-side ACTIVE tenant membership (403 tenant_membership_required;
        inactive/absent collapse into one non-enumerating detail).
        The owner is always derived here as ``google:`` + verified subject.
        """
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise ApiError("authentication_required",
                           "a valid bearer token is required",
                           detail="token_missing")
        try:
            subject = verify_token(token.strip())
        except OidcError as failure:
            raise ApiError("authentication_required",
                           "a valid bearer token is required",
                           detail=failure.detail)
        except Exception:
            # Anything unexpected from a verifier fails closed and never
            # echoes internals (or the token).
            raise ApiError("authentication_required",
                           "a valid bearer token is required",
                           detail="token_verification_failed")
        if not isinstance(subject, str) or not subject.strip():
            raise ApiError("authentication_required",
                           "a valid bearer token is required",
                           detail="token_invalid")
        subject = subject.strip()
        try:
            membership = membership_resolver(subject)
        except MembershipError as failure:
            raise ApiError("tenant_membership_required",
                           "an active tenant membership is required",
                           detail=failure.detail)
        except Exception:
            raise ApiError("tenant_membership_required",
                           "an active tenant membership is required",
                           detail="membership_unavailable")
        if membership is None:
            raise ApiError("tenant_membership_required",
                           "an active tenant membership is required",
                           detail="membership_inactive_or_absent")
        if (not isinstance(membership, membership_store.Membership)
                or membership.subject != subject or not membership.tenant_id):
            raise ApiError("tenant_membership_required",
                           "an active tenant membership is required",
                           detail="membership_unavailable")
        return subject, "google:" + subject, membership.tenant_id

    def require_store():
        store, code = store_provider()
        if code:
            raise ApiError(code, "persistence is unavailable for this request")
        if store is None:
            raise ApiError("dev_opt_in_required",
                           "the development audit store is not opted in "
                           f"({DEV_STORE_ENV} + {DEV_ONLY_ENV}=1)")
        return store

    def stream_revision(store, owner: str, result_id: str) -> int:
        try:
            current = store.current_revision(owner=owner, result_id=result_id)
        except audit_store.AuditStoreError as failure:
            raise ApiError(failure.code, "store read failed")
        except Exception:
            raise ApiError("audit_store_unavailable", "store read failed")
        return int(current or 0)

    @app.get("/v1/healthz")
    def healthz():
        return {"status": "ok"}

    def visible_row(subject: str, owner: str, tenant_id: str, result_id: str):
        """The ONE owner-visible row whose durable identity equals result_id.

        Shared by reads and writes so both obey the same visibility rule:
        if the provider no longer lists the row for this owner, NOTHING
        about that stream is reachable - history included (finding O-1
        decision: reads are gated too; audit-export paths are future work).
        """
        candidates = (rows_for(subject, owner) if rows_for is not None
                      else registry_rows(subject, owner, tenant_id))
        rows = [row for row in (candidates or [])
                if isinstance(row, dict)]
        return next(
            (candidate for candidate in rows
             if candidate.get("report_owner") == owner
             and candidate.get("tenant_id", tenant_id) == tenant_id
             and decision_core.durable_result_identity(candidate, "")
             == result_id), None)

    @app.get("/v1/results/{result_id}/events")
    def read_events(result_id: str,
                    limit: Optional[int] = Query(default=None, ge=1, le=500),
                    who=Depends(actor)):
        subject, owner, tenant_id = who
        if visible_row(subject, owner, tenant_id, result_id) is None:
            # Same indistinguishable denial as the write path.
            raise ApiError("result_not_registered", "no result for this owner")
        store = require_store()
        revision = stream_revision(store, owner, result_id)
        if store.current_revision(owner=owner, result_id=result_id) is None:
            # A registered zero-event stream is valid; absence stays private.
            raise ApiError("result_not_registered", "no result for this owner")
        try:
            events = store.events(owner=owner, result_id=result_id, limit=limit)
        except audit_store.AuditStoreError as failure:
            raise ApiError(failure.code, "event history read failed")
        return {"result_id": result_id, "events": events,
                "result_revision": revision}

    def job_backed_ingest_authorizer(subject: str, tenant_id: str,
                                     row: dict) -> bool:
        """Default provenance gate: a COMPLETED server-side job authorizes.

        The row's ``snapshot_context`` is the document digest; client ingest
        is allowed only when this exact owner+tenant has a SUCCEEDED marking
        job for that document (the genuine output is already registered).
        Unconfigured job store, missing row, an in-flight ``running`` job or
        any store failure means False - the default fails closed, and the
        claim->succeed window can never be pre-empted (finding J-1).
        """
        digest = str(row.get("snapshot_context") or "")
        owner = str(row.get("report_owner") or "")
        if not digest or not owner:
            return False
        try:
            with closing(_job_api().require_connection()) as connection:
                job_id = _job_api().find_authorizable_job(
                    connection, tenant_id=tenant_id, owner=owner,
                    document_digest=digest)
        except job_store.JobStoreError:
            return False
        return job_id is not None

    authorize_ingest = authorize_ingest or job_backed_ingest_authorizer

    def runner_authority_ok(job_id: str, tenant_id: str, owner: str,
                            digest: str) -> bool:
        """Server-side proof that THIS request holds the job's live lease.

        The runner is inside the claim it just made: the row must be
        ``running`` with an unexpired ``lease_until`` for exactly this
        owner+tenant+document. A client cannot reach this code path (only
        the run endpoint passes a ``runner_job_id``), and a stale/expired
        lease fails the check - stronger evidence than a status string
        (finding J-1 prescribed fix, part 2).
        """
        try:
            with closing(_job_api().require_connection()) as connection:
                job = _job_api().get_job(connection, tenant_id=tenant_id,
                                        owner=owner, job_id=job_id)
        except job_store.JobStoreError:
            return False
        return bool(job and job["status"] == "running"
                    and int(job["lease_until"]) > int(time.time())
                    and job["document_digest"] == digest
                    and job["tenant_id"] == tenant_id
                    and job["owner_subject"] == owner)

    def finish_ingest(*, subject: str, owner: str, tenant_id: str,
                      scope: str, original: dict, digest: str,
                      review_key: str, result_id: str, revision: str,
                      parts: dict, runner_job_id: str = None) -> dict:
        """Shared registration path for the API and the marking-job runner.

        Order (unchanged from the contract slice): shape validation of the
        original -> trusted-provenance gate -> audit anchor FIRST ->
        registry row. Authority for the gate is EITHER the client-facing
        job-backed check (succeeded jobs only) OR, when the caller is the
        lease-holding runner, its live claim (``runner_job_id``).
        """
        if not isinstance(original, dict):
            raise ApiError("original_output_invalid",
                           "original_output must be a mapping")
        if not isinstance(original.get("questions"), list):
            raise ApiError("original_output_invalid",
                           "original_output must carry a questions list")
        row = {"status": "Marked", "review_key": review_key,
               "report_owner": owner, "snapshot_context": digest,
               "tenant_id": tenant_id,
               "revision": revision, "result": original,
               "ai_result": original, "student_name": "",
               "marking_model": "ingested", "audit_model": "Not run"}
        if scope == "classroom":
            row.update({"classroom_course_id": parts["course"],
                        "classroom_assignment_id": parts["assignment"],
                        "submission_id": parts["submission"]})
        # A client-supplied digest/reference or original output is not a
        # server-issued marking result: the trusted job/provenance service
        # must authorize the exact owner/tenant/identity/output tuple. The
        # lease-holding runner authorizes with its own live claim instead of
        # the client-facing status check (finding J-1).
        if runner_job_id is not None:
            authorized = runner_authority_ok(runner_job_id, tenant_id,
                                             owner, digest)
        else:
            try:
                authorized = (authorize_ingest is not None and
                              authorize_ingest(subject, tenant_id, row) is True)
            except Exception:
                authorized = False
        if not authorized:
            raise ApiError("manual_document_provenance_required",
                           "a server-authorized result is required",
                           detail="ingest_authorization_required")
        store = require_store()
        try:
            with closing(ingest_registry.require_connection()) as connection:
                registered = store.register_result(
                    owner=owner, result_id=result_id, original_output=original)
                ingest_registry.put_row(
                    connection, tenant_id=tenant_id, owner=owner,
                    result_id=result_id, scope=scope, review_key=review_key,
                    document_digest=digest, row=row)
        except audit_store.AuditStoreError as failure:
            raise ApiError(failure.code, "registration refused")
        except ingest_registry.RegistryError as failure:
            raise ApiError(failure.detail,
                           "the ingest registry row could not be written",
                           detail=failure.detail)
        return {"result_id": result_id, "owner": owner,
                "result_revision": registered["result_revision"],
                # Interpretation of the contract's audit_reference for a
                # zero-event registration: the immutable original digest.
                "audit_reference": registered["original_digest"],
                "supersedes": None}

    @app.post("/v1/results", status_code=201)
    def ingest_result(body: IngestRequest, who=Depends(actor)):
        """Contract ingest: register an immutable original + its row.

        Server mints result_id (never client-supplied); the audit-store
        registration happens FIRST (the immutable anchor), then the dev
        registry row that makes the stream visible through the same
        owner-scoped rule as everything else. A registry failure after a
        successful registration leaves an invisible-but-immutable stream;
        a re-ingest completes the row idempotently.
        """
        subject, owner, tenant_id = who
        scope = (body.scope or "").strip().lower()
        if scope not in ("manual", "classroom"):
            raise ApiError("payload_invalid", "scope must be manual or classroom")
        original = body.original_output
        if not isinstance(original, dict):
            raise ApiError("original_output_invalid",
                           "original_output must be a mapping")
        if not isinstance(original.get("questions"), list):
            raise ApiError("original_output_invalid",
                           "original_output must carry a questions list")

        digest = ""
        revision = ""
        submission = ""
        parts: dict = {}
        if scope == "manual":
            if body.manual_identity is None:
                raise ApiError("payload_invalid",
                               "manual_identity is required for manual scope")
            digest = body.manual_identity.document_digest.strip().lower()
            if len(digest) != 64 or any(c not in "0123456789abcdef"
                                        for c in digest):
                raise ApiError("payload_invalid",
                               "document_digest must be a sha256 hex digest")
            reference = body.manual_identity.submission_ref.strip()
            if (not reference or len(reference) > 256 or "/" in reference
                    or any(ord(c) < 32 for c in reference)):
                raise ApiError("payload_invalid",
                               "submission_ref is not a valid server-issued id")
            review_key = reference
            result_id = "manual:" + digest[:16] + ":" + reference
        else:
            if body.classroom_identity is None:
                raise ApiError("payload_invalid",
                               "classroom_identity is required for classroom scope")
            identity = body.classroom_identity
            parts = {}
            for name, value in (("course", identity.course_id),
                                ("assignment", identity.assignment_id),
                                ("submission", identity.submission_id)):
                cleaned = (value or "").strip()
                if (not cleaned or len(cleaned) > 256 or "/" in cleaned
                        or any(ord(c) < 32 for c in cleaned)):
                    raise ApiError("payload_invalid",
                                   f"{name} id is not a valid server identity")
                parts[name] = cleaned
            revision = (identity.revision or "").strip()
            if revision and ("/" in revision or len(revision) > 256
                             or any(ord(c) < 32 for c in revision)):
                raise ApiError("payload_invalid", "revision is not valid")
            submission = parts["submission"]
            review_key = submission
            result_id = ("classroom:" + parts["course"] + ":"
                         + parts["assignment"] + ":" + submission + ":"
                         + (revision or "unversioned"))

        return finish_ingest(
            subject=subject, owner=owner, tenant_id=tenant_id, scope=scope,
            original=original, digest=digest, review_key=review_key,
            result_id=result_id, revision=revision, parts=parts)

    @app.get("/v1/results/{result_id}/projection")
    def project_result(result_id: str, who=Depends(actor)):
        """Contract projection: original + corrections ALONGSIDE, never over.

        Originals come from the owner-visible row (the store deliberately
        never holds answers/working text); corrections are the store's
        chained events, latest-per-question wins; only a ``corrected``
        status changes an effective mark (flags/approvals never do — the
        single-override rule). Invariant: ``original_ai_output`` is returned
        untouched and the store is never mutated.
        """
        subject, owner, tenant_id = who
        row = visible_row(subject, owner, tenant_id, result_id)
        if row is None:
            raise ApiError("result_not_registered", "no result for this owner")
        store = require_store()
        try:
            original_record = store.original_record(
                owner=owner, result_id=result_id)
            events = store.replay(owner=owner, result_id=result_id)
        except audit_store.AuditStoreError as failure:
            raise ApiError(failure.code, "projection read failed")

        pristine = row.get("ai_result") or row.get("result") or {}
        if audit_store.digest_value(pristine) != original_record["original_digest"]:
            raise ApiError("audit_record_invalid", "the original output changed")
        questions = pristine.get("questions") if isinstance(pristine, dict) \
            else None
        if not isinstance(questions, list):
            questions = []
        latest = {}
        for event in events:  # stream order: later events win
            latest[str(event.get("question_number"))] = event
        earned = 0.0
        available = 0.0
        projected = []
        for question in questions:
            number = str(question.get("question_number", ""))
            original_marks = question.get("awarded_marks")
            max_marks = question.get("max_marks")
            event = latest.get(number)
            status = (event or {}).get("review_status") or "unreviewed"
            effective = original_marks
            corrected = False
            if status == "corrected":
                try:
                    effective = int((event or {}).get("new_score"))
                except (TypeError, ValueError):
                    raise ApiError("audit_record_invalid",
                                   "a stored correction is unreadable")
                corrected = True
            entry = {"question_number": number, "max_marks": max_marks,
                     "original_awarded_marks": original_marks,
                     "effective_awarded_marks": effective,
                     "corrected": corrected, "review_status": status}
            if event is not None:
                entry["last_action"] = event.get("action")
                entry["last_reason"] = event.get("reason")
            projected.append(entry)
            if isinstance(effective, (int, float)):
                earned += float(effective)
            if isinstance(max_marks, (int, float)):
                available += float(max_marks)
        return {"result_id": result_id,
                "result_revision": original_record["result_revision"],
                "questions": projected,
                "original_ai_output": pristine,
                "totals": {"earned": earned, "available": available,
                           "percentage": (round(earned * 100.0 / available, 2)
                                          if available else 0.0)}}

    def require_document_connection():
        """The document store for THIS process, failing closed when unconfigured.

        Production: the private Supabase Storage adapter (schema 0004) when
        the bucket composition is configured. Development: the opt-in SQLite
        store. Neither path creates schema or a bucket.
        """
        if supabase_document_store.configured():
            try:
                return supabase_document_store.require_connection()
            except document_store.DocumentStoreError:
                raise ApiError("store_unusable", "the document store is unusable")
        path = (os.environ.get(document_store.PATH_ENV) or "").strip()
        acknowledged = (os.environ.get(document_store.ACK_ENV) or "").strip() == "1"
        if not path or not acknowledged:
            raise ApiError("dev_opt_in_required",
                           "the document store is not configured for development")
        try:
            return document_store.open_store(path)
        except document_store.DocumentStoreError:
            raise ApiError("store_unusable", "the document store is unusable")

    @app.post("/v1/documents", status_code=201)
    async def upload_document(request: Request,
                              filename: str = Query(default="", max_length=256),
                              who=Depends(actor)):
        """Private document adapter: validate, hash, store - never publish.

        The raw PDF bytes are the request body (already bounded by the
        middleware at the document limits). The server computes the sha256
        digest - the client never supplies the key - validates through
        ``document_limits`` (signature, size, page cap, encryption), then
        stores the bytes under owner+tenant scope. Re-uploading identical
        content is idempotent (200 + stored:false); no URL, no presigning,
        no anonymous route exists anywhere.
        """
        subject, owner, tenant_id = who
        data = bytes(await request.body())
        try:
            info = document_limits.validate_document(data, filename or None)
        except document_limits.DocumentValidationError as failure:
            # Content-free category message from the existing vocabulary.
            raise ApiError(failure.code.lower(), str(failure))
        digest = hashlib.sha256(data).hexdigest()
        connection = require_document_connection()
        try:
            with closing(connection):
                created = _document_api().put_document(
                    connection, tenant_id=tenant_id, owner=owner,
                    document_digest=digest, filename=filename or "",
                    page_count=info.get("pages"), content=data)
        except document_store.DocumentStoreError as failure:
            raise ApiError(failure.detail, "the document could not be stored")
        receipt = {"document_digest": digest, "owner": owner,
                   "bytes": len(data), "pages": info.get("pages"),
                   "kind": info.get("kind"), "stored": created}
        if not created:
            return JSONResponse(status_code=200, content=receipt)
        return receipt

    @app.get("/v1/documents/{document_digest}")
    def read_document(document_digest: str, who=Depends(actor)):
        """Owner+tenant-scoped private read of stored PDF bytes.

        A foreign, unknown or malformed digest is the SAME 404
        ``result_not_registered`` (indistinguishable, enumeration-safe), and
        the response carries no-store caching headers so intermediaries do
        not keep a student document.
        """
        subject, owner, tenant_id = who
        digest = (document_digest or "").strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ApiError("result_not_registered", "no document for this owner")
        connection = require_document_connection()
        try:
            with closing(connection):
                content = _document_api().find_document(
                    connection, tenant_id=tenant_id, owner=owner,
                    document_digest=digest)
        except document_store.DocumentStoreError as failure:
            raise ApiError(failure.detail, "the document could not be read")
        if content is None:
            raise ApiError("result_not_registered", "no document for this owner")
        return Response(content=content, media_type="application/pdf",
                        headers={"Cache-Control": "private, no-store",
                                 "X-Content-Type-Options": "nosniff"})

    def require_job_connection():
        """The job store for THIS process, failing closed when unconfigured.

        Production: durable PostgreSQL (schema 0003) through
        ``postgres_job_store`` when ``AI_MARKER_PG_DSN`` is configured.
        Development: the opt-in SQLite store. Neither path creates schema -
        the operator applies the migrations explicitly - and both keep the
        store's own fail-closed error codes.
        """
        if postgres_job_store.configured():
            try:
                return postgres_job_store.require_connection()
            except job_store.JobStoreError:
                raise ApiError("store_unusable", "the job store is unusable")
        path = (os.environ.get(job_store.PATH_ENV) or "").strip()
        acknowledged = (os.environ.get(job_store.ACK_ENV) or "").strip() == "1"
        if not path or not acknowledged:
            raise ApiError("dev_opt_in_required",
                           "the job store is not configured for development")
        try:
            return job_store.open_store(path)
        except job_store.JobStoreError:
            raise ApiError("store_unusable", "the job store is unusable")

    def _marks_only(owner: Optional[str], result_id: str):
        """Marks-only question projection for one registered result.

        Reads the audit store's stored ``original_scores`` (question number +
        awarded marks only - never answers or working text). Returns ``None``
        whenever it cannot be read (unconfigured/unusable store, unknown
        result): the caller then omits the field instead of guessing, and the
        full result stays readable through the projection endpoint.
        """
        if not owner:
            return None
        try:
            store = require_store()
            record = store.original_record(owner=owner, result_id=result_id)
        except Exception:
            return None
        scores = record.get("original_scores") if isinstance(record, dict) else None
        if not isinstance(scores, list):
            return None
        return [{"question_number": pair[0], "awarded_marks": pair[1]}
                for pair in scores
                if isinstance(pair, (list, tuple)) and len(pair) == 2]

    def job_payload(job: dict) -> dict:
        """Job state as served: no tenant, no key, no document bytes.

        A ``succeeded`` job additionally carries its question results as the
        MARKS-ONLY projection (question number + awarded marks), so a demo
        client can render the outcome without a second round trip and without
        student text ever passing through a status response.
        """
        payload = {"job_id": job["job_id"], "status": job["status"],
                   "attempts": int(job["attempts"]),
                   "max_attempts": int(job["max_attempts"]),
                   "document_digest": job["document_digest"],
                   "result_id": job["result_id"],
                   "error_code": job["error_code"]}
        if job["status"] == "succeeded" and job.get("result_id"):
            questions = _marks_only(job.get("owner_subject"), job["result_id"])
            if questions is not None:
                payload["questions"] = questions
        return payload

    @app.post("/v1/jobs", status_code=201)
    def create_marking_job(body: JobRequest, who=Depends(actor)):
        """Create (or replay) one durable marking job - idempotent.

        The referenced document must already exist for THIS owner+tenant,
        otherwise the ordinary indistinguishable 404. Replaying the same key
        with the same digest returns the SAME job (200 + current state);
        reusing the key for a different document is refused 409
        ``idempotency_key_payload_mismatch``.
        """
        subject, owner, tenant_id = who
        digest = (body.document_digest or "").strip().lower()
        if len(digest) != 64 or any(c not in "0123456789abcdef"
                                    for c in digest):
            raise ApiError("payload_invalid",
                           "document_digest must be a sha256 hex digest")
        key = (body.idempotency_key or "").strip()
        if (not key or len(key) > 128 or "/" in key
                or any(ord(c) < 32 for c in key)):
            raise ApiError("payload_invalid",
                           "idempotency_key is not a valid server-safe id")
        with closing(require_document_connection()) as documents:
            stored = _document_api().find_document(
                documents, tenant_id=tenant_id, owner=owner,
                document_digest=digest)
        if stored is None:
            raise ApiError("result_not_registered", "no document for this owner")
        connection = require_job_connection()
        try:
            with closing(connection):
                created = _job_api().create_job(
                    connection, tenant_id=tenant_id, owner=owner,
                    idempotency_key=key, document_digest=digest)
                current = _job_api().get_job(
                    connection, tenant_id=tenant_id, owner=owner,
                    job_id=created["job_id"])
        except job_store.JobStoreError as failure:
            raise ApiError(failure.detail, "the job could not be created")
        payload = job_payload(current)
        payload["owner"] = owner
        payload["created"] = bool(created["created"])
        if not created["created"]:
            return JSONResponse(status_code=200, content=payload)
        return payload

    @app.get("/v1/jobs/{job_id}")
    def read_marking_job(job_id: str, who=Depends(actor)):
        """Owner+tenant-scoped durable status; misses are indistinguishable."""
        subject, owner, tenant_id = who
        connection = require_job_connection()
        try:
            with closing(connection):
                job = _job_api().get_job(connection, tenant_id=tenant_id,
                                        owner=owner, job_id=job_id)
        except job_store.JobStoreError as failure:
            raise ApiError(failure.detail, "the job could not be read")
        if job is None:
            raise ApiError("result_not_registered", "no job for this owner")
        return job_payload(job)

    @app.post("/v1/jobs/{job_id}/run")
    def run_marking_job(job_id: str, who=Depends(actor)):
        """Synchronously execute one job (dev/demo worker, lease-guarded).

        Claim -> engine(document) -> SAME registration path as
        ``POST /v1/results`` (so the job-backed provenance gate authorizes
        it) -> success. Failures are settled with an existing-vocabulary
        error code and requeued while attempts remain; an expired lease is
        reclaimable, a live lease is never stolen. The engine is injected;
        without one this refuses 503 ``model_unavailable`` rather than
        fabricating a result.
        """
        subject, owner, tenant_id = who
        connection = require_job_connection()
        try:
            with closing(connection):
                job = _job_api().get_job(connection, tenant_id=tenant_id,
                                        owner=owner, job_id=job_id)
                if job is None:
                    raise ApiError("result_not_registered",
                                   "no job for this owner")
                if engine is None:
                    raise ApiError("model_unavailable",
                                   "no marking engine is configured")
                with closing(require_document_connection()) as documents:
                    content = _document_api().find_document(
                        documents, tenant_id=tenant_id, owner=owner,
                        document_digest=job["document_digest"])
                claimed_job, claimed = _job_api().claim_job(
                    connection, tenant_id=tenant_id, owner=owner,
                    job_id=job_id)
                if claimed_job is None:
                    raise ApiError("result_not_registered",
                                   "no job for this owner")
                if not claimed:
                    # Succeeded/failed, or another worker holds a live lease:
                    # report state, never double-run.
                    return job_payload(claimed_job)
                if content is None:
                    # The store lost the document: settle this attempt with a
                    # static code instead of executing.
                    _job_api().settle_failure(
                        connection, tenant_id=tenant_id, owner=owner,
                        job_id=job_id, error_code="result_not_registered")
                    return job_payload(_job_api().get_job(
                        connection, tenant_id=tenant_id, owner=owner,
                        job_id=job_id))
                digest = job["document_digest"]
                try:
                    original = engine(content)
                    receipt = finish_ingest(
                        subject=subject, owner=owner, tenant_id=tenant_id,
                        scope="manual", original=original, digest=digest,
                        review_key=job_id,
                        result_id="manual:" + digest[:16] + ":" + job_id,
                        revision="", parts={},
                        runner_job_id=job_id)
                except ApiError as failure:
                    _job_api().settle_failure(
                        connection, tenant_id=tenant_id, owner=owner,
                        job_id=job_id, error_code=failure.code)
                except Exception:
                    # Engine/transport failure: static existing-vocabulary
                    # outcome, retried until max_attempts is exhausted.
                    _job_api().settle_failure(
                        connection, tenant_id=tenant_id, owner=owner,
                        job_id=job_id, error_code="service_unavailable")
                else:
                    _job_api().succeed_job(
                        connection, tenant_id=tenant_id, owner=owner,
                        job_id=job_id, result_id=receipt["result_id"])
                return job_payload(_job_api().get_job(
                    connection, tenant_id=tenant_id, owner=owner,
                    job_id=job_id))
        except job_store.JobStoreError as failure:
            raise ApiError(failure.detail, "the job could not be run")

    @app.post("/v1/results/{result_id}/decisions")
    def submit_decision(result_id: str, body: DecisionRequest,
                        who=Depends(actor)):
        subject, owner, tenant_id = who
        store = require_store()

        # 1. Owner-scoped row resolution: the durable identity of a visible
        #    row must equal the requested result_id, else nothing is known
        #    about that stream for this owner (fail closed, leak nothing).
        row = visible_row(subject, owner, tenant_id, result_id)
        if row is None:
            raise ApiError("result_not_registered", "no result for this owner")

        student_key = row.get("review_key")
        if not isinstance(student_key, str) or not student_key:
            raise ApiError("review_context_unavailable",
                           "row carries no review key")

        # 2. Server-authoritative evidence claims (optional client fields).
        question, identity = decision_core.question_identity_status(
            row, body.question_number)
        claimed_status = {"approve": "approved", "correct": "corrected",
                          "flag": "flagged"}.get(body.action)
        if body.review_status is not None and body.review_status != claimed_status:
            raise ApiError("evidence_invalid",
                           "review_status does not match the requested action")
        if body.old_score is not None:
            actual = "" if question is None or question.get("awarded_marks") is None \
                else str(question.get("awarded_marks"))
            if body.old_score != actual:
                raise ApiError("evidence_invalid",
                               "old_score does not match the stored AI marks")
        if body.binding is not None and identity == question_review.QUESTION_IDENTITY_OK:
            expected_binding = decision_core.question_review_binding(
                student_key, row, body.question_number, "")
            if body.binding != expected_binding:
                raise ApiError("evidence_invalid",
                               "binding does not match the reviewed evidence")

        # 3. Normalise the contract's string score form for the core.
        corrected_mark = body.new_score
        if isinstance(corrected_mark, str):
            try:
                corrected_mark = int(corrected_mark)
            except ValueError:
                pass  # the core's validator rejects it with its real code

        # 4. Durable prior state: seed the sticky-flag/ prior-binding rule
        #    from the store's latest event for this question.
        state: dict = {}
        before = stream_revision(store, owner, result_id)
        if before:
            key = decision_core.decision_key(student_key, body.question_number)
            prior = None
            for event in store.events(owner=owner, result_id=result_id):
                if str(event.get("question_number")) == str(body.question_number):
                    prior = event
            if prior is not None:
                state.setdefault(decision_core.DECISIONS_KEY, {})[key] = {
                    "status": prior.get("review_status"),
                    "binding": prior.get("binding"),
                    "reason": prior.get("reason"),
                }

        notices: dict = {}

        def fail(code, question_number, detail=""):
            notices["value"] = (code, str(question_number), str(detail or ""))

        def write_override(student, question_number, mark, reason):
            # The API has no widgets; the per-request state mirror keeps the
            # core's write-then-verify gate intact.
            state.setdefault("teacher_overrides", {}).setdefault(
                student, {})[question_number] = int(mark)
            state.setdefault("teacher_override_reasons", {}).setdefault(
                student, {})[question_number] = reason

        def plan(candidate_row, actor_owner):
            identity_id = decision_core.durable_result_identity(candidate_row, "")
            if identity_id is None:
                return None, None, "audit_result_identity_unavailable"
            return store, {"result_id": identity_id,
                           "expected_revision": int(body.expected_revision)}, None

        decision_core.apply_review_decision(
            state, student_key=student_key, question_number=body.question_number,
            action=body.action, reason=body.reason, corrected_mark=corrected_mark,
            env=decision_core.DecisionEnv(
                subject=subject,
                owner=owner,
                resolve_row=lambda: row,
                row_owner_allowed=lambda candidate:
                candidate.get("report_owner") == owner,
                manual_context="",
                reverify=reverify_row,
                manual_provenance_ok=provenance_ok,
                plan_resolver=plan,
                write_override=write_override,
                fail=fail,
                # The durable audit event is the store's; the session-style
                # trail has no meaning for a stateless request.
                record_event=lambda *args, **kwargs: None,
                now=now,
            ))

        code, _question_number, detail = notices.get("value", (None, "", ""))
        if code != "ok":
            raise ApiError(code or "audit_persist_failed",
                           "decision refused by a fail-closed gate", detail)

        record = state.get(decision_core.DECISIONS_KEY, {}).get(
            decision_core.decision_key(student_key, body.question_number)) or {}
        reference = record.get("audit_reference") or {}
        after = stream_revision(store, owner, result_id)
        return {
            "result_id": reference.get("result_id", result_id),
            "result_revision": after,
            "event_sequence": reference.get("sequence", 0),
            # Provisional string form: stream + sequence are returned above;
            # the event id is the stable handle a client can quote back.
            "audit_reference": reference.get("event_id", ""),
            "replayed": after == before and before > 0,
            "idempotency_key": body.idempotency_key,
        }

    # Browser CORS (IFR audit P0-2): without this every cross-origin JSON or
    # Authorization request from the Pages deployment fails preflight. The
    # allowlist is EXACT origins from AI_MARKER_ALLOWED_ORIGINS - no wildcard,
    # no credentials, and a missing origin is refused rather than defaulted.
    # Added last so it is the outermost middleware: error envelopes and the
    # body-size refusal carry the same CORS decision as a success response.
    production = ((os.environ.get(runtime_config.RUNTIME_ENV) or "")
                  .strip().lower() == runtime_config.RUNTIME_PRODUCTION)
    try:
        origins = runtime_config.parse_allowed_origins(
            ",".join(allowed_origins) if allowed_origins is not None
            else (os.environ.get(runtime_config.ALLOWED_ORIGINS_ENV) or ""),
            production=production)
    except ValueError as failure:
        raise runtime_config.RuntimeConfigError(
            "service_unavailable",
            [{"code": "service_unavailable",
              "detail": f"{runtime_config.ALLOWED_ORIGINS_ENV}: {failure}"}])
    if production and not origins:
        # A production process with no browser origin can serve no browser
        # client; refuse rather than deploy a service that fails every
        # preflight. ``api/server.py`` checks this too - this covers direct
        # create_app() callers.
        raise runtime_config.RuntimeConfigError(
            "service_unavailable",
            [{"code": "service_unavailable",
              "detail": f"{runtime_config.ALLOWED_ORIGINS_ENV} is required in "
                        f"production"}])
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Accept"],
            expose_headers=[],
            max_age=600,
        )

    # Internal worker seam, NOT a route: the worker composition executes one
    # job through this exact reviewed handler - same claim, same engine call,
    # same registration path, same settle rules - instead of reimplementing
    # them. Nothing new is exposed over HTTP by this line.
    app.state.run_marking_job = run_marking_job

    return app
