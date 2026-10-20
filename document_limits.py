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

Post-S6 hardening: deep PDF metadata inspection runs in a disposable child
process under a hard wall-clock deadline (`pdf_page_count`), and Drive
attachment downloads gain a whole-transfer deadline constant consumed by the
application's bounded downloader. Both additions preserve the content-free
error contract above.
"""
import io
import json
import os
import subprocess
import sys
import threading

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

# Aggregate whole-transfer budget for one Drive attachment download (S6-R4).
# The per-socket request timeout already bounds a stalled connection
# operation, but a deliberately slow trickle body could otherwise stay
# under the byte limit while occupying a marking worker indefinitely.
MAX_ATTACHMENT_DOWNLOAD_SECONDS = _limit_from_env(
    "MARKER_MAX_DOWNLOAD_SECONDS", 300, 30, 1800)

# Hard wall-clock deadline for one isolated PDF metadata parse (S6-R2).
# A malformed or hostile page tree can keep pypdf working far longer than
# any legitimate scanned exam paper; the parse therefore runs in a
# disposable child process that the parent terminates when this budget
# expires. There is no same-process "soft" timeout fallback in production:
# only the explicitly env-disabled isolation path is unbounded.
PDF_PARSE_TIMEOUT_SECONDS = _limit_from_env(
    "MARKER_PDF_PARSE_SECONDS", 20, 2, 300)

# Maximum simultaneous isolated parser children. Marking runs are serial
# (one student at a time), so daily traffic never approaches the bound; it
# also guarantees a batch of uploads can never multiply parser memory beyond
# this cap.
PDF_PARSE_MAX_CONCURRENT = _limit_from_env(
    "MARKER_MAX_PARSER_PROCESSES", 2, 1, 8)

# Parser isolation can be explicitly disabled for environment parity checks
# and sandbox validation suites. Production default remains isolated.
_parser_isolation_disabled = (
    (os.environ.get("MARKER_PDF_PARSER_ISOLATED", "1").strip() == "0"))

# Bounded global semaphore over child parses (created once; tests may attach
# their own permit source). Re-entrance through the module import is safe.
_parser_permits = threading.BoundedSemaphore(PDF_PARSE_MAX_CONCURRENT)

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
PDF_PARSE_TIMEOUT = "PDF_PARSE_TIMEOUT"
DOWNLOAD_TIMEOUT = "DOWNLOAD_TIMEOUT"

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
    PDF_PARSE_TIMEOUT: (
        "The document could not be inspected within the allowed time; "
        "please re-upload a fresh copy."),
    DOWNLOAD_TIMEOUT: (
        "The attachment download was slower than the supported limit; "
        "another attempt is usually enough."),
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

    The pypdf metadata walk is additionally executed inside a short-lived
    child process under a hard wall-clock deadline (S6-R2 finding): the
    parent terminates a stalled parser instead of waiting for a soft
    timeout that can never fire. Only the encrypted flag and a page count
    cross the process boundary — never document content or parser text.
    Set ``MARKER_PDF_PARSER_ISOLATED=0`` to fall back to same-process
    parsing (documented environment-parity escape hatch; then the deadline
    guarantee does not apply).
    """
    try:
        from pypdf import PdfReader  # noqa: F401 — fail closed when absent
    except ImportError as exc:  # fail closed, do not skip validation
        raise DocumentValidationError(INVALID_PDF) from exc
    if _parser_isolation_disabled:
        encrypted, pages = _in_process_pdf_limits(data)
    else:
        encrypted, pages = _isolated_pdf_limits(data)
    if encrypted:
        raise DocumentValidationError(ENCRYPTED_PDF)
    if pages > MAX_PDF_PAGES:
        raise DocumentValidationError(TOO_MANY_PAGES)
    return pages


def _in_process_pdf_limits(data):
    """Same-process parse retained for parity checks and explicit opt-out.

    No deadline applies here; callers opting into this path accept the
    residual S6-R2 risk (documented in the security register).
    """
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        encrypted = bool(reader.is_encrypted)
        pages = len(reader.pages)
    except Exception:
        raise DocumentValidationError(INVALID_PDF)
    return encrypted, int(pages)


def _child_parse_limits():
    """Isolated parser child — never executes inside the marking process.

    Streams the whole PDF from stdin, prints exactly one safe JSON line
    ({"ok": bool, "encrypted": bool, "pages": int}) to stdout and exits 0
    on success or 3 on parser failure. No document text, no file names and
    no parser exception text ever leave the child. Any escape — including
    ``MemoryError`` — is reported as an ordinary parser failure.
    """
    data = sys.stdin.buffer.read()
    ok, encrypted, pages = False, False, 0
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data), strict=False)
        encrypted = bool(reader.is_encrypted)
        pages_ok = False
        try:
            pages = len(reader.pages)
            pages_ok = True
        except Exception:
            if not encrypted:
                raise
        ok = True
    except BaseException:
        ok = False
    sys.stdout.write(json.dumps(
        {"ok": ok, "encrypted": encrypted, "pages": pages}) + "\n")
    sys.stdout.flush()
    raise SystemExit(0 if ok else 3)


def _child_parse_command():
    """Command line for the parser child (tests may substitute a hanging
    parser to exercise the deadline). Import-safe, no shell involved."""
    return [sys.executable, "-c",
            "from document_limits import _child_parse_limits; "
            "_child_parse_limits()"]


def _isolated_pdf_limits(data):
    """Parse PDF metadata in a disposable child process under a hard deadline.

    Returns ``(encrypted, pages)``. A child that outlives the configured
    parser budget is terminated and reaped, and the document is rejected
    content-free. A refused spawn (locked-down hosting) also fails closed:
    the document can never be verified, so it is never accepted implicitly.
    """
    timeout = PDF_PARSE_TIMEOUT_SECONDS
    here = os.path.dirname(os.path.abspath(__file__))
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = here + (os.pathsep + existing if existing else "")
    _parser_permits.acquire()
    try:
        try:
            result = subprocess.run(
                _child_parse_command(),
                input=bytes(data),
                capture_output=True,
                timeout=timeout,
                env=env,
                cwd=here,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            # subprocess.run has already terminated and reaped the child;
            # nothing parser-related survives the deadline.
            raise DocumentValidationError(PDF_PARSE_TIMEOUT)
        except OSError:
            raise DocumentValidationError(INVALID_PDF)
    finally:
        _parser_permits.release()
    if result.returncode != 0:
        raise DocumentValidationError(INVALID_PDF)
    try:
        payload = json.loads(
            result.stdout.splitlines()[0].decode("utf-8", "replace"))
        encrypted = bool(payload["encrypted"])
        pages = int(payload["pages"])
        if not payload["ok"]:
            raise ValueError("parser reported failure")
    except (ValueError, KeyError, IndexError, TypeError, UnicodeDecodeError):
        raise DocumentValidationError(INVALID_PDF)
    return encrypted, pages


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
