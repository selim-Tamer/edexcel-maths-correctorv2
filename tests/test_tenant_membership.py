"""Tenant membership: store semantics + endpoint gating (fail-closed).

Covers IR's conditions: membership-absent/inactive/ambiguous is a hard
reject with no bypass flag, only server-side seeded rows count, unconfigured
storage is 403 membership_unavailable, and the protected API endpoints (not
healthz) require an ACTIVE membership on every call. Also pins the default
app behaviour without GOOGLE_OIDC_AUDIENCE (401 oidc_not_configured) - the
default path is the real control, not an injected test verifier.
"""
import os
import tempfile
import unittest
from pathlib import Path

from api.app import create_app
from fastapi.testclient import TestClient
from membership_store import (
    ACK_ENV,
    PATH_ENV,
    Membership,
    MembershipError,
    default_resolver,
    open_store,
    resolve_membership,
    seed_membership,
)

SUBJECT = "teacher-a-subject"
TENANT = "school-beta"


def active_member(subject: str) -> Membership:
    return Membership(subject, TENANT, "teacher")


class StoreSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "membership.sqlite3")
        self.connection = open_store(self.path)
        self.addCleanup(self.connection.close)

    def test_seeded_active_membership_resolves(self):
        seed_membership(self.connection, subject=SUBJECT, tenant_id=TENANT)
        membership = resolve_membership(self.connection, SUBJECT)
        self.assertIsNotNone(membership)
        self.assertEqual(membership.tenant_id, TENANT)
        self.assertEqual(membership.role, "teacher")
        self.assertEqual(membership.subject, SUBJECT)

    def test_no_row_resolves_to_none(self):
        self.assertIsNone(resolve_membership(self.connection, "nobody"))

    def test_inactive_membership_resolves_to_none(self):
        seed_membership(self.connection, subject=SUBJECT, tenant_id=TENANT,
                        active=False)
        self.assertIsNone(resolve_membership(self.connection, SUBJECT))

    def test_two_active_tenants_are_ambiguous_and_fail_closed(self):
        seed_membership(self.connection, subject=SUBJECT, tenant_id="t-a")
        seed_membership(self.connection, subject=SUBJECT, tenant_id="t-b")
        with self.assertRaises(MembershipError) as caught:
            resolve_membership(self.connection, SUBJECT)
        self.assertEqual(caught.exception.detail, "membership_ambiguous")

    def test_blank_subject_resolves_to_none(self):
        for bad in ("", "   ", None):
            self.assertIsNone(resolve_membership(self.connection, bad))

    def test_default_resolver_without_configuration_fails_closed(self):
        for var in (PATH_ENV, ACK_ENV):
            os.environ.pop(var, None)
        resolver = default_resolver()
        with self.assertRaises(MembershipError) as caught:
            resolver(SUBJECT)
        self.assertEqual(caught.exception.detail, "membership_unavailable")

    def test_default_resolver_requires_the_dev_acknowledgement(self):
        os.environ[PATH_ENV] = self.path
        os.environ.pop(ACK_ENV, None)
        self.addCleanup(os.environ.pop, PATH_ENV, None)
        resolver = default_resolver()
        with self.assertRaises(MembershipError) as caught:
            resolver(SUBJECT)
        self.assertEqual(caught.exception.detail, "membership_unavailable")

    def test_default_resolver_honours_a_seeded_row(self):
        seed_membership(self.connection, subject=SUBJECT, tenant_id=TENANT)
        os.environ[PATH_ENV] = self.path
        os.environ[ACK_ENV] = "1"
        self.addCleanup(os.environ.pop, PATH_ENV, None)
        self.addCleanup(os.environ.pop, ACK_ENV, None)
        resolver = default_resolver()
        self.addCleanup(getattr(resolver, "close", lambda: None))
        membership = resolver(SUBJECT)
        self.assertEqual(membership.tenant_id, TENANT)


class EndpointGatingTests(unittest.TestCase):
    @staticmethod
    def app_with(membership_resolver=None, verify_token=None, **kwargs):
        return create_app(
            verify_token=verify_token or (lambda token: SUBJECT),
            rows_for=lambda subject, owner: [],
            membership_resolver=membership_resolver, **kwargs)

    def call_decision(self, app, token="tok"):
        client = TestClient(app)
        return client.post(
            "/v1/results/manual:deadbeef:review-1/decisions",
            headers={"Authorization": f"Bearer {token}"},
            json={"question_number": "1", "action": "approve",
                  "expected_revision": 0, "idempotency_key": "k"})

    def test_default_membership_unavailable_is_403_on_protected_endpoints(self):
        for var in (PATH_ENV, ACK_ENV):
            os.environ.pop(var, None)
        app = self.app_with()  # membership_resolver defaults to env-backed
        response = self.call_decision(app)
        self.assertEqual(response.status_code, 403, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], "tenant_membership_required")
        self.assertEqual(error["detail"], "membership_unavailable")
        # healthz never requires identity or membership
        self.assertEqual(TestClient(app).get("/v1/healthz").status_code, 200)

    def test_inactive_or_absent_membership_is_403_without_enumerating(self):
        app = self.app_with(membership_resolver=lambda subject: None)
        error = self.call_decision(app).json()["error"]
        self.assertEqual(error["code"], "tenant_membership_required")
        self.assertEqual(error["detail"], "membership_inactive_or_absent")

    def test_ambiguous_membership_propagates_its_static_detail(self):
        def ambiguous(subject):
            raise MembershipError("membership_ambiguous")

        error = self.call_decision(
            self.app_with(membership_resolver=ambiguous)).json()["error"]
        self.assertEqual(error["code"], "tenant_membership_required")
        self.assertEqual(error["detail"], "membership_ambiguous")

    def test_active_membership_reaches_the_next_gate(self):
        # Membership passes: the request proceeds to persistence (dev store
        # not opted in here), proving the gate ordering.
        app = self.app_with(membership_resolver=active_member)
        response = self.call_decision(app)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"],
                         "dev_opt_in_required")

    def test_events_endpoint_is_membership_gated_too(self):
        for var in (PATH_ENV, ACK_ENV):
            os.environ.pop(var, None)
        client = TestClient(self.app_with())
        response = client.get(
            "/v1/results/manual:deadbeef:review-1/events",
            headers={"Authorization": "Bearer tok"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"],
                         "tenant_membership_required")


class DefaultVerifierTests(unittest.TestCase):
    """No injected verifier: the default path is the real control."""

    def setUp(self):
        self._previous = os.environ.pop("GOOGLE_OIDC_AUDIENCE", None)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.pop("GOOGLE_OIDC_AUDIENCE", None)
        if self._previous is not None:
            os.environ["GOOGLE_OIDC_AUDIENCE"] = self._previous

    @staticmethod
    def app():
        return create_app(rows_for=lambda subject, owner: [],
                          membership_resolver=active_member)

    def test_unconfigured_audience_fails_every_protected_call_closed(self):
        client = TestClient(self.app())
        for headers in ({}, {"Authorization": "Bearer whatever"}):
            with self.subTest(headers=headers):
                response = client.post(
                    "/v1/results/manual:deadbeef:review-1/decisions",
                    headers=headers,
                    json={"question_number": "1", "action": "approve",
                          "expected_revision": 0, "idempotency_key": "k"})
                self.assertEqual(response.status_code, 401)
                error = response.json()["error"]
                self.assertEqual(error["code"], "authentication_required")
                expected = "token_missing" if not headers else \
                    "oidc_not_configured"
                self.assertEqual(error["detail"], expected)

    def test_missing_header_still_reports_token_missing_first(self):
        client = TestClient(self.app())
        response = client.get(
            "/v1/results/manual:deadbeef:review-1/events")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["detail"], "token_missing")

    def test_healthz_stays_public_without_any_auth_configuration(self):
        self.assertEqual(TestClient(self.app()).get("/v1/healthz").status_code,
                         200)

    def test_verifier_rejection_details_never_echo_the_token(self):
        client = TestClient(self.app())
        secret = "forged-token-value-123"
        response = client.post(
            "/v1/results/manual:deadbeef:review-1/decisions",
            headers={"Authorization": f"Bearer {secret}"},
            json={"question_number": "1", "action": "approve",
                  "expected_revision": 0, "idempotency_key": "k"})
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(secret, response.text)


if __name__ == "__main__":
    unittest.main()
