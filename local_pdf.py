"""Bounded, opt-in local evidence inspection; never a replacement for marking PDFs.

PyMuPDF is imported lazily, and serialized because its document operations must
not be shared across threads. No document text/bytes are cached globally.
"""
from threading import Lock

_lock = Lock()


def inspect_pages(data, page_numbers):
    """Return explicit 1-based original pages, digital text, and a visual preview.

    Text extraction is NOT OCR or authoritative mathematical transcription.
    The excerpt is for teacher review only; callers must upload the original PDF.
    """
    if not isinstance(data, bytes) or len(data) > 50 * 1024 * 1024:
        raise ValueError("Local inspection supports PDFs up to 50 MB.")
    pages = list(page_numbers)
    if (not pages or len(pages) > 10 or len(set(pages)) != len(pages)
            or any(type(n) is not int or n < 1 for n in pages)):
        raise ValueError("Choose 1 to 10 distinct original page numbers.")
    import pymupdf
    with _lock:
        try:
            with pymupdf.open(stream=data, filetype="pdf") as source:
                if source.needs_pass or source.page_count > 2000:
                    raise ValueError("Unsupported document.")
                if max(pages) > source.page_count:
                    raise ValueError("Page outside document.")
                texts = []
                with pymupdf.open() as excerpt:
                    for n in pages:
                        page = source[n - 1]
                        # Bound returned text and page count; never log PDF contents.
                        texts.append({"page": n, "text": page.get_text("text", sort=True)[:20000]})
                        excerpt.insert_pdf(source, from_page=n - 1, to_page=n - 1,
                                           links=False, annots=True, widgets=True)
                    pdf = excerpt.tobytes()
                page = source[pages[0] - 1]
                longest = max(page.rect.width, page.rect.height)
                if longest <= 0:
                    raise ValueError("Invalid page dimensions.")
                preview = page.get_pixmap(matrix=pymupdf.Matrix(1200 / longest, 1200 / longest),
                                          alpha=False, annots=True).tobytes("png")
                return {"page_count": source.page_count, "pages": pages,
                        "text": texts, "pdf": pdf, "preview": preview}
        except Exception:
            # Parser errors can contain document text or paths. Keep the UI safe.
            raise ValueError("Could not inspect these pages. Check the PDF and page numbers.") from None
