# Proof of operation

This page collects the evidence that the platform described in these documents was **built and run
for real** on the reference host — a Windows 11 workstation with Docker Desktop/WSL2 — and states
just as plainly what has **not** been shown yet. Every item names its evidence file under
`~/.capacity-lab/` (on that host) and the command that reproduces it. All times are UTC on
2026-11-21 unless stated otherwise.

## 0. Summary

| Claim | Status | Evidence |
|---|---|---|
| The 4-node cluster, six platform services, supporting services and six workloads run | **proven** | §1 cluster snapshot |
| Network default-deny, least-privilege RBAC, admission policies and the executor field guard are enforced live | **proven** | §2 `verify-all-20261121T051022.json` |
| All automated suites pass on the deployed code — 257 / 257 cases | **proven** (E2E: see §4) | §3 junit evidence |
| The governed chain works end to end with four real users: analysis → plan → refused self-approval → approval → execution → exact patch → verification → automatic reversal | **proven once** | §4 `e2e-governed-change-20261121T035240Z.json`, audit chain |
| A change verified as safe (`verified_success`) on the live lab | **not yet shown** | §4, §10 |
| Refused commands are durably audited | **proven** | §5 |
| The audit chain is intact and matches host-signed checkpoints | **proven** | §6 |
| Encrypted backups restore into an isolated database | **proven** (one flaky drill found and fixed) | §7 |
| Load experiments are reproducible and the host watchdog protects the workstation | **proven** | §8 |
| Forecasts accept good data and refuse bad data | **proven** (accuracy not measured) | §9 |
| The local AI model is qualified | **no** — it failed qualification; the degraded mode works | §10 |
| Capacity savings, SLO improvements, forecast accuracy, B0–B3 comparisons | **not measured** | [evaluation.md](evaluation.md) |

## 1. The running lab

Snapshot taken at 05:30:

| Item | Observed |
|---|---|
| Nodes | `capacity-lab-control-plane`, `capacity-lab-worker` (platform), `capacity-lab-worker2`, `capacity-lab-worker3` (experiment) — all `Ready`, kubelet v1.36.4 |
| Pods | 38, all `Running` (capacity-ai 1, capacity-api 1, capacity-core 3, capacity-data 1, capacity-execution 1, capacity-identity 1, capacity-observability 3, capacity-system 4, cert-manager 3, kube-system 15, local-path-storage 1, workloads 4) |
| Deployments | `capacity-api`, `capacity-observer`, `capacity-capacity`, `capacity-governance`, `capacity-ai`, `capacity-executor`, `keycloak`, `grafana`, `kube-state-metrics`, External Secrets (3), `latency-api`, `burst-api`, `memory-profile`, `cpu-worker` — all 1/1 |
| Platform image | `localhost:5001/capacity-platform@sha256:7b22d7a860744e91ed9e5333b42e6bfb30a47e22435ad6431bf2aec4c4ebbc6a` on all six services |
| Operating mode | `Observe`, revision 3 |
| `GET /api/v1/capabilities` | inventory `available`, 1 h forecast `available`, optimization `available`, governed execution `disabled_by_mode`, AI `depends_on_qualified_model_gateway`, GitOps `unqualified`, S10/S23 `not_installed`; inventory `fresh`, coverage `complete` |
| NetworkPolicies | 46 |
| `capacity-lab preflight` | all 8 checks `ok` (13.7 GiB for Docker, 12 CPUs, 666 GiB free disk, kind v0.33.0, tools found, 5.9 GiB free host memory) |

Reproduce:

```powershell
kubectl --context kind-capacity-lab get nodes,deploy -A
```

```powershell
capacityctl capabilities
```

## 2. Live verification of the running lab

`capacity-lab verify all` at 05:10 on the final build — **all seven checks passed**
(`evidence/verify-all-20261121T051022.json`; the operational suite records the same checks again in
later `verify-all-*.json` files):

**Network default-deny** (probe Pods, real traffic):

| Probe | Observed |
|---|---|
| approved driver → demo workload :8080 | `CONNECTED` |
| unlabeled Pod in `capacity-experiments` → demo workload | `TIMEOUT` |
| Pod in `workloads-batch` → another namespace | `TIMEOUT` |
| DNS | `CONNECTED` |

**RBAC least privilege** (`kubectl auth can-i` as each ServiceAccount; expected = actual in all 12):

| Subject | Verb / resource / namespace | Allowed |
|---|---|---|
| `capacity-observer-sa` | list pods / workloads-batch | yes |
| `capacity-observer-sa` | patch deployments / workloads-batch | **no** |
| `capacity-observer-sa` | get secrets / capacity-core | **no** |
| `capacity-executor-sa` | patch deployments / workloads-batch | yes |
| `capacity-executor-sa` | patch deployments / capacity-core | **no** |
| `capacity-executor-sa` | update deployments/scale / workloads-batch | **no** |
| `capacity-executor-sa` | delete pods / workloads-batch | **no** |
| `capacity-executor-sa` | get secrets / workloads-batch | **no** |
| `capacity-ai-sa` | get pods / workloads-batch | **no** |
| `capacity-ai-sa` | patch deployments / workloads-batch | **no** |
| `capacity-api-sa` | patch deployments / workloads-critical | **no** |
| `capacity-engine-sa` | list pods / workloads-batch | **no** |

**Admission negative cases** (server-side dry runs): privileged Pod, missing limits, tag-only image,
token automount, writable root filesystem and a rogue experiment Job — all **denied**; a compliant Pod
**allowed**.

**Executor field guard:** a resources patch as `capacity-executor-sa` was allowed; changing replicas,
the image or the ServiceAccount was **denied**.

**Observability targets:** all up — `capacity-platform` 6, `demo-workloads` 4, `kube-state-metrics` 1,
`kubernetes-cadvisor` 4, `prometheus` 1.

**Placement and topology:** no Pod on the wrong pool; nodes control-plane 1, platform 1, experiment 2;
exactly the six platform Deployments.

Reproduce:

```powershell
capacity-lab verify all
```

## 3. Automated test results

Latest results, all on the deployed code (`evidence/tests/*.xml`, `summary.json`; run with
`capacity-lab release test`):

| Suite | Cases | Result | Run at |
|---|---|---|---|
| unit | 156 | **156 passed** | 05:40:39 |
| security | 31 | **31 passed** | 05:40:48 |
| component (API, Testcontainers PostgreSQL) | 21 | **21 passed** | 05:41:14 |
| integration (real PostgreSQL + pgvector, service roles) | 31 | **31 passed** | 05:41:43 |
| .NET workloads (xUnit) | 5 | **5 passed** | 05:44:40 |
| operational (live lab, gateway running) | 13 | **13 passed**, 0 skipped | 05:51:06 |
| end-to-end | 1 | latest junit record **failed** (an earlier attempt); the live chain itself ran at 03:21 — see §4 | 03:13:01 |

Operational runs on the final build:

| Run finished | Result | Note |
|---|---|---|
| 05:44:33 | 12 / 13 passed | the restore-drill test failed once: `pg_restore` started while the PostgreSQL image's temporary initialisation server was shutting down. The same backup then restored three times in a row after the drill's readiness probe was moved to TCP (§7) |
| 05:51:06 | **13 / 13 passed**, 0 skipped | includes a fresh backup `capacity-20261121T055020Z` restored in 27.9 s, and the model-gateway isolation probe (gateway running) |

Across all suites that is **257 / 257 passing cases** on the deployed code (E2E excluded).

Static checks: `ruff check` and `ruff format --check` clean; `mypy` reports 94 findings (not a gate).

Reproduce:

```powershell
uv run pytest tests/unit tests/security
```

```powershell
capacity-lab release test
```

## 4. The live governed change

The full chain ran through the public API with four real Keycloak users
(`evidence/e2e-governed-change-20261121T035240Z.json`, audit chain, `execution.operation_transition`):

| Time | Who | Event | Result |
|---|---|---|---|
| 03:12 | planner1 | analysis of `w04-cpu-worker` (6 h of E04 history) | memory candidate 192 Mi → 96 Mi **qualified**; every gate passed |
| 03:21:55 | admin1 | `PUT /mode` Observe → Governed | revision 1 → 2 |
| 03:21:57 | planner1 | `POST /plans` | `plan-1698278fbb3b` r1, `AwaitingApproval` |
| 03:21:57–03:22:03 | planner1 | approve own plan | **403** refused (separation of duties) |
| 03:21:57–03:22:03 | approver1 | approve with a wrong digest | **409** refused |
| 03:22:03 | approver1 | approve the exact digest | 201 accepted |
| 03:22:05 | operator1 | execution request | 202, `intent-fb7bbb62d95d` |
| 03:22:06 | executor | claim → preflight → JSON Patch → expected fields observed | `applied_pending_verification` |
| 03:22:09 | executor | verification started | rollout healthy at 03:22:39 |
| 03:39:14 | executor | verification finished | `verification_inconclusive` |
| 03:39:30 | executor | exact inverse 96 Mi → 192 Mi applied and observed | `recovered` |

The patch the executor sent:

```json
[{"op": "test", "path": "/metadata/uid", "value": "0e249f7a-d08a-44c1-971a-a15286033f22"},
 {"op": "test", "path": "/metadata/resourceVersion", "value": "40353"},
 {"op": "test", "path": "/spec/template/spec/containers/0/name", "value": "app"},
 {"op": "test", "path": "/spec/template/spec/containers/0/resources/requests/memory", "value": "192Mi"},
 {"op": "replace", "path": "/spec/template/spec/containers/0/resources/requests/memory", "value": "96Mi"}]
```

The recorded outcome (`execution.outcome`):

```json
{"operationalDisposition": "verification_inconclusive", "configuration": "pass", "rollout": "healthy",
 "safetyGuardrails": "unknown", "benefit": "inconclusive", "benefitReason": "guardrails_not_passed",
 "requestReservationDelta": {"cpu_m": 0, "memory_bytes": -100663296},
 "observedCpuDeltaMillicores": "-0.0772711665713608",
 "baselineWindowRef": "obs:verify:w04-cpu-worker:2026-11-21T03:07:06…",
 "postWindowRef": "obs:verify:w04-cpu-worker:2026-11-21T03:24:06…",
 "verifierRevision": "s26-verifier-v1", "planRevision": 1}
```

**What this proves:** authentication, the role matrix, separation of duties, exact-digest approval,
intent creation, the executor's authority re-check and preflight, the `test`-guarded patch, the
Kubernetes guard rails, the two-level verification and the automatic exact inverse — on the live
cluster, without manual help.

**What it does not prove:** a verified improvement. Prometheus held every guardrail signal for the
window (availability 1.0, no restarts, empty queue, ≈ 1.0 req/s before and after). But the verifier
requested the restart count below that query's 60 s minimum step, so the client refused it and the
guardrail was `unknown`. Unknown is never a pass, so the change was reversed as designed. The verifier
now uses each query's registered step; `test_healthy_fully_observed_w04_window_passes_its_guardrails`
proves it against the real registry, client and W04 policy. The live run has not been repeated on the
fixed build: the change and its reversal rolled W04 twice, so its forecasts refuse for about six hours
(§9).

**The E2E junit record:** the latest `e2e.xml` is a failure from 03:13, an earlier attempt that
stopped at `PUT /mode` with `500`. The API's relay to governance then passed the client certificate in
a way the installed HTTP library rejects. That defect was fixed and is covered by
`tests/security/test_internal_mtls.py`. The 03:21 run above did not finish as a pytest run, because
the test's access tokens expired during the 17-minute verification. Its evidence was captured from the
same API objects with fresh tokens, and the test now fetches a token per call.

## 5. Refused commands are durably audited

Live probes on the final build, read back from `audit.events`:

| Time | Action | Caller | Decision | Recorded reason |
|---|---|---|---|---|
| 04:11:44 | `mode.set` (to Governed) | viewer1 | deny | `authorization_denied` |
| 04:11:48 | `plan.approve` on a non-existent plan | approver1 | deny | `AUTHORIZATION_DENIED` (the caller saw only the generic 403) |

Both callers got a 403, and the mode stayed `Observe`. The integration test
`test_refused_commands_leave_durable_denial_records` proves the same for self-approval, a wrong digest
and a missing role against a real database.

## 6. Audit chain and signed checkpoints

`capacity-lab audit checkpoint` then `capacity-lab audit verify` at 05:10:

```json
{"intact": true, "chainBrokenAtSeq": null,
 "head": {"seq": 15, "eventHash": "6c92205194ed036bed111d53b31c26b1ba97a52537e47de3ae2684c6ee6a0808"},
 "checkpointsChecked": 7, "problems": [],
 "verifiedAt": "2026-11-21T05:10:27.838793+00:00"}
```

The chain holds 14 events: `mode.set` ×2 (fact), `plan.create` allow, `plan.approve` allow and deny,
`mode.set` deny, `execution.request` allow and `execution.transition` ×7. The signed checkpoint
for seq 15 has signature `65ce17b7…bdb0e`.

## 7. Backups restore into an isolated database

Each drill decrypts an AES-256-GCM backup, restores it into a PostgreSQL container with
`--network none`, and checks five things: ciphertext digest, `pg_restore` success, the migration set,
audit-chain integrity and the recorded audit head
(`backups/<id>/restore-verification.json`):

| Backup | Result | Restore duration |
|---|---|---|
| `capacity-20261119T220831Z` | all 5 checks passed | — |
| `capacity-20261120T070450Z` | not completed (container not ready within the then 120 s limit, disk saturated) | — |
| `capacity-20261120T075851Z` | all 5 passed | 25.5 s |
| `capacity-20261120T210249Z` | all 5 passed | 25.2 s |
| `capacity-20261121T040144Z` | all 5 passed | 26.6 s |
| `capacity-20261121T040748Z` | all 5 passed | 29.3 s |
| `capacity-20261121T054401Z` | first drill failed at `pg_restore` (readiness race); after the TCP readiness fix, 3 of 3 repeat drills passed | 27.5 s, 26.6 s, 26.6 s |
| `capacity-20261121T055020Z` | all 5 passed (operational suite) | 27.9 s |

Reproduce:

```powershell
capacity-lab backup create
```

```powershell
capacity-lab backup verify
```

## 8. Experiments and host safety

| Evidence | Observed |
|---|---|
| E04, 7 consecutive one-hour runs (21:11 on 2026-11-20 → 03:47) | each 3,312 / 3,312 requests succeeded at 0.9948 req/s, 0 dropped, p95 5.5–7.0 ms; **identical plan digest** `sha256:228a9722…` every time (same seed → same schedule) |
| E02 valid run | 5,050 / 5,050 at 16.8 req/s, p95 9.0 ms |
| E01 | 1,278 / 1,278 at 1.94 req/s, p95 9.9 ms |
| Capability enforcement | an E02 run with a mismatched capability got `401` for every request and was classified invalid |
| Workload self-protection | E03 was refused by W03 (`429`/`409`) and classified invalid |
| Host watchdog | at 04:36 free memory fell below 1.5 GiB → the running E04 run was aborted (`host_memory_pressure`); 11 further attempts were refused at preflight until memory recovered |
| Cleanup | every run deleted its objects by UID; after the final abort, `capacity-experiments` was empty and no run was open |

Each run's evidence is sealed under `evidence/experiments/<run-id>/` with a SHA-256 manifest. Details
are in [evaluation.md](evaluation.md#2-experiments).

## 9. Forecasting behaves as specified on live data

| When | Window | Forecasts |
|---|---|---|
| 02:44 | 72/73 CPU buckets, memory coverage 0.990 | **accepted** (CPU and memory) |
| 03:12 | gap-free, one Pod | accepted; memory candidate qualified |
| 03:56 | three W04 Pod series after the change and its reversal | **refused** (`unclassified_quality_issue` from `multiple_series_combined`) |

This shows both halves of the design: good data produces a forecast, and questionable data produces a
refusal rather than a guess. Accuracy has not been measured.

## 10. Not yet shown

* A live `verified_success` (the run in §4 ended `recovered`; it needs a clean six-hour single-Pod window
  and a steady E04 run to repeat).
* A W04 CPU decrease (its completion-rate evidence is not derived).
* A qualified local model; the AI degraded mode (`unavailable`, `model_unqualified`) is what works.
* B0–B3 comparisons, savings, SLO effects, forecast accuracy.
* A full rebuild from backup; an upgrade and rollback between releases.
* A passing release qualification: the latest bundle `rq-20261121T060550Z` (06:05) is **BLOCKED** by the
  failed E2E record and not qualified on RG03–RG06 ([evaluation.md](evaluation.md#6-release-qualification)).

## 11. Reproducing the evidence yourself

| To check | Run |
|---|---|
| host readiness | `capacity-lab preflight` |
| live security and topology | `capacity-lab verify all` |
| audit integrity | `capacity-lab audit checkpoint`, then `capacity-lab audit verify` |
| backups | `capacity-lab backup create`, then `capacity-lab backup verify` |
| all test suites | `capacity-lab release test` (start `capacity-lab model gateway` first) |
| the governed chain | build ≈ 6 h of E04 history, start ≈ 20 min into an E04 run, then `uv run pytest tests/e2e` |
| release gates | `capacity-lab release qualify` |
