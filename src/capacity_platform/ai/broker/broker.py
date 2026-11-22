"""Deterministic Tool Authorization Broker (S09 §5.1 pipeline, S17 §2).

ALLOW tool_call IF authenticated machine AND valid trusted delegation AND active workflow
AND role allows tool AND scope allows resource AND current policy/mode allow AND schema
valid AND budget available AND result disclosure allowed. Otherwise a typed denial.

The broker is defence in depth. Backends authorize again on arrival (they look up the
delegation themselves), so a compromised `ai` process that skips the broker still cannot
read outside the delegated scope, and no backend offers any mutating operation.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from capacity_platform.ai.broker.registry import REGISTRY_REVISION, TOOLS
from capacity_platform.contracts.canonical import digest
from capacity_platform.contracts.enums import AgentRole
from capacity_platform.contracts.workloads import CATALOG

log = logging.getLogger("ai.broker")


@dataclass(frozen=True)
class ToolAuthorizationContext:
    """Assembled by trusted workflow code only — never by a model."""

    workflow_run_id: str
    delegation_ref: str  # the leased `ai` job ID; backends re-validate it
    caller_machine: str
    logical_role: AgentRole
    actor_key: str
    cluster_id: str
    namespaces: frozenset[str]
    expires_at: datetime


@dataclass
class Budget:
    max_calls: int = 12
    max_calls_per_tool: int = 4
    max_result_bytes: int = 60_000
    calls: int = 0
    bytes_used: int = 0
    per_tool: Counter = field(default_factory=Counter)


class ToolResult(BaseModel):
    schemaVersion: str = "tool-result/v1alpha1"
    toolId: str
    status: (
        str  # completed | denied | unavailable | timed_out | stale | insufficient_evidence | malformed | rate_limited
    )
    reasonCode: str
    evidenceKind: str | None = None
    trust: str = "untrusted_data"
    sourceRef: str | None = None
    truncated: bool = False
    data: Any = None
    resultDigest: str | None = None


class ToolBackend(Protocol):
    def call(self, tool_id: str, args: dict, ctx: ToolAuthorizationContext) -> dict: ...


class BackendDenied(Exception):
    pass


class BackendUnavailable(Exception):
    pass


class ToolBroker:
    def __init__(
        self, backend: ToolBackend, clock: Callable[[], datetime], audit: Callable[[dict], None] | None = None
    ) -> None:
        self._backend, self._clock, self._audit = backend, clock, audit or (lambda _e: None)

    def _deny(self, ctx, tool_id, status, reason) -> ToolResult:
        self._audit(
            {
                "workflowRunId": ctx.workflow_run_id,
                "role": ctx.logical_role.value,
                "toolId": tool_id,
                "decision": "deny",
                "reason": reason,
                "registry": REGISTRY_REVISION,
            }
        )
        return ToolResult(toolId=str(tool_id)[:80], status=status, reasonCode=reason)

    def invoke(self, ctx: ToolAuthorizationContext, tool_id: Any, raw_args: Any, budget: Budget) -> ToolResult:
        # 1. registry resolution: exact versioned ID only.
        spec = TOOLS.get(tool_id) if isinstance(tool_id, str) else None
        if spec is None:
            return self._deny(ctx, tool_id, "denied", "tool_unknown")
        # 2. role binding comes from the trusted context, never from model text.
        if ctx.logical_role not in spec.roles:
            return self._deny(ctx, tool_id, "denied", "role_not_permitted")
        # 3. delegation freshness.
        if self._clock() >= ctx.expires_at:
            return self._deny(ctx, tool_id, "denied", "delegation_expired")
        # 4. typed validation; unknown fields, URLs, SQL etc. have nowhere to go.
        try:
            args = spec.args.model_validate(raw_args if isinstance(raw_args, dict) else {})
        except ValidationError:
            return self._deny(ctx, tool_id, "malformed", "arguments_invalid")
        workload = getattr(args, "workloadId", None)
        if workload is not None:
            entry = CATALOG.get(workload)
            if entry is None or entry.namespace not in ctx.namespaces:
                return self._deny(ctx, tool_id, "denied", "scope_denied")
        # 5. budget reservation.
        if budget.calls >= budget.max_calls or budget.per_tool[spec.tool_id] >= budget.max_calls_per_tool:
            return self._deny(ctx, tool_id, "rate_limited", "tool_budget_exhausted")
        budget.calls += 1
        budget.per_tool[spec.tool_id] += 1
        # 6. dispatch to the independently authorizing backend.
        try:
            data = self._backend.call(spec.tool_id, args.model_dump(), ctx)
        except BackendDenied:
            return self._deny(ctx, tool_id, "denied", "backend_denied")
        except BackendUnavailable:
            return ToolResult(toolId=spec.tool_id, status="unavailable", reasonCode="backend_unavailable")
        except TimeoutError:
            return ToolResult(toolId=spec.tool_id, status="timed_out", reasonCode="backend_timeout")
        # 7. response validation: bounded size, labelled as untrusted typed evidence.
        encoded = json.dumps(data, sort_keys=True, default=str)
        truncated = False
        limit = min(spec.max_result_bytes, budget.max_result_bytes - budget.bytes_used)
        if limit <= 0:
            return self._deny(ctx, tool_id, "rate_limited", "context_budget_exhausted")
        if len(encoded) > limit:
            truncated = True
            data = {"truncatedPreview": encoded[:limit], "note": "result truncated; values beyond are not shown"}
            encoded = json.dumps(data)
        budget.bytes_used += len(encoded)
        status = "completed"
        if isinstance(data, dict) and data.get("status") in ("insufficient_evidence", "refused", "not_applicable"):
            status = "insufficient_evidence"
        result = ToolResult(
            toolId=spec.tool_id,
            status=status,
            reasonCode="ok",
            evidenceKind=spec.evidence_kind,
            sourceRef=(data.get("sourceRef") if isinstance(data, dict) else None),
            truncated=truncated,
            data=data,
            resultDigest=digest(json.loads(encoded)),
        )
        self._audit(
            {
                "workflowRunId": ctx.workflow_run_id,
                "role": ctx.logical_role.value,
                "toolId": spec.tool_id,
                "decision": "allow",
                "reason": "ok",
                "bytes": len(encoded),
                "resultDigest": result.resultDigest,
            }
        )
        return result
