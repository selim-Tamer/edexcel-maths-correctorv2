"""Exercise actual marking orchestration, privacy and request budgets offline."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from support import load_app, fake_client, benchmark, model_question, remote
from pipeline_fakes import Clock, ApiError
import marking_requests as mr


class MarkingRequestTests(unittest.TestCase):
    def setUp(self):
        self.f = load_app()
        self.clock = Clock()
        self.f['time'] = self.clock
        self.payload = {'questions': [model_question(benchmark()['questions'][0], 'partial')]}

    def run_grade(self, client, **kw):
        return self.f['grade_with_model'](client, ['gemini-3.8-flash', 'gemini-3.6-flash', 'gemini-3.5-flash'],
            remote(name='private/student.pdf'), remote(name='private/scheme.pdf'), **kw)

    def test_original_pdfs_schema_and_evidence_are_preserved(self):
        client = fake_client([self.payload])
        question = remote(name='private/question.pdf')
        result, model = self.run_grade(client, question_file=question, selected_questions=['1'], scheme_maxima={'1': 2})
        args = client.models.generate_content.call_args.kwargs
        self.assertEqual(args['contents'][2], question)
        self.assertEqual(args['config']['response_schema'], self.f['OUTPUT_SCHEMA'])
        self.assertEqual(args['config']['system_instruction'], self.f['MARKING_SYSTEM'])
        self.assertNotIn('thinking_config', args['config'])
        self.assertNotIn('max_output_tokens', args['config'])
        self.assertEqual(args['config']['http_options']['retry_options']['attempts'], 1)
        self.assertEqual(result, self.payload)
        self.assertEqual(mr.state_for(client)['last']['document_count'], 3)

    def test_usage_and_elapsed_are_private_and_content_free(self):
        response = SimpleNamespace(text=json.dumps(self.payload), candidates=[SimpleNamespace(finish_reason='STOP')],
            model_version='gemini-3.8-flash',
            usage_metadata=SimpleNamespace(prompt_token_count=8000, candidates_token_count=900, thoughts_token_count=1200, total_token_count=10100,
                prompt_tokens_details=[SimpleNamespace(modality='IMAGE', token_count=7000)]))
        client = fake_client()
        def reply(**kwargs):
            self.clock.sleep(85)
            return response
        client.models.generate_content.side_effect = reply
        with patch.dict(os.environ, {'MATH_MARKER_DIAGNOSTICS': '1'}), self.assertLogs('math_marker.marking', level='INFO') as logs:
            result, _ = self.run_grade(client)
        record = mr.state_for(client)['last']
        self.assertEqual(record['elapsed_seconds'], 85)
        self.assertEqual(record['attempts'][0]['thoughts_token_count'], 1200)
        self.assertEqual(record['attempts'][0]['prompt_tokens_by_modality'], {'IMAGE': 7000})
        self.assertEqual(record['attempts'][0]['actual_model_version'], 'gemini-3.8-flash')
        for secret in ('private/student', 'private/scheme', self.payload['questions'][0]['working_summary']):
            self.assertNotIn(secret, str(logs.output))
        self.assertNotIn('request_id', json.dumps(result))

    def test_429_retry_after_stops_all_fallbacks_and_is_account_isolated(self):
        client = fake_client([ApiError(429, '130')])
        with self.assertRaises(RuntimeError): self.run_grade(client)
        self.assertEqual(client.models.generate_content.call_count, 1)
        with self.assertRaisesRegex(RuntimeError, 'rate-limited'): self.run_grade(client)
        self.assertEqual(client.models.generate_content.call_count, 1)
        self.assertEqual(mr.state_for(client)['last']['cooldown_remaining_seconds'], 130)
        self.run_grade(fake_client([self.payload]))
        self.clock.sleep(131)
        client.models.generate_content.side_effect = [SimpleNamespace(text=json.dumps(self.payload))]
        self.run_grade(client)

    def test_unavailable_model_skipped_on_next_student_without_wait(self):
        client = fake_client([ApiError(404), self.payload, self.payload])
        self.assertEqual(self.run_grade(client)[1], 'gemini-3.6-flash')
        self.assertEqual(self.run_grade(client)[1], 'gemini-3.6-flash')
        self.assertEqual(client.models.generate_content.call_count, 3)
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(mr.state_for(client)['last']['cooldown_skips'][0]['remaining_seconds'], 86400)

    def test_timeout_auth_and_truncated_response_never_repeat_costly_generation(self):
        for response in (TimeoutError('private key'), ApiError(401), ApiError(403), ApiError(400),
                         SimpleNamespace(text=json.dumps(self.payload), candidates=[SimpleNamespace(finish_reason='MAX_TOKENS')]),
                         SimpleNamespace(text=None, prompt_feedback=SimpleNamespace(block_reason='SAFETY'))):
            with self.subTest(response=type(response).__name__):
                client = fake_client([response, self.payload])
                with self.assertRaises(RuntimeError) as error: self.run_grade(client)
                self.assertNotIn('private key', str(error.exception))
                self.assertEqual(client.models.generate_content.call_count, 1)

    def test_total_budget_limits_second_timeout_and_attempts(self):
        client = fake_client()
        def unavailable(**kwargs):
            self.clock.sleep(90)
            raise ApiError(503)
        client.models.generate_content.side_effect = unavailable
        with self.assertRaises(RuntimeError): self.run_grade(client)
        calls = client.models.generate_content.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].kwargs['config']['http_options']['timeout'], 90000)

    def test_invalid_json_and_empty_results_have_only_one_fallback(self):
        for bad in (SimpleNamespace(text='not json'), {'questions': []}):
            client = fake_client([bad, bad, self.payload])
            with self.assertRaises(RuntimeError): self.run_grade(client)
            self.assertEqual(client.models.generate_content.call_count, 2)

    def test_no_technical_details_in_progress_or_default_logs(self):
        client = fake_client([ApiError(404), self.payload])
        progress = Mock()
        with patch.dict(os.environ, {'MATH_MARKER_DIAGNOSTICS': ''}), patch.object(mr.logging, 'getLogger') as logger:
            self.run_grade(client, status_callback=progress)
        logger.assert_not_called()
        self.assertNotIn('gemini', str(progress.call_args_list))
        self.assertNotIn('404', str(progress.call_args_list))

    def test_multiple_student_files_are_one_request(self):
        client = fake_client([self.payload])
        originals = [remote(name='part1'), remote(name='part2')]
        self.f['grade_with_model'](client, ['gemini-3.8-flash'], originals, remote(name='scheme'))
        self.assertEqual(client.models.generate_content.call_count, 1)
        self.assertEqual(client.models.generate_content.call_args.kwargs['contents'][:2], originals)

    def test_parallel_callers_are_serialized_per_client(self):
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor
        entered = threading.Event()
        release = threading.Event()
        active = [0, 0]
        lock = threading.Lock()
        client = fake_client()
        def reply(**kwargs):
            with lock:
                active[0] += 1
                active[1] = max(active)
            entered.set()
            release.wait(2)
            with lock:
                active[0] -= 1
            return SimpleNamespace(text=json.dumps(self.payload))
        client.models.generate_content.side_effect = reply
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.run_grade, client)
            self.assertTrue(entered.wait(2))
            second = pool.submit(self.run_grade, client)
            release.set()
            first.result(); second.result()
        self.assertEqual(active[1], 1)

    def test_benchmark_request_and_context_budgets(self):
        from benchmark_marking_requests import benchmark
        measured = benchmark()
        old, new = measured['cases']['success'].values()
        self.assertLess(new['prompt_characters'], old['prompt_characters'] * .6)
        self.assertEqual(new['system_characters'], old['system_characters'])
        self.assertEqual(new['schema_characters'], old['schema_characters'])
        self.assertEqual(new['response_characters'], old['response_characters'])
        self.assertEqual(new['generation_calls'], 1)
        self.assertEqual(measured['cases']['timeout']['after']['generation_calls'], 1)
