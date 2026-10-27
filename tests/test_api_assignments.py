"""Server-backed assignment contract tests (dev store, offline, synthetic).

Covers contract v1.2 PROPOSED: POST/GET /v1/assignments with
* tenant/owner derived ONLY from the verified token (the body cannot set
  them, and two teachers in one tenant are isolated);
* a server-minted opaque ``assignment_id`` (the client sends no id);
* required idempotency key: same key + same title replays the SAME
  assignment, same key + different title is 409;
* trimmed, 1..200 character titles and the existing error vocabulary;
* newest-first keyset pagination with an opaque cursor and no duplicate rows;
* fail-closed unconfigured store.

No network, no Postgres, no live service.
"""
import os
import tempfile
import unittest

import assignment_store
from api.app import create_app
from membership_store import Membership
from fastapi.testclient import TestClient

SUBJECT_A = "teacher-a-subject"
SUBJECT_B = "teacher-b-subject"
TENANT = "tenant-beta"
TOKENS = {"tok-a": SUBJECT_A, "tok-b": SUBJECT_B}
HEADERS = {"Authorization": "Bearer tok-a"}


class AssignmentApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self._env = {
            assignment_store.PATH_ENV:
                os.path.join(self.tmp.name, "assignments.sqlite3"),
            assignment_store.ACK_ENV: "1",
        }
        self._set_env(self._env)
        self.client = self._client()

    def _set_env(self, values):
        for name, value in values.items():
            os.environ[name] = value
            self.addCleanup(os.environ.pop, name, None)

    def _client(self, resolver=None):
        return TestClient(create_app(
            verify_token=TOKENS.get,
            membership_resolver=resolver or (
                lambda subject: Membership(subject, TENANT, "teacher"))))

    def _headers(self, token="tok-a"):
        return {"Authorization": f"Bearer {token}"}

    def _create(self, key="assignment-key-0001", title="Year 10 Paper 1",
                token="tok-a", client=None):
        headers = dict(self._headers(token))
        headers["Idempotency-Key"] = key
        return (client or self.client).post(
            "/v1/assignments", headers=headers, json={"title": title})

    # --- create / idempotency -------------------------------------------

    def test_create_returns_server_minted_id_and_exact_wire_shape(self):
        response = self._create()
        self.assertEqual(201, response.status_code, response.text)
        body = response.json()
        self.assertEqual({"assignment_id", "title", "created_at"},
                         set(body))
        self.assertRegex(body["assignment_id"], r"^asg_[0-9a-f]{24}$")
        self.assertEqual("Year 10 Paper 1", body["title"])
        self.assertIsInstance(body["created_at"], int)

    def test_client_cannot_choose_id_owner_or_tenant(self):
        response = self.client.post(
            "/v1/assignments", headers=self._headers(),
            json={"idempotency_key": "assignment-key-0002",
                  "title": "Mine", "assignment_id": "asg_" + "0" * 24,
                  "tenant_id": "attacker-tenant", "owner_subject": "someone"})
        self.assertEqual(201, response.status_code, response.text)
        body = response.json()
        self.assertNotEqual("asg_" + "0" * 24, body["assignment_id"])
        self.assertEqual(1, len(self.client.get(
            "/v1/assignments", headers=self._headers()).json()["assignments"]))

    def test_same_key_and_title_replays_not_duplicates(self):
        first = self._create(key="replay-key-0001")
        second = self._create(key="replay-key-0001")
        self.assertEqual(201, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual(first.json(), second.json())
        listed = self.client.get("/v1/assignments",
                                 headers=self._headers()).json()
        self.assertEqual(1, len(listed["assignments"]))

    def test_same_key_different_title_is_a_conflict(self):
        self._create(key="conflict-key-0001", title="Paper 1")
        response = self._create(key="conflict-key-0001", title="Paper 2")
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("idempotency_key_payload_mismatch", response.json()["error"]["code"])

    def test_idempotency_key_is_required(self):
        response = self.client.post(
            "/v1/assignments", headers=self._headers(), json={"title": "x"})
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual("payload_invalid", response.json()["error"]["code"])

    def test_header_is_the_canonical_idempotency_carrier(self):
        headers = dict(self._headers())
        headers["Idempotency-Key"] = "header-key-0001"
        response = self.client.post("/v1/assignments", headers=headers,
                                    json={"title": "Header carried"})
        self.assertEqual(201, response.status_code, response.text)

    def test_body_alias_still_works(self):
        response = self.client.post(
            "/v1/assignments", headers=self._headers(),
            json={"idempotency_key": "body-key-00001", "title": "Body alias"})
        self.assertEqual(201, response.status_code, response.text)

    def test_header_and_body_key_must_match(self):
        headers = dict(self._headers())
        headers["Idempotency-Key"] = "header-key-0002"
        response = self.client.post(
            "/v1/assignments", headers=headers,
            json={"idempotency_key": "body-key-00002", "title": "Mismatch"})
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual("payload_invalid", response.json()["error"]["code"])

    def test_header_and_matching_body_key_replay_the_same_row(self):
        headers = dict(self._headers())
        headers["Idempotency-Key"] = "match-key-00001"
        body = {"idempotency_key": "match-key-00001", "title": "Matched"}
        first = self.client.post("/v1/assignments", headers=headers, json=body)
        second = self.client.post("/v1/assignments", headers=headers, json=body)
        self.assertEqual(201, first.status_code)
        self.assertEqual(200, second.status_code)
        self.assertEqual(first.json(), second.json())

    # --- validation ------------------------------------------------------

    def test_title_is_trimmed(self):
        response = self._create(title="  Trimmed title  ")
        self.assertEqual(201, response.status_code)
        self.assertEqual("Trimmed title", response.json()["title"])

    def test_blank_title_is_refused(self):
        response = self._create(title="   ")
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual("payload_invalid", response.json()["error"]["code"])

    def test_title_over_200_chars_is_refused(self):
        response = self._create(title="x" * 201)
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual("payload_invalid", response.json()["error"]["code"])

    # --- identity isolation ---------------------------------------------

    def test_list_is_owner_scoped_within_one_tenant(self):
        self._create(title="A paper", token="tok-a")
        mine = self.client.get("/v1/assignments", headers=self._headers())
        theirs = self.client.get("/v1/assignments",
                                 headers=self._headers("tok-b"))
        self.assertEqual(1, len(mine.json()["assignments"]))
        self.assertEqual([], theirs.json()["assignments"])

    def test_same_key_from_another_owner_is_a_different_assignment(self):
        first = self._create(key="shared-key-0001", title="A", token="tok-a")
        second = self._create(key="shared-key-0001", title="B", token="tok-b")
        self.assertEqual(201, first.status_code)
        self.assertEqual(201, second.status_code)
        self.assertNotEqual(first.json()["assignment_id"],
                            second.json()["assignment_id"])

    # --- pagination ------------------------------------------------------

    def test_pagination_returns_each_row_once(self):
        for index in range(5):
            self._create(key=f"page-key-{index:04d}", title=f"Paper {index}")
        seen = []
        cursor = None
        for _ in range(5):
            params = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            response = self.client.get("/v1/assignments",
                                       headers=self._headers(),
                                       params=params)
            self.assertEqual(200, response.status_code, response.text)
            body = response.json()
            seen.extend(row["assignment_id"] for row in body["assignments"])
            cursor = body["next_cursor"]
            if not cursor:
                break
        self.assertEqual(5, len(seen))
        self.assertEqual(5, len(set(seen)))

    def test_malformed_cursor_is_refused(self):
        response = self.client.get("/v1/assignments", headers=self._headers(),
                                   params={"cursor": "not-a-cursor"})
        self.assertEqual(400, response.status_code, response.text)
        self.assertEqual("payload_invalid", response.json()["error"]["code"])

    # --- fail closed -----------------------------------------------------

    def test_unconfigured_store_fails_closed(self):
        os.environ.pop(assignment_store.PATH_ENV, None)
        response = self._create(key="unconfigured-key-0001")
        self.assertEqual(503, response.status_code, response.text)
        self.assertEqual("dev_opt_in_required", response.json()["error"]["code"])

    def test_unauthenticated_request_is_401(self):
        response = self.client.get("/v1/assignments")
        self.assertEqual(401, response.status_code)


if __name__ == "__main__":
    unittest.main()
