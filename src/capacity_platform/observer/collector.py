"""S11 hybrid LIST + WATCH + periodic reconciliation collector.

Per resource family: an initial paginated LIST records the collection resourceVersion,
then a WATCH resumes from it. `410 Gone` (expired resourceVersion) or any continuity doubt
invalidates the family and forces a fresh LIST. A full re-LIST also runs on a timer to
catch missed deletes. resourceVersion is kept as an opaque string and never compared
numerically. Objects are keyed by UID, so a recreated object with the same name is a new
record, not an update.

The collector runs inside the `observer` Deployment with a read-only ServiceAccount.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("observer.collector")

RELIST_INTERVAL_SECONDS = 300  # provisional S11 §7.3 full reconciliation cadence
WATCH_TIMEOUT_SECONDS = 240


@dataclass
class FamilyState:
    name: str
    state: str = "initializing"  # initializing|listing|synchronized|relisting|degraded
    resource_version: str | None = None  # opaque
    last_reconciled: datetime | None = None
    error: str | None = None
    objects: dict[str, dict] = field(default_factory=dict)  # uid -> sanitized object
    relists: int = 0


def sanitize(obj: dict) -> dict:
    """Keep only scheduler/identity-relevant fields. Env, args, volumes, annotations with
    credentials and Secret data never enter the inventory (S08 §6 data minimization)."""
    meta = obj.get("metadata", {}) or {}
    kept_annotations = {
        k: v
        for k, v in (meta.get("annotations") or {}).items()
        if k.startswith(
            (
                "capacity.lab/",
                "kustomize.toolkit.fluxcd.io/",
                "helm.toolkit.fluxcd.io/",
                "argocd.argoproj.io/",
                "deployment.kubernetes.io/revision",
            )
        )
    }
    out: dict[str, Any] = {
        "metadata": {
            "name": meta.get("name"),
            "namespace": meta.get("namespace"),
            "uid": meta.get("uid"),
            "resourceVersion": meta.get("resourceVersion"),
            "generation": meta.get("generation"),
            "labels": meta.get("labels") or {},
            "annotations": kept_annotations,
            "ownerReferences": [
                {
                    "kind": o.get("kind"),
                    "name": o.get("name"),
                    "uid": o.get("uid"),
                    "controller": o.get("controller", False),
                }
                for o in meta.get("ownerReferences") or []
            ],
            "deletionTimestamp": meta.get("deletionTimestamp"),
            "managedFieldManagers": sorted(
                {m.get("manager") for m in meta.get("managedFields") or [] if m.get("manager")}
            ),
        }
    }
    spec = obj.get("spec") or {}
    if "template" in spec or "jobTemplate" in spec:
        out["spec"] = _controller_spec(spec)
    elif "containers" in spec:
        out["spec"] = _pod_spec(spec)
    else:
        out["spec"] = {
            k: spec[k]
            for k in (
                "taints",
                "unschedulable",
                "hard",
                "limits",
                "scaleTargetRef",
                "minReplicas",
                "maxReplicas",
                "metrics",
                "selector",
            )
            if k in spec
        }
    for key in ("value", "preemptionPolicy"):  # PriorityClass top-level fields
        if key in obj:
            out[key] = obj[key]
    status = obj.get("status") or {}
    out["status"] = {
        k: status[k]
        for k in (
            "phase",
            "conditions",
            "allocatable",
            "capacity",
            "observedGeneration",
            "replicas",
            "updatedReplicas",
            "readyReplicas",
            "availableReplicas",
            "unavailableReplicas",
            "active",
            "succeeded",
            "failed",
            "startTime",
            "completionTime",
            "hard",
            "used",
            "currentReplicas",
            "desiredReplicas",
            "containerStatuses",
        )
        if k in status
    }
    if "containerStatuses" in out["status"]:
        out["status"]["containerStatuses"] = [
            {
                "name": c.get("name"),
                "restartCount": c.get("restartCount"),
                "lastTerminationReason": ((c.get("lastState") or {}).get("terminated") or {}).get("reason"),
            }
            for c in out["status"]["containerStatuses"]
        ]
    return out


def _containers(items) -> list[dict]:
    return [
        {
            "name": c.get("name"),
            "resources": c.get("resources") or {},
            **({"restartPolicy": c["restartPolicy"]} if c.get("restartPolicy") else {}),
        }
        for c in items or []
    ]


def _pod_spec(spec: dict) -> dict:
    return {
        "nodeName": spec.get("nodeName"),
        "priorityClassName": spec.get("priorityClassName"),
        "priority": spec.get("priority"),
        "schedulingGates": spec.get("schedulingGates"),
        "nodeSelector": spec.get("nodeSelector") or {},
        "affinity": spec.get("affinity") or {},
        "tolerations": spec.get("tolerations") or [],
        "topologySpreadConstraints": spec.get("topologySpreadConstraints"),
        "overhead": spec.get("overhead"),
        "resources": spec.get("resources"),
        "containers": _containers(spec.get("containers")),
        "initContainers": _containers(spec.get("initContainers")),
    }


def _controller_spec(spec: dict) -> dict:
    out = {k: spec[k] for k in ("replicas", "strategy", "suspend", "schedule", "selector") if k in spec}
    template = spec.get("template") or (spec.get("jobTemplate") or {}).get("spec", {}).get("template") or {}
    out["template"] = {"spec": _pod_spec(template.get("spec") or {})}
    return out


ListFn = Callable[..., Any]


class FamilyCollector(threading.Thread):
    """One LIST/WATCH loop for one resource family."""

    def __init__(self, family: FamilyState, list_fn: ListFn, on_change: Callable[[], None], to_dict: Callable) -> None:
        super().__init__(name=f"collector-{family.name}", daemon=True)
        self.family, self._list, self._on_change, self._to_dict = family, list_fn, on_change, to_dict
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _relist(self) -> None:
        self.family.state = "listing" if self.family.state == "initializing" else "relisting"
        objects: dict[str, dict] = {}
        cont = None
        while True:
            page = self._list(limit=500, _continue=cont) if cont else self._list(limit=500)
            for item in page.items:
                obj = sanitize(self._to_dict(item))
                objects[obj["metadata"]["uid"]] = obj
            cont = page.metadata._continue
            if not cont:
                break
        # Replace the cache atomically: deletes missed during a watch gap disappear here.
        self.family.objects = objects
        self.family.resource_version = page.metadata.resource_version
        self.family.state = "synchronized"
        self.family.last_reconciled = datetime.now(UTC)
        self.family.error = None
        self.family.relists += 1
        self._on_change()

    def run(self) -> None:
        from kubernetes import watch
        from kubernetes.client.exceptions import ApiException

        backoff = 1.0
        while not self._stop.is_set():
            try:
                self._relist()
                backoff = 1.0
                deadline = time.monotonic() + RELIST_INTERVAL_SECONDS
                w = watch.Watch()
                for event in w.stream(
                    self._list,
                    resource_version=self.family.resource_version,
                    timeout_seconds=WATCH_TIMEOUT_SECONDS,
                    allow_watch_bookmarks=True,
                ):
                    if self._stop.is_set() or time.monotonic() > deadline:
                        w.stop()
                        break
                    kind = event["type"]
                    if kind == "ERROR":
                        raise ApiException(status=event["raw_object"].get("code", 500))
                    raw = self._to_dict(event["object"])
                    rv = (raw.get("metadata") or {}).get("resourceVersion")
                    if kind == "BOOKMARK":
                        self.family.resource_version = rv
                        continue
                    obj = sanitize(raw)
                    uid = obj["metadata"]["uid"]
                    if kind == "DELETED":
                        self.family.objects.pop(uid, None)
                    else:
                        self.family.objects[uid] = obj
                    self.family.resource_version = rv
                    self._on_change()
            except ApiException as exc:
                if exc.status == 410:
                    log.info("watch expired for %s; relisting", self.family.name)
                    continue  # immediate relist, continuity is gone
                self.family.state = "degraded"
                self.family.error = f"api_{exc.status}"
            except Exception as exc:  # network errors: back off with jitter, never busy-loop
                self.family.state = "degraded"
                self.family.error = type(exc).__name__
            self._stop.wait(min(60.0, backoff) * (0.5 + random.random()))
            backoff *= 2
