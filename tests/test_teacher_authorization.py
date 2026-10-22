"""C6-B1 adversarial tests for trusted teacher authorization (offline only).

Exercises the REAL adapter (teacher_authorization), the REAL C6-A authority
(scheme_authorization) and the REAL review engine (scheme_review). Only the
provider boundary is synthetic: trusted Classroom listings are plain dicts
exactly as the production helpers return them, the clock is injected, and
session state is a plain per-session mapping (the same isolation Streamlit
guarantees between browser sessions). No live Gemini/Classroom/Drive call, no
network, no secret and no real identity is used anywhere.

Covers the C6-B1 review findings too:
- F1  Unicode identifier spoofing (zero-width, bidi, private-use, ...).
- F2  Specific approval-refusal codes survive the broad except handlers.
- F3  Stale contexts/reviews cannot permanently exhaust the registries.
"""
import ast
import copy
import hashlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scheme_review as sr
import teacher_authorization as ta
from scheme_authorization import (
    ApprovalAuthority, AuthorizationError, ServerIdentity, identifier,
    validate_context,
)

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
            "updateTime": "2026-01-02T00:00:00Z",
            "dueDate": {"year": 2026, "month": 1, "day": 10},
            "materials": [{"driveFile": {"id": "scheme-1"}}]}
    item.update(overrides)
    return item


ASSIGNMENTS = [assignment("work-1"), assignment("work-2")]
ASSIGNMENT_KEY = "course-1/work-1"
PDF_V1 = b"%PDF-1.7 synthetic teacher upload v1"
PDF_V2 = b"%PDF-1.7 synthetic teacher upload v2-EDITED"


class FakeClock:
    def __init__(self, now=1_700_000_000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


def signed_in(subject="teacher-a", clock=None):
    """One browser session's server-side state for one authenticated subject."""
    state = {}
    session = ta.current_teacher_session(state, subject, clock=clock or FakeClock())
    assert session is not None
    return state, session


def grant_all(state, subject="teacher-a", assignments=None):
    ta.remember_classroom_grants(
        state, subject, COURSES, "course-1",
        ASSIGNMENTS if assignments is None else assignments)


def manual_review(assignment_id=None):
    return sr.build_review(
        {"questions": [{"question_number": "1", "max_marks": 1,
                        "criteria": [{"criterion_id": "b", "code": "B1", "marks": 1,
                                      "source_page": 1, "source_evidence": "B1 correct"}]}]},
        assignment_id=assignment_id,
        evidence_document={"page_count": 1, "pages": [
            {"page": 1, "text": "Question 1 B1 correct", "status": "extracted"}]},
        question_mapping={"matches": [{"question_id": "1", "certainty": "explicit",
                                       "pages": [1], "references": []}],
                          "unresolved": []})


class SessionLifecycleTests(unittest.TestCase):
    """Adversarial 1-3, 13-18, 23: session/nonce lifecycle and fail-closed states."""

    def test_anonymous_session_is_rejected_and_retains_nothing(self):
        for subject in (None, "", "   ", 42, {"sub": "x"}, ["teacher-a"], True):
            state = {"teacher_authorization_state": "leftover"}
            with self.subTest(subject=repr(subject)):
                self.assertIsNone(ta.current_teacher_session(state, subject))
                self.assertNotIn(ta.AUTH_STATE_KEY, state)

    def test_missing_identity_cannot_record_any_grant(self):
        state = {}
        for recorder in (
                lambda: ta.record_course_grant(state, None, COURSES, "course-1"),
                lambda: ta.record_assignment_grant(state, "", COURSES, "course-1",
                                                   ASSIGNMENTS, "work-1"),
                lambda: ta.record_manual_pdf_grant(state, None, b"%PDF"),
        ):
            with self.subTest(recorder=recorder), self.assertRaises(
                    ta.TeacherAuthorizationError):
                recorder()
        self.assertNotIn(ta.AUTH_STATE_KEY, state)

    def test_invalid_identity_is_rejected(self):
        for subject in ("\u200b", " teacher-a", "teacher-a\n", "x" * 300):
            state = {}
            with self.subTest(subject=repr(subject)):
                self.assertIsNone(ta.current_teacher_session(state, subject))

    def test_logout_revokes_authority_and_every_capability(self):
        state, _ = signed_in()
        grant_all(state)
        context = state[ta.AUTH_STATE_KEY]["authority"].authorize(ASSIGNMENT_KEY)
        record = sr.bind_scheme_review(manual_review(assignment_id=ASSIGNMENT_KEY),
                                       context, review_id="r1",
                                       document_digest="a" * 64)
        ta.revoke_teacher_authorization(state)
        self.assertNotIn(ta.AUTH_STATE_KEY, state)
        with self.assertRaises(AuthorizationError):
            validate_context(context)
        with self.assertRaises(sr.SchemeReviewApprovalError):
            sr.approve_scheme_review(record, context, record["scheme_revision"])

    def test_account_switch_revokes_previous_authorization(self):
        state, _ = signed_in("teacher-a")
        grant_all(state)
        old = state[ta.AUTH_STATE_KEY]
        old_context = old["authority"].authorize(ASSIGNMENT_KEY)
        switched = ta.current_teacher_session(state, "teacher-b")
        self.assertTrue(old["authority"]._revoked)
        self.assertNotEqual(switched["nonce"], old["nonce"])
        self.assertEqual(switched["assignment_grants"], {})
        with self.assertRaises(AuthorizationError):
            validate_context(old_context)

    def test_relogin_same_subject_mints_fresh_nonce_and_dead_old_context(self):
        state, first = signed_in("teacher-a")
        grant_all(state)
        old_context = first["authority"].authorize(ASSIGNMENT_KEY)
        ta.revoke_teacher_authorization(state)  # explicit logout
        second = ta.current_teacher_session(state, "teacher-a")
        self.assertNotEqual(second["nonce"], first["nonce"])
        self.assertIsNot(second["authority"], first["authority"])
        with self.assertRaises(AuthorizationError):
            validate_context(old_context)
        # The old grants died with the logout; fresh login starts empty.
        with self.assertRaises(AuthorizationError):
            second["authority"].authorize(ASSIGNMENT_KEY)

    def test_reruns_are_idempotent_and_never_burn_capacity(self):
        state, first = signed_in()
        grant_all(state)
        authority = first["authority"]
        context = authority.authorize(ASSIGNMENT_KEY)
        for _ in range(150):
            again = ta.current_teacher_session(state, "teacher-a")
            self.assertIs(again, first)
            self.assertIs(again["authority"].authorize(ASSIGNMENT_KEY), context)
        self.assertEqual(len(authority._contexts), 1)

    def test_malformed_state_is_replaced_and_in_place_tampering_dies(self):
        # In-place tampering of a reachable state dict must kill the old
        # authority: the stored authority object is revoked during recovery.
        for name, tamper in (
                ("nonce_mismatch", lambda s: s.update(nonce="tampered")),
                ("authority_nonce_mismatch",
                 lambda s: setattr(s["authority"], "_c6b1_session_nonce", "x")),
                ("grants_not_dicts", lambda s: s.update(assignment_grants="junk")),
                ("created_at_nan", lambda s: s.update(created_at=float("nan"))),
        ):
            state, session = signed_in()
            grant_all(state)
            old_context = session["authority"].authorize(ASSIGNMENT_KEY)
            tamper(state[ta.AUTH_STATE_KEY])
            with self.subTest(case=name):
                fresh = ta.current_teacher_session(state, "teacher-a")
                self.assertNotEqual(fresh["nonce"], session["nonce"])
                self.assertEqual(fresh["assignment_grants"], {})
                self.assertTrue(session["authority"]._revoked)
                with self.assertRaises(AuthorizationError):
                    validate_context(old_context)
        # A replaced (non-dict) entry is unreadable: the state is reset to a
        # fresh, empty authority even though the unreachable old object
        # cannot be revoked through the mapping.
        for malformed in ("garbage", ["teacher-a", "nonce"], 42, None):
            state = {}
            session = ta.current_teacher_session(state, "teacher-a")
            state[ta.AUTH_STATE_KEY] = malformed
            with self.subTest(malformed=repr(malformed)):
                fresh = ta.current_teacher_session(state, "teacher-a")
                self.assertNotEqual(fresh["nonce"], session["nonce"])
                self.assertEqual(fresh["assignment_grants"], {})

    def test_expired_session_fails_authorization_and_replaces_state(self):
        clock = FakeClock()
        state, session = signed_in(clock=clock)
        grant_all(state)
        context = session["authority"].authorize(ASSIGNMENT_KEY)
        clock.advance(ta.SESSION_MAX_AGE_SECONDS + 1)
        label, code = sr.approval_state(manual_review(ASSIGNMENT_KEY), context,
                                        "irrelevant-revision")
        self.assertEqual(label, "invalid")
        self.assertIn(code, ("authorization_unavailable", "session_expired"))
        fresh = ta.current_teacher_session(state, "teacher-a")
        self.assertNotEqual(fresh["nonce"], session["nonce"])
        self.assertEqual(fresh["assignment_grants"], {})

    def test_stale_context_can_never_regain_authorization(self):
        state, session = signed_in()
        grant_all(state)
        old_context = session["authority"].authorize(ASSIGNMENT_KEY)
        # The assignment is edited (updateTime moves) and re-verified.
        changed = [dict(ASSIGNMENTS[0], updateTime="2026-02-02T00:00:00Z"),
                   ASSIGNMENTS[1]]
        grant_all(state, assignments=changed)
        new_context = session["authority"].authorize(ASSIGNMENT_KEY)
        self.assertIsNot(new_context, old_context)
        with self.assertRaises(AuthorizationError):
            validate_context(old_context)
        # Metadata reverts to the original revision: a NEW context is issued,
        # but the pruned one stays permanently dead.
        grant_all(state)
        reverted = session["authority"].authorize(ASSIGNMENT_KEY)
        self.assertIsNot(reverted, old_context)
        self.assertEqual(validate_context(reverted), "google:teacher-a")
        with self.assertRaises(AuthorizationError):
            validate_context(old_context)


class ClassroomGrantTests(unittest.TestCase):
    """Adversarial 4-10: course/assignment permission verification."""

    def test_missing_teacher_permission_fails_closed(self):
        state, session = signed_in()
        grant_all(state)
        authority = session["authority"]
        for unknown in ("course-1/work-unknown", "course-9/work-1", "",
                        "work-1"):
            with self.subTest(assignment=unknown), self.assertRaises(
                    AuthorizationError):
                authority.authorize(unknown)

    def test_course_not_in_trusted_listing_is_refused(self):
        state, _ = signed_in()
        with self.assertRaises(ta.TeacherAuthorizationError):
            ta.record_course_grant(state, "teacher-a", COURSES, "course-9")
        with self.assertRaises(ta.TeacherAuthorizationError):
            ta.record_assignment_grant(state, "teacher-a", COURSES, "course-9",
                                       ASSIGNMENTS, "work-1")
        self.assertEqual(state[ta.AUTH_STATE_KEY]["course_grants"], {})

    def test_course_grant_alone_never_authorizes_an_assignment(self):
        state, session = signed_in()
        ta.record_course_grant(state, "teacher-a", COURSES, "course-1")
        with self.assertRaises(AuthorizationError):
            session["authority"].authorize(ASSIGNMENT_KEY)

    def test_assignment_must_appear_in_the_course_listing(self):
        state, session = signed_in()
        ta.record_course_grant(state, "teacher-a", COURSES, "course-1")
        # work-2 exists elsewhere, but never in course-1's trusted listing.
        with self.assertRaises(ta.TeacherAuthorizationError):
            ta.record_assignment_grant(state, "teacher-a", COURSES, "course-1",
                                       [ASSIGNMENTS[0]], "work-2")
        with self.assertRaises(AuthorizationError):
            session["authority"].authorize("course-1/work-2")

    def test_invalid_or_malformed_listings_grant_nothing(self):
        state, session = signed_in()
        ta.remember_classroom_grants(state, "teacher-a", None, "course-1", None)
        ta.remember_classroom_grants(state, "teacher-a", "not-a-list",
                                     "course-1", {"id": "work-1"})
        ta.remember_classroom_grants(
            state, "teacher-a",
            [None, 42, {}, {"id": None}, {"id": "   "}], "course-1",
            [None, "work", {}, {"id": None, "workType": "ASSIGNMENT"}])
        self.assertEqual(state[ta.AUTH_STATE_KEY]["course_grants"], {})
        self.assertEqual(state[ta.AUTH_STATE_KEY]["assignment_grants"], {})
        with self.assertRaises(AuthorizationError):
            session["authority"].authorize(ASSIGNMENT_KEY)

    def test_missing_provider_metadata_fails_closed_or_is_documented(self):
        state, session = signed_in()
        # No id in the coursework: refused.
        with self.assertRaises(ta.TeacherAuthorizationError):
            ta.record_assignment_grant(state, "teacher-a", COURSES, "course-1",
                                       [{"title": "no id"}], "work-1")
        # Wrong work type: refused.
        with self.assertRaises(ta.TeacherAuthorizationError):
            ta.record_assignment_grant(
                state, "teacher-a", COURSES, "course-1",
                [assignment("work-3", workType="SHORT_ANSWER_QUESTION")],
                "work-3")
        # Missing optional metadata: a revision is still computed from what
        # the provider actually returned, never fabricated.
        sparse = [{"id": "work-4", "courseId": "course-1",
                   "workType": "ASSIGNMENT"}]
        key = ta.record_assignment_grant(state, "teacher-a", COURSES,
                                         "course-1", sparse, "work-4")
        grant = state[ta.AUTH_STATE_KEY]["assignment_grants"][key]
        self.assertEqual(len(grant["revision"]), 64)
        context = session["authority"].authorize(key)
        self.assertEqual(validate_context(context), "google:teacher-a")

    def test_multiple_distinct_assignments_coexist(self):
        state, session = signed_in()
        grant_all(state)
        first = session["authority"].authorize("course-1/work-1")
        second = session["authority"].authorize("course-1/work-2")
        self.assertIsNot(first, second)
        self.assertEqual(validate_context(first), "google:teacher-a")
        self.assertEqual(validate_context(second), "google:teacher-a")

    def test_assignment_revision_change_invalidates_stale_authorization(self):
        state, session = signed_in()
        grant_all(state)
        context = session["authority"].authorize(ASSIGNMENT_KEY)
        record = sr.bind_scheme_review(manual_review(assignment_id=ASSIGNMENT_KEY),
                                       context, review_id="r1",
                                       document_digest="a" * 64)
        sr.approve_scheme_review(record, context, record["scheme_revision"])
        self.assertEqual(sr.approval_state(record, context,
                                           record["scheme_revision"])[0],
                         "approved")
        # The assignment is edited on the provider side and re-verified.
        grant_all(state, assignments=[dict(ASSIGNMENTS[0], maxPoints=25),
                                      ASSIGNMENTS[1]])
        label, code = sr.approval_state(record, context,
                                        record["scheme_revision"])
        self.assertEqual(label, "invalid")
        self.assertIn(code, ("session_or_owner_changed",
                             "issued_authorization_required"))
        with self.assertRaises(sr.SchemeReviewApprovalError):
            sr.approve_scheme_review(record, context, record["scheme_revision"])

    def test_grant_expiry_fails_closed_until_reverified(self):
        clock = FakeClock()
        state, session = signed_in(clock=clock)
        grant_all(state)
        context = session["authority"].authorize(ASSIGNMENT_KEY)
        clock.advance(ta.ASSIGNMENT_GRANT_MAX_AGE_SECONDS + 1)
        with self.assertRaises(AuthorizationError):
            validate_context(context)
        # A fresh trusted listing re-establishes access with a new context.
        grant_all(state)
        fresh = session["authority"].authorize(ASSIGNMENT_KEY)
        self.assertEqual(validate_context(fresh), "google:teacher-a")

    def test_browser_supplied_identity_gains_nothing(self):
        # A stolen browser claim of "teacher-b" against teacher-a's server
        # session is an account switch: A dies, B starts empty.
        state, session_a = signed_in("teacher-a")
        grant_all(state)
        context_a = session_a["authority"].authorize(ASSIGNMENT_KEY)
        session_b = ta.current_teacher_session(state, "teacher-b")
        self.assertTrue(session_a["authority"]._revoked)
        self.assertEqual(session_b["assignment_grants"], {})
        with self.assertRaises(AuthorizationError):
            validate_context(context_a)
        with self.assertRaises(AuthorizationError):
            session_b["authority"].authorize(ASSIGNMENT_KEY)


class ManualPdfProvenanceTests(unittest.TestCase):
    """Adversarial 11-12: manual document ownership and digest provenance."""

    def test_manual_scope_requires_a_registered_document(self):
        state, session = signed_in()
        with self.assertRaises(AuthorizationError):
            session["authority"].authorize(None)

    def test_digest_is_computed_from_actual_bytes_and_bound_to_owner(self):
        state, session = signed_in()
        digest = ta.record_manual_pdf_grant(state, "teacher-a", PDF_V1)
        self.assertEqual(digest, hashlib.sha256(PDF_V1).hexdigest())
        context = session["authority"].authorize(None)
        self.assertEqual(validate_context(context), "google:teacher-a")
        # A browser-supplied digest string is never accepted as evidence.
        with self.assertRaises(ta.TeacherAuthorizationError):
            ta.record_manual_pdf_grant(state, "teacher-a", digest)
        with self.assertRaises(ta.TeacherAuthorizationError):
            ta.record_manual_pdf_grant(state, "teacher-a", b"")

    def test_one_teacher_cannot_use_another_teacher_manual_review(self):
        state_a, _ = signed_in("teacher-a")
        record = ta.bind_manual_review(
            state_a, "teacher-a", manual_review(), review_id="r1",
            pdf_bytes=PDF_V1)
        self.assertEqual(record["approval_binding"]["owner"], "google:teacher-a")
        self.assertEqual(record["approval_binding"]["document_digest"],
                         hashlib.sha256(PDF_V1).hexdigest())
        context_a = state_a[ta.AUTH_STATE_KEY]["authority"].authorize(None)
        sr.approve_scheme_review(record, context_a, record["scheme_revision"])
        self.assertEqual(sr.approval_state(record, context_a,
                                           record["scheme_revision"])[0],
                         "approved")
        # Teacher B, in B's own session, cannot approve A's bound review:
        # B's authority has never registered it, and A's manual grant is
        # not visible from B's server session.
        state_b, session_b = signed_in("teacher-b")
        self.assertEqual(state_b[ta.AUTH_STATE_KEY]["manual_grants"], {})
        with self.assertRaises(AuthorizationError):
            session_b["authority"].authorize(None)
        ta.record_manual_pdf_grant(state_b, "teacher-b", PDF_V2)
        context_b = session_b["authority"].authorize(None)
        record_b = copy.deepcopy(record)
        with self.assertRaisesRegex(sr.SchemeReviewApprovalError,
                                    "review_authorization_mismatch"):
            sr.approve_scheme_review(record_b, context_b,
                                     record_b["scheme_revision"])
        # Dropping the binding does not help either: an unbound record can
        # never be approved by anyone.
        unbound = copy.deepcopy(record)
        unbound.pop("approval_binding", None)
        with self.assertRaisesRegex(sr.SchemeReviewApprovalError,
                                    "bound_review_required"):
            sr.approve_scheme_review(unbound, context_b,
                                     unbound["scheme_revision"])

    def test_changed_pdf_bytes_never_ride_on_the_old_binding(self):
        state, session = signed_in()
        record = ta.bind_manual_review(
            state, "teacher-a", manual_review(), review_id="r1",
            pdf_bytes=PDF_V1)
        self.assertEqual(record["approval_binding"]["document_digest"],
                         hashlib.sha256(PDF_V1).hexdigest())
        context = session["authority"].authorize(None)
        sr.approve_scheme_review(record, context, record["scheme_revision"])
        # Rewriting the binding digest by hand (as a changed document would
        # require) no longer matches the registered snapshot.
        forged = copy.deepcopy(record)
        forged["approval_binding"]["document_digest"] = hashlib.sha256(
            PDF_V2).hexdigest()
        self.assertNotEqual(
            sr.approval_state(forged, context, forged["scheme_revision"])[0],
            "approved")
        # Re-binding the changed document goes through provenance again with
        # the digest of the ACTUAL new bytes.
        reissued = ta.bind_manual_review(
            state, "teacher-a", manual_review(), review_id="r2",
            pdf_bytes=PDF_V2)
        self.assertEqual(reissued["approval_binding"]["document_digest"],
                         hashlib.sha256(PDF_V2).hexdigest())

    def test_repeated_manual_uploads_stay_bounded(self):
        clock = FakeClock()
        state, session = signed_in(clock=clock)
        for index in range(ta.MAX_MANUAL_GRANTS + 10):
            clock.advance(1)
            ta.record_manual_pdf_grant(state, "teacher-a",
                                       ("upload-%d" % index).encode())
        grants = state[ta.AUTH_STATE_KEY]["manual_grants"]
        self.assertLessEqual(len(grants), ta.MAX_MANUAL_GRANTS)
        # The newest upload is always the authoritative document.
        newest = hashlib.sha256(
            ("upload-%d" % (ta.MAX_MANUAL_GRANTS + 9)).encode()).hexdigest()
        self.assertIn(newest, grants)

    def test_review_record_change_after_authorization_is_detected(self):
        state, session = signed_in()
        record = ta.bind_manual_review(
            state, "teacher-a", manual_review(), review_id="r1",
            pdf_bytes=PDF_V1)
        context = session["authority"].authorize(None)
        sr.approve_scheme_review(record, context, record["scheme_revision"])
        mutated = copy.deepcopy(record)
        mutated["extracted"]["questions"][0]["max_marks"] = 99
        label, _ = sr.approval_state(mutated, context,
                                     mutated["scheme_revision"])
        self.assertNotEqual(label, "approved")


class CapacityAndPruningTests(unittest.TestCase):
    """Adversarial 22-23 / F3: bounded registries, permanent invalidation."""

    def test_context_registry_stays_bounded_under_revision_churn(self):
        state, session = signed_in()
        grant_all(state)
        authority = session["authority"]
        earliest = []
        for index in range(300):
            varying = [dict(ASSIGNMENTS[0],
                            updateTime="2026-03-%02dT00:00:00Z" % (index % 28 + 1),
                            title="Algebra test %d" % index),
                       ASSIGNMENTS[1]]
            grant_all(state, assignments=varying)
            if index < 3:
                earliest.append(authority.authorize(ASSIGNMENT_KEY))
            else:
                authority.authorize(ASSIGNMENT_KEY)
            self.assertLessEqual(len(authority._contexts), 3)
        # The earliest contexts were superseded and pruned: permanently dead.
        for context in earliest[:2]:
            with self.assertRaises(AuthorizationError):
                validate_context(context)

    def test_hard_capacity_limit_still_fails_closed(self):
        state, session = signed_in()
        many = [assignment("work-%d" % index) for index in range(105)]
        ta.remember_classroom_grants(state, "teacher-a", COURSES, "course-1",
                                     many)
        authority = session["authority"]
        for index in range(100):
            authority.authorize("course-1/work-%d" % index)
        self.assertEqual(len(authority._contexts), 100)
        with self.assertRaises(AuthorizationError):
            authority.authorize("course-1/work-100")

    def test_superseded_reviews_free_registry_capacity(self):
        state, session = signed_in()
        grant_all(state)
        authority = session["authority"]
        for index in range(105):
            varying = [dict(ASSIGNMENTS[0], title="Edited %d" % index,
                            updateTime="2026-04-%02dT00:00:00Z"
                                      % (index % 28 + 1)),
                       ASSIGNMENTS[1]]
            grant_all(state, assignments=varying)
            context = authority.authorize(ASSIGNMENT_KEY)
            record = sr.bind_scheme_review(
                manual_review(assignment_id=ASSIGNMENT_KEY), context,
                review_id="rev-%d" % index, document_digest="b" * 64)
            self.assertEqual(len(record["approval_binding"]["review_id"]),
                             len("rev-%d" % index))
            self.assertLessEqual(len(authority._reviews), 100)
            self.assertLessEqual(len(authority._contexts), 3)


class UnicodeIdentifierTests(unittest.TestCase):
    """Adversarial 19 / F1: identifier spoofing."""

    def test_dangerous_unicode_is_rejected(self):
        dangerous = {
            "zero_width_space": "\u200b",
            "zero_width_joiner": "\u200d",
            "bidi_override": "\u202e",
            "left_to_right_mark": "\u200e",
            "bom": "\ufeff",
            "nbsp": "\u00a0",
            "line_separator": "\u2028",
            "control": "\x00\x1b\x7f",
            "private_use": "\ue000",
            "surrogate": "\ud800",
            "unassigned": "\u0378",
            "embedded_zero_width": "teacher\u200ba",
            "embedded_bidi": "teacher\u202ca",
        }
        for name, value in dangerous.items():
            with self.subTest(case=name):
                self.assertFalse(identifier(value))
                self.assertFalse(identifier("x" + value + "y"))

    def test_legitimate_non_ascii_identities_stay_valid(self):
        for value in ("teacher-a", "José", "田中太郎", "Οδυσσέας",
                      "Ünïcödé-Name_123", "учитель_9"):
            with self.subTest(value=value):
                self.assertTrue(identifier(value))

    def test_spoofed_identity_cannot_reach_an_authority(self):
        for owner in ("te\u200bacher", "teacher\u202e", "a\u0378b"):
            authority = ApprovalAuthority(
                lambda a: ServerIdentity(owner, "nonce-1", "rev-1"))
            with self.subTest(owner=owner), self.assertRaises(
                    AuthorizationError):
                authority.authorize("assignment-a")


class ApprovalRefusalCodeTests(unittest.TestCase):
    """Adversarial 20-21 / F2: specific codes survive the broad handlers."""

    def test_fractional_marks_fail_closed_with_specific_code(self):
        state, _ = signed_in()
        record = manual_review()
        record["approval_inputs"]["interpretation"]["questions"][0][
            "max_marks"] = 1.5
        with self.assertRaisesRegex(sr.SchemeReviewApprovalError,
                                    "exact_mark_allocations_required"):
            ta.bind_manual_review(state, "teacher-a", record, review_id="r1",
                                  pdf_bytes=PDF_V1)

    def test_boolean_string_container_and_null_allocations_fail_closed(self):
        state, _ = signed_in()
        for bad in (True, [1], None, {"marks": 1}, 1.5):
            record = manual_review()
            record["approval_inputs"]["interpretation"]["questions"][0][
                "max_marks"] = bad
            with self.subTest(value=repr(bad)), self.assertRaisesRegex(
                    sr.SchemeReviewApprovalError,
                    "exact_mark_allocations_required"):
                ta.bind_manual_review(state, "teacher-a", record,
                                      review_id="r-%s" % abs(hash(repr(bad))),
                                      pdf_bytes=PDF_V1)

    def test_non_finite_allocations_fail_closed(self):
        state, _ = signed_in()
        for bad in (float("inf"), float("nan")):
            record = manual_review()
            record["approval_inputs"]["interpretation"]["questions"][0][
                "max_marks"] = bad
            with self.subTest(value=repr(bad)), self.assertRaisesRegex(
                    sr.SchemeReviewApprovalError,
                    "invalid_review_record|exact_mark_allocations_required"):
                ta.bind_manual_review(state, "teacher-a", record,
                                      review_id="n-%s" % abs(hash(repr(bad))),
                                      pdf_bytes=PDF_V1)

    def test_non_finite_record_content_fails_closed(self):
        state, session = signed_in()
        record = ta.bind_manual_review(
            state, "teacher-a", manual_review(), review_id="r1",
            pdf_bytes=PDF_V1)
        context = session["authority"].authorize(None)
        record["extracted"]["maxima"] = {"1": float("nan")}
        label, code = sr.approval_state(record, context,
                                        record["scheme_revision"])
        self.assertEqual(label, "invalid")
        self.assertEqual(code, "invalid_review_record")

    def test_snapshot_too_large_keeps_its_specific_code(self):
        state, session = signed_in()
        ta.record_manual_pdf_grant(state, "teacher-a", PDF_V1)
        context = session["authority"].authorize(None)
        record = manual_review()
        record["verified"]["findings"].append(
            {"finding_id": "oversized", "_padding": "x" * (
                sr.MAX_SERIALIZED_CHARS * 3 + 10)})
        label, code = sr.approval_state(record, context,
                                        record["scheme_revision"])
        self.assertEqual(label, "invalid")
        self.assertEqual(code, "review_snapshot_too_large")

    def test_corrupted_records_fail_closed_with_diagnostic_codes(self):
        state, _ = signed_in()
        corruptions = {
            "missing_teacher": ("invalid_review_record",
                                lambda r: r.pop("teacher")),
            "teacher_not_dict": ("invalid_review_record",
                                 lambda r: r.update(teacher="approved")),
            "findings_not_list": ("invalid_review_record",
                                  lambda r: r["verified"].update(findings={})),
            "interpretation_corrupt": ("review_revalidation_failed",
                                       lambda r: r["approval_inputs"].update(
                                           interpretation=["not", "a", "dict"])),
        }
        for name, (code, corrupt) in corruptions.items():
            record = manual_review()
            corrupt(record)
            with self.subTest(case=name), self.assertRaisesRegex(
                    sr.SchemeReviewApprovalError, code):
                ta.bind_manual_review(state, "teacher-a", record,
                                      review_id="c-%s" % name,
                                      pdf_bytes=PDF_V1)


class AcknowledgementSeparationTests(unittest.TestCase):
    """Adversarial 24: acknowledgement is not approval."""

    def test_acknowledgement_never_grants_scheme_approval(self):
        record = manual_review()
        self.assertNotEqual(
            sr.approval_state(record, None, record["scheme_revision"])[0],
            "approved")
        self.assertNotEqual(sr.determine_status(record), sr.STATUS_APPROVED)


class NoUiOrEndpointExposureTests(unittest.TestCase):
    """Adversarial 25: no new approval UI, endpoint or provider call."""

    APP = SOURCE.read_text(encoding="utf-8")
    ADAPTER = (Path(__file__).resolve().parents[1]
               / "teacher_authorization.py").read_text(encoding="utf-8")

    def test_app_never_calls_the_privileged_approval_engine(self):
        tree = ast.parse(self.APP)
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)
                elif isinstance(node.func, ast.Name):
                    called.add(node.func.id)
        for forbidden in ("approve_scheme_review", "reject_scheme_review",
                          "bind_scheme_review", "bind_manual_review",
                          "resolve_findings"):
            self.assertNotIn(forbidden, called)

    def test_adapter_has_no_network_ui_or_persistence_surface(self):
        tree = ast.parse(self.ADAPTER)
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add(node.module)
        self.assertEqual(
            imports,
            {"hashlib", "json", "math", "secrets", "time",
             "document_limits", "scheme_authorization", "scheme_review"})
        self.assertNotIn("open(", self.ADAPTER)


class ApplicationWiringTests(unittest.TestCase):
    """The real app wiring: lifecycle hooks and best-effort grant recording."""

    def setUp(self):
        self.f = load_app()
        self.session = {}
        self.f["st"] = SimpleNamespace(
            session_state=self.session,
            user=SimpleNamespace(sub="teacher-a-subject"),
        )
        self.f["google_logged_in"] = lambda: True
        self.f["authenticated_google_identity"] = lambda: "teacher-a-subject"

    def test_authorization_state_is_owner_bound_sensitive_state(self):
        self.assertIn(ta.AUTH_STATE_KEY, self.f["SENSITIVE_STATE_KEYS"])

    def test_logout_cleanup_revokes_the_authority(self):
        state = ta.current_teacher_session(self.session, "teacher-a-subject")
        grant_all(self.session, subject="teacher-a-subject")
        context = state["authority"].authorize(ASSIGNMENT_KEY)
        self.f["invalidate_sensitive_state"]()
        self.assertNotIn(ta.AUTH_STATE_KEY, self.session)
        with self.assertRaises(AuthorizationError):
            validate_context(context)

    def test_account_switch_reconciliation_purges_authorization(self):
        self.session["sensitive_state_owner"] = "google:teacher-a-subject"
        state = ta.current_teacher_session(self.session, "teacher-a-subject")
        grant_all(self.session, subject="teacher-a-subject")
        context = state["authority"].authorize(ASSIGNMENT_KEY)
        self.f["authenticated_google_identity"] = lambda: "teacher-b-subject"
        self.f["reconcile_sensitive_state"]()
        self.assertNotIn(ta.AUTH_STATE_KEY, self.session)
        with self.assertRaises(AuthorizationError):
            validate_context(context)

    def test_anonymous_reconciliation_purges_authorization(self):
        state = ta.current_teacher_session(self.session, "teacher-a-subject")
        grant_all(self.session, subject="teacher-a-subject")
        context = state["authority"].authorize(ASSIGNMENT_KEY)
        self.f["google_logged_in"] = lambda: False
        self.f["authenticated_google_identity"] = lambda: None
        self.f["reconcile_sensitive_state"]()
        self.assertNotIn(ta.AUTH_STATE_KEY, self.session)
        with self.assertRaises(AuthorizationError):
            validate_context(context)

    def test_classroom_listing_path_records_verified_grants(self):
        self.f["record_classroom_grants"](COURSES, [], None)
        grants = self.session[ta.AUTH_STATE_KEY]["course_grants"]
        self.assertEqual(set(grants), {"course-1", "course-2"})
        self.f["record_classroom_grants"](COURSES, ASSIGNMENTS, "course-1")
        authority = self.session[ta.AUTH_STATE_KEY]["authority"]
        context = authority.authorize(ASSIGNMENT_KEY)
        self.assertEqual(validate_context(context), "google:teacher-a-subject")

    def test_classroom_grant_recording_never_breaks_marking(self):
        # Partial/failed listings are swallowed: bookkeeping only.
        self.f["record_classroom_grants"](None, "unexpected", 42)
        self.f["record_classroom_grants"]([], [], "   ")
        self.assertIn(ta.AUTH_STATE_KEY, self.session)

    def test_grants_and_authority_survive_same_teacher_reruns(self):
        # Recording binds the owner, so reconciliation on the next rerun
        # keeps the SAME teacher's authority and grants alive.
        self.f["record_classroom_grants"](COURSES, ASSIGNMENTS, "course-1")
        session_before = self.session[ta.AUTH_STATE_KEY]
        for _ in range(10):
            self.f["reconcile_sensitive_state"]()
        self.assertIs(self.session[ta.AUTH_STATE_KEY], session_before)
        context = session_before["authority"].authorize(ASSIGNMENT_KEY)
        self.assertEqual(validate_context(context), "google:teacher-a-subject")

    def test_manual_upload_path_registers_server_side_digest(self):
        pdf_bytes = b"%PDF synthetic manual upload"
        self.f["remember_manual_upload"](pdf_bytes)
        grants = self.session[ta.AUTH_STATE_KEY]["manual_grants"]
        digest = hashlib.sha256(pdf_bytes).hexdigest()
        self.assertIn(digest, grants)
        self.assertEqual(grants[digest]["owner"], "google:teacher-a-subject")
        # Anonymous session registers nothing.
        anonymous = {}
        self.f["st"] = SimpleNamespace(
            session_state=anonymous, user=SimpleNamespace())
        self.f["google_logged_in"] = lambda: False
        self.f["authenticated_google_identity"] = lambda: None
        self.f["remember_manual_upload"](pdf_bytes)
        self.assertNotIn(ta.AUTH_STATE_KEY, anonymous)

    def test_existing_acknowledgement_flow_is_still_separate(self):
        recorded = self.f["save_scheme_review_acknowledgement"]("rev-c6b1")
        self.assertTrue(recorded)
        self.assertEqual(
            self.f["scheme_review_acknowledgement_state"]("rev-c6b1"),
            "recorded")
        # Yet no scheme approval exists anywhere for that revision.
        record = manual_review()
        self.assertNotEqual(
            sr.approval_state(record, None, record["scheme_revision"])[0],
            "approved")


if __name__ == "__main__":
    unittest.main()
