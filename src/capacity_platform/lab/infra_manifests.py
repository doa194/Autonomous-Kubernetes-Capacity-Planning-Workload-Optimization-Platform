"""Supporting infrastructure (not platform Deployments): PostgreSQL, Prometheus,
kube-state-metrics, Grafana and Keycloak. Image references are pinned in versions.lock.json
and resolved to digests at install time.

Every component runs on the tainted platform worker with explicit requests/limits. Secret
values never appear here: Pods read them from Secrets that External Secrets materialises
from Vault.
"""

from __future__ import annotations

import hashlib

import yaml

from capacity_platform.lab.manifests import PLATFORM_PLACEMENT
from capacity_platform.lab.tools import ROOT

DASHBOARDS = ROOT / "configs" / "dashboards"

NONROOT = {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}
LOCKED = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}, "readOnlyRootFilesystem": True}


def _deploy(
    name,
    ns,
    image,
    *,
    args=None,
    env=None,
    ports=(),
    mounts=(),
    volumes=(),
    cpu="100m",
    mem="128Mi",
    cpu_lim="500m",
    mem_lim="256Mi",
    uid=10001,
    sa=None,
    probe=None,
    ro=True,
    priority="capacity-platform-standard",
):
    container = {
        "name": name,
        "image": image,
        "ports": [{"containerPort": p, "name": f"p{p}"} for p in ports],
        "resources": {"requests": {"cpu": cpu, "memory": mem}, "limits": {"cpu": cpu_lim, "memory": mem_lim}},
        "securityContext": {**LOCKED, "readOnlyRootFilesystem": ro},
        "volumeMounts": list(mounts),
    }
    if args:
        container["args"] = args
    if env:
        container["env"] = env
    if probe:
        container["readinessProbe"] = probe
    spec = {
        "serviceAccountName": sa or "default",
        "automountServiceAccountToken": bool(sa),
        "priorityClassName": priority,
        "securityContext": {**NONROOT, "runAsUser": uid, "runAsGroup": uid, "fsGroup": uid},
        **PLATFORM_PLACEMENT,
        "containers": [container],
        "volumes": list(volumes),
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns, "labels": {"app.kubernetes.io/name": name}},
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"app.kubernetes.io/name": name}},
            "template": {"metadata": {"labels": {"app.kubernetes.io/name": name}}, "spec": spec},
        },
    }


def _svc(name, ns, ports, node_ports=None):
    spec = {
        "selector": {"app.kubernetes.io/name": name},
        "ports": [{"name": f"p{p}", "port": p, "targetPort": p} for p in ports],
    }
    if node_ports:
        spec["type"] = "NodePort"
        for port, node_port in zip(spec["ports"], node_ports, strict=False):
            port["nodePort"] = node_port
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": ns}, "spec": spec}


def _cert(name, ns, dns, ip=()):
    return {
        "apiVersion": "cert-manager.io/v1",
        "kind": "Certificate",
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "secretName": name,
            "duration": "720h",
            "renewBefore": "168h",
            "privateKey": {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"},
            "usages": ["server auth", "digital signature"],
            "dnsNames": list(dns),
            "ipAddresses": list(ip),
            "issuerRef": {"name": "capacity-intermediate", "kind": "ClusterIssuer"},
        },
    }


def postgres(image: str) -> list[dict]:
    ns = "capacity-data"
    return [
        _cert("tls-postgres", ns, ["postgres.capacity-data.svc", "postgres.capacity-data.svc.cluster.local"]),
        {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": "postgres", "namespace": ns, "labels": {"app.kubernetes.io/name": "postgres"}},
            "spec": {
                "serviceName": "postgres",
                "replicas": 1,
                "selector": {"matchLabels": {"app.kubernetes.io/name": "postgres"}},
                "template": {
                    "metadata": {"labels": {"app.kubernetes.io/name": "postgres"}},
                    "spec": {
                        "automountServiceAccountToken": False,
                        "priorityClassName": "capacity-platform-critical",
                        "securityContext": {**NONROOT, "runAsUser": 999, "runAsGroup": 999, "fsGroup": 999},
                        **PLATFORM_PLACEMENT,
                        "containers": [
                            {
                                "name": "postgres",
                                "image": image,
                                "args": [
                                    "-c",
                                    "ssl=on",
                                    "-c",
                                    "ssl_cert_file=/tls/tls.crt",
                                    "-c",
                                    "ssl_key_file=/tls/tls.key",
                                    "-c",
                                    "max_connections=60",
                                    "-c",
                                    "shared_buffers=96MB",
                                ],
                                "env": [
                                    {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
                                    {"name": "POSTGRES_DB", "value": "capacity"},
                                    {"name": "POSTGRES_USER", "value": "migrator"},
                                    {"name": "POSTGRES_PASSWORD_FILE", "value": "/secrets/migrator-password"},
                                ],
                                "ports": [{"containerPort": 5432, "name": "p5432"}],
                                "resources": {
                                    "requests": {"cpu": "300m", "memory": "512Mi"},
                                    "limits": {"cpu": "1", "memory": "1Gi"},
                                },
                                "securityContext": LOCKED,
                                "readinessProbe": {
                                    "exec": {"command": ["pg_isready", "-U", "migrator", "-d", "capacity"]},
                                    "periodSeconds": 10,
                                },
                                "volumeMounts": [
                                    {"name": "data", "mountPath": "/var/lib/postgresql/data"},
                                    {"name": "run", "mountPath": "/var/run/postgresql"},
                                    {"name": "tmp", "mountPath": "/tmp"},
                                    {"name": "tls", "mountPath": "/tls", "readOnly": True},
                                    {"name": "secrets", "mountPath": "/secrets", "readOnly": True},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "run", "emptyDir": {}},
                            {"name": "tmp", "emptyDir": {}},
                            {"name": "tls", "secret": {"secretName": "tls-postgres", "defaultMode": 0o640}},
                            {"name": "secrets", "secret": {"secretName": "postgres-bootstrap", "defaultMode": 0o440}},
                        ],
                    },
                },
                "volumeClaimTemplates": [
                    {
                        "metadata": {"name": "data"},
                        "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "5Gi"}}},
                    }
                ],
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "postgres", "namespace": ns},
            "spec": {"selector": {"app.kubernetes.io/name": "postgres"}, "ports": [{"name": "pg", "port": 5432}]},
        },
    ]


PROMETHEUS_CONFIG = {
    "global": {"scrape_interval": "15s", "evaluation_interval": "30s"},
    "scrape_configs": [
        {  # cAdvisor through the API-server proxy: verified cluster CA, no insecure TLS skip.
            "job_name": "kubernetes-cadvisor",
            "scheme": "https",
            "scrape_interval": "15s",
            "kubernetes_sd_configs": [{"role": "node"}],
            "tls_config": {"ca_file": "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"},
            "authorization": {"credentials_file": "/var/run/secrets/kubernetes.io/serviceaccount/token"},
            "relabel_configs": [
                {"target_label": "__address__", "replacement": "kubernetes.default.svc:443"},
                {
                    "source_labels": ["__meta_kubernetes_node_name"],
                    "target_label": "__metrics_path__",
                    "replacement": "/api/v1/nodes/$1/proxy/metrics/cadvisor",
                },
                {"source_labels": ["__meta_kubernetes_node_name"], "target_label": "node"},
            ],
            # Drop unbounded/irrelevant series to respect the label-cardinality budget (S12 §3.2).
            "metric_relabel_configs": [
                {
                    "source_labels": ["__name__"],
                    "action": "keep",
                    "regex": "container_cpu_usage_seconds_total|container_memory_working_set_bytes|"
                    "container_memory_rss|container_cpu_cfs_.*",
                },
                {"regex": "id|image|name", "action": "labeldrop"},
            ],
        },
        {
            "job_name": "kube-state-metrics",
            "scrape_interval": "30s",
            "static_configs": [{"targets": ["kube-state-metrics.capacity-observability.svc:8080"]}],
        },
        {  # W01-W04 application exporters (pods labelled as demo services).
            "job_name": "demo-workloads",
            "scrape_interval": "15s",
            "kubernetes_sd_configs": [
                {
                    "role": "pod",
                    "namespaces": {"names": ["workloads-critical", "workloads-standard", "workloads-batch"]},
                }
            ],
            "relabel_configs": [
                {
                    "source_labels": ["__meta_kubernetes_pod_label_capacity_lab_demo_service"],
                    "action": "keep",
                    "regex": "true",
                },
                {
                    "source_labels": ["__meta_kubernetes_pod_ip"],
                    "target_label": "__address__",
                    "replacement": "$1:8080",
                },
                {"source_labels": ["__meta_kubernetes_namespace"], "target_label": "namespace"},
                {"source_labels": ["__meta_kubernetes_pod_name"], "target_label": "pod"},
            ],
        },
        {  # Platform services over TLS, verified against the platform CA.
            "job_name": "capacity-platform",
            "scheme": "https",
            "scrape_interval": "30s",
            "tls_config": {"ca_file": "/etc/prometheus/ca/ca.crt", "insecure_skip_verify": False},
            "kubernetes_sd_configs": [
                {
                    "role": "pod",
                    "namespaces": {"names": ["capacity-api", "capacity-core", "capacity-ai", "capacity-execution"]},
                }
            ],
            "relabel_configs": [
                {
                    "source_labels": ["__meta_kubernetes_pod_label_app_kubernetes_io_part_of"],
                    "action": "keep",
                    "regex": "capacity-platform",
                },
                # Scrape via the Service DNS name: that name is in the server certificate, a pod IP is not.
                {
                    "source_labels": [
                        "__meta_kubernetes_pod_label_capacity_lab_service_name",
                        "__meta_kubernetes_namespace",
                    ],
                    "separator": ";",
                    "regex": "(.+);(.+)",
                    "target_label": "__address__",
                    "replacement": "$1.$2.svc:8443",
                },
                {"source_labels": ["__meta_kubernetes_pod_label_app_kubernetes_io_name"], "target_label": "service"},
                {"source_labels": ["__meta_kubernetes_namespace"], "target_label": "namespace"},
            ],
        },
        {"job_name": "prometheus", "scrape_interval": "30s", "static_configs": [{"targets": ["localhost:9090"]}]},
    ],
}


def observability(prometheus_image: str, ksm_image: str, grafana_image: str) -> list[dict]:
    ns = "capacity-observability"
    objs: list[dict] = [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "prometheus", "namespace": ns}},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "capacity-prometheus"},
            "rules": [
                {"apiGroups": [""], "resources": ["nodes", "pods"], "verbs": ["get", "list", "watch"]},
                {"apiGroups": [""], "resources": ["nodes/proxy", "nodes/metrics"], "verbs": ["get"]},
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": "capacity-prometheus"},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "capacity-prometheus"},
            "subjects": [{"kind": "ServiceAccount", "name": "prometheus", "namespace": ns}],
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "prometheus-config", "namespace": ns},
            "data": {"prometheus.yml": yaml.safe_dump(PROMETHEUS_CONFIG, sort_keys=False)},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": "prometheus", "namespace": ns, "labels": {"app.kubernetes.io/name": "prometheus"}},
            "spec": {
                "serviceName": "prometheus",
                "replicas": 1,
                "selector": {"matchLabels": {"app.kubernetes.io/name": "prometheus"}},
                # The config hash makes a changed scrape config roll the Pod (Prometheus only reads it at start).
                "template": {
                    "metadata": {
                        "labels": {"app.kubernetes.io/name": "prometheus"},
                        "annotations": {
                            "capacity.lab/config-sha256": hashlib.sha256(
                                yaml.safe_dump(PROMETHEUS_CONFIG, sort_keys=False).encode()
                            ).hexdigest()
                        },
                    },
                    "spec": {
                        "serviceAccountName": "prometheus",
                        "priorityClassName": "capacity-platform-standard",
                        "securityContext": {**NONROOT, "runAsUser": 65534, "runAsGroup": 65534, "fsGroup": 65534},
                        **PLATFORM_PLACEMENT,
                        "containers": [
                            {
                                "name": "prometheus",
                                "image": prometheus_image,
                                # S12-ADR-003 provisional: 7-day retention, bounded by size within the 5Gi PVC.
                                "args": [
                                    "--config.file=/etc/prometheus/prometheus.yml",
                                    "--storage.tsdb.path=/prometheus",
                                    "--storage.tsdb.retention.time=7d",
                                    "--storage.tsdb.retention.size=4GB",
                                ],
                                "ports": [{"containerPort": 9090, "name": "p9090"}],
                                "resources": {
                                    "requests": {"cpu": "250m", "memory": "512Mi"},
                                    "limits": {"cpu": "1", "memory": "1Gi"},
                                },
                                "securityContext": LOCKED,
                                "readinessProbe": {"httpGet": {"path": "/-/ready", "port": 9090}, "periodSeconds": 10},
                                "volumeMounts": [
                                    {
                                        "name": "config",
                                        "mountPath": "/etc/prometheus/prometheus.yml",
                                        "subPath": "prometheus.yml",
                                    },
                                    {"name": "ca", "mountPath": "/etc/prometheus/ca"},
                                    {"name": "data", "mountPath": "/prometheus"},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "config", "configMap": {"name": "prometheus-config"}},
                            {"name": "ca", "configMap": {"name": "capacity-trust"}},
                        ],
                    },
                },
                "volumeClaimTemplates": [
                    {
                        "metadata": {"name": "data"},
                        "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "5Gi"}}},
                    }
                ],
            },
        },
        _svc("prometheus", ns, [9090]),
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "kube-state-metrics", "namespace": ns}},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "capacity-ksm"},
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": [
                        "nodes",
                        "pods",
                        "namespaces",
                        "resourcequotas",
                        "limitranges",
                        "services",
                        "persistentvolumeclaims",
                    ],
                    "verbs": ["list", "watch"],
                },
                {
                    "apiGroups": ["apps"],
                    "resources": ["deployments", "replicasets", "statefulsets", "daemonsets"],
                    "verbs": ["list", "watch"],
                },
                {"apiGroups": ["batch"], "resources": ["jobs", "cronjobs"], "verbs": ["list", "watch"]},
                {"apiGroups": ["autoscaling"], "resources": ["horizontalpodautoscalers"], "verbs": ["list", "watch"]},
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": "capacity-ksm"},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "capacity-ksm"},
            "subjects": [{"kind": "ServiceAccount", "name": "kube-state-metrics", "namespace": ns}],
        },
        _deploy(
            "kube-state-metrics",
            ns,
            ksm_image,
            sa="kube-state-metrics",
            ports=[8080],
            uid=65534,
            args=[
                "--resources=nodes,pods,namespaces,resourcequotas,deployments,replicasets,jobs,cronjobs,"
                "horizontalpodautoscalers,statefulsets,daemonsets"
            ],
            probe={"httpGet": {"path": "/healthz", "port": 8080}},
            cpu="50m",
            mem="64Mi",
            mem_lim="192Mi",
        ),
        _svc("kube-state-metrics", ns, [8080]),
        _cert("tls-grafana", ns, ["grafana.capacity-observability.svc", "localhost"], ["127.0.0.1"]),
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "grafana-provisioning", "namespace": ns},
            "data": {
                "datasources.yaml": yaml.safe_dump(
                    {
                        "apiVersion": 1,
                        "datasources": [
                            {
                                "name": "Prometheus",
                                "uid": "prometheus",  # referenced by the provisioned dashboards
                                "type": "prometheus",
                                "access": "proxy",
                                "isDefault": True,
                                "editable": False,
                                "url": "http://prometheus.capacity-observability.svc:9090",
                            }
                        ],
                    }
                )
            },
        },
        # Dashboards are versioned in configs/dashboards and provisioned read-only.
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "grafana-dashboard-provider", "namespace": ns},
            "data": {
                "dashboards.yaml": yaml.safe_dump(
                    {
                        "apiVersion": 1,
                        "providers": [
                            {
                                "name": "capacity-lab",
                                "type": "file",
                                "disableDeletion": True,
                                "allowUiUpdates": False,
                                "options": {"path": "/var/lib/grafana-dashboards"},
                            }
                        ],
                    }
                )
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "grafana-dashboards", "namespace": ns},
            "data": {f.name: f.read_text(encoding="utf-8") for f in sorted(DASHBOARDS.glob("*.json"))},
        },
        _deploy(
            "grafana",
            ns,
            grafana_image,
            ports=[3000],
            uid=472,
            # Measured: Grafana 13 start-up (storage migrations) hit 222 MiB and 88 % CPU throttling
            # at 256 MiB / 500m and never became ready on the lab host.
            cpu="100m",
            mem="192Mi",
            cpu_lim="1000m",
            mem_lim="512Mi",
            env=[
                {"name": "GF_SERVER_PROTOCOL", "value": "https"},
                {"name": "GF_SERVER_CERT_FILE", "value": "/tls/tls.crt"},
                {"name": "GF_SERVER_CERT_KEY", "value": "/tls/tls.key"},
                {"name": "GF_AUTH_ANONYMOUS_ENABLED", "value": "false"},
                {"name": "GF_USERS_ALLOW_SIGN_UP", "value": "false"},
                {"name": "GF_SECURITY_ADMIN_PASSWORD__FILE", "value": "/secrets/admin-password"},
                {"name": "GF_PATHS_PROVISIONING", "value": "/etc/grafana/provisioning"},
                {"name": "GF_PATHS_DATA", "value": "/var/lib/grafana"},
            ],
            mounts=[
                {"name": "tls", "mountPath": "/tls", "readOnly": True},
                {"name": "secrets", "mountPath": "/secrets", "readOnly": True},
                {"name": "prov", "mountPath": "/etc/grafana/provisioning/datasources", "readOnly": True},
                {"name": "dash-provider", "mountPath": "/etc/grafana/provisioning/dashboards", "readOnly": True},
                {"name": "dashboards", "mountPath": "/var/lib/grafana-dashboards", "readOnly": True},
                {"name": "data", "mountPath": "/var/lib/grafana"},
                {"name": "tmp", "mountPath": "/tmp"},
            ],
            volumes=[
                {"name": "tls", "secret": {"secretName": "tls-grafana", "defaultMode": 0o440}},
                {"name": "secrets", "secret": {"secretName": "grafana-admin", "defaultMode": 0o440}},
                {"name": "prov", "configMap": {"name": "grafana-provisioning"}},
                {"name": "dash-provider", "configMap": {"name": "grafana-dashboard-provider"}},
                {"name": "dashboards", "configMap": {"name": "grafana-dashboards"}},
                {"name": "data", "emptyDir": {}},
                {"name": "tmp", "emptyDir": {}},
            ],
            probe={"httpGet": {"path": "/api/health", "port": 3000, "scheme": "HTTPS"}},
        ),
        _svc("grafana", ns, [3000], node_ports=[30445]),
    ]
    return objs


def keycloak(image: str) -> list[dict]:
    ns = "capacity-identity"
    return [
        _cert(
            "tls-keycloak",
            ns,
            ["keycloak.capacity-identity.svc", "keycloak.capacity-identity.svc.cluster.local", "localhost"],
            ["127.0.0.1"],
        ),
        _deploy(
            "keycloak",
            ns,
            image,
            uid=1000,
            ports=[8443, 9000],
            cpu="250m",
            mem="640Mi",
            cpu_lim="1500m",
            mem_lim="1Gi",
            ro=False,  # Keycloak's build step writes into its install dir (exception registry)
            args=["start", "--import-realm"],
            env=[
                {"name": "KC_DB", "value": "postgres"},
                {
                    "name": "KC_DB_URL",
                    "value": "jdbc:postgresql://postgres.capacity-data.svc:5432/keycloak?ssl=true&sslmode=verify-full&sslrootcert=/ca/ca.crt",
                },
                {"name": "KC_DB_USERNAME", "value": "keycloak"},
                {"name": "KC_DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "keycloak-db", "key": "password"}}},
                {"name": "KC_HOSTNAME", "value": "https://localhost:8444"},
                {"name": "KC_HOSTNAME_BACKCHANNEL_DYNAMIC", "value": "true"},
                {"name": "KC_HTTP_ENABLED", "value": "false"},
                {"name": "KC_HTTPS_CERTIFICATE_FILE", "value": "/tls/tls.crt"},
                {"name": "KC_HTTPS_CERTIFICATE_KEY_FILE", "value": "/tls/tls.key"},
                {"name": "KC_HEALTH_ENABLED", "value": "true"},
                {"name": "KC_BOOTSTRAP_ADMIN_USERNAME", "value": "lab-bootstrap-admin"},
                {
                    "name": "KC_BOOTSTRAP_ADMIN_PASSWORD",
                    "valueFrom": {"secretKeyRef": {"name": "keycloak-admin", "key": "password"}},
                },
                {"name": "JAVA_OPTS_KC_HEAP", "value": "-XX:MaxRAMPercentage=65"},
            ],
            mounts=[
                {"name": "tls", "mountPath": "/tls", "readOnly": True},
                {"name": "ca", "mountPath": "/ca", "readOnly": True},
                {"name": "realm", "mountPath": "/opt/keycloak/data/import", "readOnly": True},
            ],
            volumes=[
                {"name": "tls", "secret": {"secretName": "tls-keycloak", "defaultMode": 0o440}},
                {"name": "ca", "configMap": {"name": "capacity-trust"}},
                {"name": "realm", "secret": {"secretName": "keycloak-realm", "defaultMode": 0o440}},
            ],
            probe={
                "httpGet": {"path": "/health/ready", "port": 9000, "scheme": "HTTPS"},
                "periodSeconds": 10,
                "failureThreshold": 30,
            },
        ),
        _svc("keycloak", ns, [8443], node_ports=[30444]),
    ]
