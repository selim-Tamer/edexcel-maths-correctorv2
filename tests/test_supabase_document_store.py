"""Private Supabase Storage adapter tests (offline; no network, no bucket).

The PostgreSQL half of the contract (0004 grants, the separate elevated
purge role) lives in ``tests/test_postgres_documents_contract.py`` and runs in
the CI ``postgres-tests`` job. This file pins the adapter's behaviour with a
scripted transport and a scripted metadata connection, so a regression in key
derivation, idempotency, fail-closed handling or credential hygiene cannot
hide behind "no bucket available here".

Explicitly NOT tested here and NOT claimed anywhere: a real Supabase Storage
round trip. The bucket, its service key and the operator roles come from the
reviewed setup packet.
"""
import contextlib
import os
import unittest
from unittest import mock

import document_store
import supabase_document_store
from document_store import DocumentStoreError
from supabase_document_store import (MAX_DOCUMENT_BYTES, storage_key,
                                     SupabaseDocumentStore)
from api.app import _document_api

DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64
URL = "https://dbx.supabase.co"
KEY = "service-key-value"
BUCKET = "marker-pdfs"


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConnection:
    """Scripted metadata connection: answers queued rows, records statements."""

    def __init__(self, answers=(), fail_on=None):
        self.answers = list(answers)
        self.statements = []
        self.fail_on = fail_on or ()
        self.bound = []

    @contextlib.contextmanager
    def transaction(self):
        yield self

    def execute(self, statement, params=None):
        self.statements.append((statement.strip().split()[0].upper(), statement,
                                params))
        if any(fragment in statement for fragment in self.fail_on):
            raise RuntimeError("scripted metadata failure")
        if statement.strip().upper().startswith("SELECT SET_CONFIG"):
            self.bound.append(params[1])
            return _FakeResult(None)
        if not self.answers:
            return _FakeResult(None)
        return _FakeResult(self.answers.pop(0))

    def close(self):
        pass


class _RecordingTransport:
    def __init__(self, status=200, body=b"", on=None):
        self.calls = []
        self.status = status
        self.body = body
        self.on = on or {}

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers), "body": body})
        return self.on.get(method, self.status), self.body


def make_store(transport, connection=None):
    return SupabaseDocumentStore(
        url=URL, service_key=KEY, bucket=BUCKET,
        connection=connection or _FakeConnection(),
        transport=transport)


class StorageKeyTests(unittest.TestCase):
    def test_keys_are_deterministic_and_carry_no_identity(self):
        key = storage_key("school-a", "google:teacher-a", DIGEST)
        self.assertEqual(key, storage_key("school-a", "google:teacher-a",
                                          DIGEST))
        self.assertNotIn("school", key)
        self.assertNotIn("teacher", key)
        self.assertNotIn("google", key)
        self.assertTrue(key.endswith(DIGEST + ".pdf"))
        # Different tenants or owners never collide on the same object.
        self.assertNotEqual(key, storage_key("school-b", "google:teacher-a",
                                             DIGEST))
        self.assertNotEqual(key, storage_key("school-a", "google:teacher-b",
                                             DIGEST))

    def test_the_service_key_is_never_in_a_url_or_a_returned_value(self):
        transport = _RecordingTransport()
        store = make_store(transport, _FakeConnection(answers=[None]))
        supabase_document_store.put_document(
            store, tenant_id="school-a", owner="o", document_digest=DIGEST,
            filename="x.pdf", page_count=1, content=b"%PDF-1.4")
        for call in transport.calls:
            self.assertNotIn(KEY, call["url"])
            self.assertEqual(call["headers"]["Authorization"],
                             f"Bearer {KEY}")


class ConfigurationTests(unittest.TestCase):
    def test_configured_needs_url_key_and_bucket(self):
        for missing in ("AI_MARKER_SUPABASE_URL",
                        "AI_MARKER_SUPABASE_SERVICE_KEY",
                        "AI_MARKER_STORAGE_BUCKET"):
            env = {"AI_MARKER_SUPABASE_URL": URL,
                   "AI_MARKER_SUPABASE_SERVICE_KEY": KEY,
                   "AI_MARKER_STORAGE_BUCKET": BUCKET}
            env.pop(missing)
            with self.subTest(missing=missing), \
                    mock.patch.dict(os.environ, env, clear=True):
                self.assertFalse(supabase_document_store.configured())

    def test_unconfigured_store_refuses_to_open(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(DocumentStoreError) as caught:
                supabase_document_store.require_connection()
            self.assertEqual(caught.exception.code
                             if hasattr(caught.exception, "code")
                             else caught.exception.args[0],
                             "dev_opt_in_required")


class PutDocumentTests(unittest.TestCase):
    def test_put_uploads_then_records_the_metadata_row(self):
        transport = _RecordingTransport(status=201)
        connection = _FakeConnection(answers=[None, ("k",)])
        store = make_store(transport, connection)
        created = supabase_document_store.put_document(
            store, tenant_id="school-a", owner="o", document_digest=DIGEST,
            filename="paper.pdf", page_count=2, content=b"%PDF-1.4")
        self.assertTrue(created)
        self.assertEqual([c["method"] for c in transport.calls], ["PUT"])
        self.assertIn(storage_key("school-a", "o", DIGEST),
                      transport.calls[0]["url"])
        inserts = [s for s in connection.statements if s[0] == "INSERT"]
        self.assertEqual(len(inserts), 1)
        # No filename is stored: pdf_references keeps digest + key + size.
        self.assertNotIn("filename", inserts[0][1])
        self.assertEqual(inserts[0][2][3],
                         storage_key("school-a", "o", DIGEST))
        # The row write is conflict-safe, not a bare INSERT.
        self.assertIn("ON CONFLICT", inserts[0][1])
        # The tenant binding comes from the caller, transaction-locally.
        self.assertEqual(connection.bound, ["school-a"])

    def test_repeat_upload_is_idempotent_and_touches_nothing(self):
        transport = _RecordingTransport()
        connection = _FakeConnection(
            answers=[(storage_key("school-a", "o", DIGEST), 7)])
        store = make_store(transport, connection)
        created = supabase_document_store.put_document(
            store, tenant_id="school-a", owner="o", document_digest=DIGEST,
            filename="", page_count=None, content=b"1234567")
        self.assertFalse(created)
        self.assertEqual(transport.calls, [])

    def test_size_mismatch_on_an_existing_row_fails_closed(self):
        connection = _FakeConnection(
            answers=[(storage_key("school-a", "o", DIGEST), 7)])
        store = make_store(_RecordingTransport(), connection)
        with self.assertRaises(DocumentStoreError):
            supabase_document_store.put_document(
                store, tenant_id="school-a", owner="o", document_digest=DIGEST,
                filename="", page_count=None, content=b"12345678")

    def test_oversized_content_is_refused_before_any_upload(self):
        transport = _RecordingTransport()
        store = make_store(transport, _FakeConnection(answers=[None, ("k",)]))
        with self.assertRaises(DocumentStoreError) as caught:
            supabase_document_store.put_document(
                store, tenant_id="school-a", owner="o", document_digest=DIGEST,
                filename="", page_count=None, content=b"x" *
                (MAX_DOCUMENT_BYTES + 1))
        self.assertIn("document_too_large", str(caught.exception))
        self.assertEqual(transport.calls, [])

    def test_a_failed_metadata_write_never_deletes_the_object(self):
        # Compensation policy (Mr.G review): a failing row write must NOT
        # delete the object - a winner may be committing (or invisible) right
        # now, and deleting its bytes is unrecoverable. Orphan objects are
        # private garbage for the retention sweep.
        transport = _RecordingTransport(status=201)
        connection = _FakeConnection(answers=[None], fail_on=("INSERT INTO",))
        store = make_store(transport, connection)
        with self.assertRaises(DocumentStoreError):
            supabase_document_store.put_document(
                store, tenant_id="school-a", owner="o", document_digest=DIGEST,
                filename="", page_count=None, content=b"%PDF-1.4")
        self.assertEqual([c["method"] for c in transport.calls], ["PUT"],
                         "no compensation delete is ever allowed")

    def test_a_concurrent_identical_upload_is_benign_and_keeps_the_object(self):
        # IR finding: two concurrent uploads of identical bytes. The conflict
        # -safe write makes the loser a no-op (RETURNING yields no row), so it
        # reports "not created" and keeps the object the winner references.
        transport = _RecordingTransport(status=201)
        connection = _FakeConnection(answers=[None, None])
        store = make_store(transport, connection)
        created = supabase_document_store.put_document(
            store, tenant_id="school-a", owner="o", document_digest=DIGEST,
            filename="", page_count=None, content=b"1234567")
        self.assertFalse(created)
        self.assertEqual([c["method"] for c in transport.calls], ["PUT"],
                         "the winner's object must survive the loser's write")

    def test_failing_closed_on_a_size_mismatch_deletes_nothing(self):
        # Ownership-barrier rule (IR/Mr.G): failing closed must never destroy
        # referenced storage. A visible row whose size disagrees with the new
        # bytes means the store is inconsistent - we refuse, and touch neither
        # the object nor the row. (The invisible-race case is covered by the
        # benign-conflict test: the conflict-safe write yields no row and the
        # loser keeps everything.)
        transport = _RecordingTransport(status=201)
        connection = _FakeConnection(
            answers=[(storage_key("school-a", "o", DIGEST), 99)])
        store = make_store(transport, connection)
        with self.assertRaises(DocumentStoreError):
            supabase_document_store.put_document(
                store, tenant_id="school-a", owner="o", document_digest=DIGEST,
                filename="", page_count=None, content=b"1234567")
        self.assertEqual(transport.calls, [],
                         "no DELETE - and no rewrite - of referenced bytes")

    def test_a_tombstoned_document_is_revived_by_a_reupload(self):
        # After a purge the row is a tombstone (storage_key = ''). A new
        # upload of the same bytes must re-establish availability instead of
        # reporting "already present" for a document whose bytes are gone.
        transport = _RecordingTransport(status=201)
        connection = _FakeConnection(answers=[("", None), ("k",)])
        store = make_store(transport, connection)
        created = supabase_document_store.put_document(
            store, tenant_id="school-a", owner="o", document_digest=DIGEST,
            filename="", page_count=None, content=b"%PDF-1.4")
        self.assertTrue(created)
        self.assertEqual([c["method"] for c in transport.calls], ["PUT"])


class FindDocumentTests(unittest.TestCase):
    def test_unknown_or_foreign_digest_is_a_silent_miss(self):
        transport = _RecordingTransport()
        store = make_store(transport, _FakeConnection(answers=[None]))
        self.assertIsNone(supabase_document_store.find_document(
            store, tenant_id="school-a", owner="o", document_digest=DIGEST))
        self.assertEqual(transport.calls, [])  # no oracle, no object fetch

    def test_row_without_object_is_inconsistent_not_a_miss(self):
        transport = _RecordingTransport(status=404, body=b"")
        connection = _FakeConnection(answers=[("k",)])
        store = make_store(transport, connection)
        with self.assertRaises(DocumentStoreError):
            supabase_document_store.find_document(
                store, tenant_id="school-a", owner="o", document_digest=DIGEST)

    def test_known_digest_returns_the_bytes(self):
        transport = _RecordingTransport(status=200, body=b"%PDF-1.4")
        connection = _FakeConnection(
            answers=[(storage_key("school-a", "o", DIGEST),)])
        store = make_store(transport, connection)
        self.assertEqual(supabase_document_store.find_document(
            store, tenant_id="school-a", owner="o", document_digest=DIGEST),
            b"%PDF-1.4")


class DeleteDocumentTests(unittest.TestCase):
    """The purge verb: elevated role only, tenant-bound, idempotent."""

    def test_purge_removes_the_object_and_tombstones_the_row(self):
        transport = _RecordingTransport(status=200)
        connection = _FakeConnection(
            answers=[(storage_key("school-a", "o", DIGEST),)])
        store = make_store(transport, connection)
        removed = supabase_document_store.delete_document(
            store, tenant_id="school-a", owner="o", document_digest=DIGEST)
        self.assertTrue(removed)
        self.assertEqual([c["method"] for c in transport.calls], ["DELETE"])
        # The row is tombstoned, never deleted: the digest stays for audit.
        updates = [s for s in connection.statements if s[0] == "UPDATE"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(
            [s for s in connection.statements if s[0] == "DELETE"], [],
            "purge must not delete the metadata row")

    def test_the_tombstone_commits_before_the_object_delete_starts(self):
        # IR's stronger ordering (finding C): row locks are never held
        # across the Storage network call. Proof: when the object DELETE is
        # issued, the tombstone UPDATE is already in the statement log. A
        # crash from that point on leaves tombstone + orphan, which the
        # sweep reclaims and put_document's tombstone-revive accepts.
        connection = _FakeConnection(
            answers=[(storage_key("school-a", "o", DIGEST),)])
        seen = {}

        def transport(method, url, headers, body):
            seen["statements_at_delete"] = list(connection.statements)
            return 200, b""

        store = make_store(transport, connection)
        removed = supabase_document_store.delete_document(
            store, tenant_id="school-a", owner="o", document_digest=DIGEST)
        self.assertTrue(removed)
        verbs = [s[0] for s in seen["statements_at_delete"]]
        self.assertIn(
            "UPDATE", verbs,
            "the tombstone must be committed before the Storage call")

    def test_the_object_delete_targets_the_recomputed_key(self):
        # The post-commit DELETE never trusts a stored key: it is recomputed
        # from (tenant, owner, digest), so a corrupted row value cannot
        # redirect the purge at another object and no row read is needed.
        connection = _FakeConnection(answers=[("objects/wrong-key",)])
        seen = {}

        def transport(method, url, headers, body):
            seen["url"] = url
            return 204, b""

        store = make_store(transport, connection)
        self.assertTrue(supabase_document_store.delete_document(
            store, tenant_id="school-a", owner="o", document_digest=DIGEST))
        self.assertTrue(
            seen["url"].endswith(storage_key("school-a", "o", DIGEST)),
            "the DELETE must use the deterministically recomputed key")

    def test_a_storage_failure_after_commit_is_sweep_cleanable(self):
        # IR ruling: after the tombstone commits, a Storage failure (network,
        # 5xx) must NOT surface as an error - the purge's authoritative
        # effect already happened; the orphan object is the sweep's to
        # reclaim and a re-upload revives the tombstone.
        def failing(method, url, headers, body):
            raise DocumentStoreError("store_unusable")

        connection = _FakeConnection(
            answers=[(storage_key("school-a", "o", DIGEST),)])
        store = make_store(failing, connection)
        removed = supabase_document_store.delete_document(
            store, tenant_id="school-a", owner="o", document_digest=DIGEST)
        self.assertTrue(removed,
                        "the committed tombstone is the authoritative effect")
        updates = [s for s in connection.statements if s[0] == "UPDATE"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(
            [s for s in connection.statements if s[0] == "DELETE"], [],
            "purge must not delete the metadata row")

    def test_any_terminal_storage_status_still_counts_as_tombstoned(self):
        # 200/204 removed the object; 404 counts as already-deleted; a 5xx
        # leaves a sweep-cleanable orphan. In every case the committed
        # tombstone is what the call reports.
        for status in (200, 204, 404, 500):
            with self.subTest(status=status):
                connection = _FakeConnection(
                    answers=[(storage_key("school-a", "o", DIGEST),)])
                store = make_store(_RecordingTransport(status=status),
                                   connection)
                self.assertTrue(supabase_document_store.delete_document(
                    store, tenant_id="school-a", owner="o",
                    document_digest=DIGEST))

    def test_purge_is_idempotent(self):
        transport = _RecordingTransport()
        # storage_key '' means "already purged": no object call, no rewrite.
        store = make_store(transport, _FakeConnection(answers=[("",)]))
        self.assertFalse(supabase_document_store.delete_document(
            store, tenant_id="school-a", owner="o", document_digest=DIGEST))
        self.assertEqual(transport.calls, [])

    def test_purge_of_an_unknown_digest_is_a_no_op(self):
        transport = _RecordingTransport()
        store = make_store(transport, _FakeConnection(answers=[None]))
        self.assertFalse(supabase_document_store.delete_document(
            store, tenant_id="school-a", owner="o", document_digest=DIGEST))
        self.assertEqual(transport.calls, [])

    def test_a_tombstoned_document_reads_as_a_miss(self):
        transport = _RecordingTransport(status=404)
        store = make_store(transport, _FakeConnection(answers=[("",)]))
        self.assertIsNone(supabase_document_store.find_document(
            store, tenant_id="school-a", owner="o", document_digest=DIGEST))
        self.assertEqual(transport.calls, [])


class FacadeTests(unittest.TestCase):
    def test_bucket_composition_selects_the_private_adapter(self):
        env = {"AI_MARKER_SUPABASE_URL": URL,
               "AI_MARKER_SUPABASE_SERVICE_KEY": KEY,
               "AI_MARKER_STORAGE_BUCKET": BUCKET}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIs(_document_api(), supabase_document_store)

    def test_without_it_the_dev_store_stays(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIs(_document_api(), document_store)


if __name__ == "__main__":
    unittest.main()
