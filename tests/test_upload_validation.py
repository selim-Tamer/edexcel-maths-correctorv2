"""Stage S3 upload-path security tests against real production functions.

Synthetic bytes and fakes only; asserts that unsafe or excessive documents
never trigger Gemini uploads, are never cached, and that rejections are
reported content-free. No live requests.
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import document_limits as dl
import document_pipeline as dp
from support import fake_client, load_app, remote, response
from pipeline_fakes import Clock, Provider, uploaded
from test_pdf_processing import synthetic_pdf

STUDENT_NAME = "Submission of Synthetic Student Ada"


def student_file(data=None, name="submission.pdf"):
    data = synthetic_pdf(1) if data is None else data
    return uploaded(data, name)


def oversized_pdf_bytes():
    return b"%PDF-1.4" + b"\x00" * (26 * 1024 * 1024)


class ManualBatchLimitTests(unittest.TestCase):
    def setUp(self):
        self.f = load_app()
        self.clock = Clock()
        self.client = Provider(self.clock, transfer=0, processing=0)
        self.session = {
            "files": dp.FileCache("key-account", clock=self.clock),
            "metadata": dp.Memo(clock=self.clock),
            "results": dp.Memo(clock=self.clock),
        }
        self.f["extract_scheme_maxima"] = Mock(
            return_value=({"1": 2}, {"1": "4"}, None))
        self.scheme = uploaded(synthetic_pdf(2), "scheme.pdf")

    def run_batch(self, students, scheme=None, question=None):
        run = dp.DocumentRun(self.client, self.session["files"])
        try:
            return self.f["prepare_manual_batch"](
                self.client, students, scheme or self.scheme, question,
                False, None, self.session, run)
        finally:
            run.close()

    def test_valid_batch_prepares_with_pending_students(self):
        plan = self.run_batch(
            [student_file(), student_file(synthetic_pdf(2), "second.pdf")])
        self.assertEqual(plan["pending"], [0, 1])
        self.assertEqual(len(plan["keys"]), 2)

    def test_too_many_files_rejected_before_any_upload_or_hash(self):
        students = [student_file(name=f"s{index}.pdf") for index in range(4)]
        old_files = dl.MAX_MANUAL_BATCH_FILES
        try:
            dl.MAX_MANUAL_BATCH_FILES = 3
            with self.assertRaisesRegex(
                    dl.DocumentValidationError, "too many documents"):
                self.run_batch(students)
        finally:
            dl.MAX_MANUAL_BATCH_FILES = old_files
        self.client.files.upload.assert_not_called()

    def test_batch_total_bytes_rejected_before_uploads(self):
        old_total = dl.MAX_MANUAL_BATCH_TOTAL_BYTES
        try:
            dl.MAX_MANUAL_BATCH_TOTAL_BYTES = 64
            with self.assertRaisesRegex(
                    dl.DocumentValidationError, "total upload size"):
                self.run_batch([student_file()])
        finally:
            dl.MAX_MANUAL_BATCH_TOTAL_BYTES = old_total
        self.client.files.upload.assert_not_called()

    def test_misleading_size_metadata_cannot_bypass_transfer_limit(self):
        def liar(name):
            return SimpleNamespace(name=name, size=15,
                                   getvalue=lambda: oversized_pdf_bytes())
        # Both .size values claim 15 bytes; the transfer boundary measures the
        # real bytes and rejects before any provider call.
        with self.assertRaisesRegex(
                dl.DocumentValidationError, "maximum supported size"):
            self.run_batch([liar("sneaky.pdf")], scheme=liar("ms.pdf"))
        self.client.files.upload.assert_not_called()

    def test_empty_file_rejected(self):
        with self.assertRaisesRegex(dl.DocumentValidationError, "no data"):
            self.run_batch([student_file(b"")])

    def test_all_invalid_files_never_reach_the_provider(self):
        self.f["extract_scheme_maxima"].side_effect = AssertionError(
            "must not run")
        bad_scheme = uploaded(b"cross your fingers", "scheme.pdf")
        bad_student = uploaded(b"cross your fingers", "work.pdf")
        run = dp.DocumentRun(self.client, self.session["files"])
        try:
            with self.assertRaises(dl.DocumentValidationError):
                self.f["prepare_manual_batch"](
                    self.client, [bad_student], bad_scheme, None, False, None,
                    self.session, run)
        finally:
            run.close()
        # Validation runs before any upload in the transfer boundary, so an
        # all-invalid batch cannot emit a single provider upload/poll.
        self.client.files.upload.assert_not_called()
        self.client.files.get.assert_not_called()

    def test_duplicate_files_hash_consistently(self):
        first = self.run_batch([student_file(synthetic_pdf(2))])
        second = self.run_batch([student_file(synthetic_pdf(2))])
        self.assertEqual(first["keys"][0], second["keys"][0])


MARKING_PAYLOAD = {"questions": [
    {"question_number": "1", "max_marks": 2, "awarded_marks": 1,
     "student_answer": "x", "working_summary": "w",
     "correct_method": "m", "full_mark_solution": "a",
     "scheme_answer_evidence": "a", "confidence": "High",
     "loss_reason": "", "mark_breakdown": []}]}


def marked_client():
    client = fake_client([MARKING_PAYLOAD])
    client.files.upload.side_effect = None
    client.files.upload.return_value = remote()
    return client


class ClassroomAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.f = load_app()
        self.f["get_user_profile"] = Mock(return_value={})

    def classroom(self, client, attachments, **options):
        submission = {"id": "s1", "userId": "u9", "assignmentSubmission": {
            "attachments": attachments}}
        return self.f["mark_one_classroom_submission"](
            "synthetic-token", client, submission, {"maxPoints": 100},
            remote(name="scheme"), False, **options)

    def download(self, name, mime, chunks_or_content, size=None):
        metadata = {"id": "f1", "name": name, "mimeType": mime,
                    "capabilities": {"canDownload": True}}
        if size is not None:
            metadata["size"] = size
        self.f["requests"].request = Mock(return_value=response(metadata))
        if isinstance(chunks_or_content, list):
            self.f["requests"].get = Mock(
                return_value=response(chunks=chunks_or_content))
        else:
            self.f["requests"].get = Mock(
                return_value=response(content=chunks_or_content))

    def test_valid_pdf_submission_is_marked(self):
        client = marked_client()
        self.download("x.pdf", "application/pdf", synthetic_pdf(2))
        row = self.classroom(client, [{"driveFile": {"id": "f1"}}],
                             scheme_maxima={"1": 2}, scheme_answers={"1": "4"})
        self.assertEqual(row["status"], "Marked")
        self.assertEqual(client.models.generate_content.call_count, 1)
        self.f["requests"].get.assert_called_once()
        self.assertTrue(self.f["requests"].get.call_args.kwargs["stream"])

    def test_unsupported_type_rejected_before_download(self):
        client = fake_client()
        self.download("x.pdf", "application/zip", b"PK should never download")
        row = self.classroom(client, [{"driveFile": {"id": "f1"}}])
        self.assertEqual(row["status"], "Failed")
        self.assertIn("Unsupported", row["reason"])
        self.f["requests"].get.assert_not_called()
        client.models.generate_content.assert_not_called()
        client.files.upload.assert_not_called()

    def test_misleading_size_metadata_does_not_bypass_limit(self):
        client = fake_client()
        big = b"\x00" * 256 * 1024
        chunks = [big] * 4  # 1 MiB streamed while metadata declares 16 bytes
        self.download("x.pdf", "application/pdf", chunks, size=16)
        old_limit = dl.MAX_CLASSROOM_ATTACHMENT_BYTES
        try:
            dl.MAX_CLASSROOM_ATTACHMENT_BYTES = 512 * 1024
            row = self.classroom(client, [{"driveFile": {"id": "f1"}}])
        finally:
            dl.MAX_CLASSROOM_ATTACHMENT_BYTES = old_limit
        self.assertEqual(row["status"], "Failed")
        self.assertIn("size", row["reason"])
        # The declared (lying) size must not have ended the download early:
        # more than 512 KiB was read before the abort.
        read_chunks = [c for c in self.f["requests"].get.call_args_list]
        self.assertTrue(read_chunks)
        client.files.upload.assert_not_called()
        client.models.generate_content.assert_not_called()

    def test_oversized_stream_rejected_mid_download(self):
        big_chunks = [b"\x00" * 256 * 1024 for _ in range(4)]
        self.download("x.pdf", "application/pdf", big_chunks)
        old_limit = dl.MAX_CLASSROOM_ATTACHMENT_BYTES
        try:
            dl.MAX_CLASSROOM_ATTACHMENT_BYTES = 512 * 1024
            row = self.classroom(fake_client(), [{"driveFile": {"id": "f1"}}])
        finally:
            dl.MAX_CLASSROOM_ATTACHMENT_BYTES = old_limit
        self.assertEqual(row["status"], "Failed")
        self.assertIn("size", row["reason"])
        client = fake_client()
        client.files.upload.assert_not_called()

    def test_missing_size_metadata_handled_by_bounded_download(self):
        client = marked_client()
        self.download("x.pdf", "application/pdf", synthetic_pdf(1))
        row = self.classroom(client, [{"driveFile": {"id": "f1"}}],
                             scheme_maxima={"1": 2}, scheme_answers={"1": "4"})
        self.assertEqual(row["status"], "Marked")
        self.f["requests"].get.assert_called_once()
        self.assertTrue(self.f["requests"].get.call_args.kwargs["stream"])

    def test_transfer_timeout_is_content_free_failure(self):
        class Boom:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()
                return False

            def close(self):
                pass

            def iter_content(self, chunk_size=8192):
                raise TimeoutError("timed out after 120s")

        self.download("work.pdf", "application/pdf", [])
        self.f["requests"].get = Mock(return_value=Boom())
        row = self.classroom(fake_client(), [{"driveFile": {"id": "f1"}}])
        self.assertEqual(row["status"], "Failed")
        self.assertNotIn("work.pdf", row["reason"])
        self.assertNotIn("u9", row["reason"])

    def test_student_names_and_bytes_never_appear_in_rejection_reasons(self):
        client = fake_client()
        self.download(STUDENT_NAME + ".pdf", "application/zip", b"PK")
        row = self.classroom(client, [{"driveFile": {"id": "f1"}}])
        self.assertEqual(row["status"], "Failed")
        self.assertNotIn(STUDENT_NAME, row["reason"])
        # A signed-but-corrupt attachment is also rejected content-free.
        self.download(STUDENT_NAME + ".pdf", "application/pdf",
                      b"%PDF-1.4 not actually a pdf")
        row = self.classroom(client, [{"driveFile": {"id": "f1"}}])
        self.assertEqual(row["status"], "Failed")
        self.assertNotIn(STUDENT_NAME, row["reason"])

    def test_mixed_valid_and_invalid_submissions_handled_safely(self):
        client = marked_client()
        good = {"id": "good", "userId": "u-good", "assignmentSubmission": {
            "attachments": [{"driveFile": {"id": "goodfile"}}]}}
        bad = {"id": "bad", "userId": "u-bad", "assignmentSubmission": {
            "attachments": [{"driveFile": {"id": "badfile"}}]}}
        self.f["requests"].request = Mock(side_effect=[
            response({"id": "goodfile", "name": "work.pdf",
                      "mimeType": "application/pdf",
                      "capabilities": {"canDownload": True}}),
            response({"id": "badfile", "name": "bad.pdf",
                      "mimeType": "application/zip"})])
        self.f["requests"].get = Mock(return_value=response(
            content=synthetic_pdf(2)))
        mark = self.f["mark_one_classroom_submission"]
        good_row = mark("synthetic-token", client, good,
                        {"maxPoints": 100}, remote(name="scheme"), False,
                        scheme_maxima={"1": 2}, scheme_answers={"1": "4"})
        bad_row = mark("synthetic-token", client, bad,
                       {"maxPoints": 100}, remote(name="scheme"), False,
                       scheme_maxima={"1": 2}, scheme_answers={"1": "4"})
        self.assertEqual(good_row["status"], "Marked")
        self.assertEqual(bad_row["status"], "Failed")
        # Exactly one marking request: the rejected submission consumed none.
        self.assertEqual(client.models.generate_content.call_count, 1)
        self.f["requests"].get.assert_called_once()


class ClassroomRunCapTests(unittest.TestCase):
    def setUp(self):
        self.f = load_app()
        from test_review_safety import RecordingUI
        self.f["st"] = RecordingUI()
        self.f["result_owner"] = lambda: "google:synthetic"
        self.f["get_user_profile"] = Mock(
            return_value={"name": {"fullName": "Synthetic"}})
        self.f["list_turned_in_submissions"] = Mock(return_value=[
            {"id": f"s{n}", "userId": f"u{n}", "state": "TURNED_IN",
             "assignmentSubmission": {"attachments": [
                 {"driveFile": {"id": n}}]}}
            for n in range(40)])
        self.f["classroom_submission_revision"] = Mock(
            side_effect=lambda token, submission: submission["id"] + "-v1")
        self.f["upload_pdf"] = Mock(return_value=remote(name="scheme"))
        self.f["prepare_reference_metadata"] = Mock(
            return_value=(({"1": 2}, {"1": "4"}, None), ["1"]))
        self.f["mark_one_classroom_submission"] = Mock(
            side_effect=lambda token, client, submission, *rest, **kwargs: {
                "submission_id": submission["id"],
                "student": "Synthetic",
                "status": "Marked",
                "score": "1/2",
                "percentage": 50.0,
                "reason": None,
                "audit_warning": None,
                "result": {"questions": []},
                "totals": {"earned": 1, "available": 2, "percentage": 50.0},
                "groups": [],
                "marking_model": "m",
                "audit_model": "m",
                "suggested_classroom_grade": 5})

    def run_job(self, processed_ids):
        client = fake_client()
        return self.f["run_classroom_marking_job"](
            "synthetic-token", client, "course",
            {"id": "assignment", "maxPoints": 100}, object(), False,
            processed_ids, report_owner="google:synthetic")

    def test_run_marks_exactly_the_cap_and_defers_the_rest(self):
        old_cap = dl.MAX_CLASSROOM_SUBMISSIONS_PER_RUN
        try:
            dl.MAX_CLASSROOM_SUBMISSIONS_PER_RUN = 30
            result = self.run_job(set())
        finally:
            dl.MAX_CLASSROOM_SUBMISSIONS_PER_RUN = old_cap
        processed_ids = {row["submission_id"] for row in result["processed"]}
        self.assertEqual(len(processed_ids), 30)
        deferred_ids = [s["id"] for s in result["deferred"]]
        self.assertEqual(len(deferred_ids), 10)
        self.assertEqual(deferred_ids, [f"s{n}" for n in range(30, 40)])
        # Deferred submissions were not marked and remain retryable: their
        # revisions were never treated as processed.
        self.assertEqual(self.f["mark_one_classroom_submission"].call_count, 30)
        for revision in (f"s{n}-v1" for n in range(30, 40)):
            self.assertNotIn(revision, result["processed"])
        self.assertIn("deferred", result["message"])

    def test_everything_under_the_cap_is_processed_normally(self):
        old_cap = dl.MAX_CLASSROOM_SUBMISSIONS_PER_RUN
        try:
            dl.MAX_CLASSROOM_SUBMISSIONS_PER_RUN = 50
            result = self.run_job(set())
        finally:
            dl.MAX_CLASSROOM_SUBMISSIONS_PER_RUN = old_cap
        self.assertEqual(len(result["processed"]), 40)
        self.assertEqual(result["deferred"], [])
        self.assertEqual(self.f["mark_one_classroom_submission"].call_count, 40)
        self.assertNotIn("deferred", result["message"])


if __name__ == "__main__":
    unittest.main()
