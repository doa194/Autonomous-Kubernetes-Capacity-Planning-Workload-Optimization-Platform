---
title: Autonomous Kubernetes Capacity Planning & Workload Optimization Platform — Master Specification Catalog
project: Autonomous Kubernetes Capacity Planning
artifact_type: project-source / master-roadmap
version: 1.0.0
status: architecture-planning
updated: 2026-11-13
slice_count: 32
domain_count: 8
source_of_truth: true
---

# Autonomous Kubernetes Capacity Planning & Workload Optimization Platform
## Master Architecture & Spec-Driven Development Catalog

**Version:** 1.0.0  
**Date:** 2026-11-13  
**Project phase:** High-level architecture / specifications; implementation details and many technology selections remain to be decided.  
**Use:** Add this Markdown document to the ChatGPT Project sources. Reference slice IDs (e.g., `S06`, `S20`) in separate chats. This file is the authoritative roadmap, not proof that every proposed technology or design decision is finalized.

> **Project north star:** Observe Kubernetes resource demand; forecast capacity risks with deterministic statistical/ML models; investigate historical and current context with AI; simulate and evaluate safe resource/placement changes; and apply only validated, explicitly authorized changes with post-change verification.

## 1. Agreed directions versus proposals

### Agreed project directions
- **Deployment model:** Fully local lab; multi-node Kubernetes using **kind** on a Windows 11 / Docker Desktop / WSL2 development environment, subject to local resource budget.
- **Demonstration workloads:** **Option B, mixed languages.** A small, realistic ecosystem rather than a complete business application: .NET 10 HTTP APIs and Python worker/batch/memory workloads. Workloads exist to create reproducible resource demand and reliability scenarios.
- **Core problem:** Capacity forecasting, CPU/memory right-sizing, scheduling feasibility, fragmentation, workload priority, risk-aware optimization and outcome evaluation.
- **Prediction policy:** LLMs must **not** invent capacity forecasts. Numerical forecasting belongs to statistical/ML or deterministic forecasting services.
- **Three AI roles:** Capacity Analyst, Optimization Planner, Risk Reviewer. Agent conclusions must be grounded in tools/evidence and independently checked.
- **Control philosophy:** Recommendation-first, deterministic safety gates, human-approved execution and post-change verification. No unrestricted LLM-issued Kubernetes mutations.
- **Engineering method:** Spec-driven, sliced architectural design with functional and nonfunctional requirements, contracts, security analysis, ADRs and testable acceptance criteria.

### Current architecture proposals — subject to slice-level decisions
- **AI:** Microsoft Agent Framework; Ollama local LLM inference; Semantic Kernel optional only where valuable.
- **Telemetry:** Prometheus, kube-state-metrics, kubelet/cAdvisor, Kubernetes API; Grafana for visualization.
- **Forecasting and control-plane services:** Python-first, pandas/statsmodels/scikit-learn as appropriate, FastAPI, Typer CLI.
- **Knowledge:** PostgreSQL + pgvector, PostgreSQL full-text search, hybrid retrieval, temporal/workload filters, optional reranking.
- **Human identity:** Keycloak (OIDC/OAuth 2.0), platform-level RBAC and resource/action permissions.
- **Cluster security:** Scoped ServiceAccounts/RBAC, NetworkPolicy enforced by compatible CNI, TLS, selected mTLS, secret-management design, Pod admission policies and artifact integrity controls.
- **Execution:** Kubernetes API adapter for directly-managed resources and GitOps adapter for Git-managed resources.
- **Delivery:** Python automation; Helm/Kustomize and local/self-hosted CI as decided later.

**Decision state vocabulary:** `Agreed direction` = user selected a principle; `Proposed` = recommended, not yet chosen; `Open` = requires explicit design decision; `Deferred` = intentionally after core platform.

## 2. Product and architectural boundaries

### In scope
1. Workload and node resource inventory (allocatable, requested, limited, observed, forecasted).
2. CPU/memory consumption, throttling, OOM signals, scheduling state, priorities and workload configuration history.
3. Forecasts with horizons, uncertainty/confidence, model provenance and data sufficiency checks.
4. Resource right-sizing, scheduling/placement feasibility and fragmentation analysis.
5. Deterministic candidate generation, constraints, scoring and simulation, with baselines.
6. Three specialized AI agents and grounded tools, operational RAG using incidents/runbooks/configuration/previous outcomes.
7. Risk review, policy enforcement, human approval, bounded execution, verification and audit.
8. Reproducible mixed-language workload scenarios and measurable experiment evaluation.
9. Security, identity, isolation, supply chain, observability, reliability and recovery across the platform.

### Out of initial scope
- Production cloud provisioning/autoscaling of actual cloud nodes, multi-cluster federation, GPU placement, writing a custom Kubernetes scheduler, unrestricted automated eviction, complex business application logic, full service mesh as a prerequisite, and a dedicated custom web frontend.
- Large-scale MLOps infrastructure (Kubeflow, feature stores, MLflow) unless a later design slice demonstrates a compelling need.

### Architectural invariants
- **Observed resource usage is not scheduling capacity.** Resource requests, allocatable resources and scheduler constraints must be modeled separately from actual consumption.
- **Hard constraints are never traded away for utilization gains.** Policies must reject unsafe candidates regardless of AI confidence.
- **AI agents do not hold mutation credentials.** Only a trusted, deterministic controller can apply an explicitly authorized plan.
- **Plan authorization is exact and time-bound.** Bind approval to plan version, allowed action, targets, policy version and current resource state; revalidate before applying.
- **Historical retrieval is evidence, not executable instruction.** Treat retrieved documents, logs and tool outputs as untrusted data.
- **Graceful degradation:** failure of Ollama/RAG cannot break metrics collection or core deterministic capacity analysis.
- **All-local does not mean unsecured.** Security controls should be demonstrable and tested.

## 3. Eight-domain, 32-slice catalog

### Domain A — Platform Foundation & Workload Ecosystem (S01–S05)

#### S01 — Product Scope, Requirements & Operational Model [P0]
**Purpose:** Establish product mission, supported operations, actors, autonomy modes, optimization objectives, success measures and explicit non-goals.
**Design topics:** target users; read-only vs approval/execution modes; per-workload vs cluster optimization; planning horizons; criticality classes; SLOs and resource headroom; safety principles; scale/resource constraints of local lab.
**Deliverables:** product vision/scope, functional and nonfunctional requirements, system context, glossary, acceptance criteria and decision log.
**Dependencies:** none.

#### S02 — System Architecture & Component Contracts [P0]
**Purpose:** Define logical services/modules, ownership, interfaces and trust boundaries without assuming one microservice per module.
**Design topics:** deployment units; synchronous APIs vs queues/jobs; persistent workflow state; tenancy/namespace scope; data contracts; service boundaries; configuration/versioning; deployment topology.
**Deliverables:** C4-style context/container/component diagrams, API/event ownership, dependency graph, failure semantics and ADRs.
**Dependencies:** S01; security and data architecture inputs from S06–S09, S11–S13.

#### S03 — Local Kubernetes Infrastructure & Cluster Topology [P0]
**Purpose:** Build reproducible local cluster topology and define realistic resource constraints.
**Design topics:** kind worker/control-plane nodes; Windows/WSL2 resource budget; CNI with NetworkPolicy enforcement; node labels/taints; namespaces; ingress; local storage; resource quotas; system-component reservations; bootstrap and cleanup.
**Deliverables:** topology specification, namespace map, resource budget, cluster provisioning contract, isolation verification plan.
**Dependencies:** S01–S02; S07–S08.

#### S04 — Mixed-Language Demonstration Workloads [P0]
**Purpose:** Define measurable, reproducible application behaviors and Kubernetes resource profiles, not business functionality.
**Candidate catalog:** (1) .NET 10 latency-sensitive API, Deployment/Service; (2) .NET 10 bursty API, Deployment/HPA; (3) Python memory-profile service, Deployment; (4) Python CPU-processing worker, Deployment; (5) Python analytics processor, CronJob; (6) Python low-priority batch processor, Job.
**Design topics:** workload profile schema; CPU/memory patterns; health probes; app-level metrics; requests/limits; PriorityClasses; graceful termination; security context; HPA targets; traffic controls.
**Deliverables:** workload specifications, image/API contracts, resource baseline matrix, profiles and acceptance tests.
**Dependencies:** S03, S07–S08, S12; linked to S05.

#### S05 — Workload Simulation & Scenario Orchestration [P0]
**Purpose:** Generate deterministic, repeatable load and controlled failures for experiments.
**Design topics:** Python scenario runner; sustained/bursty/periodic/increasing workloads; traffic rates; seed/config capture; bounded CPU/memory; time-window control; fault injection; cleanup; synthetic versus live evidence labeling.
**Deliverables:** scenario YAML schema, runner interface, baseline scenario catalog, cleanup/safety limits, experiment reproducibility criteria.
**Dependencies:** S03–S04; S12; S30–S31.

### Domain B — Identity, Security & Trust (S06–S10)

#### S06 — Identity, Authentication & Authorization [P0]
**Purpose:** Protect human/operator-facing APIs and privileged workflows.
**Design topics:** local OIDC provider (Keycloak proposed); user and machine principals; token validation; CLI login; platform RBAC plus action/resource scope; Viewer/Analyst/Planner/Approver/Executor/Administrator roles; separation of duties; approval binding; token/role lifecycle.
**Deliverables:** identity architecture, role/permission matrix, authorization decision points, API auth contracts, negative security tests.
**Dependencies:** S01–S02; required by S24–S27.

#### S07 — Workload Identity, Service Security & Network Isolation [P0]
**Purpose:** Enforce service and namespace trust boundaries inside the local cluster.
**Design topics:** dedicated ServiceAccounts; short-lived projected credentials; read-only collector vs bounded executor RBAC; service-to-service authentication; TLS and selective mTLS; default-deny NetworkPolicy; compatible CNI; namespace isolation; restricted Kubernetes API and database access.
**Deliverables:** trust boundary diagram, SA/RBAC inventory, allowed network flow matrix, certificate plan, NetworkPolicy test cases.
**Dependencies:** S02–S03; informs S08–S09, S25.

#### S08 — Secrets, Data Protection & Security Policy Enforcement [P0]
**Purpose:** Protect credentials/data and enforce secure Kubernetes workloads.
**Design topics:** secrets backend choice; secret creation/rotation; database and OIDC credentials; no secrets in agent prompts/logs; TLS keys; storage protection and retention; Pod Security Admission/admission policies; audit policy; backups; minimal pod privileges.
**Deliverables:** secret lifecycle, data classification, encryption/retention requirements, Pod/admission policy catalog, threat controls.
**Dependencies:** S03, S06–S07, S13, S18.

#### S09 — AI Agent, LLM, RAG & Tool Security [P0]
**Purpose:** Prevent AI context, retrieval or tool use from bypassing platform authorization and safety.
**Design topics:** prompt-injection threat model; untrusted retrieved documents; structured schema validation; tool allowlists; no agent mutation credentials; bounded loops/token budgets; provenance; poisoned knowledge; sensitive data redaction; model endpoint protection; tool-result trust levels; adversarial evaluations.
**Deliverables:** AI threat model, agent capability matrix, tool contracts, security test corpus, instruction/data separation controls.
**Dependencies:** S06–S08; S15–S18; S24–S25.

#### S10 — Software Supply Chain & Runtime Security [P1]
**Purpose:** Secure built workloads, platform services and deployment artifacts.
**Design topics:** lockfiles and dependency scanning; SBOMs; image vulnerability scanning; signatures/provenance; registry trust; admission verification; non-root/rootless settings; capability removal; security patch/update processes; CI gates.
**Deliverables:** secure build standards, artifact policy, scanning/signing verification, runtime-hardening tests.
**Dependencies:** S03–S04, S08, S29.

### Domain C — Cluster Intelligence & Forecasting (S11–S14)

#### S11 — Cluster State Discovery & Resource Inventory [P0]
**Purpose:** Maintain accurate current resource state and configuration metadata.
**Design topics:** nodes/allocatable; requests/limits; Pods/Deployments/Jobs; node labels, taints, affinities; PriorityClasses; scheduling events; replica ownership; desired/current state; resource version snapshots; identity mapping over restarts/redeploys.
**Deliverables:** inventory domain model, Kubernetes watch/list approach, refresh/staleness strategy, consistency tests.
**Dependencies:** S03, S07, S02.

#### S12 — Metrics Collection & Historical Telemetry [P0]
**Purpose:** Gather trustworthy time-series signals for forecasting and optimization.
**Design topics:** Prometheus retention/scrapes; kube-state-metrics; kubelet/cAdvisor; CPU rates and CFS throttling; memory working sets/peaks; OOM/eviction and Pending signals; app latency/throughput; granularity; missing samples; label cardinality and reliability.
**Deliverables:** metric catalog, PromQL contract library, sampling/retention plan, scrape topology, data quality checks.
**Dependencies:** S03–S04, S07, S11.

#### S13 — Capacity Data Model & Intelligence Storage [P0]
**Purpose:** Relate time-series, workload configuration and analysis artifacts without duplicating all Prometheus samples.
**Design topics:** namespace/workload/container identity; immutable observation windows; snapshots; forecast records; evidence identifiers; plans and outcomes; PostgreSQL ownership; temporal consistency; retention; query provenance; schema migrations.
**Deliverables:** conceptual/logical schemas, persistence ownership, API DTOs, retention strategy, integrity tests.
**Dependencies:** S11–S12, S08; consumed by S14–S26.

#### S14 — Capacity Forecasting & Demand Modeling [P0]
**Purpose:** Produce numerical forecasts outside the LLM.
**Design topics:** workload CPU/memory demand; node and cluster headroom; requested vs observed vs predicted resources; horizons; baseline seasonal/statistical models; optional scikit-learn; trend/seasonality/change points; uncertainty; low-data refusal; backtesting; MAE/RMSE/interval coverage; model lineage.
**Deliverables:** forecasting contract, model evaluation plan, confidence policies, forecast persistence and service interface.
**Dependencies:** S11–S13, S31; inputs to S19–S23 and S16.

### Domain D — Agentic AIOps & Operational Knowledge (S15–S18)

#### S15 — Local LLM Inference & AI Runtime [P0]
**Purpose:** Operate local AI models with bounded and observable inference.
**Design topics:** Ollama model/runtime choice; CPU/GPU budget; model versions; structured outputs; inference latency/concurrency; context length and budgets; timeouts; authentication/network access; fallback behavior; tracing and reproducibility.
**Deliverables:** model contract, deployment topology, performance budget, fail-safe policies, inference observability.
**Dependencies:** S02–S03, S07–S09; supports S16–S18.

#### S16 — Multi-Agent Architecture & Orchestration [P0]
**Purpose:** Implement specialized AI analysis within a deterministic, durable workflow.
**Agents:** Capacity Analyst (diagnosis/evidence), Optimization Planner (proposals from safe tools), Risk Reviewer (critique and risk factors). No direct Kubernetes mutation credentials for agents.
**Design topics:** Microsoft Agent Framework; workflow handoffs; persisted state; bounded retries; structured outputs; responsibilities; confidence and escalation; agent nonavailability.
**Deliverables:** agent responsibility matrix, workflow state diagram, handoff schemas, failure semantics and tests.
**Dependencies:** S14–S15, S17–S18, S09, S19–S22.

#### S17 — Agent Tools & Context Engineering [P0]
**Purpose:** Provide typed and authorized evidence access rather than open-ended shell/Kubernetes access.
**Design topics:** tool catalog for cluster/metrics/forecasts/retrieval/simulator/policies; tool auth; schemas; pagination and data budgets; context assembly; citation/provenance; stale data labeling; error semantics; rate limiting.
**Deliverables:** versioned tool schemas, permission maps, context policies, contract and security tests.
**Dependencies:** S06–S09, S11–S16, S18–S22.

#### S18 — Operational Knowledge Base & RAG Architecture [P0]
**Purpose:** Retrieve trustworthy historical operational evidence.
**Collections:** incidents; resource/configuration history; runbooks/policies; past optimization decisions/outcomes.
**Design topics:** PostgreSQL/pgvector and keyword search; ingestion; chunking; metadata/workload keys; namespace/time filters; hybrid fusion; optional reranking; temporal validity; document integrity; provenance; authorization-aware retrieval; RAG evaluation.
**Deliverables:** document/metadata schemas, ingestion/versioning pipeline, retrieval APIs, relevance tests and security cases.
**Dependencies:** S08–S09, S13, S15, S17, S31.

### Domain E — Optimization, Simulation & Decisions (S19–S23)

#### S19 — Optimization Objectives & Workload Policies [P0]
**Purpose:** Formalize feasible/acceptable improvements before candidate ranking.
**Design topics:** resource waste, risk and change cost; workload criticality; SLO/latency protections; minimum request floors; headroom; disruption rules; priority; policy as hard constraints vs soft objectives; risk tolerance and approval thresholds.
**Deliverables:** objective specification, policy schema, rejection reasons, calibration scenarios, ADRs.
**Dependencies:** S01, S04, S11–S14, S06–S09.

#### S20 — Resource Right-Sizing Engine [P0]
**Purpose:** Produce reproducible CPU and memory request/limit proposals.
**Design topics:** percentiles vs peaks vs forecast intervals; CPU throttling and latency; memory spikes/OOM; request vs limit semantics; headroom; historical incidents; candidate bounds; VPA recommendation-only baseline; minimum evidence rules.
**Deliverables:** algorithm specs, candidate schema, VPA comparison, expected-benefit evidence, regression tests.
**Dependencies:** S11–S14, S18–S19, S22.

#### S21 — Placement, Scheduling & Fragmentation Analysis [P0]
**Purpose:** Detect aggregate capacity versus per-node scheduling failures and evaluate placement alternatives.
**Design topics:** node-fit by requests, allocatable and existing reservations; taints/tolerations; node/pod affinity; topology spread; priorities; preemption/disruption; multiple resources; fragmentation scores; simulator limits vs scheduler fidelity.
**Deliverables:** feasibility engine contract, fragmentation measurements, placement constraints, Kubernetes-backed validation scenarios.
**Dependencies:** S03–S05, S11–S14, S19, S22.

#### S22 — Optimization Simulation & Scenario Evaluation [P0]
**Purpose:** Evaluate hypothetical changes without applying them.
**Design topics:** deterministic analytical simulation vs live Kubernetes-backed experiments; candidate revisions; projected demand; ranking; uncertainty; tradeoffs; scenario reproducibility; false-feasibility detection; evidence quality.
**Deliverables:** simulation contract, assumptions/limitations, evaluation results schema, baseline comparisons, tests.
**Dependencies:** S14, S19–S21, S05, S31.

#### S23 — Autoscaling & Advanced Workload Optimization [P1]
**Purpose:** Explore scaling and batch strategies beyond first-wave right-sizing/placement.
**Design topics:** HPA behavior/oscillation; VPA compatibility; replica ranges; peak load; batch timing; high vs low priority contention; PDB/disruption; quota and overcommit; scheduling/eviction risks.
**Deliverables:** advanced policy catalog, experiment suites, scaling recommendation schema and safety checks.
**Dependencies:** S04–S05, S14, S19–S22, S24–S26.

### Domain F — Decision Governance & Execution (S24–S26)

#### S24 — Recommendation Lifecycle & Risk Governance [P0]
**Purpose:** Govern plans from draft to approved or rejected with evidence and auditability.
**Design topics:** recommendation ID/version; evidence package; feasibility outcomes; policy/risk gates; roles/separation of duties; approval expiry; target/resource version; signing/integrity; proposed/approved/expired/rejected states.
**Deliverables:** plan lifecycle state machine, plan and approval schemas, auth matrix, risk taxonomy, audit requirements.
**Dependencies:** S06–S09, S13–S22.

#### S25 — Controlled Execution & GitOps Integration [P0]
**Purpose:** Apply only the exact authorized and revalidated operation.
**Design topics:** Kubernetes read/mutate account separation; resource scope; admission and dry-run checks; optimistic concurrency; idempotency; execution locks; GitOps ownership detection; direct Kubernetes adapter vs Git config change adapter; bounded rollout changes; crash recovery.
**Deliverables:** controller state machine, execution adapter contracts, authorization checks, concurrency/rollback tests.
**Dependencies:** S06–S10, S19–S24, S28–S29.

#### S26 — Post-Change Verification, Rollback & Outcome Learning [P0]
**Purpose:** Measure real effects and safely handle unsuccessful changes.
**Design topics:** before/after metrics; acceptance windows; SLO guardrails; rollback versus partial recovery; false gains; drift detection; plan outcome records; learning from failed changes; durable audit trail.
**Deliverables:** verification contract, recovery playbooks, outcome schema, evaluation and operational tests.
**Dependencies:** S12–S14, S20–S25, S28, S31.

### Domain G — Product Operations & Delivery (S27–S29)

#### S27 — Platform APIs, CLI & Operator Experience [P0]
**Purpose:** Offer secure operator workflows without requiring a custom UI.
**Design topics:** FastAPI APIs; Typer CLI; OIDC login and token management; inspect/forecast/plan/approve/execute/verify; pagination; API versioning; errors; Grafana integration; role-specific outputs.
**Deliverables:** OpenAPI specs, CLI command contract, auth flows, usability and negative-path tests.
**Dependencies:** S02, S06, S11–S26.

#### S28 — Platform Observability, Reliability & Failure Recovery [P0]
**Purpose:** Make the platform itself diagnosable and resilient.
**Design topics:** structured logs; traces; metrics; correlation IDs; health/readiness; alerts; retention; loss of Prometheus/Ollama/PostgreSQL/Kubernetes API; durable workflow recovery; backups/restore; graceful degradation; SLOs.
**Deliverables:** platform SLOs, telemetry standards, alert/runbook catalog, fault matrix, restore drills.
**Dependencies:** all services; especially S11–S13, S15–S18, S24–S26.

#### S29 — Infrastructure Automation, GitOps & Secure CI/CD [P0]
**Purpose:** Build and operate a reproducible local system with security gates.
**Design topics:** Python bootstrap and cleanup; pinned versions; manifests/Helm/Kustomize; environments; secrets references; container builds; test stages; SAST/SCA/images; signed artifacts where selected; controlled GitOps; promotion/rollback.
**Deliverables:** automation commands, deployment pipeline, artifact policy, release checklist, reproducibility checks.
**Dependencies:** S02–S10, S27–S28, S30.

### Domain H — Testing, Evaluation & Product Readiness (S30–S32)

#### S30 — Testing Strategy & Security Verification [P0]
**Purpose:** Prove functional correctness, authorization and failure safety.
**Design topics:** unit/integration/component/E2E/operational layers; Testcontainers; ephemeral kind tests; negative access tests; plan tampering; privilege escalation; prompt injection; retrieved instruction poisoning; stale resource versions; controller restart and idempotency.
**Deliverables:** test pyramid, test matrices, fixtures, threat test suite, traceability to requirements.
**Dependencies:** all core slices.

#### S31 — Capacity Experiments & AI/ML Evaluation [P0]
**Purpose:** Quantify the value of forecasting, optimization, RAG and agent reasoning.
**Baselines:** (1) static requests/configuration; (2) rule-based and VPA; (3) deterministic forecast-aware optimization without agents; (4) full AI-enhanced platform.
**Metrics:** forecast MAE/RMSE/interval coverage, scheduling feasibility, Pending pods, CPU throttling, OOM, p95 latency, requested-resource reduction, candidate rejection rate, realized benefit, RAG retrieval relevance, groundedness, safety.
**Deliverables:** scenario benchmark suite, evaluation protocol, evidence/report schema, experiment reproducibility requirements.
**Dependencies:** S04–S05, S12–S26, S30.

#### S32 — Operational Readiness, Documentation & Release Governance [P0]
**Purpose:** Establish maintainable, auditable readiness for local operation and handoff.
**Design topics:** operator documentation; ADR index; compatibility/versioning; threat model review; runbooks; installation upgrades; data backups; recovery; known limitations; SLO achievement; acceptance evidence; release checklist.
**Deliverables:** release readiness report, runbooks, architecture documentation, install/upgrade/restore guides, final acceptance gate.
**Dependencies:** all slices; S28–S31.

## 4. Cross-cutting architecture and security requirements

Apply to **every** slice where relevant; reference these IDs in acceptance criteria.

| ID | Cross-cutting requirement |
|---|---|
| X-SEC-01 | Authenticate human and machine principals before protected operations. |
| X-SEC-02 | Authorize sensitive actions independently of authentication, by role, action, scope and resource. |
| X-SEC-03 | Never give AI agents direct privileged Kubernetes mutation credentials. |
| X-SEC-04 | Keep credentials, tokens and other secrets out of source control, logs, retrieval, model prompts and generated proposals. |
| X-SEC-05 | Enforce network and Kubernetes RBAC least privilege; test negative access paths. |
| X-SEC-06 | Revalidate plan identity, version, policy and target state immediately before execution. |
| X-SEC-07 | Treat RAG documents, telemetry text, tool results and model outputs as untrusted data; they cannot authorize mutations. |
| X-SEC-08 | Produce attributable, tamper-resistant records for plan, approval, execution and outcomes. |
| X-REL-01 | Dependence failures must fail safe, with AI unavailability not disabling deterministic monitoring. |
| X-REL-02 | Define timeouts, bounded retries, idempotency and recovery from restarts. |
| X-DATA-01 | Label observed, historical, forecasted and simulated values distinctly. |
| X-DATA-02 | Every critical recommendation must reference evidence origin, time window and quality/staleness. |
| X-OPT-01 | Never sacrifice hard SLO/safety constraints for efficiency gains. |
| X-OPT-02 | Document approximation limits in scheduling/forecast simulation and validate against live behavior. |
| X-OPS-01 | Critical actions and services must emit correlated metrics, logs and traces. |
| X-DEL-01 | Reproducibility: pin versions, seed synthetic scenarios, and preserve executable configuration. |
| X-TST-01 | Every feature has mapped functional, security and failure-path acceptance tests. |

## 5. Example user journeys and acceptance scenarios

**Journey A — Safe CPU right-sizing:** CPU request is high relative to sustained demand; forecast validates future peak; planner proposes revision; simulation checks scheduling; Risk Reviewer cites evidence; operator approves exact version; controller applies, verifies latency/throttling and records outcome.

**Journey B — Historical memory-spike protection:** Analytics workload rarely uses more than 1.5 GiB but previously had an OOM during periodic batch activity; RAG retrieves incident and current profile; system evaluates peak-aware memory request/limit changes and refuses reductions lacking adequate safety margin.

**Journey C — Scheduling fragmentation:** Aggregate free requested CPU is sufficient but no individual node can fit a Pod; feasibility analysis identifies the real blocker; proposes bounded placement/right-sizing options while respecting affinities, taints, topology and PriorityClasses.

**Journey D — Approval tampering:** After approving a specific plan revision, an attacker modifies target namespace/request values; controller rejects execution because plan digest/revision or target preconditions do not match.

**Journey E — Prompt-injection attempt:** Malicious runbook content asks the agent to bypass policy and scale to zero; retrieved text remains evidence only, tool access is restricted, and no unauthorized action occurs.

**Journey F — Dependency outage:** Ollama stops during an analysis; forecast and deterministic detection remain available; agent-dependent proposal cannot be finalized; no mutation occurs without correct approval.

**Journey G — GitOps-owned workload:** A proposal targets a GitOps-managed Deployment; direct API mutation is prohibited by ownership policy; controller creates or requests an approved declarative change via GitOps adapter.

## 6. Spec-driven development protocol

Each slice should eventually contain these artifacts (do not pre-implement all at once):

```
specs/slices/SNN-short-name/
  01-purpose-and-scope.md
  02-requirements.md
  03-architecture-and-alternatives.md
  04-contracts-and-data.md
  05-security-and-threat-model.md
  06-failure-modes-and-operations.md
  07-acceptance-tests.md
  08-decisions-and-open-questions.md
```

Every slice specification should answer:
1. Purpose, scope, exclusions and stakeholders.
2. Functional requirements and measurable nonfunctional requirements.
3. Architectural alternatives, tradeoffs, ADRs and final selections.
4. Component responsibilities, data ownership, typed APIs/events/tools.
5. Identity, authorization, trust boundaries, secrets and threat scenarios.
6. Failure behavior, resilience, idempotency, recovery and observability.
7. Dependency assumptions, implementation phases and local resource cost.
8. Acceptance tests with requirement IDs, including negative security cases.

**Recommended requirement IDs:** `S06-FR-001`, `S06-NFR-001`, `S06-SEC-001`, `S06-TEST-001`. Keep persistent IDs for traceability.  
**Decision format:** `ADR-XXXX`, status Proposed/Accepted/Superseded, context/options/decision/consequences.  
**Quality gate:** A slice is ready for coding only once open blocking choices are resolved, contracts are defined and acceptance criteria are testable.  
**Agent guidance:** `AGENTS.md` can point AI coding agents at these specs, but it is not a security enforcement layer; actual checks live in tests, CI, API auth and Kubernetes policies.

## 7. Implementation dependency ordering and milestones

### Dependency logic
- S01–S02 define goals, service boundaries and contracts.
- S03–S05 create reproducible Kubernetes/workload behavior.
- S06–S09 define trust boundaries **before** exposing privileged interfaces; S10 hardens software delivery.
- S11–S13 establish trustworthy state, telemetry and durable intelligence records.
- S14 predicts demand; S19–S22 produce deterministic optimization and simulation.
- S15–S18 add local LLM runtime, agents, typed tools and operational history.
- S24–S26 control approval, mutation, verification and outcome capture.
- S27–S29 provide operator access, operations and delivery.
- S30–S32 measure quality, security and readiness throughout development.

### Proposed milestones
1. **Secure Kubernetes Foundation** — cluster, workload ecosystem, identity/RBAC/network baseline, bootstrap.
2. **Capacity Intelligence** — inventory, metrics, data model, dashboards.
3. **Predictive Optimization** — models, right-sizing, placement, deterministic simulation.
4. **Agentic Investigation & RAG** — local inference, knowledge, three agents, constrained tools.
5. **Governed Execution** — approval, policy revalidation, mutation adapters, verification and recovery.
6. **Hardening & Comparative Evaluation** — security/reliability testing, benchmarks, release docs.

S30 and S31 start early and evolve alongside the implementation; do not postpone testing until Milestone 6.

## 8. Initial decision register

| ID | Topic | Current direction | State | Primary slice |
|---|---|---|---|---|
| DEC-001 | Local infrastructure | kind on local machine | Agreed direction | S03 |
| DEC-002 | Demonstration approach | Option B, realistic small ecosystem | Agreed direction | S04 |
| DEC-003 | Demo languages | .NET 10 HTTP APIs + Python workers/jobs | Agreed direction | S04 |
| DEC-004 | Agents | Three: Analyst, Planner, Reviewer | Agreed direction | S16 |
| DEC-005 | Numerical forecasts | Deterministic statistical/ML engine, not LLM | Agreed direction | S14 |
| DEC-006 | Execution stance | Human-approved, validated and bounded | Agreed direction | S24–S25 |
| DEC-007 | Agent framework | Microsoft Agent Framework | Proposed | S16 |
| DEC-008 | Local inference | Ollama | Proposed | S15 |
| DEC-009 | Human identity | Keycloak OIDC | Proposed | S06 |
| DEC-010 | Telemetry | Prometheus + Kubernetes state | Proposed | S11–S12 |
| DEC-011 | Operational data / RAG | PostgreSQL + pgvector hybrid | Proposed | S13/S18 |
| DEC-012 | Operator interface | FastAPI + Typer CLI + Grafana | Proposed | S27 |
| DEC-013 | Change management | Direct Kubernetes and GitOps adapters | Proposed | S25 |
| DEC-014 | Network policy/CNI | Compatible enforcement implementation | Open | S03/S07 |
| DEC-015 | Secrets management | Select locally supportable approach | Open | S08 |
| DEC-016 | Forecast model family/horizons | Decide with data/experiments | Open | S14 |
| DEC-017 | Optimizer scoring/solver | Hard constraints + ranked candidates | Open | S19–S22 |
| DEC-018 | Platform deployable services | Set boundaries in S02 | Open | S02 |
| DEC-019 | CI/GitOps toolchain | Choose local fit | Open | S29 |
| DEC-020 | Autoscaling/batch optimization | Advanced feature set | Deferred/P1 | S23 |

## 9. Chat-to-chat usage instructions

When starting a fresh ChatGPT chat under this Project, refer to **this file** and the relevant slice ID. Suggested prompt:

> “Use the project source `Autonomous_Kubernetes_Capacity_Planning_Master_Spec_Catalog.md` as the authoritative roadmap. Let's design **S06 — Identity, Authentication & Authorization**. First list alternatives and tradeoffs, then recommend a production-inspired local architecture. Do not finalize open choices until we discuss them. Preserve agreed decisions from other slices.”

After finishing a slice, record approved decisions in the slice's spec and update the master decision register; don't silently treat recommendations as accepted. If two chats disagree, prefer the most recent explicitly accepted ADR and flag conflicts for reconciliation.

**Next recommended discussion:** `S01 — Product Scope, Requirements & Operational Model`.
