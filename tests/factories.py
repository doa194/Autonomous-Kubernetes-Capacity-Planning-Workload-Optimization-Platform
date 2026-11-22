"""Builders for synthetic, clearly-fictional test inputs.

Everything here is a `reference_fixture` (Domain E §9): it proves implementation
invariants, never simulator accuracy or real cluster behaviour.
"""

from __future__ import annotations

from pathlib import Path

from capacity_platform.capacity.inputs import (
    AnalysisEvidence,
    ContainerResourceState,
    CpuEvidence,
    ForecastSummary,
    HpaState,
    MemoryEvidence,
    PodConstraints,
    QueueEvidence,
    SloEvidence,
    TargetState,
)
from capacity_platform.capacity.placement.fit import NodeState
from capacity_platform.capacity.policy.model import EffectivePolicy, load_policy, resolve_effective_policy
from capacity_platform.contracts.enums import Coverage, Freshness
from capacity_platform.contracts.identity import ClusterRef, ControllerRef, TargetRef
from capacity_platform.contracts.units import MIB
from capacity_platform.contracts.workloads import CATALOG

ROOT = Path(__file__).resolve().parents[1]
POLICY_DOC = load_policy(ROOT / "configs" / "policies" / "optimization-policy.yaml")
CLUSTER = ClusterRef(logicalId="capacity-lab", incarnationId="inc-test-0001")

EXPERIMENT_SELECTOR = {"capacity.lab/pool": "experiment"}


def policy(workload_id: str) -> EffectivePolicy:
    entry = CATALOG[workload_id]
    return resolve_effective_policy(
        POLICY_DOC,
        cluster_id=CLUSTER.logicalId,
        namespace=entry.namespace,
        workload_id=workload_id,
        criticality=entry.criticality,
    )


def target_ref(workload_id: str, uid: str = "uid-controller-1") -> TargetRef:
    entry = CATALOG[workload_id]
    api = "apps/v1" if entry.kind == "Deployment" else "batch/v1"
    return TargetRef(
        cluster=CLUSTER,
        namespace=entry.namespace,
        logicalWorkloadId=workload_id,
        controller=ControllerRef(apiVersion=api, kind=entry.kind, namespace=entry.namespace, name=entry.name, uid=uid),
        containerName=entry.container,
    )


def target_state(
    workload_id: str,
    *,
    cpu_req=None,
    mem_req=None,
    cpu_lim=None,
    mem_lim=None,
    replicas=1,
    uid="uid-controller-1",
    unsupported=(),
    quota_cpu=10_000,
    quota_mem=10 * 1024 * MIB,
    **kw,
) -> TargetState:
    entry = CATALOG[workload_id]
    return TargetState(
        target=target_ref(workload_id, uid),
        workload_kind=entry.kind,
        replicas=replicas,
        current=ContainerResourceState(
            cpu_request_m=cpu_req if cpu_req is not None else entry.cpu_request_m,
            memory_request_bytes=mem_req if mem_req is not None else entry.memory_request_bytes,
            cpu_limit_m=cpu_lim if cpu_lim is not None else entry.cpu_limit_m,
            memory_limit_bytes=mem_lim if mem_lim is not None else entry.memory_limit_bytes,
        ),
        constraints=PodConstraints(node_selector=dict(EXPERIMENT_SELECTOR), unsupported=list(unsupported)),
        resource_version="rv-opaque-1",
        generation=1,
        quota_remaining_cpu_m=quota_cpu,
        quota_remaining_memory_bytes=quota_mem,
        **kw,
    )


def good_evidence(
    workload_id: str,
    *,
    cpu_p95=60.0,
    mem_max=150 * MIB,
    slo_p95=80.0,
    errors=0.001,
    forecast_cpu_upper=70.0,
    forecast_mem_upper=None,
    hpa=False,
    **overrides,
) -> AnalysisEvidence:
    data = dict(
        inventory_snapshot_ref="snap-1",
        inventory_coverage=Coverage.COMPLETE,
        inventory_freshness=Freshness.FRESH,
        cpu=CpuEvidence(
            p95_m=cpu_p95,
            max_sampled_m=cpu_p95 * 1.4,
            mean_m=cpu_p95 * 0.7,
            coverage=0.99,
            samples=500,
            throttle_ratio_p95=0.0,
            observation_ref="obs-cpu",
        ),
        memory=MemoryEvidence(
            max_sampled_bytes=mem_max,
            p99_bytes=mem_max * 0.95,
            coverage=0.99,
            max_gap_seconds=15,
            oom_events=0,
            restarts=0,
            observation_ref="obs-mem",
        ),
        slo=SloEvidence(p95_latency_ms=slo_p95, error_rate=errors, request_count=5000, observation_ref="obs-slo"),
        queue=QueueEvidence(
            queue_depth_trend_per_min=0.0,
            completion_within_deadline_rate=0.999,
            samples=400,
            observation_ref="obs-queue",
        ),
        forecast_cpu=ForecastSummary(
            forecast_id="fc-cpu",
            status="available",
            upper_peak=forecast_cpu_upper,
            policy_revision="forecast-cpu-1h-v1",
        ),
        forecast_memory=ForecastSummary(
            forecast_id="fc-mem",
            status="available",
            upper_peak=forecast_mem_upper if forecast_mem_upper else mem_max * 0.9,
            policy_revision="forecast-memory-1h-v1",
        ),
        hpa=HpaState(present=hpa, cpu_utilization_target=70 if hpa else None),
        ownership="direct_managed",
        host_safety="ok",
        scope_authorized=True,
    )
    data.update(overrides)
    return AnalysisEvidence(**data)


def node(
    name,
    *,
    cpu_free_m,
    mem_free_bytes,
    pool="experiment",
    taints=(),
    ready=True,
    alloc_cpu=4000,
    alloc_mem=4096 * MIB,
    pods_free=100,
) -> NodeState:
    return NodeState(
        name=name,
        uid=f"uid-{name}",
        ready=ready,
        unschedulable=False,
        labels={"capacity.lab/pool": pool, "kubernetes.io/hostname": name},
        taints=tuple(taints),
        alloc_cpu_m=alloc_cpu,
        alloc_memory_bytes=alloc_mem,
        alloc_pods=110,
        assigned_cpu_m=alloc_cpu - cpu_free_m,
        assigned_memory_bytes=alloc_mem - mem_free_bytes,
        assigned_pods=110 - pods_free,
    )


def lab_nodes() -> list[NodeState]:
    """One tainted platform worker with lots of free RAM plus two experiment workers."""
    return [
        node(
            "capacity-lab-worker",
            cpu_free_m=3000,
            mem_free_bytes=3000 * MIB,
            pool="platform",
            taints=(("capacity.lab/dedicated", "platform", "NoSchedule"),),
        ),
        node("capacity-lab-worker2", cpu_free_m=1500, mem_free_bytes=1500 * MIB),
        node("capacity-lab-worker3", cpu_free_m=1200, mem_free_bytes=1200 * MIB),
    ]
