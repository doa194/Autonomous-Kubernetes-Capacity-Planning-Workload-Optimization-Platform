"""Typed, already-qualified evidence consumed by the S19-S22 engines.

The capacity engine works from an immutable bundle of references resolved by S11 (target
state, node accounting), S12 (observation summaries) and S14 (forecasts). Every optional
field is a deliberate "unknown": the gates turn `None` into `unknown`, never into a pass.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from capacity_platform.contracts.enums import Coverage, Freshness, ResourceField
from capacity_platform.contracts.identity import TargetRef

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class ContainerResourceState(BaseModel):
    """Current per-container template values. `None` limit means "absent", not zero."""

    model_config = _FROZEN
    cpu_request_m: int | None
    memory_request_bytes: int | None
    cpu_limit_m: int | None
    memory_limit_bytes: int | None

    def get(self, field: ResourceField) -> int | None:
        return {
            ResourceField.CPU_REQUEST: self.cpu_request_m,
            ResourceField.MEMORY_REQUEST: self.memory_request_bytes,
            ResourceField.CPU_LIMIT: self.cpu_limit_m,
            ResourceField.MEMORY_LIMIT: self.memory_limit_bytes,
        }[field]

    def with_changes(self, changes: dict[ResourceField, int | None]) -> ContainerResourceState:
        data = self.model_dump()
        mapping = {
            ResourceField.CPU_REQUEST: "cpu_request_m",
            ResourceField.MEMORY_REQUEST: "memory_request_bytes",
            ResourceField.CPU_LIMIT: "cpu_limit_m",
            ResourceField.MEMORY_LIMIT: "memory_limit_bytes",
        }
        for field, value in changes.items():
            data[mapping[field]] = value
        return ContainerResourceState(**data)

    def qos_class(self) -> str:
        """Kubernetes QoS for a single-container Pod (enough for the P0 demo controllers)."""
        if None in (self.cpu_request_m, self.memory_request_bytes):
            return "Burstable" if any(v is not None for v in self.model_dump().values()) else "BestEffort"
        if self.cpu_limit_m == self.cpu_request_m and self.memory_limit_bytes == self.memory_request_bytes:
            return "Guaranteed"
        return "Burstable"


class PodConstraints(BaseModel):
    model_config = _FROZEN
    node_selector: dict[str, str] = Field(default_factory=dict)
    # Required node-affinity terms: list of OR'd terms, each a list of AND'd expressions.
    required_node_affinity: list[list[dict]] = Field(default_factory=list)
    tolerations: list[dict] = Field(default_factory=list)
    # Constraints the S21 model does not implement (e.g. pod affinity, topology spread).
    unsupported: list[str] = Field(default_factory=list)


class RolloutStrategy(BaseModel):
    model_config = _FROZEN
    kind: Literal["RollingUpdate", "Recreate", "unknown"] = "RollingUpdate"
    max_surge: int = 1
    max_unavailable: int = 0


class TargetState(BaseModel):
    """S11 time-valid view of the optimization target."""

    model_config = _FROZEN
    target: TargetRef
    workload_kind: Literal["Deployment", "CronJob", "Job"]
    replicas: int = Field(ge=0)
    current: ContainerResourceState
    constraints: PodConstraints = Field(default_factory=PodConstraints)
    strategy: RolloutStrategy = Field(default_factory=RolloutStrategy)
    resource_version: str
    generation: int | None = None
    # Effective request of the other containers / overhead in the same Pod (usually zero).
    pod_overhead_cpu_m: int = 0
    pod_overhead_memory_bytes: int = 0
    quota_remaining_cpu_m: int | None = None
    quota_remaining_memory_bytes: int | None = None


class CpuEvidence(BaseModel):
    model_config = _FROZEN
    p95_m: float = Field(ge=0)
    max_sampled_m: float = Field(ge=0)
    mean_m: float = Field(ge=0)
    coverage: float = Field(ge=0, le=1)
    samples: int = Field(ge=0)
    throttle_ratio_p95: float | None = Field(default=None, ge=0, le=1)
    flags: list[str] = Field(default_factory=list)
    observation_ref: str


class MemoryEvidence(BaseModel):
    model_config = _FROZEN
    max_sampled_bytes: float = Field(ge=0)
    p99_bytes: float = Field(ge=0)
    coverage: float = Field(ge=0, le=1)
    max_gap_seconds: int = Field(ge=0)
    oom_events: int = Field(ge=0)
    restarts: int = Field(ge=0)
    flags: list[str] = Field(default_factory=list)
    observation_ref: str


class SloEvidence(BaseModel):
    model_config = _FROZEN
    p95_latency_ms: float | None
    error_rate: float | None = Field(default=None, ge=0, le=1)
    request_count: int = Field(ge=0)
    observation_ref: str


class QueueEvidence(BaseModel):
    model_config = _FROZEN
    queue_depth_trend_per_min: float
    completion_within_deadline_rate: float | None = Field(default=None, ge=0, le=1)
    samples: int = Field(ge=0)
    observation_ref: str


class ForecastSummary(BaseModel):
    model_config = _FROZEN
    forecast_id: str
    status: Literal["available", "refused", "not_applicable"]
    upper_peak: float | None
    policy_revision: str
    reason_codes: list[str] = Field(default_factory=list)


class HpaState(BaseModel):
    model_config = _FROZEN
    present: bool
    status_known: bool = True
    cpu_utilization_target: int | None = None


class AnalysisEvidence(BaseModel):
    model_config = _FROZEN
    inventory_snapshot_ref: str
    inventory_coverage: Coverage
    inventory_freshness: Freshness
    cpu: CpuEvidence | None = None
    memory: MemoryEvidence | None = None
    slo: SloEvidence | None = None
    queue: QueueEvidence | None = None
    forecast_cpu: ForecastSummary | None = None
    forecast_memory: ForecastSummary | None = None
    hpa: HpaState | None = None
    ownership: Literal["direct_managed", "gitops_managed", "ambiguous", "unsupported"] | None = None
    host_safety: Literal["ok", "pressure", "exclusive", "unknown"] = "unknown"
    scope_authorized: bool | None = None

    def refs(self) -> list[str]:
        out = [self.inventory_snapshot_ref]
        for item in (self.cpu, self.memory, self.slo, self.queue):
            if item is not None:
                out.append(item.observation_ref)
        for fc in (self.forecast_cpu, self.forecast_memory):
            if fc is not None:
                out.append(fc.forecast_id)
        return out
