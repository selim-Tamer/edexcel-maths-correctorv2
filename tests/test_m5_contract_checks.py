"""M5 synthetic contract checks (offline, stdlib only, no network).

These tests do NOT run a FastAPI server -- no FastAPI dependency is added by
this milestone. They pin the *specification* in ``m5_api_contract.json`` to
the real source vocabulary so the contract cannot silently drift from what the
application and the C6-B3 store actually emit.

What is checked:
  1. the contract parses and is explicitly marked SPEC_NOT_IMPLEMENTED;
  2. every failure code the contract names exists in the real source;
  3. no invented grade-publication endpoint appears;
  4. the tenant/identity and separation docs exist and stay labelled provisional;
  5. the atomicity invariant is stated on the decision endpoint.

Run: python -m unittest tests.test_m5_contract_checks -v
"""
from __future__ import annotations

import json
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
CONTRACT_PATH = REPO / "m5_api_contract.json"


def _load_contract() -> dict:
    with CONTRACT_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _source(path: str) -> str:
    return (REPO / path).read_text(encoding="utf-8")


def _store_codes() -> set[str]:
    return set(re.findall(r'AuditStoreError\(\s*"([a-z_]+)"',
                          _source("audit_store.py")))


def _app_audit_codes() -> set[str]:
    return set(re.findall(r'"(audit_[a-z_]+)"',
                          _source("app5_1_automated.py")))


def _question_review_codes() -> set[str]:
    return set(re.findall(r'"(review_[a-z_]+)"',
                          _source("question_review.py")))


def _auth_codes() -> set[str]:
    return set(re.findall(r'"([a-z_]+)"',
                          _source("teacher_authorization.py")))


def _known_codes() -> set[str]:
    """Every failure code the real source can actually emit."""
    return (_store_codes() | _app_audit_codes()
            | _question_review_codes() | _auth_codes())


class ContractParseTests(unittest.TestCase):
    def test_contract_file_exists_and_parses(self):
        self.assertTrue(CONTRACT_PATH.is_file(),
                        "m5_api_contract.json must ship with the repo")
        contract = _load_contract()
        self.assertIsInstance(contract, dict)

    def test_contract_is_labelled_as_unimplemented_spec(self):
        spec = _load_contract()["spec"]
        self.assertEqual(spec["status"], "SPEC_NOT_IMPLEMENTED",
                         "the contract must never claim to be implemented")
        self.assertFalse(spec["provisioned"])
        self.assertFalse(spec["deployed"])

    def test_no_fastapi_dependency_was_added(self):
        """This milestone is a contract only: FastAPI must not enter the app manifest."""
        requirements = _source("requirements.txt")
        self.assertNotIn("fastapi", requirements.lower(),
                         "requirements.txt must stay free of FastAPI at M5")


class FailureCodeFidelityTests(unittest.TestCase):
    """The contract may only name codes the real source emits."""

    def setUp(self):
        self.contract = _load_contract()
        self.known = _known_codes()

    def test_every_endpoint_error_code_exists_in_source(self):
        for endpoint in self.contract["endpoints"]:
            for code in endpoint.get("error_codes", []):
                with self.subTest(endpoint=endpoint["path"], code=code):
                    self.assertIn(
                        code, self.known,
                        f"contract names '{code}' but no source module emits it")

    def test_store_codes_are_a_subset_of_real_store_codes(self):
        real = _store_codes()
        self.assertIn("stale_revision", real)
        self.assertIn("stream_sequence_conflict", real)
        self.assertIn("idempotency_key_conflict", real)
        self.assertIn("idempotency_key_payload_mismatch", real)
        self.assertIn("original_output_immutable", real)
        self.assertIn("dev_opt_in_required", real)

    def test_optimistic_concurrency_codes_are_present(self):
        codes = {c for e in self.contract["endpoints"]
                 for c in e.get("error_codes", [])}
        for expected in ("stale_revision", "stream_sequence_conflict",
                         "idempotency_key_conflict",
                         "idempotency_key_payload_mismatch"):
            self.assertIn(expected, codes,
                          "the decision contract must expose the optimistic-"
                          "concurrency and idempotency codes")


class BoundaryAndInvariantTests(unittest.TestCase):

    def test_decision_endpoint_declares_atomicity_invariant(self):
        contract = _load_contract()
        decision = next(e for e in contract["endpoints"]
                        if e["path"].endswith("/decisions"))
        self.assertIn("atomicity_invariant", decision)
        self.assertIn("No successful correction is persisted if its audit "
                      "write fails", decision["atomicity_invariant"])

    def test_no_grade_publication_endpoint_exists(self):
        """Honest inventory: the app performs zero mutating Classroom/Drive calls."""
        contract = _load_contract()
        for endpoint in contract["endpoints"]:
            self.assertNotIn("publish", endpoint["path"],
                             "no publication endpoint may be invented")
            self.assertNotIn("grade", endpoint["path"],
                             "no grade-send endpoint may be invented")
        self.assertIn("grade_publication", contract["absent_by_design"])

    def test_signoff_is_the_single_approval_gate(self):
        contract = _load_contract()
        approvals = [e for e in contract["endpoints"]
                     if e["path"].endswith("/approvals")]
        self.assertEqual(len(approvals), 1,
                         "exactly one sign-off endpoint may exist")
        self.assertTrue(approvals[0]["auth_required"])

    def test_all_data_endpoints_require_auth_and_tenant_scope(self):
        contract = _load_contract()
        for endpoint in contract["endpoints"]:
            if endpoint["path"] == "/v1/healthz":
                continue
            with self.subTest(path=endpoint["path"]):
                self.assertTrue(endpoint["auth_required"])
                self.assertTrue(endpoint["tenant_scoped"])

    def test_actor_is_never_the_tenant(self):
        auth = _load_contract()["auth"]
        self.assertTrue(auth["actor_is_not_tenant"])


class DocumentationLabelTests(unittest.TestCase):
    """Specs must stay clearly separated from implemented behavior."""

    def test_tenant_schema_exists_and_is_labelled_provisional(self):
        path = REPO / "M5_TENANT_IDENTITY_SCHEMA.md"
        self.assertTrue(path.is_file())
        text = path.read_text(encoding="utf-8")
        self.assertIn("SPEC / PROPOSAL", text)
        self.assertIn("nothing implemented, nothing provisioned", text)
        self.assertIn("UNDECIDED", text,
                      "retention/legal policy must be marked undecided, not decided")

    def test_separation_analysis_exists_and_names_the_real_gap(self):
        path = REPO / "M5_SESSION_STATE_SEPARATION.md"
        self.assertTrue(path.is_file())
        # Prose is line-wrapped, so collapse whitespace before substring checks.
        flat = " ".join(path.read_text(encoding="utf-8").split())
        self.assertIn("does **not** make the application API-ready", flat)
        self.assertIn("question_review_evidence_digest", flat)
        self.assertIn("decision_session_snapshot", flat)
        self.assertIn("apply_question_review_decision", flat)

    def test_separation_analysis_measured_claims_match_source(self):
        """The named session-coupled functions must actually be session-coupled."""
        app = _source("app5_1_automated.py").splitlines()

        def body_of(name):
            start = next(i for i, line in enumerate(app)
                         if re.match(r"\s*def " + name + r"\(", line))
            end = len(app)
            for j in range(start + 1, len(app)):
                if re.match(r"def |class ", app[j]):
                    end = j
                    break
            return app[start:end]

        # These three are claimed session-coupled; each must touch st.
        for name in ("decision_session_snapshot",
                     "apply_question_review_decision",
                     "question_review_evidence_digest"):
            with self.subTest(function=name):
                body = body_of(name)
                self.assertTrue(any("st.session_state" in line for line in body),
                                f"{name} is documented as session-coupled but "
                                "its body does not reference st.session_state")

        # These are claimed pure; none may touch st.
        for name in ("durable_result_identity", "audit_persistence_plan",
                     "commit_audit_decision"):
            with self.subTest(function=name):
                body = body_of(name)
                live = [line for line in body
                        if "st.session_state" in line]
                self.assertEqual(live, [],
                                 f"{name} is documented as pure but references "
                                 "st.session_state")


if __name__ == "__main__":
    unittest.main()
