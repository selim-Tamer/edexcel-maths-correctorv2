"""Offline contract tests for the SQLite development assignment store.

The PostgreSQL adapter has the same shape and is exercised in CI
postgres-tests. These tests pin the dev store's replay/mismatch semantics,
including two connections racing the same idempotency key: a unique conflict
must be a replay or an explicit mismatch, never ``store_unusable``.
"""
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from assignment_store import (AssignmentStoreError, create_assignment,
                              open_store)


class SqliteAssignmentStoreTests(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(handle)
        self.addCleanup(os.unlink, self.path)
        self.addCleanup(lambda: os.path.exists(self.path) and None)

    def _create(self, key="key-00000001", title="Paper 1"):
        connection = open_store(self.path)
        try:
            return create_assignment(
                connection, tenant_id="tenant-a", owner="owner-a",
                idempotency_key=key, title=title)
        finally:
            connection.close()

    def test_replay_returns_the_same_server_minted_id(self):
        first = self._create()
        second = self._create()
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["assignment_id"], second["assignment_id"])
        self.assertRegex(first["assignment_id"], r"^asg_[0-9a-f]{24}$")

    def test_same_key_different_title_is_a_mismatch(self):
        self._create(key="conflict-key", title="Paper 1")
        with self.assertRaises(AssignmentStoreError) as caught:
            self._create(key="conflict-key", title="Paper 2")
        self.assertEqual("idempotency_key_payload_mismatch",
                         caught.exception.detail)

    def test_two_connections_racing_same_key_replay_without_store_error(self):
        barrier = Barrier(2)

        def attempt():
            connection = open_store(self.path)
            try:
                barrier.wait(timeout=10)
                return create_assignment(
                    connection, tenant_id="tenant-a", owner="owner-a",
                    idempotency_key="race-key-0001", title="Raced")
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt) for _ in range(2)]
            outcomes = [future.result(timeout=20) for future in futures]

        self.assertEqual(1, sum(1 for item in outcomes if item["created"]))
        self.assertEqual(1, len({item["assignment_id"] for item in outcomes}))


if __name__ == "__main__":
    unittest.main()
