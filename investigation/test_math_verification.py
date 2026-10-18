"""Offline regression tests for the Stage C4 verification engine.

Covers every check kind, the result contract, domain sensitivity,
complexity limits, refusal passthrough, determinism and the review rules.
Investigation-only; no network and no student data.
"""
import json
import unittest

from investigation.math_verification import (
    CHECK_KINDS, CLAIM_SCOPE_NOTE, EXACT_KIND, APPROXIMATE_KIND,
    OUTCOME_CONTRADICTED, OUTCOME_INCONCLUSIVE, OUTCOME_UNSUPPORTED,
    OUTCOME_VERIFIED, RESULT_SCHEMA, verify_claims, verify_claim,
)

_REF = {"document_id": "pdf:unit-test", "document_role": "mark_scheme",
        "sha256": "abcd" * 16, "page": 3, "label": "unit-test page"}
_REFS = [_REF]


def _claim(kind, **keys):
    return {"source_references": _REFS, "kind": kind, "claim_id": "T-" + kind,
            **keys}


class ContractShapeTests(unittest.TestCase):

    def test_result_contract_keys(self):
        result = verify_claim(_claim("arithmetic", lhs="2 + 2", rhs="4"))
        expected = {
            "schema", "claim_id", "kind", "method_id", "method_description",
            "mathematical_input", "assumptions", "domain_notes", "outcome",
            "outcomes_vocabulary", "verification_kind", "evidence",
            "explanation", "source_references", "review_required",
            "claim_scope_note"}
        self.assertEqual(set(result), expected)
        self.assertEqual(result["schema"], RESULT_SCHEMA)
        self.assertEqual(set(result["outcomes_vocabulary"]),
                         {"verified", "contradicted", "inconclusive",
                          "unsupported"})
        self.assertIn(CLAIM_SCOPE_NOTE, result["claim_scope_note"])

    def test_every_registered_kind_is_reachable(self):
        self.assertEqual(len(CHECK_KINDS), 11)
        for kind in CHECK_KINDS:
            self.assertIn(kind, (
                "arithmetic", "algebraic_equivalence", "expansion_identity",
                "factorisation_identity", "fraction_equivalence",
                "surd_equality", "equation_solutions", "quadratic_solutions",
                "substitution", "transformation_with_assumptions",
                "approximate_equality"))

    def test_unknown_kind_and_key_mismatches_are_rejected(self):
        with self.assertRaises(ValueError):
            verify_claim(_claim("trig_identity", lhs="x", rhs="x"))
        with self.assertRaises(ValueError):
            verify_claim(_claim("arithmetic", lhs="1", rhs="1",
                                tolerance=None))
        with self.assertRaises(ValueError):
            broken = _claim("arithmetic", lhs="1", rhs="1")
            del broken["lhs"]
            verify_claim(broken)

    def test_invalid_claim_ids_are_rejected(self):
        for claim_id in ("", "  spaced  ", "x" * 101, None, 5):
            with self.assertRaises(ValueError):
                verify_claim(_claim("arithmetic", claim_id=claim_id,
                                    lhs="1", rhs="1"))

    def test_verification_is_deterministic_and_serialisable(self):
        claim = _claim("fraction_equivalence",
                       lhs="3/(x - 2)", rhs="3*(x + 2)/(x**2 - 4)")
        first = verify_claim(claim)
        second = verify_claim(claim)
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))
        self.assertEqual(first["outcome"], OUTCOME_VERIFIED)
        self.assertTrue(first["review_required"])   # domain note present

    def test_verify_claims_list_helper(self):
        claims = [_claim("arithmetic", claim_id="T-2", lhs="1 + 1", rhs="2"),
                  _claim("arithmetic", claim_id="T-1", lhs="2 + 2", rhs="5")]
        results = verify_claims(claims)
        self.assertEqual([item["claim_id"] for item in results],
                         ["T-2", "T-1"])
        self.assertEqual(results[1]["outcome"], OUTCOME_CONTRADICTED)
        with self.assertRaises(ValueError):
            verify_claims("nope")


class SourceReferenceTests(unittest.TestCase):

    def test_refs_are_validated_into_independent_copies(self):
        reference = dict(_REF)
        result = verify_claim(_claim("arithmetic", lhs="1 + 1", rhs="2",
                                     source_references=[reference]))
        self.assertEqual(result["source_references"][0]["label"],
                         "unit-test page")
        reference["label"] = "mutated"
        self.assertEqual(result["source_references"][0]["label"],
                         "unit-test page")
        self.assertEqual(result["review_required"], False)

    def test_excerpt_is_optional_and_preserved(self):
        reference = dict(_REF, excerpt="x = 4 (A1)")
        result = verify_claim(_claim("arithmetic", lhs="1 + 1", rhs="2",
                                     source_references=[reference]))
        self.assertEqual(result["source_references"][0]["excerpt"],
                         "x = 4 (A1)")

    def test_invalid_references_are_rejected(self):
        bad = [
            dict(_REF, page=0),
            dict(_REF, page="1"),
            dict(_REF, sha256="short"),
            dict(_REF, sha256="nothex" * 12 + "Z"),
            dict(_REF, document_role="teacher_notes"),
            dict(_REF, document_id=""),
            dict(_REF, label=""),
            dict(_REF, extra="no"),
        ]
        for reference in bad:
            with self.assertRaises(ValueError):
                verify_claim(_claim("arithmetic", lhs="1", rhs="1",
                                    source_references=[reference]))

    def test_lengthy_excerpt_is_rejected(self):
        with self.assertRaises(ValueError):
            verify_claim(_claim("arithmetic", lhs="1", rhs="1",
                                source_references=[dict(_REF,
                                                        excerpt="9" * 241)]))


class ArithmeticTests(unittest.TestCase):

    def test_correct_and_incorrect_arithmetic(self):
        result = verify_claim(_claim("arithmetic", lhs="2/4 + 1/3",
                                     rhs="5/6"))
        self.assertEqual(result["outcome"], OUTCOME_VERIFIED)
        self.assertEqual(result["method_description"],
                         "exact_rational_difference")
        self.assertEqual(result["verification_kind"], EXACT_KIND)
        self.assertEqual(result["review_required"], False)
        wrong = verify_claim(_claim("arithmetic", lhs="3/4", rhs="2/3"))
        self.assertEqual(wrong["outcome"], OUTCOME_CONTRADICTED)
        self.assertNotEqual(wrong["evidence"]["canonical_difference"], "0")

    def test_arithmetic_with_variables_is_unsupported_not_guessed(self):
        result = verify_claim(_claim("arithmetic", lhs="x + 1", rhs="4"))
        self.assertEqual(result["outcome"], OUTCOME_UNSUPPORTED)
        self.assertIn("arithmetic", result["evidence"]["unapplicable_reason"])

    def test_arithmetic_floats_degrade_to_approximate(self):
        result = verify_claim(_claim("arithmetic", lhs="1.5", rhs="1.5"))
        self.assertEqual(result["outcome"], OUTCOME_VERIFIED)
        self.assertEqual(result["verification_kind"], APPROXIMATE_KIND)
        self.assertTrue(result["review_required"])


class EquivalenceFamilyTests(unittest.TestCase):

    def test_expansion_identity_verified_and_contradicted(self):
        good = verify_claim(_claim("expansion_identity", lhs="(x + 2)**2",
                                   rhs="x**2 + 4*x + 4"))
        self.assertEqual(good["outcome"], OUTCOME_VERIFIED)
        self.assertEqual(good["method_description"],
                         "bounded_expand_and_canonical_difference")
        bad = verify_claim(_claim("expansion_identity", lhs="(x + 2)**2",
                                  rhs="x**2 + 4"))
        self.assertEqual(bad["outcome"], OUTCOME_CONTRADICTED)
        self.assertIn("polynomial_degree", bad["evidence"])

    def test_factorisation_identity_verified_and_contradicted(self):
        good = verify_claim(_claim("factorisation_identity", lhs="x**2 - 9",
                                   rhs="(x - 3)*(x + 3)"))
        bad = verify_claim(_claim("factorisation_identity", lhs="x**2 - 4",
                                  rhs="(x - 2)**2"))
        self.assertEqual(good["outcome"], OUTCOME_VERIFIED)
        self.assertEqual(bad["outcome"], OUTCOME_CONTRADICTED)

    def test_complexity_guard_reports_unsupported_instead_of_running(self):
        chain = "*".join(f"(x + y + {i})**3" for i in range(1, 5))
        result = verify_claim(_claim("algebraic_equivalence", lhs=chain,
                                     rhs="0"))
        self.assertEqual(result["outcome"], OUTCOME_UNSUPPORTED)
        self.assertIn("term budget",
                      result["evidence"]["unapplicable_reason"])

    def test_surds_verified_contradicted_and_inconclusive(self):
        good = verify_claim(_claim("surd_equality", lhs="sqrt(50)",
                                   rhs="5*sqrt(2)"))
        self.assertEqual(good["outcome"], OUTCOME_VERIFIED)
        self.assertEqual(good["evidence"]["canonicalisation"], "raw")
        bad = verify_claim(_claim("surd_equality", lhs="sqrt(45)",
                                  rhs="6*sqrt(2)"))
        self.assertEqual(bad["outcome"], OUTCOME_CONTRADICTED)
        # symbolic surd forms the bounded routine cannot reconcile stay
        # inconclusive; nonzero symbolic difference is NOT a contradiction
        symbolic = verify_claim(_claim("surd_equality", lhs="sqrt(x**2)",
                                       rhs="x"))
        self.assertEqual(symbolic["outcome"], OUTCOME_INCONCLUSIVE)
        self.assertTrue(symbolic["evidence"].get(
            "bounded_operations_were_insufficient"))


class FractionDomainTests(unittest.TestCase):

    def test_exact_fraction_equivalence_without_poles(self):
        result = verify_claim(_claim("fraction_equivalence",
                                     lhs="(3*x - 3)/9", rhs="x/3 - 1/3"))
        self.assertEqual(result["outcome"], OUTCOME_VERIFIED)
        self.assertEqual(result["domain_notes"], [])
        self.assertEqual(result["review_required"], False)

    def test_true_identity_with_pole_gets_domain_note_and_review(self):
        result = verify_claim(_claim("fraction_equivalence",
                                     lhs="3/(x - 2)",
                                     rhs="3*(x + 2)/(x**2 - 4)"))
        self.assertEqual(result["outcome"], OUTCOME_VERIFIED)
        self.assertTrue(any("right-hand side is undefined" in note
                            for note in result["domain_notes"]))
        self.assertTrue(result["evidence"]["extra_poles_rhs"])
        self.assertTrue(result["review_required"])

    def test_invalid_cancellation_is_contradicted(self):
        result = verify_claim(_claim("fraction_equivalence",
                                     lhs="(x**2 + 2*x + 1)/(x + 1)",
                                     rhs="x"))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)


class EquationSolutionTests(unittest.TestCase):

    def test_quadratic_complete_solution_set_is_verified(self):
        result = verify_claim(_claim("quadratic_solutions",
                                     equation="x**2 - 5*x + 6 = 0",
                                     solutions=["2", "3"]))
        self.assertEqual(result["outcome"], OUTCOME_VERIFIED)
        self.assertEqual(result["method_description"],
                         "quadratic_solution_verification")
        self.assertIn("exactly the claimed solution set",
                      result["evidence"]["completeness"])

    def test_missing_root_is_contradicted(self):
        result = verify_claim(_claim("quadratic_solutions",
                                     equation="x**2 - 5*x + 6 = 0",
                                     solutions=["2"]))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)
        computed = result["evidence"]["computed_solution_set"]
        self.assertEqual(len(computed), 2)
        self.assertIn("Integer(2)", computed)
        self.assertIn("Integer(3)", computed)

    def test_wrong_root_is_contradicted_with_residual(self):
        result = verify_claim(_claim("quadratic_solutions",
                                     equation="x**2 - 5*x + 6 = 0",
                                     solutions=["2", "5/2"]))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)
        rejected = result["evidence"]["rejected_solutions"][0]
        self.assertEqual(rejected["solution"], "Rational(5, 2)")
        self.assertEqual(rejected["residual"], "-1/4")

    def test_quadratic_requires_degree_one_or_two(self):
        result = verify_claim(_claim("quadratic_solutions",
                                     equation="x**3 - x = 0",
                                     solutions=["0"]))
        self.assertEqual(result["outcome"], OUTCOME_UNSUPPORTED)

    def test_non_polynomial_equation_roots_valid_completeness_open(self):
        result = verify_claim(_claim("equation_solutions",
                                     equation="sqrt(x) = x - 2",
                                     solutions=["4"]))
        self.assertEqual(result["outcome"], OUTCOME_INCONCLUSIVE)
        self.assertIn("completeness", result["evidence"])
        self.assertEqual(
            result["evidence"]["claimed_solutions_checked"][0]["status"],
            "root_confirmed")

    def test_extraneous_solutions_are_contradicted(self):
        result = verify_claim(_claim("equation_solutions",
                                     equation="sqrt(x) = x - 2",
                                     solutions=["1", "4"]))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)
        self.assertEqual(
            result["evidence"]["rejected_solutions"][0]["solution"],
            "Integer(1)")

    def test_multi_variable_equation_is_unsupported(self):
        result = verify_claim(_claim("equation_solutions",
                                     equation="x + y = 5", solutions=["2"]))
        self.assertEqual(result["outcome"], OUTCOME_UNSUPPORTED)

    def test_approximate_coefficients_and_solutions_refused(self):
        result = verify_claim(_claim("equation_solutions",
                                     equation="x**2 - 2.5*x = 0",
                                     solutions=["2"]))
        self.assertEqual(result["outcome"], OUTCOME_UNSUPPORTED)
        approx_root = verify_claim(_claim("equation_solutions",
                                          equation="x**2 - 5*x + 6 = 0",
                                          solutions=["2.0"]))
        self.assertEqual(approx_root["outcome"], OUTCOME_UNSUPPORTED)

    def test_solution_list_validation(self):
        with self.assertRaises(ValueError):
            verify_claim(_claim("equation_solutions",
                                equation="x = 5", solutions=[]))
        with self.assertRaises(ValueError):
            verify_claim(_claim("equation_solutions", equation="x = 5",
                                solutions=["1"] * 9))
        with self.assertRaises(ValueError):
            verify_claim(_claim("equation_solutions", equation="x = 5",
                                solutions=["1", "1"]))


class SubstitutionTests(unittest.TestCase):

    def test_substitution_verified_and_contradicted(self):
        good = verify_claim(_claim("substitution", expression="x**2 + 1",
                                   assignments={"x": "2"}, claimed_value="5"))
        bad = verify_claim(_claim("substitution", expression="x**2 + 1",
                                  assignments={"x": "2"},
                                  claimed_value="7"))
        self.assertEqual(good["outcome"], OUTCOME_VERIFIED)
        self.assertEqual(good["evidence"]["canonical_difference"], "0")
        self.assertEqual(bad["outcome"], OUTCOME_CONTRADICTED)
        self.assertEqual(good["method_description"],
                         "exact_substitution_evaluation")

    def test_partially_assigned_expression_is_unsupported(self):
        result = verify_claim(_claim("substitution", expression="x**2 + y",
                                     assignments={"x": "2"},
                                     claimed_value="5"))
        self.assertEqual(result["outcome"], OUTCOME_UNSUPPORTED)
        self.assertIn("not every variable", result["evidence"]
                      ["unapplicable_reason"])

    def test_symbolic_claimed_value_is_unsupported(self):
        result = verify_claim(_claim("substitution", expression="x**2",
                                     assignments={"x": "2"},
                                     claimed_value="2*x"))
        self.assertEqual(result["outcome"], OUTCOME_UNSUPPORTED)

    def test_assignment_validation(self):
        with self.assertRaises(ValueError):
            verify_claim(_claim("substitution", expression="x**2",
                                assignments={}, claimed_value="4"))
        with self.assertRaises(ValueError):
            verify_claim(_claim("substitution", expression="x**2",
                                assignments={"q": "2"}, claimed_value="4"))
        with self.assertRaises(ValueError):
            verify_claim(_claim("substitution", expression="x**2",
                                assignments=None, claimed_value="4"))


class AssumptionTests(unittest.TestCase):

    def test_identity_holds_only_under_stated_assumption(self):
        result = verify_claim(_claim("transformation_with_assumptions",
                                     lhs="sqrt(x**2)", rhs="x",
                                     assumptions=["x > 0"]))
        self.assertEqual(result["outcome"], OUTCOME_VERIFIED)
        self.assertIn("ONLY under the stated assumptions",
                      result["evidence"]["assumption_dependency"])
        entry = result["assumptions"][0]
        self.assertEqual(entry["statement"], "x > 0")
        self.assertEqual(entry["relation"], ">")
        # an assumption-dependent identity is never auto-trusted outside
        # its stated domain, so teacher review is mandatory
        self.assertTrue(result["review_required"])

    def test_negative_assumption_flip(self):
        result = verify_claim(_claim("transformation_with_assumptions",
                                     lhs="abs(x)", rhs="-x",
                                     assumptions=["x < 0"]))
        self.assertEqual(result["outcome"], OUTCOME_VERIFIED)

    def test_identity_without_any_assumption_dependency(self):
        result = verify_claim(_claim("transformation_with_assumptions",
                                     lhs="(x + y)**2",
                                     rhs="x**2 + 2*x*y + y**2",
                                     assumptions=["x > 0"]))
        self.assertEqual(result["outcome"], OUTCOME_VERIFIED)
        self.assertEqual(result["evidence"]["assumption_dependency"],
                         "none_required")
        self.assertEqual(result["review_required"], False)

    def test_unproven_assumption_claim_is_inconclusive_not_contradicted(self):
        # abs(x) == -x under x > 0: the bounded refinement cannot certify;
        # it also does NOT declare a false contradiction, because the
        # refinement operation is directional, not an equivalence theorem.
        result = verify_claim(_claim("transformation_with_assumptions",
                                     lhs="abs(x)", rhs="-x",
                                     assumptions=["x > 0"]))
        self.assertEqual(result["outcome"], OUTCOME_INCONCLUSIVE)
        self.assertIn("not reduced to zero",
                      result["evidence"]["assumption_dependency"])

    def test_assumption_cannot_compare_constants_only(self):
        result = verify_claim(_claim("transformation_with_assumptions",
                                     lhs="sqrt(x**2)", rhs="x",
                                     assumptions=["2 > 1"]))
        self.assertEqual(result["outcome"], OUTCOME_UNSUPPORTED)

    def test_assumption_syntax_is_bounded(self):
        cases = {"x + 5": None, "x = 5": None, "x > ": None,
                 "": None}
        for text in cases:
            result = verify_claim(_claim(
                "transformation_with_assumptions", lhs="sqrt(x**2)",
                rhs="x", assumptions=[text]))
            self.assertEqual(result["outcome"], OUTCOME_UNSUPPORTED, text)

    def test_assumption_list_bounds(self):
        with self.assertRaises(ValueError):
            verify_claim(_claim("transformation_with_assumptions",
                                lhs="sqrt(x**2)", rhs="x",
                                assumptions=["x > 0"] * 7))
        with self.assertRaises(ValueError):
            verify_claim(_claim("transformation_with_assumptions",
                                lhs="sqrt(x**2)", rhs="x",
                                assumptions=[]))


class ApproximateTests(unittest.TestCase):

    def test_within_tolerance_verified_and_flagged_for_review(self):
        result = verify_claim(_claim("approximate_equality", lhs="sqrt(2)",
                                     rhs="1.4142135", tolerance=10 ** -5))
        self.assertEqual(result["outcome"], OUTCOME_VERIFIED)
        self.assertEqual(result["verification_kind"], APPROXIMATE_KIND)
        self.assertEqual(result["evidence"]["digits"], 50)
        self.assertIn("not an exact identity proof",
                      result["evidence"]["numeric_note"])
        self.assertTrue(result["review_required"])

    def test_outside_tolerance_is_contradicted(self):
        result = verify_claim(_claim("approximate_equality", lhs="sqrt(2)",
                                     rhs="1.5", tolerance=10 ** -5))
        self.assertEqual(result["outcome"], OUTCOME_CONTRADICTED)
        self.assertFalse(result["evidence"]["within_stated_tolerance"])

    def test_default_and_invalid_tolerances(self):
        result = verify_claim(_claim("approximate_equality", lhs="sqrt(2)",
                                     rhs="1.4142135", tolerance=None))
        self.assertEqual(result["outcome"], OUTCOME_VERIFIED)
        for bad in (0, -0.5, 2.0, True, "1e-3", None):
            if bad is None:
                continue
            with self.assertRaises(ValueError):
                verify_claim(_claim("approximate_equality", lhs="sqrt(2)",
                                    rhs="1.4142135", tolerance=bad))

    def test_unresolved_symbolic_numeric_claim_is_inconclusive(self):
        result = verify_claim(_claim("approximate_equality",
                                     lhs="x + 1.0", rhs="2.0",
                                     tolerance=10 ** -5))
        self.assertEqual(result["outcome"], OUTCOME_INCONCLUSIVE)
        self.assertTrue(result["evidence"]
                        ["numeric_evaluation_not_conclusive"])


class ReviewRuleTests(unittest.TestCase):

    def _review(self, claim):
        return verify_claim(claim)["review_required"]

    def test_exact_verified_with_refs_and_no_domain_notes_needs_no_review(self):
        self.assertFalse(self._review(_claim(
            "arithmetic", lhs="2 + 2", rhs="4")))
        self.assertFalse(self._review(_claim(
            "expansion_identity", lhs="(x + y)**2",
            rhs="x**2 + 2*x*y + y**2")))

    def test_everything_else_requires_review(self):
        cases = [
            # verified but approximate
            _claim("approximate_equality", lhs="sqrt(2)", rhs="1.4142135",
                   tolerance=10 ** -5),
            # verified but without sources
            _claim("arithmetic", lhs="2 + 2", rhs="4",
                   source_references=[]),
            # contradicted
            _claim("arithmetic", lhs="2 + 2", rhs="5"),
            # unsupported by the parser
            _claim("algebraic_equivalence", lhs="2x", rhs="x + x"),
            # inconclusive
            _claim("equation_solutions", equation="sqrt(x) = x - 2",
                   solutions=["4"]),
        ]
        for claim in cases:
            self.assertTrue(self._review(claim), claim["claim_id"])


if __name__ == "__main__":
    unittest.main()
