"""S14 forecasting contract tests (companion §9.1 property list and §7.3 refusals)."""

import itertools
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from pydantic import ValidationError

from capacity_platform.capacity.forecasting.contracts import (
    BucketStatus,
    ForecastRequest,
    HistoryBucket,
    bucket_grid,
)
from capacity_platform.capacity.forecasting.engine import ForecastInputError, _metrics, forecast
from capacity_platform.capacity.forecasting.envelope import JobRunSummary, aggregate, run_envelope
from capacity_platform.capacity.forecasting.policy import policy_for
from capacity_platform.contracts.enums import Horizon

AS_OF = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)


def _history(values, minutes=5, statuses=None):
    n = len(values)
    out = []
    for i, v in enumerate(values):
        status = (statuses or {}).get(i, BucketStatus.OBSERVED if v is not None else BucketStatus.MISSING)
        out.append(HistoryBucket(startUtc=AS_OF - timedelta(minutes=minutes * (n - i)), value=v, status=status))
    return out


def _request(values, **overrides):
    base = dict(
        clusterIncarnationRef="inc-1",
        namespace="workloads-batch",
        logicalWorkloadRef="w04-cpu-worker",
        resource="cpu_consumption_millicores",
        horizon="1h",
        asOfUtc=AS_OF,
        bucketSeconds=300,
        history=_history(values),
    )
    base.update(overrides)
    return ForecastRequest(**base)


def _stationary(n=96, level=120.0, sd=6.0, seed=11):
    return [float(max(v, 0)) for v in level + np.random.default_rng(seed).normal(0, sd, n)]


def test_bucket_grid_counts_and_contiguity():
    one_hour = bucket_grid(AS_OF, Horizon.ONE_HOUR, 300)
    day = bucket_grid(AS_OF, Horizon.TWENTY_FOUR_HOURS, 3600)
    assert len(one_hour) == 12 and len(day) == 24
    assert one_hour[0][0] == AS_OF
    assert all(a[1] == b[0] for a, b in itertools.pairwise(one_hour))


def test_stationary_cpu_series_produces_calibrated_forecast():
    result = forecast(_request(_stationary()))
    assert result.status == "available"
    assert result.eligibility.overall == "eligible"
    assert len(result.predictions) == 12
    for p in result.predictions:
        assert np.isfinite(p.point) and p.point >= 0
        assert p.lower is not None and p.upper is not None and p.lower <= p.point <= p.upper
    assert result.unit == "millicore"
    # Coverage is reported from held-out folds, with its sample count.
    assert result.uncertainty.empiricalCoverage is not None
    assert result.uncertainty.effectiveTestSamples > 0


def test_same_input_gives_identical_forecast():
    a = forecast(_request(_stationary()))
    b = forecast(_request(_stationary()))
    assert a.forecastId == b.forecastId
    assert [p.point for p in a.predictions] == [p.point for p in b.predictions]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_invalid_numeric_values_are_rejected_not_coerced(bad):
    with pytest.raises(ValidationError):
        HistoryBucket(startUtc=AS_OF, value=bad)


def test_duplicate_timestamps_are_a_typed_input_error():
    hist = _history(_stationary(60))
    hist[10] = HistoryBucket(startUtc=hist[9].startUtc, value=1.0)
    with pytest.raises(ForecastInputError):
        forecast(_request([0.0], history=hist))


def test_missing_samples_are_never_treated_as_zero():
    values = _stationary()
    for i in range(30, 36):  # a 30-minute gap, longer than the CPU gap policy
        values[i] = None
    result = forecast(_request(values))
    assert result.status == "refused"
    assert "coverage_below_policy" in result.eligibility.reasonCodes
    assert result.predictions == []


def test_history_after_cutoff_is_refused_as_leakage():
    req = _request(_stationary(), asOfUtc=AS_OF - timedelta(minutes=10))
    result = forecast(req)
    assert result.eligibility.reasonCodes == ["future_information_leakage"]


def test_short_history_is_refused():
    result = forecast(_request(_stationary(30)))  # 2.5h < 4h policy
    assert "insufficient_history" in result.eligibility.reasonCodes


def test_compressed_scenario_time_never_counts_as_24h_history():
    values = _stationary(96, seed=3)
    req = _request(values, horizon="24h", bucketSeconds=3600, history=_history(values, minutes=60), timeCompressed=True)
    result = forecast(req)
    assert result.status == "refused"
    assert "insufficient_history" in result.eligibility.reasonCodes
    assert result.eligibility.horizonSupport == "unsupported"


def test_suspended_batch_workload_is_not_applicable_not_zero_demand():
    result = forecast(_request(_stationary(), logicalWorkloadRef="w05-analytics-processor"))
    assert result.status == "not_applicable"
    assert result.predictions == []


@pytest.mark.parametrize(
    ("flag", "reason"),
    [
        ("hpa_confounded", "hpa_unmodeled"),
        ("uid_mapping_ambiguous", "identity_unresolved"),
        ("host_pressure", "host_or_generator_contamination"),
        ("something_new_from_s12", "unclassified_quality_issue"),
    ],
)
def test_source_quality_flags_map_to_typed_refusals(flag, reason):
    result = forecast(_request(_stationary(), sourceQualityFlags=[flag]))
    assert result.status == "refused"
    assert reason in result.eligibility.reasonCodes


def test_peak_flag_blocks_peak_statistic_but_only_limits_mean():
    values = _stationary(96, level=300e6, sd=5e6)
    common = dict(
        resource="memory_working_set_bytes",
        logicalWorkloadRef="w03-memory-profile",
        sourceQualityFlags=["peak_not_guaranteed"],
    )
    peak = forecast(_request(values, statistic="bucket_max_sampled", **common))
    assert "memory_spike_fidelity_insufficient" in peak.eligibility.reasonCodes
    mean = forecast(_request(values, **common))
    if mean.status == "available":
        assert "memory_peak_not_guaranteed_not_sole_downsize_proof" in mean.uncertainty.limitations


def test_censored_cpu_demand_is_refused():
    values = _stationary()
    statuses = {i: BucketStatus.CENSORED for i in range(0, 10)}
    req = _request(values, history=_history(values, statuses=statuses))
    assert "demand_censored" in forecast(req).eligibility.reasonCodes


def test_better_average_error_with_bad_tail_is_not_promoted():
    policy = policy_for(Horizon.ONE_HOUR, "cpu_consumption_millicores", "bucket_mean")
    # Every high-demand actual (>=200) is above the upper band: low MAE overall, unsafe tail.
    test = [(0, 100.0, 100.0)] * 80 + [(0, 260.0, 210.0)] * 20
    bands = [(-5.0, 5.0)]
    metrics = _metrics("ses", {}, test, bands, baseline_mae=50.0, high_threshold=200.0, policy=policy, test_folds=10)
    assert metrics.mae < 50.0
    assert not metrics.qualified
    assert "high_demand_underprediction" in metrics.rejectionReasons


def test_undefined_metrics_are_none_not_zero():
    policy = policy_for(Horizon.ONE_HOUR, "cpu_consumption_millicores", "bucket_mean")
    metrics = _metrics("ses", {}, [], None, None, 0.0, policy, 0)
    assert metrics.mae is None and metrics.rmse is None and not metrics.qualified


def test_mae_arithmetic_matches_companion_example():
    # S14 companion §5.4: actual [100,150,200], forecast [110,130,220] -> MAE 50/3.
    policy = policy_for(Horizon.ONE_HOUR, "cpu_consumption_millicores", "bucket_mean")
    test = [(0, 100.0, 110.0), (1, 150.0, 130.0), (2, 200.0, 220.0)] * 4
    metrics = _metrics("persistence", {}, test, None, None, 1e9, policy, 4)
    assert metrics.mae == pytest.approx(50 / 3)


def test_run_envelope_requires_comparable_declared_runs():
    runs = [
        JobRunSummary(
            profileHash="p1",
            durationSeconds=60,
            peakCpuMillicores=200,
            peakMemoryBytes=None,
            sourceFidelity="application_reported",
        )
    ] * 2
    assert run_envelope("w05-analytics-processor", "p1", runs).status == "refused"
    extra = JobRunSummary(
        profileHash="p1",
        durationSeconds=80,
        peakCpuMillicores=240,
        peakMemoryBytes=300000000.0,
        sourceFidelity="scrape_sampled",
    )
    runs = [*runs, extra]
    env = run_envelope("w05-analytics-processor", "p1", runs)
    assert env.status == "available"
    assert env.nominalCoverage is None
    assert env.memoryBytesUpper == pytest.approx(300e6 * 1.2)  # runs without samples add no zero peak
    assert "some_runs_without_memory_samples" in env.caveats


def test_aggregate_projection_never_claims_joint_coverage():
    agg = aggregate([[1, 2], [3, 4]], [[2, 3], [4, 5]])
    assert agg.points == [4, 6]
    assert agg.nominal_coverage is None and agg.joint_calibration_status == "not_validated"
