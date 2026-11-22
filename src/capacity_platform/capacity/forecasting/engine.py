"""S14 forecast engine: eligibility -> walk-forward backtest -> calibrated forecast.

Flow (S14 companion §12): Validating -> FeatureBuilding -> Fitting -> Backtesting ->
Calibrating -> Available, or a typed refusal at any gate. A refusal is a normal result,
not an error: it records what was checked and why the platform declines to predict.

Anti-leakage rules (§5.2) enforced here:
* every history bucket must end at or before `asOfUtc`;
* each backtest fold fits only on data before its origin;
* interval quantiles come from earlier calibration folds, coverage is measured on later
  test folds, so the reported coverage is never computed on the data that built it.
"""

from __future__ import annotations

import itertools
import math
from datetime import timedelta

import numpy as np

from capacity_platform.capacity.forecasting import models as m
from capacity_platform.capacity.forecasting.contracts import (
    BucketStatus,
    Eligibility,
    EvaluationMetrics,
    ForecastRequest,
    ForecastResource,
    ForecastResult,
    ForecastStatistic,
    PredictionBucket,
    Uncertainty,
    bucket_grid,
)
from capacity_platform.capacity.forecasting.policy import (
    FLAG_REFUSALS,
    PEAK_ONLY_FLAGS,
    ForecastPolicy,
    policy_for,
)
from capacity_platform.contracts.canonical import digest
from capacity_platform.contracts.enums import Horizon
from capacity_platform.contracts.workloads import CATALOG


class ForecastInputError(ValueError):
    """Structurally invalid input (duplicate timestamps, broken grid). Distinct from refusal."""


# --------------------------------------------------------------------------- helpers


def _series(request: ForecastRequest) -> tuple[np.ndarray, np.ndarray]:
    """Return (values with NaN for unusable buckets, applicable mask)."""
    step = timedelta(seconds=request.bucketSeconds)
    starts = [b.startUtc for b in request.history]
    if len(set(starts)) != len(starts):
        raise ForecastInputError("duplicate bucket timestamps")
    for prev, nxt in itertools.pairwise(starts):
        if nxt - prev != step:
            raise ForecastInputError("history buckets must form a contiguous grid")
    values = np.array(
        [b.value if b.status == BucketStatus.OBSERVED and b.value is not None else np.nan for b in request.history],
        dtype=float,
    )
    applicable = np.array([b.status != BucketStatus.NOT_APPLICABLE for b in request.history])
    return values, applicable


def _max_gap(values: np.ndarray, applicable: np.ndarray) -> int:
    longest = current = 0
    for value, ok in zip(values, applicable, strict=True):
        if ok and math.isnan(value):
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _residual_quantiles(residuals: list[tuple[int, float]], steps: int, nominal: float):
    """Empirical residual quantiles, grouped into near/mid/far horizon thirds when each group
    has enough support, otherwise pooled. Returns per-step (low, high) offsets."""
    lo_q, hi_q = (1 - nominal) / 2, 1 - (1 - nominal) / 2
    pooled = np.array([r for _, r in residuals])
    groups = [range(0, steps // 3 or 1), range(steps // 3 or 1, 2 * steps // 3 or 1), range(2 * steps // 3 or 1, steps)]
    per_step: list[tuple[float, float]] = []
    for h in range(steps):
        group = next(g for g in groups if h in g) if any(h in g for g in groups) else range(steps)
        sample = np.array([r for step, r in residuals if step in group])
        source = sample if sample.size >= 20 else pooled
        per_step.append((float(np.quantile(source, lo_q)), float(np.quantile(source, hi_q))))
    return per_step


def _metrics(
    family: str,
    params: dict,
    test: list[tuple[int, float, float]],  # (step, actual, forecast)
    bands: list[tuple[float, float]] | None,
    baseline_mae: float | None,
    high_threshold: float,
    policy: ForecastPolicy,
    test_folds: int,
) -> EvaluationMetrics:
    reasons: list[str] = []
    if not test or test_folds < policy.min_test_folds:
        return EvaluationMetrics(
            family=family,
            params=params,
            testSamples=len(test),
            mae=None,
            rmse=None,
            meanBias=None,
            baselineSkill=None,
            pinballUpper=None,
            intervalCoverage=None,
            meanIntervalWidth=None,
            highDemandUnderpredictionRate=None,
            qualified=False,
            rejectionReasons=["insufficient_effective_folds"],
        )
    actual = np.array([a for _, a, _ in test])
    pred = np.array([f for _, _, f in test])
    err = actual - pred
    mae = float(np.mean(np.abs(err)))
    rmse = float(math.sqrt(np.mean(err**2)))
    bias = float(np.mean(pred - actual))
    skill = None
    if baseline_mae is not None and baseline_mae > 0:
        skill = 1 - mae / baseline_mae
    coverage = width = pinball = under_rate = None
    if bands is not None:
        lowers = np.array([max(0.0, f + bands[s][0]) for s, _, f in test])
        uppers = np.array([f + bands[s][1] for s, _, f in test])
        coverage = float(np.mean((actual >= lowers) & (actual <= uppers)))
        width = float(np.mean(uppers - lowers))
        q = 1 - (1 - policy.nominal_coverage) / 2
        diff = actual - uppers
        pinball = float(np.mean(np.maximum(q * diff, (q - 1) * diff)))
        # Share of all held-out samples that were high-demand AND above the upper bound.
        # (Measuring only within high-demand points would flag ~50% even for pure noise.)
        under_rate = float(np.mean((actual >= high_threshold) & (actual > uppers)))
        if coverage < policy.nominal_coverage - policy.coverage_tolerance:
            reasons.append("interval_calibration_failed")
        if under_rate > policy.high_demand_allowance:
            reasons.append("high_demand_underprediction")
    else:
        reasons.append("interval_calibration_failed")
    if family != "persistence" and (skill is None or skill < policy.min_baseline_skill):
        reasons.append("backtest_below_baseline")
    return EvaluationMetrics(
        family=family,
        params=params,
        testSamples=len(test),
        mae=mae,
        rmse=rmse,
        meanBias=bias,
        baselineSkill=skill,
        pinballUpper=pinball,
        intervalCoverage=coverage,
        meanIntervalWidth=width,
        highDemandUnderpredictionRate=under_rate,
        qualified=not reasons,
        rejectionReasons=reasons,
    )


# --------------------------------------------------------------------------- eligibility


def _pre_eligibility(request: ForecastRequest, policy: ForecastPolicy) -> tuple[list[str], dict[str, str]]:
    reasons: list[str] = []
    axes = {
        "identity": "resolved",
        "config": "stable",
        "source": "qualified",
        "history": "sufficient",
        "horizon": "supported",
    }

    for flag in request.sourceQualityFlags:
        if flag not in FLAG_REFUSALS:
            reasons.append("unclassified_quality_issue")
            axes["source"] = "rejected"
            continue
        if flag in PEAK_ONLY_FLAGS and request.statistic != ForecastStatistic.BUCKET_MAX_SAMPLED:
            continue  # mean-demand forecast stays possible; limitation recorded on the result
        code = FLAG_REFUSALS[flag]
        if code is None:
            continue
        reasons.append(code)
        if code in {"identity_unresolved", "cluster_incarnation_mismatch"}:
            axes["identity"] = "unresolved"
        elif code in {"config_regime_incompatible", "hpa_unmodeled"}:
            axes["config"] = "incompatible"
        else:
            axes["source"] = "rejected"

    if request.horizon == Horizon.TWENTY_FOUR_HOURS and request.timeCompressed:
        # A compressed scenario "day" is synthetic time, never 24h of observation (S05 §10.2).
        reasons.append("insufficient_history")
        axes["history"] = "insufficient"
        axes["horizon"] = "unsupported"

    if request.bucketSeconds != policy.bucket_seconds:
        reasons.append("unsupported_target_or_horizon")
        axes["horizon"] = "unsupported"
    return reasons, axes


def _eligibility(
    policy: ForecastPolicy, axes: dict[str, str], reasons: list[str], backtest: str, interval: str, overall: str
) -> Eligibility:
    return Eligibility(
        sourceQuality=axes["source"],  # type: ignore[arg-type]
        identityIntegrity=axes["identity"],  # type: ignore[arg-type]
        configurationStability=axes["config"],  # type: ignore[arg-type]
        historySupport=axes["history"],  # type: ignore[arg-type]
        backtest=backtest,  # type: ignore[arg-type]
        intervalCalibration=interval,  # type: ignore[arg-type]
        horizonSupport=axes["horizon"],  # type: ignore[arg-type]
        overall=overall,  # type: ignore[arg-type]
        decisionEligibility="eligible_for_named_evaluation_only" if overall == "eligible" else "none",
        policyRevision=policy.revision,
        reasonCodes=sorted(set(reasons)),
    )


# --------------------------------------------------------------------------- main entry


def forecast(request: ForecastRequest) -> ForecastResult:
    policy = policy_for(request.horizon, request.resource, request.statistic)
    unit = "millicore" if request.resource == ForecastResource.CPU else "byte"
    input_digest = digest(request)
    forecast_id = digest({"input": input_digest, "engine": "s14-engine-v1", "models": m.MODEL_REGISTRY_REVISION})

    def refused(reasons, axes, backtest="not_run", interval="unavailable", candidates=(), origins=(), status="refused"):
        return ForecastResult(
            forecastId=forecast_id,
            status=status,
            request=request,
            unit=unit,
            predictions=[],
            uncertainty=None,
            selectedModel=None,
            candidates=list(candidates),
            eligibility=_eligibility(
                policy, axes, reasons, backtest, interval, "not_applicable" if status == "not_applicable" else "refused"
            ),
            foldOriginsUtc=list(origins),
            inputDigest=input_digest,
        )

    axes0 = {
        "identity": "resolved",
        "config": "stable",
        "source": "qualified",
        "history": "sufficient",
        "horizon": "supported",
    }

    # W05/W06 have no continuous demand: a suspended CronJob is not "zero demand" (F08).
    entry = CATALOG.get(request.logicalWorkloadRef)
    if entry is not None and not entry.continuous:
        return refused(["missing_conditional_execution_assumption"], axes0, status="not_applicable")

    values, applicable = _series(request)
    if request.history:
        last_end = request.history[-1].startUtc + timedelta(seconds=request.bucketSeconds)
        if last_end > request.asOfUtc:
            return refused(["future_information_leakage"], {**axes0, "source": "rejected"})
        if last_end != request.asOfUtc:
            return refused(["temporal_alignment_invalid"], {**axes0, "source": "rejected"})

    reasons, axes = _pre_eligibility(request, policy)

    applicable_count = int(applicable.sum())
    observed = int((~np.isnan(values) & applicable).sum())
    history_seconds = applicable_count * request.bucketSeconds
    if history_seconds < policy.min_history_seconds:
        reasons.append("insufficient_history")
        axes["history"] = "insufficient"
    coverage = observed / applicable_count if applicable_count else 0.0
    if coverage < policy.min_coverage:
        reasons.append("coverage_below_policy")
        axes["source"] = "rejected"
    if _max_gap(values, applicable) > policy.max_gap_buckets:
        reasons.append(
            "memory_spike_fidelity_insufficient"
            if request.resource == ForecastResource.MEMORY
            else "coverage_below_policy"
        )
        axes["source"] = "rejected"
    censored = sum(1 for b in request.history if b.status == BucketStatus.CENSORED)
    if applicable_count and censored / applicable_count > policy.max_censored_fraction:
        reasons.append("demand_censored")
        axes["source"] = "rejected"
    if reasons:
        return refused(reasons, axes)

    # ------------------------------------------------------------ walk-forward backtest
    grid = bucket_grid(request.asOfUtc, request.horizon, request.bucketSeconds)
    steps = len(grid)
    season = policy.season_period if (policy.season_period and not request.timeCompressed) else None
    origins = list(range(policy.min_train_buckets, len(values) - steps + 1, policy.origin_step_buckets))
    origins = origins[-policy.max_folds :]
    n_cal = len(origins) // 2
    cal_origins, test_origins = origins[:n_cal], origins[n_cal:]
    origin_times = [request.history[o].startUtc for o in origins]
    if len(cal_origins) < policy.min_calibration_folds or len(test_origins) < policy.min_test_folds:
        return refused(["insufficient_effective_folds"], {**axes, "history": "insufficient"}, origins=origin_times)

    families = [
        mdl.family
        for mdl in m.fit_candidates(values[: origins[0]], rolling_window=policy.rolling_window, season_period=season)
    ]
    cal_res: dict[str, list[tuple[int, float]]] = {f: [] for f in families}
    test_pts: dict[str, list[tuple[int, float, float]]] = {f: [] for f in families}
    folds_seen: dict[str, int] = {f: 0 for f in families}
    for origin in origins:
        train, actual = values[:origin], values[origin : origin + steps]
        for family in families:
            fitted = m.fit_family(train, family, rolling_window=policy.rolling_window, season_period=season)
            if fitted is None:
                continue
            pred = fitted.predict(steps)
            for h in range(steps):
                if math.isnan(actual[h]) or not applicable[origin + h]:
                    continue  # missing actuals are excluded from the denominator, never scored as 0
                if origin in cal_origins:
                    cal_res[family].append((h, float(actual[h] - pred[h])))
                else:
                    test_pts[family].append((h, float(actual[h]), float(pred[h])))
            if origin in test_origins:
                folds_seen[family] += 1

    train_valid = values[: origins[0]][~np.isnan(values[: origins[0]])]
    high_threshold = float(np.quantile(train_valid, 0.9)) if train_valid.size else math.inf
    baseline_mae = None
    if test_pts.get("persistence"):
        baseline_mae = float(np.mean([abs(a - f) for _, a, f in test_pts["persistence"]]))

    candidates: list[EvaluationMetrics] = []
    bands_by_family: dict[str, list[tuple[float, float]]] = {}
    for family in families:
        bands = None
        if len(cal_res[family]) >= 20:
            bands = _residual_quantiles(cal_res[family], steps, policy.nominal_coverage)
            bands_by_family[family] = bands
        fitted_full = m.fit_family(values, family, rolling_window=policy.rolling_window, season_period=season)
        params = fitted_full.params if fitted_full else {}
        candidates.append(
            _metrics(family, params, test_pts[family], bands, baseline_mae, high_threshold, policy, folds_seen[family])
        )

    qualified = [c for c in candidates if c.qualified]
    if not qualified:
        codes = sorted({r for c in candidates for r in c.rejectionReasons}) or ["backtest_below_baseline"]
        interval = "failed" if "interval_calibration_failed" in codes else "unavailable"
        return refused(codes, axes, backtest="failed", interval=interval, candidates=candidates, origins=origin_times)

    best = min(qualified, key=lambda c: (round(c.mae or 0.0, 9), m.SIMPLICITY[c.family]))
    final = m.fit_family(values, best.family, rolling_window=policy.rolling_window, season_period=season)
    assert final is not None
    point = final.predict(steps)
    bands = bands_by_family[best.family]
    predictions = [
        PredictionBucket(
            startUtc=start,
            endUtc=end,
            point=max(0.0, float(point[h])),
            lower=max(0.0, float(point[h] + bands[h][0])),
            upper=max(0.0, float(point[h] + bands[h][1])),
        )
        for h, (start, end) in enumerate(grid)
    ]
    limitations = ["autocorrelated_residuals_possible", "nonnegative_truncation_applied"]
    if request.resource == ForecastResource.MEMORY:
        limitations.append("sampled_working_set_not_true_peak")
    if any(f in PEAK_ONLY_FLAGS for f in request.sourceQualityFlags):
        limitations.append("memory_peak_not_guaranteed_not_sole_downsize_proof")
    uncertainty = Uncertainty(
        nominalCoverage=policy.nominal_coverage,
        method="empirical_residual_quantiles_walk_forward",
        calibrationSampleCount=len(cal_res[best.family]),
        empiricalCoverage=best.intervalCoverage,
        effectiveTestSamples=best.testSamples,
        meanIntervalWidth=best.meanIntervalWidth,
        limitations=limitations,
    )
    return ForecastResult(
        forecastId=forecast_id,
        status="available",
        request=request,
        unit=unit,
        predictions=predictions,
        uncertainty=uncertainty,
        selectedModel=best,
        candidates=candidates,
        eligibility=_eligibility(policy, axes, [], "passed", "validated", "eligible"),
        foldOriginsUtc=origin_times,
        inputDigest=input_digest,
    )
