"""Throwaway signing keys and token minting for component tests.

These keys exist only in the test process. They exercise the production validators
(issuer/audience/signature/type checks) without any real identity provider.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://auth.capacity.test/realms/capacity"
SA_ISSUER = "https://kubernetes.default.svc.cluster.local"


class TestKeys:
    __test__ = False  # not a pytest class

    def __init__(self, directory: Path) -> None:
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.private.public_key()))
        jwk.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
        self.jwks_file = directory / "jwks.json"
        self.jwks_file.write_text(json.dumps({"keys": [jwk]}), encoding="utf-8")

    def human(
        self,
        sub: str,
        roles: list[str],
        *,
        aud="capacity-api",
        typ="Bearer",
        azp="capacityctl",
        exp_offset=300,
        key=None,
        alg="RS256",
        iss=ISSUER,
    ) -> str:
        now = int(time.time())
        claims = {
            "iss": iss,
            "sub": sub,
            "aud": aud,
            "exp": now + exp_offset,
            "iat": now,
            "typ": typ,
            "azp": azp,
            "realm_access": {"roles": roles},
        }
        return jwt.encode(claims, key or self.private, algorithm=alg, headers={"kid": "test-key"})

    def service(self, namespace: str, sa: str, *, aud="capacity-internal", exp_offset=600) -> str:
        now = int(time.time())
        claims = {
            "iss": SA_ISSUER,
            "sub": f"system:serviceaccount:{namespace}:{sa}",
            "aud": aud,
            "exp": now + exp_offset,
            "iat": now,
        }
        return jwt.encode(claims, self.private, algorithm="RS256", headers={"kid": "test-key"})
