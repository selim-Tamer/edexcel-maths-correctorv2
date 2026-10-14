"""Test real console output, not assertLogs (which supplies its own handler)."""
import io
import json
import logging
import os
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch
from support import fake_client
from pipeline_fakes import ApiError, Clock
import marking_requests as mr


class ConsoleDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger('math_marker.marking')
        self.before = (self.logger.handlers[:], self.logger.level, self.logger.propagate, self.logger.disabled,
                       mr._diagnostics_enabled, mr._startup_enabled)
        self.logger.handlers = []
        mr._diagnostics_enabled = None
        mr._startup_enabled = None
        self.env = patch.dict(os.environ, {'MATH_MARKER_DIAGNOSTICS': ''})
        self.env.start()

    def tearDown(self):
        for handler in self.logger.handlers:
            if handler not in self.before[0]: handler.close()
        (self.logger.handlers, self.logger.level, self.logger.propagate, self.logger.disabled,
         mr._diagnostics_enabled, mr._startup_enabled) = self.before
        self.env.stop()

    def test_startup_resolves_only_switch_and_deduplicates_reruns(self):
        stream = io.StringIO()
        with redirect_stderr(stream):
            for _ in range(3):
                self.assertTrue(mr.configure_diagnostics({'MATH_MARKER_DIAGNOSTICS': '1', 'API_KEY': 'PRIVATE'}))
            self.assertFalse(mr.configure_diagnostics({'MATH_MARKER_DIAGNOSTICS': 'PRIVATE'}))
        self.assertEqual(stream.getvalue().splitlines(), [
            'marking_diagnostics_startup {"enabled": true}',
            'marking_diagnostics_startup {"enabled": false}'])
        self.assertNotIn('PRIVATE', stream.getvalue())
        self.assertEqual(len(self.logger.handlers), 1)

    def test_environment_fallback_and_explicit_secret_disable(self):
        class MissingSecrets:
            def get(self, *args): raise FileNotFoundError('PRIVATE')
        with patch.dict(os.environ, {'MATH_MARKER_DIAGNOSTICS': '1'}), redirect_stderr(io.StringIO()):
            self.assertTrue(mr.configure_diagnostics(MissingSecrets()))
            self.assertFalse(mr.configure_diagnostics({'MATH_MARKER_DIAGNOSTICS': '0'}))

    def test_success_and_failure_emit_to_console_without_root_handler(self):
        root = logging.getLogger()
        with patch.object(root, 'handlers', []), patch.object(root, 'level', logging.WARNING):
            for failed in (False, True):
                stream = io.StringIO()
                client = fake_client([ApiError(503), ApiError(503)] if failed else [{'questions': [{}]}])
                with redirect_stderr(stream):
                    mr.configure_diagnostics({'MATH_MARKER_DIAGNOSTICS': '1'})
                    if failed:
                        with self.assertRaises(mr.MarkingFailure) as error:
                            mr.generate(client, ['gemini-test-a', 'gemini-test-b'], ['PRIVATE'], 'PRIVATE', {},
                                        lambda **kw: kw, json.loads, clock=Clock())
                    else:
                        result, _ = mr.generate(client, ['gemini-test-a'], ['PRIVATE'], 'PRIVATE', {},
                                                lambda **kw: kw, json.loads, clock=Clock())
                        self.assertEqual(result, {'questions': [{}]})
                records = [json.loads(line.split(' ', 1)[1]) for line in stream.getvalue().splitlines()
                           if line.startswith('marking_metrics ')]
                self.assertEqual(len(records), 1)
                record = records[0]
                self.assertNotIn('PRIVATE', stream.getvalue())
                self.assertEqual(record['outcome'], 'failed' if failed else 'success')
                self.assertIn('elapsed_seconds', record)
                if failed:
                    self.assertEqual(record['request_id'], error.exception.request_id)
                    self.assertEqual(record['failure_category'], 'service_unavailable')
                    self.assertEqual(record['stop_reason'], 'models_exhausted')
                    self.assertEqual(record['attempt_count'], 2)
                    self.assertEqual([a['http_status'] for a in record['attempts']], [503, 503])
                    self.assertEqual([a['model'] for a in record['attempts']], ['gemini-test-a', 'gemini-test-b'])
                    self.assertTrue(all('response_seconds' in a for a in record['attempts']))

    def test_disabled_has_startup_only_and_no_propagation_duplicates(self):
        stream = io.StringIO()
        root = logging.getLogger()
        root_handler = logging.StreamHandler(stream)
        with patch.object(root, 'handlers', [root_handler]), redirect_stderr(stream):
            mr.configure_diagnostics({'MATH_MARKER_DIAGNOSTICS': '0'})
            mr.emit({'request_id': 'should-not-appear'})
        self.assertEqual(stream.getvalue(), 'marking_diagnostics_startup {"enabled": false}\n')
        self.assertFalse(self.logger.propagate)

    def test_console_setup_failure_cannot_break_marking_or_startup(self):
        with patch.object(mr, '_console_logger', side_effect=OSError('PRIVATE')):
            mr.configure_diagnostics({'MATH_MARKER_DIAGNOSTICS': '1'})
            result, _ = mr.generate(fake_client([{'questions': [{}]}]), ['gemini-test'], [], '', {},
                                    lambda **kw: kw, json.loads, clock=Clock())
            self.assertEqual(result, {'questions': [{}]})

    def test_app_initializes_before_any_workers(self):
        import ast
        from support import SOURCE
        body = ast.parse(SOURCE.read_text(encoding='utf-8')).body
        calls = [i for i, node in enumerate(body) if isinstance(node, ast.Expr)
                 and isinstance(node.value, ast.Call)
                 and ast.unparse(node.value.func) == 'marking_requests.configure_diagnostics']
        self.assertEqual(len(calls), 1)
        self.assertEqual(ast.unparse(body[calls[0]].value.args[0]), 'st.secrets')
        self.assertLess(calls[0], next(i for i, n in enumerate(body) if isinstance(n, ast.FunctionDef)))
