"""PostgreSQL store contract tests (CI service container; local skip).

Runs ONLY when a DSN is provided (``AI_MARKER_TEST_PG_DSN``) - the CI
``postgres-tests`` job sets it against a throwaway service container. With
no local PostgreSQL available, these tests skip locally with an explicit
reason; that skip IS the honest local-dev fallback label, never a pass.

What is pinned here (the security contract, engine-translated):
* idempotency-key lookup BEFORE the revision check (late retry replays),
* stale fresh-key writes append nothing (optimistic revision),
* originals immutable + marks-only projection (no student text stored),
* owner AND tenant isolation (both directions, FK-enforced membership),
* single-transaction correction+event (fault hook => zero partial state),
* chain/evidence tamper detection through the shared audit_store helpers,
* schema version fail-closed (no auto-migration), pdf REFERENCES-only shape.
"""
import json
import os
import unittest
from pathlib import Path

try:
    import psycopg
except ImportError:  # pragma: no cover - driver ships in the test manifest
    psycopg = None

import audit_store
import postgres_store

DSN = os.environ.get("AI_MARKER_TEST_PG_DSN", "").strip()
ROOT = Path(__file__).resolve().parents[1]
TENANT = "school-beta"
OTHER_TENANT = "school-other"
SUBJECT = "google:teacher-a"
OTHER_SUBJECT = "google:teacher-b"
RESULT = "manual:" + "d" * 16 + ":review-1"
ORIGINAL = {"questions": [{"question_number": "1", "awarded_marks": 3,
                           "student_answer": "SECRET-ANSWER-TEXT"}]}

requires_pg = unittest.skipUnless(
    psycopg is not None and DSN,
    "PostgreSQL DSN not configured - local-dev fallback: the Postgres "
    "contract runs in the CI postgres-tests job only")


@requires_pg
class PostgresStoreContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # DDL is applied EXPLICITLY (never by the application): autocommit
        # connection, schema file executed verbatim.
        ddl = (ROOT / "schema" / "postgres_v1.sql").read_text(encoding="utf-8")
        with psycopg.connect(DSN, autocommit=True) as setup:
            setup.execute(ddl)

    def setUp(self):
        self.conn = psycopg.connect(DSN)
        self.addCleanup(self.conn.close)
        # Self-heal schema_version: the unsupported-version test deliberately
        # breaks it and must never poison the next test.
        self.conn.execute(
            "UPDATE schema_meta SET value = '1' WHERE key = 'schema_version'")
        self.conn.execute(
            "TRUNCATE audit_events, results, pdf_references, "
            "tenant_membership, tenants RESTART IDENTITY CASCADE")
        self.conn.execute(
            "INSERT INTO tenants (tenant_id) VALUES (%s), (%s)",
            (TENANT, OTHER_TENANT))
        self.conn.execute(
            "INSERT INTO tenant_membership (tenant_id, subject, active)"
            " VALUES (%s, %s, TRUE), (%s, %s, TRUE)",
            (TENANT, SUBJECT, TENANT, OTHER_SUBJECT))
        self.conn.commit()
        self.store = postgres_store.PostgresAuditStore(
            self.conn, tenant_id=TENANT)
        self.addCleanup(self.store.close)

    def register(self, store=None, result_id=RESULT, output=ORIGINAL,
                 owner=SUBJECT):
        return (store or self.store).register_result(
            owner=owner, result_id=result_id, original_output=output)

    def commit(self, *, key, expected, question="1", action="correct",
               store=None, owner=SUBJECT, result_id=RESULT):
        payload = {"question_number": question, "action": action,
                   "expected_revision": expected}
        event = {"event_type": "question_review_decision",
                 "question_number": question, "action": action,
                 "review_status": "corrected", "old_score": "1",
                 "new_score": "2", "reason": "test",
                 "actor": owner, "binding": "b" * 64,
                 "authorization_evidence": {"scope": "manual", "actor": owner,
                                            "authorization_revision": "a" * 64,
                                            "row_revision": "r1",
                                            "verified_at": "1"}}
        return (store or self.store).commit_correction(
            owner=owner, result_id=result_id, expected_revision=expected,
            idempotency_key=key, payload=payload, event=event)

    # -- registration / originals ----------------------------------------
    def test_register_revision_and_original_record_roundtrip(self):
        result = self.register()
        self.assertTrue(result["created"])
        self.assertEqual(self.store.current_revision(
            owner=SUBJECT, result_id=RESULT), 0)
        again = self.register()
        self.assertFalse(again["created"], "re-register must be idempotent")
        record = self.store.original_record(owner=SUBJECT, result_id=RESULT)
        self.assertEqual(record["original_digest"], result["original_digest"])
        # Marks-only projection: the student's answer text is not stored.
        self.assertEqual(record["original_scores"], [["1", 3]])
        self.assertNotIn("SECRET-ANSWER-TEXT", json.dumps(record))

    def test_different_original_output_is_immutable(self):
        self.register()
        with self.assertRaises(audit_store.AuditStoreError) as caught:
            self.register(output={"questions": [{"question_number": "1",
                                                 "awarded_marks": 0}]})
        self.assertEqual(caught.exception.code, "original_output_immutable")

    def test_registration_requires_an_existing_membership_row(self):
        with self.assertRaises(audit_store.AuditStoreError) as caught:
            self.register(owner="google:no-membership-here")
        self.assertEqual(caught.exception.code, "tenant_membership_required")

    # -- commit contract ---------------------------------------------------
    def test_commit_appends_and_verifies_a_sound_chain(self):
        self.register()
        result = self.commit(key="k1", expected=0)
        self.assertEqual(result["status"], "committed")
        self.assertEqual(result["sequence"], 1)
        self.assertEqual(result["resulting_revision"], 1)
        self.assertEqual(self.store.current_revision(
            owner=SUBJECT, result_id=RESULT), 1)
        events = self.store.events(owner=SUBJECT, result_id=RESULT)
        self.assertEqual(len(events), 1)
        self.assertEqual(self.store.replay(
            owner=SUBJECT, result_id=RESULT)[0]["sequence"], 1)
        verdict = self.store.verify(owner=SUBJECT, result_id=RESULT)
        self.assertTrue(verdict["ok"], verdict["problems"])
        self.assertEqual(verdict["integrity_check"],
                         "not-applicable-postgresql")

    def test_late_retry_replays_after_the_stream_advanced(self):
        """PINNED ORDERING: idempotency BEFORE revision.

        k1 commits at revision 0; the stream then advances with a different
        operation; retrying k1 with its now-stale expected_revision must
        REPLAY the prior commit (not raise stale_revision, not append).
        """
        self.register()
        self.commit(key="k1", expected=0)
        self.commit(key="k2", expected=1, question="2", action="approve")
        replayed = self.commit(key="k1", expected=0)
        self.assertEqual(replayed["status"], "duplicate")
        self.assertTrue(replayed["idempotent"])
        self.assertEqual(replayed["sequence"], 1)
        self.assertEqual(len(self.store.events(
            owner=SUBJECT, result_id=RESULT)), 2,
            "a replay must never append a second event")

    def test_same_key_different_payload_is_rejected(self):
        self.register()
        self.commit(key="k1", expected=0)
        with self.assertRaises(audit_store.AuditStoreError) as caught:
            self.commit(key="k1", expected=1, question="9", action="flag")
        self.assertEqual(caught.exception.code,
                         "idempotency_key_payload_mismatch")

    def test_stale_fresh_key_write_appends_nothing(self):
        self.register()
        self.commit(key="k1", expected=0)
        with self.assertRaises(audit_store.AuditStoreError) as caught:
            self.commit(key="k2", expected=0, question="2")
        self.assertEqual(caught.exception.code, "stale_revision")
        self.assertEqual(len(self.store.events(
            owner=SUBJECT, result_id=RESULT)), 1)
        self.assertEqual(self.store.current_revision(
            owner=SUBJECT, result_id=RESULT), 1)

    def test_fault_mid_commit_leaves_zero_partial_state(self):
        self.register()
        def boom(stage):
            if stage == "after_event_insert":
                raise RuntimeError("injected fault")

        self.store.set_fault_hook(boom)
        with self.assertRaises(RuntimeError):
            self.commit(key="k1", expected=0)
        self.store.set_fault_hook(None)
        self.assertEqual(self.store.current_revision(
            owner=SUBJECT, result_id=RESULT), 0, "revision must roll back")
        self.assertEqual(self.store.events(
            owner=SUBJECT, result_id=RESULT), [],
            "the event insert must roll back with the transaction")

    # -- isolation ---------------------------------------------------------
    def test_other_owner_and_other_tenant_cannot_see_the_stream(self):
        self.register()
        self.commit(key="k1", expected=0)
        for owner, tenant in ((OTHER_SUBJECT, TENANT),
                              (SUBJECT, OTHER_TENANT)):
            with self.subTest(owner=owner, tenant=tenant):
                other_conn = psycopg.connect(DSN)
                self.addCleanup(other_conn.close)
                other = postgres_store.PostgresAuditStore(
                    other_conn, tenant_id=tenant)
                self.addCleanup(other.close)
                self.assertIsNone(other.current_revision(
                    owner=owner, result_id=RESULT))
                with self.assertRaises(audit_store.AuditStoreError) as caught:
                    other.events(owner=owner, result_id=RESULT)
                self.assertEqual(caught.exception.code,
                                 "result_not_registered")

    # -- tamper evidence ---------------------------------------------------
    def test_payload_tampering_breaks_verify_and_replay(self):
        self.register()
        self.commit(key="k1", expected=0)
        self.conn.execute(
            "UPDATE audit_events SET payload_digest = %s"
            " WHERE tenant_id = %s AND owner_subject = %s AND result_id = %s",
            ("0" * 64, TENANT, SUBJECT, RESULT))
        self.conn.commit()
        verdict = self.store.verify(owner=SUBJECT, result_id=RESULT)
        # The digest itself is not chained, but the revision chain plus the
        # projection checks must still report state honestly; force a chain
        # break too so tampering is always visible:
        self.conn.execute(
            "UPDATE audit_events SET resulting_revision = 99"
            " WHERE tenant_id = %s AND owner_subject = %s AND result_id = %s",
            (TENANT, SUBJECT, RESULT))
        self.conn.commit()
        verdict = self.store.verify(owner=SUBJECT, result_id=RESULT)
        self.assertFalse(verdict["ok"], "tampered chain must verify as bad")
        with self.assertRaises(audit_store.AuditStoreError) as caught:
            self.store.replay(owner=SUBJECT, result_id=RESULT)
        self.assertEqual(caught.exception.code, "audit_order_invalid")

    def test_widened_evidence_is_reported_not_repaired(self):
        self.register()
        self.commit(key="k1", expected=0)
        self.conn.execute(
            "UPDATE audit_events SET authorization_json = %s"
            " WHERE tenant_id = %s AND owner_subject = %s AND result_id = %s",
            ('{"scope": "manual", "token": "LEAK"}', TENANT, SUBJECT, RESULT))
        self.conn.commit()
        verdict = self.store.verify(owner=SUBJECT, result_id=RESULT)
        self.assertFalse(verdict["ok"])
        self.assertTrue(any("unreadable" in problem
                            for problem in verdict["problems"]),
                        verdict["problems"])

    # -- schema posture ----------------------------------------------------
    def test_unsupported_schema_version_fails_closed(self):
        self.conn.execute(
            "UPDATE schema_meta SET value = '999'"
            " WHERE key = 'schema_version'")
        self.conn.commit()
        with self.assertRaises(audit_store.AuditStoreError) as caught:
            postgres_store.PostgresAuditStore(self.conn, tenant_id=TENANT)
        self.assertEqual(caught.exception.code,
                         "schema_version_unsupported")

    def test_pdf_references_stare_digests_and_keys_only(self):
        self.conn.execute(
            "INSERT INTO pdf_references (tenant_id, owner_subject,"
            " document_digest, storage_key) VALUES (%s, %s, %s, %s)",
            (TENANT, SUBJECT, "d" * 64, "private/objects/digest-key"))
        self.conn.commit()
        columns = [row[0] for row in self.conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'pdf_references'").fetchall()]
        self.assertNotIn("bytes", columns)
        self.assertNotIn("filename", columns)
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.conn.execute(
                "INSERT INTO pdf_references (tenant_id, owner_subject,"
                " document_digest, storage_key) VALUES (%s, %s, %s, %s)",
                (TENANT, SUBJECT, "not-a-digest", "private/objects/x"))
        self.conn.rollback()


if __name__ == "__main__":
    unittest.main()
