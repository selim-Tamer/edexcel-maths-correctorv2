"""Browser-facing API surface: CORS allowlist, question numbers, entrypoint.

Offline and in-process. Three things the live-beta integration needs and the
unit suite did not cover before:

* CORS - the frontend is a browser on a different origin, so without an exact
  allowlist every JSON/Authorization request fails preflight. The tests prove
  an allowed origin is echoed, a different origin is refused, credentials stay
  off and no wildcard can be configured.
* ``question_number`` - the canvas sends a JSON number while question identity
  is a string. A refused integer stalled the review flow, so both forms are
  accepted and normalised; invalid forms still fail.
* ``api/server.py`` - the deployed entrypoint must refuse an incomplete
  runtime composition instead of serving fail-closed 503s.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runtime_config
from api.app import DecisionRequest, create_app
from api.server import build_application
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from unittest import mock

ALLOWED = "https://marker.example.pages.dev"
OTHER = "https://not-this-client.example"


def app_with(origins):
    return create_app(verify_token=lambda token: "subject",
                      allowed_origins=origins)


def production_env(**overrides):
    env = {
        runtime_config.RUNTIME_ENV: "production",
        runtime_config.PG_DSN_ENV:
            "postgresql://marker:secret@pooler.example:6543/postgres"
            "?sslmode=require",
        runtime_config.OIDC_AUDIENCE_ENV:
            "1234.apps.googleusercontent.com",
        runtime_config.SUPABASE_URL_ENV: "https://ref.supabase.co",
        runtime_config.SUPABASE_KEY_ENV: "service-role-key",
        runtime_config.BUCKET_ENV: "papers",
        runtime_config.ALLOWED_ORIGINS_ENV: ALLOWED,
    }
    env.update(overrides)
    return env


class CorsTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_with([ALLOWED]))

    def preflight(self, origin, method="POST", headers="authorization"):
        return self.client.options("/v1/jobs", headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        })

    def test_preflight_for_the_configured_origin_is_allowed(self):
        response = self.preflight(ALLOWED)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"],
                         ALLOWED)
        self.assertIn("POST",
                      response.headers["access-control-allow-methods"])
        self.assertIn("authorization",
                      response.headers["access-control-allow-headers"].lower())

    def test_preflight_for_any_other_origin_is_refused(self):
        response = self.preflight(OTHER)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_actual_response_carries_the_origin_and_never_credentials(self):
        response = self.client.get("/v1/healthz", headers={"Origin": ALLOWED})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"],
                         ALLOWED)
        # Bearer tokens do not need cookies; credentials must stay off so a
        # stray cookie can never be replayed cross-origin.
        self.assertNotIn("access-control-allow-credentials", response.headers)

    def test_error_envelope_carries_the_same_cors_decision(self):
        # A 401 from the real route must be readable by the browser, otherwise
        # the client sees an opaque network error instead of the code.
        response = self.client.post(
            "/v1/results/unknown/decisions", headers={"Origin": ALLOWED},
            json={"question_number": "1", "action": "approve",
                  "expected_revision": 0, "idempotency_key": "k" * 8})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["access-control-allow-origin"],
                         ALLOWED)
        self.assertEqual(response.json()["error"]["code"],
                         "authentication_required")

    def test_wildcard_and_path_origins_are_refused_at_startup(self):
        for origins in (["*"], ["https://marker.pages.dev/reset"],
                        ["https://marker.pages.dev/"],
                        ["http://marker.pages.dev"]):
            with self.subTest(origins=origins):
                with self.assertRaises(runtime_config.RuntimeConfigError) as ctx:
                    app_with(origins)
                self.assertEqual(ctx.exception.code, "service_unavailable")

    def test_environment_allowlist_is_used_when_nothing_is_injected(self):
        with mock.patch.dict("os.environ",
                             {runtime_config.ALLOWED_ORIGINS_ENV:
                              f"{ALLOWED}, {OTHER}"}, clear=False):
            client = TestClient(create_app(verify_token=lambda token: "s"))
        response = client.options("/v1/jobs", headers={
            "Origin": OTHER,
            "Access-Control-Request-Method": "POST",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"],
                         OTHER)

    def test_no_middleware_is_added_without_an_allowlist(self):
        # Development default: no allowlist, no browser access. Fail closed
        # rather than invent an origin.
        client = TestClient(app_with([]))
        response = client.options("/v1/jobs", headers={
            "Origin": ALLOWED,
            "Access-Control-Request-Method": "POST",
        })
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_production_refuses_to_build_without_an_allowlist(self):
        # server.py already gates this; a direct create_app() must not be a
        # quieter way to ship a browser-facing service with no origin.
        env = {runtime_config.RUNTIME_ENV: runtime_config.RUNTIME_PRODUCTION}
        with mock.patch.dict("os.environ", env, clear=False):
            os.environ.pop(runtime_config.ALLOWED_ORIGINS_ENV, None)
            with self.assertRaises(runtime_config.RuntimeConfigError) as ctx:
                create_app(verify_token=lambda token: "s")
        self.assertEqual(ctx.exception.code, "service_unavailable")


class QuestionNumberTests(unittest.TestCase):
    def request(self, **overrides):
        body = {"question_number": "1", "action": "approve",
                "expected_revision": 0, "idempotency_key": "key-1234"}
        body.update(overrides)
        return DecisionRequest(**body)

    def test_the_canvas_integer_is_normalised_to_the_canonical_string(self):
        self.assertEqual(self.request(question_number=1).question_number, "1")
        self.assertEqual(self.request(question_number=12).question_number,
                         "12")

    def test_strings_are_stripped_but_not_renumbered(self):
        self.assertEqual(self.request(question_number=" 2a ").question_number,
                         "2a")
        self.assertEqual(self.request(question_number="1").question_number,
                         "1")

    def test_unusable_question_numbers_still_fail_validation(self):
        for value in ("", "   ", "x" * 257, True, False, None, ["1"]):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    self.request(question_number=value)


class ServerEntrypointTests(unittest.TestCase):
    def test_development_composition_builds_the_app(self):
        app = build_application({})
        self.assertIsInstance(app, FastAPI)
        paths = {route.path for route in app.routes}
        self.assertIn("/v1/jobs", paths)
        self.assertIn("/v1/results/{result_id}/decisions", paths)

    def test_incomplete_production_composition_refuses_to_start(self):
        with self.assertRaises(runtime_config.RuntimeConfigError):
            build_application({runtime_config.RUNTIME_ENV: "production"})

    def test_production_without_an_origin_allowlist_refuses_to_start(self):
        env = production_env()
        env.pop(runtime_config.ALLOWED_ORIGINS_ENV)
        with self.assertRaises(runtime_config.RuntimeConfigError) as ctx:
            build_application(env)
        self.assertEqual(ctx.exception.code, "service_unavailable")
        self.assertIn(runtime_config.ALLOWED_ORIGINS_ENV, str(ctx.exception))

    def test_complete_production_composition_is_ready(self):
        report = runtime_config.evaluate(production_env())
        self.assertTrue(report["ready"], report["problems"])
        self.assertTrue(
            report["components"][runtime_config.ALLOWED_ORIGINS_ENV]
            ["required_in_production"])

    def test_http_origin_is_not_a_production_origin(self):
        report = runtime_config.evaluate(
            production_env(**{runtime_config.ALLOWED_ORIGINS_ENV:
                              "http://marker.example.pages.dev"}))
        self.assertFalse(report["ready"])
        self.assertEqual(report["problems"][0]["code"], "service_unavailable")


if __name__ == "__main__":
    unittest.main()
