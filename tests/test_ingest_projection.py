"""Ingest + projection contract tests (dev stores, offline, no network).

Covers the POST /v1/results + GET /v1/results/{id}/projection slice:
server-minted durable identities, immutable originals (audit anchor first,
visibility row second), the owner-scoped visibility rule applied to reads
(O-1 consistency), corrections served ALONGSIDE untouched originals,
idempotent re-ingest vs immutable-output refusal, cross-owner invisibility,
and both dev-store opt-ins failing closed (registry unconfigured => ingest
denied AND previously ingested streams become invisible; audit store absent
=> dev_opt_in_required).
"""
import json
import os
import tempfile
import unittest

import audit_store
import ingest_registry
from api.app import create_app
from membership_store import Membership
from fastapi.testclient import TestClient

SUBJECT = "teacher-a-subject"
OTHER_SUBJECT = "teacher-b-subject"
OWNER = "google:" + SUBJECT
OTHER_OWNER = "google:" + OTHER_SUBJECT
TOKENS = {"tok-a": SUBJECT, "tok-b": OTHER_SUBJECT}
DIGEST = "d" * 64
REFERENCE = "server-issued-ref-001"
MANUAL_RESULT = "manual:" + DIGEST[:16] + ":" + REFERENCE


def original_output():
    return {"questions": [
        {"question_number": "1", "awarded_marks": 2, "max_marks": 3,
         "student_answer": "SECRET-ANSWER-1"},
        {"question_number": "2", "awarded_marks": 1, "max_marks": 1,
         "student_answer": "SECRET-ANSWER-2"},
    ]}


def manual_ingest_body(**overrides):
    body = {"scope": "manual",
            "manual_identity": {"submission_ref": REFERENCE,
                                "document_digest": DIGEST},
            "original_output": original_output()}
    body.update(overrides)
    return body


class IngestProjectionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self._env = {
            "AI_MARKER_AUDIT_DEV_STORE":
                os.path.join(self.tmp.name, "audit.sqlite3"),
            "AI_MARKER_AUDIT_DEV_ONLY": "1",
            "AI_MARKER_INGEST_DB":
                os.path.join(self.tmp.name, "ingest.sqlite3"),
            "AI_MARKER_INGEST_DEV_ONLY": "1",
        }
        for name, value in self._env.items():
            os.environ[name] = value
            self.addCleanup(os.environ.pop, name, None)
        self.store = audit_store.open_store(
            self._env["AI_MARKER_AUDIT_DEV_STORE"], dev_only_ack=True)
        self.addCleanup(self.store.close)
        self.app = create_app(
            verify_token=TOKENS.get,
            authorize_ingest=lambda subject, tenant_id, row: True,
            membership_resolver=lambda subject: Membership(subject, "tenant-beta", "teacher"),
            store_provider=lambda: (self.store, None),
            manual_document_provenance_ok=lambda row: True)
        self.client = TestClient(self.app)

    def auth(self, token="tok-a"):
        return {"Authorization": f"Bearer {token}"}

    def ingest(self, body=None, token="tok-a"):
        return self.client.post("/v1/results", headers=self.auth(token),
                                json=body if body is not None
                                else manual_ingest_body())

    def projection(self, result_id, token="tok-a"):
        return self.client.get(f"/v1/results/{result_id}/projection",
                               headers=self.auth(token))


class IngestTests(IngestProjectionBase):
    def test_manual_ingest_mints_the_durable_identity_and_registers(self):
        response = self.ingest()
        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        self.assertEqual(payload["result_id"], MANUAL_RESULT)
        self.assertEqual(payload["owner"], OWNER)
        self.assertEqual(payload["result_revision"], 0)
        self.assertTrue(payload["audit_reference"])  # immutable original digest
        self.assertIsNone(payload["supersedes"])
        # The stream is registered and immediately readable through the same
        # owner-scoped visibility rule (registry supplies the row).
        self.assertEqual(self.store.current_revision(
            owner=OWNER, result_id=MANUAL_RESULT), 0)
        events = self.client.get(
            f"/v1/results/{MANUAL_RESULT}/events", headers=self.auth())
        self.assertEqual(events.status_code, 200, events.text)
        self.assertEqual(events.json()["events"], [])

    def test_reingest_is_idempotent_but_a_different_original_is_immutable(self):
        self.assertEqual(self.ingest().status_code, 201)
        again = self.ingest()
        self.assertEqual(again.status_code, 201, again.text)
        self.assertEqual(again.json()["result_revision"], 0)
        changed = manual_ingest_body()
        changed["original_output"] = {"questions": [
            {"question_number": "1", "awarded_marks": 0, "max_marks": 3}]}
        conflict = self.ingest(changed)
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["error"]["code"],
                         "original_output_immutable")

    def test_ingest_validation_matrix_uses_contract_codes(self):
        cases = [
            (manual_ingest_body(scope="somewhere"), "payload_invalid"),
            (manual_ingest_body(manual_identity=None), "payload_invalid"),
            (manual_ingest_body(manual_identity={
                "submission_ref": REFERENCE, "document_digest": "z" * 64}),
             "payload_invalid"),
            (manual_ingest_body(manual_identity={
                "submission_ref": "evil/ref", "document_digest": DIGEST}),
             "payload_invalid"),
            (manual_ingest_body(classroom_identity=None, scope="classroom"),
             "payload_invalid"),
            (manual_ingest_body(original_output={"not_questions": []}),
             "original_output_invalid"),
            (manual_ingest_body(original_output="not-a-mapping"),
             "payload_invalid"),
        ]
        for body, code in cases:
            with self.subTest(expected=code, body=body):
                response = self.ingest(body)
                self.assertIn(response.status_code, (400, 409))
                self.assertEqual(response.json()["error"]["code"], code)

    def test_classroom_ingest_mints_identity_and_decisions_fail_closed(self):
        response = self.ingest(manual_ingest_body(
            scope="classroom",
            manual_identity=None,
            classroom_identity={"course_id": "c-1",
                                "assignment_id": "a-1",
                                "submission_id": "s-1"}))
        self.assertEqual(response.status_code, 201, response.text)
        result_id = "classroom:c-1:a-1:s-1:unversioned"
        self.assertEqual(response.json()["result_id"], result_id)
        # Projection works (originals from the row, revisions from the store).
        project = self.projection(result_id)
        self.assertEqual(project.status_code, 200, project.text)
        self.assertEqual(project.json()["result_revision"], 0)
        # Decisions on a classroom stream stay fail-closed until live
        # Classroom re-verification is wired: default refuses.
        decision = self.client.post(
            f"/v1/results/{result_id}/decisions", headers=self.auth(),
            json={"question_number": "1", "action": "approve",
                  "expected_revision": 0, "idempotency_key": "k"})
        self.assertEqual(decision.status_code, 503, decision.text)
        self.assertEqual(decision.json()["error"]["code"],
                         "classroom_verification_unavailable")


class ProjectionTests(IngestProjectionBase):
    def test_projection_shows_originals_then_corrections_alongside(self):
        self.assertEqual(self.ingest().status_code, 201)
        # Before any decision: everything unreviewed, originals equal effective.
        project = self.projection(MANUAL_RESULT)
        self.assertEqual(project.status_code, 200, project.text)
        payload = project.json()
        self.assertEqual(payload["result_revision"], 0)
        self.assertEqual([q["review_status"] for q in payload["questions"]],
                         ["unreviewed", "unreviewed"])
        self.assertEqual(payload["questions"][0]["effective_awarded_marks"], 2)
        self.assertEqual(payload["totals"],
                         {"earned": 3.0, "available": 4.0, "percentage": 75.0})
        # The store itself never carries student text; the row does.
        self.assertNotIn("SECRET-ANSWER-1", json.dumps(
            self.store.original_record(owner=OWNER, result_id=MANUAL_RESULT)))
        self.assertIn("SECRET-ANSWER-1",
                      json.dumps(payload["original_ai_output"]))

        # One correction through the real decision endpoint.
        decision = self.client.post(
            f"/v1/results/{MANUAL_RESULT}/decisions", headers=self.auth(),
            json={"question_number": "1", "action": "correct",
                  "reason": "misread", "new_score": 3,
                  "expected_revision": 0, "idempotency_key": "k1"})
        self.assertEqual(decision.status_code, 200, decision.text)

        after = self.projection(MANUAL_RESULT).json()
        first, second = after["questions"]
        self.assertTrue(first["corrected"])
        self.assertEqual(first["original_awarded_marks"], 2)
        self.assertEqual(first["effective_awarded_marks"], 3)
        self.assertEqual(first["review_status"], "corrected")
        self.assertEqual(first["last_reason"], "misread")
        # Nothing about question 2 changed; original AI output is untouched.
        self.assertFalse(second["corrected"])
        self.assertEqual(second["effective_awarded_marks"], 1)
        self.assertEqual(
            after["original_ai_output"]["questions"][0]["awarded_marks"], 2)
        self.assertEqual(after["totals"]["earned"], 4.0)
        self.assertEqual(after["result_revision"], 1)
        # The AI original in the durable store is still the pre-correction one.
        original = self.store.original_record(
            owner=OWNER, result_id=MANUAL_RESULT)
        self.assertEqual(original["original_scores"], [["1", 2], ["2", 1]])

    def test_projection_and_history_are_owner_scoped(self):
        self.assertEqual(self.ingest().status_code, 201)
        foreign_projection = self.projection(MANUAL_RESULT, token="tok-b")
        self.assertEqual(foreign_projection.status_code, 404)
        self.assertEqual(foreign_projection.json()["error"]["code"],
                         "result_not_registered")
        foreign_events = self.client.get(
            f"/v1/results/{MANUAL_RESULT}/events", headers=self.auth("tok-b"))
        self.assertEqual(foreign_events.status_code, 404)
        # Unknown streams are indistinguishable from foreign ones.
        self.assertEqual(self.projection("manual:" + "f" * 16 + ":nope"
                                         ).status_code, 404)


class FailClosedDefaultsTests(IngestProjectionBase):
    def test_unconfigured_registry_denies_ingest_and_hides_streams(self):
        self.assertEqual(self.ingest().status_code, 201)
        os.environ.pop("AI_MARKER_INGEST_DB", None)
        denied = self.ingest()
        self.assertEqual(denied.status_code, 503, denied.text)
        self.assertEqual(denied.json()["error"]["code"], "store_unusable")
        # Previously ingested streams become invisible too (rows collapse to
        # none): the durable store keeps the registration, nothing leaks.
        self.assertEqual(self.projection(MANUAL_RESULT).status_code, 404)
        self.assertEqual(self.store.current_revision(
            owner=OWNER, result_id=MANUAL_RESULT), 0)

    def test_missing_audit_store_opt_in_blocks_ingest_first(self):
        app = create_app(
            verify_token=TOKENS.get,
            authorize_ingest=lambda subject, tenant_id, row: True,
            membership_resolver=lambda subject: Membership(subject, "tenant-beta", "teacher"),
            store_provider=lambda: (None, "dev_opt_in_required"),
            manual_document_provenance_ok=lambda row: True)
        response = TestClient(app).post(
            "/v1/results", headers=self.auth(), json=manual_ingest_body())
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"],
                         "dev_opt_in_required")
        # The registry was never touched: no row for that identity exists.
        connection = ingest_registry.require_connection()
        self.addCleanup(connection.close)
        self.assertEqual(
            ingest_registry.find_rows(connection, OWNER, "tenant-beta"), [])


class IngestAuthorizationTests(IngestProjectionBase):
    """The provenance gate is opt-IN: no injected authorizer => no ingest."""

    def _app(self, authorize_ingest):
        return create_app(
            verify_token=TOKENS.get,
            authorize_ingest=authorize_ingest,
            membership_resolver=lambda subject: Membership(
                subject, "tenant-beta", "teacher"),
            store_provider=lambda: (self.store, None),
            manual_document_provenance_ok=lambda row: True)

    def test_default_authorizer_fails_closed_before_any_write(self):
        client = TestClient(self._app(None))
        response = client.post("/v1/results", headers=self.auth(),
                               json=manual_ingest_body())
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"],
                         "manual_document_provenance_required")
        self.assertEqual(response.json()["error"].get("detail"),
                         "ingest_authorization_required")
        # Neither store nor registry was touched.
        self.assertIsNone(self.store.current_revision(
            owner=OWNER, result_id=MANUAL_RESULT))
        connection = ingest_registry.require_connection()
        self.addCleanup(connection.close)
        self.assertEqual(
            ingest_registry.find_rows(connection, OWNER, "tenant-beta"), [])

    def test_authorizer_must_return_exactly_true_and_must_not_raise(self):
        for answer in (1, "yes", None, False):
            with self.subTest(answer=answer):
                client = TestClient(self._app(lambda s, t, r: answer))
                response = client.post("/v1/results", headers=self.auth(),
                                       json=manual_ingest_body())
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["error"]["code"],
                                 "manual_document_provenance_required")
                self.assertIsNone(self.store.current_revision(
                    owner=OWNER, result_id=MANUAL_RESULT))

        def explode(subject, tenant_id, row):
            raise RuntimeError("provenance service down")

        response = TestClient(self._app(explode)).post(
            "/v1/results", headers=self.auth(), json=manual_ingest_body())
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"],
                         "manual_document_provenance_required")
        self.assertIsNone(self.store.current_revision(
            owner=OWNER, result_id=MANUAL_RESULT))


class TenantBindingTests(IngestProjectionBase):
    """Rows, reads and re-ingests stay bound to the ingesting tenant."""

    def _app_for_tenant(self, tenant_id):
        return TestClient(create_app(
            verify_token=TOKENS.get,
            authorize_ingest=lambda subject, tenant, row: True,
            membership_resolver=lambda subject: Membership(
                subject, tenant_id, "teacher"),
            store_provider=lambda: (self.store, None),
            manual_document_provenance_ok=lambda row: True))

    def test_rows_are_invisible_to_another_tenant_of_the_same_owner(self):
        self.assertEqual(self.ingest().status_code, 201)
        other = self._app_for_tenant("tenant-gamma")
        hidden = other.get(f"/v1/results/{MANUAL_RESULT}/projection",
                           headers=self.auth())
        self.assertEqual(hidden.status_code, 404, hidden.text)
        self.assertEqual(hidden.json()["error"]["code"],
                         "result_not_registered")
        self.assertEqual(other.get(f"/v1/results/{MANUAL_RESULT}/events",
                                   headers=self.auth()).status_code, 404)
        # The durable registration is untouched by the cross-tenant denial.
        self.assertEqual(self.store.current_revision(
            owner=OWNER, result_id=MANUAL_RESULT), 0)

    def test_reingest_cannot_rebind_the_stream_to_another_tenant(self):
        self.assertEqual(self.ingest().status_code, 201)
        rebind = self._app_for_tenant("tenant-gamma").post(
            "/v1/results", headers=self.auth(), json=manual_ingest_body())
        self.assertEqual(rebind.status_code, 404, rebind.text)
        self.assertEqual(rebind.json()["error"]["code"],
                         "result_not_registered")
        connection = ingest_registry.require_connection()
        self.addCleanup(connection.close)
        (tenant,) = connection.execute(
            "SELECT tenant_id FROM ingest_rows WHERE owner_subject = ?"
            " AND result_id = ?", (OWNER, MANUAL_RESULT)).fetchone()
        self.assertEqual(tenant, "tenant-beta")

    def test_projection_refuses_a_row_that_no_longer_matches_the_anchor(self):
        self.assertEqual(self.ingest().status_code, 201)
        # Simulate a tampered/rewritten dev row: the immutable digest the
        # store anchors no longer describes the row's original output.
        connection = ingest_registry.require_connection()
        self.addCleanup(connection.close)
        (payload,) = connection.execute(
            "SELECT row_json FROM ingest_rows WHERE owner_subject = ?"
            " AND result_id = ?", (OWNER, MANUAL_RESULT)).fetchone()
        tampered = json.loads(payload)
        tampered["ai_result"]["questions"][0]["awarded_marks"] = 99
        connection.execute(
            "UPDATE ingest_rows SET row_json = ? WHERE owner_subject = ?"
            " AND result_id = ?",
            (json.dumps(tampered, sort_keys=True,
                        separators=(",", ":"), ensure_ascii=False,
                        allow_nan=False), OWNER, MANUAL_RESULT))
        connection.commit()
        refused = self.projection(MANUAL_RESULT)
        self.assertEqual(refused.status_code, 500, refused.text)
        self.assertEqual(refused.json()["error"]["code"],
                         "audit_record_invalid")


if __name__ == "__main__":
    unittest.main()
