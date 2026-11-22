---
name: qualify-platform-release
description: Qualify a local Autonomous Kubernetes Capacity Planning release using S30 risk-based testing, S31 B0-B3 scientific evaluation, S32 capability-specific readiness gates, S28 recovery and S29 build/delivery evidence. Use for release reviews, evidence bundles, acceptance and documentation verification.
---

# Qualify Platform Release

## Mission and required reading

Produce a **capability-scoped, evidence-backed release verdict**. Review repository `AGENTS.md`, `CLAUDE.md`, master catalog, relevant accepted slice ADRs, S14 forecasting companion, consolidated Domain E numerical/simulation companion, Domain F S24-S26, Domain G S27-S29 and Domain H S30-S32. Inspect the actual checked-out revision and test/CI/deployment/evaluation artifacts. If specific instructions disagree, authoritative accepted source and approved ADRs win; flag conflicts for owner review rather than silently altering scope.

**An accepted architecture is not an implemented feature. A passing unit test is not deployed verification. A benchmark improvement is not authorization. A release qualification is not human plan approval.**

## Inputs and preflight

1. Record Git commit, build/image digest(s), toolchain/dependency locks, config/profile revisions, migration versions, spec/ADR revisions, cluster incarnation, model/embedding revisions, installer/CI version and environment resource profile.
2. Identify the exact **release capabilities** being claimed (e.g. inventory, approved telemetry, current/1h forecasts, conditional 24h, deterministic candidate, scheduling fit, governed direct execution, AI advisory/RAG, optional P1 supply-chain/GitOps/autoscaling). Do not automatically require or claim every P1 feature for a P0 profile.
3. Check S03 resource/host/disk constraints and active S05 scenario exclusivity; reserve isolated windows for stress/mutation tests. Reuse existing infrastructure; do not spawn unnecessary permanent services.
4. Construct a requirement→implementation→test→evidence→claim traceability map, including security, operational failure and data-quality requirements, not just happy paths.

## S30 — Correctness, security and reliability

5. Select **cheapest reliable layer** following `CLAUDE.md`: meaningful unit/property/stateful tests; moderate real-dependency integration (Testcontainers); few authenticated API/component tests and E2E; kind operational tests when actual networking/admission/rollout is the contract. Tests must prove behaviors, not count files/coverage.
6. Use Python pytest, .NET test tooling, Testcontainers, kind and the S05 runner as appropriate. Use fast/integration/full suites based on risk; execute the full relevant qualification suites before declaring release ready.
7. Verify S06-S10 authorization/isolation/data protection/AI prompt and tool abuse; S11-S14 identity, telemetry and forecast validity; S19-S26 hard constraints/approval/execution/rollback transitions; S28 failures and restart/backup-restore; S29 installation/migration/build provenance; S27 API/CLI contracts.
8. Include failure and adversarial fixtures: lost metrics, K8s watch staleness, auth scope bypass, malformed evidence, timeout-after-write, executor failover, damaged/revoked knowledge, expired secrets/certs, host pressure, image-signature denial **when P1 active**. Do not conduct destructive tests outside a strictly approved disposable scope.

## S31 — Scientific and AI evaluation

9. Reuse the approved **four B0/B1/B2/B3 baselines exactly as defined in S01/S31**; do not invent/relabel their meaning. Run **paired, serial, repeated, quality-qualified** scenarios with S05; freeze workload profiles, seeds, window definitions, configuration, hardware conditions and independent evidence where the protocol requires them.
10. Pre-register hypotheses, outcome measures, exclusion/contamination conditions and comparisons. Record actual windows, repeats, host pressure, telemetry/coverage quality and confidence/uncertainty; time-compressed runs are not historical 24h ground truth.
11. Evaluate forecasting separately (S14 rolling-origin backtest, eligible/refused cases, interval coverage), optimizer and fragmentation correctness, workload SLO/safety, realized outcomes vs hypothetical request reservation reductions, and limits of supported scheduler constraints.
12. Evaluate S18 retrieval with a labeled corpus (lexical/semantic/hybrid, relevance, access control, poisoning), S16/S17 grounded/cited advisory and security; measure B3's **incremental value versus B2** without allowing LLM self-judging or treating prose quality as quantified optimization benefit.
13. Publish immutable S31 evaluation manifest/report with dataset and evidence hashes, scenario/protocol/model revisions, exclusions, sample counts, effect sizes, uncertainty, statistical limitations, negative/null results and reproducing commands. Do not invent achieved gains, SLO pass rates or benchmark values.

## S32 — Six gates and truthful release decision

14. Read the **exact six ordered release gates** and verdict semantics from `Domain_H_Testing_Evaluation_Product_Readiness_S30-S32.md` section `S32.3`; do not substitute a guessed generic gate list. For every applicable gate, collect the specified evidence and decide `pass`, `fail`, `blocked`, `not_run`, or `not_applicable` according to the source's actual vocabulary.
15. Check local reproducible install and cleanup (S29 Python+Helm/Kustomize), identity/bootstrap, migrations, smoke/health, least privilege, operational observability (Prometheus/Grafana plus optional calibrated logging/tracing), independent encrypted backup/restore drill (S28), release image/trust posture and rollback/runbooks. Capture timestamps/commands and limitations.
16. Distinguish P0 direct Kubernetes Executor from GitOps/Flux **P1**; S10 Sigstore signature enforcement and S23 autoscaling P1 must not be silently represented as active P0 gates. A failed mandatory applicable gate prevents an overall qualified verdict.
17. Run a final documentation audit under `CLAUDE.md`: README onboarding, copyable setup/run/test/cleanup instructions, correct paths/links and Mermaid diagrams, accurate distinction of local simplifications and production requirements, adequate `/docs` coverage per existing rules, no progress diaries, no claims about unimplemented features.
18. Require the human release review prescribed by S32; do not self-approve a release or authorize any S24 optimized mutation. If tests/benchmarks are missing, produce a truthful provisional/not-qualified report rather than manufacturing proof.

## Evidence bundle and reporting

Prefer the S32 accepted logical `ReleaseQualificationPolicy`, `ReleaseReadinessReport`, and bundle structure from the actual Domain H specification. Include:

- Release/cluster/config/model/artifact digests and source revisions.
- Capability-by-capability status with exact evidence refs and applicable release gate verdicts.
- S30 test summary by layer, negative/security tests, recovery and operational findings, failures/skips.
- S31 B0-B3 protocol, empirical samples, contamination/eligibility, deterministic forecast and agent/RAG evaluations with uncertainty.
- S28 backup/restore evidence and S29 install/migrate/signing evidence only where actually run.
- Demonstrated versus simulated versus inferred versus not tested behavior; residual risks, disabled/P1 features and blocking operator actions.
- Reproduction commands and a *clear final disposition*; do not represent a `not_run` gate as a pass.

Use existing source-approved artifact paths and schema contracts where implemented. Do not add a separate always-on release service or rewrite domain owners' authoritative state. If tooling is unavailable, capture the blocker precisely and stop at the strongest supportable verdict.
