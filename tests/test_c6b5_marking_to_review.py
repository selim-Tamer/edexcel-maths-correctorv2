"""C6-B5 tests: synthetic marking-to-review and manual-upload whole journeys.

Closes IR gaps #3 and #4 (C6-B4 review report section 3): C6-B4's journey
enters POST-marking (rows are injected) and every journey row is a
Classroom row. These tests drive the real marking pipeline itself with a
faked model client — normalise, validators, totals, review-row creation,
prepare/rerun semantics — and then continue into question review,
reconciliation and the durable audit stream in ONE flow, for both scopes:

  - the WHOLE manual-upload journey: upload -> mark -> review -> durable
    audit -> export-shape totals, with the C6-B2 F-2 document-provenance
    gate, and
  - the marking-to-review Classroom journey: the same marking pipeline with
    the row bound to the assignment exactly as the Classroom batch binds
    rows, decision re-verification through F-1, and the approval gate.

Offline only: the Gemini client is ``support.fake_client`` (returns
pre-declared synthetic per-question outcomes authored in
``fixtures/synthetic_marking.json`` — expected outcomes of the CONTRACT,
not evidence a real model would agree), the Classroom listing boundary is
the same synthetic fixtures C6-B2/C6-B4 use, the audit store is a local
temporary SQLite file, and the shared ``requests`` fake raises on any
unexpected HTTP call, so a green run proves no network happened. No real
student data, no credentials. Expected marks come from the fixture
allocations, never invented to fit outputs.
"""
import copy
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_c6b3_m2_app_integration as m2
from support import benchmark, fake_client, model_question, remote


class MarkingToReviewTests(m2.AppPersistenceTestCase):
    """Real marking orchestration followed by the full review chain."""

    NUMBERS = ("1", "2", "3")

    def mark_questions(self, numbers=None):
        """Drive the production per-submission marking function offline."""
        numbers = numbers or self.NUMBERS
        cases = benchmark()["questions"][:len(numbers)]
        questions = []
        for number, case in zip(numbers, cases):
            sample = model_question(case, "partial")
            sample["question_number"] = number
            questions.append(sample)
        client = fake_client([{"questions": questions}])
        maxima = {q["question_number"]: q["max_marks"] for q in questions}
        answers = {q["question_number"]: q["scheme_answer_evidence"]
                   for q in questions}
        row = self.f["mark_one_submission"](
            client, "Synthetic Student", remote(), remote(name="scheme"),
            None, False, selected_questions=list(maxima),
            scheme_maxima=maxima, scheme_answers=answers)
        self.assertEqual(row["status"], "Marked", row)
        # The pipeline itself honoured the fixture allocations: the journey
        # below reviews marks the pipeline actually produced.
        self.assertEqual(
            [q["awarded_marks"] for q in row["result"]["questions"]],
            [q["awarded_marks"] for q in questions])
        return row

    def reviewed(self, row, namespace):
        """Wrap a freshly marked row exactly as a completed run does."""
        reviewed = self.f["new_review_row"](row, namespace)
        self.assertEqual(reviewed["report_owner"], m2.OWNER)
        return self.f["prepare_review_row"](reviewed)

    def stream_event_types(self, row):
        _, events = self.stream(row)
        return [event["event_type"] for event in events]


class ManualUploadJourneyTests(MarkingToReviewTests):
    """Whole manual journey: upload -> mark -> review -> durable audit."""

    def bind_provenance(self):
        # The manual provenance grant of THIS session's uploaded scheme PDF
        # (the harness already recorded the byte digest server-side).
        self.session["last_manual_context"] = hashlib.sha256(
            m2.PDF).hexdigest()

    def test_whole_manual_upload_journey_marks_reviews_and_persists(self):
        self.enable_store()
        self.bind_provenance()
        row = self.reviewed(self.mark_questions(), "manual-upload")
        self.assertIsNone(row.get("classroom_course_id"))
        self.assertEqual(self.f["row_review_scope"](row), "manual")
        self.install(row)
        key = row["review_key"]

        # Reviewer decisions on the marks the pipeline produced: approve,
        # correct (fixture-backed: max 2, pipeline awarded 1 -> teacher 2),
        # flag for re-mark.
        self.assertEqual(self.decision(key, "1", action="approve")[0], "ok")
        self.assertEqual(
            self.decision(key, "2", action="correct",
                          reason="scheme M1 step present", mark=2)[0], "ok")
        self.assertEqual(
            self.decision(key, "3", action="flag",
                          reason="needs a human eye")[0], "ok")

        # One ordered durable event per decision, decision records carry the
        # durable reference, and the stream holds review decisions ONLY.
        _, events = self.stream(row)
        self.assertEqual([event["sequence"] for event in events], [1, 2, 3])
        self.assertEqual(self.stream_event_types(row),
                         ["question_review_decision"] * 3)
        for number in self.NUMBERS:
            record = self.stored("%s::q%s" % (key, number))
            self.assertEqual(record["audit_reference"]["result_id"],
                             self.f["durable_result_identity"](row))

        # Single override system stays authoritative; originals preserved.
        # (The display path folds overrides into the row exactly as the app
        # does before summaries/exports: session override -> apply -> marks.)
        self.assertEqual(self.session["teacher_overrides"][key]["2"], 2)
        self.assertTrue(self.f["apply_teacher_overrides"](key, row["result"]))
        self.assertEqual(
            [q["awarded_marks"] for q in row["result"]["questions"]], [1, 2, 1])
        self.assertEqual(
            [q["awarded_marks"] for q in row["ai_result"]["questions"]], [1, 1, 1])
        earned, available, _ = self.f["calculate_totals"](row["result"])
        self.assertEqual((earned, available), (4, 6))

        # A rerun of the same completed row keeps the whole chain intact.
        before_events = self.stream(row)[1]
        before_records = copy.deepcopy(
            self.session["question_review_decisions"])
        self.install(self.f["prepare_review_row"](row))
        self.assertEqual(self.session["teacher_overrides"][key]["2"], 2)
        self.assertEqual(self.stream(row)[1], before_events)
        self.assertEqual(self.session["question_review_decisions"],
                         before_records)

    def test_manual_journey_without_document_provenance_fails_closed(self):
        self.enable_store()
        row = self.reviewed(self.mark_questions(), "manual-upload")
        self.install(row)
        key = row["review_key"]
        # The session's owned upload context no longer matches ANY recorded
        # grant: the manual decision gate refuses before touching state.
        self.session["last_manual_context"] = hashlib.sha256(
            b"someone-elses-scheme").hexdigest()
        self.assertEqual(self.decision(key, "1", action="approve")[0],
                         "manual_document_provenance_required")
        self.assertNotIn("%s::q1" % key,
                         self.session.get("question_review_decisions", {}))
        self.assertNotIn(key, self.session.get("teacher_overrides", {}))
        self.assertEqual(self.session.get("question_review_events", []), [])
        # Nothing was ever committed: the store file was never even created.
        self.assertFalse(Path(self.store_path).exists())

    def test_manual_journey_refuses_foreign_owner_rows(self):
        self.enable_store()
        self.bind_provenance()
        row = self.reviewed(self.mark_questions(), "manual-upload")
        row["report_owner"] = "google:someone-else"
        self.install(row)
        self.assertEqual(
            self.decision(row["review_key"], "1", action="approve")[0],
            "review_context_unavailable")
        self.assertNotIn("question_review_decisions", self.session)


class ClassroomMarkingToReviewTests(MarkingToReviewTests):
    """Marking-to-review over a Classroom-bound row with fresh re-verification."""

    def bind_classroom(self, row):
        # The same metadata the Classroom batch binds server-side per row
        # (assignment ids, revision, context digest); the listing/attachment
        # orchestration itself stays covered by test_classroom_integration.
        row.update(classroom_course_id="course-1",
                   classroom_assignment_id="work-1",
                   classroom_context="c" * 64, revision="rev-1",
                   submission_id="submission-1",
                   selected_questions=list(self.NUMBERS),
                   classroom_max_points=100,
                   homework_question_count=len(self.NUMBERS))
        return row

    def test_marking_to_review_to_signoff_in_one_flow(self):
        self.enable_store()
        self.enable_classroom()
        row = self.bind_classroom(
            self.reviewed(self.mark_questions(), "classroom"))
        self.install(row)
        key = row["review_key"]
        self.assertEqual(self.f["row_review_scope"](row), "classroom")

        # Marking produced the AI baseline; the teacher now reviews it:
        # approve, correct, flag — every decision re-verifies against fresh
        # trusted listings at the mutation boundary (F-1).
        self.assertEqual(self.decision(key, "1", action="approve")[0], "ok")
        self.assertEqual(
            self.decision(key, "2", action="correct",
                          reason="scheme A1 earned", mark=2)[0], "ok")
        self.assertEqual(
            self.decision(key, "3", action="flag",
                          reason="unclear handwriting")[0], "ok")

        _, events = self.stream(row)
        self.assertEqual([event["sequence"] for event in events], [1, 2, 3])
        self.assertEqual([event["review_status"] for event in events],
                         ["approved", "corrected", "flagged"])

        # Sign-off stays a SEPARATE, explicit gate: the correction reset any
        # prior approval and only save_classroom_approval approves.
        self.session["classroom_grade_approvals"] = {key: True}
        self.session["classroom_approval_checkbox_%s" % key] = True
        self.assertEqual(
            self.decision(key, "2", action="correct",
                          reason="re-confirm same correction", mark=2)[0],
            "ok")
        self.assertNotIn(key, self.session.get("classroom_grade_approvals", {}))
        self.session["classroom_approval_checkbox_%s" % key] = True
        self.f["save_classroom_approval"](key)
        self.assertTrue(self.session["classroom_grade_approvals"][key])
        self.assertTrue(self.f["classroom_grade_details"](row)["approved"])

        # The durable stream NEVER records approval or publication: decisions
        # only, one type, whole journey.
        self.assertEqual(set(self.stream_event_types(row)),
                         {"question_review_decision"})
        self.assertTrue(self.f["apply_teacher_overrides"](key, row["result"]))
        self.assertEqual(
            [q["awarded_marks"] for q in row["result"]["questions"]], [1, 2, 1])
        self.assertEqual(
            [q["awarded_marks"] for q in row["ai_result"]["questions"]],
            [1, 1, 1])


if __name__ == "__main__":
    unittest.main()
