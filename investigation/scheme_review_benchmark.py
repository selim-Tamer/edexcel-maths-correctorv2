"""Phase C Stage 5 synthetic scheme-review benchmark (investigation-only).

Independent of every earlier benchmark and every C1-C4 fixture. All cases are
synthetic records invented for engineering tests. Nothing here is Pearson
material, a marking policy or a claim about a real mark scheme: the benchmark
measures whether the C5 reviewer reports the *structural* problems placed in
its own synthetic input, and whether it leaves clean input alone.

The benchmark uses the same public entry point as callers
(``review_scheme``). Providers are local callables; there are no network
calls, no credentials and no student data. Run directly to print a
deterministic summary; exit code 0 requires every expectation to match.
"""

import json
import os

from investigation import scheme_review as review

BENCHMARK_SCHEMA = "investigation.scheme_review_benchmark/1"
CASES_SCHEMA = "investigation.scheme_review_cases/1"
CASES_PATH = os.path.join(os.path.dirname(__file__), "scheme_review_cases.json")
SHA = "a" * 64

DEFAULT_PAGES = [
    "Q1 (a) M1: correct method\nA1: answer 4\n(b) B1: state units",
]


class _ProviderTimeout(Exception):
    pass


class _ProviderQuota(Exception):
    status_code = 429


class _ProviderBlocked(Exception):
    pass


def _document(role, pages, sha):
    return {
        "schema": "investigation.pdf_evidence/1",
        "document_id": role + "-doc",
        "document_role": role,
        "sha256": sha,
        "page_count": len(pages),
        "extraction_status": "extracted",
        "warnings": [],
        "pages": [
            {
                "page": index,
                "text": text,
                "status": "extracted",
                "warnings": [],
                "objects": None,
            }
            for index, text in enumerate(pages, start=1)
        ],
    }


def build_payload(case, defaults):
    """Build one strict review request from a compact benchmark case."""
    pages = case.get("scheme_pages", defaults["scheme_pages"])
    sources = {}
    if not case.get("sources_absent"):
        sources["mark_scheme"] = _document("mark_scheme", pages, SHA)
    if case.get("paper_pages"):
        sources["question_paper"] = _document(
            "question_paper", case["paper_pages"], SHA)
    assignment = {
        "assignment_id": "assignment-1",
        "paper_id": "4MA1-1H",
        "session": "synthetic benchmark",
        "revision": 1,
        "scheme_sha256": SHA,
    }
    assignment.update(case.get("assignment") or {})
    return {
        "schema": "investigation.scheme_review/1",
        "specification": "synthetic C5 benchmark fixture",
        "assignment": assignment,
        "reviewer_id": case.get("reviewer", "teacher:1"),
        "proposed_scheme": {
            "provenance": {
                "author": "synthetic C5 benchmark",
                "verified": False,
            },
            "questions": case["questions"],
        },
        "criterion_claims": case["claims"],
        "source_documents": sources,
        "question_mapping": None,
        "teacher_decisions": case.get("decisions") or [],
    }


def build_provider(spec):
    """Return a local callable implementing a synthetic provider outcome."""
    if spec is None:
        return None
    calls = []

    def provider(packet):
        calls.append(packet)
        if "error" in spec:
            error = spec["error"]
            if error == "timeout":
                raise _ProviderTimeout()
            if error == "quota":
                raise _ProviderQuota()
            if error == "blocked":
                raise _ProviderBlocked()
            raise RuntimeError("synthetic provider failure")
        if "payload" in spec:
            return spec["payload"]
        return {"findings": spec.get("findings", [])}

    provider.calls = calls
    return provider


def _ids(findings):
    return sorted(finding["rule_id"] for finding in findings)


def _multiset(items):
    counts = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return counts


def _compare_multiset(expected, actual):
    expected_counts = _multiset(expected)
    actual_counts = _multiset(actual)
    true_positive = sum(
        min(count, actual_counts.get(rule, 0))
        for rule, count in expected_counts.items())
    false_negative = sum(
        max(0, count - actual_counts.get(rule, 0))
        for rule, count in expected_counts.items())
    false_positive = sum(
        max(0, count - expected_counts.get(rule, 0))
        for rule, count in actual_counts.items())
    return true_positive, false_negative, false_positive


def evaluate_case(case, defaults):
    payload = build_payload(case, defaults)
    provider = build_provider(case.get("provider"))
    result = review.review_scheme(payload, provider=provider)
    expect = case["expect"]

    actual_c5 = _ids(result["verified"]["c5_findings"])
    actual_c2 = _ids(result["verified"]["c2_findings"])
    actual_provider_categories = sorted(
        finding["issue_category"]
        for finding in result["provider_review"]["findings"])
    expected_provider_categories = expect.get("provider_categories", [])

    c5_tp, c5_fn, c5_fp = _compare_multiset(
        expect.get("c5_rules", []), actual_c5)
    c2_tp, c2_fn, c2_fp = _compare_multiset(
        expect.get("c2_rules", []), actual_c2)
    provider_tp, provider_fn, provider_fp = _compare_multiset(
        expected_provider_categories, actual_provider_categories)

    actual_totals = {
        question_id: entry["effective_total"]
        for question_id, entry in
        result["verified"]["effective_totals"].items()}
    expected_totals = expect.get("effective_totals")
    totals_match = (
        expected_totals is None
        or {key: actual_totals.get(key) for key in expected_totals}
        == expected_totals)

    summary = result["verified"]["question_mapping"]
    mapping_questions = (
        [] if summary is None
        else [entry["question_id"] for entry in summary["matches"]])
    expected_mapping = expect.get("mapping_questions")
    mapping_match = (
        expected_mapping is None or mapping_questions == expected_mapping)

    coverage = result["verified"]["evidence_coverage"]["coverage"]
    coverage_match = (
        "coverage" not in expect
        or abs(coverage - expect["coverage"]) < 1e-9)

    status_match = result["teacher"]["review_status"] \
        == expect["review_status"]
    provider_status_match = result["provider_review"]["status"] \
        == expect["provider_status"]
    provider_bounded = (
        result["provider_review"]["request_count"] <= 1
        and result["provider_review"]["max_requests"] == 1
        and (provider is None or len(provider.calls)
             == result["provider_review"]["request_count"]))
    provider_categories_match = provider_tp > 0 or (
        not expected_provider_categories
        and not actual_provider_categories)

    checks = {
        "status": status_match,
        "c5_recall": c5_fn == 0,
        "c5_precision": c5_fp == 0,
        "c2_preservation": c2_fn == 0 and c2_fp == 0,
        "totals": totals_match,
        "mapping": mapping_match,
        "coverage": coverage_match,
        "provider_status": provider_status_match,
        "provider_categories": provider_categories_match,
        "provider_bounded": provider_bounded,
    }
    return {
        "case_id": case["id"],
        "metrics": list(case.get("metrics", [])),
        "expected": {
            "review_status": expect["review_status"],
            "c5_rules": sorted(expect.get("c5_rules", [])),
            "c2_rules": sorted(expect.get("c2_rules", [])),
            "effective_totals": expected_totals,
            "mapping_questions": expected_mapping,
            "provider_status": expect["provider_status"],
            "provider_categories": sorted(expected_provider_categories),
        },
        "actual": {
            "review_status": result["teacher"]["review_status"],
            "c5_rules": actual_c5,
            "c2_rules": actual_c2,
            "effective_totals": actual_totals,
            "mapping_questions": mapping_questions,
            "provider_status": result["provider_review"]["status"],
            "provider_categories": actual_provider_categories,
            "provider_request_count":
                result["provider_review"]["request_count"],
            "coverage": coverage,
        },
        "counts": {
            "c5_true_positive": c5_tp,
            "c5_false_negative": c5_fn,
            "c5_false_positive": c5_fp,
            "c2_true_positive": c2_tp,
            "c2_false_negative": c2_fn,
            "c2_false_positive": c2_fp,
            "provider_true_positive": provider_tp,
            "provider_false_negative": provider_fn,
            "provider_false_positive": provider_fp,
        },
        "checks": checks,
        "passed": all(checks.values()),
    }


def _rate(passed, total):
    return round(passed / total, 4) if total else 1.0


def run_benchmark(path=CASES_PATH):
    with open(path, encoding="utf-8") as handle:
        fixture = json.load(handle)
    if fixture.get("schema") != CASES_SCHEMA:
        raise ValueError("unexpected C5 case-fixture schema")
    defaults = {
        "scheme_pages": fixture.get("defaults", {}).get(
            "scheme_pages", DEFAULT_PAGES),
    }
    results = [evaluate_case(case, defaults) for case in fixture["cases"]]

    c5_tp = sum(item["counts"]["c5_true_positive"] for item in results)
    c5_fn = sum(item["counts"]["c5_false_negative"] for item in results)
    c5_fp = sum(item["counts"]["c5_false_positive"] for item in results)
    c2_tp = sum(item["counts"]["c2_true_positive"] for item in results)
    c2_fn = sum(item["counts"]["c2_false_negative"] for item in results)
    c2_fp = sum(item["counts"]["c2_false_positive"] for item in results)
    provider_tp = sum(
        item["counts"]["provider_true_positive"] for item in results)
    provider_fn = sum(
        item["counts"]["provider_false_negative"] for item in results)
    provider_fp = sum(
        item["counts"]["provider_false_positive"] for item in results)

    groups = {}
    for item in results:
        for metric in item["metrics"]:
            bucket = groups.setdefault(metric, {"cases": 0, "passed": 0})
            bucket["cases"] += 1
            bucket["passed"] += int(item["passed"])
    for bucket in groups.values():
        bucket["accuracy"] = _rate(bucket["passed"], bucket["cases"])

    escalation = {}
    for item in results:
        status = item["expected"]["review_status"]
        bucket = escalation.setdefault(status, {"cases": 0, "matched": 0})
        bucket["cases"] += 1
        bucket["matched"] += int(
            item["actual"]["review_status"] == status)
    for bucket in escalation.values():
        bucket["accuracy"] = _rate(bucket["matched"], bucket["cases"])

    mismatches = sorted(
        item["case_id"] + ":" + ",".join(
            name for name, ok in sorted(item["checks"].items()) if not ok)
        for item in results if not item["passed"])

    total = len(results)
    summary = {
        "schema": BENCHMARK_SCHEMA,
        "fixture_version": fixture.get("benchmark_version"),
        "cases": total,
        "all_matched": not mismatches,
        "mismatches": mismatches,
        "status_accuracy": _rate(
            sum(1 for item in results
                if item["checks"]["status"]), total),
        "finding_recall": _rate(c5_tp, c5_tp + c5_fn),
        "finding_precision": _rate(c5_tp, c5_tp + c5_fp),
        "false_positives": c5_fp,
        "mapping_correct": _rate(
            sum(1 for item in results
                if item["checks"]["mapping"]), total),
        "total_consistency": _rate(
            sum(1 for item in results
                if item["checks"]["totals"]), total),
        "c2_preservation": _rate(
            sum(1 for item in results
                if item["checks"]["c2_preservation"]), total),
        "c2_counts": {
            "true_positive": c2_tp,
            "false_negative": c2_fn,
            "false_positive": c2_fp,
        },
        "provider": {
            "status_accuracy": _rate(
                sum(1 for item in results
                    if item["checks"]["provider_status"]), total),
            "category_recall": _rate(
                provider_tp, provider_tp + provider_fn),
            "category_precision": _rate(
                provider_tp, provider_tp + provider_fp),
            "bounded": all(
                item["checks"]["provider_bounded"] for item in results),
        },
        "groups": {key: groups[key] for key in sorted(groups)},
        "escalation": {key: escalation[key] for key in sorted(escalation)},
        "separation": "synthetic structural review only; no official "
                      "mark-scheme claim, no student data, no live provider",
    }
    return results, summary


def main():
    _, summary = run_benchmark()
    print(json.dumps(summary, indent=2))
    return 0 if summary["all_matched"] else 1


if __name__ == "__main__":          # pragma: no cover - invoked manually
    raise SystemExit(main())
