# Known limitations and risks

Honest boundaries of the current system — what it does not do, or does not do well yet. What
production would need is in [production-considerations.md](production-considerations.md); measured
results, and what has not been measured, are in [evaluation.md](evaluation.md) and
[proof-of-operation.md](proof-of-operation.md).

## Scope

* **Single host, not highly available.** Everything runs on one Windows workstation; a host or Docker
  restart interrupts the platform, can change node addresses and seals Vault (`capacity-lab resume`
  recovers it).
* **Demo workloads only.** The executor may change only the three demo namespaces
  (`workloads-critical`, `workloads-standard`, `workloads-batch`), and only CPU and memory
  requests/limits of Deployments. CronJobs and Job templates (W05/W06) are analysed but never patched;
  plans for them are `Blocked`.
* **No autonomous changes.** Every change needs a human plan, a different human approver and a human
  execution request (autonomy levels L0–L2; there is no L3).
* **P1 capabilities are not installed:** the GitOps (Flux/Argo) adapter, HPA advisory (S23) and
  supply-chain admission (S10). The API reports them as `unqualified` or `not_installed`;
  GitOps-owned targets get advisory-only plans.

## Not yet demonstrated

* **No live `verified_success`.** The governed chain has run end to end on the live lab once. It ended
  `verification_inconclusive` → `recovered`, because the verifier could not read one guardrail at the
  time. That defect is fixed and covered by unit tests, but the live run has not been repeated, and no
  measured benefit of an applied change exists.
* **No comparisons.** No B0–B3 evaluation, forecast-accuracy measurement or savings claim has been
  made ([evaluation.md](evaluation.md)).

## Data and models

* **Forecasts need time.** The 1-hour forecast needs about six hours of well-covered telemetry (6-hour
  lookback, ≥ 95 % coverage, ≥ 4 hours of history). A new workload, or any workload after host
  downtime, is refused until then; the 24-hour forecast needs three days.
* **A rollout pauses forecasting for that workload for about six hours.** Telemetry queries select a
  workload's Pods by name prefix. When the lookback window holds more than one Pod (any rollout,
  including a governed change or its reversal), the series are flagged `multiple_series_combined`,
  which the forecasting policy does not classify and therefore refuses. Accepting such windows safely
  needs a Pod-UID-to-controller lineage check, which is not implemented. In practice a workload can be
  changed at most about once every six hours.
* **W04 CPU decreases cannot qualify.** The `slo.queue_completion` gate needs W04's
  completion-within-deadline rate. W04 exports the counts (`lab_tasks_completed_total` by outcome) and
  the policy sets a 99 % target, but the analysis evidence does not derive the rate, so the gate stays
  `unknown` and W04 CPU candidates are always `indeterminate`. Memory candidates are not affected.
* **Any telemetry gap blocks memory decreases.** A single missing 15 s sample inside the 6-hour window
  sets `scrape_gap` → `peak_not_guaranteed`, and the memory gate stays `unknown` until the gap leaves
  the window.
* **Sampled peaks are not true maxima.** Telemetry is sampled every 15 s and aggregated to 5-minute
  buckets; short spikes can be missed. Memory gates add OOM and restart history and a margin to
  compensate.
* **Thresholds are provisional.** Policy values (margins, floors, coverage, SLO limits, verification
  windows, watchdog thresholds, model-qualification gates) are starting points from the specification,
  not values calibrated by repeated experiments.
* **Placement simulation is an approximation** of the Kubernetes scheduler: no topology spread,
  inter-Pod affinity, volume topology or scheduler plugins (such candidates are `indeterminate`).
* **Verification is short and single-window.** 120 s stabilization and 900 s evaluation are lab values;
  slow-building problems (memory leaks over hours) are not caught.
* **The local model is not qualified** on the reference host (schema adherence about 0.67 against a
  0.8 gate). The AI layer stays in its degraded mode (`model_unqualified`), and W01 plans — which need
  a completed review — stay advisory-only.

## Host resources

* The full lab uses roughly 7 GiB in the Docker VM. With other applications open, free memory can fall
  below the watchdog thresholds, which blocks or aborts experiments by design (observed on 2026-11-21).
* With Docker's data on a hard disk, building images or loading models saturates the disk and makes
  etcd and the API server unresponsive for minutes; Testcontainers-based tests can then time out and
  are reported as skipped. An SSD for Docker data is strongly recommended
  ([troubleshooting.md](troubleshooting.md)).

## Security gaps in the lab

* `capacity-system` (Vault, External Secrets) and `cert-manager` are not under default-deny network
  policy; `capacity-system` enforces Pod Security `baseline`, not `restricted`.
* The database network policy admits clients from the platform namespaces; separation between services
  at the database is by credentials, grants and row-level security.
* The API accepts the test-only `capacity-e2e` password-grant client so the automated live tests can
  run.
* Vault uses a single unseal key share and file storage; the root CA, backup and checkpoint keys are on
  the same host as the backups; the lab CA publishes no revocation list.
* Keycloak runs with a writable root filesystem (it writes build artefacts at start).
* There is no image-signature verification at admission (S10 is a P1 feature).

## Observability

* Platform metrics are HTTP-level only; there are no domain metrics (queue depth, refusal rates,
  executor outcomes) and no alert rules. One overview dashboard is provisioned.
* Logs stay in the Pods (no aggregation); there is no tracing; Prometheus retention is local.

## Experiments

* The load driver is open-loop (Poisson arrivals).
* Scenario E03 (W03 memory spike) revision 1 exceeds W03's own run limits and its runs are classified
  `invalid`; it needs a redesigned scenario.
* Back-to-back runs leave a 1–2 minute gap during which the measured request rate dips.

## Engineering

* `mypy` is configured but reports 94 findings (dictionary-heavy manifest builders and dynamic
  constructions); it is not part of a release gate.
* Base images are pinned by tag, not digest, so rebuilding a workload image can change its digest from
  unchanged sources.

## Operations not rehearsed

* A full cluster rebuild from a backup (only the isolated database restore drill is automated and
  verified).
* An upgrade from one release to the next, and a rollback (there is no earlier release).
