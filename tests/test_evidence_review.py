"""Synthetic review contracts; no student documents or live service calls."""
import copy
import json
import unittest
from support import benchmark, fake_client, load_app, model_question, remote


class EvidenceReviewTests(unittest.TestCase):
    def setUp(self):
        self.f = load_app()
        self.q = model_question(benchmark()["questions"][0], "correct")
        self.q["confidence"] = "Low"

    def proposal(self, classification="final_answer_error", total=1):
        return dict(question_number="1", classification=classification,
                    reason="Check the original source before applying any change.",
                    question_evidence="Synthetic question p1: evaluate the sum.",
                    student_evidence="Synthetic answer p1: method shown, final answer differs.",
                    scheme_evidence="Synthetic scheme p1: M1 method; A1 correct answer.",
                    proposed_awarded_marks=total,
                    proposed_working_summary="2 + 2 = 5",
                    proposed_full_mark_solution="2 + 2 = 4",
                    proposed_mark_breakdown=[
                        dict(criterion_id="method", code="M1", awarded=True, reason="Addition shown, scheme allows method credit."),
                        dict(criterion_id="answer", code="A1", awarded=total == 2, reason="Compare final answer with scheme.")])

    def audited(self, reviews, **kwargs):
        client = fake_client([{"reviews": reviews}])
        original = {"questions": [copy.deepcopy(self.q)], "validation_warnings": ["Synthetic contradiction"]}
        saved = copy.deepcopy(original)
        result, model = self.f["audit_result"](client, original, remote(), remote(), ["fake"], **kwargs)
        self.assertEqual(original, saved)
        self.assertEqual(result["questions"][0]["awarded_marks"], self.q["awarded_marks"])
        self.assertEqual(result["questions"][0]["mark_breakdown"], self.q["mark_breakdown"])
        return result, client

    def test_decimal_question_mark_never_truncates_number(self):
        for tail in ("?", "? Wait", ".", ";", "!", ""):
            with self.subTest(tail=tail):
                self.assertEqual(self.f["_arithmetic_equals_issues"]("28 * 72.6 = 2032.8" + tail), [])
                self.assertTrue(self.f["_arithmetic_equals_issues"]("28 * 72.6 = 2031.8" + tail))

    def test_intermediate_transcription_and_feedback_are_not_final_answer_errors(self):
        # Minimal arithmetic reproductions, not copies of student reports.
        for field, text in (("full_mark_solution", "0*5 + 1*6 + 2*2 + 3*3 + 4*2 = 29"),
                            ("working_summary", "24 + 60 + 234 + 366 + 250 + 112 = 1040"),
                            ("working_summary", "33 + 46 + 42 + 45 = 160")):
            with self.subTest(field=field, text=text):
                q = copy.deepcopy(self.q)
                q[field] = text
                r = self.f["normalise_result"]({"questions": [q]})
                self.f["verify_math_explanations"](r)
                self.f["verify_awarded_mark_evidence"](r)
                warnings = " ".join(r["validation_warnings"])
                self.assertTrue(warnings)
                self.assertNotIn("Final-answer disagreement", warnings)
                self.assertEqual(r["questions"][0]["awarded_marks"], q["awarded_marks"])

    def test_genuine_final_mismatch_requires_source_check_and_preserves_method(self):
        self.q.update(student_answer="5", scheme_answer_evidence="4", full_mark_solution="4")
        r = self.f["normalise_result"]({"questions": [self.q]})
        self.f["verify_awarded_mark_evidence"](r)
        self.assertIn("Final-answer disagreement", " ".join(r["validation_warnings"]))
        self.assertEqual(r["questions"][0]["mark_breakdown"], self.q["mark_breakdown"])
        result, _ = self.audited([self.proposal()])
        review = result["questions"][0]["audit_review"]
        self.assertEqual(review["classification"], "final_answer_error")
        self.assertTrue(review["proposed_mark_breakdown"][0]["awarded"])
        self.assertFalse(review["proposed_mark_breakdown"][1]["awarded"])

    def test_audit_receives_all_evidence_and_optional_question_file(self):
        question = remote(name="question")
        result, client = self.audited([self.proposal()], question_file=question, scheme_answers={"1": "4"})
        contents = client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn(question, contents)
        prompt = contents[-1]
        for text in ("Synthetic contradiction", "full_mark_solution", "scheme_answer_evidence", "independent_scheme_answers"):
            self.assertIn(text, prompt)
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_review_classifications_keep_original_score_and_evidence(self):
        for kind in ("feedback_error", "transcription_uncertainty", "intermediate_error", "final_answer_error", "no_error"):
            with self.subTest(kind=kind):
                result, _ = self.audited([self.proposal(kind)])
                review = result["questions"][0]["audit_review"]
                self.assertEqual(review["classification"], kind)
                self.assertEqual(review["status"], "proposal awaiting teacher review")

    def test_incomplete_duplicate_unknown_and_empty_reviews_stay_unresolved(self):
        bad = []
        for key, value in (("scheme_evidence", ""), ("proposed_awarded_marks", 99),
                           ("proposed_awarded_marks", True), ("proposed_mark_breakdown", []),
                           ("classification", "unresolved")):
            p = self.proposal(); p[key] = value; bad.append([p])
        p = self.proposal(); p["proposed_mark_breakdown"][1]["criterion_id"] = "method"; bad.append([p])
        p = self.proposal(); p["proposed_awarded_marks"] = 2; bad.append([p])
        p = self.proposal(); p["question_number"] = "999"; bad.append([p])
        bad.extend([[], [self.proposal(), self.proposal()]])
        for reviews in bad:
            with self.subTest(reviews=reviews):
                result, _ = self.audited(reviews)
                self.assertEqual(result["questions"][0]["audit_review"]["status"], "unresolved")

    def test_failed_audit_is_bounded_and_persists_unresolved_review(self):
        client = fake_client([RuntimeError("unavailable"), RuntimeError("unavailable")])
        result, model = self.f["audit_result"](client, {"questions": [self.q]}, remote(), remote(), ["a", "b"])
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertIn("Failed", model)
        self.assertEqual(result["questions"][0]["audit_review"]["status"], "unresolved")

    def test_normalisation_revalidation_and_exports_keep_review(self):
        result, _ = self.audited([self.proposal()])
        result = self.f["normalise_result"](result)
        self.f["check_question_marks"](result, {"1": 2})
        self.f["verify_math_explanations"](result)
        self.f["verify_awarded_mark_evidence"](result)
        self.assertTrue(self.f["review_required"](result))
        report = self.f["build_markdown_report"]("Synthetic", result, dict(earned=2, available=2, percentage=100, provisional_grade="9"))
        self.assertLess(report.index("UNVERIFIED"), report.index("Score: 2 / 2"))
        self.assertIn("proposal awaiting teacher review", report)
        self.assertIn("Synthetic scheme p1", report)
        csv = self.f["build_csv_rows"]("Synthetic", result)[0]
        self.assertEqual(csv["Review Status"], "Teacher review required")
        self.assertIn("Proposed awarded marks: 1", csv["Audit Review"])

    def test_neutral_student_wrote_phrase_is_not_transcription_uncertainty(self):
        self.q["working_summary"] = "Student wrote the addition clearly: 2 + 2 = 4"
        result = self.f["normalise_result"]({"questions": [self.q]})
        self.f["verify_awarded_mark_evidence"](result)
        self.assertFalse(result["validation_warnings"])

    def test_missing_scheme_evidence_is_not_a_final_answer_error(self):
        self.q["scheme_answer_evidence"] = ""
        result = self.f["normalise_result"]({"questions": [self.q]})
        self.f["verify_awarded_mark_evidence"](result)
        self.assertIn("Insufficient marking evidence", " ".join(result["validation_warnings"]))
        self.assertNotIn("Final-answer disagreement", " ".join(result["validation_warnings"]))


if __name__ == "__main__":
    unittest.main()
