"""Server-side Google OIDC **ID-token** verification for the beta API.

Design approved by IR (Shared\\IR_M5_SECURITY_DESIGN_REVIEW.md):
* PyJWT[crypto] only (never python-jose - algorithm-confusion advisory history),
  exact version pin, algorithm allowlist strictly ``RS256`` checked BEFORE
  decode and passed to decode, pinned Google JWKS endpoint (never
  caller-supplied), bounded kid-rotation cache that fails closed when the JWKS
  is unreachable AND uncached, pinned issuer set, pinned audience, expiry with
  a small clock skew, ``azp`` checked when present.

Why ID tokens: an opaque Classroom **access** token cannot establish identity
and is never accepted here. The subject comes only from a verified token
claim, and the owner token is derived by the caller as ``google:`` + subject.

Limits, stated honestly:
* Offline verification cannot detect a *revoked* ID token; revocation in this
  codebase stays what it already is - the Classroom access token is rejected
  at the provider and every privileged path fails closed (freshness
  preserved), and an optional ``revocation_checker`` hook is provided for a
  future introspection step. Nothing here logs, echoes or stores the token.
* Replay of a stolen-but-valid token inside its short lifetime is not
  detectable statelessly; audience/issuer binding plus short expiry and TLS
  are the beta mitigations. A ``jti`` denylist is deliberately not invented.

Every failure raises :class:`OidcError` with a STATIC, bounded ``detail``
string - safe for an error envelope, guaranteed never to contain token bytes.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping, Optional

# Pinned values - never caller-supplied, never discovered from the token.
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
ALLOWED_ALGORITHMS = ("RS256",)

CLOCK_SKEW_SECONDS = 60.0
JWKS_TTL_SECONDS = 300.0
KID_REFRESH_COOLDOWN_SECONDS = 30.0
JWKS_TIMEOUT_SECONDS = 5.0


class OidcError(Exception):
    """Verification failure with a static, envelope-safe detail code."""

    def __init__(self, detail: str):
        self.detail = str(detail)
        super().__init__(self.detail)


def _fetch_jwks(url: str, timeout: float) -> Mapping[str, Any]:
    """Default JWKS transport. ``requests`` is imported lazily so this module
    stays importable in offline test environments that never install it."""
    import requests

    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        raise OidcError("jwks_malformed")
    return data


class OidcVerifier:
    """Verify Google ID tokens for one fixed audience.

    ``jwks_provider`` exists for offline tests (local RSA keys); production
    uses the pinned Google endpoint. The cache holds parsed JWKs by kid and is
    bounded by TTL plus an unknown-kid refresh cooldown, so a hostile token
    cannot turn into a JWKS request flood.
    """

    def __init__(
        self,
        *,
        audience: str,
        issuers: tuple = GOOGLE_ISSUERS,
        jwks_url: str = GOOGLE_JWKS_URL,
        jwks_provider: Optional[Callable[[str, float], Mapping[str, Any]]] = None,
        clock: Callable[[], float] = time.time,
        skew_seconds: float = CLOCK_SKEW_SECONDS,
        jwks_ttl_seconds: float = JWKS_TTL_SECONDS,
        kid_refresh_cooldown_seconds: float = KID_REFRESH_COOLDOWN_SECONDS,
        timeout_seconds: float = JWKS_TIMEOUT_SECONDS,
        revocation_checker: Optional[Callable[[str], None]] = None,
    ):
        if not isinstance(audience, str) or not audience.strip():
            raise OidcError("oidc_not_configured")
        self.audience = audience.strip()
        self.issuers = tuple(issuers)
        self.jwks_url = jwks_url
        self._provider = jwks_provider or _fetch_jwks
        self._clock = clock
        self._skew = float(skew_seconds)
        self._ttl = float(jwks_ttl_seconds)
        self._cooldown = float(kid_refresh_cooldown_seconds)
        self._timeout = float(timeout_seconds)
        self._revocation_checker = revocation_checker
        self._keys: dict = {}
        self._fetched_at = 0.0
        self._last_periodic_attempt = 0.0
        self._last_forced_attempt = float("-inf")

    # --- JWKS cache -----------------------------------------------------
    def _refresh(self, force: bool = False) -> None:
        """Bounded refresh: TTL for periodic updates, a SEPARATE cooldown
        for forced unknown-kid lookups.

        Both paths are rate-limited on purpose: a hostile stream of unknown
        kids must never turn into a JWKS request flood, while a genuine kid
        rotation still gets one forced fetch per cooldown window (the first
        unknown kid ever seen refreshes immediately).
        """
        now = self._clock()
        if force:
            if (now - self._last_forced_attempt) < self._cooldown:
                return
            self._last_forced_attempt = now
        else:
            if (now - self._fetched_at) < self._ttl:
                return
            if (now - self._last_periodic_attempt) < self._cooldown:
                return
            self._last_periodic_attempt = now
        try:
            data = self._provider(self.jwks_url, self._timeout)
        except OidcError:
            raise
        except Exception:
            # Unreachable/transport failure: keep whatever is cached; the
            # caller decides (unreachable + nothing usable = fail closed).
            return
        keys = data.get("keys") if isinstance(data, Mapping) else None
        if not isinstance(keys, list):
            return
        fresh = {}
        for jwk in keys:
            if not isinstance(jwk, dict):
                continue
            kid = jwk.get("kid")
            if isinstance(kid, str) and kid and jwk.get("kty") == "RSA":
                fresh[kid] = jwk
        if fresh:
            self._keys = fresh
            self._fetched_at = now

    def _key_for(self, kid: Any) -> Mapping[str, Any]:
        if not isinstance(kid, str) or not kid:
            raise OidcError("token_kid_missing")
        self._refresh()
        key = self._keys.get(kid)
        if key is None:
            # Unknown kid: one forced refresh (rotation), cooldown-bounded.
            self._refresh(force=True)
            key = self._keys.get(kid)
        if key is None:
            if not self._keys:
                raise OidcError("jwks_unavailable")
            raise OidcError("token_kid_unknown")
        return key

    # --- verification ---------------------------------------------------
    def verify(self, token: Any) -> str:
        """Return the verified ``sub`` or raise :class:`OidcError`.

        The token never appears in any exception message, log or return
        value - only static detail codes do.
        """
        if not isinstance(token, str) or not token.strip():
            raise OidcError("token_missing")
        parts = token.strip().split(".")
        if len(parts) != 3:
            raise OidcError("token_malformed")

        header = _decode_header(parts[0])
        algorithm = header.get("alg")
        if algorithm not in ALLOWED_ALGORITHMS:
            # Algorithm allowlist BEFORE any key use: blocks alg=none and the
            # RS256->HS256 confusion class outright.
            raise OidcError("token_algorithm_not_allowed")

        key = self._key_for(header.get("kid"))
        try:
            # Convert the raw JWK to a cryptography key explicitly: PyJWT's
            # prepare_key does not accept a bare dict on every code path.
            from jwt.algorithms import RSAAlgorithm

            prepared_key = RSAAlgorithm.from_jwk(json.dumps(dict(key)))
        except OidcError:
            raise
        except Exception as exc:
            raise OidcError("jwks_malformed") from exc
        try:
            claims = pyjwt_decode(
                token.strip(),
                key=prepared_key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=self.audience,
                issuer=list(self.issuers),
                leeway=self._skew,
                options={"require": ["exp", "iat", "sub"]},
            )
        except pyjwt_exc.ExpiredSignatureError as exc:
            raise OidcError("token_expired") from exc
        except pyjwt_exc.ImmatureSignatureError as exc:
            raise OidcError("token_not_yet_valid") from exc
        except pyjwt_exc.InvalidIssuerError as exc:
            raise OidcError("token_issuer_mismatch") from exc
        except pyjwt_exc.InvalidAudienceError as exc:
            raise OidcError("token_audience_mismatch") from exc
        except pyjwt_exc.InvalidAlgorithmError as exc:
            raise OidcError("token_algorithm_not_allowed") from exc
        except pyjwt_exc.InvalidSignatureError as exc:
            raise OidcError("token_signature_invalid") from exc
        except pyjwt_exc.InvalidIssuedAtError as exc:
            raise OidcError("token_not_yet_valid") from exc
        except pyjwt_exc.PyJWTError as exc:
            # Signature failures, malformed payloads, missing requirements.
            raise OidcError("token_invalid") from exc
        except (ValueError, TypeError) as exc:
            raise OidcError("token_malformed") from exc

        azp = claims.get("azp")
        if azp is not None and azp != self.audience:
            raise OidcError("token_azp_mismatch")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise OidcError("subject_unidentified")

        if self._revocation_checker is not None:
            try:
                self._revocation_checker(subject)
            except OidcError:
                raise
            except Exception:
                raise OidcError("token_revoked")

        return subject.strip()


def _decode_header(segment: str) -> Mapping[str, Any]:
    import base64
    import json

    try:
        padded = segment + "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        header = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise OidcError("token_malformed") from exc
    if not isinstance(header, dict):
        raise OidcError("token_malformed")
    return header


# PyJWT imports are module-level and safe: PyJWT is a pinned production
# dependency of this slice (requirements.txt + tests/pdf-requirements.txt).
import jwt as _pyjwt  # noqa: E402
from jwt import exceptions as pyjwt_exc  # noqa: E402

pyjwt_decode = _pyjwt.decode
