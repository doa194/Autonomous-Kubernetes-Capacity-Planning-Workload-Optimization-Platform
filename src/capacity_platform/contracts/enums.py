"""Closed vocabularies shared by every service.

Each enum is a security-relevant contract: an unknown value is rejected by Pydantic rather
than being coerced (S02 §6.6). Keeping them in one module stops two services from drifting
into different meanings for the same word.
"""

from __future__ import annotations

from enum import StrEnum


class OperatingMode(StrEnum):
    """S01-ADR-008. Only Governed may lead to a mutation, and only after human approval."""

    OBSERVE = "Observe"
    ADVISORY = "Advisory"
    GOVERNED = "Governed"


class Criticality(StrEnum):
    """S01-ADR-005 product policy class. Not the same thing as a Kubernetes PriorityClass."""

    CRITICAL = "Critical"
    STANDARD = "Standard"
    FLEXIBLE = "Flexible"
    OPPORTUNISTIC = "Opportunistic"


class GateStatus(StrEnum):
    """S19-FR-003 four-way rule outcome. `unknown` never counts as a pass."""

    PASS = "pass"  # noqa: S105 - gate status, not a password
    FAIL = "fail"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class Eligibility(StrEnum):
    """Domain E §5 classification of a candidate after its hard gates."""

    QUALIFIED = "qualified"
    REJECTED = "rejected"
    INDETERMINATE = "indeterminate"


class Horizon(StrEnum):
    """S01-ADR-006. `current` is an assessment, not a model forecast."""

    CURRENT = "current"
    ONE_HOUR = "1h"
    TWENTY_FOUR_HOURS = "24h"


class ResourceDimension(StrEnum):
    CPU = "cpu"
    MEMORY = "memory"


class ResourceField(StrEnum):
    """The only four fields the P0 action catalog may change (S20-FR-001, S25.2.2)."""

    CPU_REQUEST = "cpu_request"
    MEMORY_REQUEST = "memory_request"
    CPU_LIMIT = "cpu_limit"
    MEMORY_LIMIT = "memory_limit"


class MeasurementOrigin(StrEnum):
    """S12 §6.3 / S13 §5.2. Owner slices restrict who may produce each value."""

    OBSERVED_LIVE = "observed_live"
    HISTORICAL_OBSERVED = "historical_observed"
    SIMULATED = "simulated"
    FORECAST = "forecast"
    CANDIDATE_ESTIMATE = "candidate_estimate"
    REALIZED_POST_CHANGE = "realized_post_change"


class WorkloadOrigin(StrEnum):
    SCENARIO_GENERATED = "scenario_generated"
    BASELINE_LAB = "baseline_lab"
    EXTERNAL_OR_UNKNOWN = "external_or_unknown"


class SourceFidelity(StrEnum):
    SCRAPE_SAMPLED = "scrape_sampled"
    APPLICATION_REPORTED = "application_reported"
    KUBERNETES_STATE_OBSERVED = "kubernetes_state_observed"
    ANALYTICAL_DERIVED = "analytical_derived"
    ESTIMATED = "estimated"


class Coverage(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INCONSISTENT = "inconsistent"
    UNAVAILABLE = "unavailable"


class Freshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class Classification(StrEnum):
    """S08 §6 data classification. Restricted data never enters prompts, RAG or evidence."""

    PUBLIC = "Public"
    INTERNAL = "Internal"
    CONFIDENTIAL = "Confidential"
    RESTRICTED = "Restricted"


class HumanRole(StrEnum):
    """S06 §6.1 application roles. Non-hierarchical: no role implies another."""

    VIEWER = "Viewer"
    ANALYST = "Analyst"
    PLANNER = "Planner"
    APPROVER = "Approver"
    EXECUTOR = "Executor"
    ADMINISTRATOR = "Administrator"


class AgentRole(StrEnum):
    """S16 fixed logical agents. Assigned by trusted workflow code, never by a model."""

    CAPACITY_ANALYST = "CapacityAnalyst"
    OPTIMIZATION_PLANNER = "OptimizationPlanner"
    RISK_REVIEWER = "RiskReviewer"
