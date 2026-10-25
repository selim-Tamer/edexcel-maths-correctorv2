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
* FORCE RLS binds a NON-superuser table owner (and refuses its cross-tenant
  write), while a BYPASSRLS role - and the privileged admin path - sees every
  tenant: the limit of RLS is demonstrated, not claimed away;
* the runtime role cannot ``SET ROLE`` to a privileged identity, and inherits
  no privileged membership;
* no ``SECURITY DEFINER`` function in ``public`` can manufacture a tenant;
* a session-level tenant binding would leak into the next transaction, while
  the transaction-local binding used by ``postgres_store`` cannot;
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
# Probe roles for the privileged-path evidence: an plain owner (FORCE RLS must
# still bind it) and a BYPASSRLS role (RLS cannot bind it - which is exactly
# why no runtime login is ever granted that attribute).
PROBE_OWNER_ROLE = "marker_rls_probe_owner_ci"
PROBE_BYPASS_ROLE = "marker_rls_probe_bypass_ci"
PROBE_OWNER_PASSWORD = secrets.token_hex(24)
PROBE_BYPASS_PASSWORD = secrets.token_hex(24)

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
                               (NOBODY_ROLE, NOBODY_PASSWORD),
                               (PROBE_OWNER_ROLE, PROBE_OWNER_PASSWORD),
                               (PROBE_BYPASS_ROLE, PROBE_BYPASS_PASSWORD)):
            cls.admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(
                sql.Identifier(role)))
        for role, password in ((APP_ROLE, APP_PASSWORD),
                               (WORKER_ROLE, WORKER_PASSWORD),
                               (NOBODY_ROLE, NOBODY_PASSWORD),
                               (PROBE_OWNER_ROLE, PROBE_OWNER_PASSWORD)):
            cls.admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)))
        cls.bypass_available = True
        try:
            cls.admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN BYPASSRLS PASSWORD {}").format(
                    sql.Identifier(PROBE_BYPASS_ROLE),
                    sql.Literal(PROBE_BYPASS_PASSWORD)))
        except psycopg.Error:
            # BYPASSRLS needs a superuser; the probe that depends on it skips
            # with an explicit reason rather than silently passing.
            cls.bypass_available = False

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

        # Privileged-path probe: a table whose owner is NOT a superuser, so
        # FORCE RLS has something to actually bind, plus (when the cluster
        # allows creating it) a BYPASSRLS role to show the limit of RLS.
        with psycopg.connect(_dsn(), autocommit=True) as probe_setup:
            probe_setup.execute(sql.SQL(
                "CREATE SCHEMA rls_probe AUTHORIZATION {}").format(
                    sql.Identifier(PROBE_OWNER_ROLE)))
        with psycopg.connect(_dsn(user=PROBE_OWNER_ROLE,
                                  password=PROBE_OWNER_PASSWORD),
                             autocommit=True) as owner:
            owner.execute(
                "CREATE TABLE rls_probe.docs ("
                " tenant_id text NOT NULL, doc text NOT NULL)")
            owner.execute(
                "ALTER TABLE rls_probe.docs ENABLE ROW LEVEL SECURITY")
            owner.execute(
                "ALTER TABLE rls_probe.docs FORCE  ROW LEVEL SECURITY")
            owner.execute(
                "CREATE POLICY tenant_isolation ON rls_probe.docs"
                " USING (tenant_id = NULLIF(current_setting"
                "('app.tenant_id', true), ''))"
                " WITH CHECK (tenant_id = NULLIF(current_setting"
                "('app.tenant_id', true), ''))")
            for tenant, doc in ((TENANT, "a"), (OTHER_TENANT, "b")):
                with owner.transaction():
                    owner.execute(
                        "SELECT set_config('app.tenant_id', %s, true)",
                        (tenant,))
                    owner.execute(
                        "INSERT INTO rls_probe.docs (tenant_id, doc)"
                        " VALUES (%s, %s)", (tenant, doc))
            if cls.bypass_available:
                owner.execute(sql.SQL(
                    "GRANT USAGE ON SCHEMA rls_probe TO {}").format(
                        sql.Identifier(PROBE_BYPASS_ROLE)))
                owner.execute(sql.SQL(
                    "GRANT SELECT ON rls_probe.docs TO {}").format(
                        sql.Identifier(PROBE_BYPASS_ROLE)))

    @classmethod
    def tearDownClass(cls):
        try:
            cls.admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (TEST_DB,))
            cls.admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        finally:
            for role in (APP_ROLE, WORKER_ROLE, NOBODY_ROLE,
                         PROBE_OWNER_ROLE, PROBE_BYPASS_ROLE):
                cls.admin.execute(f'DROP ROLE IF EXISTS "{role}"')
            cls.admin.close()

    # -- helpers ----------------------------------------------------------
    def _connect(self, role: str, password: str):
        connection = psycopg.connect(_dsn(user=role, password=password))
        self.addCleanup(connection.close)
        return connection

    def _database_admin(self):
        """Privileged connection INSIDE the throwaway test database.

        The class-level ``cls.admin`` connection points at the cluster's
        default database, where the v1 tables do not exist; catalog checks
        about the test schema must use this one.
        """
        connection = psycopg.connect(_dsn(), autocommit=True)
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

    # -- privileged path (FORCE RLS limits) -------------------------------
    def test_force_rls_binds_the_table_owner(self):
        owner = self._connect(PROBE_OWNER_ROLE, PROBE_OWNER_PASSWORD)
        with owner.transaction():
            self.assertEqual(
                owner.execute(
                    "SELECT count(*) FROM rls_probe.docs").fetchone()[0], 0)
            owner.execute("SELECT set_config('app.tenant_id', %s, true)",
                          (TENANT,))
            rows = owner.execute(
                "SELECT tenant_id FROM rls_probe.docs").fetchall()
            self.assertEqual([row[0] for row in rows], [TENANT])
        with self.assertRaises(pg_errors.InsufficientPrivilege):
            with owner.transaction():
                owner.execute("SELECT set_config('app.tenant_id', %s, true)",
                              (TENANT,))
                owner.execute(
                    "INSERT INTO rls_probe.docs (tenant_id, doc)"
                    " VALUES (%s, 'smuggled')", (OTHER_TENANT,))

    def test_bypassrls_role_ignores_the_policy(self):
        if not self.bypass_available:
            self.skipTest(
                "cluster refused a BYPASSRLS role (admin is not superuser); the "
                "bypass limit is stated in schema/MIGRATIONS.md instead")
        bypass = self._connect(PROBE_BYPASS_ROLE, PROBE_BYPASS_PASSWORD)
        rows = bypass.execute("SELECT tenant_id FROM rls_probe.docs").fetchall()
        # No binding at all, and still both tenants: RLS cannot bind BYPASSRLS.
        self.assertEqual({row[0] for row in rows}, {TENANT, OTHER_TENANT})
        bypass.rollback()

    def test_superuser_path_is_not_constrained_by_rls(self):
        # The complement of the test above, on the real tables: a privileged
        # connection sees every tenant. Tenant authorisation for such a path is
        # the application's job (explicit tenant_id predicates), not RLS's.
        database = self._database_admin()
        rolsuper, rolbypass = database.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles"
            " WHERE rolname = current_user").fetchone()
        if not (rolsuper or rolbypass):
            self.skipTest("admin role is neither superuser nor BYPASSRLS")
        tenants = {row[0] for row in database.execute(
            "SELECT tenant_id FROM results").fetchall()}
        self.assertEqual(tenants, {TENANT, OTHER_TENANT})

    # -- pooled-request identity isolation --------------------------------
    def test_session_level_binding_would_leak_but_transaction_local_cannot(self):
        connection = self._connect(APP_ROLE, APP_PASSWORD)
        try:
            connection.execute(
                "SELECT set_config('app.tenant_id', %s, false)", (TENANT,))
            connection.commit()
            # A later transaction with no binding still sees tenant A. This is
            # the pooled-request leak that the transaction-local flag prevents.
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM results").fetchone()[0], 1)
            connection.rollback()
        finally:
            connection.rollback()
            connection.execute("SELECT set_config('app.tenant_id', '', false)")
            connection.commit()
        with connection.transaction():
            connection.execute("SELECT set_config('app.tenant_id', %s, true)",
                               (TENANT,))
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM results").fetchone()[0], 1)
        with connection.transaction():
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM results").fetchone()[0], 0)

    # -- injection / escalation surface -----------------------------------
    def test_runtime_role_cannot_assume_a_privileged_identity(self):
        connection = self._connect(APP_ROLE, APP_PASSWORD)
        for role in ("postgres", "service_role", "authenticated", "anon"):
            with self.subTest(role=role):
                with self.assertRaises(psycopg.Error):
                    connection.execute(f'SET ROLE "{role}"')
                connection.rollback()
                self.assertEqual(
                    connection.execute("SELECT current_user").fetchone()[0],
                    APP_ROLE)

    def test_runtime_roles_inherit_no_privileged_membership(self):
        query = (
            "WITH RECURSIVE reach(roleid) AS ("
            " SELECT oid FROM pg_roles WHERE rolname = %s"
            " UNION"
            " SELECT m.roleid FROM pg_auth_members m"
            " JOIN reach r ON m.member = r.roleid)"
            " SELECT rolname FROM pg_roles"
            " WHERE oid IN (SELECT roleid FROM reach)"
            " AND (rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole"
            " OR rolreplication)")
        for role in ("marker_app", "marker_worker"):
            with self.subTest(role=role):
                self.assertEqual(
                    self._database_admin().execute(query, (role,)).fetchall(),
                    [])

    def test_no_security_definer_function_can_manufacture_a_tenant(self):
        # SECURITY DEFINER runs with the owner's rights, so a function could
        # set app.tenant_id on the caller's behalf. The application schema must
        # contain none; re-run this inventory on the real project (extensions
        # and Supabase's autoRLS helper live outside public).
        rows = self._database_admin().execute(
            "SELECT p.proname FROM pg_proc p"
            " JOIN pg_namespace n ON n.oid = p.pronamespace"
            " WHERE p.prosecdef AND n.nspname = 'public'"
            " ORDER BY p.proname").fetchall()
        self.assertEqual([row[0] for row in rows], [])

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
