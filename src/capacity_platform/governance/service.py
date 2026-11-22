"""Governance application service and its internal HTTP surface (S24, S06 §7, S27).

The `api` calls these endpoints with two credentials: its own projected ServiceAccount
token (machine identity) and the human's original access token, which Governance validates
again itself. Governance never trusts a forwarded user header and never stores the token.
It re-reads current grants, mode and policy on every sensitive command.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Literal

from fastapi import Depends, FastAPI, Header, Request
from pydantic import BaseModel, Field

from capacity_platform.contracts.canonical import digest, utcnow
from capacity_platform.contracts.enums import OperatingMode
from capacity_platform.contracts.errors import ErrorCategory, PlatformError, denied, precondition
from capacity_platform.governance.authz import Decision, authorize
from capacity_platform.governance.plans import PlanState, approve, issue_plan, request_execution
from capacity_platform.governance.store import GovernanceStore
from capacity_platform.shared.auth.oidc import AccessTokenValidator, HumanPrincipal
from capacity_platform.shared.auth.service_identity import API_SA, ServiceTokenVerifier
from capacity_platform.shared.db import Connection, audit, transaction
from capacity_platform.shared.web import correlation_id, create_app

CLUSTER_ID = "capacity-lab"
log = logging.getLogger("governance")

# Refusals that are decisions about the request (who, what state, which digest) and must be
# audited. Validation and outage errors are not decisions, so they are not recorded as denials.
AUDITED_REFUSALS = (ErrorCategory.AUTHORIZATION, ErrorCategory.PRECONDITION, ErrorCategory.CONFLICT)
# The internal authorization reason (for example a missing role). The caller only ever sees the
# generic denial, so the precise reason travels to the audit record through this request-local value.
_denial_reason: ContextVar[str | None] = ContextVar("governance_denial_reason", default=None)


class GovernanceService:
    def __init__(
        self,
        connect: Callable[[], Connection],
        clock: Callable[[], datetime] = utcnow,
        verification_policy_ref: str = "verification-policy-v1",
    ) -> None:
        self._connect, self._clock, self._verification_ref = connect, clock, verification_policy_ref

    def _decide(self, store: GovernanceStore, who: HumanPrincipal, action: str, namespace: str | None) -> Decision:
        mode, _ = store.mode()
        decision = authorize(
            who.actor,
            set(who.roles),
            store.grants_for(who.actor.key()),
            action,
            cluster_id=CLUSTER_ID,
            namespace=namespace,
            mode=mode,
        )
        if not decision.allow:
            _denial_reason.set(decision.reason)  # recorded by `_audit_refusals` after rollback
        return decision

    @contextmanager
    def _audit_refusals(self, who: HumanPrincipal, action: str, scope: dict) -> Iterator[None]:
        """Record every refused command (S06 duty separation, S24 durable denial reasons).

        Wraps the command *outside* its database transaction: a refusal rolls that transaction
        back, so a denial written inside it would vanish. Writing only after the rollback also
        avoids waiting on the audit chain lock the failed transaction may have held.
        """
        _denial_reason.set(None)
        try:
            yield
        except PlatformError as exc:
            if exc.category in AUDITED_REFUSALS:
                self._record_refusal(who, action, scope, _denial_reason.get() or exc.code)
            raise

    def _record_refusal(self, who: HumanPrincipal, action: str, scope: dict, reason: str) -> None:
        try:
            with self._connect() as conn, transaction(conn):
                audit(
                    conn,
                    correlation_id=correlation_id.get(),
                    action=action,
                    scope=scope,
                    decision="deny",
                    reason=reason,
                    human_ref=who.actor.key(),
                )
        except Exception:
            # The refusal itself still stands; a missing audit record is an alert, not an allow.
            log.error("could not audit refused %s (%s)", action, reason)

    def _scoped_plan(
        self, store: GovernanceStore, who: HumanPrincipal, plan_id: str, revision: int | None, action: str = "plan.read"
    ):
        loaded = store.load_plan(plan_id, revision)
        # Identical denial whether the plan is missing or out of scope (no existence leak).
        if loaded is None or not self._decide(store, who, action, loaded[0].target.namespace).allow:
            raise denied()
        return loaded

    def create_plan(
        self, who: HumanPrincipal, evaluation_digest: str, plan_id: str | None, advisory_digest: str | None
    ) -> dict:
        with self._audit_refusals(who, "plan.create", {"evaluationDigest": evaluation_digest}):
            return self._create_plan(who, evaluation_digest, plan_id, advisory_digest)

    def _create_plan(
        self, who: HumanPrincipal, evaluation_digest: str, plan_id: str | None, advisory_digest: str | None
    ) -> dict:
        with self._connect() as conn, transaction(conn):
            store = GovernanceStore(conn)
            evaluation = store.load_evaluation(evaluation_digest)
            ctx = store.analysis_context(evaluation_digest)
            if evaluation is None or ctx is None:
                raise denied()
            namespace = evaluation.candidate.target.namespace
            decision = self._decide(store, who, "plan.create", namespace)
            if not decision.allow:
                raise denied()
            policy = store.effective_policy(evaluation.candidate.target.logicalWorkloadId)
            if policy is None:
                raise precondition("policy_unavailable", "no current effective policy for this workload")
            advisory_status, advisory_ref = "deterministic_only", None
            if advisory_digest:
                row = conn.execute(
                    "SELECT status FROM knowledge.advisory_artifact WHERE artifact_digest = %s", (advisory_digest,)
                ).fetchone()
                if row is None:
                    raise precondition("advisory_not_found", "advisory artifact does not exist")
                advisory_status = "complete" if row["status"] == "completed" else "incomplete"
                advisory_ref = advisory_digest
            mode, _ = store.mode()
            context = ctx["context"] or {}
            pid = plan_id or f"plan-{uuid.uuid4().hex[:12]}"
            plan, state = issue_plan(
                plan_id=pid,
                revision=store.next_revision(pid),
                author=who.actor,
                author_decision=decision,
                evaluation=evaluation,
                current_policy_digest=policy["policy_digest"],
                ownership=context.get("ownership") or "unsupported",
                mode=mode,
                advisory_status=advisory_status,
                advisory_ref=advisory_ref,
                deterministic_only_execution_allowed=policy["deterministic_only_execution_allowed"],
                verification_policy_ref=self._verification_ref,
                workload_kind=context.get("workloadKind") or "",
                criticality=context.get("criticality") or "",
                resource_version=context.get("resourceVersion") or "",
                now=self._clock(),
            )
            store.insert_plan(plan, state, who.actor.key(), correlation_id.get())
            return {"plan": plan.model_dump(mode="json"), "state": state.value}

    def get_plan(self, who: HumanPrincipal, plan_id: str, revision: int | None) -> dict:
        with self._audit_refusals(who, "plan.read", {"planId": plan_id, "revision": revision}):
            return self._get_plan(who, plan_id, revision)

    def _get_plan(self, who: HumanPrincipal, plan_id: str, revision: int | None) -> dict:
        with self._connect() as conn:
            store = GovernanceStore(conn)
            plan, state, _ = self._scoped_plan(store, who, plan_id, revision)
            approval = store.approval_for(plan.planId, plan.revision)
            return {
                "plan": plan.model_dump(mode="json"),
                "state": state.value,
                "approval": approval[0].model_dump(mode="json") if approval else None,
                "approvalRevoked": approval[1] if approval else None,
            }

    def decide(
        self, who: HumanPrincipal, plan_id: str, revision: int, plan_digest: str, decision: Literal["approve", "reject"]
    ) -> dict:
        action = "plan.approve" if decision == "approve" else "plan.reject"
        with self._audit_refusals(who, action, {"planId": plan_id, "revision": revision}):
            return self._decide_plan(who, plan_id, revision, plan_digest, decision)

    def _decide_plan(
        self, who: HumanPrincipal, plan_id: str, revision: int, plan_digest: str, decision: Literal["approve", "reject"]
    ) -> dict:
        with self._connect() as conn, transaction(conn):
            store = GovernanceStore(conn)
            plan, state, version = self._scoped_plan(store, who, plan_id, revision)
            now = self._clock()
            if state == PlanState.AWAITING_APPROVAL and now >= plan.expiresAtUtc:
                store.transition(
                    plan_id, revision, PlanState.EXPIRED, "plan_ttl", "system", version, correlation_id.get()
                )
                raise precondition("plan_expired", "plan revision has expired")
            action = "plan.approve" if decision == "approve" else "plan.reject"
            verdict = self._decide(store, who, action, plan.target.namespace)
            mode, _ = store.mode()
            record = approve(
                plan=plan,
                state=state,
                approver=who.actor,
                approver_decision=verdict,
                requested_revision=revision,
                requested_digest=plan_digest,
                approval_id=f"appr-{uuid.uuid4().hex[:12]}",
                mode=mode,
                now=now,
            )
            if decision == "reject":
                record = record.model_copy(update={"decision": "rejected", "reasonCode": "explicitly_rejected"})
                record = record.model_copy(
                    update={"approvalDigest": digest(record, exclude=frozenset({"approvalDigest"}))}
                )
            store.insert_approval(record, plan.createdBy.key(), version, correlation_id.get())
            return record.model_dump(mode="json")

    def revoke(self, who: HumanPrincipal, plan_id: str, revision: int, reason: str) -> dict:
        with self._audit_refusals(who, "approval.revoke", {"planId": plan_id, "revision": revision}):
            return self._revoke(who, plan_id, revision, reason)

    def _revoke(self, who: HumanPrincipal, plan_id: str, revision: int, reason: str) -> dict:
        with self._connect() as conn, transaction(conn):
            store = GovernanceStore(conn)
            plan, state, version = self._scoped_plan(store, who, plan_id, revision)
            if not self._decide(store, who, "approval.revoke", plan.target.namespace).allow:
                raise denied()
            approval = store.approval_for(plan_id, revision)
            if approval is None:
                raise precondition("no_approval", "plan has no approval to revoke")
            store.revoke_approval(approval[0].approvalId, who.actor.key(), reason, correlation_id.get())
            if state == PlanState.APPROVED:
                store.transition(
                    plan_id, revision, PlanState.REVOKED, reason, who.actor.key(), version, correlation_id.get()
                )
            return {"approvalId": approval[0].approvalId, "revoked": True}

    def request_execution(self, who: HumanPrincipal, plan_id: str, revision: int, idempotency_key: str) -> dict:
        with self._audit_refusals(who, "execution.request", {"planId": plan_id, "revision": revision}):
            return self._request_execution(who, plan_id, revision, idempotency_key)

    def _request_execution(self, who: HumanPrincipal, plan_id: str, revision: int, idempotency_key: str) -> dict:
        if not idempotency_key or len(idempotency_key) > 128:
            raise PlatformError("IDEMPOTENCY_KEY_REQUIRED", ErrorCategory.VALIDATION, "Idempotency-Key header required")
        request_digest = digest({"plan": plan_id, "revision": revision})
        scope_key = f"{who.actor.key()}|execution.request|{plan_id}|{idempotency_key}"
        with self._connect() as conn, transaction(conn):
            store = GovernanceStore(conn)
            previous = store.idempotent_result(scope_key, request_digest)
            if previous is not None:
                return previous  # same request retried: same intent, no second authority
            plan, state, version = self._scoped_plan(store, who, plan_id, revision)
            verdict = self._decide(store, who, "execution.request", plan.target.namespace)
            approval = store.approval_for(plan_id, revision)
            if approval is None:
                raise precondition("plan_not_approved", "no approval for this revision")
            mode, _ = store.mode()
            intent = request_execution(
                plan=plan,
                state=state,
                approval=approval[0],
                approval_revoked=approval[1],
                requester=who.actor,
                requester_decision=verdict,
                mode=mode,
                intent_id=f"intent-{uuid.uuid4().hex[:12]}",
                idempotency_key=idempotency_key,
                now=self._clock(),
            )
            store.insert_intent(intent, version, request_digest, scope_key, correlation_id.get())
            return {"intentId": intent.intentId}

    def set_mode(self, who: HumanPrincipal, mode: OperatingMode, expected_revision: int) -> dict:
        with self._audit_refusals(who, "mode.set", {"mode": mode.value, "expectedRevision": expected_revision}):
            return self._set_mode(who, mode, expected_revision)

    def _set_mode(self, who: HumanPrincipal, mode: OperatingMode, expected_revision: int) -> dict:
        with self._connect() as conn, transaction(conn):
            store = GovernanceStore(conn)
            if not self._decide(store, who, "mode.set", None).allow:
                raise denied()
            return {
                "mode": mode.value,
                "revision": store.set_mode(mode, who.actor.key(), expected_revision, correlation_id.get()),
            }


# ------------------------------------------------------------------------------- HTTP surface


class CreatePlanRequest(BaseModel):
    evaluationDigest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    planId: str | None = Field(default=None, pattern=r"^plan-[a-z0-9-]{4,40}$")
    advisoryArtifactDigest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")


class DecisionRequest(BaseModel):
    revision: int = Field(gt=0)
    planDigest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    decision: Literal["approve", "reject"]


class RevokeRequest(BaseModel):
    revision: int = Field(gt=0)
    reason: str = Field(min_length=3, max_length=200)


class ExecutionRequest(BaseModel):
    revision: int = Field(gt=0)


class ModeRequest(BaseModel):
    mode: OperatingMode
    expectedRevision: int = Field(gt=0)


def build_app(
    service: GovernanceService,
    users: AccessTokenValidator,
    machines: ServiceTokenVerifier,
    readiness: Callable[[], dict[str, str]] | None = None,
    mtls_port: int | None = None,
) -> FastAPI:
    app = create_app("governance", readiness)

    def caller(
        request: Request, authorization: str = Header(default=""), x_service_token: str = Header(default="")
    ) -> HumanPrincipal:
        if mtls_port is not None and (request.scope.get("server") or (None, None))[1] != mtls_port:
            # Arrived on the TLS-only port: privileged commands require the mutual-TLS listener.
            raise PlatformError("MTLS_REQUIRED", ErrorCategory.AUTHENTICATION, "mutual TLS required")
        machines.verify(x_service_token, {API_SA})
        if not authorization.lower().startswith("bearer "):
            raise PlatformError("AUTHENTICATION_REQUIRED", ErrorCategory.AUTHENTICATION, "access token required")
        return users.validate(authorization[7:])

    @app.post("/internal/v1/plans", status_code=201)
    def create_plan(body: CreatePlanRequest, who: HumanPrincipal = Depends(caller)) -> dict:
        return service.create_plan(who, body.evaluationDigest, body.planId, body.advisoryArtifactDigest)

    @app.get("/internal/v1/plans/{plan_id}")
    def get_plan(plan_id: str, revision: int | None = None, who: HumanPrincipal = Depends(caller)) -> dict:
        return service.get_plan(who, plan_id, revision)

    @app.post("/internal/v1/plans/{plan_id}/decisions", status_code=201)
    def decide(plan_id: str, body: DecisionRequest, who: HumanPrincipal = Depends(caller)) -> dict:
        return service.decide(who, plan_id, body.revision, body.planDigest, body.decision)

    @app.post("/internal/v1/plans/{plan_id}/revocations", status_code=201)
    def revoke(plan_id: str, body: RevokeRequest, who: HumanPrincipal = Depends(caller)) -> dict:
        return service.revoke(who, plan_id, body.revision, body.reason)

    @app.post("/internal/v1/plans/{plan_id}/execution-requests", status_code=202)
    def execute(
        plan_id: str,
        body: ExecutionRequest,
        idempotency_key: str = Header(default=""),
        who: HumanPrincipal = Depends(caller),
    ) -> dict:
        return service.request_execution(who, plan_id, body.revision, idempotency_key)

    @app.put("/internal/v1/mode")
    def set_mode(body: ModeRequest, who: HumanPrincipal = Depends(caller)) -> dict:
        return service.set_mode(who, body.mode, body.expectedRevision)

    return app
