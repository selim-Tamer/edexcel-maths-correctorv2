"""Container packaging contract (offline; no Docker daemon, no network).

Docker is NOT available in this environment and no image is built or pushed.
These tests therefore pin the reviewable properties of the packaging files and
exercise the container's actual startup contract IN PROCESS:

* the image installs the single reviewed manifest and runs as a non-root user;
* the default command is the documented ``uvicorn api.server:application``
  with a ``$PORT`` binding, and the health check hits ``/v1/healthz``;
* NO secret value and NO licensed mark-scheme PDF is baked into the image
  (the scheme is a read-only mount at the documented path);
* the production composition the container claims starts only when every
  required component is present, and refuses to start when one is missing.

The in-process half is the same ``runtime_config.require`` + ``create_app``
path the container entrypoint executes at import time. It is not a substitute
for a real image build: the honest label for that is in the report.
"""
import importlib
import os
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"

PRODUCTION_ENV = {
    "AI_MARKER_RUNTIME": "production",
    "AI_MARKER_PG_DSN":
        "postgresql://marker_app:p%40ss@db.example:5432/postgres?sslmode=require",
    "GOOGLE_OIDC_AUDIENCE": "1234567890.apps.googleusercontent.com",
    "AI_MARKER_SUPABASE_URL": "https://abcdefghijklmnop.supabase.co",
    "AI_MARKER_SUPABASE_SERVICE_KEY": "synthetic-service-key-not-real",
    "AI_MARKER_STORAGE_BUCKET": "marker-private",
    "AI_MARKER_ALLOWED_ORIGINS": "https://marker.example",
    # The engine stays OFF in this smoke run: no paid call, no scheme needed.
    "AI_MARKER_ENGINE_REAL": "0",
}


class DockerfileContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = DOCKERFILE.read_text(encoding="utf-8")
        cls.ignore = DOCKERIGNORE.read_text(encoding="utf-8")

    def test_base_image_is_pinned_by_tag_and_digest(self):
        from_lines = [line for line in self.text.splitlines()
                      if line.strip().upper().startswith("FROM ")]
        self.assertEqual(1, len(from_lines), "exactly one build stage")
        image = from_lines[0].split()[1]
        name, separator, digest = image.partition("@sha256:")
        self.assertEqual("python:3.13-slim-bookworm", name)
        self.assertEqual("@sha256:", separator,
                         "the base image must be digest-pinned")
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertNotIn(":latest", image)

    def test_installs_the_single_reviewed_manifest(self):
        self.assertIn("COPY requirements.txt", self.text)
        self.assertIn("pip install --no-cache-dir -r requirements.txt",
                      self.text)
        self.assertIn("pip check", self.text)

    def test_runs_as_a_non_root_user(self):
        self.assertIn("useradd", self.text)
        match = re.search(r"^USER\s+(\S+)\s*$", self.text, re.MULTILINE)
        self.assertIsNotNone(match, "the image must declare USER")
        self.assertNotIn(match.group(1), {"root", "0", "0:0"})

    def test_default_command_serves_the_asgi_entrypoint_on_port(self):
        cmd = re.search(r'^CMD\s+\[(.*)\]\s*$', self.text, re.MULTILINE)
        self.assertIsNotNone(cmd)
        self.assertIn("uvicorn", cmd.group(1))
        self.assertIn("api.server:application", cmd.group(1))
        self.assertIn("${PORT:-8080}", cmd.group(1))

    def test_healthcheck_targets_the_public_liveness_route(self):
        self.assertIn("HEALTHCHECK", self.text)
        self.assertIn("/v1/healthz", self.text)

    def test_no_secret_value_or_licensed_scheme_is_baked(self):
        for name in ("GEMINI_API_KEY", "AI_MARKER_SUPABASE_SERVICE_KEY",
                     "AI_MARKER_PG_DSN"):
            with self.subTest(name=name):
                # Naming the variable in documentation is fine; assigning a
                # value in the image is not.
                self.assertNotRegex(self.text,
                                    rf"^ENV\s+.*{re.escape(name)}\s*=",
                                    re.MULTILINE)
        self.assertNotRegex(self.text, r"COPY\s+.*scheme\.pdf")
        self.assertIn("scheme.pdf", self.ignore)

    def test_dockerignore_excludes_git_tests_and_local_data(self):
        for pattern in (".git", "tests", "*.sqlite3", ".env", "*.pdf"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, self.ignore)


class ProductionStartupContractTests(unittest.TestCase):
    """The exact composition api/server.py validates before serving."""

    def _clean_module(self):
        sys.modules.pop("api.server", None)
        self.addCleanup(sys.modules.pop, "api.server", None)

    def test_valid_production_composition_builds_and_serves_healthz(self):
        from unittest import mock
        from fastapi.testclient import TestClient

        self._clean_module()
        with mock.patch.dict(os.environ, PRODUCTION_ENV, clear=True):
            server = importlib.import_module("api.server")
            client = TestClient(server.application)
            response = client.get("/v1/healthz")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok"}, response.json())

    def test_missing_database_dsn_fails_startup_instead_of_serving(self):
        from unittest import mock
        import runtime_config

        missing = dict(PRODUCTION_ENV)
        missing.pop("AI_MARKER_PG_DSN")
        with mock.patch.dict(os.environ, missing, clear=True):
            with self.assertRaises(runtime_config.RuntimeConfigError) as caught:
                runtime_config.require()
        self.assertEqual("store_unusable", caught.exception.code)

    def test_development_store_in_production_is_refused(self):
        from unittest import mock
        import runtime_config

        bad = dict(PRODUCTION_ENV)
        bad["AI_MARKER_JOB_DB"] = "C:/tmp/jobs.sqlite3"
        with mock.patch.dict(os.environ, bad, clear=True):
            with self.assertRaises(runtime_config.RuntimeConfigError) as caught:
                runtime_config.require()
        self.assertEqual("dev_opt_in_required", caught.exception.code)

    def test_engine_opt_in_without_scheme_fails_closed(self):
        from unittest import mock
        import runtime_config

        bad = dict(PRODUCTION_ENV)
        bad["AI_MARKER_ENGINE_REAL"] = "1"
        bad["GEMINI_API_KEY"] = "synthetic"
        with mock.patch.dict(os.environ, bad, clear=True):
            with self.assertRaises(runtime_config.RuntimeConfigError) as caught:
                runtime_config.require()
        self.assertEqual("model_unavailable", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
