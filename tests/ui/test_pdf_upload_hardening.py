"""End-to-end Streamlit check: unsafe manual uploads are rejected before Gemini.

Drives the real production Mark button block through AppTest with an invalid
student upload and asserts the dedicated Stage S3 rejection message appears
and that no provider upload was attempted.
"""
import sys
import unittest
from pathlib import Path
import pandas as pd
from streamlit.testing.v1 import AppTest

TESTS = Path(__file__).resolve().parents[1]
BOOTSTRAP = f'''
import ast, sys
from types import SimpleNamespace
from unittest.mock import Mock
sys.path.insert(0, {str(TESTS)!r})
import pandas as pd
import streamlit as st
from support import load_app, SOURCE, benchmark, model_question
from pipeline_fakes import Clock, Provider, uploaded
import document_pipeline as documents
f = load_app()
f.update(st=st, pd=pd)
if "doc_fixture" not in st.session_state:
    clock = Clock()
    provider = Provider(clock, transfer=0, processing=0)
    state = {{"files": documents.FileCache("synthetic-account", clock=clock),
              "metadata": documents.Memo(clock=clock),
              "results": documents.Memo(clock=clock)}}
    st.session_state["doc_fixture"] = {{"provider": provider, "state": state,
                                        "marks": 0, "schemes": 0}}
fixture = st.session_state["doc_fixture"]
f["mark_one_submission"] = Mock(side_effect=AssertionError("must not mark"))
f["types"].HttpOptions = lambda **kw: kw
f.update(genai=SimpleNamespace(Client=lambda **kw: fixture["provider"]),
         api_key="synthetic-never-sent", ready=True,
         authenticated_google_identity=lambda: "synthetic-teacher-sub",
         # Unsafe upload: no PDF signature despite the .pdf name.
         student_pdfs=[uploaded(b"definitely not a pdf", "unsafe.pdf")],
         mark_scheme_pdf=uploaded(b"%PDF-scheme", "scheme.pdf"),
         question_pdf=None, audit_enabled=False, random_question_count=None,
         document_state=fixture["state"], retain_references=False)


def norm(text):
    return text.replace("(", "").replace(")", "").replace(" ", "")


st.checkbox("Unrelated rerun", key="rerun_control")
for node in ast.parse(SOURCE.read_text(encoding="utf-8")).body:
    if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in (
                "manual_run_requested", "manual_run_authorized")
            for t in node.targets):
        exec(compile(ast.Module(body=[node], type_ignores=[]),
                     str(SOURCE), "exec"), f)
    elif isinstance(node, ast.If) and norm(ast.unparse(node.test)) in (
            norm("manual_run_requested and not manual_run_authorized"),
            norm("manual_run_authorized")):
        exec(compile(ast.Module(body=[node], type_ignores=[]),
                     str(SOURCE), "exec"), f)
'''


class ManualUploadHardeningTests(unittest.TestCase):
    def test_block_rejects_unsafe_upload_without_gemini_activity(self):
        app = AppTest.from_string(BOOTSTRAP, default_timeout=60).run()
        self.assertFalse(app.exception)
        app.button[0].click().run()
        self.assertFalse(app.exception)
        fixture = app.session_state["doc_fixture"]
        # No client upload, no polling, no marking, nothing cached.
        fixture["provider"].files.upload.assert_not_called()
        fixture["provider"].files.get.assert_not_called()
        fixture["provider"].files.delete.assert_not_called()
        self.assertEqual(fixture["marks"], 0)
        # The dedicated Stage S3 clause is what surfaced the rejection.
        messages = [error.value for error in app.error]
        self.assertTrue(
            any("rejected" in m.lower() and "nothing was sent to gemini" in m.lower()
                for m in messages),
            f"expected upload-rejection error, got: {messages}")
        self.assertTrue(
            all("unsafe.pdf" not in m for m in messages),
            "rejection message must not echo the file name")


if __name__ == "__main__":
    unittest.main()
