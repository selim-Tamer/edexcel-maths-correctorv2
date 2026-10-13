"""Real Streamlit rendering of historical snapshot controls; all data synthetic."""
import sys
import unittest
from pathlib import Path
from streamlit.testing.v1 import AppTest

TESTS = Path(__file__).resolve().parents[1]


class SnapshotUITests(unittest.TestCase):
    def test_snapshot_download_is_historical_and_survives_rerun(self):
        script = f'''
import sys
sys.path.insert(0, {str(TESTS)!r})
import streamlit as st
from support import load_app, benchmark, model_question
f = load_app()
f['st'] = st
if 'previous_result_snapshots' not in st.session_state:
    result = f['normalise_result']({{'questions': [model_question(benchmark()['questions'][0], 'partial')]}})
    row = f['new_review_row']({{'status': 'Marked', 'result': result}}, 'manual')
    f['preserve_result_snapshot']([row], 'manual')
f['render_result_snapshots']()
'''
        app = AppTest.from_string(script, default_timeout=30).run()
        for _ in range(2):
            self.assertEqual(len(app.exception), 0)
            self.assertIn('historical results', app.caption[0].value)
            self.assertEqual(len(app.get('download_button')), 1)
            self.assertEqual(len(app.checkbox), 0)
            self.assertFalse(app.session_state['previous_result_snapshots']['manual'][0]['is_current'])
            app.run()
