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


class StaleReviewReuseTests(unittest.TestCase):
    """M3 regression: a cached C5 review always matches the extraction in use.

    The review cached under the scheme key must never be reused for
    different maxima, answers or question inventory: any material input
    change recomputes the review, so the warning channel always reflects the
    extraction currently being used. Synthetic data and mocked extraction
    only; no provider call is possible.
    """

    def setUp(self):
        self.f = load_app()
        self.clock = Clock()
        self.client = Provider(self.clock, transfer=0, processing=0)
        self.files = dp.FileCache("key-account", clock=self.clock)
        self.metadata = dp.Memo(clock=self.clock)
        self.scheme_key = ("scope", "manual:scheme", "digest", "v", ("m1",))
        self.review_key = (self.scheme_key, "scheme_review")

    def reference(self, maxima, answers, warning=None, numbers=None,
                  extractor=None):
        """One prepare_reference_metadata call with an explicit extraction."""
        mock = extractor if extractor is not None else Mock()
        mock.return_value = (maxima, answers, warning)
        self.f["extract_scheme_maxima"] = mock
        inventory_key = None
        if numbers is not None:
            inventory_key = ("scope", "inventory")
            self.metadata.put(inventory_key, list(numbers))
        run = dp.DocumentRun(self.client, self.files)
        try:
            return self.f["prepare_reference_metadata"](
                self.client, uploaded(b"%PDF-scheme", "scheme.pdf"),
                self.metadata, self.scheme_key,
                inventory_key=inventory_key)
        finally:
            run.close()

    def count_review_calls(self):
        """Wrap the deterministic review to count invocations (restored)."""
        module = self.f["scheme_review"]
        original = module.review_extracted_maxima
        counter = Mock(wraps=original)
        module.review_extracted_maxima = counter
        self.addCleanup(setattr, module, "review_extracted_maxima", original)
        return counter

    def stored(self, metadata=None):
        return (metadata or self.metadata).get(self.review_key)

    def test_a_same_extraction_reuses_the_cached_review(self):
        reviews = self.count_review_calls()
        first, _ = self.reference({"1": 2}, {"1": "4"})
        first_stored = self.stored()
        second, _ = self.reference({"1": 2}, {"1": "4"})
        second_stored = self.stored()
        self.assertEqual(first, second)
        self.assertEqual(first_stored[0], second_stored[0])
        self.assertEqual(first_stored[1]["scheme_revision"],
                         second_stored[1]["scheme_revision"])
        self.assertEqual(reviews.call_count, 1)

    def test_b_changed_maxima_recompute_the_review(self):
        reviews = self.count_review_calls()
        first, _ = self.reference({"1a!": 2}, {"1": "4"}, warning="transient")
        self.assertIn("[C5-QID-INVALID]", first[2])
        second, _ = self.reference({"1": 2}, {"1": "4"}, warning=None)
        self.assertIsNone(second[2])
        self.assertEqual(reviews.call_count, 2)

    def test_c_changed_answers_recompute_the_review(self):
        first, _ = self.reference({"1": 2}, {"1": "4"}, warning="unclear")
        first_revision = self.stored()[1]["scheme_revision"]
        second, _ = self.reference({"1": 2}, {"1": "9"}, warning="unclear")
        second_revision = self.stored()[1]["scheme_revision"]
        self.assertNotEqual(first_revision, second_revision)
        expected = sr.review_extracted_maxima({"1": 2}, {"1": "9"})
        self.assertEqual(second_revision, expected["scheme_revision"])

    def test_d_failed_extraction_then_retry_uses_current_results(self):
        first, _ = self.reference({}, {}, warning=None)
        self.assertIn("[C5-EXTRACTION-EMPTY]", first[2])
        second, _ = self.reference({"1": 2}, {"1": "4"}, warning=None)
        self.assertIsNone(second[2])

    def test_e_question_inventory_change_recomputes_the_review(self):
        first, _ = self.reference({"1": 2}, {"1": "4"}, numbers=["1", "9"])
        self.assertIn("C5-QUESTION-NOT-INTERPRETED", first[2])
        second, _ = self.reference({"1": 2}, {"1": "4"}, numbers=["1"])
        self.assertIsNone(second[2])

    def test_f_review_cache_does_not_cross_sessions(self):
        first, _ = self.reference({"1a!": 2}, {"1": "4"}, warning="transient")
        self.assertIn("[C5-QID-INVALID]", first[2])
        first_stored = self.stored()
        other_metadata = dp.Memo(clock=self.clock)
        self.assertIsNone(other_metadata.get(self.review_key))
        self.f["extract_scheme_maxima"] = Mock(
            return_value=({"1": 2}, {"1": "4"}, None))
        run = dp.DocumentRun(self.client, self.files)
        try:
            info, _ = self.f["prepare_reference_metadata"](
                self.client, uploaded(b"%PDF-scheme", "scheme.pdf"),
                other_metadata, self.scheme_key)
        finally:
            run.close()
        self.assertIsNone(info[2])
        self.assertIsNotNone(other_metadata.get(self.review_key))
        self.assertEqual(self.stored(), first_stored)

    def test_g_repeated_reviews_add_no_provider_calls(self):
        extractor = Mock(return_value=({"1": 2}, {"1": "4"}, None))
        for _ in range(3):
            self.reference({"1": 2}, {"1": "4"}, extractor=extractor)
        self.assertEqual(extractor.call_count, 1)
        self.client.files.upload.assert_not_called()
        self.client.files.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
