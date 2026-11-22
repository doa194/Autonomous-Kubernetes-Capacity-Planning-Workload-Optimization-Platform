"""Capacity runtime: durable analysis/forecast worker plus internal evidence reads.

Jobs arrive on the `capacity` queue (enqueued by the API or governance). The worker
re-checks the requesting human's current grant before doing any work, verifies that its
policy copy matches the governance-authoritative digest, and then runs the deterministic
S14 -> S19..S22 pipeline. It needs no AI, Ollama or RAG to do any of this (S02-ADR-011).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header

from capacity_platform.capacity.evidence import collect_evidence, nodes_from_snapshot, target_from_snapshot
from capacity_platform.capacity.policy.model import PolicyDocument, load_policy, resolve_effective_policy
from capacity_platform.capacity.simulation.evaluate import evaluate
from capacity_platform.capacity.store import CapacityStore
from capacity_platform.capacity.telemetry.registry import PrometheusClient
from capacity_platform.contracts.enums import Horizon, HumanRole, OperatingMode
from capacity_platform.contracts.errors import ErrorCategory, PlatformError, denied
from capacity_platform.contracts.identity import ActorRef
from capacity_platform.contracts.workloads import CATALOG
from capacity_platform.governance.authz import Grant, authorize
from capacity_platform.shared import jobs
from capacity_platform.shared.auth.oidc import AccessTokenValidator, HumanPrincipal
from capacity_platform.shared.auth.service_identity import AI_SA, API_SA, ServiceTokenVerifier
from capacity_platform.shared.db import Connection, transaction
from capacity_platform.shared.web import create_app

log = logging.getLogger("capacity")
SNAPSHOT_MAX_AGE = timedelta(seconds=90)


def current_grants(conn: Connection, subject_key: str) -> list[Grant]:
    rows = conn.execute("SELECT * FROM governance.role_grant WHERE subject_key = %s", (subject_key,)).fetchall()
    return [
        Grant(
            r["grant_id"],
            r["subject_key"],
            HumanRole(r["role"]),
            r["cluster_id"],
            frozenset(r["namespaces"]),
            r["revision"],
            r["revoked_at"],
        )
        for r in rows
    ]


def current_mode(conn: Connection) -> OperatingMode:
    return OperatingMode(conn.execute("SELECT mode FROM governance.operating_mode").fetchone()["mode"])


class CapacityWorker:
    def __init__(
        self,
        connect: Callable[[], Connection],
        prometheus: PrometheusClient,
        policy_path: Path,
        logical_cluster: str = "capacity-lab",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._connect, self._prom, self._policy_path = connect, prometheus, policy_path
        self.logical_cluster, self._clock = logical_cluster, clock
        self.last_error: str | None = None

    def policy_document(self) -> PolicyDocument:
        return load_policy(self._policy_path)

    def handle(self, job: jobs.Job, conn: Connection) -> dict:
        p = job.payload
        workload_id = p["workloadId"]
        entry = CATALOG.get(workload_id)
        if entry is None:
            raise PlatformError("target_disallowed", ErrorCategory.PRECONDITION, "unknown workload")
        actor = ActorRef(issuer=p["actor"]["issuer"], subject=p["actor"]["subject"])
        decision = authorize(
            actor,
            {HumanRole(r) for r in p.get("roles", [])},
            current_grants(conn, actor.key()),
            "analysis.request",
            cluster_id=self.logical_cluster,
            namespace=entry.namespace,
            mode=current_mode(conn),
        )
        if not decision.allow:
            raise denied()  # grant revoked while the job waited in the queue
        snap = conn.execute("SELECT * FROM inventory.snapshot ORDER BY committed_at DESC LIMIT 1").fetchone()
        if snap is None:
            raise PlatformError("inventory_unavailable", ErrorCategory.UNAVAILABLE, "no inventory snapshot yet")
        now = self._clock()
        snapshot = {
            "incarnationId": snap["incarnation_id"],
            "material": snap["material"],
            "capture": {
                "coverage": snap["coverage"],
                "freshness": "stale" if now - snap["capture_completed"] > SNAPSHOT_MAX_AGE else snap["freshness"],
            },
        }
        doc = self.policy_document()
        policy = resolve_effective_policy(
            doc,
            cluster_id=self.logical_cluster,
            namespace=entry.namespace,
            workload_id=workload_id,
            criticality=entry.criticality,
        )
        authoritative = conn.execute(
            "SELECT policy_digest FROM governance.effective_policy WHERE workload_id = %s", (workload_id,)
        ).fetchone()
        if authoritative is None or authoritative["policy_digest"] != policy.digest:
            raise PlatformError(
                "policy_superseded",
                ErrorCategory.PRECONDITION,
                "local policy copy does not match the governance-authoritative revision",
            )
        target = target_from_snapshot(snapshot, self.logical_cluster, workload_id)
        nodes, per_node = nodes_from_snapshot(snapshot, target.target.controller.uid)
        evidence, forecasts = collect_evidence(self._prom, snapshot, target, now, str(snap["snapshot_id"]))
        intended = p.get("intendedUse", "governed_execution")
        result = evaluate(
            target,
            evidence,
            policy,
            nodes,
            target_pods_per_node=per_node,
            horizon=Horizon(p.get("horizon", "1h")),
            intended_use=intended,
        )
        store = CapacityStore(conn)
        for fc in forecasts:
            store.save_forecast(
                fc, idempotency_key=f"{job.job_id}:{fc.request.resource.value}", correlation_id=job.correlation_id
            )
        store.save_analysis(result, entry.namespace, workload_id, job.correlation_id)
        return {
            "analysisDigest": result.analysisDigest,
            "status": result.status,
            "qualified": [e.integrityDigest for e in result.qualified()],
            "forecastIds": [f.forecastId for f in forecasts],
        }

    def run_forever(self, worker_id: str, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                with self._connect() as conn:
                    job = jobs.claim(conn, queue="capacity", worker=worker_id, lease=timedelta(minutes=5))
                    # The database answered: readiness must recover even when there is no work queued.
                    self.last_error = None
                    if job is None:
                        conn.commit()
                        stop.wait(2.0)
                        continue
                    try:
                        with transaction(conn):
                            result = self.handle(job, conn)
                            jobs.complete(conn, job, result)
                    except PlatformError as exc:
                        jobs.fail(conn, job, exc.code, retryable=exc.retryable)
                        conn.commit()
                self.last_error = None
            except Exception as exc:  # DB outage etc.: back off; queued work stays durable
                self.last_error = type(exc).__name__
                log.warning("capacity worker loop error: %s", self.last_error)
                stop.wait(5.0)


def build_app(
    connect: Callable[[], Connection],
    users: AccessTokenValidator,
    machines: ServiceTokenVerifier,
    worker: CapacityWorker,
) -> FastAPI:
    app = create_app("capacity", lambda: {"database": "down" if worker.last_error else "up"})

    def caller(authorization: str = Header(default=""), x_service_token: str = Header(default="")) -> HumanPrincipal:
        machines.verify(x_service_token, {API_SA, AI_SA})
        if not authorization.lower().startswith("bearer "):
            raise PlatformError("AUTHENTICATION_REQUIRED", ErrorCategory.AUTHENTICATION, "access token required")
        return users.validate(authorization[7:])

    def scoped(conn, who: HumanPrincipal, namespace: str) -> None:
        d = authorize(
            who.actor,
            set(who.roles),
            current_grants(conn, who.actor.key()),
            "evidence.read",
            cluster_id=worker.logical_cluster,
            namespace=namespace,
            mode=current_mode(conn),
        )
        if not d.allow:
            raise denied()

    @app.post("/internal/v1/tools/{tool_id}")
    def tool(
        tool_id: str, args: dict, x_service_token: str = Header(default=""), x_delegation_ref: str = Header(default="")
    ) -> dict:
        # AI evidence tools: machine identity + live delegation lookup, no human token needed.
        from capacity_platform.capacity.tools import run_tool

        machines.verify(x_service_token, {AI_SA})
        with connect() as conn:
            return run_tool(conn, tool_id, args, x_delegation_ref, worker._prom)

    @app.get("/internal/v1/forecasts/{forecast_id}")
    def get_forecast(forecast_id: str, who: HumanPrincipal = Depends(caller)) -> dict:
        with connect() as conn:
            row = conn.execute(
                "SELECT namespace, result FROM capacity.forecast_run WHERE forecast_id = %s", (forecast_id,)
            ).fetchone()
            if row is None:
                raise denied()
            scoped(conn, who, row["namespace"])
            return row["result"]

    @app.get("/internal/v1/evaluations/{digest}")
    def get_evaluation(digest: str, who: HumanPrincipal = Depends(caller)) -> dict:
        with connect() as conn:
            row = conn.execute(
                "SELECT c.namespace, e.content FROM capacity.evaluation e JOIN capacity.candidate c "
                "USING (candidate_digest) WHERE e.integrity_digest = %s",
                (digest,),
            ).fetchone()
            if row is None:
                raise denied()
            scoped(conn, who, row["namespace"])
            return row["content"]

    @app.get("/internal/v1/analyses/{digest}")
    def get_analysis(digest: str, who: HumanPrincipal = Depends(caller)) -> dict:
        with connect() as conn:
            row = conn.execute(
                "SELECT namespace, result FROM capacity.analysis WHERE analysis_digest = %s", (digest,)
            ).fetchone()
            if row is None:
                raise denied()
            scoped(conn, who, row["namespace"])
            return row["result"]

    return app
