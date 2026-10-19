"""Real-Streamlit enforcement of the manual marking authentication boundary.

No OAuth, Gemini or Classroom requests: the marked execution path is replaced
by a sentinel so the tests prove the server-side guard stops a forged widget
value *before* the provider path is reached.
"""
import ast
import sys
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))
from support import SOURCE

# Forged-UI bootstrap: the widget state claims the Mark button was pressed,
# the gate body is replaced by a sentinel provider-path marker, and the
# production guard nodes still run against real Streamlit session handling.
FORGED_BOOTSTRAP = f'''
import ast, sys
sys.path.insert(0, {str(TESTS)!r})
import streamlit as st
from support import SOURCE, load_app
def norm(text):
    return text.replace("(", "").replace(")", "").replace(" ", "")
f = load_app()
f["st"] = st
f["api_key"] = "synthetic-never-sent"
tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
for node in ast.walk(tree):
    if isinstance(node, ast.If) and norm(ast.unparse(node.test)) == norm("manual_run_authorized"):
        node.body = [ast.parse("st.code('PROVIDER_PATH_REACHED')").body[0]]
ast.fix_missing_locations(tree)
selected = []
for node in tree.body:
    if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "manual_run_authorized"
            for t in node.targets):
        selected.append(node)
    elif isinstance(node, ast.If) and norm(ast.unparse(node.test)) in (
            norm("manual_run_requested and not manual_run_authorized"),
            norm("manual_run_authorized")):
        selected.append(node)
# Forged widget value: UI-only authorization attempt.
f["manual_run_requested"] = True
exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), f)
'''

SIGN_IN_BOOTSTRAP = f'''
import sys
sys.path.insert(0, {str(TESTS)!r})
import streamlit as st
from support import load_app
f = load_app()
f["st"] = st
f["render_manual_sign_in_requirement"]()
'''


class FirebaseAuthBoundaryTests(unittest.TestCase):
    def test_forged_mark_request_from_anonymous_session_is_rejected(self):
        app = AppTest.from_string(FORGED_BOOTSTRAP, default_timeout=60).run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.code), 0)  # provider path never reached
        errors = [item.value for item in app.error]
        self.assertTrue(
            any("Marking cannot start: sign in with Google first" in e
                for e in errors), errors)

    def test_authenticated_session_reaches_the_marking_path_only(self):
        # Override the identity guard as a legitimate mocked signed-in session;
        # this exercises the gate in the same real-Streamlit harness.
        script = FORGED_BOOTSTRAP.replace(
            'exec(compile(ast.Module(body=selected, type_ignores=[]),'
            ' str(SOURCE), "exec"), f)',
            'f["authenticated_google_identity"] = lambda: "synthetic-teacher-sub"\n'
            'exec(compile(ast.Module(body=selected, type_ignores=[]),'
            ' str(SOURCE), "exec"), f)')
        app = AppTest.from_string(script, default_timeout=60).run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.error), 0)
        provider_reached = [item.value for item in app.code]
        self.assertEqual(provider_reached, ["PROVIDER_PATH_REACHED"])


class SignInRequirementUITests(unittest.TestCase):
    def test_anonymous_session_sees_sign_in_requirement_without_login_button(self):
        # No [auth] configuration in the test environment: the sign-in button
        # must be absent and the requirement still stated clearly.
        app = AppTest.from_string(SIGN_IN_BOOTSTRAP, default_timeout=60).run()
        self.assertEqual(len(app.exception), 0)
        infos = [item.value for item in app.info]
        self.assertTrue(
            any("Automatic marking is disabled until you sign in with Google" in m
                for m in infos), infos)
        self.assertEqual(len(app.button), 0)

    def test_explanatory_text_never_contains_secret_values(self):
        app = AppTest.from_string(SIGN_IN_BOOTSTRAP, default_timeout=60).run()
        for item in list(app.info) + list(app.caption):
            self.assertNotIn("synthetic-never-sent", item.value)


if __name__ == "__main__":
    unittest.main()
