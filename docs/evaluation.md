# Evaluation: what has been measured, and what has not (S31)

This page states what the evaluation protocol requires, which measurements have been made on the
reference host, and which have not. The reference host is a Windows 11 workstation with 20 GiB RAM
and Docker Desktop/WSL2, with Docker's data disk on a 7200 rpm hard disk; the GPU is described in
S15. Every number names its evidence file under `~/.capacity-lab/`. Nothing below is estimated or
extrapolated. Evidence that the platform itself works — live verification, tests, the governed change,
backups, audit — is collected in [proof-of-operation.md](proof-of-operation.md).

## 1. Evaluation protocol

The platform is meant to be compared in four arms on the same scenario, seed and workload profile:

| Arm | Meaning | Available in this lab |
|---|---|---|
| **B0** | static configuration (the S04 baseline requests/limits) | **yes** — the deployed baseline |
| **B1** | a rules/VPA recommendation, applied only through the same governed path | **no** — no VPA or rules recommender installed |
| **B2** | S14 forecasts + deterministic S19–S22 optimization, governed execution | **yes** — deployed; needs ≈ 6 h of covered, single-Pod telemetry per workload |
| **B3** | B2 plus the AI Analyst → Planner → Reviewer review | **no** — the local model is not qualified on this host |

Fairness rules from the specification:

* identical scenarios, seeds and SLO policy across arms;
* B3's numbers must equal B2's for the same inputs (the AI cannot change numbers);
* a "no change" or a refusal is a legitimate outcome, not a discarded run;
* invalid runs (run-quality `invalid`, host pressure, generator overload) are reported but excluded
  from comparisons;
* paired, repeated runs are required before any comparative claim.

**Status: no B0–B3 comparison has been run. No claim is made that the platform saves capacity,
improves SLOs, or that the AI layer adds value.**

## 2. Experiments

### E01 — W04 CPU over-provisioning, 2026-11-20 06:37

Evidence: `evidence/experiments/run-20261120T063745-5c1b61/` (sealed manifest).

| Measure | Value |
|---|---|
| planned / dispatched / completed requests | 1,278 / 1,278 / 1,278 |
| successful responses | 1,278 (100 %) |
| offered vs achieved rate | 1.94 req/s vs 1.94 req/s |
| client latency p50 / p95 | 5.9 ms / 9.9 ms |
| dropped arrivals | 0 |
| cleanup | 3 objects deleted by UID, 0 remaining |

Caveat: in this run the load driver started about four minutes after the runner's phase clock because
its image was still being pulled. The runner now waits for the driver Pod to run before starting the
clock. Two other attempts that day were correctly refused at preflight, one because free host memory
was below the watchdog's abort threshold.

### E02 — W01 latency guardrail, 2026-11-20

| Run | Outcome | Requests (planned = dispatched) | Success | Client p50 / p95 |
|---|---|---|---|---|
| `run-20261120T080155-1e42cd` (08:01) | **invalid** — every response was `401` | 5,050 | 0 | — |
| `run-20261120T081315-581163` (08:13) | `passed_pending_evaluation` | 5,050 | 5,050 (100 %) | 7.0 ms / 9.0 ms at 16.8 req/s |

The invalid run showed that the .NET workloads checked experiment capabilities against the wrong
workload identifier, so W01/W02 refused all load — the capability check failed closed. With the
correct identifier the run was valid. Under the current run-quality rules the first run is `invalid`
and is not used as evidence.

### E03 — W03 memory spike, 2026-11-20

`run-20261120T081935-74a237`: **invalid** — 17 planned requests, 9 succeeded, 5 refused by W03 with
`429 too_many_runs`, 3 with `409 internal_ceiling_reached`. W03 deliberately allows two concurrent runs
and a 200 MiB ceiling (steady = 64 MiB for 60 s, spike = 180 MiB for 30 s); scenario revision 1 sends
Poisson arrivals averaging one every 20 s, which exceeds those bounds. The workload protected itself as
designed and run-quality correctly refused the result. E03 needs a redesigned scenario (sequential
profile runs) before it can produce evidence.

### E04 — W04 steady demand, 2026-11-20 21:11 – 2026-11-21 04:54

Run back to back to give W04 continuous history and comparable load before and after a change.

| Runs | Outcome |
|---|---|
| 7 (`run-20261120T211147-07ac81`, `…T220843-20aa0b`, `…T230522-d5f2f6`, `run-20261121T000155-80dfaa`, `…T005826-f9df42`, `…T015456-c90fda`, `…T025126-f8e037`) | `passed_pending_evaluation` |
| 1 (`run-20261121T034759-88c7af`) | aborted at 04:36 by the host watchdog (`host_memory_pressure`, free memory below 1.5 GiB) |
| 11 (04:36–04:41) | refused at preflight while host memory was still low — the intended behaviour |
| 1 (`run-20261121T044108-b68f19`) | aborted by the operator when the load was stopped |

Each of the seven valid runs:

| Measure | Value (all seven) |
|---|---|
| plan digest | `sha256:228a97222652ff3e8e68fdfee1e67b88a518030b4094da224df1888cc045fbd7` (identical: same seed, same schedule) |
| planned = dispatched = completed = successful | 3,312 |
| offered = achieved rate | 0.9948 req/s |
| dropped arrivals | 0 |
| measured duration | 3,329.4 s |
| client p50 | 4.67 – 5.19 ms |
| client p95 | 5.50 – 7.03 ms |

W04 itself completed about 19,650 tasks in the 6 hours before 02:46 with **no** deadline misses
(Prometheus `lab_tasks_completed_total`), using about 14–15 millicores of CPU and 35–40 MiB of
memory against its 150m / 192 Mi requests.

## 3. Live forecasting and analysis

| When (UTC) | Telemetry in the 6-hour window | Result |
|---|---|---|
| 2026-11-20, after deployment | well under 6 h | both forecasts refused (`coverage_below_policy`) |
| 2026-11-20, after host downtime | almost none | both forecasts refused |
| 2026-11-21 02:44 | 72/73 CPU buckets, memory coverage 0.990 | forecasts **accepted**; three candidates, all `indeterminate` |
| 2026-11-21 03:12 | fully covered, one Pod | memory candidate **qualified** |
| 2026-11-21 03:56 | three Pod series (after a change and its reversal) | both forecasts refused (`unclassified_quality_issue`) |

At 02:44 the candidates were CPU request 150m → 75m, memory request 192 Mi → 96 Mi, and both together:

* the memory candidates stopped at `memory.oom_spike = unknown`, because one 45-second gap (the moment
  telemetry resumed after downtime) was still in the window — a gap means the sampled peak is not
  guaranteed;
* the CPU candidates stopped at `slo.queue_completion = unknown` (`queue_evidence_unknown`), because the
  evidence does not derive W04's completion-within-deadline rate ([limitations.md](limitations.md)).

Every other gate on those candidates passed: scope, allow-list, evidence integrity, supported action,
resource validity, scheduler fit, rollout disruption, HPA compatibility and host safety.

Forecast **accuracy** and interval coverage on live data have **not** been measured; an accepted
forecast only means its inputs passed the quality checks and its backtest met the policy.

## 4. Live governed change

One complete pass of the governed chain ran on 2026-11-21 03:21–03:39 (memory request 192 Mi → 96 Mi on
W04). It was applied exactly as planned, then verification ended `inconclusive` and the executor
applied the exact inverse (`recovered`). It is **not** a verified success, and it is not an evaluation
of benefit. The step-by-step record is in
[proof-of-operation.md](proof-of-operation.md#4-the-live-governed-change).

## 5. Local model qualification

Three runs through the real gateway, same pinned models and unchanged thresholds. Result: **not
qualified**, so the AI layer stays in its degraded mode (advisories end `unavailable`, reason
`model_unqualified`).

| Run (UTC, evidence file) | Host state | Warm-up | Schema valid | Grounded | Latency p50 / max | Injection cases | Embeddings |
|---|---|---|---|---|---|---|---|
| 2026-11-20 05:09 `model-qualification-20261120T053418Z.json` | disk saturated | — | 0 / 6 (all timed out at 180 s) | 0 / 6 | — | 0 / 2 completed | pass |
| 07:45 `model-qualification-20261120T075123Z.json` | quiet | — | 4 / 6 (1 cold-start timeout, 1 schema violation) | 4 / 6 | 20.4 s / 22.9 s | 2 / 2, nothing unsafe accepted | pass |
| 07:53 `model-qualification-20261120T075509Z.json` | quiet | 4.6 s (model already loaded) | 4 / 6 (2 schema violations) | 4 / 6 | 19.6 s / 22.4 s | 2 / 2, nothing unsafe accepted | pass |

Gateway guards passed 6 / 6 in every run. The generation model (`gemma4:e4b`) produces output violating
the strict advisory schema in about a third of responses, below the 0.8 adherence gate; the trusted
validator rejected those outputs as designed. Groundedness (0.67 ≥ 0.5), latency and prompt-injection
safety met their gates on the quiet host. A different or larger model, or tuned prompts, would need a
new qualification run.

## 6. Release qualification

Latest bundle: `release-qualification/rq-20261121T060550Z/qualification.json` (2026-11-21 06:05, on the
final build), outcome **BLOCKED**:

| Gate | Verdict | Open items |
|---|---|---|
| RG01 architecture traceability | passed | — |
| RG02 tests and security | **blocked** | `tests:e2e` failed (hard) — the latest E2E junit record is the 03:13 attempt that stopped on the relay defect; no E2E run has completed on the current build |
| RG03 quantitative evaluation | not qualified | B1 rules/VPA baseline not installed; no paired B0–B3 comparison; model unqualified |
| RG04 operational reliability | not qualified | host-safety watchdog not running at qualification time; full rebuild-from-backup not rehearsed |
| RG05 delivery compatibility | not qualified | no upgrade/rollback rehearsal (no earlier release) |
| RG06 docs and handoff | not qualified | no attributable human reviewer record |

Every other item passed, including all other hard items: the security, component, integration and
operational suites, live security verification, the restore drill, the audit chain and checkpoints, and
the migration checksum ledger. RG02 unblocks only with a completed E2E run on the current build.
Recompute with `capacity-lab release qualify`; how the gates work is in
[release-and-upgrade.md](release-and-upgrade.md#4-release-qualification-s32).

## 7. Not measured

* B0–B3 paired comparisons, resource savings and SLO effects.
* Forecast accuracy and interval coverage on live lab telemetry.
* A live `verified_success`, and any measured benefit of an applied change.
* Optimizer behaviour on live data beyond the analyses above (W01–W03 have not been analysed under
  sustained load).
* AI review quality, groundedness on real evidence, and B3-versus-B2 marginal value.
* Full rebuild-from-backup RTO; upgrade and rollback between releases.
