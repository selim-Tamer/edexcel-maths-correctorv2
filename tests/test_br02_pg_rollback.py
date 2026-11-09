"""BR-02 PG harness: guarded rollback draft behavior (offline structural
review + PG-gated live behavior; live classes unexecuted by design until the
future approval exists - a skip is the honest gate, never a pass).

Coverage map:
* offline: the ACTUAL draft artifact's guarded structure (role check before
  destructive DDL, fail-closed state validation, ACCESS EXCLUSIVE lock
  before the populated refusal, empty-only removal, no widening/disabling
  statements) and the explicit stage-2 plan disclosure;
* gated (REAL operations against the live target): a non-BYPASSRLS owner
  sees hidden rows without a tenant binding and for other-bound tenants;
  a privileged run against a POPULATED context refuses and leaves data,
  table, and marker intact; a NON-elevated role is refused even on an empty
  context; the lock/timeout behavior is atomic; and the EMPTY privileged
  success runs ONLY against a separately named sacrificial target with an
  explicit rollback-drill authorization (future only, never this target).
"""
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

ROOT = Path(__file__).resolve().parents[1]
ROLLBACK_PATH = ROOT / "schema" / "postgres_v9_result_context_rollback.sql"


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


class DraftStructureTests(unittest.TestCase):
    """The ACTUAL draft artifact's guarded structure (the review subject)."""

    def setUp(self):
        self.sql = ROLLBACK_PATH.read_text(encoding="utf-8").lower()

    def test_role_check_precedes_destructive_ddl(self):
        self.assertIn("rolsuper", self.sql)
        self.assertIn("rolbypassrls", self.sql)
        self.assertIn("pg_catalog.pg_roles", self.sql)
        self.assertLess(self.sql.index("raise exception"),
                        self.sql.index("drop "))

    def test_state_validation_is_fail_closed(self):
        self.assertIn("inconsistent state", self.sql)
        self.assertIn("already absent", self.sql)
        state_at = self.sql.index("to_regclass('public.result_context')")
        marker_at = self.sql.index("result_context_version")
        drop_at = self.sql.index("drop ")
        self.assertLess(state_at, drop_at)
        self.assertLess(marker_at, drop_at)

    def test_lock_precedes_populated_refusal_and_is_held_to_commit(self):
        self.assertIn("access exclusive", self.sql)
        self.assertLess(self.sql.index("lock table"),
                        self.sql.index("result_context is populated"))
        begin_at = self.sql.index("begin;")
        commit_at = self.sql.index("commit;")
        lock_at = self.sql.index("lock table")
        self.assertLess(begin_at, lock_at)
        self.assertLess(lock_at, commit_at)

    def test_empty_only_removal_with_no_widening(self):
        self.assertIn("delete from public.schema_meta", self.sql)
        for banned in ("cascade", "security definer",
                       "disable row level security", "disable trigger",
                       "grant "):
            self.assertNotIn(banned, self.sql)

    def test_search_path_is_pinned_before_any_guard_or_ddl(self):
        pinned = "set local search_path to pg_catalog, public;"
        self.assertIn(pinned, self.sql)
        self.assertLess(self.sql.index("begin;"), self.sql.index(pinned))
        self.assertLess(self.sql.index(pinned),
                        self.sql.index("raise exception"))
        self.assertLess(self.sql.index(pinned), self.sql.index("drop "))

    def test_every_application_object_is_schema_qualified(self):
        for qualified in (
                "pg_catalog.to_regclass('public.result_context')",
                "from public.schema_meta",
                "from public.result_context",
                "lock table public.result_context",
                "on public.result_context",
                "drop function if exists public.result_context_immutable()",
                "drop table if exists public.result_context",
                "delete from public.schema_meta",
                "pg_catalog.pg_roles",
                "pg_catalog.pg_class",
                "pg_catalog.pg_namespace",
        ):
            with self.subTest(qualified=qualified):
                self.assertIn(qualified, self.sql)
        for unqualified in (
                "from schema_meta",
                "from result_context",
                "on result_context",
                "table result_context",
                "function if exists result_context_immutable()",
                "delete from schema_meta",
        ):
            with self.subTest(unqualified=unqualified):
                self.assertNotIn(unqualified, self.sql)

    def test_capability_marker_requires_version_one(self):
        self.assertIn("from public.schema_meta", self.sql)
        self.assertIn("key = 'result_context_version'", self.sql)
        self.assertIn("marker_value is distinct from '1'", self.sql)
        self.assertIn("must be exactly 1", self.sql)
        self.assertLess(self.sql.index("must be exactly 1"),
                        self.sql.index("drop "))

    def test_expected_object_shape_is_the_ordinary_table(self):
        self.assertIn("from pg_catalog.pg_class cls", self.sql)
        self.assertIn("join pg_catalog.pg_namespace nsp", self.sql)
        self.assertIn("cls.relkind", self.sql)
        self.assertIn("relation_kind <> 'r'", self.sql)
        self.assertIn("is not the expected ", self.sql)
        self.assertIn("ordinary table (relkind %)", self.sql)

    def test_stage2_plan_discloses_checks_and_missing_transport(self):
        plan = br02_pg_target.stage2_plan()
        self.assertTrue(plan["checks"])
        self.assertEqual(len(plan["checks"]),
                         len(br02_pg_target.STAGE2_PLANNED_CHECKS))
        self.assertIn("gate", plan["phase"])
        self.assertIn("do not exist yet",
                      plan["missing_future_approval_transport"])
        self.assertEqual(plan["implementation"],
                         "stage2_run_catalog_comparison")


class _LiveRollbackBase(unittest.TestCase):
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
        # The future approval must bind a run marker to the approved manifest
        # overall run identity, and the target must already carry that EXACT
        # marker (stage-0 bootstrap is separate and explicitly authorized).
        approval = cls.approval
        manifest = getattr(approval, "approved_manifest", None)
        binding = getattr(manifest, "binding", None)
        bound_run = binding.get("run_id") if isinstance(binding, dict) else None
        run_marker = getattr(approval, "run_marker", "")
        if not isinstance(run_marker, str) or not run_marker.strip() \
                or run_marker != bound_run:
            cls.conn.close()
            raise unittest.SkipTest(
                "the future approval supplies no run_marker bound to the "
                "approved manifest run identity; live classes stay "
                "unexecuted (disclosed, never faked)")
        try:
            marker = cls.conn.execute(
                "select value from schema_meta where key = %s",
                (br02_pg_target.STAGE2_RUN_MARKER_KEY,)).fetchone()
        except Exception:
            cls.conn.close()
            raise
        if marker is None:
            cls.conn.close()
            raise unittest.SkipTest(
                "the target has no approved synthetic run marker; stage-0 "
                "bootstrap is a separate explicitly authorized operation - "
                "live classes stay unexecuted")

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

    def register_original(self):
        import identity_context
        import postgres_result_store
        store = postgres_result_store.open_store(
            self.dsn,
            identity=identity_context.from_verified_subject(
                self.bundle.raw_subject, self.bundle.tenant_id),
            fresh_completion_denied=False)
        try:
            store.register_with_context(
                result_id=self.bundle.result_id,
                original_output=self.bundle.original_output,
                scope="manual", review_key=self.bundle.review_key,
                document_digest=self.bundle.document_digest)
        finally:
            store.close()

    def effective_role_is_elevated(self):
        row = self.conn.execute(
            "select coalesce(rolsuper, false) or coalesce(rolbypassrls,"
            " false) from pg_catalog.pg_roles where rolname = current_user"
        ).fetchone()
        return bool(row and row[0])

    def effective_role_is_bypassrls(self):
        row = self.conn.execute(
            "select coalesce(rolbypassrls, false) from pg_catalog.pg_roles"
            " where rolname = current_user").fetchone()
        return bool(row and row[0])

    def context_state(self):
        # Verification must also be bounded if another session retains a lock.
        with self.conn.transaction():
            self.conn.execute("set local statement_timeout = '2s'")
            table = self.conn.execute(
                "select to_regclass('public.result_context') is not null"
            ).fetchone()[0]
            marker = self.conn.execute(
                "select count(*) from schema_meta where key ="
                " 'result_context_version'").fetchone()[0]
            rows = self.conn.execute(
                "select count(*) from result_context").fetchone()[0]
        return table, marker, rows

    def rollback_preservation_state(self):
        """Full visible row/marker contents, plus the current fixture identity.

        This elevated-role probe is bounded and never assumes an empty target;
        earlier test fixtures are deliberately retained by the harness.
        """
        with self.conn.transaction():
            self.conn.execute("set local statement_timeout = '2s'")
            table = self.conn.execute(
                "select to_regclass('public.result_context') is not null"
            ).fetchone()[0]
            markers = self.conn.execute(
                "select key, value from public.schema_meta"
                " where key = 'result_context_version' order by key"
            ).fetchall()
            rows = self.conn.execute(
                "select to_jsonb(ctx)::text from public.result_context as ctx"
                " order by tenant_id, membership_subject, result_id"
            ).fetchall()
            fixture_rows = self.conn.execute(
                "select count(*) from public.result_context"
                " where tenant_id=%s and membership_subject=%s and result_id=%s",
                (self.bundle.tenant_id, self.bundle.raw_subject,
                 self.bundle.result_id)).fetchone()[0]
        return (table, markers, rows), fixture_rows


class RollbackRefusalTests(_LiveRollbackBase):
    """Every refusal path leaves data, table, and marker intact."""

    def test_nonbypass_owner_view_is_hidden_without_binding(self):
        if self.effective_role_is_bypassrls():
            raise unittest.SkipTest(
                "the DSN role holds BYPASSRLS; the non-bypass owner view "
                "needs a restricted owner role - disclosed, not faked")
        self.register_original()
        with self.conn.transaction():
            self.conn.execute("set local statement_timeout = '2s'")
            hidden = self.conn.execute(
                "select count(*) from result_context").fetchone()[0]
        self.assertEqual(hidden, 0,
                         "FORCE RLS binds the owner too: no tenant binding, "
                         "zero rows")
        with self.conn.transaction():
            self.conn.execute("set local statement_timeout = '2s'")
            self.conn.execute(
                "select set_config('app.tenant_id', %s, true)",
                (self.bundle.other_tenant_id,))
            other = self.conn.execute(
                "select count(*) from result_context").fetchone()[0]
        self.assertEqual(other, 0, "another tenant's binding: zero rows")
        with self.conn.transaction():
            self.conn.execute("set local statement_timeout = '2s'")
            self.conn.execute(
                "select set_config('app.tenant_id', %s, true)",
                (self.bundle.tenant_id,))
            own = self.conn.execute(
                "select count(*) from result_context").fetchone()[0]
        self.assertEqual(own, 1, "the own tenant binding shows the rows")

    def test_privileged_populated_refusal_preserves_everything(self):
        if not self.effective_role_is_elevated():
            raise unittest.SkipTest(
                "the draft requires an elevated effective role by design; "
                "this DSN role is not elevated - disclosed, not faked")
        self.register_original()
        before, fixture_rows = self.rollback_preservation_state()
        self.assertTrue(before[0])
        self.assertEqual(before[1], [('result_context_version', '1')])
        self.assertEqual(fixture_rows, 1, "the current fixture exists by identity")
        # The draft raises a generic EXCEPTION with a specific message,
        # not a specific psycopg error code.
        try:
            with self.assertRaises(psycopg.errors.Error) as caught:
                self.conn.execute(ROLLBACK_PATH.read_text(encoding="utf-8"))
        finally:
            # The draft opens its own explicit transaction even in autocommit.
            self.conn.rollback()
        self.assertIn("result_context is populated", str(caught.exception))
        after, fixture_rows = self.rollback_preservation_state()
        self.assertEqual(after, before,
                         "refusal: table, marker, and rows all intact")
        self.assertEqual(fixture_rows, 1, "the current fixture remains by identity")

    def test_unauthorized_role_refused_even_on_empty_context(self):
        self.conn.execute("set role marker_app")
        try:
            rows = self.conn.execute(
                "select count(*) from result_context").fetchone()[0]
            self.assertEqual(rows, 0, "the fresh bundle has no context rows")
            with self.assertRaises(psycopg.errors.Error) as caught:
                self.conn.execute(
                    ROLLBACK_PATH.read_text(encoding="utf-8"))
            self.assertIn("rollback refused", str(caught.exception))
        finally:
            self.conn.rollback()
            self.conn.execute("reset role")
        table, marker, _rows = self.context_state()
        self.assertTrue(table)
        self.assertEqual(marker, 1)

    def test_lock_writer_blocks_the_draft_atomically(self):
        if not self.effective_role_is_elevated():
            raise unittest.SkipTest(
                "the draft requires an elevated effective role by design; "
                "this DSN role is not elevated - disclosed, not faked")
        self.register_original()
        before, fixture_rows = self.rollback_preservation_state()
        self.assertTrue(before[0])
        self.assertEqual(fixture_rows, 1, "the current fixture exists by identity")
        blocker = psycopg.connect(self.dsn, autocommit=True)
        try:
            blocker.execute("begin")
            blocker.execute(
                "lock table public.result_context in access exclusive mode")
            candidate = psycopg.connect(self.dsn, autocommit=True)
            try:
                candidate.execute("begin")
                candidate.execute("set local lock_timeout = '100ms'")
                with self.assertRaises(psycopg.errors.LockNotAvailable):
                    candidate.execute(
                        ROLLBACK_PATH.read_text(encoding="utf-8"))
            finally:
                try:
                    candidate.rollback()
                finally:
                    candidate.close()
        finally:
            try:
                blocker.rollback()
            finally:
                blocker.close()
        # Verification starts only after both owned sessions release their locks.
        after, fixture_rows = self.rollback_preservation_state()
        self.assertEqual(after, before,
                         "refusal preserves all context rows and marker values")
        self.assertEqual(fixture_rows, 1, "the same fixture remains by identity")

    def test_empty_privileged_success_is_future_only(self):
        if not self.approval.rollback_drill_authorized:
            raise unittest.SkipTest(
                "the EMPTY privileged success runs ONLY against a "
                "separately named sacrificial target with an explicit "
                "rollback-drill authorization; the populated-target "
                "refusals above are always exercised")
        if not self.approval.rollback_drill_dsn:
            raise unittest.SkipTest(
                "the rollback-drill authorization names no sacrificial "
                "target DSN")
        drill = psycopg.connect(self.approval.rollback_drill_dsn,
                                autocommit=True)
        try:
            state = drill.execute(
                "select to_regclass('public.result_context') is not null,"
                " (select count(*) from schema_meta where key ="
                " 'result_context_version')").fetchone()
            self.assertEqual(state, (True, 1),
                             "the sacrificial target is provisioned and "
                             "empty")
            drill.execute(ROLLBACK_PATH.read_text(encoding="utf-8"))
            after = drill.execute(
                "select to_regclass('public.result_context') is not null,"
                " (select count(*) from schema_meta where key ="
                " 'result_context_version')").fetchone()
            self.assertEqual(after, (False, 0),
                             "empty-only removal: objects and capability "
                             "marker gone, everything else untouched")
        finally:
            drill.close()


class Stage2ComparisonLogicTests(unittest.TestCase):
    """The stage-2 comparison LOGIC against controlled connection doubles.
    Clearly NOT a PostgreSQL proof: canned catalog rows and canned
    privilege answers exercise the diff detection, the expected-identity
    comparison, and the attested-server binding - the live proof stays
    behind the gate."""

    def setUp(self):
        self.manifest_record, _digest = \
            br02_pg_target.synthetic_approved_manifest()
        self.approved = br02_pg_target.supply_approved_manifest(
            self.manifest_record, br02_pg_target.manifest_digest(
                self.manifest_record))
        self.approval = fixtures.SeedApproval(
            approved_manifest=self.approved,
            execution_authorized=True,
            target_dsn="postgresql://inert-target/db",
            runtime_login="marker_app_login",
            attested_server_address="127.0.0.1",
            attested_server_port=5432,
            attested_server_version_prefix="PostgreSQL",
            run_marker="synthetic-run-0001",
        )
        gate = mock.patch.object(br02_pg_target, "authorize_execution")
        gate.start()
        self.addCleanup(gate.stop)

    def _double(self, privilege_overrides=(),
                run_marker="synthetic-run-0001"):
        return _CatalogDouble(privilege_overrides, run_marker=run_marker)

    def test_consistent_catalog_reports_zero_diffs(self):
        report = br02_pg_target.stage2_run_catalog_comparison(
            self._double(), self.approval, self.approved)
        self.assertEqual(report["diffs"], [], repr(report["diffs"]))
        self.assertTrue(report["passed"])
        self.assertIn("attested_server_identity", report["checked"])
        self.assertIn("effective_table_privileges", report["checked"])
        self.assertIn("membership_acl_inventory", report["checked"])

    def test_a_privilege_deviation_is_detected(self):
        report = br02_pg_target.stage2_run_catalog_comparison(
            self._double(privilege_overrides=[
                ("marker_app", "result_context", "UPDATE")]),
            self.approval, self.approved)
        diffs = [d for d in report["diffs"]
                 if d["check"] == "effective_table_privileges"]
        self.assertEqual(len(diffs), 1, repr(report["diffs"]))
        self.assertIn("marker_app", diffs[0]["detail"])
        self.assertIn("result_context", diffs[0]["detail"])
        self.assertIn("UPDATE", diffs[0]["detail"])
        self.assertFalse(report["passed"])

    def test_server_identity_mismatch_is_detected(self):
        approval = fixtures.SeedApproval(
            approved_manifest=self.approved,
            execution_authorized=True,
            target_dsn="postgresql://inert-target/db",
            runtime_login="marker_app_login",
            attested_server_address="10.9.9.9",  # NOT the double's address
            attested_server_port=5432,
            attested_server_version_prefix="PostgreSQL",
            run_marker="synthetic-run-0001",
        )
        report = br02_pg_target.stage2_run_catalog_comparison(
            self._double(), approval, self.approved)
        identity_diffs = [d for d in report["diffs"]
                          if d["check"] == "attested_server_identity"]
        self.assertTrue(identity_diffs,
                        "an attested-address mismatch must surface")
        self.assertFalse(report["passed"])

    def test_actual_marker_mismatch_is_detected(self):
        report = br02_pg_target.stage2_run_catalog_comparison(
            self._double(run_marker="another-run"), self.approval,
            self.approved)
        marker_diffs = [d for d in report["diffs"]
                        if d["check"] == "synthetic_run_marker"]
        self.assertEqual(len(marker_diffs), 2, repr(report["diffs"]))
        self.assertFalse(report["passed"])

    def test_missing_actual_marker_refuses_the_initialized_stage(self):
        with self.assertRaises(br02_pg_target.TargetRefused) as caught:
            br02_pg_target.stage2_run_catalog_comparison(
                self._double(run_marker=_MISSING_MARKER), self.approval,
                self.approved)
        self.assertEqual(caught.exception.reason,
                         br02_pg_target.TARGET_NOT_INITIALIZED)


_MISSING_MARKER = object()


class _CatalogDouble:
    """Controlled connection double: responds to exactly the stage-2
    catalog queries with canned rows derived from the expected identity,
    including the ACTUAL synthetic run marker read from schema_meta."""

    def __init__(self, privilege_overrides=(),
                 run_marker="synthetic-run-0001"):
        self.privilege_overrides = set(privilege_overrides)
        self.run_marker = run_marker

    def execute(self, sql, params=None):
        lowered = " ".join(sql.lower().split())
        if "inet_server_addr" in lowered:
            return _One(("127.0.0.1", 5432, "PostgreSQL 16.1 (synthetic)",
                         "synthetic_disposable", "marker_app_login",
                         "marker_app_login"))
        if "from schema_meta where key = %s" in lowered:
            if self.run_marker is _MISSING_MARKER:
                return _Many([])
            return _One((self.run_marker,))
        if "select cls.relname from pg_catalog.pg_class cls" in lowered:
            return _Many([(name,) for name in
                          sorted(br02_pg_target.EXPECTED_CATALOG)])
        if "select att.attname from pg_catalog.pg_attribute att" in lowered:
            if params:
                table = params[0]
                return _Many([(column,) for column in
                              sorted(br02_pg_target.EXPECTED_CATALOG[table])])
            return _Many([(column,) for column in
                          sorted(br02_pg_target.EXPECTED_CATALOG[
                              "tenant_membership"])])
        if "information_schema.tables" in lowered:
            return _Many([(name,) for name in
                          sorted(br02_pg_target.EXPECTED_CATALOG)])
        if "information_schema.columns" in lowered:
            table = params[0]
            return _Many([(column,) for column in
                          sorted(br02_pg_target.EXPECTED_CATALOG[table])])
        if "from pg_constraint where conrelid" in lowered \
                and "contype" not in lowered:
            table = params[0].split(".", 1)[1]
            names = list(br02_pg_target.EXPECTED_CONSTRAINTS.get(table, ()))
            names.append(f"{table}_pkey")
            return _Many([(name,) for name in names])
        if "contype = 'p'" in lowered:
            return _One((1,))
        if "from schema_meta" in lowered:
            return _Many([(key, value) for key, value in
                          sorted(br02_pg_target
                                 .EXPECTED_SCHEMA_MARKERS.items())])
        if "rolreplication" in lowered:
            return _Many([("marker_app", False, False, False, False,
                           False, False),
                          ("marker_worker", False, False, False, False,
                           False, False)])
        if "relforcerowsecurity" in lowered:
            return _Many([(table, True, True) for table in
                          br02_pg_target.EXPECTED_RLS_FORCED_TABLES])
        if "has_schema_privilege" in lowered:
            # Runtime roles: USAGE holds, CREATE never (the double answers
            # exactly the approved expectation).
            if "create" in lowered:
                return _One((False,))
            return _One((True,))
        if "has_table_privilege" in lowered:
            role, table, verb = params
            if (role, table, verb) in self.privilege_overrides:
                return _One((True,))
            expected = br02_pg_target.EXPECTED_EFFECTIVE_TABLE_PRIVS.get(
                table, {}).get(role, ())
            return _One((verb in expected,))
        if "pg_sequences" in lowered:
            return _Many([])
        if "has_column_privilege" in lowered:
            if len(params) == 4:
                role, table, column, verb = params
            else:
                # The worker-membership probe binds only the column and
                # inlines marker_worker / tenant_membership / SELECT. Dispatch
                # the same migration-derived matrix, never a blind False.
                role, table, verb = "marker_worker", "tenant_membership", \
                    "SELECT"
                column = params[0]
                if ("has_column_privilege('marker_worker',"
                        " 'tenant_membership', %s, 'select')") not in lowered:
                    raise AssertionError(
                        "unexpected has_column_privilege query shape: "
                        f"{lowered[:100]}")
            expected = (br02_pg_target.EXPECTED_EFFECTIVE_COLUMN_PRIVS
                        .get(table, {}).get(verb, {}).get(role, ()))
            return _One((column in expected,))
        if "aclexplode" in lowered:
            return _Many([])
        if "pg_auth_members" in lowered:
            return _One((0,))
        raise AssertionError(f"unexpected stage-2 query: {lowered[:100]}")


class _One:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row

    def fetchall(self):
        return [self._row]


class _Many:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class RollbackP2OfflineRegressionTests(unittest.TestCase):
    """Recording doubles only; no live setup, database or SQL execution."""

    def _populated_refusal(self, mutation=None):
        from types import SimpleNamespace

        case = RollbackRefusalTests()
        case.register_original = mock.Mock()
        case.effective_role_is_elevated = mock.Mock(return_value=True)
        before = (True, [('result_context_version', '1')],
                  [('retained-row',), ('current-row',)])
        after = (True, [('result_context_version', 'changed')], before[2]) \
            if mutation == 'marker' else (
                True, before[1], [('altered-row',), ('current-row',)]) \
            if mutation == 'row' else before
        order = []

        def snapshot():
            if not order:
                order.append('before')
                return before, 1
            self.assertEqual(order, ['before', 'refusal', 'rollback'])
            order.append('after')
            return after, 0 if mutation == 'fixture' else 1

        case.rollback_preservation_state = mock.Mock(side_effect=snapshot)
        case.context_state = mock.Mock(side_effect=AssertionError(
            'count-only preservation check must not be used'))
        case.conn = mock.Mock()
        failure = type('SyntheticPopulatedRefusal', (Exception,), {})

        def refuse(sql):
            self.assertEqual(sql, ROLLBACK_PATH.read_text(encoding='utf-8'))
            order.append('refusal')
            raise failure('result_context is populated')

        case.conn.execute.side_effect = refuse
        case.conn.rollback.side_effect = lambda: order.append('rollback')
        pg = SimpleNamespace(errors=SimpleNamespace(Error=failure))
        with mock.patch.object(sys.modules[__name__], 'psycopg', pg):
            try:
                case.test_privileged_populated_refusal_preserves_everything()
            finally:
                case.conn.rollback.assert_called_once_with()
        self.assertEqual(order, ['before', 'refusal', 'rollback', 'after'])
        self.assertEqual(case.rollback_preservation_state.call_count, 2)
        case.register_original.assert_called_once_with()

    def test_populated_refusal_preserves_full_snapshot_after_recovery(self):
        self._populated_refusal()

    def test_populated_refusal_rejects_same_count_changes_or_missing_fixture(self):
        for mutation in ('row', 'marker', 'fixture'):
            with self.subTest(mutation=mutation):
                with self.assertRaises(AssertionError):
                    self._populated_refusal(mutation)

    def test_rls_bindings_survive_until_their_reads_and_expire_on_exit(self):
        from contextlib import contextmanager

        case = RollbackRefusalTests()
        case.bundle = fixtures.build_bundle()
        case.register_original = mock.Mock()
        case.effective_role_is_bypassrls = mock.Mock(return_value=False)
        active, binding, sequence, reads = [False], [None], [0], []
        connection = mock.Mock()

        @contextmanager
        def transaction():
            self.assertFalse(active[0], "nested/unowned transaction")
            active[0] = True
            sequence[0] += 1
            try:
                yield
            finally:
                active[0], binding[0] = False, None

        def execute(sql, params=None):
            self.assertTrue(active[0], "binding/read outside owned transaction")
            if "set_config" in sql:
                binding[0] = params[0]
            if "count(*)" in sql:
                reads.append((sequence[0], binding[0]))
                return _One((int(binding[0] == case.bundle.tenant_id),))
            return _One((None,))

        connection.transaction.side_effect = transaction
        connection.execute.side_effect = execute
        case.conn = connection
        case.test_nonbypass_owner_view_is_hidden_without_binding()
        self.assertEqual(reads, [(1, None), (2, case.bundle.other_tenant_id),
                                 (3, case.bundle.tenant_id)])
        self.assertFalse(active[0])
        self.assertIsNone(binding[0])

    def test_preservation_snapshot_includes_all_rows_marker_and_fixture_key(self):
        from contextlib import contextmanager

        case = RollbackRefusalTests()
        case.bundle = fixtures.build_bundle()
        active, calls = [False], []
        rows = [('previous-row-a',), ('previous-row-b',), ('current-row',)]
        markers = [('result_context_version', '1')]
        connection = mock.Mock()

        @contextmanager
        def transaction():
            active[0] = True
            try:
                yield
            finally:
                active[0] = False

        def execute(sql, params=None):
            self.assertTrue(active[0])
            calls.append((sql, params))
            if "to_regclass" in sql:
                return _One((True,))
            if "schema_meta" in sql:
                return _Many(markers)
            if "to_jsonb" in sql:
                return _Many(rows)
            if "count(*)" in sql:
                self.assertEqual(params, (case.bundle.tenant_id,
                                         case.bundle.raw_subject,
                                         case.bundle.result_id))
                return _One((1,))
            return _One((None,))

        connection.transaction.side_effect = transaction
        connection.execute.side_effect = execute
        case.conn = connection
        snapshot, fixture_rows = case.rollback_preservation_state()
        self.assertEqual(snapshot, (True, markers, rows))
        self.assertEqual(fixture_rows, 1)
        self.assertEqual(calls[0][0], "set local statement_timeout = '2s'")
        self.assertIn("order by tenant_id, membership_subject, result_id",
                      calls[3][0])

    def _lock_refusal(self, changed_rows=False, missing_fixture=False):
        from types import SimpleNamespace

        case = RollbackRefusalTests()
        case.bundle = fixtures.build_bundle()
        case.dsn = "synthetic-not-a-dsn"
        case.register_original = mock.Mock()
        case.effective_role_is_elevated = mock.Mock(return_value=True)
        order = []
        before = (True, [('result_context_version', '1')],
                  [('prior-row',), ('current-row',), ('retained-row',)])
        after = (True, before[1], [('changed-row',), *before[2][1:]]) \
            if changed_rows else before

        def snapshot():
            if not order:
                order.append("before")
                return before, 1
            self.assertEqual(order[-2:], ["blocker rollback", "blocker close"])
            order.append("after")
            return after, 0 if missing_fixture else 1

        case.rollback_preservation_state = mock.Mock(side_effect=snapshot)
        failure = type("SyntheticLockTimeout", (Exception,), {})
        blocker, candidate = mock.Mock(), mock.Mock()
        blocker.rollback.side_effect = lambda: order.append("blocker rollback")
        blocker.close.side_effect = lambda: order.append("blocker close")
        candidate.rollback.side_effect = lambda: order.append("candidate rollback")
        candidate.close.side_effect = lambda: order.append("candidate close")

        def execute(sql):
            order.append(sql)
            if "result_context is populated" in sql:
                raise failure("synthetic refusal")

        candidate.execute.side_effect = execute
        pg = SimpleNamespace(connect=mock.Mock(side_effect=[blocker, candidate]),
                             errors=SimpleNamespace(LockNotAvailable=failure))
        with mock.patch.object(sys.modules[__name__], "psycopg", pg):
            case.test_lock_writer_blocks_the_draft_atomically()
        self.assertEqual(case.rollback_preservation_state.call_count, 2)
        self.assertEqual(order[0], "before")
        self.assertEqual(order[-1], "after")

    def test_lock_refusal_preserves_retained_rows_and_releases_before_readback(self):
        self._lock_refusal()

    def test_lock_refusal_rejects_same_count_row_change_or_missing_fixture(self):
        for option in ("changed_rows", "missing_fixture"):
            with self.subTest(option=option):
                with self.assertRaises(AssertionError):
                    self._lock_refusal(**{option: True})


_One_false = _One((False,))  # kept for clarity of the canned-answer shape


if __name__ == "__main__":
    unittest.main()
