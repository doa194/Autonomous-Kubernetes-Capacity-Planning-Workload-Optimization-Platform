"""The six demonstration workloads (S04 §3) and the versioned WorkloadProfile contract.

The catalog fixes identity, namespace, class and PriorityClass. These mappings are
product policy: criticality is never inferred from a namespace or a Pod label at runtime.
Resource numbers in the catalog are the S04 provisional baseline, not measurements.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from capacity_platform.contracts.enums import Criticality
from capacity_platform.contracts.units import cpu_to_millicores, memory_to_bytes

DEMO_NAMESPACES = frozenset({"workloads-critical", "workloads-standard", "workloads-batch"})

# Platform namespaces are never optimization targets (S19-FR-005).
PLATFORM_NAMESPACES = frozenset(
    {
        "capacity-api",
        "capacity-core",
        "capacity-ai",
        "capacity-execution",
        "capacity-data",
        "capacity-observability",
        "capacity-identity",
        "capacity-system",
        "capacity-experiments",
        "kube-system",
        "calico-system",
        "tigera-operator",
    }
)


class CatalogEntry(BaseModel):
    model_config = ConfigDict(frozen=True)
    workload_id: str
    name: str
    kind: Literal["Deployment", "CronJob", "Job"]
    namespace: str
    container: str
    criticality: Criticality
    priority_class: str
    runtime: Literal["dotnet", "python"]
    cpu_request_m: int
    cpu_limit_m: int
    memory_request_bytes: int
    memory_limit_bytes: int

    @property
    def continuous(self) -> bool:
        """W01-W04 have continuous demand; W05/W06 only have run-conditional demand."""
        return self.kind == "Deployment"


def _entry(wid, name, kind, ns, crit, pc, runtime, cpu_r, cpu_l, mem_r, mem_l) -> CatalogEntry:
    return CatalogEntry(
        workload_id=wid,
        name=name,
        kind=kind,
        namespace=ns,
        container="app",
        criticality=crit,
        priority_class=pc,
        runtime=runtime,
        cpu_request_m=cpu_to_millicores(cpu_r),
        cpu_limit_m=cpu_to_millicores(cpu_l),
        memory_request_bytes=memory_to_bytes(mem_r),
        memory_limit_bytes=memory_to_bytes(mem_l),
    )


CATALOG: dict[str, CatalogEntry] = {
    e.workload_id: e
    for e in (
        _entry(
            "w01-latency-api",
            "latency-api",
            "Deployment",
            "workloads-critical",
            Criticality.CRITICAL,
            "lab-critical",
            "dotnet",
            "200m",
            "500m",
            "256Mi",
            "384Mi",
        ),
        _entry(
            "w02-burst-api",
            "burst-api",
            "Deployment",
            "workloads-standard",
            Criticality.STANDARD,
            "lab-standard",
            "dotnet",
            "150m",
            "400m",
            "192Mi",
            "320Mi",
        ),
        _entry(
            "w03-memory-profile",
            "memory-profile",
            "Deployment",
            "workloads-standard",
            Criticality.STANDARD,
            "lab-standard",
            "python",
            "100m",
            "300m",
            "320Mi",
            "448Mi",
        ),
        _entry(
            "w04-cpu-worker",
            "cpu-worker",
            "Deployment",
            "workloads-batch",
            Criticality.FLEXIBLE,
            "lab-flexible",
            "python",
            "150m",
            "500m",
            "192Mi",
            "320Mi",
        ),
        _entry(
            "w05-analytics-processor",
            "analytics-processor",
            "CronJob",
            "workloads-batch",
            Criticality.FLEXIBLE,
            "lab-flexible",
            "python",
            "250m",
            "500m",
            "384Mi",
            "512Mi",
        ),
        _entry(
            "w06-opportunistic-batch",
            "opportunistic-batch",
            "Job",
            "workloads-batch",
            Criticality.OPPORTUNISTIC,
            "lab-opportunistic",
            "python",
            "200m",
            "400m",
            "256Mi",
            "384Mi",
        ),
    )
}


def by_controller(namespace: str, name: str) -> CatalogEntry | None:
    for entry in CATALOG.values():
        if entry.namespace == namespace and entry.name == name:
            return entry
    return None


# ---------------------------------------------------------------------------
# WorkloadProfile v1alpha1 (S04 §6) — a declared baseline, never proof of live state.
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProfileMetadata(_Strict):
    id: str
    revision: int = Field(gt=0)


class ProfileRuntime(_Strict):
    language: Literal["dotnet", "python"]
    version: str
    imageDigest: str | None = None


class ProfileKubernetes(_Strict):
    kind: Literal["Deployment", "CronJob", "Job"]
    namespace: str
    workloadName: str
    containerName: str
    baselineReplicas: int | None = Field(default=None, ge=0, le=3)
    suspendedTemplate: bool = False


class ProfileClassification(_Strict):
    criticality: Criticality
    priorityClass: str


class ProfileResources(_Strict):
    requests: dict[Literal["cpu", "memory"], str]
    limits: dict[Literal["cpu", "memory"], str]


class ProfileBehavior(_Strict):
    defaultProfile: str
    allowedProfiles: list[str] = Field(min_length=1)
    maxConcurrentOperations: int = Field(gt=0, le=64)
    maxRunSeconds: int = Field(gt=0, le=3600)


class ProfileOptimization(_Strict):
    allowRequestRecommendation: bool
    allowLimitRecommendation: bool
    allowPlacementRecommendation: bool
    mutationRequiresApproval: Literal[True]


class WorkloadProfileSpec(_Strict):
    runtime: ProfileRuntime
    kubernetes: ProfileKubernetes
    classification: ProfileClassification
    scheduling: dict[str, str]
    resources: ProfileResources
    behavior: ProfileBehavior
    service: dict[str, int | str] | None = None
    reliability: dict[str, str]
    optimization: ProfileOptimization


class WorkloadProfile(_Strict):
    apiVersion: Literal["capacity.lab/v1alpha1"]
    kind: Literal["WorkloadProfile"]
    metadata: ProfileMetadata
    spec: WorkloadProfileSpec

    @model_validator(mode="after")
    def _matches_catalog(self) -> WorkloadProfile:
        entry = CATALOG.get(self.metadata.id)
        if entry is None:
            raise ValueError(f"unknown workload id {self.metadata.id!r}")
        k8s = self.spec.kubernetes
        if (k8s.namespace, k8s.workloadName, k8s.kind) != (entry.namespace, entry.name, entry.kind):
            raise ValueError("profile namespace/name/kind disagree with the accepted S04 catalog")
        cls = self.spec.classification
        if (cls.criticality, cls.priorityClass) != (entry.criticality, entry.priority_class):
            raise ValueError("criticality/PriorityClass disagree with the accepted S04 catalog")
        if self.spec.scheduling.get("requiredNodePool") != "experiment":
            raise ValueError("demo workloads must require the experiment node pool")
        req, lim = self.spec.resources.requests, self.spec.resources.limits
        if set(req) != {"cpu", "memory"} or set(lim) != {"cpu", "memory"}:
            raise ValueError("explicit CPU and memory requests and limits are required")
        if cpu_to_millicores(req["cpu"]) > cpu_to_millicores(lim["cpu"]):
            raise ValueError("cpu request exceeds limit")
        if memory_to_bytes(req["memory"]) > memory_to_bytes(lim["memory"]):
            raise ValueError("memory request exceeds limit")
        if self.spec.behavior.defaultProfile not in self.spec.behavior.allowedProfiles:
            raise ValueError("default profile is not in the allowed set")
        return self


def load_profile(path: Path) -> WorkloadProfile:
    with path.open(encoding="utf-8") as handle:
        return WorkloadProfile.model_validate(yaml.safe_load(handle))
