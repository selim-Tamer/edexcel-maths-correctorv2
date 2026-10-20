"""PR14 default policy; actual controller and application paths, virtual time."""
import json
import os
import unittest
from unittest.mock import patch, Mock
from support import load_app, fake_client, remote, benchmark, model_question
from pipeline_fakes import Clock, ApiError, quota_failure, model_unavailable
from test_pdf_processing import synthetic_pdf
import marking_requests as mr


class AutomaticRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = load_app(); self.clock = Clock(); self.f['time'] = self.clock
        self.payload = {'questions': [model_question(benchmark()['questions'][0], 'partial')]}
        for patcher in (patch.object(mr, '_recovery_attempts', None),
                        patch.dict(os.environ, {'MATH_MARKER_MAX_ATTEMPTS': '3'}),
                        patch.object(mr.random, 'uniform', return_value=0.5)):
            patcher.start(); self.addCleanup(patcher.stop)

    def grade(self, client, models=None):
        return self.f['grade_with_model'](client, models or self.f['PRIMARY_MODELS'], remote(), remote(name='scheme'))

    def test_two_503s_reach_third_model_once_with_exponential_jitter(self):
        client = fake_client([ApiError(503), ApiError(503), self.payload])
        result, model = self.grade(client)
        self.assertEqual(result, self.payload)
        self.assertEqual(model, 'gemini-3.5-flash-lite')
        self.assertEqual(self.clock.sleeps, [5.5, 10.5])
        record = mr.state_for(client)['last']
        self.assertEqual(record['attempt_count'], 3)
        self.assertEqual(record['retry_count'], 2)
        calls = client.models.generate_content.call_args_list
        self.assertEqual(len({c.kwargs['model'] for c in calls}), 3)
        self.assertTrue(all(c.kwargs['contents'] == calls[0].kwargs['contents'] for c in calls))
        client.files.upload.assert_not_called()

    def test_all_models_fail_then_next_student_can_recover(self):
        client = fake_client([ApiError(503)]*3 + [self.payload])
        with self.assertRaises(mr.MarkingFailure): self.grade(client)
        self.assertEqual(client.models.generate_content.call_count, 3)
        failed = mr.state_for(client)['last']
        self.assertEqual(failed['failure_category'], 'service_unavailable')
        self.assertEqual(failed['stop_reason'], 'attempt_limit')
        self.assertEqual(self.grade(client)[0], self.payload)
        self.assertEqual(client.models.generate_content.call_count, 4)
        self.assertLessEqual(mr.state_for(client)['last']['sleep_seconds'], 30)

    def test_shared_unknown_quota_stops_but_model_quota_can_fallback(self):
        for scope in ('model', 'shared', 'unknown'):
            error = quota_failure('gemini-3.8-flash', scope, retry_after='120')
            client = fake_client([error, self.payload])
            if scope == 'model':
                self.grade(client)
                self.assertEqual(client.models.generate_content.call_count, 2)
            else:
                with self.assertRaises(mr.MarkingFailure): self.grade(client)
                with self.assertRaises(mr.MarkingFailure): self.grade(client)
                self.assertEqual(client.models.generate_content.call_count, 1)
            self.assertEqual(self.clock.sleeps, [])

    def test_connection_refused_can_recover_but_ambiguous_timeout_stops(self):
        client = fake_client([ConnectionRefusedError('PRIVATE'), self.payload])
        self.assertEqual(self.grade(client)[0], self.payload)
        self.assertEqual(mr.state_for(client)['last']['attempts'][0]['outcome'], 'connection_unavailable')
        for error in (TimeoutError('PRIVATE'), ApiError(504), ApiError(408)):
            client = fake_client([error, self.payload])
            with self.assertRaises(mr.MarkingFailure): self.grade(client)
            self.assertEqual(client.models.generate_content.call_count, 1)

    def test_nonretryable_and_unusable_output_do_not_get_extra_attempts(self):
        for error in (ApiError(400), ApiError(401), ApiError(403), ApiError(404)):
            client = fake_client([error, self.payload])
            with self.assertRaises(mr.MarkingFailure): self.grade(client)
            self.assertEqual(client.models.generate_content.call_count, 1)
        client = fake_client([{'questions': []}]*3)
        with self.assertRaises(mr.MarkingFailure): self.grade(client)
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_config_limits_and_secrets_without_echoing_values(self):
        for value, expected in [('1',1), (2,2), ('3',3), ('100',3), ('PRIVATE',3), (True,3)]:
            self.assertEqual(mr.configure_recovery({'MATH_MARKER_MAX_ATTEMPTS': value}), expected)
            client = fake_client([ApiError(503)]*4)
            with self.assertRaises(mr.MarkingFailure): self.grade(client)
            self.assertEqual(client.models.generate_content.call_count, expected)
            self.assertNotIn('PRIVATE', json.dumps(mr.state_for(client)['last']))

    def test_hard_request_limit_and_per_model_limit(self):
        names = ['gemini-test-'+str(i) for i in range(10)]
        client = fake_client([model_unavailable(n) for n in names])
        with self.assertRaises(mr.MarkingFailure): self.grade(client, names)
        self.assertEqual(client.models.generate_content.call_count, 4)
        client = fake_client([ApiError(503)]*10)
        with self.assertRaises(mr.MarkingFailure): self.grade(client, ['gemini-test']*10)
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_retry_after_total_wait_cap_and_elapsed_budget(self):
        client = fake_client([ApiError(503, '20'), ApiError(503, '20'), self.payload])
        with self.assertRaises(mr.MarkingFailure): self.grade(client)
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertEqual(self.clock.sleeps, [20])
        self.clock.seconds = 0; self.clock.sleeps.clear()
        client = fake_client()
        def fail(**kw):
            self.clock.sleep(90)
            raise ApiError(503)
        client.models.generate_content.side_effect = fail
        with self.assertRaises(mr.MarkingFailure): self.grade(client)
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertEqual(client.models.generate_content.call_args.kwargs['config']['http_options']['timeout'], 84500)

    def test_temporary_state_does_not_cross_clients_or_erase_quota(self):
        client = fake_client([ApiError(503, '120')])
        with self.assertRaises(mr.MarkingFailure): self.grade(client)
        self.grade(fake_client([self.payload]))
        with self.assertRaises(mr.MarkingFailure): self.grade(client)
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_actual_classroom_worker_failure_then_success_no_partial_result(self):
        self.f['get_user_profile'] = Mock(return_value={'name': {'fullName': 'Synthetic'}})
        self.f['download_drive_file'] = Mock(return_value=('synthetic.pdf', synthetic_pdf(), 'application/pdf'))
        self.f['upload_bytes_to_gemini'] = Mock(return_value=remote())
        client = fake_client([ApiError(503)]*3 + [self.payload])
        def mark(n):
            submission = {'id': n, 'userId': n, 'assignmentSubmission': {'attachments': [{'driveFile': {'id': n}}]}}
            return self.f['mark_one_classroom_submission']('synthetic', client, submission,
                {'maxPoints':100}, remote(name='scheme'), False, scheme_maxima={'1':2}, scheme_answers={'1':'4'})
        failed = mark('a'); success = mark('b')
        self.assertEqual(failed['status'], 'Failed')
        self.assertIsNone(failed['result']); self.assertIsNone(failed['suggested_classroom_grade'])
        self.assertEqual(success['status'], 'Marked')
        self.assertEqual(success['totals']['earned'], 1)
        self.assertEqual(client.models.generate_content.call_count, 4)
        self.f['requests'].request.assert_not_called()

    def test_reproducible_benchmark_reaches_third_without_unbounded_requests(self):
        from benchmark_automatic_recovery import benchmark
        data = benchmark()['cases']
        self.assertEqual(data['success']['before'], data['success']['after'])
        self.assertEqual(data['third_recovers']['after']['outcomes'], ['completed'])
        self.assertEqual(data['third_recovers']['before']['outcomes'], ['failed_without_marks'])
        self.assertEqual(data['all_503']['after']['requests'], 3)
        self.assertEqual(data['next_student']['after']['outcomes'], ['failed_without_marks', 'completed'])

    def test_actual_batch_continues_and_retry_skips_completed_revisions(self):
        from test_review_safety import RecordingUI
        ui = RecordingUI(); self.f['st'] = ui
        self.f['result_owner'] = lambda: 'google:synthetic'
        submissions = [{'id': n, 'userId': n, 'state': 'TURNED_IN',
                        'assignmentSubmission': {'attachments': [{'driveFile': {'id': n}}]}}
                       for n in ('a', 'b', 'c')]
        self.f['list_turned_in_submissions'] = Mock(return_value=submissions)
        self.f['classroom_submission_revision'] = lambda token, submission: submission['id']+'-v1'
        self.f['get_user_profile'] = Mock(return_value={'name': {'fullName': 'Synthetic'}})
        self.f['download_drive_file'] = Mock(return_value=('synthetic.pdf', synthetic_pdf(), 'application/pdf'))
        self.f['upload_bytes_to_gemini'] = Mock(return_value=remote())
        self.f['upload_pdf'] = Mock(return_value=remote(name='scheme'))
        self.f['prepare_reference_metadata'] = Mock(return_value=(({'1':2}, {'1':'4'}, None), ['1']))
        client = fake_client([self.payload] + [ApiError(503)]*3 + [self.payload])
        processed = set()
        def run(c):
            return self.f['run_classroom_marking_job']('synthetic', c, 'course',
                {'id':'assignment', 'maxPoints':100}, object(), False, processed)
        first = run(client)
        rows = {r['submission_id']: r for r in first['processed']}
        self.assertEqual({n:r['status'] for n,r in rows.items()}, {'a':'Marked','b':'Failed','c':'Marked'})
        self.assertEqual(processed, {'a-v1','c-v1'})
        self.assertIsNone(rows['b']['result'])
        ui.session_state['teacher_overrides'] = {rows['a']['review_key']: {'1':2}}
        retry_client = fake_client([self.payload])
        retried = run(retry_client)
        self.assertEqual([r['submission_id'] for r in retried['processed']], ['b'])
        merged = self.f['merge_classroom_results'](first, retried)
        self.assertEqual(len(merged['processed']), 3)
        self.assertEqual(processed, {'a-v1','b-v1','c-v1'})
        self.assertEqual(ui.session_state['teacher_overrides'][rows['a']['review_key']], {'1':2})
        self.assertEqual(retry_client.models.generate_content.call_count, 1)
        self.f['requests'].request.assert_not_called()

