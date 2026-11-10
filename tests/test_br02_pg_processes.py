"""BR-02 PG harness: owned child-process coverage (offline + PG-gated; live
classes unexecuted by design until the future approval exists - a skip is
the honest gate, never a pass).

Coverage map:
* offline: the child CLI refuses in-process before any OS process or server
  import; generic external argv execution is refused; the child environment
  is built from allowlisted reads only (recording-stand-in spy);
* gated (REAL operations, future live path): one owned child serves the REAL
  production composition on an ephemeral approved-loopback port with owned
  lifecycle - readiness, bounded requests, strict reaping; CONCURRENT HTTP
  competition across independent PIDs (barrier + thread pool: the same
  idempotency key yields exactly one committed event and replay receipts,
  a different payload under the same key a proper 409); an ACTUAL restart:
  child A reviews, captures the POST-COMMIT projection/history, EXITS and its
  PID is asserted reaped, then an independent child B serves the identical
  POST-COMMIT projection/history/revision AND replays the SAVED receipt; the
  IMMUTABLE original bytes/digest are compared separately through the REAL
  store. Row predicates follow the real storage contract (audit_events/
  results carry the RAW membership subject).
"""
import json
import os
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))      # tests/

try:
    import psycopg
except ImportError:  # pragma: no cover - driver ships in the test manifest
    psycopg = None

import tests.br02_pg_api_child as api_child
import br02_pg_fixtures as fixtures
import tests.br02_pg_target as br02_pg_target


def _live_approval(dsn):
    """Future supply point; returns None today, never inferred from
    env/DSN/flags."""
    return None


def _live_runtime_binding():
    """Future supply point for the RESTRICTED child runtime handle:
    ``(app_dsn, runtime_login)``, supplied independently of the
    administrative test DSN. Returns None today; never inferred."""
    return None


def _test_dsn():
    return os.environ.get(br02_pg_target.TEST_DSN_ENV, "").strip()


def _live_gate():
    """Authorization first: the current unauthorized phase is refused before
    ANY environment value access, driver connect, or module entrypoint."""
    if not br02_pg_target.execution_authorized():
        return ("live execution remains refused in the current phase: it "
                "requires a future independently approved target plus "
                "separate execution authorization; a DSN alone never "
                "unlocks it")
    if psycopg is None:
        return "psycopg driver is not installed"
    if not _test_dsn():
        return ("AI_MARKER_TEST_PG_DSN is not configured - the BR-02 "
                "PostgreSQL gate is BLOCKED locally; never AI_MARKER_PG_DSN")
    return None


class _RecordingMapping(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads = []

    def __getitem__(self, key):
        self.reads.append(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.reads.append(key)
        return super().get(key, default)


class ChildGateOfflineTests(unittest.TestCase):
    """The child refuses before imports, OS processes, and generic argv."""

    def test_child_cli_refuses_in_process_before_any_process_or_import(self):
        import io

        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            code = api_child.child_main(["--serve", "--port", "8000"])
        self.assertEqual(code, 3)
        self.assertIn("refused", stderr.getvalue())
        self.assertNotIn("api.server", sys.modules,
                         "the child gate must precede every server import")

    def test_generic_external_argv_execution_is_refused(self):
        with self.assertRaises(br02_pg_target.TargetRefused) as caught:
            api_child.launch_child({}, ["python", "-c", "print(1)"], 5,
                                   approval=None)
        self.assertEqual(caught.exception.reason,
                         br02_pg_target.EXECUTION_NOT_AUTHORIZED)

    def test_run_child_server_refuses_before_any_os_process(self):
        with mock.patch("subprocess.Popen") as spawn:
            with self.assertRaises(br02_pg_target.TargetRefused) as caught:
                api_child.run_child_server(None)
        self.assertEqual(caught.exception.reason,
                         br02_pg_target.EXECUTION_NOT_AUTHORIZED)
        spawn.assert_not_called()

    def test_child_environment_reads_allowlisted_keys_only(self):
        parent = _RecordingMapping({
            "PATH": "synthetic-path",
            "SYSTEMROOT": "synthetic-root",
            "GEMINI_API_KEY": "synthetic-not-real",
            "AI_MARKER_SUPABASE_SERVICE_KEY": "synthetic-not-real",
            _pg_dsn_env(): "postgresql://inert-parent/db",
        })
        approval = fixtures.SeedApproval(
            approved_manifest=br02_pg_target.supply_approved_manifest(
                *br02_pg_target.synthetic_approved_manifest()),
            execution_authorized=True,
            target_dsn="postgresql://inert-admin/db",
            app_dsn="postgresql://inert-app/db",
            runtime_login="marker_app_login",
        )
        with mock.patch.object(br02_pg_target, "authorize_execution"):
            child = api_child.build_child_environ(
                parent, approval, "postgresql://inert-app/db")
        for key in parent.reads:
            with self.subTest(key=key):
                self.assertIn(key, api_child.CHILD_ENV_ALLOWLIST,
                              "only neutral allowlisted names are read; "
                              "no DSN or credential name is ever read")
        self.assertNotIn("GEMINI_API_KEY", child)
        self.assertNotIn("AI_MARKER_SUPABASE_SERVICE_KEY", child)
        self.assertEqual(child[_pg_dsn_env()],
                         "postgresql://inert-app/db")
        self.assertEqual(child[_runtime_env_key()],
                         _production_value())

    def test_child_dsn_binding_refuses_admin_and_misbound_handles(self):
        manifest = br02_pg_target.supply_approved_manifest(
            *br02_pg_target.synthetic_approved_manifest())

        def approval(**overrides):
            values = {
                "approved_manifest": manifest,
                "execution_authorized": True,
                "target_dsn": "postgresql://inert-admin/db",
                "app_dsn": "postgresql://inert-app/db",
                "runtime_login": "marker_app_login",
            }
            values.update(overrides)
            return fixtures.SeedApproval(**values)

        with mock.patch.object(br02_pg_target, "authorize_execution"):
            with self.assertRaises(br02_pg_target.TargetRefused):
                api_child.build_child_environ(
                    {}, approval(), "postgresql://inert-admin/db")
            with self.assertRaises(br02_pg_target.TargetRefused):
                api_child.build_child_environ(
                    {}, approval(target_dsn="postgresql://same/db",
                                 app_dsn="postgresql://same/db"),
                    "postgresql://same/db")
            with self.assertRaises(br02_pg_target.TargetRefused):
                api_child.build_child_environ(
                    {}, approval(app_dsn="", runtime_login=""),
                    "postgresql://inert-app/db")

    def test_child_application_https_origin_and_process_environment(self):
        import auth_oidc

        manifest = br02_pg_target.supply_approved_manifest(
            *br02_pg_target.synthetic_approved_manifest())
        approval = fixtures.SeedApproval(
            approved_manifest=manifest,
            execution_authorized=True,
            target_dsn="postgresql://inert-admin/db",
            app_dsn="postgresql://inert-app/db",
            runtime_login="marker_app_login",
        )
        environ = {
            _runtime_env_key(): _production_value(),
            _pg_dsn_env(): "postgresql://inert-app/db",
            "BR02_CHILD_PORT": "8123",
        }
        sentinel = object()
        with mock.patch.object(br02_pg_target, "authorize_execution"), \
                mock.patch("api.app.create_app",
                           return_value=sentinel) as build, \
                mock.patch.dict(os.environ, {}, clear=False):
            app = api_child.build_child_application(environ,
                                                    approval=approval)
            self.assertIs(app, sentinel)
            self.assertEqual(build.call_args.kwargs["allowed_origins"],
                             ["https://127.0.0.1:8123"])
            self.assertIsNone(build.call_args.kwargs["engine"])
            verify = build.call_args.kwargs["verify_token"]
            self.assertEqual(verify("synthetic-subject:alice"), "alice")
            with self.assertRaises(auth_oidc.OidcError):
                verify("not-a-synthetic-token")
            self.assertEqual(os.environ[_runtime_env_key()],
                             _production_value())
            self.assertEqual(os.environ[_pg_dsn_env()],
                             "postgresql://inert-app/db")


def _pg_dsn_env():
    import runtime_config
    return runtime_config.PG_DSN_ENV


def _runtime_env_key():
    import runtime_config
    return runtime_config.RUNTIME_ENV


def _production_value():
    import runtime_config
    return runtime_config.RUNTIME_PRODUCTION


class _LiveProcessesBase(unittest.TestCase):
    """Live base for REAL child-process coverage (future live path)."""

    @classmethod
    def setUpClass(cls):
        reason = _live_gate()
        if reason:
            raise unittest.SkipTest(reason)
        cls.dsn = _test_dsn()
        cls.conn = psycopg.connect(cls.dsn, autocommit=True)
        try:
            rows = dict(cls.conn.execute(
                "select key, value from schema_meta"
                " where key in ('schema_version','result_context_version')"
            ).fetchall())
        except Exception:
            cls.conn.close()
            raise
        if rows.get("schema_version") != "1" \
                or rows.get("result_context_version") != "1":
            cls.conn.close()
            raise unittest.SkipTest(
                "target schema capability missing; no implicit migrations "
                "are ever applied by tests")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "conn", None) is not None:
            cls.conn.close()

    def setUp(self):
        binding = _live_runtime_binding()
        if binding is None or not all(binding):
            raise unittest.SkipTest(
                "the future approval must separately supply the restricted "
                "app_dsn and runtime_login for child processes; neither is "
                "inferred from the administrative test DSN")
        app_dsn, runtime_login = binding
        self.conn = psycopg.connect(self.dsn, autocommit=True)
        try:
            self.bundle = fixtures.build_bundle()
            self.approval = _live_approval(self.dsn)
            if self.approval is None or self.approval.app_dsn != app_dsn \
                    or self.approval.runtime_login != runtime_login \
                    or self.approval.run_marker != self.approval.approved_manifest.binding['run_id']:
                raise AssertionError('process fixture requires the approved run marker and runtime binding')
            # The seeder's unconditional gate still refuses today; the child
            # classes therefore stay skipped until the future approval
            # replaces the gate implementation.
            fixtures.seed(self.conn, self.bundle, self.approval,
                          is_production=False)
        except br02_pg_target.TargetRefused as refusal:
            self.conn.close()
            if refusal.reason == br02_pg_target.EXECUTION_NOT_AUTHORIZED:
                raise unittest.SkipTest(
                    "the future approval path does not exist yet; the child "
                    "process coverage stays unexecuted (never passed)")
            raise
        except Exception:
            self.conn.close()
            raise

    def tearDown(self):
        if getattr(self, "conn", None) is not None:
            self.conn.close()

    def auth(self, subject=None):
        return {"Authorization": "Bearer synthetic-subject:"
                                 + (subject or self.bundle.raw_subject)}

    def register_original(self):
        import identity_context
        import postgres_result_store
        store = postgres_result_store.open_store(
            self.dsn,
            identity=identity_context.from_verified_subject(
                self.bundle.raw_subject, self.bundle.tenant_id),
            fresh_completion_denied=False)
        try:
            store.register_with_context(
                result_id=self.bundle.result_id,
                original_output=self.bundle.original_output,
                scope="manual", review_key=self.bundle.review_key,
                document_digest=self.bundle.document_digest)
        finally:
            store.close()

    def result_snapshot(self):
        """One-consistency-point read through the REAL store: original
        bytes/digest plus revision and event chain."""
        import identity_context
        import postgres_result_store
        store = postgres_result_store.open_store(
            self.dsn,
            identity=identity_context.from_verified_subject(
                self.bundle.raw_subject, self.bundle.tenant_id),
            fresh_completion_denied=False)
        try:
            return store.snapshot(result_id=self.bundle.result_id)
        finally:
            store.close()

    def audit_event_count(self):
        # RAW membership subject: the real storage contract for
        # audit_events/results owner predicates.
        return self.conn.execute(
            "select count(*) from audit_events where tenant_id=%s"
            " and owner_subject=%s and result_id=%s",
            (self.bundle.tenant_id, self.bundle.raw_subject,
             self.bundle.result_id)).fetchone()[0]


class ChildProcessLiveTests(_LiveProcessesBase):
    """REAL child processes on loopback (gated; future live path). Ports are
    allocated by the owned launcher (ephemeral bind, released, bound by the
    child) under the future approval - never fixed constants."""

    def test_one_child_ready_request_and_reap(self):
        with api_child.run_child_server(self.approval) as child:
            self.assertTrue(child.pid > 0)
            status, _headers, body = child.request("GET", "/v1/healthz")
            self.assertEqual(status, 200)
            self.assertEqual(body, b'{"status":"ok"}')
        self.assertIsNotNone(child._process.poll(),
                             "the owned child is reaped, not abandoned")

    def test_concurrent_http_competition_across_independent_pids(self):
        self.register_original()
        children = []
        try:
            # Own the first child even if constructing the second one fails.
            for _ in range(2):
                children.append(api_child.run_child_server(self.approval))
            self.assertNotEqual(children[0].pid, children[1].pid)
            path = f"/v1/results/{self.bundle.result_id}/decisions"
            barrier = threading.Barrier(4, timeout=60)

            def post(index):
                body = {"question_number": "1", "action": "approve",
                        "expected_revision": 0,
                        "idempotency_key": "compete-key-1",
                        "reason": "checked"}
                child = children[index % 2]
                barrier.wait()
                status, _headers, raw = child.request(
                    "POST", path, headers={**self.auth(),
                                           "Content-Type":
                                           "application/json"},
                    body=_json_bytes(body))
                return status, json.loads(raw)

            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(post, range(4)))
        finally:
            for child in children:
                child.stop()
        for index, (status, payload) in enumerate(results):
            self.assertEqual(status, 200, (index, payload))
        committed = [payload for _s, payload in results
                     if not payload["replayed"]]
        replayed = [payload for _s, payload in results if payload["replayed"]]
        self.assertEqual(len(committed), 1,
                         "exactly one committed event under the shared key")
        self.assertEqual(len(replayed), 3)
        sequences = {payload["event_sequence"]
                     for _s, payload in results}
        self.assertEqual(len(sequences), 1,
                         "every replay returns the SAME saved sequence")
        # A DIFFERENT payload under the SAME key is a proper semantic
        # conflict, not a silent replay (fresh child, same database).
        with api_child.run_child_server(self.approval) as verifier:
            status, _headers, raw = verifier.request(
                "POST", path, headers={**self.auth(),
                                       "Content-Type": "application/json"},
                body=_json_bytes({"question_number": "1",
                                  "action": "approve",
                                  "expected_revision": 0,
                                  "idempotency_key": "compete-key-1",
                                  "reason": "a DIFFERENT reason"}))
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(raw)["error"]["code"],
                         "idempotency_key_payload_mismatch")
        self.assertEqual(self.audit_event_count(), 1,
                         "one event per accepted request across all PIDs")

    def test_actual_restart_reaped_pid_and_saved_receipts(self):
        self.register_original()
        projection_path = f"/v1/results/{self.bundle.result_id}/projection"
        events_path = f"/v1/results/{self.bundle.result_id}/events"
        decision_path = f"/v1/results/{self.bundle.result_id}/decisions"
        body = {"question_number": "1", "action": "correct",
                "expected_revision": 0, "idempotency_key": "restart-key-1",
                "reason": "rework", "new_score": "3"}
        # PRE-COMMIT immutable original through the REAL store (bytes+digest).
        before = self.result_snapshot()
        before_bytes = json.dumps(before["original_output"], sort_keys=True,
                                  separators=(",", ":")).encode("utf-8")
        with api_child.run_child_server(self.approval) as first:
            first_pid = first.pid
            status_a, _h, projection_before_a = first.request(
                "GET", projection_path, headers=self.auth())
            commit_status, _h, commit_body = first.request(
                "POST", decision_path,
                headers={**self.auth(),
                         "Content-Type": "application/json"},
                body=_json_bytes(body))
            # POST-COMMIT projection + history captured from child A BEFORE
            # its exit: these are the durable state B must reproduce.
            post_status_a, _h, projection_after_a = first.request(
                "GET", projection_path, headers=self.auth())
            history_status_a, _h, history_after_a = first.request(
                "GET", events_path, headers=self.auth())
            committed_a = self.result_snapshot()
        self.assertEqual(status_a, 200)
        self.assertEqual(json.loads(projection_before_a)["result_revision"],
                         0)
        self.assertEqual(commit_status, 200)
        self.assertFalse(json.loads(commit_body)["replayed"])
        self.assertEqual(post_status_a, 200)
        self.assertEqual(history_status_a, 200)
        # The child CONTEXT EXITED and its PID is actually reaped BEFORE B.
        self.assertIsNotNone(first._process.poll(),
                             "child A exited and was reaped")
        self.assertNotEqual(first._process.pid, 0)
        self.assertEqual(committed_a["result_revision"], 1)
        self.assertEqual(committed_a["original_digest"],
                         before["original_digest"])
        self.assertEqual(committed_a["original_output"],
                         before["original_output"])
        # An INDEPENDENT child B (new PID, new port, same database, no
        # shared memory) serves the identical POST-COMMIT durable state.
        with api_child.run_child_server(self.approval) as second:
            self.assertNotEqual(second.pid, first_pid)
            post_status_b, _h, projection_after_b = second.request(
                "GET", projection_path, headers=self.auth())
            history_status_b, _h, history_after_b = second.request(
                "GET", events_path, headers=self.auth())
            replay_status, _h, replay_body = second.request(
                "POST", decision_path,
                headers={**self.auth(),
                         "Content-Type": "application/json"},
                body=_json_bytes(body))
        self.assertEqual(post_status_b, post_status_a)
        self.assertEqual(history_status_b, history_status_a)
        self.assertEqual(projection_after_b, projection_after_a,
                         "identical POST-COMMIT projection across the restart")
        self.assertEqual(history_after_b, history_after_a,
                         "identical POST-COMMIT history across the restart")
        history = json.loads(history_after_b)
        self.assertEqual(history["result_revision"], 1)
        self.assertEqual(len(history["events"]), 1)
        self.assertEqual(
            json.loads(projection_after_b)["result_revision"], 1)
        replay = json.loads(replay_body)
        self.assertEqual(replay_status, 200)
        self.assertTrue(replay["replayed"],
                        "the SAVED receipt replays after the restart")
        self.assertEqual(replay["event_sequence"],
                         json.loads(commit_body)["event_sequence"])
        # The immutable original is unchanged across the restart: bytes and
        # digest both come from the REAL store, not the mutable projection.
        after = self.result_snapshot()
        self.assertEqual(after["result_revision"], 1)
        self.assertEqual(after["original_digest"], before["original_digest"])
        self.assertEqual(after["original_output"], before["original_output"])
        after_bytes = json.dumps(after["original_output"], sort_keys=True,
                                 separators=(",", ":")).encode("utf-8")
        self.assertEqual(after_bytes, before_bytes,
                         "immutable original bytes across the restart")


def _json_bytes(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
