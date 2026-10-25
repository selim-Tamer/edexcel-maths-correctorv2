"""The shared minimum contract must stay true to BOTH codebases.

This test is the anti-drift gate IFR asked for. It fails when:

* the artifact lists a backend route that does not exist (or omits one that
  does), so neither side can quietly invent an endpoint;
* a live frontend call is dropped from the mapping table;
* a client-owned call grows a server route without review, or a mapped call
  loses its route;
* an error code in the artifact is not a real backend code with the status the
  artifact claims;
* the artifact claims agreement without naming who agreed.

The frontend call names are pinned here on purpose: adding or removing a call
in the client means editing this contract, which is the whole point.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runtime_config
from api.app import _STATUS_BY_CODE, create_app

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "contract" / "minimum_v1.json"
DOC = ROOT / "docs" / "MINIMUM_CONTRACT.md"

# Every method on the live ApiClient, plus the one call the client must add
# (it has no job-creation call today - confirmUpload was expected to start
# marking). A change here is a contract change.
EXPECTED_FRONTEND_CALLS = {
    "createAssignment",
    "authorizeUpload",
    "confirmUpload",
    "createJob",
    "controlJob(retry)",
    "getMarkingQueue",
    "saveDraft",
    "finalizeDraft",
    "getSubmissionResults",
    "recordDecision",
    "getResultEvents",
}

DISPOSITIONS = {"mapped", "replaced", "removed", "client_owned",
                "required_addition"}


def load():
    return json.loads(ARTIFACT.read_text(encoding="utf-8"))


class ContractArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.artifact = load()
        app = create_app(verify_token=lambda token: "subject")
        cls.actual_routes = {
            (method, route.path)
            for route in app.routes
            for method in getattr(route, "methods", set())
            if route.path.startswith("/v1/")
        }

    def test_status_is_explicit_about_agreement(self):
        spec = self.artifact["spec"]
        self.assertIn(spec["status"],
                      {"PROPOSED_AWAITING_FCM_CONFIRMATION", "AGREED"})
        if spec["status"] == "AGREED":
            # Agreement is a message from FCM, not a unilateral edit.
            self.assertTrue(spec["confirmed_by"],
                            "AGREED requires the confirming message id")

    def test_server_routes_match_the_implemented_app_exactly(self):
        listed = {(entry["method"], entry["path"])
                  for entry in self.artifact["server_routes"]
                  if entry["implemented"]}
        self.assertEqual(listed, self.actual_routes)

    def test_every_listed_route_is_flagged_implemented(self):
        for entry in self.artifact["server_routes"]:
            with self.subTest(route=f'{entry["method"]} {entry["path"]}'):
                self.assertIs(entry["implemented"], True,
                              "the beta contract only lists served routes")

    def test_every_frontend_call_is_mapped_once(self):
        names = [entry["client_method"]
                 for entry in self.artifact["frontend_calls"]]
        self.assertEqual(len(names), len(set(names)), "duplicate client call")
        self.assertEqual(set(names), EXPECTED_FRONTEND_CALLS)

    def test_dispositions_are_valid_and_route_references_resolve(self):
        listed = {(entry["method"], entry["path"])
                  for entry in self.artifact["server_routes"]}
        for entry in self.artifact["frontend_calls"]:
            with self.subTest(call=entry["client_method"]):
                self.assertIn(entry["disposition"], DISPOSITIONS)
                self.assertTrue(entry["justification"].strip())
                route = entry["server_route"]
                if entry["disposition"] in ("client_owned", "removed"):
                    self.assertIsNone(
                        route,
                        "client-owned state must not grow a server route")
                else:
                    self.assertIsNotNone(route)
                    self.assertIn((route["method"], route["path"]), listed)

    def test_client_owned_names_the_replacement_when_one_exists(self):
        # authorizeUpload is the only "replaced" call: it must point at the
        # upload route that takes its place, otherwise the client has no path.
        replaced = [entry for entry in self.artifact["frontend_calls"]
                    if entry["disposition"] == "replaced"]
        self.assertEqual([entry["client_method"] for entry in replaced],
                         ["authorizeUpload"])
        self.assertEqual(replaced[0]["server_route"]["path"], "/v1/documents")

    def test_error_codes_are_real_backend_codes_with_the_claimed_status(self):
        errors = self.artifact["error_contract"]
        expected = {}
        for status, codes in errors["auth"].items():
            if status == "rule":
                continue
            for code in codes:
                expected[code] = int(status)
        for code in errors["conflict_codes"]:
            expected[code] = 409
        for code in errors["not_found_codes"]:
            expected[code] = 404
        for code, status in expected.items():
            with self.subTest(code=code):
                self.assertIn(code, _STATUS_BY_CODE)
                self.assertEqual(_STATUS_BY_CODE[code], status)

    def test_runtime_codes_are_not_claimed_as_http_codes(self):
        errors = self.artifact["error_contract"]
        self.assertNotIn("unconfigured", errors)
        sources = "\n".join(
            (ROOT / name).read_text(encoding="utf-8")
            for name in ("runtime_config.py", "api/app.py"))
        for code in errors["runtime_codes"]["values"]:
            with self.subTest(code=code):
                self.assertIn(code, sources)
        # These two exist as startup/job codes only, never as envelope statuses.
        for code in ("oidc_not_configured", "service_unavailable"):
            self.assertNotIn(code, _STATUS_BY_CODE)

    def test_question_number_contract_is_string_with_integer_tolerance(self):
        identity = self.artifact["identity"]
        self.assertEqual(identity["question_number_wire_type"], "string")
        self.assertEqual(set(identity["question_number_backend_accepts"]),
                         {"string", "integer"})

    def test_tenant_403_is_not_described_as_an_expired_session(self):
        rule = self.artifact["error_contract"]["auth"]["rule"]
        self.assertIn("tenant_membership_required", rule)
        self.assertIn("NOT an expired session", rule)

    def test_cors_contract_matches_the_runtime_contract(self):
        cors = self.artifact["cors"]
        self.assertEqual(cors["env"], runtime_config.ALLOWED_ORIGINS_ENV)
        self.assertIs(cors["wildcard"], False)
        self.assertIs(cors["credentials"], False)
        self.assertEqual(cors["production_scheme"], "https only")

    def test_retry_does_not_regenerate_the_job_key(self):
        idempotency = self.artifact["idempotency"]
        self.assertIn("sha256", idempotency["job_key_rule"])
        self.assertIn("document_digest",
                      idempotency["regenerate_only_when"])
        self.assertIn("no new key", idempotency["retry_rule"])

    def test_the_doc_exists_and_carries_the_same_rules(self):
        self.assertTrue(DOC.is_file(), f"missing {DOC}")
        text = DOC.read_text(encoding="utf-8")
        for phrase in ("tenant_membership_required", "stale_revision",
                       "no wildcard", "25 MiB", "question_number"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
