"""Render Kubernetes manifests that depend on build output or live discovery.

Static policy (namespaces, RBAC, NetworkPolicy, admission) lives in infra/kustomize/base.
Everything that needs an image digest, a discovered address or a generated secret name is
rendered here from typed tables, so the six platform Deployments and the demo workloads are
consistent by construction (same security context, probes, identity token, resources).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import yaml

from capacity_platform.contracts.units import bytes_to_quantity, millicores_to_quantity
from capacity_platform.contracts.workloads import CATALOG

KEYCLOAK_ISSUER = "https://localhost:8444/realms/capacity"
KEYCLOAK_INTERNAL = "https://keycloak.capacity-identity.svc:8443"
RESTRICTED_POD = {
    "runAsNonRoot": True,
    "runAsUser": 10001,
    "runAsGroup": 10001,
    "fsGroup": 10001,
    "seccompProfile": {"type": "RuntimeDefault"},
}
RESTRICTED_CONTAINER = {
    "allowPrivilegeEscalation": False,
    "readOnlyRootFilesystem": True,
    "capabilities": {"drop": ["ALL"]},
}
PLATFORM_PLACEMENT = {
    "nodeSelector": {"capacity.lab/pool": "platform"},
    "tolerations": [
        {"key": "capacity.lab/dedicated", "operator": "Equal", "value": "platform", "effect": "NoSchedule"}
    ],
}


def dump(objs: list[dict]) -> str:
    return "\n---\n".join(yaml.safe_dump(o, sort_keys=False) for o in objs)


@dataclass(frozen=True)
class Role:
    name: str
    namespace: str
    sa: str
    priority: str
    cpu: str
    memory: str
    cpu_limit: str
    memory_limit: str
    kube_api: bool = False
    env: dict[str, str] = field(default_factory=dict)
    node_port: int | None = None
    mtls: bool = False


def roles() -> list[Role]:
    prom = "http://prometheus.capacity-observability.svc:9090"
    common = {"PROMETHEUS_URL": prom, "POLICY_FILE": "/etc/capacity/policies/optimization-policy.yaml"}
    return [
        Role(
            "api",
            "capacity-api",
            "capacity-api-sa",
            "capacity-platform-standard",
            "100m",
            "160Mi",
            "500m",
            "320Mi",
            env={
                "GOVERNANCE_URL": "https://capacity-governance.capacity-core.svc:8444",
                "AI_URL": "https://capacity-ai.capacity-ai.svc:8443",
            },
            node_port=30443,
        ),
        Role(
            "observer",
            "capacity-core",
            "capacity-observer-sa",
            "capacity-platform-critical",
            "150m",
            "256Mi",
            "500m",
            "512Mi",
            kube_api=True,
        ),
        Role(
            "capacity",
            "capacity-core",
            "capacity-engine-sa",
            "capacity-platform-standard",
            "200m",
            "320Mi",
            "1000m",
            "768Mi",
            env=common,
        ),
        Role(
            "ai",
            "capacity-ai",
            "capacity-ai-sa",
            "capacity-platform-standard",
            "150m",
            "256Mi",
            "500m",
            "512Mi",
            env={
                "CAPACITY_URL": "https://capacity-capacity.capacity-core.svc:8443",
                "INFERENCE_PROFILE_FILE": "/etc/capacity/models/inference-profile.yaml",
                "GATEWAY_KEY_FILE": "/var/run/secrets/capacity/gateway/key",
            },
        ),
        Role(
            "governance",
            "capacity-core",
            "capacity-governance-sa",
            "capacity-platform-critical",
            "100m",
            "192Mi",
            "500m",
            "384Mi",
            env={**common, "GOVERNANCE_POLICY_FILE": "/etc/capacity/policies/governance-policy.yaml"},
            mtls=True,
        ),
        Role(
            "executor",
            "capacity-execution",
            "capacity-executor-sa",
            "capacity-platform-critical",
            "100m",
            "192Mi",
            "500m",
            "384Mi",
            kube_api=True,
            env={**common, "VERIFICATION_POLICY_FILE": "/etc/capacity/policies/verification-policy.yaml"},
        ),
    ]


def platform(image: str, sa_issuer: str, allowed_clients: str) -> list[dict]:
    objs: list[dict] = []
    for r in roles():
        app = f"capacity-{r.name if r.name != 'capacity' else 'engine'}"
        svc_name = f"capacity-{r.name}"
        env = {
            "CAPACITY_ROLE": r.name,
            "CAPACITY_DB_DSN_FILE": "/var/run/secrets/capacity/db/dsn",
            "OIDC_ISSUER": KEYCLOAK_ISSUER,
            "OIDC_AUDIENCE": "capacity-api",
            "OIDC_JWKS_URL": f"{KEYCLOAK_INTERNAL}/realms/capacity/protocol/openid-connect/certs",
            "OIDC_ALLOWED_CLIENTS": allowed_clients,
            "OIDC_CA_BUNDLE": "/var/run/secrets/capacity/ca/ca.crt",
            "INTERNAL_CA_BUNDLE": "/var/run/secrets/capacity/ca/ca.crt",
            "SA_ISSUER": sa_issuer,
            "SA_JWKS_FILE": "/var/run/secrets/capacity/sa-jwks/jwks.json",
            "SERVE_TLS_CERT": "/var/run/secrets/capacity/tls/tls.crt",
            "SERVE_TLS_KEY": "/var/run/secrets/capacity/tls/tls.key",
            "PORT": "8443",
            **r.env,
        }
        if r.mtls:
            env |= {"SERVE_MTLS_PORT": "8444", "SERVE_TLS_CLIENT_CA": "/var/run/secrets/capacity/ca/ca.crt"}
        if r.name == "api":
            env |= {
                "CLIENT_TLS_CERT": "/var/run/secrets/capacity/tls/tls.crt",
                "CLIENT_TLS_KEY": "/var/run/secrets/capacity/tls/tls.key",
            }
        mounts = [
            {"name": "tmp", "mountPath": "/tmp"},
            {"name": "db", "mountPath": "/var/run/secrets/capacity/db", "readOnly": True},
            {"name": "tls", "mountPath": "/var/run/secrets/capacity/tls", "readOnly": True},
            {"name": "ca", "mountPath": "/var/run/secrets/capacity/ca", "readOnly": True},
            {"name": "identity", "mountPath": "/var/run/secrets/capacity/token-dir", "readOnly": True},
            {"name": "sa-jwks", "mountPath": "/var/run/secrets/capacity/sa-jwks", "readOnly": True},
            {"name": "policies", "mountPath": "/etc/capacity/policies", "readOnly": True},
        ]
        volumes = [
            {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}},
            {"name": "db", "secret": {"secretName": f"db-{r.name}", "defaultMode": 0o440}},
            {"name": "tls", "secret": {"secretName": f"tls-{r.name}", "defaultMode": 0o440}},
            {"name": "ca", "configMap": {"name": "capacity-trust"}},
            # Audience-bound, 10-minute service identity token for internal calls (S07 §3).
            {
                "name": "identity",
                "projected": {
                    "sources": [
                        {
                            "serviceAccountToken": {
                                "audience": "capacity-internal",
                                "expirationSeconds": 600,
                                "path": "token",
                            }
                        }
                    ]
                },
            },
            {"name": "sa-jwks", "configMap": {"name": "sa-jwks"}},
            {"name": "policies", "configMap": {"name": "capacity-policies"}},
        ]
        if r.name == "ai":
            mounts += [
                {"name": "models", "mountPath": "/etc/capacity/models", "readOnly": True},
                {"name": "gateway", "mountPath": "/var/run/secrets/capacity/gateway", "readOnly": True},
            ]
            volumes += [
                {"name": "models", "configMap": {"name": "inference-profile"}},
                {"name": "gateway", "secret": {"secretName": "model-gateway-key", "defaultMode": 0o440}},
            ]
        ports = [{"name": "https", "containerPort": 8443}] + (
            [{"name": "mtls", "containerPort": 8444}] if r.mtls else []
        )
        probe = {
            "httpGet": {"path": "/health/ready", "port": 8443, "scheme": "HTTPS"},
            "periodSeconds": 10,
            "failureThreshold": 3,
            "timeoutSeconds": 3,
        }
        objs.append(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {
                    "name": svc_name,
                    "namespace": r.namespace,
                    "labels": {"app.kubernetes.io/name": app, "app.kubernetes.io/part-of": "capacity-platform"},
                },
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app.kubernetes.io/name": app}},
                    "strategy": {"type": "Recreate"}
                    if r.name in ("executor", "observer")
                    else {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
                    "template": {
                        # service-name lets Prometheus scrape by the DNS name that is in the TLS certificate.
                        "metadata": {
                            "labels": {
                                "app.kubernetes.io/name": app,
                                "app.kubernetes.io/part-of": "capacity-platform",
                                "capacity.lab/service-name": svc_name,
                            }
                        },
                        "spec": {
                            "serviceAccountName": r.sa,
                            "automountServiceAccountToken": r.kube_api,
                            "priorityClassName": r.priority,
                            "securityContext": RESTRICTED_POD,
                            "terminationGracePeriodSeconds": 30,
                            **PLATFORM_PLACEMENT,
                            "containers": [
                                {
                                    "name": "service",
                                    "image": image,
                                    "args": [r.name],
                                    "env": [{"name": k, "value": v} for k, v in sorted(env.items())],
                                    "ports": ports,
                                    "resources": {
                                        "requests": {"cpu": r.cpu, "memory": r.memory},
                                        "limits": {"cpu": r.cpu_limit, "memory": r.memory_limit},
                                    },
                                    "securityContext": RESTRICTED_CONTAINER,
                                    "volumeMounts": mounts,
                                    "readinessProbe": probe,
                                    "livenessProbe": {
                                        **probe,
                                        "httpGet": {**probe["httpGet"], "path": "/health/live"},
                                        "periodSeconds": 20,
                                    },
                                    "startupProbe": {**probe, "failureThreshold": 30},
                                }
                            ],
                            "volumes": volumes,
                        },
                    },
                },
            }
        )
        service = {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": svc_name, "namespace": r.namespace, "labels": {"app.kubernetes.io/name": app}},
            "spec": {
                "selector": {"app.kubernetes.io/name": app},
                "ports": [
                    {"name": p["name"], "port": p["containerPort"], "targetPort": p["containerPort"]} for p in ports
                ],
            },
        }
        if r.node_port:
            service["spec"]["type"] = "NodePort"
            service["spec"]["ports"][0]["nodePort"] = r.node_port
        objs.append(service)
        # Server certificate (and client usage for the api's mTLS calls) from the intermediate.
        dns = [f"{svc_name}.{r.namespace}.svc", f"{svc_name}.{r.namespace}.svc.cluster.local"]
        if r.node_port:
            dns.append("localhost")
        objs.append(
            {
                "apiVersion": "cert-manager.io/v1",
                "kind": "Certificate",
                "metadata": {"name": f"tls-{r.name}", "namespace": r.namespace},
                "spec": {
                    "secretName": f"tls-{r.name}",
                    "duration": "720h",
                    "renewBefore": "168h",
                    "privateKey": {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"},
                    "usages": ["server auth", "client auth", "digital signature"],
                    "dnsNames": dns,
                    "ipAddresses": ["127.0.0.1"] if r.node_port else [],
                    "issuerRef": {"name": "capacity-intermediate", "kind": "ClusterIssuer"},
                },
            }
        )
    return objs


# ------------------------------------------------------------------------ demo workloads
def workloads(dotnet_images: dict[str, str], python_image: str) -> list[dict]:
    objs: list[dict] = []
    commands = {"w03-memory-profile": ["python", "memory_profile.py"], "w04-cpu-worker": ["python", "cpu_worker.py"]}
    for wid in ("w01-latency-api", "w02-burst-api", "w03-memory-profile", "w04-cpu-worker"):
        e = CATALOG[wid]
        image = dotnet_images[wid] if e.runtime == "dotnet" else python_image
        labels = {
            "app.kubernetes.io/name": e.name,
            "capacity.lab/workload-id": wid,
            "capacity.lab/demo-service": "true",
            "capacity.lab/criticality": e.criticality.value,
        }
        container = {
            "name": e.container,
            "image": image,
            "env": [
                {"name": "WORKLOAD_VERSION", "value": "1"},
                {"name": "CAPABILITY_PUBLIC_KEY", "value": "/etc/capacity/capability/public.pem"},
            ],
            "ports": [{"name": "http", "containerPort": 8080}],
            "resources": {
                "requests": {
                    "cpu": millicores_to_quantity(e.cpu_request_m),
                    "memory": bytes_to_quantity(e.memory_request_bytes),
                },
                "limits": {
                    "cpu": millicores_to_quantity(e.cpu_limit_m),
                    "memory": bytes_to_quantity(e.memory_limit_bytes),
                },
            },
            "securityContext": RESTRICTED_CONTAINER,
            "volumeMounts": [
                {"name": "tmp", "mountPath": "/tmp"},
                {"name": "capability", "mountPath": "/etc/capacity/capability", "readOnly": True},
            ],
            "readinessProbe": {"httpGet": {"path": "/health/ready", "port": 8080}, "periodSeconds": 10},
            "livenessProbe": {"httpGet": {"path": "/health/live", "port": 8080}, "periodSeconds": 20},
        }
        if wid in commands:
            container["command"] = commands[wid]
        objs.append(
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                # Declares direct (not GitOps) ownership so the executor's ownership check can pass.
                "metadata": {
                    "name": e.name,
                    "namespace": e.namespace,
                    "labels": {**labels, "capacity.lab/ownership": "direct"},
                },
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app.kubernetes.io/name": e.name}},
                    "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0}},
                    "template": {
                        "metadata": {"labels": labels},
                        "spec": {
                            "serviceAccountName": f"{wid[:3]}-sa",
                            "automountServiceAccountToken": False,
                            "priorityClassName": e.priority_class,
                            "securityContext": RESTRICTED_POD,
                            "terminationGracePeriodSeconds": 25,
                            "affinity": {
                                "nodeAffinity": {
                                    "requiredDuringSchedulingIgnoredDuringExecution": {
                                        "nodeSelectorTerms": [
                                            {
                                                "matchExpressions": [
                                                    {
                                                        "key": "capacity.lab/pool",
                                                        "operator": "In",
                                                        "values": ["experiment"],
                                                    }
                                                ]
                                            }
                                        ]
                                    }
                                }
                            },
                            "containers": [container],
                            "volumes": [
                                {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}},
                                {"name": "capability", "configMap": {"name": "capability-public-key"}},
                            ],
                        },
                    },
                },
            }
        )
        objs.append(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {"name": e.name, "namespace": e.namespace, "labels": labels},
                "spec": {
                    "selector": {"app.kubernetes.io/name": e.name},
                    "ports": [{"name": "http", "port": 8080, "targetPort": 8080}],
                },
            }
        )
    # W05: CronJob template, suspended by default (S04 §4.5). Scenarios create Jobs from it.
    e5 = CATALOG["w05-analytics-processor"]
    objs.append(
        {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {
                "name": e5.name,
                "namespace": e5.namespace,
                "labels": {"capacity.lab/workload-id": e5.workload_id},
            },
            "spec": {
                "schedule": "0 3 * * *",
                "suspend": True,
                "concurrencyPolicy": "Forbid",
                "startingDeadlineSeconds": 300,
                "jobTemplate": {"spec": _batch_spec("w05-analytics-processor", python_image)},
            },
        }
    )
    objs.append(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "w06-job-template", "namespace": "workloads-batch"},
            "data": {"job.json": json.dumps(_batch_spec("w06-opportunistic-batch", python_image))},
        }
    )
    return objs


def _batch_spec(wid: str, image: str) -> dict:
    e = CATALOG[wid]
    return {
        "backoffLimit": 1,
        "activeDeadlineSeconds": 900,
        "ttlSecondsAfterFinished": 600,
        "template": {
            "metadata": {"labels": {"capacity.lab/workload-id": wid}},
            "spec": {
                "serviceAccountName": f"{wid[:3]}-sa",
                "automountServiceAccountToken": False,
                "restartPolicy": "Never",
                "priorityClassName": e.priority_class,
                "securityContext": RESTRICTED_POD,
                "affinity": {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {"key": "capacity.lab/pool", "operator": "In", "values": ["experiment"]}
                                    ]
                                }
                            ]
                        }
                    }
                },
                "containers": [
                    {
                        "name": e.container,
                        "image": image,
                        "command": ["python", "batch.py"],
                        "env": [
                            {"name": "WORKLOAD_ID", "value": wid},
                            {
                                "name": "RUN_PROFILE",
                                "value": "analytics-small" if wid.startswith("w05") else "batch-short",
                            },
                            {"name": "RUN_SEED", "value": "1"},
                        ],
                        "resources": {
                            "requests": {
                                "cpu": millicores_to_quantity(e.cpu_request_m),
                                "memory": bytes_to_quantity(e.memory_request_bytes),
                            },
                            "limits": {
                                "cpu": millicores_to_quantity(e.cpu_limit_m),
                                "memory": bytes_to_quantity(e.memory_limit_bytes),
                            },
                        },
                        "securityContext": RESTRICTED_CONTAINER,
                        "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                    }
                ],
                "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}],
            },
        },
    }
