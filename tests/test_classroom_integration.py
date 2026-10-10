"""Deterministic Google Classroom/Drive HTTP contracts; never writes grades."""
import unittest
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock
from support import benchmark, fake_client, load_app, model_question, remote, response
from test_pdf_processing import synthetic_pdf


class ClassroomTests(unittest.TestCase):
    def setUp(self):
        self.f = load_app()

    def test_all_listing_helpers_follow_pagination(self):
        for name, args, field in [
            ("list_teacher_courses", ("dummy",), "courses"),
            ("list_assignments", ("dummy", "course"), "courseWork"),
            ("list_turned_in_submissions", ("dummy", "course", "work"), "studentSubmissions"),
        ]:
            with self.subTest(helper=name):
                request = Mock(side_effect=[
                    response({field: [{"id": "1", "workType": "ASSIGNMENT"}],
                              "nextPageToken": "page-two"}),
                    response({field: [{"id": "2", "workType": "ASSIGNMENT"}]})])
                self.f["requests"].request = request
                self.assertEqual([x["id"] for x in self.f[name](*args)], ["1", "2"])
                self.assertEqual(request.call_count, 2)
                self.assertEqual(request.call_args.kwargs["params"]["pageToken"], "page-two")
                self.assertTrue(all(c.kwargs["method"] == "GET" for c in request.call_args_list))

    def test_repeated_page_token_fails_instead_of_looping(self):
        self.f["requests"].request = Mock(return_value=response(
            {"courses": [], "nextPageToken": "repeat"}))
        with self.assertRaisesRegex(RuntimeError, "pagination token"):
            self.f["list_teacher_courses"]("dummy")
        self.assertEqual(self.f["requests"].request.call_count, 2)

    def test_later_page_permission_error_does_not_return_partial_listing(self):
        self.f["requests"].request = Mock(side_effect=[
            response({"courses": [{"id": "1"}], "nextPageToken": "two"}),
            response(status=403)])
        with self.assertRaisesRegex(RuntimeError, "403"):
            self.f["list_teacher_courses"]("dummy")
        self.assertEqual(self.f["requests"].request.call_count, 2)

    def test_assignment_filter_and_submission_states(self):
        request = Mock(return_value=response({"courseWork": [
            {"id": "1", "workType": "ASSIGNMENT"}, {"id": "2", "workType": "SHORT_ANSWER_QUESTION"}]}))
        self.f["requests"].request = request
        self.assertEqual([x["id"] for x in self.f["list_assignments"]("dummy", "c")], ["1"])
        request.return_value = response({"studentSubmissions": []})
        self.assertEqual(self.f["list_turned_in_submissions"]("dummy", "c", "w"), [])
        self.assertEqual(request.call_args.kwargs["params"]["states"], "TURNED_IN")

    def test_legacy_states_translation_does_not_mutate_caller(self):
        self.f["requests"].request = Mock(return_value=response())
        params = {"studentSubmissionStates": "TURNED_IN"}
        self.f["classroom_request"]("GET", "https://example.invalid", "dummy", params=params)
        kw = self.f["requests"].request.call_args.kwargs
        self.assertEqual(kw["params"], {"states": "TURNED_IN"})
        self.assertEqual(params, {"studentSubmissionStates": "TURNED_IN"})
        self.assertEqual(kw["timeout"], 60)
        self.assertEqual(kw["headers"]["Authorization"], "Bearer dummy")

    def test_http_errors_and_empty_response(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                self.f["requests"].request = Mock(return_value=response(status=status))
                with self.assertRaisesRegex(RuntimeError, str(status)):
                    self.f["classroom_request"]("GET", "https://example.invalid", "dummy")
        self.f["requests"].request = Mock(return_value=response(content=b""))
        self.assertEqual(self.f["classroom_request"]("GET", "https://example.invalid", "dummy"), {})

    def test_drive_pdf_download_and_google_doc_export(self):
        data = synthetic_pdf(2)
        for mime, endpoint, params in [
            ("application/pdf", "/files/f", {"alt": "media"}),
            ("application/vnd.google-apps.document", "/files/f/export", {"mimeType": "application/pdf"}),
        ]:
            with self.subTest(mime=mime):
                self.f["requests"].request = Mock(return_value=response(
                    {"name": "synthetic.pdf", "mimeType": mime}))
                self.f["requests"].get = Mock(return_value=response(content=data))
                name, downloaded, actual_mime = self.f["download_drive_file"]("dummy", "f")
                self.assertEqual((name, downloaded, actual_mime), ("synthetic.pdf", data, "application/pdf"))
                self.assertTrue(self.f["requests"].get.call_args.args[0].endswith(endpoint))
                self.assertEqual(self.f["requests"].get.call_args.kwargs["params"], params)

    def test_drive_denied_download_and_http_failure(self):
        self.f["requests"].request = Mock(return_value=response(
            {"name": "synthetic.pdf", "mimeType": "application/pdf",
             "capabilities": {"canDownload": False}}))
        with self.assertRaisesRegex(RuntimeError, "does not allow"):
            self.f["download_drive_file"]("dummy", "f")
        self.f["requests"].get.assert_not_called()
        self.f["requests"].request.return_value = response(
            {"name": "synthetic.pdf", "mimeType": "application/pdf"})
        self.f["requests"].get = Mock(return_value=response(status=403))
        with self.assertRaisesRegex(RuntimeError, "Could not download"):
            self.f["download_drive_file"]("dummy", "f")

    def classroom(self, client, attachments=None, **options):
        submission = {"id": "s", "userId": "u", "assignmentSubmission": {
            "attachments": attachments if attachments is not None else
            [{"driveFile": {"id": "f1"}}, {"driveFile": {"id": "f2"}}]}}
        return self.f["mark_one_classroom_submission"](
            "dummy", client, submission, {"maxPoints": 100}, remote(name="scheme"),
            False, **options)

    def test_full_classroom_path_two_attachments_matches_manual_totals_without_writes(self):
        q = model_question(benchmark()["questions"][0], "partial")
        client = fake_client([{"questions": [q]}, {"questions": [q]}])
        client.files.upload.side_effect = [remote(name="first"), remote(name="second")]
        self.f["requests"].request = Mock(side_effect=[
            response({"name": {"fullName": "Synthetic"}}),
            response({"name": "first.pdf", "mimeType": "application/pdf"}),
            response({"name": "second.pdf", "mimeType": "application/pdf"})])
        self.f["requests"].get = Mock(return_value=response(content=synthetic_pdf()))
        progress = Queue()
        options = dict(selected_questions=["1"], scheme_maxima={"1": 2},
                       scheme_answers={"1": "4"})
        row = self.classroom(client, progress_queue=progress, **options)
        manual = self.f["mark_one_submission"](
            client, "Synthetic", remote(), remote(name="scheme"), None, False, **options)
        self.assertEqual(row["status"], "Marked")
        self.assertEqual(row["totals"], manual["totals"])
        self.assertEqual(row["suggested_classroom_grade"], 50)
        self.assertEqual(client.models.generate_content.call_count, 2)
        contents = client.models.generate_content.call_args_list[0].kwargs["contents"]
        self.assertEqual([x.name for x in contents[:3]], ["first", "second", "scheme"])
        self.assertEqual(client.files.delete.call_count, 2)
        self.assertEqual({c.kwargs["name"] for c in client.files.delete.call_args_list},
                         {"first", "second"})
        self.assertEqual(self.f["requests"].request.call_count, 3)
        self.assertEqual(self.f["requests"].get.call_count, 2)
        self.assertTrue(all(c.kwargs["method"] == "GET"
                            for c in self.f["requests"].request.call_args_list))
        self.assertFalse(progress.empty())

    def test_missing_or_unsupported_attachment_never_generates(self):
        self.f["get_user_profile"] = Mock(return_value={})
        client = fake_client()
        row = self.classroom(client, attachments=[{"link": {"url": "https://example.invalid"}}])
        self.assertEqual(row["status"], "Skipped")
        self.f["download_drive_file"] = Mock(return_value=("bad.zip", b"bad", "application/zip"))
        row = self.classroom(client)
        self.assertEqual(row["status"], "Failed")
        self.assertIn("Unsupported", row["reason"])
        client.models.generate_content.assert_not_called()

    def test_second_attachment_failure_cleans_first_and_does_not_mark_partial_work(self):
        self.f["get_user_profile"] = Mock(side_effect=RuntimeError("profile unavailable"))
        self.f["download_drive_file"] = Mock(side_effect=[
            ("first.pdf", synthetic_pdf(), "application/pdf"), RuntimeError("download failed")])
        client = fake_client()
        client.files.upload.side_effect = None
        client.files.upload.return_value = remote(name="first")
        row = self.classroom(client)
        self.assertEqual(row["status"], "Failed")
        self.assertEqual(row["student"], "u")
        self.assertIsNone(row["suggested_classroom_grade"])
        client.files.delete.assert_called_once_with(name="first")
        client.models.generate_content.assert_not_called()
