"""S26 post-change verification and bounded preauthorized recovery.

Level 1 checks configuration and rollout (exact fields, UID, observedGeneration, ready
replicas). Level 2 checks operational guardrails and, separately, whether any benefit is
supported by *comparable* observed evidence. The outcome keeps every dimension separate:
a safe change with unproven benefit is `verified_safe_benefit_unproven`, not a success story.

Recovery is a new external operation, allowed only as the exact inverse that the human
approved, and only when the live object still shows exactly the approved after-image
under the same UID. Anything else escalates. At most `maxInverseAttempts` inverses run.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from capacity_platform.contracts.canonical import digest
from capacity_platform.contracts.enums import GateStatus
from capacity_platform.executor.operations import (
    Operation,
    St,
    build_patch,
    container_index,
    fields_match,
)
from capacity_platform.governance.plans import ExecutionPlan

VERIFIER_REVISION = "s26-verifier-v1"


class GuardrailThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    p95_latency_ms_max: float | None = None
    error_rate_max: float | None = None
    max_oom_restarts: int = 0
    min_availability: float | None = None
    max_queue_growth_per_min: float | None = None
    min_coverage: float = 0.9


class VerificationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schemaVersion: Literal["verification-policy/v1alpha1"]
    policyId: str
    revision: int
    workloads: dict[str, GuardrailThresholds]
    loadComparabilityTolerance: float = Field(gt=0, le=1)
    rolloutDeadlineSeconds: int = Field(gt=0)
    baselineSeconds: int
    stabilizationSeconds: int
    evaluationSeconds: int
    maxInverseAttempts: int = Field(ge=0, le=1)

    def digest(self) -> str:
        return digest(self)


def load_verification_policy(path: Path) -> VerificationPolicy:
    with path.open(encoding="utf-8") as handle:
        return VerificationPolicy.model_validate(yaml.safe_load(handle))


class WindowSummary(BaseModel):
    """Qualified S12 summary for one verification window. `None` = not measured."""

    model_config = ConfigDict(frozen=True)
    start: datetime
    end: datetime
    coverage: float
    request_rate: float | None = None
    p95_latency_ms: float | None = None
    error_rate: float | None = None
    availability: float | None = None
    oom_restarts: int | None = None
    queue_growth_per_min: float | None = None
    cpu_mean_m: float | None = None
    contamination: list[str] = Field(default_factory=list)  # host_pressure, generator_overloaded, ...
    observation_ref: str


class ConfigVerdict(BaseModel):
    configuration: GateStatus
    rollout: Literal["healthy", "incomplete", "failed", "unknown"]
    reasons: list[str]


class OperationalVerdict(BaseModel):
    guardrails: GateStatus
    guardrailFindings: dict[str, str]
    benefit: Literal["supported", "inconclusive", "not_demonstrated"]
    benefitReason: str
    loadComparability: Literal["sufficient", "insufficient", "unknown"]
    observedCpuDeltaMillicores: float | None


class Outcome(BaseModel):
    schemaVersion: str = "optimization-outcome/v1alpha1"
    outcomeId: str
    planId: str
    planRevision: int
    planDigest: str
    forwardOperationId: str
    recoveryOperationRef: str | None = None
    configuration: GateStatus
    rollout: str
    safetyGuardrails: GateStatus
    benefit: str
    benefitReason: str
    recovery: str
    operationalDisposition: Literal[
        "verified_safe_benefit_verified",
        "verified_safe_benefit_unproven",
        "verification_failed",
        "verification_inconclusive",
        "recovered",
        "escalated",
        "pending_verification",
    ]
    requestReservationDelta: dict[str, int]
    reservationBasis: str = "applied_template_config_not_measured_cpu_saving"
    observedCpuDeltaMillicores: float | None
    baselineWindowRef: str | None
    postWindowRef: str | None
    verificationPolicyDigest: str
    verifierRevision: str = VERIFIER_REVISION
    finishedAtUtc: datetime
    outcomeDigest: str = ""


def verify_configuration(
    plan: ExecutionPlan, live: dict | None, *, rollout_started: datetime, now: datetime, deadline_seconds: int
) -> ConfigVerdict:
    if live is None or live["metadata"].get("uid") != plan.target.controller.uid:
        return ConfigVerdict(configuration=GateStatus.FAIL, rollout="unknown", reasons=["target_identity_changed"])
    index = container_index(live, plan.target.containerName)
    if index is None or not fields_match(live, index, plan.approvedFieldChanges, use_after=True):
        return ConfigVerdict(configuration=GateStatus.FAIL, rollout="unknown", reasons=["applied_fields_drifted"])
    status, spec, meta = live.get("status", {}), live.get("spec", {}), live["metadata"]
    desired = spec.get("replicas", 1)
    for cond in status.get("conditions", []) or []:
        if cond.get("type") == "Progressing" and cond.get("reason") == "ProgressDeadlineExceeded":
            return ConfigVerdict(
                configuration=GateStatus.PASS, rollout="failed", reasons=["progress_deadline_exceeded"]
            )
    complete = (
        status.get("observedGeneration", 0) >= meta.get("generation", 0)
        and status.get("updatedReplicas", 0) == desired
        and status.get("availableReplicas", 0) >= desired
        and not status.get("unavailableReplicas")
    )
    if complete:
        return ConfigVerdict(configuration=GateStatus.PASS, rollout="healthy", reasons=[])
    if (now - rollout_started).total_seconds() > deadline_seconds:
        return ConfigVerdict(configuration=GateStatus.PASS, rollout="failed", reasons=["rollout_deadline_exceeded"])
    return ConfigVerdict(configuration=GateStatus.PASS, rollout="incomplete", reasons=["rollout_in_progress"])


def verify_operational(
    thresholds: GuardrailThresholds,
    tolerance: float,
    baseline: WindowSummary | None,
    post: WindowSummary | None,
    reservation_delta: dict[str, int],
) -> OperationalVerdict:
    findings: dict[str, str] = {}
    if post is None or post.coverage < thresholds.min_coverage:
        # Missing telemetry is never a pass and never a zero (S26-T07).
        return OperationalVerdict(
            guardrails=GateStatus.UNKNOWN,
            guardrailFindings={"coverage": "insufficient"},
            benefit="inconclusive",
            benefitReason="post_window_insufficient",
            loadComparability="unknown",
            observedCpuDeltaMillicores=None,
        )

    def check(name: str, value, limit, higher_is_bad: bool = True) -> None:
        if limit is None:
            return
        if value is None:
            findings[name] = "unknown"
        elif (value > limit) if higher_is_bad else (value < limit):
            findings[name] = "fail"
        else:
            findings[name] = "pass"

    check("p95_latency_ms", post.p95_latency_ms, thresholds.p95_latency_ms_max)
    check("error_rate", post.error_rate, thresholds.error_rate_max)
    check("oom_restarts", post.oom_restarts, thresholds.max_oom_restarts)
    check("availability", post.availability, thresholds.min_availability, higher_is_bad=False)
    check("queue_growth_per_min", post.queue_growth_per_min, thresholds.max_queue_growth_per_min)
    if any(v == "fail" for v in findings.values()):
        guard = GateStatus.FAIL
    elif any(v == "unknown" for v in findings.values()):
        guard = GateStatus.UNKNOWN
    else:
        guard = GateStatus.PASS

    comparable: Literal["sufficient", "insufficient", "unknown"] = "unknown"
    if baseline and baseline.request_rate and post.request_rate is not None:
        drift = abs(post.request_rate - baseline.request_rate) / baseline.request_rate
        comparable = "sufficient" if drift <= tolerance else "insufficient"
    contaminated = bool((baseline.contamination if baseline else []) + post.contamination)
    cpu_delta = None
    if comparable == "sufficient" and baseline and baseline.cpu_mean_m is not None and post.cpu_mean_m is not None:
        cpu_delta = post.cpu_mean_m - baseline.cpu_mean_m

    reduced = reservation_delta.get("cpu_m", 0) < 0 or reservation_delta.get("memory_bytes", 0) < 0
    if guard != GateStatus.PASS:
        benefit, reason = "inconclusive", "guardrails_not_passed"
    elif contaminated:
        benefit, reason = "inconclusive", "experiment_contamination"
    elif comparable != "sufficient":
        benefit, reason = "inconclusive", "incomparable_load"
    elif not reduced:
        benefit, reason = "not_demonstrated", "no_reservation_reduction"
    else:
        benefit, reason = "supported", "reservation_reduced_under_comparable_safe_load"
    return OperationalVerdict(
        guardrails=guard,
        guardrailFindings=findings,
        benefit=benefit,
        benefitReason=reason,
        loadComparability=comparable,
        observedCpuDeltaMillicores=cpu_delta,
    )


def disposition(cfg: ConfigVerdict, ops: OperationalVerdict | None) -> str:
    if cfg.configuration != GateStatus.PASS or cfg.rollout == "failed":
        return "verification_failed"
    if cfg.rollout != "healthy":
        return "pending_verification"
    if ops is None:
        return "verification_inconclusive"
    if ops.guardrails == GateStatus.FAIL:
        return "verification_failed"
    if ops.guardrails == GateStatus.UNKNOWN:
        return "verification_inconclusive"
    return "verified_safe_benefit_verified" if ops.benefit == "supported" else "verified_safe_benefit_unproven"


def build_outcome(
    plan: ExecutionPlan,
    op: Operation,
    cfg: ConfigVerdict,
    ops: OperationalVerdict | None,
    policy: VerificationPolicy,
    baseline: WindowSummary | None,
    post: WindowSummary | None,
    now: datetime,
    recovery: str = "not_required",
) -> Outcome:
    delta = {"cpu_m": 0, "memory_bytes": 0}
    for f in plan.approvedFieldChanges:
        if f.before is not None and f.after is not None:
            if f.field.value == "cpu_request":
                delta["cpu_m"] = f.after - f.before
            elif f.field.value == "memory_request":
                delta["memory_bytes"] = f.after - f.before
    disp = disposition(cfg, ops)
    if recovery == "verified":
        disp = "recovered"
    elif recovery in ("escalated", "failed"):
        disp = "escalated"
    out = Outcome(
        outcomeId=digest({"op": op.operationId, "at": now}),
        planId=plan.planId,
        planRevision=plan.revision,
        planDigest=plan.planDigest,
        forwardOperationId=op.operationId,
        configuration=cfg.configuration,
        rollout=cfg.rollout,
        safetyGuardrails=ops.guardrails if ops else GateStatus.UNKNOWN,
        benefit=ops.benefit if ops else "inconclusive",
        benefitReason=ops.benefitReason if ops else "not_evaluated",
        recovery=recovery,
        operationalDisposition=disp,
        requestReservationDelta=delta,
        observedCpuDeltaMillicores=ops.observedCpuDeltaMillicores if ops else None,
        baselineWindowRef=baseline.observation_ref if baseline else None,
        postWindowRef=post.observation_ref if post else None,
        verificationPolicyDigest=policy.digest(),
        finishedAtUtc=now,
    )
    return out.model_copy(update={"outcomeDigest": digest(out, exclude=frozenset({"outcomeDigest"}))})


# --------------------------------------------------------------------------- recovery


class RecoveryDecision(BaseModel):
    action: Literal["not_required", "apply_inverse", "escalate"]
    reason: str
    patch: list[dict] | None = None


def plan_recovery(plan: ExecutionPlan, op: Operation, live: dict | None, cluster_incarnation: str) -> RecoveryDecision:
    """S26.4 recovery preconditions. Returns the exact inverse patch only if every check holds."""
    if op.recoveryAttempts >= plan.maxInverseAttempts:
        return RecoveryDecision(action="escalate", reason="max_inverse_attempts_reached")
    if op.state not in (St.VERIFICATION_FAILED, St.VERIFICATION_INCONCLUSIVE):
        return RecoveryDecision(action="escalate", reason="recovery_not_triggered_by_verification")
    if plan.target.cluster.incarnationId != cluster_incarnation:
        return RecoveryDecision(action="escalate", reason="cluster_incarnation_changed")
    if live is None or live["metadata"].get("uid") != plan.target.controller.uid:
        # A recreated controller is a different target; the old approval cannot touch it.
        return RecoveryDecision(action="escalate", reason="target_identity_changed")
    index = container_index(live, plan.target.containerName)
    if index is None:
        return RecoveryDecision(action="escalate", reason="container_identity_ambiguous")
    if fields_match(live, index, plan.approvedFieldChanges, use_after=False):
        return RecoveryDecision(action="not_required", reason="forward_change_not_present")
    if not fields_match(live, index, plan.approvedFieldChanges, use_after=True):
        # Someone else edited the fields after us: never overwrite a concurrent change.
        return RecoveryDecision(action="escalate", reason="concurrent_edit_detected")
    patch = build_patch(live, index, plan.target.containerName, plan.inverseFieldChanges)
    return RecoveryDecision(action="apply_inverse", reason="preauthorized_exact_inverse", patch=patch)
