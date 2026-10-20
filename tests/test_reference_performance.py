"""Real preparation functions with only the Gemini boundary replaced."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from support import load_app, fake_client
from pipeline_fakes import Clock, Provider, uploaded
from test_pdf_processing import synthetic_pdf
import document_pipeline as dp


def payload(**changes):
    return dict({"questions": [{"question_number": "1", "max_marks": 2, "answer_summary": "4"}],
                 "question_numbers": ["1", "2"], "inventory_complete": True}, **changes)


class ReferencePerformanceTests(unittest.TestCase):
    def setUp(self):
        self.f = load_app()
        self.clock = Clock()
        self.memo = dp.Memo(clock=self.clock)
        self.f['model_on_quota_cooldown'] = lambda _: False
        self.f['remember_model_failure'] = lambda *_: None
        self.f['PRIMARY_MODELS'] = ['synthetic-model']

    def prepare(self, client, key='scheme', inventory='numbers', question=None):
        return self.f['prepare_reference_metadata'](client, 'original scheme', self.memo, key, inventory, question)

    def test_one_call_inventory_includes_question_with_unknown_maximum(self):
        client = fake_client([payload()])
        info, numbers = self.prepare(client, question='original question paper')
        self.assertEqual(numbers, ['1', '2'])
        self.assertEqual(info[0], {'1': 2})
        self.assertEqual(client.models.generate_content.call_count, 1)
        args = client.models.generate_content.call_args.kwargs
        self.assertEqual(args['contents'][:2], ['original scheme', 'original question paper'])
        self.assertEqual(args['config']['http_options']['retry_options']['attempts'], 1)
        self.assertIn('ONLY from the official mark scheme', args['contents'][-1])

    def test_invalid_inventory_falls_back_without_reextracting_scheme(self):
        for changes in ({'inventory_complete': False}, {'inventory_complete': 'true'},
                        {'question_numbers': ['2']}, {'question_numbers': ['1', '1']},
                        {'question_numbers': '12'}, {'question_numbers': ['1', None]}):
            with self.subTest(changes=changes):
                self.memo = dp.Memo(clock=self.clock)
                client = fake_client([payload(**changes), {'question_numbers': ['1', '2', '3']}])
                info, numbers = self.prepare(client)
                self.assertEqual(numbers, ['1', '2', '3'])
                self.assertEqual(info[0], {'1': 2})
                self.assertEqual(client.models.generate_content.call_count, 2)

    def test_truncated_response_not_cached_or_used_as_inventory(self):
        import json
        response = SimpleNamespace(text=json.dumps(payload()), candidates=[SimpleNamespace(finish_reason='MAX_TOKENS')])
        client = fake_client([response, {'question_numbers': ['1', '2']}])
        info, numbers = self.prepare(client)
        self.assertTrue(info[2])
        self.assertIsNone(self.memo.get(('scheme', 'scheme')))
        self.assertEqual(numbers, ['1', '2'])

    def test_warm_zero_calls_changed_question_only_discovers_expiry_reextracts(self):
        client = fake_client([payload(), {'question_numbers': ['1', '2', '3']}, payload()])
        self.prepare(client)
        self.prepare(client)
        self.assertEqual(client.models.generate_content.call_count, 1)
        self.prepare(client, inventory='changed-paper', question='new paper')
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertNotIn('questions', client.models.generate_content.call_args.kwargs['config']['response_schema']['properties'])
        self.clock.sleep(601)
        self.prepare(client)
        self.assertEqual(client.models.generate_content.call_count, 3)

    def test_assignment_and_session_isolation(self):
        client = fake_client([payload(), payload(), payload()])
        self.prepare(client, key=('account', 'assignment1'), inventory=('account', 'assignment1', 'numbers'))
        self.prepare(client, key=('account', 'assignment2'), inventory=('account', 'assignment2', 'numbers'))
        self.memo = dp.Memo()
        self.prepare(client, key=('account', 'assignment1'), inventory=('account', 'assignment1', 'numbers'))
        self.assertEqual(client.models.generate_content.call_count, 3)

    def test_manual_and_classroom_actual_entrypoints_use_one_call_and_exact_bytes(self):
        for classroom in (False, True):
            with self.subTest(classroom=classroom):
                clock = Clock()
                client = Provider(clock, transfer=0, processing=0)
                client.models = fake_client([payload()]).models
                session = {'files': dp.FileCache('account', clock=clock), 'metadata': dp.Memo(clock=clock), 'results': dp.Memo(clock=clock)}
                run = dp.DocumentRun(client, session['files'])
                scheme_data = synthetic_pdf(2)
                scheme = uploaded(scheme_data, 'scheme.pdf')
                self.f['select_spread_questions'] = lambda numbers, count: numbers[:count]
                try:
                    if classroom:
                        _, info, numbers = self.f['prepare_classroom_reference'](client, scheme, session, run, 'assignment', 1, lambda *_: None)
                    else:
                        result = self.f['prepare_manual_batch'](client, [uploaded(b'%PDF-original-student')], scheme, None, False, 1, session, run)
                        info = result['scheme_info']
                        self.assertEqual(result['selected'], ['1'])
                        self.assertIn(b'%PDF-original-student', client.data.values())
                    self.assertEqual(info[0], {'1': 2})
                    self.assertIn(scheme_data, client.data.values())
                    self.assertEqual(client.models.generate_content.call_count, 1)
                finally:
                    run.close()

    def test_question_change_does_not_reextract_scheme_but_invalidates_results(self):
        client = Provider(self.clock, transfer=0, processing=0)
        client.models = fake_client([payload()]).models
        session = {'files': dp.FileCache('account'), 'metadata': self.memo, 'results': dp.Memo()}
        plans = []
        for question in (b'%PDF- paper one', b'%PDF- paper two'):
            run = dp.DocumentRun(client, session['files'])
            try:
                plan = self.f['prepare_manual_batch'](client, [uploaded(b'%PDF-student')], uploaded(b'%PDF-scheme'), uploaded(question), False, None, session, run)
                plans.append(plan)
                session['results'].put(plan['keys'][0], {'status': 'Marked'})
            finally:
                run.close()
        self.assertNotEqual(plans[0]['keys'], plans[1]['keys'])
        self.assertEqual(plans[1]['pending'], [0])
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_numeric_chain_parsing_reused_only_within_one_explanation(self):
        original = self.f['_numeric_expression_value']
        self.f['_numeric_expression_value'] = Mock(wraps=original)
        text = '1690/60 = 28.16... = 28.2'
        self.assertEqual(self.f['_arithmetic_equals_issues'](text), [])
        calls = [c.args[0] for c in self.f['_numeric_expression_value'].call_args_list]
        self.assertEqual(calls.count('28.16'), 1)
        before = len(calls)
        self.f['_arithmetic_equals_issues'](text)
        self.assertEqual(self.f['_numeric_expression_value'].call_count, before * 2)
        self.assertTrue(self.f['_arithmetic_equals_issues']('1690/60 = 28.16... = 28.1'))

    def test_validator_matches_pr8_on_correct_and_incorrect_synthetic_chains(self):
        from pathlib import Path
        old = load_app()
        exec((Path(__file__).parent / 'fixtures/reference_baseline.py').read_text(encoding='utf-8'), old)
        cases = ['1690/60 = 28.16... = 28.2', '1690/60 = 28.16... = 28.1',
                 '1/3 = 0.(3)', '1/0 = 0', '67 + 1 / 2 = 34', '(67+1)/2 = 34']
        for n in range(-20, 21):
            for denominator in (3, 7):
                for error in (0, 0.1):
                    cases.append(f'{n}/{denominator} = {n/denominator:.4f} = {n/denominator+error:.1f}')
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(self.f['_arithmetic_equals_issues'](text), old['_arithmetic_equals_issues'](text))
