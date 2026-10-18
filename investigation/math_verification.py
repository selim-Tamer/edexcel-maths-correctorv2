"""Offline Sympy-backed mathematical verification engine (Stage C4).

Investigation-only. NOT imported by the production app; imports nothing
from it. No network, no AI calls, no disk writes, no execution of any
untrusted string, no ``eval``/``exec``/``sympify``/``parse_expr``.

Every claim is parsed by :mod:`investigation.safe_math` (validated AST,
whitelisted grammar, bounded complexity). Verification uses only bounded,
deterministic symbolic operations:

- exact canonical difference (``cancel`` guarded by the combinatorial
  expand bound)
- bounded rational pole extraction for domain notes
- bounded polynomial solving (``sympy.roots``) for degree <= 4
- ``powsimp``/``radsimp`` for surd forms and ``refine`` for explicitly
  stated assumptions — never used without the structural input bounds
- 50-digit numeric evaluation ONLY for explicitly approximate claims,
  never as an exactness proof

Outcomes are explicit states only (no numeric confidence): ``verified``,
``contradicted``, ``inconclusive``, ``unsupported``.

Soundness rules
===============

- A zero canonical difference is exact proof of identity within the
  declared bounded operation class.
- A nonzero canonical difference proves a contradiction ONLY for
  constant/rational/polynomial classes; for surd/``abs`` forms it stays
  ``inconclusive``.
- Equivalence is never claimed from finite numeric sampling; numeric
  evaluation is limited to explicitly approximate claims and always
  requires teacher review.
- Parser refusals map to ``unsupported`` with the refusal reason kept.
- A mathematical contradiction applies to the submitted claim only; it
  is never, by itself, a statement about any official document.
"""

import hashlib

from investigation.safe_math import (
    DEFAULT_VARIABLES, ExpressionRefused, canonical_text,
    expand_is_bounded, parse_bounded_expression, validate_variables,
)

RESULT_SCHEMA = "investigation.math_verification/1"

OUTCOME_VERIFIED = "verified"
OUTCOME_CONTRADICTED = "contradicted"
OUTCOME_INCONCLUSIVE = "inconclusive"
OUTCOME_UNSUPPORTED = "unsupported"
OUTCOMES = (
    OUTCOME_VERIFIED, OUTCOME_CONTRADICTED,
    OUTCOME_INCONCLUSIVE, OUTCOME_UNSUPPORTED,
)

KIND_ARITHMETIC = "arithmetic"
KIND_EQUIVALENCE = "algebraic_equivalence"
KIND_EXPANSION = "expansion_identity"
KIND_FACTORISATION = "factorisation_identity"
KIND_FRACTIONS = "fraction_equivalence"
KIND_SURDS = "surd_equality"
KIND_EQUATION = "equation_solutions"
KIND_QUADRATIC = "quadratic_solutions"
KIND_SUBSTITUTION = "substitution"
KIND_ASSUMPTIONS = "transformation_with_assumptions"
KIND_APPROXIMATE = "approximate_equality"

CHECK_KINDS = (
    KIND_ARITHMETIC, KIND_EQUIVALENCE, KIND_EXPANSION, KIND_FACTORISATION,
    KIND_FRACTIONS, KIND_SURDS, KIND_EQUATION, KIND_QUADRATIC,
    KIND_SUBSTITUTION, KIND_ASSUMPTIONS, KIND_APPROXIMATE,
)

EXACT_KIND = "exact"
APPROXIMATE_KIND = "approximate"

CLAIM_SCOPE_NOTE = (
    "A mathematical contradiction applies only to the submitted claim; "
    "it is not by itself a statement that any official document, mark "
    "scheme or examination paper is incorrect.")

DEFAULT_TOLERANCE = 10 ** -5
MAX_CLAIM_ID_CHARS = 100
MAX_SOLUTIONS = 8
MAX_ASSIGNMENTS = 6
MAX_ASSUMPTIONS = 6
MAX_DEGREE = 4

_REF_KEYS = {"document_id", "document_role", "sha256", "page", "label"}

_KIND_KEYS = {
    KIND_ARITHMETIC: {"claim_id", "kind", "lhs", "rhs", "source_references"},
    KIND_EQUIVALENCE: {"claim_id", "kind", "lhs", "rhs", "source_references"},
    KIND_EXPANSION: {"claim_id", "kind", "lhs", "rhs", "source_references"},
    KIND_FACTORISATION: {"claim_id", "kind", "lhs", "rhs",
                         "source_references"},
    KIND_FRACTIONS: {"claim_id", "kind", "lhs", "rhs", "source_references"},
    KIND_SURDS: {"claim_id", "kind", "lhs", "rhs", "source_references"},
    KIND_EQUATION: {"claim_id", "kind", "equation", "solutions",
                    "source_references"},
    KIND_QUADRATIC: {"claim_id", "kind", "equation", "solutions",
                     "source_references"},
    KIND_SUBSTITUTION: {"claim_id", "kind", "expression", "assignments",
                        "claimed_value", "source_references"},
    KIND_ASSUMPTIONS: {"claim_id", "kind", "lhs", "rhs", "assumptions",
                       "source_references"},
    KIND_APPROXIMATE: {"claim_id", "kind", "lhs", "rhs", "tolerance",
                       "source_references"},
}


class _EngineUnsupported(Exception):
    """Internal guard: the requested check is inapplicable to this claim."""


def _validate_claim_id(claim_id):
    if not isinstance(claim_id, str) or not claim_id.strip() \
            or len(claim_id) > MAX_CLAIM_ID_CHARS \
            or claim_id.strip() != claim_id:
        raise ValueError(
            "claim_id must be a bounded trimmed non-empty string")
    return claim_id


def _validate_source_references(references):
    if references is None:
        return []
    if not isinstance(references, (list, tuple)):
        raise ValueError("source_references must be a list")
    validated = []
    for reference in references:
        if not isinstance(reference, dict) \
                or set(reference) not in (_REF_KEYS, _REF_KEYS | {"excerpt"}):
            raise ValueError(
                "source reference needs id/role/sha256/page/label keys")
        if not isinstance(reference["document_id"], str) \
                or not reference["document_id"].strip() \
                or len(reference["document_id"]) > 100:
            raise ValueError("source reference document_id is invalid")
        if reference["document_role"] not in ("question_paper",
                                              "mark_scheme"):
            raise ValueError("source reference document_role is invalid")
        digest = reference["sha256"]
        if (not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdef"
                       for character in digest)):
            raise ValueError("source reference sha256 is invalid")
        if len(digest) != len(hashlib.sha256(b"").hexdigest()):
            raise AssertionError("digest length invariant broken")
        if type(reference["page"]) is not int or reference["page"] < 1:
            raise ValueError("source reference page must be a 1-based int")
        if not isinstance(reference["label"], str) \
                or not reference["label"].strip() \
                or len(reference["label"]) > 120:
            raise ValueError("source reference label is invalid")
        item = {key: reference[key] for key in _REF_KEYS}
        if "excerpt" in reference:
            excerpt = reference["excerpt"]
            if not isinstance(excerpt, str) or not excerpt \
                    or len(excerpt) > 240:
                raise ValueError("source reference excerpt is invalid")
            item["excerpt"] = excerpt
        validated.append(item)
    return validated


def _numeric_poles(expression):
    """Exact denominator roots of a rational expression, degree-bounded.

    Returns ``(sorted_canonical_poles, analysis_incomplete)``.
    """
    from sympy import together, Poly, PolynomialError, roots
    try:
        denominator = together(expression).as_numer_denom()[1]
    except Exception:
        return [], True                      # non-rational shape
    if denominator == 1 or denominator.is_number:
        return [], False
    try:
        poly = Poly(denominator)
        if poly.total_degree() > MAX_DEGREE:
            return [], True
        found = {}
        for root, multiplicity in roots(poly).items():
            found[canonical_text(root)] = multiplicity
        return sorted(found), False
    except (PolynomialError, ValueError, TypeError):
        return [], True


def _domain_notes_for_pair(lhs, rhs):
    """Domain compatibility notes recorded alongside an identity."""
    lhs_poles, lhs_incomplete = _numeric_poles(lhs)
    rhs_poles, rhs_incomplete = _numeric_poles(rhs)
    notes = []
    if lhs_incomplete or rhs_incomplete:
        notes.append(
            "pole analysis incomplete: a denominator exceeded the "
            "engine's bounded degree cap, so domain conditions could "
            "not be fully resolved")
    extra_lhs = [pole for pole in lhs_poles if pole not in rhs_poles]
    extra_rhs = [pole for pole in rhs_poles if pole not in lhs_poles]
    if extra_lhs:
        notes.append(
            "the identity holds on the common domain only: the "
            "left-hand side is undefined at the excluded value(s) "
            + ", ".join(extra_lhs))
    if extra_rhs:
        notes.append(
            "the identity holds on the common domain only: the "
            "right-hand side is undefined at the excluded value(s) "
            + ", ".join(extra_rhs))
    return notes, extra_lhs, extra_rhs


def _require_constants(lhs, rhs, kind):
    if lhs.free_symbols or rhs.free_symbols:
        raise _EngineUnsupported(
            f"the {kind} check compares constant values; the claim "
            "contains variables, so use an equivalence-family check")


def _approximately_compare(lhs, rhs, tolerance, result):
    result["verification_kind"] = APPROXIMATE_KIND
    tol = DEFAULT_TOLERANCE if tolerance is None else tolerance
    if isinstance(tol, bool) or not isinstance(tol, (int, float)) \
            or not 0 < tol <= 1:
        raise ValueError("tolerance must satisfy 0 < tolerance <= 1")
    evaluated_lhs = lhs.evalf(50)
    evaluated_rhs = rhs.evalf(50)
    difference = evaluated_lhs - evaluated_rhs
    evidence = {
        "evaluated_lhs": str(evaluated_lhs),
        "evaluated_rhs": str(evaluated_rhs),
        "rendered_difference": str(difference),
        "tolerance": tol,
        "digits": 50,
        "numeric_note": "this is bounded numeric evidence only; it is "
                        "not an exact identity proof",
    }
    if not difference.is_number:
        evidence["numeric_evaluation_not_conclusive"] = True
        return OUTCOME_INCONCLUSIVE, evidence
    if abs(difference) <= tol:
        return OUTCOME_VERIFIED, evidence
    evidence["within_stated_tolerance"] = False
    return OUTCOME_CONTRADICTED, evidence


def _parse_side(text, whitelist):
    if not isinstance(text, str):
        raise ExpressionRefused("not_a_string")
    return parse_bounded_expression(text, whitelist)["lhs"]


def parse_stated_assumption(text, whitelist):
    """Parse one bounded assumption ``expr REL expr`` into a Q predicate.

    Returns ``(predicate, canonical_difference_text, rel)``. The predicate
    is built from the DIFFERENCE of both sides, so ``x > -3`` becomes
    ``Q.positive(x + 3)``.
    """
    if not isinstance(text, str) or not text.strip() or len(text) > 100:
        raise ExpressionRefused("unsupported_syntax", "assumption")
    relation = None
    for rel in ("<=", ">=", "!=", "<", ">"):
        if rel in text:
            relation = rel
            break
    if relation is None:
        raise ExpressionRefused("unsupported_syntax",
                                "no comparison relation")
    left_text, right_text = text.split(relation, 1)
    if not left_text.strip() or not right_text.strip():
        raise ExpressionRefused("unsupported_syntax", "assumption side")
    left = _parse_side(left_text.strip(), whitelist)
    right = _parse_side(right_text.strip(), whitelist)
    difference = left - right
    if not difference.free_symbols:
        raise _EngineUnsupported(
            "the assumption compares two constants and states nothing "
            "about the claim variables")
    from sympy import Q
    predicate = getattr(Q, {"<": "negative", ">": "positive",
                            "<=": "nonpositive", ">=": "nonnegative",
                            "!=": "nonzero"}[relation])
    return predicate(difference), canonical_text(difference), relation


def _parse_claimed_solutions(solutions_text, whitelist):
    if not isinstance(solutions_text, (list, tuple)) \
            or not 1 <= len(solutions_text) <= MAX_SOLUTIONS:
        raise ValueError(
            f"solutions must be a list of 1 to {MAX_SOLUTIONS} strings")
    parsed = []
    for item in solutions_text:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("each claimed solution must be a string")
        parsed.append(parse_bounded_expression(item, whitelist)["lhs"])
    if len({canonical_text(item) for item in parsed}) != len(parsed):
        raise ValueError("claimed solutions repeat")
    return parsed


def _input_record(lhs, rhs, has_floats, extra=None):
    record = {
        "canonical_lhs": canonical_text(lhs),
        "canonical_rhs": canonical_text(rhs),
        "rendered_lhs": str(lhs),
        "rendered_rhs": str(rhs),
        "variables": sorted(str(symbol)
                            for symbol in lhs.free_symbols | rhs.free_symbols),
        "has_floats": has_floats,
    }
    if extra:
        record.update(extra)
    return record


def _verify_pair(claim, whitelist, result):
    lhs_record = parse_bounded_expression(claim["lhs"], whitelist)
    rhs_record = parse_bounded_expression(claim["rhs"], whitelist)
    lhs, rhs = lhs_record["lhs"], rhs_record["lhs"]
    has_floats = lhs_record["has_floats"] or rhs_record["has_floats"]
    result["verification_kind"] = APPROXIMATE_KIND if has_floats \
        else EXACT_KIND
    result["mathematical_input"] = _input_record(
        lhs, rhs, has_floats,
        {"lhs_text": claim["lhs"], "rhs_text": claim["rhs"]})
    return lhs, rhs, has_floats


def _identity_outcome(lhs, rhs, result, method_description):
    """Bounded canonical-identity decision shared by equivalence checks."""
    result["method_description"] = method_description
    diff_expression = lhs - rhs
    if not expand_is_bounded(diff_expression):
        raise _EngineUnsupported(
            "the expanded difference would exceed the bounded term "
            "budget; the claim is left inconclusive rather than risking "
            "unbounded symbolic work")
    from sympy import cancel, together
    canonical = cancel(diff_expression)
    if canonical == 0:
        notes, extra_lhs, extra_rhs = _domain_notes_for_pair(lhs, rhs)
        result["domain_notes"] = notes
        return OUTCOME_VERIFIED, {
            "canonical_difference": "0",
            "extra_poles_lhs": extra_lhs,
            "extra_poles_rhs": extra_rhs}
    numer, denominator = together(canonical).as_numer_denom()
    if denominator != 1 and numer == 0:
        notes, extra_lhs, extra_rhs = _domain_notes_for_pair(lhs, rhs)
        result["domain_notes"] = notes
        return OUTCOME_VERIFIED, {
            "canonical_difference": "0",
            "extra_poles_lhs": extra_lhs,
            "extra_poles_rhs": extra_rhs}
    from sympy import Poly, PolynomialError
    variables = tuple(sorted(lhs.free_symbols | rhs.free_symbols, key=str))
    evidence_base = {
        "canonical_difference": canonical_text(canonical),
        "rendered_difference": str(canonical)}
    try:
        poly = Poly(numer, *variables) if variables else None
    except (PolynomialError, ValueError, TypeError):
        poly = None
    if poly is not None:
        degree = poly.total_degree()
        if degree <= MAX_DEGREE:
            evidence_base["polynomial_degree"] = degree
            if not poly.is_zero:
                return OUTCOME_CONTRADICTED, evidence_base
        else:
            evidence_base["polynomial_degree_exceeds_bound"] = degree
            evidence_base["exceeds_engine_bound"] = True
            return OUTCOME_INCONCLUSIVE, evidence_base
    if canonical.is_number:
        return OUTCOME_CONTRADICTED, evidence_base
    return OUTCOME_INCONCLUSIVE, evidence_base


def _verify_arithmetic(claim, whitelist, result):
    lhs, rhs, has_floats = _verify_pair(claim, whitelist, result)
    result["method_description"] = "exact_rational_difference"
    _require_constants(lhs, rhs, "arithmetic")
    if has_floats:
        return _approximately_compare(lhs, rhs, claim.get("tolerance"),
                                      result)
    from sympy import cancel
    difference = cancel(lhs - rhs)
    if difference == 0:
        result["verification_kind"] = EXACT_KIND
        return OUTCOME_VERIFIED, {"canonical_difference": "0",
                                  "rendered_difference": "0"}
    result["verification_kind"] = EXACT_KIND
    return OUTCOME_CONTRADICTED, {
        "canonical_difference": canonical_text(difference),
        "rendered_difference": str(difference)}


def _verify_equivalence(claim, whitelist, result):
    lhs, rhs, has_floats = _verify_pair(claim, whitelist, result)
    if has_floats:
        return _approximately_compare(lhs, rhs, claim.get("tolerance"),
                                      result)
    return _identity_outcome(lhs, rhs, result, "exact_canonical_difference")


def _verify_expansion(claim, whitelist, result):
    lhs, rhs, has_floats = _verify_pair(claim, whitelist, result)
    if has_floats:
        return _approximately_compare(lhs, rhs, claim.get("tolerance"),
                                      result)
    return _identity_outcome(lhs, rhs, result,
                             "bounded_expand_and_canonical_difference")


def _verify_factorisation(claim, whitelist, result):
    lhs, rhs, has_floats = _verify_pair(claim, whitelist, result)
    if has_floats:
        return _approximately_compare(lhs, rhs, claim.get("tolerance"),
                                      result)
    return _identity_outcome(lhs, rhs, result,
                             "factorisation_verified_by_expansion")


def _verify_fractions(claim, whitelist, result):
    lhs, rhs, has_floats = _verify_pair(claim, whitelist, result)
    if has_floats:
        return _approximately_compare(lhs, rhs, claim.get("tolerance"),
                                      result)
    return _identity_outcome(lhs, rhs, result,
                             "bounded_rational_canonicalisation")


def _verify_surds(claim, whitelist, result):
    from sympy import radsimp, powsimp, cancel
    lhs, rhs, has_floats = _verify_pair(claim, whitelist, result)
    if has_floats:
        return _approximately_compare(lhs, rhs, claim.get("tolerance"),
                                      result)
    result["method_description"] = "bounded_radical_canonicalisation"
    diff_expression = lhs - rhs
    strategy = None
    for candidate, label in (
            (diff_expression, "raw"),
            (radsimp(diff_expression), "radsimp"),
            (powsimp(radsimp(diff_expression), deep=False),
             "radsimp+powsimp")):
        if not expand_is_bounded(candidate):
            continue
        strategy = label
        canonical = cancel(candidate)
        if canonical == 0:
            break
    if strategy is None:
        raise _EngineUnsupported(
            "radical canonicalisation candidates exceeded the bounded "
            "term budget")
    if canonical == 0:
        return OUTCOME_VERIFIED, {
            "canonicalisation": strategy, "canonical_difference": "0"}
    evidence = {
        "canonicalisation": strategy,
        "canonical_difference": canonical_text(canonical),
        "rendered_difference": str(canonical)}
    if canonical.is_number:
        return OUTCOME_CONTRADICTED, evidence
    evidence["bounded_operations_were_insufficient"] = True
    return OUTCOME_INCONCLUSIVE, evidence


def _equation_diff(claim, whitelist, result, method_description):
    equation_record = parse_bounded_expression(
        claim["equation"], whitelist, allow_equation=True)
    lhs, rhs = equation_record["lhs"], equation_record["rhs"]
    if equation_record["has_floats"]:
        raise _EngineUnsupported(
            "equation verification requires exact coefficients; "
            "approximate (float) equations are not solved in this stage")
    diff_expression = lhs - rhs
    result["verification_kind"] = EXACT_KIND
    result["mathematical_input"] = _input_record(
        lhs, rhs, False,
        {"equation_text": claim["equation"],
         "claimed_solutions_text": list(claim["solutions"])})
    result["method_description"] = method_description
    return diff_expression, tuple(sorted(lhs.free_symbols | rhs.free_symbols,
                                         key=str))


def _verify_equation_solutions(claim, whitelist, result):
    from sympy import Poly, PolynomialError, roots as sympy_roots
    is_quadratic = claim["kind"] == KIND_QUADRATIC
    diff_expression, variable_and_others = _equation_diff(
        claim, whitelist, result,
        "quadratic_solution_verification" if is_quadratic
        else "bounded_polynomial_solution_check")
    if len(variable_and_others) != 1:
        raise _EngineUnsupported(
            "solution verification is supported for single-variable "
            "polynomial equations only")
    variable = variable_and_others[0]
    claimed_list = _parse_claimed_solutions(claim["solutions"], whitelist)
    root_reports = []
    all_roots_confirmed = True
    for candidate_root in claimed_list:
        if candidate_root.free_symbols:
            raise _EngineUnsupported(
                "claimed solutions must be exact constants in this stage")
        if candidate_root.is_Float:
            raise _EngineUnsupported(
                "claimed solutions must be exact; approximate floats are "
                "not accepted as solutions in this stage")
        value = diff_expression.subs(variable, candidate_root)
        if value == 0:
            root_reports.append({"solution": canonical_text(candidate_root),
                                 "status": "root_confirmed",
                                 "residual": "0"})
        elif value.is_number:
            all_roots_confirmed = False
            root_reports.append({"solution": canonical_text(candidate_root),
                                 "status": "root_rejected",
                                 "residual": str(value)})
        else:
            all_roots_confirmed = False
            root_reports.append({"solution": canonical_text(candidate_root),
                                 "status": "root_unresolved",
                                 "residual": canonical_text(value)})
    evidence = {"claimed_solutions_checked": root_reports}
    if not all_roots_confirmed:
        rejected = [entry for entry in root_reports
                    if entry["status"] != "root_confirmed"]
        evidence["rejected_solutions"] = rejected
        return OUTCOME_CONTRADICTED, evidence
    poly = None
    try:
        poly = Poly(diff_expression, variable)
    except (PolynomialError, ValueError, TypeError):
        poly = None
    if poly is None or poly.total_degree() > MAX_DEGREE:
        evidence["completeness"] = (
            "completeness was not established: the equation is outside "
            "the engine's bounded polynomial class (degree cap "
            f"{MAX_DEGREE}); claimed solutions are valid roots")
        return OUTCOME_INCONCLUSIVE, evidence
    if is_quadratic and poly.total_degree() not in (1, 2):
        raise _EngineUnsupported(
            "the quadratic check requires an equation of degree 1 or 2")
    computed = sorted((canonical_text(root) for root, multiplicity
                       in sympy_roots(poly).items()))
    claimed_texts = sorted(canonical_text(item) for item in claimed_list)
    evidence["computed_solution_set"] = computed
    evidence["claimed_solution_set"] = claimed_texts
    if computed == claimed_texts:
        evidence["completeness"] = (
            "solution set verified: the bounded polynomial solve found "
            "exactly the claimed solution set")
        return OUTCOME_VERIFIED, evidence
    evidence["completeness"] = (
        "the bounded polynomial solve did not agree with the claimed "
        "solution set")
    return OUTCOME_CONTRADICTED, evidence


def _verify_substitution(claim, whitelist, result):
    from sympy import cancel
    expression_record = parse_bounded_expression(
        claim["expression"], whitelist)
    claimed_record = parse_bounded_expression(
        claim["claimed_value"], whitelist)
    if expression_record["has_floats"] or claimed_record["has_floats"]:
        raise _EngineUnsupported(
            "substitution verification requires exact claimed values")
    expression = expression_record["lhs"]
    claimed_value = claimed_record["lhs"]
    result["verification_kind"] = EXACT_KIND
    result["method_description"] = "exact_substitution_evaluation"
    assignments = claim["assignments"]
    if not isinstance(assignments, dict) or not assignments \
            or len(assignments) > MAX_ASSIGNMENTS:
        raise ValueError(
            f"assignments must map 1 to {MAX_ASSIGNMENTS} variable names")
    if claimed_value.free_symbols:
        raise _EngineUnsupported(
            "the claimed substituted value must be a constant in this "
            "stage")
    substituted = expression
    assignment_records = []
    for variable_name, value_text in assignments.items():
        if not isinstance(variable_name, str) \
                or variable_name not in whitelist:
            raise ValueError("assignments use whitelisted variable names")
        value = _parse_side(value_text, whitelist)
        if value.free_symbols:
            raise _EngineUnsupported(
                "assignment values must be constants in this stage")
        substituted = substituted.subs(variable_name, value)
        assignment_records.append({"variable": variable_name,
                                   "value": canonical_text(value)})
    result["mathematical_input"] = _input_record(
        expression, claimed_value, False,
        {"expression_text": claim["expression"],
         "claimed_value_text": claim["claimed_value"],
         "assignments": assignment_records})
    if substituted.free_symbols:
        raise _EngineUnsupported(
            "not every variable was assigned; the residual still "
            "contains free symbols, so the claim cannot be evaluated")
    difference = cancel(substituted - claimed_value)
    if difference == 0:
        return OUTCOME_VERIFIED, {
            "substituted_value": str(substituted),
            "claimed_value": str(claimed_value),
            "canonical_difference": "0"}
    return OUTCOME_CONTRADICTED, {
        "substituted_value": str(substituted),
        "claimed_value": str(claimed_value),
        "canonical_difference": canonical_text(difference),
        "rendered_difference": str(difference)}


def _verify_assumptions(claim, whitelist, result):
    from sympy import And, refine, cancel
    lhs, rhs, has_floats = _verify_pair(claim, whitelist, result)
    if has_floats:
        return _approximately_compare(lhs, rhs, claim.get("tolerance"),
                                      result)
    result["method_description"] = "assumption_scoped_refinement"
    assumptions = claim["assumptions"]
    if not isinstance(assumptions, (list, tuple)) \
            or 1 > len(assumptions) or len(assumptions) > MAX_ASSUMPTIONS:
        raise ValueError(
            f"assumptions must list 1 to {MAX_ASSUMPTIONS} strings")
    predicates = []
    canonical_assumptions = []
    for assumption_text in assumptions:
        if not isinstance(assumption_text, str):
            raise ExpressionRefused("not_a_string")
        predicate, canonical, relation = parse_stated_assumption(
            assumption_text, whitelist)
        predicates.append(predicate)
        canonical_assumptions.append(
            {"statement": assumption_text.strip(),
             "predicate_on": canonical, "relation": relation})
    result["assumptions"] = canonical_assumptions
    diff_expression = lhs - rhs
    if not expand_is_bounded(diff_expression):
        raise _EngineUnsupported(
            "the difference exceeds the bounded term budget")
    unconstrained = cancel(diff_expression)
    evidence = {"stated_assumptions": canonical_assumptions}
    if unconstrained == 0:
        evidence["assumption_dependency"] = "none_required"
        return OUTCOME_VERIFIED, evidence
    constrained = refine(diff_expression, And(*predicates))
    if constrained == 0:
        evidence["assumption_dependency"] = (
            "the identity holds ONLY under the stated assumptions; "
            "without them it is NOT an identity")
        return OUTCOME_VERIFIED, evidence
    evidence["refined_difference"] = canonical_text(constrained)
    evidence["rendered_difference"] = str(constrained)
    evidence["assumption_dependency"] = (
        "the claim was not reduced to zero under the stated assumptions "
        "by the bounded refinement operation; inconclusive, not "
        "contradicted")
    return OUTCOME_INCONCLUSIVE, evidence


def _verify_approximate(claim, whitelist, result):
    lhs, rhs, _ = _verify_pair(claim, whitelist, result)
    result["method_description"] = "bounded_numeric_evaluation"
    return _approximately_compare(lhs, rhs, claim["tolerance"], result)


def _explanation_for(outcome, result):
    scope = (" " + CLAIM_SCOPE_NOTE) if outcome == OUTCOME_CONTRADICTED else ""
    if outcome == OUTCOME_VERIFIED:
        if result["verification_kind"] == APPROXIMATE_KIND:
            return ("The claim is consistent within the stated tolerance "
                    "under 50-digit numeric evaluation; numeric agreement "
                    "is not an exact identity proof, so teacher review "
                    "remains required." + scope)
        text = ("The claim was verified by exact bounded symbolic "
                "canonicalisation")
        if result["assumptions"]:
            text += (" under the stated assumptions: "
                     + ", ".join(entry["statement"]
                                 for entry in result["assumptions"]))
        if result["domain_notes"]:
            text += ("; the identity holds on the common domain recorded "
                     "in the domain notes")
        return text + "." + scope
    if outcome == OUTCOME_CONTRADICTED:
        return ("The claim was contradicted by exact bounded canonical "
                "evaluation; the recorded evidence shows a nonzero "
                "difference or a rejected value." + scope)
    if outcome == OUTCOME_UNSUPPORTED:
        return ("The claim could not be verified: the parser or the "
                "requested check refused it for a recorded structural "
                "reason; nothing mathematical was asserted either way.")
    return ("The available bounded exact operations could not certify "
            "or refute the claim; inconclusive means evidence is "
            "insufficient, not that either side is wrong.")


def verify_claim(claim, variable_whitelist=None):
    """Verify one mathematical claim; return the result contract dict."""
    if not isinstance(claim, dict):
        raise ValueError("claim must be an object")
    kind = claim.get("kind")
    if kind not in CHECK_KINDS:
        raise ValueError(f"claim kind must be one of {CHECK_KINDS}")
    expected = _KIND_KEYS[kind]
    if set(claim) != expected:
        raise ValueError(f"claim keys for kind '{kind}' must be exactly "
                         f"{sorted(expected)}")
    claim_id = _validate_claim_id(claim["claim_id"])
    references = _validate_source_references(claim["source_references"])
    whitelist = validate_variables(
        DEFAULT_VARIABLES if variable_whitelist is None
        else variable_whitelist)
    result = {
        "schema": RESULT_SCHEMA,
        "claim_id": claim_id,
        "kind": kind,
        "method_id": kind,
        "method_description": "",
        "mathematical_input": {},
        "assumptions": [],
        "domain_notes": [],
        "outcome": OUTCOME_INCONCLUSIVE,
        "outcomes_vocabulary": list(OUTCOMES),
        "verification_kind": EXACT_KIND,
        "evidence": {},
        "explanation": "",
        "source_references": references,
        "review_required": True,
        "claim_scope_note": CLAIM_SCOPE_NOTE,
    }
    handlers = {
        KIND_ARITHMETIC: _verify_arithmetic,
        KIND_EQUIVALENCE: _verify_equivalence,
        KIND_EXPANSION: _verify_expansion,
        KIND_FACTORISATION: _verify_factorisation,
        KIND_FRACTIONS: _verify_fractions,
        KIND_SURDS: _verify_surds,
        KIND_EQUATION: _verify_equation_solutions,
        KIND_QUADRATIC: _verify_equation_solutions,
        KIND_SUBSTITUTION: _verify_substitution,
        KIND_ASSUMPTIONS: _verify_assumptions,
        KIND_APPROXIMATE: _verify_approximate,
    }
    try:
        outcome, evidence = handlers[kind](claim, whitelist, result)
    except ExpressionRefused as refusal:
        outcome, evidence = OUTCOME_UNSUPPORTED, {
            "refusal_reason": refusal.reason,
            "refusal_detail": refusal.detail}
    except _EngineUnsupported as guard:
        outcome, evidence = OUTCOME_UNSUPPORTED, {
            "unapplicable_reason": str(guard)}
    result["outcome"] = outcome
    result["evidence"] = evidence
    result["explanation"] = _explanation_for(outcome, result)
    result["review_required"] = bool(
        outcome != OUTCOME_VERIFIED
        or result["domain_notes"]
        or result["verification_kind"] == APPROXIMATE_KIND
        or kind == KIND_APPROXIMATE
        or (outcome == OUTCOME_VERIFIED and result["assumptions"]
            and result["evidence"].get("assumption_dependency")
            != "none_required")
        or not references)
    return result


def verify_claims(claims, variable_whitelist=None):
    """Verify an ordered list of claims into result dicts."""
    if not isinstance(claims, (list, tuple)):
        raise ValueError("claims must be a list")
    return [verify_claim(claim, variable_whitelist) for claim in claims]
