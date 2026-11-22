"""Recovery after a Docker Desktop or host restart (`capacity-lab resume`).

Why this file exists: a restart of Docker Desktop restarts all kind node containers. Three things
then need attention, observed on the reference host:
* the node containers can get new IP addresses, so the NetworkPolicies rendered from the old
  API-server address block every Kubernetes API client;
* Vault restarts sealed (single-share manual unseal) and occasionally hangs in start-up;
* Pods that crashed while the network was down sit in long CrashLoopBackOff delays.
"""

from __future__ import annotations

import json
import subprocess

from capacity_platform.lab import cluster, install, secrets
from capacity_platform.lab.tools import HOME, kubectl, kubectl_json, wait_for

# Pods that talk to the Kubernetes API and therefore depend on the rendered API-server rules.
API_CLIENTS = [
    ("capacity-core", "app.kubernetes.io/name=capacity-observer"),
    ("capacity-execution", "app.kubernetes.io/name=capacity-executor"),
    ("capacity-observability", "app.kubernetes.io/name=kube-state-metrics"),
    ("capacity-observability", "app.kubernetes.io/name=prometheus"),
]


def resume() -> dict:
    wait_for("all nodes Ready", _nodes_ready, timeout=900)
    old = (
        json.loads((HOME / "release" / "network.json").read_text())
        if (HOME / "release" / "network.json").exists()
        else {}
    )
    facts = cluster.network_facts()
    install.apply_dynamic_network(facts)
    changed = {k: (old.get(k), v) for k, v in facts.items() if old.get(k) != v}
    if changed:
        for namespace, selector in API_CLIENTS:
            kubectl("-n", namespace, "delete", "pod", "-l", selector, "--wait=false", check=False, quiet=True)
    _restart_crash_looping()
    _unseal_vault()
    return {"networkFactsChanged": changed, "vault": "unsealed"}


def _nodes_ready() -> bool:
    nodes = kubectl_json("get", "nodes")["items"]
    return bool(nodes) and all(
        any(c["type"] == "Ready" and c["status"] == "True" for c in n["status"]["conditions"]) for n in nodes
    )


def _restart_crash_looping() -> None:
    for pod in kubectl_json("get", "pods", "-A")["items"]:
        statuses = pod.get("status", {}).get("containerStatuses") or []
        if any((s.get("state", {}).get("waiting") or {}).get("reason") == "CrashLoopBackOff" for s in statuses):
            kubectl(
                "-n",
                pod["metadata"]["namespace"],
                "delete",
                "pod",
                pod["metadata"]["name"],
                "--wait=false",
                check=False,
                quiet=True,
            )


def _vault_answers() -> bool:
    try:
        proc = kubectl(
            "-n",
            "capacity-system",
            "exec",
            "vault-0",
            "--",
            "vault",
            "status",
            "-format=json",
            check=False,
            quiet=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:  # a hung Vault never answers `vault status`
        return False
    return proc.stdout.strip().startswith("{")


def _unseal_vault() -> None:
    wait_for(
        "vault Pod running",
        lambda: (
            kubectl_json("-n", "capacity-system", "get", "pod", "vault-0").get("status", {}).get("phase") == "Running"
        ),
        timeout=600,
    )
    if not _vault_answers():  # hung in start-up: a fresh Pod starts cleanly
        kubectl("-n", "capacity-system", "delete", "pod", "vault-0", "--wait=true", timeout=300)
        wait_for("vault answering", _vault_answers, timeout=600)
    secrets.init_and_unseal()
