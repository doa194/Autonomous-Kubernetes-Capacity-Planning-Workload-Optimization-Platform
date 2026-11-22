"""S24 immutable plan revisions, exact human approval and execution intents.

Three separate aggregates with separate state:
* `ExecutionPlan` — immutable content `(planId, revision)` identified by `planDigest`.
  Any change to fields, target, policy, adapter or recovery envelope is a NEW revision.
* `ApprovalRecord` — one independent human approval bound to the exact plan digest,
  target, fields, policy, evidence, recovery envelope and an expiry.
* `ExecutionIntent` — a distinct, separately authorized request to execute. It is a durable
  handoff to the executor, not a bearer credential: the executor re-reads authority itself.

Only trusted Governance code populates `allRequiredHardGatesPassed`; callers cannot assert it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from capacity_platform.capacity.simulation.evaluate import OptimizationEvaluation
from capacity_platform.contracts.canonical import digest
from capacity_platform.contracts.enums import Eligibility, GateStatus, OperatingMode, ResourceField
from capacity_platform.contracts.errors import ErrorCategory, PlatformError, denied, precondition
from capacity_platform.contracts.identity import ActorRef, TargetRef
from capacity_platform.governance.authz import Decision

# Provisional governance policy (S24 §4 "15 min is provisional"); pinned by revision.
GOVERNANCE_POLICY_REVISION = "governance-policy-v1"
PLAN_TTL = timedelta(minutes=60)
APPROVAL_TTL = timedelta(minutes=15)
INTENT_TTL = timedelta(minutes=15)
MAX_INVERSE_ATTEMPTS = 1


class PlanState(StrEnum):
    DRAFT = "Draft"
    RISK_EVALUATING = "RiskEvaluating"
    BLOCKED = "Blocked"
    ADVISORY_ONLY = "AdvisoryOnly"
    AWAITING_APPROVAL = "AwaitingApproval"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    EXPIRED = "Expired"
    REVOKED = "Revoked"
    SUPERSEDED = "Superseded"
    EXECUTION_REQUESTED = "ExecutionRequested"
    CONSUMED_BY_EXECUTOR = "ConsumedByExecutor"
    REFUSED = "Refused"
    OUTCOME_RECORDED = "OutcomeRecorded"


S = PlanState
# S24.5 legal transitions. Anything not listed is rejected.
TRANSITIONS: dict[PlanState, frozenset[PlanState]] = {
    S.DRAFT: frozenset({S.RISK_EVALUATING, S.SUPERSEDED}),
    S.RISK_EVALUATING: frozenset({S.BLOCKED, S.AWAITING_APPROVAL, S.ADVISORY_ONLY}),
    S.AWAITING_APPROVAL: frozenset({S.REJECTED, S.APPROVED, S.EXPIRED, S.SUPERSEDED}),
    S.APPROVED: frozenset({S.REVOKED, S.EXPIRED, S.EXECUTION_REQUESTED, S.SUPERSEDED}),
    S.EXECUTION_REQUESTED: frozenset({S.CONSUMED_BY_EXECUTOR, S.REFUSED}),
    S.CONSUMED_BY_EXECUTOR: frozenset({S.OUTCOME_RECORDED}),
}


def check_transition(current: PlanState, new: PlanState) -> None:
    if new not in TRANSITIONS.get(current, frozenset()):
        raise precondition("illegal_plan_transition", f"{current} -> {new} is not a legal transition")


class FieldSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    field: ResourceField
    before: int | None
    after: int | None


class PlanRisk(BaseModel):
    model_config = ConfigDict(frozen=True)
    findings: list[dict]
    allRequiredHardGatesPassed: bool
    governanceReasons: list[str]


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schemaVersion: str = "execution-plan/v1alpha1"
    planId: str
    revision: int = Field(gt=0)
    createdAtUtc: datetime
    expiresAtUtc: datetime
    createdBy: ActorRef
    modeAtCreation: OperatingMode
    intendedUse: Literal["governed_execution", "advisory"]
    target: TargetRef
    workloadKind: str
    workloadCriticality: str
    resourceVersionAtAnalysis: str
    approvedFieldChanges: tuple[FieldSet, ...]
    inverseFieldChanges: tuple[FieldSet, ...]
    candidateDigest: str
    evaluationDigest: str
    policyDigest: str
    evidenceManifestDigest: str
    advisoryReviewStatus: Literal["deterministic_only", "complete", "incomplete"]
    advisoryReviewRef: str | None = None
    risk: PlanRisk
    adapter: Literal["direct_kubernetes", "gitops"]
    verificationPolicyRef: str
    governancePolicyRevision: str = GOVERNANCE_POLICY_REVISION
    maxInverseAttempts: int = MAX_INVERSE_ATTEMPTS
    planDigest: str = ""

    def finalize(self) -> ExecutionPlan:
        return self.model_copy(update={"planDigest": digest(self, exclude=frozenset({"planDigest"}))})

    def change_digest(self) -> str:
        return digest([fs.model_dump() for fs in self.approvedFieldChanges])

    def recovery_digest(self) -> str:
        return digest([fs.model_dump() for fs in self.inverseFieldChanges])


class ApprovalBinding(BaseModel):
    model_config = ConfigDict(frozen=True)
    clusterIncarnationRef: str
    namespace: str
    controllerUid: str
    containerName: str
    approvedChangeDigest: str
    evidenceManifestDigest: str
    effectivePolicyDigest: str
    adapter: str
    recoveryEnvelopeDigest: str
    verificationPolicyRef: str


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(frozen=True)
    schemaVersion: str = "approval-record/v1alpha1"
    approvalId: str
    planId: str
    planRevision: int
    planDigest: str
    decision: Literal["approved", "rejected"]
    approver: ActorRef
    grantRevision: int | None
    binding: ApprovalBinding
    issuedAtUtc: datetime
    expiresAtUtc: datetime
    reasonCode: str
    approvalDigest: str = ""


class ExecutionIntent(BaseModel):
    model_config = ConfigDict(frozen=True)
    schemaVersion: str = "execution-intent/v1alpha1"
    intentId: str
    planId: str
    planRevision: int
    planDigest: str
    approvalId: str
    approvalDigest: str
    requester: ActorRef
    requesterGrantRevision: int | None
    createdAtUtc: datetime
    expiresAtUtc: datetime
    targetKey: str
    operationDigest: str
    adapter: str
    recoveryDigest: str
    idempotencyKey: str
    intentDigest: str = ""


# --------------------------------------------------------------------------- issuance


def issue_plan(
    *,
    plan_id: str,
    revision: int,
    author: ActorRef,
    author_decision: Decision,
    evaluation: OptimizationEvaluation,
    current_policy_digest: str,
    ownership: str,
    mode: OperatingMode,
    advisory_status: Literal["deterministic_only", "complete", "incomplete"],
    advisory_ref: str | None,
    deterministic_only_execution_allowed: bool | None,
    verification_policy_ref: str,
    workload_kind: str,
    criticality: str,
    resource_version: str,
    now: datetime,
) -> tuple[ExecutionPlan, PlanState]:
    """Deterministic S24 issuance (S24.2.2). Returns the immutable plan and its initial
    lifecycle state: AwaitingApproval, AdvisoryOnly or Blocked. Never Approved."""
    if not author_decision.allow:
        raise denied()
    cand = evaluation.candidate
    if evaluation.policyDigest != cand.source.effectivePolicyDigest:
        raise precondition("evidence_invalid", "evaluation and candidate disagree on policy")
    if digest(evaluation, exclude=frozenset({"integrityDigest"})) != evaluation.integrityDigest:
        raise precondition("evidence_invalid", "evaluation integrity digest does not verify")

    reasons: list[str] = []
    gates = [g.model_dump() for g in evaluation.hardGates]
    required = [g for g in evaluation.hardGates if g.required and g.status != GateStatus.NOT_APPLICABLE]
    all_pass = evaluation.status == Eligibility.QUALIFIED and all(g.status == GateStatus.PASS for g in required)
    if not all_pass:
        reasons.append(
            "hard_gate_failed" if any(g.status == GateStatus.FAIL for g in required) else "hard_gate_unknown"
        )
    if current_policy_digest != evaluation.policyDigest:
        reasons.append("policy_superseded")
    if workload_kind != "Deployment":
        reasons.append("unsupported_action")
    adapter: Literal["direct_kubernetes", "gitops"] = "direct_kubernetes"
    if ownership == "gitops_managed":
        adapter = "gitops"
        reasons.append("adapter_unqualified")  # Flux path is P1 and not qualified in this release
    elif ownership != "direct_managed":
        reasons.append("ownership_ambiguous")
    if advisory_status == "incomplete":
        reasons.append("review_incomplete")
    if advisory_status == "deterministic_only" and deterministic_only_execution_allowed is not True:
        # S24.3 H: if the policy does not explicitly allow deterministic-only execution, block.
        reasons.append("deterministic_only_policy_unset")

    fields = tuple(FieldSet(field=f.field, before=f.before, after=f.after) for f in cand.fields)
    inverse = tuple(FieldSet(field=f.field, before=f.after, after=f.before) for f in cand.fields)
    intended: Literal["governed_execution", "advisory"] = (
        "governed_execution" if mode == OperatingMode.GOVERNED else "advisory"
    )
    plan = ExecutionPlan(
        planId=plan_id,
        revision=revision,
        createdAtUtc=now,
        expiresAtUtc=now + PLAN_TTL,
        createdBy=author,
        modeAtCreation=mode,
        intendedUse=intended,
        target=cand.target,
        workloadKind=workload_kind,
        workloadCriticality=criticality,
        resourceVersionAtAnalysis=resource_version,
        approvedFieldChanges=fields,
        inverseFieldChanges=inverse,
        candidateDigest=cand.candidateDigest,
        evaluationDigest=evaluation.integrityDigest,
        policyDigest=evaluation.policyDigest,
        evidenceManifestDigest=cand.source.evidenceManifestDigest,
        advisoryReviewStatus=advisory_status,
        advisoryReviewRef=advisory_ref,
        risk=PlanRisk(findings=gates, allRequiredHardGatesPassed=all_pass, governanceReasons=sorted(set(reasons))),
        adapter=adapter,
        verificationPolicyRef=verification_policy_ref,
    ).finalize()
    if reasons:
        blocking = set(reasons) - {"deterministic_only_policy_unset", "review_incomplete", "adapter_unqualified"}
        state = S.BLOCKED if blocking else S.ADVISORY_ONLY
    elif mode != OperatingMode.GOVERNED:
        state = S.ADVISORY_ONLY
    else:
        state = S.AWAITING_APPROVAL
    return plan, state


# --------------------------------------------------------------------------- approval


def approve(
    *,
    plan: ExecutionPlan,
    state: PlanState,
    approver: ActorRef,
    approver_decision: Decision,
    requested_revision: int,
    requested_digest: str,
    approval_id: str,
    mode: OperatingMode,
    now: datetime,
) -> ApprovalRecord:
    if not approver_decision.allow:
        raise denied()
    if mode != OperatingMode.GOVERNED:
        raise precondition("mode_not_governed", "approvals are only accepted in Governed mode")
    if requested_revision != plan.revision or requested_digest != plan.planDigest:
        # The human must approve exactly what they saw; a changed payload is a different plan.
        raise precondition("plan_digest_mismatch", "approval does not match the current plan revision/digest")
    if state != S.AWAITING_APPROVAL:
        raise precondition("plan_not_awaiting_approval", f"plan is {state}")
    if now >= plan.expiresAtUtc:
        raise precondition("plan_expired", "plan revision has expired")
    if approver.kind != "human":
        raise denied("machine_principal_not_permitted")
    if approver.key() == plan.createdBy.key():
        raise PlatformError(
            "separation_of_duties", ErrorCategory.AUTHORIZATION, "the plan author may not approve the same plan"
        )
    if not plan.risk.allRequiredHardGatesPassed or plan.risk.governanceReasons:
        raise precondition("hard_gate_unknown", "plan is not governance-qualified")
    binding = ApprovalBinding(
        clusterIncarnationRef=plan.target.cluster.incarnationId,
        namespace=plan.target.namespace,
        controllerUid=plan.target.controller.uid,
        containerName=plan.target.containerName,
        approvedChangeDigest=plan.change_digest(),
        evidenceManifestDigest=plan.evidenceManifestDigest,
        effectivePolicyDigest=plan.policyDigest,
        adapter=plan.adapter,
        recoveryEnvelopeDigest=plan.recovery_digest(),
        verificationPolicyRef=plan.verificationPolicyRef,
    )
    record = ApprovalRecord(
        approvalId=approval_id,
        planId=plan.planId,
        planRevision=plan.revision,
        planDigest=plan.planDigest,
        decision="approved",
        approver=approver,
        grantRevision=approver_decision.grant_revision,
        binding=binding,
        issuedAtUtc=now,
        expiresAtUtc=min(now + APPROVAL_TTL, plan.expiresAtUtc),
        reasonCode="explicitly_approved",
    )
    return record.model_copy(update={"approvalDigest": digest(record, exclude=frozenset({"approvalDigest"}))})


def approval_effective(approval: ApprovalRecord, plan: ExecutionPlan, *, revoked: bool, now: datetime) -> str | None:
    """Return a refusal reason, or None if the approval still authorizes this exact plan."""
    if revoked:
        return "approval_revoked"
    if approval.decision != "approved":
        return "approval_not_granted"
    if now >= approval.expiresAtUtc:
        return "approval_expired"
    if approval.planDigest != plan.planDigest or approval.planRevision != plan.revision:
        return "plan_digest_mismatch"
    if digest(approval, exclude=frozenset({"approvalDigest"})) != approval.approvalDigest:
        return "approval_integrity_invalid"
    if approval.binding.approvedChangeDigest != plan.change_digest():
        return "plan_digest_mismatch"
    if approval.binding.recoveryEnvelopeDigest != plan.recovery_digest():
        return "plan_digest_mismatch"
    return None


# --------------------------------------------------------------------------- execution request


def request_execution(
    *,
    plan: ExecutionPlan,
    state: PlanState,
    approval: ApprovalRecord,
    approval_revoked: bool,
    requester: ActorRef,
    requester_decision: Decision,
    mode: OperatingMode,
    intent_id: str,
    idempotency_key: str,
    now: datetime,
) -> ExecutionIntent:
    if not requester_decision.allow:
        raise denied()
    if mode != OperatingMode.GOVERNED:
        raise precondition("mode_not_governed", "execution requests require Governed mode")
    if state != S.APPROVED:
        raise precondition("plan_not_approved", f"plan is {state}")
    reason = approval_effective(approval, plan, revoked=approval_revoked, now=now)
    if reason:
        raise precondition(reason, "approval does not currently authorize execution")
    operation_digest = digest(
        {
            "planDigest": plan.planDigest,
            "fields": [f.model_dump() for f in plan.approvedFieldChanges],
            "targetKey": plan.target.target_key(),
            "adapter": plan.adapter,
        }
    )
    intent = ExecutionIntent(
        intentId=intent_id,
        planId=plan.planId,
        planRevision=plan.revision,
        planDigest=plan.planDigest,
        approvalId=approval.approvalId,
        approvalDigest=approval.approvalDigest,
        requester=requester,
        requesterGrantRevision=requester_decision.grant_revision,
        createdAtUtc=now,
        expiresAtUtc=min(now + INTENT_TTL, approval.expiresAtUtc),
        targetKey=plan.target.target_key(),
        operationDigest=operation_digest,
        adapter=plan.adapter,
        recoveryDigest=plan.recovery_digest(),
        idempotencyKey=idempotency_key,
    )
    return intent.model_copy(update={"intentDigest": digest(intent, exclude=frozenset({"intentDigest"}))})
