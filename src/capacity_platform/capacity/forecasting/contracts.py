"""Typed forecast inputs and outputs (S14 companion §1, §2, §6, §7).

The input is an already-qualified bucket series produced from registered S12 queries.
The engine never fetches data itself, so forecasting stays deterministic and testable
without Prometheus. Missing buckets are explicit (`value=None` with a status), never 0.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from capacity_platform.contracts.enums import Horizon, MeasurementOrigin, WorkloadOrigin

ENGINE_REVISION = "s14-engine-v1"


class ForecastResource(StrEnum):
    CPU = "cpu_consumption_millicores"
    MEMORY = "memory_working_set_bytes"


class ForecastStatistic(StrEnum):
    BUCKET_MEAN = "bucket_mean"
    BUCKET_MAX_SAMPLED = "bucket_max_sampled"


class BucketStatus(StrEnum):
    OBSERVED = "observed"
    MISSING = "missing"
    CENSORED = "censored"  # usage pinned at a limit (throttling/OOM): not unconstrained demand
    NOT_APPLICABLE = "not_applicable"  # target did not exist yet; not counted as a gap


class HistoryBucket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    startUtc: datetime
    value: float | None = None
    status: BucketStatus = BucketStatus.OBSERVED

    @model_validator(mode="after")
    def _consistent(self) -> HistoryBucket:
        if self.startUtc.tzinfo is None:
            raise ValueError("bucket timestamps must be UTC-aware")
        if self.status == BucketStatus.OBSERVED:
            if self.value is None:
                raise ValueError("observed bucket requires a value")
            if self.value != self.value or self.value in (float("inf"), float("-inf")):
                raise ValueError("NaN/Inf values are rejected, not coerced")
            if self.value < 0:
                raise ValueError("CPU/memory demand cannot be negative")
        elif self.status in (BucketStatus.MISSING, BucketStatus.NOT_APPLICABLE) and self.value is not None:
            raise ValueError("missing/not-applicable buckets must not carry a value")
        return self


class ForecastRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    clusterIncarnationRef: str
    namespace: str
    logicalWorkloadRef: str
    resource: ForecastResource
    statistic: ForecastStatistic = ForecastStatistic.BUCKET_MEAN
    horizon: Literal[Horizon.ONE_HOUR, Horizon.TWENTY_FOUR_HOURS]
    asOfUtc: datetime
    bucketSeconds: int = Field(gt=0)
    history: list[HistoryBucket]
    sourceQualityFlags: list[str] = Field(default_factory=list)
    measurementOrigin: MeasurementOrigin = MeasurementOrigin.HISTORICAL_OBSERVED
    workloadOrigin: WorkloadOrigin = WorkloadOrigin.BASELINE_LAB
    # True when a short scenario replays a "day" faster than wall time (S05 §10.2).
    timeCompressed: bool = False
    inputManifestDigest: str | None = None
    observationRefs: list[str] = Field(default_factory=list)

    @field_validator("asOfUtc")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("asOfUtc must be UTC-aware")
        return value


class PredictionBucket(BaseModel):
    startUtc: datetime
    endUtc: datetime
    point: float
    lower: float | None
    upper: float | None


class Uncertainty(BaseModel):
    nominalCoverage: float | None
    method: str
    calibrationSampleCount: int
    empiricalCoverage: float | None
    effectiveTestSamples: int
    meanIntervalWidth: float | None
    limitations: list[str]


class EvaluationMetrics(BaseModel):
    """Held-out walk-forward metrics. `None` means undefined (n=0), never zero error."""

    family: str
    params: dict[str, float | int]
    testSamples: int
    mae: float | None
    rmse: float | None
    meanBias: float | None
    baselineSkill: float | None
    pinballUpper: float | None
    intervalCoverage: float | None
    meanIntervalWidth: float | None
    highDemandUnderpredictionRate: float | None
    qualified: bool
    rejectionReasons: list[str]


class Eligibility(BaseModel):
    sourceQuality: Literal["qualified", "rejected"]
    identityIntegrity: Literal["resolved", "unresolved"]
    configurationStability: Literal["stable", "incompatible"]
    historySupport: Literal["sufficient", "insufficient"]
    backtest: Literal["passed", "failed", "not_run"]
    intervalCalibration: Literal["validated", "failed", "unavailable"]
    horizonSupport: Literal["supported", "unsupported"]
    overall: Literal["eligible", "refused", "not_applicable"]
    decisionEligibility: Literal["eligible_for_named_evaluation_only", "none"]
    policyRevision: str
    reasonCodes: list[str]


class ForecastResult(BaseModel):
    schemaVersion: str = "forecast-result/v1alpha1"
    forecastId: str
    status: Literal["available", "refused", "not_applicable"]
    request: ForecastRequest
    unit: Literal["millicore", "byte"]
    predictions: list[PredictionBucket]
    uncertainty: Uncertainty | None
    selectedModel: EvaluationMetrics | None
    candidates: list[EvaluationMetrics]
    eligibility: Eligibility
    foldOriginsUtc: list[datetime]
    engineRevision: str = ENGINE_REVISION
    inputDigest: str

    def upper_peak(self) -> float | None:
        """Largest upper bound over the horizon, the value right-sizing uses as a demand input."""
        uppers = [p.upper for p in self.predictions if p.upper is not None]
        return max(uppers) if uppers else None


def bucket_grid(as_of: datetime, horizon: Horizon, bucket_seconds: int) -> list[tuple[datetime, datetime]]:
    """S14 companion §1.1: B_k = [asOf + kΔ, asOf + (k+1)Δ). Horizon must divide exactly."""
    horizon_seconds = 3600 if horizon == Horizon.ONE_HOUR else 86400
    if horizon_seconds % bucket_seconds:
        raise ValueError("horizon is not divisible by the bucket size")
    step = timedelta(seconds=bucket_seconds)
    return [(as_of + k * step, as_of + (k + 1) * step) for k in range(horizon_seconds // bucket_seconds)]
