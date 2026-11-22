"""S19 hierarchical optimization policy and tighten-only resolution.

Layers merge global -> cluster -> namespace -> criticality -> workload. A more specific
layer may only make a safety parameter *stricter* (raise a floor, lower an allowed
decrease, narrow the action set). An attempt to relax produces `policy_conflict` and the
whole resolution fails closed (S19 §2). The resolved policy is immutable and identified by
its canonical digest, which every candidate, evaluation and plan pins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from capacity_platform.contracts.canonical import digest
from capacity_platform.contracts.enums import Criticality, ResourceField
from capacity_platform.contracts.errors import ErrorCategory, PlatformError

# Direction of "safer" for each tunable. +1: a larger value is safer; -1: smaller is safer.
SAFETY_DIRECTION: dict[str, int] = {
    "cpu_request_floor_m": +1,
    "memory_request_floor_bytes": +1,
    "cpu_margin": +1,
    "memory_margin": +1,
    "max_decrease_fraction": -1,
    "max_increase_fraction": -1,
    "p95_latency_ms_max": -1,
    "error_rate_max": -1,
    "throttle_ratio_max": -1,
    "min_slo_requests": +1,
    "min_completion_rate": +1,
    "min_cpu_coverage": +1,
    "min_memory_coverage": +1,
    "max_memory_gap_seconds": -1,
}
BOOLEAN_SAFER_TRUE = {"require_forecast_for_downsize"}

NON_OVERRIDABLE = ("identity.scope", "target.allowlist", "evidence.integrity", "action.supported")


class PolicyConflict(PlatformError):
    def __init__(self, message: str) -> None:
        super().__init__("policy_conflict", ErrorCategory.PRECONDITION, message)


class ClassParameters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cpu_request_floor_m: int = Field(ge=1)
    memory_request_floor_bytes: int = Field(ge=1)
    cpu_margin: float = Field(ge=0, le=5)
    memory_margin: float = Field(ge=0, le=5)
    max_decrease_fraction: float = Field(ge=0, le=0.9)
    max_increase_fraction: float = Field(ge=0, le=5)
    require_forecast_for_downsize: bool
    p95_latency_ms_max: float | None = None
    error_rate_max: float | None = None
    throttle_ratio_max: float | None = None
    min_slo_requests: int = 0
    min_completion_rate: float | None = None
    min_cpu_coverage: float = Field(ge=0, le=1)
    min_memory_coverage: float = Field(ge=0, le=1)
    max_memory_gap_seconds: int = Field(ge=0)


class ObjectiveWeights(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    efficiency: float = Field(ge=0)
    fit_benefit: float = Field(ge=0)
    risk: float = Field(ge=0)
    change_cost: float = Field(ge=0)
    pareto_tolerance: float = Field(ge=0, le=0.5)


class PolicyDocument(BaseModel):
    """The versioned YAML file shape (configs/policies/optimization-policy.yaml)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schemaVersion: str
    policyId: str
    revision: int = Field(gt=0)
    classification: str
    allowedActions: list[ResourceField]
    allowedNamespaces: list[str]
    maxCandidates: int = Field(gt=0, le=32)
    cpuGridMillicores: int = Field(gt=0)
    memoryGridBytes: int = Field(gt=0)
    weights: ObjectiveWeights
    classes: dict[Criticality, ClassParameters]
    clusterOverrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    namespaceOverrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    workloadOverrides: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("schemaVersion")
    @classmethod
    def _version(cls, v: str) -> str:
        if v != "optimization-policy/v1alpha1":
            raise ValueError("unsupported optimization policy schema")
        return v


class EffectivePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)
    policyId: str
    revision: int
    workloadId: str
    criticality: Criticality
    allowedActions: tuple[ResourceField, ...]
    maxCandidates: int
    cpuGridMillicores: int
    memoryGridBytes: int
    weights: ObjectiveWeights
    params: ClassParameters
    nonOverridable: tuple[str, ...] = NON_OVERRIDABLE
    digest: str = ""

    def with_digest(self) -> EffectivePolicy:
        return self.model_copy(update={"digest": digest(self, exclude=frozenset({"digest"}))})


def _tighten(base: dict[str, Any], override: dict[str, Any], layer: str) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key == "allowedActions":
            continue  # handled separately, may only narrow
        if key not in base:
            raise PolicyConflict(f"{layer}: unknown policy parameter {key!r}")
        current = base[key]
        if key in SAFETY_DIRECTION:
            if current is None:
                merged[key] = value
                continue
            if value is None:
                raise PolicyConflict(f"{layer}: may not remove safety parameter {key}")
            if (value - current) * SAFETY_DIRECTION[key] < 0:
                raise PolicyConflict(f"{layer}: {key} {value} relaxes inherited {current}")
        elif key in BOOLEAN_SAFER_TRUE:
            if current and not value:
                raise PolicyConflict(f"{layer}: may not disable {key}")
        merged[key] = value
    return merged


def resolve_effective_policy(
    document: PolicyDocument,
    *,
    cluster_id: str,
    namespace: str,
    workload_id: str,
    criticality: Criticality,
) -> EffectivePolicy:
    if namespace not in document.allowedNamespaces:
        raise PlatformError("target_disallowed", ErrorCategory.PRECONDITION, "namespace is not optimizable")
    if criticality not in document.classes:
        raise PolicyConflict(f"no class policy for {criticality}")
    params = document.classes[criticality].model_dump()
    actions = set(document.allowedActions)
    for layer, override in (
        ("cluster", document.clusterOverrides.get(cluster_id, {})),
        ("namespace", document.namespaceOverrides.get(namespace, {})),
        ("workload", document.workloadOverrides.get(workload_id, {})),
    ):
        params = _tighten(params, override, layer)
        if "allowedActions" in override:
            narrowed = {ResourceField(a) for a in override["allowedActions"]}
            if not narrowed <= actions:
                raise PolicyConflict(f"{layer}: may not add actions beyond {sorted(actions)}")
            actions = narrowed
    return EffectivePolicy(
        policyId=document.policyId,
        revision=document.revision,
        workloadId=workload_id,
        criticality=criticality,
        allowedActions=tuple(sorted(actions)),
        maxCandidates=document.maxCandidates,
        cpuGridMillicores=document.cpuGridMillicores,
        memoryGridBytes=document.memoryGridBytes,
        weights=document.weights,
        params=ClassParameters(**params),
    ).with_digest()


def load_policy(path: Path) -> PolicyDocument:
    with path.open(encoding="utf-8") as handle:
        return PolicyDocument.model_validate(yaml.safe_load(handle))
