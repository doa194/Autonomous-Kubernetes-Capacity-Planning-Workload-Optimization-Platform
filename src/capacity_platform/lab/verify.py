"""Operational verification against the live cluster (S03 §10, S07 §10, S08 §12, S30).

These checks test real enforcement, not YAML presence: probe Pods attempt allowed and
forbidden TCP connections through Calico, `kubectl auth can-i --as=<sa>` interrogates real
RBAC, and server-side dry-run requests hit the real PSA/CEL admission chain. Every verdict
is `pass`, `fail` or `blocked` with the observed evidence; nothing is assumed.
"""

from __future__ import annotations

import json
import time
import uuid

import yaml

from capacity_platform.lab.cluster import LOCK
from capacity_platform.lab.tools import CLUSTER, HOME, ToolError, apply, binary, kubectl, kubectl_json, run

SAFE_CONTAINER = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}
SAFE_POD = {"runAsNonRoot": True, "runAsUser": 65534, "seccompProfile": {"type": "RuntimeDefault"}}


def _python_digest() -> str:
    ref = run(
        [binary("docker"), "inspect", "--format", "{{index .RepoDigests 0}}", LOCK["images"]["pythonBase"]], quiet=True
    ).stdout.strip()
    return ref  # e.g. python@sha256:...


def _pod(name: str, namespace: str, labels: dict, command: list[str], image: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "securityContext": SAFE_POD,
            "nodeSelector": {"capacity.lab/pool": "experiment"},
            "containers": [
                {
                    "name": "probe",
                    "image": image,
                    "command": command,
                    "resources": {
                        "requests": {"cpu": "10m", "memory": "32Mi"},
                        "limits": {"cpu": "100m", "memory": "64Mi"},
                    },
                    "securityContext": SAFE_CONTAINER,
                }
            ],
        },
    }


CONNECT = (
    "import socket,sys\n"
    "try:\n socket.create_connection((sys.argv[1], int(sys.argv[2])), 4); print('CONNECTED')\n"
    "except socket.timeout: print('TIMEOUT')\n"
    "except OSError as e: print('ERROR', type(e).__name__)\n"
)


def _probe(namespace: str, labels: dict, host: str, port: int, image: str) -> str:
    name = f"probe-{uuid.uuid4().hex[:6]}"
    apply(yaml.safe_dump(_pod(name, namespace, labels, ["python", "-c", CONNECT, host, str(port)], image)))
    try:
        for _ in range(60):
            phase = kubectl_json("-n", namespace, "get", "pod", name)["status"].get("phase")
            if phase in ("Succeeded", "Failed"):
                break
            time.sleep(2)
        return kubectl("-n", namespace, "logs", name, check=False, quiet=True).stdout.strip() or "NO_OUTPUT"
    finally:
        kubectl("-n", namespace, "delete", "pod", name, "--wait=false", check=False, quiet=True)


def network_enforced() -> dict:
    """Fail-closed gate: a dedicated echo target must be reachable ONLY from the approved source."""
    image = _python_digest()
    target_ns = "workloads-standard"
    server = _pod(
        "netcheck-target",
        target_ns,
        {"capacity.lab/demo-service": "true", "app": "netcheck"},
        ["python", "-c", "import http.server as h;h.HTTPServer(('',8080),h.SimpleHTTPRequestHandler).serve_forever()"],
        image,
    )
    server["spec"]["containers"][0]["workingDir"] = "/tmp"
    server["spec"]["volumes"] = [{"name": "tmp", "emptyDir": {}}]
    server["spec"]["containers"][0]["volumeMounts"] = [{"name": "tmp", "mountPath": "/tmp"}]
    apply(yaml.safe_dump(server))
    kubectl("-n", target_ns, "wait", "--for=condition=Ready", "pod/netcheck-target", "--timeout=180s", timeout=200)
    ip = kubectl_json("-n", target_ns, "get", "pod", "netcheck-target")["status"]["podIP"]
    try:
        allowed = _probe("capacity-experiments", {"capacity.lab/template": "load-driver-v1"}, ip, 8080, image)
        unlabeled = _probe("capacity-experiments", {"app": "intruder"}, ip, 8080, image)
        cross = _probe("workloads-batch", {"app": "intruder"}, ip, 8080, image)
        dns = _probe(
            "capacity-experiments",
            {"capacity.lab/template": "load-driver-v1"},
            "kube-dns.kube-system.svc.cluster.local",
            53,
            image,
        )
    finally:
        kubectl("-n", target_ns, "delete", "pod", "netcheck-target", "--wait=false", check=False, quiet=True)
    checks = {
        "approved_driver_to_demo": allowed == "CONNECTED",
        "unlabeled_experiment_pod_denied": allowed == "CONNECTED" and unlabeled == "TIMEOUT",
        "cross_namespace_denied": cross == "TIMEOUT",
        "dns_allowed": dns == "CONNECTED",
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "observed": {"allowed": allowed, "unlabeled": unlabeled, "cross": cross, "dns": dns},
    }


def _can_i(verb: str, resource: str, namespace: str, sa: str) -> bool:
    out = kubectl("auth", "can-i", verb, resource, "-n", namespace, f"--as={sa}", check=False, quiet=True)
    return out.stdout.strip() == "yes"


RBAC_MATRIX = [
    # (subject, verb, resource, namespace, expected)
    ("system:serviceaccount:capacity-core:capacity-observer-sa", "list", "pods", "workloads-batch", True),
    ("system:serviceaccount:capacity-core:capacity-observer-sa", "patch", "deployments", "workloads-batch", False),
    ("system:serviceaccount:capacity-core:capacity-observer-sa", "get", "secrets", "capacity-core", False),
    ("system:serviceaccount:capacity-execution:capacity-executor-sa", "patch", "deployments", "workloads-batch", True),
    ("system:serviceaccount:capacity-execution:capacity-executor-sa", "patch", "deployments", "capacity-core", False),
    (
        "system:serviceaccount:capacity-execution:capacity-executor-sa",
        "update",
        "deployments/scale",
        "workloads-batch",
        False,
    ),
    ("system:serviceaccount:capacity-execution:capacity-executor-sa", "delete", "pods", "workloads-batch", False),
    ("system:serviceaccount:capacity-execution:capacity-executor-sa", "get", "secrets", "workloads-batch", False),
    ("system:serviceaccount:capacity-ai:capacity-ai-sa", "get", "pods", "workloads-batch", False),
    ("system:serviceaccount:capacity-ai:capacity-ai-sa", "patch", "deployments", "workloads-batch", False),
    ("system:serviceaccount:capacity-api:capacity-api-sa", "patch", "deployments", "workloads-critical", False),
    ("system:serviceaccount:capacity-core:capacity-engine-sa", "list", "pods", "workloads-batch", False),
    ("system:serviceaccount:capacity-core:capacity-governance-sa", "patch", "deployments", "workloads-batch", False),
    ("system:serviceaccount:capacity-experiments:capacity-driver-sa", "list", "pods", "capacity-experiments", False),
    ("system:serviceaccount:workloads-batch:w04-sa", "list", "pods", "workloads-batch", False),
    ("system:serviceaccount:capacity-system:capacity-runner", "create", "jobs", "capacity-experiments", True),
    ("system:serviceaccount:capacity-system:capacity-runner", "patch", "deployments", "workloads-critical", False),
    ("system:serviceaccount:capacity-system:capacity-watchdog", "delete", "jobs", "capacity-experiments", True),
    ("system:serviceaccount:capacity-system:capacity-watchdog", "delete", "jobs", "workloads-batch", False),
    ("system:serviceaccount:capacity-system:capacity-watchdog", "patch", "deployments", "workloads-batch", False),
]


def rbac_matrix() -> dict:
    rows = []
    for sa, verb, resource, ns, expected in RBAC_MATRIX:
        actual = _can_i(verb, resource, ns, sa)
        rows.append(
            {
                "subject": sa.split(":")[-1],
                "verb": verb,
                "resource": resource,
                "namespace": ns,
                "expected": expected,
                "actual": actual,
                "ok": actual == expected,
            }
        )
    return {"ok": all(r["ok"] for r in rows), "rows": rows}


def _dry_run(manifest: dict, as_user: str | None = None) -> tuple[bool, str]:
    args = ["create", "--dry-run=server", "-f", "-"]
    if as_user:
        args += [f"--as={as_user}"]
    out = kubectl(*args, input_text=json.dumps(manifest), check=False, quiet=True)
    return out.returncode == 0, (out.stderr or out.stdout).strip()[-300:]


def admission_negatives() -> dict:
    image = _python_digest()
    base = _pod("adm-test", "workloads-batch", {"app": "adm"}, ["sleep", "1"], image)
    cases = {}
    privileged = json.loads(json.dumps(base))
    privileged["spec"]["containers"][0]["securityContext"] = {"privileged": True}
    cases["privileged_pod_denied"] = _dry_run(privileged)
    no_limits = json.loads(json.dumps(base))
    del no_limits["spec"]["containers"][0]["resources"]["limits"]
    cases["missing_limits_denied"] = _dry_run(no_limits)
    tagged = json.loads(json.dumps(base))
    tagged["spec"]["containers"][0]["image"] = "python:3.13-slim"
    cases["tag_only_image_denied"] = _dry_run(tagged)
    token = json.loads(json.dumps(base))
    token["spec"]["automountServiceAccountToken"] = True
    cases["token_automount_denied"] = _dry_run(token)
    writable = json.loads(json.dumps(base))
    writable["spec"]["containers"][0]["securityContext"]["readOnlyRootFilesystem"] = False
    cases["writable_root_denied"] = _dry_run(writable)
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": "rogue",
            "namespace": "capacity-experiments",
            "labels": {"capacity.lab/template": "load-driver-v1"},
        },
        "spec": {
            "activeDeadlineSeconds": 60,
            "ttlSecondsAfterFinished": 60,
            "template": {"spec": {**base["spec"], "serviceAccountName": "default"}},
        },
    }
    cases["rogue_experiment_job_denied"] = _dry_run(job, "system:serviceaccount:capacity-system:capacity-runner")
    verdicts = {name: {"denied": not allowed, "message": msg} for name, (allowed, msg) in cases.items()}
    good = json.loads(json.dumps(base))
    allowed, msg = _dry_run(good)
    verdicts["compliant_pod_allowed"] = {"denied": not allowed, "message": msg}
    ok = (
        all(v["denied"] for k, v in verdicts.items() if k != "compliant_pod_allowed")
        and not verdicts["compliant_pod_allowed"]["denied"]
    )
    return {"ok": ok, "cases": verdicts}


def executor_field_guard() -> dict:
    """POL-EXEC-001 via impersonated server-side dry-run patches against a live demo Deployment."""
    sa = "system:serviceaccount:capacity-execution:capacity-executor-sa"
    try:
        dep = kubectl_json("-n", "workloads-batch", "get", "deployment", "cpu-worker")
    except ToolError:
        return {"ok": False, "verdict": "blocked", "reason": "cpu-worker not deployed"}
    idx = 0

    def patch(ops: list[dict]) -> tuple[bool, str]:
        out = kubectl(
            "-n",
            "workloads-batch",
            "patch",
            "deployment",
            "cpu-worker",
            "--type=json",
            "--dry-run=server",
            f"--as={sa}",
            "-p",
            json.dumps(ops),
            check=False,
            quiet=True,
        )
        return out.returncode == 0, (out.stderr or out.stdout).strip()[-200:]

    resources_ok = patch(
        [{"op": "replace", "path": f"/spec/template/spec/containers/{idx}/resources/requests/cpu", "value": "140m"}]
    )
    replicas = patch([{"op": "replace", "path": "/spec/replicas", "value": 3}])
    image = patch(
        [{"op": "replace", "path": f"/spec/template/spec/containers/{idx}/image", "value": "evil@sha256:" + "0" * 64}]
    )
    sa_change = patch([{"op": "replace", "path": "/spec/template/spec/serviceAccountName", "value": "default"}])
    del dep
    return {
        "ok": resources_ok[0] and not replicas[0] and not image[0] and not sa_change[0],
        "resourcesPatchAllowed": resources_ok,
        "replicasDenied": not replicas[0],
        "imageDenied": not image[0],
        "serviceAccountDenied": not sa_change[0],
    }


def placement() -> dict:
    pods = kubectl_json("get", "pods", "-A")["items"]
    nodes = {
        n["metadata"]["name"]: n["metadata"]["labels"].get("capacity.lab/pool")
        for n in kubectl_json("get", "nodes")["items"]
    }
    wrong = []
    for p in pods:
        ns, node = p["metadata"]["namespace"], p["spec"].get("nodeName")
        if node is None:
            continue
        if ns.startswith("workloads-") or ns == "capacity-experiments":
            if nodes.get(node) != "experiment":
                wrong.append(f"{ns}/{p['metadata']['name']} on {node}")
        if ns in ("capacity-api", "capacity-core", "capacity-ai", "capacity-execution", "capacity-data"):
            if nodes.get(node) != "platform":
                wrong.append(f"{ns}/{p['metadata']['name']} on {node}")
    return {"ok": not wrong, "violations": wrong}


def topology() -> dict:
    nodes = kubectl_json("get", "nodes")["items"]
    custom = [
        d["metadata"]["name"]
        for d in kubectl_json("get", "deployments", "-A")["items"]
        if d["metadata"].get("labels", {}).get("app.kubernetes.io/part-of") == "capacity-platform"
    ]
    roles = {"control-plane": 0, "platform": 0, "experiment": 0}
    for n in nodes:
        labels = n["metadata"]["labels"]
        roles[
            "control-plane"
            if "node-role.kubernetes.io/control-plane" in labels
            else labels.get("capacity.lab/pool", "?")
        ] += 1
    return {
        "ok": roles == {"control-plane": 1, "platform": 1, "experiment": 2} and len(custom) == 6,
        "nodes": roles,
        "platformDeployments": sorted(custom),
    }


def host_identities() -> dict:
    """Separate, narrow kubeconfigs for the scenario runner and watchdog (S07 §4.3)."""
    server = kubectl("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}", quiet=True).stdout
    ca = kubectl(
        "config",
        "view",
        "--minify",
        "--raw",
        "-o",
        "jsonpath={.clusters[0].cluster.certificate-authority-data}",
        quiet=True,
    ).stdout
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": CLUSTER, "cluster": {"server": server, "certificate-authority-data": ca}}],
        "contexts": [],
        "users": [],
    }
    for sa, ctx in (("capacity-runner", "capacity-lab-runner"), ("capacity-watchdog", "capacity-lab-watchdog")):
        token = kubectl("-n", "capacity-system", "create", "token", sa, "--duration=24h", quiet=True).stdout.strip()
        config["users"].append({"name": sa, "user": {"token": token}})
        config["contexts"].append({"name": ctx, "context": {"cluster": CLUSTER, "user": sa}})
    path = HOME / "kubeconfig"
    path.write_text(yaml.safe_dump(config))
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {
        "kubeconfig": str(path),
        "contexts": ["capacity-lab-runner", "capacity-lab-watchdog"],
        "tokenLifetime": "24h",
    }


def _scrape_health() -> list[dict]:
    import httpx

    from capacity_platform.lab.install import port_forward

    with port_forward("capacity-observability", "pod/prometheus-0", 0, 9090) as host:
        return httpx.get(f"http://{host}/api/v1/targets?state=active", timeout=20).json()["data"]["activeTargets"]


def observability_targets() -> dict:
    """Every Prometheus scrape target is up, including each platform service's /metrics (S28).

    A target for a Pod that was just replaced can show `down` until service discovery catches up,
    so a failure is only reported if it is still there one scrape interval later.
    """
    targets = _scrape_health()
    if any(t["health"] != "up" for t in targets):
        time.sleep(45)
        targets = _scrape_health()
    jobs: dict[str, list[str]] = {}
    for t in targets:
        jobs.setdefault(t["labels"].get("job", "?"), []).append(t["health"])
    down = {job: h for job, h in jobs.items() if any(x != "up" for x in h)}
    platform_up = sum(1 for t in targets if t["labels"].get("job") == "capacity-platform" and t["health"] == "up")
    return {
        "ok": not down and platform_up >= 6,
        "jobs": {j: len(h) for j, h in jobs.items()},
        "down": down,
        "platformTargetsUp": platform_up,
    }


def run_checks(area: str) -> dict:
    checks: dict[str, dict] = {}

    def record(name: str, fn) -> None:
        try:
            result = fn()
            checks[name] = {"verdict": "pass" if result.get("ok") else "fail", **result}
        except Exception as exc:  # environment missing: blocked, never pass
            checks[name] = {"verdict": "blocked", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}

    if area in ("networking", "all"):
        record("network_default_deny_enforced", network_enforced)
    if area in ("security", "all"):
        record("rbac_least_privilege", rbac_matrix)
        record("admission_negative_cases", admission_negatives)
        record("executor_field_guard", executor_field_guard)
    if area in ("observability", "all"):
        record("observability_targets", observability_targets)
    if area in ("scheduling", "all"):
        record("placement_pools", placement)
        record("topology_and_six_deployments", topology)
    return {"area": area, "checkedAt": time.time(), "checks": checks}
