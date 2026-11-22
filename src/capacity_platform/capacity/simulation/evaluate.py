"""S22 deterministic optimization evaluation: baseline -> candidates -> gates -> rank.

Stages (S22 §2): pin inputs, evaluate the unchanged baseline, generate S20 candidates,
apply them to a *copy* of the S11 scheduling state, run S21 fit for steady state and for
rollout overlap, run the S19 gates, classify, Pareto-filter the qualified ones and rank
them with versioned weights. Nothing here talks to Kubernetes.

Benefit kinds produced: `calculated_reservation_delta` and `modeled_scheduler_fit_delta`.
`verified_realized_delta` is produced only by S26 after a real, verified change.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, Field

from capacity_platform.capacity.candidates import ResourceCandidate
from capacity_platform.capacity.inputs import AnalysisEvidence, TargetState
from capacity_platform.capacity.placement.fit import (
    PLACEMENT_REVISION,
    NodeState,
    PodShape,
    fit_single,
    fragmentation,
)
from capacity_platform.capacity.policy.gates import (
    EVALUATOR_REVISION,
    PRE_CANDIDATE,
    GateContext,
    GateResult,
    classify,
    evaluate_all,
)
from capacity_platform.capacity.policy.model import EffectivePolicy
from capacity_platform.capacity.rightsizing.generator import FieldRefusal, generate_candidates
from capacity_platform.contracts.canonical import digest
from capacity_platform.contracts.enums import Eligibility, Horizon, ResourceField

SIMULATOR_REVISION = "s22-counterfactual-v1"


class PlacementSummary(BaseModel):
    status: str
    fitCapableNodes: int
    perNodeReasons: dict[str, list[str]]
    unknownConstraints: list[str]
    surgeStatus: str | None
    stranded_cpu_m: int
    stranded_memory_bytes: int
    algorithm: str = PLACEMENT_REVISION
    digest: str = ""


class Objectives(BaseModel):
    efficiency: float
    fit_benefit: float
    risk: float
    change_cost: float


class OptimizationEvaluation(BaseModel):
    schemaVersion: str = "optimization-evaluation/v1alpha1"
    candidate: ResourceCandidate
    policyDigest: str
    status: Eligibility
    hardGates: list[GateResult]
    placement: PlacementSummary
    reservationDeltaPerPod: dict[str, int]
    benefitKinds: list[str]
    objectives: Objectives | None = None
    paretoFront: int | None = None
    score: float | None = None
    rank: int | None = None
    empiricalValidation: Literal["not_performed"] = "not_performed"
    integrityDigest: str = ""


class AnalysisResult(BaseModel):
    schemaVersion: str = "optimization-analysis/v1alpha1"
    status: Literal["completed", "rejected_before_generation"]
    targetKey: str
    policyDigest: str
    horizon: Horizon
    baselineGates: list[GateResult]
    baselinePlacement: PlacementSummary
    refusals: list[FieldRefusal]
    evaluations: list[OptimizationEvaluation]
    revisions: dict[str, str] = Field(default_factory=dict)
    # Facts S24 needs for plan issuance, pinned inside the analysis digest.
    context: dict[str, str | None] = Field(default_factory=dict)
    analysisDigest: str = ""

    def qualified(self) -> list[OptimizationEvaluation]:
        return sorted((e for e in self.evaluations if e.status == Eligibility.QUALIFIED), key=lambda e: e.rank or 0)


def _shape(target: TargetState, cpu_m: int, memory_bytes: int) -> PodShape:
    c = target.constraints
    return PodShape(
        cpu_m=cpu_m + target.pod_overhead_cpu_m,
        memory_bytes=memory_bytes + target.pod_overhead_memory_bytes,
        node_selector=dict(c.node_selector),
        required_node_affinity=tuple(tuple(term) for term in c.required_node_affinity),
        tolerations=tuple(c.tolerations),
        unsupported=tuple(c.unsupported),
    )


def _without_target_pods(nodes: list[NodeState], target_pods: dict[str, int], shape: PodShape) -> list[NodeState]:
    """Steady state: the controller's current Pods are replaced, so their reservation leaves."""
    out = []
    for node in nodes:
        count = target_pods.get(node.name, 0)
        out.append(node.add(-shape.cpu_m * count, -shape.memory_bytes * count, -count) if count else node)
    return out


def _placement(nodes: list[NodeState], shape: PodShape, surge_nodes: list[NodeState] | None) -> PlacementSummary:
    fit = fit_single(nodes, shape)
    frag = fragmentation(nodes, shape)
    surge = fit_single(surge_nodes, shape).status if surge_nodes is not None else None
    summary = PlacementSummary(
        status=fit.status,
        fitCapableNodes=fit.fit_capable_nodes,
        perNodeReasons={n.node: list(n.reasons) for n in fit.per_node if n.reasons},
        unknownConstraints=sorted({u for n in fit.per_node for u in n.unknown}),
        surgeStatus=surge,
        stranded_cpu_m=frag.stranded_cpu_m_for_shape,
        stranded_memory_bytes=frag.stranded_memory_bytes_for_shape,
    )
    return summary.model_copy(update={"digest": digest(summary, exclude=frozenset({"digest"}))})


def _objectives(
    target: TargetState,
    cand: ResourceCandidate,
    base: PlacementSummary,
    new: PlacementSummary,
    envelopes: dict[str, float],
    eligible_nodes: int,
) -> Objectives:
    # Each dimension is normalized separately; millicores and bytes are never added.
    fractions, risks = [], []
    for f in cand.fields:
        if f.before and f.after is not None and f.field in (ResourceField.CPU_REQUEST, ResourceField.MEMORY_REQUEST):
            fractions.append((f.before - f.after) / f.before)
            env = envelopes.get("cpu_m" if f.field == ResourceField.CPU_REQUEST else "memory_bytes")
            if env and f.after < f.before:
                risks.append(min(1.0, env / f.after))
            else:
                risks.append(0.0)
    efficiency = sum(fractions) / len(fractions) if fractions else 0.0
    fit_benefit = (new.fitCapableNodes - base.fitCapableNodes) / max(eligible_nodes, 1)
    magnitude = [abs((f.after - f.before) / f.before) for f in cand.fields if f.before and f.after is not None]
    change_cost = len(cand.fields) / 4 + (sum(magnitude) / len(magnitude) if magnitude else 0) / 2
    return Objectives(
        efficiency=round(efficiency, 9),
        fit_benefit=round(max(-1, min(1, fit_benefit)), 9),
        risk=round(sum(risks) / len(risks) if risks else 0.0, 9),
        change_cost=round(change_cost, 9),
    )


def _dominates(a: Objectives, b: Objectives, tol: float) -> bool:
    better_or_equal = (
        a.efficiency >= b.efficiency - tol
        and a.fit_benefit >= b.fit_benefit - tol
        and a.risk <= b.risk + tol
        and a.change_cost <= b.change_cost + tol
    )
    strictly = (
        a.efficiency > b.efficiency + tol
        or a.fit_benefit > b.fit_benefit + tol
        or a.risk < b.risk - tol
        or a.change_cost < b.change_cost - tol
    )
    return better_or_equal and strictly


def rank_qualified(evals: list[OptimizationEvaluation], policy: EffectivePolicy) -> list[OptimizationEvaluation]:
    """Only qualified candidates are ranked: Pareto fronts first, then weighted score, then digest."""
    w = policy.weights
    remaining = [e for e in evals if e.status == Eligibility.QUALIFIED and e.objectives is not None]
    ranked: list[OptimizationEvaluation] = []
    front = 1
    while remaining:
        current = [
            e
            for e in remaining
            if not any(
                _dominates(o.objectives, e.objectives, w.pareto_tolerance)  # type: ignore[arg-type]
                for o in remaining
                if o is not e
            )
        ]
        for e in current:
            o = e.objectives
            assert o is not None
            score = (
                w.efficiency * o.efficiency
                + w.fit_benefit * o.fit_benefit
                - w.risk * o.risk
                - w.change_cost * o.change_cost
            )
            ranked.append(e.model_copy(update={"paretoFront": front, "score": round(score, 9)}))
        remaining = [e for e in remaining if e not in current]
        front += 1
    ranked.sort(key=lambda e: (e.paretoFront, -(e.score or 0.0), e.candidate.candidateDigest))
    return [e.model_copy(update={"rank": i + 1}) for i, e in enumerate(ranked)]


def evaluate(
    target: TargetState,
    evidence: AnalysisEvidence,
    policy: EffectivePolicy,
    nodes: list[NodeState],
    *,
    target_pods_per_node: dict[str, int] | None = None,
    horizon: Horizon = Horizon.ONE_HOUR,
    intended_use: str = "governed_execution",
) -> AnalysisResult:
    target_pods_per_node = target_pods_per_node or {}
    cur = target.current
    base_shape = _shape(target, cur.cpu_request_m or 0, cur.memory_request_bytes or 0)
    steady_nodes = _without_target_pods(nodes, target_pods_per_node, base_shape)
    baseline_placement = _placement(steady_nodes, base_shape, None)

    base_ctx = GateContext(target=target, evidence=evidence, policy=policy, intended_use=intended_use)
    baseline_gates = [rule(base_ctx) for rule in PRE_CANDIDATE]
    revisions = {"simulator": SIMULATOR_REVISION, "gates": EVALUATOR_REVISION, "placement": PLACEMENT_REVISION}
    context = {
        "ownership": evidence.ownership,
        "workloadKind": target.workload_kind,
        "criticality": policy.criticality.value,
        "resourceVersion": target.resource_version,
        "intendedUse": intended_use,
    }

    def finish(result: AnalysisResult) -> AnalysisResult:
        return result.model_copy(update={"analysisDigest": digest(result, exclude=frozenset({"analysisDigest"}))})

    if classify(baseline_gates) == Eligibility.REJECTED:
        return finish(
            AnalysisResult(
                status="rejected_before_generation",
                targetKey=target.target.target_key(),
                policyDigest=policy.digest,
                horizon=horizon,
                baselineGates=baseline_gates,
                baselinePlacement=baseline_placement,
                refusals=[],
                evaluations=[],
                revisions=revisions,
                context=context,
            )
        )

    generated = generate_candidates(target, evidence, policy, horizon)
    eligible_nodes = sum(1 for n in nodes if n.ready and not n.unschedulable)
    evaluations: list[OptimizationEvaluation] = []
    for cand in generated.candidates:
        proposed = cur.with_changes(cand.changes())
        shape = replace(
            base_shape,
            cpu_m=(proposed.cpu_request_m or 0) + target.pod_overhead_cpu_m,
            memory_bytes=(proposed.memory_request_bytes or 0) + target.pod_overhead_memory_bytes,
        )
        # Rollout overlap: with maxSurge > 0 the new Pod must fit while old Pods still hold space.
        surge_nodes = nodes if target.workload_kind == "Deployment" and target.strategy.max_surge > 0 else None
        placement = _placement(steady_nodes, shape, surge_nodes)
        ctx = GateContext(
            target=target,
            evidence=evidence,
            policy=policy,
            intended_use=intended_use,
            candidate=cand,
            placement_status=placement.status,
            placement_ref=placement.digest,
            surge_placement_status=placement.surgeStatus,
        )
        gates = evaluate_all(ctx)
        status = classify(gates)
        objectives = (
            _objectives(target, cand, baseline_placement, placement, generated.envelopes, eligible_nodes)
            if status == Eligibility.QUALIFIED
            else None
        )
        ev = OptimizationEvaluation(
            candidate=cand,
            policyDigest=policy.digest,
            status=status,
            hardGates=gates,
            placement=placement,
            reservationDeltaPerPod=cand.reservation_delta(),
            benefitKinds=["calculated_reservation_delta", "modeled_scheduler_fit_delta"],
            objectives=objectives,
        )
        evaluations.append(ev)

    ranked = {e.candidate.candidateDigest: e for e in rank_qualified(evaluations, policy)}
    final = []
    for e in evaluations:
        e = ranked.get(e.candidate.candidateDigest, e)
        final.append(e.model_copy(update={"integrityDigest": digest(e, exclude=frozenset({"integrityDigest"}))}))
    final.sort(key=lambda e: (e.rank is None, e.rank or 0, e.candidate.candidateDigest))
    return finish(
        AnalysisResult(
            status="completed",
            targetKey=target.target.target_key(),
            policyDigest=policy.digest,
            horizon=horizon,
            baselineGates=baseline_gates,
            baselinePlacement=baseline_placement,
            refusals=generated.refusals,
            evaluations=final,
            revisions=revisions,
            context=context,
        )
    )
