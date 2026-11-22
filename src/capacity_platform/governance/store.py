"""Governance persistence (svc_governance role). One writer for plans, approvals, intents.

Every state change writes the new state with compare-and-swap on `version`, an append-only
transition row, an outbox event and an audit fact in ONE transaction. Raw bearer tokens are
never persisted; only issuer+subject references.
"""

from __future__ import annotations

from typing import Any

from capacity_platform.capacity.simulation.evaluate import OptimizationEvaluation
from capacity_platform.contracts.enums import HumanRole, OperatingMode
from capacity_platform.contracts.errors import ErrorCategory, PlatformError, precondition
from capacity_platform.governance.authz import Grant
from capacity_platform.governance.plans import (
    ApprovalRecord,
    ExecutionIntent,
    ExecutionPlan,
    PlanState,
    check_transition,
)
from capacity_platform.shared import jobs
from capacity_platform.shared.db import Connection, audit, emit_event, jsonb


class GovernanceStore:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    # ---------------------------------------------------------------- mode and grants
    def mode(self) -> tuple[OperatingMode, int]:
        row = self.conn.execute("SELECT mode, revision FROM governance.operating_mode").fetchone()
        return OperatingMode(row["mode"]), row["revision"]

    def set_mode(self, mode: OperatingMode, actor_key: str, expected_revision: int, correlation_id: str) -> int:
        row = self.conn.execute(
            "UPDATE governance.operating_mode SET mode = %s, revision = revision + 1, changed_by = %s, "
            "changed_at = now() "
            "WHERE revision = %s RETURNING revision",
            (mode.value, actor_key, expected_revision),
        ).fetchone()
        if row is None:
            raise precondition("mode_revision_conflict", "operating mode changed concurrently")
        audit(
            self.conn,
            correlation_id=correlation_id,
            action="mode.set",
            scope={"cluster": "capacity-lab"},
            decision="fact",
            reason=f"mode_{mode.value}",
            human_ref=actor_key,
        )
        return row["revision"]

    def grants_for(self, subject_key: str) -> list[Grant]:
        rows = self.conn.execute(
            "SELECT grant_id, subject_key, role, cluster_id, namespaces, revision, revoked_at "
            "FROM governance.role_grant WHERE subject_key = %s",
            (subject_key,),
        ).fetchall()
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

    def add_grant(self, grant: Grant, granted_by: str) -> None:
        self.conn.execute(
            "INSERT INTO governance.role_grant (grant_id, subject_key, role, cluster_id, namespaces, revision, "
            "granted_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                grant.grant_id,
                grant.subject_key,
                grant.role.value,
                grant.cluster_id,
                sorted(grant.namespaces),
                grant.revision,
                granted_by,
            ),
        )

    def revoke_grant(self, grant_id: str, revoked_by: str) -> None:
        self.conn.execute(
            "UPDATE governance.role_grant SET revoked_at = now(), revoked_by = %s "
            "WHERE grant_id = %s AND revoked_at IS NULL",
            (revoked_by, grant_id),
        )

    # ---------------------------------------------------------------- policy
    def effective_policy(self, workload_id: str) -> dict[str, Any] | None:
        return self.conn.execute(
            "SELECT * FROM governance.effective_policy WHERE workload_id = %s", (workload_id,)
        ).fetchone()

    def set_effective_policy(
        self, workload_id: str, policy_id: str, revision: int, policy_digest: str, deterministic_only_allowed: bool
    ) -> None:
        self.conn.execute(
            "INSERT INTO governance.effective_policy (workload_id, policy_id, policy_revision, policy_digest, "
            "deterministic_only_execution_allowed) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (workload_id) DO UPDATE SET "
            "policy_id = EXCLUDED.policy_id, policy_revision = EXCLUDED.policy_revision, "
            "policy_digest = EXCLUDED.policy_digest, deterministic_only_execution_allowed = "
            "EXCLUDED.deterministic_only_execution_allowed, updated_at = now()",
            (workload_id, policy_id, revision, policy_digest, deterministic_only_allowed),
        )

    # ---------------------------------------------------------------- capacity evidence (read-only)
    def load_evaluation(self, integrity_digest: str) -> OptimizationEvaluation | None:
        row = self.conn.execute(
            "SELECT content FROM capacity.evaluation WHERE integrity_digest = %s", (integrity_digest,)
        ).fetchone()
        return OptimizationEvaluation.model_validate(row["content"]) if row else None

    def analysis_context(self, integrity_digest: str) -> dict[str, Any] | None:
        return self.conn.execute(
            "SELECT a.result->'context' AS context, a.result->>'targetKey' AS target_key, a.workload_id "
            "FROM capacity.evaluation e JOIN capacity.analysis a ON a.analysis_digest = e.analysis_digest "
            "WHERE e.integrity_digest = %s",
            (integrity_digest,),
        ).fetchone()

    # ---------------------------------------------------------------- plans
    def next_revision(self, plan_id: str) -> int:
        row = self.conn.execute(
            "SELECT coalesce(max(revision), 0) + 1 AS r FROM governance.plan_revision WHERE plan_id = %s", (plan_id,)
        ).fetchone()
        return row["r"]

    def insert_plan(self, plan: ExecutionPlan, state: PlanState, actor_key: str, correlation_id: str) -> None:
        # Earlier, still-open revisions of the same plan are superseded (S24 §2 revision policy).
        for prev in self.conn.execute(
            "SELECT revision, state, version FROM governance.plan_state WHERE plan_id = %s "
            "AND state IN ('Draft', 'AwaitingApproval', 'Approved')",
            (plan.planId,),
        ).fetchall():
            self.transition(
                plan.planId,
                prev["revision"],
                PlanState.SUPERSEDED,
                "new_revision_issued",
                actor_key,
                prev["version"],
                correlation_id,
            )
        self.conn.execute(
            "INSERT INTO governance.plan_revision (plan_id, revision, plan_digest, author_key, namespace, workload_id, "
            "target_key, content) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                plan.planId,
                plan.revision,
                plan.planDigest,
                plan.createdBy.key(),
                plan.target.namespace,
                plan.target.logicalWorkloadId,
                plan.target.target_key(),
                jsonb(plan),
            ),
        )
        self.conn.execute(
            "INSERT INTO governance.plan_state (plan_id, revision, state) VALUES (%s, %s, %s)",
            (plan.planId, plan.revision, state.value),
        )
        self.conn.execute(
            "INSERT INTO governance.plan_transition (plan_id, revision, from_state, to_state, reason, actor_key) "
            "VALUES (%s, %s, NULL, %s, %s, %s)",
            (plan.planId, plan.revision, state.value, ",".join(plan.risk.governanceReasons) or "issued", actor_key),
        )
        emit_event(
            self.conn,
            "governance",
            "PlanRevisionIssued.v1",
            f"{plan.planId}/{plan.revision}",
            {"planId": plan.planId, "revision": plan.revision, "planDigest": plan.planDigest, "state": state.value},
            correlation_id,
        )
        audit(
            self.conn,
            correlation_id=correlation_id,
            action="plan.create",
            scope={"namespace": plan.target.namespace, "planId": plan.planId, "revision": plan.revision},
            decision="allow",
            reason=state.value,
            human_ref=actor_key,
            details={"planDigest": plan.planDigest},
        )

    def load_plan(self, plan_id: str, revision: int | None = None) -> tuple[ExecutionPlan, PlanState, int] | None:
        query = (
            "SELECT p.content, s.state, s.version FROM governance.plan_revision p JOIN governance.plan_state s "
            "USING (plan_id, revision) WHERE p.plan_id = %s "
        )
        args: tuple = (plan_id,)
        if revision is None:
            query += "ORDER BY p.revision DESC LIMIT 1"
        else:
            query += "AND p.revision = %s"
            args = (plan_id, revision)
        row = self.conn.execute(query, args).fetchone()
        if row is None:
            return None
        return ExecutionPlan.model_validate(row["content"]), PlanState(row["state"]), row["version"]

    def transition(
        self,
        plan_id: str,
        revision: int,
        new: PlanState,
        reason: str,
        actor_key: str,
        expected_version: int,
        correlation_id: str,
    ) -> int:
        current = self.conn.execute(
            "SELECT state FROM governance.plan_state WHERE plan_id = %s AND revision = %s", (plan_id, revision)
        ).fetchone()
        if current is None:
            raise PlatformError("NOT_FOUND", ErrorCategory.NOT_FOUND, "plan not found")
        check_transition(PlanState(current["state"]), new)
        row = self.conn.execute(
            "UPDATE governance.plan_state SET state = %s, version = version + 1, updated_at = now() "
            "WHERE plan_id = %s AND revision = %s AND version = %s RETURNING version",
            (new.value, plan_id, revision, expected_version),
        ).fetchone()
        if row is None:
            raise precondition("plan_state_conflict", "plan state changed concurrently; re-read and retry")
        self.conn.execute(
            "INSERT INTO governance.plan_transition (plan_id, revision, from_state, to_state, reason, actor_key) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (plan_id, revision, current["state"], new.value, reason, actor_key),
        )
        emit_event(
            self.conn,
            "governance",
            "PlanStateChanged.v1",
            f"{plan_id}/{revision}",
            {"planId": plan_id, "revision": revision, "from": current["state"], "to": new.value, "reason": reason},
            correlation_id,
        )
        return row["version"]

    # ---------------------------------------------------------------- approvals and intents
    def insert_approval(self, record: ApprovalRecord, author_key: str, plan_version: int, correlation_id: str) -> None:
        self.conn.execute(
            "INSERT INTO governance.approval (approval_id, plan_id, revision, plan_digest, decision, approver_key, "
            "author_key, content, approval_digest, issued_at, expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                record.approvalId,
                record.planId,
                record.planRevision,
                record.planDigest,
                record.decision,
                record.approver.key(),
                author_key,
                jsonb(record),
                record.approvalDigest,
                record.issuedAtUtc,
                record.expiresAtUtc,
            ),
        )
        new_state = PlanState.APPROVED if record.decision == "approved" else PlanState.REJECTED
        self.transition(
            record.planId,
            record.planRevision,
            new_state,
            record.reasonCode,
            record.approver.key(),
            plan_version,
            correlation_id,
        )
        audit(
            self.conn,
            correlation_id=correlation_id,
            action="plan.approve",
            scope={"planId": record.planId, "revision": record.planRevision, "namespace": record.binding.namespace},
            decision="allow",
            reason=record.decision,
            human_ref=record.approver.key(),
            details={"planDigest": record.planDigest, "approvalDigest": record.approvalDigest},
        )

    def approval_for(self, plan_id: str, revision: int) -> tuple[ApprovalRecord, bool] | None:
        row = self.conn.execute(
            "SELECT a.content, (r.approval_id IS NOT NULL) AS revoked FROM governance.approval a "
            "LEFT JOIN governance.approval_revocation r USING (approval_id) "
            "WHERE a.plan_id = %s AND a.revision = %s AND a.decision = 'approved'",
            (plan_id, revision),
        ).fetchone()
        return (ApprovalRecord.model_validate(row["content"]), row["revoked"]) if row else None

    def revoke_approval(self, approval_id: str, actor_key: str, reason: str, correlation_id: str) -> None:
        self.conn.execute(
            "INSERT INTO governance.approval_revocation (approval_id, revoked_by, reason) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (approval_id, actor_key, reason),
        )
        audit(
            self.conn,
            correlation_id=correlation_id,
            action="approval.revoke",
            scope={"approvalId": approval_id},
            decision="allow",
            reason=reason,
            human_ref=actor_key,
        )

    def idempotent_result(self, scope_key: str, request_digest: str) -> dict | None:
        row = self.conn.execute(
            "SELECT request_digest, response FROM governance.idempotency WHERE scope_key = %s", (scope_key,)
        ).fetchone()
        if row is None:
            return None
        if row["request_digest"] != request_digest:
            raise PlatformError(
                "IDEMPOTENCY_CONFLICT", ErrorCategory.CONFLICT, "idempotency key was used with a different request"
            )
        return row["response"]

    def insert_intent(
        self, intent: ExecutionIntent, plan_version: int, request_digest: str, scope_key: str, correlation_id: str
    ) -> None:
        self.conn.execute(
            "INSERT INTO governance.execution_intent (intent_id, plan_id, revision, approval_id, requester_key, "
            "idempotency_key, request_digest, content, intent_digest, expires_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                intent.intentId,
                intent.planId,
                intent.planRevision,
                intent.approvalId,
                intent.requester.key(),
                intent.idempotencyKey,
                request_digest,
                jsonb(intent),
                intent.intentDigest,
                intent.expiresAtUtc,
            ),
        )
        self.transition(
            intent.planId,
            intent.planRevision,
            PlanState.EXECUTION_REQUESTED,
            "execution_requested",
            intent.requester.key(),
            plan_version,
            correlation_id,
        )
        # Durable handoff: the job carries only the intent ID; the executor re-reads authority itself.
        jobs.enqueue(
            self.conn,
            queue="executor",
            kind="execute_intent",
            payload={"intentId": intent.intentId},
            idempotency_key=f"intent:{intent.intentId}",
            correlation_id=correlation_id,
            max_attempts=5,
        )
        self.conn.execute(
            "INSERT INTO governance.idempotency (scope_key, request_digest, response) VALUES (%s, %s, %s)",
            (scope_key, request_digest, jsonb({"intentId": intent.intentId})),
        )
        audit(
            self.conn,
            correlation_id=correlation_id,
            action="execution.request",
            scope={"planId": intent.planId, "revision": intent.planRevision},
            decision="allow",
            reason="intent_created",
            human_ref=intent.requester.key(),
            details={"intentDigest": intent.intentDigest},
        )
