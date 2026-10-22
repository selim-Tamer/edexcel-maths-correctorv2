"""Phase C5 evaluation-harness tests (investigation-only, offline).

Checks that the synthetic scheme-review eval set (a) runs deterministically,
(b) detects every declared expected finding on each defect case, (c) stays
silent on the clean case, (d) escalates every defective scheme to teacher
review while leaving the bound clean case at CHECKED, (e) always flags
alternative-method groups for human review instead of summing them, and
(f) reports what the pre-C5-style baseline could and could not see. No
network, no Gemini calls, no student data.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from investigation import scheme_review_eval as eval_module


class EvalCaseSetTests(unittest.TestCase):
    def test_case_set_loads_with_expected_envelope(self):
        cases = eval_module.load_cases()
        self.assertGreaterEqual(len(cases), 10)
        ids = [case["id"] for case in cases]
        self.assertEqual(len(ids), len(set(ids)))
        clean = [case for case in cases if case.get("clean")]
        defect = [case for case in cases if not case.get("clean")]
        self.assertGreaterEqual(len(clean), 1)
        self.assertGreaterEqual(len(defect), 5)

    def test_case_payloads_stay_within_review_bounds(self):
        for case in eval_module.load_cases():
            self.assertLessEqual(len((case.get("payload") or {})
                                     .get("questions", [])),
                                 200, case["id"])


class EvalMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary = eval_module.evaluate()

    def test_every_case_expectation_passes(self):
        self.assertTrue(self.summary["summary_ok"])
        self.assertEqual(self.summary["cases_passed"],
                         self.summary["cases_total"])

    def test_expected_recognition_is_complete(self):
        self.assertEqual(self.summary["expected_rules_detected"],
                         self.summary["expected_rules_total"])
        self.assertGreater(self.summary["expected_rules_total"], 10)

    def test_clean_case_produces_no_findings(self):
        self.assertEqual(self.summary["clean_cases_flagged"], 0)
        self.assertEqual(self.summary["false_positive_rate"], 0.0)

    def test_escalation_is_correct_on_every_case(self):
        self.assertEqual(self.summary["escalation_correct"],
                         self.summary["cases_total"])

    def test_alternative_groups_never_summed(self):
        self.assertGreaterEqual(self.summary["alt_cases_total"], 1)
        self.assertEqual(self.summary["alt_cases_flagged"],
                         self.summary["alt_cases_total"])

    def test_baseline_comparison_is_reported(self):
        self.assertEqual(self.summary["defect_cases_detected"],
                         self.summary["defect_cases"])
        # Most defect types pre-dated C5 with no deterministic check, so the
        # comparison must show cases the naive baseline cannot reach.
        self.assertGreater(self.summary["baseline_beyond_count"], 5)

    def test_baseline_stays_silent_on_clean_case(self):
        for result in self.summary["case_results"]:
            if result["clean"]:
                self.assertEqual(result["baseline_rule_ids"], [])

    def test_results_are_deterministic(self):
        again = eval_module.evaluate()
        self.assertEqual(json.dumps(self.summary, sort_keys=True),
                         json.dumps(again, sort_keys=True))

    def test_baseline_finding_shape_records_pre_c5_view(self):
        result = {entry["id"]: entry
                  for entry in self.summary["case_results"]}
        self.assertEqual(result["defect_total_mismatch"]["baseline_rule_ids"],
                         ["baseline-total-mismatch"])
        self.assertEqual(result["defect_marks_negative"]["baseline_rule_ids"],
                         ["baseline-max-invalid"])
        self.assertEqual(
            result["defect_dependency_cycle"]["baseline_rule_ids"], [])

    def test_baseline_finds_nothing_it_hears_about_only(self):
        # The baseline description documents its limited scope.
        self.assertIn("positive integer max_marks",
                      eval_module.BASELINE_DESCRIPTION)


class BaselineUnitTests(unittest.TestCase):
    def test_baseline_flags_negative_and_mismatch_only(self):
        payload = {"questions": [
            {"question_number": "1", "max_marks": -1, "criteria": []},
            {"question_number": "2", "max_marks": 3, "criteria": [
                {"criterion_id": "Q2-c1", "code": "M1", "marks": 2,
                 "dependencies": [], "follow_through": None,
                 "alternative_group": None, "source_page": None,
                 "source_evidence": None, "note": None}]},
            {"question_number": "3", "max_marks": 2, "criteria": [
                {"criterion_id": "Q3-c1", "code": "B1", "marks": 1,
                 "dependencies": [], "follow_through": None,
                 "alternative_group": None, "source_page": None,
                 "source_evidence": None, "note": None}]},
        ]}
        self.assertEqual(eval_module.baseline_findings(payload),
                         ["baseline-max-invalid", "baseline-total-mismatch"])

    def test_baseline_ignores_non_integer_criteria(self):
        payload = {"questions": [
            {"question_number": "1", "max_marks": 2, "criteria": [
                {"criterion_id": "Q1-c1", "code": "M1", "marks": None,
                 "dependencies": [], "follow_through": None,
                 "alternative_group": None, "source_page": None,
                 "source_evidence": None, "note": None}]},
        ]}
        self.assertEqual(eval_module.baseline_findings(payload), [])


class MainEntryPointTests(unittest.TestCase):
    def test_main_returns_zero_when_expectations_hold(self):
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(eval_module.main([]), 0)


if __name__ == "__main__":
    unittest.main()
