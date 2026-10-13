import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from support import fake_client, load_app
from pipeline_fakes import ApiError, Clock, model_unavailable
import test_review_safety as safety
import marking_requests as mr


class FailureDiagnosticsTests(unittest.TestCase):
    def run_failure(self, responses, models=None):
        client = fake_client(responses)
        with self.assertRaises(mr.MarkingFailure) as caught:
            mr.generate(client, models or ['gemini-test-a', 'gemini-test-b'], [], '', {},
                        lambda **kw: kw, __import__('json').loads, clock=Clock())
        return caught.exception, mr.state_for(client)['last']

    def test_failure_categories_and_stop_reasons(self):
        for responses, category, stop in [
            ([ApiError(429)], 'quota_limited', 'quota_limited'),
            ([TimeoutError('SECRET')], 'timeout', 'timeout'),
            ([ApiError(400)], 'request_rejected', 'request_rejected'),
            ([ApiError(404)], 'request_rejected', 'request_rejected'),
            ([ApiError(503), ApiError(503)], 'service_unavailable', 'attempt_limit'),
            ([SimpleNamespace(text='SECRET')] * 2, 'invalid_response', 'attempt_limit'),
            ([SimpleNamespace(text='')] * 2, 'empty_response', 'attempt_limit'),
            ([RuntimeError('SECRET')], 'transport_or_client_error', 'transport_or_client_error'),
        ]:
            with self.subTest(category=category):
                error, record = self.run_failure(responses)
                self.assertEqual(error.category, category)
                self.assertEqual(record['stop_reason'], stop)
                self.assertEqual(record['outcome'], 'failed')
                self.assertIn(record['request_id'], str(error))
                self.assertNotIn('SECRET', str(error))
                self.assertNotIn('SECRET', str(record))

    def test_no_models_and_request_cap_are_explicit(self):
        names = ['gemini-test-' + str(i) for i in range(5)]
        error, record = self.run_failure([model_unavailable(n) for n in names], names)
        self.assertEqual(record['stop_reason'], 'request_limit')
        self.assertEqual(error.category, 'model_unavailable')

    def test_zero_attempt_failures_and_deadline(self):
        for models, setup, category, stop in [
            ([], {}, 'no_models', 'models_exhausted'),
            (['gemini-test'], {'blocked': {'gemini-test': 90}}, 'cooldown', 'models_exhausted'),
            (['gemini-test'], {'account_until': 90}, 'quota_limited', 'account_cooldown'),
        ]:
            client = fake_client()
            mr.state_for(client).update(setup)
            with self.assertRaises(mr.MarkingFailure) as caught:
                mr.generate(client, models, [], '', {}, lambda **kw: kw,
                            __import__('json').loads, clock=Clock())
            self.assertEqual(caught.exception.category, category)
            self.assertEqual(mr.state_for(client)['last']['stop_reason'], stop)
            client.models.generate_content.assert_not_called()
        client, clock = fake_client(), Clock()
        def late(**kw):
            clock.sleep(181)
            return SimpleNamespace(text='{"questions":[{}]}')
        client.models.generate_content.side_effect = late
        with self.assertRaises(mr.MarkingFailure) as caught:
            mr.generate(client, ['gemini-test'], [], '', {}, lambda **kw: kw,
                        __import__('json').loads, clock=clock)
        self.assertEqual(caught.exception.category, 'deadline_exceeded')

    def test_blocked_and_truncated_responses_do_not_fallback(self):
        for reason, category in [('SAFETY', 'blocked_response'), ('MAX_TOKENS', 'truncated_response')]:
            error, record = self.run_failure([SimpleNamespace(text='SECRET',
                candidates=[SimpleNamespace(finish_reason=reason)])])
            self.assertEqual(error.category, category)
            self.assertEqual(len(record['attempts']), 1)
            self.assertNotIn('SECRET', str(record))

    def test_diagnostics_failure_cannot_discard_success(self):
        client = fake_client([{'questions': [{}]}])
        with patch.object(mr, 'emit', side_effect=OSError('SECRET')):
            result, _ = mr.generate(client, ['gemini-test'], [], '', {}, lambda **kw: kw,
                                    __import__('json').loads, clock=Clock())
        self.assertEqual(result, {'questions': [{}]})


class ResultSnapshotTests(unittest.TestCase):
    setUp = safety.SafetyRegressionTests.setUp
    row = safety.SafetyRegressionTests.row

    def test_snapshot_preserves_marks_and_override_without_aliasing(self):
        row = self.f['new_review_row'](self.row(), 'manual')
        key = row['review_key']
        self.ui.session_state['teacher_overrides'] = {key: {'1': 2}}
        self.f['preserve_result_snapshot']([row], 'manual')
        snapshot = copy.deepcopy(self.ui.session_state['previous_result_snapshots']['manual'])
        self.assertEqual(snapshot[0]['totals']['earned'], 2)
        row['result']['questions'].clear()
        self.f['preserve_result_snapshot']([{'status': 'Failed'}], 'manual')
        self.assertEqual(self.ui.session_state['previous_result_snapshots']['manual'], snapshot)

    def test_classroom_failed_remark_archives_success_and_revokes_approval(self):
        row = self.f['new_review_row'](self.row(), 'classroom')
        row.update(submission_id='s', student='Synthetic', is_current=True)
        key = row['review_key']
        self.ui.session_state['classroom_grade_approvals'] = {key: True}
        failed = {'submission_id': 's', 'status': 'Failed', 'result': None}
        current = self.f['merge_classroom_results']({'processed': [row]},
                    {'processed': [failed], 'active_submission_ids': ['s']})
        self.assertEqual(current['processed'][0]['status'], 'Failed')
        self.assertNotIn(key, self.ui.session_state['classroom_grade_approvals'])
        saved = self.ui.session_state['previous_result_snapshots']['classroom'][0]
        self.assertEqual(saved['totals']['earned'], 1)
        self.assertFalse(saved['is_current'])

    def test_manual_store_archives_before_replacing_results(self):
        import ast
        from support import SOURCE
        row = self.f['new_review_row'](self.row(), 'manual')
        self.ui.session_state['last_batch_results'] = [row]
        self.f['batch_results'] = [{'status': 'Failed', 'result': None}]
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        for parent in ast.walk(tree):
            body = getattr(parent, 'body', None)
            if not isinstance(body, list):
                continue
            for i, node in enumerate(body):
                if isinstance(node, ast.Assign) and any(
                    ast.unparse(t) == "st.session_state['last_batch_results']" for t in node.targets):
                    exec(compile(ast.Module(body=body[i-1:i+1], type_ignores=[]), str(SOURCE), 'exec'), self.f)
        self.assertEqual(self.ui.session_state['last_batch_results'][0]['status'], 'Failed')
        self.assertEqual(self.ui.session_state['previous_result_snapshots']['manual'][0]['totals']['earned'], 1)
