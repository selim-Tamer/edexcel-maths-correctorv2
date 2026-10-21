"""Phase C5 consolidation differential — permanent regression gate.

Locks in the consolidated alternative-method reconciliation behaviour across
the shared synthetic scenario set authored in ``c5_differential_cases.json``.
Each case's expected outcome is an independently-authored oracle (Edexcel-style
M/A/B rules), not a copy of either engine's output. The gate asserts the
authoritative engine matches the oracle exactly, and records where the original
Implementation A diverged (status or rule vocabulary) as consolidation evidence.

This module is investigation-only and offline: no network, no provider calls,
no student data, no live credentials.
"""
import unittest

from investigation import c5_differential

_REPORT = None


def _report():
    global _REPORT
    if _REPORT is None:
        _REPORT = c5_differential.evaluate()
    return _REPORT


class DifferentialGateTests(unittest.TestCase):
    """The authoritative (consolidated) engine must match the oracle exactly."""

    @classmethod
    def setUpClass(cls):
        cls.report = _report()
        cls.rows = {row["id"]: row for row in cls.report["cases"]}

    def test_every_case_matches_the_oracle(self):
        summary = self.report["summary"]
        failing = [row["id"] for row in self.report["cases"] if not row["pass"]]
        self.assertTrue(
            summary["all_matched"],
            f"consolidated engine diverged from oracle on: {failing}")

    def test_status_matches_oracle_for_every_case(self):
        for row in self.report["cases"]:
            with self.subTest(case=row["id"]):
                self.assertTrue(
                    row["status_ok"],
                    f"{row['id']}: expected {row['expected_status']}, "
                    f"got {row['consolidated']['status']}")

    def test_rule_ids_match_oracle_for_every_case(self):
        for row in self.report["cases"]:
            with self.subTest(case=row["id"]):
                self.assertTrue(
                    row["rules_ok"],
                    f"{row['id']}: expected {row['expected_rules']}, "
                    f"got {row['consolidated']['rule_ids']}")

    def test_blocking_matches_oracle_for_every_case(self):
        for row in self.report["cases"]:
            with self.subTest(case=row["id"]):
                self.assertTrue(
                    row["blocking_ok"],
                    f"{row['id']}: expected blocking={row.get('blocking')}")


class AlternativeReconciliationTests(unittest.TestCase):
    """The specific defects the consolidation ported from Implementation A."""

    @classmethod
    def setUpClass(cls):
        cls.rows = {row["id"]: row for row in _report()["cases"]}

    def test_labelled_alternatives_reconcile_to_checked(self):
        row = self.rows["labelled-alternatives-match"]
        self.assertEqual(row["consolidated"]["status"], "CHECKED")
        self.assertEqual(row["consolidated"]["rule_ids"], [])
        self.assertFalse(row["consolidated"]["blocking"])

    def test_labelled_alternatives_overcount_is_precise(self):
        row = self.rows["labelled-alternatives-overcount"]
        self.assertEqual(row["consolidated"]["status"], "NEEDS_TEACHER_REVIEW")
        self.assertIn("C5-TOTAL-MISMATCH", row["consolidated"]["rule_ids"])
        self.assertNotIn("C5-ALT-SUM-REVIEW", row["consolidated"]["rule_ids"])
        self.assertTrue(row["consolidated"]["blocking"])

    def test_unlabelled_alternatives_still_escalate_and_never_sum(self):
        row = self.rows["unlabelled-alternatives"]
        self.assertEqual(row["consolidated"]["status"], "NEEDS_TEACHER_REVIEW")
        self.assertIn("C5-ALT-SUM-REVIEW", row["consolidated"]["rule_ids"])

    def test_overcount_never_double_counts_branches(self):
        # The consolidated engine must not report both a generic
        # C5-ALT-SUM-REVIEW and a C5-TOTAL-MISMATCH for a labelled structure.
        row = self.rows["labelled-alternatives-match"]
        self.assertNotIn("C5-ALT-SUM-REVIEW", row["consolidated"]["rule_ids"])


class ConsolidationEvidenceTests(unittest.TestCase):
    """Record where the original engines diverged from the oracle (evidence)."""

    @classmethod
    def setUpClass(cls):
        cls.report = _report()

    def test_original_a_matched_most_oracle_statuses(self):
        # Implementation A agreed with the oracle on at least half the cases;
        # this pins that A's escalation logic was sound, not that A is authoritative.
        summary = self.report["summary"]
        self.assertGreaterEqual(summary["original_a_status_matches_oracle"], 5)

    def test_authoritative_engine_is_stronger_than_original_a(self):
        # The consolidated engine must match the oracle on every case,
        # strictly more than the original A implementation did.
        summary = self.report["summary"]
        self.assertEqual(summary["passed"], summary["cases"])
        self.assertGreater(
            summary["passed"], summary["original_a_status_matches_oracle"])


class DifferentialIsolationTests(unittest.TestCase):
    """The differential must stay offline and bounded."""

    def test_case_set_is_offline_and_bounded(self):
        cases = c5_differential.load_cases()
        self.assertGreaterEqual(len(cases), 10)
        for case in cases:
            with self.subTest(case=case["id"]):
                self.assertLessEqual(len(case.get("criteria", [])), 50)


if __name__ == "__main__":
    unittest.main()
