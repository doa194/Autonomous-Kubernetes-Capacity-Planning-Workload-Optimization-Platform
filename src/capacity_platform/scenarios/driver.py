"""In-cluster load driver (runs as an ephemeral, tokenless Job in `capacity-experiments`).

Open-loop dispatch: each planned arrival fires at its planned offset whether or not earlier
requests have finished, so a slow workload cannot hide its own latency (no coordinated
omission). In-flight work is capped; arrivals that cannot start because the cap is full are
counted as `generator_dropped`, never silently skipped. Every counter has an explicit
denominator (S05 §4.2). The summary is printed as one `SUMMARY {...}` line for the runner.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import Counter
from pathlib import Path

import httpx

ROUTES = {
    "compute": ("POST", "/api/v1/compute"),
    "process": ("POST", "/api/v1/process"),
    "profileRun": ("POST", "/api/v1/profile/runs"),
    "task": ("POST", "/api/v1/tasks"),
}


def _body(operation: str, profile: str, seed: int) -> dict:
    if operation in ("compute", "process"):
        return {"profile": profile, "seed": seed, "workUnits": 1}
    if operation == "profileRun":
        return {"mode": profile, "seed": seed, "idempotencyKey": f"run-{seed}"}
    return {"profile": profile, "seed": seed}


async def _run(plan: dict, targets: dict[str, str], token: str, max_inflight: int, timeout: float) -> dict:
    counts: Counter = Counter()
    latencies: list[float] = []
    lag_ms: list[float] = []
    inflight = asyncio.Semaphore(max_inflight)
    start = time.monotonic()
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=timeout, verify=os.environ.get("TARGET_CA_BUNDLE", True)) as client:

        async def fire(base: str, method: str, path: str, body: dict) -> None:
            t0 = time.monotonic()
            try:
                resp = await client.request(method, base + path, json=body, headers=headers)
                counts["completed"] += 1
                counts[
                    "response_success"
                    if resp.status_code < 400
                    else ("rejected" if resp.status_code in (429, 503) else "response_error")
                ] += 1
                latencies.append((time.monotonic() - t0) * 1000)
            except httpx.TimeoutException:
                counts["timeout"] += 1
            except httpx.HTTPError:
                counts["response_error"] += 1
            finally:
                inflight.release()

        tasks = []
        for action in plan["actions"]:
            if action["kind"] != "httpTraffic":
                continue
            base = targets[action["target"]]
            method, path = ROUTES[action["operation"]]
            for arrival in action["arrivals"]:
                counts["planned_arrivals"] += 1
                delay = arrival["offsetMs"] / 1000 - (time.monotonic() - start)
                if delay > 0:
                    await asyncio.sleep(delay)
                lag_ms.append(max(0.0, -delay * 1000))
                counts["dispatch_attempted"] += 1
                if inflight.locked():
                    counts["generator_dropped"] += 1
                    continue
                await inflight.acquire()
                counts["dispatch_accepted"] += 1
                tasks.append(
                    asyncio.create_task(
                        fire(base, method, path, _body(action["operation"], action["profile"], arrival["payloadSeed"]))
                    )
                )
        if tasks:
            await asyncio.gather(*tasks)
    elapsed = time.monotonic() - start
    latencies.sort()
    planned = counts["planned_arrivals"]
    return {
        "planDigest": plan["planDigest"],
        "elapsedSeconds": round(elapsed, 3),
        "counts": dict(counts),
        "rates": {
            "offeredPerSecond": round(planned / elapsed, 4) if elapsed else None,
            "achievedPerSecond": round(counts["completed"] / elapsed, 4) if elapsed else None,
        },
        "dropFraction": {"numerator": counts["generator_dropped"], "denominator": planned},
        "latencyMs": {
            "p50": latencies[len(latencies) // 2] if latencies else None,
            "p95": latencies[int(0.95 * (len(latencies) - 1))] if latencies else None,
            "samples": len(latencies),
        },
        "dispatchLagMsP95": sorted(lag_ms)[int(0.95 * (len(lag_ms) - 1))] if lag_ms else None,
    }


def main() -> None:
    plan = json.loads(Path(os.environ.get("PLAN_FILE", "/plan/plan.json")).read_text())
    token = Path(os.environ.get("CAPABILITY_FILE", "/capability/token")).read_text().strip()
    targets = json.loads(os.environ["TARGETS_JSON"])
    summary = asyncio.run(
        _run(
            plan,
            targets,
            token,
            int(os.environ.get("MAX_INFLIGHT", "16")),
            float(os.environ.get("REQUEST_TIMEOUT", "3")),
        )
    )
    print("SUMMARY " + json.dumps(summary, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
