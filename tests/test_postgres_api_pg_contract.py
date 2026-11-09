"""PostgreSQL production persistence contract tests (gated; never executed
locally).

Layer naming (deliberate, to avoid the earlier misleading labels):

1. ``Offline static wiring`` (no database): validates the production
   composition path the offline suites cannot reach - ``require_capability``
   fail-closed, service identity translation, and the migration/rollback SQL
   invariants (RLS ENABLE+FORCE, SELECT/INSERT-only grants, immutability
   trigger, capability marker distinct from schema_version, composite FKs,
   guarded rollback shape). These run without any DSN.

2. ``Live PostgreSQL store-adapter contract`` (skipped unless every gate
   layer passes): the STORE/SERVICE ADAPTER layer used by the production API -
   snapshot read-back, concurrent identical registration, idempotent replay,
   fault-rollback that preserves a separately committed original at revision
   0 with no events, and cross-tenant denial. This is NOT the HTTP API layer;
   the API composition itself is verified offline in the static-wiring class.
   The snapshot read-back test is single-writer (it never had a concurrent
   writer; the earlier label claimed one).

Live gating is lazy (inside ``setUpClass``, strictly before any connection,
never at import/discovery, never via ``AI_MARKER_PG_DSN``): psycopg present,
``AI_MARKER_TEST_PG_DSN`` set, the future independently supplied seed
approval present, and the module-level execution gate open - a DSN alone
never unlocks anything. Setup performs NO implicit migrations (capability
markers are checked read-only) and NO deletes, truncates, or ON CONFLICT
reactivation: fixture rows come from the explicit non-production seeder
under one unique run ID, and a disposable database is dropped whole by the
operator. After seeding, every assertion goes through the real runtime
adapters; nothing about the production policy is weakened.

The live API-child launcher (``br02_pg_api_child``) is additionally covered
offline: separate allowlisted environment, restricted (never administrative)
runtime handle, forced production policy, delayed ``api.server`` import, and
a disclosed synthetic verifier with no fake persistence or provenance.
"""
import importlib
import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))      # tests/

try:
    import psycopg
except ImportError:  # pragma: no cover - driver ships in the test manifest
    psycopg = None

import audit_store
import br02_pg_fixtures as fixtures
import tests.br02_pg_target as br02_pg_target
import decision_core
import identity_context
import postgres_result_store
import result_service
import runtime_config
from api.app import create_app

ROOT = Path(__file__).resolve().parents[1]
TENANT = "school-pgapi"
SUBJECT = "teacher-pgapi"
OWNER = "google:" + SUBJECT
RESULT = "manual:" + "p" * 16 + ":review-pgapi"
ORIGINAL = {"questions": [{"question_number": "1", "awarded_marks": 2,
                           "max_marks": 3}]}


def _test_dsn() -> str:
    """The dedicated test DSN variable only. Read lazily, never at import;
    the value is used solely for the gated connection and never echoed."""
    return os.environ.get(br02_pg_target.TEST_DSN_ENV, "").strip()


def _live_approval():
    """Placeholder for the future independently supplied seed approval.
    Returns None today: no approval exists, so the live classes always skip.
    Never inferred from a DSN, an environment variable, or a flag."""
    return None


def _live_gate():
    """Live gate, evaluated ONLY inside setUpClass and strictly before any
    connection. Order matters: the current unauthorized phase is refused
    BEFORE any environment value is read, any driver connect, or any module
    entrypoint runs - the dedicated test DSN variable is read only after
    every authorization layer passes."""
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
        return ("AI_MARKER_TEST_PG_DSN is not configured - live PostgreSQL "
                "production tests require a verified disposable synthetic "
                "DB; never AI_MARKER_PG_DSN")
    return None


class _FakeStore:
    """Minimal store double that records the owner passed to storage calls."""

    def register_result(self, *, owner, result_id, original_output):
        return {"owner": owner, "result_revision": 0}

    def commit_correction(self, *, owner, result_id, expected_revision,
                          idempotency_key, payload, event):
        return {"owner": owner, "status": "committed"}


class ProductionStaticWiringTests(unittest.TestCase):
    """Offline validation of production composition wiring (no DB needed;
    production policy is exercised here, never weakened)."""

    # -- identity translation -----------------------------------------------
    def test_service_translates_canonical_owner_to_membership_subject(self):
        store = _FakeStore()
        identity = identity_context.from_verified_subject(SUBJECT, TENANT)
        service = result_service.ResultService(store, identity)
        self.assertEqual(service.identity.membership_subject, SUBJECT)
        self.assertEqual(service.identity.owner_subject, OWNER)
        self.assertEqual(service.identity.tenant_id, TENANT)

    def test_service_refuses_a_foreign_canonical_owner(self):
        store = _FakeStore()
        identity = identity_context.from_verified_subject(SUBJECT, TENANT)
        service = result_service.ResultService(store, identity)
        with self.assertRaises(audit_store.AuditStoreError) as caught:
            service.register_result(owner="google:other", result_id=RESULT,
                                    original_output=ORIGINAL)
        self.assertEqual(caught.exception.code, "tenant_membership_required")

    def test_service_requires_verified_actor_for_correction(self):
        store = _FakeStore()
        identity = identity_context.from_verified_subject(SUBJECT, TENANT)
        service = result_service.ResultService(store, identity)
        event = {"actor": OWNER, "question_number": "1", "action": "correct"}
        service.commit_correction(owner=OWNER, result_id=RESULT,
                                  expected_revision=0, idempotency_key="k",
                                  payload={"question_number": "1"},
                                  event=event)
        with self.assertRaises(audit_store.AuditStoreError) as caught:
            service.commit_correction(owner=OWNER, result_id=RESULT,
                                      expected_revision=0,
                                      idempotency_key="k",
                                      payload={"question_number": "1"},
                                      event={**event, "actor": "google:other"})
        self.assertEqual(caught.exception.code, "audit_record_invalid")

    # -- fail-closed capability gate ----------------------------------------
    def test_configured_tracks_only_the_production_dsn(self):
        self.assertFalse(result_service.configured({}))
        self.assertFalse(result_service.configured(
            {runtime_config.PG_DSN_ENV: "  "}))
        self.assertTrue(result_service.configured(
            {runtime_config.PG_DSN_ENV: "postgresql://example/db"}))

    def test_open_service_without_a_dsn_fails_closed(self):
        identity = identity_context.from_verified_subject(SUBJECT, TENANT)
        for dsn in ("", "   ", None):
            with self.subTest(dsn=dsn):
                with self.assertRaises(audit_store.AuditStoreError) as caught:
                    result_service.open_service(identity, dsn=dsn)
                self.assertEqual(caught.exception.code, "store_unusable")

    def test_startup_capability_requires_the_production_dsn(self):
        with self.assertRaises(runtime_config.RuntimeConfigError) as caught:
            result_service.require_capability({})
        self.assertEqual(caught.exception.code, "store_unusable")

    # -- migration/rollback SQL invariants (static, no execution) ------------
    def _sql(self, name):
        return (ROOT / "schema" / name).read_text(encoding="utf-8").lower()

    def test_migration_sql_has_rls_enabled_and_forced(self):
        sql = self._sql("postgres_v9_result_context.sql")
        self.assertIn("enable row level security", sql)
        self.assertIn("force  row level security", sql)

    def test_migration_sql_grants_select_insert_only(self):
        sql = self._sql("postgres_v9_result_context.sql")
        self.assertIn("grant select, insert on result_context", sql)
        self.assertNotIn("grant update", sql)
        self.assertNotIn("grant delete", sql)

    def test_migration_sql_has_immutability_trigger(self):
        sql = self._sql("postgres_v9_result_context.sql")
        self.assertIn("before update or delete on result_context", sql)
        self.assertIn("result_context rows are immutable", sql)

    def test_migration_sql_capability_marker_distinct_from_schema_version(self):
        sql = self._sql("postgres_v9_result_context.sql")
        self.assertIn("result_context_version", sql)
        self.assertNotIn("schema_version', '2", sql)

    def test_migration_sql_has_composite_fk_constraints(self):
        sql = self._sql("postgres_v9_result_context.sql")
        self.assertIn("references results (tenant_id, owner_subject, result_id)",
                      sql)
        self.assertIn("references tenant_membership (tenant_id, subject)", sql)
        self.assertIn("fk_result_context_job", sql)
        self.assertIn("fk_result_context_assignment", sql)

    def test_rollback_sql_refuses_populated_originals(self):
        rollback = self._sql("postgres_v9_result_context_rollback.sql")
        self.assertIn("result_context is populated", rollback)
        self.assertIn("rollback refused", rollback)
        self.assertLess(rollback.index("raise exception"),
                        rollback.index("drop table if exists "
                                       "public.result_context"))

    def test_rollback_sql_requires_elevated_role_and_locks_first(self):
        rollback = self._sql("postgres_v9_result_context_rollback.sql")
        self.assertIn("rolsuper", rollback)
        self.assertIn("rolbypassrls", rollback)
        self.assertIn("access exclusive", rollback)
        self.assertLess(rollback.index("lock table"),
                        rollback.index("result_context is populated"))

    # -- production app composition (no DB) ---------------------------------
    def test_production_app_refuses_without_postgres_dsn(self):
        env = {runtime_config.RUNTIME_ENV: runtime_config.RUNTIME_PRODUCTION,
               runtime_config.PG_DSN_ENV: "",
               runtime_config.ALLOWED_ORIGINS_ENV: "https://marker.example",
               "GOOGLE_OIDC_AUDIENCE": "1234567890.apps.googleusercontent.com",
               "AI_MARKER_ENGINE_REAL": "0"}
        with mock.patch.dict("os.environ", env, clear=True):
            # A production app with no PG DSN must not serve via the dev store.
            with self.assertRaises(runtime_config.RuntimeConfigError) as caught:
                create_app(verify_token=lambda token: SUBJECT,
                           allowed_origins=["https://marker.example"])
            self.assertEqual(caught.exception.code, "store_unusable")


class ApiChildGateTests(unittest.TestCase):
    """Offline behavioral gating of the live API-child launcher. The
    execution gate is stubbed ONLY where a test explicitly needs to exercise
    the post-gate environment construction - a disclosed offline test double,
    never a real approval."""

    def _stubbed_gate(self):
        import tests.br02_pg_api_child as api_child_module
        return mock.patch.object(api_child_module.target, "authorize_execution",
                                 lambda *a, **k: None)

    def _synthetic_approval(self):
        import tests.br02_pg_api_child as api_child  # noqa: F401 - gated below
        manifest_record, digest = br02_pg_target.synthetic_approved_manifest()
        return fixtures.SeedApproval(
            approved_manifest=br02_pg_target.supply_approved_manifest(
                manifest_record, digest),
            execution_authorized=True,
            approved_by="synthetic",
            approval_ref="synthetic-ref",
            # The attested ADMINISTRATIVE target DSN is bound to the
            # approval object itself; the child runtime NEVER uses it. The
            # child gets the separately supplied RESTRICTED app_dsn bound to
            # runtime_login, and the manifest role inventory stays restricted.
            target_dsn="postgresql://inert-admin/db",
            app_dsn="postgresql://inert-app/db",
            runtime_login="marker_app_login",
        )

    def test_child_environ_refuses_without_approval(self):
        import tests.br02_pg_api_child as api_child

        # Without the gate stub, authorize_execution always refuses.
        # This test verifies the refusal code is EXECUTION_NOT_AUTHORIZED.
        with self.assertRaises(br02_pg_target.TargetRefused) as caught:
            api_child.build_child_environ({}, None,
                                          "postgresql://inert-target/db")
        self.assertEqual(caught.exception.reason,
                         br02_pg_target.EXECUTION_NOT_AUTHORIZED)

    def test_child_environ_is_allowlisted_and_production_forced(self):
        import tests.br02_pg_api_child as api_child

        approval = self._synthetic_approval()
        parent = {"PATH": "synthetic-path", "SYSTEMROOT": "synthetic-root",
                  "GEMINI_API_KEY": "synthetic-not-real",
                  "AI_MARKER_SUPABASE_SERVICE_KEY": "synthetic-not-real",
                  runtime_config.PG_DSN_ENV: "postgresql://inert-parent/db"}
        with self._stubbed_gate():
            child = api_child.build_child_environ(
                parent, approval, "postgresql://inert-app/db")
        self.assertEqual(child[runtime_config.RUNTIME_ENV],
                         runtime_config.RUNTIME_PRODUCTION)
        self.assertEqual(child[runtime_config.PG_DSN_ENV],
                         "postgresql://inert-app/db")
        self.assertEqual(child[runtime_config.ENGINE_REAL_ENV], "0")
        self.assertEqual(child[runtime_config.OIDC_AUDIENCE_ENV],
                         "synthetic-test-audience")
        self.assertEqual(child["PATH"], "synthetic-path")
        # Credentials and non-allowlisted variables never reach the child.
        self.assertNotIn("GEMINI_API_KEY", child)
        self.assertNotIn("AI_MARKER_SUPABASE_SERVICE_KEY", child)

    def test_child_refuses_reusing_the_parent_production_dsn(self):
        import tests.br02_pg_api_child as api_child

        approval = self._synthetic_approval()
        parent = {runtime_config.PG_DSN_ENV: "postgresql://inert-parent/db"}
        with self._stubbed_gate():
            with self.assertRaises(br02_pg_target.TargetRefused) as caught:
                api_child.build_child_environ(
                    parent, approval, "postgresql://inert-parent/db")
        self.assertEqual(caught.exception.reason,
                         br02_pg_target.PRODUCTION_DSN_FALLBACK)

    def test_child_never_accepts_the_administrative_target_dsn(self):
        import tests.br02_pg_api_child as api_child

        approval = self._synthetic_approval()
        with self._stubbed_gate():
            with self.assertRaises(br02_pg_target.TargetRefused) as caught:
                api_child.build_child_environ(
                    {}, approval, "postgresql://inert-admin/db")
        self.assertEqual(caught.exception.reason,
                         br02_pg_target.PRODUCTION_DSN_FALLBACK)

    def test_child_refuses_a_missing_or_misbound_restricted_identity(self):
        import tests.br02_pg_api_child as api_child

        manifest = br02_pg_target.supply_approved_manifest(
            *br02_pg_target.synthetic_approved_manifest())

        def approval(**overrides):
            values = {"approved_manifest": manifest,
                      "execution_authorized": True,
                      "target_dsn": "postgresql://inert-admin/db",
                      "app_dsn": "postgresql://inert-app/db",
                      "runtime_login": "marker_app_login"}
            values.update(overrides)
            return fixtures.SeedApproval(**values)

        with self._stubbed_gate():
            # No restricted identity at all: refused outright, with no
            # legacy/permissive branch even for this inert helper shape.
            with self.assertRaises(br02_pg_target.TargetRefused) as caught:
                api_child.build_child_environ(
                    {}, approval(app_dsn="", runtime_login=""),
                    "postgresql://inert-admin/db")
            self.assertEqual(caught.exception.reason,
                             br02_pg_target.TARGET_IDENTITY_INCOMPLETE)
            # Present but blank is equally refused.
            with self.assertRaises(br02_pg_target.TargetRefused) as caught:
                api_child.build_child_environ(
                    {}, approval(app_dsn="  ", runtime_login=" "),
                    "postgresql://inert-app/db")
            self.assertEqual(caught.exception.reason,
                             br02_pg_target.TARGET_IDENTITY_INCOMPLETE)
            # The restricted handle may never equal the admin handle.
            with self.assertRaises(br02_pg_target.TargetRefused) as caught:
                api_child.build_child_environ(
                    {}, approval(app_dsn="postgresql://inert-admin/db"),
                    "postgresql://inert-admin/db")
            self.assertEqual(caught.exception.reason,
                             br02_pg_target.INCONSISTENT_TARGET)

        for roles in (["postgres"], ["superuser"], ["marker_admin"],
                      ["marker_app", "root"]):
            with self.subTest(roles=roles):
                with self.assertRaises(br02_pg_target.TargetRefused) as caught:
                    br02_pg_target.assert_restricted_roles(roles)
                self.assertEqual(caught.exception.reason,
                                 br02_pg_target.INCONSISTENT_TARGET)

    def test_api_server_import_stays_behind_the_gate(self):
        import br02_pg_api_child as api_child  # noqa: F401

        sys.modules.pop("br02_pg_api_child", None)
        importlib.import_module("br02_pg_api_child")
        self.assertNotIn(
            "api.server", sys.modules,
            "api.server must be imported only behind the execution gate, "
            "never at module import")

    def test_launch_refuses_even_with_a_formed_synthetic_approval(self):
        import br02_pg_api_child as api_child

        approval = self._synthetic_approval()
        with self.assertRaises(br02_pg_target.TargetRefused) as caught:
            api_child.launch_child({}, ["python", "-c", "pass"], 5,
                                   approval=approval)
        self.assertEqual(caught.exception.reason,
                         br02_pg_target.EXECUTION_NOT_AUTHORIZED)

    def test_synthetic_verifier_is_disclosed(self):
        import br02_pg_api_child as api_child

        self.assertIn("SYNTHETIC", api_child.SYNTHETIC_VERIFIER_DISCLOSURE)


class _LivePgBase(unittest.TestCase):
    """Shared live-database base; skipped unless every gate layer passes.

    Setup NEVER applies migrations and NEVER deletes, truncates, or
    reactivates rows: capability markers are checked read-only, then the
    explicit non-production seeder plants one unique fixture bundle. There is
    no tear-down SQL at all - a disposable database is dropped whole by the
    operator.
    """

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
                "target schema capability missing - the disposable target "
                "must be provisioned by the approved operator process; "
                "tests never apply migrations implicitly")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "conn", None) is not None:
            cls.conn.close()

    def setUp(self):
        # One UNIQUE bundle per test: no context conflicts, no cleanup SQL,
        # rows retained. Each test owns its connection and closes it.
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        try:
            self.bundle = fixtures.build_bundle()
            fixtures.seed(self.conn, self.bundle, self.approval,
                          is_production=False)
        except Exception:
            self.conn.close()
            raise

    def tearDown(self):
        # No cleanup SQL at all: the disposable database is discarded whole
        # by the operator. Only the test's OWN connection is closed.
        if getattr(self, "conn", None) is not None:
            self.conn.close()

    def store_for(self, subject, tenant_id):
        return postgres_result_store.open_store(
            self.dsn, identity=identity_context.from_verified_subject(
                subject, tenant_id))

    def fixture_store(self):
        """Explicit non-production registration; runtime stores keep denial."""
        return postgres_result_store.open_store(
            self.dsn, identity=identity_context.from_verified_subject(
                self.bundle.raw_subject, self.bundle.tenant_id),
            fresh_completion_denied=False)


class PostgresStoreAdapterLiveContractTests(_LivePgBase):
    """Store/service ADAPTER persistence contract on live PostgreSQL
    (skipped locally; the gated class is the honest BR-02D gate, never a
    pass). NOT the HTTP API layer - the production API composition is
    verified offline above."""

    def test_snapshot_readback_is_consistent(self):
        # Single-writer snapshot read-back. A true concurrent-writer
        # consistency probe needs the future approved target; the earlier
        # label claimed a concurrent writer this single-threaded test does
        # not exercise.
        store = self.fixture_store()
        try:
            store.register_with_context(
                result_id=self.bundle.result_id,
                original_output=self.bundle.original_output,
                scope="manual", review_key=self.bundle.review_key,
                document_digest=self.bundle.document_digest)
            snap = store.snapshot(result_id=self.bundle.result_id)
            self.assertEqual(snap["original_output"],
                             self.bundle.original_output)
            self.assertEqual(snap["result_revision"], 0)
            self.assertEqual(snap["events"], [])
        finally:
            store.close()

    def test_concurrent_identical_register_is_idempotent_no_duplicate(self):
        created = []

        def register():
            store = self.fixture_store()
            try:
                created.append(store.register_with_context(
                    result_id=self.bundle.result_id,
                    original_output=self.bundle.original_output,
                    scope="manual", review_key=self.bundle.review_key))
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda _: register(), range(2)))
        self.assertEqual(len(created), 2)
        self.assertEqual(sum(1 for r in created if r.get("created")), 1)

    def test_idempotent_commit_correction_replays_saved_receipt(self):
        event = {"actor": self.bundle.canonical_owner,
                 "question_number": "1", "action": "correct",
                 "review_status": "corrected", "old_score": "2",
                 "new_score": "3", "reason": "rework", "binding": ""}
        payload = {"question_number": "1", "action": "correct",
                   "review_status": "corrected", "old_score": "2",
                   "new_score": "3", "reason": "rework", "binding": ""}
        idempotency_key = f"k-{self.bundle.run_id}"
        store = self.fixture_store()
        try:
            store.register_with_context(
                result_id=self.bundle.result_id,
                original_output=self.bundle.original_output,
                scope="manual", review_key=self.bundle.review_key)
            first = store.commit_correction(
                owner=self.bundle.raw_subject,
                result_id=self.bundle.result_id, expected_revision=0,
                idempotency_key=idempotency_key, payload=payload, event=event)
            self.assertEqual(first["status"], "committed")
            second = store.commit_correction(
                owner=self.bundle.raw_subject,
                result_id=self.bundle.result_id, expected_revision=0,
                idempotency_key=idempotency_key, payload=payload, event=event)
            self.assertEqual(second["status"], "duplicate")
            self.assertEqual(second["sequence"], first["sequence"])
            self.assertEqual(
                len(store.history(result_id=self.bundle.result_id)["events"]),
                1)
        finally:
            store.close()

    def test_fault_before_commit_preserves_committed_original(self):
        # Separately committed original at revision 0, no events.
        store = self.fixture_store()
        try:
            receipt = store.register_with_context(
                result_id=self.bundle.result_id,
                original_output=self.bundle.original_output,
                scope="manual", review_key=self.bundle.review_key)
            self.assertEqual(receipt["result_revision"], 0)
        finally:
            store.close()
        # A faulted correction must roll back; the committed original survives.
        faulted = self.fixture_store()
        faulted.set_fault_hook(
            lambda stage: (_ for _ in ()).throw(
                audit_store.AuditStoreError("injected", "boom")))
        try:
            event = {"actor": self.bundle.canonical_owner,
                     "question_number": "1", "action": "correct",
                     "review_status": "corrected", "old_score": "2",
                     "new_score": "3", "reason": "r", "binding": ""}
            payload = {"question_number": "1", "action": "correct",
                       "review_status": "corrected", "old_score": "2",
                       "new_score": "3", "reason": "r", "binding": ""}
            with self.assertRaises(audit_store.AuditStoreError):
                faulted.commit_correction(
                    owner=self.bundle.raw_subject,
                    result_id=self.bundle.result_id, expected_revision=0,
                    idempotency_key=f"k-{self.bundle.run_id}",
                    payload=payload, event=event)
        finally:
            faulted.close()
        reopened = self.fixture_store()
        try:
            snap = reopened.snapshot(result_id=self.bundle.result_id)
            self.assertEqual(snap["original_output"],
                             self.bundle.original_output)
            self.assertEqual(snap["result_revision"], 0)
            self.assertEqual(snap["events"], [])
        finally:
            reopened.close()

    def test_cross_tenant_read_is_indistinguishable_from_absent(self):
        store = self.fixture_store()
        try:
            store.register_with_context(
                result_id=self.bundle.result_id,
                original_output=self.bundle.original_output,
                scope="manual", review_key=self.bundle.review_key)
        finally:
            store.close()
        other = self.store_for(self.bundle.other_subject,
                               self.bundle.other_tenant_id)
        try:
            self.assertIsNone(other.visible_row(self.bundle.result_id))
            with self.assertRaises(audit_store.AuditStoreError) as caught:
                other.snapshot(result_id=self.bundle.result_id)
            self.assertEqual(caught.exception.code, "result_not_registered")
        finally:
            other.close()

    def test_runtime_roles_hold_select_insert_only_on_result_context(self):
        # Runtime roles (marker_app/marker_worker) must not hold UPDATE/DELETE.
        with psycopg.connect(self.dsn) as probe:
            probe.execute("SET ROLE marker_app")
            probe.execute("SELECT set_config('app.tenant_id', %s, true)",
                          (self.bundle.tenant_id,))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                probe.execute(
                    "UPDATE result_context SET review_key = review_key "
                    "WHERE result_id = %s", (self.bundle.result_id,))
            probe.rollback()
            probe.execute("SELECT set_config('app.tenant_id', %s, true)",
                          (self.bundle.tenant_id,))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                probe.execute(
                    "DELETE FROM result_context WHERE result_id = %s",
                    (self.bundle.result_id,))


if __name__ == "__main__":
    unittest.main()
