"""C6-B4 tests: end-to-end teacher review journey, offline and synthetic.

Closes the one coverage gap left by C6-B2/C6-B3: no test drove the WHOLE
teacher journey as a single sequential flow with the durable store on, and no
test proved that a Streamlit rerun preserves the entire chain (decision
records + overrides + reconciliation + sign-off) rather than one piece of it.

Every provider boundary is synthetic (faked token/listing functions, injected
session state, a local temporary SQLite file). The shared ``requests`` fake
raises on any unexpected HTTP call, so a green run proves no network happened.
No real student data, no credentials, no live provider, no grade ever sent.

Covered:
  - the full journey: login -> mark -> question review (approve/correct/flag)
    -> total reconciliation -> durable audit -> sign-off, as ONE flow,
  - rerun idempotency across the whole chain: a second run changes nothing,
  - account switch / logout mid-journey fails closed and cannot inherit the
    prior owner's rows, overrides or approvals,
  - foreign-owner and stale-context rows are refused at the review boundary,
  - duplicate question ids stay refused for the whole journey,
  - injected audit failure aborts the journey with no partial state,
  - the approval/publication boundary is the only sign-off gate; nothing in
    the review path publishes, and the durable stream never records approval.
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

import question_review as qr
import teacher_authorization as ta
import test_c6b2_security as c6b2
import test_c6b3_m2_app_integration as m2
from support import benchmark, load_app, model_question

SUBJECT = m2.SUBJECT
OWNER = m2.OWNER
PDF = m2.PDF
STORE_ENV = m2.STORE_ENV
ACK_ENV = m2.ACK_ENV


class JourneyTestCase(m2.AppPersistenceTestCase):
    """Reuses the M2 harness; adds journey-scale rows and totals checks."""

    def totals_of(self, row):
        earned, available, percentage = self.f["calculate_totals"](
            row["result"])
        return earned, available, percentage

    def journey_row(self, numbers=("1", "2", "3"), **kwargs):
        row = self.classroom_row(tuple(numbers), **kwargs)
        earned, available, percentage = self.totals_of(row)
        row["totals"] = {"earned": earned, "available": available,
                         "percentage": percentage,
                         "provisional_grade":
                             self.f["percentage_band_grade"](percentage)}
        return row


class FullJourneyTests(JourneyTestCase):
    """The whole teacher journey in order, with the durable store on."""

    def test_full_teacher_journey_runs_in_order_and_persists(self):
        self.enable_store()
        self.enable_classroom()
        row = self.journey_row()
        key = row["review_key"]
        self.install(row)

        # 1. Review: approve one question, correct another, flag a third.
        self.assertEqual(self.decision(key, "1", action="approve")[0], "ok")
        self.assertEqual(
            self.decision(key, "2", action="correct",
                          reason="scheme B1 missed a mark", mark=2)[0], "ok")
        self.assertEqual(
            self.decision(key, "3", action="flag",
                          reason="handwriting needs a human")[0], "ok")

        # 2. Reconciliation: the override is in session state and the totals
        #    recompute from the single override system, AI originals intact.
        self.assertEqual(self.session["teacher_overrides"][key]["2"], 2)
        self.assertTrue(self.f["apply_teacher_overrides"](key, row["result"]))
        self.assertEqual(
            [q["awarded_marks"] for q in row["result"]["questions"]], [1, 2, 1])
        # The pristine AI output is preserved untouched by the override path.
        self.assertEqual(
            [q["awarded_marks"] for q in row["ai_result"]["questions"]],
            [1, 1, 1])
        details = self.f["classroom_grade_details"](row)
        self.assertTrue(details["has_overrides"])
        self.assertEqual(details["score"], "4/6")

        # 3. Durable audit: three ordered events, one per decision, and every
        #    session decision record carries its durable reference.
        store, events = self.stream(row)
        self.assertEqual([event["sequence"] for event in events], [1, 2, 3])
        self.assertEqual([event["review_status"] for event in events],
                         ["approved", "corrected", "flagged"])
        self.assertEqual([event["new_score"] for event in events],
                         ["", "2", ""])
        for number in ("1", "2", "3"):
            record = self.stored(f"{key}::q{number}")
            reference = record.get("audit_reference")
            self.assertIsNotNone(reference, number)
            self.assertEqual(reference["result_id"],
                             self.f["durable_result_identity"](row))

        # 4. Sign-off: the correction invalidated any prior approval; the
        #    teacher re-approves through the separate, explicit gate only.
        self.session["classroom_grade_approvals"] = {key: True}
        self.session[f"classroom_approval_checkbox_{key}"] = True
        self.assertEqual(
            self.decision(key, "2", action="correct",
                          reason="re-confirm the same correction", mark=2)[0],
            "ok")
        self.assertNotIn(key, self.session.get("classroom_grade_approvals", {}))
        self.session[f"classroom_approval_checkbox_{key}"] = True
        self.f["save_classroom_approval"](key)
        self.assertTrue(self.session["classroom_grade_approvals"][key])
        self.assertTrue(self.f["classroom_grade_details"](row)["approved"])

        # 5. The stream holds review decisions ONLY: no approval or publish
        #    record is ever written to the durable store.
        _, events = self.stream(row)
        self.assertEqual({event["event_type"] for event in events},
                         {"question_review_decision"})

    def test_whole_chain_survives_a_rerun_without_changes(self):
        self.enable_store()
        self.enable_classroom()
        row = self.journey_row()
        key = row["review_key"]
        self.install(row)
        self.assertEqual(
            self.decision(key, "2", action="correct",
                          reason="scheme B1 missed a mark", mark=2)[0], "ok")
        self.session[f"classroom_approval_checkbox_{key}"] = True
        self.f["save_classroom_approval"](key)
        before_records = copy.deepcopy(
            self.session["question_review_decisions"])
        before_events = self.stream(row)[1]

        # A rerun re-renders the same installed row: nothing is re-applied,
        # duplicated or lost anywhere in the chain.
        self.install(row)
        after_events = self.stream(row)[1]
        self.assertEqual(before_events, after_events)
        self.assertEqual(self.session["question_review_decisions"],
                         before_records)
        self.assertEqual(self.session["teacher_overrides"][key]["2"], 2)
        self.assertTrue(self.session["classroom_grade_approvals"][key])
        self.assertTrue(self.f["classroom_grade_details"](row)["approved"])
        # Re-issuing the identical decision is idempotent in the stream too.
        self.assertEqual(
            self.decision(key, "2", action="correct",
                          reason="scheme B1 missed a mark", mark=2)[0], "ok")
        self.assertEqual(self.stream(row)[1], after_events)


class JourneyLifecycleTests(JourneyTestCase):
    """Account switch, logout, foreign owners, stale context, duplicates."""

    def test_account_switch_cannot_inherit_the_journey(self):
        self.enable_store()
        self.enable_classroom()
        row = self.journey_row()
        key = row["review_key"]
        self.install(row)
        self.assertEqual(
            self.decision(key, "2", action="correct",
                          reason="first teacher", mark=2)[0], "ok")
        # The next authenticated subject is a different teacher.
        self.f["authenticated_google_identity"] = lambda: "teacher-b-subject"
        self.f["st"] = SimpleNamespace(
            session_state=self.session,
            user=SimpleNamespace(sub="teacher-b-subject"))
        # The prior owner's row is no longer owned: the review fails closed
        # with no new record, no override change and no store event.
        before = self.stream(row)[1]
        self.assertEqual(self.decision(key, "2", action="correct",
                                       reason="second teacher",
                                       mark=3)[0],
                         "review_context_unavailable")
        self.assertEqual(self.session["teacher_overrides"][key]["2"], 2)
        self.assertEqual(self.stream(row)[1], before)

    def test_logout_then_anonymous_use_is_refused(self):
        self.enable_store()
        self.enable_classroom()
        row = self.journey_row()
        key = row["review_key"]
        self.install(row)
        self.assertEqual(self.decision(key, "1", action="approve")[0], "ok")
        before = self.stream(row)[1]
        # Logout: no authenticated identity, so every decision is refused.
        self.f["authenticated_google_identity"] = lambda: None
        self.f["st"] = SimpleNamespace(
            session_state=self.session,
            user=SimpleNamespace(is_logged_in=False, sub=None))
        self.assertEqual(self.decision(key, "1", action="approve")[0],
                         "authentication_required")
        self.assertEqual(self.stream(row)[1], before)

    def test_foreign_owner_row_is_never_reviewable(self):
        self.enable_classroom()
        row = self.journey_row(review_key="classroom_row-9")
        row["report_owner"] = "google:someone-else"
        self.install(row)
        self.assertEqual(
            self.decision(row["review_key"], "1", action="approve")[0],
            "review_context_unavailable")
        self.assertNotIn("question_review_decisions", self.session)
        self.assertNotIn("teacher_overrides", self.session)

    def test_duplicate_ids_stay_refused_across_the_journey(self):
        self.enable_classroom()
        row = self.journey_row()
        row["result"]["questions"][1]["question_number"] = "1"  # collide
        row["ai_result"] = copy.deepcopy(row["result"])
        self.install(row)
        key = row["review_key"]
        self.assertEqual(self.decision(key, "1", action="approve")[0],
                         "review_question_ambiguous")
        self.assertEqual(self.decision(key, "1", action="correct",
                                       reason="try anyway", mark=2)[0],
                         "review_question_ambiguous")
        self.assertNotIn(key, self.session.get("teacher_overrides", {}))
        self.assertNotIn(f"{key}::q1", self.session.get(
            "question_review_decisions", {}))


class JourneyFailureTests(JourneyTestCase):
    """Injected audit failure and store refusal mid-journey."""

    def test_audit_failure_aborts_the_journey_without_partial_state(self):
        self.enable_store()
        self.enable_classroom()
        row = self.journey_row()
        key = row["review_key"]
        self.install(row)
        self.assertEqual(
            self.decision(key, "2", action="correct",
                          reason="before the fault", mark=2)[0], "ok")
        store = self.open_store()
        store.set_fault_hook(m2.exploding("after_event_insert"))
        self.assertEqual(
            self.decision(key, "1", action="correct",
                          reason="must not survive", mark=2)[0],
            "audit_persist_failed")
        store.set_fault_hook(None)
        # The failed correction left nothing behind anywhere in the chain.
        self.assertNotIn("1", self.session["teacher_overrides"][key])
        self.assertIsNone(self.stored(f"{key}::q1"))
        # The earlier, successful correction is untouched.
        self.assertEqual(self.session["teacher_overrides"][key]["2"], 2)
        _, events = self.stream(row)
        self.assertEqual([event["sequence"] for event in events], [1])
        self.assertEqual(events[0]["question_number"], "2")

    def test_store_requested_but_unacknowledged_refuses_the_journey(self):
        self.enable_store(acknowledge=False)
        self.enable_classroom()
        row = self.journey_row()
        key = row["review_key"]
        self.install(row)
        self.assertEqual(self.decision(key, "1", action="approve")[0],
                         "audit_store_unavailable")
        self.assertNotIn(f"{key}::q1", self.session.get(
            "question_review_decisions", {}))
        self.assertFalse(os.path.exists(self.store_path))


if __name__ == "__main__":
    unittest.main()
