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
import os
import sys
import tempfile
import unittest
import unittest.mock as mock
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
        self.assertEqual(fresh_attempt.status_code, 409, fresh_attempt.text)
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
        self.assertEqual(replay_attempt.status_code, 409)
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

    # -- REAL ingest/registration and job endpoints (post-engine slice) ----

    def test_client_ingest_registration_and_jobs_fail_closed_and_cannot_cross(self):
        # REPLACES the old ``no_ingestion_or_job_endpoints_exist_yet`` pin:
        # that absence was true at b782528 but is FALSE since the
        # engine/results slice merged (POST /v1/results and the /v1/jobs
        # routes exist). The equally strong attack expectations, against the
        # REAL endpoints over HTTP, are:
        #   a. a client-supplied identity+original NEVER registers without
        #      the server-issued provenance authorization (fail closed);
        #   b. forged identity shapes are 400 refused before any gate;
        #   c. a silent SWAP of the pristine output for an identity that is
        #      already registered is refused 409 original_output_immutable
        #      through the REAL registration path (not just the decision
        #      path), leaving the registered original and every prior event
        #      intact;
        #   d. an identical identity ingested by ANOTHER verified teacher
        #      cannot cross into (nor rewrite) the first teacher's stream;
        #   e. job creation is owner+tenant-AND-document-scoped: an unknown
        #      document is an indistinguishable 404, reusing one key for a
        #      different stored document is 409 idempotency_key_payload_
        #      mismatch, and the honest same-key same-document replay
        #      returns the SAME job id.
        # All storage backends are the dev opt-in stores this repo's own
        # job/document tests use (temp SQLite via env); the ONLY harness
        # seam is the trusted-provenance authorization, injected explicitly
        # per case with a disclosure in scope - never bypassed silently.
        SUBJECT_B = "bob-subject"
        OWNER_B = "google:" + SUBJECT_B
        self.assertNotIn(OWNER_B, ("google:" + SUBJECT, ),
                         "attack fixture sanity")
        import document_store
        import job_store
        import ingest_registry
        digest_hex = hashlib.sha256(PDF).hexdigest()
        attacker_pdf = PDF + b"-attacker-tag"
        attacker_digest = hashlib.sha256(attacker_pdf).hexdigest()
        env = {document_store.PATH_ENV: str(
                   Path(self.tmp.name) / "doc.sqlite3"),
               document_store.ACK_ENV: "1",
               job_store.PATH_ENV: str(
                   Path(self.tmp.name) / "jobs.sqlite3"),
               job_store.ACK_ENV: "1",
               ingest_registry.PATH_ENV: str(
                   Path(self.tmp.name) / "ingest.sqlite3"),
               ingest_registry.ACK_ENV: "1"}
        original = {"questions": [
            {"question_number": "1", "awarded_marks": 1, "max_marks": 2},
            {"question_number": "2", "awarded_marks": 0, "max_marks": 2}]}
        swapped = {"questions": [
            {"question_number": "1", "awarded_marks": 2, "max_marks": 2},
            {"question_number": "2", "awarded_marks": 0, "max_marks": 2}]}

        def ingest_body(digest, swapped_output=False):
            return {"scope": "manual",
                    "original_output": swapped if swapped_output
                    else original,
                    "manual_identity": {
                        "document_digest": digest,
                        "submission_ref": "server-ref-1"}}

        with mock.patch.dict(os.environ, env):
            documents = document_store.open_store(env[document_store.PATH_ENV])
            honest_job = document_store.put_document(
                documents, tenant_id="tenant-c6b6", owner=OWNER,
                document_digest=digest_hex, filename="work.pdf",
                page_count=1, content=PDF)
            self.assertTrue(honest_job, "harness sanity: document stored")
            # A second, legitimately-stored document for Alpha gives the
            # idempotency_key_payload_mismatch attack a real target.
            attacker_stored = document_store.put_document(
                documents, tenant_id="tenant-c6b6", owner=OWNER,
                document_digest=attacker_digest, filename="other.pdf",
                page_count=1, content=attacker_pdf)
            self.assertTrue(attacker_stored, "harness sanity")
            documents.commit()
            documents.close()

            def app_for(trust_ingest):
                return create_app(
                    verify_token={"tok": SUBJECT, "tok-b": SUBJECT_B}.get,
                    rows_for=lambda subject, owner: [r for r in self.rows
                                                     if r.get("report_owner")
                                                     == owner],
                    store_provider=lambda: (self.store, None),
                    manual_document_provenance_ok=lambda row: True,
                    membership_resolver=lambda subject:
                        membership_store.Membership(subject, "tenant-c6b6",
                                                    "teacher"),
                    authorize_ingest=(None if not trust_ingest
                                      else lambda s, t, r: True))

            default_client = TestClient(app_for(False),
                                        raise_server_exceptions=False)
            trusted_client = TestClient(app_for(True),
                                        raise_server_exceptions=False)

            # (a) no provenance -> fail closed, nothing registered.
            unauthorized = default_client.post(
                "/v1/results", headers=self.auth(),
                json=ingest_body(digest_hex))
            self.assertEqual(unauthorized.status_code, 403, unauthorized.text)
            self.assertEqual(unauthorized.json()["error"]["code"],
                             "manual_document_provenance_required")
            self.assertEqual(unauthorized.json()["error"]["detail"],
                             "ingest_authorization_required")

            # (b) forged identity shapes -> 400 before any gate/store.
            for forged in ("z" * 63, "GG" * 32, PDF.hex() + "00"):
                body = ingest_body(forged)
                raw = self.auth()
                forged_response = default_client.post(
                    "/v1/results", headers=raw,
                    json={"scope": "manual",
                          "original_output": original,
                          "manual_identity": {
                              "document_digest": forged,
                              "submission_ref": "server-ref-1"}})
                self.assertEqual(forged_response.status_code, 400,
                                 forged_response.text)
                self.assertEqual(forged_response.json()["error"]["code"],
                                 "payload_invalid")
                self.assertEqual(self.store.current_revision(
                    owner=OWNER, result_id="manual:" + forged[:16]
                    + ":server-ref-1"), None)
            slash_ref = {**ingest_body(digest_hex),
                         "manual_identity": {"document_digest": digest_hex,
                                             "submission_ref": "a/b"}}
            forged_ref = default_client.post(
                "/v1/results", headers=self.auth(), json=slash_ref)
            self.assertEqual(forged_ref.status_code, 400, forged_ref.text)

            # (c) authorized first ingest, then a silent output swap on the
            # SAME identity: the REAL registration path refuses 409 and the
            # registered original stays exactly what it was.
            first = trusted_client.post("/v1/results", headers=self.auth(),
                                        json=ingest_body(digest_hex))
            self.assertEqual(first.status_code, 201, first.text)
            self.assertTrue(first.json()["result_id"].startswith("manual:"))
            self.assertEqual(self.store.current_revision(
                owner=OWNER, result_id=first.json()["result_id"]), 0)
            swap = trusted_client.post(
                "/v1/results", headers=self.auth(),
                json=ingest_body(digest_hex, swapped_output=True))
            self.assertEqual(swap.status_code, 409, swap.text)
            self.assertEqual(swap.json()["error"]["code"],
                             "original_output_immutable")
            kept = self.store.original_record(
                owner=OWNER, result_id=first.json()["result_id"])
            self.assertEqual(kept["original_scores"],
                             [["1", 1], ["2", 0]])
            self.assertEqual(self.store.events(
                owner=OWNER, result_id=first.json()["result_id"]), [])

            # (d) another verified teacher, identical identity: the minted
            # result id is the same STRING, but it lives under Bob's owner
            # only - Alpha's original/revision/event stream is untouched.
            follower = trusted_client.post(
                "/v1/results",
                headers={"Authorization": "Bearer tok-b"},
                json=ingest_body(digest_hex, swapped_output=True))
            self.assertEqual(follower.status_code, 201, follower.text)
            self.assertEqual(
                self.store.current_revision(
                    owner=OWNER, result_id=first.json()["result_id"]), 0,
                "Bob's trusted ingest must not touch Alpha's stream")
            bob_record = self.store.original_record(
                owner=OWNER_B, result_id=follower.json()["result_id"])
            self.assertEqual(bob_record["original_scores"],
                             [["1", 2], ["2", 0]])

            # (e) jobs: unknown document is an indistinguishable 404 for
            # Bob (the digest is Alpha's), then the honest key replay and
            # the key-mismatch refusal for Alpha.
            other_teacher = default_client.post(
                "/v1/jobs", headers={"Authorization": "Bearer tok-b"},
                json={"idempotency_key": "job-key-0001",
                      "document_digest": digest_hex})
            self.assertEqual(other_teacher.status_code, 404,
                             other_teacher.text)
            self.assertEqual(other_teacher.json()["error"]["code"],
                             "result_not_registered")
            created = trusted_client.post(
                "/v1/jobs", headers=self.auth(),
                json={"idempotency_key": "job-key-0001",
                      "document_digest": digest_hex})
            self.assertEqual(created.status_code, 201, created.text)
            replay = trusted_client.post(
                "/v1/jobs", headers=self.auth(),
                json={"idempotency_key": "job-key-0001",
                      "document_digest": digest_hex})
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()["job_id"], created.json()["job_id"],
                             "same key + same document must replay one job")
            reuse = trusted_client.post(
                "/v1/jobs", headers=self.auth(),
                json={"idempotency_key": "job-key-0001",
                      "document_digest": attacker_digest})
            self.assertNotEqual(reuse.status_code, 201, reuse.text)
            self.assertEqual(reuse.json()["error"]["code"],
                             "idempotency_key_payload_mismatch")


if __name__ == "__main__":
    unittest.main()
