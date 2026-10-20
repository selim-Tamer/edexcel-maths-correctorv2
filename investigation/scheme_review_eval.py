"""Phase C5 evaluation harness: does the deterministic scheme reviewer catch
what it is supposed to catch on synthetic inputs, without firing on clean ones?

The review engine itself is the repo-root ``scheme_review`` module (the app
integration target). The pre-C5-style baseline in here is an honest, minimal
validator: it only checks that each question's stated maximum is a positive
integer and, when criterion marks are integers themselves, that they sum to
that maximum. Anything else (dependencies, alternative groups, evidence
binding, question mapping, unsupported codes, duplicates) pre-dated C5 with no
deterministic check in the app, so the comparison shows what C5 adds.

Everything is synthetic and offline: no real mark schemes, no Pearson
material, no network calls, no Gemini calls, no student data. Case-level
expectations on synthetic inputs are NOT a claim of real-world accuracy or of
improved marking accuracy.

Run directly (``python -m investigation.scheme_review_eval``) to print a
deterministic JSON summary; exit code 1 signals failed case expectations.
"""

import argparse
import json
import os

EVAL_SCHEMA = "investigation.scheme_review_eval/1"
CASES_SCHEMA = "investigation.scheme_review_eval_cases/1"
CASES_PATH = os.path.join(os.path.dirname(__file__), "scheme_review_eval_cases.json")

SCHEMA = EVAL_SCHEMA
BASELINE_DESCRIPTION = (
    "pre-C5-style validator: positive integer max_marks; integer criterion "
    "marks, if any, summing to max_marks. Nothing more.")

DEFAULT_PAGE_TEXT = (
    "M1 valid method\nA1 correct answer\nB1 independent mark\n"
    "M1 alternative method\nA1 alternate answer\nB1 suffix point")
EVIDENCE_PAGE_COUNT = 5
EVIDENCE_PAGE = 2

ALT_GROUP_REVIEW_RULE = "C5-ALT-SUM-REVIEW"
STATUS_CLEAN_EXPECTED = "CHECKED"
STATUS_DEFECT_EXPECTED = "NEEDS_TEACHER_REVIEW"


def load_cases(path=None):
    """Load the synthetic case set and validate its envelope."""
    with open(path or CASES_PATH, encoding="utf-8") as handle:
        data = json.load(handle)
    schema = data.get("schema")
    if schema != CASES_SCHEMA:
        raise ValueError(f"unexpected cases schema: {schema!r}")
    return data["cases"]


def evidence_document(page_text=DEFAULT_PAGE_TEXT,
                      page=EVIDENCE_PAGE, page_count=EVIDENCE_PAGE_COUNT):
    return {"page_count": page_count, "pages": [
        {"page": page, "text": page_text, "status": "extracted",
         "warnings": [], "objects": []}]}


def question_mapping(matched):
    """Build a C3-shaped mapping record for the listed question ids."""
    return {"matches": [{"question_id": str(q), "certainty": "explicit",
                         "references": [], "pages": [EVIDENCE_PAGE]}
                        for q in matched],
            "unresolved": []}


def _case_kwargs(case):
    kwargs = {}
    if case.get("evidence"):
        kwargs["evidence_document"] = evidence_document()
    if "matched_questions" in case:
        kwargs["question_mapping"] = question_mapping(
            case["matched_questions"])
    return kwargs


def baseline_findings(payload):
    """Pre-C5-style naive structural validation of an interpretation payload."""
    findings = []
    questions = (payload or {}).get("questions") or []
    for question in questions:
        number = question.get("question_number")
        max_marks = question.get("max_marks")
        if max_marks != "unspecified" and (
                not isinstance(max_marks, int) or isinstance(max_marks, bool)
                or max_marks < 1):
            findings.append("baseline-max-invalid")
            continue
        criteria = question.get("criteria") or []
        marks = [criterion.get("marks") for criterion in criteria]
        if marks and all(
                isinstance(m, int) and not isinstance(m, bool) for m in marks):
            if sum(marks) != max_marks:
                findings.append("baseline-total-mismatch")
    return sorted(set(findings))


def _bound_evidence_ratio(payload, evidence):
    """Fraction of evidence-citing criteria whose quotes exist on the page."""
    cited = 0
    bound = 0
    page_text = " ".join(
        page.get("text", "") for page in (evidence.get("pages") or []))
    pages = {page.get("page") for page in (evidence.get("pages") or [])}
    for question in (payload.get("questions") or []):
        for criterion in question.get("criteria") or []:
            evidence_text = criterion.get("source_evidence")
            if not evidence_text:
                continue
            cited += 1
            page_ok = evidence.get("pages") and (
                criterion.get("source_page") in pages)
            if page_ok and evidence_text in page_text:
                bound += 1
    if not cited:
        return None
    return round(bound / cited, 4)


def run_case(case, defaults=None):
    """Run one case through the reviewer and score it against expectations."""
    import scheme_review as reviewer

    defaults = defaults or {}
    payload = case["payload"]
    entry = case.get("entry", "build_review")
    if entry == "maxima":
        reference = reviewer.review_extracted_maxima(
            payload.get("maxima", {}), payload.get("answers", {}))
        evidence_ratio = None
        alt_declared = False
    else:
        reference = reviewer.build_review(payload, **_case_kwargs(case))
        evidence_ratio = None
        if case.get("evidence"):
            evidence_ratio = _bound_evidence_ratio(payload,
                                                   evidence_document())
        alt_declared = any(
            criterion.get("alternative_group") is not None
            for question in payload.get("questions") or []
            for criterion in question.get("criteria") or [])
    verified = reference.get("verified") or {}
    findings = [finding for finding in verified.get("findings", [])
                if not finding.get("teacher_resolved")]
    rule_ids = sorted({finding.get("rule_id") or finding.get("category", "?")
                       for finding in findings})
    expected = sorted(case.get("expected_rule_ids") or [])
    missing = [rule for rule in expected if rule not in rule_ids]
    unexpected = [rule for rule in rule_ids if rule not in expected]
    clean = bool(case.get("clean"))
    status = (reference.get("teacher") or {}).get("status")
    escalation_ok = status == (STATUS_CLEAN_EXPECTED if clean
                               else STATUS_DEFECT_EXPECTED)
    alt_flagged = ALT_GROUP_REVIEW_RULE in rule_ids
    baseline = baseline_findings(payload)
    baseline_ok = not (case.get("baseline_detects") or []) or all(
        token in baseline for token in case["baseline_detects"])
    passes = (not missing and not unexpected and escalation_ok
              and (not clean or not rule_ids)
              and (not alt_declared or alt_flagged)
              and (evidence_ratio is None
                   or evidence_ratio >= (case.get("min_evidence_ratio", 1.0)))
              and baseline_ok)
    return {
        "id": case.get("id"),
        "capability": case.get("capability"),
        "clean": clean,
        "rule_ids": rule_ids,
        "expected_rule_ids": expected,
        "missing": missing,
        "unexpected": unexpected,
        "status": status,
        "escalation_ok": escalation_ok,
        "alt_declared": alt_declared,
        "alt_flagged": alt_flagged,
        "evidence_bound_ratio": evidence_ratio,
        "baseline_rule_ids": sorted(baseline),
        "baseline_detects_expected": baseline_ok,
        "pass": passes,
    }


def evaluate(cases=None):
    """Run every case and aggregate bounded metrics."""
    if cases is None:
        cases = load_cases()
    results = [run_case(case) for case in cases]
    defect = [result for result in results if not result["clean"]]
    clean_results = [result for result in results if result["clean"]]
    expected_rules = [rule for result in results for rule in
                      result["expected_rule_ids"]]
    missing_rules = [rule for result in results for rule in result["missing"]]
    flagged_alt = [result for result in results if result["alt_declared"]]
    evidence_ratios = [result["evidence_bound_ratio"] for result in results
                       if result["evidence_bound_ratio"] is not None]
    baseline_compared = [result for result in defect
                         if not result["rule_ids"] == []]
    baseline_beyond = [result for result in defect
                       if result["rule_ids"] and not result["baseline_rule_ids"]]
    summary = {
        "schema": EVAL_SCHEMA,
        "cases_total": len(results),
        "cases_passed": sum(1 for result in results if result["pass"]),
        "defect_cases": len(defect),
        "clean_cases": len(clean_results),
        "clean_cases_flagged": sum(1 for result in clean_results
                                   if result["rule_ids"]),
        "false_positive_rate": round(
            sum(1 for result in clean_results if result["rule_ids"])
            / max(len(clean_results), 1), 4),
        "expected_rules_total": len(expected_rules),
        "expected_rules_detected": len(expected_rules) - len(missing_rules),
        "escalation_correct": sum(1 for result in results
                                  if result["escalation_ok"]),
        "alt_cases_total": len(flagged_alt),
        "alt_cases_flagged": sum(1 for result in flagged_alt
                                 if result["alt_flagged"]),
        "evidence_bound_min": min(evidence_ratios) if evidence_ratios else None,
        "evidence_bound_avg": round(sum(evidence_ratios)
                                    / len(evidence_ratios), 4)
        if evidence_ratios else None,
        "defect_cases_detected": len(baseline_compared),
        "baseline_beyond_count": len(baseline_beyond),
        "baseline_beyond_ids": [result["id"] for result in baseline_beyond],
        "summary_ok": all(result["pass"] for result in results),
        "case_results": results,
    }
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the synthetic Phase C5 scheme-review eval set.")
    parser.add_argument("--cases", default=None,
                        help="Alternate case JSON path.")
    arguments = parser.parse_args(argv)
    cases = load_cases(arguments.cases)
    summary = evaluate(cases)
    summary["baseline_description"] = BASELINE_DESCRIPTION
    print(json.dumps(summary, indent=2, sort_keys=False))
    return 0 if summary["summary_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
