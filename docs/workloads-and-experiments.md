# Demo workloads and experiments (S04, S05)

The platform needs something to observe and optimize. Six small, purpose-built workloads run on the
two experiment nodes, and a host-side scenario runner drives reproducible load against them. A host
watchdog protects the workstation while experiments run. Results of the runs are in
[evaluation.md](evaluation.md) and [proof-of-operation.md](proof-of-operation.md).

## 1. Workloads (S04)

| ID | Name | Kind | Namespace | Class / PriorityClass | Runtime | CPU req / limit | Memory req / limit |
|---|---|---|---|---|---|---|---|
| W01 | `latency-api` | Deployment | workloads-critical | Critical / `lab-critical` | .NET 10 | 200m / 500m | 256Mi / 384Mi |
| W02 | `burst-api` | Deployment | workloads-standard | Standard / `lab-standard` | .NET 10 | 150m / 400m | 192Mi / 320Mi |
| W03 | `memory-profile` | Deployment | workloads-standard | Standard / `lab-standard` | Python | 100m / 300m | 320Mi / 448Mi |
| W04 | `cpu-worker` | Deployment | workloads-batch | Flexible / `lab-flexible` | Python | 150m / 500m | 192Mi / 320Mi |
| W05 | `analytics-processor` | CronJob (suspended) | workloads-batch | Flexible / `lab-flexible` | Python | 250m / 500m | 384Mi / 512Mi |
| W06 | `opportunistic-batch` | Job template (ConfigMap) | workloads-batch | Opportunistic / `lab-opportunistic` | Python | 200m / 400m | 256Mi / 384Mi |

These requests and limits are the **B0 static baseline**: the values a team would have set by hand.
The catalog lives in [`contracts/workloads.py`](../src/capacity_platform/contracts/workloads.py) and
refuses to load if a workload's class and PriorityClass disagree.

| Workload | Behaviour | Built-in bounds | What it exercises |
|---|---|---|---|
| W01 `latency-api` (`workloads/dotnet/LatencyApi`) | ASP.NET Core minimal API; `POST /api/v1/compute` runs CPU-bound work | ≤ 4 concurrent requests, ≤ 10 work units per request | the latency/error SLO gate for a Critical workload |
| W02 `burst-api` (`BurstApi`) | `POST /api/v1/process`; bursty traffic | ≤ 8 concurrent, ≤ 5 work units | bursts, Standard SLO gate |
| W03 `memory-profile` (`memory_profile.py`) | allocates and holds memory in named profiles: `steady` 64 MiB/60 s, `ramp` 128 MiB/90 s, `spike` 180 MiB/30 s, `retain` 96 MiB/120 s | ≤ 2 concurrent runs (`429 too_many_runs`), 200 MiB ceiling (`409 internal_ceiling_reached`) | memory OOM/spike gate |
| W04 `cpu-worker` (`cpu_worker.py`) | accepts tasks into a bounded in-process queue; 2 worker threads hash a 1 MiB buffer (`light` 20, `normal` 80, `heavy` 240 rounds) | 30 s deadline: a task waiting longer is counted `deadline_missed` | CPU over-provisioning, queue growth, completion within deadline |
| W05 `analytics-processor` (`batch.py`) | run-to-completion analytics: `analytics-small` (50,000 rows), `analytics-large` (400,000 rows) | 600 s run deadline | run envelopes for batch jobs |
| W06 `opportunistic-batch` (`batch.py`) | run-to-completion: `batch-short` (200 rounds, 16 MiB), `batch-long` (1,500 rounds, 64 MiB) | 600 s deadline; lowest priority — expected to wait when capacity is short | Pending/placement behaviour |

All workloads expose `/health/live`, `/health/ready` and `/metrics` on port 8080 with bounded labels
(`workload`, `operation`, `outcome`): `lab_requests_total`, `lab_request_duration_seconds`, and for W04
`lab_queue_depth`, `lab_tasks_completed_total{outcome="completed"|"deadline_missed"}`,
`lab_task_duration_seconds`, `lab_task_queue_delay_seconds`, `lab_tasks_inflight`. Batch runs are
seeded, so their output digest is reproducible.

### Experiment capabilities

Every load-bearing endpoint requires an **experiment capability**: a short-lived ES256 JWT issued on
the host for one run, scoped to the run's workloads and operations, and verified by the workload
against the public-key ConfigMap. Without it, load cannot be generated (an E02 run in which the
capability check used the wrong workload ID was answered `401` for every request and classified
invalid). A missing verifier key fails closed. Workloads enforce their own maximum intensity whatever
a request asks for.

### Hardening

All demo Pods run as non-root (UID 10001) with a read-only root filesystem (only `/tmp` writable via
an emptyDir), no ServiceAccount token, `RuntimeDefault` seccomp, all capabilities dropped, a
digest-pinned image, explicit requests and limits, and a required node affinity to the experiment
pool.

## 2. Scenarios (S05)

A scenario file (`configs/scenarios/*.yaml`) declares targets, phases, actions, safety limits and a
seed:

| Scenario | Target | Class | Phases | What it shows |
|---|---|---|---|---|
| `e01-cpu-overprovisioning.yaml` | W04 | moderate | warmup → measured → cooldown | sustained load far below the CPU request → downsizing candidates |
| `e02-latency-guardrail.yaml` | W01 | moderate | baseline then heavy load | latency SLO gate |
| `e03-memory-spike.yaml` | W03 | exclusive | steady then spike | memory OOM/spike gate (revision 1 exceeds W03's own bounds; see §6) |
| `e04-steady-demand.yaml` | W04 | moderate | warmup 30 s → steady-a 1650 s → steady-b 1650 s → cooldown 30 s, 1 light task/s | about one hour of constant demand; run back to back it builds continuous history and comparable load before and after a governed change |

**Validation** (`capacity-scenario validate`) rejects: unknown targets, operations or profiles; rates
above hard caps; arbitrary commands; deferred fault types; a phase longer than 1800 s or a scenario
longer than 3600 s; and any phase order other than *warmup (excluded) → measured (included)+ →
cooldown (excluded)*. E04 uses two identical measured phases precisely because one phase may last at
most 30 minutes.

### Reproducible demand

`capacity-scenario plan` builds the full arrival schedule before anything runs, using named PCG64
random streams per action (changing one phase does not shift the others) and thinning for
time-varying rates. The same scenario and seed always give the same plan digest — all seven passing
E04 runs had plan digest `sha256:228a9722…` and exactly 3,312 planned arrivals. Measurements still
vary between runs, and the evidence says so.

## 3. What a run does

```mermaid
sequenceDiagram
  participant R as capacity-scenario (host)
  participant J as SQLite journal
  participant W as watchdog
  participant K as Kubernetes
  participant D as load-driver Job
  participant T as target workload
  R->>W: preflight: heartbeat fresh, state ok
  R->>J: lease (serial/exclusive, fencing generation)
  R->>K: create plan ConfigMap, capability Secret, driver Job (each journaled first)
  K->>D: start on the experiment pool (admission-checked)
  R->>R: wait until the driver Pod is Running (≤ 300 s), then start the phase clock
  D->>T: planned arrivals (open-loop Poisson), capability token
  R->>J: heartbeat every 2 s; stop on abort or lost lease
  D-->>R: SUMMARY (counts, latencies, rates, drops)
  R->>R: classify run quality
  R->>K: delete every created object by exact UID
  R->>R: seal evidence (manifest with SHA-256 per file)
```

1. **Preflight:** watchdog alive and not under pressure; no other open run for non-light classes (the
   run being checked is ignored); targets ready; quota visible.
2. **Lease:** a serial or exclusive lease with a fencing generation in the SQLite journal.
3. **Create** (each step journaled as an intent first): the plan ConfigMap, the capability Secret, the
   load-driver Job in `capacity-experiments` (admission-checked template, pinned image, experiment pool
   only, finite deadline), and any W05/W06 batch Jobs built **only** from the installed templates
   (changing nothing but profile and seed).
4. **Phases:** the phase clock starts only once the driver Pod is running (otherwise the run aborts
   with `driver_not_started`); the lease is heartbeated every 2 s.
5. **Collect:** the driver's `SUMMARY` line and batch Job results, including why a Pod did not
   schedule. The run is classified in `run-quality.json`: `passed_pending_evaluation` only if at least
   95 % of requests were answered successfully and at most 5 % of planned arrivals were dropped;
   otherwise `invalid`. An invalid run is never evidence about the workload.
6. **Cleanup:** every created object is deleted **by exact UID** (Kubernetes delete preconditions); an
   object whose creation was uncertain is never deleted by name alone.
7. **Seal:** `manifest.json` with the SHA-256 of every evidence file under
   `~/.capacity-lab/evidence/experiments/<run-id>/` (`scenario.resolved.json`, `demand-plan.json`,
   `preflight.json`, `phase-results.json`, `driver-summary.json`, `run-quality.json`,
   `cleanup-report.json`, `manifest.json`, `manifest.sha256`).

`capacity-scenario recover` handles a runner that died mid-run: it never resumes a half-run phase; it
aborts, cleans up owned objects by UID and marks the run `inconclusive`.

The runner uses its own kubeconfig context (`capacity-lab-runner`). It can create and delete
Jobs/ConfigMaps/Secrets in `capacity-experiments`, create and delete batch Jobs in `workloads-batch`,
read only the two batch templates, and read demo Deployments. It has no `update` or `patch` permission
anywhere, so it can never change a workload's resources.

### Running an experiment

```powershell
capacity-scenario validate configs/scenarios/e04-steady-demand.yaml
```

```powershell
capacity-scenario plan configs/scenarios/e04-steady-demand.yaml
```

```powershell
$env:DRIVER_IMAGE = (Get-Content $HOME\.capacity-lab\release\images.json | ConvertFrom-Json).platform
```

```powershell
capacity-scenario run configs/scenarios/e04-steady-demand.yaml
```

```powershell
capacity-scenario status <run-id>
```

```powershell
capacity-scenario abort <run-id>
```

## 4. Host safety watchdog

`capacity-watchdog run` is a separate process that every 10 s reads Windows available memory
(`GlobalMemoryStatusEx`) and the kind node containers' memory:

| Available memory | State | Effect |
|---|---|---|
| ≥ 2.0 GiB | `ok` (or `exclusive` during an exclusive run) | normal |
| < 2.0 GiB | `pressure` | new heavy experiments refused; the executor and the `host.safety` gate treat the host as not OK |
| < 1.5 GiB | `abort` | all open runs are asked to abort |

It publishes the state to the `capacity-system/host-safety` ConfigMap (through its own narrow
context) and heartbeats the journal. If the watchdog stops, the runner sees a stale heartbeat and
refuses heavy runs, and the executor sees a stale signal (older than 2 minutes) and refuses changes —
both fail closed. The thresholds are provisional.

The watchdog acted for real on 2026-11-21: at 04:36 free memory fell below 1.5 GiB, it aborted the
running E04 run (`abort_reason: host_memory_pressure`), and the next 11 attempts were refused at
preflight until memory recovered.

## 5. Recorded runs

| Scenario | Runs | Result |
|---|---|---|
| E01 | 1 valid (2026-11-20 06:37) | 1,278 / 1,278 requests succeeded, 1.94 req/s offered = achieved, p50 5.9 ms / p95 9.9 ms |
| E02 | 1 invalid, 1 valid (08:13) | valid run: 5,050 / 5,050 succeeded at 16.8 req/s, p50 7.0 ms / p95 9.0 ms |
| E03 | 1 invalid | W03 protected itself (`429`/`409`); the scenario exceeds W03's bounds |
| E04 | 7 valid, 1 watchdog abort, 11 refused at preflight, 1 operator abort | each valid run: 3,312 / 3,312 succeeded at 0.99 req/s, p95 5.5–7.0 ms, no drops |

Details and evidence paths: [evaluation.md](evaluation.md).

## 6. Known issues

* **E03 revision 1 is invalid by design of the workload:** it sends Poisson arrivals averaging one every
  20 s, which exceeds W03's two-concurrent-run and 200 MiB bounds. A redesigned scenario with
  sequential profile runs is needed before E03 can produce evidence.
* The load driver is open-loop (Poisson arrivals); it does not model closed-loop clients.
* Between back-to-back E04 runs there is a gap of about 1–2 minutes (cooldown, runner start-up,
  driver start), during which the 5-minute request rate dips.
