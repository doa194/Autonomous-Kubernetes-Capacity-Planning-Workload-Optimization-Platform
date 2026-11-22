"""Assemble immutable, quality-annotated inventory snapshots from the collector caches.

A snapshot is a bounded observation, not an atomic etcd image (S11 §7.1): it records the
capture window, per-family sync state and opaque resourceVersions, and two separate
quality axes (coverage and freshness). Missing families make coverage `partial`; nothing
missing is ever assumed to consume zero.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from capacity_platform.contracts.canonical import digest
from capacity_platform.observer.collector import FamilyState
from capacity_platform.observer.normalize import (
    NORMALIZER_REVISION,
    effective_pod_request,
    holds_node_reservation,
    node_accounting,
    scheduling_status,
)

REQUIRED_FAMILIES = (
    "nodes",
    "namespaces",
    "pods",
    "deployments",
    "replicasets",
    "jobs",
    "cronjobs",
    "horizontalpodautoscalers",
    "priorityclasses",
    "resourcequotas",
    "limitranges",
    "daemonsets",
)
FRESH_WINDOW = timedelta(seconds=30)


def _unsupported_constraints(pod_spec: dict) -> list[str]:
    found = []
    affinity = pod_spec.get("affinity") or {}
    for kind in ("podAffinity", "podAntiAffinity"):
        if (affinity.get(kind) or {}).get("requiredDuringSchedulingIgnoredDuringExecution"):
            found.append(f"{kind}_required")
    for tsc in pod_spec.get("topologySpreadConstraints") or []:
        if tsc.get("whenUnsatisfiable", "DoNotSchedule") == "DoNotSchedule":
            found.append("topology_spread_hard")
            break
    return found


def _required_node_affinity(pod_spec: dict) -> list[list[dict]]:
    terms = (
        ((pod_spec.get("affinity") or {}).get("nodeAffinity") or {}).get(
            "requiredDuringSchedulingIgnoredDuringExecution"
        )
        or {}
    ).get("nodeSelectorTerms") or []
    return [list(t.get("matchExpressions") or []) for t in terms]


def build_material(families: dict[str, FamilyState], host_safety: dict | None) -> dict[str, Any]:
    pods = list(families["pods"].objects.values())
    rs_owner = {
        uid: next((o for o in rs["metadata"]["ownerReferences"] if o.get("controller")), None)
        for uid, rs in families["replicasets"].objects.items()
    }
    nodes = []
    for node in sorted(families["nodes"].objects.values(), key=lambda n: n["metadata"]["name"]):
        acct = node_accounting(node, pods)
        nodes.append(
            {
                "name": acct.name,
                "uid": acct.uid,
                "ready": acct.ready,
                "unschedulable": acct.unschedulable,
                "labels": acct.labels,
                "taints": acct.taints,
                "allocatable": {
                    "cpu_m": acct.allocatable.cpu_m,
                    "memory_bytes": acct.allocatable.memory_bytes,
                    "pods": acct.allocatable_pods,
                },
                "assigned": {
                    "cpu_m": acct.assigned.cpu_m,
                    "memory_bytes": acct.assigned.memory_bytes,
                    "pods": acct.assigned_pods,
                },
            }
        )
    pod_rows = []
    for pod in sorted(pods, key=lambda p: (p["metadata"]["namespace"], p["metadata"]["name"])):
        owner = next((o for o in pod["metadata"]["ownerReferences"] if o.get("controller")), None)
        controller = owner
        if owner and owner.get("kind") == "ReplicaSet":
            controller = rs_owner.get(owner["uid"]) or owner
        eff = effective_pod_request(pod["spec"])
        statuses = pod["status"].get("containerStatuses") or []
        pod_rows.append(
            {
                "namespace": pod["metadata"]["namespace"],
                "name": pod["metadata"]["name"],
                "uid": pod["metadata"]["uid"],
                "nodeName": pod["spec"].get("nodeName"),
                "schedulingStatus": scheduling_status(pod),
                "holdsReservation": holds_node_reservation(pod),
                "phase": pod["status"].get("phase"),
                "controllerKind": controller.get("kind") if controller else None,
                "controllerUid": controller.get("uid") if controller else None,
                "ownerResolved": controller is not None or not pod["metadata"]["ownerReferences"],
                "effectiveRequest": {"cpu_m": eff.vector.cpu_m, "memory_bytes": eff.vector.memory_bytes},
                "unsupportedDimensions": list(eff.unsupported_dimensions),
                "restarts": sum(s.get("restartCount") or 0 for s in statuses),
                "oomKilled": any(s.get("lastTerminationReason") == "OOMKilled" for s in statuses),
            }
        )
    workloads = []
    for family in ("deployments", "cronjobs", "jobs"):
        for obj in families[family].objects.values():
            spec = obj["spec"]
            pod_spec = spec.get("template", {}).get("spec", {})
            meta = obj["metadata"]
            workloads.append(
                {
                    "namespace": meta["namespace"],
                    "name": meta["name"],
                    "uid": meta["uid"],
                    "kind": {"deployments": "Deployment", "cronjobs": "CronJob", "jobs": "Job"}[family],
                    "generation": meta.get("generation"),
                    "resourceVersion": meta.get("resourceVersion"),
                    "replicas": spec.get("replicas"),
                    "strategy": spec.get("strategy"),
                    "suspend": spec.get("suspend"),
                    "containers": {c["name"]: c["resources"] for c in pod_spec.get("containers", [])},
                    "nodeSelector": pod_spec.get("nodeSelector") or {},
                    "tolerations": pod_spec.get("tolerations") or [],
                    "requiredNodeAffinity": _required_node_affinity(pod_spec),
                    "unsupportedConstraints": _unsupported_constraints(pod_spec),
                    "overhead": pod_spec.get("overhead"),
                    "labels": meta.get("labels") or {},
                    "annotations": meta.get("annotations") or {},
                    "managedFieldManagers": meta.get("managedFieldManagers") or [],
                    "status": obj["status"],
                }
            )
    workloads.sort(key=lambda w: (w["namespace"], w["kind"], w["name"]))
    return {
        "nodes": nodes,
        "pods": pod_rows,
        "workloads": workloads,
        "hpas": sorted(
            (
                {
                    "namespace": h["metadata"]["namespace"],
                    "name": h["metadata"]["name"],
                    "target": h["spec"].get("scaleTargetRef"),
                    "metrics": h["spec"].get("metrics"),
                    "status": h["status"],
                }
                for h in families["horizontalpodautoscalers"].objects.values()
            ),
            key=lambda h: (h["namespace"], h["name"]),
        ),
        "quotas": sorted(
            (
                {
                    "namespace": q["metadata"]["namespace"],
                    "name": q["metadata"]["name"],
                    "hard": q["status"].get("hard"),
                    "used": q["status"].get("used"),
                }
                for q in families["resourcequotas"].objects.values()
            ),
            key=lambda q: (q["namespace"], q["name"]),
        ),
        "priorityClasses": sorted(
            (
                {"name": p["metadata"]["name"], "value": p.get("value"), "preemptionPolicy": p.get("preemptionPolicy")}
                for p in families["priorityclasses"].objects.values()
            ),
            key=lambda p: p["name"],
        ),
        "hostSafety": host_safety,
    }


def build_snapshot(
    incarnation_id: str,
    families: dict[str, FamilyState],
    host_safety: dict | None,
    capture_started: datetime,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    flags: list[str] = []
    missing = [f for f in REQUIRED_FAMILIES if families.get(f) is None or families[f].state != "synchronized"]
    coverage = "complete" if not missing else ("unavailable" if len(missing) == len(REQUIRED_FAMILIES) else "partial")
    if missing:
        flags.append(
            "rbac_scope_incomplete"
            if any(families.get(f) and families[f].error == "api_403" for f in missing)
            else "watch_gap_detected"
        )
    oldest = min(
        (families[f].last_reconciled for f in REQUIRED_FAMILIES if families.get(f) and families[f].last_reconciled),
        default=None,
    )
    freshness = "fresh" if not missing and oldest and now - oldest <= timedelta(seconds=RELIST_GRACE) else "stale"
    material = build_material(families, host_safety) if "pods" not in missing else {}
    if material and any(not p["ownerResolved"] for p in material["pods"]):
        flags.append("owner_unresolved")
    if material and any(p["unsupportedDimensions"] for p in material["pods"]):
        flags.append("unsupported_resource_dimension")
    snapshot = {
        "schemaVersion": "inventory-snapshot/v1alpha1",
        "snapshotId": str(uuid.uuid4()),
        "incarnationId": incarnation_id,
        "capture": {
            "startedAt": capture_started,
            "completedAt": now,
            "coverage": coverage,
            "freshness": freshness,
            "qualityFlags": sorted(set(flags)),
        },
        "families": {
            name: {
                "state": st.state,
                "resourceVersionOpaque": st.resource_version,
                "lastReconciledAt": st.last_reconciled,
                "relists": st.relists,
            }
            for name, st in sorted(families.items())
        },
        "material": material,
        "normalizerRevision": NORMALIZER_REVISION,
    }
    snapshot["contentDigest"] = digest({k: v for k, v in snapshot.items() if k != "snapshotId"})
    return snapshot


# A family re-LISTs every RELIST_INTERVAL; a healthy watch keeps it current in between.
RELIST_GRACE = 330
