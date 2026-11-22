# Setup guide

This guide builds the complete lab on one Windows machine — the kind cluster, all supporting
services, the six platform services and the demo workloads — then verifies it, shows how to pause
and resume it, and how to remove it. Every bootstrap stage is idempotent and recorded, so an
interrupted bootstrap can be resumed.

Expect the **first** bootstrap to take a long time: it downloads the kind node image, Calico,
cert-manager, Helm charts and container images, and builds the platform and workload images.

## 1. Prerequisites

| Requirement | Why | Checked by `preflight` |
|---|---|---|
| Windows 11 with WSL2 | Docker Desktop backend | — |
| Docker Desktop with **≥ 4 CPUs** and **≥ 10 GiB** memory for its VM | 4 kind nodes + platform | `docker_cpus`, `docker_memory` |
| **≥ 30 GiB** free disk where Docker stores data | images, volumes, registry | `disk_free` |
| **≥ 2 GiB** free host memory at start | the watchdog's pressure threshold | `host_available_memory` |
| `kubectl`, `helm` and `docker` on `PATH` | cluster and chart operations | `tool_kubectl`, `tool_helm`, `tool_docker` |
| [uv](https://docs.astral.sh/uv/) | Python 3.13 environment and locked dependencies | — |
| .NET 10 SDK (optional) | only to run the .NET tests on the host — the images build inside Docker | — |
| Ollama with `gemma4:e4b` and `nomic-embed-text` (optional) | only for the AI advisory layer | — |
| Internet access during bootstrap | pinned binary, manifests, charts and images | — |

**Strongly recommended:** keep Docker Desktop's disk image on an SSD. On a hard disk, etcd becomes
slow under load and the API server can fail its health checks (see
[troubleshooting.md](troubleshooting.md#1-host-and-cluster)).

The pinned kind binary (v0.33.0) is downloaded into `.tools/bin` by preflight and refused if its
SHA-256 differs from [`infra/versions.lock.json`](../infra/versions.lock.json). The Calico and
cert-manager manifests are verified the same way.

## 2. Install the Python environment

From the repository root:

```powershell
uv sync --all-extras
```

This creates `.venv` with the project, the AI extra (Microsoft Agent Framework) and the command-line
tools `capacity-lab`, `capacityctl`, `capacity-scenario`, `capacity-watchdog` and
`capacity-service`. Activate it, or prefix commands with `.venv\Scripts\`:

```powershell
.venv\Scripts\Activate.ps1
```

## 3. Check the host

```powershell
capacity-lab preflight
```

Every check must report `"ok": true`. Example from the reference host:

```json
{"checks": {"docker_memory": {"ok": true, "detail": "13.7 GiB available to Docker/WSL2"},
            "docker_cpus": {"ok": true, "detail": "12 CPUs"},
            "disk_free": {"ok": true, "detail": "666 GiB free"},
            "kind_pinned": {"ok": true, "detail": "v0.33.0"},
            "tool_kubectl": {"ok": true, "detail": "found"},
            "tool_helm": {"ok": true, "detail": "found"},
            "tool_docker": {"ok": true, "detail": "found"},
            "host_available_memory": {"ok": true, "detail": "5.9 GiB"}},
 "ok": true}
```

## 4. Bootstrap

```powershell
capacity-lab bootstrap
```

The 17 stages run in dependency order. Completed stages are recorded in
`~/.capacity-lab/bootstrap-journal.json` and skipped on the next run.

| # | Stage | What it does |
|---|---|---|
| 1 | `preflight` | host checks; pinned kind download |
| 2 | `registry` | local image registry `localhost:5001` (`registry:3`) |
| 3 | `cluster` | 4-node kind cluster `capacity-lab`; ports 8443/8444/8445 bound to 127.0.0.1 |
| 4 | `calico` | Calico CNI with enforced NetworkPolicy |
| 5 | `base` | namespaces, PriorityClasses, quotas, LimitRanges, RBAC, NetworkPolicies, admission policies |
| 6 | `network-dynamic` | discovers API-server and host-gateway addresses; renders the dynamic policies |
| 7 | `verify-network` | **fails the bootstrap** unless default-deny is really enforced |
| 8 | `pki` | cert-manager, lab root CA (host) and the name-constrained intermediate issuer |
| 9 | `secrets` | Vault (init, unseal, policies) and External Secrets Operator; generated credentials |
| 10 | `database` | PostgreSQL + pgvector, migrations, service role passwords |
| 11 | `identity` | Keycloak with the `capacity` realm and the lab users |
| 12 | `observability` | Prometheus, kube-state-metrics, Grafana (dashboard provisioned) |
| 13 | `images` | builds and pushes the platform and workload images; records their digests |
| 14 | `platform` | the six platform Deployments, policy and profile ConfigMaps |
| 15 | `workloads` | W01–W06 |
| 16 | `grants` | role grants for the lab users |
| 17 | `host-identities` | narrow kubeconfig contexts for the scenario runner and the watchdog |

Useful variants:

```powershell
capacity-lab status
```

```powershell
capacity-lab bootstrap --only images
```

```powershell
capacity-lab bootstrap --from-stage platform
```

`--only` runs exactly one stage; `--from-stage` resumes from a stage and runs the rest;
`--force` re-runs stages that are already recorded.

## 5. Verify

```powershell
capacity-lab verify all
```

This runs seven live checks and writes evidence to `~/.capacity-lab/evidence/verify-all-<time>.json`;
any failure exits non-zero:

| Check | Proves |
|---|---|
| `network_default_deny_enforced` | approved flows connect, denied flows time out, DNS works |
| `rbac_least_privilege` | 12 allow/deny cases for the platform ServiceAccounts |
| `admission_negative_cases` | privileged Pods, missing limits, tag-only images, token mounts, writable roots and rogue Jobs are denied; a compliant Pod is allowed |
| `executor_field_guard` | the executor may patch resources but not replicas, image or ServiceAccount |
| `observability_targets` | every scrape target is up, including six platform targets |
| `placement_pools` | no Pod is on the wrong node pool |
| `topology_and_six_deployments` | 1 control plane, 1 platform node, 2 experiment nodes; exactly six platform Deployments |

Single areas can be checked with `capacity-lab verify networking|security|observability|scheduling`.

Then check the API as a person:

```powershell
capacityctl auth login
```

```powershell
capacityctl capabilities
```

```powershell
capacityctl workloads
```

The login opens the browser. Lab passwords are generated per installation; print one when you need
it:

```powershell
capacity-lab credentials planner1
```

## 6. Start the host safety watchdog

Experiments are refused, and the executor refuses changes, unless the watchdog is publishing a fresh
host-safety signal. Leave it running in its own terminal:

```powershell
capacity-watchdog run
```

## 7. Optional: enable the AI advisory layer

1. Install Ollama and make sure the pinned models are present (`gemma4:e4b`, `nomic-embed-text`).
   Nothing is downloaded automatically.
2. Start the protected gateway in its own terminal:

   ```powershell
   capacity-lab model gateway
   ```

3. Qualify the models on this host:

   ```powershell
   capacity-lab model qualify
   ```

   Only a passing qualification marks the inference profile `qualified: true` and publishes it to the
   cluster. Until then advisories end `unavailable` (`model_unqualified`). See
   [ai-advisory.md](ai-advisory.md).

## 8. Ports on the host

| URL | Service |
|---|---|
| `https://localhost:8443` | platform API (`/api/v1`, OpenAPI at `/openapi.json`) |
| `https://localhost:8444` | Keycloak (OIDC issuer `https://localhost:8444/realms/capacity`) |
| `https://localhost:8445` | Grafana |
| `https://127.0.0.1:11500` | model gateway (only while running) |
| `http://localhost:5001` | local image registry |

All certificates chain to the lab root CA at `~/.capacity-lab/pki/root-ca.crt`. Browsers warn until
that certificate is trusted; the CLI tools trust it automatically. Prometheus is not published; use
`kubectl port-forward` (see [observability.md](observability.md)).

## 9. Host state

Everything the lab keeps outside the cluster is under `~/.capacity-lab`:

| Path | Contents |
|---|---|
| `bootstrap-journal.json` | completed bootstrap stages |
| `pki/` | root CA certificate and DPAPI-protected key |
| `vault/` | DPAPI-protected unseal key and root token |
| `identity/` | DPAPI-protected lab user passwords |
| `keys/` | experiment capability signing key |
| `release/` | `images.json` (deployed digests), `network.json`, cluster facts |
| `evidence/` | verification, model qualification, test results, experiment bundles, E2E records |
| `backups/` | encrypted backups and restore verifications; backup key |
| `audit/` | checkpoint key, public key and signed checkpoints |
| `release-qualification/` | release qualification bundles |
| `scenario-journal.sqlite` | experiment runs and watchdog heartbeat |
| `kubeconfig` | runner and watchdog contexts |

Back this folder up yourself if the lab data matters; it is not inside the cluster.

## 10. Stop, resume and clean up

**Pause:** stop Docker Desktop. After it starts again, bring the lab back:

```powershell
capacity-lab resume
```

`resume` waits for the nodes, re-discovers the node and gateway addresses, re-renders the dynamic
NetworkPolicies if they changed, restarts the Pods that depend on them and any Pod stuck in
CrashLoopBackOff, and unseals Vault. `capacity-lab unseal` alone only unseals Vault. Telemetry for
the downtime is missing, so forecasts refuse until about six hours of continuous history exist again.

**Tear down:** deletion refuses to run until a backup has passed its restore check:

```powershell
capacity-lab backup create
```

```powershell
capacity-lab backup verify
```

```powershell
capacity-lab destroy --confirm capacity-lab --no-dry-run
```

Without `--confirm capacity-lab --no-dry-run`, `destroy` only reports what would be deleted. Host
state in `~/.capacity-lab` is kept; delete it yourself only when you no longer need the backups,
keys and evidence.

Problems during setup are covered in [troubleshooting.md](troubleshooting.md). Every command is
listed in [cli-reference.md](cli-reference.md).
