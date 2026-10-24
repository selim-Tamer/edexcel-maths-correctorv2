"""C6-B6 independent attacks: tenant/owner isolation of api/app.py.

CM's OwnerScopeTests prove the honest-provider case (foreign owner sees
404). These attacks assume the provider is BROKEN or adversarial and pin
the API's own defense-in-depth — because ``rows_for`` is a trusted seam
whose implementation (DB tenant scoping, membership) does not exist yet:

  - provider leak: handing another owner's rows to the wrong subject must
    still yield nothing — the API re-checks ``report_owner`` against the
    server-derived owner and the store is owner-scoped anyway;
  - result-id collision across tenants: two teachers marked against the
    SAME uploaded document (same digest) and the same review key derive
    the SAME ``result_id``; their streams must stay owner-isolated —
    sequences, revisions, original records and reads never cross;
  - membership inactive: rows withdrawn after commits — decisions fail
    closed while the owner's own audit history remains readable (current
    behaviour pinned, with finding O-1 flagged for contract review:
    read-gating is provider-side until the tenant slice lands);
  - identity/path abuse: oversized subjects and result ids, traversal
    segments, swapped digests — every refusal is a deny code with zero
    mutation, never a crash that leaks internals;
  - cross-owner writes: nobody's stream revision advances as a side
    effect of somebody else's attempt.

Offline, in-process, synthetic temp SQLite only.
"""
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audit_store
import decision_core
import membership_store
from api.app import create_app
from fastapi.testclient import TestClient

TOKENS = {"tok-alice": "alice-subject", "tok-bob": "bob-subject"}
ALICE = "google:alice-subject"
BOB = "google:bob-subject"
PDF = b"%PDF-1.7 c6b6 tenant isolation fixture"
DIGEST = hashlib.sha256(PDF).hexdigest()
STUDENT_CANARY = "SENTINEL_student_answer_c6b6_7f3a"


def _active_membership(subject):
    # Explicit ACTIVE membership fixture (real membership_store.Membership),
    # never a bypass: the membership layer itself is attacked in
    # test_c6b6_oidc_attacks.py. The tenant/owner isolation attacks below only
    # need each subject to pass the now-present membership gate so they keep
    # pinning owner scoping, not the membership layer.
    return membership_store.Membership(subject, "tenant-c6b6", "teacher")


def question(number="1", awarded=1):
    return {"question_number": number, "awarded_marks": awarded,
            "max_marks": 2, "student_answer": STUDENT_CANARY,
            "working_summary": STUDENT_CANARY + " working",
            "scheme_answer_evidence": "s", "full_mark_solution": "42",
            "mark_breakdown": [], "confidence": "High",
            "loss_reason": "", "correct_method": "m"}


def manual_row(owner, review_key="review-1", digest=DIGEST, numbers=("1",)):
    questions = [question(n) for n in numbers]
    return {"status": "Marked", "review_key": review_key,
            "report_owner": owner, "snapshot_context": digest,
            "revision": "", "selected_questions": list(numbers),
            "ai_result": {"questions": [dict(q) for q in questions]},
            "result": {"questions": [dict(q) for q in questions]},
            "student_name": "Synthetic", "marking_model": "fake",
            "audit_model": "Not run"}


def identity_of(row):
    return decision_core.durable_result_identity(row, "")


def decision_body(question_number="1", **overrides):
    body = {"question_number": question_number, "action": "approve",
            "expected_revision": 0, "idempotency_key": "k-1"}
    body.update(overrides)
    return body


class TenantIsolationAttacks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store_path = str(Path(self.tmp.name) / "tenant.db")
        self.store = audit_store.open_store(self.store_path,
                                            dev_only_ack=True)
        self.addCleanup(self.store.close)
        # Provider is honest by default; individual attacks corrupt it.
        self.rows = {}

        self.app = create_app(
            verify_token=TOKENS.get,
            rows_for=lambda subject, owner: list(self.rows.get(owner, [])),
            store_provider=lambda: (self.store, None),
            manual_document_provenance_ok=lambda row: True,
            membership_resolver=_active_membership)
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def auth(self, who="alice"):
        return {"Authorization": f"Bearer tok-{who}"}

    def store_bytes(self):
        blob = b""
        for name in sorted(Path(self.tmp.name).iterdir()):
            blob += name.read_bytes()
        return blob

    # -- provider-leak defense -----------------------------------------

    def test_leaked_foreign_rows_still_write_nothing_for_the_wrong_owner(self):
        alice_row = manual_row(ALICE)
        rid = identity_of(alice_row)
        # Provider BUG: bob's listing returns alice's row object.
        self.rows[BOB] = [alice_row]
        stolen = self.client.post(
            f"/v1/results/{rid}/decisions", headers=self.auth("bob"),
            json=decision_body())
        self.assertEqual(stolen.status_code, 404, stolen.text)
        self.assertEqual(stolen.json()["error"]["code"],
                         "result_not_registered")
        self.assertIsNone(self.store.current_revision(
            owner=BOB, result_id=rid))
        self.assertIsNone(self.store.current_revision(
            owner=ALICE, result_id=rid))
        # The leak also cannot READ alice's data through bob's scope.
        events = self.client.get(f"/v1/results/{rid}/events",
                                 headers=self.auth("bob"))
        self.assertEqual(events.status_code, 404)
        # Even the row CONTENT never crosses the seam: bob's error envelope
        # carries no review key, no digest, no student canary.
        self.assertNotIn(STUDENT_CANARY, events.text)
        self.assertNotIn("review-1", stolen.text)

    def test_two_owners_same_result_id_get_fully_isolated_streams(self):
        # Same document digest + same review_key => IDENTICAL result_id for
        # both tenants. The store must key every read/write by owner too.
        alice_row = manual_row(ALICE)
        bob_row = manual_row(BOB)          # same digest, same review key
        rid = identity_of(alice_row)
        self.assertEqual(rid, identity_of(bob_row))
        self.rows[ALICE] = [alice_row]
        self.rows[BOB] = [bob_row]

        a1 = self.client.post(f"/v1/results/{rid}/decisions",
                              headers=self.auth("alice"),
                              json=decision_body(action="correct",
                                                 new_score="2",
                                                 reason="alice correction"))
        self.assertEqual(a1.status_code, 200, a1.text)
        b1 = self.client.post(f"/v1/results/{rid}/decisions",
                              headers=self.auth("bob"),
                              json=decision_body())
        self.assertEqual(b1.status_code, 200, b1.text)

        alice_events = self.store.events(owner=ALICE, result_id=rid)
        bob_events = self.store.events(owner=BOB, result_id=rid)
        self.assertEqual(len(alice_events), 1)
        self.assertEqual(len(bob_events), 1)
        self.assertEqual(alice_events[0]["actor"], ALICE)
        self.assertEqual(bob_events[0]["actor"], BOB)
        self.assertEqual(alice_events[0]["review_status"], "corrected")
        self.assertEqual(bob_events[0]["review_status"], "approved")
        # Revisions are per owner: bob's approve landed on bob's stream at
        # revision 1; alice's stream is untouched at revision 1 with her own
        # correction (bob could NOT clobber alice's revision or marks).
        self.assertEqual(self.store.current_revision(owner=ALICE,
                                                     result_id=rid), 1)
        self.assertEqual(self.store.current_revision(owner=BOB,
                                                     result_id=rid), 1)
        read_alice = self.client.get(f"/v1/results/{rid}/events",
                                     headers=self.auth("alice")).json()
        self.assertEqual(len(read_alice["events"]), 1)
        self.assertEqual(read_alice["events"][0]["actor"], ALICE)

    # -- membership inactive --------------------------------------------

    def test_membership_inactive_rows_refuse_writes_and_history_stays_scoped(
            self):
        # Finding O-1 (resolved by PR #55): with the row withdrawn, the API
        # now denies BOTH writes and reads with the same indistinguishable 404
        # result_not_registered — the shared visible_row() resolver gates
        # history reads too, so a withdrawn row's audit stream is not
        # reachable over HTTP (pinned here as the reviewed decision, never an
        # accidental regression).
        row = manual_row(ALICE)
        rid = identity_of(row)
        self.rows[ALICE] = [row]
        committed = self.client.post(f"/v1/results/{rid}/decisions",
                                     headers=self.auth("alice"),
                                     json=decision_body())
        self.assertEqual(committed.status_code, 200)
        # Membership gone: the provider now lists nothing for alice.
        self.rows[ALICE] = []
        late_write = self.client.post(
            f"/v1/results/{rid}/decisions", headers=self.auth("alice"),
            json=decision_body(question_number="2"))
        self.assertEqual(late_write.status_code, 404)
        self.assertEqual(late_write.json()["error"]["code"],
                         "result_not_registered")
        self.assertEqual(self.store.current_revision(owner=ALICE,
                                                     result_id=rid), 1)
        late_read = self.client.get(f"/v1/results/{rid}/events",
                                    headers=self.auth("alice"))
        self.assertEqual(late_read.status_code, 404, late_read.text)
        self.assertEqual(late_read.json()["error"]["code"],
                         "result_not_registered")
        # Other tenants still get nothing at all.
        foreign = self.client.get(f"/v1/results/{rid}/events",
                                  headers=self.auth("bob"))
        self.assertEqual(foreign.status_code, 404)

    # -- identity and path abuse -----------------------------------------

    def test_oversized_subject_and_result_id_are_denied_without_mutation(self):
        # Store identity limits (512) must surface as contract deny codes,
        # not crashes, and must never half-write.
        row = manual_row(ALICE)
        rid = identity_of(row)

        def big_subject(token):
            return "g" * 600

        big_owner = "google:" + "g" * 600

        # Honest provider (no rows for an unknown tenant): with the O-1
        # decision, BOTH routes are denied at the row gate with the same
        # indistinguishable 404 result_not_registered BEFORE the store is
        # ever touched - the oversized owner is just one more owner with no
        # visible rows (leak nothing), not a 400 about store internals.
        app = create_app(verify_token=big_subject,
                         rows_for=lambda s, o: [],
                         store_provider=lambda: (self.store, None),
                         manual_document_provenance_ok=lambda r: True,
                         membership_resolver=_active_membership)
        client = TestClient(app, raise_server_exceptions=False)
        headers = {"Authorization": "Bearer big"}
        events = client.get(f"/v1/results/{rid}/events", headers=headers)
        self.assertEqual(events.status_code, 404)
        self.assertEqual(events.json()["error"]["code"],
                         "result_not_registered")
        write = client.post(f"/v1/results/{rid}/decisions", headers=headers,
                            json=decision_body())
        self.assertEqual(write.status_code, 404)
        self.assertEqual(write.json()["error"]["code"],
                         "result_not_registered")

        # Leaked-provider provider (the row IS handed over): the store's
        # identity guard still refuses at registration time -- a deny code,
        # not a crash, and nothing at all lands in either table.
        leaky = create_app(
            verify_token=big_subject,
            rows_for=lambda s, o: ([dict(row, report_owner=big_owner)]
                                   if o == big_owner else []),
            store_provider=lambda: (self.store, None),
            manual_document_provenance_ok=lambda r: True,
            membership_resolver=_active_membership)
        leaky_client = TestClient(leaky, raise_server_exceptions=False)
        leaked_write = leaky_client.post(
            f"/v1/results/{rid}/decisions", headers=headers,
            json=decision_body())
        self.assertEqual(leaked_write.status_code, 400)
        self.assertEqual(leaked_write.json()["error"]["code"],
                         "owner_too_long")
        import sqlite3
        connection = sqlite3.connect(self.store_path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM audit_events")
                .fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM results")
                .fetchone()[0], 0)
        finally:
            connection.close()

        # An oversized result_id is now first a ROW-GATE miss (O-1): it is
        # indistinguishable from any nonexistent result - 404, leak nothing.
        # The store's own result_id_too_long guard stays real behind the gate
        # (owner-side enforcement is still pinned via the leaked-provider
        # registration above); it is the audit store's suite, not the API,
        # that owns that guard now.
        long_rid = "manual:" + ("m" * 600)
        oversized = self.client.get(f"/v1/results/{long_rid}/events",
                                    headers=self.auth("alice"))
        self.assertEqual(oversized.status_code, 404)
        self.assertEqual(oversized.json()["error"]["code"],
                         "result_not_registered")

    def test_traversal_and_encoded_segments_cannot_escape_the_route(self):
        # result_id is a single path segment; traversal-looking ids must
        # resolve to contract denials, and a segment that would change the
        # PATH (contains '/') must land on no route at all -- the framework
        # 404, never an error containing store internals.
        for sneaky in ("../../etc/passwd", "..%2f..%2fetc", "a/b",
                       '"\'<>;', "manual:%s:../../../"):
            with self.subTest(result_id=sneaky):
                read = self.client.get(
                    f"/v1/results/{sneaky}/events", headers=self.auth("alice"))
                self.assertIn(read.status_code, (400, 404))
                write = self.client.post(
                    f"/v1/results/{sneaky}/decisions",
                    headers=self.auth("alice"), json=decision_body())
                self.assertIn(write.status_code, (400, 404))
        self.assertIsNone(self.store.current_revision(
            owner=ALICE, result_id="../../etc/passwd"))

    def test_swapped_document_digest_does_not_reach_the_registered_stream(self):
        # An attacker (or buggy provider) that keeps the review key but
        # swaps the server-derived document digest produces a DIFFERENT
        # identity: the previously registered stream stays unreachable via
        # the new row, and the old result_id no longer resolves.
        alice_row = manual_row(ALICE)
        rid = identity_of(alice_row)
        self.rows[ALICE] = [alice_row]
        self.assertEqual(self.client.post(
            f"/v1/results/{rid}/decisions", headers=self.auth("alice"),
            json=decision_body()).status_code, 200)

        swapped = manual_row(ALICE, digest=hashlib.sha256(
            b"someone-else-scheme").hexdigest())
        self.rows[ALICE] = [swapped]
        old = self.client.post(f"/v1/results/{rid}/decisions",
                               headers=self.auth("alice"),
                               json=decision_body(expected_revision=1))
        self.assertEqual(old.status_code, 404)
        # And the swapped digest is a legitimately different stream: it can
        # register its OWN history without touching the first.
        new_rid = identity_of(swapped)
        self.assertNotEqual(new_rid, rid)
        first = self.client.post(f"/v1/results/{new_rid}/decisions",
                                 headers=self.auth("alice"),
                                 json=decision_body())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(self.store.events(owner=ALICE, result_id=rid)), 1)
        self.assertEqual(len(self.store.events(owner=ALICE,
                                               result_id=new_rid)), 1)

    def test_store_bytes_never_carry_student_content_across_owners(self):
        # Privacy of the durable substrate: register_result keeps a
        # marks-only projection; decisions keep marks/binding/reason. The
        # student answer canary must be absent from every byte on disk,
        # under any number of owner streams and commits.
        alice_row = manual_row(ALICE)
        bob_row = manual_row(BOB, review_key="bob-2", numbers=("1",))
        self.rows[ALICE] = [alice_row]
        self.rows[BOB] = [bob_row]
        for headers, row in ((self.auth("alice"), alice_row),
                             (self.auth("bob"), bob_row)):
            response = self.client.post(
                f"/v1/results/{identity_of(row)}/decisions", headers=headers,
                json=decision_body(action="correct", new_score="2",
                                   reason="teacher note"))
            self.assertEqual(response.status_code, 200, response.text)
        blob = self.store_bytes()
        self.assertNotIn(STUDENT_CANARY.encode(), blob)
        self.assertNotIn("working".encode() + b" " +
                         STUDENT_CANARY.encode(), blob)
        # The immutable originals are marks-only projections.
        original = self.store.original_record(
            owner=ALICE, result_id=identity_of(alice_row))
        self.assertEqual(original["original_scores"], [["1", 1]])


if __name__ == "__main__":
    unittest.main()
