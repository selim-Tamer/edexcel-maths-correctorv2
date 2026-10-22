"""Real Streamlit UI tests for the Phase C5 scheme-review panel.

Checks that the review panel renders findings safely for anonymous and
authenticated teachers, that acknowledgement writing is owner-bound and
fail-closed, and that logout clears the acknowledgement state. Uses the
headless loader plus the real Streamlit widget runtime; no OAuth, Gemini or
Classroom calls.
"""
import json
import os
import sys
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))

FLAGGED_INFO = ({"1a!": 2}, {}, None)


def bootstrap(identity=None):
    return f"""
import json, sys
sys.path.insert(0, {str(TESTS)!r})
import streamlit as st
from support import load_app
f = load_app()
f.update(st=st)
SUBJECT = {"None" if identity is None else json.dumps(identity)}
if SUBJECT != "None":
    f["authenticated_google_identity"] = lambda: SUBJECT
    f["google_logged_in"] = lambda: True
f["render_scheme_review_panel"]({FLAGGED_INFO!r})
def record_ack(_):
    f["save_scheme_review_acknowledgement"]("synthetic-revision")
st.button("Record acknowledgement", key="record-ack", on_click=record_ack,
          args=("synthetic-revision",))
states = {{
    "ack_save_result": bool(f["save_scheme_review_acknowledgement"]
                            ("synthetic-revision")),
    "ack_state": f["scheme_review_acknowledgement_state"]("synthetic-revision"),
    "bogus_state": f["scheme_review_acknowledgement_state"]("unknown-revision"),
}}
st.caption(json.dumps(states))
"""


class SchemeReviewPanelTests(unittest.TestCase):
    def auth_app(self, identity="teacher-a-subject"):
        app = AppTest.from_string(bootstrap(identity), default_timeout=20).run()
        self.assertEqual(len(app.exception), 0)
        return app

    @staticmethod
    def publish(app, needle="ack_save_result"):
        caps = [item for item in app.caption if needle in item.value]
        return json.loads(caps[0].value)

    def test_findings_render_and_anonymous_cannot_acknowledge(self):
        app = AppTest.from_string(bootstrap(None), default_timeout=20).run()
        self.assertEqual(len(app.exception), 0)
        warnings = " ".join(str(item.value) for item in app.warning)
        self.assertIn("C5-QID-INVALID", warnings)
        published = self.publish(app)
        self.assertFalse(published["ack_save_result"])
        self.assertEqual(published["ack_state"], "absent")
        self.assertEqual(published["bogus_state"], "absent")
        self.assertFalse(app.checkbox[0].value)
        self.assertTrue(app.checkbox[0].disabled)

    def test_authenticated_acknowledgement_is_recorded_and_owner_bound(self):
        app = self.auth_app()
        published = self.publish(app)
        self.assertTrue(published["ack_save_result"])
        self.assertEqual(published["ack_state"], "recorded")
        self.assertFalse(app.checkbox[0].value)

    def test_checkbox_does_not_clear_blockers_approve_scheme_or_publish_grades(self):
        script = bootstrap("teacher-a-subject") + """
review = f["scheme_review_for_info"](({'1a!': 2}, {}, None))
st.caption(json.dumps({
    "approval_boundary": True,
    "status": f["scheme_review"].review_summary(review)["status"],
    "unresolved": len(f["scheme_review"].unresolved_findings(review)),
    "result_approvals": bool(st.session_state.get("classroom_grade_approvals")),
    "overrides": bool(st.session_state.get("teacher_overrides")),
}))
"""
        app = AppTest.from_string(script, default_timeout=20).run()
        app.checkbox[0].check().run()
        self.assertEqual(len(app.exception), 0)
        self.assertTrue(app.checkbox[0].value)
        published = self.publish(app, needle="approval_boundary")
        self.assertEqual(published["status"], "NEEDS_TEACHER_REVIEW")
        self.assertGreater(published["unresolved"], 0)
        self.assertFalse(published["result_approvals"])
        self.assertFalse(published["overrides"])
        self.assertTrue(app.warning)

    def test_logout_clears_scheme_review_acknowledgements(self):
        script = bootstrap("teacher-a-subject") + """
f["invalidate_sensitive_state"]()
cleared = "scheme_review_approvals" not in st.session_state
state = f["scheme_review_acknowledgement_state"]("synthetic-revision")
st.caption(json.dumps({"cleared": cleared, "state": state}))
"""
        app = AppTest.from_string(script, default_timeout=20).run()
        self.assertEqual(len(app.exception), 0)
        published = self.publish(app, needle="cleared")
        self.assertTrue(published["cleared"])
        self.assertEqual(published["state"], "absent")

    def test_other_teacher_never_receives_the_acknowledgement(self):
        script = bootstrap("teacher-a-subject") + """
f["authenticated_google_identity"] = lambda: "teacher-b-subject"
st.caption(json.dumps(
    {"marker": "post_switch",
     "state": f["scheme_review_acknowledgement_state"]("synthetic-revision"),
     "save": f["save_scheme_review_acknowledgement"]("synthetic-revision")}))
"""
        app = AppTest.from_string(script, default_timeout=20).run()
        self.assertEqual(len(app.exception), 0)
        published = self.publish(app, needle="post_switch")
        self.assertEqual(published["state"], "owner_mismatch")
        self.assertEqual(published["save"], False)

    def test_foreign_teacher_click_never_checks_the_widget(self):
        script = bootstrap("teacher-a-subject") + """
f["authenticated_google_identity"] = lambda: "teacher-b-subject"
"""
        app = AppTest.from_string(script, default_timeout=20).run()
        self.assertEqual(len(app.exception), 0)
        self.assertFalse(app.checkbox[0].value)
        app.checkbox[0].check().run()
        self.assertEqual(len(app.exception), 0)
        # The writer fails closed and returns the widget to unchecked.
        self.assertFalse(app.checkbox[0].value)


if __name__ == "__main__":
    unittest.main()

