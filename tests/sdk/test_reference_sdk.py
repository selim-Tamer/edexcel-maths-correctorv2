"""Metadata schema serialization through the real minimum SDK; HTTP intercepted."""
import json
import sys
import unittest
from pathlib import Path
import httpx
from google import genai
from google.genai import types
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support import load_app
from test_reference_performance import payload
import document_pipeline as dp


class ReferenceSDKTests(unittest.TestCase):
    def test_combined_schema_and_original_references(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={'candidates': [{'finishReason': 'STOP', 'content': {
                'role': 'model', 'parts': [{'text': json.dumps(payload())}]}}]})
        f = load_app()
        f['types'] = types
        f['model_on_quota_cooldown'] = lambda _: False
        with genai.Client(api_key='synthetic-not-real', http_options=types.HttpOptions(
                client_args={'transport': httpx.MockTransport(handler)})) as client:
            info, nums = f['prepare_reference_metadata'](client, 'original scheme', dp.Memo(), 'scheme', 'inventory', 'original questions')
        self.assertEqual(len(requests), 1)
        cfg = requests[0]['generationConfig']
        self.assertIn('inventory_complete', cfg['responseJsonSchema']['required'])
        self.assertEqual(cfg['maxOutputTokens'], 8192)
        self.assertEqual(info[0], {'1': 2})
        self.assertEqual(nums, ['1', '2'])
        parts = requests[0]['contents'][0]['parts']
        self.assertEqual([p['text'] for p in parts[:2]], ['original scheme', 'original questions'])
