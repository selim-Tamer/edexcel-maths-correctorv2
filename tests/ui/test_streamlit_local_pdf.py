import sys
import unittest
from pathlib import Path
from streamlit.testing.v1 import AppTest

TESTS = Path(__file__).resolve().parents[1]
SCRIPT = f'''
import sys
sys.path.insert(0, {str(TESTS)!r})
import streamlit as st
from support import load_app
from pipeline_fakes import uploaded
from pdf_cases import make_pdf
f=load_app()
f['st']=st
f['render_local_pdf_inspector']([uploaded(make_pdf(), 'synthetic.pdf')])
'''


class LocalPDFUITests(unittest.TestCase):
    def test_explicit_inspection_then_rerun_does_not_retain_text(self):
        app = AppTest.from_string(SCRIPT, default_timeout=30).run()
        self.assertFalse(app.exception)
        self.assertEqual(len(app.code), 0)
        app.button(key='local_pdf_inspect').click().run()
        self.assertFalse(app.exception)
        self.assertIn('(67 + 1) / 2 = 34', app.code[0].value)
        app.run()
        self.assertEqual(len(app.code), 0)

    def test_bad_page_numbers_safe_warning(self):
        app = AppTest.from_string(SCRIPT, default_timeout=30).run()
        app.text_input(key='local_pdf_pages').set_value('999').run()
        app.button(key='local_pdf_inspect').click().run()
        self.assertFalse(app.exception)
        self.assertTrue(app.warning)
        self.assertEqual(len(app.code), 0)
