"""BR-02 result-context contract tests (offline + PG-gated).

Offline tests pin the pieces that do not need a server:
* explicit verified-subject -> membership-subject -> canonical-owner mapping;
* the production service's owner translation and actor check (service layer,
  distinct from the store adapter layer);
* fail-closed composition (missing DSN / missing capability marker);
* the non-production fixture seeder's gating and INSERT-only plan;
* the migration/rollback SQL invariants the security review depends on.

The PostgreSQL contract class runs ONLY when all of the following hold, and
the gate is evaluated lazily inside ``setUpClass`` - never at import or
discovery, and never by reading ``AI_MARKER_PG_DSN``:

* the psycopg driver is installed;
* ``AI_MARKER_TEST_PG_DSN`` points at an independently verified disposable
  synthetic database;
* the future independently supplied seed approval exists AND the module-level
  execution gate allows live execution (a DSN alone never unlocks anything).

Setup performs NO implicit migrations: the capability markers are checked and
the class skips when the target is not already provisioned. All fixture rows
come from the explicit non-production seeder (``br02_pg_fixtures``) under one
unique run ID, so nothing is ever deleted, truncated, or reactivated. After
seeding, every assertion goes through the real runtime adapters. With no such
target (the local case), the class skips with an explicit reason - that skip
is the honest gate, never a pass.
"""
import os
import sys
import types
import unittest
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
import br02_pg_target
import identity_context
import postgres_result_store
import result_service
import runtime_config

ROOT = Path(__file__).resolve().parents[1]
TENANT = "school-br02"
SUBJECT = "teacher-br02"
OWNER = "google:" + SUBJECT
RESULT = "manual:" + "a" * 16 + ":review-br02"
ORIGINAL = {"questions": [{"question_number": "1", "awarded_marks": 2,
                           "max_marks": 3, "student_answer": "SYNTHETIC"}]}


def _test_dsn() -> str:
    """The dedicated test DSN variable only. Read lazily, never at import;
    the value is used solely for the gated connection and never echoed."""
    return os.environ.get(br02_pg_target.TEST_DSN_ENV, "").strip()


def _live_approval():
    """Placeholder for the future independently supplied seed approval.

    Returns None today: no approval exists, so the live classes always skip.
    When the lead supplies the attested target and execution authorization,
    this returns the explicit ``fixtures.SeedApproval`` object - it is never
    inferred from a DSN, an environment variable, or a flag.
    """
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
        return ("AI_MARKER_TEST_PG_DSN is not configured - the BR-02 "
                "PostgreSQL gate is BLOCKED locally and runs only against a "
                "verified disposable synthetic DB; never AI_MARKER_PG_DSN")
    return None


class IdentityContextTests(unittest.TestCase):
    def test_verified_subject_maps_to_all_three_representations(self):
        context = identity_context.from_verified_subject(SUBJECT, TENANT)
        self.assertEqual(context.verified_subject, SUBJECT)
        self.assertEqual(context.membership_subject, SUBJECT)
        self.assertEqual(context.owner_subject, OWNER)
        self.assertEqual(context.tenant_id, TENANT)

    def test_an_existing_google_prefix_is_never_stripped(self):
        # Stripping a client-shaped prefix would let a caller choose the
        # canonical owner; the prefix is always ADDED, exactly once.
        context = identity_context.from_verified_subject("google:alice", TENANT)
        self.assertEqual(context.membership_subject, "google:alice")
        self.assertEqual(context.owner_subject, "google:google:alice")

    def test_actor_owner_must_be_exactly_the_canonical_form(self):
        context = identity_context.from_actor(SUBJECT, OWNER, TENANT)
        self.assertEqual(context.membership_subject, SUBJECT)
        for bad in ("teacher-br02", "Google:" + SUBJECT, OWNER + " "):
            with self.subTest(bad=bad):
                with self.assertRaises(
                        identity_context.IdentityContextError):
                    identity_context.from_actor(SUBJECT, bad, TENANT)

    def test_trusted_owner_must_match_the_membership_subject_exactly(self):
        context = identity_context.from_trusted_owner(OWNER, TENANT, SUBJECT)
        self.assertEqual(context.owner_subject, OWNER)
        with self.assertRaises(identity_context.IdentityContextError):
            identity_context.from_trusted_owner(OWNER, TENANT, "other")

    def test_blank_oversized_and_control_values_fail_closed(self):
        for bad in ("", "   ", None, 7, "x" * 600, "a\nb"):
            with self.subTest(bad=bad):
                with self.assertRaises(identity_context.IdentityContextError):
                    identity_context.from_verified_subject(bad, TENANT)


class _RecordingStore:
    """Minimal store double for ResultService translation tests."""

    def __init__(self):
        self.calls = []
        self.register_result_calls = 0

    def register_result(self, **kwargs):
        self.calls.append(("register_result", kwargs))
        self.register_result_calls += 1
        return {"result_revision": 0, "original_digest": "d" * 64}

    def original_record(self, **kwargs):
        self.calls.append(("original_record", kwargs))
        self.register_result_calls += 1
        return {"original_digest": audit_store.digest_value(ORIGINAL),
                "result_revision": 0,
                "original_scores": audit_store.original_score_projection(ORIGINAL)}

    def context_record(self, **kwargs):
        self.calls.append(("context_record", kwargs))
        return {"original_digest": audit_store.digest_value(ORIGINAL),
                "owner_subject": OWNER,
                "membership_subject": SUBJECT,
                "tenant_id": TENANT}

    def commit_correction(self, **kwargs):
        self.calls.append(("commit_correction", kwargs))
        return {"status": "committed"}

    def close(self):
        self.calls.append(("close", {}))


class ResultServiceTranslationTests(unittest.TestCase):
    """Service-layer owner translation and actor checks (NOT the store
    adapter layer; storage behaviour is contracted below and, live, in the
    gated class)."""

    def setUp(self):
        self.store = _RecordingStore()
        self.identity = identity_context.from_verified_subject(SUBJECT, TENANT)
        self.service = result_service.ResultService(self.store, self.identity)

    def test_register_result_validates_context_and_anchor(self):
        self.service.register_result(owner=OWNER, result_id=RESULT,
                                     original_output=ORIGINAL)
        # context_record is called FIRST (read-only validation), then
        # original_record. register_result is never invoked (no creation).
        names = [name for name, _ in self.store.calls]
        self.assertEqual(names[0], "context_record")
        self.assertEqual(names[1], "original_record")
        self.assertNotIn("register_result", names)
        # context_record is called with just result_id
        name, kwargs = self.store.calls[0]
        self.assertEqual(name, "context_record")
        self.assertEqual(kwargs, {"result_id": RESULT})
        # original_record is called with owner=SUBJECT (membership subject)
        name, kwargs = self.store.calls[1]
        self.assertEqual(name, "original_record")
        self.assertEqual(kwargs["owner"], SUBJECT)
        self.assertEqual(kwargs["result_id"], RESULT)

    def test_register_result_refuses_missing_context(self):
        self.store.calls = []
        from identity_context import IdentityContext
        identity = identity_context.from_verified_subject(SUBJECT, TENANT)
        service = result_service.ResultService(self.store, identity)
        original_context_record = self.store.context_record

        def failing_context_record(**kwargs):
            raise audit_store.AuditStoreError(
                "result_not_registered", "no result context for this owner")
        self.store.context_record = failing_context_record
        try:
            with self.assertRaises(audit_store.AuditStoreError) as caught:
                service.register_result(owner=OWNER, result_id=RESULT,
                                        original_output=ORIGINAL)
            self.assertEqual(caught.exception.code, "result_not_registered")
        finally:
            self.store.context_record = original_context_record
        # No register_result call was made
        self.assertEqual(self.store.register_result_calls, 0)

    def test_register_result_refuses_mismatched_digest(self):
        self.store.calls = []
        from identity_context import IdentityContext
        identity = identity_context.from_verified_subject(SUBJECT, TENANT)
        service = result_service.ResultService(self.store, identity)
        original_context_record = self.store.context_record

        def wrong_digest_context(**kwargs):
            return {"original_digest": "e" * 64,  # different digest
                    "owner_subject": OWNER,
                    "membership_subject": SUBJECT,
                    "tenant_id": TENANT}
        self.store.context_record = wrong_digest_context
        try:
            with self.assertRaises(audit_store.AuditStoreError) as caught:
                service.register_result(owner=OWNER, result_id=RESULT,
                                        original_output=ORIGINAL)
            self.assertEqual(caught.exception.code,
                             "original_output_immutable")
        finally:
            self.store.context_record = original_context_record
        # No register_result call was made
        self.assertEqual(self.store.register_result_calls, 0)

    def test_register_result_valid_existing_returns_receipt(self):
        self.store.calls = []
        from identity_context import IdentityContext
        identity = identity_context.from_verified_subject(SUBJECT, TENANT)
        service = result_service.ResultService(self.store, identity)
        result = service.register_result(owner=OWNER, result_id=RESULT,
                                         original_output=ORIGINAL)
        self.assertEqual(result["result_revision"], 0)
        self.assertEqual(result["original_digest"],
                         audit_store.digest_value(ORIGINAL))
        # original_record was called once (read-only validation)
        self.assertEqual(self.store.register_result_calls, 1)
        # No register_result write call was made
        self.assertEqual(sum(1 for name, _ in self.store.calls
                             if name == "register_result"), 0)

    def test_commit_correction_requires_the_verified_actor(self):
        event = {"actor": OWNER, "question_number": "1"}
        self.service.commit_correction(owner=OWNER, result_id=RESULT,
                                       expected_revision=0,
                                       idempotency_key="k",
                                       payload={"a": 1}, event=event)
        name, kwargs = self.store.calls[0]
        self.assertEqual(name, "commit_correction")
        self.assertEqual(kwargs["owner"], SUBJECT)
        with self.assertRaises(audit_store.AuditStoreError) as caught:
            self.service.commit_correction(
                owner=OWNER, result_id=RESULT, expected_revision=0,
                idempotency_key="k", payload={"a": 1},
                event={**event, "actor": "google:other"})
        self.assertEqual(caught.exception.code, "audit_record_invalid")


class FailClosedCompositionTests(unittest.TestCase):
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

    def test_open_store_closes_the_connection_when_capability_is_missing(self):
        closed = []
        fake = types.ModuleType("psycopg")

        class _Connection:
            def transaction(self):
                return _Transaction()

            def execute(self, sql, params=None):
                if "schema_version" in sql:
                    return _Result(("1",))
                if "to_regclass" in sql:
                    return _Result((None,))
                return _Result(None)

            def close(self):
                closed.append(True)

        class _Transaction:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        class _Result:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        def connect(dsn, **kwargs):
            return _Connection()

        fake.connect = connect
        identity = identity_context.from_verified_subject(SUBJECT, TENANT)
        with mock.patch.dict(sys.modules, {"psycopg": fake}):
            with self.assertRaises(audit_store.AuditStoreError) as caught:
                postgres_result_store.open_store(
                    "postgresql://disposable.example/db", identity=identity)
        self.assertEqual(caught.exception.code, "store_unusable")
        self.assertTrue(closed, "a failed capability check must close the "
                                "connection instead of leaking it")


class _RecordingConnection:
    """Connection double that records every statement without executing."""

    def __init__(self, precheck_rows=None):
        self.calls = []
        self.precheck_rows = list(precheck_rows or [])

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _ResultSet(list(self.precheck_rows))


class _ResultSet:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class FixtureSeederGateTests(unittest.TestCase):
    """Behavioral gating of the explicit non-production fixture seeder."""

    def setUp(self):
        self.bundle = fixtures.build_bundle(run_id="gate-run-0001")

    def test_bundle_identities_are_unique_and_deterministic(self):
        other = fixtures.build_bundle(run_id="gate-run-0002")
        mine = fixtures.build_bundle(run_id="gate-run-0001")
        self.assertEqual(mine, self.bundle)  # deterministic for one run_id
        disjoint = ("tenant_id", "raw_subject", "result_id", "job_id",
                    "assignment_id", "document_digest")
        for field in disjoint:
            self.assertNotEqual(getattr(self.bundle, field),
                                getattr(other, field), field)

    def test_seed_plan_is_insert_only_with_rls_bindings(self):
        plan = fixtures.seed_plan(self.bundle)
        self.assertTrue(plan)
        for statement, params in plan:
            lowered = statement.lower()
            self.assertTrue(
                lowered.startswith("select set_config")
                or lowered.startswith("insert into"),
                statement)
            for banned in ("on conflict", "delete", "truncate", "drop",
                           "alter", "update "):
                self.assertNotIn(banned, lowered)
            self.assertIsNotNone(params)

    def test_seed_refuses_without_approval_and_touches_nothing(self):
        connection = _RecordingConnection()
        with self.assertRaises(br02_pg_target.TargetRefused) as caught:
            fixtures.seed(connection, self.bundle, None,
                          is_production=False)
        self.assertEqual(caught.exception.reason,
                         br02_pg_target.EXECUTION_NOT_AUTHORIZED)
        self.assertEqual(connection.calls, [],
                         "the approval gate must precede every statement")

    def test_production_seeding_is_refused(self):
        connection = _RecordingConnection()
        with self.assertRaises(br02_pg_target.TargetRefused):
            fixtures.seed(connection, self.bundle, None,
                          is_production=True)
        self.assertEqual(connection.calls, [])

    def test_even_a_fully_formed_future_approval_is_refused_today(self):
        # A synthetic approval object (disclosed test data, never a real
        # approval) still cannot pass the unconditional execution gate.
        manifest_record, digest = br02_pg_target.synthetic_approved_manifest()
        approval = fixtures.SeedApproval(
            approved_manifest=br02_pg_target.supply_approved_manifest(
                manifest_record, digest),
            execution_authorized=True,
            approved_by="synthetic",
            approval_ref="synthetic-ref",
        )
        connection = _RecordingConnection()
        with self.assertRaises(br02_pg_target.TargetRefused) as caught:
            fixtures.seed(connection, self.bundle, approval,
                          is_production=False)
        self.assertEqual(caught.exception.reason,
                         br02_pg_target.EXECUTION_NOT_AUTHORIZED)
        self.assertEqual(connection.calls, [])

    def test_capability_precheck_refuses_without_implicit_migrations(self):
        connection = _RecordingConnection()  # no marker rows -> capability absent
        # A synthetic, well-formed approval object (disclosed test data)
        # passes the shape check so the test can reach the capability
        # precheck; the execution gate itself is stubbed as an offline test
        # double and must have been consulted exactly once.
        manifest_record, digest = br02_pg_target.synthetic_approved_manifest()
        approval = fixtures.SeedApproval(
            approved_manifest=br02_pg_target.supply_approved_manifest(
                manifest_record, digest),
            execution_authorized=True,
            approved_by="synthetic",
            approval_ref="synthetic-ref",
        )
        with mock.patch.object(br02_pg_target, "authorize_execution") as gate_stub:
            with self.assertRaises(br02_pg_target.TargetRefused) as caught:
                fixtures.seed(connection, self.bundle, approval,
                              is_production=False)
        gate_stub.assert_called_once()
        self.assertEqual(caught.exception.reason, br02_pg_target.MISSING_TARGET)
        self.assertEqual(len(connection.calls), 1)
        self.assertIn("schema_meta", connection.calls[0][0])
        self.assertNotIn("insert", connection.calls[0][0].lower())

    def test_bundle_provenance_shapes_match_the_real_schema(self):
        self.assertRegex(self.bundle.result_id, r"^manual:[0-9a-f]{16}:")
        self.assertRegex(self.bundle.assignment_id, r"^asg_[0-9a-f]{24}$")
        self.assertRegex(self.bundle.document_digest, r"^[0-9a-f]{64}$")
        self.assertEqual(self.bundle.canonical_owner,
                         "google:" + self.bundle.raw_subject)
        self.assertEqual(self.bundle.job_status, "succeeded")


class MigrationSqlTests(unittest.TestCase):
    def setUp(self):
        self.sql = (ROOT / "schema" /
                    "postgres_v9_result_context.sql").read_text(
                        encoding="utf-8").lower()
        self.rollback = (ROOT / "schema" /
                         "postgres_v9_result_context_rollback.sql").read_text(
                             encoding="utf-8").lower()

    def test_additive_table_with_scoped_identity_and_links(self):
        self.assertIn("create table if not exists result_context", self.sql)
        self.assertIn("primary key (tenant_id, membership_subject, result_id)",
                      self.sql)
        self.assertIn("references results (tenant_id, owner_subject, result_id)",
                      self.sql)
        self.assertIn("references tenant_membership (tenant_id, subject)",
                      self.sql)
        self.assertIn("fk_result_context_anchor", self.sql)

    def test_rls_is_enabled_and_forced(self):
        self.assertIn("enable row level security", self.sql)
        self.assertIn("force  row level security", self.sql)
        self.assertIn("current_setting('app.tenant_id', true)", self.sql)

    def test_runtime_grants_are_select_and_insert_only(self):
        self.assertIn("grant select, insert on result_context", self.sql)
        self.assertNotIn("grant update", self.sql)
        self.assertNotIn("grant delete", self.sql)
        self.assertIn("before update or delete on result_context", self.sql)

    def test_capability_marker_is_distinct_from_schema_version(self):
        self.assertIn("result_context_version", self.sql)
        self.assertIn("result_context_version", self.rollback)
        self.assertNotIn("schema_version', '2", self.sql)

    def test_owner_representation_is_enforced_in_storage(self):
        self.assertIn("owner_subject = 'google:' || membership_subject",
                      self.sql)

    def test_rollback_refuses_populated_originals(self):
        self.assertIn("result_context is populated", self.rollback)
        self.assertIn("rollback refused", self.rollback)
        self.assertLess(self.rollback.index("raise exception"),
                        self.rollback.index("drop table if exists "
                                            "public.result_context"))

    def test_rollback_requires_an_elevated_effective_role_before_ddl(self):
        self.assertIn("rolsuper", self.rollback)
        self.assertIn("rolbypassrls", self.rollback)
        self.assertIn("pg_catalog.pg_roles", self.rollback)
        first_raise = self.rollback.index("raise exception")
        first_drop = self.rollback.index("drop ")
        self.assertLess(first_raise, first_drop)

    def test_rollback_locks_before_the_populated_check(self):
        self.assertIn("access exclusive", self.rollback)
        lock_at = self.rollback.index("lock table")
        populated_at = self.rollback.index("result_context is populated")
        self.assertLess(lock_at, populated_at)

    def test_rollback_never_widens_or_disables_anything(self):
        for banned in ("cascade", "security definer",
                       "disable row level security", "disable trigger",
                       "grant "):
            self.assertNotIn(banned, self.rollback)


class _PgContextBase(unittest.TestCase):
    """Shared live-database base; skipped unless every gate layer passes.

    Setup NEVER applies migrations and NEVER deletes or reactivates rows:
    the capability markers are checked (read-only) and the fixture rows are
    seeded through the explicit non-production seeder under one unique run
    ID. A disposable database is dropped whole by the operator - there is no
    tear-down SQL here at all.
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
        # One UNIQUE bundle per test: no context conflicts between tests, no
        # clean-up SQL, rows retained for the operator. Each test owns its
        # connection and closes it - no shared state, no reactivation.
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        try:
            self.bundle = fixtures.build_bundle()
            fixtures.seed(self.conn, self.bundle, self.approval,
                          is_production=False)
        except Exception:
            self.conn.close()
            raise

    def tearDown(self):
        # No deletes, no truncates: the disposable database is discarded
        # whole by the operator. Only the test's OWN connection is closed.
        if getattr(self, "conn", None) is not None:
            self.conn.close()

    def identity(self):
        return identity_context.from_verified_subject(
            self.bundle.raw_subject, self.bundle.tenant_id)

    def store_for(self, subject, tenant_id):
        return postgres_result_store.open_store(
            self.dsn, identity=identity_context.from_verified_subject(
                subject, tenant_id))

    def fixture_store(self):
        """The explicit NON-PRODUCTION fixture registration store: the
        fresh-completion denial is lifted HERE ONLY (fixture seeding path,
        behind the live gate). Production runtime reads and reviews keep the
        default denial; the production policy itself is never patched."""
        return postgres_result_store.open_store(
            self.dsn,
            identity=identity_context.from_verified_subject(
                self.bundle.raw_subject, self.bundle.tenant_id),
            fresh_completion_denied=False)


class PostgresResultContextContractTests(_PgContextBase):
    """Store-adapter persistence contract on live PostgreSQL (skipped
    locally; the gated class is the honest BR-02D gate, never a pass)."""

    def test_registration_commits_context_and_anchor_together(self):
        store = self.fixture_store()
        try:
            receipt = store.register_with_context(
                result_id=self.bundle.result_id,
                original_output=self.bundle.original_output,
                scope="manual", review_key=self.bundle.review_key,
                document_digest=self.bundle.document_digest)
            self.assertEqual(receipt["membership_subject"],
                             self.bundle.raw_subject)
            self.assertEqual(receipt["owner"], self.bundle.canonical_owner)
            self.assertEqual(store.context_record(
                result_id=self.bundle.result_id)["original_output"],
                self.bundle.original_output)
            self.assertEqual(store.current_revision(
                owner=self.bundle.raw_subject,
                result_id=self.bundle.result_id), 0)
        finally:
            store.close()

    def test_conflicting_original_under_the_same_identity_is_refused(self):
        store = self.fixture_store()
        try:
            store.register_with_context(
                result_id=self.bundle.result_id,
                original_output=self.bundle.original_output,
                scope="manual", review_key=self.bundle.review_key)
            changed = {"questions": [{"question_number": "1",
                                      "awarded_marks": 999}]}
            with self.assertRaises(audit_store.AuditStoreError) as caught:
                store.register_with_context(
                    result_id=self.bundle.result_id, original_output=changed,
                    scope="manual", review_key=self.bundle.review_key)
            self.assertEqual(caught.exception.code, "original_output_immutable")
        finally:
            store.close()

    def test_reads_survive_reopening_a_second_connection(self):
        # Reopen: a NEW connection to the SAME running database. This is not
        # a process or server restart - durability across a restart needs the
        # future approved target and is a separate, stronger check.
        store = self.fixture_store()
        store.register_with_context(
            result_id=self.bundle.result_id,
            original_output=self.bundle.original_output, scope="manual",
            review_key=self.bundle.review_key)
        store.close()
        reopened = self.store_for(self.bundle.raw_subject,
                                  self.bundle.tenant_id)
        try:
            self.assertIsNotNone(reopened.visible_row(self.bundle.result_id))
            snapshot = reopened.snapshot(result_id=self.bundle.result_id)
            self.assertEqual(snapshot["original_output"],
                             self.bundle.original_output)
            self.assertEqual(snapshot["result_revision"], 0)
        finally:
            reopened.close()

    def test_cross_tenant_lookup_is_indistinguishable_from_absent(self):
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

    def test_runtime_role_cannot_update_or_delete_context(self):
        store = self.fixture_store()
        try:
            store.register_with_context(
                result_id=self.bundle.result_id,
                original_output=self.bundle.original_output,
                scope="manual", review_key=self.bundle.review_key)
        finally:
            store.close()
        with psycopg.connect(self.dsn) as probe:
            probe.execute("SET ROLE marker_app")
            probe.execute("SELECT set_config('app.tenant_id', %s, true)",
                          (self.bundle.tenant_id,))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                probe.execute("UPDATE result_context SET review_key ="
                              " review_key WHERE result_id = %s",
                              (self.bundle.result_id,))
            probe.rollback()
            probe.execute("SELECT set_config('app.tenant_id', %s, true)",
                          (self.bundle.tenant_id,))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                probe.execute("DELETE FROM result_context WHERE result_id = %s",
                              (self.bundle.result_id,))

    def test_rollback_draft_refuses_while_originals_exist(self):
        store = self.fixture_store()
        try:
            store.register_with_context(
                result_id=self.bundle.result_id,
                original_output=self.bundle.original_output,
                scope="manual", review_key=self.bundle.review_key)
        finally:
            store.close()
        with psycopg.connect(self.dsn, autocommit=True) as probe:
            ddl = (ROOT / "schema" /
                   "postgres_v9_result_context_rollback.sql").read_text(
                       encoding="utf-8")
            with self.assertRaises(psycopg.errors.RaiseException):
                probe.execute(ddl)


if __name__ == "__main__":
    unittest.main()
