"""Synthetic contract tests, NOT an estimate of handwriting/model accuracy."""
import copy
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from support import benchmark, fake_client, load_app, model_question, remote, response


class MarkingPipelineTests(unittest.TestCase):
    def setUp(self):
        self.f = load_app()
        self.cases = benchmark()["questions"]

    def run_manual(self, questions, **kw):
        client = fake_client([{"questions": questions}])
        maxima = {q["question_number"]: q["max_marks"] for q in questions}
        answers = {q["question_number"]: q["scheme_answer_evidence"] for q in questions}
        options = dict(selected_questions=list(maxima), scheme_maxima=maxima,
                       scheme_answers=answers)
        options.update(kw)
        row = self.f["mark_one_submission"](
            client, "Synthetic", remote(), remote(name="scheme"), None, False, **options)
        return row, client

    def test_24_questions_four_response_variants_and_fixed_allocations(self):
        self.assertGreaterEqual(len(self.cases), 20)
        self.assertEqual(len({c["id"] for c in self.cases}), len(self.cases))
        for variant, expected_total in [("correct", 48), ("partial", 24),
                                        ("incorrect", 0), ("blank", 0)]:
            with self.subTest(variant=variant):
                questions = [model_question(c, variant) for c in self.cases]
                row, client = self.run_manual(questions)
                self.assertEqual(row["totals"]["earned"], expected_total)
                self.assertEqual(row["totals"]["available"], 48)
                self.assertEqual(row["totals"]["percentage"], expected_total / 48 * 100)
                self.assertFalse(row["result"]["validation_warnings"])
                actual = {q["question_number"]: q["awarded_marks"]
                          for q in row["result"]["questions"]}
                for case in self.cases:
                    self.assertEqual(len(case["allocation"]), case["max_marks"])
                    self.assertEqual(actual[case["id"]],
                                     case["responses"][variant]["expected_marks"])
                self.assertEqual(client.models.generate_content.call_count, 1)
                client.files.delete.assert_not_called()

    def test_false_full_credit_is_detected_for_every_wrong_synthetic_answer(self):
        for case in self.cases:
            with self.subTest(question=case["id"]):
                q = model_question(case, "incorrect")
                q["awarded_marks"] = 2
                row, _ = self.run_manual([q])
                self.assertTrue(any("Full marks" in w
                                    for w in row["result"]["validation_warnings"]))
                self.assertEqual(row["totals"]["earned"], 2,
                                 "Warnings must not silently change awards")
                self.assertTrue(row["result"]["grade_unverified"])

    def test_blank_awarded_marks_requires_review(self):
        q = model_question(self.cases[0], "blank")
        q["awarded_marks"] = 2
        row, _ = self.run_manual([q])
        self.assertTrue(any("clearly transcribed" in w
                            for w in row["result"]["validation_warnings"]))

    def test_missing_selected_question_is_reported(self):
        q = model_question(self.cases[0], "correct")
        row, _ = self.run_manual([q], selected_questions=["1", "2"])
        self.assertTrue(any("question 2 is missing" in w
                            for w in row["result"]["validation_warnings"]))

    def test_full_paper_missing_question_is_reported(self):
        q = model_question(self.cases[0], "correct")
        row, _ = self.run_manual([q], selected_questions=None,
                                 scheme_maxima={"1": 2, "2": 2})
        self.assertTrue(any("question 2 is missing" in w
                            for w in row["result"]["validation_warnings"]))

    def test_selection_excludes_unselected_questions(self):
        questions = [model_question(c, "correct") for c in self.cases[:2]]
        row, _ = self.run_manual(questions, selected_questions=["2"])
        self.assertEqual([q["question_number"] for q in row["result"]["questions"]], ["2"])
        self.assertEqual(row["totals"]["earned"], 2)

    def test_empty_selection_result_fails(self):
        with self.assertRaisesRegex(ValueError, "selected homework questions"):
            self.run_manual([model_question(self.cases[0], "correct")],
                            selected_questions=["999"])

    def test_maximum_disagreement_and_duplicates_do_not_rewrite_marks(self):
        q = model_question(self.cases[0], "correct")
        row, _ = self.run_manual([q, copy.deepcopy(q)], scheme_maxima={"1": 3})
        warnings = row["result"]["validation_warnings"]
        self.assertTrue(any("Duplicate" in w for w in warnings))
        self.assertTrue(any("AI maximum" in w for w in warnings))
        self.assertEqual(row["totals"]["earned"], 4)
        self.assertTrue(row["result"]["max_marks_review_required"])

    def test_q49_self_correction_products_and_combined_totals(self):
        examples = [
            ("Wait, actually the answer is 4", "self-correction"),
            ("Products: 138, 252, 144, 245, 100, 51; sum = 960", "frequency products"),
            ("Total for class A = 500; overall = 900; class B mean = 450/10",
             "group totals"),
        ]
        for working, expected in examples:
            with self.subTest(working=working):
                q = model_question(self.cases[0], "correct")
                q.update(question_number="49", working_summary=working)
                row, _ = self.run_manual([q])
                self.assertTrue(any(expected in w for w in row["result"]["validation_warnings"]))
                self.assertEqual(row["totals"]["earned"], 2)

    def test_q49_full_credit_feedback_contradiction(self):
        q = model_question(self.cases[0], "correct")
        q["loss_reason"] = "The student's answer is incorrect."
        row, _ = self.run_manual([q])
        self.assertTrue(any("AI awarded full marks" in w
                            for w in row["result"]["validation_warnings"]))

    def test_multimark_breakdown_and_mismatched_evidence(self):
        q = model_question(self.cases[0], "correct")
        q["mark_breakdown"] = [{"code": "M2", "awarded": True, "reason": "Shown method"}]
        row, _ = self.run_manual([q])
        self.assertFalse(row["result"]["validation_warnings"])
        q["mark_breakdown"][0]["code"] = "M1"
        row, _ = self.run_manual([q])
        self.assertTrue(any("accounts for 1 marks" in w
                            for w in row["result"]["validation_warnings"]))

    def test_json_fences_prose_and_malformed_response(self):
        for text in ['{"questions":[]}', '~~~', '', 'prefix {"questions":[]} suffix',
                     chr(96)*3 + 'json\n{"questions":[]}\n' + chr(96)*3]:
            with self.subTest(text=text):
                if text in ("~~~", ""):
                    with self.assertRaises(ValueError):
                        self.f["extract_json"](text)
                else:
                    self.assertEqual(self.f["extract_json"](text), {"questions": []})

    def test_normalisation_bounds_and_empty_totals(self):
        result = self.f["normalise_result"]({"questions": [
            {"max_marks": 2, "awarded_marks": 999},
            {"max_marks": -4, "awarded_marks": -1},
            {"max_marks": "bad", "awarded_marks": None}, None]})
        self.assertEqual([(q["awarded_marks"], q["max_marks"])
                          for q in result["questions"]], [(2, 2), (0, 0), (0, 0)])
        self.assertEqual(self.f["calculate_totals"]({"questions": []}), (0, 0, 0.0))

    def test_offline_performance_budget(self):
        questions = [model_question(c, "correct") for c in self.cases]
        started = time.perf_counter()
        requests = 0
        for _ in range(100):
            row, client = self.run_manual(copy.deepcopy(questions))
            self.assertEqual(row["totals"]["earned"], 48)
            requests += client.models.generate_content.call_count
        elapsed = time.perf_counter() - started
        print(json.dumps({"benchmark": "100 x 24 synthetic questions",
                          "seconds": round(elapsed, 4), "mock_generation_calls": requests,
                          "live_requests": 0}))
        self.assertEqual(requests, 100)
        self.assertLess(elapsed, 15, "Offline CPU budget; not live Gemini latency")


class GeminiBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.f = load_app()

    def grade(self, client):
        return self.f["grade_with_model"](client, ["fake-a", "fake-b"],
                                         remote(), remote(name="scheme"))

    def test_quota_fallback_has_no_same_model_retry_or_sleep(self):
        client = fake_client([RuntimeError("429 perday quota exceeded"), {"questions": []}])
        clock = SimpleNamespace(monotonic=lambda: 100, sleep=Mock())
        self.f["time"] = clock
        self.assertEqual(self.grade(client)[1], "fake-b")
        self.assertEqual([c.kwargs["model"] for c in client.models.generate_content.call_args_list],
                         ["fake-a", "fake-b"])
        clock.sleep.assert_not_called()
        self.assertTrue(self.f["model_on_quota_cooldown"]("fake-a"))

    def test_cooldown_skips_model_and_expires(self):
        self.f["time"] = SimpleNamespace(monotonic=lambda: 100)
        self.f["remember_model_failure"]("fake-a", RuntimeError("429 quota exceeded"))
        client = fake_client([{"questions": []}])
        self.assertEqual(self.grade(client)[1], "fake-b")
        self.assertEqual(client.models.generate_content.call_count, 1)
        self.f["time"] = SimpleNamespace(monotonic=lambda: 191)
        self.assertFalse(self.f["model_on_quota_cooldown"]("fake-a"))

    def test_all_models_failing_is_bounded(self):
        client = fake_client([RuntimeError("503 unavailable"), RuntimeError("503 unavailable")])
        with self.assertRaisesRegex(RuntimeError, "All configured"):
            self.grade(client)
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_malformed_response_falls_back_once(self):
        client = fake_client([SimpleNamespace(text="not JSON"), {"questions": []}])
        self.assertEqual(self.grade(client)[1], "fake-b")
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_scheme_conflicts_removed_and_unknown_maxima_not_invented(self):
        client = fake_client([{"questions": [
            {"question_number": "1", "max_marks": 2, "answer_summary": "4"},
            {"question_number": "1", "max_marks": 3, "answer_summary": "5"},
            {"question_number": "2", "max_marks": 2, "answer_summary": "5"},
            {"question_number": "3", "max_marks": "unknown"}]}])
        maxima, answers, warning = self.f["extract_scheme_maxima"](client, remote())
        self.assertEqual(maxima, {"2": 2})
        self.assertEqual(answers, {"2": "5"})
        self.assertIsNone(warning)
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_high_confidence_audit_makes_no_request(self):
        result = {"questions": [model_question(benchmark()["questions"][0], "correct")]}
        client = fake_client()
        audited, model = self.f["audit_result"](client, result, remote(), remote(), ["fake"])
        self.assertEqual(audited, result)
        self.assertIn("Skipped", model)
        client.models.generate_content.assert_not_called()

    def test_scalar_audit_cannot_silently_change_marks(self):
        q = model_question(benchmark()["questions"][0], "partial")
        q["confidence"] = "Low"
        original = {"questions": [q]}
        client = fake_client([{"corrections": [
            {"question_number": "1", "awarded_marks": 2, "reason": "Visible final answer"},
            {"question_number": "999", "awarded_marks": 50, "reason": "Ignore"}]}])
        result, _ = self.f["audit_result"](client, original, remote(), remote(), ["fake"])
        self.assertEqual(len(result["questions"]), 1)
        self.assertEqual(result["questions"][0]["awarded_marks"], 1)
        self.assertEqual(result["questions"][0]["audit_review"]["status"], "unresolved")
        self.assertEqual(original["questions"][0]["awarded_marks"], 1)
