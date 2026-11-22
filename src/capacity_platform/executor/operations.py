"""S25 reconcile-first execution workflow.

Ordering that matters for safety:
1. Claim a lease with a fencing epoch; a stale worker can no longer commit state.
2. Re-read authority (plan, approval, intent, mode, policy, grant) from Governance's
   authoritative read view. The outbox event that woke us is never treated as authority.
3. Fresh live GET of the target; verify cluster incarnation, UID, container, ownership,
   HPA, host safety and the exact approved before-image.
4. Server-side dry run of the exact patch.
5. Persist the operation intent (journal) BEFORE the first external write.
6. Submit once. A timeout means UNKNOWN, never failure; the next step is always to observe.
7. Reconcile from a fresh GET: exact after-image -> applied_pending_verification;
   untouched object -> refused; anything else -> escalated. No blind second patch.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from capacity_platform.contracts.canonical import digest
from capacity_platform.contracts.enums import OperatingMode, ResourceField
from capacity_platform.contracts.ownership import detect_ownership
from capacity_platform.contracts.units import (
    bytes_to_quantity,
    cpu_to_millicores,
    memory_to_bytes,
    millicores_to_quantity,
)
from capacity_platform.contracts.workloads import DEMO_NAMESPACES
from capacity_platform.executor.kube import (
    KubeConflict,
    KubeNotFound,
    KubeRejected,
    KubernetesPort,
    KubeUncertain,
)
from capacity_platform.governance.plans import (
    ApprovalRecord,
    ExecutionIntent,
    ExecutionPlan,
    FieldSet,
    PlanState,
    approval_effective,
)


class OpState(StrEnum):
    REQUESTED = "requested"
    AWAITING_REVALIDATION = "awaiting_revalidation"
    REFUSED = "refused"
    IN_PROGRESS = "in_progress"
    UNKNOWN = "unknown"
    APPLIED_PENDING_VERIFICATION = "applied_pending_verification"
    VERIFICATION_IN_PROGRESS = "verification_in_progress"
    VERIFIED_SUCCESS = "verified_success"
    VERIFICATION_FAILED = "verification_failed"
    VERIFICATION_INCONCLUSIVE = "verification_inconclusive"
    RECOVERY_IN_PROGRESS = "recovery_in_progress"
    RECOVERED = "recovered"
    ESCALATED = "escalated"


St = OpState  # short alias keeps the transition table readable
# S25.3 combined S25/S26 lifecycle.
OP_TRANSITIONS: dict[OpState, frozenset[OpState]] = {
    St.REQUESTED: frozenset({St.AWAITING_REVALIDATION}),
    St.AWAITING_REVALIDATION: frozenset({St.REFUSED, St.IN_PROGRESS}),
    St.IN_PROGRESS: frozenset({St.APPLIED_PENDING_VERIFICATION, St.UNKNOWN, St.REFUSED, St.ESCALATED}),
    St.UNKNOWN: frozenset({St.APPLIED_PENDING_VERIFICATION, St.ESCALATED, St.REFUSED}),
    St.APPLIED_PENDING_VERIFICATION: frozenset({St.VERIFICATION_IN_PROGRESS}),
    St.VERIFICATION_IN_PROGRESS: frozenset(
        {St.VERIFIED_SUCCESS, St.VERIFICATION_FAILED, St.VERIFICATION_INCONCLUSIVE, St.ESCALATED}
    ),
    St.VERIFICATION_FAILED: frozenset({St.RECOVERY_IN_PROGRESS, St.ESCALATED}),
    St.VERIFICATION_INCONCLUSIVE: frozenset({St.RECOVERY_IN_PROGRESS, St.ESCALATED, St.VERIFIED_SUCCESS}),
    St.RECOVERY_IN_PROGRESS: frozenset({St.RECOVERED, St.ESCALATED}),
}
TERMINAL = frozenset({St.REFUSED, St.VERIFIED_SUCCESS, St.RECOVERED, St.ESCALATED})


class IllegalOperationTransition(Exception):
    pass


class Transition(BaseModel):
    at: datetime
    from_state: OpState
    to_state: OpState
    reason: str
    fence: int


class Operation(BaseModel):
    schemaVersion: str = "execution-operation/v1alpha1"
    operationId: str
    intentId: str
    planId: str
    planRevision: int
    planDigest: str
    targetKey: str
    adapter: str
    state: OpState = St.REQUESTED
    attempt: int = 0
    fencingEpoch: int = 0
    refusalReason: str | None = None
    observedResourceVersion: str | None = None
    patch: list[dict[str, Any]] | None = None
    patchDigest: str | None = None
    intentPersistedAt: datetime | None = None
    reconciliation: dict[str, Any] | None = None
    recoveryAttempts: int = 0
    history: list[Transition] = Field(default_factory=list)

    def move(self, new: OpState, reason: str, now: datetime) -> None:
        if new not in OP_TRANSITIONS.get(self.state, frozenset()):
            raise IllegalOperationTransition(f"{self.state} -> {new}")
        self.history.append(
            Transition(at=now, from_state=self.state, to_state=new, reason=reason, fence=self.fencingEpoch)
        )
        self.state = new


def operation_key(intent: ExecutionIntent, plan: ExecutionPlan) -> str:
    """S25.5 OperationKey: duplicate deliveries of the same intent map to ONE operation."""
    return digest(
        {
            "planId": plan.planId,
            "revision": plan.revision,
            "planDigest": plan.planDigest,
            "targetUid": plan.target.controller.uid,
            "operationDigest": intent.operationDigest,
            "adapter": plan.adapter,
        }
    )


# --------------------------------------------------------------------------- ports


class AuthorityBundle(BaseModel):
    """Governance-authoritative read view (S24 `get_execution_authority.v1`)."""

    plan: ExecutionPlan
    plan_state: PlanState
    approval: ApprovalRecord
    approval_revoked: bool
    intent: ExecutionIntent
    mode: OperatingMode
    current_policy_digest: str
    requester_grant_valid: bool


class GovernancePort(Protocol):
    def get_authority(self, intent_id: str) -> AuthorityBundle | None: ...


class FenceLost(Exception):
    """The lease moved to another worker; this worker must stop committing state."""


class ExecutionStore(Protocol):
    def claim(self, intent_id: str, worker_id: str, now: datetime) -> int | None: ...
    def load(self, intent_id: str) -> Operation | None: ...
    def save(self, op: Operation) -> None: ...  # raises FenceLost if op.fencingEpoch is stale


class Environment(BaseModel):
    cluster_incarnation: str
    host_safety: str  # ok | pressure | exclusive | unknown
    hpa_present: bool | None


# --------------------------------------------------------------------------- live-state helpers

_FIELD_LOC = {
    ResourceField.CPU_REQUEST: ("requests", "cpu"),
    ResourceField.MEMORY_REQUEST: ("requests", "memory"),
    ResourceField.CPU_LIMIT: ("limits", "cpu"),
    ResourceField.MEMORY_LIMIT: ("limits", "memory"),
}


def container_index(deployment: dict, name: str) -> int | None:
    containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    matches = [i for i, c in enumerate(containers) if c.get("name") == name]
    return matches[0] if len(matches) == 1 else None


def live_value(deployment: dict, index: int, field: ResourceField) -> tuple[int | None, str | None]:
    section, key = _FIELD_LOC[field]
    raw = (deployment["spec"]["template"]["spec"]["containers"][index].get("resources") or {}).get(section, {}) or {}
    text = raw.get(key)
    if text is None:
        return None, None
    text = str(text)
    return (cpu_to_millicores(text) if key == "cpu" else memory_to_bytes(text)), text


def build_patch(deployment: dict, index: int, container: str, changes: tuple[FieldSet, ...]) -> list[dict]:
    """Exact JSON Patch with `test` preconditions; built only from trusted plan fields."""
    meta = deployment["metadata"]
    base = f"/spec/template/spec/containers/{index}"
    ops: list[dict] = [
        {"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
        {"op": "test", "path": f"{base}/name", "value": container},
    ]
    resources = deployment["spec"]["template"]["spec"]["containers"][index].get("resources") or {}
    if not resources:
        ops.append({"op": "add", "path": f"{base}/resources", "value": {}})
    created_sections: set[str] = set()
    for change in changes:
        section, key = _FIELD_LOC[change.field]
        path = f"{base}/resources/{section}/{key}"
        _, live_text = live_value(deployment, index, change.field)
        if live_text is not None:
            ops.append({"op": "test", "path": path, "value": live_text})
        if change.after is None:
            ops.append({"op": "remove", "path": path})
            continue
        rendered = millicores_to_quantity(change.after) if key == "cpu" else bytes_to_quantity(change.after)
        if section not in resources and section not in created_sections:
            ops.append({"op": "add", "path": f"{base}/resources/{section}", "value": {}})
            created_sections.add(section)
        ops.append({"op": "replace" if live_text is not None else "add", "path": path, "value": rendered})
    return ops


def fields_match(deployment: dict, index: int, expected: tuple[FieldSet, ...], use_after: bool) -> bool:
    for change in expected:
        value, _ = live_value(deployment, index, change.field)
        if value != (change.after if use_after else change.before):
            return False
    return True


# --------------------------------------------------------------------------- preflight


def preflight(bundle: AuthorityBundle, live: dict | None, env: Environment, now: datetime) -> list[str]:
    """Ordered S25.2.1 checks. Returns refusal reasons; empty list means proceed."""
    plan, intent = bundle.plan, bundle.intent
    reasons: list[str] = []
    if digest(intent, exclude=frozenset({"intentDigest"})) != intent.intentDigest:
        return ["intent_integrity_invalid"]
    if intent.planDigest != plan.planDigest or intent.approvalId != bundle.approval.approvalId:
        return ["plan_digest_mismatch"]
    if bundle.mode != OperatingMode.GOVERNED:
        reasons.append("mode_not_governed")
    if bundle.plan_state not in (PlanState.EXECUTION_REQUESTED, PlanState.CONSUMED_BY_EXECUTOR):
        reasons.append("plan_not_execution_requested")
    eff = approval_effective(bundle.approval, plan, revoked=bundle.approval_revoked, now=now)
    if eff:
        reasons.append(eff)
    if now >= intent.expiresAtUtc:
        reasons.append("intent_expired")
    if not bundle.requester_grant_valid:
        reasons.append("requester_grant_revoked")
    if bundle.current_policy_digest != plan.policyDigest:
        reasons.append("policy_superseded")
    if plan.adapter != "direct_kubernetes":
        reasons.append("adapter_unqualified")
    if plan.target.cluster.incarnationId != env.cluster_incarnation:
        reasons.append("cluster_incarnation_mismatch")
    if plan.target.namespace not in DEMO_NAMESPACES:
        reasons.append("target_disallowed")
    if env.host_safety != "ok":
        reasons.append("host_pressure" if env.host_safety != "exclusive" else "exclusive_experiment")
    if reasons:
        return reasons
    if live is None:
        return ["target_not_found"]
    if live["metadata"].get("uid") != plan.target.controller.uid:
        return ["target_identity_changed"]
    index = container_index(live, plan.target.containerName)
    if index is None:
        return ["container_identity_ambiguous"]
    ownership = detect_ownership(live)
    if ownership != "direct_managed":
        reasons.append("ownership_" + ownership)
    if env.hpa_present is None:
        reasons.append("hpa_status_unknown")
    elif env.hpa_present and any(f.field == ResourceField.CPU_REQUEST for f in plan.approvedFieldChanges):
        reasons.append("hpa_request_coupling_unmodeled")
    if not fields_match(live, index, plan.approvedFieldChanges, use_after=False):
        reasons.append("before_image_mismatch")
    return reasons


# --------------------------------------------------------------------------- workflow


class Executor:
    def __init__(self, kube: KubernetesPort, governance: GovernancePort, store: ExecutionStore, worker_id: str) -> None:
        self.kube, self.governance, self.store, self.worker_id = kube, governance, store, worker_id

    def _get(self, plan: ExecutionPlan) -> dict | None:
        try:
            return self.kube.get_deployment(plan.target.namespace, plan.target.controller.name)
        except KubeNotFound:
            return None

    def reconcile(self, op: Operation, plan: ExecutionPlan, now: datetime) -> Operation:
        """Observe the live object and classify the effect. Never writes to Kubernetes."""
        live = self._get(plan)
        index = container_index(live, plan.target.containerName) if live else None
        evidence: dict[str, Any] = {"observedAt": now.isoformat(), "found": live is not None}
        if live is None or index is None or live["metadata"].get("uid") != plan.target.controller.uid:
            op.reconciliation = {**evidence, "classification": "target_identity_changed"}
            op.move(St.ESCALATED, "target_identity_changed", now)
            return op
        evidence["resourceVersion"] = live["metadata"].get("resourceVersion")
        if fields_match(live, index, plan.approvedFieldChanges, use_after=True):
            op.reconciliation = {**evidence, "classification": "applied"}
            op.move(St.APPLIED_PENDING_VERIFICATION, "expected_fields_observed", now)
        elif (
            fields_match(live, index, plan.approvedFieldChanges, use_after=False)
            and evidence["resourceVersion"] == op.observedResourceVersion
        ):
            op.reconciliation = {**evidence, "classification": "unapplied"}
            op.refusalReason = "submission_unapplied"
            op.move(St.REFUSED, "definitely_unapplied", now)
        else:
            op.reconciliation = {**evidence, "classification": "drift_or_partial"}
            op.move(St.ESCALATED, "concurrent_drift", now)
        return op

    def run(self, intent_id: str, env: Environment, now: datetime) -> Operation | None:
        fence = self.store.claim(intent_id, self.worker_id, now)
        if fence is None:
            return None  # another worker holds the lease
        bundle = self.governance.get_authority(intent_id)
        op = self.store.load(intent_id)
        if bundle is None:
            if op is not None:
                return op
            return None
        plan = bundle.plan
        if op is None:
            op = Operation(
                operationId=operation_key(bundle.intent, plan),
                intentId=intent_id,
                planId=plan.planId,
                planRevision=plan.revision,
                planDigest=plan.planDigest,
                targetKey=plan.target.target_key(),
                adapter=plan.adapter,
            )
        op.fencingEpoch = fence
        if op.state in TERMINAL or op.state in (St.APPLIED_PENDING_VERIFICATION, St.VERIFICATION_IN_PROGRESS):
            return op
        if op.state in (St.IN_PROGRESS, St.UNKNOWN):
            # A previous worker may have written to Kubernetes. Observe; do not re-patch.
            if op.state == St.IN_PROGRESS:
                op.move(St.UNKNOWN, "resumed_after_interruption", now)
                self.store.save(op)
            self.reconcile(op, plan, now)
            self.store.save(op)
            return op

        if op.state == St.REQUESTED:
            op.move(St.AWAITING_REVALIDATION, "claimed", now)
        live = self._get(plan)
        reasons = preflight(bundle, live, env, now)
        if reasons:
            op.refusalReason = reasons[0]
            op.reconciliation = {"refusalReasons": reasons}
            op.move(St.REFUSED, ",".join(reasons), now)
            self.store.save(op)
            return op
        assert live is not None
        index = container_index(live, plan.target.containerName)
        assert index is not None
        patch = build_patch(live, index, plan.target.containerName, plan.approvedFieldChanges)
        try:
            self.kube.patch_deployment(plan.target.namespace, plan.target.controller.name, patch, dry_run=True)
        except (KubeRejected, KubeConflict) as exc:
            op.refusalReason = "dry_run_rejected"
            op.reconciliation = {"dryRunError": type(exc).__name__}
            op.move(St.REFUSED, "dry_run_rejected", now)
            self.store.save(op)
            return op

        # Journal the exact operation BEFORE the external write (intent-before-effect).
        op.patch = patch
        op.patchDigest = digest(patch)
        op.observedResourceVersion = live["metadata"]["resourceVersion"]
        op.intentPersistedAt = now
        op.attempt += 1
        op.move(St.IN_PROGRESS, "operation_intent_persisted", now)
        self.store.save(op)  # FenceLost here aborts before any mutation

        if now >= bundle.intent.expiresAtUtc:
            op.refusalReason = "intent_expired"
            op.move(St.REFUSED, "authority_expired_before_write", now)
            self.store.save(op)
            return op
        try:
            self.kube.patch_deployment(plan.target.namespace, plan.target.controller.name, patch, dry_run=False)
        except KubeConflict:
            # Preconditions failed server-side: nothing was overwritten. Observe to record why.
            self.reconcile(op, plan, now)
            if op.state == St.REFUSED:
                op.refusalReason = "precondition_conflict"
            self.store.save(op)
            return op
        except KubeRejected:
            op.refusalReason = "admission_rejected"
            op.move(St.REFUSED, "admission_rejected", now)
            self.store.save(op)
            return op
        except KubeUncertain:
            op.move(St.UNKNOWN, "submission_outcome_unknown", now)
            self.store.save(op)
        if op.state == St.UNKNOWN or op.state == St.IN_PROGRESS:
            self.reconcile(op, plan, now)
        self.store.save(op)
        return op
