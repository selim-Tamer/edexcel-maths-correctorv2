"""Durable marking-job tests (dev stores, offline, synthetic engine).

Covers POST /v1/jobs, GET /v1/jobs/{id} and the lease-guarded
POST /v1/jobs/{id}/run: server-minted job identity, idempotency-key replay
and payload-mismatch refusal, engine execution through the SAME registration
path as POST /v1/results (so the job-backed provenance gate authorizes it),
bounded retries with existing-vocabulary error codes, live-lease protection
with expired-lease crash recovery, cross-owner/tenant indistinguishable 404s,
fail-closed unconfigured stores, and no-engine refusal.
"""
import hashlib
import os
import tempfile
import time
import unittest

import document_store
import ingest_registry
import job_store
import pdf_cases
from api.app import create_app
from membership_store import Membership
from fastapi.testclient import TestClient

SUBJECT = "teacher-a-subject"
OWNER = "google:" + SUBJECT
TENANT = "tenant-beta"
TOKENS = {"tok-a": SUBJECT, "tok-b": "teacher-b-subject"}


def engine_output(data=None):
    return {"questions": [
        {"question_number": "1", "awarded_marks": 2, "max_marks": 3,
         "student_answer": "SYNTHETIC-ANSWER"},
        {"question_number": "2", "awarded_marks": 1, "max_marks": 1,
         "student_answer": "SYNTHETIC-ANSWER-2"},
    ]}


class JobApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self._env = {
            "AI_MARKER_AUDIT_DEV_STORE":
                os.path.join(self.tmp.name, "audit.sqlite3"),
            "AI_MARKER_AUDIT_DEV_ONLY": "1",
            ingest_registry.PATH_ENV: os.path.join(self.tmp.name,
                                                    "ingest.sqlite3"),
            ingest_registry.ACK_ENV: "1",
            document_store.PATH_ENV: os.path.join(self.tmp.name,
                                                   "documents.sqlite3"),
            document_store.ACK_ENV: "1",
            job_store.PATH_ENV: os.path.join(self.tmp.name, "jobs.sqlite3"),
            job_store.ACK_ENV: "1",
        }
        for name, value in self._env.items():
            os.environ[name] = value
            self.addCleanup(os.environ.pop, name, None)
        self.pdf = pdf_cases.make_pdf()
        self.engine = engine_output
        self.client = self._client(TENANT)
        self.digest = hashlib.sha256(self.pdf).hexdigest()
        uploaded = self.client.post(
            "/v1/documents", headers=self._headers(),
            params={"filename": "work.pdf"}, content=self.pdf)
        assert uploaded.status_code == 201, uploaded.text

    def _client(self, tenant_id, engine_marker=...):
        engine = self.engine if engine_marker is ... else engine_marker
        return TestClient(create_app(
            verify_token=TOKENS.get,
            membership_resolver=lambda subject: Membership(
                subject, tenant_id, "teacher"),
            manual_document_provenance_ok=lambda row: True,
            engine=engine))

    def _headers(self, token="tok-a"):
        return {"Authorization": f"Bearer {token}"}

    def _create(self, key="job-key-0001", digest=None, token="tok-a",
                client=None):
        return (client or self.client).post(
            "/v1/jobs", headers=self._headers(token),
            json={"idempotency_key": key,
                  "document_digest": digest or self.digest})

    def _run(self, job_id, token="tok-a", client=None):
        return (client or self.client).post(
            f"/v1/jobs/{job_id}/run", headers=self._headers(token))

    def test_create_mints_the_job_and_replays_idempotently(self):
        first = self._create()
        self.assertEqual(first.status_code, 201, first.text)
        payload = first.json()
        self.assertTrue(payload["job_id"].startswith("job:"))
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["attempts"], 0)
        self.assertTrue(payload["created"])
        self.assertNotIn("idempotency_key", payload)
        replay = self._create()
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()["job_id"], payload["job_id"])
        self.assertFalse(replay.json()["created"])
        # The same key cannot be repointed at a different document: upload a
        # second real document and reuse the key against IT.
        other_pdf = pdf_cases.make_pdf(pages=2)
        other_upload = self.client.post(
            "/v1/documents", headers=self._headers(),
            params={"filename": "other.pdf"}, content=other_pdf)
        self.assertEqual(other_upload.status_code, 201, other_upload.text)
        mismatch = self._create(digest=other_upload.json()["document_digest"])
        self.assertEqual(mismatch.status_code, 409, mismatch.text)
        self.assertEqual(mismatch.json()["error"]["code"],
                         "idempotency_key_payload_mismatch")

    def test_create_requires_an_existing_owner_document(self):
        unknown = self._create(digest="f" * 64)
        foreign = self._create(digest=self.digest, token="tok-b")
        for response in (unknown, foreign):
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(response.json(), unknown.json())
        self.assertEqual(unknown.json()["error"]["code"],
                         "result_not_registered")

    def test_run_executes_the_engine_and_registers_a_readable_result(self):
        job_id = self._create().json()["job_id"]
        ran = self._run(job_id)
        self.assertEqual(ran.status_code, 200, ran.text)
        state = ran.json()
        self.assertEqual(state["status"], "succeeded", ran.text)
        self.assertEqual(state["attempts"], 1)
        self.assertTrue(state["result_id"].startswith("manual:"), state)
        # The result is immediately readable through the contract projection.
        projection = self.client.get(f"/v1/results/{state['result_id']}/projection",
                                     headers=self._headers())
        self.assertEqual(projection.status_code, 200, projection.text)
        body = projection.json()
        self.assertEqual([q["effective_awarded_marks"] for q in body["questions"]],
                         [2, 1])
        self.assertEqual(body["totals"],
                         {"earned": 3.0, "available": 4.0, "percentage": 75.0})
        # Status endpoint reports the same durable outcome.
        status = self.client.get(f"/v1/jobs/{job_id}", headers=self._headers())
        self.assertEqual(status.json()["status"], "succeeded")
        # Re-running a succeeded job is a no-op: it never re-executes the
        # engine (proved with a counting engine on a fresh client).
        calls = []

        def counting(data):
            calls.append(data)
            return engine_output()

        hostile = self._client(TENANT, engine_marker=counting)
        self.assertEqual(
            self._run(job_id, client=hostile).json()["status"], "succeeded")
        self.assertEqual(calls, [], "a finished job must not re-execute")

    def test_engine_failures_retry_bounded_then_fail(self):
        def broken(data):
            raise RuntimeError("provider exploded")
        self.engine = broken
        client = self._client(TENANT)
        job_id = self._create(client=client).json()["job_id"]
        outcomes = [self._run(job_id, client=client).json() for _ in range(3)]
        self.assertEqual([o["status"] for o in outcomes],
                         ["queued", "queued", "failed"])
        self.assertEqual([o["attempts"] for o in outcomes], [1, 2, 3])
        self.assertEqual(outcomes[-1]["error_code"], "service_unavailable")
        # Exhausted: no fourth execution attempt is ever made.
        self.engine = engine_output
        self.assertEqual(self._run(job_id, client=client).json()["status"],
                         "failed")

    def test_live_lease_is_never_stolen_and_expired_lease_is_reclaimed(self):
        job_id = self._create().json()["job_id"]
        connection = job_store.open_store(self._env[job_store.PATH_ENV])
        self.addCleanup(connection.close)
        connection.execute(
            "UPDATE marking_jobs SET status = 'running', attempts = 1,"
            " lease_until = ? WHERE job_id = ?",
            (int(time.time()) + 3600, job_id))
        connection.commit()
        blocked = self._run(job_id)
        self.assertEqual(blocked.status_code, 200, blocked.text)
        self.assertEqual(blocked.json()["status"], "running")
        self.assertEqual(blocked.json()["attempts"], 1)  # not re-executed
        # Crash simulation: lease expired -> the job is reclaimable.
        connection.execute(
            "UPDATE marking_jobs SET lease_until = ? WHERE job_id = ?",
            (int(time.time()) - 1, job_id))
        connection.commit()
        recovered = self._run(job_id)
        self.assertEqual(recovered.json()["status"], "succeeded")
        self.assertEqual(recovered.json()["attempts"], 2)

    def test_job_backed_provenance_gate_is_the_default(self):
        # authorize_ingest not injected: the job store authorizes ingest.
        digest = self.digest
        denied = self.client.post(
            "/v1/results", headers=self._headers(),
            json={"scope": "manual",
                  "manual_identity": {"submission_ref": "ref-1",
                                      "document_digest": digest},
                  "original_output": engine_output()})
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()["error"]["code"],
                         "manual_document_provenance_required")
        self.assertEqual(denied.json()["error"].get("detail"),
                         "ingest_authorization_required")
        # A succeeded job for THIS owner+tenant+document authorizes it.
        job_id = self._create().json()["job_id"]
        self.assertEqual(self._run(job_id).json()["status"], "succeeded")
        allowed = self.client.post(
            "/v1/results", headers=self._headers(),
            json={"scope": "manual",
                  "manual_identity": {"submission_ref": "ref-1",
                                      "document_digest": digest},
                  "original_output": engine_output()})
        self.assertEqual(allowed.status_code, 201, allowed.text)

    def test_unconfigured_stores_fail_closed(self):
        job_id = None
        os.environ.pop(job_store.PATH_ENV, None)
        created = self._create()
        self.assertEqual(created.status_code, 503, created.text)
        self.assertEqual(created.json()["error"]["code"],
                         "dev_opt_in_required")
        # A job created before the store became unavailable still 503s.
        os.environ[job_store.PATH_ENV] = self._env[job_store.PATH_ENV]
        job_id = self._create().json()["job_id"]
        os.environ.pop(job_store.PATH_ENV, None)
        for response in (self.client.get(f"/v1/jobs/{job_id}",
                                         headers=self._headers()),
                         self._run(job_id)):
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.json()["error"]["code"],
                             "dev_opt_in_required")
        # The ingest gate also fails closed without the job store.
        denied = self.client.post(
            "/v1/results", headers=self._headers(),
            json={"scope": "manual",
                  "manual_identity": {"submission_ref": "ref-2",
                                      "document_digest": self.digest},
                  "original_output": engine_output()})
        self.assertEqual(denied.status_code, 403, denied.text)

    def test_jobs_are_owner_and_tenant_scoped(self):
        job_id = self._create().json()["job_id"]
        other_tenant = self._client("tenant-gamma")
        foreign_read = other_tenant.get(f"/v1/jobs/{job_id}",
                                        headers=self._headers())
        foreign_run = self._run(job_id, client=other_tenant)
        other_owner = self.client.get(f"/v1/jobs/{job_id}",
                                      headers=self._headers("tok-b"))
        unknown = self.client.get("/v1/jobs/" + "job:" + "0" * 24,
                                  headers=self._headers())
        for response in (foreign_read, foreign_run, other_owner, unknown):
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(response.json(), unknown.json())
        self.assertEqual(unknown.json()["error"]["code"],
                         "result_not_registered")
        # The foreign tenant cannot point a job at this tenant's document.
        blocked = other_tenant.post(
            "/v1/jobs", headers=self._headers(),
            json={"idempotency_key": "job-key-0002",
                  "document_digest": self.digest})
        self.assertEqual(blocked.status_code, 404, blocked.text)

    def test_no_engine_is_refused_not_faked(self):
        client = self._client(TENANT, engine_marker=None)
        job_id = self._create(client=client).json()["job_id"]
        refused = self._run(job_id, client=client)
        self.assertEqual(refused.status_code, 503, refused.text)
        self.assertEqual(refused.json()["error"]["code"],
                         "model_unavailable")
        # The job is untouched: still queued, zero attempts.
        state = client.get(f"/v1/jobs/{job_id}",
                           headers=self._headers()).json()
        self.assertEqual((state["status"], state["attempts"]),
                         ("queued", 0))


if __name__ == "__main__":
    unittest.main()
