"""Real Streamlit widget lifecycle tests; no OAuth, Gemini or Classroom requests."""
import ast
import sys
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))
from support import SOURCE

# Run production functions and the actual Classroom results block with synthetic
# session data. The headless loader keeps external service boundaries fail-closed.
BOOTSTRAP = f"""
import ast
import sys
sys.path.insert(0, {str(TESTS)!r})
import pandas as pd
import streamlit as st
from support import SOURCE, load_app, benchmark, model_question
f = load_app()
f.update(st=st, pd=pd)
if "classroom_jobs" not in st.session_state or not st.session_state["classroom_jobs"].get("synthetic_context"):
    result = f["normalise_result"]({{"questions": [model_question(benchmark()["questions"][0], "partial")]}})
    row = f["new_review_row"]({{"status":"Marked", "result": result,
        "student":"Synthetic", "submission_id":"s", "marking_model":"fake",
        "audit_model":"Not run", "suggested_classroom_grade":50,
        "classroom_max_points":100, "is_current":True, "revision":"r1"}}, "classroom")
    st.session_state["classroom_jobs"] = {{"synthetic_context": {{
        "processed": set(),
        "last_result": {{"processed":[row], "active_submission_ids":["s"], "pending":0}}}}}}
state = st.session_state["classroom_jobs"]["synthetic_context"]
def withdraw():
    state["last_result"] = f["merge_classroom_results"](
        state["last_result"], {{"processed":[], "active_submission_ids":[], "pending":0}},
        set())
st.button("Simulate withdrawal", key="withdraw", on_click=withdraw)
tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
for parent in ast.walk(tree):
    body = getattr(parent, "body", None)
    if not isinstance(body, list):
        continue
    for index, node in enumerate(body):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "last_result" for t in node.targets):
            block = body[index:index + 2]
f["job_state"] = state
exec(compile(ast.Module(body=block, type_ignores=[]), str(SOURCE), "exec"), f)
"""

class StreamlitReviewTests(unittest.TestCase):
    def test_working_grouping_and_transcription_uncertainty_are_visible(self):
        script = BOOTSTRAP.replace('state = st.session_state["classroom_jobs"]["synthetic_context"]', '''
state = st.session_state["classroom_jobs"]["synthetic_context"]
row = state["last_result"]["processed"][0]
q = row["ai_result"]["questions"][0]
q["student_answer"] = "34"
q["working_summary"] = "(67 + 1) / 2 = 34"
q["transcription_uncertainty"] = "Synthetic fraction bar needs checking"
f["check_question_marks"](row["ai_result"], {"1": 2})
''')
        app = AppTest.from_string(script, default_timeout=20).run()
        self.assertEqual(len(app.exception), 0)
        self.assertIn("(67 + 1) / 2 = 34", " ".join(str(x.value) for x in app.markdown))
        self.assertIn("Synthetic fraction bar needs checking", " ".join(str(x.value) for x in app.warning))
        self.assertFalse(app.checkbox[0].value)

    def app(self):
        app = AppTest.from_string(BOOTSTRAP, default_timeout=20).run()
        self.assertEqual(len(app.exception), 0)
        return app

    def test_apply_uses_new_values_in_same_event_and_resets_approval(self):
        app = self.app()
        app.checkbox[0].check().run()
        self.assertTrue(app.checkbox[0].value)
        # Do not run between editing the inputs and clicking: these updates
        # must be read from Session State by the pre-render button callback.
        app.number_input[0].set_value(2)
        app.text_input[0].set_value("Verified working")
        next(b for b in app.button if b.label == "Apply override").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertFalse(app.checkbox[0].value)
        self.assertEqual(app.metric[0].value, "2 / 2")
        row = app.session_state["classroom_jobs"]["synthetic_context"]["last_result"]["processed"][0]
        self.assertEqual(row["result"]["questions"][0]["teacher_override"]["reason"], "Verified working")
        # A subsequent ordinary rerun must preserve the corrected result.
        app.run()
        self.assertEqual(app.metric[0].value, "2 / 2")

    def test_historical_result_has_no_current_grade_or_editable_approval(self):
        app = self.app()
        app.checkbox[0].check().run()
        app.button(key="withdraw").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertFalse(app.checkbox[0].value)
        self.assertTrue(app.checkbox[0].disabled)
        self.assertEqual(len(app.number_input), 0)
        frame = app.dataframe[0].value
        self.assertEqual(frame.iloc[0]["Status"], "Historical")
        self.assertEqual(frame.iloc[0]["Current suggested Classroom grade"], "—")
        self.assertIn("Historical", frame.iloc[0]["Teacher review"])
        self.assertEqual(app.metric[0].value, "1 / 2")

    def test_unresolved_full_score_shows_review_without_changing_marks(self):
        script = BOOTSTRAP.replace('state = st.session_state["classroom_jobs"]["synthetic_context"]', '''
state = st.session_state["classroom_jobs"]["synthetic_context"]
row = state["last_result"]["processed"][0]
q = row["ai_result"]["questions"][0]
q["awarded_marks"] = 2
q["confidence"] = "Low"
q["audit_review"] = {"status": "unresolved", "reason": "Original handwriting needs review."}
row["ai_result"]["validation_warnings"] = ["Question 1: unresolved source discrepancy"]
''')
        app = AppTest.from_string(script, default_timeout=20).run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.metric[0].value, "2 / 2")
        self.assertIn("unverified", app.metric[0].label.lower())
        self.assertFalse(app.checkbox[0].value)
        visible = " ".join(str(item.value) for item in app.markdown)
        self.assertIn("Original handwriting needs review", visible)
        self.assertIn("marks unchanged by audit", visible)

if __name__ == "__main__":
    unittest.main()
