---
name: validate-capacity-intelligence
description: Validate Kubernetes resource inventory, Prometheus telemetry, statistical forecasting, deterministic right-sizing, placement feasibility and optimization simulation against S11-S14 and S19-S23. Use for numerical validation, model evaluation, fixtures, and capacity decision quality gates.
---

# Validate Capacity Intelligence

## Mission and source authority

Validate the **correctness, lineage, uncertainty, eligibility and limitations** of capacity conclusions before they reach governance or AI. Read `AGENTS.md`, `CLAUDE.md`, master catalog, S11-S14, `S14_Forecasting_Model_Evaluation_Contracts.md`, the combined S19-S23 Domain E file **including its numerical/simulation companion**, and relevant S04-S05/S13/S24/S30-S31. Read actual implementation and registered query/model/policy versions. Accepted specifications and ADRs override examples in this skill; thresholds marked provisional require calibration.

## Semantic guardrails

- Never equate Kubernetes `capacity`, `allocatable`, effective Pod **requests**, resource **limits**, observed CPU/memory, numerical **forecasts**, hypothetical simulation results, applied configuration, or verified realized outcomes.
- CPU is labeled in millicores; memory in bytes; exact units/dimensions and statistic kind travel with evidence. Use UTC, explicit `[start,end)` intervals and immutable refs/digests.
- Missing/partial/stale measurements are **not zeros**; sampled memory peaks are not true instantaneous peaks; sampling and counter reset/identity churn matter.
- Node fit depends on effective Pod requests, allocatable and supported scheduler constraints, **not average observed consumption**.
- `resourceVersion` is opaque and cannot be treated as a globally ordered timestamp. Pod/controller UID and cluster incarnation distinguish recreation and historical configuration.
- Only S14 deterministic code computes numerical demand predictions. Agent prose and retrieved incidents cannot generate or override forecasts or hard policy verdicts.
- `current` is an assessment, `1h` is a forecast when eligible, `24h` is conditional; future W05/W06 demand is conditional on a specific scheduled execution/profile.
- Candidate, optimization evaluation, plan, approval, application and realized outcome are separate objects; a simulated request saving is **not measured CPU saving**.

## Verification workflow

### A. Resolve inputs and authority

1. Identify the target workload IDs W01-W06, exact namespace/controller/container, cluster incarnation, versioned `WorkloadProfile`, S05 scenario/run/seed/phase and evidence origin (workload vs measurement).
2. Resolve committed S11 immutable inventory snapshot, S12 registered query revision and observed windows, S13 evidence manifests, S14 forecast profile and S19-S22 policy/model/simulation revisions. Verify digest, authorization, identity and point-in-time validity; never fabricate fixtures as live evidence.
3. Determine required analysis mode (inspection / forecast / candidate / placement fit / analytical simulation / empirical evaluation) and the exact qualified input contract.

### B. Inventory and telemetry checks (S11-S13)

4. Check S11 LIST→WATCH→periodic reconciliation behavior including lost/expired watch, relist, deleted/recreated objects, owner graph, effective Pod scheduling requests, init/overhead considerations, allocatable and unscheduled demand; assess resource-family freshness and non-atomic snapshot skew.
5. Check metric name/unit/type, counter rate resets, `kube-state-metrics` semantics vs cAdvisor usage, app SLO metrics, short-lived Job blind spots, missing targets, multiple replicas, allowed label cardinality, Prometheus scrape retention and registered-only PromQL.
6. Validate observation coverage, sampling fidelity, contamination (host pressure, overloaded generator, HPA, changing spec), time alignment and source availability. Ensure SQL stores compact immutable evidence and refs, not copies of the whole Prometheus TSDB.

### C. Forecasting checks (S14 + companion)

7. Verify time-safe feature assembly and `asOfUtc` cutoffs using both event and availability time when needed; never use future CPU/memory, achieved throughput, Pod placement, future latency or errors in training features.
8. Evaluate persistence/rolling/appropriate exponential-smoothing baselines and supported challengers with **rolling-origin walk-forward** backtesting and point-in-time eligibility; no automatic selection by complexity.
9. Assess targets and horizon granularity (including bucket completeness), unit correctness, error metrics, interval/quantile calibration where evidence supports them, model provenance, workload-specific W01-W06 behavior and typed refusal when insufficient.
10. Test stability under gaps, spikes, OOM censoring, regime shifts, cold start, confounded HPA and low sample counts. Do not claim numerical confidence/probability absent calibration.

### D. Optimization and placement checks (S19-S23)

11. Verify S19 rule hierarchy, `pass | fail | unknown | not_applicable` semantics, mandatory gates **before** scoring and blocking of infrastructure/system/identity/observability targets.
12. Verify S20 deterministic finite bounded candidate generation and exact CPU/memory request/limit fields for **one allowlisted demo controller**; no free-form LLM-proposed numeric mutations.
13. Verify S21 resource fit under currently supported scheduler semantics and fragmentation metrics. If affinity/topology/PDB/taints/priority constraints are unsupported or state ambiguous, return explicit unknown/refusal rather than optimistic `fits`.
14. Verify S22 deterministic baseline vs candidate analytical simulation, reproducible ranking and assumptions, evidence/version identity, no future information leakage and honest model fidelity limits.
15. Treat S23 HPA/advanced autoscaling as **P1**; W02 HPA is off in baseline. Do not expand P0 mutating target to replicas/HPA/VPA.

### E. Experiment and safety checks

16. For empirical claims, use S05's separately authorized serial runner with independent watchdog, bounded workloads, immutable run bundles and S31 B0/B1/B2/B3 study design. Avoid competing experiment/Executor changes and verify cleanup ownership.
17. Classify every claim as `reference_fixture`, `analytical_simulation`, `observed_measurement`, `forecast`, `applied_configuration`, or `qualified_realized_outcome` using the authoritative owner and evidence.
18. Run focused deterministic/unit/property tests before integration tests; use kind/Prometheus operational verification only where actual environment behavior matters. Record exact commands and whether tests were skipped.

## Required negative/property cases

Check monotonic/sane units and integer rounding; missing≠zero; invalid NaN/Inf/negative data rejection; cross-incarnation/UID mixing; stale inventory; counter reset; short burst aliasing; ephemeral Job gaps; `resourceVersion` ordering assumption; impossible fit under hard constraints; `unknown` hard gate; candidate sorting/dedup/idempotency; HPA coupling; requested reservation vs observed consumption; time-compressed workload falsely labeled 24h history; changed policy/model digest; live state drift before S25 execution.

## Deliverable

Return an **evidence-qualified validation report**: analyzed scope and immutable input refs, numerical semantics, model/forecast quality and refusal reasons, scheduler constraint fidelity, candidate and simulation status, test cases and observed results, uncertainty/contamination, and unverified/blocked assertions. Separate measured empirical outcomes from analytical predictions, and never issue an approval or invoke optimization mutation.
