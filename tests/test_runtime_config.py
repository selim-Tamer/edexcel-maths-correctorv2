"""Offline tests for the production runtime composition contract.

Pinned here:
* the exact env names the deployed runtime reads (no invented variables);
* production refuses to start without the database, identity and storage
  components, and refuses development-only store paths outright;
* a production DSN must require TLS and a Supabase URL must be HTTPS;
* the engine stays OFF unless explicitly opted in, and a half-configured
  engine is refused rather than silently skipped;
* the report never contains a secret value (safe to log/attach);
* the setup packet documents every env name in the contract.
"""
from __future__ import annotations

import json
import pathlib
import unittest

import runtime_config

ROOT = pathlib.Path(__file__).resolve().parents[1]
PACKET = ROOT / "docs" / "BETA_DEPLOY_SETUP.md"

SECRET_DSN = "postgresql://marker_runtime:s3cr3t-pg-pass@db.example.supabase.co:5432/postgres?sslmode=require"
SECRET_SUPABASE = "sbp_s3cr3t-service-role-key"
SECRET_GEMINI = "AIza-s3cr3t-gemini-key"


def production_env(**overrides):
    env = {
        runtime_config.RUNTIME_ENV: "production",
        runtime_config.PG_DSN_ENV: SECRET_DSN,
        runtime_config.OIDC_AUDIENCE_ENV: "1234567890-public-client-id.apps.googleusercontent.com",
        runtime_config.SUPABASE_URL_ENV: "https://abcdefghijklmno.supabase.co",
        runtime_config.SUPABASE_KEY_ENV: SECRET_SUPABASE,
        runtime_config.BUCKET_ENV: "marker-pdfs",
        runtime_config.ALLOWED_ORIGINS_ENV:
            "https://marker-beta.pages.dev",
    }
    env.update(overrides)
    return {key: value for key, value in env.items() if value is not None}


class DevelopmentTests(unittest.TestCase):
    def test_default_runtime_is_development_and_needs_nothing(self):
        report = runtime_config.evaluate({})
        self.assertEqual(report["runtime"], "development")
        self.assertFalse(report["production"])
        self.assertTrue(report["ready"])
        self.assertEqual(report["problems"], [])

    def test_unknown_runtime_value_is_refused_not_guessed(self):
        report = runtime_config.evaluate(
            {runtime_config.RUNTIME_ENV: "prod"})
        self.assertFalse(report["ready"])
        self.assertEqual(report["problems"][0]["code"], "dev_opt_in_required")
        self.assertEqual(report["runtime"], "development")

    def test_development_does_not_require_production_components(self):
        report = runtime_config.evaluate(
            {runtime_config.RUNTIME_ENV: "development"})
        self.assertTrue(report["ready"])
        self.assertIn("development", report["mode_label"])


class ProductionReadinessTests(unittest.TestCase):
    def test_complete_production_configuration_is_ready(self):
        report = runtime_config.require(production_env())
        self.assertTrue(report["ready"])
        self.assertTrue(report["production"])
        self.assertEqual(report["problems"], [])
        self.assertFalse(report["engine"]["opted_in"])

    def test_each_missing_component_is_named_with_its_existing_code(self):
        expected = {
            runtime_config.PG_DSN_ENV: "store_unusable",
            runtime_config.OIDC_AUDIENCE_ENV: "oidc_not_configured",
            runtime_config.SUPABASE_URL_ENV: "store_unusable",
            runtime_config.SUPABASE_KEY_ENV: "store_unusable",
            runtime_config.BUCKET_ENV: "store_unusable",
        }
        for name, code in expected.items():
            with self.subTest(missing=name):
                report = runtime_config.evaluate(production_env(**{name: ""}))
                self.assertFalse(report["ready"])
                problems = {p["detail"]: p["code"] for p in report["problems"]}
                self.assertIn(code, problems.values())
                self.assertTrue(any(name in detail for detail in problems))

    def test_plaintext_database_dsn_is_refused(self):
        dsn = SECRET_DSN.replace("?sslmode=require", "")
        report = runtime_config.evaluate(
            production_env(**{runtime_config.PG_DSN_ENV: dsn}))
        self.assertFalse(report["ready"])
        self.assertIn("sslmode=require",
                      "".join(p["detail"] for p in report["problems"]))

    def test_non_postgres_dsn_is_refused(self):
        report = runtime_config.evaluate(production_env(
            **{runtime_config.PG_DSN_ENV: "sqlite:///dev.db?sslmode=require"}))
        self.assertFalse(report["ready"])

    def test_http_supabase_url_is_refused(self):
        report = runtime_config.evaluate(production_env(
            **{runtime_config.SUPABASE_URL_ENV:
               "http://abcdefghijklmno.supabase.co"}))
        self.assertFalse(report["ready"])

    def test_development_store_paths_are_refused_in_production(self):
        for name in runtime_config.DEV_STORE_ENVS:
            with self.subTest(dev_store=name):
                report = runtime_config.evaluate(
                    production_env(**{name: "C:\\dev\\store.db"}))
                self.assertFalse(report["ready"])
                self.assertEqual(report["problems"][0]["code"],
                                 "dev_opt_in_required")
                self.assertIn(name, report["problems"][0]["detail"])

    def test_require_raises_with_the_first_problem_code(self):
        env = production_env(**{runtime_config.OIDC_AUDIENCE_ENV: ""})
        with self.assertRaises(runtime_config.RuntimeConfigError) as caught:
            runtime_config.require(env)
        self.assertEqual(caught.exception.code, "oidc_not_configured")
        self.assertTrue(caught.exception.problems)


class EngineGateTests(unittest.TestCase):
    def test_engine_is_off_by_default(self):
        report = runtime_config.evaluate(production_env(
            **{runtime_config.ENGINE_REAL_ENV: "0"}))
        self.assertTrue(report["ready"])
        self.assertFalse(report["engine"]["opted_in"])
        self.assertFalse(report["engine"]["configured"])

    def test_half_configured_engine_is_refused(self):
        for missing in (runtime_config.GEMINI_KEY_ENV,
                        runtime_config.SCHEME_ENV):
            with self.subTest(missing=missing):
                env = production_env(**{runtime_config.ENGINE_REAL_ENV: "1",
                                        runtime_config.GEMINI_KEY_ENV: SECRET_GEMINI,
                                        runtime_config.SCHEME_ENV: "/app/scheme.pdf"})
                env.pop(missing)
                report = runtime_config.evaluate(env)
                self.assertFalse(report["ready"])
                self.assertIn("model_unavailable",
                              [p["code"] for p in report["problems"]])

    def test_fully_configured_engine_is_reported_as_configured(self):
        report = runtime_config.evaluate(production_env(
            **{runtime_config.ENGINE_REAL_ENV: "1",
               runtime_config.GEMINI_KEY_ENV: SECRET_GEMINI,
               runtime_config.SCHEME_ENV: "/app/scheme.pdf"}))
        self.assertTrue(report["ready"])
        self.assertTrue(report["engine"]["configured"])


class SecretHygieneTests(unittest.TestCase):
    def test_report_never_contains_a_secret_value(self):
        report = runtime_config.evaluate(production_env(
            **{runtime_config.ENGINE_REAL_ENV: "1",
               runtime_config.GEMINI_KEY_ENV: SECRET_GEMINI,
               runtime_config.SCHEME_ENV: "/app/scheme.pdf"}))
        rendered = json.dumps(report, default=str)
        for secret in (SECRET_DSN, SECRET_SUPABASE, SECRET_GEMINI, "s3cr3t"):
            self.assertNotIn(secret, rendered)

    def test_failure_messages_never_contain_a_secret_value(self):
        env = production_env(**{runtime_config.PG_DSN_ENV:
                                SECRET_DSN.replace("sslmode=require", "")})
        with self.assertRaises(runtime_config.RuntimeConfigError) as caught:
            runtime_config.require(env)
        self.assertNotIn("s3cr3t-pg-pass", str(caught.exception))

    def test_secret_names_are_the_expected_three(self):
        self.assertEqual(runtime_config.secret_names(),
                         {runtime_config.PG_DSN_ENV,
                          runtime_config.SUPABASE_KEY_ENV,
                          runtime_config.GEMINI_KEY_ENV})


class BrowserOriginTests(unittest.TestCase):
    """CORS allowlist is part of the production contract, parsed strictly."""

    def test_production_requires_an_origin_allowlist(self):
        report = runtime_config.evaluate(production_env(
            **{runtime_config.ALLOWED_ORIGINS_ENV: None}))
        self.assertFalse(report["ready"])
        problems = [p for p in report["problems"]
                    if p["code"] == "service_unavailable"]
        self.assertTrue(problems)
        self.assertIn(runtime_config.ALLOWED_ORIGINS_ENV,
                      problems[0]["detail"])

    def test_wildcards_paths_and_plain_http_are_refused_in_production(self):
        for value in ("*", "https://marker.pages.dev/path",
                      "http://marker.pages.dev", "https://a.pages.dev,",
                      "https://*.pages.dev"):
            with self.subTest(value=value):
                report = runtime_config.evaluate(production_env(
                    **{runtime_config.ALLOWED_ORIGINS_ENV: value}))
                self.assertFalse(report["ready"])
                self.assertIn("service_unavailable",
                              [p["code"] for p in report["problems"]])

    def test_exact_https_origins_are_lower_cased_and_deduplicated(self):
        parsed = runtime_config.parse_allowed_origins(
            "https://Marker.Pages.dev, https://marker.pages.dev,"
            " https://preview-1.pages.dev:8443", production=True)
        self.assertEqual(parsed, ("https://marker.pages.dev",
                                  "https://preview-1.pages.dev:8443"))

    def test_development_allows_loopback_http_only(self):
        self.assertEqual(
            runtime_config.parse_allowed_origins("http://localhost:5173"),
            ("http://localhost:5173",))
        with self.assertRaises(ValueError):
            runtime_config.parse_allowed_origins("http://marker.pages.dev")

    def test_an_empty_value_is_not_a_wildcard(self):
        self.assertEqual(runtime_config.parse_allowed_origins(""), ())


class ContractDocumentationTests(unittest.TestCase):
    def test_setup_packet_exists(self):
        self.assertTrue(PACKET.is_file(), f"missing {PACKET}")

    def test_packet_documents_every_env_name_in_the_contract(self):
        packet = PACKET.read_text(encoding="utf-8")
        missing = [item.name for item in runtime_config.CONTRACT
                   if item.name not in packet]
        self.assertEqual(missing, [])


class MigrationRunbookTests(unittest.TestCase):
    """The runbook IR reviews must keep the properties it was approved for."""

    RUNBOOK = ROOT / "schema" / "MIGRATIONS.md"

    def _runbook(self) -> str:
        self.assertTrue(self.RUNBOOK.is_file(), f"missing {self.RUNBOOK}")
        return self.RUNBOOK.read_text(encoding="utf-8")

    def test_runbook_names_a_backout_for_the_hardening_step(self):
        text = self._runbook()
        self.assertIn("postgres_v2_tenant_rls_rollback.sql", text)
        self.assertIn("pg_dump", text)
        # Free has no platform backups: the runbook must not imply otherwise.
        self.assertIn("no automatic backups", text)

    def test_runbook_documents_that_force_rls_does_not_bind_privileged_roles(self):
        text = self._runbook()
        self.assertIn("BYPASSRLS", text)
        self.assertIn("superuser", text)

    def test_no_artifact_claims_force_rls_constrains_the_owner_beyond_reality(self):
        # Regression pin for the overclaim IR flagged: FORCE binds the owner but
        # never a superuser/BYPASSRLS role, so this sentence must not come back.
        paths = (self.RUNBOOK, PACKET,
                 ROOT / "schema" / "postgres_v2_tenant_rls.sql")
        for path in paths:
            with self.subTest(path=path.name):
                self.assertNotIn("even the table owner is subject",
                                 path.read_text(encoding="utf-8"))

    def test_storage_cap_is_the_application_limit_not_the_platform_ceiling(self):
        packet = PACKET.read_text(encoding="utf-8")
        self.assertIn("25 MB", packet)
        self.assertIn("platform ceiling", packet)

    def test_runbook_justifies_every_grant_and_refuses_runtime_deletes(self):
        # IR finding: an UPDATE/DELETE grant on audit_events would let a
        # compromised runtime credential rewrite verification history.
        text = self._runbook()
        self.assertIn("Per-grant justification", text)
        self.assertIn("elevated purge role", text)
        self.assertIn("append-only", text)
        self.assertIn("audit_events", text)

    def test_runbook_requires_a_backout_drill_before_any_populated_apply(self):
        # IR: a backout that was never executed is not a backout, and the
        # empty-project shortcut must be proven, not assumed.
        text = self._runbook()
        self.assertIn("Backout drill", text)
        self.assertIn("conditional, never assumed", text)
        self.assertIn("scratch", text)
        drill = text.split("Backout drill", 1)[1]
        for step in ("restore the dump", "apply the forward file",
                     "apply the backout file", "re-apply the forward file"):
            self.assertIn(step, drill)

    def test_migration_grants_no_delete_and_no_audit_update(self):
        sql = (ROOT / "schema" / "postgres_v2_tenant_rls.sql").read_text(
            encoding="utf-8")
        grants = [line.strip() for line in sql.splitlines()
                  if line.strip().upper().startswith("GRANT")]
        self.assertEqual(
            [line for line in grants if "DELETE" in line.upper()], [],
            "a runtime role must not hold DELETE")
        audit_grants = [line for line in grants
                        if "AUDIT_EVENTS" in line.upper()]
        self.assertTrue(audit_grants, "audit_events lost its grant line")
        for line in audit_grants:
            self.assertNotIn("UPDATE", line.upper(),
                             "audit_events must be append-only at runtime")


if __name__ == "__main__":
    unittest.main()
