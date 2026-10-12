"""Offline same-input PDF inspection comparison, with optional historical baseline.

Default: no PyMuPDF import or dependency. --baseline-module PATH loads a trusted
local snapshot of PR9's old local_pdf.py for a one-time comparison, if its historical
dependency is installed in an isolated benchmark environment. Never contacts APIs.
"""
import argparse
import importlib.util
import importlib.metadata
import json
import platform
import statistics
import sys
import time
from pathlib import Path
from io import BytesIO
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_pdf import inspect_pages
from pdf_cases import make_pdf, render_page
from pypdf import PdfReader


def measurements(function, data, repeats=20):
    function(data, [3, 1])  # Warm imports, native font caches and code paths.
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        result = function(data, [3, 1])
        samples.append((time.perf_counter() - started) * 1000)
    return {'median_ms': round(statistics.median(samples), 3),
            'min_ms': round(min(samples), 3), 'max_ms': round(max(samples), 3),
            'excerpt_bytes': len(result['pdf']), 'preview_bytes': len(result['preview'])}


def benchmark(baseline_path=None):
    baseline = None
    if baseline_path:
        spec = importlib.util.spec_from_file_location('trusted_pdf_baseline', baseline_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        baseline = module.inspect_pages
    result = {'live_api_requests': 0, 'python': platform.python_version(), 'platform': platform.system(),
              'versions': {n: importlib.metadata.version(n) for n in ('pypdf', 'pypdfium2', 'Pillow', 'reportlab')},
              'repeats': 20, 'cases': {}}
    for name, options in [('digital_ink', {}), ('image_scan', {'scanned': True}), ('form_ink', {'forms': True})]:
        data = make_pdf(**options)
        replacement = inspect_pages(data, [3, 1])
        assert len(PdfReader(BytesIO(replacement['pdf'])).pages) == 2
        for index, original in enumerate([2, 0]):
            with render_page(data, original) as before, render_page(replacement['pdf'], index) as after:
                assert before.size == after.size and before.tobytes() == after.tobytes()
        row = {'source_bytes': len(data), 'source_pages': 6, 'selected_pages': [3, 1],
               'rendered_pixels_preserved': True, 'replacement': measurements(inspect_pages, data)}
        if baseline:
            row['pr9_pymupdf_baseline'] = measurements(baseline, data)
        # pypdf alone is useful for text/page copies but has no page renderer.
        values = []
        for _ in range(20):
            started = time.perf_counter()
            reader = PdfReader(BytesIO(data))
            _ = [reader.pages[n].extract_text() for n in [2, 0]]
            values.append((time.perf_counter() - started) * 1000)
        row['pypdf_only_two_page_text_median_ms'] = round(statistics.median(values), 3)
        result['cases'][name] = row
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline-module', type=Path)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.baseline_module), indent=2))
