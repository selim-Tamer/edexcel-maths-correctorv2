"""Real-PostgreSQL membership resolution contract (CI; local skip).

Runs ONLY when ``AI_MARKER_TEST_PG_DSN`` is set (the CI ``postgres-tests``
job). Locally the module skips with an explicit reason - that skip is the
honest fallback label, never a pass.

What this proves, on a real server in an isolated throwaway database:

* an ACTIVE membership resolves to exactly that subject's tenant;
* INACTIVE and ABSENT both resolve to ``None`` (no enumeration);
* TWO active tenants are ambiguous and fail closed;
* a subject in tenant B never comes back as tenant A;
* revocation takes effect on the NEXT resolution (no cache, no restart);
* the transaction-local ``app.subject`` binding cannot bleed across a
  reused/pooled connection, while a session-level binding does (the contrast
  is the justification for the flag);
* the runtime role cannot create, change or delete a membership row - only
  read its own five columns;
* the rollback restores the 0002 posture (no runtime read at all).

And one limit, asserted rather than claimed away: a compromised database
credential can set ``app.subject`` to a subject it knows and read that
subject's membership. RLS narrows a leaked runtime credential's reach; it is
not an authenticity boundary.
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

import membership_store
import postgres_membership_store

DSN = os.environ.get("AI_MARKER_TEST_PG_DSN", "").strip()
ROOT = Path(__file__).resolve().parents[1]
TEST_DB = "marker_membership_ci"
APP_ROLE = "marker_membership_app_ci"
APP_PASSWORD = secrets.token_hex(24)

TENANT_A = "school-mem-a"
TENANT_B = "school-mem-b"
SUBJECT_A = "google:mem-teacher-a"
SUBJECT_B = "google:mem-teacher-b"
SUBJECT_INACTIVE = "google:mem-teacher-inactive"
SUBJECT_ABSENT = "google:mem-teacher-absent"
SUBJECT_AMBIGUOUS = "google:mem-teacher-ambiguous"

requires_pg = unittest.skipUnless(
    psycopg is not None and DSN,
    "PostgreSQL DSN not configured - the membership contract runs in the CI "
    "postgres-tests job only")


def _dsn(database: str = TEST_DB, *, user: str = "", password: str = "") -> str:
    overrides = {"dbname": database}
    if user:
        overrides.update(user=user, password=password)
    return conninfo.make_conninfo(DSN, **overrides)


def _sql_file(connection, name: str) -> None:
    connection.execute((ROOT / "schema" / name).read_text(encoding="utf-8"))


@requires_pg
class PostgresMembershipContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.admin = psycopg.connect(DSN, autocommit=True)
        cls.admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE datname = %s AND pid <> pg_backend_pid()", (TEST_DB,))
        cls.admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        cls.admin.execute(f'CREATE DATABASE "{TEST_DB}"')
        cls.admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(
            sql.Identifier(APP_ROLE)))
        cls.admin.execute(
            sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                sql.Identifier(APP_ROLE), sql.Literal(APP_PASSWORD)))
        with psycopg.connect(_dsn(), autocommit=True) as setup:
            _sql_file(setup, "postgres_v1.sql")
            _sql_file(setup, "postgres_v2_tenant_rls.sql")
            _sql_file(setup, "postgres_v5_membership_access.sql")
            # Idempotent: a second apply must not fail.
            _sql_file(setup, "postgres_v5_membership_access.sql")
            setup.execute(f'GRANT marker_app TO "{APP_ROLE}"')
            setup.execute(
                "INSERT INTO tenants (tenant_id) VALUES (%s), (%s)",
                (TENANT_A, TENANT_B))
            for tenant, subject, active in (
                    (TENANT_A, SUBJECT_A, True),
                    (TENANT_B, SUBJECT_B, True),
                    (TENANT_A, SUBJECT_INACTIVE, False),
                    (TENANT_A, SUBJECT_AMBIGUOUS, True),
                    (TENANT_B, SUBJECT_AMBIGUOUS, True)):
                setup.execute(
                    "INSERT INTO tenant_membership"
                    " (tenant_id, subject, role, active)"
                    " VALUES (%s, %s, %s, %s)",
                    (tenant, subject, "teacher", active))

    @classmethod
    def tearDownClass(cls):
        # Roles are cluster-global. Leaving marker_app grants in
        # marker_membership_ci blocks the RLS suite's DROP ROLE backout
        # (same lesson as the job-store / documents suites).
        admin = getattr(cls, "admin", None)
        if admin is not None:
            try:
                admin.execute(sql.SQL("REVOKE marker_app FROM {}").format(
                    sql.Identifier(APP_ROLE)))
            except Exception:
                pass
            try:
                admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(
                    sql.Identifier(APP_ROLE)))
            except Exception:
                pass
            try:
                admin.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = %s AND pid <> pg_backend_pid()",
                    (TEST_DB,))
                admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
            except Exception:
                pass
            try:
                admin.close()
            except Exception:
                pass

    def app_dsn(self):
        return _dsn(user=APP_ROLE, password=APP_PASSWORD)

    # --- resolution semantics -------------------------------------------

    def test_active_membership_resolves_to_its_tenant(self):
        resolved = postgres_membership_store.resolve(SUBJECT_A,
                                                     dsn=self.app_dsn())
        self.assertIsInstance(resolved, membership_store.Membership)
        self.assertEqual(resolved.subject, SUBJECT_A)
        self.assertEqual(resolved.tenant_id, TENANT_A)

    def test_inactive_and_absent_both_resolve_to_none(self):
        for subject in (SUBJECT_INACTIVE, SUBJECT_ABSENT):
            with self.subTest(subject=subject):
                self.assertIsNone(postgres_membership_store.resolve(
                    subject, dsn=self.app_dsn()))

    def test_two_active_tenants_are_ambiguous_and_refused(self):
        with self.assertRaises(membership_store.MembershipError) as caught:
            postgres_membership_store.resolve(SUBJECT_AMBIGUOUS,
                                              dsn=self.app_dsn())
        self.assertEqual(caught.exception.detail, "membership_ambiguous")

    def test_cross_tenant_subjects_never_cross(self):
        a = postgres_membership_store.resolve(SUBJECT_A, dsn=self.app_dsn())
        b = postgres_membership_store.resolve(SUBJECT_B, dsn=self.app_dsn())
        self.assertEqual(a.tenant_id, TENANT_A)
        self.assertEqual(b.tenant_id, TENANT_B)
        self.assertNotEqual(a.tenant_id, b.tenant_id)

    def test_revocation_takes_effect_on_the_next_resolution(self):
        with psycopg.connect(_dsn(), autocommit=True) as admin:
            admin.execute(
                "UPDATE tenant_membership SET active = FALSE"
                " WHERE tenant_id = %s AND subject = %s",
                (TENANT_B, SUBJECT_B))
        try:
            self.assertIsNone(postgres_membership_store.resolve(
                SUBJECT_B, dsn=self.app_dsn()))
        finally:
            with psycopg.connect(_dsn(), autocommit=True) as admin:
                admin.execute(
                    "UPDATE tenant_membership SET active = TRUE"
                    " WHERE tenant_id = %s AND subject = %s",
                    (TENANT_B, SUBJECT_B))
        self.assertIsNotNone(postgres_membership_store.resolve(
            SUBJECT_B, dsn=self.app_dsn()))

    # --- connection reuse / binding scope -------------------------------

    def _subject_rows(self, connection, subject: str) -> int:
        return len(connection.execute(
            "SELECT tenant_id FROM tenant_membership"
            " WHERE subject = %s AND active IS TRUE",
            (subject,)).fetchall())

    def test_transaction_local_binding_cannot_bleed_on_a_reused_connection(self):
        with psycopg.connect(self.app_dsn()) as connection:
            with connection.transaction():
                connection.execute(
                    "SELECT set_config('app.subject', %s, true)", (SUBJECT_A,))
                self.assertEqual(self._subject_rows(connection, SUBJECT_A), 1)
            # Same physical connection, NEW transaction, no binding: the
            # previous subject must be gone (fail closed, zero rows).
            with connection.transaction():
                self.assertEqual(connection.execute(
                    "SELECT tenant_id FROM tenant_membership").fetchall(), [])
            # Binding B in a later transaction sees B only, never A.
            with connection.transaction():
                connection.execute(
                    "SELECT set_config('app.subject', %s, true)", (SUBJECT_B,))
                rows = connection.execute(
                    "SELECT subject FROM tenant_membership").fetchall()
            self.assertEqual([row[0] for row in rows], [SUBJECT_B])

    def test_session_level_binding_would_leak_and_is_not_used(self):
        # Contrast case: documents WHY the adapter uses the `true` flag. A
        # session-level binding survives into the next transaction even with
        # no new binding - exactly the pooled-connection leak to avoid.
        with psycopg.connect(self.app_dsn()) as connection:
            connection.execute(
                "SELECT set_config('app.subject', %s, false)", (SUBJECT_A,))
            connection.commit()
            try:
                with connection.transaction():
                    leaked = connection.execute(
                        "SELECT tenant_id FROM tenant_membership"
                        " WHERE subject = %s", (SUBJECT_A,)).fetchall()
                self.assertEqual(len(leaked), 1)
            finally:
                connection.execute("SELECT set_config('app.subject', '', false)")
                connection.commit()

    def test_unbound_connection_sees_nothing(self):
        with psycopg.connect(self.app_dsn()) as connection:
            with connection.transaction():
                self.assertEqual(connection.execute(
                    "SELECT tenant_id FROM tenant_membership").fetchall(), [])

    # --- least privilege ------------------------------------------------

    def test_runtime_role_cannot_write_membership_rows(self):
        # One statement per transaction: a denied INSERT aborts the tx, and
        # later statements would otherwise raise InFailedSqlTransaction
        # instead of the InsufficientPrivilege we need to assert.
        for statement, params in (
                ("INSERT INTO tenant_membership"
                 " (tenant_id, subject, role, active)"
                 " VALUES (%s, %s, 'teacher', TRUE)",
                 (TENANT_A, "google:self-granted")),
                ("UPDATE tenant_membership SET active = TRUE"
                 " WHERE subject = %s", (SUBJECT_INACTIVE,)),
                ("DELETE FROM tenant_membership WHERE subject = %s",
                 (SUBJECT_A,))):
            with self.subTest(statement=statement.split()[0]):
                with psycopg.connect(self.app_dsn()) as connection:
                    with connection.transaction():
                        connection.execute(
                            "SELECT set_config('app.tenant_id', %s, true)",
                            (TENANT_A,))
                        with self.assertRaises(psycopg.Error) as caught:
                            connection.execute(statement, params)
                        self.assertIsInstance(
                            caught.exception, pg_errors.InsufficientPrivilege)

    def test_forged_subject_guc_is_a_known_limit_not_claimed_away(self):
        # A leaked runtime credential can name a subject it already knows and
        # read that subject's membership. This is a documented LIMIT of RLS:
        # the control that stops it is credential custody (runtime-only,
        # revocable, never client-visible), not the GUC.
        with psycopg.connect(self.app_dsn()) as connection:
            with connection.transaction():
                connection.execute(
                    "SELECT set_config('app.subject', %s, true)", (SUBJECT_B,))
                rows = connection.execute(
                    "SELECT manager.tenant_id FROM tenant_membership manager"
                    " WHERE manager.subject = %s", (SUBJECT_B,)).fetchall()
        self.assertEqual(rows, [(TENANT_B,)])

    def test_rollback_restores_the_no_read_posture_then_reapplies(self):
        with psycopg.connect(_dsn(), autocommit=True) as admin:
            _sql_file(admin, "postgres_v5_membership_access_rollback.sql")
        try:
            with psycopg.connect(self.app_dsn()) as connection:
                with connection.transaction():
                    connection.execute(
                        "SELECT set_config('app.subject', %s, true)",
                        (SUBJECT_A,))
                    with self.assertRaises(psycopg.Error) as caught:
                        connection.execute(
                            "SELECT tenant_id FROM tenant_membership"
                            " WHERE subject = %s", (SUBJECT_A,))
                self.assertIsInstance(caught.exception,
                                      pg_errors.InsufficientPrivilege)
            with psycopg.connect(_dsn(), autocommit=True) as admin:
                marker = admin.execute(
                    "SELECT value FROM schema_meta WHERE key ="
                    " 'membership_access_version'").fetchone()
            self.assertIsNone(marker)
        finally:
            with psycopg.connect(_dsn(), autocommit=True) as admin:
                _sql_file(admin, "postgres_v5_membership_access.sql")
        self.assertIsNotNone(postgres_membership_store.resolve(
            SUBJECT_A, dsn=self.app_dsn()))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
