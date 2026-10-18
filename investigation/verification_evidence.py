"""Link Stage C2 verification findings to offline PDF evidence; align docs.

Phase C Stage 3. Investigation-only: no production imports (the C2 engine
itself is investigation code and provides only the finding-state
vocabulary), no network, no AI calls, no execution of PDF content.

Guarantees (all enforced in code and by tests):

- Every original C2 finding is preserved untouched: no conclusion, state,
  id, evidence or explanation is modified. Linking only ADDS a
  ``pdf_evidence`` section to a deep copy.
- A PDF page/heading association NEVER upgrades a finding. The structural
  conclusion of ``verify_scheme_document`` stands on its structured input;
  PDF evidence merely names source pages to check, and its provenance is
  always labelled (pdf_extracted / caller_supplied / heuristic_inference).
- Criterion-level evidence is attached only when the caller EXPLICITLY
  supplies a page hint for that criterion id; nothing is inferred from a
  criterion's note or code.
- Alignment findings are conservative: text-based absence is
  ``suspected_issue``, evidence gaps are ``evidence_insufficient``,
  ambiguous mappings are ``human_review_required``. Nothing in this stage
  is ever reported as a *verified* claim about a real Pearson document,
  because text extraction cannot prove that anything is missing.
- Alignment finding identity is deterministic ``rule_id::question_id::
  discriminator`` where the discriminator carries the document role
  (plus the heading number / role-specific detail), so logically distinct
  findings — e.g. the same rule in both documents — never collide, and
  output order is never used as identity. ``check_document_alignment``
  raises AssertionError if finding ids are not unique, so findings can
  never be silently lost or overwritten.
- ``PDF-ALIGN-EXTRACTION-INCOMPLETE`` is emitted whenever per-page
  extraction fails, a page is truncated by the text cap
  (``ambiguous_layout``), the aggregate text budget runs out
  (``budget_exhausted``), or the document-level status is
  ``unsupported_content``; its evidence keeps a separate page list per
  condition, so none of the conditions masks another. Incompleteness of
  evidence is never presented as an error in the source documents'
  mathematical or mark-scheme content.
- ``pdf_evidence.certainty`` aggregates per-document certainties and
  caller criterion hints with the documented weakest-source policy
  (``AGGREGATION_POLICY``): the combined certainty is never stronger than
  its weakest contributing source; any ambiguous match makes it ambiguous
  and sets ``review_required``. Each source keeps its own certainty in
  ``source_certainties``. An explicit page match in one document can never
  upgrade a heuristic or hint-only mapping in the other document into
  verified source evidence.
"""
import copy

from investigation.pdf_evidence import (
    DATA_ORIGINS, DOCUMENT_ROLES, MAX_PAGES, validate_document_evidence,
    validate_evidence_reference,
)
from investigation.pdf_question_map import (
    METHOD_CONTINUATION, METHOD_HEADING, METHOD_HINT, METHODS,
    validate_question_mapping,
)
from investigation.verification_engine import (
    HUMAN_REVIEW, INSUFFICIENT, SUSPECTED, REVIEW_STATES,
)

LINK_SCHEMA = "investigation.verification_evidence/2"
ALIGNMENT_SCHEMA = "investigation.verification_evidence.alignment/1"

_C2_FINDING_KEYS = {
    "finding_id", "rule_id", "issue_category", "state", "severity",
    "question_id", "criterion_ids", "evidence", "explanation",
    "review_required",
}

CERTAINTY_EXPLICIT = "explicit_page_match"
CERTAINTY_AMBIGUOUS = "ambiguous_page_match"
CERTAINTY_HINT = "page_hint_only"
CERTAINTY_HEURISTIC = "heuristic_page_match"
CERTAINTY_UNAVAILABLE = "unavailable"

ALIGNMENT_RULES = (
    "PDF-ALIGN-UNMATCHED-IN-PAPER",
    "PDF-ALIGN-UNMATCHED-IN-SCHEME",
    "PDF-ALIGN-AMBIGUOUS-MAPPING",
    "PDF-ALIGN-UNMATCHED-HEADINGS",
    "PDF-ALIGN-TEXT-LAYER-MISSING",
    "PDF-ALIGN-EXTRACTION-INCOMPLETE",
)

_HEADING_CERTAINTY_TO_LINK = {
    "explicit": CERTAINTY_EXPLICIT,
    "ambiguous": CERTAINTY_AMBIGUOUS,
    "heuristic": CERTAINTY_HEURISTIC,
    "hint_only": CERTAINTY_HINT,
    "unresolved": CERTAINTY_UNAVAILABLE,
}


def validate_c2_finding(finding):
    """Validate the shape of one Stage C2 finding; return it unchanged."""
    if not isinstance(finding, dict) or set(finding) != _C2_FINDING_KEYS:
        raise ValueError("Not a Stage C2 finding dict (unexpected keys)")
    if not isinstance(finding["finding_id"], str) \
            or not finding["finding_id"]:
        raise ValueError("Finding needs a non-empty finding_id")
    if not isinstance(finding["rule_id"], str) or not finding["rule_id"]:
        raise ValueError("Finding needs a non-empty rule_id")
    if not isinstance(finding["issue_category"], str) \
            or not finding["issue_category"]:
        raise ValueError("Finding needs a non-empty issue_category")
    if finding["state"] not in REVIEW_STATES:
        raise ValueError("Finding state is outside the C2 state vocabulary")
    if not isinstance(finding["severity"], str) or not finding["severity"]:
        raise ValueError("Finding needs a non-empty severity")
    question_id = finding["question_id"]
    if question_id is not None and (not isinstance(question_id, str)
                                    or not question_id.strip()):
        raise ValueError("Finding question_id must be a string or None")
    criterion_ids = finding["criterion_ids"]
    if not isinstance(criterion_ids, list) \
            or any(not isinstance(item, str) or not item.strip()
                   for item in criterion_ids):
        raise ValueError("Finding criterion_ids must be a list of strings")
    if not isinstance(finding["evidence"], dict):
        raise ValueError("Finding evidence must be a dict")
    if not isinstance(finding["explanation"], str) \
            or not finding["explanation"]:
        raise ValueError("Finding needs a non-empty explanation")
    if not isinstance(finding["review_required"], bool):
        raise ValueError("Finding review_required must be a boolean")
    return finding


def _validate_criterion_page_hints(hints, question_paper_document,
                                   mark_scheme_document):
    if hints is None:
        return {"mark_scheme": {}, "question_paper": {}}
    if not isinstance(hints, dict):
        raise ValueError("criterion_page_hints must be a two-level dict")
    unknown = set(hints) - set(DOCUMENT_ROLES)
    if unknown:
        raise ValueError("criterion_page_hints roles are document roles")
    validated = {}
    for role in DOCUMENT_ROLES:
        entries = hints.get(role, {})
        if not isinstance(entries, dict):
            raise ValueError(f"criterion_page_hints['{role}'] must be a dict")
        section = {}
        for reference_id, pages in entries.items():
            if (not isinstance(reference_id, str) or not reference_id.strip()
                    or not isinstance(pages, (list, tuple)) or not pages):
                raise ValueError(
                    "criterion hints need an id and a non-empty page list")
            if len(set(pages)) != len(pages):
                raise ValueError("criterion hint pages repeat")
            for page in pages:
                if type(page) is not int or page < 1:
                    raise ValueError("criterion hint pages are 1-based ints")
                document = (mark_scheme_document if role == "mark_scheme"
                            else question_paper_document)
                if document is not None and page > document["page_count"]:
                    raise ValueError(
                        f"criterion hint page {page} for '{reference_id}' "
                        "is outside the document")
                if page > MAX_PAGES:
                    raise ValueError("criterion hint page beyond bounds")
            section[reference_id] = sorted(pages)
        validated[role] = section
    return validated


def _role_section(document, mapping, question_id):
    """Build one role's evidence block for a finding."""
    if document is None or mapping is None:
        return None
    digest = document["sha256"]
    if mapping["sha256"] != digest:
        raise ValueError("Mapping and document digests disagree")
    if mapping["document_role"] != document["document_role"]:
        raise ValueError("Mapping and document roles disagree")
    entry = next((match for match in mapping["matches"]
                  if match["question_id"] == question_id), None)
    references = []
    if entry is not None:
        for reference in entry["references"]:
            validate_evidence_reference(reference)
            # Reference identity must remain bound to THIS document record:
            # digest, document id and role all have to agree, otherwise a
            # reference could point at a different PDF than the finding's
            # evidence section describes.
            if (reference["sha256"] != digest
                    or reference["document_id"] != document["document_id"]
                    or reference["document_role"]
                    != document["document_role"]):
                raise ValueError(
                    "Evidence reference belongs to a different document "
                    "record than the finding's evidence section")
            references.append(reference)
    hint_pages = mapping["hint_pages"].get(question_id, []) if entry else []
    return {
        "document_id": document["document_id"],
        "sha256": digest,
        "document_role": document["document_role"],
        "page_count": document["page_count"],
        "links": [copy.deepcopy(reference) for reference in references],
        "hint_pages": list(hint_pages),
        "certainty": _HEADING_CERTAINTY_TO_LINK.get(
            entry["certainty"]) if entry else None,
    }


# Weakest-source aggregation ranks. The combined certainty is never stronger
# than the weakest contributing source: heuristic page-order continuation and
# caller-supplied hints can never be upgraded by an explicit page match in
# another document. An ambiguous match anywhere is reported as ambiguous and
# always requires human review.
AGGREGATION_POLICY = (
    "weakest contributing source: the combined certainty equals the "
    "weakest per-document certainty (explicit > heuristic > hint_only) "
    "or caller hint; any ambiguous page match makes the combined "
    "certainty ambiguous and requires human review")

_CERTAINTY_RANK = {
    CERTAINTY_EXPLICIT: 3,
    CERTAINTY_HEURISTIC: 2,
    CERTAINTY_HINT: 1,
}


def _aggregate_certainty(sections, hint_links):
    """Weakest-source certainty aggregation (documented, deterministic).

    Every contributing source keeps its own certainty in
    ``source_certainties``; the aggregate NEVER reports a stronger level
    than any contributing source. heuristic matches and caller hints are
    never promoted to independently verified source evidence.
    """
    values = [section["certainty"] for section in sections
              if section is not None and section["certainty"] is not None]
    if hint_links:
        # Caller criterion hints are contributing evidence too (they add
        # page lists), so they also bound the combined certainty.
        values.append(CERTAINTY_HINT)
    if CERTAINTY_AMBIGUOUS in values:
        # Ambiguous is the most cautious outcome: human review.
        return CERTAINTY_AMBIGUOUS
    if values:
        return min(values, key=lambda value: _CERTAINTY_RANK[value])
    return CERTAINTY_UNAVAILABLE


_NOTES = {
    CERTAINTY_EXPLICIT:
        "Page-level references attached from the extracted text layer; "
        "PDF evidence does not independently confirm the structural "
        "finding recorded above.",
    CERTAINTY_AMBIGUOUS:
        "Multiple candidate pages for one question; the structural "
        "finding is unchanged and a teacher must choose the source page.",
    CERTAINTY_HINT:
        "At least one contributing source provides only caller-supplied "
        "hints or page-order heuristics for these pages; they were "
        "validated but not verified against the PDF.",
    CERTAINTY_HEURISTIC:
        "The combined certainty is limited by at least one source whose "
        "page references used page-order heuristics; treat these page "
        "references as needs-review, not as independently verified "
        "extracted evidence.",
    CERTAINTY_UNAVAILABLE:
        "No page-level mapping for this finding; no page references were "
        "inferred. Absence of mapping is not absence of source pages.",
}


def _linked_page_warnings(role, document, section):
    """Bounded, deduplicated warnings from a role's document and links."""
    warnings = []
    if document is None or section is None:
        return warnings
    indexed_pages = {page["page"]: page for page in document["pages"]}
    warnings.extend(document["warnings"])
    for reference in section["links"]:
        page_record = indexed_pages.get(reference["page"])
        if page_record is not None:
            warnings.extend(page_record["warnings"])
    return warnings


def link_findings_to_pdf_evidence(
        findings, question_paper_document=None, mark_scheme_document=None,
        question_paper_mapping=None, mark_scheme_mapping=None,
        criterion_page_hints=None):
    """Attach auditable PDF evidence to C2 findings without altering them.

    Returns the enriched findings in input order. Original findings are
    never mutated: the enriched copy is identical except for the added
    ``pdf_evidence`` section.
    """
    for document in (question_paper_document, mark_scheme_document):
        if document is not None:
            validate_document_evidence(document)
    for mapping in (question_paper_mapping, mark_scheme_mapping):
        if mapping is not None:
            validate_question_mapping(mapping)
    hints = _validate_criterion_page_hints(
        criterion_page_hints, question_paper_document, mark_scheme_document)

    role_documents = {"question_paper": question_paper_document,
                      "mark_scheme": mark_scheme_document}
    enriched = []
    for finding in findings:
        validate_c2_finding(finding)
        question_id = finding["question_id"]
        paper_section = _role_section(question_paper_document,
                                      question_paper_mapping, question_id)
        scheme_section = _role_section(mark_scheme_document,
                                       mark_scheme_mapping, question_id)
        criterion_hints = []
        for role in DOCUMENT_ROLES:
            section = hints.get(role, {})
            for reference_id in finding["criterion_ids"]:
                pages = section.get(reference_id)
                if pages:
                    criterion_hints.append({
                        "document_role": role,
                        "criterion_id": reference_id,
                        "pages": list(pages),
                        "marked_only_when": "explicit caller hint",
                    })

        warnings = []
        for role, section in (("question_paper", paper_section),
                              ("mark_scheme", scheme_section)):
            warnings.extend(_linked_page_warnings(role, role_documents[role],
                                                  section))
        deduplicated = sorted(set(warnings))[:200]

        certainty = _aggregate_certainty((paper_section, scheme_section),
                                         criterion_hints)
        pdf_evidence = {
            "schema": LINK_SCHEMA,
            "question_paper": paper_section,
            "mark_scheme": scheme_section,
            "source_certainties": {
                "question_paper": (paper_section["certainty"]
                                   if paper_section is not None else None),
                "mark_scheme": (scheme_section["certainty"]
                                if scheme_section is not None else None),
            },
            "aggregation_policy": AGGREGATION_POLICY,
            "criterion_page_hints": sorted(
                criterion_hints,
                key=lambda hint: (hint["document_role"],
                                  hint["criterion_id"], hint["pages"])),
            "extraction_warnings": deduplicated,
            "evidence_available": certainty != CERTAINTY_UNAVAILABLE,
            "certainty": certainty,
            "note": _NOTES[certainty],
            "review_required": (finding["review_required"]
                                or certainty == CERTAINTY_AMBIGUOUS),
        }
        merged = copy.deepcopy(finding)
        merged["pdf_evidence"] = pdf_evidence
        enriched.append(merged)
    return enriched


def _alignment_finding(rule_id, category, state, severity, question_id,
                       evidence, explanation, discriminator):
    if state == "verified_structural_inconsistency":
        raise AssertionError(
            "Alignment findings must never claim structural verification")
    if (not isinstance(discriminator, str) or not discriminator.strip()):
        raise ValueError("Alignment findings need a role/discriminator")
    # Identity is rule + question + explicit discriminator (document role,
    # heading number, ...). Logically distinct findings can therefore never
    # collide (e.g. the same category of problem in both documents), and
    # output order is never a discriminator.
    finding_id = (f"{rule_id}::{question_id or '-'}"
                  f"::{discriminator}")
    return {
        "finding_id": finding_id,
        "rule_id": rule_id,
        "issue_category": category,
        "state": state,
        "severity": severity,
        "question_id": question_id,
        "criterion_ids": [],
        "evidence": evidence,
        "explanation": explanation,
        "review_required": True,
        "schema": ALIGNMENT_SCHEMA,
    }


def _ordered_alignment_findings(findings):
    """Deterministic ordering plus a hard uniqueness guard for finding ids.

    Distinct findings must never share an identity (a repeated id would mean
    one finding was overwritten in any downstream id-addressed consumer), so
    this is checked before every return of ``check_document_alignment`` and
    raises AssertionError rather than silently losing findings.
    """
    ordered = sorted(findings, key=lambda f: (f["finding_id"],
                                              f["rule_id"]))
    identifiers = [finding["finding_id"] for finding in ordered]
    if len(set(identifiers)) != len(identifiers):
        raise AssertionError(
            "Alignment finding ids must be unique; identity data was lost")
    return ordered


def _extraction_gaps(document):
    """Pages with unusable text layers grouped by state."""
    empty = [page["page"] for page in document["pages"]
             if page["status"] == "empty_text_layer"]
    failed = [page["page"] for page in document["pages"]
              if page["status"] == "extraction_failed"]
    ambiguous = [page["page"] for page in document["pages"]
                 if page["status"] == "ambiguous_layout"]
    trapped = [page["page"] for page in document["pages"]
               if page["status"] == "budget_exhausted"]
    return {"empty_text_layer": empty, "extraction_failed": failed,
            "ambiguous_layout": ambiguous, "budget_exhausted": trapped}


def check_document_alignment(
        question_paper_document=None, question_paper_mapping=None,
        mark_scheme_document=None, mark_scheme_mapping=None):
    """Conservative paper-vs-scheme alignment findings.

    Only the unions of the explicitly mapped/unmapped information are
    compared. A question whose heading is absent from extracted paper text
    is a SUSPECTED absence (extraction may simply have failed on a
    scanned page, hence never a *verified* mismatch of source PDFs);
    evidence gaps are INSUFFICIENT; multiple candidate scheme pages are
    HUMAN_REVIEW.
    """
    findings = []
    for document, mapping in ((question_paper_document,
                               question_paper_mapping),
                              (mark_scheme_document,
                               mark_scheme_mapping)):
        if document is not None:
            validate_document_evidence(document)
        if mapping is not None:
            validate_question_mapping(mapping)
        if document is not None and mapping is not None:
            if document["sha256"] != mapping["sha256"]:
                raise ValueError("Mapping digest disagrees with document")
            if document["document_role"] != mapping["document_role"]:
                raise ValueError("Mapping role disagrees with document")
    role_pairs = (
        ("question_paper", question_paper_document, question_paper_mapping),
        ("mark_scheme", mark_scheme_document, mark_scheme_mapping))

    # Text-layer and extraction-completeness findings (per role, honest
    # about the limitation rather than making claims about the document).
    for role, document, _mapping in role_pairs:
        if document is None:
            continue
        gaps = _extraction_gaps(document)
        if gaps["empty_text_layer"]:
            findings.append(_alignment_finding(
                "PDF-ALIGN-TEXT-LAYER-MISSING", "text_layer_missing",
                INSUFFICIENT, "low", None,
                {"document_role": role,
                 "pages": gaps["empty_text_layer"]},
                "The "
                + role.replace("_", " ")
                + " has page(s) without a digital text layer. No verdict "
                "about these pages is possible from text extraction; "
                "teacher review with the original PDF is required, and "
                "extracted-text absence never proves content absence.",
                role))
        # Incomplete evidence is reported whenever ANY extraction limitation
        # occurs: page extraction failures, per-page truncation
        # (ambiguous_layout), aggregate text-budget exhaustion, or
        # document-level unsupported content. One finding per document role,
        # with a separate page list per condition so no condition masks
        # another. This is an evidence incompleteness record, never an
        # interpretation of the mathematical or official mark-scheme content.
        incomplete_conditions = []
        if gaps["extraction_failed"]:
            incomplete_conditions.append("some pages failed extraction")
        if gaps["ambiguous_layout"]:
            incomplete_conditions.append(
                "some pages were truncated at the per-page text cap "
                "(reading order and completeness are uncertain)")
        if gaps["budget_exhausted"]:
            incomplete_conditions.append(
                "the aggregate text budget ran out, so no text was taken "
                "from later pages")
        if document["extraction_status"] == "unsupported_content":
            incomplete_conditions.append(
                "the document contains PDF features this engine "
                "deliberately does not interpret (such as embedded "
                "actions), flagged at document level")
        if incomplete_conditions:
            findings.append(_alignment_finding(
                "PDF-ALIGN-EXTRACTION-INCOMPLETE",
                "document_evidence_incomplete", INSUFFICIENT, "low", None,
                {"document_role": role,
                 "extraction_status": document["extraction_status"],
                 "failed_pages": gaps["extraction_failed"],
                 "ambiguous_layout_pages": gaps["ambiguous_layout"],
                 "budget_capped_pages": gaps["budget_exhausted"]},
                "Document evidence is incomplete for the "
                + role.replace("_", " ") + ": "
                + "; ".join(incomplete_conditions)
                + ". Alignment conclusions are bounded and require review; "
                "none of this implies anything about the source "
                "documents' mathematical or official mark-scheme content.",
                role))

    # Unmatched headings: numbers that appear as headings but were not in
    # the caller's id list. Recorded, never silently numbered-adjacent.
    for role, _document, mapping in role_pairs:
        if mapping is None:
            continue
        heading_groups = {}
        for entry in mapping["unmatched_headings"]:
            heading_groups.setdefault(entry["heading_number"],
                                      []).append(entry["page"])
        for number in sorted(heading_groups, key=int):
            pages = sorted(set(heading_groups[number]))
            findings.append(_alignment_finding(
                "PDF-ALIGN-UNMATCHED-HEADINGS",
                "heading_outside_requested_ids",
                HUMAN_REVIEW if len(pages) > 1 else SUSPECTED,
                "low", None,
                {"document_role": role, "heading_number": number,
                 "pages": pages, "multiple_pages": len(pages) > 1},
                f"The " + role + " text contains a question heading for "
                f"question number {number}, which is outside the "
                "caller-supplied id list. No mapping was invented; a "
                "teacher must decide whether the id list or the document "
                "is authoritative. Multiple pages make it ambiguous.",
                f"{role}:heading-{number}"))

    if question_paper_mapping is None or mark_scheme_mapping is None:
        return _ordered_alignment_findings(findings)

    # Cross-document comparison uses only mapped certainties.
    def mapped_ids(mapping):
        out = {}
        for match in mapping["matches"]:
            if match["certainty"] in {"explicit", "ambiguous", "heuristic",
                                      "hint_only"}:
                out[match["question_id"]] = match
        return out

    paper_mapped = mapped_ids(question_paper_mapping)
    scheme_mapped = mapped_ids(mark_scheme_mapping)

    def gaps_for(role, mapping, document):
        gaps = {}
        if document is None:
            return gaps
        for unresolved in mapping["unresolved"]:
            gaps[unresolved["question_id"]] = \
                set(unresolved.get("reasons", []))
        extraction = _extraction_gaps(document)
        unusable = set(extraction["empty_text_layer"]) \
            | set(extraction["extraction_failed"]) \
            | set(extraction["budget_exhausted"])
        return {"gaps": gaps, "unusable_pages": unusable}

    paper_state = gaps_for("question_paper", question_paper_mapping,
                           question_paper_document)
    scheme_state = gaps_for("mark_scheme", mark_scheme_mapping,
                            mark_scheme_document)

    for question_id in sorted(set(scheme_mapped) - set(paper_mapped)):
        paper_reasons = paper_state["gaps"].get(question_id)
        blocked = bool(paper_reasons and paper_reasons
                       & {"all_pages_lack_text_layer"})
        findings.append(_alignment_finding(
            "PDF-ALIGN-UNMATCHED-IN-PAPER", "question_not_found_in_paper",
            INSUFFICIENT if blocked else SUSPECTED,
            "medium", question_id,
            {"mapped_in": "mark_scheme", "missing_in": "question_paper",
             "paper_unresolved_reasons": sorted(paper_reasons or [])},
            "A question page was mapped in the mark scheme but no "
            "corresponding paper heading was found in the extracted "
            "paper text. This is a SUSPECTED mismatch: text extraction "
            "limits (including missing text layers) can hide real "
            "paper content, so this never asserts that the paper is "
            "missing material.",
            "question_paper"))

    for question_id in sorted(set(paper_mapped) - set(scheme_mapped)):
        scheme_reasons = scheme_state["gaps"].get(question_id)
        blocked = bool(scheme_reasons and scheme_reasons
                       & {"all_pages_lack_text_layer"})
        findings.append(_alignment_finding(
            "PDF-ALIGN-UNMATCHED-IN-SCHEME",
            "question_not_found_in_scheme",
            INSUFFICIENT if blocked else SUSPECTED, "medium", question_id,
            {"mapped_in": "question_paper", "missing_in": "mark_scheme",
             "scheme_unresolved_reasons": sorted(scheme_reasons or [])},
            "A question page was mapped in the question paper but no "
            "corresponding scheme heading was found in the extracted "
            "scheme text. Suspected only; extraction limitations can "
            "hide real scheme rows.",
            "mark_scheme"))

    # Multiple candidate pages (ambiguous mapping) -> human review.
    for role, mapping in (("mark_scheme", mark_scheme_mapping),
                          ("question_paper", question_paper_mapping)):
        if mapping is None:
            continue
        for match in mapping["matches"]:
            if match["certainty"] != "ambiguous":
                continue
            findings.append(_alignment_finding(
                "PDF-ALIGN-AMBIGUOUS-MAPPING", "multiple_candidate_pages",
                HUMAN_REVIEW, "medium", match["question_id"],
                {"document_role": role, "candidate_pages": match["pages"]},
                f"Question '{match['question_id']}' has multiple candidate "
                "pages in the " + role + "; the mapping is explicitly "
                "ambiguous and requires a teacher to resolve it. No "
                "candidate page was chosen automatically.",
                role))

    return _ordered_alignment_findings(findings)
