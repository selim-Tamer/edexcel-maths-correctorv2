"""Production-boundary and security checks for the Phase C5 reviewer.

C5 is investigation-only. These tests assert that the reviewer cannot be
reached from the marking app, imports nothing that can make a live provider
call, never publishes or alters student marks, and keeps provider output as a
bounded proposal.
"""
import ast
import json
import unittest
from pathlib import Path

from investigation import scheme_review as review

ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = ROOT / "app5_1_automated.py"
C5_SOURCE = ROOT / "investigation" / "scheme_review.py"
SHA = "a" * 64

FORBIDDEN_MODULES = {
    "requests", "httpx", "socket", "urllib", "http", "aiohttp",
    "google", "google.generativeai", "google.genai", "openai",
    "streamlit", "app5_1_automated", "marking_requests", "gemini_limits",
}
FORBIDDEN_CALLS = {
    "open", "eval", "exec", "compile", "__import__", "urlopen",
    "getpass", "input",
}


def _document(pages):
    return {
        "schema": "investigation.pdf_evidence/1",
        "document_id": "mark_scheme-doc",
        "document_role": "mark_scheme",
        "sha256": SHA,
        "page_count": len(pages),
        "extraction_status": "extracted",
        "warnings": [],
        "pages": [
            {
                "page": index,
                "text": text,
                "status": "extracted",
                "warnings": [],
                "objects": None,
            }
            for index, text in enumerate(pages, start=1)
        ],
    }


def _payload():
    return {
        "schema": "investigation.scheme_review/1",
        "specification": "synthetic boundary fixture",
        "assignment": {
            "assignment_id": "assignment-1",
            "paper_id": "4MA1-1H",
            "session": "synthetic",
            "revision": 1,
            "scheme_sha256": SHA,
        },
        "reviewer_id": "teacher:1",
        "proposed_scheme": {
            "provenance": {"author": "synthetic", "verified": False},
            "questions": [{
                "id": "Q1",
                "total": 1,
                "criteria": [{"id": "Q1-c1", "code": "M1", "weight": 1}],
            }],
        },
        "criterion_claims": {
            "Q1-c1": {
                "part": "(a)",
                "mark_type": "M",
                "marks": 1,
                "extraction_confidence": "high",
                "source": {
                    "document_role": "mark_scheme",
                    "page": 1,
                    "excerpt": "M1: correct method",
                },
            },
        },
        "source_documents": {
            "mark_scheme": _document(["Q1 M1: correct method"]),
        },
        "question_mapping": None,
        "teacher_decisions": [],
    }


class ProductionBoundaryTests(unittest.TestCase):

    def test_marking_app_never_imports_the_c5_reviewer(self):
        source = APP_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("investigation.scheme_review", source)
        self.assertNotIn("investigation import scheme_review", source)

    def test_c5_module_is_investigation_only(self):
        source = C5_SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for module in imported:
            with self.subTest(module=module):
                self.assertFalse(
                    module.split(".")[0] in FORBIDDEN_MODULES,
                    f"C5 must not import {module}")
                self.assertTrue(
                    module in {"copy", "hashlib", "json", "re"}
                    or module.startswith("investigation."),
                    f"unexpected C5 import {module}")

    def test_c5_module_has_no_file_io_or_dynamic_execution(self):
        tree = ast.parse(C5_SOURCE.read_text(encoding="utf-8"))
        used = {
            node.func.id for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
        }
        self.assertFalse(used & FORBIDDEN_CALLS,
                         sorted(used & FORBIDDEN_CALLS))

    def test_result_contract_is_fixed_and_excludes_student_marks(self):
        result = review.review_scheme(_payload())
        self.assertEqual(
            set(result),
            {"schema", "specification", "assignment", "extracted",
             "verified", "provider_review", "teacher", "safeguards"})
        text = json.dumps(result).lower()
        for forbidden in (
                "student_name", "student_id", "grade_published",
                "awarded_marks", "final_grade", "classroom_grade"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)
        self.assertEqual(
            result["safeguards"],
            {
                "official_scheme_authoritative": True,
                "provider_findings_are_proposals": True,
                "publishes_grades": False,
                "affects_student_marks": False,
                "requires_teacher_confirmation": True,
                "live_provider_calls": False,
            })

    def test_provider_is_optional_and_at_most_one_request(self):
        calls = []

        def provider(packet):
            calls.append(packet)
            return {"findings": []}

        result = review.review_scheme(_payload(), provider=provider)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["provider_review"]["request_count"], 1)
        self.assertEqual(result["provider_review"]["max_requests"], 1)
        self.assertEqual(review.MAX_PROVIDER_REQUESTS, 1)

    def test_proposal_cannot_add_criteria_change_totals_or_certify(self):
        proposal = {
            "findings": [{
                "category": "CRITERION_MISSING",
                "question_id": "Q1",
                "criterion_ids": [],
                "explanation": "Synthetic proposal only.",
                "confidence": "high",
                "suggested_action": "Confirm against the official scheme.",
                "source": {
                    "document_role": "mark_scheme",
                    "page": 1,
                    "excerpt": "M1: correct method",
                },
            }],
        }
        baseline = review.review_scheme(_payload())
        result = review.review_scheme(
            _payload(), provider=lambda packet: proposal)
        self.assertEqual(
            result["extracted"]["proposed_scheme"],
            baseline["extracted"]["proposed_scheme"])
        self.assertEqual(
            result["verified"]["effective_totals"],
            baseline["verified"]["effective_totals"])
        self.assertEqual(
            result["verified"]["c2_findings"],
            baseline["verified"]["c2_findings"])
        self.assertNotIn(
            result["provider_review"]["findings"][0]["finding_id"],
            {finding["finding_id"]
             for finding in result["verified"]["c5_findings"]})
        finding = result["provider_review"]["findings"][0]
        self.assertEqual(finding["origin"], "gemini_proposal")
        self.assertFalse(finding["blocks_approval"])
        self.assertTrue(finding["acknowledgement_required"])
        self.assertEqual(result["teacher"]["review_status"],
                         "NEEDS_TEACHER_REVIEW")

    def test_provider_failure_never_leaks_its_message(self):
        def provider(packet):
            raise RuntimeError("PRIVATE student answer 4")

        result = review.review_scheme(_payload(), provider=provider)
        self.assertEqual(result["provider_review"]["status"], "unavailable")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertIn("C5-PROVIDER-REVIEW-UNAVAILABLE",
                      [finding["rule_id"]
                       for finding in result["verified"]["c5_findings"]])

    def test_anonymous_review_neither_calls_provider_nor_approves(self):
        payload = _payload()
        payload["reviewer_id"] = None
        calls = []

        def provider(packet):
            calls.append(packet)
            return {"findings": []}

        result = review.review_scheme(payload, provider=provider)
        self.assertEqual(calls, [])
        self.assertEqual(result["provider_review"]["status"],
                         "anonymous_refused")
        self.assertEqual(result["teacher"]["review_status"], "UNREVIEWED")
        self.assertFalse(result["teacher"]["approval_valid"])

    def test_result_is_json_bounded_and_round_trips(self):
        result = review.review_scheme(_payload())
        encoded = json.dumps(result, sort_keys=True)
        self.assertEqual(json.loads(encoded), result)
        self.assertLessEqual(len(encoded), review.MAX_RESULT_CHARS)


if __name__ == "__main__":
    unittest.main()
