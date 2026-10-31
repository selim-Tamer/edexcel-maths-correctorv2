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
import json
import os
import tempfile
import time
import unittest

import assignment_store
import audit_store
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
            assignment_store.PATH_ENV: os.path.join(self.tmp.name,
                                                    "assignments.sqlite3"),
            assignment_store.ACK_ENV: "1",
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

    def _create_assignment(self, key="assign-key-0001",
                           title="Year 10 Paper 1", token="tok-a",
                           client=None):
        headers = self._headers(token)
        headers["Idempotency-Key"] = key
        return (client or self.client).post(
            "/v1/assignments", headers=headers, json={"title": title})

    def _run(self, job_id, token="tok-a", client=None):
        return (client or self.client).post(
            f"/v1/jobs/{job_id}/run", headers=self._headers(token))

    def test_job_links_to_an_owned_assignment_end_to_end(self):
        created = self._create_assignment()
        self.assertEqual(201, created.status_code, created.text)
        assignment_id = created.json()["assignment_id"]

        linked = self.client.post(
            "/v1/jobs", headers=self._headers(),
            json={"idempotency_key": "job-link-key-0001",
                  "document_digest": self.digest,
                  "assignment_id": assignment_id})
        self.assertEqual(201, linked.status_code, linked.text)
        self.assertEqual(assignment_id, linked.json()["assignment_id"])

        status = self.client.get(f"/v1/jobs/{linked.json()['job_id']}",
                                 headers=self._headers())
        self.assertEqual(200, status.status_code, status.text)
        self.assertEqual(assignment_id, status.json()["assignment_id"])

    def test_unlinked_jobs_keep_the_agreed_v1_2_payload_shape(self):
        """Absent-means-omit: an unlinked job must not grow a null field.

        The agreed v1.2 response shape did not contain assignment_id. Emitting
        null on every job would change bytes for a client that never opted in,
        so the field appears only when a link exists.
        """
        payload = self._create().json()
        self.assertNotIn("assignment_id", payload)
        status = self.client.get(f"/v1/jobs/{payload['job_id']}",
                                 headers=self._headers())
        self.assertEqual(200, status.status_code, status.text)
        self.assertNotIn("assignment_id", status.json())

    def test_malformed_assignment_id_is_payload_invalid(self):
        for bad in ("asg_nothex", "asg_" + "A" * 24, "assignment-1", " "):
            with self.subTest(value=bad):
                response = self.client.post(
                    "/v1/jobs", headers=self._headers(),
                    json={"idempotency_key": "job-bad-asg-0001",
                          "document_digest": self.digest,
                          "assignment_id": bad})
                self.assertEqual(400, response.status_code, response.text)
                self.assertEqual("payload_invalid",
                                 response.json()["error"]["code"])

    def test_another_owners_assignment_is_indistinguishable_not_found(self):
        created = self._create_assignment()
        assignment_id = created.json()["assignment_id"]
        other = self._client(TENANT, engine_marker=None)
        response = other.post(
            "/v1/jobs", headers=self._headers("tok-b"),
            json={"idempotency_key": "job-other-owner-0001",
                  "document_digest": self.digest,
                  "assignment_id": assignment_id})
        self.assertEqual(404, response.status_code, response.text)
        self.assertEqual("assignment_not_found",
                         response.json()["error"]["code"])

    def test_replay_with_a_different_assignment_is_a_conflict(self):
        first = self._create_assignment(key="assign-key-a", title="A")
        second = self._create_assignment(key="assign-key-b", title="B")
        body = {"idempotency_key": "job-retarget-0001",
                "document_digest": self.digest,
                "assignment_id": first.json()["assignment_id"]}
        created = self.client.post("/v1/jobs", headers=self._headers(),
                                   json=body)
        self.assertEqual(201, created.status_code, created.text)
        retargeted = dict(body,
                          assignment_id=second.json()["assignment_id"])
        replay = self.client.post("/v1/jobs", headers=self._headers(),
                                  json=retargeted)
        self.assertEqual(409, replay.status_code, replay.text)
        self.assertEqual("idempotency_key_payload_mismatch",
                         replay.json()["error"]["code"])

    # -- GET /v1/assignments/{assignment_id}/jobs (agreed v1.2.1 item 3) ---
    def test_assignment_jobs_lists_only_linked_jobs_byte_identically(self):
        """The recovery route returns linked jobs only, payload-for-payload.

        Contract item 3: each item is exactly what ``GET /v1/jobs/{job_id}``
        returns for that job (``assignment_id`` present because the row is
        linked by construction), and an unlinked job never appears.
        """
        created = self._create_assignment()
        self.assertEqual(201, created.status_code, created.text)
        assignment_id = created.json()["assignment_id"]
        linked_ids = []
        for index in (1, 2):
            response = self.client.post(
                "/v1/jobs", headers=self._headers(),
                json={"idempotency_key": f"job-list-link-{index:04d}",
                      "document_digest": self.digest,
                      "assignment_id": assignment_id})
            self.assertEqual(201, response.status_code, response.text)
            linked_ids.append(response.json()["job_id"])
        unlinked = self._create(key="job-list-unlinked-0001")
        self.assertEqual(201, unlinked.status_code, unlinked.text)

        page = self.client.get(
            f"/v1/assignments/{assignment_id}/jobs",
            headers=self._headers())
        self.assertEqual(200, page.status_code, page.text)
        body = page.json()
        self.assertIsNone(body["next_cursor"],
                          "a not-full page must not emit a cursor")
        listed = body["jobs"]
        self.assertEqual(set(linked_ids),
                         {job["job_id"] for job in listed})
        for job in listed:
            single = self.client.get(f"/v1/jobs/{job['job_id']}",
                                     headers=self._headers())
            self.assertEqual(200, single.status_code, single.text)
            self.assertEqual(single.json(), job,
                             "payload must be identical to GET /v1/jobs/{id}")
            self.assertEqual(assignment_id, job["assignment_id"])

    def test_assignment_jobs_pages_by_keyset_without_duplicates_or_gaps(self):
        created = self._create_assignment(key="assign-key-page")
        assignment_id = created.json()["assignment_id"]
        expected = set()
        for index in range(3):
            response = self.client.post(
                "/v1/jobs", headers=self._headers(),
                json={"idempotency_key": f"job-page-{index:04d}",
                      "document_digest": self.digest,
                      "assignment_id": assignment_id})
            self.assertEqual(201, response.status_code, response.text)
            expected.add(response.json()["job_id"])
        seen, cursor = [], None
        for _ in range(10):
            params = {"limit": 1}
            if cursor:
                params["cursor"] = cursor
            page = self.client.get(
                f"/v1/assignments/{assignment_id}/jobs",
                headers=self._headers(), params=params)
            self.assertEqual(200, page.status_code, page.text)
            body = page.json()
            seen.extend(job["job_id"] for job in body["jobs"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        else:
            self.fail("the cursor did not terminate")
        self.assertEqual(expected, set(seen), "pages skipped or invented rows")
        self.assertEqual(len(seen), len(set(seen)), "duplicate rows across pages")

    def test_assignment_jobs_malformed_id_and_cursor_are_payload_invalid(self):
        for bad in ("asg_nothex", "asg_" + "A" * 24, "assignment-1", " "):
            with self.subTest(assignment_id=bad):
                response = self.client.get(
                    f"/v1/assignments/{bad}/jobs",
                    headers=self._headers())
                self.assertEqual(400, response.status_code, response.text)
                self.assertEqual("payload_invalid",
                                 response.json()["error"]["code"])
        created = self._create_assignment(key="assign-key-bad-cursor")
        assignment_id = created.json()["assignment_id"]
        for bad_cursor in ("not-a-cursor!!", "b3RoZXI=", "asg_" + "a" * 24):
            with self.subTest(cursor=bad_cursor):
                response = self.client.get(
                    f"/v1/assignments/{assignment_id}/jobs",
                    headers=self._headers(),
                    params={"cursor": bad_cursor})
                self.assertEqual(400, response.status_code, response.text)
                self.assertEqual("payload_invalid",
                                 response.json()["error"]["code"])

    def test_assignment_jobs_unknown_and_foreign_ids_are_indistinguishable(self):
        missing = self.client.get(
            "/v1/assignments/" + "asg_" + "0" * 24 + "/jobs",
            headers=self._headers())
        self.assertEqual(404, missing.status_code, missing.text)
        created = self._create_assignment(key="assign-key-foreign-list")
        assignment_id = created.json()["assignment_id"]
        other = self._client(TENANT, engine_marker=None)
        foreign = other.get(f"/v1/assignments/{assignment_id}/jobs",
                            headers=self._headers("tok-b"))
        self.assertEqual(404, foreign.status_code, foreign.text)
        # Same code for "never existed" and "owned by someone else", never 403.
        self.assertEqual(missing.json()["error"]["code"],
                         foreign.json()["error"]["code"])
        self.assertEqual("assignment_not_found",
                         foreign.json()["error"]["code"])

    def test_assignment_jobs_rejects_out_of_range_limit_like_assignments(self):
        created = self._create_assignment(key="assign-key-limit")
        assignment_id = created.json()["assignment_id"]
        for bad_limit in (0, 101):
            with self.subTest(limit=bad_limit):
                response = self.client.get(
                    f"/v1/assignments/{assignment_id}/jobs",
                    headers=self._headers(),
                    params={"limit": bad_limit})
                # Same validation as GET /v1/assignments (identical bounds).
                self.assertEqual(response.status_code,
                                 self.client.get(
                                     "/v1/assignments",
                                     headers=self._headers(),
                                     params={"limit": bad_limit}
                                 ).status_code)

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
        # Question results ride on the succeeded status payload as the
        # MARKS-ONLY projection: question number + awarded marks, never the
        # student's answers or working text.
        self.assertEqual(
            state["questions"],
            [{"question_number": "1", "awarded_marks": 2},
             {"question_number": "2", "awarded_marks": 1}])
        raw = json.dumps(state)
        self.assertNotIn("student_answer", raw)
        self.assertNotIn("SYNTHETIC-ANSWER", raw)
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
        # A non-succeeded job never advertises question results.
        for outcome in outcomes:
            self.assertNotIn("questions", outcome)
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
        # Configured store, but no job: the denial wrote NOTHING.
        result_id = "manual:" + digest[:16] + ":ref-1"
        audit = audit_store.open_store(
            self._env["AI_MARKER_AUDIT_DEV_STORE"], dev_only_ack=True)
        self.addCleanup(audit.close)
        self.assertIsNone(audit.current_revision(owner=OWNER,
                                                 result_id=result_id))
        registry = ingest_registry.open_store(
            self._env[ingest_registry.PATH_ENV])
        self.addCleanup(registry.close)
        self.assertEqual(ingest_registry.find_rows(registry, OWNER, TENANT),
                         [])
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

    def test_running_window_cannot_preempt_the_result(self):
        """Finding J-1 regression: a live claim is runner-only authority.

        While a job is in flight (status running, unexpired lease), an
        authenticated owner posting /v1/results with a fabricated original -
        under EITHER an arbitrary reference or the job's deterministic
        identity - must be denied, with ZERO rows written anywhere.
        """
        job_id = self._create().json()["job_id"]
        connection = job_store.open_store(self._env[job_store.PATH_ENV])
        self.addCleanup(connection.close)
        connection.execute(
            "UPDATE marking_jobs SET status = 'running', attempts = 1,"
            " lease_until = ? WHERE job_id = ?",
            (int(time.time()) + 3600, job_id))
        connection.commit()
        audit = audit_store.open_store(
            self._env["AI_MARKER_AUDIT_DEV_STORE"], dev_only_ack=True)
        self.addCleanup(audit.close)
        registry = ingest_registry.open_store(
            self._env[ingest_registry.PATH_ENV])
        self.addCleanup(registry.close)
        for reference in ("attacker-ref-0001", job_id):
            result_id = "manual:" + self.digest[:16] + ":" + reference
            denied = self.client.post(
                "/v1/results", headers=self._headers(),
                json={"scope": "manual",
                      "manual_identity": {"submission_ref": reference,
                                          "document_digest": self.digest},
                      "original_output": engine_output()})
            self.assertEqual(denied.status_code, 403, denied.text)
            self.assertEqual(denied.json()["error"]["code"],
                             "manual_document_provenance_required")
            self.assertEqual(denied.json()["error"].get("detail"),
                             "ingest_authorization_required")
            # Zero writes: no audit registration, no registry row.
            self.assertIsNone(audit.current_revision(owner=OWNER,
                                                     result_id=result_id))
            self.assertEqual(
                ingest_registry.find_rows(registry, OWNER, TENANT), [])
        # The lease-holding runner itself still succeeds through its own
        # authority even though the client-facing gate now refuses.
        connection.execute(
            "UPDATE marking_jobs SET status = 'queued', attempts = 0,"
            " lease_until = 0 WHERE job_id = ?", (job_id,))
        connection.commit()
        self.assertEqual(self._run(job_id).json()["status"], "succeeded")

    def test_garbage_engine_output_fails_with_a_specific_code(self):
        self.engine = lambda data: "not a marking result"
        client = self._client(TENANT)
        job_id = self._create(client=client).json()["job_id"]
        state = self._run(job_id, client=client).json()
        self.assertEqual(state["status"], "queued")
        self.assertEqual(state["error_code"], "original_output_invalid")
        self.assertEqual(state["attempts"], 1)

    def test_default_engine_wiring_is_fail_closed_without_opt_in(self):
        """create_app() with NO engine argument builds the REAL wiring path.

        With no AI_MARKER_ENGINE_REAL opt-in (and no key/mark scheme) that
        path yields no engine, so the runner refuses 503 model_unavailable
        and the job stays untouched - the default can never fabricate work.
        """
        for name in ("AI_MARKER_ENGINE_REAL", "AI_MARKER_MARK_SCHEME",
                     "GEMINI_API_KEY", "AI_MARKER_ENGINE_AUDIT"):
            prior = os.environ.pop(name, None)
            if prior is not None:
                self.addCleanup(os.environ.__setitem__, name, prior)
            else:
                self.addCleanup(os.environ.pop, name, None)
        client = TestClient(create_app(
            verify_token=TOKENS.get,
            membership_resolver=lambda subject: Membership(
                subject, TENANT, "teacher"),
            manual_document_provenance_ok=lambda row: True))
        job_id = self._create(client=client).json()["job_id"]
        refused = self._run(job_id, client=client)
        self.assertEqual(refused.status_code, 503, refused.text)
        self.assertEqual(refused.json()["error"]["code"], "model_unavailable")
        state = client.get(f"/v1/jobs/{job_id}",
                           headers=self._headers()).json()
        self.assertEqual((state["status"], state["attempts"]), ("queued", 0))

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
