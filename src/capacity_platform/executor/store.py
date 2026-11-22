"""Executor persistence (svc_executor role): authority reads and the fenced operation journal.

Authority comes only from `governance.executor_authority_v`, which recomputes revocation,
mode, current policy and the requester's grant on every read. The executor has no write
grant on any governance table, so it cannot mint or extend approvals.

The target lock is keyed by controller (S25.5). Claiming increments the fencing epoch;
`save` commits only while the lock still carries this worker's epoch.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from capacity_platform.contracts.enums import OperatingMode
from capacity_platform.executor.operations import AuthorityBundle, FenceLost, Operation
from capacity_platform.governance.plans import ApprovalRecord, ExecutionIntent, ExecutionPlan, PlanState
from capacity_platform.shared.db import Connection, audit, emit_event, jsonb

LEASE = timedelta(seconds=60)


class SqlGovernancePort:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def get_authority(self, intent_id: str) -> AuthorityBundle | None:
        row = self.conn.execute(
            "SELECT * FROM governance.executor_authority_v WHERE intent_id = %s", (intent_id,)
        ).fetchone()
        if row is None:
            return None
        return AuthorityBundle(
            plan=ExecutionPlan.model_validate(row["plan"]),
            plan_state=PlanState(row["plan_state"]),
            approval=ApprovalRecord.model_validate(row["approval"]),
            approval_revoked=row["approval_revoked"],
            intent=ExecutionIntent.model_validate(row["intent"]),
            mode=OperatingMode(row["mode"]),
            current_policy_digest=row["current_policy_digest"] or "",
            requester_grant_valid=row["requester_grant_valid"],
        )


class SqlExecutionStore:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self._epoch: dict[str, int] = {}

    def _target_key(self, intent_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT intent->>'targetKey' AS k FROM governance.executor_authority_v WHERE intent_id = %s", (intent_id,)
        ).fetchone()
        if row is None:
            existing = self.conn.execute(
                "SELECT target_key FROM execution.operation WHERE intent_id = %s", (intent_id,)
            ).fetchone()
            return existing["target_key"] if existing else None
        return row["k"]

    def claim(self, intent_id: str, worker_id: str, now: datetime) -> int | None:
        key = self._target_key(intent_id)
        if key is None:
            return None
        with self.conn.transaction():
            row = self.conn.execute(
                """
                INSERT INTO execution.target_lock (target_key, intent_id, holder, fencing_epoch, lease_expires_at)
                VALUES (%(k)s, %(i)s, %(w)s, 1, %(exp)s)
                ON CONFLICT (target_key) DO UPDATE
                  SET intent_id = EXCLUDED.intent_id, holder = EXCLUDED.holder,
                      fencing_epoch = execution.target_lock.fencing_epoch + 1,
                      lease_expires_at = EXCLUDED.lease_expires_at
                  WHERE execution.target_lock.lease_expires_at < %(now)s
                     OR execution.target_lock.intent_id = EXCLUDED.intent_id
                     OR NOT EXISTS (SELECT 1 FROM execution.operation o
                                    WHERE o.intent_id = execution.target_lock.intent_id
                                      AND o.state NOT IN ('refused', 'verified_success', 'recovered', 'escalated'))
                RETURNING fencing_epoch
                """,
                {"k": key, "i": intent_id, "w": worker_id, "exp": now + LEASE, "now": now},
            ).fetchone()
        if row is None:
            return None  # another operation on the same controller is still open
        self._epoch[intent_id] = row["fencing_epoch"]
        return row["fencing_epoch"]

    def load(self, intent_id: str) -> Operation | None:
        row = self.conn.execute("SELECT content FROM execution.operation WHERE intent_id = %s", (intent_id,)).fetchone()
        return Operation.model_validate(row["content"]) if row else None

    def save(self, op: Operation) -> None:
        with self.conn.transaction():
            lock = self.conn.execute(
                "SELECT fencing_epoch FROM execution.target_lock WHERE target_key = %s FOR UPDATE", (op.targetKey,)
            ).fetchone()
            if lock is None or lock["fencing_epoch"] != op.fencingEpoch:
                raise FenceLost(op.intentId)
            previous = self.conn.execute(
                "SELECT state, content FROM execution.operation WHERE intent_id = %s", (op.intentId,)
            ).fetchone()
            self.conn.execute(
                """
                INSERT INTO execution.operation (intent_id, operation_id, plan_id, plan_revision, target_key, state,
                                                 attempt, fencing_epoch, content)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (intent_id) DO UPDATE SET state = EXCLUDED.state, attempt = EXCLUDED.attempt,
                    fencing_epoch = EXCLUDED.fencing_epoch, content = EXCLUDED.content, updated_at = now()
                """,
                (
                    op.intentId,
                    op.operationId,
                    op.planId,
                    op.planRevision,
                    op.targetKey,
                    op.state.value,
                    op.attempt,
                    op.fencingEpoch,
                    jsonb(op),
                ),
            )
            known = len(previous["content"]["history"]) if previous else 0
            for t in op.history[known:]:
                self.conn.execute(
                    "INSERT INTO execution.operation_transition (intent_id, from_state, to_state, reason, fence, at) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (op.intentId, t.from_state.value, t.to_state.value, t.reason, t.fence, t.at),
                )
                emit_event(
                    self.conn,
                    "execution",
                    "ExecutionStateChanged.v1",
                    op.intentId,
                    {
                        "intentId": op.intentId,
                        "planId": op.planId,
                        "revision": op.planRevision,
                        "from": t.from_state.value,
                        "to": t.to_state.value,
                        "reason": t.reason,
                    },
                    op.intentId,
                )
                audit(
                    self.conn,
                    correlation_id=op.intentId,
                    action="execution.transition",
                    scope={"targetKey": op.targetKey, "planId": op.planId},
                    decision="fact",
                    reason=t.to_state.value,
                    details={"reason": t.reason, "fence": t.fence},
                )
