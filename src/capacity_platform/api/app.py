"""Operator API facade (S27): `/api/v1` over HTTPS with Keycloak access tokens.

Responsibilities: validate the human token, check role+scope for the request, read
authorized projections, enqueue long-running work (returning 202 + operation handle), and
relay governed commands to Governance with BOTH the human token and the API's machine
identity so Governance can re-validate independently. The API has no Kubernetes
credentials, cannot write governance/execution state and exposes no generic relay
(/kubectl, /sql, /promql, /patch, ... do not exist).
"""

from __future__ import annotations

import ssl
import uuid
from collections.abc import Callable
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from capacity_platform.capacity.service import current_grants, current_mode
from capacity_platform.contracts.canonical import digest
from capacity_platform.contracts.enums import Horizon, OperatingMode
from capacity_platform.contracts.errors import ErrorCategory, PlatformError, denied, unavailable
from capacity_platform.contracts.workloads import CATALOG
from capacity_platform.governance.authz import authorize
from capacity_platform.shared import jobs
from capacity_platform.shared.auth.oidc import AccessTokenValidator, HumanPrincipal
from capacity_platform.shared.auth.service_identity import read_own_token
from capacity_platform.shared.db import Connection, transaction
from capacity_platform.shared.web import correlation_id, create_app

CLUSTER = "capacity-lab"
P1_CAPABILITIES = {
    "gitops_flux_adapter": "unqualified",  # S25/S29 P1: direct adapter is the only executable path
    "supply_chain_admission_s10": "not_installed",
    "autoscaling_advisory_s23": "not_installed",
}


class AnalysisRequest(BaseModel):
    workloadId: str = Field(pattern=r"^w0[1-6]-[a-z-]{3,30}$")
    horizon: Literal["1h", "24h"] = "1h"
    intendedUse: Literal["governed_execution", "advisory"] = "governed_execution"


class AdvisoryCreate(BaseModel):
    workloadId: str = Field(pattern=r"^w0[1-6]-[a-z-]{3,30}$")
    mode: Literal["diagnostic", "optimization_review"]
    analysisDigest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    evaluationDigest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class Relay:
    def __init__(
        self, governance_url: str, ai_url: str, ca_bundle: str | None, client_cert: tuple[str, str] | None = None
    ) -> None:
        self._gov, self._ai, self._ca_bundle = governance_url, ai_url, ca_bundle
        self._cert = client_cert  # presented to governance's mutual-TLS listener

    def _tls(self) -> ssl.SSLContext:
        # httpx 0.28 accepts a client certificate only inside an SSL context (there is no `cert=`
        # argument). The context is built per call so certificates renewed by cert-manager are used
        # without restarting the Pod; relayed calls are rare, so the cost does not matter.
        ctx = ssl.create_default_context(cafile=self._ca_bundle)
        if self._cert:
            ctx.load_cert_chain(*self._cert)
        return ctx

    def send(
        self,
        target: str,
        method: str,
        path: str,
        token: str,
        json_body: dict | None = None,
        headers: dict | None = None,
    ) -> JSONResponse:
        base = self._gov if target == "governance" else self._ai
        try:
            tls = self._tls()
        except (OSError, ssl.SSLError) as exc:
            # A missing or unreadable certificate is an outage of this hop, not an internal bug.
            raise unavailable(f"{target}_unavailable", f"{target} service is unavailable") from exc
        try:
            resp = httpx.request(
                method,
                base + path,
                json=json_body,
                timeout=15.0,
                verify=tls,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Service-Token": read_own_token() or "",
                    "X-Correlation-ID": correlation_id.get(),
                    **(headers or {}),
                },
            )
        except httpx.HTTPError as exc:
            raise unavailable(f"{target}_unavailable", f"{target} service is unavailable") from exc
        return JSONResponse(status_code=resp.status_code, content=resp.json())


def build_app(connect: Callable[[], Connection], users: AccessTokenValidator, relay: Relay) -> FastAPI:
    app = create_app("api")

    def human(authorization: str = Header(default="")) -> tuple[HumanPrincipal, str]:
        if not authorization.lower().startswith("bearer "):
            raise PlatformError("AUTHENTICATION_REQUIRED", ErrorCategory.AUTHENTICATION, "access token required")
        token = authorization[7:]
        return users.validate(token), token

    def require(conn, who: HumanPrincipal, action: str, namespace: str | None) -> None:
        d = authorize(
            who.actor,
            set(who.roles),
            current_grants(conn, who.actor.key()),
            action,
            cluster_id=CLUSTER,
            namespace=namespace,
            mode=current_mode(conn),
        )
        if not d.allow:
            raise denied()

    # ------------------------------------------------------------------ context
    @app.get("/api/v1/capabilities")
    def capabilities(auth=Depends(human)) -> dict:
        with connect() as conn:
            mode = current_mode(conn)
            # The revision is what `PUT /api/v1/mode` needs for its optimistic-concurrency check.
            mode_revision = conn.execute("SELECT revision FROM governance.operating_mode").fetchone()["revision"]
            snap = conn.execute(
                "SELECT capture_completed, coverage, freshness FROM inventory.snapshot "
                "ORDER BY committed_at DESC LIMIT 1"
            ).fetchone()
        return {
            "mode": mode.value,
            "modeRevision": mode_revision,
            "autonomy": "L0-L2 (no L3)",
            "capabilities": {
                "inventory": "available" if snap else "unavailable",
                "deterministic_forecast_1h": "available",
                "conditional_forecast_24h": "evidence_qualified_only",
                "deterministic_optimization": "available",
                "governed_direct_execution": "available" if mode == OperatingMode.GOVERNED else "disabled_by_mode",
                "ai_advisory": "depends_on_qualified_model_gateway",
                **P1_CAPABILITIES,
            },
            "inventory": snap
            and {"capturedAt": snap["capture_completed"], "coverage": snap["coverage"], "freshness": snap["freshness"]},
        }

    @app.get("/api/v1/me/scopes")
    def scopes(auth=Depends(human)) -> dict:
        who, _ = auth
        with connect() as conn:
            grants = current_grants(conn, who.actor.key())
        return {
            "subject": who.actor.subject,
            "tokenRoles": sorted(r.value for r in who.roles),
            "grants": [
                {"role": g.role.value, "namespaces": sorted(g.namespaces)} for g in grants if g.revoked_at is None
            ],
        }

    # ------------------------------------------------------------------ inventory reads
    @app.get("/api/v1/workloads")
    def workloads(auth=Depends(human)) -> dict:
        who, _ = auth
        with connect() as conn:
            snap = conn.execute(
                "SELECT snapshot_id, capture_completed, coverage, freshness, material "
                "FROM inventory.snapshot ORDER BY committed_at DESC LIMIT 1"
            ).fetchone()
            visible = []
            for entry in CATALOG.values():
                try:
                    require(conn, who, "inventory.read", entry.namespace)
                except PlatformError:
                    continue  # silently omitted: no existence or count leak
                live = next(
                    (
                        w
                        for w in (snap["material"]["workloads"] if snap else [])
                        if w["namespace"] == entry.namespace and w["name"] == entry.name
                    ),
                    None,
                )
                visible.append(
                    {
                        "workloadId": entry.workload_id,
                        "namespace": entry.namespace,
                        "kind": entry.kind,
                        "criticality": entry.criticality.value,
                        "uid": live and live["uid"],
                        "requestsAndLimits": live and live["containers"].get(entry.container),
                        "basis": "kubernetes_configured_requests_not_usage",
                    }
                )
        return {
            "data": visible,
            "sourceRef": snap and str(snap["snapshot_id"]),
            "asOfUtc": snap and snap["capture_completed"],
            "quality": snap and {"coverage": snap["coverage"], "freshness": snap["freshness"]},
        }

    @app.get("/api/v1/nodes")
    def nodes(auth=Depends(human)) -> dict:
        who, _ = auth
        with connect() as conn:
            require(conn, who, "inventory.read", "*cluster*")
            snap = conn.execute(
                "SELECT snapshot_id, capture_completed, material FROM inventory.snapshot "
                "ORDER BY committed_at DESC LIMIT 1"
            ).fetchone()
        return {
            "data": [
                {k: n[k] for k in ("name", "ready", "unschedulable", "allocatable", "assigned", "taints")}
                for n in (snap["material"]["nodes"] if snap else [])
            ],
            "semantics": "allocatable and assigned are scheduler reservations, not observed usage",
            "sourceRef": snap and str(snap["snapshot_id"]),
        }

    # ------------------------------------------------------------------ capacity (async)
    @app.post("/api/v1/analyses", status_code=202)
    def create_analysis(
        body: AnalysisRequest, request: Request, idempotency_key: str = Header(default=""), auth=Depends(human)
    ) -> dict:
        who, _ = auth
        entry = CATALOG[body.workloadId]
        payload = {
            "workloadId": body.workloadId,
            "horizon": Horizon(body.horizon).value,
            "intendedUse": body.intendedUse,
            "actor": {"issuer": who.actor.issuer, "subject": who.actor.subject},
            "roles": sorted(r.value for r in who.roles),
        }
        key = idempotency_key or str(uuid.uuid4())
        with connect() as conn, transaction(conn):
            require(conn, who, "analysis.request", entry.namespace)
            job_id = jobs.enqueue(
                conn,
                queue="capacity",
                kind="analysis",
                payload=payload,
                idempotency_key=f"{who.actor.key()}|analysis|{key}",
                correlation_id=correlation_id.get(),
            )
        return {
            "schemaVersion": "capacity-operation/v1alpha1",
            "operationId": job_id,
            "operationType": "analysis",
            "state": "queued",
            "statusUrl": f"/api/v1/operations/{job_id}",
            "requestDigest": digest(payload),
            "correlationId": correlation_id.get(),
        }

    @app.post("/api/v1/advisories", status_code=202)
    def create_advisory(body: AdvisoryCreate, auth=Depends(human)) -> dict:
        who, _ = auth
        entry = CATALOG[body.workloadId]
        run_id = f"adv-{uuid.uuid4().hex[:12]}"
        payload = {
            **body.model_dump(),
            "workflowRunId": run_id,
            "namespace": entry.namespace,
            "actor": {"issuer": who.actor.issuer, "subject": who.actor.subject},
            "roles": sorted(r.value for r in who.roles),
        }
        with connect() as conn, transaction(conn):
            require(conn, who, "advisory.request", entry.namespace)
            job_id = jobs.enqueue(
                conn,
                queue="ai",
                kind="advisory",
                payload=payload,
                idempotency_key=run_id,
                correlation_id=correlation_id.get(),
            )
        return {
            "operationId": job_id,
            "operationType": "advisory",
            "workflowRunId": run_id,
            "state": "queued",
            "statusUrl": f"/api/v1/operations/{job_id}",
        }

    @app.get("/api/v1/operations/{operation_id}")
    def operation(operation_id: str, auth=Depends(human)) -> dict:
        who, _ = auth
        with connect() as conn:
            row = conn.execute(
                "SELECT job_id, queue, kind, state, attempt, payload, result, error_code, created_at, "
                "finished_at FROM workflow.jobs WHERE job_id::text = %s AND queue IN ('capacity','ai')",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise denied()
            entry = CATALOG.get(row["payload"].get("workloadId", ""))
            require(conn, who, "evidence.read", entry.namespace if entry else None)
        return {
            "operationId": str(row["job_id"]),
            "operationType": row["kind"],
            "state": row["state"],
            "attempt": row["attempt"],
            "result": row["result"],
            "errorCode": row["error_code"],
            "submittedAtUtc": row["created_at"],
            "finishedAtUtc": row["finished_at"],
            "note": "a completed analysis is evidence, not an approved or executed change",
        }

    def _scoped_read(table_sql: str, key: str, auth) -> dict:
        who, _ = auth
        with connect() as conn:
            row = conn.execute(table_sql, (key,)).fetchone()
            if row is None:
                raise denied()
            require(conn, who, "evidence.read", row["namespace"])
            return row["content"]

    @app.get("/api/v1/analyses/{digest_}")
    def get_analysis(digest_: str, auth=Depends(human)) -> dict:
        return _scoped_read(
            "SELECT namespace, result AS content FROM capacity.analysis WHERE analysis_digest = %s", digest_, auth
        )

    @app.get("/api/v1/candidates/{digest_}")
    def get_candidate(digest_: str, auth=Depends(human)) -> dict:
        return _scoped_read(
            "SELECT c.namespace, e.content FROM capacity.evaluation e JOIN capacity.candidate c "
            "USING (candidate_digest) WHERE e.integrity_digest = %s",
            digest_,
            auth,
        )

    @app.get("/api/v1/forecasts/{forecast_id}")
    def get_forecast(forecast_id: str, auth=Depends(human)) -> dict:
        return _scoped_read(
            "SELECT namespace, result AS content FROM capacity.forecast_run WHERE forecast_id = %s", forecast_id, auth
        )

    @app.get("/api/v1/advisories/{run_id}")
    def get_advisory(run_id: str, auth=Depends(human)) -> dict:
        return _scoped_read(
            "SELECT namespace, content FROM knowledge.advisory_artifact WHERE workflow_run_id = %s", run_id, auth
        )

    # ------------------------------------------------------------------ governance relay
    @app.post("/api/v1/plans", status_code=201)
    def create_plan(body: dict, auth=Depends(human)):
        return relay.send("governance", "POST", "/internal/v1/plans", auth[1], body)

    @app.get("/api/v1/plans/{plan_id}")
    def get_plan(plan_id: str, revision: int | None = None, auth=Depends(human)):
        suffix = f"?revision={revision}" if revision else ""
        return relay.send("governance", "GET", f"/internal/v1/plans/{plan_id}{suffix}", auth[1])

    @app.post("/api/v1/plans/{plan_id}/approvals", status_code=201)
    def approvals(plan_id: str, body: dict, auth=Depends(human)):
        return relay.send("governance", "POST", f"/internal/v1/plans/{plan_id}/decisions", auth[1], body)

    @app.post("/api/v1/plans/{plan_id}/revocations", status_code=201)
    def revocations(plan_id: str, body: dict, auth=Depends(human)):
        return relay.send("governance", "POST", f"/internal/v1/plans/{plan_id}/revocations", auth[1], body)

    @app.post("/api/v1/plans/{plan_id}/execution-requests", status_code=202)
    def execution_requests(plan_id: str, body: dict, idempotency_key: str = Header(default=""), auth=Depends(human)):
        return relay.send(
            "governance",
            "POST",
            f"/internal/v1/plans/{plan_id}/execution-requests",
            auth[1],
            body,
            {"Idempotency-Key": idempotency_key},
        )

    @app.put("/api/v1/mode")
    def set_mode(body: dict, auth=Depends(human)):
        return relay.send("governance", "PUT", "/internal/v1/mode", auth[1], body)

    @app.post("/api/v1/knowledge/documents", status_code=201)
    def ingest(body: dict, auth=Depends(human)):
        return relay.send("ai", "POST", "/internal/v1/knowledge/documents", auth[1], body)

    # ------------------------------------------------------------------ execution reads
    @app.get("/api/v1/executions/{intent_id}")
    def execution(intent_id: str, auth=Depends(human)) -> dict:
        who, _ = auth
        with connect() as conn:
            row = conn.execute(
                "SELECT o.state, o.content, p.namespace FROM execution.operation o "
                "JOIN governance.plan_revision p ON p.plan_id = o.plan_id AND p.revision = o.plan_revision "
                "WHERE o.intent_id = %s",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise denied()
            require(conn, who, "execution.read", row["namespace"])
            outcome = conn.execute(
                "SELECT disposition, content FROM execution.outcome WHERE intent_id = %s "
                "ORDER BY finished_at DESC LIMIT 1",
                (intent_id,),
            ).fetchone()
        return {
            "intentId": intent_id,
            "state": row["state"],
            "operation": row["content"],
            "outcome": outcome and outcome["content"],
            "note": "applied_pending_verification is not success; only S26 outcomes report verification",
        }

    return app
