"""S20 deterministic bounded right-sizing candidate generation.

For each supported field the generator builds a *protected demand envelope* from qualified
evidence (observed tail + eligible S14 forecast upper bound) plus a class-specific policy
margin, then enumerates a small fixed set of values:

    nominal      = round_up(envelope)
    conservative = round_up(envelope * 1.15)
    budget_edge  = the largest decrease the change budget allows (if above the envelope)

Values are clamped to the policy floor and the change budget, deduplicated and sorted, so
the same inputs always produce the same candidate list and digests. If the evidence for a
field is not qualified, the generator returns a typed refusal for that field instead of
inventing numbers. Increases are generated too: under-reservation is a reliability risk.
"""

from __future__ import annotations

import math

from pydantic import BaseModel

from capacity_platform.capacity.candidates import CandidateSource, ResourceCandidate, make_change
from capacity_platform.capacity.inputs import AnalysisEvidence, TargetState
from capacity_platform.capacity.policy.model import EffectivePolicy
from capacity_platform.contracts.canonical import digest
from capacity_platform.contracts.enums import Horizon, ResourceField
from capacity_platform.contracts.workloads import CATALOG

GENERATOR_REVISION = "s20-generator-v1"
CONSERVATIVE_FACTOR = 1.15

STRATEGIES = {
    "w01-latency-api": "critical_latency_protected_cpu",
    "w02-burst-api": "burst_tail_aware",
    "w03-memory-profile": "retained_working_set_tail",
    "w04-cpu-worker": "cpu_with_queue_guardrail",
    "w05-analytics-processor": "run_conditional_template_advisory",
    "w06-opportunistic-batch": "run_conditional_template_advisory",
}


class FieldRefusal(BaseModel):
    field: ResourceField
    reasonCode: str


class CandidateSet(BaseModel):
    candidates: list[ResourceCandidate]
    refusals: list[FieldRefusal]
    envelopes: dict[str, float]
    generatorRevision: str = GENERATOR_REVISION


def _round_up(value: float, grid: int) -> int:
    return int(math.ceil(value / grid) * grid)


def _levels(current: int, envelope: float, floor: int, grid: int, max_dec: float, max_inc: float) -> list[int]:
    lowest = math.ceil(current * (1 - max_dec))
    highest = math.floor(current * (1 + max_inc))
    raw = {
        _round_up(envelope, grid),
        _round_up(envelope * CONSERVATIVE_FACTOR, grid),
    }
    budget_edge = _round_up(lowest, grid)
    if budget_edge >= envelope:
        raw.add(budget_edge)
    under_reserved = current < envelope
    out = set()
    for value in raw:
        value = max(value, floor)
        # Clamp to the change budget; the resources.valid gate re-checks independently.
        value = max(min(value, highest), lowest)
        # Increases only answer under-reservation; decreases only answer over-reservation.
        if value == current or (value > current) != under_reserved:
            continue
        out.add(value)
    return sorted(out)


def generate_candidates(
    target: TargetState,
    evidence: AnalysisEvidence,
    policy: EffectivePolicy,
    horizon: Horizon = Horizon.ONE_HOUR,
) -> CandidateSet:
    p = policy.params
    container = target.target.containerName
    allowed = set(policy.allowedActions)
    refusals: list[FieldRefusal] = []
    envelopes: dict[str, float] = {}
    singles: list[ResourceCandidate] = []
    source = CandidateSource(
        inventorySnapshotRef=evidence.inventory_snapshot_ref,
        observationRefs=sorted(
            r.observation_ref for r in (evidence.cpu, evidence.memory, evidence.slo, evidence.queue) if r is not None
        ),
        forecastRefs=sorted(f.forecast_id for f in (evidence.forecast_cpu, evidence.forecast_memory) if f is not None),
        effectivePolicyDigest=policy.digest,
        evidenceManifestDigest=digest(evidence),
    )
    strategy = STRATEGIES.get(target.target.logicalWorkloadId, "generic")
    entry = CATALOG.get(target.target.logicalWorkloadId)

    def build(changes: dict[ResourceField, int | None], basis: list[str]) -> ResourceCandidate:
        fields = tuple(make_change(f, container, target.current.get(f), v) for f, v in changes.items())
        assumptions = [f"horizon:{horizon.value}"]
        if entry is not None and not entry.continuous:
            assumptions.append("conditional_on_declared_job_run")
        return ResourceCandidate(
            target=target.target,
            fields=fields,
            source=source,
            horizon=horizon,
            strategy=strategy,
            basis=basis,
            assumptions=assumptions,
        ).finalize()

    # ---- CPU -------------------------------------------------------------------
    cpu_levels: list[int] = []
    cpu = evidence.cpu
    if cpu is None or cpu.coverage < p.min_cpu_coverage or cpu.samples == 0:
        refusals.append(FieldRefusal(field=ResourceField.CPU_REQUEST, reasonCode="coverage_insufficient"))
    elif target.current.cpu_request_m is None:
        refusals.append(FieldRefusal(field=ResourceField.CPU_REQUEST, reasonCode="current_request_absent"))
    else:
        demand = cpu.p95_m
        basis = ["observed_cpu_p95"]
        fc = evidence.forecast_cpu
        if fc is not None and fc.status == "available" and fc.upper_peak is not None:
            demand = max(demand, fc.upper_peak)
            basis.append("s14_forecast_upper")
        envelope = demand * (1 + p.cpu_margin)
        envelopes["cpu_m"] = envelope
        if ResourceField.CPU_REQUEST in allowed:
            cpu_levels = _levels(
                target.current.cpu_request_m,
                envelope,
                p.cpu_request_floor_m,
                policy.cpuGridMillicores,
                p.max_decrease_fraction,
                p.max_increase_fraction,
            )
            singles += [build({ResourceField.CPU_REQUEST: v}, basis) for v in cpu_levels]
        # Throttling with a limit present: offer a bounded limit increase (never a decrease here).
        lim = target.current.cpu_limit_m
        if (
            ResourceField.CPU_LIMIT in allowed
            and lim is not None
            and cpu.throttle_ratio_p95 is not None
            and p.throttle_ratio_max is not None
            and cpu.throttle_ratio_p95 > p.throttle_ratio_max
        ):
            raised = min(
                _round_up(max(lim * 1.25, cpu.max_sampled_m * 1.5), policy.cpuGridMillicores),
                math.floor(lim * (1 + p.max_increase_fraction)),
            )
            if raised > lim:
                singles.append(build({ResourceField.CPU_LIMIT: raised}, ["cpu_throttling_observed"]))

    # ---- memory ----------------------------------------------------------------
    mem_levels: list[int] = []
    mem = evidence.memory
    if mem is None or mem.coverage == 0:
        refusals.append(FieldRefusal(field=ResourceField.MEMORY_REQUEST, reasonCode="memory_evidence_missing"))
    elif target.current.memory_request_bytes is None:
        refusals.append(FieldRefusal(field=ResourceField.MEMORY_REQUEST, reasonCode="current_request_absent"))
    else:
        demand = max(mem.max_sampled_bytes, mem.p99_bytes)
        basis = ["observed_memory_max_sampled"]
        fm = evidence.forecast_memory
        if fm is not None and fm.status == "available" and fm.upper_peak is not None:
            demand = max(demand, fm.upper_peak)
            basis.append("s14_forecast_upper")
        envelope = demand * (1 + p.memory_margin)
        envelopes["memory_bytes"] = envelope
        if ResourceField.MEMORY_REQUEST in allowed:
            mem_levels = _levels(
                target.current.memory_request_bytes,
                envelope,
                p.memory_request_floor_bytes,
                policy.memoryGridBytes,
                p.max_decrease_fraction,
                p.max_increase_fraction,
            )
            # A memory request above the existing limit would be invalid; skip those levels.
            lim = target.current.memory_limit_bytes
            mem_levels = [v for v in mem_levels if lim is None or v <= lim]
            singles += [build({ResourceField.MEMORY_REQUEST: v}, basis) for v in mem_levels]
        lim = target.current.memory_limit_bytes
        if ResourceField.MEMORY_LIMIT in allowed and lim is not None and lim < envelope * (1 + p.memory_margin):
            raised = min(
                _round_up(envelope * (1 + p.memory_margin), policy.memoryGridBytes),
                math.floor(lim * (1 + p.max_increase_fraction)),
            )
            if raised > lim:
                singles.append(build({ResourceField.MEMORY_LIMIT: raised}, ["memory_limit_below_protected_tail"]))

    # ---- one compatible combination: nominal CPU + nominal memory request ----------------
    combos: list[ResourceCandidate] = []
    if cpu_levels and mem_levels:
        combos.append(
            build(
                {ResourceField.CPU_REQUEST: cpu_levels[0], ResourceField.MEMORY_REQUEST: mem_levels[0]},
                ["combined_nominal_levels"],
            )
        )

    unique = {c.candidateDigest: c for c in singles + combos}
    ordered = sorted(
        unique.values(),
        key=lambda c: (
            len(c.fields),
            tuple(f.field.value for f in c.fields),
            tuple(f.after or 0 for f in c.fields),
            c.candidateDigest,
        ),
    )
    return CandidateSet(candidates=ordered[: policy.maxCandidates], refusals=refusals, envelopes=envelopes)
