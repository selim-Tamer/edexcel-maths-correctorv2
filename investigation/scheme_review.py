"""Phase C Stage 5: reviewed mark-scheme interpretations (offline-first).

This module is the C5 review layer. It does NOT extract mark schemes itself and
it does NOT replace the marking engine. Instead it composes the existing
offline stages:

- Stage C2 (:mod:`investigation.scheme_model`,
  :mod:`investigation.verification_engine`) provides the strict structural
  scheme model and deterministic structural findings.
- Stage C3 (:mod:`investigation.pdf_evidence`,
  :mod:`investigation.pdf_question_map`) provides page evidence and
  conservative question-to-page mappings.
- Stage C4 (SymPy verification) remains a separate stage and is never invoked
  or modified here. C2's "mathematical claim not verified" finding is passed
  through unchanged.

The reviewer deliberately keeps three information states separate:

``extracted``
    What the model believes the mark scheme says. Never certified.

``verified``
    What deterministic checks establish from explicit declarations and
    supplied C3 page evidence. A deterministic finding is a statement about
    the *review record*; it never asserts that an official Pearson mark scheme
    is wrong.

``teacher``
    What an authenticated, owner-bound teacher explicitly accepted or
    rejected. Approval is bound to one assignment revision and one scheme
    digest. A different reviewer never inherits it.

Safety properties (enforced in code and tests):

- The official scheme is authoritative. Every review result says so.
- Missing page evidence is visible and blocks approval; it is never filled by
  inference.
- Alternative methods are reconciled with a group-aware effective total; they
  are never simply summed.
- Follow-through is checked only against explicit declarations; uncertain
  cases become review warnings, not invented rules.
- A provider (Gemini) review is optional, explicitly supplied, bounded to one
  request, and can only add *suggestion* findings. It cannot add, remove or
  reweight criteria, cannot change marks, and cannot approve anything.
- Provider responses are validated strictly. A finding that cites a
  non-existent criterion, an unknown category or a fabricated page excerpt is
  rejected rather than accepted.
- No network, Google SDK, Streamlit, student data or file IO appears here.
  This module is investigation-only and is not imported by the production app.

The public entry points are :func:`review_scheme`,
:func:`build_provider_packet` and :class:`ReviewCache`.
"""
import copy
import hashlib
import json
import re

from investigation.pdf_evidence import (
    MAX_EXCERPT_CHARS,
    evidence_reference,
    validate_document_evidence,
)
from investigation.pdf_question_map import (
    CONTINUATION_POLICY_HEURISTIC_ADJACENT,
    METHOD_CONTINUATION,
    map_question_pages,
    validate_question_mapping,
)
from investigation.scheme_model import (
    SchemeModelError,
    code_has_follow_through_suffix,
    code_is_supported,
    parse_scheme,
)
from investigation.verification_engine import (
    HUMAN_REVIEW,
    INSUFFICIENT,
    SUSPECTED,
    VERIFIED,
    verify_scheme_document,
)

REVIEW_SCHEMA = "investigation.scheme_review/1"
RESULT_SCHEMA = "investigation.scheme_review.result/1"
PROVIDER_PACKET_SCHEMA = "investigation.scheme_review.provider_packet/1"

REVIEW_STATUSES = (
    "UNREVIEWED",
    "CHECKED",
    "NEEDS_TEACHER_REVIEW",
    "TEACHER_APPROVED",
    "REJECTED",
)
CONFIDENCES = ("high", "medium", "low", "unknown")
MARK_TYPES = ("M", "A", "B", "UNKNOWN")
DECISION_KINDS = ("approve", "reject", "needs_review")
TARGET_KINDS = ("scheme", "question", "criterion")
SOURCE_ROLES = ("mark_scheme", "question_paper")

PROVIDER_CATEGORIES = (
    "TOTAL_MISMATCH",
    "CRITERION_MISSING",
    "CRITERION_UNSUPPORTED",
    "MARK_TYPE_UNCERTAIN",
    "DEPENDENCY_CONFLICT",
    "FOLLOW_THROUGH_UNCERTAIN",
    "ALTERNATIVE_METHOD_CONFLICT",
    "QUESTION_MAPPING_CONFLICT",
    "SOURCE_EVIDENCE_MISSING",
)

PROVIDER_STATUSES = (
    "not_requested",
    "completed",
    "completed_with_rejections",
    "invalid_json",
    "schema_invalid",
    "timeout",
    "quota_limited",
    "blocked",
    "empty_response",
    "unavailable",
    "anonymous_refused",
    "unavailable_evidence",
    "request_limit",
)

MAX_QUESTIONS = 100
MAX_CRITERIA = 50
MAX_CLAIMS = 5000
MAX_DECISIONS = 200
MAX_ACKNOWLEDGEMENTS = 500
MAX_PROVIDER_FINDINGS = 100
MAX_PROVIDER_REQUESTS = 1
MAX_TEXT_CHARS = 2000
MAX_EXPLANATION_CHARS = 1000
MAX_ACTION_CHARS = 300
MAX_REVIEWER_CHARS = 200
MAX_ASSIGNMENT_CHARS = 200
MAX_SPECIFICATION_CHARS = 300
MAX_PACKET_PAGE_CHARS = 4000
MAX_PACKET_CHARS = 120_000
# A full-paper review result is larger than a provider packet because it keeps
# the extracted record, every finding and every evidence reference separate.
# The result remains strictly bounded, but the bound must allow a realistic
# 4MA1 paper (roughly 100-200 criteria) to be represented without truncation.
MAX_RESULT_CHARS = 2_000_000

_QUESTION_ID_PATTERN = re.compile(
    r"^(?:(?:Q|Question)\s*)?[1-9][0-9]{0,2}$", re.IGNORECASE)
_PART_PATTERN = re.compile(
    r"^(?:[a-z]|\([a-z]\)|"
    r"(?:[a-z]|\([a-z]\))\((?:i|ii|iii|iv|v)\)|"
    r"\((?:i|ii|iii|iv|v)\))$", re.IGNORECASE)
_CANONICAL_PART_PATTERN = re.compile(
    r"^(?:[a-z]|\([a-z]\)|(?:[a-z]|\([a-z]\))\((?:i|ii|iii|iv|v)\)|"
    r"\((?:i|ii|iii|iv|v)\))$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_CLAIM_KEYS = {
    "part", "mark_type", "marks", "follow_through", "alternative_option",
    "extraction_confidence", "source",
}
_SOURCE_KEYS = {"document_role", "page", "excerpt"}
_DECISION_KEYS = {
    "decision_id", "owner_id", "decision", "target", "assignment_id",
    "revision", "scheme_sha256", "acknowledged_finding_ids", "reason",
}

_REVIEW_TOP_LEVEL_KEYS = {
    "schema", "specification", "assignment", "reviewer_id", "proposed_scheme",
    "criterion_claims", "source_documents", "question_mapping",
    "teacher_decisions",
}
_ASSIGNMENT_KEYS = {
    "assignment_id", "paper_id", "session", "revision", "scheme_sha256",
}

SAFEGUARDS = {
    "official_scheme_authoritative": True,
    "provider_findings_are_proposals": True,
    "publishes_grades": False,
    "affects_student_marks": False,
    "requires_teacher_confirmation": True,
    "live_provider_calls": False,
}

PROVIDER_PROMPT = (
    "You are reviewing a PROPOSED extraction of an official mathematics mark "
    "scheme. The official document is authoritative. Compare the proposed "
    "criteria and their cited page excerpts with the supplied source pages. "
    "Report only discrepancies you can support with the supplied evidence. "
    "Do not award or change student marks. Do not invent criteria, pages or "
    "excerpts. Return JSON findings only, using the allowed categories. "
    "A finding is a proposal for teacher review, not a certification."
)

PROVIDER_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": list(PROVIDER_CATEGORIES),
                    },
                    "question_id": {"type": "string"},
                    "criterion_ids": {
                        "type": "array", "items": {"type": "string"},
                    },
                    "source": {
                        "type": "object",
                        "properties": {
                            "document_role": {"type": "string"},
                            "page": {"type": "integer"},
                            "excerpt": {"type": "string"},
                        },
                        "required": ["document_role", "page"],
                    },
                    "explanation": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "suggested_action": {"type": "string"},
                },
                "required": [
                    "category", "question_id", "criterion_ids",
                    "explanation", "confidence", "suggested_action",
                ],
            },
        }
    },
    "required": ["findings"],
}


class SchemeReviewError(ValueError):
    """Malformed review input; messages are content-free and position-only."""


class ProviderReviewError(RuntimeError):
    """Bounded provider failure; carries only a allowlisted category."""

    def __init__(self, category):
        self.category = category if category in PROVIDER_STATUSES else "unavailable"
        super().__init__("Scheme review provider unavailable: " + self.category)


# ---------------------------------------------------------------------------
# Small validation helpers


def _require_string(value, position, field, limit):
    if not isinstance(value, str) or not value.strip():
        raise SchemeReviewError(
            f"{position}: a non-empty string '{field}' is required")
    if len(value) > limit:
        raise SchemeReviewError(
            f"{position}: '{field}' exceeds the {limit}-character limit")
    return value


def _optional_string(value, position, field, limit):
    if value is None:
        return None
    return _require_string(value, position, field, limit)


def _strict_int(value, position, field, minimum=None):
    if type(value) is not int:
        raise SchemeReviewError(
            f"{position}: field '{field}' must be an integer")
    if minimum is not None and value < minimum:
        raise SchemeReviewError(
            f"{position}: field '{field}' must be at least {minimum}")
    return value


def _optional_digest(value, position, field):
    if value is None:
        return None
    if not isinstance(value, str) or not _DIGEST_PATTERN.fullmatch(value):
        raise SchemeReviewError(
            f"{position}: field '{field}' must be a lowercase sha256 hex digest")
    return value


def _bounded_json(value, position, what, limit=MAX_PACKET_CHARS):
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise SchemeReviewError(
            f"{position}: {what} must be JSON-serializable data") from exc
    if len(encoded) > limit:
        raise SchemeReviewError(
            f"{position}: {what} exceeds the serialized size limit")


def _normalize_assignment(raw):
    if not isinstance(raw, dict):
        raise SchemeReviewError("Field 'assignment' must be an object")
    unknown = set(raw) - _ASSIGNMENT_KEYS
    if unknown:
        raise SchemeReviewError("Assignment has unexpected fields")
    return {
        "assignment_id": _require_string(
            raw.get("assignment_id"), "assignment", "assignment_id",
            MAX_ASSIGNMENT_CHARS),
        "paper_id": _require_string(
            raw.get("paper_id"), "assignment", "paper_id",
            MAX_ASSIGNMENT_CHARS),
        "session": _optional_string(
            raw.get("session"), "assignment", "session",
            MAX_ASSIGNMENT_CHARS),
        "revision": _strict_int(
            raw.get("revision"), "assignment", "revision", minimum=1),
        "scheme_sha256": _optional_digest(
            raw.get("scheme_sha256"), "assignment", "scheme_sha256"),
    }


def _normalize_source_claim(raw, position):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SchemeReviewError(
            f"{position}: field 'source' must be an object or omitted")
    unknown = set(raw) - _SOURCE_KEYS
    if unknown:
        raise SchemeReviewError(
            f"{position}: source has unexpected fields")
    role = raw.get("document_role")
    if role not in SOURCE_ROLES:
        raise SchemeReviewError(
            f"{position}: source document_role is not a known role")
    page = _strict_int(raw.get("page"), position, "source.page", minimum=1)
    excerpt = raw.get("excerpt")
    if excerpt is not None:
        if not isinstance(excerpt, str) or not excerpt:
            raise SchemeReviewError(
                f"{position}: source excerpt must be a non-empty string or "
                "omitted")
        if len(excerpt) > MAX_EXCERPT_CHARS:
            raise SchemeReviewError(
                f"{position}: source excerpt exceeds "
                f"{MAX_EXCERPT_CHARS} characters")
    return {"document_role": role, "page": page, "excerpt": excerpt}


def _normalize_claim(raw, position):
    if not isinstance(raw, dict):
        raise SchemeReviewError(
            f"{position}: a criterion claim must be an object")
    unknown = set(raw) - _CLAIM_KEYS
    if unknown:
        raise SchemeReviewError(
            f"{position}: criterion claim has unexpected fields")
    part = raw.get("part")
    if part is not None:
        if not isinstance(part, str) or not part.strip():
            raise SchemeReviewError(
                f"{position}: field 'part' must be a non-empty string or "
                "omitted")
        if len(part) > 40:
            raise SchemeReviewError(
                f"{position}: field 'part' exceeds 40 characters")
    mark_type = raw.get("mark_type")
    if mark_type is not None and mark_type not in MARK_TYPES:
        raise SchemeReviewError(
            f"{position}: field 'mark_type' is not a supported type")
    marks = raw.get("marks")
    if marks is not None:
        marks = _strict_int(marks, position, "marks", minimum=0)
    follow_through = raw.get("follow_through")
    if follow_through is not None and type(follow_through) is not bool:
        raise SchemeReviewError(
            f"{position}: field 'follow_through' must be a strict bool or "
            "omitted")
    option = raw.get("alternative_option")
    if option is not None:
        if not isinstance(option, str) or not option.strip():
            raise SchemeReviewError(
                f"{position}: field 'alternative_option' must be a "
                "non-empty string or omitted")
        if len(option) > 100:
            raise SchemeReviewError(
                f"{position}: field 'alternative_option' exceeds 100 "
                "characters")
    confidence = raw.get("extraction_confidence", "unknown")
    if confidence not in CONFIDENCES:
        raise SchemeReviewError(
            f"{position}: field 'extraction_confidence' is not a supported "
            "confidence")
    return {
        "part": part,
        "mark_type": mark_type,
        "marks": marks,
        "follow_through": follow_through,
        "alternative_option": option,
        "extraction_confidence": confidence,
        "source": _normalize_source_claim(raw.get("source"), position),
    }


def _normalize_target(raw, position):
    if not isinstance(raw, dict):
        raise SchemeReviewError(f"{position}: target must be an object")
    if set(raw) - {"kind", "id"}:
        raise SchemeReviewError(f"{position}: target has unexpected fields")
    kind = raw.get("kind")
    if kind not in TARGET_KINDS:
        raise SchemeReviewError(f"{position}: unknown target kind")
    target_id = raw.get("id")
    if kind == "scheme":
        if target_id is not None:
            raise SchemeReviewError(
                f"{position}: a scheme target cannot carry an id")
    else:
        target_id = _require_string(
            target_id, position, "target.id", MAX_ASSIGNMENT_CHARS)
    return {"kind": kind, "id": target_id}


def _normalize_decision(raw, index):
    position = f"teacher_decisions[{index}]"
    if not isinstance(raw, dict):
        raise SchemeReviewError(f"{position}: decision must be an object")
    unknown = set(raw) - _DECISION_KEYS
    if unknown:
        raise SchemeReviewError(f"{position}: decision has unexpected fields")
    decision = raw.get("decision")
    if decision not in DECISION_KINDS:
        raise SchemeReviewError(f"{position}: unknown decision")
    acknowledgements = raw.get("acknowledged_finding_ids", [])
    if not isinstance(acknowledgements, list) \
            or len(acknowledgements) > MAX_ACKNOWLEDGEMENTS:
        raise SchemeReviewError(
            f"{position}: acknowledged_finding_ids must be a list of at most "
            f"{MAX_ACKNOWLEDGEMENTS} ids")
    cleaned = []
    for item in acknowledgements:
        cleaned.append(_require_string(
            item, position, "acknowledged_finding_id", 400))
    if len(set(cleaned)) != len(cleaned):
        raise SchemeReviewError(
            f"{position}: acknowledged finding ids repeat")
    reason = raw.get("reason", "")
    if not isinstance(reason, str) or len(reason) > MAX_TEXT_CHARS:
        raise SchemeReviewError(
            f"{position}: reason must be a bounded string")
    return {
        "decision_id": _require_string(
            raw.get("decision_id"), position, "decision_id", 200),
        "owner_id": _require_string(
            raw.get("owner_id"), position, "owner_id", MAX_REVIEWER_CHARS),
        "decision": decision,
        "target": _normalize_target(raw.get("target"), position),
        "assignment_id": _require_string(
            raw.get("assignment_id"), position, "assignment_id",
            MAX_ASSIGNMENT_CHARS),
        "revision": _strict_int(
            raw.get("revision"), position, "revision", minimum=1),
        "scheme_sha256": _optional_digest(
            raw.get("scheme_sha256"), position, "scheme_sha256"),
        "acknowledged_finding_ids": cleaned,
        "reason": reason,
    }


def _normalize_request(payload):
    """Validate a review request and return a deep-copied normalized record."""
    if not isinstance(payload, dict):
        raise SchemeReviewError("A review request must be an object")
    unknown = set(payload) - _REVIEW_TOP_LEVEL_KEYS
    if unknown:
        raise SchemeReviewError("Review request has unexpected top-level keys")
    if payload.get("schema") != REVIEW_SCHEMA:
        raise SchemeReviewError("Unknown review schema")
    specification = _require_string(
        payload.get("specification"), "request", "specification",
        MAX_SPECIFICATION_CHARS)
    assignment = _normalize_assignment(payload.get("assignment"))
    reviewer_id = payload.get("reviewer_id")
    if reviewer_id is not None:
        reviewer_id = _require_string(
            reviewer_id, "request", "reviewer_id", MAX_REVIEWER_CHARS)
    try:
        document = parse_scheme(copy.deepcopy(payload.get("proposed_scheme")))
    except SchemeModelError as exc:
        raise SchemeReviewError(
            "The proposed scheme is malformed; no review was performed") from exc
    if len(document.questions) > MAX_QUESTIONS:
        raise SchemeReviewError("Too many questions in the proposed scheme")

    claims_raw = payload.get("criterion_claims", {})
    if not isinstance(claims_raw, dict):
        raise SchemeReviewError("criterion_claims must be an object")
    if len(claims_raw) > MAX_CLAIMS:
        raise SchemeReviewError("Too many criterion claims")
    claims = {}
    for criterion_id, claim in claims_raw.items():
        if not isinstance(criterion_id, str) or not criterion_id.strip() \
                or len(criterion_id) > 200:
            raise SchemeReviewError(
                "criterion_claims keys must be non-empty strings of at most "
                "200 characters")
        claims[criterion_id] = _normalize_claim(
            claim, f"criterion_claims['{criterion_id}']")

    sources_raw = payload.get("source_documents", {})
    if not isinstance(sources_raw, dict) or set(sources_raw) - set(SOURCE_ROLES):
        raise SchemeReviewError(
            "source_documents must contain only mark_scheme/question_paper")
    sources = {}
    for role, record in sources_raw.items():
        if record is None:
            sources[role] = None
            continue
        try:
            sources[role] = copy.deepcopy(validate_document_evidence(record))
        except ValueError as exc:
            raise SchemeReviewError(
                f"source_documents['{role}'] is not a valid C3 record") from exc
        if sources[role]["document_role"] != role:
            raise SchemeReviewError(
                f"source_documents['{role}'] has a different document_role")
    mapping = payload.get("question_mapping")
    if mapping is not None:
        try:
            mapping = copy.deepcopy(validate_question_mapping(mapping))
        except ValueError as exc:
            raise SchemeReviewError(
                "question_mapping is not a valid C3 mapping") from exc

    decisions_raw = payload.get("teacher_decisions", [])
    if not isinstance(decisions_raw, list) \
            or len(decisions_raw) > MAX_DECISIONS:
        raise SchemeReviewError(
            f"teacher_decisions must be a list of at most {MAX_DECISIONS}")
    decisions = [_normalize_decision(entry, index)
                 for index, entry in enumerate(decisions_raw)]
    decision_ids = [entry["decision_id"] for entry in decisions]
    if len(set(decision_ids)) != len(decision_ids):
        raise SchemeReviewError("teacher decision ids repeat")

    normalized = {
        "schema": REVIEW_SCHEMA,
        "specification": specification,
        "assignment": assignment,
        "reviewer_id": reviewer_id,
        "proposed_scheme": document,
        "criterion_claims": claims,
        "source_documents": sources,
        "question_mapping": mapping,
        "teacher_decisions": decisions,
    }
    _bounded_json(payload, "request", "review request")
    return normalized


# ---------------------------------------------------------------------------
# Findings


def _finding(rule_id, category, state, severity, question_id,
             criterion_ids, evidence, explanation, *, blocking,
             acknowledgement_required, discriminator=None,
             origin="deterministic"):
    ordered = sorted(criterion_ids)
    parts = ["C5", rule_id, question_id or "-", ",".join(ordered)]
    if discriminator is not None:
        parts.append(str(discriminator))
    return {
        "finding_id": "::".join(parts),
        "rule_id": rule_id,
        "issue_category": category,
        "state": state,
        "severity": severity,
        "question_id": question_id,
        "criterion_ids": ordered,
        "evidence": evidence,
        "explanation": explanation,
        "review_required": True,
        "origin": origin,
        "blocks_approval": bool(blocking),
        "acknowledgement_required": bool(acknowledgement_required),
    }


def _criterion_ids(question):
    return [criterion.id for criterion in question.criteria]


def _derived_mark_type(code):
    if not code_is_supported(code):
        return "UNKNOWN", False
    return ("M" if code.startswith("dM") else code[0]), code.startswith("dM")


def _derived_mark_types(document):
    """Derived code interpretation, kept separate from extracted claims."""
    entries = []
    for question, criterion in document.criteria():
        mark_type, dependent = _derived_mark_type(criterion.code)
        entries.append({
            "question_id": question.id,
            "criterion_id": criterion.id,
            "code": criterion.code,
            "supported_code": code_is_supported(criterion.code),
            "derived_mark_type": mark_type,
            "dependent_method": dependent,
            "follow_through_suffix": code_has_follow_through_suffix(
                criterion.code or ""),
        })
    return entries


def _canonical_question_id(question_id):
    """The comparable main-question number, or None for a nonstandard id."""
    if not isinstance(question_id, str):
        return None
    match = re.fullmatch(
        r"(?:(?:Q|Question)\s*)?([1-9][0-9]{0,2})", question_id,
        re.IGNORECASE)
    return None if match is None else match.group(1)


def _unique_findings(findings):
    """Ensure every finding id is unique without discarding evidence."""
    grouped = {}
    for finding in findings:
        grouped.setdefault(finding["finding_id"], []).append(finding)
    ordered = []
    for finding_id in sorted(grouped):
        group = sorted(
            grouped[finding_id],
            key=lambda item: json.dumps(item, sort_keys=True, default=str))
        if len(group) == 1:
            ordered.append(group[0])
            continue
        for index, finding in enumerate(group):
            finding["finding_id"] = f"{finding_id}#{index}"
            ordered.append(finding)
    return ordered


# ---------------------------------------------------------------------------
# Deterministic checks


def _check_questions(document):
    findings = []
    if not document.questions:
        findings.append(_finding(
            "C5-SCHEME-EMPTY", "empty_scheme", VERIFIED, "high", None, [],
            {}, "The proposed scheme contains no questions, so there is no "
            "mark-scheme interpretation to review.",
            blocking=True, acknowledgement_required=False))
    for question in document.questions:
        if not _QUESTION_ID_PATTERN.fullmatch(question.id):
            findings.append(_finding(
                "C5-QUESTION-ID-INVALID", "invalid_question_id", VERIFIED,
                "high", question.id, _criterion_ids(question),
                {"question_id": question.id,
                 "accepted_examples": ["1", "Q1", "Question 1"]},
                f"Question identifier '{question.id}' is outside the "
                "conservative main-question notation. Parts belong in the "
                "'part' claim, not in the main question id.",
                blocking=True, acknowledgement_required=False,
                discriminator=question.id))
        if not question.criteria:
            findings.append(_finding(
                "C5-QUESTION-NO-CRITERIA", "question_without_criteria",
                INSUFFICIENT, "high", question.id, [],
                {"total": question.total},
                f"Question '{question.id}' has no proposed criteria; a total "
                "cannot be reconciled and no criterion can be certified.",
                blocking=True, acknowledgement_required=False))
    return findings


def _check_claims(document, claims):
    findings = []
    known_criteria = {criterion.id for _, criterion in document.criteria()}
    for criterion_id in sorted(set(claims) - known_criteria):
        findings.append(_finding(
            "C5-CLAIM-UNKNOWN-CRITERION", "claim_for_unknown_criterion",
            VERIFIED, "high", None, [criterion_id],
            {"criterion_id": criterion_id},
            f"A criterion claim refers to '{criterion_id}', which is not a "
            "criterion in the proposed scheme. The claim is not attached or "
            "certified.", blocking=True, acknowledgement_required=False,
            discriminator=criterion_id))

    location = {}
    for question_position, question in enumerate(document.questions):
        for criterion_position, criterion in enumerate(question.criteria):
            location[criterion.id] = (
                question, criterion, question_position, criterion_position)

    for question, criterion in document.criteria():
        claim = claims.get(criterion.id)
        if claim is None:
            findings.append(_finding(
                "C5-EXTRACTION-CLAIM-MISSING", "extraction_claim_missing",
                INSUFFICIENT, "high", question.id, [criterion.id],
                {"criterion_id": criterion.id},
                f"Criterion '{criterion.id}' has no extraction claim. Its "
                "part, mark type, confidence and source evidence are "
                "unavailable; nothing is inferred.", blocking=True,
                acknowledgement_required=False,
                discriminator=criterion.id))
            continue

        part = claim["part"]
        if part is not None and not _PART_PATTERN.fullmatch(part):
            findings.append(_finding(
                "C5-PART-ID-INVALID", "invalid_part_id", VERIFIED, "high",
                question.id, [criterion.id], {"part": part},
                f"Criterion '{criterion.id}' declares part '{part}', which "
                "is outside the conservative (a)/(b)/(i)/(ii) notation.",
                blocking=True, acknowledgement_required=False,
                discriminator=part))
        elif part is not None and not _CANONICAL_PART_PATTERN.fullmatch(part):
            findings.append(_finding(
                "C5-PART-ID-NONCANONICAL", "noncanonical_part_id", SUSPECTED,
                "low", question.id, [criterion.id], {"part": part},
                f"Criterion '{criterion.id}' declares part '{part}' in a "
                "noncanonical case form. The part is preserved verbatim and "
                "requires confirmation.", blocking=False,
                acknowledgement_required=True, discriminator=part))

        derived_type, _ = _derived_mark_type(criterion.code)
        claimed_type = claim["mark_type"]
        if claimed_type is None:
            findings.append(_finding(
                "C5-MARK-TYPE-UNDECLARED", "mark_type_undeclared",
                INSUFFICIENT, "low", question.id, [criterion.id],
                {"code": criterion.code, "derived_from_code": derived_type},
                f"Criterion '{criterion.id}' has no declared mark type. The "
                "type is not inferred from the code and is left for teacher "
                "confirmation.", blocking=False,
                acknowledgement_required=True))
        else:
            if claimed_type == "UNKNOWN" and derived_type != "UNKNOWN":
                findings.append(_finding(
                    "C5-MARK-TYPE-UNCERTAIN", "uncertain_mark_type",
                    SUSPECTED, "low", question.id, [criterion.id],
                    {"declared": claimed_type, "derived_from_code":
                     derived_type, "code": criterion.code},
                    f"Criterion '{criterion.id}' declares an unknown mark "
                    "type although its code implies {derived_type}; the "
                    "meaning is not certified.", blocking=False,
                    acknowledgement_required=True,
                    discriminator=claimed_type))
            elif derived_type != "UNKNOWN" and claimed_type != derived_type:
                findings.append(_finding(
                    "C5-MARK-TYPE-CONFLICT", "mark_type_conflict", VERIFIED,
                    "high", question.id, [criterion.id],
                    {"declared": claimed_type, "derived_from_code":
                     derived_type, "code": criterion.code},
                    f"Criterion '{criterion.id}' declares mark type "
                    f"{claimed_type} but code {criterion.code} is a "
                    f"{derived_type} mark; the two explicit declarations "
                    "conflict.", blocking=True,
                    acknowledgement_required=False,
                    discriminator=f"{claimed_type}|{criterion.code}"))

        if claim["marks"] is not None and criterion.weight is not None \
                and claim["marks"] != criterion.weight:
            findings.append(_finding(
                "C5-MARK-ALLOCATION-CONFLICT", "mark_allocation_conflict",
                VERIFIED, "high", question.id, [criterion.id],
                {"claim_marks": claim["marks"],
                 "criterion_weight": criterion.weight},
                f"Criterion '{criterion.id}' claims {claim['marks']} mark(s) "
                f"but its structural weight is {criterion.weight}.",
                blocking=True, acknowledgement_required=False))

        if claim["follow_through"] is not None \
                and criterion.follow_through is not None \
                and claim["follow_through"] != criterion.follow_through:
            findings.append(_finding(
                "C5-FOLLOW-THROUGH-CONFLICT", "follow_through_conflict",
                VERIFIED, "high", question.id, [criterion.id],
                {"claim_follow_through": claim["follow_through"],
                 "criterion_follow_through": criterion.follow_through},
                f"Criterion '{criterion.id}' has contradictory explicit "
                "follow-through declarations.", blocking=True,
                acknowledgement_required=False))

        if claim["alternative_option"] is not None \
                and criterion.alternative_group is None:
            findings.append(_finding(
                "C5-ALTERNATIVE-OPTION-ORPHAN", "orphan_alternative_option",
                SUSPECTED, "medium", question.id, [criterion.id],
                {"alternative_option": claim["alternative_option"]},
                f"Criterion '{criterion.id}' declares an alternative option "
                "but belongs to no alternative group.", blocking=False,
                acknowledgement_required=True,
                discriminator=claim["alternative_option"]))

        confidence = claim["extraction_confidence"]
        if confidence == "unknown":
            findings.append(_finding(
                "C5-EXTRACTION-CONFIDENCE-UNKNOWN",
                "extraction_confidence_unknown", INSUFFICIENT, "medium",
                question.id, [criterion.id], {"confidence": confidence},
                f"Criterion '{criterion.id}' has no extraction confidence; "
                "the interpretation cannot be silently certified.",
                blocking=False, acknowledgement_required=True))
        elif confidence == "low":
            findings.append(_finding(
                "C5-LOW-EXTRACTION-CONFIDENCE",
                "low_extraction_confidence", SUSPECTED, "medium",
                question.id, [criterion.id], {"confidence": confidence},
                f"Criterion '{criterion.id}' was extracted with low "
                "confidence and requires source confirmation.", blocking=False,
                acknowledgement_required=True))

    return findings


def _question_source_pages(mark_document):
    """A compact page-text index used only for evidence excerpts."""
    if mark_document is None:
        return {}
    return {page["page"]: page for page in mark_document["pages"]}


def _check_source_evidence(document, claims, sources, mapping):
    findings = []
    references = {}
    mark_document = sources.get("mark_scheme")
    question_pages = _question_source_pages(mark_document)
    mapping_by_question = {}
    if mapping is not None:
        mapping_by_question = {
            entry["question_id"]: entry for entry in mapping["matches"]}

    if mark_document is None:
        findings.append(_finding(
            "C5-SOURCE-DOCUMENT-MISSING", "source_document_missing",
            INSUFFICIENT, "high", None, [], {},
            "No C3 mark-scheme document record was supplied. Page references "
            "and supporting evidence cannot be verified, so no criterion can "
            "be certified.", blocking=True,
            acknowledgement_required=False))
        return findings, references, question_pages, mapping_by_question

    for question, criterion in document.criteria():
        claim = claims.get(criterion.id)
        if claim is None:
            continue
        source = claim["source"]
        if source is None:
            findings.append(_finding(
                "C5-SOURCE-PAGE-MISSING", "source_page_missing",
                INSUFFICIENT, "high", question.id, [criterion.id],
                {"criterion_id": criterion.id},
                f"Criterion '{criterion.id}' has no source page reference. "
                "The claim is preserved but cannot be certified.",
                blocking=True, acknowledgement_required=False,
                discriminator=criterion.id))
            continue
        if source["document_role"] != "mark_scheme":
            findings.append(_finding(
                "C5-SOURCE-ROLE-INVALID", "source_role_invalid", VERIFIED,
                "high", question.id, [criterion.id],
                {"document_role": source["document_role"]},
                f"Criterion '{criterion.id}' cites a "
                f"{source['document_role']} reference as mark-scheme "
                "evidence; only the official mark-scheme document can "
                "support a criterion claim.", blocking=True,
                acknowledgement_required=False,
                discriminator=source["document_role"]))
            continue
        page = source["page"]
        if page > mark_document["page_count"]:
            findings.append(_finding(
                "C5-SOURCE-PAGE-OUT-OF-RANGE", "source_page_out_of_range",
                VERIFIED, "high", question.id, [criterion.id],
                {"page": page, "page_count": mark_document["page_count"]},
                f"Criterion '{criterion.id}' cites page {page}, outside the "
                f"supplied document's {mark_document['page_count']} page(s).",
                blocking=True, acknowledgement_required=False,
                discriminator=str(page)))
            continue
        page_record = question_pages[page]
        if page_record["status"] != "extracted" \
                or not page_record["text"].strip():
            findings.append(_finding(
                "C5-SOURCE-EVIDENCE-UNAVAILABLE",
                "source_evidence_unavailable", INSUFFICIENT, "high",
                question.id, [criterion.id],
                {"page": page, "page_status": page_record["status"]},
                f"Criterion '{criterion.id}' cites page {page}, but that page "
                "has no usable text layer. Absence of extracted text is not "
                "treated as corruption or as proof about the source content.",
                blocking=True, acknowledgement_required=False,
                discriminator=str(page)))
            continue
        excerpt = source["excerpt"]
        if excerpt is None:
            findings.append(_finding(
                "C5-SOURCE-EVIDENCE-MISSING", "source_evidence_missing",
                INSUFFICIENT, "high", question.id, [criterion.id],
                {"page": page},
                f"Criterion '{criterion.id}' cites page {page} without a "
                "verbatim supporting excerpt. A page reference alone is not "
                "supporting evidence.", blocking=True,
                acknowledgement_required=False))
            continue
        try:
            reference = evidence_reference(
                mark_document, page, excerpt=excerpt,
                question_id=criterion.question_id,
                method="criterion_source_claim",
                data_origin="caller_supplied")
        except ValueError:
            findings.append(_finding(
                "C5-SOURCE-EVIDENCE-MISMATCH", "source_evidence_mismatch",
                VERIFIED, "high", question.id, [criterion.id],
                {"page": page, "excerpt": excerpt},
                f"The excerpt claimed for criterion '{criterion.id}' is not "
                "an exact substring of the supplied page text. The record "
                "contradicts its cited evidence; this is not a claim that the "
                "official document is wrong.", blocking=True,
                acknowledgement_required=False))
            continue
        references[criterion.id] = reference

        entry = mapping_by_question.get(question.id)
        if entry is None:
            findings.append(_finding(
                "C5-QUESTION-MAPPING-MISSING", "question_mapping_missing",
                INSUFFICIENT, "high", question.id, [criterion.id],
                {"question_id": question.id},
                f"Question '{question.id}' could not be mapped to source "
                "pages, so its evidence cannot be located reliably.",
                blocking=True, acknowledgement_required=False))
            continue
        if entry["certainty"] == "ambiguous":
            findings.append(_finding(
                "C5-QUESTION-MAPPING-AMBIGUOUS",
                "question_mapping_ambiguous", HUMAN_REVIEW, "high",
                question.id, [criterion.id],
                {"question_id": question.id,
                 "pages": entry["pages"]},
                f"Question '{question.id}' has headings on multiple pages; "
                "the criterion's page cannot be attributed unambiguously.",
                blocking=True, acknowledgement_required=False))
            continue
        if entry["certainty"] == "unresolved":
            findings.append(_finding(
                "C5-QUESTION-MAPPING-UNRESOLVED",
                "question_mapping_unresolved", INSUFFICIENT, "high",
                question.id, [criterion.id],
                {"question_id": question.id},
                f"Question '{question.id}' has no resolved page mapping; "
                "text absence never proves the source content is missing.",
                blocking=True, acknowledgement_required=False))
            continue
        if page not in entry["pages"]:
            findings.append(_finding(
                "C5-EVIDENCE-OUTSIDE-QUESTION-MAPPING",
                "evidence_outside_question_mapping", SUSPECTED, "medium",
                question.id, [criterion.id],
                {"page": page, "mapped_pages": entry["pages"]},
                f"Criterion '{criterion.id}' cites page {page}, which is not "
                "among the pages mapped to question "
                f"'{question.id}'. The page is preserved for review.",
                blocking=False, acknowledgement_required=True,
                discriminator=str(page)))
        else:
            page_methods = {
                item["method"] for item in entry["references"]
                if item["page"] == page}
            if page_methods and METHOD_CONTINUATION in page_methods:
                findings.append(_finding(
                    "C5-MULTIPAGE-MAPPING-HEURISTIC",
                    "multipage_mapping_heuristic", SUSPECTED, "low",
                    question.id, [criterion.id], {"page": page},
                    f"Criterion '{criterion.id}' is supported by a "
                    "continuation page mapped heuristically, not by a "
                    "question heading. Confirm the page belongs to this "
                    "question.", blocking=False,
                    acknowledgement_required=True,
                    discriminator=str(page)))
    return findings, references, question_pages, mapping_by_question


def _alternative_structure(question, claims):
    """Group-aware totals: alternatives contribute their maximum branch."""
    groups = {}
    for criterion in question.criteria:
        if criterion.alternative_group is not None:
            groups.setdefault(criterion.alternative_group, []).append(criterion)
    unresolved = []
    details = []
    group_total = 0
    for group_name in sorted(groups):
        members = groups[group_name]
        options = {}
        for criterion in members:
            claim = claims.get(criterion.id)
            option = claim["alternative_option"] if claim else None
            if not option:
                unresolved.append(group_name)
                break
            options.setdefault(option, []).append(criterion)
        if group_name in unresolved:
            continue
        if len(options) < 2:
            unresolved.append(group_name)
            continue
        option_totals = {}
        for option, option_members in options.items():
            if any(criterion.weight is None or criterion.weight <= 0
                   for criterion in option_members):
                unresolved.append(group_name)
                break
            option_totals[option] = sum(
                criterion.weight for criterion in option_members)
        if group_name in unresolved:
            continue
        group_total += max(option_totals.values())
        details.append({
            "alternative_group": group_name,
            "options": option_totals,
            "contribution": max(option_totals.values()),
        })
    standalone = [criterion for criterion in question.criteria
                  if criterion.alternative_group is None]
    if any(criterion.weight is None or criterion.weight <= 0
           for criterion in standalone):
        return None, details, unresolved, None
    standalone_total = sum(criterion.weight for criterion in standalone)
    raw_total = sum(criterion.weight for criterion in question.criteria
                    if criterion.weight is not None)
    if unresolved:
        # A partially reconciled sum would look authoritative while whole
        # branches are still unknown. Report no effective total instead.
        return None, details, unresolved, raw_total
    return standalone_total + group_total, details, unresolved, raw_total


def _check_totals(document, claims):
    findings = []
    reconciliation = []
    effective_totals = {}
    for question in document.questions:
        effective, details, unresolved, raw_total = _alternative_structure(
            question, claims)
        entry = {
            "question_id": question.id,
            "declared_total": question.total,
            "raw_criterion_sum": raw_total,
            "effective_total": effective,
            "alternative_groups": details,
            "alternative_structure_resolved": not unresolved,
        }
        effective_totals[question.id] = entry
        if unresolved:
            findings.append(_finding(
                "C5-ALTERNATIVE-STRUCTURE-UNKNOWN",
                "alternative_structure_unknown", INSUFFICIENT, "high",
                question.id, _criterion_ids(question),
                {"unresolved_groups": sorted(set(unresolved))},
                f"Question '{question.id}' declares alternative groups "
                "whose options cannot be resolved. The branches are not "
                "summed and the total is not certified.", blocking=True,
                acknowledgement_required=False))
            continue
        if effective is None or question.total is None:
            continue
        if effective < question.total:
            findings.append(_finding(
                "C5-CRITERION-MISSING", "missing_criterion", SUSPECTED,
                "high", question.id, [],
                {"declared_total": question.total,
                 "effective_total": effective,
                 "shortfall": question.total - effective},
                f"Question '{question.id}' declares {question.total} mark(s), "
                f"but its reconciled criteria account for {effective}. "
                "Criteria or marks may be missing; nothing is invented to "
                "fill the gap.", blocking=True,
                acknowledgement_required=False))
        elif effective > question.total:
            findings.append(_finding(
                "C5-TOTAL-OVERCOUNT", "total_overcount", VERIFIED, "high",
                question.id, [],
                {"declared_total": question.total,
                 "effective_total": effective,
                 "excess": effective - question.total},
                f"Question '{question.id}' declares {question.total} mark(s), "
                f"but its reconciled criteria account for {effective}. "
                "Alternative branches are counted once, so this is not an "
                "alternative-method sum.", blocking=True,
                acknowledgement_required=False))
        elif raw_total is not None and raw_total != effective:
            reconciliation.append({
                "question_id": question.id,
                "c2_rule_id": "SCHEME-TOTAL-MISMATCH",
                "reason": "alternative_methods_counted_once",
                "raw_criterion_sum": raw_total,
                "effective_total": effective,
                "declared_total": question.total,
            })
        option_totals = [
            detail["options"] for detail in details]
        for options in option_totals:
            if len(set(options.values())) > 1:
                findings.append(_finding(
                    "C5-ALTERNATIVE-MAX-MISMATCH",
                    "alternative_max_mismatch", SUSPECTED, "medium",
                    question.id,
                    [criterion.id for criterion in question.criteria
                     if criterion.alternative_group is not None],
                    {"option_totals": options},
                    f"Alternative branches in question '{question.id}' offer "
                    "different maximum totals. This may be legitimate, but it "
                    "requires examiner confirmation.", blocking=False,
                    acknowledgement_required=True,
                    discriminator=json.dumps(options, sort_keys=True)))
                break
    return findings, reconciliation, effective_totals


def _check_dependencies_and_follow_through(document, claims):
    findings = []
    location = {}
    for question_position, question in enumerate(document.questions):
        for criterion_position, criterion in enumerate(question.criteria):
            location[criterion.id] = (
                question, criterion, question_position, criterion_position)
    for question, criterion in document.criteria():
        source_claim = claims.get(criterion.id) or {}
        for dependency in criterion.dependencies:
            target = location.get(dependency)
            if target is None:
                continue                    # C2 reports undefined references
            target_question, target_criterion, target_q, target_c = target
            if target_question.id != question.id:
                findings.append(_finding(
                    "C5-DEPENDENCY-CROSS-QUESTION",
                    "cross_question_dependency", SUSPECTED, "medium",
                    question.id, [criterion.id, dependency],
                    {"dependency_id": dependency,
                     "target_question_id": target_question.id},
                    f"Criterion '{criterion.id}' depends on '{dependency}' in "
                    "a different question. Cross-question dependencies are "
                    "unusual and require confirmation.", blocking=False,
                    acknowledgement_required=True,
                    discriminator=dependency))
            elif target_c >= criterion.document_index:
                findings.append(_finding(
                    "C5-DEPENDENCY-FORWARD", "forward_dependency", SUSPECTED,
                    "medium", question.id, [criterion.id, dependency],
                    {"dependency_id": dependency},
                    f"Criterion '{criterion.id}' depends on '{dependency}', "
                    "which appears at or after it in the proposed order. A "
                    "forward dependency may be a misread ordering.",
                    blocking=False, acknowledgement_required=True,
                    discriminator=dependency))
            if criterion.alternative_group is not None \
                    and target_criterion.alternative_group is not None:
                source_option = source_claim.get("alternative_option")
                target_claim = claims.get(dependency) or {}
                target_option = target_claim.get("alternative_option")
                if criterion.alternative_group == \
                        target_criterion.alternative_group \
                        and source_option and target_option \
                        and source_option != target_option:
                    findings.append(_finding(
                        "C5-ALTERNATIVE-DEPENDENCY-CONFLICT",
                        "alternative_dependency_conflict", SUSPECTED,
                        "medium", question.id,
                        [criterion.id, dependency],
                        {"dependency_id": dependency,
                         "source_option": source_option,
                         "target_option": target_option},
                        f"Criterion '{criterion.id}' depends on a criterion "
                        "in a different alternative branch; the branches "
                        "should be independent.", blocking=False,
                        acknowledgement_required=True,
                        discriminator=dependency))

        _, dependent_method = _derived_mark_type(criterion.code)
        if dependent_method and not criterion.dependencies:
            findings.append(_finding(
                "C5-DEPENDENT-METHOD-NO-DEPENDENCY",
                "dependent_method_without_dependency", SUSPECTED, "medium",
                question.id, [criterion.id], {"code": criterion.code},
                f"Dependent-method criterion '{criterion.id}' declares no "
                "dependency; its prerequisite is not explicit.", blocking=False,
                acknowledgement_required=True))

        follow_through = (
            criterion.follow_through is True
            or code_has_follow_through_suffix(criterion.code or ""))
        if follow_through:
            earlier = [
                other for other in question.criteria
                if other.document_index < criterion.document_index]
            if not earlier:
                findings.append(_finding(
                    "C5-FOLLOW-THROUGH-UNSUPPORTED",
                    "follow_through_unsupported", SUSPECTED, "medium",
                    question.id, [criterion.id], {"code": criterion.code},
                    f"Follow-through criterion '{criterion.id}' has no "
                    "earlier criterion in the question to follow through "
                    "from.", blocking=False,
                    acknowledgement_required=True))
            elif not criterion.dependencies:
                findings.append(_finding(
                    "C5-FOLLOW-THROUGH-SOURCE-UNSPECIFIED",
                    "follow_through_source_unspecified", SUSPECTED, "low",
                    question.id, [criterion.id], {"code": criterion.code},
                    f"Follow-through criterion '{criterion.id}' does not name "
                    "the earlier criterion it follows. The official scheme "
                    "may permit a conditional follow-through that a flat "
                    "criterion list cannot express.", blocking=False,
                    acknowledgement_required=True))
            else:
                earlier_ids = {other.id for other in earlier}
                if not any(dependency in earlier_ids
                           for dependency in criterion.dependencies):
                    findings.append(_finding(
                        "C5-FOLLOW-THROUGH-UNSUPPORTED",
                        "follow_through_unsupported", SUSPECTED, "medium",
                        question.id, [criterion.id],
                        {"dependencies": list(criterion.dependencies)},
                        f"Follow-through criterion '{criterion.id}' depends "
                        "only on later criteria; no earlier source is "
                        "explicit.", blocking=False,
                        acknowledgement_required=True))
    return findings


def _summarize_mapping(mapping):
    if mapping is None:
        return None
    return {
        "document_id": mapping["document_id"],
        "document_role": mapping["document_role"],
        "sha256": mapping["sha256"],
        "page_count": mapping["page_count"],
        "continuation_policy": mapping["continuation_policy"],
        "matches": [
            {
                "question_id": entry["question_id"],
                "certainty": entry["certainty"],
                "pages": list(entry["pages"]),
                "methods": sorted({
                    reference["method"] for reference in entry["references"]}),
            }
            for entry in mapping["matches"]
        ],
        "unresolved": copy.deepcopy(mapping["unresolved"]),
        "unmatched_headings": copy.deepcopy(mapping["unmatched_headings"]),
        "warnings": list(mapping["warnings"]),
    }


def _build_mapping(document, sources, supplied_mapping):
    mark_document = sources.get("mark_scheme")
    if mark_document is None:
        return None, []
    findings = []
    if supplied_mapping is not None:
        if (supplied_mapping["document_id"] != mark_document["document_id"]
                or supplied_mapping["sha256"] != mark_document["sha256"]):
            findings.append(_finding(
                "C5-MAPPING-DOCUMENT-MISMATCH",
                "mapping_document_mismatch", VERIFIED, "high", None, [],
                {"mapping_document_id": supplied_mapping["document_id"],
                 "source_document_id": mark_document["document_id"]},
                "The supplied question mapping belongs to a different "
                "document record than the supplied mark scheme.",
                blocking=True, acknowledgement_required=False))
            return None, findings
        return supplied_mapping, findings
    question_ids = [question.id for question in document.questions]
    if not question_ids or len(set(question_ids)) != len(question_ids):
        # Duplicate identifiers are a C2 finding; mapping cannot be built
        # without silently choosing one occurrence.
        return None, findings
    mapping = map_question_pages(
        mark_document, question_ids,
        continuation_policy=CONTINUATION_POLICY_HEURISTIC_ADJACENT)
    return mapping, findings


# ---------------------------------------------------------------------------
# Provider boundary (mockable, never live in this module)


def build_provider_packet(request, document, claims, references, findings,
                          mapping):
    """Build a bounded, source-only packet for an optional provider review.

    The packet never contains teacher decisions, reviewer identity, student
    data or any proposed award. It contains the proposed criteria, their
    cited excerpts, a bounded sample of the cited source pages and the
    deterministic findings.
    """
    mark_document = request["source_documents"].get("mark_scheme")
    pages = {}
    if mark_document is not None:
        cited = sorted({reference["page"] for reference in references.values()})
        for page_number in cited:
            page = mark_document["pages"][page_number - 1]
            pages[page_number] = page["text"][:MAX_PACKET_PAGE_CHARS]
    question_packets = []
    for question in document.questions:
        criteria = []
        for criterion in question.criteria:
            claim = claims.get(criterion.id) or {}
            reference = references.get(criterion.id)
            criteria.append({
                "criterion_id": criterion.id,
                "code": criterion.code,
                "weight": criterion.weight,
                "part": claim.get("part"),
                "mark_type": claim.get("mark_type"),
                "dependencies": list(criterion.dependencies),
                "follow_through": criterion.follow_through,
                "alternative_group": criterion.alternative_group,
                "alternative_option": claim.get("alternative_option"),
                "extraction_confidence": claim.get("extraction_confidence"),
                "source_page": None if reference is None else reference["page"],
                "source_excerpt": (None if reference is None
                                   else reference["excerpt"]),
            })
        question_packets.append({
            "question_id": question.id,
            "total": question.total,
            "criteria": criteria,
        })
    packet = {
        "schema": PROVIDER_PACKET_SCHEMA,
        "specification": request["specification"],
        "assignment": {
            "assignment_id": request["assignment"]["assignment_id"],
            "paper_id": request["assignment"]["paper_id"],
            "session": request["assignment"]["session"],
            "revision": request["assignment"]["revision"],
        },
        "questions": question_packets,
        "source_pages": pages,
        "question_mapping": _summarize_mapping(mapping),
        "deterministic_findings": [
            {
                "finding_id": finding["finding_id"],
                "rule_id": finding["rule_id"],
                "state": finding["state"],
                "question_id": finding["question_id"],
                "criterion_ids": list(finding["criterion_ids"]),
                "explanation": finding["explanation"][:MAX_EXPLANATION_CHARS],
            }
            for finding in findings
        ],
        "instructions": PROVIDER_PROMPT,
    }
    encoded = json.dumps(packet, sort_keys=True, ensure_ascii=False)
    if len(encoded) > MAX_PACKET_CHARS:
        raise SchemeReviewError(
            "The provider review packet exceeds the serialized size limit")
    return packet


def _provider_error_category(exc):
    """Classify a provider exception without reading its message text."""
    if isinstance(exc, ProviderReviewError):
        return exc.category
    if isinstance(exc, TimeoutError):
        return "timeout"
    type_name = type(exc).__name__.lower()
    if "timeout" in type_name:
        return "timeout"
    if "block" in type_name or "safety" in type_name:
        return "blocked"
    if "resourceexhausted" in type_name or "quota" in type_name \
            or "ratelimit" in type_name:
        return "quota_limited"
    for attribute in ("status_code", "code"):
        value = getattr(exc, attribute, None)
        if type(value) is int and value == 429:
            return "quota_limited"
    return "unavailable"


def _validate_provider_source(raw, request, question_id, criterion_ids):
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - _SOURCE_KEYS:
        raise ValueError("invalid source object")
    role = raw.get("document_role")
    if role not in SOURCE_ROLES:
        raise ValueError("invalid source role")
    page = raw.get("page")
    if type(page) is not int or page < 1:
        raise ValueError("invalid source page")
    document = request["source_documents"].get(role)
    if document is None:
        raise ValueError("source document unavailable")
    if page > document["page_count"]:
        raise ValueError("source page outside document")
    excerpt = raw.get("excerpt")
    if excerpt is not None:
        if not isinstance(excerpt, str) or not excerpt \
                or len(excerpt) > MAX_EXCERPT_CHARS:
            raise ValueError("invalid source excerpt")
        return evidence_reference(
            document, page, excerpt=excerpt, question_id=question_id,
            method="provider_suggestion", data_origin="heuristic_inference")
    return evidence_reference(
        document, page, question_id=question_id, method="provider_suggestion",
        data_origin="heuristic_inference")


def _validate_provider_payload(payload, request, document):
    """Return (accepted_findings, rejections, status). Never trusts output."""
    if not isinstance(payload, dict):
        return [], [{"index": None, "reason": "payload_not_object"}], \
            "schema_invalid"
    if set(payload) - {"findings"}:
        return [], [{"index": None, "reason": "unexpected_top_level_fields"}], \
            "schema_invalid"
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        return [], [{"index": None, "reason": "findings_not_list"}], \
            "schema_invalid"
    if len(raw_findings) > MAX_PROVIDER_FINDINGS:
        return [], [{"index": None, "reason": "too_many_findings"}], \
            "schema_invalid"
    known_questions = {question.id for question in document.questions}
    known_criteria = {criterion.id for _, criterion in document.criteria()}
    accepted = []
    rejected = []
    for index, raw in enumerate(raw_findings):
        try:
            if not isinstance(raw, dict):
                raise ValueError("finding_not_object")
            allowed = {
                "category", "question_id", "criterion_ids", "source",
                "explanation", "confidence", "suggested_action",
            }
            if set(raw) - allowed:
                raise ValueError("unexpected_finding_fields")
            required = {
                "category", "question_id", "criterion_ids", "explanation",
                "confidence", "suggested_action",
            }
            if required - set(raw):
                raise ValueError("missing_finding_fields")
            category = raw["category"]
            if category not in PROVIDER_CATEGORIES:
                raise ValueError("unsupported_category")
            question_id = raw["question_id"]
            if question_id is not None and (
                    not isinstance(question_id, str)
                    or question_id not in known_questions):
                raise ValueError("hallucinated_question")
            criterion_ids = raw["criterion_ids"]
            if not isinstance(criterion_ids, list) or len(criterion_ids) > 50:
                raise ValueError("hallucinated_criterion")
            if any(not isinstance(item, str) or item not in known_criteria
                   for item in criterion_ids):
                raise ValueError("hallucinated_criterion")
            if len(set(criterion_ids)) != len(criterion_ids):
                raise ValueError("duplicate_criterion_reference")
            if category == "CRITERION_MISSING" and criterion_ids:
                raise ValueError("missing_criterion_cannot_target_existing")
            if category == "CRITERION_UNSUPPORTED" and not criterion_ids:
                raise ValueError("unsupported_criterion_needs_target")
            if question_id is None and category not in {
                    "TOTAL_MISMATCH", "SOURCE_EVIDENCE_MISSING"}:
                raise ValueError("question_required_for_category")
            if question_id is not None and criterion_ids:
                question_of = {}
                for question, criterion in document.criteria():
                    question_of[criterion.id] = question.id
                if any(question_of.get(cid) != question_id
                       for cid in criterion_ids):
                    raise ValueError("criterion_question_mismatch")
            explanation = raw["explanation"]
            if not isinstance(explanation, str) or not explanation.strip() \
                    or len(explanation) > MAX_EXPLANATION_CHARS:
                raise ValueError("invalid_explanation")
            confidence = raw["confidence"]
            if confidence not in ("high", "medium", "low"):
                raise ValueError("invalid_confidence")
            action = raw["suggested_action"]
            if not isinstance(action, str) or not action.strip() \
                    or len(action) > MAX_ACTION_CHARS:
                raise ValueError("invalid_action")
            reference = _validate_provider_source(
                raw.get("source"), request, question_id, criterion_ids)
        except (ValueError, KeyError, TypeError):
            rejected.append({"index": index, "reason": "finding_rejected"})
            continue
        accepted.append({
            "rule_id": "C5-PROVIDER-" + category,
            "issue_category": category,
            "state": SUSPECTED,
            "severity": "medium" if confidence == "high" else "low",
            "question_id": question_id,
            "criterion_ids": sorted(criterion_ids),
            "evidence": {
                "provider_confidence": confidence,
                "source_reference": reference,
            },
            "explanation": explanation,
            "suggested_action": action,
            "review_required": True,
            "origin": "gemini_proposal",
            "blocks_approval": False,
            "acknowledgement_required": True,
        })
    # Assign IDs from a canonical ordering so the same proposal set always
    # produces the same findings, whatever order the provider returned them in.
    def canonical_key(item):
        reference = item["evidence"]["source_reference"] or {}
        return (
            item["issue_category"], item["question_id"] or "",
            tuple(item["criterion_ids"]), item["explanation"],
            item["evidence"]["provider_confidence"],
            item["suggested_action"],
            reference.get("document_role") or "",
            reference.get("page") or 0,
            reference.get("excerpt") or "",
        )

    accepted.sort(key=canonical_key)
    for index, item in enumerate(accepted):
        item["finding_id"] = (
            "C5-PROVIDER::" + item["issue_category"] + "::"
            + (item["question_id"] or "-") + "::"
            + ",".join(sorted(item["criterion_ids"])) + "::"
            + str(index))
    accepted.sort(key=lambda item: item["finding_id"])
    if rejected and accepted:
        status = "completed_with_rejections"
    elif rejected:
        status = "schema_invalid"
    else:
        status = "completed"
    return accepted, rejected, status


def _run_provider(request, document, packet, provider):
    """Run exactly one optional provider request; never live in this module."""
    empty = {
        "status": "not_requested",
        "request_count": 0,
        "findings": [],
        "rejected_findings": [],
        "error_category": None,
        "max_requests": MAX_PROVIDER_REQUESTS,
    }
    if provider is None:
        return empty
    if request["reviewer_id"] is None:
        return {**empty, "status": "anonymous_refused"}
    if request["source_documents"].get("mark_scheme") is None:
        return {**empty, "status": "unavailable_evidence"}
    if not callable(provider):
        return {**empty, "status": "unavailable"}
    try:
        payload = provider(copy.deepcopy(packet))
    except Exception as exc:                 # never read str(exc)
        category = _provider_error_category(exc)
        return {**empty, "status": category, "request_count": 1,
                "error_category": category}
    if payload is None:
        return {**empty, "status": "empty_response", "request_count": 1}
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return {**empty, "status": "invalid_json", "request_count": 1,
                    "error_category": "invalid_json"}
    if isinstance(payload, str):
        if not payload.strip():
            return {**empty, "status": "empty_response", "request_count": 1,
                    "error_category": "empty_response"}
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return {**empty, "status": "invalid_json", "request_count": 1,
                    "error_category": "invalid_json"}
    accepted, rejected, status = _validate_provider_payload(
        payload, request, document)
    return {
        "status": status,
        "request_count": 1,
        "findings": accepted,
        "rejected_findings": rejected,
        "error_category": None if status.startswith("completed") else status,
        "max_requests": MAX_PROVIDER_REQUESTS,
    }


class ReviewCache:
    """In-memory, owner-scoped cache for optional provider findings.

    This is intentionally not a database and not global application state.
    A future Streamlit integration must place it inside the existing
    owner-bound session store and clear it in the same logout/account-switch
    path as ``SENSITIVE_STATE_KEYS``. The key includes reviewer, assignment,
    revision, scheme digest and packet digest, so neither another teacher nor
    a revised scheme can reuse a cached review.
    """

    def __init__(self):
        self._entries = {}

    @staticmethod
    def key(reviewer_id, assignment_id, revision, scheme_sha256, packet_digest):
        return (reviewer_id or "", assignment_id, revision,
                scheme_sha256 or "", packet_digest)

    def get(self, reviewer_id, assignment_id, revision, scheme_sha256,
            packet_digest):
        key = self.key(reviewer_id, assignment_id, revision, scheme_sha256,
                       packet_digest)
        value = self._entries.get(key)
        return None if value is None else copy.deepcopy(value)

    def put(self, reviewer_id, assignment_id, revision, scheme_sha256,
            packet_digest, provider_result):
        key = self.key(reviewer_id, assignment_id, revision, scheme_sha256,
                       packet_digest)
        self._entries[key] = copy.deepcopy(provider_result)
        return key

    def clear_owner(self, reviewer_id):
        for key in [key for key in self._entries if key[0] == reviewer_id]:
            self._entries.pop(key, None)

    def clear(self):
        self._entries.clear()

    def size(self, reviewer_id=None):
        if reviewer_id is None:
            return len(self._entries)
        return sum(1 for key in self._entries if key[0] == reviewer_id)


def _provider_packet_digest(packet):
    encoded = json.dumps(packet, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Teacher decisions and review state


def _decision_applicability(request, effective_sha):
    reviewer_id = request["reviewer_id"]
    assignment = request["assignment"]
    applicable = []
    ignored_reasons = set()
    for decision in request["teacher_decisions"]:
        reasons = set()
        if reviewer_id is None or decision["owner_id"] != reviewer_id:
            reasons.add("owner_mismatch")
        if decision["assignment_id"] != assignment["assignment_id"]:
            reasons.add("wrong_assignment")
        if decision["revision"] != assignment["revision"]:
            reasons.add("stale_revision")
        if effective_sha is not None:
            if decision["scheme_sha256"] != effective_sha:
                reasons.add("scheme_changed")
        elif decision["scheme_sha256"] is not None:
            reasons.add("scheme_changed")
        if reasons:
            ignored_reasons.update(reasons)
        else:
            applicable.append(decision)
    return applicable, ignored_reasons


def _criteria_by_question(document):
    mapping = {question.id: set() for question in document.questions}
    for question, criterion in document.criteria():
        mapping[question.id].add(criterion.id)
    return mapping


def _question_of_criterion(document):
    return {criterion.id: question.id
            for question, criterion in document.criteria()}


def _decision_findings(ignored_reasons, applicable, document):
    findings = []
    if ignored_reasons:
        findings.append(_finding(
            "C5-APPROVAL-NOT-APPLICABLE", "approval_not_applicable",
            VERIFIED, "high", None, [],
            {"reasons": sorted(ignored_reasons)},
            "One or more supplied teacher decisions were not applied because "
            "they are not bound to this reviewer, assignment, revision and "
            "scheme digest. Previous approval is never inherited.",
            blocking=True, acknowledgement_required=False))

    known_questions = {question.id for question in document.questions}
    known_criteria = {criterion.id for _, criterion in document.criteria()}
    for decision in applicable:
        kind = decision["target"]["kind"]
        target_id = decision["target"]["id"]
        if kind == "question" and target_id not in known_questions:
            findings.append(_finding(
                "C5-DECISION-TARGET-UNKNOWN", "decision_target_unknown",
                VERIFIED, "high", None, [], {"target_kind": kind},
                "A teacher decision targets a question that does not exist in "
                "the proposed scheme. The decision is not applied.",
                blocking=True, acknowledgement_required=False,
                discriminator="question"))
        elif kind == "criterion" and target_id not in known_criteria:
            findings.append(_finding(
                "C5-DECISION-TARGET-UNKNOWN", "decision_target_unknown",
                VERIFIED, "high", None, [], {"target_kind": kind},
                "A teacher decision targets a criterion that does not exist in "
                "the proposed scheme. The decision is not applied.",
                blocking=True, acknowledgement_required=False,
                discriminator="criterion"))

    scheme_decisions = [
        decision for decision in applicable
        if decision["target"]["kind"] == "scheme"]
    question_decisions = [
        decision for decision in applicable
        if decision["target"]["kind"] == "question"]
    criterion_decisions = [
        decision for decision in applicable
        if decision["target"]["kind"] == "criterion"]

    if len(scheme_decisions) > 1:
        findings.append(_finding(
            "C5-DECISION-AMBIGUOUS", "ambiguous_teacher_decision", VERIFIED,
            "high", None, [], {"scheme_decisions": len(scheme_decisions)},
            "More than one applicable scheme-level decision was supplied; "
            "the authoritative decision cannot be identified.",
            blocking=True, acknowledgement_required=False))

    def _conflicting(decisions):
        seen_approve = set()
        seen_reject = set()
        for decision in decisions:
            target = decision["target"]["id"]
            if decision["decision"] == "approve":
                seen_approve.add(target)
            elif decision["decision"] == "reject":
                seen_reject.add(target)
        return sorted(seen_approve & seen_reject)

    overlapping_questions = _conflicting(question_decisions)
    if overlapping_questions:
        findings.append(_finding(
            "C5-DECISION-AMBIGUOUS", "ambiguous_teacher_decision", VERIFIED,
            "high", None, [],
            {"overlapping_questions": overlapping_questions},
            "The same question has both an applicable approval and rejection; "
            "the teacher's decision is ambiguous.", blocking=True,
            acknowledgement_required=False, discriminator="question_conflict"))

    rejected_criteria = {
        decision["target"]["id"] for decision in criterion_decisions
        if decision["decision"] == "reject"}
    approved_criteria = {
        decision["target"]["id"] for decision in criterion_decisions
        if decision["decision"] == "approve"}
    overlapping_criteria = sorted(rejected_criteria & approved_criteria)
    if overlapping_criteria:
        findings.append(_finding(
            "C5-DECISION-AMBIGUOUS", "ambiguous_teacher_decision", VERIFIED,
            "high", None, overlapping_criteria,
            {"overlapping_criteria": overlapping_criteria},
            "The same criterion has both an applicable approval and "
            "rejection; the teacher's decision is ambiguous.",
            blocking=True, acknowledgement_required=False,
            discriminator="criterion_conflict"))

    criteria_by_question = _criteria_by_question(document)
    question_of_criterion = _question_of_criterion(document)
    rejected_questions = {
        decision["target"]["id"] for decision in question_decisions
        if decision["decision"] == "reject"}
    approved_questions = {
        decision["target"]["id"] for decision in question_decisions
        if decision["decision"] == "approve"}

    conflict = False
    if any(decision["decision"] == "approve"
           for decision in scheme_decisions) \
            and (rejected_criteria or rejected_questions):
        conflict = True
    for criterion_id in rejected_criteria:
        if question_of_criterion.get(criterion_id) in approved_questions:
            conflict = True
    for question_id in rejected_questions:
        if criteria_by_question.get(question_id, set()) & approved_criteria:
            conflict = True
    if conflict:
        affected = set(rejected_criteria)
        for question_id in rejected_questions:
            affected.update(criteria_by_question.get(question_id, set()))
        findings.append(_finding(
            "C5-APPROVAL-CONFLICT", "approval_conflict", VERIFIED, "high",
            None, sorted(affected), {"conflicting_rejections": sorted(affected)},
            "An applicable approval conflicts with an applicable rejection at "
            "another level. The teacher's intent is ambiguous.",
            blocking=True, acknowledgement_required=False))
    return findings, scheme_decisions, question_decisions, criterion_decisions


def _finding_affects(finding, criterion_id, question_id, criteria_of_question):
    """True when a review finding concerns the approval target."""
    ids = set(finding.get("criterion_ids") or [])
    if criterion_id is not None:
        if ids:
            return criterion_id in ids
        return finding.get("question_id") in (None, question_id)
    if ids:
        return bool(ids & criteria_of_question.get(question_id, set()))
    return finding.get("question_id") in (None, question_id)


def _check_approval_acknowledgements(applicable, ack_findings, hard_findings,
                                     known_ids, document):
    """Validate approvals against final finding ids and the hard-blocker set.

    A scheme-level approval is valid only when no hard blocker exists, every
    non-hard review finding is acknowledged, and no unknown id is cited.
    Criterion/question approvals are validated against the findings that
    actually concern their target.
    """
    findings = []
    unknown = set()
    for decision in applicable:
        unknown.update(
            set(decision["acknowledged_finding_ids"]) - known_ids)
    if unknown:
        findings.append(_finding(
            "C5-ACKNOWLEDGEMENT-UNKNOWN", "unknown_acknowledgement",
            VERIFIED, "high", None, [],
            {"unknown_count": len(unknown)},
            "A teacher decision acknowledges finding ids that do not exist in "
            "this review. Unknown acknowledgements are not trusted.",
            blocking=True, acknowledgement_required=False))

    criteria_by_question = _criteria_by_question(document)
    question_of_criterion = _question_of_criterion(document)
    valid_targets = set()

    def _missing(decision, criterion_id=None, question_id=None):
        acknowledged = set(decision["acknowledged_finding_ids"])
        return sorted(
            finding["finding_id"] for finding in ack_findings
            if finding["finding_id"] not in acknowledged
            and _finding_affects(
                finding, criterion_id, question_id, criteria_by_question))

    def _hard(criterion_id=None, question_id=None):
        return sorted(
            finding["finding_id"] for finding in hard_findings
            if _finding_affects(
                finding, criterion_id, question_id, criteria_by_question))

    criterion_approvals = [
        decision for decision in applicable
        if decision["target"]["kind"] == "criterion"
        and decision["decision"] == "approve"]
    for decision in criterion_approvals:
        criterion_id = decision["target"]["id"]
        question_id = question_of_criterion.get(criterion_id)
        if question_id is None:
            continue                        # unknown target already reported
        hard = _hard(criterion_id, question_id)
        if hard:
            findings.append(_finding(
                "C5-CRITERION-APPROVAL-BLOCKED",
                "criterion_approval_blocked", VERIFIED, "high", question_id,
                [criterion_id], {"hard_blocker_count": len(hard)},
                f"Criterion '{criterion_id}' has a hard blocker, so it cannot "
                "be approved. The blocker must be resolved, not acknowledged.",
                blocking=True, acknowledgement_required=False,
                discriminator=criterion_id))
        missing = _missing(decision, criterion_id, question_id)
        if missing:
            findings.append(_finding(
                "C5-APPROVAL-INCOMPLETE", "approval_incomplete", VERIFIED,
                "high", question_id, [criterion_id],
                {"missing_acknowledgements": missing},
                f"The approval of criterion '{criterion_id}' does not "
                "acknowledge every review finding that concerns it.",
                blocking=True, acknowledgement_required=False,
                discriminator=criterion_id))
        if not hard and not missing and not unknown:
            valid_targets.add(("criterion", criterion_id))

    question_approvals = [
        decision for decision in applicable
        if decision["target"]["kind"] == "question"
        and decision["decision"] == "approve"]
    for decision in question_approvals:
        question_id = decision["target"]["id"]
        if question_id not in criteria_by_question:
            continue
        hard = _hard(None, question_id)
        if hard:
            findings.append(_finding(
                "C5-APPROVAL-BLOCKED", "approval_blocked", VERIFIED, "high",
                question_id, [], {"hard_blocker_count": len(hard)},
                f"Question '{question_id}' has a hard blocker, so it cannot "
                "be approved. The blocker must be resolved, not acknowledged.",
                blocking=True, acknowledgement_required=False,
                discriminator="question:" + question_id))
        missing = _missing(decision, None, question_id)
        if missing:
            findings.append(_finding(
                "C5-APPROVAL-INCOMPLETE", "approval_incomplete", VERIFIED,
                "high", question_id, [],
                {"missing_acknowledgements": missing},
                f"The approval of question '{question_id}' does not "
                "acknowledge every review finding that concerns it.",
                blocking=True, acknowledgement_required=False,
                discriminator="question:" + question_id))
        if not hard and not missing and not unknown:
            valid_targets.add(("question", question_id))

    approved_scheme = [decision for decision in applicable
                       if decision["target"]["kind"] == "scheme"
                       and decision["decision"] == "approve"]
    if not approved_scheme:
        return findings, False, valid_targets

    decision = approved_scheme[0]
    acknowledged = set(decision["acknowledged_finding_ids"])
    if hard_findings:
        findings.append(_finding(
            "C5-APPROVAL-BLOCKED", "approval_blocked", VERIFIED, "high",
            None, [], {"hard_blocker_count": len(hard_findings)},
            "The scheme-level approval cannot be applied while hard blockers "
            "remain. A hard blocker must be resolved, never acknowledged "
            "away.", blocking=True, acknowledgement_required=False,
            discriminator="scheme"))
    missing = sorted(
        finding["finding_id"] for finding in ack_findings
        if finding["finding_id"] not in acknowledged)
    if missing:
        findings.append(_finding(
            "C5-APPROVAL-INCOMPLETE", "approval_incomplete", VERIFIED, "high",
            None, [], {"missing_acknowledgements": missing},
            "The approval does not acknowledge every non-hard review "
            "finding. An approval cannot silently bypass a warning.",
            blocking=True, acknowledgement_required=False))
    valid = (not hard_findings) and (not missing) and (not unknown)
    return findings, valid, valid_targets


def _affected_criteria(findings, document):
    """Criteria needing review, including question- and scheme-level findings."""
    affected = set()
    criteria_by_question = _criteria_by_question(document)
    all_criteria = {criterion.id for _, criterion in document.criteria()}
    for finding in findings:
        ids = set(finding.get("criterion_ids") or [])
        if ids:
            affected.update(ids)
            continue
        question_id = finding.get("question_id")
        if question_id is None:
            affected.update(all_criteria)
        else:
            affected.update(criteria_by_question.get(question_id, set()))
    return affected


def _collect_accounting(c2_findings, c2_accounting, c5_findings,
                        provider_findings):
    hard = []
    acknowledgements = []
    for finding in c2_findings:
        flags = c2_accounting[finding["finding_id"]]
        if flags["blocks_approval"]:
            hard.append(finding)
        elif flags["acknowledgement_required"]:
            acknowledgements.append(finding)
    for finding in c5_findings:
        if finding.get("blocks_approval"):
            hard.append(finding)
        elif finding.get("acknowledgement_required"):
            acknowledgements.append(finding)
    for finding in provider_findings:
        acknowledgements.append(finding)
    return hard, acknowledgements


def _compute_status(request, hard_blockers, required_acknowledgements,
                    applicable, approval_valid, provider_findings):
    if any(decision["decision"] == "reject"
           and decision["target"]["kind"] == "scheme"
           for decision in applicable):
        return "REJECTED"
    if any(decision["decision"] == "reject"
           and decision["target"]["kind"] in ("question", "criterion")
           for decision in applicable):
        return "NEEDS_TEACHER_REVIEW"
    if any(decision["decision"] == "needs_review"
           for decision in applicable):
        return "NEEDS_TEACHER_REVIEW"
    if request["reviewer_id"] is None:
        return "UNREVIEWED"
    if hard_blockers:
        return "NEEDS_TEACHER_REVIEW"
    if approval_valid:
        return "TEACHER_APPROVED"
    if required_acknowledgements or provider_findings:
        return "NEEDS_TEACHER_REVIEW"
    return "CHECKED"


# ---------------------------------------------------------------------------
# Public review entry point


def review_scheme(payload, provider=None, cache=None):
    """Review a proposed mark-scheme interpretation.

    ``provider`` is an optional callable accepting the bounded packet from
    :func:`build_provider_packet`. It is never called by default, at most once
    per invocation, and cannot change extracted or verified information.
    ``cache`` is an optional :class:`ReviewCache`; only provider findings are
    cached, never teacher decisions or approvals.
    """
    request = _normalize_request(payload)
    document = request["proposed_scheme"]
    claims = request["criterion_claims"]

    c2_findings = verify_scheme_document(document)
    c5_findings = []
    c5_findings.extend(_check_questions(document))
    c5_findings.extend(_check_claims(document, claims))

    mapping, mapping_findings = _build_mapping(
        document, request["source_documents"], request["question_mapping"])
    c5_findings.extend(mapping_findings)
    source_findings, references, _, _ = _check_source_evidence(
        document, claims, request["source_documents"], mapping)
    c5_findings.extend(source_findings)
    total_findings, reconciliation, effective_totals = _check_totals(
        document, claims)
    c5_findings.extend(total_findings)
    c5_findings.extend(_check_dependencies_and_follow_through(
        document, claims))

    # C2 findings are preserved exactly. Approval accounting may treat a raw
    # total mismatch as explained by a valid alternative-method structure.
    explained_total_mismatch = {
        entry["question_id"] for entry in reconciliation}
    c2_blocking = {}
    for finding in c2_findings:
        rule_id = finding["rule_id"]
        explained = (rule_id == "SCHEME-TOTAL-MISMATCH"
                     and finding["question_id"] in explained_total_mismatch)
        if explained:
            blocking = False
            ack = False
        elif finding["state"] == VERIFIED:
            blocking = True
            ack = False
        elif rule_id in {
                "SCHEME-TOTAL-ABSENT", "SCHEME-TOTAL-UNRECONCILABLE",
                "SCHEME-REF-AMBIGUOUS"}:
            blocking = True
            ack = False
        elif rule_id in {
                "SCHEME-MATH-UNSUPPORTED", "SCHEME-PROVENANCE-ABSENT"}:
            blocking = False
            ack = True
        elif finding["state"] == INSUFFICIENT:
            blocking = True
            ack = False
        else:
            blocking = False
            ack = True
        c2_blocking[finding["finding_id"]] = {
            "explained_by_alternatives": explained,
            "blocks_approval": blocking,
            "acknowledgement_required": ack,
        }

    c5_findings = _unique_findings(c5_findings)

    source_document = request["source_documents"].get("mark_scheme")
    source_sha = None if source_document is None else source_document["sha256"]
    assignment_sha = request["assignment"]["scheme_sha256"]
    if assignment_sha is not None and source_sha is not None \
            and assignment_sha != source_sha:
        c5_findings.append(_finding(
            "C5-SCHEME-REVISION-MISMATCH", "scheme_revision_mismatch",
            VERIFIED, "high", None, [],
            {"assignment_scheme_sha256": assignment_sha,
             "source_scheme_sha256": source_sha},
            "The assignment's declared scheme digest does not match the "
            "supplied mark-scheme document. Any previous approval is stale.",
            blocking=True, acknowledgement_required=False))
        c5_findings = _unique_findings(c5_findings)

    # Build the optional provider packet only after deterministic work, and
    # only when a provider is explicitly supplied.
    provider_review = {
        "status": "not_requested", "request_count": 0, "findings": [],
        "rejected_findings": [], "error_category": None,
        "max_requests": MAX_PROVIDER_REQUESTS,
    }
    cache_state = "not_used"
    if provider is not None:
        try:
            packet = build_provider_packet(
                request, document, claims, references,
                c2_findings + c5_findings, mapping)
            packet_digest = _provider_packet_digest(packet)
        except SchemeReviewError:
            packet = None
            packet_digest = None
            provider_review = {
                "status": "unavailable_evidence", "request_count": 0,
                "findings": [], "rejected_findings": [],
                "error_category": "packet_unavailable",
                "max_requests": MAX_PROVIDER_REQUESTS,
            }
        if packet is not None:
            cached = None
            if cache is not None:
                cached = cache.get(
                    request["reviewer_id"],
                    request["assignment"]["assignment_id"],
                    request["assignment"]["revision"],
                    _effective_scheme_sha(request, mapping),
                    packet_digest)
            if cached is not None:
                provider_review = cached
                cache_state = "hit"
            else:
                provider_review = _run_provider(
                    request, document, packet, provider)
                cache_state = "miss"
                if cache is not None and provider_review["status"].startswith(
                        "completed"):
                    cache.put(
                        request["reviewer_id"],
                        request["assignment"]["assignment_id"],
                        request["assignment"]["revision"],
                        _effective_scheme_sha(request, mapping),
                        packet_digest, provider_review)
    provider_findings = provider_review["findings"]
    if provider_review["status"] not in {
            "not_requested", "completed", "completed_with_rejections"}:
        unavailable = _finding(
            "C5-PROVIDER-REVIEW-UNAVAILABLE", "provider_review_unavailable",
            INSUFFICIENT, "medium", None, [],
            {"provider_status": provider_review["status"]},
            "The optional provider review did not complete. Deterministic "
            "checks still ran; the provider finding set is unavailable and "
            "must not be assumed empty.", blocking=False,
            acknowledgement_required=True, origin="deterministic")
        c5_findings.append(unavailable)
        c5_findings = _unique_findings(c5_findings)

    # Teacher decisions are applied only after every deterministic and
    # provider finding is known, so acknowledgements are checked against the
    # complete set.
    effective_sha = _effective_scheme_sha(request, mapping)
    applicable, ignored_reasons = _decision_applicability(
        request, effective_sha)
    decision_findings, scheme_decisions, question_decisions, \
        criterion_decisions = _decision_findings(
            ignored_reasons, applicable, document)
    c5_findings.extend(decision_findings)
    c5_findings = _unique_findings(c5_findings)

    hard_findings, ack_findings = _collect_accounting(
        c2_findings, c2_blocking, c5_findings, provider_findings)
    known_ids = ({finding["finding_id"] for finding in c2_findings}
                 | {finding["finding_id"] for finding in c5_findings}
                 | {finding["finding_id"] for finding in provider_findings})
    approval_findings, approval_valid, approved_targets = \
        _check_approval_acknowledgements(
            applicable, ack_findings, hard_findings, known_ids, document)
    c5_findings.extend(approval_findings)
    c5_findings = _unique_findings(c5_findings)

    # Recompute accounting from the final (uniquely identified) findings so a
    # suffixed id can never escape approval accounting.
    hard_findings, ack_findings = _collect_accounting(
        c2_findings, c2_blocking, c5_findings, provider_findings)
    hard_blockers = {finding["finding_id"] for finding in hard_findings}
    required_acknowledgements = {
        finding["finding_id"] for finding in ack_findings}
    if hard_blockers:
        approval_valid = False
    if approval_findings:
        approval_valid = False

    review_status = _compute_status(
        request, hard_blockers, required_acknowledgements, applicable,
        approval_valid, provider_findings)

    affected = _affected_criteria(
        c2_findings + c5_findings + provider_findings, document)
    rejected_criteria = {
        decision["target"]["id"] for decision in criterion_decisions
        if decision["decision"] == "reject"}
    rejected_questions = {
        decision["target"]["id"] for decision in question_decisions
        if decision["decision"] == "reject"}
    criterion_status = {}
    for question, criterion in document.criteria():
        if review_status == "REJECTED":
            criterion_status[criterion.id] = "REJECTED"
        elif criterion.id in rejected_criteria \
                or question.id in rejected_questions:
            criterion_status[criterion.id] = "REJECTED"
        elif review_status == "TEACHER_APPROVED":
            criterion_status[criterion.id] = "TEACHER_APPROVED"
        elif ("criterion", criterion.id) in approved_targets \
                or ("question", question.id) in approved_targets:
            criterion_status[criterion.id] = "TEACHER_APPROVED"
        elif criterion.id in affected:
            criterion_status[criterion.id] = "NEEDS_TEACHER_REVIEW"
        elif review_status == "CHECKED":
            criterion_status[criterion.id] = "CHECKED"
        else:
            criterion_status[criterion.id] = "UNREVIEWED"

    question_status = {}
    for question in document.questions:
        statuses = [criterion_status[criterion.id]
                    for criterion in question.criteria]
        if "REJECTED" in statuses:
            question_status[question.id] = "REJECTED"
        elif "NEEDS_TEACHER_REVIEW" in statuses:
            question_status[question.id] = "NEEDS_TEACHER_REVIEW"
        elif statuses and all(status == "TEACHER_APPROVED"
                              for status in statuses):
            question_status[question.id] = "TEACHER_APPROVED"
        elif statuses and all(status == "CHECKED" for status in statuses):
            question_status[question.id] = "CHECKED"
        else:
            question_status[question.id] = "UNREVIEWED"

    evidence_total = sum(
        1 for question in document.questions for _ in question.criteria)
    explicit_pages = 0
    heuristic_pages = 0
    if mapping is not None:
        for entry in mapping["matches"]:
            for reference in entry["references"]:
                if reference["method"] == METHOD_CONTINUATION:
                    heuristic_pages += 1
                else:
                    explicit_pages += 1
    result = {
        "schema": RESULT_SCHEMA,
        "specification": request["specification"],
        "assignment": copy.deepcopy(request["assignment"]),
        "extracted": {
            "origin": "proposed_model_extraction",
            "proposed_scheme": document.as_dict(),
            "criterion_claims": copy.deepcopy(claims),
        },
        "verified": {
            "c2_findings": c2_findings,
            "c5_findings": c5_findings,
            "c2_approval_accounting": c2_blocking,
            "effective_totals": effective_totals,
            "reconciliation": reconciliation,
            "evidence_references": references,
            "question_mapping": _summarize_mapping(mapping),
            "evidence_coverage": {
                "criteria_total": evidence_total,
                "validated_references": len(references),
                "explicit_mapping_pages": explicit_pages,
                "heuristic_mapping_pages": heuristic_pages,
                "coverage": (len(references) / evidence_total
                             if evidence_total else 0.0),
            },
            "derived_mark_types": _derived_mark_types(document),
            "required_acknowledgements": sorted(required_acknowledgements),
            "hard_blockers": sorted(hard_blockers),
            "source_scheme_sha256": source_sha,
        },
        "provider_review": dict(
            provider_review,
            cache=cache_state,
            findings=provider_findings,
        ),
        "teacher": {
            "reviewer_id": request["reviewer_id"],
            "review_status": review_status,
            "approval_valid": bool(approval_valid),
            "approval_binding": {
                "reviewer_id": request["reviewer_id"],
                "assignment_id": request["assignment"]["assignment_id"],
                "revision": request["assignment"]["revision"],
                "scheme_sha256": effective_sha,
            },
            "decisions_applied": copy.deepcopy(applicable),
            "decisions_ignored": len(request["teacher_decisions"])
            - len(applicable),
            "scheme_decisions_applied": len(scheme_decisions),
            "question_decisions_applied": len(question_decisions),
            "criterion_decisions_applied": len(criterion_decisions),
            "criterion_status": criterion_status,
            "question_status": question_status,
        },
        "safeguards": dict(SAFEGUARDS),
    }
    _bounded_json(result, "result", "review result", limit=MAX_RESULT_CHARS)
    return result


def _effective_scheme_sha(request, mapping):
    assignment_sha = request["assignment"]["scheme_sha256"]
    if assignment_sha is not None:
        return assignment_sha
    if mapping is not None:
        return mapping["sha256"]
    source = request["source_documents"].get("mark_scheme")
    return None if source is None else source["sha256"]


def finding_key(finding):
    """Compact comparable descriptor for C5 findings."""
    return (finding["rule_id"], finding["question_id"],
            tuple(finding["criterion_ids"]))
