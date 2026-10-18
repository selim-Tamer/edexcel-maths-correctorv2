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
- Original-domain exclusions come from the parser (exact zero sets of
  every original denominator, recorded BEFORE any automatic
  cancellation). Symbolic cancellation is never trusted to widen the
  original domain: claims landing on an excluded value are refused,
  and identities whose domain analysis is incomplete stay
  ``inconclusive`` rather than ``verified``.
- Equation completeness: ``verified`` solution sets are only reported
  when the bounded solve provably enumerates the class (degree <= 2).
  Degree 3-4 radical solver output may be incomplete, so confirmed
  roots there stay ``inconclusive``; identities (infinitely many
  solutions) and root-free equations are explicitly distinguished.
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


def _excluded_pairs(record):
    """Variable-specific exclusion pairs from ORIGINAL-denominator provenance.

    Each pair is ``(variable_name, canonical_zero_text)`` and comes only
    from exactly resolved univariate original denominators. Values are
    never pooled across variables: a zero of ``x`` never excludes an
    assignment to ``y``.
    """
    pairs = set()
    for item in record["original_denominators"]:
        if not item["resolvable"]:
            continue
        for zero in item["zeros"]:
            pairs.add((item["variables"][0], zero))
    return pairs


def _domain_notes_for_pair(lhs_record, rhs_record):
    """Domain compatibility notes from the ORIGINAL parse-level provenance.

    The excluded pairs are variable-specific zero sets of every original
    denominator (recorded by the parser BEFORE any automatic
    cancellation), so symbolic cancellation can never erase them, and a
    zero of one variable is never compared against another variable's
    assignments.
    """
    lhs_pairs = _excluded_pairs(lhs_record)
    rhs_pairs = _excluded_pairs(rhs_record)
    lhs_excluded = set(lhs_record["excluded_values"])
    rhs_excluded = set(rhs_record["excluded_values"])
    combined_complete = lhs_record["domain_analysis_complete"] \
        and rhs_record["domain_analysis_complete"]
    notes = []
    if not combined_complete:
        notes.append(
            "the original-domain analysis is incomplete: at least one "
            "denominator's restriction could not be resolved exactly, so "
            "the identity cannot be certified as fully-domain-safe")
    extra_lhs = sorted(pair for pair in lhs_pairs if pair not in rhs_pairs)
    extra_rhs = sorted(pair for pair in rhs_pairs if pair not in lhs_pairs)
    if extra_lhs:
        notes.append(
            "the identity holds on the common domain only: the "
            "left-hand side is undefined at the excluded value(s) "
            + ", ".join(value + " (variable " + variable + ")"
                        for variable, value in extra_lhs))
    if extra_rhs:
        notes.append(
            "the identity holds on the common domain only: the "
            "right-hand side is undefined at the excluded value(s) "
            + ", ".join(value + " (variable " + variable + ")"
                        for variable, value in extra_rhs))
    return notes, extra_lhs, extra_rhs, combined_complete


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
    return lhs, rhs, lhs_record, rhs_record, has_floats


def _identity_outcome(lhs, rhs, lhs_record, rhs_record, result,
                      method_description):
    """Bounded canonical-identity decision shared by equivalence checks."""
    result["method_description"] = method_description
    diff_expression = lhs - rhs
    if not expand_is_bounded(diff_expression):
        raise _EngineUnsupported(
            "the expanded difference would exceed the bounded term "
            "budget; the claim is left inconclusive rather than risking "
            "unbounded symbolic work")
    from sympy import cancel, Poly, PolynomialError
    canonical = cancel(diff_expression)
    notes, extra_lhs, extra_rhs, domain_complete = \
        _domain_notes_for_pair(lhs_record, rhs_record)
    result["domain_notes"] = notes
    if canonical == 0:
        if not domain_complete:
            # Requirement: with original-domain uncertainty the engine
            # stays inconclusive rather than claiming a verified
            # identity on a domain it cannot resolve exactly.
            return OUTCOME_INCONCLUSIVE, {
                "canonical_difference": "0",
                "domain_analysis_incomplete": True,
                "extra_poles_lhs": extra_lhs,
                "extra_poles_rhs": extra_rhs}
        return OUTCOME_VERIFIED, {
            "canonical_difference": "0",
            "extra_poles_lhs": extra_lhs,
            "extra_poles_rhs": extra_rhs}
    variables = tuple(sorted(lhs.free_symbols | rhs.free_symbols, key=str))
    evidence_base = {
        "canonical_difference": canonical_text(canonical),
        "rendered_difference": str(canonical)}
    try:
        poly = Poly(canonical, *variables) if variables else None
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
    lhs, rhs, _, _, has_floats = _verify_pair(claim, whitelist, result)
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
    lhs, rhs, lhs_record, rhs_record, has_floats = _verify_pair(
        claim, whitelist, result)
    if has_floats:
        return _approximately_compare(lhs, rhs, claim.get("tolerance"),
                                      result)
    return _identity_outcome(lhs, rhs, lhs_record, rhs_record, result,
                             "exact_canonical_difference")


def _verify_expansion(claim, whitelist, result):
    lhs, rhs, lhs_record, rhs_record, has_floats = _verify_pair(
        claim, whitelist, result)
    if has_floats:
        return _approximately_compare(lhs, rhs, claim.get("tolerance"),
                                      result)
    return _identity_outcome(lhs, rhs, lhs_record, rhs_record, result,
                             "bounded_expand_and_canonical_difference")


def _verify_factorisation(claim, whitelist, result):
    lhs, rhs, lhs_record, rhs_record, has_floats = _verify_pair(
        claim, whitelist, result)
    if has_floats:
        return _approximately_compare(lhs, rhs, claim.get("tolerance"),
                                      result)
    return _identity_outcome(lhs, rhs, lhs_record, rhs_record, result,
                             "factorisation_verified_by_expansion")


def _verify_fractions(claim, whitelist, result):
    lhs, rhs, lhs_record, rhs_record, has_floats = _verify_pair(
        claim, whitelist, result)
    if has_floats:
        return _approximately_compare(lhs, rhs, claim.get("tolerance"),
                                      result)
    return _identity_outcome(lhs, rhs, lhs_record, rhs_record, result,
                             "bounded_rational_canonicalisation")


def _verify_surds(claim, whitelist, result):
    from sympy import radsimp, powsimp, cancel
    lhs, rhs, _, _, has_floats = _verify_pair(claim, whitelist, result)
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
    return (diff_expression, tuple(sorted(lhs.free_symbols | rhs.free_symbols,
                                          key=str)), equation_record)


def _classify_equation_diff(canonical, variable):
    """Bounded classification of a cancelled equation difference.

    Returns ``(poly_or_None, status)`` where status is one of
    ``non_polynomial``, ``exceeds_bound``, ``trusted`` (degree <= 2;
    the complete solution set is provable) or ``solver_partial``
    (degree 3-4: the radical solver may return incomplete sets).
    """
    from sympy import Poly, PolynomialError
    try:
        poly = Poly(canonical, variable)
    except (PolynomialError, ValueError, TypeError):
        return None, "non_polynomial"
    degree = poly.total_degree()
    if degree > MAX_DEGREE:
        return poly, "exceeds_bound"
    if degree <= 2:
        return poly, "trusted"
    return poly, "solver_partial"       # degree 3..4: incompleteness risk


def zero_of_any_denominator(excluded_pairs, candidate_root):
    """True when the claimed value zeroes SOME resolvable denominator.

    Used only where the owning variable is structurally ambiguous
    (cancelled-away multi-variable equations); the single-variable
    paths match the pair exactly.
    """
    text = canonical_text(candidate_root)
    return any(value == text for _variable, value in excluded_pairs)


def _verify_equation_solutions(claim, whitelist, result):
    from sympy import cancel, roots as sympy_roots
    is_quadratic = claim["kind"] == KIND_QUADRATIC
    diff_expression, variable_and_others, equation_record = _equation_diff(
        claim, whitelist, result,
        "quadratic_solution_verification" if is_quadratic
        else "bounded_polynomial_solution_check")
    claimed_list = _parse_claimed_solutions(claim["solutions"], whitelist)
    referenced = set(equation_record["referenced_variables"])
    excluded_pairs = _excluded_pairs(equation_record)
    excluded = sorted({zero for _variable, zero in excluded_pairs})
    domain_complete = equation_record["domain_analysis_complete"]
    if len(variable_and_others) != 1:
        if variable_and_others:
            raise _EngineUnsupported(
                "solution verification is supported for single-variable "
                "polynomial equations only")
        # No free symbols survive: either the equation is genuinely
        # constant, or original variables were cancelled away (x/x = 1).
        # Both are handled from the ORIGINAL-domain record, never from
        # the cancelled shape alone.
        value = cancel(diff_expression)
        checked = []
        for candidate_root in claimed_list:
            if candidate_root.free_symbols:
                raise _EngineUnsupported(
                    "claimed solutions must be exact constants in "
                    "this stage")
            if candidate_root.is_Float:
                raise _EngineUnsupported(
                    "claimed solutions must be exact; approximate "
                    "floats are not accepted as solutions in this "
                    "stage")
            if zero_of_any_denominator(excluded_pairs, candidate_root):
                # The original equation is undefined at this value: the
                # claimed solution zeroes some original denominator.
                checked.append({
                    "solution": canonical_text(candidate_root),
                    "status": "solution_excluded_by_original_domain",
                    "residual": None})
                continue
            if value == 0:
                checked.append({
                    "solution": canonical_text(candidate_root),
                    "status": "identity_is_not_a_finite_set",
                    "residual": "0"})
            else:
                checked.append({
                    "solution": canonical_text(candidate_root),
                    "status": "root_rejected",
                    "residual": str(value)})
        evidence = {"claimed_solutions_checked": checked,
                    "original_domain_excluded_values": excluded}
        if not domain_complete:
            evidence["domain_analysis_incomplete"] = True
        evidence["rejected_solutions"] = [
            entry for entry in checked
            if entry["status"] in ("root_rejected",
                                   "solution_excluded_by_original_domain")]
        if value == 0:
            evidence["completeness"] = (
                "the equation reduces to an identity on the common "
                "domain: infinitely many solutions"
                + (", except the excluded value(s) "
                   + ", ".join(sorted(excluded)) if excluded else "")
                + "; a finite claimed solution set cannot be complete")
        elif referenced:
            evidence["completeness"] = (
                "the equation has NO solutions: its exact difference is "
                "a nonzero constant, so no value can satisfy it")
        else:
            evidence["completeness"] = (
                "the equation has NO solutions: both sides are "
                "constants that differ, so no value can satisfy it")
        return OUTCOME_CONTRADICTED, evidence
    variable = variable_and_others[0]
    root_reports = []
    all_claimed_roots_valid = True
    for candidate_root in claimed_list:
        if candidate_root.free_symbols:
            raise _EngineUnsupported(
                "claimed solutions must be exact constants in this stage")
        if candidate_root.is_Float:
            raise _EngineUnsupported(
                "claimed solutions must be exact; approximate floats are "
                "not accepted as solutions in this stage")
        if (variable.name, canonical_text(candidate_root)) in excluded_pairs:
            # The ORIGINAL equation is undefined where an original
            # denominator in this variable is zero; such solutions are
            # refused even if cancellation erased the denominator.
            all_claimed_roots_valid = False
            root_reports.append({
                "solution": canonical_text(candidate_root),
                "status": "solution_excluded_by_original_domain",
                "residual": None})
            continue
        value = diff_expression.subs(variable, candidate_root)
        if value == 0:
            root_reports.append({"solution": canonical_text(candidate_root),
                                 "status": "root_confirmed",
                                 "residual": "0"})
        elif value.is_number:
            all_claimed_roots_valid = False
            root_reports.append({"solution": canonical_text(candidate_root),
                                 "status": "root_rejected",
                                 "residual": str(value)})
        else:
            all_claimed_roots_valid = False
            root_reports.append({"solution": canonical_text(candidate_root),
                                 "status": "root_unresolved",
                                 "residual": canonical_text(value)})
    evidence = {
        "claimed_solutions_checked": root_reports,
        "original_domain_excluded_values": excluded,
    }
    if not domain_complete:
        evidence["domain_analysis_incomplete"] = True
    # Canonical classification AFTER the original-domain checks; `cancel`
    # may simplify the difference, but it is never trusted to widen the
    # original domain (excluded values above were recorded before it).
    canonical = cancel(diff_expression)
    poly, status = _classify_equation_diff(canonical, variable)
    claimed_texts = sorted(canonical_text(item) for item in claimed_list)

    if not all_claimed_roots_valid:
        evidence["rejected_solutions"] = [
            entry for entry in root_reports
            if entry["status"] != "root_confirmed"]
        if status == "trusted" and domain_complete:
            _attach_computed_set(poly, evidence, excluded_pairs, variable)
            if evidence.get("solution_set_on_common_domain") == []:
                evidence["completeness"] = (
                    "the equation has NO solutions in the supported "
                    "class: the bounded solve found no valid roots"
                    + ("; the only computed root(s) were excluded by "
                       "the original domain"
                       if evidence.get("excluded_computed_roots") else ""))
            elif evidence.get("excluded_computed_roots"):
                evidence["completeness"] = (
                    "the claimed solutions do not all satisfy the "
                    "equation; the complete solution set on the common "
                    "domain is stated in the evidence")
        return OUTCOME_CONTRADICTED, evidence

    if not domain_complete:
        evidence["completeness"] = (
            "completeness was not established: the original-domain "
            "analysis of the equation is incomplete, so a verified "
            "solution set could hide a denominator-zero solution")
        return OUTCOME_INCONCLUSIVE, evidence
    if status == "non_polynomial":
        evidence["completeness"] = (
            "completeness was not established: the equation is outside "
            "the engine's bounded polynomial class (degree cap "
            f"{MAX_DEGREE}); claimed solutions are valid roots")
        return OUTCOME_INCONCLUSIVE, evidence
    if status == "exceeds_bound":
        evidence["completeness"] = (
            "completeness was not established: the equation exceeds the "
            f"engine's degree cap {MAX_DEGREE}; claimed solutions are "
            "valid roots")
        return OUTCOME_INCONCLUSIVE, evidence
    if is_quadratic and poly.total_degree() not in (1, 2):
        raise _EngineUnsupported(
            "the quadratic check requires an equation of degree 1 or 2")
    if poly.is_zero:
        # Identically true equation: infinitely many solutions on the
        # common domain (every value except the excluded ones), so a
        # finite claimed solution set can never be the complete set.
        # Checked BEFORE the solver: asking ``roots`` for the zero
        # polynomial would return an empty set that misreads as
        # "no solutions".
        evidence["completeness"] = (
            "the equation reduces to an identity on the common domain: "
            "infinitely many solutions"
            + (", except the excluded value(s) "
               + ", ".join(sorted(excluded)) if excluded else "")
            + "; a finite claimed solution set cannot be complete")
        return OUTCOME_CONTRADICTED, evidence
    computed = sorted({canonical_text(root) for root, multiplicity
                       in sympy_roots(poly).items()})
    excluded_computed = [text for text in computed
                         if (variable.name, text) in excluded_pairs]
    effective = [text for text in computed
                 if (variable.name, text) not in excluded_pairs]
    evidence["computed_solution_set"] = computed
    evidence["solution_set_on_common_domain"] = effective
    evidence["excluded_computed_roots"] = excluded_computed
    if not effective:
        evidence["completeness"] = (
            "the equation has NO solutions in the supported class: the "
            "bounded solve found no valid roots"
            + ("; the only computed root(s) were excluded by the "
               "original domain" if excluded_computed else ""))
        return OUTCOME_CONTRADICTED, evidence
    if status == "trusted":
        if effective == claimed_texts:
            evidence["completeness"] = (
                "solution set verified: the bounded polynomial solve "
                "found exactly the claimed solution set"
                + (" after the original-domain exclusions"
                   if excluded_computed else ""))
            return OUTCOME_VERIFIED, evidence
        evidence["claimed_solution_set"] = claimed_texts
        evidence["completeness"] = (
            "the bounded polynomial solve did not agree with the "
            "claimed solution set")
        return OUTCOME_CONTRADICTED, evidence
    # degree 3-4: radical solvers may return incomplete sets, so
    # completeness is never asserted for this class.
    evidence["completeness"] = (
        "claimed solutions are valid roots, but completeness was not "
        "established: the degree-3/4 radical solver can return "
        "incomplete sets, so the claim is left inconclusive")
    return OUTCOME_INCONCLUSIVE, evidence


def _attach_computed_set(poly, evidence, excluded_pairs, variable):
    """Best-effort diagnostic solution set for contradicted claims.

    ``variable`` is the solving symbol; computed roots are filtered
    through the variable-specific original denominators (provenance
    pairs keyed by symbol NAME), never through a pooled set.
    """
    from sympy import roots as sympy_roots
    try:
        computed = sorted({canonical_text(root) for root, _multiplicity
                           in sympy_roots(poly).items()})
    except Exception:
        return
    evidence["computed_solution_set"] = computed
    evidence["excluded_computed_roots"] = [
        text for text in computed if (variable.name, text) in excluded_pairs]
    evidence["solution_set_on_common_domain"] = [
        text for text in computed
        if (variable.name, text) not in excluded_pairs]


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
    excluded_pairs = _excluded_pairs(expression_record)
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
    excluded_assignments = []
    for variable_name, value_text in assignments.items():
        if not isinstance(variable_name, str) \
                or variable_name not in whitelist:
            raise ValueError("assignments use whitelisted variable names")
        value = _parse_side(value_text, whitelist)
        if value.free_symbols:
            raise _EngineUnsupported(
                "assignment values must be constants in this stage")
        if (variable_name, canonical_text(value)) in excluded_pairs:
            # The ORIGINAL expression is undefined at this assignment:
            # a resolvable original denominator in this very variable
            # evaluates to zero there. Values are never pooled across
            # variables; an excluded value of x never rejects y.
            excluded_assignments.append(
                {"variable": variable_name,
                 "value": canonical_text(value)})
            continue
        substituted = substituted.subs(variable_name, value)
        assignment_records.append({"variable": variable_name,
                                   "value": canonical_text(value)})
    result["mathematical_input"] = _input_record(
        expression, claimed_value, False,
        {"expression_text": claim["expression"],
         "claimed_value_text": claim["claimed_value"],
         "assignments": assignment_records})
    if excluded_assignments:
        result["domain_notes"] = [
            "the original expression is undefined at the substituted "
            "value(s): " + ", ".join(
                entry["variable"] + "=" + entry["value"]
                for entry in excluded_assignments)]
        return OUTCOME_CONTRADICTED, {
            "substituted_value": None,
            "claimed_value": str(claimed_value),
            "undefined_assignments": excluded_assignments,
            "rejection": ("the original expression is undefined at this "
                          "assignment, so it has no value to compare")}
    if not expression_record["domain_analysis_complete"]:
        # Original-domain analysis is incomplete (e.g. radical or
        # over-cap denominators): the engine must not certify a value
        # it cannot place on a fully resolved domain.
        result["domain_notes"] = [
            "the original-domain analysis of the expression is "
            "incomplete, so the substituted value cannot be certified "
            "as domain-safe"]
        return OUTCOME_INCONCLUSIVE, {
            "substituted_value": str(substituted),
            "claimed_value": str(claimed_value),
            "domain_analysis_incomplete": True}
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
    lhs, rhs, _, _, has_floats = _verify_pair(claim, whitelist, result)
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
    lhs, rhs, _, _, _ = _verify_pair(claim, whitelist, result)
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
