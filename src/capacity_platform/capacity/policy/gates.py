"""S19 hard-rule catalog and the Domain E §5 gate classification oracle.

Each rule returns `pass | fail | unknown | not_applicable` with a stable reason code.
Missing evidence becomes `unknown`, never `pass`. `not_applicable` is only returned when
the rule itself says it does not apply (for example a latency SLO rule on a batch worker);
an absent signal is never relabeled as not applicable.

Rules run in phases (S19 §2): pre_candidate, candidate, post_fit, post_simulation. No
score is computed until every required rule in every phase has passed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict

from capacity_platform.capacity.candidates import ResourceCandidate
from capacity_platform.capacity.inputs import AnalysisEvidence, TargetState
from capacity_platform.capacity.policy.model import EffectivePolicy
from capacity_platform.contracts.enums import (
    Coverage,
    Criticality,
    Eligibility,
    Freshness,
    GateStatus,
    ResourceField,
)
from capacity_platform.contracts.workloads import CATALOG, DEMO_NAMESPACES, PLATFORM_NAMESPACES

EVALUATOR_REVISION = "s19-gates-v1"


class GateResult(BaseModel):
    model_config = ConfigDict(frozen=True)
    ruleId: str
    phase: str
    status: GateStatus
    reasonCode: str
    required: bool = True
    evidenceRefs: tuple[str, ...] = ()


def _r(rule: str, phase: str, status: GateStatus, reason: str, refs=(), required=True) -> GateResult:
    return GateResult(
        ruleId=rule, phase=phase, status=status, reasonCode=reason, required=required, evidenceRefs=tuple(refs)
    )


@dataclass
class GateContext:
    target: TargetState
    evidence: AnalysisEvidence
    policy: EffectivePolicy
    intended_use: str = "governed_execution"  # or "advisory"
    candidate: ResourceCandidate | None = None
    placement_status: str | None = None  # feasible | infeasible | indeterminate
    placement_ref: str | None = None
    surge_placement_status: str | None = None
    notes: list[str] = field(default_factory=list)

    def decreases(self, *fields: ResourceField) -> bool:
        if self.candidate is None:
            return False
        return any(f.field in fields and f.is_decrease for f in self.candidate.fields)

    def changes(self, *fields: ResourceField) -> bool:
        return self.candidate is not None and any(f.field in fields for f in self.candidate.fields)


# --------------------------------------------------------------- pre_candidate phase


def identity_scope(ctx: GateContext) -> GateResult:
    ok = ctx.evidence.scope_authorized
    if ok is None:
        return _r("identity.scope", "pre_candidate", GateStatus.UNKNOWN, "scope_unverified")
    return _r(
        "identity.scope", "pre_candidate", GateStatus.PASS if ok else GateStatus.FAIL, "ok" if ok else "scope_denied"
    )


def target_allowlist(ctx: GateContext) -> GateResult:
    t = ctx.target.target
    entry = CATALOG.get(t.logicalWorkloadId)
    if t.namespace in PLATFORM_NAMESPACES or t.namespace not in DEMO_NAMESPACES:
        return _r("target.allowlist", "pre_candidate", GateStatus.FAIL, "target_disallowed")
    if entry is None or (entry.namespace, entry.name, entry.kind) != (
        t.namespace,
        t.controller.name,
        ctx.target.workload_kind,
    ):
        return _r("target.allowlist", "pre_candidate", GateStatus.FAIL, "target_disallowed")
    if not t.controller.uid or not t.cluster.incarnationId:
        return _r("target.allowlist", "pre_candidate", GateStatus.UNKNOWN, "target_identity_ambiguous")
    return _r("target.allowlist", "pre_candidate", GateStatus.PASS, "ok")


def evidence_integrity(ctx: GateContext) -> GateResult:
    ev = ctx.evidence
    refs = ev.refs()
    if ev.inventory_coverage != Coverage.COMPLETE or ev.inventory_freshness != Freshness.FRESH:
        return _r("evidence.integrity", "pre_candidate", GateStatus.UNKNOWN, "inventory_stale", refs)
    return _r("evidence.integrity", "pre_candidate", GateStatus.PASS, "ok", refs)


def action_supported(ctx: GateContext) -> GateResult:
    if ctx.candidate is not None:
        allowed = set(ctx.policy.allowedActions)
        if any(f.field not in allowed for f in ctx.candidate.fields):
            return _r("action.supported", "pre_candidate", GateStatus.FAIL, "action_unsupported")
    if ctx.intended_use == "governed_execution":
        # S25.2.2: P0 direct mutation covers Deployments only; Job/CronJob templates are advisory.
        if ctx.target.workload_kind != "Deployment":
            return _r("action.supported", "pre_candidate", GateStatus.FAIL, "action_unsupported")
        ownership = ctx.evidence.ownership
        if ownership is None:
            return _r("action.supported", "pre_candidate", GateStatus.UNKNOWN, "gitops_owner_unknown")
        if ownership in ("ambiguous", "unsupported"):
            return _r("action.supported", "pre_candidate", GateStatus.FAIL, "ownership_ambiguous")
    return _r("action.supported", "pre_candidate", GateStatus.PASS, "ok")


# --------------------------------------------------------------- candidate phase


def resources_valid(ctx: GateContext) -> GateResult:
    cand = ctx.candidate
    if cand is None:
        return _r("resources.valid", "candidate", GateStatus.NOT_APPLICABLE, "no_candidate")
    p = ctx.policy.params
    proposed = ctx.target.current.with_changes(cand.changes())
    for f in cand.fields:
        if f.after is not None and f.after <= 0:
            return _r("resources.valid", "candidate", GateStatus.FAIL, "quantity_invalid")
    if proposed.cpu_request_m is not None and proposed.cpu_request_m < p.cpu_request_floor_m:
        return _r("resources.valid", "candidate", GateStatus.FAIL, "floor_violation")
    if proposed.memory_request_bytes is not None and proposed.memory_request_bytes < p.memory_request_floor_bytes:
        return _r("resources.valid", "candidate", GateStatus.FAIL, "floor_violation")
    for req, lim in (
        (proposed.cpu_request_m, proposed.cpu_limit_m),
        (proposed.memory_request_bytes, proposed.memory_limit_bytes),
    ):
        if req is not None and lim is not None and req > lim:
            return _r("resources.valid", "candidate", GateStatus.FAIL, "request_exceeds_limit")
    for f in cand.fields:
        if f.before and f.after is not None:
            change = (f.after - f.before) / f.before
            if change < -p.max_decrease_fraction - 1e-9 or change > p.max_increase_fraction + 1e-9:
                return _r("resources.valid", "candidate", GateStatus.FAIL, "change_budget_exceeded")
    if ctx.target.current.qos_class() != proposed.qos_class():
        return _r("resources.valid", "candidate", GateStatus.FAIL, "qos_class_change_unqualified")
    delta = cand.reservation_delta()
    replicas = max(ctx.target.replicas, 1)
    for remaining, d in (
        (ctx.target.quota_remaining_cpu_m, delta["cpu_m"]),
        (ctx.target.quota_remaining_memory_bytes, delta["memory_bytes"]),
    ):
        if d > 0:
            if remaining is None:
                return _r("resources.valid", "candidate", GateStatus.UNKNOWN, "quota_status_unknown")
            if d * replicas > remaining:
                return _r("resources.valid", "candidate", GateStatus.FAIL, "quota_exceeded")
    return _r("resources.valid", "candidate", GateStatus.PASS, "ok")


def slo_latency_errors(ctx: GateContext) -> GateResult:
    entry = CATALOG[ctx.target.target.logicalWorkloadId]
    if entry.workload_id not in ("w01-latency-api", "w02-burst-api"):
        return _r("slo.latency_errors", "post_simulation", GateStatus.NOT_APPLICABLE, "rule_scope_excludes_workload")
    if not ctx.decreases(ResourceField.CPU_REQUEST, ResourceField.CPU_LIMIT):
        return _r("slo.latency_errors", "post_simulation", GateStatus.PASS, "not_a_risk_sensitive_change")
    p, slo = ctx.policy.params, ctx.evidence.slo
    if slo is None or slo.p95_latency_ms is None or slo.error_rate is None or slo.request_count < p.min_slo_requests:
        return _r("slo.latency_errors", "post_simulation", GateStatus.UNKNOWN, "slo_evidence_unknown")
    refs = (slo.observation_ref,)
    if p.p95_latency_ms_max is None or p.error_rate_max is None:
        return _r("slo.latency_errors", "post_simulation", GateStatus.UNKNOWN, "slo_threshold_uncalibrated", refs)
    if slo.p95_latency_ms > p.p95_latency_ms_max or slo.error_rate > p.error_rate_max:
        return _r("slo.latency_errors", "post_simulation", GateStatus.FAIL, "slo_violation", refs)
    cpu = ctx.evidence.cpu
    if (
        p.throttle_ratio_max is not None
        and cpu is not None
        and cpu.throttle_ratio_p95 is not None
        and cpu.throttle_ratio_p95 > p.throttle_ratio_max
    ):
        return _r("slo.latency_errors", "post_simulation", GateStatus.FAIL, "cpu_throttling_present", refs)
    return _r("slo.latency_errors", "post_simulation", GateStatus.PASS, "ok", refs)


def slo_queue_completion(ctx: GateContext) -> GateResult:
    wid = ctx.target.target.logicalWorkloadId
    if wid not in ("w04-cpu-worker", "w05-analytics-processor", "w06-opportunistic-batch"):
        return _r("slo.queue_completion", "post_simulation", GateStatus.NOT_APPLICABLE, "rule_scope_excludes_workload")
    if not ctx.decreases(ResourceField.CPU_REQUEST, ResourceField.CPU_LIMIT):
        return _r("slo.queue_completion", "post_simulation", GateStatus.PASS, "not_a_risk_sensitive_change")
    q, p = ctx.evidence.queue, ctx.policy.params
    if q is None or q.completion_within_deadline_rate is None or q.samples == 0:
        return _r("slo.queue_completion", "post_simulation", GateStatus.UNKNOWN, "queue_evidence_unknown")
    refs = (q.observation_ref,)
    # F04: low CPU with a growing queue is not spare capacity.
    if q.queue_depth_trend_per_min > 0:
        return _r("slo.queue_completion", "post_simulation", GateStatus.FAIL, "batch_deadline_risk", refs)
    if p.min_completion_rate is not None and q.completion_within_deadline_rate < p.min_completion_rate:
        return _r("slo.queue_completion", "post_simulation", GateStatus.FAIL, "batch_deadline_risk", refs)
    return _r("slo.queue_completion", "post_simulation", GateStatus.PASS, "ok", refs)


def memory_oom_spike(ctx: GateContext) -> GateResult:
    if not ctx.decreases(ResourceField.MEMORY_REQUEST, ResourceField.MEMORY_LIMIT):
        return _r("memory.oom_spike", "candidate", GateStatus.PASS, "not_a_risk_sensitive_change")
    mem, p = ctx.evidence.memory, ctx.policy.params
    if mem is None:
        return _r("memory.oom_spike", "candidate", GateStatus.UNKNOWN, "memory_tail_unqualified")
    refs = (mem.observation_ref,)
    if mem.oom_events > 0:
        return _r("memory.oom_spike", "candidate", GateStatus.FAIL, "oom_risk", refs)
    peak_flags = {"peak_not_guaranteed", "short_job_unsampled", "scrape_gap"}
    if (
        (peak_flags & set(mem.flags))
        or mem.coverage < p.min_memory_coverage
        or mem.max_gap_seconds > p.max_memory_gap_seconds
    ):
        return _r("memory.oom_spike", "candidate", GateStatus.UNKNOWN, "memory_tail_unqualified", refs)
    proposed = ctx.target.current.with_changes(ctx.candidate.changes())  # type: ignore[union-attr]
    envelope = max(mem.max_sampled_bytes, mem.p99_bytes) * (1 + p.memory_margin)
    if proposed.memory_limit_bytes is not None and proposed.memory_limit_bytes < envelope:
        return _r("memory.oom_spike", "candidate", GateStatus.FAIL, "oom_risk", refs)
    return _r("memory.oom_spike", "candidate", GateStatus.PASS, "ok", refs)


def forecast_support(ctx: GateContext) -> GateResult:
    """S19-FR-007: a downsize of continuous demand needs an eligible S14 forecast; a refused
    forecast never becomes a hidden confidence bonus (Domain E §6 `forecast_refused`)."""
    p = ctx.policy.params
    entry = CATALOG[ctx.target.target.logicalWorkloadId]
    if not entry.continuous:
        return _r("forecast.support", "post_simulation", GateStatus.NOT_APPLICABLE, "run_conditional_workload")
    needs_cpu = ctx.decreases(ResourceField.CPU_REQUEST, ResourceField.CPU_LIMIT)
    needs_mem = ctx.decreases(ResourceField.MEMORY_REQUEST, ResourceField.MEMORY_LIMIT)
    if not (needs_cpu or needs_mem) or not p.require_forecast_for_downsize:
        return _r("forecast.support", "post_simulation", GateStatus.PASS, "not_required")
    for needed, fc in ((needs_cpu, ctx.evidence.forecast_cpu), (needs_mem, ctx.evidence.forecast_memory)):
        if not needed:
            continue
        if fc is None or fc.status != "available" or fc.upper_peak is None:
            return _r("forecast.support", "post_simulation", GateStatus.UNKNOWN, "forecast_refused")
    refs = [fc.forecast_id for fc in (ctx.evidence.forecast_cpu, ctx.evidence.forecast_memory) if fc]
    return _r("forecast.support", "post_simulation", GateStatus.PASS, "ok", refs)


# --------------------------------------------------------------- post_fit phase


def scheduler_fit(ctx: GateContext) -> GateResult:
    refs = (ctx.placement_ref,) if ctx.placement_ref else ()
    status = ctx.placement_status
    if status == "feasible":
        return _r("scheduler.fit", "post_fit", GateStatus.PASS, "ok", refs)
    if status == "infeasible":
        return _r("scheduler.fit", "post_fit", GateStatus.FAIL, "node_fit_failure", refs)
    return _r("scheduler.fit", "post_fit", GateStatus.UNKNOWN, "scheduler_unsupported", refs)


def hpa_compatibility(ctx: GateContext) -> GateResult:
    """S23 P0 compatibility gate: an active CPU-utilization HPA plus a CPU request change is
    unmodeled coupling (F06: 140m/200m=70% becomes 140% at 100m)."""
    hpa = ctx.evidence.hpa
    if hpa is None or not hpa.status_known:
        return _r("hpa.compatibility", "post_simulation", GateStatus.UNKNOWN, "hpa_status_unknown")
    if hpa.present and ctx.changes(ResourceField.CPU_REQUEST):
        return _r("hpa.compatibility", "post_simulation", GateStatus.UNKNOWN, "hpa_request_coupling_unmodeled")
    return _r("hpa.compatibility", "post_simulation", GateStatus.PASS, "ok")


def host_safety(ctx: GateContext) -> GateResult:
    state = ctx.evidence.host_safety
    if state == "ok":
        return _r("host.safety", "post_simulation", GateStatus.PASS, "ok")
    if state in ("pressure", "exclusive"):
        return _r(
            "host.safety",
            "post_simulation",
            GateStatus.FAIL,
            "host_pressure" if state == "pressure" else "exclusive_experiment",
        )
    return _r("host.safety", "post_simulation", GateStatus.UNKNOWN, "host_safety_unknown")


def change_disruption(ctx: GateContext) -> GateResult:
    strategy = ctx.target.strategy
    if ctx.target.workload_kind != "Deployment":
        return _r("change.disruption", "post_fit", GateStatus.NOT_APPLICABLE, "no_rollout_for_template")
    if strategy.kind != "RollingUpdate":
        return _r("change.disruption", "post_fit", GateStatus.UNKNOWN, "rollout_unmodeled")
    if strategy.max_unavailable >= ctx.target.replicas and ctx.policy.criticality == Criticality.CRITICAL:
        return _r("change.disruption", "post_fit", GateStatus.FAIL, "disruption_risk")
    if strategy.max_surge > 0:
        if ctx.surge_placement_status == "infeasible":
            return _r("change.disruption", "post_fit", GateStatus.FAIL, "disruption_risk")
        if ctx.surge_placement_status != "feasible":
            return _r("change.disruption", "post_fit", GateStatus.UNKNOWN, "rollout_overlap_unknown")
    return _r("change.disruption", "post_fit", GateStatus.PASS, "ok")


PRE_CANDIDATE = (identity_scope, target_allowlist, evidence_integrity, action_supported)
CANDIDATE = (resources_valid, memory_oom_spike)
POST_FIT = (scheduler_fit, change_disruption)
POST_SIMULATION = (slo_latency_errors, slo_queue_completion, forecast_support, hpa_compatibility, host_safety)


def evaluate_all(ctx: GateContext) -> list[GateResult]:
    return [rule(ctx) for rule in PRE_CANDIDATE + CANDIDATE + POST_FIT + POST_SIMULATION]


def classify(results: list[GateResult]) -> Eligibility:
    """Domain E §5 oracle: any required fail -> rejected; any required non-pass -> indeterminate."""
    mandatory = [r for r in results if r.required and r.status != GateStatus.NOT_APPLICABLE]
    if any(r.status == GateStatus.FAIL for r in mandatory):
        return Eligibility.REJECTED
    if any(r.status != GateStatus.PASS for r in mandatory):
        return Eligibility.INDETERMINATE
    return Eligibility.QUALIFIED
