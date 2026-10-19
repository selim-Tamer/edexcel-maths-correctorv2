"""Offline PDF evidence extraction and evidence-reference contract.

Phase C Stage 3. Investigation-only: this module is NOT imported by the
production app and imports nothing from it (no app5_1_automated.py,
document_pipeline.py, local_pdf.py, marking_requests.py, Gemini SDK,
Streamlit or classroom code). No network, no AI API calls, no disk writes of
document content, no global byte cache, no eval/exec anywhere.

Purpose: give the mark-scheme verification engine a STRICT, validated, in
-memory representation of evidence extracted from question-paper and mark
-scheme PDFs. PDF text extraction is NOT OCR and NOT an authoritative
mathematical transcription: fractions, exponents, diagrams and complex
layouts are not reconstructed, and extracted strings are never auto-corrected
(for example "x2" is never rewritten as "x²"). Extracted text is untrusted
DATA and is never executed, never used in paths and never sent anywhere.

Two capabilities live here:

1. ``extract_pdf_evidence`` — bounded, per-page text extraction that preserves
   the original 1-based page order and records an explicit per-page extraction
   state instead of a numeric confidence. Empty text layers are reported as an
   EXTRACTION LIMITATION (the source page is never declared blank or
   mathematically wrong).

2. ``evidence_reference`` — the structured contract that links a verification
   finding to source evidence. An excerpt is accepted ONLY if it is an exact
   substring of the page's extracted text (and, when supplied, matches the
   char span exactly), so references cannot invent content that extraction
   did not produce.

Bounds mirror the production PDF conventions (``local_pdf.py``): a 50 MB
document cap, a 2000-page cap, a 20000-character per-page text cap, plus an
aggregate text budget so no document can drive unbounded memory growth. All
native PDF calls are serialized behind a module lock because the PDFium C API
is not thread-safe. The native document object is closed on every exit path.

Explicit extraction states (page level) instead of invented percentages:

- ``extracted``            text obtained within all bounds
- ``ambiguous_layout``     text obtained but truncated at a cap, so reading
                           order and completeness are uncertain
- ``empty_text_layer``     no characters obtained (possible scan, image-only
                           or graphical page; this is a limitation, not a
                           verdict about the document)
- ``extraction_failed``    the page could not be extracted
- ``budget_exhausted``     the aggregate text budget was spent before this
                           page, so no text was taken from it

Document-level status ``unsupported_content`` flags PDF features this engine
deliberately does not interpret (embedded JavaScript / open actions).

Page numbering is 1-based and ORIGINAL throughout (page ``n`` in the source
document is page ``n`` here). Documents are produced deterministically:
extracting the same bytes twice yields equal structures with no timestamps.
"""
import hashlib
import threading
from contextlib import closing
from io import BytesIO

__all__ = [
    "PdfExtractionError", "extract_pdf_evidence", "validate_document_evidence",
    "evidence_reference", "page_reference", "validate_evidence_reference",
]

MAX_DOCUMENT_BYTES = 50 * 1024 * 1024
MAX_PAGES = 2000
MAX_PAGE_TEXT_CHARS = 20000
MAX_TOTAL_TEXT_CHARS = 2_000_000
MAX_WARNINGS_PER_LEVEL = 100
MAX_WARNING_CHARS = 300
MAX_EXCERPT_CHARS = 240
MAX_OBJECT_CENSUS = 500
MAX_DOCUMENT_ID_CHARS = 100

DOCUMENT_ROLES = ("question_paper", "mark_scheme")

PAGE_STATES = (
    "extracted", "empty_text_layer", "extraction_failed",
    "ambiguous_layout", "budget_exhausted",
)

DOCUMENT_STATUSES = (
    "extracted", "partial_extraction", "extraction_failed",
    "unsupported_content",
)

# Where linked information comes from. A PDF text span is NOT automatically
# reliable evidence of a mathematical answer; only ``human_approved`` marks
# teacher/examiner-approved content and nothing in this stage grants it.
DATA_ORIGINS = (
    "pdf_extracted",           # read out of the PDF text layer verbatim
    "structured_declaration",  # explicit C2-style structured input
    "caller_supplied",         # explicit caller metadata (validated, trusted
                               #  as metadata, not verified against the PDF)
    "human_approved",          # only ever granted by an explicit approval
    "heuristic_inference",     # inferred by a heuristic rule; review needed
)

DOCUMENT_SCHEMA = "investigation.pdf_evidence/1"

# Single global lock: the native PDFium API is not thread-safe (same
# serialization convention as production local inspection).
_pdfium_lock = threading.Lock()


class PdfExtractionError(ValueError):
    """Malformed, oversized or unsupported PDF input.

    Message names the failure cause only; it never echoes document bytes,
    paths or extracted text, so it is safe to surface in a UI or log.
    """


def _warn(warnings, message):
    if len(warnings) < MAX_WARNINGS_PER_LEVEL:
        warnings.append(str(message)[:MAX_WARNING_CHARS])
    elif warnings and warnings[-1] != (
            f"Further warnings omitted (cap {MAX_WARNINGS_PER_LEVEL})"):
        warnings.append(
            f"Further warnings omitted (cap {MAX_WARNINGS_PER_LEVEL})")


def _normalize_input(data):
    """Accept bounded bytes or a bounded binary stream; return bytes only."""
    if isinstance(data, (bytes, bytearray)):
        payload = bytes(data)
    else:
        read = getattr(data, "read", None)
        if not callable(read):
            raise PdfExtractionError(
                "Extraction accepts bytes or a readable binary stream")
        try:
            payload = read(MAX_DOCUMENT_BYTES + 1)
        except Exception as exc:
            raise PdfExtractionError("Could not read the PDF stream") from exc
        if not isinstance(payload, (bytes, bytearray)):
            raise PdfExtractionError("PDF stream did not yield binary data")
        payload = bytes(payload)
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise PdfExtractionError(
            "PDF extraction supports documents up to "
            f"{MAX_DOCUMENT_BYTES // (1024 * 1024)} MB in this stage")
    if not payload:
        raise PdfExtractionError("No PDF bytes were supplied")
    return payload


def _classify_object(object_type):
    # FPDF_PAGEOBJ_* (pdfium): 1 text, 2 path, 3 image, 4 shading, 5 form.
    return {1: "text", 2: "path", 3: "image", 4: "shading",
            5: "form"}.get(object_type, "other")


def _page_census(page):
    """Bounded count of visible object kinds; never renders, never raises.

    Returns None when the census cannot be produced, so callers never
    mistake a failed census for an empty page.
    """
    counts = {"text": 0, "path": 0, "image": 0, "shading": 0,
              "form": 0, "other": 0, "limit_reached": False}
    try:
        get_objects = getattr(page, "get_objects", None)
        if get_objects is None:
            return None
        counted = 0
        for pdf_object in get_objects():
            if counted >= MAX_OBJECT_CENSUS:
                counts["limit_reached"] = True
                break
            counts[_classify_object(pdf_object.type)] += 1
            counted += 1
    except Exception:
        return None              # census is optional evidence, never failure
    return counts


def _unsupported_catalog_features(reader):
    """Names of catalog features deliberately not interpreted.

    Read-only catalogue inspection; the returned labels are fixed strings,
    never document-controlled values. Embedded scripts, open actions and
    additional actions are NOT executed or followed, only reported.
    """
    found = []
    try:
        catalog = reader.trailer["/Root"]
        if catalog.get("/OpenAction") is not None:
            found.append("OpenAction")
        if catalog.get("/AA") is not None:
            found.append("AdditionalActions")
        names = catalog.get("/Names")
        if names is not None and names.get("/JavaScript") is not None:
            found.append("EmbeddedJavaScript")
    except Exception:
        found.append("CatalogueUnreadable")
    return found


def extract_pdf_evidence(data, document_role, document_id=None):
    """Extract bounded per-page PDF evidence into the Stage 3 contract.

    Raises :class:`PdfExtractionError` (fail-closed) for oversized, empty,
    unreadable, encrypted, malformed or page-capped input. Per-page
    extraction problems never raise: they are recorded as an explicit page
    state so partial evidence stays auditable. The returned value is a plain
    JSON-serializable dict (see DOCUMENT_SCHEMA).
    """
    if document_role not in DOCUMENT_ROLES:
        raise ValueError(
            "document_role must be 'question_paper' or 'mark_scheme'")
    if document_id is not None:
        if (not isinstance(document_id, str) or not document_id.strip()
                or len(document_id) > MAX_DOCUMENT_ID_CHARS):
            raise ValueError(
                "document_id must be a non-empty string of at most "
                f"{MAX_DOCUMENT_ID_CHARS} characters")
    payload = _normalize_input(data)
    digest = hashlib.sha256(payload).hexdigest()
    if document_id is None:
        document_id = f"pdf:{digest[:16]}"

    # Imports are lazy so importing this module never loads PDF libraries
    # for consumers that only read already-extracted records.
    from pypdf import PdfReader
    import pypdfium2 as pdfium

    document = {
        "schema": DOCUMENT_SCHEMA,
        "document_id": document_id,
        "document_role": document_role,
        "sha256": digest,
        "page_count": 0,
        "extraction_status": "extraction_failed",
        "warnings": [],
        "pages": [],
    }
    with _pdfium_lock:
        pdf = None
        try:
            try:
                reader = PdfReader(BytesIO(payload), strict=True)
            except Exception:
                raise PdfExtractionError(
                    "The input is not readable as a PDF") from None
            if reader.is_encrypted:
                raise PdfExtractionError(
                    "Encrypted documents are not supported")
            try:
                pdf = pdfium.PdfDocument(payload)
            except Exception:
                raise PdfExtractionError(
                    "The input is not readable as a PDF") from None
            count = len(pdf)
            if count > MAX_PAGES:
                raise PdfExtractionError(
                    f"PDF extraction supports at most {MAX_PAGES} pages")
            if len(reader.pages) != count:
                raise PdfExtractionError(
                    "Conflicting page inventories between PDF parsers")
            document["page_count"] = count
            unsupported = _unsupported_catalog_features(reader)
            if unsupported:
                _warn(document["warnings"],
                      "Deliberately not interpreted: " + ", ".join(unsupported))
                document["extraction_status"] = "unsupported_content"
            warnings = document["warnings"]
            chars_used = 0
            failed_pages = 0
            for index in list(range(count)):
                page_record = {
                    "page": index + 1,
                    "text": "",
                    "status": "extraction_failed",
                    "warnings": [],
                    "objects": None,
                }
                document["pages"].append(page_record)
                text = None
                truncated = False
                try:
                    with closing(pdf[index]) as page:
                        page_record["objects"] = _page_census(page)
                        with closing(page.get_textpage()) as textpage:
                            available = textpage.count_chars()
                            remaining = MAX_TOTAL_TEXT_CHARS - chars_used
                            if remaining <= 0:
                                page_record["status"] = "budget_exhausted"
                                _warn(page_record["warnings"],
                                      "Aggregate text budget was already "
                                      "spent, so no text was taken")
                                continue
                            else:
                                capped = min(MAX_PAGE_TEXT_CHARS, remaining,
                                             available)
                                truncated = available > capped
                                text = (textpage.get_text_range(0, capped)
                                        if capped > 0 else "")
                except Exception:
                    failed_pages += 1
                    text = None
                    _warn(page_record["warnings"],
                          "Page extraction failed; the page state records "
                          "no usable text, not an empty document")
                    continue
                if text is None or not isinstance(text, str):
                    failed_pages += 1
                    continue          # defensive; never seen in practice
                chars_used += len(text)
                if truncated or len(text) > MAX_PAGE_TEXT_CHARS:
                    page_record["text"] = text
                    page_record["status"] = "ambiguous_layout"
                    _warn(page_record["warnings"],
                          "Text layer truncated at the extraction cap; "
                          "completeness and reading order are uncertain")
                elif not text.strip():
                    page_record["status"] = "empty_text_layer"
                    page_record["text"] = ""
                    _warn(page_record["warnings"],
                          "No digital text layer; this is an extraction "
                          "limitation (possible scan or graphical page), "
                          "not a claim that the page is blank")
                else:
                    page_record["text"] = text
                    page_record["status"] = "extracted"
            if failed_pages == count:
                document["extraction_status"] = "extraction_failed"
            elif failed_pages:
                document["extraction_status"] = "partial_extraction"
            elif any(page["status"] in ("ambiguous_layout",
                                        "budget_exhausted")
                     for page in document["pages"]):
                # Stage C4 Phase 6 correction: per-page truncation or
                # aggregate budget exhaustion is NOT a complete,
                # fully-verified extraction, so the document-level status
                # must say partial even when no page outright failed.
                document["extraction_status"] = "partial_extraction"
            elif document["extraction_status"] != "unsupported_content":
                document["extraction_status"] = "extracted"
            if any(p["status"] == "empty_text_layer"
                   for p in document["pages"]):
                _warn(warnings,
                      "At least one page has no digital text layer; text "
                      "absence in this record never implies content absence "
                      "in the source PDF")
            return document
        finally:
            if pdf is not None:
                try:
                    pdf.close()
                except Exception:
                    pass


def validate_document_evidence(document):
    """Strictly validate an extracted-document record; return it unchanged.

    Raises ValueError (content-free message) on any structural problem so
    downstream mapping/linking cannot operate on a fabricated record.
    """
    if not isinstance(document, dict):
        raise ValueError("A document evidence record must be an object")
    if document.get("schema") != DOCUMENT_SCHEMA:
        raise ValueError("Unknown document evidence schema")
    expected = {
        "schema", "document_id", "document_role", "sha256", "page_count",
        "extraction_status", "warnings", "pages",
    }
    if set(document) != expected:
        raise ValueError("Document evidence record has unexpected keys")
    document_id = document["document_id"]
    if (not isinstance(document_id, str) or not document_id.strip()
            or len(document_id) > MAX_DOCUMENT_ID_CHARS):
        raise ValueError("Invalid document_id in document evidence record")
    if document["document_role"] not in DOCUMENT_ROLES:
        raise ValueError("Invalid document_role in document evidence record")
    digest = document["sha256"]
    if (not isinstance(digest, str) or len(digest) != 64
            or any(character not in "0123456789abcdef"
                   for character in digest)):
        raise ValueError("Invalid sha256 in document evidence record")
    page_count = document["page_count"]
    if type(page_count) is not int or page_count < 1 or page_count > MAX_PAGES:
        raise ValueError("Invalid page_count in document evidence record")
    if document["extraction_status"] not in DOCUMENT_STATUSES:
        raise ValueError("Invalid extraction_status in document record")
    warnings = document["warnings"]
    if not isinstance(warnings, list) or len(warnings) > MAX_WARNINGS_PER_LEVEL:
        raise ValueError("Invalid warnings list in document record")
    if any(not isinstance(item, str) or len(item) > MAX_WARNING_CHARS
           for item in warnings):
        raise ValueError("Invalid warning entries in document record")
    pages = document["pages"]
    if not isinstance(pages, list) or len(pages) != page_count:
        raise ValueError("Pages list does not match page_count")
    for expected_page, page in enumerate(pages, start=1):
        if not isinstance(page, dict) or set(page) != {
                "page", "text", "status", "warnings", "objects"}:
            raise ValueError("Malformed page record in document evidence")
        if page["page"] != expected_page:
            raise ValueError("Page records must be 1-based and ordered")
        if page["status"] not in PAGE_STATES:
            raise ValueError("Invalid page extraction state")
        if not isinstance(page["text"], str):
            raise ValueError("Page text must be a string")
        if not isinstance(page["warnings"], list) \
                or len(page["warnings"]) > MAX_WARNINGS_PER_LEVEL:
            raise ValueError("Invalid page warnings list")
        if page["objects"] is not None \
                and not isinstance(page["objects"], dict):
            raise ValueError("Invalid page object census")
    return document


def validate_evidence_reference(reference):
    """Strictly validate one evidence-reference record; return it unchanged."""
    if not isinstance(reference, dict):
        raise ValueError("An evidence reference must be an object")
    expected = {
        "document_id", "document_role", "sha256", "page", "excerpt", "span",
        "question_id", "extraction_state", "method", "data_origin",
    }
    if set(reference) != expected:
        raise ValueError("Evidence reference has unexpected keys")
    if (not isinstance(reference["document_id"], str)
            or not reference["document_id"].strip()
            or len(reference["document_id"]) > MAX_DOCUMENT_ID_CHARS):
        raise ValueError("Invalid document_id in evidence reference")
    if reference["document_role"] not in DOCUMENT_ROLES:
        raise ValueError("Invalid document_role in evidence reference")
    digest = reference["sha256"]
    if (not isinstance(digest, str) or len(digest) != 64
            or any(character not in "0123456789abcdef"
                   for character in digest)):
        raise ValueError("Invalid sha256 in evidence reference")
    page = reference["page"]
    if type(page) is not int or page < 1 or page > MAX_PAGES:
        raise ValueError("Invalid page in evidence reference")
    if not isinstance(reference["extraction_state"], str) \
            or not reference["extraction_state"]:
        raise ValueError("Invalid extraction_state in evidence reference")
    if not isinstance(reference["method"], str) \
            or not reference["method"].strip() or len(reference["method"]) > 80:
        raise ValueError("Invalid method in evidence reference")
    if reference["data_origin"] not in DATA_ORIGINS:
        raise ValueError("Invalid data_origin in evidence reference")
    question_id = reference["question_id"]
    if question_id is not None and (not isinstance(question_id, str)
                                    or not question_id.strip()
                                    or len(question_id) > 200):
        raise ValueError("Invalid question_id in evidence reference")
    excerpt = reference["excerpt"]
    if excerpt is not None:
        if (not isinstance(excerpt, str) or not excerpt
                or len(excerpt) > MAX_EXCERPT_CHARS):
            raise ValueError("Invalid excerpt in evidence reference")
    span = reference["span"]
    if span is not None:
        if (not isinstance(span, (list, tuple)) or len(span) != 2
                or any(type(bound) is not int for bound in span)
                or not 0 <= span[0] <= span[1]):
            raise ValueError("Invalid span in evidence reference")
        if excerpt is None or span[1] - span[0] != len(excerpt):
            raise ValueError("Span must bound exactly the excerpt")
    return reference


def evidence_reference(document, page, *, excerpt=None, span=None,
                       question_id=None, method, data_origin):
    """Build one validated evidence-reference record.

    The ``excerpt`` is accepted only when it is an EXACT substring of that
    page's extracted text (and matches ``span`` precisely when supplied);
    this makes fabricated spans structurally impossible. ``extraction_state``
    is copied from the page record, never invented here.
    """
    validate_document_evidence(document)
    if type(page) is not int or page < 1 or page > document["page_count"]:
        raise ValueError("page is outside the document")
    page_record = document["pages"][page - 1]
    if excerpt is not None:
        if not isinstance(excerpt, str) or not excerpt:
            raise ValueError("excerpt must be a non-empty string when present")
        source_text = page_record["text"]
        if excerpt not in source_text:
            raise ValueError(
                "excerpt is not an exact substring of the extracted text")
        location = source_text.find(excerpt)
        if span is None:
            span = [location, location + len(excerpt)]
        elif span[0] < 0 or span[1] > len(source_text) \
                or source_text[span[0]:span[1]] != excerpt:
            raise ValueError(
                "span does not bound the excerpt within the page text")
    reference = {
        "document_id": document["document_id"],
        "document_role": document["document_role"],
        "sha256": document["sha256"],
        "page": page,
        "excerpt": excerpt[:MAX_EXCERPT_CHARS]
        if excerpt is not None else None,
        "span": [span[0], span[1]] if span is not None else None,
        "question_id": question_id,
        "extraction_state": page_record["status"],
        "method": str(method),
        "data_origin": data_origin,
    }
    return validate_evidence_reference(reference)


def page_reference(document, page, *, method, data_origin, question_id=None):
    """Build a page-only evidence reference (no excerpt can be established)."""
    return evidence_reference(document, page, excerpt=None, span=None,
                              question_id=question_id, method=method,
                              data_origin=data_origin)
