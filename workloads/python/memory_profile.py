"""W03 memory-profile (Standard, workloads-standard): bounded, observable memory behaviour.

Runs physically touch every page they allocate so the container working set really grows
(S04 §4.3). Every run has a mode-specific size and TTL, the process has a hard internal
ceiling below the container limit, and at most two runs exist at once. Logical allocated
bytes are exported for comparison, but they are NOT the cgroup working set; only kubelet
metrics are evidence of real memory use.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

from labkit import Service
from prometheus_client import Counter, Gauge

MIB = 2**20
CEILING = int(os.environ.get("MEMORY_CEILING_MIB", "200")) * MIB  # stays well below the 448Mi limit
MODES = {"steady": (64, 60), "ramp": (128, 90), "spike": (180, 30), "retain": (96, 120)}  # (MiB, TTL s)
MAX_RUNS = 2

ALLOCATED = Gauge("lab_memory_allocated_bytes", "Logical bytes held by active runs", ["workload", "profile"])
FAILURES = Counter("lab_memory_allocation_refusals_total", "Refused allocations", ["workload", "reason"])

runs: dict[str, dict] = {}
by_key: dict[str, str] = {}
lock = threading.Lock()


def held() -> int:
    return sum(r["bytes"] for r in runs.values() if r["state"] in ("allocating", "holding"))


def worker(run_id: str) -> None:
    run = runs[run_id]
    target, ttl = MODES[run["mode"]][0] * MIB, MODES[run["mode"]][1]
    chunks: list[bytearray] = []
    step = 8 * MIB
    steps = max(1, target // step)
    for i in range(steps):
        if run["state"] == "cancelled":
            break
        block = bytearray(step)
        for off in range(0, step, 4096):  # touch every page so it is resident, not just reserved
            block[off] = 1
        chunks.append(block)
        run["bytes"] = len(chunks) * step
        ALLOCATED.labels("memory-profile", run["mode"]).set(held())
        if run["mode"] == "ramp":
            time.sleep(ttl / 3 / steps)
    if run["state"] != "cancelled":
        run["state"] = "holding"
        deadline = time.monotonic() + ttl
        while time.monotonic() < deadline and run["state"] == "holding":
            time.sleep(0.5)
    chunks.clear()
    run["bytes"] = 0
    run["state"] = "released" if run["state"] != "cancelled" else "cancelled"
    run["finishedAt"] = time.time()
    ALLOCATED.labels("memory-profile", run["mode"]).set(held())


def start(body: dict, _: dict) -> tuple[int, dict]:
    mode, key = body.get("mode"), str(body.get("idempotencyKey", ""))[:64]
    if mode not in MODES or not key:
        return 400, {"error": "parameters_out_of_bounds"}
    with lock:
        if key in by_key:  # idempotent submission
            return 200, runs[by_key[key]] | {"runId": by_key[key]}
        active = [r for r in runs.values() if r["state"] in ("allocating", "holding")]
        if len(active) >= MAX_RUNS:
            FAILURES.labels("memory-profile", "concurrency").inc()
            return 429, {"error": "too_many_runs"}
        if held() + MODES[mode][0] * MIB > CEILING:
            FAILURES.labels("memory-profile", "ceiling").inc()
            return 409, {"error": "internal_ceiling_reached"}  # refuse rather than risk OOM
        run_id = uuid.uuid4().hex[:12]
        runs[run_id] = {"mode": mode, "state": "allocating", "bytes": 0, "createdAt": time.time()}
        by_key[key] = run_id
    threading.Thread(target=worker, args=(run_id,), daemon=True).start()
    return 202, {"runId": run_id, "mode": mode, "ttlSeconds": MODES[mode][1]}


def read(_: dict, ctx: dict) -> tuple[int, dict]:
    run = runs.get(ctx["suffix"])
    return (200, run | {"runId": ctx["suffix"]}) if run else (404, {"error": "not_found"})


def cancel(_: dict, ctx: dict) -> tuple[int, dict]:
    run_id = ctx["suffix"].removesuffix("/cancel")
    run = runs.get(run_id)
    if not run:
        return 404, {"error": "not_found"}
    if run["state"] in ("allocating", "holding"):
        run["state"] = "cancelled"
    return 200, {"runId": run_id, "state": run["state"]}


if __name__ == "__main__":
    svc = Service("memory-profile", "w03-memory-profile")
    svc.route("POST", "/api/v1/profile/runs", "profile.start", lambda b, c: (
        cancel(b, c) if c["suffix"].endswith("/cancel") else start(b, c)))
    svc.route("GET", "/api/v1/profile/runs", "profile.read", read)
    svc.serve()
