"""C6-B2 adversarial tests for the question-level teacher review workflow.

Offline only: every provider boundary is synthetic (plain listing dicts as the
production helpers return them, faked token/listing functions in the loaded app
scope, injected session state). No live Gemini/Classroom/Drive request is made
anywhere; the shared ``requests`` fake in the headless loader raises on any
unexpected HTTP call, so a passing run proves no network access happened.

The 30 enumerated adversarial cases (A01-A30) cover:
  - mark-value validation (bool, NaN, inf, negative, > max, fractional),
  - reason validation (empty, non-text, over-long),
  - decision binding freshness (AI marks, scheme digest, revision, selection),
  - decision evaluation rules (unreviewed, flagged stickiness, mark presence),
  - bounded audit state,
  - the app mutation boundary (identity, ownership, document provenance,
    live Classroom re-verification, correction routing through the single
    override system, evidence-change invalidation, zero provider calls on
    read-only rendering).
"""
import hashlib
import math
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import question_review as qr
from support import load_app, model_question, benchmark

SUBJECT = "teacher-a-subject"
OWNER = "google:" + SUBJECT
PDF = b"%PDF-1.7 c6b2 question review provenance fixture"


class RecordingUI:
    """Minimal Streamlit surface for read-only render assertions."""

    def __init__(self, session_state):
        self.session_state = session_state
        self.blocks = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def columns(self, count):
        return [self] * int(count)

    def expander(self, label, **kwargs):
        self.blocks.append(("expander", label))
        return self

    def dataframe(self, frame, **kwargs):
        self.blocks.append(("dataframe", frame))

    def radio(self, label, options, **kwargs):
        self.blocks.append(("radio", label))
        return list(options)[0]

    def selectbox(self, label, options, **kwargs):
        self.blocks.append(("selectbox", label))
        return list(options)[0]

    def number_input(self, label, **kwargs):
        return kwargs.get("value", 0)

    def text_input(self, *args, **kwargs):
        return ""

    def button(self, *args, **kwargs):
        self.blocks.append(("button", args[0] if args else ""))
        return False

    def metric(self, *args, **kwargs):
        return None

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def binding(**overrides):
    inputs = dict(assignment_id="manual", student_id="review-1",
                  question_number="1", variant="", row_revision="rev-0",
                  ai_marks=[1], scheme_digest="d" * 64)
    inputs.update(overrides)
    return qr.question_review_binding(**inputs)


class QuestionReviewValidationTests(unittest.TestCase):
    """A01-A07: corrected-mark and reason validation, no silent rounding."""

    def test_A01_boolean_mark_is_rejected(self):
        for value in (True, False):
            with self.subTest(value=value):
                ok, code = qr.validate_mark_value(value, 2)
                self.assertFalse(ok)
                self.assertEqual(code, "mark_not_a_number")

    def test_A02_nan_and_infinite_marks_are_rejected(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                ok, code = qr.validate_mark_value(value, 4)
                self.assertFalse(ok)
                self.assertEqual(code, "mark_not_finite")

    def test_A03_negative_mark_is_rejected(self):
        ok, code = qr.validate_mark_value(-1, 4)
        self.assertFalse(ok)
        self.assertEqual(code, "mark_negative")

    def test_A04_mark_above_scheme_maximum_is_rejected(self):
        ok, code = qr.validate_mark_value(5, 4)
        self.assertFalse(ok)
        self.assertEqual(code, "mark_exceeds_scheme_maximum")
        ok, code = qr.validate_mark_value(4, 4)
        self.assertTrue(ok)

    def test_A05_fractional_mark_is_rejected_not_rounded(self):
        for value in (1.5, 0.4, 2.999):
            with self.subTest(value=value):
                ok, code = qr.validate_mark_value(value, 4)
                self.assertFalse(ok)
                self.assertEqual(code, "mark_fractional_on_integer_rubric")

    def test_A06_non_numeric_and_unknown_maxima_fail_closed(self):
        for value in ("2", None, [1], {}, object()):
            with self.subTest(value=repr(value)):
                self.assertFalse(qr.validate_mark_value(value, 2)[0])
        for maximum in (None, "4", math.nan, -1, {}):
            with self.subTest(maximum=repr(maximum)):
                ok, code = qr.validate_mark_value(1, maximum)
                self.assertFalse(ok)
                self.assertEqual(code, "mark_maximum_unknown")

    def test_A07_reason_rules_are_enforced(self):
        self.assertEqual(qr.validate_reason("  ok  "), (True, "ok"))
        for reason in (None, 5, [], "", "   "):
            with self.subTest(reason=repr(reason)):
                self.assertFalse(qr.validate_reason(reason)[0])
        ok, code = qr.validate_reason("x" * (qr.MAX_REASON_LENGTH + 1))
        self.assertFalse(ok)
        self.assertEqual(code, "reason_too_long")

    def test_mark_sequence_validates_against_maxima(self):
        ok, code, normalized = qr.validate_mark_sequence([1, 0, 2], [2, 2, 2])
        self.assertTrue(ok)
        self.assertEqual(normalized, [1, 0, 2])
        for marks, maxima in (([1, 5], [2, 2]), ([1, 1.5], [2, 2]),
                              ([], [2]), ([1], "2"), ([1, 2], [2]),
                              ("1", [2]), (None, [2])):
            with self.subTest(marks=marks, maxima=maxima):
                ok, code, normalized = qr.validate_mark_sequence(marks, maxima)
                self.assertFalse(ok)
                self.assertEqual(normalized, [])


class QuestionReviewBindingTests(unittest.TestCase):
    """A08-A13: decision binding invalidates on any evidence change."""

    def test_A08_ai_mark_change_invalidates_the_binding(self):
        current = binding(ai_marks=[1])
        self.assertFalse(qr.binding_matches(current, binding(ai_marks=[2])))
        self.assertTrue(qr.binding_matches(current, binding(ai_marks=[1])))

    def test_A09_scheme_digest_change_invalidates_the_binding(self):
        current = binding(scheme_digest="d" * 64)
        self.assertFalse(
            qr.binding_matches(current, binding(scheme_digest="e" * 64)))

    def test_A10_row_revision_change_invalidates_the_binding(self):
        current = binding(row_revision="rev-0")
        self.assertFalse(
            qr.binding_matches(current, binding(row_revision="rev-1")))

    def test_A11_selection_change_invalidates_the_binding(self):
        current = binding(variant='["1", "2"]')
        self.assertFalse(
            qr.binding_matches(current, binding(variant='["1", "3"]')))

    def test_A12_question_and_assignment_are_part_of_the_binding(self):
        current = binding()
        self.assertFalse(
            qr.binding_matches(current, binding(question_number="2")))
        self.assertFalse(qr.binding_matches(current, binding(student_id="x")))
        self.assertFalse(qr.binding_matches(current, binding(assignment_id="y")))

    def test_A13_malformed_bindings_never_match(self):
        current = binding()
        for other in (None, "", "a" * 63, "a" * 65, "A" * 64, 42, {}, PDF):
            with self.subTest(other=repr(other)):
                self.assertFalse(qr.binding_matches(current, other))
                self.assertFalse(qr.binding_matches(other, current))


class QuestionReviewDecisionTests(unittest.TestCase):
    """A14-A19: decision evaluation and bounded audit state."""

    def test_A14_unreviewed_status_is_not_submittable(self):
        code, status, state = qr.evaluate_question_review(
            status="unreviewed", binding=binding(), reason="n/a")
        self.assertEqual(code, "unreviewed_not_submittable")
        self.assertEqual(state, {})

    def test_A15_corrected_requires_valid_marks_and_reason(self):
        code, _, state = qr.evaluate_question_review(
            status="corrected", binding=binding(), corrected_marks=[],
            reason="transcription mismatch")
        self.assertEqual(code, "corrected_marks_required")
        code, _, state = qr.evaluate_question_review(
            status="corrected", binding=binding(), corrected_marks=[2],
            reason="   ")
        self.assertTrue(code.startswith("reason_required"))
        code, _, state = qr.evaluate_question_review(
            status="corrected", binding=binding(), corrected_marks=[2],
            reason="transcription mismatch")
        self.assertEqual(code, "ok")
        self.assertEqual(state["corrected_marks"], [2])

    def test_A16_approved_must_not_carry_corrected_marks(self):
        code, _, _ = qr.evaluate_question_review(
            status="approved", binding=binding(), corrected_marks=[2],
            reason="looks fine")
        self.assertEqual(code, "corrected_marks_not_expected")

    def test_A17_live_flag_cannot_be_silently_approved(self):
        flagged = binding()
        code, _, _ = qr.evaluate_question_review(
            status="approved", binding=flagged, reason="",
            prior_status="flagged", prior_binding=flagged)
        self.assertEqual(code, "flagged_decision_requires_reason")
        code, _, state = qr.evaluate_question_review(
            status="approved", binding=flagged, reason="re-checked scan",
            prior_status="flagged", prior_binding=flagged)
        self.assertEqual(code, "ok")
        self.assertEqual(state["status"], "approved")

    def test_A18_flag_on_stale_evidence_is_superseded_not_sticky(self):
        code, _, state = qr.evaluate_question_review(
            status="approved", binding=binding(), reason="",
            prior_status="flagged", prior_binding=binding(row_revision="old"))
        self.assertEqual(code, "ok")
        self.assertEqual(state["status"], "approved")

    def test_A19_bounded_audit_events_and_summary(self):
        events = [qr.make_review_event(
            assignment_id="manual", student_id="review-1",
            question_number=str(index), action="approved", actor=OWNER,
            binding=binding(), timestamp=str(index))
            for index in range(qr.MAX_AUDIT_EVENTS + 25)]
        trimmed = qr.bounded_events(events)
        self.assertEqual(len(trimmed), qr.MAX_AUDIT_EVENTS)
        self.assertEqual(trimmed[0]["question_number"], "25")
        self.assertEqual(trimmed[-1]["question_number"],
                         str(qr.MAX_AUDIT_EVENTS + 24))
        for bad in (None, "events", {}, [1, "x"]):
            with self.subTest(bad=repr(bad)):
                self.assertEqual(qr.bounded_events(bad), [])
        self.assertEqual(qr.bounded_events(events, 0), [])
        counts = qr.summarize_question_review({
            "a": {"status": "approved"}, "b": {"status": "flagged"},
            "c": {"status": "made-up"}, "d": "junk", "e": None})
        self.assertEqual(counts["approved"], 1)
        self.assertEqual(counts["flagged"], 1)
        self.assertEqual(counts["unreviewed"], 1)
        self.assertEqual(counts["corrected"], 0)


class QuestionReviewAppBoundaryTests(unittest.TestCase):
    """A20-A30: the app mutation boundary, fully intercepted and offline."""

    def setUp(self):
        self.f = load_app()
        self.session = {}
        self.f["st"] = SimpleNamespace(
            session_state=self.session,
            user=SimpleNamespace(sub=SUBJECT))
        self.f["google_logged_in"] = lambda: True
        self.f["authenticated_google_identity"] = lambda: SUBJECT
        self.f["pd"] = SimpleNamespace(DataFrame=lambda rows: rows)
        self.mark_pdf()
        self.row = self.manual_row()

    def mark_pdf(self):
        """Record an owned manual document grant (server-side provenance)."""
        self.f["remember_manual_upload"](PDF)
        self.session["last_manual_context"] = hashlib.sha256(PDF).hexdigest()

    def question(self, awarded=1, maximum=2, number="1"):
        sample = model_question(benchmark()["questions"][0], "partial")
        sample["question_number"] = number
        sample["awarded_marks"] = awarded
        sample["max_marks"] = maximum
        return sample

    def manual_row(self, awarded=1, review_key="review-1"):
        result = self.f["normalise_result"](
            {"questions": [self.question(awarded=awarded)]})
        return {"status": "Marked", "review_key": review_key,
                "report_owner": OWNER, "result": result,
                "ai_result": self.f["normalise_result"](
                    {"questions": [self.question(awarded=awarded)]}),
                "totals": {"earned": awarded, "available": 2,
                           "percentage": 50.0, "provisional_grade": "5"},
                "groups": [], "student_name": "Synthetic",
                "marking_model": "fake", "audit_model": "Not run"}

    def install_row(self, row):
        self.session["last_batch_results"] = [row]
        return row

    def decision(self, action="approve", reason="", mark=None, **overrides):
        arguments = dict(student_key=self.row["review_key"],
                         question_number="1", action=action, reason=reason,
                         corrected_mark=mark)
        arguments.update(overrides)
        self.f["apply_question_review_decision"](**arguments)
        return self.session.get("question_review_notice")

    def stored(self, key="review-1::q1"):
        return self.session.get("question_review_decisions", {}).get(key)

    def classroom_row(self, revision="rev-1", context="c" * 64):
        row = self.manual_row(review_key="classroom_row-1")
        row.update({"classroom_course_id": "course-1",
                    "classroom_assignment_id": "work-1",
                    "classroom_context": context,
                    "revision": revision, "selected_questions": ["1"]})
        return row

    def enable_classroom(self, courses=None, assignments=None):
        import test_c6b2_security as fixture
        self.courses = Mock(return_value=list(fixture.COURSES))
        self.assignments = Mock(return_value=list(fixture.ASSIGNMENTS))
        self.f["list_teacher_courses"] = self.courses
        self.f["list_assignments"] = self.assignments
        self.f["get_google_access_token"] = lambda: "synthetic-token"
        self.f["record_classroom_grants"](
            list(fixture.COURSES), list(fixture.ASSIGNMENTS), "course-1")

    def test_A20_anonymous_session_is_rejected(self):
        self.install_row(self.row)
        self.f["authenticated_google_identity"] = lambda: None
        notice = self.decision()
        self.assertEqual(notice[0], "authentication_required")
        self.assertIsNone(self.stored())

    def test_A21_foreign_or_unowned_row_is_rejected(self):
        # Row owned by another teacher.
        foreign = self.install_row(self.manual_row())
        foreign["report_owner"] = "google:teacher-b-subject"
        self.assertEqual(self.decision()[0], "review_context_unavailable")
        self.assertIsNone(self.stored())
        # Row not present in any owner-verified store at all.
        self.session["last_batch_results"] = []
        self.assertEqual(self.decision()[0], "review_context_unavailable")
        # Row whose review_key does not match the one being reviewed.
        mismatch = self.install_row(self.manual_row(review_key="other-key"))
        self.session["teacher_overrides"] = {}
        self.assertEqual(self.decision()[0], "review_context_unavailable")

    def test_A22_manual_row_without_owned_document_is_rejected(self):
        self.install_row(self.row)
        self.session["last_manual_context"] = hashlib.sha256(
            b"other document").hexdigest()
        notice = self.decision(action="approve")
        self.assertEqual(notice[0], "manual_document_provenance_required")
        self.assertIsNone(self.stored())

    def test_A23_owned_document_approval_is_recorded_with_actor(self):
        self.install_row(self.row)
        notice = self.decision(action="approve")
        self.assertEqual(notice[0], "ok")
        record = self.stored()
        self.assertEqual(record["status"], "approved")
        self.assertEqual(record["actor"], OWNER)
        self.assertEqual(len(record["binding"]), 64)
        events = self.session["question_review_events"]
        self.assertEqual(events[-1]["action"], "approved")
        self.assertEqual(events[-1]["actor"], OWNER)

    def test_A24_correction_routes_through_the_single_override_system(self):
        self.install_row(self.row)
        notice = self.decision(action="correct", reason="scheme B1 missed",
                               mark=2)
        self.assertEqual(notice[0], "ok")
        overrides = self.session["teacher_overrides"]["review-1"]
        self.assertEqual(overrides["1"], 2)
        self.assertEqual(
            self.session["teacher_override_reasons"]["review-1"]["1"],
            "scheme B1 missed")
        # Original AI marks preserved on the question record.
        applied = self.row["result"]["questions"][0]
        self.f["apply_teacher_overrides"]("review-1", self.row["result"])
        self.assertEqual(applied["teacher_override"]["original_marks"], 1)
        self.assertEqual(applied["awarded_marks"], 2)
        self.assertEqual(self.stored()["status"], "corrected")

    def test_A25_invalid_corrections_write_no_override(self):
        self.install_row(self.row)
        for mark in (True, math.nan, math.inf, -1, 9, 1.5, None):
            with self.subTest(mark=mark):
                notice = self.decision(action="correct", reason="bad input",
                                       mark=mark)
                self.assertNotEqual(notice[0], "ok")
        self.assertNotIn("review-1", self.session.get("teacher_overrides", {}))
        self.assertIsNone(self.stored())

    def test_A26_correction_without_reason_is_rejected(self):
        self.install_row(self.row)
        for reason in ("", "   ", None, 5):
            with self.subTest(reason=reason):
                notice = self.decision(action="correct", reason=reason, mark=2)
                self.assertTrue(notice[0].startswith("reason_required"))
        for reason in ("", "   "):
            with self.subTest(reason=reason, action="flag"):
                notice = self.decision(action="flag", reason=reason)
                self.assertTrue(notice[0].startswith("reason_required"))
        self.assertNotIn("review-1", self.session.get("teacher_overrides", {}))

    def test_A27_classroom_decision_requires_live_reverification(self):
        self.enable_classroom()
        row = self.install_row(self.classroom_row())
        # Revoked inside the cached-grant window: fresh listing lacks the
        # course, so the privileged decision is refused.
        self.courses.return_value = [{"id": "course-2", "name": "Other"}]
        notice = self.decision(student_key=row["review_key"])
        self.assertEqual(notice[0], "course_access_unverified")
        self.assertIsNone(self.stored("classroom_row-1::q1"))
        # Fresh trusted listing: the same decision now succeeds and binds.
        import test_c6b2_security as fixture
        self.courses.return_value = list(fixture.COURSES)
        notice = self.decision(student_key=row["review_key"])
        self.assertEqual(notice[0], "ok")
        self.assertEqual(self.stored("classroom_row-1::q1")["status"],
                         "approved")

    def test_A28_classroom_api_failure_fails_closed_at_the_boundary(self):
        self.enable_classroom()
        row = self.install_row(self.classroom_row())
        self.courses.side_effect = RuntimeError("network down")
        notice = self.decision(student_key=row["review_key"])
        self.assertEqual(notice[0], "classroom_verification_unavailable")
        self.assertIsNone(self.stored("classroom_row-1::q1"))
        self.assertEqual(self.assignments.call_count, 0)

    def test_A29_evidence_change_invalidates_a_stored_decision(self):
        self.install_row(self.row)
        self.assertEqual(self.decision()[0], "ok")
        decision, invalidated = self.f["stored_question_review"](
            "review-1", "1", self.row)
        self.assertFalse(invalidated)
        self.assertEqual(decision["status"], "approved")
        # (a) The mark scheme behind the row changes.
        changed = self.manual_row()
        changed["snapshot_context"] = hashlib.sha256(
            b"new scheme").hexdigest()
        decision, invalidated = self.f["stored_question_review"](
            "review-1", "1", changed)
        self.assertIsNone(decision)
        self.assertTrue(invalidated)
        # (b) The pristine AI marks change (regrade).
        changed = self.manual_row(awarded=0)
        decision, invalidated = self.f["stored_question_review"](
            "review-1", "1", changed)
        self.assertIsNone(decision)
        self.assertTrue(invalidated)
        # (c) A Classroom resubmission changes the row revision.
        self.enable_classroom()
        crow = self.install_row(self.classroom_row())
        self.assertEqual(
            self.decision(student_key=crow["review_key"])[0], "ok")
        decision, invalidated = self.f["stored_question_review"](
            crow["review_key"], "1", crow)
        self.assertFalse(invalidated)
        resubmitted = self.classroom_row(revision="rev-2")
        self.session["last_batch_results"] = [resubmitted]
        decision, invalidated = self.f["stored_question_review"](
            crow["review_key"], "1", resubmitted)
        self.assertIsNone(decision)
        self.assertTrue(invalidated)

    def test_A30_read_only_rendering_makes_no_classroom_request(self):
        self.enable_classroom()
        row = self.install_row(self.classroom_row())
        row["groups"] = self.f["make_group_rows"](row["result"]["questions"])
        self.f["st"] = RecordingUI(self.session)
        self.f["render_student_report"](
            "Synthetic", row["result"], row["totals"], row["groups"],
            "fake", "Not run", student_key=row["review_key"], review_row=row)
        self.assertEqual(self.courses.call_count, 0)
        self.assertEqual(self.assignments.call_count, 0)
        # With a valid review context the per-question controls DO render.
        rendered = [entry for entry in self.f["st"].blocks
                    if entry[0] == "button"
                    and entry[1].startswith("Submit question review")]
        self.assertEqual(len(rendered), 1)
        # Without a review row, no review controls render at all.
        self.f["st"] = RecordingUI(self.session)
        self.f["render_student_report"](
            "Synthetic", row["result"], row["totals"], row["groups"],
            "fake", "Not run", student_key=row["review_key"])
        self.assertEqual(
            [entry for entry in self.f["st"].blocks if entry[0] == "button"
             and entry[1].startswith("Submit question review")], [])
        self.assertEqual(self.courses.call_count, 0)
        self.assertEqual(self.assignments.call_count, 0)

    def test_manual_row_without_review_context_renders_no_controls(self):
        self.install_row(self.row)
        self.f["st"] = RecordingUI(self.session)
        self.f["render_student_report"](
            "Synthetic", self.row["result"], self.row["totals"], [],
            "fake", "Not run", student_key=self.row["review_key"])
        self.assertEqual(
            [entry for entry in self.f["st"].blocks if entry[0] == "button"
             and entry[1].startswith("Submit question review")], [])

    def test_widget_callback_reads_only_scalar_snapshots(self):
        self.install_row(self.row)
        self.session["qreview_status_review-1_1"] = "approve"
        self.session["qreview_reason_review-1_1"] = "checked scan"
        self.session["qreview_mark_review-1_1"] = 9
        self.f["save_question_review_from_widgets"]("review-1", "1")
        self.assertEqual(self.stored()["status"], "approved")


if __name__ == "__main__":
    unittest.main()
