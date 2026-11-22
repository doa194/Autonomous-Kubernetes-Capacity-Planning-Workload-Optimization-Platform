"""Protected host-side Ollama gateway (S09 §9, S15 §2, S07 §7.3).

A small authenticated reverse proxy that runs on the Windows host next to Ollama. It is a
supporting host component, not a platform Deployment. It:

* requires a bearer key (constant-time compare) that only the `ai` service holds;
* exposes exactly two routes, `/api/chat` and `/api/embed`, and nothing else of Ollama
  (no pull, push, create, delete, or model listing);
* allows only the pinned model names, rebuilds the request from allowlisted fields,
  caps output tokens and context, and refuses streaming;
* runs one generation at a time with a short wait queue (429 when full);
* logs model, status, sizes and duration — never prompt or response text.

Ollama itself stays bound to 127.0.0.1 so the gateway is the only path to it.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("model-gateway")
MAX_BODY = 200_000
ALLOWED_OPTIONS = {"temperature", "seed", "num_ctx", "num_predict"}


def build_gateway(
    key_file: str | None,
    upstream: str,
    chat_models: set[str],
    embed_models: set[str],
    max_output_tokens: int = 1024,
    max_ctx: int = 8192,
    max_waiting: int = 2,
    key: bytes | None = None,
) -> FastAPI:
    """`key` may be passed directly (the lab launcher reads it from Vault) so it never touches disk."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    if key is None:
        if not key_file:
            raise ValueError("gateway needs a key or key_file")
        key = Path(key_file).read_text(encoding="utf-8").strip().encode()
    slot = asyncio.Semaphore(1)
    waiting = {"n": 0}

    def authorized(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return False
        return hmac.compare_digest(header[7:].encode(), key)

    async def forward(path: str, body: dict) -> JSONResponse:
        async with httpx.AsyncClient(base_url=upstream, timeout=180.0) as client:
            resp = await client.post(path, json=body)
        return JSONResponse(status_code=resp.status_code, content=resp.json())

    @app.middleware("http")
    async def guard(request: Request, call_next):
        started = time.monotonic()
        if request.url.path not in ("/api/chat", "/api/embed", "/healthz") or (
            request.method != "POST" and request.url.path != "/healthz"
        ):
            return JSONResponse(status_code=404, content={"error": "not_found"})
        if request.url.path != "/healthz" and not authorized(request):
            log.warning("gateway auth denied from %s", request.client.host if request.client else "?")
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        length = request.headers.get("content-length")
        if length and int(length) > MAX_BODY:
            return JSONResponse(status_code=413, content={"error": "too_large"})
        response = await call_next(request)
        log.info(
            "gateway %s status=%s ms=%d",
            request.url.path,
            response.status_code,
            int((time.monotonic() - started) * 1000),
        )
        return response

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/api/chat")
    async def chat(request: Request):
        raw = await request.json()
        if raw.get("model") not in chat_models or raw.get("stream", False):
            return JSONResponse(status_code=403, content={"error": "model_or_mode_not_allowed"})
        options = {k: v for k, v in (raw.get("options") or {}).items() if k in ALLOWED_OPTIONS}
        options["num_predict"] = min(int(options.get("num_predict", max_output_tokens)), max_output_tokens)
        options["num_ctx"] = min(int(options.get("num_ctx", max_ctx)), max_ctx)
        messages = [
            {"role": m.get("role"), "content": str(m.get("content", ""))}
            for m in raw.get("messages", [])
            if m.get("role") in ("system", "user", "assistant")
        ]
        body = {"model": raw["model"], "messages": messages, "stream": False, "options": options}
        if isinstance(raw.get("format"), dict | str):
            body["format"] = raw["format"]
        if waiting["n"] >= max_waiting:
            return JSONResponse(status_code=429, content={"error": "busy"})
        waiting["n"] += 1
        try:
            async with slot:
                return await forward("/api/chat", body)
        finally:
            waiting["n"] -= 1

    @app.post("/api/embed")
    async def embed(request: Request):
        raw = await request.json()
        if raw.get("model") not in embed_models:
            return JSONResponse(status_code=403, content={"error": "model_not_allowed"})
        inputs = raw.get("input")
        if not isinstance(inputs, list) or len(inputs) > 32:
            return JSONResponse(status_code=422, content={"error": "input_invalid"})
        return await forward("/api/embed", {"model": raw["model"], "input": [str(x)[:8000] for x in inputs]})

    return app


def main() -> None:
    """Entry point: `capacity-model-gateway` (reads settings from environment)."""
    import uvicorn

    logging.basicConfig(level="INFO", format="%(asctime)s %(name)s %(message)s")
    app = build_gateway(
        key_file=os.environ["GATEWAY_KEY_FILE"],
        upstream=os.environ.get("OLLAMA_UPSTREAM", "http://127.0.0.1:11434"),
        chat_models=set(filter(None, os.environ.get("GATEWAY_CHAT_MODELS", "").split(","))),
        embed_models=set(filter(None, os.environ.get("GATEWAY_EMBED_MODELS", "").split(","))),
    )
    uvicorn.run(
        app,
        host=os.environ.get("GATEWAY_BIND", "127.0.0.1"),
        port=int(os.environ.get("GATEWAY_PORT", "11500")),
        ssl_certfile=os.environ.get("GATEWAY_TLS_CERT"),
        ssl_keyfile=os.environ.get("GATEWAY_TLS_KEY"),
    )
