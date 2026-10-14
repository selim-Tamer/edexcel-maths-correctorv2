"""Real SDK wire requests intercepted locally; no API key or network required."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from google import genai
from google.genai import types
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support import load_app, benchmark, model_question
import marking_requests as mr
from pipeline_fakes import quota_failure, model_unavailable, Clock


class MarkingSDKTests(unittest.TestCase):
    def test_wire_schema_usage_and_inherited_retries_disabled(self):
        for failure, expected in ((429, 1), (503, 2), (None, 1)):
            seen = []
            payload = {'questions': [model_question(benchmark()['questions'][0], 'partial')]}
            def handler(request):
                seen.append((json.loads(request.content), request.extensions['timeout']))
                if failure:
                    return httpx.Response(failure, json={'error': {'code': failure, 'message': 'synthetic private provider detail'}})
                return httpx.Response(200, json={'candidates': [{'finishReason': 'STOP', 'content': {
                    'role': 'model', 'parts': [{'text': json.dumps(payload)}]}}],
                    'usageMetadata': {'promptTokenCount': 8000, 'candidatesTokenCount': 900, 'thoughtsTokenCount': 1000}})
            f = load_app(); f['types'] = types; f['time'] = Clock()
            # Even an operator's inherited SDK retry configuration is overridden.
            with genai.Client(api_key='synthetic-not-real', http_options=types.HttpOptions(
                    retry_options=types.HttpRetryOptions(attempts=5),
                    client_args={'transport': httpx.MockTransport(handler)})) as client:
                if failure:
                    with self.assertRaises(RuntimeError):
                        f['grade_with_model'](client, f['PRIMARY_MODELS'], 'synthetic student', 'synthetic scheme')
                else:
                    result, _ = f['grade_with_model'](client, f['PRIMARY_MODELS'], 'synthetic student', 'synthetic scheme')
                    self.assertEqual(result['questions'][0]['awarded_marks'], 1)
                    self.assertEqual(mr.state_for(client)['last']['attempts'][0]['thoughts_token_count'], 1000)
            self.assertEqual(len(seen), expected)
            self.assertLessEqual(seen[0][1]['read'], 120)
            config = seen[0][0]['generationConfig']
            self.assertIn('mark_breakdown', config['responseSchema']['properties']['questions']['items']['properties'])
            self.assertNotIn('thinkingConfig', config)
            self.assertNotIn('maxOutputTokens', config)

    def test_timeout_does_not_trigger_second_provider_request(self):
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout('synthetic private provider detail')
        f = load_app(); f['types'] = types; f['time'] = Clock()
        with genai.Client(api_key='synthetic-not-real', http_options=types.HttpOptions(
                client_args={'transport': httpx.MockTransport(handler)})) as client:
            with self.assertRaises(RuntimeError) as error:
                f['grade_with_model'](client, f['PRIMARY_MODELS'], 'synthetic student', 'synthetic scheme')
            self.assertEqual(mr.state_for(client)['last']['attempts'][0]['outcome'], 'timeout')
        self.assertEqual(len(calls), 1)
        self.assertNotIn('private provider', str(error.exception))

    def test_structured_provider_errors_and_fresh_client_recovery(self):
        f = load_app(); f['types'] = types; f['time'] = Clock()
        payload = {'questions': [model_question(benchmark()['questions'][0], 'partial')]}
        for scenario, expected in (('model', 2), ('shared', 1), ('unknown', 1), ('404', 3), ('503', 2), ('504', 1)):
            for fresh_run in range(2):
                seen = []
                def handler(request):
                    seen.append(request.url.path)
                    model = request.url.path.rsplit('/', 1)[-1].split(':')[0]
                    if (scenario == '404' and len(seen) <= 2) or len(seen) == 1:
                        if scenario == '404':
                            return httpx.Response(404, json=model_unavailable(model).details)
                        if scenario in ('model', 'shared', 'unknown'):
                            return httpx.Response(429, json=quota_failure(model, scenario).details)
                        return httpx.Response(int(scenario), json={'error': {'code': int(scenario), 'message': 'synthetic'}})
                    return httpx.Response(200, json={'candidates': [{'finishReason': 'STOP', 'content': {
                        'role': 'model', 'parts': [{'text': json.dumps(payload)}]}}]})
                with self.subTest(scenario=scenario, fresh_run=fresh_run), genai.Client(
                        api_key='synthetic-not-real', http_options=types.HttpOptions(
                            retry_options=types.HttpRetryOptions(attempts=5),
                            client_args={'transport': httpx.MockTransport(handler)})) as client:
                    if scenario in ('shared', 'unknown', '504'):
                        with self.assertRaises(RuntimeError):
                            f['grade_with_model'](client, f['PRIMARY_MODELS'], 'student', 'scheme')
                    else:
                        result, _ = f['grade_with_model'](client, f['PRIMARY_MODELS'], 'student', 'scheme')
                        self.assertEqual(result, payload)
                    self.assertEqual(len(seen), expected)

    def test_real_sdk_retry_after_header_and_single_fallback(self):
        f = load_app(); f['types'] = types; f['time'] = Clock()
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(503, headers={'Retry-After': '12'},
                    json={'error': {'code': 503, 'message': 'synthetic private response'}})
            return httpx.Response(200, json={'candidates': [{'finishReason': 'STOP', 'content': {
                'role': 'model', 'parts': [{'text': '{"questions": [{"awarded_marks": 1}]}'}]}}]})
        with genai.Client(api_key='synthetic-not-real', http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=5),
                client_args={'transport': httpx.MockTransport(handler)})) as client:
            f['grade_with_model'](client, f['PRIMARY_MODELS'], 'student', 'scheme')
            self.assertEqual(f['time'].sleeps, [12])
            self.assertEqual(mr.state_for(client)['last']['sleep_seconds'], 12)
            self.assertNotIn('synthetic private response', json.dumps(mr.state_for(client)['last']))
        self.assertEqual(len(calls), 2)
