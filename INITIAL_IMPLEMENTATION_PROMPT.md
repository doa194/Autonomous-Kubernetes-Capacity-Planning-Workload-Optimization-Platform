# Codex Initial Implementation Prompt — Autonomous Kubernetes Capacity Planning

You are the principal implementation agent for **Autonomous Kubernetes Capacity Planning & Workload Optimization Platform**, a fully local, production-inspired AIOps Kubernetes capacity operations system. The architecture has already been designed and owner-approved across **8 domains and 32 slices (S01–S32)**. Your job is to implement the accepted system in this repository, not to redesign it.

## 0. First actions and instruction hierarchy

1. Inspect the repository, its Git state, existing files, environment and tooling. Do not overwrite or discard user changes.
2. Read and obey the repository-level `CLAUDE.md` and any applicable `AGENTS.md`. The supplied `CLAUDE.md` governs testing, documentation and comments. If Codex does not automatically read `CLAUDE.md`, you must read it explicitly. Keep the original instruction content intact; any Codex-specific `AGENTS.md` should point to it or reproduce its operative rules without contradiction.
3. Read **every** provided architecture source in `specs/accepted/`, including the master catalog, S01–S18, the S14 numerical companion, the complete consolidated Domain E (S19–S23 plus numerical/simulation companion), Domain F (S24–S26), Domain G (S27–S29), and Domain H (S30–S32). Do not infer source content from filenames. Inspect all chapters, ADRs, acceptance tables, security controls, inter-slice contracts, priorities and explicit deferred/provisional fields. The consolidated sources count as coverage of their constituent slices; the companion contracts are additional normative/implementation-reference inputs. There are 32 *slice IDs*, not necessarily 32 separate Markdown files.
4. Construct an **internal implementation work register** mapping each S01–S32 requirement, accepted ADR, interface, invariant, test oracle and dependency to the owning package, implementation status and verification. Keep implementation work registers separate from the final product `/docs`; do not turn user-facing project documentation into a roadmap or progress diary.
5. Check source precedence: approved, newer slice-level decisions supersede old *proposed* possibilities in the master catalog, but no accepted ADR may be silently overridden. Preserve the exact source filenames or maintain a manifest mapping supplied files to canonical IDs. If two accepted specifications cannot be reconciled, identify the precise clauses, propose a minimal safe resolution and make no unsafe assumption. Clearly mark newly selected implementation details versus accepted architecture.
6. Take stock of runtime availability (Windows host, WSL2, Docker, kind, kubectl, Helm, Python/uv, .NET SDK, Ollama, tool versions). If hardware or infrastructure is not directly reachable, implement and verify offline portions, record unverified operational gates honestly, and provide runnable local checks. Do not fabricate live installations, empirical experiments, security validations, models, or successful releases.
7. **Begin implementing after initial source reconciliation.** Avoid stopping after generating only a plan, scaffolding, interfaces, TODOs or documentation. Work in small coherent dependency-ordered increments, run appropriate tests for each, and continue as far as the current execution environment allows. Do not ask for repeated architectural choices that the accepted sources already settle. Seek user input only for genuine unresolved high-impact conflicts or actions requiring credentials, physical host access, irreversible changes, or explicit human approvals.

### Source location contract

Expected repository layout:

```text
repo-root/
  CLAUDE.md
  specs/
    accepted/
      Autonomous_Kubernetes_Capacity_Planning_Master_Spec_Catalog.md
      S01_Product_Scope_Requirements_Operational_Model(1).md
      S02_System_Architecture_Component_Contracts(1).md
      S03_Local_Kubernetes_Infrastructure_Cluster_Topology(1).md
      S04_Mixed_Language_Demonstration_Workloads.md
      S05_Workload_Simulation_Scenario_Orchestration.md
      S06_Identity_Authentication_Authorization.md
      S07_Workload_Identity_Service_Security_Network_Isolation.md
      S08_Secrets_Data_Protection_Security_Policy_Enforcement.md
      S09_AI_Agent_LLM_RAG_Tool_Security.md
      S10_Software_Supply_Chain_Runtime_Security.md
      S11_Cluster_State_Discovery_Resource_Inventory.md
      S12_Metrics_Collection_Historical_Telemetry.md
      S13_Capacity_Data_Model_Intelligence_Storage.md
      S14_Capacity_Forecasting_Demand_Modeling.md
      S14_Forecasting_Model_Evaluation_Contracts.md
      S15_Local_LLM_Inference_AI_Runtime.md
      S16_Multi_Agent_Architecture_Orchestration.md
      S17_Agent_Tools_Context_Engineering.md
      S18_Operational_Knowledge_Base_RAG_Architecture.md
      S19-S20-S21-S22-S23.md
      Domain_F_Decision_Governance_Execution_S24-S26.md
      Domain_G_Product_Operations_Delivery_S27-S29.md
      Domain_H_Testing_Evaluation_Product_Readiness_S30-S32.md
```

Use discovered paths rather than assuming exact names if the file package is organized differently. The S19–S23 consolidated file **also contains the Domain E numerical, placement and simulation contracts**: read this full combined document, including the companion section. Ignore only exact duplicate copies after verifying hashes/content.

## 1. Product mission and non-negotiable boundaries (Master/S01/S02)

Build an **AIOps Capacity Operations Platform** for Platform Engineers, SREs and Kubernetes operators which:

- Observes live Kubernetes resource configuration, scheduler-relevant inventory, application SLIs, CPU/memory usage, throttling, OOM, Pending Pods, workload and namespace constraints.
- Distinguishes **configured/requested/limited**, **node capacity/allocatable**, **observed**, **forecast**, **simulated**, **applied**, and **verified realized** values; never confuse actual CPU use with scheduling reservations or a hypothetical reduction with measured savings.
- Produces deterministic statistical/ML numerical CPU/memory forecasts and associated uncertainty, provenance, data sufficiency, confidence/eligibility and honest refusals.
- Generates deterministic right-sizing options, policy outcomes, scheduling-fit/fragmentation evaluations and simulation results under explicit SLO, safety, cost, resource and time constraints.
- Provides evidence-grounded local agentic advisory reasoning and historical operational RAG without granting LLMs resource-setting, approval or mutation authority.
- Issues immutable exact plans, requires independent authorized human approval, performs narrow trusted execution, independently verifies rollout, SLO safety and actual benefit, and permits only preauthorized bounded safe recovery.
- Demonstrates results through reproducible local mixed-language workloads, scenario orchestration and controlled B0/B1/B2/B3 experiments with provenance and statistically honest comparisons.

Supported modes: **Observe**, **Advisory**, **Governed**. Autonomy: **L0–L2 only; never L3 autonomous mutation**. Planning: current observation/assessment, 1-hour forecast, **conditional evidence-qualified** 24-hour forecast. Scopes: workload, namespace, node/cluster hierarchy. Criticality: **Critical, Standard, Flexible, Opportunistic**. Prioritize hard safety, authorization and feasibility **before** soft efficiency ranking. A simulation pass, agent recommendation, signed artifact, CI pass or successful deployment never substitutes for human S24 authorization.

Fully local: no paid/cloud inference, remotely hosted application backends, production cloud node provisioning, multi-cluster federation, GPU scheduling, custom scheduler, broad autoscaling control, arbitrary eviction, mandatory service mesh, arbitrary shell tools or dedicated SPA. **P0** capabilities must not be blocked by absent **P1** features; preserve stable upgrade points and truthfully disclose capability status.

## 2. Deployment architecture, modules and data ownership (S02/S13)

Use a cohesive, typed **modular monorepo with exactly six persistent custom business Deployments**:

1. `api`: external operator HTTP/OpenAPI facade and authorization integration.
2. `observer`: Kubernetes read-only LIST/WATCH/reconcile, identity and inventory snapshots.
3. `capacity`: S12 qualified queries, S14 deterministic forecasting, S19–S22 optimization and simulation, quality/evidence imports.
4. `ai`: S15 bounded inference client; S16 three logical agents; S17 tool broker/context; S18 RAG indexing/search.
5. `governance`: scoped authorization/policy, plan lifecycle, immutable approvals and authoritative governance state.
6. `executor`: separate privileged trusted service with exact bounded Kubernetes/GitOps operation and reconciliation/recovery flows.

These are deployment roles, not six demo workloads. Supporting infrastructure (PostgreSQL, Keycloak, Vault, ESO, cert-manager, Prometheus, Grafana, kube-state-metrics, ingress, Calico, OCI registry, admission verifier, Flux when enabled) may run additional Pods without becoming seventh custom business Deployments. The host experiment runner/watchdog and protected Ollama gateway are **not** additional persistent custom in-cluster Deployments. Do not create standalone forecasting, RAG, policy, scheduler, tool, database, messaging, approval or agent services.

Communication: authenticated typed REST for bounded synchronous reads/actions; **PostgreSQL-backed durable jobs, leases, retries, transactional outbox and idempotent handlers** for longer workflows and events. No mandatory RabbitMQ/Kafka/Redis broker. At-least-once delivery is not exactly-once external effects. Define versioned request/response/event/evidence contracts, correlation, errors, pagination, timeouts and idempotency. No shared domain model allowing arbitrary cross-service writes.

One PostgreSQL server, **seven authoritative schemas** with owner-specific DB roles, migrations and write authority: `inventory`, `capacity`, `knowledge`, `governance`, `execution`, `audit`, `workflow`. Preserve named authoritative writers and cross-domain read/contract boundaries. Immutable decision-critical evidence: canonical units/UTC windows, hash/digest, source versions, classifications, actor, scope, cluster incarnation, Kubernetes UID lineage, temporal validity, quality/refusal, retention pins and immutable reference graph. No bulk copy of every Prometheus sample into SQL. S05 runner SQLite remains its execution journal, not a substitute for S13 domain authority. Use versioned bounded JSONB where justified, relational columns/constraints/indexes for authoritative invariants.

## 3. Domain A — Platform and demonstration ecosystem (S01–S05)

**S01**: model operator actors/modes, capability matrix, autonomy bounds, criticality, SLO preservation, workload/namespace/cluster policies, objectives and measurable success. Operationally enforce these, not merely describe them.

**S02**: implement six role boundaries, explicit contracts, trust zones, schema owners, asynchronous workflows/outbox, failure semantics, versioned DTOs and no AI/core dependency inversion.

**S03**: target Windows 11, Docker Desktop + WSL2, **one kind control-plane plus three workers**: one dedicated/tainted platform worker and two experiment workers. Accepted host baseline Ryzen 5 3600, **20 GB RAM**, candidate **12 GiB WSL2 memory cap** (not guaranteed); Calico CNI with real default-deny ingress/egress NetworkPolicies, functional namespaces, quotas/LimitRanges, PriorityClasses, taints/affinity, localhost-restricted authenticated HTTPS, PVCs, independent backups and Python-first idempotent bootstrap/teardown. *Explicit addendum obligation*: merge S05-approved **`capacity-experiments` namespace** into S03 namespace definitions and configure its quota, policies, tokenless driver identity and experiment-node placement; preserve existing namespace/workload assignments. Resource budgets and all node/host availability parameters must be measured before qualification. The kind nodes share one physical host: no fictional high availability or failure-domain independence.

**S04**: implement **six distinct and bounded versioned demonstration workloads** with stable IDs, workload profiles, requests/limits, metrics, probes, priority, controlled endpoints, graceful termination and real SLO/SLI evidence:

| ID | Name | Runtime/controller | Namespace | Class |
|---|---|---|---|---|
| W01 | `latency-api` | .NET 10 ASP.NET Core HTTP Deployment | `workloads-critical` | Critical |
| W02 | `burst-api` | .NET 10 HTTP Deployment; HPA optional opt-in | `workloads-standard` | Standard |
| W03 | `memory-profile` | Python HTTP-controlled memory Deployment | `workloads-standard` | Standard |
| W04 | `cpu-worker` | Python HTTP-controlled CPU task Deployment | `workloads-batch` | Flexible |
| W05 | `analytics-processor` | Python suspended-by-default CronJob template; scenario-triggered Job | `workloads-batch` | Flexible |
| W06 | `opportunistic-batch` | Python scenario-created Job template | `workloads-batch` | Opportunistic |

Normal baseline = **four one-replica Deployments**, W05 CronJob suspended, **no running W05/W06 permanent Pods**; W02 HPA disabled at baseline. Expose measurable latency/error/queue/memory/CPU/Job signals as specified. No business application expansion, unsafe unbounded load, direct mutation credentials or per-workload mandatory database.

**S05**: build a **host Python/Typer scenario orchestrator** with versioned declarative `ExperimentScenario`/`WorkloadProfile` YAML, seed-pinned open/closed-loop demand schedules, phased orchestration, bounded experiment admission, normal/light/moderate/exclusive classes, realistic traffic via ephemeral **in-cluster tokenless Python load-driver Jobs** in `capacity-experiments`, controlled allowlisted faults, host and per-node safety monitoring, **independently credentialed watchdog**, SQLite journal/leases, abort/recovery, ownership/UID-aware idempotent cleanup, content-addressed immutable evidence, phased timestamps, provenance and serial repeatable B0–B3 studies. Same seed guarantees planned traffic schedule/payloads, **not** identical measured CPU/latency/placement. Scenario/driver privilege is separate from S25 executor; runner never resets authorized changes by broad cleanup. Distinguish synthetic **workload origin** from real observed **measurement origin**. Compressed experiments are not 24h history.

## 4. Domain B — Identity, trust and supply chain (S06–S10)

**S06 — human authentication/authorization:** Keycloak OIDC/OAuth2, CLI Authorization Code + PKCE, JWT audience/issuer/signature validation, Viewer/Analyst/Planner/Approver/Executor/Administrator roles, and **RBAC + action + resource/namespace scope + policy conditions**. Administrator is not implicit superuser bypass. Author and approver must be different authorized humans for executable plans. Machine principals/agents cannot pose as humans. Never retain raw bearer tokens in durable jobs. Check current grant/mode/scope at sensitive boundaries, including resume, approval and execution.

**S07 — workload identity/network:** dedicated Kubernetes ServiceAccounts; projected audience-bound expiring tokens only where required; Observer read-only scoped list/watch, Executor narrow read/mutation, `api`/`capacity`/`ai`/`governance` no broad Kubernetes mutation; demo/driver Pods tokenless. Authenticated TLS and selective **mTLS** on privileged paths; Calico default deny verified by positive **and negative** connectivity tests. Short-lived scoped app capabilities for drivers; separate constrained host runner vs watchdog credentials. No mandatory service mesh. Prove host Ollama gateway cannot be bypassed from Kubernetes via alternate host routes.

**S08 — credentials/protection/admission:** **Vault + External Secrets Operator**, offline-protected root CA and **cert-manager-managed intermediate**, differentiated secret delivery and renewal, secure independent encrypted backups, rotation/revocation/recovery, data classification `Public/Internal/Confidential/Restricted`, **redact before embedding**, no credentials in prompts/chunks/logs/metrics/traces/evidence. Enforce **Pod Security Admission Restricted + native CEL ValidatingAdmissionPolicy**, with narrow documented bootstrap exceptions and server-side negative tests; protect audit integrity using separately trusted anchored/signature-verifiable evidence, not mutable hashes stored alongside mutable data. Do not assume these systems are already installed.

**S09 — agent/model/tool threats:** deterministic **in-process Tool Authorization Broker within `ai`**, code-defined per-role capabilities, authenticated and bound actor delegation, independent backend authorization, typed allowlisted tools/arguments, query/cost/byte/time budgets, hostile-result treatment, evidence/claim validation, authorization-aware and provenance-checked RAG. Protected **minimal authenticated host-side reverse proxy for Ollama**, model/path/rate allowlists and no unrestricted host exposure. Prompt injection, poisoned runbooks, model output, forged context and tool response cannot mint approval/privilege or execute Kubernetes, experiment or shell operations. No required general MCP server/gateway.

**S10 — P1 supply chain:** local lightweight **OCI Distribution Registry** (not Harbor), pinned/locked source dependencies and builders, Python/Gitea local secure CI, SAST/SCA/secret scanning, **Syft** SBOM, **Trivy** vulnerabilities, **Cosign key-based signing**, independently verifiable provenance, **Sigstore Policy Controller** for cluster cryptographic admission, layered PSA/CEL verification and periodic read-only runtime conformance/drift assessment (Falco not mandatory). Include all platform/demo/driver images and separate third-party image policy. Supply-chain evidence/signature is not human execution approval. Do not make uninstalled P1 tooling a false prerequisite for honest P0 qualification; provide a capability-specific P1 rollout gate.

## 5. Domain C — Inventory, telemetry, storage and numerical forecasting (S11–S14)

**S11 — cluster truth:** `observer` alone performs hybrid Kubernetes **LIST + WATCH + periodic reconciliation**, bounded typed per-kind cache, owner/UID lineage, Nodes/Namespaces/Pods/Deployments/StatefulSets/DaemonSets/Jobs/CronJobs/HPA/PDB/priority/quotas/taints/affinity/constraints as required by the source, node allocatable and scheduler-effective Pod requests including special cases, assigned versus unscheduled demand. Track per-kind sync, staleness, relist, inconsistency and reconstruction; opaque Kubernetes resourceVersion is **not** a global clock. Persist immutable quality-classified `inventory` snapshots. Never equate Pod consumption with schedulability; do not claim snapshots are globally atomic.

**S12 — observed measurements:** one bounded **Prometheus** pull stack with kubelet/cAdvisor, kube-state-metrics, W01–W04 application exporters and optional node-exporter; Grafana. Approved versioned parameterized PromQL/query registry, exact dimensions/units/identities, quality attributes (coverage/freshness/gaps/reset/censoring/UID/spec matches/host pressure/measurement origin), ephemeral-job visibility and durable decision-critical query evidence. **15s core scrape, 30s supporting scrape, seven-day retention, 5–10GiB Prometheus PVC** are provisional, subject to host measurement. Missing != zero; sampled maximum != true invisible peak; no arbitrary PromQL from users or agents.

**S13 — evidence storage:** define real migrations/constraints for seven schemas; cluster incarnation, workload/controller/Pod/container identity and valid time; request/observed/forecast/simulation/applied/realized distinctions; immutable referenced observations, forecast and evaluation records, scenario imports, artifact digests, classified scope, retention pins, authorized reads, schema-specific roles, transactional outbox, idempotent writes, backups and restoration. Prometheus owns raw TSDB; API/Kubernetes owns live state; SQLite owns S05 journal; Git owns Git-managed desired state.

**S14 — forecasting** plus **entire S14 numerical companion contract**: implement statistical-first baseline competition (persistence, robust rolling, appropriate exponential smoothing, qualified seasonality, optional evidence-tested scikit-learn challengers) in existing `capacity`; point-in-time deterministic feature cutoffs and walk-forward validation, no future-data leakage; explicit CPU millicores/memory bytes, target statistic and time buckets; current is **not** 1h/24h model output; one-hour and conditional evidence-qualified 24-hour; W01–W04 continuous demand; W05/W06 **run-conditional envelopes** only with declared Job assumptions. Preserve S14 feature manifests, source-quality eligibility, interval/quantile calibration where supported, model selection versus baselines, uncertainty coverage, forecast identity/revision/inputs, bounded work, typed refusals and quality reasons. No LLM-generated forecasts, guaranteed seasonal fit, mandatory deep learning, MLflow, Kubeflow or GPU. Forecasts survive AI/RAG outages.

## 6. Domain D — Local AI, fixed agents, tools and RAG (S15–S18)

**S15:** host **Ollama** served only via accepted S09 authenticated reverse proxy; versioned compact quantized generation model and **distinct** small embedding model, chosen/pinned **after local qualification**, no unverified model name/digest or remote fallback. At most one active generative inference under bounded concurrency/queue/tokens/context/memory/host-pressure/exclusive S05 scenario policy. Schema-constrained JSON plus independent trusted validation. Audit exact model/embedding revision, tokenizer/prompt/version/config and measured latency/limits without prompt secrets. Hardware GPU is optional, CPU-only profile must be measured rather than assumed.

**S16:** **Microsoft Agent Framework** in `ai` (select/pin compatible current release only after confirming actual local SDK/API). Exactly three logical specialists **Capacity Analyst → Optimization Planner → Risk Reviewer**, fixed deterministic handoffs, no dynamic swarm. Durable PostgreSQL jobs/stage checkpoints, leases/fencing/retry/cancel, validated typed evidence-linked handoff and scope recheck; maximum one bounded controlled re-evaluation, never for overriding a hard policy fail. Separate `diagnostic` mode (does not require optimizer) from `optimization_review` mode (requires working S19–S22). An advisory cannot approve, write K8s or assert verified benefit.

**S17:** static versioned tool allowlist and permission matrix. At minimum bounded tools for S11 workload/node data, S12 approved windows, S14 forecast/refusal, S18 authorized hybrid knowledge retrieval, S19–S22 deterministic policy/candidate/fit/simulation evidence. No arbitrary SQL/PromQL/HTTP URLs/files/shell/kubectl, no agent-created tool permissions. Separate trusted instructions/actor context from untrusted retrieved text, preserve exact numeric values/units/quality/warnings, validate immutable citation identity/scope/time/digest, enforce total workflow and per-stage tool budgets, safe typed errors and independent backend scope checks. External MCP is optional/deferred, **not** a P0 requirement.

**S18:** implement four operational knowledge collections as defined in source (runbooks, incidents, configuration history/diffs and verified outcomes), inside existing PostgreSQL `knowledge`, **pgvector + PostgreSQL full-text** hybrid. Authenticated source ingestion and revision/revocation, allowlist/classify/redact **before** chunking and embeddings, source-aware chunks/offsets, independent embedding revisions and reindex lifecycle, server-side scope/classification/time prefilter before both lexical/vector search, Reciprocal Rank Fusion plus deterministic provenance/temporal reranking, bounded cited results and retrieval evaluation. Lexical-only degraded retrieval may work when embeddings offline if policy allows. Historical text is never current-state authority, execution instruction or security credential.

## 7. Domain E — Deterministic optimization, placement and simulations (S19–S23)

Read *all five* S19–S23 specifications and the appended **Domain E Optimization Numerical, Placement & Simulation Contracts**; faithfully implement the detailed numerical oracles, golden fixtures and policy definitions. Modules belong to existing `capacity` with only scoped typed tool/API consumption by AI.

- **S19:** hierarchical versioned declarative workload/namespace/cluster policies and class/SLO protections. Distinguish immutable invariants, hard gates and soft unit-safe objectives; hard gate results `pass | fail | unknown | not_applicable`; **unknown required gates block execution eligibility**. Fail closed on stale/ambiguous/missing risk data; immutable evaluation digest/reasons. Protect platform/system workloads from optimization.
- **S20:** deterministic bounded **CPU vs memory** and **requests vs limits** right-sizing candidate generation; W01–W06-specific strategies, resource/cgroup/sampled peak/forecast uncertainty, conservative envelopes, constraints and stable ordering/digests; immutable ResourceCandidate. VPA is **recommendation-only baseline** if used; never create a competing autoupdate controller. P0 action boundary: one allowlisted demo controller and exact compatible container CPU/memory request/limit fields, not generic patches or autoscaling mutations.
- **S21:** Kubernetes scheduler-aware feasibility/fragmentation with node allocatable, already committed effective requests, init/overhead, taints/tolerations, selectors, affinity/anti-affinity/topology and supported PDB/priority constraints as mandated by source. Correctly model uncertainty/unsupported constraints; refuse confident fit when not supported. No custom scheduler, actual Pod placement mutation or physical-host independence assumption.
- **S22:** deterministic candidate/simulation flow, baseline comparison, feasibility/policy-before-ranking, scenario/constraint/risk/objective and forecast-source compatibility, noncircular dependency resolution, immutable OptimizationEvaluation and analytical versus empirical distinction. Simulation output is **not** approval, applied change or realized savings. Use Domain E gate/ranking oracles and golden fixtures.
- **S23:** P1 advanced HPA/autoscaling advisory and request/replica coupling analysis; W02 HPA baseline off. Handle risk and separate optimization-advisory results from actual mutation, do not silently expand P0 allowed action set or install VPA update controller.

## 8. Domain F — Human governance, exact effects and evidence-qualified recovery (S24–S26)

Treat the consolidated Domain F specification as **normative in full**.

**S24:** authoritative immutable plan revisions, policy/risk/evidence identity, before-and-after field envelope, target cluster incarnation/controller UID/container and adapter/expected-state preconditions; proper state machine and exact content hash. Reevaluate hard constraints at plan issuance and approval. Require **one independent authorized human approver separate from author**, current role/scope/mode and time-bound exact approval. Distinguish candidate, optimization evaluation, plan, approval, intent, applied and verified states. A changed payload invalidates approval; no model, administrator shortcut, release signature or CI-run approval.

**S25:** separate narrow `executor` process and mutating SA, **durable reconcile-first workflow**: intent stored *before* external effect; lease/fencing/dedupe; claim and independently validate approval, current policy, fresh UID/spec/resourceVersion or safe compare-and-set, exact allowed fields, ownership and conflict. Primary P0 adapter = direct Kubernetes API mutation of allowlisted demo controller. Architecture includes **ownership-aware GitOps adapter**; Flux integration is P1, and never treat a Git commit as applied/safe. On uncertain timeout or lease expiry **observe before retry**; no blind second patch or claim of exactly-once K8s effects. Enforce no-op/idempotency semantics and strict independence of experimental S05 authority.

**S26:** separate configuration/rollout verification from operational safety/performance validation; qualified time windows, SLO effects, requested vs observed resource semantics, source quality and immutable outcomes. Distinct statuses for applied-pending-verification, verified-safe, benefit-verified, inconclusive, recovery-verified, escalated. Recovery is a **new external operation** permitted only under a *preauthorized bounded inverse* after fresh current preconditions/ownership checks; otherwise stop/escalate, never blind reset. Outcome and historical learning must cite authoritative actual evidence, not simulation or agent self-assessment.

## 9. Domain G — API/CLI, operations, reproducible delivery (S27–S29)

**S27:** existing `api` hosts versioned authenticated FastAPI/OpenAPI; Python **Typer `capacityctl`** operator CLI with OIDC/PKCE, explicit Observe/Advisory/Governed capabilities, scope, typed pagination/errors, optimistic concurrency/idempotency, async operation status, and separate candidate/plan/approval/execution/outcome read semantics. Distinct `capacity-lab` infrastructure automation and S05 scenario utility. Read-oriented Grafana; no custom SPA or generic cluster mutation endpoint.

**S28:** reuse bounded Prometheus/Grafana and application/operational metrics; structured logs, OpenTelemetry-compatible correlation, optional host-qualified Jaeger/Loki rather than mandatory extra-heavy infrastructure. Define capability-specific readiness/degradation so Ollama failure cannot block observer and capacity core. Alerts, runbooks, dependency failure and queue/backpressure, exact authorization/secret/PKI faults, audit health, backup/restore and chaos/rebuild-and-restore exercises; protect independent encrypted recovery material. Single workstation does not qualify as multi-host HA; report measured, not aspirational, SLOs.

**S29:** Python-first idempotent **`capacity-lab`** cluster/bootstrap/validate/cleanup; Helm/Kustomize pinned manifests, ordering of CNI, security, PKI, secrets, storage, identity, database, core services, workloads and observability; compact/balanced/verification profiles with host budget preflight. Local secure **Gitea Actions** plus equivalent Python-run local pipeline stages. **Flux CD is accepted P1** as the GitOps controller, gated by S25 authority/ownership/reconciliation. P1 S10 image registry, scanning, signing, provenance and Sigstore admission are accepted exact tooling choices. Pin and verify third-party versions, Python dependencies (uv lock), .NET 10 dependencies and image digests; no floating latest in qualified release. Keep CI, release, S05 runner and S25 executor principals isolated; updates/migrations/backups/restore/releases have idempotent recorded status, cleanup and separately scoped rollback from S26 workload recovery.

## 10. Domain H — Tests, scientific evaluation and release honesty (S30–S32)

**S30:** apply `CLAUDE.md` test philosophy: meaningful, risk-based **many unit; moderate integration; small component/API; very few critical E2E; necessary real infrastructure/operational**. Python pytest, .NET tests, Testcontainers, real kind tests as appropriate. Use fast/integration/full execution profiles with targeted suites per increment, not a full expensive suite after every edit. Contract, property/golden, durable workflow replay and state-machine, ID/time/units, authorization, credential/token revocation, network/admission negative checks, malicious RAG, tool endpoint bypass, forged approval, stale-state conflict, restore and compensation tests. Never fabricate test results or mark an unavailable cluster test passing.

**S31:** perform **four exact S01 B0–B3 comparisons** as defined in source; use pinned scenario seeds/profiles, qualified paired/serial/repeated evaluations and controlled conditions; evaluate forecast and uncertainty vs baselines, deterministic right-sizing, scheduling fit/fragmentation, safety/SLOs, simulation-vs-actual, empirical savings separately from request-reservation reductions, RAG search quality, grounded agent accuracy/tool security, and **marginal B3 versus B2 benefit**. Record independent ground truth, refusals, confounders, host pressure, confidence/stats and immutable result manifests. Do not treat AI-produced explanations or in-sample fit as independent proof, do not fabricate benefit, and do not convert compressed lab tests to 24h history.

**S32:** capability-scoped P0/P1 release profiles and six evidence-backed release gates according to source: security/correctness, operational behavior, reproducibility, experimental qualification and other precisely specified gate categories. Produce immutable redacted release evidence, claim-to-proof traces, applicable S30/S31/S28/S29 verification, explicit human release review and truthful `not_run`/`blocked`/`failed`/`qualified` states. Approval of *capacity mutation* remains independent of software release qualification. P1 Flux/S10 not required for truthful P0 claims unless the selected release profile advertises them.

## 11. Critical invariants to encode as tests and boundaries

1. **AI -> Kubernetes/Executor/experiment changes is forbidden** by network/identity/backend controls, not solely prompts.
2. **Unknown critical quality/fit/security/policy = not eligible**; no missing-as-zero, false interval/peak confidence or silent degraded safety.
3. **No actual mutation without exact current human approval**; author != approver; expired, stale, changed-scope, changed-payload or superseded plans denied.
4. **S05 experiment setup authority != S25 optimization authority**, including cleanup/recovery.
5. **Forecast != candidate != plan != approval != applied != verified outcome** with separate evidence and database ownership.
6. **Kubernetes UID/cluster incarnation and time-valid config matter**; names/resourceVersion alone are not stable global identity or time.
7. **Hard SLO/scheduling/resource/security constraints are never exchanged for soft utilization benefits**.
8. **Current scheduling requests and allocatable are not observed CPU/memory**; forecast demand and sampled maxima are explicitly qualified and typed.
9. **At-least-once job and external Kubernetes/Git operation handling uses idempotent reconcile-after-unknown**; no blind retry/rollback.
10. **One local physical host**; kind worker count is not physical failure isolation; admission protects physical RAM/CPU and experiment exclusivity.
11. **No secrets, tokens, raw credentials or Restricted data in agent prompts, embeddings, evidence bundles or telemetry**.
12. **P0 independent deterministic core** remains available if AI, embeddings, knowledge or Ollama fail.
13. **No seventh permanent custom business Deployment, second authoritative K8s watcher, required message broker or generic Kubernetes mutation API**.
14. **Architecture-approved != implemented != deployed != tested != empirically qualified**; capability claims must follow evidence.

## 12. Implementation method and dependency-aware work sequence

Implement incrementally, maintaining a working repository. Use these as engineering increments **not as final project documentation chapters or arbitrary milestones overriding slice dependencies**:

**Increment A — executable baseline:** source inventory/reconciliation, repository architecture and typed contracts, Python tooling/uv and .NET 10 setup, static checks, local developer/container tests, version-pinning framework, secure initial CI, kind topology/Calico/namespaces/Python automation, initial runtime budget checks. Fix accepted S03 `capacity-experiments` addendum in implementation. Produce real unit/integration checks and initial actual deployability, not only stubs.

**Increment B — credible demand & observation:** implement W01–W06 bounded workload behavior/deployments and S05 CLI/scenario/driver/watchdog/journal/evidence; S11 real Kubernetes Observer; S12 Prometheus/metrics and S13 minimal authoritative schemas; run safe limited scenario and show aligned inventory/metric facts, or mark live checks blocked with exact runnable scripts.

**Increment C — deterministic intelligence:** implement S14 and numerical companion with baseline/backtest/refusal/quality; S19–S22 candidate generation, scheduler-fit and simulation with Domain E numerical fixtures. Persist typed evidence and prove safe non-eligibility cases before any mutating path.

**Increment D — secure operator/governance:** Keycloak OIDC, scoped application authorization, service identity/TLS/Calico, Vault/ESO, cert-manager and PSA/CEL; REST/Typer public read contracts, Governance immutable plan/approval state, Executor exact narrow operations, observe-before-retry and S26 verification/inverse recovery. **Keep mutation feature disabled** until all required authorization, precondition, policy, admission, network and operational negative tests pass. Demonstrate only explicitly approved local test operations and never use this prompt as permission to approve an actual plan.

**Increment E — advisory intelligence:** local protected Ollama and separately pinned/qualified generation+embedding, Microsoft Agent Framework fixed workflow, deterministic Tool Authorization Broker, `knowledge` pgvector/FTS, authorized hybrid RAG, strong injection/forgery/groundedness tests and graceful isolation; connect to real S11–S14/S19–S22 evidence rather than mock success.

**Increment F — operations & release:** S27–S29 operational polish, observability/readiness, backup/rebuild/restore, security/cert/key rotation checks, Helm/Kustomize/environment profiles, local Gitea pipeline and S10/Flux **P1** gated features; S30 safety regression, S31 fair B0–B3 experiments, S32 capability-scoped evidence-backed release. S23 P1 autoscaling advisory must remain optional. Qualify only the features that genuinely pass.

This sequence may be adjusted for dependencies (for example bootstrap a minimum identity/PKI layer before integrating secured services). Never postpone essential security controls until after enabling real mutations. Use tests and example fixtures with safe synthetic data before live capacity experiments. Keep complex platform dependencies resource-qualified; no pretending a 20GB workstation can always run all optional stacks simultaneously. Prefer smaller verified operational profiles rather than bypassing identity, TLS, network isolation or policy.

## 13. File structure and engineering quality

Follow exact accepted architecture and ownership; proposed directories are merely a reference:

```text
repo/
  CLAUDE.md                       # canonical provided guidance
  AGENTS.md                       # optional Codex bridge without contradictory rules
  specs/accepted/                 # exact accepted source package
  src/
    api/ observer/ capacity/ ai/ governance/ executor/
    contracts/ shared-infrastructure/
  workloads/
    w01-latency-api/ w02-burst-api/ w03-memory-profile/
    w04-cpu-worker/ w05-analytics-processor/ w06-opportunistic-batch/
  tools/
    capacityctl/ capacity-lab/ scenarios/ watchdog/
  infra/
    kind/ helm/ kustomize/ security/ observability/ identity/ storage/
  db/migrations/
  configs/
    profiles/ workloads/ scenarios/ policies/ models/
  tests/
    unit/ integration/ component/ e2e/ operational/ security/ evaluation/
  docs/
  README.md
```

Adopt clean package boundaries, type checking, linting, config/schema validation, dependency locks, minimal reproducible containers, non-root/readonly filesystems/capability drops, explicit time/units, structured error contracts and local-first performance. Prefer Python for all host automation (not shell/PowerShell as the primary orchestration); .NET 10 for the two specified demo APIs. Implement real stable migrations and coherent contract/version strategies instead of large speculative scaffolding. Use supported APIs of pinned current frameworks, no imaginary SDK endpoints.

**`CLAUDE.md` instructions remain binding**:
- Test important contracts/failure modes at the cheapest reliable layer; after a change run relevant suites plus required operational verification; do not inflate test counts or duplicate assertions.
- Short useful ordinary language comments in important files and for important rules/security/failure handling; no C# XML-doc `/// <summary>` blocks; don't comment trivial lines.
- Final product README is focused on mission/features/stack, architecture, setup/run/test/cleanup, limits, multiple actual tech badges and deeper documentation links.
- Final `/docs` describes the **implemented/verified final system**, not implementation chronology/progress/agent activity/roadmaps. The provided file requires **at least 20–25 meaningful `/docs` documents** despite also discouraging useless documents: satisfy this through genuinely distinct final system concerns (e.g. architecture, security, identity, deployment, recovery, forecasting, evaluations, API, operational runbooks) and avoid filler. Reinspect final code and running commands during a **dedicated final documentation pass**; Mermaid architecture diagrams must match actual system.
- Clearly distinguish implemented, simplified local-lab behavior, unavailable P1 capability and production migration requirements.

## 14. Acceptance and communication contract

For each implemented increment, inspect actual state, make changes, validate with relevant commands, fix regressions and report **concrete file paths, implemented behavior, tests actually executed and their results, live environment checks, blocked cases, unverified assumptions and next dependency**. Do not claim P0 complete if any required security/availability/execution gate remains absent. Record any accepted-spec deviation with proposed ADR and obtain owner approval before implementing conflicting behavior.

The definition of done for the overall project is the **actual S30/S31/S32 release profile evidence**, not code generation or a passing unit-only suite. Explicitly track spec-to-code-to-test-to-evidence links for S01–S32. Do not produce fake benchmarks, credentials, backup recoveries, admission validation, container scans, model qualifications or empirical savings. No unrestricted external download or remote model-service fallback during runtime. Preserve user edits and avoid destructive cleanup outside scenario-owned resources.

**START NOW:** verify files are accessible, study all sources, map accepted architecture and its key dependencies, inspect current repository, implement the first safely testable vertical slice, run its verification and continue through subsequent dependency-ordered work as available. Do not ask me to repeat decisions contained in the accepted specifications. Do not attempt an unsupervised cluster mutation merely because this kickoff prompt mentions governed execution. Demonstrate mutations only under the explicitly implemented S24 human approval and S25 executor safeguards.
