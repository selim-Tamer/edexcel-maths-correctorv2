"""Offline rounding/recurrence regressions using synthetic numeric evidence."""
import copy
import unittest
from support import load_app


class ArithmeticRoundingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f = load_app()

    def test_q66_truncated_intermediate_and_rounded_final(self):
        for text in ("1690 / 60 = 28.16... = 28.2", "1690 / 60 = 28.16… ≈ 28.2",
                     "1690 / 60 = 28.1666... = 28.17", "1690 / 60 ≈ 28.2"):
            with self.subTest(text=text):
                self.assertEqual(self.f["_arithmetic_equals_issues"](text), [])

    def test_bad_final_is_not_hidden_by_ellipsis(self):
        for text in ("1690 / 60 = 28.16... = 28.1", "1690 / 60 = 28.16… ≈ 28.3",
                     "1690 / 60 = 28.166... = 28.16"):
            with self.subTest(text=text):
                issues = self.f["_arithmetic_equals_issues"](text)
                self.assertEqual(len(issues), 1)
                self.assertIn(text.split()[-1], issues[0])

    def test_ellipsis_requires_correct_prefix_not_wider_rounding(self):
        for text in ("2 / 3 = 0.667...", "1 / 3 = 0.334…", "1690 / 60 = 28.17...",
                     "1 / 2 = 0.48...", "-1 / 2 = -0.48..."):
            with self.subTest(text=text):
                self.assertTrue(self.f["_arithmetic_equals_issues"](text))

    def test_recurring_decimal_notation(self):
        for text in ("1 / 3 = 0.(3)", "1 / 6 = 0.1(6) = 0.17",
                     "22 / 7 = 3.(142857) ≈ 3.14", "-1 / 3 = -0.(3)",
                     "2 / 3 = 0.666… ≈ 0.67"):
            with self.subTest(text=text):
                self.assertEqual(self.f["_arithmetic_equals_issues"](text), [])
        for text in ("1 / 3 = 0.(4)", "1 / 6 = 0.1(7)", "1 / 3 = 0.(3) = 0.34"):
            with self.subTest(text=text):
                self.assertTrue(self.f["_arithmetic_equals_issues"](text))

    def test_negative_truncation_and_rounding(self):
        for text in ("-1690 / 60 = -28.16... = -28.2", "-1 / 3 = -0.333... ≈ -0.33"):
            self.assertEqual(self.f["_arithmetic_equals_issues"](text), [])
        for text in ("-1690 / 60 = -28.17...", "-1 / 3 = -0.334..."):
            self.assertTrue(self.f["_arithmetic_equals_issues"](text))

    def test_approximate_operators_and_integer_rounding(self):
        for sign in ("≈", "≃", "~="):
            self.assertEqual(self.f["_arithmetic_equals_issues"](f"7 / 3 {sign} 2"), [])
            self.assertTrue(self.f["_arithmetic_equals_issues"](f"7 / 3 {sign} 3"))
        self.assertTrue(self.f["_arithmetic_equals_issues"]("7 / 3 = 2"))
        self.assertTrue(self.f["_arithmetic_equals_issues"]("2 + 2 ≈ 5"))

    def test_exact_origin_prevents_double_rounding(self):
        self.assertTrue(self.f["_arithmetic_equals_issues"]("1249 / 1000 = 1.25 = 1.3"))
        self.assertEqual(self.f["_arithmetic_equals_issues"]("1249 / 1000 = 1.25 = 1.2"), [])

    def test_completed_decimals_keep_precision_specific_tolerance(self):
        for text in ("1 / 6 = 0.17", "1 / 3 = 0.333", "28 * 72.6 = 2032.8?",
                     "1690 / 60 = 28.2."):
            self.assertEqual(self.f["_arithmetic_equals_issues"](text), [])
        for text in ("1 / 6 = 0.16", "1 / 3 = 0.334", "1690 / 60 = 28.16",
                     "0*5 + 1*6 + 2*2 + 3*3 + 4*2 = 29"):
            self.assertTrue(self.f["_arithmetic_equals_issues"](text))

    def test_precision_boundaries_use_exact_arithmetic(self):
        for text in ("1 / 3 = 0.333333333333333333...", "1 / 8 = 0.125 = 0.13",
                     "1 / 2 = 0.500...", "-1 / 2 = -0.500...", "1 = 0.(9)",
                     "1 = 0.999...", "-1 / 2 = -0.4999..."):
            self.assertEqual(self.f["_arithmetic_equals_issues"](text), [])
        for text in ("1 / 3 = 0.333333333333333334...", "1 / 8 = 0.125 = 0.14"):
            self.assertTrue(self.f["_arithmetic_equals_issues"](text))

    def test_arithmetic_errors_are_not_erased_by_later_correct_answer(self):
        self.assertTrue(self.f["_arithmetic_equals_issues"]("1690 / 60 = 27.16... = 28.2"))
        self.assertEqual(self.f["_arithmetic_equals_issues"]("Sum of 5 = 1 / 6 = 0.166… = 0.17"), [])
        self.assertEqual(self.f["_arithmetic_equals_issues"]("1 / 3 = 0.333...; 2 + 2 = 4"), [])
        self.assertTrue(self.f["_arithmetic_equals_issues"]("1 / 3 = 0.333...; 2 + 2 = 5"))

    def test_full_validators_accept_rounding_but_keep_wrong_final_and_marks(self):
        q = dict(question_number="66", max_marks=2, awarded_marks=2, confidence="High",
                 student_answer="28.2", scheme_answer_evidence="28.2",
                 correct_method="Divide the total by the frequency.",
                 full_mark_solution="1690 / 60 = 28.16... = 28.2",
                 working_summary="1690 / 60 = 28.16... = 28.2",
                 mark_breakdown=[dict(code="M1", awarded=True, reason="Division shown"),
                                 dict(code="A1", awarded=True, reason="Correct rounded answer")])
        for wrong in (False, True):
            question = copy.deepcopy(q)
            if wrong:
                question["student_answer"] = "28.1"
                question["working_summary"] = "1690 / 60 = 28.16... = 28.1"
            result = dict(questions=[question], validation_warnings=[])
            self.f["verify_math_explanations"](result)
            self.f["verify_awarded_mark_evidence"](result, {"66": "28.2"})
            self.assertEqual(bool(result["validation_warnings"]), wrong)
            self.assertEqual(question["awarded_marks"], 2)
            self.assertEqual(question["mark_breakdown"], q["mark_breakdown"])


if __name__ == "__main__":
    unittest.main()
