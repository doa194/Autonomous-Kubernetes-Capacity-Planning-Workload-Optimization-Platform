# Troubleshooting

Problems an operator can meet on the reference Windows host, each with the **symptom**, the **cause**
and the **fix**. Every entry below was met on the reference host while the lab was built and run.

**First steps for any problem:**

```powershell
capacity-lab status
```

```powershell
capacity-lab verify all
```

```powershell
kubectl --context kind-capacity-lab get pods -A
```

Then read the logs of the affected Pod (`kubectl … logs deploy/<name>`), find the correlation ID in
the error body, and follow it across services ([observability.md](observability.md#5-logs)).

---

## 1. Host and cluster

### API server restarts; etcd reports "apply request took too long"
**Symptom:** `kubectl` returns `Unable to connect to the server: EOF`, `Forbidden` for the admin
user, or `/readyz` fails with `rbac/bootstrap-roles failed`; Pods show
`FailedMount … timed out waiting for the condition`.
**Cause:** the disk holding Docker Desktop's data is saturated. etcd needs low-latency `fsync`; when
it is starved the API server fails its health checks. On the reference host Docker's data disk
(`docker_data.vhdx`) is on a 7200 rpm hard disk, which measured about 100 ms average latency with a
queue of 10 while the lab, image builds and model loading ran together.
**Check:**
```powershell
Get-PhysicalDisk | Select-Object FriendlyName, MediaType
```
```powershell
Get-Counter '\PhysicalDisk(*)\Avg. Disk sec/Transfer'
```
**Fix:** put Docker Desktop's disk image on an SSD (Docker Desktop → Settings → Resources → Advanced →
Disk image location). Otherwise avoid building images or loading models while experiments run, and
wait for the load to drop. Excluding the project, Docker data and model folders from Windows Search
indexing also helps. These are host settings the lab never changes for you.

### After a Docker or host restart: Kubernetes API clients time out, Vault is sealed
**Symptom:** observer, executor, Prometheus or kube-state-metrics log
`dial tcp 10.96.0.1:443: i/o timeout`; External Secrets reports errors; Pods sit in CrashLoopBackOff.
**Cause:** Docker can give the kind node containers new IP addresses on restart (observed: the control
plane moved from 172.18.0.3 to 172.18.0.5). The NetworkPolicies that allow API-server access are
rendered for a specific address. Vault restarts sealed.
**Fix:**
```powershell
capacity-lab resume
```
It waits for the nodes, re-discovers the addresses, re-renders the policies, restarts the Pods that
depend on them and any Pod stuck in CrashLoopBackOff, and unseals Vault (restarting it first if it
does not answer). Telemetry for the downtime is missing, so forecasts refuse (`coverage_below_policy`)
until about six hours of continuous history exist again.

### Vault does not answer `vault status`
**Cause:** Vault occasionally hangs during start-up when the host is under heavy disk load.
**Fix:** `capacity-lab resume` restarts it automatically; by hand:
```powershell
kubectl --context kind-capacity-lab -n capacity-system delete pod vault-0
```
```powershell
capacity-lab unseal
```

### `kubectl get --raw /readyz` says "the server could not find the requested resource" in Git Bash
**Cause:** Git Bash rewrites arguments that start with `/` into Windows paths.
**Fix:** use PowerShell, or prefix the command with `MSYS_NO_PATHCONV=1`.

### Local ports stay occupied by old `kubectl port-forward` processes
**Cause:** on Windows `kubectl` is often a package-manager shim (for example Chocolatey) that starts the
real binary as a child process; stopping only the shim leaves the forward running. The lab tools stop
the whole process tree and use free ports, but forwards started by hand can linger.
**Fix:** list them and stop the ones you no longer need:
```powershell
Get-CimInstance Win32_Process -Filter "Name='kubectl.exe'" | Where-Object { $_.CommandLine -like '*port-forward*' }
```

### Grafana never becomes ready
**Symptom:** readiness probe `connection refused` on port 3000; the log stops during start-up
(storage migrations, "Installed APIs for app").
**Cause:** resource starvation. Grafana 13 needs more than 256 MiB / 500m during start-up (measured:
222 MiB working set and 88 % of CPU periods throttled at that size).
**Fix:** the lab gives it 192 Mi / 100m requests and 512 Mi / 1 CPU limits. If it still stalls,
delete the Pod once the host is responsive.

### A service reports `not_ready` with `database: down` although PostgreSQL is fine
**Cause:** readiness reflects the worker loop's last database round-trip; it clears on the next
successful claim, within seconds, even on an empty queue.
**Fix:** if it persists, the service cannot reach PostgreSQL — check the `capacity-data` Pod and the
NetworkPolicies (`capacity-lab verify networking`).

## 2. Certificates and TLS

### "a root or intermediate certificate is not authorized to sign for this name"
**Cause:** a certificate requested a name outside the intermediate CA's name constraints (only
`*.svc`, `*.svc.cluster.local`, `localhost`, `127.0.0.0/8`).
**Fix:** use the fully qualified Service name (for example `vault-0.vault-internal.capacity-system.svc`),
never short names.

### "Missing Authority Key Identifier" / "CA cert does not include key usage extension"
**Cause:** Python 3.13 verifies certificates strictly; every CA certificate in the chain needs
Subject/Authority Key Identifiers and correct key usage. The lab PKI issues them.
**Fix:** if a CA was created by other means, re-run `capacity-lab bootstrap --only pki`, delete the
service certificate Secrets so cert-manager re-issues them, and restart the Pods that serve TLS.

### `curl` on Windows: "the revocation status is unknown"
**Cause:** the Windows TLS stack (schannel) checks revocation, and the lab CA publishes no revocation
list.
**Fix:** `curl.exe --ssl-no-revoke --cacert $HOME\.capacity-lab\pki\root-ca.crt …`, or Python/httpx
with the root CA.

### Plan, approval, execution or mode calls return `503 governance_unavailable`
**Symptom:** `capacityctl plans …` or `capacityctl mode …` fails with `governance_unavailable`, while
read-only calls (`capabilities`, `workloads`, analyses) work.
**Cause:** the API relays these calls to governance's mutual-TLS listener (8444). The relay fails when
the API's client certificate is missing or unreadable, when governance is down, or when the
`api-egress` / `governance-from-api` policies do not match.
**Check:**
```powershell
kubectl --context kind-capacity-lab -n capacity-core get pods -l app.kubernetes.io/name=capacity-governance
```
```powershell
kubectl --context kind-capacity-lab get certificate -A
```
**Fix:** let cert-manager re-issue a missing certificate (delete its Secret), restart the `api` Pod if
its certificate files were replaced, and run `capacity-lab verify networking`.
`tests/security/test_internal_mtls.py` proves the relay with a real mutual-TLS handshake.

## 3. Admission and resources

### A Pod without resource limits is accepted, or gets limits it never asked for
**Cause:** a `LimitRange` item of `type: Container` with `max` or `min` makes Kubernetes fill in
`default` and `defaultRequest` and inject them into containers that omit them, before the admission
policies run.
**Fix:** the lab's LimitRanges use `type: Pod`, which bounds Pods without defaulting. A live object
must show no `default` or `defaultRequest`:
```powershell
kubectl --context kind-capacity-lab -n workloads-batch get limitrange bounds -o yaml
```
`capacity-lab verify security` includes a dry-run Pod without limits that must be denied.

## 4. Identity

### Every API call returns 401 although a token was issued
**Check:** decode the access token (without verifying) and look for `sub`, `azp`, `aud` and
`realm_access.roles`. All four must be present; `aud` must contain `capacity-api` and `azp` must be
`capacityctl` (or `capacity-e2e` for the automated tests).
**Cause:** the realm or client scopes were changed by hand. The generated realm declares the `basic`
(`sub`) and `roles` (`realm_access.roles`) client scopes explicitly, because a realm import with a
`clientScopes` list does not create Keycloak's built-in ones.
**Fix:** re-run the `secrets` stage to store the generated realm, delete the `capacity` realm in the
Keycloak admin console, restart Keycloak so it re-imports the realm, then
`capacity-lab bootstrap --only grants` (users get new IDs; grants for the old IDs are revoked).

### A long-running script starts getting 401 after about 10 minutes
**Cause:** access tokens live 10 minutes. A script that fetched a token once keeps sending it after it
expired.
**Fix:** fetch a token per call or refresh it (`capacityctl` refreshes automatically; the live test
helper `tests/live.py::token()` caches tokens until a minute before expiry).

### `capacity-lab bootstrap --only grants` times out
**Cause:** Keycloak's admin API answers slowly while the host is loaded.
**Fix:** re-run the stage; it is idempotent.

## 5. Analyses and changes

### No candidate qualifies; forecasts refuse
**Check:** `capacityctl analysis show <digest>` — look at `forecast.support`, `memory.oom_spike` and
the forecast reason codes (`capacityctl` or `GET /api/v1/forecasts/{id}`).

| Reason | Cause | Fix |
|---|---|---|
| `coverage_below_policy`, `insufficient_history` | less than ~6 h of continuous telemetry | keep the workload under steady load (E04 for W04) and wait |
| `unclassified_quality_issue` with source flag `multiple_series_combined` | several Pods in the 6-hour window after a rollout | wait about six hours after the last rollout of the workload |
| `memory_tail_unqualified` (gate) | a gap in the memory series (`scrape_gap`) | wait until the gap leaves the 6-hour window |
| `queue_evidence_unknown` (gate) on a W04 CPU candidate | completion-rate evidence not derived | known limitation; memory candidates are unaffected |

### Plan created as `AdvisoryOnly`
**Cause:** the mode was not `Governed` when the plan was created, or the workload needs an advisory
review (W01).
**Fix:** switch to `Governed` first, then create a new plan.

### Execution `refused`
**Check:** `capacityctl execution <intent-id>` — the transition reason names the failed preflight check
(`host_pressure`, `before_image_mismatch`, `intent_expired`, …; full list in
[governance-and-execution.md](governance-and-execution.md#3-the-executor-s25)).
**Common cause:** the watchdog is not running, so the host-safety signal is stale (`host_pressure`).
Start `capacity-watchdog run` and request execution again with a fresh approval if it expired.

### Verification ends `verification_inconclusive` and the change is reversed
**Check:** the outcome's `safetyGuardrails` and `benefitReason`, and the executor log for
`verification query … failed`.
**Causes:** load not comparable (`incomparable_load` — the request rate changed by more than 20 %);
a guardrail query failed (missing data); coverage below 0.9 in the post-window.
**Fix:** run the change while the workload is under steady load (start about 20 minutes into an E04
run), and check telemetry coverage before planning.

## 6. Experiments

### Run rejected with "another run is still open (serial execution required)"
**Cause:** a different run is still open (only light runs may overlap).
**Fix:** wait for it, abort it with `capacity-scenario abort <run-id>`, or run
`capacity-scenario recover` if its runner died.

### Run rejected with "watchdog state pressure/abort/dead"
**Cause:** free host memory is below 2 GiB (pressure) or 1.5 GiB (abort), or the watchdog is not
running. This is the safety system working (observed on 2026-11-21 at 04:36).
**Fix:** start `capacity-watchdog run`, close memory-heavy applications, then retry.

### Run aborted with `driver_not_started`
**Cause:** the load-driver Pod did not reach `Running` within 5 minutes — usually the first pull of the
platform image on an experiment node, on a slow disk.
**Fix:** retry; the image is cached afterwards.

### Run classified `invalid`
Read `run-quality.json` in the run's evidence folder. Typical causes: the workload refused requests
(`401` when capabilities do not match, `429`/`409` when the scenario exceeds the workload's own
bounds) or arrivals were dropped. An invalid run is never used as evidence.

## 7. AI layer

### Model qualification: generation calls end with `ReadTimeout`
**Cause:** the model could not be loaded and run within the 180 s profile timeout — typically because
the disk holding the models is saturated or memory is short.
**Fix:** re-run `capacity-lab model qualify` when the host is idle. The report shows the warm-up (model
load) separately.

### Advisory says `unavailable`
Check, in order: the inference profile has `qualified: true` (otherwise the reason is
`model_unqualified`, the intended degraded mode); `capacity-lab model gateway` is running; Ollama is
running on `127.0.0.1:11434`; the `ai` Pod can reach `https://host.docker.internal:11500/healthz`.

### The operational gateway test is skipped
**Cause:** the host model gateway is not running, so `test_model_gateway_is_reachable_from_ai_but_not_from_demo_pods`
skips (it never passes without the gateway).
**Fix:** start `capacity-lab model gateway` before `capacity-lab release test --suite operational`.
