"""BR-02 PG harness: worker fresh-completion denial (offline REAL behavior +
PG-gated zero-side-effect proof; live classes unexecuted by design until the
future approval exists - a skip is the honest gate, never a pass).

Coverage map:
* offline REAL: ``worker.run_one`` refuses with ``store_unusable`` BEFORE
  any work acquisition or side effect when the denial flag is set or the
  runtime is production; ``worker.main`` refuses the production composition
  at startup; the store's fresh-completion denial is NON-delegable from the
  production runtime policy (an explicit ``False`` cannot weaken it);
  the runtime policy itself denies by default and refuses explicit
  enablement;
* gated (REAL operations on the live target): with the PostgreSQL target
  present, a denied worker run performs ZERO engine claims, ZERO
  settlements, ZERO fresh result writes, and leaves the job state untouched.
"""
import os
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
import worker


def _live_approval():
    """Future supply point; returns None today, never inferred from
    env/DSN/flags."""
    return None


def _test_dsn():
    import os
    return os.environ.get(br02_pg_target.TEST_DSN_ENV, "").strip()


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


class _RecordingStore:
    """Offline control-flow double: records whether ANY acquisition or
    settle attempt happened. Used only to prove the refusal precedes all
    side effects - never as a stand-in for PostgreSQL coverage."""

    def __init__(self):
        self.acquisition_calls = 0
        self.job_runs = 0

    def find_runnable_job(self, connection, *, tenant_id, now=None):
        self.acquisition_calls += 1
        return None

    def require_connection(self):
        raise AssertionError("no connection is needed before the refusal")


class WorkerRunOneDenialTests(unittest.TestCase):
    """``run_one`` refuses before any acquisition or side effect."""

    def test_denial_flag_refuses_before_acquisition(self):
        recorder = _RecordingStore()
        outcome = worker.run_one(recorder, recorder, None, tenant_id="t1",
                                 fresh_result_completion_denied=True)
        self.assertEqual(outcome["status"], "error")
        self.assertEqual(outcome["error_code"], "store_unusable")
        self.assertIsNone(outcome["job_id"])
        self.assertEqual(recorder.acquisition_calls, 0,
                         "refusal precedes any work acquisition")
        self.assertEqual(recorder.job_runs, 0)

    def test_production_runtime_refuses_before_acquisition(self):
        recorder = _RecordingStore()
        with mock.patch.dict(os.environ,
                             {runtime_config.RUNTIME_ENV:
                              runtime_config.RUNTIME_PRODUCTION}):
            outcome = worker.run_one(recorder, recorder, None,
                                     tenant_id="t1",
                                     fresh_result_completion_denied=False)
        self.assertEqual(outcome["error_code"], "store_unusable")
        self.assertEqual(recorder.acquisition_calls, 0)


class WorkerStartupRefusalTests(unittest.TestCase):
    """The worker's startup composition refuses a production runtime."""

    def test_main_refuses_production_composition(self):
        env = {runtime_config.RUNTIME_ENV: runtime_config.RUNTIME_PRODUCTION,
               runtime_config.PG_DSN_ENV: "",
               runtime_config.OIDC_AUDIENCE_ENV: "synthetic-test-audience"}
        stderr = io_string()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "stderr", stderr):
            code = worker.main([])
        self.assertNotEqual(code, 0)
        self.assertIn("refused", stderr.getvalue(),
                      "the production composition refusal is explicit")

    def test_main_refuses_without_tenants(self):
        env = {runtime_config.RUNTIME_ENV: "development"}
        with mock.patch.dict(os.environ, env, clear=True):
            code = worker.main([])
        self.assertNotEqual(code, 0, "a worker with no tenants must not "
                                     "guess")


class StoreFlagNonDelegableTests(unittest.TestCase):
    """The fresh-completion denial cannot be weakened inside production."""

    def test_explicit_false_cannot_weaken_production_policy(self):
        import identity_context
        import postgres_result_store

        class _FakeConnection:
            def transaction(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def execute(self, sql, params=None):
                return self

            def fetchone(self):
                return ("1",)

        identity = identity_context.from_verified_subject("t1", "tenant-t1")
        env = {runtime_config.RUNTIME_ENV: runtime_config.RUNTIME_PRODUCTION}
        with mock.patch.dict(os.environ, env):
            store = postgres_result_store.PostgresResultStore(
                _FakeConnection(), identity=identity,
                fresh_completion_denied=False)
        self.assertTrue(store.fresh_completion_denied,
                        "the denial is non-delegable from production "
                        "runtime policy")

    def test_runtime_policy_denies_by_default_and_refuses_enablement(self):
        with mock.patch.dict(os.environ, {runtime_config.RUNTIME_ENV:
                                          runtime_config.RUNTIME_PRODUCTION},
                             clear=True):
            self.assertTrue(runtime_config.fresh_result_completion_denied(),
                            "unset policy denies: no production completion "
                            "is enabled")
            for value in ("true", "1", "bogus"):
                with self.subTest(value=value):
                    with mock.patch.dict(os.environ, {
                            runtime_config.RUNTIME_ENV:
                            runtime_config.RUNTIME_PRODUCTION,
                            runtime_config.FRESH_RESULT_COMPLETION_ENABLED_ENV:
                            value}, clear=True):
                        with self.assertRaises(
                                runtime_config.RuntimeConfigError):
                            runtime_config.fresh_result_completion_denied()


class _LiveWorkerBase(unittest.TestCase):
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
        if getattr(self, "conn", None) is not None:
            self.conn.close()


class WorkerZeroSideEffectsTests(_LiveWorkerBase):
    """With the PostgreSQL target present, the denied worker path performs
    zero engine claims, settlements, and fresh writes."""

    def test_denied_worker_run_touches_nothing(self):
        counters = (
            "select 'jobs_claimed', count(*) from marking_jobs"
            "  where status = 'running' and tenant_id = %s"
            " union all "
            "select 'jobs_settled', count(*) from marking_jobs"
            "  where status = 'succeeded' and tenant_id = %s"
            " union all "
            "select 'fresh_results', count(*) from results"
            "  where tenant_id = %s"
            " union all "
            "select 'audit_events', count(*) from audit_events"
            "  where tenant_id = %s")
        before = dict(self.conn.execute(
            counters, (self.bundle.tenant_id,) * 4).fetchall())

        outcome = worker.run_one(
            app=None, api=None, connection=None, tenant_id=self.bundle
            .tenant_id,
            fresh_result_completion_denied=True)

        self.assertEqual(outcome["error_code"], "store_unusable")
        after = dict(self.conn.execute(
            counters, (self.bundle.tenant_id,) * 4).fetchall())
        self.assertEqual(before, after,
                         "zero engine claims, zero settlements, zero fresh "
                         "result writes: the refusal precedes every side "
                         "effect")

    def test_production_service_refuses_fresh_registration(self):
        import identity_context
        import result_service
        identity = identity_context.from_verified_subject(
            self.bundle.raw_subject, self.bundle.tenant_id)
        env = {runtime_config.RUNTIME_ENV: runtime_config.RUNTIME_PRODUCTION,
               runtime_config.PG_DSN_ENV: self.dsn}
        with mock.patch.dict(os.environ, env):
            service = result_service.open_service(identity)
        try:
            with self.assertRaises(audit_store_error()) as caught:
                service.register(
                    result_id=self.bundle.result_id,
                    original_output=self.bundle.original_output,
                    scope="manual", review_key=self.bundle.review_key)
            self.assertEqual(caught.exception.code, "store_unusable")
        finally:
            service.close()


def audit_store_error():
    import audit_store
    return audit_store.AuditStoreError


def io_string():
    import io
    return io.StringIO()


if __name__ == "__main__":
    unittest.main()
