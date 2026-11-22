# Observability (S12, S28)

Observability has two jobs here. It is the **input** to capacity decisions — the telemetry that
forecasts, gates and verification read (see [telemetry.md](telemetry.md)) — and it is how an operator
sees whether the platform itself is healthy. This page covers the second job and the shared
infrastructure.

## 1. Components (namespace `capacity-observability`)

| Component | Version | Role | Access |
|---|---|---|---|
| Prometheus | v3.16.0 | scrapes and stores metrics (StatefulSet `prometheus-0` with a persistent volume) | in-cluster only; use port-forward |
| kube-state-metrics | v2.20.0 | Kubernetes object state: replicas, restarts, requests/limits, Pod phases, HPAs | in-cluster |
| Grafana | 13.3.1 | dashboards; Prometheus provisioned as the data source (uid `prometheus`) | `https://localhost:8445` |

Grafana's admin password is generated into Vault (`capacity/grafana/admin`) and delivered as a Secret.
Read it only when needed:

```powershell
kubectl --context kind-capacity-lab -n capacity-observability get secret grafana-admin -o jsonpath="{.data.admin-password}"
```

(the value is Base64; decode it locally). Grafana runs with 100m / 192Mi requests and 1 CPU / 512Mi
limits — smaller sizes starved its start-up (see [troubleshooting.md](troubleshooting.md)).

## 2. Scrape jobs

| Job | Targets (live) | Notes |
|---|---|---|
| `kubernetes-cadvisor` | 4 (every node, via the API-server proxy) | verified cluster CA; only CPU, memory and CFS throttling series kept (cardinality budget) |
| `kube-state-metrics` | 1 | object state |
| `demo-workloads` | 4 (W01–W04 Pods labelled `capacity.lab/demo-service=true`) | application metrics on port 8080 |
| `capacity-platform` | 6 (the six services' `/metrics` over HTTPS) | addressed by Service DNS name (`<service>.<namespace>.svc:8443`) so the TLS certificate verifies; no insecure skip |
| `prometheus` | 1 | itself |

The Prometheus configuration carries a config-hash annotation, so a changed scrape configuration
rolls the Pod. The target counts above are from the live verification of 2026-11-21 05:10 (all up).

## 3. Dashboard

Grafana provisions one read-only dashboard from
[`configs/dashboards/capacity-overview.json`](../configs/dashboards/capacity-overview.json),
**Capacity platform overview**:

| Panel | Shows | Why |
|---|---|---|
| CPU usage vs request | workload CPU (millicores) against its configured request | the gap the optimizer works on |
| Memory working set vs request | working set against the memory request | memory head-room and the downsizing case |
| CPU throttling | throttled CFS periods / periods per Pod | a CPU request or limit that is too low |
| Demo p95 latency | W01/W02 request latency | the SLO guardrail |
| Platform requests | request rate and p95 latency per platform service | health of the platform itself |

To change it, edit the JSON and run `capacity-lab bootstrap --only observability`.

## 4. Platform service telemetry

Every service exposes:

* `/health/live` — the process is up (liveness probe);
* `/health/ready` — its dependencies are reachable (readiness probe). For worker services readiness
  reflects the worker loop's last database round-trip and recovers on the next success, including on
  an idle queue;
* `/metrics` — `capacity_http_requests_total` and `capacity_http_request_duration_seconds`, labelled
  only by service, route template and status class (low cardinality).

## 5. Logs

Logs are JSON lines on stdout with `ts`, `level`, `logger`, `msg` and `correlationId`; errors add the
exception text. A redaction filter removes bearer tokens, passwords and keys before anything is
written. Notable log lines:

| Logger / message | Meaning |
|---|---|
| `api` `unhandled error` | an unexpected exception; the client got `INTERNAL_ERROR` with the same correlation ID |
| `executor` `verification query <id> failed: <code>` | a guardrail query failed; that guardrail is `unknown` |
| `executor` `intent worker error` | the intent loop hit an error; readiness reflects it until the next success |
| `governance` `could not audit refused <action>` | a denial could not be written to the audit chain (the refusal still stands) |
| `uvicorn.access` | one line per HTTP request with method, path and status |

Read and filter them with `kubectl`:

```powershell
kubectl --context kind-capacity-lab -n capacity-core logs deploy/capacity-capacity --since=15m
```

```powershell
kubectl --context kind-capacity-lab -n capacity-api logs deploy/capacity-api | Select-String "<correlation-id>"
```

Because every job, outbox event and audit event carries the same correlation ID, one user action can
be followed across the API, the worker, governance and the executor.

## 6. Domain state without metrics

Capacity decisions and their outcomes are recorded in PostgreSQL and the audit chain rather than as
metrics: job states (`workflow.jobs`), forecasts and analyses (`capacity.*`), plan transitions,
operation transitions and outcomes (`execution.*`), and every allow/deny/fact in `audit.events`. See
[data-model.md](data-model.md#10-inspecting-the-data) for queries.

## 7. Checking it works

```powershell
capacity-lab verify observability
```

This port-forwards to Prometheus and requires every active scrape target to be `up`, including at
least six platform targets. It is also part of `capacity-lab verify all` and of the operational test
suite (`test_live_verification_check_passes`).

To look at Prometheus directly:

```powershell
kubectl --context kind-capacity-lab -n capacity-observability port-forward pod/prometheus-0 9090:9090
```

Then open `http://127.0.0.1:9090/targets`.

## 8. Current limits

* Platform metrics are HTTP-level only; there are no domain metrics for queue depth, forecast refusal
  rates or executor outcomes (those are in PostgreSQL and the audit log).
* No alert rules are provisioned; alerts would need Alertmanager and a notification route.
* Retention is Prometheus's local default on one volume; there is no long-term storage, and telemetry
  for any period the host was down is simply missing.
* No log aggregation or tracing; logs live in the Pods until they restart.

See [limitations.md](limitations.md) and [production-considerations.md](production-considerations.md).
