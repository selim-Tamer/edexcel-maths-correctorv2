"""Offline selection/contract tests for the PostgreSQL membership resolver.

These run WITHOUT a database: they pin the fail-closed decisions and the
process-level selection, so a regression cannot quietly fall back to the
SQLite dev store when ``AI_MARKER_PG_DSN`` is configured, and a refusal can
never echo the DSN or a password.

The real-PostgreSQL half lives in ``test_postgres_membership_contract.py``
(CI service container; local skip with an explicit reason).
"""
import os
import unittest
from unittest import mock

import membership_store
import postgres_membership_store
from api.app import _default_membership_resolver, create_app
from fastapi.testclient import TestClient


class SelectionTests(unittest.TestCase):
    def test_no_dsn_keeps_the_development_resolver(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_MARKER_PG_DSN", None)
            self.assertFalse(postgres_membership_store.configured())
            resolver = _default_membership_resolver()
            self.assertIsNot(resolver, postgres_membership_store.resolve)
            # The development resolver is fail-closed without its opt-in.
            with self.assertRaises(membership_store.MembershipError) as caught:
                resolver("google:teacher-a")
        self.assertEqual(caught.exception.detail, "membership_unavailable")

    def test_a_configured_dsn_selects_the_postgres_resolver(self):
        with mock.patch.dict(os.environ,
                             {"AI_MARKER_PG_DSN": "postgresql://db/app"}):
            self.assertTrue(postgres_membership_store.configured())
            resolver = _default_membership_resolver()
        self.assertIs(resolver, postgres_membership_store.resolve)

    def test_blank_dsn_is_not_a_configured_store(self):
        for blank in ("", "   "):
            with self.subTest(dsn=blank):
                with mock.patch.dict(os.environ,
                                     {"AI_MARKER_PG_DSN": blank}):
                    self.assertFalse(postgres_membership_store.configured())


class FailClosedTests(unittest.TestCase):
    def test_blank_subject_is_absent_not_a_guess(self):
        with self.assertRaises(membership_store.MembershipError) as caught:
            postgres_membership_store.resolve("   ",
                                              dsn="postgresql://unused/app")
        self.assertEqual(caught.exception.detail,
                         "membership_inactive_or_absent")

    def test_no_dsn_is_unavailable(self):
        with self.assertRaises(membership_store.MembershipError) as caught:
            postgres_membership_store.resolve("google:teacher-a", dsn="")
        self.assertEqual(caught.exception.detail, "membership_unavailable")

    def test_an_unreachable_database_is_unavailable_and_silent(self):
        # A syntactically valid but unroutable DSN: resolution must refuse
        # with the static code and never put the DSN (or any password) in the
        # exception text.
        dsn = ("postgresql://marker:super-secret-password@127.0.0.1:1/"
               "does-not-exist?connect_timeout=1")
        with self.assertRaises(membership_store.MembershipError) as caught:
            postgres_membership_store.resolve("google:teacher-a", dsn=dsn)
        self.assertEqual(caught.exception.detail, "membership_unavailable")
        self.assertNotIn("super-secret-password", str(caught.exception))
        self.assertNotIn("does-not-exist", str(caught.exception))

    def test_open_store_without_a_dsn_fails_closed(self):
        with self.assertRaises(membership_store.MembershipError) as caught:
            postgres_membership_store.open_store("")
        self.assertEqual(caught.exception.detail, "membership_unavailable")


class EndpointMappingTests(unittest.TestCase):
    """A resolver refusal keeps its 403 envelope and static detail."""

    def app(self, detail):
        def refuse(_subject):
            raise membership_store.MembershipError(detail)

        return TestClient(create_app(
            verify_token=lambda token: "teacher-a",
            membership_resolver=refuse, engine=None))

    def test_unavailable_maps_to_403_membership_unavailable(self):
        response = self.app("membership_unavailable").get(
            "/v1/jobs/job-1", headers={"Authorization": "Bearer t"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"],
                         "tenant_membership_required")
        self.assertEqual(response.json()["error"]["detail"],
                         "membership_unavailable")

    def test_ambiguous_maps_to_403_with_its_own_detail(self):
        response = self.app("membership_ambiguous").get(
            "/v1/jobs/job-1", headers={"Authorization": "Bearer t"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["detail"],
                         "membership_ambiguous")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
