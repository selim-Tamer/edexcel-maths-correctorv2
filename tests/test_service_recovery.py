"""Actual controller with virtual provider time; no Gemini traffic."""
import json
import unittest
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch
from support import load_app, fake_client, remote, benchmark, model_question
from pipeline_fakes import Clock, ApiError
import marking_requests as mr


class ServiceRecoveryTests(unittest.TestCase):
    def setUp(self):
        # Regression coverage for operators retaining the PR13 attempt limit.
        limit = patch.object(mr, 'configured_attempts', return_value=2)
        limit.start(); self.addCleanup(limit.stop)
        jitter = patch.object(mr.random, 'uniform', return_value=0)
        jitter.start(); self.addCleanup(jitter.stop)
        self.f = load_app()
        self.clock = Clock()
        self.f['time'] = self.clock
        self.payload = {'questions': [{'awarded_marks': 1, 'max_marks': 2}]}

    def grade(self, client, models=None, progress=None):
        return self.f['grade_with_model'](client, models or self.f['PRIMARY_MODELS'],
            remote(), remote(name='scheme'), status_callback=progress)

    def test_503_recovery_waits_and_preserves_attachments(self):
        client = fake_client([ApiError(503), self.payload])
        progress = Mock()
        self.assertEqual(self.grade(client, progress=progress)[0], self.payload)
        self.assertEqual(self.clock.sleeps, [5])
        first, second = client.models.generate_content.call_args_list
        self.assertEqual(first.kwargs['contents'], second.kwargs['contents'])
        self.assertEqual(first.kwargs['config'], second.kwargs['config'])
        record = mr.state_for(client)['last']
        self.assertEqual(record['attempts'][0]['retry_decision'], 'delayed_fallback')
        self.assertEqual(record['sleep_seconds'], 5)
        self.assertNotIn('503', str(progress.call_args_list))

    def test_two_503s_stop_without_third_request_or_final_sleep(self):
        client = fake_client([ApiError(503), ApiError(503), self.payload])
        with self.assertRaises(mr.MarkingFailure): self.grade(client)
        record = mr.state_for(client)['last']
        self.assertEqual(record['attempt_count'], 2)
        self.assertEqual(record['stop_reason'], 'attempt_limit')
        self.assertEqual(self.clock.sleeps, [5])
        # Next student waits for the service cooldown, then can use the third model.
        self.assertEqual(self.grade(client)[0], self.payload)
        self.assertEqual(client.models.generate_content.call_count, 3)

    def test_retry_after_seconds_and_http_date(self):
        for header in ('12', format_datetime(datetime.fromtimestamp(self.clock.time()+12, timezone.utc))):
            self.clock.seconds = 0
            client = fake_client([ApiError(503, header), self.payload])
            start = self.clock.monotonic()
            self.grade(client)
            self.assertEqual(self.clock.monotonic()-start, 12)
        client = fake_client([ApiError(503, '30'), self.payload])
        self.grade(client)
        self.assertEqual(self.clock.sleeps[-1], 30)

    def test_long_retry_after_stops_without_early_fallback(self):
        client = fake_client([ApiError(503, '120'), self.payload])
        with self.assertRaises(mr.MarkingFailure): self.grade(client)
        self.assertEqual(client.models.generate_content.call_count, 1)
        self.assertEqual(self.clock.sleeps, [])
        record = mr.state_for(client)['last']
        self.assertEqual(record['stop_reason'], 'retry_wait_limit')
        self.assertEqual(record['attempts'][0]['retry_decision'], 'deferred_to_manual_retry')
        with self.assertRaises(mr.MarkingFailure): self.grade(client)
        self.assertEqual(client.models.generate_content.call_count, 1)
        self.grade(fake_client([self.payload]))  # state never crosses clients
        self.clock.sleep(121)
        self.grade(client)

    def test_malformed_headers_are_finite_and_content_free(self):
        for header in ('private-secret', 'nan', 'inf', '-1', '0'):
            client = fake_client([ApiError(503, header), self.payload])
            self.grade(client)
            self.assertEqual(self.clock.sleeps[-1], 5)
            self.assertNotIn('private-secret', json.dumps(mr.state_for(client)['last']))

    def test_no_wait_without_eligible_fallback(self):
        client = fake_client([ApiError(503)])
        with self.assertRaises(mr.MarkingFailure): self.grade(client, ['gemini-test'])
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_other_http_failures_keep_previous_policy(self):
        for status in (400, 401, 403, 404, 429, 500, 502, 504):
            self.clock.sleeps.clear()
            client = fake_client([ApiError(status), self.payload])
            if status in (500, 502):
                self.grade(client)
                self.assertEqual(client.models.generate_content.call_count, 2)
            else:
                with self.assertRaises(mr.MarkingFailure): self.grade(client)
                self.assertEqual(client.models.generate_content.call_count, 1)
            self.assertEqual(self.clock.sleeps, [5] if status in (500, 502) else [])

    def test_wait_cannot_use_remaining_deadline(self):
        client = fake_client()
        def reply(**kw):
            self.clock.sleep(178)
            raise ApiError(503)
        client.models.generate_content.side_effect = reply
        with self.assertRaises(mr.MarkingFailure): self.grade(client)
        self.assertEqual(client.models.generate_content.call_count, 1)
        self.assertEqual(self.clock.sleeps, [178])

    def test_oversleep_does_not_launch_request_after_deadline(self):
        client = fake_client([ApiError(503), self.payload])
        self.clock.sleep = lambda seconds: setattr(self.clock, 'seconds', 181)
        with self.assertRaises(mr.MarkingFailure): self.grade(client)
        self.assertEqual(client.models.generate_content.call_count, 1)

    def test_model_configuration_has_no_redirected_alias(self):
        for key in ('PRIMARY_MODELS', 'AUDIT_MODELS'):
            self.assertEqual(self.f[key], ['gemini-3.8-flash', 'gemini-3.6-flash', 'gemini-3.5-flash-lite'])

    def test_benchmark_recovery_is_bounded_and_success_cost_unchanged(self):
        from benchmark_service_recovery import benchmark
        cases = benchmark()['cases']
        self.assertEqual(cases['success']['before'], cases['success']['after'])
        for name in ('recovers_after_5s', 'retry_after_12s'):
            self.assertEqual(cases[name]['before']['outcome'], 'failed_without_marks')
            self.assertEqual(cases[name]['after']['outcome'], 'completed')
        for case in cases.values():
            self.assertLessEqual(case['after']['requests'], case['before']['requests'])
            self.assertLessEqual(case['after']['sleep_seconds'], 30)

    def test_fresh_clients_still_obey_limits(self):
        for _ in range(3):
            client = fake_client([ApiError(503), ApiError(503), self.payload])
            with self.assertRaises(mr.MarkingFailure): self.grade(client)
            self.assertEqual(client.models.generate_content.call_count, 2)

    def test_recovery_runs_actual_marking_validation_for_all_credit_levels(self):
        case = benchmark()['questions'][0]
        for variant in ('correct', 'incorrect', 'partial'):
            payload = {'questions': [model_question(case, variant)]}
            args = ('Synthetic', remote(), remote(name='scheme'), None, False)
            direct = self.f['mark_one_submission'](fake_client([payload]), *args)
            recovered = self.f['mark_one_submission'](fake_client([ApiError(503), payload]), *args)
            self.assertEqual(recovered['result'], direct['result'])
            self.assertEqual(recovered['totals'], direct['totals'])

