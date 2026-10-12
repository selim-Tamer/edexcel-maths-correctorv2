"""Actual SDK serialization with intercepted HTTP; never contacts Gemini."""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
import httpx
from google import genai
from google.genai import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support import load_app, benchmark, model_question
import test_evidence_review as evidence_cases


class AuditSDKTests(unittest.TestCase):
    def audit(self, status=200, finish="STOP"):
        captured = []
        payload = {"reviews": [evidence_cases.EvidenceReviewTests().proposal()]}

        def handler(request):
            captured.append(json.loads(request.content))
            body = ({"candidates": [{"finishReason": finish, "content": {
                "role": "model", "parts": [{"text": json.dumps(payload)}]}}]}
                if status == 200 else {"error": {"code": status, "message": "synthetic private provider error", "status": "RESOURCE_EXHAUSTED"}})
            return httpx.Response(status, json=body)

        f = load_app()
        f["types"] = types
        q = model_question(benchmark()["questions"][0], "correct")
        q["confidence"] = "Low"
        with genai.Client(api_key="synthetic-not-a-real-key", http_options=types.HttpOptions(
                client_args={"transport": httpx.MockTransport(handler)})) as client:
            result, _ = f["audit_result"](client, {"questions": [q]}, "synthetic student", "synthetic scheme", f["AUDIT_MODELS"])
        self.assertEqual(len(captured), 1)
        cfg = captured[0]["generationConfig"]
        self.assertEqual(cfg["responseMimeType"], "application/json")
        self.assertIn("reviews", cfg["responseJsonSchema"]["properties"])
        self.assertEqual(cfg["maxOutputTokens"], 8192)
        return result["questions"][0]["audit_review"]

    def test_real_sdk_schema_roundtrip(self):
        self.assertEqual(self.audit()["status"], "proposal awaiting teacher review")

    def test_real_sdk_quota_does_not_retry_or_leak_provider_body(self):
        review = self.audit(status=429)
        self.assertEqual(review["diagnostics"][0]["outcome"], "quota_limited")
        self.assertNotIn("private provider", json.dumps(review))

    def test_real_sdk_truncation_does_not_accept_partial_result(self):
        self.assertEqual(self.audit(finish="MAX_TOKENS")["diagnostics"][0]["outcome"], "truncated_response")


if __name__ == "__main__":
    unittest.main()
