"""0006 privilege-narrowing contract (CI postgres-tests only).

IR required the still-open `UPDATE(result_revision)` blocker to be closed by
implementation and meaningful privilege regression tests, not by reserving a
migration number.

This class applies 0001 -> 0002 -> 0006 (twice) in an isolated database and
pins the real privilege boundary:

* `marker_app` / `marker_worker` keep SELECT, INSERT and
  `UPDATE (result_revision)` on `results`;
* table-wide UPDATE is gone, and every OTHER column is refused by the server;
* the exact statement `postgres_store.commit()` runs still succeeds;
* DELETE remains impossible;
* the rollback restores the 0002 shape and refuses when 0006 was never applied.

Locally these skip with an explicit reason; that skip is the honest label.
"""
import os
import secrets
import unittest
from pathlib import Path

try:
    import psycopg
    from psycopg import conninfo
    from psycopg import errors as pg_errors
    from psycopg import sql
except ImportError:  # pragma: no cover
    psycopg = None

DSN = os.environ.get("AI_MARKER_TEST_PG_DSN", "").strip()
ROOT = Path(__file__).resolve().parents[1]
TEST_DB = "marker_privilege_ci"

TENANT = "school-narrow-a"
SUBJECT = "google:teacher-narrow"
RESULT = "manual:narrow"

APP_ROLE = "marker_narrow_app_ci"
APP_PASSWORD = secrets.token_hex(24)

OTHER_COLUMNS = (
    "tenant_id", "owner_subject", "result_id", "original_digest",
    "original_scores", "original_scores_digest", "created_at",
)

requires_pg = unittest.skipUnless(
    psycopg is not None and DSN,
    "PostgreSQL DSN not configured - this contract runs in CI postgres-tests")


def _dsn(database: str = TEST_DB, *, user: str = "", password: str = "") -> str:
    overrides = {"dbname": database}
    if user:
        overrides.update(user=user, password=password)
    return conninfo.make_conninfo(DSN, **overrides)


def _execute_sql_file(connection, name: str) -> None:
    connection.execute((ROOT / "schema" / name).read_text(encoding="utf-8"))


@requires_pg
class ResultRevisionNarrowingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = psycopg.connect(DSN, autocommit=True)
        cls.admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (TEST_DB,))
        cls.admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        cls.admin.execute(f'CREATE DATABASE "{TEST_DB}"')
        cls.admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(
            sql.Identifier(APP_ROLE)))
        cls.admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(APP_ROLE), sql.Literal(APP_PASSWORD)))
        with psycopg.connect(_dsn(), autocommit=True) as setup:
            _execute_sql_file(setup, "postgres_v1.sql")
            _execute_sql_file(setup, "postgres_v2_tenant_rls.sql")
            _execute_sql_file(setup, "postgres_v6_result_narrowing.sql")
            # Idempotent: a second apply must not fail and must not widen.
            _execute_sql_file(setup, "postgres_v6_result_narrowing.sql")
            setup.execute(sql.SQL("GRANT marker_app TO {}").format(
                sql.Identifier(APP_ROLE)))
            setup.execute(
                "INSERT INTO tenants (tenant_id) VALUES (%s)", (TENANT,))
            setup.execute(
                "INSERT INTO tenant_membership (tenant_id, subject)"
                " VALUES (%s, %s)", (TENANT, SUBJECT))
            setup.execute(
                "INSERT INTO results (tenant_id, owner_subject, result_id,"
                " original_digest, original_scores, original_scores_digest)"
                " VALUES (%s, %s, %s, %s, %s, %s)",
                (TENANT, SUBJECT, RESULT, "d" * 64, "[]", "e" * 64))

    @classmethod
    def tearDownClass(cls):
        cls.admin.execute(sql.SQL("REVOKE marker_app FROM {}").format(
            sql.Identifier(APP_ROLE)))
        cls.admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(
            sql.Identifier(APP_ROLE)))
        cls.admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (TEST_DB,))
        cls.admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        cls.admin.close()

    def _database_admin(self):
        """Privileged connection INSIDE the throwaway test database.

        ``cls.admin`` points at the cluster's default database, where the v1
        tables do not exist; every catalog/row check about this schema must
        use this one.
        """
        connection = psycopg.connect(_dsn(), autocommit=True)
        self.addCleanup(connection.close)
        return connection

    def _runtime_connection(self):
        connection = psycopg.connect(
            _dsn(user=APP_ROLE, password=APP_PASSWORD), prepare_threshold=None)
        self.addCleanup(connection.close)
        return connection

    def _has(self, role, column, privilege="UPDATE"):
        db = self._database_admin()
        if column is None:
            return db.execute(
                "SELECT has_table_privilege(%s, 'public.results', %s)",
                (role, privilege)).fetchone()[0]
        return db.execute(
            "SELECT has_column_privilege(%s, 'public.results', %s, %s)",
            (role, column, privilege)).fetchone()[0]

    def _explicit_column_grant(self, role, column, privilege="UPDATE"):
        """Is there a COLUMN-scoped grant row (not implied by table UPDATE)?

        ``has_column_privilege`` answers "can this role update this column",
        which a table-wide grant also satisfies. To tell the narrowed shape
        apart from the 0002 shape we need the explicit grant row itself.
        """
        row = self._database_admin().execute(
            "SELECT 1 FROM information_schema.role_column_grants"
            " WHERE grantee = %s AND table_name = 'results'"
            " AND column_name = %s AND privilege_type = %s",
            (role, column, privilege)).fetchone()
        return row is not None

    # --- the grant itself -------------------------------------------------

    def test_marker_keeps_only_the_revision_column_update(self):
        for role in ("marker_app", "marker_worker"):
            with self.subTest(role=role):
                self.assertFalse(self._has(role, None),
                                 "table-wide UPDATE must be gone")
                self.assertTrue(self._has(role, "result_revision"))
                self.assertTrue(
                    self._explicit_column_grant(role, "result_revision"),
                    "the narrowing must be an explicit COLUMN grant")
                for column in OTHER_COLUMNS:
                    self.assertFalse(self._has(role, column),
                                     f"{role} must not update {column}")

    def test_select_and_insert_are_unchanged(self):
        for role in ("marker_app", "marker_worker"):
            with self.subTest(role=role):
                self.assertTrue(self._has(role, None, "SELECT"))
                self.assertTrue(self._has(role, None, "INSERT"))
                self.assertFalse(self._has(role, None, "DELETE"))

    def test_marker_is_recorded(self):
        row = self._database_admin().execute(
            "SELECT value FROM schema_meta"
            " WHERE key = 'results_update_version'").fetchone()
        self.assertEqual(("1",), row)

    # --- the shipped statement still works, everything else is refused ----

    def test_adapter_shaped_revision_update_still_succeeds(self):
        connection = self._runtime_connection()
        with connection.transaction():
            connection.execute(
                "SELECT set_config('app.tenant_id', %s, true)", (TENANT,))
            cursor = connection.execute(
                "UPDATE results SET result_revision = %s"
                " WHERE tenant_id = %s AND owner_subject = %s"
                " AND result_id = %s", (1, TENANT, SUBJECT, RESULT))
            self.assertEqual(1, cursor.rowcount)
        revision = self._database_admin().execute(
            "SELECT result_revision FROM results WHERE result_id = %s",
            (RESULT,)).fetchone()[0]
        self.assertEqual(1, revision)

    def test_rewriting_the_original_result_is_denied(self):
        for statement in (
                "UPDATE results SET original_scores = '[]'",
                "UPDATE results SET original_digest = 'x'",
                "UPDATE results SET result_id = 'manual:other'",
                "UPDATE results SET tenant_id = 'school-narrow-b'"):
            with self.subTest(statement=statement):
                connection = self._runtime_connection()
                with connection.transaction():
                    connection.execute(
                        "SELECT set_config('app.tenant_id', %s, true)",
                        (TENANT,))
                    with self.assertRaises(pg_errors.InsufficientPrivilege):
                        connection.execute(statement)

    def test_delete_remains_denied(self):
        connection = self._runtime_connection()
        with connection.transaction():
            connection.execute(
                "SELECT set_config('app.tenant_id', %s, true)", (TENANT,))
            with self.assertRaises(pg_errors.InsufficientPrivilege):
                connection.execute(
                    "DELETE FROM results WHERE result_id = %s", (RESULT,))

    # --- rollback / re-apply ---------------------------------------------

    def test_rollback_restores_the_0002_shape_and_refuses_when_absent(self):
        with psycopg.connect(_dsn(), autocommit=True) as setup:
            _execute_sql_file(setup,
                              "postgres_v6_result_narrowing_rollback.sql")
            # 0002 shape is back: table-wide UPDATE, and NO column-scoped grant
            # row. (has_column_privilege is true again here BECAUSE the table
            # grant implies it, so the explicit row is the real discriminator.)
            self.assertTrue(self._has("marker_app", None))
            self.assertFalse(
                self._explicit_column_grant("marker_app", "result_revision"))
            marker = setup.execute(
                "SELECT 1 FROM schema_meta"
                " WHERE key = 'results_update_version'").fetchone()
            self.assertIsNone(marker)
            with self.assertRaises(psycopg.Error):
                _execute_sql_file(
                    setup, "postgres_v6_result_narrowing_rollback.sql")
            # Restore the narrowed state for any later test/order.
            _execute_sql_file(setup, "postgres_v6_result_narrowing.sql")
            self.assertTrue(
                self._explicit_column_grant("marker_app", "result_revision"))
            self.assertFalse(self._has("marker_app", None))


if __name__ == "__main__":
    unittest.main()
