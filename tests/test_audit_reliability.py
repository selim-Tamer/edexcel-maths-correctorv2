"""Synthetic responses exercise production parsing, validation and report code."""
import copy
import json
import unittest
from types import SimpleNamespace
from support import load_app, fake_client, remote, model_question, benchmark
import test_evidence_review as evidence_cases


class AuditReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.f = load_app()
        self.q = model_question(benchmark()["questions"][0], "correct")
        self.q["confidence"] = "Low"

    def run_audit(self, responses, models=None):
        client = fake_client(responses)
        before = {"questions": [copy.deepcopy(self.q)]}
        result, label = self.f["audit_result"](client, before, remote(), remote(), models or ["a", "b", "c"])
        self.assertEqual(result["questions"][0]["awarded_marks"], self.q["awarded_marks"])
        self.assertEqual(result["questions"][0]["mark_breakdown"], self.q["mark_breakdown"])
        self.assertNotIn("audit_review", before["questions"][0])
        return result["questions"][0]["audit_review"], client, label

    def test_q29_report_preserves_transcription_instead_of_fabricating_sum(self):
        text = "24 + 60 + 234 + 366 + 230 + 112 = 1040"
        self.assertEqual(self.f["_numeric_expression_value"](text.split("=")[0]), 1026)
        self.q["working_summary"] = text
        result = self.f["normalise_result"]({"questions": [self.q]})
        self.f["verify_awarded_mark_evidence"](result)
        self.assertTrue(result["validation_warnings"])
        report = self.f["build_markdown_report"]("Synthetic", result, dict(earned=2, available=2, percentage=100, provisional_grade="9"))
        self.assertIn(text, report)
        proposal = evidence_cases.EvidenceReviewTests().proposal("transcription_uncertainty", 2)
        proposal["proposed_working_summary"] = "24 + 80 + 234 + 360 + 230 + 112 = 1040"
        review, _, _ = self.run_audit([{"reviews": [proposal]}])
        self.assertEqual(review["status"], "proposal awaiting teacher review")
        self.assertEqual(self.q["working_summary"], text)

    def test_parentheses_and_precedence_are_preserved_and_checked(self):
        check = self.f["_arithmetic_equals_issues"]
        self.assertEqual(check("(67 + 1) / 2 = 34"), [])
        self.assertTrue(check("67 + 1 / 2 = 34"))
        self.assertTrue(check("(67 + 1) / 2 = 35"))
        self.assertEqual(check("((8 + 4) / 2) * 3 = 18"), [])
        self.q["working_summary"] = "(67 + 1) / 2 = 34"
        result = self.f["normalise_result"]({"questions": [self.q]})
        report = self.f["build_markdown_report"]("Synthetic", result, dict(earned=2, available=2, percentage=100, provisional_grade="9"))
        self.assertIn(self.q["working_summary"], report)

    def test_uncertain_grouping_triggers_audit_without_deducting_marks(self):
        self.q["transcription_uncertainty"] = "Synthetic fraction bar grouping is unclear."
        self.q["confidence"] = "High"
        r = self.f["normalise_result"]({"questions": [self.q]})
        self.f["check_question_marks"](r, {"1": 2})
        self.assertEqual(r["questions"][0]["confidence"], "Low")
        self.assertEqual(r["questions"][0]["awarded_marks"], 2)
        self.assertTrue(r["validation_warnings"])

    def test_json_fences_and_sdk_parsed_payload(self):
        payload = {"reviews": [evidence_cases.EvidenceReviewTests().proposal()]}
        for response in (SimpleNamespace(text="```json\n" + json.dumps(payload) + "\n```"),
                         SimpleNamespace(text=None, parsed=payload)):
            review, client, _ = self.run_audit([response])
            self.assertEqual(review["status"], "proposal awaiting teacher review")
            self.assertEqual(client.models.generate_content.call_count, 1)
            cfg = client.models.generate_content.call_args.kwargs["config"]
            self.assertEqual(cfg["http_options"]["retry_options"]["attempts"], 1)
            self.assertEqual(cfg["max_output_tokens"], 8192)
            required = cfg["response_json_schema"]["properties"]["reviews"]["items"]["required"]
            self.assertNotIn("proposed_awarded_marks", required)

    def test_minimal_unresolved_review_does_not_require_invented_marks(self):
        review, client, _ = self.run_audit([{"reviews": [{"question_number": "1", "classification": "unresolved", "reason": "Synthetic page illegible"}]}])
        self.assertEqual(review["status"], "unresolved")
        self.assertIn("illegible", review["source_review_reason"])
        self.assertNotIn("proposed_awarded_marks", review)
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_truncation_even_with_valid_partial_json_stops_without_retry(self):
        response = SimpleNamespace(text=json.dumps({"reviews": [evidence_cases.EvidenceReviewTests().proposal()]}),
                                   candidates=[SimpleNamespace(finish_reason="MAX_TOKENS")],
                                   usage_metadata=SimpleNamespace(candidates_token_count=8192, thoughts_token_count=1000))
        review, client, _ = self.run_audit([response])
        self.assertEqual(review["status"], "unresolved")
        self.assertEqual(review["diagnostics"][0]["outcome"], "truncated_response")
        self.assertEqual(review["diagnostics"][0]["candidates_token_count"], 8192)
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_invalid_json_fallback_is_bounded_and_deduplicated(self):
        review, client, _ = self.run_audit([SimpleNamespace(text="{bad"), SimpleNamespace(text="")], ["a", "a", "b", "c"])
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertEqual([x["outcome"] for x in review["diagnostics"]], ["invalid_json", "empty_response"])

    def test_wrong_shape_is_distinct_from_json_parse_failure(self):
        for text in ('[]', '{"corrections": []}', '{"reviews": {}}'):
            review, client, _ = self.run_audit([SimpleNamespace(text=text)])
            self.assertEqual(review["diagnostics"][0]["outcome"], "invalid_response_shape")
            self.assertEqual(client.models.generate_content.call_count, 1)

    def test_safe_diagnostics_never_include_provider_secrets_or_response_text(self):
        secret = "API_KEY_SECRET private student text"
        for status, expected in ((429, "quota_limited"), (400, "request_rejected"), (401, "authentication_failed"), (404, "model_unavailable")):
            error = RuntimeError(secret); error.code = status
            review, client, _ = self.run_audit([error, error])
            self.assertNotIn(secret, json.dumps(review))
            self.assertEqual(review["diagnostics"][0]["outcome"], expected)
            self.assertLessEqual(client.models.generate_content.call_count, 2 if status == 404 else 1)
        response = SimpleNamespace(text=secret, candidates=[SimpleNamespace(finish_reason="SAFETY")])
        review, _, _ = self.run_audit([response])
        self.assertNotIn(secret, json.dumps(review))
        self.assertEqual(review["diagnostics"][0]["outcome"], "blocked_or_incomplete_response")

    def test_cooldown_makes_no_request(self):
        self.f["model_on_quota_cooldown"] = lambda model: True
        review, client, _ = self.run_audit([])
        self.assertEqual(client.models.generate_content.call_count, 0)
        self.assertEqual(review["diagnostics"], [{"outcome": "all_models_on_cooldown"}])

    def test_timeout_does_not_repeat_an_ambiguous_request(self):
        for error in (TimeoutError("private details"), type("ReadTimeout", (Exception,), {})("private details")):
            review, client, _ = self.run_audit([error])
            self.assertEqual(review["diagnostics"][0]["outcome"], "timeout")
            self.assertEqual(client.models.generate_content.call_count, 1)

    def test_contradictory_or_missing_proposal_evidence_stays_unresolved(self):
        for key, value in (("proposed_full_mark_solution", "2 + 2 = 5"),
                           ("scheme_evidence", ""), ("student_evidence", "unreadable"),
                           ("proposed_working_summary", "67 + 1 / 2 = 34")):
            proposal = evidence_cases.EvidenceReviewTests().proposal("no_error", 2)
            proposal[key] = value
            review, _, _ = self.run_audit([{"reviews": [proposal]}])
            self.assertEqual(review["status"], "unresolved")
            self.assertEqual(review["validation_code"], "proposal_rejected")

    def test_partial_method_marks_survive_genuine_error_review(self):
        proposal = evidence_cases.EvidenceReviewTests().proposal("intermediate_error", 1)
        review, _, _ = self.run_audit([{"reviews": [proposal]}])
        self.assertTrue(review["proposed_mark_breakdown"][0]["awarded"])
        self.assertFalse(review["proposed_mark_breakdown"][1]["awarded"])
        self.assertEqual(review["proposed_awarded_marks"], 1)


if __name__ == "__main__":
    unittest.main()
