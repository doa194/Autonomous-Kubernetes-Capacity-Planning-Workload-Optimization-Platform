"""Short-lived scoped experiment capabilities for load drivers (S07 §6, S08 §4.1).

The host runner signs an ES256 JWT that names the run, the allowed workloads and actions,
the audience `capacity-workload-control` and a short expiry. W01-W04 verify it with the
PUBLIC key only (mounted from a ConfigMap), so a workload or driver can never mint a
capability. The signing key stays in a protected host file and never enters the cluster.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

AUDIENCE = "capacity-workload-control"
ISSUER = "capacity-scenario-runner"
MAX_TTL_SECONDS = 3600

ACTIONS = {
    "w01-latency-api": ["compute"],
    "w02-burst-api": ["process"],
    "w03-memory-profile": ["profile.start", "profile.cancel", "profile.read"],
    "w04-cpu-worker": ["task.submit", "task.read"],
}


def generate_keypair(private_path: Path, public_path: Path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    try:
        private_path.chmod(0o600)
    except OSError:
        pass  # Windows ACLs are applied by capacity-lab on the containing directory
    public_path.write_bytes(
        key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )


def issue(private_path: Path, run_id: str, workloads: list[str], ttl_seconds: int) -> tuple[str, str]:
    if ttl_seconds <= 0 or ttl_seconds > MAX_TTL_SECONDS:
        raise ValueError("capability TTL out of bounds")
    actions = sorted({a for w in workloads for a in ACTIONS.get(w, [])})
    capability_id = uuid.uuid4().hex
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": ISSUER,
            "sub": "scenario-driver",
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + ttl_seconds,
            "experimentRunId": run_id,
            "allowedWorkloads": sorted(workloads),
            "allowedActions": actions,
            "jti": capability_id,
        },
        private_path.read_bytes(),
        algorithm="ES256",
    )
    return token, capability_id


def verify(token: str, public_pem: bytes, workload: str, action: str) -> dict:
    """Reference verifier (the Python workloads use this; the .NET APIs implement the same checks)."""
    claims = jwt.decode(
        token,
        public_pem,
        algorithms=["ES256"],
        audience=AUDIENCE,
        issuer=ISSUER,
        options={"require": ["exp", "iat", "aud", "iss", "jti"]},
    )
    if workload not in claims.get("allowedWorkloads", []) or action not in claims.get("allowedActions", []):
        raise PermissionError("capability does not cover this workload/action")
    return claims
