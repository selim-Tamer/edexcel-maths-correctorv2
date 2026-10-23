"""C6-B3 M2 adversarial tests: development-only durable audit store.

Offline and local only: the store under test is a stdlib SQLite file inside a
temporary directory. No network, no provider, no credentials and no real
student data are touched anywhere in this module.

Covered (storage contract in AUDIT_STORAGE_CONTRACT.md):
  - explicit development-only opt-in and development-only labelling
    (the adapter can never be mistaken for school-ready durability),
  - atomic correction + audit commit: a fault injected at any stage rolls
    back BOTH rows, so a correction never exists without its audit event,
  - immutable original AI output reference: a changed original is refused,
    and only a digest + marks projection is stored (never answers/text),
  - owner isolation: another owner can never read, write, verify or export
    a stream it does not own, and the miss leaks nothing,
  - optimistic concurrency: a stale write fails, and concurrent writers lose
    no updates (every commit is exactly one sequence + one revision step),
  - scoped idempotency: same key + same payload returns the prior result,
    same key + different payload is rejected, uniqueness enforced IN STORAGE
    (constraint violations surface even for raw SQL bypassing the API),
  - ordered streams: sequence, revision chain, supersedes references and
    evidence whitelisting are re-validated on replay and fail closed,
  - restart/recovery, snapshot/restore round trip, corrupted-snapshot
    refusal, authorized export, and NO delete/trim/purge API at all.
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audit_store
from audit_store import AuditStoreError

OWNER = "google:teacher-a-subject"
OTHER_OWNER = "google:teacher-b-subject"
RESULT_ID = "manual:0123456789abcdef:review-1"
SENTINEL = "SENTINEL_RAW_ANSWER_7f3a9c"
TOKEN = "synthetic-token-abc123"

_EVENT_INSERT = (
    "INSERT INTO audit_events (owner, result_id, sequence, event_id, "
    "idempotency_key, payload_digest, event_type, question_number, action, "
    "review_status, expected_revision, resulting_revision, actor, created_at) "
    "VALUES (?, ?, ?, ?, ?, 'x', 'question_review_decision', '1', 'approve', "
    "'approved', 0, 1, ?, '1700000000')")


def original_output(numbers=("1", "2")):
    """Pristine AI output carrying sentinel answer/working text (never stored)."""

    return {"questions": [
        {"question_number": number, "max_marks": 2, "awarded_marks": 1,
         "student_answer": f"{SENTINEL} answer {number}",
         "working_summary": f"{SENTINEL} working {number}",
         "notes": f"{SENTINEL} note {number}",
         "confidence": "High"}
        for number in numbers], "validation_warnings": []}


def payload(question_number="1", action="approve", review_status="approved",
            old_score="1", new_score="", reason="", binding="b" * 64,
            expected_revision=0):
    return {"question_number": str(question_number), "action": action,
            "review_status": review_status, "old_score": str(old_score),
            "new_score": str(new_score), "reason": reason,
            "binding": binding, "expected_revision": int(expected_revision)}


def event_fields(question_number="1", action="approve", review_status="approved",
                 old_score="1", new_score="", reason="", actor=OWNER,
                 binding="b" * 64, evidence=None):
    return {"event_type": "question_review_decision",
            "question_number": str(question_number), "action": action,
            "review_status": review_status, "old_score": str(old_score),
            "new_score": str(new_score), "reason": reason, "actor": actor,
            "binding": binding, "authorization_evidence": (
                {"scope": "manual", "actor": actor,
                 "authorization_revision": "d" * 64, "row_revision": "rev-1",
                 "verified_at": "1700000000"} if evidence is None else evidence)}


def exploding(stage_name):
    """Failure-injection hook that aborts a commit at one named stage."""

    def hook(stage):
        if stage == stage_name:
            raise RuntimeError(f"injected fault at {stage}")
    return hook


class StoreTestCase(unittest.TestCase):
    """Shared temporary store path; every test opts in explicitly."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "audit.db")

    def open_store(self, path=None, ack=True):
        store = audit_store.open_store(path or self.path, dev_only_ack=ack)
        self.addCleanup(store.close)
        return store

    def registered_store(self, numbers=("1", "2"), owner=OWNER,
                         result_id=RESULT_ID):
        store = self.open_store()
        store.register_result(owner=owner, result_id=result_id,
                              original_output=original_output(numbers))
        return store

    def commit(self, store, *, owner=OWNER, result_id=RESULT_ID,
               expected_revision=0, question_number="1", action="approve",
               review_status="approved", old_score="1", new_score="",
               reason="", key=None, payload_override=None):
        body = payload(question_number=question_number, action=action,
                       review_status=review_status, old_score=old_score,
                       new_score=new_score, reason=reason,
                       expected_revision=expected_revision)
        if payload_override is not None:
            body = dict(body, **payload_override)
        return store.commit_correction(
            owner=owner, result_id=result_id,
            expected_revision=expected_revision,
            idempotency_key=(key if key is not None else
                             audit_store.make_idempotency_key(
                                 owner=owner, result_id=result_id, payload=body)),
            payload=body,
            event=event_fields(question_number=question_number, action=action,
                               review_status=review_status, old_score=old_score,
                               new_score=new_score, reason=reason, actor=owner))

    def execute(self, sql, params=()):
        """Raw, committed write used only to tamper with the file directly."""

        connection = sqlite3.connect(self.path)
        try:
            connection.execute(sql, params)
            connection.commit()
        finally:
            connection.close()

    def query(self, sql, params=()):
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def store_bytes(self):
        blob = b""
        for name in sorted(os.listdir(self.tmp)):
            with open(os.path.join(self.tmp, name), "rb") as handle:
                blob += handle.read()
        return blob


class OptInAndLabellingTests(StoreTestCase):
    """Persistence only on explicit opt-in, and always labelled dev-only."""

    def test_opening_without_the_opt_in_is_refused_and_writes_nothing(self):
        with self.assertRaises(AuditStoreError) as caught:
            audit_store.open_store(self.path, dev_only_ack=False)
        self.assertEqual(caught.exception.code, "dev_opt_in_required")
        self.assertFalse(os.path.exists(self.path))

    def test_store_is_labelled_development_only_not_school_ready(self):
        store = self.open_store()
        self.assertIn("development-only", store.mode_label)
        self.assertIn("NOT school-ready", store.mode_label)
        self.assertEqual(self.query(
            "SELECT value FROM store_meta WHERE key='mode'")[0][0],
            "development-only")
        self.assertEqual(store.schema_version, audit_store.SCHEMA_VERSION)

    def test_schema_holds_only_meta_results_and_audit_events(self):
        store = self.registered_store()
        self.commit(store)
        tables = {row[0] for row in self.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables & {"store_meta", "results", "audit_events"},
                         {"store_meta", "results", "audit_events"})
        # No grade/publication/approval surface exists here: publication stays
        # a separate, separately gated product concern.
        self.assertFalse(any(name.startswith(("grade", "publication", "approval"))
                             for name in tables))

    def test_unsupported_schema_version_fails_closed(self):
        store = self.open_store()
        store.close()
        self.execute("UPDATE store_meta SET value='99' WHERE key='schema_version'")
        with self.assertRaises(AuditStoreError) as caught:
            audit_store.open_store(self.path, dev_only_ack=True)
        self.assertEqual(caught.exception.code, "schema_version_unsupported")

    def test_a_non_database_file_is_refused_not_overwritten(self):
        garbage = os.path.join(self.tmp, "garbage.db")
        with open(garbage, "wb") as handle:
            handle.write(b"not a database at all")
        with self.assertRaises(AuditStoreError) as caught:
            audit_store.open_store(garbage, dev_only_ack=True)
        self.assertEqual(caught.exception.code, "store_unusable")
        with open(garbage, "rb") as handle:
            self.assertEqual(handle.read(), b"not a database at all")


class AtomicCommitTests(StoreTestCase):
    """Correction and audit event share one fate, at every fault stage."""

    def test_commit_advances_the_stream_and_records_the_event(self):
        store = self.registered_store()
        result = self.commit(store, action="correct", review_status="corrected",
                             new_score="2", reason="scheme B1 missed")
        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["sequence"], 1)
        self.assertEqual(result["resulting_revision"], 1)
        self.assertEqual(store.current_revision(owner=OWNER, result_id=RESULT_ID), 1)
        events = store.events(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["review_status"], "corrected")
        self.assertEqual(events[0]["new_score"], "2")
        self.assertEqual(events[0]["reason"], "scheme B1 missed")
        self.assertEqual(events[0]["expected_revision"], 0)

    def test_a_commit_before_registration_is_refused(self):
        store = self.open_store()
        with self.assertRaises(AuditStoreError) as caught:
            self.commit(store)
        self.assertEqual(caught.exception.code, "result_not_registered")

    def test_fault_after_the_event_write_rolls_back_everything(self):
        store = self.registered_store()
        store.set_fault_hook(exploding("after_event_insert"))
        with self.assertRaises(RuntimeError):
            self.commit(store)
        store.set_fault_hook(None)
        # Neither the audit event nor the revision survived: a correction
        # never exists without its audit event.
        self.assertEqual(store.events(owner=OWNER, result_id=RESULT_ID), [])
        self.assertEqual(store.current_revision(owner=OWNER, result_id=RESULT_ID), 0)
        # The store stays usable afterwards (the fault was transient).
        self.assertEqual(self.commit(store)["status"], "committed")
        self.assertEqual(len(store.events(owner=OWNER, result_id=RESULT_ID)), 1)

    def test_fault_before_commit_rolls_back_everything(self):
        store = self.registered_store()
        for stage in ("begin", "before_commit"):
            with self.subTest(stage=stage):
                store.set_fault_hook(exploding(stage))
                with self.assertRaises(RuntimeError):
                    self.commit(store)
                store.set_fault_hook(None)
                self.assertEqual(store.events(owner=OWNER, result_id=RESULT_ID), [])
                self.assertEqual(
                    store.current_revision(owner=OWNER, result_id=RESULT_ID), 0)

    def test_fault_during_registration_writes_nothing(self):
        store = self.open_store()
        store.set_fault_hook(exploding("begin"))
        with self.assertRaises(RuntimeError):
            store.register_result(owner=OWNER, result_id=RESULT_ID,
                                  original_output=original_output())
        store.set_fault_hook(None)
        self.assertIsNone(
            store.current_revision(owner=OWNER, result_id=RESULT_ID))

    def test_later_events_keep_the_prior_ones_untouched(self):
        store = self.registered_store()
        self.commit(store, question_number="1", action="correct",
                    review_status="corrected", new_score="2", reason="fixed")
        second = self.commit(store, question_number="1", action="approve",
                             review_status="approved", old_score="2",
                             expected_revision=1, key="explicit-second-key",
                             reason="re-checked")
        self.assertEqual(second["sequence"], 2)
        events = store.events(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual([event["sequence"] for event in events], [1, 2])
        self.assertEqual(events[1]["supersedes_event_id"], events[0]["event_id"])
        self.assertEqual(events[0]["old_score"], "1")
        self.assertEqual(events[0]["new_score"], "2")
        # The later approval supersedes without rewriting the first event.
        self.assertEqual(events[1]["new_score"], "")


class ImmutableOriginalTests(StoreTestCase):
    """The original AI output reference is registered once and never replaced."""

    def test_re_registering_the_same_output_is_idempotent(self):
        store = self.open_store()
        first = store.register_result(owner=OWNER, result_id=RESULT_ID,
                                      original_output=original_output())
        second = store.register_result(owner=OWNER, result_id=RESULT_ID,
                                       original_output=original_output())
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["original_digest"], second["original_digest"])
        self.assertEqual(second["result_revision"], 0)

    def test_a_changed_original_output_is_refused(self):
        store = self.registered_store()
        self.commit(store)
        changed = original_output(("1", "2"))
        changed["questions"][0]["awarded_marks"] = 2
        with self.assertRaises(AuditStoreError) as caught:
            store.register_result(owner=OWNER, result_id=RESULT_ID,
                                  original_output=changed)
        self.assertEqual(caught.exception.code, "original_output_immutable")
        record = store.original_record(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual(record["original_scores"], [["1", 1], ["2", 1]])

    def test_the_store_keeps_marks_only_never_answers_or_notes(self):
        store = self.registered_store()
        self.commit(store, action="correct", review_status="corrected",
                    new_score="2", reason="scheme B1 missed")
        blob = self.store_bytes()
        for leaked in (SENTINEL, "student_answer", "working_summary",
                       "full_mark_solution", "notes"):
            self.assertNotIn(leaked.encode("utf-8"), blob,
                             f"{leaked} must never reach the durable store")
        record = store.original_record(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual(record["original_scores"], [["1", 1], ["2", 1]])
        self.assertEqual(len(record["original_digest"]), 64)

    def test_duplicate_question_numbers_stay_visible_in_the_projection(self):
        store = self.open_store()
        store.register_result(owner=OWNER, result_id=RESULT_ID,
                              original_output=original_output(("1", "1")))
        record = store.original_record(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual(record["original_scores"], [["1", 1], ["1", 1]])


class IdempotencyTests(StoreTestCase):
    """Retry safety is scoped to one owner + result stream."""

    def test_same_key_and_same_payload_returns_the_prior_result(self):
        store = self.registered_store()
        first = self.commit(store)
        second = self.commit(store)
        self.assertEqual(second["status"], "duplicate")
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["sequence"], first["sequence"])
        self.assertEqual(second["event_id"], first["event_id"])
        self.assertEqual(len(store.events(owner=OWNER, result_id=RESULT_ID)), 1)
        self.assertEqual(store.current_revision(owner=OWNER, result_id=RESULT_ID), 1)

    def test_same_key_with_a_different_payload_is_rejected(self):
        store = self.registered_store()
        self.commit(store, key="fixed-key")
        with self.assertRaises(AuditStoreError) as caught:
            self.commit(store, key="fixed-key", action="correct",
                        review_status="corrected", new_score="2",
                        reason="changed my mind",
                        payload_override={"new_score": "2"})
        self.assertEqual(caught.exception.code, "idempotency_key_payload_mismatch")
        events = store.events(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["new_score"], "")

    def test_an_explicitly_different_correction_is_a_new_operation(self):
        store = self.registered_store()
        self.commit(store)
        self.commit(store, action="correct", review_status="corrected",
                    new_score="2", reason="different decision",
                    expected_revision=1)
        events = store.events(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual([event["sequence"] for event in events], [1, 2])
        self.assertEqual(store.current_revision(owner=OWNER, result_id=RESULT_ID), 2)

    def test_idempotency_keys_are_scoped_per_owner_and_stream(self):
        store = self.registered_store()
        store.register_result(owner=OTHER_OWNER, result_id=RESULT_ID,
                              original_output=original_output())
        key = "shared-key"
        self.assertEqual(self.commit(store, key=key)["status"], "committed")
        self.assertEqual(
            self.commit(store, owner=OTHER_OWNER, key=key)["status"], "committed")
        self.assertEqual(len(store.events(owner=OTHER_OWNER, result_id=RESULT_ID)), 1)
        # A different result stream under the same owner is scoped too.
        store.register_result(owner=OWNER, result_id=RESULT_ID + "-b",
                              original_output=original_output())
        third = self.commit(store, result_id=RESULT_ID + "-b", key=key)
        self.assertEqual(third["status"], "committed")

    def test_uniqueness_is_enforced_in_storage_not_only_in_python(self):
        store = self.registered_store()
        self.commit(store)
        duplicate_key = self.commit(store, question_number="2",
                                    expected_revision=1)["idempotency_key"]
        connection = sqlite3.connect(self.path)
        try:
            # Duplicate stream sequence, written around the API entirely.
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(_EVENT_INSERT,
                                   (OWNER, RESULT_ID, 1, "raw-dup-seq",
                                    "raw-key-1", OWNER))
            # Duplicate scoped idempotency key, same story.
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(_EVENT_INSERT,
                                   (OWNER, RESULT_ID, 99, "raw-dup-key",
                                    duplicate_key, OWNER))
        finally:
            connection.rollback()
            connection.close()
        # The tampered writes are gone; the committed stream still verifies.
        events = store.events(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual([event["sequence"] for event in events], [1, 2])
        self.assertTrue(store.verify(owner=OWNER, result_id=RESULT_ID)["ok"])


class OrderingAndReplayTests(StoreTestCase):
    """Streams are ordered, chained and replayed fail-closed."""

    def test_replay_returns_the_ordered_chain(self):
        store = self.registered_store()
        self.commit(store, question_number="1", action="correct",
                    review_status="corrected", new_score="2", reason="fixed")
        self.commit(store, question_number="2", expected_revision=1)
        self.commit(store, question_number="1", action="approve",
                    review_status="approved", old_score="2", expected_revision=2)
        replayed = store.replay(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual([event["sequence"] for event in replayed], [1, 2, 3])
        self.assertEqual([event["resulting_revision"] for event in replayed],
                         [1, 2, 3])
        self.assertEqual([event["expected_revision"] for event in replayed],
                         [0, 1, 2])
        self.assertEqual(replayed[2]["supersedes_event_id"],
                         replayed[0]["event_id"])
        self.assertIsNone(replayed[1]["supersedes_event_id"])

    def test_a_gap_in_the_stream_fails_the_replay_closed(self):
        store = self.registered_store()
        self.commit(store)
        self.commit(store, question_number="2", expected_revision=1)
        self.execute("DELETE FROM audit_events WHERE owner=? AND result_id=? "
                     "AND sequence=1", (OWNER, RESULT_ID))
        with self.assertRaises(AuditStoreError) as caught:
            store.replay(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual(caught.exception.code, "audit_order_invalid")

    def test_a_broken_revision_chain_fails_the_replay_closed(self):
        store = self.registered_store()
        self.commit(store)
        self.commit(store, question_number="2", expected_revision=1)
        self.execute("UPDATE audit_events SET resulting_revision=7 WHERE owner=? "
                     "AND result_id=? AND sequence=2", (OWNER, RESULT_ID))
        with self.assertRaises(AuditStoreError) as caught:
            store.replay(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual(caught.exception.code, "audit_order_invalid")

    def test_tampered_evidence_is_rejected_on_read(self):
        store = self.registered_store()
        self.commit(store)
        self.execute(
            "UPDATE audit_events SET authorization_json=? WHERE owner=? "
            "AND result_id=?",
            ('{"scope": "manual", "actor": "x", "authorization_revision": '
             '"d", "row_revision": "", "verified_at": "", "token": "%s"}'
             % TOKEN, OWNER, RESULT_ID))
        with self.assertRaises(AuditStoreError) as caught:
            store.events(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual(caught.exception.code, "audit_record_invalid")

    def test_verify_reports_tampering_instead_of_trusting_the_file(self):
        store = self.registered_store()
        self.commit(store)
        healthy = store.verify(owner=OWNER, result_id=RESULT_ID)
        self.assertTrue(healthy["ok"], healthy)
        self.assertEqual(healthy["problems"], [])
        self.assertEqual(healthy["event_count"], 1)
        self.assertEqual(healthy["integrity_check"], "ok")
        # A revision row that no event chain explains.
        self.execute("UPDATE results SET result_revision=5 WHERE owner=? "
                     "AND result_id=?", (OWNER, RESULT_ID))
        report = store.verify(owner=OWNER, result_id=RESULT_ID)
        self.assertFalse(report["ok"])
        self.assertTrue(any(problem.startswith("result_revision_mismatch")
                            for problem in report["problems"]), report)
        # A tampered marks projection.
        self.execute("UPDATE results SET original_scores=? WHERE owner=? "
                     "AND result_id=?", ('[["1", 2]]', OWNER, RESULT_ID))
        report = store.verify(owner=OWNER, result_id=RESULT_ID)
        self.assertFalse(report["ok"])
        self.assertIn("original_projection_digest_mismatch", report["problems"])

    def test_verify_of_an_unregistered_stream_reports_not_registered(self):
        store = self.registered_store()
        report = store.verify(owner=OWNER, result_id="manual:missing:row")
        self.assertFalse(report["ok"])
        self.assertEqual(report["problems"], ["result_not_registered"])


class ConcurrencyTests(StoreTestCase):
    """Optimistic revisions: stale writes fail, concurrent writers lose nothing."""

    def test_a_stale_write_fails_and_changes_nothing(self):
        store = self.registered_store()
        self.commit(store, question_number="1")
        with self.assertRaises(AuditStoreError) as caught:
            self.commit(store, question_number="2", expected_revision=0)
        self.assertEqual(caught.exception.code, "stale_revision")
        self.assertEqual(len(store.events(owner=OWNER, result_id=RESULT_ID)), 1)
        self.assertEqual(store.current_revision(owner=OWNER, result_id=RESULT_ID), 1)

    def test_two_writers_on_one_revision_exactly_one_wins(self):
        writer_a = self.registered_store()
        writer_b = self.open_store()
        both_read = writer_a.current_revision(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual(both_read, 0)
        first = self.commit(writer_a, question_number="1")
        self.assertEqual(first["status"], "committed")
        with self.assertRaises(AuditStoreError) as caught:
            self.commit(writer_b, question_number="2", expected_revision=both_read)
        self.assertEqual(caught.exception.code, "stale_revision")
        events = writer_a.events(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["question_number"], "1")
        self.assertTrue(writer_a.verify(owner=OWNER, result_id=RESULT_ID)["ok"])

    def test_concurrent_workers_commit_a_dense_lossless_stream(self):
        workers, per_worker = 3, 6
        seed = self.registered_store()
        self.commit(seed, question_number="warmup")
        seed.close()
        barrier = threading.Barrier(workers)
        failures = []

        def run(index):
            store = None
            try:
                store = audit_store.open_store(self.path, dev_only_ack=True)
                barrier.wait()
                for attempt in range(per_worker):
                    body = {"worker": index, "attempt": attempt}
                    for _ in range(50):
                        revision = store.current_revision(
                            owner=OWNER, result_id=RESULT_ID) or 0
                        keyed = dict(body, expected_revision=revision)
                        try:
                            store.commit_correction(
                                owner=OWNER, result_id=RESULT_ID,
                                expected_revision=revision,
                                idempotency_key=audit_store.make_idempotency_key(
                                    owner=OWNER, result_id=RESULT_ID,
                                    payload=keyed),
                                payload=keyed,
                                event=event_fields(
                                    question_number=f"{index}-{attempt}",
                                    reason=f"worker {index} attempt {attempt}"))
                            break
                        except AuditStoreError as error:
                            if error.code != "stale_revision":
                                failures.append(error)
                                break
                    else:
                        failures.append(AssertionError("commit never succeeded"))
            except Exception as error:  # pragma: no cover - diagnostic path
                failures.append(error)
            finally:
                if store is not None:
                    store.close()

        threads = [threading.Thread(target=run, args=(index,))
                   for index in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertEqual(failures, [])
        store = self.open_store()
        expected = 1 + workers * per_worker
        self.assertEqual(store.current_revision(owner=OWNER, result_id=RESULT_ID),
                         expected)
        events = store.events(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual([event["sequence"] for event in events],
                         list(range(1, expected + 1)))
        self.assertEqual(len({event["event_id"] for event in events}), expected)
        self.assertTrue(store.verify(owner=OWNER, result_id=RESULT_ID)["ok"])


class OwnerIsolationTests(StoreTestCase):
    """One owner's stream is invisible and untouchable to another owner."""

    def test_another_owner_never_reads_writes_or_exports_the_stream(self):
        store = self.registered_store()
        self.commit(store)
        for call in (lambda: store.events(owner=OTHER_OWNER, result_id=RESULT_ID),
                     lambda: store.replay(owner=OTHER_OWNER, result_id=RESULT_ID),
                     lambda: store.original_record(owner=OTHER_OWNER,
                                                   result_id=RESULT_ID),
                     lambda: store.export_stream(owner=OTHER_OWNER,
                                                 result_id=RESULT_ID)):
            with self.subTest(call=call):
                with self.assertRaises(AuditStoreError) as caught:
                    call()
                self.assertEqual(caught.exception.code, "result_not_registered")
        with self.assertRaises(AuditStoreError) as caught:
            self.commit(store, owner=OTHER_OWNER, question_number="2")
        self.assertEqual(caught.exception.code, "result_not_registered")
        # The owner's own stream is untouched by the foreign attempts.
        self.assertEqual(len(store.events(owner=OWNER, result_id=RESULT_ID)), 1)

    def test_owners_keep_independent_streams_for_the_same_result_id(self):
        store = self.registered_store()
        store.register_result(owner=OTHER_OWNER, result_id=RESULT_ID,
                              original_output=original_output())
        self.commit(store, question_number="1")
        self.commit(store, owner=OTHER_OWNER, question_number="9")
        self.commit(store, owner=OTHER_OWNER, question_number="9",
                    expected_revision=1)
        mine = store.events(owner=OWNER, result_id=RESULT_ID)
        theirs = store.events(owner=OTHER_OWNER, result_id=RESULT_ID)
        self.assertEqual([event["sequence"] for event in mine], [1])
        self.assertEqual([event["sequence"] for event in theirs], [1, 2])
        self.assertEqual(mine[0]["actor"], OWNER)
        self.assertEqual(theirs[0]["actor"], OTHER_OWNER)
        # One owner's originals never overwrite the other's registration.
        changed = original_output(("1", "2"))
        changed["questions"][0]["awarded_marks"] = 2
        with self.assertRaises(AuditStoreError):
            store.register_result(owner=OTHER_OWNER, result_id=RESULT_ID,
                                  original_output=changed)
        self.assertEqual(
            store.original_record(owner=OWNER, result_id=RESULT_ID)["original_scores"],
            [["1", 1], ["2", 1]])

    def test_an_empty_owner_or_result_id_is_refused(self):
        store = self.registered_store()
        for bad in ("", "   ", None, 5):
            with self.subTest(bad=bad):
                with self.assertRaises(AuditStoreError):
                    store.events(owner=bad, result_id=RESULT_ID)
                with self.assertRaises(AuditStoreError):
                    store.events(owner=OWNER, result_id=bad)


class RestartRecoveryTests(StoreTestCase):
    """History, originals and revisions survive a process restart."""

    def test_restart_preserves_history_and_originals(self):
        store = self.registered_store()
        self.commit(store, question_number="1", action="correct",
                    review_status="corrected", new_score="2", reason="fixed")
        self.commit(store, question_number="2", expected_revision=1)
        before = store.export_stream(owner=OWNER, result_id=RESULT_ID)
        digest = before["result"]["original_digest"]
        store.close()
        reopened = self.open_store()
        after = reopened.export_stream(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual(after, before)
        self.assertEqual(after["result"]["original_digest"], digest)
        self.assertEqual(after["result"]["result_revision"], 2)
        self.assertTrue(reopened.verify(owner=OWNER, result_id=RESULT_ID)["ok"])
        # A fresh decision after the restart continues the same ordered stream.
        third = self.commit(reopened, question_number="3", expected_revision=2)
        self.assertEqual(third["sequence"], 3)
        self.assertEqual(third["resulting_revision"], 3)

    def test_a_second_open_of_the_same_file_is_safe_and_shared(self):
        first = self.registered_store()
        second = self.open_store()
        self.commit(first, question_number="1")
        self.assertEqual(second.current_revision(owner=OWNER, result_id=RESULT_ID), 1)
        self.commit(second, question_number="2", expected_revision=1)
        self.assertEqual(len(first.events(owner=OWNER, result_id=RESULT_ID)), 2)

    def test_operations_on_a_closed_store_fail_closed(self):
        store = self.registered_store()
        store.close()
        with self.assertRaises(AuditStoreError) as caught:
            store.events(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual(caught.exception.code, "store_closed")


class SnapshotRestoreTests(StoreTestCase):
    """Explicit snapshot and verified restore of the dev-only store."""

    def test_snapshot_and_restore_round_trip(self):
        store = self.registered_store()
        self.commit(store, question_number="1", action="correct",
                    review_status="corrected", new_score="2", reason="fixed")
        snapshot = os.path.join(self.tmp, "snapshots", "audit-1.db")
        info = store.snapshot_to(snapshot)
        self.assertTrue(os.path.exists(snapshot))
        self.assertEqual(len(info["sha256"]), 64)
        expected = store.export_stream(owner=OWNER, result_id=RESULT_ID)
        store.close()
        # Disaster: the live store is lost entirely.
        for name in os.listdir(self.tmp):
            if name.startswith("audit.db"):
                os.remove(os.path.join(self.tmp, name))
        report = audit_store.restore_from(snapshot, self.path, dev_only_ack=True)
        self.assertTrue(report["restored"])
        recovered = self.open_store()
        self.assertEqual(recovered.export_stream(owner=OWNER, result_id=RESULT_ID),
                         expected)
        self.assertTrue(recovered.verify(owner=OWNER, result_id=RESULT_ID)["ok"])
        self.assertEqual(recovered.current_revision(owner=OWNER, result_id=RESULT_ID), 1)

    def test_restoring_a_corrupted_snapshot_leaves_the_store_intact(self):
        store = self.registered_store()
        self.commit(store)
        snapshot = os.path.join(self.tmp, "audit-2.db")
        store.snapshot_to(snapshot)
        before = store.export_stream(owner=OWNER, result_id=RESULT_ID)
        store.close()
        with open(snapshot, "r+b") as handle:
            handle.seek(0)
            handle.write(b"CORRUPTED SNAPSHOT BYTES")
        with self.assertRaises(AuditStoreError) as caught:
            audit_store.restore_from(snapshot, self.path, dev_only_ack=True)
        self.assertEqual(caught.exception.code, "snapshot_invalid")
        reopened = self.open_store()
        self.assertEqual(reopened.export_stream(owner=OWNER, result_id=RESULT_ID),
                         before)

    def test_restore_requires_the_explicit_opt_in_and_a_real_snapshot(self):
        with self.assertRaises(AuditStoreError) as caught:
            audit_store.restore_from("missing.db", self.path, dev_only_ack=False)
        self.assertEqual(caught.exception.code, "dev_opt_in_required")
        with self.assertRaises(AuditStoreError) as caught:
            audit_store.restore_from(os.path.join(self.tmp, "missing.db"),
                                     self.path, dev_only_ack=True)
        self.assertEqual(caught.exception.code, "snapshot_missing")

    def test_a_non_store_file_is_never_restored(self):
        store = self.registered_store()
        self.commit(store)
        store.close()
        fake = os.path.join(self.tmp, "fake.db")
        other = sqlite3.connect(fake)
        other.execute("CREATE TABLE unrelated (id INTEGER)")
        other.commit()
        other.close()
        with self.assertRaises(AuditStoreError) as caught:
            audit_store.restore_from(fake, self.path, dev_only_ack=True)
        self.assertEqual(caught.exception.code, "snapshot_invalid")
        reopened = self.open_store()
        self.assertEqual(reopened.current_revision(owner=OWNER, result_id=RESULT_ID), 1)


class ExportAndNoErasureTests(StoreTestCase):
    """Export exists for authorized data access; erasure has no API at all."""

    def test_export_carries_marks_and_events_but_no_answers(self):
        store = self.registered_store()
        self.commit(store, action="correct", review_status="corrected",
                    new_score="2", reason="scheme B1 missed")
        export = store.export_stream(owner=OWNER, result_id=RESULT_ID)
        self.assertIn("development-only", export["mode"])
        self.assertEqual(export["result"]["result_revision"], 1)
        self.assertEqual(len(export["events"]), 1)
        blob = audit_store.canonical_json(export)
        self.assertNotIn(SENTINEL, blob)
        self.assertNotIn(TOKEN, blob)
        self.assertEqual(export["events"][0]["authorization_evidence"]["scope"],
                         "manual")

    def test_the_store_exposes_no_delete_trim_or_purge_surface(self):
        store = self.registered_store()
        forbidden = ("delete", "purge", "drop", "truncate", "clear", "remove",
                     "erase", "expire", "retention")
        exposed = [name for name in dir(store)
                   if not name.startswith("_") and callable(getattr(store, name))]
        for name in exposed:
            self.assertFalse(name.lower().startswith(forbidden),
                             f"{name} would be a silent-erasure surface")
        # The complete public surface is exactly this reviewed set.
        self.assertEqual(sorted(exposed), sorted([
            "close", "commit_correction", "current_revision", "events",
            "export_stream", "original_record", "register_result", "replay",
            "set_fault_hook", "snapshot_to", "verify"]))


class EvidenceAndBoundsTests(StoreTestCase):
    """The store re-whitelists evidence and bounds every stored field."""

    def test_store_level_whitelist_drops_unknown_evidence_keys(self):
        store = self.registered_store()
        store.commit_correction(
            owner=OWNER, result_id=RESULT_ID, expected_revision=0,
            idempotency_key="evidence-key", payload=payload(),
            event=event_fields(evidence={"scope": "classroom", "actor": OWNER,
                                         "authorization_revision": "d" * 64,
                                         "row_revision": "rev-1",
                                         "verified_at": "1700000000",
                                         "access_token": TOKEN,
                                         "student_email": "learner@example.test"}))
        evidence = store.events(owner=OWNER, result_id=RESULT_ID)[0][
            "authorization_evidence"]
        self.assertEqual(set(evidence), set(audit_store.EVIDENCE_FIELDS))
        blob = audit_store.canonical_json(evidence)
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn("learner@example.test", blob)
        self.assertEqual(evidence["scope"], "classroom")

    def test_non_mapping_evidence_is_refused(self):
        store = self.registered_store()
        for index, bad in enumerate((TOKEN, ["scope"], 5)):
            with self.subTest(bad=bad):
                with self.assertRaises(AuditStoreError) as caught:
                    store.commit_correction(
                        owner=OWNER, result_id=RESULT_ID, expected_revision=0,
                        idempotency_key=f"bad-evidence-{index}",
                        payload=payload(), event=event_fields(evidence=bad))
                self.assertEqual(caught.exception.code, "evidence_invalid")
        self.assertEqual(store.events(owner=OWNER, result_id=RESULT_ID), [])

    def test_oversized_evidence_values_are_bounded_not_stored_verbatim(self):
        store = self.registered_store()
        store.commit_correction(
            owner=OWNER, result_id=RESULT_ID, expected_revision=0,
            idempotency_key="oversized-evidence", payload=payload(),
            event=event_fields(evidence={"scope": "manual", "actor": OWNER * 100,
                                         "authorization_revision": "d",
                                         "row_revision": "", "verified_at": ""}))
        evidence = store.events(owner=OWNER, result_id=RESULT_ID)[0][
            "authorization_evidence"]
        self.assertLessEqual(len(evidence["actor"]),
                             audit_store.EVIDENCE_FIELD_LIMIT)
        self.assertNotIn(OWNER * 100, evidence["actor"])

    def test_unbounded_identifiers_and_revisions_are_refused(self):
        store = self.registered_store()
        with self.assertRaises(AuditStoreError):
            store.commit_correction(
                owner=OWNER, result_id=RESULT_ID, expected_revision=0,
                idempotency_key="x" * 600, payload=payload(),
                event=event_fields())
        with self.assertRaises(AuditStoreError):
            store.commit_correction(
                owner=OWNER, result_id=RESULT_ID, expected_revision="not-a-number",
                idempotency_key="bad-revision", payload=payload(),
                event=event_fields())
        with self.assertRaises(AuditStoreError):
            store.commit_correction(
                owner=OWNER, result_id=RESULT_ID, expected_revision=-1,
                idempotency_key="negative-revision", payload=payload(),
                event=event_fields())
        with self.assertRaises(AuditStoreError) as caught:
            store.commit_correction(
                owner=OWNER, result_id=RESULT_ID, expected_revision=0,
                idempotency_key="no-question", payload=payload(),
                event=event_fields(question_number=""))
        self.assertEqual(caught.exception.code, "question_number_invalid")
        self.assertEqual(store.events(owner=OWNER, result_id=RESULT_ID), [])

    def test_fault_hook_must_be_callable(self):
        store = self.registered_store()
        with self.assertRaises(AuditStoreError):
            store.set_fault_hook("not-callable")


if __name__ == "__main__":
    unittest.main()
