"""C6-B6 independent attacks: transaction, replay and marking-integrity.

CM's DecisionEndpointTests prove the ordinary commit/replay/stale paths.
These attacks target the ORDER and NORMALISATION of the guarantee itself,
plus fault injection through HTTP -- the ways a retry-driven client (job
retries, timeouts, duplicated deliveries) actually breaks things:

  - idempotency is evaluated BEFORE the optimistic revision check, so a
    late retry of an already-committed decision replays the PRIOR result
    even after the stream moved on -- and can never append a second event
    or clobber the newer revision;
  - type confusion cannot duplicate audits: new_score 2 (int) and "2"
    (str) are the same logical decision (the server derives the payload
    digest after normalisation); reason whitespace is the same too;
  - the CLIENT idempotency_key is only a correlation token: it cannot
    merge two different decisions into one event, and changing it cannot
    split one logical decision into two -- the server-derived
    owner+result+payload key alone decides replay;
  - mid-transaction faults (register begin, after event insert, before
    commit) roll the WHOLE transaction back over HTTP: 500 deny, zero
    events, zero revision movement, and the honest retry succeeds;
  - marking integrity under silent evidence swap: re-submitting the SAME
    decision against the same identity whose pristine AI output changed
    is refused as ``original_output_immutable``; the registered original
    (digest + marks projection) and every prior event stay intact -- an
    ingestion bug can neither rewrite history nor forge over it;
  - preserved original vs effective totals: events carry old_score and
    new_score per decision; the immutable original projection keeps the
    AI marks; effective marks are the caller-derived overlay, never a
    mutation of what is registered.

Offline, in-process, synthetic temp SQLite only; no network, no student
data, no real provider.
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

SUBJECT = "alice-subject"
OWNER = "google:" + SUBJECT
PDF = b"%PDF-1.7 c6b6 transaction attack fixture"
DIGEST = hashlib.sha256(PDF).hexdigest()
STUDENT_CANARY = "SENTINEL_student_answer_c6b6_transaction_5e1c"


def _active_membership(subject):
    # Explicit ACTIVE membership fixture (real membership_store.Membership),
    # never a bypass: the membership layer itself is attacked in
    # test_c6b6_oidc_attacks.py. These transaction/replay attacks only need a
    # subject to pass the now-present membership gate so they keep pinning the
    # store/transaction guarantees, not the membership layer.
    return membership_store.Membership(subject, "tenant-c6b6", "teacher")


def question(number, awarded=1):
    return {"question_number": number, "awarded_marks": awarded,
            "max_marks": 2, "student_answer": STUDENT_CANARY + number,
            "working_summary": "synthetic working " + number,
            "scheme_answer_evidence": "s", "full_mark_solution": "42",
            "mark_breakdown": [], "confidence": "High",
            "loss_reason": "", "correct_method": "m"}


def manual_row(ai_marks=(("1", 1), ("2", 1)), review_key="review-1"):
    questions = [question(n, a) for n, a in ai_marks]
    return {"status": "Marked", "review_key": review_key,
            "report_owner": OWNER, "snapshot_context": DIGEST,
            "revision": "", "selected_questions": [n for n, _ in ai_marks],
            "ai_result": {"questions": [dict(q) for q in questions]},
            "result": {"questions": [dict(q) for q in questions]},
            "student_name": "Synthetic", "marking_model": "fake",
            "audit_model": "Not run"}


def identity_of(row):
    return decision_core.durable_result_identity(row, "")


class TransactionAttackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store_path = str(Path(self.tmp.name) / "tx.db")
        self.store = audit_store.open_store(self.store_path,
                                            dev_only_ack=True)
        self.addCleanup(self.store.close)
        self.row = manual_row()
        self.rid = identity_of(self.row)
        self.rows = [self.row]
        app = create_app(
            verify_token={"tok": SUBJECT}.get,
            rows_for=lambda subject, owner: [
                r for r in self.rows if r.get("report_owner") == owner],
            store_provider=lambda: (self.store, None),
            manual_document_provenance_ok=lambda row: True,
            membership_resolver=_active_membership)
        self.client = TestClient(app, raise_server_exceptions=False)

    def auth(self):
        return {"Authorization": "Bearer tok"}

    def post(self, **body_overrides):
        body = {"question_number": "1", "action": "approve",
                "expected_revision": 0, "idempotency_key": "k"}
        body.update(body_overrides)
        return self.client.post(f"/v1/results/{self.rid}/decisions",
                                headers=self.auth(), json=body)

    def events(self):
        return self.store.events(owner=OWNER, result_id=self.rid)

    # -- replay precedence ------------------------------------------------

    def test_late_retry_replays_after_the_stream_moved_on(self):
        first = self.post(action="correct", new_score="2", reason="r1",
                          idempotency_key="c1")
        self.assertEqual(first.status_code, 200, first.text)
        newer = self.post(question_number="2", expected_revision=1,
                          idempotency_key="c2")
        self.assertEqual(newer.status_code, 200, newer.text)
        # Job-retry reality: the ORIGINAL request is delivered again, late,
        # with its stale expected_revision and a brand-new client key. It
        # must replay the prior commit (same sequence, same event id), not
        # append, not clobber and not fail on the revision.
        retry = self.post(action="correct", new_score="2", reason="r1",
                          idempotency_key="zzz-fresh-key",
                          expected_revision=0)
        self.assertEqual(retry.status_code, 200, retry.text)
        payload = retry.json()
        self.assertTrue(payload["replayed"])
        self.assertEqual(payload["event_sequence"],
                         first.json()["event_sequence"])
        self.assertEqual(payload["audit_reference"],
                         first.json()["audit_reference"])
        # Stream integrity: still exactly two events at revision 2.
        events = self.events()
        self.assertEqual([e["sequence"] for e in events], [1, 2])
        self.assertEqual(self.store.current_revision(
            owner=OWNER, result_id=self.rid), 2)

    def test_type_confusion_and_whitespace_cannot_duplicate_audits(self):
        a = self.post(action="correct", new_score=2, reason=" same ")
        b = self.post(action="correct", new_score="2", reason="same")
        self.assertEqual(a.status_code, 200, a.text)
        self.assertEqual(b.status_code, 200, b.text)
        self.assertFalse(a.json()["replayed"])
        self.assertTrue(b.json()["replayed"],
                        "int/str score and padded reason are the SAME "
                        "logical decision after server normalisation")
        self.assertEqual(len(self.events()), 1)

    def test_client_idempotency_key_can_neither_merge_nor_split(self):
        # Same logical decision, different client keys -> still ONE event
        # (client key cannot split a decision into duplicates)...
        one = self.post(action="correct", new_score="2", reason="x",
                        idempotency_key="key-A")
        two = self.post(action="correct", new_score="2", reason="x",
                        idempotency_key="key-B")
        self.assertEqual((one.status_code, two.status_code), (200, 200))
        self.assertTrue(two.json()["replayed"])
        self.assertEqual(len(self.events()), 1)
        # ...and two DIFFERENT decisions forced under ONE client key must
        # both commit (client key cannot merge distinct payloads and so
        # cannot suppress an audit event the second decision requires).
        other = self.post(question_number="2", action="correct",
                          new_score="2", reason="distinct payload",
                          expected_revision=1, idempotency_key="key-A")
        self.assertEqual(other.status_code, 200, other.text)
        self.assertFalse(other.json()["replayed"])
        self.assertEqual([e["sequence"] for e in self.events()], [1, 2])

    # -- fault injection through HTTP ------------------------------------

    def _faulting(self, stage):
        def hook(current_stage):
            if current_stage == stage:
                raise RuntimeError("injected fault at " + current_stage)
        return hook

    def test_fault_at_every_named_stage_rolls_back_completely_then_retry(self):
        for stage in ("begin", "after_event_insert", "before_commit"):
            with self.subTest(stage=stage):
                fresh = audit_store.open_store(
                    str(Path(self.tmp.name) / ("fault-%s.db" % stage)),
                    dev_only_ack=True)
                self.addCleanup(fresh.close)
                row = manual_row(review_key="fault-" + stage)
                rid = identity_of(row)
                app = create_app(
                    verify_token={"tok": SUBJECT}.get,
                    rows_for=lambda s, o: [row],
                    store_provider=lambda: (fresh, None),
                    manual_document_provenance_ok=lambda r: True,
                    membership_resolver=_active_membership)
                client = TestClient(app, raise_server_exceptions=False)
                body = {"question_number": "1", "action": "correct",
                        "new_score": "2", "reason": "fault " + stage,
                        "expected_revision": 0, "idempotency_key": "f"}
                fresh.set_fault_hook(self._faulting(stage))
                broken = client.post(f"/v1/results/{rid}/decisions",
                                     headers=self.auth(), json=body)
                self.assertEqual(broken.status_code, 500, broken.text)
                self.assertEqual(broken.json()["error"]["code"],
                                 "audit_persist_failed")
                # Rollback: either no registration at all, or a registered
                # stream at revision 0 -- but NEVER a half-commit, and the
                # faulted response must not echo the injected internals.
                self.assertNotIn("injected fault", broken.text)
                revision = fresh.current_revision(owner=OWNER,
                                                  result_id=rid)
                self.assertIn(revision, (None, 0))
                self.assertEqual(fresh.events(owner=OWNER, result_id=rid)
                                 if revision is not None else [], [])
                # The honest retry (fault cleared) commits normally.
                fresh.set_fault_hook(None)
                ok = client.post(f"/v1/results/{rid}/decisions",
                                 headers=self.auth(), json=body)
                self.assertEqual(ok.status_code, 200, ok.text)
                self.assertEqual(len(fresh.events(owner=OWNER,
                                                  result_id=rid)), 1)

    # -- marking integrity: the immutable original ------------------------

    def test_silently_swapped_ai_output_cannot_rewrite_or_forgery_replay(self):
        committed = self.post(action="correct", new_score="2",
                              reason="first truth")
        self.assertEqual(committed.status_code, 200, committed.text)
        original_before = self.store.original_record(
            owner=OWNER, result_id=self.rid)

        # The provider now yields the SAME identity (same digest+review_key)
        # but a tampered pristine AI output (marks silently raised).
        tampered = manual_row(ai_marks=(("1", 2), ("2", 2)))
        self.assertEqual(identity_of(tampered), self.rid)
        self.rows[:] = [tampered]

        # 1. A genuinely new decision under the tampered evidence: the
        #    store refuses to re-register a different original. The stream
        #    is NOT advanced and the registered original is NOT replaced.
        fresh_attempt = self.post(question_number="2", action="correct",
                                  new_score="2", reason="under tampering",
                                  expected_revision=1,
                                  idempotency_key="t1")
        self.assertEqual(fresh_attempt.status_code, 400, fresh_attempt.text)
        self.assertEqual(fresh_attempt.json()["error"]["code"],
                         "original_output_immutable")
        self.assertEqual(self.store.current_revision(
            owner=OWNER, result_id=self.rid), 1)

        # 2. Even the REPLAY of the original honest decision is now refused
        #    while the provider keeps lying about the evidence: the
        #    original check precedes any reply. Nothing can be committed
        #    against forged evidence -- history stays exactly as it was.
        replay_attempt = self.post(action="correct", new_score="2",
                                   reason="first truth")
        self.assertEqual(replay_attempt.status_code, 400)
        self.assertEqual(replay_attempt.json()["error"]["code"],
                         "original_output_immutable")

        after = self.store.original_record(owner=OWNER, result_id=self.rid)
        self.assertEqual(after["original_digest"],
                         original_before["original_digest"])
        self.assertEqual(after["original_scores"], [["1", 1], ["2", 1]])
        events = self.events()
        self.assertEqual([e["sequence"] for e in events], [1])
        self.assertEqual(events[0]["review_status"], "corrected")
        self.assertEqual(events[0]["old_score"], "1")
        self.assertEqual(events[0]["new_score"], "2")

    def test_original_marks_stay_the_immutable_baseline_of_effective_totals(
            self):
        # Two corrections; the original projection keeps the AI marks while
        # the events carry the effective per-question scores the totals are
        # computed from. This is the API-scope proof of "preserved original
        # vs effective totals".
        c1 = self.post(question_number="1", action="correct",
                       new_score="2", reason="M1 earned",
                       idempotency_key="o1")
        self.assertEqual(c1.status_code, 200, c1.text)
        c2 = self.post(question_number="2", action="correct",
                       new_score="0", reason="A1 missed",
                       expected_revision=1, idempotency_key="o2")
        self.assertEqual(c2.status_code, 200, c2.text)
        original = self.store.original_record(owner=OWNER,
                                              result_id=self.rid)
        self.assertEqual(original["original_scores"], [["1", 1], ["2", 1]])
        effective = {e["question_number"]: e["new_score"]
                     for e in self.events()}
        self.assertEqual(effective, {"1": "2", "2": "0"})
        # The AI original inside the row is untouched by the decision
        # plumbing (decision_core never mutates ai_result).
        self.assertEqual(
            [q["awarded_marks"] for q in self.row["ai_result"]["questions"]],
            [1, 1])

    # -- privacy of the durable substrate ---------------------------------

    def test_store_bytes_never_contain_student_content_or_tokens(self):
        response = self.post(action="correct", new_score="2",
                             reason="teacher note only")
        self.assertEqual(response.status_code, 200, response.text)
        blob = b""
        for name in Path(self.tmp.name).iterdir():
            if name.name.startswith("tx"):
                blob += name.read_bytes()
        self.assertIn(b"question_review_decision", blob,
                      "sanity: the store really holds the decision")
        self.assertNotIn(STUDENT_CANARY.encode(), blob)
        self.assertNotIn(b"Bearer", blob)

    # -- unimplemented surface is honestly absent --------------------------

    def test_no_ingestion_or_job_endpoints_exist_yet(self):
        # The beta brief mentions jobs/retries/timeouts. At the API level
        # only DECISION delivery duplication is implementable: ingestion
        # and job endpoints do not exist, and this test pins THAT (a real
        # 404/405) instead of pretending to test a job engine. It fails
        # the moment an ingestion route appears WITHOUT its own retry-
        # duplication attacks being added here.
        for method, path in (("GET", "/v1/results"),
                             ("POST", "/v1/results"),
                             ("GET", "/v1/jobs"),
                             ("POST", "/v1/jobs"),
                             ("GET", "/v1/results/%s/progress" % self.rid)):
            with self.subTest(method=method, path=path):
                response = getattr(self.client, method.lower())(
                    path, headers=self.auth())
                self.assertIn(response.status_code, (404, 405))


if __name__ == "__main__":
    unittest.main()
