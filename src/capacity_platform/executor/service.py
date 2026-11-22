"""Executor runtime: intent worker, S26 verification loop and bounded recovery.

Runs as the only Deployment holding the `capacity-executor-sa` mutation identity. It does
not expose any mutation endpoint: work arrives exclusively as durable intent jobs written by
Governance, and authority is re-read from the governance view for every attempt.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from capacity_platform.capacity.telemetry.registry import REGISTRY, PrometheusClient, Series, render
from capacity_platform.contracts.workloads import CATALOG
from capacity_platform.executor.kube import KubeConflict, KubernetesClientAdapter, KubeUncertain
from capacity_platform.executor.operations import Environment, Executor, Operation, St
from capacity_platform.executor.store import SqlExecutionStore, SqlGovernancePort
from capacity_platform.executor.verification import (
    VerificationPolicy,
    WindowSummary,
    build_outcome,
    plan_recovery,
    verify_configuration,
    verify_operational,
)
from capacity_platform.governance.plans import ExecutionPlan
from capacity_platform.shared import jobs
from capacity_platform.shared.db import Connection, emit_event, jsonb, transaction
from capacity_platform.shared.web import create_app

log = logging.getLogger("executor")
HOST_SAFETY = ("capacity-system", "host-safety")
VERIFY_STEP_SECONDS = 15


def observe_window(prom: PrometheusClient, workload_id: str, start: datetime, end: datetime) -> WindowSummary | None:
    """Summarise one verification window (S26 level 2) from the registered telemetry queries.

    A query that fails leaves its value as None, so the guardrail becomes `unknown` — never a pass
    and never a zero. Each query runs at no less than its registered minimum step; asking for a
    finer step is refused by the client and would silently turn that guardrail into `unknown`.
    """
    entry = CATALOG[workload_id]
    sel = {"namespace": entry.namespace, "pod_prefix": entry.name, "container": entry.container, "workload": entry.name}

    def fetch(qid: str) -> Series | None:
        d = REGISTRY[(qid, 1)]
        try:
            return prom.range(d, render(d, **sel), start, end, max(VERIFY_STEP_SECONDS, d.min_step_seconds))
        except Exception as exc:
            # Logged so an `unknown` verdict can be traced to its cause; the value stays missing.
            log.warning("verification query %s failed: %s", qid, getattr(exc, "code", type(exc).__name__))
            return None

    def present(s: Series | None) -> list[float]:
        return [v for v in s.values if v is not None] if s else []

    def mean(qid: str) -> float | None:
        vals = present(fetch(qid))
        return sum(vals) / len(vals) if vals else None

    cpu_series = fetch("container.cpu.rate.millicores")
    if cpu_series is None or cpu_series.coverage == 0:
        return None
    cpu = present(cpu_series)
    p95 = mean("app.http.p95.seconds")
    availability = present(fetch("workload.availability.ratio"))
    restarts = present(fetch("container.oom.restarts"))
    queue_series = fetch("app.queue.depth")
    queue = present(queue_series)
    growth = None
    if queue_series is not None and len(queue) > 10:  # least-squares slope, queue depth per minute
        n, minutes_per_point = len(queue), queue_series.step_seconds / 60
        xs = [i * minutes_per_point for i in range(n)]
        mx, my = sum(xs) / n, sum(queue) / n
        growth = sum((x - mx) * (y - my) for x, y in zip(xs, queue, strict=True)) / sum((x - mx) ** 2 for x in xs)
    return WindowSummary(
        start=start,
        end=end,
        coverage=cpu_series.coverage,
        request_rate=mean("app.http.request.rate"),
        p95_latency_ms=p95 * 1000 if p95 is not None else None,
        error_rate=mean("app.http.error.ratio"),
        availability=min(availability) if availability else None,
        oom_restarts=int(max(restarts)) if restarts else None,
        queue_growth_per_min=growth,
        cpu_mean_m=sum(cpu) / len(cpu),
        observation_ref=f"obs:verify:{workload_id}:{start.isoformat()}",
    )


class ExecutorRuntime:
    def __init__(
        self,
        connect: Callable[[], Connection],
        cluster_incarnation: str,
        policy: VerificationPolicy,
        prometheus: PrometheusClient,
        worker_id: str = "executor-0",
    ) -> None:
        from kubernetes import client

        self._connect, self._incarnation, self._policy = connect, cluster_incarnation, policy
        self._prom, self.worker_id = prometheus, worker_id
        self.kube = KubernetesClientAdapter()
        self._core, self._hpa = client.CoreV1Api(), client.AutoscalingV2Api()
        self.last_error: str | None = None

    # ------------------------------------------------------------------ environment signals
    def environment(self, namespace: str, name: str) -> Environment:
        host = "unknown"
        try:
            cm = self._core.read_namespaced_config_map(HOST_SAFETY[1], HOST_SAFETY[0])
            data = cm.data or {}
            updated = datetime.fromisoformat((data.get("updatedAt") or "").replace("Z", "+00:00"))
            if datetime.now(UTC) - updated <= timedelta(minutes=2):
                host = data.get("state", "unknown")
        except Exception:
            host = "unknown"  # no fresh signal: S25 step 7 fails closed
        hpa: bool | None
        try:
            hpas = self._hpa.list_namespaced_horizontal_pod_autoscaler(namespace).items
            hpa = any(h.spec.scale_target_ref.name == name for h in hpas)
        except Exception:
            hpa = None
        return Environment(cluster_incarnation=self._incarnation, host_safety=host, hpa_present=hpa)

    # ------------------------------------------------------------------ intent worker
    def process_intents(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                with self._connect() as conn:
                    conn.autocommit = True
                    job = jobs.claim(conn, queue="executor", worker=self.worker_id, lease=timedelta(minutes=2))
                    # The database answered: readiness must recover even when there is no work queued.
                    self.last_error = None
                    if job is None:
                        stop.wait(2.0)
                        continue
                    intent_id = job.payload["intentId"]
                    gov = SqlGovernancePort(conn)
                    bundle = gov.get_authority(intent_id)
                    env = (
                        self.environment(bundle.plan.target.namespace, bundle.plan.target.controller.name)
                        if bundle
                        else Environment(cluster_incarnation=self._incarnation, host_safety="unknown", hpa_present=None)
                    )
                    op = Executor(self.kube, gov, SqlExecutionStore(conn), self.worker_id).run(
                        intent_id, env, datetime.now(UTC)
                    )
                    if op is None:
                        # Controller busy with another open operation: retry later, never in parallel.
                        jobs.fail(conn, job, "target_locked", retryable=True)
                    else:
                        jobs.complete(conn, job, {"state": op.state.value})
                self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__
                log.warning("intent worker error: %s", self.last_error)
                stop.wait(5.0)

    # ------------------------------------------------------------------ S26 verification
    def verify_pending(self) -> None:
        with self._connect() as conn:
            conn.autocommit = True
            rows = conn.execute(
                "SELECT intent_id FROM execution.operation WHERE state IN "
                "('applied_pending_verification', 'verification_in_progress', "
                "'verification_failed', 'verification_inconclusive')"
            ).fetchall()
            for row in rows:
                self._verify_one(conn, row["intent_id"])

    def _verify_one(self, conn: Connection, intent_id: str) -> None:
        store = SqlExecutionStore(conn)
        gov = SqlGovernancePort(conn)
        bundle = gov.get_authority(intent_id)
        epoch = store.claim(intent_id, self.worker_id, datetime.now(UTC))
        op = store.load(intent_id)
        if bundle is None or epoch is None or op is None:
            return
        op.fencingEpoch = epoch
        plan: ExecutionPlan = bundle.plan
        now = datetime.now(UTC)
        applied_at = next((t.at for t in op.history if t.to_state == St.APPLIED_PENDING_VERIFICATION), now)
        if op.state == St.APPLIED_PENDING_VERIFICATION:
            op.move(St.VERIFICATION_IN_PROGRESS, "verification_started", now)
            store.save(op)
        if op.state in (St.VERIFICATION_FAILED, St.VERIFICATION_INCONCLUSIVE):
            self._recover(conn, store, op, plan, now)
            return
        live = self.kube.get_deployment(plan.target.namespace, plan.target.controller.name)
        cfg = verify_configuration(
            plan, live, rollout_started=applied_at, now=now, deadline_seconds=self._policy.rolloutDeadlineSeconds
        )
        if cfg.rollout == "incomplete":
            return
        stable = applied_at + timedelta(seconds=self._policy.stabilizationSeconds)
        post_end = stable + timedelta(seconds=self._policy.evaluationSeconds)
        ops_verdict = None
        baseline = post = None
        if cfg.rollout == "healthy" and cfg.configuration.value == "pass":
            if now < post_end:
                return  # still collecting the qualified post-change window
            wid = plan.target.logicalWorkloadId
            baseline = observe_window(
                self._prom, wid, applied_at - timedelta(seconds=self._policy.baselineSeconds), applied_at
            )
            post = observe_window(self._prom, wid, stable, post_end)
            thresholds = self._policy.workloads.get(plan.target.logicalWorkloadId)
            if thresholds is not None:
                delta = {"cpu_m": 0, "memory_bytes": 0}
                for f in plan.approvedFieldChanges:
                    if f.before is not None and f.after is not None and f.field.value.endswith("request"):
                        delta["cpu_m" if f.field.value.startswith("cpu") else "memory_bytes"] = f.after - f.before
                ops_verdict = verify_operational(
                    thresholds, self._policy.loadComparabilityTolerance, baseline, post, delta
                )
        outcome = build_outcome(plan, op, cfg, ops_verdict, self._policy, baseline, post, now)
        disposition = outcome.operationalDisposition
        target = {
            "verified_safe_benefit_verified": St.VERIFIED_SUCCESS,
            "verified_safe_benefit_unproven": St.VERIFIED_SUCCESS,
            "verification_failed": St.VERIFICATION_FAILED,
            "verification_inconclusive": St.VERIFICATION_INCONCLUSIVE,
        }.get(disposition)
        if target is None:
            return
        op.move(target, disposition, now)
        with transaction(conn):
            conn.execute(
                "INSERT INTO execution.outcome (outcome_id, intent_id, disposition, content, outcome_digest, "
                "finished_at) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (outcome.outcomeId, intent_id, disposition, jsonb(outcome), outcome.outcomeDigest, now),
            )
            emit_event(
                conn,
                "execution",
                "OutcomeFinalized.v1",
                intent_id,
                {"intentId": intent_id, "outcomeDigest": outcome.outcomeDigest, "disposition": disposition},
                intent_id,
            )
        store.save(op)

    def _recover(self, conn, store: SqlExecutionStore, op: Operation, plan: ExecutionPlan, now: datetime) -> None:
        live = self.kube.get_deployment(plan.target.namespace, plan.target.controller.name)
        decision = plan_recovery(plan, op, live, self._incarnation)
        if decision.action == "escalate":
            op.move(St.ESCALATED, decision.reason, now)
            store.save(op)
            return
        op.move(St.RECOVERY_IN_PROGRESS, decision.reason, now)
        op.recoveryAttempts += 1
        store.save(op)  # journal the inverse intent before the external write
        if decision.action == "apply_inverse":
            try:
                self.kube.patch_deployment(
                    plan.target.namespace, plan.target.controller.name, decision.patch, dry_run=False
                )
            except (KubeConflict, KubeUncertain):
                pass  # outcome decided only by the observation below
        restored = self.kube.get_deployment(plan.target.namespace, plan.target.controller.name)
        from capacity_platform.executor.operations import container_index, fields_match

        idx = container_index(restored, plan.target.containerName) if restored else None
        ok = (
            restored is not None
            and idx is not None
            and restored["metadata"]["uid"] == plan.target.controller.uid
            and fields_match(restored, idx, plan.approvedFieldChanges, use_after=False)
        )
        op.move(St.RECOVERED if ok else St.ESCALATED, "inverse_observed" if ok else "inverse_not_observed", now)
        store.save(op)

    def verification_loop(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.verify_pending()
            except Exception as exc:
                log.warning("verification loop error: %s", type(exc).__name__)
            stop.wait(15.0)

    def readiness(self) -> dict[str, str]:
        return {"database": "down" if self.last_error else "up"}


def build_app(runtime: ExecutorRuntime):
    # Deliberately no mutation routes: health and metrics only.
    return create_app("executor", runtime.readiness)
