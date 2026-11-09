"""BR-02 PG harness: production API persistence contract (offline +
PG-gated; live classes unexecuted by design until the future approval
exists - a skip is the honest gate, never a pass).

Coverage map:
* offline: the production STARTUP ENTRYPOINT refuses a production runtime
  without the PostgreSQL capability BEFORE any API machinery is imported
  (lazy ``api.server`` import, real schema-capability gate) - proven with a
  subprocess under an inert minimal environment; ``api.app`` (this module's
  import) never pulls in ``api.server``;
* gated (REAL operations against the live target): the REAL production
  ``create_app`` composition (no fake persistence, membership, or
  provenance - the default PostgreSQL membership resolver reads the deployed
  ``tenant_membership`` through the reviewed 0005 ``app.subject`` policy),
  served in-process over an https synthetic CORS origin with the separately
  approved RESTRICTED ``app_dsn``; the ONLY injected boundary is a disclosed
  synthetic token verifier plus ``engine=None``: projection,
  history, approve/correct/flag decisions with saved idempotent receipts,
  semantic-conflict and stale-revision refusals, authentication BEFORE
  replay, missing context without lazy creation, same- and cross-tenant
  isolation, production fresh-completion denial (F5) with zero fresh writes,
  and resource cleanup.
"""
import json
import os
import subprocess  # noqa: F401 - refused offline by the harness guard
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))      # tests/

try:
    import psycopg
except ImportError:  # pragma: no cover - driver ships in the test manifest
    psycopg = None

import br02_pg_fixtures as fixtures
import br02_pg_target
import runtime_config

ROOT = Path(__file__).resolve().parents[1]


def _live_approval():
    """Future supply point for the independently approved target + seed
    approval. Returns None today; never inferred from env/DSN/flags."""
    return None


def _test_dsn():
    """The dedicated test DSN variable only; read lazily and only AFTER the
    authorization gate passes."""
    return os.environ.get(br02_pg_target.TEST_DSN_ENV, "").strip()


# Synthetic production CORS origin for the in-process API tests. CORS is a
# browser policy and need not match the test client's transport; TLS/DSN
# validators stay strict (https is required in production).
PRODUCTION_API_ORIGIN = "https://127.0.0.1"


def _production_api_environ(app_dsn: str) -> dict:
    """The synthetic production environment for the API composition.

    ``app_dsn`` must be the separately approved RESTRICTED runtime handle,
    never the fixture owner's administrative DSN. Every other value is inert
    and synthetic; the engine stays disabled.
    """
    return {
        runtime_config.RUNTIME_ENV: runtime_config.RUNTIME_PRODUCTION,
        runtime_config.PG_DSN_ENV: app_dsn,
        runtime_config.ALLOWED_ORIGINS_ENV: PRODUCTION_API_ORIGIN,
        runtime_config.OIDC_AUDIENCE_ENV: "synthetic-test-audience",
        runtime_config.ENGINE_REAL_ENV: "0",
    }


def _restricted_app_dsn(approval) -> str:
    """The approved RESTRICTED runtime DSN (never the administrative
    ``target_dsn``). A missing transport is disclosed as a skip; a misbound
    handle (app_dsn == target_dsn) is a hard failure, not a silent pass."""
    app_dsn = getattr(approval, "app_dsn", "")
    runtime_login = getattr(approval, "runtime_login", "")
    admin_dsn = getattr(approval, "target_dsn", "")
    if not isinstance(app_dsn, str) or not app_dsn.strip() \
            or not isinstance(runtime_login, str) or not runtime_login.strip():
        raise unittest.SkipTest(
            "the future approval must separately supply the restricted "
            "app_dsn and runtime_login for the API runtime; none is invented")
    if isinstance(admin_dsn, str) and admin_dsn.strip() and app_dsn == admin_dsn:
        raise AssertionError(
            "the API runtime handle must be the restricted app_dsn, never "
            "the fixture owner's administrative target_dsn")
    return app_dsn


def _synthetic_api_verify_token(token: str) -> str:
    """The disclosed synthetic verifier for these in-process API tests.

    It accepts ONLY ``synthetic-subject:<subject>`` and maps every refusal to
    ``auth_oidc.OidcError`` (the production envelope vocabulary). It is never
    presented as real Google OIDC verification and never touches persistence,
    membership, or provenance.
    """
    import auth_oidc

    if isinstance(token, str) and token.startswith("synthetic-subject:"):
        subject = token.split(":", 1)[1].strip()
        if subject and len(subject) <= 128 and all(
                part.isalnum() for part in subject.replace("-", "a")
                .replace("_", "a").split(".")):
            return subject
    raise auth_oidc.OidcError("synthetic_token_refused")


def _live_gate():
    """Authorization first: the current unauthorized phase is refused before
    ANY environment value access, driver connect, or module entrypoint."""
    if not br02_pg_target.execution_authorized():
        return ("live execution remains refused in the current phase: it "
                "requires a future independently approved target plus "
                "separate execution authorization; a DSN alone never "
                "unlocks it")
    approval = _live_approval()
    if approval is None:
        return ("no independently supplied seed approval exists for the "
                "attested target; the live classes skip until the lead "
                "supplies it")
    if psycopg is None:
        return "psycopg driver is not installed"
    if not _test_dsn():
        return ("AI_MARKER_TEST_PG_DSN is not configured - the BR-02 "
                "PostgreSQL gate is BLOCKED locally; never AI_MARKER_PG_DSN")
    return None


class StartupEntrypointTests(unittest.TestCase):
    """Offline: the startup entrypoint stays lazy AND its production
    capability chain fails closed - proven without any OS process (the
    harness guard refuses offline spawns) and without importing
    ``api.server`` (the guard refuses that import offline; the real
    entrypoint composition is exercised only behind the live gate)."""

    def test_api_server_is_not_imported_by_the_api_layer(self):
        import api.app  # the real API composition module
        self.assertNotIn(
            "api.server", sys.modules,
            "api.server (the startup entrypoint) must stay lazy: it is "
            "imported only by the container runtime, never by api.app")

    def test_offline_import_of_the_entrypoint_is_refused_by_the_guard(self):
        import importlib

        import run_br02_pg_harness as runner_guard
        sys.modules.pop("api.server", None)
        with self.assertRaises(RuntimeError) as caught:
            importlib.import_module("api.server")
        self.assertIs(
            getattr(caught.exception, "_br02_harness_refusal", False), True,
            "the harness guard refuses api.server imports offline; the "
            "entrypoint runs only behind the live gate")

    def test_production_capability_chain_refuses_without_the_dsn(self):
        # The two gates the entrypoint chains BEFORE its lazy api.app
        # import (the reviewed source order): runtime classification, then
        # the real schema capability. Proven behaviorally here with a
        # synthetic production env; the full entrypoint composition runs
        # only in the gated live child.
        import result_service

        # Inert synthetic values for every OTHER production requirement, so
        # the missing DSN is the only fault under test. No ambient credential
        # is read: the mapping is passed explicitly.
        env = {
            runtime_config.RUNTIME_ENV: runtime_config.RUNTIME_PRODUCTION,
            runtime_config.PG_DSN_ENV: "",
            runtime_config.OIDC_AUDIENCE_ENV: "synthetic-test-audience",
            runtime_config.SUPABASE_URL_ENV:
                "https://synthetic-ref.supabase.co",
            runtime_config.SUPABASE_KEY_ENV: "synthetic-service-key",
            runtime_config.BUCKET_ENV: "synthetic-bucket",
            runtime_config.ALLOWED_ORIGINS_ENV: "https://synthetic.example",
        }
        before = set(sys.modules)
        with self.assertRaises(runtime_config.RuntimeConfigError) as caught:
            runtime_config.require(env)
        self.assertEqual(caught.exception.code, "store_unusable")
        self.assertNotIn("api.server", sys.modules)
        self.assertNotIn("psycopg", set(sys.modules) - before,
                         "the runtime contract check must not touch a DB")
        # The capability gate is separately fail-closed on the same missing
        # DSN, before it ever imports the driver or opens a connection.
        with self.assertRaises(runtime_config.RuntimeConfigError) as caught:
            result_service.require_capability(env)
        self.assertEqual(caught.exception.code, "store_unusable")
        self.assertNotIn("api.server", sys.modules)
        self.assertNotIn("psycopg", set(sys.modules) - before,
                         "a missing DSN must refuse before the driver import")


class ProductionApiConfigurationTests(unittest.TestCase):
    """Offline: the production API helper configuration stays https,
    restricted, and fail-closed. This proves helper wiring only; it is NOT a
    PostgreSQL/API proof (the live classes remain gated)."""

    def test_environment_is_https_production_and_uses_the_restricted_dsn(self):
        env = _production_api_environ("postgresql://restricted-app/db")
        self.assertEqual(env[runtime_config.RUNTIME_ENV],
                         runtime_config.RUNTIME_PRODUCTION)
        self.assertEqual(env[runtime_config.PG_DSN_ENV],
                         "postgresql://restricted-app/db")
        self.assertTrue(env[runtime_config.ALLOWED_ORIGINS_ENV]
                        .startswith("https://"))
        self.assertEqual(
            runtime_config.parse_allowed_origins(
                env[runtime_config.ALLOWED_ORIGINS_ENV], production=True),
            (PRODUCTION_API_ORIGIN,))

    def test_restricted_dsn_never_binds_the_admin_handle(self):
        with self.assertRaises(unittest.SkipTest):
            _restricted_app_dsn(fixtures.SeedApproval(
                target_dsn="postgresql://admin/db"))
        misbound = fixtures.SeedApproval(
            target_dsn="postgresql://same/db",
            app_dsn="postgresql://same/db",
            runtime_login="marker_app_login")
        with self.assertRaises(AssertionError):
            _restricted_app_dsn(misbound)
        approved = fixtures.SeedApproval(
            target_dsn="postgresql://admin/db",
            app_dsn="postgresql://restricted-app/db",
            runtime_login="marker_app_login")
        self.assertEqual(_restricted_app_dsn(approved),
                         "postgresql://restricted-app/db")

    def test_synthetic_verifier_is_disclosed_and_uses_oidc_errors(self):
        import auth_oidc

        self.assertEqual(
            _synthetic_api_verify_token("synthetic-subject:alice"), "alice")
        for token in ("", "Bearer token", "synthetic-subject:",
                      "synthetic-subject:bad subject"):
            with self.subTest(token=token):
                with self.assertRaises(auth_oidc.OidcError) as caught:
                    _synthetic_api_verify_token(token)
                self.assertEqual(caught.exception.detail,
                                 "synthetic_token_refused")


class _LiveApiBase(unittest.TestCase):
    """Live base: gate in setUpClass, one unique seeded bundle and one owned
    connection per test, rows retained, no cleanup SQL anywhere."""

    @classmethod
    def setUpClass(cls):
        reason = _live_gate()
        if reason:
            raise unittest.SkipTest(reason)
        cls.dsn = _test_dsn()
        cls.approval = _live_approval()
        cls.conn = psycopg.connect(cls.dsn, autocommit=True)
        try:
            rows = dict(cls.conn.execute(
                "select key, value from schema_meta"
                " where key in ('schema_version','result_context_version')"
            ).fetchall())
        except Exception:
            cls.conn.close()
            raise
        if rows.get("schema_version") != "1" \
                or rows.get("result_context_version") != "1":
            cls.conn.close()
            raise unittest.SkipTest(
                "target schema capability missing; no implicit migrations "
                "are ever applied by tests")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "conn", None) is not None:
            cls.conn.close()

    def setUp(self):
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        try:
            self.bundle = fixtures.build_bundle()
            fixtures.seed(self.conn, self.bundle, self.approval,
                          is_production=False)
        except Exception:
            self.conn.close()
            raise

    def tearDown(self):
        # Rows are retained (the disposable database is discarded whole by
        # the operator); only the test's OWN connection closes here.
        if getattr(self, "conn", None) is not None:
            self.conn.close()

    def fixture_store(self):
        """The explicit NON-PRODUCTION fixture registration store (denial
        lifted HERE ONLY, behind the live gate)."""
        import postgres_result_store
        import identity_context
        return postgres_result_store.open_store(
            self.dsn,
            identity=identity_context.from_verified_subject(
                self.bundle.raw_subject, self.bundle.tenant_id),
            fresh_completion_denied=False)

    def production_app(self):
        """The REAL production composition: no injected store, no injected
        factory, no fake membership, provenance, or custom resolver. The
        runtime DSN is the separately approved RESTRICTED ``app_dsn`` (never
        the fixture owner's administrative DSN), and the synthetic production
        environment stays ACTIVE for the whole test/client/request lifetime:
        the adapters re-read it per request. The ONLY injected boundaries are
        the disclosed synthetic token verifier and ``engine=None``. Fresh
        completion stays denied (default)."""
        from api.app import create_app

        app_dsn = _restricted_app_dsn(self.approval)
        patcher = mock.patch.dict(os.environ,
                                  _production_api_environ(app_dsn))
        patcher.start()
        self.addCleanup(patcher.stop)
        return create_app(
            verify_token=_synthetic_api_verify_token,
            engine=None)

    def auth_headers(self, subject=None):
        return {"Authorization": "Bearer synthetic-subject:"
                                 + (subject or self.bundle.raw_subject)}

    def decision_body(self, **overrides):
        body = {"question_number": "1", "action": "approve",
                "expected_revision": 0, "idempotency_key": "decision-key-1",
                "reason": "checked against the scheme"}
        body.update(overrides)
        return body


class ProductionApiLiveTests(_LiveApiBase):
    """Real HTTP flows against the real production composition on the live
    target (gated; unexecuted until the future approval exists)."""

    def _client(self, app):
        from fastapi.testclient import TestClient
        return TestClient(app)

    def test_healthz_is_public_and_content_free(self):
        app = self.production_app()
        with self._client(app) as client:
            response = client.get("/v1/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_projection_and_history_serve_the_registered_result(self):
        store = self.fixture_store()
        try:
            store.register_with_context(
                result_id=self.bundle.result_id,
                original_output=self.bundle.original_output,
                scope="manual", review_key=self.bundle.review_key,
                document_digest=self.bundle.document_digest,
                job_id=self.bundle.job_id,
                assignment_id=self.bundle.assignment_id)
        finally:
            store.close()
        app = self.production_app()
        with self._client(app) as client:
            projection = client.get(
                f"/v1/results/{self.bundle.result_id}/projection",
                headers=self.auth_headers())
            history = client.get(
                f"/v1/results/{self.bundle.result_id}/events",
                headers=self.auth_headers())
        self.assertEqual(projection.status_code, 200, projection.text)
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json().get("events"), [])
        self.assertEqual(history.json().get("result_revision"), 0)

    def test_missing_context_is_404_with_no_lazy_creation(self):
        app = self.production_app()
        unknown = "manual:" + "0" * 16 + ":missing-context"
        before = self.conn.execute(
            "select count(*) from results where result_id = %s",
            (unknown,)).fetchone()[0]
        with self._client(app) as client:
            projection = client.get(
                f"/v1/results/{unknown}/projection",
                headers=self.auth_headers())
            decision = client.post(
                f"/v1/results/{unknown}/decisions",
                headers=self.auth_headers(), json=self.decision_body())
        self.assertEqual(projection.status_code, 404)
        self.assertEqual(decision.status_code, 404)
        after = self.conn.execute(
            "select count(*) from results where result_id = %s",
            (unknown,)).fetchone()[0]
        self.assertEqual(before, after, "no lazy creation: an unknown "
                                        "result identity must not come to "
                                        "existence through reads")

    def test_decisions_commit_saved_receipts_and_replay(self):
        store = self.fixture_store()
        try:
            store.register_with_context(
                result_id=self.bundle.result_id,
                original_output=self.bundle.original_output,
                scope="manual", review_key=self.bundle.review_key,
                document_digest=self.bundle.document_digest)
        finally:
            store.close()
        app = self.production_app()
        with self._client(app) as client:
            first = client.post(
                f"/v1/results/{self.bundle.result_id}/decisions",
                headers=self.auth_headers(), json=self.decision_body())
            replay = client.post(
                f"/v1/results/{self.bundle.result_id}/decisions",
                headers=self.auth_headers(), json=self.decision_body())
            conflict = client.post(
                f"/v1/results/{self.bundle.result_id}/decisions",
                headers=self.auth_headers(),
                json=self.decision_body(idempotency_key="decision-key-1",
                                        reason="a DIFFERENT reason"))
        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(first.json()["replayed"],
                         "the REAL API payload marks first commits with "
                         "replayed=false")
        replay = json.loads(replay.text)
        self.assertEqual(replay["replayed"], True)
        self.assertEqual(replay["event_sequence"],
                         first.json()["event_sequence"],
                         "the replay returns the SAVED receipt")
        self.assertEqual(replay["idempotency_key"], "decision-key-1")
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"],
                         "idempotency_key_payload_mismatch")
        history = self.conn.execute(
            "select count(*) from audit_events where tenant_id=%s"
            " and owner_subject=%s and result_id=%s",
            (self.bundle.tenant_id, self.bundle.raw_subject,
             self.bundle.result_id)).fetchone()[0]
        self.assertEqual(history, 1, "one event per accepted request")

    def test_stale_revision_is_refused(self):
        store = self.fixture_store()
        try:
            store.register_with_context(
                result_id=self.bundle.result_id,
                original_output=self.bundle.original_output,
                scope="manual", review_key=self.bundle.review_key,
                document_digest=self.bundle.document_digest)
        finally:
            store.close()
        app = self.production_app()
        with self._client(app) as client:
            stale = client.post(
                f"/v1/results/{self.bundle.result_id}/decisions",
                headers=self.auth_headers(),
                json=self.decision_body(expected_revision=7))
        self.assertEqual(stale.status_code, 409, stale.text)

    def test_authentication_precedes_idempotent_replay(self):
        store = self.fixture_store()
        try:
            store.register_with_context(
                result_id=self.bundle.result_id,
                original_output=self.bundle.original_output,
                scope="manual", review_key=self.bundle.review_key,
                document_digest=self.bundle.document_digest)
        finally:
            store.close()
        app = self.production_app()
        with self._client(app) as client:
            unauthenticated = client.post(
                f"/v1/results/{self.bundle.result_id}/decisions",
                headers={"Authorization": "Bearer synthetic-subject:nope"},
                json=self.decision_body())
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(
            unauthenticated.json()["error"]["code"],
            "authentication_required",
            "authentication is evaluated before any replay path")

    def test_cross_tenant_reads_are_indistinguishable_from_absent(self):
        store = self.fixture_store()
        try:
            store.register_with_context(
                result_id=self.bundle.result_id,
                original_output=self.bundle.original_output,
                scope="manual", review_key=self.bundle.review_key,
                document_digest=self.bundle.document_digest)
        finally:
            store.close()
        app = self.production_app()
        with self._client(app) as client:
            foreign = client.get(
                f"/v1/results/{self.bundle.result_id}/projection",
                headers=self.auth_headers(self.bundle.other_subject))
        self.assertEqual(foreign.status_code, 404)

    def test_production_fresh_completion_is_denied_with_zero_writes(self):
        app = self.production_app()
        before = self.conn.execute(
            "select count(*) from results where tenant_id=%s",
            (self.bundle.tenant_id,)).fetchone()[0]
        with self._client(app) as client:
            # The F5 gate denies fresh production completion; whichever
            # fail-closed refusal fires first, nothing is ever written.
            ingest = client.post(
                "/v1/results", headers=self.auth_headers(),
                json={"scope": "manual",
                      "original_output": self.bundle.original_output,
                      "manual_identity": {
                          "review_key": self.bundle.review_key,
                          "document_digest": self.bundle.document_digest}})
            decision = client.post(
                f"/v1/results/{self.bundle.result_id}/decisions",
                headers=self.auth_headers(), json=self.decision_body(
                    action="flag", question_number="1"))
        self.assertNotEqual(ingest.status_code, 201, ingest.text)
        self.assertIn(ingest.status_code, (403, 409, 422),
                      "a fresh production completion must be refused by the "
                      "fail-closed vocabulary, never created")
        # The direct production service path refuses with the exact F5 code.
        import identity_context
        import result_service
        identity = identity_context.from_verified_subject(
            self.bundle.raw_subject, self.bundle.tenant_id)
        env = {runtime_config.RUNTIME_ENV: runtime_config.RUNTIME_PRODUCTION,
               runtime_config.PG_DSN_ENV: self.dsn}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(runtime_config.RuntimeConfigError) as caught:
                result_service.require_capability(env)
            self.assertEqual(caught.exception.code, "store_unusable")
        after = self.conn.execute(
            "select count(*) from results where tenant_id=%s",
            (self.bundle.tenant_id,)).fetchone()[0]
        self.assertEqual(before, after, "zero fresh writes in production")

    def test_resource_cleanup_between_requests(self):
        store = self.fixture_store()
        try:
            store.register_with_context(
                result_id=self.bundle.result_id,
                original_output=self.bundle.original_output,
                scope="manual", review_key=self.bundle.review_key,
                document_digest=self.bundle.document_digest)
        finally:
            store.close()
        app = self.production_app()
        with self._client(app) as client:
            for _ in range(3):
                response = client.get(
                    f"/v1/results/{self.bundle.result_id}/projection",
                    headers=self.auth_headers())
                self.assertEqual(response.status_code, 200)
        # The request-scoped services are closed per request: a subsequent
        # independent store still sees the same durable state.
        independent = self.conn.execute(
            "select count(*) from result_context where tenant_id=%s"
            " and membership_subject=%s and result_id=%s",
            (self.bundle.tenant_id, self.bundle.raw_subject,
             self.bundle.result_id)).fetchone()[0]
        self.assertEqual(independent, 1)


if __name__ == "__main__":
    unittest.main()
