# Glossary

Terms as they are used in this project. Kubernetes and security terms are explained in plain
language; project-specific terms point to the page that covers them in depth.

## Kubernetes and resources

| Term | Meaning |
|---|---|
| **Container request** | The CPU and memory the scheduler *reserves* for a container on a node. Too high wastes capacity; too low over-commits the node. |
| **Container limit** | The hard ceiling for a container. CPU above the limit is throttled; memory above the limit gets the container killed (OOM). |
| **Millicore (m)** | One thousandth of a CPU core. `150m` = 0.15 cores. The platform stores CPU as integer millicores. |
| **Mi / MiB** | Mebibyte, 1,048,576 bytes. The platform stores memory as integer bytes. |
| **Effective request** | What a Pod really reserves on a node: the larger of the biggest init container and the sum of the app containers (plus restartable sidecars), plus Pod overhead. See [optimization-engine.md](optimization-engine.md). |
| **Working set** | The memory a container actively uses (what the kernel counts towards the OOM limit). |
| **CFS throttling** | The Linux scheduler pausing a container that used its CPU quota; measured as throttled periods / periods. |
| **OOM kill** | The kernel stopping a container whose memory exceeded its limit. |
| **Deployment / ReplicaSet / Pod** | A Deployment declares the desired Pods; each template change creates a new ReplicaSet and rolls Pods over to it. |
| **Rolling update / surge Pod** | Kubernetes starts a new Pod before stopping the old one; the extra ("surge") Pod must fit on a node too. |
| **Taint / toleration** | A taint keeps Pods off a node unless they tolerate it. The platform node is tainted so demo load cannot land there. |
| **Node affinity / node selector** | Rules that place a Pod on nodes with certain labels (for example `capacity.lab/pool=experiment`). |
| **PriorityClass** | A Pod's scheduling priority; lower-priority Pods are preempted first. See [cluster-topology.md](cluster-topology.md). |
| **ResourceQuota / LimitRange** | Namespace budgets for requests/limits and per-Pod bounds. |
| **HPA** | Horizontal Pod Autoscaler; changes the replica count. A CPU-based HPA reacts to request changes, so the platform blocks CPU-request changes on HPA targets. |
| **NetworkPolicy** | Rules for which Pods may talk to which; enforced here by Calico. |
| **Pod Security Admission (PSA)** | Built-in Kubernetes check of Pod security settings per namespace (`restricted`, `baseline`). |
| **ValidatingAdmissionPolicy (CEL)** | A Kubernetes-native rule written in the CEL expression language that accepts or rejects API requests. |
| **ServiceAccount (SA)** | A Kubernetes identity for software. **Projected SA token**: a short-lived, audience-specific token the kubelet writes into a Pod. |
| **RBAC** | Kubernetes role-based access control: which identity may use which verb on which resource. |
| **kind** | "Kubernetes in Docker": each node is a Docker container. |
| **JSON Patch `test`** | A patch operation that makes the *whole* patch fail unless a field still has the expected value. |

## Platform concepts

| Term | Meaning |
|---|---|
| **Analysis** | One run of the capacity engine for one workload: evidence, forecasts, candidates and gate results, identified by a digest. Evidence only — it changes nothing. |
| **Evidence** | The recorded inputs of an analysis: inventory snapshot, telemetry windows with quality flags, forecasts, placement inputs. |
| **Query registry** | The fixed list of named, versioned Prometheus queries the engine may run. See [telemetry.md](telemetry.md). |
| **Coverage** | The share of expected samples or buckets that actually have data. |
| **Quality flag** | A label on a telemetry series such as `scrape_gap`, `multiple_series_combined` or `peak_not_guaranteed`. Unknown flags make forecasts refuse. |
| **Censored bucket** | A bucket where measured demand may be lower than real demand (for example while CPU-throttled). |
| **Forecast** | A predicted demand curve for one workload, resource and horizon, with an interval; or a **refusal** with typed reasons. See [forecasting.md](forecasting.md). |
| **Walk-forward backtest** | Testing a model by repeatedly fitting it only on data before a point and scoring it on data after that point. |
| **Persistence model** | "Next value = last value"; every selected model must be at least as good as this baseline. |
| **Refusal** | A deliberate "no answer" with a reason (for example `insufficient_history`), stored like a result. Downstream gates treat it as `unknown`. |
| **Candidate** | One proposed set of new values for a workload's CPU/memory requests and/or limits. |
| **Demand envelope** | max(observed peak or p95, forecast upper bound) × (1 + class margin): the protected demand a candidate must cover. |
| **Class** | A workload's criticality (`Critical`, `Standard`, `Flexible`, `Opportunistic`), which sets floors, margins and change budgets. |
| **Gate** | A hard rule a candidate must satisfy; results are `pass`, `fail`, `unknown` or `not_applicable`. |
| **Classification** | The gate verdict for a candidate: `qualified` (all required gates pass), `rejected` (any fail) or `indeterminate` (any unknown). |
| **Evaluation** | A candidate together with its gate results, placement result and ranking; a plan is created from an evaluation digest. |
| **Pareto front** | The candidates that no other candidate beats on every objective at once. |
| **Tighten-only** | Policy overrides may only make a parameter stricter, never looser. |
| **Digest** | SHA-256 over canonical JSON (sorted keys, no extra whitespace). Two services computing the same object get the same digest. |
| **Plan** | An immutable, digest-identified description of exactly one change to one workload. A changed plan is a new revision. |
| **Approval** | A record that an Approver accepted one exact plan revision and digest. Valid 15 minutes, revocable until consumed. |
| **Separation of duties** | Creating, approving and executing need different roles; the approver must not be the plan's author. |
| **Operating mode** | `Observe` (analysis only), `Advisory` (adds AI advisories) or `Governed` (adds approvals and execution). |
| **Grant** | A database record giving a person a role for named namespaces; roles without a covering grant do nothing. |
| **Intent** | Governance's record that an approved plan revision should be executed; valid 15 minutes. |
| **Authority view** | `governance.executor_authority_v`: the only governance data the executor may read to decide whether it may act. |
| **Operation** | The executor's record of applying one intent, with an append-only list of state transitions. |
| **Fencing epoch** | A counter increased each time a job or target lock is taken; a holder with an older epoch can no longer write. |
| **Reconcile-first** | After any uncertainty (timeout, crash) the executor reads the live object before deciding anything. |
| **Verification** | Checking a change: first the configuration (exact values, rollout done), then the guardrails against a comparable baseline. |
| **Guardrail** | A per-workload safety threshold checked after a change: latency, error rate, OOM restarts, availability, queue growth. |
| **Inverse change** | Putting back the recorded "before" values when verification fails or is inconclusive; at most one attempt. |
| **Escalated** | An operation the executor could not resolve safely; a person must look. |
| **Outbox / inbox** | Tables for publishing events in the same transaction as the data (outbox) and ignoring duplicates on the consumer side (inbox). |
| **Audit chain** | The append-only `audit.events` table where each event's hash includes the previous event's hash. |
| **Audit checkpoint** | The chain head signed with a host-held Ed25519 key, so even a database superuser cannot silently rewrite history. |

## Experiments and evaluation

| Term | Meaning |
|---|---|
| **Scenario** | A YAML file describing an experiment: targets, phases, actions, safety limits, seed. See [workloads-and-experiments.md](workloads-and-experiments.md). |
| **Phase** | A timed part of a scenario: `warmup` and `cooldown` (excluded from measurement) around one or more measured phases. |
| **Demand plan** | The full arrival schedule computed before a run; same scenario + seed → same plan digest. |
| **Capability token** | A short-lived ES256 JWT that lets one experiment run generate load against specific workloads. |
| **Load driver** | The short-lived Job that sends the planned requests to a workload. |
| **Run quality** | The classification of a finished run: `passed_pending_evaluation` or `invalid`. |
| **Watchdog** | The host process that measures free memory, publishes a host-safety signal and can abort experiments. |
| **B0 / B1 / B2 / B3** | Evaluation arms: static configuration; rules/VPA recommendation; forecast-aware deterministic optimization; B2 plus AI review. See [evaluation.md](evaluation.md). |
| **Release gate (RG01–RG06)** | One of six release checks computed from recorded evidence. See [release-and-upgrade.md](release-and-upgrade.md). |

## Security and infrastructure

| Term | Meaning |
|---|---|
| **OIDC** | OpenID Connect: login on top of OAuth 2.0; Keycloak issues the tokens. |
| **PKCE** | Proof Key for Code Exchange: protects the browser login of a public client (`capacityctl`) against code interception. |
| **`azp`** | "Authorized party": the client a token was issued to; the API accepts only allowed clients. |
| **mTLS** | Mutual TLS: both sides present certificates. Required on the governance write port. |
| **Root CA / intermediate CA** | The lab's certificate authorities; the root stays on the host, the name-constrained intermediate signs service certificates. |
| **Name constraints** | A CA certificate extension limiting which names the CA may sign. |
| **SKI / AKI** | Subject/Authority Key Identifier extensions that link certificates in a chain; required by Python 3.13's strict verification. |
| **DPAPI** | Windows Data Protection API: encrypts a file so only the same Windows user on the same machine can decrypt it. |
| **Vault / ESO** | HashiCorp Vault stores secrets; External Secrets Operator copies them into Kubernetes Secrets per namespace. |
| **Unseal** | Giving Vault its key after a restart so it can decrypt its data. |
| **RLS** | PostgreSQL row-level security: limits which rows a role can see or change. |
| **`SKIP LOCKED`** | A PostgreSQL locking option that lets several workers take different queue rows without blocking each other. |
| **pgvector / RRF** | pgvector stores embeddings in PostgreSQL; Reciprocal Rank Fusion combines full-text and vector rankings. |
| **Ollama / model gateway** | Ollama runs the local model on the host; the gateway is the authenticated TLS front door the `ai` Pod must use. |
