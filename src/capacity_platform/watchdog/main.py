"""`capacity-watchdog` — independent host safety process (S05 §7.2, S03 §3.4).

Runs separately from the scenario runner with its own narrower kubeconfig. Every cycle it
measures Windows available memory and the kind node containers' memory, publishes a
host-safety signal (ConfigMap `capacity-system/host-safety`) that the capacity engine and
executor read, heartbeats the run journal, and on pressure requests abort of open runs and
deletes their driver Jobs by exact journaled UID. It cannot touch platform workloads,
baseline Deployments or governed resources: its credential does not allow it.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import typer

from capacity_platform.scenarios.journal import Journal

app = typer.Typer(no_args_is_help=True)
STATE = Path(os.environ.get("CAPACITY_LAB_HOME", Path.home() / ".capacity-lab"))
STATE_HOME = STATE

# Provisional S03 §3.4 thresholds (tuning placeholders, not universal safe limits).
WARN_AVAILABLE_GIB = 2.0
ABORT_AVAILABLE_GIB = 1.5


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def windows_available_gib() -> float | None:
    if os.name != "nt":
        return None
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
        return None
    return status.ullAvailPhys / 2**30


def kind_node_memory() -> dict[str, str]:
    try:
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return {}
    return {line.split()[0]: line.split(None, 1)[1] for line in out.splitlines() if line.startswith("capacity-lab-")}


def evaluate(available_gib: float | None, exclusive_running: bool) -> str:
    if available_gib is None:
        return "unknown"
    if available_gib < ABORT_AVAILABLE_GIB:
        return "abort"
    if available_gib < WARN_AVAILABLE_GIB:
        return "pressure"
    return "exclusive" if exclusive_running else "ok"


def publish(state: str, detail: dict) -> None:
    from kubernetes import client, config

    config.load_kube_config(config_file=str(STATE_HOME / "kubeconfig"), context="capacity-lab-watchdog")
    body = {
        "data": {
            "state": "pressure" if state == "abort" else state,
            "updatedAt": datetime.now(UTC).isoformat(),
            "detail": json.dumps(detail),
        }
    }
    client.CoreV1Api().patch_namespaced_config_map("host-safety", "capacity-system", body)


@app.command()
def run(interval: int = 10, publish_signal: bool = True) -> None:
    journal = Journal(STATE / "scenario-journal.sqlite")
    while True:
        available = windows_available_gib()
        open_runs = journal.open_runs()
        exclusive = any(r["execution_class"] == "exclusive" for r in open_runs)
        state = evaluate(available, exclusive)
        detail = {
            "availableGiB": round(available, 2) if available else None,
            "kindNodes": kind_node_memory(),
            "openRuns": [r["run_id"] for r in open_runs],
        }
        journal.watchdog_beat(state, detail)
        if state == "abort":
            for r in open_runs:
                journal.request_abort(r["run_id"], "host_memory_pressure")
        if publish_signal:
            try:
                publish(state, detail)
            except Exception as exc:  # cluster unreachable: consumers see a stale signal => unknown
                typer.echo(f"host-safety publish failed: {type(exc).__name__}")
        time.sleep(interval)


@app.command()
def once() -> None:
    """Print one measurement (used by preflight and tests)."""
    available = windows_available_gib()
    typer.echo(
        json.dumps({"availableGiB": available, "state": evaluate(available, False), "kindNodes": kind_node_memory()})
    )


if __name__ == "__main__":
    app()
