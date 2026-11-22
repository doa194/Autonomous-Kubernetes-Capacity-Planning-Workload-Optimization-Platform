"""`ai` runtime: advisory job worker, governed knowledge ingestion, verified-outcome feed.

The `ai` Deployment holds no Kubernetes credential, no executor/governance write grant and
no experiment authority. It reaches evidence only through the capacity tool backend (with
machine identity + live delegation) and its own knowledge schema, and reaches the model
only through the protected host gateway. If the gateway is down, advisory jobs finish as
`unavailable`; nothing else in the platform depends on it.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import Depends, FastAPI, Header

from capacity_platform.ai.agents.workflow import AdvisoryRequest, AdvisoryWorkflow
from capacity_platform.ai.broker.broker import (
    BackendDenied,
    BackendUnavailable,
    ToolAuthorizationContext,
    ToolBroker,
)
from capacity_platform.ai.inference.client import GatewayClient
from capacity_platform.ai.knowledge.store import KnowledgeStore, SourceDocument
from capacity_platform.capacity.service import current_grants, current_mode
from capacity_platform.contracts.enums import AgentRole, HumanRole
from capacity_platform.contracts.errors import ErrorCategory, PlatformError, denied
from capacity_platform.contracts.identity import ActorRef
from capacity_platform.governance.authz import authorize
from capacity_platform.shared import jobs
from capacity_platform.shared.auth.oidc import AccessTokenValidator, HumanPrincipal
from capacity_platform.shared.auth.service_identity import API_SA, ServiceTokenVerifier, read_own_token
from capacity_platform.shared.db import Connection, already_processed, audit, emit_event, jsonb, transaction
from capacity_platform.shared.web import create_app

log = logging.getLogger("ai")
CLUSTER = "capacity-lab"
LEASE = timedelta(minutes=10)


class HttpToolBackend:
    """Calls the capacity tool backend; knowledge search stays in-process (ai owns it)."""

    def __init__(
        self,
        capacity_url: str,
        knowledge: Callable[[], Connection],
        ca_bundle: str | None,
        gateway: GatewayClient | None,
    ) -> None:
        self._url, self._knowledge, self._verify, self._gateway = capacity_url, knowledge, ca_bundle or True, gateway

    def call(self, tool_id: str, args: dict, ctx: ToolAuthorizationContext) -> dict:
        if tool_id == "knowledge.search_authorized.v1":
            vector = None
            if self._gateway is not None and self._gateway.profile.embeddingModel is not None:
                try:
                    vector = self._gateway.embed([args["query"]])[0]
                except Exception:
                    vector = None  # lexical-only degraded retrieval (S18 §9)
            with self._knowledge() as conn:
                return KnowledgeStore(conn).search(
                    query=args["query"],
                    cluster_id=ctx.cluster_id,
                    namespaces=sorted(ctx.namespaces),
                    max_classification="Confidential",
                    collections=args.get("collections"),
                    top_k=args.get("topK", 5),
                    query_vector=vector,
                )
        try:
            resp = httpx.post(
                f"{self._url}/internal/v1/tools/{tool_id}",
                json=args,
                timeout=15.0,
                verify=self._verify,
                headers={
                    "X-Service-Token": read_own_token() or "",
                    "X-Delegation-Ref": ctx.delegation_ref,
                    "X-Correlation-ID": ctx.workflow_run_id,
                },
            )
        except httpx.TimeoutException as exc:
            raise TimeoutError from exc
        except httpx.HTTPError as exc:
            raise BackendUnavailable(str(exc)) from exc
        if resp.status_code in (401, 403, 404):
            raise BackendDenied(resp.status_code)
        if resp.status_code != 200:
            raise BackendUnavailable(resp.status_code)
        return resp.json()


class AdvisoryWorker:
    def __init__(
        self,
        connect: Callable[[], Connection],
        backend: HttpToolBackend,
        model_call,
        model_ref: dict,
        model_qualified: bool,
    ) -> None:
        self._connect, self._backend = connect, backend
        self._model_call, self._model_ref, self._qualified = model_call, model_ref, model_qualified
        self.last_error: str | None = None

    def handle(self, job: jobs.Job, conn: Connection) -> dict:
        p = job.payload
        actor = ActorRef(issuer=p["actor"]["issuer"], subject=p["actor"]["subject"])
        grants = current_grants(conn, actor.key())
        roles = {HumanRole(r) for r in p.get("roles", [])}
        mode = current_mode(conn)
        namespaces = frozenset(
            ns
            for g in grants
            if g.revoked_at is None
            for ns in g.namespaces
            if authorize(actor, roles, grants, "evidence.read", cluster_id=CLUSTER, namespace=ns, mode=mode).allow
        )
        if not authorize(
            actor, roles, grants, "advisory.request", cluster_id=CLUSTER, namespace=p["namespace"], mode=mode
        ).allow:
            raise denied()
        request = AdvisoryRequest(
            workflowRunId=p["workflowRunId"],
            mode=p["mode"],
            workloadId=p["workloadId"],
            analysisDigest=p.get("analysisDigest"),
            topEvaluationDigest=p.get("evaluationDigest"),
        )
        if not self._qualified:
            # S15: no unverified model is used for an advisory that governance could rely on.
            art_status, reasons = "unavailable", ["model_unqualified"]
            artifact = None
        else:
            ctx = ToolAuthorizationContext(
                workflow_run_id=request.workflowRunId,
                delegation_ref=job.job_id,
                caller_machine="capacity-ai-sa",
                logical_role=AgentRole.CAPACITY_ANALYST,
                actor_key=actor.key(),
                cluster_id=CLUSTER,
                namespaces=namespaces,
                expires_at=datetime.now(UTC) + timedelta(minutes=9),
            )

            def tool_audit(event: dict) -> None:
                audit(
                    conn,
                    correlation_id=request.workflowRunId,
                    action="ai.tool",
                    scope={"workload": p["workloadId"]},
                    decision=event["decision"],
                    reason=event["reason"],
                    details={k: v for k, v in event.items() if k not in ("decision", "reason")},
                )

            def checkpoint(run_id: str, stage: str, attempt: int, content: dict) -> None:
                from capacity_platform.contracts.canonical import digest

                conn.execute(
                    "INSERT INTO knowledge.workflow_checkpoint VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (run_id, stage, attempt, jsonb(content), digest(content)),
                )

            broker = ToolBroker(self._backend, clock=lambda: datetime.now(UTC), audit=tool_audit)
            wf = AdvisoryWorkflow(broker, self._model_call, self._model_ref, lambda: datetime.now(UTC), checkpoint)
            artifact = wf.run(request, ctx)
            art_status, reasons = artifact.status, artifact.reasonCodes
        if artifact is not None:
            conn.execute(
                "INSERT INTO knowledge.advisory_artifact (artifact_digest, workflow_run_id, mode, status, "
                "namespace, workload_id, content) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    artifact.artifactDigest,
                    request.workflowRunId,
                    request.mode,
                    artifact.status,
                    p["namespace"],
                    p["workloadId"],
                    jsonb(artifact),
                ),
            )
        event = "AdvisoryReviewCompleted.v1" if art_status == "completed" else "AdvisoryReviewUnavailable.v1"
        emit_event(
            conn,
            "knowledge",
            event,
            request.workflowRunId,
            {
                "workflowRunId": request.workflowRunId,
                "status": art_status,
                "reasonCodes": reasons,
                "artifactDigest": artifact.artifactDigest if artifact else None,
            },
            job.correlation_id,
        )
        return {
            "status": art_status,
            "artifactDigest": artifact.artifactDigest if artifact else None,
            "reasonCodes": reasons,
        }

    def ingest_outcomes(self) -> None:
        """Turn finalized S26 outcomes into redacted verified_outcomes knowledge (deduplicated)."""
        with self._connect() as conn, transaction(conn):
            rows = conn.execute(
                "SELECT o.event_id, o.payload, oc.content FROM execution.outbox o "
                "JOIN execution.outcome oc ON oc.outcome_digest = o.payload->>'outcomeDigest' "
                "WHERE o.event_type = 'OutcomeFinalized.v1' ORDER BY o.created_at LIMIT 20"
            ).fetchall()
            store = KnowledgeStore(conn)
            for row in rows:
                if already_processed(conn, "svc_ai", str(row["event_id"])):
                    continue
                oc = row["content"]
                body = (
                    f"# Verified outcome {oc['planId']} r{oc['planRevision']}\n\nDisposition: "
                    f"{oc['operationalDisposition']}. Benefit: {oc['benefit']} ({oc['benefitReason']}). "
                    f"Reservation delta per Pod: {oc['requestReservationDelta']}. "
                    f"Basis: {oc['reservationBasis']}. Guardrails: {oc['safetyGuardrails']}."
                )
                store.ingest(
                    SourceDocument(
                        documentId=f"outcome-{oc['planId']}-r{oc['planRevision']}",
                        collection="verified_outcomes",
                        title=f"Outcome {oc['planId']}",
                        clusterId=CLUSTER,
                        namespaceScope="*cluster*",
                        classification="Confidential",
                        sourceKind="s26_outcome",
                        validFrom=oc["finishedAtUtc"],
                        body=body,
                    ),
                    principal="executor-outcome-feed",
                )

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                with self._connect() as conn:
                    job = jobs.claim(conn, queue="ai", worker="ai-0", lease=LEASE)
                    # The database answered: readiness must recover even when there is no work queued.
                    self.last_error = None
                    if job is None:
                        conn.commit()
                        self.ingest_outcomes()
                        stop.wait(3.0)
                        continue
                    try:
                        with transaction(conn):
                            jobs.complete(conn, job, self.handle(job, conn))
                    except PlatformError as exc:
                        jobs.fail(conn, job, exc.code, retryable=exc.retryable)
                        conn.commit()
                self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__
                log.warning("advisory worker error: %s", self.last_error)
                stop.wait(5.0)


def build_app(
    connect: Callable[[], Connection],
    users: AccessTokenValidator,
    machines: ServiceTokenVerifier,
    worker: AdvisoryWorker,
    gateway: GatewayClient | None,
) -> FastAPI:
    def readiness() -> dict[str, str]:
        # The model is optional for platform readiness: its outage degrades only advisory capability.
        model = "up" if gateway is not None and gateway.healthy() else "down"
        return {"database": "down" if worker.last_error else "up", "optional:model_gateway": model}

    app = create_app("ai", readiness)

    def caller(authorization: str = Header(default=""), x_service_token: str = Header(default="")) -> HumanPrincipal:
        machines.verify(x_service_token, {API_SA})
        if not authorization.lower().startswith("bearer "):
            raise PlatformError("AUTHENTICATION_REQUIRED", ErrorCategory.AUTHENTICATION, "access token required")
        return users.validate(authorization[7:])

    @app.post("/internal/v1/knowledge/documents", status_code=201)
    def ingest(doc: SourceDocument, who: HumanPrincipal = Depends(caller)) -> dict:
        embed = gateway.embed if gateway is not None and gateway.profile.embeddingModel else None
        with connect() as conn, transaction(conn):
            grants = current_grants(conn, who.actor.key())
            if not authorize(
                who.actor,
                set(who.roles),
                grants,
                "knowledge.ingest",
                cluster_id=CLUSTER,
                namespace=doc.namespaceScope,
                mode=current_mode(conn),
            ).allow:
                raise denied()
            model = gateway.profile.embeddingModel.name if embed else None
            result = KnowledgeStore(conn, embed=embed, embedding_model=model).ingest(doc, "knowledge-curator")
            audit(
                conn,
                correlation_id=str(uuid.uuid4()),
                action="knowledge.ingest",
                scope={"namespace": doc.namespaceScope},
                decision="allow",
                reason="ingested",
                human_ref=who.actor.key(),
                details={"documentId": doc.documentId, "redactions": result.redactions},
            )
            return result.model_dump()

    return app
