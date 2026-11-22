"""Worker readiness must recover after a transient database error even when no work is queued.

Readiness for the capacity, AI and executor services is derived from the worker loop's last error.
A loop that only cleared the error after processing a job stayed "not ready" forever on an idle
queue after one blip (seen live after a Docker restart).
"""

import contextlib
import threading
from pathlib import Path

import psycopg

from capacity_platform.capacity import service as capacity_service


class _Conn:
    def commit(self):
        pass


def test_capacity_worker_readiness_recovers_on_an_idle_queue(monkeypatch):
    stop = threading.Event()
    calls = {"n": 0}

    def claim(conn, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.OperationalError("connection refused")  # transient outage
        stop.set()  # second round-trip succeeds with no job queued; end the loop
        return None

    monkeypatch.setattr(capacity_service.jobs, "claim", claim)
    worker = capacity_service.CapacityWorker(
        lambda: contextlib.nullcontext(_Conn()), prometheus=None, policy_path=Path("unused")
    )
    monkeypatch.setattr(stop, "wait", lambda timeout=None: stop.is_set())  # no real sleeping
    worker.run_forever("w-test", stop)
    assert calls["n"] == 2
    assert worker.last_error is None
