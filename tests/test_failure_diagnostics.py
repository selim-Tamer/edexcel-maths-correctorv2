import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from support import fake_client, load_app
from pipeline_fakes import ApiError, Clock, model_unavailable
import test_review_safety as safety
import marking_requests as mr


class FailureDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        limit = patch.object(mr, 'configured_attempts', return_value=2)
        limit.start(); self.addCleanup(limit.stop)

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
        self.f['google_logged_in'] = lambda: True
        self.ui.user = SimpleNamespace(sub='teacher-a')
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
        # The hardened store path binds rows to the owner captured at run
        # start; the harness provides the same owner the run would capture.
        self.f['store_owner'] = self.f['result_owner']()
        tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
        matched = False
        for parent in ast.walk(tree):
            for attribute in ('body', 'orelse', 'finalbody'):
                body = getattr(parent, attribute, None)
                if not isinstance(body, list):
                    continue
                for i, node in enumerate(body):
                    if isinstance(node, ast.Assign) and any(
                        ast.unparse(t) == "st.session_state['last_batch_results']" for t in node.targets):
                        exec(compile(ast.Module(body=body[i-1:i+1], type_ignores=[]), str(SOURCE), 'exec'), self.f)
                        matched = True
        self.assertTrue(matched, 'manual store block not found')
        self.assertEqual(self.ui.session_state['last_batch_results'][0]['status'], 'Failed')
        self.assertEqual(self.ui.session_state['previous_result_snapshots']['manual'][0]['totals']['earned'], 1)

    def test_partial_batches_do_not_erase_other_students(self):
        a = self.f['new_review_row'](self.row(), 'manual')
        b = self.f['new_review_row'](self.row(), 'manual')
        self.f['preserve_result_snapshot']([a, b], 'manual', 'scheme-a', 'Assignment A')
        newer = self.f['new_review_row'](self.row(), 'manual')
        self.f['preserve_result_snapshot']([newer, {'status': 'Failed'}], 'manual', 'scheme-b', 'Assignment B')
        self.f['preserve_result_snapshot']([a], 'manual', 'scheme-a')
        saved = self.ui.session_state['previous_result_snapshots']['manual']
        self.assertEqual(len(saved), 3)
        self.assertEqual({r['snapshot_context'] for r in saved}, {'scheme-a', 'scheme-b'})
        self.assertTrue(all(r['status'] == 'Historical' and not r['is_current'] for r in saved))
        self.assertEqual(self.f['prepare_review_row'](saved[0]), saved[0])

    def test_classroom_owner_gate_and_download_redaction(self):
        self.f['google_logged_in'] = lambda: True
        self.ui.user = SimpleNamespace(sub='teacher-a')
        row = self.f['new_review_row'](self.row(), 'classroom')
        row['suggested_classroom_grade'] = 50
        self.ui.session_state['teacher_overrides'] = {row['review_key']: {'1': 2}}
        self.f['preserve_result_snapshot']([row], 'classroom', 'assignment-a', 'Assignment A')
        self.f['render_result_snapshots']()
        data = self.ui.downloads[-1][1]['data']
        self.assertNotIn('teacher-a', data)
        self.assertNotIn('suggested_classroom_grade', data)
        self.assertEqual(__import__('json').loads(data)[0]['totals']['earned'], 2)
        self.ui.downloads.clear()
        self.ui.user = SimpleNamespace(sub='teacher-b')
        self.f['render_result_snapshots']()
        self.assertEqual(self.ui.downloads, [])
        self.f['preserve_result_snapshot']([row], 'classroom', 'other')
        self.assertEqual(len(self.ui.session_state['previous_result_snapshots']['classroom']), 1)
        self.f['google_logged_in'] = lambda: False
        self.f['render_result_snapshots']()
        self.assertEqual(self.ui.downloads, [])

    def test_anonymous_session_isolation_and_unowned_rows_fail_closed(self):
        row = self.f['new_review_row'](self.row(), 'manual')
        self.f['preserve_result_snapshot']([row], 'manual')
        self.ui.session_state.pop('manual_report_owner')
        self.f['render_result_snapshots']()
        self.assertEqual(self.ui.downloads, [])
        self.ui.session_state = {}
        self.f['preserve_result_snapshot']([self.row()], 'manual')
        self.assertEqual(self.ui.session_state.get('previous_result_snapshots'), {})

    def test_history_bound_is_explicit(self):
        rows = [self.f['new_review_row'](self.row(), 'manual') for _ in range(201)]
        self.f['preserve_result_snapshot'](rows, 'manual')
        saved = self.ui.session_state['previous_result_snapshots']['manual']
        self.assertEqual(len(saved), 200)
        self.assertTrue(self.ui.session_state['snapshot_history_trimmed'])
        self.assertNotIn(rows[0]['review_key'], [r['review_key'] for r in saved])

    def test_preparation_failure_revokes_old_revision_before_marking(self):
        from unittest.mock import Mock
        self.f['google_logged_in'] = lambda: True
        self.ui.user = SimpleNamespace(sub='teacher-a')
        row = self.f['new_review_row'](self.row(), 'classroom')
        row.update(submission_id='s', revision='old', is_current=True)
        self.ui.session_state['classroom_grade_approvals'] = {row['review_key']: True}
        self.ui.session_state['classroom_jobs'] = {'ctx': {'last_result': {'processed': [row]}}}
        self.f['list_turned_in_submissions'] = Mock(return_value=[{'id': 's', 'state': 'TURNED_IN'}])
        self.f['classroom_submission_revision'] = Mock(return_value='new')
        self.f['upload_pdf'] = Mock(side_effect=RuntimeError('synthetic preparation failure'))
        with self.assertRaises(RuntimeError):
            self.f['run_classroom_marking_job']('fake', fake_client(), 'course', {'id': 'assignment'},
                                               None, False, {'old'}, document_context='ctx')
        self.assertFalse(row['is_current'])
        self.assertNotIn(row['review_key'], self.ui.session_state['classroom_grade_approvals'])
        saved = self.ui.session_state['previous_result_snapshots']['classroom'][0]
        self.assertEqual(saved['totals']['earned'], 1)
        self.assertEqual(saved['snapshot_context'], 'ctx')


class SupportReferenceTests(unittest.TestCase):
    def test_reference_is_random_and_logging_failure_preserves_category(self):
        import uuid
        refs = []
        for _ in range(2):
            client = fake_client([TimeoutError('PRIVATE')])
            with patch.object(mr, 'emit', side_effect=OSError('PRIVATE')), self.assertRaises(mr.MarkingFailure) as caught:
                mr.generate(client, ['gemini-test'], [], '', {}, lambda **kw: kw,
                            __import__('json').loads, clock=Clock())
            ref = caught.exception.request_id
            self.assertEqual(uuid.UUID(hex=ref).version, 4)
            self.assertEqual(ref, mr.state_for(client)['last']['request_id'])
            self.assertEqual(caught.exception.category, 'timeout')
            self.assertNotIn('PRIVATE', str(caught.exception))
            refs.append(ref)
        self.assertNotEqual(*refs)
