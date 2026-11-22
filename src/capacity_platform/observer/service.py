"""Observer runtime: collectors, snapshot commits, and a small read/status surface.

Snapshots are committed when the normalized material changes (coalesced, at most every
15 s) and at least every 10 minutes as a freshness heartbeat. Snapshot row, outbox event
and nothing else commit in one transaction; an event is never published for a snapshot
that failed to commit (S11 §4.1 step 7).
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from capacity_platform.contracts.canonical import digest
from capacity_platform.observer.collector import FamilyCollector, FamilyState
from capacity_platform.observer.snapshot import REQUIRED_FAMILIES, build_snapshot
from capacity_platform.shared.db import Connection, emit_event, jsonb, transaction
from capacity_platform.shared.web import create_app

log = logging.getLogger("observer")
MIN_COMMIT_INTERVAL = 15
HEARTBEAT_INTERVAL = 600
HOST_SAFETY_CONFIGMAP = ("capacity-system", "host-safety")


def incarnation_from_anchor(logical_cluster: str, kube_system_uid: str) -> str:
    """A rebuilt kind cluster gets a new kube-system UID, hence a new incarnation (S11 §5.1)."""
    return "inc-" + hashlib.sha256(f"{logical_cluster}|{kube_system_uid}".encode()).hexdigest()[:16]


class Observer:
    def __init__(self, connect: Callable[[], Connection], logical_cluster: str = "capacity-lab") -> None:
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self._core, self._apps = client.CoreV1Api(), client.AppsV1Api()
        self._batch, self._autoscaling = client.BatchV1Api(), client.AutoscalingV2Api()
        self._scheduling = client.SchedulingV1Api()
        self._sanitize = client.ApiClient().sanitize_for_serialization
        self._connect = connect
        self.logical_cluster = logical_cluster
        self.families = {name: FamilyState(name) for name in REQUIRED_FAMILIES}
        self._dirty = threading.Event()
        self.last_commit: datetime | None = None
        self.last_error: str | None = None
        anchor = self._core.read_namespace("kube-system").metadata.uid
        self.incarnation_id = incarnation_from_anchor(logical_cluster, anchor)
        self._anchor = anchor

    def _list_functions(self) -> dict[str, Callable]:
        return {
            "nodes": self._core.list_node,
            "namespaces": self._core.list_namespace,
            "pods": self._core.list_pod_for_all_namespaces,
            "resourcequotas": self._core.list_resource_quota_for_all_namespaces,
            "limitranges": self._core.list_limit_range_for_all_namespaces,
            "deployments": self._apps.list_deployment_for_all_namespaces,
            "replicasets": self._apps.list_replica_set_for_all_namespaces,
            "daemonsets": self._apps.list_daemon_set_for_all_namespaces,
            "jobs": self._batch.list_job_for_all_namespaces,
            "cronjobs": self._batch.list_cron_job_for_all_namespaces,
            "horizontalpodautoscalers": self._autoscaling.list_horizontal_pod_autoscaler_for_all_namespaces,
            "priorityclasses": self._scheduling.list_priority_class,
        }

    def _host_safety(self) -> dict | None:
        try:
            cm = self._core.read_namespaced_config_map(HOST_SAFETY_CONFIGMAP[1], HOST_SAFETY_CONFIGMAP[0])
        except Exception:
            return None  # absent signal => capacity treats host safety as unknown
        data = cm.data or {}
        return {"state": data.get("state"), "updatedAt": data.get("updatedAt"), "source": "host-watchdog"}

    def start(self) -> None:
        for name, fn in self._list_functions().items():
            FamilyCollector(self.families[name], fn, self._dirty.set, self._sanitize).start()
        threading.Thread(target=self._commit_loop, name="snapshot-commit", daemon=True).start()

    def _commit_loop(self) -> None:
        last_digest, last_time = None, 0.0
        while True:
            self._dirty.wait(timeout=HEARTBEAT_INTERVAL)
            time.sleep(MIN_COMMIT_INTERVAL)  # coalesce bursts of watch events
            self._dirty.clear()
            started = datetime.now(UTC)
            snap = build_snapshot(self.incarnation_id, self.families, self._host_safety(), started)
            material_digest = digest(snap["material"])
            if material_digest == last_digest and time.monotonic() - last_time < HEARTBEAT_INTERVAL:
                continue
            try:
                self.commit(snap)
                last_digest, last_time = material_digest, time.monotonic()
            except Exception as exc:  # DB down: no event, retry on next cycle
                self.last_error = type(exc).__name__
                log.warning("snapshot commit failed: %s", self.last_error)

    def commit(self, snap: dict) -> None:
        with self._connect() as conn, transaction(conn):
            conn.execute(
                "INSERT INTO inventory.cluster_incarnation (incarnation_id, logical_cluster, anchor_uid) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (self.incarnation_id, self.logical_cluster, self._anchor),
            )
            cap = snap["capture"]
            conn.execute(
                "INSERT INTO inventory.snapshot (snapshot_id, incarnation_id, capture_started, capture_completed, "
                "coverage, freshness, quality_flags, families, material, normalizer_rev, content_digest) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    snap["snapshotId"],
                    self.incarnation_id,
                    cap["startedAt"],
                    cap["completedAt"],
                    cap["coverage"],
                    cap["freshness"],
                    jsonb(cap["qualityFlags"]),
                    jsonb(snap["families"]),
                    jsonb(snap["material"]),
                    snap["normalizerRevision"],
                    snap["contentDigest"],
                ),
            )
            emit_event(
                conn,
                "inventory",
                "InventorySnapshotCommitted.v1",
                snap["snapshotId"],
                {
                    "snapshotId": snap["snapshotId"],
                    "contentDigest": snap["contentDigest"],
                    "coverage": cap["coverage"],
                    "freshness": cap["freshness"],
                },
                snap["snapshotId"],
            )
        self.last_commit = datetime.now(UTC)
        self.last_error = None

    def readiness(self) -> dict[str, str]:
        synced = all(f.state == "synchronized" for f in self.families.values())
        return {"kubernetes_watch": "up" if synced else "degraded", "database": "down" if self.last_error else "up"}


def build_app(observer: Observer):
    app = create_app("observer", observer.readiness)

    @app.get("/internal/v1/status")
    def status() -> dict:
        return {
            "incarnationId": observer.incarnation_id,
            "lastCommit": observer.last_commit,
            "families": {
                n: {"state": f.state, "objects": len(f.objects), "relists": f.relists, "error": f.error}
                for n, f in observer.families.items()
            },
        }

    return app
