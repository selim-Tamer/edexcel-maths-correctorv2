"""Offline regression tests for the Stage C4 C2/C3 integration layer.

Confirms: C2 findings pass through unchanged, C3 source hashes and pages
are preserved and cross-checked, caller structures are never mutated, and
mathematical correctness stays separated from marking eligibility.
"""
import json
import unittest

from investigation.pdf_evidence import extract_pdf_evidence
from investigation.pdf_test_fixtures import build_pdf
from investigation.math_claims import (
    ENVELOPE_SCHEMA, SEPARATION_NOTES, verify_math_claims,
)
from investigation.math_verification import (
    OUTCOME_CONTRADICTED, OUTCOME_VERIFIED, verify_claim,
)


class _IntegrationBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        data = build_pdf([["Q1 (a) A1: x = 4"], ["Q2 (a) M1: forming"]])
        cls.document = extract_pdf_evidence(data, "mark_scheme")
        cls.good_ref = {
            "document_id": cls.document["document_id"],
            "document_role": "mark_scheme",
            "sha256": cls.document["sha256"],
            "page": 1,
            "label": "Q1 page",
        }

    def _claim(self, claim_id="M-1", **overrides):
        claim = {
            "claim_id": claim_id,
            "kind": "arithmetic",
            "lhs": "2 + 2",
            "rhs": "4",
            "source_references": [dict(self.good_ref)],
        }
        claim.update(overrides)
        return claim


class EnvelopeTests(_IntegrationBase):

    def test_envelope_schema_notes_and_ordering(self):
        claims = [self._claim("M-2"), self._claim("M-1")]
        envelope = verify_math_claims(claims)
        self.assertEqual(envelope["schema"], ENVELOPE_SCHEMA)
        self.assertEqual(envelope["claims_verified"], 2)
        self.assertEqual([item["claim_id"] for item in envelope["results"]],
                         ["M-1", "M-2"])          # sorted, deterministic
        for note in SEPARATION_NOTES:
            self.assertIn(note, envelope["notes"])

    def test_results_match_direct_verification(self):
        envelope = verify_math_claims([self._claim()])
        direct = verify_claim(self._claim())
        self.assertEqual(envelope["results"][0]["outcome"],
                         direct["outcome"])
        self.assertEqual(envelope["results"][0]["schema"], direct["schema"])

    def test_ref_hashes_and_pages_are_preserved_verbatim(self):
        envelope = verify_math_claims([self._claim()])
        reference = envelope["results"][0]["source_references"][0]
        self.assertEqual(reference["sha256"], self.document["sha256"])
        self.assertEqual(reference["page"], 1)
        self.assertEqual(reference["document_id"],
                         self.document["document_id"])

    def test_verdicts_carry_no_marking_decisions(self):
        envelope = verify_math_claims(
            [self._claim(), self._claim("M-bad", lhs="2 + 2", rhs="5")])
        self.assertEqual(envelope["results"][1]["outcome"],
                         OUTCOME_CONTRADICTED)
        names = set()
        for result in envelope["results"]:
            names.update(result)
        for forbidden in ("grade", "mark", "total", "eligibility"):
            self.assertNotIn(forbidden, names)


class C2PassThroughTests(_IntegrationBase):

    def test_c2_findings_are_passed_through_unchanged(self):
        findings = [{"finding_id": "C2F-1::q1::mark_scheme",
                     "statement": "unrelated C2 finding",
                     "pages": [2]}]
        envelope = verify_math_claims([self._claim()],
                                      c2_findings=findings)
        self.assertEqual(envelope["c2_findings_passed_through"], findings)
        # deep copy: later mutation of the caller's list cannot leak in
        findings[0]["statement"] = "tampered"
        self.assertEqual(
            envelope["c2_findings_passed_through"][0]["statement"],
            "unrelated C2 finding")

    def test_c2_findings_can_be_omitted(self):
        envelope = verify_math_claims([self._claim()])
        self.assertEqual(envelope["c2_findings_passed_through"], [])

    def test_caller_claims_are_never_mutated(self):
        claim = self._claim()
        snapshot = json.dumps(claim, sort_keys=True)
        verify_math_claims([claim])
        self.assertEqual(json.dumps(claim, sort_keys=True), snapshot)


class C3CrossCheckTests(_IntegrationBase):

    def test_matching_reference_is_accepted_against_c3_record(self):
        envelope = verify_math_claims([self._claim()],
                                      c3_documents=[self.document])
        self.assertEqual(envelope["results"][0]["outcome"],
                         OUTCOME_VERIFIED)

    def test_digest_mismatch_is_rejected(self):
        bad_ref = dict(self.good_ref, sha256="0" * 64)
        with self.assertRaises(ValueError):
            verify_math_claims([self._claim(source_references=[bad_ref])],
                               c3_documents=[self.document])

    def test_page_outside_the_document_is_rejected(self):
        bad_ref = dict(self.good_ref, page=99)
        with self.assertRaises(ValueError):
            verify_math_claims([self._claim(source_references=[bad_ref])],
                               c3_documents=[self.document])

    def test_role_disagreement_is_rejected(self):
        bad_ref = dict(self.good_ref, document_role="question_paper")
        with self.assertRaises(ValueError):
            verify_math_claims([self._claim(source_references=[bad_ref])],
                               c3_documents=[self.document])

    def test_unbound_references_are_allowed_without_c3_documents(self):
        unbound = {"document_id": "pdf:other", "document_role": "mark_scheme",
                   "sha256": "0" * 64, "page": 1, "label": "other"}
        envelope = verify_math_claims(
            [self._claim(source_references=[unbound])])
        self.assertEqual(envelope["results"][0]["outcome"],
                         OUTCOME_VERIFIED)


class EnvelopeInputValidationTests(_IntegrationBase):

    def test_duplicate_claim_ids_are_rejected(self):
        with self.assertRaises(ValueError):
            verify_math_claims([self._claim("M-1"), self._claim("M-1")])

    def test_unsupported_kinds_are_rejected_with_error(self):
        with self.assertRaises(ValueError):
            verify_math_claims([self._claim(kind="trig_identity")])

    def test_structure_and_count_bounds(self):
        with self.assertRaises(ValueError):
            verify_math_claims("not-a-list")
        with self.assertRaises(ValueError):
            verify_math_claims([self._claim(f"M-{index}")
                                for index in range(201)])


if __name__ == "__main__":
    unittest.main()
