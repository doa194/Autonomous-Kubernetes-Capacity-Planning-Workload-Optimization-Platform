# Production considerations

The lab is a faithful small-scale model of the architecture, but several parts are deliberately
simplified to fit on one workstation. This page separates **what exists now**, **what is simplified
for the lab**, and **what would have to change** for production use. Nothing in the last column is
implemented.

## 1. Area by area

| Area | Implemented now | Lab simplification | Needed for production |
|---|---|---|---|
| Cluster | kind, 1 control plane + 3 workers, Calico, tainted platform pool | single host, no HA, Docker Desktop networking, etcd on a shared disk | managed or self-managed multi-node cluster, HA control plane, etcd on fast dedicated disks |
| Platform services | 6 Deployments with separate identities, DB roles, certificates, policies | 1 replica each, no PodDisruptionBudgets | ≥ 2 replicas for API, governance and executor (the executor already has fencing and leases), PDBs, HPA where safe |
| Database | PostgreSQL 17 StatefulSet, TLS, 7 roles, RLS, append-only tables | single instance on a local-path volume | managed PostgreSQL or an operator with replication, PITR and automated backups |
| Backups | on-demand AES-256-GCM dumps, network-less restore drill, audit-head check | keys and backups on the same host; no schedule | scheduled backups to separate storage, keys in a KMS/HSM, automated periodic restore tests, rehearsed full rebuild |
| Secrets | Vault + External Secrets, per-namespace Vault policies, file mounts | one unseal share, file storage, root token kept DPAPI-protected | Vault HA with auto-unseal (KMS), no stored root token, an audit device, short-lived dynamic DB credentials |
| PKI | offline root (host) + name-constrained intermediate in cert-manager, 30-day leaf certificates | root key on the workstation, lab-only CA, no revocation lists | root in an HSM or offline vault, CRL/OCSP, enterprise trust distribution |
| Identity | Keycloak OIDC + PKCE, per-namespace grants, separation of duties | generated lab users; test-only `capacity-e2e` password-grant client accepted by the API | corporate IdP federation, MFA, no password grant, `capacity-e2e` removed from `OIDC_ALLOWED_CLIENTS` |
| Network | Calico default-deny on all platform and workload namespaces, dynamic API-server policies | `capacity-system` and `cert-manager` outside default-deny; mTLS on one hop | default-deny everywhere, egress gateway, optional service mesh for mTLS on every hop |
| Admission | PSA `restricted` + 4 CEL policies (hardening, no token, driver template, executor field guard) | no image-signature verification (S10 not installed) | signed images (cosign) verified at admission, SBOMs, vulnerability-scanning gates |
| Telemetry | fixed query registry, quality flags, fail-closed refusals | Pods selected by name prefix; multi-Pod windows refuse | Pod-UID-to-controller lineage so windows spanning rollouts can be used safely |
| Observability | Prometheus, kube-state-metrics, Grafana, JSON logs with correlation IDs | one dashboard, no alert rules, local retention, no log aggregation | SLO dashboards and alerts, long-term metric storage, central logs, tracing, domain metrics |
| AI runtime | Ollama behind an authenticated TLS gateway, on-host qualification | one consumer-grade model, provisional thresholds, not qualified | dedicated inference hosts, capacity planning for inference, a model registry with signed artifacts |
| Experiments | host runner with journal, capability tokens, watchdog | host-side SQLite state | not applicable to production clusters; real demand replaces synthetic load |
| Release | digest-pinned images, locked dependencies, computed release gates | local registry, no CI, base images pinned by tag | CI running the same gates, promotion between environments, signed release manifests, digest-pinned base images |

## 2. Behaviour that would stay the same

The core safety properties do not depend on the lab simplifications and would carry over unchanged:

* every resource number comes from deterministic, recomputable code;
* hard gates run before scoring, and unknown evidence never passes;
* plans are immutable and approved by exact digest by a different person;
* the executor is reconcile-first, uses `test` preconditions and fencing, and is limited by RBAC and
  the field-guard admission policy;
* verification compares against a comparable baseline, and an unverified change is reversed exactly
  once;
* refusals are durably audited; history is append-only with a signed hash chain;
* the AI can only comment on evidence.

## 3. Scaling notes

* The job queue uses PostgreSQL polling. It suits analysis workloads (seconds to minutes per job); at
  much higher rates a broker or a `LISTEN/NOTIFY`-based wake-up would be needed.
* The observer keeps a full LIST/WATCH of the relevant object families in memory; very large clusters
  would need sharding by namespace.
* Forecasting is per workload and per resource and is CPU-cheap; the 6-hour telemetry lookback per
  analysis is the main Prometheus cost.
* The single audit hash chain serializes audit inserts with an advisory lock; very high write rates
  would need partitioned chains with periodic cross-anchoring.
* One change per workload per ~6 hours (the multi-Pod refusal) is acceptable for a lab but would be too
  slow for production; Pod-UID lineage removes that limit.
