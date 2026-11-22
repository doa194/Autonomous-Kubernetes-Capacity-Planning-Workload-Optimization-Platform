# Configuration reference

Every configuration file, the values that matter in each, the environment variables of the services
and host tools, and the fixed constants worth knowing.

## 1. Versioned configuration files

| File | Read by | Delivered as | Contents |
|---|---|---|---|
| [`configs/policies/optimization-policy.yaml`](../configs/policies/optimization-policy.yaml) | capacity, governance (policy sync), executor | ConfigMap `capacity-policies` | allowed actions and namespaces, grids, ranking weights, per-class floors/margins/change budgets, SLO limits, coverage requirements, workload overrides |
| [`configs/policies/governance-policy.yaml`](../configs/policies/governance-policy.yaml) | governance | ConfigMap `capacity-policies` | per workload: may deterministic-only plans execute? |
| [`configs/policies/verification-policy.yaml`](../configs/policies/verification-policy.yaml) | executor | ConfigMap `capacity-policies` | rollout deadline, windows, load tolerance, per-workload guardrails, inverse attempts |
| [`configs/models/inference-profile.yaml`](../configs/models/inference-profile.yaml) | ai, `capacity-lab model` | ConfigMap `inference-profile` | gateway URL, pinned models and digests, limits, `qualified` flag |
| [`configs/scenarios/*.yaml`](../configs/scenarios) | `capacity-scenario` | read on the host | experiment definitions E01–E04 |
| [`configs/dashboards/capacity-overview.json`](../configs/dashboards/capacity-overview.json) | Grafana | ConfigMap (provisioned dashboard) | the "Capacity platform overview" dashboard |
| [`infra/versions.lock.json`](../infra/versions.lock.json) | `capacity-lab` | read on the host | pinned third-party versions, URLs and SHA-256 hashes |
| [`infra/kind/cluster.yaml`](../infra/kind/cluster.yaml), [`infra/kustomize/base/`](../infra/kustomize/base) | `capacity-lab` | applied to the cluster | cluster topology and baseline objects ([cluster-topology.md](cluster-topology.md)) |

**Revisions.** Policy files and the inference profile carry a `revision`. Bump it whenever a value
changes, so results computed under the old values stay interpretable — every forecast, analysis,
plan and outcome records the policy revision or digest it used. In the cluster the files are
delivered as ConfigMaps by the `platform` bootstrap stage; re-run it after a change:

```powershell
capacity-lab bootstrap --only platform
```

## 2. Optimization policy (summary)

Full table and meaning: [optimization-engine.md](optimization-engine.md#1-policy-s19).

| Key | Value |
|---|---|
| `policyId` / `revision` | `demo-resource-governance` / 1 |
| `allowedActions` | `cpu_request`, `memory_request`, `cpu_limit`, `memory_limit` |
| `allowedNamespaces` | `workloads-critical`, `workloads-standard`, `workloads-batch` |
| `maxCandidates` | 8 |
| `cpuGridMillicores` / `memoryGridBytes` | 5 / 1,048,576 |
| `weights` | efficiency 1.0, fit_benefit 0.5, risk 0.8, change_cost 0.2, pareto_tolerance 0.01 |
| `classes` | `Critical`, `Standard`, `Flexible`, `Opportunistic` (floors, margins, change budgets, SLO and coverage limits) |
| `workloadOverrides` | `w03-memory-profile.memory_margin: 0.35` |

Overrides may only **tighten** a value; loosening raises `PolicyConflict`.

## 3. Governance policy

```yaml
schemaVersion: governance-policy/v1alpha1
revision: 1
deterministicOnlyExecutionAllowed:
  w01-latency-api: false        # Critical: requires a completed advisory review
  w02-burst-api: true
  w03-memory-profile: true
  w04-cpu-worker: true
```

A workload missing from the list is treated as "not allowed" (the plan becomes `AdvisoryOnly`).

## 4. Verification policy

| Key | Value |
|---|---|
| `rolloutDeadlineSeconds` | 300 |
| `baselineSeconds` / `stabilizationSeconds` / `evaluationSeconds` | 900 / 120 / 900 |
| `loadComparabilityTolerance` | 0.2 |
| `maxInverseAttempts` | 1 |
| `workloads.<id>` | `p95_latency_ms_max`, `error_rate_max`, `max_oom_restarts`, `min_availability`, `max_queue_growth_per_min`, `min_coverage` |

Per-workload values are in [governance-and-execution.md](governance-and-execution.md#4-verification-and-recovery-s26).
A threshold left empty means that guardrail is not evaluated for the workload.

## 5. Inference profile

| Key | Value |
|---|---|
| `profileId` / `revision` | `local-cpu-gpu-compact` / 1 |
| `gatewayUrl` | `https://host.docker.internal:11500` |
| `generationModel` | `gemma4:e4b` (`digest: null` until qualified) |
| `embeddingModel` / `embeddingDimensions` | `nomic-embed-text` / 768 |
| `maxPromptChars` / `maxOutputTokens` / `numCtx` | 24000 / 900 / 8192 |
| `timeoutSeconds` / `seed` | 180 / 42 |
| `qualified` / `qualificationRef` | `false` / `null` — set only by a passing `capacity-lab model qualify` |

## 6. Scenario files

```yaml
apiVersion: capacity.lab/v1alpha1
kind: ExperimentScenario
metadata: {id: e04-steady-demand, revision: 1}
spec:
  targets: [{workloadId: w04-cpu-worker, profileRevision: 1, namespace: workloads-batch}]
  execution: {seed: 42044, executionClass: moderate, mode: live, maxDurationSeconds: 3600, maxParallelDrivers: 1}
  phases:
    - {id: warmup, durationSeconds: 30, measurement: excluded, actions: [...]}
    - {id: steady-a, durationSeconds: 1650, measurement: included, actions: [...]}
    - {id: steady-b, durationSeconds: 1650, measurement: included, actions: [...]}
    - {id: cooldown, durationSeconds: 30, measurement: excluded, actions: [{kind: idle}]}
  safety: {maxInflightRequests: 8, requestTimeoutSeconds: 5, hostPressurePolicyRef: local-host-safe-v1}
```

Limits enforced by validation: one phase ≤ 1800 s, a scenario ≤ 3600 s, phase order *warmup
(excluded) → measured (included)+ → cooldown (excluded)*, known targets/operations/profiles only,
rates under hard caps, no arbitrary commands. See
[workloads-and-experiments.md](workloads-and-experiments.md).

## 7. Platform service environment

Set by `lab/manifests.py`; listed here so a service can be run or debugged elsewhere.

| Variable | Services | Meaning |
|---|---|---|
| `CAPACITY_ROLE` | all | which of the six roles to run |
| `PORT` | all | HTTPS port (8443) |
| `SERVE_TLS_CERT`, `SERVE_TLS_KEY` | all | server certificate files |
| `SERVE_MTLS_PORT`, `SERVE_TLS_CLIENT_CA` | governance | second listener (8444) that requires client certificates from this CA |
| `CAPACITY_DB_DSN_FILE` | all | file containing the service's PostgreSQL DSN (from Vault via External Secrets) |
| `OIDC_ISSUER`, `OIDC_AUDIENCE`, `OIDC_JWKS_URL`, `OIDC_ALLOWED_CLIENTS`, `OIDC_CA_BUNDLE` | all | user-token validation (lab: `OIDC_ALLOWED_CLIENTS=capacityctl,capacity-e2e`) |
| `OIDC_JWKS_FILE` | all | optional local JWKS file instead of fetching `OIDC_JWKS_URL` |
| `SA_ISSUER`, `SA_JWKS_FILE` | all | service-token validation (cluster issuer, `sa-jwks` ConfigMap) |
| `INTERNAL_CA_BUNDLE` | all | trust for internal HTTPS calls |
| `PROMETHEUS_URL` | capacity, governance, executor | `http://prometheus.capacity-observability.svc:9090` |
| `POLICY_FILE` | capacity, governance, executor | optimization policy path |
| `GOVERNANCE_POLICY_FILE` | governance | governance policy path |
| `VERIFICATION_POLICY_FILE` | executor | verification policy path |
| `GOVERNANCE_URL`, `AI_URL` | api | `https://capacity-governance.capacity-core.svc:8444`, `https://capacity-ai.capacity-ai.svc:8443` |
| `CLIENT_TLS_CERT`, `CLIENT_TLS_KEY` | api | client certificate for the governance mTLS listener |
| `CAPACITY_URL` | ai | `https://capacity-capacity.capacity-core.svc:8443` (tool endpoint) |
| `INFERENCE_PROFILE_FILE`, `GATEWAY_KEY_FILE` | ai | model profile and gateway key paths |
| `GATEWAY_CA_BUNDLE` | ai | trust for the host gateway certificate (optional) |
| `LOG_LEVEL` | all | log level (default `INFO`) |

## 8. Host tool environment

| Variable | Tool | Default |
|---|---|---|
| `CAPACITY_LAB_HOME` | scenario runner, watchdog | `~/.capacity-lab` |
| `DRIVER_IMAGE` | `capacity-scenario run` / `recover` | required for `run`: the platform image digest from `~/.capacity-lab/release/images.json` |
| `CAPACITY_API_URL` | `capacityctl` | `https://localhost:8443` |
| `CAPACITY_OIDC_ISSUER` | `capacityctl` | `https://localhost:8444/realms/capacity` |
| `CAPACITY_CA_BUNDLE` | `capacityctl` | `~/.capacity-lab/pki/root-ca.crt` |
| `CAPACITYCTL_CALLBACK_PORT` | `capacityctl` | `8765` |
| `OLLAMA_UPSTREAM` | model gateway | `http://127.0.0.1:11434` |
| `GATEWAY_BIND`, `GATEWAY_PORT` | model gateway | `127.0.0.1`, `11500` |

## 9. Constants worth knowing

| Constant | Value | Where |
|---|---|---|
| kind context | `kind-capacity-lab` | `lab/tools.py` |
| local registry | `localhost:5001` | `lab/cluster.py` |
| Keycloak issuer | `https://localhost:8444/realms/capacity` | `lab/manifests.py` |
| service-token audience / lifetime | `capacity-internal` / 600 s | `lab/manifests.py` |
| user access-token lifetime | 600 s | `lab/identity.py` |
| plan / approval / intent lifetime | 60 / 15 / 15 minutes | `governance/plans.py` |
| evidence lookback | 6 hours, aligned to 5 minutes | `capacity/evidence.py` |
| job max attempts / backoff cap | 3 / 300 s | `shared/jobs.py` |
| job leases | capacity 5 min, ai 10 min, executor 2 min | `capacity/service.py`, `ai/service.py`, `executor/service.py` |
| host-safety freshness for the executor | 2 minutes | `executor/service.py` |
| watchdog thresholds | warn < 2.0 GiB, abort < 1.5 GiB free; interval 10 s | `watchdog/main.py` |
| AI broker budget | 12 calls, 4 per tool, 60,000 result bytes | `ai/broker/broker.py` |
| run-quality limits | success ≥ 95 %, dropped arrivals ≤ 5 % | `scenarios/runner.py` |
| driver start timeout | 300 s | `scenarios/runner.py` |
