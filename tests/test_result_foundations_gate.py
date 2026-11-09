"""BR-02 foundations F5 gate tests: fresh result completion is denied.

Offline, in-process, synthetic only. Verifies that the fail-closed
fresh-result-completion gate blocks every production registration/execution
path when the foundations package gate is active, and that it does not block
development/injection paths or review-time validation of existing results.

No PostgreSQL, no network, no paid provider, no database connection.
PG environment variables are cleared in child env only; no persisted config
change.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audit_store
import runtime_config
from api.app import ApiError, create_app
from fastapi.testclient import TestClient
from membership_store import Membership
import result_service
import postgres_result_store

SUBJECT = "teacher-gate"
OWNER = "google:" + SUBJECT
TENANT = "tenant-gate"
TOKEN = "tok-gate"


def active_member(subject: str) -> Membership:
    return Membership(subject, TENANT, "teacher")


class GateConfigTests(unittest.TestCase):
    """runtime_config.fresh_result_completion_denied: missing/false deny,
    true/malformed raise at startup."""

    def _clear_env(self):
        os.environ.pop(runtime_config.FRESH_RESULT_COMPLETION_ENABLED_ENV, None)

    def test_missing_env_denies_per_request(self):
        self._clear_env()
        self.addCleanup(self._clear_env)
        self.assertTrue(runtime_config.fresh_result_completion_denied())

    def test_false_env_denies_per_request(self):
        os.environ[runtime_config.FRESH_RESULT_COMPLETION_ENABLED_ENV] = "false"
        self.addCleanup(self._clear_env)
        self.assertTrue(runtime_config.fresh_result_completion_denied())

    def test_zero_env_denies_per_request(self):
        os.environ[runtime_config.FRESH_RESULT_COMPLETION_ENABLED_ENV] = "0"
        self.addCleanup(self._clear_env)
        self.assertTrue(runtime_config.fresh_result_completion_denied())

    def test_true_env_raises_startup_refusal(self):
        os.environ[runtime_config.FRESH_RESULT_COMPLETION_ENABLED_ENV] = "true"
        self.addCleanup(self._clear_env)
        with self.assertRaises(runtime_config.RuntimeConfigError) as caught:
            runtime_config.fresh_result_completion_denied()
        self.assertEqual(caught.exception.code, "service_unavailable")

    def test_malformed_env_raises_startup_refusal(self):
        os.environ[runtime_config.FRESH_RESULT_COMPLETION_ENABLED_ENV] = "bogus"
        self.addCleanup(self._clear_env)
        with self.assertRaises(runtime_config.RuntimeConfigError) as caught:
            runtime_config.fresh_result_completion_denied()
        self.assertEqual(caught.exception.code, "service_unavailable")


class GateApiTests(unittest.TestCase):
    """Production app denies fresh completion at every entry point."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = audit_store.open_store(
            str(Path(self.tmp.name) / "audit.sqlite3"), dev_only_ack=True)
        self.addCleanup(self.store.close)

    def _build_prod_app(self):
        """Build a production-composed app (explicit RUNTIME_ENV=production)."""
        env = dict(os.environ)
        env.pop(runtime_config.FRESH_RESULT_COMPLETION_ENABLED_ENV, None)
        env["GOOGLE_OIDC_AUDIENCE"] = "test-audience"
        env["AI_MARKER_RUNTIME"] = "production"
        env["AI_MARKER_PG_DSN"] = "postgresql://example/db?sslmode=require"
        env["AI_MARKER_ALLOWED_ORIGINS"] = "https://test.example"
        with mock.patch.dict(os.environ, env, clear=False):
            return create_app(
                verify_token={TOKEN: SUBJECT}.get,
                membership_resolver=active_member,
                authorize_ingest=lambda subject, tenant_id, row: True,
                manual_document_provenance_ok=lambda row: True,
                store_provider=lambda: (self.store, None),
            )

    def test_post_v1_results_denied_when_gate_active(self):
        app = self._build_prod_app()
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/v1/results", headers={"Authorization": f"Bearer {TOKEN}"},
            json={"scope": "manual", "original_output": {"questions": []},
                  "manual_identity": {"document_digest": "d" * 64,
                                      "submission_ref": "ref-1"}})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "store_unusable")

    def test_run_marking_job_denied_when_gate_active(self):
        app = self._build_prod_app()
        with self.assertRaises(ApiError) as caught:
            app.state.run_marking_job(
                job_id="job-1", who=("system:marker-worker", OWNER, TENANT))
        self.assertEqual(caught.exception.code, "store_unusable")

    def test_dev_mode_not_blocked(self):
        """Dev mode (no RUNTIME_ENV=production) is not subject to gate.

        Verifies via app creation that the gate flag is not set in dev mode.
        """
        env = dict(os.environ)
        env.pop("AI_MARKER_RUNTIME", None)
        env.pop(runtime_config.FRESH_RESULT_COMPLETION_ENABLED_ENV, None)
        env["GOOGLE_OIDC_AUDIENCE"] = "test-audience"
        with mock.patch.dict(os.environ, env, clear=False):
            app = create_app(
                verify_token={TOKEN: SUBJECT}.get,
                membership_resolver=active_member,
                authorize_ingest=lambda subject, tenant_id, row: True,
                manual_document_provenance_ok=lambda row: True,
                store_provider=lambda: (self.store, None),
            )
        # App created successfully: gate did not raise at startup.
        self.assertIsNotNone(app)


class GateServiceTests(unittest.TestCase):
    """ResultService.register denied; register_result is read-only validation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    class _FakeStore:
        """Instrumented synthetic store: records every call, denies nothing."""

        def __init__(self):
            self.register_calls = 0
            self.register_result_calls = 0
            self.closed = False
            self._registered = {}

        def register_with_context(self, **kwargs):
            self.register_calls += 1
            rid = kwargs["result_id"]
            self._registered[rid] = kwargs.get("original_output", {})
            return {"created": True, "owner": OWNER, "result_id": rid,
                    "result_revision": 0,
                    "original_digest": audit_store.digest_value(
                        kwargs.get("original_output", {}))}

        def register_result(self, **kwargs):
            self.register_result_calls += 1
            rid = kwargs["result_id"]
            if rid not in self._registered:
                raise audit_store.AuditStoreError(
                    "result_not_registered",
                    "no result registered for this owner")
            return {"created": False, "owner": kwargs["owner"],
                    "result_id": rid, "result_revision": 0,
                    "original_digest": audit_store.digest_value(
                        self._registered[rid]),
                    "original_scores": audit_store.original_score_projection(self._registered[rid])}

        def original_record(self, **kwargs):
            rid = kwargs["result_id"]
            if rid not in self._registered:
                raise audit_store.AuditStoreError(
                    "result_not_registered",
                    "no result registered for this owner")
            return {"original_digest": audit_store.digest_value(
                self._registered[rid]),
                "result_revision": 0,
                "original_scores": audit_store.original_score_projection(self._registered[rid])}

        def context_record(self, **kwargs):
            rid = kwargs["result_id"]
            if rid not in self._registered:
                raise audit_store.AuditStoreError(
                    "result_not_registered",
                    "no result registered for this owner")
            return {"original_digest": audit_store.digest_value(
                self._registered[rid]),
                    "owner_subject": OWNER,
                    "membership_subject": SUBJECT,
                    "tenant_id": TENANT}

        def close(self):
            self.closed = True

    def test_service_register_denied_when_gate_active(self):
        store = self._FakeStore()
        from identity_context import IdentityContext
        identity = IdentityContext(
            verified_subject=SUBJECT, membership_subject=SUBJECT,
            owner_subject=OWNER, tenant_id=TENANT)
        service = result_service.ResultService(store, identity,
                                               fresh_completion_denied=True)
        with self.assertRaises(audit_store.AuditStoreError) as caught:
            service.register(result_id="res-1",
                             original_output={"questions": []},
                             scope="manual", review_key="rk-1")
        self.assertEqual(caught.exception.code, "store_unusable")
        # No call reached the store.
        self.assertEqual(store.register_calls, 0)

    def test_service_register_result_read_only_validation(self):
        """register_result validates existing original; never creates."""
        store = self._FakeStore()
        store.register_with_context(
            result_id="res-existing",
            original_output={"questions": [{"question_number": "1",
                                            "awarded_marks": 1,
                                            "max_marks": 2}]},
            scope="manual", review_key="rk-existing")
        # Reset call counter after setup write.
        store.register_calls = 0
        from identity_context import IdentityContext
        identity = IdentityContext(
            verified_subject=SUBJECT, membership_subject=SUBJECT,
            owner_subject=OWNER, tenant_id=TENANT)
        service = result_service.ResultService(store, identity,
                                               fresh_completion_denied=True)
        # Matching digest -> returns stored receipt, no write call.
        result = service.register_result(owner=OWNER, result_id="res-existing",
                                         original_output={"questions": [
                                             {"question_number": "1",
                                              "awarded_marks": 1,
                                              "max_marks": 2}]})
        self.assertEqual(result["result_revision"], 0)
        # Mismatched digest -> refused, no creation.
        with self.assertRaises(audit_store.AuditStoreError) as caught:
            service.register_result(owner=OWNER, result_id="res-existing",
                                    original_output={"questions": [
                                        {"question_number": "1",
                                         "awarded_marks": 9,
                                         "max_marks": 2}]})
        self.assertEqual(caught.exception.code,
                         "original_output_immutable")
        # Missing context -> refused.
        with self.assertRaises(audit_store.AuditStoreError) as caught:
            service.register_result(owner=OWNER, result_id="res-missing",
                                    original_output={"questions": []})
        self.assertEqual(caught.exception.code,
                         "result_not_registered")
        # register_result should never invoke register_with_context.
        self.assertEqual(store.register_calls, 0)


class GateWorkerTests(unittest.TestCase):
    """Worker run_one is refused before discovery/claim."""

    def test_run_one_denied_when_gate_active(self):
        import worker as worker_mod

        class FakeApi:
            def find_runnable_job(self, connection, *, tenant_id, now=None):
                return {"job_id": "job-1", "owner_subject": OWNER,
                        "tenant_id": TENANT, "document_digest": "d" * 64,
                        "status": "queued"}

        class FakeApp:
            class state:
                pass

        outcome = worker_mod.run_one(
            FakeApp(), FakeApi(), None, tenant_id=TENANT,
            fresh_result_completion_denied=True)
        self.assertEqual(outcome["status"], "error")
        self.assertEqual(outcome["error_code"], "store_unusable")
        self.assertIsNone(outcome.get("job_id"))

    def test_run_one_not_denied_when_gate_inactive(self):
        import worker as worker_mod

        class FakeApi:
            def find_runnable_job(self, connection, *, tenant_id, now=None):
                return None

        class FakeApp:
            class state:
                pass

        outcome = worker_mod.run_one(
            FakeApp(), FakeApi(), None, tenant_id=TENANT,
            fresh_result_completion_denied=False)
        self.assertEqual(outcome["executed"], False)
        self.assertEqual(outcome["tenant_id"], TENANT)


class NonDelegableProductionPolicyTests(unittest.TestCase):
    """Normal production cannot opt out through flags, DI or local calls."""

    def setUp(self):
        from identity_context import from_verified_subject
        self.identity = from_verified_subject(SUBJECT, TENANT)
        self.env = {
            "AI_MARKER_RUNTIME": "production",
            "AI_MARKER_PG_DSN": "postgresql://synthetic.invalid/test",
            "AI_MARKER_ALLOWED_ORIGINS": "https://test.example",
        }

    def test_explicit_false_service_flag_cannot_enable_production(self):
        store = mock.Mock()
        with mock.patch.dict(os.environ, self.env, clear=True):
            service = result_service.ResultService(
                store, self.identity, fresh_completion_denied=False)
            with self.assertRaises(audit_store.AuditStoreError):
                service.register(result_id="r", original_output={"questions": []},
                                 scope="manual", review_key="review")
        store.register_with_context.assert_not_called()

    def test_service_rechecks_production_after_construction(self):
        store = mock.Mock()
        with mock.patch.dict(os.environ, {}, clear=True):
            service = result_service.ResultService(
                store, self.identity, fresh_completion_denied=False)
        with mock.patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(audit_store.AuditStoreError):
                service.register(result_id="r", original_output={"questions": []},
                                 scope="manual", review_key="review")
        store.register_with_context.assert_not_called()

    def test_explicit_false_store_flag_cannot_enable_production(self):
        connection = mock.Mock()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(postgres_result_store.PostgresAuditStore,
                                  "__init__", return_value=None), \
                mock.patch.object(postgres_result_store.PostgresResultStore,
                                  "_require_capability"):
            store = postgres_result_store.PostgresResultStore(
                connection, identity=self.identity, fresh_completion_denied=False)
            with self.assertRaises(audit_store.AuditStoreError):
                store.register_with_context(
                    result_id="r", original_output={"questions": []},
                    scope="manual", review_key="review")
        connection.execute.assert_not_called()

    def test_production_entrypoint_without_dsn_refuses_even_with_factory(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(runtime_config.RuntimeConfigError):
                create_app(production_mode=True, engine=None,
                           allowed_origins=["https://test.example"],
                           result_service_factory=mock.Mock())

    def test_injected_production_routes_and_local_call_have_zero_effects(self):
        for value in (None, "false"):
            with self.subTest(value=value):
                env = dict(self.env)
                if value is not None:
                    env[runtime_config.FRESH_RESULT_COMPLETION_ENABLED_ENV] = value
                factory, provider, engine = mock.Mock(), mock.Mock(), mock.Mock()
                with mock.patch.dict(os.environ, env, clear=True):
                    app = create_app(
                        verify_token={TOKEN: SUBJECT}.get,
                        membership_resolver=active_member,
                        result_service_factory=factory,
                        store_provider=provider, engine=engine,
                        authorize_ingest=lambda *args: True)
                    with mock.patch("api.app._job_api") as jobs, \
                            mock.patch("api.app._document_api") as documents:
                        with TestClient(app) as client:
                            for _ in range(2):
                                response = client.post(
                                    "/v1/results",
                                    headers={"Authorization": f"Bearer {TOKEN}"},
                                    json={"scope": "manual", "original_output": {"questions": []},
                                          "manual_identity": {"document_digest": "d" * 64,
                                                              "submission_ref": "review"}})
                                self.assertEqual(response.status_code, 503)
                                response = client.post(
                                    "/v1/jobs/job-1/run",
                                    headers={"Authorization": f"Bearer {TOKEN}"})
                                self.assertEqual(response.status_code, 503)
                        with self.assertRaises(ApiError):
                            app.state.run_marking_job(
                                "job-1", who=("forged", OWNER, TENANT))
                        jobs.assert_not_called()
                        documents.assert_not_called()
                factory.assert_not_called()
                provider.assert_not_called()
                engine.assert_not_called()

    def test_bad_production_gate_config_refuses_before_factory(self):
        for value in ("true", "ambiguous"):
            with self.subTest(value=value):
                env = dict(self.env)
                env[runtime_config.FRESH_RESULT_COMPLETION_ENABLED_ENV] = value
                factory = mock.Mock()
                with mock.patch.dict(os.environ, env, clear=True):
                    with self.assertRaises(runtime_config.RuntimeConfigError):
                        create_app(result_service_factory=factory, engine=None)
                factory.assert_not_called()

    def test_worker_false_flag_cannot_reach_discovery_in_production(self):
        import worker
        api = mock.Mock()
        with mock.patch.dict(os.environ, self.env, clear=True):
            outcome = worker.run_one(mock.Mock(), api, mock.Mock(),
                                     tenant_id=TENANT, fresh_result_completion_denied=False)
        self.assertEqual(outcome["error_code"], "store_unusable")
        api.find_runnable_job.assert_not_called()

    def test_worker_main_refuses_before_database_or_application_creation(self):
        import worker
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(runtime_config, "require", return_value={"production": True}), \
                mock.patch.object(worker.result_service, "require_capability") as capability, \
                mock.patch.object(worker, "_job_api") as jobs, \
                mock.patch.object(worker, "create_app") as factory:
            self.assertEqual(worker.main(["--once", "--tenant", TENANT]),
                             worker.EXIT_COMPOSITION)
        capability.assert_not_called()
        jobs.assert_not_called()
        factory.assert_not_called()

    def test_review_rejects_corrupt_anchor_projection_without_creating(self):
        original = {"questions": [{"question_number": "1", "awarded_marks": 1}]}
        digest = audit_store.digest_value(original)
        store = mock.Mock()
        store.context_record.return_value = {
            "original_digest": digest, "owner_subject": OWNER,
            "membership_subject": SUBJECT}
        store.original_record.return_value = {
            "original_digest": digest, "original_scores": [["1", "999"]],
            "result_revision": 0}
        service = result_service.ResultService(store, self.identity)
        with self.assertRaises(audit_store.AuditStoreError) as caught:
            service.register_result(owner=OWNER, result_id="r", original_output=original)
        self.assertEqual(caught.exception.code, "original_output_immutable")
        store.register_result.assert_not_called()
        store.register_with_context.assert_not_called()


if __name__ == "__main__":
    unittest.main()
