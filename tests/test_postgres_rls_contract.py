"""RLS + least-privilege contract tests (CI service container; local skip).

These tests prove the hardening migration ``schema/postgres_v2_tenant_rls.sql``
and its rollback against a REAL PostgreSQL server, in an isolated throwaway
database so the v1 store contract tests are never affected.

Threats exercised
-----------------
* runtime role cannot escalate (no SUPERUSER/BYPASSRLS/CREATEDB/CREATEROLE);
* runtime role cannot run DDL or TRUNCATE;
* cross-tenant reads return NOTHING even when the query omits a tenant filter;
* cross-tenant INSERT is refused by the WITH CHECK policy;
* an unbound transaction (no ``app.tenant_id``) sees nothing - fail closed;
* the binding is transaction-local, so a pooled/reused connection cannot leak
  the previous tenant's scope into the next request;
* the worker role cannot DELETE;
* ``schema_meta`` is read-only at runtime;
* the migration is idempotent and the rollback restores the v1 shape.

The login roles created here are throwaway CI roles with random passwords;
no secret is stored or printed. Locally the module skips with an explicit
reason when no DSN is configured (the honest fallback label, never a pass).
"""
from __future__ import annotations

import os
import secrets
import unittest
from pathlib import Path

try:
    import psycopg
    from psycopg import conninfo
    from psycopg import sql
    from psycopg import errors as pg_errors
except ImportError:  # pragma: no cover - driver ships in the test manifest
    psycopg = None

DSN = os.environ.get("AI_MARKER_TEST_PG_DSN", "").strip()
ROOT = Path(__file__).resolve().parents[1]
TEST_DB = "marker_rls_ci"
APP_ROLE = "marker_rls_app_ci"
WORKER_ROLE = "marker_rls_worker_ci"
NOBODY_ROLE = "marker_rls_nobody_ci"
APP_PASSWORD = secrets.token_hex(24)
WORKER_PASSWORD = secrets.token_hex(24)
NOBODY_PASSWORD = secrets.token_hex(24)

TENANT = "school-a"
OTHER_TENANT = "school-b"
SUBJECT = "google:teacher-a"
OTHER_SUBJECT = "google:teacher-b"

requires_pg = unittest.skipUnless(
    psycopg is not None and DSN,
    "PostgreSQL DSN not configured - the RLS contract runs in the CI "
    "postgres-tests job only")


def _dsn(database: str = TEST_DB, *, user: str = "", password: str = "") -> str:
    overrides = {"dbname": database}
    if user:
        overrides.update(user=user, password=password)
    return conninfo.make_conninfo(DSN, **overrides)


def _execute_sql_file(connection, name: str) -> None:
    connection.execute((ROOT / "schema" / name).read_text(encoding="utf-8"))


@requires_pg
class TenantRlsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = psycopg.connect(DSN, autocommit=True)
        # Isolated database: never touch the database the v1 tests use.
        cls.admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (TEST_DB,))
        cls.admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        cls.admin.execute(f'CREATE DATABASE "{TEST_DB}"')
        for role, password in ((APP_ROLE, APP_PASSWORD),
                               (WORKER_ROLE, WORKER_PASSWORD),
                               (NOBODY_ROLE, NOBODY_PASSWORD)):
            cls.admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(
                sql.Identifier(role)))
            cls.admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)))

        with psycopg.connect(_dsn(), autocommit=True) as setup:
            _execute_sql_file(setup, "postgres_v1.sql")
            _execute_sql_file(setup, "postgres_v2_tenant_rls.sql")
            # Idempotent: a second apply must not fail.
            _execute_sql_file(setup, "postgres_v2_tenant_rls.sql")
            setup.execute(f'GRANT marker_app TO "{APP_ROLE}"')
            setup.execute(f'GRANT marker_worker TO "{WORKER_ROLE}"')
            # Seed as the migration/owner role (superuser bypasses RLS).
            setup.execute(
                "INSERT INTO tenants (tenant_id) VALUES (%s), (%s)",
                (TENANT, OTHER_TENANT))
            setup.execute(
                "INSERT INTO tenant_membership (tenant_id, subject) "
                "VALUES (%s, %s), (%s, %s)",
                (TENANT, SUBJECT, OTHER_TENANT, OTHER_SUBJECT))
            for tenant, subject, result in (
                    (TENANT, SUBJECT, "manual:tenant-a"),
                    (OTHER_TENANT, OTHER_SUBJECT, "manual:tenant-b")):
                setup.execute(
                    "INSERT INTO results (tenant_id, owner_subject, result_id,"
                    " original_digest, original_scores, original_scores_digest)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (tenant, subject, result, "d" * 64, "[]", "e" * 64))

    @classmethod
    def tearDownClass(cls):
        try:
            cls.admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (TEST_DB,))
            cls.admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        finally:
            for role in (APP_ROLE, WORKER_ROLE, NOBODY_ROLE):
                cls.admin.execute(f'DROP ROLE IF EXISTS "{role}"')
            cls.admin.close()

    # -- helpers ----------------------------------------------------------
    def _connect(self, role: str, password: str):
        connection = psycopg.connect(_dsn(user=role, password=password))
        self.addCleanup(connection.close)
        return connection

    def _bound(self, connection, tenant: str = TENANT):
        """Open a transaction bound to one tenant (the app's normal shape)."""
        return connection.transaction()

    # -- role attributes / DDL -------------------------------------------
    def test_runtime_roles_cannot_escalate(self):
        for role in ("marker_app", "marker_worker"):
            with self.subTest(role=role):
                with self.admin.cursor() as cur:
                    cur.execute(
                        "SELECT rolsuper, rolbypassrls, rolcreatedb,"
                        " rolcreaterole FROM pg_roles WHERE rolname = %s",
                        (role,))
                    row = cur.fetchone()
                self.assertEqual(row, (False, False, False, False))

    def test_runtime_role_cannot_run_ddl_or_truncate(self):
        connection = self._connect(APP_ROLE, APP_PASSWORD)
        for statement in ("CREATE TABLE forbidden (x int)",
                          "ALTER TABLE results ADD COLUMN leaked text",
                          "DROP TABLE results",
                          "TRUNCATE results"):
            with self.subTest(statement=statement):
                with self.assertRaises(pg_errors.InsufficientPrivilege):
                    connection.execute(statement)
                connection.rollback()

    # -- tenant isolation -------------------------------------------------
    def test_reads_only_see_the_bound_tenant(self):
        connection = self._connect(APP_ROLE, APP_PASSWORD)
        with connection.transaction():
            connection.execute("SELECT set_config('app.tenant_id', %s, true)",
                               (TENANT,))
            rows = connection.execute(
                "SELECT tenant_id FROM results").fetchall()
            self.assertEqual([row[0] for row in rows], [TENANT])
            # An explicit cross-tenant filter still returns nothing.
            rows = connection.execute(
                "SELECT tenant_id FROM results WHERE tenant_id = %s",
                (OTHER_TENANT,)).fetchall()
            self.assertEqual(rows, [])

    def test_cross_tenant_insert_is_refused(self):
        connection = self._connect(APP_ROLE, APP_PASSWORD)
        with self.assertRaises(pg_errors.InsufficientPrivilege):
            with connection.transaction():
                connection.execute(
                    "SELECT set_config('app.tenant_id', %s, true)", (TENANT,))
                connection.execute(
                    "INSERT INTO results (tenant_id, owner_subject, result_id,"
                    " original_digest, original_scores,"
                    " original_scores_digest) VALUES (%s, %s, %s, %s, %s, %s)",
                    (OTHER_TENANT, OTHER_SUBJECT, "manual:smuggled",
                     "a" * 64, "[]", "b" * 64))

    def test_unbound_transaction_fails_closed(self):
        connection = self._connect(APP_ROLE, APP_PASSWORD)
        with connection.transaction():
            self.assertEqual(
                connection.execute("SELECT count(*) FROM results").fetchone()[0],
                0)
        with self.assertRaises(pg_errors.InsufficientPrivilege):
            with connection.transaction():
                connection.execute(
                    "INSERT INTO results (tenant_id, owner_subject, result_id,"
                    " original_digest, original_scores,"
                    " original_scores_digest) VALUES (%s, %s, %s, %s, %s, %s)",
                    (TENANT, SUBJECT, "manual:unbound", "c" * 64, "[]",
                     "f" * 64))

    def test_binding_does_not_leak_across_pooled_transactions(self):
        # Same connection, two consecutive transactions: the setting is
        # transaction-local, so transaction 2 must NOT inherit tenant A.
        connection = self._connect(APP_ROLE, APP_PASSWORD)
        with connection.transaction():
            connection.execute("SELECT set_config('app.tenant_id', %s, true)",
                               (TENANT,))
            self.assertEqual(
                connection.execute("SELECT count(*) FROM results").fetchone()[0],
                1)
        with connection.transaction():
            self.assertEqual(
                connection.execute("SELECT count(*) FROM results").fetchone()[0],
                0)
            # ... and it can be rebound to the other tenant in its own txn.
            connection.execute("SELECT set_config('app.tenant_id', %s, true)",
                               (OTHER_TENANT,))
            rows = connection.execute("SELECT tenant_id FROM results").fetchall()
            self.assertEqual([row[0] for row in rows], [OTHER_TENANT])

    # -- least privilege --------------------------------------------------
    def test_worker_role_cannot_delete(self):
        connection = self._connect(WORKER_ROLE, WORKER_PASSWORD)
        with self.assertRaises(pg_errors.InsufficientPrivilege):
            with connection.transaction():
                connection.execute("SELECT set_config('app.tenant_id', %s, true)",
                                   (TENANT,))
                connection.execute("DELETE FROM results")

    def test_role_without_grants_is_denied(self):
        connection = self._connect(NOBODY_ROLE, NOBODY_PASSWORD)
        with self.assertRaises(pg_errors.InsufficientPrivilege):
            connection.execute("SELECT count(*) FROM results")
        connection.rollback()

    def test_schema_meta_is_read_only_for_the_runtime(self):
        connection = self._connect(APP_ROLE, APP_PASSWORD)
        with connection.transaction():
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()[0], "1")
        for statement in ("UPDATE schema_meta SET value = '9'",
                          "DELETE FROM schema_meta"):
            with self.subTest(statement=statement):
                with self.assertRaises(pg_errors.InsufficientPrivilege):
                    connection.execute(statement)
                connection.rollback()

    # -- rollback ---------------------------------------------------------
    def test_zz_rollback_restores_the_v1_shape(self):
        with psycopg.connect(_dsn(), autocommit=True) as setup:
            # The operator login roles hold membership, which blocks DROP ROLE;
            # revoke first (this is the documented rollback prerequisite).
            setup.execute(
                sql.SQL("REVOKE marker_app FROM {}").format(
                    sql.Identifier(APP_ROLE)))
            setup.execute(
                sql.SQL("REVOKE marker_worker FROM {}").format(
                    sql.Identifier(WORKER_ROLE)))
            _execute_sql_file(setup, "postgres_v2_tenant_rls_rollback.sql")
            for table in ("tenants", "tenant_membership", "results",
                          "audit_events", "pdf_references"):
                self.assertFalse(setup.execute(
                    "SELECT relrowsecurity FROM pg_class WHERE relname = %s",
                    (table,)).fetchone()[0])
            self.assertIsNone(setup.execute(
                "SELECT value FROM schema_meta WHERE key = 'rls_version'"
            ).fetchone())
            self.assertEqual(setup.execute(
                "SELECT count(*) FROM pg_roles WHERE rolname IN "
                "('marker_app', 'marker_worker')").fetchone()[0], 0)
            # Re-apply so the class teardown drops a hardened database and
            # the idempotent path is exercised once more.
            _execute_sql_file(setup, "postgres_v2_tenant_rls.sql")
            setup.execute(
                sql.SQL("GRANT marker_app TO {}").format(
                    sql.Identifier(APP_ROLE)))
            setup.execute(
                sql.SQL("GRANT marker_worker TO {}").format(
                    sql.Identifier(WORKER_ROLE)))


if __name__ == "__main__":
    unittest.main()
