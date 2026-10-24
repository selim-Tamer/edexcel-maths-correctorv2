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
import time
from typing import Any, Callable, Iterable, Optional, Tuple, Union

from fastapi import Depends, FastAPI, Header, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import audit_store
import decision_core
import question_review
from auth_oidc import OidcError, OidcVerifier
from membership_store import MembershipError
import membership_store

DEV_STORE_ENV = "AI_MARKER_AUDIT_DEV_STORE"
DEV_ONLY_ENV = "AI_MARKER_AUDIT_DEV_ONLY"

# Untrusted-input guard: every contract body is a few hundred bytes at most
# (reason is capped at 2000 characters), so anything near this size is not a
# legitimate request. Declared Content-Length is checked before the body is
# read; chunked bodies without a length are rejected too, because this API
# never accepts streaming uploads.
MAX_BODY_BYTES = 32_768

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
    "audit_persist_failed": 500,
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

    question_number: str = Field(min_length=1, max_length=256)
    action: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=256)
    reason: str = Field(default="", max_length=2000)
    new_score: Optional[Union[int, str]] = None
    review_status: Optional[str] = Field(default=None, max_length=64)
    old_score: Optional[str] = Field(default=None, max_length=256)
    binding: Optional[str] = Field(default=None, max_length=512)


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


def create_app(*, verify_token: Optional[Callable[[str], Optional[str]]] = None,
               rows_for: Callable[[str, str], Iterable[dict]],
               store_provider: Optional[Callable] = None,
               manual_document_provenance_ok: Optional[Callable[[dict], bool]] = None,
               reverify: Optional[Callable[[dict], tuple]] = None,
               membership_resolver: Optional[Callable[[str], Any]] = None,
               now: Callable[[], str] = _now) -> FastAPI:
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
    * ``rows_for(subject, owner)`` provides owner-scoped row visibility and
      ``store_provider() -> (store, code | None)`` the persistence; both
      still fail closed when absent.
    """
    app = FastAPI(title="ai-maths-marker-api", version="0.1.0-slice-a",
                  docs_url=None, redoc_url=None, openapi_url=None)
    store_provider = store_provider or _default_store_provider()
    provenance_ok = manual_document_provenance_ok or (lambda row: False)
    reverify_row = reverify or _refuse_classroom
    membership_resolver = membership_resolver or membership_store.default_resolver()
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
        declared = request.headers.get("content-length")
        if declared is None:
            return envelope(413, "payload_invalid",
                            "content-length header is required")
        try:
            size = int(declared)
        except ValueError:
            return envelope(413, "payload_invalid",
                            "content-length header is not a number")
        if size < 0 or size > MAX_BODY_BYTES:
            return envelope(413, "payload_invalid", "request body too large")
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
        return subject, "google:" + subject

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

    @app.get("/v1/results/{result_id}/events")
    def read_events(result_id: str,
                    limit: Optional[int] = Query(default=None, ge=1, le=500),
                    who=Depends(actor)):
        _, owner = who
        store = require_store()
        revision = stream_revision(store, owner, result_id)
        if revision == 0:
            # Owner-scoped miss is indistinguishable from "does not exist".
            raise ApiError("result_not_registered", "no result for this owner")
        try:
            events = store.events(owner=owner, result_id=result_id, limit=limit)
        except audit_store.AuditStoreError as failure:
            raise ApiError(failure.code, "event history read failed")
        return {"result_id": result_id, "events": events,
                "result_revision": revision}

    @app.post("/v1/results/{result_id}/decisions")
    def submit_decision(result_id: str, body: DecisionRequest,
                        who=Depends(actor)):
        subject, owner = who
        store = require_store()

        # 1. Owner-scoped row resolution: the durable identity of a visible
        #    row must equal the requested result_id, else nothing is known
        #    about that stream for this owner (fail closed, leak nothing).
        rows = [row for row in (rows_for(subject, owner) or [])
                if isinstance(row, dict)]
        row = next(
            (candidate for candidate in rows
             if candidate.get("report_owner") == owner
             and decision_core.durable_result_identity(candidate, "")
             == result_id), None)
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

    return app
