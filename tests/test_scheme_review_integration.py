"""Phase C5 production integration tests: reference-path wiring is offline.

Verifies that the deterministic scheme review hooks into the existing
reference pipeline without adding any provider calls, that teacher warnings
surface, and that ownership/invalidation keys are protected. Uses mocked
extraction functions and synthetic documents only.
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))
sys.path.insert(0, str(TESTS))

import document_pipeline as dp
from pipeline_fakes import Clock, Provider, uploaded
from support import load_app
import scheme_review as sr


class PrepareReferenceReviewTests(unittest.TestCase):
    def setUp(self):
        self.f = load_app()
        self.clock = Clock()
        self.client = Provider(self.clock, transfer=0, processing=0)
        self.session = {
            "files": dp.FileCache("key-account", clock=self.clock),
            "metadata": dp.Memo(clock=self.clock),
            "results": dp.Memo(clock=self.clock),
        }
        self.scheme = uploaded(b"%PDF-scheme", "scheme.pdf")

    def reference(self, maxima=None, answers=None, warning=None):
        """A None maxima means the synthetic clean extraction; {} means empty."""
        if maxima is None:
            maxima, answers = {"1": 2}, {"1": "4"}
        self.f["extract_scheme_maxima"] = Mock(
            return_value=(maxima, answers, warning))
        run = dp.DocumentRun(self.client, self.session["files"])
        try:
            return self.f["prepare_reference_metadata"](
                self.client, uploaded(b"%PDF-scheme", "scheme.pdf"),
                self.session["metadata"], ("scope", "manual:scheme", "digest",
                                           "v", ("m1",)))
        finally:
            run.close()

    def test_clean_extraction_keeps_warning_channel_untouched(self):
        info, numbers = self.reference()
        self.assertEqual(info, ({"1": 2}, {"1": "4"}, None))

    def test_no_provider_calls_are_added_by_the_review(self):
        self.reference(maxima={"1a!": 2})
        self.client.files.upload.assert_not_called()
        self.client.files.get.assert_not_called()

    def test_problematic_extraction_extends_the_warning_channel(self):
        info, numbers = self.reference(maxima={"1a!": 2})
        self.assertIn("[C5-QID-INVALID]", info[2])
        key = (("scope", "manual:scheme", "digest", "v", ("m1",)),
               "scheme_review")
        self.assertIsNotNone(self.session["metadata"].get(key))

    def test_review_is_cached_beside_the_extraction(self):
        first, _ = self.reference(maxima={"1a!": 2})
        second, _ = self.reference(maxima={"1a!": 2}, warning=None)
        self.assertEqual(first[2], second[2])

    def test_empty_extraction_flags_were_expected_and_warning_text_added(self):
        info, numbers = self.reference(maxima={}, answers={})
        self.assertIn("[C5-EXTRACTION-EMPTY]", info[2])

    def test_question_numbers_coverage_adds_a_message(self):
        info, numbers = self.reference(
            maxima={"1": 2}, answers={"1": "4"},
            warning="Could not verify totals.")
        self.assertTrue(info[2].startswith("Could not verify totals."))


class ReviewStateKeysTests(unittest.TestCase):
    def setUp(self):
        self.f = load_app()

    def test_scheme_review_approvals_are_sensitive_state(self):
        self.assertIn("scheme_review_approvals", self.f["SENSITIVE_STATE_KEYS"])
        self.assertIn("scheme_review_ack_",
                      self.f["SENSITIVE_WIDGET_PREFIXES"])

    def test_scheme_review_for_info_rebuilds_the_review(self):
        review = self.f["scheme_review_for_info"](
            ({"1a!": 2}, {"1a!": "4"}, None))
        self.assertIsNotNone(review)
        self.assertIn("C5-QID-INVALID",
                      [finding["rule_id"] for finding in
                       sr.unresolved_findings(review)])
        self.assertIsNone(self.f["scheme_review_for_info"](None))


if __name__ == "__main__":
    unittest.main()
