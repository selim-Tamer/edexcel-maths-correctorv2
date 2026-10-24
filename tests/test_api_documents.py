"""Private document adapter tests (dev store, offline, no network).

Covers the POST /v1/documents + GET /v1/documents/{digest} slice:
server-computed sha256 identity, document_limits validation at the API edge,
idempotent re-upload, exact-byte private reads with no-store caching, owner
AND tenant scoping with indistinguishable 404s, no anonymous access, and both
dev-store opt-in / validation failures refusing before any write.
"""
import hashlib
import os
import tempfile
import unittest

import document_limits
import document_store
import pdf_cases
from api.app import create_app
from membership_store import Membership
from fastapi.testclient import TestClient

SUBJECT = "teacher-a-subject"
OWNER = "google:" + SUBJECT
TENANT = "tenant-beta"
OTHER_TENANT = "tenant-gamma"
TOKENS = {"tok-a": SUBJECT, "tok-b": "teacher-b-subject"}


class DocumentApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "documents.sqlite3")
        self._env = {
            document_store.PATH_ENV: self.db,
            document_store.ACK_ENV: "1",
        }
        for name, value in self._env.items():
            os.environ[name] = value
            self.addCleanup(os.environ.pop, name, None)
        self.pdf = pdf_cases.make_pdf()
        self.client = self._client(TENANT)

    def _client(self, tenant_id, token_default="tok-a"):
        app = create_app(
            verify_token=TOKENS.get,
            membership_resolver=lambda subject: Membership(
                subject, tenant_id, "teacher"),
            manual_document_provenance_ok=lambda row: True)
        return TestClient(app)

    def _headers(self, token="tok-a"):
        return {"Authorization": f"Bearer {token}"}

    def _upload(self, data=None, token="tok-a", filename="work.pdf",
                client=None):
        return (client or self.client).post(
            "/v1/documents", headers=self._headers(token),
            params={"filename": filename},
            content=data if data is not None else self.pdf)

    def test_upload_mints_the_server_digest_and_is_idempotent(self):
        response = self._upload()
        self.assertEqual(response.status_code, 201, response.text)
        receipt = response.json()
        expected = hashlib.sha256(self.pdf).hexdigest()
        self.assertEqual(receipt["document_digest"], expected)
        self.assertEqual(receipt["owner"], OWNER)
        self.assertEqual(receipt["bytes"], len(self.pdf))
        self.assertGreater(receipt["pages"], 0)
        self.assertTrue(receipt["stored"])
        # Identical content again: 200, same receipt, stored unchanged.
        again = self._upload()
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["document_digest"], expected)
        self.assertFalse(again.json()["stored"])
        # Exactly one row exists for this owner+tenant.
        connection = document_store.open_store(self.db)
        self.addCleanup(connection.close)
        count = connection.execute(
            "SELECT COUNT(*) FROM documents").fetchone()[0]
        self.assertEqual(count, 1)

    def test_read_returns_the_exact_bytes_with_private_headers(self):
        digest = self._upload().json()["document_digest"]
        response = self.client.get(f"/v1/documents/{digest}",
                                   headers=self._headers())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, self.pdf)
        self.assertEqual(response.headers["content-type"],
                         "application/pdf")
        self.assertEqual(response.headers["cache-control"],
                         "private, no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_unknown_foreign_and_malformed_digests_are_indistinguishable(self):
        digest = self._upload().json()["document_digest"]
        unknown = self.client.get("/v1/documents/" + "f" * 64,
                                  headers=self._headers())
        foreign = self.client.get(f"/v1/documents/{digest}",
                                  headers=self._headers("tok-b"))
        malformed = self.client.get("/v1/documents/not-a-digest",
                                    headers=self._headers())
        for response in (unknown, foreign, malformed):
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(response.json(),
                             unknown.json(), "denials must be identical")
        self.assertEqual(unknown.json()["error"]["code"],
                         "result_not_registered")
        # The stored bytes are untouched by any denial.
        self.assertEqual(
            self.client.get(f"/v1/documents/{digest}",
                            headers=self._headers()).status_code, 200)

    def test_reads_are_tenant_scoped_and_tenants_do_not_share_rows(self):
        digest = self._upload().json()["document_digest"]
        other_tenant = self._client(OTHER_TENANT)
        # Same owner, different active tenant: no visibility of tenant-beta's
        # row, and the denial is the ordinary 404.
        hidden = other_tenant.get(f"/v1/documents/{digest}",
                                  headers=self._headers())
        self.assertEqual(hidden.status_code, 404, hidden.text)
        self.assertEqual(hidden.json()["error"]["code"],
                         "result_not_registered")
        # The other tenant may upload the SAME content as its own row.
        stored = other_tenant.post("/v1/documents", headers=self._headers(),
                                   params={"filename": "work.pdf"},
                                   content=self.pdf)
        self.assertEqual(stored.status_code, 201, stored.text)
        self.assertTrue(stored.json()["stored"])
        self.assertEqual(other_tenant.get(
            f"/v1/documents/{digest}", headers=self._headers()).status_code,
            200)
        # Tenant-beta's row still resolves for its own tenant.
        self.assertEqual(
            self.client.get(f"/v1/documents/{digest}",
                            headers=self._headers()).status_code, 200)

    def test_no_anonymous_access_and_membership_is_required(self):
        digest = self._upload().json()["document_digest"]
        anonymous_get = self.client.get(f"/v1/documents/{digest}")
        self.assertEqual(anonymous_get.status_code, 401)
        self.assertEqual(anonymous_get.json()["error"]["code"],
                         "authentication_required")
        anonymous_post = self.client.post("/v1/documents",
                                          content=self.pdf)
        self.assertEqual(anonymous_post.status_code, 401)
        no_membership = TestClient(create_app(
            verify_token=TOKENS.get,
            membership_resolver=lambda subject: None,
            manual_document_provenance_ok=lambda row: True)).post(
            "/v1/documents", headers=self._headers(), content=self.pdf)
        self.assertEqual(no_membership.status_code, 403)
        self.assertEqual(no_membership.json()["error"]["code"],
                         "tenant_membership_required")
        # Nothing was written by the denied uploads (only the seeded row).
        self.assertEqual(self._rows(), 1)

    def _rows(self):
        if not os.path.exists(self.db):
            return 0
        connection = document_store.open_store(self.db)
        self.addCleanup(connection.close)
        return connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def test_invalid_documents_are_refused_before_storage(self):
        cases = [(b"", "empty_document", 400),
                 (b"not a pdf at all", "pdf_signature_missing", 400),
                 (self.pdf, "unsupported_type", 400)]
        for data, code, status in cases:
            with self.subTest(code=code):
                filename = "work.pdf" if data else "work.pdf"
                if code == "unsupported_type":
                    filename = "work.txt"
                response = self._upload(data=data, filename=filename)
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(response.json()["error"]["code"], code)
        # Nothing reached the store: a later valid upload is still #1 stored.
        self.assertEqual(self._upload().json()["stored"], True)

    def test_oversized_body_is_refused_by_the_middleware(self):
        original = document_limits.MAX_DOCUMENT_BYTES
        document_limits.MAX_DOCUMENT_BYTES = 1024
        self.addCleanup(setattr, document_limits, "MAX_DOCUMENT_BYTES",
                        original)
        response = self._upload(data=b"%PDF-1.4" + b"\x00" * 4096)
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(response.json()["error"]["code"], "payload_invalid")
        self.assertEqual(self._rows(), 0)

    def test_unconfigured_store_fails_closed_for_write_and_read(self):
        digest = hashlib.sha256(self.pdf).hexdigest()
        os.environ.pop(document_store.PATH_ENV, None)
        denied = self._upload()
        self.assertEqual(denied.status_code, 503, denied.text)
        self.assertEqual(denied.json()["error"]["code"],
                         "dev_opt_in_required")
        read = self.client.get(f"/v1/documents/{digest}",
                               headers=self._headers())
        self.assertEqual(read.status_code, 503, read.text)
        self.assertEqual(read.json()["error"]["code"], "dev_opt_in_required")
        self.assertFalse(os.path.exists(self.db))


if __name__ == "__main__":
    unittest.main()
