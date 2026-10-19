"""Real-Streamlit student-data isolation tests for Stage S2.1.

Synthetic teachers and results only. No OAuth, Gemini or Classroom requests:
identities are substituted at the single trusted boundary
(`authenticated_google_identity`), while all production ownership logic,
rendering and storage paths run unchanged.
"""
import ast
import sys
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

TESTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS))
from support import SOURCE

NORMALISE = "def norm(text):\n" \
    "    return text.replace('(', '').replace(')', '').replace(' ', '')\n"

ISOLATION_BOOTSTRAP = f'''
import ast, sys
sys.path.insert(0, {str(TESTS)!r})
import pandas as pd
import streamlit as st
from support import SOURCE, load_app, benchmark, model_question
f = load_app()
f.update(st=st, pd=pd)

# Synthetic Teacher A marking session: production write path under identity A.
f["authenticated_google_identity"] = lambda: "teacher-a-subject"
f["google_logged_in"] = lambda: True
result = f["normalise_result"]({{"questions": [model_question(benchmark()["questions"][0], "partial")]}})
row_a = f["new_review_row"]({{"status": "Marked", "result": result, "student_name": "StudentA",
    "marking_model": "gemini-synthetic", "audit_model": "Not run"}}, "manual")
assert row_a["report_owner"] == "google:teacher-a-subject"
if OWNERLESS_ROW:
    stale = dict(row_a)
    stale.pop("report_owner", None)  # simulates a record without owner metadata
    st.session_state["last_batch_results"] = [stale]
else:
    st.session_state["last_batch_results"] = [row_a]
    st.session_state["previous_result_snapshots"] = {{"manual": [dict(row_a)]}}
    st.session_state["classroom_jobs"] = {{
        "synthetic": {{"processed": set(),
                       "last_result": {{"processed": [row_a], "active_submission_ids": [], "pending": 0}}}}}}
st.session_state["sensitive_state_owner"] = "google:teacher-a-subject"
st.session_state["teacher_overrides"] = {{row_a["review_key"]: {{"1": 1}}}}
st.session_state["teacher_override_reasons"] = {{row_a["review_key"]: {{"1": "A override"}}}}
st.session_state["classroom_grade_approvals"] = {{row_a["review_key"]: True}}
st.session_state["manual_report_owner"] = "stale-anonymous-id"

# The authenticated subject changes (account switch, expiry or logout).
f["authenticated_google_identity"] = lambda: CURRENT_SUBJECT
f["google_logged_in"] = lambda: CURRENT_SUBJECT is not None
# Override writes happen through the production guard exactly as a leftover
# or forged widget event would.
f["save_teacher_override"](row_a["review_key"], "1", 2, "B attempt")
f["save_classroom_approval"](row_a["review_key"])

# Run the production reconciliation and the production manual-results render
# block, selected verbatim from the source tree.
tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
blocks = []
for node in tree.body:
    if isinstance(node, ast.Expr) and getattr(getattr(node, "value", None), "func", None):
        if getattr(node.value.func, "id", "") == "reconcile_sensitive_state":
            blocks.append(node)
    if isinstance(node, ast.If) and "last_batch_results" in ast.unparse(node.test):
        blocks.append(node)
assert blocks, "production blocks not found"
exec(compile(ast.Module(body=blocks, type_ignores=[]), str(SOURCE), "exec"), f)
f["render_result_snapshots"]()
'''

RENDER_BOOTSTRAP = f'''
import ast, sys
sys.path.insert(0, {str(TESTS)!r})
from types import SimpleNamespace
import pandas as pd
import streamlit as st
from support import SOURCE, load_app, benchmark, model_question
def norm(text):
    return text.replace("(", "").replace(")", "").replace(" ", "")
f = load_app()
f.update(st=st, pd=pd,
         mark_scheme_pdf=SimpleNamespace(getvalue=lambda: b"scheme", name="scheme.pdf"))

def current_owner():
    return OWNER_NOW
f["sensitive_state_owner"] = current_owner
f["result_owner"] = current_owner

# Synthetic marked rows exactly as the marking engine produced them.
result = f["normalise_result"]({{"questions": [model_question(benchmark()["questions"][0], "partial")]}})
f["batch_results"] = [{{"student_name": "StudentA", "status": "Marked", "result": result,
    "marking_model": "gemini-synthetic", "audit_model": "Not run"}}]
st.session_state["last_batch_results"] = []
st.session_state["last_manual_context"] = "old"
st.session_state["last_mark_scheme_name"] = "old"

tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
target = None
for node in ast.walk(tree):
    if isinstance(node, ast.If) and norm(ast.unparse(node.test)) == norm(
            "store_ownerisNoneorsensitive_state_owner()!=store_owner"):
        target = node
assert target is not None, "manual store guard not found"
# The guard body reads store_owner from the surrounding scope.
f["store_owner"] = OWNER_BEFORE
exec(compile(ast.Module(body=[target], type_ignores=[]), str(SOURCE), "exec"), f)
'''


def bootstrap(subject, ownerless_row=False):
    script = (ISOLATION_BOOTSTRAP
              .replace("CURRENT_SUBJECT", repr(subject))
              .replace("OWNERLESS_ROW", repr(ownerless_row)))
    return AppTest.from_string(script, default_timeout=300).run()


def rendered_texts(app):
    values = []
    for attribute in ("markdown", "code", "info", "warning", "error", "caption", "metric"):
        for item in getattr(app, attribute):
            values.append(str(item.value))
    for item in app.dataframe:
        try:
            values.append(item.value.to_csv(index=True))
        except Exception:
            values.append(str(item.value))
    return values


class AccountIsolationTests(unittest.TestCase):
    def app(self, subject, ownerless_row=False):
        app = bootstrap(subject, ownerless_row)
        self.assertEqual(len(app.exception), 0)
        return app

    def test_teacher_b_cannot_view_or_export_teacher_a_results(self):
        app = self.app("teacher-b-subject")
        visible = " ".join(rendered_texts(app))
        self.assertNotIn("StudentA", visible)
        self.assertNotIn("A override", visible)
        self.assertEqual(len(app.download_button), 0)
        # Owner switched: Teacher A's whole store is dropped, not just hidden.
        for key in ("last_batch_results", "previous_result_snapshots", "classroom_jobs",
                    "teacher_overrides", "teacher_override_reasons",
                    "classroom_grade_approvals", "sensitive_state_owner",
                    "manual_report_owner"):
            self.assertNotIn(key, app.session_state)

    def test_anonymous_session_after_logout_sees_no_owner_bound_results(self):
        app = self.app(None)
        visible = " ".join(rendered_texts(app))
        self.assertNotIn("StudentA", visible)
        self.assertNotIn("last_batch_results", app.session_state)
        self.assertNotIn("sensitive_state_owner", app.session_state)
        self.assertEqual(len(app.download_button), 0)

    def test_same_teacher_rerun_preserves_authorized_results(self):
        app = self.app("teacher-a-subject")
        visible = " ".join(rendered_texts(app))
        self.assertIn("StudentA", visible)
        self.assertIn("last_batch_results", app.session_state)
        self.assertIn("previous_result_snapshots", app.session_state)
        self.assertEqual(app.session_state.get("sensitive_state_owner"), "google:teacher-a-subject")
        self.assertTrue(any("Download" in b.label for b in app.download_button))

    def test_teacher_b_cannot_apply_overrides_or_approvals_to_teacher_a_results(self):
        app = self.app("teacher-b-subject")
        self.assertNotIn("teacher_overrides", app.session_state)
        self.assertNotIn("teacher_override_reasons", app.session_state)
        self.assertNotIn("classroom_grade_approvals", app.session_state)

    def test_malformed_owner_identifier_fails_closed(self):
        # A row without any owner metadata is never rendered or exported.
        app = self.app("teacher-a-subject", ownerless_row=True)
        visible = " ".join(rendered_texts(app))
        self.assertNotIn("StudentA", visible)
        self.assertEqual(len(app.download_button), 0)


class StaleRecordWriteTests(unittest.TestCase):
    def test_results_discarded_when_account_changes_during_marking_run(self):
        script = (RENDER_BOOTSTRAP
                  .replace("OWNER_NOW", "'google:teacher-b-subject'")
                  .replace("OWNER_BEFORE", "'google:teacher-a-subject'"))
        app = AppTest.from_string(script, default_timeout=300).run()
        self.assertEqual(len(app.exception), 0)
        errors = [item.value for item in app.error]
        self.assertTrue(any("discarded to protect student data" in e for e in errors), errors)
        # The whole previous owner's context is invalidated, not just the batch.
        for key in ("last_batch_results", "last_manual_context", "last_mark_scheme_name"):
            self.assertNotIn(key, app.session_state)
        self.assertNotIn("sensitive_state_owner", app.session_state)

    def test_results_stored_when_owner_is_unchanged(self):
        script = (RENDER_BOOTSTRAP
                  .replace("OWNER_NOW", "'google:teacher-a-subject'")
                  .replace("OWNER_BEFORE", "'google:teacher-a-subject'"))
        app = AppTest.from_string(script, default_timeout=300).run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(len(app.error), 0)
        self.assertEqual(len(app.session_state["last_batch_results"]), 1)
        self.assertEqual(app.session_state["last_batch_results"][0]["report_owner"],
                         "google:teacher-a-subject")
        self.assertEqual(app.session_state["sensitive_state_owner"],
                         "google:teacher-a-subject")

    def test_results_discarded_when_run_started_without_an_owner(self):
        script = (RENDER_BOOTSTRAP
                  .replace("OWNER_NOW", "None")
                  .replace("OWNER_BEFORE", "None"))
        app = AppTest.from_string(script, default_timeout=300).run()
        self.assertEqual(len(app.exception), 0)
        errors = [item.value for item in app.error]
        self.assertTrue(any("discarded to protect student data" in e for e in errors), errors)
        self.assertNotIn("last_batch_results", app.session_state)
        self.assertNotIn("sensitive_state_owner", app.session_state)


class LogoutCleanupTests(unittest.TestCase):
    def test_explicit_logout_cleanup_clears_sensitive_state_and_keeps_shared_settings(self):
        script = f'''
import sys
sys.path.insert(0, {str(TESTS)!r})
import streamlit as st
from support import load_app
f = load_app()
f.update(st=st)
st.session_state["marking_mode"] = "Mark entire homework"
for key in ("last_batch_results", "previous_result_snapshots", "teacher_overrides",
            "teacher_override_reasons", "classroom_grade_approvals", "classroom_jobs",
            "last_manual_context", "last_mark_scheme_name", "snapshot_history_trimmed"):
    st.session_state[key] = {{"synthetic": True}}
st.session_state["classroom_approval_checkbox_classroom_x"] = True
st.session_state["override_mark_classroom_x_1"] = 3
f["invalidate_sensitive_state"]()
'''
        app = AppTest.from_string(script, default_timeout=120).run()
        self.assertEqual(len(app.exception), 0)
        for key in ("last_batch_results", "previous_result_snapshots", "teacher_overrides",
                    "teacher_override_reasons", "classroom_grade_approvals", "classroom_jobs",
                    "last_manual_context", "last_mark_scheme_name", "snapshot_history_trimmed",
                    "classroom_approval_checkbox_classroom_x", "override_mark_classroom_x_1",
                    "manual_report_owner", "sensitive_state_owner"):
            self.assertNotIn(key, app.session_state)
        # Safe shared application configuration survives logout.
        self.assertEqual(app.session_state.get("marking_mode"), "Mark entire homework")

    def test_logout_handler_cleanup_then_login_does_not_restore_data(self):
        script = f'''
import sys
sys.path.insert(0, {str(TESTS)!r})
import streamlit as st
from support import load_app
f = load_app()
f.update(st=st)
f["authenticated_google_identity"] = lambda: "teacher-a-subject"
f["google_logged_in"] = lambda: True
st.session_state["last_batch_results"] = ["teacher-a row"]
st.session_state["sensitive_state_owner"] = "google:teacher-a-subject"
# Explicit logout invalidates first...
f["invalidate_sensitive_state"]()
# ...then Teacher B signs in and reconciliation must leave nothing behind.
f["authenticated_google_identity"] = lambda: "teacher-b-subject"
f["google_logged_in"] = lambda: True
f["reconcile_sensitive_state"]()
'''
        app = AppTest.from_string(script, default_timeout=120).run()
        self.assertEqual(len(app.exception), 0)
        self.assertNotIn("last_batch_results", app.session_state)
        self.assertNotIn("sensitive_state_owner", app.session_state)


class ErrorPrivacyTests(unittest.TestCase):
    def setUp(self):
        from support import load_app
        self.app = load_app()

    def test_gemini_error_is_shown_as_content_free_category(self):
        exc = RuntimeError(
            "429 GET v1beta/models: Quota exceeded; prompt echoed: "
            "'Synthetic student wrote 34 and / 2 = 17'")
        reason = self.app["marking_failure_reason"](exc)
        self.assertIn("rate or usage limit", reason)
        self.assertNotIn("34 and / 2", reason)
        self.assertNotIn("Synthetic student wrote", reason)

    def test_parse_failure_category_for_validator_errors(self):
        reason = self.app["marking_failure_reason"](ValueError("bad JSON: {synthetic}"))
        self.assertIn("could not be parsed", reason)
        self.assertNotIn("bad JSON", reason)

    def test_classroom_error_redacts_oauth_token_and_gemini_key(self):
        exc = RuntimeError(
            "401 Unauthorized token=Bearer oauth-token-synthetic key=gemini-key-synthetic")
        message = self.app["safe_error_detail"](exc, "gemini-key-synthetic",
                                                "oauth-token-synthetic")
        self.assertNotIn("oauth-token-synthetic", message)
        self.assertNotIn("gemini-key-synthetic", message)
        self.assertIn("AUTH_REJECTED", message)
        # No exception body at all may reach the UI, not even redacted text.
        self.assertNotIn("Unauthorized", message)
        self.assertNotIn("Bearer", message)

    def test_manual_batch_error_detail_is_bounded_and_body_free(self):
        exc = RuntimeError("x" * 5000)
        message = self.app["safe_error_detail"](exc, "unused-secret")
        self.assertLess(len(message), 80)
        self.assertNotIn("xxxx", message)

    def test_unknown_credentials_and_student_like_text_never_reach_the_ui(self):
        cases = (
            (RuntimeError(
                "500 attachment sk-unknown-credential-987 failed; prompt: "
                "'Synthetic Student wrote 34 and / 2 = 17'"),
             ["sk-unknown-credential-987", "34 and / 2", "Synthetic Student"],
             "SERVICE_UNAVAILABLE"),
            (ValueError(
                "bad response, token=Bearer unknown-oauth-token-value, student: Amina"),
             ["Bearer unknown-oauth-token-value", "Amina"],
             "RESPONSE_INVALID"),
        )
        for exc, forbidden_texts, code in cases:
            with self.subTest(code=code):
                message = self.app["safe_error_detail"](exc, "unused-secret")
                for text in forbidden_texts:
                    self.assertNotIn(text, message)
                self.assertEqual(message, f"{type(exc).__name__} [{code}]")
        # The per-student path is content-free as well.
        shown = self.app["marking_failure_reason"](
            RuntimeError("Quota exceeded sk-unknown-credential-987 Amina wrote 34"))
        self.assertIn("rate or usage limit", shown)
        self.assertNotIn("sk-unknown-credential-987", shown)
        self.assertNotIn("Amina", shown)

    def test_allowlisted_codes_are_stable_and_complete(self):
        expected = {
            RuntimeError("key reported as leaked"): "LEAKED_KEY",
            RuntimeError("401 Unauthorized"): "AUTH_REJECTED",
            RuntimeError("API key not valid. Please pass a valid API key."): "AUTH_REJECTED",
            RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded"): "QUOTA_LIMIT",
            RuntimeError("403 PERMISSION_DENIED"): "PERMISSION_DENIED",
            RuntimeError("503 The model is overloaded"): "SERVICE_UNAVAILABLE",
            RuntimeError("400 INVALID_ARGUMENT bad request"): "REQUEST_INVALID",
            ValueError("json parse error"): "RESPONSE_INVALID",
            OSError("mystery synthetic transport failure"): "UNKNOWN",
        }
        for exc, code in expected.items():
            self.assertEqual(self.app["provider_error_code"](exc), code)
            self.assertTrue(self.app["safe_error_detail"](
                exc).endswith(f" [{code}]"))

    def test_classification_branches_still_match_error_categories(self):
        # The stable categories keep the existing status-code triage working.
        for text, expected in (("401 Unauthorized", "rejected"),
                               ("403 Forbidden", "refused access"),
                               ("429 resource exhausted", "rate or usage limit"),
                               ("key reported as leaked", "blocked")):
            reason = self.app["marking_failure_reason"](RuntimeError(text))
            self.assertIn(expected, reason)


class SnapshotMessageTests(unittest.TestCase):
    def test_snapshot_caption_states_the_current_authentication_policy(self):
        script = f'''
import sys
sys.path.insert(0, {str(TESTS)!r})
import streamlit as st
from support import load_app
f = load_app()
f.update(st=st)
st.session_state["manual_report_owner"] = "x"
st.session_state["previous_result_snapshots"] = {{
    "manual": [{{"review_key": "manual_x", "report_owner": "session:x", "status": "Marked"}}]}}
f["render_result_snapshots"]()
'''
        app = AppTest.from_string(script, default_timeout=120).run()
        self.assertEqual(len(app.exception), 0)
        captions = [item.value for item in app.caption]
        self.assertTrue(any("Marking requires Google sign-in" in c for c in captions), captions)
        joined = " ".join(captions)
        self.assertNotIn("authentication is not required for manual marking", joined)


if __name__ == "__main__":
    unittest.main()
