"""C6-B6 synthetic 14-stage acceptance journey for the Oct 26 beta brief.

One ordered flow over the REAL components that exist, with every mocked
external service labelled, exactly as required ("label real vs mocked;
no inflated live claims; do not claim unsupported features pass"):

 stage  component                                          real or mocked
  1  auth (bearer -> subject)                          TEST-ONLY seam: the
     production OIDC/JWKS verifier is NOT implemented; the seam itself is
     attacked in test_c6b6_verifier_boundary.py. Mocked, labelled.
  2  dashboard (teacher sees own results)              NO list endpoint
     exists (pinned as absent); the owner-scoped row provider stands in.
     Mocked, labelled.
  3  assignment selection (mark scheme + questions)    REAL engine inputs
     from fixtures/synthetic_marking.json (question ids/maxima).
  4  authorized PDF upload + server-derived digest     digest computed
     from real bytes via hashlib in the provider; the grant store is a
     MOCKED provenance predicate (same shape as the app's grant).
  5  validated job / stream registration               REAL audit store:
     first read is 404 (nothing registered before the first accepted
     decision -- pinned).
  6  marking engine                                   REAL
     ``mark_one_submission`` headless with ``fake_client`` (model mocked;
     fixture-declared outcomes, not accuracy claims).
  7  progress                                         NOT IMPLEMENTED as
     an endpoint: pinned absent (404). Honest gap, labelled.
  8  marks + evidence presentation                      REAL row: ai
     marks, binding via the production ``question_review_binding``.
  9  teacher correction over HTTP                      REAL FastAPI app +
     REAL decision_core + REAL SQLite commit.
 10  original vs effective totals                      REAL engine
     functions: AI originals intact; override folds into effective marks.
 11  durable audit                                    REAL store reads:
     sequence/actor/binding/replay, decisions only.
 12  logout / token expiry                             verifier flips to
     reject -> 401 both routes, zero further mutation. Seam-mocked
     revocation (labelled), API behaviour real.
 13  second teacher denied                             REAL owner scoping:
     a different subject cannot read or write this stream (404).
 14  late retry idempotency over the journey           REAL store: the
     stage-9 request redelivered replays, never double-writes.

Offline, in-process, synthetic only. No network, no student data, no
publication, no production persistence claim (dev-only opt-in SQLite).
"""
import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audit_store
import decision_core
from api.app import create_app
from fastapi.testclient import TestClient
from support import benchmark, fake_client, load_app, model_question, remote

SUBJECT = "journey-teacher-a"
OWNER = "google:" + SUBJECT
OTHER = "google:journey-teacher-b"
PDF = b"%PDF-1.7 c6b6 journey synthetic scheme"
DIGEST = hashlib.sha256(PDF).hexdigest()
STUDENT_CANARY = "SENTINEL_journey_answer"


def marked_engine_row(f):
    """Stage 3+6: real per-submission marking over fixture evidence."""
    cases = benchmark()["questions"][:3]
    questions = []
    for case in cases:
        sample = model_question(case, "partial")
        sample["question_number"] = case["id"]
        questions.append(sample)
    client = fake_client([{"questions": questions}])
    maxima = {q["question_number"]: q["max_marks"] for q in questions}
    answers = {q["question_number"]: q["scheme_answer_evidence"]
               for q in questions}
    row = f["mark_one_submission"](
        client, "Synthetic Student", remote(), remote(name="scheme"),
        None, False, selected_questions=list(maxima),
        scheme_maxima=maxima, scheme_answers=answers)
    return row


class JourneyTests(unittest.TestCase):
    def setUp(self):
        # REAL engine, headless session stub (identity seam, same pattern
        # as the C6-B2/B4/C6-B5 suites).
        self.f = load_app()
        self.session = {}
        self.f["st"] = SimpleNamespace(
            session_state=self.session, user=SimpleNamespace(sub=SUBJECT))
        self.f["google_logged_in"] = lambda: True
        self.f["authenticated_google_identity"] = lambda: SUBJECT

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = audit_store.open_store(
            str(Path(self.tmp.name) / "journey.db"), dev_only_ack=True)
        self.addCleanup(self.store.close)

        # Stage 1 (mocked seam): a verifier the journey can revoke.
        self.live = {"value": True}

        def verify_token(token):
            if not self.live["value"]:
                return None
            if token == "tok-a":
                return SUBJECT
            if token == "tok-b":
                return "journey-teacher-b"
            return None

        # Stage 2 (mocked provider): owner-scoped listing only.
        self.visible = {}

        self.app = create_app(
            verify_token=verify_token,
            rows_for=lambda subject, owner: list(
                self.visible.get(owner, [])),
            store_provider=lambda: (self.store, None),
            # Stage 4 (mocked grant store; predicate shape from the app).
            manual_document_provenance_ok=lambda row: (
                row.get("snapshot_context") == DIGEST))
        self.client = TestClient(self.app, raise_server_exceptions=False)

    def auth(self, who="a"):
        return {"Authorization": f"Bearer tok-{who}"}

    def test_full_acceptance_journey(self):
        # -- stage 3+6: the marking engine runs for real ------------------
        engine_row = marked_engine_row(self.f)
        self.assertEqual(engine_row["status"], "Marked")
        ai_marks = [q["awarded_marks"] for q in engine_row["result"]["questions"]]
        self.assertEqual(ai_marks, [1, 1, 1],
                         "fixture-declared partial outcomes")

        # -- stage 8 wrap: production review-row wrappers ----------------
        reviewed = self.f["new_review_row"](engine_row, "api-journey")
        reviewed = self.f["prepare_review_row"](reviewed)
        # Server-derived upload context (stage 4) for the API identity.
        reviewed["snapshot_context"] = DIGEST
        reviewed["revision"] = ""
        rid = decision_core.durable_result_identity(reviewed, "")
        self.assertTrue(rid.startswith("manual:"))
        key = reviewed["review_key"]
        self.visible[OWNER] = [reviewed]

        # -- stage 5: nothing is registered before first acceptance -------
        before = self.client.get(f"/v1/results/{rid}/events",
                                 headers=self.auth())
        self.assertEqual(before.status_code, 404,
                         "stream only exists once a real decision commits")

        # -- stage 7: progress/dashboard/ingestion do not exist yet -------
        for path in ("/v1/results", "/v1/jobs", f"/v1/results/{rid}/progress"):
            self.assertIn(self.client.get(path, headers=self.auth())
                          .status_code, (404, 405), path)

        # -- stage 8: the evidence binding the correction reviews ---------
        binding = decision_core.question_review_binding(key, reviewed, "2", "")
        self.assertTrue(binding)

        # -- stage 9: teacher correction over REAL HTTP + CORE + STORE ----
        correction = self.client.post(
            f"/v1/results/{rid}/decisions", headers=self.auth(),
            json={"question_number": "2", "action": "correct",
                  "new_score": "2", "reason": "scheme A1 earned",
                  "expected_revision": 0, "idempotency_key": "j-9",
                  "old_score": "1", "binding": binding,
                  "review_status": "corrected"})
        self.assertEqual(correction.status_code, 200, correction.text)
        payload = correction.json()
        self.assertEqual(payload["event_sequence"], 1)
        self.assertEqual(payload["result_revision"], 1)
        self.assertFalse(payload["replayed"])

        # -- stage 10: original vs effective totals with engine rules -----
        self.assertEqual(
            [q["awarded_marks"] for q in reviewed["ai_result"]["questions"]],
            [1, 1, 1])
        effective = copy.deepcopy(reviewed["ai_result"])
        self.session["teacher_overrides"] = {key: {"2": 2}}
        self.f["apply_teacher_overrides"](key, effective)
        earned, available, _ = self.f["calculate_totals"](effective)
        self.assertEqual((earned, available), (4, 6))
        # And the engine-original stays untouched everywhere in the store.
        original = self.store.original_record(owner=OWNER, result_id=rid)
        self.assertEqual([pair[1] for pair in original["original_scores"]],
                         [1, 1, 1])

        # -- stage 11: durable audit reads back exactly one decision ------
        read = self.client.get(f"/v1/results/{rid}/events", headers=self.auth())
        self.assertEqual(read.status_code, 200, read.text)
        events = read.json()["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["review_status"], "corrected")
        self.assertEqual(events[0]["old_score"], "1")
        self.assertEqual(events[0]["new_score"], "2")
        self.assertEqual(events[0]["actor"], OWNER)
        self.assertEqual({e["event_type"] for e in events},
                         {"question_review_decision"})
        # Student content and the bearer token never reach the substrate.
        blob = b""
        for item in Path(self.tmp.name).iterdir():
            blob += item.read_bytes()
        self.assertNotIn(STUDENT_CANARY.encode(), blob)
        self.assertNotIn(b"tok-a", blob)

        # -- stage 12: logout / expiry: revocation freezes everything -----
        self.live["value"] = False
        self.assertEqual(
            self.client.get(f"/v1/results/{rid}/events",
                            headers=self.auth()).status_code, 401)
        blocked = self.client.post(
            f"/v1/results/{rid}/decisions", headers=self.auth(),
            json={"question_number": "1", "action": "approve",
                  "expected_revision": 1, "idempotency_key": "j-12"})
        self.assertEqual(blocked.status_code, 401)
        self.assertEqual(self.store.current_revision(owner=OWNER,
                                                     result_id=rid), 1)

        # -- stage 13: the second teacher is denied on both routes --------
        self.live["value"] = True
        foreign_events = self.client.get(f"/v1/results/{rid}/events",
                                         headers=self.auth("b"))
        self.assertEqual(foreign_events.status_code, 404)
        foreign_write = self.client.post(
            f"/v1/results/{rid}/decisions", headers=self.auth("b"),
            json={"question_number": "1", "action": "approve",
                  "expected_revision": 0, "idempotency_key": "j-13"})
        self.assertEqual(foreign_write.status_code, 404)
        self.assertEqual(self.store.current_revision(owner=OWNER,
                                                     result_id=rid), 1)

        # -- stage 14: a late redelivery of stage 9 replays, never repeats
        replay = self.client.post(
            f"/v1/results/{rid}/decisions", headers=self.auth(),
            json={"question_number": "2", "action": "correct",
                  "new_score": "2", "reason": "scheme A1 earned",
                  "expected_revision": 0, "idempotency_key": "j-9-late",
                  "old_score": "1", "binding": binding,
                  "review_status": "corrected"})
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(replay.json()["event_sequence"], 1)
        self.assertEqual(len(self.store.events(owner=OWNER,
                                               result_id=rid)), 1)


if __name__ == "__main__":
    unittest.main()
