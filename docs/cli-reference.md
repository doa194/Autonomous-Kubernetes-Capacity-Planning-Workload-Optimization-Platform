# Command-line reference

The platform has four command-line tools for people, plus one entry point used inside containers.
All are installed into `.venv\Scripts\` by `uv sync --all-extras`. Every command accepts `--help`.

| Tool | Who uses it | Identity |
|---|---|---|
| [`capacity-lab`](#capacity-lab) | the lab owner: bootstrap, verify, backup, audit, model, release | kind admin context (bootstrap), DPAPI-protected host keys |
| [`capacityctl`](#capacityctl) | people operating the platform | their own Keycloak tokens (PKCE) |
| [`capacity-scenario`](#capacity-scenario) | whoever runs load experiments | `capacity-lab-runner` context |
| [`capacity-watchdog`](#capacity-watchdog) | the host safety process | `capacity-lab-watchdog` context |
| [`capacity-service`](#capacity-service) | the platform containers | the Pod's ServiceAccount |

---

## capacity-lab

### Lifecycle

| Command | What it does |
|---|---|
| `capacity-lab preflight` | prints the host checks (Docker memory and CPUs, free disk, pinned kind, kubectl/helm/docker, free host memory) as JSON |
| `capacity-lab bootstrap` | runs the 17 stages in order, skipping those already recorded |
| `capacity-lab bootstrap --only <stage>` | runs exactly one stage |
| `capacity-lab bootstrap --from-stage <stage>` | resumes from a stage and runs the rest |
| `capacity-lab bootstrap --force` | re-runs stages that are already recorded |
| `capacity-lab status` | prints the bootstrap journal (completed stages and durations) |
| `capacity-lab resume` | after a Docker or host restart: waits for nodes, re-renders dynamic NetworkPolicies if addresses changed, restarts dependent and crash-looping Pods, unseals Vault |
| `capacity-lab unseal` | only unseals Vault |
| `capacity-lab destroy` | dry run: reports what would be deleted |
| `capacity-lab destroy --confirm capacity-lab --no-dry-run` | deletes the cluster — refuses unless a backup has passed its restore check |

Stages: `preflight`, `registry`, `cluster`, `calico`, `base`, `network-dynamic`, `verify-network`,
`pki`, `secrets`, `database`, `identity`, `observability`, `images`, `platform`, `workloads`,
`grants`, `host-identities` (described in [setup-guide.md](setup-guide.md#4-bootstrap)).

### Verification

| Command | What it does |
|---|---|
| `capacity-lab verify all` | all seven live checks; evidence `verify-all-<time>.json`; non-zero exit on any failure |
| `capacity-lab verify networking` | default-deny probes |
| `capacity-lab verify security` | RBAC matrix, admission negative cases, executor field guard |
| `capacity-lab verify observability` | every scrape target up, six platform targets |
| `capacity-lab verify scheduling` | node pools and the six-Deployment topology |

### Credentials

| Command | What it does |
|---|---|
| `capacity-lab credentials <user>` | prints one lab test user's password (`planner1`, `approver1`, `operator1`, `viewer1`, `admin1`) — the only command that displays a credential; never logged |

### Backup and audit

| Command | What it does |
|---|---|
| `capacity-lab backup create` | records the audit head, dumps the database and roles, encrypts with AES-256-GCM, writes a manifest |
| `capacity-lab backup verify [path]` | restore drill of the latest (or given) backup into a network-less throwaway PostgreSQL; writes `restore-verification.json` |
| `capacity-lab audit checkpoint` | signs the current audit-chain head with the host Ed25519 key |
| `capacity-lab audit verify` | recomputes the chain and checks every signed checkpoint |

### Local model

| Command | What it does |
|---|---|
| `capacity-lab model gateway` | starts the protected TLS gateway in front of Ollama on `127.0.0.1:11500` (foreground) |
| `capacity-lab model qualify [--samples 6]` | measures the pinned models through the gateway; marks the profile qualified only if every gate passes |

### Release

| Command | What it does |
|---|---|
| `capacity-lab release test` | runs every test suite and keeps junit results in `~/.capacity-lab/evidence/tests/` |
| `capacity-lab release test --suite unit --suite security …` | runs selected suites (`unit`, `security`, `component`, `integration`, `operational`, `e2e`, `dotnet`) |
| `capacity-lab release qualify` | computes the six release gates from recorded evidence (never runs experiments itself) |

---

## capacityctl

Uses `https://localhost:8443` and the issuer `https://localhost:8444/realms/capacity` by default
(override with `CAPACITY_API_URL`, `CAPACITY_OIDC_ISSUER`, `CAPACITY_CA_BUNDLE`,
`CAPACITYCTL_CALLBACK_PORT`; see [configuration-reference.md](configuration-reference.md)).

| Command | Role | What it does |
|---|---|---|
| `capacityctl auth login` | any | browser login (authorization code + PKCE); tokens stored with DPAPI |
| `capacityctl auth status` | any | who you are, roles and token expiry |
| `capacityctl auth logout` | any | deletes the stored tokens |
| `capacityctl capabilities` | any | operating mode and revision, inventory freshness, capability status |
| `capacityctl workloads` | any | demo workloads you are granted, with configured requests/limits |
| `capacityctl nodes` | any | nodes with allocatable and requested resources |
| `capacityctl analysis create <workload> [--horizon 1h\|24h] [--advisory-only]` | Analyst, Planner | starts an analysis job (`--advisory-only` marks it for advisory use, not governed execution) |
| `capacityctl operation <operation-id> [--watch]` | any | job state and result; `--watch` polls until finished |
| `capacityctl analysis show <analysis-digest>` | any | the full analysis: gates, refusals, evaluations |
| `capacityctl advisory <workload> [--mode diagnostic\|optimization_review] [--analysis <digest>] [--evaluation <digest>]` | Analyst, Planner (mode Advisory/Governed) | starts an AI advisory job |
| `capacityctl mode <Observe\|Advisory\|Governed> --expected-revision <n>` | Administrator | changes the operating mode |
| `capacityctl plans create <evaluation-digest> [--advisory-artifact <digest>]` | Planner | creates a plan revision from a qualified evaluation |
| `capacityctl plans show <plan-id> [--revision <n>]` | any | plan, state, approval, revocation |
| `capacityctl plans approve <plan-id> --revision <n> --digest <planDigest> [--reject] [--yes]` | Approver (not the author) | approves (or rejects) the exact revision and digest after showing the changes |
| `capacityctl plans revoke <plan-id> --revision <n> --reason <text>` | Approver | withdraws an approval before execution |
| `capacityctl plans execution-request <plan-id> --revision <n>` | Executor | asks the platform to execute an approved revision |
| `capacityctl execution <intent-id>` | any | operation state, transitions and verification outcome |

Typical flow: [operator-guide.md](operator-guide.md).

---

## capacity-scenario

| Command | What it does |
|---|---|
| `capacity-scenario validate <path>` | structural and semantic validation; no cluster access |
| `capacity-scenario plan <path> [--seed <n>]` | prints the deterministic demand plan summary, its digest and a side-effect preview; writes nothing |
| `capacity-scenario run <path> [--seed <n>] --driver-image <ref>` | runs the experiment: preflight, lease, driver Job, phases, collection, cleanup by UID, sealed evidence |
| `capacity-scenario status <run-id>` | run state, outcome, transitions |
| `capacity-scenario abort <run-id> [--reason operator_abort]` | requests an abort; the runner stops the phases and cleans up |
| `capacity-scenario recover [--driver-image <ref>]` | reconciles runs interrupted by a runner crash: cleans owned objects by UID, never resumes |

`--driver-image` can also be given as the `DRIVER_IMAGE` environment variable; it must be the
platform image digest from `~/.capacity-lab/release/images.json`:

```powershell
$env:DRIVER_IMAGE = (Get-Content $HOME\.capacity-lab\release\images.json | ConvertFrom-Json).platform
```

```powershell
capacity-scenario run configs/scenarios/e04-steady-demand.yaml
```

Details: [workloads-and-experiments.md](workloads-and-experiments.md).

---

## capacity-watchdog

| Command | What it does |
|---|---|
| `capacity-watchdog run [--interval 10] [--publish-signal\|--no-publish-signal]` | measures Windows free memory and kind node memory every interval, heartbeats the journal, publishes the `host-safety` ConfigMap, asks open runs to abort below 1.5 GiB |
| `capacity-watchdog once` | prints one measurement (used by preflight and tests) |

---

## capacity-service

`capacity-service <role>` starts one platform service (`api`, `observer`, `capacity`, `governance`,
`ai`, `executor`). It is the container entry point; it is not meant to be run on the host except for
debugging with the environment described in [configuration-reference.md](configuration-reference.md).
