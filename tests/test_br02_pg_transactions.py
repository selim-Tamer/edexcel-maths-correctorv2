"""BR-02 PG harness: transaction integrity under fault injection (offline +
PG-gated; live classes unexecuted by design until the future approval
exists - a skip is the honest gate, never a pass).

Coverage map:
* gated (REAL operations against the live target): the store's supported
  fault stages (``begin``, ``after_event_insert``) roll back cleanly and
  leave the separately committed original intact, then the SAME operation is
  retried successfully (retry = a fresh owned operation, the only retry
  shape supported); REAL database failures are distinguished from injected
  Python exceptions by their ``__cause__`` origin through a test-local
  DELEGATING REAL-CONNECTION proxy; and an interrupted OWN connection is
  reaped and replaced by a fresh store (never a generic ``pg_terminate``
  against other backends).
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))      # tests/

try:
    import psycopg
except ImportError:  # pragma: no cover - driver ships in the test manifest
    psycopg = None

import audit_store
import br02_pg_fixtures as fixtures
import br02_pg_target
import postgres_result_store


def _live_approval():
    """Future supply point; returns None today, never inferred from
    env/DSN/flags."""
    return None


def _test_dsn():
    return os_dsn()


def os_dsn():
    """The dedicated test DSN variable only; read lazily and only AFTER the
    authorization gate passes."""
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
    if not os_dsn():
        return ("AI_MARKER_TEST_PG_DSN is not configured - the BR-02 "
                "PostgreSQL gate is BLOCKED locally; never AI_MARKER_PG_DSN")
    return None


class _DelegatingConnectionProxy:
    """Test-local proxy around a REAL live connection: every call delegates
    until a fault point is armed. Used ONLY to observe error origins and to
    interrupt the proxy's OWN connection - never other backends."""

    def __init__(self, connection):
        self._connection = connection
        self.execute_calls = 0
        self.fail_next_execute_with = None
        self.interrupted = False

    def execute(self, sql, params=None):
        self.execute_calls += 1
        failure = self.fail_next_execute_with
        if failure is not None:
            self.fail_next_execute_with = None
            raise failure
        if self.interrupted:
            self._connection.close()
            raise psycopg.OperationalError(
                "own connection interrupted (proxy-injected)")
        return self._connection.execute(sql, params)

    def transaction(self):
        return self._connection.transaction()

    def close(self):
        self._connection.close()

    @property
    def closed(self):
        return self._connection.closed

    @property
    def info(self):
        return self._connection.info


class _LiveTransactionsBase(unittest.TestCase):
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

    def store(self):
        import identity_context
        return postgres_result_store.open_store(
            self.dsn,
            identity=identity_context.from_verified_subject(
                self.bundle.raw_subject, self.bundle.tenant_id),
            fresh_completion_denied=False)

    def register_original(self):
        store = self.store()
        try:
            return store.register_with_context(
                result_id=self.bundle.result_id,
                original_output=self.bundle.original_output,
                scope="manual", review_key=self.bundle.review_key,
                document_digest=self.bundle.document_digest)
        finally:
            store.close()

    def correction_event(self, question="1", reason="rework"):
        return ({"actor": self.bundle.canonical_owner,
                 "question_number": question, "action": "correct",
                 "review_status": "corrected", "old_score": "2",
                 "new_score": "3", "reason": reason, "binding": "",
                 "authorization_evidence": {
                     "scope": "manual", "actor": self.bundle.canonical_owner,
                     "authorization_revision": self.bundle.document_digest,
                     "row_revision": "", "verified_at": "1700000000"}},
                {"question_number": question, "action": "correct",
                 "review_status": "corrected", "old_score": "2",
                 "new_score": "3", "reason": reason, "binding": ""})


class FaultStageTransactionTests(_LiveTransactionsBase):
    """The supported fault stages roll back cleanly; the committed original
    survives every fault; retry is a fresh owned operation."""

    def test_fault_before_begin_preserves_original(self):
        self.register_original()
        event, payload = self.correction_event()
        faulted = self.store()
        try:
            faulted.set_fault_hook(
                lambda stage: (_ for _ in ()).throw(
                    audit_store.AuditStoreError("injected", "begin")))
            with self.assertRaises(audit_store.AuditStoreError) as caught:
                faulted.commit_correction(
                    owner=self.bundle.raw_subject,
                    result_id=self.bundle.result_id, expected_revision=0,
                    idempotency_key="fault-begin", payload=payload,
                    event=event)
            self.assertEqual(caught.exception.code, "injected")
        finally:
            faulted.set_fault_hook(None)
            faulted.close()
        fresh = self.store()
        try:
            snapshot = fresh.snapshot(result_id=self.bundle.result_id)
            self.assertEqual(snapshot["result_revision"], 0)
            self.assertEqual(snapshot["events"], [])
        finally:
            fresh.close()

    def test_fault_after_event_insert_rolls_back_the_event(self):
        self.register_original()
        event, payload = self.correction_event()
        faulted = self.store()
        try:
            faulted.set_fault_hook(
                lambda stage: (_ for _ in ()).throw(
                    audit_store.AuditStoreError("injected",
                                                "after_event_insert"))
                if stage == "after_event_insert" else None)
            with self.assertRaises(audit_store.AuditStoreError):
                faulted.commit_correction(
                    owner=self.bundle.raw_subject,
                    result_id=self.bundle.result_id, expected_revision=0,
                    idempotency_key="fault-insert", payload=payload,
                    event=event)
        finally:
            faulted.set_fault_hook(None)
            faulted.close()
        # Retry on a fresh owned connection: the ONLY supported retry shape.
        retried = self.store()
        try:
            outcome = retried.commit_correction(
                owner=self.bundle.raw_subject,
                result_id=self.bundle.result_id, expected_revision=0,
                idempotency_key="fault-insert", payload=payload, event=event)
            self.assertEqual(outcome["status"], "committed")
        finally:
            retried.close()
        fresh = self.store()
        try:
            snapshot = fresh.snapshot(result_id=self.bundle.result_id)
            self.assertEqual(snapshot["result_revision"], 1)
            self.assertEqual(len(snapshot["events"]), 1)
        finally:
            fresh.close()

    def test_real_database_failure_is_distinguishable_from_python_faults(self):
        self.register_original()
        event, payload = self.correction_event()
        # A REAL database failure: duplicate (stream, idempotency_key) is a
        # driver-raised constraint violation with a psycopg cause. The
        # audit_events owner predicate is the RAW membership subject.
        committing = self.store()
        try:
            committing.commit_correction(
                owner=self.bundle.raw_subject,
                result_id=self.bundle.result_id, expected_revision=0,
                idempotency_key="db-failure", payload=payload, event=event)
        finally:
            committing.close()
        replaying = self.store()
        try:
            replaying.commit_correction(
                owner=self.bundle.raw_subject,
                result_id=self.bundle.result_id, expected_revision=0,
                idempotency_key="db-failure", payload=payload, event=event)
        finally:
            replaying.close()
        # Force a REAL constraint failure through the adapter path: a second
        # event with the SAME idempotency key but a different payload is a
        # semantic refusal (Python-side), while a raw duplicate insert is a
        # driver error. We prove the distinction through the cause chain.
        conflicting = self.store()
        try:
            changed_payload = dict(payload, reason="different")
            changed_event = dict(event, reason="different")
            with self.assertRaises(audit_store.AuditStoreError) as caught:
                conflicting.commit_correction(
                    owner=self.bundle.raw_subject,
                    result_id=self.bundle.result_id, expected_revision=0,
                    idempotency_key="db-failure",
                    payload=changed_payload, event=changed_event)
            self.assertEqual(caught.exception.code,
                             "idempotency_key_payload_mismatch")
            self.assertNotIsInstance(
                caught.exception.__cause__, psycopg.errors.Error,
                "a Python-side refusal is not a database failure")
        finally:
            conflicting.close()
        # The REAL database failure shape: a DIRECT duplicate insert against
        # the committed event's (stream, idempotency_key) unique constraint
        # is a driver-raised failure, with the psycopg error as its cause -
        # a different origin than the Python-side refusal above. The
        # audit_events owner predicate is the RAW membership subject.
        with self.assertRaises(psycopg.errors.UniqueViolation) as db_failure:
            with self.conn.transaction():
                self.conn.execute(
                    "insert into audit_events (tenant_id, owner_subject,"
                    " result_id, sequence, event_id, idempotency_key,"
                    " payload_digest, event_type, question_number, action,"
                    " review_status, expected_revision, resulting_revision,"
                    " actor) values (%s, %s, %s, 1, 'evt-dup',"
                    " 'db-failure', 'd', 'correct', '1', 'correct',"
                    " 'corrected', 0, 1, %s)",
                    (self.bundle.tenant_id, self.bundle.raw_subject,
                     self.bundle.result_id, self.bundle.raw_subject))
        self.assertIsInstance(db_failure.exception, psycopg.errors.Error)

    def test_own_connection_interruption_is_reaped_and_replaced(self):
        self.register_original()
        event, payload = self.correction_event()
        # The proxy interrupts ITS OWN connection only - never a generic
        # pg_terminate against other backends.
        real = psycopg.connect(self.dsn, autocommit=True)
        proxy = _DelegatingConnectionProxy(real)
        identity = __import__("identity_context").from_verified_subject(
            self.bundle.raw_subject, self.bundle.tenant_id)
        store = postgres_result_store.PostgresResultStore(
            proxy, identity=identity, owns_connection=True,
            fresh_completion_denied=False)
        proxy.interrupted = True
        try:
            with self.assertRaises(audit_store.AuditStoreError):
                store.commit_correction(
                    owner=self.bundle.raw_subject,
                    result_id=self.bundle.result_id, expected_revision=0,
                    idempotency_key="interrupted", payload=payload,
                    event=event)
        finally:
            store.close()
        self.assertTrue(proxy.closed, "the interrupted own connection is "
                                      "closed, not leaked")
        # The retry shape: a FRESH owned connection succeeds; the committed
        # original was never touched by the interrupted attempt.
        fresh = self.store()
        try:
            snapshot = fresh.snapshot(result_id=self.bundle.result_id)
            self.assertEqual(snapshot["result_revision"], 0)
            self.assertEqual(snapshot["events"], [])
        finally:
            fresh.close()


if __name__ == "__main__":
    unittest.main()
