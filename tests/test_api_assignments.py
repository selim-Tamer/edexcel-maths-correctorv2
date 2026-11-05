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
import membership_store
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


class AssignmentAccessControlTests(unittest.TestCase):
    """Fail-closed auth/membership and cross-tenant denial for assignments.

    The create/replay/conflict happy paths live in ``AssignmentApiTests``;
    this class pins the denial paths a browser client must see once CORS
    lets the request through: 401 without a verified token, 403 without an
    ACTIVE membership, and a foreign tenant that can neither read, replay
    nor address the owner's rows.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        os.environ[assignment_store.PATH_ENV] = os.path.join(
            self.tmp.name, "assignments.sqlite3")
        self.addCleanup(os.environ.pop, assignment_store.PATH_ENV, None)
        os.environ[assignment_store.ACK_ENV] = "1"
        self.addCleanup(os.environ.pop, assignment_store.ACK_ENV, None)

    def _client(self, resolver):
        return TestClient(create_app(
            verify_token=TOKENS.get, membership_resolver=resolver))

    @staticmethod
    def _active(subject):
        return Membership(subject, TENANT, "teacher")

    @staticmethod
    def _two_tenants(subject):
        # Subject A stays in tenant-beta; subject B is a different tenant.
        return Membership(subject, TENANT if subject == SUBJECT_A
                          else "tenant-gamma", "teacher")

    def _create(self, client, key="access-key-0001", title="Year 10 Paper 1",
                token="tok-a"):
        headers = {"Authorization": f"Bearer {token}",
                   "Idempotency-Key": key}
        return client.post("/v1/assignments", headers=headers,
                           json={"title": title})

    # --- authentication ---------------------------------------------------

    def test_missing_or_unverifiable_token_is_401(self):
        client = self._client(self._active)
        for headers in ({"Idempotency-Key": "auth-key-00001"},
                        {"Authorization": "Bearer not-a-token",
                         "Idempotency-Key": "auth-key-00002"},
                        {"Authorization": "Basic dXNlcjpwdw==",
                         "Idempotency-Key": "auth-key-00003"}):
            with self.subTest(headers=headers):
                response = client.post("/v1/assignments", headers=headers,
                                       json={"title": "Nope"})
                self.assertEqual(401, response.status_code, response.text)
                self.assertEqual("authentication_required",
                                 response.json()["error"]["code"])
        # No row was written by any refused attempt.
        self.assertEqual([], client.get(
            "/v1/assignments",
            headers={"Authorization": "Bearer tok-a"}).json()["assignments"])

    # --- membership -------------------------------------------------------

    def test_inactive_unknown_or_mismatched_membership_is_403(self):
        # resolver None => no ACTIVE row for the verified subject, whether
        # the membership is inactive or simply unknown.
        inactive = self._client(lambda subject: None)
        response = self._create(inactive, key="inactive-key-0001")
        self.assertEqual(403, response.status_code, response.text)
        self.assertEqual("tenant_membership_required",
                         response.json()["error"]["code"])

        # A row that does not belong to the verified subject fails closed
        # with the same non-enumerating envelope.
        mismatched = self._client(
            lambda subject: Membership("someone-else", TENANT, "teacher"))
        response = self._create(mismatched, key="mismatched-key-0001")
        self.assertEqual(403, response.status_code, response.text)
        self.assertEqual("tenant_membership_required",
                         response.json()["error"]["code"])

        # A failing membership store is 403 too - never 500, never 401.
        def broken(subject):
            raise membership_store.MembershipError("membership_unavailable")
        failing = self._client(broken)
        response = self._create(failing, key="broken-key-00001")
        self.assertEqual(403, response.status_code, response.text)
        self.assertEqual("tenant_membership_required",
                         response.json()["error"]["code"])

    # --- tenant isolation -------------------------------------------------

    def test_foreign_tenant_is_denied_rows_keys_and_jobs(self):
        client = self._client(self._two_tenants)
        created = self._create(client, key="cross-key-0001", title="Mine")
        self.assertEqual(201, created.status_code, created.text)

        foreign = {"Authorization": "Bearer tok-b"}
        # Another tenant reads an empty list, not the owner's row.
        listed = client.get("/v1/assignments", headers=foreign)
        self.assertEqual(200, listed.status_code, listed.text)
        self.assertEqual([], listed.json()["assignments"])

        # The SAME key in the other tenant is its own reservation, not a
        # replay of - nor a conflict with - the first tenant's row.
        other = self._create(client, key="cross-key-0001", title="Theirs",
                             token="tok-b")
        self.assertEqual(201, other.status_code, other.text)
        self.assertNotEqual(created.json()["assignment_id"],
                            other.json()["assignment_id"])

        # Addressing the owner's assignment from the other tenant is the
        # indistinguishable 404, never a 403 oracle or a data grant.
        jobs = client.get(
            f"/v1/assignments/{created.json()['assignment_id']}/jobs",
            headers=foreign)
        self.assertEqual(404, jobs.status_code, jobs.text)
        self.assertEqual("assignment_not_found", jobs.json()["error"]["code"])

        # The owner's rows are untouched by all of the above.
        mine = client.get("/v1/assignments",
                          headers={"Authorization": "Bearer tok-a"})
        self.assertEqual(1, len(mine.json()["assignments"]))

    # --- representative routes --------------------------------------------

    def test_representative_routes_keep_their_existing_contracts(self):
        client = self._client(self._two_tenants)
        health = client.get("/v1/healthz")
        self.assertEqual(200, health.status_code)
        self.assertEqual({"status": "ok"}, health.json())

        created = self._create(client, key="route-key-00001")
        self.assertEqual(201, created.status_code, created.text)
        self.assertEqual({"assignment_id", "title", "created_at"},
                         set(created.json()))

        listed = client.get("/v1/assignments",
                            headers={"Authorization": "Bearer tok-a"})
        self.assertEqual(200, listed.status_code, listed.text)
        self.assertEqual({"assignments", "next_cursor"}, set(listed.json()))
        # No Origin on the request, so no CORS grant is invented.
        self.assertNotIn("access-control-allow-origin", listed.headers)


if __name__ == "__main__":
    unittest.main()
