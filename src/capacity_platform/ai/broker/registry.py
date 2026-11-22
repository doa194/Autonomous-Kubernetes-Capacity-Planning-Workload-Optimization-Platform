"""Static, versioned tool registry and role permission matrix (S09 §4-5, S17 §3, Domain E §8).

Tools are code-defined. Nothing at runtime (model output, retrieved text, configuration
from a document) can register a tool, widen a role, or add an argument. Every argument
model forbids unknown fields and bounds its values, so there is no slot for a URL, SQL,
PromQL, shell command, file path or free-form patch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from capacity_platform.contracts.enums import AgentRole

REGISTRY_REVISION = "tool-registry-v1"
WORKLOAD_ID = r"^w0[1-6]-[a-z-]{3,30}$"
DIGEST = r"^sha256:[0-9a-f]{64}$"


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkloadArgs(_Args):
    workloadId: str = Field(pattern=WORKLOAD_ID)


class MetricsArgs(_Args):
    workloadId: str = Field(pattern=WORKLOAD_ID)
    queryId: Literal[
        "container.cpu.rate.millicores",
        "container.memory.working_set.bytes",
        "container.cpu.throttle.ratio",
        "app.http.p95.seconds",
        "app.http.error.ratio",
        "app.queue.depth",
    ]
    hours: int = Field(ge=1, le=6)


class ForecastArgs(_Args):
    workloadId: str = Field(pattern=WORKLOAD_ID)
    resource: Literal["cpu_consumption_millicores", "memory_working_set_bytes"]


class KnowledgeArgs(_Args):
    query: str = Field(min_length=3, max_length=300)
    collections: list[Literal["runbooks", "incidents", "configuration_history", "verified_outcomes"]] | None = None
    topK: int = Field(default=5, ge=1, le=5)


class EvaluationArgs(_Args):
    evaluationDigest: str = Field(pattern=DIGEST)


class AnalysisArgs(_Args):
    analysisDigest: str = Field(pattern=DIGEST)


@dataclass(frozen=True)
class ToolSpec:
    tool_id: str
    args: type[_Args]
    roles: frozenset[AgentRole]
    evidence_kind: str
    max_result_bytes: int
    mutating: bool = False  # always False in P0; kept explicit so a test can assert it


A, P, R = AgentRole.CAPACITY_ANALYST, AgentRole.OPTIMIZATION_PLANNER, AgentRole.RISK_REVIEWER
ALL = frozenset({A, P, R})

TOOLS: dict[str, ToolSpec] = {
    t.tool_id: t
    for t in (
        ToolSpec("cluster.get_workload_state.v1", WorkloadArgs, ALL, "current_observation", 16_000),
        ToolSpec("cluster.get_node_fit_inputs.v1", WorkloadArgs, ALL, "current_observation", 16_000),
        ToolSpec("metrics.get_approved_window.v1", MetricsArgs, ALL, "historical_observed", 8_000),
        ToolSpec("forecast.get_capacity_estimate.v1", ForecastArgs, ALL, "deterministic_forecast", 16_000),
        ToolSpec("knowledge.search_authorized.v1", KnowledgeArgs, ALL, "retrieved_document", 12_000),
        ToolSpec("candidate.list_eligible.v1", AnalysisArgs, ALL, "analytical_simulation", 24_000),
        ToolSpec("simulation.get_result.v1", EvaluationArgs, ALL, "analytical_simulation", 24_000),
        # Planner-only invoke tool (Domain E §8): bounded re-evaluation of an existing candidate.
        ToolSpec("simulation.evaluate_candidate.v1", EvaluationArgs, frozenset({P}), "analytical_simulation", 24_000),
        ToolSpec("policy.inspect_constraints.v1", WorkloadArgs, ALL, "policy_reference", 8_000),
        ToolSpec("risk.get_deterministic_findings.v1", EvaluationArgs, ALL, "analytical_simulation", 12_000),
    )
}

# Names that must never appear in the registry (S09 §5.2 forbidden universal tools).
FORBIDDEN_TOOL_NAMES = (
    "http_fetch",
    "sql",
    "promql",
    "shell",
    "kubectl",
    "file",
    "secret",
    "approve",
    "execute",
    "patch",
    "experiment",
    "policy.edit",
)
