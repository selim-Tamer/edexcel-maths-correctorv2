"""Trusted teacher authorization adapter between app auth state and C6-A.

Phase C6-B1. This module connects the application's server-authenticated
Google session (Streamlit OIDC, consumed through a trusted identity function)
to the C6-A ``scheme_authorization.ApprovalAuthority`` boundary. It registers
verified teacher grants — course access, assignment access with a stable
revision, and manual-upload document provenance — and re-verifies all of them
on every authority call. It authenticates nobody itself and exposes no
endpoint, no UI and no network call.

TRUST BOUNDARY
--------------
- Identity is derived only from the server-side authenticated subject handed
  in by trusted application code (production: ``authenticated_google_identity``,
  which reads the Streamlit OIDC session). Browser-submitted values are never
  accepted: every entry point re-checks that the caller's subject equals the
  subject captured when the session authority was created. An OAuth subject
  alone is never treated as proof of Classroom teacher permission.
- Course/assignment grants are recorded only from trusted, server-fetched
  Classroom listings (``list_teacher_courses`` / ``list_assignments`` using the
  authenticated teacher's own access token). A course ID typed into the app,
  a cached list or any client-controlled value is not proof.
- Each authenticated (subject, login session) owns exactly one
  ``ApprovalAuthority`` with a fresh ``secrets.token_urlsafe(32)`` nonce. The
  authority and its grant store live in server-side Streamlit session state
  only; they are never serialized, logged, or placed in URLs. One Streamlit
  session can never observe another session's authority object.
- Explicit logout, an authenticated-subject change (account switch), anonymous
  replacement, expired sessions and malformed stored state all revoke the
  authority and drop every grant; previously issued contexts can then never
  validate again (removal from the authority registry is permanent).

DOCUMENTED LIMITATIONS (not silently claimed away)
--------------------------------------------------
- Streamlit exposes no reliable OIDC login/refresh event, so a silent token
  refresh of an unchanged authenticated session is indistinguishable from an
  ongoing session. The nonce therefore rotates only on transitions the server
  CAN observe: explicit logout, subject change, session-age expiry and
  malformed-state recovery — not at cookie-refresh boundaries.
- Grants are session-cached with a bounded trust window (GRANT_MAX_AGE_*
  seconds). Provider-side revocation (e.g. a teacher removed from a course
  between listing refreshes) cannot be detected offline; privileged scheme
  approval has NO user interface in C6-B1, and C6-B2 must decide whether the
  trust window is acceptable or re-verification per approval is required.
"""
import hashlib
import json
import math
import secrets
import time

import document_limits
from scheme_authorization import (
    ApprovalAuthority,
    AuthorizationError,
    ServerIdentity,
    identifier,
)

# Session-state key holding the whole owner-bound authorization state dict.
AUTH_STATE_KEY = "teacher_authorization_state"

# Owner prefix, matching the application's sensitive_state_owner() format.
OWNER_PREFIX = "google:"

# A login session (as observed by the server) stops authorizing after this
# age; the next access mints a NEW authority with a NEW nonce and empty
# grants, so an expired session never restores previous privileges.
SESSION_MAX_AGE_SECONDS = 12 * 3600

# Trust window for cached Classroom grants. After this age the grant stops
# re-verifying (fail closed) until a fresh trusted listing re-records it; the
# app re-fetches course/assignment listings on every Classroom rerun.
COURSE_GRANT_MAX_AGE_SECONDS = 30 * 60
ASSIGNMENT_GRANT_MAX_AGE_SECONDS = 30 * 60

# Bounded grant stores. Reaching a cap refuses new grants (fail closed); a
# fresh login starts from an empty store.
MAX_COURSE_GRANTS = 100
MAX_ASSIGNMENT_GRANTS = 200
MAX_MANUAL_GRANTS = 50

# Metadata fields covered by the deterministic assignment revision (see
# assignment_revision for the exact threat/coverage statement).
ASSIGNMENT_REVISION_FIELDS = (
    "id", "title", "description", "maxPoints", "workType", "state",
    "creationTime", "updateTime", "dueDate", "dueTime", "materials",
)


class TeacherAuthorizationError(AuthorizationError):
    """Content-free teacher-authorization failure (subclass of C6-A's error)."""


def _owner(subject):
    return OWNER_PREFIX + subject


def _stored(state):
    try:
        return state.get(AUTH_STATE_KEY)
    except Exception:
        return None


def _finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _state_shape_valid(stored):
    """Fail closed on anything but a well-formed in-memory state dict."""
    return (type(stored) is dict
            and identifier(stored.get("subject"))
            and identifier(stored.get("nonce"))
            and _finite_number(stored.get("created_at"))
            and callable(stored.get("_clock"))
            and type(stored.get("authority")) is ApprovalAuthority
            and getattr(stored["authority"], "_c6b1_session_nonce", None)
            == stored.get("nonce")
            and not stored["authority"]._revoked
            and type(stored.get("course_grants")) is dict
            and type(stored.get("assignment_grants")) is dict
            and type(stored.get("manual_grants")) is dict)


def _new_state(subject, clock):
    """Create a fresh authority + nonce + empty grants for ONE login session."""
    nonce = secrets.token_urlsafe(32)
    state = {
        "subject": subject,
        "nonce": nonce,
        "created_at": clock(),
        "_clock": clock,
        "course_grants": {},
        "assignment_grants": {},
        "manual_grants": {},
    }
    authority = ApprovalAuthority(_verifier(state, subject, nonce, clock))
    # Tamper-evident binding between this authority and its state entry; a
    # swapped-in authority object (different nonce) reads as malformed state.
    authority._c6b1_session_nonce = nonce
    state["authority"] = authority
    return state


def _revoke_state(state):
    """Revoke the stored authority (killing every context) and drop the state."""
    stored = _stored(state)
    if type(stored) is dict:
        authority = stored.get("authority")
        if type(authority) is ApprovalAuthority:
            authority.revoke()
    try:
        state.pop(AUTH_STATE_KEY, None)
    except Exception:
        pass


def current_teacher_session(state, current_subject, *, clock=time.time):
    """Return this authenticated session's live authorization state, or None.

    Idempotent across Streamlit reruns (same subject keeps the same authority
    and nonce). Fail-closed and privilege-dropping on anonymous sessions,
    account switching, session expiry and malformed stored state: each of
    those revokes the previous authority and, when a subject is still
    authenticated, starts a brand-new one whose grants are empty.
    """
    if not identifier(current_subject):
        # Anonymous or unusable identity: no session may retain authority.
        _revoke_state(state)
        return None
    stored = _stored(state)
    if type(stored) is dict and _state_shape_valid(stored):
        if (stored["subject"] == current_subject
                and stored["_clock"]() - stored["created_at"]
                <= SESSION_MAX_AGE_SECONDS):
            return stored
        # Account switch or expired login session: the previous privileges
        # are revoked, never restored.
    # Anonymous, malformed or changed sessions: destroy, then start fresh
    # (empty grants, new nonce) when an authenticated subject remains.
    _revoke_state(state)
    fresh = _new_state(current_subject, clock)
    try:
        state[AUTH_STATE_KEY] = fresh
    except Exception:
        raise TeacherAuthorizationError("session_state_unavailable") from None
    return fresh


def revoke_teacher_authorization(state):
    """Explicitly revoke and remove the session authority and all grants.

    Called by the application's sensitive-state cleanup (logout, account
    switch, expiry reconciliation). Idempotent and safe on empty state.
    """
    _revoke_state(state)


def _require_session(state, current_subject):
    session = current_teacher_session(state, current_subject)
    if session is None:
        raise TeacherAuthorizationError("authenticated_teacher_required")
    return session


def assignment_revision(course_id, coursework):
    """Stable revision from server-fetched coursework metadata, or refusal.

    Google Classroom courseWork exposes no provider revision ID, so the
    revision is a SHA-256 over a canonical JSON document of
    ASSIGNMENT_REVISION_FIELDS plus the trusted course_id. Covered: title,
    description, points, work type, state, timestamps, due date and materials
    (attachments). Because every Classroom courseWork update bumps
    ``updateTime``, edits are detected even if a future field is added.
    NOT covered (escape detection): changes outside these fields that do not
    bump ``updateTime``; submission-level changes (tracked separately by the
    marking pipeline's submission revisions). Values are taken verbatim from
    the provider response — never fabricated.
    """
    if not identifier(course_id) or not isinstance(coursework, dict) \
            or not identifier(coursework.get("id")):
        raise TeacherAuthorizationError("assignment_metadata_required")
    payload = {"course_id": course_id}
    payload.update({field: coursework.get(field)
                    for field in ASSIGNMENT_REVISION_FIELDS})
    try:
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise TeacherAuthorizationError("assignment_metadata_required") from None
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def record_course_grant(state, current_subject, verified_courses, course_id):
    """Record a teacher course grant from a trusted server-fetched listing.

    ``verified_courses`` must be the raw response of a successful
    ``list_teacher_courses(token)`` call made with THIS authenticated
    teacher's token (teacherId=me). A course absent from that listing is not
    proven to be taught by the account and is refused. Returns the course ID.
    """
    session = _require_session(state, current_subject)
    if not identifier(course_id) or not isinstance(verified_courses, (list, tuple)):
        raise TeacherAuthorizationError("course_access_unverified")
    trusted = {course.get("id") for course in verified_courses
               if type(course) is dict and identifier(course.get("id"))}
    if course_id not in trusted:
        raise TeacherAuthorizationError("course_access_unverified")
    grants = session["course_grants"]
    if course_id not in grants and len(grants) >= MAX_COURSE_GRANTS:
        raise TeacherAuthorizationError("course_grant_capacity_reached")
    grants[course_id] = {"owner": _owner(current_subject),
                         "verified_at": session["_clock"]()}
    return course_id


def record_assignment_grant(state, current_subject, verified_courses,
                            course_id, verified_assignments, coursework_id):
    """Record an assignment grant bound to a verified course and listing.

    ``verified_assignments`` must be the raw response of a successful
    ``list_assignments(token, course_id)`` call made with this teacher's
    token for a course that passes ``record_course_grant``. The coursework
    must appear in that listing as a published ASSIGNMENT; its revision is
    computed from the server-fetched metadata. Returns the composite
    assignment identifier ("course_id/coursework_id") that scheme reviews
    must use — a course grant alone never authorizes an assignment.
    """
    session = _require_session(state, current_subject)
    record_course_grant(state, current_subject, verified_courses, course_id)
    if not identifier(coursework_id) or not isinstance(verified_assignments, (list, tuple)):
        raise TeacherAuthorizationError("assignment_access_unverified")
    coursework = None
    for item in verified_assignments:
        if (type(item) is dict and item.get("id") == coursework_id
                and item.get("workType") == "ASSIGNMENT"):
            coursework = item
            break
    if coursework is None:
        raise TeacherAuthorizationError("assignment_access_unverified")
    revision = assignment_revision(course_id, coursework)
    assignment_key = course_id + "/" + coursework_id
    if not identifier(assignment_key):
        raise TeacherAuthorizationError("assignment_access_unverified")
    grants = session["assignment_grants"]
    if assignment_key not in grants and len(grants) >= MAX_ASSIGNMENT_GRANTS:
        raise TeacherAuthorizationError("assignment_grant_capacity_reached")
    grants[assignment_key] = {
        "owner": _owner(current_subject),
        "course_id": course_id,
        "work_id": coursework_id,
        "revision": revision,
        "verified_at": session["_clock"](),
    }
    return assignment_key


def remember_classroom_grants(state, current_subject, courses, course_id,
                              assignments):
    """Record every grant provable by the given trusted listings.

    Course grants come from the ``courses`` listing (teacherId=me); assignment
    grants come from ``assignments`` fetched for ``course_id``. Unusable
    entries are skipped; a wrong/absent course never grants anything. This is
    the batch helper the application calls after its existing Classroom
    listings succeed — it performs no provider call of its own.
    """
    session = _require_session(state, current_subject)
    if isinstance(courses, (list, tuple)):
        for course in courses:
            course_id_item = course.get("id") if type(course) is dict else None
            if identifier(course_id_item):
                try:
                    record_course_grant(state, current_subject, courses,
                                        course_id_item)
                except TeacherAuthorizationError:
                    continue
    if identifier(course_id) and isinstance(assignments, (list, tuple)):
        for item in assignments:
            work_id = item.get("id") if type(item) is dict else None
            if identifier(work_id):
                try:
                    record_assignment_grant(state, current_subject, courses,
                                            course_id, assignments, work_id)
                except TeacherAuthorizationError:
                    continue
    return session


def record_manual_pdf_grant(state, current_subject, pdf_bytes):
    """Register the actual uploaded PDF bytes as this session's manual document.

    The digest is computed HERE from the real bytes; a caller-supplied digest
    is never accepted (a ``str`` is refused outright). The grant is bound to
    the authenticated owner captured in the session state. Re-uploading the
    same bytes refreshes the same grant; different bytes produce a different
    digest, so a changed document can never ride on the old grant. Returns the
    SHA-256 hex digest of the uploaded bytes.
    """
    session = _require_session(state, current_subject)
    if type(pdf_bytes) is not bytes or not pdf_bytes:
        raise TeacherAuthorizationError("pdf_bytes_required")
    if len(pdf_bytes) > document_limits.MAX_DOCUMENT_BYTES:
        raise TeacherAuthorizationError("pdf_bytes_required")
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    grants = session["manual_grants"]
    if digest not in grants and len(grants) >= MAX_MANUAL_GRANTS:
        # Bounded: the oldest grant owned by this session is dropped
        # permanently; the authoritative document is the latest upload.
        oldest = min(grants, key=lambda key: grants[key].get("verified_at", 0))
        del grants[oldest]
    grants[digest] = {"owner": _owner(current_subject),
                      "verified_at": session["_clock"]()}
    return digest


def bind_manual_review(state, current_subject, record, *, review_id,
                       pdf_bytes):
    """Bind a manual-review record with real digest/owner provenance (C6-A API).

    Digest comes from the actual uploaded bytes; ownership from the trusted
    server session; the binding context from the session authority's manual
    scope. This is the ONLY sanctioned way for application code to bind a
    manual-review record — calling ``scheme_review.bind_scheme_review``
    directly with a hand-made digest bypasses provenance and is refused by
    policy. Returns the record with its approval binding, as C6-A does.
    """
    import scheme_review
    session = current_teacher_session(state, current_subject)
    if session is None:
        raise TeacherAuthorizationError("authenticated_teacher_required")
    digest = record_manual_pdf_grant(state, current_subject, pdf_bytes)
    grant = session["manual_grants"].get(digest)
    if type(grant) is not dict or grant.get("owner") != _owner(current_subject):
        raise TeacherAuthorizationError("manual_document_ownership_required")
    context = session["authority"].authorize(None)
    return scheme_review.bind_scheme_review(record, context,
                                            review_id=review_id,
                                            document_digest=digest)


def _verifier(state, subject, nonce, clock):
    """The trusted verifier closure wired into the session's authority.

    Rechecks, on EVERY authorize/validate call: the stored subject and nonce
    still match this closure's captured values (account switch or tampering
    fails here), the login session has not expired, and — per scope — the
    grant for the requested assignment/manual document still exists, is owned
    by this subject and is fresh. Any failure raises, which the C6-A boundary
    converts into a fail-closed refusal.
    """
    def verify(assignment_id):
        if state.get("subject") != subject or state.get("nonce") != nonce:
            raise TeacherAuthorizationError("session_identity_changed")
        created = state.get("created_at")
        if not _finite_number(created) or clock() - created > SESSION_MAX_AGE_SECONDS:
            raise TeacherAuthorizationError("session_expired")
        if assignment_id is None:
            return _manual_identity(state, subject, nonce)
        return _assignment_identity(state, subject, nonce, assignment_id)
    return verify


def _assignment_identity(state, subject, nonce, assignment_id):
    if not identifier(assignment_id):
        raise TeacherAuthorizationError("authorization_unavailable")
    grant = state.get("assignment_grants", {}).get(assignment_id)
    if type(grant) is not dict or grant.get("owner") != _owner(subject):
        # No verified course+coursework grant: an OAuth subject, a course
        # grant, or a browser-supplied ID authorize nothing.
        raise TeacherAuthorizationError("assignment_access_unverified")
    verified_at = grant.get("verified_at")
    if (not _finite_number(verified_at)
            or state["_clock"]() - verified_at > ASSIGNMENT_GRANT_MAX_AGE_SECONDS):
        raise TeacherAuthorizationError("assignment_grant_expired")
    revision = grant.get("revision")
    if not identifier(revision):
        raise TeacherAuthorizationError("verified_assignment_revision_required")
    return ServerIdentity(_owner(subject), nonce, revision)


def _manual_identity(state, subject, nonce):
    grants = state.get("manual_grants")
    if type(grants) is not dict or not any(
            type(grant) is dict and grant.get("owner") == _owner(subject)
            for grant in grants.values()):
        raise TeacherAuthorizationError("manual_document_grant_required")
    return ServerIdentity(_owner(subject), nonce)
