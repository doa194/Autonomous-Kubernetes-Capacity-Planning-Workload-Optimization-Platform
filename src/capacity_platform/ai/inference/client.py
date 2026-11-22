"""S15 bounded inference client used by the `ai` service.

All model traffic goes to the protected host gateway — never to Ollama directly, never to
a remote service. One generation request at a time (process-wide semaphore), bounded
prompt size, bounded output tokens, deterministic decoding (temperature 0, fixed seed),
schema-constrained JSON output and a hard deadline. Prompts and responses are not logged;
only model identity, token counts and duration are recorded.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field


class ModelRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    digest: str | None = None  # must be filled by `capacity-lab model qualify` before use


class InferenceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schemaVersion: str
    profileId: str
    revision: int
    gatewayUrl: str
    generationModel: ModelRef
    embeddingModel: ModelRef | None = None
    embeddingDimensions: int | None = None
    maxPromptChars: int = Field(gt=0, le=60_000)
    maxOutputTokens: int = Field(gt=0, le=2048)
    numCtx: int = Field(gt=0, le=16384)
    timeoutSeconds: float = Field(gt=0, le=300)
    seed: int = 42
    qualified: bool = False
    qualificationRef: str | None = None


def load_profile(path: Path) -> InferenceProfile:
    with path.open(encoding="utf-8") as handle:
        return InferenceProfile.model_validate(yaml.safe_load(handle))


class InferenceUnavailable(Exception):
    """Gateway down, model missing, timeout or malformed output: the advisory is incomplete."""


class InvocationMeta(BaseModel):
    model: str
    modelDigest: str | None
    promptChars: int
    promptTokens: int | None
    outputTokens: int | None
    durationMs: int


_GENERATION_SLOT = threading.Semaphore(1)


class GatewayClient:
    def __init__(
        self,
        profile: InferenceProfile,
        api_key_file: str | None,
        ca_bundle: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.profile = profile
        self._key_file, self._verify = api_key_file, ca_bundle or True
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        if not self._key_file:
            raise InferenceUnavailable("gateway credential not mounted")
        return {"Authorization": "Bearer " + Path(self._key_file).read_text(encoding="utf-8").strip()}

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.profile.gatewayUrl,
            timeout=self.profile.timeoutSeconds,
            verify=self._verify,
            transport=self._transport,
        )

    def healthy(self) -> bool:
        try:
            with self._client() as client:
                return client.get("/healthz", timeout=2.0).status_code == 200
        except httpx.HTTPError:
            return False

    def chat_json(self, system: str, user: str, schema: dict[str, Any]) -> tuple[str, InvocationMeta]:
        prompt_chars = len(system) + len(user)
        if prompt_chars > self.profile.maxPromptChars:
            raise InferenceUnavailable("prompt exceeds the profile budget")
        body = {
            "model": self.profile.generationModel.name,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": schema,
            "options": {
                "temperature": 0,
                "seed": self.profile.seed,
                "num_ctx": self.profile.numCtx,
                "num_predict": self.profile.maxOutputTokens,
            },
        }
        if not _GENERATION_SLOT.acquire(timeout=self.profile.timeoutSeconds):
            raise InferenceUnavailable("inference slot busy")  # backpressure, not an unbounded queue
        started = time.monotonic()
        try:
            with self._client() as client:
                resp = client.post("/api/chat", json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise InferenceUnavailable(type(exc).__name__) from exc
        finally:
            _GENERATION_SLOT.release()
        if resp.status_code != 200:
            raise InferenceUnavailable(f"gateway_status_{resp.status_code}")
        data = resp.json()
        content = (data.get("message") or {}).get("content")
        if not isinstance(content, str):
            raise InferenceUnavailable("malformed_model_response")
        return content, InvocationMeta(
            model=self.profile.generationModel.name,
            modelDigest=self.profile.generationModel.digest,
            promptChars=prompt_chars,
            promptTokens=data.get("prompt_eval_count"),
            outputTokens=data.get("eval_count"),
            durationMs=int((time.monotonic() - started) * 1000),
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.profile.embeddingModel is None:
            raise InferenceUnavailable("no qualified embedding model in profile")
        try:
            with self._client() as client:
                resp = client.post(
                    "/api/embed",
                    headers=self._headers(),
                    json={"model": self.profile.embeddingModel.name, "input": texts[:32]},
                )
        except httpx.HTTPError as exc:
            raise InferenceUnavailable(type(exc).__name__) from exc
        if resp.status_code != 200:
            raise InferenceUnavailable(f"gateway_status_{resp.status_code}")
        vectors = resp.json().get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts[:32]):
            raise InferenceUnavailable("malformed_embedding_response")
        dims = self.profile.embeddingDimensions
        if dims and any(len(v) != dims for v in vectors):
            raise InferenceUnavailable("embedding_dimension_mismatch")
        return vectors
