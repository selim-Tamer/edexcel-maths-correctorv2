"""Conservative mapping between extracted PDF pages and question ids.

Phase C Stage 3. Investigation-only module: no production imports, no
network, no OCR, and no guessing of marks or totals from extracted text.

Principles:

- Question identifiers are supplied EXPLICITLY by the caller; this module
  never invents or renumbers question ids.
- A heading match is recognised only from a conservative anchored pattern
  (``Q3``, ``Question 3`` with a following separator or end of line).
  Numbers inside tables, worked solutions, equations or marks are NOT
  headings; bare numbers are only recognised behind an explicit opt-in.
- A page/question association can have exactly one of these origins:
  a heading found in extracted text (``pdf_extracted``), an explicit
  caller-supplied hint page (``caller_supplied``), an opt-in heuristic
  continuation covering pages between headings (``heuristic_inference``),
  or an approved teacher mapping (``human_approved``, never granted here).
- When a question heading appears on more than one page the mapping is
  marked ambiguous; every candidate is kept visible and human review is
  required.
- Missing text layers make mapping unresolved: text absence never proves
  a question is missing.

Output is deterministic and JSON-serializable (schema
``investigation.pdf_question_map/1``).
"""
import re

from investigation.pdf_evidence import (
    DOCUMENT_ROLES,
    evidence_reference,
    page_reference,
    validate_document_evidence,
)

MAX_QUESTION_IDS = 500
MAX_QUESTION_ID_CHARS = 200

REQUEST_SCHEMA = "investigation.pdf_question_map/1"

CONTINUATION_POLICY_EXPLICIT = "explicit_only"
CONTINUATION_POLICY_HEURISTIC_ADJACENT = "heuristic_adjacent"
CONTINUATION_POLICIES = (CONTINUATION_POLICY_EXPLICIT,
                         CONTINUATION_POLICY_HEURISTIC_ADJACENT)

METHOD_HEADING = "heading_recognition"
METHOD_HINT = "caller_supplied_hint"
METHOD_CONTINUATION = "page_order_continuation"
METHODS = (METHOD_HEADING, METHOD_HINT, METHOD_CONTINUATION)

_HEADING_PATTERN = re.compile(
    r"^\s*(?:Question|Q)\s*(\d{1,3})"
    r"(?=\s*(?:\([a-z\d]+\)|[.:,;\-]|total\b|marks?\b|\s*$))",
    re.IGNORECASE)
_BARE_NUMBER_PATTERN = re.compile(r"^\s*(\d{1,3})\s*[.:)\]]\s*$")


def _number_for_heading(text, allow_bare_numbers):
    """Return the heading question number on one text line, or None."""
    match = _HEADING_PATTERN.match(text)
    if match:
        return match.group(1), "prefix"
    if allow_bare_numbers:
        match = _BARE_NUMBER_PATTERN.match(text)
        if match:
            return match.group(1), "bare_number"
    return None


def _canonical_number(question_id):
    """Derive a comparable heading number from an explicit id, or None.

    Only ids of shape ``Q<int>``, ``Question <int>`` or ``<int>`` can match
    extracted headings automatically; every other id shape is left for
    explicit hints, never guessed at.
    """
    stripped = question_id.strip()
    match = re.fullmatch(r"Question\s+(\d{1,3})", stripped, re.IGNORECASE)
    if match:
        return match.group(1), "prefix"
    match = re.fullmatch(r"Q(\d{1,3})", stripped)
    if match:
        return match.group(1), "prefix"
    if re.fullmatch(r"\d{1,3}", stripped):
        return stripped, "bare_number"
    return None


def validate_question_mapping(record):
    """Strictly validate a mapping record; return it unchanged.

    Downstream linking relies on this so no fabricated mapping can attach
    evidence references to findings.
    """
    from investigation.pdf_evidence import (
        validate_document_evidence, validate_evidence_reference,
    )
    if not isinstance(record, dict):
        raise ValueError("A question mapping must be an object")
    if record.get("schema") != REQUEST_SCHEMA:
        raise ValueError("Unknown question mapping schema")
    expected = {
        "schema", "document_id", "document_role", "sha256", "page_count",
        "question_ids", "heading_recognition", "matches", "unresolved",
        "unmatched_headings", "hint_pages", "unheadable_ids", "warnings",
        "continuation_policy",
    }
    if set(record) != expected:
        raise ValueError("Question mapping has unexpected keys")
    if record["document_role"] not in DOCUMENT_ROLES:
        raise ValueError("Invalid document_role in question mapping")
    page_count = record["page_count"]
    if type(page_count) is not int or page_count < 1:
        raise ValueError("Invalid page_count in question mapping")
    question_ids = record["question_ids"]
    if not isinstance(question_ids, list) or not question_ids:
        raise ValueError("Invalid question id list in question mapping")
    unmatched = sum(1 for entry in record["matches"]
                    if not isinstance(entry, dict)
                    or entry.get("question_id") not in question_ids
                    or entry.get("certainty") not in (
                        "explicit", "ambiguous", "heuristic",
                        "hint_only", "unresolved")
                    or not isinstance(entry.get("references"), list))
    if unmatched or len(record["matches"]) != len(question_ids):
        raise ValueError("Malformed matches in question mapping")
    for entry in record["matches"]:
        for reference in entry["references"]:
            validate_evidence_reference(reference)
            if reference["question_id"] != entry["question_id"]:
                raise ValueError(
                    "Evidence reference does not belong to its match")
            if reference["page"] > page_count:
                raise ValueError("Reference page outside the document")
    if record.get("unresolved") is not None:
        for entry in record["unresolved"]:
            if (not isinstance(entry, dict)
                    or entry.get("question_id") not in question_ids
                    or not isinstance(entry.get("reasons"), list)):
                raise ValueError("Malformed unresolved entry in mapping")
    if record.get("hint_pages") is not None:
        hints = record["hint_pages"]
        if not isinstance(hints, dict):
            raise ValueError("Invalid hint_pages in question mapping")
        for question_id, pages in hints.items():
            if (question_id not in question_ids
                    or not isinstance(pages, list)
                    or any(type(page) is not int or page < 1
                           or page > page_count for page in pages)):
                raise ValueError("Invalid hint pages in question mapping")
    return record


def validate_question_ids(question_ids):
    """Strictly validate the explicit caller question-id list."""
    if not isinstance(question_ids, (list, tuple)):
        raise ValueError("question_ids must be a list of explicit ids")
    if not question_ids or len(question_ids) > MAX_QUESTION_IDS:
        raise ValueError(
            f"question_ids must contain 1 to {MAX_QUESTION_IDS} ids")
    for question_id in question_ids:
        if (not isinstance(question_id, str) or not question_id.strip()
                or len(question_id) > MAX_QUESTION_ID_CHARS):
            raise ValueError(
                "Each question id must be a non-empty string of at most "
                f"{MAX_QUESTION_ID_CHARS} characters")
    if len(set(question_ids)) != len(question_ids):
        raise ValueError("question_ids must be unique")
    return list(question_ids)


def validate_hints(hints, question_ids, page_count):
    """Strictly validate an optional explicit caller hint mapping."""
    if hints is None:
        return {}
    if not isinstance(hints, dict):
        raise ValueError("hints must map question ids to page lists")
    if set(hints) - set(question_ids):
        raise ValueError("hints contain ids outside question_ids")
    validated = {}
    for question_id, pages in hints.items():
        if not isinstance(pages, (list, tuple)) or not pages:
            raise ValueError(f"hint pages for '{question_id}' must be a "
                             "non-empty list")
        if len(set(pages)) != len(pages):
            raise ValueError(f"hint pages for '{question_id}' repeat")
        for page in pages:
            if type(page) is not int or page < 1 or page > page_count:
                raise ValueError(
                    f"hint pages for '{question_id}' are outside the "
                    "document page range")
        validated[question_id] = list(pages)
    return validated


def _page_heading_tokens(document, allow_bare_numbers):
    """All heading tokens per page; excerpt and span kept verbatim."""
    tokens = []
    for page_record in document["pages"]:
        page_tokens = []
        for line in page_record["text"].splitlines():
            found = _number_for_heading(line, allow_bare_numbers)
            if found:
                number, style = found
                page_tokens.append({
                    "number": number, "page": page_record["page"],
                    "excerpt": line[:240], "style": style})
        tokens.append(page_tokens)
    return tokens


def map_question_pages(document, question_ids, *, hints=None,
                       allow_bare_numbers=False,
                       continuation_policy=CONTINUATION_POLICY_EXPLICIT):
    """Map explicit question ids onto documented pages, conservatively."""
    validate_document_evidence(document)
    validate_question_ids(question_ids)
    if continuation_policy not in CONTINUATION_POLICIES:
        raise ValueError("Unknown continuation policy")
    hint_pages = validate_hints(hints, question_ids,
                                document["page_count"])
    tokens = _page_heading_tokens(document, allow_bare_numbers)
    canonical = {}
    unheadable = []
    for question_id in question_ids:
        derived = _canonical_number(question_id)
        if derived is None:
            unheadable.append(question_id)
        else:
            canonical[derived[0]] = question_id

    matches = {question_id: [] for question_id in question_ids}
    unmatched_headings = []
    heading_numbers_by_page = {}
    for page_tokens in tokens:
        for token in page_tokens:
            # Same number must map to at most one requested id: duplicates
            # would make every downstream attribution ambiguous.
            question_id = canonical.get(token["number"])
            heading_numbers_by_page.setdefault(token["page"], set()).add(
                token["number"])
            if question_id is None:
                unmatched_headings.append({
                    "page": token["page"],
                    "heading_number": token["number"],
                    "excerpt": token["excerpt"]})
                continue
            reference = evidence_reference(
                document, token["page"], excerpt=token["excerpt"],
                question_id=question_id, method=METHOD_HEADING,
                data_origin="pdf_extracted")
            matches[question_id].append(reference)

    pages_all_empty = all(not page["text"].strip()
                          for page in document["pages"])

    result = {
        "schema": REQUEST_SCHEMA,
        "document_id": document["document_id"],
        "document_role": document["document_role"],
        "sha256": document["sha256"],
        "page_count": document["page_count"],
        "question_ids": list(question_ids),
        "heading_recognition": {
            "prefix_pattern": "^(?:Question|Q)\\s*\\d+",
            "bare_numbers_allowed": bool(allow_bare_numbers),
            "marks_ever_parsed": False,
        },
        "matches": [],
        "unresolved": [],
        "unmatched_headings": sorted(
            unmatched_headings, key=lambda entry: (entry["page"],
                                                   entry["heading_number"])),
        "hint_pages": {question_id: list(hint_pages.get(question_id, []))
                       for question_id in question_ids},
        "unheadable_ids": sorted(unheadable),
        "warnings": [],
        "continuation_policy": continuation_policy,
    }

    # Continuation: opt-in only; pages between a heading and the next
    # heading (in page order) MAY belong to the spanning question. This is
    # a heuristic inference and is always labelled as such.
    if continuation_policy == CONTINUATION_POLICY_HEURISTIC_ADJACENT:
        ordered = []
        for question_id in question_ids:
            for match in matches[question_id]:
                ordered.append((match["page"], question_id))
        ordered.sort()
        for position, (page_number, question_id) in enumerate(ordered):
            next_page = (ordered[position + 1][0]
                         if position + 1 < len(ordered)
                         else document["page_count"] + 1)
            for between in range(page_number + 1, min(next_page,
                                                      document["page_count"]
                                                      + 1)):
                page_record = document["pages"][between - 1]
                if not page_record["text"].strip():
                    continue
                matches[question_id].append(page_reference(
                    document, between, method=METHOD_CONTINUATION,
                    data_origin="heuristic_inference",
                    question_id=question_id))

    for question_id in question_ids:
        references = matches[question_id]
        heading_references = [ref for ref in references
                              if ref["method"] == METHOD_HEADING]
        hint_present = question_id in hint_pages
        if heading_references:
            distinct_pages = {ref["page"] for ref in heading_references}
            if len(distinct_pages) > 1:
                certainty = "ambiguous"
                result["warnings"].append(
                    f"Question '{question_id}' heading appears on "
                    f"{len(distinct_pages)} pages; mapping is ambiguous")
            else:
                certainty = "explicit"
        elif hint_present:
            certainty = "hint_only"
        elif any(ref["method"] == METHOD_CONTINUATION
                 for ref in references):
            certainty = "heuristic"
        else:
            certainty = "unresolved"
            if pages_all_empty:
                result["unresolved"].append({
                    "question_id": question_id,
                    "reasons": ["all_pages_lack_text_layer"]})
            elif question_id in unheadable:
                result["unresolved"].append({
                    "question_id": question_id,
                    "reasons": ["id_shape_not_matchable", "no_heading_found"]})
            else:
                result["unresolved"].append({
                    "question_id": question_id,
                    "reasons": ["no_heading_found"]})
        ordered_references = sorted(
            references, key=lambda ref: (ref["page"], ref["method"],
                                         ref["excerpt"] or ""))
        result["matches"].append({
            "question_id": question_id,
            "certainty": certainty,
            "references": ordered_references,
            "pages": sorted({ref["page"] for ref in ordered_references}),
        })
    result["matches"].sort(key=lambda entry: entry["question_id"])
    result["unresolved"].sort(key=lambda entry: entry["question_id"])
    if unmatched_headings:
        result["warnings"].append(
            "Headings were found for question numbers outside the "
            "requested id list; they are recorded, never invented into "
            "mappings")
    sequence = [(page, int(number))
                for page in sorted(heading_numbers_by_page)
                for number in sorted(heading_numbers_by_page[page],
                                     key=int)]
    ranks = [number for _, number in sequence]
    if ranks != sorted(ranks):
        result["warnings"].append(
            "Heading numbers are not in ascending page order; treat "
            "continuation reasoning with care")
    return result
