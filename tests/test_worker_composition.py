"""Worker composition tests (offline; the PostgreSQL half lives in the job suite).

What is pinned here:
* the worker's discovery is tenant-scoped and EXPLICIT (env or ``--tenant``),
  never a privileged cross-tenant read;
* a job is executed through the app's OWN reviewed handler
  (``app.state.run_marking_job``) - the seam adds no route and no second
  implementation of claim/engine/register/settle;
* outcome reporting uses existing error vocabulary only, and the exit codes
  separate "wrongly composed" from "the job failed";
* the dev store gains the same ``find_runnable_job`` discovery the PostgreSQL
  store has, so the two backends stay interchangeable.
"""
import os
import tempfile
import types
import unittest
from unittest import mock

import job_store
import postgres_job_store
import worker
from api.app import ApiError, _job_api, create_app


class TenantSelectionTests(unittest.TestCase):
    def test_env_parses_comma_separated_tenants(self):
        env = {"AI_MARKER_WORKER_TENANTS": "school-a, school-b"}
        self.assertEqual(worker.tenants_from(environ=env),
                         ("school-a", "school-b"))

    def test_cli_overrides_env_and_duplicates_collapse(self):
        env = {"AI_MARKER_WORKER_TENANTS": "school-env"}
        self.assertEqual(
            worker.tenants_from(environ=env, cli_tenants=["a", "b", "a"]),
            ("a", "b"))

    def test_no_tenants_is_empty_not_a_guess(self):
        self.assertEqual(worker.tenants_from(environ={}), ())
        self.assertEqual(worker.tenants_from(environ={
            "AI_MARKER_WORKER_TENANTS": " , "}), ())


class JobApiSelectionTests(unittest.TestCase):
    def test_pg_dsn_selects_the_postgres_store(self):
        with mock.patch.dict(os.environ, {"AI_MARKER_PG_DSN":
                                          "postgresql://x"}):
            self.assertIs(_job_api(), postgres_job_store)

    def test_no_dsn_keeps_the_dev_store(self):
        env = dict(os.environ)
        env.pop("AI_MARKER_PG_DSN", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIs(_job_api(), job_store)


class _FakeApi:
    def __init__(self, candidate=None):
        self._candidate = candidate
        self.connections = 0

    def find_runnable_job(self, connection, *, tenant_id, now=None):
        self.seen = (tenant_id, now)
        return self._candidate

    def require_connection(self):
        self.connections += 1
        return types.SimpleNamespace(close=lambda: None)


class _FakeApp:
    def __init__(self, outcome=None, error=None):
        def run_marking_job(job_id, who):
            if error is not None:
                raise error
            return outcome
        self.state = types.SimpleNamespace(run_marking_job=run_marking_job)


class RunOneTests(unittest.TestCase):
    def test_no_candidate_is_not_an_execution(self):
        api = _FakeApi(candidate=None)
        outcome = worker.run_one(_FakeApp(), api, object(),
                                 tenant_id="school-a")
        self.assertEqual(outcome, {"executed": False, "tenant_id": "school-a"})

    def test_candidate_runs_through_the_shared_handler(self):
        api = _FakeApi(candidate={"job_id": "job:1", "owner_subject": "o"})
        outcome = worker.run_one(_FakeApp(outcome={"status": "succeeded"}),
                                 api, object(), tenant_id="school-a")
        self.assertEqual(outcome["executed"], True)
        self.assertEqual(outcome["job_id"], "job:1")
        self.assertEqual(outcome["status"], "succeeded")

    def test_handler_error_is_reported_with_its_existing_code(self):
        api = _FakeApi(candidate={"job_id": "job:1", "owner_subject": "o"})
        outcome = worker.run_one(
            _FakeApp(error=ApiError("model_unavailable", "no engine")),
            api, object(), tenant_id="school-a")
        self.assertEqual(outcome["status"], "error")
        self.assertEqual(outcome["error_code"], "model_unavailable")

    def test_store_error_maps_to_the_store_code(self):
        api = _FakeApi(candidate={"job_id": "job:1", "owner_subject": "o"})
        outcome = worker.run_one(
            _FakeApp(error=job_store.JobStoreError("store_unusable")),
            api, object(), tenant_id="school-a")
        self.assertEqual(outcome["error_code"], "store_unusable")


class MainExitCodeTests(unittest.TestCase):
    def _main(self, argv, api, app, tenants="school-a"):
        env = dict(os.environ)
        env.pop("AI_MARKER_PG_DSN", None)
        if tenants:
            env["AI_MARKER_WORKER_TENANTS"] = tenants
        else:
            env.pop("AI_MARKER_WORKER_TENANTS", None)
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(worker.runtime_config, "require",
                                  lambda: None), \
                mock.patch.object(worker, "create_app", lambda: app), \
                mock.patch.object(worker, "_job_api", lambda: api):
            return worker.main(argv)

    def test_missing_tenants_is_a_composition_error(self):
        self.assertEqual(self._main(["--once"], _FakeApi(), _FakeApp(),
                                    tenants=""), worker.EXIT_COMPOSITION)

    def test_once_with_no_work_is_ok(self):
        self.assertEqual(self._main(["--once"], _FakeApi(), _FakeApp()),
                         worker.EXIT_OK)

    def test_once_runs_one_job_to_success(self):
        api = _FakeApi(candidate={"job_id": "job:1", "owner_subject": "o"})
        app = _FakeApp(outcome={"status": "succeeded"})
        self.assertEqual(self._main(["--once"], api, app), worker.EXIT_OK)

    def test_once_with_a_failed_job_is_a_job_exit(self):
        api = _FakeApi(candidate={"job_id": "job:1", "owner_subject": "o"})
        app = _FakeApp(outcome={"status": "failed",
                                "error_code": "service_unavailable"})
        self.assertEqual(self._main(["--once"], api, app), worker.EXIT_JOB)

    def test_a_wrongly_composed_process_is_a_composition_exit(self):
        api = _FakeApi(candidate={"job_id": "job:1", "owner_subject": "o"})
        app = _FakeApp(error=ApiError("model_unavailable", "no engine"))
        self.assertEqual(self._main(["--once"], api, app),
                         worker.EXIT_COMPOSITION)

    def test_once_and_loop_together_are_refused(self):
        self.assertEqual(self._main(["--once", "--loop"], _FakeApi(),
                                    _FakeApp()), worker.EXIT_COMPOSITION)


class DevStoreDiscoveryTests(unittest.TestCase):
    """The dev store must offer the same discovery the PostgreSQL store does."""

    def test_find_runnable_job_picks_queued_and_expired_only(self):
        with tempfile.TemporaryDirectory() as folder:
            connection = job_store.open_store(os.path.join(folder, "jobs.db"))
            try:
                job_store.create_job(connection, tenant_id="school-a",
                                     owner="o", idempotency_key="k1",
                                     document_digest="a" * 64)
                found = job_store.find_runnable_job(
                    connection, tenant_id="school-a", now=1000)
                self.assertEqual(found["idempotency_key"], "k1")
                # Claim it with a live lease: discovery must NOT return it.
                _, claimed = job_store.claim_job(
                    connection, tenant_id="school-a", owner="o",
                    job_id=found["job_id"], now=1000, lease_seconds=60)
                self.assertTrue(claimed)
                self.assertIsNone(job_store.find_runnable_job(
                    connection, tenant_id="school-a", now=1030))
                # Lease expired: discoverable again (crash recovery).
                self.assertIsNotNone(job_store.find_runnable_job(
                    connection, tenant_id="school-a", now=1100))
                # Another tenant never sees it.
                self.assertIsNone(job_store.find_runnable_job(
                    connection, tenant_id="school-b", now=1100))
            finally:
                # Windows cannot delete the temp dir while the file is open.
                connection.close()


class WorkerSeamTests(unittest.TestCase):
    def test_the_seam_is_the_reviewed_handler_not_a_new_route(self):
        app = create_app(verify_token=lambda token: "subject")
        self.assertTrue(callable(app.state.run_marking_job))
        routes = {(method, route.path)
                  for route in app.routes
                  for method in getattr(route, "methods", set())
                  if route.path.startswith("/v1/")}
        self.assertIn(("POST", "/v1/jobs/{job_id}/run"), routes)
        self.assertEqual([path for _, path in routes if "worker" in path], [],
                         "the seam must not become a route")


if __name__ == "__main__":
    unittest.main()
