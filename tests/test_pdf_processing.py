"""PDF byte transport and remote processing lifecycle; no OCR accuracy claims."""
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from support import fake_client, load_app, remote
from pipeline_fakes import Clock


def synthetic_pdf(pages=1):
    # A tiny valid PDF built entirely from original text; xref offsets are real.
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    kids = []
    for index in range(pages):
        page_id = len(objects) + 1
        kids.append(f"{page_id} 0 R")
        stream = b"BT /F1 12 Tf 20 100 Td (Synthetic maths: 2 + 2 = 4) Tj ET"
        objects.extend([
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
             f"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 "
             f"/BaseFont /Helvetica >> >> >> /Contents {page_id + 1} 0 R >>").encode(),
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        ])
    objects[1] = (f"<< /Type /Pages /Count {pages} /Kids [{' '.join(kids)}] >>").encode()
    data = b"%PDF-1.4\n"
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    data += (f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
             f"startxref\n{xref}\n%%EOF\n").encode()
    return data


class PdfProcessingTests(unittest.TestCase):
    def invoke(self, f, client, data, manual):
        if manual:
            return f["upload_pdf"](client, SimpleNamespace(name="synthetic.pdf", getvalue=lambda: data))
        return f["upload_bytes_to_gemini"](client, data, "synthetic.pdf")

    def test_manual_and_classroom_preserve_multipage_pdf_and_cleanup(self):
        for manual in (True, False):
            with self.subTest(manual=manual):
                f = load_app()
                client = fake_client()
                data = synthetic_pdf(3)
                paths = []
                def upload(*, file, config=None):
                    paths.append(file)
                    self.assertEqual(Path(file).read_bytes(), data)
                    return remote("PROCESSING")
                client.files.upload.side_effect = upload
                client.files.get.side_effect = [remote("PROCESSING"), remote("ACTIVE")]
                f["time"] = SimpleNamespace(monotonic=lambda: 0, sleep=Mock())
                self.assertEqual(self.invoke(f, client, data, manual).state.name, "ACTIVE")
                self.assertEqual(client.files.upload.call_count, 1)
                self.assertEqual(client.files.get.call_count, 2)
                self.assertEqual(f["time"].sleep.call_count, 2)
                self.assertTrue(all(not os.path.exists(p) for p in paths))

    def test_processing_failure_timeout_and_transport_error_cleanup(self):
        for manual in (True, False):
            for failure in ("FAILED", "timeout", "upload error"):
                with self.subTest(manual=manual, failure=failure):
                    f = load_app()
                    client = fake_client()
                    paths = []
                    def upload(*, file, config=None):
                        paths.append(file)
                        if failure == "upload error":
                            raise RuntimeError("synthetic transport error")
                        return remote("PROCESSING" if failure == "timeout" else "FAILED")
                    client.files.upload.side_effect = upload
                    f["time"] = Clock()
                    if failure == "timeout":
                        client.files.get.side_effect = lambda **kwargs: remote("PROCESSING")
                    with self.assertRaises((RuntimeError, TimeoutError)):
                        self.invoke(f, client, synthetic_pdf(), manual)
                    self.assertTrue(paths)
                    self.assertTrue(all(not os.path.exists(p) for p in paths))
                    if failure == "timeout":
                        self.assertEqual(f["time"].monotonic(), 180)
                        self.assertLessEqual(client.files.get.call_count, 92)
                    else:
                        client.files.get.assert_not_called()

    def test_empty_pdf_rejected_before_upload_and_corrupt_pdf_rejected_locally(self):
        import document_limits
        # Empty file: rejected at the transfer boundary, no provider or temp use.
        for manual in (True, False):
            with self.subTest(data="empty", manual=manual):
                f = load_app()
                client = fake_client()
                f["time"] = Clock()
                with self.assertRaises(document_limits.DocumentValidationError):
                    self.invoke(f, client, b"", manual)
                client.files.upload.assert_not_called()
                self.assertFalse(client.files.get.called)
        # Signed-but-corrupt PDF: rejected by the deep validation layer used
        # by the manual pre-flight and Classroom attachment checks.
        with self.subTest(data="corrupt"):
            with self.assertRaisesRegex(
                    document_limits.DocumentValidationError, "malformed"):
                document_limits.validate_document(b"%PDF-corrupt", "synthetic.pdf", deep=True)
