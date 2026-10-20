"""Offline PDF parser-isolation deadline benchmark (post-S6 finding S6-R2).

Measures the per-document cost of the isolated (child-process, hard-deadline)
deep PDF metadata inspection against the historic same-process path on
synthetic documents, and proves that a stalled parser is terminated and the
calling worker recovers. Deterministic; never contacts an API.
"""
import json
import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import document_limits
from pdf_cases import make_pdf


def median_of(function, data, warm, repeats):
    for _ in range(warm):
        function(data)
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        function(data)
        samples.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(samples), 1)


def deadline_kills_stalled_parser():
    document_limits._parser_isolation_disabled = False
    original = document_limits._child_parse_command
    original_budget = document_limits.PDF_PARSE_TIMEOUT_SECONDS
    try:
        document_limits._child_parse_command = (
            lambda: [sys.executable, "-c", "import time; time.sleep(30)"])
        document_limits.PDF_PARSE_TIMEOUT_SECONDS = 2
        started = time.time()
        try:
            document_limits._isolated_pdf_limits(b"%PDF-1.4 synthetic")
            raise AssertionError("stalled parser should be rejected")
        except document_limits.DocumentValidationError as rejected:
            terminated = rejected.code == document_limits.PDF_PARSE_TIMEOUT
        elapsed = round(time.time() - started, 1)
        # The worker must recover after the kill with normal parsing.
        document_limits._child_parse_command = original
        document_limits.PDF_PARSE_TIMEOUT_SECONDS = original_budget
        data = make_pdf()
        assert document_limits._isolated_pdf_limits(data)[1] >= 1
        return {"terminated": terminated, "kill_seconds": elapsed,
                "worker_recovered": True}
    finally:
        document_limits._child_parse_command = original
        document_limits.PDF_PARSE_TIMEOUT_SECONDS = original_budget


def benchmark():
    dl = document_limits
    dl._parser_isolation_disabled = False
    result = {"live_api_requests": 0, "python": platform.python_version(),
              "platform": platform.system(),
              "parser_budget_seconds": dl.PDF_PARSE_TIMEOUT_SECONDS,
              "isolated_children_limit": dl.PDF_PARSE_MAX_CONCURRENT,
              "cases": {}, "deadline_check": deadline_kills_stalled_parser()}
    for name, options in (("digital_ink", {}), ("image_scan", {"scanned": True})):
        data = make_pdf(**options)
        isolated = median_of(dl._isolated_pdf_limits, data, 3, 8)
        in_process = median_of(dl._in_process_pdf_limits, data, 3, 30)
        result["cases"][name] = {
            "source_bytes": len(data),
            "isolated_median_ms": isolated,
            "in_process_median_ms": in_process,
            "isolation_overhead_median_ms": round(isolated - in_process, 1),
        }
    return result


if __name__ == "__main__":
    print(json.dumps(benchmark(), indent=2))
