"""Exercise actual marking orchestration, privacy and request budgets offline."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from support import load_app, fake_client, benchmark, model_question, remote
from pipeline_fakes import Clock, ApiError, model_unavailable, quota_failure
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
        client = fake_client([model_unavailable('gemini-3.8-flash'), self.payload, self.payload])
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
        self.assertEqual(calls[1].kwargs['config']['http_options']['timeout'], 85000)

    def test_invalid_json_and_empty_results_have_only_one_fallback(self):
        for bad in (SimpleNamespace(text='not json'), {'questions': []}):
            client = fake_client([bad, bad, self.payload])
            with self.assertRaises(RuntimeError): self.run_grade(client)
            self.assertEqual(client.models.generate_content.call_count, 2)

    def test_no_technical_details_in_progress_or_default_logs(self):
        client = fake_client([model_unavailable('gemini-3.8-flash'), self.payload])
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
        for case, calls in [('model_quota_recovery', 2), ('unavailable_recovery', 3)]:
            self.assertEqual(measured['cases'][case]['after']['outcome'], 'completed')
            self.assertEqual(measured['cases'][case]['after']['generation_calls'], calls)

    def test_model_quota_recovers_and_only_blocks_affected_model(self):
        client = fake_client([quota_failure('gemini-3.8-flash', retry_after='130'), self.payload, self.payload])
        self.assertEqual(self.run_grade(client)[1], 'gemini-3.6-flash')
        state = mr.state_for(client)
        self.assertEqual(state['account_until'], 0)
        self.assertEqual(state['blocked'], {'gemini-3.8-flash': 130})
        self.assertEqual(self.run_grade(client)[1], 'gemini-3.6-flash')
        self.assertEqual(client.models.generate_content.call_count, 3)
        self.run_grade(fake_client([self.payload]))
        self.clock.sleep(131)
        client.models.generate_content.side_effect = [SimpleNamespace(text=json.dumps(self.payload))]
        self.assertEqual(self.run_grade(client)[1], 'gemini-3.8-flash')

    def test_shared_unknown_and_message_only_quota_stop(self):
        for error in (quota_failure('gemini-3.8-flash', 'shared'),
                      quota_failure('gemini-3.8-flash', 'unknown'),
                      RuntimeError('429 PerModel quota for gemini-3.8-flash')):
            client = fake_client([error, self.payload])
            with self.assertRaises(RuntimeError): self.run_grade(client)
            self.assertEqual(client.models.generate_content.call_count, 1)
            self.assertGreater(mr.state_for(client)['account_until'], 0)

    def test_mixed_mismatched_and_malformed_quota_fail_closed(self):
        import copy
        base = quota_failure('gemini-3.8-flash')
        variants = []
        mixed = copy.deepcopy(base.details)
        mixed['error']['details'][0]['violations'].append(
            quota_failure('gemini-3.8-flash', 'shared').details['error']['details'][0]['violations'][0])
        variants.append(mixed)
        variants.extend([{}, {'error': {'details': [None]}}, {'error': {'details': 'bad'}},
                         quota_failure('different-model').details])
        for field in ('quotaDimensions', 'quotaId', 'quotaMetric'):
            malformed = copy.deepcopy(base.details)
            malformed['error']['details'][0]['violations'][0][field] = None
            variants.append(malformed)
        unfamiliar = copy.deepcopy(base.details)
        unfamiliar['error']['details'].append({'@type': 'unknown'})
        variants.append(unfamiliar)
        for details in variants:
            error = ApiError(429); error.details = details
            client = fake_client([error, self.payload])
            with self.assertRaises(RuntimeError): self.run_grade(client)
            self.assertEqual(client.models.generate_content.call_count, 1)

    def test_two_unavailable_models_reach_third_on_every_fresh_client(self):
        for _ in range(3):
            client = fake_client([model_unavailable('gemini-3.8-flash'),
                                  model_unavailable('gemini-3.6-flash'), self.payload])
            self.assertEqual(self.run_grade(client)[1], 'gemini-3.5-flash')
            self.assertEqual(client.models.generate_content.call_count, 3)
            self.assertEqual(mr.state_for(client)['last']['generation_attempt_count'], 1)

    def test_absolute_cap_and_duplicate_models(self):
        for kind in ('404', '429'):
            names = ['gemini-test-' + str(i) for i in range(20)]
            client = fake_client([model_unavailable(n) if kind == '404' else quota_failure(n) for n in names])
            with self.assertRaises(RuntimeError):
                self.f['grade_with_model'](client, names, remote(), remote())
            self.assertEqual(client.models.generate_content.call_count, 4)
        client = fake_client([model_unavailable('gemini-test')])
        with self.assertRaises(RuntimeError):
            self.f['grade_with_model'](client, ['gemini-test'] * 20, remote(), remote())
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_fourth_model_reachable_after_three_fast_rejections(self):
        names = ['gemini-test-' + str(i) for i in range(4)]
        client = fake_client([model_unavailable(names[0]), quota_failure(names[1]),
                              model_unavailable(names[2]), self.payload])
        self.assertEqual(self.f['grade_with_model'](client, names, remote(), remote())[1], names[3])
        self.assertEqual(client.models.generate_content.call_count, 4)

    def test_slow_rejections_consume_generation_and_elapsed_budget(self):
        for duration in (11, 90):
            client = fake_client()
            def reply(**kwargs):
                self.clock.sleep(duration)
                raise model_unavailable(kwargs['model'])
            client.models.generate_content.side_effect = reply
            with self.assertRaises(RuntimeError): self.run_grade(client)
            self.assertEqual(client.models.generate_content.call_count, 2)
            self.assertEqual(mr.state_for(client)['last']['generation_attempt_count'], 2)

    def test_unidentified_or_file_404_does_not_poison_model(self):
        for message in ('files/private-file not found', 'not found',
                        'files/private-file not found while using models/gemini-3.8-flash'):
            error = ApiError(404)
            error.details = {'error': {'message': message}}
            client = fake_client([error, self.payload])
            with self.assertRaises(RuntimeError): self.run_grade(client)
            self.assertEqual(client.models.generate_content.call_count, 1)
            self.assertEqual(mr.state_for(client)['blocked'], {})

    def test_503_recovers_but_504_never_retries(self):
        client = fake_client([ApiError(503), self.payload])
        self.assertEqual(self.run_grade(client)[1], 'gemini-3.6-flash')
        client = fake_client([ApiError(504), self.payload])
        with self.assertRaises(RuntimeError): self.run_grade(client)
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_fast_skip_does_not_extend_two_expensive_attempts(self):
        names = self.f['PRIMARY_MODELS']
        client = fake_client([model_unavailable(names[0]), ApiError(503), ApiError(503), self.payload])
        with self.assertRaises(RuntimeError):
            self.f['grade_with_model'](client, names, remote(), remote())
        self.assertEqual(client.models.generate_content.call_count, 3)

    def test_quota_diagnostics_do_not_record_structured_error_content(self):
        error = quota_failure('gemini-3.8-flash')
        client = fake_client([error, self.payload])
        with patch.dict(os.environ, {'MATH_MARKER_DIAGNOSTICS': '1'}), self.assertLogs('math_marker.marking', level='INFO') as logs:
            self.run_grade(client)
        self.assertNotIn('synthetic private quota response', str(logs.output))
        self.assertNotIn('quotaDimensions', str(logs.output))
        self.assertEqual(mr.state_for(client)['last']['attempts'][0]['quota_scope'], 'model')
