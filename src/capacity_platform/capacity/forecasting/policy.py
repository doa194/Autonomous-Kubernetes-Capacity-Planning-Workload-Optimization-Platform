"""Versioned forecast eligibility policy registry (S14 companion §7).

The numbers here are the companion's *provisional* starting candidates. What is accepted
architecture is that thresholds exist, are versioned, and are recorded on every result;
the values themselves still need S31 calibration. An old forecast keeps the policy
revision it was evaluated under.
"""

from __future__ import annotations

from dataclasses import dataclass

from capacity_platform.capacity.forecasting.contracts import ForecastResource, ForecastStatistic
from capacity_platform.contracts.enums import Horizon


@dataclass(frozen=True)
class ForecastPolicy:
    revision: str
    bucket_seconds: int
    min_history_seconds: int
    min_coverage: float
    min_train_buckets: int
    origin_step_buckets: int
    max_folds: int
    min_calibration_folds: int
    min_test_folds: int
    nominal_coverage: float
    coverage_tolerance: float
    high_demand_allowance: float
    min_baseline_skill: float
    rolling_window: int
    season_period: int | None
    max_gap_buckets: int
    max_censored_fraction: float


_POLICIES: dict[tuple[Horizon, ForecastResource], ForecastPolicy] = {
    (Horizon.ONE_HOUR, ForecastResource.CPU): ForecastPolicy(
        revision="forecast-cpu-1h-v1",
        bucket_seconds=300,
        min_history_seconds=4 * 3600,
        min_coverage=0.95,
        min_train_buckets=24,
        origin_step_buckets=3,
        max_folds=40,
        min_calibration_folds=4,
        min_test_folds=4,
        nominal_coverage=0.90,
        coverage_tolerance=0.10,
        high_demand_allowance=0.10,
        min_baseline_skill=0.0,
        rolling_window=12,
        season_period=None,
        max_gap_buckets=3,
        max_censored_fraction=0.05,
    ),
    (Horizon.ONE_HOUR, ForecastResource.MEMORY): ForecastPolicy(
        revision="forecast-memory-1h-v1",
        bucket_seconds=300,
        min_history_seconds=4 * 3600,
        min_coverage=0.95,
        min_train_buckets=24,
        origin_step_buckets=3,
        max_folds=40,
        min_calibration_folds=4,
        min_test_folds=4,
        nominal_coverage=0.90,
        coverage_tolerance=0.10,
        # Memory under-prediction can mean OOM, so the allowance is stricter than CPU.
        high_demand_allowance=0.08,
        min_baseline_skill=0.0,
        rolling_window=12,
        season_period=None,
        max_gap_buckets=1,
        max_censored_fraction=0.0,
    ),
    (Horizon.TWENTY_FOUR_HOURS, ForecastResource.CPU): ForecastPolicy(
        revision="forecast-cpu-24h-v1",
        bucket_seconds=3600,
        min_history_seconds=72 * 3600,
        min_coverage=0.95,
        min_train_buckets=24,
        origin_step_buckets=6,
        max_folds=20,
        min_calibration_folds=2,
        min_test_folds=2,
        nominal_coverage=0.90,
        coverage_tolerance=0.10,
        high_demand_allowance=0.10,
        min_baseline_skill=0.0,
        rolling_window=24,
        season_period=24,
        max_gap_buckets=1,
        max_censored_fraction=0.05,
    ),
    (Horizon.TWENTY_FOUR_HOURS, ForecastResource.MEMORY): ForecastPolicy(
        revision="forecast-memory-24h-v1",
        bucket_seconds=3600,
        min_history_seconds=72 * 3600,
        min_coverage=0.95,
        min_train_buckets=24,
        origin_step_buckets=6,
        max_folds=20,
        min_calibration_folds=2,
        min_test_folds=2,
        nominal_coverage=0.90,
        coverage_tolerance=0.10,
        high_demand_allowance=0.08,
        min_baseline_skill=0.0,
        rolling_window=24,
        season_period=24,
        max_gap_buckets=0,
        max_censored_fraction=0.0,
    ),
}


def policy_for(horizon: Horizon, resource: ForecastResource, statistic: ForecastStatistic) -> ForecastPolicy:
    return _POLICIES[(horizon, resource)]


# Upstream S12 quality flag -> S14 refusal reason (S14 companion §2.3). A flag absent from
# this table is "unclassified" and blocks eligibility rather than being ignored.
FLAG_REFUSALS: dict[str, str | None] = {
    "scrape_gap": None,  # handled by the coverage computation itself
    "target_missing": None,
    "coverage_below_policy": "coverage_below_policy",
    "source_stale": "snapshot_stale_or_partial",
    "late_ingest": None,
    "counter_reset": None,  # S12-approved rate semantics already repaired the rate
    "insufficient_rate_samples": "coverage_below_policy",
    "owner_unresolved": "identity_unresolved",
    "uid_mapping_ambiguous": "identity_unresolved",
    "spec_changed_mid_window": "config_regime_incompatible",
    "snapshot_skew": "temporal_alignment_invalid",
    "unmatched_config_interval": "config_regime_incompatible",
    "peak_not_guaranteed": "memory_spike_fidelity_insufficient",
    "short_job_unsampled": "memory_spike_fidelity_insufficient",
    "histogram_low_count": None,
    "hpa_confounded": "hpa_unmodeled",
    "generator_overloaded": "host_or_generator_contamination",
    "host_pressure": "host_or_generator_contamination",
    "metric_unavailable": "metric_unavailable",
    "schema_mismatch": "metric_unavailable",
    "artifact_missing": "artifact_integrity_invalid",
    "cluster_incarnation_mismatch": "cluster_incarnation_mismatch",
}

# Peak-fidelity flags only block statistics that claim something about peaks.
PEAK_ONLY_FLAGS = {"peak_not_guaranteed", "short_job_unsampled"}
