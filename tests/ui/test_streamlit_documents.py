"""Execute the production Mark button block through real Streamlit reruns."""
import sys
import unittest
from pathlib import Path
# Import this test dependency before starting AppTest's script deadline. Cold
# native imports on Windows can exceed the UI interaction timeout.
import pandas as pd
from streamlit.testing.v1 import AppTest
TESTS=Path(__file__).resolve().parents[1]
BOOTSTRAP=f'''
import ast, sys
from types import SimpleNamespace
from unittest.mock import Mock
sys.path.insert(0, {str(TESTS)!r})
import pandas as pd
import streamlit as st
from support import load_app, SOURCE, benchmark, model_question
from pipeline_fakes import Clock, Provider, uploaded
import document_pipeline as documents
f=load_app()
f.update(st=st, pd=pd)
if "doc_fixture" not in st.session_state:
    clock=Clock()
    provider=Provider(clock,transfer=0,processing=0)
    state={{"files":documents.FileCache("synthetic-account",clock=clock), "metadata":documents.Memo(clock=clock), "results":documents.Memo(clock=clock)}}
    st.session_state["doc_fixture"]={{"provider":provider,"state":state,"marks":0,"schemes":0}}
fixture=st.session_state["doc_fixture"]
def extract(*args):
    fixture["schemes"]+=1
    return {{"1":2}},{{"1":"4"}},None
def mark(*args, **kwargs):
    fixture["marks"]+=1
    if kwargs.get("status_callback"):
        kwargs["status_callback"]("Checking marking evidence…")
    result=f["normalise_result"]({{"questions":[model_question(benchmark()["questions"][0],"partial")]}})
    return {{"student_name":"synthetic.pdf","status":"Marked","result":result,"audit_warning":None,"totals":{{}},"groups":[]}}
f["extract_scheme_maxima"]=extract
f["mark_one_submission"]=mark
f["types"].HttpOptions=lambda **kw:kw
f.update(genai=SimpleNamespace(Client=lambda **kw:fixture["provider"]),
    api_key="synthetic-never-sent", ready=True,
    authenticated_google_identity=lambda:"synthetic-teacher-sub",
    student_pdfs=[uploaded(b"student")], mark_scheme_pdf=uploaded(b"scheme","scheme.pdf"),
    question_pdf=None, audit_enabled=False,random_question_count=None,
    document_state=fixture["state"],retain_references=False)
def norm(text):
    return text.replace("(","").replace(")","").replace(" ","")
st.checkbox("Unrelated rerun",key="rerun_control")
for node in ast.parse(SOURCE.read_text(encoding="utf-8")).body:
    if isinstance(node,ast.Assign) and any(
            isinstance(t,ast.Name) and t.id in ("manual_run_requested","manual_run_authorized")
            for t in node.targets):
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(SOURCE),"exec"),f)
    elif isinstance(node,ast.If) and norm(ast.unparse(node.test)) in (
            norm("manual_run_requested and not manual_run_authorized"),
            norm("manual_run_authorized")):
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(SOURCE),"exec"),f)
'''
class StreamlitDocumentTests(unittest.TestCase):
    def app(self):
        app=AppTest.from_string(BOOTSTRAP,default_timeout=20).run()
        self.assertFalse(app.exception)
        return app
    def test_unrelated_reruns_do_not_upload(self):
        app=self.app(); app.checkbox(key='rerun_control').check().run(); app.run()
        self.assertEqual(app.session_state['doc_fixture']['provider'].files.upload.call_count,0)
    def test_repeat_mark_skips_uploads_preparation_and_marking(self):
        app=self.app(); app.button[0].click().run()
        self.assertFalse(app.exception)
        fixture=app.session_state['doc_fixture']
        self.assertEqual(fixture['provider'].files.upload.call_count,2)
        self.assertEqual(fixture['provider'].files.delete.call_count,2)
        self.assertEqual(fixture['marks'],1); self.assertEqual(fixture['schemes'],1)
        app.button[0].click().run()
        self.assertFalse(app.exception)
        self.assertEqual(fixture['provider'].files.upload.call_count,2)
        self.assertEqual(fixture['marks'],1); self.assertEqual(fixture['schemes'],1)
        stages=[r['Stage'] for r in app.session_state['last_document_timings']]
        self.assertIn('All results reused; no Gemini requests',stages)
        app.checkbox(key='rerun_control').check().run()
        self.assertEqual(fixture['provider'].files.upload.call_count,2)
