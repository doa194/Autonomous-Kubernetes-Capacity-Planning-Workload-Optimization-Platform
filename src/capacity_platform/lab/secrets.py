"""Vault + External Secrets bootstrap (S08-ADR-001/003).

Vault holds every generated credential. Each consuming namespace has its own
`vault-reader` ServiceAccount, Vault Kubernetes-auth role and policy that can read ONLY that
namespace's paths; an External Secrets `SecretStore` in the same namespace uses it, so a
compromised namespace cannot materialise another namespace's credentials.

Lab simplifications (documented in docs/production-considerations.md): a single Vault server
with one unseal key share, and the root token kept DPAPI-encrypted on the host for
re-bootstrap. Neither would be acceptable in production.
"""

from __future__ import annotations

import base64
import json
import secrets as pysecrets

from capacity_platform.lab.protect import read_json, write_json
from capacity_platform.lab.tools import HOME, kubectl, wait_for

VAULT_NS = "capacity-system"
VAULT_FILE = HOME / "vault" / "init.json.dpapi"
DB_HOST = "postgres.capacity-data.svc"
CA_PATH = "/var/run/secrets/capacity/ca/ca.crt"

# namespace -> {vault path: {kubernetes secret name: {secret key: vault field}}}
CONSUMERS: dict[str, dict[str, tuple[str, dict[str, str]]]] = {
    "capacity-api": {"db/api": ("db-api", {"dsn": "dsn"})},
    "capacity-core": {
        "db/observer": ("db-observer", {"dsn": "dsn"}),
        "db/capacity": ("db-capacity", {"dsn": "dsn"}),
        "db/governance": ("db-governance", {"dsn": "dsn"}),
    },
    "capacity-ai": {"db/ai": ("db-ai", {"dsn": "dsn"}), "gateway/key": ("model-gateway-key", {"key": "key"})},
    "capacity-execution": {"db/executor": ("db-executor", {"dsn": "dsn"})},
    "capacity-data": {"db/migrator": ("postgres-bootstrap", {"migrator-password": "password"})},
    "capacity-identity": {
        "db/keycloak": ("keycloak-db", {"password": "password"}),
        "keycloak/admin": ("keycloak-admin", {"password": "password"}),
        "keycloak/realm": ("keycloak-realm", {"capacity-realm.json": "realm"}),
    },
    "capacity-observability": {"grafana/admin": ("grafana-admin", {"admin-password": "password"})},
}
SERVICE_ROLES = {
    "api": "svc_api",
    "observer": "svc_observer",
    "capacity": "svc_capacity",
    "ai": "svc_ai",
    "governance": "svc_governance",
    "executor": "svc_executor",
}


def vault(*args: str, token: str | None = None, input_text: str | None = None) -> str:
    env = ["env", f"VAULT_TOKEN={token}"] if token else []
    return kubectl(
        "-n", VAULT_NS, "exec", "-i", "vault-0", "--", *env, "vault", *args, input_text=input_text, quiet=True
    ).stdout


def init_and_unseal() -> dict:
    status = json.loads(
        kubectl(
            "-n", VAULT_NS, "exec", "vault-0", "--", "vault", "status", "-format=json", check=False, quiet=True
        ).stdout
        or "{}"
    )
    if not status.get("initialized"):
        init = json.loads(vault("operator", "init", "-key-shares=1", "-key-threshold=1", "-format=json"))
        write_json(VAULT_FILE, {"unseal": init["unseal_keys_b64"][0], "root": init["root_token"]})
    material = read_json(VAULT_FILE)
    if status.get("sealed", True):
        # The unseal key goes over stdin, never as a command argument (S08 §4.1).
        # `vault operator unseal` has no stdin mode, so the raw sys/unseal endpoint is written instead.
        vault("write", "sys/unseal", "-", input_text=json.dumps({"key": material["unseal"]}))
    return material


def generate_secrets() -> dict[str, dict[str, str]]:
    """Fresh random credentials (only on first bootstrap; re-runs reuse Vault's values)."""
    pw = lambda: pysecrets.token_urlsafe(24)  # noqa: E731
    values: dict[str, dict[str, str]] = {}
    for role, db_role in SERVICE_ROLES.items():
        password = pw()
        values[f"db/{role}"] = {
            "password": password,
            "role": db_role,
            "dsn": f"postgresql://{db_role}:{password}@{DB_HOST}:5432/capacity"
            f"?sslmode=verify-full&sslrootcert={CA_PATH}",
        }
    values["db/migrator"] = {"password": pw()}
    values["db/keycloak"] = {"password": pw()}
    values["keycloak/admin"] = {"password": pw()}
    values["grafana/admin"] = {"password": pw()}
    values["gateway/key"] = {"key": pysecrets.token_urlsafe(32)}
    return values


def configure(material: dict, values: dict[str, dict[str, str]]) -> None:
    root = material["root"]
    mounts = vault("secrets", "list", "-format=json", token=root)
    if '"capacity/"' not in mounts:
        vault("secrets", "enable", "-path=capacity", "kv-v2", token=root)
    auths = vault("auth", "list", "-format=json", token=root)
    if '"kubernetes/"' not in auths:
        vault("auth", "enable", "kubernetes", token=root)
    vault("write", "auth/kubernetes/config", "kubernetes_host=https://kubernetes.default.svc:443", token=root)
    for path, data in values.items():
        existing = vault("kv", "get", "-format=json", f"capacity/{path}", token=root) if _exists(root, path) else None
        if existing is None:
            vault("kv", "put", f"capacity/{path}", "-", token=root, input_text=json.dumps(data))
    for namespace, paths in CONSUMERS.items():
        policy = "\n".join(f'path "capacity/data/{p}" {{ capabilities = ["read"] }}' for p in paths)
        vault("policy", "write", f"read-{namespace}", "-", token=root, input_text=policy)
        vault(
            "write",
            f"auth/kubernetes/role/{namespace}",
            "bound_service_account_names=vault-reader",
            f"bound_service_account_namespaces={namespace}",
            f"policies=read-{namespace}",
            "ttl=20m",
            "audience=vault",
            token=root,
        )


def _exists(root: str, path: str) -> bool:
    out = kubectl(
        "-n",
        VAULT_NS,
        "exec",
        "vault-0",
        "--",
        "env",
        f"VAULT_TOKEN={root}",
        "vault",
        "kv",
        "get",
        "-format=json",
        f"capacity/{path}",
        check=False,
        quiet=True,
    )
    return out.returncode == 0


def read_value(path: str, field: str) -> str:
    root = read_json(VAULT_FILE)["root"]
    data = json.loads(vault("kv", "get", "-format=json", f"capacity/{path}", token=root))
    return data["data"]["data"][field]


def put_value(path: str, data: dict) -> None:
    root = read_json(VAULT_FILE)["root"]
    vault("kv", "put", f"capacity/{path}", "-", token=root, input_text=json.dumps(data))


def external_secret_manifests(ca_pem: str) -> list[dict]:
    objs: list[dict] = []
    for namespace, paths in CONSUMERS.items():
        objs.append(
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": {"name": "vault-reader", "namespace": namespace},
                "automountServiceAccountToken": False,
            }
        )
        objs.append(
            {
                "apiVersion": "external-secrets.io/v1",
                "kind": "SecretStore",
                "metadata": {"name": "vault", "namespace": namespace},
                "spec": {
                    "provider": {
                        "vault": {
                            "server": "https://vault.capacity-system.svc:8200",
                            "path": "capacity",
                            "version": "v2",
                            "caBundle": base64.b64encode(ca_pem.encode()).decode(),
                            "auth": {
                                "kubernetes": {
                                    "mountPath": "kubernetes",
                                    "role": namespace,
                                    "serviceAccountRef": {"name": "vault-reader", "audiences": ["vault"]},
                                }
                            },
                        }
                    }
                },
            }
        )
        for path, (secret_name, keys) in paths.items():
            objs.append(
                {
                    "apiVersion": "external-secrets.io/v1",
                    "kind": "ExternalSecret",
                    "metadata": {"name": secret_name, "namespace": namespace},
                    "spec": {
                        "refreshInterval": "10m",
                        "secretStoreRef": {"name": "vault", "kind": "SecretStore"},
                        "target": {"name": secret_name, "creationPolicy": "Owner"},
                        "data": [
                            {"secretKey": k, "remoteRef": {"key": path, "property": field}} for k, field in keys.items()
                        ],
                    },
                }
            )
    return objs


def wait_external_secrets() -> None:
    for namespace, paths in CONSUMERS.items():
        for _, (secret_name, _keys) in paths.items():
            wait_for(
                f"secret {namespace}/{secret_name} synced from Vault",
                lambda ns=namespace, s=secret_name: (
                    kubectl("-n", ns, "get", "secret", s, check=False, quiet=True).returncode == 0
                ),
                timeout=300,
            )
