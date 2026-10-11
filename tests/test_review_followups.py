"""State-transition and credential regressions using synthetic data only."""
import ast
import copy
import os
import unittest
from unittest.mock import Mock, patch

from support import SOURCE, load_app
import test_review_safety as safety


class CallbackUI(safety.RecordingUI):
    def __init__(self):
        super().__init__()
        self.buttons = {}
        self.inputs = []
        self.checkboxes = []
        self.secrets = {}
        self.errors = []

    def error(self, message):
        self.errors.append(message)

    def button(self, label, **kwargs):
        self.buttons[label] = kwargs
        return False

    def text_input(self, label, **kwargs):
        self.inputs.append((label, kwargs))
        return kwargs.get("value", "")

    def checkbox(self, label, **kwargs):
        self.checkboxes.append((label, kwargs))
        return kwargs.get("value", False)


class ReviewFollowupTests(unittest.TestCase):
    setUp = safety.SafetyRegressionTests.setUp
    row = safety.SafetyRegressionTests.row
    configure_job = safety.ClassroomStateTests.configure_job
    run_job = safety.ClassroomStateTests.run_job

    def test_registered_override_callback_reads_latest_widget_values(self):
        self.ui = CallbackUI()
        self.f["st"] = self.ui
        row = self.row()
        key = "classroom_review"
        self.ui.session_state["classroom_grade_approvals"] = {key: True}
        self.f["render_teacher_override_controls"](key, row["result"])
        callback = self.ui.buttons["Apply override"]
        # Streamlit updates widget state before calling the registered callback.
        self.ui.session_state[f"override_mark_{key}_1"] = 2
        self.ui.session_state[f"override_reason_{key}_1"] = "Checked latest working"
        callback["on_click"](*callback["args"])
        self.assertEqual(self.ui.session_state["teacher_overrides"][key]["1"], 2)
        self.assertEqual(self.ui.session_state["teacher_override_reasons"][key]["1"],
                         "Checked latest working")
        self.assertNotIn(key, self.ui.session_state["classroom_grade_approvals"])

    def test_failed_forced_remark_is_retried_by_normal_check(self):
        for failure in ("Failed", "Skipped"):
            with self.subTest(status=failure):
                client = self.configure_job()
                marked = copy.deepcopy(self.f["mark_one_classroom_submission"].return_value)
                processed = set()
                self.run_job(client, processed)
                self.f["mark_one_classroom_submission"].return_value = {
                    "submission_id": "s", "student": "Synthetic", "status": failure,
                    "reason": "Temporary failure", "result": None}
                self.run_job(client, processed, force=True)
                self.f["mark_one_classroom_submission"].return_value = marked
                retried = self.run_job(client, processed)
                self.assertEqual(retried["pending"], 1)
                self.assertEqual(retried["processed"][0]["status"], "Marked")

    def test_withdrawal_clears_approval_and_resubmission_needs_new_review(self):
        client = self.configure_job()
        processed = set()
        first = self.run_job(client, processed)
        old_key = first["processed"][0]["review_key"]
        self.ui.session_state["classroom_grade_approvals"] = {old_key: True}
        self.ui.session_state[f"classroom_approval_checkbox_{old_key}"] = True
        self.f["list_turned_in_submissions"].return_value = []
        empty = self.run_job(client, processed)
        history = self.f["merge_classroom_results"](first, empty, processed)
        row = history["processed"][0]
        details = self.f["classroom_grade_details"](row)
        self.assertFalse(row["is_current"])
        self.assertFalse(details["approved"])
        self.assertEqual(details["display_grade"], "—")
        self.assertIn("Historical", details["approval_status"])
        self.assertNotIn(old_key, self.ui.session_state["classroom_grade_approvals"])
        self.assertFalse(self.ui.session_state[f"classroom_approval_checkbox_{old_key}"])
        self.f["list_turned_in_submissions"].return_value = [{"id": "s", "state": "TURNED_IN"}]
        returned = self.run_job(client, processed)
        self.assertEqual(returned["pending"], 1)
        fresh = self.f["merge_classroom_results"](history, returned, processed)["processed"][0]
        self.assertNotEqual(fresh["review_key"], old_key)
        self.assertTrue(fresh["is_current"])
        self.assertFalse(self.f["classroom_grade_details"](fresh)["approved"])

    def test_unchanged_current_result_keeps_approval(self):
        client = self.configure_job()
        processed = set()
        first = self.run_job(client, processed)
        key = first["processed"][0]["review_key"]
        self.ui.session_state["classroom_grade_approvals"] = {key: True}
        unchanged = self.run_job(client, processed)
        result = self.f["merge_classroom_results"](first, unchanged, processed)
        self.assertTrue(self.f["classroom_grade_details"](result["processed"][0])["approved"])


class CredentialTests(unittest.TestCase):
    def sidebar_key_controls(self, environment_key="", secret_key="", test_result=None):
        f = load_app()
        ui = CallbackUI()
        ui.secrets = {"GEMINI_API_KEY": secret_key}
        f.update(st=ui, __file__=str(SOURCE))
        if test_result is not None:
            ui.button = lambda *args, **kwargs: True
            f["test_api_key"] = Mock(return_value=test_result)
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        sidebar = next(n for n in tree.body if isinstance(n, ast.With)
                       and ast.unparse(n.items[0].context_expr) == "st.sidebar")
        # Execute the real credential UI, stopping before unrelated marking settings.
        stop = next(i for i, n in enumerate(sidebar.body) if "App version:" in ast.unparse(n))
        with patch.dict(os.environ, {"GEMINI_API_KEY": environment_key}):
            exec(compile(ast.Module(body=sidebar.body[:stop], type_ignores=[]),
                         str(SOURCE), "exec"), f)
        return f, ui

    def test_server_credentials_are_not_sent_to_input_widgets(self):
        for environment_key, secret_key in [("synthetic-env-key", ""),
                                            ("", "synthetic-secret-key")]:
            with self.subTest(source="environment" if environment_key else "secrets"):
                f, ui = self.sidebar_key_controls(environment_key, secret_key)
                self.assertEqual(f["api_key"], environment_key or secret_key)
                self.assertEqual(ui.inputs, [])

    def test_provider_error_cannot_echo_server_key_to_browser(self):
        _, ui = self.sidebar_key_controls(
            environment_key="synthetic-env-key",
            test_result=(False, "Rejected request containing synthetic-env-key"))
        self.assertEqual(len(ui.errors), 1)
        self.assertNotIn("synthetic-env-key", ui.errors[0])

    def test_manual_key_has_no_shared_persistence_option(self):
        _, ui = self.sidebar_key_controls()
        self.assertEqual(ui.inputs[0][1].get("value", ""), "")
        self.assertFalse(any("Remember" in label for label, _ in ui.checkboxes))


if __name__ == "__main__":
    unittest.main()
