"""Offline tests for the Stage C4 synthetic mathematical benchmark.

Verifies: every expected outcome matches through the public engine, the
benchmark is deterministic and JSON-serialisable, it has enough coverage
breadth, and it shares no data with any existing benchmark or marking
policy source.
"""
import json
import unittest
from pathlib import Path

from investigation.math_benchmark import BENCHMARK_SCHEMA, run_benchmark
from investigation.math_verification import OUTCOMES


class BenchmarkOutcomeTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.results, cls.summary = run_benchmark()

    def test_the_benchmark_has_at_least_twenty_cases(self):
        self.assertGreaterEqual(len(self.results), 20)
        self.assertEqual(self.summary["cases"], len(self.results))

    def test_every_outcome_matches_its_expectation(self):
        self.assertTrue(self.summary["outcome_matches"])
        self.assertEqual(self.summary["mismatches"], [])
        self.assertTrue(self.summary["review_flags_respected"])

    def test_every_result_uses_the_engine_schema_and_vocabulary(self):
        for result in self.results:
            self.assertEqual(result["schema"],
                             "investigation.math_verification/1")
            self.assertIn(result["outcome"], OUTCOMES)
            self.assertIn("claim_id", result)
            self.assertIn("review_required", result)

    def test_coverage_of_the_required_breadth(self):
        kinds = {result["kind"] for result in self.results}
        for required in ("arithmetic", "algebraic_equivalence",
                         "expansion_identity", "factorisation_identity",
                         "fraction_equivalence", "surd_equality",
                         "equation_solutions", "quadratic_solutions",
                         "substitution", "transformation_with_assumptions",
                         "approximate_equality"):
            self.assertIn(required, kinds)

    def test_unsupported_and_malicious_cases_are_covered(self):
        refusals = [result for result in self.results
                    if result["outcome"] == "unsupported"]
        self.assertGreaterEqual(len(refusals), 4)
        refusal_reasons = " ".join(
            json.dumps(result["evidence"]) for result in refusals)
        self.assertIn("unsupported", refusal_reasons)

    def test_summary_is_deterministic_and_serialisable(self):
        again_results, again_summary = run_benchmark()
        self.assertEqual(json.dumps(self.summary, sort_keys=True),
                         json.dumps(again_summary, sort_keys=True))
        self.assertEqual(
            json.dumps([dict(item) for item in self.results],
                       sort_keys=True),
            json.dumps([dict(item) for item in again_results],
                       sort_keys=True))
        self.assertEqual(again_summary["schema"], BENCHMARK_SCHEMA)


class BenchmarkIndependenceTests(unittest.TestCase):

    def test_no_data_shares_with_existing_benchmarks_or_policy(self):
        base = Path(__file__).parent.resolve()
        source = (base / "math_benchmark.py").read_text(encoding="utf-8")
        for forbidden in ("accuracy_cases", "verification_cases",
                          "case_governance", "graph", "grade",
                          "marking_requests", "app5"):
            self.assertNotIn(forbidden, source)
        self.assertIn("separation", source)


if __name__ == "__main__":
    unittest.main()
