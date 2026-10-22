"""Offline tests for the Phase C5 synthetic review benchmark.

The benchmark itself is synthetic and local: no network, no credentials, no
official mark-scheme material and no student data. These tests keep the
fixture honest (every case carries expectations) and keep the public
benchmark result at full agreement.
"""
import ast
import json
import unittest

from investigation import scheme_review_benchmark as benchmark


class BenchmarkTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(benchmark.CASES_PATH, encoding="utf-8") as handle:
            cls.fixture = json.load(handle)
        cls.results, cls.summary = benchmark.run_benchmark()

    def test_fixture_declares_its_synthetic_scope(self):
        self.assertEqual(self.fixture["schema"], benchmark.CASES_SCHEMA)
        self.assertIn("NOT official Pearson", self.fixture["provenance"])
        self.assertIn("no network", self.fixture["provenance"])
        self.assertGreaterEqual(len(self.fixture["cases"]), 20)

    def test_every_case_has_a_description_and_expectations(self):
        for case in self.fixture["cases"]:
            with self.subTest(case=case["id"]):
                self.assertTrue(case["description"].strip())
                self.assertIn("review_status", case["expect"])
                self.assertIn("provider_status", case["expect"])
                self.assertIn("questions", case)
                self.assertIn("claims", case)

    def test_benchmark_matches_every_expectation(self):
        self.assertTrue(self.summary["all_matched"],
                        self.summary["mismatches"])
        self.assertEqual(self.summary["cases"], len(self.fixture["cases"]))
        self.assertEqual(self.summary["status_accuracy"], 1.0)
        self.assertEqual(self.summary["finding_recall"], 1.0)
        self.assertEqual(self.summary["finding_precision"], 1.0)
        self.assertEqual(self.summary["false_positives"], 0)
        self.assertEqual(self.summary["mapping_correct"], 1.0)
        self.assertEqual(self.summary["total_consistency"], 1.0)
        self.assertEqual(self.summary["c2_preservation"], 1.0)
        self.assertTrue(self.summary["provider"]["bounded"])

    def test_required_metric_groups_are_present(self):
        for metric in (
                "clean_control", "missing_criterion", "dependency",
                "alternative", "mapping", "evidence", "approval",
                "provider", "escalation", "c2_preservation"):
            with self.subTest(metric=metric):
                self.assertIn(metric, self.summary["groups"])
                self.assertGreater(self.summary["groups"][metric]["cases"], 0)

    def test_escalation_states_are_explicit(self):
        self.assertEqual(
            set(self.summary["escalation"]),
            {"CHECKED", "NEEDS_TEACHER_REVIEW", "UNREVIEWED"})
        for bucket in self.summary["escalation"].values():
            self.assertEqual(bucket["accuracy"], 1.0)

    def test_provider_cases_are_bounded_and_never_assume_absence(self):
        for item in self.results:
            with self.subTest(case=item["case_id"]):
                self.assertTrue(item["checks"]["provider_bounded"])
                status = item["actual"]["provider_status"]
                if status not in (
                        "not_requested", "completed",
                        "completed_with_rejections"):
                    self.assertIn(
                        "C5-PROVIDER-REVIEW-UNAVAILABLE",
                        item["actual"]["c5_rules"])

    def test_result_is_deterministic(self):
        _, again = benchmark.run_benchmark()
        self.assertEqual(again, self.summary)

    def test_benchmark_module_has_no_network_or_credential_imports(self):
        with open(benchmark.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for module in imported:
            with self.subTest(module=module):
                self.assertIn(
                    module.split(".")[0], {"json", "os", "investigation"})
        with open(benchmark.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("GEMINI_API_KEY", "getpass", "api_key"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
