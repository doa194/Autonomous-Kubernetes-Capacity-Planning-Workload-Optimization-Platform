"""Current assessment, W05/W06 run-conditional envelopes and aggregate projection.

These three outputs are deliberately *not* forecasts from the model portfolio:
* the current assessment is S11 reservations plus recent S12 observations (§1);
* W05/W06 envelopes are conditional on an explicitly declared run (§10);
* aggregate projections sum compatible predictions but carry no joint coverage claim (§11).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from pydantic import BaseModel, Field


class CurrentAssessment(BaseModel):
    """Scheduling headroom and consumption pressure are different numbers on purpose."""

    allocatableCpuM: int
    assignedRequestCpuM: int
    observedCpuM: float | None
    schedulingHeadroomCpuM: int
    consumptionHeadroomCpuM: float | None
    consumptionPressure: float | None
    allocatableMemoryBytes: int
    assignedRequestMemoryBytes: int
    observedMemoryBytes: float | None
    schedulingHeadroomMemoryBytes: int
    caveats: list[str]


def current_assessment(
    allocatable_cpu_m: int,
    assigned_cpu_m: int,
    observed_cpu_m: float | None,
    allocatable_mem: int,
    assigned_mem: int,
    observed_mem: float | None,
) -> CurrentAssessment:
    return CurrentAssessment(
        allocatableCpuM=allocatable_cpu_m,
        assignedRequestCpuM=assigned_cpu_m,
        observedCpuM=observed_cpu_m,
        schedulingHeadroomCpuM=allocatable_cpu_m - assigned_cpu_m,
        consumptionHeadroomCpuM=None if observed_cpu_m is None else allocatable_cpu_m - observed_cpu_m,
        consumptionPressure=None
        if observed_cpu_m is None or allocatable_cpu_m == 0
        else observed_cpu_m / allocatable_cpu_m,
        allocatableMemoryBytes=allocatable_mem,
        assignedRequestMemoryBytes=assigned_mem,
        observedMemoryBytes=observed_mem,
        schedulingHeadroomMemoryBytes=allocatable_mem - assigned_mem,
        caveats=[
            "scheduling_headroom_is_not_placement_guarantee",
            "consumption_headroom_is_not_schedulable_space",
        ],
    )


class JobRunSummary(BaseModel):
    profileHash: str
    durationSeconds: float = Field(gt=0)
    peakCpuMillicores: float | None = Field(default=None, ge=0)
    peakMemoryBytes: float | None = Field(default=None, ge=0)
    sourceFidelity: str  # application_reported | scrape_sampled


class RunDemandEnvelope(BaseModel):
    status: str  # available | refused
    workloadId: str
    conditionalOnProfileHash: str
    comparableRuns: int
    durationSecondsUpper: float | None = None
    cpuMillicoresUpper: float | None = None
    memoryBytesUpper: float | None = None
    nominalCoverage: None = None  # never claimed for small run samples
    reasonCodes: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


MIN_COMPARABLE_RUNS = 3  # provisional; S31 calibrates


def run_envelope(
    workload_id: str, profile_hash: str, runs: list[JobRunSummary], margin: float = 0.2
) -> RunDemandEnvelope:
    """Conservative envelope for one declared future run of W05/W06.

    Only runs of the *same* pinned profile count. A run with no sampled peak contributes
    duration only; it never contributes a zero peak.
    """
    comparable = [r for r in runs if r.profileHash == profile_hash]
    base = {"workloadId": workload_id, "conditionalOnProfileHash": profile_hash, "comparableRuns": len(comparable)}
    if len(comparable) < MIN_COMPARABLE_RUNS:
        return RunDemandEnvelope(status="refused", reasonCodes=["insufficient_history"], **base)
    cpu = [r.peakCpuMillicores for r in comparable if r.peakCpuMillicores is not None]
    mem = [r.peakMemoryBytes for r in comparable if r.peakMemoryBytes is not None]
    caveats = ["conditional_on_declared_run", "sampled_peaks_not_true_maxima"]
    if len(mem) < len(comparable):
        caveats.append("some_runs_without_memory_samples")
    return RunDemandEnvelope(
        status="available",
        durationSecondsUpper=max(r.durationSeconds for r in comparable) * (1 + margin),
        cpuMillicoresUpper=max(cpu) * (1 + margin) if cpu else None,
        memoryBytesUpper=max(mem) * (1 + margin) if mem else None,
        caveats=caveats,
        **base,
    )


@dataclass(frozen=True)
class AggregateProjection:
    points: list[float]
    upper_envelope: list[float]
    interval_kind: str = "conservative_envelope"
    nominal_coverage: None = None
    joint_calibration_status: str = "not_validated"


def aggregate(points: list[list[float]], uppers: list[list[float]]) -> AggregateProjection:
    """Sum aligned per-workload predictions. Summing marginal upper bounds gives a
    conservative envelope, not a calibrated joint interval (§11 Case C)."""
    return AggregateProjection(
        points=list(np.sum(np.array(points), axis=0)),
        upper_envelope=list(np.sum(np.array(uppers), axis=0)),
    )
