"""Offline CPU measurements + explicitly SIMULATED Gemini reference latency.
Run with the PDF test requirements and SymPy 1.14.0. Never contacts Gemini.
"""
import ast
from contextlib import closing
import cProfile
import io
import json
import pstats
import statistics
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from support import load_app
from pipeline_fakes import Clock, Provider, uploaded
from pdf_cases import make_pdf
from test_reference_performance import payload
import document_pipeline as dp
from local_pdf import inspect_pages


def timed(fn, repeats=20):
    values = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        values.append((time.perf_counter() - start) * 1000)
    return round(statistics.median(values), 4)


def preparation(old=False, scanned=False):
    f = load_app()
    if old:
        exec((Path(__file__).parent / 'fixtures/reference_baseline.py').read_text(encoding='utf-8'), f)
    clock = Clock()
    client = Provider(clock, transfer=0, processing=0)
    def generate(**kwargs):
        clock.sleep(15)  # Explicit scenario assumption, not a live timing.
        body = payload() if 'TOTAL maximum marks' in kwargs['contents'][-1] else {'question_numbers': ['1', '2']}
        return SimpleNamespace(text=json.dumps(body))
    client.models = SimpleNamespace(generate_content=Mock(side_effect=generate))
    f['select_spread_questions'] = lambda nums, count: nums[:count]
    f['model_on_quota_cooldown'] = lambda _: False
    f['remember_model_failure'] = lambda *_: None
    session = {'files': dp.FileCache('synthetic-account', clock=clock), 'metadata': dp.Memo(clock=clock), 'results': dp.Memo(clock=clock)}
    student, scheme = uploaded(make_pdf(scanned), 'student.pdf'), uploaded(make_pdf(), 'scheme.pdf')
    with_run = dp.DocumentRun(client, session['files'])
    try:
        plan = f['prepare_manual_batch'](client, [student], scheme, None, False, 1, session, with_run)
        assert plan['selected'] == ['1'] and plan['scheme_info'][0] == {'1': 2}
    finally:
        with_run.close()
    output = {'metadata_generations': client.models.generate_content.call_count,
              'uploads': client.files.upload.call_count, 'virtual_metadata_seconds': clock.seconds}
    # A completed repeat stays zero-request, including the random inventory.
    session['results'].put(plan['keys'][0], {'status': 'Marked'})
    client.models.generate_content.reset_mock(); client.files.upload.reset_mock()
    repeat = dp.DocumentRun(client, session['files'])
    try:
        f['prepare_manual_batch'](client, [student], scheme, None, False, 1, session, repeat)
    finally:
        repeat.close()
    output['completed_repeat_requests'] = client.models.generate_content.call_count + client.files.upload.call_count
    return output


def benchmark():
    start = time.perf_counter()
    import sympy
    sympy_import_ms = (time.perf_counter() - start) * 1000
    import pypdfium2 as pdfium
    f = load_app()
    # Typed AST construction only: never sympify/parse_expr/eval student strings.
    def symbolic(expression):
        def visit(n):
            if isinstance(n, ast.Constant) and type(n.value) in (int, float):
                return sympy.Rational(str(n.value))
            if isinstance(n, ast.BinOp) and type(n.op) in (ast.Add, ast.Sub, ast.Mult, ast.Div):
                a, b = visit(n.left), visit(n.right)
                return {ast.Add: lambda: a+b, ast.Sub: lambda: a-b,
                        ast.Mult: lambda: a*b, ast.Div: lambda: a/b}[type(n.op)]()
            raise ValueError('Unsupported synthetic expression')
        return visit(ast.parse(expression, mode='eval').body)
    expressions = ['(67 + 1) / 2', '24+60+234+366+230+112', '1690/60', '0*5+1*6+2*2+3*3+4*2']
    numeric = timed(lambda: [f['_numeric_expression_value'](e) for e in expressions], 200)
    symbolic_time = timed(lambda: [symbolic(e) for e in expressions], 200)
    old = load_app()
    exec((Path(__file__).parent / 'fixtures/reference_baseline.py').read_text(encoding='utf-8'), old)
    chains = ['1690/60 = 28.16... = 28.2', '1/3 = 0.333... = 0.33',
              '(67+1)/2 = 34', '24+60+234+366+230+112 = 1040']
    validation_before = timed(lambda: [old['_arithmetic_equals_issues'](e) for e in chains], 200)
    validation_after = timed(lambda: [f['_arithmetic_equals_issues'](e) for e in chains], 200)
    pdfs = {}
    for scanned in (False, True):
        data = make_pdf(scanned)
        def text_only():
            with pdfium.PdfDocument(data) as doc:
                values = []
                for n in range(len(doc)):
                    with closing(doc[n]) as page, closing(page.get_textpage()) as text:
                        values.append(text.get_text_range(0, min(20000, text.count_chars())))
                return values
        texts = text_only()
        excerpt = inspect_pages(data, [1, 3])
        pdfs['synthetic_scan' if scanned else 'digital_with_pen_strokes'] = {
            'original_bytes': len(data), 'excerpt_bytes': len(excerpt['pdf']),
            'digital_characters': sum(map(len, texts)),
            'all_page_text_median_ms': timed(text_only),
            'two_page_review_with_preview_median_ms': timed(lambda: inspect_pages(data, [1, 3])),
            'before': preparation(True, scanned), 'after': preparation(False, scanned)}
    profile = cProfile.Profile()
    profile.enable()
    for _ in range(100):
        for text in ('(67+1)/2 = 34', '24+60+234+366+230+112 = 1040', '1690/60 = 28.16... = 28.2'):
            f['_arithmetic_equals_issues'](text)
    profile.disable()
    buf = io.StringIO()
    pstats.Stats(profile, stream=buf).strip_dirs().sort_stats('cumulative').print_stats(8)
    return {'live_gemini_requests': 0, 'scenario': 'Reference metadata only; 15 virtual seconds per generation, zero transfer/processing delay. Marking/audit latency unchanged.',
            'pdfs': pdfs, 'numeric_four_expressions_median_ms': numeric,
            'sympy_four_expressions_median_ms': symbolic_time, 'sympy_import_ms': round(sympy_import_ms, 2),
            'four_equality_checks_before_median_ms': validation_before,
            'four_equality_checks_after_median_ms': validation_after,
            'validation_profile': buf.getvalue()}


if __name__ == '__main__':
    print(json.dumps(benchmark(), indent=2))
