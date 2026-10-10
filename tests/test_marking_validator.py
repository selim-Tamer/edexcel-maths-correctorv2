"""Offline marking-validator regression tests. No Gemini key or student data required."""
import ast
import re
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "app5_1_automated.py"
NAMES = {
    "_numeric_expression_value",
    "_arithmetic_equals_issues",
    "_plain_numeric_answer_list",
    "_final_numeric_answer_for_part",
    "_numeric_answers_disagree",
    "verify_awarded_mark_evidence",
}

def load_validator_functions():
    # Extract pure validators without importing the interactive Streamlit app
    # (its top-level UI/OAuth setup is intentionally not invoked in CI).
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    functions = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in NAMES]
    found = {fn.name for fn in functions}
    missing = NAMES - found
    if missing:
        raise AssertionError(f"Missing validator functions: {sorted(missing)}")
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    scope = {"ast": ast, "re": re}
    exec(compile(module, str(SOURCE), "exec"), scope)
    return scope

F = load_validator_functions()

def question(student, solution, scheme, working="Found method and calculated result",
             awarded=3, max_marks=3, number="77"):
    return {
        "question_number": number, "awarded_marks": awarded, "max_marks": max_marks,
        "student_answer": student, "working_summary": working,
        "scheme_answer_evidence": scheme, "full_mark_solution": solution,
        "mark_breakdown": [{"code": "M1", "awarded": True,
                            "reason": "Student used the method shown"} for _ in range(awarded)],
        "confidence": "High",
    }

def warnings_for(q, independent=None):
    data = {"questions": [q], "validation_warnings": []}
    return F["verify_awarded_mark_evidence"](data, independent or {}).get("validation_warnings", [])

class ArithmeticRegressionTests(unittest.TestCase):
    def test_labels_are_not_equalities(self):
        for phrase in [
            "Sum of 5 = 5 * 398 = 1990",
            "Found Q1 = 6, Q3 = 51. IQR = 51 - 6 = 45",
            "Divided by 80 = 66.375, rounded to 66.4",
            "Sum of 4 = 13*4 = 52",
        ]:
            with self.subTest(phrase=phrase):
                self.assertEqual(F["_arithmetic_equals_issues"](phrase), [])

    def test_question_25_wrong_sum(self):
        result = F["_arithmetic_equals_issues"](
            "0×5 + 1×6 + 2×2 + 3×3 + 4×2 = 29"
        )
        self.assertTrue(result, "An incorrect total must be flagged")

    def test_question_25_correct_sum(self):
        self.assertEqual(F["_arithmetic_equals_issues"](
            "0×5 + 1×8 + 2×2 + 3×3 + 4×2 = 29"
        ), [])

    def test_fraction_and_intermediate_solution(self):
        answer = F["_final_numeric_answer_for_part"]
        comp = F["_numeric_answers_disagree"]
        self.assertFalse(comp(answer("(b) 764 / 52 = 14.7", "b"),
                              answer("(b) 14.7", "b")))
        self.assertFalse(comp(answer("(c) 24/52 = 6/13", "c"),
                              answer("(c) 6/13", "c")))

    def test_scientific_notation(self):
        answer = F["_final_numeric_answer_for_part"]
        comp = F["_numeric_answers_disagree"]
        self.assertFalse(comp(answer("(a) 35700000; (b) 2.6 × 10^3", "b"),
                              answer("(b) 2600", "b")))

class AwardEvidenceRegressionTests(unittest.TestCase):
    def test_question_77_full_credit_mismatch(self):
        q = question("83, 84, 64", "64, 73, 80", "64, 73, 80")
        issues = warnings_for(q)
        self.assertTrue(any("Full marks" in issue for issue in issues))
        self.assertEqual(q["awarded_marks"], 3, "Review warnings must not change marks")
        self.assertEqual(q["confidence"], "Low")

    def test_matching_answer_list_does_not_warn(self):
        q = question("64, 73, 80", "64, 73, 80", "64, 73, 80")
        self.assertFalse(warnings_for(q))

    def test_question_6_unknown_bound_is_not_unreadable(self):
        q = question(
            "(a) 22; (b) 5294; (c) don't know midpoints for XL, S",
            "(a) 22 (b) 5294", "Matches official scheme",
            working="(a) 55 / 150 * 60 = 22. (b) 68 * 48 + 58 * 35 = 5294. "
                    "(c) Open-ended groups have no midpoints",
            awarded=2, max_marks=2, number="6",
        )
        self.assertFalse(any("not reliably transcribed" in s for s in warnings_for(q)))

    def test_unreadable_student_answer_is_flagged(self):
        q = question("unreadable", "64, 73, 80", "64, 73, 80")
        self.assertTrue(warnings_for(q))

    def test_math_solution_disagreement_is_flagged(self):
        q = question("64, 73, 80", "64, 73, 80", "64, 73, 80",
                     working="Calculated result carefully")
        self.assertFalse(warnings_for(q))
        q["student_answer"] = "83, 84, 64"
        self.assertTrue(warnings_for(q))

if __name__ == "__main__":
    unittest.main()
