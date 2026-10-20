"""Post-S6 cleanup regression tests (findings S6-R1, S6-R2, S6-R4, S6-R5).

S6-R1 — Classroom error privacy: raw provider exception text (HTTP response
bodies, student filenames, Drive IDs, credential echoes) must never reach a
result row's ``reason``, the UI or an export. Only the exception class and a
stable allowlisted category are stored, matching the S2.1 marking contract.

S6-R2 — PDF parser resource safety: deep PDF metadata inspection must run
inside a disposable child process whose whole execution is bounded by a hard
wall-clock deadline that the parent enforces by terminating the parser.

S6-R4 — Classroom download deadline: Drive attachment transfers must carry an
aggregate wall-clock budget checked with a monotonic clock around the request
and before every buffered chunk, always closing the HTTP response.

S6-R5 — Action version comments: the three pinned SHA references keep the
comment labels verified here against the GitHub refs API at review time;
updating a pin forces rerunning that verification and updating this table.

Everything is deterministic and offline: no network, no live provider calls,
no real student data.
"""
import re
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from support import load_app, response, fake_client, remote, model_question, benchmark
from test_pdf_processing import synthetic_pdf
from test_document_limits import encrypted_pdf

BASE = Path(__file__).resolve().parents[1]

# Synthetic, clearly-fake sensitive values used to prove sanitization.
STUDENT_NAME = "Synthetic Pupil (very private name)"
DRIVE_ID = "1aSyntheticDriveFileId_9z7XkQq"
FAKE_TOKEN = "Bearer ya29.SYNTHETIC-credential-not-real"
FAKE_KEY = "AIzaSySYNTHETIC-FAKE-key-000000"
FAKE_BODY = "<html>Provider response body echoing " + FAKE_KEY + "</html>"


class ClassroomErrorPrivacy(unittest.TestCase):
    """S6-R1: one content-free reason contract for the Classroom worker."""

    def setUp(self):
        self.f = load_app()

    def privacy(self, reason):
        """Every sanitized field keeps the S2.1 wording contract."""
        self.assertIn("no details are shown to protect student data", reason)
        return reason

    def test_classroom_request_never_embeds_response_bodies(self):
        for status in (401, 403, 429, 500):
            with self.subTest(status=status):
                self.f["requests"].request = Mock(return_value=response(
                    status=status,
                    text=FAKE_BODY + " student scan " + STUDENT_NAME))
                with self.assertRaises(RuntimeError) as caught:
                    self.f["classroom_request"](
                        "GET", "https://classroom.example.invalid", "dummy")
                message = str(caught.exception)
                self.assertEqual(message, f"Google API error {status}.")
                self.assertNotIn(STUDENT_NAME, message)
                self.assertNotIn(FAKE_KEY, message)
                self.assertIn(str(status), message)

    def test_failure_reasons_hide_students_ids_and_credentials(self):
        exceptions = [
            RuntimeError(f"Google API error 403 downloading Drive file "
                         f"{DRIVE_ID} for {STUDENT_NAME}: {FAKE_TOKEN}"),
            TimeoutError(f"socket operation on {DRIVE_ID} for {STUDENT_NAME} "
                         f"timed out after 120s"),
            RuntimeError(f"{FAKE_KEY} exhausted for student {STUDENT_NAME}"),
            Exception(f"unexpected crash {STUDENT_NAME} {FAKE_TOKEN}"),
        ]
        for exc in exceptions:
            with self.subTest(type=type(exc).__name__):
                reason = self.f["classroom_failure_reason"](exc)
                self.privacy(reason)
                self.assertNotIn(STUDENT_NAME, reason)
                self.assertNotIn(DRIVE_ID, reason)
                self.assertNotIn(FAKE_TOKEN, reason)
                self.assertNotIn(FAKE_KEY, reason)
                self.assertIn(type(exc).__name__, reason)

    def test_http_status_is_preserved_as_a_safe_category(self):
        reason = self.f["classroom_failure_reason"](
            RuntimeError(f"Google API error 403 from {STUDENT_NAME}"))
        self.privacy(reason)
        self.assertIn("403", reason)
        self.assertIn("refused access", reason)
        self.assertNotIn(STUDENT_NAME, reason)

    def test_gemini_quota_error_keeps_its_useful_category(self):
        reason = self.f["classroom_failure_reason"](
            RuntimeError("429 RESOURCE_EXHAUSTED for " + STUDENT_NAME))
        self.privacy(reason)
        self.assertIn("rate or usage limit", reason)
        self.assertNotIn(STUDENT_NAME, reason)

    def test_download_and_document_categories_stay_content_free(self):
        import document_limits
        for code, marker in (
                (document_limits.UNSUPPORTED_TYPE, "document was rejected"),
                (document_limits.TOO_MANY_PAGES, "document was rejected"),
                (document_limits.ENCRYPTED_PDF, "document was rejected"),
                (document_limits.DOWNLOAD_TIMEOUT, "document was rejected"),
                (document_limits.PDF_PARSE_TIMEOUT, "document was rejected")):
            with self.subTest(code=code):
                reason = self.privacy(self.f["classroom_failure_reason"](
                    document_limits.DocumentValidationError(code)))
                self.assertIn(code, reason)

    def test_worker_result_failure_rows_keep_student_data_out_of_reasons(self):
        """Full worker path: the classification applied at the result row."""
        self.f["get_user_profile"] = Mock(return_value={})
        self.f["download_drive_file"] = Mock(side_effect=RuntimeError(
            f"Google API error 500 {FAKE_BODY} {DRIVE_ID} {STUDENT_NAME}"))
        client = fake_client()
        submission = {"id": "s", "userId": "u", "assignmentSubmission": {
            "attachments": [{"driveFile": {"id": DRIVE_ID}}]}}
        row = self.f["mark_one_classroom_submission"](
            "dummy", client, submission, {"maxPoints": 100},
            remote(name="scheme"), False)
        self.assertEqual(row["status"], "Failed")
        reason = self.privacy(row["reason"])
        self.assertNotIn(STUDENT_NAME, reason)
        self.assertNotIn(DRIVE_ID, reason)
        self.assertNotIn(FAKE_KEY, reason)
        client.models.generate_content.assert_not_called()

    def test_classroom_audit_warning_stays_allowlisted(self):
        question = model_question(benchmark()["questions"][0], "partial")
        client = fake_client([{"questions": [question]}])
        self.f["audit_result"] = Mock(side_effect=RuntimeError(
            f"audit provider rejected key {FAKE_KEY} for {STUDENT_NAME}"))
        self.f["requests"].request = Mock(side_effect=[
            response({"name": {"fullName": STUDENT_NAME}}),
            response({"name": "work.pdf", "mimeType": "application/pdf"})])
        self.f["requests"].get = Mock(return_value=response(content=synthetic_pdf()))
        client.files.upload.side_effect = None
        client.files.upload.return_value = remote(name="student")
        row = self.f["mark_one_classroom_submission"](
            "dummy", client,
            {"id": "s", "userId": "u", "assignmentSubmission":
                {"attachments": [{"driveFile": {"id": "f"}}]}},
            {"maxPoints": 100}, remote(name="scheme"), True,
            selected_questions=["1"], scheme_maxima={"1": 2},
            scheme_answers={"1": "4"})
        self.assertEqual(row["status"], "Marked")
        self.assertRegex(row["audit_warning"], r"^[A-Za-z_]+ \[[A-Z_]+\]$")
        self.assertNotIn(STUDENT_NAME, row["audit_warning"])
        self.assertNotIn(FAKE_KEY, row["audit_warning"])

    def test_manual_audit_warning_stays_allowlisted(self):
        question = model_question(benchmark()["questions"][0], "partial")
        client = fake_client([{"questions": [question]}])
        self.f["audit_result"] = Mock(side_effect=RuntimeError(
            f"leaked key {FAKE_KEY}; student {STUDENT_NAME}"))
        row = self.f["mark_one_submission"](
            client, STUDENT_NAME, remote(), remote(name="scheme"), None, True,
            selected_questions=["1"], scheme_maxima={"1": 2},
            scheme_answers={"1": "4"})
        self.assertEqual(row["status"], "Marked")
        self.assertRegex(row["audit_warning"], r"^[A-Za-z_]+ \[[A-Z_]+\]$")
        self.assertNotIn(STUDENT_NAME, row["audit_warning"])
        self.assertNotIn(FAKE_KEY, row["audit_warning"])


class FakeClock:
    """Deterministic monotonic clock injectable into the downloader."""

    def __init__(self, now=0.0):
        self.value = float(now)

    def monotonic(self):
        return self.value


class FakeStream:
    """Streaming download fake with controllable elapsed time."""

    def __init__(self, chunks, clock=None, step=0.0, status=200):
        self.chunks = list(chunks)
        self.clock = clock
        self.step = step
        self.status_code = status
        self.closed = False
        self.text = FAKE_BODY

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def iter_content(self, chunk_size=8192):
        for chunk in self.chunks:
            if self.clock is not None and self.step:
                self.clock.value += self.step
            yield chunk

    def close(self):
        self.closed = True


class DownloadDeadline(unittest.TestCase):
    """S6-R4: an aggregate monotonic wall-clock budget for Drive transfers."""

    def setUp(self):
        self.f = load_app()
        self.stream = None

    def stream_of(self, chunks, clock=None, step=0.0, status=200):
        stream = FakeStream(chunks, clock=clock, step=step, status=status)
        self.stream = stream
        self.f["requests"].get = Mock(return_value=stream)
        return stream

    def test_normal_transfer_scores_full_bytes_and_closes(self):
        clock = FakeClock()
        self.stream_of([b"hello ", b"world"], clock=clock, step=0.1)
        data = self.f["_download_bounded"]("t", "u", {}, 1024, duration=30, clock=clock)
        self.assertEqual(data, b"hello world")
        self.assertTrue(self.stream.closed)

    def test_missing_declared_size_still_finishes_partially_mocked_flow(self):
        document_limits = __import__("document_limits")
        old = document_limits.MAX_ATTACHMENT_DOWNLOAD_SECONDS
        try:
            document_limits.MAX_ATTACHMENT_DOWNLOAD_SECONDS = 60
            clock = FakeClock()
            # No declared size in metadata: bounded downloader still applies.
            self.f["requests"].request = Mock(return_value=response(
                {"id": "f", "name": "x.pdf", "mimeType": "application/pdf"}))
            self.stream_of([synthetic_pdf(1)], clock=clock, step=0.5)
            name, data, mime = self.f["download_drive_file"]("t", "f")
            self.assertEqual(mime, "application/pdf")
            get_kwargs = self.f["requests"].get.call_args.kwargs
            self.assertTrue(get_kwargs["stream"])
            self.assertEqual(get_kwargs["timeout"], 60)
        finally:
            document_limits.MAX_ATTACHMENT_DOWNLOAD_SECONDS = old

    def test_socket_read_timeout_is_capped_by_the_budget(self):
        clock = FakeClock()
        self.stream_of([b"x"], clock=clock, step=0.01)
        self.f["_download_bounded"]("t", "u", {}, 1024, duration=45, clock=clock)
        self.assertEqual(self.f["requests"].get.call_args.kwargs["timeout"], 45)
        clock = FakeClock()
        self.stream_of([b"x"], clock=clock, step=0.01)
        self.f["_download_bounded"]("t", "u", {}, 1024, duration=1000, clock=clock)
        self.assertEqual(self.f["requests"].get.call_args.kwargs["timeout"], 120)

    def test_oversized_transfer_fails_mid_stream_and_closes(self):
        clock = FakeClock()
        self.stream_of([b"x" * 256 * 1024] * 4, clock=clock, step=0.1)
        import document_limits
        with self.assertRaises(document_limits.DocumentValidationError) as ctx:
            self.f["_download_bounded"]("t", "u", {}, 1024, duration=30, clock=clock)
        self.assertEqual(ctx.exception.code, document_limits.DOCUMENT_TOO_LARGE)
        self.assertTrue(self.stream.closed)

    def test_deadline_expired_before_the_first_chunk(self):
        clock = FakeClock()
        stream = FakeStream([b"late-arriving"], clock=clock, step=0.0)
        self.f["requests"].get = Mock(side_effect=lambda *a, **kw: (
            setattr(clock, "value", 45.0), stream)[1])
        import document_limits
        with self.assertRaises(document_limits.DocumentValidationError) as ctx:
            self.f["_download_bounded"]("t", "u", {}, 1024, duration=30, clock=clock)
        self.assertEqual(ctx.exception.code, document_limits.DOWNLOAD_TIMEOUT)
        self.assertTrue(stream.closed)

    def test_deadline_expiring_between_chunks_discards_partials(self):
        clock = FakeClock()
        big = b"y" * 700
        self.stream_of([big, big, big], clock=clock, step=16.0)
        import document_limits
        with self.assertRaises(document_limits.DocumentValidationError) as ctx:
            self.f["_download_bounded"]("t", "u", {}, 1024, duration=30, clock=clock)
        self.assertEqual(ctx.exception.code, document_limits.DOWNLOAD_TIMEOUT)
        self.assertTrue(self.stream.closed)
        # The failure message stays content-free.
        self.assertNotIn("yyyyy", str(ctx.exception))

    def test_slow_transfer_is_caught_despite_small_total(self):
        """Few bytes but plenty of elapsed wall clock: deadline still trips."""
        clock = FakeClock()
        self.stream_of([b"a", b"b", b"c"], clock=clock, step=11.0)
        import document_limits
        with self.assertRaises(document_limits.DocumentValidationError) as ctx:
            self.f["_download_bounded"]("t", "u", {}, 1024, duration=30, clock=clock)
        self.assertEqual(ctx.exception.code, document_limits.DOWNLOAD_TIMEOUT)
        self.assertTrue(self.stream.closed)

    def test_mid_stream_failure_closes_response_and_propagates(self):
        clock = FakeClock()
        stream = FakeStream([b"first"], clock=clock, step=0.0)
        stream.closed = False
        stream.text = FAKE_BODY

        def failing_chunks(chunk_size=8192):
            yield b"first"
            raise RuntimeError(f"connection reset in mid-transfer {FAKE_KEY}")
        stream.iter_content = failing_chunks
        self.f["requests"].get = Mock(return_value=stream)
        with self.assertRaises(RuntimeError) as ctx:
            self.f["_download_bounded"]("t", "u", {}, 1024, duration=30, clock=clock)
        # The raw exception is intentionally propagated here; any consumer
        # (the marking worker) sanitizes it via classroom_failure_reason
        # before it can be stored or displayed.
        self.assertIn("reset", str(ctx.exception))
        self.assertEqual(
            "connection reset in mid-transfer " + FAKE_KEY, str(ctx.exception))
        reason = self.f["classroom_failure_reason"](ctx.exception)
        self.assertIn("timed out or was cut off", reason)
        self.assertNotIn(FAKE_KEY, reason)
        self.assertTrue(stream.closed)

    def test_http_failure_status_keeps_safe_message(self):
        clock = FakeClock()
        self.stream_of([], clock=clock, step=0.0, status=403)
        with self.assertRaises(RuntimeError) as ctx:
            self.f["_download_bounded"]("t", "u", {}, 1024, duration=30, clock=clock)
        self.assertIn("Could not download", str(ctx.exception))
        self.assertIn("403", str(ctx.exception))
        self.assertNotIn(FAKE_BODY, str(ctx.exception))


class ParserIsolation(unittest.TestCase):
    """S6-R2: deep PDF inspection is bounded by a real killable process."""

    @classmethod
    def setUpClass(cls):
        import document_limits
        cls.dl = document_limits
        cls.pdf = synthetic_pdf(4)
        cls.enc = encrypted_pdf()
        # Keep the default isolation flag; tests patch attributes instead of
        # environment variables so parallel suites never observe leaks.

    def test_isolated_and_in_process_paths_agree(self):
        dl = self.dl
        self.assertEqual(dl._isolated_pdf_limits(self.pdf)[1], 4)
        self.assertFalse(dl._isolated_pdf_limits(self.pdf)[0])
        self.assertEqual(dl._in_process_pdf_limits(self.pdf)[1], 4)
        corrupted = self.pdf[:-4] + b"\xff\x00 junk"
        for limits in (dl._isolated_pdf_limits, dl._in_process_pdf_limits):
            with self.subTest(path=limits.__name__):
                with self.assertRaises(dl.DocumentValidationError) as ctx:
                    limits(corrupted)
                self.assertEqual(ctx.exception.code, dl.INVALID_PDF)

    def test_encrypted_flag_survives_page_walk_failures(self):
        encrypted, pages = self.dl._isolated_pdf_limits(self.enc)
        self.assertTrue(encrypted)
        with self.assertRaisesRegex(self.dl.DocumentValidationError,
                                    "password-protected"):
            self.dl.pdf_page_count(self.enc)

    def test_page_limit_still_enforced_isolated(self):
        dl = self.dl
        old = dl.MAX_PDF_PAGES
        dl.MAX_PDF_PAGES = 2
        try:
            with self.assertRaisesRegex(dl.DocumentValidationError,
                                        "page count"):
                dl.pdf_page_count(self.pdf)
        finally:
            dl.MAX_PDF_PAGES = old

    def test_validate_document_still_reports_page_counts_isolated(self):
        self.assertEqual(
            self.dl.validate_document(self.pdf, "work.pdf"),
            {"kind": "pdf", "pages": 4})

    def test_stalled_parser_is_recycled_when_budget_expires(self):
        dl = self.dl
        original_command, original_timeout = dl._child_parse_command, dl.PDF_PARSE_TIMEOUT_SECONDS
        original_disabled = dl._parser_isolation_disabled
        dl._child_parse_command = lambda: [sys.executable, "-c", "import time; time.sleep(60)"]
        dl.PDF_PARSE_TIMEOUT_SECONDS = 2
        dl._parser_isolation_disabled = False
        permits = dl._parser_permits._initial_value \
            if hasattr(dl._parser_permits, "_initial_value") else dl.PDF_PARSE_MAX_CONCURRENT
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(
                    dl.DocumentValidationError, "allowed time") as ctx:
                dl.pdf_page_count(self.pdf)
            self.assertEqual(ctx.exception.code, dl.PDF_PARSE_TIMEOUT)
            elapsed = time.monotonic() - started
            # Determinism bound: the kill must fire near the deadline, and a
            # generous ceiling keeps the assertion load-independent.
            self.assertLess(elapsed, 30)
            # The permit pool survives the kill: another parse can run.
            dl._child_parse_command = original_command
            dl.PDF_PARSE_TIMEOUT_SECONDS = original_timeout
            self.assertEqual(dl.pdf_page_count(self.pdf), 4)
        finally:
            dl._child_parse_command = original_command
            dl.PDF_PARSE_TIMEOUT_SECONDS = original_timeout
            dl._parser_isolation_disabled = original_disabled

    def test_run_level_timeout_maps_content_free(self):
        dl = self.dl
        original_run = dl.subprocess.run
        original_disabled = dl._parser_isolation_disabled
        dl._parser_isolation_disabled = False
        try:
            def timing_out(*args, **kwargs):
                raise subprocess.TimeoutExpired(cmd=["python"], timeout=2)
            dl.subprocess.run = timing_out
            with self.assertRaisesRegex(dl.DocumentValidationError,
                                        "allowed time"):
                dl._isolated_pdf_limits(self.pdf)
            def refusing(*args, **kwargs):
                raise OSError("spawn refused by the sandbox")
            dl.subprocess.run = refusing
            with self.assertRaisesRegex(dl.DocumentValidationError, "malformed"):
                dl._isolated_pdf_limits(self.pdf)
        finally:
            dl.subprocess.run = original_run
            dl._parser_isolation_disabled = original_disabled

    def test_concurrent_parses_respect_the_global_permit_bound(self):
        dl = self.dl
        original_run = dl.subprocess.run
        original_permits = dl._parser_permits
        stats = {"current": 0, "max": 0}
        lock = threading.Lock()

        def bounded_run(*args, **kwargs):
            with lock:
                stats["current"] += 1
                stats["max"] = max(stats["max"], stats["current"])
            time.sleep(0.05)
            with lock:
                stats["current"] -= 1
            return SimpleNamespace(
                returncode=0,
                stdout=b'{"ok": true, "encrypted": false, "pages": 3}\n',
                stderr=b"")
        try:
            dl.subprocess.run = bounded_run
            dl._parser_permits = threading.BoundedSemaphore(1)
            threads = [
                threading.Thread(target=dl._isolated_pdf_limits, args=(self.pdf,))
                for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
                self.assertFalse(thread.is_alive())
            self.assertEqual(stats["max"], 1,
                             "the permit bound must serialize concurrent parses")
            self.assertEqual(stats["current"], 0)
        finally:
            dl.subprocess.run = original_run
            dl._parser_permits = original_permits


VERIFIED_ACTIONS = {
    # Verified 2026-10-20 against the GitHub refs API
    # (refs/tags/<tag> -> commit object). Update in lockstep with any pin.
    "actions/checkout": ("34e114876b0b11c390a56381ad16ebd13914f8d5", "v4.3.1"),
    "actions/setup-python": ("a26af69be951a213d495a4c3e4e4022e16d87065", "v5.6.0"),
    "actions/github-script": ("f28e40c7f34bde8b3046d885e986cb6290c5673b", "v7.1.0"),
}


class WorkflowVersionPins(unittest.TestCase):
    """S6-R5: version comments must match the pinned commits exactly."""

    def test_every_action_pin_matches_its_verified_comment(self):
        lines = (BASE / ".github" / "workflows" / "marking-tests.yml").read_text(
            encoding="utf-8").splitlines()
        seen = set()
        for line in lines:
            if "uses:" not in line:
                continue
            _, _, reference = line.partition("uses:")
            match = re.match(
                r"\s*(?P<repo>[A-Za-z0-9_.\-/]+)@(?P<sha>[0-9a-f]{40})"
                r"\s*#\s*(?P<label>v[0-9.]+)\s*$", reference)
            self.assertIsNotNone(
                match, f"Pin/comment format drifted: {line!r}")
            repo, sha, label = (match.group("repo"), match.group("sha"),
                                match.group("label"))
            with self.subTest(action=repo):
                self.assertIn(repo, VERIFIED_ACTIONS,
                              f"Unreviewed action introduced: {repo}")
                verified_sha, verified_label = VERIFIED_ACTIONS[repo]
                self.assertEqual(sha, verified_sha,
                                 f"{repo} pin no longer matches the verified commit; "
                                 "re-verify against the GitHub refs API")
                self.assertEqual(label, verified_label,
                                 f"{repo} version comment does not match the "
                                 f"verified tag {verified_label}")
            seen.add(repo)
        self.assertEqual(seen, set(VERIFIED_ACTIONS))


if __name__ == "__main__":
    unittest.main()
