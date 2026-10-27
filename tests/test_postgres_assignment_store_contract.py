"""PostgreSQL assignment-store contract tests (CI service container).

* ``OpenStoreConnectionKwargsTests`` runs OFFLINE: the opener must pass
  ``prepare_threshold=None`` (Supabase transaction pooler) and must NOT create
  schema itself (fail closed, like every other production adapter).
* ``PostgresAssignmentContractTests`` runs ONLY with ``AI_MARKER_TEST_PG_DSN``
  (CI ``postgres-tests``) against an isolated database. It applies
  0001 -> 0002 -> 0003 -> 0004 -> 0007 (twice, for idempotency) and pins:
  server-minted ids, key replay/conflict, owner+tenant isolation, keyset
  pagination, RLS fail-closed, and the zero-UPDATE/zero-DELETE grant.

Locally these skip with an explicit reason; that skip is the honest label.
The real Supabase pgBouncer pooler is NOT exercised here.
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
except ImportError:  # pragma: no cover
    psycopg = None

import postgres_assignment_store
from assignment_store import AssignmentStoreError

DSN = os.environ.get("AI_MARKER_TEST_PG_DSN", "").strip()
ROOT = Path(__file__).resolve().parents[1]
TEST_DB = "marker_assignments_ci"

TENANT = "school-assignments-a"
OTHER_TENANT = "school-assignments-b"
OWNER = "google:teacher-a"
OTHER_OWNER = "google:teacher-b"

APP_ROLE = "marker_assignments_app_ci"
APP_PASSWORD = secrets.token_hex(24)

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


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConnection:
    def __init__(self, version="1", table="assignments"):
        self._answers = [_FakeResult((version,)), _FakeResult((table,))]

    @contextlib.contextmanager
    def transaction(self):
        yield self

    def execute(self, *_args, **_kwargs):
        return self._answers.pop(0)

    def close(self):
        pass


class OpenStoreConnectionKwargsTests(unittest.TestCase):
    def _open_with_fake(self, fake_connection):
        calls = {}
        fake = types.ModuleType("psycopg")

        def connect(dsn, **kwargs):
            calls["dsn"] = dsn
            calls["kwargs"] = kwargs
            return fake_connection

        fake.connect = connect
        with mock.patch.dict(sys.modules, {"psycopg": fake}):
            store = postgres_assignment_store.open_store(
                "  postgresql://pooler.example:6543/postgres  ")
        return calls, store

    def test_open_store_disables_prepared_statements(self):
        calls, store = self._open_with_fake(_FakeConnection())
        self.assertEqual(calls["dsn"],
                         "postgresql://pooler.example:6543/postgres")
        self.assertIsNone(calls["kwargs"]["prepare_threshold"])
        self.assertIsNotNone(store)

    def test_open_store_refuses_a_schema_it_did_not_find(self):
        for version, table in (("1", None), (None, "assignments"),
                               ("2", "assignments")):
            with self.subTest(version=version, table=table):
                with self.assertRaises(AssignmentStoreError) as caught:
                    self._open_with_fake(
                        _FakeConnection(version=version, table=table))
                self.assertEqual(caught.exception.detail, "store_unusable")

    def test_open_store_refuses_an_empty_dsn(self):
        for bad in ("", "   ", None):
            with self.subTest(dsn=bad):
                with self.assertRaises(AssignmentStoreError) as caught:
                    postgres_assignment_store.open_store(bad)
                self.assertEqual(caught.exception.detail, "store_unusable")


@requires_pg
class PostgresAssignmentContractTests(unittest.TestCase):
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
                         "postgres_v3_jobs.sql", "postgres_v4_documents.sql",
                         "postgres_v7_assignments.sql"):
                _execute_sql_file(setup, name)
            # Idempotent: a second apply must not fail.
            _execute_sql_file(setup, "postgres_v7_assignments.sql")
            setup.execute(sqlmod.SQL("GRANT marker_app TO {}").format(
                sqlmod.Identifier(APP_ROLE)))
            setup.execute(
                "INSERT INTO tenants (tenant_id) VALUES (%s), (%s)",
                (TENANT, OTHER_TENANT))

    @classmethod
    def tearDownClass(cls):
        cls.admin.execute(sqlmod.SQL("REVOKE marker_app FROM {}").format(
            sqlmod.Identifier(APP_ROLE)))
        cls.admin.execute(sqlmod.SQL("DROP ROLE IF EXISTS {}").format(
            sqlmod.Identifier(APP_ROLE)))
        cls.admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()", (TEST_DB,))
        cls.admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}"')
        cls.admin.close()

    def setUp(self):
        self.connection = psycopg.connect(
            _dsn(user=APP_ROLE, password=APP_PASSWORD),
            prepare_threshold=None)
        self.addCleanup(self.connection.close)
        with psycopg.connect(_dsn(), autocommit=True) as admin:
            admin.execute("DELETE FROM assignments")

    def create(self, key="assign-key-0001", title="Paper 1",
               owner=OWNER, tenant=TENANT):
        return postgres_assignment_store.create_assignment(
            self.connection, tenant_id=tenant, owner=owner,
            idempotency_key=key, title=title)

    # --- identity + idempotency -----------------------------------------

    def test_id_is_server_minted_and_replay_returns_it(self):
        first = self.create()
        second = self.create()
        self.assertRegex(first["assignment_id"], r"^asg_[0-9a-f]{24}$")
        self.assertEqual(first["assignment_id"], second["assignment_id"])
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])

    def test_same_key_different_title_is_a_conflict(self):
        self.create(key="conflict-key", title="Paper 1")
        with self.assertRaises(AssignmentStoreError) as caught:
            self.create(key="conflict-key", title="Paper 2")
        self.assertEqual("idempotency_key_payload_mismatch",
                         caught.exception.detail)

    def test_same_key_for_another_owner_is_isolated(self):
        mine = self.create(key="shared-key", owner=OWNER)
        theirs = self.create(key="shared-key", owner=OTHER_OWNER)
        self.assertNotEqual(mine["assignment_id"], theirs["assignment_id"])

    def test_get_is_owner_and_tenant_scoped(self):
        mine = self.create()
        self.assertIsNotNone(postgres_assignment_store.get_assignment(
            self.connection, tenant_id=TENANT, owner=OWNER,
            assignment_id=mine["assignment_id"]))
        self.assertIsNone(postgres_assignment_store.get_assignment(
            self.connection, tenant_id=TENANT, owner=OTHER_OWNER,
            assignment_id=mine["assignment_id"]))
        self.assertIsNone(postgres_assignment_store.get_assignment(
            self.connection, tenant_id=OTHER_TENANT, owner=OWNER,
            assignment_id=mine["assignment_id"]))

    # --- pagination ------------------------------------------------------

    def test_keyset_pagination_has_no_duplicates(self):
        for index in range(5):
            self.create(key=f"page-key-{index}", title=f"Paper {index}")
        seen = []
        after_at, after_id = 0, ""
        while True:
            page = postgres_assignment_store.list_assignments(
                self.connection, tenant_id=TENANT, owner=OWNER, limit=2,
                after_created_at=after_at, after_assignment_id=after_id)
            if not page:
                break
            seen.extend(row["assignment_id"] for row in page)
            after_at = page[-1]["created_at"]
            after_id = page[-1]["assignment_id"]
        self.assertEqual(5, len(seen))
        self.assertEqual(5, len(set(seen)))

    # --- least privilege / RLS -----------------------------------------

    def test_runtime_role_cannot_update_or_delete(self):
        mine = self.create()
        for statement in (
                ("UPDATE assignments SET title = 'x' WHERE assignment_id = %s",
                 (mine["assignment_id"],)),
                ("DELETE FROM assignments WHERE assignment_id = %s",
                 (mine["assignment_id"],))):
            with self.subTest(statement=statement[0].split()[0]):
                with psycopg.connect(
                        _dsn(user=APP_ROLE, password=APP_PASSWORD),
                        prepare_threshold=None) as connection:
                    with connection.transaction():
                        connection.execute(
                            "SELECT set_config('app.tenant_id', %s, true)",
                            (TENANT,))
                        with self.assertRaises(pg_errors.InsufficientPrivilege):
                            connection.execute(statement[0], statement[1])

    def test_rls_fails_closed_without_a_tenant_guc(self):
        self.create()
        with psycopg.connect(_dsn(user=APP_ROLE, password=APP_PASSWORD),
                             prepare_threshold=None) as connection:
            with connection.transaction():
                row = connection.execute(
                    "SELECT count(*) FROM assignments").fetchone()
        self.assertEqual(0, row[0])


if __name__ == "__main__":
    unittest.main()
