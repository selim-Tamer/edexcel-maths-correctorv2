"""Intercept the diagnostic HTTP boundary; zero external requests."""
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import httpx
from google.genai import types
from investigation import diagnose as d


class AvailabilityWireTests(unittest.TestCase):
    def test_each_http_failure_sends_exactly_once_without_retry(self):
        for code in (400,401,403,404,429,503,504,302):
            seen=[]
            def handler(request):
                seen.append(request)
                return httpx.Response(code,headers={'location':'https://example.invalid'},json={'error':'PRIVATE'})
            result=d.probe(d.MODELS[0],'A',5,'synthetic-key',transport=httpx.MockTransport(handler))
            self.assertEqual(len(seen),1)
            self.assertEqual(result['attempt_count'],1)
            self.assertEqual(result['http_status'],code)
            self.assertNotIn('PRIVATE',json.dumps(result))
            self.assertNotIn('synthetic-key',json.dumps(result))

    def test_transport_timeouts_are_not_retried(self):
        for error in (httpx.ConnectTimeout,httpx.ReadTimeout,httpx.WriteTimeout):
            seen=[]
            def handler(request):
                seen.append(request); raise error('PRIVATE')
            result=d.probe(d.MODELS[0],'B',5,'synthetic-key',transport=httpx.MockTransport(handler))
            self.assertEqual(len(seen),1)
            self.assertEqual(result['outcome'],'client_timeout')
            self.assertNotIn('PRIVATE',json.dumps(result))

    def test_success_and_small_pdf_request_use_expected_host_and_schema(self):
        for level in ('A','B','C'):
            seen=[]
            def handler(request):
                seen.append(request)
                return httpx.Response(200,json={'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':'{"answer":4}'}]}}]})
            result=d.probe(d.MODELS[0],level,15,'synthetic-key',transport=httpx.MockTransport(handler))
            self.assertEqual(len(seen),1)
            self.assertEqual(seen[0].url.host,'generativelanguage.googleapis.com')
            self.assertEqual(seen[0].extensions['timeout']['read'],15)
            self.assertNotIn('synthetic-key',str(seen[0].url))
            payload=json.loads(seen[0].content)
            config=payload['generationConfig']
            parsed=types.GenerateContentConfig(response_mime_type=config['responseMimeType'],response_schema=config['responseSchema'])
            self.assertIsNotNone(parsed.response_schema)
            if level=='A': self.assertTrue(result['score_agreement'])
            if level=='C': self.assertEqual(sum('inlineData' in p for p in payload['contents'][0]['parts']),2)

    def test_oversized_response_rejected_without_retry(self):
        seen=[]
        def handler(request):
            seen.append(request); return httpx.Response(200,content=b'x'*(d.MAX_BODY+1))
        result=d.probe(d.MODELS[0],'A',5,'synthetic',transport=httpx.MockTransport(handler))
        self.assertEqual(result['outcome'],'response_too_large')
        self.assertEqual(len(seen),1)
