"""API slice A tests: auth, envelope, decision commit, scoping, rollback.

Offline and in-process: ``fastapi.testclient`` drives the app, a temporary
SQLite ``audit_store`` stands in for persistence, and the shared loader's
network fakes are not needed because this module never imports the Streamlit
app. A green run proves: fail-closed authentication, the contract's error
envelope, owner-scoped result/event access, store-enforced optimistic
concurrency with idempotent replay, store-backed sticky-flag prior state, and
that the defaults refuse manual provenance and Classroom rows.
"""
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audit_store
import question_review as qr
from api.app import create_app
from fastapi.testclient import TestClient
from membership_store import Membership


def active_member(subject: str) -> Membership:
    """Fixture membership: every fake-verified subject is one active teacher."""
    return Membership(subject, "tenant-beta", "teacher")

SUBJECT = "alice-subject"
OWNER = "google:" + SUBJECT
OTHER_SUBJECT = "bob-subject"
OTHER_OWNER = "google:" + OTHER_SUBJECT
TOKENS = {"tok-alice": SUBJECT, "tok-bob": OTHER_SUBJECT}
PDF = b"%PDF-1.7 api slice a fixture"
DIGEST = hashlib.sha256(PDF).hexdigest()


def question():
    return {"question_number": "1", "awarded_marks": 1, "max_marks": 2,
            "student_answer": "42", "working_summary": "34 + 8",
            "scheme_answer_evidence": "scheme", "full_mark_solution": "42",
            "mark_breakdown": [], "confidence": "High", "loss_reason": "",
            "correct_method": "method"}


def manual_row(owner=OWNER, review_key="review-1"):
    questions = [question()]
    return {"status": "Marked", "review_key": review_key,
            "report_owner": owner, "snapshot_context": DIGEST,
            "revision": "", "selected_questions": ["1"],
            "ai_result": {"questions": questions},
            "result": {"questions": [dict(q) for q in questions]},
            "student_name": "Synthetic", "marking_model": "fake",
            "audit_model": "Not run"}


def classroom_row(owner=OWNER):
    row = manual_row(owner=owner, review_key="classroom_review-1")
    row.update({"classroom_course_id": "course-1",
                "classroom_assignment_id": "work-1",
                "classroom_context": "c" * 64, "submission_id": "sub-1",
                "revision": "rev-1"})
    return row


def result_id_of(row):
    import decision_core
    return decision_core.durable_result_identity(row, "")


class ApiTestCase(unittest.TestCase):
    """Shared app + temporary dev store + two identities."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = audit_store.open_store(
            str(Path(self.tmp.name) / "audit.sqlite3"), dev_only_ack=True)
        self.addCleanup(self.store.close)
        self.rows = {
            OWNER: [manual_row()],
            OTHER_OWNER: [],
        }
        self.app = create_app(
            verify_token=TOKENS.get,
            rows_for=lambda subject, owner: self.rows.get(owner, []),
            store_provider=lambda: (self.store, None),
            manual_document_provenance_ok=lambda row: True,
            membership_resolver=active_member,
        )
        self.client = TestClient(self.app)

    def auth(self, token="tok-alice"):
        return {"Authorization": f"Bearer {token}"}


def result_id_of(row):
    import decision_core
    return decision_core.durable_result_identity(row, "")


class HealthAndAuthTests(ApiTestCase):

    def test_healthz_is_public_and_content_free(self):
        response = self.client.get("/v1/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_missing_and_bad_tokens_fail_closed_with_the_envelope(self):
        result_id = result_id_of(manual_row())
        for headers in ({}, {"Authorization": "Basic dXNlcjpwdw=="},
                        {"Authorization": "Bearer not-a-token"},
                        {"Authorization": "Bearer"}):
            with self.subTest(headers=headers):
                response = self.client.post(
                    f"/v1/results/{result_id}/decisions", headers=headers,
                    json={"question_number": "1", "action": "approve",
                          "expected_revision": 0, "idempotency_key": "k1"})
                self.assertEqual(response.status_code, 401)
                error = response.json()["error"]
                self.assertEqual(error["code"], "authentication_required")
                # The envelope is top-level; no framework "detail" wrapper.
                self.assertIn("message", error)
                self.assertNotIn("detail", response.json())
        # Nothing was written anywhere.
        self.assertIsNone(self.store.current_revision(
            owner=OWNER, result_id=result_id))


class DecisionEndpointTests(ApiTestCase):

    def decide(self, **overrides):
        body = {"question_number": "1", "action": "correct",
                "expected_revision": 0, "idempotency_key": "client-key-1",
                "reason": "misread the working", "new_score": "2"}
        result_id = overrides.pop("_result_id", result_id_of(manual_row()))
        body.update(overrides)
        return self.client.post(f"/v1/results/{result_id}/decisions",
                                headers=self.auth(), json=body)

    def test_decision_returns_the_contract_response_and_commits(self):
        response = self.decide()
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["result_id"], result_id_of(manual_row()))
        self.assertEqual(payload["result_revision"], 1)
        self.assertEqual(payload["event_sequence"], 1)
        self.assertTrue(payload["audit_reference"])
        self.assertFalse(payload["replayed"])
        self.assertEqual(payload["idempotency_key"], "client-key-1")
        events = self.store.events(owner=OWNER,
                                   result_id=result_id_of(manual_row()))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["review_status"], "corrected")
        self.assertEqual(events[0]["new_score"], "2")
        self.assertEqual(events[0]["actor"], OWNER)
        # Original AI marks are untouched: the store keeps marks-only history.
        original = self.store.original_record(
            owner=OWNER, result_id=result_id_of(manual_row()))
        self.assertTrue(original)

    def test_identical_retry_replays_without_a_second_event(self):
        first = self.decide()
        second = self.decide()
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertFalse(first.json()["replayed"])
        self.assertTrue(second.json()["replayed"])
        events = self.store.events(owner=OWNER,
                                   result_id=result_id_of(manual_row()))
        self.assertEqual(len(events), 1)
        self.assertEqual(second.json()["event_sequence"], 1)
        self.assertEqual(second.json()["result_revision"], 1)

    def test_stale_client_revision_is_rejected_by_the_store(self):
        self.assertEqual(self.decide().status_code, 200)
        # A different payload (new decision, fresh client key) with the
        # now-stale revision: the store's transaction refuses it.
        response = self.decide(action="approve", reason="checked",
                               new_score=None, idempotency_key="client-key-2")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "stale_revision")
        events = self.store.events(owner=OWNER,
                                   result_id=result_id_of(manual_row()))
        self.assertEqual(len(events), 1, "a stale write must append nothing")

    def test_wrong_revision_on_a_new_stream_is_rejected(self):
        response = self.decide(expected_revision=7)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "stale_revision")
        # Registration (the immutable original reference) may exist after a
        # refused commit -- exactly as in the application path -- but no
        # event may have been written.
        self.assertIn(self.store.current_revision(
            owner=OWNER, result_id=result_id_of(manual_row())), (None, 0))
        self.assertEqual(self.store.events(
            owner=OWNER, result_id=result_id_of(manual_row())), [])

    def test_sticky_flag_prior_state_comes_from_the_store(self):
        flagged = self.decide(action="flag", reason="answer unclear",
                              new_score=None)
        self.assertEqual(flagged.status_code, 200, flagged.text)
        # A reason-less approval over the SAME evidence must stay refused,
        # even though this request carries no session state at all.
        approved = self.decide(action="approve", reason="", new_score=None,
                               idempotency_key="client-key-2")
        self.assertEqual(approved.status_code, 400, approved.text)
        self.assertEqual(approved.json()["error"]["code"],
                         "flagged_decision_requires_reason")
        # Withdrawing the flag requires a stated reason, like the app; the
        # stream is now at revision 1 (the flag commit).
        withdrawn = self.decide(action="approve", reason="verified against scheme",
                                new_score=None, idempotency_key="client-key-3",
                                expected_revision=1)
        self.assertEqual(withdrawn.status_code, 200, withdrawn.text)
        events = self.store.events(owner=OWNER,
                                   result_id=result_id_of(manual_row()))
        self.assertEqual([e["review_status"] for e in events],
                         ["flagged", "approved"])

    def test_invalid_action_and_out_of_range_score_use_source_codes(self):
        bad_action = self.decide(action="release-grade")
        self.assertEqual(bad_action.status_code, 400)
        self.assertEqual(bad_action.json()["error"]["code"],
                         "review_action_invalid")
        expected_code = qr.validate_mark_value(99, 2)[1]
        bad_score = self.decide(new_score=99)
        self.assertEqual(bad_score.status_code, 400)
        self.assertEqual(bad_score.json()["error"]["code"], expected_code)

    def test_client_evidence_claims_are_checked_against_the_server(self):
        import decision_core
        good_binding = decision_core.question_review_binding(
            "review-1", manual_row(), "1", "")
        for claim in ({"old_score": "5"},
                      {"binding": "0" * 64},
                      {"review_status": "flagged"}):
            with self.subTest(claim=claim):
                response = self.decide(**claim)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["code"],
                                 "evidence_invalid")
        # The same claims when they match the server's derivation succeed.
        matching = self.decide(old_score="1", binding=good_binding,
                               review_status="corrected")
        self.assertEqual(matching.status_code, 200, matching.text)

    def test_invalid_body_uses_the_payload_invalid_envelope(self):
        response = self.client.post(
            f"/v1/results/{result_id_of(manual_row())}/decisions",
            headers=self.auth(), json={"action": "approve"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "payload_invalid")
        negative = self.client.post(
            f"/v1/results/{result_id_of(manual_row())}/decisions",
            headers=self.auth(),
            json={"question_number": "1", "action": "approve",
                  "expected_revision": -1, "idempotency_key": "k"})
        self.assertEqual(negative.status_code, 400)
        self.assertEqual(negative.json()["error"]["code"], "payload_invalid")


class FailClosedDefaultTests(ApiTestCase):

    def test_without_a_provenance_grant_the_decision_is_refused(self):
        app = create_app(verify_token=TOKENS.get,
                         rows_for=lambda subject, owner: [manual_row()],
                         store_provider=lambda: (self.store, None),
                         membership_resolver=active_member)
        client = TestClient(app)
        response = client.post(
            f"/v1/results/{result_id_of(manual_row())}/decisions",
            headers=self.auth(),
            json={"question_number": "1", "action": "approve",
                  "expected_revision": 0, "idempotency_key": "k"})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"],
                         "manual_document_provenance_required")
        self.assertIsNone(self.store.current_revision(
            owner=OWNER, result_id=result_id_of(manual_row())))

    def test_classroom_rows_are_refused_until_reverification_is_wired(self):
        row = classroom_row()
        self.rows[OWNER] = [row]
        response = self.client.post(
            f"/v1/results/{result_id_of(row)}/decisions",
            headers=self.auth(),
            json={"question_number": "1", "action": "approve",
                  "expected_revision": 0, "idempotency_key": "k"})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"],
                         "classroom_verification_unavailable")

    def test_missing_store_opt_in_is_dev_opt_in_required(self):
        app = create_app(verify_token=TOKENS.get,
                         rows_for=lambda subject, owner: [manual_row()],
                         store_provider=lambda: (None, "dev_opt_in_required"),
                         membership_resolver=active_member)
        client = TestClient(app)
        response = client.post(
            f"/v1/results/{result_id_of(manual_row())}/decisions",
            headers=self.auth(),
            json={"question_number": "1", "action": "approve",
                  "expected_revision": 0, "idempotency_key": "k"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"],
                         "dev_opt_in_required")
        events = client.get(
            f"/v1/results/{result_id_of(manual_row())}/events",
            headers=self.auth())
        self.assertEqual(events.status_code, 503)


class OwnerScopeTests(ApiTestCase):

    def test_another_owner_sees_no_result_and_no_events(self):
        result_id = result_id_of(manual_row())
        decided = self.client.post(
            f"/v1/results/{result_id}/decisions", headers=self.auth(),
            json={"question_number": "1", "action": "approve",
                  "expected_revision": 0, "idempotency_key": "k",
                  "reason": ""})
        self.assertEqual(decided.status_code, 200, decided.text)

        foreign_decision = self.client.post(
            f"/v1/results/{result_id}/decisions", headers=self.auth("tok-bob"),
            json={"question_number": "1", "action": "approve",
                  "expected_revision": 0, "idempotency_key": "k"})
        self.assertEqual(foreign_decision.status_code, 404)
        self.assertEqual(foreign_decision.json()["error"]["code"],
                         "result_not_registered")

        foreign_events = self.client.get(
            f"/v1/results/{result_id}/events", headers=self.auth("tok-bob"))
        self.assertEqual(foreign_events.status_code, 404)
        self.assertEqual(foreign_events.json()["error"]["code"],
                         "result_not_registered")

        own_events = self.client.get(
            f"/v1/results/{result_id}/events", headers=self.auth())
        self.assertEqual(own_events.status_code, 200, own_events.text)
        payload = own_events.json()
        self.assertEqual(payload["result_id"], result_id)
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["result_revision"], 1)

    def test_unknown_result_is_never_distinguished_from_foreign(self):
        missing = self.client.get(
            "/v1/results/manual:doesnotexist:review-9/events",
            headers=self.auth())
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"]["code"],
                         "result_not_registered")

    def test_withdrawn_row_gates_history_reads_too(self):
        """Finding O-1 decision: reads obey the same visibility rule as
        writes - provider withdraws the row => no history, stream intact."""
        row = manual_row()
        self.rows[OWNER] = [row]
        result_id = result_id_of(row)
        committed = self.client.post(
            f"/v1/results/{result_id}/decisions", headers=self.auth(),
            json={"question_number": "1", "action": "approve",
                  "expected_revision": 0, "idempotency_key": "k1"})
        self.assertEqual(committed.status_code, 200, committed.text)
        visible = self.client.get(f"/v1/results/{result_id}/events",
                                  headers=self.auth())
        self.assertEqual(visible.status_code, 200)
        self.assertEqual(len(visible.json()["events"]), 1)

        self.rows[OWNER] = []  # provider withdraws the row
        denied_write = self.client.post(
            f"/v1/results/{result_id}/decisions", headers=self.auth(),
            json={"question_number": "2", "action": "approve",
                  "expected_revision": 1, "idempotency_key": "k2"})
        self.assertEqual(denied_write.status_code, 404)
        denied_read = self.client.get(f"/v1/results/{result_id}/events",
                                      headers=self.auth())
        self.assertEqual(denied_read.status_code, 404)
        self.assertEqual(denied_read.json()["error"]["code"],
                         "result_not_registered")
        # The audit stream itself is untouched by the denial.
        self.assertEqual(self.store.current_revision(owner=OWNER,
                                                     result_id=result_id), 1)


class RequestBodyLimitTests(ApiTestCase):

    def test_oversized_write_body_is_refused_before_parsing(self):
        response = self.client.post(
            f"/v1/results/{result_id_of(manual_row())}/decisions",
            headers=self.auth(),
            json={"question_number": "1", "action": "approve",
                  "expected_revision": 0, "idempotency_key": "k",
                  "reason": "x" * 40000})
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(response.json()["error"]["code"], "payload_invalid")
        self.assertIsNone(self.store.current_revision(
            owner=OWNER, result_id=result_id_of(manual_row())))

    def test_reads_are_not_affected_by_the_body_guard(self):
        self.assertEqual(self.client.get("/v1/healthz").status_code, 200)
        self.assertEqual(
            self.client.get(f"/v1/results/{result_id_of(manual_row())}/events",
                            headers=self.auth()).status_code, 404)


if __name__ == "__main__":
    unittest.main()
