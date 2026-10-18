"""Synthetic offline PDF fixtures for Phase C Stage 3 evidence tests.

Investigation-only. No student data, no OCR, no network. Deterministic
bytes are built by hand (real xref offsets, Helvetica base font) so the
tests do not need ReportLab and cannot drift with library versions. Every
fixture is deliberately tiny; none claims to resemble real Pearson
documents beyond exercising extraction paths.
"""
import json

MAX_FIXTURE_PAGES = 2100

__all__ = [
    "escape_pdf_text", "build_pdf", "image_only_page_bytes",
    "FIXTURE_SCHEME_PAGE_TEXTS", "FIXTURE_PAPER_PAGE_TEXTS",
    "empty_text_layer_page", "fixture_document_ids",
]


def escape_pdf_text(text):
    """Escape a string for a PDF literal; no Unicode remapping is applied."""
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _content_stream(lines, path_only=False, image_name=None):
    operations = []
    if path_only:
        # Non-text visible content (a line), so an empty text layer is a
        # genuine extraction limitation, not an empty page.
        operations.append(b"0.5 w 10 10 m 150 50 l S")
    if image_name is not None:
        operations.append(b"q 595 0 0 842 0 0 cm /%s Do Q" % image_name.encode())
    for index, line in enumerate(lines):
        operations.append(
            b"BT /F1 12 Tf 40 %d Td (%s) Tj ET"
            % (792 - 24 * index, escape_pdf_text(line).encode("latin-1")))
    return b"\n".join(operations)


def _object(entries, kids):
    entries[1] = (f"<< /Type /Pages /Count {len(kids)} "
                  f"/Kids [{' '.join(kids)}] >>").encode()
    data = b"%PDF-1.4\n"
    offsets = [0]
    for number, entry in enumerate(entries, start=1):
        offsets.append(len(data))
        data += f"{number} 0 obj\n".encode() + entry + b"\nendobj\n"
    xref = len(data)
    data += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{offset:010d} 00000 n \n".encode()
                     for offset in offsets[1:])
    data += (f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
             f"startxref\n{xref}\n%%EOF\n").encode()
    return data


def build_pdf(page_texts, path_only_pages=(), image_only_pages=()):
    """Build a deterministic multi-page PDF from per-page line lists.

    ``page_texts`` is a list of line lists (page order = list order).
    ``path_only_pages`` indexes of pages drawn with a vector line only.
    ``image_only_pages`` indexes of pages carrying a tiny embedded grayscale
    image instead of text (a stand-in for a scanned page).

    Object numbering is computed in construction order: page dictionary,
    content stream and (when present) that page's image XObject are placed
    consecutively, so references are always valid.
    """
    pages = []            # (kids entry built after other ids known)
    entries = [b"<< /Type /Catalog /Pages 2 0 R >>", None]
    kids = []
    for index, lines in enumerate(page_texts):
        page_id = len(entries) + 1
        entries.append(None)          # placeholder for the page dictionary
        content_id = page_id + 1
        entries.append(None)          # placeholder for the content stream
        kids.append(f"{page_id} 0 R")
        image_name = f"Im{index}" if index in image_only_pages else None
        image_id = None
        if image_name is not None:
            image_id = len(entries) + 1
            bitmap = image_only_page_bytes()
            image_stream = (f"<< /Type /XObject /Subtype /Image /Width 4 "
                            f"/Height 4 /ColorSpace /DeviceGray "
                            f"/BitsPerComponent 8 /Length {len(bitmap)} "
                            f">>\nstream\n").encode() + bitmap \
                + b"\nendstream"
            entries.append(image_stream)
        stream = _content_stream(lines if lines is not None else [],
                                 path_only=index in path_only_pages,
                                 image_name=image_name)
        resources = ("/Resources << /Font << /F1 << /Type /Font /Subtype "
                     "/Type1 /BaseFont /Helvetica >> >>")
        if image_name is not None:
            resources += f" /XObject << /{image_name} {image_id} 0 R >>"
        resources += " >>"
        entries[page_id - 1] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 210 842] "
            f"{resources} /Contents {content_id} 0 R >>").encode()
        entries[content_id - 1] = (
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream + b"\nendstream")
    return _object(entries, kids)


def image_only_page_bytes():
    """Bitmap stream for scanned-page fixtures: 4x4 grayscale, 8 bpc."""
    return bytes([0x00, 0x3C, 0xA5, 0xFF] * 4)


def empty_text_layer_page():
    return []           # with path_only_pages


FIXTURE_SCHEME_PAGE_TEXTS = [
    # Page 1: two questions; method and award criteria in scheme style.
    ["Q1 (a) M1: forms 2x + 3 = 11",
     "Q1 (b) A1: x = 4",
     "Q2 (a) B1: requires 5 marks total"],
    # Page 2: continuation of Q2 with no repeated heading, plus Q3.
    ["alternative M1: expands correctly"],
    # Page 3: Q3 heading.
    ["Q3 (c) A1: completes the table"],
]

FIXTURE_PAPER_PAGE_TEXTS = [
    ["Question 1 total for this question: 2 marks",
     "Solve 2x + 3 = 11",
     "Question 2 total for this question: 5 marks"],
    ["Question 3 total for this question: 4 marks",
     "(b) another subpart row"],
]


def fixture_document_ids(document):
    """Return the deterministic ids used in Stage 3 test documents."""
    return document["document_id"], document["sha256"]


if __name__ == "__main__":
    # Manual offline check only: prints fixture sizes, writes nothing.
    sample = build_pdf([["Q1 heading"], ["continuation"]])
    print(json.dumps({"bytes": len(sample), "pages": 2}))
