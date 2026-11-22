# Internal implementation work register

Internal tracking only — not product documentation (see `/docs` for that). Status vocabulary:
`verified-live` (exercised on the kind lab), `verified-tests` (automated tests pass),
`implemented` (code present, not exercised beyond static checks), `partial`, `not-run`,
`not-installed` (P1 / out of P0 scope), `blocked` (environment).

| Slice | Key requirements / invariants | Owning package | Status | Verification |
|---|---|---|---|---|
| S01 | Observe/Advisory/Governed; L0–L2 only; current/1h/conditional 24h | contracts, governance.authz | verified-tests, verified-live (mode reported, Governed-only actions) | test_governance; live capabilities |
| S02 | six deployables, jobs + outbox, no broker, digests | shared, all services | verified-tests, verified-live (6 Deployments running, analysis job round-trip) | test_architecture, test_schema_boundaries |
| S03 | 1 CP + 3 workers, tainted platform node, Calico default-deny, capacity-experiments addendum | infra, lab.cluster | verified-live | verify networking/scheduling evidence |
| S04 | W01–W06 baselines, capabilities, bounded intensity | workloads, contracts.workloads | verified-live (W01–W04 running, W05 CronJob, W06 template) | .NET tests; live deploy |
| S05 | Typer runner, seeds, journal, watchdog, UID cleanup, batch Jobs | scenarios, watchdog | verified-tests; live runs (see evidence/experiments) | test_scenarios; journal |
| S06 | OIDC PKCE, roles, grants, namespace scope, author≠approver | shared.auth, governance | verified-tests, verified-live (token → grants → 200/401) | test_api_boundaries; operational auth test |
| S07 | projected SA tokens, NetworkPolicy, model-gateway isolation | shared.auth.service_identity, infra | verified-live (network probes) | verify networking; gateway bypass test |
| S08 | Vault + ESO, offline root + constrained intermediate, PSA, CEL VAP, DPAPI | lab.secrets, lab.pki, admission.yaml | verified-live | admission_negatives, executor_field_guard |
| S09 | gateway auth, tool broker, untrusted framing, validators | ai | verified-tests; gateway guards verified-live | test_ai_tool_security; qualification report |
| S10 | pins, digests, locked deps | images, infra/versions.lock.json | partial (signature admission not-installed) | RG05 |
| S11 | inventory snapshots, effective requests, incarnation | observer | verified-tests, verified-live (fresh/complete snapshot) | test_accounting_and_rightsizing |
| S12 | query registry, buckets, coverage | capacity.telemetry, Prometheus | verified-live (analysis evidence refs) | observability check |
| S13 | seven schemas, roles, RLS, immutability, audit chain | db/migrations | verified-tests, verified-live (restore drill, audit verify) | integration suite |
| S14 | model portfolio, walk-forward, calibrated intervals, refusals | capacity.forecasting | verified-tests; verified-live refusal (coverage_below_policy) | test_forecasting |
| S15 | Ollama behind gateway, profile, qualification | ai.inference, ai.gateway, lab.model | partial: gateway verified-live; generation NOT qualified (timeouts under host load) | model-qualification report |
| S16 | MAF, three fixed agents, no tools in framework | ai.agents | verified-tests | security tests |
| S17 | fixed read-only tools, delegation re-check | ai.broker, capacity.tools | verified-tests | security tests |
| S18 | pgvector + FTS, RRF, redaction, authz prefilter | ai.knowledge | verified-tests (integration) | test_knowledge_retrieval |
| S19 | tighten-only policy, four-valued gates, oracle | capacity.policy | verified-tests | golden tests |
| S20 | bounded candidate grid, guards | capacity.rightsizing | verified-tests | golden + rightsizing tests |
| S21 | node fit, backtracking, fragmentation | capacity.placement | verified-tests | golden tests |
| S22 | Pareto + weighted ranking, deterministic tie-break | capacity.simulation | verified-tests | golden tests |
| S23 | HPA compatibility gate (P0), advisory (P1) | capacity.policy.gates | gate verified-tests; advisory not-installed | golden tests |
| S24 | immutable plans, exact-digest approval, TTLs, intents | governance | verified-tests; live E2E pending telemetry history | test_governance, integration chain, e2e |
| S25 | reconcile-first, test-op patches, fencing, field guard | executor | verified-tests; field guard verified-live; recovery after Pod loss in operational suite | test_executor, operational |
| S26 | two-level verification, bounded inverse | executor.verification | verified-tests; live pending | test_executor, e2e |
| S27 | FastAPI, capacityctl, async ops, error envelope | api, cli | verified-tests, verified-live (capabilities, scopes, workloads, analyses) | component tests |
| S28 | metrics, logs, health, observability stack | shared.web, lab.infra_manifests | partial (HTTP metrics only, no dashboards/alerts) | verify observability |
| S29 | bootstrap stages, images, backup/restore, unseal | lab | verified-live (all stages run; restore drill passed) | bootstrap journal; restore-verification.json |
| S30 | layered tests | tests | unit 149 + security 28 pass; component/integration run under Docker; operational/e2e need lab | junit evidence |
| S31 | B0–B3 protocol | release.bundle | not-run (no paired comparisons); arm availability reported | RG03 |
| S32 | six gates, honest not_run, human review | release.gates, release.bundle | verified-tests | test_release_gates; release qualify |

## Live defects found and fixed during bring-up

* Vault cert SAN outside name constraints; unseal via stdin; CA without key identifiers (Python 3.13 strict).
* Windows path in libpq DSN; kubectl port-forward single-connection behaviour; `ALTER ROLE` parameters.
* Platform venv path mismatch in image; `dotnet publish --locked-mode`.
* Keycloak realm missing `basic`/`roles` scopes (no `sub`, no roles in tokens); grant seeding vs new user IDs.
* Scenario preflight counted its own run as "another open run"; phase clock started before driver ran.
* Prometheus could not scrape the API (missing ingress rule) and could not verify pod-IP TLS (now Service DNS);
  Prometheus config changes did not roll the Pod (config hash annotation).
* `destroy` looked for backups in the wrong place; kind/helm prerequisites now automated.
* Container-type LimitRanges injected default limits/requests (1 CPU / 768Mi); now Pod-type bounds.
* pytest-timeout (thread method on Windows) killed the whole live suite at 300 s; live suites get 1800 s.
* Transient `down` scrape targets after Pod replacement; the check now confirms after one interval.
* Docker Desktop data disk on a 7200 rpm HDD: root cause of etcd/API-server instability (host setting).
* W01/W02 (.NET) checked capabilities against the metrics name instead of the workload ID: every
  E02 request got 401. Fixed; runner now classifies runs (`invalid` when load was not answered).
* kubectl Chocolatey shim leaked port-forward children; now the process tree is stopped.
* Grafana 13 undersized (256 MiB / 500m); resized from measured usage.

## Open items

* Live `verified_success` not yet recorded. The scheduled 09:50 re-run was cancelled at the owner's
  request (2026-11-21 ~04:50 UTC); E04 load stopped (last run aborted by operator, driver cleaned up),
  watchdog stopped, lab left in Observe with W04 at 150m/192Mi and no open operations or runs.
  The 03:47 E04 run had already been aborted at 04:36 by the watchdog (`host_memory_pressure`).
  To finish: run E04 back to back for ~6 h with no W04 rollout, then `capacity-lab release test --suite e2e`
  ~20 min into a run, then `capacity-lab release qualify`.
* W04 queue completion-within-deadline evidence (CPU candidates always indeterminate).
* Pod-UID lineage for multi-Pod telemetry windows (forecasts refuse ~6 h after any rollout).
* Model qualification to be re-run on an idle host.
* `capacity-system` / `cert-manager` default-deny; domain metrics and dashboards; full rebuild drill.

## Improvement candidates (not implemented)

* Evidence window: start the telemetry window at the current configuration revision instead of a fixed
  6 h lookback, so coverage and the 4 h minimum history are independent (today a new workload needs ~5.7 h).
* Domain metrics (queue depth, refusal rates, executor outcomes) and alert rules.
* mypy clean-up (dict-typed manifest builders).

## Findings from the overnight restart (2026-11-20 ~20:20 UTC)

* Docker restart reassigned kind node IPs (control plane 172.18.0.3 → .5): rendered API-server policies went
  stale. Added `capacity-lab resume` (re-render, restart dependants, unseal Vault).
* Worker readiness (capacity, executor, ai) stayed `down` after a transient DB error on an idle queue;
  fixed (error cleared after any successful claim) with a regression test.
* Host downtime removed telemetry from the 6 h lookback: live governed E2E must wait for ~6 h of continuous
  history again (not_run until then).
* E03 revision 1 arrival rate exceeds W03's own run bounds; needs a redesigned (sequential) scenario.

## Findings from the covered-telemetry analysis (2026-11-21 02:44 UTC)

* With ~6 h of E04 load (72/73 CPU buckets, memory coverage 0.990) both W04 forecasts were accepted
  (no refusals) and the engine produced three candidates: CPU request 150m → 75m, memory request
  192 Mi → 96 Mi, and both combined. None qualified:
  * memory candidates: `memory.oom_spike` unknown (`memory_tail_unqualified`) because one 45 s gap at
    20:45 (telemetry resume) set `scrape_gap` → `peak_not_guaranteed`. Spec-conformant (S19: scrape gap
    → unknown for risk-sensitive sizing); the gap leaves the 6 h window at 02:50.
  * CPU candidates: `slo.queue_completion` unknown (`queue_evidence_unknown`) — **defect**:
    `capacity/evidence.py` always sets `completion_within_deadline_rate=None`, although W04 emits
    `lab_tasks_completed_total{outcome="completed"|"deadline_missed"}` and the policy sets
    `min_completion_rate: 0.99`. No W04 CPU decrease can qualify until the evidence derives the rate
    (new registry query, never an edit of an existing one).
* 03:13 — with the gap gone the memory candidate qualified, and the E2E then failed at `PUT /api/v1/mode`
  (500): the API relay called `httpx.request(..., cert=...)`, which httpx 0.28 no longer accepts, so
  **every** API → governance/ai relay call crashed in the deployed image. Component tests build the
  relay without a client certificate, so they never exercised it. Fixed (client certificate loaded
  into an `ssl.SSLContext`, built per call so renewed certificates are picked up; unreadable
  certificate → `unavailable`) with `tests/security/test_internal_mtls.py`, which performs a real
  mutual-TLS handshake.
* 03:21–03:39 — first complete live chain (`plan-1698278fbb3b`, intent `intent-fb7bbb62d95d`): W04 memory
  request 192 Mi → 96 Mi applied at 03:22:06, configuration pass, rollout healthy, then
  `verification_inconclusive` (guardrails `unknown`) and the pre-authorised exact inverse → `recovered`
  at 03:39:30. Prometheus held every guardrail signal for the window (availability 1.0, 0 restarts,
  queue 0, request rate ≈ 1.0/s before and after). **Defect:** the executor queried every series at a
  15 s step, but `container.oom.restarts` is registered with a 60 s minimum, so the client refused it,
  the error was swallowed and `oom_restarts` was always missing — no change could ever be
  `verified_success`. Fixed (`executor/service.py: observe_window` uses each query's minimum step and
  logs failed queries) with two unit tests that run the real registry and client.
* 03:52 — the E2E test held one token per user from its start; they expired during verification, its
  polls got 401 and its cleanup could not restore Observe mode. Stopped it, restored Observe by hand,
  captured the evidence (`e2e-governed-change-20261121T035240Z.json`). Fixed: `tests/live.token()`
  caches until a minute before expiry and the test asks for a token at every call.
* 03:56 — forecasts refuse after the change and its inverse: the 6 h window holds three W04 Pods, the
  name-prefix queries return three series, `align()` flags `multiple_series_combined`, which
  `FLAG_REFUSALS` does not classify → `unclassified_quality_issue`. Kept fail-closed (S12/S14: no
  name-only join without Pod-UID lineage). Next clean window ≈ 09:50 UTC; the E2E is scheduled then.
* 04:11 — **defect:** refused governance commands were never durably audited. `_decide` wrote the
  `deny` row inside the command's transaction and the refusal rolled it back (no `deny` rows existed
  at all; S06 "Denial/audit", S24 durable denial reasons). Fixed: `_audit_refusals` wraps every
  command outside its transaction and writes the denial afterwards on a fresh connection (precise
  reason kept internally); integration test `test_refused_commands_leave_durable_denial_records`;
  verified live (`mode.set` by viewer1, approval on an unknown plan).
* Not fixed (noted): in `decide`, an expired plan's `EXPIRED` transition is written and then rolled back
  by the precondition error; expiry is re-detected on every later call, so the effect is cosmetic.
* `images` stage rebuilds workload images with new digests even when their sources are unchanged (base
  images are tag-pinned, not digest-pinned); `images.json` was reset to the deployed workload digests
  and later platform rebuilds were done for the platform image only.

## Final verification state (2026-11-20 21:04 UTC)

* All suites: unit 154, security 28, component 21, integration 30, operational 13, .NET 5 — passed.
* E2E: skipped (no qualified candidate: telemetry gap after host downtime). Re-run with
  `uv run pytest tests/e2e` once W04 has ~6 h of continuous telemetry.
* Release qualification `rq-20261120T210419Z`: NOT_QUALIFIED, no blocked gate.

## Documentation pass and final evidence (2026-11-21 05:00–05:55 UTC)

* README and all /docs rewritten (35 pages incl. index; new: how-it-works, proof-of-operation,
  test-catalog, cli-reference, cluster-topology, telemetry). Link/anchor checker: 36 files, 0 problems.
* Fresh evidence on the final build: `verify-all-20261121T051022.json` (7/7), audit verify intact (seq 15,
  7 checkpoints), suites 05:40–05:51: unit 156, security 31, component 21, integration 31, .NET 5,
  operational 13 — all passed.
* **Defect found:** restore drill flaky — `_wait_ready` accepted the postgres image's temporary init
  server (Unix socket only), `pg_restore` then hit its shutdown (`capacity-20261121T054401Z`, 12.3 s,
  failed). Fixed: readiness probe over TCP `127.0.0.1`; `restoreError` recorded on failure. 3/3 repeat
  drills + operational re-run passed.
