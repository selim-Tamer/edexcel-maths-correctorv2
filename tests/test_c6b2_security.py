"""C6-B2 adversarial tests for F-1 classroom freshness and F-2 provenance.

Offline only: the provider boundary is always synthetic (plain listing dicts
exactly as the production helpers return them, injected clocks, faked token
and listing functions in the loaded app scope). No live Gemini/Classroom/
Drive request is made anywhere; the shared ``requests`` fake in the headless
loader raises on any unexpected HTTP call.

F-1 — Classroom permission freshness:
  privileged question-review decisions must reverify teacher and assignment
  permission against FRESH trusted listings at the decision boundary, bind to
  the exact teacher/course/coursework, and fail closed on revocation, missing
  metadata, changed revisions, stale grants, API failures or anonymous use.

F-2 — Manual PDF provenance:
  scheme-level bindings must prove that the digest came from a session-owned
  document; grant eviction or foreign sessions invalidate effective approval;
  approval displays never rely on stored metadata alone.
"""
import copy
import hashlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scheme_review as sr
import teacher_authorization as ta
from scheme_authorization import AuthorizationError, identifier

from support import SOURCE, load_app

COURSES = [
    {"id": "course-1", "name": "Year 11 Maths", "section": "A"},
    {"id": "course-2", "name": "Year 10 Maths", "section": "B"},
]


def assignment(work_id, course_id="course-1", **overrides):
    item = {"id": work_id, "courseId": course_id, "title": "Algebra test",
            "description": "Show all working", "maxPoints": 20,
            "workType": "ASSIGNMENT", "state": "PUBLISHED",
            "creationTime": "2026-01-01T00:00:00Z",
            "updateTime": "2026-01-02T00:00:00Z"}
    item.update(overrides)
    return item


ASSIGNMENTS = [assignment("work-1"), assignment("work-2")]
ASSIGNMENT_KEY = "course-1/work-1"
PDF_V1 = b"%PDF-1.7 c6b2 provenance fixture v1"
PDF_V2 = b"%PDF-1.7 c6b2 provenance fixture v2"
SUBJECT = "teacher-a"


class FakeClock:
    def __init__(self, now=1_700_000_000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def signed_in(subject=SUBJECT, clock=None):
    state = {}
    session = ta.current_teacher_session(state, subject,
                                          clock=clock or FakeClock())
    assert session is not None
    return state, session


def grant_all(state, subject=SUBJECT, assignments=None, courses=None):
    ta.remember_classroom_grants(
        state, subject, COURSES if courses is None else courses, "course-1",
        ASSIGNMENTS if assignments is None else assignments)


def manual_review_record():
    return sr.build_review(
        {"questions": [{"question_number": "1", "max_marks": 1,
                        "criteria": [{"criterion_id": "b", "code": "B1",
                                      "marks": 1, "source_page": 1,
                                      "source_evidence": "B1 correct"}]}]},
        assignment_id=None,
        evidence_document={"page_count": 1, "pages": [
            {"page": 1, "text": "Question 1 B1 correct",
             "status": "extracted"}]},
        question_mapping={"matches": [{"question_id": "1",
                                       "certainty": "explicit",
                                       "pages": [1], "references": []}],
                          "unresolved": []})


class ClassroomFreshnessTests(unittest.TestCase):
    """F-1: live re-verification at the privileged decision boundary."""

    def test_revoked_course_access_detected_inside_cache_window(self):
        clock = FakeClock()
        state, _ = signed_in(clock=clock)
        grant_all(state)
        # The cached grant is seconds old, but the FRESH listing no longer
        # contains the course: provider-side revocation must be detected.
        with self.assertRaisesRegex(ta.TeacherAuthorizationError,
                                    "course_access_unverified"):
            ta.reverify_assignment_access(
                state, SUBJECT, course_id="course-1",
                coursework_id="work-1", trusted_courses=[COURSES[1]],
                trusted_assignments=ASSIGNMENTS)

    def test_cross_course_decision_is_refused(self):
        state, _ = signed_in()
        grant_all(state)
        # The trusted fresh listing proves teacher access to course-2 only,
        # while the decision targets a course-1 row: refused.
        with self.assertRaisesRegex(ta.TeacherAuthorizationError,
                                    "course_access_unverified"):
            ta.reverify_assignment_access(
                state, SUBJECT, course_id="course-1",
                coursework_id="work-1", trusted_courses=[COURSES[1]],
                trusted_assignments=ASSIGNMENTS)

    def test_cross_teacher_decision_is_refused(self):
        state, _ = signed_in("teacher-a")
        grant_all(state, subject="teacher-a")
        # Teacher B's authenticated session has no grants for A's course:
        # switching identity starts empty and never inherits.
        with self.assertRaises(ta.TeacherAuthorizationError):
            ta.reverify_assignment_access(
                state, "teacher-b", course_id="course-1",
                coursework_id="work-1", trusted_courses=COURSES,
                trusted_assignments=ASSIGNMENTS)

    def test_missing_assignment_or_wrong_work_type_refused(self):
        state, _ = signed_in()
        grant_all(state)
        with self.assertRaisesRegex(ta.TeacherAuthorizationError,
                                    "assignment_access_unverified"):
            ta.reverify_assignment_access(
                state, SUBJECT, course_id="course-1",
                coursework_id="work-9", trusted_courses=COURSES,
                trusted_assignments=ASSIGNMENTS)
        wrong_type = [assignment("work-1", workType="SHORT_ANSWER_QUESTION")]
        with self.assertRaisesRegex(ta.TeacherAuthorizationError,
                                    "assignment_access_unverified"):
            ta.reverify_assignment_access(
                state, SUBJECT, course_id="course-1",
                coursework_id="work-1", trusted_courses=COURSES,
                trusted_assignments=wrong_type)

    def test_missing_or_malformed_metadata_fails_closed(self):
        state, _ = signed_in()
        grant_all(state)
        for course_id, work_id in (("course-1", ""), ("course-1", "   "),
                                   ("", "work-1"), (None, "work-1"),
                                   ("course-1", None)):
            with self.subTest(course=course_id, work=work_id), \
                    self.assertRaises(ta.TeacherAuthorizationError):
                ta.reverify_assignment_access(
                    state, SUBJECT, course_id=course_id,
                    coursework_id=work_id, trusted_courses=COURSES,
                    trusted_assignments=ASSIGNMENTS)

    def test_assignment_edit_changes_revision_and_is_rejected(self):
        clock = FakeClock()
        state, _ = signed_in(clock=clock)
        grant_all(state)
        edited = [dict(ASSIGNMENTS[0], updateTime="2026-02-02T00:00:00Z"),
                  ASSIGNMENTS[1]]
        with self.assertRaisesRegex(ta.TeacherAuthorizationError,
                                    "assignment_revision_changed"):
            ta.reverify_assignment_access(
                state, SUBJECT, course_id="course-1",
                coursework_id="work-1", trusted_courses=COURSES,
                trusted_assignments=edited)
        # Points edits are detected too, even without a timestamp change.
        regraded = [dict(ASSIGNMENTS[0], maxPoints=25), ASSIGNMENTS[1]]
        with self.assertRaisesRegex(ta.TeacherAuthorizationError,
                                    "assignment_revision_changed"):
            ta.reverify_assignment_access(
                state, SUBJECT, course_id="course-1",
                coursework_id="work-1", trusted_courses=COURSES,
                trusted_assignments=regraded)

    def test_stale_grant_fails_closed_even_with_fresh_listing(self):
        clock = FakeClock()
        state, _ = signed_in(clock=clock)
        grant_all(state)
        clock.advance(ta.ASSIGNMENT_GRANT_MAX_AGE_SECONDS + 1)
        with self.assertRaisesRegex(ta.TeacherAuthorizationError,
                                    "assignment_grant_expired"):
            ta.reverify_assignment_access(
                state, SUBJECT, course_id="course-1",
                coursework_id="work-1", trusted_courses=COURSES,
                trusted_assignments=ASSIGNMENTS)
        # A fresh trusted listing re-records the grant and unblocks it.
        grant_all(state)
        binding = ta.reverify_assignment_access(
            state, SUBJECT, course_id="course-1", coursework_id="work-1",
            trusted_courses=COURSES, trusted_assignments=ASSIGNMENTS)
        self.assertEqual(binding["revision"],
                         ta.assignment_revision("course-1", ASSIGNMENTS[0]))

    def test_missing_or_untrusted_listing_shapes_fail_closed(self):
        state, _ = signed_in()
        grant_all(state)
        for courses, work_items in (
                (None, ASSIGNMENTS), ("not-a-list", ASSIGNMENTS),
                ({"id": "course-1"}, ASSIGNMENTS), (COURSES, None),
                (COURSES, {"id": "work-1"}), (COURSES, "not-a-list")):
            with self.subTest(courses=courses, work=work_items), \
                    self.assertRaisesRegex(ta.TeacherAuthorizationError,
                                           "classroom_verification_unavailable"):
                ta.reverify_assignment_access(
                    state, SUBJECT, course_id="course-1",
                    coursework_id="work-1", trusted_courses=courses,
                    trusted_assignments=work_items)

    def test_ungrant_never_proves_access(self):
        state, _ = signed_in()
        # Verified-looking fresh listings WITHOUT a recorded grant are not
        # enough for a privileged decision: grants come only from the
        # recording path that ran the same trusted listing earlier.
        with self.assertRaisesRegex(ta.TeacherAuthorizationError,
                                    "assignment_access_unverified"):
            ta.reverify_assignment_access(
                state, SUBJECT, course_id="course-1",
                coursework_id="work-1", trusted_courses=COURSES,
                trusted_assignments=ASSIGNMENTS)

    def test_anonymous_session_is_rejected_without_creating_grants(self):
        state = {}
        with self.assertRaisesRegex(ta.TeacherAuthorizationError,
                                    "authenticated_teacher_required"):
            ta.reverify_assignment_access(
                state, None, course_id="course-1", coursework_id="work-1",
                trusted_courses=COURSES, trusted_assignments=ASSIGNMENTS)
        stored = state.get(ta.AUTH_STATE_KEY)
        self.assertTrue(stored is None or not stored["assignment_grants"])

    def test_binding_is_bound_to_exact_teacher_course_and_coursework(self):
        state, _ = signed_in()
        grant_all(state)
        binding = ta.reverify_assignment_access(
            state, SUBJECT, course_id="course-1", coursework_id="work-1",
            trusted_courses=COURSES, trusted_assignments=ASSIGNMENTS)
        self.assertEqual(binding["owner"], "google:" + SUBJECT)
        self.assertEqual(binding["course_id"], "course-1")
        self.assertEqual(binding["coursework_id"], "work-1")
        self.assertEqual(binding["assignment_key"], ASSIGNMENT_KEY)
        self.assertEqual(len(binding["revision"]), 64)
        self.assertEqual(
            set(binding),
            {"owner", "course_id", "coursework_id", "assignment_key",
             "revision", "verified_at"})


class ManualProvenanceTests(unittest.TestCase):
    """F-2: session-owned document provenance for privileged bindings."""

    def test_direct_bind_with_arbitrary_digest_is_refused(self):
        state, session = signed_in()
        ta.record_manual_pdf_grant(state, SUBJECT, PDF_V1)
        context = session["authority"].authorize(None)
        # The digest below is well-formed but NOT proven by this session's
        # document store: the public binding API must refuse it.
        with self.assertRaisesRegex(sr.SchemeReviewApprovalError,
                                    "manual_document_provenance_required"):
            sr.bind_scheme_review(manual_review_record(), context,
                                  review_id="r-forged",
                                  document_digest=hashlib.sha256(
                                      PDF_V2).hexdigest())
        self.assertEqual(session["authority"]._reviews, {})

    def test_direct_bind_with_malformed_digest_is_refused(self):
        state, session = signed_in()
        ta.record_manual_pdf_grant(state, SUBJECT, PDF_V1)
        context = session["authority"].authorize(None)
        for digest in (None, "", "a" * 63, "a" * 65, "A" * 64,
                       "z" * 64, {}, 42, PDF_V1):
            with self.subTest(digest=repr(digest)), \
                    self.assertRaises(sr.SchemeReviewApprovalError):
                sr.bind_scheme_review(manual_review_record(), context,
                                      review_id="r-bad",
                                      document_digest=digest)

    def test_foreign_session_document_is_refused(self):
        state_a, _ = signed_in("teacher-a")
        ta.record_manual_pdf_grant(state_a, "teacher-a", PDF_V1)
        state_b, session_b = signed_in("teacher-b")
        ta.record_manual_pdf_grant(state_b, "teacher-b", PDF_V2)
        context_b = session_b["authority"].authorize(None)
        # B owns a document, but not A's document: binding A's digest from
        # B's session fails provenance.
        with self.assertRaisesRegex(sr.SchemeReviewApprovalError,
                                    "manual_document_provenance_required"):
            sr.bind_scheme_review(manual_review_record(), context_b,
                                  review_id="r-cross",
                                  document_digest=hashlib.sha256(
                                      PDF_V1).hexdigest())

    def test_sanctioned_bind_still_works_and_is_digest_exact(self):
        state, session = signed_in()
        record = ta.bind_manual_review(
            state, SUBJECT, manual_review_record(), review_id="r1",
            pdf_bytes=PDF_V1)
        self.assertEqual(record["approval_binding"]["document_digest"],
                         hashlib.sha256(PDF_V1).hexdigest())
        context = session["authority"].authorize(None)
        sr.approve_scheme_review(record, context, record["scheme_revision"])
        self.assertEqual(sr.approval_state(record, context,
                                           record["scheme_revision"])[0],
                         "approved")

    def test_grant_eviction_invalidates_effective_approval(self):
        clock = FakeClock()
        state, session = signed_in(clock=clock)
        record = ta.bind_manual_review(
            state, SUBJECT, manual_review_record(), review_id="r1",
            pdf_bytes=PDF_V1)
        context = session["authority"].authorize(None)
        sr.approve_scheme_review(record, context, record["scheme_revision"])
        self.assertEqual(sr.approval_state(record, context,
                                           record["scheme_revision"])[0],
                         "approved")
        # The document grant is evicted by later uploads (bounded store).
        for index in range(ta.MAX_MANUAL_GRANTS + 5):
            clock.advance(1)
            ta.record_manual_pdf_grant(state, SUBJECT,
                                       ("later-upload-%d" % index).encode())
        digest = hashlib.sha256(PDF_V1).hexdigest()
        self.assertNotIn(digest,
                         state[ta.AUTH_STATE_KEY]["manual_grants"])
        # The approval display must NOT rely on stored approval metadata:
        # live provenance re-verification fails, so it is not approved.
        label, reason = sr.approval_state(record, context,
                                          record["scheme_revision"])
        self.assertNotEqual(label, "approved")
        self.assertEqual(reason, "manual_document_provenance_required")
        # Pruning permanently drops the entry so it cannot resurrect.
        session["authority"].prune_superseded()
        self.assertNotIn("r1", session["authority"]._reviews)
        with self.assertRaises(sr.SchemeReviewApprovalError):
            sr.approve_scheme_review(record, context,
                                     record["scheme_revision"])

    def test_forged_binding_digest_never_approves(self):
        state, session = signed_in()
        ta.record_manual_pdf_grant(state, SUBJECT, PDF_V2)
        record = ta.bind_manual_review(
            state, SUBJECT, manual_review_record(), review_id="r1",
            pdf_bytes=PDF_V1)
        context = session["authority"].authorize(None)
        sr.approve_scheme_review(record, context, record["scheme_revision"])
        forged = copy.deepcopy(record)
        forged["approval_binding"]["document_digest"] = hashlib.sha256(
            PDF_V2).hexdigest()
        label, _ = sr.approval_state(forged, context,
                                     forged["scheme_revision"])
        self.assertNotEqual(label, "approved")

    def test_stored_teacher_metadata_is_never_effective_approval(self):
        state, session = signed_in()
        record = ta.bind_manual_review(
            state, SUBJECT, manual_review_record(), review_id="r1",
            pdf_bytes=PDF_V1)
        context = session["authority"].authorize(None)
        record["teacher"] = {"status": sr.STATUS_APPROVED,
                             "approved_by": "google:" + SUBJECT,
                             "approved_revision": record["scheme_revision"]}
        label, _ = sr.approval_state(record, context,
                                     record["scheme_revision"])
        self.assertNotEqual(label, "approved")
        self.assertNotEqual(sr.determine_status(record, context),
                            sr.STATUS_APPROVED)

    def test_manual_document_owned_read_only_and_fail_closed(self):
        state = {}
        digest = hashlib.sha256(PDF_V1).hexdigest()
        # No state at all: False, and the check must not mint authorization.
        self.assertFalse(ta.manual_document_owned(state, SUBJECT, digest))
        self.assertNotIn(ta.AUTH_STATE_KEY, state)
        ta.record_manual_pdf_grant(state, SUBJECT, PDF_V1)
        self.assertTrue(ta.manual_document_owned(state, SUBJECT, digest))
        # Wrong subject, foreign digest, malformed inputs, anonymous.
        self.assertFalse(ta.manual_document_owned(state, "teacher-b", digest))
        self.assertFalse(ta.manual_document_owned(state, None, digest))
        self.assertFalse(ta.manual_document_owned(
            state, SUBJECT, hashlib.sha256(PDF_V2).hexdigest()))
        for bad in (None, "", "a" * 63, "A" * 64, 42, {}, PDF_V1):
            with self.subTest(digest=repr(bad)):
                self.assertFalse(ta.manual_document_owned(
                    state, SUBJECT, bad))
        # Tampered (unowned) stored state fails the shape check too.
        tampered = {ta.AUTH_STATE_KEY: dict(state[ta.AUTH_STATE_KEY],
                                            subject="intruder")}
        self.assertFalse(ta.manual_document_owned(tampered, "intruder",
                                                  digest))


class AppReverificationWrapperTests(unittest.TestCase):
    """F-1 app surface: the decision-boundary wrapper, fully intercepted."""

    def setUp(self):
        self.f = load_app()
        self.session = {}
        self.f["st"] = SimpleNamespace(
            session_state=self.session,
            user=SimpleNamespace(sub="teacher-a-subject"))
        self.f["google_logged_in"] = lambda: True
        self.f["authenticated_google_identity"] = lambda: "teacher-a-subject"
        self.f["get_google_access_token"] = lambda: "synthetic-token"
        self.courses_listing = Mock(return_value=list(COURSES))
        self.assignments_listing = Mock(return_value=list(ASSIGNMENTS))
        self.f["list_teacher_courses"] = self.courses_listing
        self.f["list_assignments"] = self.assignments_listing
        self.row = {"classroom_course_id": "course-1",
                    "classroom_assignment_id": "work-1",
                    "submission_id": "submission-1", "revision": "rev-1"}

    def grant(self):
        self.f["record_classroom_grants"](list(COURSES), list(ASSIGNMENTS),
                                          "course-1")

    def test_fresh_synthetic_listing_permits_and_binds_the_decision(self):
        self.grant()
        binding, code = self.f["reverify_classroom_review_access"](self.row)
        self.assertIsNone(code)
        self.assertEqual(binding["owner"], "google:teacher-a-subject")
        self.assertEqual(binding["coursework_id"], "work-1")
        self.courses_listing.assert_called_once_with("synthetic-token")
        self.assignments_listing.assert_called_once_with(
            "synthetic-token", "course-1")

    def test_revoked_access_inside_cached_grant_window_denied(self):
        self.grant()
        self.courses_listing.return_value = [COURSES[1]]
        binding, code = self.f["reverify_classroom_review_access"](self.row)
        self.assertIsNone(binding)
        self.assertEqual(code, "course_access_unverified")

    def test_classroom_api_failure_during_reverification_fails_closed(self):
        self.grant()
        self.courses_listing.side_effect = RuntimeError("network down")
        binding, code = self.f["reverify_classroom_review_access"](self.row)
        self.assertIsNone(binding)
        self.assertEqual(code, "classroom_verification_unavailable")
        # Assignment listing never runs after the first failure.
        self.assertEqual(self.assignments_listing.call_count, 0)

    def test_missing_token_fails_closed_without_any_listing(self):
        self.grant()
        self.f["get_google_access_token"] = lambda: ""
        binding, code = self.f["reverify_classroom_review_access"](self.row)
        self.assertIsNone(binding)
        self.assertEqual(code, "classroom_verification_unavailable")
        self.assertEqual(self.courses_listing.call_count, 0)
        self.assertEqual(self.assignments_listing.call_count, 0)

    def test_row_without_assignment_metadata_is_denied(self):
        for row in ({}, {"classroom_course_id": "course-1"},
                    {"classroom_assignment_id": "work-1"},
                    {"classroom_course_id": "", "classroom_assignment_id": ""},
                    {"classroom_course_id": None,
                     "classroom_assignment_id": None}, None, "row"):
            with self.subTest(row=row):
                binding, code = (
                    self.f["reverify_classroom_review_access"](row))
                self.assertIsNone(binding)
                self.assertIn(code, ("assignment_context_unavailable",
                                     "review_context_unavailable"))
        self.assertEqual(self.courses_listing.call_count, 0)

    def test_anonymous_session_is_denied_without_provider_calls(self):
        self.grant()
        self.f["authenticated_google_identity"] = lambda: None
        binding, code = self.f["reverify_classroom_review_access"](self.row)
        self.assertIsNone(binding)
        self.assertEqual(code, "authentication_required")
        self.assertEqual(self.courses_listing.call_count, 0)
        self.assertEqual(self.assignments_listing.call_count, 0)

    def test_revoked_teacher_grant_recording_path_denies_decision(self):
        # A recorded-then-revoked teacher: fresh listing still shows the
        # course, but the session was revoked (logout) -> no grants remain.
        self.grant()
        ta.revoke_teacher_authorization(self.session)
        binding, code = self.f["reverify_classroom_review_access"](self.row)
        self.assertIsNone(binding)
        self.assertIn(code, ("authenticated_teacher_required",
                             "assignment_access_unverified"))

    def test_read_only_display_paths_make_no_classroom_request(self):
        self.grant()
        # Display-time helpers are pure: no listing call ever happens there.
        self.assertEqual(self.f["row_review_scope"](self.row), "classroom")
        self.assertEqual(self.f["row_review_scope"]({"a": 1}), "manual")
        self.assertIsNone(self.f["row_review_scope"](None))
        self.assertFalse(
            self.f["manual_document_provenance_verified"](self.row))
        self.assertEqual(self.courses_listing.call_count, 0)
        self.assertEqual(self.assignments_listing.call_count, 0)

    def test_manual_document_gate_verifies_owned_upload_offline(self):
        row = {"snapshot_context": None}
        self.assertFalse(
            self.f["manual_document_provenance_verified"](row))
        pdf = b"%PDF-1.7 synthetic manual scheme"
        self.f["remember_manual_upload"](pdf)
        self.session["last_manual_context"] = hashlib.sha256(pdf).hexdigest()
        self.assertTrue(
            self.f["manual_document_provenance_verified"](row))
        # A row pinned to a different (foreign) document digest is denied.
        other = {"snapshot_context": hashlib.sha256(b"other").hexdigest()}
        self.assertFalse(
            self.f["manual_document_provenance_verified"](other))
        # Anonymous sessions never verify anything.
        self.f["authenticated_google_identity"] = lambda: None
        self.assertFalse(
            self.f["manual_document_provenance_verified"](row))
        self.assertEqual(self.courses_listing.call_count, 0)
        self.assertEqual(self.assignments_listing.call_count, 0)


if __name__ == "__main__":
    unittest.main()
