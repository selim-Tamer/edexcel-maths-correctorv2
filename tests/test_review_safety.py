from pipeline_fakes import Clock
"""Regressions for review state, audit sequencing and upload ownership."""
import ast
import copy
import csv
import io
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from support import SOURCE, benchmark, fake_client, load_app, model_question, remote, response


class RecordingUI:
    def __init__(self):
        self.session_state = {}
        self.metrics = []
        self.downloads = []
        self.tables = []
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def download_button(self, label, **kwargs):
        self.downloads.append((label, kwargs))
    def dataframe(self, frame, **kwargs):
        self.tables.append(frame.rows if hasattr(frame, 'rows') else frame)
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


class Frame:
    def __init__(self, rows):
        self.rows = rows
    def to_csv(self, index=False):
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=list(self.rows[0]) if self.rows else [])
        writer.writeheader()
        writer.writerows(self.rows)
        return stream.getvalue()


class ReviewStateTests(unittest.TestCase):
    setUp = SafetyRegressionTests.setUp
    row = SafetyRegressionTests.row
    def test_new_run_same_filename_cannot_inherit_override_or_approval(self):
        original = self.row()
        first = self.f["new_review_row"](original, "manual")
        # The S2.1 ownership guard requires result rows registered in an
        # owner-scoped store; register first's row as the session's own.
        self.ui.session_state["last_batch_results"] = [first]
        self.f["save_teacher_override"](first["review_key"], "1", 2, "Checked working")
        self.f["prepare_review_row"](first)
        second = self.f["new_review_row"](original, "manual")
        self.f["prepare_review_row"](second)
        self.assertNotEqual(first["review_key"], second["review_key"])
        self.assertEqual(first["totals"]["earned"], 2)
        self.assertEqual(second["totals"]["earned"], 1)
        self.assertEqual(original["result"]["questions"][0]["awarded_marks"], 1)
        self.assertEqual(first["ai_result"]["questions"][0]["awarded_marks"], 1)

    def test_rerenders_are_idempotent_and_removing_override_restores_ai(self):
        row = self.f["new_review_row"](self.row(), "manual")
        self.ui.session_state["last_batch_results"] = [row]  # owner guard store
        self.f["save_teacher_override"](row["review_key"], "1", 2, "Teacher checked")
        self.f["prepare_review_row"](row)
        saved = copy.deepcopy(row)
        self.f["prepare_review_row"](row)
        self.assertEqual(row, saved)
        self.ui.session_state["teacher_overrides"][row["review_key"]] = {}
        self.f["prepare_review_row"](row)
        self.assertEqual(row["totals"]["earned"], 1)
        self.assertNotIn("teacher_override", row["result"]["questions"][0])

    def test_override_callback_invalidates_only_this_review_approval(self):
        owner = self.f["result_owner"]()
        self.ui.session_state["classroom_grade_approvals"] = {"classroom_a": True, "classroom_b": True}
        self.ui.session_state["classroom_approval_checkbox_classroom_a"] = True
        # The S2.1 ownership guard: register classroom_a as an owned row.
        self.ui.session_state["last_batch_results"] = [
            {"review_key": "classroom_a", "report_owner": owner}]
        self.f["save_teacher_override"]("classroom_a", "1", 2, "checked")
        self.assertNotIn("classroom_a", self.ui.session_state["classroom_grade_approvals"])
        self.assertTrue(self.ui.session_state["classroom_grade_approvals"]["classroom_b"])
        self.assertFalse(self.ui.session_state["classroom_approval_checkbox_classroom_a"])

    def test_actual_manual_summary_metrics_and_all_exports_agree_first_rerun(self):
        row = self.f["new_review_row"](self.row(), "manual")
        self.ui.session_state["last_batch_results"] = [row]
        self.f["save_teacher_override"](row["review_key"], "1", 2, "Verified method")
        self.f["pd"] = SimpleNamespace(DataFrame=Frame)
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        block = next(node for node in tree.body if isinstance(node, ast.If)
                     and ast.unparse(node.test) == "'last_batch_results' in st.session_state")
        exec(compile(ast.Module(body=[block], type_ignores=[]), str(SOURCE), "exec"), self.f)
        self.assertEqual(self.ui.tables[0][0]["Score"], "2 / 2")
        self.assertEqual(self.ui.metrics[0][1], "2 / 2")
        markdown = [kw["data"] for _, kw in self.ui.downloads if kw["mime"] == "text/markdown"]
        self.assertEqual(len(markdown), 2)
        for report in markdown:
            self.assertIn("Score: 2 / 2", report)
            self.assertIn("Teacher override: 1 -> 2", report)
        csv_text = next(kw["data"] for _, kw in self.ui.downloads if kw["mime"] == "text/csv")
        record = list(csv.DictReader(io.StringIO(csv_text)))[0]
        self.assertEqual(record["Awarded"], "2")
        self.assertEqual(record["Original AI Awarded"], "1")
        self.assertEqual(record["Teacher Override Reason"], "Verified method")

    def test_unused_grade_boundary_uploader_is_not_offered(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        upload_labels = [node.args[0].value for node in ast.walk(tree)
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                         and node.func.attr == "file_uploader" and node.args
                         and isinstance(node.args[0], ast.Constant)]
        self.assertNotIn("Official grade-boundary PDF", upload_labels)
        self.assertIn("Student PDF(s)", upload_labels)
        self.assertIn("Mark scheme PDF", upload_labels)


class ClassroomStateTests(unittest.TestCase):
    setUp = SafetyRegressionTests.setUp
    row = SafetyRegressionTests.row
    def context(self, **changes):
        values = dict(account_id="teacher", course_id="course", coursework={"id": "work", "maxPoints": 100},
                      scheme_data=b"synthetic scheme", audit_enabled=True, random_count=None)
        values.update(changes)
        return self.f["classroom_context_key"](**values)

    def test_assignment_account_scheme_and_setting_changes_isolate_history(self):
        first = self.f["classroom_job_state"](self.context())
        first["last_result"] = {"processed": [{"submission_id": "private-synthetic"}]}
        for change in [dict(account_id="other"), dict(course_id="other"),
                       dict(coursework={"id": "other", "maxPoints": 100}),
                       dict(coursework={"id": "work", "maxPoints": 50}),
                       dict(scheme_data=b"new scheme"), dict(audit_enabled=False), dict(random_count=5)]:
            with self.subTest(change=change):
                other = self.f["classroom_job_state"](self.context(**change))
                self.assertIsNone(other["last_result"])
                self.assertEqual(other["processed"], set())
        self.assertIs(self.f["classroom_job_state"](self.context()), first)

    def test_actual_classroom_view_uses_current_context_and_reviewed_totals(self):
        row = self.f["new_review_row"](self.row(), "classroom")
        row.update(submission_id="s", student="Synthetic", suggested_classroom_grade=50,
                   classroom_max_points=100, selected_questions=None, audit_warning=None)
        state = self.f["classroom_job_state"](self.context())
        state["last_result"] = {"processed": [row]}
        self.f["save_teacher_override"](row["review_key"], "1", 2, "Checked")
        self.f["pd"] = SimpleNamespace(DataFrame=Frame)
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        nodes = None
        for parent in ast.walk(tree):
            body = getattr(parent, "body", None)
            if not isinstance(body, list):
                continue
            for index, node in enumerate(body):
                if (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                    and target.id == "last_result" for target in node.targets)
                    and "job_state" in ast.unparse(node.value)):
                    nodes = body[index:index + 2]
                    break
        self.assertIsNotNone(nodes)
        code = compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec")
        self.f["job_state"] = state
        exec(code, self.f)
        self.assertEqual(self.ui.tables[0][0]["Score"], "2/2")
        self.assertEqual(self.ui.tables[0][0]["Current suggested Classroom grade"], "100.00")
        self.assertEqual(self.ui.metrics[0][1], "2 / 2")
        # A different assignment must not render the previous student's table.
        self.ui.tables.clear()
        self.ui.metrics.clear()
        self.f["job_state"] = self.f["classroom_job_state"](self.context(course_id="other"))
        exec(code, self.f)
        self.assertEqual(self.ui.tables, [])
        self.assertEqual(self.ui.metrics, [])

    def test_drive_content_edit_changes_revision_without_submission_timestamp_change(self):
        submission = {"id": "s", "updateTime": "same", "assignmentSubmission": {
            "attachments": [{"driveFile": {"id": "f"}}]}}
        self.f["requests"].request = Mock(side_effect=[
            response({"id": "f", "version": "1"}), response({"id": "f", "version": "2"})])
        self.assertNotEqual(self.f["classroom_submission_revision"]("dummy", submission),
                            self.f["classroom_submission_revision"]("dummy", submission))
        self.assertEqual(self.f["requests"].request.call_count, 2)

    def configure_job(self):
        self.f["list_turned_in_submissions"] = Mock(return_value=[{"id": "s", "state": "TURNED_IN"}])
        self.f["classroom_submission_revision"] = Mock(return_value="revision-1")
        self.f["upload_pdf"] = Mock(return_value=remote(name="scheme"))
        self.f["extract_scheme_maxima"] = Mock(return_value=({"1": 2}, {"1": "4"}, None))
        row = self.row()
        row.update(submission_id="s", student="Synthetic", score="1/2", percentage="50.00%",
                   suggested_classroom_grade=50, classroom_max_points=100)
        self.f["mark_one_classroom_submission"] = Mock(return_value=row)
        return fake_client()

    def run_job(self, client, processed, force=False):
        return self.f["run_classroom_marking_job"](
            "dummy", client, "course", {"id": "work"}, object(), False,
            processed, force_remark=force)

    def test_unchanged_skipped_changed_revision_and_explicit_remark_run(self):
        client = self.configure_job()
        processed = set()
        first = self.run_job(client, processed)
        self.assertEqual(processed, {"revision-1"})
        self.assertEqual(self.run_job(client, processed)["pending"], 0)
        self.f["classroom_submission_revision"].return_value = "revision-2"
        second = self.run_job(client, processed)
        forced = self.run_job(client, processed, force=True)
        self.assertEqual(self.f["mark_one_classroom_submission"].call_count, 3)
        keys = [r["processed"][0]["review_key"] for r in [first, second, forced]]
        self.assertEqual(len(set(keys)), 3)

    def test_failed_or_skipped_submission_remains_retryable(self):
        client = self.configure_job()
        for status in ["Failed", "Skipped"]:
            with self.subTest(status=status):
                self.f["mark_one_classroom_submission"].return_value = {
                    "submission_id": "s", "student": "Synthetic", "status": status,
                    "reason": "Synthetic failure", "result": None}
                processed = set()
                self.run_job(client, processed)
                self.assertEqual(processed, set())

    def test_revision_lookup_failure_does_not_silently_skip_work(self):
        client = self.configure_job()
        self.f["classroom_submission_revision"].side_effect = RuntimeError("lookup failed")
        result = self.run_job(client, {"revision-1"})
        self.assertEqual(result["pending"], 1)
        self.f["mark_one_classroom_submission"].assert_called_once()

    def test_refresh_preserves_older_rows_and_replaces_remarked_rows(self):
        previous = {"processed": [{"submission_id": "a", "score": 1}, {"submission_id": "b", "score": 2}]}
        current = {"pending": 1, "processed": [{"submission_id": "a", "score": 3}],
                   "active_submission_ids": ["a", "b"]}
        result = self.f["merge_classroom_results"](previous, current)
        self.assertEqual({r["submission_id"]: r["score"] for r in result["processed"]}, {"a": 3, "b": 2})
        empty = dict(current, pending=0, processed=[])
        self.assertEqual(self.f["merge_classroom_results"](result, empty)["processed"], result["processed"])


class UploadOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.f = load_app()

    def test_batch_failure_cleans_successes_even_after_first_failure(self):
        client = fake_client()
        failed = threading.Event()
        def upload(client, upload):
            if upload == "fail":
                failed.set()
                raise RuntimeError("synthetic upload failure")
            self.assertTrue(failed.wait(2))
            return remote(name=upload)
        self.f["upload_pdf"] = upload
        with self.assertRaisesRegex(RuntimeError, "synthetic upload failure"):
            self.f["upload_pdf_batch"](client, {"first": "fail", "second": "late-success"})
        client.files.delete.assert_called_once_with(name="late-success")

    def test_successful_batch_leaves_ownership_to_caller(self):
        client = fake_client()
        self.f["upload_pdf"] = lambda client, upload: remote(name=upload)
        uploaded = self.f["upload_pdf_batch"](client, {"a": "a", "b": "b"})
        self.assertEqual(set(uploaded), {"a", "b"})
        client.files.delete.assert_not_called()

    def test_timeout_poll_error_and_unknown_state_delete_remote(self):
        for failure in ["timeout", "poll", "unknown"]:
            with self.subTest(failure=failure):
                f = load_app()
                client = fake_client()
                client.files.upload.side_effect = None
                client.files.upload.return_value = remote("" if failure == "unknown" else "PROCESSING", "pending")
                f["time"] = Clock()
                client.files.get.side_effect = (lambda **kw: remote("PROCESSING", "pending")) if failure == "timeout" else RuntimeError("poll error")
                with self.assertRaises((RuntimeError, TimeoutError)):
                    f["upload_bytes_to_gemini"](client, b"synthetic", "synthetic.pdf")
                client.files.delete.assert_called_once_with(name="pending")

    def test_cleanup_error_preserves_original_processing_error(self):
        client = fake_client()
        client.files.upload.side_effect = None
        client.files.upload.return_value = remote("FAILED")
        client.files.delete.side_effect = RuntimeError("delete failed")
        with self.assertRaisesRegex(RuntimeError, "could not finish processing"):
            self.f["upload_bytes_to_gemini"](client, b"synthetic", "synthetic.pdf")
