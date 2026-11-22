"""W05 analytics-processor and W06 opportunistic-batch: bounded, seeded batch executables.

Both run as Kubernetes Jobs created only by an S05 scenario (W05 from its suspended
CronJob template, W06 from its Job template). The run prints exactly one
`RUN_SUMMARY {...}` line before exit so the runner can capture evidence even if the Pod is
gone before Prometheus scrapes it — missing scrapes are never read as zero usage.
Exit code 0 means success; any other code is an explicit failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import resource
import sys
import time

PROFILES = {
    "analytics-small": {"rows": 50_000, "chunk": 5_000},
    "analytics-large": {"rows": 400_000, "chunk": 20_000},
    "batch-short": {"rounds": 200, "buffer_mib": 16},
    "batch-long": {"rounds": 1_500, "buffer_mib": 64},
}
MAX_SECONDS = int(os.environ.get("RUN_DEADLINE_SECONDS", "600"))


def analytics(profile: dict, seed: int) -> dict:
    rng = random.Random(seed)  # deterministic dataset for the pinned seed
    totals: dict[str, float] = {}
    digest = hashlib.sha256()
    for start in range(0, profile["rows"], profile["chunk"]):
        rows = [(f"k{rng.randrange(100)}", rng.random() * 100) for _ in range(min(profile["chunk"],
                                                                               profile["rows"] - start))]
        for key, value in rows:
            totals[key] = totals.get(key, 0.0) + value
        digest.update(json.dumps(rows[:10]).encode())
    digest.update(json.dumps(sorted((k, round(v, 6)) for k, v in totals.items())).encode())
    return {"groups": len(totals), "resultDigest": digest.hexdigest()}


def batch(profile: dict, seed: int) -> dict:
    buf = bytearray(profile["buffer_mib"] * 2**20)
    for off in range(0, len(buf), 4096):
        buf[off] = (seed + off) & 0xFF
    digest = b""
    for _ in range(profile["rounds"]):
        digest = hashlib.sha256(bytes(buf[:65536]) + digest).digest()
    return {"resultDigest": digest.hex()}


def main() -> int:
    workload = os.environ.get("WORKLOAD_ID", "w05-analytics-processor")
    profile_name = os.environ.get("RUN_PROFILE", "analytics-small")
    seed = int(os.environ.get("RUN_SEED", "1"))
    profile = PROFILES.get(profile_name)
    started = time.time()
    if profile is None:
        print("RUN_SUMMARY " + json.dumps({"workload": workload, "status": "rejected", "reason": "unknown_profile"}))
        return 2
    result = analytics(profile, seed) if "rows" in profile else batch(profile, seed)
    elapsed = time.time() - started
    status = "succeeded" if elapsed <= MAX_SECONDS else "deadline_exceeded"
    summary = {"workload": workload, "profile": profile_name, "seed": seed, "status": status,
               "durationSeconds": round(elapsed, 3),
               # Linux reports ru_maxrss in KiB; this is the process high-water mark, application_reported.
               "peakRssBytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
               "sourceFidelity": "application_reported", **result}
    print("RUN_SUMMARY " + json.dumps(summary, sort_keys=True), flush=True)
    return 0 if status == "succeeded" else 3


if __name__ == "__main__":
    sys.exit(main())
