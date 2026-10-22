"""Phase C5: bounded mark-scheme reviewer (deterministic core).

Reviews whether an AI-extracted interpretation of an official Pearson Edexcel
mark scheme actually represents that scheme. The OFFICIAL SCHEME IS ALWAYS
AUTHORITATIVE: every Gemini-assisted interpretation processed here is a
PROPOSAL. Review output never changes awarded student marks or maxima; it
adds teacher-visible validation warnings, forces a teacher-review status
while findings are unresolved, and affects result-caching eligibility until
a teacher acts.

Relationship to earlier phases:
- C2 (investigation.scheme_model / verification_engine) is the structural
  verification engine. The deterministic criteria checks here CONVERT a C5
  interpretation payload into a C2 scheme payload and reuse C2 findings
  verbatim (duplicates, unsupported codes, dependency faults, cycles,
  follow-through contradictions, alternative-group validity).
- C3 (investigation.pdf_evidence / pdf_question_map) gives page evidence and
  question mapping. The evidence checks here can bind criteria to a C3
  document record: page presence, verbatim-excerpt containment, and page
  availability (missing text layer is UNAVAILABLE evidence, never a
  contradiction).
- C4 (investigation.math_verification, SymPy) stays offline-diagnostic and is
  never imported here: the deployed app keeps plain-arithmetic verification
  only. Structured mathematical verdicts are out of C5 scope.

Three information channels are kept strictly separated, never merged:
- ``extracted``: what the model believes the scheme says (verbatim, bounded).
- ``verified``: what deterministic checks establish (findings + checks run).
- ``teacher``: what a teacher has explicitly accepted (status, approver,
  approved revision). ``approval_state`` re-evaluates the exact registered
  snapshot and live server authorization; stored metadata alone grants no
  approval. The existing app still uses its separate owner-bound warning
  acknowledgement, not these privileged APIs.

Gemini-assisted review is OFFLINE-ONLY in this phase. The response schema,
strict parsing, outcome classification and findings validation are fully
implemented and tested against mocked responses; NO live provider call is
made anywhere in this module. Live activation is a separately approved future
step (see ASSISTED_REVIEW_ACTIVATED).

Bounded and fail-closed: all forms are size-capped, deterministic, offline and
content-free on errors (errors name positions or categories, never scheme
prose). Reviewer output always keeps ``review_required`` true on findings.
"""
import hashlib
import json
import re

from scheme_authorization import AuthorizationError, identifier, validate_context

try:
    from investigation import verification_engine as _c2_engine
    from investigation.scheme_model import SchemeModelError, code_is_supported
except Exception:  # pragma: no cover - defensive; module set is part of repo
    _c2_engine = None
    SchemeModelError = ValueError
    def code_is_supported(code):
        return isinstance(code, str) and bool(
            re.fullmatch(r"^(?:(?:dM|M|A|B)[1-9][0-9]*)(?:ft)?$", code or ""))

SCHEME_REVIEW_SCHEMA = "app.scheme_review/1"

# Review statuses (Phase C5 Stage 9). UNREVIEWED is the initial state, CHECKED
# means deterministic checks ran with no findings, NEEDS_TEACHER_REVIEW means
# findings exist (or assisted suggestions) and a teacher must act.
REVIEW_STATUSES = ("UNREVIEWED", "CHECKED", "NEEDS_TEACHER_REVIEW",
                   "TEACHER_APPROVED", "REJECTED")
STATUS_UNREVIEWED = "UNREVIEWED"
STATUS_CHECKED = "CHECKED"
STATUS_NEEDS_REVIEW = "NEEDS_TEACHER_REVIEW"
STATUS_APPROVED = "TEACHER_APPROVED"
STATUS_REJECTED = "REJECTED"

# Mark types represented explicitly; "UNKNOWN" is an explicit state, never a
# guess (Phase C5 requirement: unknown information is represented, not filled).
MARK_TYPES = ("M", "A", "B", "dM", "UNKNOWN")
MARK_TYPE_UNKNOWN = "UNKNOWN"

# Finding categories (stable machine-readable ids). First nine follow the
# Phase C5 design list; the rest cover deterministic outcomes that have no
# listed category without distorting checks.
CATEGORIES = (
    "TOTAL_MISMATCH",
    "CRITERION_MISSING",
    "CRITERION_UNSUPPORTED",
    "MARK_TYPE_UNCERTAIN",
    "DEPENDENCY_CONFLICT",
    "FOLLOW_THROUGH_UNCERTAIN",
    "ALTERNATIVE_METHOD_CONFLICT",
    "QUESTION_MAPPING_CONFLICT",
    "SOURCE_EVIDENCE_MISSING",
    "DUPLICATE_CRITERION",
    "SOURCE_EVIDENCE_CONFLICT",
    "EXAMINER_GUIDANCE_REQUIRED",
    "INVALID_MARK_ALLOCATION",
    "QUESTION_MAPPING_UNCERTAIN",
    "INTERPRETATION_MALFORMED",
)

CATEGORY_TOTAL_MISMATCH = "TOTAL_MISMATCH"
CATEGORY_CRITERION_MISSING = "CRITERION_MISSING"
CATEGORY_CRITERION_UNSUPPORTED = "CRITERION_UNSUPPORTED"
CATEGORY_MARK_TYPE_UNCERTAIN = "MARK_TYPE_UNCERTAIN"
CATEGORY_DEPENDENCY_CONFLICT = "DEPENDENCY_CONFLICT"
CATEGORY_FOLLOW_THROUGH_UNCERTAIN = "FOLLOW_THROUGH_UNCERTAIN"
CATEGORY_ALTERNATIVE_METHOD_CONFLICT = "ALTERNATIVE_METHOD_CONFLICT"
CATEGORY_QUESTION_MAPPING_CONFLICT = "QUESTION_MAPPING_CONFLICT"
CATEGORY_QUESTION_MAPPING_UNCERTAIN = "QUESTION_MAPPING_UNCERTAIN"
CATEGORY_SOURCE_EVIDENCE_MISSING = "SOURCE_EVIDENCE_MISSING"
CATEGORY_SOURCE_EVIDENCE_CONFLICT = "SOURCE_EVIDENCE_CONFLICT"
CATEGORY_EXAMINER_GUIDANCE_REQUIRED = "EXAMINER_GUIDANCE_REQUIRED"
CATEGORY_INVALID_MARK_ALLOCATION = "INVALID_MARK_ALLOCATION"
CATEGORY_DUPLICATE_CRITERION = "DUPLICATE_CRITERION"
CATEGORY_MALFORMED = "INTERPRETATION_MALFORMED"

# C2 finding states (shared vocabulary, never remade here).
C2_VERIFIED = "verified_structural_inconsistency"
C2_SUSPECTED = "suspected_issue"
C2_INSUFFICIENT = "evidence_insufficient"
C2_HUMAN_REVIEW = "human_review_required"
FINDING_STATES = (C2_VERIFIED, C2_SUSPECTED, C2_INSUFFICIENT, C2_HUMAN_REVIEW)

_SEVERITIES = ("high", "medium", "low")

# Bounds (Phase C5 data contract). Everything is capped before processing.
MAX_QUESTIONS = 100
MAX_CRITERIA_PER_QUESTION = 50
MAX_DEPENDENCIES_PER_CRITERION = 20
MAX_ID_CHARS = 50
MAX_PART_CHARS = 20
MAX_NOTE_CHARS = 400
MAX_EXCERPT_CHARS = 500
MAX_SPEC_CHARS = 200
MAX_ANSWER_SUMMARY_CHARS = 500
MAX_SERIALIZED_CHARS = 400_000
MAX_QUESTION_IDS = 1000
MAX_TEACHER_MESSAGES = 12
MAX_TEACHER_MESSAGE_CHARS = 1000
MARK_VALUE_MAX = 20

_HOMEWORK_QUESTION_PATTERN = re.compile(r"^[1-9]\d{0,3}$")
_INTEGER_PATTERN = re.compile(r"^-?\d+$")

# Missing evidence and proven conflicts require correction and revalidation.
EVIDENCE_BLOCKING_CATEGORIES = (
    CATEGORY_SOURCE_EVIDENCE_MISSING,
    CATEGORY_SOURCE_EVIDENCE_CONFLICT,
    CATEGORY_QUESTION_MAPPING_CONFLICT,
)

# Rules that may never be waived as a side effect of approval.
BLOCKING_RULES = ("C5-EXTRACTION-EMPTY",)

# Checks executed by the deterministic pipeline (recorded on every review).
DETERMINISTIC_CHECKS = (
    "question_identifiers",
    "question_parts",
    "mark_allocations",
    "criteria_per_question",
    "question_totals_reconciliation",
    "alternative_groups_reconciliation",
    "missing_criteria_coverage",
    "c2_structural_verification",
    "follow_through_support",
    "source_page_references",
    "source_evidence_availability",
    "source_evidence_containment",
    "question_mapping_consistency",
)

# ---------------------------------------------------------------------------
# Assisted (Gemini-reviewed) findings contract. Mocked offline only.

ASSISTED_REVIEW_SCHEMA = "app.scheme_review.assisted/1"
ASSISTED_ORIGIN = "assisted"
ASSISTED_CATEGORIES = (
    "TOTAL_MISMATCH", "CRITERION_MISSING", "CRITERION_UNSUPPORTED",
    "MARK_TYPE_UNCERTAIN", "DEPENDENCY_CONFLICT", "FOLLOW_THROUGH_UNCERTAIN",
    "ALTERNATIVE_METHOD_CONFLICT", "QUESTION_MAPPING_CONFLICT",
    "SOURCE_EVIDENCE_MISSING",
)
ASSISTED_CONFIDENCES = ("low", "medium", "high")
MAX_ASSISTED_FINDINGS = 200
MAX_ASSISTED_TEXT_CHARS = 2000
MAX_ASSISTED_RESPONSE_CHARS = 131_072
# Response JSON schema for the future bounded assisted pass. It is published
# here so tests and reviewers can pin the exact contract; no live call is made.
ASSISTED_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "category": {"type": "string",
                             "enum": list(ASSISTED_CATEGORIES)},
                "question_number": {"type": "string"},
                "criterion_id": {"type": "string"},
                "evidence_reference": {"type": "string"},
                "explanation": {"type": "string"},
                "confidence": {"type": "string",
                               "enum": list(ASSISTED_CONFIDENCES)},
                "suggested_action": {"type": "string"},
            },
            "required": ["category", "explanation"],
        }},
    },
    "required": ["findings"],
}

ASSISTED_OUTCOMES = (
    "completed",
    "completed_no_findings",
    "invalid_json",
    "invalid_response_shape",
    "empty_response",
    "blocked_or_incomplete_response",
    "response_too_large",
    "timeout",
    "quota_exhausted",
    "model_error",
)

# Live activation of an assisted reviewer pass is a SEPARATELY APPROVED future
# step. Until then this constant stays False and no provider call exists.
ASSISTED_REVIEW_ACTIVATED = False


class SchemeReviewError(ValueError):
    """Reviewer data-contract violation; message is content-free."""


class SchemeReviewApprovalError(SchemeReviewError):
    """Approval refused; message names the reason category, not content."""


class AssistedResponseError(SchemeReviewError):
    """Mocked assisted response rejected; outcome is an allowlisted code."""

    def __init__(self, outcome, detail=None):
        self.outcome = outcome
        super().__init__(detail or f"assisted review outcome: {outcome}")


# ---------------------------------------------------------------------------
# Review record construction (Phase C5 data contract).

def compute_revision(payload):
    """Scheme revision token: sha256 over canonical bounded JSON."""
    serialized = json.dumps(payload, sort_keys=True,
                            separators=(",", ":"), ensure_ascii=False)
    if len(serialized) > MAX_SERIALIZED_CHARS:
        raise SchemeReviewError("payload exceeds the serialization limit")
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _finding(category, rule_id, *, question_number=None, part=None,
             criterion_ids=(), severity="medium", state=C2_HUMAN_REVIEW,
             evidence=None, explanation="", suggested_action="",
             origin="deterministic", source_engine=None, confidence=None):
    """One reviewer finding; content-free identity, bounded payload."""
    if category not in CATEGORIES:
        raise SchemeReviewError("unknown finding category")
    if state not in FINDING_STATES or severity not in _SEVERITIES:
        raise SchemeReviewError("unknown finding state or severity")
    ordered = sorted(str(cid) for cid in criterion_ids)
    parts = [rule_id, str(question_number or "-"), str(part or "-"),
             ",".join(ordered)]
    if origin == ASSISTED_ORIGIN:
        parts.append("assisted")
    explanation = _bounded_text(explanation, MAX_NOTE_CHARS)
    suggestion = _bounded_text(suggested_action, MAX_NOTE_CHARS)
    finding = {
        "finding_id": "::".join(parts),
        "rule_id": rule_id,
        "category": category,
        "origin": origin,
        "state": state,
        "severity": severity,
        "question_number": None if question_number is None
        else str(question_number),
        "part": None if part is None else str(part),
        "criterion_ids": ordered,
        "evidence": dict(evidence) if evidence else {},
        "explanation": explanation,
        "suggested_action": suggestion,
        "confidence": confidence,
        "review_required": True,
        "teacher_resolved": False,
    }
    if source_engine:
        finding["source_engine"] = source_engine
    return finding


def _bounded_text(text, limit):
    if text is None:
        return ""
    text = str(text)
    return text if len(text) <= limit else text[:limit]


def empty_teacher_section():
    return {"status": STATUS_UNREVIEWED, "approved_by": None,
            "approved_revision": None}


def build_review(interpretation, *, specification=None, assignment_id=None,
                 scheme_revision=None, question_numbers=None,
                 evidence_document=None, question_mapping=None):
    """Run the deterministic pipeline and build the review record.

    The three channels are always separated: extracted (verbatim bounded
    clone), verified (deterministic findings only), teacher (status only).
    """
    questions, malformed = normalize_interpretation(interpretation)
    if scheme_revision is None:
        scheme_revision = compute_revision(interpretation if isinstance(
            interpretation, dict) else {"questions": []})
    findings = list(malformed)
    checks_run = list(DETERMINISTIC_CHECKS)
    findings.extend(_run_all_checks(questions, question_numbers=question_numbers,
                                    evidence_document=evidence_document,
                                    question_mapping=question_mapping))
    if evidence_document is not None or question_mapping is not None:
        checks_run = checks_run + ["pdf_evidence_binding"]
    record = {
        "schema": SCHEME_REVIEW_SCHEMA,
        "specification": _bounded_text(specification, MAX_SPEC_CHARS)
        or "UNKNOWN",
        "assignment_id": None if assignment_id is None
        else _bounded_text(assignment_id, MAX_ID_CHARS),
        "scheme_revision": scheme_revision,
        "extracted": {"questions": questions},
        "verified": {"checks_run": checks_run, "findings": _consolidate(
            findings)},
        "assisted": None,
        "teacher": empty_teacher_section(),
        "warnings": [],
    }
    # Preserve complete inputs for privileged revalidation, independently of
    # bounded display excerpts. Oversized/non-JSON inputs remain read-only.
    record["approval_inputs"] = None
    try:
        raw = json.dumps({"interpretation": interpretation,
                          "evidence_document": evidence_document,
                          "question_mapping": question_mapping,
                          "question_numbers": question_numbers}, allow_nan=False)
        if len(raw) <= MAX_SERIALIZED_CHARS:
            record["approval_inputs"] = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        pass
    record["teacher"]["status"] = determine_status(record)
    return record


def determine_status(record, authorization=None):
    """Status derivation; explicit, never inferred beyond findings."""
    if not _status_shape_valid(record):
        return STATUS_NEEDS_REVIEW
    teacher = record.get("teacher") or {}
    status = teacher.get("status")
    findings = list((record.get("verified") or {}).get("findings") or [])
    assisted = record.get("assisted") or {}
    assisted_findings = list(assisted.get("findings") or [])
    if status == STATUS_APPROVED:
        state, _ = approval_state(record, authorization, record.get("scheme_revision"))
        return STATUS_APPROVED if state == "approved" else STATUS_NEEDS_REVIEW
    if status == STATUS_REJECTED:
        return STATUS_REJECTED
    if findings or assisted_findings or assisted.get("failure_note"):
        return STATUS_NEEDS_REVIEW
    if _checks_ran(record):
        return STATUS_CHECKED
    return STATUS_UNREVIEWED


def _checks_ran(record):
    return bool((record.get("verified") or {}).get("checks_run"))


def refresh_status(record):
    record["teacher"]["status"] = determine_status(record)
    return record["teacher"]["status"]


def unresolved_findings(record):
    return [finding for finding in (record.get("verified") or {}).get(
        "findings", []) if not finding.get("teacher_resolved")]


def teacher_messages(record, limit=MAX_TEACHER_MESSAGES):
    """Bounded teacher-facing message strings derived from findings."""
    messages = []
    for finding in unresolved_findings(record):
        question = finding.get("question_number") or "-"
        label = finding.get("rule_id") or finding.get("category")
        suggestion = finding.get("suggested_action") or finding.get(
            "explanation", "")
        messages.append(_bounded_text(
            f"Question {question} [{label}]: {suggestion}",
            MAX_TEACHER_MESSAGE_CHARS))
        if len(messages) >= limit:
            break
    return messages


def review_summary(record, authorization=None):
    """Bounded display summary for teacher-facing surfaces."""
    if not _status_shape_valid(record):
        return {"status": STATUS_NEEDS_REVIEW, "revision": "",
                "revision_short": "", "messages": ["Invalid review record."],
                "historical_approval": False}
    status = determine_status(record, authorization)
    revision = str(record.get("scheme_revision") or "")
    return {
        "status": status,
        "revision": revision,
        "revision_short": revision[:24],
        "messages": teacher_messages(record),
        "historical_approval": bool((record.get("teacher") or {}).get("approved_by")),
    }


def _status_shape_valid(record):
    return (type(record) is dict and type(record.get("teacher")) is dict
            and type(record.get("verified")) is dict
            and type(record["verified"].get("findings")) is list
            and all(type(item) is dict for item in record["verified"]["findings"])
            and (record.get("assisted") is None or
                 (type(record["assisted"]) is dict and
                  type(record["assisted"].get("findings", [])) is list)))


# ---------------------------------------------------------------------------
# Interpretation payload normalization (extracted channel).

def normalize_interpretation(payload):
    """Validate shape, bound sizes and CLONE the payload verbatim.

    Malformed positions become findings (never exceptions for scheme data);
    only structurally impossible inputs (wrong top-level container) raise
    SchemeReviewError. Returns (questions, malformed_findings).
    """
    if not isinstance(payload, dict):
        raise SchemeReviewError(
            "interpretation payload must be an object with a 'questions' list")
    _size_guard(payload)
    raw_questions = payload.get("questions")
    if raw_questions is None:
        raise SchemeReviewError(
            "interpretation payload requires a 'questions' list")
    if not isinstance(raw_questions, list):
        raise SchemeReviewError(
            "interpretation payload requires a 'questions' list")
    truncated_by_cap = len(raw_questions) > MAX_QUESTIONS
    if truncated_by_cap:
        raw_questions = raw_questions[:MAX_QUESTIONS]
    findings = []
    if truncated_by_cap:
        findings.append(_finding(
            CATEGORY_MALFORMED, "C5-QUESTIONS-CAPPED", severity="medium",
            state=C2_INSUFFICIENT,
            evidence={"limit": MAX_QUESTIONS},
            explanation="The interpretation declares more questions than the "
            "supported cap; the remainder is not interpreted and is never "
            "inferred.",
            suggested_action="Re-extract within the supported question "
            "count."))
    questions = []
    for question_pos, raw in enumerate(raw_questions):
        question, question_findings = _normalize_question(raw, question_pos)
        questions.append(question)
        findings.extend(question_findings)
    return questions, findings


def _size_guard(payload):
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False, default=str)
    if len(serialized) > MAX_SERIALIZED_CHARS:
        raise SchemeReviewError(
            f"interpretation payload exceeds {MAX_SERIALIZED_CHARS} characters")


def _normalize_question(raw, question_pos):
    if not isinstance(raw, dict):
        raise SchemeReviewError(
            f"questions[{question_pos}] must be an object")
    number = raw.get("question_number")
    if isinstance(number, bool) or not isinstance(number, (
            int, str)) or not str(number).strip():
        return {"question_number": None, "part": None, "max_marks": None,
                "criteria": []}, [_finding(
            CATEGORY_MALFORMED, "C5-QID-INVALID", severity="high", state=C2_VERIFIED,
            evidence={"position": f"questions[{question_pos}]"},
            explanation="Missing or invalid question identifier; the "
            "interpretation is dropped from reconciliation.",
            suggested_action="Supply the official question number; false "
            "identifiers must not be reconciled.")]
    number = str(number).strip()
    if len(number) > MAX_ID_CHARS:
        return {"question_number": number, "part": None, "max_marks": None,
                "criteria": []}, [_finding(
            CATEGORY_MALFORMED, "C5-QID-INVALID", question_number=None,
            severity="high", state=C2_VERIFIED,
            evidence={"position": f"questions[{question_pos}]",
                      "oversized_identifier": True},
            explanation="Question identifier exceeds the size limit.",
            suggested_action="Truncate or correct the question identifier.")]
    part = raw.get("part")
    if part is not None and (not isinstance(part, str)
                             or len(part) > MAX_PART_CHARS or not part.strip()):
        return {"question_number": number, "part": None, "max_marks": None,
                "criteria": []}, [_finding(
            CATEGORY_MALFORMED, "C5-PART-INVALID", question_number=number,
            severity="medium", state=C2_VERIFIED,
            evidence={"position": f"questions[{question_pos}]"},
            explanation="Invalid question part identifier.",
            suggested_action="Supply a short part label such as '(a)' or omit.")]
    max_marks = raw.get("max_marks")
    question_findings = []
    if max_marks is not None:
        normalized_marks, marks_findings = _normalize_mark_value(
            max_marks, number)
        max_marks = normalized_marks
        question_findings.extend(marks_findings)
    diagram_dependent = raw.get("diagram_dependent")
    if diagram_dependent is not None and type(diagram_dependent) is not bool:
        diagram_dependent = None
        question_findings.append(_finding(
            CATEGORY_EXAMINER_GUIDANCE_REQUIRED, "C5-DIAGRAM-SHAPE",
            question_number=number, severity="low", state=C2_INSUFFICIENT,
            explanation="diagram_dependent must be a strict boolean; the "
            "declaration is retained as explicitly unknown.",
            suggested_action="Confirm diagram dependence from the official "
            "scheme."))
    raw_criteria = raw.get("criteria")
    if raw_criteria is None:
        raw_criteria = []
    if not isinstance(raw_criteria, list):
        return {"question_number": number, "part": part, "max_marks": max_marks,
                "criteria": []}, question_findings + [_finding(
            CATEGORY_MALFORMED, "C5-CRITERIA-SHAPE", question_number=number,
            severity="high", state=C2_VERIFIED,
            evidence={"expected": "list"},
            explanation="Criteria must be a list; the question is treated as "
            "having no criteria for reconciliation.",
            suggested_action="Re-extract the criteria as a list.")]
    criteria = []
    for criterion_raw in raw_criteria[:MAX_CRITERIA_PER_QUESTION]:
        criterion, criterion_findings = _normalize_criterion(
            criterion_raw, number, part)
        criteria.append(criterion)
        question_findings.extend(criterion_findings)
    if len(raw_criteria) > MAX_CRITERIA_PER_QUESTION:
        question_findings.append(_finding(
            CATEGORY_MALFORMED, "C5-CRITERIA-SHAPE", question_number=number,
            severity="high", state=C2_VERIFIED,
            evidence={"limit": MAX_CRITERIA_PER_QUESTION,
                      "declared": len(raw_criteria)},
            explanation="Criterion list exceeds the size cap; the remainder "
            "is not interpreted.",
            suggested_action="Reduce to supported criterion counts."))
    question = {"question_number": number, "part": part,
                "max_marks": max_marks, "diagram_dependent": diagram_dependent,
                "criteria": criteria}
    return question, question_findings


def _normalize_mark_value(value, question_number):
    """Marks must be non-negative integers within the supported range."""
    findings = []
    if isinstance(value, bool) or not isinstance(value, (
            int, str, float)) or (
            isinstance(value, str) and not _INTEGER_PATTERN.fullmatch(
                value.strip())):
        return None, [_finding(
            CATEGORY_MALFORMED, "C5-MARKS-INVALID", question_number=question_number,
            severity="high", state=C2_VERIFIED,
            evidence={"value_type": type(value).__name__},
            explanation="Mark value is not an integer; it is recorded as "
            "explicitly unknown, never guessed.",
            suggested_action="Enter the integer mark allocation manually.")]
    integer = value if isinstance(value, int) else int(value.strip() if
                                                       isinstance(value, str) else value)
    if integer < 0:
        return None, [_finding(
            CATEGORY_INVALID_MARK_ALLOCATION, "C5-MARKS-NEGATIVE",
            question_number=question_number, severity="high",
            state=C2_VERIFIED,
            explanation="Negative mark allocation is impossible; recorded as "
            "unknown instead of a negative total.",
            suggested_action="Correct the allocation manually.")]
    if integer > MARK_VALUE_MAX:
        return None, [_finding(
            CATEGORY_INVALID_MARK_ALLOCATION, "C5-MARKS-OUT-OF-RANGE",
            question_number=question_number, severity="medium",
            state=C2_SUSPECTED,
            explanation="Mark allocation exceeds the single-question cap for "
            "this platform boundary; verify against the official scheme.",
            suggested_action="Verify the maximum with the official document.")]
    return integer, findings


def _normalize_criterion(raw, question_number, part):
    if not isinstance(raw, dict):
        raise SchemeReviewError(
            f"criterion in question '{question_number}' must be an object")
    code = raw.get("code")
    mark_type = _mark_type_of(code)
    findings = []
    if code is not None and (not isinstance(code, str)
                             or len(code) > MAX_ID_CHARS):
        code, mark_type = None, MARK_TYPE_UNKNOWN
        findings.append(_finding(
            CATEGORY_MARK_TYPE_UNCERTAIN, "C5-MARKTYPE-INVALID",
            question_number=question_number, part=part,
            severity="medium", state=C2_VERIFIED,
            explanation="Mark code is not a readable string; mark type is "
            "explicitly unknown.",
            suggested_action="Read the official code (e.g. M1, A1, B1ft)."))
    criterion_id = raw.get("criterion_id")
    if isinstance(criterion_id, bool) or not isinstance(criterion_id, (
            int, str)) or not str(criterion_id).strip():
        criterion_id = None
        findings.append(_finding(
            CATEGORY_CRITERION_UNSUPPORTED, "C5-CRITERION-ID-MISSING",
            question_number=question_number, part=part, severity="high",
            state=C2_VERIFIED,
            explanation="Criterion identifier missing; the criterion cannot "
            "be referenced or reconciled.",
            suggested_action="Re-extract with stable criterion identifiers."))
    else:
        criterion_id = str(criterion_id).strip()
        if len(criterion_id) > MAX_ID_CHARS:
            findings.append(_finding(
                CATEGORY_CRITERION_UNSUPPORTED, "C5-CRITERION-ID-OVERSIZED",
                question_number=question_number, part=part, severity="high",
                state=C2_VERIFIED,
                explanation="Criterion identifier exceeds the size cap.",
                suggested_action="Use bounded criterion identifiers."))
            criterion_id = None
    marks, marks_findings = _normalize_mark_value(raw.get("marks"),
                                                  question_number)
    findings.extend(marks_findings)
    dependencies = raw.get("dependencies") or []
    if not isinstance(dependencies, list):
        dependencies = []
        findings.append(_finding(
            CATEGORY_DEPENDENCY_CONFLICT, "C5-DEP-SHAPE",
            question_number=question_number, part=part, severity="medium",
            state=C2_VERIFIED,
            explanation="Dependencies must be a list; treated as none, which "
            "understates required method prerequisites.",
            suggested_action="Re-extract dependencies explicitly."))
    dependencies = [str(dep) for dep in
                    dependencies[:MAX_DEPENDENCIES_PER_CRITERION]]
    follow_through = raw.get("follow_through")
    if follow_through is not None and type(follow_through) is not bool:
        follow_through = None
        findings.append(_finding(
            CATEGORY_FOLLOW_THROUGH_UNCERTAIN, "C5-FT-SHAPE",
            question_number=question_number, part=part, severity="low",
            state=C2_INSUFFICIENT,
            explanation="Follow-through flag is not a strict boolean; "
            "follow-through status is explicitly unknown.",
            suggested_action="Confirm from the official scheme prose."))
    alternative_group = raw.get("alternative_group")
    if alternative_group is not None and (not isinstance(alternative_group, str)
                                          or not alternative_group.strip()
                                          or len(alternative_group) > MAX_ID_CHARS):
        alternative_group = None
        findings.append(_finding(
            CATEGORY_ALTERNATIVE_METHOD_CONFLICT, "C5-ALT-SHAPE",
            question_number=question_number, part=part, severity="medium",
            state=C2_INSUFFICIENT,
            explanation="Alternative group label invalid; grouping is "
            "explicitly unknown.",
            suggested_action="Confirm alternative methods from the official "
            "notes."))
    # Optional branch label for an alternative-method group. When every member
    # of a group carries a distinct, non-empty label the branches can be
    # reconciled by maximum (never summed); when absent or malformed the group
    # stays unresolved and is escalated, never silently summed.
    alternative_option = raw.get("alternative_option")
    if alternative_option is not None and (
            not isinstance(alternative_option, str)
            or not alternative_option.strip()
            or len(alternative_option) > MAX_ID_CHARS):
        alternative_option = None
        findings.append(_finding(
            CATEGORY_ALTERNATIVE_METHOD_CONFLICT, "C5-ALT-OPTION-SHAPE",
            question_number=question_number, part=part, severity="low",
            state=C2_INSUFFICIENT,
            explanation="Alternative-branch label invalid; the branch is "
            "explicitly unknown and its group is not reconciled.",
            suggested_action="Confirm the alternative branch from the official "
            "notes."))
    source_page = raw.get("source_page")
    if source_page is not None and (isinstance(source_page, bool)
                                    or not isinstance(source_page, int)
                                    or source_page < 1):
        source_page = None
        findings.append(_finding(
            CATEGORY_SOURCE_EVIDENCE_MISSING, "C5-SOURCE-PAGE-INVALID",
            question_number=question_number, part=part, severity="low",
            state=C2_INSUFFICIENT,
            explanation="Source page reference is not a positive integer; "
            "evidence is treated as unavailable.",
            suggested_action="Locate the page manually."))
    source_evidence = raw.get("source_evidence")
    if source_evidence is not None and (not isinstance(source_evidence, str)
                                        or not source_evidence.strip()):
        source_evidence = None
    elif source_evidence is not None:
        source_evidence = source_evidence[:MAX_EXCERPT_CHARS]
    note = raw.get("note")
    if note is not None and not isinstance(note, str):
        note = None
    elif note is not None:
        note = note[:MAX_NOTE_CHARS]
    criterion = {
        "criterion_id": criterion_id,
        "code": code,
        "mark_type": mark_type,
        "marks": marks,
        "dependencies": dependencies,
        "follow_through": follow_through,
        "alternative_group": alternative_group,
        "alternative_option": alternative_option,
        "source_page": source_page,
        "source_evidence": source_evidence,
        "note": note,
    }
    return criterion, findings


def _mark_type_of(code):
    """Explicit mark type from a supported mark code; UNKNOWN otherwise."""
    if code is None or not code_is_supported(code):
        return MARK_TYPE_UNKNOWN
    base = code[:2] if code.startswith("dM") else code[:1]
    return base if base in MARK_TYPES else MARK_TYPE_UNKNOWN


def code_base(code):
    base = None
    if code and code_is_supported(code):
        base = code[:2] if code.startswith("dM") else code[:1]
    return base


# ---------------------------------------------------------------------------
# C2 structural bridge (reuse, never duplicate).

_C2_CATEGORY_MAP = {
    "SCHEME-DUP-QUESTIONS": CATEGORY_DUPLICATE_CRITERION,
    "SCHEME-DUP-CRITERIA": CATEGORY_DUPLICATE_CRITERION,
    "SCHEME-CODE-UNSUPPORTED": CATEGORY_MARK_TYPE_UNCERTAIN,
    "SCHEME-WEIGHT-NONPOSITIVE": CATEGORY_INVALID_MARK_ALLOCATION,
    "SCHEME-FIELD-MISSING": CATEGORY_MALFORMED,
    "SCHEME-TOTAL-MISMATCH": CATEGORY_TOTAL_MISMATCH,
    "SCHEME-TOTAL-ABSENT": CATEGORY_TOTAL_MISMATCH,
    "SCHEME-TOTAL-UNRECONCILABLE": CATEGORY_TOTAL_MISMATCH,
    "SCHEME-DEP-UNDEFINED": CATEGORY_DEPENDENCY_CONFLICT,
    "SCHEME-DEP-SELF": CATEGORY_DEPENDENCY_CONFLICT,
    "SCHEME-DEP-DUPLICATED": CATEGORY_DEPENDENCY_CONFLICT,
    "SCHEME-REF-AMBIGUOUS": CATEGORY_DEPENDENCY_CONFLICT,
    "SCHEME-DEP-CYCLE": CATEGORY_DEPENDENCY_CONFLICT,
    "SCHEME-FT-CONTRADICTION": CATEGORY_FOLLOW_THROUGH_UNCERTAIN,
    "SCHEME-FT-MISMATCH": CATEGORY_FOLLOW_THROUGH_UNCERTAIN,
    "SCHEME-ALT-GROUP-INVALID": CATEGORY_ALTERNATIVE_METHOD_CONFLICT,
    "SCHEME-DIAGRAM-REVIEW": CATEGORY_EXAMINER_GUIDANCE_REQUIRED,
    "SCHEME-MATH-UNSUPPORTED": CATEGORY_EXAMINER_GUIDANCE_REQUIRED,
    "SCHEME-PROVENANCE-ABSENT": CATEGORY_SOURCE_EVIDENCE_MISSING,
}

_C2_RULE_ID_MAP = {
    "SCHEME-DEP-UNDEFINED": "C5-DEP-UNDEFINED",
    "SCHEME-DEP-SELF": "C5-DEP-SELF",
    "SCHEME-DEP-DUPLICATED": "C5-DEP-DUPLICATED",
    "SCHEME-REF-AMBIGUOUS": "C5-REF-AMBIGUOUS",
    "SCHEME-DEP-CYCLE": "C5-DEP-CYCLE",
    "SCHEME-CODE-UNSUPPORTED": "C5-MARKTYPE-UNSUPPORTED",
    "SCHEME-TOTAL-MISMATCH": "C5-TOTAL-MISMATCH",
}


def _to_c2_payload(questions):
    """Convert normalized C5 questions into a C2 scheme payload."""
    payload = {"provenance": {"origin": "phase-c5-review", "verified": False},
               "questions": []}
    for question in questions:
        if question["question_number"] is None:
            continue
        total = question["max_marks"]
        criteria = []
        for criterion in question["criteria"]:
            if criterion["criterion_id"] is None:
                continue
            criteria.append({
                "id": criterion["criterion_id"],
                "code": criterion["code"],
                "weight": criterion["marks"],
                "dependencies": list(criterion["dependencies"]),
                "follow_through": criterion["follow_through"],
                "alternative_group": criterion["alternative_group"],
                "note": criterion["note"],
                "source_ref": _source_ref_of(criterion),
            })
        payload["questions"].append({
            "id": "Q" + question["question_number"],
            "total": total,
            "diagram_dependent": question.get("diagram_dependent"),
            "claimed_solution": None,
            "source_ref": None,
            "criteria": criteria,
        })
    return payload


def _source_ref_of(criterion):
    source_ref = {}
    if criterion["source_page"] is not None:
        source_ref["page"] = criterion["source_page"]
    if criterion["source_evidence"]:
        source_ref["excerpt"] = criterion["source_evidence"]
    return source_ref or None


def _map_c2_findings(c2_findings):
    mapped = []
    for finding in c2_findings:
        rule = finding.get("rule_id")
        question_id = finding.get("question_id")
        category = _C2_CATEGORY_MAP.get(rule, CATEGORY_CRITERION_UNSUPPORTED)
        mapped.append(_finding(
            category, _C2_RULE_ID_MAP.get(rule, rule),
            question_number=(str(question_id[1:]) if isinstance(question_id, str)
                             and question_id.startswith("Q") else question_id),
            criterion_ids=finding.get("criterion_ids") or (),
            severity=finding.get("severity") or "medium",
            state=finding.get("state") or C2_HUMAN_REVIEW,
            evidence=finding.get("evidence") or {},
            explanation=finding.get("explanation") or "",
            suggested_action="Confirm against the official mark scheme.",
            source_engine="c2"))
    return mapped


# ---------------------------------------------------------------------------
# App-level deterministic checks (beyond C2 coverage).

def check_question_identifiers(questions):
    """Question identifiers must follow the homework numbering grammar."""
    findings = []
    numbers_seen = set()
    for question in questions:
        number = question["question_number"]
        if number is None:
            continue
        if not _HOMEWORK_QUESTION_PATTERN.fullmatch(number):
            findings.append(_finding(
                CATEGORY_MALFORMED, "C5-QID-INVALID", question_number=number,
                severity="medium", state=C2_SUSPECTED,
                explanation="Question identifier is outside the supported "
                "homework numbering pattern; verify against the official "
                "scheme (main questions count 1..9999).",
                suggested_action="Confirm the numbering of the official "
                "document."))
            continue
        if number in numbers_seen:
            findings.append(_finding(
                CATEGORY_DUPLICATE_CRITERION, "C5-QID-DUPLICATE",
                question_number=number, severity="high", state=C2_VERIFIED,
                explanation="The same question is declared more than once; "
                "mappings cannot be attributed unambiguously.",
                suggested_action="Remove the duplicated question entry."))
        numbers_seen.add(number)
    return findings


def check_criteria_weights(questions):
    """Duplicate/oversized criterion ids are found again at question level."""
    findings = []
    for question in questions:
        number = question["question_number"]
        seen_criterion = set()
        for criterion in question["criteria"]:
            criterion_id = criterion["criterion_id"]
            if criterion_id is None:
                continue
            if criterion_id in seen_criterion:
                findings.append(_finding(
                    CATEGORY_DUPLICATE_CRITERION, "C5-DUP-CRITERIA",
                    question_number=number,
                    criterion_ids=[criterion_id],
                    severity="high", state=C2_VERIFIED,
                    explanation="Duplicate criterion identifier; a duplicate "
                    "must not be summed into the maximum.",
                    suggested_action="Re-extract the question criteria."))
            seen_criterion.add(criterion_id)
    return findings


def check_question_totals(questions, *, flat=False):
    """Criteria-weight reconciliation, alternative-aware and honest.

    ``flat=True`` runs the plain sum-vs-total check only where the C2 engine
    could not run (fallback protection); normally C2 owns that rule so no
    issue is reported twice.

    Alternative-method handling (ported from the investigation reviewer):
    when every member of every alternative group carries a distinct branch
    label, the effective total is standalone criteria + the maximum branch
    total per group. Branches are counted ONCE (the largest), never summed.
    A fully consistent labelled structure produces no finding. An
    unlabelled or ambiguous group is escalated (never summed) so a teacher
    reconciles it. Returns (findings, explained_total_mismatch) where the
    second element maps question numbers whose criteria-sum differs from the
    total solely because alternative branches were counted once.
    """
    findings = []
    explained = {}
    for question in questions:
        number = question["question_number"]
        criteria = [c for c in question["criteria"]
                    if c["marks"] is not None and c["criterion_id"] is not None]
        groups = {}
        for criterion in criteria:
            if criterion["alternative_group"] is not None:
                groups.setdefault(criterion["alternative_group"],
                                  []).append(criterion)
        weighted_sum = sum(criterion["marks"] for criterion in criteria)
        if question["max_marks"] is None:
            if criteria:
                findings.append(_finding(
                    CATEGORY_TOTAL_MISMATCH, "C5-TOTAL-ABSENT",
                    question_number=number, severity="low",
                    state=C2_INSUFFICIENT,
                    evidence={"criterion_weight_sum": weighted_sum},
                    explanation="Question has criteria but no declared "
                    "maximum; the sum is never inferred.",
                    suggested_action="Enter the official question maximum."))
            continue
        if not criteria:
            continue
        declared = question["max_marks"]
        if groups:
            group_findings, group_explained = _reconcile_alternative_groups(
                question, criteria, groups, declared, weighted_sum)
            findings += group_findings
            if group_explained is not None:
                explained[number] = group_explained
        elif flat and weighted_sum != declared:
            findings.append(_finding(
                CATEGORY_TOTAL_MISMATCH, "C5-TOTAL-MISMATCH",
                question_number=number,
                criterion_ids=[c["criterion_id"] for c in criteria],
                severity="high", state=C2_VERIFIED,
                evidence={"declared_total": declared,
                          "criterion_weight_sum": weighted_sum},
                explanation="The criterion weights only sum to "
                f"{weighted_sum}, not the declared maximum {declared}; mark "
                "allocations are inconsistent.",
                suggested_action="Find the missing or misallocated criterion "
                "before trusting this question's totals."))
    return findings, explained


def _reconcile_alternative_groups(question, criteria, groups, declared,
                                  weighted_sum):
    """Reconcile alternative groups by branch maximum; never sum branches.

    Returns (findings, explained). ``explained`` is a dict describing a
    legitimate criteria-sum≠total situation that the branch-max structure
    fully accounts for; the redundant mapped C2 total mismatch for such a
    question is suppressed by ``_mark_explained_total_mismatches`` rather
    than double-reported. Returns None when nothing is explained.
    Unlabelled/ambiguous groups are escalated and never summed.
    """
    number = question["question_number"]
    findings = []
    standalone = [c for c in criteria if c["alternative_group"] is None]
    standalone_total = sum(c["marks"] for c in standalone)
    group_total = 0
    unresolved = False
    options_detail = {}
    for name in sorted(groups):
        members = groups[name]
        labels = [c.get("alternative_option") for c in members]
        if any(label is None for label in labels) or len(set(labels)) < 2:
            # Cannot separate branches deterministically: escalate, never sum.
            unresolved = True
            break
        by_option = {}
        for member, label in zip(members, labels):
            by_option.setdefault(label, []).append(member)
        option_totals = {label: sum(c["marks"] for c in members)
                         for label, members in by_option.items()}
        group_total += max(option_totals.values())
        options_detail[name] = option_totals
    if unresolved:
        findings.extend(_alternative_group_findings(
            question, declared, weighted_sum, groups))
        return findings, None

    effective_total = standalone_total + group_total
    explained = {
        "declared_total": declared,
        "raw_criterion_weight_sum": weighted_sum,
        "effective_total": effective_total,
        "alternative_options": options_detail,
        "reason": "alternative_branches_counted_once",
    }
    if effective_total == declared and weighted_sum != declared:
        # The structure is fully consistent; the raw sum differs only because
        # branches were counted once. No new finding is emitted here, and the
        # redundant mapped C2 total mismatch is suppressed downstream by
        # _mark_explained_total_mismatches. The criteria keep their group and
        # branch labels in the extracted channel, so the inputs that justify
        # the suppression stay auditable in the record.
        return findings, explained
    if effective_total < declared:
        findings.append(_finding(
            CATEGORY_CRITERION_MISSING, "C5-CRITERIA-MISSING",
            question_number=number, severity="high", state=C2_VERIFIED,
            evidence={"declared_total": declared,
                      "effective_total": effective_total,
                      "shortfall": declared - effective_total},
            explanation=f"Question '{number}' declares {declared} mark(s) but "
            f"its reconciled criteria (alternative branches counted once) "
            f"account for {effective_total}. Criteria or marks may be "
            "missing; nothing is invented to fill the gap.",
            suggested_action="Find the missing criterion before trusting this "
            "question's totals."))
        return findings, None
    if effective_total > declared:
        findings.append(_finding(
            CATEGORY_TOTAL_MISMATCH, "C5-TOTAL-MISMATCH",
            question_number=number,
            criterion_ids=[c["criterion_id"] for c in criteria],
            severity="high", state=C2_VERIFIED,
            evidence={"declared_total": declared,
                      "effective_total": effective_total,
                      "excess": effective_total - declared,
                      "alternative_options": options_detail},
            explanation=f"Question '{number}' declares {declared} mark(s) but "
            f"its reconciled criteria (alternative branches counted once) "
            f"account for {effective_total}; the largest alternative branch "
            "already exceeds the declared maximum.",
            suggested_action="Check the alternative-method notes: the maximum "
            "must cover the largest single branch, not their sum."))
        return findings, None
    # effective_total == declared and weighted_sum == declared: fully
    # consistent and already reconciled; nothing to report.
    return findings, explained


def _alternative_group_findings(question, declared, weighted_sum, groups):
    """Unlabelled/ambiguous alternative groups must never be summed.

    A human-review finding is produced whenever alternative groups exist but
    their branches cannot be separated deterministically; the review is never
    silent and the raw sum is never treated as authoritative.
    """
    number = question["question_number"]
    detail = {"alternative_groups": {
        name: sorted(c["criterion_id"] or "?" for c in members)
        for name, members in groups.items()},
        "cumulative_interpretation_sum": weighted_sum,
        "declared_total": declared}
    findings = [_finding(
        CATEGORY_ALTERNATIVE_METHOD_CONFLICT, "C5-ALT-SUM-REVIEW",
        question_number=number,
        criterion_ids=[c["criterion_id"] or "?" for members in groups.values()
                       for c in members],
        severity="medium", state=C2_HUMAN_REVIEW,
        evidence=detail,
        explanation="Alternative-method groups exist without separable branch "
        "labels; whether group members are whole alternative methods, "
        "mutually exclusive alternatives, or independent parts needs examiner "
        "judgement. Cumulative summation must never be assumed for totals; "
        "the marked total reconciliation is delegated to teacher review.",
        suggested_action="Check the official special/alternative-method notes "
        "before trusting the maximum.")]
    return findings


def check_criteria_coverage(questions, question_numbers=None, *,
                            require_criteria=True):
    """Declared questions must have criteria; disagree loudly otherwise.

    ``require_criteria=False`` is used by the production maxima-only path,
    where criteria were never part of the extraction and their absence is
    therefore not an extraction defect.
    """
    findings = []
    for question in questions:
        if not question["criteria"]:
            number = question["question_number"]
            if require_criteria and question["max_marks"] is not None:
                findings.append(_finding(
                    CATEGORY_CRITERION_MISSING, "C5-CRITERIA-MISSING",
                    question_number=number, severity="high", state=C2_VERIFIED,
                    evidence={"declared_max_marks": question["max_marks"]},
                    explanation="The question declares a maximum but no "
                    "criteria were interpreted; marking rules are missing.",
                    suggested_action="Re-check the official scheme for this "
                    "question before marking."))
    if question_numbers is not None:
        declared = {question["question_number"] for question in questions}
        for number in list(question_numbers)[:MAX_QUESTION_IDS]:
            number = str(number).strip()
            if number and number not in declared:
                findings.append(_finding(
                    CATEGORY_CRITERION_MISSING, "C5-QUESTION-NOT-INTERPRETED",
                    question_number=number, severity="medium",
                    state=C2_INSUFFICIENT,
                    explanation="A declared question number has no "
                    "interpreted criteria; the interpretation is incomplete.",
                    suggested_action="Re-check the official scheme for this "
                    "question."))
    return findings


def check_follow_through_support(questions):
    """Follow-through requires evidence; the engine never confirms it alone."""
    findings = []
    for question in questions:
        for criterion in question["criteria"]:
            if criterion["follow_through"] is not True:
                continue
            if not criterion["source_evidence"] and criterion["note"] is None:
                findings.append(_finding(
                    CATEGORY_FOLLOW_THROUGH_UNCERTAIN,
                    "C5-FT-UNSUPPORTED", question_number=question["question_number"],
                    criterion_ids=[criterion["criterion_id"] or "?"],
                    severity="medium", state=C2_INSUFFICIENT,
                    evidence={"follow_through": True,
                              "evidence_present": False},
                    explanation="Follow-through is declared without recovered "
                    "evidence; whether ft applies must be verified against "
                    "the official scheme wording.",
                    suggested_action="Find the official follow-through note "
                    "for this criterion."))
    return findings


def check_source_pages(questions):
    """Missing page references for ALL criteria of a question are a finding."""
    findings = []
    for question in questions:
        criteria = [criterion for criterion in question["criteria"]
                    if criterion["criterion_id"] is not None]
        missing_pages = [criterion["criterion_id"] for criterion in criteria
                         if criterion["source_page"] is None]
        if criteria and len(missing_pages) == len(criteria):
            findings.append(_finding(
                CATEGORY_SOURCE_EVIDENCE_MISSING, "C5-SOURCE-PAGE-MISSING",
                question_number=question["question_number"],
                criterion_ids=missing_pages, severity="medium",
                state=C2_INSUFFICIENT,
                explanation="No official page reference exists for any "
                "criterion of this question; missing evidence cannot be "
                "silently certified.",
                suggested_action="Locate the official mark-scheme pages for "
                "this question."))
        elif missing_pages:
            findings.append(_finding(
                CATEGORY_SOURCE_EVIDENCE_MISSING, "C5-SOURCE-PAGE-PARTIAL",
                question_number=question["question_number"],
                criterion_ids=missing_pages, severity="low",
                state=C2_INSUFFICIENT,
                explanation="Some criteria lack an official page reference.",
                suggested_action="Complete the page references before "
                "certifying the interpretation."))
    return findings


# ---------------------------------------------------------------------------
# Deterministic pipeline and evidence binding.

def verify_interpretation(payload, *, question_numbers=None,
                          evidence_document=None, question_mapping=None):
    """Full offline deterministic review of one interpretation payload.

    Returns (questions, findings, checks_run). Uses C2 for structural checks
    and the C5 app-level checks here; evidence attachment runs when a C3
    document record (and/or mapping) is available — page absence and no text
    layer are UNAVAILABLE evidence, verbatim mismatches are CONFLICTS.
    """
    questions, findings = normalize_interpretation(payload)
    findings.extend(_run_all_checks(questions, question_numbers,
                                    evidence_document, question_mapping))
    findings = _consolidate(findings)
    checks_run = list(DETERMINISTIC_CHECKS)
    if evidence_document is not None or question_mapping is not None:
        checks_run = checks_run + ["pdf_evidence_binding"]
    return questions, findings, checks_run


def _run_all_checks(questions, question_numbers=None, evidence_document=None,
                    question_mapping=None):
    """Every deterministic check over normalized questions, in fixed order.

    C2 runs first so the flat total check is delegated to it when it could
    execute (one finding per issue, no duplicated noise); the flat check
    still protects the C2-unavailable path.
    """
    findings = []
    findings.extend(check_question_identifiers(questions))
    findings.extend(check_criteria_weights(questions))
    # C2 structural verification is reused, never duplicated. When the
    # payload cannot be converted, or the engine is unavailable, that fact
    # becomes an evidence-gap finding, never a silent pass.
    c2_findings = None
    if _c2_engine is not None:
        c2_findings = run_c2_verification(questions)
    total_findings, explained_totals = check_question_totals(
        questions, flat=c2_findings is None)
    findings.extend(total_findings)
    findings.extend(check_criteria_coverage(questions, question_numbers))
    findings.extend(check_follow_through_support(questions))
    findings.extend(check_source_pages(questions))
    if _c2_engine is not None:
        if c2_findings is None:
            findings.append(_finding(
                CATEGORY_MALFORMED, "C5-C2-UNSUPPORTED", severity="medium",
                state=C2_INSUFFICIENT,
                explanation="The payload could not be converted for C2 "
                "structural verification; remaining checks are evidence "
                "gaps, not substitutes for C2.",
                suggested_action="Re-check the payload structure."))
        else:
            _mark_explained_total_mismatches(c2_findings, explained_totals)
            findings.extend(c2_findings)
    else:
        findings.append(_finding(
            CATEGORY_MALFORMED, "C5-C2-UNAVAILABLE", severity="medium",
            state=C2_INSUFFICIENT,
            explanation="The C2 engine was unavailable at runtime; "
            "structure checks were skipped, not passed.",
            suggested_action="Restore the verification module."))
    findings.extend(bind_evidence(questions, evidence_document,
                                  question_mapping))
    return findings


def run_c2_verification(questions):
    """Verified structural C2 finding bridge; None when C2 still refuses."""
    payload = _to_c2_payload(questions)
    try:
        return _map_c2_findings(_c2_engine.verify_scheme(payload))
    except SchemeModelError:
        return None
    except Exception:  # safety: never propagate model errors into marking
        return None


def _mark_explained_total_mismatches(c2_findings, explained_totals):
    """Remove a C2 total mismatch that a verified alternative structure explains.

    A labelled alternative structure that legitimately reconciles to the
    declared total (branches counted once) fully accounts for the raw
    criteria-sum≠total situation. That C2 total mismatch is therefore not a
    real inconsistency and is suppressed here: for such a question
    ``check_question_totals`` emits no finding either, so the consistent
    labelled structure reviews cleanly with zero findings (behaviour pinned
    by the ``labelled-alternatives-match`` differential oracle case). The
    audit trace is the group/branch labels the criteria keep in the record's
    extracted channel plus this deterministic, documented rule — no per-review
    trace object is persisted. A total mismatch the structure does NOT
    explain is retained untouched (and ``check_question_totals`` reports its
    own shortfall/overcount finding alongside it).
    """
    if not explained_totals:
        return
    drop = [
        index for index, finding in enumerate(c2_findings)
        if finding.get("rule_id") in ("C5-TOTAL-MISMATCH", "SCHEME-TOTAL-MISMATCH")
        and explained_totals.get(finding.get("question_number")) is not None]
    for index in reversed(drop):
        c2_findings.pop(index)


def bind_evidence(questions, document, mapping):
    """Evidence binding honoured versus C3 document/mapping records."""
    findings = []
    if document is None and mapping is None:
        return findings
    page_count = None
    page_records = {}
    if document is not None:
        page_count = document.get("page_count")
        page_records = {page.get("page"): page
                        for page in document.get("pages", [])}
    mapping_index = {}
    unresolved_ids = set()
    if mapping is not None:
        mapping_index = {entry["question_id"]: entry
                         for entry in (mapping.get("matches") or [])}
        unresolved_ids = {entry["question_id"]
                          for entry in (mapping.get("unresolved") or [])}
    for question in questions:
        number = question["question_number"]
        if mapping is not None:
            entry = mapping_index.get(number)
            if number in unresolved_ids or entry is None:
                findings.append(_finding(
                    CATEGORY_QUESTION_MAPPING_CONFLICT,
                    "C5-MAPPING-CONFLICT", question_number=number,
                    severity="medium", state=C2_INSUFFICIENT,
                    explanation="The question could not be located in the "
                    "official document mapping; mapping is uncertain.",
                    suggested_action="Locate the question pages manually."))
            elif entry.get("certainty") not in ("explicit",):
                findings.append(_finding(
                    CATEGORY_QUESTION_MAPPING_UNCERTAIN,
                    "C5-MAPPING-UNCERTAIN", question_number=number,
                    severity="low", state=C2_INSUFFICIENT,
                    evidence={"certainty": entry.get("certainty")},
                    explanation="Question mapping is not explicitly "
                    "confirmed by headings; teacher should verify the page.",
                    suggested_action="Check the official document pages."))
        for criterion in question["criteria"]:
            page = criterion["source_page"]
            if document is None or page is None:
                continue
            if page_count is not None and not (1 <= page <= page_count):
                findings.append(_finding(
                    CATEGORY_SOURCE_EVIDENCE_MISSING,
                    "C5-SOURCE-PAGE-OUT-OF-RANGE", question_number=number,
                    criterion_ids=[criterion["criterion_id"] or "?"],
                    severity="medium", state=C2_VERIFIED,
                    evidence={"page": page, "page_count": page_count},
                    explanation="Cited page is outside the official "
                    "document; the evidence cannot exist.",
                    suggested_action="Cite a real official page."))
                continue
            page_record = page_records.get(page)
            status = (page_record or {}).get(
                "status", "extracted") if page_record else "extracted"
            text = (page_record or {}).get("text") if page_record else ""
            excerpt = criterion["source_evidence"]
            if status != "extracted":
                findings.append(_finding(
                    CATEGORY_SOURCE_EVIDENCE_MISSING,
                    "C5-EVIDENCE-UNAVAILABLE", question_number=number,
                    criterion_ids=[criterion["criterion_id"] or "?"],
                    severity="medium", state=C2_INSUFFICIENT,
                    evidence={"page": page, "page_status": status},
                    explanation="The cited page's official text is "
                    "unavailable (e.g. no digital text layer or extraction "
                    "failure); unavailable evidence is not a confirmation "
                    "and never silently certified.",
                    suggested_action="Inspect the original page visually "
                    "before relying on this criterion."))
            elif excerpt:
                if excerpt not in (text or ""):
                    findings.append(_finding(
                        CATEGORY_SOURCE_EVIDENCE_CONFLICT,
                        "C5-EVIDENCE-CONFLICT", question_number=number,
                        criterion_ids=[criterion["criterion_id"] or "?"],
                        severity="high", state=C2_VERIFIED,
                        evidence={"page": page},
                        explanation="The cited excerpt does not occur "
                        "verbatim on the official page; the claim is "
                        "contradicted, not merely unverifiable.",
                        suggested_action="Re-read the official wording for "
                        "this criterion."))
    return findings


def _consolidate(findings):
    """Deterministic ordering and bounded dedup of identical findings."""
    seen = set()
    unique = []
    for finding in findings:
        identifier = json.dumps(finding, sort_keys=True,
                                separators=(",", ":"))
        if identifier in seen:
            continue
        seen.add(identifier)
        unique.append(finding)
    return sorted(unique, key=lambda item: item["finding_id"])


def attach_evidence(record, document=None, mapping=None):
    """Bind evidence findings to an existing review record in place.

    Runs the complete deterministic pipeline over the extracted questions
    (fresh checks plus evidence binding), so a previously checked record is
    never left in an inconsistent state; evidence binding is additive, the
    extracted and teacher channels are untouched.
    """
    questions = (record.get("extracted") or {}).get("questions") or []
    additional = bind_evidence(questions, document, mapping)
    checks = record["verified"].setdefault("checks_run", [])
    if document is not None or mapping is not None:
        if "pdf_evidence_binding" not in checks:
            checks.append("pdf_evidence_binding")
    elif _checks_ran(record):
        checks.append("pdf_evidence_rebound")
    findings = _consolidate(
        list((record.get("verified") or {}).get("findings") or [])
        + additional)
    record["verified"]["findings"] = findings
    return record


# ---------------------------------------------------------------------------
# Production helper: review the shared maxima extraction.

def review_extracted_maxima(maxima, answers=None, question_numbers=None,
                            scheme_revision=None, specification=None):
    """Deterministic review of the extracted per-question maxima record.

    This is the production-integration entry point: it consumes the same
    dict that extract_scheme_maxima already produces and adds explicit,
    deterministic checks: identifier validity, integer mark values, coverage
    against declared question numbers, and bounded answer summaries. No
    Gemini call occurs and no criteria exist at this level (criteria-level
    structural checks require a criteria interpretation payload).
    """
    if not isinstance(maxima, dict):
        raise SchemeReviewError("maxima must be a dict of question -> integer")
    payload = {
        "questions": [
            {"question_number": str(number), "part": None,
             "max_marks": value, "criteria": []}
            for number, value in list(maxima.items())[:MAX_QUESTIONS]],
    }
    questions, findings = normalize_interpretation(payload)
    findings.extend(check_question_identifiers(questions))
    if not maxima:
        findings.append(_finding(
            CATEGORY_CRITERION_MISSING, "C5-EXTRACTION-EMPTY",
            severity="high", state=C2_INSUFFICIENT,
            explanation="No question maxima were extracted; nothing was "
            "verified for this scheme revision.",
            suggested_action="Re-run extraction or mark totals manually."))
    findings.extend(check_criteria_coverage(
        questions, question_numbers, require_criteria=False))
    checks = ["question_identifiers", "question_parts", "mark_allocations",
              "missing_criteria_coverage"]
    if _c2_engine is None:
        findings.append(_finding(
            CATEGORY_MALFORMED, "C5-C2-UNAVAILABLE", severity="medium",
            state=C2_INSUFFICIENT,
            explanation="C2 engine unavailable; structural checks skipped.",
            suggested_action="Restore verification modules."))
    findings = _consolidate(findings)
    record = {
        "schema": SCHEME_REVIEW_SCHEMA,
        "specification": _bounded_text(specification, MAX_SPEC_CHARS)
        or "UNKNOWN",
        "assignment_id": None,
        "scheme_revision": scheme_revision or compute_revision(
            {"maxima": maxima, "answers": answers or {}}),
        "extracted": {"maxima": {str(k): v for k, v in
                                 list(maxima.items())[:MAX_QUESTIONS]},
                      "answers": {str(k): _bounded_text(v,
                                   MAX_ANSWER_SUMMARY_CHARS)
                                  for k, v in list((answers or {}).items())
                                  [:MAX_QUESTIONS]
                                  if isinstance(v, str)},
                      "questions": questions},
        "verified": {"checks_run": checks, "findings": findings},
        "assisted": None,
        "teacher": empty_teacher_section(),
        "warnings": [],
    }
    record["teacher"]["status"] = determine_status(record)
    return record


# ---------------------------------------------------------------------------
# Teacher approval state (Phase C5 Stage 9).

def _approval_error(code):
    raise SchemeReviewApprovalError(code)


def _authorized(authorization):
    try:
        return validate_context(authorization)
    except AuthorizationError as exc:
        raise SchemeReviewApprovalError(str(exc)) from None


def _snapshot(record):
    """Hash every reviewed field, including complete source inputs/resolutions."""
    try:
        if not _status_shape_valid(record):
            _approval_error("invalid_review_record")
        payload = {key: value for key, value in record.items() if key != "teacher"}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False)
        if len(encoded) > MAX_SERIALIZED_CHARS * 3:
            _approval_error("review_snapshot_too_large")
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, RecursionError):
        _approval_error("invalid_review_record")


def bind_scheme_review(record, authorization, *, review_id, document_digest):
    """Register a revalidated snapshot in a trusted session authority.

    Server code must supply the actual PDF SHA-256 and fresh review ID from its
    own document/review storage, never a browser claim. Maxima-only production
    warning records intentionally cannot use this privileged API.
    """
    owner = _authorized(authorization)
    _snapshot(record)
    if not identifier(review_id) or not identifier(record.get("scheme_revision")):
        _approval_error("review_identity_and_revision_required")
    if type(document_digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", document_digest):
        _approval_error("document_digest_required")
    if record.get("assignment_id") != authorization.assignment_id:
        _approval_error("assignment_mismatch")
    if "approval_binding" in record or record["teacher"] != {
            "status": determine_status(record), "approved_by": None,
            "approved_revision": None}:
        _approval_error("fresh_review_required")
    inputs = record.get("approval_inputs")
    if type(inputs) is not dict or inputs.get("evidence_document") is None or inputs.get("question_mapping") is None:
        _approval_error("review_source_inputs_required")
    try:
        # Legacy display normalization accepts floats. Privileged approval
        # must not certify a silently truncated official allocation (C5 L1).
        for question in inputs["interpretation"]["questions"]:
            allocations = [question.get("max_marks")] + [
                criterion.get("marks") for criterion in question.get("criteria", [])]
            if any(type(value) not in (int, str) for value in allocations):
                _approval_error("exact_mark_allocations_required")
        rebuilt = build_review(**inputs, specification=record.get("specification"),
                               assignment_id=record.get("assignment_id"),
                               scheme_revision=record["scheme_revision"])
        if (record["verified"] != rebuilt["verified"] or
                record["extracted"] != rebuilt["extracted"]):
            _approval_error("review_revalidation_failed")
        questions = record["extracted"]["questions"]
        if not questions or any(not q["criteria"] for q in questions):
            _approval_error("full_review_required")
    except (KeyError, TypeError, ValueError, RecursionError):
        _approval_error("review_revalidation_failed")
    registry = authorization._authority._reviews
    if review_id in registry or len(registry) >= 100:
        _approval_error("fresh_review_identity_required")
    binding = {"owner": owner, "assignment_id": authorization.assignment_id,
               "assignment_revision": authorization._identity.assignment_revision,
               "review_id": review_id, "document_digest": document_digest}
    candidate = dict(record, approval_binding=binding)
    digest = _snapshot(candidate)
    record["approval_binding"] = binding
    registry[review_id] = {"snapshot": digest, "binding": dict(binding),
                           "identity": authorization._identity,
                           "initial_teacher": dict(record["teacher"]),
                           "decision": None}
    return record


def _bound_review(record, authorization):
    owner = _authorized(authorization)
    _snapshot(record)
    binding = record.get("approval_binding")
    if type(binding) is not dict or not identifier(binding.get("review_id")):
        _approval_error("bound_review_required")
    entry = authorization._authority._reviews.get(binding["review_id"])
    if (entry is None or binding != entry["binding"] or binding.get("owner") != owner
            or binding.get("assignment_id") != authorization.assignment_id
            or record.get("assignment_id") != authorization.assignment_id
            or entry["identity"] != authorization._identity):
        _approval_error("review_authorization_mismatch")
    if entry["snapshot"] != _snapshot(record):
        _approval_error("review_content_changed")
    if record["teacher"] != (entry["decision"] or entry["initial_teacher"]):
        _approval_error("review_decision_changed")
    return entry


def _resolvable(finding):
    # Only heuristic mapping uncertainty can be confirmed without changing the
    # interpretation. All other findings require correction and a new review.
    return (finding.get("rule_id") == "C5-MAPPING-UNCERTAIN"
            and finding.get("category") == CATEGORY_QUESTION_MAPPING_UNCERTAIN
            and finding.get("state") == C2_INSUFFICIENT
            and finding.get("origin") == "deterministic")


def resolve_findings(record, finding_ids, authorization, *, reason=None, evidence=None):
    """Resolve ONE allowlisted finding with attributable, bound source evidence.

    No blanket waiver. Evidence is {document_digest, page, excerpt}; it must
    match a cited criterion and the mapped page in the registered source.
    """
    entry = _bound_review(record, authorization)
    if entry["decision"] is not None:
        _approval_error("fresh_review_required")
    if type(finding_ids) is not list or len(finding_ids) != 1 or type(finding_ids[0]) is not str:
        _approval_error("individual_finding_required")
    matches = [f for f in record["verified"]["findings"] if f["finding_id"] == finding_ids[0]]
    if len(matches) != 1 or not _resolvable(matches[0]):
        _approval_error("finding_requires_correction_and_revalidation")
    if type(reason) is not str or not reason.strip() or len(reason) > MAX_NOTE_CHARS:
        _approval_error("resolution_reason_required")
    if type(evidence) is not dict or set(evidence) != {"document_digest", "page", "excerpt"}:
        _approval_error("resolution_evidence_required")
    page, excerpt = evidence["page"], evidence["excerpt"]
    if (evidence["document_digest"] != entry["binding"]["document_digest"]
            or type(page) is not int or page < 1 or type(excerpt) is not str
            or not excerpt.strip() or len(excerpt) > MAX_EXCERPT_CHARS):
        _approval_error("resolution_evidence_invalid")
    finding = matches[0]
    inputs = record["approval_inputs"]
    pages = inputs["evidence_document"].get("pages", [])
    mapping = inputs["question_mapping"].get("matches", [])
    questions = record["extracted"]["questions"]
    valid = (any(p.get("page") == page and p.get("status") == "extracted"
                 and excerpt in p.get("text", "") for p in pages)
             and any(m.get("question_id") == finding["question_number"]
                     and page in m.get("pages", []) for m in mapping)
             and any(q["question_number"] == finding["question_number"]
                     and any(c["source_page"] == page and c["source_evidence"] == excerpt
                             for c in q["criteria"]) for q in questions))
    if not valid:
        _approval_error("resolution_evidence_unverified")
    # All checks precede mutation. Exact source text stays in session only.
    finding.update(teacher_resolved=True, resolved_by=entry["binding"]["owner"],
                   resolution_reason=reason.strip(), resolution_evidence=dict(evidence))
    entry["snapshot"] = _snapshot(record)
    refresh_status(record)
    return record


def _blocking_findings(record):
    """Structural hard-blocker classification (unchanged C5 contract)."""
    return [finding for finding in unresolved_findings(record)
            if finding["state"] == C2_VERIFIED
            or finding["category"] in EVIDENCE_BLOCKING_CATEGORIES
            or finding["rule_id"] in BLOCKING_RULES]


def approve_scheme_review(record, authorization, scheme_revision):
    """Approve only the current registered, authorized, fully resolved review."""
    entry = _bound_review(record, authorization)
    if not identifier(scheme_revision) or scheme_revision != record.get("scheme_revision"):
        _approval_error("approval_revision_mismatch")
    if entry["decision"] is not None and entry["decision"]["status"] == STATUS_REJECTED:
        _approval_error("rejected_review_requires_regeneration")
    # Even soft uncertainties require an individual verified resolution before
    # approval, while retaining C5's separate structural-blocker classification.
    blockers = unresolved_findings(record)
    if blockers:
        _approval_error("unresolved_finding_" + blockers[0]["category"])
    assisted = record.get("assisted") or {}
    if assisted.get("findings") or assisted.get("failure_note"):
        _approval_error("assisted_review_unresolved")
    decision = {"status": STATUS_APPROVED, "approved_by": entry["binding"]["owner"],
                "approved_revision": scheme_revision}
    entry["decision"] = dict(decision)
    record["teacher"] = decision
    return record


def reject_scheme_review(record, authorization):
    entry = _bound_review(record, authorization)
    decision = {"status": STATUS_REJECTED, "approved_by": entry["binding"]["owner"],
                "approved_revision": record["scheme_revision"]}
    entry["decision"] = dict(decision)
    record["teacher"] = decision
    return record


def approval_state(record, authorization, scheme_revision):
    """Single authoritative effective-approval evaluator; metadata is not proof."""
    try:
        entry = _bound_review(record, authorization)
        if scheme_revision != record.get("scheme_revision"):
            return "stale_revision", "scheme_revision_changed"
        decision = entry["decision"]
        if decision is None or decision["status"] != STATUS_APPROVED:
            return "absent", None
        assisted = record.get("assisted") or {}
        if unresolved_findings(record) or assisted.get("findings") or assisted.get("failure_note"):
            return "stale_revision", "unresolved_review"
        return "approved", None
    except SchemeReviewApprovalError as exc:
        return "invalid", str(exc)


# ---------------------------------------------------------------------------
# Bounded assisted-review response handling (mocked/offline only in C5).

def parse_assisted_response(response, *, diagnostic=None):
    """Strictly parse a mocked assisted response into (findings, outcome).

    Mirrors the production audit-path safeguards: finish-reason allowlist,
    response bounds, JSON validation, shape validation. Responses come from
    tests only; no client call is made here.
    """
    candidates = getattr(response, "candidates", None) or []
    finish = getattr(candidates[0], "finish_reason", None) if candidates else None
    if finish is not None and str(getattr(finish, "value", finish)) != "STOP":
        return None, "blocked_or_incomplete_response"
    try:
        text = response.text
    except AttributeError:
        return None, "empty_response"
    if not isinstance(text, str) or not text.strip():
        return None, "empty_response"
    if len(text) > MAX_ASSISTED_RESPONSE_CHARS:
        return None, "response_too_large"
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None, "invalid_json"
    if not isinstance(payload, dict) or not isinstance(
            payload.get("findings"), list):
        return None, "invalid_response_shape"
    try:
        findings = normalize_assisted_findings(payload["findings"])
    except AssistedResponseError as exc:
        return None, exc.outcome
    outcome = "completed_no_findings" if not findings else "completed"
    return findings, outcome


def normalize_assisted_findings(items):
    """Validate a Gemini-assisted findings list strictly (fail-closed)."""
    if not isinstance(items, list):
        raise AssistedResponseError("invalid_response_shape")
    if len(items) > MAX_ASSISTED_FINDINGS:
        raise AssistedResponseError("invalid_response_shape")
    findings = []
    for item in items:
        if not isinstance(item, dict):
            raise AssistedResponseError("invalid_response_shape")
        category = item.get("category")
        if category not in ASSISTED_CATEGORIES:
            raise AssistedResponseError("invalid_response_shape")
        explanation = item.get("explanation")
        if not isinstance(explanation, str) or not explanation.strip():
            raise AssistedResponseError("invalid_response_shape")
        confidence = item.get("confidence")
        if confidence is not None and confidence not in ASSISTED_CONFIDENCES:
            raise AssistedResponseError("invalid_response_shape")
        question_number = item.get("question_number")
        if question_number is not None:
            question_number = _bounded_text(question_number, MAX_ID_CHARS)
        else:
            question_number = None
        criterion_id = item.get("criterion_id")
        if criterion_id is not None:
            criterion_id = _bounded_text(criterion_id, MAX_ID_CHARS)
        finding = _finding(
            category, "ASSISTED-" + category, question_number=question_number,
            criterion_ids=[criterion_id] if criterion_id else (),
            severity="low", state=C2_HUMAN_REVIEW,
            evidence={"evidence_reference": _bounded_text(
                item.get("evidence_reference"), MAX_EXCERPT_CHARS) or None},
            explanation=explanation,
            suggested_action=_bounded_text(
                item.get("suggested_action"), MAX_NOTE_CHARS),
            origin=ASSISTED_ORIGIN,
            confidence=confidence)
        findings.append(finding)
    return findings


def apply_assisted_findings(record, findings, outcome):
    """Attach assisted findings as SUGGESTIONS; never auto-certification.

    Assisted findings live in their own ``assisted`` channel, separate from
    deterministic ``verified`` results, and force NEEDS_TEACHER_REVIEW.
    """
    if outcome == "completed_no_findings":
        record["assisted"] = {"findings": [], "outcome": outcome,
                              "suggestions": True}
    elif outcome == "completed":
        record["assisted"] = {"findings": list(findings), "outcome": outcome,
                              "suggestions": True}
    else:
        record["assisted"] = {"findings": [], "outcome": outcome,
                              "suggestions": True,
                              "failure_note": outcome}
    refresh_status(record)
    return record


def apply_assisted_outcome(record, outcome):
    """Record a failed/absent assisted pass; fail-safe to teacher review."""
    if outcome not in ASSISTED_OUTCOMES or outcome in (
            "completed", "completed_no_findings"):
        raise SchemeReviewError("unsupported assisted outcome")
    return apply_assisted_findings(record, [], outcome)


def classify_assisted_failure(exception):
    """Content-free outcome classification of a (mocked) provider failure."""
    name = type(exception).__name__.upper()
    text_value = ""
    try:
        text_value = str(exception)
    except Exception:
        text_value = ""
    lowered = text_value.lower()
    if isinstance(exception, TimeoutError) or "timeout" in name or (
            "timeout" in lowered or "deadline" in lowered):
        return "timeout"
    if isinstance(exception, AssistedResponseError):
        return exception.outcome
    if ("quota" in lowered or "resource" in lowered
            or "rate" in lowered or "exhaust" in lowered):
        return "quota_exhausted"
    return "model_error"
