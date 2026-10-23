"""C6-B3 M2 tests: development-only durable audit persistence in the app path.

Offline only: every provider boundary is synthetic (faked token/listing
functions in the loaded app scope, injected session state, a local temporary
SQLite file). The shared ``requests`` fake in the headless loader raises on
any unexpected HTTP call, so a passing run proves no network access happened.
No real student data, no credentials and no live provider are involved.

Covered:
  - persistence is OFF by default: no store file, no audit reference, and
    removing the opt-in reverts to the exact session-only behaviour,
  - an accepted decision commits correction + audit event atomically, with a
    durable reference on the decision record and a verifiable ordered stream,
  - every gate still runs first and fail-closed WITH persistence enabled:
    fresh Classroom re-verification, manual PDF byte/owner binding, question
    identity, separate approval/publication state,
  - failure injection: a failed audit write keeps NOTHING (no session record,
    no session override, no store event), and a stale revision is refused
    without partial state,
  - store unusable / opt-in missing / durable identity missing all refuse the
    decision instead of writing an unaudited correction,
  - M1 IR findings L-1 (override filter normalisation) and L-2 (has_overrides
    accuracy) with regressions.
"""
import copy
import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audit_store
import question_review as qr
import teacher_authorization as ta
import test_c6b2_security as c6b2
from support import benchmark, load_app, model_question

SUBJECT = "teacher-a-subject"
OWNER = "google:" + SUBJECT
PDF = b"%PDF-1.7 c6b3 m2 durable audit fixture"
STORE_ENV = "AI_MARKER_AUDIT_DEV_STORE"
ACK_ENV = "AI_MARKER_AUDIT_DEV_ONLY"
SENTINEL = "SENTINEL_RAW_ANSWER_m2_9d2b"


def sample_question(number="1", awarded=1, maximum=2):
    sample = model_question(benchmark()["questions"][0], "partial")
    sample["question_number"] = number
    sample["awarded_marks"] = awarded
    sample["max_marks"] = maximum
    sample["student_answer"] = f"{SENTINEL} answer {number}"
    sample["working_summary"] = f"{SENTINEL} working {number}"
    return sample


def exploding(stage_name):
    """Failure-injection hook that aborts a commit at one named stage."""

    def hook(stage):
        if stage == stage_name:
            raise RuntimeError(f"injected fault at {stage}")
    return hook


class AppPersistenceTestCase(unittest.TestCase):
    """Loaded app + session state + local temporary store path."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store_path = os.path.join(self.tmp, "audit.db")
        self.f = load_app()
        self.session = {}
        self.f["st"] = SimpleNamespace(
            session_state=self.session, user=SimpleNamespace(sub=SUBJECT))
        self.f["google_logged_in"] = lambda: True
        self.f["authenticated_google_identity"] = lambda: SUBJECT
        self.f["pd"] = SimpleNamespace(DataFrame=lambda rows: rows)
        self.f["remember_manual_upload"](PDF)
        self.session["last_manual_context"] = hashlib.sha256(PDF).hexdigest()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        # Close the cached store and remove the process-wide opt-in, so no
        # other test module can ever inherit it.
        self.f["reset_dev_audit_store"]()
        os.environ.pop(STORE_ENV, None)
        os.environ.pop(ACK_ENV, None)

    def enable_store(self, path=None, acknowledge=True):
        os.environ[STORE_ENV] = path or self.store_path
        if acknowledge:
            os.environ[ACK_ENV] = "1"
        else:
            os.environ.pop(ACK_ENV, None)
        return path or self.store_path

    def open_store(self):
        store, code = self.f["dev_audit_store"]()
        self.assertIsNone(code, code)
        self.assertIsNotNone(store)
        return store

    def store_bytes(self):
        blob = b""
        for name in sorted(os.listdir(self.tmp)):
            with open(os.path.join(self.tmp, name), "rb") as handle:
                blob += handle.read()
        return blob

    def row_for(self, numbers, review_key="review-1"):
        result = self.f["normalise_result"](
            {"questions": [sample_question(number) for number in numbers]})
        earned, available, percentage = self.f["calculate_totals"](result)
        return {"status": "Marked", "review_key": review_key,
                "report_owner": OWNER, "result": copy.deepcopy(result),
                "ai_result": copy.deepcopy(result),
                "totals": {"earned": earned, "available": available,
                           "percentage": percentage,
                           "provisional_grade":
                               self.f["percentage_band_grade"](percentage)},
                "groups": self.f["make_group_rows"](result["questions"]),
                "student_name": "Synthetic", "marking_model": "fake",
                "audit_model": "Not run"}

    def classroom_row(self, numbers=("1", "2"), review_key="classroom_row-1",
                      revision="rev-1", context="c" * 64,
                      submission_id="submission-1"):
        row = self.row_for(list(numbers), review_key=review_key)
        row.update({"classroom_course_id": "course-1",
                    "classroom_assignment_id": "work-1",
                    "classroom_context": context, "revision": revision,
                    "submission_id": submission_id,
                    "selected_questions": list(numbers),
                    "classroom_max_points": 100,
                    "homework_question_count": len(numbers)})
        return row

    def install(self, row):
        self.session["last_batch_results"] = [row]
        return row

    def enable_classroom(self):
        self.courses = Mock(return_value=list(c6b2.COURSES))
        self.assignments = Mock(return_value=list(c6b2.ASSIGNMENTS))
        self.f["list_teacher_courses"] = self.courses
        self.f["list_assignments"] = self.assignments
        self.f["get_google_access_token"] = lambda: "synthetic-token"
        self.f["record_classroom_grants"](
            list(c6b2.COURSES), list(c6b2.ASSIGNMENTS), "course-1")

    def decision(self, student_key, question_number="1", action="approve",
                 reason="", mark=None):
        self.f["apply_question_review_decision"](
            student_key, question_number, action, reason, mark)
        return self.session.get("question_review_notice")

    def stored(self, key):
        return self.session.get("question_review_decisions", {}).get(key)

    def stream(self, row, owner=OWNER):
        """Owner-scoped events of one row's durable stream."""

        store = self.open_store()
        return store, store.events(owner=owner,
                                   result_id=self.f["durable_result_identity"](row))


class DefaultOffTests(AppPersistenceTestCase):
    """Off by default, and reversible when the opt-in is removed again."""

    def test_default_off_keeps_session_only_behaviour(self):
        row = self.install(self.row_for(["1"]))
        self.assertEqual(self.decision(row["review_key"])[0], "ok")
        self.assertIsNone(self.stored("review-1::q1").get("audit_reference"))
        self.assertEqual(len(self.session["question_review_events"]), 1)
        # Nothing was created on disk: no env means no store at all.
        self.assertFalse(os.path.exists(self.store_path))
        self.assertEqual(self.f["dev_audit_store"](), (None, None))

    def test_unsetting_the_opt_in_reverts_to_session_only(self):
        self.enable_store()
        row = self.install(self.row_for(["1", "2"]))
        self.assertEqual(self.decision(row["review_key"], "1")[0], "ok")
        store = self.open_store()
        identity = self.f["durable_result_identity"](row)
        self.assertEqual(len(store.events(owner=OWNER, result_id=identity)), 1)
        digest = store.original_record(owner=OWNER,
                                       result_id=identity)["original_digest"]
        store.close()
        # Operator turns persistence off again.
        os.environ.pop(STORE_ENV, None)
        os.environ.pop(ACK_ENV, None)
        self.f["reset_dev_audit_store"]()
        self.assertEqual(self.decision(row["review_key"], "2")[0], "ok")
        self.assertIsNone(self.stored("review-1::q2").get("audit_reference"))
        # The store file itself was never touched by the second decision.
        self.f["reset_dev_audit_store"]()
        self.enable_store()
        store = self.open_store()
        self.assertEqual(len(store.events(owner=OWNER, result_id=identity)), 1)
        self.assertEqual(
            store.original_record(owner=OWNER, result_id=identity)["original_digest"],
            digest)
        self.assertIsNone(self.stored("review-1::q2").get("audit_reference"))


class OptInCommitTests(AppPersistenceTestCase):
    """An accepted decision and its audit event are committed together."""

    def test_opted_in_decision_commits_an_event_and_a_reference(self):
        self.enable_store()
        row = self.install(self.row_for(["1"]))
        self.assertEqual(self.decision(row["review_key"])[0], "ok")
        store, events = self.stream(row)
        self.assertEqual(len(events), 1)
        record = self.stored("review-1::q1")
        reference = record["audit_reference"]
        identity = self.f["durable_result_identity"](row)
        self.assertEqual(reference["result_id"], identity)
        self.assertEqual(reference["sequence"], events[0]["sequence"])
        self.assertEqual(reference["event_id"], events[0]["event_id"])
        self.assertEqual(events[0]["event_type"], "question_review_decision")
        self.assertEqual(events[0]["review_status"], "approved")
        self.assertEqual(events[0]["actor"], OWNER)
        self.assertEqual(events[0]["expected_revision"], 0)
        self.assertEqual(events[0]["resulting_revision"], 1)
        self.assertTrue(store.verify(owner=OWNER, result_id=identity)["ok"])
        # The event is the durable twin of the session decision.
        self.assertEqual(record["status"], events[0]["review_status"])
        self.assertEqual(record["authorization_evidence"],
                         events[0]["authorization_evidence"])
        # Raw answers/working text never reach the durable file.
        self.assertNotIn(SENTINEL.encode("utf-8"), self.store_bytes())
        self.assertNotIn(b"synthetic-token", self.store_bytes())

    def test_correction_commits_old_and_new_scores_with_the_session_write(self):
        self.enable_store()
        row = self.install(self.row_for(["1"]))
        self.assertEqual(
            self.decision(row["review_key"], "1", action="correct",
                          reason="scheme B1 missed", mark=2)[0], "ok")
        store, events = self.stream(row)
        event = events[0]
        self.assertEqual(event["old_score"], "1")
        self.assertEqual(event["new_score"], "2")
        self.assertEqual(event["review_status"], "corrected")
        self.assertEqual(event["reason"], "scheme B1 missed")
        # Session override and durable event describe the same correction.
        self.assertEqual(self.session["teacher_overrides"]["review-1"]["1"], 2)
        self.assertEqual(self.stored("review-1::q1")["corrected_marks"], [2])
        self.assertEqual(
            store.current_revision(owner=OWNER,
                                   result_id=self.f["durable_result_identity"](row)),
            1)

    def test_repeating_a_decision_does_not_append_a_second_event(self):
        self.enable_store()
        row = self.install(self.row_for(["1"]))
        self.assertEqual(self.decision(row["review_key"])[0], "ok")
        self.assertEqual(self.decision(row["review_key"])[0], "ok")
        store, events = self.stream(row)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            store.current_revision(owner=OWNER,
                                   result_id=self.f["durable_result_identity"](row)),
            1)

    def test_restart_recovers_history_and_continues_the_stream(self):
        self.enable_store()
        row = self.install(self.row_for(["1", "2"]))
        self.assertEqual(self.decision(row["review_key"], "1")[0], "ok")
        # Simulate a process restart: close the cached handle entirely.
        self.f["reset_dev_audit_store"]()
        store = self.open_store()
        identity = self.f["durable_result_identity"](row)
        replayed = store.replay(owner=OWNER, result_id=identity)
        self.assertEqual([event["sequence"] for event in replayed], [1])
        # A decision made after the restart continues the same ordered stream.
        self.assertEqual(self.decision(row["review_key"], "2")[0], "ok")
        events = store.events(owner=OWNER, result_id=identity)
        self.assertEqual([event["sequence"] for event in events], [1, 2])
        self.assertEqual([event["question_number"] for event in events], ["1", "2"])
        self.assertTrue(store.verify(owner=OWNER, result_id=identity)["ok"])
        # The original output reference is unchanged by the restart.
        self.assertEqual(len(store.original_record(
            owner=OWNER, result_id=identity)["original_digest"]), 64)

    def test_a_changed_original_output_refuses_later_decisions(self):
        self.enable_store()
        row = self.install(self.row_for(["1"]))
        self.assertEqual(self.decision(row["review_key"])[0], "ok")
        # The same durable identity is later presented with different AI marks
        # (a re-mark under the same provider revision): never silently
        # relabelled — the decision is refused and nothing partial is kept.
        row["ai_result"]["questions"][0]["awarded_marks"] = 2
        row["result"]["questions"][0]["awarded_marks"] = 2
        notice = self.decision(row["review_key"], "1", action="correct",
                               reason="re-marked", mark=2)
        self.assertEqual(notice[0], "original_output_immutable")
        store, events = self.stream(row)
        self.assertEqual(len(events), 1)
        # Nothing partial: the earlier decision record and its reference are
        # untouched, and no correction override survived the refusal.
        record = self.stored("review-1::q1")
        self.assertEqual(record["status"], "approved")
        self.assertEqual(record["audit_reference"]["sequence"], 1)
        self.assertNotIn("review-1", self.session.get("teacher_overrides", {}))


class FailClosedTests(AppPersistenceTestCase):
    """Persistence problems refuse the decision instead of losing the audit."""

    def test_missing_opt_in_acknowledgement_refuses_the_decision(self):
        self.enable_store(acknowledge=False)
        row = self.install(self.row_for(["1"]))
        self.assertEqual(
            self.decision(row["review_key"], "1", action="correct",
                          reason="should not persist", mark=2)[0],
            "audit_store_unavailable")
        self.assertIsNone(self.stored("review-1::q1"))
        self.assertNotIn("review-1", self.session.get("teacher_overrides", {}))
        self.assertNotIn("question_review_events", self.session)
        self.assertFalse(os.path.exists(self.store_path))

    def test_an_unusable_store_path_refuses_the_decision(self):
        self.enable_store(path=self.tmp)  # a directory is not a database
        row = self.install(self.row_for(["1"]))
        self.assertEqual(self.decision(row["review_key"])[0],
                         "audit_store_unavailable")
        self.assertIsNone(self.stored("review-1::q1"))
        self.assertNotIn("question_review_events", self.session)

    def test_missing_durable_identity_refuses_the_decision(self):
        self.enable_store()
        row = self.classroom_row(("1",))
        del row["submission_id"]  # no provider-stable identity to key on
        self.enable_classroom()
        self.install(row)
        self.assertEqual(self.decision(row["review_key"])[0],
                         "audit_result_identity_unavailable")
        self.assertIsNone(self.stored("classroom_row-1::q1"))
        self.assertNotIn("question_review_events", self.session)
        store = self.open_store()
        self.assertIsNone(
            store.current_revision(owner=OWNER, result_id="classroom:missing"))

    def test_a_failed_audit_write_keeps_nothing(self):
        self.enable_store()
        row = self.install(self.row_for(["1"]))
        store = self.open_store()
        store.set_fault_hook(exploding("after_event_insert"))
        notice = self.decision(row["review_key"], "1", action="correct",
                               reason="must not survive", mark=2)
        self.assertEqual(notice[0], "audit_persist_failed")
        store.set_fault_hook(None)
        # Session decision record: absent. Session override: rolled back.
        self.assertIsNone(self.stored("review-1::q1"))
        self.assertNotIn("review-1", self.session.get("teacher_overrides", {}))
        self.assertNotIn("review-1", self.session.get("teacher_override_reasons",
                                                      {}))
        self.assertNotIn("question_review_events", self.session)
        # Store: registration without any event or revision (nothing partial).
        identity = self.f["durable_result_identity"](row)
        self.assertEqual(store.current_revision(owner=OWNER, result_id=identity), 0)
        self.assertEqual(store.events(owner=OWNER, result_id=identity), [])
        # Pristine marks untouched; a later attempt succeeds cleanly.
        self.assertEqual(
            [q["awarded_marks"] for q in row["ai_result"]["questions"]], [1])
        self.assertEqual(self.decision(row["review_key"], "1", action="correct",
                                       reason="retry after fault", mark=2)[0],
                         "ok")
        events = store.events(owner=OWNER, result_id=identity)
        self.assertEqual([event["sequence"] for event in events], [1])
        self.assertEqual(self.session["teacher_overrides"]["review-1"]["1"], 2)

    def test_a_failed_commit_restores_the_cleared_approval(self):
        """A failed correction must not cost the teacher its approval.

        A Classroom correction invalidates the grade approval as a side effect
        of the override write; when the durable audit commit then fails, both
        the override and that invalidation are rolled back, because no real
        correction ever happened.
        """
        self.enable_store()
        self.enable_classroom()
        row = self.install(self.classroom_row(("1",)))
        key = row["review_key"]
        self.session["classroom_grade_approvals"] = {key: True}
        self.session[f"classroom_approval_checkbox_{key}"] = True
        store = self.open_store()
        store.set_fault_hook(exploding("after_event_insert"))
        notice = self.decision(key, "1", action="correct",
                               reason="must not survive", mark=2)
        self.assertEqual(notice[0], "audit_persist_failed")
        store.set_fault_hook(None)
        # Approval and its checkbox are exactly as before the failed attempt.
        self.assertTrue(self.session["classroom_grade_approvals"][key])
        self.assertTrue(self.session[f"classroom_approval_checkbox_{key}"])
        # No override, no decision record, no session event, no store event.
        self.assertNotIn(key, self.session.get("teacher_overrides", {}))
        self.assertNotIn(key, self.session.get("teacher_override_reasons", {}))
        self.assertIsNone(self.stored(f"{key}::q1"))
        self.assertNotIn("question_review_events", self.session)
        identity = self.f["durable_result_identity"](row)
        self.assertEqual(store.events(owner=OWNER, result_id=identity), [])
        self.assertEqual(store.current_revision(owner=OWNER, result_id=identity), 0)

    def test_a_stale_revision_is_refused_without_partial_state(self):
        self.enable_store()
        row = self.install(self.row_for(["1"]))
        store = self.open_store()
        identity = self.f["durable_result_identity"](row)
        # Another session commits first (a concurrent writer wins the race).
        store.register_result(owner=OWNER, result_id=identity,
                              original_output=row["ai_result"])
        store.commit_correction(
            owner=OWNER, result_id=identity, expected_revision=0,
            idempotency_key="concurrent-session",
            payload={"question_number": "2", "action": "approve",
                     "review_status": "approved", "old_score": "1",
                     "new_score": "", "reason": "", "binding": "b" * 64},
            event={"event_type": "question_review_decision",
                   "question_number": "2", "action": "approve",
                   "review_status": "approved", "old_score": "1",
                   "new_score": "", "reason": "", "actor": OWNER,
                   "binding": "b" * 64,
                   "authorization_evidence":
                       {"scope": "manual", "actor": OWNER,
                        "authorization_revision": "d" * 64,
                        "row_revision": "rev-1", "verified_at": "1700000000"}})
        # Our decision read revision 0 BEFORE that commit landed.
        store.current_revision = lambda **kwargs: 0
        notice = self.decision(row["review_key"], "1", action="correct",
                               reason="lost the race", mark=2)
        self.assertEqual(notice[0], "stale_revision")
        self.assertIsNone(self.stored("review-1::q1"))
        self.assertNotIn("review-1", self.session.get("teacher_overrides", {}))
        self.assertNotIn("question_review_events", self.session)
        # The winner's event is the only one; our write left no trace.
        self.f["reset_dev_audit_store"]()
        store = self.open_store()
        events = store.events(owner=OWNER, result_id=identity)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["question_number"], "2")
        self.assertEqual(events[0]["resulting_revision"], 1)

    def test_an_ambiguous_question_never_touches_the_store(self):
        self.enable_store()
        row = self.install(self.row_for(["1", "1"]))
        self.assertEqual(self.decision(row["review_key"], "1")[0],
                         "review_question_ambiguous")
        store = self.open_store()
        identity = self.f["durable_result_identity"](row)
        # Not even a registration happened: refused before any store write.
        self.assertIsNone(store.current_revision(owner=OWNER, result_id=identity))


class GatePreservationTests(AppPersistenceTestCase):
    """Fresh authorization and separate gates are unchanged by persistence."""

    def test_revocation_refuses_the_decision_before_any_store_write(self):
        self.enable_store()
        self.enable_classroom()
        row = self.install(self.classroom_row(("1",)))
        self.courses.return_value = [c6b2.COURSES[1]]  # provider revocation
        self.assertEqual(self.decision(row["review_key"])[0],
                         "course_access_unverified")
        store = self.open_store()
        identity = self.f["durable_result_identity"](row)
        self.assertIsNone(store.current_revision(owner=OWNER, result_id=identity))
        self.assertIsNone(self.stored("classroom_row-1::q1"))
        self.assertNotIn("question_review_events", self.session)

    def test_manual_pdf_byte_and_owner_binding_reaches_the_stored_event(self):
        self.enable_store()
        row = self.install(self.row_for(["1"]))
        self.assertEqual(self.decision(row["review_key"])[0], "ok")
        _, events = self.stream(row)
        evidence = events[0]["authorization_evidence"]
        self.assertEqual(set(evidence), set(qr.AUTHORIZATION_EVIDENCE_FIELDS))
        self.assertEqual(evidence["scope"], "manual")
        self.assertEqual(evidence["actor"], OWNER)
        # The byte binding of the OWNED upload survives persistence exactly.
        self.assertEqual(evidence["authorization_revision"],
                         hashlib.sha256(PDF).hexdigest())
        self.assertEqual(evidence["authorization_revision"],
                         self.session["last_manual_context"])

    def test_classroom_event_keeps_the_freshly_computed_revision(self):
        self.enable_store()
        self.enable_classroom()
        row = self.install(self.classroom_row(("1",)))
        self.assertEqual(self.decision(row["review_key"])[0], "ok")
        _, events = self.stream(row)
        evidence = events[0]["authorization_evidence"]
        self.assertEqual(evidence["scope"], "classroom")
        self.assertEqual(
            evidence["authorization_revision"],
            ta.assignment_revision("course-1", c6b2.assignment("work-1")))
        self.assertNotIn("synthetic-token", str(events))
        # The row's own durable identity is provider-stable, not the review key.
        identity = self.f["durable_result_identity"](row)
        self.assertEqual(identity,
                         "classroom:course-1:work-1:submission-1:rev-1")

    def test_approval_and_publication_stay_separate_with_persistence_on(self):
        self.enable_store()
        self.enable_classroom()
        row = self.install(self.classroom_row(("1",)))
        key = row["review_key"]
        self.session["classroom_grade_approvals"] = {key: True}
        self.session[f"classroom_approval_checkbox_{key}"] = True
        self.assertEqual(self.decision(key, "1", action="approve")[0], "ok")
        # Approving a question neither publishes nor clears the approval.
        self.assertTrue(self.session["classroom_grade_approvals"][key])
        self.assertEqual(
            self.decision(key, "1", action="correct",
                          reason="scheme B1 missed", mark=2)[0], "ok")
        self.assertNotIn(key, self.session["classroom_grade_approvals"])
        # Publication still needs its own separately gated action.
        self.assertTrue(self.f["owned_review_key"](key))
        self.session[f"classroom_approval_checkbox_{key}"] = True
        self.f["save_classroom_approval"](key)
        self.assertTrue(self.session["classroom_grade_approvals"][key])
        # The durable stream holds review decisions only — no approval or
        # publication record is ever written to this store.
        _, events = self.stream(row)
        self.assertEqual({event["event_type"] for event in events},
                         {"question_review_decision"})
        self.assertTrue(all(event["review_status"] in ("approved", "corrected",
                                                       "flagged")
                            for event in events))


class OverrideIndicatorRegressionTests(AppPersistenceTestCase):
    """M1 IR findings L-1 (filter normalisation) and L-2 (indicator accuracy)."""

    def raw_row(self, numbers):
        """A row whose question numbers were never strip-normalised (IR probe)."""

        questions = [sample_question(number) for number in numbers]
        result = {"questions": questions}
        return {"status": "Marked", "review_key": "review-1",
                "report_owner": OWNER, "result": copy.deepcopy(result),
                "ai_result": copy.deepcopy(result),
                "totals": {"earned": 1, "available": 2, "percentage": 50.0,
                           "provisional_grade": "5"},
                "groups": [], "student_name": "Synthetic",
                "marking_model": "fake", "audit_model": "Not run"}

    def test_l1_the_override_filter_uses_normalised_question_keys(self):
        row = self.raw_row([" 1", " 1"])  # collides only after normalising
        self.assertEqual(qr.ambiguous_question_numbers(row["result"]["questions"]),
                         ["1"])
        self.session["teacher_overrides"] = {"review-1": {" 1": 2}}
        # L-1: the override key is compared with the same normalisation the
        # collision was detected with, so it is dropped instead of applied.
        self.assertEqual(self.f["unambiguous_overrides"](
            row["result"]["questions"], {" 1": 2}), {})
        changed = self.f["apply_teacher_overrides"]("review-1", row["result"])
        self.assertFalse(changed)
        self.assertEqual(
            [q["awarded_marks"] for q in row["result"]["questions"]], [1, 1])
        # Control: a single, unambiguous question with the same raw key still
        # takes its override (the fix drops collisions only).
        unique = self.raw_row([" 1"])
        self.session["teacher_overrides"] = {"review-1": {" 1": 2}}
        self.assertTrue(self.f["apply_teacher_overrides"]("review-1",
                                                          unique["result"]))
        self.assertEqual(
            [q["awarded_marks"] for q in unique["result"]["questions"]], [2])

    def test_l2_has_overrides_reports_only_overrides_that_apply(self):
        row = self.raw_row(["1", "2"])
        row.update({"classroom_max_points": 100,
                    "homework_question_count": 2, "selected_questions": None})
        # A stored key that matches no question in this result is not applied,
        # so it must not be reported as an applied override.
        self.session["teacher_overrides"] = {"review-1": {"99": 2}}
        details = self.f["classroom_grade_details"](row)
        self.assertEqual(details["score"], "2/4")
        self.assertFalse(details["has_overrides"])
        # Control: an override that applies to a question is still reported.
        self.session["teacher_overrides"] = {"review-1": {"2": 2}}
        details = self.f["classroom_grade_details"](row)
        self.assertEqual(details["score"], "3/4")
        self.assertTrue(details["has_overrides"])
        # A colliding override (dropped by L-1) is never reported either.
        colliding = self.raw_row([" 1", " 1"])
        colliding.update({"classroom_max_points": 100,
                          "homework_question_count": 2,
                          "selected_questions": None})
        self.session["teacher_overrides"] = {"review-1": {" 1": 2}}
        details = self.f["classroom_grade_details"](colliding)
        self.assertEqual(details["score"], "2/4")
        self.assertFalse(details["has_overrides"])


if __name__ == "__main__":
    unittest.main()
