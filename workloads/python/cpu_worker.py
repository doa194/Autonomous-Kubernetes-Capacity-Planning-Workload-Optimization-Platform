"""W04 cpu-worker (Flexible, workloads-batch): bounded in-process task queue.

Tasks do deterministic hashing over a seeded buffer; `hashlib` releases the GIL for large
buffers, so the two worker threads use real CPU in parallel. The queue is bounded: a full
queue rejects with 429 instead of growing. Queued work lives in memory and is lost on
restart — a declared demo limitation, reported honestly rather than as completed work.
"""

from __future__ import annotations

import hashlib
import queue
import threading
import time
import uuid

from labkit import Service
from prometheus_client import Counter, Gauge, Histogram

PROFILES = {"light": 20, "normal": 80, "heavy": 240}  # hash rounds over a 1 MiB buffer
DEADLINE_SECONDS = 30
WORKERS = 2

QUEUE: queue.Queue[str] = queue.Queue(maxsize=50)
DEPTH = Gauge("lab_queue_depth", "Queued tasks", ["workload"])
COMPLETED = Counter("lab_tasks_completed_total", "Finished tasks", ["workload", "outcome"])
TASK_SECONDS = Histogram("lab_task_duration_seconds", "Task processing time", ["workload", "operation"])
QUEUE_DELAY = Histogram("lab_task_queue_delay_seconds", "Time waiting in queue", ["workload"])
INFLIGHT = Gauge("lab_tasks_inflight", "Tasks being processed", ["workload"])

tasks: dict[str, dict] = {}


def worker() -> None:
    while True:
        task_id = QUEUE.get()
        DEPTH.labels("cpu-worker").set(QUEUE.qsize())
        task = tasks[task_id]
        waited = time.time() - task["acceptedAt"]
        QUEUE_DELAY.labels("cpu-worker").observe(waited)
        if waited > DEADLINE_SECONDS:
            task["state"] = "deadline_missed"
            COMPLETED.labels("cpu-worker", "deadline_missed").inc()
            continue
        INFLIGHT.labels("cpu-worker").inc()
        started = time.monotonic()
        buf = hashlib.sha256(str(task["seed"]).encode()).digest() * (2**20 // 32)
        digest = b""
        for _ in range(PROFILES[task["profile"]]):
            digest = hashlib.sha256(buf + digest).digest()
        TASK_SECONDS.labels("cpu-worker", task["profile"]).observe(time.monotonic() - started)
        INFLIGHT.labels("cpu-worker").dec()
        task.update(state="completed", checksum=digest.hex(), finishedAt=time.time())
        COMPLETED.labels("cpu-worker", "completed").inc()


def submit(body: dict, _: dict) -> tuple[int, dict]:
    profile = body.get("profile")
    if profile not in PROFILES or not isinstance(body.get("seed"), int):
        return 400, {"error": "parameters_out_of_bounds"}
    task_id = uuid.uuid4().hex[:12]
    tasks[task_id] = {"profile": profile, "seed": body["seed"], "state": "queued", "acceptedAt": time.time()}
    try:
        QUEUE.put_nowait(task_id)
    except queue.Full:
        del tasks[task_id]
        COMPLETED.labels("cpu-worker", "rejected").inc()
        return 429, {"error": "queue_full"}  # backpressure, explicit and counted
    DEPTH.labels("cpu-worker").set(QUEUE.qsize())
    return 202, {"taskId": task_id, "state": "queued"}


def read(_: dict, ctx: dict) -> tuple[int, dict]:
    task = tasks.get(ctx["suffix"])
    return (200, task | {"taskId": ctx["suffix"]}) if task else (404, {"error": "not_found"})


if __name__ == "__main__":
    for _ in range(WORKERS):
        threading.Thread(target=worker, daemon=True).start()
    svc = Service("cpu-worker", "w04-cpu-worker")
    svc.route("POST", "/api/v1/tasks", "task.submit", submit)
    svc.route("GET", "/api/v1/tasks", "task.read", read)
    svc.serve()
