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
) -> dict[str, Any]:
    """Create one audit event for the bounded in-memory trail."""

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
        "timestamp": timestamp,
    }


def bounded_events(events: Any, maximum: int = MAX_AUDIT_EVENTS) -> list[dict[str, Any]]:
    """Trim an audit event list to ``maximum`` newest entries, oldest dropped."""

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
