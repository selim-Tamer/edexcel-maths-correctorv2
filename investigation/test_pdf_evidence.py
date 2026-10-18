"""Offline regression tests for Stage C3 PDF evidence extraction.

Investigation-only. No network, no AI API calls, no OCR, no student data.
Synthetic PDFs are built in-memory by hand (deterministic bytes) - none of
these fixtures represents real Pearson material.
"""
import unittest
from io import BytesIO
from unittest import mock

from investigation import pdf_evidence as evidence
from investigation.pdf_evidence import (
    DOCUMENT_ROLES, DOCUMENT_SCHEMA, DOCUMENT_STATUSES,
    PdfExtractionError, evidence_reference, extract_pdf_evidence,
    page_reference)
from investigation.pdf_test_fixtures import build_pdf


class ExtractionContractTests(unittest.TestCase):

    def test_single_page_mark_scheme_extracts_with_original_numbering(self):
        import hashlib
        data = build_pdf([["Q1 (a) M1: forms equation",
                           "Q1 (b) A1: x = 4"]])
        document = extract_pdf_evidence(data, "mark_scheme")
        self.assertEqual(document["schema"], DOCUMENT_SCHEMA)
        self.assertEqual(document["document_role"], "mark_scheme")
        self.assertEqual(document["page_count"], 1)
        self.assertEqual(
            [page["page"] for page in document["pages"]], [1])
        self.assertEqual(document["extraction_status"], "extracted")
        self.assertIn("Q1 (a) M1: forms equation",
                      document["pages"][0]["text"])
        self.assertEqual(document["sha256"], hashlib.sha256(data).hexdigest())

    def test_multi_page_scheme_preserves_page_order(self):
        line_sets = ([f"Q{n} (a) heading line"] for n in (1, 2, 3))
        document = extract_pdf_evidence(build_pdf(list(line_sets)), "mark_scheme")
        self.assertEqual([page["page"] for page in document["pages"]],
                         [1, 2, 3])
        for number, page in enumerate(document["pages"], start=1):
            self.assertIn(f"Q{number} (a)", page["text"])
            self.assertEqual(page["status"], "extracted")

    def test_question_paper_role_is_recorded(self):
        document = extract_pdf_evidence(build_pdf([["Question 1 total"]]),
                                        "question_paper")
        self.assertEqual(document["document_role"], "question_paper")
        self.assertNotIn("mark_scheme", document["extraction_status"])
        self.assertIn(document["document_role"], DOCUMENT_ROLES)

    def test_extracted_text_is_verbatim_not_repaired(self):
        # x2 stays x2; fractions stay flattened; exponents are NOT
        # re-interpreted: text extraction is not math transcription.
        document = extract_pdf_evidence(
            build_pdf([["x2 equals 8", "1/2 (67+1)/2 = 34"]]),
            "question_paper")
        text = document["pages"][0]["text"]
        self.assertIn("x2 equals 8", text)
        self.assertNotIn("x²", text)
        self.assertNotIn("x^2", text)
        self.assertIn("1/2 (67+1)/2 = 34", text)

    def test_empty_text_layer_is_a_limitation_not_a_verdict(self):
        document = extract_pdf_evidence(
            build_pdf([["real text"], None, []], path_only_pages=(1, 2)),
            "mark_scheme")
        page_two = document["pages"][1]
        page_three = document["pages"][2]
        self.assertEqual(page_two["status"], "empty_text_layer")
        self.assertEqual(page_two["text"], "")
        self.assertEqual(page_three["status"], "empty_text_layer")
        self.assertEqual(document["pages"][0]["status"], "extracted")
        self.assertIn(document["extraction_status"], DOCUMENT_STATUSES)
        self.assertTrue(any("extraction limitation" in warning
                            for warning in page_two["warnings"]))
        self.assertTrue(any("not a claim that the page is blank" in warning
                            for warning in page_two["warnings"]))

    def test_scanned_like_page_counts_image_objects_in_census(self):
        document = extract_pdf_evidence(
            build_pdf([None], image_only_pages=(0,)), "question_paper")
        census = document["pages"][0]["objects"]
        self.assertTrue(census, "census should be present")
        self.assertGreaterEqual(census["image"], 1)
        self.assertEqual(document["pages"][0]["status"], "empty_text_layer")

    def test_determinism_and_json_serializability(self):
        import json
        data = build_pdf([["Q1 (a) M1", "x2"], ["Q2 (a) A1"]])
        first = extract_pdf_evidence(data, "mark_scheme", document_id="D1")
        second = extract_pdf_evidence(data, "mark_scheme", document_id="D1")
        self.assertEqual(first, second)
        json.dumps(first)
        json.dumps(second)

    def test_document_id_defaults_to_digest(self):
        import hashlib
        data = build_pdf([["Q1"]])
        document = extract_pdf_evidence(data, "mark_scheme")
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(document["document_id"], f"pdf:{digest[:16]}")
        self.assertEqual(document["sha256"], digest)

    def test_stream_input_matches_bytes_input(self):
        data = build_pdf([["Q1 (a) M1"]])
        from_bytes = extract_pdf_evidence(data, "mark_scheme")
        from_stream = extract_pdf_evidence(BytesIO(data), "mark_scheme")
        self.assertEqual(from_bytes, from_stream)


class RejectionTests(unittest.TestCase):

    def test_corrupted_pdf_bytes_rejected_with_content_free_error(self):
        marker = b"private-student-synthetic-invalid-027b2"
        data = b"%PDF-1.4\n" + marker + b"\ntrailer junk"
        with self.assertRaises(PdfExtractionError) as caught:
            extract_pdf_evidence(data, "mark_scheme")
        self.assertNotIn("private-student", str(caught.exception),
                         "error must not echo document bytes")

    def test_not_a_pdf_input_rejected(self):
        for payload in (b"plain text is not a pdf",
                        b"PK\x03\x04 zip-shaped bytes"):
            with self.subTest(payload=payload[:20]), \
                    self.assertRaises(PdfExtractionError):
                extract_pdf_evidence(payload, "mark_scheme")

    def test_oversized_pdf_rejected(self):
        oversize_payload = b"%PDF-1.4" + b"\x00" * (
            evidence.MAX_DOCUMENT_BYTES + 1)
        for name, payload in (("bytes", oversize_payload),
                              ("stream", BytesIO(oversize_payload))):
            with self.subTest(kind=name), self.assertRaises(PdfExtractionError):
                extract_pdf_evidence(payload, "mark_scheme")

    def test_page_count_limit_rejected(self):
        data = build_pdf(
            [[f"Q{index} fill"] for index in range(evidence.MAX_PAGES + 1)])
        with self.assertRaises(PdfExtractionError) as caught:
            extract_pdf_evidence(data, "mark_scheme")
        self.assertIn("at most", str(caught.exception))

    def test_encrypted_pdf_rejected(self):
        import io as io_module
        from pypdf import PdfReader, PdfWriter
        source = PdfReader(io_module.BytesIO(build_pdf([["Q1 (a)"]])))
        writer = PdfWriter()
        writer.append(source)
        with io_module.BytesIO() as output:
            writer.encrypt("synthetic-only")
            writer.write(output)
            encrypted = output.getvalue()
        with self.assertRaises(PdfExtractionError):
            extract_pdf_evidence(encrypted, "mark_scheme")

    def test_invalid_roles_and_ids_rejected(self):
        data = build_pdf([["Q1 (a)"]])
        with self.assertRaises(ValueError):
            extract_pdf_evidence(data, "student_submission")
        with self.assertRaises(ValueError):
            extract_pdf_evidence(data, None)
        with self.assertRaises(ValueError):
            extract_pdf_evidence(data, "mark_scheme", document_id="")
        with self.assertRaises(ValueError):
            extract_pdf_evidence(data, "mark_scheme", document_id="x" * 101)

    def test_non_input_types_rejected(self):
        with self.assertRaises(PdfExtractionError):
            extract_pdf_evidence("not binary", "mark_scheme")
        with self.assertRaises(PdfExtractionError):
            extract_pdf_evidence(123, "mark_scheme")


class BudgetAndLayoutTests(unittest.TestCase):

    def test_text_budget_exhaustion_is_explicit_state(self):
        data = build_pdf([["Q1 text"], ["Q2 text"], ["Q3 text"]])
        with mock.patch.object(evidence, "MAX_TOTAL_TEXT_CHARS", 1):
            document = extract_pdf_evidence(data, "mark_scheme")
        self.assertEqual(document["pages"][0]["status"], "ambiguous_layout")
        self.assertEqual(document["pages"][0]["text"], "Q")
        statuses = [page["status"] for page in document["pages"][1:]]
        self.assertEqual(statuses, ["budget_exhausted", "budget_exhausted"])
        self.assertIn("budget", document["pages"][1]["warnings"][0])

    def test_oversized_text_page_is_ambiguous_layout(self):
        data = build_pdf([["A" * (evidence.MAX_PAGE_TEXT_CHARS + 1),
                           "tail line"]])
        document = extract_pdf_evidence(data, "mark_scheme")
        page = document["pages"][0]
        self.assertEqual(page["status"], "ambiguous_layout")
        self.assertLessEqual(len(page["text"]), evidence.MAX_PAGE_TEXT_CHARS)
        self.assertTrue(any("truncated" in warning
                            for warning in page["warnings"]))


class EvidenceReferenceContractTests(unittest.TestCase):

    def setUp(self):
        self.document = extract_pdf_evidence(
            build_pdf([["Q1 (a) M1: forms equation",
                        "Q1 (b) A1: x = 4"]]),
            "mark_scheme", document_id="doc-1")

    def test_reference_with_exact_excerpt_and_span(self):
        text = self.document["pages"][0]["text"]
        start = text.find("Q1 (b)")
        reference = evidence_reference(
            self.document, 1,
            excerpt=text[start:start + 10], span=[start, start + 10],
            question_id="Q1", method="heading_recognition",
            data_origin="pdf_extracted")
        self.assertEqual(reference["excerpt"], "Q1 (b) A1:")
        self.assertEqual(reference["page"], 1)
        self.assertEqual(reference["extraction_state"], "extracted")
        self.assertEqual(reference["document_role"], "mark_scheme")
        self.assertEqual(reference["data_origin"], "pdf_extracted")

    def test_reference_excerpt_must_be_verbatim_substring(self):
        with self.assertRaises(ValueError):
            evidence_reference(self.document, 1,
                               excerpt="Q1 (c) THE FAKE SPAN",
                               question_id="Q1", method="m",
                               data_origin="pdf_extracted")
        with self.assertRaises(ValueError):
            evidence_reference(self.document, 1,
                               excerpt="fabricated excerpt",
                               question_id="Q1", method="m",
                               data_origin="pdf_extracted")

    def test_reference_span_must_bound_the_excerpt(self):
        text = self.document["pages"][0]["text"]
        excerpt = text[2:12]
        with self.assertRaises(ValueError):
            evidence_reference(self.document, 1, excerpt=excerpt,
                               span=[2, 11],
                               question_id="Q1", method="m",
                               data_origin="pdf_extracted")

    def test_page_only_reference_has_no_excerpt(self):
        reference = page_reference(self.document, 1, method="caller",
                                   data_origin="caller_supplied",
                                   question_id="Q1")
        self.assertIsNone(reference["excerpt"])
        self.assertIsNone(reference["span"])
        self.assertEqual(reference["method"], "caller")
        self.assertEqual(reference["extraction_state"], "extracted")

    def test_reference_rejects_out_of_range_pages(self):
        with self.assertRaises(ValueError):
            page_reference(self.document, 5, method="m",
                           data_origin="pdf_extracted")
        with self.assertRaises(ValueError):
            page_reference(self.document, 0, method="m",
                           data_origin="pdf_extracted")

    def test_reference_validates_origins_and_methods(self):
        with self.assertRaises(ValueError):
            page_reference(self.document, 1, method="m",
                           data_origin="verified_everything")
        with self.assertRaises(ValueError):
            page_reference(self.document, 1, method="",
                           data_origin="pdf_extracted")
        for origin in evidence.DATA_ORIGINS:
            with self.subTest(origin=origin):
                page_reference(self.document, 1, method="k", data_origin=origin)

    def test_document_validator_rejects_tampered_records(self):
        import copy
        with self.assertRaises(ValueError):
            evidence.validate_document_evidence({"schema": "x"})
        tampered = copy.deepcopy(self.document)
        tampered["page_count"] = 5
        with self.assertRaises(ValueError):
            evidence.validate_document_evidence(tampered)
        tampered = copy.deepcopy(self.document)
        tampered["sha256"] = "nothex" * 8
        with self.assertRaises(ValueError):
            evidence.validate_document_evidence(tampered)
        tampered = copy.deepcopy(self.document)
        tampered["pages"][0]["page"] = 2
        with self.assertRaises(ValueError):
            evidence.validate_document_evidence(tampered)
        tampered = copy.deepcopy(self.document)
        tampered["pages"][0]["status"] = "unverified_magic"
        with self.assertRaises(ValueError):
            evidence.validate_document_evidence(tampered)


class ResourceSafetyTests(unittest.TestCase):

    def _patched_pdfium(self, page_textpage_raises):
        """Patch pdfium.PdfDocument so extraction hits our fail paths."""
        import pypdfium2 as pdfium_module
        from pypdf import PdfReader

        calls = {"open": 0, "close": 0}

        class FakeTextPage:
            def __init__(self, fail):
                self.fail = fail

            def count_chars(self):
                if self.fail:
                    raise RuntimeError("synthetic pdfium failure")
                return 4

            def get_text_range(self, start, count):
                if self.fail:
                    raise RuntimeError("synthetic pdfium failure")
                return "ABCD"

            def close(self):
                pass

        class FakePage:
            def __init__(self, fail):
                self.fail = fail

            def get_objects(self):
                return iter(())

            def get_textpage(self):
                return FakeTextPage(self.fail)

            def close(self):
                pass

        class FakeDocument:
            def __init__(self, data):
                calls["open"] += 1
                self.pages = [FakePage(False), FakePage(page_textpage_raises)]

            def __len__(self):
                return 2

            def __getitem__(self, index):
                return self.pages[index]

            def close(self):
                calls["close"] += 1

        return pdfium_module, FakeDocument, calls

    def test_document_closed_even_when_extraction_raises(self):
        pdfium_module, FakeDocument, calls = self._patched_pdfium(False)
        data = build_pdf([["Q1"], ["Q2"]])
        from pypdf import PdfReader
        self.assertEqual(
            len(PdfReader(BytesIO(data)).pages), 2,
            "fixture must satisfy the parser-parity check")
        with mock.patch.object(pdfium_module, "PdfDocument", FakeDocument):
            document = extract_pdf_evidence(data, "mark_scheme")
        self.assertEqual(calls["close"], 1)
        self.assertEqual(document["page_count"], 2)

    def test_page_failure_is_structured_not_raised(self):
        pdfium_module, FakeDocument, calls = self._patched_pdfium(True)
        data = build_pdf([["Q1"], ["Q2"]])
        with mock.patch.object(pdfium_module, "PdfDocument", FakeDocument):
            document = extract_pdf_evidence(data, "mark_scheme")
        self.assertEqual(calls["close"], 1)     # cleanup, even on failure
        self.assertEqual(document["extraction_status"],
                         "partial_extraction")
        self.assertEqual(document["pages"][0]["status"], "extracted")
        self.assertEqual(document["pages"][1]["status"], "extraction_failed")
        self.assertTrue(document["pages"][1]["warnings"])

    def test_repeated_failures_do_not_leak_lock_or_slots(self):
        pdfium_module, FakeDocument, calls = self._patched_pdfium(True)
        data = build_pdf([["Q1"], ["Q2"]])
        with mock.patch.object(pdfium_module, "PdfDocument", FakeDocument):
            for _ in range(20):
                extract_pdf_evidence(data, "mark_scheme")
        self.assertEqual(calls["open"], 20)
        self.assertEqual(calls["close"], 20)


class UnsupportedContentTests(unittest.TestCase):

    def test_embedded_action_is_flagged_not_executed(self):
        # Craft a catalog-level OpenAction (a Dests-free action dictionary).
        entries = [b"<< /Type /Catalog /Pages 2 0 R /OpenAction "
                   b"<< /Type /Action /S /JavaScript /JS (never executed) >>"
                   b" >>", b""]
        # minimal, manually built single-page body
        stream = b"BT /F1 12 Tf 20 780 Td (Q1 (a) text) Tj ET"
        entries.extend([
            (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 842] "
             b"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 "
             b"/BaseFont /Helvetica >> >> >> /Contents 3 0 R >>"),
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
            + stream + b"\nendstream"])
        entries[1] = (b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>")
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
        document = extract_pdf_evidence(data, "mark_scheme")
        self.assertTrue(any("OpenAction" in warning
                            for warning in document["warnings"]))
        self.assertEqual(document["extraction_status"],
                         "unsupported_content")
        self.assertTrue(document["pages"])    # text still extracted


class ModuleHygieneTests(unittest.TestCase):
    """Stage C3 modules must stay offline and free of code-execution hazards."""

    FORBIDDEN = (
        "import google", "from google", "import streamlit", "from streamlit",
        "import marking_requests", "from marking_requests",
        "import app5", "from app5", "import document_pipeline",
        "from document_pipeline", "import local_pdf", "from local_pdf",
        "import urllib", "from urllib", "import socket", "import requests",
        "from requests", "import subprocess", "from subprocess",
        "import fitz", "import pytesseract", "import pymupdf",
        "eval(", "exec(", "os.environ")

    MODULE_NAMES = ("pdf_evidence.py", "pdf_question_map.py",
                    "pdf_test_fixtures.py", "verification_evidence.py")

    def test_modules_import_nothing_forbidden(self):
        from pathlib import Path
        base = Path(__file__).parent.resolve()
        for name in self.MODULE_NAMES:
            source = (base / name).read_text(encoding="utf-8")
            for pattern in self.FORBIDDEN:
                self.assertNotIn(pattern, source,
                                 f"{name} contains {pattern!r}")

    def test_no_network_or_ai_calls_possible_from_extractors(self):
        import re
        from pathlib import Path
        base = Path(__file__).parent.resolve()
        for name in self.MODULE_NAMES:
            source = (base / name).read_text(encoding="utf-8")
            self.assertFalse(
                re.search(r"http[s]?://|api[_-]?key|token=|files\.upload",
                          source),
                f"{name} must not reference network endpoints")


if __name__ == "__main__":
    unittest.main()
