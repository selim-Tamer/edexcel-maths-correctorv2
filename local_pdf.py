"""Bounded, opt-in local evidence inspection; never a replacement for marking PDFs.

pypdf copies page objects; PDFium extracts digital text and renders the preview.
PDFium calls are serialized across sessions (its native API is not thread-safe).
No document text/bytes are cached globally. Imports are lazy.
"""
from io import BytesIO
from contextlib import closing
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
    from pypdf import PdfReader, PdfWriter
    import pypdfium2 as pdfium
    with _lock:
        try:
            with closing(PdfReader(BytesIO(data), strict=True)) as reader, pdfium.PdfDocument(data) as source:
                if reader.is_encrypted:
                    raise ValueError("Encrypted document.")
                source.init_forms()
                count = len(source)
                if count > 2000 or len(reader.pages) != count:
                    raise ValueError("Unsupported document.")
                if max(pages) > count:
                    raise ValueError("Page outside document.")
                texts = []
                preview = None
                for n in pages:
                    with closing(source[n - 1]) as page:
                        with closing(page.get_textpage()) as textpage:
                            # Bound extraction before converting native text to Python.
                            text = textpage.get_text_range(0, min(20000, textpage.count_chars()))
                            texts.append({"page": n, "text": text})
                        if preview is None:
                            longest = max(page.get_size())
                            if longest <= 0:
                                raise ValueError("Invalid page dimensions.")
                            with closing(page.render(scale=1200 / longest, draw_annots=True)) as bitmap:
                                with bitmap.to_pil() as image, BytesIO() as output:
                                    image.save(output, format="PNG", compress_level=1)
                                    preview = output.getvalue()
                # append preserves form fields and annotations, unlike add_page alone.
                # Keep vector/text/image streams; never rasterize the excerpt.
                with closing(PdfWriter()) as writer, BytesIO() as output:
                    writer.append(reader, pages=[n - 1 for n in pages], import_outline=False,
                                  excluded_fields=["/B"])
                    writer.write(output)
                    pdf = output.getvalue()
                return {"page_count": count, "pages": pages,
                        "text": texts, "pdf": pdf, "preview": preview}
        except Exception:
            # Parser errors can contain document text or paths. Keep the UI safe.
            raise ValueError("Could not inspect these pages. Check the PDF and page numbers.") from None
