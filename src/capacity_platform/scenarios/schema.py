"""`ExperimentScenario v1alpha1` (S05 §3): versioned, declarative, bounded.

Tier-1 validation lives here: strict structure, unknown fields rejected, finite bounds, only
catalogued targets, operations and fault IDs, and phase durations that fit inside the
declared maximum. There is no field that can carry a URL, script, image or command, so a
scenario file cannot smuggle in arbitrary behaviour.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from capacity_platform.contracts.canonical import digest
from capacity_platform.contracts.workloads import CATALOG

# Server-side S04 maxima are enforced again by the workloads themselves.
OPERATIONS = {
    "w01-latency-api": {"compute": {"idle", "normal", "heavy"}},
    "w02-burst-api": {"process": {"normal", "burst"}},
    "w03-memory-profile": {"profileRun": {"steady", "ramp", "spike", "retain"}},
    "w04-cpu-worker": {"task": {"light", "normal", "heavy"}},
}
# Batch runs may only select a profile the batch program itself knows (workloads/python/batch.py).
BATCH_PROFILES = {
    "w05-analytics-processor": {"analytics-small", "analytics-large"},
    "w06-opportunistic-batch": {"batch-short", "batch-long"},
}
HARD_LIMITS = {"maxDurationSeconds": 3600, "requestsPerSecond": 50, "maxInflight": 64, "maxParallelDrivers": 2}
FAULTS = {"F01", "F02", "F03", "F04", "F05"}  # F06 needs a separate capability; F08/F09 deferred


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Target(_Strict):
    workloadId: str
    profileRevision: int = Field(gt=0)
    namespace: str


class Execution(_Strict):
    seed: int = Field(ge=0, le=2**63 - 1)
    executionClass: Literal["light", "moderate", "exclusive"]
    mode: Literal["live"]
    maxDurationSeconds: int = Field(gt=0, le=HARD_LIMITS["maxDurationSeconds"])
    maxParallelDrivers: int = Field(gt=0, le=HARD_LIMITS["maxParallelDrivers"])


class TrafficAction(_Strict):
    kind: Literal["httpTraffic"]
    target: str
    operation: str
    model: Literal["constant", "stepBurst", "ramp", "periodic"]
    requestsPerSecond: float = Field(gt=0, le=HARD_LIMITS["requestsPerSecond"])
    peakRequestsPerSecond: float | None = Field(default=None, gt=0, le=HARD_LIMITS["requestsPerSecond"])
    periodSeconds: int | None = Field(default=None, gt=0, le=600)
    profile: str


class JobAction(_Strict):
    kind: Literal["batchJob"]
    target: Literal["w05-analytics-processor", "w06-opportunistic-batch"]
    profile: str
    expectedUnschedulable: bool = False


class IdleAction(_Strict):
    kind: Literal["idle"]


class Phase(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,30}$")
    durationSeconds: int = Field(gt=0, le=1800)
    measurement: Literal["included", "excluded"]
    actions: list[TrafficAction | JobAction | IdleAction] = Field(min_length=1, max_length=4)


class Safety(_Strict):
    maxInflightRequests: int = Field(gt=0, le=HARD_LIMITS["maxInflight"])
    requestTimeoutSeconds: float = Field(gt=0, le=30)
    hostPressurePolicyRef: str
    abortOnUnexpectedOOM: bool = True


class Cleanup(_Strict):
    deleteOwnedDriverJobs: Literal[True] = True
    retainArtifacts: bool = True


class Spec(_Strict):
    targets: list[Target] = Field(min_length=1, max_length=3)
    execution: Execution
    phases: list[Phase] = Field(min_length=1, max_length=8)
    faults: list[str] = Field(default_factory=list)
    safety: Safety
    cleanup: Cleanup = Field(default_factory=Cleanup)
    baselineClass: Literal["B0", "B1", "B2", "B3"] | None = None


class Metadata(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,60}$")
    revision: int = Field(gt=0)


class ExperimentScenario(_Strict):
    apiVersion: Literal["capacity.lab/v1alpha1"]
    kind: Literal["ExperimentScenario"]
    metadata: Metadata
    spec: Spec

    @model_validator(mode="after")
    def _semantic(self) -> ExperimentScenario:
        targets = {t.workloadId for t in self.spec.targets}
        for t in self.spec.targets:
            entry = CATALOG.get(t.workloadId)
            if entry is None or entry.namespace != t.namespace:
                raise ValueError(f"unknown target or namespace: {t.workloadId}")
        total = sum(p.durationSeconds for p in self.spec.phases)
        if total + 60 > self.spec.execution.maxDurationSeconds:  # 60 s guard margin for cleanup
            raise ValueError("phase durations plus guard margin exceed maxDurationSeconds")
        for phase in self.spec.phases:
            for action in phase.actions:
                if isinstance(action, TrafficAction):
                    if action.target not in targets:
                        raise ValueError(f"action target {action.target} not declared in targets")
                    allowed = OPERATIONS.get(action.target, {}).get(action.operation)
                    if allowed is None or action.profile not in allowed:
                        raise ValueError(f"operation/profile not allowlisted for {action.target}")
                    if action.model in ("stepBurst", "ramp") and action.peakRequestsPerSecond is None:
                        raise ValueError(f"{action.model} requires peakRequestsPerSecond")
                    if action.model == "periodic" and action.periodSeconds is None:
                        raise ValueError("periodic requires periodSeconds")
                elif isinstance(action, JobAction):
                    if action.target not in targets:
                        raise ValueError(f"action target {action.target} not declared in targets")
                    if action.profile not in BATCH_PROFILES[action.target]:
                        raise ValueError(f"batch profile not allowlisted for {action.target}")
        pattern = "".join("i" if p.measurement == "included" else "e" for p in self.spec.phases)
        import re as _re

        if not _re.fullmatch(r"e*i+e*", pattern):
            raise ValueError("phases must be warmup (excluded)* -> measured (included)+ -> cooldown (excluded)*")
        if not set(self.spec.faults) <= FAULTS:
            raise ValueError("fault not in the approved P0 catalog")
        return self

    def digest(self) -> str:
        return digest(self)


def load_scenario(path: Path) -> ExperimentScenario:
    with path.open(encoding="utf-8") as handle:
        return ExperimentScenario.model_validate(yaml.safe_load(handle))
