"""Offline compatibility checks using real Streamlit/Authlib crypto validation.

Only synthetic identities and ephemeral signing keys are used. Provider metadata
and public keys are supplied locally; no OAuth or Google request is performed.
"""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from authlib.integrations import starlette_client
from authlib.jose import jwt as legacy_jwt
from joserfc import jwt
from joserfc.jwk import RSAKey
from streamlit import auth_util
from streamlit.errors import StreamlitAuthError
from streamlit.web.server.starlette.starlette_auth_routes import (
    _create_streamlit_oauth_class,
)


class AuthlibCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.network = patch("httpx.AsyncClient.send", side_effect=AssertionError("Network forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.secret = patch.object(auth_util, "get_signing_secret", return_value="synthetic-cookie-signing-secret-32-bytes")
        self.secret.start()
        self.addCleanup(self.secret.stop)

    def token_paths(self):
        return (
            (auth_util.encode_provider_token, auth_util.decode_provider_token),
            (auth_util._encode_provider_token_with_authlib, auth_util._decode_provider_token_with_authlib),
        )

    def test_provider_token_roundtrip(self):
        for encode, decode in self.token_paths():
            with self.subTest(path=encode.__name__):
                self.assertEqual(decode(encode("google"))["provider"], "google")

    def test_expired_provider_token_rejected(self):
        for encode, decode in self.token_paths():
            with self.subTest(path=encode.__name__):
                with patch.object(auth_util, "_get_provider_token_expiration_timestamp", return_value=1):
                    token = encode("google")
                with self.assertRaises(StreamlitAuthError):
                    decode(token)

    def test_foreign_signing_key_rejected(self):
        for encode, decode in self.token_paths():
            with self.subTest(path=encode.__name__):
                token = encode("google")
                with patch.object(auth_util, "get_signing_secret", return_value="different-synthetic-signing-secret-32"):
                    with self.assertRaises(StreamlitAuthError):
                        decode(token)

    def test_empty_hmac_key_rejected(self):
        for algorithm in ("HS256", "HS384", "HS512"):
            with self.subTest(algorithm=algorithm), self.assertRaises(ValueError):
                legacy_jwt.encode({"alg": algorithm}, {"sub": "synthetic"}, b"")

    def oidc(self, overrides=None, remove_nonce=False):
        key = RSAKey.generate_key()
        client = _create_streamlit_oauth_class(starlette_client)().register(
            "google", client_id="synthetic-client", client_kwargs={"scope": "openid email profile"}
        )
        client.load_server_metadata = AsyncMock(return_value={
            "issuer": "https://oidc.example.invalid", "id_token_signing_alg_values_supported": ["RS256"]
        })
        client.fetch_jwk_set = AsyncMock(return_value={"keys": [key.as_dict()]})
        claims = {"iss": "https://oidc.example.invalid", "sub": "synthetic-teacher",
                  "aud": "synthetic-client", "iat": int(time.time()),
                  "exp": int(time.time()) + 300, "nonce": "saved-nonce"}
        claims.update(overrides or {})
        if remove_nonce:
            claims.pop("nonce")
        token = {"id_token": jwt.encode({"alg": "RS256"}, claims, key), "access_token": "synthetic-only"}
        return asyncio.run(client.parse_id_token(token, nonce="saved-nonce"))

    def test_signed_oidc_identity_accepted(self):
        self.assertEqual(self.oidc()["sub"], "synthetic-teacher")

    def test_nonce_supported_false_cannot_bypass_nonce(self):
        from joserfc.errors import JoseError
        for missing in (False, True):
            with self.subTest(missing=missing), self.assertRaises(JoseError):
                self.oidc({"nonce": "wrong", "nonce_supported": False}, remove_nonce=missing)

    def test_expired_oidc_identity_rejected(self):
        from joserfc.errors import JoseError
        with self.assertRaises(JoseError):
            self.oidc({"exp": 1})

    def test_wrong_issuer_or_audience_rejected(self):
        from joserfc.errors import JoseError
        for claims in ({"iss": "https://foreign.example.invalid"}, {"aud": "foreign-client"}):
            with self.subTest(claims=claims), self.assertRaises(JoseError):
                self.oidc(claims)


if __name__ == "__main__":
    unittest.main()

