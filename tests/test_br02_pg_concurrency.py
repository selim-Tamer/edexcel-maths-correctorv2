"""BR-02 PG harness: concurrency contract (offline + PG-gated; live classes
unexecuted by design until the future approval exists - a skip is the honest
gate, never a pass).

Coverage map (gated, REAL operations against the live target):
* barrier-synchronized overlap with DISTINCT backend PIDs (proven via
  ``pg_backend_pid()``, not assumed);
* concurrent identical registration: exactly one creator, identical receipt
  identity, one anchor and one context row;
* concurrent changed-content registration: exactly one winner, the loser is
  refused as ``original_output_immutable``;
* competing reviews on distinct questions: serialized revisions, no lost
  update, exactly one event per accepted request;
* a REPEATABLE READ reader sees a consistent snapshot across a concurrent
  writer's commit, then the new revision after its own commit - synchronized
  by events, never by sleeps.
"""
import threading
import unittest
from pathlib import Path

sys_path_root = str(Path(__file__).resolve().parents[1])
sys_path_tests = str(Path(__file__).resolve().parent)

import sys  # noqa: E402
sys.path.insert(0, sys_path_root)
sys.path.insert(0, sys_path_tests)

try:
    import psycopg
except ImportError:  # pragma: no cover - driver ships in the test manifest
    psycopg = None

import audit_store  # noqa: E402
import br02_pg_fixtures as fixtures  # noqa: E402
import br02_pg_target  # noqa: E402
import identity_context  # noqa: E402
import postgres_result_store  # noqa: E402


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


class _LiveConcurrencyBase(unittest.TestCase):
    """Live base: gate in setUpClass, one unique seeded bundle and one owned
    control connection per test, rows retained, no cleanup SQL anywhere."""

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

    def new_store(self):
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
            document_digest=self.bundle.document_digest)

    def correction_event(self, question, revision, key, reason):
        return ({"actor": self.bundle.canonical_owner,
                 "question_number": question, "action": "correct",
                 "review_status": "corrected", "old_score": "2",
                 "new_score": "3", "reason": reason, "binding": ""},
                {"question_number": question, "action": "correct",
                 "review_status": "corrected", "old_score": "2",
                 "new_score": "3", "reason": reason, "binding": ""},
                revision, key)


class ConcurrencyOverlapTests(_LiveConcurrencyBase):
    """Barrier-synchronized overlap on distinct backend connections."""

    def test_distinct_backends_identical_register_one_creator(self):
        barrier = threading.Barrier(2, timeout=30)
        outcomes = [None, None]

        def register(index):
            store = self.new_store()
            try:
                # psycopg starts a transaction even for SELECT. End this owned
                # observation before the barrier: registration requires an idle
                # dedicated connection and must own its complete transaction.
                with store._conn.transaction():
                    backend = store._conn.execute(
                        "select pg_backend_pid()").fetchone()[0]
                barrier.wait()
                outcomes[index] = (backend,
                                   store.register_with_context(
                                       **self.registration_arguments()))
            except Exception as failure:  # surfaced below, never swallowed
                outcomes[index] = ("error", failure)
            finally:
                store.close()

        threads = [threading.Thread(target=register, args=(i,))
                   for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        for index, outcome in enumerate(outcomes):
            self.assertIsNotNone(outcome, f"thread {index} did not finish")
            self.assertNotEqual(outcome[0], "error", repr(outcome[1]))
        first, second = outcomes
        self.assertNotEqual(first[0], second[0],
                            "the overlap runs on distinct backend PIDs")
        creators = [outcome for outcome in (first, second)
                    if outcome[1].get("created")]
        self.assertEqual(len(creators), 1, "exactly one creator")
        self.assertEqual(first[1]["original_digest"],
                         second[1]["original_digest"])
        anchor_rows = self.conn.execute(
            "select count(*) from results where tenant_id=%s"
            " and owner_subject=%s and result_id=%s",
            (self.bundle.tenant_id, self.bundle.raw_subject,
             self.bundle.result_id)).fetchone()[0]
        context_rows = self.conn.execute(
            "select count(*) from result_context where tenant_id=%s"
            " and membership_subject=%s and result_id=%s",
            (self.bundle.tenant_id, self.bundle.raw_subject,
             self.bundle.result_id)).fetchone()[0]
        self.assertEqual((anchor_rows, context_rows), (1, 1))

    def test_concurrent_changed_content_exactly_one_winner(self):
        changed = {"questions": [{"question_number": "1",
                                  "awarded_marks": 4, "max_marks": 3}]}
        barrier = threading.Barrier(2, timeout=30)
        outcomes = [None, None]

        def register(index, content):
            store = None
            try:
                store = self.new_store()
                barrier.wait()
                outcomes[index] = store.register_with_context(
                    **self.registration_arguments(content))
            except Exception as failure:  # setup/barrier/operation failures too
                outcomes[index] = failure
            finally:
                if store is not None:
                    try:
                        store.close()
                    except Exception as failure:
                        outcomes[index] = failure

        inputs = [self.bundle.original_output, changed]
        threads = [threading.Thread(target=register, args=(i, content),
                                    daemon=True)
                   for i, content in enumerate(inputs)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertFalse(any(thread.is_alive() for thread in threads),
                         "registration workers exceeded bounded joins")
        # Either thread may win; identify the winner and verify stored content.
        for index, outcome in enumerate(outcomes):
            self.assertIsNotNone(outcome, f"worker {index} did not report")
            if isinstance(outcome, Exception) and not isinstance(
                    outcome, audit_store.AuditStoreError):
                raise AssertionError(f"worker {index} failed") from outcome
        dict_outcomes = [(i, o) for i, o in enumerate(outcomes)
                         if isinstance(o, dict)]
        error_outcomes = [o for o in outcomes if isinstance(o, audit_store.AuditStoreError)]
        self.assertEqual(len(dict_outcomes), 1, "exactly one winner")
        self.assertEqual(len(error_outcomes), 1, "the loser is refused")
        winner_index, winner = dict_outcomes[0]
        expected_digest = audit_store.digest_value(inputs[winner_index])
        self.assertEqual(winner["original_digest"],
                         expected_digest)
        self.assertEqual(error_outcomes[0].code, "original_output_immutable")
        stored = self.conn.execute(
            "select original_digest from results where tenant_id=%s"
            " and owner_subject=%s and result_id=%s",
            (self.bundle.tenant_id, self.bundle.raw_subject,
             self.bundle.result_id)).fetchone()[0]
        self.assertEqual(stored, expected_digest)

    def test_competing_reviews_serialize_without_lost_updates(self):
        store = self.new_store()
        try:
            store.register_with_context(**self.registration_arguments())
        finally:
            store.close()
        barrier = threading.Barrier(2, timeout=30)
        outcomes = [None, None]

        def review(index):
            worker = self.new_store()
            try:
                current = worker.current_revision(
                    owner=self.bundle.raw_subject,
                    result_id=self.bundle.result_id)
                question = f"q{index + 1}"
                event, payload, revision, key = self.correction_event(
                    question, current, f"competing-{index}",
                    f"serialized-{index}")
                barrier.wait()
                outcomes[index] = worker.commit_correction(
                    owner=self.bundle.raw_subject,
                    result_id=self.bundle.result_id,
                    expected_revision=revision, idempotency_key=key,
                    payload=payload, event=event)
            except audit_store.AuditStoreError as failure:
                outcomes[index] = failure
            except Exception as failure:
                outcomes[index] = failure
            finally:
                worker.close()

        threads = [threading.Thread(target=review, args=(i,))
                   for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        # Both threads read revision 0 before the barrier. Exactly one commits;
        # the other receives stale_revision because the first writer's commit
        # advances the revision. This is independent of thread scheduling order.
        dict_outcomes = [o for o in outcomes if isinstance(o, dict)]
        stale_outcomes = [o for o in outcomes
                          if isinstance(o, audit_store.AuditStoreError)
                          and o.code == "stale_revision"]
        self.assertEqual(len(dict_outcomes), 1,
                         "exactly one commit; the other is refused stale_revision")
        self.assertEqual(len(stale_outcomes), 1)
        self.assertEqual(dict_outcomes[0]["status"], "committed")
        self.assertEqual(dict_outcomes[0]["resulting_revision"], 1)
        final = self.conn.execute(
            "select count(*) from audit_events where tenant_id=%s"
            " and owner_subject=%s and result_id=%s",
            (self.bundle.tenant_id, self.bundle.raw_subject,
             self.bundle.result_id)).fetchone()[0]
        self.assertEqual(final, 1, "exactly one event persisted")
        revision = self.conn.execute(
            "select result_revision from results where tenant_id=%s"
            " and owner_subject=%s and result_id=%s",
            (self.bundle.tenant_id, self.bundle.raw_subject,
             self.bundle.result_id)).fetchone()[0]
        self.assertEqual(revision, 1, "revision increments once: 0 -> 1")

    def test_consistent_reader_across_writer_commit(self):
        store = self.new_store()
        try:
            store.register_with_context(**self.registration_arguments())
        finally:
            store.close()
        writer_done = threading.Event()
        reader_ready = threading.Event()
        observed = {}

        def reader():
            connection = psycopg.connect(self.dsn)
            try:
                with connection.transaction():
                    connection.execute(
                        "set transaction isolation level repeatable read")
                    connection.execute(
                        "select set_config('app.tenant_id', %s, true)",
                        (self.bundle.tenant_id,))
                    before = connection.execute(
                        "select result_revision from results"
                        " where tenant_id=%s and owner_subject=%s"
                        " and result_id=%s",
                        (self.bundle.tenant_id, self.bundle.raw_subject,
                         self.bundle.result_id)).fetchone()[0]
                    reader_ready.set()
                    writer_done.wait(30)
                    # STILL inside the same REPEATABLE READ transaction:
                    after = connection.execute(
                        "select result_revision from results"
                        " where tenant_id=%s and owner_subject=%s"
                        " and result_id=%s",
                        (self.bundle.tenant_id, self.bundle.raw_subject,
                         self.bundle.result_id)).fetchone()[0]
                observed["before"] = before
                observed["after_same_transaction"] = after
                # A NEW transaction after commit sees the writer's revision.
                connection.execute(
                    "select set_config('app.tenant_id', %s, true)",
                    (self.bundle.tenant_id,))
                observed["after_new_transaction"] = connection.execute(
                    "select result_revision from results"
                    " where tenant_id=%s and owner_subject=%s"
                    " and result_id=%s",
                    (self.bundle.tenant_id, self.bundle.raw_subject,
                     self.bundle.result_id)).fetchone()[0]
            finally:
                connection.close()

        thread = threading.Thread(target=reader)
        thread.start()
        self.assertTrue(reader_ready.wait(30), "reader never started")
        writer = self.new_store()
        try:
            event, payload, revision, key = self.correction_event(
                "1", 0, "writer-commit", "across-reader")
            writer.commit_correction(
                owner=self.bundle.raw_subject,
                result_id=self.bundle.result_id, expected_revision=revision,
                idempotency_key=key, payload=payload, event=event)
        finally:
            writer.close()
        writer_done.set()
        thread.join(60)
        self.assertEqual(observed["before"], 0)
        self.assertEqual(observed["after_same_transaction"], 0,
                         "the REPEATABLE READ snapshot stays consistent "
                         "across the concurrent writer's commit")
        self.assertEqual(observed["after_new_transaction"], 1,
                         "a fresh transaction observes the committed "
                         "revision")


if __name__ == "__main__":
    unittest.main()
