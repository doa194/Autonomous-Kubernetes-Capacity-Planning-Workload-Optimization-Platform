"""Normalize Kubernetes objects into scheduler-relevant resource facts (S11 §6).

This module answers "how much does the scheduler think this Pod reserves?" It does not
answer "how much does the Pod use?" — usage comes only from Prometheus (S12).

Effective Pod request follows Kubernetes semantics including restartable init containers
(native sidecars, `restartPolicy: Always`) and Pod overhead:

    main     = sum(app containers) + sum(sidecar init containers)
    init_max = max over ordinary init container i of
               (request_i + sum of sidecars declared before i)
    effective = max(main, init_max) + overhead

Pod-level `spec.resources` (Kubernetes 1.32+ feature) replaces the container sum when set.
Anything the normalizer does not understand (extended resources) is reported as an
unsupported dimension instead of being treated as zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from capacity_platform.contracts.units import QuantityError, cpu_to_millicores, memory_to_bytes

NORMALIZER_REVISION = "resource-normalizer-v1"
_CORE_DIMENSIONS = {"cpu", "memory", "ephemeral-storage"}


@dataclass(frozen=True)
class ResourceVector:
    cpu_m: int = 0
    memory_bytes: int = 0

    def __add__(self, other: ResourceVector) -> ResourceVector:
        return ResourceVector(self.cpu_m + other.cpu_m, self.memory_bytes + other.memory_bytes)

    def __sub__(self, other: ResourceVector) -> ResourceVector:
        return ResourceVector(self.cpu_m - other.cpu_m, self.memory_bytes - other.memory_bytes)

    @staticmethod
    def max(a: ResourceVector, b: ResourceVector) -> ResourceVector:
        return ResourceVector(max(a.cpu_m, b.cpu_m), max(a.memory_bytes, b.memory_bytes))


@dataclass(frozen=True)
class ContainerResources:
    name: str
    kind: str  # "app" | "init" | "sidecar"
    request_cpu_m: int | None
    request_memory_bytes: int | None
    limit_cpu_m: int | None
    limit_memory_bytes: int | None

    def request_vector(self) -> ResourceVector:
        # An absent request is treated as 0 for scheduling, matching kube-scheduler.
        # The absence itself is preserved in the fields above for policy decisions.
        return ResourceVector(self.request_cpu_m or 0, self.request_memory_bytes or 0)


@dataclass(frozen=True)
class EffectiveRequest:
    vector: ResourceVector
    source: str  # "containers" | "pod_level"
    unsupported_dimensions: tuple[str, ...] = ()
    quality_flags: tuple[str, ...] = ()


def _quantity(resources: dict[str, Any] | None, section: str, key: str) -> int | None:
    if not resources:
        return None
    section_map = resources.get(section) or {}
    raw = section_map.get(key)
    if raw is None:
        return None
    return cpu_to_millicores(str(raw)) if key == "cpu" else memory_to_bytes(str(raw))


def _unsupported(resources: dict[str, Any] | None) -> set[str]:
    found: set[str] = set()
    for section in ("requests", "limits"):
        for key in (resources or {}).get(section) or {}:
            if key not in _CORE_DIMENSIONS and not key.startswith("hugepages-"):
                found.add(key)
    return found


def container_resources(spec: dict[str, Any], kind: str) -> ContainerResources:
    res = spec.get("resources") or {}
    return ContainerResources(
        name=spec["name"],
        kind=kind,
        request_cpu_m=_quantity(res, "requests", "cpu"),
        request_memory_bytes=_quantity(res, "requests", "memory"),
        limit_cpu_m=_quantity(res, "limits", "cpu"),
        limit_memory_bytes=_quantity(res, "limits", "memory"),
    )


def pod_containers(pod_spec: dict[str, Any]) -> list[ContainerResources]:
    result: list[ContainerResources] = []
    for init in pod_spec.get("initContainers") or []:
        kind = "sidecar" if init.get("restartPolicy") == "Always" else "init"
        result.append(container_resources(init, kind))
    for app in pod_spec.get("containers") or []:
        result.append(container_resources(app, "app"))
    return result


def effective_pod_request(pod_spec: dict[str, Any]) -> EffectiveRequest:
    """Compute the scheduler's effective request for one Pod spec."""
    flags: list[str] = []
    unsupported: set[str] = set()
    try:
        containers = pod_containers(pod_spec)
    except QuantityError:
        return EffectiveRequest(ResourceVector(), "containers", (), ("quantity_invalid",))

    for raw in (pod_spec.get("initContainers") or []) + (pod_spec.get("containers") or []):
        unsupported |= _unsupported(raw.get("resources"))

    overhead_raw = pod_spec.get("overhead") or {}
    overhead = ResourceVector(
        cpu_to_millicores(str(overhead_raw["cpu"])) if "cpu" in overhead_raw else 0,
        memory_to_bytes(str(overhead_raw["memory"])) if "memory" in overhead_raw else 0,
    )

    pod_level = pod_spec.get("resources")
    if pod_level and (pod_level.get("requests") or {}):
        unsupported |= _unsupported(pod_level)
        vec = ResourceVector(
            _quantity(pod_level, "requests", "cpu") or 0,
            _quantity(pod_level, "requests", "memory") or 0,
        )
        return EffectiveRequest(vec + overhead, "pod_level", tuple(sorted(unsupported)), tuple(flags))

    main = ResourceVector()
    sidecars_so_far = ResourceVector()
    init_max = ResourceVector()
    for c in containers:
        if c.kind == "sidecar":
            sidecars_so_far = sidecars_so_far + c.request_vector()
            main = main + c.request_vector()
        elif c.kind == "init":
            init_max = ResourceVector.max(init_max, c.request_vector() + sidecars_so_far)
        else:
            main = main + c.request_vector()
    effective = ResourceVector.max(main, init_max) + overhead
    return EffectiveRequest(effective, "containers", tuple(sorted(unsupported)), tuple(flags))


# ---------------------------------------------------------------------------
# Pod scheduling status and node accounting
# ---------------------------------------------------------------------------

TERMINAL_PHASES = {"Succeeded", "Failed"}


def scheduling_status(pod: dict[str, Any]) -> str:
    spec, status, meta = pod.get("spec", {}), pod.get("status", {}), pod.get("metadata", {})
    phase = status.get("phase")
    if phase in TERMINAL_PHASES:
        return "Completed"
    if meta.get("deletionTimestamp"):
        return "Terminating"  # still holds its reservation until removed
    if spec.get("schedulingGates"):
        return "GateBlocked"
    if spec.get("nodeName"):
        return "Assigned"
    if phase == "Pending":
        return "UnassignedPending"
    return "Unknown"


def holds_node_reservation(pod: dict[str, Any]) -> bool:
    """A Pod reserves node resources while bound and not terminal (terminating included)."""
    return scheduling_status(pod) in {"Assigned", "Terminating"}


@dataclass
class NodeAccounting:
    name: str
    uid: str
    ready: bool
    unschedulable: bool
    labels: dict[str, str]
    taints: list[dict[str, str]]
    allocatable: ResourceVector
    allocatable_pods: int
    assigned: ResourceVector = field(default_factory=ResourceVector)
    assigned_pods: int = 0

    @property
    def unrequested(self) -> ResourceVector:
        # May be negative: we keep accounting deficits visible instead of clamping (S21 §2).
        return self.allocatable - self.assigned

    @property
    def free_pod_slots(self) -> int:
        return self.allocatable_pods - self.assigned_pods


def node_accounting(node: dict[str, Any], pods: list[dict[str, Any]]) -> NodeAccounting:
    meta, spec, status = node["metadata"], node.get("spec", {}), node.get("status", {})
    alloc = status.get("allocatable", {})
    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", []))
    acct = NodeAccounting(
        name=meta["name"],
        uid=meta["uid"],
        ready=ready,
        unschedulable=bool(spec.get("unschedulable")),
        labels=dict(meta.get("labels") or {}),
        taints=[{k: t.get(k, "") for k in ("key", "value", "effect")} for t in (spec.get("taints") or [])],
        allocatable=ResourceVector(
            cpu_to_millicores(str(alloc.get("cpu", "0"))), memory_to_bytes(str(alloc.get("memory", "0")))
        ),
        allocatable_pods=int(alloc.get("pods", "0")),
    )
    for pod in pods:
        if pod.get("spec", {}).get("nodeName") == acct.name and holds_node_reservation(pod):
            acct.assigned = acct.assigned + effective_pod_request(pod["spec"]).vector
            acct.assigned_pods += 1
    return acct
