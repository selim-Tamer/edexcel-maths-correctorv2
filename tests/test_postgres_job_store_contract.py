"""PostgreSQL job-store contract tests (CI service container; local skip).

Two halves, so a regression cannot hide:

* ``OpenStoreConnectionKwargsTests`` runs WITHOUT any server (offline): the
  opener must pass ``prepare_threshold=None`` for the Supabase transaction
  pooler and must NOT auto-create the 0003 schema (fail closed, like
  ``postgres_store``). The dev SQLite store creates its schema; this port
  deliberately does not.
* ``PostgresJobStoreContractTests`` runs ONLY with a DSN (CI
  ``postgres-tests``), against an isolated ``marker_jobs_ci`` database and the
  digest-pinned service container. It applies 0001 -> 0002 -> 0003 -> 0003
  again (idempotency), then pins the ported lease/attempt semantics, the RLS
  binding, and the zero-DELETE grant.

Locally these skip with an explicit reason; that skip IS the honest label,
never a pass. The real Supabase pgBouncer transaction pooler is NOT exercised
here - nothing in this file claims a pooled connection works.
"""
import contextlib
import os
import secrets
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

try:
    import psycopg
    from psycopg import conninfo
    from psycopg import errors as pg_errors
    from psycopg import sql as sqlmod
except ImportError:  # pragma: no cover - driver ships in the test manifest
    psycopg = None

import postgres_job_store
from job_store import JobStoreError

DSN = os.environ.get("AI_MARKER_TEST_PG_DSN", "").strip()
ROOT = Path(__file__).resolve().parents[1]
TEST_DB = "marker_jobs_ci"

TENANT = "school-jobs-a"
OTHER_TENANT = "school-jobs-b"
OWNER = "google:teacher-a"
OTHER_OWNER = "google:teacher-b"

APP_ROLE = "marker_jobs_app_ci"
APP_PASSWORD = secrets.token_hex(24)

requires_pg = unittest.skipUnless(
    psycopg is not None and DSN,
    "PostgreSQL DSN not configured - the job-store contract runs in the CI "
    "postgres-tests job only")


def _dsn(database: str = TEST_DB, *, user: str = "", password: str = "") -> str:
    overrides = {"dbname": database}
    if user:
        overrides.update(user=user, password=password)
    return conninfo.make_conninfo(DSN, **overrides)


def _execute_sql_file(connection, name: str) -> None:
    connection.execute((ROOT / "schema" / name).read_text(encoding="utf-8"))


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConnection:
    """Minimal psycopg connection stand-in for ``open_store`` validation."""

    def __init__(self, jobs_version="1", table="marking_jobs"):
        self._answers = [_FakeResult((jobs_version,)),
                         _FakeResult((table,))]

    @contextlib.contextmanager
    def transaction(self):
        yield self

    def execute(self, *_args, **_kwargs):
        return self._answers.pop(0)

    def close(self):
        pass


class OpenStoreConnectionKwargsTests(unittest.TestCase):
    """Offline pins: pooler-safe connect kwargs, and no silent auto-DDL."""

    def _open_with_fake(self, fake_connection):
        calls = {}
        fake = types.ModuleType("psycopg")

        def connect(dsn, **kwargs):
            calls["dsn"] = dsn
            calls["kwargs"] = kwargs
            return fake_connection

        fake.connect = connect
        with mock.patch.dict(sys.modules, {"psycopg": fake}):
            store = postgres_job_store.open_store(
                "  postgresql://pooler.example:6543/postgres  ")
        return calls, store

    def test_open_store_disables_prepared_statements(self):
        calls, store = self._open_with_fake(_FakeConnection())
        self.assertEqual(calls["dsn"],
                         "postgresql://pooler.example:6543/postgres")
        self.assertIn("prepare_threshold", calls["kwargs"])
        self.assertIsNone(calls["kwargs"]["prepare_threshold"])
        self.assertIsNotNone(store)

    def test_open_store_refuses_a_schema_it_did_not_find(self):
        for version, table in (("1", None), (None, "marking_jobs"),
                               ("2", "marking_jobs")):
            with self.subTest(version=version, table=table):
                with self.assertRaises(JobStoreError) as caught:
                    self._open_with_fake(
                        _FakeConnection(jobs_version=version, table=table))
                self.assertEqual(caught.exception.detail, "store_unusable")

    def test_open_store_refuses_an_empty_dsn(self):
        for bad in ("", "   ", None):
            with self.subTest(dsn=bad):
                with self.assertRaises(JobStoreError) as caught:
                    postgres_job_store.open_store(bad)
                self.assertEqual(caught.exception.detail, "store_unusable")


@requires_pg
class PostgresJobStoreContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = psycopg.connect(DSN, autocommit=True)
        cls.admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (TEST_DB,))
        cls.admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        cls.admin.execute(f'CREATE DATABASE "{TEST_DB}"')
        cls.admin.execute(sqlmod.SQL("DROP ROLE IF EXISTS {}").format(
            sqlmod.Identifier(APP_ROLE)))
        cls.admin.execute(sqlmod.SQL(
            "CREATE ROLE {} LOGIN PASSWORD {}").format(
                sqlmod.Identifier(APP_ROLE), sqlmod.Literal(APP_PASSWORD)))
        with psycopg.connect(_dsn(), autocommit=True) as setup:
            _execute_sql_file(setup, "postgres_v1.sql")
            _execute_sql_file(setup, "postgres_v2_tenant_rls.sql")
            _execute_sql_file(setup, "postgres_v3_jobs.sql")
            # Idempotent: a second apply must not fail.
            _execute_sql_file(setup, "postgres_v3_jobs.sql")
            setup.execute(sqlmod.SQL("GRANT marker_app TO {}").format(
                sqlmod.Identifier(APP_ROLE)))
            # Seed as the migration/owner role (superuser bypasses RLS).
            setup.execute(
                "INSERT INTO tenants (tenant_id) VALUES (%s), (%s)",
                (TENANT, OTHER_TENANT))
            setup.execute(
                "INSERT INTO tenant_membership (tenant_id, subject, active)"
                " VALUES (%s, %s, TRUE), (%s, %s, TRUE)",
                (TENANT, OWNER, OTHER_TENANT, OTHER_OWNER))

    @classmethod
    def tearDownClass(cls):
        # Drop our login role and its membership: the RLS suite's own backout
        # refuses to drop a group role that still has dependents.
        cls.admin.execute(sqlmod.SQL("REVOKE marker_app FROM {}").format(
            sqlmod.Identifier(APP_ROLE)))
        cls.admin.execute(sqlmod.SQL("DROP ROLE IF EXISTS {}").format(
            sqlmod.Identifier(APP_ROLE)))
        cls.admin.close()

    def setUp(self):
        self.connection = postgres_job_store.open_store(_dsn())
        self.addCleanup(self.connection.close)
        with self.connection.transaction():
            self.connection.execute("DELETE FROM marking_jobs")

    # -- identity and idempotency ----------------------------------------
    def test_create_is_idempotent_and_refuses_retargeting(self):
        created = postgres_job_store.create_job(
            self.connection, tenant_id=TENANT, owner=OWNER,
            idempotency_key="key-1", document_digest="a" * 64)
        self.assertTrue(created["created"])
        replay = postgres_job_store.create_job(
            self.connection, tenant_id=TENANT, owner=OWNER,
            idempotency_key="key-1", document_digest="a" * 64)
        self.assertFalse(replay["created"])
        self.assertEqual(replay["job_id"], created["job_id"])
        with self.assertRaises(JobStoreError) as caught:
            postgres_job_store.create_job(
                self.connection, tenant_id=TENANT, owner=OWNER,
                idempotency_key="key-1", document_digest="b" * 64)
        self.assertEqual(caught.exception.detail,
                         "idempotency_key_payload_mismatch")

    def test_get_job_is_owner_and_tenant_scoped(self):
        job = postgres_job_store.create_job(
            self.connection, tenant_id=TENANT, owner=OWNER,
            idempotency_key="key-2", document_digest="a" * 64)
        self.assertIsNotNone(postgres_job_store.get_job(
            self.connection, tenant_id=TENANT, owner=OWNER,
            job_id=job["job_id"]))
        for tenant, owner in ((TENANT, OTHER_OWNER), (OTHER_TENANT, OWNER)):
            with self.subTest(tenant=tenant, owner=owner):
                self.assertIsNone(postgres_job_store.get_job(
                    self.connection, tenant_id=tenant, owner=owner,
                    job_id=job["job_id"]))

    def test_find_authorizable_job_only_returns_succeeded(self):
        job = postgres_job_store.create_job(
            self.connection, tenant_id=TENANT, owner=OWNER,
            idempotency_key="key-3", document_digest="a" * 64)
        self.assertIsNone(postgres_job_store.find_authorizable_job(
            self.connection, tenant_id=TENANT, owner=OWNER,
            document_digest="a" * 64))
        postgres_job_store.succeed_job(
            self.connection, tenant_id=TENANT, owner=OWNER,
            job_id=job["job_id"], result_id="result-1")
        self.assertEqual(postgres_job_store.find_authorizable_job(
            self.connection, tenant_id=TENANT, owner=OWNER,
            document_digest="a" * 64), job["job_id"])

    # -- lease and attempts ----------------------------------------------
    def test_claim_sets_a_lease_and_never_steals_a_live_one(self):
        job = postgres_job_store.create_job(
            self.connection, tenant_id=TENANT, owner=OWNER,
            idempotency_key="key-4", document_digest="a" * 64)
        claimed_job, claimed = postgres_job_store.claim_job(
            self.connection, tenant_id=TENANT, owner=OWNER,
            job_id=job["job_id"], now=1000, lease_seconds=60)
        self.assertTrue(claimed)
        self.assertEqual(claimed_job["status"], "running")
        self.assertEqual(claimed_job["attempts"], 1)
        self.assertEqual(claimed_job["lease_until"], 1060)
        # Inside the lease: not runnable, lease NOT stolen.
        stolen, ok = postgres_job_store.claim_job(
            self.connection, tenant_id=TENANT, owner=OWNER,
            job_id=job["job_id"], now=1030, lease_seconds=60)
        self.assertFalse(ok)
        self.assertEqual(stolen["status"], "running")
        # After expiry: crash recovery reclaims it and spends an attempt.
        reclaimed, ok = postgres_job_store.claim_job(
            self.connection, tenant_id=TENANT, owner=OWNER,
            job_id=job["job_id"], now=1100, lease_seconds=60)
        self.assertTrue(ok)
        self.assertEqual(reclaimed["attempts"], 2)

    def test_claim_of_an_unknown_job_is_not_an_error(self):
        job, ok = postgres_job_store.claim_job(
            self.connection, tenant_id=TENANT, owner=OWNER,
            job_id="job:missing")
        self.assertIsNone(job)
        self.assertFalse(ok)

    def test_settle_failure_requeues_then_fails_after_max_attempts(self):
        job = postgres_job_store.create_job(
            self.connection, tenant_id=TENANT, owner=OWNER,
            idempotency_key="key-5", document_digest="a" * 64,
            max_attempts=2)
        postgres_job_store.claim_job(
            self.connection, tenant_id=TENANT, owner=OWNER,
            job_id=job["job_id"], now=1000)
        self.assertEqual(postgres_job_store.settle_failure(
            self.connection, tenant_id=TENANT, owner=OWNER,
            job_id=job["job_id"], error_code="model_unavailable"), "queued")
        postgres_job_store.claim_job(
            self.connection, tenant_id=TENANT, owner=OWNER,
            job_id=job["job_id"], now=2000)
        self.assertEqual(postgres_job_store.settle_failure(
            self.connection, tenant_id=TENANT, owner=OWNER,
            job_id=job["job_id"], error_code="model_unavailable"), "failed")
        stored = postgres_job_store.get_job(
            self.connection, tenant_id=TENANT, owner=OWNER,
            job_id=job["job_id"])
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["error_code"], "model_unavailable")

    def test_settle_failure_of_an_unknown_job_is_refused(self):
        with self.assertRaises(JobStoreError) as caught:
            postgres_job_store.settle_failure(
                self.connection, tenant_id=TENANT, owner=OWNER,
                job_id="job:missing", error_code="store_unusable")
        self.assertEqual(caught.exception.detail, "result_not_registered")

    # -- RLS and least privilege -----------------------------------------
    def test_job_rows_are_bound_to_the_transaction_tenant(self):
        for tenant, key, digest in ((TENANT, "key-a", "a" * 64),
                                    (OTHER_TENANT, "key-b", "b" * 64)):
            postgres_job_store.create_job(
                self.connection, tenant_id=tenant, owner=OWNER,
                idempotency_key=key, document_digest=digest)
        app = psycopg.connect(
            _dsn(user=APP_ROLE, password=APP_PASSWORD), autocommit=True)
        self.addCleanup(app.close)
        # Unbound: fail closed, zero rows.
        self.assertEqual(app.execute(
            "SELECT count(*) FROM marking_jobs").fetchone()[0], 0)
        with app.transaction():
            app.execute("SELECT set_config('app.tenant_id', %s, true)",
                        (TENANT,))
            rows = app.execute(
                "SELECT tenant_id FROM marking_jobs").fetchall()
        self.assertEqual([row[0] for row in rows], [TENANT])
        # A cross-tenant write is refused by the WITH CHECK policy.
        with self.assertRaises(pg_errors.InsufficientPrivilege):
            with app.transaction():
                app.execute("SELECT set_config('app.tenant_id', %s, true)",
                            (TENANT,))
                app.execute(
                    "INSERT INTO marking_jobs (tenant_id, owner_subject,"
                    " job_id, idempotency_key, document_digest, status,"
                    " attempts, max_attempts, lease_until, created_at,"
                    " updated_at) VALUES (%s, %s, 'job:smuggled',"
                    " 'key-x', %s, 'queued', 0, 3, 0, 0, 0)",
                    (OTHER_TENANT, OWNER, "c" * 64))

    def test_runtime_roles_hold_no_delete_on_marking_jobs(self):
        postgres_job_store.create_job(
            self.connection, tenant_id=TENANT, owner=OWNER,
            idempotency_key="key-6", document_digest="a" * 64)
        app = psycopg.connect(
            _dsn(user=APP_ROLE, password=APP_PASSWORD), autocommit=True)
        self.addCleanup(app.close)
        with self.assertRaises(pg_errors.InsufficientPrivilege):
            app.execute("DELETE FROM marking_jobs")
        app.rollback()
        # The granted verbs still work: the app role can read its own row.
        with app.transaction():
            app.execute("SELECT set_config('app.tenant_id', %s, true)",
                        (TENANT,))
            self.assertEqual(app.execute(
                "SELECT count(*) FROM marking_jobs").fetchone()[0], 1)

    def test_zz_rollback_restores_the_pre_v3_shape_then_reapplies(self):
        with psycopg.connect(_dsn(), autocommit=True) as setup:
            _execute_sql_file(setup, "postgres_v3_jobs_rollback.sql")
            self.assertIsNone(setup.execute(
                "SELECT to_regclass('public.marking_jobs')").fetchone()[0])
            self.assertIsNone(setup.execute(
                "SELECT value FROM schema_meta WHERE key = 'jobs_version'"
            ).fetchone())
            # 0002's append-only audit guarantee survives the backout.
            grants = setup.execute(
                "SELECT grantee, privilege_type"
                " FROM information_schema.role_table_grants"
                " WHERE table_name = 'audit_events' AND grantee IN"
                " ('marker_app','marker_worker') ORDER BY 1,2").fetchall()
            self.assertEqual(grants, [
                ("marker_app", "INSERT"), ("marker_app", "SELECT"),
                ("marker_worker", "INSERT"), ("marker_worker", "SELECT")])
            # Re-apply so a later run starts from the hardened shape.
            _execute_sql_file(setup, "postgres_v3_jobs.sql")
            self.assertIsNotNone(setup.execute(
                "SELECT to_regclass('public.marking_jobs')").fetchone()[0])


class JobMigrationArtifactTests(unittest.TestCase):
    """Offline pins: the SQL artifacts cannot drift while PostgreSQL is absent."""

    def _sql(self, name: str) -> str:
        return (ROOT / "schema" / name).read_text(encoding="utf-8")

    def test_v3_grants_only_the_verbs_the_adapter_issues(self):
        sql = self._sql("postgres_v3_jobs.sql")
        grants = [line.strip() for line in sql.splitlines()
                  if line.strip().upper().startswith("GRANT")]
        self.assertEqual(
            [line for line in grants if "DELETE" in line.upper()], [],
            "a runtime role must not hold DELETE on marking_jobs")
        self.assertTrue(
            any("SELECT, INSERT, UPDATE ON marking_jobs" in line
                for line in grants),
            "the adapter needs exactly SELECT/INSERT/UPDATE on marking_jobs")

    def test_v3_enables_and_forces_rls_with_the_tenant_predicate(self):
        sql = self._sql("postgres_v3_jobs.sql")
        self.assertIn("ENABLE ROW LEVEL SECURITY", sql)
        self.assertIn("FORCE ROW LEVEL SECURITY", sql)
        self.assertIn("NULLIF(current_setting('app.tenant_id', true), '')", sql)

    def test_the_adapter_writes_no_ddl(self):
        # The dev SQLite store creates its schema; this port must fail closed
        # instead of silently migrating a managed database.
        source = Path(postgres_job_store.__file__).read_text(encoding="utf-8")
        self.assertNotIn("CREATE TABLE", source)
        self.assertIn("jobs_version", source)

    def test_rollback_drops_the_table_and_restores_the_audit_grants(self):
        sql = self._sql("postgres_v3_jobs_rollback.sql")
        self.assertIn("DROP TABLE IF EXISTS marking_jobs", sql)
        self.assertIn("GRANT SELECT, INSERT ON audit_events", sql)
        self.assertIn("drops every job row", sql)  # the data-loss disclosure

    def test_runbook_ledgers_0003_and_justifies_its_grants(self):
        runbook = (ROOT / "schema" / "MIGRATIONS.md").read_text(encoding="utf-8")
        self.assertIn("postgres_v3_jobs.sql", runbook)
        self.assertIn("`marking_jobs` (0003)", runbook)
        self.assertIn("jobs_version", runbook)


if __name__ == "__main__":
    unittest.main()
