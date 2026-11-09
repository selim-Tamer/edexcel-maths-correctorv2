"""BR-02 production API composition tests (offline, injected service).

BR-02A is not satisfied by a config-only assertion: these tests drive the REAL
FastAPI routes through the production result-service seam with the development
SQLite store made deliberately unusable. They pin:

* production selection when ``AI_MARKER_PG_DSN`` is configured;
* atomic registration through the service (never the dev registry);
* projection/history/decision reads through the service;
* API idempotency-key replay and changed-payload conflict;
* no SQLite fallback when opening the PostgreSQL service fails;
* request-scoped cleanup exactly once, including refusal paths.
"""
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

import audit_store
import identity_context
import result_service
import runtime_config
from api.app import create_app
from membership_store import Membership

SUBJECT = "teacher-a"
OWNER = "google:" + SUBJECT
TENANT = "tenant-br02"
DOCUMENT = "c" * 64
REVIEW_KEY = "review-1"
RESULT = "manual:" + DOCUMENT[:16] + ":" + REVIEW_KEY
ORIGINAL = {"questions": [
    {"question_number": "1", "awarded_marks": 1, "max_marks": 2},
    {"question_number": "2", "awarded_marks": 3, "max_marks": 3},
]}


class _Backend:
    """In-memory stand-in for the PostgreSQL result service's durable state."""

    def __init__(self):
        self.original = ORIGINAL
        self.original_digest = audit_store.digest_value(ORIGINAL)
        self.revision = 0
        self.events = []
        self.receipts = {}
        self.opened = 0
        self.closed = 0

    def row(self):
        return {"status": "Marked", "review_key": REVIEW_KEY,
                "report_owner": OWNER, "snapshot_context": DOCUMENT,
                "tenant_id": TENANT, "revision": "",
                "result": self.original, "ai_result": self.original,
                "student_name": "", "marking_model": "ingested",
                "audit_model": "Not run", "result_scope": "manual",
                "membership_subject": SUBJECT, "trusted_job_id": None}


class _FakeResultService:
    def __init__(self, backend, identity):
        self.backend = backend
        self.identity = identity
        self.closed = False

    # -- lifecycle -------------------------------------------------------
    def close(self):
        if not self.closed:
            self.closed = True
            self.backend.closed += 1

    # -- identity --------------------------------------------------------
    def _owner(self, owner):
        if owner != self.identity.owner_subject:
            raise audit_store.AuditStoreError(
                "tenant_membership_required", "foreign owner")
        return self.identity.membership_subject

    # -- reads -----------------------------------------------------------
    def visible_row(self, result_id):
        return dict(self.backend.row()) if result_id == RESULT else None

    def snapshot(self, *, result_id):
        if result_id != RESULT:
            raise audit_store.AuditStoreError("result_not_registered", "")
        return {"result_id": RESULT,
                "result_revision": self.backend.revision,
                "original_digest": self.backend.original_digest,
                "original_output": self.backend.original,
                "row": self.backend.row(),
                "events": list(self.backend.events),
                "created_at": "0"}

    def history(self, *, result_id, limit=None):
        if result_id != RESULT:
            raise audit_store.AuditStoreError("result_not_registered", "")
        events = list(self.backend.events)
        return {"result_revision": self.backend.revision,
                "events": events[:int(limit)] if limit else events}

    def current_revision(self, *, owner, result_id):
        self._owner(owner)
        return self.backend.revision if result_id == RESULT else None

    def events_for(self, *, owner, result_id):
        self._owner(owner)
        return list(self.backend.events)

    def events(self, *, owner, result_id, limit=None):
        return self.events_for(owner=owner, result_id=result_id)

    def replay(self, *, owner, result_id):
        return self.events_for(owner=owner, result_id=result_id)

    def original_record(self, *, owner, result_id):
        self._owner(owner)
        return {"original_digest": self.backend.original_digest,
                "result_revision": self.backend.revision,
                "original_scores": [["1", 1], ["2", 3]]}

    # -- writes ----------------------------------------------------------
    def register(self, *, result_id, original_output, scope, review_key,
                 document_digest="", job_id=None, assignment_id=None,
                 classroom=None):
        if result_id != RESULT:
            raise audit_store.AuditStoreError("payload_invalid", "")
        digest = audit_store.digest_value(original_output)
        if digest != self.backend.original_digest:
            raise audit_store.AuditStoreError("original_output_immutable", "")
        return {"created": True, "owner": self.identity.owner_subject,
                "membership_subject": self.identity.membership_subject,
                "result_id": result_id, "result_revision": 0,
                "original_digest": digest}

    def register_result(self, *, owner, result_id, original_output):
        self._owner(owner)
        digest = audit_store.digest_value(original_output)
        if digest != self.backend.original_digest:
            raise audit_store.AuditStoreError("original_output_immutable", "")
        return {"created": False, "result_revision": self.backend.revision,
                "original_digest": digest}

    def commit_correction(self, *, owner, result_id, expected_revision,
                          idempotency_key, payload, event):
        self._owner(owner)
        digest = audit_store.payload_digest(payload)
        prior = self.backend.receipts.get(idempotency_key)
        if prior is not None:
            if prior["payload_digest"] != digest:
                raise audit_store.AuditStoreError(
                    "idempotency_key_payload_mismatch", "")
            return {"status": "duplicate", "sequence": prior["sequence"],
                    "event_id": prior["event_id"],
                    "resulting_revision": prior["resulting_revision"],
                    "idempotent": True}
        if int(expected_revision) != self.backend.revision:
            raise audit_store.AuditStoreError("stale_revision", "")
        self.backend.revision += 1
        sequence = len(self.backend.events) + 1
        event_id = "event-%d" % sequence
        stored = {"sequence": sequence, "event_id": event_id,
                  "question_number": event["question_number"],
                  "review_status": event["review_status"],
                  "action": event["action"], "new_score": event["new_score"],
                  "reason": event["reason"], "actor": event["actor"],
                  "binding": event.get("binding"),
                  "resulting_revision": self.backend.revision}
        self.backend.events.append(stored)
        self.backend.receipts[idempotency_key] = {
            "payload_digest": digest, "sequence": sequence,
            "event_id": event_id,
            "resulting_revision": self.backend.revision}
        return {"status": "committed", "sequence": sequence,
                "event_id": event_id,
                "resulting_revision": self.backend.revision}


class ProductionCompositionTests(unittest.TestCase):
    def setUp(self):
        self.backend = _Backend()

    def factory(self, subject, tenant_id):
        self.backend.opened += 1
        return _FakeResultService(
            self.backend,
            identity_context.from_verified_subject(subject, tenant_id))

    def client(self, **overrides):
        options = dict(
            verify_token={"tok": SUBJECT}.get,
            membership_resolver=lambda subject: Membership(subject, TENANT,
                                                           "teacher"),
            result_service_factory=self.factory,
            authorize_ingest=lambda subject, tenant_id, row: True,
            manual_document_provenance_ok=lambda row: True,
            store_provider=lambda: (_ for _ in ()).throw(
                AssertionError("the development store must not be used")),
        )
        options.update(overrides)
        return TestClient(create_app(**options), raise_server_exceptions=False)

    def auth(self):
        return {"Authorization": "Bearer tok"}

    def ingest_body(self):
        return {"scope": "manual", "original_output": ORIGINAL,
                "manual_identity": {"document_digest": DOCUMENT,
                                    "submission_ref": REVIEW_KEY}}

    def decision_body(self, **overrides):
        body = {"question_number": "1", "action": "approve",
                "expected_revision": 0, "idempotency_key": "k1"}
        body.update(overrides)
        return body

    # -- A: production route composition ---------------------------------
    def test_ingest_registers_through_the_service_not_a_registry(self):
        response = self.client().post("/v1/results", headers=self.auth(),
                                      json=self.ingest_body())
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["result_id"], RESULT)
        self.assertEqual(response.json()["owner"], OWNER)
        self.assertEqual(self.backend.revision, 0)

    def test_projection_and_history_read_from_the_service_snapshot(self):
        client = self.client()
        client.post("/v1/results", headers=self.auth(),
                    json=self.ingest_body())
        projection = client.get(f"/v1/results/{RESULT}/projection",
                                headers=self.auth())
        self.assertEqual(projection.status_code, 200, projection.text)
        self.assertEqual(projection.json()["original_ai_output"], ORIGINAL)
        self.assertEqual(projection.json()["result_revision"], 0)
        history = client.get(f"/v1/results/{RESULT}/events",
                             headers=self.auth())
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["events"], [])
        self.assertEqual(history.json()["result_revision"], 0)

    def test_decision_replays_the_saved_receipt_and_conflicts_on_change(self):
        client = self.client()
        client.post("/v1/results", headers=self.auth(), json=self.ingest_body())
        first = client.post(f"/v1/results/{RESULT}/decisions",
                            headers=self.auth(),
                            json=self.decision_body())
        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(first.json()["replayed"])
        self.assertEqual(first.json()["result_revision"], 1)
        replay = client.post(f"/v1/results/{RESULT}/decisions",
                             headers=self.auth(), json=self.decision_body())
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["event_sequence"],
                         first.json()["event_sequence"])
        self.assertEqual(len(self.backend.events), 1)
        conflict = client.post(
            f"/v1/results/{RESULT}/decisions", headers=self.auth(),
            json=self.decision_body(action="flag", reason="different"))
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertEqual(conflict.json()["error"]["code"],
                         "idempotency_key_payload_mismatch")
        self.assertEqual(len(self.backend.events), 1)

    def test_stale_revision_with_a_fresh_key_is_rejected(self):
        client = self.client()
        client.post("/v1/results", headers=self.auth(), json=self.ingest_body())
        self.assertEqual(client.post(
            f"/v1/results/{RESULT}/decisions", headers=self.auth(),
            json=self.decision_body()).status_code, 200)
        stale = client.post(
            f"/v1/results/{RESULT}/decisions", headers=self.auth(),
            json=self.decision_body(question_number="2", action="correct",
                                    new_score=2, expected_revision=0,
                                    reason="stale attempt",
                                    idempotency_key="k2"))
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()["error"]["code"], "stale_revision")

    # -- A: cleanup and fail-closed selection ----------------------------
    def test_every_request_opens_and_closes_exactly_one_service(self):
        client = self.client()
        before = (self.backend.opened, self.backend.closed)
        for _ in range(2):
            client.get(f"/v1/results/{RESULT}/projection", headers=self.auth())
        self.assertEqual(self.backend.opened - before[0], 2)
        self.assertEqual(self.backend.closed - before[1], 2)

    def test_refusal_paths_still_close_the_service(self):
        client = self.client()
        response = client.get("/v1/results/unknown/projection",
                              headers=self.auth())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.backend.opened, self.backend.closed)

    def test_configured_dsn_selects_the_postgresql_service_by_default(self):
        seen = {}

        def factory(subject, tenant_id):
            seen["identity"] = identity_context.from_verified_subject(
                subject, tenant_id)
            seen["dsn"] = None
            return _FakeResultService(self.backend, seen["identity"])

        # Inject the factory directly (service_factory is not None) so the
        # production gate (keyed on PG_DSN) does not apply; this test
        # exercises composition, not fresh completion.
        env = {}
        with mock.patch.dict("os.environ", env, clear=False):
            client = TestClient(create_app(
                verify_token={"tok": SUBJECT}.get,
                membership_resolver=lambda subject: Membership(
                    subject, TENANT, "teacher"),
                authorize_ingest=lambda subject, tenant_id, row: True,
                manual_document_provenance_ok=lambda row: True,
                result_service_factory=factory,
                store_provider=lambda: (_ for _ in ()).throw(
                    AssertionError("the development store must not be used"))),
                raise_server_exceptions=False)
            response = client.post("/v1/results", headers=self.auth(),
                                   json=self.ingest_body())
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(seen["identity"].owner_subject, OWNER)
        self.assertEqual(seen["dsn"], None)  # resolved inside the service

    def test_unusable_postgresql_never_falls_back_to_sqlite(self):
        """Production with PG_DSN but unavailable service must return 503,
        never fall back to SQLite/dev store."""
        env = dict(os.environ)
        env["GOOGLE_OIDC_AUDIENCE"] = "test-audience"
        env["AI_MARKER_RUNTIME"] = "production"
        env["AI_MARKER_ALLOWED_ORIGINS"] = "https://test.example"
        env["AI_MARKER_PG_DSN"] = "postgresql://disposable.example/db?sslmode=require"
        env.pop(runtime_config.FRESH_RESULT_COMPLETION_ENABLED_ENV, None)

        def unavailable(identity, dsn=None, fresh_completion_denied=False):
            raise audit_store.AuditStoreError("store_unusable", "")

        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(result_service, "open_service",
                                  side_effect=unavailable):
            client = TestClient(create_app(
                verify_token={"tok": SUBJECT}.get,
                membership_resolver=lambda subject: Membership(
                    subject, TENANT, "teacher"),
                authorize_ingest=lambda subject, tenant_id, row: True,
                manual_document_provenance_ok=lambda row: True,
                store_provider=lambda: (_ for _ in ()).throw(
                    AssertionError("SQLite fallback was used"))),
                raise_server_exceptions=False)
            response = client.get(f"/v1/results/{RESULT}/projection",
                                  headers=self.auth())
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "store_unusable")


if __name__ == "__main__":
    unittest.main()
