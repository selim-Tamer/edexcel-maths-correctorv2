"""C6-B5 UI tests: the review journey and its reruns in real Streamlit.

Closes IR gap #1 (C6-B4 review report section 3/5): the teacher review
chain was approved at FUNCTION level only (C6-B4) — nothing exercised the
actual Streamlit widgets and reruns the way a teacher touches them. These
AppTest runs use the production render functions
(``render_question_review_summary`` / ``render_question_review_block``),
the production decision path with its real button callbacks, synthetic
session data and the opted-in development audit store. They make no OAuth,
Classroom or Gemini requests; the mock-listing variant keeps the headless
loader's fail-closed ``requests`` fake in place, so any unexpected HTTP
call would surface as an app exception.

Covered:
  - an approve decision submitted through the real widgets persists in the
    session chain AND the durable store, and a plain rerun preserves the
    whole chain unchanged (rerun-identity at the UI layer);
  - a corrected mark submitted through the widgets goes through the same
    single override system; the durable stream still holds review
    decisions only — no approval/publication record;
  - the publication/release surface is absent and inert at this layer: no
    publish-like widget is rendered, the Classroom approval state was never
    set, and the grade approval gate stays a separate session-only action;
  - named 401 variant: an EXPIRED credential at the real HTTP boundary
    rejects the submitted decision with the fail-closed notice in the UI,
    after exactly one provider call, and a rerun proves nothing partial
    survived (no decision record, no override, no audit event, no store).
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(TESTS.parent))

import audit_store
from support import load_app

SUBJECT = "teacher-a-subject"
OWNER = "google:" + SUBJECT
STORE_ENV = "AI_MARKER_AUDIT_DEV_STORE"
ACK_ENV = "AI_MARKER_AUDIT_DEV_ONLY"
EXPIRED_UI_TOKEN = "synthetic-expired-ui-token-4f01"

# The review page: production render functions over one synthetic
# Classroom-bound marked row, with the F-1 grant recorded. LISTING_SNIPPET
# selects the boundary: synthetic mock listings (green path) or the REAL
# classroom_request boundary behind a routed 401 fake (fail-closed path).
BOOTSTRAP = """
import sys
sys.path.insert(0, %(tests)r)
import pandas as pd
import streamlit as st
from support import SOURCE, load_app, benchmark, model_question
import test_c6b2_security as c6b2

f = load_app()
f.update(st=st, pd=pd)
f["google_logged_in"] = lambda: True
f["authenticated_google_identity"] = lambda: %(subject)r
f["get_google_access_token"] = lambda: st.session_state["access_token"]
%(listing)s

if "c6b5_seeded" not in st.session_state:
    st.session_state["access_token"] = %(token)r
    questions = []
    for number in ("1", "2"):
        q = model_question(benchmark()["questions"][int(number) - 1], "partial")
        q["question_number"] = number
        questions.append(q)
    result = f["normalise_result"]({"questions": questions})
    marked = {"status": "Marked", "student": "Synthetic", "result": result,
              "marking_model": "fake", "audit_model": "Not run"}
    row = f["new_review_row"](marked, "c6b5-apptest")
    row.update(classroom_course_id="course-1",
               classroom_assignment_id="work-1",
               classroom_context="c" * 64, revision="rev-1",
               submission_id="submission-1",
               selected_questions=["1", "2"], classroom_max_points=100,
               homework_question_count=2)
    row = f["prepare_review_row"](row)
    st.session_state["last_batch_results"] = [row]
    st.session_state["c6b5_seeded"] = True

f["record_classroom_grants"](list(c6b2.COURSES), list(c6b2.ASSIGNMENTS),
                             "course-1")
row = st.session_state["last_batch_results"][0]
# Every rerun re-prepares the row exactly like the production results path:
# session overrides fold into a fresh display copy of the pristine AI output.
row = f["prepare_review_row"](row)
st.session_state["last_batch_results"] = [row]
key = row["review_key"]
f["render_question_review_summary"](row, key, row["result"])
f["render_question_review_block"](row, key, row["result"]["questions"][0])
f["render_question_review_block"](row, key, row["result"]["questions"][1])
"""

MOCK_LISTINGS = """
f["list_teacher_courses"] = lambda token: [dict(c) for c in c6b2.COURSES]
f["list_assignments"] = lambda token, course_id: [
    dict(a) for a in c6b2.ASSIGNMENTS]
"""

REAL_HTTP_401 = """
from types import SimpleNamespace
from support import response
st.session_state.setdefault("http_status", 401)
st.session_state.setdefault("http_calls", [])


def _fake_request(*args, method=None, url=None, headers=None, timeout=None,
                  params=None, **kwargs):
    st.session_state["http_calls"].append({"url": url,
                                           "headers": dict(headers or {})})
    if st.session_state["http_status"] == 200:
        if url.endswith("/courses"):
            return response(data={"courses": [dict(c) for c in c6b2.COURSES]})
        return response(data={"courseWork":
                              [dict(a) for a in c6b2.ASSIGNMENTS]})
    return response(status=st.session_state["http_status"],
                    content=b'{"error": "Expired credential canary c6b5"}')


f["requests"] = SimpleNamespace(
    request=_fake_request,
    get=lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("Unexpected HTTP download")))
"""


class ReviewJourneyAppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store_path = os.path.join(self.tmp, "c6b5_ui_audit.db")
        os.environ[STORE_ENV] = self.store_path
        os.environ[ACK_ENV] = "1"
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.environ.pop, STORE_ENV, None)
        self.addCleanup(os.environ.pop, ACK_ENV, None)

    def run_journey_app(self, listing=MOCK_LISTINGS, token="synthetic-ui-token"):
        script = BOOTSTRAP % {"tests": str(TESTS), "subject": SUBJECT,
                              "listing": listing, "token": token}
        app = AppTest.from_string(script, default_timeout=30).run()
        self.assertEqual([str(e.value) for e in app.exception], [])
        return app

    def review_key(self, app):
        return app.session_state["last_batch_results"][0]["review_key"]

    def durable_events(self, app):
        row = app.session_state["last_batch_results"][0]
        if not os.path.exists(self.store_path):
            return []
        scope = load_app()
        result_id = scope["durable_result_identity"](row)
        store = audit_store.open_store(self.store_path, dev_only_ack=True)
        try:
            return list(store.events(owner=OWNER, result_id=result_id))
        finally:
            store.close()

    def assert_publication_surface_absent(self, app):
        """Nothing at this layer publishes, releases or posts grades."""
        labels = []
        for group in (app.button, app.checkbox, app.radio, app.text_input):
            for widget in group:
                labels.append(str(getattr(widget, "label", "") or ""))
                labels.append(str(getattr(widget, "key", "") or ""))
        blob = " ".join(labels).lower()
        for word in ("publish", "release", "return work", "post grade",
                     "send grade", "overwrite classroom"):
            self.assertNotIn(word, blob)
        # The Classroom approval is a separate gate: never set here, and its
        # checkbox lives only in the results block, not the review journey.
        self.assertNotIn("classroom_grade_approvals", app.session_state)
        for checkbox in app.checkbox:
            self.assertFalse(str(checkbox.key).startswith(
                "classroom_approval_checkbox_"))

    def test_approve_via_widgets_survives_rerun_with_durable_chain(self):
        app = self.run_journey_app()
        key = self.review_key(app)
        app.button(key="qreview_submit_%s_1" % key).click().run()
        self.assertEqual([str(e.value) for e in app.exception], [])
        self.assertEqual(app.session_state["question_review_notice"][0], "ok")
        record = app.session_state["question_review_decisions"][
            "%s::q1" % key]
        self.assertEqual(record["status"], "approved")
        self.assertIn("Review decision recorded",
                      " ".join(str(item.value) for item in app.success))
        events = self.durable_events(app)
        self.assertEqual([e["sequence"] for e in events], [1])
        self.assertEqual({e["event_type"] for e in events},
                         {"question_review_decision"})

        # A plain rerun (fresh script run, same session) re-verifies its
        # gates and preserves the ENTIRE chain: decision record, durable
        # event, no duplicates, publication surface still absent.
        before = dict(app.session_state["question_review_decisions"])
        app.run()
        self.assertEqual([str(e.value) for e in app.exception], [])
        self.assertEqual(
            app.session_state["question_review_decisions"], before)
        self.assertEqual([e["sequence"] for e in self.durable_events(app)],
                         [1])
        self.assertIn("Approved",
                      " ".join(str(item.value) for item in app.markdown)
                      + str(app.dataframe[0].value.to_dict()))
        self.assert_publication_surface_absent(app)

    def test_correction_via_widgets_uses_single_override_system(self):
        app = self.run_journey_app()
        key = self.review_key(app)
        # Approve q1, then correct q2 through the widgets: radio -> rerender
        # exposes the marks input -> edit mark + reason -> submit in ONE
        # event, exactly like a teacher click.
        app.button(key="qreview_submit_%s_1" % key).click().run()
        app.radio[1].set_value("correct").run()
        app.number_input[0].set_value(2)
        app.text_input[1].set_value("scheme A1 earned, missed by the AI")
        app.button(key="qreview_submit_%s_2" % key).click().run()
        self.assertEqual([str(e.value) for e in app.exception], [])
        decisions = app.session_state["question_review_decisions"]
        self.assertEqual(decisions["%s::q2" % key]["status"], "corrected")
        self.assertEqual(
            app.session_state["teacher_overrides"][key], {"2": 2})
        row = app.session_state["last_batch_results"][0]
        self.assertEqual([q["awarded_marks"] for q in row["result"]["questions"]],
                         [1, 2])
        self.assertEqual(
            [q["awarded_marks"] for q in row["ai_result"]["questions"]], [1, 1])

        events = self.durable_events(app)
        self.assertEqual([e["sequence"] for e in events], [1, 2])
        self.assertEqual([e["review_status"] for e in events],
                         ["approved", "corrected"])
        # Durable stream: decisions only, never an approval/publication
        # record; the release surface stays absent and inert.
        self.assertEqual({e["event_type"] for e in events},
                         {"question_review_decision"})
        self.assert_publication_surface_absent(app)

        # Rerun keeps the corrected chain exactly.
        app.run()
        self.assertEqual([str(e.value) for e in app.exception], [])
        self.assertEqual(app.session_state["teacher_overrides"][key], {"2": 2})
        self.assertEqual([e["sequence"] for e in self.durable_events(app)],
                         [1, 2])

    def test_expired_401_credential_rejects_decision_in_ui_without_trace(self):
        # The REAL classroom_request boundary answers 401 with the expired
        # token: the submitted decision must fail closed in front of the
        # teacher, with exactly one provider call and no partial state.
        app = self.run_journey_app(listing=REAL_HTTP_401,
                                   token=EXPIRED_UI_TOKEN)
        key = self.review_key(app)
        app.button(key="qreview_submit_%s_1" % key).click().run()
        self.assertEqual([str(e.value) for e in app.exception], [])
        self.assertEqual(
            "Review decision was rejected: classroom_verification_unavailable",
            app.error[0].value)
        self.assertEqual(app.session_state["question_review_notice"][0],
                         "classroom_verification_unavailable")
        # One outbound call only: courses listing 401s, the decision stops
        # before the assignment listing, nothing is cached or retried.
        self.assertEqual(len(app.session_state["http_calls"]), 1)
        self.assertEqual(
            app.session_state["http_calls"][0]["headers"]["Authorization"],
            "Bearer " + EXPIRED_UI_TOKEN)
        # No partial state anywhere: no decision record, no override, no
        # durable event, and the store file was never created.
        self.assertNotIn("%s::q1" % key, app.session_state.get(
            "question_review_decisions", {}))
        self.assertNotIn(key, app.session_state.get("teacher_overrides", {}))
        self.assertEqual(self.durable_events(app), [])
        self.assertFalse(os.path.exists(self.store_path))

        # A rerun re-shows the fail-closed notice and makes NO further
        # provider call: nothing half-written can silently become visible
        # later, and the refusal is replayed from session state only.
        app.run()
        self.assertEqual(
            "Review decision was rejected: classroom_verification_unavailable",
            app.error[0].value)
        self.assertEqual(len(app.session_state["http_calls"]), 1)
        self.assertNotIn("%s::q1" % key, app.session_state.get(
            "question_review_decisions", {}))
        self.assert_publication_surface_absent(app)


if __name__ == "__main__":
    unittest.main()
