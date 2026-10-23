"""Question-level teacher review model for Phase C6-B2.

Pure, dependency-free module: no Streamlit, no network, no filesystem access.
It defines the review statuses, input validation rules, review binding
fingerprints (used to invalidate review decisions when the underlying marking
evidence or teacher-override revision changes), the decision-evaluation
function and a bounded in-memory audit trail.

The module intentionally contains *no* authorization logic: authorization is
performed by the app-layer handlers (``app5_1_automated.py``) and by
``teacher_authorization.py`` before any state produced here is persisted.
Corrections produced by a review decision are routed through the existing
``save_teacher_override`` / ``teacher_overrides`` mechanism so that there
remains exactly one override system in the product.

Question identity is resolved fail-closed: a question number that matches
more than one question (a duplicate/colliding id) never resolves, so no
decision, binding or override can silently target a different question.

AUDIT LIMITATION (documented, not claimed away): decisions and the bounded
audit trail live in Streamlit session state only (``MAX_AUDIT_EVENTS``
newest events). They are lost when the session ends and are not a durable
audit store; persistence is a separate, undecided scope. Retained
authorization evidence is audit-only provenance of a past check and is
never read back as authorization.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable, Mapping

__all__ = [
    "REVIEW_STATUSES",
    "REVIEW_STATUSES_REQUIRING_REASON",
    "MAX_REASON_LENGTH",
    "MAX_AUDIT_EVENTS",
    "review_status",
    "validate_mark_value",
    "validate_mark_sequence",
    "validate_reason",
    "question_review_binding",
    "binding_matches",
    "QUESTION_IDENTITY_OK",
    "AMBIGUOUS_QUESTION_CODE",
    "UNAVAILABLE_QUESTION_CODE",
    "question_number_key",
    "resolve_question",
    "ambiguous_question_numbers",
    "question_is_ambiguous",
    "AUTHORIZATION_SCOPES",
    "AUTHORIZATION_EVIDENCE_FIELDS",
    "EVIDENCE_FIELD_LIMIT",
    "authorization_evidence",
    "evaluate_question_review",
    "make_review_event",
    "bounded_events",
    "summarize_question_review",
]

# Review statuses. "unreviewed" is the implicit default for every question.
REVIEW_STATUSES: tuple[str, ...] = (
    "unreviewed",
    "approved",
    "corrected",
    "flagged",
)

# Statuses that may only be recorded with a non-empty reason.
REVIEW_STATUSES_REQUIRING_REASON: frozenset[str] = frozenset({"corrected", "flagged"})

MAX_REASON_LENGTH = 2000

# Bounded audit trail kept in session state (read-only display use).
MAX_AUDIT_EVENTS = 500

# Fail-closed question-identity codes returned by ``resolve_question``.
QUESTION_IDENTITY_OK = "ok"
AMBIGUOUS_QUESTION_CODE = "review_question_ambiguous"
UNAVAILABLE_QUESTION_CODE = "review_question_unavailable"

# Audit-only authorization evidence: a fixed whitelist of short string
# fields. Nothing else can be stored there: no tokens, credentials or
# browser-supplied values.
AUTHORIZATION_SCOPES: tuple[str, ...] = ("classroom", "manual", "unscoped")
AUTHORIZATION_EVIDENCE_FIELDS: tuple[str, ...] = (
    "scope", "actor", "authorization_revision", "row_revision", "verified_at",
)
EVIDENCE_FIELD_LIMIT = 200

_BINDING_FIELDS = (
    "assignment_id",
    "student_id",
    "question_number",
    "variant",
    "row_revision",
    "ai_marks",
    "scheme_digest",
)


def review_status(value: Any) -> str:
    """Return ``value`` when it is a known review status, else ``unreviewed``."""

    if isinstance(value, str) and value in REVIEW_STATUSES:
        return value
    return "unreviewed"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_mark_value(value: Any, max_value: Any) -> tuple[bool, str]:
    """Validate a single corrected mark.

    Rejects booleans, non-numeric values, NaN, infinities, negative values,
    values above the scheme maximum and silently-rounded fractions. Corrected
    marks on integer rubrics must be integers (the check is applied against
    the mark scheme maximum, which is the authoritative rubric definition).
    """

    if isinstance(value, bool):
        return False, "mark_not_a_number"
    if not _is_number(value):
        return False, "mark_not_a_number"
    number = float(value)
    if math.isnan(number):
        return False, "mark_not_finite"
    if math.isinf(number):
        return False, "mark_not_finite"
    if number < 0:
        return False, "mark_negative"
    if number != int(number):
        return False, "mark_fractional_on_integer_rubric"
    if not _is_number(max_value):
        return False, "mark_maximum_unknown"
    maximum = float(max_value)
    if math.isnan(maximum) or math.isinf(maximum) or maximum < 0:
        return False, "mark_maximum_unknown"
    if number > maximum:
        return False, "mark_exceeds_scheme_maximum"
    return True, "ok"


def validate_mark_sequence(
    marks: Any, max_marks: Any
) -> tuple[bool, str, list[Any]]:
    """Validate a whole sequence of corrected marks against scheme maxima.

    Returns ``(ok, code, normalized)``. ``normalized`` is empty unless the
    sequence is fully valid; integers stay integers (no silent rounding of
    fractions - a fractional mark is rejected instead).
    """

    if not isinstance(marks, (list, tuple)):
        return False, "marks_not_a_sequence", []
    if not isinstance(max_marks, (list, tuple)):
        return False, "mark_maxima_unavailable", []
    if len(marks) != len(max_marks):
        return False, "marks_maxima_length_mismatch", []
    if not marks:
        return False, "marks_empty", []
    normalized: list[Any] = []
    for index, (mark, maximum) in enumerate(zip(marks, max_marks)):
        ok, code = validate_mark_value(mark, maximum)
        if not ok:
            return False, f"{code}:part{index}", []
        normalized.append(int(mark))
    return True, "ok", normalized


def validate_reason(reason: Any) -> tuple[bool, str]:
    """Validate a teacher reason: non-empty, bounded, plain text."""

    if not isinstance(reason, str):
        return False, "reason_not_text"
    stripped = reason.strip()
    if not stripped:
        return False, "reason_empty"
    if len(stripped) > MAX_REASON_LENGTH:
        return False, "reason_too_long"
    return True, "ok"


def question_review_binding(
    *,
    assignment_id: Any,
    student_id: Any,
    question_number: Any,
    variant: Any,
    row_revision: Any,
    ai_marks: Iterable[Any],
    scheme_digest: Any,
) -> str:
    """Fingerprint the exact marking evidence a review decision refers to.

    The binding covers the document/assignment binding, the question identity,
    the pristine AI marks as awarded, the mark-scheme digest and the current
    teacher-override revision of the row. When any of those change (AI marks
    re-generated, scheme updated, override saved, selection changed), the
    stored decision's binding no longer matches and the decision is treated as
    invalidated.
    """

    payload = {
        "assignment_id": str(assignment_id),
        "student_id": str(student_id),
        "question_number": str(question_number),
        "variant": str(variant),
        "row_revision": str(row_revision),
        "ai_marks": [int(m) if _is_number(m) and float(m) == int(m) else str(m) for m in ai_marks],
        "scheme_digest": str(scheme_digest),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def binding_matches(expected: Any, actual: Any) -> bool:
    """Constant-shape comparison of two binding digests (both must be strings)."""

    if not isinstance(expected, str) or not isinstance(actual, str):
        return False
    if len(expected) != 64 or len(actual) != 64:
        return False
    return expected == actual


def question_number_key(value: Any) -> str:
    """Normalised identity key for one question number (comparison only).

    The application compares question numbers as strings, so normalising to a
    stripped string keeps collision detection at least as strict as the lookup
    it protects: ``1``, ``"1"`` and ``" 1 "`` are the same question id, and a
    paper whose numbers collide only after normalisation is still ambiguous.
    """

    return str(value).strip()


def resolve_question(questions: Any, question_number: Any) -> tuple[Any, str]:
    """Resolve one question id to exactly one question, failing closed.

    Returns ``(question, code)``. ``code`` is ``QUESTION_IDENTITY_OK`` only
    when exactly one question carries this id. Two or more matching questions
    return ``(None, AMBIGUOUS_QUESTION_CODE)`` instead of the first match:
    binding a decision or an override to a colliding id could silently target
    a different question. An id that matches nothing (or an unusable question
    list) returns ``(None, UNAVAILABLE_QUESTION_CODE)``.
    """

    if not isinstance(questions, (list, tuple)):
        return None, UNAVAILABLE_QUESTION_CODE
    target = question_number_key(question_number)
    matches = [item for item in questions
               if isinstance(item, dict)
               and question_number_key(item.get("question_number", "")) == target]
    if not matches:
        return None, UNAVAILABLE_QUESTION_CODE
    if len(matches) > 1:
        return None, AMBIGUOUS_QUESTION_CODE
    return matches[0], QUESTION_IDENTITY_OK


def ambiguous_question_numbers(questions: Any) -> list[str]:
    """Every question id in ``questions`` that resolves to more than one row."""

    if not isinstance(questions, (list, tuple)):
        return []
    counts: dict[str, int] = {}
    for item in questions:
        if not isinstance(item, dict):
            continue
        key = question_number_key(item.get("question_number", ""))
        counts[key] = counts.get(key, 0) + 1
    return sorted(key for key, count in counts.items() if count > 1)


def question_is_ambiguous(questions: Any, question_number: Any) -> bool:
    """True only when ``question_number`` collides with another question id."""

    return resolve_question(questions, question_number)[1] == AMBIGUOUS_QUESTION_CODE


def _evidence_field(value: Any) -> str:
    """One bounded string field for audit-only evidence (never a credential)."""

    if value is None:
        return ""
    return str(value)[:EVIDENCE_FIELD_LIMIT]


def authorization_evidence(
    *,
    scope: Any,
    actor: Any,
    authorization_revision: Any = "",
    row_revision: Any = "",
    verified_at: Any = "",
) -> dict[str, str]:
    """Minimal, audit-ONLY record of the gate that authorized one decision.

    Kept with the decision record and its audit event so a reviewer can see
    which server-side check actually ran: the actor, the gate scope, the fresh
    server-derived revision that authorized THIS decision (classroom: the
    assignment revision recomputed from live listings just then; manual: the
    digest of the owned uploaded document), the row revision, and the
    verification time. Only these five short string fields exist.

    This is evidence of a past decision and NEVER authorization itself: no
    gate reads it back, every mutation re-verifies from scratch, and no token,
    credential or unnecessary personal data is included. Stored or forged
    evidence therefore never becomes reusable authorization.
    """

    return {
        "scope": (scope if isinstance(scope, str) and scope in AUTHORIZATION_SCOPES
                  else "unscoped"),
        "actor": _evidence_field(actor),
        "authorization_revision": _evidence_field(authorization_revision),
        "row_revision": _evidence_field(row_revision),
        "verified_at": _evidence_field(verified_at),
    }


def _clean_evidence(evidence: Any) -> dict[str, str] | None:
    """Whitelist one supplied evidence record down to the five known fields.

    Anything that is not a mapping is dropped, and unknown keys (a token, a
    browser-supplied value, an oversized blob) never reach the audit trail.
    """

    if not isinstance(evidence, Mapping):
        return None
    return authorization_evidence(
        scope=evidence.get("scope"),
        actor=evidence.get("actor"),
        authorization_revision=evidence.get("authorization_revision"),
        row_revision=evidence.get("row_revision"),
        verified_at=evidence.get("verified_at"),
    )


def evaluate_question_review(
    *,
    status: Any,
    binding: Any,
    corrected_marks: Any = None,
    reason: Any = None,
    prior_status: Any = None,
    prior_binding: Any = None,
) -> tuple[str, str, dict[str, Any]]:
    """Evaluate a review decision into persisted state.

    Returns ``(code, new_status, state)``. ``code`` is ``"ok"`` when the
    decision is accepted. Once a question has been flagged it remains flagged
    until explicitly approved or corrected with a fresh binding (flagging is
    sticky against silent approvals with stale bindings).
    """

    new_status = review_status(status)
    if new_status == "unreviewed":
        return "unreviewed_not_submittable", review_status(prior_status), {}
    if not isinstance(binding, str) or len(binding) != 64:
        return "binding_malformed", review_status(prior_status), {}

    requires_reason = new_status in REVIEW_STATUSES_REQUIRING_REASON
    ok, reason_code = validate_reason(reason)
    if requires_reason and not ok:
        return f"reason_required:{reason_code}", review_status(prior_status), {}
    if not requires_reason and reason is not None and not ok and reason != "":
        return f"reason_invalid:{reason_code}", review_status(prior_status), {}

    state: dict[str, Any] = {
        "status": new_status,
        "binding": binding,
        "reason": (reason or "").strip(),
    }
    if new_status == "corrected":
        if not isinstance(corrected_marks, (list, tuple)) or not corrected_marks:
            return "corrected_marks_required", review_status(prior_status), []
        state["corrected_marks"] = [int(m) for m in corrected_marks]
    elif corrected_marks is not None:
        return "corrected_marks_not_expected", review_status(prior_status), {}

    # A live flag cannot be silently replaced by a reason-less approval: an
    # approval recorded on the SAME evidence must state why the flag is
    # withdrawn. A flag recorded against older (stale) evidence is simply
    # superseded, so a changed document or resubmission never traps the row.
    prior = review_status(prior_status)
    if prior == "flagged" and binding_matches(prior_binding, binding):
        if new_status == "approved" and not state["reason"]:
            return "flagged_decision_requires_reason", prior, {}

    return "ok", new_status, state


def make_review_event(
    *,
    assignment_id: Any,
    student_id: Any,
    question_number: Any,
    action: Any,
    actor: Any,
    binding: Any,
    timestamp: Any,
    evidence: Any = None,
) -> dict[str, Any]:
    """Create one audit event for the bounded in-memory trail.

    ``evidence`` is the audit-only authorization evidence retained with the
    decision; it is whitelisted to the five known fields here, so an unexpected
    value can never widen what the trail stores.
    """

    if not isinstance(action, str) or not action:
        action = "unknown"
    if not isinstance(actor, str) or not actor:
        actor = "unknown"
    if not isinstance(timestamp, str) or not timestamp:
        timestamp = ""
    return {
        "assignment_id": str(assignment_id),
        "student_id": str(student_id),
        "question_number": str(question_number),
        "action": action,
        "actor": actor,
        "binding": binding if isinstance(binding, str) else "",
        "authorization_evidence": _clean_evidence(evidence),
        "timestamp": timestamp,
    }


def bounded_events(events: Any, maximum: int = MAX_AUDIT_EVENTS) -> list[dict[str, Any]]:
    """Trim an audit event list to ``maximum`` newest entries, oldest dropped.

    The trail itself lives only in session state (see the module docstring):
    it is bounded and non-durable, not a persistent audit store.
    """

    if not isinstance(events, list):
        return []
    clean = [e for e in events if isinstance(e, dict)]
    if maximum <= 0:
        return []
    if len(clean) > maximum:
        clean = clean[-maximum:]
    return clean


def summarize_question_review(decisions: Any) -> dict[str, int]:
    """Counts per status for the review summary table (read-only display)."""

    counts = {status: 0 for status in REVIEW_STATUSES}
    if isinstance(decisions, Mapping):
        for value in decisions.values():
            if isinstance(value, Mapping):
                counts[review_status(value.get("status"))] += 1
    return counts
