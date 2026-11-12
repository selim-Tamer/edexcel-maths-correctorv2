"""Real Requests preparation, redirects and streaming with offline HTTP responses."""
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support import load_app
import document_limits


class RequestsCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.app = load_app()
        self.app["requests"] = requests
        self.captured = []
        self.responses = []
        for target in ("requests.sessions.get_netrc_auth", "requests.sessions.get_environ_proxies"):
            guard = patch(target, return_value={} if target.endswith("proxies") else None)
            guard.start()
            self.addCleanup(guard.stop)
        # Catch every HTTP adapter invocation: no socket or production credentials.
        guard = patch.object(requests.adapters.HTTPAdapter, "send", self.intercept)
        guard.start()
        self.addCleanup(guard.stop)

    def intercept(self, request, **kwargs):
        self.captured.append((request, kwargs))
        if not self.responses:
            raise AssertionError("Unexpected HTTP attempt")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        response.request = request
        response.url = request.url
        return response

    def response(self, status=200, body=b'{}', headers=None):
        response = requests.Response()
        response.status_code = status
        response.raw = io.BytesIO(body)
        response.close = Mock(wraps=response.close)
        response.headers.update(headers or {"Content-Type": "application/json"})
        self.responses.append(response)
        return response

    def call(self, **kwargs):
        return self.app["classroom_request"]("GET", "https://classroom.example.invalid/v1/courses", "synthetic-token", **kwargs)

    def test_headers_timeout_and_legacy_parameter(self):
        self.response(body=b'{"courses": []}')
        self.assertEqual(self.call(params={"studentSubmissionStates": "TURNED_IN"}), {"courses": []})
        request, options = self.captured[0]
        self.assertEqual(request.headers["Authorization"], "Bearer synthetic-token")
        self.assertEqual(request.headers["Accept"], "application/json")
        self.assertEqual(options["timeout"], 60)
        self.assertTrue(options["verify"])
        self.assertEqual(parse_qs(urlsplit(request.url).query), {"states": ["TURNED_IN"]})

    def test_http_error_hides_body_and_does_not_retry(self):
        self.response(403, b'synthetic-private-body synthetic-token')
        with self.assertRaisesRegex(RuntimeError, r"^Google API error 403\.$"):
            self.call()
        self.assertEqual(len(self.captured), 1)

    def test_timeout_does_not_retry(self):
        self.responses.append(requests.exceptions.ReadTimeout("synthetic timeout"))
        with self.assertRaises(requests.exceptions.ReadTimeout):
            self.call()
        self.assertEqual(len(self.captured), 1)

    def test_empty_and_malformed_json(self):
        self.response(204, b'')
        self.assertEqual(self.call(), {})
        self.response(body=b'not-json')
        with self.assertRaises(requests.exceptions.JSONDecodeError):
            self.call()

    def test_cross_origin_redirect_strips_bearer(self):
        self.response(302, b'', {"Location": "https://other.example.invalid/result"})
        self.response()
        self.assertEqual(self.call(), {})
        self.assertEqual(len(self.captured), 2)
        self.assertNotIn("Authorization", self.captured[1][0].headers)

    def test_repeated_pagination_stops(self):
        for _ in range(2):
            self.response(body=b'{"courses": [], "nextPageToken": "repeated"}')
        with self.assertRaisesRegex(RuntimeError, "repeated a pagination token"):
            self.app["classroom_list_all"]("synthetic-token", "https://classroom.example.invalid/courses", "courses", {})
        self.assertEqual(len(self.captured), 2)

    def download(self, limit=20):
        return self.app["_download_bounded"]("synthetic-token", "https://drive.example.invalid/file", {}, limit, duration=10)

    def test_streamed_download_is_bounded_and_closed(self):
        response = self.response(body=b'synthetic-pdf')
        self.assertEqual(self.download(), b'synthetic-pdf')
        response.close.assert_called_once_with()
        options = self.captured[0][1]
        self.assertEqual(options["timeout"], 10)
        self.assertTrue(options["stream"])
        self.assertTrue(options["verify"])

    def test_oversized_stream_closed_without_returning_partial_data(self):
        response = self.response(body=b'x' * 30)
        with self.assertRaises(document_limits.DocumentValidationError):
            self.download()
        self.assertTrue(response.raw.closed)
        self.assertEqual(len(self.captured), 1)

    def test_drive_http_error_is_private_and_closed(self):
        response = self.response(403, b'synthetic-private-body synthetic-token')
        with self.assertRaisesRegex(RuntimeError, r"^Could not download the Drive attachment \(HTTP 403\)\.$"):
            self.download()
        self.assertTrue(response.raw.closed)


if __name__ == "__main__":
    unittest.main()
