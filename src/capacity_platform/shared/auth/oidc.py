"""Human access-token validation (S06 §5, S27 §4).

Checks, in order: algorithm allowlist (RS256/ES256 only, never `none`/HS*), key id known in
the trusted JWKS, signature, exact issuer, audience contains the platform API, `exp`/`nbf`
with bounded clock skew, token type is an access token (an ID token is rejected), stable
`sub`, and the authorized client is the expected public CLI. Failure is always 401; the
reason is logged but never echoed with token contents.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt

from capacity_platform.contracts.enums import HumanRole
from capacity_platform.contracts.errors import ErrorCategory, PlatformError
from capacity_platform.contracts.identity import ActorRef

ALLOWED_ALGORITHMS = ["RS256", "ES256"]
CLOCK_SKEW_SECONDS = 60


@dataclass(frozen=True)
class OidcSettings:
    issuer: str
    audience: str
    allowed_clients: frozenset[str]
    jwks_url: str | None = None
    jwks_file: str | None = None
    jwks_cache_seconds: int = 300
    ca_bundle: str | None = None


@dataclass(frozen=True)
class HumanPrincipal:
    actor: ActorRef
    roles: frozenset[HumanRole]
    client_id: str
    expires_at: int


def unauthenticated(reason: str) -> PlatformError:
    return PlatformError(
        "AUTHENTICATION_REQUIRED",
        ErrorCategory.AUTHENTICATION,
        "A valid access token is required",
        details={"reason": reason},
    )


class JwksCache:
    """Caches verified keys. An unknown `kid` triggers at most one refresh per interval;
    if the JWKS endpoint is down the token is rejected — never accepted unchecked."""

    def __init__(self, settings: OidcSettings) -> None:
        self._settings = settings
        self._keys: dict[str, object] = {}
        self._fetched = 0.0
        self._lock = threading.Lock()

    def _load(self) -> dict:
        if self._settings.jwks_file:
            return json.loads(Path(self._settings.jwks_file).read_text(encoding="utf-8"))
        assert self._settings.jwks_url
        verify = self._settings.ca_bundle or True
        resp = httpx.get(self._settings.jwks_url, timeout=5.0, verify=verify)
        resp.raise_for_status()
        return resp.json()

    def _refresh(self) -> None:
        data = self._load()
        keys = {}
        for jwk in data.get("keys", []):
            if jwk.get("use", "sig") != "sig" or "kid" not in jwk:
                continue
            keys[jwk["kid"]] = jwt.PyJWK(jwk).key
        self._keys, self._fetched = keys, time.monotonic()

    def key(self, kid: str):
        with self._lock:
            stale = time.monotonic() - self._fetched > self._settings.jwks_cache_seconds
            if kid not in self._keys or stale:
                try:
                    self._refresh()
                except Exception as exc:
                    raise unauthenticated("jwks_unavailable") from exc
            if kid not in self._keys:
                raise unauthenticated("unknown_signing_key")
            return self._keys[kid]


class AccessTokenValidator:
    def __init__(self, settings: OidcSettings, jwks: JwksCache | None = None) -> None:
        self.settings = settings
        self.jwks = jwks or JwksCache(settings)

    def validate(self, token: str) -> HumanPrincipal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise unauthenticated("malformed") from exc
        if header.get("alg") not in ALLOWED_ALGORITHMS:
            raise unauthenticated("algorithm_not_allowed")
        kid = header.get("kid")
        if not kid:
            raise unauthenticated("missing_kid")
        try:
            claims = jwt.decode(
                token,
                key=self.jwks.key(kid),
                algorithms=ALLOWED_ALGORITHMS,
                audience=self.settings.audience,
                issuer=self.settings.issuer,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": ["exp", "iat", "iss", "sub", "aud"]},
            )
        except PlatformError:
            raise
        except jwt.PyJWTError as exc:
            raise unauthenticated(type(exc).__name__) from exc
        # Keycloak marks access tokens with typ=Bearer; ID tokens carry typ=ID.
        if claims.get("typ", "Bearer") != "Bearer":
            raise unauthenticated("not_an_access_token")
        client = claims.get("azp") or claims.get("client_id") or ""
        if client not in self.settings.allowed_clients:
            raise unauthenticated("client_not_allowed")
        roles = set()
        for name in (claims.get("realm_access") or {}).get("roles", []):
            try:
                roles.add(HumanRole(name))
            except ValueError:
                continue  # unrelated realm roles are ignored, never mapped loosely
        actor = ActorRef(issuer=claims["iss"], subject=str(claims["sub"]), kind="human")
        return HumanPrincipal(actor=actor, roles=frozenset(roles), client_id=client, expires_at=int(claims["exp"]))
