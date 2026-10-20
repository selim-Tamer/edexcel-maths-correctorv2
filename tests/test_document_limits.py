"""Stage S3 document resource limits: deterministic, offline validation matrix.

Covers the central limit module and the final transfer boundary with
synthetic bytes only. No provider traffic, no real documents.
"""
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import document_limits as dl
import document_pipeline as dp
from test_pdf_processing import synthetic_pdf
from pipeline_fakes import Clock, Provider


def encrypted_pdf():
    from pypdf import PdfWriter
    import io
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("secret")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class LimitConfigurationTests(unittest.TestCase):
    REPO_ROOT = Path(__file__).resolve().parents[1]

    def subprocess_env_limit(self, env_name, attribute, value, expected):
        """Fresh interpreter import with an env override, absolute cwd.

        The repository path is derived from this file, never from the
        runner's current directory: a relative ``cwd=".."`` once escaped the
        repository during CI discovery (run started at the repo root) and
        the import failed with an empty stdout. Child failures are surfaced
        with their stderr so an import error can never masquerade as a
        merely empty result, and the child inserts the repository path on
        ``sys.path`` itself so imports work no matter where it executes.
        """
        code = (
            "import os, sys\n"
            f"os.environ[{env_name!r}] = {value!r}\n"
            f"sys.path.insert(0, {str(self.REPO_ROOT)!r})\n"
            "import document_limits as d\n"
            f"print(d.{attribute} // (1024 * 1024))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, cwd=str(self.cwd))
        self.assertEqual(
            result.returncode, 0,
            f"subprocess failed (cwd={self.cwd}):\n{result.stderr}")
        self.assertEqual(result.stdout.strip(), expected)

    def test_defaults_are_safe_and_bounded(self):
        self.assertEqual(dl.MAX_DOCUMENT_BYTES, 25 * 1024 * 1024)
        self.assertEqual(dl.MAX_CLASSROOM_ATTACHMENT_BYTES, 25 * 1024 * 1024)
        self.assertEqual(dl.MAX_MANUAL_BATCH_TOTAL_BYTES, 150 * 1024 * 1024)
        self.assertEqual(dl.MAX_MANUAL_BATCH_FILES, 30)
        self.assertEqual(dl.MAX_PDF_PAGES, 150)
        self.assertEqual(dl.MAX_CLASSROOM_SUBMISSIONS_PER_RUN, 30)

    def test_env_override_is_clamped_from_any_working_directory(self):
        for cwd in (self.REPO_ROOT, Path(__file__).resolve().parent):
            with self.subTest(cwd=str(cwd)):
                self.cwd = cwd
                with self.subTest(case="above ceiling -> clamped"):
                    self.subprocess_env_limit(
                        "MARKER_MAX_DOCUMENT_MIB", "MAX_DOCUMENT_BYTES",
                        "10000", "256")
                with self.subTest(case="garbage -> default kept"):
                    self.subprocess_env_limit(
                        "MARKER_MAX_DOCUMENT_MIB", "MAX_DOCUMENT_BYTES",
                        "garbage", "25")
                with self.subTest(case="in range -> honoured"):
                    self.subprocess_env_limit(
                        "MARKER_MAX_DOCUMENT_MIB", "MAX_DOCUMENT_BYTES",
                        "35", "35")
                with self.subTest(case="attachment limit independent"):
                    self.subprocess_env_limit(
                        "MARKER_MAX_ATTACHMENT_MIB",
                        "MAX_CLASSROOM_ATTACHMENT_BYTES", "9", "9")


class SingleDocumentValidationTests(unittest.TestCase):
    def test_valid_synthetic_pdf_accepted_with_page_count(self):
        for pages in (1, 3):
            with self.subTest(pages=pages):
                self.assertEqual(
                    dl.validate_document(synthetic_pdf(pages), "work.pdf"),
                    {"kind": "pdf", "pages": pages})

    def test_empty_file_rejected(self):
        for data in (b"", None):
            with self.subTest(data=data):
                with self.assertRaisesRegex(
                        dl.DocumentValidationError, "no data"):
                    dl.validate_document(data, "work.pdf")

    def test_oversized_file_rejected(self):
        with self.assertRaisesRegex(dl.DocumentValidationError, "25 MB"):
            dl.validate_document(
                b"%PDF-1.4" + b"\x00" * (26 * 1024 * 1024), "work.pdf")

    def test_boundary_size_accepted_and_one_byte_more_rejected(self):
        tiny_pdf = synthetic_pdf(1)
        old = dl.MAX_DOCUMENT_BYTES
        try:
            dl.MAX_DOCUMENT_BYTES = len(tiny_pdf)
            self.assertEqual(dl.validate_document(tiny_pdf, "w.pdf")["pages"], 1)
            with self.assertRaises(dl.DocumentValidationError):
                dl.validate_document(tiny_pdf + b" ", "w.pdf")
        finally:
            dl.MAX_DOCUMENT_BYTES = old

    def test_non_pdf_disguised_with_pdf_extension_rejected(self):
        with self.assertRaisesRegex(
                dl.DocumentValidationError, "no PDF signature"):
            dl.validate_document(b"This is really a ZIP file", "paper.pdf")

    def test_mislabeled_other_extension_rejected(self):
        with self.assertRaisesRegex(
                dl.DocumentValidationError, "Unsupported"):
            dl.validate_document(synthetic_pdf(), "paper.txt")

    def test_corrupt_pdf_rejected(self):
        for data in (b"%PDF-1.4 garbage not a pdf",
                     b"%PDF-1.4" + b"\x00" * 300):
            with self.subTest(data=data):
                with self.assertRaisesRegex(
                        dl.DocumentValidationError, "malformed"):
                    dl.validate_document(data, "work.pdf")

    def test_encrypted_pdf_rejected(self):
        with self.assertRaisesRegex(
                dl.DocumentValidationError, "password-protected"):
            dl.validate_document(encrypted_pdf(), "work.pdf")

    def test_excessive_page_count_rejected(self):
        old = dl.MAX_PDF_PAGES
        try:
            dl.MAX_PDF_PAGES = 2
            with self.assertRaisesRegex(
                    dl.DocumentValidationError, "page count"):
                dl.validate_document(synthetic_pdf(3), "work.pdf")
            self.assertNotIn(synthetic_pdf(2).count(b"/Type /Page") - 1, [9])
            self.assertEqual(dl.validate_document(synthetic_pdf(2), "w.pdf")["pages"], 2)
        finally:
            dl.MAX_PDF_PAGES = old

    def test_image_roles(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
        jpeg = b"\xff\xd8\xff" + b"\x00" * 40
        webp = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 20
        for name, data in (("a.png", png), ("b.jpg", jpeg), ("c.webp", webp)):
            with self.subTest(name=name):
                self.assertEqual(
                    dl.validate_document(data, name, allow_images=True),
                    {"kind": "image", "pages": None})
        with self.assertRaisesRegex(
                dl.DocumentValidationError, "malformed"):
            dl.validate_document(b"GIF8" + b"\x00" * 40, "d.png", allow_images=True)

    def test_light_validation_skips_pypdf_but_keeps_structure_checks(self):
        # A signed-but-corrupt PDF passes the light boundary by design: the
        # deep layer owns semantic checks; the boundary layer is cheap.
        self.assertEqual(
            dl.validate_document(b"%PDF-anything", "w.pdf", deep=False),
            {"kind": "pdf", "pages": None})

    def test_validation_errors_are_content_free(self):
        student_named_file = " programme of 'Synthetic Student Ada' 2025.pdf"
        secrets_payload = b"%PDF-1.4 leaked-credential-SK-synthetic-999 " + b"x" * 50
        try:
            dl.validate_document(secrets_payload, student_named_file, deep=True)
        except dl.DocumentValidationError as err:
            text = str(err)
            self.assertNotIn("Ada", text)
            self.assertNotIn("leaked", text)
            self.assertNotIn("credential", text)
        else:
            self.fail("expected rejection")


class TransferBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.provider = Provider(self.clock, transfer=0, processing=0)

    def test_valid_pdf_transfers_and_empty_rejected(self):
        remote = dp.transfer(self.provider, synthetic_pdf(1), "w.pdf", clock=self.clock)
        self.assertTrue(remote.name)
        self.assertEqual(list(self.provider.data.values()), [synthetic_pdf(1)])

    def test_empty_and_mislabeled_and_oversized_rejected_at_boundary(self):
        for data, name in ((b"", "w.pdf"),
                           (b"plain text", "mislabeled.pdf"),
                           (b"%PDF-x", "big.pdf")):
            with self.subTest(name=name):
                self.clock = Clock()
                provider = Provider(self.clock, transfer=0, processing=0)
                if data == b"%PDF-x":
                    old = dl.MAX_DOCUMENT_BYTES
                    dl.MAX_DOCUMENT_BYTES = 2
                try:
                    with self.assertRaises(dl.DocumentValidationError):
                        dp.transfer(provider, data, name, clock=self.clock)
                    provider.files.upload.assert_not_called()
                    self.assertFalse(provider.files.get.called)
                finally:
                    if data == b"%PDF-x":
                        dl.MAX_DOCUMENT_BYTES = old

    def test_image_transfers_are_allowed_with_allow_images(self):
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
        remote = dp.transfer(self.provider, png, "work.png", clock=self.clock)
        self.assertTrue(remote.name)

    def test_pdf_signature_missing_never_touches_provider(self):
        with self.assertRaises(dl.DocumentValidationError):
            dp.transfer(self.provider, b"hello world", "w.pdf", clock=self.clock)
        self.provider.files.upload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
