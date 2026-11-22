# Specification traceability

The accepted specifications are in `specs/accepted/`; `specs/manifest.yaml` records the SHA-256 of
each source file, and release gate RG01 blocks if any of them changes. This page maps each slice to
where it is implemented and how it is verified, then lists every deliberate deviation and open item.

## 1. Slice map

| Slice | Topic | Implementation | Verification |
|---|---|---|---|
| S01 | scope, operating modes, autonomy L0–L2, horizons | `contracts/enums.py`, `governance/authz.py` (mode rules) | `test_governance.py`; `/capabilities` reports `L0-L2 (no L3)` |
| S02 | six services, contracts, jobs/outbox | `shared/serve.py`, `shared/jobs.py`, `shared/db.py` | `test_architecture.py`, `test_schema_boundaries.py` |
| S03 | kind topology, pools, Calico, priorities, quotas | `infra/kind`, `infra/kustomize/base`, `lab/cluster.py` | `capacity-lab verify networking/scheduling` ([cluster-topology.md](cluster-topology.md)) |
| S04 | W01–W06 demonstration workloads | `workloads/`, `contracts/workloads.py`, `lab/manifests.py` | .NET tests; live deployment; experiments |
| S05 | scenarios, runner, journal, capabilities, watchdog | `scenarios/`, `watchdog/` | `test_scenarios.py`; live runs E01–E04 |
| S06 | OIDC, roles, grants | `shared/auth/oidc.py`, `governance/authz.py`, `lab/identity.py` | `test_api_boundaries.py`, `test_governance.py`, operational auth test |
| S07 | workload identity, network isolation, selective mTLS | `shared/auth/service_identity.py`, `networkpolicies.yaml`, `shared/serve.py` (mTLS listener), `api/app.py` (relay) | `capacity-lab verify`, `test_live_lab.py`, `test_internal_mtls.py` |
| S08 | secrets, PKI, PSA, CEL admission, audit | `lab/secrets.py`, `lab/pki.py`, `admission.yaml`, `0002_workflow_and_audit.sql` | admission negative cases, executor field guard, `capacity-lab audit verify` |
| S09 | AI/LLM/RAG/tool security | `ai/gateway.py`, `ai/broker`, `ai/agents/workflow.py` | `test_ai_tool_security.py`, model qualification |
| S10 | supply chain | pinned versions, digest-pinned images, locked dependencies | RG05; signature admission **not installed** (P1) |
| S11 | discovery and inventory | `observer/` | `test_accounting_and_rightsizing.py` |
| S12 | telemetry | `capacity/telemetry/registry.py`, Prometheus configuration | observability check; window-observation unit tests ([telemetry.md](telemetry.md)) |
| S13 | data model | `db/migrations` | integration tests |
| S14 | forecasting | `capacity/forecasting/` | `test_forecasting.py`; live analyses |
| S15 | local inference | `ai/inference/client.py`, `ai/gateway.py`, `lab/model.py` | model qualification reports |
| S16 | three-agent workflow | `ai/agents/workflow.py` (Microsoft Agent Framework) | security tests |
| S17 | tools and context | `ai/broker/registry.py`, `capacity/tools.py` | security tests |
| S18 | knowledge base / RAG | `ai/knowledge/store.py` | `test_knowledge_retrieval.py` |
| S19 | optimization policy and gates | `capacity/policy/` | `test_optimization_golden.py` |
| S20 | candidate generation | `capacity/rightsizing/generator.py` | golden + rightsizing tests |
| S21 | placement simulation | `capacity/placement/fit.py` | golden tests |
| S22 | ranking | `capacity/simulation/evaluate.py` | golden tests |
| S23 | HPA advisory | compatibility gate only (`hpa.compatibility`) | golden tests; advisory **not installed** (P1) |
| S24 | plans, approvals, denial records | `governance/plans.py`, `governance/service.py` | `test_governance.py`, integration chain, live E2E |
| S25 | executor | `executor/operations.py`, `executor/service.py`, `executor/store.py` | `test_executor.py`, operational recovery test, live E2E |
| S26 | verification and recovery | `executor/verification.py`, `executor/service.py` (`observe_window`) | `test_executor.py`, live E2E |
| S27 | API and CLI | `api/app.py`, `cli/capacityctl.py` | component tests, live E2E |
| S28 | observability and operations | Prometheus / kube-state-metrics / Grafana, JSON logs, health endpoints | observability check |
| S29 | delivery, bootstrap, backup | `lab/`, `images/`, `lab/backup.py`, `lab/recovery.py` | bootstrap journal, restore drills |
| S30 | test strategy | `tests/` | `capacity-lab release test` |
| S31 | evaluation protocol | `release/bundle.py` (arm availability), [evaluation.md](evaluation.md) | RG03 |
| S32 | release gates | `release/gates.py`, `release/bundle.py` | `test_release_gates.py`, `capacity-lab release qualify` |

## 2. Deviations and open items

| Item | Specification expectation | Implemented | Reason / status |
|---|---|---|---|
| cert-manager namespace | platform components in `capacity-system` | cert-manager in its own `cert-manager` namespace | the pinned upstream manifest is applied unmodified and hash-verified |
| `capacity-system` PSA level | restricted | `baseline` (audit/warn restricted) | the Vault and External Secrets charts need it ([limitations.md](limitations.md)) |
| default-deny coverage | all managed namespaces | not on `capacity-system` and `cert-manager` | open item |
| Keycloak root filesystem | read-only | writable (single documented exception) | Keycloak writes build artefacts at start |
| test-only client | no password grant | `capacity-e2e` direct grant accepted by the API in the lab | needed for automated live tests; removal listed in [production-considerations.md](production-considerations.md) |
| B1 baseline | rules/VPA recommendation arm | not installed | reported as `not_run` in RG03 |
| P1 features (GitOps adapter, S10 admission, S23 advisory) | optional / P1 | not installed | reported by the API as `unqualified` / `not_installed` |
| generation model | compact 3–4B instruction model, pinned after qualification | `gemma4:e4b` (Ollama reports 7.5B parameters, Q4_K_M) | the locally available model; pinned by digest only after it passes qualification — it has not |
| multi-Pod telemetry windows | join series through Pod-UID lineage (no name-only joins) | name-prefix selection; windows with several Pod series refuse (`multiple_series_combined` → `unclassified_quality_issue`) | fail-closed substitute; forecasts pause ≈ 6 h after any rollout |
| W04 queue completion evidence | completion-within-deadline rate for the queue gate | queue-depth trend only; completion rate not derived | W04 CPU decreases are always `indeterminate` |
| platform domain metrics | capability health metrics | HTTP metrics + health endpoints | open item ([observability.md](observability.md)) |
| full rebuild-from-backup drill | rehearsed, with RPO/RTO | database restore drill automated; full rebuild not rehearsed | RG04 reports `not_run` |
| live governed verification | a verified outcome on the live lab | chain run once, ended `recovered` | repeat on a clean 6-hour window ([proof-of-operation.md](proof-of-operation.md)) |
| static typing | — | `mypy` configured, 94 findings | not part of a release gate |
