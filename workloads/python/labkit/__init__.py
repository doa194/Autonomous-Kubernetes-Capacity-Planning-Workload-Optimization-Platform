"""Shared helpers for the Python demonstration workloads (W03-W06).

Stdlib HTTP server (small, predictable memory baseline), Prometheus metrics with the bounded
S04 §8 label set, capability verification with the public key only, and SIGTERM handling
that flips readiness off and drains bounded work.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import jwt
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

AUDIENCE = "capacity-workload-control"
ISSUER = "capacity-scenario-runner"
MAX_BODY = 16_384

REQUESTS = Counter("lab_requests_total", "Requests", ["workload", "operation", "outcome"])
DURATION = Histogram("lab_request_duration_seconds", "Request duration", ["workload", "operation", "outcome"],
                     buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5))
INFO = Gauge("lab_workload_info", "Workload info", ["workload", "version"])


class Capability:
    def __init__(self, path: str = "/etc/capacity/capability/public.pem") -> None:
        self._key = open(path, "rb").read() if os.path.exists(path) else None

    def check(self, header: str | None, workload: str, action: str) -> str | None:
        if self._key is None:
            return "verifier_not_configured"  # fail closed
        if not header or not header.startswith("Bearer "):
            return "missing"
        try:
            claims = jwt.decode(header[7:], self._key, algorithms=["ES256"], audience=AUDIENCE, issuer=ISSUER,
                                options={"require": ["exp", "aud", "iss"]})
        except jwt.PyJWTError:
            return "invalid"
        if workload not in claims.get("allowedWorkloads", []) or action not in claims.get("allowedActions", []):
            return "not_allowed"
        return None


Route = Callable[[dict[str, Any], dict[str, str]], tuple[int, dict[str, Any]]]


class Service:
    def __init__(self, workload: str, workload_id: str) -> None:
        self.workload, self.workload_id = workload, workload_id
        self.capability = Capability(os.environ.get("CAPABILITY_PUBLIC_KEY", "/etc/capacity/capability/public.pem"))
        self.accepting = True
        self.routes: dict[tuple[str, str], tuple[str, Route]] = {}
        INFO.labels(workload, os.environ.get("WORKLOAD_VERSION", "dev")).set(1)

    def route(self, method: str, prefix: str, action: str, fn: Route) -> None:
        self.routes[(method, prefix)] = (action, fn)

    def serve(self, port: int = 8080) -> None:
        service = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _dispatch(self, method: str) -> None:
                path = self.path.split("?")[0]
                if method == "GET" and path == "/health/live":
                    return self._send(200, {"status": "live"})
                if method == "GET" and path == "/health/ready":
                    return self._send(200 if service.accepting else 503, {"ready": service.accepting})
                if method == "GET" and path == "/metrics":
                    return self._send(200, generate_latest(), CONTENT_TYPE_LATEST)
                match = next(((k, v) for k, v in service.routes.items()
                              if k[0] == method and (path == k[1] or path.startswith(k[1] + "/"))), None)
                if match is None:
                    return self._send(404, {"error": "not_found"})
                (_, prefix), (action, fn) = match
                denied = service.capability.check(self.headers.get("Authorization"), service.workload_id, action)
                if denied:
                    return self._send(401, {"error": denied})
                if not service.accepting:
                    return self._send(503, {"error": "shutting_down"})
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self._send(413, {"error": "too_large"})
                try:
                    body = json.loads(self.rfile.read(length) or b"{}") if length else {}
                except json.JSONDecodeError:
                    return self._send(400, {"error": "malformed_json"})
                started = time.monotonic()
                code, payload = fn(body, {"path": path, "suffix": path[len(prefix):].strip("/")})
                outcome = "success" if code < 400 else ("rejected" if code in (409, 429) else "error")
                REQUESTS.labels(service.workload, action, outcome).inc()
                DURATION.labels(service.workload, action, outcome).observe(time.monotonic() - started)
                self._send(code, payload)

            def do_GET(self) -> None:  # noqa: N802
                self._dispatch("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._dispatch("POST")

            def log_message(self, *args: Any) -> None:
                pass  # no per-request logs with client data

        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 - cluster-internal ClusterIP only

        def stop(*_: Any) -> None:
            service.accepting = False  # readiness off first, then drain
            threading.Thread(target=lambda: (time.sleep(5), server.shutdown()), daemon=True).start()

        signal.signal(signal.SIGTERM, stop)
        server.serve_forever()
