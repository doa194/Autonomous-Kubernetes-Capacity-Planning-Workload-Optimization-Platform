"""In-memory test doubles for the executor's ports.

`FakeKube` implements real JSON Patch semantics for the operations the executor uses
(test/add/replace/remove), bumps resourceVersion on each write and can inject the failure
modes S25 must survive: lost response after a successful write, conflicts, rejections.
"""

from __future__ import annotations

import copy
from datetime import datetime

from capacity_platform.executor.kube import KubeConflict, KubeNotFound, KubeRejected, KubeUncertain
from capacity_platform.executor.operations import AuthorityBundle, FenceLost, Operation


def _walk(doc, path):
    parts = [p.replace("~1", "/").replace("~0", "~") for p in path.lstrip("/").split("/")]
    parent = doc
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    return parent, parts[-1]


def apply_json_patch(doc: dict, patch: list[dict]) -> dict:
    out = copy.deepcopy(doc)
    for op in patch:
        parent, key = _walk(out, op["path"])
        if isinstance(parent, list):
            key = int(key)
        if op["op"] == "test":
            current = parent[key] if (isinstance(parent, list) or key in parent) else None
            if current != op["value"]:
                raise KubeConflict(f"test failed at {op['path']}")
        elif op["op"] in ("add", "replace"):
            if op["op"] == "replace" and not isinstance(parent, list) and key not in parent:
                raise KubeRejected(f"replace on missing path {op['path']}")
            parent[key] = copy.deepcopy(op["value"])
        elif op["op"] == "remove":
            del parent[key]
        else:
            raise KubeRejected(f"unsupported op {op['op']}")
    return out


def deployment(
    name="cpu-worker",
    namespace="workloads-batch",
    uid="uid-controller-1",
    cpu_req="150m",
    mem_req="192Mi",
    cpu_lim="500m",
    mem_lim="320Mi",
    replicas=1,
    labels=None,
) -> dict:
    return {
        "metadata": {
            "name": name,
            "namespace": namespace,
            "uid": uid,
            "resourceVersion": "100",
            "generation": 3,
            "labels": labels or {},
        },
        "spec": {
            "replicas": replicas,
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": "registry.local/w04@sha256:abc",
                            "resources": {
                                "requests": {"cpu": cpu_req, "memory": mem_req},
                                "limits": {"cpu": cpu_lim, "memory": mem_lim},
                            },
                        }
                    ]
                }
            },
        },
        "status": {
            "observedGeneration": 3,
            "replicas": replicas,
            "updatedReplicas": replicas,
            "availableReplicas": replicas,
            "readyReplicas": replicas,
        },
    }


class FakeKube:
    def __init__(self, obj: dict | None) -> None:
        self.obj = obj
        self.writes: list[list[dict]] = []
        self.dry_runs = 0
        self.lose_response_after_write = False
        self.reject = False
        self.bump_before_write = False  # simulate a concurrent editor between GET and PATCH

    def get_deployment(self, namespace, name):
        if self.obj is None or self.obj["metadata"]["name"] != name:
            raise KubeNotFound(name)
        return copy.deepcopy(self.obj)

    def patch_deployment(self, namespace, name, patch, *, dry_run):
        if self.obj is None:
            raise KubeNotFound(name)
        if self.reject:
            raise KubeRejected("admission denied")
        if dry_run:
            self.dry_runs += 1
            apply_json_patch(self.obj, patch)
            return copy.deepcopy(self.obj)
        if self.bump_before_write:
            self.obj["metadata"]["resourceVersion"] = str(int(self.obj["metadata"]["resourceVersion"]) + 1)
        new = apply_json_patch(self.obj, patch)
        new["metadata"]["resourceVersion"] = str(int(new["metadata"]["resourceVersion"]) + 1)
        new["metadata"]["generation"] += 1
        self.obj = new
        self.writes.append(patch)
        if self.lose_response_after_write:
            raise KubeUncertain("response lost")
        return copy.deepcopy(new)


class FakeGovernance:
    def __init__(self, bundle: AuthorityBundle | None) -> None:
        self.bundle = bundle

    def get_authority(self, intent_id):
        return self.bundle


class MemoryStore:
    """Execution journal with lease fencing. Only the latest claimer may save."""

    def __init__(self) -> None:
        self.ops: dict[str, Operation] = {}
        self.epoch = 0
        self.saves = 0
        self.fail_next_save = False

    def claim(self, intent_id, worker_id, now: datetime):
        self.epoch += 1
        return self.epoch

    def load(self, intent_id):
        op = self.ops.get(intent_id)
        return op.model_copy(deep=True) if op else None

    def save(self, op: Operation):
        if op.fencingEpoch != self.epoch:
            raise FenceLost(op.intentId)
        if self.fail_next_save:
            self.fail_next_save = False
            raise RuntimeError("database unavailable")
        self.saves += 1
        self.ops[op.intentId] = op.model_copy(deep=True)
