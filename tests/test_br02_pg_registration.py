"""BR-02 PG harness: registration both-or-nothing contract (offline +
PG-gated; live classes unexecuted by design until the future approval
exists - a skip is the honest gate, never a pass).

Coverage map:
* offline: the live gate refuses BEFORE any environment value access
  (recording-stand-in spy, never the real env); the fixture identity follows
  the REAL durable result contract (``manual:<document_digest[:16]>:
  <review_key>``); the seed plan binds raw subject for membership-linked
  metadata and canonical owner for jobs/assignments, with the job carrying
  the assignment link; the seeder owns exactly one transaction and never
  partially commits;
* gated (real operations against the live target): anchor-then-context
  both-or-nothing in BOTH failure directions, replay idempotency, changed
  content/digest/projection immutability, trusted-link consistency, and the
  no-initial-review state.
"""
import os
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

import audit_store
import br02_pg_fixtures as fixtures
import br02_pg_target
import identity_context
import postgres_result_store


def _live_approval():
    """Future supply point for the independently approved target + seed
    approval. Returns None today; never inferred from env/DSN/flags."""
    return None


def _test_dsn():
    """The dedicated test DSN variable only; read lazily and only AFTER the
    authorization gate passes."""
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


class _RecordingMapping(dict):
    """Inert stand-in for os.environ that records reads; the real process
    environment is never read, copied, or modified."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads = []

    def __getitem__(self, key):
        self.reads.append(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.reads.append(key)
        return super().get(key, default)


_MISSING_MARKER = object()


class _RecordingConnection:
    """Connection double with a transaction surface: records statements and
    commit/rollback events without executing anything. It distinguishes the
    capability-marker query from the ACTUAL synthetic run-marker read so the
    seeder's marker contract is exercised, never bypassed."""

    def __init__(self, precheck_rows=None, fail_on_statement=None,
                 marker_value=_MISSING_MARKER):
        self.statements = []
        self.precheck_rows = list(precheck_rows or [])
        self.fail_on_statement = fail_on_statement
        self.marker_value = marker_value
        self.commits = 0
        self.rollbacks = 0

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if (self.fail_on_statement is not None
                and len(self.statements) == self.fail_on_statement):
            raise RuntimeError("injected failure (test double)")
        lowered = " ".join(sql.lower().split())
        if "key in ('schema_version','result_context_version')" in lowered:
            return _ResultSet(list(self.precheck_rows))
        if "from schema_meta where key = %s" in lowered:
            if self.marker_value is _MISSING_MARKER:
                return _ResultSet([])
            return _ResultSet([(self.marker_value,)])
        return _ResultSet([])

    def transaction(self):
        return _RecordingTransaction(self)


class _RecordingTransaction:
    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_exc):
        if exc_type is None:
            self._connection.commits += 1
        else:
            self._connection.rollbacks += 1
        return False


class _ResultSet:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _RegistrationConnection(_RecordingConnection):
    """Inert tuple-row connection with transactional anchor/context state."""

    def __init__(self):
        super().__init__()
        from types import SimpleNamespace
        from psycopg.pq import TransactionStatus
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        self.anchors = {}
        self.contexts = {}
        self.fail_context_insert = False

    def transaction(self):
        connection = self

        class Transaction(_RecordingTransaction):
            def __enter__(self):
                self.before = (dict(connection.anchors),
                               dict(connection.contexts))
                return self

            def __exit__(self, exc_type, *exc):
                if exc_type is not None:
                    connection.anchors, connection.contexts = self.before
                return super().__exit__(exc_type, *exc)

        return Transaction(self)

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        lowered = " ".join(sql.lower().split())
        rowcount = 0
        rows = []
        if lowered.startswith("insert into results "):
            key = params[:3]
            rowcount = int(key not in self.anchors)
            self.anchors.setdefault(key, (0, params[3], params[4],
                                          params[5], "1700000000"))
        elif lowered.startswith("insert into result_context "):
            if self.fail_context_insert:
                raise RuntimeError("inert context insert failure")
            key = (params[0], params[1], params[3])
            rowcount = int(key not in self.contexts)
            # Exact _RESULT_CONTEXT_COLUMNS order, as ordinary psycopg tuples.
            self.contexts.setdefault(key, tuple(params[6:8]) +
                                     tuple(params[4:6]) + tuple(params[8:15]) +
                                     (params[2], params[1], "1700000000"))
        elif " from results " in lowered:
            row = self.anchors.get(tuple(params))
            rows = [] if row is None else [row]
        elif " from result_context " in lowered:
            row = self.contexts.get(tuple(params))
            rows = [] if row is None else [row]
        elif "schema_meta" in lowered:
            rows = [("1",)]
        elif "to_regclass" in lowered:
            rows = [("result_context",)]
        elif "from tenant_membership" in lowered:
            rows = [(1,)]
        else:
            assert lowered.startswith("select set_config"), sql
        result = _ResultSet(rows)
        result.rowcount = rowcount
        return result


class RegistrationTupleRowTests(unittest.TestCase):
    """Real registration, selection, decoder and transaction orchestration."""

    def setUp(self):
        self.connection = _RegistrationConnection()
        identity = identity_context.from_verified_subject("tuple-teacher",
                                                          "tuple-tenant")
        with mock.patch("runtime_config.is_production_runtime", return_value=False):
            self.store = postgres_result_store.PostgresResultStore(
                self.connection, identity=identity, fresh_completion_denied=False)
        self.addCleanup(self.store.close)
        self.connection.commits = self.connection.rollbacks = 0
        self.arguments = dict(result_id="manual:tuple-result",
                              original_output={"questions": []},
                              scope="manual", review_key="tuple-review")
        self.key = ("tuple-tenant", "tuple-teacher", "manual:tuple-result")
        self.runtime = mock.patch("runtime_config.is_production_runtime",
                                  return_value=False)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)

    def test_success_commits_anchor_and_tuple_context(self):
        receipt = self.store.register_with_context(**self.arguments)
        self.assertTrue(receipt["created"])
        self.assertEqual(receipt["owner"], "google:tuple-teacher")
        self.assertEqual(receipt["result_revision"], 0)
        self.assertIsInstance(self.connection.contexts[self.key], tuple)
        self.assertEqual(set(self.connection.anchors), {self.key})
        self.assertEqual(set(self.connection.contexts), {self.key})
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (1, 0))

    def test_replay_is_idempotent(self):
        first = self.store.register_with_context(**self.arguments)
        before = (dict(self.connection.anchors), dict(self.connection.contexts))
        second = self.store.register_with_context(**self.arguments)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["original_digest"], second["original_digest"])
        self.assertEqual(before, (self.connection.anchors, self.connection.contexts))
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (2, 0))

    def test_conflicting_context_rolls_back_and_preserves_original(self):
        self.store.register_with_context(**self.arguments)
        before = (dict(self.connection.anchors), dict(self.connection.contexts))
        with self.assertRaises(audit_store.AuditStoreError) as caught:
            self.store.register_with_context(**dict(self.arguments, review_key="other"))
        self.assertEqual(caught.exception.code, "original_output_immutable")
        self.assertEqual(before, (self.connection.anchors, self.connection.contexts))
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (1, 1))

    def test_context_insert_failure_rolls_back_new_anchor(self):
        self.connection.fail_context_insert = True
        with self.assertRaises(audit_store.AuditStoreError) as caught:
            self.store.register_with_context(**self.arguments)
        self.assertEqual(caught.exception.code, "storage_constraint_violation")
        self.assertEqual(self.connection.anchors, {})
        self.assertEqual(self.connection.contexts, {})
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 1))

    def test_invalid_stored_output_rolls_back_new_anchor(self):
        digest = audit_store.digest_value(self.arguments["original_output"])
        self.connection.contexts[self.key] = (
            "invalid-json", digest, "manual", "tuple-review", "", None,
            None, None, None, None, "", "google:tuple-teacher",
            "tuple-teacher", "1700000000")
        with self.assertRaises(audit_store.AuditStoreError) as caught:
            self.store.register_with_context(**self.arguments)
        self.assertEqual(caught.exception.code, "audit_record_invalid")
        self.assertEqual(self.connection.anchors, {})
        self.assertEqual((self.connection.commits, self.connection.rollbacks), (0, 1))


class GateOrderTests(unittest.TestCase):
    """The gate refuses the unauthorized phase before ANY environment
    value access (spy-proven with inert mappings, never the real env)."""

    def test_unauthorized_gate_reads_no_environment(self):
        spy = _RecordingMapping()
        with mock.patch.object(os, "environ", spy):
            reason = _live_gate()
        self.assertIn("refused", reason)
        self.assertNotIn(
            br02_pg_target.TEST_DSN_ENV, spy.reads,
            "the test DSN variable must not be read before authorization")
        self.assertNotIn(br02_pg_target.PRODUCTION_DSN_ENV, spy.reads)

    def test_authorized_phase_reads_only_the_test_dsn_variable(self):
        spy = _RecordingMapping()
        approval = fixtures.SeedApproval(
            approved_manifest=br02_pg_target.supply_approved_manifest(
                *br02_pg_target.synthetic_approved_manifest()),
            execution_authorized=True,
            target_dsn="postgresql://inert-target/db",
        )
        with mock.patch.object(os, "environ", spy), \
                mock.patch.object(br02_pg_target, "execution_authorized",
                                  return_value=True), \
                mock.patch.object(sys.modules[__name__], "_live_approval",
                                  return_value=approval):
            reason = _live_gate()
        # Driver present, approval present: the ONLY env read left is the
        # dedicated test DSN variable (absent here -> honest refusal).
        self.assertIn("AI_MARKER_TEST_PG_DSN", reason)
        self.assertEqual(spy.reads, [br02_pg_target.TEST_DSN_ENV])
        self.assertNotIn(br02_pg_target.PRODUCTION_DSN_ENV, spy.reads)


class BundleIdentityContractTests(unittest.TestCase):
    """The fixture identity matches the REAL durable result contract and the
    seeded provenance is internally consistent."""

    def setUp(self):
        self.bundle = fixtures.build_bundle(run_id="identity-run-0001")

    def test_result_id_follows_the_real_durable_contract(self):
        expected = ("manual:" + self.bundle.document_digest[:16] + ":"
                    + self.bundle.review_key)
        self.assertEqual(self.bundle.result_id, expected)
        self.assertRegex(self.bundle.result_id, r"^manual:[0-9a-f]{16}:")

    def test_job_links_the_same_identity_and_the_assignment(self):
        plan = fixtures.seed_plan(self.bundle)
        job_insert = next(params for sql, params in plan
                          if "into marking_jobs" in sql)
        # (tenant, owner, job_id, key, doc_digest, status, attempts,
        #  max_attempts, lease, result_id, error_code, created, updated,
        #  assignment_id)
        self.assertEqual(job_insert[9], self.bundle.result_id)
        self.assertEqual(job_insert[13], self.bundle.assignment_id)
        assignment_insert = next(params for sql, params in plan
                                 if "into assignments" in sql)
        self.assertEqual(assignment_insert[2], self.bundle.assignment_id)

    def test_owner_representation_split_matches_the_schema(self):
        plan = fixtures.seed_plan(self.bundle)
        pdf_params = next(params for sql, params in plan
                          if "into pdf_references" in sql)
        job_params = next(params for sql, params in plan
                          if "into marking_jobs" in sql)
        asg_params = next(params for sql, params in plan
                          if "into assignments" in sql)
        # Membership-linked metadata carries the RAW subject (FK to
        # tenant_membership); jobs/assignments keep the CANONICAL owner.
        self.assertEqual(pdf_params[1], self.bundle.raw_subject)
        self.assertEqual(job_params[1], self.bundle.canonical_owner)
        self.assertEqual(asg_params[1], self.bundle.canonical_owner)


class SeedTransactionTests(unittest.TestCase):
    """The seeder owns exactly one transaction: capability check, RLS
    bindings and every INSERT commit together or not at all."""

    def setUp(self):
        self.bundle = fixtures.build_bundle(run_id="txn-run-0001")
        # The marker identity is the approved manifest's OVERALL run id, never
        # the per-test fixture-bundle id.
        self.approved = br02_pg_target.supply_approved_manifest(
            *br02_pg_target.synthetic_approved_manifest())
        self.approval = fixtures.SeedApproval(
            approved_manifest=self.approved,
            execution_authorized=True,
            target_dsn="postgresql://inert-target/db",
            run_marker=self.approved.binding["run_id"],
        )
        self.gate_stub = mock.patch.object(
            br02_pg_target, "authorize_execution")
        self.gate_stub.start()
        self.addCleanup(self.gate_stub.stop)

    def test_success_runs_one_owned_transaction(self):
        connection = _RecordingConnection(
            precheck_rows=[("schema_version", "1"),
                           ("result_context_version", "1")],
            marker_value=self.approval.run_marker)
        receipt = fixtures.seed(connection, self.bundle, self.approval,
                                is_production=False)
        self.assertEqual(receipt["transaction"], "owned-one-transaction")
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        # Capability check and the ACTUAL run-marker read run FIRST, inside
        # the owned transaction; then the plan's RLS bindings and INSERT-only
        # statements.
        self.assertIn("schema_meta", connection.statements[0][0])
        self.assertEqual(connection.statements[1][1],
                         (br02_pg_target.STAGE2_RUN_MARKER_KEY,))
        plan_length = len(fixtures.seed_plan(self.bundle))
        self.assertEqual(len(connection.statements), 2 + plan_length)
        for sql, _params in connection.statements[2:]:
            lowered = sql.lower()
            self.assertTrue(
                lowered.startswith("select set_config")
                or lowered.startswith("insert into"), sql)

    def test_failure_rolls_back_everything(self):
        connection = _RecordingConnection(
            precheck_rows=[("schema_version", "1"),
                           ("result_context_version", "1")],
            marker_value=self.approval.run_marker,
            fail_on_statement=5)  # a later INSERT fails
        with self.assertRaises(RuntimeError):
            fixtures.seed(connection, self.bundle, self.approval,
                          is_production=False)
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(connection.commits, 0)
        self.assertLess(len(connection.statements),
                        2 + len(fixtures.seed_plan(self.bundle)))

    def test_capability_refusal_rolls_back_before_any_insert(self):
        connection = _RecordingConnection(precheck_rows=[])
        with self.assertRaises(br02_pg_target.TargetRefused) as caught:
            fixtures.seed(connection, self.bundle, self.approval,
                          is_production=False)
        self.assertEqual(caught.exception.reason, br02_pg_target.MISSING_TARGET)
        self.assertEqual(connection.rollbacks, 1)
        self.assertEqual(len(connection.statements), 1)
        self.assertIn("schema_meta", connection.statements[0][0])

    def test_missing_run_marker_refuses_before_any_insert(self):
        connection = _RecordingConnection(
            precheck_rows=[("schema_version", "1"),
                           ("result_context_version", "1")])
        with self.assertRaises(br02_pg_target.TargetRefused) as caught:
            fixtures.seed(connection, self.bundle, self.approval,
                          is_production=False)
        self.assertEqual(caught.exception.reason,
                         br02_pg_target.TARGET_NOT_INITIALIZED)
        self.assertEqual(connection.rollbacks, 1)
        self.assertFalse(
            [sql for sql, _params in connection.statements
             if sql.lower().lstrip().startswith("insert into")],
            "normal seed never creates the initialized marker")

    def test_another_runs_marker_refuses_before_any_insert(self):
        connection = _RecordingConnection(
            precheck_rows=[("schema_version", "1"),
                           ("result_context_version", "1")],
            marker_value="another-run")
        with self.assertRaises(br02_pg_target.TargetRefused) as caught:
            fixtures.seed(connection, self.bundle, self.approval,
                          is_production=False)
        self.assertEqual(caught.exception.reason,
                         br02_pg_target.INCONSISTENT_TARGET)
        self.assertEqual(connection.rollbacks, 1)
        self.assertFalse(
            [sql for sql, _params in connection.statements
             if sql.lower().lstrip().startswith("insert into")])


class _LiveRegistrationBase(unittest.TestCase):
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

    def fixture_store(self):
        """The explicit NON-PRODUCTION fixture registration store: the
        fresh-completion denial is lifted HERE ONLY, behind the live gate.
        Production runtime reads and reviews keep the default denial; the
        production policy itself is never patched."""
        return postgres_result_store.open_store(
            self.dsn,
            identity=identity_context.from_verified_subject(
                self.bundle.raw_subject, self.bundle.tenant_id),
            fresh_completion_denied=False)

    def registration_arguments(self, original_output=None):
        return dict(
            result_id=self.bundle.result_id,
            original_output=original_output or self.bundle.original_output,
            scope="manual", review_key=self.bundle.review_key,
            document_digest=self.bundle.document_digest,
            job_id=self.bundle.job_id,
            assignment_id=self.bundle.assignment_id)


class RegistrationBothOrNothingTests(_LiveRegistrationBase):
    """Real registration operations on the live target (gated; unexecuted
    until the future approval exists)."""

    def test_anchor_and_context_are_written_together(self):
        store = self.fixture_store()
        try:
            receipt = store.register_with_context(
                **self.registration_arguments())
            self.assertTrue(receipt["created"])
            self.assertEqual(receipt["result_revision"], 0)
        finally:
            store.close()
        anchor = self.conn.execute(
            "select original_digest, result_revision from results"
            " where tenant_id=%s and owner_subject=%s and result_id=%s",
            (self.bundle.tenant_id, self.bundle.raw_subject,
             self.bundle.result_id)).fetchone()
        self.assertIsNotNone(anchor, "the anchor row must exist")
        context = self.conn.execute(
            "select original_output, job_id, assignment_id from"
            " result_context where tenant_id=%s and membership_subject=%s"
            " and result_id=%s",
            (self.bundle.tenant_id, self.bundle.raw_subject,
             self.bundle.result_id)).fetchone()
        self.assertIsNotNone(context, "the context row must exist")
        # original_output is stored as JSON TEXT; parse it before semantic digest
        # comparison to avoid double-encoding the TEXT representation.
        self.assertEqual(
            audit_store.digest_value(audit_store.json.loads(context[0])),
            anchor[0])
        self.assertEqual(context[1], self.bundle.job_id)
        self.assertEqual(context[2], self.bundle.assignment_id)

    def test_changed_original_refused_and_context_absent(self):
        # Direction 1: the anchor exists with a DIFFERENT original; the
        # context write must never happen.
        import json
        changed = {"questions": [{"question_number": "1",
                                  "awarded_marks": 999, "max_marks": 3}]}
        digest = audit_store.digest_value(changed)
        projection = audit_store.original_score_projection(changed)
        self.conn.execute(
            "insert into results (tenant_id, owner_subject, result_id,"
            " result_revision, original_digest, original_scores,"
            " original_scores_digest) values (%s, %s, %s, 0, %s, %s, %s)",
            (self.bundle.tenant_id, self.bundle.raw_subject,
             self.bundle.result_id, digest,
             json.dumps(projection, separators=(",", ":")),
             audit_store.digest_value(projection)))
        store = self.fixture_store()
        try:
            with self.assertRaises(audit_store.AuditStoreError) as caught:
                store.register_with_context(
                    **self.registration_arguments())
            self.assertEqual(caught.exception.code,
                             "original_output_immutable")
        finally:
            store.close()
        context = self.conn.execute(
            "select 1 from result_context where tenant_id=%s"
            " and membership_subject=%s and result_id=%s",
            (self.bundle.tenant_id, self.bundle.raw_subject,
             self.bundle.result_id)).fetchone()
        self.assertIsNone(context, "both-or-nothing: no context row")

    def test_conflicting_context_identity_refused_no_second_row(self):
        store = self.fixture_store()
        try:
            store.register_with_context(**self.registration_arguments())
            # Direction 2: same identity, a conflicting context field.
            conflicting = self.registration_arguments()
            conflicting["review_key"] = self.bundle.review_key + "-other"
            with self.assertRaises(audit_store.AuditStoreError):
                store.register_with_context(**conflicting)
        finally:
            store.close()
        count = self.conn.execute(
            "select count(*) from result_context where tenant_id=%s"
            " and membership_subject=%s and result_id=%s",
            (self.bundle.tenant_id, self.bundle.raw_subject,
             self.bundle.result_id)).fetchone()[0]
        self.assertEqual(count, 1)

    def test_replay_is_idempotent_with_no_second_write(self):
        store = self.fixture_store()
        try:
            first = store.register_with_context(
                **self.registration_arguments())
            second = store.register_with_context(
                **self.registration_arguments())
            self.assertTrue(first["created"])
            self.assertFalse(second["created"])
            self.assertEqual(first["original_digest"],
                             second["original_digest"])
        finally:
            store.close()
        rows = self.conn.execute(
            "select count(*) from result_context where tenant_id=%s"
            " and membership_subject=%s and result_id=%s",
            (self.bundle.tenant_id, self.bundle.raw_subject,
             self.bundle.result_id)).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_projection_digest_and_immutability(self):
        store = self.fixture_store()
        try:
            store.register_with_context(**self.registration_arguments())
        finally:
            store.close()
        stored = self.conn.execute(
            "select r.original_scores, r.original_scores_digest,"
            " c.original_digest from results r"
            " join result_context c on c.tenant_id = r.tenant_id"
            " and c.membership_subject = r.owner_subject"
            " and c.result_id = r.result_id"
            " where r.tenant_id=%s and r.owner_subject=%s and r.result_id=%s",
            (self.bundle.tenant_id, self.bundle.raw_subject,
             self.bundle.result_id)).fetchone()
        expected = audit_store.original_score_projection(
            self.bundle.original_output)
        import json
        self.assertEqual(stored[0],
                         json.dumps(expected, separators=(",", ":")))
        self.assertEqual(stored[1], audit_store.digest_value(expected))
        self.assertEqual(stored[2],
                         audit_store.digest_value(self.bundle.original_output))
        # The marks-only projection and the immutable original refuse UPDATE
        # at the privilege and trigger layers (same probe pattern as the
        # reviewed store contract: SET ROLE, then the refused statement).
        # Under marker_app, both UPDATEs are refused by privilege first;
        # trigger enforcement is pending a separately gated role/path that
        # can reach it without changing grants.
        self.conn.execute("set role marker_app")
        try:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                self.conn.execute(
                    "update results set original_scores = original_scores"
                    " where result_id = %s", (self.bundle.result_id,))
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                self.conn.execute(
                    "update result_context set review_key = review_key"
                    " where result_id = %s", (self.bundle.result_id,))
        finally:
            self.conn.execute("reset role")

    def test_no_initial_review_state(self):
        store = self.fixture_store()
        try:
            store.register_with_context(**self.registration_arguments())
            self.assertEqual(store.current_revision(
                owner=self.bundle.raw_subject,
                result_id=self.bundle.result_id), 0)
            self.assertEqual(store.history(
                result_id=self.bundle.result_id)["events"], [])
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
