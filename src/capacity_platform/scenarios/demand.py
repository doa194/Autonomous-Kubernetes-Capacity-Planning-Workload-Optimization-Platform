"""Seed-pinned deterministic demand plans (S05 §4.1).

The same scenario digest + seed + generator revision always yields the same planned
arrival schedule and payloads (S05-NFR-001). Each (phase, action) gets its own named random
stream derived from the seed, so editing one action never shifts another action's
schedule. The plan guarantees the *planned* traffic only; measured latency, CPU and Pod
placement will still vary run to run on shared hardware.
"""

from __future__ import annotations

import hashlib

import numpy as np
from pydantic import BaseModel

from capacity_platform.contracts.canonical import digest
from capacity_platform.scenarios.schema import ExperimentScenario, IdleAction, JobAction, TrafficAction

GENERATOR_REVISION = "demand-plan-v1"


class PlannedArrival(BaseModel):
    offsetMs: int
    payloadSeed: int


class ActionPlan(BaseModel):
    phaseId: str
    actionIndex: int
    kind: str
    target: str | None
    operation: str | None = None
    profile: str | None = None
    arrivals: list[PlannedArrival] = []
    measurement: str


class DemandPlan(BaseModel):
    scenarioId: str
    scenarioRevision: int
    scenarioDigest: str
    seed: int
    generatorRevision: str = GENERATOR_REVISION
    phases: list[dict]
    actions: list[ActionPlan]
    planDigest: str = ""


def _stream(seed: int, *names: str) -> np.random.Generator:
    material = hashlib.sha256("|".join([str(seed), *names]).encode()).digest()
    return np.random.Generator(np.random.PCG64(int.from_bytes(material[:16], "big")))


def _rate_at(action: TrafficAction, t: float, duration: float) -> float:
    base, peak = action.requestsPerSecond, action.peakRequestsPerSecond or action.requestsPerSecond
    if action.model == "constant":
        return base
    if action.model == "stepBurst":  # burst in the middle third of the phase
        return peak if duration / 3 <= t < 2 * duration / 3 else base
    if action.model == "ramp":
        return base + (peak - base) * (t / duration)
    period = action.periodSeconds or 60
    return peak if (t % period) < period / 2 else base


def build_plan(scenario: ExperimentScenario, seed_override: int | None = None) -> DemandPlan:
    seed = scenario.spec.execution.seed if seed_override is None else seed_override
    actions: list[ActionPlan] = []
    phases, offset_ms = [], 0
    for phase in scenario.spec.phases:
        phases.append(
            {
                "id": phase.id,
                "startOffsetMs": offset_ms,
                "durationSeconds": phase.durationSeconds,
                "measurement": phase.measurement,
            }
        )
        for idx, action in enumerate(phase.actions):
            if isinstance(action, IdleAction):
                actions.append(
                    ActionPlan(
                        phaseId=phase.id, actionIndex=idx, kind="idle", target=None, measurement=phase.measurement
                    )
                )
                continue
            if isinstance(action, JobAction):
                actions.append(
                    ActionPlan(
                        phaseId=phase.id,
                        actionIndex=idx,
                        kind="batchJob",
                        target=action.target,
                        profile=action.profile,
                        measurement=phase.measurement,
                    )
                )
                continue
            rng = _stream(seed, phase.id, str(idx), "interarrival")
            payload_rng = _stream(seed, phase.id, str(idx), "payload")
            arrivals: list[PlannedArrival] = []
            t, duration = 0.0, float(phase.durationSeconds)
            # Open-loop non-homogeneous Poisson arrivals via thinning against the peak rate.
            peak = max(action.requestsPerSecond, action.peakRequestsPerSecond or 0)
            while True:
                t += float(rng.exponential(1.0 / peak))
                if t >= duration:
                    break
                if float(rng.random()) <= _rate_at(action, t, duration) / peak:
                    arrivals.append(
                        PlannedArrival(
                            offsetMs=offset_ms + int(t * 1000), payloadSeed=int(payload_rng.integers(0, 2**31 - 1))
                        )
                    )
            actions.append(
                ActionPlan(
                    phaseId=phase.id,
                    actionIndex=idx,
                    kind="httpTraffic",
                    target=action.target,
                    operation=action.operation,
                    profile=action.profile,
                    arrivals=arrivals,
                    measurement=phase.measurement,
                )
            )
        offset_ms += phase.durationSeconds * 1000
    plan = DemandPlan(
        scenarioId=scenario.metadata.id,
        scenarioRevision=scenario.metadata.revision,
        scenarioDigest=scenario.digest(),
        seed=seed,
        phases=phases,
        actions=actions,
    )
    return plan.model_copy(update={"planDigest": digest(plan, exclude=frozenset({"planDigest"}))})
