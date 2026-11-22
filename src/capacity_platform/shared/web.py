"""FastAPI application factory shared by the six services (S28 telemetry standard).

Every service gets: a correlation ID per request (propagated via `X-Correlation-ID`),
structured JSON logs with redaction, the typed error envelope, bounded request bodies,
Prometheus metrics with low-cardinality labels only, and separate liveness/readiness.
Readiness reports *dependency-specific* state, so an Ollama outage makes only the AI
capability degraded, never the whole platform (S28 §4).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

from capacity_platform.contracts.errors import ErrorCategory, PlatformError, to_envelope
from capacity_platform.shared.redaction import redact

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="-")
MAX_BODY_BYTES = 256 * 1024

REQUESTS = Counter("capacity_http_requests_total", "HTTP requests", ["service", "route", "status_class"])
LATENCY = Histogram(
    "capacity_http_request_duration_seconds",
    "HTTP latency",
    ["service", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message, _ = redact(record.getMessage())
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": message,
            "correlationId": correlation_id.get(),
        }
        if record.exc_info:
            payload["error"] = redact(self.formatException(record.exc_info))[0][-2000:]
        return json.dumps(payload)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def create_app(service: str, readiness: Callable[[], dict[str, str]] | None = None) -> FastAPI:
    app = FastAPI(
        title=f"capacity-{service}", version="1.0.0", openapi_url="/openapi.json", docs_url=None, redoc_url=None
    )
    log = logging.getLogger(service)

    @app.middleware("http")
    async def _context(request: Request, call_next):
        cid = request.headers.get("x-correlation-id") or str(uuid.uuid4())
        if len(cid) > 64 or not all(c.isalnum() or c in "-_" for c in cid):
            cid = str(uuid.uuid4())  # never reflect attacker-controlled junk into logs
        token = correlation_id.set(cid)
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            correlation_id.reset(token)
            return JSONResponse(status_code=413, content={"error": {"code": "BODY_TOO_LARGE"}})
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            route = getattr(request.scope.get("route"), "path", "unmatched")
            LATENCY.labels(service, route).observe(time.perf_counter() - start)
        REQUESTS.labels(service, route, f"{response.status_code // 100}xx").inc()
        response.headers["X-Correlation-ID"] = cid
        correlation_id.reset(token)
        return response

    @app.exception_handler(PlatformError)
    async def _platform_error(_: Request, exc: PlatformError):
        if exc.category in (ErrorCategory.INTERNAL, ErrorCategory.UNAVAILABLE):
            log.warning("request failed: %s", exc.code)
        return JSONResponse(
            status_code=exc.http_status, content=to_envelope(exc, correlation_id.get()).model_dump(mode="json")
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        err = PlatformError("REQUEST_INVALID", ErrorCategory.VALIDATION, "request failed schema validation")
        body = to_envelope(err, correlation_id.get()).model_dump(mode="json")
        body["error"]["fields"] = [".".join(str(p) for p in e["loc"]) for e in exc.errors()][:20]
        return JSONResponse(status_code=422, content=body)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        log.exception("unhandled error")
        err = PlatformError("INTERNAL_ERROR", ErrorCategory.INTERNAL, "internal error")
        return JSONResponse(status_code=500, content=to_envelope(err, correlation_id.get()).model_dump(mode="json"))

    @app.get("/health/live", include_in_schema=False)
    def live() -> dict:
        return {"status": "live"}

    @app.get("/health/ready", include_in_schema=False)
    def ready():
        checks = readiness() if readiness else {}
        critical_down = [k for k, v in checks.items() if v == "down" and not k.startswith("optional:")]
        status = 503 if critical_down else 200
        return JSONResponse(
            status_code=status, content={"status": "ready" if status == 200 else "not_ready", "checks": checks}
        )

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
