"""Independent attack/regression contract tests for v1.2 assignments (BCM).

Complements CM's tests/test_api_assignments.py (happy paths) with the
production-blocker angles the coordinator assigned to the independent lane:

* cross-TENANT isolation: two verified subjects in different tenants must
  see disjoint lists, and one tenant's rows must never surface in the
  other tenant's list however the key or cursor is reused;
* cursor abuse: a cursor minted for another owner must neither crash nor
  leak rows, and shape manipulation must fail closed (400);
* idempotency-key abuse: whitespace normalization, key length bounds, and
  the exact key-vs-title replay matrix including order effects;
* authorization and limit boundary behaviour on BOTH routes.

Same synthetic offline harness as the rest of the suite: dev opt-in SQLite
store, no network, no PostgreSQL, no live service.
"""
import os
import tempfile
import unittest

import assignment_store
from api.app import create_app
from membership_store import Membership
from fastapi.testclient import TestClient

TENANT_ALPHA = "tenant-alpha"
TENANT_BETA = "tenant-beta"
TOKENS = {"tok-a1": "teacher-alpha-1", "tok-b1": "teacher-beta-1"}
HEADERS_A1 = {"Authorization": "Bearer tok-a1"}


class AssignmentAttackContractTests(unittest.TestCase):
    """Independent lane: attack and isolation angles over the v1.2 shape."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self._env = {
            assignment_store.PATH_ENV:
                os.path.join(self.tmp.name, "assignments.sqlite3"),
            assignment_store.ACK_ENV: "1",
        }
        for name, value in self._env.items():
            os.environ[name] = value
            self.addCleanup(os.environ.pop, name, None)
        self.client = TestClient(create_app(
            verify_token=lambda token: TOKENS.get(token),
            membership_resolver=self._resolver))

    def _resolver(self, subject):
        tenant = TENANT_ALPHA if subject == TOKENS["tok-a1"] else TENANT_BETA
        return Membership(subject, tenant, "teacher")

    def _post(self, key, title, token="tok-a1"):
        return self.client.post(
            "/v1/assignments", headers={"Authorization": f"Bearer {token}"},
            json={"idempotency_key": key, "title": title})

    def _get(self, token="tok-a1", **params):
        return self.client.get(
            "/v1/assignments", headers={"Authorization": f"Bearer {token}"},
            params=params or None)

    # --- cross-tenant isolation ------------------------------------------

    def test_lists_of_two_tenants_are_disjoint_even_with_shared_keys(self):
        """Both teachers reuse the same literal idempotency keys and titles.

        The server mints ids per (tenant, owner, key) so identity collision
        across tenants must never merge, replay, or cross-serve rows.
        """
        first_a = self._post("isolation-key-0001", "Alpha paper", "tok-a1")
        first_b = self._post("isolation-key-0001", "Alpha paper", "tok-b1")
        self.assertEqual(201, first_a.status_code, first_a.text)
        self.assertEqual(201, first_b.status_code, first_b.text)
        self.assertNotEqual(first_a.json()["assignment_id"],
                            first_b.json()["assignment_id"],
                            "two tenants must mint independent ids")
        # Same tenant replays; the other tenant cannot enter another
        # tenant's row even with a byte-identical key AND title.
        replay_a = self._post("isolation-key-0001", "Alpha paper", "tok-a1")
        replay_b = self._post("isolation-key-0001", "Alpha paper", "tok-b1")
        self.assertEqual(200, replay_a.status_code, replay_a.text)
        self.assertEqual(200, replay_b.status_code, replay_b.text)
        self.assertEqual(first_a.json()["assignment_id"],
                         replay_a.json()["assignment_id"])
        self.assertEqual(first_b.json()["assignment_id"],
                         replay_b.json()["assignment_id"])
        mine = self._get("tok-a1").json()["assignments"]
        theirs = self._get("tok-b1").json()["assignments"]
        self.assertEqual([first_a.json()["assignment_id"]],
                         [row["assignment_id"] for row in mine])
        self.assertEqual([first_b.json()["assignment_id"]],
                         [row["assignment_id"] for row in theirs])
        self.assertNotIn(first_b.json()["assignment_id"], mine)
        self.assertNotIn(first_a.json()["assignment_id"], theirs)

    def test_cross_tenant_titles_do_not_conflict(self):
        self._post("conflict-less-key-01", "Same rational title", "tok-a1")
        response = self._post("conflict-less-key-01",
                              "Different-tenant title", "tok-b1")
        # Key is scoped per tenant/owner, so differing titles only conflict
        # within one identity; across tenants both create.
        self.assertEqual(201, response.status_code, response.text)

    def test_cursor_of_another_teacher_neither_crashes_nor_leaks(self):
        first = self._post("cursor-key-0001", "Alpha paper").json()
        mine_first_page = self._get(limit=1).json()
        cursor = mine_first_page["next_cursor"]
        # A tenant-B teacher carrying tenant-A's minted cursor must get a
        # 200 page containing ONLY tenant-B rows (empty here), never alpha's.
        leaked = self._get("tok-b1", limit=1, cursor=cursor)
        self.assertEqual(200, leaked.status_code, leaked.text)
        ids = [row["assignment_id"] for row in leaked.json()["assignments"]]
        self.assertNotIn(first["assignment_id"], ids)
        # Own cursor is still fine and deterministic.
        self.assertEqual({"assignment_id", "title", "created_at"},
                         set(mine_first_page["assignments"][0]))

    def test_cursor_tampering_fails_closed(self):
        for payload in ("....", "aGVsbG8=", "1:asg_", "0:not-an-id",
                        "99999999999999:asg_" + "z" * 24):
            with self.subTest(cursor=payload):
                response = self._get(cursor=payload)
                self.assertEqual(400, response.status_code, response.text)
                self.assertEqual("payload_invalid",
                                 response.json()["error"]["code"])

    # --- retry / Idempotency-Key transport (header-canonical head) -------

    def test_retry_round_trips_identically_across_repeats(self):
        """A network retry (same request re-sent) cannot mint a second row."""
        first = self._post("retry-key-00001", "Paper").json()
        for _ in range(4):
            response = self._post("retry-key-00001", "Paper")
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(first, response.json())
        self.assertEqual(1, len(self._get().json()["assignments"]))

    def test_canonical_header_key_is_accepted_with_body_alias_absent(self):
        response = self.client.post(
            "/v1/assignments", headers=HEADERS_A1 | {"Idempotency-Key":
                                                     "hdr-zone-key-0001"},
            json={"title": "Header paper"})
        self.assertEqual(201, response.status_code, response.text)
        replay = self.client.post(
            "/v1/assignments", headers=HEADERS_A1 | {"Idempotency-Key":
                                                     "hdr-zone-key-0001"},
            json={"title": "Header paper"})
        self.assertEqual(200, replay.status_code)
        self.assertEqual(response.json()["assignment_id"],
                         replay.json()["assignment_id"])

    def test_header_body_mismatch_is_refused_not_precedence_resolved(self):
        response = self.client.post(
            "/v1/assignments",
            headers=HEADERS_A1 | {"Idempotency-Key": "mismatch-key-00001"},
            json={"idempotency_key": "mismatch-key-00002",
                  "title": "Ambiguous"})
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual("payload_invalid",
                         response.json()["error"]["code"])
        self.assertEqual([], self._get().json()["assignments"],
                         "a refused ambiguous request must not create")

    def test_retry_header_matches_first_call_and_replays(self):
        """FCM retry path: same retry re-declares the SAME key + field."""
        first = self.client.post(
            "/v1/assignments", headers=HEADERS_A1 | {"Idempotency-Key":
                                                     "retryhdr-key-0001"},
            json={"idempotency_key": "retryhdr-key-0001", "title": "Retry"})
        self.assertEqual(201, first.status_code, first.text)
        again = self.client.post(
            "/v1/assignments", headers=HEADERS_A1 | {"Idempotency-Key":
                                                     "retryhdr-key-0001"},
            json={"idempotency_key": "retryhdr-key-0001", "title": "Retry"})
        self.assertEqual(200, again.status_code)
        self.assertEqual(first.json(), again.json())

    def test_retry_after_conflict_must_replay_original_not_diverge(self):
        retry_key = "resolve-key-00001"
        self._post(retry_key, "Original title").json()
        first_failed = self._post(retry_key, "Trailing modified")
        self.assertEqual(409, first_failed.status_code)
        retried = self._post(retry_key, "Original title")
        self.assertEqual(200, retried.status_code)
        self.assertEqual("Original title",
                         self._post(retry_key, "Original title").json()["title"])

    # --- idempotency-key abuse ------------------------------------------

    def test_key_is_trimmed_so_whitespace_variants_replay(self):
        first = self._post("  trim-key-0001  ", "Paper").json()
        second = self._post("trim-key-0001", "Paper").json()
        self.assertEqual(first["assignment_id"], second["assignment_id"])

    def test_key_length_bounds_are_enforced(self):
        too_short = self._post("key", "Paper")
        too_long = self._post("k" * 129, "Paper")
        for response in (too_short, too_long):
            self.assertEqual(400, response.status_code, response.text)
            self.assertEqual("payload_invalid",
                             response.json()["error"]["code"])

    def test_empty_body_fields_fail_the_existing_vocabulary(self):
        missing_key = self.client.post(
            "/v1/assignments", headers=HEADERS_A1, json={"title": "x"})
        missing_title = self.client.post(
            "/v1/assignments", headers=HEADERS_A1,
            json={"idempotency_key": "bounds-key-0001"})
        for response in (missing_key, missing_title):
            self.assertEqual(400, response.status_code, response.text)
            self.assertEqual("payload_invalid",
                             response.json()["error"]["code"])

    def test_replay_matrix_is_exact(self):
        """201 -> 200 (same key+title) -> 409 (same key, changed title)."""
        self.assertEqual(201, self._post("matrix-key-0001", "V1").status_code)
        replay = self._post("matrix-key-0001", "V1")
        self.assertEqual(200, replay.status_code)
        conflict = self._post("matrix-key-0001", "V2")
        self.assertEqual(409, conflict.status_code)
        self.assertEqual("idempotency_key_payload_mismatch",
                         conflict.json()["error"]["code"])
        # The conflict must not have mutated the row.
        merged = self._post("matrix-key-0001", "V1")
        self.assertEqual(200, merged.status_code)

    # --- identity, limits, both routes ----------------------------------

    def test_both_assignment_routes_demand_a_token(self):
        self.assertEqual(401, self.client.get("/v1/assignments").status_code)
        self.assertEqual(401, self.client.post(
            "/v1/assignments", json={"idempotency_key": "anon-key-00001",
                                     "title": "x"}).status_code)

    def test_limit_boundaries(self):
        self.assertEqual(200, self._get(limit=1).status_code)
        self.assertEqual(200, self._get(limit=100).status_code)
        # Out-of-range limits come back as the contract's own vocabulary,
        # 400 payload_invalid — NOT a raw FastAPI 422 (the app maps them).
        for bad_limit in (0, 101, -5):
            with self.subTest(limit=bad_limit):
                response = self._get(limit=bad_limit)
                self.assertEqual(400, response.status_code, response.text)
                self.assertEqual("payload_invalid",
                                 response.json()["error"]["code"])


class AssignmentJobLinkageTests(unittest.TestCase):
    """Independent lane for the v1.2 additive jobs-linkage (PR72 21da3d61).

    Distinct from CM's tests/test_api_jobs.py linkage cases: byte-level
    indistinguishability of missing vs foreign ids, cross-TENANT linkage,
    linkage persistence across job replay AND run, isolation of another
    owner's job, and the failed-attempt key-consumption race.
    """

    @classmethod
    def setUpClass(cls):
        import hashlib
        import pdf_cases
        import document_store
        import job_store
        import ingest_registry
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.pdf = pdf_cases.make_pdf()
        cls.digest = hashlib.sha256(cls.pdf).hexdigest()

    def setUp(self):
        import audit_store
        import document_store
        import ingest_registry
        import job_store
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self._env = {
            assignment_store.PATH_ENV:
                os.path.join(self.tmp.name, "assignments.sqlite3"),
            assignment_store.ACK_ENV: "1",
            job_store.PATH_ENV: os.path.join(self.tmp.name, "jobs.sqlite3"),
            job_store.ACK_ENV: "1",
            document_store.PATH_ENV:
                os.path.join(self.tmp.name, "documents.sqlite3"),
            document_store.ACK_ENV: "1",
            ingest_registry.PATH_ENV:
                os.path.join(self.tmp.name, "ingest.sqlite3"),
            ingest_registry.ACK_ENV: "1",
            "AI_MARKER_AUDIT_DEV_STORE":
                os.path.join(self.tmp.name, "audit.sqlite3"),
            "AI_MARKER_AUDIT_DEV_ONLY": "1",
        }
        for name, value in self._env.items():
            os.environ[name] = value
            self.addCleanup(os.environ.pop, name, None)
        self.client = self._client(TENANT_ALPHA)
        uploaded = self.client.post("/v1/documents",
                                    headers=self._headers(),
                                    params={"filename": "work.pdf"},
                                    content=self.pdf)
        assert uploaded.status_code == 201, uploaded.text

    def _client(self, tenant_id, engine_marker=True):
        def engine(data=None):
            return {"questions": [{"question_number": "1",
                                   "awarded_marks": 2, "max_marks": 3,
                                   "student_answer": "SYNTHETIC-ANSWER"}]}
        engine = engine if engine_marker is True else None
        client = TestClient(create_app(
            verify_token=TOKENS.get,
            membership_resolver=lambda subject: Membership(
                subject, tenant_id, "teacher"),
            manual_document_provenance_ok=lambda row: True,
            engine=engine))
        return client

    def _headers(self, token="tok-a1"):
        return {"Authorization": f"Bearer {token}"}

    def _assignment(self, key, title="Linked paper", token="tok-a1"):
        return self.client.post(
            "/v1/assignments", headers=self._headers(token) | {
                "Idempotency-Key": key}, json={"title": title})

    def _job(self, key, assignment_id=None, token="tok-a1", client=None):
        body = {"idempotency_key": key, "document_digest": self.digest}
        if assignment_id:
            body["assignment_id"] = assignment_id
        return (client or self.client).post(
            "/v1/jobs", headers=self._headers(token), json=body)

    # --- indistinguishability ---------------------------------------------

    def test_missing_and_foreign_ids_are_byte_identical_404s(self):
        missing = "asg_" + "f" * 24
        # A genuinely foreign id: minted by a different tenant+owner.
        isolated = self._client(TENANT_BETA)
        isolated.post("/v1/documents", headers=self._headers("tok-b1"),
                      params={"filename": "beta.pdf"}, content=self.pdf)
        foreign_created = isolated.post(
            "/v1/assignments",
            headers=self._headers("tok-b1") | {"Idempotency-Key":
                                              "asg-key-00000001"},
            json={"title": "Beta paper"})
        self.assertEqual(201, foreign_created.status_code, foreign_created.text)
        foreign = foreign_created.json()["assignment_id"]
        bodies = []
        for target in (missing, foreign):
            response = self._job("probe-key-0001", assignment_id=target)
            self.assertEqual(404, response.status_code, response.text)
            self.assertEqual("assignment_not_found",
                             response.json()["error"]["code"])
            bodies.append((response.json(),
                           response.headers.get("content-length")))
        self.assertEqual(bodies[0], bodies[1],
                         "a foreign id must be indistinguishable from a miss")

    def test_cross_tenant_assignment_is_not_linkable(self):
        created = self._assignment("asg-key-00000002")
        assignment_id = created.json()["assignment_id"]
        tenant_beta = self._client(TENANT_BETA)
        tenant_beta.post("/v1/documents", headers=self._headers("tok-b1"),
                         params={"filename": "beta.pdf"}, content=self.pdf)
        response = tenant_beta.post(
            "/v1/jobs", headers=self._headers("tok-b1"),
            json={"idempotency_key": "xtid-key-00000001",
                  "document_digest": self.digest,
                  "assignment_id": assignment_id})
        self.assertEqual(404, response.status_code, response.text)
        self.assertEqual("assignment_not_found",
                         response.json()["error"]["code"])
        # The same teacher scores their own fine: scope is tenant+owner.
        own = tenant_beta.post(
            "/v1/assignments",
            headers=self._headers("tok-b1") | {"Idempotency-Key":
                                              "asg-key-00000004"},
            json={"title": "Linked paper"})
        self.assertEqual(201, own.status_code, own.text)
        ok = tenant_beta.post(
            "/v1/jobs", headers=self._headers("tok-b1"),
            json={"idempotency_key": "xtid-key-00000002",
                  "document_digest": self.digest,
                  "assignment_id": own.json()["assignment_id"]})
        self.assertEqual(201, ok.status_code, ok.text)

    def test_failed_link_attempt_does_not_consume_the_key(self):
        failed = self._job("consume-key-0001", assignment_id="asg_" + "d" * 24)
        self.assertEqual(404, failed.status_code, failed.text)
        # The key must remain usable: same key + valid payload still seeds.
        seeded = self._job("consume-key-0001")
        self.assertEqual(201, seeded.status_code, seeded.text)

    # --- persistence / echo -----------------------------------------------

    def test_link_persists_across_replay_and_run(self):
        created = self._assignment("asg-key-00000005").json()
        job = self._job("persist-key-0001",
                        assignment_id=created["assignment_id"])
        self.assertEqual(201, job.status_code, job.text)
        job_id = job.json()["job_id"]
        replay = self._job("persist-key-0001",
                           assignment_id=created["assignment_id"])
        self.assertEqual(200, replay.status_code)
        self.assertEqual(created["assignment_id"],
                         replay.json()["assignment_id"])
        self.assertEqual(job_id, replay.json()["job_id"])
        run = self.client.post(f"/v1/jobs/{job_id}/run",
                               headers=self._headers())
        self.assertEqual(200, run.status_code, run.text)
        status = self.client.get(f"/v1/jobs/{job_id}",
                                 headers=self._headers())
        self.assertEqual(200, status.status_code, status.text)
        self.assertEqual(created["assignment_id"],
                         status.json()["assignment_id"],
                         "linkage must survive registration")
        mine = self.client.get("/v1/assignments", headers=self._headers())
        self.assertEqual([created["assignment_id"]],
                         [row["assignment_id"]
                          for row in mine.json()["assignments"]],
                         "the list never widens because of the link")

    def test_unlinked_job_keeps_the_agreed_v1_2_payload_bytes(self):
        """0668bf5 absent-means-omit: only a LINKED job may grow the field.

        The agreed v1.2 response shape had no assignment_id, so an unlinked
        job must not emit ``null`` - that would change bytes for a strict
        pre-v1.2.1 client. The linked bundle must differ by exactly that key.
        """
        unlinked = self._job("omit-key-00000001")
        self.assertEqual(201, unlinked.status_code, unlinked.text)
        created = unlinked.json()
        self.assertNotIn("assignment_id", created)
        self.assertEqual({"job_id", "status", "attempts", "max_attempts",
                          "document_digest", "result_id", "error_code",
                          "owner", "created"},
                         set(created),
                         "an unlinked job must expose exactly the v1.2 keys")
        unlinked_status = self.client.get(f"/v1/jobs/{created['job_id']}",
                                          headers=self._headers())
        self.assertEqual(200, unlinked_status.status_code)
        self.assertNotIn("assignment_id", unlinked_status.json())

        linked = self._assignment("asg-key-00000007").json()
        with_link = self._job("omit-key-00000002",
                              assignment_id=linked["assignment_id"])
        self.assertEqual(201, with_link.status_code, with_link.text)
        linked_body = with_link.json()
        self.assertEqual(linked["assignment_id"], linked_body["assignment_id"])
        baseline = dict(created)
        baseline.pop("job_id")
        compared = dict(linked_body)
        compared.pop("job_id")
        self.assertEqual({"assignment_id"}, set(compared) - set(baseline))
        for key, value in baseline.items():
            self.assertEqual(value, compared[key],
                             f"linking must not perturb {key}")

    def test_another_owner_cannot_read_the_linked_job(self):
        created = self._assignment("asg-key-00000006").json()
        job = self._job("isolate-key-0001",
                        assignment_id=created["assignment_id"])
        job_id = job.json()["job_id"]
        response = self._client(TENANT_ALPHA).get(
            f"/v1/jobs/{job_id}", headers=self._headers("tok-b1"))
        self.assertEqual(404, response.status_code, response.text)
        self.assertEqual("result_not_registered",
                         response.json()["error"]["code"],
                         "foreign job read must be enumeration-safe, not"
                         " a forbidden leak")


if __name__ == "__main__":
    unittest.main()
