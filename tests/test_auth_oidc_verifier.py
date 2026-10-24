"""OIDC ID-token verifier: offline negative matrix (IR design conditions).

Local RSA keys + a fake JWKS provider stand in for Google's endpoint; no
network, no real token ever exists. Covered: signature/issuer/audience/expiry
enforcement, algorithm-confusion defence (RS256-only allowlist checked before
key use), JWKS rotation cache bounds and unreachable fail-closed behaviour,
static error details that never contain token bytes, and the revocation hook.
"""
import base64
import json
import time
import unittest

from auth_oidc import (
    ALLOWED_ALGORITHMS,
    GOOGLE_ISSUERS,
    OidcError,
    OidcVerifier,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt as pyjwt

AUDIENCE = "beta-client-id.apps.googleusercontent.com"
SUBJECT = "teacher-a-subject"


def _b64url(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(
        value.to_bytes(length, "big")).rstrip(b"=").decode("ascii")


class KeyMaterial:
    """One RSA key exposed both as a signer and as a JWKS entry."""

    def __init__(self, kid: str = "kid-1"):
        self.kid = kid
        self.private_key = rsa.generate_private_key(public_exponent=65537,
                                                    key_size=2048)
        numbers = self.private_key.public_key().public_numbers()
        self.jwk = {"kty": "RSA", "kid": kid, "alg": "RS256", "use": "sig",
                    "n": _b64url(numbers.n), "e": _b64url(numbers.e)}
        self.pem = self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption())

    def token(self, claims: dict, *, algorithm: str = "RS256",
              kid: str = None):
        headers = {"kid": self.kid if kid is None else kid}
        # HS256 needs a symmetric secret (an RSA PEM is refused on purpose by
        # PyJWT) - which is exactly why RS256-only is the allowlist.
        key = self.pem if algorithm == "RS256" else b"hs256-test-secret"
        return pyjwt.encode(claims, key, algorithm=algorithm,
                            headers=headers)


def base_claims(now: float) -> dict:
    return {"iss": GOOGLE_ISSUERS[0], "aud": AUDIENCE, "sub": SUBJECT,
            "iat": int(now - 60), "exp": int(now + 600)}


class VerifierHarness(unittest.TestCase):
    def setUp(self):
        # Claims are validated by PyJWT against REAL wall-clock time, so the
        # token timestamps must be real; the frozen clock only drives the
        # JWKS cache (cooldown/TTL assertions below).
        self.now = time.time()
        self.key = KeyMaterial()
        self.other = KeyMaterial(kid="kid-other")
        self.fetches = []

    def verifier(self, keys=None, unreachable=False, revocation=None,
                 audience=AUDIENCE, cooldown=30.0):
        keys = [self.key.jwk] if keys is None else keys

        def provider(url, timeout):
            self.fetches.append(url)
            if unreachable:
                raise OSError("network down")
            return {"keys": keys}

        return OidcVerifier(
            audience=audience, jwks_provider=provider,
            clock=lambda: self.now, kid_refresh_cooldown_seconds=cooldown,
            revocation_checker=revocation)

    def assert_detail(self, verifier, token, detail):
        with self.assertRaises(OidcError) as caught:
            verifier.verify(token)
        self.assertEqual(caught.exception.detail, detail)
        # Static detail: the token bytes never leak through the exception.
        if isinstance(token, str) and token:
            self.assertNotIn(token, str(caught.exception))
        return caught.exception


class ValidTokenTests(VerifierHarness):
    def test_valid_token_returns_the_verified_subject(self):
        token = self.key.token(base_claims(self.now))
        self.assertEqual(self.verifier().verify(token), SUBJECT)

    def test_google_issuer_alias_is_accepted(self):
        claims = base_claims(self.now)
        claims["iss"] = "accounts.google.com"
        self.assertEqual(self.verifier().verify(self.key.token(claims)),
                         SUBJECT)

    def test_expiry_within_the_allowed_clock_skew_still_verifies(self):
        claims = base_claims(self.now)
        claims["exp"] = int(self.now - 30)  # inside the 60s skew
        self.assertEqual(self.verifier().verify(self.key.token(claims)),
                         SUBJECT)


class ClaimEnforcementTests(VerifierHarness):
    def test_expired_beyond_skew_is_rejected(self):
        claims = base_claims(self.now)
        claims["exp"] = int(self.now - 120)
        self.assert_detail(self.verifier(), self.key.token(claims),
                           "token_expired")

    def test_wrong_issuer_is_rejected(self):
        claims = base_claims(self.now)
        claims["iss"] = "https://evil.example.com"
        self.assert_detail(self.verifier(), self.key.token(claims),
                           "token_issuer_mismatch")

    def test_foreign_audience_is_rejected(self):
        claims = base_claims(self.now)
        claims["aud"] = "someone-elses-client.apps.googleusercontent.com"
        self.assert_detail(self.verifier(), self.key.token(claims),
                           "token_audience_mismatch")

    def test_wrong_azp_is_rejected(self):
        claims = base_claims(self.now)
        claims["azp"] = "another-client.apps.googleusercontent.com"
        self.assert_detail(self.verifier(), self.key.token(claims),
                           "token_azp_mismatch")

    def test_future_iat_beyond_skew_is_rejected(self):
        claims = base_claims(self.now)
        claims["iat"] = int(self.now + 300)
        self.assert_detail(self.verifier(), self.key.token(claims),
                           "token_not_yet_valid")

    def test_missing_sub_claim_is_rejected(self):
        claims = base_claims(self.now)
        claims.pop("sub")
        self.assert_detail(self.verifier(), self.key.token(claims),
                           "token_invalid")

    def test_blank_sub_is_rejected(self):
        claims = base_claims(self.now)
        claims["sub"] = "   "
        self.assert_detail(self.verifier(), self.key.token(claims),
                           "subject_unidentified")


class AlgorithmConfusionTests(VerifierHarness):
    def test_hs256_signed_token_never_reaches_the_rsa_key(self):
        token = self.key.token(base_claims(self.now), algorithm="HS256")
        self.assert_detail(self.verifier(), token,
                           "token_algorithm_not_allowed")

    def test_alg_none_header_is_rejected(self):
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "none", "typ": "JWT"}).encode()).rstrip(b"=")
        payload = base64.urlsafe_b64encode(
            json.dumps(base_claims(self.now)).encode()).rstrip(b"=")
        unsigned = (header + b"." + payload + b".").decode()
        self.assert_detail(self.verifier(), unsigned,
                           "token_algorithm_not_allowed")

    def test_tampered_payload_fails_the_signature_check(self):
        token = self.key.token(base_claims(self.now))
        head, body, tail = token.split(".")
        claims = base_claims(self.now)
        claims["sub"] = "someone-else"  # privilege-escalation attempt
        forged = base64.urlsafe_b64encode(
            json.dumps(claims).encode()).rstrip(b"=").decode()
        self.assert_detail(self.verifier(), f"{head}.{forged}.{tail}",
                           "token_signature_invalid")

    def test_allowed_algorithms_is_rs256_only(self):
        self.assertEqual(ALLOWED_ALGORITHMS, ("RS256",))


class JwksCacheTests(VerifierHarness):
    def test_unknown_kid_forces_one_bounded_refresh_then_fails_closed(self):
        verifier = self.verifier(keys=[self.other.jwk])
        token = self.key.token(base_claims(self.now), kid="kid-unknown")
        self.assert_detail(verifier, token, "token_kid_unknown")
        first_fetches = len(self.fetches)
        # A burst of unknown kids inside the cooldown must not refetch.
        for _ in range(5):
            self.assert_detail(verifier, token, "token_kid_unknown")
        self.assertEqual(len(self.fetches), first_fetches,
                         "unknown-kid burst must stay inside the cooldown")

    def test_unreachable_jwks_with_empty_cache_fails_closed(self):
        verifier = self.verifier(unreachable=True)
        token = self.key.token(base_claims(self.now))
        self.assert_detail(verifier, token, "jwks_unavailable")

    def test_unreachable_jwks_still_verifies_with_a_cached_key(self):
        verifier = self.verifier()
        token = self.key.token(base_claims(self.now))
        self.assertEqual(verifier.verify(token), SUBJECT)
        verifier._provider = lambda url, timeout: (_ for _ in ()).throw(
            OSError("network down"))
        verifier._fetched_at = -10_000.0  # force a refresh attempt
        other = self.key.token(base_claims(self.now), kid="kid-1")
        self.assertEqual(verifier.verify(other), SUBJECT)

    def test_rotated_key_is_picked_up_by_the_forced_refresh(self):
        rotated = KeyMaterial(kid="kid-1")
        calls = {"count": 0}

        def rotating_provider(url, timeout):
            calls["count"] += 1
            # First fetch serves the old key; the forced fetch serves the
            # rotated key for the same kid (real Google kid rotation).
            return {"keys": ([self.other.jwk] if calls["count"] < 2
                             else [rotated.jwk])}

        verifier = OidcVerifier(
            audience=AUDIENCE, jwks_provider=rotating_provider,
            clock=lambda: self.now, kid_refresh_cooldown_seconds=30.0)
        token = rotated.token(base_claims(self.now))
        self.assertEqual(verifier.verify(token), SUBJECT)
        self.assertGreaterEqual(calls["count"], 2,
                                "unknown kid must force one refresh")


class StaticDetailTests(VerifierHarness):
    def test_missing_and_malformed_tokens(self):
        verifier = self.verifier()
        for bad, detail in ((None, "token_missing"), ("", "token_missing"),
                            ("not-a-jwt", "token_malformed"),
                            ("a.b", "token_malformed")):
            with self.subTest(token=bad):
                self.assert_detail(verifier, bad, detail)

    def test_header_without_kid_fails_closed(self):
        token = self.key.token(base_claims(self.now), kid="")
        self.assert_detail(self.verifier(), token, "token_kid_missing")

    def test_revocation_checker_failure_is_token_revoked(self):
        def revoked(subject):
            raise RuntimeError("introspection down")

        token = self.key.token(base_claims(self.now))
        self.assert_detail(self.verifier(revocation=revoked), token,
                           "token_revoked")

    def test_unconfigured_audience_cannot_build_a_verifier(self):
        for bad in ("", "   ", None):
            with self.subTest(audience=bad):
                with self.assertRaises(OidcError) as caught:
                    OidcVerifier(audience=bad or "")
                self.assertEqual(caught.exception.detail,
                                 "oidc_not_configured")


if __name__ == "__main__":
    unittest.main()
