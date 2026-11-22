# Design decisions

Each entry records **what was chosen**, **why**, the **alternatives** considered, and the
**trade-offs** accepted. The accepted specifications in `specs/accepted/` are the source of these
decisions; [spec-traceability.md](spec-traceability.md) maps them to code and lists deviations.

| ID | Decision | Area |
|---|---|---|
| [DD-01](#dd-01-six-small-services-one-image) | six small services, one image | architecture |
| [DD-02](#dd-02-postgresql-jobs-and-outbox-instead-of-a-message-broker) | PostgreSQL jobs and outbox instead of a broker | architecture |
| [DD-03](#dd-03-deterministic-numbers-optional-ai) | deterministic numbers, optional AI | decision logic |
| [DD-04](#dd-04-small-explainable-forecasting-models-with-refusals) | small explainable forecasting models with refusals | decision logic |
| [DD-05](#dd-05-a-fixed-versioned-telemetry-query-registry) | a fixed, versioned telemetry query registry | decision logic |
| [DD-06](#dd-06-hard-gates-before-scoring-and-unknown-is-not-a-pass) | hard gates before scoring; unknown is not a pass | decision logic |
| [DD-07](#dd-07-immutable-plans-and-exact-digest-approval-by-another-person) | immutable plans, exact-digest approval by another person | governance |
| [DD-08](#dd-08-a-reconcile-first-executor-with-fencing-and-test-preconditions) | reconcile-first executor with fencing and `test` preconditions | execution |
| [DD-09](#dd-09-verification-against-a-comparable-baseline-with-one-exact-inverse) | verification against a comparable baseline, one exact inverse | execution |
| [DD-10](#dd-10-defence-in-depth-in-kubernetes-for-the-executor) | defence in depth in Kubernetes for the executor | security |
| [DD-11](#dd-11-production-like-identity-secrets-and-pki-locally) | production-like identity, secrets and PKI, locally | security |
| [DD-12](#dd-12-selective-mtls) | selective mTLS | security |
| [DD-13](#dd-13-refusals-audited-outside-the-refused-transaction) | refusals audited outside the refused transaction | security |
| [DD-14](#dd-14-kind-with-calico-and-proven-enforcement) | kind with Calico and proven enforcement | infrastructure |
| [DD-15](#dd-15-network-policies-rendered-from-live-discovery) | network policies rendered from live discovery | infrastructure |
| [DD-16](#dd-16-limitranges-that-bound-but-never-default) | LimitRanges that bound but never default | infrastructure |
| [DD-17](#dd-17-purpose-built-mixed-language-demo-workloads) | purpose-built, mixed-language demo workloads | experiments |
| [DD-18](#dd-18-experiments-driven-from-the-host-with-an-independent-watchdog) | host-driven experiments with an independent watchdog | experiments |
| [DD-19](#dd-19-honest-evaluation-and-computed-release-gates) | honest evaluation and computed release gates | release |
| [DD-20](#dd-20-digest-pinned-everything) | digest-pinned everything | release |

---

## DD-01 Six small services, one image

**Chosen:** six Deployments (`api`, `observer`, `capacity`, `governance`, `ai`, `executor`) built
from one Python image and started with `capacity-service <role>`.

**Why:** each service gets its own identity, database role, network policy and failure domain.
That is what makes "only the executor can change the cluster" and "the AI can read but never
write" enforceable by the platform rather than by convention. One image keeps the build, pinning
and supply-chain story simple on a single workstation.

**Alternatives:**
* a single monolith — simpler, but one compromised process would hold every privilege;
* one image per service — cleaner dependency separation, but six builds and six digests to pin for
  no security gain, because privileges come from identity, not from the image.

**Trade-offs:** every service ships code it does not run. `tests/unit/test_architecture.py` parses
imports so AI, API, capacity and governance code can never import executor code or the Kubernetes
client, and checks that exactly six platform Deployments exist.

## DD-02 PostgreSQL jobs and outbox instead of a message broker

**Chosen:** a `workflow.jobs` table claimed with `FOR UPDATE SKIP LOCKED`, leases, fencing epochs
and poison-job parking, plus a transactional outbox per schema and an inbox for deduplication.

**Why:** the platform already needs PostgreSQL. Jobs and their results commit atomically, there is
one less stateful system to secure and back up, and it fits a laptop's memory budget. Row-level
security gives per-service queue isolation for free.

**Alternatives:** Kafka, RabbitMQ or NATS — better throughput and fan-out, but extra memory, extra
credentials and a second consistency boundary between "result stored" and "event published".

**Trade-offs:** workers poll, so latency from enqueue to claim is seconds, and throughput is far
below a broker. That suits capacity analysis (one job takes seconds to minutes) but not high-rate
event streams. See [workflow-and-jobs.md](workflow-and-jobs.md).

## DD-03 Deterministic numbers, optional AI

**Chosen:** every resource number comes from deterministic code (S14 forecasting, S19–S22
optimization). The AI layer may only describe and critique evidence. Its output schema has no
fields for resource values, approval or execution, and a trusted validator rejects any number not
present in the cited evidence.

**Why:** reproducibility and safety. A forecast or candidate can be recomputed from its inputs and
digests; a language model's output cannot.

**Alternatives:** LLM-generated recommendations — flexible, but neither reproducible nor verifiable.

**Trade-offs:** the AI adds explanation and risk review, not extra savings. When the model is
unavailable or unqualified, advisories end `unavailable` and everything else keeps working — which
is the state on the reference host (see [ai-advisory.md](ai-advisory.md)).

## DD-04 Small, explainable forecasting models with refusals

**Chosen:** a fixed portfolio (persistence, rolling median, simple exponential smoothing, damped
Holt, seasonal naive for 24 h) selected by walk-forward backtesting. Intervals come from empirical
residual quantiles of earlier folds and are tested on later folds. Typed refusals are returned when
history is short, sparse, censored or of unknown quality.

**Why:** a single host cannot train or serve heavy models. Simple models with honest uncertainty
and explicit refusals are safer inputs for resource decisions than an accurate-looking number with
no error bars.

**Alternatives:** Prophet, ARIMA or machine-learning models — potentially more accurate, but
heavier, slower and much harder to validate locally.

**Trade-offs:** a 1-hour forecast needs at least 4 hours of history with ≥ 95 % coverage inside a
6-hour lookback, so a fresh lab refuses for hours. See [forecasting.md](forecasting.md).

## DD-05 A fixed, versioned telemetry query registry

**Chosen:** the engine may run only the nine named queries in `capacity/telemetry/registry.py`.
Each has a revision, a unit, a statistic, a minimum step, a maximum range and a series budget, and
label values are inserted only after a safe-character check. A changed meaning gets a new name or
revision; an existing query is never edited in place.

**Why:** free-form PromQL would make evidence irreproducible and would let a caller (including the
AI) probe arbitrary metrics. Fixed queries also let the quality of the data (coverage, gaps,
combined series) travel with every result.

**Alternatives:** letting each component write PromQL, or a generic metrics proxy.

**Trade-offs:** adding a signal needs a code change and a new revision. Pods are selected by
**name prefix** (`pod=~"cpu-worker-.*"`); because a name-only join could mix unrelated Pods, a
window that contains several Pod series is flagged `multiple_series_combined` and forecasts refuse
it. A Pod-UID lineage check that would accept such windows safely is not implemented, so forecasts
pause for about six hours after any rollout. See [telemetry.md](telemetry.md).

## DD-06 Hard gates before scoring, and unknown is not a pass

**Chosen:** a tighten-only hierarchical policy; 13 gates in four phases returning `pass`, `fail`,
`unknown` or `not_applicable`; candidate generation on a bounded grid; node-fit and surge
simulation; Pareto and weighted ranking **only** among candidates that passed every gate.

**Why:** a high score must never hide a broken safety rule, and missing evidence must never be read
as good evidence.

**Alternatives:** a single weighted score with penalties — easier to tune, but a large efficiency
gain could outweigh a safety violation.

**Trade-offs:** conservative. Genuinely safe savings are refused when evidence is missing — for
example every W04 CPU decrease today, because the queue-completion evidence is not derived. See
[optimization-engine.md](optimization-engine.md).

## DD-07 Immutable plans and exact-digest approval by another person

**Chosen:** plans are immutable revisions identified by a SHA-256 digest of canonical JSON.
Approval binds to that exact revision and digest, is valid for 15 minutes, is accepted only in
`Governed` mode, and must come from a different person than the author. The last rule is enforced
in code **and** by a database `CHECK` constraint.

**Why:** what was approved is exactly what can run, and nobody can approve their own change — even
if a bug slipped into the service.

**Alternatives:** approving a workload or a recommendation type instead of an exact change;
allowing self-approval for low-risk changes.

**Trade-offs:** any edit needs a new revision and a new approval; short TTLs mean an approval can
expire before execution and must be repeated.

## DD-08 A reconcile-first executor with fencing and `test` preconditions

**Chosen:** the executor records its intent before writing, takes a per-target lock with a fencing
epoch, applies a JSON Patch whose `test` operations check UID, resourceVersion, container name and
the planned before-values, and after any uncertainty reads the live object before deciding
anything (`unknown` → `awaiting_revalidation`).

**Why:** Kubernetes writes can time out *after* succeeding, and someone can edit a Deployment
between planning and execution. Blind retries could double-apply or overwrite another change.

**Alternatives:** a simple apply-and-retry loop, server-side apply with field ownership, or a GitOps
pull request.

**Trade-offs:** more states and more code than an apply loop. GitOps-owned targets are refused
(the GitOps adapter is a P1 feature that is not installed).

## DD-09 Verification against a comparable baseline, with one exact inverse

**Chosen:** two-level verification. First the configuration: exact values, rollout complete within
300 s. Then, after 120 s of stabilization, the guardrails over 900 s compared with the 900 s before
the change, with load comparable within 20 %. If verification **fails or is inconclusive**, the
executor applies one exact inverse using the authority already granted; if that is not possible
safely, it escalates.

**Why:** a change that cannot be shown to be safe should not stay in place silently, and recovery
must never loop.

**Alternatives:** leave inconclusive changes in place and alert; retry verification until it
passes; unlimited rollback attempts.

**Trade-offs:** an inconclusive result undoes a change that may have been fine — as happened in the
live run, where a verifier defect made a guardrail unreadable. Each guardrail query is now issued at
its registered minimum step, and a failing query is logged so an `unknown` can be traced. See
[governance-and-execution.md](governance-and-execution.md).

## DD-10 Defence in depth in Kubernetes for the executor

**Chosen:** besides the platform's own checks, Kubernetes enforces two more layers. RBAC lets
`capacity-executor-sa` patch Deployments only in the three demo namespaces. The
`capacity-executor-field-guard` ValidatingAdmissionPolicy rejects any executor update that changes
replicas, labels, template metadata, container identity, image, command, env, ServiceAccount or
scheduling — leaving only CPU/memory requests and limits.

**Why:** even a fully compromised executor process cannot do more than resize demo containers.

**Alternatives:** trusting the executor's code; a mutating webhook.

**Trade-offs:** the policy has to be kept in step with the fields the executor may change;
`capacity-lab verify` proves it live (resources patch allowed; replicas, image and ServiceAccount
changes denied).

## DD-11 Production-like identity, secrets and PKI, locally

**Chosen:** Keycloak OIDC with PKCE for people; projected ServiceAccount tokens for services; Vault
plus External Secrets Operator with per-namespace policies for credentials; an offline root CA on
the host (key protected with Windows DPAPI) and a name-constrained intermediate in cert-manager.

**Why:** the security behaviour (scopes, audiences, rotation, least privilege) is what the project
demonstrates. Shortcuts like static passwords or a self-signed cert per service would hide exactly
those properties.

**Alternatives:** static API keys, Kubernetes Secrets created by hand, mkcert.

**Trade-offs:** more moving parts to bootstrap and to recover after a restart (Vault unseal). The
lab keeps a single unseal share and host-held keys; production replacements are listed in
[production-considerations.md](production-considerations.md).

## DD-12 Selective mTLS

**Chosen:** all internal traffic is TLS; **mutual** TLS is required only on the governance write
listener (8444) that the API uses to relay plan actions and mode changes.

**Why:** that is the one internal call that changes authority-bearing state. A service mesh would
add sidecars and memory the host does not have.

**Alternatives:** a service mesh (Istio, Linkerd) with mTLS everywhere; no mTLS at all.

**Trade-offs:** other internal calls rely on TLS plus a verified ServiceAccount token rather than a
client certificate. The relay builds its TLS context on every call so renewed certificates are
used without a restart; `tests/security/test_internal_mtls.py` performs a real mutual-TLS handshake.

## DD-13 Refusals audited outside the refused transaction

**Chosen:** each governance command runs inside a wrapper that sits *outside* its database
transaction. When the command is refused (authorization, state or digest), the transaction rolls
back first, then a `deny` audit record with the precise internal reason is written on a fresh
connection. The caller still sees only the generic denial.

**Why:** a denial written inside the refused transaction is erased by its own rollback — which is
exactly what happened before this design. Writing after the rollback also avoids waiting on the
audit-chain lock the failed transaction may have held.

**Alternatives:** committing the denial in a savepoint; logging denials only to stdout.

**Trade-offs:** if the audit write itself fails, the refusal still stands and an error is logged
(an alert, never an allow).

## DD-14 kind with Calico and proven enforcement

**Chosen:** kind with its default CNI disabled and Calico v3.33.1 installed; the bootstrap stage
`verify-network` starts probe Pods and **fails the bootstrap** unless denied flows really time out.

**Why:** kind's default CNI accepts NetworkPolicy objects but does not enforce them. A platform that
claims default-deny must prove it.

**Alternatives:** minikube or k3d; Cilium.

**Trade-offs:** Calico's memory and start-up time on a small host; the probes add a few minutes to
bootstrap and verification.

## DD-15 Network policies rendered from live discovery

**Chosen:** the `network-dynamic` stage discovers the control-plane IP, the `kubernetes` Service
address and the Docker host gateway, stores them in `~/.capacity-lab/release/network.json`, and
renders policies labelled `capacity.lab/rendered=network-dynamic`. `capacity-lab resume`
re-renders them when addresses change.

**Why:** some addresses only exist after the cluster is created, and Docker can reassign node IPs on
restart (observed: the control plane moved from 172.18.0.3 to 172.18.0.5).

**Alternatives:** allowing whole subnets; hard-coding addresses.

**Trade-offs:** after a Docker restart, API-server access is blocked until `resume` runs.

## DD-16 LimitRanges that bound but never default

**Chosen:** LimitRanges use `type: Pod` with `min`/`max` only.

**Why:** a `type: Container` item with `max`/`min` makes Kubernetes inject `default` and
`defaultRequest` into containers that omit them, before admission runs — silently changing
experiment evidence and hiding missing limits from the `capacity-pod-hardening` policy.

**Trade-offs:** bounds are per Pod; all demo and driver Pods have one container, so this is
equivalent here.

## DD-17 Purpose-built, mixed-language demo workloads

**Chosen:** six small workloads — two ASP.NET Core APIs (.NET 10) and four Python programs — each
designed to exercise one decision path (latency SLO, bursts, memory spikes, CPU queue, batch runs,
opportunistic scheduling), each requiring a signed experiment capability before it accepts load.

**Why:** real applications would make results noisy and unrepeatable; purpose-built workloads with
bounded, seeded behaviour make each gate observable.

**Trade-offs:** demo behaviour is simpler than production traffic, so no result here is a claim
about real applications. See [workloads-and-experiments.md](workloads-and-experiments.md).

## DD-18 Experiments driven from the host, with an independent watchdog

**Chosen:** the scenario runner runs on the host with its own narrow kubeconfig context and a
SQLite journal; a separate watchdog process measures Windows free memory every 10 s and can abort
runs and block changes.

**Why:** the component most likely to fail during a load experiment is the host itself. A watchdog
outside the cluster still works when the cluster is struggling. (It did: at 04:36 on 2026-11-21 it
aborted an E04 run when free memory fell below 1.5 GiB.)

**Alternatives:** an in-cluster operator for experiments; Kubernetes resource limits alone.

**Trade-offs:** host-side state (journal, keys, evidence) must be backed up separately from the
database.

## DD-19 Honest evaluation and computed release gates

**Chosen:** the release report is computed by pure functions from recorded evidence. Anything not
run is `not_run`, never a pass. A hard failure blocks. A human reviewer can refuse a passing result
but can never override a failure.

**Why:** a lab project is tempting to over-claim. Computing the verdict makes that impossible by
construction. See [release-and-upgrade.md](release-and-upgrade.md) and [evaluation.md](evaluation.md).

**Trade-offs:** the honest outcome on the reference host is `NOT_QUALIFIED`, because comparisons,
the full rebuild drill and an upgrade rehearsal have not been run.

## DD-20 Digest-pinned everything

**Chosen:** third-party manifests and the kind binary are verified by SHA-256; Helm charts and images
are pinned to exact versions; platform and workload images are deployed by digest; Python and .NET
dependencies are locked; applied migrations have a checksum ledger; specification sources have a
digest manifest.

**Why:** the same inputs must produce the same lab, and a silent upstream change must stop the
bootstrap rather than alter it.

**Trade-offs:** upgrading anything is an explicit change to `infra/versions.lock.json` or a lock
file. Base images are pinned by tag, not digest, so rebuilding a workload image can produce a new
digest from unchanged sources.
