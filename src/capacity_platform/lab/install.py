"""Install stages for supporting infrastructure, images, platform and workloads (S29 §4).

Ordering follows S29's dependency graph: CNI/policy -> PKI -> secrets -> storage/database ->
identity -> observability -> images -> six platform Deployments -> demo workloads. Every stage
is safe to re-run. Database migrations run from the host through a short-lived port-forward
using the migration identity; services never migrate their own schema at start-up.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import subprocess

import psycopg
from psycopg import sql

from capacity_platform.lab import identity, infra_manifests, manifests, secrets
from capacity_platform.lab.cluster import LOCK, REGISTRY_HOST, _verified_manifest
from capacity_platform.lab.pki import issue_intermediate
from capacity_platform.lab.tools import (
    CONTEXT,
    HOME,
    ROOT,
    ToolError,
    apply,
    binary,
    docker,
    kubectl,
    kubectl_json,
    rollout,
    run,
    wait_for,
)

PKI = HOME / "pki"
TRUST_NAMESPACES = [
    "capacity-api",
    "capacity-core",
    "capacity-ai",
    "capacity-execution",
    "capacity-observability",
    "capacity-identity",
    "capacity-system",
    "capacity-data",
]
PLATFORM_NAMESPACES = ["capacity-api", "capacity-core", "capacity-ai", "capacity-execution"]
IMAGES_FILE = HOME / "release" / "images.json"


def configmap(name: str, namespace: str, data: dict[str, str]) -> dict:
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": namespace}, "data": data}


# --------------------------------------------------------------------------------- PKI
def install_pki() -> None:
    apply(_verified_manifest("certManager"), server_side=True)
    for dep in ("cert-manager", "cert-manager-webhook", "cert-manager-cainjector"):
        rollout("cert-manager", f"deployment/{dep}")
    key_pem, cert_pem, root_pem = issue_intermediate(PKI)
    # Intermediate key + chain go in as the CA issuer secret; the ROOT key never enters the cluster.
    apply(
        manifests.dump(
            [
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "type": "kubernetes.io/tls",
                    "metadata": {"name": "capacity-intermediate-ca", "namespace": "cert-manager"},
                    "data": {
                        "tls.crt": base64.b64encode(cert_pem + root_pem).decode(),
                        "tls.key": base64.b64encode(key_pem).decode(),
                    },
                },
                {
                    "apiVersion": "cert-manager.io/v1",
                    "kind": "ClusterIssuer",
                    "metadata": {"name": "capacity-intermediate"},
                    "spec": {"ca": {"secretName": "capacity-intermediate-ca"}},
                },
                *[configmap("capacity-trust", ns, {"ca.crt": root_pem.decode()}) for ns in TRUST_NAMESPACES],
            ]
        )
    )
    wait_for(
        "ClusterIssuer ready",
        lambda: any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in kubectl_json("get", "clusterissuer", "capacity-intermediate")
            .get("status", {})
            .get("conditions", [])
        ),
    )


# --------------------------------------------------------------------------------- Vault + ESO
def install_secrets() -> None:
    from capacity_platform.lab.cluster import ensure_helm_repos

    ensure_helm_repos()
    # Only names inside the intermediate's name constraints (*.svc, localhost, 127/8): a SAN like the
    # chart's short "vault-0.vault-internal" would make the whole certificate fail verification.
    apply(
        manifests.dump(
            [
                infra_manifests._cert(
                    "tls-vault",
                    "capacity-system",
                    ["vault.capacity-system.svc", "vault-0.vault-internal.capacity-system.svc"],
                    ["127.0.0.1"],
                )
            ]
        )
    )
    wait_for(
        "vault certificate issued",
        lambda: kubectl("-n", "capacity-system", "get", "secret", "tls-vault", check=False, quiet=True).returncode == 0,
    )
    values = {
        "global.tlsDisable": "false",
        "server.standalone.enabled": "true",
        "server.dataStorage.size": "1Gi",
        "server.resources.requests.cpu": "50m",
        "server.resources.requests.memory": "128Mi",
        "server.resources.limits.cpu": "500m",
        "server.resources.limits.memory": "256Mi",
        "server.nodeSelector.capacity\\.lab/pool": "platform",
        "injector.enabled": "false",
        "ui.enabled": "false",
        "server.volumes[0].name": "tls",
        "server.volumes[0].secret.secretName": "tls-vault",
        "server.volumeMounts[0].name": "tls",
        "server.volumeMounts[0].mountPath": "/vault/tls",
        "server.extraEnvironmentVars.VAULT_CACERT": "/vault/tls/ca.crt",
    }
    standalone = (
        'ui = false\ndisable_mlock = true\nlistener "tcp" {\n  address = "[::]:8200"\n'
        '  cluster_address = "[::]:8201"\n  tls_cert_file = "/vault/tls/tls.crt"\n'
        '  tls_key_file = "/vault/tls/tls.key"\n}\nstorage "file" {\n  path = "/vault/data"\n}\n'
    )
    sets = [arg for k, v in values.items() for arg in ("--set", f"{k}={v}")]
    tolerations = '[{"key":"capacity.lab/dedicated","operator":"Equal","value":"platform","effect":"NoSchedule"}]'
    run(
        [
            binary("helm"),
            "--kube-context",
            CONTEXT,
            "upgrade",
            "--install",
            "vault",
            "hashicorp/vault",
            "--version",
            LOCK["charts"]["vault"]["version"],
            "-n",
            "capacity-system",
            *sets,
            "--set-json",
            f"server.tolerations={tolerations}",
            "--set-string",
            f"server.standalone.config={standalone}",
        ],
        timeout=600,
    )
    wait_for(
        "vault pod running",
        lambda: kubectl_json("-n", "capacity-system", "get", "pod", "vault-0")["status"].get("phase") == "Running",
        timeout=600,
    )
    material = secrets.init_and_unseal()
    values_now = secrets.generate_secrets()
    secrets.configure(material, values_now)
    run(
        [
            binary("helm"),
            "--kube-context",
            CONTEXT,
            "upgrade",
            "--install",
            "external-secrets",
            "external-secrets/external-secrets",
            "--version",
            LOCK["charts"]["externalSecrets"]["version"],
            "-n",
            "capacity-system",
            "--set",
            "installCRDs=true",
            "--set",
            "resources.requests.cpu=20m",
            "--set",
            "resources.requests.memory=64Mi",
            "--set",
            "webhook.resources.requests.cpu=10m",
            "--set",
            "webhook.resources.requests.memory=48Mi",
            "--set",
            "certController.resources.requests.cpu=10m",
            "--set",
            "certController.resources.requests.memory=48Mi",
        ],
        timeout=600,
    )
    rollout("capacity-system", "deployment/external-secrets")
    rollout("capacity-system", "deployment/external-secrets-webhook")
    # Realm JSON (with generated test-user passwords) is stored in Vault too.
    secrets.put_value("keycloak/realm", {"realm": identity.realm_json(with_e2e_client=True)})
    root_pem = (PKI / "root-ca.crt").read_text()
    apply(manifests.dump(secrets.external_secret_manifests(root_pem)), server_side=True)
    secrets.wait_external_secrets()


# --------------------------------------------------------------------------------- database
@contextlib.contextmanager
def port_forward(namespace: str, target: str, local: int, remote: int):
    """Short-lived `kubectl port-forward` for ONE client connection.

    Recent kubectl versions end the forward when a forwarded stream closes with an error (which a
    TLS close from libpq can trigger), so callers open one forward per connection instead of
    sharing it.
    """
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [
            binary("kubectl"),
            "--context",
            CONTEXT,
            "-n",
            namespace,
            "port-forward",
            target,
            f"{local}:{remote}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        # kubectl prints "Forwarding from ..." once the listener is up. Probing the port with a
        # throwaway TCP connection is avoided because that alone can end the forward.
        first_line = proc.stdout.readline() if proc.stdout else ""
        if "Forwarding from" not in first_line:
            raise ToolError(f"port-forward to {namespace}/{target} did not start")
        # With local=0 kubectl picks a free port and prints it: "Forwarding from 127.0.0.1:53211 -> 9090".
        chosen = first_line.split("->")[0].rsplit(":", 1)[-1].strip()
        yield f"127.0.0.1:{chosen}"
    finally:
        _stop_tree(proc)


def _stop_tree(proc: subprocess.Popen) -> None:
    """Stop kubectl and any child it started.

    On Windows `kubectl` is often a package-manager shim that launches the real binary as a child;
    terminating only the shim would leave the forward running and the local port occupied.
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)  # noqa: S607 S603
    else:
        proc.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=10)


def migrator_dsn(host: str) -> str:
    """Migration identity over the port-forward; the server certificate is verified against the root CA."""
    password = secrets.read_value("db/migrator", "password")
    hostname, port = host.split(":")
    return (
        f"host={hostname} port={port} dbname=capacity user=migrator password={password} "
        f"sslmode=verify-ca sslrootcert={(PKI / 'root-ca.crt').as_posix()}"
    )


def install_database() -> None:
    apply(manifests.dump(infra_manifests.postgres(LOCK["images"]["postgres"])))
    rollout("capacity-data", "statefulset/postgres")
    from capacity_platform.shared.migrate import apply_migrations

    with port_forward("capacity-data", "svc/postgres", 0, 5432) as host:
        applied = apply_migrations(migrator_dsn(host))
    print(f"  migrations applied: {applied or 'none (already current)'}")
    passwords = {
        db_role: secrets.read_value(f"db/{role}", "password") for role, db_role in secrets.SERVICE_ROLES.items()
    }
    kc = secrets.read_value("db/keycloak", "password")
    with (
        port_forward("capacity-data", "svc/postgres", 0, 5432) as host,
        psycopg.connect(migrator_dsn(host), autocommit=True) as conn,
    ):
        for db_role, pw in passwords.items():
            # Utility statements cannot take bound parameters, so values are quoted by psycopg.sql.
            conn.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(db_role), sql.Literal(pw)))
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'keycloak'").fetchone():
            conn.execute(sql.SQL("CREATE ROLE keycloak LOGIN PASSWORD {}").format(sql.Literal(kc)))
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = 'keycloak'").fetchone():
            conn.execute("CREATE DATABASE keycloak OWNER keycloak")


# --------------------------------------------------------------------------------- identity, observability
def install_identity() -> None:
    apply(manifests.dump(infra_manifests.keycloak(LOCK["images"]["keycloak"])))
    rollout("capacity-identity", "deployment/keycloak", timeout="900s")


def install_observability() -> None:
    i = LOCK["images"]
    apply(manifests.dump(infra_manifests.observability(i["prometheus"], i["kubeStateMetrics"], i["grafana"])))
    rollout("capacity-observability", "statefulset/prometheus")
    rollout("capacity-observability", "deployment/kube-state-metrics")
    rollout("capacity-observability", "deployment/grafana")


# --------------------------------------------------------------------------------- images
def build_images() -> dict[str, str]:
    """Build, push to the local registry and return digest-pinned references."""

    def build(tag: str, dockerfile: str, build_args: dict[str, str] | None = None) -> str:
        args = [arg for k, v in (build_args or {}).items() for arg in ("--build-arg", f"{k}={v}")]
        docker(
            "build", "-f", str(ROOT / dockerfile), "-t", f"{REGISTRY_HOST}/{tag}:dev", *args, str(ROOT), timeout=3600
        )
        docker("push", f"{REGISTRY_HOST}/{tag}:dev", timeout=900)
        return docker(
            "inspect", "--format", "{{index .RepoDigests 0}}", f"{REGISTRY_HOST}/{tag}:dev", quiet=True
        ).stdout.strip()

    images = {
        "platform": build("capacity-platform", "images/platform.Dockerfile"),
        "workload-python": build("workload-python", "images/workload-python.Dockerfile"),
        "w01-latency-api": build("w01-latency-api", "images/workload-dotnet.Dockerfile", {"PROJECT": "LatencyApi"}),
        "w02-burst-api": build("w02-burst-api", "images/workload-dotnet.Dockerfile", {"PROJECT": "BurstApi"}),
    }
    IMAGES_FILE.parent.mkdir(parents=True, exist_ok=True)
    IMAGES_FILE.write_text(json.dumps(images, indent=2))
    return images


def _images() -> dict[str, str]:
    if not IMAGES_FILE.exists():
        raise ToolError("no built images recorded; run `capacity-lab images build` first")
    return json.loads(IMAGES_FILE.read_text())


# --------------------------------------------------------------------------------- platform + workloads
def platform_config(allowed_clients: str) -> None:
    jwks = kubectl("get", "--raw", "/openid/v1/jwks", quiet=True).stdout
    issuer = json.loads(kubectl("get", "--raw", "/.well-known/openid-configuration", quiet=True).stdout)["issuer"]
    policies = {p.name: p.read_text() for p in (ROOT / "configs" / "policies").glob("*.yaml")}
    profile = (ROOT / "configs" / "models" / "inference-profile.yaml").read_text()
    objs = [configmap("sa-jwks", ns, {"jwks.json": jwks}) for ns in PLATFORM_NAMESPACES]
    objs += [
        configmap("capacity-policies", ns, policies)
        for ns in ("capacity-core", "capacity-execution", "capacity-api", "capacity-ai")
    ]
    objs.append(configmap("inference-profile", "capacity-ai", {"inference-profile.yaml": profile}))
    pub = HOME / "keys" / "capability-signing.pub"
    if not pub.exists():
        from capacity_platform.scenarios.capability import generate_keypair

        generate_keypair(HOME / "keys" / "capability-signing.pem", pub)
    objs += [
        configmap("capability-public-key", ns, {"public.pem": pub.read_text()})
        for ns in ("workloads-critical", "workloads-standard", "workloads-batch")
    ]
    apply(manifests.dump(objs))
    (HOME / "release").mkdir(parents=True, exist_ok=True)
    (HOME / "release" / "cluster.json").write_text(json.dumps({"saIssuer": issuer, "allowedClients": allowed_clients}))


def deploy_platform(allowed_clients: str = "capacityctl,capacity-e2e") -> None:
    platform_config(allowed_clients)
    issuer = json.loads((HOME / "release" / "cluster.json").read_text())["saIssuer"]
    apply(manifests.dump(manifests.platform(_images()["platform"], issuer, allowed_clients)))
    for ns, name in (
        ("capacity-core", "capacity-governance"),
        ("capacity-core", "capacity-observer"),
        ("capacity-core", "capacity-capacity"),
        ("capacity-execution", "capacity-executor"),
        ("capacity-ai", "capacity-ai"),
        ("capacity-api", "capacity-api"),
    ):
        rollout(ns, f"deployment/{name}")


def deploy_workloads() -> None:
    img = _images()
    apply(
        manifests.dump(
            manifests.workloads(
                {"w01-latency-api": img["w01-latency-api"], "w02-burst-api": img["w02-burst-api"]},
                img["workload-python"],
            )
        )
    )
    for ns, name in (
        ("workloads-critical", "latency-api"),
        ("workloads-standard", "burst-api"),
        ("workloads-standard", "memory-profile"),
        ("workloads-batch", "cpu-worker"),
    ):
        rollout(ns, f"deployment/{name}")


def seed_grants() -> dict:
    """Map each Keycloak test user's stable `sub` to governance grants (bootstrap only)."""
    import httpx

    from capacity_platform.contracts.enums import HumanRole
    from capacity_platform.governance.authz import Grant
    from capacity_platform.governance.store import GovernanceStore
    from capacity_platform.shared.db import transaction

    admin_pw = secrets.read_value("keycloak/admin", "password")
    verify = str(PKI / "root-ca.crt")
    token = httpx.post(
        "https://localhost:8444/realms/master/protocol/openid-connect/token",
        verify=verify,
        timeout=60,
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": "lab-bootstrap-admin",
            "password": admin_pw,
        },
    ).json()["access_token"]
    users = httpx.get(
        "https://localhost:8444/admin/realms/capacity/users?max=50",
        verify=verify,
        timeout=60,
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    ids = {u["username"]: u["id"] for u in users}
    demo = frozenset({"workloads-critical", "workloads-standard", "workloads-batch", "*cluster*"})
    issuer = manifests.KEYCLOAK_ISSUER
    pw = secrets.read_value("db/governance", "password")
    with port_forward("capacity-data", "svc/postgres", 0, 5432) as host:
        hostname, port = host.split(":")
        dsn = (
            f"host={hostname} port={port} dbname=capacity user=svc_governance password={pw} "
            f"sslmode=verify-ca sslrootcert={(PKI / 'root-ca.crt').as_posix()}"
        )
        with psycopg.connect(dsn, row_factory=psycopg.rows.dict_row) as conn, transaction(conn):
            store = GovernanceStore(conn)
            active = {
                r["grant_id"]: r["subject_key"]
                for r in conn.execute(
                    "SELECT grant_id, subject_key FROM governance.role_grant WHERE revoked_at IS NULL"
                )
            }
            for user, roles in identity.TEST_USERS.items():
                subject = f"human:{issuer}#{ids[user]}"
                for role in roles:
                    prefix = f"lab-{user}-{role.lower()}"
                    # A re-imported realm gives users new IDs: revoke grants for the old subject.
                    for gid, old_subject in active.items():
                        if gid.startswith(prefix) and old_subject != subject:
                            store.revoke_grant(gid, "capacity-lab-bootstrap")
                    gid = f"{prefix}-{ids[user][:8]}"
                    if gid not in active:
                        store.add_grant(
                            Grant(gid, subject, HumanRole(role), "capacity-lab", demo, 1), "capacity-lab-bootstrap"
                        )
    return {u: ids.get(u) for u in identity.TEST_USERS}


def apply_dynamic_network(facts: dict) -> None:
    from capacity_platform.lab.cluster import render_dynamic_network

    apply(manifests.dump(render_dynamic_network(facts)))
    (HOME / "release").mkdir(parents=True, exist_ok=True)
    (HOME / "release" / "network.json").write_text(json.dumps(facts, indent=2))
