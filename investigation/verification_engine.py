"""Deterministic, offline mark-scheme verification (Phase C Stage 2).

Investigation-only: never imported by the production app; imports nothing from
it; no network, no AI API calls, no eval/exec, no student documents. The only
input is a structured scheme payload (see investigation/scheme_model.py); the
only output is a list of structured findings.

EVERY finding carries ``review_required: true``: no verification output may
ever change marking criteria, maximum marks or awarded grades on its own, and
no automated transition to an approved state exists (teacher sign-off is a
Phase C Stage 6 concern). No AI confidence score exists anywhere in this
module; "verified" means deterministic proof from the suppliant record's own
explicit declarations - never an official statement about Pearson schemes.

Finding states (``finding["state"]``):
- verified_structural_inconsistency: provable from explicit declarations.
- suspected_issue: plausible inconsistency that requires interpretation.
- evidence_insufficient: a check could not run honestly; never fabricates.
- human_review_required: explicitly declared to be beyond deterministic scope.

Deterministic checks (rule ids are stable identifiers used by tests):
- SCHEME-DUP-QUESTIONS   duplicate question identifiers
- SCHEME-DUP-CRITERIA    duplicate criterion identifiers (document-wide)
- SCHEME-CODE-UNSUPPORTED mark code outside the M/A/B/dM(+level)(+ft) grammar
- SCHEME-WEIGHT-NONPOSITIVE zero or negative criterion weight
- SCHEME-FIELD-MISSING   missing required criterion fields (code/weight)
- SCHEME-TOTAL-MISMATCH  criterion-weight sum != explicit question total
- SCHEME-TOTAL-ABSENT    question has criteria but no explicit total
  (evidence_insufficient: reconciliation skipped, never guessed)
- SCHEME-TOTAL-UNRECONCILABLE total present but some weight missing/invalid
  (evidence_insufficient)
- SCHEME-DEP-UNDEFINED   dependency references a nonexistent criterion id
- SCHEME-DEP-SELF        criterion depends on itself
- SCHEME-DEP-DUPLICATED  the same dependency declared twice (contradictory
  explicit declarations)
- SCHEME-DEP-CYCLE       dependency cycle (iterative Tarjan SCC, bounded by
  the documented size caps; unbounded traversal is impossible)
- SCHEME-FT-CONTRADICTION follow-through flag contradicts an explicit "ft"
  code suffix (e.g. code "A1ft" with follow_through false)
- SCHEME-FT-MISMATCH     follow-through flag true but code lacks the "ft"
  suffix (suspected_issue: not provably wrong)
- SCHEME-ALT-GROUP-INVALID alternative group with fewer than two members
- SCHEME-DIAGRAM-REVIEW  explicitly diagram-dependent question
  (human_review_required)
- SCHEME-MATH-UNSUPPORTED a claimed solution string is present: Stage C2 has
  NO algebra/SymPy verification, so this is an evidence gap, never a verdict
- SCHEME-PROVENANCE-ABSENT document without provenance (evidence_insufficient)

Unsupported-by-design in this stage (explicitly NOT attempted): algebraic
equivalence checks, solution verification, OCR quality judgments, question
paper/mark scheme document matching, exam-board policy interpretation.
Arithmetic is deliberately not evaluated here even though production uses a
small safe evaluator, because re-implementing ad-hoc verification invites
false positives; SymPy integration is a later stage needing approval.
"""
from collections import Counter

from investigation.scheme_model import (
    SchemeModelError,
    code_has_follow_through_suffix,
    code_is_supported,
    parse_scheme,
)

VERIFIED = "verified_structural_inconsistency"
SUSPECTED = "suspected_issue"
INSUFFICIENT = "evidence_insufficient"
HUMAN_REVIEW = "human_review_required"

REVIEW_STATES = (VERIFIED, SUSPECTED, INSUFFICIENT, HUMAN_REVIEW)


def _finding(rule_id, category, state, severity, question_id, criterion_ids,
             evidence, explanation):
    """Build one finding dict with a deterministic, collision-checked id."""
    ordered_criteria = sorted(criterion_ids)
    parts = [rule_id, question_id or "-", ",".join(ordered_criteria)]
    finding_id = "::".join(parts)
    return {
        "finding_id": finding_id,
        "rule_id": rule_id,
        "issue_category": category,
        "state": state,
        "severity": severity,
        "question_id": question_id,
        "criterion_ids": ordered_criteria,
        "evidence": evidence,
        "explanation": explanation,
        "review_required": True,
    }


def _criterion_evidence(criterion):
    """Verbatim structural evidence; prose is quoted, never interpreted."""
    return {
        "code": criterion.code,
        "weight": criterion.weight,
        "dependencies": list(criterion.dependencies),
        "follow_through": criterion.follow_through,
        "alternative_group": criterion.alternative_group,
    }


def check_duplicate_question_ids(document):
    findings = []
    counts = Counter(question.id for question in document.questions)
    for question_id, count in counts.items():
        if count > 1:
            findings.append(_finding(
                "SCHEME-DUP-QUESTIONS", "duplicate_question_id", VERIFIED,
                "high", question_id, [],
                {"occurrences": count},
                f"Question identifier '{question_id}' appears {count} times; "
                "awards cannot be attributed unambiguously to one question."))
    return findings


def check_duplicate_criterion_ids(document):
    findings = []
    counts = Counter(criterion.id for _, criterion in document.criteria())
    for criterion_id, count in counts.items():
        if count > 1:
            findings.append(_finding(
                "SCHEME-DUP-CRITERIA", "duplicate_criterion_id", VERIFIED,
                "high", None, [criterion_id],
                {"occurrences": count},
                f"Criterion identifier '{criterion_id}' appears {count} times "
                "document-wide; per-criterion alignment is ambiguous."))
    return findings


def check_mark_codes(document):
    findings = []
    for question, criterion in document.criteria():
        if criterion.code is None:
            continue               # missing code is a separate required-field rule
        if not code_is_supported(criterion.code):
            findings.append(_finding(
                "SCHEME-CODE-UNSUPPORTED", "unsupported_mark_code", VERIFIED,
                "medium", question.id, [criterion.id],
                {"code": criterion.code,
                 "supported_bases": ["M", "A", "B", "dM"],
                 "grammar": "^(dM|M|A|B)[1-9][0-9]*ft?$"},
                f"Mark code '{criterion.code}' is outside the supported "
                "M/A/B/dM notation, so its meaning cannot be reconciled; "
                "unknown codes are never assumed valid."))
    return findings


def check_weights(document):
    findings = []
    for question, criterion in document.criteria():
        if criterion.weight is None:
            continue               # missing weight is a required-field finding
        if criterion.weight <= 0:
            findings.append(_finding(
                "SCHEME-WEIGHT-NONPOSITIVE", "invalid_criterion_weight",
                VERIFIED, "medium", question.id, [criterion.id],
                {"weight": criterion.weight},
                f"Criterion '{criterion.id}' declares weight "
                f"{criterion.weight}; weights must be positive integers."))
    return findings


def check_required_fields(document):
    findings = []
    for question, criterion in document.criteria():
        missing = []
        if criterion.code is None:
            missing.append("code")
        if criterion.weight is None:
            missing.append("weight")
        if not missing:
            continue
        findings.append(_finding(
            "SCHEME-FIELD-MISSING", "missing_required_criterion_field", VERIFIED,
            "high" if "weight" in missing else "medium",
            question.id, [criterion.id],
            {"missing_fields": missing, "evidence": _criterion_evidence(criterion)},
            f"Criterion '{criterion.id}' is missing required structural "
            f"field(s): {', '.join(missing)}. The record cannot be scored or "
            "reconciled; absence is reported, never filled by inference."))
    return findings


def _question_weight_inventory(question):
    """(all_valid, total_sum, unscorable_ids) for the reconciliation check."""
    unscorable = []
    weights_sum = 0
    for criterion in question.criteria:
        if criterion.weight is None or criterion.weight <= 0:
            unscorable.append(criterion.id)
        else:
            weights_sum += criterion.weight
    return (not unscorable, weights_sum, unscorable)


def check_question_totals(document):
    """Weight-sum reconciliation ONLY against an explicitly supplied total."""
    findings = []
    for question in document.questions:
        if not question.criteria:
            continue
        all_valid, weights_sum, unscorable = _question_weight_inventory(question)
        if question.total is None:
            findings.append(_finding(
                "SCHEME-TOTAL-ABSENT", "question_total_missing", INSUFFICIENT,
                "low", question.id, [],
                {"criteria_count": len(question.criteria)},
                f"Question '{question.id}' has no explicit total, so the "
                "criterion-weight sum was not reconciled. No total is inferred."))
        elif not all_valid:
            findings.append(_finding(
                "SCHEME-TOTAL-UNRECONCILABLE", "question_total_unreconcilable",
                INSUFFICIENT, "medium", question.id, unscorable,
                {"total": question.total, "unscorable_criterion_ids": unscorable},
                f"Question '{question.id}' declares total {question.total}, "
                "but some criterion weights are missing or non-positive, so "
                "the sum cannot be honestly reconciled."))
        elif weights_sum != question.total:
            findings.append(_finding(
                "SCHEME-TOTAL-MISMATCH", "question_total_mismatch", VERIFIED,
                "high", question.id, [],
                {"explicit_total": question.total, "criterion_weight_sum":
                 weights_sum,
                 "criterion_ids": [c.id for c in question.criteria]},
                f"Question '{question.id}' states total {question.total} but "
                f"its criterion weights sum to {weights_sum}."))
    return findings


def check_dependencies(document):
    id_set = set()
    for _, criterion in document.criteria():
        id_set.add(criterion.id)

    undefined = []
    self_dependent = []
    duplicated = []
    for question, criterion in document.criteria():
        seen = set()
        for dependency in criterion.dependencies:
            if dependency == criterion.id:
                self_dependent.append((question.id, criterion.id))
                continue    # self-reference is undefined-reference noise, too
            if dependency not in id_set and dependency not in seen:
                undefined.append((question.id, criterion.id, dependency))
            if dependency in seen:
                duplicated.append((question.id, criterion.id, dependency))
            seen.add(dependency)

    findings = []
    for question_id, criterion_id, dependency in undefined:
        findings.append(_finding(
            "SCHEME-DEP-UNDEFINED", "undefined_dependency", VERIFIED,
            "high", question_id, [criterion_id],
            {"dependency_id": dependency},
            f"Criterion '{criterion_id}' depends on '{dependency}', which is "
            "not an identifier of any criterion in the document."))
    for question_id, criterion_id in self_dependent:
        findings.append(_finding(
            "SCHEME-DEP-SELF", "self_dependency", VERIFIED,
            "high", question_id, [criterion_id],
            {"dependency_id": criterion_id},
            f"Criterion '{criterion_id}' declares itself as a dependency; "
            "qualifications cannot resolve."))
    for question_id, criterion_id, dependency in duplicated:
        findings.append(_finding(
            "SCHEME-DEP-DUPLICATED", "contradictory_dependency_declaration",
            VERIFIED, "medium", question_id, [criterion_id],
            {"dependency_id": dependency},
            f"Criterion '{criterion_id}' declares dependency '{dependency}' "
            "twice; duplicate declarations are contradictory bookkeeping."))
    return findings


def _dependents(document, defined_ids):
    adjacency = {}
    for _, criterion in document.criteria():
        adjacency[criterion.id] = sorted(
            dependency for dependency in criterion.dependencies
            if dependency in defined_ids and dependency != criterion.id)
    return adjacency


def _strongly_connected(adjacency, nodes):
    """Iterative Tarjan SCC; bounded by the scheme-model size caps."""
    index = {}
    lowlink = {}
    on_stack = set()
    stack = []
    components = []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        index[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(adjacency[root]))]
        while work:
            node, successors = work[-1]
            advanced = False
            for successor in successors:
                if successor not in index:
                    index[successor] = lowlink[successor] = counter
                    counter += 1
                    stack.append(successor)
                    on_stack.add(successor)
                    work.append((successor, iter(adjacency[successor])))
                    advanced = True
                    break
                if successor in on_stack:
                    lowlink[node] = min(lowlink[node], index[successor])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    components.append(sorted(component))
    return components


def check_dependency_cycles(document):
    defined_ids = {criterion.id for _, criterion in document.criteria()}
    adjacency = _dependents(document, defined_ids)
    components = _strongly_connected(adjacency, sorted(adjacency))
    findings = []
    for component in components:
        findings.append(_finding(
            "SCHEME-DEP-CYCLE", "dependency_cycle", VERIFIED, "high",
            None, component,
            {"cycle_members": component},
            "Criteria " + ", ".join(component)
            + " form a dependency cycle; no criterion in the cycle can be "
            "qualified first."))
    return findings


def check_follow_through(document):
    findings = []
    for question, criterion in document.criteria():
        if criterion.code is None or not code_is_supported(criterion.code):
            continue
        code_ft = code_has_follow_through_suffix(criterion.code)
        flag = criterion.follow_through
        if code_ft and flag is False:
            findings.append(_finding(
                "SCHEME-FT-CONTRADICTION", "contradictory_follow_through",
                VERIFIED, "medium", question.id, [criterion.id],
                {"code": criterion.code, "follow_through": flag},
                f"Criterion '{criterion.id}' uses follow-through code suffix "
                f"'ft' ({criterion.code}) but explicitly sets "
                "follow_through=false; the two explicit declarations "
                "contradict each other."))
        elif flag is True and not code_ft:
            findings.append(_finding(
                "SCHEME-FT-MISMATCH", "follow_through_code_mismatch",
                SUSPECTED, "low", question.id, [criterion.id],
                {"code": criterion.code, "follow_through": flag},
                f"Criterion '{criterion.id}' is flagged follow-through but "
                f"code {criterion.code} lacks the 'ft' suffix; this is a "
                "plausible incongruity requiring interpretation."))
    return findings


def check_alternative_groups(document):
    findings = []
    for question in document.questions:
        groups = {}
        for criterion in question.criteria:
            if criterion.alternative_group is not None:
                groups.setdefault(criterion.alternative_group, []).append(criterion.id)
        for group_name, members in groups.items():
            if len(members) < 2:
                findings.append(_finding(
                    "SCHEME-ALT-GROUP-INVALID", "invalid_alternative_group",
                    VERIFIED, "medium", question.id, members,
                    {"alternative_group": group_name, "members": members},
                    f"Alternative group '{group_name}' in question "
                    f"'{question.id}' has {len(members)} member(s); an "
                    "alternatives declaration requires at least two."))
    return findings


def check_diagram_dependence(document):
    findings = []
    for question in document.questions:
        if question.diagram_dependent is True:
            findings.append(_finding(
                "SCHEME-DIAGRAM-REVIEW", "diagram_dependent_question",
                HUMAN_REVIEW, "high", question.id,
                [c.id for c in question.criteria],
                {"diagram_dependent": True},
                f"Question '{question.id}' is explicitly declared "
                "diagram-dependent; correctness cannot be judged from "
                "transcription and requires the original document."))
    return findings


def check_claimed_solutions(document):
    findings = []
    for question in document.questions:
        if question.claimed_solution is None:
            continue
        findings.append(_finding(
            "SCHEME-MATH-UNSUPPORTED", "unsupported_mathematical_claim",
            INSUFFICIENT, "high", question.id,
            [c.id for c in question.criteria],
            {"claimed_solution_present": True},
            f"Question '{question.id}' carries a claimed solution string; "
            "this engine performs no arithmetic or algebraic verification in "
            "Stage C2, so the claim is neither validated nor refuted here "
            "and requires human review."))
    return findings


def check_provenance(document):
    if document.provenance:
        return []
    return [_finding(
        "SCHEME-PROVENANCE-ABSENT", "provenance_absent", INSUFFICIENT,
        "low", None, [],
        {},
        "The scheme record carries no provenance information, so its origin "
        "and verification status cannot be attributed. Provenance is "
        "optional data, never filled in by inference.")]


_CHECKS = (
    check_duplicate_question_ids,
    check_duplicate_criterion_ids,
    check_mark_codes,
    check_weights,
    check_required_fields,
    check_question_totals,
    check_dependencies,
    check_dependency_cycles,
    check_follow_through,
    check_alternative_groups,
    check_diagram_dependence,
    check_claimed_solutions,
    check_provenance,
)

SUPPORTED_RULES = (
    "SCHEME-DUP-QUESTIONS", "SCHEME-DUP-CRITERIA", "SCHEME-CODE-UNSUPPORTED",
    "SCHEME-WEIGHT-NONPOSITIVE", "SCHEME-FIELD-MISSING",
    "SCHEME-TOTAL-MISMATCH", "SCHEME-TOTAL-ABSENT",
    "SCHEME-TOTAL-UNRECONCILABLE", "SCHEME-DEP-UNDEFINED", "SCHEME-DEP-SELF",
    "SCHEME-DEP-DUPLICATED", "SCHEME-DEP-CYCLE", "SCHEME-FT-CONTRADICTION",
    "SCHEME-FT-MISMATCH", "SCHEME-ALT-GROUP-INVALID", "SCHEME-DIAGRAM-REVIEW",
    "SCHEME-MATH-UNSUPPORTED", "SCHEME-PROVENANCE-ABSENT",
)


def verify_scheme_document(document):
    """Run all deterministic checks over a parsed SchemeDocument.

    Returns findings sorted by finding_id (deterministic, exactly assertable).
    Findings with identical ids (possible when question identifiers themselves
    duplicate) are merged: the first occurrence is kept and its evidence
    records ``colliding_occurrences``, so no report is duplicated or lost.
    """
    findings = []
    for check in _CHECKS:
        findings.extend(check(document))
    merged = {}
    for finding in findings:
        key = finding["finding_id"]
        if key in merged:
            evidence = merged[key]["evidence"]
            evidence["colliding_occurrences"] = \
                evidence.get("colliding_occurrences", 1) + 1
        else:
            merged[key] = finding
    return sorted(merged.values(), key=lambda finding: finding["finding_id"])


def verify_scheme(payload):
    """Parse (raising SchemeModelError on malformed input) then verify."""
    return verify_scheme_document(parse_scheme(payload))


def finding_key(finding):
    """Compact comparable descriptor: (rule_id, question_id, criterion tuple)."""
    return (finding["rule_id"], finding["question_id"],
            tuple(finding["criterion_ids"]))


# ---------------------------------------------------------------------------
# Offline synthetic benchmark (Phase C Stage 2 harness).

BENCHMARK_FILENAME = "verification_cases.json"

BENCHMARK_PROVENANCE = (
    "Synthetic verification benchmark authored for engineering tests, NOT "
    "official Pearson 4MA1 material. Cases exercise the deterministic "
    "structural rules above; none of the expected findings constitutes a "
    "claim about a real Pearson mark scheme. Deliberately valid schemes and "
    "negative controls are included; cases beyond Stage C2 scope expect "
    "evidence gaps or human-review findings instead of fabricated verdicts.")


def load_benchmark(path=None):
    """Strictly validate the benchmark file structure and return its cases.

    Raises ValueError (content-free, position-only messages) on structural
    malformation of the benchmark itself.
    """
    import json
    from pathlib import Path

    if path is None:
        path = Path(__file__).with_name(BENCHMARK_FILENAME)
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Benchmark payload must be an object")
    if not isinstance(data.get("cases"), list) or not data["cases"]:
        raise ValueError("Benchmark must contain a non-empty 'cases' list")
    seen = set()
    for case in data["cases"]:
        if not isinstance(case, dict):
            raise ValueError("Benchmark case must be an object")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip() or case_id in seen:
            raise ValueError(f"Invalid or duplicate benchmark case id: {case_id}")
        seen.add(case_id)
        if not isinstance(case.get("scheme"), dict):
            raise ValueError(f"Benchmark case {case_id} needs a scheme object")
        expected = case.get("expected_findings")
        if not isinstance(expected, list):
            raise ValueError(f"Benchmark case {case_id} needs expected_findings")
        for entry in expected:
            if (not isinstance(entry, dict)
                    or not isinstance(entry.get("rule_id"), str)
                    or not isinstance(entry.get("criterion_ids"), list)
                    or set(entry) - {"rule_id", "question_id", "criterion_ids"}):
                raise ValueError(
                    f"Benchmark case {case_id} has a malformed expected finding")
        # every case must parse with the same strict model as production input
        parse_scheme(case["scheme"])
    return data


def run_benchmark(path=None):
    """Run every benchmark case and compare against expected findings.

    Returns {"benchmark_version", "provenance", "results", "passed"} where
    each result carries the exact actual/expected finding keys.
    """
    data = load_benchmark(path)
    results = []
    for case in data["cases"]:
        actual = verify_scheme(case["scheme"])
        expected_keys = sorted([
            (entry["rule_id"], entry.get("question_id"),
             tuple(entry["criterion_ids"]))
            for entry in case["expected_findings"]])
        actual_keys = sorted(finding_key(finding) for finding in actual)
        results.append({
            "id": case["id"],
            "status": "pass" if actual_keys == expected_keys else "fail",
            "expected": [list(key) for key in expected_keys],
            "actual": [list(key) for key in actual_keys],
            "unsupported_in_stage": case.get("unsupported_in_stage", []),
        })
    return {
        "benchmark_version": data.get("benchmark_version"),
        "provenance": data.get("provenance"),
        "results": results,
        "passed": all(result["status"] == "pass" for result in results),
    }
