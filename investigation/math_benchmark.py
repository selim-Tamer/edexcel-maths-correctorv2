"""Stage C4 synthetic mathematical benchmark (investigation-only).

Independent of every existing benchmark: this file shares NO data with
the C2 accuracy/verification/case-governance fixtures or any Phase C1-C3
fixture. None of the cases here represents real Pearson material or any
marking policy: synthetic mathematical correctness is kept strictly
separate from official examination marking.

Run directly to evaluate every case through the SAME public verification
entry points (``verify_claim``) that callers use, printing only
deterministic summary counts. Exit code 0 requires every expected
outcome to match.
"""

from investigation.math_verification import (
    OUTCOME_CONTRADICTED, OUTCOME_INCONCLUSIVE, OUTCOME_UNSUPPORTED,
    OUTCOME_VERIFIED, verify_claim,
)

BENCHMARK_SCHEMA = "investigation.math_benchmark/1"

_REF = {"document_id": "pdf:benchmark-ref", "document_role": "mark_scheme",
        "sha256": "0123456789abcdef" * 4, "page": 1,
        "label": "benchmark page"}

# (claim dict, expected outcome). claim_id pattern: MBC-<nn>.
CASES = (
    # 01 correct arithmetic
    (dict(claim_id="MBC-01", kind="arithmetic", lhs="2/4 + 1/3", rhs="5/6",
          source_references=[dict(_REF)]), OUTCOME_VERIFIED),
    # 02 incorrect final answer (arithmetic)
    (dict(claim_id="MBC-02", kind="arithmetic", lhs="3/4", rhs="2/3",
          source_references=[dict(_REF)]), OUTCOME_CONTRADICTED),
    # 03 algebraic equivalence, correct
    (dict(claim_id="MBC-03", kind="algebraic_equivalence",
          lhs="(x - 2)*(x + 3)", rhs="x**2 + x - 6",
          source_references=[]), OUTCOME_VERIFIED),
    # 04 algebraic equivalence, wrong
    (dict(claim_id="MBC-04", kind="algebraic_equivalence",
          lhs="(x - 2)*(x + 3)", rhs="x**2 + x + 6",
          source_references=[dict(_REF)]), OUTCOME_CONTRADICTED),
    # 05 valid alternative algebraic form
    (dict(claim_id="MBC-05", kind="algebraic_equivalence",
          lhs="x**2 - 9", rhs="(x - 3)*(x + 3)",
          source_references=[]), OUTCOME_VERIFIED),
    # 06 incorrect expansion
    (dict(claim_id="MBC-06", kind="expansion_identity",
          lhs="(x + 2)**2", rhs="x**2 + 4",
          source_references=[dict(_REF)]), OUTCOME_CONTRADICTED),
    # 07 correct expansion
    (dict(claim_id="MBC-07", kind="expansion_identity",
          lhs="(x + 2)**2", rhs="x**2 + 4*x + 4",
          source_references=[]), OUTCOME_VERIFIED),
    # 08 incorrect factorisation
    (dict(claim_id="MBC-08", kind="factorisation_identity",
          lhs="x**2 - 4", rhs="(x - 2)**2",
          source_references=[dict(_REF)]), OUTCOME_CONTRADICTED),
    # 09 equivalent fractions (exact rational identity)
    (dict(claim_id="MBC-09", kind="fraction_equivalence",
          lhs="(3*x - 3)/9", rhs="x/3 - 1/3",
          source_references=[]), OUTCOME_VERIFIED),
    # 10 invalid cancellation
    (dict(claim_id="MBC-10", kind="fraction_equivalence",
          lhs="(x**2 + 2*x + 1)/(x + 1)", rhs="x",
          source_references=[dict(_REF)]), OUTCOME_CONTRADICTED),
    # 11 division-by-zero restriction (domain note on a true identity)
    (dict(claim_id="MBC-11", kind="fraction_equivalence",
          lhs="3/(x - 2)", rhs="3*(x + 2)/(x**2 - 4)",
          source_references=[dict(_REF)]), OUTCOME_VERIFIED),
    # 12 exact surd form, correct
    (dict(claim_id="MBC-12", kind="surd_equality", lhs="sqrt(50)",
          rhs="5*sqrt(2)", source_references=[]), OUTCOME_VERIFIED),
    # 13 exact surd form, incorrect
    (dict(claim_id="MBC-13", kind="surd_equality", lhs="sqrt(45)",
          rhs="6*sqrt(2)", source_references=[dict(_REF)]),
     OUTCOME_CONTRADICTED),
    # 14 quadratic roots, correct complete set
    (dict(claim_id="MBC-14", kind="quadratic_solutions",
          equation="x**2 - 5*x + 6 = 0", solutions=["2", "3"],
          source_references=[]), OUTCOME_VERIFIED),
    # 15 extraneous solution rejected by substitution
    (dict(claim_id="MBC-15", kind="equation_solutions",
          equation="sqrt(x) = x - 2", solutions=["1", "4"],
          source_references=[dict(_REF)]), OUTCOME_CONTRADICTED),
    # 16 domain restriction needing a stated assumption that is absent:
    # sqrt(x^2) == x is NOT an identity without x >= 0
    (dict(claim_id="MBC-16", kind="algebraic_equivalence",
          lhs="sqrt(x**2)", rhs="x",
          source_references=[dict(_REF)]), OUTCOME_INCONCLUSIVE),
    # 17 same transformation verified once the assumption is stated
    (dict(claim_id="MBC-17", kind="transformation_with_assumptions",
          lhs="sqrt(x**2)", rhs="x", assumptions=["x > 0"],
          source_references=[]), OUTCOME_VERIFIED),
    # 18 undeclared assumption behind an invalid claim: abs(x) == -x
    # produces no verified outcome without a real domain restriction
    (dict(claim_id="MBC-18", kind="transformation_with_assumptions",
          lhs="abs(x)", rhs="-x", assumptions=["x > 0"],
          source_references=[dict(_REF)]), OUTCOME_INCONCLUSIVE),
    # 19 approximate numerical equality (never a proof of exact identity)
    (dict(claim_id="MBC-19", kind="approximate_equality", lhs="sqrt(2)",
          rhs="1.4142135", tolerance=10 ** -5,
          source_references=[]), OUTCOME_VERIFIED),
    # 20 approximate value that disagrees beyond tolerance
    (dict(claim_id="MBC-20", kind="approximate_equality", lhs="sqrt(2)",
          rhs="1.5", tolerance=10 ** -5,
          source_references=[dict(_REF)]), OUTCOME_CONTRADICTED),
    # 21 ambiguous/unsupported notation (implicit product "2x")
    (dict(claim_id="MBC-21", kind="algebraic_equivalence", lhs="2x",
          rhs="x + x", source_references=[]), OUTCOME_UNSUPPORTED),
    # 22 malicious input rejected outright (attribute access)
    (dict(claim_id="MBC-22", kind="algebraic_equivalence",
          lhs='x.__class__', rhs="x",
          source_references=[]), OUTCOME_UNSUPPORTED),
    # 23 excessive complexity refused rather than computed
    (dict(claim_id="MBC-23", kind="algebraic_equivalence",
          lhs="*".join(f"(x + {i})**3" for i in range(1, 8)), rhs="0",
          source_references=[]), OUTCOME_UNSUPPORTED),
    # 24 substitution, correct
    (dict(claim_id="MBC-24", kind="substitution", expression="x**2 + 1",
          assignments={"x": "2"}, claimed_value="5",
          source_references=[]), OUTCOME_VERIFIED),
    # 25 substitution, incorrect claimed value
    (dict(claim_id="MBC-25", kind="substitution", expression="x**2 + 1",
          assignments={"x": "2"}, claimed_value="7",
          source_references=[dict(_REF)]), OUTCOME_CONTRADICTED),
    # 26 valid statement that must not error anywhere
    (dict(claim_id="MBC-26", kind="expansion_identity", lhs="(x + y)**2",
          rhs="x**2 + 2*x*y + y**2", source_references=[]), OUTCOME_VERIFIED),
    # 27 missing source references still verified but flagged for review
    (dict(claim_id="MBC-27", kind="arithmetic", lhs="2 + 2", rhs="4",
          source_references=[]), OUTCOME_VERIFIED),
    # 28 arithmetic misuse: variables present -> unsupported, not a guess
    (dict(claim_id="MBC-28", kind="arithmetic", lhs="x + 1", rhs="4",
          source_references=[]), OUTCOME_UNSUPPORTED),
)

REVIEW_REQUIRED_EXPECTATIONS = {
    # cases whose verified outcome must still demand teacher review
    "MBC-11",   # domain restriction on a nominally verified identity
    "MBC-17",   # identity holds ONLY under the stated assumption
    "MBC-19",   # approximate equality is not an exact identity proof
    "MBC-27",   # verified but without any source reference
}


def evaluate_case(case):
    """Run one case through the public verification path."""
    claim, expected = case
    return verify_claim(claim), expected


def run_benchmark():
    """Evaluate every case; return (results, summary) deterministically."""
    results = []
    mismatches = []
    for case in CASES:
        result, expected = evaluate_case(case)
        result = dict(result)
        result["expected_outcome"] = expected
        result["outcome_matches"] = result["outcome"] == expected
        if not result["outcome_matches"]:
            mismatches.append(result["claim_id"])
        if result["claim_id"] in REVIEW_REQUIRED_EXPECTATIONS \
                and not result["review_required"]:
            mismatches.append(result["claim_id"] + ":review")
        results.append(result)
    summary = {
        "schema": BENCHMARK_SCHEMA,
        "cases": len(CASES),
        "outcome_matches": all(item["outcome_matches"] for item in results),
        "review_flags_respected": not any(
            item.endswith(":review") for item in mismatches),
        "mismatches": sorted(mismatches),
        "verification_engine": "investigation.math_verification",
        "separation": "synthetic mathematical correctness only; no "
                      "marking policy, no official-document claim",
    }
    return results, summary


def main():
    import json
    results, summary = run_benchmark()
    json.dumps(summary)
    print(json.dumps(summary, indent=2))
    if not summary["outcome_matches"] \
            or not summary["review_flags_respected"]:
        return 1
    return 0


if __name__ == "__main__":          # pragma: no cover - invoked manually
    raise SystemExit(main())
