"""Phase C5 consolidation differential harness.

Runs shared synthetic scenarios (``c5_differential_cases.json``) through BOTH
original reviewers — Implementation A (``investigation.scheme_review``) and
Implementation B (repo-root ``scheme_review``) — and against an INDEPENDENT
oracle. The oracle expected outcomes are authored from explicit Edexcel-style
M/A/B rules in the cases file; they are never derived from either engine.

Purpose:
- Before consolidation: expose meaningful behavioural divergence between the
  two implementations (Phase 10).
- After consolidation: prove the authoritative engine matches the oracle and
  that the key safety properties from BOTH implementations are preserved.

This module is investigation-only and offline. It imports no network, UI or
provider SDK. Import of Implementation A is deliberately tolerant so the
harness keeps working once A is labelled legacy.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
CASES_PATH = os.path.join(HERE, "c5_differential_cases.json")
CASES_SCHEMA = "investigation.c5_differential_cases/1"

PAGE = 1

# The two engines use different (both correct) rule vocabularies for the same
# semantic condition. The oracle matches on the authoritative engine's id but
# records the equivalents so divergence between original A and B is legible.
RULE_ALIASES = {
    "C5-CRITERIA-MISSING": ["C5-CRITERION-MISSING"],
    "C5-TOTAL-MISMATCH": ["C5-TOTAL-OVERCOUNT"],
    "C5-ALT-SUM-REVIEW": ["C5-ALTERNATIVE-STRUCTURE-UNKNOWN"],
    "C5-EVIDENCE-CONFLICT": ["C5-SOURCE-EVIDENCE-MISMATCH"],
}


# ---------------------------------------------------------------------------
# Independent input translators (shared scenario -> each engine's shape).


def _criteria_of(case):
    return case["criteria"]


def build_b_payload(case):
    """Scenario -> Implementation B interpretation payload (root scheme_review)."""
    criteria = []
    for criterion in _criteria_of(case):
        entry = {
            "criterion_id": criterion["id"],
            "code": criterion["code"],
            "marks": criterion["marks"],
        }
        if criterion.get("dependencies"):
            entry["dependencies"] = list(criterion["dependencies"])
        if criterion.get("alternative_group") is not None:
            entry["alternative_group"] = criterion["alternative_group"]
        if criterion.get("alternative_option") is not None:
            entry["alternative_option"] = criterion["alternative_option"]
        if criterion.get("source_page") is not None:
            entry["source_page"] = criterion["source_page"]
        if criterion.get("source_evidence") is not None:
            entry["source_evidence"] = criterion["source_evidence"]
        criteria.append(entry)
    return {"questions": [{
        "question_number": "1",
        "part": None,
        "max_marks": case["total"],
        "criteria": criteria,
    }]}


def build_b_evidence(case):
    """Scenario -> C3-shaped evidence document for B's bind_evidence."""
    if not any(c.get("source_page") is not None for c in _criteria_of(case)):
        return None
    return {"page_count": 5, "pages": [{
        "page": PAGE,
        "text": case.get("page_text", ""),
        "status": "extracted",
        "warnings": [],
        "objects": [],
    }]}


def _claim_for(criterion):
    part = "(a)"
    mark_type = {"M": "M", "A": "A", "B": "B"}.get(
        criterion["code"][:1], "UNKNOWN")
    source = None
    if criterion.get("source_page") is not None:
        source = {"document_role": "mark_scheme",
                  "page": criterion["source_page"],
                  "excerpt": criterion.get("source_evidence")}
    claim = {
        "part": part,
        "mark_type": mark_type,
        "marks": criterion["marks"],
        "extraction_confidence": "high",
        "source": source,
    }
    if criterion.get("alternative_option") is not None:
        claim["alternative_option"] = criterion["alternative_option"]
    return claim


def _a_document(case):
    """Valid C3 document evidence record; reuses A's proven builder shape."""
    from investigation.test_scheme_review import _document
    return _document(pages=[case.get("page_text", "")])


def build_a_payload(case):
    """Scenario -> Implementation A (investigation) review request."""
    criteria = []
    claims = {}
    for criterion in _criteria_of(case):
        entry = {"id": criterion["id"], "code": criterion["code"],
                 "weight": criterion["marks"]}
        if criterion.get("dependencies"):
            entry["dependencies"] = list(criterion["dependencies"])
        if criterion.get("alternative_group") is not None:
            entry["alternative_group"] = criterion["alternative_group"]
        criteria.append(entry)
        claims[criterion["id"]] = _claim_for(criterion)
    sha = _a_document(case)["sha256"]
    document = _a_document(case)
    return {
        "schema": "investigation.scheme_review/1",
        "specification": "synthetic differential fixture",
        "assignment": {"assignment_id": "assignment-1", "paper_id": "4MA1-1H",
                       "session": "synthetic", "revision": 1,
                       "scheme_sha256": sha},
        "reviewer_id": "teacher:1",
        "proposed_scheme": {"provenance": {"author": "synthetic", "verified": False},
                            "questions": [{"id": "Q1", "total": case["total"],
                                           "criteria": criteria}]},
        "criterion_claims": claims,
        "source_documents": {"mark_scheme": document},
        "question_mapping": None,
        "teacher_decisions": [],
    }


# ---------------------------------------------------------------------------
# Engine runners (each tolerates absence of its engine).


def run_b(case):
    import scheme_review as sr
    payload = build_b_payload(case)
    evidence = build_b_evidence(case)
    record = sr.build_review(payload, evidence_document=evidence)
    rules = sorted({f["rule_id"] for f in record["verified"]["findings"]})
    blocking = bool(sr._blocking_findings(record))
    return {"status": record["teacher"]["status"], "rule_ids": rules,
            "blocking": blocking}


def run_a(case):
    try:
        import investigation.scheme_review as a_review
    except Exception as exc:  # pragma: no cover - legacy import guard
        return {"status": "UNAVAILABLE", "rule_ids": [],
                "blocking": False, "error": type(exc).__name__}
    payload = build_a_payload(case)
    try:
        result = a_review.review_scheme(payload)
    except Exception as exc:  # pragma: no cover - defensive
        return {"status": "ERROR", "rule_ids": [], "blocking": False,
                "error": type(exc).__name__}
    c5 = result["verified"]["c5_findings"]
    c2 = result["verified"]["c2_findings"]
    rules = sorted({f["rule_id"] for f in c5})
    c2_rules = sorted({f["rule_id"] for f in c2})
    status = result["teacher"]["review_status"]
    hard = bool(result["verified"]["hard_blockers"])
    return {"status": status, "rule_ids": rules, "c2_rule_ids": c2_rules,
            "blocking": hard}


def load_cases(path=None):
    with open(path or CASES_PATH, encoding="utf-8") as handle:
        data = json.load(handle)
    if data.get("schema") != CASES_SCHEMA:
        raise ValueError(f"unexpected cases schema: {data.get('schema')!r}")
    return data["cases"]


def _status_matches(expected_status, actual_status, rules, expected_rules):
    """CHECKED requires an exact empty finding set; escalation matches by name."""
    if expected_status == "CHECKED":
        return actual_status == "CHECKED" and rules == expected_rules
    return actual_status == expected_status


def _alias_ok(actual_rules, expected_rules):
    """True when A's rule set is a semantic equivalent of the oracle.

    Mapping is exact on the authoritative ids plus any aliased equivalents.
    """
    if actual_rules == expected_rules:
        return True
    expanded = set(expected_rules)
    for rule in expected_rules:
        expanded.update(RULE_ALIASES.get(rule, []))
    return set(actual_rules) <= expanded


def evaluate():
    cases = load_cases()
    rows = []
    for case in cases:
        expect = case["expect"]
        consolidated = run_b(case)
        original_a = run_a(case)
        rules = consolidated["rule_ids"]
        expected_rules = sorted(expect["authoritative_rule_ids"])
        status_ok = _status_matches(
            expect["authoritative_status"], consolidated["status"],
            rules, expected_rules)
        rules_ok = rules == expected_rules
        blocking_ok = consolidated["blocking"] == expect["blocking"]
        a_status_ok = original_a["status"] == expect["authoritative_status"]
        a_rules_ok = _alias_ok(original_a.get("rule_ids", []), expected_rules)
        rows.append({
            "id": case["id"],
            "expected_status": expect["authoritative_status"],
            "expected_rules": expected_rules,
            "consolidated": consolidated,
            "original_a": original_a,
            "status_ok": status_ok,
            "rules_ok": rules_ok,
            "blocking_ok": blocking_ok,
            "a_status_matches_oracle": a_status_ok,
            "a_rules_semantically_match": a_rules_ok,
            "pass": status_ok and rules_ok and blocking_ok,
        })
    summary = {
        "cases": len(rows),
        "passed": sum(1 for row in rows if row["pass"]),
        "all_matched": all(row["pass"] for row in rows),
        # Divergence evidence: original A vs the oracle on the same cases.
        "original_a_status_matches_oracle": sum(
            1 for row in rows if row["a_status_matches_oracle"]),
        "original_a_rules_semantically_match": sum(
            1 for row in rows if row["a_rules_semantically_match"]),
        "separation": "synthetic structural differential only; no official "
                      "mark-scheme claim, no student data, no live provider",
    }
    return {"summary": summary, "cases": rows}


def main():  # pragma: no cover - CLI entry
    report = evaluate()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["summary"]["all_matched"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
