"""Central document resource limits for every upload path (Stage S3, finding F-03).

One source of truth for size, format, page-count and batch bounds so the
manual, Classroom, mark-scheme, question-paper, inspection and Gemini-transfer
paths cannot drift apart or bypass each other. Defaults are tuned for scanned
IGCSE papers (typically 2-10 MB and 12-40 pages per document) with generous
headroom, and stay far below the Gemini Files API per-file limit, so normal
student work is never rejected. Values can be tuned by environment variables
for review and testing; they are resource limits, NOT subscription credits.

Validation is deliberately content-free: error messages never include
filenames (which may contain student names), document text or raw parser
exceptions. Deep PDF checks use pypdf lazily with ``strict=False`` to obtain a
bounded page count and encryption status only — no page rendering, no script
or action extraction, no decompression beyond what pypdf performs to read the
page tree. If pypdf is unavailable the check fails closed rather than
silently uploading an unverifiable document.
"""
import io
import os

_MIB = 1024 * 1024


def _limit_from_env(name, default, minimum, maximum):
    """Read an integer environment override, clamped to safe bounds."""
    value = default
    raw = os.environ.get(name)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = default
    return max(minimum, min(value, maximum))


# Individual document cap (student PDFs, mark schemes, question papers,
# Classroom attachments). 25 MB ≈ 3-5x a typical scanned exam paper and
# comfortably below the 2 GB Gemini Files API limit.
MAX_DOCUMENT_BYTES = _limit_from_env("MARKER_MAX_DOCUMENT_MIB", 25, 1, 256) * _MIB

# Classroom attachments can be tuned independently if the school policies
# require a tighter bound on Drive-sourced documents.
MAX_CLASSROOM_ATTACHMENT_BYTES = _limit_from_env(
    "MARKER_MAX_ATTACHMENT_MIB", 25, 1, 256) * _MIB

# Aggregate manual batch bytes (all students + scheme + question paper).
MAX_MANUAL_BATCH_TOTAL_BYTES = _limit_from_env(
    "MARKER_MAX_BATCH_MIB", 150, 1, 512) * _MIB

# Documents per manual marking batch.
MAX_MANUAL_BATCH_FILES = _limit_from_env("MARKER_MAX_BATCH_FILES", 30, 1, 100)

# Bounded page count; a scanned exam paper never approaches this.
MAX_PDF_PAGES = _limit_from_env("MARKER_MAX_PDF_PAGES", 150, 1, 1000)

# Classroom submissions marked per run; any excess stays TURNED_IN and unmarked,
# remaining retryable on the next check instead of being silently discarded.
MAX_CLASSROOM_SUBMISSIONS_PER_RUN = _limit_from_env(
    "MARKER_MAX_SUBMISSIONS", 30, 1, 100)

# Per the PDF specification a file is still readable when junk precedes the
# header, and Acrobat scans the first 1024 bytes for its signature.
PDF_HEADER_WINDOW = 1024
PDF_SIGNATURE = b"%PDF-"

# Sanity-only magic checks for image attachments (no decoding, no rendering).
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JPEG_SIGNATURE = b"\xff\xd8\xff"
WEBP_PREFIX = b"RIFF"

# Stable, content-free validation categories. Values are part of the user
# contract and must never embed filenames, student names or document bytes.
EMPTY_DOCUMENT = "EMPTY_DOCUMENT"
DOCUMENT_TOO_LARGE = "DOCUMENT_TOO_LARGE"
UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
PDF_SIGNATURE_MISSING = "PDF_SIGNATURE_MISSING"
INVALID_PDF = "INVALID_PDF"
INVALID_IMAGE = "INVALID_IMAGE"
ENCRYPTED_PDF = "ENCRYPTED_PDF"
TOO_MANY_PAGES = "TOO_MANY_PAGES"
BATCH_TOO_MANY_FILES = "BATCH_TOO_MANY_FILES"
BATCH_TOO_LARGE = "BATCH_TOO_LARGE"

CODE_MESSAGES = {
    EMPTY_DOCUMENT: "The document contains no data.",
    DOCUMENT_TOO_LARGE: "The document exceeds the maximum supported size.",
    UNSUPPORTED_TYPE: "The uploaded file type is not supported.",
    PDF_SIGNATURE_MISSING: "The file is named as a PDF but has no PDF signature.",
    INVALID_PDF: "The PDF file is malformed and could not be read safely.",
    INVALID_IMAGE: "The image file is malformed and could not be read safely.",
    ENCRYPTED_PDF: "The PDF is password-protected; remove the password first.",
    TOO_MANY_PAGES: "The PDF exceeds the maximum supported page count.",
    BATCH_TOO_MANY_FILES: "The marking batch contains too many documents.",
    BATCH_TOO_LARGE: "The marking batch exceeds its total upload size.",
}


def humanize_bytes(limit):
    """Stable, non-sensitive size description for messages."""
    return f"{max(1, limit // _MIB)} MB"


class DocumentValidationError(Exception):
    """Content-free document rejection. Safe to display and to log.

    The message may mention only the stable category, the configured limit
    and the document count/size — never file names, student names, bytes or
    raw parser output.
    """

    def __init__(self, code, message=None):
        super().__init__(message or CODE_MESSAGES.get(code, "Document rejected."))
        self.code = code


def has_pdf_signature(data):
    if not isinstance(data, (bytes, bytearray)):
        return False
    return PDF_SIGNATURE in bytes(data[:PDF_HEADER_WINDOW])


def has_image_signature(data):
    if not isinstance(data, (bytes, bytearray)) or len(data) < 12:
        return False
    return (bytes(data[:8]) == PNG_SIGNATURE
            or bytes(data[:3]) == JPEG_SIGNATURE
            or (bytes(data[:4]) == WEBP_PREFIX and bytes(data[8:12]) == b"WEBP"))


def pdf_page_count(data):
    """Bounded pypdf metadata inspection: encryption flag and page count.

    Never renders pages, never extracts content beyond the page tree, and
    never runs any document-provided script. Raises ``DocumentValidationError``
    for encrypted or unreadable documents.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # fail closed, do not skip validation
        raise DocumentValidationError(INVALID_PDF) from exc
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise DocumentValidationError(ENCRYPTED_PDF)
        pages = len(reader.pages)
    except DocumentValidationError:
        raise
    except Exception as exc:
        raise DocumentValidationError(INVALID_PDF) from exc
    if pages > MAX_PDF_PAGES:
        raise DocumentValidationError(TOO_MANY_PAGES)
    return pages


def validate_document(data, filename=None, *, allow_images=False, deep=True,
                      max_bytes=None):
    """Validate one document before it may reach any provider transfer.

    ``deep=False`` performs only the cheap structural checks and is used at
    the final transfer boundary; ``deep=True`` additionally runs bounded
    pypdf metadata inspection. Returns ``{"kind": "pdf"|"image", "pages": n}``.
    """
    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        raise DocumentValidationError(EMPTY_DOCUMENT)
    limit = max_bytes or MAX_DOCUMENT_BYTES
    if len(data) > limit:
        raise DocumentValidationError(
            DOCUMENT_TOO_LARGE,
            f"The document exceeds the maximum supported size of "
            f"{humanize_bytes(limit)}.")
    suffix = os.path.splitext(filename or "")[1].lower()
    if allow_images and suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        if not has_image_signature(data):
            raise DocumentValidationError(INVALID_IMAGE)
        return {"kind": "image", "pages": None}
    if suffix and suffix != ".pdf":
        raise DocumentValidationError(
            UNSUPPORTED_TYPE,
            f"Unsupported submitted file type ({suffix}).")
    if not has_pdf_signature(data):
        raise DocumentValidationError(PDF_SIGNATURE_MISSING)
    pages = pdf_page_count(data) if deep else None
    return {"kind": "pdf", "pages": pages}
