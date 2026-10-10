"""Regressions for review state, audit sequencing and upload ownership."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from support import benchmark, fake_client, load_app, model_question, remote


class RecordingUI:
    def __init__(self):
        self.session_state = {}
        self.metrics = []
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def columns(self, count):
        return [self] * count
    def metric(self, label, value):
        self.metrics.append((label, value))
    def selectbox(self, label, options, **kwargs):
        return list(options)[0]
    def number_input(self, label, **kwargs):
        return kwargs.get("value", 0)
    def text_input(self, *args, **kwargs):
        return ""
    def button(self, *args, **kwargs):
        return False
    def __getattr__(self, name):
        return lambda *args, **kwargs: self


class SafetyRegressionTests(unittest.TestCase):
    def setUp(self):
        self.f = load_app()
        self.ui = RecordingUI()
        self.f["st"] = self.ui
        self.f["pd"] = SimpleNamespace(DataFrame=lambda rows: rows)

    def row(self):
        q = model_question(benchmark()["questions"][0], "partial")
        result = self.f["normalise_result"]({"questions": [q]})
        return {"status": "Marked", "result": result,
                "totals": {"earned": 1, "available": 2, "percentage": 50,
                           "provisional_grade": "5"},
                "groups": self.f["make_group_rows"](result["questions"]),
                "student_name": "Synthetic", "marking_model": "fake",
                "audit_model": "Not run"}

    def test_report_metrics_match_overridden_questions(self):
        row = self.row()
        self.ui.session_state["teacher_overrides"] = {"review": {"1": 2}}
        self.f["render_student_report"](
            "Synthetic", row["result"], row["totals"], row["groups"],
            "fake", "Not run", student_key="review")
        self.assertEqual(self.ui.metrics[0][1], "2 / 2")
        self.assertEqual(row["result"]["questions"][0]["awarded_marks"], 2)

    def test_manual_validator_findings_are_audited(self):
        q = model_question(benchmark()["questions"][0], "correct")
        q["student_answer"] = "999"
        client = fake_client([{"questions": [q]}, {"corrections": []}])
        row = self.f["mark_one_submission"](
            client, "Synthetic", remote(), remote(), None, True,
            scheme_maxima={"1": 2}, scheme_answers={"1": "4"})
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertNotIn("Skipped", row["audit_model"])
        self.assertTrue(row["result"]["validation_warnings"])

    def test_classroom_validator_findings_are_audited(self):
        q = model_question(benchmark()["questions"][0], "correct")
        q["student_answer"] = "999"
        client = fake_client([{"questions": [q]}, {"corrections": []}])
        self.f["get_user_profile"] = Mock(return_value={})
        self.f["download_drive_file"] = Mock(return_value=("synthetic.pdf", b"pdf", "application/pdf"))
        self.f["upload_bytes_to_gemini"] = Mock(return_value=remote())
        row = self.f["mark_one_classroom_submission"](
            "dummy", client, {"id": "s", "assignmentSubmission": {
                "attachments": [{"driveFile": {"id": "f"}}]}},
            {"maxPoints": 100}, remote(name="scheme"), True,
            scheme_maxima={"1": 2}, scheme_answers={"1": "4"})
        self.assertEqual(row["status"], "Marked")
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.assertTrue(row["result"]["validation_warnings"])

    def test_failed_remote_pdf_is_deleted_in_both_upload_paths(self):
        for manual in (True, False):
            with self.subTest(manual=manual):
                client = fake_client()
                client.files.upload.side_effect = None
                client.files.upload.return_value = remote("FAILED", "failed-upload")
                with self.assertRaises(RuntimeError):
                    if manual:
                        self.f["upload_pdf"](client, SimpleNamespace(
                            name="synthetic.pdf", getvalue=lambda: b"synthetic"))
                    else:
                        self.f["upload_bytes_to_gemini"](client, b"synthetic", "synthetic.pdf")
                client.files.delete.assert_called_once_with(name="failed-upload")
