"""Build typed analysis inputs from an S11 snapshot and S12 registered queries.

This is the join point S12 §8 describes: Prometheus series are attributed to a workload
only through the snapshot's exact controller identity (namespace + controller name +
container), and the snapshot's quality axes flow into the evidence unchanged. If a source
is missing, the corresponding evidence field stays `None` and the gates report `unknown`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from capacity_platform.capacity.forecasting.contracts import (
    BucketStatus,
    ForecastRequest,
    ForecastResult,
    HistoryBucket,
)
from capacity_platform.capacity.forecasting.engine import forecast
from capacity_platform.capacity.inputs import (
    AnalysisEvidence,
    ContainerResourceState,
    CpuEvidence,
    ForecastSummary,
    HpaState,
    MemoryEvidence,
    PodConstraints,
    QueueEvidence,
    RolloutStrategy,
    SloEvidence,
    TargetState,
)
from capacity_platform.capacity.placement.fit import NodeState
from capacity_platform.capacity.telemetry.registry import REGISTRY, PrometheusClient, bucketize, render, window
from capacity_platform.contracts.enums import Coverage, Freshness
from capacity_platform.contracts.errors import ErrorCategory, PlatformError
from capacity_platform.contracts.identity import ClusterRef, ControllerRef, TargetRef
from capacity_platform.contracts.ownership import detect_ownership
from capacity_platform.contracts.units import cpu_to_millicores, memory_to_bytes
from capacity_platform.contracts.workloads import CATALOG

HOST_SAFETY_MAX_AGE = timedelta(minutes=2)
EVIDENCE_LOOKBACK = timedelta(hours=6)


def _q(resources: dict, section: str, key: str) -> int | None:
    raw = (resources.get(section) or {}).get(key)
    if raw is None:
        return None
    return cpu_to_millicores(str(raw)) if key == "cpu" else memory_to_bytes(str(raw))


def _strategy(raw: dict | None) -> RolloutStrategy:
    if not raw or raw.get("type") != "RollingUpdate":
        return RolloutStrategy(kind="Recreate" if raw and raw.get("type") == "Recreate" else "unknown")
    ru = raw.get("rollingUpdate") or {}

    def as_int(v, default):
        if v is None:
            return default
        if isinstance(v, str) and v.endswith("%"):
            return -1  # percentage forms are resolved against replicas below
        return int(v)

    return RolloutStrategy(
        kind="RollingUpdate",
        max_surge=as_int(ru.get("maxSurge"), 1),
        max_unavailable=as_int(ru.get("maxUnavailable"), 0),
    )


def target_from_snapshot(snapshot: dict[str, Any], logical_cluster: str, workload_id: str) -> TargetState:
    entry = CATALOG.get(workload_id)
    if entry is None:
        raise PlatformError("target_disallowed", ErrorCategory.PRECONDITION, "unknown workload")
    material = snapshot["material"]
    matches = [
        w
        for w in material.get("workloads", [])
        if (w["namespace"], w["name"], w["kind"]) == (entry.namespace, entry.name, entry.kind)
    ]
    if len(matches) != 1:
        raise PlatformError(
            "target_identity_ambiguous",
            ErrorCategory.PRECONDITION,
            "target controller not uniquely present in the snapshot",
        )
    w = matches[0]
    res = w["containers"].get(entry.container)
    if res is None:
        raise PlatformError("target_identity_ambiguous", ErrorCategory.PRECONDITION, "container not found")
    strategy = _strategy(w.get("strategy"))
    replicas = w.get("replicas") if w.get("replicas") is not None else (0 if entry.kind != "Deployment" else 1)
    if strategy.max_surge < 0 or strategy.max_unavailable < 0:  # e.g. "25%": round as Kubernetes does
        strategy = RolloutStrategy(
            kind="RollingUpdate", max_surge=max(1, -(-replicas * 25 // 100)), max_unavailable=(replicas * 25) // 100
        )
    quota_cpu = quota_mem = None
    for q in material.get("quotas", []):
        if q["namespace"] == entry.namespace and q.get("hard") and q.get("used"):
            hard, used = q["hard"], q["used"]
            if "requests.cpu" in hard:
                quota_cpu = cpu_to_millicores(hard["requests.cpu"]) - cpu_to_millicores(used.get("requests.cpu", "0"))
            if "requests.memory" in hard:
                quota_mem = memory_to_bytes(hard["requests.memory"]) - memory_to_bytes(used.get("requests.memory", "0"))
    return TargetState(
        target=TargetRef(
            cluster=ClusterRef(logicalId=logical_cluster, incarnationId=snapshot["incarnationId"]),
            namespace=entry.namespace,
            logicalWorkloadId=workload_id,
            controller=ControllerRef(
                apiVersion="apps/v1" if entry.kind == "Deployment" else "batch/v1",
                kind=entry.kind,
                namespace=entry.namespace,
                name=entry.name,
                uid=w["uid"],
            ),
            containerName=entry.container,
        ),
        workload_kind=entry.kind,
        replicas=replicas,
        current=ContainerResourceState(
            cpu_request_m=_q(res, "requests", "cpu"),
            memory_request_bytes=_q(res, "requests", "memory"),
            cpu_limit_m=_q(res, "limits", "cpu"),
            memory_limit_bytes=_q(res, "limits", "memory"),
        ),
        constraints=PodConstraints(
            node_selector=w.get("nodeSelector") or {},
            required_node_affinity=w.get("requiredNodeAffinity") or [],
            tolerations=w.get("tolerations") or [],
            unsupported=w.get("unsupportedConstraints") or [],
        ),
        strategy=strategy,
        resource_version=w.get("resourceVersion") or "",
        generation=w.get("generation"),
        quota_remaining_cpu_m=quota_cpu,
        quota_remaining_memory_bytes=quota_mem,
    )


def nodes_from_snapshot(snapshot: dict[str, Any], controller_uid: str) -> tuple[list[NodeState], dict[str, int]]:
    material = snapshot["material"]
    nodes = [
        NodeState(
            name=n["name"],
            uid=n["uid"],
            ready=n["ready"],
            unschedulable=n["unschedulable"],
            labels=n["labels"],
            taints=tuple((t.get("key", ""), t.get("value", ""), t.get("effect", "")) for t in n["taints"]),
            alloc_cpu_m=n["allocatable"]["cpu_m"],
            alloc_memory_bytes=n["allocatable"]["memory_bytes"],
            alloc_pods=n["allocatable"]["pods"],
            assigned_cpu_m=n["assigned"]["cpu_m"],
            assigned_memory_bytes=n["assigned"]["memory_bytes"],
            assigned_pods=n["assigned"]["pods"],
        )
        for n in material.get("nodes", [])
    ]
    per_node: dict[str, int] = {}
    for pod in material.get("pods", []):
        if pod.get("controllerUid") == controller_uid and pod.get("holdsReservation") and pod.get("nodeName"):
            per_node[pod["nodeName"]] = per_node.get(pod["nodeName"], 0) + 1
    return nodes, per_node


def host_safety_state(snapshot: dict[str, Any], now: datetime) -> str:
    signal = (snapshot.get("material") or {}).get("hostSafety") or {}
    state, updated = signal.get("state"), signal.get("updatedAt")
    if not state or not updated:
        return "unknown"
    try:
        age = now - datetime.fromisoformat(updated.replace("Z", "+00:00"))
    except ValueError:
        return "unknown"
    if age > HOST_SAFETY_MAX_AGE:
        return "unknown"  # a stale "ok" is not evidence of safety
    return state if state in ("ok", "pressure", "exclusive") else "unknown"


def _history(points: list[tuple[datetime, float | None]]) -> list[HistoryBucket]:
    return [
        HistoryBucket(startUtc=t, value=v, status=BucketStatus.OBSERVED if v is not None else BucketStatus.MISSING)
        for t, v in points
    ]


def collect_evidence(
    prom: PrometheusClient, snapshot: dict[str, Any], target: TargetState, now: datetime, snapshot_id: str
) -> tuple[AnalysisEvidence, list[ForecastResult]]:
    entry = CATALOG[target.target.logicalWorkloadId]
    start, end = window(now, int(EVIDENCE_LOOKBACK.total_seconds()))
    end = end.replace(second=0) - timedelta(minutes=end.minute % 5)  # align to the 5-minute bucket grid
    start = end - EVIDENCE_LOOKBACK
    sel = {"namespace": entry.namespace, "pod_prefix": entry.name, "container": entry.container, "workload": entry.name}

    def fetch(qid: str, step: int = 15):
        d = REGISTRY[(qid, 1)]
        try:
            return prom.range(d, render(d, **sel), start, end, step)
        except PlatformError:
            return None

    forecasts: list[ForecastResult] = []
    cpu_ev = mem_ev = slo_ev = queue_ev = None
    fc_cpu = fc_mem = None
    cpu = fetch("container.cpu.rate.millicores")
    if cpu is not None and cpu.coverage > 0:
        vals = np.array([v for v in cpu.values if v is not None])
        throttle = fetch("container.cpu.throttle.ratio")
        tvals = [v for v in (throttle.values if throttle else ()) if v is not None]
        cpu_ev = CpuEvidence(
            p95_m=float(np.quantile(vals, 0.95)),
            max_sampled_m=float(vals.max()),
            mean_m=float(vals.mean()),
            coverage=cpu.coverage,
            samples=int(vals.size),
            throttle_ratio_p95=float(np.quantile(tvals, 0.95)) if tvals else None,
            flags=list(cpu.flags),
            observation_ref=f"obs:{cpu.query_id}:{start.isoformat()}",
        )
        if entry.continuous:
            req = ForecastRequest(
                clusterIncarnationRef=snapshot["incarnationId"],
                namespace=entry.namespace,
                logicalWorkloadRef=entry.workload_id,
                resource="cpu_consumption_millicores",
                horizon="1h",
                asOfUtc=end,
                bucketSeconds=300,
                history=_history(bucketize(cpu, 300, "mean")),
                sourceQualityFlags=list(cpu.flags),
            )
            result = forecast(req)
            forecasts.append(result)
            fc_cpu = ForecastSummary(
                forecast_id=result.forecastId,
                status=result.status,
                upper_peak=result.upper_peak(),
                policy_revision=result.eligibility.policyRevision,
                reason_codes=result.eligibility.reasonCodes,
            )
    mem = fetch("container.memory.working_set.bytes")
    if mem is not None and mem.coverage > 0:
        vals = np.array([v for v in mem.values if v is not None])
        restarts = fetch("container.oom.restarts", step=60)
        oom = sum(
            1
            for p in snapshot["material"].get("pods", [])
            if p.get("controllerUid") == target.target.controller.uid and p.get("oomKilled")
        )
        flags = [*list(mem.flags), "peak_not_guaranteed"] if "scrape_gap" in mem.flags else list(mem.flags)
        mem_ev = MemoryEvidence(
            max_sampled_bytes=float(vals.max()),
            p99_bytes=float(np.quantile(vals, 0.99)),
            coverage=mem.coverage,
            max_gap_seconds=mem.max_gap_seconds(),
            oom_events=oom,
            restarts=int(max([v for v in (restarts.values if restarts else ()) if v is not None], default=0)),
            flags=sorted(set(flags)),
            observation_ref=f"obs:{mem.query_id}:{start.isoformat()}",
        )
        if entry.continuous:
            req = ForecastRequest(
                clusterIncarnationRef=snapshot["incarnationId"],
                namespace=entry.namespace,
                logicalWorkloadRef=entry.workload_id,
                resource="memory_working_set_bytes",
                horizon="1h",
                asOfUtc=end,
                bucketSeconds=300,
                history=_history(bucketize(mem, 300, "max")),
                sourceQualityFlags=list(mem.flags),
            )
            result = forecast(req)
            forecasts.append(result)
            fc_mem = ForecastSummary(
                forecast_id=result.forecastId,
                status=result.status,
                upper_peak=result.upper_peak(),
                policy_revision=result.eligibility.policyRevision,
                reason_codes=result.eligibility.reasonCodes,
            )
    if entry.workload_id in ("w01-latency-api", "w02-burst-api"):
        p95, err, rate = fetch("app.http.p95.seconds"), fetch("app.http.error.ratio"), fetch("app.http.request.rate")
        p95v = [v for v in (p95.values if p95 else ()) if v is not None]
        errv = [v for v in (err.values if err else ()) if v is not None]
        ratev = [v for v in (rate.values if rate else ()) if v is not None]
        if p95v and ratev:
            slo_ev = SloEvidence(
                p95_latency_ms=float(np.quantile(p95v, 0.95)) * 1000,
                error_rate=float(np.mean(errv)) if errv else None,
                request_count=int(sum(ratev) * 15),
                observation_ref=f"obs:app.http:{start.isoformat()}",
            )
    if entry.workload_id == "w04-cpu-worker":
        depth = fetch("app.queue.depth")
        dv = [(i, v) for i, v in enumerate(depth.values if depth else ()) if v is not None]
        if len(dv) > 10:
            xs, ys = np.array([i for i, _ in dv]) * 15 / 60, np.array([v for _, v in dv])
            slope = float(np.polyfit(xs, ys, 1)[0])
            queue_ev = QueueEvidence(
                queue_depth_trend_per_min=round(slope, 4),
                completion_within_deadline_rate=None,
                samples=len(dv),
                observation_ref=f"obs:app.queue.depth:{start.isoformat()}",
            )
    hpas = [
        h
        for h in snapshot["material"].get("hpas", [])
        if h["namespace"] == entry.namespace and (h.get("target") or {}).get("name") == entry.name
    ]
    owner_obj = next(
        (w for w in snapshot["material"].get("workloads", []) if w["uid"] == target.target.controller.uid), None
    )
    ownership = None
    if owner_obj is not None:
        ownership = detect_ownership(
            {
                "metadata": {
                    "labels": owner_obj.get("labels"),
                    "annotations": owner_obj.get("annotations"),
                    "managedFields": [{"manager": m} for m in owner_obj.get("managedFieldManagers", [])],
                }
            }
        )
    cap = snapshot["capture"] if "capture" in snapshot else snapshot
    evidence = AnalysisEvidence(
        inventory_snapshot_ref=snapshot_id,
        inventory_coverage=Coverage(cap["coverage"]),
        inventory_freshness=Freshness(cap["freshness"]),
        cpu=cpu_ev,
        memory=mem_ev,
        slo=slo_ev,
        queue=queue_ev,
        forecast_cpu=fc_cpu,
        forecast_memory=fc_mem,
        hpa=HpaState(present=bool(hpas), cpu_utilization_target=None),
        ownership=ownership,
        host_safety=host_safety_state(snapshot, now),  # type: ignore[arg-type]
        scope_authorized=True,
    )
    return evidence, forecasts


def utc(ts: datetime) -> datetime:
    return ts.astimezone(UTC)
