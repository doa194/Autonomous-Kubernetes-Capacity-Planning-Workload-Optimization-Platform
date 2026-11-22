# REST API reference (S27)

Base URL: `https://localhost:8443/api/v1`. Every endpoint needs `Authorization: Bearer <access
token>` from the `capacity` realm (see [identity-and-access.md](identity-and-access.md)). Requests
and responses are JSON. The OpenAPI document is served at `https://localhost:8443/openapi.json`.
`capacityctl` wraps every endpoint ([cli-reference.md](cli-reference.md)).

## 1. Conventions

* **Authorization.** Every action needs a role from the action matrix **and** a grant covering the
  target namespace; approvals and execution requests also need `Governed` mode. Plan, approval,
  execution and mode calls are relayed by the API to governance over mutual TLS, and governance
  authorizes them again itself.
* **Asynchronous work.** Analyses and advisories return `202 Accepted` with an `operationId`; poll
  `GET /operations/{operationId}` until `state` is `completed`, `failed` or `timed_out`.
* **Idempotency.** `POST /analyses` and `POST /plans/{id}/execution-requests` take an
  `Idempotency-Key` header. Repeating a request with the same key returns the same operation or
  intent; reusing a key with a different body returns `409`.
* **Strict bodies.** Unknown request fields are rejected (`422`), so a client cannot smuggle extra
  parameters.
* **Correlation.** Send `X-Correlation-ID` to trace a request across services; otherwise one is
  generated. It appears in every error body and in every log line.
* **Opaque denials.** A target outside your scope and a target that does not exist give the same
  response, so the API never reveals what exists.

## 2. Errors

All errors use one envelope:

```json
{"schemaVersion": "platform-error/v1alpha1",
 "error": {"code": "AUTHENTICATION_REQUIRED", "category": "authentication",
           "message": "access token required", "retryable": false,
           "correlationId": "118addee-9217-4798-a334-318834839760"}}
```

| Category | HTTP | Typical codes |
|---|---|---|
| authentication | 401 | `AUTHENTICATION_REQUIRED`, invalid/expired token |
| authorization | 403 | `AUTHORIZATION_DENIED` (same message whether or not the target exists), `separation_of_duties` |
| not_found | 404 | unknown route |
| validation | 422 | malformed body (with `fields`), `IDEMPOTENCY_KEY_REQUIRED` |
| precondition | 409 | `plan_digest_mismatch`, `plan_expired`, `plan_not_awaiting_approval`, `no_approval`, stale mode revision |
| conflict | 409 | idempotency key reused with a different payload |
| rate_limited | 429 | |
| unavailable | 503 | `governance_unavailable`, `ai_unavailable` (dependency down or its TLS hop failed) |
| internal | 500 | `INTERNAL_ERROR` — detail only in the server log |

## 3. Endpoints

| Method and path | Action (roles) | Purpose |
|---|---|---|
| `GET /capabilities` | any role | operating mode and revision, autonomy level, capability status, inventory freshness |
| `GET /me/scopes` | any role | the caller's subject, token roles and grants |
| `GET /workloads` | `inventory.read` | demo workloads with **configured** requests/limits (not usage) |
| `GET /nodes` | `inventory.read` | nodes with allocatable and requested resources |
| `POST /analyses` | `analysis.request` (Analyst, Planner) | start an analysis |
| `GET /operations/{id}` | `evidence.read` | job state and result |
| `GET /analyses/{digest}` | `evidence.read` | full analysis: baseline gates, refusals, evaluations, context |
| `GET /candidates/{evaluationDigest}` | `evidence.read` | one evaluated candidate with its gates and placement |
| `GET /forecasts/{forecastId}` | `evidence.read` | forecast with eligibility, model, intervals or refusal reasons |
| `POST /advisories` | `advisory.request` (Analyst, Planner; mode Advisory/Governed) | start an AI advisory |
| `GET /advisories/{runId}` | `evidence.read` | the validated advisory artifact |
| `POST /plans` | `plan.create` (Planner) | create a plan from an evaluation digest |
| `GET /plans/{planId}?revision=N` | `plan.read` | plan, state, approval and revocation |
| `POST /plans/{planId}/approvals` | `plan.approve` / `plan.reject` (Approver, Governed) | approve or reject an exact revision and digest |
| `POST /plans/{planId}/revocations` | `approval.revoke` (Approver) | withdraw an approval |
| `POST /plans/{planId}/execution-requests` | `execution.request` (Executor, Governed) | create an execution intent |
| `GET /executions/{intentId}` | `execution.read` | operation state, transitions, verification outcome |
| `PUT /mode` | `mode.set` (Administrator) | change the operating mode |
| `POST /knowledge/documents` | `knowledge.ingest` (Administrator) | add an operational knowledge document (Restricted content is refused) |

There are deliberately **no** generic relay or pass-through endpoints.

## 4. Request and response examples

Examples are taken from the reference lab (identifiers shortened where marked).

### `GET /capabilities`

```json
{"mode": "Observe", "modeRevision": 3, "autonomy": "L0-L2 (no L3)",
 "capabilities": {"inventory": "available",
                  "deterministic_forecast_1h": "available",
                  "conditional_forecast_24h": "evidence_qualified_only",
                  "deterministic_optimization": "available",
                  "governed_direct_execution": "disabled_by_mode",
                  "ai_advisory": "depends_on_qualified_model_gateway",
                  "gitops_flux_adapter": "unqualified",
                  "supply_chain_admission_s10": "not_installed",
                  "autoscaling_advisory_s23": "not_installed"},
 "inventory": {"capturedAt": "2026-11-21T05:30:58.151883Z", "coverage": "complete", "freshness": "fresh"}}
```

### `GET /me/scopes` (as viewer1)

```json
{"subject": "5ee5348c-…", "tokenRoles": ["Viewer"],
 "grants": [{"role": "Viewer", "namespaces": ["*cluster*", "workloads-batch", "workloads-critical", "workloads-standard"]}]}
```

### `GET /workloads` (first entry)

```json
{"data": [{"workloadId": "w01-latency-api", "namespace": "workloads-critical", "kind": "Deployment",
           "criticality": "Critical", "uid": "47601618-…",
           "requestsAndLimits": {"requests": {"cpu": "200m", "memory": "256Mi"},
                                 "limits": {"cpu": "500m", "memory": "384Mi"}},
           "basis": "kubernetes_configured_requests_not_usage"}]}
```

### `POST /analyses`

```http
POST /api/v1/analyses
Idempotency-Key: 6f3c…
Content-Type: application/json

{"workloadId": "w04-cpu-worker", "horizon": "1h"}
```

`202 Accepted` → `{"operationId": "…"}`. `horizon` is `1h` or `24h`; `intendedUse` may be
`governed_execution` (default) or `advisory`. When the operation completes, its `result` contains
`analysisDigest`, `qualified` (evaluation digests) and `forecastIds`.

### `POST /plans`

```json
{"evaluationDigest": "sha256:…"}
```

`201 Created` → `{"plan": {"planId": "plan-1698278fbb3b", "revision": 1, "planDigest": "sha256:…",
"approvedFieldChanges": [{"field": "memory_request", "before": 201326592, "after": 100663296}], …},
"state": "AwaitingApproval"}`. An optional `planId` and `advisoryArtifactDigest` may be supplied.

### `POST /plans/{planId}/approvals`

```json
{"revision": 1, "planDigest": "sha256:…", "decision": "approve"}
```

`201 Created` with the approval record. The author gets `403`; a wrong digest gets `409`
(`plan_digest_mismatch`); outside `Governed` mode the request is denied (`403`; the audit record
keeps the internal reason `mode_not_governed`).

### `POST /plans/{planId}/execution-requests`

```http
Idempotency-Key: 9a1e…

{"revision": 1}
```

`202 Accepted` → `{"intentId": "intent-fb7bbb62d95d"}`.

### `GET /executions/{intentId}`

```json
{"intentId": "intent-fb7bbb62d95d", "state": "recovered",
 "operation": {"planId": "plan-1698278fbb3b", "adapter": "direct_kubernetes", "attempt": 1,
               "patch": [{"op": "test", "path": "/metadata/uid", "value": "0e249f7a-…"},
                         {"op": "test", "path": "/metadata/resourceVersion", "value": "40353"},
                         {"op": "test", "path": "/spec/template/spec/containers/0/name", "value": "app"},
                         {"op": "test", "path": "/spec/template/spec/containers/0/resources/requests/memory", "value": "192Mi"},
                         {"op": "replace", "path": "/spec/template/spec/containers/0/resources/requests/memory", "value": "96Mi"}],
               "history": [{"from_state": "requested", "to_state": "awaiting_revalidation", "reason": "claimed", "at": "2026-11-21T03:22:06Z"}, "…"]}}
```

### `PUT /mode`

```json
{"mode": "Governed", "expectedRevision": 1}
```

`200 OK` → `{"mode": "Governed", "revision": 2}`. A stale `expectedRevision` is refused.

## 5. Health and metrics (every service)

| Path | Purpose |
|---|---|
| `/health/live` | liveness: the process is up |
| `/health/ready` | readiness: dependencies (database and, per service, what it needs) reachable |
| `/metrics` | Prometheus metrics: `capacity_http_requests_total`, `capacity_http_request_duration_seconds` |

## 6. Tested behaviour

`tests/component/test_api_boundaries.py` runs the real FastAPI app against a disposable PostgreSQL
with real service roles and locally signed tokens: missing tokens give a typed 401; invalid tokens
and `alg=none` are rejected; a viewer sees only granted namespaces and cannot request analyses;
analysis requests are asynchronous and idempotent; out-of-scope analyses and unknown operations are
opaque; no generic relay endpoints exist; arbitrary request fields are rejected; governance requires
the API's machine identity; a forged user header does not authenticate.
`tests/security/test_internal_mtls.py` proves the relay to governance over a real mutual-TLS handshake.
