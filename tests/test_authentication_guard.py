"""Fail-closed authentication guards for every quota-consuming entry point.

Offline only. Production functions are loaded from source; Streamlit, OAuth
callbacks and the Gemini transport are synthetic. No live Google/Gemini
service, key or account is touched. No secret value is read or printed: the
only key strings here are obviously synthetic and exist solely to assert they
never leak into user-facing authentication failures.

Covers finding F-01: a signed-in Google session (or nothing) decides every
billable marking boundary; widget state and UI availability never authorize.
"""
import ast
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from support import SOURCE, load_app


def app_source() -> str:
    return SOURCE.read_text(encoding="utf-8")


def module_nodes():
    return ast.parse(app_source(), filename=str(SOURCE)).body


def normalize(condition_source: str) -> str:
    """Version-stable comparison: Python minors unparenthesize differently."""
    return condition_source.replace("(", "").replace(")", "").replace(" ", "")


class StatusCtx:
    """Minimal st.status()/st.expander() context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def update(self, *args, **kwargs):
        pass


class RecordingErrorUI:
    """Minimal st stub that records user-visible errors only."""

    def __init__(self):
        self.session_state = {}
        self.errors = []
        self.user = SimpleNamespace()  # anonymous: no is_logged_in attribute

    def error(self, message):
        self.errors.append(message)

    def status(self, *args, **kwargs):
        return StatusCtx()

    def expander(self, *args, **kwargs):
        return StatusCtx()

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class NodeLoader:
    """Execute selected production top-level statements headlessly."""

    def __init__(self, ui):
        self.nodes = module_nodes()
        self.f = load_app()
        self.f["st"] = ui

    def assign(self, target):
        for node in self.nodes:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == target for t in node.targets
            ):
                return node
        raise AssertionError(f"Assign {target!r} not found in app source")

    def if_block(self, test_source):
        expected = normalize(test_source)
        for node in ast.walk(ast.parse(app_source(), filename=str(SOURCE))):
            if isinstance(node, ast.If) and normalize(ast.unparse(node.test)) == expected:
                return node
        raise AssertionError(f"If {test_source!r} not found in app source")


class IdentityGuardTests(unittest.TestCase):
    """The guard fails closed for anonymous, expired and unidentified sessions."""

    def setUp(self):
        self.f = load_app()

    def stub_user(self, logged_in=None, subject=None):
        fields = {}
        if logged_in is not None:
            fields["is_logged_in"] = logged_in
        if subject is not None:
            fields["sub"] = subject
        self.f["st"] = SimpleNamespace(user=SimpleNamespace(**fields))

    def test_anonymous_session_has_no_identity(self):
        self.stub_user(logged_in=False, subject="google-sub-1")
        self.assertIsNone(self.f["authenticated_google_identity"]())

    def test_unavailable_authentication_fails_closed(self):
        self.f["st"] = SimpleNamespace(user=Mock(
            side_effect=RuntimeError("Synthetic auth configuration failure")))
        self.assertIsNone(self.f["authenticated_google_identity"]())

    def test_authenticated_unidentified_session_fails_closed(self):
        self.stub_user(logged_in=True, subject="")
        self.assertIsNone(self.f["authenticated_google_identity"]())

    def test_non_string_subject_fails_closed(self):
        for value in (None, 12345, [], object()):
            with self.subTest(subject=type(value).__name__):
                self.stub_user(logged_in=True, subject=value)
                self.assertIsNone(self.f["authenticated_google_identity"]())

    def test_valid_identity_is_the_stable_subject(self):
        self.stub_user(logged_in=True, subject="  synthetic-teacher-sub  ")
        self.assertEqual(self.f["authenticated_google_identity"](),
                         "synthetic-teacher-sub")


class ExecutionBoundaryTests(unittest.TestCase):
    """The identity guard must run server-side inside every billable block,
    before any Gemini client construction: UI visibility is not authorization."""

    def test_manual_marking_is_gated_by_the_identity_guard(self):
        source = app_source()
        requested = source.index("manual_run_requested = st.button(")
        authorized = source.index("manual_run_authorized = (")
        gate = source.index("if manual_run_authorized:")
        # The guard participates in the authorized expression itself.
        header = source[requested:gate]
        self.assertIn("authenticated_google_identity()", header)
        self.assertLess(authorized, gate)
        first_client = source.index("genai.Client(", requested)
        self.assertLess(authorized, first_client)
        self.assertLess(gate, first_client)

    def test_classroom_marking_rechecks_identity_before_its_client(self):
        source = app_source()
        job = source.index("if job_request:")
        guard = source.index("authenticated_google_identity() is None", job)
        client = source.index("classroom_client = genai.Client(", job)
        self.assertLess(guard, client)

    def test_api_key_probe_is_gated_before_the_key_is_used(self):
        source = app_source()
        button = source.index('st.button("🔑 Test API key"')
        guard = source.index("authenticated_google_identity() is None", button)
        probe = source.index("test_api_key(api_key)", button)
        self.assertLess(guard, probe)

    def test_no_unguarded_gemini_client_construction_remains(self):
        source = app_source()
        constructions = [index for index in range(len(source))
                         if source.startswith("genai.Client(", index)]
        self.assertEqual(len(constructions), 3)  # key probe, classroom, manual
        gate_manual = source.index("if manual_run_authorized:")
        guard_classroom = source.index(
            "authenticated_google_identity() is None",
            source.index("if job_request:"))
        button_probe = source.index('st.button("🔑 Test API key"')
        guard_probe = source.index("authenticated_google_identity() is None",
                                   button_probe)
        probe_call = source.index("test_api_key(api_key)", button_probe)
        probe_fn_start = source.index("def test_api_key(")
        probe_fn_end = source.index("\ndef ", probe_fn_start + 10)
        for index in constructions:
            with self.subTest(client_offset=index):
                in_manual = gate_manual < index
                in_classroom = guard_classroom < index
                # The only client inside test_api_key is reachable solely
                # through the guarded sidebar call.
                in_probe = (probe_fn_start < index < probe_fn_end
                            and guard_probe < probe_call)
                self.assertTrue(in_manual or in_classroom or in_probe)


class ManualFlowAuthorizationTests(unittest.TestCase):
    """A forged pressed-widget value still fails the server-side identity."""

    def setUp(self):
        self.ui = RecordingErrorUI()
        self.harness = NodeLoader(self.ui)
        self.f = self.harness.f
        # Synthetic operator key; must never leak into any failure message.
        self.f["api_key"] = "synthetic-server-secret-key-value"

    def exec_manual_boundary(self):
        self.f["manual_run_requested"] = True  # forged/clicked widget state
        nodes = [self.harness.assign("manual_run_authorized"),
                 self.harness.if_block(
                     "manual_run_requested and not manual_run_authorized"),
                 self.harness.if_block("manual_run_authorized")]
        exec(compile(ast.Module(body=nodes, type_ignores=[]),
                     str(SOURCE), "exec"), self.f)

    def test_anonymous_forged_widget_value_cannot_authorize_marking(self):
        with self.subTest(session="anonymous"):
            self.exec_manual_boundary()
            self.assertFalse(self.f["manual_run_authorized"])
            self.assertEqual(len(self.ui.errors), 1)
            self.assertNotIn("synthetic-server-secret-key-value",
                             self.ui.errors[0])

    def test_unidentified_authenticated_session_cannot_authorize_marking(self):
        with self.subTest(session="authenticated-but-unidentified"):
            self.ui.user = SimpleNamespace(is_logged_in=True, sub="")
            self.exec_manual_boundary()
            self.assertFalse(self.f["manual_run_authorized"])
            self.assertEqual(len(self.ui.errors), 1)
            self.assertNotIn("synthetic-server-secret-key-value",
                             self.ui.errors[0])

    def test_logged_out_session_cannot_authorize_marking(self):
        with self.subTest(session="logged-out"):
            self.ui.user = SimpleNamespace(is_logged_in=False, sub="stale-sub")
            self.exec_manual_boundary()
            self.assertFalse(self.f["manual_run_authorized"])
            self.assertEqual(len(self.ui.errors), 1)

    def test_expired_session_cannot_authorize_marking(self):
        with self.subTest(session="expired"):
            # An expired session presents the same shape: no usable identity.
            self.ui.user = SimpleNamespace(
                is_logged_in=Mock(side_effect=RuntimeError("expired")))
            self.exec_manual_boundary()
            self.assertFalse(self.f["manual_run_authorized"])
            self.assertEqual(len(self.ui.errors), 1)

    def test_error_is_static_and_privacy_safe(self):
        self.exec_manual_boundary()
        message = self.f["AUTH_REQUIRED_MARKING"]
        self.assertIn("sign in with Google", message)
        self.assertIn("is not teacher verification", message)
        self.assertNotIn("synthetic-server-secret-key-value", message)
        self.assertEqual(self.ui.errors[0], message)


class ClassroomFlowAuthorizationTests(unittest.TestCase):
    """The `if job_request:` boundary stops before its client and job."""

    def setUp(self):
        self.ui = RecordingErrorUI()
        self.harness = NodeLoader(self.ui)
        self.f = self.harness.f
        self.f["api_key"] = "synthetic-server-secret-key-value"
        self.f["token"] = ""
        self.f["selected_course_id"] = "course-1"
        self.f["selected_assignment"] = {"id": "work-1", "title": "Synthetic"}
        self.f["ms_upload"] = None
        self.f["classroom_audit"] = False
        self.f["processed_ids"] = set()
        self.f["classroom_random_count"] = None
        self.f["force_remark"] = False
        self.f["document_state"] = None
        self.f["retain_references"] = False
        self.f["context_key"] = "synthetic-context"
        self.f["job_state"] = {"processed": set(), "last_result": None}
        self.f["job_request"] = True  # forged pressed "process now" widget
        self.f["genai"] = SimpleNamespace(Client=Mock(
            side_effect=AssertionError("Unexpected Gemini client construction")))
        self.f["run_classroom_marking_job"] = Mock(
            side_effect=AssertionError("Unexpected Classroom marking job"))
        self.job_block = self.harness.if_block("job_request")

    def exec_classroom_boundary(self):
        exec(compile(ast.Module(body=[self.job_block], type_ignores=[]),
                     str(SOURCE), "exec"), self.f)

    def test_anonymous_session_starts_no_classroom_job(self):
        self.exec_classroom_boundary()
        self.assertEqual(len(self.ui.errors), 1)
        self.assertIn("Classroom marking cannot start", self.ui.errors[0])
        self.assertIn("Nothing was uploaded or marked", self.ui.errors[0])
        self.assertNotIn("synthetic-server-secret-key-value", self.ui.errors[0])
        self.f["genai"].Client.assert_not_called()
        self.f["run_classroom_marking_job"].assert_not_called()

    def test_unidentified_session_starts_no_classroom_job(self):
        self.ui.user = SimpleNamespace(is_logged_in=True, sub="   ")
        self.exec_classroom_boundary()
        self.assertEqual(len(self.ui.errors), 1)
        self.assertIn("unidentified", self.ui.errors[0])
        self.assertNotIn("synthetic-server-secret-key-value", self.ui.errors[0])
        self.f["run_classroom_marking_job"].assert_not_called()

    def test_logged_out_session_starts_no_classroom_job(self):
        self.ui.user = SimpleNamespace(is_logged_in=False, sub="stale-sub")
        self.exec_classroom_boundary()
        self.assertEqual(len(self.ui.errors), 1)
        self.f["run_classroom_marking_job"].assert_not_called()

    def test_authenticated_session_reaches_the_classroom_job(self):
        self.f["authenticated_google_identity"] = lambda: "synthetic-teacher-sub"
        self.f["ms_upload"] = SimpleNamespace(
            name="scheme.pdf", size=10, getvalue=lambda: b"%PDF-synthetic")
        self.f["genai"] = SimpleNamespace(Client=Mock(return_value=Mock()))
        self.f["types"] = SimpleNamespace(HttpOptions=lambda **kw: kw)
        self.f["run_classroom_marking_job"] = Mock(return_value={
            "pending": 0, "processed": [],
            "message": "No new turned-in submissions found.",
            "active_submission_ids": []})
        self.exec_classroom_boundary()
        self.assertEqual(len(self.ui.errors), 0)
        self.f["run_classroom_marking_job"].assert_called_once()

    def test_classroom_failures_never_echo_the_key(self):
        self.f["authenticated_google_identity"] = lambda: "synthetic-teacher-sub"
        self.f["genai"] = SimpleNamespace(Client=Mock(return_value=Mock()))
        self.f["types"] = SimpleNamespace(HttpOptions=lambda **kw: kw)
        self.f["run_classroom_marking_job"] = Mock(
            side_effect=RuntimeError("synthetic failure with "
                                     "synthetic-server-secret-key-value"))
        self.exec_classroom_boundary()
        for recorded in self.ui.errors:
            self.assertNotIn("synthetic-server-secret-key-value", recorded)


class SecretRedactionTests(unittest.TestCase):
    def setUp(self):
        self.f = load_app()
        self.redact = self.f["redact_secret_values"]

    def test_configured_key_is_removed_from_provider_messages(self):
        redacted = self.redact(
            "Synthetic failure for synthetic-server-secret-key-value via v1beta",
            "synthetic-server-secret-key-value")
        self.assertNotIn("synthetic-server-secret-key-value", redacted)
        self.assertIn("redacted", redacted)

    def test_whitespace_padded_key_is_still_redacted(self):
        redacted = self.redact("bad key:  padded-key-0001 ", "  padded-key-0001 ")
        self.assertNotIn("padded-key-0001", redacted)

    def test_empty_or_missing_secrets_leave_message_unchanged(self):
        message = "Synthetic provider failure text."
        self.assertEqual(self.redact(message), message)
        self.assertEqual(self.redact(message, None, "", "   "), message)


class KeyProbeAuthorizationTests(unittest.TestCase):
    """The sidebar key probe must not run for anonymous sessions."""

    def setUp(self):
        self.ui = RecordingErrorUI()
        self.harness = NodeLoader(self.ui)
        self.f = self.harness.f
        self.f["api_key"] = "synthetic-server-secret-key-value"
        self.f["test_api_key"] = Mock(side_effect=AssertionError(
            "Unexpected provider key probe"))
        self.ui.button = lambda *args, **kwargs: True  # pressed probe widget
        sidebar = next(node for node in module_nodes()
                       if isinstance(node, ast.With)
                       and ast.unparse(node.items[0].context_expr) == "st.sidebar")
        self.pressed = next(node for node in sidebar.body
                            if isinstance(node, ast.If)
                            and "Test API key" in ast.unparse(node.test))

    def exec_key_probe(self):
        exec(compile(ast.Module(body=[self.pressed], type_ignores=[]),
                     str(SOURCE), "exec"), self.f)

    def test_anonymous_session_cannot_probe_the_server_key(self):
        self.exec_key_probe()
        self.assertEqual(len(self.ui.errors), 1)
        self.assertIn("Sign in with Google", self.ui.errors[0])
        self.assertNotIn("synthetic-server-secret-key-value", self.ui.errors[0])
        self.f["test_api_key"].assert_not_called()

    def test_authenticated_session_can_probe_the_key(self):
        self.f["authenticated_google_identity"] = lambda: "synthetic-teacher-sub"
        self.f["test_api_key"] = Mock(return_value=(True, "API key accepted."))
        self.exec_key_probe()
        self.assertEqual(self.ui.errors, [])
        self.f["test_api_key"].assert_called_once_with(
            "synthetic-server-secret-key-value")


class OwnerIsolationTests(unittest.TestCase):
    def setUp(self):
        self.ui = RecordingErrorUI()
        self.f = load_app()
        self.f["st"] = self.ui

    def test_marked_results_bind_to_authenticated_owner(self):
        self.ui.user = SimpleNamespace(is_logged_in=True,
                                       sub="synthetic-teacher-sub")
        row = self.f["new_review_row"]({"status": "Marked", "result": {"x": 1}},
                                       "manual")
        self.assertEqual(row["report_owner"], "google:synthetic-teacher-sub")

    def test_unidentified_sessions_do_not_receive_results(self):
        self.ui.user = SimpleNamespace(is_logged_in=True, sub="")
        self.assertIsNone(self.f["result_owner"]())


if __name__ == "__main__":
    unittest.main()
