"""Offline Phase C5 scheme-review benchmark.

Measures the deterministic reviewer's local CPU cost, record size, and
contract bounds on synthetic data, plus mocked assisted-response parsing
overhead. There are no provider calls anywhere: the validated incremental
provider request count is 0 by construction because no client object exists
in the review path. Timings are local medians over repeats, NOT live
deployment measurements, and the synthetic payload bounds (fewer than the
module caps) are scenario sizes, not real mark-scheme claims. No student
data, no real mark schemes, no Pearson material.
"""
import json
import statistics
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scheme_review as sr

BENCHMARK_SCHEMA = "tests.benchmark_scheme_review/1"
REPEATS = 15
QUESTIONS = 20
CRITERIA_PER_QUESTION = 3

PAGE_TEXT = ("M1 valid method\nA1 correct answer\nB1 independent mark\n"
             "M1 alternative method\nA1 answer\nB1 statement")


def criterion(Q, index):
    return {"criterion_id": f"Q{Q}-c{index}",
            "code": "M1" if index % 2 else "A1",
            "marks": 1, "dependencies": [], "follow_through": None,
            "alternative_group": None, "source_page": 2,
            "source_evidence": PAGE_TEXT.splitlines()[0 if index % 2 else 1],
            "note": None}


def synthetic_criteria_payload(questions=QUESTIONS, per=CRITERIA_PER_QUESTION):
    payload = {"questions": []}
    for question in range(1, questions + 1):
        payload["questions"].append({
            "question_number": str(question), "max_marks": per,
            "criteria": [criterion(question, index)
                         for index in range(per)]})
    return payload


def synthetic_maxima(questions=QUESTIONS):
    return {str(number): 1 for number in range(1, questions + 1)}


def evidence_document():
    return {"page_count": 5, "pages": [
        {"page": 2, "text": PAGE_TEXT, "status": "extracted",
         "warnings": [], "objects": []}]}


def question_mapping(questions=QUESTIONS):
    return {"matches": [{"question_id": str(number), "certainty": "explicit",
                         "references": [], "pages": [2]}
                        for number in range(1, questions + 1)],
            "unresolved": []}


def timed(fn):
    values = []
    for _ in range(REPEATS):
        started = time.perf_counter()
        fn()
        values.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(values), 4)


class _MockResponse:
    """Bounded mocked assisted response shape (never a live client)."""

    def __init__(self, text):
        self.candidates = [type("C", (), {"finish_reason": "STOP"})()]
        self.text = text


def benchmark():
    payload = synthetic_criteria_payload()
    document = evidence_document()
    mapping = question_mapping()
    with_evidence = lambda: sr.build_review(payload,
                                            evidence_document=document,
                                            question_mapping=mapping)
    without_evidence = lambda: sr.build_review(payload)
    record = with_evidence()
    record_json = json.dumps(record, sort_keys=True)
    payload_json = json.dumps(payload)
    assisted_text = json.dumps({"findings": [
        {"question_number": "1", "criterion_id": "Q1-c0",
         "rule_id": "C5-ASSIST-DEMO", "severity": "low",
         "explanation": "synthetic mocked finding",
         "suggested_action": "confirm against official scheme"}]})
    metrics = SimpleNamespace(record=record)
    output = {
        "schema": BENCHMARK_SCHEMA,
        "repeats": REPEATS,
        "scenario": f"synthetic scheme: {QUESTIONS} questions, "
                    f"{CRITERIA_PER_QUESTION} criteria each; backend-family "
                    "CPU medians, local machine",
        "deterministic_criteria_review_with_evidence_ms":
            timed(with_evidence),
        "deterministic_criteria_review_without_evidence_ms":
            timed(without_evidence),
        "evidence_binding_overhead_ms": round(
            timed(with_evidence) - timed(without_evidence), 4),
        "deterministic_maxima_review_ms": timed(
            lambda: sr.review_extracted_maxima(synthetic_maxima(),
                                               synthetic_maxima())),
        "teacher_message_bound_ms": timed(
            lambda: sr.teacher_messages(metrics.record)),
        "assisted_mock_parse_ms": timed(
            lambda: sr.parse_assisted_response(
                _MockResponse(assisted_text))),
        "review_record_limit_chars": sr.MAX_ASSISTED_RESPONSE_CHARS,
        "review_record_bytes": len(record_json.encode("utf-8")),
        "review_payload_bytes": len(payload_json.encode("utf-8")),
        "record_to_payload_size_ratio": round(
            len(record_json) / max(len(payload_json), 1), 4),
        "teacher_messages": len(sr.teacher_messages(metrics.record)),
        "teacher_messages_max": sr.MAX_TEACHER_MESSAGES,
        "checks_run": record["verified"]["checks_run"],
        "status": record["teacher"]["status"],
        "provider_requests": 0,
        "provider_requests_note": "no provider client exists in "
            "scheme_review.py; the assisted channel stays disabled "
            "(ASSISTED_REVIEW_ACTIVATED = False) so the incremental "
            "Gemini request cost of C5 is zero by construction",
        "live_calls": "none",
    }
    return output


if __name__ == "__main__":
    print(json.dumps(benchmark(), indent=2))
