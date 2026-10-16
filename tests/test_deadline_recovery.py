"""Synthetic requests and virtual time; no Gemini calls."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from support import fake_client, load_app, remote, benchmark, model_question
from pipeline_fakes import Clock, ApiError, model_unavailable
import marking_requests as mr
import pipeline_metrics as pm


def deadline_error():
    error = ApiError(504)
    error.response.status_code = 504
    error.details = {'error': {'code': 504, 'status': 'DEADLINE_EXCEEDED', 'message': 'PRIVATE'}}
    return error


class DeadlineRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.client = fake_client()
        self.payload = {'questions': [{'awarded_marks': 1, 'max_marks': 2}]}
        for p in (patch.object(mr, 'configured_attempts', return_value=3),
                  patch.object(mr.random, 'uniform', return_value=0)):
            p.start(); self.addCleanup(p.stop)

    def run_request(self, events):
        events = iter(events)
        def response(**kwargs):
            elapsed, value = next(events)
            self.clock.sleep(elapsed)
            if isinstance(value, Exception): raise value
            return SimpleNamespace(text=json.dumps(value))
        self.client.models.generate_content.side_effect = response
        return mr.generate(self.client, ['gemini-a', 'gemini-b', 'gemini-c'],
                           [remote(), remote(name='scheme')], 'instructions', {},
                           lambda **kw: kw, json.loads, clock=self.clock)

    def test_long_provider_deadline_can_recover_once(self):
        result, model = self.run_request([(120, deadline_error()), (20, self.payload)])
        self.assertEqual(result, self.payload); self.assertEqual(model, 'gemini-b')
        calls = self.client.models.generate_content.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].kwargs['config']['http_options']['timeout'], 45000)
        self.assertEqual(calls[0].kwargs['contents'], calls[1].kwargs['contents'])
        self.assertEqual(self.clock.seconds, 145)
        self.assertNotIn('PRIVATE', json.dumps(mr.state_for(self.client)['last']))

    def test_only_one_post_deadline_request_even_if_quick_rejection(self):
        for error in (deadline_error(), ApiError(503), model_unavailable('gemini-b')):
            self.client = fake_client(); self.clock = Clock()
            with self.assertRaises(mr.MarkingFailure):
                self.run_request([(120, deadline_error()), (1, error), (1, self.payload)])
            self.assertEqual(self.client.models.generate_content.call_count, 2)

    def test_unconfirmed_or_ambiguous_timeouts_never_replay(self):
        for error in (ApiError(504), TimeoutError('PRIVATE')):
            self.client = fake_client()
            with self.assertRaises(mr.MarkingFailure): self.run_request([(120, error), (1, self.payload)])
            self.assertEqual(self.client.models.generate_content.call_count, 1)

    def test_configured_one_attempt_cannot_be_bypassed(self):
        with patch.object(mr, 'configured_attempts', return_value=1):
            with self.assertRaises(mr.MarkingFailure): self.run_request([(120, deadline_error())])
        self.assertEqual(self.client.models.generate_content.call_count, 1)

    def test_no_fallback_without_useful_time_remaining(self):
        with self.assertRaises(mr.MarkingFailure): self.run_request([(166, deadline_error())])
        self.assertEqual(self.client.models.generate_content.call_count, 1)
        self.assertLessEqual(self.clock.seconds, 180)

    def test_success_and_delayed_success_are_unchanged(self):
        for duration in (10, 119):
            self.client = fake_client()
            self.assertEqual(self.run_request([(duration, self.payload)])[0], self.payload)
            self.assertEqual(self.client.models.generate_content.call_count, 1)

    def test_second_deadline_stops_within_budget(self):
        with self.assertRaises(mr.MarkingFailure):
            self.run_request([(120, deadline_error()), (45, deadline_error())])
        self.assertEqual(self.clock.seconds, 170)
        self.assertEqual(self.client.models.generate_content.call_count, 2)

    def test_slow_model_skipped_for_next_student_and_fresh_client_isolated(self):
        self.run_request([(120, deadline_error()), (20, self.payload)])
        self.assertEqual(self.run_request([(20, self.payload)])[1], 'gemini-b')
        self.client = fake_client()
        self.assertEqual(self.run_request([(20, self.payload)])[1], 'gemini-a')

    def test_actual_worker_keeps_partial_marks_and_stage_diagnostics(self):
        f = load_app(); f['time'] = self.clock
        payload = {'questions': [model_question(benchmark()['questions'][0], 'partial')]}
        client = fake_client([payload]); progress = []
        with patch.object(mr, 'diagnostics_enabled', return_value=True), patch.object(mr, '_console_logger') as logger:
            row = f['mark_one_submission'](client, 'PRIVATE', remote(), remote(name='scheme'), None, False,
                                           status_callback=progress.append)
        self.assertEqual(row['status'], 'Marked')
        self.assertEqual(row['result']['questions'][0]['awarded_marks'], 1)
        self.assertTrue(any('Checking' in x for x in progress))
        records = [json.loads(c.args[1]) for c in logger.return_value.info.call_args_list
                   if c.args[0].startswith('marking_pipeline_metrics')]
        self.assertIn('validation', records[0]['stages_seconds'])
        self.assertEqual(records[0]['request_id'], mr.state_for(client)['last']['request_id'])
        self.assertNotIn('PRIVATE', json.dumps(records))
        self.assertIsNone(pm.request_id())

    def test_logging_failure_cannot_break_success_or_exception(self):
        with patch.object(mr, 'diagnostics_enabled', return_value=True), patch.object(mr, '_console_logger', side_effect=RuntimeError('PRIVATE')):
            with pm.job('manual_student', self.clock):
                self.assertEqual(pm.call('validation', lambda: 4), 4)
            with self.assertRaises(ValueError):
                with pm.job('manual_student', self.clock): raise ValueError('original')
        self.assertIsNone(pm.request_id())

    def test_classroom_failed_deadlines_save_no_incomplete_marks(self):
        from unittest.mock import Mock
        f = load_app(); f['time'] = self.clock
        f['get_user_profile'] = Mock(return_value={'name': {'fullName': 'PRIVATE'}})
        f['download_drive_file'] = Mock(return_value=('PRIVATE.pdf', b'%PDF synthetic', 'application/pdf'))
        f['upload_bytes_to_gemini'] = Mock(return_value=remote())
        client = fake_client([deadline_error(), deadline_error()])
        submission = {'id':'a', 'userId':'a', 'assignmentSubmission': {'attachments':[{'driveFile':{'id':'a'}}]}}
        with patch.object(mr, 'diagnostics_enabled', return_value=True), patch.object(mr, '_console_logger') as logger:
            row = f['mark_one_classroom_submission']('PRIVATE', client, submission, {'maxPoints':100}, remote(), False)
        self.assertEqual(row['status'], 'Failed')
        self.assertIsNone(row['result']); self.assertIsNone(row['suggested_classroom_grade'])
        self.assertEqual(client.models.generate_content.call_count, 2)
        f['requests'].request.assert_not_called()
        records = [json.loads(c.args[1]) for c in logger.return_value.info.call_args_list
                   if c.args[0].startswith('marking_pipeline_metrics')]
        self.assertEqual(records[0]['outcome'], 'failed')
        self.assertNotIn('PRIVATE', json.dumps(records))

    def test_upload_and_processing_timings_preserve_original_bytes(self):
        import document_pipeline
        from pipeline_fakes import Provider
        provider = Provider(self.clock, transfer=2, processing=3)
        with patch.object(mr, 'diagnostics_enabled', return_value=True), patch.object(mr, '_console_logger') as logger:
            document_pipeline.transfer(provider, b'%PDF synthetic original', 'PRIVATE.pdf', clock=self.clock)
        record = json.loads(logger.return_value.info.call_args.args[1])
        self.assertEqual(record['stages_seconds']['upload'], 2)
        self.assertGreaterEqual(record['stages_seconds']['file_processing'], 3)
        self.assertEqual(list(provider.data.values()), [b'%PDF synthetic original'])
        self.assertNotIn('PRIVATE', json.dumps(record))

    def test_mismatched_deadline_evidence_is_not_enough(self):
        for field in ('response', 'code', 'status'):
            error = deadline_error()
            if field == 'response': error.response.status_code = 502
            else: error.details['error'][field] = 'unexpected'
            self.assertFalse(mr.provider_deadline(error))

    def test_api_and_parsing_durations_are_separate(self):
        self.client.models.generate_content.side_effect = None
        self.client.models.generate_content.return_value = SimpleNamespace(text=json.dumps(self.payload))
        def parse(text):
            self.clock.sleep(2)
            return json.loads(text)
        mr.generate(self.client, ['gemini-a'], [], '', {}, lambda **kw:kw, parse, clock=self.clock)
        attempt = mr.state_for(self.client)['last']['attempts'][0]
        self.assertEqual(attempt['api_response_seconds'], 0)
        self.assertEqual(attempt['parse_seconds'], 2)
