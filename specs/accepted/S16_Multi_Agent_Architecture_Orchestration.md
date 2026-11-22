---
title: "S16 — Multi-Agent Architecture & Orchestration"
project: "Autonomous Kubernetes Capacity Planning"
artifact_type: "accepted-slice-specification"
slice_id: S16
domain: "D — Agentic AIOps & Operational Knowledge"
priority: P0
version: "1.0.0"
status: "accepted-architecture / not-yet-implemented"
approved_on: 2026-11-19
source_of_truth_scope: "Microsoft Agent Framework workflow, three logical roles, trusted delegation, stage/handoff contracts, durable checkpoints, advisory outcomes, bounded retries and recovery, test suite, S16 ADRs"
prerequisites: "Master; accepted S01–S15 and S14 companion; S17/S18 integrated contracts"
---

# S16 — Multi-Agent Architecture & Orchestration

> **Approval record — 2026-11-19.** The project owner accepted **S16-01–S16-06**: Microsoft Agent Framework; fixed sequential Capacity Analyst → Optimization Planner → Risk Reviewer; PostgreSQL-authoritative durable stage state and validated checkpoints; typed evidence-linked handoffs; bounded retries and at most one controlled re-evaluation; and two capability modes (`diagnostic` and `optimization_review`). Approval is architectural only; no framework runtime, workflow, model output, checkpoint recovery or integration test is installed/verified. The precise framework release/API and timing values remain implementation gates.

## 0. Authoritative inherited boundaries

| Accepted input | Consequence |
|---|---|
| S01 | Advisory product, L0–L2 only; Observe/Advisory/Governed and exact human approval before governed action; Current/1h/24h; Critical/Standard/Flexible/Opportunistic; B0–B3 experiments |
| S02 | Exactly six custom Deployments; `ai` runs all three logical agents and workflow code; `governance` owns plan/approval lifecycle; PostgreSQL durable workflow/outbox, no broker requirement |
| S03–S05 | One host-kind lab, W01–W06; experiment orchestration is host Python with separate authority; no agent experiment launch, faults, or Pod termination; S05 study provenance is evidence |
| S06/S07 | Keycloak scoped users; trusted machine/delegation and service auth; roles are logical inside the `ai` machine principal, not separate SA credentials |
| S08/S09 | No secrets/Restricted data in prompts or advisory; fixed three-role broker; read-only typed tool permissions; no shell/SQL/Kubernetes mutation; hostile source text is data |
| S10–S13 | Versioned evidence, immutable refs, source-classification/quality and PostgreSQL schema-owned writes; agent output cannot overwrite S11/S12/S13 evidence |
| S14 + companion | Only deterministic forecasts, numerical uncertainty and refusals; `current` assessment distinct from `1h`/conditional `24h`; batch future demand only under declared S05 scenario assumptions |
| S15 | Host Ollama via gateway; bounded schema output and resource admission; no remote fallback |
| S17/S18 | Trusted tool broker/context/citations and historical knowledge retrieval; historical instructions do not become workflow instructions |
| S19–S22 | Future deterministic candidate/simulation/constraints APIs; **not already implemented**; optimization mode requires these capabilities to be qualified |
| S24/S25/S26 | Governance receives review-ready advisory, independently decides; human approval and Executor mutation/verified outcome never produced by an agent |

## 1. Mission, requirements and non-goals

**Mission:** coordinate three specialized local AI advisors through a deterministic, recoverable, authorized workflow that transforms preexisting quality-qualified evidence into a cited diagnostic or optional optimization review, without assigning a model any authority over safety, policy, approval or Kubernetes changes.

- `S16-FR-001` Durable advisory submission has an authenticated origin, exact scope and mode, validated resource target, stable ID and immutable analysis evidence reference.
- `S16-FR-002` Coordinator assigns fixed role identities in order; model cannot register a role, choose a tool class or transfer control to an arbitrary agent.
- `S16-FR-003` Analyst diagnoses demand/quality; Planner interprets deterministic candidate and simulation outputs; Reviewer critiques with deterministic constraints and operational history.
- `S16-FR-004` Stage outputs are schema-validated, authorization-scoped, citation-checked, versioned and immutable before handoff.
- `S16-FR-005` Workflow state, leases, ownership, heartbeats, stage checksums and terminal results survive Pod restart without falsely claiming exactly-once model calls.
- `S16-FR-006` Failures, insufficiency, evidence quality, cancellation and unsupported optimizer capabilities produce explicit non-success states.
- `S16-FR-007` At most one controlled re-evaluation under a defined issue type and total budget; policy safety rejection is not subject to model reconsideration.
- `S16-FR-008` Workflow modes clearly separate diagnostic reasoning from full optimization-review, with a hard dependency on S19–S22 for the latter.
- `S16-FR-009` Agent reviews are not equivalent to deterministic gate success, plan creation, human approval, execution request, application or verified benefit.
- `S16-FR-010` Scope and delegation are revalidated on resume and prior to sensitive tool access, not stored as reusable raw OIDC tokens.

**Non-goals:** unconstrained agent swarm, agent-to-agent free-form debating, LLM numerical optimization, self-modifying prompts/tools, model-driven reconfiguration, direct Kubernetes API, experiment control, hidden code interpreter, per-agent Deployment, separate vector database, a model-controlled approval field, mandatory custom UI or speculative outcome success.

## 2. Deployment and composition

```mermaid
flowchart TB
  H["Human or authorized service purpose"] --> API["Operator API"]
  API --> GOV["Governance current authorization"]
  GOV -->|"Durable authorized advisory job"| WF[("PostgreSQL workflow / leases / outbox")]
  WF --> CO["ai Deployment / trusted coordinator"]
  subgraph AG["Microsoft Agent Framework, single ai process"]
    CO --> AN["Capacity Analyst"]
    AN --> PL["Optimization Planner"]
    PL --> RR["Risk Reviewer"]
    AN --> TB["S17 broker"]
    PL --> TB
    RR --> TB
  end
  TB --> DET["S11-S14 + future S19-S22 read/deterministic APIs"]
  TB --> RAG["S18 authorized retrieval"]
  AG --> MODEL["S15 protected host Ollama"]
  RR --> VAL["Trusted advisory validator"]
  VAL --> KA[("knowledge / advisory artifacts")]
  VAL --> GOV
  GOV -. "Human-approved exact plan only" .-> EX["S25 Executor"]
```

**Authority:** Governance authorizes creation and consumption of advisory work; AI owns validated advisory artifacts, not Governance policy or state. S13 `workflow` lease/job storage must be accessed with narrowly restricted workflow-owner permissions; `knowledge` is AI authoritative write schema, with consumer reads via scoped APIs. A checkpoint alone never approves a plan. No mutable cross-schema writes. Keep event handoffs via existing PostgreSQL-backed jobs/outbox with at-least-once delivery and dedupe. REST handles immediate authenticated reads/status, not open tool/endpoints.

## 3. Agents, responsibilities, tools and outputs

| Logical agent | Input | Allowed operation | Required deliverable | Prohibited |
|---|---|---|---|---|
| `CapacityAnalyst` | Scope-bound S11 inventory, S12 qualified observations, S14 results, relevant S18 incidents | Read only via S17 broker; diagnose resources/SLO/coverage and uncertainty | `CapacityAssessment` with claims, evidenceRefs, risks, missingness | Invent measurements/forecasts, mutate K8s, generate plans, run S05 |
| `OptimizationPlanner` | Validated Analyst output, eligible deterministic S19–S22 results/candidate capability | Request bounded deterministic candidate creation/evaluation; inspect policies and evidence | `OptimizationAdvisory` referencing exact candidate/simulation refs, alternatives and reasons | Set arbitrary resource values, bypass hard gates, claim candidate eligible without backend |
| `RiskReviewer` | Both preceding validated outputs, policy findings, historical S18 evidence and actual forecast quality | Read-only risk/constraint/simulation review, contradiction checks | `RiskReview` including unresolved risks, counter-evidence and advisory disposition | Human approval, execution authorization, safety-gate override or hidden candidate creation |

All roles run as logical identities under one `capacity-ai-sa` process with immutable code-bound capability mapping. The LLM cannot set `logicalRole`, `actor`, `namespaceScope`, tool registration or workflow priority. A full `ai` Pod compromise still cannot bypass backend/DB/cluster permissions; in-process broker is not a sandbox for compromised process code.

### 3.1 Domain-oriented examples

- **W01 Critical `.NET` latency-api:** low mean CPU but throttling and increased p95 under a qualified scenario: Analyst reports contradictory efficiency/SLO evidence; Planner cannot treat low mean CPU as safe request target; Reviewer cites latency and policy guardrail.
- **W02 Standard burst-api:** HPA off by default. If opt-in HPA experiment, quality and changing replicas must be explicit; refuse unqualified static-denominator forecasts.
- **W03 Standard Python memory-profile:** sampled memory peaks are not true maxima; retained spikes and OOM censoring matter; missing high-load time must block unsafe downsize-grade claims.
- **W04 Flexible CPU worker:** low CPU plus accumulating queue is not proof of spare resource; Reviewer checks throughput/completion/queue SLO.
- **W05 suspended CronJob / W06 on-demand Job:** conditional execution envelopes only with declared future run/profile/window. No fabricated periodic demand and no assumption that Job absence means zero consumption.

## 4. Modes and eligibility

| Mode | Stages | Preconditions | Terminal success semantics |
|---|---|---|---|
| `diagnostic` | Evidence → Analyst → typed validated diagnostic, with optional deterministic critique stage that cannot produce a candidate | Authorized scope, sufficient trustworthy S11–S14 evidence; RAG may be missing with explicit quality | `diagnostic_completed`, not eligible right-sizing plan |
| `optimization_review` | Analyst → Planner → Reviewer → validated final advisory | Same plus qualified S19–S22 candidate generation, policy, fit/simulation and risk interfaces | `review_ready` only if evidence/policy checks pass; governance decides independently |

**Three-role workflow selection:** S16-02 accepts a fixed three-role chain for *full optimization review*. Diagnostic mode may terminate after Analyst because its purpose does not include a candidate; this is not a second dynamic agent graph. If a comprehensive diagnostic wants a Reviewer opinion, treat it as a fixed explicit variant with no Planner-generated candidate, and do not brand the result `optimization_review`. S16 implementation should initially prefer Analyst-only diagnostic to simplify feature readiness.

Readiness must be independently checked against registered capabilities and versions. `capability_unavailable` from an unimplemented S19–S22 service is not a simulated success. In `Observe` mode, authorized diagnosis may run but no plan is offered for execution; `Advisory` allows proposals without mutation; `Governed` still requires S24/S25 hard checks/human approval.

## 5. State machine and durable lifecycle

```mermaid
stateDiagram-v2
  [*] --> Requested
  Requested --> Authorized
  Requested --> Denied
  Authorized --> EvidenceResolving
  EvidenceResolving --> Inconclusive: insufficient / stale / scope
  EvidenceResolving --> AnalystRunning: eligible
  AnalystRunning --> AnalystValidated
  AnalystRunning --> Failed: exhausted budget
  AnalystValidated --> DiagnosticCompleted: diagnostic
  AnalystValidated --> PlannerRunning: optimization_review
  PlannerRunning --> PlannerValidated
  PlannerRunning --> Inconclusive: deterministic capabilities unavailable
  PlannerValidated --> ReviewerRunning
  ReviewerRunning --> ReviewerValidated
  ReviewerValidated --> ReviewReady: trust and policy references valid
  ReviewerValidated --> Inconclusive: unresolvable concern
  Requested --> Cancelled
  Authorized --> Cancelled
  AnalystRunning --> Cancelled
  PlannerRunning --> Cancelled
  ReviewerRunning --> Cancelled
  ReviewReady --> [*]
  DiagnosticCompleted --> [*]
  Inconclusive --> [*]
  Denied --> [*]
  Failed --> [*]
  Cancelled --> [*]
```

**Terminal outcomes:** `diagnostic_completed`, `review_ready`, `inconclusive`, `denied`, `cancelled`, `expired`, `failed`, `unavailable`; status names are proposed internal typed enum and not final S27 URLs. Additional `ValidationFailed` is a stage failure reason, not evidence of agent success. `review_ready` does not carry `approved=true`.

**Durable state** in the existing `workflow` schema: `workflow_run`, `stage_attempt`, `workflow_lease`, `stage_checkpoint`, `workflow_outbox` logical aggregates (exact tables by S13/S02 workflow owner); one owner/job token, monotonic lease generation/fence, expected stage predecessor, idempotency key scoped to actor/target/mode/input digest, status and cancellation. Stage artifacts belong to AI `knowledge` domain and are immutable; `workflow` stores references/digests, not full prompts/raw hidden reasoning. Build an outbox event atomically with owner state update, deliver at least once and dedupe by event ID.

### 5.1 Checkpoint policy

After each stage: validate tool trace and output schema; dereference authorized evidence; compute a trusted digest for canonical stage artifact; persist a stage record; atomically advance workflow state using lease fence and expected stage. Framework checkpoint may capture mechanics, but **PostgreSQL is authoritative for recovery**. Framework checkpoint data cannot override failed authorization or stale authoritative evidence. Never persist private chain-of-thought, model scratchpad, credentials, raw human access tokens or unrestricted tool payloads.

Recovery: claim expired lease with new fencing generation → reconcile latest committed checkpoint → validate immutable artifact hashes and versioned model/tool/policy profile → re-authorize actor/scope and expiry → revalidate freshness of decision-relevant evidence → resume next stage or end with `expired`/`inconclusive`. A timed-out Ollama request may have produced text while the client disconnected; do not claim exactly-once inference. Only one canonical committed stage result may win via fencing+uniqueness.

## 6. Core typed contracts (logical `v1alpha1`)

### 6.1 `AdvisoryWorkflowRequest`

```yaml
schemaVersion: advisory-workflow-request/v1alpha1
mode: optimization_review
clusterIncarnationRef: verified-cluster-incarnation-ref
target:
  namespace: workloads-batch
  logicalWorkloadId: w04-cpu-worker
planningHorizon: 1h
analysisInputManifestRef: immutable-s13-analysis-input-ref
forecastRef: eligible-s14-forecast-ref
requestedOperation: capacity.review
authorizationRef: trusted-S06-Governance-delegation-ref
reasonCode: operator_requested
```

Public caller may specify target/mode subject to S06 authorization, but cannot supply a trusted `authorizationRef`, machine identity, grant revision, role order, artifact digest or ready-to-execute bit. The gateway/API constructs trusted fields after authentication and policy checks. `current` is a current assessment, not a projected forecast; `24h` may be refused. A permitted system job uses explicit configured machine-purpose authorization, not fabricated human identity.

### 6.2 `CapacityAssessment`

```json
{
 "schemaVersion":"capacity-assessment/v1alpha1",
 "targetRef":"w04-cpu-worker",
 "evidenceStatus":"qualified_with_caveats",
 "observations":[{"claimKind":"queue_risk","narrative":"Queue pressure requires inspection","evidenceRefs":["metric-evidence-ref"]}],
 "forecastInterpretation":[{"forecastRef":"immutable-forecast-ref","interpretation":"advisory-only"}],
 "uncertainties":["sampled peak is not full peak"],
 "missingEvidence":[],
 "proposedFollowupKinds":["inspect_deterministic_candidate"]
}
```

Model-produced text/claims are *proposals*. Trusted adapter injects actual `workflowRunRef`, fixed role, authorized target, resolved evidence and content digest. Narrative must not contain invented numeric facts. Scalar numerical fields, where absolutely needed, are **typed copies** from validated deterministic artifacts with backend-source field paths and exact units, not generated strings parsed as facts.

### 6.3 `OptimizationAdvisory`

| Field | Constraint |
|---|---|
| `candidateRefs[]` | Exact deterministic candidate IDs/digests from S19–S22 only |
| `simulationRefs[]` | Exact immutable versioned simulation evidence; method, intended use and limitations |
| `policyFindingRefs[]` | Deterministic hard-gate results; AI cannot change status |
| `comparativeNarrative` | Human-readable explanation grounded in referenced evidence |
| `requestedBenefits` | References to numeric backend estimates, not free-form CPU/memory numbers |
| `alternativeRefs[]` | Only backend-generated alternatives, not inferred patch manifests |
| `unresolvedQuestions[]` | Explicit uncertainties and required evidence |

### 6.4 `RiskReview` and final `AdvisoryArtifact`

```yaml
schemaVersion: agent-advisory/v1alpha1
workflowRunRef: advisory-run-ref
mode: optimization_review
validatedStageRefs:
  analyst: immutable-stage-ref
  planner: immutable-stage-ref
  reviewer: immutable-stage-ref
candidateRefs: [exact-deterministic-candidate-ref]
simulationRefs: [immutable-simulation-ref]
riskReview:
  findings:
    - category: historical_memory_peak
      severity: high
      evidenceRefs: [authorized-incident-revision-ref]
  unresolvedRisks: []
advisoryDisposition: review_recommended # or reject_candidate / inconclusive
policyGateRefs: [authoritative-deterministic-policy-result-ref]
evidenceStatus: qualified
modelProfileRef: immutable-s15-profile-ref
contextManifestDigest: sha256:trusted-resolved-context
artifactDigest: sha256:trusted-derived-artifact
```

**Forbidden authoritative fields:** `isApproved`, `approvedByAgent`, `executeNow`, `kubernetesPatch`, self-issued actor tokens or a fabricated authorization reference. `review_recommended` is only a non-authoritative opinion, and the authoritative policy result must be read independently at S24. Reviewer cannot mark an S19 hard rejection as passed. Every agent citation is validated by S17, not trusted from model-generated strings. `artifactDigest` computed by trusted serializer over canonical content, not copied from model output.

### 6.5 Workflow factual event catalog

- `AdvisoryWorkflowRequested.v1` — durable intent created; not authorization to mutate.
- `AdvisoryStageFinalized.v1` — validated stage artifact committed, with stage and digest.
- `AdvisoryWorkflowFinalized.v1` — terminal `diagnostic_completed`/`review_ready`/failure and reason.
- `AdvisoryReviewAvailable.v1` — references immutable AI advisory for Governance to inspect.

Emit through owner PostgreSQL/outbox with stable event identity, at-least-once delivery, idempotent consumption and no assumption of global ordering. Model-text-generated events are invalid; actor/authorization metadata only from trusted workflow context. Event details/version subject to S02/S13/S24 alignment.

## 7. Retries, re-evaluation and resource budgets

Retry only transient provider/network/leased-worker failures with bounded exponential backoff, jitter where appropriate and total deadline. A violated authorization, stale source, foreign namespace, invalid hard gate or unsupported deterministic capability is *not retryable by changing prompts*. One controlled Reviewer→Planner re-evaluation is allowed **only** where the Reviewer identifies a resolvable informational gap, new qualified deterministic evidence can be fetched within authorized tool scope, and the original immutable candidate/policy state can be rechecked. Record first and second stage artifacts distinctly; never overwrite rejected output. No additional Reviewer→Planner cycle.

**Provisional budgets** (S28/S30 calibration): max three normal role generations, one bounded re-evaluation of relevant role(s), strict maximum tool-call count per role, overall workflow wall deadline, per-role output caps and a queue limit consistent with S15 single active generator. Never hardcode a budget that is larger than model/host protections. Timeouts include tool and queue waits. Cancellation is an authoritative state transition and causes late results to be ignored through lease fencing.

## 8. Evidence, confidence and escalation semantics

- Confidence is **multidimensional**: evidence quality, forecast qualification (S14), deterministic simulation validity (S21/S22), historical relevance (S18), explanation citation coverage, and workflow validation. Do **not** present model verbal confidence as a statistically calibrated probability.
- Report missingness, low histogram count, sampled memory peak limits, source staleness, UID mismatch, HPA confounding, future-Job assumptions and source-time uncertainty explicitly.
- A `RiskReview` with `reject_candidate` is not a deterministic veto itself; Governance considers cited AI concerns but authoritative hard constraints still apply independently. A deterministic hard fail must block regardless of positive agent review.
- Human escalation may request new evidence, a fresh S14 forecast, or an S19/S22 candidate evaluation; it may **not** request that agent override an unpassed gate or impersonate S24 approval.

## 9. Security and threat tests

| ID | Test | Pass condition |
|---|---|---|
| S16-TEST-001 | Model includes `role=Executor`, `approved=true`, `executeNow` | Role unchanged; output rejected/ignored as unauthorized authority |
| S16-TEST-002 | Reviewer claims candidate passed but S19 gate failed | `review_ready` cannot misstate authoritative policy; fail/inconclusive |
| S16-TEST-003 | Duplicate outbox event + two competing worker leases | Single canonical committed stage; stale fence write rejected |
| S16-TEST-004 | AI Pod restarted after Analyst artifact commit | Validated checkpoint resumed without losing scope/provenance |
| S16-TEST-005 | Human grants revoked mid-workflow | Tool/critical step denied on current authorization; no stale approval |
| S16-TEST-006 | Forged actor/delegation headers | Backend and S17 broker reject; no cross-scope information |
| S16-TEST-007 | S14 low-data refusal or stale snapshot | Inconclusive/diagnostic caveat; no invented numeric forecast |
| S16-TEST-008 | S19–S22 absent | Diagnostic remains available if qualified; full review returns capability unavailable |
| S16-TEST-009 | Re-evaluation request loops | Maximum one controlled cycle; never extend deadline indefinitely |
| S16-TEST-010 | Model JSON malformed or wrong citation | Typed validation failure and bounded repair, no stored fake truth |
| S16-TEST-011 | Ollama stopped mid-run | Durable failure/unavailable, core S14 stays available |
| S16-TEST-012 | Prompt-injected retrieved incident requests Pod delete | No tool privilege path; refusal/security audit |
| S16-TEST-013 | W03 incident from foreign namespace/wrong incarnation | Citation denied or marked non-applicable; not silently used |
| S16-TEST-014 | Cancel during provider request, late provider completion | Late result cannot replace terminal state |
| S16-TEST-015 | Complete authorized review fixture | Three typed stage refs, candidate/simulation evidence, Reviewer finding, explicit non-approval |
| S16-TEST-016 | S31 B2 vs B3 | Deterministic baseline identical; AI value assessed separately, no fabricated impact |

Unit: state-machine guards, DTO/schema and canonical digest. Integration: PostgreSQL leases/outbox, S17 tool broker and S18 retrieval with Testcontainers; limited E2E: W01 latency and W03 memory risk on kind; operational: host outage, model restart, database restore, revoked grants. Evaluate precision of evidence citations and unsafe-finding detection, not simply user satisfaction. Tests must independently inspect Kubernetes mutation audit for **zero agent-originated changes**.

## 10. ADR register — accepted

| ADR | Status | Decision | Architectural consequence |
|---|---|---|---|
| S16-01 | **Accepted** | Microsoft Agent Framework | Runtime API/version pin and checkpoint integration testing needed |
| S16-02 | **Accepted** | Fixed Analyst → Planner → Reviewer for full review | No unrestricted swarm; explicit diagnostic short-path |
| S16-03 | **Accepted** | PostgreSQL-authoritative workflow and validated checkpoints | Framework state never replaces lease/state authority |
| S16-04 | **Accepted** | Strict typed handoffs, immutable evidence-backed advisories | Model text cannot set plan or approval fields |
| S16-05 | **Accepted** | Bounded retry and at most one controlled re-evaluation | Cost and loop ceiling; no safety-gate prompt workaround |
| S16-06 | **Accepted** | Diagnostic versus full optimization review modes | S19–S22 qualified availability is mandatory for full review |

## 11. Traceability and ownership handoffs

| Owner | S16 use | Downstream verification |
|---|---|---|
| S01–S02 | L0–L2, 6 Deployments, durable jobs, Advisory vs Governed | TEST-003/014/015; S24 |
| S03–S05 | Resource budget, W01–W06, S05 independent test provenance | TEST-011/013; S31 |
| S06–S09 | Machine+actor+role, tool allowlists, prompt injection, no secrets | TEST-001/005/006/012; S17 |
| S11–S14 | Exact inventory/metrics/forecast and refusal semantics | TEST-007/013; S17/S19 |
| S15 | Schema/model provider, pressure and deadline | TEST-009/011/014; S28 |
| S17 | Tool registry, context manifest and citations | TEST-010/015; S30 |
| S18 | Historical incidents/valid-time source evidence | TEST-012/013; S31 |
| S19–S22 | Candidate/policy/simulation deterministic results | TEST-002/008/015; Domain E |
| S24–S26 | Plan/human approval/exact execute/verify independent | TEST-001/002; Domains F/G |
| S28–S32 | Reliability, version locks, tests, measured value/release | TEST-003/004/011/016 |

**Open implementation gates:** exact Agent Framework release/API mapping; typed transport serialization and schema validation libraries; one S16 owner for leases/table names; workflow timeout/stage budgets and tuning; concrete Governance advisory request/handback APIs (S24/S27); S19–S22 final candidate schema; host-pressure signal; definitive error codes and read projections; retention and restore policies. These remain *implementation/downstream ownership*, not unresolved approval of the six ADRs.

## 12. Completion statement

All six S16 decisions approved **2026-11-19**. Implementation evidence, real-world agent accuracy, exact release versions and runtime validation are pending. Do not describe an advisory as an approved plan or measured optimization outcome.
