"""C6-B6 independent attacks: the bearer-identity boundary of api/app.py.

These tests probe what CM's own API tests do not: the VERIFIER SEAM itself
and every way identity could arrive at the decision path. Honest framing
(Mr.G: "don't accept injected identity as proof of a real verifier"): the
production OIDC/JWKS verifier is NOT implemented — ``create_app`` takes a
caller-injected ``verify_token``. There is therefore no product code here
that parses signed tokens, and no test here claims otherwise. What IS
product behavior and gets pinned:

  - the documented default (a reject-everything verifier) denies every
    protected route; nothing is readable/writable without the seam;
  - authentication is decided BEFORE any body is parsed or any store is
    touched (attacker learns nothing about validation internals);
  - only a non-empty string return value from the verifier can ever
    produce identity; garbage returns, blanks and verifier crashes all
    deny with ZERO store mutation (the crash is a 500, not a success —
    recorded as finding F-1 for CM: the contract names no envelope for a
    verifier exception; the API must not pretend it is 401);
  - the identity can only come from the verifier: body fields, query
    parameters and duplicate headers never override or merge it;
  - the token never echoes: not in responses, not in error bodies, not in
    the durable store bytes;
  - revocation/rotation semantics as supported by the seam: a token the
    verifier stops accepting can neither read nor write anything further,
    and the SAME token re-accepted under a DIFFERENT subject cannot
    hijack the previous owner's owner-scoped stream;
  - the public surface stays minimal (healthz only; docs/openapi disabled)
    and a static guard fails this suite if a JWT/cookie verifier is ever
    shipped inside ``api/`` without this seam being re-attacked.

Offline, in-process, synthetic only: temporary SQLite, fake subjects.
"""
import hashlib
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import audit_store
import decision_core
from api.app import create_app
from fastapi.testclient import TestClient

SUBJECT = "alice-subject"
OWNER = "google:" + SUBJECT
PDF = b"%PDF-1.7 c6b6 verifier boundary fixture"
DIGEST = hashlib.sha256(PDF).hexdigest()
TOKEN_CANARY = "tok-canary-never-echo-9d3f"


def question(number="1", awarded=1):
    return {"question_number": number, "awarded_marks": awarded,
            "max_marks": 2, "student_answer": "synthetic answer",
            "working_summary": "34 + 8", "scheme_answer_evidence": "s",
            "full_mark_solution": "42", "mark_breakdown": [],
            "confidence": "High", "loss_reason": "", "correct_method": "m"}


def manual_row(owner=OWNER, review_key="review-1"):
    questions = [question()]
    return {"status": "Marked", "review_key": review_key,
            "report_owner": owner, "snapshot_context": DIGEST,
            "revision": "", "selected_questions": ["1"],
            "ai_result": {"questions": [dict(q) for q in questions]},
            "result": {"questions": [dict(q) for q in questions]},
            "student_name": "Synthetic", "marking_model": "fake",
            "audit_model": "Not run"}


RESULT_ID = decision_core.durable_result_identity(manual_row(), "")


def decision_body(**overrides):
    body = {"question_number": "1", "action": "approve",
            "expected_revision": 0, "idempotency_key": "k-1"}
    body.update(overrides)
    return body


class VerifierSeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "verifier.db")
        self.store = audit_store.open_store(self.path, dev_only_ack=True)
        self.addCleanup(self.store.close)
        self.rows = {OWNER: [manual_row()]}

    def app(self, verify_token, provenance_ok=True):
        built = create_app(
            verify_token=verify_token,
            rows_for=lambda subject, owner: list(self.rows.get(owner, [])),
            store_provider=lambda: (self.store, None),
            manual_document_provenance_ok=(
                (lambda row: True) if provenance_ok else
                (lambda row: False)))
        return TestClient(built, raise_server_exceptions=False)

    def assert_denied_everywhere(self, client):
        """The seam is the ONLY way in: both protected routes refuse."""
        wrong = {"Authorization": "Bearer some-token"}
        self.assertEqual(
            client.post(f"/v1/results/{RESULT_ID}/decisions",
                        headers=wrong, json=decision_body()).status_code, 401)
        self.assertEqual(
            client.get(f"/v1/results/{RESULT_ID}/events",
                       headers=wrong).status_code, 401)

    def test_reject_all_verifier_is_the_documented_inert_default(self):
        # The docstring contract: with no verifier configured, every
        # authenticated endpoint returns 401. This is the security property
        # the injected seam must NEVER violate.
        client = self.app(lambda token: None)
        self.assert_denied_everywhere(client)
        self.assertIsNone(self.store.current_revision(
            owner=OWNER, result_id=RESULT_ID))
        self.assertEqual(
            client.get("/v1/healthz").status_code, 200,
            "health stays public and content-free")
        self.assertEqual(client.get("/v1/healthz").json(), {"status": "ok"})

    def test_authentication_precedes_body_validation_and_store_access(self):
        # An attacker sending a garbage body with a rejected token gets 401,
        # not a validation error or a store touch: the seam reveals nothing
        # about internals before identity exists.
        client = self.app(lambda token: None)
        response = client.post(f"/v1/results/{RESULT_ID}/decisions",
                               headers={"Authorization": "Bearer x"},
                               json={"junk": True})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"],
                         "authentication_required")
        self.assertIsNone(self.store.current_revision(
            owner=OWNER, result_id=RESULT_ID))

    def test_garbage_verifier_returns_never_become_identity(self):
        # The seam contract says subject-or-None; a sloppy verifier leaking
        # wrong types must not authenticate. (int is truthy, dict/list
        # truthy-empty etc.)
        for garbage in (123, 0, True, False, {}, [], {"sub": SUBJECT},
                        ["x"], "", "   ", "\t\n"):
            with self.subTest(return_value=repr(garbage)):
                client = self.app(lambda token, value=garbage: value)
                self.assert_denied_everywhere(client)
        self.assertIsNone(self.store.current_revision(
            owner=OWNER, result_id=RESULT_ID))

    def test_verifier_exception_denies_without_any_mutation(self):
        # Finding F-1 (recorded, not papered over): a verifier crash
        # surfaces as a raw 500, not the contract's envelope. It is still a
        # DENIAL — no decision path runs, no registration, no event — and
        # the token never appears in the response. CM owes the contract an
        # explicit verifier-failure code; this test pins the SECURITY
        # outcome regardless of which code is later chosen, and would fail
        # if a crash ever became a success.
        exploded = "verifier-exploded-canary"

        def boom(token):
            raise ValueError(exploded)

        client = self.app(boom)
        headers = {"Authorization": f"Bearer {TOKEN_CANARY}"}
        for response in (
                client.get(f"/v1/results/{RESULT_ID}/events", headers=headers),
                client.post(f"/v1/results/{RESULT_ID}/decisions",
                            headers=headers, json=decision_body())):
            self.assertGreaterEqual(response.status_code, 400)
            self.assertNotEqual(response.status_code, 200)
            self.assertNotIn(TOKEN_CANARY, response.text)
            self.assertNotIn(exploded, response.text)
        self.assertIsNone(self.store.current_revision(
            owner=OWNER, result_id=RESULT_ID))

    def test_verifier_receives_only_the_trimmed_token(self):
        seen = []

        def spy(token):
            seen.append(token)
            return None

        client = self.app(spy)
        headers = {"Authorization": f"Bearer  {TOKEN_CANARY}  "}
        self.assertEqual(
            client.get(f"/v1/results/{RESULT_ID}/events",
                       headers=headers).status_code, 401)
        self.assertEqual(seen, [TOKEN_CANARY])

    def test_scheme_case_and_duplicate_headers_never_merge_or_override(self):
        accepted = []

        def verifier(token):
            accepted.append(token)
            return SUBJECT if token == "tok-alice" else None

        client = self.app(verifier)
        # lowercase scheme is RFC-7235 case-insensitive and accepted; the
        # identity still comes solely from the verifier return value.
        response = client.get(f"/v1/results/{RESULT_ID}/events",
                              headers={"Authorization": "bearer tok-alice"})
        self.assertIn(response.status_code, (200, 404))
        # A duplicated Authorization header must authenticate at most ONE
        # subject (first-wins); the second value never leaks into the
        # verifier as a merged token and never adds a second identity.
        accepted.clear()
        client.get(f"/v1/results/{RESULT_ID}/events",
                   headers=[("Authorization", "Bearer tok-alice"),
                            ("Authorization", "Bearer tok-evil")])
        self.assertTrue(accepted)
        for token in accepted:
            self.assertNotIn(",", token)

    def test_body_and_query_cannot_spoof_identity(self):
        # Every spoofable-looking input: report_owner/owner/actor/subject in
        # the body, subject/owner in the query. None may alter who the
        # decision is attributed to; the store actor stays the token's
        # server-derived owner.
        client = self.app({"tok": SUBJECT}.get)
        headers = {"Authorization": "Bearer tok"}
        response = client.post(
            f"/v1/results/{RESULT_ID}/decisions?subject=attacker"
            f"&owner=google:attacker",
            headers=headers,
            json=decision_body(report_owner="google:attacker",
                               owner="google:attacker",
                               actor="attacker", subject="attacker"))
        self.assertEqual(response.status_code, 200, response.text)
        events = self.store.events(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], OWNER)
        # The attacker's own owner scope stays empty: the spoof wrote
        # NOTHING there.
        self.assertIsNone(self.store.current_revision(
            owner="google:attacker", result_id=RESULT_ID))

    def test_token_is_never_echoed_or_persisted(self):
        client = self.app({"tok": SUBJECT}.get)
        headers = {"Authorization": f"Bearer {TOKEN_CANARY}"}
        ok = client.post(f"/v1/results/{RESULT_ID}/decisions", headers={
            "Authorization": "Bearer tok"}, json=decision_body())
        self.assertEqual(ok.status_code, 200)
        bad = client.get(f"/v1/results/{RESULT_ID}/events", headers=headers)
        self.assertEqual(bad.status_code, 401)
        self.assertNotIn(TOKEN_CANARY, bad.text)
        blob = b""
        for name in sorted(__import__("os").listdir(self.tmp.name)):
            with open(Path(self.tmp.name) / name, "rb") as handle:
                blob += handle.read()
        self.assertNotIn(TOKEN_CANARY.encode(), blob)

    def test_revoked_or_rotated_token_cannot_reach_the_stream_anymore(self):
        # Seam-level revocation semantics: the verifier is the single source
        # of truth per request. Once it stops accepting the token, no
        # further read OR write is possible — even a previously valid
        # idempotent replay body is denied. There is no session/cache in
        # the API to invalidate, and this proves it.
        live = {"value": True}

        def verifier(token):
            return SUBJECT if token == "tok" and live["value"] else None

        client = self.app(verifier)
        headers = {"Authorization": "Bearer tok"}
        self.assertEqual(
            client.post(f"/v1/results/{RESULT_ID}/decisions", headers=headers,
                        json=decision_body()).status_code, 200)
        live["value"] = False  # expiry / logout / kid retired server-side
        self.assertEqual(
            client.get(f"/v1/results/{RESULT_ID}/events",
                       headers=headers).status_code, 401)
        replay = client.post(f"/v1/results/{RESULT_ID}/decisions",
                             headers=headers, json=decision_body())
        self.assertEqual(replay.status_code, 401)
        # The stream holds exactly the one pre-revocation event.
        events = self.store.events(owner=OWNER, result_id=RESULT_ID)
        self.assertEqual(len(events), 1)

    def test_reissued_token_for_a_different_subject_hijacks_nothing(self):
        # Rotation hazard: the same bearer string re-accepted under a NEW
        # subject must not see or extend the previous owner's stream —
        # ownership is derived from the verifier's RETURN VALUE only.
        mapping = {"value": SUBJECT}

        def verifier(token):
            return mapping["value"] if token == "tok" else None

        client = self.app(verifier)
        headers = {"Authorization": "Bearer tok"}
        self.assertEqual(
            client.post(f"/v1/results/{RESULT_ID}/decisions", headers=headers,
                        json=decision_body()).status_code, 200)
        mapping["value"] = "bob-subject"  # token now yields another subject
        stolen = client.get(f"/v1/results/{RESULT_ID}/events",
                            headers=headers)
        self.assertEqual(stolen.status_code, 404)
        self.assertEqual(stolen.json()["error"]["code"],
                         "result_not_registered")
        self.rows["google:bob-subject"] = [manual_row(owner="google:bob-subject",
                                                      review_key="bob-1")]
        forged = client.post(
            f"/v1/results/{RESULT_ID}/decisions", headers=headers,
            json=decision_body(expected_revision=1))
        self.assertEqual(forged.status_code, 404,
                         "bob's visible rows carry no result_id matching "
                         "alice's stream")
        self.assertEqual(len(self.store.events(owner=OWNER,
                                               result_id=RESULT_ID)), 1)

    def test_schema_and_docs_surface_is_disabled(self):
        client = self.app(lambda token: None)
        for route in ("/docs", "/openapi.json", "/redoc"):
            self.assertEqual(client.get(route).status_code, 404, route)

    def test_api_package_ships_no_local_token_parser(self):
        # Missing-behavior guard (Mr.G: real verifier is a future CM slice).
        # Identity in this app may ONLY come through the injected seam. If
        # a JWT/JWKS/cookie verifier is ever added inside api/, this test
        # must be replaced by seam-parsing attacks on that verifier, not
        # silently deleted: shipping a verifier without extending this
        # suite is the failure mode being prevented.
        api_dir = Path(__file__).resolve().parents[1] / "api"
        offenders = []
        pattern = re.compile(
            r"\b(jwt|jose|jwks|authlib|itsdangerous|signed_secret|cookie|"
            r"set_session)\b", re.IGNORECASE)
        for file in sorted(api_dir.glob("*.py")):
            text = file.read_text(encoding="utf-8")
            for line in text.splitlines():
                if line.strip().startswith("#"):
                    continue
                if pattern.search(line) and "import" in line:
                    offenders.append("%s: %s" % (file.name, line.strip()))
        self.assertEqual(offenders, [],
                         "api/ must not grow a local token verifier before "
                         "the verifier attack suite is extended to cover it")


if __name__ == "__main__":
    unittest.main()
