"""Machine identity between platform services (S07 §3, §5).

Each caller presents its Kubernetes projected ServiceAccount token, minted for the
audience `capacity-internal` with a short expiry. The receiver verifies it offline
against the cluster's service-account issuer JWKS (exported by `capacity-lab` into a
ConfigMap), checks issuer, audience, expiry and that `sub` is one of the ServiceAccounts
allowed to call that endpoint. Network reachability alone never authenticates anybody.

Limitation (documented in docs/security.md): offline verification cannot see a deleted
Pod before its token expires; tokens are therefore kept short-lived (10 minutes), and the
executor's authority path avoids HTTP entirely by reading a database view.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import jwt

from capacity_platform.contracts.errors import ErrorCategory, PlatformError
from capacity_platform.contracts.identity import ActorRef
from capacity_platform.shared.auth.oidc import ALLOWED_ALGORITHMS, OidcSettings

INTERNAL_AUDIENCE = "capacity-internal"
TOKEN_PATH = "/var/run/secrets/capacity/token-dir/token"  # projected volume in every platform Pod  # noqa: S105 - a file path, not a secret


@dataclass(frozen=True)
class ServicePrincipal:
    namespace: str
    service_account: str

    @property
    def subject(self) -> str:
        return f"system:serviceaccount:{self.namespace}:{self.service_account}"

    def actor(self) -> ActorRef:
        return ActorRef(issuer="kubernetes", subject=self.subject, kind="machine")


class ServiceTokenVerifier:
    def __init__(self, issuer: str, jwks_file: str, audience: str = INTERNAL_AUDIENCE) -> None:
        from capacity_platform.shared.auth.oidc import JwksCache

        self._issuer, self._audience = issuer, audience
        self._jwks = JwksCache(
            OidcSettings(issuer=issuer, audience=audience, allowed_clients=frozenset(), jwks_file=jwks_file)
        )

    def verify(self, token: str | None, allowed: set[str]) -> ServicePrincipal:
        if not token:
            raise PlatformError("SERVICE_AUTH_REQUIRED", ErrorCategory.AUTHENTICATION, "service identity required")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in ALLOWED_ALGORITHMS:
                raise PlatformError("SERVICE_AUTH_INVALID", ErrorCategory.AUTHENTICATION, "invalid service token")
            claims = jwt.decode(
                token,
                key=self._jwks.key(header.get("kid", "")),
                algorithms=ALLOWED_ALGORITHMS,
                audience=self._audience,
                issuer=self._issuer,
                leeway=30,
                options={"require": ["exp", "iss", "sub", "aud"]},
            )
        except PlatformError:
            raise
        except jwt.PyJWTError as exc:
            raise PlatformError("SERVICE_AUTH_INVALID", ErrorCategory.AUTHENTICATION, "invalid service token") from exc
        sub = str(claims["sub"])
        parts = sub.split(":")
        if len(parts) != 4 or parts[:2] != ["system", "serviceaccount"]:
            raise PlatformError("SERVICE_AUTH_INVALID", ErrorCategory.AUTHENTICATION, "invalid service token")
        principal = ServicePrincipal(namespace=parts[2], service_account=parts[3])
        if principal.subject not in allowed:
            raise PlatformError("SERVICE_NOT_PERMITTED", ErrorCategory.AUTHORIZATION, "caller not permitted")
        return principal


def read_own_token(path: str = TOKEN_PATH) -> str | None:
    """The kubelet rotates the projected token file; read it fresh on every call."""
    p = Path(path)
    return p.read_text(encoding="utf-8").strip() if p.exists() else None


# Which service accounts may call which internal surface (S07 §7.2 allowlist).
API_SA = "system:serviceaccount:capacity-api:capacity-api-sa"
AI_SA = "system:serviceaccount:capacity-ai:capacity-ai-sa"
GOVERNANCE_SA = "system:serviceaccount:capacity-core:capacity-governance-sa"
CAPACITY_SA = "system:serviceaccount:capacity-core:capacity-engine-sa"
OBSERVER_SA = "system:serviceaccount:capacity-core:capacity-observer-sa"
EXECUTOR_SA = "system:serviceaccount:capacity-execution:capacity-executor-sa"
