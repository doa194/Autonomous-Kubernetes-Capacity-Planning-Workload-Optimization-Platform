"""Cluster lifecycle stages: preflight, registry, kind, Calico, baseline policy, network render.

Each stage is idempotent: re-running it converges instead of duplicating (S03-TEST-013).
Destructive deletion is dry-run by default and needs the cluster name typed as confirmation;
it refuses while the only copy of the platform database has no verified backup (S03 §7.3).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import shutil
import time
import urllib.request
from pathlib import Path

from capacity_platform.lab.tools import (
    CLUSTER,
    CONTEXT,
    HOME,
    ROOT,
    TOOLS,
    ToolError,
    apply,
    binary,
    docker,
    kubectl,
    kubectl_json,
    run,
    wait_for,
)

LOCK = json.loads((ROOT / "infra" / "versions.lock.json").read_text())
REGISTRY = "capacity-registry"
REGISTRY_HOST = "localhost:5001"


def ensure_kind() -> None:
    """Download the pinned kind binary into .tools/bin once; refuse it if the checksum differs."""
    target = TOOLS / "kind.exe"
    if target.exists():
        return
    TOOLS.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(LOCK["kind"]["url"], timeout=120) as resp:  # noqa: S310 - pinned https URL
        data = resp.read()
    if hashlib.sha256(data).hexdigest() != LOCK["kind"]["binarySha256"]:
        raise ToolError("kind download does not match the pinned checksum; not installed")
    target.write_bytes(data)


def ensure_helm_repos() -> None:
    for name, key in (("hashicorp", "vault"), ("external-secrets", "externalSecrets")):
        run([binary("helm"), "repo", "add", name, LOCK["charts"][key]["repo"], "--force-update"], quiet=True)
    run([binary("helm"), "repo", "update", "hashicorp", "external-secrets"], quiet=True, timeout=300)


def preflight(min_free_gib: float = 30.0, min_docker_mem_gib: float = 10.0) -> dict:
    report: dict = {"checks": {}, "ok": True}
    ensure_kind()

    def check(name: str, ok: bool, detail) -> None:
        report["checks"][name] = {"ok": ok, "detail": detail}
        report["ok"] &= ok

    info = json.loads(docker("info", "--format", "{{json .}}", quiet=True).stdout)
    mem_gib = info.get("MemTotal", 0) / 2**30
    check("docker_memory", mem_gib >= min_docker_mem_gib, f"{mem_gib:.1f} GiB available to Docker/WSL2")
    check("docker_cpus", info.get("NCPU", 0) >= 4, f"{info.get('NCPU')} CPUs")
    free = shutil.disk_usage(Path(info.get("DockerRootDir", "/")).anchor or "C:\\").free / 2**30
    check("disk_free", free >= min_free_gib, f"{free:.0f} GiB free")
    kind_hash = hashlib.sha256((TOOLS / "kind.exe").read_bytes()).hexdigest() if (TOOLS / "kind.exe").exists() else None
    check("kind_pinned", kind_hash == LOCK["kind"]["binarySha256"], LOCK["kind"]["version"])
    for tool in ("kubectl", "helm", "docker"):
        try:
            binary(tool)
            check(f"tool_{tool}", True, "found")
        except ToolError as exc:
            check(f"tool_{tool}", False, str(exc))
    from capacity_platform.watchdog.main import windows_available_gib

    avail = windows_available_gib()
    check("host_available_memory", avail is None or avail >= 2.0, f"{avail:.1f} GiB" if avail else "not windows")
    return report


def ensure_registry() -> None:
    state = docker("inspect", "-f", "{{.State.Running}}", REGISTRY, check=False, quiet=True)
    if state.returncode != 0:
        docker(
            "run", "-d", "--restart=always", "-p", "127.0.0.1:5001:5000", "--name", REGISTRY, LOCK["images"]["registry"]
        )
    elif state.stdout.strip() != "true":
        docker("start", REGISTRY)


def cluster_exists() -> bool:
    out = run([binary("kind"), "get", "clusters"], quiet=True).stdout.split()
    return CLUSTER in out


def create_cluster() -> None:
    if cluster_exists():
        print("  cluster already exists; reusing")
    else:
        run(
            [
                binary("kind"),
                "create",
                "cluster",
                "--name",
                CLUSTER,
                "--image",
                LOCK["nodeImage"],
                "--config",
                str(ROOT / "infra" / "kind" / "cluster.yaml"),
                "--wait",
                "0s",
            ],
            timeout=900,
        )
    # Containerd on every node resolves localhost:5001 to the registry container (kind docs pattern).
    nodes = run([binary("kind"), "get", "nodes", "--name", CLUSTER], quiet=True).stdout.split()
    hosts = f'[host."http://{REGISTRY}:5000"]\n'
    for node in nodes:
        docker("exec", node, "mkdir", "-p", f"/etc/containerd/certs.d/{REGISTRY_HOST}", quiet=True)
        run(
            [
                binary("docker"),
                "exec",
                "-i",
                node,
                "cp",
                "/dev/stdin",
                f"/etc/containerd/certs.d/{REGISTRY_HOST}/hosts.toml",
            ],
            input_text=hosts,
            quiet=True,
        )
    connected = docker("inspect", "-f", "{{json .NetworkSettings.Networks.kind}}", REGISTRY, quiet=True).stdout
    if connected.strip() in ("null", ""):
        docker("network", "connect", "kind", REGISTRY)


def _verified_manifest(key: str) -> str:
    meta = LOCK["manifests"][key]
    cached = TOOLS / f"{key}-{meta['version']}.yaml"
    if not cached.exists():
        with urllib.request.urlopen(meta["url"], timeout=60) as resp:  # noqa: S310 - pinned https URL
            cached.write_bytes(resp.read())
    digest = hashlib.sha256(cached.read_bytes()).hexdigest()
    if digest != meta["sha256"]:
        raise ToolError(f"{key} manifest hash mismatch: {digest}")
    return cached.read_text(encoding="utf-8")


def install_calico() -> None:
    manifest = _verified_manifest("calico")
    # Align Calico's IP pool with the kind podSubnet (S03 §5).
    manifest = manifest.replace(
        '# - name: CALICO_IPV4POOL_CIDR\n            #   value: "192.168.0.0/16"',
        '- name: CALICO_IPV4POOL_CIDR\n              value: "192.168.128.0/17"',
    )
    apply(manifest, server_side=True)
    wait_for("calico-node ready on all nodes", lambda: _ds_ready("kube-system", "calico-node"), timeout=900)
    wait_for("all nodes Ready", _nodes_ready, timeout=600)


def _ds_ready(ns: str, name: str) -> bool:
    ds = kubectl_json("-n", ns, "get", "daemonset", name)
    st = ds["status"]
    return st.get("desiredNumberScheduled", 0) > 0 and st.get("numberReady") == st.get("desiredNumberScheduled")


def _nodes_ready() -> bool:
    nodes = kubectl_json("get", "nodes")["items"]
    return len(nodes) == 4 and all(
        any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"]) for n in nodes
    )


def apply_base() -> None:
    kubectl("apply", "-k", str(ROOT / "infra" / "kustomize" / "base"), timeout=300)


def network_facts() -> dict:
    kind_net = json.loads(docker("network", "inspect", "kind", quiet=True).stdout)[0]
    subnet = next(c["Subnet"] for c in kind_net["IPAM"]["Config"] if ":" not in c["Subnet"])
    cp = kubectl_json("get", "node", f"{CLUSTER}-control-plane")
    cp_ip = next(a["address"] for a in cp["status"]["addresses"] if a["type"] == "InternalIP")
    host_ip = run(
        [binary("docker"), "run", "--rm", "--network", "kind", "busybox:1.37", "nslookup", "host.docker.internal"],
        check=False,
        quiet=True,
    ).stdout
    gateway = next(
        (
            line.split()[-1]
            for line in host_ip.splitlines()
            if line.strip().startswith("Address")
            and "53" not in line.split()[-1]
            and ipaddress.ip_address(line.split()[-1].split("%")[0]).version == 4
        ),
        None,
    )
    return {"kindSubnet": subnet, "apiServerIp": cp_ip, "hostGatewayIp": gateway}


def render_dynamic_network(facts: dict) -> list[dict]:
    api = [{"ipBlock": {"cidr": f"{facts['apiServerIp']}/32"}}, {"ipBlock": {"cidr": "10.96.0.1/32"}}]
    api_ports = [{"protocol": "TCP", "port": 6443}, {"protocol": "TCP", "port": 443}]
    node_src = [{"ipBlock": {"cidr": facts["kindSubnet"]}}]

    def np(name, ns, selector, egress=None, ingress=None):
        spec = {"podSelector": {"matchLabels": {"app.kubernetes.io/name": selector}}, "policyTypes": []}
        if egress:
            spec["policyTypes"].append("Egress")
            spec["egress"] = egress
        if ingress:
            spec["policyTypes"].append("Ingress")
            spec["ingress"] = ingress
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": name, "namespace": ns, "labels": {"capacity.lab/rendered": "network-dynamic"}},
            "spec": spec,
        }

    objs = [
        np("observer-kube-api", "capacity-core", "capacity-observer", egress=[{"to": api, "ports": api_ports}]),
        np("executor-kube-api", "capacity-execution", "capacity-executor", egress=[{"to": api, "ports": api_ports}]),
        np("prometheus-kube-api", "capacity-observability", "prometheus", egress=[{"to": api, "ports": api_ports}]),
        np("ksm-kube-api", "capacity-observability", "kube-state-metrics", egress=[{"to": api, "ports": api_ports}]),
        # Host access arrives through the platform node's NodePort (localhost-bound on Windows).
        np(
            "api-nodeport",
            "capacity-api",
            "capacity-api",
            ingress=[{"from": node_src, "ports": [{"protocol": "TCP", "port": 8443}]}],
        ),
        np(
            "keycloak-nodeport",
            "capacity-identity",
            "keycloak",
            ingress=[{"from": node_src, "ports": [{"protocol": "TCP", "port": 8443}]}],
        ),
        np(
            "grafana-nodeport",
            "capacity-observability",
            "grafana",
            ingress=[{"from": node_src, "ports": [{"protocol": "TCP", "port": 3000}]}],
        ),
    ]
    if facts.get("hostGatewayIp"):
        # Only the ai Pod may reach the protected host model gateway (S07 §7.3).
        objs.append(
            np(
                "ai-model-gateway",
                "capacity-ai",
                "capacity-ai",
                egress=[
                    {
                        "to": [{"ipBlock": {"cidr": f"{facts['hostGatewayIp']}/32"}}],
                        "ports": [{"protocol": "TCP", "port": 11500}],
                    }
                ],
            )
        )
    return objs


def _verified_backups() -> list[str]:
    """Backup folders whose isolated restore check succeeded (see lab/backup.py)."""
    verified = []
    for check in (HOME / "backups").glob("*/restore-verification.json"):
        if json.loads(check.read_text()).get("restored"):
            verified.append(str(check.parent))
    return sorted(verified)


def destroy(confirm: str | None, dry_run: bool) -> dict:
    report = {
        "cluster": CLUSTER,
        "exists": cluster_exists(),
        "context": CONTEXT,
        "persistentVolumes": "PostgreSQL, Prometheus (inside kind node containers)",
        "backups": _verified_backups(),
    }
    if dry_run or confirm != CLUSTER:
        report["action"] = "dry-run only; pass --confirm capacity-lab to delete"
        return report
    if not report["backups"]:
        raise ToolError(
            "refusing: no verified encrypted backup exists for the platform database "
            "(run `capacity-lab backup create` and `capacity-lab backup verify` first)"
        )
    run([binary("kind"), "delete", "cluster", "--name", CLUSTER], timeout=600)
    report["action"] = "deleted"
    report["deletedAt"] = time.time()
    return report
