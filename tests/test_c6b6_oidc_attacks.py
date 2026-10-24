"""C6-B6 independent attacks: the REAL OIDC verifier now shipped at the repo
root (``auth_oidc.py``) and wired as ``create_app``'s default.

Why this file exists. CM's ``tests/test_auth_oidc_verifier.py`` exercises
``OidcVerifier`` as a UNIT (its own harness, its own keys). That proves the
verifier's internals; it does NOT prove that the running API actually uses it,
nor what an attacker sees at the HTTP boundary once real signed tokens are in
play. This suite attacks the OTHER side:

  - the default verifier is the real one (a stub or an injected fake must
    never be able to stand in for it silently);
  - every signed-token failure class an attacker can produce is answered with
    a STATIC 401 detail and zero mutation, over the real ``actor`` dependency;
  - identity can only come from the verified token: body, query and duplicate
    headers stay inert even when the caller is genuinely authenticated;
  - a token signed for one subject can never reach another subject's stream
    (account-switch / re-issued-token hijack), and a retired signing key's
    token dies with it;
  - the JWKS transport is pinned and rate-bounded: a hostile stream of unknown
    kids must not become a request flood, and an attacker-chosen ``jku``
    header must never redirect key resolution;
  - membership is enforced AFTER a real verification and can never mint an
    owner: a bogus membership object cannot move a decision into another
    tenant's stream;
  - the token is never echoed - not in an error body, not on disk;
  - finding F-1 (verifier crash = raw 500) is now closed as 401
    ``token_verification_failed`` and is pinned so it cannot regress.

Honest limits, stated up front:
  * Offline and synthetic only: local RSA keys, a controlled JWKS transport
    patched in as ``auth_oidc._fetch_jwks``. No network, no Google, no real
    identity, no real student data.
  * Replay of a stolen-but-unexpired token is NOT detectable statelessly by
    design (``auth_oidc`` docstring); this suite pins that the token still
    cannot escape its own owner scope, and does not claim replay detection.
  * Kid rotation: the rotated kid is picked up by ONE bounded forced
    refresh, and the retired kid dies exactly when that forced refresh
    replaces the cached keys (pinned in the rotation test with the JWKS
    call count as evidence). The 300 s TTL is a deliberate availability
    tradeoff: until a refresh actually happens, a cached-but-retired key
    still verifies - documented here as an honest limit, not papered over.
  * ``revocation_checker`` is not wired by ``create_app``; revocation is
    therefore tested as "verifier stops accepting", not introspection.
"""
import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auth_oidc
import audit_store
import decision_core
from api.app import create_app
from fastapi.testclient import TestClient

AUDIENCE = "synthetic-audience.apps.googleusercontent.com"
GOOGLE_ISSUER = "https://accounts.google.com"
TOKEN_CANARY = "tok-canary-real-verifier-5b71"

SUBJECT_A = "subject-alpha-1111"
SUBJECT_B = "subject-beta-2222"
OWNER_A = "google:" + SUBJECT_A
OWNER_B = "google:" + SUBJECT_B

PDF = b"%PDF-1.7 c6b6 real verifier fixture"
DIGEST = hashlib.sha256(PDF).hexdigest()


# --- key material -------------------------------------------------------

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _jwk_for(private_key, kid):
    """The public JWK for a private key, as a verifier would publish it."""
    from jwt.algorithms import RSAAlgorithm

    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return jwk


def _jwks_doc(*pairs):
    return {"keys": [_jwk_for(key, kid) for key, kid in pairs]}


def _sign(private_key, kid, claims, *, alg="RS256", headers=None,
          key=None):
    header = {"kid": kid, "alg": alg, "typ": "JWT"}
    if headers:
        header.update(headers)
    return auth_oidc._pyjwt.encode(
        claims, key if key is not None else private_key,
        algorithm=alg, headers=header)


def _now():
    import time

    return int(time.time())


def _claims(sub=SUBJECT_A, **overrides):
    """Claims for a token that is valid RIGHT NOW - the verifier uses the
    real wall clock (``create_app`` cannot inject one), so every fixture is
    derived from it rather than hard-coded."""
    issued = _now() - 10
    claims = {
        "iss": GOOGLE_ISSUER,
        "aud": AUDIENCE,
        "sub": sub,
        "iat": issued,
        "exp": issued + 600,
        "azp": AUDIENCE,
        "email": "synthetic@example.invalid",
    }
    claims.update(overrides)
    return claims


def _expired_claims(**overrides):
    return _claims(exp=_now() - 3600, **overrides)


def _immature_claims(**overrides):
    future = _now() + 3600
    return _claims(iat=future, exp=future + 600, **overrides)


def _handcrafted(header: dict, payload: dict, signature: str = "") -> str:
    """A token assembled without PyJWT - the attacker's own encoder."""
    return ".".join((_b64url(json.dumps(header).encode()),
                     _b64url(json.dumps(payload).encode()),
                     signature))


def _tamper(token: str, payload: dict) -> str:
    """Keep a valid signature, replace the payload: forgery attempt."""
    header, _, signature = token.split(".")
    return ".".join((header, _b64url(json.dumps(payload).encode()),
                     signature))


def _hs256_token(header: dict, payload: dict, secret: bytes) -> str:
    """A hand-crafted HS256 JWS, the way a real attacker builds it.

    PyJWT 2.15.1 refuses to ENCODE a JWK or an asymmetric PEM as an HMAC
    secret (observed in this suite's earlier runs), so the real confusion
    attacker does not use PyJWT: HMAC-SHA256 over the b64url(header) + '.' +
    b64url(payload) signing input with the RSA public key PEM as the secret.
    """
    import hashlib
    import hmac

    signing_input = ".".join((_b64url(json.dumps(header).encode()),
                              _b64url(json.dumps(payload).encode())))
    signature = hmac.new(secret, signing_input.encode("ascii"),
                         hashlib.sha256).digest()
    return signing_input + "." + _b64url(signature)


class ControlledJwks:
    """A deterministic JWKS transport that records every call.

    Doubles as the rotation switch (``serve``) and as the outage switch
    (``fail``), so attacks never touch the network.
    """

    def __init__(self, documents):
        self.documents = list(documents)
        self.calls = 0
        self.urls = []
        self.fail = None

    def __call__(self, url, timeout):
        self.calls += 1
        self.urls.append(url)
        if self.fail is not None:
            raise self.fail
        return self.documents[min(self.index, len(self.documents) - 1)]

    index = 0

    def rotate_to(self, index):
        self.index = index


def question(number="1", awarded=1):
    return {"question_number": number, "awarded_marks": awarded,
            "max_marks": 2, "student_answer": "synthetic answer",
            "working_summary": "34 + 8", "scheme_answer_evidence": "s",
            "full_mark_solution": "42", "mark_breakdown": [],
            "confidence": "High", "loss_reason": "", "correct_method": "m"}


def manual_row(owner, review_key="review-1"):
    questions = [question()]
    return {"status": "Marked", "review_key": review_key,
            "report_owner": owner, "snapshot_context": DIGEST,
            "revision": "", "selected_questions": ["1"],
            "ai_result": {"questions": [dict(q) for q in questions]},
            "result": {"questions": [dict(q) for q in questions]},
            "student_name": "Synthetic", "marking_model": "fake",
            "audit_model": "Not run"}


def decision_body(**overrides):
    body = {"question_number": "1", "action": "approve",
            "expected_revision": 0, "idempotency_key": "k-1"}
    body.update(overrides)
    return body


class RealVerifierAttackTests(unittest.TestCase):
    """Attacks driven through ``create_app(verify_token=None)`` - i.e. through
    the verifier the product actually ships, not an injected stand-in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = audit_store.open_store(
            str(Path(self.tmp.name) / "real.db"), dev_only_ack=True)
        self.addCleanup(self.store.close)
        self.rows = {}
        self.membership_calls = []
        self.membership_result = _ActiveMembership()
        self.primary = _RSA_KEY_A
        self.primary_kid = "kid-primary"
        self.jwks = ControlledJwks([_jwks_doc((self.primary,
                                               self.primary_kid))])

    # -- harness ---------------------------------------------------------

    def client(self, *, membership=None, store=None, provenance_ok=True):
        """Build the app with the REAL default verifier and a controlled
        JWKS transport, exactly as production would build it - the only
        patched thing is the network fetch."""
        resolver = membership if membership is not None else self._resolve
        patcher = mock.patch.object(auth_oidc, "_fetch_jwks", self.jwks)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ,
                              {"GOOGLE_OIDC_AUDIENCE": AUDIENCE})
        env.start()
        self.addCleanup(env.stop)
        app = create_app(
            verify_token=None,
            rows_for=lambda subject, owner: list(self.rows.get(owner, [])),
            store_provider=lambda: (store or self.store, None),
            manual_document_provenance_ok=(
                (lambda row: True) if provenance_ok else (lambda row: False)),
            membership_resolver=resolver)
        self.addCleanup(getattr(resolver, "close", lambda: None))
        return TestClient(app, raise_server_exceptions=False)

    def _resolve(self, subject):
        self.membership_calls.append(subject)
        if isinstance(self.membership_result, Exception):
            raise self.membership_result
        return self.membership_result

    def token(self, **overrides):
        return _sign(self.primary, self.primary_kid, _claims(**overrides))

    def bearer(self, token):
        return {"Authorization": f"Bearer {token}"}

    def seed_alpha(self):
        self.rows[OWNER_A] = [manual_row(OWNER_A)]
        return decision_core.durable_result_identity(self.rows[OWNER_A][0], "")

    def revision(self, owner=OWNER_A, result_id=None):
        return self.store.current_revision(owner=owner, result_id=result_id)

    def assert_no_mutation(self, result_id):
        self.assertIsNone(self.revision(result_id=result_id),
                          "a denied request must not create a stream")
        try:
            events = self.store.events(owner=OWNER_A, result_id=result_id)
        except audit_store.AuditStoreError:
            # An unregistered owner stream reads as "nothing exists" - which
            # is exactly the no-mutation proof this helper wants.
            events = []
        self.assertEqual(events, [], "a denied request must not write events")

    # -- the default verifier is the real one ----------------------------

    def test_default_verifier_is_the_real_oidc_verifier_and_it_accepts_a_signed_token(self):
        # Positive control for everything below: with NO injected verifier,
        # the app builds auth_oidc.OidcVerifier and a correctly signed token
        # authenticates. Without this the rest of the file proves nothing.
        result_id = self.seed_alpha()
        client = self.client()
        response = client.post(
            f"/v1/results/{result_id}/decisions",
            headers=self.bearer(self.token()), json=decision_body())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result_revision"], 1)
        self.assertFalse(response.json()["replayed"])
        self.assertEqual(
            len(self.store.events(owner=OWNER_A, result_id=result_id)), 1,
            "the decision must land in the token-derived owner's stream")
        self.assertEqual(self.jwks.urls, [auth_oidc.GOOGLE_JWKS_URL],
                         "key resolution must use the pinned Google JWKS URL")

    def test_no_audience_configured_means_every_protected_call_fails_closed(self):
        # The real verifier refuses to be built without an audience: no
        # half-configured production path can authenticate anybody.
        result_id = self.seed_alpha()
        patcher = mock.patch.object(auth_oidc, "_fetch_jwks", self.jwks)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("GOOGLE_OIDC_AUDIENCE", None)
        unconfigured = create_app(
            verify_token=None,
            rows_for=lambda subject, owner: list(self.rows.get(owner, [])),
            store_provider=lambda: (self.store, None),
            membership_resolver=self._resolve)
        client = TestClient(unconfigured, raise_server_exceptions=False)
        for route in (f"/v1/results/{result_id}/decisions",
                      f"/v1/results/{result_id}/events"):
            response = (client.post(route, headers=self.bearer(self.token()),
                                    json=decision_body())
                        if route.endswith("decisions") else
                        client.get(route, headers=self.bearer(self.token())))
            self.assertEqual(response.status_code, 401, response.text)
            self.assertEqual(response.json()["error"]["detail"],
                             "oidc_not_configured")
        self.assertEqual(self.jwks.calls, 0,
                         "no verifier is built, so no key may be fetched")
        self.assert_no_mutation(result_id)

    # -- signed-token failure classes over HTTP --------------------------

    def assert_denied(self, token, result_id, detail, **request_kwargs):
        client = self.client(**request_kwargs)
        response = client.post(
            f"/v1/results/{result_id}/decisions",
            headers=self.bearer(token), json=decision_body())
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["error"]["detail"], detail)
        self.assert_no_mutation(result_id)

    def test_expired_token_is_rejected_with_a_static_detail(self):
        result_id = self.seed_alpha()
        self.assert_denied(
            _sign(self.primary, self.primary_kid, _expired_claims()),
            result_id, "token_expired")

    def test_token_from_the_future_is_rejected_as_immature(self):
        result_id = self.seed_alpha()
        self.assert_denied(
            _sign(self.primary, self.primary_kid, _immature_claims()),
            result_id, "token_not_yet_valid")

    def test_wrong_issuer_is_rejected(self):
        result_id = self.seed_alpha()
        self.assert_denied(self.token(iss="https://accounts.evil.example"),
                           result_id, "token_issuer_mismatch")

    def test_foreign_audience_is_rejected(self):
        result_id = self.seed_alpha()
        self.assert_denied(self.token(aud="someone-elses-app"), result_id,
                           "token_audience_mismatch")

    def test_mismatched_azp_is_rejected(self):
        result_id = self.seed_alpha()
        self.assert_denied(self.token(azp="someone-elses-app"), result_id,
                           "token_azp_mismatch")

    def test_alg_none_is_rejected_before_any_key_is_used(self):
        result_id = self.seed_alpha()
        unsigned = _handcrafted({"alg": "none", "kid": self.primary_kid},
                                _claims())
        self.assert_denied(unsigned, result_id, "token_algorithm_not_allowed")

    def test_hs256_confusion_never_reaches_the_rsa_key(self):
        # The classic RS256->HS256 confusion attack, hand-crafted exactly as a
        # real attacker does it (bypassing PyJWT's own encoder, which refuses
        # an asymmetric PEM as an HMAC secret): the public key PEM is the HMAC
        # secret over header.payload. The verifier's allowlist must stop this
        # BEFORE any RSA key is used.
        result_id = self.seed_alpha()
        from cryptography.hazmat.primitives import serialization

        public_pem = self.primary.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo)
        confused = _hs256_token(
            {"alg": "HS256", "kid": self.primary_kid, "typ": "JWT"},
            _claims(), public_pem)
        self.assert_denied(confused, result_id, "token_algorithm_not_allowed")

    def test_tampered_payload_fails_the_signature_check(self):
        result_id = self.seed_alpha()
        forged = _tamper(self.token(), _claims(sub=SUBJECT_B))
        self.assert_denied(forged, result_id, "token_signature_invalid")

    def test_unknown_kid_is_rejected_after_one_bounded_refresh(self):
        result_id = self.seed_alpha()
        stranger = _RSA_KEY_B
        self.assert_denied(_sign(stranger, "kid-stranger", _claims()),
                           result_id, "token_kid_unknown")

    def test_header_without_kid_is_rejected(self):
        result_id = self.seed_alpha()
        header = {"alg": "RS256", "typ": "JWT"}
        token = auth_oidc._pyjwt.encode(_claims(), self.primary,
                                        algorithm="RS256", headers=header)
        self.assert_denied(token, result_id, "token_kid_missing")

    def test_opaque_access_token_is_not_an_id_token(self):
        # Classroom opaque access tokens must never authenticate.
        result_id = self.seed_alpha()
        self.assert_denied("ya29.a0AfH6SMBx-oauth-opaque", result_id,
                           "token_malformed")

    def test_blank_subject_is_rejected_as_unidentified(self):
        result_id = self.seed_alpha()
        self.assert_denied(self.token(sub="   "), result_id,
                           "subject_unidentified")

    def test_missing_sub_claim_is_rejected(self):
        result_id = self.seed_alpha()
        claims = _claims()
        claims.pop("sub")
        self.assert_denied(_sign(self.primary, self.primary_kid, claims),
                           result_id, "token_invalid")

    def test_malformed_header_segment_is_rejected(self):
        result_id = self.seed_alpha()
        self.assert_denied("!!!not-base64!!!." + _b64url(b"{}") + ".sig",
                           result_id, "token_malformed")

    def test_header_that_is_not_an_object_is_rejected(self):
        result_id = self.seed_alpha()
        self.assert_denied(
            _handcrafted(["not", "an", "object"], _claims()), result_id,
            "token_malformed")

    def test_two_segment_and_four_segment_tokens_are_rejected(self):
        result_id = self.seed_alpha()
        for token in ("only.two", "a.b.c.d"):
            self.assert_denied(token, result_id, "token_malformed")

    # -- identity injection, even for a genuine caller -------------------

    def test_body_query_and_duplicate_headers_cannot_move_an_authenticated_identity(self):
        # Alpha is genuinely authenticated. Can she still become Bob?
        result_id = self.seed_alpha()
        self.rows[OWNER_B] = [manual_row(OWNER_B)]
        bob_id = decision_core.durable_result_identity(
            self.rows[OWNER_B][0], "")
        client = self.client()
        attempts = [
            ("body", client.post(f"/v1/results/{result_id}/decisions",
                                 headers=self.bearer(self.token()),
                                 json=decision_body(subject=SUBJECT_B,
                                                    owner=OWNER_B))),
            ("query", client.post(
                f"/v1/results/{result_id}/decisions?subject={SUBJECT_B}"
                f"&owner={OWNER_B}",
                headers=self.bearer(self.token()), json=decision_body())),
            ("read-query", client.get(
                f"/v1/results/{bob_id}/events?subject={SUBJECT_A}"
                f"&owner={OWNER_A}", headers=self.bearer(self.token()))),
        ]
        for label, response in attempts:
            self.assertEqual(response.status_code, 200, (label, response.text))
        self.assertEqual(self.revision(owner=OWNER_B, result_id=bob_id), None,
                         "no write may land in another owner's stream")
        self.assertEqual(self.revision(result_id=result_id), 1)

    def test_reissued_token_for_another_subject_hijacks_nothing(self):
        # Account switching: Bob's genuine token must not reach Alpha's
        # stream, and the denial must be indistinguishable from absence.
        result_id = self.seed_alpha()
        client = self.client()
        response = client.post(
            f"/v1/results/{result_id}/decisions",
            headers=self.bearer(self.token(sub=SUBJECT_B)),
            json=decision_body())
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"]["code"],
                         "result_not_registered")
        self.assert_no_mutation(result_id)

    def test_valid_token_for_one_subject_cannot_read_another_subjects_history(self):
        result_id = self.seed_alpha()
        client = self.client()
        committed = client.post(
            f"/v1/results/{result_id}/decisions",
            headers=self.bearer(self.token()), json=decision_body())
        self.assertEqual(committed.status_code, 200, committed.text)
        stolen = client.get(f"/v1/results/{result_id}/events",
                            headers=self.bearer(self.token(sub=SUBJECT_B)))
        self.assertEqual(stolen.status_code, 404, stolen.text)
        self.assertEqual(stolen.json()["error"]["code"],
                         "result_not_registered")

    # -- JWKS transport --------------------------------------------------

    def test_attacker_supplied_jku_header_never_redirects_key_resolution(self):
        result_id = self.seed_alpha()
        hostile = _sign(self.primary, self.primary_kid, _claims(),
                        headers={"jku": "https://attacker.example/jwks",
                                 "x5u": "https://attacker.example/cert"})
        client = self.client()
        response = client.post(
            f"/v1/results/{result_id}/decisions",
            headers=self.bearer(hostile), json=decision_body())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.jwks.urls, [auth_oidc.GOOGLE_JWKS_URL],
                         "only the pinned JWKS endpoint may ever be fetched")

    def test_unknown_kid_flood_is_rate_bounded(self):
        # An attacker who can mint unsigned/stranger tokens must not be able
        # to turn each request into a JWKS fetch.
        result_id = self.seed_alpha()
        client = self.client()
        stranger = _RSA_KEY_B
        for index in range(12):
            token = _sign(stranger, f"kid-flood-{index}", _claims())
            response = client.post(
                f"/v1/results/{result_id}/decisions",
                headers=self.bearer(token), json=decision_body())
            self.assertEqual(response.status_code, 401, response.text)
            self.assertEqual(response.json()["error"]["detail"],
                             "token_kid_unknown")
        self.assertLessEqual(self.jwks.calls, 2,
                             "unknown-kid refreshes must be cooldown-bounded")
        self.assert_no_mutation(result_id)

    def test_jwks_outage_with_an_empty_cache_fails_closed(self):
        result_id = self.seed_alpha()
        self.jwks.fail = OSError("simulated JWKS outage")
        client = self.client()
        response = client.post(
            f"/v1/results/{result_id}/decisions",
            headers=self.bearer(self.token()), json=decision_body())
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["error"]["detail"],
                         "jwks_unavailable")
        self.assert_no_mutation(result_id)

    def test_jwks_outage_with_a_cached_key_still_authenticates(self):
        result_id = self.seed_alpha()
        client = self.client()
        first = client.post(
            f"/v1/results/{result_id}/decisions",
            headers=self.bearer(self.token()), json=decision_body())
        self.assertEqual(first.status_code, 200, first.text)
        self.jwks.fail = OSError("simulated JWKS outage")
        second = client.post(
            f"/v1/results/{result_id}/decisions",
            headers=self.bearer(self.token()),
            json=decision_body(expected_revision=1, idempotency_key="k-2"))
        self.assertEqual(second.status_code, 200, second.text)

    def test_malformed_jwks_document_is_rejected(self):
        result_id = self.seed_alpha()
        self.jwks.documents = [{"keys": "not-a-list"}]
        self.assert_denied(self.token(), result_id, "jwks_unavailable")

    def test_non_rsa_keys_in_the_jwks_are_ignored(self):
        # A JWKS that only offers EC keys must not silently authenticate.
        result_id = self.seed_alpha()
        from jwt.algorithms import ECAlgorithm

        ec_key = _ec_private()
        ec_jwk = json.loads(ECAlgorithm.to_jwk(ec_key.public_key()))
        ec_jwk.update({"kid": "kid-ec", "alg": "ES256", "use": "sig"})
        self.jwks.documents = [{"keys": [ec_jwk]}]
        self.assert_denied(self.token(), result_id, "jwks_unavailable")

    def test_retired_signing_key_dies_with_the_rotation(self):
        # Key rotation: the JWKS document now publishes only the new key. A
        # token signed by the retired key dies once the cache actually picks
        # up the rotation - the bounded refresh REPLACES the cached keys, so
        # the retired kid is gone. (The 300 s TTL means the old key stays
        # trusted for up to one cache lifetime if no refresh happens; that is
        # the documented availability tradeoff, not asserted here.)
        result_id = self.seed_alpha()
        rotated = _RSA_KEY_B
        self.jwks.documents.append(
            _jwks_doc((rotated, "kid-rotated")))
        client = self.client()
        accepted = client.post(
            f"/v1/results/{result_id}/decisions",
            headers=self.bearer(self.token()), json=decision_body())
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.jwks.rotate_to(1)
        # The rotated kid is unknown: one bounded forced refresh fetches the
        # new document and REPLACES the cache, dropping the retired kid. The
        # rotated key's token is fully authorized: it commits a real decision
        # (a different action, so the store appends rather than replays).
        rotated_token = _sign(rotated, "kid-rotated", _claims())
        rotated_response = client.post(
            f"/v1/results/{result_id}/decisions",
            headers=self.bearer(rotated_token),
            json=decision_body(action="correct", new_score="2",
                               reason="authorized under the rotated key",
                               expected_revision=1, idempotency_key="k-2"))
        self.assertEqual(rotated_response.status_code, 200, rotated_response.text)
        self.assertFalse(rotated_response.json()["replayed"],
                         "a genuinely new decision under the rotated key "
                         "must append, not replay")
        self.assertEqual(self.jwks.calls, 2,
                         "one initial fetch plus exactly one forced "
                         "rotation refresh: JWKS traffic stays bounded")
        # Now the retired key is gone from the cache: its token is refused
        # and cannot advance the stream (revision stays 2).
        stale = client.post(
            f"/v1/results/{result_id}/decisions",
            headers=self.bearer(self.token()),
            json=decision_body(action="correct", new_score="0",
                               reason="denial attempt", expected_revision=2,
                               idempotency_key="k-3"))
        self.assertEqual(stale.status_code, 401, stale.text)
        self.assertEqual(stale.json()["error"]["detail"], "token_kid_unknown")
        self.assertEqual(self.revision(result_id=result_id), 2,
                         "the rotated key committed (revision 2); the "
                         "retired key's token must add nothing")

    # -- membership, after a real verification ---------------------------

    def test_verified_token_without_active_membership_is_403(self):
        result_id = self.seed_alpha()
        self.membership_result = None
        client = self.client()
        response = client.post(
            f"/v1/results/{result_id}/decisions",
            headers=self.bearer(self.token()), json=decision_body())
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"],
                         "tenant_membership_required")
        self.assertEqual(response.json()["error"]["detail"],
                         "membership_inactive_or_absent")
        self.assert_no_mutation(result_id)

    def test_ambiguous_membership_fails_closed_without_guessing(self):
        result_id = self.seed_alpha()
        self.membership_result = _MembershipError("membership_ambiguous")
        client = self.client()
        response = client.post(
            f"/v1/results/{result_id}/decisions",
            headers=self.bearer(self.token()), json=decision_body())
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["detail"],
                         "membership_ambiguous")
        self.assert_no_mutation(result_id)

    def test_membership_store_failure_is_403_not_500(self):
        result_id = self.seed_alpha()
        self.membership_result = _MembershipError("membership_unavailable")
        client = self.client()
        response = client.post(
            f"/v1/results/{result_id}/decisions",
            headers=self.bearer(self.token()), json=decision_body())
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["detail"],
                         "membership_unavailable")
        self.assert_no_mutation(result_id)

    def test_membership_resolver_crash_fails_closed(self):
        result_id = self.seed_alpha()

        def exploding(subject):
            raise RuntimeError("simulated membership store crash")

        client = self.client(membership=exploding)
        response = client.post(
            f"/v1/results/{result_id}/decisions",
            headers=self.bearer(self.token()), json=decision_body())
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["detail"],
                         "membership_unavailable")
        self.assert_no_mutation(result_id)

    def test_authentication_is_decided_before_membership_is_consulted(self):
        # An unauthenticated caller must never reach (or be told about) the
        # membership layer: 401 first, and the resolver is never called.
        result_id = self.seed_alpha()
        client = self.client()
        response = client.post(
            f"/v1/results/{result_id}/decisions",
            headers=self.bearer(self.token(exp=1_600_000_000)),
            json=decision_body())
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["error"]["detail"], "token_expired")
        self.assertEqual(self.membership_calls, [],
                         "membership must not be consulted for a bad token")
        self.assert_no_mutation(result_id)

    def test_membership_cannot_mint_an_owner(self):
        # The resolver is trusted, but even a hostile one must not be able to
        # move a decision into another tenant's stream: the owner is always
        # derived from the verified subject.
        result_id = self.seed_alpha()
        self.rows[OWNER_B] = [manual_row(OWNER_B)]
        bob_id = decision_core.durable_result_identity(
            self.rows[OWNER_B][0], "")
        self.membership_result = _BogusMembership("attacker-tenant")
        client = self.client()
        response = client.post(
            f"/v1/results/{result_id}/decisions",
            headers=self.bearer(self.token()), json=decision_body())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["result_revision"], 1)
        self.assertEqual(
            len(self.store.events(owner=OWNER_A, result_id=result_id)), 1)
        self.assertEqual(self.revision(owner=OWNER_B, result_id=bob_id), None)
        self.assertNotIn("attacker-tenant", response.text)

    def test_membership_of_another_subject_does_not_help(self):
        result_id = self.seed_alpha()
        resolver_calls = []

        def resolver(subject):
            resolver_calls.append(subject)
            return _ActiveMembership() if subject == SUBJECT_B else None

        client = self.client(membership=resolver)
        response = client.post(
            f"/v1/results/{result_id}/decisions",
            headers=self.bearer(self.token()), json=decision_body())
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(resolver_calls, [SUBJECT_A])
        self.assert_no_mutation(result_id)

    # -- disclosure ------------------------------------------------------

    def test_no_token_bytes_or_internals_ever_reach_the_client_or_disk(self):
        result_id = self.seed_alpha()
        token = self.token()
        client = self.client()
        for bad in (token, _tamper(token, _claims(sub=SUBJECT_B)),
                    "!!!not-base64!!!." + _b64url(b"{}") + ".sig"):
            response = client.post(f"/v1/results/{result_id}/decisions",
                                   headers=self.bearer(bad),
                                   json=decision_body())
            body = response.text
            self.assertNotIn(TOKEN_CANARY, body)
            self.assertNotIn(bad, body)
            self.assertNotIn(bad.split(".")[0], body)
            self.assertNotIn("Traceback", body)
            self.assertNotIn("auth_oidc", body)
        for name in sorted(Path(self.tmp.name).iterdir()):
            self.assertNotIn(token.encode(), name.read_bytes())
        # The valid token legitimately commits exactly one decision; the two
        # denied tokens add nothing on top of it. The token bytes themselves
        # never reach the store (checked above).
        self.assertEqual(
            len(self.store.events(owner=OWNER_A, result_id=result_id)), 1,
            "the valid token commits once and the denied tokens add nothing")

    def test_verifier_crash_is_a_static_401_not_a_raw_500(self):
        # Finding F-1 (BCM, reviewed by IR as VALID) is closed by CM: an
        # unexpected verifier failure now maps to 401 token_verification_failed.
        # Pinned here so the raw-500 gap cannot come back.
        result_id = self.seed_alpha()
        client = self.client()
        with mock.patch.object(auth_oidc, "pyjwt_decode",
                               side_effect=RuntimeError("boom")):
            response = client.post(
                f"/v1/results/{result_id}/decisions",
                headers=self.bearer(self.token()), json=decision_body())
        self.assertEqual(response.status_code, 401, response.text)
        self.assertEqual(response.json()["error"]["detail"],
                         "token_verification_failed")
        self.assertNotIn("boom", response.text)
        self.assert_no_mutation(result_id)

    def test_healthz_stays_public_and_content_free(self):
        client = self.client()
        response = client.get("/v1/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        for route in ("/docs", "/openapi.json", "/redoc"):
            self.assertEqual(client.get(route).status_code, 404, route)


# --- module-level fixtures (shared by every test) ------------------------

from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402


def _rsa():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _ec_private():
    from cryptography.hazmat.primitives.asymmetric import ec

    return ec.generate_private_key(ec.SECP256R1())


_RSA_KEY_A = _rsa()
_RSA_KEY_B = _rsa()


class _ActiveMembership:
    """An explicit ACTIVE membership fixture - never a bypass: the resolver
    still has to return it, and an absent resolver still fails closed."""

    subject = SUBJECT_A
    tenant_id = "tenant-alpha"
    role = "teacher"


class _BogusMembership:
    def __init__(self, tenant_id):
        self.subject = "not-the-real-subject"
        self.tenant_id = tenant_id
        self.role = "attacker"


class _MembershipError(Exception):
    def __init__(self, detail):
        self.detail = detail
        super().__init__(detail)


# The membership seam's own exception type, so the 403 mapping is exercised
# against the real class rather than a look-alike.
import membership_store  # noqa: E402

_MembershipError = membership_store.MembershipError


class VerifierCoverageGuardTests(unittest.TestCase):
    """The vacuous ``api/``-only scan is replaced by a guard that follows the
    verifier wherever it ships.

    Failure mode prevented: a token verifier lands in the tree (today
    ``auth_oidc.py`` at the repo root), the old scan looks only at ``api/``,
    passes vacuously, and nobody re-attacks the new verifier. This guard
    fails meaningfully instead.
    """

    # Every module that ships token-verification logic must be named here,
    # together with the file that attacks it.
    VERIFIER_MODULES = {
        "auth_oidc.py": "tests/test_c6b6_oidc_attacks.py",
    }

    def test_every_verifier_module_is_named_and_attacked(self):
        root = Path(__file__).resolve().parents[1]
        for module, attack_file in self.VERIFIER_MODULES.items():
            self.assertTrue((root / module).is_file(),
                            f"{module} is declared as a verifier but is gone")
            self.assertTrue((root / attack_file).is_file(),
                            f"{module} has no attack suite ({attack_file})")

    def test_the_declared_verifier_is_the_one_the_api_wires_by_default(self):
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "api" / "app.py").read_text(encoding="utf-8")
        for module in self.VERIFIER_MODULES:
            stem = module[:-3]
            self.assertIn(f"from {stem} import", app_source,
                          f"api/app.py no longer imports {stem}")
        self.assertIn("verify_token=None", app_source,
                      "the real verifier must remain the documented default")

    def test_the_verifier_vocabulary_is_pinned(self):
        # If the static detail codes drift, this suite's assertions stop
        # meaning anything - so the codes themselves are pinned here.
        root = Path(__file__).resolve().parents[1]
        source = (root / "auth_oidc.py").read_text(encoding="utf-8")
        for code in ("token_expired", "token_not_yet_valid",
                     "token_issuer_mismatch", "token_audience_mismatch",
                     "token_azp_mismatch", "token_algorithm_not_allowed",
                     "token_signature_invalid", "token_kid_unknown",
                     "token_kid_missing", "jwks_unavailable",
                     "jwks_malformed", "token_malformed", "token_missing",
                     "token_invalid", "subject_unidentified",
                     "token_revoked"):
            self.assertIn(f'"{code}"', source,
                          f"detail code {code} is no longer produced")
        self.assertIn("RS256", source)
        self.assertIn("GOOGLE_JWKS_URL", source)

    def test_api_package_still_ships_no_second_competing_verifier(self):
        root = Path(__file__).resolve().parents[1]
        api_dir = root / "api"
        offenders = []
        pattern = re.compile(
            r"\b(jwt|jose|jwks|authlib|itsdangerous|signed_secret|cookie|"
            r"set_session)\b", re.IGNORECASE)
        for file in sorted(api_dir.glob("*.py")):
            if file.name == "__init__.py":
                continue
            for line in file.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("#"):
                    continue
                if pattern.search(line) and "import" in line:
                    offenders.append(f"{file.name}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "api/ must not grow a second, un-attacked verifier")


import re  # noqa: E402

if __name__ == "__main__":
    unittest.main()
