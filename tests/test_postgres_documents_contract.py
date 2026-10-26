"""0004 contract tests: document metadata access + the separate purge role.

Runs ONLY with a DSN (CI ``postgres-tests``), in an isolated
``marker_docs_ci`` database. It applies 0001 -> 0002 -> 0003 -> 0004 -> 0004
(idempotency), then pins the security posture IR required for retention:

* runtime roles get SELECT/INSERT on ``pdf_references`` ONLY - no UPDATE, and
  **no DELETE anywhere** (retention is not a runtime verb);
* the retention/purge path uses a SEPARATE elevated role (``marker_purge``),
  never a widened runtime role, and that role holds nothing on results,
  jobs or the audit trail;
* every role is tenant-bound like the rest of the schema: a purge is per
  tenant with ``app.tenant_id`` set, never a privileged cross-tenant delete,
  and an unbound purge touches zero rows (fail closed);
* the backout restores the 0002 posture and cannot weaken the append-only
  audit grants.

Locally these skip with an explicit reason; that skip is the honest label,
never a pass.
"""
import os
import secrets
import unittest
from pathlib import Path

try:
    import psycopg
    from psycopg import errors as pg_errors
    from psycopg import sql as sqlmod
except ImportError:  # pragma: no cover - driver ships in the test manifest
    psycopg = None

DSN = os.environ.get("AI_MARKER_TEST_PG_DSN", "").strip()
ROOT = Path(__file__).resolve().parents[1]
TEST_DB = "marker_docs_ci"

TENANT = "school-docs-a"
OTHER_TENANT = "school-docs-b"
OWNER = "google:teacher-a"
OTHER_OWNER = "google:teacher-b"

APP_ROLE = "marker_docs_app_ci"
PURGE_ROLE = "marker_docs_purge_ci"
APP_PASSWORD = secrets.token_hex(24)
PURGE_PASSWORD = secrets.token_hex(24)

requires_pg = unittest.skipUnless(
    psycopg is not None and DSN,
    "PostgreSQL DSN not configured - the documents contract runs in the CI "
    "postgres-tests job only")


def _dsn(database: str = TEST_DB, *, user: str = "", password: str = "") -> str:
    overrides = {"dbname": database}
    if user:
        overrides.update(user=user, password=password)
    return psycopg.conninfo.make_conninfo(DSN, **overrides)


def _execute_sql_file(connection, name: str) -> None:
    connection.execute((ROOT / "schema" / name).read_text(encoding="utf-8"))


class MigrationArtifactTests(unittest.TestCase):
    """Offline pins: the 0004 artifacts cannot drift while PostgreSQL is absent."""

    def _sql(self, name: str) -> str:
        return (ROOT / "schema" / name).read_text(encoding="utf-8")

    def test_no_role_ever_gets_delete(self):
        sql = self._sql("postgres_v4_documents.sql")
        grants = [line.strip() for line in sql.splitlines()
                  if line.strip().upper().startswith("GRANT")]
        self.assertEqual(
            [line for line in grants if "DELETE" in line.upper()], [],
            "0004 must not grant DELETE to anyone")
        self.assertTrue(any("SELECT, INSERT ON pdf_references" in line
                            for line in grants))
        self.assertTrue(any("SELECT, UPDATE ON pdf_references TO marker_purge"
                            in line for line in grants))

    def test_the_purge_role_is_nologin_and_escapes_nothing(self):
        sql = self._sql("postgres_v4_documents.sql")
        self.assertIn("CREATE ROLE marker_purge NOLOGIN", sql)
        self.assertIn("rolbypassrls", sql)  # escalation-attribute check
        self.assertNotIn("BYPASSRLS", sql.split("CREATE ROLE")[1][:200])

    def test_the_rollback_discloses_what_it_does_not_undo(self):
        sql = self._sql("postgres_v4_documents_rollback.sql")
        self.assertIn("does NOT delete document objects", sql)
        self.assertIn("does NOT delete `pdf_references` rows", sql)
        self.assertIn("DROP ROLE marker_purge", sql)

    def test_the_runbook_ledgers_0004_and_justifies_the_purge_role(self):
        runbook = (ROOT / "schema" / "MIGRATIONS.md").read_text(encoding="utf-8")
        self.assertIn("postgres_v4_documents.sql", runbook)
        self.assertIn("`marker_purge`", runbook)
        self.assertIn("no DELETE, ever", runbook)


@requires_pg
class DocumentsContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = psycopg.connect(DSN, autocommit=True)
        cls.admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (TEST_DB,))
        cls.admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        cls.admin.execute(f'CREATE DATABASE "{TEST_DB}"')
        for role in (APP_ROLE, PURGE_ROLE):
            cls.admin.execute(sqlmod.SQL("DROP ROLE IF EXISTS {}").format(
                sqlmod.Identifier(role)))
            cls.admin.execute(sqlmod.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sqlmod.Identifier(role), sqlmod.Literal(
                        APP_PASSWORD if role == APP_ROLE else PURGE_PASSWORD)))
        with psycopg.connect(_dsn(), autocommit=True) as setup:
            for name in ("postgres_v1.sql", "postgres_v2_tenant_rls.sql",
                         "postgres_v3_jobs.sql", "postgres_v4_documents.sql"):
                _execute_sql_file(setup, name)
            # Idempotent: a second apply must not fail.
            _execute_sql_file(setup, "postgres_v4_documents.sql")
            setup.execute(sqlmod.SQL("GRANT marker_app TO {}").format(
                sqlmod.Identifier(APP_ROLE)))
            setup.execute(sqlmod.SQL("GRANT marker_purge TO {}").format(
                sqlmod.Identifier(PURGE_ROLE)))
            setup.execute(
                "INSERT INTO tenants (tenant_id) VALUES (%s), (%s)",
                (TENANT, OTHER_TENANT))
            setup.execute(
                "INSERT INTO tenant_membership (tenant_id, subject, active)"
                " VALUES (%s, %s, TRUE), (%s, %s, TRUE)",
                (TENANT, OWNER, OTHER_TENANT, OTHER_OWNER))

    @classmethod
    def tearDownClass(cls):
        # Leave no dependent membership or database behind: another suite's
        # backout refuses to drop group roles with dependents (the lesson from
        # the job-store suite's red run).
        for role, group in ((APP_ROLE, "marker_app"),
                            (PURGE_ROLE, "marker_purge")):
            try:
                cls.admin.execute(sqlmod.SQL(
                    "REVOKE {} FROM {}").format(
                        sqlmod.Identifier(group), sqlmod.Identifier(role)))
            except Exception:
                pass
            cls.admin.execute(sqlmod.SQL("DROP ROLE IF EXISTS {}").format(
                sqlmod.Identifier(role)))
        cls.admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (TEST_DB,))
        cls.admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        cls.admin.close()

    def setUp(self):
        self.setup = psycopg.connect(_dsn(), autocommit=True)
        self.addCleanup(self.setup.close)
        self.setup.execute("DELETE FROM pdf_references")
        for tenant, owner, digest in ((TENANT, OWNER, "a" * 64),
                                      (OTHER_TENANT, OTHER_OWNER, "b" * 64)):
            self.setup.execute(
                "INSERT INTO pdf_references (tenant_id, owner_subject,"
                " document_digest, storage_key, size_bytes)"
                " VALUES (%s, %s, %s, %s, 10)",
                (tenant, owner, digest,
                 f"objects/{'c' * 40}/{digest}.pdf"))

    def _connect(self, role, password):
        connection = psycopg.connect(
            _dsn(user=role, password=password), autocommit=True)
        self.addCleanup(connection.close)
        return connection

    # -- runtime posture --------------------------------------------------
    def test_runtime_reads_and_writes_its_own_rows_only(self):
        app = self._connect(APP_ROLE, APP_PASSWORD)
        # Unbound: fail closed.
        self.assertEqual(app.execute(
            "SELECT count(*) FROM pdf_references").fetchone()[0], 0)
        with app.transaction():
            app.execute("SELECT set_config('app.tenant_id', %s, true)",
                        (TENANT,))
            self.assertEqual(app.execute(
                "SELECT count(*) FROM pdf_references").fetchone()[0], 1)
            app.execute(
                "INSERT INTO pdf_references (tenant_id, owner_subject,"
                " document_digest, storage_key, size_bytes)"
                " VALUES (%s, %s, %s, %s, 3)",
                (TENANT, OWNER, "d" * 64, f"objects/{'e' * 40}/{'d' * 64}.pdf"))
        # Cross-tenant INSERT is refused by the WITH CHECK policy.
        with self.assertRaises(pg_errors.InsufficientPrivilege):
            with app.transaction():
                app.execute("SELECT set_config('app.tenant_id', %s, true)",
                            (TENANT,))
                app.execute(
                    "INSERT INTO pdf_references (tenant_id, owner_subject,"
                    " document_digest, storage_key, size_bytes)"
                    " VALUES (%s, %s, %s, %s, 3)",
                    (OTHER_TENANT, OTHER_OWNER, "f" * 64,
                     f"objects/{'9' * 40}/{'f' * 64}.pdf"))

    def test_runtime_roles_hold_no_delete_or_update_on_documents(self):
        for role, password in ((APP_ROLE, APP_PASSWORD),):
            app = self._connect(role, password)
            for statement in ("DELETE FROM pdf_references",
                              "UPDATE pdf_references SET storage_key = 'x'"):
                with self.subTest(statement=statement):
                    with self.assertRaises(pg_errors.InsufficientPrivilege):
                        app.execute(statement)
                    app.rollback()

    # -- the separate elevated purge role ---------------------------------
    def test_purge_tombstones_only_inside_its_bound_tenant(self):
        purge = self._connect(PURGE_ROLE, PURGE_PASSWORD)
        # Unbound: fail closed, zero rows touched.
        with purge.transaction():
            self.assertEqual(purge.execute(
                "UPDATE pdf_references SET storage_key = ''").rowcount, 0)
        # Bound to TENANT: only that tenant's row is tombstoned.
        with purge.transaction():
            purge.execute("SELECT set_config('app.tenant_id', %s, true)",
                          (TENANT,))
            self.assertEqual(purge.execute(
                "UPDATE pdf_references SET storage_key = '',"
                " size_bytes = NULL").rowcount, 1)
        rows = self.setup.execute(
            "SELECT tenant_id, storage_key FROM pdf_references"
            " ORDER BY tenant_id").fetchall()
        # The tombstone row STAYS (digest kept for audit), the other tenant is
        # untouched.
        self.assertEqual(rows[0][0], TENANT)
        self.assertEqual(rows[0][1], "")
        self.assertTrue(rows[1][1].startswith("objects/"))

    def test_purge_role_cannot_touch_results_jobs_or_the_audit_trail(self):
        purge = self._connect(PURGE_ROLE, PURGE_PASSWORD)
        for statement in ("SELECT count(*) FROM results",
                          "SELECT count(*) FROM marking_jobs",
                          "SELECT count(*) FROM audit_events",
                          "INSERT INTO audit_events (tenant_id) VALUES ('x')",
                          "DELETE FROM results",
                          "DELETE FROM pdf_references"):
            with self.subTest(statement=statement):
                with self.assertRaises(pg_errors.InsufficientPrivilege):
                    purge.execute(statement)
                purge.rollback()

    def test_purge_role_is_nologin_and_unprivileged(self):
        row = self.setup.execute(
            "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb,"
            " rolcreaterole FROM pg_roles WHERE rolname = 'marker_purge'"
        ).fetchone()
        self.assertEqual(row, (False, False, False, False, False))
        grants = self.setup.execute(
            "SELECT table_name, privilege_type"
            " FROM information_schema.role_table_grants"
            " WHERE grantee = 'marker_purge' ORDER BY 1, 2").fetchall()
        self.assertEqual(grants, [("pdf_references", "SELECT"),
                                  ("pdf_references", "UPDATE")])

    # -- backout ----------------------------------------------------------
    def test_zz_rollback_restores_the_0002_posture_then_reapplies(self):
        with psycopg.connect(_dsn(), autocommit=True) as setup:
            setup.execute(sqlmod.SQL("REVOKE marker_purge FROM {}").format(
                sqlmod.Identifier(PURGE_ROLE)))
            _execute_sql_file(setup, "postgres_v4_documents_rollback.sql")
            self.assertEqual(setup.execute(
                "SELECT count(*) FROM pg_roles WHERE rolname ="
                " 'marker_purge'").fetchone()[0], 0)
            self.assertIsNone(setup.execute(
                "SELECT value FROM schema_meta WHERE key = 'docs_version'"
            ).fetchone())
            # Back to the 0002 posture: no runtime grant on pdf_references.
            self.assertEqual(setup.execute(
                "SELECT count(*) FROM information_schema.role_table_grants"
                " WHERE table_name = 'pdf_references' AND grantee IN"
                " ('marker_app','marker_worker')").fetchone()[0], 0)
            # And the append-only audit guarantee is untouched.
            self.assertEqual(setup.execute(
                "SELECT privilege_type FROM information_schema"
                ".role_table_grants WHERE table_name = 'audit_events'"
                " AND grantee = 'marker_app' ORDER BY 1").fetchall(),
                [("INSERT",), ("SELECT",)])
            # Re-apply so a later run starts from the hardened shape.
            _execute_sql_file(setup, "postgres_v4_documents.sql")


if __name__ == "__main__":
    unittest.main()
