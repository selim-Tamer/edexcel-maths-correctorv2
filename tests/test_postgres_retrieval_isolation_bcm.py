"""Independent real-PostgreSQL isolation proof for the v1.2.1 recovery read.

BCM lane (test-only). The offline contract suite proves the API derives tenant
and owner from the verified subject and never from the request. This file
proves the second half against a real PostgreSQL service container: the
assignment-scoped job read (``GET /v1/assignments/{assignment_id}/jobs`` ->
``list_jobs_for_assignment``) is bounded by RLS *and* the owner predicate, so a
tenant-A connection cannot see tenant-B rows even when B reuses the identical
``assignment_id``, and an unbound connection fails closed.

Runs ONLY with ``AI_MARKER_TEST_PG_DSN`` (CI ``postgres-tests``); locally it
skips with an explicit reason. It applies 0001/0002/0003/0007/0008 to an
isolated database, exactly like the other PG contracts. No production system,
migration or data is touched.
"""
import os
import secrets
import unittest
from pathlib import Path

try:
    import psycopg
    from psycopg import conninfo
    from psycopg import sql as sqlmod
except ImportError:  # pragma: no cover
    psycopg = None

import postgres_job_store

DSN = os.environ.get("AI_MARKER_TEST_PG_DSN", "").strip()
ROOT = Path(__file__).resolve().parents[1]
TEST_DB = "marker_bcm_retrieval_ci"

TENANT_A = "school-bcm-retrieval-a"
TENANT_B = "school-bcm-retrieval-b"
OWNER_A = "google:bcm-teacher-a"
OWNER_B = "google:bcm-teacher-b"
SHARED_ASSIGNMENT = "asg_" + "a" * 24

APP_ROLE = "marker_bcm_retrieval_ci"
APP_PASSWORD = secrets.token_hex(24)

requires_pg = unittest.skipUnless(
    psycopg is not None and DSN,
    "PostgreSQL DSN not configured - the BCM retrieval isolation proof runs "
    "in CI postgres-tests")


def _dsn(database: str = TEST_DB, *, user: str = "", password: str = "") -> str:
    overrides = {"dbname": database}
    if user:
        overrides.update(user=user, password=password)
    return conninfo.make_conninfo(DSN, **overrides)


def _apply(connection, name: str) -> None:
    connection.execute((ROOT / "schema" / name).read_text(encoding="utf-8"))


@requires_pg
class PostgresRetrievalIsolationTests(unittest.TestCase):
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
            for name in ("postgres_v1.sql", "postgres_v2_tenant_rls.sql",
                         "postgres_v3_jobs.sql",
                         "postgres_v7_assignments.sql",
                         "postgres_v8_job_assignment_linkage.sql"):
                _apply(setup, name)
            setup.execute(sqlmod.SQL("GRANT marker_app TO {}").format(
                sqlmod.Identifier(APP_ROLE)))
            setup.execute(
                "INSERT INTO tenants (tenant_id) VALUES (%s), (%s)",
                (TENANT_A, TENANT_B))
        cls.jobs = postgres_job_store.open_store(_dsn())

    @classmethod
    def tearDownClass(cls):
        cls.jobs.close()
        cls.admin.execute(sqlmod.SQL("REVOKE marker_app FROM {}").format(
            sqlmod.Identifier(APP_ROLE)))
        cls.admin.execute(sqlmod.SQL("DROP ROLE IF EXISTS {}").format(
            sqlmod.Identifier(APP_ROLE)))
        cls.admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (TEST_DB,))
        cls.admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        cls.admin.close()

    def _seed(self, tenant_id, owner, key, digest, assignment_id, created):
        self.admin.execute(
            "INSERT INTO assignments (tenant_id, owner_subject,"
            " assignment_id, idempotency_key, title, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (tenant_id, owner, assignment_id, key, "BCM isolation paper",
             created))
        return postgres_job_store.create_job(
            self.jobs, tenant_id=tenant_id, owner=owner,
            idempotency_key=key + "-job", document_digest=digest,
            assignment_id=assignment_id)

    def test_a_tenant_cannot_see_b_rows_even_with_the_same_assignment_id(self):
        """Same literal assignment id in both tenants must stay disjoint."""
        a_job = self._seed(TENANT_A, OWNER_A, "bcm-iso-key-a", "a" * 64,
                           SHARED_ASSIGNMENT, 1)
        b_job = self._seed(TENANT_B, OWNER_A, "bcm-iso-key-b", "b" * 64,
                           SHARED_ASSIGNMENT, 2)

        seen_a = postgres_job_store.list_jobs_for_assignment(
            self.jobs, tenant_id=TENANT_A, owner=OWNER_A,
            assignment_id=SHARED_ASSIGNMENT, limit=50)
        seen_b = postgres_job_store.list_jobs_for_assignment(
            self.jobs, tenant_id=TENANT_B, owner=OWNER_A,
            assignment_id=SHARED_ASSIGNMENT, limit=50)
        self.assertEqual([a_job["job_id"]], [row["job_id"] for row in seen_a])
        self.assertEqual([b_job["job_id"]], [row["job_id"] for row in seen_b])

        # The database enforces the same boundary independently of the
        # adapter's WHERE clause: bind tenant A and read unqualified.
        with psycopg.connect(
                _dsn(user=APP_ROLE, password=APP_PASSWORD)) as raw:
            raw.execute("SELECT set_config('app.tenant_id', %s, true)",
                        (TENANT_A,))
            visible = raw.execute(
                "SELECT tenant_id, job_id FROM marking_jobs"
                " WHERE assignment_id = %s", (SHARED_ASSIGNMENT,)).fetchall()
        self.assertEqual([(TENANT_A, a_job["job_id"])], visible)

    def test_retrieval_is_owner_scoped_inside_one_tenant(self):
        assignment = "asg_" + "b" * 24
        a_job = self._seed(TENANT_A, OWNER_A, "bcm-owner-key-a", "c" * 64,
                           assignment, 3)
        b_job = self._seed(TENANT_A, OWNER_B, "bcm-owner-key-b", "d" * 64,
                           assignment, 4)
        mine = postgres_job_store.list_jobs_for_assignment(
            self.jobs, tenant_id=TENANT_A, owner=OWNER_A,
            assignment_id=assignment, limit=50)
        theirs = postgres_job_store.list_jobs_for_assignment(
            self.jobs, tenant_id=TENANT_A, owner=OWNER_B,
            assignment_id=assignment, limit=50)
        self.assertEqual([a_job["job_id"]], [row["job_id"] for row in mine])
        self.assertEqual([b_job["job_id"]], [row["job_id"] for row in theirs])
        self.assertNotIn(b_job["job_id"], [row["job_id"] for row in mine])

    def test_unbound_connection_fails_closed(self):
        with psycopg.connect(
                _dsn(user=APP_ROLE, password=APP_PASSWORD)) as raw:
            count = raw.execute(
                "SELECT count(*) FROM marking_jobs").fetchone()[0]
        self.assertEqual(0, count,
                         "RLS must return zero rows without a tenant binding")


if __name__ == "__main__":
    unittest.main()
