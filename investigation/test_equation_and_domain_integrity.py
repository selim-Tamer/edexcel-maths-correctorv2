"""Offline regression tests for the PR #25 review corrections.

Issue 1: equation solution-set completeness (identities, empty solution
sets, constant equations, repeated roots, degree-3/4 solver limits,
rational equations reduced to polynomials).
Issue 2: original-domain restrictions (denominator zero-sets recorded
before cancellation, excluded solutions, undefined substitutions,
incomplete domain analysis stays inconclusive).
Issue 3: C3 source-reference document_id binding and ambiguity rules.

Investigation-only; no network and no student data.
"""
import json
import unittest

from investigation.pdf_evidence import extract_pdf_evidence
from investigation.pdf_test_fixtures import build_pdf
from investigation.math_claims import verify_math_claims
from investigation.math_verification import (
    OUTCOME_CONTRADICTED, OUTCOME_INCONCLUSIVE, OUTCOME_VERIFIED,
    verify_claim,
)

_REF = {"document_id": "pdf:unit-test", "document_role": "mark_scheme",
        "sha256": "abcd" * 16, "page": 3, "label": "unit-test page"}
_REFS = [_REF]


def _claim(kind, **keys):
    return {"source_references": _REFS, "kind": kind,
            "claim_id": "R-" + kind, **keys}


class EquationCompletenessTests(unittest.TestCase):
    """Issue 1: a claimed solution set is never marked complete unless
    the bounded solve provably enumerates the class."""

    def test_identically_true_equation_is_reported_infinitely_many(self):
        result = verify_claim(_claim("equation_solutions",
                                     equation="x = x", solutions=["1"]))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)
        self.assertIn("infinitely many solutions",
                      result["evidence"]["completeness"])
        self.assertIn("finite claimed solution set",
                      result["evidence"]["completeness"])
        # The identity is established BEFORE the solver, so the solver's
        # empty root list for the zero polynomial is never reported as
        # "no solutions".
        self.assertNotIn("computed_solution_set", result["evidence"])

    def test_identity_equation_with_excluded_original_domain(self):
        # x/x = 1: the difference cancels to zero, but the original
        # denominator excludes x = 0; the identity still has infinitely
        # many solutions, and the excluded value is stated.
        result = verify_claim(_claim("equation_solutions",
                                     equation="x/x = 1", solutions=["1"]))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)
        completeness = result["evidence"]["completeness"]
        self.assertIn("infinitely many solutions", completeness)
        self.assertIn("Integer(0)", completeness)

    def test_claimed_solution_on_an_excluded_value_is_refused(self):
        result = verify_claim(_claim("equation_solutions",
                                     equation="x/x = 1", solutions=["0"]))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)
        rejected = result["evidence"]["rejected_solutions"]
        self.assertEqual(
            rejected[0]["status"], "solution_excluded_by_original_domain")
        self.assertEqual(rejected[0]["solution"], "Integer(0)")

    def test_contradictory_constant_equation_reports_no_solutions(self):
        result = verify_claim(_claim("equation_solutions",
                                     equation="5 = 6", solutions=["3"]))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)
        self.assertIn("NO solutions", result["evidence"]["completeness"])
        rejected = result["evidence"]["rejected_solutions"]
        self.assertEqual(rejected[0]["status"], "root_rejected")
        self.assertEqual(rejected[0]["residual"], "-1")

    def test_constant_identity_equation_reports_infinitely_many(self):
        result = verify_claim(_claim("equation_solutions",
                                     equation="0 = 0", solutions=["2"]))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)
        self.assertIn("infinitely many solutions",
                      result["evidence"]["completeness"])

    def test_single_variable_equation_without_solutions(self):
        result = verify_claim(_claim("equation_solutions",
                                     equation="x + 1 = x + 2",
                                     solutions=["1"]))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)
        self.assertIn("NO solutions", result["evidence"]["completeness"])
        evidence = result["evidence"]
        self.assertEqual(evidence["computed_solution_set"], [])
        self.assertEqual(evidence["solution_set_on_common_domain"], [])

    def test_repeated_root_is_not_a_false_mismatch(self):
        # (x - 1)^2 = 0 has the single distinct solution 1 (multiplicity
        # two); multiplicity must not create a duplicate mismatch.
        result = verify_claim(_claim("equation_solutions",
                                     equation="(x - 1)**2 = 0",
                                     solutions=["1"]))
        self.assertEqual(result["outcome"], OUTCOME_VERIFIED)
        evidence = result["evidence"]
        self.assertEqual(evidence["computed_solution_set"], ["Integer(1)"])
        self.assertEqual(evidence["claimed_solutions_checked"][0]["status"],
                         "root_confirmed")

    def test_quadratic_repeated_root_complete_set(self):
        result = verify_claim(_claim("quadratic_solutions",
                                     equation="x**2 - 2*x + 1 = 0",
                                     solutions=["1"]))
        self.assertEqual(result["outcome"], OUTCOME_VERIFIED)
        self.assertEqual(result["evidence"]["solution_set_on_common_domain"],
                         ["Integer(1)"])

    def test_cubic_confirmed_roots_stay_inconclusive(self):
        # Degree-3 radical solvers can return incomplete sets, so even
        # exactly confirmed roots must never yield a verified complete set.
        result = verify_claim(_claim("equation_solutions",
                                     equation="x**3 - x = 0",
                                     solutions=["0", "1"]))
        self.assertEqual(result["outcome"], OUTCOME_INCONCLUSIVE)
        self.assertIn("completeness was not established",
                      result["evidence"]["completeness"])
        for entry in result["evidence"]["claimed_solutions_checked"]:
            self.assertEqual(entry["status"], "root_confirmed")

    def test_cubic_wrong_root_still_contradicted_by_exact_residual(self):
        result = verify_claim(_claim("equation_solutions",
                                     equation="x**3 - x = 0",
                                     solutions=["0", "7"]))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)
        rejected = result["evidence"]["rejected_solutions"]
        self.assertEqual(rejected[0]["residual"], "336")

    def test_rational_equation_reduced_to_polynomial_respects_domain(self):
        # (x^2 - 4)/(x - 2) = 0: the removable singularity at 2 must be
        # excluded, so the complete on-domain solution set is {-2}.
        result = verify_claim(_claim("equation_solutions",
                                     equation="(x**2 - 4)/(x - 2) = 0",
                                     solutions=["-2"]))
        self.assertEqual(result["outcome"], OUTCOME_VERIFIED)
        evidence = result["evidence"]
        self.assertEqual(evidence["computed_solution_set"], ["Integer(-2)"])
        self.assertEqual(evidence["original_domain_excluded_values"],
                         ["Integer(2)"])

    def test_claimed_root_zeroing_the_original_denominator_is_rejected(self):
        result = verify_claim(_claim("equation_solutions",
                                     equation="(x**2 - 4)/(x - 2) = 0",
                                     solutions=["2"]))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)
        rejected = result["evidence"]["rejected_solutions"]
        self.assertEqual(
            rejected[0]["status"], "solution_excluded_by_original_domain")

    def test_claimed_excluded_root_policy_for_common_fraction(self):
        # 1/x = 1/x is an identity except at x = 0; a claim containing 0
        # must be refused on the original domain, while any other claim
        # hits the infinitely-many rule.
        result = verify_claim(_claim("equation_solutions",
                                     equation="1/x = 1/x", solutions=["0"]))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)
        self.assertEqual(
            result["evidence"]["rejected_solutions"][0]["status"],
            "solution_excluded_by_original_domain")
        result = verify_claim(_claim("equation_solutions",
                                     equation="1/x = 1/x", solutions=["2"]))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)
        self.assertIn("infinitely many",
                      result["evidence"]["completeness"])

    def test_wrong_claim_on_complete_set_reports_computed_set(self):
        result = verify_claim(_claim("equation_solutions",
                                     equation="x**2 - 5*x + 6 = 0",
                                     solutions=["2"]))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)
        self.assertEqual(
            sorted(result["evidence"]["computed_solution_set"]),
            ["Integer(2)", "Integer(3)"])


class DomainRestrictionTests(unittest.TestCase):
    """Issue 2: original denominators are honoured before and after
    simplification; incomplete domain analysis blocks verification."""

    def test_cancelled_expression_keeps_its_original_exclusion(self):
        # x/x parses to 1, but the original expression is undefined at 0.
        result = verify_claim(_claim("algebraic_equivalence",
                                     lhs="x/x", rhs="1"))
        self.assertEqual(result["outcome"], OUTCOME_VERIFIED)
        self.assertEqual(result["review_required"], True)
        domain_notes = result["domain_notes"]
        self.assertTrue(any("left-hand side" in note and "Integer(0)" in note
                            for note in domain_notes))

    def test_incomplete_domain_analysis_is_inconclusive_not_verified(self):
        # A radical denominator cannot be resolved exactly, so even a
        # textually identical identity stays inconclusive.
        result = verify_claim(_claim("algebraic_equivalence",
                                     lhs="1/sqrt(x)", rhs="1/sqrt(x)"))
        self.assertEqual(result["outcome"], OUTCOME_INCONCLUSIVE)
        self.assertTrue(result["evidence"]["domain_analysis_incomplete"])
        self.assertNotEqual(result["outcome"], OUTCOME_VERIFIED)

    def test_original_domain_is_derived_before_cancellation(self):
        # (x+1)^-1 has excluded value -1 even though no literal '/' is used.
        result = verify_claim(_claim("substitution",
                                     expression="(x + 1)**-1",
                                     assignments={"x": "-1"},
                                     claimed_value="1"))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)
        self.assertEqual(
            result["evidence"]["undefined_assignments"],
            [{"variable": "x", "value": "Integer(-1)"}])

    def test_substitution_on_undefined_point_is_contradicted(self):
        result = verify_claim(_claim("substitution", expression="x/x",
                                     assignments={"x": "0"},
                                     claimed_value="1"))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)
        self.assertNotEqual(result["outcome"], OUTCOME_VERIFIED)
        self.assertIn("Integer(0)",
                      json.dumps(result["evidence"]
                                 ["undefined_assignments"]))

    def test_substitution_is_never_verified_on_a_pole(self):
        result = verify_claim(_claim("substitution", expression="1/(x - 1)",
                                     assignments={"x": "1"},
                                     claimed_value="5"))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)
        self.assertNotEqual(result["outcome"], OUTCOME_VERIFIED)
        self.assertIn("undefined", " ".join(result["domain_notes"]))

    def test_substitution_with_radical_denominator_stays_inconclusive(self):
        # 1/sqrt(x) at x = 4 is numerically 1/2, but the original-domain
        # analysis cannot be completed exactly, so verification is refused.
        result = verify_claim(_claim("substitution", expression="1/sqrt(x)",
                                     assignments={"x": "4"},
                                     claimed_value="1/2"))
        self.assertEqual(result["outcome"], OUTCOME_INCONCLUSIVE)
        self.assertTrue(result["evidence"]["domain_analysis_incomplete"])

    def test_substitution_at_a_valid_point_is_still_verified(self):
        result = verify_claim(_claim("substitution", expression="1/(x - 1)",
                                     assignments={"x": "2"},
                                     claimed_value="1"))
        self.assertEqual(result["outcome"], OUTCOME_VERIFIED)
        self.assertEqual(result["review_required"], False)

    def test_negative_power_exclusion(self):
        result = verify_claim(_claim("substitution", expression="x**-2",
                                     assignments={"x": "0"},
                                     claimed_value="1"))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)

    def test_invalid_cancellation_still_contradicted_with_domain(self):
        result = verify_claim(_claim("fraction_equivalence",
                                     lhs="(x**2 + 2*x + 1)/(x + 1)",
                                     rhs="x"))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)

    def test_common_domain_widening_is_reported_for_fractions(self):
        # 3/(x-2) equals 3(x+2)/(x^2-4) only on the common domain; the
        # right-hand side has the extra excluded value -2.
        result = verify_claim(_claim("fraction_equivalence",
                                     lhs="3/(x - 2)",
                                     rhs="3*(x + 2)/(x**2 - 4)"))
        self.assertEqual(result["outcome"], OUTCOME_VERIFIED)
        self.assertTrue(result["review_required"])
        domain_notes = " | ".join(result["domain_notes"])
        self.assertIn("right-hand side", domain_notes)
        self.assertIn("Integer(-2)", domain_notes)


class SourceReferenceBindingTests(unittest.TestCase):
    """Issue 3: document_id must bind a C3 record sharing the digest;
    duplicate digest+id bindings are ambiguous and rejected."""

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

    def _envelope(self, source_references, documents):
        claim = {"claim_id": "M-BIND", "kind": "arithmetic",
                 "lhs": "2 + 2", "rhs": "4",
                 "source_references": source_references}
        return verify_math_claims([claim], c3_documents=documents)

    def test_matching_document_id_is_accepted(self):
        envelope = self._envelope([dict(self.good_ref)], [self.document])
        self.assertEqual(envelope["results"][0]["outcome"],
                         OUTCOME_VERIFIED)

    def test_mismatched_document_id_is_rejected(self):
        bad_ref = dict(self.good_ref, document_id="pdf:somebody-else")
        with self.assertRaises(ValueError):
            self._envelope([bad_ref], [self.document])

    def test_document_id_is_checked_even_with_matching_digest_role_page(self):
        # sha256, role and page all agree with SOME record, but the
        # claimed document_id belongs to a different digest, so no
        # record matches both: the claim must be rejected.
        other_pdf = build_pdf([["Q3 (a) B1: different content"]])
        other = extract_pdf_evidence(other_pdf, "mark_scheme")
        self.assertNotEqual(other["sha256"], self.document["sha256"])
        decoy_ref = dict(self.good_ref, document_id=other["document_id"])
        with self.assertRaises(ValueError):
            self._envelope([decoy_ref], [self.document, other])

    def test_same_digest_different_ids_binds_by_document_id(self):
        alias = dict(self.document, document_id="pdf:alias")
        ref_alias = dict(self.good_ref, document_id="pdf:alias")
        envelope = self._envelope([ref_alias, dict(self.good_ref)],
                                  [self.document, alias])
        self.assertEqual(envelope["results"][0]["outcome"],
                         OUTCOME_VERIFIED)
        returned = envelope["results"][0]["source_references"]
        self.assertEqual(returned[0]["document_id"], "pdf:alias")
        self.assertEqual(returned[0]["sha256"], self.document["sha256"])
        self.assertEqual(returned[0]["page"], 1)

    def test_ambiguous_duplicate_digest_and_id_is_rejected(self):
        twin = dict(self.document)
        with self.assertRaises(ValueError):
            self._envelope([dict(self.good_ref)],
                           [self.document, twin])

    def test_role_and_page_checked_against_the_bound_record(self):
        alias = dict(self.document, document_id="pdf:alias")
        wrong_role = dict(self.good_ref, document_id="pdf:alias",
                          document_role="question_paper")
        with self.assertRaises(ValueError):
            self._envelope([wrong_role], [self.document, alias])
        wrong_page = dict(self.good_ref, document_id="pdf:alias", page=9)
        with self.assertRaises(ValueError):
            self._envelope([wrong_page], [self.document, alias])


if __name__ == "__main__":
    unittest.main()
